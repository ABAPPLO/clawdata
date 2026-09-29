"""
按指定主题/关键词采集抖音视频（无头浏览器优先，含验证回退）。

示例：
    python collect_theme.py --keyword 功夫
    python collect_theme.py --keyword "单人武术展示" --keyword 太极 --per-word 8
    python collect_theme.py --keywords-file themes.txt --list-only
    python collect_theme.py --keyword 武术 --category 武术合集 --per-word 5

`--keywords-file` 每行一个关键词/主题，`#` 开头为注释。
每个词会作为一个类型（tag_category）写入记录，方便面板按主题分组。
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import time as dtime

from clawdata.download.downloader import DownloadConfig, download_items
from clawdata.core.humanize import QuietHours
from clawdata.core.session import load_cookies, has_session
from clawdata.core.paths import DEFAULT_DB_PATH, DOWNLOADS_DIR


def parse_time(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def read_keywords(args) -> list[str]:
    kws = list(args.keyword)
    if args.keywords_file:
        with open(args.keywords_file, encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        kws.extend(lines)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for k in kws:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


def collect_from_browser(keyword: str, count: int, ck, headless: bool, probe: float, max_wait: float) -> list[dict]:
    from clawdata.collection.browser_collect import search_videos_auto  # 延迟导入

    return search_videos_auto(
        keyword, count=count, cookies=ck, headless=headless,
        probe=probe, max_wait=max_wait,
        on_progress=lambda m: print(f"[{keyword}] {m}"),
    )


def collect_from_api(keyword: str, count: int, ck) -> list[dict]:
    from clawdata.collection.douyin_videos import search_videos  # 延迟导入

    vids = search_videos(keyword, ck, count=count * 2, timeout=15)
    items = []
    for v in vids:
        if not v.get("url"):
            continue
        items.append({
            "aweme_id": v.get("aweme_id", ""),
            "title": v.get("title", ""),
            "author": v.get("author", ""),
            "url": v["url"],
            "word": keyword,
        })
    return items[:count]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按主题/关键词采集抖音视频")
    parser.add_argument("--keyword", action="append", default=[], help="关键词/主题（可重复）")
    parser.add_argument("--keywords-file", default="", help="从文件读取关键词，每行一个")
    parser.add_argument("--per-word", type=int, default=5, help="每个词采集条数")
    parser.add_argument("--category", default="", help="记录类型覆盖（默认用关键词本身）")
    parser.add_argument("--list-only", action="store_true", help="只列不下载")
    parser.add_argument("--mode", choices=["auto", "browser", "api"], default="auto",
                        help="采集方式：browser=浏览器（稳），api=直连，auto=先浏览器后接口")
    parser.add_argument("--headless", action="store_true", help="浏览器采集以无头模式优先")
    parser.add_argument("--probe", type=float, default=25.0, help="无头浏览器尝试时长（秒）")
    parser.add_argument("--max-wait", type=float, default=300.0,
                        help="回退到有头窗口后等待手动过验证的最长时间（秒）")
    parser.add_argument("--outdir", default=DOWNLOADS_DIR, help="下载目录")
    parser.add_argument("--delay-lo", type=float, default=3.0)
    parser.add_argument("--delay-hi", type=float, default=8.0)
    parser.add_argument("--quiet-start", default="00:00")
    parser.add_argument("--quiet-end", default="07:00")
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ck = load_cookies()
    if not has_session(ck):
        print("[提示] Cookie 可能缺少会话字段，若搜索页跳登录请更新 config/cookies.json")

    keywords = read_keywords(args)
    if not keywords:
        print("[错误] 请用 --keyword 或 --keywords-file 指定主题/关键词", file=sys.stderr)
        return 1
    print(f"[主题] 共 {len(keywords)} 个：{', '.join(keywords)}")

    items: list[dict] = []
    for kw in keywords:
        cat = args.category or kw
        got = []
        if args.mode in ("auto", "browser"):
            got = collect_from_browser(kw, args.per_word, ck, args.headless, args.probe, args.max_wait)
        if not got and args.mode in ("auto", "api"):
            print(f"[接口] 回退直连搜索「{kw}」…")
            try:
                got = collect_from_api(kw, args.per_word, ck)
            except Exception as exc:  # noqa: BLE001
                print(f"[接口] 「{kw}」失败：{exc}", file=sys.stderr)
        if not got:
            print(f"[主题] 「{kw}」未采集到视频")
            continue
        for v in got:
            author = v.get("author", "") or "dy"
            desc = (v.get("title") or v.get("aweme_id") or "video")[:30]
            items.append({
                "title": f"[{cat}] {author} | {desc}",
                "author": author,
                "aweme_id": v.get("aweme_id", ""),
                "word": kw,
                "category": cat,
                "url": v["url"],
                "filename": f"{author}_{v.get('aweme_id')}.mp4",
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
