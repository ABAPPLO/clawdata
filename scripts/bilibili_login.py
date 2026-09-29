"""
B站登录 Cookie 采集：打开 Edge 到 B 站，等你登录，自动保存 Cookie。

用法：
    python scripts/bilibili_login.py

流程：
  1. 弹出 Edge 窗口打开 https://www.bilibili.com
  2. 你在窗口里登录（扫码或账号密码均可）
  3. 检测到 SESSDATA 后自动写入 config/cookies_bilibili.json 并关闭窗口
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from clawdata.core.paths import BILIBILI_COOKIES_PATH  # noqa: E402

EDGE_PATH = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
PROFILE_DIR = str(PROJECT_ROOT / "edge-bilibili-profile")
LOGIN_URL = "https://www.bilibili.com/"
WAIT_SECONDS = 300  # 最多等 5 分钟
POLL_INTERVAL = 3


def main() -> int:
    from playwright.sync_api import sync_playwright

    print(f"[B站登录] 打开 Edge：{LOGIN_URL}")
    print("[B站登录] 请在弹出的窗口里登录 B 站（扫码或账号密码）。登录成功后 Cookie 会自动保存。")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            executable_path=EDGE_PATH,
            headless=False,
            viewport={"width": 1280, "height": 860},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL)
        try:
            deadline = time.time() + WAIT_SECONDS
            while time.time() < deadline:
                cookies = context.cookies(["https://www.bilibili.com"])
                names = {c["name"] for c in cookies}
                if "SESSDATA" in names:
                    raw = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
                    Path(BILIBILI_COOKIES_PATH).write_text(
                        json.dumps({"raw": raw}, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    uid = next((c["value"] for c in cookies if c["name"] == "DedeUserID"), "?")
                    print(f"[B站登录] 已保存 {len(cookies)} 个 Cookie 到 {BILIBILI_COOKIES_PATH}（用户ID: {uid}）")
                    return 0
                time.sleep(POLL_INTERVAL)
            print("[B站登录] 超时未检测到登录，可稍后重新运行本脚本。")
            return 1
        finally:
            context.close()


if __name__ == "__main__":
    raise SystemExit(main())
