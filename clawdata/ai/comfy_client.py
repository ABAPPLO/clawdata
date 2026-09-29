"""
ComfyUI HTTP API 客户端（仅用标准库 urllib）。

用于把视频/图片上传到 ComfyUI 的 input 目录、提交图中的 prompt、
轮询执行结果并取出文本输出。与具体工作流解耦，节点之间的关系由 clawdata.ai.tagging 构建。

    from clawdata.ai.comfy_client import ComfyClient
    client = ComfyClient("http://10.168.1.106:8818")
    info = client.upload(video_path, kind="image")
    pid = client.submit(graph)
    entry = client.wait(pid)
    text = client.extract_text(entry)
"""

from __future__ import annotations

import json
import os
import time
import uuid
import urllib.parse
import urllib.request
from typing import Any


class ComfyError(RuntimeError):
    """ComfyUI 调用失败的统一异常。"""


class ComfyClient:
    def __init__(self, server: str, timeout: int = 300) -> None:
        self.server = server.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------ utils
    def _request(
        self,
        method: str,
        path: str,
        *,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
    ) -> bytes:
        url = self.server + path
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers or {}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            raise ComfyError(f"HTTP {e.code} {path}: {e.read().decode('utf-8', 'replace')[:300]}")
        except urllib.error.URLError as e:
            raise ComfyError(f"无法连接 ComfyUI {self.server}: {e.reason}")

    def _json(self, method: str, path: str, **kw) -> Any:
        return json.loads(self._request(method, path, **kw).decode("utf-8"))

    # -------------------------------------------------------------- upload file
    def upload(self, path: str, kind: str = "image") -> dict[str, Any]:
        """把本地文件上传到 ComfyUI input 目录，返回 {'name','subfolder','type'}。

        说明：ComfyUI 的 /upload/image 端点实际接收任意文件（含 mp4），
        并以 field name='image'、type=input 存入 input 目录，因此视频也用该端点。
        """
        if not os.path.isfile(path):
            raise ComfyError(f"文件不存在：{path}")
        boundary = "----ComfyBoundary" + uuid.uuid4().hex
        filename = os.path.basename(path)
        parts: list[str] = []
        for field, value in [("type", "input"), ("overwrite", "true")]:
            parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{field}"\r\n\r\n'
                f"{value}\r\n"
            )
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        )
        body = (
            "".join(parts).encode("utf-8")
            + open(path, "rb").read()
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        return self._json(
            "POST", "/upload/image", data=body, headers=headers, timeout=self.timeout
        )

    # ------------------------------------------------------------- submit prompt
    def submit(self, graph: dict[str, Any]) -> str:
        body = json.dumps(
            {"prompt": graph, "client_id": str(uuid.uuid4())}
        ).encode("utf-8")
        res = self._json(
            "POST",
            "/prompt",
            data=body,
            headers={"Content-Type": "application/json"},
            timeout=60,
        )
        pid = res.get("prompt_id")
        if not pid:
            # 部分版本返回 error 字段
            raise ComfyError(f"提交 prompt 失败：{res}")
        return pid

    # ------------------------------------------------------------------ wait
    def wait(
        self,
        prompt_id: str,
        timeout: float = 600.0,
        poll: float = 3.0,
    ) -> dict[str, Any]:
        """轮询 /history 直到该 prompt 完成或超时。返回该 prompt 的 history 条目。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                hist = self._json("GET", f"/history/{urllib.parse.quote(prompt_id)}")
            except ComfyError:
                hist = {}
            entry = hist.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("completed"):
                    return entry
                if status.get("status_str") == "error":
                    raise ComfyError(
                        self._error_message(entry) or "ComfyUI 工作流执行出错"
                    )
            time.sleep(poll)
        raise ComfyError(f"等待工作流结果超时（{int(timeout)}s）")

    @staticmethod
    def _error_message(entry: dict[str, Any]) -> str:
        for msg in entry.get("status", {}).get("messages", []):
            if msg[0] == "execution_error":
                return msg[1].get("exception_message", "") or msg[1].get("node_type", "")
        return ""

    # -------------------------------------------------------------- extract text
    @staticmethod
    def extract_text(entry: dict[str, Any]) -> str:
        """从 history 条目中取出第一个含 text 的节点输出。"""
        outputs = entry.get("outputs", {}) or {}
        for _node_id, value in outputs.items():
            if isinstance(value, dict):
                text = value.get("text")
                if text:
                    if isinstance(text, list):
                        return "".join(str(t) for t in text)
                    return str(text)
        return ""

    def ping(self) -> bool:
        try:
            self._json("GET", "/system_stats", timeout=5)
            return True
        except ComfyError:
            return False
