import itertools
import json
import sqlite3
import threading
from collections import Counter
from pathlib import Path

from .api import oid, team_roles
from .database import ALIAS_PATH, DATA_DB_PATH, MASTER_DB_PATH, connect_data, now_ms

RECENT_DAYS = 30
MILLIS_PER_DAY = 86400000
STATS_LOCK = threading.Lock()
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
    FROM gvg_rounds AS r
    WHERE r.start_ts >= ?
      AND (SELECT COUNT(*) FROM gvg_units AS d
           WHERE d.battle_id=r.battle_id AND d.round_idx=r.round_idx
             AND d.side='def') = 3
      AND (SELECT COUNT(DISTINCT d.role_id) FROM gvg_units AS d
           WHERE d.battle_id=r.battle_id AND d.round_idx=r.round_idx
             AND d.side='def' AND d.role_id IN (?, ?, ?)) = 3
), attacks AS (
    SELECT r.battle_id, r.round_idx, r.win,
           MIN(a.role_id) AS first_role, MAX(a.role_id) AS third_role,
           (SELECT role_id FROM gvg_units AS middle
            WHERE middle.battle_id=r.battle_id AND middle.round_idx=r.round_idx
              AND middle.side='atk'
            ORDER BY role_id LIMIT 1 OFFSET 1) AS second_role,
           MAX(CASE WHEN a.dead != 0 THEN 1 ELSE 0 END) AS dropped
    FROM matched AS r
    CROSS JOIN gvg_units AS a
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
    fields = 'cuid, name, avatar_role_id'
    if query.isdecimal():
        row = conn.execute(
            f'SELECT {fields} FROM gvg_members WHERE cuid=?', (query,)
        ).fetchone()
        if row:
            return row, None
    conn.create_function('fold_name', 1, _fold)
    for predicate in ('fold_name(name) = ?', 'instr(fold_name(name), ?) > 0'):
        rows = conn.execute(
            f'SELECT {fields} FROM gvg_members WHERE {predicate} '
            'ORDER BY name, cuid', (_fold(query),)).fetchmany(13)
        if len(rows) == 1:
            return rows[0], None
        if rows:
            message = _ambiguous_players(rows[:12])
            if len(rows) > 12:
                message += '\n匹配过多，请输入更完整的名字或UID。'
            return None, message
    return None, '没有找到玩家“{}”。'.format(query)


MAIN_PROP_LABELS = {
    'HPValue': '生', 'HPRate': '生',
    'AttackValue': '攻', 'AttackRate': '攻',
    'DefenceValue': '防', 'DefenceRate': '防',
    'SpeedValue': '速', 'CriticalRate': '暴', 'CriticalDamageRate': '爆',
    'EffectHitRate': '命', 'ResistanceRate': '抗',
}


def defence_role_line(role, stats, master):
    equipment = role.get('EquipmentMap') or {}
    counts = Counter((equipment.get(part) or {}).get('Set')
                     for part in (*SPEED_PARTS, 'Shoes'))
    sets = []
    for set_id, count in counts.most_common():
        if not set_id:
            continue
        required = max(int((master.equipment_sets.get(set_id) or {}).get('Count') or 99), 1)
        active = count // required
        if active:
            sets.append('{}{}'.format(active if active > 1 else '',
                                     master.equipment_set_name(set_id)))
    main_props = ''.join(MAIN_PROP_LABELS.get(
        ((equipment.get(part) or {}).get('MainProp') or {}).get('PropertyType'), '?')
        for part in ('Shoes', 'Ring', 'Necklace'))
    details = [''.join(sets) or '无成套', main_props]
    bond = role.get('ArtifactData') or {}
    if bond.get('StaticID'):
        details.append('{}级{}'.format(bond.get('LV', '?'),
                                     master.artifact_name(bond['StaticID'])))
    details.append('{}生'.format(round(stats['HP'])))
    return '{}：{}'.format(master.role_name(role['StaticID']), ' / '.join(details))


_MASTER_SIGNATURE = None


def _load_stats_master(helper):
    global _MASTER_SIGNATURE
    path = Path(MASTER_DB_PATH).resolve()
    stat = path.stat()
    signature = (str(path), stat.st_mtime_ns, stat.st_size)
    if _MASTER_SIGNATURE != signature or helper.MASTER is None \
            or helper.MASTER.path.resolve() != path:
        # Release the previous tables before constructing the replacement.
        helper.MASTER = None
        _MASTER_SIGNATURE = None
        helper.load_master_data(path)
        _MASTER_SIGNATURE = signature
    return helper.MASTER


def format_defence(query, db_path=DATA_DB_PATH):
    from ..Frida import helper

    with STATS_LOCK:
        conn = connect_data(db_path)
        try:
            player, error = resolve_player(query, conn)
            if error:
                return error
            defence = conn.execute(
                "SELECT payload, snapshot_date FROM gvg_snapshots "
                "WHERE cuid=? AND kind='defence' ORDER BY snapshot_date DESC LIMIT 1",
                (player['cuid'],)).fetchone()
            if defence is None:
                return '该玩家暂无团战防守数据，请先更新数据。'
            # Decode one snapshot at a time; retain only deduplicated equipment.
            cards = (json.loads(row['payload']) for row in conn.execute(
                "SELECT payload FROM gvg_snapshots WHERE cuid=? AND kind='equipment' "
                'ORDER BY snapshot_date DESC', (player['cuid'],)))
            builds = theoretical_builds(cards)
            master = _load_stats_master(helper)
            speeds = [format(build[0], 'g') if build else '-' for build in builds]
            lines = [
                '{} | CUID {}'.format(player['name'], player['cuid']),
                '头像：{}'.format(master.role_name(player['avatar_role_id'])),
                '一速：{} | 二速：{}'.format(*speeds),
                '日期：{}'.format(defence['snapshot_date']),
            ]
            teams = json.loads(defence['payload'])['DefenceTeamData']
            for key, label in (('FirstTeam', '上半'), ('SecondTeam', '下半')):
                lines.append('── {} ──'.format(label))
                roles = team_roles(teams.get(key) or {})
                for role, stats in zip(roles, helper.calculate_team_stats(roles)):
                    lines.append(defence_role_line(role, stats, master))
            return '\n'.join(lines)
        finally:
            conn.close()
