import asyncio
import random
import re

from .database import init_database
from .queries import format_defence, format_solutions, resolve_roles
from .updater import (
    daily_result_text,
    run_daily_sync,
    update_all_sync,
    update_result_text,
)


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


async def run_update_job(service, bot=None, ev=None, notify_superuser=True,
                         run_daily=False):
    try:
        result = await asyncio.to_thread(update_all_sync, run_daily=run_daily)
        message = update_result_text(result)
        if notify_superuser:
            await report_to_superuser(message)
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


async def run_daily_job(service, bot=None, ev=None, notify_superuser=True):
    try:
        result = await asyncio.to_thread(run_daily_sync)
        message = daily_result_text(result)
    except Exception as exc:
        service.logger.exception(exc)
        message = '日常清理失败：{}'.format(exc)
    if bot is not None and ev is not None:
        await bot.send(ev, message, at_sender=False)
    else:
        service.logger.info(message)
    if notify_superuser:
        await report_to_superuser(message)


GVG_HELP = (
    '团战指令：\n'
    '团战 [作业] 角色1 角色2 角色3\n'
    '团战 [数据] 玩家名或UID\n'
    '团战 清日常（仅限Bot主）\n'
    '团战 更新数据（仅限Bot主）'
)

# Serialize queries, including snapshot reads, to bound concurrent memory.
QUERY_LOCK = asyncio.Lock()


def query_reply(raw):
    if raw == '数据' or re.match(r'数据\s', raw):
        query = raw[2:].strip()
        return format_defence(query) if query else '格式：团战 数据 玩家名或UID'
    explicit = raw.startswith('作业')
    parts = (raw[2:].strip() if explicit else raw).split()
    if explicit or len(parts) == 3:
        if len(parts) != 3:
            return '格式：团战 [作业] 角色1 角色2 角色3'
        role_ids, error = resolve_roles(parts)
        if not error:
            return format_solutions(role_ids)
        if explicit:
            return error
        # A player's full name may contain spaces.
    return format_defence(raw)


_REGISTERED = False


def register_gvg(service):
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    init_database()

    @service.scheduled_job('cron', hour=0, minute=1, timezone='UTC')
    async def gvg_daily_update():
        await run_update_job(service, run_daily=True)

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

        if raw == '清日常':
            from hoshino.config import SUPERUSERS
            if str(ev.user_id) not in {str(user) for user in SUPERUSERS}:
                await bot.send(ev, '只有机器人主人可以清理日常。',
                               at_sender=False)
                return
            await bot.send(ev, '开始清理日常，请稍候。', at_sender=False)
            await run_daily_job(
                service, bot=bot, ev=ev, notify_superuser=False)
            return

        try:
            async with QUERY_LOCK:
                message = await asyncio.to_thread(query_reply, raw)
        except Exception as exc:
            service.logger.exception(exc)
            message = '查询失败：{}'.format(exc)
        await bot.send(ev, str(message).lstrip('\r\n'), at_sender=False)
