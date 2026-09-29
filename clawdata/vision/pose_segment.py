"""
连续动作筛选的核心引擎：YOLO 人体姿态检测 + 相邻帧姿态相似度切分。

算法（按需求）：
  1. 用 YOLO(ONNX) 的人体姿态检测，逐帧提取人物的 17 个关键点。
  2. 对相邻两帧的姿态做相似度比较（归一化关键点的余弦相似度）。
  3. 相似度骤降（"差距大"）之处作为切分点，把视频切成若干段。
  4. 按段时长排序，取最长的前三个视频片段，并可导出成 mp4。

说明：姿态相似度衡量的是"动作是否连贯"。相似度高 = 人物动作在连续流动；
相似度骤降 = 镜头切换/大幅姿态跳变，视为间断。
同时记录每段的平均姿态变化量（motion）与连贯度，便于后续按"连续且有动作"筛选。
"""

from __future__ import annotations

import json
import os
from typing import Any

import cv2
import numpy as np
import onnxruntime as ort

from clawdata.core.paths import MODEL_PATH, PROJECT_ROOT, SEGMENTS_DIR


COCO_KEYPOINTS = [
    "nose", "l_eye", "r_eye", "l_ear", "r_ear",
    "l_shoulder", "r_shoulder", "l_elbow", "r_elbow",
    "l_wrist", "r_wrist", "l_hip", "r_hip",
    "l_knee", "r_knee", "l_ankle", "r_ankle",
]


POSESEG_DEFAULTS = {
    "model_path": MODEL_PATH,
    "img_size": 320,
    "conf_thres": 0.25,
    "iou_thres": 0.45,
    "frame_stride": 1,          # 每 1 帧分析一次（= 逐帧）
    "cut_threshold": 0.55,      # 相似度低于此值视为"差距大"，切分
    "smooth_window": 3,         # 相似度平滑窗口（奇数）
    "min_seg_sec": 0.8,         # 小于该时长的段并入相邻段
    "min_motion": 0.12,         # 段的平均姿态变化量低于此值视为"无动作"，不计入 top/最长
    "max_segments": 20,         # 最多保留的段数
    "top_k": 3,                 # 取最长的几段
    "extract_clips": True,      # 是否把 top 段导出成 mp4
}


def merge_pose_config(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    out = dict(POSESEG_DEFAULTS)
    if cfg:
        out.update({k: v for k, v in cfg.items() if v is not None})
    out["smooth_window"] = max(1, int(out["smooth_window"]) | 1)  # 强制奇数
    out["top_k"] = 3  # 本工具按需求固定保留 Top3 最长动作段
    return out


# ------------------------------------------------------------------ inference
def _load_model(cfg: dict[str, Any]) -> ort.InferenceSession:
    path = cfg["model_path"]
    if not os.path.isfile(path):
        raise FileNotFoundError(f"找不到姿态检测模型：{path}")
    # 若显式指定了 provider，则优先使用；否则用 CPU（兼顾兼容性）
    providers = cfg.get("providers")
    if not providers:
        available = ort.get_available_providers()
        providers = ["CPUExecutionProvider"] if "CPUExecutionProvider" in available else available
    return ort.InferenceSession(path, providers=providers)


def _letterbox(img: np.ndarray, size: int):
    h, w = img.shape[:2]
    s = size / max(h, w)
    nw, nh = max(1, int(round(w * s))), max(1, int(round(h * s)))
    resized = cv2.resize(img, (nw, nh))
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    ox, oy = (size - nw) // 2, (size - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized
    return canvas, s, ox, oy


def _nms(boxes: list, scores: list, iou_thres: float) -> list[int]:
    """简易 NMS，避免依赖 cv2.dnn 在不同 OpenCV 版本的 API 差异。"""
    if len(boxes) == 0:
        return []
    boxes = np.asarray(boxes, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep: list[int] = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / np.maximum(areas[i] + areas[order[1:]] - inter, 1e-6)
        order = order[1:][iou <= iou_thres]
    return keep


def _postprocess(outputs, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """把 (1,56,2100) 解析为人形检测列表。"""
    pred = np.squeeze(outputs)            # (56, 2100)
    pred = np.asarray(pred, dtype=np.float32).T   # (2100, 56)
    boxes = pred[:, :4]                    # cx, cy, w, h
    obj = pred[:, 4]
    kpts = pred[:, 5:56].reshape(len(pred), 17, 3)
    mask = obj > cfg["conf_thres"]
    if not mask.any():
        return []
    boxes, obj, kpts = boxes[mask], obj[mask], kpts[mask]
    x1 = boxes[:, 0] - boxes[:, 2] / 2
    y1 = boxes[:, 1] - boxes[:, 3] / 2
    x2 = boxes[:, 0] + boxes[:, 2] / 2
    y2 = boxes[:, 1] + boxes[:, 3] / 2
    keep = _nms(
        list(zip(x1.tolist(), y1.tolist(), x2.tolist(), y2.tolist())),
        obj.tolist(),
        cfg["iou_thres"],
    )
    out = []
    for i in keep:
        bw, bh = x2[i] - x1[i], y2[i] - y1[i]
        out.append({
            "bbox": (float(x1[i]), float(y1[i]), float(x2[i]), float(y2[i])),
            "conf": float(obj[i]),
            "area": max(1.0, float(bw * bh)),
            "kpts": kpts[i].astype(np.float32),   # (17,3)
        })
    return out


def _centroid(kpts: np.ndarray) -> np.ndarray:
    conf = kpts[:, 2]
    m = conf > 0.2
    xy = kpts[:, :2]
    return xy[m].mean(0) if m.sum() >= 3 else xy.reshape(-1, 2).mean(0)


def _choose_primary(
    dets: list[dict[str, Any]],
    prev_center: np.ndarray | None,
    img_size: float,
):
    if not dets:
        return None, None
    if prev_center is not None:
        best, bestd = None, None
        for d in dets:
            c = _centroid(d["kpts"])
            dd = float(np.linalg.norm(c - prev_center))
            if bestd is None or dd < bestd:
                best, bestd = d, dd
        if bestd is not None and bestd <= 0.35 * img_size:  # 接近上一帧主体
            return best, _centroid(best["kpts"])
    d = max(dets, key=lambda x: x["area"])
    return d, _centroid(d["kpts"])


def _descriptor(kpts: np.ndarray) -> np.ndarray:
    """归一化的 34 维姿态描述子（平移/尺度不变）。"""
    xy = kpts[:, :2].astype(np.float32)
    conf = kpts[:, 2]
    m = conf > 0.2
    center = xy[m].mean(0) if m.sum() >= 3 else xy.reshape(-1, 2).mean(0)
    dist = np.linalg.norm(xy - center, axis=1)
    scale = max(1e-6, float(dist.mean()))
    vec = (xy - center) / scale
    if m.sum() >= 3:
        vec = vec * conf[:, None]
    return vec.reshape(-1)


def _similarity(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-6 or nb < 1e-6:
        return 0.0
    return float(max(0.0, np.dot(a, b) / (na * nb)))


def _motion(a: np.ndarray | None, b: np.ndarray | None) -> float:
    if a is None or b is None:
        return 0.0
    return float(np.linalg.norm(a - b))


def descriptor_from_image(image_bytes: bytes, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """从一张起始姿态图片提取 34 维姿态描述子。"""
    cfg = merge_pose_config(cfg)
    data = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("无法解析图片，请使用 jpg/png/webp")
    canvas, _, _, _ = _letterbox(image, cfg["img_size"])
    blob = cv2.dnn.blobFromImage(
        canvas, 1 / 255.0, (cfg["img_size"], cfg["img_size"]),
        (0, 0, 0), swapRB=True,
    ).astype(np.float32)
    session = _load_model(cfg)
    outputs = session.run(None, {session.get_inputs()[0].name: blob})
    detections = _postprocess(outputs, cfg)
    if not detections:
        return None
    primary = max(detections, key=lambda item: item["area"])
    return {
        "descriptor": _descriptor(primary["kpts"]).tolist(),
        "confidence": round(float(primary["conf"]), 4),
        "bbox": [round(float(v), 2) for v in primary["bbox"]],
    }


def descriptor_from_video_time(
    video_abs: str, time_sec: float, cfg: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """从视频指定时间截帧并提取姿态描述子，供库内选帧输入使用。"""
    cfg = merge_pose_config(cfg)
    if not os.path.isfile(video_abs):
        raise FileNotFoundError(f"视频不存在：{video_abs}")
    cap = cv2.VideoCapture(video_abs)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_abs}")
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, float(time_sec)) * 1000.0)
        ok, frame = cap.read()
        if not ok or frame is None:
            return None
        canvas, _, _, _ = _letterbox(frame, cfg["img_size"])
        blob = cv2.dnn.blobFromImage(
            canvas, 1 / 255.0, (cfg["img_size"], cfg["img_size"]),
            (0, 0, 0), swapRB=True,
        ).astype(np.float32)
        session = _load_model(cfg)
        outputs = session.run(None, {session.get_inputs()[0].name: blob})
        detections = _postprocess(outputs, cfg)
        if not detections:
            return None
        primary = max(detections, key=lambda item: item["area"])
        preview = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])[1].tobytes()
        import base64
        return {
            "descriptor": _descriptor(primary["kpts"]).tolist(),
            "confidence": round(float(primary["conf"]), 4),
            "bbox": [round(float(v), 2) for v in primary["bbox"]],
            "preview": "data:image/jpeg;base64," + base64.b64encode(preview).decode("ascii"),
        }
    finally:
        cap.release()


# ----------------------------------------------------------------- segmentation
def _smooth(sig: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(sig) < window:
        return sig
    kernel = np.ones(window) / window
    pad = window // 2
    padded = np.pad(sig, (pad, pad), mode="edge")
    return np.convolve(padded, kernel, mode="valid")[: len(sig)]


def _build_segments(
    sims: np.ndarray,
    deltas: np.ndarray,
    has_person: np.ndarray,
    fps: float,
    cfg: dict[str, Any],
    phys: np.ndarray,
) -> list[dict[str, Any]]:
    """根据相邻帧相似度序列切分视频，返回按时间排序的段列表。"""
    n = len(sims) + 1            # 分析帧数
    sims_s = _smooth(sims, cfg["smooth_window"])
    cuts = np.zeros(n, dtype=bool)
    for i in range(1, len(sims_s)):
        if sims_s[i] < cfg["cut_threshold"]:
            cuts[i] = True       # 第 i 帧（索引）之前切
        if i < len(has_person) and not has_person[i]:
            cuts[i] = True       # 该帧没检测到人，强制间断

    segs: list[dict[str, Any]] = []
    start = 0
    for i in range(1, n):
        if cuts[i]:
            if i - start >= 1:
                segs.append((start, i))
            start = i
    if n - start >= 1:
        segs.append((start, n))

    def seg_span(a: int, b: int) -> tuple[float, float]:
        """按物理帧号换算段的时间区间（秒）。"""
        pa, pb = int(phys[a]), int(phys[b - 1])
        return pa / fps, (pb + 1) / fps

    # 合并过短段
    merged: list[tuple[int, int]] = []
    for seg in segs:
        if merged and _seg_dur(seg, fps, phys) < cfg["min_seg_sec"]:
            merged[-1] = (merged[-1][0], seg[1])   # 短段并入前一段
        else:
            merged.append(seg)

    result = []
    for idx, (a, b) in enumerate(merged):
        dur = _seg_dur((a, b), fps, phys)
        if dur < cfg["min_seg_sec"]:
            continue
        start, end = seg_span(a, b)
        sub_sim = sims_s[a:max(a + 1, b - 1)]
        sub_del = deltas[a:max(a + 1, b - 1)]
        result.append({
            "idx": idx,
            "_a": a,
            "_b": b,
            "start": round(start, 3),
            "end": round(end, 3),
            "duration": round(dur, 3),
            "mean_similarity": round(float(np.mean(sub_sim)) if sub_sim.size else 0.0, 4),
            "mean_delta": round(float(np.mean(sub_del)) if sub_del.size else 0.0, 4),
            "active": bool(np.mean(sub_del) >= cfg["min_motion"]) if sub_del.size else False,
        })
    result.sort(key=lambda s: s["start"])
    return result[: cfg["max_segments"]]


def _seg_dur(seg: tuple[int, int], fps: float, phys: np.ndarray) -> float:
    a, b = seg
    return (int(phys[b - 1]) + 1 - int(phys[a])) / fps


# -------------------------------------------------------------------- main
def segment_video(video_abs: str, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """对单个视频做姿态化连续动作分段，返回段列表、top 段与统计。"""
    cfg = merge_pose_config(cfg)
    if not os.path.isfile(video_abs):
        raise FileNotFoundError(f"视频不存在：{video_abs}")
    sess = _load_model(cfg)

    cap = cv2.VideoCapture(video_abs)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频：{video_abs}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    stride = max(1, int(cfg["frame_stride"]))

    descs: list[np.ndarray | None] = []
    has_person: list[bool] = []
    phys: list[int] = []
    prev_center: np.ndarray | None = None
    frame_idx = 0
    analyzed = 0
    conf_sum = 0.0
    conf_n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % stride != 0:
            frame_idx += 1
            continue
        canvas, s, ox, oy = _letterbox(frame, cfg["img_size"])
        blob = cv2.dnn.blobFromImage(
            canvas, 1 / 255.0, (cfg["img_size"], cfg["img_size"]),
            (0, 0, 0), swapRB=True,
        ).astype(np.float32)
        outputs = sess.run(None, {sess.get_inputs()[0].name: blob})
        dets = _postprocess(outputs, cfg)
        primary, prev_center = _choose_primary(dets, prev_center, float(cfg["img_size"]))
        if primary is not None:
            descs.append(_descriptor(primary["kpts"]))
            has_person.append(True)
            conf_sum += primary["conf"]
            conf_n += 1
        else:
            descs.append(None)
            has_person.append(False)
        phys.append(frame_idx)
        analyzed += 1
        frame_idx += 1
    cap.release()

    if analyzed < 2:
        frames = _build_frame_records(
            descs, has_person, phys, fps, [], cfg
        )
        return {
            "segments": [], "top": [], "longest": 0.0,
            "duration": 0.0, "fps": fps, "num_frames": total,
            "frames_analyzed": analyzed, "avg_conf": 0.0, "frames": frames,
        }

    sims = np.array([_similarity(descs[i], descs[i + 1]) for i in range(len(descs) - 1)], dtype=np.float32)
    deltas = np.array([_motion(descs[i], descs[i + 1]) for i in range(len(descs) - 1)], dtype=np.float32)
    has_arr = np.array(has_person[: len(descs)], dtype=bool)
    all_segs = _build_segments(sims, deltas, has_arr, fps, cfg, np.asarray(phys, dtype=np.int64))
    segs = all_segs[: cfg["max_segments"]]
    frames = _build_frame_records(descs, has_person, phys, fps, all_segs, cfg)
    for seg in all_segs:
        seg.pop("_a", None)
        seg.pop("_b", None)

    active = [s for s in segs if s.get("active")]
    top = sorted(active, key=lambda s: s["duration"], reverse=True)[: cfg["top_k"]]
    top = [dict(t, rank=i + 1) for i, t in enumerate(top)]
    return {
        "segments": segs,
        "top": top,
        "longest": top[0]["duration"] if top else 0.0,
        "duration": round(total / fps, 3) if fps else 0.0,
        "fps": round(fps, 2),
        "num_frames": total,
        "frames_analyzed": analyzed,
        "avg_conf": round(conf_sum / conf_n, 3) if conf_n else 0.0,
        "active_count": len(active),
        "frames": frames,
    }


def _build_frame_records(
    descs: list[np.ndarray | None],
    has_person: list[bool],
    phys: list[int],
    fps: float,
    segments: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    """把内存中的姿态描述子转成可入库的帧记录。"""
    spans = {
        local_idx: (int(seg.get("idx", -1)), bool(seg.get("active")))
        for seg in segments
        for local_idx in range(int(seg["_a"]), int(seg["_b"]))
        if "_a" in seg and "_b" in seg
    }
    start_indices = {int(seg["_a"]) for seg in segments if "_a" in seg}
    records: list[dict[str, Any]] = []
    for local_idx, descriptor in enumerate(descs):
        physical_idx = int(phys[local_idx])
        segment_idx, active = spans.get(local_idx, (-1, False))
        records.append({
            "frame_idx": physical_idx,
            "time_sec": round(physical_idx / fps, 4) if fps else 0.0,
            "has_person": bool(has_person[local_idx]),
            "descriptor": descriptor.tolist() if descriptor is not None else None,
            "segment_idx": segment_idx,
            "segment_start": local_idx in start_indices,
            "active": active,
        })
    return records


# ------------------------------------------------------------------ clip
def extract_clips(
    video_abs: str,
    segments: list[dict[str, Any]],
    *,
    top_k: int = 3,
    out_dir: str | None = None,
) -> list[dict[str, Any]]:
    """把 top 段导出为 mp4，返回 [{rank,start,end,duration,path}]。"""
    import os
    from pathlib import Path
    out_dir = out_dir or SEGMENTS_DIR
    os.makedirs(out_dir, exist_ok=True)
    stem = Path(video_abs).stem
    clips = []
    top = sorted(segments, key=lambda s: s["duration"], reverse=True)[:top_k]
    for rank, seg in enumerate(top, 1):
        start, end = seg["start"], seg["end"]
        out = os.path.join(out_dir, f"{stem}_seg{rank}_{int(start)}-{int(end)}.mp4")
        if not _write_clip(video_abs, start, end, out):
            continue
        clips.append({
            "rank": rank,
            "start": seg["start"],
            "end": seg["end"],
            "duration": seg["duration"],
            "path": os.path.normpath(out),
        })
    return clips


def _write_clip(video_abs: str, start: float, end: float, out: str) -> bool:
    """优先用 ffmpeg 转成浏览器可播的 H.264；失败则回退 OpenCV。"""
    exe = _ffmpeg_exe()
    if exe:
        if _write_clip_ffmpeg(exe, video_abs, start, end, out):
            return True
    return _write_clip_cv2(video_abs, start, end, out)


def _ffmpeg_exe() -> str | None:
    import shutil
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg") or None


def _write_clip_ffmpeg(exe: str, video_abs: str, start: float, end: float, out: str) -> bool:
    import subprocess
    if os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass
    cmd = [
        exe, "-y", "-nostdin",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", video_abs,
        "-map", "0:v:0", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", out,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception:
        return False
    if r.returncode == 0 and os.path.isfile(out) and os.path.getsize(out) > 0:
        return True
    return False


def _write_clip_cv2(video_abs: str, start: float, end: float, out: str) -> bool:
    cap = cv2.VideoCapture(video_abs)
    if not cap.isOpened():
        return False
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    writer = None
    for codec in ("mp4v", "avc1", "MJPG"):
        writer = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*codec), fps, (w, h))
        if writer.isOpened():
            break
        writer.release()
        writer = None
    if writer is None:
        cap.release()
        return False
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    n = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
        if t >= end:
            break
        writer.write(frame)
        n += 1
    writer.release()
    cap.release()
    return n > 0 and os.path.isfile(out) and os.path.getsize(out) > 0
