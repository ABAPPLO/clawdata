"""
抖音热门视频下载主流程。

流程：
  1. 抓取抖音热榜（无需登录）
  2. 对前 N 个热词，用登录 cookie 搜索视频，收集下载地址
  3. 逐个（一条一条）下载，带随机人类间隔，深夜禁止下载

用法：
  python -m clawdata                     # 采集前 5 个热词各 3 条并下载
  python -m clawdata --keywords 10 --per-word 5
  python -m clawdata --list-only         # 只列视频清单，不下载
  python -m clawdata --no-cookie         # 跳过 Cookie，供测试

深夜禁止时段默认 00:00-07:00，可在命令行 --quiet-start/--quiet-end 覆盖。
"""

from __future__ import annotations

import argparse
import io
import os
import sys
from datetime import time as dtime
from typing import Any

from datetime import datetime

from clawdata.collection.douyin_hot import fetch_hot_list, normalize
from clawdata.collection.douyin_videos import search_videos
from clawdata.download.downloader import DownloadConfig, download_items
from clawdata.core.humanize import QuietHours
from clawdata.core.session import has_session, load_cookies
from clawdata.core.paths import DEFAULT_DB_PATH, DEFAULT_LINKS_PATH, DOWNLOADS_DIR
from clawdata.storage.store import save_hotlist_snapshot


def parse_time(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def start_tee(logfile: str) -> None:
    """把 stdout/stderr 同时写入日志文件，便于调度任务追溯。"""
    log = open(logfile, "a", encoding="utf-8", buffering=1)

    class _Tee:
        def __init__(self, stream):
            self.stream = stream

        def write(self, data):
            self.stream.write(data)
            if data:
                log.write(data)
            return len(data)

        def flush(self):
            self.stream.flush()
            log.flush()

    sys.stdout = _Tee(sys.stdout)
    sys.stderr = _Tee(sys.stderr)


def build_items(words: list[str], keywords: int, per_word: int, cookies: dict[str, str] | None) -> list[dict[str, Any]]:
    """为前 keywords 个热词各搜索 per_word 条视频，汇总成待下载列表。"""
    items: list[dict[str, Any]] = []
    for i, word in enumerate(words[:keywords], start=1):
        print(f"[采集] ({i}/{min(keywords, len(words))}) 热词：{word}")
        if not cookies:
            print("  跳过（未提供登录 Cookie）")
            continue
        try:
            vids = search_videos(word, cookies, count=per_word * 2)
        except Exception as exc:  # noqa: BLE001
            print(f"  采集失败：{exc}")
            continue
        vids = [v for v in vids if v.get("url")]
        for v in vids[:per_word]:
            desc = (v.get("title") or v.get("aweme_id") or "video")[:40]
            filename = f"{v.get('author') or 'dy'}_{v.get('aweme_id')}_{desc}.mp4"
            items.append({
                "title": f"{word} | {v.get('author')} | {desc}",
                "author": v.get("author", ""),
                "aweme_id": v.get("aweme_id", ""),
                "word": word,
                "url": v["url"],
                "filename": filename,
                "referer": "https://www.douyin.com/",
            })
    return items


def build_items_from_links(path: str, cookies: dict[str, str]) -> list[dict[str, Any]]:
    """从文件逐行读取链接（抖音 / B 站可混写），解析成待下载列表。"""
    from clawdata.collection.links import resolve_lines

    with open(path, encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    items, errors = resolve_lines(
        lines, cookies,
        on_progress=lambda i, n, ln, msg: print(f"  [{i}/{n}] {msg.split('：')[0]}：{ln}"),
    )
    for err in errors:
        print(f"  解析失败：{err}")
    return items


def build_items_from_hot(count: int, cookies: dict[str, str]) -> list[dict[str, Any]]:
    """用真实浏览器采集抖音热点视频（优先路径）。"""
    from clawdata.collection.browser_collect import collect_hot  # 延迟导入，避免强依赖 playwright

    try:
        hot = collect_hot(count=count, headless=True, cookies=cookies,
                          on_progress=lambda m: print(f"[浏览器采集] {m}"))
    except Exception as exc:  # noqa: BLE001
        print(f"[浏览器采集] 失败：{exc}")
        return []
    items = []
    for h in hot:
        filename = f"{h.get('author') or 'dy'}_{h.get('aweme_id')}_hot.mp4"
        items.append({
            "title": f"[热点] {h.get('author')} | {h.get('title')[:30]}",
            "author": h.get("author", ""),
            "aweme_id": h.get("aweme_id", ""),
            "word": h.get("word", "热点"),
            "url": h["url"],
            "filename": filename,
            "referer": "https://www.douyin.com/",
        })
    return items


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抖音热门视频下载（人味化）")
    parser.add_argument("--keywords", type=int, default=5, help="处理前 N 个热词")
    parser.add_argument("--per-word", type=int, default=3, help="每个热词采集 M 条视频")
    parser.add_argument("--list-only", action="store_true", help="只打印视频清单，不下载")
    parser.add_argument("--hot-list-only", action="store_true", help="只刷新热榜事件快照，不采集/下载视频")
    parser.add_argument("--no-cookie", action="store_true", help="跳过 Cookie（供测试流程）")
    parser.add_argument("--links", metavar="FILE", help="从文件逐行读取视频 ID/分享链接并下载（优先于热词采集）")
    parser.add_argument("--category", default="", help="给这些下载标记一个类型/分类（写入 tag_category）")
    parser.add_argument("--logfile", default="", help="把运行输出追加写入该日志文件")
    parser.add_argument("--outdir", default=DOWNLOADS_DIR, help="下载目录")
    parser.add_argument("--quiet-start", default="00:00", help="深夜禁止窗口开始 HH:MM")
    parser.add_argument("--quiet-end", default="07:00", help="深夜禁止窗口结束 HH:MM")
    parser.add_argument("--delay-lo", type=float, default=3.0, help="条间延迟下限（秒）")
    parser.add_argument("--delay-hi", type=float, default=8.0, help="条间延迟上限（秒）")
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if args.logfile:
        start_tee(args.logfile)

    # 1. 热榜
    try:
        raw = fetch_hot_list()
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 抓取热榜失败：{exc}", file=sys.stderr)
        return 1
    hot_items = [normalize(r, i + 1) for i, r in enumerate(raw) if r.get("word")]
    save_hotlist_snapshot(datetime.now().strftime("%Y-%m-%d"), [dict(h.__dict__) for h in hot_items])
    words = [h.word for h in hot_items]
    print(f"[热榜] 获取 {len(words)} 条，将处理前 {args.keywords} 个热词")
    if args.hot_list_only:
        print("[热榜] 已按新策略只保留热点事件列表，不下载视频")
        return 0

    # 2. 登录 cookie
    cookies: dict[str, str] | None = None
    if not args.no_cookie:
        try:
            cookies = load_cookies()
            if has_session(cookies):
                print(f"[会话] 已加载登录 Cookie（{len(cookies)} 个）")
            else:
                print("[会话] 已加载 Cookie，但未检测到明确的会话字段，可能仍需登录")
        except FileNotFoundError as exc:
            print(f"[会话] {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[会话] Cookie 解析失败：{exc}")

    # 3. 采集
    if args.links:
        if not cookies:
            print("[链接模式] 未加载抖音 Cookie：抖音链接会解析失败，B 站链接不受影响")
        print(f"[链接模式] 从 {args.links} 读取视频 ID/链接")
        items = build_items_from_links(args.links, cookies)
    else:
        hot_count = max(args.per_word * 3, 10)
        # 1) 优先：真实浏览器采集热点视频（稳定）
        items = build_items_from_hot(hot_count, cookies or {})
        # 2) 自动检索热词（可能被 verify_check 拦）
        if not items:
            items = build_items(words, args.keywords, args.per_word, cookies)
        # 3) 兜底：data/links/links.txt
        if not items and os.path.exists(DEFAULT_LINKS_PATH):
            print("[回退] 改用 data/links/links.txt")
            if not cookies:
                print("  跳过（未提供登录 Cookie）")
            else:
                items = build_items_from_links(DEFAULT_LINKS_PATH, cookies)
    print(f"[采集] 共收集 {len(items)} 个视频下载地址")
    for it in items:
        if args.category:
            it["category"] = args.category
        print(f"  - {it['title']}")

    if args.list_only or not items:
        return 0

    # 4. 人味化下载
    cfg = DownloadConfig(
        outdir=args.outdir,
        delay_range=(args.delay_lo, args.delay_hi),
        quiet=QuietHours(parse_time(args.quiet_start), parse_time(args.quiet_end)),
        cookies=cookies or {},
        store=DEFAULT_DB_PATH,
        category=args.category,
    )
    download_items(items, cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
