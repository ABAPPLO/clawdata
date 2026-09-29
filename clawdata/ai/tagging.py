"""
视频打标签服务：复用 ComfyUI(10.168.1.106:8818) 中的 Qwen3-VL 工作流。

流程：把本地视频上传到 ComfyUI input 目录 -> 构建图
      LoadVideo -> VideoFrameSample -> GetVideoComponents -> TextGenerate(Qwen3-VL)
      -> 读取文本 -> 解析成 {category, tags, summary} -> 写入 SQLite。

并提供一个后台队列 TagWorker，由 app.py 启动，供网页面板异步打标签。
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
from datetime import datetime
from typing import Any

from clawdata.ai.comfy_client import ComfyClient, ComfyError
from clawdata.core.paths import DEFAULT_TAGGING_CONFIG_PATH, PROJECT_ROOT
from clawdata.storage import store


DEFAULT_CATEGORIES = [
    "美食", "宠物", "搞笑", "剧情/短剧", "知识科普", "音乐", "舞蹈",
    "游戏", "科技数码", "户外旅行", "动漫", "时尚美妆", "运动健身",
    "单人武术展示",
    "汽车", "亲子育儿", "生活Vlog", "新闻资讯", "影视", "教育", "直播", "其他",
]

DEFAULT_PROMPT = (
    "请观察这个短视频画面的多个关键帧，综合判断它属于哪一类视频，"
    "并给出 3~6 个中文标签和一句话摘要。"
    "类别只能从下面列表中选：{categories}。"
    "严格按如下 JSON 输出，不要输出多余文字："
    '{{"category":"<类别>","tags":["<标签1>","<标签2>",...],"summary":"<一句话摘要>"}}'
)


# ----------------------------------------------------------------- config
def default_config() -> dict[str, Any]:
    return {
        "server": "http://10.168.1.106:8818",
        "clip_name": "qwen3vl_8b_fp8_scaled.safetensors",
        "clip_type": "ideogram4",
        "device": "default",
        "num_frames": 6,
        "strategy": "uniform",
        "seed": 0,
        "max_length": 512,
        "temperature": 0.2,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.05,
        "repetition_penalty": 1.05,
        "presence_penalty": 0.0,
        "thinking": False,
        "use_default_template": True,
        "max_tags": 6,
        "categories": DEFAULT_CATEGORIES,
        "prompt": DEFAULT_PROMPT,
    }


def load_config(path: str = DEFAULT_TAGGING_CONFIG_PATH) -> dict[str, Any]:
    cfg = default_config()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update(data)
    return cfg


# ------------------------------------------------------------ workflow build
def build_workflow(
    video_filename: str, cfg: dict[str, Any], prompt: str | None = None
) -> dict[str, Any]:
    """按 ComfyUI API 图格式构造 Qwen3-VL 视频打标签工作流。

    传入自定义 `prompt` 时，直接用它作为提示词（不再做 {categories} 替换），
    供「连续动作筛选」等其它 Qwen3-VL 工具复用同一张图。
    """
    categories = cfg.get("categories", DEFAULT_CATEGORIES)
    prompt = prompt or cfg.get("prompt", DEFAULT_PROMPT)
    # 只有默认提示词里才有 {categories} 占位符；自定义提示词（如连续动作）
    # 不含该占位符，下面的替换是无副作用空操作。
    if "{categories}" in prompt:
        prompt = prompt.replace("{categories}", "、".join(categories))

    sampling = {
        "sampling_mode": "on",
        "sampling_mode.temperature": float(cfg.get("temperature", 0.2)),
        "sampling_mode.top_k": int(cfg.get("top_k", 64)),
        "sampling_mode.top_p": float(cfg.get("top_p", 0.95)),
        "sampling_mode.min_p": float(cfg.get("min_p", 0.05)),
        "sampling_mode.repetition_penalty": float(cfg.get("repetition_penalty", 1.05)),
        "sampling_mode.seed": int(cfg.get("seed", 0)),
        "sampling_mode.presence_penalty": float(cfg.get("presence_penalty", 0.0)),
    }
    text_gen_inputs = {
        "clip": ["1", 0],
        "prompt": prompt,
        "video": ["4", 0],
        "max_length": int(cfg.get("max_length", 512)),
        **sampling,
        "thinking": bool(cfg.get("thinking", False)),
        "use_default_template": bool(cfg.get("use_default_template", True)),
    }
    num_frames = int(cfg.get("num_frames", 6))
    return {
        "1": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": cfg.get("clip_name", "qwen3vl_8b_fp8_scaled.safetensors"),
                "type": cfg.get("clip_type", "ideogram4"),
                "device": cfg.get("device", "default"),
            },
        },
        "2": {"class_type": "LoadVideo", "inputs": {"file": video_filename}},
        "3": {
            "class_type": "VideoFrameSample",
            "inputs": {
                "video": ["2", 0],
                "num_frames": num_frames,
                "strategy": cfg.get("strategy", "uniform"),
                "seed": int(cfg.get("seed", 0)),
            },
        },
        "4": {"class_type": "GetVideoComponents", "inputs": {"video": ["3", 0]}},
        "5": {"class_type": "TextGenerate", "inputs": text_gen_inputs},
        "6": {"class_type": "PreviewAny", "inputs": {"source": ["5", 0]}},
    }


# ---------------------------------------------------------------- tag parsing
def _clamp_category(category: str, categories: list[str]) -> str:
    if not category:
        return "其他"
    low = category.strip()
    for c in categories:
        if c.lower() == low.lower():
            return c
    # 去后缀/含包含关系，比如 "搞笑视频" -> "搞笑"
    for c in categories:
        if c in low or low in c:
            return c
    return "其他"


def _clean_tags(tags: Any, max_tags: int) -> list[str]:
    if isinstance(tags, str):
        raw = re.split(r"[、,，|;；\s]+", tags)
    elif isinstance(tags, list):
        raw = tags
    else:
        raw = []
    out: list[str] = []
    seen: set[str] = set()
    for t in raw:
        t = str(t).strip().strip("'\"[]“”")
        if not t or len(t) > 30:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(out) >= max_tags:
            break
    return out


def parse_tag_text(text: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """从模型输出中稳健地解析出 category/tags/summary。"""
    categories = cfg.get("categories", DEFAULT_CATEGORIES)
    max_tags = int(cfg.get("max_tags", 6))
    txt = (text or "").strip()
    # 优先解析 JSON 对象
    match = re.search(r"\{.*\}", txt, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return {
                    "category": _clamp_category(str(obj.get("category", "")), categories),
                    "tags": _clean_tags(obj.get("tags", []), max_tags),
                    "summary": str(obj.get("summary", "")).strip(),
                }
        except (ValueError, TypeError):
            pass
    # 回退：猜测类别，其余当标签
    category = "其他"
    for c in categories:
        if c in txt:
            category = c
            break
    tags = _clean_tags(txt, max_tags)
    if not tags and txt:
        tags = [txt[:20]]
    return {"category": category, "tags": tags, "summary": txt[:200]}


# ---------------------------------------------------------- single tag run
def tag_video(video_abs: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """对单个视频文件执行打标签，返回解析后的结果。"""
    client = ComfyClient(cfg.get("server", default_config()["server"]))
    info = client.upload(video_abs, kind="image")
    filename = info.get("name") or os.path.basename(video_abs)
    graph = build_workflow(filename, cfg)
    pid = client.submit(graph)
    entry = client.wait(pid)
    text = client.extract_text(entry)
    result = parse_tag_text(text, cfg)
    result["raw"] = text
    return result


# -------------------------------------------------------------- background
class TagWorker:
    """后台打标签队列：按去重后的视频文件逐个处理。"""

    def __init__(self, db_path: str, cfg: dict[str, Any] | None = None) -> None:
        self.db_path = db_path
        self.cfg = cfg or load_config()
        self._q: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self._queued: set[str] = set()      # 已排队/正在处理的文件
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
                msg = str(e)[:500]
                if job.get("run_id"):
                    store.update_tool_run_progress(
                        self.db_path, int(job["run_id"]),
                        success=False, error=msg,
                    )
                if job.get("target_type") == "asset":
                    store.set_asset_tag(
                        self.db_path, int(job.get("id") or 0),
                        category="", tags=[], summary="", status="error", error=msg,
                    )
                else:
                    file = job.get("file", "")
                    store.set_tags_for_file(
                        self.db_path, file, category="", tags=[], summary="",
                        status="error",
                    )
                    for rec in store.list_for_tagging(self.db_path):
                        if rec.get("file") == file:
                            store.set_tag(
                                self.db_path, rec["id"], status="error", error=msg
                            )
            finally:
                with self._lock:
                    self._queued.discard(job.get("key", ""))
                    self._running = None
                self._q.task_done()

    def _process(self, job: dict[str, Any]) -> None:
        file = job.get("file", "")
        with self._lock:
            self._running = file
        run_id = int(job.get("run_id") or 0)
        if run_id:
            store.start_tool_run(self.db_path, run_id)
        video_abs = resolve_video_path(file, self.db_path)
        result = tag_video(video_abs, self.cfg)
        output_mode = job.get("output_mode") or "source"
        output_name = str(job.get("output_name") or "").strip()
        output_category = str(job.get("output_category") or "").strip()
        final_category = output_category or result["category"]
        if job.get("target_type") == "asset":
            store.set_asset_tag_status(self.db_path, int(job.get("id") or 0), "tagging")
            store.set_asset_tag(
                self.db_path, int(job.get("id") or 0),
                category=final_category, tags=result["tags"],
                summary=result["summary"], status="tagged",
            )
        else:
            for rec in store.list_for_tagging(self.db_path):
                if rec.get("file") == file:
                    store.set_tag_status(self.db_path, rec["id"], "tagging")
            store.set_tags_for_file(
                self.db_path,
                file,
                category=final_category,
                tags=result["tags"],
                summary=result["summary"],
                status="tagged",
            )
        if output_mode in ("collection", "both") and job.get("collection_id") and job.get("source_key"):
            store.assign_tool_source_to_collection(
                self.db_path, job["source_key"], int(job["collection_id"])
            )
        if run_id:
            store.update_tool_run_progress(self.db_path, run_id, success=True)
        self.done += 1

    def enqueue(self, file: str) -> bool:
        """把文件加入队列（去重）。返回是否真正入队。"""
        if not file:
            return False
        if f"download:{file}" in self._queued:
            return False
        return self.enqueue_download(file)

    def enqueue_download(self, file: str, run_id: int | None = None) -> bool:
        if not file:
            return False
        key = f"download:{file}"
        with self._lock:
            if file in self._queued:
                return False
            if key in self._queued:
                return False
            self._queued.add(key)
            self._running = self._running or file
        return self._enqueue_download_job(file, run_id)

    def _enqueue_download_job(
        self, file: str, run_id: int | None = None, *,
        output_mode: str = "source", output_name: str = "",
        output_category: str = "",
    ) -> bool:
        key = f"download:{file}"
        self._q.put({
            "key": key, "target_type": "download", "file": file, "run_id": run_id,
            "source_key": key, "output_mode": output_mode,
            "output_name": output_name, "output_category": output_category,
        })
        return True

    def enqueue_asset(self, asset_id: int, run_id: int | None = None) -> bool:
        asset = store.get_asset(self.db_path, asset_id)
        if not asset or not asset.get("file"):
            return False
        file = asset["file"]
        key = f"asset:{asset_id}"
        with self._lock:
            if key in self._queued:
                return False
            self._queued.add(key)
            self._running = self._running or file
        return self._enqueue_asset_job(asset_id, file, run_id)

    def _enqueue_asset_job(
        self, asset_id: int, file: str, run_id: int | None = None, *,
        output_mode: str = "source", output_name: str = "",
        output_category: str = "",
    ) -> bool:
        key = f"asset:{asset_id}"
        self._q.put({
            "key": key, "target_type": "asset", "id": asset_id, "file": file,
            "run_id": run_id,
            "source_key": key, "output_mode": output_mode,
            "output_name": output_name, "output_category": output_category,
        })
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            queued = list(self._queued)
            running = self._running
        return {
            "queued": queued,
            "running": running,
            "done": self.done,
            "errors": self.errors,
        }


# ------------------------------------------------------------ path helpers
def resolve_video_path(file: str, db_path: str) -> str:
    """把数据库里的 file（可能是相对路径）解析成绝对路径。"""
    if os.path.isabs(file):
        return os.path.normpath(file)
    # file 通常相对于项目根目录（如 downloads\\xxx.mp4）
    base = PROJECT_ROOT
    cand = os.path.abspath(os.path.join(base, file))
    if not os.path.isfile(cand):
        # 再尝试相对数据库目录，兼容日后再迁库
        alt = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(db_path)), file))
        if os.path.isfile(alt):
            return alt
    return cand


def build_worker(db_path: str, cfg: dict[str, Any] | None = None) -> TagWorker:
    return TagWorker(db_path, cfg)


_AUTO_WORKERS: dict[str, TagWorker] = {}
_AUTO_WORKER_LOCK = threading.Lock()


def get_or_start_worker(
    db_path: str, cfg: dict[str, Any] | None = None
) -> TagWorker:
    """给 CLI/下载引擎使用的自动打标队列；面板可继续共用主 worker。"""
    key = os.path.abspath(db_path)
    with _AUTO_WORKER_LOCK:
        worker = _AUTO_WORKERS.get(key)
        if not worker:
            worker = build_worker(db_path, cfg)
            worker.start()
            _AUTO_WORKERS[key] = worker
        return worker
