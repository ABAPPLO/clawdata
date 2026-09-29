"""
按指定账号采集抖音视频（复用登录 Cookie + a_bogus 签名 + 人味化下载）。

示例：
    python collect_account.py --account "https://www.douyin.com/user/MS4wLjABAAAA..."
    python collect_account.py --account "MS4wLjABAAAA..." --count 20
    python collect_account.py --account "https://v.douyin.com/xxxx/" --list-only
    python collect_account.py --account "https://www.douyin.com/user/xxx" --count 10 --category 武术账号

账号可以是：用户主页链接、分享短链、或直接的 sec_user_id（MS4w 开头）。
会调用 /aweme/v1/web/aweme/post/ 拉取该账号的作品列表并逐个下载。
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import time as dtime

from clawdata.collection.account_videos import fetch_account_videos, resolve_sec_uid
from clawdata.download.downloader import DownloadConfig, download_items
from clawdata.core.humanize import QuietHours
from clawdata.core.session import load_cookies, has_session
from clawdata.core.paths import DEFAULT_DB_PATH, DOWNLOADS_DIR


def parse_time(s: str) -> dtime:
    hh, mm = s.split(":")
    return dtime(int(hh), int(mm))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="按指定账号采集抖音视频")
    parser.add_argument("--account", required=True, help="账号主页链接 / 分享短链 / sec_user_id")
    parser.add_argument("--count", type=int, default=10, help="采集条数")
    parser.add_argument("--category", default="指定账号", help="记录类型/分类（tag_category）")
    parser.add_argument("--list-only", action="store_true", help="只列不下载")
    parser.add_argument("--mode", choices=["auto", "browser", "api"], default="auto",
                        help="采集方式：browser=真实浏览器（稳），api=直连接口，auto=先浏览器后接口")
    parser.add_argument("--headless", action="store_true", help="浏览器采集以无头模式优先")
    parser.add_argument("--outdir", default=DOWNLOADS_DIR, help="下载目录")
    parser.add_argument("--delay-lo", type=float, default=3.0)
    parser.add_argument("--delay-hi", type=float, default=8.0)
    parser.add_argument("--quiet-start", default="00:00")
    parser.add_argument("--quiet-end", default="07:00")
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    ck = load_cookies()
    if not has_session(ck):
        print("[提示] Cookie 可能缺少会话字段，若接口返回请先登录请更新 config/cookies.json")

    # 1. 解析出 sec_user_id
    try:
        sec_uid = resolve_sec_uid(args.account, ck)
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 无法解析账号：{exc}", file=sys.stderr)
        return 1
    print(f"[账号] sec_user_id = {sec_uid}")

    # 2. 拉取作品列表（浏览器/接口）
    vids: list = []
    if args.mode in ("auto", "browser"):
        from clawdata.collection.browser_collect import collect_account_videos  # 延迟导入，避免强依赖 playwright

        try:
            vids = collect_account_videos(
                sec_uid,
                count=args.count,
                headless=args.headless,
                cookies=ck,
                on_progress=lambda m: print(f"[浏览器] {m}"),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[浏览器] 采集失败：{exc}")
            vids = []

    if not vids and args.mode in ("auto", "api"):
        try:
            print("[接口] 回退直连 /aweme/post/ 拉取…")
            vids = fetch_account_videos(sec_uid, ck, count=args.count)
        except Exception as exc:  # noqa: BLE001
            print(f"[接口] 拉取失败：{exc}", file=sys.stderr)

    if not vids:
        print("[采集] 未采集到视频（可能账号为空/未公开/风控，或用浏览器手动过验证后重试）")
        return 1

    # 3. 组待下载列表
    items = []
    for v in vids:
        account = v.get("account") or v.get("author") or sec_uid
        desc = (v.get("title") or v.get("aweme_id") or "video")[:30]
        items.append({
            "title": f"[{args.category}] {account} | {desc}",
            "author": v.get("author", ""),
            "aweme_id": v.get("aweme_id", ""),
            "account": account,
            "word": f"account:{account}",
            "category": args.category,
            "url": v["url"],
            "filename": f"{account}_{v.get('aweme_id')}.mp4",
            "referer": f"https://www.douyin.com/user/{sec_uid}",
        })
    print(f"[采集] {account} 共 {len(items)} 条")
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
