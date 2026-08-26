import json
import sqlite3
import shutil
import time
from datetime import datetime
from pathlib import Path

from .api import BASE_DIR, GameRequestError, INFO_IMAGE_LOCK


DATA_DIR = BASE_DIR / 'data'
DATA_DB_PATH = DATA_DIR / 'data.db'
MASTER_DB_PATH = DATA_DIR / 'master.db'
ALIAS_PATH = DATA_DIR / 'character_dic.json'
IMAGES_DIR = BASE_DIR / 'images'


def today():
    return datetime.now().strftime('%Y-%m-%d')


def now_ms():
    return int(time.time() * 1000)


def connect_data(path=DATA_DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 30000')
    return conn


def clear_info_images(path=IMAGES_DIR):
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError('拒绝清理符号链接图片目录：{}'.format(path))
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def init_database(path=DATA_DB_PATH):
    conn = connect_data(path)
    try:
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS gvg_members (
                cuid INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_role_id TEXT,
                max_speed TEXT,
                info TEXT,
                info_date TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gvg_current_members (
                cuid INTEGER PRIMARY KEY,
                snapshot_date TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                upper_1_role_id TEXT,
                upper_2_role_id TEXT,
                upper_3_role_id TEXT,
                lower_1_role_id TEXT,
                lower_2_role_id TEXT,
                lower_3_role_id TEXT,
                FOREIGN KEY (cuid) REFERENCES gvg_members(cuid)
            );
            CREATE TABLE IF NOT EXISTS gvg_member_info_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cuid INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                match_date TEXT,
                enemy_guild_id TEXT,
                enemy_guild_name TEXT,
                info TEXT NOT NULL,
                info_date TEXT,
                archived_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gvg_rounds (
                battle_id TEXT NOT NULL,
                round_idx INTEGER NOT NULL,
                start_ts INTEGER NOT NULL,
                atk_cuid INTEGER,
                atk_name TEXT,
                atk_guild TEXT,
                def_cuid INTEGER,
                def_name TEXT,
                def_guild TEXT,
                win INTEGER NOT NULL,
                PRIMARY KEY (battle_id, round_idx)
            );
            CREATE TABLE IF NOT EXISTS gvg_units (
                battle_id TEXT NOT NULL,
                round_idx INTEGER NOT NULL,
                side TEXT NOT NULL,
                pos INTEGER NOT NULL,
                role_id TEXT NOT NULL,
                star INTEGER,
                awaken INTEGER,
                imprint INTEGER,
                dead INTEGER NOT NULL,
                PRIMARY KEY (battle_id, round_idx, side, pos)
            );
            CREATE TABLE IF NOT EXISTS plugin_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_gvg_rounds_recent
                ON gvg_rounds(start_ts);
            CREATE INDEX IF NOT EXISTS idx_gvg_rounds_defender
                ON gvg_rounds(def_cuid, atk_guild, start_ts);
            CREATE INDEX IF NOT EXISTS idx_gvg_units_role
                ON gvg_units(side, role_id);
            CREATE INDEX IF NOT EXISTS idx_gvg_info_history_player
                ON gvg_member_info_history(cuid, archived_at DESC, id DESC);
            ''')
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


def replace_current_members(members, enemy_guild, db_path=DATA_DB_PATH,
                            snapshot_date=None, images_dir=IMAGES_DIR):
    with INFO_IMAGE_LOCK:
        return _replace_current_members_locked(
            members,
            enemy_guild,
            db_path=db_path,
            snapshot_date=snapshot_date,
            images_dir=images_dir,
        )


def _text_only_info(raw):
    """Return encoded text segments, dropping images and invalid old data."""
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get('format') != 'gvg_info_v1':
        return None
    source_segments = data.get('segments')
    if not isinstance(source_segments, list):
        return None
    segments = []
    for segment in source_segments:
        if not isinstance(segment, dict) or segment.get('type') != 'text':
            continue
        content = str(segment.get('content') or '')
        if content:
            segments.append({'type': 'text', 'content': content})
    if not segments:
        return None
    return json.dumps(
        {'format': 'gvg_info_v1', 'segments': segments},
        ensure_ascii=False,
        separators=(',', ':'),
    )


def _archive_member_info(conn, match_date, enemy_guild_id,
                         enemy_guild_name):
    archived_at = now_ms()
    for member in conn.execute(
            'SELECT cuid, name, info, info_date FROM gvg_members '
            'WHERE info IS NOT NULL'):
        text_info = _text_only_info(member['info'])
        if text_info is None:
            continue
        conn.execute(
            '''
            INSERT INTO gvg_member_info_history(
                cuid, player_name, match_date,
                enemy_guild_id, enemy_guild_name,
                info, info_date, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (int(member['cuid']), str(member['name']), match_date,
             enemy_guild_id, enemy_guild_name,
             text_info, member['info_date'], archived_at),
        )


def _replace_current_members_locked(members, enemy_guild,
                                    db_path=DATA_DB_PATH,
                                    snapshot_date=None,
                                    images_dir=IMAGES_DIR):
    init_database(db_path)
    snapshot_date = snapshot_date or today()
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        guild_id = str(enemy_guild.get('id') or '')
        guild_name = str(enemy_guild.get('name') or '')
        previous_date = meta_get(conn, 'current_match_date') or ''
        previous_guild_id = meta_get(conn, 'current_enemy_guild_id') or ''
        previous_guild_name = meta_get(
            conn, 'current_enemy_guild_name') or ''
        same_enemy = (
            bool(guild_id and previous_guild_id == guild_id)
            or bool(guild_name and previous_guild_name == guild_name)
        )
        is_new_match = previous_date != snapshot_date or not same_enemy
        conn.execute('DELETE FROM gvg_current_members')
        if is_new_match:
            _archive_member_info(
                conn,
                previous_date,
                previous_guild_id,
                previous_guild_name,
            )
            conn.execute(
                'UPDATE gvg_members SET info=NULL, info_date=NULL '
                'WHERE info IS NOT NULL OR info_date IS NOT NULL')
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
        if is_new_match:
            clear_info_images(images_dir)
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
