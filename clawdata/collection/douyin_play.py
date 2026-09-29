"""
按 aweme_id / 分享链接解析抖音视频的真实下载地址。

背景：抖音的"搜索"接口会被风控要求验证（verify_check），但从这个环境实测
`aweme/detail` 接口用登录 cookie 可直接返回完整视频详情，因此拿到视频 ID
即可解析出下载地址。本模块负责：
- 从分享链接 / 纯 ID 中提取 aweme_id
- 调 aweme/detail 拿最佳视频下载地址
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from typing import Any

from clawdata.core.session import cookie_header


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
DETAIL_URL = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
_ID_RE = re.compile(r"\b(\d{15,20})\b")


def extract_aweme_id(text: str) -> str:
    """从分享链接或纯数字 ID 中提取 aweme_id。"""
    m = _ID_RE.search(text)
    if not m:
        raise ValueError(f"未从输入中识别到视频 ID：{text[:60]}")
    return m.group(1)


def _detail(aweme_id: str, cookies: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
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
        "aweme_id": aweme_id,
    }
    url = DETAIL_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Cookie": cookie_header(cookies),
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "ignore")
    obj = json.loads(body)
    if obj.get("status_code") != 0:
        raise RuntimeError(f"详情接口 status_code={obj.get('status_code')} msg={obj.get('status_msg')}")
    detail = obj.get("aweme_detail") or {}
    if not detail:
        raise RuntimeError(f"未取到视频详情，ID 可能无效：{aweme_id}")
    return detail


def _pick_url(detail: dict[str, Any]) -> str:
    """优先原视频下载地址，其次视频播放地址（挑选最好码率）。"""
    video = detail.get("video", {}) or {}
    # 1) download_addr
    for node in [video.get("download_addr"), video.get("download_suffix_logo_addr")]:
        if isinstance(node, dict):
            urls = node.get("url_list")
            if urls:
                return str(urls[0])
    # 2) bit_rate 里最高码率的 play_addr
    bit_rates = video.get("bit_rate") or []
    if bit_rates:
        best = max(bit_rates, key=lambda b: int(b.get("bit_rate", 0) or 0) if isinstance(b, dict) else 0)
        if isinstance(best, dict):
            urls = (best.get("play_addr") or {}).get("url_list")
            if urls:
                return str(urls[0])
    # 3) play_addr
    play = video.get("play_addr")
    if isinstance(play, dict):
        urls = play.get("url_list")
        if urls:
            return str(urls[0])
    return ""


def resolve(aweme_id_or_link: str, cookies: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    """解析一个视频，返回含下载地址的字典。"""
    id_ = extract_aweme_id(aweme_id_or_link)
    detail = _detail(id_, cookies, timeout)
    author = detail.get("author", {}) or {}
    stats = detail.get("statistics", {}) or {}
    url = _pick_url(detail)
    if not url:
        raise RuntimeError("未解析到视频下载地址（可能被加密/已失效）")
    return {
        "title": detail.get("desc", ""),
        "aweme_id": id_,
        "author": author.get("nickname", ""),
        "create_time": int(detail.get("create_time", 0) or 0),
        "url": url,
        "digg_count": int(stats.get("digg_count", 0) or 0),
        "comment_count": int(stats.get("comment_count", 0) or 0),
        "play_count": int(stats.get("play_count", 0) or 0),
    }
