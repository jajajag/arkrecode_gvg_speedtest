"""Offline, standard-library-only migration to the six-table database."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sqlite3
import uuid

from HoshinoBot.database import (
    SCHEMA_SQL, MASTER_DB_PATH, EQUIP_COLUMNS, card_equipment,
    save_equipment_rows, insert_defence,
)

CORE_TABLES = ('plugin_meta', 'gvg_rounds', 'gvg_units', 'pvp_equips', 'gvg_defence', 'gvg_defence_units')


def readonly_uri(path):
    return Path(path).resolve().as_uri() + '?mode=ro'


def tables(conn, schema='legacy'):
    return {r[0] for r in conn.execute(f"SELECT name FROM {schema}.sqlite_master WHERE type='table'")}


def columns(conn, table, schema='main'):
    return [r[1] for r in conn.execute(f'PRAGMA {schema}.table_info({table})')]


def check_integrity(conn):
    for row in conn.execute('PRAGMA main.integrity_check'):
        if row[0] != 'ok':
            raise ValueError('数据库完整性检查失败：' + str(row[0]))


def put_meta(conn, key, value):
    conn.execute('INSERT OR REPLACE INTO main.plugin_meta VALUES (?,?)', (key, value))


def match_meta(conn, date, guild_id='', guild_name=''):
    put_meta(conn, date, json.dumps({
        'date': date, 'enemy_guild_id': str(guild_id), 'enemy_guild_name': str(guild_name),
    }, ensure_ascii=False, separators=(',', ':')))


def copy_exact(conn, table):
    fields = ','.join(columns(conn, table))
    conn.execute(f'INSERT INTO main.{table} ({fields}) SELECT {fields} FROM legacy.{table}')
    before = conn.execute(f'SELECT COUNT(*) FROM legacy.{table}').fetchone()[0]
    after = conn.execute(f'SELECT COUNT(*) FROM main.{table}').fetchone()[0]
    if before != after:
        raise ValueError(table + ' 行数校验失败')


def import_equips(conn, schema):
    if 'pvp_equips' not in tables(conn, schema):
        return 0
    source_columns = set(columns(conn, 'pvp_equips', schema))
    missing = set(EQUIP_COLUMNS) - source_columns
    if missing:
        raise ValueError('旧 pvp_equips 缺少字段：' + ','.join(sorted(missing)))
    fields = ','.join(EQUIP_COLUMNS)
    count = 0
    for row in conn.execute(f'SELECT {fields} FROM {schema}.pvp_equips'):
        if not row['equip_id'] or row['cuid'] is None:
            raise ValueError('旧 pvp_equips 存在无装备 ID 或无 UID 的记录，拒绝丢弃')
        save_equipment_rows(conn, [tuple(row)])
        count += 1
    if conn.execute(f'SELECT equip_id FROM {schema}.pvp_equips EXCEPT '
                    'SELECT equip_id FROM main.pvp_equips LIMIT 1').fetchone():
        raise ValueError('旧装备 ID 校验失败')
    return count


def import_defence_rows(conn, master_path, notices):
    """Convert already flattened records; lost raw values cannot be recovered."""
    old_format = 'match_id' in columns(conn, 'gvg_defence', 'legacy')
    names = None
    missing_values = 0
    unit_columns = columns(conn, 'gvg_defence_units')
    for parent in conn.execute('SELECT * FROM legacy.gvg_defence ORDER BY id'):
        # The latest record wins if the old schema allowed multiple records per day.
        conn.execute('DELETE FROM main.gvg_defence WHERE match_date=? AND cuid=?',
                     (parent['match_date'], parent['cuid']))
        cursor = conn.execute('INSERT INTO main.gvg_defence(match_date,cuid,name,avatar_role_id) '
                              'VALUES (?,?,?,?)', tuple(parent[k] for k in
                              ('match_date', 'cuid', 'name', 'avatar_role_id')))
        for old in conn.execute('SELECT * FROM legacy.gvg_defence_units WHERE defence_id=?', (parent['id'],)):
            unit = dict(old)
            unit['defence_id'] = cursor.lastrowid
            unit['team'] = unit.get('team', unit.get('half'))
            unit['skill_levels'] = ','.join((unit.get('skill_levels') or '').split(',')[:3])
            text = unit.get('sets') or ''
            if text and not re.fullmatch(r'[A-Za-z0-9_,]+', text):
                if names is None:
                    with closing(sqlite3.connect(readonly_uri(master_path), uri=True)) as master:
                        names = {name.removesuffix('套装'): key for name, key in master.execute(
                            'SELECT COALESCE(c.Value,e.Name,e.ID),e.ID FROM EquipmentSet e '
                            'LEFT JOIN CHS c ON c.Key=e.Name')}
                result = []
                while text:
                    number = re.match(r'\d+', text)
                    count = int(number[0]) if number else 1
                    text = text[len(number[0]):] if number else text
                    label = next((name for name in sorted(names, key=len, reverse=True)
                                  if text.startswith(name)), None)
                    if label is None:
                        raise ValueError('无法识别旧套装文字：' + text)
                    result.extend([names[label]] * count)
                    text = text[len(label):]
                unit['sets'] = ','.join(result)
            if old_format:
                for part in ('shoes', 'ring', 'necklace'):
                    if unit[part + '_value'] == 0:
                        unit[part + '_value'] = None
                        missing_values += 1
            conn.execute('INSERT INTO main.gvg_defence_units (' + ','.join(unit_columns) + ') VALUES ('
                         + ','.join('?' for _ in unit_columns) + ')', tuple(unit[k] for k in unit_columns))
    if old_format:
        notices.append('旧明细缺少原始装备数据，无法重算历史血量；要修复血量请从含完整防守 JSON 的旧备份迁移。')
    if missing_values:
        notices.append(f'旧明细有 {missing_values} 项主属性为 0，已标为 NULL（未知）；恢复准确数值需要原始快照。')


def import_snapshots(conn, old_tables, old_meta, master_path):
    counts = {'defence': 0, 'equipment': 0}
    if 'gvg_snapshots' not in old_tables:
        return counts
    has_members = 'gvg_members' in old_tables
    for row in conn.execute('SELECT * FROM legacy.gvg_snapshots ORDER BY snapshot_date DESC'):
        if row['kind'] not in counts:
            raise ValueError('未知快照类型：' + row['kind'])
        payload = json.loads(row['payload'])
        cuid, date = int(row['cuid']), row['snapshot_date']
        info = payload.get('PlayerInfo') or (payload.get('BattleSupportData') or {}).get('PlayerInfo') or {}
        member = conn.execute('SELECT name, avatar_role_id FROM legacy.gvg_members WHERE cuid=?',
                              (cuid,)).fetchone() if has_members else None
        name = str(info.get('Name') or (member['name'] if member else cuid))
        if row['kind'] == 'equipment':
            save_equipment_rows(conn, card_equipment(payload, cuid, name))
        else:
            team = payload.get('DefenceTeamData')
            if not isinstance(team, dict):
                raise ValueError(f'{date} UID {cuid} 的防守快照缺少 DefenceTeamData')
            guild = info.get('GuildSubInfo') or {}
            guild_id = guild.get('_id') or ''
            if isinstance(guild_id, dict):
                guild_id = guild_id.get('$oid') or ''
            guild_name = guild.get('Name') or ''
            if date == old_meta.get('current_match_date'):
                guild_id = old_meta.get('current_enemy_guild_id') or guild_id
                guild_name = old_meta.get('current_enemy_guild_name') or guild_name
            match_meta(conn, date, guild_id, guild_name)
            avatar = str(info.get('LeaderSID') or (member['avatar_role_id'] if member else '') or '')
            insert_defence(conn, date, cuid, name, avatar, team, master_path)
        counts[row['kind']] += 1
    return counts


def migrate(source, output, backup=None, equipment_source=None, master_path=MASTER_DB_PATH):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise ValueError('源数据库不存在：' + str(source))
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path(backup).resolve() if backup else source.with_name(
        source.name + '.backup-' + stamp + '-' + uuid.uuid4().hex[:8])
    if len({source, output, backup}) != 3 or output.exists() or backup.exists():
        raise ValueError('源、输出、备份必须不同，且输出及备份不能已存在')
    if equipment_source:
        equipment_source = Path(equipment_source).resolve()
        if not equipment_source.is_file() or equipment_source in {output, backup}:
            raise ValueError('装备补充来源必须是已存在的独立数据库')
    with backup.open('xb'):
        pass
    with closing(sqlite3.connect(readonly_uri(source), uri=True)) as old, \
            closing(sqlite3.connect(backup)) as saved:
        old.backup(saved, pages=256)
        check_integrity(saved)
    print('完整旧库备份：' + str(backup), flush=True)
    with output.open('xb'):
        pass
    notices = []
    try:
        with closing(sqlite3.connect(output, uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('PRAGMA foreign_keys=ON')
            conn.execute('PRAGMA cache_size=-2048')
            conn.execute('PRAGMA temp_store=FILE')
            conn.executescript(SCHEMA_SQL)
            conn.execute('ATTACH DATABASE ? AS legacy', (readonly_uri(backup),))
            old_tables = tables(conn)
            if not {'gvg_rounds', 'gvg_units'} <= old_tables:
                raise ValueError('源库缺少 gvg_rounds 或 gvg_units，请使用团战 data.db')
            with conn:
                for table in ('gvg_rounds', 'gvg_units'):
                    copy_exact(conn, table)
                old_meta = {}
                for table in ('pvp_meta', 'plugin_meta'):
                    if table in old_tables:
                        old_meta.update((r['key'], r['value']) for r in conn.execute(
                            f'SELECT key,value FROM legacy.{table}'))
                if 'master_catalog' in old_meta:
                    put_meta(conn, 'master_catalog', old_meta['master_catalog'])
                for key, value in old_meta.items():
                    if key.startswith('match:') or re.fullmatch(r'\d{4}-\d{2}-\d{2}', key):
                        data = json.loads(value)
                        date = data.get('date') or key.removeprefix('match:')[:10]
                        match_meta(conn, date, data.get('enemy_guild_id', ''), data.get('enemy_guild_name', ''))
                if old_meta.get('current_match_date') and (
                        old_meta.get('current_enemy_guild_id') or old_meta.get('current_enemy_guild_name')):
                    match_meta(conn, old_meta['current_match_date'],
                               old_meta.get('current_enemy_guild_id', ''), old_meta.get('current_enemy_guild_name', ''))
                imported = import_equips(conn, 'legacy')
                if equipment_source:
                    conn.execute('ATTACH DATABASE ? AS equipment_old', (readonly_uri(equipment_source),))
                    if 'pvp_equips' not in tables(conn, 'equipment_old'):
                        raise ValueError('装备补充库不存在 pvp_equips')
                    imported += import_equips(conn, 'equipment_old')
                if 'gvg_defence' in old_tables:
                    if 'team_data' in columns(conn, 'gvg_defence', 'legacy'):
                        for row in conn.execute('SELECT * FROM legacy.gvg_defence ORDER BY id'):
                            insert_defence(conn, row['match_date'], row['cuid'],
                                           row['name'], row['avatar_role_id'], json.loads(row['team_data']), master_path)
                    else:
                        import_defence_rows(conn, master_path, notices)
                snapshot_counts = import_snapshots(conn, old_tables, old_meta, master_path)
                if not imported:
                    notices.append('未导入旧 pvp_equips 记录；如此前迁移丢掉过此表，请用 --equipment-source 指定完整旧备份。')
                if not conn.execute('SELECT 1 FROM main.gvg_defence LIMIT 1').fetchone():
                    notices.append('原库没有可恢复的完整防守快照，待下次开战采集；不会用角色 ID 伪造装备情报。')
            conn.execute('DETACH DATABASE legacy')
            if equipment_source:
                conn.execute('DETACH DATABASE equipment_old')
            conn.execute('VACUUM')
            check_integrity(conn)
            if conn.execute('PRAGMA foreign_key_check').fetchone():
                raise ValueError('防守主表与角色明细的关联校验失败')
            counts = {table: conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                      for table in CORE_TABLES}
            indexes = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")]
        return {'output': str(output), 'backup': str(backup), 'counts': counts,
                'equipment_imported': imported, 'snapshots_converted': snapshot_counts,
                'indexes': indexes, 'notices': notices}
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser(description='备份旧库并生成六张业务表的新库，不修改原库。')
    parser.add_argument('source', type=Path, help='团战 data.db（或完整旧备份）')
    parser.add_argument('--output', type=Path, help='默认同目录 data.clean.db')
    parser.add_argument('--master-db', type=Path, help='迁移旧防守时用于计算生命，默认源库同目录 master.db')
    parser.add_argument('--backup', type=Path, help='完整备份路径，默认自动生成')
    parser.add_argument('--equipment-source', type=Path, help='可选：从先前完整备份补回 pvp_equips')
    args = parser.parse_args()
    try:
        result = migrate(args.source, args.output or args.source.with_name('data.clean.db'),
                         args.backup, args.equipment_source, args.master_db or args.source.parent / 'master.db')
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f'迁移失败：{exc}\n原库未修改，已生成的备份保留。\n')
    for table, count in result['counts'].items():
        print(f'{table}: {count} 行')
    print('导入旧装备记录：', result['equipment_imported'])
    print('转换旧快照：', result['snapshots_converted'])
    print('额外索引：', '、'.join(result['indexes']))
    for notice in result['notices']:
        print('提示：' + notice)
    print('校验通过，新数据库：' + result['output'])
    print('停机后替换 data.db；迁移期间若仍有写入，需停机后重新迁移。')


if __name__ == '__main__':
    main()
