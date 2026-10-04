"""远程素材源：把局域网内其他 clawdata 面板的素材库当本机的素材来用。

远端只要以 `--host 0.0.0.0`（或 start_dashboard.bat）启动，本模块即可调用其
只读端点做浏览/搜索，并通过 `/media/<id>` 代理在线播放——文件不复制到本机。
需要落到本机的条目走迁移通道：`/api/migrate/export` 打 zip 再 `import_bundle`
导入（按视频 ID 去重，与 `python -m clawdata.migrate pull` 同一套机制）。

源清单存 `config/remote.json`：
    {"sources": [{"id": "s1", "name": "105素材库", "base": "http://10.168.1.105:8000", "enabled": true}]}
"""

from __future__ import annotations

import json
import os
import tempfile
import urllib.parse
import urllib.request

from clawdata.core.paths import DEFAULT_DB_PATH, DEFAULT_REMOTE_CONFIG_PATH
from clawdata.storage import migrate

# 只调用远端的只读 GET 端点；不做任意路径代理，避免本机面板变成远端的写入口
CHECK_TIMEOUT = 3.0
FETCH_TIMEOUT = 15.0


def load_sources(path: str = DEFAULT_REMOTE_CONFIG_PATH) -> list[dict]:
    """读取源清单（无文件时返回空列表，字段规范化）。"""
    data = {}
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f) or {}
        except (ValueError, OSError):
            data = {}
    sources = []
    for s in data.get("sources") or []:
        if not isinstance(s, dict) or not str(s.get("base") or "").strip():
            continue
        sources.append({
            "id": str(s.get("id") or ""),
            "name": str(s.get("name") or "").strip() or str(s["base"]).rstrip("/"),
            "base": str(s["base"]).strip().rstrip("/"),
            "enabled": bool(s.get("enabled", True)),
        })
    return sources


def save_sources(sources: list[dict], path: str = DEFAULT_REMOTE_CONFIG_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"sources": sources}, f, ensure_ascii=False, indent=2)


def get_source(sid: str, path: str = DEFAULT_REMOTE_CONFIG_PATH) -> dict | None:
    for s in load_sources(path):
        if s["id"] == sid:
            return s
    return None


def _new_id(existing: list[str]) -> str:
    n = 1
    while f"s{n}" in existing:
        n += 1
    return f"s{n}"


def add_source(name: str, base: str, path: str = DEFAULT_REMOTE_CONFIG_PATH) -> dict:
    """验证地址可达且是 clawdata 面板后加入清单，返回新源。"""
    base = base.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError("地址需以 http:// 或 https:// 开头")
    check_source(base)  # 不可达/非 clawdata 时抛 ValueError
    sources = load_sources(path)
    if any(s["base"] == base for s in sources):
        raise ValueError("该地址已在源清单中")
    src = {
        "id": _new_id([s["id"] for s in sources]),
        "name": name.strip() or base,
        "base": base,
        "enabled": True,
    }
    sources.append(src)
    save_sources(sources, path)
    return src


def fetch_json(url: str, timeout: float = FETCH_TIMEOUT):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_source(base: str) -> dict:
    """探活并确认对面是 clawdata 面板（/api/history 返回按天汇总数组）。"""
    try:
        data = fetch_json(f"{base.rstrip('/')}/api/history", timeout=CHECK_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法访问 {base}：{str(exc)[:120]}") from exc
    if not isinstance(data, list):
        raise ValueError(f"{base} 响应异常，疑似不是 clawdata 面板")
    total = sum(int(d.get("count") or 0) for d in data if isinstance(d, dict))
    return {"reachable": True, "days": len(data), "records": total}


def source_state(base: str) -> dict:
    """探活的非抛错版本，用于源列表展示。"""
    try:
        info = check_source(base)
    except ValueError as exc:
        return {"reachable": False, "error": str(exc), "days": 0, "records": 0}
    return info


def remote_history(base: str) -> list[dict]:
    return fetch_json(f"{base.rstrip('/')}/api/history")


def remote_records(base: str, day: str = "", q: str = "", limit: int = 200) -> list[dict]:
    """拉远端下载记录：q 非空走全库搜索（/api/tags），否则按天（缺省=今天）。"""
    base = base.rstrip("/")
    if q:
        data = fetch_json(
            f"{base}/api/tags?status=all&limit={int(limit)}"
            f"&q={urllib.parse.quote(q)}")
        return data.get("records") or []
    if day:
        return fetch_json(f"{base}/api/day?date={urllib.parse.quote(day)}") or []
    return fetch_json(f"{base}/api/today") or []


def pull_one(base: str, asset_type: str, rid: int, db_path: str = DEFAULT_DB_PATH) -> dict:
    """把远端单个资产导入本机库（zip 通道，aweme_id 去重）。"""
    base = base.rstrip("/")
    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    try:
        with urllib.request.urlopen(
            f"{base}/api/migrate/export?type={urllib.parse.quote(asset_type)}&id={int(rid)}",
            timeout=600.0,
        ) as resp, open(tmp.name, "wb") as out:
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        return migrate.import_bundle(db_path, tmp.name)
    finally:
        try:
            os.remove(tmp.name)
        except OSError:
            pass
