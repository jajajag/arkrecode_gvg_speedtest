from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import itertools
import json
import re
import sqlite3
from pathlib import Path

from .database import (
    ALIAS_PATH, DATA_DB_PATH, MASTER_DB_PATH, connect_data, init_database,
    meta_get, now_ms,
)

RECENT_DAYS = 30
SOLUTION_LIMIT = 10
MAX_WRONGBOOK_MATCHES = 10
MILLIS_PER_DAY = 86400000
SPEED_PARTS = ("Weapon", "Head", "Body", "Necklace", "Ring")
SPEED_SET_BASE = 189
BROKEN_SET_BASE = 169

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
    """旧版口径：UTC周一、周三、周五，每个日期算一场团战。"""
    match_day = datetime.fromtimestamp(int(start_ts) / 1000, timezone.utc)
    return (match_day.strftime('%Y-%m-%d')
            if match_day.weekday() in (0, 2, 4) else None)


def _resolve_attack_guild(conn, query):
    query = str(query).strip()
    if not query:
        return None, '格式：团战 错题本 团名 [场数]'
    names = [str(row['atk_guild']) for row in conn.execute('''
        SELECT DISTINCT atk_guild FROM gvg_rounds
        WHERE TRIM(COALESCE(atk_guild, '')) != '' ORDER BY atk_guild
    ''')]
    folded = _fold(query)
    exact = [name for name in names if _fold(name) == folded]
    if exact:
        return exact[0], None
    partial = [name for name in names if folded in _fold(name)]
    if len(partial) == 1:
        return partial[0], None
    if partial:
        return None, '团名“{}”匹配多个佣兵团：{}。请使用完整团名。'.format(
            query, '、'.join(partial[:12]))
    return None, '没有找到“{}”的进攻记录。'.format(query)


def _recent_gvg_matches(conn, atk_guild, limit):
    matches = []
    seen = set()
    for row in conn.execute(
            'SELECT start_ts FROM gvg_rounds '
            'WHERE atk_guild=? ORDER BY start_ts DESC', (atk_guild,)):
        date = _gvg_match_date(row['start_ts'])
        if date is None or date in seen:
            continue
        matches.append(date)
        seen.add(date)
        if len(matches) == limit:
            break
    return matches


def _wrongbook_results(conn, atk_guild, match_dates):
    grouped = {date: defaultdict(lambda: {
        False: defaultdict(set), True: defaultdict(set),
    }) for date in match_dates}
    start = datetime.strptime(min(match_dates), '%Y-%m-%d').replace(tzinfo=timezone.utc)
    end = (datetime.strptime(max(match_dates), '%Y-%m-%d').replace(tzinfo=timezone.utc)
           + timedelta(days=1))
    rows = conn.execute('''
        SELECT r.battle_id, r.round_idx, r.start_ts, r.atk_name, r.atk_cuid,
               r.win, u.side, u.role_id
        FROM gvg_rounds AS r
        JOIN gvg_units AS u
          ON r.battle_id=u.battle_id AND r.round_idx=u.round_idx
        WHERE r.atk_guild=? AND r.start_ts>=? AND r.start_ts<?
        ORDER BY r.start_ts, r.battle_id, r.round_idx, u.side, u.role_id
    ''', (atk_guild, int(start.timestamp() * 1000), int(end.timestamp() * 1000)))
    for _, items in itertools.groupby(rows, key=lambda row: (
            row['battle_id'], row['round_idx'])):
        items = list(items)
        battle = items[0]
        date = _gvg_match_date(battle['start_ts'])
        if date not in grouped:
            continue
        teams = {side: tuple(sorted(unit['role_id'] for unit in items
                                    if unit['side'] == side))
                 for side in ('atk', 'def')}
        if any(len(team) != 3 for team in teams.values()):
            continue
        attacker = str(battle['atk_name'] or battle['atk_cuid'] or '未知团员')
        grouped[date][teams['def']][bool(battle['win'])][teams['atk']].add(attacker)
    return grouped


def _wrongbook_section(date, atk_guild, results, roles):
    lines = ['{} {}错题本'.format(date, atk_guild)]
    defenses = [(team, outcomes) for team, outcomes in results.items()
                if outcomes[False]]
    if not defenses:
        return '\n'.join(lines + ['- 暂无进攻失败记录'])
    defenses.sort(key=lambda item: (
        -sum(len(names) for names in item[1][False].values()), item[0]))
    for index, (def_team, outcomes) in enumerate(defenses, 1):
        lines.append('{}. {}：'.format(
            index, '+'.join(roles.get(role, role) for role in def_team)))
        for win, label in ((False, '失败'), (True, '成功')):
            attacks = sorted(outcomes[win].items(),
                             key=lambda item: (-len(item[1]), item[0]))
            entries = [
                '{}（{}）'.format(
                    '+'.join(roles.get(role, role) for role in atk_team),
                    '，'.join(sorted(attackers, key=_fold)))
                for atk_team, attackers in attacks
            ]
            lines.append('{}：'.format(label))
            lines.extend('- ' + entry for entry in (entries or ['暂无记录']))
    return '\n'.join(lines)


def format_wrongbook(guild_query, match_count=1, db_path=DATA_DB_PATH):
    try:
        match_count = int(match_count)
    except (TypeError, ValueError) as exc:
        raise ValueError('场数必须是1到{}之间的整数'.format(MAX_WRONGBOOK_MATCHES)) from exc
    if not 1 <= match_count <= MAX_WRONGBOOK_MATCHES:
        raise ValueError('场数必须是1到{}之间的整数'.format(MAX_WRONGBOOK_MATCHES))
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN')
        atk_guild, error = _resolve_attack_guild(conn, guild_query)
        if error:
            return error
        match_dates = _recent_gvg_matches(conn, atk_guild, match_count)
        if not match_dates:
            return '没有找到“{}”的进攻记录。'.format(atk_guild)
        results = _wrongbook_results(conn, atk_guild, match_dates)
    finally:
        conn.close()
    roles = _role_name_map()
    return '\n\n'.join(_wrongbook_section(date, atk_guild, results[date], roles)
                       for date in match_dates)


def _ambiguous_players(rows):
    roles = _role_name_map()
    lines = ['发现同名玩家，请使用UID代替玩家名：']
    for index, row in enumerate(rows, 1):
        avatar_role_id = row['avatar_role_id']
        avatar = roles.get(avatar_role_id, avatar_role_id) or '未知'
        lines.append('{}. {} {} {}头像'.format(
            index, row['name'], row['cuid'], avatar))
    return '\n'.join(lines)


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


def theoretical_builds(conn, cuid):
    equips = []
    for row in conn.execute('SELECT * FROM pvp_equips WHERE cuid=?', (int(cuid),)):
        if row['equip_type'] not in SPEED_PARTS:
            continue
        speed = next((float(row[f'sub{i}_value'] or 0) for i in range(1, 5)
                      if row[f'sub{i}_prop'] == 'SpeedValue'), 0)
        equips.append({'equip_id': row['equip_id'], 'equip_type': row['equip_type'],
                       'set_name': row['set_name'], 'speed': speed})
    first = best_speed_combo(equips)
    used = {item['equip_id'] for item in first[1]} if first else set()
    return first, best_speed_combo(equips, used)


def _solution_lines(title, ranked, roles):
    lines = [title]
    for rate, total, drop_rate, team in ranked:
        names = '+'.join(roles.get(role, role) for role in team)
        win_pct = int(rate * 100 + 0.5)
        drop_pct = int(drop_rate * 100 + 0.5)
        lines.append('- {}，胜率{}%，场次{}，掉人率{}%'.format(
            names, win_pct, total, drop_pct))
    if len(lines) == 1:
        lines.append('- 暂无记录')
    return lines


# Rank by posterior mean win rate with a Jeffreys Beta(0.5, 0.5) prior.
SOLUTION_SQL = '''
WITH matched AS (
    SELECT r.battle_id, r.round_idx, r.win
    FROM gvg_rounds AS r INDEXED BY idx_gvg_rounds_recent
    WHERE r.start_ts >= ?
      AND (SELECT COUNT(*) FROM gvg_units AS d INDEXED BY sqlite_autoindex_gvg_units_1
           WHERE d.battle_id=r.battle_id AND d.round_idx=r.round_idx
             AND d.side='def') = 3
      AND (SELECT COUNT(DISTINCT d.role_id) FROM gvg_units AS d INDEXED BY sqlite_autoindex_gvg_units_1
           WHERE d.battle_id=r.battle_id AND d.round_idx=r.round_idx
             AND d.side='def' AND d.role_id IN (?, ?, ?)) = 3
), attacks AS (
    SELECT r.battle_id, r.round_idx, r.win,
           MIN(a.role_id) AS first_role, MAX(a.role_id) AS third_role,
           (SELECT role_id FROM gvg_units AS middle INDEXED BY sqlite_autoindex_gvg_units_1
            WHERE middle.battle_id=r.battle_id AND middle.round_idx=r.round_idx
              AND middle.side='atk'
            ORDER BY role_id LIMIT 1 OFFSET 1) AS second_role,
           MAX(CASE WHEN a.dead != 0 THEN 1 ELSE 0 END) AS dropped
    FROM matched AS r
    CROSS JOIN gvg_units AS a INDEXED BY sqlite_autoindex_gvg_units_1
      ON a.battle_id=r.battle_id AND a.round_idx=r.round_idx AND a.side='atk'
    GROUP BY r.battle_id, r.round_idx
    HAVING COUNT(*) = 3
)
SELECT first_role, second_role, third_role,
       SUM(win) * 1.0 / COUNT(*) AS rate, COUNT(*) AS total,
       SUM(dropped) * 1.0 / COUNT(*) AS drop_rate
FROM attacks
GROUP BY first_role, second_role, third_role
HAVING SUM(win) > 0
ORDER BY (SUM(win) + 0.5) / (COUNT(*) + 1) DESC,
         rate DESC, total DESC, drop_rate, first_role, second_role, third_role
LIMIT ?
'''

MATCHUP_SOLUTION_SQL = SOLUTION_SQL.replace(
    'WHERE r.start_ts >= ?',
    'WHERE r.start_ts >= ? AND r.atk_guild = ? AND r.def_guild = ?',
)


def ranked_solutions(conn, target, atk_guild=None, def_guild=None,
                     limit=SOLUTION_LIMIT):
    args = [now_ms() - RECENT_DAYS * MILLIS_PER_DAY]
    if atk_guild is None or def_guild is None:
        sql = SOLUTION_SQL
    else:
        sql = MATCHUP_SOLUTION_SQL
        args.extend((atk_guild, def_guild))
    args.extend(target)
    args.append(limit)
    return [
        (row['rate'], row['total'], row['drop_rate'],
         (row['first_role'], row['second_role'], row['third_role']))
        for row in conn.execute(sql, args)
    ]


def format_solutions(role_ids, db_path=DATA_DB_PATH):
    target = tuple(sorted(role_ids))
    if len(target) != 3 or len(set(target)) != 3:
        return '请输入三个不同角色。'
    roles = _role_name_map()
    title = '防守：' + '+'.join(roles.get(role, role) for role in target)
    conn = connect_data(db_path)
    try:
        ranked = ranked_solutions(conn, target)
        return '\n'.join(_solution_lines(title, ranked, roles))
    finally:
        conn.close()


def resolve_player(query, conn):
    query = str(query).strip()
    if not query:
        return None, '请输入玩家名或UID。'
    latest = conn.execute('SELECT match_date FROM gvg_defence '
                          'ORDER BY match_date DESC, id DESC LIMIT 1').fetchone()
    if latest is None:
        return None, '暂无团战防守数据，请先更新数据。'
    scope = 'match_date=?'
    args = (latest['match_date'],)
    fields = 'id, cuid, name, avatar_role_id, match_date'
    if query.isdecimal():
        row = conn.execute(
            f'SELECT {fields} FROM gvg_defence WHERE {scope} AND cuid=?', (*args, query)
        ).fetchone()
        if row:
            return row, None
    conn.create_function('fold_name', 1, _fold)
    for predicate in ('fold_name(name) = ?', 'instr(fold_name(name), ?) > 0'):
        rows = conn.execute(
            f'SELECT {fields} FROM gvg_defence WHERE {scope} AND {predicate} '
            'ORDER BY name, cuid', (*args, _fold(query))).fetchmany(13)
        if len(rows) == 1:
            return rows[0], None
        if rows:
            message = _ambiguous_players(rows[:12])
            if len(rows) > 12:
                message += '\n匹配过多，请输入更完整的名字或UID。'
            return None, message
    return None, '最近一场团战中没有找到玩家“{}”。'.format(query)


MAIN_PROP_LABELS = {
    'HPValue': '生', 'HPRate': '生',
    'AttackValue': '攻', 'AttackRate': '攻',
    'DefenceValue': '防', 'DefenceRate': '防',
    'SpeedValue': '速', 'CriticalRate': '暴', 'CriticalDamageRate': '爆',
    'EffectHitRate': '命', 'ResistanceRate': '抗',
}


def main_prop_text(kind):
    return MAIN_PROP_LABELS.get(kind, '?')


def load_artifact_names(path=MASTER_DB_PATH):
    conn = sqlite3.connect(str(path))
    try:
        try:
            rows = conn.execute("SELECT a.ID, COALESCE(i.Name, 'T_Item_Name_' || a.ID), c.Value "
                'FROM Artifact a LEFT JOIN Item i ON i.ID=a.ID '
                "LEFT JOIN CHS c ON c.Key=COALESCE(i.Name, 'T_Item_Name_' || a.ID)")
        except sqlite3.OperationalError:
            rows = conn.execute("SELECT a.ID, 'T_Item_Name_' || a.ID, c.Value FROM Artifact a "
                                "LEFT JOIN CHS c ON c.Key='T_Item_Name_' || a.ID")
        return {aid: value or (aid if key.startswith(('T_', 'UI_')) else key)
                for aid, key, value in rows}
    finally:
        conn.close()


def load_set_names(path=MASTER_DB_PATH):
    with sqlite3.connect(str(path)) as conn:
        return {key: name.removesuffix('套装') for key, name in conn.execute(
            'SELECT e.ID, COALESCE(c.Value,e.Name,e.ID) FROM EquipmentSet e '
            'LEFT JOIN CHS c ON c.Key=e.Name')}


def defence_role_line(row, roles, artifacts, set_names):
    main_props = ''.join(main_prop_text(row[part + '_prop'])
                         for part in ('shoes', 'ring', 'necklace'))
    sets = ''.join((str(count) if count > 1 else '') + set_names.get(key, key)
                   for key, count in Counter(filter(None, (row['sets'] or '').split(','))).items())
    details = [sets or '无成套', main_props]
    if row['artifact_id']:
        details.append('{}级{}'.format(row['artifact_lv'] if row['artifact_lv'] is not None else '?',
                                     artifacts.get(row['artifact_id'], row['artifact_id'])))
    details.append('{}生'.format(row['hp']))
    return '{}：{}'.format(roles.get(row['role_id'], row['role_id']), ' | '.join(details))


def format_defence(query, db_path=DATA_DB_PATH):
    conn = connect_data(db_path)
    try:
        # Keep the parent lookup and child read in one snapshot during a refresh.
        conn.execute('BEGIN')
        player, error = resolve_player(query, conn)
        if error:
            return error
        builds = theoretical_builds(conn, player['cuid'])
        roles, artifacts = _role_name_map(), load_artifact_names()
        set_names = load_set_names()
        speeds = [format(build[0], 'g') if build else '-' for build in builds]
        lines = [
            '{} | CUID {}'.format(player['name'], player['cuid']),
            '头像：{}'.format(roles.get(player['avatar_role_id'], player['avatar_role_id'])),
            '一速：{} | 二速：{}'.format(*speeds),
            '日期：{}'.format(player['match_date']),
        ]
        for team, label in ((1, '上半'), (2, '下半')):
            lines.append('── {} ──'.format(label))
            for row in conn.execute('SELECT * FROM gvg_defence_units '
                                    'WHERE defence_id=? AND team=? ORDER BY pos', (player['id'], team)):
                lines.append(defence_role_line(row, roles, artifacts, set_names))
        return '\n'.join(lines)
    finally:
        conn.close()


# Sub-account member intelligence
MATCH_ORDER = (
    "CAST(substr(match_id, 1, instr(match_id, '~') - 1) AS INTEGER) DESC, "
    "CAST(substr(match_id, instr(match_id, '~') + 1) AS INTEGER) DESC"
)


def _current_match(conn):
    return meta_get(conn, 'sub_current_match_id') or ''


def resolve_member_player(query, conn, current_only=True):
    query = str(query).strip()
    if not query:
        return None, '请输入玩家名或UID。'
    if current_only:
        match_id = _current_match(conn)
        if not match_id:
            return None, '暂无小号当前团战名单，请先更新数据。'
        rows = conn.execute(
            'SELECT * FROM gvg_members WHERE match_id=? ORDER BY name, cuid',
            (match_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT * FROM gvg_members ORDER BY ' + MATCH_ORDER,
        ).fetchall()
        seen = set()
        latest = []
        for row in rows:
            if row['cuid'] not in seen:
                latest.append(row)
                seen.add(row['cuid'])
        rows = latest
    folded = _fold(query)
    for matches in (
            [row for row in rows if _fold(row['name']) == folded],
            [row for row in rows if str(row['cuid']) == query],
            [row for row in rows if folded in _fold(row['name'])]):
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, _ambiguous_players(matches[:12])
    return None, '没有找到玩家“{}”。'.format(query)


def normalize_speed(value):
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
    normalized = normalize_speed(speed)
    if normalized is None:
        return '一速格式错误，请输入 227、265-270 或 122+。'
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        player, error = resolve_member_player(player_query, conn)
        if error:
            return error
        conn.execute(
            'UPDATE gvg_members SET max_speed=? WHERE match_id=? AND cuid=?',
            (normalized, player['match_id'], player['cuid']),
        )
        conn.commit()
        return '已更新 {} 的一速：{}'.format(player['name'], normalized)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def resolve_member_info_target(text, db_path=DATA_DB_PATH):
    """Find the longest player name that leaves at least one information word."""
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN')
        matches = list(re.finditer(r'\S+', text))
        if len(matches) < 2:
            return None, None, '格式：团战 信息 玩家名或UID 信息内容'
        errors = []
        for split_at in range(len(matches) - 1, 0, -1):
            query = text[:matches[split_at - 1].end()].strip()
            player, error = resolve_member_player(query, conn)
            if player is not None:
                return dict(player), matches[split_at].start(), None
            errors.append(error)
        return None, None, errors[-1] if errors else '没有找到玩家。'
    finally:
        conn.close()


def set_member_info(player, info, db_path=DATA_DB_PATH):
    info = str(info).strip()
    if not info:
        return '信息内容不能为空。'
    init_database(db_path)
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        try:
            if _current_match(conn) != player['match_id']:
                return '团战场次已变化，请重新输入信息。'
            cursor = conn.execute(
                'UPDATE gvg_members SET info=? WHERE match_id=? AND cuid=?',
                (info, player['match_id'], player['cuid']),
            )
            if cursor.rowcount != 1:
                return '玩家已不在当前团战名单中，请重新输入。'
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return '已保存 {} 的信息。'.format(player['name'])
    finally:
        conn.close()


def format_member_history(player_query, db_path=DATA_DB_PATH, limit=5):
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN')
        player, error = resolve_member_player(player_query, conn, current_only=False)
        if error:
            return error
        rows = conn.execute('''
            SELECT match_id, match_date, guild_name, info
            FROM gvg_members
            WHERE cuid=? AND match_id<>? AND info IS NOT NULL AND trim(info)<>''
            ORDER BY ''' + MATCH_ORDER + ' LIMIT ?',
            (player['cuid'], _current_match(conn), int(limit)),
        ).fetchall()
        if not rows:
            return '{} 暂无历史信息。'.format(player['name'])
        lines = ['{} 的最近{}次历史信息：'.format(player['name'], len(rows))]
        for index, row in enumerate(rows, 1):
            lines.append('{}. {}（{}）对阵{}\n{}'.format(
                index, row['match_date'], row['match_id'],
                row['guild_name'], row['info']))
        return '\n'.join(lines)
    finally:
        conn.close()


def format_member_player(player_query, db_path=DATA_DB_PATH):
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN')
        player, error = resolve_member_player(player_query, conn)
        if error:
            return error
        roles = _role_name_map()
        avatar = roles.get(player['avatar_role_id'],
                           player['avatar_role_id']) or '未知'
        lines = ['{}（{}）'.format(player['name'], avatar)]
        if player['max_speed']:
            lines.append('一速：{}'.format(player['max_speed']))
        if player['info']:
            lines.append(player['info'])
        return '\n'.join(lines)
    finally:
        conn.close()


def format_member_solutions(role_ids, db_path=DATA_DB_PATH):
    target = tuple(sorted(role_ids))
    if len(target) != 3 or len(set(target)) != 3:
        return '请输入三个不同角色。'
    conn = connect_data(db_path)
    try:
        conn.execute('BEGIN')
        if not _current_match(conn):
            return '暂无小号当前团战信息，请先更新数据。'
        our_guild = meta_get(conn, 'sub_target_guild_name') or ''
        enemy_guild = meta_get(conn, 'sub_enemy_guild_name') or ''
        if not our_guild or not enemy_guild:
            return '暂无小号当前交战双方信息，请先更新数据。'
        roles = _role_name_map()
        title = '防守：' + '+'.join(roles.get(role, role) for role in target)
        sections = []
        for atk_guild, def_guild in (
                (our_guild, enemy_guild), (enemy_guild, our_guild)):
            ranked = ranked_solutions(
                conn, target, atk_guild, def_guild, limit=-1)
            if ranked:
                sections.append('\n'.join(_solution_lines(
                    '{}解法：'.format(atk_guild), ranked, roles)))
        if not sections:
            sections.append('近30天内无针对该防守的交手记录，以下为整体解法。')
            sections.append('\n'.join(_solution_lines(
                '整体解法：', ranked_solutions(conn, target), roles)))
        return title + '\n' + '\n'.join(sections)
    finally:
        conn.close()
