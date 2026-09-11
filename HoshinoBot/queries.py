import itertools
import json
import threading
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from .api import GameRequestError, oid, team_roles
from .database import (
    ALIAS_PATH,
    DATA_DB_PATH,
    MASTER_DB_PATH,
    connect_data,
    init_database,
    meta_get,
    now_ms,
    today,
)


RECENT_DAYS = 30
MILLIS_PER_DAY = 24 * 60 * 60 * 1000
INFO_FORMAT = 'gvg_info_v1'
MAX_WRONGBOOK_MATCHES = 10
STATS_LOCK = threading.Lock()
SPEED_PARTS = ("Weapon", "Head", "Body", "Necklace", "Ring")
SPEED_SET_BASE = 189
BROKEN_SET_BASE = 169

def encode_info_segments(segments):
    normalized = []
    for segment in segments:
        kind = str(segment.get('type') or '')
        if kind == 'text':
            content = str(segment.get('content') or '')
            if content:
                normalized.append({'type': 'text', 'content': content})
        elif kind == 'image':
            image_path = str(segment.get('path') or '').replace('\\', '/')
            parts = PurePosixPath(image_path).parts
            if not image_path or not parts or parts[0] != 'images' \
                    or '..' in parts or PurePosixPath(image_path).is_absolute():
                raise ValueError('图片路径必须位于 images 目录内')
            normalized.append({'type': 'image', 'path': image_path})
        else:
            raise ValueError('不支持的信息段类型：{}'.format(kind))
    if not normalized:
        raise ValueError('信息内容不能为空')
    return json.dumps(
        {'format': INFO_FORMAT, 'segments': normalized},
        ensure_ascii=False,
        separators=(',', ':'),
    )


def decode_info_segments(raw):
    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise GameRequestError('玩家信息格式无效') from exc
    if not isinstance(data, dict) or data.get('format') != INFO_FORMAT \
            or not isinstance(data.get('segments'), list):
        raise GameRequestError('玩家信息格式无效')
    encoded = encode_info_segments(data['segments'])
    return json.loads(encoded)['segments']


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
            ''').fetchall()
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


def _gvg_match_date(start_ts):
    """Return the UTC start date of the GVG match containing a log.

    Matches run from 00:00 UTC Monday/Wednesday/Friday until 00:00 UTC the
    following day. The weekday check excludes timestamps between matches.
    """
    match_day = datetime.fromtimestamp(int(start_ts) / 1000, timezone.utc)
    if match_day.weekday() not in (0, 2, 4):
        return None
    return match_day.strftime('%Y-%m-%d')


def _resolve_attack_guild(conn, query):
    query = str(query).strip()
    if not query:
        return None, '格式：团战 错题本 团名 [场数]'
    names = [str(row['atk_guild']) for row in conn.execute(
        '''
        SELECT DISTINCT atk_guild
        FROM gvg_rounds
        WHERE TRIM(COALESCE(atk_guild, '')) != ''
        ORDER BY atk_guild
        ''')]
    folded = _fold(query)
    exact = [name for name in names if _fold(name) == folded]
    if exact:
        return exact[0], None
    partial = [name for name in names if folded in _fold(name)]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, '团名“{}”匹配多个佣兵团：{}。请使用完整团名。'.format(
            query, '、'.join(partial[:12]))
    return None, '没有找到“{}”的进攻记录。'.format(query)


def _recent_gvg_matches(conn, atk_guild, limit):
    matches = []
    seen = set()
    for row in conn.execute(
            'SELECT start_ts FROM gvg_rounds '
            'WHERE atk_guild = ? ORDER BY start_ts DESC',
            (atk_guild,)):
        match_date = _gvg_match_date(row['start_ts'])
        if match_date is None or match_date in seen:
            continue
        seen.add(match_date)
        matches.append(match_date)
        if len(matches) >= limit:
            break
    return matches


def _wrongbook_failures(conn, atk_guild, match_dates):
    selected_dates = set(match_dates)
    units = defaultdict(lambda: {'atk': [], 'def': []})
    for row in conn.execute(
        '''
        SELECT u.*
        FROM gvg_units AS u
        JOIN gvg_rounds AS r
          ON r.battle_id=u.battle_id AND r.round_idx=u.round_idx
        WHERE r.atk_guild = ? AND r.win = 0
        ORDER BY u.battle_id, u.round_idx, u.side, u.pos
        ''',
        (atk_guild,),
    ):
        units[(row['battle_id'], int(row['round_idx']))][row['side']].append(
            row)

    grouped = {date: defaultdict(lambda: defaultdict(set))
               for date in match_dates}
    for row in conn.execute(
        'SELECT * FROM gvg_rounds '
        'WHERE atk_guild = ? AND win = 0 '
        'ORDER BY start_ts, battle_id, round_idx',
        (atk_guild,),
    ):
        date = _gvg_match_date(row['start_ts'])
        if date not in selected_dates:
            continue
        teams = units[(row['battle_id'], int(row['round_idx']))]
        if len(teams['atk']) != 3 or len(teams['def']) != 3:
            continue
        atk_team = tuple(sorted(unit['role_id'] for unit in teams['atk']))
        def_team = tuple(sorted(unit['role_id'] for unit in teams['def']))
        attacker = str(row['atk_name'] or row['atk_cuid'] or '未知团员')
        grouped[date][def_team][atk_team].add(attacker)
    return grouped


def _wrongbook_section(date, atk_guild, failures, roles):
    lines = ['{} {}错题本'.format(date, atk_guild)]
    if not failures:
        lines.append('- 暂无进攻失败记录')
        return '\n'.join(lines)

    defenses = sorted(
        failures.items(),
        key=lambda item: (
            -sum(len(names) for names in item[1].values()),
            item[0],
        ),
    )
    for index, (def_team, attacks) in enumerate(defenses, 1):
        defense = '+'.join(roles.get(role, role) for role in def_team)
        lines.append('{}. {}：'.format(index, defense))
        ranked_attacks = sorted(
            attacks.items(),
            key=lambda item: (-len(item[1]), item[0]),
        )
        for atk_team, attackers in ranked_attacks:
            attack = '+'.join(roles.get(role, role) for role in atk_team)
            names = '，'.join(sorted(attackers, key=_fold))
            lines.append('- {}（{}）'.format(attack, names))
    return '\n'.join(lines)


def format_wrongbook(guild_query, match_count=1, db_path=DATA_DB_PATH):
    try:
        match_count = int(match_count)
    except (TypeError, ValueError) as exc:
        raise GameRequestError('场数必须是1到{}之间的整数'.format(
            MAX_WRONGBOOK_MATCHES)) from exc
    if not 1 <= match_count <= MAX_WRONGBOOK_MATCHES:
        raise GameRequestError('场数必须是1到{}之间的整数'.format(
            MAX_WRONGBOOK_MATCHES))

    init_database(db_path)
    conn = connect_data(db_path)
    try:
        atk_guild, error = _resolve_attack_guild(conn, guild_query)
        if error:
            return error
        match_dates = _recent_gvg_matches(conn, atk_guild, match_count)
        if not match_dates:
            return '没有找到“{}”的进攻记录。'.format(atk_guild)
        failures = _wrongbook_failures(conn, atk_guild, match_dates)
    finally:
        conn.close()

    roles = _role_name_map()
    return '\n\n'.join(
        _wrongbook_section(date, atk_guild, failures[date], roles)
        for date in match_dates
    )


def _guild_context(conn):
    our_guild = {'name': meta_get(conn, 'our_guild_name') or ''}
    enemy_guild = {'name': meta_get(conn, 'current_enemy_guild_name') or ''}
    if not our_guild['name']:
        raise GameRequestError('暂无我方团信息，请先更新数据')
    if not enemy_guild['name']:
        raise GameRequestError('暂无当前对战敌方团信息，请先更新数据')
    return {'our': our_guild, 'enemy': enemy_guild}


def _current_members(conn):
    return conn.execute(
        '''
        SELECT m.*, d.snapshot_date, d.sort_order,
               d.upper_1_role_id, d.upper_2_role_id, d.upper_3_role_id,
               d.lower_1_role_id, d.lower_2_role_id, d.lower_3_role_id
        FROM gvg_current_members AS d
        JOIN gvg_members AS m ON m.cuid = d.cuid
        ORDER BY d.sort_order, m.cuid
        ''').fetchall()


def _ambiguous_players(rows):
    roles = _role_name_map()
    lines = ['发现同名玩家，请使用UID代替玩家名：']
    for index, row in enumerate(rows, 1):
        avatar_role_id = row['avatar_role_id']
        avatar = roles.get(avatar_role_id, avatar_role_id) or '未知'
        lines.append('{}. {} {} {}头像'.format(
            index, row['name'], row['cuid'], avatar))
    return '\n'.join(lines)


def resolve_player(query, conn=None, current_only=True):
    close_conn = conn is None
    conn = conn or connect_data()
    try:
        if current_only:
            _guild_context(conn)
            rows = _current_members(conn)
        else:
            rows = conn.execute(
                'SELECT * FROM gvg_members ORDER BY name, cuid').fetchall()
        query = str(query).strip()
        query_folded = _fold(query)
        exact_name = [row for row in rows
                      if _fold(row['name']) == query_folded]
        if len(exact_name) == 1:
            return exact_name[0], None
        if len(exact_name) > 1:
            return None, _ambiguous_players(exact_name)

        by_cuid = [row for row in rows if str(row['cuid']) == query]
        if by_cuid:
            return by_cuid[0], None

        partial = [row for row in rows
                   if query_folded in _fold(row['name'])]
        if len(partial) == 1:
            return partial[0], None
        if len(partial) > 1:
            return None, _ambiguous_players(partial)
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


def _rank_solutions(rounds, target, atk_guild=None, def_guild=None,
                    limit=None):
    grouped = defaultdict(list)
    for item in rounds:
        row = item['row']
        if tuple(sorted(item['def'])) != target:
            continue
        if atk_guild is not None and row['atk_guild'] != atk_guild:
            continue
        if def_guild is not None and row['def_guild'] != def_guild:
            continue
        grouped[tuple(sorted(item['atk']))].append(item)

    ranked = []
    for team, items in grouped.items():
        total = len(items)
        wins = sum(int(item['row']['win']) for item in items)
        if wins == 0:
            continue
        drops = sum(int(item['atk_dead']) for item in items)
        ranked.append((wins / total, total, drops / total, team))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    return ranked[:limit] if limit is not None else ranked


def _solution_lines(title, ranked, roles):
    lines = [title]
    if not ranked:
        lines.append('- 暂无记录')
    for rate, total, drop_rate, team in ranked:
        names = '+'.join(roles.get(role, role) for role in team)
        win_pct = int(rate * 100 + 0.5)
        drop_pct = int(drop_rate * 100 + 0.5)
        lines.append('- {}，胜率{}%，场次{}，掉人率{}%'.format(
            names, win_pct, total, drop_pct))
    return lines


def _solution_data(role_ids, db_path):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        context = _guild_context(conn)
        since_ts = now_ms() - RECENT_DAYS * MILLIS_PER_DAY
        rounds = _rounds_since(conn, since_ts)
    finally:
        conn.close()
    target = tuple(sorted(role_ids))
    roles = _role_name_map()
    defense = '防守：{}'.format(
        '+'.join(roles.get(role, role) for role in target))
    return context, rounds, target, roles, defense


def format_solutions(role_ids, db_path=DATA_DB_PATH):
    context, rounds, target, roles, defense = _solution_data(
        role_ids, db_path)
    sections = []
    has_matchup_solutions = False
    matchups = (
        (context['our']['name'], context['enemy']['name']),
        (context['enemy']['name'], context['our']['name']),
    )
    for atk_guild, def_guild in matchups:
        ranked = _rank_solutions(
            rounds, target,
            atk_guild=atk_guild,
            def_guild=def_guild,
        )
        has_matchup_solutions = has_matchup_solutions or bool(ranked)
        if ranked:
            sections.append('\n'.join(_solution_lines(
                '{}解法：'.format(atk_guild), ranked, roles)))
    if not has_matchup_solutions:
        ranked = _rank_solutions(rounds, target, limit=10)
        sections.append(
            '近30天内无针对该防守的交手记录，以下为整体解法。')
        sections.append('\n'.join(_solution_lines(
            '整体解法：', ranked, roles)))
    return defense + '\n' + '\n'.join(sections)


def member_defense_stats(conn, cuid, def_guild_name, atk_guild_name=None):
    since_ts = now_ms() - RECENT_DAYS * MILLIS_PER_DAY
    where = 'start_ts >= ? AND def_cuid = ? AND def_guild = ?'
    params = [since_ts, int(cuid), def_guild_name]
    if atk_guild_name is not None:
        where += ' AND atk_guild = ?'
        params.append(atk_guild_name)
    row = conn.execute(
        ('''
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN win = 0 THEN 1 ELSE 0 END) AS successes
        FROM gvg_rounds
        WHERE {}
        ''').format(where),
        params,
    ).fetchone()
    if not row or not row['total']:
        return 0, 0
    return int(row['successes'] or 0), int(row['total'])


def member_defense_rate(conn, cuid, def_guild_name, atk_guild_name=None,
                        fallback_overall=False):
    successes, total = member_defense_stats(
        conn, cuid, def_guild_name, atk_guild_name)
    overall = False
    if not total and fallback_overall and atk_guild_name is not None:
        successes, total = member_defense_stats(
            conn, cuid, def_guild_name)
        overall = bool(total)
    if not total:
        return '-'
    text = '{:.1f}%'.format(successes / total * 100)
    return text + '（整体）' if overall else text


def format_win_rates(db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        context = _guild_context(conn)
        members = _current_members(conn)
        if not members:
            return '暂无{}防守数据，请先更新。'.format(
                context['enemy']['name'])
        roles = _role_name_map()
        since_ts = now_ms() - RECENT_DAYS * MILLIS_PER_DAY
        has_direct_records = conn.execute(
            'SELECT 1 FROM gvg_rounds '
            'WHERE start_ts >= ? AND atk_guild = ? AND def_guild = ? '
            'LIMIT 1',
            (since_ts, context['our']['name'], context['enemy']['name']),
        ).fetchone() is not None
        atk_guild_name = (context['our']['name']
                          if has_direct_records else None)
        ranked = []
        for member in members:
            successes, total = member_defense_stats(
                conn, member['cuid'], context['enemy']['name'],
                atk_guild_name)
            rate = successes / total * 100 if total else None
            ranked.append((member, rate, total))
        ranked.sort(key=lambda item: (
            item[1] is None,
            -(item[1] or 0),
            -item[2],
            int(item[0]['sort_order']),
        ))
        title = '{} {}防守胜率'.format(
            members[0]['snapshot_date'], context['enemy']['name'])
        lines = []
        if not has_direct_records:
            lines.append('近30天内无交手记录，以下为整体防守胜率。')
        lines.append(title)
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
    match = re.fullmatch(r'(\d{1,4})(?:(-)(\d{1,4})|(\+))?', value.strip())
    if not match:
        return None
    lower = int(match.group(1))
    upper = int(match.group(3)) if match.group(3) else None
    if lower <= 0 or (upper is not None and upper < lower):
        return None
    if upper is not None:
        return '{}-{}'.format(lower, upper)
    return '{}+'.format(lower) if match.group(4) else str(lower)


def set_max_speed(player_query, speed, db_path=DATA_DB_PATH):
    init_database(db_path)
    normalized = _normalize_speed(speed)
    if normalized is None:
        return '一速格式错误，请输入 227、265-270 或 122+。'
    conn = connect_data(db_path)
    try:
        player, error = resolve_player(
            player_query, conn, current_only=False)
        if error:
            return error
        conn.execute(
            'UPDATE gvg_members SET max_speed=?, updated_at=? WHERE cuid=?',
            (normalized, now_ms(), int(player['cuid'])),
        )
        conn.commit()
        return '已更新 {} 的一速：{}'.format(player['name'], normalized)
    finally:
        conn.close()


def resolve_member_info_target(text, has_images=False,
                               db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        matches = list(re.finditer(r'\S+', text))
        max_split = len(matches) if has_images else len(matches) - 1
        if max_split < 1:
            return None, None, '格式：团战 信息 玩家名或UID 信息内容或图片'
        errors = []
        for split_at in range(max_split, 0, -1):
            query_end = matches[split_at - 1].end()
            query = text[:query_end].strip()
            player, error = resolve_player(query, conn)
            if player is not None:
                payload_start = (matches[split_at].start()
                                 if split_at < len(matches) else len(text))
                return dict(player), payload_start, None
            errors.append(error)
        return None, None, errors[-1] if errors else '没有找到玩家。'
    finally:
        conn.close()


def set_member_info(player, segments, db_path=DATA_DB_PATH):
    init_database(db_path)
    encoded = encode_info_segments(segments)
    conn = connect_data(db_path)
    try:
        conn.execute(
            'UPDATE gvg_members '
            'SET info=?, info_date=?, updated_at=? '
            'WHERE cuid=?',
            (encoded, today(), now_ms(), int(player['cuid'])),
        )
        conn.commit()
        return '已保存 {} 的信息。'.format(player['name'])
    finally:
        conn.close()


def format_member_history(player_query, db_path=DATA_DB_PATH, limit=5):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        player, error = resolve_player(
            player_query, conn, current_only=False)
        if error:
            return error
        rows = conn.execute(
            '''
            SELECT player_name, match_date, enemy_guild_name,
                   info, info_date
            FROM gvg_member_info_history
            WHERE cuid = ?
            ORDER BY archived_at DESC, id DESC
            LIMIT ?
            ''',
            (int(player['cuid']), int(limit)),
        ).fetchall()
        if not rows:
            return '{} 暂无历史信息。'.format(player['name'])
        lines = ['{} 的最近{}次历史信息：'.format(
            player['name'], len(rows))]
        for index, row in enumerate(rows, 1):
            date = row['match_date'] or row['info_date'] or '日期未知'
            enemy = row['enemy_guild_name'] or '对手未知'
            segments = decode_info_segments(row['info'])
            content = ''.join(
                segment['content'] for segment in segments
                if segment['type'] == 'text'
            ).strip()
            lines.append('{}. {} 对阵{}\n{}'.format(
                index, date, enemy, content))
        return '\n'.join(lines)
    finally:
        conn.close()


def format_player(player_query, db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        context = _guild_context(conn)
        player, error = resolve_player(player_query, conn)
        if error:
            return error
        roles = _role_name_map()
        avatar = roles.get(player['avatar_role_id'], player['avatar_role_id'])
        lines = ['{}（{}）'.format(player['name'], avatar)]
        if player['max_speed']:
            lines.append('一速：{}'.format(player['max_speed']))
        lines.append('防守胜率：{}'.format(member_defense_rate(
            conn, player['cuid'], context['enemy']['name'],
            context['our']['name'], fallback_overall=True)))
        if player['info']:
            segments = decode_info_segments(player['info'])
            return [
                {'type': 'text', 'content': '\n'.join(lines) + '\n'},
                *segments,
            ]
        return '\n'.join(lines)
    finally:
        conn.close()


def equipment_pool(cards):
    """Keep the latest observation of each item across newest-first snapshots."""
    pool, seen = [], set()
    for card in cards:
        roles = team_roles((card.get('PVPInfo') or {}).get('DefenceTeam') or {})
        roles += [item.get('Role') or {} for item in
                  (card.get('BattleSupportData') or {}).get('RoleDataList') or []]
        for role in roles:
            for part, equip in (role.get('EquipmentMap') or {}).items():
                if part not in SPEED_PARTS:
                    continue
                identity = oid(equip.get('_id')) or json.dumps(equip, sort_keys=True)
                if identity in seen:
                    continue
                seen.add(identity)
                props = (equip.get('SubProps') or {}).get('SourceValues') or []
                speed = next((
                    float(prop.get('Value', prop.get('SValue', 0)) or 0)
                    for prop in props if prop.get('PropertyType') == 'SpeedValue'
                ), 0)
                pool.append(dict(equip_id=identity, equip_type=part,
                                 set_name=equip.get('Set'), speed=speed))
    return pool


def best_speed_combo(equips, used=None):
    """pvp_speed.py convention: five non-shoe pieces, rabbit bases 169/189."""
    used = used or set()
    by_part = {part: [] for part in SPEED_PARTS}
    for equip in equips:
        if equip['equip_id'] not in used and equip['equip_type'] in by_part:
            by_part[equip['equip_type']].append(equip)
    # For each part, only the fastest Speed/non-Speed item can win.
    # Filter after excluding used IDs so second speed still sees the runners-up.
    for part, items in by_part.items():
        fastest = {}
        for item in items:
            is_speed = item['set_name'] == 'Speed'
            if is_speed not in fastest or item['speed'] > fastest[is_speed]['speed']:
                fastest[is_speed] = item
        best_ids = {item['equip_id'] for item in fastest.values()}
        by_part[part] = [item for item in items if item['equip_id'] in best_ids]
    best = None
    for combo in itertools.product(*(by_part[part] for part in SPEED_PARTS)):
        speed_set = sum(item['set_name'] == 'Speed' for item in combo) >= 3
        base = SPEED_SET_BASE if speed_set else BROKEN_SET_BASE
        total = base + sum(item['speed'] for item in combo)
        if best is None or total > best[0]:
            best = total, combo, '速度套' if speed_set else '散件'
    return best


def theoretical_builds(cards):
    equips = equipment_pool(cards)
    first = best_speed_combo(equips)
    used = {item['equip_id'] for item in first[1]} if first else set()
    return first, best_speed_combo(equips, used)


def defence_role_line(role, stats, master):
    equips = (role.get('EquipmentMap') or {}).values()
    counts = Counter(equip.get('Set') for equip in equips)
    details = []
    for set_id, count in counts.items():
        required = int((master.equipment_sets.get(set_id) or {}).get('Count') or 99)
        active = count // required
        if active:
            name = master.equipment_set_name(set_id)
            details.append('{}{}'.format(active if active > 1 else '', name))
    bond = role.get('ArtifactData') or {}
    if bond:
        details.append('{}级{}'.format(
            bond.get('LV', '?'), master.artifact_name(bond.get('StaticID'))))
    return '{}：{}生 {}'.format(
        master.role_name(role['StaticID']), round(stats['HP']), ' / '.join(details)).rstrip()


def format_defence(query, db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        player, error = resolve_player(query, conn)
        if error:
            return error
        rows = conn.execute('SELECT * FROM gvg_snapshots WHERE cuid=? '
                            'ORDER BY snapshot_date DESC', (player['cuid'],)).fetchall()
    finally:
        conn.close()
    snapshots = {}
    for row in rows:
        snapshots.setdefault(row['kind'], row)
    defence = snapshots.get('defence')
    if defence is None:
        return '该玩家暂无团战防守数据，请先更新数据。'
    from ..Frida import helper
    with STATS_LOCK:
        master = helper.load_master_data(MASTER_DB_PATH)
        lines = ['{} ｜ CUID {}'.format(player['name'], player['cuid']),
                 '头像：{} ｜ 防守日期：{}'.format(
                     master.role_name(player['avatar_role_id']), defence['snapshot_date'])]
        teams = json.loads(defence['payload'])['DefenceTeamData']
        for key, label in (('FirstTeam', '上半'), ('SecondTeam', '下半')):
            lines.append('── {} ──'.format(label))
            roles = team_roles(teams.get(key) or {})
            for role, stats in zip(roles, helper.calculate_team_stats(roles)):
                lines.append(defence_role_line(role, stats, master))
        equipment = snapshots.get('equipment')
        if equipment:
            cards = [json.loads(row['payload']) for row in rows if row['kind'] == 'equipment']
            lines.append('── 理论配装（兔子基准）──')
            for label, build in zip(('一速', '二速'), theoretical_builds(cards)):
                if build is None:
                    lines.append('{}：已知装备不足五个部位'.format(label))
                    continue
                total, combo, mode = build
                pieces = ' / '.join('{} {:g}'.format(
                    master.equipment_set_name(item['set_name']), item['speed']) for item in combo)
                lines.append('{}：{:g}（{}）\n{}'.format(label, total, mode, pieces))
            lines.append('装备顺序：武器／头／衣／项链／戒指')
            lines.append('最新采集：{}；汇总历史装备，同件取最新记录；二速排除一速已用装备。'.format(equipment['snapshot_date']))
        else:
            lines.append('暂未采集到竞技场／助战装备。')
        return '\n'.join(lines)
