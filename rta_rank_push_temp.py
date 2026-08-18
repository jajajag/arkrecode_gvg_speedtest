"""临时 RTA 排名私聊推送；删除本文件即可立即停用。"""

import asyncio
import random
from datetime import datetime
from pathlib import Path

import hoshino
from hoshino import Service, priv

from .HoshinoBot.api import get_sub_game_client


# 只需把 0 改成接收消息的 QQ 号。
QQ_ID = 0

# 游戏登录不在这里配置：直接复用 HoshinoBot/data/account.json 中的
# SubAccount 共享客户端。MainAccount 留给正常团战更新使用。
TRACKED_CUID = 104827882
RANKS = (1, 2, 3, 50)
BOT_SELF_ID = None  # 多个 Bot QQ 同时在线时，可填写指定的 Bot QQ；否则随机选一个。

THIS_FILE = Path(__file__).resolve()

sv = Service(
    name='临时RTA排名推送',
    use_priv=priv.NORMAL,
    manage_priv=priv.ADMIN,
    visible=False,
    enable_on_default=True,
    bundle='娱乐',
)


def _query_rank_data():
    # 复用进程内既有客户端及其请求锁，不创建新登录、不覆盖团战会话。
    client = get_sub_game_client()
    return client.call(
        'RTARankBattleHandler.QueryRankList',
        {},
        delay=(0.8, 1.2),
        required_key='RankInfos',
    )


def _rank_row(item):
    rank_info = item.get('PlayerInfo') or {}
    player = rank_info.get('PlayerInfo') or {}
    try:
        return {
            'rank': int(item.get('Rank')),
            'name': str(player.get('Name') or '未知玩家'),
            'cuid': int(player.get('CUID')),
            'score': int(rank_info.get('Score')),
        }
    except (TypeError, ValueError):
        return None


def select_rank_rows(rank_data):
    selected = []
    for item in rank_data.get('RankInfos') or []:
        if not isinstance(item, dict):
            continue
        row = _rank_row(item)
        if row is not None and (
                row['rank'] in RANKS or row['cuid'] == TRACKED_CUID):
            selected.append(row)
    return sorted(selected, key=lambda row: row['rank'])


def format_rank_message(rank_data, now=None):
    rows = select_rank_rows(rank_data)
    if not rows:
        raise ValueError('RTA 排名响应中没有找到要推送的名次')

    timestamp = (now or datetime.now()).strftime('%Y-%m-%d %H:%M')
    lines = ['【RTA 排名分数线】{}'.format(timestamp)]
    for row in rows:
        followed = '（关注）' if row['cuid'] == TRACKED_CUID else ''
        lines.append('第{}名｜{}｜{}分{}'.format(
            row['rank'], row['name'], row['score'], followed))
    if not any(row['cuid'] == TRACKED_CUID for row in rows):
        lines.append('关注玩家 {} 当前不在返回的前50名中'.format(TRACKED_CUID))
    return '\n'.join(lines)


async def _send_private_message(message):
    bot = hoshino.get_bot()
    self_ids = list(bot.get_self_ids())
    if not self_ids:
        raise RuntimeError('当前没有可用的 QQ Bot 账号')

    if BOT_SELF_ID is None:
        self_id = random.choice(self_ids)
    else:
        available_ids = {int(value) for value in self_ids}
        if int(BOT_SELF_ID) not in available_ids:
            raise RuntimeError('配置的 BOT_SELF_ID 当前未登录')
        self_id = int(BOT_SELF_ID)

    await bot.send_private_msg(
        self_id=self_id,
        user_id=int(QQ_ID),
        message=message,
    )


@sv.scheduled_job('cron', minute='0,20,40')
async def rta_rank_push_job():
    # 模块已加载时直接删除本文件，也会从下一次调度起停止请求和发送。
    if not THIS_FILE.is_file() or int(QQ_ID) <= 0:
        return
    try:
        rank_data = await asyncio.to_thread(_query_rank_data)
        message = format_rank_message(rank_data)
        await _send_private_message(message)
        sv.logger.info('RTA 排名已推送：\n%s', message)
    except Exception as exc:
        sv.logger.exception('RTA 排名推送失败：%s', exc)
