"""
指定账号视频采集：用登录态 Cookie 调抖音「用户作品列表」接口。

接口：GET /aweme/v1/web/aweme/post/
需要：登录 cookie + a_bogus 签名 + sec_user_id。

用法：
    from clawdata.collection.account_videos import fetch_account_videos, resolve_sec_uid

    sec_uid = resolve_sec_uid("https://www.douyin.com/user/MS4wLjABAAAA...")
    vids = fetch_account_videos(sec_uid, cookies, count=20)

说明：
- resolve_sec_uid 支持三种输入：主页 URL、裸 sec_user_id、分享口令/短链。
  - 主页 URL  形如 https://www.douyin.com/user/<sec_uid>?...
  - 分享口令  形如 https://v.douyin.com/xxxx/ （先跟随跳转拿到主页 URL）
- 若只给数字 uid，会尝试先取用户详情拿到 sec_uid。
"""

from __future__ import annotations

import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from clawdata.core.session import cookie_header


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
POST_URL = "https://www.douyin.com/aweme/v1/web/aweme/post/"
USER_DETAIL_URL = "https://www.douyin.com/aweme/v1/web/user/profile/other/"


def _http_json(url: str, headers: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"接口 HTTP {exc.code}，可能需要新的 cookie 或签名") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"返回不是合法 JSON: {body[:120]}") from exc


def _signed_url(base: str, params: dict[str, str]) -> str:
    """生成 query、签 a_bogus（签名失败则回退无签名，可能被 verify_check 拦）。"""
    param_str = "&".join(
        [f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in params.items()]
    )
    try:
        from clawdata.signing.sign import make_abogus

        param_str = make_abogus(param_str, UA)
    except Exception:  # noqa: BLE001
        pass
    return base + "?" + param_str


def resolve_sec_uid(account: str, cookies: dict[str, str], timeout: float = 15.0) -> str:
    """
    把用户给的「账号」解析成 sec_user_id。

    支持：
    - https://www.douyin.com/user/MS4wLjABAAAA...（主页 URL）
    - https://v.douyin.com/xxxx/（分享口令，自动跟随跳转）
    - 直接的 sec_user_id 字符串（以 MS4w 开头的 base64）
    - 数字 uid（先调用户详情接口换 sec_user_id）
    """
    account = (account or "").strip()
    if not account:
        raise ValueError("账号不能为空")

    # 分享短链：跟随跳转拿到真实主页 URL
    if re.match(r"https?://v\.douyin\.com/", account):
        furl = _resolve_short_url(account, cookies, timeout)
        account = furl

    # 主页 URL：取路径里的 user/<sec_uid>，或 query 里的 sec_uid
    if account.startswith("http"):
        parsed = urllib.parse.urlparse(account)
        m = re.search(r"/user/([^/?#]+)", parsed.path)
        if m:
            return urllib.parse.unquote(m.group(1))
        qs = urllib.parse.parse_qs(parsed.query)
        if qs.get("sec_uid"):
            return qs["sec_uid"][0]
        if qs.get("sec_user_id"):
            return qs["sec_user_id"][0]

    # 裸字符串：MS4w 开头大概率就是 sec_user_id
    if account.startswith("MS4w"):
        return account

    # 纯数字 uid：取用户详情换 sec_user_id
    if account.isdigit():
        info = _user_detail(account, cookies, timeout)
        sec_uid = (info.get("user") or {}).get("sec_uid", "")
        if sec_uid:
            return sec_uid

    raise ValueError(
        f"无法从「{account[:40]}」解析出 sec_user_id。请粘贴用户主页链接，"
        "例如 https://www.douyin.com/user/MS4wLjABAAAA..."
    )


def _resolve_short_url(url: str, cookies: dict[str, str], timeout: float) -> str:
    headers = {
        "User-Agent": UA,
        "Cookie": cookie_header(cookies),
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final = resp.geturl()
    except urllib.error.HTTPError as exc:
        if exc.code in (301, 302, 303, 307, 308):
            return exc.headers.get("Location", url)
        raise RuntimeError(f"短链解析 HTTP {exc.code}") from exc
    return final or url


def _user_detail(uid: str, cookies: dict[str, str], timeout: float) -> dict[str, Any]:
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
        "sec_user_id": uid,
    }
    url = _signed_url(USER_DETAIL_URL, params)
    headers = {
        "User-Agent": UA,
        "Cookie": cookie_header(cookies),
        "Referer": "https://www.douyin.com/",
        "Accept": "application/json, text/plain, */*",
    }
    obj = _http_json(url, headers, timeout)
    if obj.get("status_code") not in (0, None):
        raise RuntimeError(f"用户详情 status_code={obj.get('status_code')} msg={obj.get('status_msg')}")
    return obj.get("user") or obj.get("data") or {}


def _post_params(sec_uid: str, cursor: int, count: int) -> dict[str, str]:
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
        "sec_user_id": sec_uid,
        "max_cursor": str(cursor),
        "count": str(min(count, 20)),
        "publish_video_strategy_type": "2",
    }


def iter_account_video_pages(
    sec_uid: str,
    cookies: dict[str, str],
    per_page: int = 20,
    max_pages: int = 20,
    timeout: float = 15.0,
    page_delay: tuple[float, float] = (1.5, 3.5),
) -> Iterator[list[dict[str, Any]]]:
    """逐页拉取账号作品列表（cursor 翻页，按发布时间倒序）。

    每页 yield 解析后的条目列表（同 _parse_aweme 结构，去重）；取下一页前
    随机停顿 page_delay 秒，模拟人类翻页。has_more=0、cursor 不再前进或达到
    max_pages 时停止。调用方 break 后生成器即被丢弃，不会再发请求。
    """
    cursor = 0
    seen: set[str] = set()
    for _page in range(max_pages):
        url = _signed_url(POST_URL, _post_params(sec_uid, cursor, per_page))
        headers = {
            "User-Agent": UA,
            "Cookie": cookie_header(cookies),
            "Referer": f"https://www.douyin.com/user/{sec_uid}",
            "Accept": "application/json, text/plain, */*",
        }
        obj = _http_json(url, headers, timeout)
        status = obj.get("status_code")
        if status not in (0, None):
            msg = obj.get("status_msg", "")
            raise RuntimeError(f"作品列表 status_code={status} msg={msg}（{str(obj)[:120]}）")

        data = obj.get("data", {}) or {}
        aweme_list = data.get("aweme_list", []) or []
        items: list[dict[str, Any]] = []
        for aweme in aweme_list:
            parsed = _parse_aweme(aweme, sec_uid)
            aweme_id = str(parsed.get("aweme_id") or "")
            if not parsed.get("url") or not aweme_id or aweme_id in seen:
                continue
            seen.add(aweme_id)
            items.append(parsed)
        yield items

        has_more = data.get("has_more", 0)
        next_cursor = data.get("max_cursor", 0)
        if not has_more or not next_cursor or next_cursor == cursor:
            return
        cursor = next_cursor
        time.sleep(random.uniform(*page_delay))


def fetch_account_videos(
    sec_uid: str,
    cookies: dict[str, str],
    count: int = 10,
    timeout: float = 15.0,
    max_pages: int = 20,
) -> list[dict[str, Any]]:
    """拉取某个账号的作品列表，返回解析后的视频条目（按发布时间倒序）。"""
    out: list[dict[str, Any]] = []
    per_page = max(1, min(count, 20))
    pages = max(1, min(max_pages, -(-max(count, 1) // per_page)))
    for page in iter_account_video_pages(
        sec_uid, cookies, per_page=per_page, max_pages=pages, timeout=timeout
    ):
        out.extend(page)
        if len(out) >= count:
            break
    return out[:count]


def _parse_aweme(item: dict[str, Any], sec_uid: str) -> dict[str, Any]:
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
        "sec_uid": str(author.get("sec_uid", "") or sec_uid),
        "account": str(author.get("nickname", "") or author.get("sec_uid", "") or sec_uid),
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
