"""Create a compact core database from an old data.db, preserving a full backup.

Only Python's standard library is required. The source is never modified.
"""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from HoshinoBot.schema import SCHEMA_SQL


CORE_TABLES = (
    'gvg_members', 'gvg_current_members', 'gvg_snapshots',
    'gvg_rounds', 'gvg_units', 'plugin_meta',
)


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def readonly_uri(path):
    return path.resolve().as_uri() + '?mode=ro'


def check_integrity(conn):
    for row in conn.execute('PRAGMA integrity_check'):
        if row[0] != 'ok':
            raise ValueError('数据库完整性检查失败：' + str(row[0]))


def migrate(source, output, backup=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise ValueError('源数据库不存在：' + str(source))
    if backup is None:
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        backup = source.with_name(source.name + '.backup-' + stamp + '-' + uuid.uuid4().hex[:8])
    backup = Path(backup).resolve()
    if len({source, output, backup}) != 3:
        raise ValueError('源数据库、输出和备份必须使用不同路径')
    if output.exists() or backup.exists():
        raise ValueError('输出或备份文件已存在，拒绝覆盖')
    if not output.parent.is_dir() or not backup.parent.is_dir():
        raise ValueError('输出和备份目录必须已存在')

    # SQLite's backup API includes committed WAL data in a consistent snapshot.
    # Exclusively reserve the name to avoid overwriting existing files.
    with backup.open('xb'):
        pass
    with closing(sqlite3.connect(readonly_uri(source), uri=True)) as old, \
            closing(sqlite3.connect(backup)) as saved:
        old.backup(saved, pages=256)
        check_integrity(saved)
    print('完整旧库备份：' + str(backup), flush=True)

    with output.open('xb'):
        pass
    try:
        with closing(sqlite3.connect(output, uri=True)) as conn:
            conn.execute('PRAGMA cache_size=-2048')
            conn.execute('PRAGMA temp_store=FILE')
            conn.executescript(SCHEMA_SQL)
            conn.execute('ATTACH DATABASE ? AS legacy', (readonly_uri(backup),))
            old_tables = {row[0] for row in conn.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type='table'")}
            counts = {}
            with conn:
                for table in CORE_TABLES:
                    origin = table
                    if table == 'gvg_current_members' and table not in old_tables:
                        origin = 'gvg_defences'
                    if table == 'plugin_meta' and table not in old_tables:
                        origin = 'pvp_meta'
                    if origin not in old_tables:
                        if table in ('gvg_current_members', 'plugin_meta'):
                            counts[table] = 0
                            continue
                        raise ValueError('源库缺少核心表：' + table)
                    columns = [row[1] for row in conn.execute(
                        'PRAGMA main.table_info(' + quote(table) + ')')]
                    old_columns = {row[1] for row in conn.execute(
                        'PRAGMA legacy.table_info(' + quote(origin) + ')')}
                    missing = set(columns) - old_columns
                    if missing:
                        raise ValueError(origin + ' 缺少字段：' + ', '.join(sorted(missing)))
                    fields = ', '.join(map(quote, columns))
                    conn.execute(f'INSERT INTO main.{quote(table)} ({fields}) '
                                 f'SELECT {fields} FROM legacy.{quote(origin)}')
                    counts[table] = conn.execute(
                        f'SELECT COUNT(*) FROM main.{quote(table)}').fetchone()[0]
                    # Compare every retained value, entirely within SQLite.
                    for left, right in ((f'main.{quote(table)}', f'legacy.{quote(origin)}'),
                                        (f'legacy.{quote(origin)}', f'main.{quote(table)}')):
                        if conn.execute(f'SELECT {fields} FROM {left} EXCEPT '
                                        f'SELECT {fields} FROM {right} LIMIT 1').fetchone():
                            raise ValueError(table + ' 数据校验失败')
                    old_count = conn.execute(
                        f'SELECT COUNT(*) FROM legacy.{quote(origin)}').fetchone()[0]
                    if counts[table] != old_count:
                        raise ValueError(table + ' 行数校验失败')
                # Match the live plugin's older metadata migration behavior.
                if 'pvp_meta' in old_tables:
                    conn.execute('INSERT OR IGNORE INTO main.plugin_meta(key,value) '
                                 'SELECT key,value FROM legacy.pvp_meta')
                if 'gvg_defences' in old_tables:
                    marker = conn.execute("SELECT value FROM plugin_meta WHERE key='legacy_gvg_defences_migrated'").fetchone()
                    if not marker:
                        if not counts['gvg_current_members']:
                            cols = ','.join(quote(row[1]) for row in conn.execute(
                                'PRAGMA main.table_info(gvg_current_members)'))
                            conn.execute(f'INSERT INTO main.gvg_current_members({cols}) '
                                         f'SELECT {cols} FROM legacy.gvg_defences')
                        conn.execute("INSERT INTO plugin_meta VALUES ('legacy_gvg_defences_migrated', 'done')")
                for table in CORE_TABLES:
                    counts[table] = conn.execute(f'SELECT COUNT(*) FROM {quote(table)}').fetchone()[0]
            check_integrity(conn)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('外键校验失败，原库可能存在缺失的玩家记录')
            # Core indexes are built by SCHEMA_SQL; optimize the query planner.
            conn.execute('ANALYZE main')
            conn.commit()
        return {'source': str(source), 'output': str(output), 'backup': str(backup),
                'counts': counts, 'excluded_tables': sorted(old_tables - set(CORE_TABLES) - {'sqlite_sequence'})}
    except BaseException:
        # Only remove the new file exclusively created by this invocation.
        output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description='备份旧团战数据库并生成精简新版；不修改源库。')
    parser.add_argument('source', type=Path, help='旧 data.db 路径（不是 master.db）')
    parser.add_argument('--output', type=Path, help='新库路径，默认同目录 data.core.db')
    parser.add_argument('--backup', type=Path, help='完整备份路径，默认自动生成唯一名称')
    args = parser.parse_args()
    output = args.output or args.source.with_name(args.source.stem + '.core.db')
    try:
        result = migrate(args.source, output, args.backup)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, '迁移失败：' + str(exc) + '\n源数据库未修改；已生成的备份保留。\n')
    for table, count in result['counts'].items():
        print(f'{table}: {count} 行')
    print('仅保留在完整备份中的旧表：' + ('、'.join(result['excluded_tables']) or '无'))
    print('校验通过，新数据库：' + result['output'])
    print('旧情报字段不进入新库，所有日期的战斗和快照均保留。')
    print('请停止机器人后替换数据库；若迁移时机器人仍在写入，请停机后重新迁移。')


if __name__ == '__main__':
    main()
