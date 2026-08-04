import asyncio
import itertools
import sqlite3
from collections import Counter

import numpy as np

from .api import (
    GameRequestError,
    get_sub_game_client,
    query_player_card,
    search_friend_players,
)
from .database import DATA_DB_PATH, save_pvp_equips


PARTS = ('Weapon', 'Head', 'Body', 'Necklace', 'Ring')
SPEED_SET_BASE = 189
BROKEN_SET_BASE = 169

# An action gauge of >100 means the character is the first to act.
def compute_speed(
        allies: list[tuple[str, float, float, float]],
        enemies: list[tuple[str, float, float]],
        N_sample: int = int(1e6)):
    '''
    allies: list of tuples (name, start_gauge, end_gauge, speed)
    enemy: tuple (name, start_gauge, end_gauge)
    '''
    ally_start_gauge, ally_end_gauge = [], []
    enemy_start_gauge, enemy_end_gauge = [], []

    for i in range(len(allies)):
        # Ranges of allies' action gauges
        ally_start_gauge_lower = max(allies[i][1] - 0.5, 0)
        ally_start_gauge_upper = min(allies[i][1] + 0.5, 5)
        ally_end_gauge_lower = min(allies[i][2] - 0.5, 100)
        ally_end_gauge_upper = min(allies[i][2] + 0.5, 100)
        # Sample allies' action gauges
        ally_start_gauge.append(np.random.uniform(
            ally_start_gauge_lower, ally_start_gauge_upper, N_sample))
        ally_end_gauge.append(np.random.uniform(
            ally_end_gauge_lower, ally_end_gauge_upper, N_sample))

    for i in range(len(enemies)):
        # Ranges of enemies' action gauges
        enemy_start_gauge_lower = max(enemies[i][1] - 0.5, 0)
        enemy_start_gauge_upper = min(enemies[i][1] + 0.5, 5)
        enemy_end_gauge_lower = min(enemies[i][2] - 0.5, 100)
        enemy_end_gauge_upper = min(enemies[i][2] + 0.5, 100)
        # Sample enemies' action gauges
        enemy_start_gauge.append(np.random.uniform(
            enemy_start_gauge_lower, enemy_start_gauge_upper, N_sample))
        enemy_end_gauge.append(np.random.uniform(
            enemy_end_gauge_lower, enemy_end_gauge_upper, N_sample))

    enemy_info = []
    for i in range(len(enemies)):
        # Initialize enemy's speed bounds
        enemy_min_speed, enemy_max_speed = 0, float('inf')
        enemy_speed_cat = []

        for j in range(len(allies)):
            # Compute enemy's speed using Monte Carlo
            enemy_speed = (enemy_end_gauge[i] - enemy_start_gauge[i]) \
                    / (ally_end_gauge[j] - ally_start_gauge[j]) * allies[j][3]
            # Enemy's strict speed bounds (Now we use Monte Carlo)
            #min_speed = ally_speed * (enemy_end_lower - enemy_start_upper) \
            #        / (ally_end_upper - ally_start_lower)
            #max_speed = ally_speed * (enemy_end_upper - enemy_start_lower) \
            #        / (ally_end_lower - ally_start_upper)
            enemy_min_speed = max(enemy_min_speed, np.min(enemy_speed))
            enemy_max_speed = min(enemy_max_speed, np.max(enemy_speed))
            enemy_speed_cat.append(enemy_speed)

        # Filter out impossible speeds
        enemy_speed_cat = np.concatenate(enemy_speed_cat)
        enemy_speed_cat = enemy_speed_cat[np.where(
            (enemy_speed_cat <= enemy_max_speed) \
            & (enemy_speed_cat >= enemy_min_speed))]

        # Compute mean and median speeds
        mean = np.mean(enemy_speed_cat)
        med = np.median(enemy_speed_cat)
        # Most likely integer speed (mode of rounded samples)
        spd_int = np.rint(enemy_speed_cat).astype(np.int64)
        if spd_int.size == 0:
            # If the initial action gauge is fake, spd_int will be empty
            mode_int = np.nan
        else:
            lo = spd_int.min()
            mode_int = (np.bincount(spd_int - lo).argmax() + lo)
        # Compute ally's minimum speed to act before this enemy
        ally_min_speed = enemy_max_speed / 0.95
        
        enemy_info.append((enemies[i], enemy_min_speed, enemy_max_speed,
                           mean, med, mode_int, ally_min_speed))

    return enemy_info

async def compute_speed_async(*args, **kwargs):
    return await asyncio.to_thread(compute_speed, *args, **kwargs)

# Calculate the probability that chara_1 acts before chara_2
def overtake_prob(v1, v2):
    # Compute the ratio of speeds
    r = v2 / v1

    # 4 possible outcomes (close-form solution dereived by ChatGPT):
    if r <= 19/20:
        return 1.0
    if r >= 20/19:
        return 0.0
    if r <= 1.0:
        p = -200 * r + 381 - 361 / (2 * r)
    else:
        p = 361 / 2 * r - 380 + 200 / r

    return p


def _speed_value(row):
    for index in range(1, 5):
        if row['sub{}_prop'.format(index)] == 'SpeedValue':
            return float(row['sub{}_value'.format(index)] or 0)
    return 0.0


def _player_cuids(conn, query):
    if query.isdigit():
        rows = conn.execute(
            'SELECT DISTINCT cuid FROM pvp_equips WHERE cuid=?',
            (int(query),),
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT DISTINCT cuid FROM pvp_equips WHERE player_name LIKE ?',
            ('%{}%'.format(query),),
        ).fetchall()
    return [int(row['cuid']) for row in rows]


def _display_player_name(names, query):
    counts = Counter(names)
    if query and not query.isdigit():
        for name, _ in counts.most_common():
            if query == name:
                return name
        for name, _ in counts.most_common():
            if query in name:
                return name
    return counts.most_common(1)[0][0]


def load_player_equips(conn, query):
    cuids = _player_cuids(conn, query)
    if not cuids:
        return {}
    placeholders = ','.join('?' for _ in cuids)
    rows = conn.execute(
        '''
        SELECT * FROM pvp_equips
        WHERE cuid IN ({})
          AND equip_type IN ('Weapon', 'Head', 'Body', 'Necklace', 'Ring')
        ORDER BY cuid, player_name, equip_type
        '''.format(placeholders),
        cuids,
    ).fetchall()
    players = {}
    names = {}
    for row in rows:
        cuid = int(row['cuid'])
        equip = dict(row)
        equip['speed'] = _speed_value(row)
        players.setdefault(cuid, []).append(equip)
        names.setdefault(cuid, []).append(row['player_name'])
    return {
        (cuid, _display_player_name(names[cuid], query)): equips
        for cuid, equips in players.items()
    }


def best_speed_combo(equips, used=None):
    used = used or set()
    by_part = {part: [] for part in PARTS}
    for equip in equips:
        if equip['equip_id'] in used:
            continue
        part = equip['equip_type']
        if part in by_part:
            by_part[part].append(equip)
    if any(not by_part[part] for part in PARTS):
        return None

    best_total = -1.0
    best_combo = []
    best_mode = ''
    for combo in itertools.product(*(by_part[part] for part in PARTS)):
        speed_set_count = sum(
            equip['set_name'] == 'Speed' for equip in combo)
        sub_speed = sum(float(equip['speed']) for equip in combo)
        candidates = [('散件', BROKEN_SET_BASE + sub_speed)]
        if speed_set_count >= 3:
            candidates.append(('速度', SPEED_SET_BASE + sub_speed))
        for mode, total in candidates:
            if total > best_total:
                best_total = total
                best_combo = list(combo)
                best_mode = mode
    if best_total < 0:
        return None
    return best_total, best_combo, best_mode


def _format_speed(value):
    if value is None:
        return '-'
    return str(round(value, 2)).rstrip('0').rstrip('.')


def calculate_speeds(db_path, query):
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        players = load_player_equips(conn, query)
    finally:
        conn.close()

    result = []
    for (cuid, name), equips in sorted(
            players.items(), key=lambda item: (item[0][1], item[0][0])):
        first = best_speed_combo(equips)
        used = {equip['equip_id'] for equip in first[1]} if first else set()
        second = best_speed_combo(equips, used)
        result.append({
            'cuid': cuid,
            'name': name,
            'max_speed': first[0] if first else None,
            'max_mode': first[2] if first else None,
            'second_speed': second[0] if second else None,
            'second_mode': second[2] if second else None,
        })
    return result


def format_speed_results(players, query):
    if not players:
        return '没有找到包含“{}”的玩家装备。'.format(query)
    blocks = []
    for player in players:
        first = _format_speed(player['max_speed'])
        second = _format_speed(player['second_speed'])
        if player['max_mode']:
            first += '（{}）'.format(player['max_mode'])
        if player['second_mode']:
            second += '（{}）'.format(player['second_mode'])
        blocks.append('\n'.join((
            '{}（{}）'.format(player['name'], player['cuid']),
            '一速：{}'.format(first),
            '二速：{}'.format(second),
        )))
    return '\n'.join(blocks)


def _pvp_rank_item_from_card(card, fallback_player=None):
    pvp_info = card.get('PVPInfo') or {}
    support_data = card.get('BattleSupportData') or {}
    player = (support_data.get('PlayerInfo') or card.get('PlayerInfo')
              or fallback_player or {})
    if not player or not (pvp_info.get('DefenceTeam') or {}):
        return None
    return {'PlayerInfo': player, 'PVPInfo': pvp_info}


def query_pvp_speeds_sync(query, db_path=DATA_DB_PATH):
    query = str(query).strip()
    if not query:
        return '格式：查速 玩家名或UID'

    client = get_sub_game_client()
    client.login(attempts=3)
    if query.isdigit():
        targets = [{'CUID': int(query), 'Name': query}]
    else:
        targets = search_friend_players(client, query)
        if not targets:
            return '没有搜索到等级超过60的玩家“{}”。'.format(query)

    rank_items = []
    for player in targets:
        try:
            card = query_player_card(client, player['CUID'])
        except Exception:
            continue
        item = _pvp_rank_item_from_card(card, player)
        if item:
            rank_items.append(item)
    if not rank_items:
        raise GameRequestError('没有查询到可用的玩家PVP防守装备')

    save_pvp_equips({'PVPRankInfoList': rank_items}, db_path)
    players = calculate_speeds(db_path, query)
    return format_speed_results(players, query)

if __name__ == '__main__':
    ally_1 = ('水马', 1, 56, 135)
    ally_2 = ('水琴', 1, 70, 170)
    ally_3 = ('水拳', 4, 58, 131)
    enemy_1 = ('朱茵', 1, 101)
    enemy_2 = ('盖儿', 1, 84)
    print(compute_speed([ally_1, ally_2, ally_3], [enemy_1, enemy_2], 
                        N_sample=int(1e6)))
    print(overtake_prob(100, 100))
    print(overtake_prob(95, 100))
    print(overtake_prob(100, 95))
    print(overtake_prob(240, 246))
    print(overtake_prob(246, 240))
