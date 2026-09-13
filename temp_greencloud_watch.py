"""临时 HoshinoBot 库存监控。

填写 GROUP_ID 和 AT_QQ 后重启 HoshinoBot，每 5 分钟检查一次。
直接使用 HoshinoBot 已连接的 QQ 机器人，不需要额外 HTTP 接口或 Token。
删除本文件并重启即可卸载，根目录的可选加载入口会自动跳过缺失文件。
状态仅存内存，重启后若仍有货会再次提醒；不产生配置或状态文件。
"""

import asyncio
import re
import time
from html.parser import HTMLParser
from urllib.request import Request, urlopen

from hoshino import get_bot

# ==================== 在这里填写 ====================
GROUP_ID = 0                  # 要发送提醒的 QQ 群号
AT_QQ = 0                     # 要 @ 的 QQ 号
CHECK_INTERVAL_SECONDS = 300  # 推荐每 5 分钟检查一次，至少 60 秒
# ====================================================

URL = "https://greencloudvps.com/billing/store/cn-premium-optimized"
PLAN = "CN Premium Optimized Plan Mini (Tokyo)"
TIMEOUT_SECONDS = 30
_REGISTERED = False


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def parse_stock(html):
    parser = PageText()
    parser.feed(html)
    text = " ".join(" ".join(parser.parts).split())
    if text.count(PLAN) != 1:
        raise ValueError("无法唯一定位 Tokyo Mini 商品，可能是验证页面或网页改版")
    section = text.split(PLAN, 1)[1].split("CN Premium Optimized Plan", 1)[0]
    # 只接受紧随目标名称之后的明确库存数字，避免读到其他套餐或把缺失当有货。
    stock = re.match(r"\s*([0-9]+)\s+Available\b", section, re.I)
    if not stock:
        raise ValueError("目标商品缺少明确的 Available 数字，本次不提醒")
    if not re.search(r"\$\s*25\.00\s+USD\s+Monthly\b", section, re.I):
        raise ValueError("目标商品价格不是预期的 25 USD/月，本次不提醒")
    return int(stock.group(1))


def fetch_stock():
    request = Request(URL, headers={
        "User-Agent": "Mozilla/5.0 (compatible; GreenCloudStockWatch/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    })
    with urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        html = response.read().decode("utf-8", errors="replace")
    return parse_stock(html)


async def send_notification(stock):
    bot = get_bot()
    await bot.send_group_msg(
        group_id=int(GROUP_ID),
        message=(
            f"[CQ:at,qq={int(AT_QQ)}] "
            f"GreenCloud 补货提醒：{PLAN}\n"
            f"当前库存：{stock} Available\n价格：25 USD/月\n{URL}\n"
            "库存可能随时变化，请打开网页确认。"
        ),
    )


class Monitor:
    def __init__(self, logger):
        self.logger = logger
        self.notified = False
        self.failures = 0
        self.retry_after = 0

    async def check(self):
        if time.monotonic() < self.retry_after:
            return
        try:
            # 在线程中抓取，避免阻塞 HoshinoBot 的其他命令。
            stock = await asyncio.get_running_loop().run_in_executor(None, fetch_stock)
            self.logger.info(f"{PLAN}：{stock} Available")
            if stock == 0:
                self.notified = False
            elif not self.notified:
                await send_notification(stock)
                # 发送成功才标记；失败时保留状态以便重试。
                self.notified = True
                self.logger.info("已发送库存 @ 提醒，本轮有货不再重复发送")
            self.failures = 0
            self.retry_after = 0
        except Exception as exc:
            self.failures += 1
            delay = min(CHECK_INTERVAL_SECONDS * 2 ** min(self.failures - 1, 6),
                        max(1800, CHECK_INTERVAL_SECONDS))
            self.retry_after = time.monotonic() + delay
            self.logger.error(f"库存检查或通知失败，至少 {delay} 秒后重试：{exc}")


def register(service):
    global _REGISTERED
    if _REGISTERED:
        return
    if not str(GROUP_ID).isdigit() or int(GROUP_ID) <= 0 or not str(AT_QQ).isdigit() or int(AT_QQ) <= 0:
        service.logger.warning("临时库存监控未启用：请填写 temp_greencloud_watch.py 中的 GROUP_ID 和 AT_QQ")
        return
    if CHECK_INTERVAL_SECONDS < 60:
        service.logger.warning("临时库存监控未启用：检查间隔至少需要 60 秒")
        return
    monitor = Monitor(service.logger)
    service.scheduled_job(
        'interval', seconds=CHECK_INTERVAL_SECONDS,
        id='temp_greencloud_stock_watch', max_instances=1, coalesce=True,
    )(monitor.check)
    _REGISTERED = True
    service.logger.info(f"临时库存监控已启用，每 {CHECK_INTERVAL_SECONDS} 秒检查一次")
