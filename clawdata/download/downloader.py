"""
人味化串行下载引擎。

设计要点：
- 严格"一条一条"下载：for 循环逐个处理，绝不并发。
- 每条之间随机停顿（默认 3~8 秒），模拟真人浏览节奏。
- 深夜禁止窗口：下载前检查 QuietHours，若处于禁止时段则等待到开放。
- 断点续传：已存在且大小正确的 skip；中断的临时文件可断点续传。
- 网络抖动重试：单条失败会按节奏重试。
- 输出进度与日志：记录到 console + logs/download.log。

用法：
    from clawdata.download.downloader import download_items
    items = [{"title": "视频1", "url": "...", "filename": "1.mp4"}]
    download_items(items, outdir="downloads")
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from clawdata.core.humanize import QuietHours, human_headers, random_delay
from clawdata.core.paths import DOWNLOAD_LOG_PATH, DOWNLOADS_DIR
from clawdata.storage.store import record


@dataclass
class DownloadConfig:
    outdir: str = DOWNLOADS_DIR
    delay_range: tuple[float, float] = (3.0, 8.0)
    quiet: QuietHours = field(default_factory=QuietHours)
    timeout: float = 60.0
    max_retry: int = 3
    chunk_size: int = 1024 * 256
    log_file: str = DOWNLOAD_LOG_PATH
    referer: str = "https://www.douyin.com/"
    cookies: dict[str, str] = field(default_factory=dict)
    store: str = ""  # 非空则把每条结果写入按日的 SQLite 记录
    category: str = ""  # 记录的类型/分类，写入 tag_category
    auto_tag: bool = True
    auto_tag_enqueuer: Any = None


def _log(config: DownloadConfig, msg: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    with open(config.log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _safe_name(name: str) -> str:
    for ch in '\\/:*?"<>|':
        name = name.replace(ch, "_")
    return name.strip() or "video"


def _download_one(url: str, dest: str, config: DownloadConfig, referer: str) -> int:
    """下载单个文件，支持断点续传。返回本次新增字节数。"""
    headers = human_headers({"Referer": referer, "Accept": "*/*"})
    # config.cookies 是抖音登录态，只发给抖音域名，避免跨站携带
    if config.cookies and "douyin.com" in referer:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in config.cookies.items())
    partial = dest + ".part"
    already = os.path.getsize(partial) if os.path.exists(partial) else 0
    if already:
        headers["Range"] = f"bytes={already}-"

    req = urllib.request.Request(url, headers=headers)
    written = 0
    with urllib.request.urlopen(req, timeout=config.timeout) as resp:
        mode = "ab" if already and resp.status == 206 else "wb"
        if already and resp.status != 206:
            already = 0
        with open(partial, mode) as f:
            while True:
                chunk = resp.read(config.chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
    os.replace(partial, dest)
    return written


def _ffmpeg_exe() -> str:
    """内置静态 ffmpeg（imageio-ffmpeg 依赖自带）。"""
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _mux_av(video_path: str, audio_path: str, dest: str, config: DownloadConfig) -> None:
    """把分离的视频流和音频流无损合流为 MP4。"""
    import subprocess

    cmd = [_ffmpeg_exe(), "-y", "-loglevel", "error",
           "-i", video_path, "-i", audio_path,
           "-c", "copy", "-movflags", "+faststart", dest]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError(f"ffmpeg 合流失败：{(proc.stderr or '').strip()[:300]}")


def _download_media(item: dict[str, Any], dest: str, config: DownloadConfig) -> int:
    """下载一个条目。带 audio_url 的（B 站 DASH）先分离下载再合流；返回文件字节数。"""
    audio_url = str(item.get("audio_url") or "")
    referer = item.get("referer", config.referer)
    if not audio_url:
        _download_one(item["url"], dest, config, referer)
        return os.path.getsize(dest)

    video_tmp = dest + ".video.mp4"
    audio_tmp = dest + ".audio.m4a"
    try:
        _download_one(item["url"], video_tmp, config, referer)
        _download_one(audio_url, audio_tmp, config, referer)
        _mux_av(video_tmp, audio_tmp, dest, config)
    finally:
        for tmp in (video_tmp, audio_tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return os.path.getsize(dest)


def download_items(items: list[dict[str, Any]] | None, config: DownloadConfig | None = None) -> int:
    """逐个下载视频，返回成功条数。"""
    items = items or []
    config = config or DownloadConfig()
    os.makedirs(config.outdir, exist_ok=True)
    _log(config, f"开始下载，共 {len(items)} 条（一条一条串行，每条间隔 {config.delay_range[0]:.0f}-{config.delay_range[1]:.0f} 秒）")

    ok = 0
    for i, item in enumerate(items, start=1):
        config.quiet.wait_until_open()
        title = str(item.get("title", "") or f"video_{i}")
        filename = _safe_name(str(item.get("filename", "")) or title + ".mp4")
        dest = os.path.join(config.outdir, filename)
        url = item["url"]

        _log(config, f"[{i}/{len(items)}] 开始：{title}")

        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            _log(config, f"  已存在，跳过：{filename}")
            if config.store:
                record(config.store, title=str(item.get("title", "")), category=str(item.get("category", config.category)),
                       author=str(item.get("author", "")), aweme_id=str(item.get("aweme_id", "")), word=str(item.get("word", "")),
                       account=str(item.get("account", "")), url=url, file=dest, size=os.path.getsize(dest), status="already")
                if config.auto_tag:
                    _auto_tag(dest, config)
            ok += 1
            continue

        success = False
        for attempt in range(1, config.max_retry + 1):
            try:
                written = _download_media(item, dest, config)
                _log(config, f"  完成 ({written/1024:.0f} KB)：{filename}")
                if config.store:
                    record(config.store, title=str(item.get("title", "")), category=str(item.get("category", config.category)),
                           author=str(item.get("author", "")), aweme_id=str(item.get("aweme_id", "")), word=str(item.get("word", "")),
                           account=str(item.get("account", "")), url=url, file=dest, size=os.path.getsize(dest), status="downloaded")
                if config.auto_tag:
                    _auto_tag(dest, config)
                success = True
                break
            except Exception as exc:  # noqa: BLE001
                _log(config, f"  第 {attempt} 次失败：{type(exc).__name__}: {exc}")
                if attempt == config.max_retry and config.store:
                    record(config.store, title=str(item.get("title", "")), category=str(item.get("category", config.category)),
                           author=str(item.get("author", "")), aweme_id=str(item.get("aweme_id", "")), word=str(item.get("word", "")),
                           account=str(item.get("account", "")), url=url, file=dest, size=0, status="failed")
                if attempt < config.max_retry:
                    random_delay(5, 12)

        if success:
            ok += 1
        else:
            _log(config, f"  放弃：{title}")

        if i < len(items):
            random_delay(*config.delay_range)

    _log(config, f"结束：成功 {ok}/{len(items)} 条")
    return ok


def _auto_tag(file: str, config: DownloadConfig) -> None:
    """每次下载完成后自动进入内容理解队列。"""
    if not config.store:
        return
    if config.auto_tag_enqueuer:
        config.auto_tag_enqueuer(file)
        return
    try:
        from clawdata.ai.tagging import get_or_start_worker
        get_or_start_worker(config.store).enqueue(file)
    except Exception as exc:
        print(f"[自动打标] 入队失败：{exc}")


def download_single(url: str, dest: str, config: DownloadConfig | None = None) -> int:
    """便捷：下载单个文件（用于测试或单条场景）。"""
    config = config or DownloadConfig()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    return _download_one(url, dest, config, config.referer)
