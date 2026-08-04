import asyncio
import random
import re

from .database import init_database
from .queries import (
    format_defenses,
    format_player,
    format_solutions,
    format_win_rates,
    resolve_roles,
    set_max_speed,
    set_member_info,
)
from .speed import query_pvp_speeds_sync
from .updater import update_all_sync, update_result_text


async def report_to_superuser(message):
    import hoshino
    from hoshino.config import SUPERUSERS

    if not SUPERUSERS:
        return
    bot = hoshino.get_bot()
    self_ids = list(bot.get_self_ids())
    if not self_ids:
        return
    await bot.send_private_msg(
        self_id=random.choice(self_ids),
        user_id=SUPERUSERS[0],
        message=message,
    )


async def run_update_job(service, bot=None, ev=None, notify_superuser=True):
    try:
        result = await asyncio.to_thread(update_all_sync)
        message = update_result_text(result)
        if notify_superuser:
            for warning in result['warnings']:
                await report_to_superuser('团战更新警告：\n' + warning)
        if bot is not None and ev is not None:
            await bot.send(ev, message, at_sender=False)
        else:
            service.logger.info(message)
    except Exception as exc:
        service.logger.exception(exc)
        message = '团战数据更新失败：\n{}'.format(exc)
        if bot is not None and ev is not None:
            await bot.send(ev, message, at_sender=False)
        if notify_superuser:
            await report_to_superuser(message)


GVG_HELP = (
    '团战指令：\n'
    '团战 作业 角色1 角色2 角色3\n'
    '团战 防守\n'
    '团战 胜率表\n'
    '团战 一速 玩家名或UID 速度\n'
    '团战 信息 玩家名或UID 内容\n'
    '团战 玩家名或UID\n'
    '团战 更新数据（仅限Bot主）'
)


def _format_query_reply(message):
    return str(message).lstrip('\r\n')


_REGISTERED = False


def register_gvg(service):
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    init_database()

    @service.scheduled_job('cron', hour=8, minute=5)
    async def gvg_daily_update():
        await run_update_job(service)

    @service.on_prefix('查速')
    async def pvp_speed_command(bot, ev):
        query = ev.message.extract_plain_text().strip()
        try:
            message = await asyncio.to_thread(query_pvp_speeds_sync, query)
        except Exception as exc:
            message = '查速失败：{}'.format(exc)
        await bot.send(ev, _format_query_reply(message), at_sender=False)

    @service.on_prefix('团战')
    async def gvg_command(bot, ev):
        raw = ev.message.extract_plain_text().strip()
        if raw.startswith(('测速', '总结')):
            return
        if not raw:
            await bot.send(ev, GVG_HELP, at_sender=False)
            return

        if raw == '更新数据':
            from hoshino.config import SUPERUSERS
            if str(ev.user_id) not in {str(user) for user in SUPERUSERS}:
                await bot.send(ev, '只有机器人主人可以强制更新数据。',
                               at_sender=False)
                return
            await bot.send(ev, '开始更新团战数据，请稍候。', at_sender=False)
            await run_update_job(
                service, bot=bot, ev=ev, notify_superuser=False)
            return

        if raw == '防守':
            try:
                message = format_defenses()
            except Exception as exc:
                message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=False)
            return

        if raw == '胜率表':
            try:
                message = format_win_rates()
            except Exception as exc:
                message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=False)
            return

        if raw.startswith('作业'):
            queries = raw[len('作业'):].strip().split()
            if len(queries) != 3:
                message = '格式：团战 作业 角色1 角色2 角色3'
            else:
                try:
                    role_ids, error = resolve_roles(queries)
                    message = error or format_solutions(role_ids)
                except Exception as exc:
                    message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=False)
            return

        if raw.startswith('一速'):
            content = raw[len('一速'):].strip()
            match = re.fullmatch(r'(.+?)\s+(\d{1,4}(?:-\d{1,4})?)', content)
            if not match:
                message = '格式：团战 一速 玩家名或UID 227（或265-270）'
            else:
                try:
                    message = set_max_speed(match.group(1), match.group(2))
                except Exception as exc:
                    message = '更新失败：{}'.format(exc)
            await bot.send(ev, message, at_sender=False)
            return

        if raw.startswith('信息'):
            try:
                message = set_member_info(raw[len('信息'):].strip())
            except Exception as exc:
                message = '更新失败：{}'.format(exc)
            await bot.send(ev, message, at_sender=False)
            return

        try:
            message = format_player(raw)
        except Exception as exc:
            message = '查询失败：{}'.format(exc)
        await bot.send(ev, _format_query_reply(message), at_sender=False)
