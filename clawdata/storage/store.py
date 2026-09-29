"""
抓取结果存储（SQLite，标准库）。

每一条"抓取/下载的视频"都会写入数据库，用于：
- 按天查看当天抓取结果
- 查看历史汇总（每天抓了多少、多大）
- 网页面板读数据、播放视频
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any

import numpy as np

from clawdata.core.paths import (
    DEFAULT_ASSETS_CONFIG_PATH,
    DEFAULT_HOTLIST_DIR,
    PROJECT_ROOT,
)


HOTLIST_DIR = DEFAULT_HOTLIST_DIR


def _conn(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        # WAL：面板多线程 + 局域网多端访问时减少 database is locked
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn


def init(db_path: str = "data/clawdata.db") -> None:
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                day        TEXT NOT NULL,
                title      TEXT,
                author     TEXT,
                aweme_id   TEXT,
                word       TEXT,
                url        TEXT,
                file       TEXT,
                size       INTEGER,
                status     TEXT
            )
            """
        )
        # 打标签功能新增字段（对已存在的库做增量迁移）
        extra = [
            ("tag_category", "TEXT"),
            ("tags", "TEXT"),           # JSON 数组
            ("tag_summary", "TEXT"),
            ("tag_status", "TEXT DEFAULT 'untagged'"),
            ("tagged_at", "TEXT"),
            ("tag_error", "TEXT"),
            ("account", "TEXT"),        # 视频归属的指定账号（昵称或 sec_user_id）
        ]
        existing = {r[1] for r in conn.execute("PRAGMA table_info(downloads)").fetchall()}
        for col, decl in extra:
            if col not in existing:
                conn.execute(f"ALTER TABLE downloads ADD COLUMN {col} {decl}")
        # 「连续动作筛选」工具：姿态分段，每个视频文件一条分析结果
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pose_segments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                file         TEXT UNIQUE,
                segments     TEXT,           -- JSON：全部分段
                top          TEXT,           -- JSON：最长 top 段（含片段路径）
                longest      REAL,           -- 最长连续段时长（秒）
                total_dur    REAL,           -- 视频总时长（秒）
                fps          REAL,
                num_frames   INTEGER,
                frames_analyzed INTEGER,
                avg_conf     REAL,           -- 检测到的人平均置信度
                status       TEXT DEFAULT 'pending',  -- pending / analyzed / error
                error        TEXT,
                analyzed_at  TEXT
            )
            """
        )
        # 「资产」工具：导出的连续动作片段（H.264 mp4）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS assets (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                file         TEXT UNIQUE,        -- 片段相对路径（相对项目根）
                basename     TEXT,                -- 片段文件名
                source_file  TEXT,                -- 源视频在 downloads.file 中的相对路径
                rank         INTEGER,
                start        REAL,
                end          REAL,
                duration     REAL,
                size         INTEGER,
                kind         TEXT DEFAULT 'tool', -- download / tool
                tool_id      TEXT DEFAULT '',
                tool_name    TEXT DEFAULT '',
                collection_id INTEGER DEFAULT 0,
                run_id       INTEGER DEFAULT 0,
                created_at   TEXT
            )
            """
        )
        # 帧级动作库：保留每帧姿态描述子，用于起始姿态匹配
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pose_frames (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                file          TEXT NOT NULL,
                frame_idx     INTEGER NOT NULL,
                time_sec      REAL NOT NULL,
                has_person    INTEGER NOT NULL DEFAULT 0,
                descriptor    BLOB,
                segment_idx   INTEGER NOT NULL DEFAULT -1,
                segment_start INTEGER NOT NULL DEFAULT 0,
                active        INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT,
                UNIQUE(file, frame_idx)
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pose_frames_file ON pose_frames(file, frame_idx)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pose_frames_start ON pose_frames(has_person, segment_start)")
        existing_assets = {r[1] for r in conn.execute("PRAGMA table_info(assets)").fetchall()}
        if "category" not in existing_assets:
            conn.execute("ALTER TABLE assets ADD COLUMN category TEXT DEFAULT ''")
        if "kind" not in existing_assets:
            conn.execute("ALTER TABLE assets ADD COLUMN kind TEXT DEFAULT 'tool'")
        if "tool_id" not in existing_assets:
            conn.execute("ALTER TABLE assets ADD COLUMN tool_id TEXT DEFAULT ''")
        if "tool_name" not in existing_assets:
            conn.execute("ALTER TABLE assets ADD COLUMN tool_name TEXT DEFAULT ''")
        if "collection_id" not in existing_assets:
            conn.execute("ALTER TABLE assets ADD COLUMN collection_id INTEGER DEFAULT 0")
        if "run_id" not in existing_assets:
            conn.execute("ALTER TABLE assets ADD COLUMN run_id INTEGER DEFAULT 0")
        asset_tag_columns = {
            r[1] for r in conn.execute("PRAGMA table_info(assets)").fetchall()
        }
        for col, decl in [
            ("tag_category", "TEXT DEFAULT ''"),
            ("tags", "TEXT DEFAULT '[]'"),
            ("tag_summary", "TEXT DEFAULT ''"),
            ("tag_status", "TEXT DEFAULT 'untagged'"),
            ("tag_error", "TEXT DEFAULT ''"),
            ("tagged_at", "TEXT"),
        ]:
            if col not in asset_tag_columns:
                conn.execute(f"ALTER TABLE assets ADD COLUMN {col} {decl}")
        conn.execute(
            "UPDATE assets SET kind = 'tool' WHERE kind IS NULL OR kind = ''"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS asset_collections (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT UNIQUE NOT NULL,
                tool_id    TEXT DEFAULT '',
                tool_name  TEXT DEFAULT '',
                created_at TEXT
            )
            """
        )
        # 用户自定义收藏夹：多对多关联（资产可同时在工具合集和多个收藏夹里）
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS asset_favorites (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS asset_favorite_items (
                favorite_id INTEGER NOT NULL,
                asset_id    INTEGER NOT NULL,
                added_at    TEXT NOT NULL,
                PRIMARY KEY (favorite_id, asset_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tool_runs (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_id              TEXT NOT NULL,
                    tool_name            TEXT NOT NULL,
                    status               TEXT NOT NULL DEFAULT 'queued',
                    total_count          INTEGER NOT NULL DEFAULT 0,
                    done_count           INTEGER NOT NULL DEFAULT 0,
                    error_count          INTEGER NOT NULL DEFAULT 0,
                    skipped_count        INTEGER NOT NULL DEFAULT 0,
                    output_collection_id INTEGER DEFAULT 0,
                    params               TEXT DEFAULT '{}',
                    error                TEXT DEFAULT '',
                    created_at           TEXT NOT NULL,
                    started_at           TEXT,
                    finished_at          TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tool_runs_tool ON tool_runs(tool_id, created_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    name              TEXT NOT NULL,
                    homepage          TEXT NOT NULL UNIQUE,
                    sec_uid           TEXT DEFAULT '',
                    category          TEXT DEFAULT '订阅博主',
                    enabled           INTEGER DEFAULT 1,
                    interval_hours    INTEGER DEFAULT 6,
                    last_checked_at   TEXT,
                    next_check_at     TEXT,
                    last_new_count    INTEGER DEFAULT 0,
                    last_error        TEXT DEFAULT '',
                    created_at        TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_subscriptions_next ON subscriptions(next_check_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS digests (
                    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at         TEXT NOT NULL,
                    day                TEXT NOT NULL,
                    platform           TEXT NOT NULL,
                    aweme_id           TEXT NOT NULL UNIQUE,
                    subscription_id    INTEGER DEFAULT 0,
                    author             TEXT DEFAULT '',
                    title              TEXT DEFAULT '',
                    homepage           TEXT DEFAULT '',
                    url                TEXT DEFAULT '',
                    pubdate            TEXT DEFAULT '',
                    desc_text          TEXT DEFAULT '',
                    subtitle_text      TEXT DEFAULT '',
                    links_json         TEXT DEFAULT '[]',
                    summary            TEXT DEFAULT '',
                    key_points_json    TEXT DEFAULT '[]',
                    doc_path           TEXT DEFAULT '',
                    status             TEXT DEFAULT 'done',
                    error              TEXT DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_digests_created ON digests(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_digests_sub ON digests(subscription_id)")
        # 订阅表增量迁移：account 列缓存从视频数据解析出的博主昵称（与 downloads.account 精确关联）
        sub_cols = {d[1] for d in conn.execute("PRAGMA table_info(subscriptions)").fetchall()}
        if "account" not in sub_cols:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN account TEXT DEFAULT ''")
        # 资产表增量迁移：platform 列记录来源平台（douyin / bilibili），同步时回填
        asset_cols = {d[1] for d in conn.execute("PRAGMA table_info(assets)").fetchall()}
        if "platform" not in asset_cols:
            conn.execute("ALTER TABLE assets ADD COLUMN platform TEXT DEFAULT ''")
        conn.execute(
            """
            INSERT OR IGNORE INTO asset_collections(name, tool_id, tool_name, created_at)
            VALUES ('连续动作筛选', 'action', '连续动作筛选', ?)
            """,
            (datetime.now().isoformat(timespec="seconds"),),
        )
        conn.execute(
            """
            UPDATE assets
               SET collection_id = (
                   SELECT id FROM asset_collections
                    WHERE tool_id = 'action'
                    ORDER BY id LIMIT 1
               )
             WHERE kind = 'tool'
               AND tool_id = 'action'
               AND (collection_id IS NULL OR collection_id = 0)
            """
        )
        # 旧数据里的 downloads/segments 全部来自「连续动作筛选」工具。
        conn.execute(
            """
            UPDATE assets
               SET tool_id = 'action', tool_name = '连续动作筛选'
             WHERE kind = 'tool'
               AND (tool_id IS NULL OR tool_id = '')
               AND file LIKE 'downloads' || ? || 'segments' || ? || '%'
            """,
            (os.sep, os.sep),
        )
        conn.commit()
    finally:
        conn.close()


def set_tag(
    db_path: str,
    rid: int,
    *,
    category: str = "",
    tags: list[str] | None = None,
    summary: str = "",
    status: str = "tagged",
    error: str = "",
    now: datetime | None = None,
) -> None:
    """写入一条记录的打标签结果。"""
    when = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            UPDATE downloads
               SET tag_category = ?, tags = ?, tag_summary = ?,
                   tag_status = ?, tag_error = ?, tagged_at = ?
             WHERE id = ?
            """,
            (
                category,
                json.dumps(tags or [], ensure_ascii=False),
                summary,
                status,
                error,
                when if status == "tagged" else None,
                rid,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def set_tag_status(db_path: str, rid: int, status: str, error: str = "") -> None:
    conn = _conn(db_path)
    try:
        conn.execute(
            "UPDATE downloads SET tag_status = ?, tag_error = ? WHERE id = ?",
            (status, error, rid),
        )
        conn.commit()
    finally:
        conn.close()


def set_tags_for_file(
    db_path: str,
    file: str,
    *,
    category: str,
    tags: list[str],
    summary: str,
    status: str = "tagged",
    now: datetime | None = None,
) -> None:
    """把原视频记录与资产画廊中的对应原片统一更新为同一份标签。"""
    when = (now or datetime.now()).isoformat(timespec="seconds")
    file_norm = os.path.normpath(file)
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            UPDATE downloads
               SET tag_category = ?, tags = ?, tag_summary = ?,
                   tag_status = ?, tag_error = '',
                   tagged_at = CASE WHEN ? = 'tagged' THEN ? ELSE NULL END
             WHERE file IN (?, ?)
            """,
            (category, json.dumps(tags or [], ensure_ascii=False), summary, status,
             status, when, file, file_norm),
        )
        conn.execute(
            """
            UPDATE assets
               SET tag_category = ?, tags = ?, tag_summary = ?,
                   tag_status = ?, tag_error = '',
                   tagged_at = CASE WHEN ? = 'tagged' THEN ? ELSE NULL END
             WHERE file IN (?, ?)
            """,
            (
                category, json.dumps(tags or [], ensure_ascii=False), summary,
                status, status, when, file, file_norm,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_for_tagging(
    db_path: str,
    status: str = "all",
    q: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """打标签页面的记录列表（可按打标状态和标签语义过滤）。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        sql = "SELECT * FROM downloads"
        args: list[str] = []
        if status and status != "all":
            sql += " WHERE tag_status = ?"
            args.append(status)
        if q:
            sql += " AND (" if status and status != "all" else " WHERE ("
            sql += """
                title LIKE ? OR author LIKE ? OR file LIKE ? OR
                tag_category LIKE ? OR tags LIKE ? OR tag_summary LIKE ?
            )
            """
            like = f"%{q}%"
            args.extend([like, like, like, like, like, like])
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def tagging_stats(db_path: str) -> dict[str, Any]:
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT tag_status, COUNT(*) FROM downloads GROUP BY tag_status"
        ).fetchall()
        stats = {"total": 0, "untagged": 0, "tagging": 0, "tagged": 0, "error": 0}
        for status, n in rows:
            stats["total"] += n
            if status in stats:
                stats[status] += n
        return stats
    finally:
        conn.close()


def set_asset_tag_status(db_path: str, aid: int, status: str, error: str = "") -> bool:
    """更新资产视频的打标状态。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE assets SET tag_status = ?, tag_error = ? WHERE id = ?",
            (status, error, aid),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_asset_tag(
    db_path: str,
    aid: int,
    *,
    category: str,
    tags: list[str],
    summary: str,
    status: str = "tagged",
    error: str = "",
    now: datetime | None = None,
) -> bool:
    """写入资产视频的内容理解结果。"""
    init(db_path)
    when = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        cur = conn.execute(
            """
            UPDATE assets
               SET tag_category = ?, tags = ?, tag_summary = ?,
                   tag_status = ?, tag_error = ?,
                   tagged_at = CASE WHEN ? = 'tagged' THEN ? ELSE tagged_at END
             WHERE id = ?
            """,
            (
                category, json.dumps(tags or [], ensure_ascii=False), summary,
                status, error, status, when, aid,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _video_file_predicate(alias: str = "a") -> str:
    exts = (".mp4", ".mov", ".mkv", ".webm", ".avi")
    return "(" + " OR ".join(
        f"LOWER({alias}.file) LIKE '%{ext}'" for ext in exts
    ) + ")"


def list_download_tag_sources(
    db_path: str,
    *,
    status: str = "all",
    q: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """工具页批量选择用的去重原视频列表。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        sql = """
            SELECT d.id, d.file, d.title, d.author, d.size,
                   d.tag_category, d.tags, d.tag_summary,
                   d.tag_status, d.tag_error, d.tagged_at
              FROM downloads d
              JOIN (
                   SELECT file, MIN(id) AS mid FROM downloads
                    WHERE file IS NOT NULL AND file != ''
                    GROUP BY file
              ) m ON d.id = m.mid
        """
        args: list[Any] = []
        if status and status != "all":
            sql += " WHERE d.tag_status = ?"
            args.append(status)
        sql += " ORDER BY d.created_at DESC, d.id DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        item = {
            "id": r[0], "file": r[1] or "", "title": r[2] or "",
            "author": r[3] or "", "size": r[4] or 0,
            "category": r[5] or "", "tags": _safe_json(r[6]),
            "summary": r[7] or "", "status": r[8] or "untagged",
            "error": r[9] or "", "tagged_at": r[10] or "",
            "target_type": "download", "source_type": "原视频",
        }
        if q and q.lower() not in " ".join(
            [item["title"], item["author"], item["file"]]
        ).lower():
            continue
        if os.path.isfile(os.path.join(PROJECT_ROOT, item["file"])):
            item["exists"] = True
            items.append(item)
    return items


def list_asset_tag_sources(
    db_path: str,
    *,
    status: str = "all",
    q: str = "",
    kind: str = "",
    limit: int = 500,
) -> list[dict[str, Any]]:
    """工具页批量选择用的资产视频列表。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        sql = f"""
            SELECT a.id, a.file, a.basename, a.source_file, a.kind, a.duration,
                   a.size, a.tag_category, a.tags, a.tag_summary,
                   a.tag_status, a.tag_error, a.tagged_at,
                   c.name,
                   '' AS title,
                   '' AS author
             FROM assets a
              LEFT JOIN asset_collections c ON c.id = a.collection_id
             WHERE a.kind = 'tool' AND {_video_file_predicate('a')}
        """
        args: list[Any] = []
        if kind:
            sql += " AND CASE WHEN a.kind = 'download' THEN 'download' ELSE 'collection:' || COALESCE(a.collection_id, 0) END = ?"
            args.append(kind)
        if status and status != "all":
            sql += " AND COALESCE(NULLIF(a.tag_status, ''), 'untagged') = ?"
            args.append(status)
        sql += " ORDER BY a.created_at DESC, a.id DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        source_file = r[3] or ""
        title_row = ""
        author_row = ""
        if source_file:
            conn2 = _conn(db_path)
            try:
                meta = conn2.execute(
                    "SELECT title, author FROM downloads WHERE file = ? ORDER BY id LIMIT 1",
                    (source_file,),
                ).fetchone()
                if meta:
                    title_row, author_row = meta[0] or "", meta[1] or ""
            finally:
                conn2.close()
        item = {
            "id": r[0], "file": r[1] or "", "basename": r[2] or "",
            "source_file": source_file, "kind": r[4] or "tool",
            "duration": _num(r[5]), "size": r[6] or 0,
            "category": r[7] or "", "tags": _safe_json(r[8]),
            "summary": r[9] or "", "status": r[10] or "untagged",
            "error": r[11] or "", "tagged_at": r[12] or "",
            "collection_name": r[13] or "",
            "title": r[14] or title_row or r[2],
            "author": r[15] or author_row,
            "target_type": "asset", "source_type": "资产视频",
        }
        if q and q.lower() not in " ".join(
            [item["title"], item["author"], item["basename"], item["collection_name"]]
        ).lower():
            continue
        if os.path.isfile(os.path.join(PROJECT_ROOT, item["file"])):
            item["exists"] = True
            items.append(item)
    return items


def tagging_tool_stats(db_path: str, source: str) -> dict[str, Any]:
    """工具页原视频/资产视频的打标状态统计。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        if source == "asset":
            rows = conn.execute(
                f"""
                SELECT COALESCE(NULLIF(tag_status, ''), 'untagged'), COUNT(*)
                  FROM assets a
                 WHERE a.kind = 'tool' AND {_video_file_predicate('a')}
                 GROUP BY 1
                """
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT COALESCE(NULLIF(d.tag_status, ''), 'untagged'), COUNT(*)
                  FROM downloads d
                  JOIN (
                       SELECT file, MIN(id) AS mid FROM downloads
                        WHERE file IS NOT NULL AND file != ''
                        GROUP BY file
                  ) m ON d.id = m.mid
                 GROUP BY 1
                """
            ).fetchall()
    finally:
        conn.close()
    stats = {"total": 0, "untagged": 0, "tagging": 0, "tagged": 0, "error": 0}
    for status, count in rows:
        stats["total"] += count
        if status in stats:
            stats[status] += count
    return stats


def distinct_video_files(db_path: str) -> list[str]:
    """去重后的视频文件路径（用于批量打标签）。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT file FROM downloads WHERE file IS NOT NULL AND file != ''"
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def untagged_video_files(db_path: str, force: bool = False) -> list[str]:
    """返回需要打标签的视频文件（去重后，且尚未有 tagged 记录）。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        if force:
            rows = conn.execute(
                "SELECT DISTINCT file FROM downloads "
                "WHERE file IS NOT NULL AND file != ''"
            ).fetchall()
            return [r[0] for r in rows]
        rows = conn.execute(
            """
            SELECT file FROM downloads
            WHERE file IS NOT NULL AND file != ''
            GROUP BY file
            HAVING SUM(CASE WHEN tag_status = 'tagged' THEN 1 ELSE 0 END) = 0
            """
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def get_record_by_id(db_path: str, rid: int) -> dict[str, Any] | None:
    return get(db_path, rid)


def record(
    db_path: str,
    *,
    title: str = "",
    category: str = "",
    author: str = "",
    aweme_id: str = "",
    word: str = "",
    account: str = "",
    url: str = "",
    file: str = "",
    size: int = 0,
    status: str = "downloaded",
    now: datetime | None = None,
) -> int:
    """写入一条结果，返回新记录 id。"""
    now = now or datetime.now()
    init(db_path)
    conn = _conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO downloads
                (created_at, day, title, tag_category, author, aweme_id, word, account, url, file, size, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now.isoformat(timespec="seconds"),
                now.strftime("%Y-%m-%d"),
                title,
                category,
                author,
                aweme_id,
                word,
                account,
                url,
                file,
                size,
                status,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_by_day(db_path: str, day: str) -> list[dict[str, Any]]:
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM downloads WHERE day = ? ORDER BY created_at DESC",
            (day,),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def today(db_path: str, now: datetime | None = None) -> list[dict[str, Any]]:
    return list_by_day(db_path, (now or datetime.now()).strftime("%Y-%m-%d"))


def get(db_path: str, rid: int) -> dict[str, Any] | None:
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM downloads WHERE id = ?", (rid,)).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def history(db_path: str) -> list[dict[str, Any]]:
    """按天汇总历史，最新在前。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT day,
                   COUNT(*)                                         AS count,
                   SUM(CASE WHEN status='downloaded' THEN 1 ELSE 0 END) AS ok,
                   SUM(size)                                        AS size
            FROM downloads
            GROUP BY day
            ORDER BY day DESC
            """
        ).fetchall()
        return [
            {
                "day": r[0],
                "count": r[1],
                "ok": r[2] or 0,
                "size": r[3] or 0,
            }
            for r in rows
        ]
    finally:
        conn.close()


def _row(r: tuple) -> dict[str, Any]:
    cols = [
        "id", "created_at", "day", "title", "author", "aweme_id", "word",
        "url", "file", "size", "status",
        "tag_category", "tags", "tag_summary", "tag_status", "tagged_at", "tag_error",
        "account",
    ]
    return dict(zip(cols, r))


def save_hotlist_snapshot(day: str, items: list[dict[str, Any]]) -> None:
    """把某天的热榜快照写成 JSON 文件，供面板展示。"""
    os.makedirs(HOTLIST_DIR, exist_ok=True)
    path = os.path.join(HOTLIST_DIR, f"{day}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    return path


def load_hotlist_snapshot(day: str) -> list[dict[str, Any]]:
    path = os.path.join(HOTLIST_DIR, f"{day}.json")
    if not os.path.isfile(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def add_subscription(
    db_path: str,
    *,
    name: str,
    homepage: str,
    sec_uid: str = "",
    category: str = "订阅博主",
    interval_hours: int = 6,
    now: datetime | None = None,
) -> int | None:
    """新增博主订阅；重复主页时返回已有 ID。"""
    init(db_path)
    now_dt = now or datetime.now()
    conn = _conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO subscriptions
                (name, homepage, sec_uid, category, enabled, interval_hours,
                 next_check_at, created_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                name.strip(), homepage.strip(), sec_uid.strip(), category.strip() or "订阅博主",
                max(1, int(interval_hours)), now_dt.isoformat(timespec="seconds"),
                now_dt.isoformat(timespec="seconds"),
            ),
        )
        conn.commit()
        return int(cur.lastrowid) if cur.rowcount else int(
            conn.execute("SELECT id FROM subscriptions WHERE homepage = ?", (homepage.strip(),)).fetchone()[0]
        )
    finally:
        conn.close()


def list_subscriptions(db_path: str) -> list[dict[str, Any]]:
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM subscriptions ORDER BY created_at DESC, id DESC"
        ).fetchall()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def get_subscription(db_path: str, sid: int) -> dict[str, Any] | None:
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sid,)).fetchone()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
        return dict(zip(cols, row)) if row else None
    finally:
        conn.close()


def update_subscription(
    db_path: str, sid: int, *, sec_uid: str | None = None,
    last_checked_at: str | None = None, next_check_at: str | None = None,
    last_new_count: int | None = None, error: str | None = None,
    account: str | None = None,
) -> None:
    init(db_path)
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            UPDATE subscriptions
               SET sec_uid = COALESCE(?, sec_uid),
                   last_checked_at = COALESCE(?, last_checked_at),
                   next_check_at = COALESCE(?, next_check_at),
                   last_new_count = COALESCE(?, last_new_count),
                   last_error = COALESCE(?, last_error),
                   account = COALESCE(?, account)
             WHERE id = ?
            """,
            (sec_uid, last_checked_at, next_check_at,
             last_new_count, error, account, sid),
        )
        conn.commit()
    finally:
        conn.close()


def delete_subscription(db_path: str, sid: int) -> bool:
    init(db_path)
    conn = _conn(db_path)
    try:
        cur = conn.execute("DELETE FROM subscriptions WHERE id = ?", (sid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def due_subscriptions(db_path: str, now: datetime | None = None) -> list[dict[str, Any]]:
    init(db_path)
    now_dt = now or datetime.now()
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM subscriptions
             WHERE enabled = 1
               AND (next_check_at IS NULL OR next_check_at <= ?)
             ORDER BY COALESCE(next_check_at, created_at)
            """,
            (now_dt.isoformat(timespec="seconds"),),
        ).fetchall()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(subscriptions)").fetchall()]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


def downloads_by_account(db_path: str, account: str, limit: int = 300) -> list[dict[str, Any]]:
    """查某个博主/UP主已下载的全部视频，用于订阅详情视图。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM downloads
             WHERE account = ? OR word = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            (account, f"account:{account}", limit),
        ).fetchall()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(downloads)").fetchall()]
        return [dict(zip(cols, row)) for row in rows]
    finally:
        conn.close()


# --------------------------------------------------------------- digests
def _digest_row(row: tuple, cols: list[str]) -> dict[str, Any]:
    item = dict(zip(cols, row))
    try:
        item["links"] = json.loads(item.get("links_json") or "[]")
    except (ValueError, TypeError):
        item["links"] = []
    try:
        item["key_points"] = json.loads(item.get("key_points_json") or "[]")
    except (ValueError, TypeError):
        item["key_points"] = []
    return item


def record_digest(
    db_path: str,
    *,
    platform: str,
    aweme_id: str,
    subscription_id: int = 0,
    author: str = "",
    title: str = "",
    homepage: str = "",
    url: str = "",
    pubdate: str = "",
    desc_text: str = "",
    subtitle_text: str = "",
    links: list[dict[str, Any]] | None = None,
    summary: str = "",
    key_points: list[str] | None = None,
    doc_path: str = "",
    status: str = "done",
    error: str = "",
    now: datetime | None = None,
) -> int:
    """按视频 ID upsert 一条文库记录，返回记录 ID。"""
    init(db_path)
    now_dt = now or datetime.now()
    conn = _conn(db_path)
    try:
        values = (
            now_dt.isoformat(timespec="seconds"), now_dt.strftime("%Y-%m-%d"),
            platform, str(aweme_id), int(subscription_id), author, title,
            homepage, url, pubdate, desc_text, subtitle_text,
            json.dumps(links or [], ensure_ascii=False),
            summary, json.dumps(key_points or [], ensure_ascii=False),
            doc_path, status, error,
        )
        cur = conn.execute(
            """
            INSERT INTO digests
                (created_at, day, platform, aweme_id, subscription_id, author, title,
                 homepage, url, pubdate, desc_text, subtitle_text, links_json,
                 summary, key_points_json, doc_path, status, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(aweme_id) DO UPDATE SET
                created_at=excluded.created_at, day=excluded.day,
                subscription_id=excluded.subscription_id, author=excluded.author,
                title=excluded.title, homepage=excluded.homepage, url=excluded.url,
                pubdate=excluded.pubdate, desc_text=excluded.desc_text,
                subtitle_text=excluded.subtitle_text, links_json=excluded.links_json,
                summary=excluded.summary, key_points_json=excluded.key_points_json,
                doc_path=excluded.doc_path, status=excluded.status, error=excluded.error
            """,
            values,
        )
        conn.commit()
        row = conn.execute("SELECT id FROM digests WHERE aweme_id = ?", (str(aweme_id),)).fetchone()
        return int(row[0]) if row else int(cur.lastrowid or 0)
    finally:
        conn.close()


def get_digest(db_path: str, dig_id: int) -> dict[str, Any] | None:
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM digests WHERE id = ?", (dig_id,)).fetchone()
        if not row:
            return None
        cols = [d[1] for d in conn.execute("PRAGMA table_info(digests)").fetchall()]
        return _digest_row(row, cols)
    finally:
        conn.close()


def get_digest_by_video(db_path: str, aweme_id: str) -> dict[str, Any] | None:
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM digests WHERE aweme_id = ?", (str(aweme_id),)).fetchone()
        if not row:
            return None
        cols = [d[1] for d in conn.execute("PRAGMA table_info(digests)").fetchall()]
        return _digest_row(row, cols)
    finally:
        conn.close()


def list_digests(
    db_path: str,
    *,
    platform: str = "",
    author: str = "",
    q: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """文库列表：可按平台/博主筛选，关键词匹配标题、摘要、简介。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        sql = "SELECT * FROM digests WHERE 1=1"
        args: list[Any] = []
        if platform:
            sql += " AND platform = ?"
            args.append(platform)
        if author:
            sql += " AND author = ?"
            args.append(author)
        if q:
            sql += " AND (title LIKE ? OR summary LIKE ? OR desc_text LIKE ? OR author LIKE ?)"
            like = f"%{q}%"
            args.extend([like, like, like, like])
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(int(limit))
        rows = conn.execute(sql, args).fetchall()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(digests)").fetchall()]
        return [_digest_row(row, cols) for row in rows]
    finally:
        conn.close()


def search_digests(db_path: str, keywords: list[str], limit: int = 6) -> list[dict[str, Any]]:
    """问询检索：把问题拆成关键词，按标题/摘要/简介/字幕加权匹配（词间 OR，命中越多分越高）。"""
    kws = [k for k in (str(x).strip() for x in keywords) if len(k) >= 2][:8]
    if not kws:
        return []
    init(db_path)
    conn = _conn(db_path)
    try:
        selects: list[str] = []
        wheres: list[str] = []
        args: list[Any] = []
        for kw in kws:
            like = f"%{kw}%"
            selects.append(
                "(CASE WHEN title LIKE ? THEN 3 ELSE 0 END"
                " + CASE WHEN summary LIKE ? THEN 2 ELSE 0 END"
                " + CASE WHEN desc_text LIKE ? THEN 1 ELSE 0 END"
                " + CASE WHEN subtitle_text LIKE ? THEN 1 ELSE 0 END)"
            )
            args.extend([like, like, like, like])
            wheres.append(
                "(title LIKE ? OR summary LIKE ? OR desc_text LIKE ? OR subtitle_text LIKE ?)"
            )
            args.extend([like, like, like, like])
        sql = (
            "SELECT id, platform, aweme_id, author, title, url, pubdate,"
            " summary, desc_text, subtitle_text,"
            f" ({' + '.join(selects)}) AS score"
            " FROM digests"
            f" WHERE {' OR '.join(wheres)}"
            " ORDER BY score DESC, id DESC LIMIT ?"
        )
        rows = conn.execute(sql, (*args, int(limit))).fetchall()
        names = ["id", "platform", "aweme_id", "author", "title", "url", "pubdate",
                 "summary", "desc_text", "subtitle_text", "score"]
        return [dict(zip(names, row)) for row in rows]
    finally:
        conn.close()


def existing_digest_ids(db_path: str) -> set[str]:
    """全部已整理的视频 ID（BV号/aweme_id），用于增量判重。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        return {str(r[0]) for r in conn.execute("SELECT aweme_id FROM digests").fetchall()}
    finally:
        conn.close()


def delete_digest(db_path: str, dig_id: int) -> str:
    """删除记录并返回其文档路径（供调用方决定是否删文件）。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT doc_path FROM digests WHERE id = ?", (dig_id,)).fetchone()
        conn.execute("DELETE FROM digests WHERE id = ?", (dig_id,))
        conn.commit()
        return str(row[0]) if row else ""
    finally:
        conn.close()


def digest_stats(db_path: str) -> dict[str, Any]:
    init(db_path)
    conn = _conn(db_path)
    try:
        total = conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
        bili = conn.execute("SELECT COUNT(*) FROM digests WHERE platform = 'bilibili'").fetchone()[0]
        dy = conn.execute("SELECT COUNT(*) FROM digests WHERE platform = 'douyin'").fetchone()[0]
        last = conn.execute("SELECT created_at FROM digests ORDER BY id DESC LIMIT 1").fetchone()
        authors = conn.execute(
            "SELECT author, COUNT(*) AS n FROM digests GROUP BY author ORDER BY n DESC LIMIT 50"
        ).fetchall()
        return {
            "total": int(total),
            "bilibili": int(bili),
            "douyin": int(dy),
            "last_created_at": str(last[0]) if last else "",
            "authors": [{"author": str(a or ""), "count": int(n)} for a, n in authors],
        }
    finally:
        conn.close()


def list_favorites(db_path: str) -> list[dict[str, Any]]:
    """用户收藏夹列表（带成员数）。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT f.id, f.name, f.created_at, COUNT(i.asset_id)
              FROM asset_favorites f
              LEFT JOIN asset_favorite_items i ON i.favorite_id = f.id
             GROUP BY f.id
             ORDER BY f.id DESC
            """
        ).fetchall()
        return [{"id": r[0], "name": r[1], "created_at": r[2], "count": r[3]} for r in rows]
    finally:
        conn.close()


def create_favorite(db_path: str, name: str) -> int | None:
    name = (name or "").strip()
    if not name:
        return None
    init(db_path)
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO asset_favorites (name, created_at) VALUES (?, ?)",
            (name, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        row = conn.execute("SELECT id FROM asset_favorites WHERE name = ?", (name,)).fetchone()
        return int(row[0]) if row else None
    finally:
        conn.close()


def rename_favorite(db_path: str, fid: int, name: str) -> bool:
    name = (name or "").strip()
    if not name:
        return False
    init(db_path)
    conn = _conn(db_path)
    try:
        cur = conn.execute("UPDATE asset_favorites SET name = ? WHERE id = ?", (name, fid))
        conn.commit()
        return cur.rowcount > 0
    except Exception:  # noqa: BLE001  重名冲突
        conn.rollback()
        return False
    finally:
        conn.close()


def delete_favorite(db_path: str, fid: int) -> bool:
    """删除收藏夹本身；成员资产不受影响。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        cur = conn.execute("DELETE FROM asset_favorites WHERE id = ?", (fid,))
        conn.execute("DELETE FROM asset_favorite_items WHERE favorite_id = ?", (fid,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def favorite_add(db_path: str, fid: int, asset_ids: list[int]) -> int:
    init(db_path)
    when = datetime.now().isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO asset_favorite_items (favorite_id, asset_id, added_at) VALUES (?, ?, ?)",
            [(fid, int(aid), when) for aid in asset_ids],
        )
        conn.commit()
        return conn.execute(
            "SELECT COUNT(*) FROM asset_favorite_items WHERE favorite_id = ?", (fid,)
        ).fetchone()[0]
    finally:
        conn.close()


def favorite_remove(db_path: str, fid: int, asset_ids: list[int]) -> int:
    init(db_path)
    conn = _conn(db_path)
    try:
        conn.executemany(
            "DELETE FROM asset_favorite_items WHERE favorite_id = ? AND asset_id = ?",
            [(fid, int(aid)) for aid in asset_ids],
        )
        conn.commit()
        return conn.execute(
            "SELECT COUNT(*) FROM asset_favorite_items WHERE favorite_id = ?", (fid,)
        ).fetchone()[0]
    finally:
        conn.close()


def assets_favorite_ids(db_path: str, asset_ids: list[int]) -> dict[int, list[int]]:
    """给定一批资产 id，返回 {asset_id: [favorite_id,...]}，用于渲染收藏星标。"""
    if not asset_ids:
        return {}
    init(db_path)
    conn = _conn(db_path)
    try:
        marks = ",".join("?" for _ in asset_ids)
        rows = conn.execute(
            f"SELECT asset_id, favorite_id FROM asset_favorite_items WHERE asset_id IN ({marks})",
            [int(a) for a in asset_ids],
        ).fetchall()
        out: dict[int, list[int]] = {}
        for aid, fid in rows:
            out.setdefault(int(aid), []).append(int(fid))
        return out
    finally:
        conn.close()


def existing_aweme_ids(db_path: str) -> set[str]:
    """已入库的 aweme_id 集合，用于博主订阅增量去重。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        return {
            str(r[0]) for r in conn.execute(
                "SELECT aweme_id FROM downloads WHERE aweme_id IS NOT NULL AND aweme_id != ''"
            ).fetchall()
        }
    finally:
        conn.close()


def prune_non_keyword_downloads(db_path: str) -> dict[str, Any]:
    """删除历史下载数据；关键词搜索下载（word 为实际关键词）除外。"""
    init(db_path)
    conn = _conn(db_path)
    removed_files: list[str] = []
    removed = 0
    try:
        rows = conn.execute(
            """
            SELECT id, file FROM downloads
             WHERE file IS NOT NULL AND file != ''
               AND NOT (
                   word != ''
                   AND word NOT LIKE 'account:%'
                   AND word NOT LIKE 'account：%'
                   AND word != '热点'
                   AND word NOT LIKE '[热点]%'
               )
            """
        ).fetchall()
        ids = [int(r[0]) for r in rows]
        removed_files = [str(r[1]) for r in rows if r[1]]
        norm_paths = [os.path.normpath(p) for p in removed_files]
        asset_marks = ",".join("?" for _ in norm_paths)
        if norm_paths:
            asset_ids = [
                int(r[0]) for r in conn.execute(
                    f"SELECT id FROM assets WHERE kind='download' AND file IN ({asset_marks})",
                    norm_paths,
                ).fetchall()
            ]
            if asset_ids:
                asset_marks2 = ",".join("?" for _ in asset_ids)
                conn.execute(f"DELETE FROM assets WHERE id IN ({asset_marks2})", asset_ids)
        if ids:
            marks = ",".join("?" for _ in ids)
            cur = conn.execute(f"DELETE FROM downloads WHERE id IN ({marks})", ids)
            removed = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return {"removed": removed, "asset_removed": len(removed_files), "files": removed_files}


def list_hotlist_days() -> list[str]:
    if not os.path.isdir(HOTLIST_DIR):
        return []
    return sorted(
        (fn[:-5] for fn in os.listdir(HOTLIST_DIR) if fn.endswith(".json")),
        reverse=True,
    )


# ======================================================== 连续动作筛选（姿态分段）
def pose_stats(db_path: str) -> dict[str, Any]:
    """姿态分段统计：总文件数、已分析/失败/待分析。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        total = conn.execute(
            "SELECT COUNT(DISTINCT file) FROM downloads "
            "WHERE file IS NOT NULL AND file != ''"
        ).fetchone()[0]
        analyzed = 0
        error = 0
        for status, n in conn.execute(
            "SELECT status, COUNT(*) FROM pose_segments GROUP BY status"
        ).fetchall():
            if status == "analyzed":
                analyzed += n
            elif status == "error":
                error += n
        longest_max = 0.0
        row = conn.execute(
            "SELECT MAX(longest) FROM pose_segments WHERE status = 'analyzed'"
        ).fetchone()
        if row and row[0]:
            longest_max = float(row[0])
        st = {"total": total, "analyzed": analyzed, "error": error,
              "pending": max(0, total - analyzed - error),
              "longest_max": round(longest_max, 2)}
        return st
    finally:
        conn.close()


def list_pose_items(
    db_path: str,
    *,
    status: str = "all",
    min_longest: float = 0.0,
    category: str = "",
    q: str = "",
) -> list[dict[str, Any]]:
    """连续动作筛选列表：每个本地视频文件一条，带上姿态分段结果。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.id, d.title, d.author, d.aweme_id, d.word,
                   d.size, d.tag_category, d.tags, d.status AS dl_status, d.file,
                   p.segments, p.top, p.longest, p.total_dur, p.fps,
                   p.num_frames, p.frames_analyzed, p.avg_conf,
                   p.status AS p_status, p.error AS p_error, p.analyzed_at
              FROM downloads d
              JOIN (
                   SELECT file, MIN(id) AS mid FROM downloads
                    WHERE file IS NOT NULL AND file != ''
                    GROUP BY file
              ) m ON d.id = m.mid
              LEFT JOIN pose_segments p ON p.file = d.file
            """
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for r in rows:
        item = _pose_item(r)
        if _match_pose(item, status=status, min_longest=min_longest, category=category, q=q):
            items.append(item)
    return items


def _pose_item(r: tuple) -> dict[str, Any]:
    """列顺序（见 list_pose_items SELECT）：
      0 d.id  1 title 2 author 3 aweme_id 4 word 5 size 6 tag_category 7 tags
      8 dl_status 9 file 10 segments 11 top 12 longest 13 total_dur 14 fps
      15 num_frames 16 frames_analyzed 17 avg_conf 18 p_status 19 p_error 20 analyzed_at
    """
    segments = _safe_json(r[10])
    top = _safe_json(r[11])
    return {
        "id": r[0],
        "title": r[1] or "",
        "author": r[2] or "",
        "aweme_id": r[3] or "",
        "word": r[4] or "",
        "size": r[5] or 0,
        "cat": r[6] or "",
        "tags": _safe_json(r[7]) if not isinstance(r[7], list) else r[7],
        "dl_status": r[8] or "",
        "file": r[9] or "",
        "has_file": bool(r[9]),
        "segments": segments,
        "top": top,
        "active_count": sum(1 for s in segments if s.get("active")),
        "longest": _num(r[12]),
        "total_dur": _num(r[13]),
        "fps": _num(r[14]),
        "num_frames": r[15] or 0,
        "frames_analyzed": r[16] or 0,
        "avg_conf": _num(r[17]),
        "status": r[18] or "pending",
        "error": r[19] or "",
        "analyzed_at": r[20] or "",
    }


def _match_pose(
    item: dict[str, Any],
    *,
    status: str,
    min_longest: float,
    category: str,
    q: str,
) -> bool:
    if status != "all" and item["status"] != status:
        return False
    if min_longest > 0:
        if item["status"] != "analyzed" or item["longest"] < min_longest:
            return False
    if category and item["cat"] != category:
        return False
    if q:
        hay = " ".join([item.get("title", ""), item.get("author", ""),
                        item.get("file", "")]).lower()
        if q.lower() not in hay:
            return False
    return True


def unanalyzed_pose_files(db_path: str, force: bool = False) -> list[str]:
    """需要做姿态分段的去重视频文件（已分析过的默认跳过，force=True 全部）。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        if force:
            rows = conn.execute(
                "SELECT DISTINCT file FROM downloads "
                "WHERE file IS NOT NULL AND file != ''"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT DISTINCT d.file FROM downloads d
                LEFT JOIN pose_segments p ON p.file = d.file
                WHERE d.file IS NOT NULL AND d.file != ''
                  AND (p.file IS NULL OR p.status IN ('pending', 'error'))
                """
            ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def set_pose_analysis(
    db_path: str,
    file: str,
    *,
    segments: list[dict[str, Any]] | None = None,
    top: list[dict[str, Any]] | None = None,
    longest: float = 0.0,
    total_dur: float = 0.0,
    fps: float = 0.0,
    num_frames: int = 0,
    frames_analyzed: int = 0,
    avg_conf: float = 0.0,
    status: str = "analyzed",
    error: str = "",
    now: datetime | None = None,
) -> None:
    """写入/覆盖某个视频文件的姿态分段结果。"""
    when = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            INSERT INTO pose_segments
                (file, segments, top, longest, total_dur, fps, num_frames,
                 frames_analyzed, avg_conf, status, error, analyzed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file) DO UPDATE SET
                segments=excluded.segments, top=excluded.top, longest=excluded.longest,
                total_dur=excluded.total_dur, fps=excluded.fps, num_frames=excluded.num_frames,
                frames_analyzed=excluded.frames_analyzed, avg_conf=excluded.avg_conf,
                status=excluded.status, error=excluded.error, analyzed_at=excluded.analyzed_at
            """,
            (
                file,
                json.dumps(segments or [], ensure_ascii=False),
                json.dumps(top or [], ensure_ascii=False),
                longest, total_dur, fps, num_frames, frames_analyzed, avg_conf,
                status, error,
                when if status == "analyzed" else None,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def set_pose_status(db_path: str, file: str, status: str, error: str = "") -> None:
    set_pose_analysis(db_path, file, status=status, error=error)


def _safe_json(raw) -> Any:
    if raw is None:
        return []
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw) if raw else []
    except (ValueError, TypeError):
        return []


def _num(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (ValueError, TypeError):
        return 0.0


# ======================================================== 资产（连续动作片段）
def _assets_dir(db_path: str) -> str:
    return os.path.join(PROJECT_ROOT, "downloads", "segments")


def sync_assets(db_path: str) -> dict[str, Any]:
    """同步下载原片和各工具登记的产物（幂等）。"""
    init(db_path)
    seg_dir = _assets_dir(db_path)
    found: list[str] = []
    found += _sync_download_assets(db_path)
    _sync_output_assets(db_path)
    # 回填来源平台：按 source_file 关联下载记录推断（B站文件名统一 bili_ 前缀，兼容两种分隔符）
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            UPDATE assets
               SET platform = COALESCE((
                   SELECT CASE WHEN d.word = 'bilibili'
                                OR d.file GLOB '*bili_*'
                           THEN 'bilibili' ELSE 'douyin' END
                     FROM downloads d
                    WHERE d.file = assets.source_file
                    ORDER BY d.id LIMIT 1), '')
             WHERE 1 = 1
            """
        )
        conn.commit()
    finally:
        conn.close()
    if os.path.isdir(seg_dir):
        for fn in os.listdir(seg_dir):
            if not fn.lower().endswith(".mp4"):
                continue
            m = re.match(
                r"^(?P<stem>.+)_seg(?P<rank>\d+)_(?P<start>-?\d+)-(?P<end>-?\d+)\.mp4$",
                fn,
            )
            if not m:
                continue
            abs_path = os.path.join(seg_dir, fn)
            rel = os.path.normpath(os.path.relpath(abs_path, PROJECT_ROOT))
            stem = m.group("stem")
            source_file = _find_source_file(db_path, stem)
            duration = float(m.group("end")) - float(m.group("start"))
            _upsert_asset(
                db_path,
                file=rel,
                basename=fn,
                source_file=source_file,
                rank=int(m.group("rank")),
                start=float(m.group("start")),
                end=float(m.group("end")),
                duration=duration,
                size=int(os.path.getsize(abs_path)),
                kind="tool",
                tool_id="action",
                tool_name="连续动作筛选",
            )
            found.append(rel)
    # 清理已不存在片段的记录
    action_collection = ensure_asset_collection(
        db_path, "连续动作筛选", tool_id="action", tool_name="连续动作筛选"
    )
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT id, file, kind, tool_id FROM assets").fetchall()
        for aid, rel, asset_kind, tool_id in rows:
            abs_path = os.path.join(PROJECT_ROOT, rel)
            managed = asset_kind == "download" or tool_id == "action"
            if (managed and rel not in found) or not os.path.isfile(abs_path):
                conn.execute("DELETE FROM assets WHERE id = ?", (aid,))
        if action_collection:
            conn.execute(
                """
                UPDATE assets
                   SET collection_id = ?
                 WHERE kind = 'tool' AND tool_id = 'action'
                   AND (collection_id IS NULL OR collection_id = 0)
                """,
                (action_collection,),
            )
        conn.commit()
    finally:
        conn.close()
    return {"assets": len(found)}


def _sync_output_assets(db_path: str) -> int:
    """扫描 out/ 中的媒体产物，自动登记为独立工具分组。"""
    out_dir = os.path.join(PROJECT_ROOT, "out")
    media_exts = {
        ".mp4", ".mov", ".mkv", ".webm", ".avi",
        ".png", ".jpg", ".jpeg", ".webp", ".gif",
        ".mp3", ".wav", ".m4a",
    }
    count = 0
    if not os.path.isdir(out_dir):
        return count
    for root, _dirs, files in os.walk(out_dir):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in media_exts:
                continue
            abs_path = os.path.join(root, fn)
            rel = os.path.normpath(os.path.relpath(abs_path, PROJECT_ROOT))
            register_asset(
                db_path,
                file=rel,
                size=int(os.path.getsize(abs_path)),
                category="工具产物",
                tool_id="output_media",
                tool_name="输出目录产物",
                kind="tool",
            )
            count += 1
    return count


def _sync_download_assets(db_path: str) -> list[str]:
    """把本地存在的下载原片登记为 download 类型资产。"""
    keep_categories: set[str] | None = None
    if os.path.isfile(DEFAULT_ASSETS_CONFIG_PATH):
        with open(DEFAULT_ASSETS_CONFIG_PATH, encoding="utf-8") as config_file:
            configured = json.load(config_file).get("keep_categories")
        if isinstance(configured, list):
            keep_categories = {str(item) for item in configured if str(item)}
            if "*" in keep_categories:
                keep_categories = None  # "*" 表示收录全部分类

    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT d.file, d.id, COALESCE(p.total_dur, 0), d.tag_category
              FROM downloads d
              JOIN (
                   SELECT file, MIN(id) AS mid FROM downloads
                    WHERE file IS NOT NULL AND file != ''
                    GROUP BY file
              ) m ON d.id = m.mid
              LEFT JOIN pose_segments p ON p.file = d.file
             ORDER BY d.created_at DESC, d.id DESC
            """
        ).fetchall()
    finally:
        conn.close()

    found: list[str] = []
    for rel, _source_id, duration, tag_category in rows:
        if keep_categories is not None and (tag_category or "") not in keep_categories:
            continue
        abs_path = os.path.join(PROJECT_ROOT, rel)
        if not os.path.isfile(abs_path):
            continue
        normalized = os.path.normpath(rel)
        _upsert_asset(
            db_path,
            file=normalized,
            basename=os.path.basename(normalized),
            source_file=normalized,
            rank=0,
            start=0.0,
            end=_num(duration),
            duration=_num(duration),
            size=int(os.path.getsize(abs_path)),
            kind="download",
            tool_id="",
            tool_name="",
        )
        found.append(normalized)
    return found


def replace_pose_frames(
    db_path: str,
    file: str,
    frames: list[dict[str, Any]],
    *,
    now: datetime | None = None,
) -> int:
    """重建某个视频的帧级动作库（重新分析时保证数据幂等）。"""
    init(db_path)
    when = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        conn.execute("BEGIN")
        conn.execute("DELETE FROM pose_frames WHERE file = ?", (file,))
        rows = []
        for frame in frames:
            descriptor = frame.get("descriptor")
            blob = None
            if descriptor is not None:
                arr = np.asarray(descriptor, dtype=np.float32).reshape(-1)
                blob = arr.tobytes()
            rows.append((
                file, int(frame["frame_idx"]), float(frame.get("time_sec", 0.0)),
                1 if frame.get("has_person") else 0, blob,
                int(frame.get("segment_idx", -1)), 1 if frame.get("segment_start") else 0,
                1 if frame.get("active") else 0, when,
            ))
        conn.executemany(
            """
            INSERT INTO pose_frames
                (file, frame_idx, time_sec, has_person, descriptor,
                 segment_idx, segment_start, active, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def pose_frame_stats(db_path: str) -> dict[str, Any]:
    init(db_path)
    conn = _conn(db_path)
    try:
        files = conn.execute(
            "SELECT COUNT(DISTINCT file) FROM pose_frames WHERE descriptor IS NOT NULL"
        ).fetchone()[0]
        frames = conn.execute(
            "SELECT COUNT(*) FROM pose_frames WHERE descriptor IS NOT NULL"
        ).fetchone()[0]
        starts = conn.execute(
            "SELECT COUNT(*) FROM pose_frames WHERE descriptor IS NOT NULL "
            "AND has_person = 1 AND segment_start = 1"
        ).fetchone()[0]
        return {"files": files, "frames": frames, "matchable_starts": starts}
    finally:
        conn.close()


def get_pose_frame_descriptor(
    db_path: str, file: str, frame_idx: int
) -> list[float] | None:
    """读取库内某一帧的姿态描述子，作为检索起点。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT descriptor FROM pose_frames "
            "WHERE file = ? AND frame_idx = ? AND descriptor IS NOT NULL",
            (file, frame_idx),
        ).fetchone()
        if not row or not row[0]:
            return None
        return np.frombuffer(row[0], dtype=np.float32).tolist()
    finally:
        conn.close()


def search_pose_sequences(
    db_path: str,
    query_descriptor: list[float] | np.ndarray,
    *,
    top_k: int = 20,
    min_score: float = 0.55,
    category: str = "",
    q: str = "",
) -> list[dict[str, Any]]:
    """用起始姿态描述子匹配每个动作段的起始帧。"""
    init(db_path)
    query = np.asarray(query_descriptor, dtype=np.float32).reshape(-1)
    if query.size != 34 or not np.isfinite(query).all():
        raise ValueError("姿态描述子必须是 34 个有效数值")
    query_norm = float(np.linalg.norm(query))
    if query_norm < 1e-6:
        raise ValueError("姿态描述子不能是全零向量")

    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT p.file, p.frame_idx, p.time_sec, p.descriptor,
                   p.segment_idx, p.active,
                   COALESCE(d.id, 0), COALESCE(d.id IS NOT NULL, 0),
                   COALESCE(a.id, 0),
                   COALESCE(d.title, a.basename, p.file),
                   COALESCE(d.author, ''),
                   COALESCE(d.word, ''),
                   COALESCE(d.tag_category, a.tag_category, ''),
                   s.segments,
                   COALESCE(c.name, '')
              FROM pose_frames p
              JOIN (
                   SELECT file, MIN(id) AS mid FROM downloads
                    WHERE file IS NOT NULL AND file != ''
                    GROUP BY file
              ) m ON p.file = m.file
              JOIN downloads d ON d.id = m.mid
              LEFT JOIN pose_segments s ON s.file = p.file
              LEFT JOIN assets a
                ON a.file = p.file AND a.kind = 'tool'
              LEFT JOIN asset_collections c ON c.id = a.collection_id
             WHERE p.has_person = 1
               AND p.segment_start = 1
               AND p.active = 1
               AND p.descriptor IS NOT NULL
             ORDER BY p.file, p.frame_idx
            """
        ).fetchall()
    finally:
        conn.close()

    best_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not row[3]:
            continue
        candidate = np.frombuffer(row[3], dtype=np.float32)
        score = float(max(0.0, np.dot(query, candidate) /
                          (query_norm * max(1e-6, float(np.linalg.norm(candidate))))))
        if score < min_score:
            continue
        segments = _safe_json(row[13])
        segment_idx = int(row[4])
        segment = next((s for s in segments if int(s.get("idx", -1)) == segment_idx), None)
        source_type = "download" if row[7] else "asset"
        source_id = int(row[6] or 0)
        asset_id = int(row[8] or 0)
        source_key = f"{source_type}:{source_id if source_type == 'asset' else row[0]}"
        collection_name = row[14] or ""
        item = {
            "file": row[0],
            "source_key": source_key,
            "source_type": source_type,
            "source_id": source_id,
            "asset_id": asset_id,
            "frame_idx": int(row[1]),
            "frame_time": _num(row[2]),
            "score": round(score, 4),
            "segment_idx": segment_idx,
            "active": bool(row[5]),
            "segment": segment,
            "source_id": source_id,
            "asset_id": asset_id,
            "title": row[9] or "",
            "author": row[10] or "",
            "word": row[11] or "",
            "cat": row[12] or "",
            "collection_name": collection_name,
            "video_url": f"/media/{source_id}" if source_type == "download" and source_id else f"/asset-file/{asset_id}",
        }
        if category and item["cat"] != category:
            continue
        if q:
            haystack = " ".join([
                item["title"], item["author"], item["word"], item["file"]
            ]).lower()
            if q.lower() not in haystack:
                continue
        old = best_by_key.get(source_key)
        if old is None or item["score"] > old["score"]:
            best_by_key[source_key] = item

    return sorted(best_by_key.values(), key=lambda x: x["score"], reverse=True)[:top_k]


def _find_source_file(db_path: str, stem: str) -> str:
    """根据片段文件名 stem 找到源视频在 downloads.file 里的相对路径。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        # 优先精确 basename==stem.mp4
        rows = conn.execute(
            "SELECT file FROM downloads "
            "WHERE file IS NOT NULL AND file != '' ORDER BY id DESC"
        ).fetchall()
        match = ""
        for (f,) in rows:
            base = os.path.basename(f)
            if base == stem + ".mp4" or base == stem + ".MP4":
                match = f
                break
        return match
    finally:
        conn.close()


def _upsert_asset(
    db_path: str,
    *,
    file: str,
    basename: str,
    source_file: str,
    rank: int,
    start: float,
    end: float,
    duration: float,
    size: int,
    category: str | None = None,
    kind: str | None = None,
    tool_id: str | None = None,
    tool_name: str | None = None,
    collection_id: int | None = None,
    run_id: int | None = None,
    now: datetime | None = None,
) -> None:
    when = (now or datetime.now()).isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        if not source_file:
            row = conn.execute(
                "SELECT source_file FROM assets WHERE file = ?", (file,)
            ).fetchone()
            if row:
                source_file = row[0] or ""
        existing_category = ""
        existing_kind = "tool"
        existing_tool_id = ""
        existing_tool_name = ""
        existing_collection_id = 0
        existing_run_id = 0
        if (category is None or kind is None or tool_id is None or tool_name is None
                or collection_id is None or run_id is None):
            row = conn.execute(
                "SELECT category, kind, tool_id, tool_name, collection_id, run_id FROM assets WHERE file = ?", (file,)
            ).fetchone()
            if row:
                existing_category = row[0] or ""
                existing_kind = row[1] or "tool"
                existing_tool_id = row[2] or ""
                existing_tool_name = row[3] or ""
                existing_collection_id = int(row[4] or 0)
                existing_run_id = int(row[5] or 0)
        if category is None:
            category = existing_category
        if kind is None:
            kind = existing_kind
        if tool_id is None:
            tool_id = existing_tool_id
        if tool_name is None:
            tool_name = existing_tool_name
        if collection_id is None:
            collection_id = existing_collection_id
        if run_id is None:
            run_id = existing_run_id
        conn.execute(
            """
            INSERT INTO assets
                (file, basename, source_file, rank, start, end, duration, size,
                 category, kind, tool_id, tool_name, collection_id, run_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(file) DO UPDATE SET
                basename=excluded.basename, source_file=excluded.source_file,
                rank=excluded.rank, start=excluded.start, end=excluded.end,
                duration=excluded.duration, size=excluded.size,
                category=excluded.category, kind=excluded.kind,
                tool_id=excluded.tool_id, tool_name=excluded.tool_name,
                collection_id=excluded.collection_id, run_id=excluded.run_id,
                created_at=excluded.created_at
            """,
            (file, basename, source_file, rank, start, end, duration, size,
             category, kind, tool_id, tool_name, collection_id, run_id, when),
        )
        conn.commit()
    finally:
        conn.close()


def list_assets(
    db_path: str,
    *,
    q: str = "",
    source: str = "",
    category: str = "",
    kind: str = "",
    asset_type: str = "",
    tool: str = "",
    tag: str = "",
    platform: str = "",
    run_id: int = 0,
    favorite: int = 0,
    limit: int = 500,
) -> list[dict[str, Any]]:
    init(db_path)
    conn = _conn(db_path)
    try:
        conditions = ["NOT (a.kind = 'tool' AND COALESCE(a.tool_id, '') = 'tagging')"]
        params: list[Any] = []
        if run_id:
            conditions.append("a.run_id = ?")
            params.append(int(run_id))
        if favorite:
            conditions.append("a.id IN (SELECT asset_id FROM asset_favorite_items WHERE favorite_id = ?)")
            params.append(int(favorite))
        if kind:
            conditions.append(
                "CASE WHEN a.kind = 'download' THEN 'download' "
                "ELSE 'collection:' || COALESCE(a.collection_id, 0) END = ?"
            )
            params.append(kind)
        where_sql = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = conn.execute(
            """
            SELECT a.id, a.file, a.basename, a.source_file, a.rank, a.start,
                   a.end, a.duration, a.size, a.created_at,
                   (SELECT title FROM downloads WHERE file=a.source_file ORDER BY id LIMIT 1) AS title,
                   (SELECT author FROM downloads WHERE file=a.source_file ORDER BY id LIMIT 1) AS author,
                   (SELECT tag_category FROM downloads WHERE file=a.source_file ORDER BY id LIMIT 1) AS cat,
                   (SELECT id FROM downloads WHERE file=a.source_file ORDER BY id LIMIT 1) AS source_id,
                   a.category,
                   a.kind,
                   a.tool_id,
                   a.tool_name,
                   a.collection_id,
                   c.name,
                   a.tag_category,
                   a.tags,
                   a.tag_summary,
                   a.tag_status,
                   a.tag_error,
                   a.tagged_at,
                   (SELECT tags FROM downloads WHERE file=a.source_file ORDER BY id LIMIT 1) AS source_tags,
                   a.platform,
                   (SELECT COUNT(*) FROM assets t
                     WHERE t.kind = 'tool' AND t.source_file = a.file) AS derived_count,
                   a.run_id
              FROM assets a
              LEFT JOIN asset_collections c ON c.id = a.collection_id
             """ + where_sql + """
             ORDER BY a.created_at DESC, a.id DESC
             LIMIT ?
            """,
            (*params, limit),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for r in rows:
        item = _asset_item(r)
        if _match_asset(
            item,
            q=q,
            source=source,
            category=category,
            kind=kind,
            asset_type=asset_type,
            tool=tool,
            tag=tag,
            platform=platform,
        ):
            items.append(item)
    return items

def list_assets_page(
    db_path: str,
    *,
    q: str = "",
    source: str = "",
    category: str = "",
    kind: str = "",
    asset_type: str = "",
    tool: str = "",
    tag: str = "",
    platform: str = "",
    run_id: int = 0,
    favorite: int = 0,
    page: int = 1,
    page_size: int = 24,
) -> tuple[list[dict[str, Any]], int]:
    """Return one gallery page and the total count after all filters."""
    safe_page = max(1, int(page or 1))
    safe_size = min(100, max(1, int(page_size or 24)))
    items = list_assets(
        db_path,
        q=q,
        source=source,
        category=category,
        kind=kind,
        asset_type=asset_type,
        tool=tool,
        tag=tag,
        platform=platform,
        run_id=run_id,
        favorite=favorite,
        limit=10000,
    )
    total = len(items)
    start = (safe_page - 1) * safe_size
    return items[start:start + safe_size], total


def _asset_item(r: tuple) -> dict[str, Any]:
    file_rel = r[1] or ""
    return {
        "id": r[0],
        "file": file_rel,
        "basename": r[2] or "",
        "source_file": r[3] or "",
        "rank": r[4] or 0,
        "start": _num(r[5]),
        "end": _num(r[6]),
        "duration": _num(r[7]),
        "size": r[8] or 0,
        "created_at": r[9] or "",
        "title": r[10] or "",
        "author": r[11] or "",
        "cat": r[12] or "",
        "source_id": r[13] or 0,
        "category": r[14] or "",
        "kind": r[15] or "tool",
        "tool_id": r[16] or "",
        "tool_name": r[17] or "",
        "group_id": _asset_group_id(r[15] or "tool", int(r[18] or 0), r[16] or ""),
        "group_name": _asset_group_name(
            r[15] or "tool", int(r[18] or 0), r[19] or "", r[17] or ""
        ),
        "collection_id": int(r[18] or 0),
        "collection_name": r[19] or "",
        "tag_category": r[20] or r[12] or "",
        "tags": _safe_json(r[21]) or _safe_json(r[26]),
        "tag_summary": r[22] or "",
        "tag_status": r[23] or "untagged",
        "tag_error": r[24] or "",
        "tagged_at": r[25] or "",
        "url": _asset_url(r[15] or "tool", r[16] or "", r[0], r[13], r[2]),
        "exists": bool(file_rel and os.path.isfile(os.path.join(PROJECT_ROOT, file_rel))),
        "platform": r[27] or "",
        "derived_count": int(r[28] or 0),
        "run_id": int(r[29] or 0),
    }


def _match_asset(
    item: dict[str, Any], *, q: str, source: str, category: str = "",
    kind: str = "", asset_type: str = "", tool: str = "", tag: str = "",
    platform: str = "",
) -> bool:
    if platform and (item.get("platform") or "") != platform:
        return False
    if kind and item.get("group_id") != kind:
        return False
    if asset_type and item.get("kind") != asset_type:
        return False
    if tool and item.get("kind") == "tool" and item.get("tool_id") != tool:
        return False
    if category:
        wanted = "" if category == "未分类" else category
        if (item.get("category") or item.get("cat") or "") != wanted:
            return False
    if tag:
        wanted = tag.strip().casefold()
        tag_values = [str(item.get("tag_category") or "")]
        tag_values.extend(str(value) for value in (item.get("tags") or []))
        if wanted not in {value.strip().casefold() for value in tag_values if value.strip()}:
            return False
    if source:
        hay_source = (item.get("source_file") or "") + (item.get("title") or "")
        if source.lower() not in hay_source.lower():
            return False
    if q:
        hay = " ".join([
            item.get("title", ""), item.get("author", ""),
            item.get("basename", ""), item.get("source_file", ""),
        ]).lower()
        if q.lower() not in hay:
            return False
    return True


def asset_stats(db_path: str) -> dict[str, Any]:
    init(db_path)
    conn = _conn(db_path)
    try:
        managed_filter = "NOT (kind = 'tool' AND COALESCE(tool_id, '') = 'tagging')"
        n = conn.execute(
            f"SELECT COUNT(*) FROM assets WHERE {managed_filter}"
        ).fetchone()[0]
        total_size = conn.execute(
            f"SELECT COALESCE(SUM(size), 0) FROM assets WHERE {managed_filter}"
        ).fetchone()[0]
        sources = conn.execute(
            f"""
            SELECT COUNT(DISTINCT source_file) FROM assets
             WHERE source_file IS NOT NULL AND source_file != '' AND {managed_filter}
            """
        ).fetchone()[0]
        categories = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(a.category, ''),
                            (SELECT tag_category FROM downloads
                              WHERE file=a.source_file ORDER BY id LIMIT 1), ''),
                   COUNT(*), COALESCE(SUM(a.size), 0)
              FROM assets a
             WHERE {managed_filter}
             GROUP BY 1
             ORDER BY COUNT(*) DESC
            """
        ).fetchall()
        category_items = [
            {"name": r[0] or "未分类", "count": r[1], "size": r[2] or 0}
            for r in categories
        ]
        asset_group_rows = conn.execute(
            f"""
            SELECT CASE WHEN kind = 'download' THEN 'download'
                        ELSE COALESCE(NULLIF(tool_id, ''), 'unknown') END,
                   CASE WHEN kind = 'download' THEN '原下载视频'
                        ELSE COALESCE(NULLIF(tool_name, ''), '未命名工具') END,
                   COUNT(*), COALESCE(SUM(size), 0)
              FROM assets a
             WHERE {managed_filter}
             GROUP BY 1, 2
            """
        ).fetchall()
        tool_counts = {
            r[0]: {"name": r[1], "count": r[2], "size": r[3] or 0}
            for r in asset_group_rows
        }
        canonical_tools = [
            ("download", "原下载视频"),
            ("action", "连续动作筛选"),
            ("pose_match", "首帧姿态匹配"),
        ]
        kinds = [
            {
                "id": tool_id,
                "name": name,
                "count": tool_counts.get(tool_id, {}).get("count", 0),
                "size": tool_counts.get(tool_id, {}).get("size", 0),
            }
            for tool_id, name in canonical_tools
        ]
        known_tool_ids = {tool_id for tool_id, _name in canonical_tools} | {"tagging"}
        kinds.extend(
            {"id": tool_id, "name": values["name"], "count": values["count"], "size": values["size"]}
            for tool_id, values in sorted(tool_counts.items())
            if tool_id not in known_tool_ids
        )
        tag_rows = conn.execute(
            f"""
            SELECT a.tag_category, a.tags,
                   (SELECT tag_category FROM downloads
                     WHERE file=a.source_file ORDER BY id LIMIT 1),
                   (SELECT tags FROM downloads
                     WHERE file=a.source_file ORDER BY id LIMIT 1)
              FROM assets a
             WHERE {managed_filter}
            """
        ).fetchall()
        category_counter: dict[str, int] = {}
        tag_counter: dict[str, int] = {}
        for asset_category, asset_tags, source_category, source_tags in tag_rows:
            tag_category = asset_category or source_category or ""
            raw_tags = asset_tags or source_tags or ""
            if tag_category:
                category_counter[tag_category] = category_counter.get(tag_category, 0) + 1
            try:
                asset_tags = json.loads(raw_tags) if raw_tags else []
            except (TypeError, ValueError, json.JSONDecodeError):
                asset_tags = []
            if isinstance(asset_tags, list):
                for tag_value in asset_tags:
                    if tag_value:
                        tag_counter[str(tag_value)] = tag_counter.get(str(tag_value), 0) + 1
        tag_options = [
            {"type": "category", "value": name, "label": name, "count": count}
            for name, count in sorted(category_counter.items(), key=lambda item: (-item[1], item[0]))
        ]
        tag_options.extend(
            {"type": "tag", "value": name, "label": name, "count": count}
            for name, count in sorted(tag_counter.items(), key=lambda item: (-item[1], item[0]))
        )
        platform_rows = conn.execute(
            f"""
            SELECT COALESCE(NULLIF(platform, ''), 'unknown'), COUNT(*)
              FROM assets WHERE {managed_filter}
             GROUP BY 1
            """
        ).fetchall()
        platform_names = {"douyin": "抖音", "bilibili": "B站", "unknown": "未知来源"}
        platforms = [
            {"id": pid, "name": platform_names.get(pid, pid), "count": count}
            for pid, count in platform_rows
        ]
        unprocessed = conn.execute(
            """
            SELECT COUNT(*) FROM assets a
             WHERE a.kind = 'download'
               AND NOT EXISTS (
                   SELECT 1 FROM assets t
                    WHERE t.kind = 'tool' AND t.source_file = a.file
               )
            """
        ).fetchone()[0]
        return {
            "count": n, "size": total_size or 0, "sources": sources,
            "categories": category_items,
            "kinds": kinds,
            "tag_options": tag_options,
            "platforms": platforms,
            "unprocessed": int(unprocessed or 0),
        }
    finally:
        conn.close()


def asset_chain(db_path: str, asset_id: int) -> dict[str, Any] | None:
    """处理链视图：原片资产返回全部衍生片段；工具资产返回原片和同批兄弟片段。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if not row:
            return None
        cols = [d[1] for d in conn.execute("PRAGMA table_info(assets)").fetchall()]
        a = dict(zip(cols, row))

        def chain_item(r) -> dict[str, Any]:
            it = dict(zip(cols, r))
            source_id = conn.execute(
                "SELECT id FROM downloads WHERE file = ? ORDER BY id LIMIT 1",
                (it.get("source_file") or "",),
            ).fetchone()
            return {
                "id": it["id"], "basename": it.get("basename") or "",
                "rank": int(it.get("rank") or 0),
                "start": _num(it.get("start")), "end": _num(it.get("end")),
                "duration": _num(it.get("duration")),
                "size": int(it.get("size") or 0),
                "kind": it.get("kind") or "tool",
                "tool_id": it.get("tool_id") or "",
                "tool_name": it.get("tool_name") or "",
                "created_at": it.get("created_at") or "",
                "url": _asset_url(it.get("kind") or "tool", it.get("tool_id") or "",
                                  it["id"], source_id[0] if source_id else 0,
                                  it.get("basename") or ""),
            }

        if (a.get("kind") or "") == "download":
            derived = conn.execute(
                """
                SELECT * FROM assets
                 WHERE kind = 'tool' AND source_file = ?
                 ORDER BY tool_id, rank, id
                """,
                (a.get("file") or "",),
            ).fetchall()
            return {"mode": "download", "derived": [chain_item(r) for r in derived]}

        source = conn.execute(
            """
            SELECT * FROM assets
             WHERE kind = 'download' AND file = ?
             ORDER BY id LIMIT 1
            """,
            (a.get("source_file") or "",),
        ).fetchone()
        siblings = conn.execute(
            """
            SELECT * FROM assets
             WHERE kind = 'tool' AND source_file = ? AND tool_id = ?
             ORDER BY rank, id
            """,
            (a.get("source_file") or "", a.get("tool_id") or ""),
        ).fetchall()
        source_info = None
        if source:
            si = chain_item(source)
            dl = conn.execute(
                "SELECT id, title, author FROM downloads WHERE file = ? ORDER BY id LIMIT 1",
                (a.get("source_file") or "",),
            ).fetchone()
            si["source_id"] = dl[0] if dl else 0
            si["title"] = dl[1] if dl else si["basename"]
            si["author"] = dl[2] if dl else ""
            source_info = si
        return {
            "mode": "tool",
            "source": source_info,
            "siblings": [chain_item(r) for r in siblings],
            "current_id": asset_id,
        }
    finally:
        conn.close()


def get_asset(db_path: str, aid: int) -> dict[str, Any] | None:
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT id, file, basename, source_file, kind, tool_id, tool_name, collection_id "
            "FROM assets WHERE id = ?",
            (aid,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "file": row[1], "basename": row[2],
            "source_file": row[3] or "", "kind": row[4],
            "tool_id": row[5] or "", "tool_name": row[6] or "",
            "collection_id": int(row[7] or 0),
        }
    finally:
        conn.close()


def set_asset_category(db_path: str, aid: int, category: str) -> bool:
    init(db_path)
    conn = _conn(db_path)
    try:
        cur = conn.execute(
            "UPDATE assets SET category = ? WHERE id = ?", (category.strip(), aid)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_asset_collections(db_path: str) -> list[dict[str, Any]]:
    """列出可选择的输出资产集。"""
    init(db_path)
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            """
            SELECT c.id, c.name, c.tool_id, c.tool_name, c.created_at,
                   COUNT(a.id), COALESCE(SUM(a.size), 0)
              FROM asset_collections c
              LEFT JOIN assets a
                ON a.collection_id = c.id AND a.kind = 'tool'
             GROUP BY c.id
             ORDER BY c.created_at DESC, c.id DESC
            """
        ).fetchall()
        return [
            {
                "id": r[0], "name": r[1], "tool_id": r[2], "tool_name": r[3],
                "created_at": r[4], "count": r[5], "size": r[6] or 0,
            }
            for r in rows
        ]
    finally:
        conn.close()


def ensure_asset_collection(
    db_path: str,
    name: str,
    *,
    tool_id: str = "",
    tool_name: str = "",
) -> int | None:
    """按名字取资产集；不存在则自动新建。"""
    clean = name.strip()
    if not clean:
        return None
    init(db_path)
    when = datetime.now().isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM asset_collections WHERE name = ?", (clean,)
        ).fetchone()
        if row:
            return int(row[0])
        cur = conn.execute(
            """
            INSERT INTO asset_collections(name, tool_id, tool_name, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (clean, tool_id, tool_name, when),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_tool_sources(
    db_path: str,
    *,
    source: str = "all",
    q: str = "",
    collection_id: int = 0,
    action_status: str = "all",
    tag_status: str = "all",
    limit: int = 500,
    page: int = 1,
    page_size: int = 0,
    return_meta: bool = False,
) -> list[dict[str, Any]] | dict[str, Any]:
    """统一返回可被工具选择的原视频与工具资产视频。"""
    init(db_path)
    wanted = source if source in ("all", "download", "asset") else "all"
    items: list[dict[str, Any]] = []
    conn = _conn(db_path)
    try:
        if wanted in ("all", "download"):
            rows = conn.execute(
                """
                SELECT d.file, d.title, d.author, d.size, d.tag_category,
                       COALESCE(d.tag_status, 'untagged'),
                       COALESCE(p.status, 'pending'), p.longest,
                       COALESCE(d.created_at, '')
                  FROM downloads d
                  JOIN (
                       SELECT file, MIN(id) AS mid FROM downloads
                        WHERE file IS NOT NULL AND file != ''
                        GROUP BY file
                  ) m ON d.id = m.mid
                  LEFT JOIN pose_segments p ON p.file = d.file
                 ORDER BY d.created_at DESC, d.id DESC
                """
            ).fetchall()
            for r in rows:
                file = r[0] or ""
                action_state = r[6] or "pending"
                tag_state = r[5] or "untagged"
                if action_status != "all" and action_state != action_status:
                    continue
                if tag_status != "all" and tag_state != tag_status:
                    continue
                item = {
                    "key": f"download:{file}", "source_type": "download",
                    "source_name": "原视频", "id": 0, "file": file,
                    "basename": os.path.basename(file), "title": r[1] or file,
                    "author": r[2] or "", "category": r[4] or "",
                    "collection_id": 0, "collection_name": "",
                    "size": r[3] or 0, "duration": _num(r[7]),
                    "action_status": action_state, "tag_status": tag_state,
                    "created_at": r[8] or "",
                    "url": "",
                    "exists": os.path.isfile(os.path.join(PROJECT_ROOT, file)),
                }
                if q and q.lower() not in " ".join([
                    item["title"], item["author"], item["basename"]
                ]).lower():
                    continue
                if item["exists"]:
                    items.append(item)
        if wanted in ("all", "asset"):
            rows = conn.execute(
                """
                SELECT a.id, a.file, a.basename, a.source_file, a.duration, a.size,
                       a.kind, a.tool_id, a.tool_name, a.collection_id,
                       COALESCE(c.name, '未分组资产'),
                       COALESCE(a.tag_status, 'untagged'),
                       COALESCE(a.tag_category, ''),
                       COALESCE(a.created_at, ''),
                       (SELECT title FROM downloads WHERE file=a.source_file ORDER BY id LIMIT 1),
                       (SELECT author FROM downloads WHERE file=a.source_file ORDER BY id LIMIT 1)
                  FROM assets a
                  LEFT JOIN asset_collections c ON c.id = a.collection_id
                 WHERE a.kind = 'tool'
                   AND (LOWER(a.file) LIKE '%.mp4' OR LOWER(a.file) LIKE '%.mov'
                        OR LOWER(a.file) LIKE '%.mkv' OR LOWER(a.file) LIKE '%.webm')
                 ORDER BY a.created_at DESC, a.id DESC
                """
            ).fetchall()
            for r in rows:
                tag_state = r[11] or "untagged"
                if tag_status != "all" and tag_state != tag_status:
                    continue
                if collection_id and int(r[9] or 0) != collection_id:
                    continue
                item = {
                    "key": f"asset:{r[0]}", "source_type": "asset",
                    "source_name": "资产视频", "id": int(r[0]), "file": r[1] or "",
                    "basename": r[2] or "", "title": r[2] or r[14] or r[1],
                    "author": r[15] or "", "category": r[12] or "",
                    "collection_id": int(r[9] or 0), "collection_name": r[10] or "",
                    "size": r[5] or 0, "duration": _num(r[4]),
                    "action_status": "pending", "tag_status": tag_state,
                    "created_at": r[13] or "", "tool_id": r[7] or "",
                    "tool_name": r[8] or "", "url": "",
                    "exists": os.path.isfile(os.path.join(PROJECT_ROOT, r[1] or "")),
                }
                if q and q.lower() not in " ".join([
                    item["title"], item["author"], item["basename"],
                    item["collection_name"]
                ]).lower():
                    continue
                if item["exists"]:
                    items.append(item)
    finally:
        conn.close()
    total = len(items)
    if page_size > 0:
        page = max(1, int(page))
        size = max(1, min(int(page_size), 200))
        start = (page - 1) * size
        page_items = items[start:start + size]
    else:
        size = max(0, int(limit))
        page_items = items[:size] if size else items
    if return_meta:
        return {
            "items": page_items,
            "total": total,
            "page": max(1, int(page)),
            "page_size": size,
            "total_pages": max(1, (total + size - 1) // size) if size else 1,
        }
    return page_items


def create_tool_run(
    db_path: str,
    *,
    tool_id: str,
    tool_name: str,
    total_count: int,
    output_collection_id: int = 0,
    params: dict[str, Any] | None = None,
) -> int:
    init(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        cur = conn.execute(
            """
            INSERT INTO tool_runs
                (tool_id, tool_name, status, total_count,
                 output_collection_id, params, created_at)
            VALUES (?, ?, 'queued', ?, ?, ?, ?)
            """,
            (tool_id, tool_name, total_count, output_collection_id,
             json.dumps(params or {}, ensure_ascii=False), now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def start_tool_run(db_path: str, run_id: int) -> None:
    init(db_path)
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            UPDATE tool_runs
               SET status = CASE WHEN status = 'queued' THEN 'running' ELSE status END,
                   started_at = COALESCE(started_at, ?)
             WHERE id = ?
            """,
            (datetime.now().isoformat(timespec="seconds"), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def finish_empty_tool_run(db_path: str, run_id: int) -> None:
    init(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        conn.execute(
            """
            UPDATE tool_runs
               SET status='completed', started_at=COALESCE(started_at, ?),
                   finished_at=?
             WHERE id=? AND total_count=0
            """,
            (now, now, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def update_tool_run_progress(
    db_path: str, run_id: int, *, success: bool = True, error: str = ""
) -> None:
    init(db_path)
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT total_count, done_count, error_count FROM tool_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        if not row:
            return
        total, done, errors = int(row[0] or 0), int(row[1] or 0), int(row[2] or 0)
        done += 1
        errors += 0 if success else 1
        status = "running"
        finished = None
        if done >= total:
            status = "failed" if errors >= done else ("partial" if errors else "completed")
            finished = now
        conn.execute(
            """
            UPDATE tool_runs
               SET status=?, done_count=?, error_count=?, finished_at=?,
                   error=CASE WHEN ?='' THEN error ELSE ? END
             WHERE id=?
            """,
            (status, done, errors, finished, error, error, run_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_tool_run(db_path: str, run_id: int) -> dict[str, Any] | None:
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM tool_runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        cols = [d[1] for d in conn.execute("PRAGMA table_info(tool_runs)").fetchall()]
        item = dict(zip(cols, row))
        item["params"] = _safe_json(item.get("params", "{}"))
        return item
    finally:
        conn.close()


def list_tool_runs(
    db_path: str, *, tool_id: str = "", limit: int = 20
) -> list[dict[str, Any]]:
    init(db_path)
    conn = _conn(db_path)
    try:
        where = "WHERE r.tool_id = ?" if tool_id else ""
        args: tuple[Any, ...] = (tool_id,) if tool_id else ()
        rows = conn.execute(
            f"""
            SELECT r.*, COALESCE(c.name, '') AS collection_name
              FROM tool_runs r
              LEFT JOIN asset_collections c ON c.id = r.output_collection_id
             {where}
             ORDER BY r.created_at DESC, r.id DESC LIMIT ?
            """,
            (*args, limit),
        ).fetchall()
        cols = [d[1] for d in conn.execute("PRAGMA table_info(tool_runs)").fetchall()]
        out = []
        for row in rows:
            item = dict(zip([*cols, "collection_name"], [*row, row[-1]]))
            item["params"] = _safe_json(item.get("params", "{}"))
            out.append(item)
        return out
    finally:
        conn.close()


def set_tool_source_category(
    db_path: str, source_key: str, category: str, *, status: str | None = None
) -> bool:
    """把工具结果按用户定义写回所选原视频/资产记录。"""
    clean = category.strip()
    if not clean or not source_key.startswith(("download:", "asset:")):
        return False
    source_type, raw_id = source_key.split(":", 1)
    file_norm = os.path.normpath(raw_id)
    init(db_path)
    conn = _conn(db_path)
    try:
        if source_type == "download":
            cur = conn.execute(
                """
                UPDATE downloads
                   SET tag_category = ?,
                       tag_status = COALESCE(?, tag_status)
                 WHERE file IN (?, ?)
                """,
                (clean, status, raw_id, file_norm),
            )
            conn.execute(
                """
                UPDATE assets
                   SET category = ?,
                       tag_category = ?,
                       tag_status = COALESCE(?, tag_status)
                 WHERE file IN (?, ?)
                """,
                (clean, clean, status, raw_id, file_norm),
            )
        else:
            if not raw_id.isdigit():
                return False
            cur = conn.execute(
                """
                UPDATE assets
                   SET category = ?,
                       tag_category = ?,
                       tag_status = COALESCE(?, tag_status)
                 WHERE id = ?
                """,
                (clean, clean, status, int(raw_id)),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def assign_tool_source_to_collection(
    db_path: str, source_key: str, collection_id: int
) -> bool:
    """把所选原视频/资产挂到用户定义的输出资产集。"""
    if not source_key.startswith(("download:", "asset:")) or not collection_id:
        return False
    source_type, raw_id = source_key.split(":", 1)
    file_norm = os.path.normpath(raw_id)
    init(db_path)
    conn = _conn(db_path)
    try:
        if source_type == "download":
            cur = conn.execute(
                "UPDATE assets SET collection_id = ? WHERE file IN (?, ?)",
                (collection_id, raw_id, file_norm),
            )
        else:
            if not raw_id.isdigit():
                return False
            cur = conn.execute(
                "UPDATE assets SET collection_id = ? WHERE id = ?",
                (collection_id, int(raw_id)),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def register_asset(
    db_path: str,
    *,
    file: str,
    source_file: str = "",
    size: int = 0,
    category: str = "",
    kind: str = "other",
    tool_id: str = "",
    tool_name: str = "",
    collection_id: int | None = None,
    collection_name: str = "",
    run_id: int | None = None,
    now: datetime | None = None,
) -> None:
    """供工具登记产物；tool_name 会作为资产画廊分组名。"""
    init(db_path)
    rel = os.path.normpath(file)
    normalized_tool_id = tool_id.strip() or kind
    normalized_tool_name = tool_name.strip() or "未命名工具"
    normalized_kind = "download" if normalized_tool_id == "download" else "tool"
    if collection_id is None:
        collection_id = ensure_asset_collection(
            db_path,
            collection_name or normalized_tool_name,
            tool_id=normalized_tool_id,
            tool_name=normalized_tool_name,
        )
    _upsert_asset(
        db_path,
        file=rel,
        basename=os.path.basename(rel),
        source_file=os.path.normpath(source_file) if source_file else "",
        rank=0,
        start=0.0,
        end=0.0,
        duration=0.0,
        size=size,
        category=category,
        kind=normalized_kind,
        tool_id=normalized_tool_id,
        tool_name=normalized_tool_name,
        collection_id=collection_id,
        run_id=run_id,
        now=now,
    )


def _asset_group_id(kind: str, collection_id: int, tool_id: str = "") -> str:
    if kind == "download":
        return "download"
    if collection_id:
        return f"collection:{collection_id}"
    return tool_id or "unknown"


def _asset_group_name(
    kind: str, collection_id: int, collection_name: str = "", tool_name: str = ""
) -> str:
    if kind == "download":
        return "下载原片"
    if collection_name:
        return collection_name
    if tool_id == "action" and not tool_name:
        return "连续动作筛选"
    return collection_name or tool_name or "未命名工具"


def _asset_url(kind: str, tool_id: str, asset_id: int, source_id: int, basename: str) -> str:
    if kind == "download" and source_id:
        return f"/media/{source_id}"
    if kind == "tool" and tool_id == "action" and basename:
        return f"/segment/{_urlquote(basename)}"
    return f"/asset-file/{asset_id}"


def delete_asset(db_path: str, aid: int) -> dict[str, Any] | None:
    init(db_path)
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT id, file, basename FROM assets WHERE id = ?", (aid,)).fetchone()
        if not row:
            return None
        conn.execute("DELETE FROM assets WHERE id = ?", (aid,))
        conn.commit()
        return {"id": row[0], "file": row[1], "basename": row[2]}
    finally:
        conn.close()


def _urlquote(s: str) -> str:
    from urllib.parse import quote

    return quote(s or "", safe="")
