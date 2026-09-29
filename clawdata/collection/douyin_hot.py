"""
抖音热榜抓取工具（零第三方依赖，仅用标准库）

数据源：抖音网页版热榜公开接口
    GET https://www.douyin.com/aweme/v1/web/hot/search/list/

该接口当前无需登录/签名即可访问，返回 51 条抖音热榜（热搜话题）。
注意：这是"热门话题/热搜榜"，不是单条视频的详细信息。
要拿每个热词背后的具体视频，需要登录态 + a_bogus/X-Bogus 签名，
见 README 中的"扩展路径"说明。

用法：
    python -m clawdata.collection.douyin_hot                 # 抓取并写入 JSON + CSV
    python -m clawdata.collection.douyin_hot --limit 20      # 只保留前 20 条
    python -m clawdata.collection.douyin_hot --outdir out    # 自定义输出目录
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Any

from clawdata.core.paths import OUTPUT_DIR


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
HOT_LIST_URL = "https://www.douyin.com/aweme/v1/web/hot/search/list/"


@dataclass
class HotItem:
    """单条抖音热榜条目（规范化后的字段）。"""

    rank: int
    word: str
    hot_value: int
    view_count: int
    discuss_video_count: int
    label: str
    is_new: bool
    sentence_id: str
    url: str


def _build_url() -> str:
    params = {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "pc_client_type": "1",
        "version_code": "190500",
        "version_name": "19.5.0",
        "cookie_enabled": "true",
        "screen_width": "1920",
        "screen_height": "1080",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "129.0.0.0",
        "platform": "PC",
    }
    return HOT_LIST_URL + "?" + urllib.parse.urlencode(params)


def _headers() -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.douyin.com/hot",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }


def fetch_hot_list(timeout: float = 15.0) -> list[dict[str, Any]]:
    """请求热榜接口，返回原始 word_list。"""
    req = urllib.request.Request(_build_url(), headers=_headers())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "ignore")
    obj = json.loads(body)
    if obj.get("status_code") != 0:
        raise RuntimeError(f"接口异常 status_code={obj.get('status_code')} msg={obj.get('status_msg')}")
    words = obj.get("data", {}).get("word_list", []) or obj.get("word_list", [])
    return words


def normalize(raw: dict[str, Any], rank: int) -> HotItem:
    """把接口原始字段规范化成 HotItem。"""
    word = raw.get("word", "")
    return HotItem(
        rank=rank,
        word=word,
        hot_value=int(raw.get("hot_value", 0) or 0),
        view_count=int(raw.get("view_count", 0) or 0),
        discuss_video_count=int(raw.get("discuss_video_count", 0) or 0),
        label=str(raw.get("label", "") or ""),
        is_new=bool(raw.get("is_n1", False)),
        sentence_id=str(raw.get("sentence_id", "")),
        url="https://www.douyin.com/hot/" + urllib.parse.quote(word),
    )


def to_records(items: list[HotItem]) -> list[dict[str, Any]]:
    return [asdict(item) for item in items]


def write_json(records: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)


def write_csv(records: list[dict[str, Any]], path: str) -> None:
    """写 CSV，使用 utf-8-sig，Excel 可直接打开不乱码。"""
    fieldnames = list(records[0].keys()) if records else []
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(records)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抓取抖音热榜")
    parser.add_argument("--limit", type=int, default=0, help="只保留前 N 条，0 表示全部")
    parser.add_argument("--outdir", default=OUTPUT_DIR, help="输出目录")
    parser.add_argument("--json", action="store_true", help="额外输出 JSON 文件")
    args = parser.parse_args(argv)

    try:
        raw = fetch_hot_list()
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] 抓取失败: {exc}", file=sys.stderr)
        return 1

    items = [normalize(row, i + 1) for i, row in enumerate(raw)]
    if args.limit > 0:
        items = items[: args.limit]
    records = to_records(items)

    os.makedirs(args.outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(args.outdir, f"douyin_hot_{ts}.csv")
    write_csv(records, csv_path)

    printed = [csv_path]
    if args.json:
        json_path = os.path.join(args.outdir, f"douyin_hot_{ts}.json")
        write_json(records, json_path)
        printed.append(json_path)

    print(f"[完成] 共 {len(records)} 条热榜，已写入:")
    for p in printed:
        print("  " + os.path.abspath(p))
    print("\n前 10 条预览：")
    for r in records[:10]:
        print(f"  #{r['rank']:>2}  {r['word']}  热度={r['hot_value']:,}  播放={r['view_count']:,}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
