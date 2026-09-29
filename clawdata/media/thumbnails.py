from __future__ import annotations

import os
import threading
from pathlib import Path

import cv2


_lock = threading.Lock()


def first_frame_thumbnail(video_path: str, output_path: str, width: int = 480) -> bool:
    """Write a JPEG thumbnail from a video's first readable frame."""
    source = Path(video_path)
    output = Path(output_path)
    if not source.is_file():
        return False

    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_file() and output.stat().st_mtime >= source.stat().st_mtime:
        return True

    with _lock:
        cap = cv2.VideoCapture(str(source))
        try:
            if not cap.isOpened():
                return False
            okay, frame = cap.read()
            if not okay or frame is None:
                return False
            height, frame_width = frame.shape[:2]
            if frame_width > width:
                target_height = max(1, round(height * width / frame_width))
                frame = cv2.resize(frame, (width, target_height), interpolation=cv2.INTER_AREA)
            okay, encoded = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 84]
            )
            if not okay:
                return False
            temporary = output.with_suffix(".tmp.jpg")
            temporary.write_bytes(encoded.tobytes())
            os.replace(temporary, output)
            return True
        finally:
            cap.release()

