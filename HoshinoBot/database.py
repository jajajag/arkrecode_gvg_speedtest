import sqlite3
import shutil
import time
from datetime import datetime
from pathlib import Path

from .api import BASE_DIR, GameRequestError, INFO_IMAGE_LOCK, oid


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
            CREATE TABLE IF NOT EXISTS gvg_defences (
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
            CREATE TABLE IF NOT EXISTS pvp_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pvp_equips (
                equip_id TEXT PRIMARY KEY,
                cuid INTEGER,
                player_name TEXT,
                equip_type TEXT,
                static_id TEXT,
                set_name TEXT,
                class_lv INTEGER,
                lv INTEGER,
                main_prop TEXT,
                main_value REAL,
                sub1_prop TEXT,
                sub1_value REAL,
                sub2_prop TEXT,
                sub2_value REAL,
                sub3_prop TEXT,
                sub3_value REAL,
                sub4_prop TEXT,
                sub4_value REAL
            );
            CREATE INDEX IF NOT EXISTS idx_gvg_rounds_recent
                ON gvg_rounds(start_ts);
            CREATE INDEX IF NOT EXISTS idx_gvg_rounds_defender
                ON gvg_rounds(def_cuid, atk_guild, start_ts);
            CREATE INDEX IF NOT EXISTS idx_gvg_units_role
                ON gvg_units(side, role_id);
            ''')
        conn.commit()
    finally:
        conn.close()


def meta_get(conn, key):
    row = conn.execute(
        'SELECT value FROM pvp_meta WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else None


def meta_set(conn, key, value):
    conn.execute(
        'INSERT INTO pvp_meta(key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(value)),
    )


def update_our_guild_meta(guild_info, db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        meta_set(conn, 'our_guild_name', guild_info.get('name') or '')
        conn.commit()
    finally:
        conn.close()


def save_pvp_equips(data, db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    saved = 0
    try:
        conn.execute('BEGIN IMMEDIATE')
        for item in data.get('PVPRankInfoList') or []:
            player = item.get('PlayerInfo') or {}
            pvp_info = item.get('PVPInfo') or {}
            defense = pvp_info.get('DefenceTeam') or {}
            role_map = defense.get('PositionRoleMap') or {}
            for role in role_map.values():
                for equip_type, equip in (role.get('EquipmentMap') or {}).items():
                    equip_id = oid(equip.get('_id'))
                    if not equip_id:
                        continue
                    new_lv = int(equip.get('LV') or 0)
                    old = conn.execute(
                        'SELECT lv FROM pvp_equips WHERE equip_id=?',
                        (equip_id,),
                    ).fetchone()
                    if old and new_lv <= int(old['lv'] or 0):
                        continue
                    main_prop = equip.get('MainProp') or {}
                    source_values = ((equip.get('SubProps') or {}).get(
                        'SourceValues') or [])
                    subprops = (list(source_values) + [{}] * 4)[:4]
                    conn.execute(
                        '''
                        INSERT OR REPLACE INTO pvp_equips(
                            equip_id, cuid, player_name, equip_type,
                            static_id, set_name, class_lv, lv,
                            main_prop, main_value,
                            sub1_prop, sub1_value, sub2_prop, sub2_value,
                            sub3_prop, sub3_value, sub4_prop, sub4_value
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                  ?, ?, ?, ?)
                        ''',
                        (equip_id, player.get('CUID'),
                         str(player.get('Name') or ''), str(equip_type),
                         str(equip.get('StaticID') or ''),
                         str(equip.get('Set') or ''),
                         int(equip.get('ClassLV') or 0), new_lv,
                         str(main_prop.get('PropertyType') or ''),
                         main_prop.get('Value') or 0,
                         str(subprops[0].get('PropertyType') or ''),
                         subprops[0].get('Value') or 0,
                         str(subprops[1].get('PropertyType') or ''),
                         subprops[1].get('Value') or 0,
                         str(subprops[2].get('PropertyType') or ''),
                         subprops[2].get('Value') or 0,
                         str(subprops[3].get('PropertyType') or ''),
                         subprops[3].get('Value') or 0),
                    )
                    saved += 1
        conn.commit()
        return saved
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def replace_defenses(members, enemy_guild, db_path=DATA_DB_PATH,
                     snapshot_date=None, images_dir=IMAGES_DIR):
    with INFO_IMAGE_LOCK:
        return _replace_defenses_locked(
            members,
            enemy_guild,
            db_path=db_path,
            snapshot_date=snapshot_date,
            images_dir=images_dir,
        )


def _replace_defenses_locked(members, enemy_guild, db_path=DATA_DB_PATH,
                             snapshot_date=None, images_dir=IMAGES_DIR):
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
        conn.execute('DELETE FROM gvg_defences')
        if is_new_match:
            conn.execute(
                'UPDATE gvg_members SET info=NULL, info_date=NULL '
                'WHERE info IS NOT NULL OR info_date IS NOT NULL')
        for member in members:
            upper = [role_id for _, role_id in sorted(member['first'])]
            lower = [role_id for _, role_id in sorted(member['second'])]
            if len(upper) != 3 or len(lower) != 3:
                raise GameRequestError(
                    '{} 的防守阵容不是上下半各三人'.format(member['name']))
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
                INSERT OR REPLACE INTO gvg_defences(
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
