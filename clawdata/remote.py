"""远程素材源：把局域网内其他素材库当本机的素材来用。

支持两类源（添加时自动探测）：
- clawdata：另一台 clawdata 面板——浏览/搜索走其只读 API，播放代理 `/media/<id>`，
  单条「拉取到本地」走迁移 zip 通道（按视频 ID 去重）。
- videoshare：video-share 文件素材库（同款服务，见 D:\\project\\aitoeren\\video-share）
  ——浏览/搜索走 `/api/list`，播放代理 `/api/stream?id=`，单条拉取 = `/api/download`
  下载入库（按 `vs_<hash>` 键去重）。可选 Bearer Token 鉴权。

源清单存 `config/remote.json`：
    {"sources": [{"id": "s1", "name": "105素材库", "base": "http://10.168.1.105:8000",
                  "enabled": true, "type": "clawdata", "token": ""}]}
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.parse
import urllib.request

from clawdata.core.paths import DEFAULT_DB_PATH, DOWNLOADS_DIR, DEFAULT_REMOTE_CONFIG_PATH
from clawdata.storage import migrate, store

# 只调用远端的只读 GET 端点；不做任意路径代理，避免本机面板变成远端的写入口
CHECK_TIMEOUT = 3.0
FETCH_TIMEOUT = 15.0
VS_MAX_PAGES = 20  # videoshare 全量翻页上限（每页 500 条）


# ------------------------------------------------------------ 源清单
def load_sources(path: str = DEFAULT_REMOTE_CONFIG_PATH) -> list[dict]:
    """读取源清单（无文件时返回空列表，字段规范化）。"""
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (ValueError, OSError):
            data = {}
    sources = []
    for s in data.get("sources") or []:
        if not isinstance(s, dict) or not str(s.get("base") or "").strip():
            continue
        stype = str(s.get("type") or "").strip()
        if stype not in ("clawdata", "videoshare"):
            stype = "clawdata"  # 旧配置无 type 字段，默认同构面板
        sources.append({
            "id": str(s.get("id") or ""),
            "name": str(s.get("name") or "").strip() or str(s["base"]).rstrip("/"),
            "base": str(s["base"]).strip().rstrip("/"),
            "enabled": bool(s.get("enabled", True)),
            "type": stype,
            "token": str(s.get("token") or ""),
        })
    return sources


def save_sources(sources: list[dict], path: str = DEFAULT_REMOTE_CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sources": sources}, f, ensure_ascii=False, indent=2)


def get_source(sid: str, path: str = DEFAULT_REMOTE_CONFIG_PATH) -> dict | None:
    for s in load_sources(path):
        if s["id"] == sid:
            return s
    return None


def _new_id(existing: list[str]) -> str:
    n = 1
    while f"s{n}" in existing:
        n += 1
    return f"s{n}"


# ------------------------------------------------------------ HTTP 客户端
def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"} if token else {}


def fetch_json(url: str, timeout: float = FETCH_TIMEOUT, token: str = ""):
    req = urllib.request.Request(url, headers=_headers(token))
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ------------------------------------------------------------ 探活与类型探测
def detect_source(base: str, token: str = "") -> tuple[str, dict]:
    """探测对面是什么：返回 (类型, 首个响应数据)，都不匹配抛 ValueError。
    videoshare 命中时 data 为 /api/stats 结果，clawdata 为 /api/history 结果，
    供 check_source 复用避免二次往返。"""
    base = base.rstrip("/")
    try:
        data = fetch_json(f"{base}/api/stats", timeout=CHECK_TIMEOUT, token=token)
        if isinstance(data, dict) and data.get("service") == "video-share":
            return "videoshare", data
    except Exception:  # noqa: BLE001
        pass
    data = fetch_json(f"{base}/api/history", timeout=CHECK_TIMEOUT, token=token)
    if isinstance(data, list):
        return "clawdata", data
    raise ValueError(f"{base} 响应异常，既不是 clawdata 面板也不是 video-share 服务")


def check_source(base: str, token: str = "") -> dict:
    """探活 + 识别类型（单次请求）。返回 {type, reachable, days, records, detail}；失败抛 ValueError。"""
    stype, data = detect_source(base, token)
    if stype == "videoshare":
        counts = data.get("counts") or {}
        return {"type": stype, "reachable": True, "days": 0,
                "records": int(counts.get("videos") or 0),
                "detail": str(data.get("root") or "")}
    total = sum(int(d.get("count") or 0) for d in data if isinstance(d, dict))
    return {"type": stype, "reachable": True, "days": len(data), "records": total, "detail": ""}


def source_state(src: dict) -> dict:
    """探活的非抛错版本（含配置类型与实际服务的失配检测），用于源列表展示。"""
    try:
        info = check_source(src["base"], src.get("token", ""))
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": str(exc)[:160], "days": 0, "records": 0}
    if info.get("type") and info["type"] != src.get("type"):
        info["type_mismatch"] = True
        info["error"] = (f"远端服务已变为 {info['type']}（源配置为 {src.get('type')}），"
                         "请删除后重新添加")
    return info


def add_source(name: str, base: str, token: str = "",
               path: str = DEFAULT_REMOTE_CONFIG_PATH) -> dict:
    """探活验证（自动识别类型）后加入清单，返回新源。"""
    base = base.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("地址需以 http:// 或 https:// 开头")
    info = check_source(base, token)  # 不可达/类型不识别时抛 ValueError
    sources = load_sources(path)
    if any(s["base"] == base for s in sources):
        raise ValueError("该地址已在源清单中")
    src = {
        "id": _new_id([s["id"] for s in sources]),
        "name": name.strip() or base,
        "base": base,
        "enabled": True,
        "type": info["type"],
        "token": token.strip(),
    }
    sources.append(src)
    save_sources(sources, path)
    return src


# ------------------------------------------------------------ 浏览（按类型分派）
def remote_history(src: dict) -> list[dict]:
    """按天汇总。clawdata 走 /api/history；videoshare 按 mtime 聚合全量视频。"""
    if src.get("type") == "videoshare":
        agg: dict = {}
        for it in _vs_list_all(src, kind="video"):
            day = str(it.get("mtime") or "")[:10]
            if not day:
                continue
            a = agg.setdefault(day, {"day": day, "count": 0, "ok": 0, "size": 0})
            a["count"] += 1
            a["ok"] += 1
            a["size"] += int(it.get("size") or 0)
        return sorted(agg.values(), key=lambda x: x["day"], reverse=True)
    return fetch_json(f"{src['base']}/api/history", token=src.get("token", ""))


def remote_records(src: dict, day: str = "", q: str = "", limit: int = 200,
                   kind: str = "video") -> list[dict]:
    """拉远端素材记录，统一映射成 {id,title,author,word,size,created_at,day,file,
    kind,playable,pullable}。q 非空走全库搜索；day 过滤某天；缺省=最新。"""
    if src.get("type") == "videoshare":
        return _vs_records(src, day=day, q=q, limit=limit, kind=kind)
    base = src["base"]
    token = src.get("token", "")
    if q:
        data = fetch_json(
            f"{base}/api/tags?status=all&limit={int(limit)}"
            f"&q={urllib.parse.quote(q)}", token=token)
        rows = data.get("records") or []
    elif day:
        rows = fetch_json(f"{base}/api/day?date={urllib.parse.quote(day)}", token=token) or []
    else:
        rows = fetch_json(f"{base}/api/today", token=token) or []
    out = []
    for r in rows:
        rec = dict(r)
        rec["playable"] = bool(r.get("file"))
        rec["pullable"] = True
        out.append(rec)
    return out


def media_url(src: dict, rid: str) -> str:
    """远端媒体直连地址（供代理转发/入库引用）。"""
    rid = urllib.parse.quote(str(rid))
    if src.get("type") == "videoshare":
        return f"{src['base']}/api/stream?id={rid}"
    return f"{src['base']}/media/{rid}"


# ------------------------------------------------------------ videoshare 适配
def _vs_list_all(src: dict, kind: str = "video") -> list[dict]:
    """分页拉 video-share 全量素材（上限 VS_MAX_PAGES 页）。"""
    base, token, items, page = src["base"], src.get("token", ""), [], 1
    while page <= VS_MAX_PAGES:
        url = f"{base}/api/list?recursive=1&kind={urllib.parse.quote(kind)}&page={page}&size=500"
        data = fetch_json(url, token=token)
        batch = data.get("items") or []
        items.extend(batch)
        if not batch or page >= int(data.get("pages") or 1):
            break
        page += 1
    return items


def _vs_record(it: dict) -> dict:
    rel = str(it.get("rel") or "")
    mtime = str(it.get("mtime") or "")
    kind = str(it.get("kind") or "")
    parent = rel.replace("\\", "/").rsplit("/", 1)[0] if ("/" in rel.replace("\\", "/")) else ""
    return {
        "id": str(it.get("id") or ""),
        "title": str(it.get("name") or rel),
        "author": parent,
        "word": "远程文件",
        "size": int(it.get("size") or 0),
        "created_at": mtime.replace("T", " ")[:16],
        "day": mtime[:10],
        "file": rel,  # 非空 → 远端有文件
        "kind": kind,
        "aweme_id": rel,
        "url": rel,
        "playable": kind in ("video", "audio"),
        "pullable": kind == "video",
    }


def _vs_records(src: dict, day: str, q: str, limit: int, kind: str) -> list[dict]:
    base, token = src["base"], src.get("token", "")
    if q:
        # 远端 q 自带全库递归搜索（按路径模糊匹配）
        url = (f"{base}/api/list?q={urllib.parse.quote(q)}"
               f"&kind={urllib.parse.quote(kind)}&size={max(1, min(limit, 500))}")
        data = fetch_json(url, token=token)
        recs = [_vs_record(it) for it in (data.get("items") or [])]
    else:
        recs = [_vs_record(it) for it in _vs_list_all(src, kind=kind)]
        recs.sort(key=lambda r: r["created_at"], reverse=True)
    if day:
        recs = [r for r in recs if r["day"] == day]
    return recs[:max(1, limit)]


def _vs_pull(src: dict, rid: str, db_path: str) -> dict:
    """video-share 单条拉取：meta 拿元数据 → 按 rel 哈希去重 → 下载入库。"""
    base, token = src["base"], src.get("token", "")
    rid = urllib.parse.quote(str(rid))
    meta = fetch_json(f"{base}/api/meta?id={rid}", token=token)
    rel = str(meta.get("rel") or "")
    name = str(meta.get("name") or rel or "remote_file")
    size = int(meta.get("size") or 0)
    if str(meta.get("kind") or "") != "video":
        raise ValueError("只支持拉取视频类型素材")
    key = "vs_" + hashlib.md5(f"{base}/{rel}".encode("utf-8")).hexdigest()[:12]
    if key in migrate.existing_ids(db_path, "downloads"):
        return {"ok": True, "action": "skipped", "type": "downloads",
                "key": key, "reason": "目标库已存在"}
    # 下载到 downloads/（同名加序号），文件名优先取响应头 Content-Disposition
    req = urllib.request.Request(f"{base}/api/download?id={rid}", headers=_headers(token))
    with urllib.request.urlopen(req, timeout=600.0) as resp:
        fname = _filename_from_disposition(resp.headers.get("Content-Disposition")) or name
        dest = _unique_path(os.path.join(DOWNLOADS_DIR, fname))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        got = 0
        with open(dest, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
    rid_new = store.record(
        db_path, title=name, author="", aweme_id=key, word="远程文件库",
        url=f"{base}/api/stream?id={rid}", file=os.path.relpath(dest, os.path.dirname(DOWNLOADS_DIR)),
        size=got or size, status="downloaded")
    return {"ok": True, "action": "imported", "type": "downloads",
            "key": key, "id": rid_new, "file": os.path.relpath(dest, os.path.dirname(DOWNLOADS_DIR))}


def _filename_from_disposition(value: str | None) -> str:
    """解析 Content-Disposition 的 filename*=UTF-8''<名字>（RFC 5987）。"""
    if not value:
        return ""
    for part in value.split(";"):
        part = part.strip()
        if part.lower().startswith("filename*="):
            raw = part.split("=", 1)[1].strip().strip('"')
            if "''" in raw:
                raw = raw.split("''", 1)[1]
            return urllib.parse.unquote(raw)
    return ""


def _unique_path(path: str) -> str:
    """同名文件加序号，避免覆盖 downloads/ 里已有文件。"""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{stem}_{n}{ext}"):
        n += 1
    return f"{stem}_{n}{ext}"


# ------------------------------------------------------------ 单条拉取（按类型分派）
def pull_one(src: dict, rid, asset_type: str = "downloads",
             db_path: str = DEFAULT_DB_PATH) -> dict:
    """把远端单个素材导入本机库。clawdata 走迁移 zip 通道；videoshare 直接下载入库。"""
    if src.get("type") == "videoshare":
        return _vs_pull(src, str(rid), db_path)
    base = src["base"]
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        with urllib.request.urlopen(
            f"{base}/api/migrate/export?type={urllib.parse.quote(str(asset_type))}&id={int(rid)}",
            timeout=600.0,
        ) as resp, open(tmp.name, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        return migrate.import_bundle(db_path, tmp.name)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
