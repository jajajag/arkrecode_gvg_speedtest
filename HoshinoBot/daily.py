import copy
import re
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .api import GameRequestError, oid
from .database import MASTER_DB_PATH


LOCAL_TZ = timezone(timedelta(hours=8))
HUNT_STATUS_ID = 'HuntActivity'
HUNT_ELEMENTS = ('Fire', 'Ice', 'Earth', 'Light', 'Dark')
HUNT_NAMES = dict(zip(HUNT_ELEMENTS, ('火', '水', '木', '光', '暗')))
DEFAULT_HUNT_RUNS = {element: 1 for element in HUNT_ELEMENTS}
DAILY_FREE_SUMMON_ID = 'NormalSummon'
QUEST_BATCH_SIZE = 10
SUPPORT_ITEM_IDS = ('CR14', 'CR24', 'CR34', 'CR44', 'CR54')
LAB_REWARD_ROUTES = (
    ('ArkStarForceLabHandler.ChargeTesseract', 'NextCanChargeTime'),
    ('ArkStarForceLabHandler.RewardPotion', 'NextCanReceivePotionTime'),
    ('ArkStarForceLabHandler.RewardStarForce',
     'NextCanReceiveStarForceTime'),
    ('ArkStarForceLabHandler.RewardTesseract',
     'NextCanReceiveTesseractTime'),
)
DAILY_STORE_PURCHASES = (
    ({'StaticID': 'FriendShip3'}, 1),
    ({'StaticID': 'FriendShip4'}, 1),
    ({'StaticID': 'FriendShip6'}, 1),
    ({'StaticID': 'FriendShip7'}, 1),
    ({'StaticID': 'VIPGIFT_VIPQuick1'}, 1),
    ({'StaticID': 'VIPGIFT_VIPQuick2'}, 1),
    ({'StaticID': 'VIPGIFT_VIPQuick3'}, 1),
    ({'StaticID': 'VIPGIFT_VIPQuick4'}, 1),
    ({'StaticID': 'MedalHonor2'}, 3),
)
SCORE_REWARD_IDS = (
    ('DailyScore', (10, 20, 30, 50, 80, 100)),
    ('WeekScore', (20, 40, 60, 80, 100, 120)),
    ('FantasyStar', tuple(range(1, 11))),
)
BRANCH_REWARD_COUNT = 6
BRANCH_ACHIEVEMENT_COUNT = 8
SECRET_SHOP_ITEMS = {
    'EC11', 'EC21', 'EC31', 'EC41', 'EC51', 'EC61',
    '5', '6',
}
DEFAULT_SECRET_SHOP_REFRESH_LIMIT = 30


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def get_nested(value, *keys):
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def date_ms(value):
    try:
        if isinstance(value, dict):
            value = value.get('$date')
        return int(value)
    except (TypeError, ValueError):
        return 0


def now_ms():
    return int(time.time() * 1000)


def same_local_day(left_ms, right_ms=None):
    if not left_ms:
        return False
    right_ms = now_ms() if right_ms is None else int(right_ms)
    left_day = datetime.fromtimestamp(left_ms / 1000, LOCAL_TZ).date()
    right_day = datetime.fromtimestamp(right_ms / 1000, LOCAL_TZ).date()
    return left_day == right_day


def intv(value, default=0):
    try:
        return default if value in (None, '') else int(value)
    except (TypeError, ValueError):
        return default


class DailyReport:
    def __init__(self):
        self.warnings = []
        self.ok_counts = {}

    def ok(self, section):
        self.ok_counts[section] = self.ok_counts.get(section, 0) + 1

    def skip(self, section, reason=None):
        pass

    def warn(self, message):
        if len(self.warnings) < 8:
            self.warnings.append(message)

    def fail(self, section, exc):
        self.warn('{}：{}'.format(section, exc))


def safe_call(client, report, section, route, data=None,
              skip_if=False, delay=(0.15, 0.35),
              report_failure=False):
    if skip_if:
        report.skip(section)
        return None
    try:
        result = client.call_once(route, data or {}, delay=delay)
        report.ok(section)
        return result
    except Exception as exc:
        if report_failure:
            report.fail(section, exc)
        else:
            report.skip(section)
        return None


def table_columns(db_path, table):
    db_path = Path(db_path)
    if not db_path.is_file():
        return set()
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return {
                str(row[1])
                for row in conn.execute(
                    'PRAGMA table_info("{}")'.format(
                        table.replace('"', '""')))
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


def load_equipment_parts(db_path=MASTER_DB_PATH):
    if 'Part' not in table_columns(db_path, 'Equipment'):
        return {}
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            return {
                str(row[0]): str(row[1])
                for row in conn.execute('SELECT ID, Part FROM Equipment')
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return {}


def role_score(role):
    score = len(role)
    for key in (
            'StaticID', 'LV', 'AwakenLV', 'Star', 'Skills',
            'EquipmentMap', 'ArtifactData'):
        if key in role:
            score += 1
    return score


class LoginTeamBuilder:
    def __init__(self, login_data):
        self.login_data = login_data
        self.equipment_parts = load_equipment_parts()
        self.roles = self.collect_roles()
        self.equips, self.artifacts = self.collect_gear()

    def collect_roles(self):
        roles = {}
        for node in walk(self.login_data):
            role_id = oid(node.get('_id'))
            static_id = node.get('StaticID')
            if not role_id or not isinstance(static_id, str):
                continue
            if not static_id.startswith('H'):
                continue
            if 'Skills' not in node and 'LV' not in node \
                    and 'AwakenLV' not in node:
                continue
            old = roles.get(role_id)
            if old is None or role_score(node) > role_score(old):
                roles[role_id] = copy.deepcopy(node)
        return roles

    def collect_gear(self):
        equips = {}
        artifacts = {}
        for node in walk(self.login_data):
            role_id = oid(node.get('EquipRole'))
            static_id = node.get('StaticID')
            if not role_id or not isinstance(static_id, str):
                continue
            if 'MainProp' in node or 'SubProps' in node or 'Set' in node:
                part = node.get('Part') or self.equipment_parts.get(static_id)
                if part:
                    equips.setdefault(role_id, {})[part] = copy.deepcopy(node)
            elif 'Enhance' in node:
                artifacts[role_id] = copy.deepcopy(node)
        return equips, artifacts

    def role_for_team(self, role_id):
        role = copy.deepcopy(self.roles.get(role_id) or {})
        if not role:
            raise GameRequestError('登录数据里找不到队伍角色：{}'.format(role_id))
        role['_id'] = role_id
        if role_id in self.equips:
            role.setdefault('EquipmentMap', {}).update(self.equips[role_id])
        if role_id in self.artifacts:
            role.setdefault('ArtifactData', self.artifacts[role_id])
        return role

    def build_camp(self, setting):
        role_pos_map = get_nested(setting, 'TeamSetting', 'RolePosMap') or {}
        if not role_pos_map:
            return None
        pos_map = {}
        for raw_role_id, raw_pos in role_pos_map.items():
            pos_map[str(raw_pos)] = self.role_for_team(oid(raw_role_id))
        camp = {'PositionRoleMap': pos_map}
        if setting.get('Name'):
            camp['Name'] = setting.get('Name')
        return camp

    def teams(self):
        result = []
        settings = get_nested(self.login_data, 'Teams', 'Settings') or []
        for setting in settings:
            camp = self.build_camp(setting)
            if camp and camp.get('PositionRoleMap'):
                result.append(camp)
        return result


def first_team(login_data):
    teams = LoginTeamBuilder(login_data).teams()
    return teams[0] if teams else None


def scene_is_passed(scene):
    return any(scene.get('Stars') or []) or intv(scene.get('PassCount')) > 0


def highest_passed_scene(login_data, pattern):
    best = (0, None)
    scenes = get_nested(login_data, 'SceneDataContainer', 'Scenes') or []
    for scene in scenes:
        static_id = scene.get('StaticID')
        if not isinstance(static_id, str) or not scene_is_passed(scene):
            continue
        match = re.fullmatch(pattern, static_id)
        if not match:
            continue
        index = intv(match.group(1))
        if index > best[0]:
            best = (index, static_id)
    return best


def has_active_status(login_data, static_id):
    login_time = date_ms(
        get_nested(login_data, 'Info', 'LoginTime')) or now_ms()
    statuses = get_nested(login_data, 'StatusContainer', 'List') or []
    for status in statuses:
        if status.get('StaticID') != static_id:
            continue
        start = date_ms(status.get('TriggerTime'))
        end = date_ms(status.get('EndTime'))
        if (not start or start <= login_time) and (not end or login_time < end):
            return True
    return False


def hunt_run_counts(config):
    configured = config.get('DailyHuntRuns')
    if not isinstance(configured, dict):
        return dict(DEFAULT_HUNT_RUNS)
    return {
        element: max(intv(configured.get(element), 1), 0)
        for element in HUNT_ELEMENTS
    }


def hunt_rotation(counts):
    rounds = max(counts.values(), default=0)
    return [
        element
        for index in range(rounds)
        for element in HUNT_ELEMENTS
        if counts[element] > index
    ]


def pickup_from_login(login_data):
    login_time = date_ms(get_nested(login_data, 'Info', 'LoginTime'))
    candidates = []
    for node in walk(login_data):
        activity_id = node.get('ActivityID')
        if not isinstance(activity_id, str):
            continue
        match = re.search(r'H\d+', activity_id)
        if not match:
            continue
        start = date_ms(node.get('StartTime'))
        end = date_ms(node.get('EndTime'))
        if login_time and start and end and not start <= login_time <= end:
            continue
        suffix = match.group(0)
        priority = 0
        if activity_id == 'Branch{}'.format(suffix):
            priority = 3
        elif activity_id == 'ActivitySignIn{}'.format(suffix):
            priority = 2
        elif activity_id.startswith(('AcyivitySummon', 'ActivitySummon')):
            priority = 1
        if priority:
            candidates.append((priority, start, suffix))
    return max(candidates)[2] if candidates else None


def fallback_pickup(db_path=MASTER_DB_PATH):
    if {'ID', 'Type'} - table_columns(db_path, 'Activity'):
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = [
                row[0] for row in conn.execute(
                    "SELECT ID FROM Activity WHERE Type='SideStory'")
                if re.fullmatch(r'BranchH\d+', str(row[0] or ''))
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    branch = max(rows, key=lambda value: intv(value.replace('BranchH', '')),
                 default=None)
    return branch.replace('Branch', '') if branch else None


def activity_scene_ids(pickup, db_path=MASTER_DB_PATH):
    fallback = ['B{}_1_{}'.format(pickup, index) for index in range(1, 15)]
    if {'ID', 'Chapter'} - table_columns(db_path, 'Scene'):
        return fallback
    chapter = 'Branch{}'.format(pickup)
    prefix = 'B{}_1_'.format(pickup)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = [
                str(row[0]) for row in conn.execute(
                    'SELECT ID FROM Scene WHERE Chapter=?', (chapter,))
                if re.fullmatch(r'{}\d+'.format(re.escape(prefix)),
                                str(row[0] or ''))
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        return fallback
    rows.sort(key=lambda scene_id: intv(scene_id.removeprefix(prefix)))
    return rows or fallback


def parse_team(raw):
    members = []
    for item in re.findall(r'\{[^{}]*M:"[^"]+"[^{}]*\}', raw or ''):
        match = re.search(
            r'M:"(?P<sid>[^"]+)"[^}]*?Pos:(?P<pos>\d+)'
            r'[^}]*?LV:(?P<lv>\d+)',
            item)
        if not match:
            continue
        artifact = re.search(r'ArtifactID:"(?P<id>[^"]+)"', item)
        artifact_lv = re.search(r'ArtifactLV:(?P<lv>\d+)', item)
        members.append({
            'sid': match.group('sid'),
            'pos': intv(match.group('pos')),
            'lv': intv(match.group('lv'), 60),
            'artifact_id': artifact.group('id') if artifact else '',
            'artifact_lv': intv(
                artifact_lv.group('lv') if artifact_lv else None, 1),
        })
    return members


def role_skill_ids(role_static_id, conn=None):
    if conn is None:
        return []
    prefix = str(role_static_id or '').removeprefix('PVP')
    try:
        return [
            str(row[0]) for row in conn.execute(
                'SELECT ID FROM Skill WHERE ID LIKE ? ORDER BY ID',
                ('{}S%'.format(prefix),),
            )
        ]
    except sqlite3.Error:
        return []


def npc_role(static_id, lv=60, artifact_id='', artifact_lv=1,
             skill_ids=None):
    role_id = str(uuid.uuid4())
    role_sid = str(static_id or '')
    skill_ids = skill_ids or []
    role = {
        '_id': role_id,
        'StaticID': role_sid,
        'Exp': 0,
        'LV': intv(lv, 60),
        'AwakenLV': 0,
        'AwakenValue': 0,
        'Star': 6,
        'ImprintLV': 0,
        'Locks': [],
        'IsLock': 0,
        'IsFavorite': 0,
        'IsSelfImprintOpen': 0,
        'IsDispatched': 0,
        'IsSelfImprint': 0,
        'Skills': {'Skills': [
            {'Level': 1, 'StaticID': skill_id}
            for skill_id in skill_ids
        ]},
    }
    if artifact_id:
        role['ArtifactData'] = {
            '_id': '',
            'StaticID': artifact_id,
            'Exp': 0,
            'LV': intv(artifact_lv, 1),
            'Enhance': 0,
            'IsLock': 0,
            'IsNew': 1,
        }
    return role


def npc_camp(scene_id, db_path=MASTER_DB_PATH):
    if {'ID', 'WaveInfoJsonString'} - table_columns(db_path, 'Scene'):
        return None
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                'SELECT WaveInfoJsonString FROM Scene WHERE ID=?',
                (scene_id,),
            ).fetchone()
            if not row:
                return None
            members = parse_team(row[0])
            if not members:
                return None
            role_map = {
                str(member['pos']): npc_role(
                    member['sid'],
                    member['lv'],
                    member.get('artifact_id') or '',
                    member.get('artifact_lv') or 1,
                    role_skill_ids(member['sid'], conn),
                )
                for member in members
            }
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return {
        'Name': '方舟α维安小队',
        'PositionRoleMap': role_map,
    }


def activity_npc_maps(pickup, db_path=MASTER_DB_PATH):
    if {'ID', 'Chapter', 'MyCampTeam'} - table_columns(db_path, 'Scene'):
        return {1: {'0': {'StaticID': 'AcStory{}'.format(pickup), 'LV': 60}}}
    chapter = 'Branch{}'.format(pickup)
    prefix = 'B{}_1_'.format(pickup)
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = [
                dict(ID=row[0], MyCampTeam=row[1])
                for row in conn.execute(
                    'SELECT ID, MyCampTeam FROM Scene WHERE Chapter=?',
                    (chapter,))
                if row[1]
            ]
        finally:
            conn.close()
    except sqlite3.Error:
        return {1: {'0': {'StaticID': 'AcStory{}'.format(pickup), 'LV': 60}}}
    rows.sort(key=lambda row: intv(str(row['ID']).removeprefix(prefix)))
    maps = {}
    for row in rows:
        members = parse_team(row['MyCampTeam'])
        if not members:
            continue
        preferred = [m for m in members if m['sid'] == 'AcStory{}'.format(
            pickup)]
        source = preferred or members[:1]
        index = intv(str(row['ID']).rsplit('_', 1)[-1]) - 1
        maps[index] = {
            str(pos): {'StaticID': item['sid'], 'LV': item['lv']}
            for pos, item in enumerate(source)
        }
    return maps or {1: {'0': {'StaticID': 'AcStory{}'.format(pickup),
                              'LV': 60}}}


def current_event(login_data):
    pickup = pickup_from_login(login_data) or fallback_pickup()
    if not pickup:
        return None
    return {
        'pickup': pickup,
        'scene_ids': activity_scene_ids(pickup),
        'npc_maps': activity_npc_maps(pickup),
    }


def support_items(login_data):
    counts = {item_id: 0 for item_id in SUPPORT_ITEM_IDS}
    items = get_nested(login_data, 'ItemContainer', 'Items') or []
    counts.update({
        item['StaticID']: intv(item.get('Count'))
        for item in items
        if item.get('StaticID') in counts
    })
    counts['CUID'] = get_nested(login_data, 'Info', 'CUID')
    return counts


def has_guild(login_data):
    return bool(oid(get_nested(login_data, 'PlayerGuildInfo', 'GID')).strip())


def run_guild_support(client, login_data, report):
    if not has_guild(login_data):
        report.skip('佣兵团支援')
        return
    sups = support_items(login_data)
    if not sups.get('CUID'):
        report.skip('佣兵团支援')
        return
    data = safe_call(
        client, report, '佣兵团支援', 'GuildHandler.QueryFullGuildData')
    if not isinstance(data, dict):
        return
    aid_items = get_nested(data, 'GuildData', 'GuildAidItemInfoList') or []
    for item in aid_items:
        item_id = item.get('ItemID')
        if item.get('NowCount', 0) >= 8:
            report.skip('佣兵团支援')
            continue
        if sups.get(item_id, 0) < 2:
            report.skip('佣兵团支援')
            continue
        if get_nested(item, 'Requester', 'CUID') == sups['CUID']:
            report.skip('佣兵团支援')
            continue
        if sups['CUID'] in (item.get('SupporterList') or []):
            report.skip('佣兵团支援')
            continue
        result = safe_call(
            client,
            report,
            '佣兵团支援',
            'GuildHandler.SupportGuildAid',
            {'GuildAidItemInfoID': oid(item.get('_id'))},
        )
        if result is not None:
            sups[item_id] -= 2
    owned = {key: value for key, value in sups.items() if key != 'CUID'}
    if owned:
        safe_call(
            client,
            report,
            '佣兵团请求',
            'GuildHandler.RequestGuildAid',
            {'ItemID': min(owned, key=owned.get)},
        )


def store_record_map(login_data):
    result = {}
    for record in get_nested(login_data, 'StoreRecordContainer', 'Records') or []:
        static_id = record.get('StaticID')
        if isinstance(static_id, str):
            result[static_id] = record
    return result


def store_bought_today(records, static_id):
    record = records.get(static_id) or {}
    return same_local_day(date_ms(record.get('LastBuyTime')))


def claim_daily_free_summon(client, records, report):
    record = records.get(DAILY_FREE_SUMMON_ID)
    if not record:
        report.skip('每日免费召唤')
        return
    buy_count = intv(record.get('BuyCount'))
    result = safe_call(
        client,
        report,
        '每日免费召唤',
        'StoreHandler.BuyCommodity',
        {
            'Record': {
                '_id': oid(record.get('_id')),
                'StaticID': DAILY_FREE_SUMMON_ID,
            },
            'Count': 1,
            'SelcetCostItemID': '',
        },
        skip_if=buy_count >= 1,
        report_failure=True,
    )
    if result is not None:
        record['BuyCount'] = buy_count + 1


def run_basic_daily(client, login_data, event, report):
    in_guild = has_guild(login_data)
    if in_guild:
        run_guild_support(client, login_data, report)
    else:
        report.skip('佣兵团支援')
    reactor = login_data.get('ArkReactorData') or {}
    lab = login_data.get('ArkStarForceLabData') or {}
    guild = login_data.get('PlayerGuildInfo') or {}
    records = store_record_map(login_data)
    month = login_data.get('MonthSignInData') or {}
    timing = login_data.get('TimingMailData') or {}
    activity_id = 'ActivitySignIn{}'.format(event['pickup']) if event else ''
    week_rows = get_nested(
        login_data, 'WeekSignInDataContainer', 'WeekSignInDataList') or []
    week_row = next(
        (row for row in week_rows if row.get('ActivityID') == activity_id),
        {},
    )

    safe_call(
        client,
        report,
        '日常领取',
        'ArkReactorHandler.RewardArkReactor',
        skip_if=date_ms(reactor.get('NextCanReceiveTime')) > now_ms(),
    )
    for route, key in LAB_REWARD_ROUTES:
        safe_call(
            client,
            report,
            '星源实验室',
            route,
            skip_if=date_ms(lab.get(key)) > now_ms(),
        )
    if in_guild:
        safe_call(
            client,
            report,
            '佣兵团签到',
            'GuildHandler.GuildMemberCheckIn',
            skip_if=same_local_day(date_ms(guild.get('LastCheckInTime'))),
            report_failure=True,
        )
        safe_call(
            client,
            report,
            '佣兵团捐献',
            'GuildHandler.DonateCourage',
            {'ItemID': '28', 'Count': 3},
            skip_if=intv(guild.get('DayDonateCourageCount')) >= 3,
            report_failure=True,
        )
        safe_call(
            client,
            report,
            '佣兵团捐献',
            'GuildHandler.DonateGold',
            {'ItemID': '1', 'Count': 10},
            skip_if=intv(guild.get('DayDonateGoldCount')) >= 10,
            report_failure=True,
        )
        safe_call(
            client,
            report,
            '佣兵团签到',
            'GuildHandler.GuildMemberDayCheckReward',
            skip_if=not guild.get('CanLastDayCheckReward'),
            report_failure=True,
        )
    else:
        report.skip('佣兵团签到')
        report.skip('佣兵团捐献')
    safe_call(
        client,
        report,
        '签到',
        'MonthSignInHandler.SignIn',
        skip_if=same_local_day(date_ms(month.get('LastSignInTime'))),
    )
    if activity_id:
        safe_call(
            client,
            report,
            '签到',
            'WeekSignInHandler.SignIn',
            {'ActivityID': activity_id},
            skip_if=same_local_day(date_ms(week_row.get('LastSignInTime'))),
        )
    safe_call(client, report, '月卡礼包', 'ServerStatusHandler.Query')
    claim_daily_free_summon(client, records, report)
    for record, count in DAILY_STORE_PURCHASES:
        static_id = record['StaticID']
        safe_call(
            client,
            report,
            '商店日常',
            'StoreHandler.BuyCommodity',
            {'Record': dict(record), 'Count': count},
            skip_if=store_bought_today(records, static_id),
        )
    abyss_scene = highest_passed_scene(login_data, r'Abyss_(\d+)')[1]
    safe_call(
        client,
        report,
        '深渊净化',
        'SceneHandler.PurityScene',
        {'StaticID': abyss_scene or 'Abyss_80'},
    )
    safe_call(client, report, '友情点', 'SupportFriendHandler.GetReward')


def iter_finished_unrewarded_quests(node):
    if isinstance(node, dict):
        if node.get('IsFinish') is True and node.get('IsRewarded') is False:
            quest_id = (
                node.get('StaticID')
                or node.get('ID')
                or node.get('QuestStaticID')
            )
            if quest_id:
                yield quest_id
        for value in node.values():
            yield from iter_finished_unrewarded_quests(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_finished_unrewarded_quests(item)


def unique_in_order(values):
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def chunks(values, size):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def claim_quest_rewards(client, login_data, event, report):
    quest_ids = unique_in_order(iter_finished_unrewarded_quests(login_data))
    for batch in chunks(quest_ids, QUEST_BATCH_SIZE):
        safe_call(
            client,
            report,
            '任务奖励',
            'QuestHandler.RewardQuest',
            {'RewardQuestInfos': [
                {'ID': quest_id, 'Index': 0}
                for quest_id in batch
            ]},
        )

    reward_groups = [
        [{'ID': '{}{}'.format(prefix, value), 'Index': 0}
         for value in values]
        for prefix, values in SCORE_REWARD_IDS
    ]
    if event:
        pickup = event['pickup']
        reward_groups.extend((
            [{'ID': 'Branch{}'.format(pickup), 'Index': 0}]
            + [{'ID': 'Branch{}_{}'.format(pickup, i + 1), 'Index': 0}
               for i in range(BRANCH_REWARD_COUNT)],
            [{'ID': 'Branch{}_Achievement_{}'.format(pickup, i + 1),
              'Index': 0} for i in range(BRANCH_ACHIEVEMENT_COUNT)],
        ))
    for rewards in reward_groups:
        safe_call(
            client,
            report,
            '任务奖励',
            'QuestHandler.RewardQuest',
            {'RewardQuestInfos': rewards},
        )


def battle_pass_id(login_data):
    rows = get_nested(
        login_data, 'BattlePassDataContainer', 'BattlePassDataList') or []
    return rows[0].get('ActivityID') if rows else None


def claim_battle_pass(client, login_data, report):
    activity_id = battle_pass_id(login_data)
    if not activity_id:
        report.skip('通行证')
        return
    safe_call(
        client,
        report,
        '通行证',
        'BattlePassHandler.GetAllNowRankReward',
        {'ActivityID': activity_id},
    )


def npc_next_times(login_data):
    result = {}
    for item in get_nested(login_data, 'PVPData', 'NPCPVPInfoList') or []:
        npc_id = item.get('NPCID')
        if npc_id is None:
            continue
        result[str(npc_id)] = max(
            date_ms(item.get('NextTime')),
            result.get(str(npc_id), 0),
        )
    return result


def npc_battle_end_data(scene_id, team, enemy_camp):
    enemy_roles = (enemy_camp or {}).get('PositionRoleMap') or {}
    return {
        'StartBattleInfo': {
            'SceneData': {
                'StaticID': 'PVP',
                'Stars': [0, 0, 0],
                'PassCount': 0,
            },
            'CampData1': team,
            'CampData2': enemy_camp,
            'IsRestart': 0,
            'Round': 0,
            'GM_Wave': 0,
            'IsRepeatAuto': 0,
            'BattleCountDown': -1,
            'IsNPCPVP': 1,
        },
        'Camp2DeadList': [
            oid(role.get('_id')) for _, role in sorted(enemy_roles.items())
        ],
        'Result': 'Win',
        'TurnRole': 0,
        'FinishWave': 0,
    }


def run_npc_and_dispatch(client, login_data, team, report):
    if not team:
        report.skip('NPC')
    else:
        for npc_id, next_time in npc_next_times(login_data).items():
            if next_time > now_ms():
                report.skip('NPC')
                continue
            scene_id = 'HellNPC_{}'.format(npc_id)
            ticket = safe_call(
                client,
                report,
                'NPC',
                'PVPHandler.PVPCheckTicket',
                {'NPCSceneID': scene_id, 'IsRevenge': 0},
            )
            log_id = (ticket or {}).get('LogID')
            enemy_camp = npc_camp(scene_id)
            if not log_id or not enemy_camp:
                report.skip('NPC')
                continue
            safe_call(
                client,
                report,
                'NPC',
                'PVPHandler.NPCPVPBattleEnd',
                {
                    'NPCSceneID': scene_id,
                    'IsRevenge': 0,
                    'EnemyLogID': log_id,
                    'EndData': npc_battle_end_data(
                        scene_id, team, enemy_camp),
                },
            )

    quests = get_nested(login_data, 'ArkHighCommandData',
                        'DispatchedQuests') or []
    for quest in quests:
        static_id = quest.get('StaticID')
        if not static_id or date_ms(quest.get('FinishTime')) > now_ms():
            report.skip('派遣')
            continue
        reward = safe_call(
            client,
            report,
            '派遣',
            'ArkHighCommandHandler.RewardQuest',
            {'QuestStaticID': static_id},
        )
        if reward is None:
            continue
        heroes = (
            get_nested(reward, 'FinishedQuest', 'DispatchedHeroIDs')
            or quest.get('DispatchedHeroIDs')
            or []
        )
        safe_call(
            client,
            report,
            '派遣',
            'ArkHighCommandHandler.DispatchQuest',
            {'Quest': {'StaticID': static_id, 'DispatchedHeroIDs': heroes}},
        )

    timing = login_data.get('TimingMailData') or {}
    safe_call(
        client,
        report,
        '饭点体力',
        'TimingMealHandler.SentMeal',
        skip_if=same_local_day(date_ms(timing.get('LastSentMailTime'))),
    )


def find_support_by_cuid(source, cuid):
    target = intv(cuid)
    for item in walk(source):
        player_cuid = intv(get_nested(
            item, 'PlayerRoleData', 'PlayerInfo', 'CUID'))
        if player_cuid == target and 'PlayerRoleData' in item:
            return copy.deepcopy(item)
    return None


def support_placeholder(cuid):
    return {
        'PlayerRoleData': {
            'PlayerInfo': {'CUID': intv(cuid)},
            'RoleData': {'StaticID': 'H001'},
        },
    }


def activity_support(client, report):
    support_cuid = intv(client.config.get('ActivitySupportCUID'))
    if not support_cuid:
        report.skip('活动借人')
        return None
    data = safe_call(
        client,
        report,
        '活动借人',
        'SupportFriendHandler.QueryBattleSupportDataList',
    )
    return find_support_by_cuid(data, support_cuid) \
        or support_placeholder(support_cuid)


def finish_scene(client, report, section, scene_id, team, support=None,
                 report_failure=False):
    start_info = {
        'SceneData': {'StaticID': scene_id},
        'CampData1': team,
    }
    if support:
        start_info['Support'] = support
    return safe_call(
        client,
        report,
        section,
        'SceneHandler.FinishScene',
        {
            'BattleEndData': {
                'StartBattleInfo': start_info,
                'Result': 'Win',
            },
        },
        report_failure=report_failure,
    )


def finish_activity_opening(client, event, default_team, report, support=None):
    scene_ids = event['scene_ids'][:12]
    last_scene_id = None
    for index, scene_id in enumerate(scene_ids):
        team = default_team
        if index in event['npc_maps']:
            team = {'PositionRoleMap': event['npc_maps'][index]}
        data = finish_scene(
            client,
            report,
            '活动开图',
            scene_id,
            team,
            support,
            report_failure=index == 0,
        )
        if data is None:
            if index == 0:
                report.warn('活动开图首战失败，活动讨伐未继续')
            return last_scene_id
        last_scene_id = scene_id
    return last_scene_id


def urgent_scene_ids(source):
    scene_ids = []
    for container in (
            get_nested(source, 'UrgentMissionContainer'),
            get_nested(source, 'AccountSaveData', 'UrgentMissionContainer'),
    ):
        for mission in get_nested(container or {}, 'Missions') or []:
            scene_id = str(mission.get('SceneID') or '').strip()
            if scene_id and scene_id not in scene_ids:
                scene_ids.append(scene_id)
    return scene_ids


def run_urgent_missions(client, source, team, report, support=None, limit=20):
    if not team:
        report.skip('紧急任务')
        return
    pending = urgent_scene_ids(source)
    finished = set()
    while pending and len(finished) < limit:
        scene_id = pending.pop(0)
        if scene_id in finished:
            continue
        finished.add(scene_id)
        data = finish_scene(client, report, '紧急任务', scene_id, team,
                            support)
        for new_scene_id in urgent_scene_ids(data):
            if new_scene_id not in finished and new_scene_id not in pending:
                pending.append(new_scene_id)


def run_activity(client, login_data, event, team, report):
    if not event:
        report.warn('活动讨伐未执行：未识别当前活动')
        return
    if not team:
        report.warn('活动讨伐未执行：登录数据里没有可用队伍')
        return
    pickup = event['pickup']
    _, scene_id = highest_passed_scene(
        login_data, r'B{}_1_(\d+)'.format(re.escape(pickup)))
    support = activity_support(client, report)
    if not scene_id:
        scene_id = finish_activity_opening(
            client, event, team, report, support)
    if not scene_id:
        report.warn('活动讨伐未执行：没有可用活动关卡')
        return
    run_urgent_missions(client, login_data, team, report, support)
    index = 0
    while True:
        data = finish_scene(
            client,
            report,
            '活动讨伐',
            scene_id,
            team,
            support,
            report_failure=index == 0,
        )
        if data is None:
            if index == 0:
                report.warn('活动讨伐首战失败，讨伐未继续')
            return
        run_urgent_missions(client, data, team, report, support)
        index += 1


def run_hunts(client, login_data, team, report):
    if not team:
        report.warn('讨伐未执行：登录数据里没有可用队伍')
        return
    counts = hunt_run_counts(client.config)
    elements = hunt_rotation(counts)
    if not elements:
        report.warn('讨伐未执行：DailyHuntRuns 全部为 0')
        return
    run_urgent_missions(client, login_data, team, report)
    index = 0
    while True:
        element = elements[index % len(elements)]
        _, scene_id = highest_passed_scene(
            login_data, r'Hunt{}_(\d+)'.format(element))
        scene_id = scene_id or 'Hunt{}_11'.format(element)
        data = finish_scene(
            client,
            report,
            '{}讨伐'.format(HUNT_NAMES[element]),
            scene_id,
            team,
            report_failure=index == 0,
        )
        if data is None:
            if index == 0:
                report.warn('{}讨伐首战失败，讨伐未继续'.format(
                    HUNT_NAMES[element]))
            return
        run_urgent_missions(client, data, team, report)
        index += 1


def secret_records(login_data):
    return [
        record for record in get_nested(
            login_data, 'StoreRecordContainer', 'Records') or []
        if record.get('Store') == 'SecretShop'
    ]


def desired_secret_item(record):
    items = get_nested(record, 'DropResult', 'Items') or []
    if not items:
        return False
    item = items[0].get('Item')
    if isinstance(item, dict):
        return str(item.get('StaticID') or '') in SECRET_SHOP_ITEMS
    return False


def buy_secret_records(client, records, report):
    bought = 0
    for record in records:
        if not desired_secret_item(record):
            report.skip('神秘商店')
            continue
        result = safe_call(
            client,
            report,
            '神秘商店',
            'StoreHandler.BuyCommodity',
            {'Record': {
                '_id': oid(record.get('_id')),
                'StaticID': record.get('StaticID'),
            }},
        )
        if result is not None:
            bought += 1
    return bought


def run_secret_shop(client, login_data, report):
    records = secret_records(login_data)
    account_save = login_data.setdefault('AccountSaveData', {})
    refreshes = max(0, intv(account_save.get(
        'RandomStoreDayRefreshCount')))
    refresh_limit = max(0, intv(
        client.config.get('RandomStoreDayRefreshLimit'),
        DEFAULT_SECRET_SHOP_REFRESH_LIMIT,
    ))
    while True:
        if refreshes >= refresh_limit:
            report.skip('神秘商店刷新')
            return
        buy_secret_records(client, records, report)
        data = safe_call(
            client,
            report,
            '神秘商店刷新',
            'StoreHandler.ResetRandomStore',
            {'StoreID': 'SecretShop', 'IsUseGold': 1},
            report_failure=refreshes == 0,
        )
        if not isinstance(data, dict):
            if refreshes == 0:
                report.warn('神秘商店一次都没刷新成功')
            return
        refreshes += 1
        account_save['RandomStoreDayRefreshCount'] = refreshes
        records = data.get('Records') or []


def run_daily_cleanup(client, login_data):
    if not isinstance(login_data, dict):
        raise GameRequestError('登录响应缺失，无法执行日常')
    report = DailyReport()
    event = current_event(login_data)
    team = first_team(login_data)

    run_basic_daily(client, login_data, event, report)
    run_npc_and_dispatch(client, login_data, team, report)
    if has_active_status(login_data, HUNT_STATUS_ID):
        run_hunts(client, login_data, team, report)
    else:
        run_activity(client, login_data, event, team, report)
    run_secret_shop(client, login_data, report)
    claim_battle_pass(client, login_data, report)
    claim_quest_rewards(client, login_data, event, report)
    return {
        'summary': '日常正常' if not report.warnings else None,
        'warnings': report.warnings,
    }
