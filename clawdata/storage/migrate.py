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
import shutil
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


def _insert_download_row(db_path: str, row: dict, new_rel: str, size: int) -> int:
    """插入一条下载记录（保留原始日期/标签），返回新 id。zip 导入与文件夹接管共用。"""
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
            (row.get("aweme_id") or "", new_rel)).fetchone()
    return int(new_id[0]) if new_id else 0


def _insert_digest_row(db_path: str, row: dict, key: str, doc_rel: str) -> int:
    """按 aweme_id 插入一条文库记录（已存在则忽略），返回记录 id。"""
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
    return int(new_row[0]) if new_row else 0


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
            new_id = _insert_download_row(db_path, row, new_rel, size)
            return {"ok": True, "action": "imported", "type": t, "key": key,
                    "id": new_id, "file": new_rel}
        # digests
        doc_rel = ""
        if meta.get("payload") in names:
            digest_dir = os.path.join(root, "data", "digests")
            os.makedirs(digest_dir, exist_ok=True)
            doc_name = f"{row.get('platform', 'video')}_{key}.md"
            doc_rel = os.path.relpath(os.path.join(digest_dir, doc_name), root)
            _safe_extract(zf, meta["payload"], os.path.join(digest_dir, doc_name))
        new_id = _insert_digest_row(db_path, row, key, doc_rel)
        return {"ok": True, "action": "imported", "type": t, "key": key,
                "id": new_id, "doc_path": doc_rel}


# ------------------------------------------------------------- folder adopt

def _find_in_dir(base: str, names: tuple[str, ...]) -> str:
    """在 base 下按候选相对路径找第一个存在的文件/目录。"""
    for n in names:
        p = os.path.join(base, *n.split("/"))
        if os.path.exists(p):
            return p
    return ""


def adopt_folder(db_path: str, src_dir: str, root: str = PROJECT_ROOT,
                 limit: int = 0, dry_run: bool = False,
                 on_progress=None, should_stop=None) -> dict[str, Any]:
    """接管一个「整份拷贝过来的文件夹」：合并其 downloads/digests 全部资产到本机库。

    src_dir 支持两种摆放：
    - 源项目根目录的拷贝（含 data/clawdata.db 与 downloads/）
    - 只拷了 data/ 与 downloads/ 两个目录的文件夹（含 clawdata.db 与 downloads/）

    拷贝方式随意（scp/rsync/U盘/共享目录）。按视频 ID 去重合并（不清空本机已有数据），
    并把库里 Windows 风格的相对路径（downloads\\x.mp4）归一化为本机分隔符。
    """
    say = on_progress or (lambda _m: None)
    src_dir = os.path.abspath(src_dir)
    if not os.path.isdir(src_dir):
        raise ValueError(f"目录不存在：{src_dir}")
    src_db = _find_in_dir(src_dir, ("data/clawdata.db", "clawdata.db"))
    if not src_db:
        raise ValueError(f"{src_dir} 下找不到 clawdata.db（应包含 data/clawdata.db 或直接是 clawdata.db）")
    video_root = _find_in_dir(src_dir, ("downloads", "data/downloads")) or ""
    doc_root = _find_in_dir(src_dir, ("data/digests", "digests")) or ""

    stats = {"downloads": {"imported": 0, "skipped": 0, "failed": 0, "missing_file": 0},
             "digests": {"imported": 0, "skipped": 0, "failed": 0, "missing_file": 0}}
    result = {"src": src_dir, "types": stats, "error": ""}
    say(f"接管文件夹 {src_dir}（源库 {src_db}）")

    src = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        for t in TYPES:
            known = existing_ids(db_path, t)
            done = 0
            rows = src.execute(
                f"SELECT * FROM {'downloads' if t == 'downloads' else 'digests'} ORDER BY id")
            for row in rows:
                if should_stop and should_stop():
                    say("已手动停止")
                    return result
                if limit and done >= limit:
                    break
                row = dict(row)
                if t == "downloads":
                    rel = _rel_path(row.get("file", ""), src_dir)
                    base = os.path.basename(rel) or f"{row.get('aweme_id')}.mp4"
                    key = str(row.get("aweme_id") or "") or base
                    if not key or key in known:
                        stats[t]["skipped"] += 1
                        continue
                    done += 1
                    src_abs = os.path.join(video_root, base) if video_root else ""
                    local_rel = os.path.join("downloads", base)
                    title = (row.get("title") or base)[:36]
                    if dry_run:
                        say(f"  [试跑] {title}")
                        continue
                    try:
                        size = int(row.get("size") or 0)
                        local_abs = os.path.join(root, local_rel)
                        if src_abs and os.path.isfile(src_abs):
                            os.makedirs(os.path.dirname(local_abs), exist_ok=True)
                            if not (os.path.isfile(local_abs) and os.path.getsize(local_abs) == os.path.getsize(src_abs)):
                                shutil.copyfile(src_abs, local_abs)
                            size = os.path.getsize(local_abs)
                        else:
                            stats[t]["missing_file"] += 1
                        new_id = _insert_download_row(db_path, row, local_rel, size)
                        known.add(key)
                        stats[t]["imported"] += 1
                        say(f"  [导入] {title} -> 本地 #{new_id}")
                    except Exception as exc:  # noqa: BLE001
                        stats[t]["failed"] += 1
                        say(f"  [失败] {title}: {str(exc)[:200]}")
                else:
                    key = str(row.get("aweme_id") or "")
                    if not key or key in known:
                        stats[t]["skipped"] += 1
                        continue
                    done += 1
                    title = (row.get("title") or key)[:36]
                    if dry_run:
                        say(f"  [试跑] 文库 {title}")
                        continue
                    try:
                        doc_name = os.path.basename(_rel_path(row.get("doc_path", ""), src_dir)) \
                            or f"{row.get('platform', 'video')}_{key}.md"
                        src_doc = os.path.join(doc_root, doc_name) if doc_root else ""
                        digest_dir = os.path.join(root, "data", "digests")
                        os.makedirs(digest_dir, exist_ok=True)
                        if src_doc and os.path.isfile(src_doc):
                            shutil.copyfile(src_doc, os.path.join(digest_dir, doc_name))
                        else:
                            stats[t]["missing_file"] += 1
                        doc_rel = os.path.relpath(os.path.join(digest_dir, doc_name), root)
                        new_id = _insert_digest_row(db_path, row, key, doc_rel)
                        known.add(key)
                        stats[t]["imported"] += 1
                        say(f"  [导入] 文库 {title} -> 本地 #{new_id}")
                    except Exception as exc:  # noqa: BLE001
                        stats[t]["failed"] += 1
                        say(f"  [失败] 文库 {title}: {str(exc)[:200]}")
            s = stats[t]
            say(f"=== {t} 完成：导入 {s['imported']}，跳过 {s['skipped']}，"
                f"缺文件 {s['missing_file']}，失败 {s['failed']} ===")
    finally:
        src.close()
    return result
