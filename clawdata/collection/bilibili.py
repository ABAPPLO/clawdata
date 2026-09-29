"""
Bilibili 视频解析：从视频链接/短链/BV号 拿到可下载的流地址。

- 支持的输入：BV号、av号、www.bilibili.com/video/... 链接、b23.tv 短链
- 元数据走公开的 web-interface/view 接口，播放地址走 player/playurl（DASH）
- B 站 DASH 流是音视频分离的，resolve() 会同时返回 video_url 和 audio_url，
  由下载器分别下载后用 ffmpeg 合流
- 匿名可拿 360P/480P；把登录 Cookie 存到 config/cookies_bilibili.json
  （{"raw": "SESSDATA=...; buvid3=..."}）可提升到账号允许的清晰度
- 只解析公开可看内容；充电专属/付费视频接口不会返回流地址，属预期边界

用法：
    from clawdata.collection import bilibili
    item = bilibili.resolve("https://b23.tv/xxxx")
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.parse
import urllib.request
from typing import Any

from clawdata.core.paths import BILIBILI_COOKIES_PATH
from clawdata.core.session import cookie_header, load_cookies


UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)
REFERER = "https://www.bilibili.com/"
VIEW_URL = "https://api.bilibili.com/x/web-interface/view"
PLAY_URL = "https://api.bilibili.com/x/player/playurl"
POPULAR_URL = "https://api.bilibili.com/x/web-interface/popular"
SEARCH_URL = "https://api.bilibili.com/x/web-interface/wbi/search/type"
SPACE_ARC_URL = "https://api.bilibili.com/x/space/wbi/arc/search"
NAV_URL = "https://api.bilibili.com/x/web-interface/nav"

# wbi 混淆密钥的取值下标（B站 web 端公开常量）
_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27,
              43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48,
              7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54,
              21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]
_wbi_cache: dict[str, Any] = {"key": "", "ts": 0.0}

_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"\bav(\d{6,})\b", re.IGNORECASE)
_BILI_HOSTS = ("bilibili.com", "b23.tv", "biliwanyi", "biligame", "bigfun")

# 清晰度 id：16=360P 32=480P 64=720P 74=720P60 80=1080P 112=1080P+ 116=1080P60
_QN_IDS = (16, 32, 64, 74, 80, 112, 116, 120)


def is_bilibili_link(text: str) -> bool:
    """判断一行输入是否是 B 站链接/BV号/av号。"""
    text = (text or "").strip()
    if not text:
        return False
    if _BV_RE.search(text):
        return True
    try:
        host = urllib.parse.urlparse(text if "://" in text else "http://" + text).hostname or ""
    except ValueError:
        return False
    return any(h == host or host.endswith("." + h) for h in ("bilibili.com", "b23.tv"))


def load_bilibili_cookies() -> dict[str, str]:
    """可选的登录 Cookie；文件不存在时返回空（匿名模式）。"""
    try:
        return load_cookies(BILIBILI_COOKIES_PATH)
    except (FileNotFoundError, ValueError):
        return {}


def _api_get(url: str, cookies: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    headers = {
        "User-Agent": UA,
        "Referer": REFERER,
        "Accept": "application/json, text/plain, */*",
    }
    if cookies:
        headers["Cookie"] = cookie_header(cookies)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", "ignore")
    obj = json.loads(body)
    if obj.get("code") != 0:
        raise RuntimeError(f"B站接口 code={obj.get('code')} msg={obj.get('message')}")
    return obj.get("data") or {}


def _final_url(url: str, timeout: float = 10.0) -> str:
    """跟随重定向（b23.tv 短链），返回最终 URL。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.geturl() or url


def extract_bvid(text: str) -> str:
    """从任意输入提取 BV 号；av 号会自动换算。"""
    m = _BV_RE.search(text)
    if m:
        return m.group(1)
    if "b23.tv" in text or ("bilibili.com" in text and "/video/" not in text):
        text = _final_url(text.strip().split()[0])
        m = _BV_RE.search(text)
        if m:
            return m.group(1)
    m = _AV_RE.search(text)
    if m:
        data = _api_get(VIEW_URL + "?" + urllib.parse.urlencode({"aid": m.group(1)}), {})
        bvid = str(data.get("bvid", ""))
        if bvid:
            return bvid
    raise ValueError(f"未从输入中识别到 B 站视频 ID：{text[:60]}")


def _normalize_stream_url(url: str) -> str:
    # 接口返回的 upos 地址多为 http，改 https 可直接访问；base64 变体同样处理
    return url.replace("http://", "https://", 1)


def fetch_video_info(bvid: str, cookies: dict[str, str] | None = None) -> dict[str, Any]:
    data = _api_get(VIEW_URL + "?" + urllib.parse.urlencode({"bvid": bvid}), cookies or {})
    cid = data.get("cid")
    if not cid:
        pages = data.get("pages") or []
        cid = pages[0].get("cid") if pages else None
    if not cid:
        raise RuntimeError(f"未取到视频 cid，可能不是普通视频：{bvid}")
    owner = data.get("owner") or {}
    stat = data.get("stat") or {}
    return {
        "bvid": bvid,
        "aid": str(data.get("aid", "")),
        "cid": str(cid),
        "title": str(data.get("title", "")),
        "desc": str(data.get("desc", "")),
        "author": str(owner.get("name", "")),
        "owner_mid": str(owner.get("mid", "")),
        "pubdate": int(data.get("pubdate", 0) or 0),
        "duration": int(data.get("duration", 0) or 0),
        "pic": str(data.get("pic", "")),
        "view_count": int(stat.get("view", 0) or 0),
    }


def fetch_play_urls(bvid: str, cid: str, cookies: dict[str, str] | None = None) -> tuple[str, str]:
    """返回 (video_url, audio_url)。DASH 不可用时回退 MP4 单流（audio_url 为空串）。"""
    cookies = cookies or {}
    params = {"bvid": bvid, "cid": cid, "qn": "80", "fnval": "16", "fourk": "1"}
    data = _api_get(PLAY_URL + "?" + urllib.parse.urlencode(params), cookies)

    dash = data.get("dash") or {}
    videos = [v for v in (dash.get("video") or []) if v.get("baseUrl") or v.get("base_url")]
    audios = [a for a in (dash.get("audio") or []) if a.get("baseUrl") or a.get("base_url")]
    if videos:
        video = max(videos, key=lambda v: (_QN_IDS.index(v["id"]) if v.get("id") in _QN_IDS else -1,
                                           int(v.get("bandwidth", 0) or 0)))
        audio = max(audios, key=lambda a: int(a.get("bandwidth", 0) or 0)) if audios else None
        video_url = _normalize_stream_url(str(video.get("baseUrl") or video.get("base_url")))
        audio_url = _normalize_stream_url(str(audio.get("baseUrl") or audio.get("base_url"))) if audio else ""
        return video_url, audio_url

    # 回退：MP4/FLV durl 单文件（自带音轨）
    durl = data.get("durl") or []
    if durl:
        return _normalize_stream_url(str(durl[0].get("url", ""))), ""
    raise RuntimeError(f"未解析到播放流（可能是付费/充电专属或地区受限）：{bvid}")


def resolve(link: str, cookies: dict[str, str] | None = None, timeout: float = 15.0) -> dict[str, Any]:
    """解析一个 B 站视频，返回与抖音 resolve 同构的下载信息字典。"""
    bvid = extract_bvid(link)
    if cookies is None:
        cookies = load_bilibili_cookies()
    info = fetch_video_info(bvid, cookies)
    video_url, audio_url = fetch_play_urls(bvid, info["cid"], cookies)
    if not video_url:
        raise RuntimeError(f"未解析到视频下载地址：{bvid}")
    return {
        "platform": "bilibili",
        "title": info["title"],
        "aweme_id": bvid,  # 复用下载记录的视频 ID 字段
        "author": info["author"],
        "create_time": 0,
        "url": video_url,
        "audio_url": audio_url,
        "referer": REFERER,
        "view_count": info["view_count"],
    }


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if not target:
        print("用法: python -m clawdata.collection.bilibili <视频链接|BV号|av号>")
        raise SystemExit(1)
    result = resolve(target)
    for key in ("platform", "title", "aweme_id", "author", "url", "audio_url"):
        print(f"{key}: {result[key]}")


# ---------------- wbi 签名与发现类接口（热门/搜索/UP主作品） ----------------

def _mixin_key(cookies: dict[str, str]) -> str:
    """取 wbi 混淆密钥（来自 nav 接口，缓存 30 分钟）。"""
    now = time.time()
    if _wbi_cache["key"] and now - _wbi_cache["ts"] < 1800:
        return str(_wbi_cache["key"])
    data = _api_get(NAV_URL, cookies)
    wbi = data.get("wbi_img") or {}
    img = str(wbi.get("img_url", "")).rsplit("/", 1)[-1].split(".")[0]
    sub = str(wbi.get("sub_url", "")).rsplit("/", 1)[-1].split(".")[0]
    raw = (img + sub)[:64]
    key = "".join(raw[i] for i in _MIXIN_TAB if i < len(raw))[:32]
    if not key:
        raise RuntimeError("未取到 wbi 密钥（nav 接口异常）")
    _wbi_cache.update(key=key, ts=now)
    return key


def _wbi_get(url: str, params: dict[str, Any], cookies: dict[str, str], timeout: float = 15.0) -> dict[str, Any]:
    """带 wbi 签名的 GET（search / space 接口需要）。"""
    params = {k: str(v) for k, v in params.items()}
    params["wts"] = str(int(time.time()))
    query = urllib.parse.urlencode(
        {k: re.sub(r"[!'()*]", "", v) for k, v in sorted(params.items())}
    )
    w_rid = hashlib.md5((query + _mixin_key(cookies)).encode()).hexdigest()
    return _api_get(f"{url}?{query}&w_rid={w_rid}", cookies, timeout)


def _strip_em(text: str) -> str:
    """搜索结果标题里的 <em class=\"keyword\"> 高亮标签去掉。"""
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


def fetch_popular(count: int = 20, cookies: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """B站热门榜（无需登录、无需签名）。返回 {bvid,title,author,mid,view} 列表。"""
    cookies = cookies or load_bilibili_cookies()
    data = _api_get(POPULAR_URL + "?" + urllib.parse.urlencode({"ps": min(count, 50)}), cookies)
    out = []
    for v in data.get("list", [])[:count]:
        out.append({
            "bvid": str(v.get("bvid", "")),
            "title": _strip_em(v.get("title", "")),
            "author": str((v.get("owner") or {}).get("name", "")),
            "mid": str((v.get("owner") or {}).get("mid", "")),
            "view": int((v.get("stat") or {}).get("view", 0) or 0),
        })
    return out


def search_videos(keyword: str, count: int = 10, cookies: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """按关键词搜索视频（wbi 签名）。返回 {bvid,title,author,mid,view} 列表。"""
    if not keyword.strip():
        return []
    cookies = cookies or load_bilibili_cookies()
    out: list[dict[str, Any]] = []
    page = 1
    while len(out) < count and page <= 3:
        data = _wbi_get(SEARCH_URL, {
            "search_type": "video", "keyword": keyword, "page": page, "page_size": 20,
        }, cookies)
        results = data.get("result") or []
        if not results:
            break
        for v in results:
            if not v.get("bvid"):
                continue
            out.append({
                "bvid": str(v["bvid"]),
                "title": _strip_em(v.get("title", "")),
                "author": str(v.get("author", "")),
                "mid": str(v.get("mid", "")),
                "view": int(v.get("play", 0) or 0),
            })
            if len(out) >= count:
                break
        page += 1
        time.sleep(0.8)  # 翻页间隔
    return out[:count]


_SPACE_URL_RE = re.compile(r"space\.bilibili\.com/(?:#/)?(\d{3,})", re.IGNORECASE)


def is_space_url(text: str) -> bool:
    """是否是 B站 UP 主页链接（space.bilibili.com/<mid>）。"""
    return bool(_SPACE_URL_RE.search(text or ""))


def extract_mid(text: str) -> str:
    """从 UP 主页链接提取数字 mid。"""
    m = _SPACE_URL_RE.search(text or "")
    if not m:
        raise ValueError(f"未识别到 B站 UP 主页（形如 space.bilibili.com/12345）：{text[:50]}")
    return m.group(1)


def fetch_user_videos(mid: str, count: int = 30, cookies: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """拉取 UP 主最新作品（wbi 签名），按发布时间倒序。返回 {bvid,title,author,created} 列表。"""
    cookies = cookies or load_bilibili_cookies()
    params: dict[str, Any] = {
        "mid": mid, "ps": min(count, 30), "tid": 0, "pn": 1,
        "keyword": "", "order": "pubdate", "platform": "web", "web_location": "1550101",
        "order_avoided": "true",
    }
    data = _wbi_get(SPACE_ARC_URL, params, cookies)
    vlist = (data.get("list") or {}).get("vlist") or []
    out = []
    for v in vlist[:count]:
        out.append({
            "bvid": str(v.get("bvid", "")),
            "title": _strip_em(v.get("title", "")),
            "author": str(v.get("author", "")),
            "created": int(v.get("created", 0) or 0),
        })
    return out


# ------------------------------------------------------------ CC 字幕
PLAYER_V2_URL = "https://api.bilibili.com/x/player/wbi/v2"
_SUBTITLE_LANG_PRIORITY = ("zh-Hans", "zh-CN", "zh-hans", "ai-zh", "zh")


def fetch_subtitle(bvid: str, cid: str, cookies: dict[str, str] | None = None) -> str:
    """拉取视频 CC 字幕全文（json3 转纯文本）。

    优先人工字幕（zh-Hans/zh-CN），其次 AI 生成字幕；AI 字幕需要登录 Cookie。
    无字幕/未登录时返回空串，不抛错。
    """
    cookies = cookies or load_bilibili_cookies()
    try:
        data = _wbi_get(PLAYER_V2_URL, {"bvid": bvid, "cid": cid}, cookies)
    except Exception:  # noqa: BLE001 - 字幕属于增强信息，失败不影响主流程
        return ""
    subtitles = (data.get("subtitle") or {}).get("subtitles") or []
    if not subtitles:
        return ""
    chosen = None
    for lang in _SUBTITLE_LANG_PRIORITY:
        for s in subtitles:
            if str(s.get("lan", "")) == lang:
                chosen = s
                break
        if chosen:
            break
    if chosen is None:
        for s in subtitles:
            if "zh" in str(s.get("lan", "")):
                chosen = s
                break
    if chosen is None:
        chosen = subtitles[0]
    sub_url = str(chosen.get("subtitle_url", ""))
    if not sub_url:
        return ""
    if sub_url.startswith("//"):
        sub_url = "https:" + sub_url
    try:
        req = urllib.request.Request(sub_url, headers={"User-Agent": UA, "Referer": REFERER})
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except Exception:  # noqa: BLE001
        return ""
    lines = []
    for item in payload.get("body") or []:
        text = str(item.get("content", "")).strip()
        if text:
            lines.append(text)
    return _dedupe_adjacent(lines)


def _dedupe_adjacent(lines: list[str]) -> str:
    """合并相邻重复行（CC 字幕常有逐句滚动重复）。"""
    out: list[str] = []
    for text in lines:
        if out and out[-1] == text:
            continue
        out.append(text)
    return "".join(out)
