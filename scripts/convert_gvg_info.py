import argparse
import json
import sqlite3
from datetime import datetime
from pathlib import Path


INFO_FORMAT = 'gvg_info_v1'
DEFAULT_DB = (
    Path(__file__).resolve().parents[1] / 'HoshinoBot' / 'data' / 'data.db'
)


def structured_info(text):
    return json.dumps(
        {
            'format': INFO_FORMAT,
            'segments': [{'type': 'text', 'content': text}],
        },
        ensure_ascii=False,
        separators=(',', ':'),
    )


def is_current_format(value):
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return False
    return (
        isinstance(data, dict)
        and data.get('format') == INFO_FORMAT
        and isinstance(data.get('segments'), list)
    )


def backup_database(conn, db_path):
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = db_path.with_name(
        '{}.before-info-v1-{}.bak'.format(db_path.name, timestamp))
    backup = sqlite3.connect(str(backup_path))
    try:
        conn.backup(backup)
    finally:
        backup.close()
    return backup_path


def convert(db_path, dry_run=False):
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError('找不到数据库：{}'.format(db_path))

    conn = sqlite3.connect(str(db_path), timeout=30)
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='table' AND name='gvg_members'"
        ).fetchone()
        if not table:
            raise RuntimeError('数据库中没有 gvg_members 表')

        rows = conn.execute(
            'SELECT cuid, info FROM gvg_members WHERE info IS NOT NULL'
        ).fetchall()
        pending = []
        empty = []
        skipped = 0
        for cuid, info in rows:
            info = str(info)
            if is_current_format(info):
                skipped += 1
            elif info:
                pending.append((structured_info(info), int(cuid)))
            else:
                empty.append(int(cuid))

        if dry_run:
            return {
                'converted': len(pending),
                'emptied': len(empty),
                'skipped': skipped,
                'backup': None,
            }

        backup_path = backup_database(conn, db_path)
        conn.execute('BEGIN IMMEDIATE')
        conn.executemany(
            'UPDATE gvg_members SET info=? WHERE cuid=?', pending)
        conn.executemany(
            'UPDATE gvg_members SET info=NULL, info_date=NULL WHERE cuid=?',
            [(cuid,) for cuid in empty],
        )
        conn.commit()
        return {
            'converted': len(pending),
            'emptied': len(empty),
            'skipped': skipped,
            'backup': backup_path,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description='将 gvg_members.info 的旧版纯文字转换为 gvg_info_v1 JSON。')
    parser.add_argument(
        '--db', type=Path, default=DEFAULT_DB,
        help='data.db 路径（默认：%(default)s）')
    parser.add_argument(
        '--dry-run', action='store_true',
        help='只统计，不修改数据库，也不创建备份')
    args = parser.parse_args()

    result = convert(args.db, dry_run=args.dry_run)
    print('转换 {} 条，清理空信息 {} 条，跳过新版数据 {} 条。'.format(
        result['converted'], result['emptied'], result['skipped']))
    if result['backup']:
        print('备份：{}'.format(result['backup']))
    elif args.dry_run:
        print('当前为 dry-run，数据库未修改。')


if __name__ == '__main__':
    main()
