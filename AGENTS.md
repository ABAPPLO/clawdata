# AGENT.md — clawdata Agent 操作接口说明

给 AI agent / 自动化脚本用的操作手册。clawdata 是本地素材生产线(抖音 + B站)：发现热点、采集下载视频、AI 打标、姿态筛选；外加「视频文库」：订阅博主 → 提取视频简介/字幕中的文档代码信息 → AI 总结成可检索问询的知识文档。

人类用的 Web 面板和 agent 用的 API 是同一套后端。**优先直接调 HTTP API,不要模拟页面点击**——更快、更稳、结果结构化。

## 启动与访问

```bash
python -m clawdata.web --port 8000    # 启动面板/API（前台运行）
```

- API 基址：`http://127.0.0.1:8000`，仅绑定本机回环，无鉴权
- 所有 POST/PUT 均为 JSON body（`Content-Type: application/json`），响应均为 JSON
- 命令行入口见文末「CLI 等价命令」

## 通用模式：长任务 = 启动 + 轮询

采集、下载、订阅刷新、文库整理都是后台任务，同一时间各只允许一个实例，模式统一：

1. `POST /api/xxx/...` 启动 → 立即返回 `{ok: true, active: true, job: {...}}`；已在运行则返回 HTTP 409
2. `GET /api/xxx/status` 轮询（建议 3~4 秒一次）→ `{active: bool, job: {status, message, ...}}`
3. `job.status` 变为 `completed` / `failed` 即结束，`active: false`；之后拉列表接口看结果

超时设置：启动接口秒回；**`/api/digest/ask` 是同步接口，要等 AI 生成，客户端超时建议 ≥ 120 秒**。

---

## 1. 视频文库（核心：信息收集与整理问询）

### 整理订阅博主最新视频

```bash
# 启动整理：subscription_id=0 表示全部启用订阅；limit=每个订阅最多处理的新视频数（0=用配置默认 5）
curl -X POST http://127.0.0.1:8000/api/digest/run \
  -H "Content-Type: application/json" \
  -d '{"subscription_id": 0, "limit": 5}'

# 轮询进度
curl http://127.0.0.1:8000/api/digest/status
# {"active": true, "job": {"status": "running", "message": "(1/2) 检查「xx」…", ...}}
```

整理内容：B站视频取完整简介 + CC 字幕（AI 字幕需 `config/cookies_bilibili.json` 登录态）；抖音取完整简介。自动提取网盘/GitHub/飞书等链接与提取码，交局域网 ComfyUI（Qwen3-VL，`config/digest.json` 的 `server`）总结。不下载视频；按视频 ID 判重，已整理过的自动跳过。AI 不可用时结构化信息照常入库，`status` 为 `no_llm`，可事后重新整理。

### 查询文库

```bash
# 列表：platform 可选 bilibili/douyin；q 匹配标题/摘要/简介/作者；limit 默认 200
curl "http://127.0.0.1:8000/api/digest/list?platform=&author=&q=关键词"

# 详情：含提取的链接表、AI 要点、总结、Markdown 原文（doc_md）
curl "http://127.0.0.1:8000/api/digest/detail?id=1"

# 下载该条 Markdown 文档原文（注意：响应无 Content-Disposition，自行指定保存文件名）
curl "http://127.0.0.1:8000/api/digest/doc?id=1" -o digest_1.md
```

列表 item 关键字段：`id, platform, aweme_id, author, title, pubdate, url(原视频), summary, key_points[], links[{type,value,note}], status(done|no_llm|error), doc_path`。`stats` 含总数与博主分布。

### 知识问询（同步，超时设长）

```bash
curl -X POST http://127.0.0.1:8000/api/digest/ask \
  -H "Content-Type: application/json" \
  -d '{"q": "哪个视频提供了代码仓库或网盘链接？"}'
# {"ok": true, "answer": "……", "refs": [{"id": 3, "title": "…", "author": "…"}]}
```

回答基于文库检索（无编造），`refs` 是参考文档 id 列表，可再调 detail 查看。文库为空或无命中时 answer 会明说。

### 单条维护

```bash
# 重新 AI 总结一条（同步，等 ComfyUI）
curl -X POST http://127.0.0.1:8000/api/digest/redigest -H "Content-Type: application/json" -d '{"id": 1}'

# 删除一条（同时删除对应 md 文档）
curl -X POST http://127.0.0.1:8000/api/digest/delete -H "Content-Type: application/json" -d '{"id": 1}'
```

### 修改整理规则（AI 关注点）

```bash
curl http://127.0.0.1:8000/api/digest/config                    # 读
curl -X POST http://127.0.0.1:8000/api/digest/config \
  -H "Content-Type: application/json" \
  -d '{"focus_points": ["文档/资料下载链接", "代码仓库与代码片段说明", "工具清单", "教程步骤", "结论与建议"]}'
```

保存到 `config/digest.json`，下次整理生效。

---

## 2. 订阅管理（文库与下载共用的博主清单）

```bash
# 列出订阅（含 enabled/interval_hours/last_error/account）
curl http://127.0.0.1:8000/api/subscriptions
# {"subscriptions": [...], "active": false, "job": null}

# 添加订阅：homepage 支持 抖音主页/分享短链/sec_uid、space.bilibili.com/<mid>
curl -X POST http://127.0.0.1:8000/api/subscriptions/add \
  -H "Content-Type: application/json" \
  -d '{"name": "某UP", "homepage": "https://space.bilibili.com/123456", "interval_hours": 6}'

# 触发订阅下载刷新（下载新视频，与文库整理无关）：id 缺省/0 = 到期订阅全部
curl -X POST http://127.0.0.1:8000/api/subscriptions/refresh -H "Content-Type: application/json" -d '{"id": 3}'
# 下载为逐页进行：一页全部下载完才翻下一页（页间随机停顿数秒），某页无新视频即停止；
# 首次刷新会自动回溯订阅前的历史作品（单次上限约 20 页）。抖音接口翻页失败自动回退浏览器采集最新一批。

# 某订阅已下载的视频
curl "http://127.0.0.1:8000/api/subscriptions/videos?id=3"

# 删除订阅（不删已下载文件）
curl -X POST http://127.0.0.1:8000/api/subscriptions/delete -H "Content-Type: application/json" -d '{"id": 3}'
```

---

## 3. 采集与下载

```bash
# 粘贴链接批量下载（抖音分享链接/视频ID 与 B站 BV号/链接可混贴，一行一条，# 注释）
curl -X POST http://127.0.0.1:8000/api/links/download \
  -H "Content-Type: application/json" \
  -d '{"text": "https://www.bilibili.com/video/BVxxxx\n7312345678901234567", "category": "手动下载"}'
curl http://127.0.0.1:8000/api/links/status      # 轮询

# B站热门榜 / 关键词采集下载
curl -X POST http://127.0.0.1:8000/api/bili/collect \
  -H "Content-Type: application/json" -d '{"mode": "popular", "count": 5}'
curl -X POST http://127.0.0.1:8000/api/bili/collect \
  -H "Content-Type: application/json" -d '{"mode": "keyword", "keyword": "python", "count": 5}'
# 进度同 /api/links/status（同一下载队列）

# 热榜（只抓列表，不下载）
curl -X POST http://127.0.0.1:8000/api/hot/refresh
curl http://127.0.0.1:8000/api/hottoday          # 今日热点事件
curl http://127.0.0.1:8000/api/hotdays           # 可用日期
# 按热点词采集下载
curl -X POST http://127.0.0.1:8000/api/hot/download -H "Content-Type: application/json" -d '{"word": "某热点", "count": 3}'
curl http://127.0.0.1:8000/api/collect/status    # 轮询
```

抖音链接需要 `config/cookies.json` 登录态；B站匿名可用，`config/cookies_bilibili.json` 可提清晰度。

## 4. 下载记录与内容理解

```bash
curl http://127.0.0.1:8000/api/today                     # 当天下载记录
curl http://127.0.0.1:8000/api/history                   # 按天汇总
curl "http://127.0.0.1:8000/api/day?date=2026-09-22"     # 某天记录
curl -X POST http://127.0.0.1:8000/api/tagall -H "Content-Type: application/json" -d '{"force": false}'   # 全部未打标视频入队（ComfyUI 视觉打标）
curl http://127.0.0.1:8000/api/tag/stats                 # 打标进度
curl http://127.0.0.1:8000/media/123                     # 播放/下载 id=123 的视频文件
```

---

## CLI 等价命令

不想走 HTTP 时，直接在项目根目录执行：

```bash
python -m clawdata.digest                    # 全部启用订阅整理（每订阅默认 5 条）
python -m clawdata.digest --sub 3 --limit 2  # 指定订阅/条数
python -m clawdata.digest --ask "代码仓库地址是什么"
python -m clawdata --hot-list-only           # 只刷新热榜
python -m clawdata.collection.bilibili "https://www.bilibili.com/video/BVxxxx"   # 解析单个B站视频
python -m clawdata.migrate pull --from http://<源机IP>:8000                     # 从另一台机器按资产拉取迁移
```

## 数据迁移（搬到部署机）

按资产逐文件传输：一个 zip 包 = 一条视频（或一篇文库文档）+ 完整元数据；按视频 ID 去重，中断后重跑同一命令即续传。

```bash
# 源机不用装东西：面板对部署机可达即可（start_dashboard.bat 或 --host 0.0.0.0 启动）
# 部署机执行：
python -m clawdata.migrate pull --from http://<源机IP>:8000                     # 全量 downloads+digests
python -m clawdata.migrate pull --from http://<源机IP>:8000 --type digests --limit 10 --dry-run
python -m clawdata.migrate import-file download_59.zip                          # 手动导入单个包
python -m clawdata.migrate adopt --from-dir /tmp/clawdata_copy --dry-run        # 接管整份拷贝的文件夹
```

大批量搬运可用「文件夹接管」：任意方式（scp/rsync/U盘/共享目录）把源机的 `data/`（含 clawdata.db）与
`downloads/` 整个拷到目标机，再执行 `adopt --from-dir <目录>` 一次合并入库——按视频 ID 去重合并、
自动修正 Windows/Linux 路径分隔符；拷贝前先停源面板保证 db 是完整快照。

等价 API：`GET /api/migrate/list?type=downloads|digests&after_id=&limit=`（列表）、
`GET /api/migrate/export?type=&id=`（zip 附件）、`POST /api/migrate/import`（请求体为 zip 二进制）、
`POST /api/migrate/pull`（后台拉取任务，进度看 `GET /api/migrate/pull/status`）。
面板左侧「数据迁移」页就是这些能力的 Web 入口：填源面板地址后台拉取，或直接上传 zip 导入。
导入保留原始日期与 AI 标签；订阅清单不随迁（目标机重新 add），打标/姿态筛选结果可重跑。

## 远程素材源（把其他素材库当本机的用）

在面板左侧「远程素材」页填入远端地址即可在线浏览/搜索/播放对方素材库——文件不复制到本机、不占磁盘，远端新增素材立即可见；需要的条目可单条「拉取到本地」。源清单持久化在 `config/remote.json`，可配多个源。**支持两类源，添加时自动探测**：

- `clawdata`：另一台 clawdata 面板（远端需 `--host 0.0.0.0` 启动）。按天浏览、全库搜索、`/media/<id>` 代理播放；拉取走迁移 zip 通道（按视频 ID 去重）。
- `videoshare`：video-share 文件素材库（同款服务，零依赖 Node）。目录/搜索浏览（按 mtime 聚合日期）、类型过滤（视频/图片/音频）、`/api/stream` 代理播放；拉取 = 直接下载入库（按 `vs_<hash>` 去重，仅视频）。远端开 `--token` 时添加源填 token。

```bash
# 源管理（token 可选；添加时自动识别 clawdata / videoshare）
curl http://127.0.0.1:8000/api/remote/sources                       # 清单 + 在线状态（3s 探活）
curl -X POST http://127.0.0.1:8000/api/remote/sources \
  -H "Content-Type: application/json" \
  -d '{"name": "本机素材库", "base": "http://127.0.0.1:8000", "token": ""}'
curl -X POST http://127.0.0.1:8000/api/remote/sources/update \
  -H "Content-Type: application/json" -d '{"id": "s1", "enabled": false}'   # 改名/启停
curl -X POST http://127.0.0.1:8000/api/remote/sources/delete \
  -H "Content-Type: application/json" -d '{"id": "s1"}'                     # 删除（仅移除本机配置）

# 浏览与播放（source 填源 id；kind 对 videoshare 生效：video/image/audio/file）
curl "http://127.0.0.1:8000/api/remote/records?source=s1"                       # 最新（今天）
curl "http://127.0.0.1:8000/api/remote/records?source=s1&q=关键词"              # 远端全库搜索
curl "http://127.0.0.1:8000/api/remote/records?source=s1&day=2026-10-04"        # 指定日期
curl "http://127.0.0.1:8000/api/remote/records?source=s2&kind=image"            # video-share 按类型
curl "http://127.0.0.1:8000/api/remote/history?source=s1"                       # 按天汇总（日期导航）
curl "http://127.0.0.1:8000/remote-media/s1/<素材id>" -o remote.mp4             # 代理播放/下载（支持 Range）

# 单条拉取到本机库（同步，大视频视带宽可能数十秒；重复拉取自动跳过）
curl -X POST http://127.0.0.1:8000/api/remote/pull-one \
  -H "Content-Type: application/json" -d '{"source": "s1", "id": 392}'
```

记录字段统一为 `{id, title, author, size, created_at, kind, playable, pullable}`（playable=可代理播放，pullable=可拉取入库；clawdata 源恒为 true/true，videoshare 源按素材类型）。与「数据迁移」的区别：迁移是把资产复制进本机库（离线可用、占磁盘、一次性）；远程源是常驻在线视图（不占磁盘、依赖远端可达、增量即时可见）。`/remote-media/` 代理只访问远端只读 GET 端点，不会成为远端的写入口。

## MCP 接入（agent 用工具而非 curl 操作平台）

部署形态 = Web 面板 + MCP 服务两个常驻单元：`clawdata.service`（:8000 面板/API）与
`clawdata-mcp.service`（:8100 streamable-http，Bearer Token 在 `config/mcp.json`，健康检查 `GET /`）。

`python -m clawdata.mcp` 把面板 API 包装成 32 个 MCP 工具：查询问询（status_overview/digest_list/digest_ask…）、
任务启动+轮询（subscription_refresh_start→job_status…）、迁移（migrate_pull_start/migrate_adopt_start…）、
远程素材源（remote_sources_list/remote_records/remote_pull_one…）。
删除/迁移等危险工具需显式 `confirm=true`。客户端两种接法：
- HTTP 直连（推荐）：`url = http://<部署机>:8100/mcp` + `Authorization: Bearer <token>`
- stdio 按需拉起：`python -m clawdata.mcp --api http://127.0.0.1:8000`（依赖 `requirements-mcp.txt`）

配置模板 `docs/mcp-zcode.json`（ZCode）与 `docs/mcp-generic.json`（Claude Code/Cursor）。
长任务仍是「启动+轮询」，digest_ask 同步耗时 1~2 分钟。

## 注意事项

- 面板/API 只绑 `127.0.0.1`。agent 在远端时，用 SSH 端口转发或在项目机器本地执行 curl/CLI。
- AI 功能（文库总结、问询、打标）依赖局域网 ComfyUI：文库用 `config/digest.json` 的 `server`（当前 `http://10.168.1.112:8188`），打标用 `config/tagging.json`（`http://10.168.1.106:8818`）。离线时文库整理仍会入库结构化信息（`status: no_llm`），可稍后 redigest。
- `/api/digest/delete` 会同时删除 `data/digests/` 下对应 Markdown 文档；删除订阅、删除文库记录都不影响 `downloads/` 里的视频。
- 数据都在 `data/clawdata.db`（SQLite）与 `data/`、`downloads/` 目录，路径相对项目根。
- 完整功能与配置说明见 `README.md`。
