"""
「连续动作筛选」工具的后台队列：基于 pose_segment 的姿态分段引擎。

流程：从队列取视频文件 -> pose_segment.segment_video() 逐帧 YOLO 人姿检测、
相邻帧相似度切分、取最长 top 段 -> 按需导出 mp4 片段 -> 写入 SQLite 的
pose_segments 表。与「打标签」完全独立。
"""

from __future__ import annotations

import os
import queue
import threading
from typing import Any

from clawdata.vision import pose_segment
from clawdata.storage import store
from clawdata.core.paths import DEFAULT_TOOLS_CONFIG_PATH


def load_tool_config() -> dict[str, Any]:
    """连续动作工具配置（在默认值上叠加可选的 tools_config.json）。"""
    cfg = pose_segment.merge_pose_config()
    path = DEFAULT_TOOLS_CONFIG_PATH
    if os.path.isfile(path):
        import json

        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update(data)
    return pose_segment.merge_pose_config(cfg)


class PoseWorker:
    """后台姿态分段队列：按去重后的视频文件逐个处理。"""

    def __init__(self, db_path: str, cfg: dict[str, Any] | None = None) -> None:
        self.db_path = db_path
        self.cfg = cfg or load_tool_config()
        self._q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._running: str | None = None
        self.done = 0
        self.errors = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            try:
                self._process(job)
            except Exception as e:  # noqa: BLE001
                self.errors += 1
                store.set_pose_status(
                    self.db_path, job.get("file", ""), "error", str(e)[:800]
                )
                if job.get("run_id"):
                    store.update_tool_run_progress(
                        self.db_path, int(job["run_id"]),
                        success=False, error=str(e)[:500],
                    )
            finally:
                with self._lock:
                    self._queued.discard(job.get("file", ""))
                    self._running = None
                self._q.task_done()

    def _process(self, job: dict[str, Any]) -> None:
        file = job["file"]
        with self._lock:
            self._running = file
        run_id = int(job.get("run_id") or 0)
        if run_id:
            store.start_tool_run(self.db_path, run_id)
        output_mode = job.get("output_mode") or "collection"
        output_name = str(job.get("output_name") or "").strip()
        output_category = str(job.get("output_category") or "").strip()
        store.set_pose_status(self.db_path, file, "pending")
        video_abs = resolve_video_path(file, self.db_path)
        result = pose_segment.segment_video(video_abs, self.cfg)

        clips: list[dict[str, Any]] = []
        if output_mode in ("collection", "both") and self.cfg.get("extract_clips", True) and result.get("segments"):
            clips = pose_segment.extract_clips(
                video_abs, result.get("top") or [],
                top_k=3,
            )
        top = result.get("top", [])
        # 把片段路径附加到对应 top 段上
        for t in top:
            for c in clips:
                if c["rank"] == t.get("rank"):
                    t["clip"] = c["path"]
                    break

        store.replace_pose_frames(self.db_path, file, result.get("frames", []))
        store.set_pose_analysis(
            self.db_path, file,
            segments=result.get("segments", []),
            top=top,
            longest=result.get("longest", 0.0),
            total_dur=result.get("duration", 0.0),
            fps=result.get("fps", 0.0),
            num_frames=result.get("num_frames", 0),
            frames_analyzed=result.get("frames_analyzed", 0),
            avg_conf=result.get("avg_conf", 0.0),
            status="analyzed", error="",
        )
        # 把本次产物登记到用户选择的输出资产集
        try:
            collection_id = job.get("collection_id")
            for clip in clips:
                clip_rel = os.path.normpath(os.path.relpath(clip["path"], pose_segment.PROJECT_ROOT))
                store.register_asset(
                    self.db_path,
                    file=clip_rel,
                    source_file=file,
                    size=int(os.path.getsize(clip["path"])),
                    tool_id="action",
                    tool_name="连续动作筛选",
                    collection_id=collection_id,
                    run_id=run_id or None,
                )
            store.sync_assets(self.db_path)
        except Exception:  # noqa: BLE001
            pass
        if output_mode in ("source", "both") and job.get("source_key"):
            try:
                store.set_tool_source_category(
                    self.db_path, job["source_key"],
                    output_category or output_name, status="analyzed",
                )
            except Exception:
                pass
        if run_id:
            store.update_tool_run_progress(self.db_path, run_id, success=True)
        self.done += 1

    def enqueue(
        self, item: dict[str, Any], collection_id: int | None = None,
        run_id: int | None = None, *, output_mode: str = "collection",
        output_name: str = "", output_category: str = "",
    ) -> bool:
        file = item.get("file") if isinstance(item, dict) else item
        if not file:
            return False
        with self._lock:
            if file in self._queued:
                return False
            self._queued.add(file)
            self._running = self._running or file
        self._q.put({
            "file": file, "collection_id": collection_id, "run_id": run_id,
            "source_key": item.get("key") if isinstance(item, dict) else "",
            "output_mode": output_mode, "output_name": output_name,
            "output_category": output_category,
        })
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            queued = list(self._queued)
            running = self._running
        return {
            "queued": queued, "running": running,
            "done": self.done, "errors": self.errors,
        }


def build_pose_worker(db_path: str, cfg: dict[str, Any] | None = None) -> PoseWorker:
    return PoseWorker(db_path, cfg)


def resolve_video_path(file: str, db_path: str) -> str:
    """把数据库里的 file（可能是相对路径）解析成绝对路径。"""
    if os.path.isabs(file):
        return os.path.normpath(file)
    base = pose_segment.PROJECT_ROOT
    cand = os.path.abspath(os.path.join(base, file))
    if os.path.isfile(cand):
        return cand
    alt = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(db_path)), file))
    return cand if os.path.isfile(cand) else alt
