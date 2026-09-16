import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

from .api import BASE_DIR, GameRequestError
from .schema import SCHEMA_SQL


DATA_DIR = BASE_DIR / 'data'
DATA_DB_PATH = DATA_DIR / 'data.db'
MASTER_DB_PATH = DATA_DIR / 'master.db'
ALIAS_PATH = DATA_DIR / 'character_dic.json'


def today():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def now_ms():
    return int(time.time() * 1000)


def connect_data(path=DATA_DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 30000')
    return conn


def init_database(path=DATA_DB_PATH):
    conn = connect_data(path)
    try:
        conn.executescript(SCHEMA_SQL)
        _migrate_legacy_tables(conn)
        conn.commit()
    finally:
        conn.close()


def _table_exists(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _migrate_legacy_tables(conn):
    if _table_exists(conn, 'pvp_meta'):
        conn.execute(
            '''
            INSERT OR IGNORE INTO plugin_meta(key, value)
            SELECT key, value FROM pvp_meta
            ''')
    if _table_exists(conn, 'gvg_defences'):
        migration_key = 'legacy_gvg_defences_migrated'
        if meta_get(conn, migration_key):
            return
        has_current_members = conn.execute(
            'SELECT 1 FROM gvg_current_members LIMIT 1'
        ).fetchone() is not None
        if has_current_members:
            meta_set(conn, migration_key, 'skipped')
            return
        conn.execute(
            '''
            INSERT OR IGNORE INTO gvg_current_members(
                cuid, snapshot_date, sort_order,
                upper_1_role_id, upper_2_role_id, upper_3_role_id,
                lower_1_role_id, lower_2_role_id, lower_3_role_id
            )
            SELECT
                cuid, snapshot_date, sort_order,
                upper_1_role_id, upper_2_role_id, upper_3_role_id,
                lower_1_role_id, lower_2_role_id, lower_3_role_id
            FROM gvg_defences
            ''')
        meta_set(conn, migration_key, 'done')


def meta_get(conn, key):
    row = conn.execute(
        'SELECT value FROM plugin_meta WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else None


def meta_set(conn, key, value):
    conn.execute(
        'INSERT INTO plugin_meta(key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(value)),
    )


def update_our_guild_meta(guild_info, db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        meta_set(conn, 'our_guild_id', guild_info.get('id') or '')
        meta_set(conn, 'our_guild_name', guild_info.get('name') or '')
        conn.commit()
    finally:
        conn.close()


def replace_current_members(members, enemy_guild,
                            db_path=DATA_DB_PATH, snapshot_date=None):
    init_database(db_path)
    snapshot_date = snapshot_date or today()
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        guild_id = str(enemy_guild.get('id') or '')
        guild_name = str(enemy_guild.get('name') or '')
        conn.execute('DELETE FROM gvg_current_members')
        for member in members:
            upper = [role_id for _, role_id in sorted(
                member.get('first') or [])]
            lower = [role_id for _, role_id in sorted(
                member.get('second') or [])]
            if (upper and len(upper) != 3) or (lower and len(lower) != 3):
                raise GameRequestError(
                    '{} 的防守阵容不是上下半各三人'.format(member['name']))
            upper = (upper + [None, None, None])[:3]
            lower = (lower + [None, None, None])[:3]
            conn.execute(
                '''
                INSERT INTO gvg_members(
                    cuid, name, avatar_role_id, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(cuid) DO UPDATE SET
                    name=excluded.name,
                    avatar_role_id=excluded.avatar_role_id,
                    updated_at=excluded.updated_at
                ''',
                (member['cuid'], member['name'],
                 member['avatar_role_id'], now_ms()),
            )
            conn.execute(
                '''
                INSERT OR REPLACE INTO gvg_current_members(
                    cuid, snapshot_date, sort_order,
                    upper_1_role_id, upper_2_role_id, upper_3_role_id,
                    lower_1_role_id, lower_2_role_id, lower_3_role_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (member['cuid'], snapshot_date, member['order'],
                 upper[0], upper[1], upper[2],
                 lower[0], lower[1], lower[2]),
            )
        meta_set(conn, 'current_enemy_guild_id', guild_id)
        meta_set(conn, 'current_enemy_guild_name', guild_name)
        meta_set(conn, 'current_match_date', snapshot_date)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def save_battle_rows(rows, db_path=DATA_DB_PATH, conn=None):
    if not rows:
        return False
    owns_connection = conn is None
    if owns_connection:
        init_database(db_path)
        conn = connect_data(db_path)
    battle_id = rows[0]['battle_id']
    try:
        conn.execute('BEGIN IMMEDIATE')
        exists = conn.execute(
            'SELECT 1 FROM gvg_rounds WHERE battle_id = ? LIMIT 1',
            (battle_id,),
        ).fetchone()
        if exists:
            conn.rollback()
            return False
        for row in rows:
            conn.execute(
                '''
                INSERT INTO gvg_rounds(
                    battle_id, round_idx, start_ts,
                    atk_cuid, atk_name, atk_guild,
                    def_cuid, def_name, def_guild, win
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''',
                (row['battle_id'], row['round_idx'], row['start_ts'],
                 row['atk_cuid'], row['atk_name'], row['atk_guild'],
                 row['def_cuid'], row['def_name'], row['def_guild'],
                 int(row['win'])),
            )
            for side, team in (('atk', row['atk_team']),
                               ('def', row['def_team'])):
                conn.executemany(
                    '''
                    INSERT INTO gvg_units(
                        battle_id, round_idx, side, pos, role_id,
                        star, awaken, imprint, dead
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''',
                    [(row['battle_id'], row['round_idx'], side, unit['pos'],
                      unit['role_id'], unit['star'], unit['awaken'],
                      unit['imprint'], int(unit['dead'])) for unit in team],
                )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_connection:
            conn.close()


def existing_battle_ids(battle_ids, db_path=DATA_DB_PATH, conn=None):
    battle_ids = list(battle_ids)
    if not battle_ids:
        return set()
    owns_connection = conn is None
    if owns_connection:
        init_database(db_path)
        conn = connect_data(db_path)
    try:
        existing = set()
        batch_size = 900
        for start in range(0, len(battle_ids), batch_size):
            batch = battle_ids[start:start + batch_size]
            placeholders = ','.join('?' for _ in batch)
            existing.update(row['battle_id'] for row in conn.execute(
                'SELECT DISTINCT battle_id FROM gvg_rounds '
                'WHERE battle_id IN ({})'.format(placeholders),
                batch,
            ))
        return existing
    finally:
        if owns_connection:
            conn.close()


def save_snapshot(cuid, kind, payload, db_path=DATA_DB_PATH):
    conn = connect_data(db_path)
    try:
        with conn:
            conn.execute('INSERT OR REPLACE INTO gvg_snapshots VALUES (?, ?, ?, ?)',
                         (int(cuid), kind, today(), json.dumps(payload, ensure_ascii=False)))
    finally:
        conn.close()
