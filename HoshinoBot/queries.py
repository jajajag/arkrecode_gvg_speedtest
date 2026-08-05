import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from .api import GameRequestError
from .database import (
    ALIAS_PATH,
    DATA_DB_PATH,
    MASTER_DB_PATH,
    connect_data,
    init_database,
    meta_get,
    now_ms,
)


RECENT_DAYS = 30
MILLIS_PER_DAY = 24 * 60 * 60 * 1000


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
        FROM gvg_defences AS d
        JOIN gvg_members AS m ON m.cuid = d.cuid
        ORDER BY d.sort_order, m.cuid
        ''').fetchall()


def _ambiguous_players(rows):
    lines = ['发现同名玩家：']
    lines.extend('{}. {} {}'.format(index, row['name'], row['cuid'])
                 for index, row in enumerate(rows, 1))
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
        if re.fullmatch(r'\d{9}', query):
            by_cuid = [row for row in rows if str(row['cuid']) == query]
            if by_cuid:
                return by_cuid[0], None
            return None, '没有找到玩家ID“{}”。'.format(query)

        query_folded = _fold(query)
        exact_name = [row for row in rows
                      if _fold(row['name']) == query_folded]
        if len(exact_name) == 1:
            return exact_name[0], None
        if len(exact_name) > 1:
            return None, _ambiguous_players(exact_name)

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
        lines.append('- {}，胜率{}%，掉人率{}%'.format(
            names, win_pct, drop_pct))
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
            limit=5,
        )
        has_matchup_solutions = has_matchup_solutions or bool(ranked)
        if ranked:
            sections.append('\n'.join(_solution_lines(
                '{}解法：'.format(atk_guild), ranked, roles)))
    if not has_matchup_solutions:
        ranked = _rank_solutions(rounds, target, limit=5)
        sections.append(
            '近30天内无针对该防守的交手记录，以下为整体解法。')
        sections.append('\n'.join(_solution_lines(
            '整体解法：', ranked, roles)))
    return defense + '\n' + '\n'.join(sections)


def format_defenses(db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        context = _guild_context(conn)
        members = _current_members(conn)
    finally:
        conn.close()
    if not members:
        return '暂无{}防守数据，请先更新。'.format(context['enemy']['name'])
    roles = _role_name_map()
    date = members[0]['snapshot_date']
    title = '{} {}防守'.format(date, context['enemy']['name'])
    blocks = []
    for index, member in enumerate(members, 1):
        avatar = roles.get(member['avatar_role_id'], member['avatar_role_id'])
        upper = (member['upper_1_role_id'], member['upper_2_role_id'],
                 member['upper_3_role_id'])
        lower = (member['lower_1_role_id'], member['lower_2_role_id'],
                 member['lower_3_role_id'])
        first = '+'.join(roles.get(role, role) for role in upper if role) or '-'
        second = '+'.join(roles.get(role, role) for role in lower if role) or '-'
        blocks.append('\n'.join((
            '{:02d}. {}（{}）'.format(index, member['name'], avatar),
            '上半：{}'.format(first),
            '下半：{}'.format(second),
        )))
    return title + '\n' + '\n'.join(blocks)


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
    match = re.fullmatch(r'(\d{1,4})(?:-(\d{1,4}))?', value.strip())
    if not match:
        return None
    lower = int(match.group(1))
    upper = int(match.group(2)) if match.group(2) else None
    if lower <= 0 or (upper is not None and upper < lower):
        return None
    return str(lower) if upper is None else '{}-{}'.format(lower, upper)


def set_max_speed(player_query, speed, db_path=DATA_DB_PATH):
    init_database(db_path)
    normalized = _normalize_speed(speed)
    if normalized is None:
        return '一速格式错误，请输入 227 或 265-270。'
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


def _resolve_player_prefix(text, conn):
    tokens = text.strip().split()
    if len(tokens) < 2:
        return None, None, '格式：团战 信息 玩家名或UID 信息内容'
    errors = []
    for split_at in range(len(tokens) - 1, 0, -1):
        query = ' '.join(tokens[:split_at])
        player, error = resolve_player(query, conn)
        if player is not None:
            return player, ' '.join(tokens[split_at:]), None
        errors.append(error)
    return None, None, errors[-1] if errors else '没有找到玩家。'


def set_member_info(text, db_path=DATA_DB_PATH):
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        player, info, error = _resolve_player_prefix(text, conn)
        if error:
            return error
        conn.execute(
            'UPDATE gvg_members SET info=?, updated_at=? '
            'WHERE cuid=?',
            (info, now_ms(), int(player['cuid'])),
        )
        conn.commit()
        return '已保存 {} 的信息。'.format(player['name'])
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
            lines.append(str(player['info']))
        return '\n'.join(lines)
    finally:
        conn.close()
