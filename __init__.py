import re
from hoshino import Service, priv
from hoshino.typing import CQEvent
from .speed import compute_speed_async, overtake_prob

sv_name = '团战测速'
sv_help = (
    '[团战测速] 星陨计划团战测速，示例：\n'
    '团战测速\n'
    '水马 1 56 135\n'
    '水琴 1 70 170\n'
    '水拳 4 58 131\n'
    '朱茵 1 101\n'
    '盖儿 1 84\n'
    '（没输入速度的为敌方，101表示到达终点）\n'
    '[团战总结] 直接输出团战总结，示例：\n'
    '团战总结 上路上半 水马 1 56 135 水琴 1 70 170 水拳 4 58 131 朱茵 1 101 专武1.3w 盖儿 1 84 闪避羁绊\n'
    '[乱速] 计算两个角色乱速的概率，示例：\n'
    '乱速 245 240'
).strip()

sv = Service(name=sv_name, use_priv=priv.NORMAL, manage_priv=priv.ADMIN,
             visible=True, enable_on_default=True, bundle='娱乐', help_=sv_help)

def _parse_tokens_test(text: str):
    # Initialization
    tokens = text.strip().split()
    allies, enemies, character = [], [], []
    pos = 0
    for token in tokens:
        # Character name, or action gauges
        if pos < 3:
            if pos == 0: character.append(token)
            else: character.append(int(token))
            pos += 1
        else:
            # Speed
            if token.isdigit():
                character.append(int(token))
                allies.append(tuple(character))
                character = []
                pos = 0
            # Next character's name
            else:
                enemies.append(tuple(character))
                character = [token]
                pos = 1
    # Put the last charactor into enmies if it has three elements
    if character: enemies.append(tuple(character))
    # Change the action gauge to 100 if not a single >100 value is specified
    end_gauge = [x[2] for x in allies + enemies]
    if not any(v > 100 for v in end_gauge) and end_gauge.count(100) == 1:
        for lst in (allies, enemies):
            for i, item in enumerate(lst):
                if item[2] == 100:
                    lst[i] = item[:2] + (101,) + item[3:]
    return allies, enemies

# 示例：团战测速 水马 1 56 135 水琴 1 70 170 水拳 4 58 131 朱茵 1 101 盖儿 1 84
@sv.on_prefix('团战测速')
async def speed_test(bot, ev: CQEvent):
    # Extract plain text after 团战测速
    raw = ev.message.extract_plain_text()
    # Output instructions if no text is provided
    if not raw.strip():
        await bot.finish(ev, '\n' + sv_help, at_sender=True)
    # Parse the input tokens
    try:
        allies, enemies = _parse_tokens_test(raw)
    except Exception as e:
        await bot.finish(ev, 
            f'输入错误，请检查输入，或发团战测速查看详细用法', at_sender=True)
    # Compute the speeds of enemies
    try:
        ret = await compute_speed_async(allies=allies, enemies=enemies, 
                                        N_sample=int(1e6))
        lines = []
        for (enemy, enemy_min, enemy_max, mean, med, mode_int, ally_min) in ret:
            enemy = enemy[0]
            lines.append(
                f'\n- {enemy}：速度区间[{enemy_min:.1f}, {enemy_max:.1f}]，'
                f'MC均值{mean:.1f}，中位数{med:.1f}，最可能速度{mode_int:d}'
                f'稳定超车速度{ally_min:.1f}'
            )
        msg = ''.join(lines)
        await bot.send(ev, msg, at_sender=True)
    except Exception as e:
        await bot.send(ev, f'计算错误，请检查输入数值是否正确', at_sender=True)

def _parse_tokens_summary(text: str):
    tokens = text.strip().split()
    # Re for checking int input
    is_int = lambda s: re.fullmatch(r"[+-]?\d+", s) is not None
    # Title
    title = tokens[0]
    allies, enemies, notes = [], [], {}
    i = 1
    while i < len(tokens):
        # (name, g1, g2)
        if i + 2 >= len(tokens) or is_int(tokens[i]) or (not is_int(tokens[i+1])) or (not is_int(tokens[i+2])):
            i += 1
            continue
        name = tokens[i]
        g1 = int(tokens[i + 1])
        g2 = int(tokens[i + 2])
        i += 3
        # Alley: (name, g1, g2, speed)
        if i < len(tokens) and is_int(tokens[i]):
            allies.append((name, g1, g2, int(tokens[i])))
            i += 1
            continue
        # Enemy: (name, g1, g2, note or "")
        note = ""
        if i < len(tokens) and not is_int(tokens[i]):
            if not (i + 2 < len(tokens) and is_int(tokens[i + 1]) and is_int(tokens[i + 2])):
                note = tokens[i]
                i += 1
        enemies.append((name, g1, g2, note))
    # Change the action gauge to 100 if not a single >100 value is specified
    end_gauge = [x[2] for x in allies + enemies]
    if not any(v > 100 for v in end_gauge) and end_gauge.count(100) == 1:
        for lst in (allies, enemies):
            for idx, item in enumerate(lst):
                if item[2] == 100:
                    lst[idx] = item[:2] + (101,) + item[3:]
    return title, allies, enemies

# 示例：团战总结 上路上半 水马 1 56 135 水琴 1 70 170 水拳 4 58 131 朱茵 1 101 专武1.3w 盖儿 1 84 闪避羁绊
@sv.on_prefix('团战总结')
async def speed_summary(bot, ev: CQEvent):
    raw = ev.message.extract_plain_text()
    if not raw.strip():
        await bot.finish(ev, '\n' + sv_help, at_sender=True)
    try:
        title, allies, enemies = _parse_tokens_summary(raw)
    except Exception:
        await bot.finish(ev, 
            f'输入错误，请检查输入，或发团战测速查看详细用法', at_sender=True)
    try:
        ret = await compute_speed_async(allies=allies, enemies=enemies,
                                        N_sample=int(1e6))
        # Sort by mode_int (most possible speed)
        ret = sorted(ret, key=lambda x: x[5], reverse=True)
        lines = []
        for (enemy, enemy_min, enemy_max, mean, med, mode_int, ally_min) in ret:
            enemy_name, note = enemy[0], enemy[3]
            lines.append(f"{enemy_name}{mode_int:d}-{note}")
        prefix = f"{title}：" if title else ""
        msg = prefix + "，".join(lines)
        await bot.send(ev, msg, at_sender=False)
    except Exception:
        await bot.send(ev, "计算错误：请检查输入数值是否正确", at_sender=True)

#@sv.on_rex(r'^乱速\s*(\d+)\s+(\d+)$')
@sv.on_rex(r'^乱速\s*(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)$')
async def overtake(bot, ev: CQEvent):
    # Match input numbers
    match = ev['match']
    #v1 = float(match.group(1))
    #v2 = float(match.group(2))
    v1, v2 = sorted((float(match.group(1)), float(match.group(2))))
    try:
        # Calculate the overtaking probability
        prob = overtake_prob(v1, v2)
        percent = round(prob * 100, 2)
        await bot.send(ev,
            f'\n乱速的概率为：{percent}%', at_sender=True)
    except Exception as e:
        await bot.send(ev, f'计算错误，请检查输入数值是否正确', at_sender=True)
