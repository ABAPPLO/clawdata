"""
浏览器自动化采集抖音热点视频（真实 Chromium/Edge 引擎）。

背景：搜索/部分接口会因 IP/行为风控返回 verify_check；但用真实浏览器加载
抖音热榜页，其前端会正常发起热点视频 feed（`aweme/v1/web/channel/hotspot`），
可拦截到真实视频列表与下载地址。本模块负责：
- 用系统 Edge 驱动真实浏览器（无需下载浏览器内核）
- 注入登录 Cookie，加载 https://www.douyin.com/hot
- 拦截 channel/hotspot 响应，提取热点视频（作者/标题/直链下载地址）

依赖：playwright（python）+ 系统 Edge；运行 `pip install playwright gmssl`
(Playwright 可通过 Edge，无需 download 浏览器)。
"""

from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Callable

from clawdata.core.session import load_cookies


DEFAULT_EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
HOT_URL = "https://www.douyin.com/hot"
TARGET = "channel/hotspot"


def collect_hot(
    count: int = 20,
    headless: bool = True,
    timeout: float = 25.0,
    cookies: dict[str, str] | None = None,
    edge_path: str | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """返回热点视频列表。每条含 aweme_id/title/author/url(直链)/cover。"""
    from playwright.sync_api import sync_playwright  # 延迟导入

    cookies = cookies or load_cookies()
    edge_path = edge_path or DEFAULT_EDGE
    captured: list[tuple[str, str]] = []

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    tries = [headless]
    if not headless:
        tries = [True]
    else:
        tries = [True, False]  # headless 优先，失败再用有头

    with sync_playwright() as p:
        for mode in tries:
            captured.clear()
            try:
                browser = p.chromium.launch(
                    executable_path=edge_path,
                    headless=mode,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"无法启动浏览器（确保已安装 Edge/Chrome）：{exc}") from exc

            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1366, "height": 900}, user_agent=UA)
            ctx.add_cookies([
                {
                    "name": name,
                    "value": value,
                    "domain": ".douyin.com",
                    "path": "/",
                    "httpOnly": name in ("sessionid", "sid_guard", "sid_tt"),
                }
                for name, value in cookies.items()
            ])
            page = ctx.new_page()

            def on_resp(resp) -> None:
                u = resp.url
                if any(s in u for s in ("/aweme/", "/hot/", "/feed/", "/channel/")):
                    try:
                        body = resp.text()
                    except Exception:  # noqa: BLE001
                        return
                    captured.append((u, body))

            page.on("response", on_resp)
            for attempt in range(1, 3):
                log(f"打开/刷新抖音热榜页（{'有头' if not mode else '无头'}）第{attempt}次…")
                if attempt == 1:
                    page.goto(HOT_URL, timeout=60000, wait_until="domcontentloaded")
                else:
                    try:
                        page.reload(wait_until="domcontentloaded")
                    except Exception:  # noqa: BLE001
                        page.goto(HOT_URL, timeout=60000, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                if _has_data(captured):
                    break
            browser.close()
            found = _collect(captured)
            log(f"该模式采集到 {len(found)} 条")
            if found:
                break

    items = list(found.values())
    if len(items) > count:
        items = items[:count]
    log(f"采集到 {len(items)} 条热点视频")
    return items


def search_videos_manual(
    keyword: str,
    count: int = 20,
    cookies: dict[str, str] | None = None,
    on_progress: Callable[[str], None] | None = None,
    max_wait: float = 300.0,
    step: float = 3.0,
    edge_path: str | None = None,
) -> list[dict[str, Any]]:
    """有人值守的搜索采集：启动有头浏览器，遇到验证码由用户手动完成。

    会打开一个可见的浏览器窗口，用户在其中登录/完成滑块验证后，
    本函数轮询拦截到的搜索响应，拿到 aweme_list 并返回。
    """
    from playwright.sync_api import sync_playwright

    cookies = cookies or load_cookies()
    edge_path = edge_path or DEFAULT_EDGE
    captured: list[tuple[str, str]] = []

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    url = "https://www.douyin.com/search/" + urllib.parse.quote(keyword)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=edge_path,
            headless=False,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--start-maximized"],
        )
        ctx = browser.new_context(locale="zh-CN", viewport={"width": 1366, "height": 900},
                                  user_agent=UA)
        ctx.add_cookies([
            {"name": n, "value": v, "domain": ".douyin.com", "path": "/",
             "httpOnly": n in ("sessionid", "sid_guard", "sid_tt")}
            for n, v in cookies.items()
        ])
        page = ctx.new_page()

        def on_resp(resp) -> None:
            u = resp.url
            if any(s in u for s in ("/aweme/", "/search/", "/general/", "/channel/")):
                try:
                    body = resp.text()
                except Exception:  # noqa: BLE001
                    return
                captured.append((u, body))

        page.on("response", on_resp)
        log("正在打开抖音搜索页，请在窗口里完成登录/滑块验证…")
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        try:
            page.bring_to_front()
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(2000)
        # 点一下「视频」标签，确保是视频结果（若已存在）
        try:
            for label in ("视频", "短视频"):
                tab = page.get_by_text(label, exact=True)
                if tab.count() > 0:
                    tab.first.click(timeout=2000)
                    break
        except Exception:  # noqa: BLE001
            pass

        found: dict[str, dict[str, Any]] = {}
        attempted: set[str] = set()
        deadline = time.time() + max_wait
        from clawdata.collection.douyin_play import resolve  # noqa: PLC0415

        def add_item(item: dict[str, Any]) -> None:
            if item and item["aweme_id"] and item["aweme_id"] not in found:
                found[item["aweme_id"]] = item

        while time.time() < deadline and len(found) < count:
            page.wait_for_timeout(int(step * 1000))
            # 1) 拦截到的搜索结果
            for url, body in captured:
                if "search" in url:
                    for a in _parse_capture(body) or []:
                        add_item(_to_item(a))
            # 2) DOM 兜底：从页面读视频 ID 并解析（目标为 count 数量）
            if len(found) < count:
                for aid in _extract_aweme_ids_from_page(page):
                    if aid in found or aid in attempted or len(found) >= count:
                        continue
                    attempted.add(aid)
                    try:
                        info = resolve(aid, cookies, timeout=10)
                    except Exception:  # noqa: BLE001
                        continue
                    found[aid] = {
                        "aweme_id": aid, "title": info.get("title", ""),
                        "author": info.get("author", ""), "url": info["url"], "word": keyword,
                    }
                    log(f"  已解析 {info.get('author', '')} | {info.get('title', '')[:20]}")
            log(f"已采集 {len(found)}/{count} 条…")

        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass

    items = list(found.values())[:count]
    log(f"采集到 {len(items)} 条“{keyword}”视频")
    return items


def _extract_aweme_ids_from_page(page) -> list[str]:
    """从搜索结果页 DOM 提取视频 ID，作为网络拦截的兜底。"""
    import re
    try:
        hrefs = page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => e.getAttribute('href')).filter(Boolean)",
        )
    except Exception:  # noqa: BLE001
        return []
    ids = set()
    for h in hrefs:
        for m in re.findall(r"/video/(\d+)", h):
            ids.add(m)
    return list(ids)[:50]


def search_videos_auto(
    keyword: str,
    count: int = 10,
    cookies: dict[str, str] | None = None,
    on_progress: Callable[[str], None] | None = None,
    headless: bool = True,
    probe: float = 25.0,
    max_wait: float = 300.0,
    step: float = 3.0,
    edge_path: str | None = None,
) -> list[dict[str, Any]]:
    """按关键词采集视频：无头浏览器优先，失败回退有头窗口（可手动过验证）。

    与 search_videos_manual 相比：支持 headless 自动尝试，无头拿不到数据才弹
    可见窗口。仍以拦截搜索响应为主、DOM 提取 + resolve 兜底。
    """
    from playwright.sync_api import sync_playwright  # 延迟导入

    cookies = cookies or load_cookies()
    edge_path = edge_path or DEFAULT_EDGE
    captured: list[tuple[str, str]] = []

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    url = "https://www.douyin.com/search/" + urllib.parse.quote(keyword)
    tries: list[bool] = [True, False] if headless else [False]

    with sync_playwright() as p:
        for mode in tries:
            captured.clear()
            try:
                browser = p.chromium.launch(
                    executable_path=edge_path,
                    headless=mode,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
                    + ([] if mode else ["--start-maximized"]),
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"无法启动浏览器（确保已安装 Edge/Chrome）：{exc}") from exc

            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1366, "height": 900}, user_agent=UA)
            ctx.add_cookies([
                {
                    "name": name,
                    "value": value,
                    "domain": ".douyin.com",
                    "path": "/",
                    "httpOnly": name in ("sessionid", "sid_guard", "sid_tt"),
                }
                for name, value in cookies.items()
            ])
            page = ctx.new_page()

            def on_resp(resp) -> None:
                u = resp.url
                if any(s in u for s in ("/search/", "/general/", "/aweme/")):
                    try:
                        body = resp.text()
                    except Exception:  # noqa: BLE001
                        return
                    captured.append((u, body))

            page.on("response", on_resp)
            log(f"搜索「{keyword}」（{'无头' if mode else '有头窗口，请完成验证'}）…")
            page.goto(url, timeout=60000, wait_until="domcontentloaded")
            if not mode:
                try:
                    page.bring_to_front()
                except Exception:  # noqa: BLE001
                    pass
            page.wait_for_timeout(2000)
            try:
                for label in ("视频", "短视频"):
                    tab = page.get_by_text(label, exact=True)
                    if tab.count() > 0:
                        tab.first.click(timeout=2000)
                        break
            except Exception:  # noqa: BLE001
                pass

            found: dict[str, dict[str, Any]] = {}
            attempted: set[str] = set()

            def add_item(item: dict[str, Any] | None) -> None:
                if item and item["aweme_id"] and item["aweme_id"] not in found:
                    found[item["aweme_id"]] = item

            deadline = time.time() + (probe if mode else max_wait)
            from clawdata.collection.douyin_play import resolve  # noqa: PLC0415

            while time.time() < deadline and len(found) < count:
                page.wait_for_timeout(int(step * 1000))
                for u, body in captured:
                    if "search" in u or "/aweme/" in u:
                        for a in _parse_capture(body) or []:
                            add_item(_to_item(a))
                if len(found) < count:
                    for aid in _extract_aweme_ids_from_page(page):
                        if aid in found or aid in attempted or len(found) >= count:
                            continue
                        attempted.add(aid)
                        try:
                            info = resolve(aid, cookies, timeout=10)
                        except Exception:  # noqa: BLE001
                            continue
                        add_item({
                            "aweme_id": aid, "title": info.get("title", ""),
                            "author": info.get("author", ""), "url": info["url"],
                            "word": keyword,
                        })
                        log(f"  已解析 {info.get('author', '')} | {info.get('title', '')[:20]}")
                log(f"已采集 {len(found)}/{count} 条…")

            browser.close()
            if found:
                items = list(found.values())[:count]
                log(f"「{keyword}」采集到 {len(items)} 条")
                return items

    return []


def collect_account_videos(
    sec_uid: str,
    count: int = 10,
    headless: bool = True,
    timeout: float = 30.0,
    cookies: dict[str, str] | None = None,
    edge_path: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    max_scroll: int = 8,
) -> list[dict[str, Any]]:
    """打开指定账号主页，滚动加载并拦截其作品列表（/aweme/post/）。

    优先无头；若拿不到数据再回退有头（有头时用户可顺手过验证）。
    返回每条含 aweme_id/title/author/url/cover/word/account。
    """
    from playwright.sync_api import sync_playwright  # 延迟导入

    cookies = cookies or load_cookies()
    edge_path = edge_path or DEFAULT_EDGE
    captured: list[tuple[str, str]] = []

    def log(msg: str) -> None:
        if on_progress:
            on_progress(msg)

    url = f"https://www.douyin.com/user/{sec_uid}"
    tries = [True, False] if headless else [False]

    with sync_playwright() as p:
        for mode in tries:
            captured.clear()
            try:
                browser = p.chromium.launch(
                    executable_path=edge_path,
                    headless=mode,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"]
                    + ([] if mode else ["--start-maximized"]),
                )
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"无法启动浏览器（确保已安装 Edge/Chrome）：{exc}") from exc

            ctx = browser.new_context(locale="zh-CN", viewport={"width": 1366, "height": 900}, user_agent=UA)
            ctx.add_cookies([
                {
                    "name": name,
                    "value": value,
                    "domain": ".douyin.com",
                    "path": "/",
                    "httpOnly": name in ("sessionid", "sid_guard", "sid_tt"),
                }
                for name, value in cookies.items()
            ])
            page = ctx.new_page()

            def on_resp(resp) -> None:
                u = resp.url
                if any(s in u for s in ("/aweme/", "/user/", "/post/", "/general/")):
                    try:
                        body = resp.text()
                    except Exception:  # noqa: BLE001
                        return
                    captured.append((u, body))

            page.on("response", on_resp)
            log(f"打开账号主页（{'有头' if not mode else '无头'}），滚动加载作品…")
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as exc:  # noqa: BLE001
                log(f"加载失败：{exc}")
                browser.close()
                continue
            page.wait_for_timeout(3000)

            found: dict[str, dict[str, Any]] = {}

            def add_item(item: dict[str, Any] | None) -> None:
                if item and item["aweme_id"] and item["aweme_id"] not in found:
                    found[item["aweme_id"]] = item

            for _ in range(max_scroll):
                page.mouse.wheel(0, 2400)
                page.wait_for_timeout(1500)
                # 从拦截到的 post 响应解析
                for u, body in captured:
                    if "/post/" in u or "/aweme/" in u:
                        for a in _parse_capture(body) or []:
                            add_item(_to_account_item(a, sec_uid))
                if len(found) >= count:
                    break

            # DOM 兜底：从页面链接补足到 count
            if len(found) < count:
                for aid in _extract_aweme_ids_from_page(page):
                    if aid in found or len(found) >= count:
                        continue
                    from clawdata.collection.douyin_play import resolve  # noqa: PLC0415
                    try:
                        info = resolve(aid, cookies, timeout=10)
                    except Exception:  # noqa: BLE001
                        continue
                    add_item({
                        "aweme_id": aid, "title": info.get("title", ""),
                        "author": info.get("author", ""), "url": info["url"],
                        "word": f"account:{sec_uid}", "account": info.get("author", ""),
                    })
                    log(f"  已解析 {info.get('author', '')} | {info.get('title', '')[:20]}")

            browser.close()
            items = list(found.values())[:count]
            log(f"该模式采集到 {len(items)} 条")
            if items:
                return items

    return []


def _to_account_item(a: dict[str, Any], sec_uid: str) -> dict[str, Any] | None:
    video = a.get("video", {}) or {}
    url = _first_url(video.get("download_addr")) or _first_url(video.get("play_addr"))
    if not url:
        return None
    author = a.get("author", {}) or {}
    nickname = author.get("nickname", "")
    return {
        "aweme_id": str(a.get("aweme_id", "")),
        "title": a.get("desc", ""),
        "author": nickname,
        "account": nickname or author.get("sec_uid", "") or sec_uid,
        "url": url,
        "cover": _first_url(video.get("cover")),
        "word": f"account:{sec_uid}",
    }


def _has_data(captured: list[tuple[str, str]]) -> bool:
    return any(_parse_capture(body) for _, body in captured)


def _collect(captured: list[tuple[str, str]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for _, body in captured:
        for a in _parse_capture(body) or []:
            item = _to_item(a)
            if item and item["aweme_id"] and item["aweme_id"] not in out:
                out[item["aweme_id"]] = item
    return out


def _parse_capture(body: str) -> list[dict[str, Any]] | None:
    try:
        obj = json.loads(body)
    except Exception:  # noqa: BLE001
        return None
    data = obj.get("data", {}) if isinstance(obj.get("data"), dict) else obj
    al = data.get("aweme_list", []) if isinstance(data, dict) else []
    return al if isinstance(al, list) and al else None


def _to_item(a: dict[str, Any]) -> dict[str, Any] | None:
    video = a.get("video", {}) or {}
    url = _first_url(video.get("download_addr")) or _first_url(video.get("play_addr"))
    if not url:
        return None
    author = a.get("author", {}) or {}
    return {
        "aweme_id": str(a.get("aweme_id", "")),
        "title": a.get("desc", ""),
        "author": author.get("nickname", ""),
        "url": url,
        "cover": _first_url(video.get("cover")),
        "word": "热点",
    }


def _first_url(node: dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    urls = node.get("url_list")
    if isinstance(urls, list) and urls:
        return str(urls[0])
    return ""
