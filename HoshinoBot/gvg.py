import asyncio
import random
import re

from .api import group_account
from .database import init_database
from .queries import (
    format_defence,
    format_member_history,
    format_member_player,
    format_member_solutions,
    format_solutions,
    format_wrongbook,
    resolve_member_info_target,
    resolve_roles,
    set_max_speed,
    set_member_info,
)
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


GVG_HELP_MAIN = (
    '团战指令：\n'
    '团战 [作业] 角色1 角色2 角色3\n'
    '团战 [数据] 玩家名或UID\n'
    '团战 错题本 团名 [场数]\n'
    '团战 清日常（仅限Bot主）\n'
    '团战 更新数据（仅限Bot主）'
)
GVG_HELP_SUB = (
    '团战指令：\n'
    '团战 [作业] 角色1 角色2 角色3\n'
    '团战 一速 玩家名或UID 速度\n'
    '团战 信息 玩家名或UID 内容\n'
    '团战 历史 玩家名或UID\n'
    '团战 玩家名或UID\n'
    '团战 错题本 团名 [场数]\n'
    '团战 清日常（仅限Bot主）\n'
    '团战 更新数据（仅限Bot主）'
)

# Serialize queries, including snapshot reads, to bound concurrent memory.
QUERY_LOCK = asyncio.Lock()


def query_reply(raw, account='main', has_images=False):
    if raw == '错题本' or re.match(r'错题本\s', raw):
        content = raw[len('错题本'):].strip()
        match = re.fullmatch(r'(.+?)(?:\s+(\d+))?', content)
        if not match:
            return '格式：团战 错题本 团名 [场数]'
        return format_wrongbook(match.group(1), match.group(2) or 1)
    if raw in ('防守', '胜率表') or re.match(r'(防守|胜率表)\s', raw):
        return '该指令暂不支持。'
    if account == 'alt' and (raw == '一速' or re.match(r'一速\s', raw)):
        content = raw[len('一速'):].strip()
        match = re.fullmatch(r'(.+?)\s+(\d{1,4}(?:-\d{1,4}|\+)?)', content)
        if not match:
            return '格式：团战 一速 玩家名或UID 227（或265-270、122+）'
        return set_max_speed(match.group(1), match.group(2))
    if account == 'alt' and (raw == '历史' or re.match(r'历史\s', raw)):
        query = raw[len('历史'):].strip()
        return (format_member_history(query) if query else
                '格式：团战 历史 玩家名或UID')
    if account == 'alt' and (raw == '信息' or re.match(r'信息\s', raw)):
        if has_images:
            return '当前版本的团战信息只支持文字。'
        body = raw[len('信息'):].strip()
        player, payload_start, error = resolve_member_info_target(body)
        return error or set_member_info(player, body[payload_start:])
    if account == 'main' and (raw == '数据' or re.match(r'数据\s', raw)):
        query = raw[2:].strip()
        return format_defence(query) if query else '格式：团战 数据 玩家名或UID'
    if account == 'alt' and (raw == '数据' or re.match(r'数据\s', raw)):
        return '小号群请用“团战 玩家名或UID”查询手动信息。'
    explicit = raw.startswith('作业')
    parts = (raw[2:].strip() if explicit else raw).split()
    if explicit or len(parts) == 3:
        if len(parts) != 3:
            return '格式：团战 [作业] 角色1 角色2 角色3'
        role_ids, error = resolve_roles(parts)
        if not error:
            return (format_member_solutions(role_ids) if account == 'alt'
                    else format_solutions(role_ids))
        if explicit:
            return error
        # A player's full name may contain spaces.
    return format_member_player(raw) if account == 'alt' else format_defence(raw)


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
        try:
            account = group_account(getattr(ev, 'group_id', None))
        except Exception as exc:
            service.logger.exception(exc)
            await bot.send(ev, '团战群配置错误：{}'.format(exc), at_sender=False)
            return
        if account is None:
            return
        if not raw:
            await bot.send(ev, GVG_HELP_MAIN if account == 'main'
                           else GVG_HELP_SUB, at_sender=False)
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
                has_images = any(
                    (segment.get('type') if hasattr(segment, 'get')
                     else getattr(segment, 'type', None)) == 'image'
                    for segment in ev.message)
                message = await asyncio.to_thread(
                    query_reply, raw, account, has_images)
        except Exception as exc:
            service.logger.exception(exc)
            message = '查询失败：{}'.format(exc)
        await bot.send(ev, str(message).lstrip('\r\n'), at_sender=False)
