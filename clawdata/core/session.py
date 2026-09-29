"""
抖音登录会话（Cookie）模块。

抖音的搜索/视频详情接口需要**登录态**。本模块负责：
- 读取用户导出的 config/cookies.json（登录后的 Cookie）
- 组装 Cookie 请求头，供采集视频地址使用

如何导出 Cookie：
1. 用 Chrome/Edge 打开并登录 https://www.douyin.com
2. 按 F12 -> Network -> 刷新页面，点第一条 douyin.com 请求
3. 在 Request Headers 里复制整段 Cookie 字符串
4. 存入 config/cookies.json（或写成 "raw" 字符串），格式见 config/cookies.example.json
"""

from __future__ import annotations

import json
import os
from typing import Any

from clawdata.core.paths import DEFAULT_COOKIES_PATH

def load_cookies(path: str = DEFAULT_COOKIES_PATH) -> dict[str, str]:
    """读取 config/cookies.json，返回 {name: value}。"""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"未找到 {path}。请先按 README 导出抖音登录 Cookie 并存入该文件。"
        )
    with open(path, encoding="utf-8") as f:
        data: Any = json.load(f)

    if isinstance(data, list):
        # Firefox/Cookie-Editor 导出的列表格式
        return {str(item["name"]): str(item["value"]) for item in data if item.get("name")}
    if isinstance(data, dict) and "raw" in data:
        # 原始 Cookie 字符串
        raw = str(data["raw"])
        if not raw.strip():
            raise ValueError(f"{path} 里的 raw 为空。")
        return _parse_raw(raw)
    if isinstance(data, dict):
        return {str(k): str(v) for k, v in data.items() if v is not None}
    raise ValueError(f"{path} 格式无法识别，请使用 {{{{key: value}}}} 或 {{'raw': '...'}} 格式。")


def _parse_raw(raw: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def cookie_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def has_session(cookies: dict[str, str]) -> bool:
    """粗略判断是否像已登录会话（存在会话类 cookie）。"""
    keys = {k.lower() for k in cookies}
    return bool({"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "passport_csrf_token"} & keys)
