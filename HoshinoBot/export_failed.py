"""直接运行：导出最近30天烟雨阁进攻失败的小局，日期按UTC。"""

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from itertools import groupby
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parent / 'data'
GUILD = '烟雨阁'
RECENT_DAYS = 30
BASE_HEADERS = [
    '团战日期', '战斗ID', '小局序号',
    '进攻公会', '进攻玩家', '进攻玩家UID',
    '防守公会', '防守玩家', '防守玩家UID', '小局胜负',
]
SLOTS = [(side, index) for side in ('进攻', '防守') for index in range(1, 4)]
HEADERS = (
    BASE_HEADERS
    + ['{}角色{}'.format(side, index) for side, index in SLOTS]
)


def open_readonly(path):
    path = Path(path).resolve()
    if not path.is_file():
        raise FileNotFoundError('找不到数据库：{}'.format(path))
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def load_role_names(path):
    """名称仅用于展示，缺少master.db时仍保留全部战斗记录。"""
    if not Path(path).is_file():
        print('提示：找不到master.db，角色名称列将使用角色ID。')
        return {}
    conn = None
    try:
        conn = open_readonly(path)
        rows = conn.execute('''
            SELECT r.ID, COALESCE(c.Value, r.ID) AS name
            FROM Role AS r
            LEFT JOIN CHS AS c ON c.Key = r.NAME
            WHERE r.ID LIKE 'H%'
        ''')
        return {str(row['ID']): str(row['name']) for row in rows}
    except sqlite3.Error as exc:
        print('提示：无法读取角色名称，名称列将使用角色ID：{}'.format(exc))
        return {}
    finally:
        if conn is not None:
            conn.close()


def export_failed(data_dir=DATA_DIR, now=None):
    data_dir = Path(data_dir)
    now = now or datetime.now(timezone.utc)
    now = now.astimezone(timezone.utc)
    start = now - timedelta(days=RECENT_DAYS)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)
    conn = open_readonly(data_dir / 'data.db')
    try:
        # 同一读取快照中选取小局与角色；LEFT JOIN保留缺少角色的小局。
        records = conn.execute('''
            SELECT r.*, u.side, u.pos, u.role_id
            FROM gvg_rounds AS r
            LEFT JOIN gvg_units AS u
              ON r.battle_id = u.battle_id AND r.round_idx = u.round_idx
            WHERE r.start_ts >= ? AND r.start_ts <= ?
              AND r.atk_guild = ? AND r.win = 0
            ORDER BY r.start_ts, r.battle_id, r.round_idx, u.side, u.pos
        ''', (start_ms, end_ms, GUILD)).fetchall()
    finally:
        conn.close()

    names = load_role_names(data_dir / 'master.db') if records else {}
    output_rows = []
    incomplete_count = 0
    for key, items in groupby(records, key=lambda row: (
            row['battle_id'], row['round_idx'])):
        items = list(items)
        battle = items[0]
        teams = {side: sorted(
            [unit for unit in items if unit['side'] == side],
            key=lambda unit: unit['pos']) for side in ('atk', 'def')}
        if any(len(team) > 3 for team in teams.values()):
            raise ValueError('战斗 {} 小局 {} 的角色超过3人，请检查数据库。'.format(*key))
        if any(len(team) != 3 for team in teams.values()):
            incomplete_count += 1
        units = []
        for side in ('atk', 'def'):
            units.extend(teams[side] + [None] * (3 - len(teams[side])))
        row = [
            datetime.fromtimestamp(battle['start_ts'] / 1000,
                                   timezone.utc).strftime('%Y-%m-%d'),
            battle['battle_id'], battle['round_idx'],
            battle['atk_guild'], battle['atk_name'], battle['atk_cuid'],
            battle['def_guild'], battle['def_name'], battle['def_cuid'], '失败',
        ]
        row.extend(names.get(str(unit['role_id']), unit['role_id'])
                   if unit is not None else '' for unit in units)
        output_rows.append(row)

    output = data_dir / '{}_进攻失败_近{}天_{}.csv'.format(
        GUILD, RECENT_DAYS, now.strftime('%Y%m%dT%H%M%S%fZ'))
    with output.open('w', newline='', encoding='utf-8-sig') as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        writer.writerows(output_rows)
    print('筛选时间（UTC）：{} 至 {}'.format(
        start.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S')))
    print('共导出 {} 条失败小局记录。'.format(len(output_rows)))
    if incomplete_count:
        print('提示：{} 条记录的角色数据不完整，缺少的角色列留空。'.format(incomplete_count))
    print('CSV：{}'.format(output.resolve()))
    return output


if __name__ == '__main__':
    try:
        export_failed()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise SystemExit('导出失败：{}'.format(exc))
