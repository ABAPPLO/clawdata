"""clawdata MCP 适配器：把面板 HTTP API 暴露为 MCP 工具，供 agent 客户端接入。

两种形态：
- stdio（默认）：由 agent 客户端在 mcpServers 配置里声明后按需拉起，会话结束退出
- http：常驻服务（部署时与 Web 面板并列，`--transport http`），局域网 agent
  经 http://<host>:<port>/mcp 直连，可选用 Bearer Token 鉴权（--token /
  环境变量 CLAWDATA_MCP_TOKEN / config/mcp.json 的 {"token": ...}）

所有工具调用都翻译为对面板现有 HTTP API 的请求，面板仍是唯一后端
（与网页、curl 完全等价）。

用法：
    python -m clawdata.mcp --api http://127.0.0.1:8000                    # stdio
    python -m clawdata.mcp --transport http --host 0.0.0.0 --port 8100    # 常驻 HTTP

长任务（采集/下载/整理/迁移）遵循「启动 + 轮询」：start 工具立即返回
任务已启动，agent 用 job_status 轮询（建议 3~4 秒一次）。
digest_ask 是唯一同步长调用（等 AI 生成，客户端超时需 ≥120 秒）。

依赖：pip install -r requirements-mcp.txt（官方 mcp SDK）
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "http://127.0.0.1:8000"
_TIMEOUT = 20.0
_TIMEOUT_LONG = 180.0  # digest_ask 等待 ComfyUI 生成


def _get(path: str, timeout: float = _TIMEOUT) -> Any:
    url = API + path
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post(path: str, body: dict | None = None, timeout: float = _TIMEOUT) -> Any:
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        API + path, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _j(obj: Any, limit: int = 4000) -> str:
    """压缩成单行 JSON 文本返回给 agent；超长截断防刷上下文。"""
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= limit else s[:limit] + "…(截断)"


def _build() -> "FastMCP":  # noqa: F821 - 延迟导入后类型仅作注释
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("clawdata")

    # ------------------------------------------------------------ 查询/问询
    @mcp.tool()
    def status_overview() -> str:
        """平台总览：当天下载量、各后台任务状态、订阅数、打标进度。一眼看全。"""
        out: dict[str, Any] = {}
        for key, path in (("today_count", "/api/today"), ("collect", "/api/collect/status"),
                          ("download", "/api/links/status"), ("digest", "/api/digest/status"),
                          ("subscriptions", "/api/subscriptions"), ("tag", "/api/tag/stats")):
            try:
                v = _get(path)
                if key == "today_count":
                    out[key] = len(v) if isinstance(v, list) else v
                elif key == "subscriptions":
                    out[key] = {"count": len(v.get("subscriptions") or []),
                                "active": v.get("active"), "job": v.get("job")}
                else:
                    out[key] = v
            except Exception as exc:  # noqa: BLE001
                out[key] = {"error": str(exc)[:120]}
        return _j(out)

    @mcp.tool()
    def today_records(limit: int = 50) -> str:
        """当天下载记录列表（id/标题/作者/分类/大小/打标状态）。"""
        rows = _get("/api/today")
        if isinstance(rows, list):
            slim = [{k: r.get(k) for k in
                     ("id", "title", "author", "tag_category", "size", "tag_status", "file")}
                    for r in rows[:max(1, min(limit, 200))]]
            return _j({"count": len(rows), "items": slim})
        return _j(rows)

    @mcp.tool()
    def day_records(date: str, limit: int = 100) -> str:
        """某天的下载记录（date 格式 YYYY-MM-DD）。"""
        rows = _get(f"/api/day?date={urllib.parse.quote(date)}")
        if isinstance(rows, list):
            slim = [{k: r.get(k) for k in ("id", "title", "author", "tag_category", "size", "tag_status")}
                    for r in rows[:max(1, min(limit, 300))]]
            return _j({"date": date, "count": len(rows), "items": slim})
        return _j(rows)

    @mcp.tool()
    def history_summary() -> str:
        """按天汇总的下载历史（数量/成功/大小）。"""
        return _j(_get("/api/history"))

    @mcp.tool()
    def hot_today() -> str:
        """今日热点事件列表（本地缓存，可用 hot_refresh 刷新）。"""
        return _j(_get("/api/hottoday"))

    @mcp.tool()
    def hot_days() -> str:
        """可用的热点日期列表。"""
        return _j(_get("/api/hotdays"))

    # ------------------------------------------------------------ 订阅
    @mcp.tool()
    def subscriptions_list() -> str:
        """订阅博主列表（含启用状态/检查间隔/下次检查/最近错误）。"""
        return _j(_get("/api/subscriptions"))

    @mcp.tool()
    def subscription_videos(subscription_id: int) -> str:
        """某订阅已下载的视频列表。"""
        return _j(_get(f"/api/subscriptions/videos?id={int(subscription_id)}"))

    @mcp.tool()
    def subscription_add(name: str, homepage: str, interval_hours: int = 6,
                         category: str = "订阅博主") -> str:
        """添加订阅。homepage 支持 抖音主页/分享短链/sec_uid、space.bilibili.com/<mid>。"""
        return _j(_post("/api/subscriptions/add", {
            "name": name, "homepage": homepage,
            "interval_hours": int(interval_hours), "category": category}))

    @mcp.tool()
    def subscription_delete(subscription_id: int, confirm: bool = False) -> str:
        """删除订阅（不删已下载文件）。危险操作：必须 confirm=true。"""
        if not confirm:
            return "已阻止：请确认后重试（confirm=true）。删除订阅不影响已下载的视频文件。"
        return _j(_post("/api/subscriptions/delete", {"id": int(subscription_id)}))

    @mcp.tool()
    def subscription_refresh_start(subscription_id: int = 0) -> str:
        """触发订阅下载刷新（逐页下载新视频，首次会回溯历史作品）。
        subscription_id=0 表示全部到期订阅。用 job_status(kind="subscriptions") 轮询。"""
        return _j(_post("/api/subscriptions/refresh", {"id": int(subscription_id)}))

    # ------------------------------------------------------------ 视频文库
    @mcp.tool()
    def digest_list(platform: str = "", author: str = "", q: str = "", limit: int = 30) -> str:
        """文库检索：q 匹配标题/摘要/简介/作者；platform 可选 bilibili/douyin。"""
        params = urllib.parse.urlencode({k: v for k, v in
                                         (("platform", platform), ("author", author),
                                          ("q", q), ("limit", max(1, min(limit, 100)))) if v})
        return _j(_get(f"/api/digest/list?{params}"))

    @mcp.tool()
    def digest_detail(digest_id: int) -> str:
        """文库单条详情：AI 要点/总结/提取的链接表（网盘、仓库等）。"""
        return _j(_get(f"/api/digest/detail?id={int(digest_id)}"), limit=6000)

    @mcp.tool()
    def digest_doc(digest_id: int) -> str:
        """文库单条的 Markdown 原文（含简介/字幕全文）。"""
        with urllib.request.urlopen(f"{API}/api/digest/doc?id={int(digest_id)}",
                                    timeout=_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", "replace")
        return text[:15000] + ("…(截断)" if len(text) > 15000 else "")

    @mcp.tool()
    def digest_ask(question: str) -> str:
        """知识问询（同步，等 AI 生成，可能 1~2 分钟）：基于文库检索回答，不编造；
        返回 answer 与参考文档 refs（可用 digest_detail 深挖）。"""
        return _j(_post("/api/digest/ask", {"q": question}, timeout=_TIMEOUT_LONG), limit=6000)

    @mcp.tool()
    def digest_run_start(subscription_id: int = 0, limit: int = 0) -> str:
        """启动文库整理（拉订阅最新视频→提取简介/字幕→AI 总结入库）。
        subscription_id=0 全部启用订阅；limit=每订阅最多处理新视频数（0=默认5）。
        用 job_status(kind="digest") 轮询。"""
        return _j(_post("/api/digest/run", {"subscription_id": int(subscription_id),
                                            "limit": int(limit)}))

    @mcp.tool()
    def digest_redigest(digest_id: int) -> str:
        """重新 AI 总结一条文库记录（同步，需等待）。"""
        return _j(_post("/api/digest/redigest", {"id": int(digest_id)}, timeout=_TIMEOUT_LONG))

    @mcp.tool()
    def digest_delete(digest_id: int, confirm: bool = False) -> str:
        """删除一条文库记录及其 Markdown 文档。危险操作：必须 confirm=true。"""
        if not confirm:
            return "已阻止：请确认后重试（confirm=true）。该操作会同时删除 data/digests/ 下的文档。"
        return _j(_post("/api/digest/delete", {"id": int(digest_id)}))

    @mcp.tool()
    def digest_config() -> str:
        """读取文库整理配置（AI 关注点 focus_points 等）。"""
        return _j(_get("/api/digest/config"))

    # ------------------------------------------------------------ 采集/下载/打标
    @mcp.tool()
    def links_download_start(text: str, category: str = "手动下载") -> str:
        """粘贴链接批量下载：抖音分享链接/视频ID 与 B站 BV号/链接混贴，一行一条。
        用 job_status(kind="download") 轮询。"""
        return _j(_post("/api/links/download", {"text": text, "category": category}))

    @mcp.tool()
    def bili_collect_start(mode: str = "popular", keyword: str = "", count: int = 5) -> str:
        """B站采集下载：mode=popular（热门榜）或 keyword（关键词搜索）。
        用 job_status(kind="download") 轮询。"""
        return _j(_post("/api/bili/collect", {"mode": mode, "keyword": keyword, "count": count}))

    @mcp.tool()
    def hot_refresh() -> str:
        """只刷新热榜缓存（不下载视频），之后用 hot_today 查看。"""
        return _j(_post("/api/hot/refresh"))

    @mcp.tool()
    def hot_download_start(word: str, count: int = 3) -> str:
        """按热点词采集下载视频。用 job_status(kind="collect") 轮询。"""
        return _j(_post("/api/hot/download", {"word": word, "count": count}))

    @mcp.tool()
    def tag_enqueue_all(force: bool = False) -> str:
        """全部未打标视频入队（局域网 ComfyUI 视觉打标）。force=true 重打已打的。"""
        return _j(_post("/api/tagall", {"force": force}))

    @mcp.tool()
    def tag_stats() -> str:
        """打标进度（总数/未打/打标中/已打/失败）。"""
        return _j(_get("/api/tag/stats"))

    # ------------------------------------------------------------ 迁移
    @mcp.tool()
    def migrate_pull_start(source: str, type: str = "downloads,digests", limit: int = 0) -> str:
        """从另一台 clawdata 面板按资产拉取导入（后台任务，按视频 ID 去重可续传）。
        source 形如 http://10.168.1.105:8000（源面板需可达）。
        用 job_status(kind="migrate") 轮询。"""
        return _j(_post("/api/migrate/pull", {"source": source, "type": type, "limit": limit}))

    @mcp.tool()
    def migrate_adopt_start(path: str, limit: int = 0, dry_run: bool = False,
                            confirm: bool = False) -> str:
        """接管整份拷贝过来的文件夹（含 clawdata.db 与 downloads/）合并入库。
        path=服务器上的目录；dry_run=true 只列出不落库。
        正式接管（dry_run=false）必须 confirm=true。用 job_status(kind="migrate") 轮询。"""
        if not dry_run and not confirm:
            return "已阻止：正式接管请加 confirm=true（合并入库，不清空已有数据，按视频 ID 去重）。"
        return _j(_post("/api/migrate/adopt", {"path": path, "limit": limit, "dry_run": dry_run}))

    @mcp.tool()
    def migrate_stop() -> str:
        """停止当前迁移任务（当前资产完成后停下）。"""
        return _j(_post("/api/migrate/pull/stop"))

    # ------------------------------------------------------------ 任务轮询
    @mcp.tool()
    def job_status(kind: str = "download") -> str:
        """轮询后台任务进度。kind 可选：collect(热点采集)/download(链接与B站下载)/
        digest(文库整理)/subscriptions(订阅刷新)/migrate(数据迁移)/tag(打标)。"""
        kind = (kind or "").strip().lower()
        mapping = {
            "collect": "/api/collect/status",
            "download": "/api/links/status",
            "digest": "/api/digest/status",
            "subscriptions": "/api/subscriptions",
            "migrate": "/api/migrate/pull/status",
            "tag": "/api/tag/stats",
        }
        if kind not in mapping:
            return f"未知 kind：{kind}，可选：{'/'.join(mapping)}"
        v = _get(mapping[kind])
        if kind == "subscriptions":
            v = {"active": v.get("active"), "job": v.get("job")}
        return _j(v, limit=3000)

    return mcp


def _load_token(cli_token: str) -> str:
    """Token 优先级：命令行 > 环境变量 CLAWDATA_MCP_TOKEN > config/mcp.json。空=不鉴权。"""
    if cli_token:
        return cli_token
    import os

    env = os.environ.get("CLAWDATA_MCP_TOKEN", "")
    if env:
        return env
    try:
        from clawdata.core.paths import CONFIG_DIR

        cfg_path = os.path.join(CONFIG_DIR, "mcp.json")
        if os.path.isfile(cfg_path):
            with open(cfg_path, encoding="utf-8") as f:
                return str(json.load(f).get("token") or "")
    except Exception:  # noqa: BLE001
        pass
    return ""


def _auth_asgi(app, token: str):
    """给 streamable-http 的 ASGI 应用包一层 Bearer Token 校验 + 根路径健康检查。"""

    async def wrapped(scope, receive, send):
        if scope["type"] != "http":
            await app(scope, receive, send)
            return
        path = scope.get("path", "/")
        if path == "/" :
            body = b'{"ok":true,"service":"clawdata-mcp"}'
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            await send({"type": "http.response.body", "body": body})
            return
        if token:
            headers = {k.decode("latin1").lower(): v.decode("latin1")
                       for k, v in scope.get("headers", [])}
            if headers.get("authorization") != f"Bearer {token}":
                body = b'{"error":"unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return wrapped


def main(argv: list[str] | None = None) -> int:
    global API
    parser = argparse.ArgumentParser(
        prog="python -m clawdata.mcp",
        description="clawdata MCP 适配器：把面板 API 暴露为 MCP 工具（stdio 或常驻 HTTP）")
    parser.add_argument("--api", default=API, help="面板地址（默认 http://127.0.0.1:8000）")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio",
                        help="stdio=客户端按需拉起；http=常驻服务（部署形态）")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 模式绑定地址（如 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8100, help="HTTP 模式端口（默认 8100）")
    parser.add_argument("--token", default="", help="HTTP 模式 Bearer Token（空=不鉴权）")
    parser.add_argument("--list-tools", action="store_true",
                        help="调试：列出工具名与描述后退出（不走 MCP 协议）")
    args = parser.parse_args(argv)
    API = args.api.rstrip("/")

    mcp = _build()
    if args.list_tools:
        import asyncio

        async def _dump() -> None:
            tools = await mcp.list_tools()
            for t in tools:
                desc = (t.description or "").splitlines()[0]
                print(f"{t.name}: {desc}")

        asyncio.run(_dump())
        return 0
    if args.transport == "http":
        import uvicorn

        token = _load_token(args.token)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        app = _auth_asgi(mcp.streamable_http_app(), token)
        print(f"clawdata-mcp http://{'0.0.0.0' if args.host == '0.0.0.0' else args.host}:{args.port}/mcp"
              f"（鉴权：{'Token 已启用' if token else '未启用（内网信任）'}，面板 {API}）")
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0
    mcp.run()  # stdio
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
