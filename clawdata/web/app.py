"""
抖音抓取结果网页面板（标准库 http.server + SQLite，零第三方依赖）。

启动：
    python -m clawdata.web [--port 8000]

然后浏览器打开 http://127.0.0.1:8000

功能：
- /          查看当天抓取结果 + 历史汇总
- /api/today 当天记录（JSON）
- /api/history 历史按天汇总（JSON）
- /api/day?date=YYYY-MM-DD 某天记录（JSON）
- /media/<id> 播放/下载对应视频
"""

from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from datetime import datetime, time as dtime

from clawdata.download.downloader import DownloadConfig, download_items
from clawdata.core.humanize import QuietHours, random_delay
from clawdata.core.session import load_cookies
from clawdata.collection.douyin_hot import fetch_hot_list, normalize as normalize_hot
from clawdata.collection.account_videos import fetch_account_videos, resolve_sec_uid

from clawdata.storage.store import get, history, init, today
from clawdata.storage.store import list_hotlist_days, load_hotlist_snapshot
from clawdata.storage import store
from clawdata.ai.tagging import build_worker, load_config
from clawdata.ai.comfy_client import ComfyClient
from clawdata.vision import pose_segment
from clawdata.vision import tools_filter
from clawdata.core.paths import PROJECT_ROOT as ROOT, INDEX_PATH
from clawdata.core.paths import (
    DEFAULT_DB_PATH,
    DEFAULT_TAGGING_CONFIG_PATH,
    THUMBNAILS_DIR,
)
from clawdata.media.thumbnails import first_frame_thumbnail


# 后台打标签 worker 与配置（在 main 中初始化）
WORKER = None
TAG_CONFIG = None
POSE_WORKER = None

# 面板「按主题采集」任务（同一时间只跑一个）
COLLECT_LOCK = threading.Lock()
COLLECT_JOB: dict | None = None
COLLECT_THREAD: threading.Thread | None = None
SUBSCRIPTION_LOCK = threading.Lock()
SUBSCRIPTION_JOB: dict | None = None
SUBSCRIPTION_THREAD: threading.Thread | None = None
SUBSCRIPTION_SCHEDULER: threading.Thread | None = None
# 面板「链接速下 / B站采集」任务（同一时间只跑一个）
LINKS_LOCK = threading.Lock()
LINKS_JOB: dict | None = None
LINKS_THREAD: threading.Thread | None = None
# 面板「视频文库」整理任务（同一时间只跑一个）
DIGEST_LOCK = threading.Lock()
DIGEST_JOB: dict | None = None
DIGEST_THREAD: threading.Thread | None = None


def _collect_status() -> dict:
    """返回当前采集任务的对外状态快照。"""
    with COLLECT_LOCK:
        if not COLLECT_JOB:
            return {"active": False, "job": None}
        job = {k: v for k, v in COLLECT_JOB.items() if k != "stop"}
        return {"active": job.get("status") == "running", "job": job}


def _collect_update(job: dict, **changes) -> None:
    with COLLECT_LOCK:
        job.update(changes)


def _collect_should_stop(job: dict) -> bool:
    with COLLECT_LOCK:
        return bool(job.get("stop"))


def _collect_worker(job: dict) -> None:
    """后台执行「按主题/关键词采集 + 下载」，进度写回 job。"""
    db = Handler.db
    try:
        cookies = load_cookies()
        from clawdata.collection.browser_collect import search_videos_auto  # 延迟导入，避免面板启动就拉起 playwright

        for idx, kw in enumerate(job["keywords"], start=1):
            if _collect_should_stop(job):
                break
            _collect_update(
                job,
                current_keyword=kw,
                keyword_index=idx,
                message=f"({idx}/{len(job['keywords'])}) 正在采集「{kw}」…",
            )

            def on_progress(msg: str) -> None:
                _collect_update(job, message=f"({idx}/{len(job['keywords'])}) {msg}")

            vids = search_videos_auto(
                kw,
                count=job["per_word"],
                cookies=cookies,
                headless=job["headless"],
                on_progress=on_progress,
            )
            job["collected"] += len(vids)
            if not vids:
                _collect_update(job, message=f"「{kw}」未采集到视频")
                continue

            cat = job["category"] or kw
            cfg = DownloadConfig(
                outdir="downloads",
                delay_range=(1.5, 4.0),
                quiet=QuietHours(dtime(0, 0), dtime(0, 0)),  # 面板手动采集不受深夜窗口限制
                cookies=cookies,
                store=db,
                category=cat,
                auto_tag_enqueuer=lambda path: WORKER.enqueue(path) if WORKER else None,
            )
            for j, v in enumerate(vids, start=1):
                if _collect_should_stop(job):
                    break
                author = v.get("author", "") or "dy"
                desc = (v.get("title") or v.get("aweme_id") or "video")[:30]
                item = {
                    "title": f"[{cat}] {author} | {desc}",
                    "author": author,
                    "aweme_id": v.get("aweme_id", ""),
                    "word": kw,
                    "category": cat,
                    "url": v["url"],
                    "filename": f"{author}_{v.get('aweme_id')}.mp4",
                    "referer": "https://www.douyin.com/",
                }
                _collect_update(
                    job,
                    current_file=item["title"],
                    message=f"({idx}/{len(job['keywords'])}) 下载 {j}/{len(vids)}：{item['title']}",
                )
                ok = download_items([item], cfg)
                job["downloaded"] += ok
                if j < len(vids):
                    random_delay(1.5, 4.0)

        _collect_update(
            job,
            status="stopped" if job.get("stop") else "done",
            message="已停止" if job.get("stop") else "采集完成",
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )
    except Exception as exc:  # noqa: BLE001
        _collect_update(
            job,
            status="error",
            message=f"采集失败：{exc}",
            error=str(exc),
            finished_at=datetime.now().isoformat(timespec="seconds"),
        )


def _auto_tag_downloaded(path: str) -> None:
    if WORKER:
        WORKER.enqueue(path)
    else:
        from clawdata.ai.tagging import get_or_start_worker
        get_or_start_worker(Handler.db).enqueue(path)


def refresh_today_hot_events() -> tuple[int, str]:
    """只刷新热点事件列表，不触发任何视频下载。"""
    raw = fetch_hot_list()
    items = [normalize_hot(row, i + 1).__dict__ for i, row in enumerate(raw) if row.get("word")]
    day = datetime.now().strftime("%Y-%m-%d")
    store.save_hotlist_snapshot(day, items)
    return len(items), day


def _subscription_status() -> dict:
    with SUBSCRIPTION_LOCK:
        if not SUBSCRIPTION_JOB:
            return {"active": False, "job": None}
        job = {k: v for k, v in SUBSCRIPTION_JOB.items() if k != "stop"}
        return {"active": job.get("status") == "running", "job": job}


def _subscription_update(job: dict, **changes) -> None:
    with SUBSCRIPTION_LOCK:
        job.update(changes)


def _update_bilibili_subscription(db: str, job: dict, idx: int, total: int, sub: dict, name: str) -> None:
    """处理一个 B站 UP 主订阅：拉最新作品、增量下载、刷新检查时间。"""
    from datetime import timedelta
    from clawdata.collection import bilibili as bili_mod

    _subscription_update(job, current=name, index=idx, status="running",
                         message=f"({idx}/{total}) 正在检查 B站「{name}」…")
    bcookies = bili_mod.load_bilibili_cookies()
    mid = sub.get("sec_uid") or bili_mod.extract_mid(sub["homepage"])
    vids = bili_mod.fetch_user_videos(mid, count=30, cookies=bcookies)
    known = store.existing_aweme_ids(db)
    new = [v for v in vids if v.get("bvid") and v["bvid"] not in known]
    downloaded = 0
    for v in new:
        try:
            info = bili_mod.resolve(v["bvid"], bcookies)
        except Exception as exc:  # noqa: BLE001
            _subscription_update(job, message=f"「{name}」解析失败 {v['bvid']}：{exc}")
            continue
        account = v.get("author") or name
        desc = (v.get("title") or v["bvid"])[:30]
        download_items([{
            "title": f"[{sub.get('category')}] {account} | {desc}",
            "author": v.get("author", ""), "aweme_id": v["bvid"],
            "account": account, "word": f"account:{account}",
            "url": info["url"], "audio_url": info.get("audio_url", ""),
            "filename": f"bili_{v['bvid']}_{desc}.mp4",
            "referer": f"https://space.bilibili.com/{mid}",
        }], DownloadConfig(
            outdir="downloads", delay_range=(1.5, 4.0),
            quiet=QuietHours(dtime(0, 0), dtime(0, 0)), cookies={},
            store=db, category=sub.get("category") or "订阅UP主",
            auto_tag_enqueuer=_auto_tag_downloaded,
        ))
        downloaded += 1
        _subscription_update(job, message=f"「{name}」下载新增 {downloaded}/{len(new)}")
    now = datetime.now()
    interval = max(1, int(sub.get("interval_hours") or 6))
    resolved_account = (vids[0].get("author") if vids else "") or sub.get("account") or name
    store.update_subscription(db, int(sub["id"]), sec_uid=mid,
        last_checked_at=now.isoformat(timespec="seconds"),
        next_check_at=(now + timedelta(hours=interval)).isoformat(timespec="seconds"),
        last_new_count=downloaded, error="", account=resolved_account)
    _subscription_update(job, downloaded=job.get("downloaded", 0) + downloaded,
                         message=f"「{name}」新增 {downloaded} 条")


def _subscription_worker(target_id: int = 0) -> None:
    db = Handler.db
    job = SUBSCRIPTION_JOB
    try:
        cookies = load_cookies()
        subs = [store.get_subscription(db, target_id)] if target_id else store.due_subscriptions(db)
        subs = [x for x in subs if x]
        _subscription_update(job, total=len(subs), message=f"准备更新 {len(subs)} 个订阅")
        for idx, sub in enumerate(subs, start=1):
            name = sub.get("name") or sub.get("homepage")
            _subscription_update(job, current=name, index=idx, status="running",
                                 message=f"({idx}/{len(subs)}) 正在检查「{name}」…")
            try:
                from clawdata.collection import bilibili as bili_mod

                if bili_mod.is_space_url(sub["homepage"]):
                    _update_bilibili_subscription(db, job, idx, len(subs), sub, name)
                    continue

                sec_uid = sub.get("sec_uid") or resolve_sec_uid(sub["homepage"], cookies)
                vids = []
                try:
                    from clawdata.collection.browser_collect import collect_account_videos
                    vids = collect_account_videos(sec_uid, count=50, headless=True, cookies=cookies,
                                                  on_progress=lambda m: _subscription_update(job, message=f"({idx}/{len(subs)}) {m}"))
                except Exception:
                    vids = []
                if not vids:
                    vids = fetch_account_videos(sec_uid, cookies, count=50)
                known = store.existing_aweme_ids(db)
                new = [v for v in vids if v.get("aweme_id") and str(v["aweme_id"]) not in known]
                for v in new:
                    account = v.get("account") or v.get("author") or name
                    desc = (v.get("title") or v.get("aweme_id") or "video")[:30]
                    download_items([{
                        "title": f"[{sub.get('category')}] {account} | {desc}",
                        "author": v.get("author", ""), "aweme_id": v.get("aweme_id", ""),
                        "account": account, "word": f"account:{account}",
                        "category": sub.get("category") or "订阅博主",
                        "url": v["url"],
                        "filename": f"{account}_{v.get('aweme_id')}.mp4",
                        "referer": f"https://www.douyin.com/user/{sec_uid}",
                    }], DownloadConfig(
                        outdir="downloads", delay_range=(1.5, 4.0),
                        quiet=QuietHours(dtime(0, 0), dtime(0, 0)), cookies=cookies,
                        store=db, category=sub.get("category") or "订阅博主",
                        auto_tag_enqueuer=_auto_tag_downloaded,
                    ))
                now = datetime.now()
                interval = max(1, int(sub.get("interval_hours") or 6))
                from datetime import timedelta
                resolved_account = ""
                for v in vids:
                    resolved_account = str(v.get("account") or v.get("author") or "")
                    if resolved_account:
                        break
                store.update_subscription(db, int(sub["id"]), sec_uid=sec_uid,
                    last_checked_at=now.isoformat(timespec="seconds"),
                    next_check_at=(now + timedelta(hours=interval)).isoformat(timespec="seconds"),
                    last_new_count=len(new), error="",
                    account=resolved_account or sub.get("account") or name)
                _subscription_update(job, downloaded=job.get("downloaded", 0) + len(new),
                                     message=f"「{name}」新增 {len(new)} 条")
            except Exception as exc:
                from datetime import timedelta
                store.update_subscription(db, int(sub["id"]),
                    last_checked_at=datetime.now().isoformat(timespec="seconds"),
                    next_check_at=(datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds"),
                    error=str(exc)[:500])
                _subscription_update(job, errors=job.get("errors", 0) + 1,
                                     message=f"「{name}」更新失败：{exc}")
        _subscription_update(job, status="completed",
                             finished_at=datetime.now().isoformat(timespec="seconds"), message="订阅更新完成")
    except Exception as exc:
        _subscription_update(job, status="failed", error=str(exc),
                             finished_at=datetime.now().isoformat(timespec="seconds"), message=f"失败：{exc}")


def _start_subscription_refresh(target_id: int = 0) -> bool:
    global SUBSCRIPTION_JOB, SUBSCRIPTION_THREAD
    with SUBSCRIPTION_LOCK:
        if SUBSCRIPTION_JOB and SUBSCRIPTION_JOB.get("status") == "running":
            return False
        SUBSCRIPTION_JOB = {"status": "running", "target_id": target_id, "total": 0,
                            "index": 0, "current": "", "downloaded": 0, "errors": 0,
                            "message": "准备开始…",
                            "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": ""}
        SUBSCRIPTION_THREAD = threading.Thread(target=_subscription_worker, args=(target_id,),
                                               daemon=True, name="subscription-worker")
        SUBSCRIPTION_THREAD.start()
        return True


def _digest_status() -> dict:
    with DIGEST_LOCK:
        if not DIGEST_JOB:
            return {"active": False, "job": None}
        job = {k: v for k, v in DIGEST_JOB.items() if k != "stop"}
        return {"active": job.get("status") == "running", "job": job}


def _digest_update(job: dict, **changes) -> None:
    with DIGEST_LOCK:
        job.update(changes)


def _digest_worker(subscription_id: int, limit: int) -> None:
    """后台执行「视频文库」整理：拉订阅最新视频 -> 提取简介/字幕 -> AI 总结 -> 入库。"""
    from clawdata.digest import service as digest_service

    job = DIGEST_JOB
    try:
        stats = digest_service.run_digest(
            Handler.db, subscription_id=subscription_id, limit=limit or None,
            on_progress=lambda m: _digest_update(job, message=m),
        )
        _digest_update(job, status="completed", stats=stats,
                       message=f"整理完成：入库 {stats.get('created', 0)} 条",
                       finished_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001
        _digest_update(job, status="failed", error=str(exc),
                       message=f"整理失败：{exc}",
                       finished_at=datetime.now().isoformat(timespec="seconds"))


def _start_digest_job(subscription_id: int = 0, limit: int = 0) -> bool:
    global DIGEST_JOB, DIGEST_THREAD
    with DIGEST_LOCK:
        if DIGEST_JOB and DIGEST_JOB.get("status") == "running":
            return False
        DIGEST_JOB = {"status": "running", "subscription_id": subscription_id,
                      "limit": limit, "message": "准备开始…", "error": "", "stats": None,
                      "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": ""}
        DIGEST_THREAD = threading.Thread(target=_digest_worker, args=(subscription_id, limit),
                                         daemon=True, name="digest-worker")
        DIGEST_THREAD.start()
        return True


def _links_status() -> dict:
    with LINKS_LOCK:
        if not LINKS_JOB:
            return {"active": False, "job": None}
        job = {k: v for k, v in LINKS_JOB.items() if k != "stop"}
        return {"active": job.get("status") == "running", "job": job}


def _links_update(job: dict, **changes) -> None:
    with LINKS_LOCK:
        job.update(changes)


def _links_worker(kind: str, payload: dict) -> None:
    """统一下载任务：粘贴链接（抖音/B站混贴）或 B站热门/关键词，解析后串行下载入库。"""
    db = Handler.db
    job = LINKS_JOB
    try:
        category = str(payload.get("category") or "").strip()
        try:
            douyin_cookies = load_cookies()
        except Exception:  # noqa: BLE001
            douyin_cookies = {}

        if kind == "links":
            from clawdata.collection.links import resolve_lines

            lines = [ln for ln in re.split(r"[\n\r]+", str(payload.get("text") or "")) if ln.strip()]
            _links_update(job, total=len(lines), message=f"开始解析 {len(lines)} 条链接…")

            def _progress(i: int, n: int, ln: str, msg: str) -> None:
                _links_update(job, resolved=i, message=f"解析 ({i}/{n}) {msg}")

            items, errors = resolve_lines(lines, douyin_cookies, on_progress=_progress)
            _links_update(job, resolved=len(items), errors=len(errors))
        else:
            from clawdata.collection import bilibili as bili_mod

            count = max(1, min(int(payload.get("count") or 5), 30))
            bcookies = bili_mod.load_bilibili_cookies()
            if kind == "bili_popular":
                _links_update(job, message="获取 B站热门榜…")
                found = bili_mod.fetch_popular(count, bcookies)
            else:
                kw = str(payload.get("keyword") or "").strip()
                _links_update(job, message=f"搜索 B站「{kw}」…")
                found = bili_mod.search_videos(kw, count, bcookies)
            _links_update(job, total=len(found))
            items = []
            for i, v in enumerate(found, start=1):
                _links_update(job, message=f"解析 ({i}/{len(found)}) {(v.get('title') or '')[:36]}")
                try:
                    info = bili_mod.resolve(v["bvid"], bcookies)
                except Exception:  # noqa: BLE001
                    _links_update(job, errors=job.get("errors", 0) + 1)
                    continue
                items.append({
                    "title": f"{info['author']} | {info['title']}",
                    "author": info.get("author", ""),
                    "aweme_id": info.get("aweme_id", ""),
                    "word": "bilibili",
                    "url": info["url"],
                    "audio_url": info.get("audio_url", ""),
                    "filename": f"bili_{info['aweme_id']}_{(info.get('title') or 'video')[:30]}.mp4",
                    "referer": bili_mod.REFERER,
                })
                _links_update(job, resolved=i)

        if category:
            for it in items:
                it["category"] = category
        _links_update(job, message=f"解析完成，开始下载 {len(items)} 条")
        ok = download_items(items, DownloadConfig(
            outdir=os.path.join(ROOT, "downloads"),
            delay_range=(1.5, 4.0),
            quiet=QuietHours(dtime(0, 0), dtime(0, 0)),
            cookies=douyin_cookies,
            store=db, category=category or "链接下载",
            auto_tag_enqueuer=_auto_tag_downloaded,
        ))
        _links_update(job, downloaded=int(ok), status="completed",
                      message=f"完成：下载 {ok}/{len(items)} 条",
                      finished_at=datetime.now().isoformat(timespec="seconds"))
    except Exception as exc:  # noqa: BLE001
        _links_update(job, status="failed", error=str(exc), message=f"失败：{exc}",
                      finished_at=datetime.now().isoformat(timespec="seconds"))


def _start_links_job(kind: str, payload: dict) -> bool:
    global LINKS_JOB, LINKS_THREAD
    with LINKS_LOCK:
        if LINKS_JOB and LINKS_JOB.get("status") == "running":
            return False
        LINKS_JOB = {"status": "running", "kind": kind, "total": 0, "resolved": 0,
                     "downloaded": 0, "errors": 0, "message": "准备开始…", "error": "",
                     "started_at": datetime.now().isoformat(timespec="seconds"), "finished_at": ""}
        LINKS_THREAD = threading.Thread(target=_links_worker, args=(kind, payload),
                                        daemon=True, name="links-worker")
        LINKS_THREAD.start()
        return True


def _subscription_scheduler_loop() -> None:
    while True:
        try:
            if store.due_subscriptions(Handler.db):
                _start_subscription_refresh(0)
        except Exception:
            pass
        time.sleep(3600)


def _start_collect(
    keywords: list[str],
    *,
    per_word: int = 5,
    category: str = "",
    headless: bool = True,
) -> bool:
    """启动一个采集任务；已有任务在跑则返回 False。"""
    global COLLECT_JOB, COLLECT_THREAD
    keywords = [k.strip() for k in keywords if str(k).strip()]
    if not keywords:
        return False
    per_word = max(1, min(int(per_word or 5), 30))
    with COLLECT_LOCK:
        if COLLECT_JOB and COLLECT_JOB.get("status") == "running":
            return False
        job = {
            "status": "running",
            "keywords": keywords,
            "per_word": per_word,
            "category": category,
            "headless": bool(headless),
            "keyword_index": 0,
            "current_keyword": "",
            "current_file": "",
            "collected": 0,
            "downloaded": 0,
            "message": "准备开始…",
            "error": "",
            "stop": False,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": "",
        }
        COLLECT_JOB = job
        COLLECT_THREAD = threading.Thread(
            target=_collect_worker, args=(job,), daemon=True, name="collect-worker"
        )
        COLLECT_THREAD.start()
        return True


class Handler(BaseHTTPRequestHandler):
    db: str = "data/clawdata.db"

    def log_message(self, fmt, *args):  # 静默访问日志
        pass

    def _json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_file(self, path: str, *, cache: bool = False) -> None:
        if not os.path.isfile(path):
            self.send_error(404, "no such file")
            return
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        size = os.path.getsize(path)

        # 支持 Range（让 <video> 能拖进度条、秒开），否则浏览器可能无法继续播放
        range_header = self.headers.get("Range")
        start = 0
        end = size - 1
        status = 200
        if range_header:
            m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
            if m:
                start = int(m.group(1)) if m.group(1) else 0
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
            if start > end or start >= size:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206

        length = end - start + 1
        # 前端单文件迭代频繁，禁止缓存，避免浏览器拿旧版路由/UI
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        if cache:
            self.send_header("Cache-Control", "private, max-age=3600")
        else:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Pragma", "no-cache")
        self.send_header("Accept-Ranges", "bytes")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(length))


    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw) or {}
        except (ValueError, TypeError):
            return {}

    def _record_to_view(self, rec: dict) -> dict:
        """把数据库记录转成前端用的打标签视图，把 tags JSON 字符串解析成数组。"""
        out = dict(rec)
        raw_tags = rec.get("tags") or "[]"
        try:
            out["tags"] = json.loads(raw_tags) if raw_tags else []
        except (ValueError, TypeError):
            out["tags"] = []
        out["has_file"] = bool(rec.get("file"))
        # 供前端展示的分类（首字）
        out["cat"] = rec.get("tag_category") or ""
        out["summary"] = rec.get("tag_summary") or ""
        out["tag_status"] = rec.get("tag_status") or "untagged"
        out["tag_error"] = rec.get("tag_error") or ""
        out["tagged_at"] = rec.get("tagged_at") or ""
        out["account"] = rec.get("account") or ""
        return out

    def _export_action(self, qs: dict) -> None:
        """导出连续动作筛选结果（CSV / JSON），作为附件下载。"""
        status = qs.get("status", ["all"])[0]
        try:
            min_longest = float(qs.get("min_longest", ["0"])[0] or 0)
        except (ValueError, TypeError):
            min_longest = 0.0
        category = qs.get("category", [""])[0]
        q = qs.get("q", [""])[0]
        fmt = qs.get("format", ["csv"])[0]
        items = store.list_pose_items(
            self.db, status=status, min_longest=min_longest, category=category, q=q
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        tag = f"{status}_{int(min_longest*100)}" if status != "all" else "all"

        if fmt == "json":
            body = json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")
            ctype = "application/json; charset=utf-8"
            fname = f"action_{tag}_{stamp}.json"
        else:  # csv（带 BOM，Excel 直接打开不乱码）
            lines = ["标题,作者,视频ID,类别,总时长,最长连续段,检测人数,分段数,Top1时长,Top2时长,Top3时长,文件"]

            def cell(v):
                s = str(v if v is not None else "").replace('"', '""')
                return f'"{s}"'

            for it in items:
                top = it.get("top") or []
                t = [x.get("duration", 0) for x in top] + [0, 0, 0]
                lines.append(",".join([
                    cell(it.get("title", "")),
                    cell(it.get("author", "")),
                    cell(it.get("aweme_id", "")),
                    cell(it.get("cat", "")),
                    cell(round(it.get("total_dur", 0), 2)),
                    cell(round(it.get("longest", 0), 2)),
                    cell(round(it.get("avg_conf", 0), 3)),
                    cell(len(it.get("segments", []))),
                    cell(round(t[0], 2)),
                    cell(round(t[1], 2)),
                    cell(round(t[2], 2)),
                    cell(it.get("file", "")),
                ]))
            body = ("\ufeff" + "\n".join(lines) + "\n").encode("utf-8")
            ctype = "text/csv; charset=utf-8"
            fname = f"action_{tag}_{stamp}.csv"

        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(fname)}")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self._serve_file(INDEX_PATH)
        elif path == "/tags":
            # 与首页合并后的别名，旧链接仍可用
            self._serve_file(INDEX_PATH)
        elif path == "/api/collect/status":
            self._json(_collect_status())
        elif path == "/api/links/status":
            self._json(_links_status())
        elif path == "/api/today":
            self._json(today(self.db))
        elif path == "/api/history":
            self._json(history(self.db))
        elif path == "/api/day":
            day = qs.get("date", [""])[0]
            self._json(_list_day(self.db, day))
        elif path == "/api/hottoday":
            self._json(load_hotlist_snapshot(datetime.now().strftime("%Y-%m-%d")))
        elif path == "/api/hotdays":
            self._json(list_hotlist_days())
        elif path == "/api/hotday":
            day = qs.get("date", [""])[0]
            self._json(load_hotlist_snapshot(day))
        elif path == "/api/subscriptions":
            self._json({"subscriptions": store.list_subscriptions(self.db), **_subscription_status()})
        elif path == "/api/subscriptions/videos":
            try:
                sid = int(qs.get("id", ["0"])[0])
            except (ValueError, TypeError):
                sid = 0
            sub = store.get_subscription(self.db, sid) if sid else None
            if not sub:
                self._json({"ok": False, "error": "订阅不存在"}, status=404)
                return
            account = (sub.get("account") or "").strip() or sub.get("name", "")
            records = store.downloads_by_account(self.db, account)
            self._json({
                "ok": True, "id": sid, "name": sub.get("name", ""),
                "account": account, "homepage": sub.get("homepage", ""),
                "records": records,
            })
        elif path == "/api/subscriptions/status":
            self._json(_subscription_status())
        elif path == "/api/digest/list":
            platform = qs.get("platform", [""])[0]
            author = qs.get("author", [""])[0]
            q = qs.get("q", [""])[0]
            try:
                limit = int(qs.get("limit", ["200"])[0])
            except (ValueError, TypeError):
                limit = 200
            items = store.list_digests(self.db, platform=platform, author=author, q=q, limit=limit)
            self._json({"ok": True, "items": items, "stats": store.digest_stats(self.db)})
        elif path == "/api/digest/detail":
            try:
                dig_id = int(qs.get("id", ["0"])[0])
            except (ValueError, TypeError):
                dig_id = 0
            item = store.get_digest(self.db, dig_id) if dig_id else None
            if not item:
                self._json({"ok": False, "error": "记录不存在"}, status=404)
                return
            doc_md = ""
            doc_abs = os.path.join(ROOT, str(item.get("doc_path") or ""))
            if item.get("doc_path") and os.path.isfile(doc_abs):
                with open(doc_abs, encoding="utf-8") as f:
                    doc_md = f.read()
            self._json({"ok": True, "item": item, "doc_md": doc_md})
        elif path == "/api/digest/status":
            self._json(_digest_status())
        elif path == "/api/digest/config":
            from clawdata.digest import llm as digest_llm

            cfg = digest_llm.load_config()
            cfg.pop("summary_prompt", None)
            cfg.pop("ask_prompt", None)
            self._json({"ok": True, "config": cfg})
        elif path == "/api/digest/doc":
            try:
                dig_id = int(qs.get("id", ["0"])[0])
            except (ValueError, TypeError):
                dig_id = 0
            item = store.get_digest(self.db, dig_id) if dig_id else None
            doc_abs = os.path.join(ROOT, str(item.get("doc_path") or "")) if item else ""
            if not doc_abs or not os.path.isfile(doc_abs):
                self._json({"ok": False, "error": "文档不存在"}, status=404)
                return
            self._serve_file(doc_abs, cache=True)
        elif path == "/api/tags":
            status = qs.get("status", ["all"])[0]
            try:
                limit = int(qs.get("limit", ["500"])[0])
            except (ValueError, TypeError):
                limit = 500
            records = [self._record_to_view(r) for r in store.list_for_tagging(
                self.db, status, qs.get("q", [""])[0], limit
            )]
            self._json({"records": records, "stats": store.tagging_stats(self.db)})
        elif path == "/api/tag/stats":
            worker = WORKER
            self._json({
                "stats": store.tagging_stats(self.db),
                "worker": worker.status() if worker else {"queued": [], "running": None, "done": 0, "errors": 0},
                "categories": (TAG_CONFIG or {}).get("categories", []),
                "server": (TAG_CONFIG or {}).get("server", ""),
            })
        elif path == "/api/tag/categories":
            self._json((TAG_CONFIG or {}).get("categories", []))
        elif path == "/api/tools":
            self._json({
                "tools": [{
                    "id": "action",
                    "name": "连续动作筛选",
                    "desc": "用 YOLO 人体姿态识别连续动作，按时长保留 Top3 最长片段；"
                            "其他候选分段仅参与分析，不导出资产。",
                }, {
                    "id": "pose_match",
                    "name": "首帧姿态匹配",
                    "desc": "上传起始姿态图，在姿态帧库中匹配以相近姿态开始的完整动作段。",
                }, {
                    "id": "tagging",
                    "name": "内容理解",
                    "desc": "批量勾选原视频或资产视频，用 Qwen3-VL 生成类别、标签和摘要。",
                }]
            })
        elif path == "/api/tools/action":
            status = qs.get("status", ["all"])[0]
            try:
                min_longest = float(qs.get("min_longest", ["0"])[0] or 0)
            except (ValueError, TypeError):
                min_longest = 0.0
            category = qs.get("category", [""])[0]
            q = qs.get("q", [""])[0]
            items = store.list_pose_items(
                self.db, status=status, min_longest=min_longest,
                category=category, q=q,
            )
            self._json({
                "stats": store.pose_stats(self.db),
                "frame_stats": store.pose_frame_stats(self.db),
                "collections": store.list_asset_collections(self.db),
                "items": items,
                "categories": (TAG_CONFIG or {}).get("categories", []),
            })
        elif path == "/api/tools/action/status":
            worker = POSE_WORKER
            self._json(worker.status() if worker else {"queued": [], "running": None, "done": 0, "errors": 0})
        elif path == "/api/tools/action/model":
            cfg = tools_filter.load_tool_config()
            model = cfg.get("model_path", "")
            import os as _os
            from clawdata.vision.pose_segment import _load_model
            ok = bool(model) and _os.path.isfile(model)
            providers = []
            if ok:
                try:
                    providers = list(_load_model(cfg).get_providers())
                except Exception as e:  # noqa: BLE001
                    ok = False
                    providers = [str(e)[:120]]
            self._json({"ok": ok, "model": model, "exists": ok, "providers": providers})
        elif path == "/api/tools/action/export":
            self._export_action(qs)
        elif path == "/api/tools/sources":
            source = qs.get("source", ["all"])[0]
            try:
                collection_id = int(qs.get("collection_id", ["0"])[0] or 0)
                limit = int(qs.get("limit", ["500"])[0])
                page = int(qs.get("page", ["1"])[0] or 1)
                page_size = int(qs.get("page_size", ["0"])[0] or 0)
            except (ValueError, TypeError):
                collection_id, limit, page, page_size = 0, 500, 1, 0
            result = store.list_tool_sources(
                self.db,
                source=source,
                q=qs.get("q", [""])[0],
                collection_id=collection_id,
                action_status=qs.get("action_status", ["all"])[0],
                tag_status=qs.get("tag_status", ["all"])[0],
                limit=limit,
                page=page,
                page_size=page_size,
                return_meta=bool(page_size),
            )
            self._json(result)
        elif path == "/api/tools/runs":
            self._json({"runs": store.list_tool_runs(
                self.db,
                tool_id=qs.get("tool_id", [""])[0],
                limit=max(1, min(int(qs.get("limit", ["20"])[0] or 20), 100)),
            )})
        elif path == "/api/tools/tagging":
            source = "asset" if qs.get("source", ["download"])[0] == "asset" else "download"
            status = qs.get("status", ["all"])[0]
            q = qs.get("q", [""])[0]
            kind = qs.get("kind", [""])[0]
            try:
                limit = int(qs.get("limit", ["500"])[0])
            except (ValueError, TypeError):
                limit = 500
            if source == "asset":
                items = store.list_asset_tag_sources(
                    self.db, status=status, q=q, kind=kind, limit=limit
                )
            else:
                items = store.list_download_tag_sources(
                    self.db, status=status, q=q, limit=limit
                )
            self._json({
                "source": source,
                "stats": store.tagging_tool_stats(self.db, source),
                "items": items,
                "collections": store.list_asset_collections(self.db),
                "categories": (TAG_CONFIG or {}).get("categories", []),
                "worker": WORKER.status() if WORKER else {},
            })
        elif path == "/api/action/frames":
            self._json({
                "stats": store.pose_frame_stats(self.db),
                "categories": (TAG_CONFIG or {}).get("categories", []),
            })
        elif path == "/api/assets":
            q = qs.get("q", [""])[0]
            source = qs.get("source", [""])[0]
            category = qs.get("category", [""])[0]
            kind = qs.get("kind", [""])[0]
            asset_type = qs.get("type", [""])[0]
            tool = qs.get("tool", [""])[0]
            tag = qs.get("tag", [""])[0]
            platform = qs.get("platform", [""])[0]
            try:
                run_id = int(qs.get("run_id", ["0"])[0] or 0)
            except (ValueError, TypeError):
                run_id = 0
            try:
                favorite = int(qs.get("favorite", ["0"])[0] or 0)
            except (ValueError, TypeError):
                favorite = 0
            try:
                page = max(1, int(qs.get("page", ["1"])[0] or 1))
            except (ValueError, TypeError):
                page = 1
            try:
                page_size = min(100, max(1, int(qs.get("page_size", ["24"])[0] or 24)))
            except (ValueError, TypeError):
                page_size = 24
            try:
                limit = int(qs.get("limit", ["500"])[0])
            except (ValueError, TypeError):
                limit = 500
            stats = store.asset_stats(self.db)
            configured_categories = {str(item) for item in ((TAG_CONFIG or {}).get("categories") or []) if str(item)}
            existing_options = {item.get("value") for item in stats.get("tag_options", [])}
            stats.setdefault("tag_options", []).extend(
                {"type": "category", "value": name, "label": name, "count": 0}
                for name in sorted(configured_categories - existing_options)
            )
            items, total = store.list_assets_page(
                self.db, q=q, source=source, category=category,
                kind=kind, asset_type=asset_type, tool=tool, tag=tag,
                platform=platform, run_id=run_id, favorite=favorite,
                page=page, page_size=page_size,
            )
            fav_map = store.assets_favorite_ids(self.db, [it["id"] for it in items])
            for it in items:
                it["fav_ids"] = fav_map.get(it["id"], [])
            self._json({
                "stats": stats,
                "items": items,
                "pagination": {
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "total_pages": max(1, (total + page_size - 1) // page_size),
                },
            })
        elif path == "/api/favorites":
            self._json({"favorites": store.list_favorites(self.db)})
        elif path == "/api/assets/detail":
            try:
                aid = int(qs.get("id", ["0"])[0])
            except (ValueError, TypeError):
                aid = 0
            chain = store.asset_chain(self.db, aid) if aid else None
            if not chain:
                self._json({"ok": False, "error": "资产不存在"}, status=404)
                return
            self._json({"ok": True, **chain})
        elif path.startswith("/asset-file/"):
            aid = path.rsplit("/", 1)[-1]
            if aid.isdigit():
                got = store.get_asset(self.db, int(aid))
                if got and got.get("file"):
                    abs_path = os.path.normpath(os.path.join(ROOT, got["file"]))
                    if abs_path.startswith(ROOT) and os.path.isfile(abs_path):
                        self._serve_file(abs_path)
                        return
            self.send_error(404, "asset not found")
        elif path.startswith("/asset-poster/"):
            aid = path.rsplit("/", 1)[-1]
            if not aid.isdigit():
                self.send_error(404, "poster not found")
                return
            asset = store.get_asset(self.db, int(aid))
            relative = (asset.get("source_file") if asset and asset.get("kind") == "download" else asset.get("file") if asset else "") or ""
            video_path = os.path.normpath(os.path.join(ROOT, relative))
            if not relative or not video_path.startswith(ROOT) or not os.path.isfile(video_path):
                self.send_error(404, "poster source not found")
                return
            poster_path = os.path.join(THUMBNAILS_DIR, f"{aid}.jpg")
            try:
                first_frame_thumbnail(video_path, poster_path)
            except Exception:
                self.send_error(500, "poster generation failed")
                return
            if os.path.isfile(poster_path):
                self._serve_file(poster_path, cache=True)
                return
            self.send_error(404, "poster not found")
        elif path.startswith("/segment/"):
            name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            seg = os.path.join(pose_segment.SEGMENTS_DIR, name)
            if name and os.path.isfile(seg):
                self._serve_file(seg)
                return
            self.send_error(404, "segment not found")
        elif path.startswith("/media/"):
            rid = path.rsplit("/", 1)[-1]
            if rid.isdigit():
                rec = get(self.db, int(rid))
                if rec and rec.get("file") and os.path.isfile(rec["file"]):
                    self._serve_file(rec["file"])
                    return
            self.send_error(404, "media not found")
        else:
            self.send_error(404, "not found")

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        body = self._read_body()
        worker = WORKER

        if path == "/api/collect/start":
            keywords = body.get("keywords") or []
            if isinstance(keywords, str):
                import re as _re

                keywords = [k for k in _re.split(r"[,，\s]+", keywords) if k]
            per_word = body.get("per_word", 5)
            category = str(body.get("category", "")).strip()
            headless = bool(body.get("headless", True))
            started = _start_collect(
                keywords, per_word=per_word, category=category, headless=headless
            )
            if not started:
                self._json({"ok": False, "error": "已有采集任务在运行"}, status=409)
                return
            self._json({"ok": True, **_collect_status()})
        elif path == "/api/collect/stop":
            with COLLECT_LOCK:
                if COLLECT_JOB and COLLECT_JOB.get("status") == "running":
                    COLLECT_JOB["stop"] = True
            self._json({"ok": True, **_collect_status()})
        elif path == "/api/hot/refresh":
            try:
                count, day = refresh_today_hot_events()
                self._json({"ok": True, "count": count, "day": day})
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, status=500)
        elif path == "/api/hot/download":
            word = str(body.get("word", "")).strip()
            per_word = max(1, min(int(body.get("count", 3) or 3), 20))
            if not word:
                self._json({"ok": False, "error": "缺少热点关键词"}, status=400)
                return
            started = _start_collect([word], per_word=per_word, category=word, headless=True)
            if not started:
                self._json({"ok": False, "error": "已有采集任务在运行"}, status=409)
                return
            self._json({"ok": True, **_collect_status()})
        elif path == "/api/links/download":
            text = str(body.get("text") or "")
            category = str(body.get("category", "")).strip()
            lines = [ln for ln in re.split(r"[\n\r]+", text)
                     if ln.strip() and not ln.strip().startswith("#")]
            if not lines:
                self._json({"ok": False, "error": "请粘贴至少一条视频链接"}, status=400)
                return
            started = _start_links_job("links", {"text": text, "category": category})
            if not started:
                self._json({"ok": False, "error": "已有下载任务在运行"}, status=409)
                return
            self._json({"ok": True, **_links_status()})
        elif path == "/api/bili/collect":
            mode = str(body.get("mode") or "popular")
            keyword = str(body.get("keyword") or "").strip()
            if mode not in ("popular", "keyword") or (mode == "keyword" and not keyword):
                self._json({"ok": False, "error": "模式不合法或缺少关键词"}, status=400)
                return
            try:
                count = max(1, min(int(body.get("count", 5) or 5), 30))
            except (ValueError, TypeError):
                count = 5
            started = _start_links_job(f"bili_{mode}", {
                "keyword": keyword, "count": count,
                "category": str(body.get("category", "")).strip() or (keyword or "B站热门"),
            })
            if not started:
                self._json({"ok": False, "error": "已有下载任务在运行"}, status=409)
                return
            self._json({"ok": True, **_links_status()})
        elif path == "/api/favorites/create":
            name = str(body.get("name", "")).strip()
            if not name:
                self._json({"ok": False, "error": "请填写收藏夹名称"}, status=400)
                return
            fid = store.create_favorite(self.db, name)
            if not fid:
                self._json({"ok": False, "error": "创建失败（可能重名）"}, status=409)
                return
            self._json({"ok": True, "id": fid, "name": name})
        elif path == "/api/favorites/rename":
            try:
                fid = int(body.get("id", 0) or 0)
            except (ValueError, TypeError):
                fid = 0
            name = str(body.get("name", "")).strip()
            if not fid or not name:
                self._json({"ok": False, "error": "参数不完整"}, status=400)
                return
            ok = store.rename_favorite(self.db, fid, name)
            self._json({"ok": ok, "error": "" if ok else "重命名失败（可能重名）"})
        elif path == "/api/favorites/delete":
            try:
                fid = int(body.get("id", 0) or 0)
            except (ValueError, TypeError):
                fid = 0
            if not fid:
                self._json({"ok": False, "error": "参数不完整"}, status=400)
                return
            ok = store.delete_favorite(self.db, fid)
            self._json({"ok": ok, "error": "" if ok else "收藏夹不存在"})
        elif path == "/api/favorites/add":
            try:
                fid = int(body.get("id", 0) or 0)
            except (ValueError, TypeError):
                fid = 0
            asset_ids = [int(a) for a in (body.get("asset_ids") or []) if str(a).isdigit()]
            if not fid or not asset_ids:
                self._json({"ok": False, "error": "参数不完整"}, status=400)
                return
            count = store.favorite_add(self.db, fid, asset_ids)
            self._json({"ok": True, "count": count})
        elif path == "/api/favorites/remove":
            try:
                fid = int(body.get("id", 0) or 0)
            except (ValueError, TypeError):
                fid = 0
            asset_ids = [int(a) for a in (body.get("asset_ids") or []) if str(a).isdigit()]
            if not fid or not asset_ids:
                self._json({"ok": False, "error": "参数不完整"}, status=400)
                return
            count = store.favorite_remove(self.db, fid, asset_ids)
            self._json({"ok": True, "count": count})
        elif path == "/api/downloads/prune":
            result = store.prune_non_keyword_downloads(self.db)
            deleted_files = 0
            for rel in result.get("files", []):
                abs_path = os.path.normpath(os.path.join(ROOT, rel))
                downloads_root = os.path.normpath(os.path.join(ROOT, "downloads"))
                if not abs_path.startswith(downloads_root + os.sep):
                    continue
                if os.path.isfile(abs_path):
                    try:
                        os.remove(abs_path)
                        deleted_files += 1
                    except OSError:
                        pass
            result["files_deleted"] = deleted_files
            self._json({"ok": True, **result})
        elif path == "/api/subscriptions/add":
            name = str(body.get("name", "")).strip()
            homepage = str(body.get("homepage", body.get("account", ""))).strip()
            category = str(body.get("category", "订阅博主")).strip()
            interval = int(body.get("interval_hours", 6) or 6)
            if not name or not homepage:
                self._json({"ok": False, "error": "请填写博主名称和主页"}, status=400)
                return
            sid = store.add_subscription(
                self.db, name=name, homepage=homepage, category=category,
                interval_hours=interval,
            )
            self._json({"ok": True, "id": sid, **_subscription_status()})
        elif path == "/api/subscriptions/refresh":
            sid = int(body.get("id", 0) or 0)
            started = _start_subscription_refresh(sid)
            if not started:
                self._json({"ok": False, "error": "订阅更新已在运行"}, status=409)
                return
            self._json({"ok": True, **_subscription_status()})
        elif path == "/api/subscriptions/delete":
            sid = int(body.get("id", 0) or 0)
            self._json({"ok": bool(sid) and store.delete_subscription(self.db, sid)})
        elif path == "/api/digest/run":
            sid = int(body.get("subscription_id", 0) or 0)
            try:
                limit = int(body.get("limit", 0) or 0)
            except (ValueError, TypeError):
                limit = 0
            started = _start_digest_job(sid, limit)
            if not started:
                self._json({"ok": False, "error": "整理任务已在运行"}, status=409)
                return
            self._json({"ok": True, **_digest_status()})
        elif path == "/api/digest/ask":
            from clawdata.digest import service as digest_service

            q = str(body.get("q", "")).strip()
            if not q:
                self._json({"ok": False, "error": "请输入问题"}, status=400)
                return
            try:
                result = digest_service.ask(self.db, q)
                self._json({"ok": True, **result})
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)[:500]}, status=500)
        elif path == "/api/digest/redigest":
            from clawdata.digest import service as digest_service

            dig_id = int(body.get("id", 0) or 0)
            if not dig_id:
                self._json({"ok": False, "error": "缺少 id"}, status=400)
                return
            item = digest_service.redigest(self.db, dig_id)
            if not item:
                self._json({"ok": False, "error": "记录不存在"}, status=404)
                return
            self._json({"ok": True, "item": item})
        elif path == "/api/digest/delete":
            dig_id = int(body.get("id", 0) or 0)
            doc_path = store.delete_digest(self.db, dig_id) if dig_id else ""
            if doc_path:
                abs_path = os.path.join(ROOT, doc_path)
                if os.path.isfile(abs_path):
                    try:
                        os.remove(abs_path)
                    except OSError:
                        pass
            self._json({"ok": bool(dig_id)})
        elif path == "/api/digest/config":
            from clawdata.digest import llm as digest_llm

            updates = {
                "focus_points": [str(x).strip() for x in body.get("focus_points", []) if str(x).strip()],
                "per_subscription_limit": max(1, min(int(body.get("per_subscription_limit", 5) or 5), 20)),
                "subtitle_max_chars": max(1000, int(body.get("subtitle_max_chars", 12000) or 12000)),
            }
            try:
                digest_llm.save_config(updates)
            except Exception as exc:  # noqa: BLE001
                self._json({"ok": False, "error": str(exc)[:300]}, status=500)
                return
            self._json({"ok": True})
        elif path == "/api/tag":
            rid = int(body.get("id") or 0)
            rec = store.get_record_by_id(self.db, rid) if rid else None
            if not rec or not rec.get("file"):
                self._json({"ok": False, "error": "记录不存在或无视频文件"}, status=400)
                return
            file = rec["file"]
            # 把同一文件的所有记录置为处理中
            for r in store.list_for_tagging(self.db):
                if r.get("file") == file:
                    store.set_tag_status(self.db, r["id"], "tagging")
            queued = worker.enqueue(file) if worker else False
            self._json({"ok": True, "queued": queued, "file": file, "title": rec.get("title", "")})
        elif path == "/api/tagall":
            force = bool(body.get("force"))
            files = store.untagged_video_files(self.db, force=force)
            n = 0
            if worker:
                for file in files:
                    if worker.enqueue(file):
                        n += 1
            self._json({"ok": True, "total": len(files), "queued": n})
        elif path == "/api/tag/update":
            rid = int(body.get("id") or 0)
            category = str(body.get("category", ""))
            tags = body.get("tags", [])
            summary = str(body.get("summary", ""))
            if not rid:
                self._json({"ok": False, "error": "缺少 id"}, status=400)
                return
            if not isinstance(tags, list):
                tags = [str(tags)] if tags else []
            store.set_tag(self.db, rid, category=category, tags=tags, summary=summary, status="tagged", error="")
            self._json({"ok": True})
        elif path == "/api/tools/action/analyze":
            file = str(body.get("file", ""))
            output_name = str(body.get("output_name", body.get("collection_name", ""))).strip()
            collection_id = store.ensure_asset_collection(
                self.db, output_name,
                tool_id="action", tool_name="连续动作筛选",
            ) if output_name else None
            queued = bool(file) and bool(POSE_WORKER)
            if queued:
                queued = POSE_WORKER.enqueue({"file": file, "key": f"download:{file}"}, collection_id)
            self._json({
                "ok": bool(file), "queued": queued, "file": file,
                "output_name": output_name, "collection_id": collection_id,
            })
        elif path == "/api/tools/action/analyze-all":
            force = bool(body.get("force"))
            selected = body.get("files")
            if isinstance(selected, list):
                known_files = set(store.distinct_video_files(self.db))
                files = [str(f) for f in selected if str(f) in known_files]
                if not force:
                    analyzed_files = {
                        x["file"] for x in store.list_pose_items(self.db, status="analyzed")
                        if x.get("file")
                    }
                    files = [f for f in files if f not in analyzed_files]
            else:
                files = store.unanalyzed_pose_files(self.db, force=force)
            output_name = str(body.get("output_name", body.get("collection_name", ""))).strip()
            if not output_name:
                self._json({"ok": False, "error": "请填写输出资产名"}, status=400)
                return
            collection_id = store.ensure_asset_collection(
                self.db, output_name,
                tool_id="action", tool_name="连续动作筛选",
            )
            n = 0
            if POSE_WORKER:
                for file in files:
                    if POSE_WORKER.enqueue({"file": file, "key": f"download:{file}"}, collection_id):
                        n += 1
            self._json({
                "ok": True, "total": len(files), "queued": n,
                "output_name": output_name, "collection_id": collection_id,
            })
        elif path == "/api/tools/action/runs":
            selected_keys = [str(v) for v in (body.get("items") or []) if str(v).strip()]
            output_name = str(body.get("output_name", body.get("collection_name", ""))).strip()
            force = bool(body.get("force"))
            output_mode = body.get("output_mode") or "both"
            output_category = str(body.get("output_category", "")).strip()
            if output_mode not in ("source", "collection", "both"):
                self._json({"ok": False, "error": "输出策略无效"}, status=400)
                return
            if output_mode in ("collection", "both") and not output_name:
                self._json({"ok": False, "error": "请填写保存资产名"}, status=400)
                return
            sources = {x["key"]: x for x in store.list_tool_sources(self.db, limit=5000)}
            targets = [sources[k] for k in selected_keys if k in sources and sources[k]["exists"]]
            if not force:
                targets = [x for x in targets if x.get("action_status") != "analyzed"]
            collection_id = store.ensure_asset_collection(
                self.db, output_name, tool_id="action", tool_name="连续动作筛选"
            ) if output_mode in ("collection", "both") else 0
            run_id = store.create_tool_run(
                self.db, tool_id="action", tool_name="连续动作筛选",
                total_count=len(targets), output_collection_id=collection_id,
                params={"output_mode": output_mode, "output_name": output_name,
                        "output_category": output_category, "force": force},
            )
            queued = 0
            if not targets:
                store.finish_empty_tool_run(self.db, run_id)
            elif POSE_WORKER:
                for item in targets:
                    if POSE_WORKER.enqueue(item, collection_id, run_id,
                                           output_mode=output_mode,
                                           output_name=output_name,
                                           output_category=output_category):
                        queued += 1
            self._json({
                "ok": True, "run_id": run_id, "total": len(targets),
                "queued": queued, "output_mode": output_mode,
                "output_name": output_name,
                "collection_id": collection_id,
            })
        elif path == "/api/tools/tagging/runs":
            selected_keys = [str(v) for v in (body.get("items") or []) if str(v).strip()]
            force = bool(body.get("force"))
            output_mode = body.get("output_mode") or "source"
            output_name = str(body.get("output_name", body.get("collection_name", ""))).strip()
            output_category = str(body.get("output_category", "")).strip()
            if output_mode not in ("source", "collection", "both"):
                self._json({"ok": False, "error": "输出策略无效"}, status=400)
                return
            if output_mode in ("collection", "both") and not output_name:
                self._json({"ok": False, "error": "请填写保存资产名"}, status=400)
                return
            sources = {x["key"]: x for x in store.list_tool_sources(self.db, limit=5000)}
            targets = [sources[k] for k in selected_keys if k in sources and sources[k]["exists"]]
            if not force:
                targets = [x for x in targets if x.get("tag_status") not in ("tagged", "tagging")]
            collection_id = store.ensure_asset_collection(
                self.db, output_name, tool_id="tagging", tool_name="内容理解"
            ) if output_mode in ("collection", "both") else 0
            run_id = store.create_tool_run(
                self.db, tool_id="tagging", tool_name="内容理解",
                total_count=len(targets), output_collection_id=collection_id,
                params={"output_mode": output_mode, "output_name": output_name,
                        "output_category": output_category, "force": force},
            )
            queued = 0
            if not targets:
                store.finish_empty_tool_run(self.db, run_id)
            elif WORKER:
                for item in targets:
                    queued += int(
                        WORKER._enqueue_asset_job(
                            item["id"], item["file"], run_id,
                            output_mode=output_mode, output_name=output_name,
                            output_category=output_category,
                        )
                        if item["source_type"] == "asset"
                        else WORKER._enqueue_download_job(
                            item["file"], run_id,
                            output_mode=output_mode, output_name=output_name,
                            output_category=output_category,
                        )
                    )
            self._json({
                "ok": True, "run_id": run_id,
                "total": len(targets), "queued": queued,
                "output_mode": output_mode, "output_name": output_name,
            })
        elif path == "/api/tools/tagging/run":
            source = "asset" if body.get("source_type", body.get("source", "download")) == "asset" else "download"
            force = bool(body.get("force"))
            queued = 0
            total = 0
            if source == "asset":
                ids = [int(v) for v in (body.get("ids") or []) if str(v).strip().isdigit()]
                available = {
                    item["id"]: item
                    for item in store.list_asset_tag_sources(self.db, limit=2000)
                }
                targets = [available[aid] for aid in ids if aid in available]
                if not force:
                    targets = [x for x in targets if x.get("status") not in ("tagged", "tagging")]
                total = len(targets)
                if WORKER:
                    for item in targets:
                        if WORKER.enqueue_asset(int(item["id"])):
                            queued += 1
            else:
                selected_files = [str(v) for v in (body.get("files") or []) if str(v).strip()]
                known_files = set(store.distinct_video_files(self.db))
                available = {
                    item["file"]: item
                    for item in store.list_download_tag_sources(self.db, limit=2000)
                }
                targets = [available[f] for f in selected_files if f in known_files and f in available]
                if not force:
                    targets = [x for x in targets if x.get("status") not in ("tagged", "tagging")]
                total = len(targets)
                if WORKER:
                    for item in targets:
                        if WORKER.enqueue_download(item["file"]):
                            queued += 1
            self._json({
                "ok": True, "source_type": source,
                "total": total, "queued": queued,
            })
        elif path == "/api/action/match":
            try:
                query = None
                query_meta = {"type": "descriptor"}
                image_data = str(body.get("image", "")).strip()
                if image_data:
                    if image_data.startswith("data:"):
                        image_data = image_data.split(",", 1)[-1]
                    raw = base64.b64decode(image_data, validate=True)
                    if len(raw) > 12 * 1024 * 1024:
                        raise ValueError("图片不能超过 12MB")
                    result = pose_segment.descriptor_from_image(
                        raw, tools_filter.load_tool_config()
                    )
                    if not result:
                        self._json({"ok": False, "error": "图中未检测到人体姿态"}, status=400)
                        return
                    query = result["descriptor"]
                    query_meta = {
                        "type": "image",
                        "confidence": result["confidence"],
                        "bbox": result["bbox"],
                    }
                else:
                    frame_idx = int(body.get("frame_idx", -1))
                    source_file = str(body.get("file", "")).strip()
                    source_id = int(body.get("source_id", 0) or 0)
                    if source_id and not source_file:
                        rec = get(self.db, source_id)
                        source_file = rec.get("file", "") if rec else ""
                    if source_file and frame_idx >= 0:
                        query = store.get_pose_frame_descriptor(
                            self.db, source_file, frame_idx
                        )
                        query_meta = {
                            "type": "frame", "file": source_file, "frame_idx": frame_idx
                        }
                    else:
                        query = body.get("descriptor")
                if query is None:
                    raise ValueError("请提供姿态图片、库内起始帧或 34 维描述子")
                matches = store.search_pose_sequences(
                    self.db,
                    query,
                    top_k=max(1, min(int(body.get("top_k", 20) or 20), 100)),
                    min_score=max(0.0, min(float(body.get("min_score", 0.55) or 0), 1.0)),
                    category=str(body.get("category", "")).strip(),
                    q=str(body.get("q", "")).strip(),
                )
                self._json({
                    "ok": True,
                    "query": query_meta,
                    "frame_stats": store.pose_frame_stats(self.db),
                    "matches": matches,
                })
            except (ValueError, TypeError, binascii.Error) as e:
                self._json({"ok": False, "error": str(e)}, status=400)
            except Exception as e:
                self._json({"ok": False, "error": f"匹配失败：{e}"}, status=500)
        elif path == "/api/action/frame-descriptor":
            try:
                source_key = str(body.get("source_key", "")).strip()
                time_sec = float(body.get("time_sec") or 0)
                if not source_key.startswith(("download:", "asset:")):
                    raise ValueError("请先选择原视频或资产视频")
                source_type, raw_id = source_key.split(":", 1)
                if source_type == "asset":
                    if not raw_id.isdigit():
                        raise ValueError("资产 ID 无效")
                    asset = store.get_asset(self.db, int(raw_id))
                    if not asset or not asset.get("file"):
                        raise ValueError("资产视频不存在")
                    file = asset["file"]
                else:
                    file = raw_id
                abs_path = os.path.normpath(os.path.join(ROOT, file))
                if not abs_path.startswith(ROOT) or not os.path.isfile(abs_path):
                    raise ValueError("视频文件不存在")
                result = pose_segment.descriptor_from_video_time(
                    abs_path, time_sec, tools_filter.load_tool_config()
                )
                if not result:
                    self._json({"ok": False, "error": "当前帧未检测到人体姿态"}, status=400)
                    return
                self._json({
                    "ok": True,
                    "descriptor": result["descriptor"],
                    "confidence": result["confidence"],
                    "bbox": result["bbox"],
                    "preview": result["preview"],
                    "time_sec": time_sec,
                    "source_key": source_key,
                })
            except (ValueError, TypeError) as e:
                self._json({"ok": False, "error": str(e)}, status=400)
            except Exception as e:
                self._json({"ok": False, "error": f"截帧失败：{e}"}, status=500)
        elif path == "/api/tools/pose-match/runs":
            matches = body.get("matches") or []
            output_name = str(body.get("output_name", body.get("collection_name", ""))).strip()
            output_category = str(body.get("output_category", "")).strip()
            output_mode = body.get("output_mode") or "both"
            if output_mode not in ("source", "collection", "both"):
                self._json({"ok": False, "error": "输出策略无效"}, status=400)
                return
            if output_mode in ("collection", "both") and not output_name:
                self._json({"ok": False, "error": "请填写保存资产名"}, status=400)
                return
            valid = []
            for item in matches:
                if not isinstance(item, dict):
                    continue
                file = str(item.get("file", "")).strip()
                abs_path = os.path.normpath(os.path.join(ROOT, file))
                if not abs_path.startswith(ROOT) or not os.path.isfile(abs_path):
                    continue
                try:
                    start = float(item.get("start") or 0)
                    end = float(item.get("end") or 0)
                except (ValueError, TypeError):
                    continue
                if end <= start:
                    continue
                valid.append({**item, "file": os.path.relpath(abs_path, ROOT), "start": start, "end": end})
            collection_id = store.ensure_asset_collection(
                self.db, output_name, tool_id="pose_match", tool_name="首帧姿态匹配"
            ) if output_mode in ("collection", "both") else 0
            run_id = store.create_tool_run(
                self.db, tool_id="pose_match", tool_name="首帧姿态匹配",
                total_count=len(valid), output_collection_id=collection_id,
                params={"output_mode": output_mode, "output_name": output_name,
                        "output_category": output_category},
            )
            created = 0
            if not valid:
                store.finish_empty_tool_run(self.db, run_id)
            else:
                for index, item in enumerate(valid, 1):
                    try:
                        clips = pose_segment.extract_clips(
                            os.path.join(ROOT, item["file"]),
                            [{"start": item["start"], "end": item["end"], "duration": item["end"] - item["start"]}],
                            top_k=1,
                        )
                        for clip in clips:
                            clip_rel = os.path.normpath(os.path.relpath(clip["path"], ROOT))
                            store.register_asset(
                                self.db, file=clip_rel, source_file=item["file"],
                                size=int(os.path.getsize(clip["path"])),
                                tool_id="pose_match", tool_name="首帧姿态匹配",
                                collection_id=collection_id, run_id=run_id,
                                category=output_category,
                            )
                            created += 1
                        if output_mode in ("source", "both") and item.get("source_key"):
                            store.set_tool_source_category(
                                self.db, item["source_key"],
                                output_category or output_name, status="matched",
                            )
                        store.update_tool_run_progress(self.db, run_id, success=True)
                    except Exception as e:
                        store.update_tool_run_progress(
                            self.db, run_id, success=False, error=str(e)[:500]
                        )
            self._json({
                "ok": True, "run_id": run_id, "total": len(valid),
                "created": created, "output_mode": output_mode,
                "output_name": output_name,
            })
        elif path == "/api/assets/sync":
            result = store.sync_assets(self.db)
            self._json({"ok": True, **result})
        elif path == "/api/assets/register":
            file = str(body.get("file", "")).strip()
            if not file:
                self._json({"ok": False, "error": "缺少 file"}, status=400)
                return
            abs_path = os.path.normpath(os.path.join(ROOT, file))
            if not abs_path.startswith(ROOT) or not os.path.isfile(abs_path):
                self._json({"ok": False, "error": "文件不存在或不在项目目录内"}, status=400)
                return
            tool_id = str(body.get("tool_id", "")).strip()
            tool_name = str(body.get("tool_name", "")).strip()
            if not tool_id or not tool_name:
                self._json({"ok": False, "error": "缺少 tool_id 或 tool_name"}, status=400)
                return
            store.register_asset(
                self.db,
                file=os.path.relpath(abs_path, ROOT),
                source_file=str(body.get("source_file", "")).strip(),
                size=int(body.get("size") or os.path.getsize(abs_path)),
                category=str(body.get("category", "")).strip(),
                tool_id=tool_id,
                tool_name=tool_name,
                kind="tool",
                collection_id=int(body.get("collection_id") or 0) or None,
                collection_name=str(body.get("collection_name", body.get("output_name", ""))).strip(),
            )
            self._json({"ok": True})
        elif path == "/api/assets/category":
            aid = int(body.get("id") or 0)
            category = str(body.get("category", "")).strip()
            ok = bool(aid) and store.set_asset_category(self.db, aid, category)
            self._json({"ok": ok}, status=200 if ok else 400)
        elif path == "/api/assets/delete":
            aid = int(body.get("id") or 0)
            got = store.delete_asset(self.db, aid) if aid else None
            removed_file = False
            delete_file = bool(body.get("delete_file", got and got.get("kind") != "download"))
            if got and got.get("file") and delete_file:
                abs_path = os.path.join(ROOT, got["file"])
                if os.path.isfile(abs_path):
                    try:
                        os.remove(abs_path)
                        removed_file = True
                    except OSError:
                        removed_file = False
            self._json({"ok": bool(got), "removed_file": removed_file, "asset": got})
        elif path == "/api/tag/status":
            self._json(worker.status() if worker else {"queued": [], "running": None, "done": 0, "errors": 0})
        elif path == "/api/tag/ping":
            client = ComfyClient((TAG_CONFIG or {}).get("server", "http://10.168.1.106:8818"))
            self._json({"ok": client.ping()})
        else:
            self.send_error(404, "not found")


def _list_day(db: str, day: str) -> list[dict]:
    from clawdata.storage.store import list_by_day

    return list_by_day(db, day)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="抖音抓取结果网页面板")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--host", default="127.0.0.1",
                        help="绑定地址；0.0.0.0 表示开放给局域网访问")
    parser.add_argument("--db", default=DEFAULT_DB_PATH)
    parser.add_argument("--config", default=DEFAULT_TAGGING_CONFIG_PATH)
    args = parser.parse_args(argv)

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    init(args.db)
    global WORKER, TAG_CONFIG, POSE_WORKER, SUBSCRIPTION_SCHEDULER
    TAG_CONFIG = load_config(args.config)
    WORKER = build_worker(args.db, TAG_CONFIG)
    WORKER.start()
    POSE_WORKER = tools_filter.build_pose_worker(args.db, tools_filter.load_tool_config())
    POSE_WORKER.start()
    SUBSCRIPTION_SCHEDULER = threading.Thread(target=_subscription_scheduler_loop, daemon=True, name="subscription-scheduler")
    SUBSCRIPTION_SCHEDULER.start()
    try:
        store.sync_assets(args.db)
    except Exception as e:  # noqa: BLE001
        print(f"资产同步跳过：{e}")
    Handler.db = args.db
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"面板已启动：http://{args.host}:{args.port}")
    if args.host == "0.0.0.0":
        import socket

        try:
            lan_ips = {
                ip
                for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                for ip in [info[4][0]]
                if not ip.startswith("127.")
            }
        except OSError:
            lan_ips = set()
        for ip in sorted(lan_ips):
            print(f"局域网访问：http://{ip}:{args.port}")
    print(f"打标签服务：{TAG_CONFIG.get('server')}（{WORKER.status().get('queued', [])} 个任务排队中）")
    print(f"连续动作拆分：姿态模型 {'就绪' if tools_filter.load_tool_config().get('model_path') else '缺失'}")
    print("按 Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
