import hashlib
import importlib
import json
import sqlite3
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
DATA_DB_PATH = DATA_DIR / 'data.db'
MASTER_DB_PATH = DATA_DIR / 'master.db'
ALIAS_PATH = DATA_DIR / 'character_dic.json'

SCHEMA_SQL = """
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
CREATE TABLE IF NOT EXISTS pvp_equips (
    equip_id TEXT PRIMARY KEY,
    cuid INTEGER, player_name TEXT,
    equip_type TEXT, static_id TEXT, set_name TEXT,
    class_lv INTEGER, lv INTEGER, main_prop TEXT, main_value REAL,
    sub1_prop TEXT, sub1_value REAL, sub2_prop TEXT, sub2_value REAL,
    sub3_prop TEXT, sub3_value REAL, sub4_prop TEXT, sub4_value REAL
);
CREATE TABLE IF NOT EXISTS gvg_defence (
    id INTEGER PRIMARY KEY,
    match_id TEXT NOT NULL,
    match_date TEXT NOT NULL,
    cuid INTEGER NOT NULL,
    name TEXT NOT NULL,
    avatar_role_id TEXT,
    UNIQUE (match_date, match_id, cuid)
);
CREATE TABLE IF NOT EXISTS gvg_defence_units (
    defence_id INTEGER NOT NULL REFERENCES gvg_defence(id) ON DELETE CASCADE,
    team INTEGER NOT NULL CHECK (team IN (1, 2)),
    pos INTEGER NOT NULL,
    role_id TEXT NOT NULL,
    lv INTEGER,
    star INTEGER,
    awaken_lv INTEGER,
    imprint_lv INTEGER,
    is_self_imprint INTEGER,
    artifact_id TEXT,
    artifact_lv INTEGER,
    skill_levels TEXT,
    sets TEXT,
    shoes_prop TEXT, shoes_value REAL,
    ring_prop TEXT, ring_value REAL,
    necklace_prop TEXT, necklace_value REAL,
    hp INTEGER NOT NULL,
    PRIMARY KEY (defence_id, team, pos)
);
"""

def today():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d')


def now_ms():
    return int(time.time() * 1000)


def connect_data(path=DATA_DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_database(path=DATA_DB_PATH):
    conn = connect_data(path)
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables & {'gvg_members', 'gvg_current_members', 'gvg_snapshots', 'gvg_defences', 'pvp_meta'}:
            raise ValueError('检测到旧数据库，请先运行 migrate_gvg_db.py 并替换生成的新库')
        if 'gvg_defence' in tables and 'team_data' in {
                row[1] for row in conn.execute('PRAGMA table_info(gvg_defence)')}:
            raise ValueError('检测到 JSON 防守表，请先运行 migrate_gvg_db.py')
        if 'pvp_equips' in tables and 'observed_at' in {
                row[1] for row in conn.execute('PRAGMA table_info(pvp_equips)')}:
            raise ValueError('检测到旧版装备时间戳，请先运行 migrate_gvg_db.py')
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


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


def save_player_equipment(card, cuid, name, db_path=DATA_DB_PATH):
    conn = connect_data(db_path)
    try:
        with conn:
            return save_equipment_rows(conn, card_equipment(card, cuid, name))
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


EQUIP_COLUMNS = (
    'equip_id', 'cuid', 'player_name', 'equip_type', 'static_id', 'set_name',
    'class_lv', 'lv', 'main_prop', 'main_value',
    'sub1_prop', 'sub1_value', 'sub2_prop', 'sub2_value',
    'sub3_prop', 'sub3_value', 'sub4_prop', 'sub4_value',
)
EQUIP_UPSERT = (
    'INSERT INTO pvp_equips (' + ','.join(EQUIP_COLUMNS) + ') VALUES ('
    + ','.join('?' for _ in EQUIP_COLUMNS) + ') ON CONFLICT(equip_id) DO UPDATE SET '
    + ','.join(f'{key}=excluded.{key}' for key in EQUIP_COLUMNS if key != 'equip_id')
    + ' WHERE COALESCE(excluded.lv,0) > COALESCE(pvp_equips.lv,0)'
)


def card_equipment(card, cuid, name):
    pvp = ((card.get('PVPInfo') or {}).get('DefenceTeam') or {}).get('PositionRoleMap') or {}
    roles = list(pvp.values())
    roles.extend(item.get('Role') or {} for item in
                 (card.get('BattleSupportData') or {}).get('RoleDataList') or [])
    for role in roles:
        for part, equip in (role.get('EquipmentMap') or {}).items():
            identity = equip.get('_id')
            if isinstance(identity, dict):
                identity = identity.get('$oid') or identity.get('$id')
            if not identity:
                # Older snapshots can lack IDs; keep them with stable player-scoped keys.
                digest = hashlib.sha256(json.dumps(equip, sort_keys=True).encode()).hexdigest()
                identity = f'legacy:{cuid}:{part}:{digest}'
            main = equip.get('MainProp') or {}
            props = ((equip.get('SubProps') or {}).get('SourceValues') or [])
            if len(props) > 4:
                raise ValueError('装备副属性超过四条，拒绝截断保存')
            props = props + [{}] * (4 - len(props))
            row = [str(identity), int(cuid), str(name), part,
                   equip.get('StaticID'), equip.get('Set'), equip.get('ClassLV'),
                   int(equip.get('LV') or 0), main.get('PropertyType'),
                   main.get('Value', main.get('SValue', 0))]
            for prop in props:
                row.extend((prop.get('PropertyType', ''), prop.get('Value', prop.get('SValue', 0))))
            yield tuple(row)


def save_equipment_rows(conn, rows):
    before = conn.total_changes
    conn.executemany(EQUIP_UPSERT, rows)
    return conn.total_changes - before


_STATS_LOCK = threading.Lock()
_MASTER_SIGNATURE = None


def _stats_source(master_path):
    global _MASTER_SIGNATURE
    prefix = __package__.rsplit('.', 1)[0] + '.' if '.' in __package__ else ''
    helper = importlib.import_module(prefix + 'Frida.helper')
    path = Path(master_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f'计算防守生命需要 master.db：{path}；迁移时可用 --master-db 指定')
    stat = path.stat()
    signature = (str(path), stat.st_mtime_ns, stat.st_size)
    if _MASTER_SIGNATURE != signature or helper.MASTER is None or helper.MASTER.path.resolve() != path:
        helper.MASTER = None
        _MASTER_SIGNATURE = None
        helper.load_master_data(path)
        _MASTER_SIGNATURE = signature
    return helper


def defence_unit_rows(teams, master_path=MASTER_DB_PATH):
    """Calculate HP before discarding raw data; preserve response skill order."""
    rows = []
    with _STATS_LOCK:
        helper = _stats_source(master_path)
        master = helper.MASTER
        for team, key in ((1, 'FirstTeam'), (2, 'SecondTeam')):
            entries = sorted(((teams.get(key) or {}).get('PositionRoleMap') or {}).items(),
                             key=lambda item: int(item[0]))
            if len(entries) > 3:
                raise ValueError('每半场防守角色不能超过三人')
            stats = helper.calculate_team_stats([role for _, role in entries])
            for (pos, role), panel in zip(entries, stats):
                equipment = role.get('EquipmentMap') or {}
                counts = Counter((equipment.get(part) or {}).get('Set') for part in
                                 ('Weapon', 'Head', 'Body', 'Shoes', 'Ring', 'Necklace'))
                sets = []
                for set_id, count in counts.most_common():
                    if not set_id:
                        continue
                    need = max(int((master.equipment_sets.get(set_id) or {}).get('Count') or 99), 1)
                    active = count // need
                    if active:
                        sets.append(f'{active if active > 1 else ""}{master.equipment_set_name(set_id)}')
                bond = role.get('ArtifactData') or {}
                skills = (role.get('Skills') or {}).get('Skills') or []
                levels = ','.join(str(int(skill['Level']) - 1) if skill.get('Level') is not None else '?'
                                  for skill in skills)
                row = [team, int(pos), role['StaticID'], role.get('LV', role.get('Level')),
                       role.get('Star'), role.get('AwakenLV'), role.get('ImprintLV'),
                       int(role['IsSelfImprint']) if 'IsSelfImprint' in role else None,
                       bond.get('StaticID'), bond.get('LV'), levels, ''.join(sets)]
                for part in ('Shoes', 'Ring', 'Necklace'):
                    prop = (equipment.get(part) or {}).get('MainProp') or {}
                    row.extend((prop.get('PropertyType'), prop.get('Value', prop.get('SValue'))))
                row.append(round(panel['HP']))
                rows.append(tuple(row))
    return rows


def insert_defence(conn, match_id, match_date, cuid, name, avatar, teams, master_path=MASTER_DB_PATH):
    units = defence_unit_rows(teams, master_path)
    cursor = conn.execute('INSERT INTO gvg_defence(match_id,match_date,cuid,name,avatar_role_id) '
                          'VALUES (?,?,?,?,?)', (match_id, match_date, int(cuid), name, avatar))
    defence_id = cursor.lastrowid
    conn.executemany('INSERT INTO gvg_defence_units VALUES (' + ','.join('?' for _ in range(20)) + ')',
                     ((defence_id, *row) for row in units))
    return defence_id


def save_defence_match(players, enemy, db_path=DATA_DB_PATH, match_date=None):
    if not players:
        return
    match_date = match_date or today()
    match_id = match_date + ':' + str(enemy.get('id') or enemy.get('name') or 'unknown')
    conn = connect_data(db_path)
    try:
        with conn:
            conn.execute('DELETE FROM gvg_defence WHERE match_date=? AND match_id=?', (match_date, match_id))
            for player in players:
                info = player['PlayerInfo']
                insert_defence(conn, match_id, match_date, info['CUID'], str(info.get('Name') or info['CUID']),
                               str(info.get('LeaderSID') or ''), player['DefenceTeamData'])
            meta_set(conn, 'match:' + match_id, json.dumps({
                'date': match_date, 'enemy_guild_id': str(enemy.get('id') or ''),
                'enemy_guild_name': str(enemy.get('name') or ''),
            }, ensure_ascii=False, separators=(',', ':')))
    finally:
        conn.close()
