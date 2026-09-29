"""
人味化行为工具：随机延迟、深夜禁止窗口、伪装请求头。

这些工具用来让自动化动作尽量像真人：
- 每个动作之间存在随机的、有上下限的停顿，而不是固定间隔。
- 深夜（可配置时段）禁止下载，自动等待到允许时段再继续。
- 请求头用真实浏览器 UA / Accept 等，减少被风控识别的概率。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, time as dtime


def random_delay(lo: float, hi: float) -> None:
    """在 [lo, hi] 秒之间随机休眠，模拟人类动作间隔。"""
    if hi <= lo:
        hi = lo + 1
    time.sleep(random.uniform(lo, hi))


@dataclass
class QuietHours:
    """深夜禁止时段。默认 00:00 - 07:00 不下载。"""

    start: dtime = dtime(0, 0)
    end: dtime = dtime(7, 0)

    def in_quiet(self, now: datetime | None = None) -> bool:
        now = now or datetime.now()
        cur = now.time()
        if self.start <= self.end:
            return self.start <= cur < self.end
        # 跨天时段，例如 22:00 - 07:00
        return cur >= self.start or cur < self.end

    def next_open(self, now: datetime | None = None) -> datetime:
        """返回下一个允许下载的时间点。"""
        now = now or datetime.now()
        if not self.in_quiet(now):
            return now
        end_dt = datetime.combine(now.date(), self.end)
        if self.start <= self.end:
            # 当天时段，例如 00:00-07:00：今天 end 时刻开放
            return end_dt
        # 跨天时段，例如 22:00-07:00：
        # 若当前在凌晨（cur < end），今天 end 开放；若在深夜（cur >= start），明天 end 开放
        if now.time() < self.end:
            return end_dt
        return end_dt + timedelta(days=1)

    def wait_until_open(self, poll: float = 60.0) -> None:
        """如果当前在禁止时段，一直等到允许时段（每隔 poll 秒检查一次）。"""
        if not self.in_quiet():
            return
        print(f"[人味化] 当前是深夜禁止时段 ({self.start:%H:%M}-{self.end:%H:%M})，等待到 {self.next_open():%H:%M:%S} 再继续...")
        while self.in_quiet():
            time.sleep(poll)


def human_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """返回一组接近真实浏览器的请求头。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
    }
    if extra:
        headers.update(extra)
    return headers
