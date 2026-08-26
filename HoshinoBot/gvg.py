import asyncio
import random
import re
from pathlib import Path

from .api import BASE_DIR, INFO_IMAGE_LOCK, cache_info_images
from .database import IMAGES_DIR, init_database
from .queries import (
    format_member_history,
    format_player,
    format_solutions,
    format_win_rates,
    format_wrongbook,
    resolve_member_info_target,
    resolve_roles,
    set_max_speed,
    set_member_info,
)
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


GVG_HELP = (
    '团战指令：\n'
    '团战 作业 角色1 角色2 角色3\n'
    '团战 胜率表\n'
    '团战 错题本 团名 [场数，最多10场]\n'
    '团战 一速 玩家名或UID 速度\n'
    '团战 信息 玩家名或UID 内容或图片\n'
    '团战 历史 玩家名或UID\n'
    '团战 玩家名或UID\n'
    '团战 更新数据（仅限Bot主）'
)


def _format_query_reply(message):
    return str(message).lstrip('\r\n')


def _message_parts(message):
    for segment in message:
        try:
            kind = segment['type']
            data = segment.get('data', {})
        except (KeyError, TypeError, AttributeError):
            continue
        yield str(kind), dict(data)


def _has_images(message):
    return any(kind == 'image' for kind, _ in _message_parts(message))


def _extract_info_segments(message, remove_plain_chars):
    remaining = int(remove_plain_chars)
    segments = []
    image_data = []
    for kind, data in _message_parts(message):
        if kind == 'text':
            content = str(data.get('text') or '')
            if remaining:
                removed = min(remaining, len(content))
                remaining -= removed
                content = content[removed:]
            if content:
                if segments and segments[-1]['type'] == 'text':
                    segments[-1]['content'] += content
                else:
                    segments.append({'type': 'text', 'content': content})
        elif kind == 'image' and remaining == 0:
            image_index = len(image_data)
            image_data.append(data)
            segments.append({'type': 'image', 'source_index': image_index})
    if remaining:
        raise ValueError('无法从原始消息中定位玩家信息内容')
    while segments and segments[0]['type'] == 'text' \
            and not segments[0]['content'].strip():
        segments.pop(0)
    return segments, image_data


def _replace_image_sources(segments, image_paths):
    result = []
    for segment in segments:
        if segment['type'] == 'image':
            result.append({
                'type': 'image',
                'path': image_paths[segment['source_index']],
            })
        else:
            result.append(segment)
    return result


def _store_member_info(player, source_segments, image_data):
    with INFO_IMAGE_LOCK:
        image_paths = cache_info_images(image_data, IMAGES_DIR)
        info_segments = _replace_image_sources(
            source_segments, image_paths)
        return set_member_info(player, info_segments)


def _render_player_segments(segments):
    rendered = []
    base_dir = BASE_DIR.resolve()
    for segment in segments:
        if segment['type'] == 'text':
            rendered.append({
                'type': 'text',
                'data': {'text': segment['content']},
            })
            continue
        image_path = (BASE_DIR / Path(segment['path'])).resolve()
        try:
            image_path.relative_to(base_dir)
        except ValueError as exc:
            raise ValueError('图片路径超出插件目录') from exc
        if not image_path.is_file():
            raise FileNotFoundError('图片文件不存在：{}'.format(image_path.name))
        rendered.append({
            'type': 'image',
            'data': {'file': image_path.as_uri()},
        })
    return rendered


_REGISTERED = False


def register_gvg(service):
    global _REGISTERED
    if _REGISTERED:
        return
    _REGISTERED = True
    init_database()

    @service.scheduled_job('cron', hour=8, minute=5)
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

        if raw == '胜率表':
            try:
                message = format_win_rates()
            except Exception as exc:
                message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=False)
            return

        if raw == '错题本' or re.match(r'错题本\s', raw):
            content = raw[len('错题本'):].strip()
            match = re.fullmatch(r'(.+?)(?:\s+(\d+))?', content)
            if not match:
                message = '格式：团战 错题本 团名 [场数，最多10场]'
            else:
                try:
                    message = format_wrongbook(
                        match.group(1), match.group(2) or 1)
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
            match = re.fullmatch(
                r'(.+?)\s+(\d{1,4}(?:-\d{1,4}|\+)?)', content)
            if not match:
                message = '格式：团战 一速 玩家名或UID 227（或265-270、122+）'
            else:
                try:
                    message = set_max_speed(match.group(1), match.group(2))
                except Exception as exc:
                    message = '更新失败：{}'.format(exc)
            await bot.send(ev, message, at_sender=False)
            return

        if raw == '历史' or re.match(r'历史\s', raw):
            player_query = raw[len('历史'):].strip()
            if not player_query:
                message = '格式：团战 历史 玩家名或UID'
            else:
                try:
                    message = format_member_history(player_query)
                except Exception as exc:
                    message = '查询失败：{}'.format(exc)
            await bot.send(ev, _format_query_reply(message), at_sender=False)
            return

        if raw == '信息' or re.match(r'信息\s', raw):
            try:
                match = re.match(r'信息\s*', raw)
                body_start = match.end()
                body = raw[body_start:]
                has_images = _has_images(ev.message)
                player, payload_start, error = resolve_member_info_target(
                    body, has_images=has_images)
                if error:
                    message = error
                else:
                    plain_message = ev.message.extract_plain_text()
                    raw_start = plain_message.find(raw)
                    if raw_start < 0:
                        raise ValueError('无法定位团战信息指令')
                    source_segments, image_data = _extract_info_segments(
                        ev.message,
                        raw_start + body_start + payload_start,
                    )
                    message = await asyncio.to_thread(
                        _store_member_info,
                        player,
                        source_segments,
                        image_data,
                    )
            except Exception as exc:
                message = '更新失败：{}'.format(exc)
            await bot.send(ev, message, at_sender=False)
            return

        try:
            message = format_player(raw)
            if isinstance(message, list):
                message = _render_player_segments(message)
        except Exception as exc:
            message = '查询失败：{}'.format(exc)
        if isinstance(message, list):
            await bot.send(ev, message, at_sender=False)
        else:
            await bot.send(ev, _format_query_reply(message), at_sender=False)
