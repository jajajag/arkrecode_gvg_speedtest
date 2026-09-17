import itertools
import json
import sqlite3
from pathlib import Path

from .database import ALIAS_PATH, DATA_DB_PATH, MASTER_DB_PATH, connect_data, now_ms

RECENT_DAYS = 30
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
ORDER BY rate DESC, total DESC, drop_rate, first_role, second_role, third_role
'''


def format_solutions(role_ids, db_path=DATA_DB_PATH):
    target = tuple(sorted(role_ids))
    if len(target) != 3 or len(set(target)) != 3:
        return '请输入三个不同角色。'
    roles = _role_name_map()
    title = '防守：' + '+'.join(roles.get(role, role) for role in target)
    conn = connect_data(db_path)
    try:
        rows = conn.execute(SOLUTION_SQL, (
            now_ms() - RECENT_DAYS * MILLIS_PER_DAY, *target))
        ranked = ((row['rate'], row['total'], row['drop_rate'],
                   (row['first_role'], row['second_role'], row['third_role']))
                  for row in rows)
        return '\n'.join(_solution_lines(title, ranked, roles))
    finally:
        conn.close()


def resolve_player(query, conn):
    query = str(query).strip()
    if not query:
        return None, '请输入玩家名或UID。'
    latest = conn.execute('SELECT match_id, match_date FROM gvg_defence '
                          'ORDER BY match_date DESC, id DESC LIMIT 1').fetchone()
    if latest is None:
        return None, '暂无团战防守数据，请先更新数据。'
    scope = 'match_date=? AND match_id=?'
    args = (latest['match_date'], latest['match_id'])
    fields = 'id, cuid, name, avatar_role_id, match_id, match_date'
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


def main_prop_text(kind, value):
    kind = kind or ''
    label = MAIN_PROP_LABELS.get(kind, '?')
    if value is None:
        return '?' + label if kind else '?'
    number = float(value)
    if kind.endswith('Rate'):
        return '{:g}%{}'.format(round(number * 100, 2), label)
    return '{:g}{}'.format(round(number, 2), label)


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


def defence_role_line(row, roles, artifacts):
    main_props = ' '.join(main_prop_text(row[part + '_prop'], row[part + '_value'])
                         for part in ('shoes', 'ring', 'necklace'))
    details = [row['sets'] or '无成套', main_props]
    if row['artifact_id']:
        details.append('{}级{}'.format(row['artifact_lv'] if row['artifact_lv'] is not None else '?',
                                     artifacts.get(row['artifact_id'], row['artifact_id'])))
    details.append('{}生'.format(row['hp']))
    return '{}：{}'.format(roles.get(row['role_id'], row['role_id']), ' / '.join(details))


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
                lines.append(defence_role_line(row, roles, artifacts))
        return '\n'.join(lines)
    finally:
        conn.close()
