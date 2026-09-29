"""
抖音视频采集：用登录态 cookie 调搜索接口，解析出视频下载地址。

注意：
- 需要登录态 cookie（见 clawdata/core/session.py / config/cookies.example.json）。
- 部分接口可能需要 a_bogus/X-Bogus 签名；若返回签名为 0 或风控码，
  会在 status_msg 里体现，本模块会把原始状态打印出来便于排查。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from clawdata.core.session import cookie_header


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
SEARCH_URL = "https://www.douyin.com/aweme/v1/web/general/search/single/"


def _build_params(keyword: str, cursor: int = 0, count: int = 10, search_id: str | None = None) -> dict[str, str]:
    return {
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
        "search_channel": "aweme_general",
        "search_source": "normal_search",
        "keyword": keyword,
        "search_id": search_id or str(int(time.time() * 1000)),
        "cursor": str(cursor),
        "count": str(count),
    }


def search_videos(
    keyword: str,
    cookies: dict[str, str],
    count: int = 10,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    """按关键词搜索，返回解析后的视频条目列表。"""
    params = _build_params(keyword, 0, count)
    param_str = "&".join(
        [f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items()]
    )
    try:
        from clawdata.signing.sign import make_abogus

        param_str = make_abogus(param_str, UA)
    except Exception:  # noqa: BLE001
        # 签名失败则回退到无签名（可能被 verify_check 拦）
        pass
    url = SEARCH_URL + "?" + param_str
    headers = {
        "User-Agent": UA,
        "Cookie": cookie_header(cookies),
        "Referer": "https://www.douyin.com/search/" + urllib.parse.quote(keyword),
        "Accept": "application/json, text/plain, */*",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"搜索接口 HTTP {exc.code}，可能需要新的 cookie 或签名") from exc

    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"返回不是合法 JSON: {body[:120]}") from exc

    status = obj.get("status_code")
    msg = obj.get("status_msg")
    if status != 0:
        raise RuntimeError(f"搜索接口 status_code={status} msg={msg}（{body[:120]}）")

    data = obj.get("data", {}) or {}
    aweme_list = data.get("aweme_list", []) or []
    return [_parse_aweme(a) for a in aweme_list]


def _parse_aweme(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author", {}) or {}
    stats = item.get("statistics", {}) or {}
    video = item.get("video", {}) or {}

    play_url = _first_key(video, ["download_addr", "play_addr", "play_addr_h264"])
    cover = _first_key(video, ["cover", "origin_cover", "dynamic_cover"])

    return {
        "title": item.get("desc", ""),
        "aweme_id": str(item.get("aweme_id", "")),
        "author": author.get("nickname", ""),
        "author_id": str(author.get("uid", "")),
        "create_time": int(item.get("create_time", 0) or 0),
        "url": play_url,
        "cover": cover,
        "digg_count": int(stats.get("digg_count", 0) or 0),
        "comment_count": int(stats.get("comment_count", 0) or 0),
        "play_count": int(stats.get("play_count", 0) or 0),
        "share_count": int(stats.get("share_count", 0) or 0),
    }


def _first_key(video: dict[str, Any], keys: list[str]) -> str:
    for k in keys:
        node = video.get(k)
        if isinstance(node, dict):
            urls = node.get("url_list") or node.get("url")
            if isinstance(urls, list) and urls:
                return str(urls[0])
            if isinstance(urls, str) and urls:
                return urls
    return ""
