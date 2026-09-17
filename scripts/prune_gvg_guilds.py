"""Preview or remove rounds involving infrequently observed guilds (stdlib only)."""

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3


DEFAULT_DB = Path(__file__).resolve().parents[1] / 'HoshinoBot/data/data.db'


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError('门槛必须是正整数')
    return number


def prepare_candidates(conn, min_days=None, min_battles=None):
    # Count both sides together; two rounds of one battle count only once.
    setup_sql = '''
        CREATE TEMP TABLE guild_counts AS
        WITH appearances AS (
            SELECT trim(atk_guild) AS guild, battle_id, start_ts FROM gvg_rounds
            UNION ALL
            SELECT trim(def_guild), battle_id, start_ts FROM gvg_rounds
        )
        SELECT guild, COUNT(DISTINCT battle_id) AS battles,
               COUNT(DISTINCT date(start_ts / 1000.0, 'unixepoch')) AS days
        FROM appearances
        WHERE guild IS NOT NULL AND guild != ''
        GROUP BY guild;
        CREATE UNIQUE INDEX temp.idx_guild_counts ON guild_counts(guild);
        CREATE TEMP TABLE rare_guilds (guild TEXT PRIMARY KEY);
        CREATE TEMP TABLE doomed_rounds (
            battle_id TEXT, round_idx INTEGER,
            PRIMARY KEY (battle_id, round_idx)
        );
    '''
    for statement in setup_sql.split(';'):
        if statement.strip():
            conn.execute(statement)
    column, threshold = ('days', min_days) if min_days is not None else ('battles', min_battles)
    conn.execute(
        f'INSERT INTO rare_guilds SELECT guild FROM guild_counts WHERE {column} < ?',
        (threshold,),
    )
    conn.execute('''
        INSERT INTO doomed_rounds
        SELECT battle_id, round_idx FROM gvg_rounds
        WHERE trim(atk_guild) IN (SELECT guild FROM rare_guilds)
           OR trim(def_guild) IN (SELECT guild FROM rare_guilds)
    ''')
    return conn.execute('''
        SELECT guild, days, battles FROM guild_counts
        WHERE guild IN (SELECT guild FROM rare_guilds)
        ORDER BY days, battles, guild
    ''').fetchall()


MATCH = '''EXISTS (
    SELECT 1 FROM doomed_rounds AS d
    WHERE d.battle_id = {table}.battle_id AND d.round_idx = {table}.round_idx
)'''


def prune(db_path, min_days=None, min_battles=None, apply=False):
    db_path = Path(db_path).resolve()
    if not db_path.is_file():
        raise FileNotFoundError('数据库不存在：{}'.format(db_path))
    if min_days is None and min_battles is None:
        min_days = 3
    if (min_days is not None and min_battles is not None
            or (min_days if min_days is not None else min_battles) < 1):
        raise ValueError('请选择一个正整数门槛')

    # Keep selection, backup and deletion under the same transaction.
    mode = 'rw' if apply else 'ro'
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=' + mode,
                                uri=True, timeout=30, isolation_level=None)) as conn:
        conn.execute('PRAGMA foreign_keys = ON')
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        try:
            guilds = prepare_candidates(conn, min_days, min_battles)
            rounds = conn.execute('SELECT COUNT(*) FROM doomed_rounds').fetchone()[0]
            units = conn.execute('SELECT COUNT(*) FROM gvg_units WHERE ' +
                                 MATCH.format(table='gvg_units')).fetchone()[0]
            unit_label, threshold = ('天（UTC）', min_days) if min_days is not None else ('场不同战斗', min_battles)
            print('数据库：{}'.format(db_path))
            print('合并攻防，按全库统计；筛选少于 {} {}的公会。'.format(threshold, unit_label))
            for guild, days, battles in guilds:
                print('  {}：{} 天 / {} 场'.format(guild, days, battles))
            print('命中 {} 个公会，{} 条回合，{} 条角色数据。'.format(len(guilds), rounds, units))
            if not apply or not rounds:
                print('仅预览，未删除数据。' if not apply else '无需删除。')
                conn.rollback()
                return

            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup_path = db_path.with_name(db_path.name + '.before-prune-' + stamp + '.bak')
            # A separate reader can back up while this connection holds the write reservation.
            with backup_path.open('xb'):
                pass
            with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(str(backup_path))) as backup:
                    source.backup(backup)
            print('完整备份：{}'.format(backup_path))
            conn.execute('DELETE FROM gvg_units WHERE ' + MATCH.format(table='gvg_units'))
            conn.execute('DELETE FROM gvg_rounds WHERE ' + MATCH.format(table='gvg_rounds'))
            conn.commit()
            print('已删除 {} 条回合及 {} 条角色数据。'.format(rounds, units))
        except BaseException:
            conn.rollback()
            raise


def main():
    parser = argparse.ArgumentParser(description='清理低频公会的团战数据；默认预览，不删除。')
    parser.add_argument('--db', type=Path, default=DEFAULT_DB, help='data.db 路径')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--min-days', type=positive_int, help='保留至少出现这么多天的公会（UTC，默认 3）')
    group.add_argument('--min-battles', type=positive_int, help='保留至少出现这么多场不同战斗的公会')
    parser.add_argument('--apply', action='store_true', help='自动备份后执行删除')
    args = parser.parse_args()
    try:
        prune(args.db, args.min_days, args.min_battles, args.apply)
    except (OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, '清理失败：{}\n'.format(exc))


if __name__ == '__main__':
    main()
