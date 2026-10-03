"""按资产逐条迁移：把一个资产（一条库记录 + 它的文件）打成一个 zip 包。

设计：**一个文件就是一个资产**——导出侧每个资产一个 `download_<id>.zip` /
`digest_<id>.zip`（包内 meta.json 存数据库原始行 + payload 存媒体本体，视频用
ZIP_STORED 不二次压缩）；导入侧收一个包落一条库，按视频 ID / 文件名去重，
重复导入自动跳过，因此传输可以中断、可以重跑（断点续传）。

支持两类资产：
- downloads：下载的视频（downloads 表整行 + downloads/ 下的视频文件）
- digests   : 视频文库文档（digests 表整行 + data/digests/ 下的 Markdown）

跨平台注意：源库里的相对路径是 Windows 分隔符（downloads\\xxx.mp4），导入时
统一归一化为目标机原生分隔符再入库。

纯函数模块，供 web 端点与 CLI（clawdata.migrate）共用；不依赖 HTTP。
"""

from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from datetime import datetime
from typing import Any

from clawdata.core.paths import PROJECT_ROOT
from clawdata.storage import store

TYPES = ("downloads", "digests")
META_NAME = "meta.json"
PAYLOAD_PREFIX = "payload"


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _norm_type(type_: str) -> str:
    t = (type_ or "").strip().lower()
    if t in ("download", "video", "videos"):
        t = "downloads"
    if t in ("digest", "doc"):
        t = "digests"
    if t not in TYPES:
        raise ValueError(f"未知资产类型 {type_!r}，可选：{'/'.join(TYPES)}")
    return t


def _rel_path(file_value: str, root: str) -> str:
    """把库里的 file/doc_path 归一化为相对项目根的 posix 路径（跨平台包内表示）。"""
    p = str(file_value or "").replace("\\", "/").strip()
    if os.path.isabs(p):
        try:
            p = os.path.relpath(p, root).replace("\\", "/")
        except ValueError:
            p = os.path.basename(p)
    return p.lstrip("./")


def _payload_name(file_value: str) -> str:
    ext = os.path.splitext(str(file_value or ""))[1] or ".bin"
    return PAYLOAD_PREFIX + ext


# --------------------------------------------------------------------- export

def list_items(db_path: str, type_: str, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
    """列出可迁移资产（按 id 升序、支持 after_id 翻页），供对端逐个拉取。"""
    t = _norm_type(type_)
    store.init(db_path)
    limit = max(1, min(int(limit or 100), 500))
    with _conn(db_path) as conn:
        if t == "downloads":
            rows = conn.execute(
                "SELECT id, created_at, day, title, author, aweme_id, file, size, status"
                "  FROM downloads WHERE id > ? ORDER BY id LIMIT ?",
                (int(after_id or 0), limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, created_at, day, platform, aweme_id, author, title,"
                "       doc_path, status FROM digests WHERE id > ? ORDER BY id LIMIT ?",
                (int(after_id or 0), limit),
            ).fetchall()
    items = []
    for r in rows:
        it = dict(r)
        if t == "downloads":
            rel = _rel_path(it.get("file", ""), PROJECT_ROOT)
            it["key"] = str(it.get("aweme_id") or "") or os.path.basename(rel)
            it["has_file"] = bool(rel) and os.path.isfile(os.path.join(PROJECT_ROOT, rel))
        else:
            it["key"] = str(it.get("aweme_id") or "")
            rel = _rel_path(it.get("doc_path", ""), PROJECT_ROOT)
            it["has_file"] = bool(rel) and os.path.isfile(os.path.join(PROJECT_ROOT, rel))
        items.append(it)
    return items


def existing_ids(db_path: str, type_: str) -> set[str]:
    """目标库里已有的资产 key（视频 ID 或文件名），导入前去重用。"""
    t = _norm_type(type_)
    store.init(db_path)
    keys: set[str] = set()
    with _conn(db_path) as conn:
        if t == "downloads":
            for r in conn.execute("SELECT aweme_id, file FROM downloads"):
                key = str(r["aweme_id"] or "")
                if not key:
                    key = os.path.basename(_rel_path(r["file"] or "", PROJECT_ROOT))
                if key:
                    keys.add(key)
        else:
            keys = {str(r[0]) for r in conn.execute("SELECT aweme_id FROM digests") if r[0]}
    return keys


def build_bundle(db_path: str, type_: str, rid: int, out_path: str,
                 root: str = PROJECT_ROOT) -> dict[str, Any]:
    """把一个资产打成 zip（写到 out_path）。视频文件缺失时打元数据-only 包。"""
    t = _norm_type(type_)
    store.init(db_path)
    with _conn(db_path) as conn:
        table = "downloads" if t == "downloads" else "digests"
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (int(rid),)).fetchone()
    if row is None:
        raise ValueError(f"{t} 里不存在 id={rid}")

    row = dict(row)
    if t == "downloads":
        rel = _rel_path(row.get("file", ""), root)
        src_abs = os.path.join(root, rel) if rel else ""
        key = str(row.get("aweme_id") or "") or os.path.basename(rel)
    else:
        rel = _rel_path(row.get("doc_path", ""), root)
        src_abs = os.path.join(root, rel) if rel else ""
        key = str(row.get("aweme_id") or "")

    has_payload = bool(src_abs) and os.path.isfile(src_abs)
    meta = {
        "meta_version": 1,
        "type": t,
        "key": key,
        "row": row,
        "payload": _payload_name(rel) if has_payload else "",
        "source_rel_path": rel,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
    }
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(META_NAME, json.dumps(meta, ensure_ascii=False, indent=2))
        if has_payload:
            zf.write(src_abs, arcname=meta["payload"])
    return {
        "ok": True, "type": t, "id": int(rid), "key": key,
        "zip": out_path, "size": os.path.getsize(out_path),
        "has_payload": has_payload,
        "filename": f"{'download' if t == 'downloads' else 'digest'}_{rid}.zip",
    }


# --------------------------------------------------------------------- import

def _safe_extract(zf: zipfile.ZipFile, member: str, dest_abs: str) -> None:
    """解压指定成员到 dest_abs（包内成员名固定为 payload.*，仍防御路径穿越）。"""
    os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
    with zf.open(member) as src, open(dest_abs, "wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


def import_bundle(db_path: str, zip_path: str, root: str = PROJECT_ROOT) -> dict[str, Any]:
    """导入一个资产包：按 key 去重，已存在跳过；写文件 + 落库（保留原始日期/标签）。"""
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if META_NAME not in names:
            raise ValueError("包里缺少 meta.json，不是合法的资产包")
        meta = json.loads(zf.read(META_NAME).decode("utf-8"))
        t = _norm_type(meta.get("type", ""))
        row = meta.get("row") or {}
        key = str(meta.get("key") or "")
        if not key:
            raise ValueError("meta.json 缺少 key")

        known = existing_ids(db_path, t)
        if key in known:
            return {"ok": True, "action": "skipped", "type": t, "key": key,
                    "reason": "目标库已存在"}

        store.init(db_path)
        if t == "downloads":
            rel = _rel_path(row.get("file", ""), root)
            base = os.path.basename(rel) or f"{key}.mp4"
            new_rel = os.path.join("downloads", base)  # 目标机原生分隔符
            if meta.get("payload") in names:
                _safe_extract(zf, meta["payload"], os.path.join(root, new_rel))
                size = os.path.getsize(os.path.join(root, new_rel))
            else:
                new_rel, size = rel, int(row.get("size") or 0)
            cols = ("created_at", "day", "title", "tag_category", "tags", "tag_summary",
                    "tag_status", "tagged_at", "tag_error", "author", "aweme_id", "word",
                    "account", "url", "file", "size", "status")
            defaults = {"created_at": datetime.now().isoformat(timespec="seconds"),
                        "day": datetime.now().strftime("%Y-%m-%d"),
                        "tag_status": "untagged", "status": "migrated",
                        "tags": "[]", "size": 0}
            overrides = {"file": new_rel, "size": size,
                         "status": row.get("status") or "migrated"}
            values = []
            for c in cols:
                v = overrides.get(c)
                if v is None:
                    v = row.get(c)
                    if v is None or v == "":
                        v = defaults.get(c, "")
                values.append(v)
            with _conn(db_path) as conn:
                conn.execute(
                    f"INSERT INTO downloads ({', '.join(cols)})"
                    " VALUES ({})".format(", ".join("?" * len(cols))),
                    values,
                )
                conn.commit()
                new_id = conn.execute(
                    "SELECT id FROM downloads WHERE aweme_id = ? OR file = ? ORDER BY id DESC LIMIT 1",
                    (key, new_rel)).fetchone()
            return {"ok": True, "action": "imported", "type": t, "key": key,
                    "id": int(new_id[0]) if new_id else 0, "file": new_rel}
        # digests
        doc_rel = ""
        if meta.get("payload") in names:
            digest_dir = os.path.join(root, "data", "digests")
            os.makedirs(digest_dir, exist_ok=True)
            doc_name = f"{row.get('platform', 'video')}_{key}.md"
            doc_rel = os.path.relpath(os.path.join(digest_dir, doc_name), root)
            _safe_extract(zf, meta["payload"], os.path.join(digest_dir, doc_name))
        now = row.get("created_at") or datetime.now().isoformat(timespec="seconds")
        day = row.get("day") or str(now)[:10]
        cols = ("created_at", "day", "platform", "aweme_id", "subscription_id", "author",
                "title", "homepage", "url", "pubdate", "desc_text", "subtitle_text",
                "links_json", "summary", "key_points_json", "doc_path", "status", "error")
        with _conn(db_path) as conn:
            conn.execute(
                f"INSERT OR IGNORE INTO digests ({', '.join(cols)})"
                " VALUES ({})".format(", ".join("?" * len(cols))),
                [now, day, row.get("platform", ""), key,
                 int(row.get("subscription_id") or 0), row.get("author", ""),
                 row.get("title", ""), row.get("homepage", ""), row.get("url", ""),
                 row.get("pubdate", ""), row.get("desc_text", ""),
                 row.get("subtitle_text", ""), row.get("links_json", "[]"),
                 row.get("summary", ""), row.get("key_points_json", "[]"),
                 doc_rel, row.get("status", "done"), row.get("error", "")],
            )
            conn.commit()
            new_row = conn.execute("SELECT id FROM digests WHERE aweme_id = ?", (key,)).fetchone()
        return {"ok": True, "action": "imported", "type": t, "key": key,
                "id": int(new_row[0]) if new_row else 0, "doc_path": doc_rel}
