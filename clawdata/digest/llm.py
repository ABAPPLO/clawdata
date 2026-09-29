"""
ComfyUI(Qwen3-VL) 纯文本生成通道，供「视频文库」的总结与问询复用。

与打标服务（clawdata.ai.tagging）共用同一台 ComfyUI，但不需要上传真实视频：
- 首选「纯文本图」：CLIPLoader -> TextGenerate -> PreviewAny（无 video 输入）。
- 若该自定义节点强制要求 video 输入，自动回退「占位视频图」：用 OpenCV 生成
  一张 512x512 纯色 2 帧小 mp4，走与打标完全同构的 LoadVideo 链路，零节点类型风险。
模式探测结果在进程内缓存。

用法：
    from clawdata.digest import llm
    text = llm.generate_text("用一句话介绍你自己")

自检：
    python -m clawdata.digest.llm "回复：通道正常"
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from clawdata.ai.comfy_client import ComfyClient, ComfyError
from clawdata.core.paths import DEFAULT_DIGEST_CONFIG_PATH, DEFAULT_DIGEST_DIR


# ----------------------------------------------------------------- config
def default_config() -> dict[str, Any]:
    return {
        # ComfyUI 服务器与模型参数默认与打标服务保持一致
        "server": "http://10.168.1.112:8188",
        "clip_name": "qwen3vl_8b_fp8_scaled.safetensors",
        "clip_type": "ideogram4",
        "device": "default",
        "max_length": 1024,
        "temperature": 0.3,
        "top_k": 64,
        "top_p": 0.95,
        "min_p": 0.05,
        "repetition_penalty": 1.05,
        "seed": 0,
        "thinking": False,
        "use_default_template": True,
        # 整理行为
        "per_subscription_limit": 5,
        "subtitle_max_chars": 12000,
        "desc_max_chars": 4000,
        "focus_points": [
            "文档/资料下载链接（网盘、文档站等）",
            "代码仓库与代码片段说明",
            "视频中提到的工具清单",
            "教程或操作步骤要点",
            "结论与建议",
        ],
        "link_rules": [],  # 追加的自定义规则 [{"type": "xxx", "pattern": "xxx.com"}]
        "summary_prompt": (
            "你是资料整理助手。下面是一个视频的标题、简介和字幕（可能缺失部分），"
            "请整理成结构化知识文档。用户特别关注：{focus}。\n"
            "严格按如下 JSON 输出，不要输出多余文字：\n"
            '{{"summary":"<100~200字的中文总结>",'
            '"key_points":["<要点1>","<要点2>",...],'
            '"resources":[{{"type":"<类型>","value":"<链接或名称>","note":"<说明>"}}]}}\n\n'
            "【标题】{title}\n【博主】{author}\n【发布时间】{pubdate}\n"
            "【简介】\n{desc}\n\n【字幕】\n{subtitle}\n\n【已提取的链接】\n{links}"
        ),
        "ask_prompt": (
            "你是资料问答助手。请只依据下面的文库资料回答问题；资料中没有的信息"
            "要明确说明没有找到，不要编造。回答用中文，条理清晰，"
            "并在末尾列出用到的资料编号。\n\n【问题】{question}\n\n【文库资料】\n{context}"
        ),
    }


def load_config(path: str = DEFAULT_DIGEST_CONFIG_PATH) -> dict[str, Any]:
    cfg = default_config()
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cfg.update(data)
    return cfg


def save_config(cfg: dict[str, Any], path: str = DEFAULT_DIGEST_CONFIG_PATH) -> None:
    """把面板上编辑的整理规则写回配置文件（保留未知键）。"""
    merged = load_config(path)
    merged.update(cfg)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------ workflow build
def _sampling(cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "sampling_mode": "on",
        "sampling_mode.temperature": float(cfg.get("temperature", 0.3)),
        "sampling_mode.top_k": int(cfg.get("top_k", 64)),
        "sampling_mode.top_p": float(cfg.get("top_p", 0.95)),
        "sampling_mode.min_p": float(cfg.get("min_p", 0.05)),
        "sampling_mode.repetition_penalty": float(cfg.get("repetition_penalty", 1.05)),
        "sampling_mode.seed": int(cfg.get("seed", 0)),
        "sampling_mode.presence_penalty": 0.0,
    }


def build_text_workflow(prompt: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """纯文本图：不接 video 输入，直接让 Qwen3-VL 生成文本。"""
    return {
        "1": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": cfg.get("clip_name", "qwen3vl_8b_fp8_scaled.safetensors"),
                "type": cfg.get("clip_type", "ideogram4"),
                "device": cfg.get("device", "default"),
            },
        },
        "2": {
            "class_type": "TextGenerate",
            "inputs": {
                "clip": ["1", 0],
                "prompt": prompt,
                "max_length": int(cfg.get("max_length", 1024)),
                **_sampling(cfg),
                "thinking": bool(cfg.get("thinking", False)),
                "use_default_template": bool(cfg.get("use_default_template", True)),
            },
        },
        "3": {"class_type": "PreviewAny", "inputs": {"source": ["2", 0]}},
    }


def build_blank_workflow(video_filename: str, prompt: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """占位视频图：与打标工作流完全同构，仅采样 1 帧。"""
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
            "inputs": {"video": ["2", 0], "num_frames": 1, "strategy": "uniform",
                       "seed": int(cfg.get("seed", 0))},
        },
        "4": {"class_type": "GetVideoComponents", "inputs": {"video": ["3", 0]}},
        "5": {
            "class_type": "TextGenerate",
            "inputs": {
                "clip": ["1", 0],
                "prompt": prompt,
                "video": ["4", 0],
                "max_length": int(cfg.get("max_length", 1024)),
                **_sampling(cfg),
                "thinking": bool(cfg.get("thinking", False)),
                "use_default_template": bool(cfg.get("use_default_template", True)),
            },
        },
        "6": {"class_type": "PreviewAny", "inputs": {"source": ["5", 0]}},
    }


# ------------------------------------------------------------ blank video
_BLANK_LOCK = threading.Lock()
_BLANK_CANDIDATES = ("blank_512.mp4", ".blank.mp4")


def ensure_blank_video() -> str:
    """生成（或复用）一张 512x512 纯色 2 帧的占位 mp4，返回本地路径。"""
    os.makedirs(DEFAULT_DIGEST_DIR, exist_ok=True)
    for name in _BLANK_CANDIDATES:
        path = os.path.join(DEFAULT_DIGEST_DIR, name)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
    path = os.path.join(DEFAULT_DIGEST_DIR, _BLANK_CANDIDATES[0])
    with _BLANK_LOCK:
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            import cv2
            import numpy as np

            writer = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), 2, (512, 512))
            frame = np.full((512, 512, 3), 240, dtype=np.uint8)
            for _ in range(2):
                writer.write(frame)
            writer.release()
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise ComfyError("占位视频生成失败，请检查 opencv-python 是否可用")
    return path


# --------------------------------------------------------------- generation
_mode_cache: dict[str, str] = {"mode": "text"}  # "text" | "blank"
_mode_lock = threading.Lock()


def _set_mode(mode: str) -> None:
    with _mode_lock:
        _mode_cache["mode"] = mode


def _get_mode() -> str:
    with _mode_lock:
        return _mode_cache["mode"]


def generate_text(prompt: str, cfg: dict[str, Any] | None = None, timeout: float = 600.0) -> str:
    """让 ComfyUI 上的 Qwen3-VL 生成一段文本。

    先用进程缓存的模式提交；首次运行先试纯文本图，若该节点要求 video 输入
    （提交或执行报错）则自动切到占位视频模式重试一次。
    """
    cfg = cfg or load_config()
    client = ComfyClient(cfg.get("server", default_config()["server"]), timeout=300)

    tried: set[str] = set()
    mode = _get_mode()
    last_error: Exception | None = None
    while mode not in tried:
        tried.add(mode)
        try:
            if mode == "text":
                graph = build_text_workflow(prompt, cfg)
                pid = client.submit(graph)
            else:
                blank = ensure_blank_video()
                info = client.upload(blank, kind="image")
                filename = info.get("name") or os.path.basename(blank)
                graph = build_blank_workflow(filename, prompt, cfg)
                pid = client.submit(graph)
            entry = client.wait(pid, timeout=timeout)
            text = client.extract_text(entry)
            _set_mode(mode)
            return text.strip()
        except ComfyError as exc:
            last_error = exc
            mode = "blank" if mode == "text" else "text"
    raise ComfyError(f"ComfyUI 文本生成失败（text/blank 两种模式均不可用）：{last_error}")


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "请回复：通道正常"
    print(f"server: {load_config().get('server')}")
    print(f"mode: {_get_mode()}")
    print(generate_text(q))
