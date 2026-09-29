"""
通用链接路由：逐行识别抖音 / B 站输入，解析成统一的待下载条目。

流水线 CLI 和 Web 面板共用这一份路由逻辑，保证两个入口行为一致：
- B 站：BV号、av号、bilibili.com 视频页、b23.tv 短链（无需抖音 Cookie）
- 抖音：视频 ID、分享链接、主页短链（需要 config/cookies.json 登录态）
"""

from __future__ import annotations

from typing import Any, Callable

from clawdata.collection import bilibili
from clawdata.collection.douyin_play import resolve as resolve_douyin


def resolve_line(line: str, douyin_cookies: dict[str, str] | None) -> dict[str, Any]:
    """解析单行输入，返回下载条目。解析失败抛异常。"""
    if bilibili.is_bilibili_link(line):
        info = bilibili.resolve(line)
        return {
            "title": f"{info.get('author')} | {info.get('title')}",
            "author": info.get("author", ""),
            "aweme_id": info.get("aweme_id", ""),
            "word": "bilibili",
            "url": info["url"],
            "audio_url": info.get("audio_url", ""),
            "filename": f"bili_{info.get('aweme_id')}_{(info.get('title') or 'video')[:30]}.mp4",
            "referer": bilibili.REFERER,
        }
    info = resolve_douyin(line, douyin_cookies or {})
    return {
        "title": f"{info.get('author')} | {info.get('title')}",
        "author": info.get("author", ""),
        "aweme_id": info.get("aweme_id", ""),
        "word": "",
        "url": info["url"],
        "audio_url": "",
        "filename": f"{info.get('author') or 'dy'}_{info.get('aweme_id')}_{(info.get('title') or 'video')[:30]}.mp4",
        "referer": "https://www.douyin.com/",
    }


def resolve_lines(
    lines: list[str],
    douyin_cookies: dict[str, str] | None,
    on_progress: Callable[[int, int, str, str], None] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """批量解析。返回 (条目列表, 失败信息列表)；# 开头和空行会被忽略。"""
    lines = [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    for i, line in enumerate(lines, start=1):
        try:
            item = resolve_line(line, douyin_cookies)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{line[:40]} -> {exc}")
            if on_progress:
                on_progress(i, len(lines), line[:40], f"失败：{exc}")
            continue
        items.append(item)
        if on_progress:
            on_progress(i, len(lines), item["title"][:40], "解析成功")
    return items, errors
