import json
from pathlib import Path

import requests

from .api import (
    GameRequestError,
    get_game_client,
    oid,
    query_bulletin,
    query_battle_detail,
    query_member_logs,
    query_guild,
    query_top_guilds,
)
from .database import (
    ALIAS_PATH,
    DATA_DB_PATH,
    MASTER_DB_PATH,
    connect_data,
    existing_battle_ids,
    init_database,
    meta_get,
    meta_set,
    replace_current_members,
    save_battle_rows,
    update_our_guild_meta,
)
from .daily import run_daily_cleanup
from .master import update_master_db

# Borrowed from StardustChocolate/openrubi
ALIAS_URL = (
    'https://github.com/StardustChocolate/openrubi/raw/refs/heads/main/'
    'arkrecode/members/character_dic.json'
)


def partial_guild_info(data):
    guild = (data or {}).get('GuildData') or {}
    info = (
        guild.get('Info')
        or guild.get('GuildInfo')
        or guild.get('GuildSubInfo')
        or guild
    )
    return {
        'id': oid(info.get('_id') or guild.get('_id')),
        'name': str(info.get('Name') or guild.get('Name') or ''),
    }


def partial_enemy_guild_id(data):
    if isinstance(data, dict):
        candidate = data.get('EnemyGuildID')
        if candidate:
            return oid(candidate)
        for value in data.values():
            guild_id = partial_enemy_guild_id(value)
            if guild_id:
                return guild_id
    elif isinstance(data, list):
        for value in data:
            guild_id = partial_enemy_guild_id(value)
            if guild_id:
                return guild_id
    return ''


def guild_members(guild_data):
    guild = guild_data.get('GuildData') or {}
    members = None
    for source in (guild, guild_data):
        for key in ('MemberList', 'MemberInfoList'):
            if key in source:
                members = source[key]
                break
        if members is not None:
            break
    if not isinstance(members, list):
        raise GameRequestError('公会响应缺少成员列表')
    return members


def guild_member_snapshots(guild_data):
    result = []
    for order, member in enumerate(guild_members(guild_data), 1):
        player = member.get('PlayerInfo') or member
        cuid = player.get('CUID')
        if cuid is None:
            raise GameRequestError('公会成员中有成员缺少 CUID')
        result.append({
            'cuid': int(cuid),
            'name': str(player.get('Name') or cuid),
            'avatar_role_id': str(player.get('LeaderSID') or ''),
            'order': order,
        })
    return result


def collect_gvg_battle_refs(client, guild_data_list):
    refs = {}
    failed_members = 0
    seen_cuids = set()
    for guild_data in guild_data_list:
        for member in guild_members(guild_data):
            player = member.get('PlayerInfo') or {}
            cuid = player.get('CUID')
            if cuid is None or int(cuid) in seen_cuids:
                continue
            seen_cuids.add(int(cuid))
            try:
                logs = query_member_logs(client, cuid)
            except Exception:
                failed_members += 1
                continue
            for item in logs.get('SubLogs') or []:
                battle_id = oid(item.get('_id'))
                if battle_id:
                    refs[battle_id] = item
    return refs, failed_members


def _parsed_unit(pos, role, dead_ids):
    object_id = oid((role or {}).get('_id'))
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
    battle_id = oid(log.get('_id'))
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
        dead_ids = {oid(value) for value in item.get('Camp1DeadList') or []}
        dead_ids.update(oid(value) for value in item.get('Camp2DeadList') or [])
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


def update_gvg_battles(client, guild_data_list, db_path=DATA_DB_PATH):
    refs, member_failures = collect_gvg_battle_refs(
        client, guild_data_list)
    init_database(db_path)
    conn = connect_data(db_path)
    saved = 0
    detail_failures = 0
    parse_failures = 0
    consecutive_failures = 0
    try:
        known_ids = existing_battle_ids(refs, db_path, conn)
        for battle_id in sorted(set(refs) - known_ids):
            try:
                detail = query_battle_detail(client, battle_id)
            except Exception:
                detail_failures += 1
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    client.login(attempts=3, force=True)
                    consecutive_failures = 0
                continue
            consecutive_failures = 0
            try:
                rows = parse_battle_detail(detail)
            except Exception:
                parse_failures += 1
                continue
            if save_battle_rows(rows, db_path, conn):
                saved += 1
    finally:
        conn.close()
    return {
        'saved': saved,
        'member_failures': member_failures,
        'detail_failures': detail_failures,
        'parse_failures': parse_failures,
    }


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


def update_master(bulletin, http):
    conn = connect_data()
    try:
        current_catalog = meta_get(conn, 'master_catalog')
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
        meta_set(conn, 'master_catalog', catalog)
        conn.commit()
    finally:
        conn.close()
    return catalog, changed


def update_all_sync(run_daily=False):
    init_database()
    client = get_game_client(reload_config=True)
    client.login(attempts=3, force=True)
    our_guild_id = str(client.config.get('GuildID') or '').strip()
    if not our_guild_id:
        raise GameRequestError('account.json 缺少 GuildID')

    our_guild_data = query_guild(client, our_guild_id)
    our_guild = partial_guild_info(our_guild_data)
    if not our_guild['name']:
        raise GameRequestError('公会响应缺少我方团信息')
    our_guild['id'] = our_guild['id'] or our_guild_id
    update_our_guild_meta(our_guild)
    enemy_guild_id = partial_enemy_guild_id(our_guild_data)

    warnings = []
    master_changed = False
    alias_count = 0
    with requests.Session() as download_session:
        try:
            bulletin = query_bulletin(download_session)
            _, master_changed = update_master(
                bulletin, download_session)
        except Exception as exc:
            warnings.append('master.db 更新失败：{}'.format(exc))
        try:
            alias_count = update_aliases(session=download_session)
        except Exception as exc:
            warnings.append('角色别名表下载失败：{}'.format(exc))

    result = {
        'our_guild': our_guild['name'],
        'enemy_guild': None,
        'members': None,
        'battles': None,
        'ranked_guilds': None,
        'aliases': alias_count,
        'master_changed': master_changed,
        'warnings': warnings,
    }

    if enemy_guild_id:
        enemy_guild_data = query_guild(client, enemy_guild_id)
        enemy_guild = partial_guild_info(enemy_guild_data)
        if not enemy_guild['name']:
            raise GameRequestError('公会响应缺少敌方团信息')
        enemy_guild['id'] = enemy_guild['id'] or enemy_guild_id
        members = guild_member_snapshots(enemy_guild_data)
        if not members:
            raise GameRequestError('敌方公会没有成员数据')
        replace_current_members(members, enemy_guild)
        result['enemy_guild'] = enemy_guild['name']
        result['members'] = len(members)
        battle_sources = [our_guild_data, enemy_guild_data]
    else:
        ranked_guilds = query_top_guilds(client)
        result['ranked_guilds'] = len(ranked_guilds)
        battle_sources = ranked_guilds
    battle_result = update_gvg_battles(client, battle_sources)
    result['battles'] = battle_result['saved']
    if battle_result['member_failures']:
        warnings.append('有 {} 名成员日志查询失败，已跳过'.format(
            battle_result['member_failures']))
    if battle_result['detail_failures']:
        warnings.append('有 {} 场战斗详情查询失败，已跳过'.format(
            battle_result['detail_failures']))
    if battle_result['parse_failures']:
        warnings.append('有 {} 场战斗详情解析失败，已跳过'.format(
            battle_result['parse_failures']))
    if run_daily:
        try:
            daily_result = run_daily_cleanup(client, client.login_data)
            warnings.extend(daily_result['warnings'])
        except Exception as exc:
            warnings.append('日常清理失败：{}'.format(exc))
    return result


def update_result_text(result):
    parts = []
    if result['enemy_guild']:
        parts.append('当前对战 {} vs {}'.format(
            result['our_guild'], result['enemy_guild']))
    else:
        parts.append('今日未开启团战')
    if result['ranked_guilds'] is not None:
        parts.append('前排团 {} 个'.format(result['ranked_guilds']))
    if result['members'] is not None:
        parts.append('敌方成员 {} 人'.format(result['members']))
    if result['battles'] is not None:
        parts.append('团战战斗新增 {} 场'.format(result['battles']))
    parts.append('别名 {} 条'.format(result['aliases']))
    parts.append('master.db {}'.format(
        '已更新' if result['master_changed'] else '无需更新'))
    text = '团战数据更新完成：' + '，'.join(parts)
    if result['warnings']:
        text += '\n' + '\n'.join(result['warnings'])
    return text
