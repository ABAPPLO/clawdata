"""
按类型采集抖音视频（有人值守，验证码由你在弹出的浏览器窗口里手动完成）。

示例：
    python collect_category.py                     # 单人武术展示
    python collect_category.py --keyword 功夫 --category 单人武术展示
    python collect_category.py --count 10 --max-wait 300
    python collect_category.py --list-only          # 只列不下载

会打开一个可见的 Edge 窗口，你完成登录/滑块后它会自动抓到搜索结果并下载。
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import time as dtime

from clawdata.collection.browser_collect import search_videos_manual
from clawdata.download.downloader import DownloadConfig, download_items
from clawdata.core.humanize import QuietHours
from clawdata.core.session import load_cookies, has_session
from clawdata.core.paths import DEFAULT_DB_PATH, DOWNLOADS_DIR


def parse_time(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按类型采集抖音视频（手动过验证）")
    parser.add_argument("--keyword", default="单人武术展示", help="搜索关键词")
    parser.add_argument("--category", default="单人武术展示", help="记录类型/分类（tag_category）")
    parser.add_argument("--count", type=int, default=8, help="采集条数")
    parser.add_argument("--max-wait", type=float, default=300, help="等待手动过验证的最长时间（秒）")
    parser.add_argument("--list-only", action="store_true", help="只列不下载")
    parser.add_argument("--outdir", default=DOWNLOADS_DIR, help="下载目录")
    parser.add_argument("--delay-lo", type=float, default=3.0)
    parser.add_argument("--delay-hi", type=float, default=8.0)
    parser.add_argument("--quiet-start", default="00:00")
    parser.add_argument("--quiet-end", default="07:00")
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ck = load_cookies()
    if not has_session(ck):
        print("[提示] Cookie 可能缺少会话字段，若弹登录请登录后继续")

    print(f"[采集] 关键词：{args.keyword}；类型：{args.category}；最多 {args.count} 条")
    hot = search_videos_manual(args.keyword, args.count, ck,
                               on_progress=lambda m: print(f"[浏览器] {m}"),
                               max_wait=args.max_wait)
    if not hot:
        print("[采集] 未采集到视频")
        return 1

    items = []
    for h in hot:
        items.append({
            "title": f"[{args.category}] {h.get('author')} | {h.get('title')[:30]}",
            "author": h.get("author", ""),
            "aweme_id": h.get("aweme_id", ""),
            "word": args.keyword,
            "category": args.category,
            "url": h["url"],
            "filename": f"{h.get('author') or 'dy'}_{h.get('aweme_id')}.mp4",
            "referer": "https://www.douyin.com/",
        })
    print(f"[采集] 共 {len(items)} 条")
    for it in items:
        print("  -", it["title"])

    if args.list_only or not items:
        return 0

    cfg = DownloadConfig(
        outdir=args.outdir,
        delay_range=(args.delay_lo, args.delay_hi),
        quiet=QuietHours(parse_time(args.quiet_start), parse_time(args.quiet_end)),
        cookies=ck,
        store=DEFAULT_DB_PATH,
        category=args.category,
    )
    download_items(items, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
