import asyncio
import base64
import json
import random
import re
import sqlite3
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import requests

from .gvg_master import update_master_db


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
ACCOUNT_PATH = DATA_DIR / 'account.json'
DATA_DB_PATH = DATA_DIR / 'data.db'
MASTER_DB_PATH = DATA_DIR / 'master.db'
ALIAS_PATH = DATA_DIR / 'character_dic.json'
ALIAS_URL = (
    'https://github.com/StardustChocolate/openrubi/raw/refs/heads/main/'
    'arkrecode/members/character_dic.json'
)

GAME_URL = 'https://game-arkre-labs.ecchi.xxx/Router/RouterHandler.ashx'
TOKEN_URL = 'https://sadpki-portal-v2.ebuajk.com/api/v2/token/access'
GAME_HEADERS = {
    'Content-Type': 'application/octet-stream',
    'User-Agent': (
        'UnityPlayer/2022.3.62f2 '
        '(UnityWebRequest/1.0, libcurl/8.10.1-DEV)'
    ),
}
RECENT_DAYS = 30
MILLIS_PER_DAY = 24 * 60 * 60 * 1000

requests.packages.urllib3.disable_warnings()


class ConfigError(RuntimeError):
    pass


class GameRequestError(RuntimeError):
    pass


def _today():
    return datetime.now().strftime('%Y-%m-%d')


def _now_ms():
    return int(time.time() * 1000)


def _oid(value):
    if isinstance(value, dict):
        return str(value.get('$oid') or value.get('$id') or '')
    return str(value or '')


def load_config(path=ACCOUNT_PATH):
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            '找不到 {}，请复制 account_example.json 后填写。'.format(path.name))
    try:
        with path.open('r', encoding='utf-8') as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError('account.json 读取失败：{}'.format(exc)) from exc
    required = (
        'Name', 'Token', 'our_guild_id', 'our_guild_name',
        'target_guild_id', 'target_guild_name',
    )
    missing = [key for key in required if not str(config.get(key, '')).strip()]
    if missing:
        raise ConfigError('account.json 缺少：{}'.format('、'.join(missing)))
    if config['our_guild_name'] == config['target_guild_name']:
        raise ConfigError('我方团名和目标团名不能相同')
    if config['our_guild_id'] == config['target_guild_id']:
        raise ConfigError('我方团 GID 和目标团 GID 不能相同')
    return config


def save_config(config, path=ACCOUNT_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + '.tmp')
    with temp_path.open('w', encoding='utf-8') as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
    temp_path.replace(path)


def connect_data(path=DATA_DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_legacy_tables(conn):
    legacy_tables = (
        ('xiyan_members', 'gvg_members', (
            'cuid', 'name', 'avatar_role_id', 'first_speed',
            'info', 'info_date', 'updated_at',
        )),
        ('xiyan_defenses', 'gvg_defenses', (
            'cuid', 'snapshot_date', 'sort_order',
        )),
        ('xiyan_defense_units', 'gvg_defense_units', (
            'cuid', 'half', 'pos', 'role_id',
        )),
        ('xiyan_meta', 'gvg_meta', ('key', 'value')),
    )
    existing = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    for legacy, current, columns in legacy_tables:
        if legacy not in existing:
            continue
        if current not in existing:
            conn.execute('ALTER TABLE "{}" RENAME TO "{}"'.format(
                legacy, current))
            existing.remove(legacy)
            existing.add(current)
            continue
        column_sql = ', '.join(columns)
        conn.execute(
            'INSERT OR IGNORE INTO "{}" ({}) '
            'SELECT {} FROM "{}"'.format(
                current, column_sql, column_sql, legacy))


def init_database(path=DATA_DB_PATH):
    conn = connect_data(path)
    try:
        _migrate_legacy_tables(conn)
        conn.executescript(
            '''
            CREATE TABLE IF NOT EXISTS gvg_members (
                cuid INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                avatar_role_id TEXT,
                first_speed TEXT,
                info TEXT,
                info_date TEXT,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gvg_defenses (
                cuid INTEGER PRIMARY KEY,
                snapshot_date TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                FOREIGN KEY (cuid) REFERENCES gvg_members(cuid)
            );
            CREATE TABLE IF NOT EXISTS gvg_defense_units (
                cuid INTEGER NOT NULL,
                half INTEGER NOT NULL,
                pos INTEGER NOT NULL,
                role_id TEXT NOT NULL,
                PRIMARY KEY (cuid, half, pos)
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
            CREATE TABLE IF NOT EXISTS gvg_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_gvg_rounds_recent
                ON gvg_rounds(start_ts);
            CREATE INDEX IF NOT EXISTS idx_gvg_rounds_defender
                ON gvg_rounds(def_cuid, atk_guild, start_ts);
            CREATE INDEX IF NOT EXISTS idx_gvg_units_role
                ON gvg_units(side, role_id);
            '''
        )
        conn.commit()
    finally:
        conn.close()


def _meta_get(conn, key):
    row = conn.execute(
        'SELECT value FROM gvg_meta WHERE key = ?', (key,)).fetchone()
    return row['value'] if row else None


def _meta_set(conn, key, value):
    conn.execute(
        'INSERT INTO gvg_meta(key, value) VALUES (?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
        (key, str(value)),
    )


class GameClient:
    def __init__(self, config, account_path=ACCOUNT_PATH, session=None):
        self.config = config
        self.account_path = Path(account_path)
        self.http = session or requests.Session()
        self.aid = None
        self.session_id = None
        self.cuid = None
        self.bulletin = None

    def _post(self, url, payload=None, headers=None, timeout=60):
        response = self.http.post(
            url,
            json=payload,
            headers=headers or GAME_HEADERS,
            verify=False,
            timeout=timeout,
        )
        response.raise_for_status()
        response.encoding = 'utf-8'
        data = response.json()
        self._check_response(data)
        return data

    @staticmethod
    def _check_response(data):
        if not isinstance(data, dict):
            raise GameRequestError('服务器返回的不是 JSON 对象')
        error = data.get('Error') or data.get('error')
        error_code = data.get('ErrorCode')
        code = data.get('Code', data.get('code'))
        bad_code = code not in (None, 0, '0', 200, '200', 'Success', 'success')
        if data.get('Success') is False or error \
                or error_code not in (None, 0, '0', '') or bad_code:
            message = data.get('Message') or data.get('message')
            raise GameRequestError(str(message or error or error_code or code))

    def _send_route(self, route, data, delay=None):
        if delay:
            time.sleep(random.uniform(*delay))
        return self._post(GAME_URL, {'route': route, 'data': data})

    def _login_once(self):
        bulletin = self._send_route(
            'GameServerDBSettingHandler.QueryBulletinInfoResult', {})
        info = bulletin.get('Info') or {}
        versions = info.get('AvailableVersions') or []
        if not versions:
            raise GameRequestError('公告响应缺少 AvailableVersions')

        token = self.config['Token']
        parts = token.split('.')
        if len(parts) < 2:
            raise ConfigError('Token 不是有效的 JWT')
        encoded = parts[1] + '=' * (-len(parts[1]) % 4)
        try:
            token_data = json.loads(base64.urlsafe_b64decode(encoded))
            login_id = token_data['user_id']
        except Exception as exc:
            raise ConfigError('Token 无法解析 user_id') from exc

        is_new_sdk = 'exp' in token_data
        login_token = token
        if is_new_sdk:
            headers = dict(GAME_HEADERS)
            headers['Authorization'] = 'Bearer {}'.format(token)
            token_result = self._post(
                TOKEN_URL, headers=headers, timeout=60)
            token_payload = token_result.get('data') or {}
            login_id = token_payload.get('userId')
            login_token = token_payload.get('accessToken')
            refresh_token = token_payload.get('refreshToken')
            if not login_id or not login_token or not refresh_token:
                raise GameRequestError('刷新 Token 的响应字段不完整')
            self.config['Token'] = refresh_token
            save_config(self.config, self.account_path)

        result = self._send_route('AccountHandler.Login', {
            'LoginID': login_id,
            'Token': login_token,
            'Version': versions[-1],
            'LoginType': 'Erolabs',
            'IsNewSDK': is_new_sdk,
        })
        account_info = result.get('Info') or {}
        self.aid = _oid(account_info.get('_id'))
        self.session_id = result.get('SessionID')
        self.cuid = account_info.get('CUID')
        if not self.aid or not self.session_id or self.cuid is None:
            raise GameRequestError('登录响应缺少 AID、SessionID 或 CUID')
        self.bulletin = bulletin
        return result

    def login(self, attempts=3):
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                return self._login_once()
            except Exception as exc:
                last_error = exc
                self.aid = self.session_id = self.cuid = None
                if attempt < attempts:
                    time.sleep(attempt)
        raise GameRequestError('连续登录 {} 次失败：{}'.format(
            attempts, last_error)) from last_error

    def call(self, route, data=None, attempts=3, delay=None,
             required_key=None):
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                if not self.session_id:
                    self.login(attempts=3)
                payload = dict(data or {})
                payload.update({'AID': self.aid, 'SessionID': self.session_id})
                result = self._send_route(route, payload, delay=delay)
                if required_key and required_key not in result:
                    raise GameRequestError(
                        '{} 响应缺少 {}'.format(route, required_key))
                return result
            except Exception as exc:
                last_error = exc
                self.aid = self.session_id = self.cuid = None
                if attempt < attempts:
                    self.login(attempts=3)
        raise GameRequestError(
            '{} 连续请求 {} 次失败：{}'.format(route, attempts, last_error)
        ) from last_error

    def call_once(self, route, data=None, delay=None, required_key=None):
        """Send one request without clearing or refreshing the session."""
        if not self.session_id:
            raise GameRequestError('当前没有有效登录会话')
        payload = dict(data or {})
        payload.update({'AID': self.aid, 'SessionID': self.session_id})
        result = self._send_route(route, payload, delay=delay)
        if required_key and required_key not in result:
            raise GameRequestError(
                '{} 响应缺少 {}'.format(route, required_key))
        return result


def query_guild(client, guild_id):
    return client.call(
        'GuildHandler.QueryPartialGuildDataForGuildWar',
        {'GuildID': guild_id},
        delay=(0.8, 1.2),
        required_key='GuildData',
    )


def guild_members(guild_data):
    guild = guild_data.get('GuildData') or {}
    members = guild.get('MemberList')
    if not isinstance(members, list):
        raise GameRequestError('公会响应缺少 GuildData.MemberList')
    return members


def validate_guild_name(guild_data, expected_name):
    actual_name = ((guild_data.get('GuildData') or {}).get('Info') or {}).get(
        'Name')
    if actual_name and actual_name != expected_name:
        raise ConfigError(
            'GID 对应团名为“{}”，与配置的“{}”不一致'.format(
                actual_name, expected_name))


def replace_defenses(members, db_path=DATA_DB_PATH, snapshot_date=None):
    init_database(db_path)
    snapshot_date = snapshot_date or _today()
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE gvg_members SET info=NULL, info_date=NULL')
        conn.execute('DELETE FROM gvg_defense_units')
        conn.execute('DELETE FROM gvg_defenses')
        for member in members:
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
                 member['avatar_role_id'], _now_ms()),
            )
            conn.execute(
                'INSERT INTO gvg_defenses(cuid, snapshot_date, sort_order) '
                'VALUES (?, ?, ?)',
                (member['cuid'], snapshot_date, member['order']),
            )
            for half, key in ((1, 'first'), (2, 'second')):
                conn.executemany(
                    'INSERT INTO gvg_defense_units('
                    'cuid, half, pos, role_id) VALUES (?, ?, ?, ?)',
                    [(member['cuid'], half, pos, role_id)
                     for pos, role_id in member[key]],
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_member_info(db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        conn.execute('UPDATE gvg_members SET info=NULL, info_date=NULL')
        conn.commit()
    finally:
        conn.close()


def query_member_logs(client, cuid):
    return client.call(
        'GuildWarHandler.QueryGuildWarBattleLogListByAccount',
        {'TargetCUID': int(cuid)},
        delay=(0.08, 0.12),
        required_key='SubLogs',
    )


def query_battle_detail(client, battle_id):
    return client.call_once(
        'GuildWarHandler.QueryGuildWarBattleLogByID',
        {'TargetCUID': int(client.cuid), 'TargetID': battle_id},
        delay=(0.8, 1.2),
        required_key='Logs',
    )


def collect_mutual_battle_refs(client, guild_data_list, guild_names):
    guild_names = set(guild_names)
    refs = {}
    for guild_data in guild_data_list:
        for member in guild_members(guild_data):
            player = member.get('PlayerInfo') or {}
            cuid = player.get('CUID')
            if cuid is None:
                continue
            logs = query_member_logs(client, cuid)
            for item in logs.get('SubLogs') or []:
                attacker = item.get('AttackerPlayerInfo') or {}
                defender = item.get('DefenderPlayerInfo') or {}
                attacker_guild = (attacker.get('GuildSubInfo') or {}).get(
                    'Name', '')
                defender_guild = (defender.get('GuildSubInfo') or {}).get(
                    'Name', '')
                if int(attacker.get('CUID') or -1) != int(cuid):
                    continue
                if {attacker_guild, defender_guild} != guild_names:
                    continue
                battle_id = _oid(item.get('_id'))
                if battle_id:
                    refs[battle_id] = item
    return refs


def _parsed_unit(pos, role, dead_ids):
    object_id = _oid((role or {}).get('_id'))
    return {
        'pos': int(pos),
        'role_id': str((role or {}).get('StaticID') or ''),
        'star': int((role or {}).get('Star') or 0),
        'awaken': int((role or {}).get('AwakenLV') or 0),
        'imprint': int((role or {}).get('ImprintLV') or 0),
        'dead': object_id in dead_ids,
    }


def parse_battle_detail(data):
    logs = data.get('Logs') or []
    if not logs:
        raise GameRequestError('战斗详情缺少 Logs')
    log = logs[0]
    battle_id = _oid(log.get('_id'))
    start_ts = int(((log.get('StartTime') or {}).get('$date')) or 0)
    attacker = log.get('AttackerPlayerInfo') or {}
    defender = log.get('DefenderPlayerInfo') or {}
    atk_guild = (attacker.get('GuildSubInfo') or {}).get('Name', '')
    def_guild = (defender.get('GuildSubInfo') or {}).get('Name', '')
    rows = []
    for round_idx, item in enumerate(log.get('EndDatas') or [], 1):
        battle_info = item.get('StartBattleInfo') or {}
        camp1 = ((battle_info.get('CampData1') or {}).get(
            'PositionRoleMap') or {})
        camp2 = ((battle_info.get('CampData2') or {}).get(
            'PositionRoleMap') or {})
        dead_ids = {_oid(value) for value in item.get('Camp1DeadList') or []}
        dead_ids.update(_oid(value) for value in item.get('Camp2DeadList') or [])
        atk_team = sorted(
            (_parsed_unit(pos, role, dead_ids) for pos, role in camp1.items()),
            key=lambda unit: unit['pos'],
        )
        def_team = sorted(
            (_parsed_unit(pos, role, dead_ids) for pos, role in camp2.items()),
            key=lambda unit: unit['pos'],
        )
        if len(atk_team) != 3 or len(def_team) != 3:
            continue
        rows.append({
            'battle_id': battle_id,
            'round_idx': round_idx,
            'start_ts': start_ts,
            'atk_cuid': int(attacker.get('CUID') or 0),
            'atk_name': str(attacker.get('Name') or ''),
            'atk_guild': str(atk_guild),
            'def_cuid': int(defender.get('CUID') or 0),
            'def_name': str(defender.get('Name') or ''),
            'def_guild': str(def_guild),
            'win': item.get('Result') == 'Win',
            'atk_team': atk_team,
            'def_team': def_team,
        })
    if not battle_id or not rows:
        raise GameRequestError('战斗详情没有可入库的回合')
    return rows


def _defense_from_rows(rows, cuid):
    rounds = sorted(
        (row for row in rows if int(row['def_cuid']) == int(cuid)),
        key=lambda row: int(row['round_idx']),
    )
    if len(rounds) < 2:
        return None
    teams = []
    for row in rounds[:2]:
        team = sorted(
            ((int(unit['pos']), str(unit['role_id']))
             for unit in row['def_team'] if unit.get('role_id')),
            key=lambda unit: unit[0],
        )
        if len(team) != 3:
            return None
        teams.append(team)
    return {'first': teams[0], 'second': teams[1]}


def _defense_log_candidates(logs, cuid, guild_name):
    candidates = {}
    for item in logs.get('SubLogs') or []:
        defender = item.get('DefenderPlayerInfo') or {}
        defender_guild = (defender.get('GuildSubInfo') or {}).get('Name', '')
        if int(defender.get('CUID') or -1) != int(cuid):
            continue
        if defender_guild != guild_name:
            continue
        battle_id = _oid(item.get('_id'))
        if not battle_id:
            continue
        start_ts = int(((item.get('StartTime') or {}).get('$date')) or 0)
        previous = candidates.get(battle_id)
        if previous is None or start_ts > previous[0]:
            candidates[battle_id] = (start_ts, battle_id, item)
    return sorted(
        candidates.values(), key=lambda candidate: candidate[0], reverse=True)


def collect_defense_members(client, guild_data, guild_name):
    """Fetch a fresh defense snapshot from every member's live SubLogs."""
    roster = []
    for order, member in enumerate(guild_members(guild_data), 1):
        player = member.get('PlayerInfo') or {}
        cuid = player.get('CUID')
        if cuid is None:
            continue
        logs = query_member_logs(client, cuid)
        candidates = _defense_log_candidates(logs, cuid, guild_name)
        roster.append((order, player, candidates))

    result = []
    consecutive_failures = 0
    for order, player, candidates in roster:
        cuid = int(player['CUID'])
        selected = None
        selected_log = None
        for _, battle_id, sublog in candidates:
            try:
                detail = query_battle_detail(client, battle_id)
            except Exception:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    client.login(attempts=3)
                    consecutive_failures = 0
                continue
            consecutive_failures = 0
            try:
                rows = parse_battle_detail(detail)
            except Exception:
                continue
            selected = _defense_from_rows(rows, cuid)
            if selected:
                selected_log = sublog
                break

        if not selected:
            continue
        defender = (selected_log or {}).get('DefenderPlayerInfo') or {}
        result.append({
            'cuid': cuid,
            'name': str(player.get('Name') or defender.get('Name') or cuid),
            'avatar_role_id': str(
                player.get('LeaderSID') or defender.get('LeaderSID') or ''),
            'order': order,
            'first': selected['first'],
            'second': selected['second'],
        })
    return result


def save_battle_rows(rows, db_path=DATA_DB_PATH):
    if not rows:
        return False
    init_database(db_path)
    battle_id = rows[0]['battle_id']
    conn = connect_data(db_path)
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
        conn.close()


def existing_battle_ids(battle_ids, db_path=DATA_DB_PATH):
    init_database(db_path)
    battle_ids = list(battle_ids)
    if not battle_ids:
        return set()
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
        conn.close()


def update_mutual_battles(client, guild_data_list, config,
                          db_path=DATA_DB_PATH):
    guild_names = (config['our_guild_name'], config['target_guild_name'])
    refs = collect_mutual_battle_refs(client, guild_data_list, guild_names)
    known_ids = existing_battle_ids(refs, db_path)
    saved = 0
    consecutive_failures = 0
    for battle_id in sorted(set(refs) - known_ids):
        try:
            detail = query_battle_detail(client, battle_id)
        except Exception:
            consecutive_failures += 1
            if consecutive_failures >= 3:
                client.login(attempts=3)
                consecutive_failures = 0
            continue
        consecutive_failures = 0
        try:
            rows = parse_battle_detail(detail)
        except Exception:
            continue
        if {rows[0]['atk_guild'], rows[0]['def_guild']} != set(guild_names):
            continue
        if save_battle_rows(rows, db_path):
            saved += 1
    return saved


def update_aliases(path=ALIAS_PATH, session=None):
    http = session or requests.Session()
    response = http.get(ALIAS_URL, timeout=60)
    response.raise_for_status()
    aliases = response.json()
    if not isinstance(aliases, dict):
        raise RuntimeError('角色别名表不是 JSON 对象')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + '.tmp')
    with temp_path.open('w', encoding='utf-8') as file:
        json.dump(aliases, file, ensure_ascii=False, indent=2)
    temp_path.replace(path)
    return len(aliases)


def _update_master(bulletin, http):
    conn = connect_data()
    try:
        current_catalog = _meta_get(conn, 'master_catalog')
    finally:
        conn.close()
    catalog, changed = update_master_db(
        bulletin,
        MASTER_DB_PATH,
        current_catalog=current_catalog,
        session=http,
    )
    conn = connect_data()
    try:
        _meta_set(conn, 'master_catalog', catalog)
        conn.commit()
    finally:
        conn.close()
    return catalog, changed


def update_all_sync(mode=None):
    init_database()
    config = load_config()
    client = GameClient(config)
    client.login(attempts=3)
    warnings = []
    master_changed = False
    alias_count = 0
    try:
        _, master_changed = _update_master(client.bulletin, client.http)
    except Exception as exc:
        warnings.append('master.db 更新失败：{}'.format(exc))
    try:
        alias_count = update_aliases(session=client.http)
    except Exception as exc:
        warnings.append('角色别名表下载失败：{}'.format(exc))

    weekday = datetime.now().isoweekday()
    if mode is None:
        mode = 'defense' if weekday in (1, 3, 5) else \
            'battles' if weekday in (2, 4, 6) else 'both'
    if mode not in ('defense', 'battles', 'both'):
        raise ValueError('未知更新模式：{}'.format(mode))

    result = {
        'mode': mode,
        'defenses': None,
        'battles': None,
        'aliases': alias_count,
        'master_changed': master_changed,
        'warnings': warnings,
    }
    guild_data = {}
    if mode in ('defense', 'both'):
        target = query_guild(client, config['target_guild_id'])
        validate_guild_name(target, config['target_guild_name'])
        guild_data['target'] = target
        members = collect_defense_members(
            client, target, config['target_guild_name'])
        if not members:
            raise GameRequestError(
                '目标团成员的 SubLogs 中没有解析到可用的两队防守阵容')
        replace_defenses(members)
        result['defenses'] = len(members)

    if mode in ('battles', 'both'):
        clear_member_info()
        if 'target' not in guild_data:
            guild_data['target'] = query_guild(
                client, config['target_guild_id'])
            validate_guild_name(
                guild_data['target'], config['target_guild_name'])
        guild_data['our'] = query_guild(client, config['our_guild_id'])
        validate_guild_name(guild_data['our'], config['our_guild_name'])
        result['battles'] = update_mutual_battles(
            client, [guild_data['target'], guild_data['our']], config)
    return result


def load_aliases(path=ALIAS_PATH):
    try:
        with Path(path).open('r', encoding='utf-8') as file:
            data = json.load(file)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def load_roles(path=MASTER_DB_PATH):
    if not Path(path).is_file():
        raise RuntimeError('找不到 master.db，请先发送“团战 更新数据”。')
    conn = sqlite3.connect(str(path))
    try:
        rows = conn.execute(
            '''
            SELECT r.ID, COALESCE(c.Value, r.ID)
            FROM Role AS r
            LEFT JOIN CHS AS c ON c.Key = r.NAME
            WHERE r.ID LIKE 'H%'
            '''
        ).fetchall()
    finally:
        conn.close()
    return {str(role_id): str(name) for role_id, name in rows}


def _fold(value):
    return str(value).strip().casefold()


def role_candidates(query, roles=None, aliases=None):
    roles = roles or load_roles()
    aliases = aliases if aliases is not None else load_aliases()
    folded = _fold(query)
    alias_value = next(
        (value for key, value in aliases.items() if _fold(key) == folded),
        None,
    )
    if alias_value is not None:
        target = _fold(alias_value)
        exact = [(role_id, name) for role_id, name in roles.items()
                 if _fold(name) == target or _fold(role_id) == target]
        if exact:
            return exact

    exact = [(role_id, name) for role_id, name in roles.items()
             if _fold(role_id) == folded or _fold(name) == folded]
    if exact:
        return exact
    return [(role_id, name) for role_id, name in roles.items()
            if folded in _fold(role_id) or folded in _fold(name)]


def resolve_roles(queries):
    roles = load_roles()
    aliases = load_aliases()
    resolved = []
    for query in queries:
        matches = role_candidates(query, roles, aliases)
        if not matches:
            return None, '没有找到角色“{}”。'.format(query)
        if len(matches) > 1:
            labels = '、'.join('{}（{}）'.format(name, role_id)
                              for role_id, name in matches[:12])
            return None, '“{}”有重名角色：{}'.format(query, labels)
        resolved.append(matches[0][0])
    if len(set(resolved)) != len(resolved):
        return None, '输入中有重复角色。'
    return resolved, None


def _role_name_map():
    try:
        return load_roles()
    except Exception:
        return {}


def _current_members(conn):
    return conn.execute(
        '''
        SELECT m.*, d.snapshot_date, d.sort_order
        FROM gvg_defenses AS d
        JOIN gvg_members AS m ON m.cuid = d.cuid
        ORDER BY d.sort_order, m.cuid
        '''
    ).fetchall()


def _avatar_tokens(role_id, roles, aliases):
    tokens = {role_id, roles.get(role_id, role_id)}
    canonical = _fold(roles.get(role_id, role_id))
    for alias, value in aliases.items():
        if _fold(value) == canonical:
            tokens.add(str(alias))
    return {token for token in tokens if token}


def _player_options(rows, roles, aliases):
    result = []
    for row in rows:
        avatar_name = roles.get(row['avatar_role_id'], row['avatar_role_id'])
        tokens = _avatar_tokens(row['avatar_role_id'], roles, aliases)
        result.append((row, avatar_name, tokens))
    return result


def resolve_player(query, conn=None):
    close_conn = conn is None
    conn = conn or connect_data()
    try:
        rows = _current_members(conn)
        roles = _role_name_map()
        aliases = load_aliases()
        options = _player_options(rows, roles, aliases)
        query_folded = _fold(query)

        exact_name = [item for item in options
                      if _fold(item[0]['name']) == query_folded]
        if len(exact_name) == 1:
            return exact_name[0][0], None

        qualified = []
        for row, _, tokens in options:
            for token in tokens:
                labels = (str(row['name']) + token,
                          str(row['name']) + ' ' + token)
                if any(_fold(label) == query_folded for label in labels):
                    qualified.append(row)
                    break
        unique = {int(row['cuid']): row for row in qualified}
        if len(unique) == 1:
            return next(iter(unique.values())), None

        if len(exact_name) > 1 or len(unique) > 1:
            ambiguous = exact_name or list(unique.values())
            labels = '、'.join('{}{}'.format(
                row['name'], roles.get(row['avatar_role_id'],
                                       row['avatar_role_id']))
                for row in ambiguous)
            return None, '玩家有重名，请加头像角色名：{}'.format(labels)

        partial = [item[0] for item in options
                   if query_folded in _fold(item[0]['name'])]
        if len(partial) == 1:
            return partial[0], None
        if len(partial) > 1:
            labels = '、'.join('{}{}'.format(
                row['name'], roles.get(row['avatar_role_id'],
                                       row['avatar_role_id']))
                for row in partial)
            return None, '玩家名匹配到多人，请加头像角色名：{}'.format(labels)
        return None, '没有找到玩家“{}”。'.format(query)
    finally:
        if close_conn:
            conn.close()


def _rounds_since(conn, since_ts):
    units = defaultdict(lambda: {'atk': [], 'def': []})
    for row in conn.execute(
        '''
        SELECT u.*
        FROM gvg_units AS u
        JOIN gvg_rounds AS r
          ON r.battle_id=u.battle_id AND r.round_idx=u.round_idx
        WHERE r.start_ts >= ?
        ORDER BY u.battle_id, u.round_idx, u.side, u.pos
        ''',
        (since_ts,),
    ):
        units[(row['battle_id'], int(row['round_idx']))][row['side']].append(
            row)
    rounds = []
    for row in conn.execute(
        'SELECT * FROM gvg_rounds WHERE start_ts >= ? '
        'ORDER BY start_ts, battle_id, round_idx',
        (since_ts,),
    ):
        teams = units[(row['battle_id'], int(row['round_idx']))]
        if len(teams['atk']) != 3 or len(teams['def']) != 3:
            continue
        rounds.append({
            'row': row,
            'atk': tuple(unit['role_id'] for unit in teams['atk']),
            'def': tuple(unit['role_id'] for unit in teams['def']),
            'atk_dead': any(bool(unit['dead']) for unit in teams['atk']),
        })
    return rounds


def format_solutions(role_ids, db_path=DATA_DB_PATH):
    init_database(db_path)
    config = load_config()
    conn = connect_data(db_path)
    try:
        since_ts = _now_ms() - RECENT_DAYS * MILLIS_PER_DAY
        rounds = _rounds_since(conn, since_ts)
    finally:
        conn.close()
    target = tuple(sorted(role_ids))
    roles = _role_name_map()
    sections = []
    for guild in (config['our_guild_name'], config['target_guild_name']):
        grouped = defaultdict(list)
        for item in rounds:
            row = item['row']
            if row['atk_guild'] == guild and tuple(sorted(item['def'])) == target:
                grouped[item['atk']].append(item)
        ranked = []
        for team, items in grouped.items():
            total = len(items)
            wins = sum(int(item['row']['win']) for item in items)
            drops = sum(int(item['atk_dead']) for item in items)
            ranked.append((wins / total, total, team, drops))
        ranked.sort(key=lambda item: (
            item[0], item[1], tuple(roles.get(role, role) for role in item[2])
        ), reverse=True)
        lines = ['{}解法：'.format(guild)]
        if not ranked:
            lines.append('- 暂无记录')
        for rate, total, team, drops in ranked:
            names = '+'.join(roles.get(role, role) for role in team)
            win_pct = int(rate * 100 + 0.5)
            drop_pct = int(drops / total * 100 + 0.5)
            lines.append('- {}，胜率{}%，掉人率{}%'.format(
                names, win_pct, drop_pct))
        sections.append('\n'.join(lines))
    return '\n\n'.join(sections)


def _defense_units(conn):
    units = defaultdict(lambda: {1: [], 2: []})
    for row in conn.execute(
        'SELECT * FROM gvg_defense_units ORDER BY cuid, half, pos'):
        units[int(row['cuid'])][int(row['half'])].append(row['role_id'])
    return units


def format_defenses(db_path=DATA_DB_PATH):
    init_database(db_path)
    config = load_config()
    conn = connect_data(db_path)
    try:
        members = _current_members(conn)
        units = _defense_units(conn)
    finally:
        conn.close()
    if not members:
        return '暂无防守数据，请先更新。'
    roles = _role_name_map()
    date = members[0]['snapshot_date']
    title = '{} {}防守'.format(date, config['target_guild_name'])
    blocks = []
    for index, member in enumerate(members, 1):
        avatar = roles.get(member['avatar_role_id'], member['avatar_role_id'])
        first = '+'.join(roles.get(role, role)
                         for role in units[int(member['cuid'])][1]) or '-'
        second = '+'.join(roles.get(role, role)
                          for role in units[int(member['cuid'])][2]) or '-'
        blocks.append('\n\n'.join((
            '{:02d}. {}（{}）'.format(index, member['name'], avatar),
            '上半：{}'.format(first),
            '下半：{}'.format(second),
        )))
    return title + '\n\n' + '\n\n'.join(blocks)


def member_defense_stats(conn, cuid, our_guild_name):
    since_ts = _now_ms() - RECENT_DAYS * MILLIS_PER_DAY
    row = conn.execute(
        '''
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN win = 0 THEN 1 ELSE 0 END) AS successes
        FROM gvg_rounds
        WHERE start_ts >= ? AND def_cuid = ? AND atk_guild = ?
        ''',
        (since_ts, int(cuid), our_guild_name),
    ).fetchone()
    if not row or not row['total']:
        return 0, 0
    return int(row['successes'] or 0), int(row['total'])


def member_defense_rate(conn, cuid, our_guild_name):
    successes, total = member_defense_stats(conn, cuid, our_guild_name)
    if not total:
        return '-'
    return '{:.1f}%'.format(successes / total * 100)


def format_win_rates(db_path=DATA_DB_PATH):
    init_database(db_path)
    config = load_config()
    conn = connect_data(db_path)
    try:
        members = _current_members(conn)
        if not members:
            return '暂无防守数据，请先更新。'
        roles = _role_name_map()
        ranked = []
        for member in members:
            successes, total = member_defense_stats(
                conn, member['cuid'], config['our_guild_name'])
            rate = successes / total * 100 if total else None
            ranked.append((member, rate, total))
        ranked.sort(key=lambda item: (
            item[1] is None,
            -(item[1] or 0),
            -item[2],
            int(item[0]['sort_order']),
        ))
        lines = ['{} {}防守胜率'.format(
            members[0]['snapshot_date'], config['target_guild_name'])]
        for index, (member, rate, _) in enumerate(ranked, 1):
            avatar = roles.get(member['avatar_role_id'],
                               member['avatar_role_id'])
            rate_text = '-' if rate is None else '{:.1f}%'.format(rate)
            lines.append('{:02d}. {}（{}）{}'.format(
                index, member['name'], avatar, rate_text))
        return '\n'.join(lines)
    finally:
        conn.close()


def _normalize_speed(value):
    match = re.fullmatch(r'(\d{1,4})(?:-(\d{1,4}))?', value.strip())
    if not match:
        return None
    lower = int(match.group(1))
    upper = int(match.group(2)) if match.group(2) else None
    if lower <= 0 or (upper is not None and upper < lower):
        return None
    return str(lower) if upper is None else '{}-{}'.format(lower, upper)


def set_first_speed(player_query, speed, db_path=DATA_DB_PATH):
    init_database(db_path)
    normalized = _normalize_speed(speed)
    if normalized is None:
        return '一速格式错误，请输入 227 或 265-270。'
    conn = connect_data(db_path)
    try:
        player, error = resolve_player(player_query, conn)
        if error:
            return error
        conn.execute(
            'UPDATE gvg_members SET first_speed=?, updated_at=? WHERE cuid=?',
            (normalized, _now_ms(), int(player['cuid'])),
        )
        conn.commit()
        return '已更新 {} 的一速：{}'.format(player['name'], normalized)
    finally:
        conn.close()


def _resolve_player_prefix(text, conn):
    tokens = text.strip().split()
    if len(tokens) < 2:
        return None, None, '格式：团战 信息 玩家名 信息内容'
    errors = []
    for split_at in range(len(tokens) - 1, 0, -1):
        query = ' '.join(tokens[:split_at])
        player, error = resolve_player(query, conn)
        if player is not None:
            return player, ' '.join(tokens[split_at:]), None
        errors.append(error)
    return None, None, errors[-1] if errors else '没有找到玩家。'


def set_member_info(text, db_path=DATA_DB_PATH, weekday=None):
    init_database(db_path)
    weekday = datetime.now().isoweekday() if weekday is None else weekday
    if weekday not in (1, 3, 5):
        return '临时信息只在周一、周三、周五保存。'
    conn = connect_data(db_path)
    try:
        player, info, error = _resolve_player_prefix(text, conn)
        if error:
            return error
        conn.execute(
            'UPDATE gvg_members SET info=?, info_date=?, updated_at=? '
            'WHERE cuid=?',
            (info, _today(), _now_ms(), int(player['cuid'])),
        )
        conn.commit()
        return '已保存 {} 的信息。'.format(player['name'])
    finally:
        conn.close()


def format_player(player_query, db_path=DATA_DB_PATH):
    init_database(db_path)
    config = load_config()
    conn = connect_data(db_path)
    try:
        player, error = resolve_player(player_query, conn)
        if error:
            return error
        roles = _role_name_map()
        avatar = roles.get(player['avatar_role_id'], player['avatar_role_id'])
        lines = ['{}（{}）'.format(player['name'], avatar)]
        if player['first_speed']:
            lines.append('一速：{}'.format(player['first_speed']))
        lines.append('防守胜率：{}'.format(member_defense_rate(
            conn, player['cuid'], config['our_guild_name'])))
        if player['info']:
            lines.append(str(player['info']))
        return '\n'.join(lines)
    finally:
        conn.close()


def update_result_text(result):
    parts = []
    if result['defenses'] is not None:
        parts.append('防守 {} 人'.format(result['defenses']))
    if result['battles'] is not None:
        parts.append('互打战斗 {} 场'.format(result['battles']))
    parts.append('别名 {} 条'.format(result['aliases']))
    parts.append('master.db {}'.format(
        '已更新' if result['master_changed'] else '无需更新'))
    text = '团战数据更新完成：' + '，'.join(parts)
    if result['warnings']:
        text += '\n' + '\n'.join(result['warnings'])
    return text


async def report_to_superuser(message):
    import hoshino
    from hoshino.config import SUPERUSERS

    if not SUPERUSERS:
        return
    bot = hoshino.get_bot()
    self_ids = list(bot.get_self_ids())
    if not self_ids:
        return
    await bot.send_private_msg(
        self_id=random.choice(self_ids),
        user_id=SUPERUSERS[0],
        message=message,
    )


async def run_update_job(service, mode, bot=None, ev=None,
                         notify_superuser=True):
    try:
        result = await asyncio.to_thread(update_all_sync, mode)
        message = update_result_text(result)
        if notify_superuser:
            for warning in result['warnings']:
                await report_to_superuser('团战更新警告：\n' + warning)
        if bot is not None and ev is not None:
            await bot.send(ev, message, at_sender=True)
        else:
            service.logger.info(message)
    except Exception as exc:
        service.logger.exception(exc)
        message = '团战数据更新失败：\n{}'.format(exc)
        if bot is not None and ev is not None:
            await bot.send(ev, message, at_sender=True)
        if notify_superuser:
            await report_to_superuser(message)


GVG_HELP = (
    '团战指令：\n'
    '团战 作业 角色1 角色2 角色3\n'
    '团战 防守\n'
    '团战 胜率表\n'
    '团战 一速 玩家名 227（或265-270）\n'
    '团战 信息 玩家名 信息内容\n'
    '团战 玩家名（重名时在名字后加头像角色名）\n'
    '团战 更新数据（仅限机器人主人）'
)


def _format_query_reply(message):
    return str(message).lstrip('\r\n')


_REGISTERED = False


def register_gvg(service):
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    init_database()

    @service.scheduled_job('cron', day_of_week='mon,wed,fri', hour=8,
                           minute=5)
    async def gvg_update_defenses():
        await run_update_job(service, 'defense')

    @service.scheduled_job('cron', day_of_week='tue,thu,sat', hour=8,
                           minute=5)
    async def gvg_update_battles():
        await run_update_job(service, 'battles')

    @service.on_prefix('团战')
    async def gvg_command(bot, ev):
        raw = ev.message.extract_plain_text().strip()
        if raw.startswith(('测速', '总结')):
            return
        if not raw:
            await bot.send(ev, GVG_HELP, at_sender=True)
            return

        if raw == '更新数据':
            from hoshino.config import SUPERUSERS
            if str(ev.user_id) not in {str(user) for user in SUPERUSERS}:
                await bot.send(ev, '只有机器人主人可以强制更新数据。',
                               at_sender=True)
                return
            await bot.send(ev, '开始更新团战数据，请稍候。', at_sender=True)
            await run_update_job(
                service, 'both', bot, ev, notify_superuser=False)
            return

        if raw == '防守':
            try:
                message = format_defenses()
            except Exception as exc:
                message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=False)
            return

        if raw == '胜率表':
            try:
                message = format_win_rates()
            except Exception as exc:
                message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=False)
            return

        if raw.startswith('作业'):
            queries = raw[len('作业'):].strip().split()
            if len(queries) != 3:
                message = '格式：团战 作业 角色1 角色2 角色3'
            else:
                try:
                    role_ids, error = resolve_roles(queries)
                    message = error or format_solutions(role_ids)
                except Exception as exc:
                    message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=True)
            return

        if raw.startswith('一速'):
            content = raw[len('一速'):].strip()
            match = re.fullmatch(r'(.+?)\s+(\d{1,4}(?:-\d{1,4})?)', content)
            if not match:
                message = '格式：团战 一速 玩家名 227（或265-270）'
            else:
                try:
                    message = set_first_speed(match.group(1), match.group(2))
                except Exception as exc:
                    message = '更新失败：{}'.format(exc)
            await bot.send(ev, message, at_sender=True)
            return

        if raw.startswith('信息'):
            try:
                message = set_member_info(raw[len('信息'):].strip())
            except Exception as exc:
                message = '更新失败：{}'.format(exc)
            await bot.send(ev, message, at_sender=True)
            return

        try:
            message = format_player(raw)
        except Exception as exc:
            message = '查询失败：{}'.format(exc)
        await bot.send(ev, _format_query_reply(message), at_sender=True)
