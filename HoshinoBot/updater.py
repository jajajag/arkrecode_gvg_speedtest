import json
import re
import threading
from pathlib import Path

import requests

from .api import (
    GameRequestError,
    get_game_client,
    oid,
    team_roles,
    query_bulletin,
    query_battle_detail,
    query_member_logs,
    query_guild,
    query_top_guilds,
    query_pvp_ranks,
)
from .database import (
    ALIAS_PATH,
    DATA_DB_PATH,
    MASTER_DB_PATH,
    connect_data,
    clear_current_member_match,
    existing_battle_ids,
    init_database,
    meta_get,
    meta_set,
    save_battle_rows,
    save_defence_match,
    save_member_match,
    save_player_equipment,
)
from .daily import run_daily_cleanup
from .master import update_master_db

WORKFLOW_LOCK = threading.Lock()

# User-confirmed season schedule: season 11 begins at week 662 (13 weeks each).
GVG_SEASON_WEEKS = 13
GVG_FIRST_SEASON_WEEK = 532


def gvg_collection_pause(data):
    """Wait for stable rankings through the first week's settlement (~1)."""
    server = (data.get('GuildWarData') or {}).get('ServerInfo') or {}
    try:
        start_week = int(server['StartWeek'])
        season = int(server['NowSeasonID'])
        match = re.fullmatch(r'(\d+)~([135])', str(server['NowGuildWarID']))
        if match is None or season < 1 or start_week != GVG_FIRST_SEASON_WEEK:
            raise ValueError
        week, day = map(int, match.groups())
        first_week = start_week + GVG_SEASON_WEEKS * (season - 1)
        if not first_week <= week < first_week + GVG_SEASON_WEEKS:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return '赛季信息缺失或与13周规则不符，暂缓前20公会战绩采集'
    if (week, day) < (first_week + 1, 3):
        return '第{}赛季初期，前20公会战绩采集从 {}~3 开始（当前 {}）'.format(
            season, first_week + 1, server['NowGuildWarID'])
    return None


def current_match_id(data):
    server = (data.get('GuildWarData') or {}).get('ServerInfo') or {}
    match_id = str(server.get('NowGuildWarID') or '')
    if not re.fullmatch(r'\d+~[135]', match_id):
        raise GameRequestError('团战响应缺少有效 NowGuildWarID')
    return match_id

# Borrowed from StardustChocolate/openrubi
ALIAS_URL = (
    'https://github.com/StardustChocolate/openrubi/raw/refs/heads/main/'
    'arkrecode/members/character_dic.json'
)


def collect_defences(data, db_path=DATA_DB_PATH):
    war = data.get('GuildWarData')
    if not isinstance(war, dict):
        raise GameRequestError('团战响应缺少有效 GuildWarData，无法判断开战状态')
    def guild(camp):
        info = (camp or {}).get('GuildInfo') or {}
        return {'id': oid(info.get('_id')), 'name': str(info.get('Name') or '')}
    our = guild(war.get('MyCampData'))
    if not our['name']:
        raise GameRequestError('团战响应缺少我方公会信息')
    camp = war.get('EnemyCampData') or {}
    enemy = guild(camp)
    players = camp.get('PlayerInfoList') or []
    if not isinstance(players, list):
        raise GameRequestError('敌方防守列表格式异常')
    members = []
    for player in players:
        info = player.get('PlayerInfo') or {}
        defence = player.get('DefenceTeamData')
        if info.get('CUID') is None or not isinstance(defence, dict):
            raise GameRequestError('敌方成员缺少 CUID 或防守数据')
        member = dict(cuid=int(info['CUID']), name=str(info.get('Name') or info['CUID']))
        for key in ('FirstTeam', 'SecondTeam'):
            roles = team_roles(defence.get(key) or {})
            if len(roles) != 3:
                raise GameRequestError('敌方防守阵容不完整，暂不更新当前名单')
        members.append(member)
    if members and not enemy['name']:
        raise GameRequestError('敌方防守存在但缺少公会信息')
    save_defence_match(players, enemy, db_path)
    return our, enemy, members


def collect_equipment(client, members, db_path=DATA_DB_PATH):
    saved, failures = 0, []
    for member in members:
        try:
            card = client.call('AccountHandler.QueryPlayerCardData',
                               {'CUID': member['cuid']}, required_key='BattleSupportData')
            if not isinstance(card.get('PVPInfo'), dict):
                raise GameRequestError('玩家卡缺少竞技场防守数据')
            save_player_equipment(card, member['cuid'], member['name'], db_path)
            saved += 1
        except Exception as exc:
            failures.append('装备采集 {}（{}）失败：{}'.format(
                member['name'], member['cuid'], exc))
    return saved, failures


def collect_rank_equipment(client, db_path=DATA_DB_PATH):
    rows = query_pvp_ranks(client)
    saved = 0
    for item in rows[:100]:
        info = item.get('PlayerInfo') or {}
        if info.get('CUID') is None or not isinstance(item.get('PVPInfo'), dict):
            raise GameRequestError('竞技场排名缺少玩家或防守数据')
        saved += save_player_equipment(item, info['CUID'], info.get('Name') or str(info['CUID']), db_path)
    return saved


def guild_members(guild_data):
    guild = guild_data.get('GuildData') or {}
    members = guild.get('MemberList')
    if members is None:
        members = guild.get('MemberInfoList')
    if members is None:
        members = guild_data.get('MemberList')
    if members is None:
        members = guild_data.get('MemberInfoList')
    if not isinstance(members, list):
        raise GameRequestError('公会响应缺少成员列表')
    return members


def partial_guild_info(data):
    guild = (data or {}).get('GuildData') or {}
    info = (guild.get('Info') or guild.get('GuildInfo')
            or guild.get('GuildSubInfo') or guild)
    return {
        'id': oid(info.get('_id') or guild.get('_id')),
        'name': str(info.get('Name') or guild.get('Name') or ''),
    }


def partial_enemy_guild_id(data):
    """Old partial-guild responses may nest the campaign at different levels."""
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


def collect_target_members(client, war_data=None, db_path=DATA_DB_PATH):
    target_id = str(client.config.get('TargetGuildID') or
                    client.config.get('GuildID') or '').strip()
    if not target_id:
        raise GameRequestError('SubAccount 缺少 TargetGuildID')
    target_data = query_guild(client, target_id)
    target = partial_guild_info(target_data)
    if not target['name']:
        raise GameRequestError('目标公会响应缺少公会名')
    target['id'] = target['id'] or target_id
    enemy_id = partial_enemy_guild_id(target_data)
    if not enemy_id:
        clear_current_member_match(db_path)
        return {'target': target['name'], 'enemy': None,
                'members': 0, 'match_id': None}
    if war_data is None:
        war_data = client.call('GuildWarHandler.QueryFullGuildWarData', {},
                               required_key='GuildWarData')
    match_id = current_match_id(war_data)
    enemy_data = query_guild(client, enemy_id)
    enemy = partial_guild_info(enemy_data)
    if not enemy['name']:
        raise GameRequestError('对手公会响应缺少公会名')
    enemy['id'] = enemy['id'] or enemy_id
    members = []
    for item in guild_members(enemy_data):
        player = item.get('PlayerInfo') or item
        cuid = player.get('CUID')
        if cuid is None:
            raise GameRequestError('对手公会成员缺少 CUID')
        members.append({
            'cuid': int(cuid),
            'name': str(player.get('Name') or cuid),
            'avatar_role_id': str(player.get('LeaderSID') or ''),
        })
    if not members:
        raise GameRequestError('对手公会没有成员数据')
    save_member_match(match_id, target, enemy, members, db_path)
    return {'target': target['name'], 'enemy': enemy['name'],
            'members': len(members), 'match_id': match_id}


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
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise GameRequestError('已有日常或数据采集任务运行中')
    try:
        return _update_all_sync(run_daily)
    finally:
        WORKFLOW_LOCK.release()


def cleanup_account(client):
    """Return one status while keeping daily failures local to the account."""
    try:
        report = run_daily_cleanup(client, client.login_data)
        return '；'.join(report['warnings']) or '正常'
    except Exception as exc:
        return '失败：{}'.format(exc)


def _update_all_sync(run_daily=False):
    progress = {
        '大号日常': '未执行' if run_daily else '未执行（本次仅更新数据）',
        '大号数据查询': '未执行',
        '小号日常': '未执行' if run_daily else '未执行（本次仅更新数据）',
        '小号目标名单': '未执行',
        '小号数据采集': '未执行',
    }
    warnings = []
    try:
        result = _update_all_with_progress(run_daily, progress, warnings)
    except Exception as exc:
        running = [name for name, status in progress.items() if status == '运行中']
        for name in running:
            progress[name] = '失败：{}'.format(exc)
        if not running:
            warnings.append('错误：{}'.format(exc))
        result = {'warnings': warnings, 'failed': True}
    result['progress'] = progress
    return result


def _update_all_with_progress(run_daily, progress, warnings):
    init_database()
    client = get_game_client(reload_config=True, account='main')
    alt = get_game_client(reload_config=True, account='alt')
    client.login(attempts=3, force=True)
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

    if run_daily:
        progress['大号日常'] = cleanup_account(client)
    progress['大号数据查询'] = '运行中'
    war = our_guild = enemy_guild = None
    members = []
    try:
        war = client.call('GuildWarHandler.QueryFullGuildWarData', {},
                          required_key='GuildWarData')
        our_guild, enemy_guild, members = collect_defences(war)
        progress['大号数据查询'] = '正常'
    except Exception as exc:
        progress['大号数据查询'] = '失败：{}'.format(exc)

    progress['小号数据采集'] = '运行中'
    try:
        alt.login(attempts=3, force=True)
    except Exception:
        if run_daily:
            progress['小号日常'] = '未执行（登录失败）'
        raise
    progress['小号目标名单'] = '运行中'
    target_result = None
    try:
        target_result = collect_target_members(alt, war)
        progress['小号目标名单'] = '正常'
    except Exception as exc:
        progress['小号目标名单'] = '失败：{}'.format(exc)

    if run_daily:
        progress['小号日常'] = cleanup_account(alt)

    result = {
        'our_guild': our_guild['name'] if our_guild else None,
        'enemy_guild': None,
        'members': None,
        'battles': None,
        'ranked_guilds': None,
        'aliases': alias_count,
        'master_changed': master_changed,
        'warnings': warnings,
    }
    if target_result:
        result.update({
            'target_guild': target_result['target'],
            'target_enemy': target_result['enemy'],
            'target_members': target_result['members'],
            'target_match_id': target_result['match_id'],
        })

    collection_warnings = []
    if progress['大号数据查询'] != '正常':
        collection_warnings.append('大号防守查询失败，跳过对手装备采集')
    elif members:
        result['enemy_guild'] = enemy_guild['name']
        result['members'] = len(members)
        saved, failures = collect_equipment(alt, members)
        result['equipment'] = saved
        collection_warnings.extend(failures)
    elif war is not None:
        pause = gvg_collection_pause(war)
        if pause:
            result['gvg_collection_pause'] = pause
        else:
            ranked_guilds = query_top_guilds(alt)
            result['ranked_guilds'] = len(ranked_guilds)
            battle_result = update_gvg_battles(alt, ranked_guilds)
            result['battles'] = battle_result['saved']
            for key, label in (('member_failures', '成员日志'),
                               ('detail_failures', '战斗详情'),
                               ('parse_failures', '战斗解析')):
                if battle_result[key]:
                    collection_warnings.append('{}失败 {} 条'.format(label, battle_result[key]))
    try:
        collect_rank_equipment(alt)
    except Exception as exc:
        collection_warnings.append('竞技场装备采集失败：{}'.format(exc))
    progress['小号数据采集'] = (
        '；'.join(collection_warnings) or '正常')
    return result


def run_daily_sync():
    if not WORKFLOW_LOCK.acquire(blocking=False):
        raise GameRequestError('已有日常或数据采集任务运行中')
    try:
        summaries = []
        for account, label in (('main', '大号'), ('alt', '小号')):
            try:
                client = get_game_client(reload_config=True, account=account)
                client.login(attempts=3, force=True)
                status = cleanup_account(client)
            except Exception as exc:
                status = '失败：{}'.format(exc)
            summaries.append('{}日常：{}'.format(label, status))
        return {'summary': '\n'.join(summaries)}
    finally:
        WORKFLOW_LOCK.release()


def daily_result_text(result):
    return '日常清理完成\n' + result['summary']


def update_result_text(result):
    progress = result['progress']
    failed = result.get('failed') or any(
        status.startswith('失败') for status in progress.values())
    lines = ['团战任务结束（有失败）' if failed else '团战任务完成']
    lines.extend('{}：{}'.format(name, status) for name, status in progress.items())
    if 'our_guild' in result:
        lines.append(update_details_text(result))
    lines.extend(result['warnings'])
    return '\n'.join(lines)


def update_details_text(result):
    parts = []
    if result.get('enemy_guild'):
        parts.append('当前对战 {} vs {}'.format(
            result['our_guild'], result['enemy_guild']))
    elif not result.get('our_guild'):
        parts.append('大号防守数据查询失败')
    else:
        parts.append('今日未开启团战')
    if result.get('target_guild'):
        if result.get('target_enemy'):
            parts.append('小号目标 {} vs {}（{}，{} 人）'.format(
                result['target_guild'], result['target_enemy'],
                result['target_match_id'], result['target_members']))
        else:
            parts.append('小号目标 {} 暂无对手'.format(result['target_guild']))
    if result['ranked_guilds'] is not None:
        parts.append('前排团 {} 个'.format(result['ranked_guilds']))
    if result['members'] is not None:
        parts.append('敌方成员 {} 人'.format(result['members']))
    if result['battles'] is not None:
        parts.append('团战战斗新增 {} 场'.format(result['battles']))
    if result.get('gvg_collection_pause'):
        parts.append(result['gvg_collection_pause'])
    if 'equipment' in result:
        parts.append('装备采集 {} 人'.format(result['equipment']))
    parts.append('别名 {} 条'.format(result['aliases']))
    parts.append('master.db {}'.format(
        '已更新' if result['master_changed'] else '无需更新'))
    return '团战数据更新完成：' + '，'.join(parts)
