# clawdata

clawdata 是一条本地化的素材生产线：支持抖音与 B 站，发现热点或博主内容，采集并下载视频，沉淀成可搜索的素材资产，再用 AI 打标和姿态筛选找出可二次创作的片段。

## 产品流程

```text
发现选题 -> 采集视频 -> 人味化下载 -> SQLite 沉淀 -> AI/姿态筛选 -> 面板管理素材
```

核心能力分为 7 个边界：

| 领域 | 说明 |
| --- | --- |
| 发现 | 抖音热榜、热点事件、主题关键词、博主作品；B 站热门榜、关键词搜索、UP 主订阅 |
| 采集 | 直连接口与浏览器自动化两条路径（抖音）；公开接口解析（B 站） |
| 下载 | 串行、随机间隔、断点续传、失败重试、深夜暂停 |
| 存储 | SQLite 统一记录下载、热榜、订阅、资产和标签 |
| 内容理解 | ComfyUI/Qwen3-VL 生成类别、标签和摘要 |
| 素材筛选 | YOLOv8n-pose + OpenCV 拆分连续动作片段 |
| 知识整理 | 订阅博主最新视频的简介/字幕提取文档代码信息，AI 总结成可检索问询的文库 |
| 面板 | 本地 Web UI 查看历史、播放、订阅、触发任务、修正标签 |

## 目录结构

```text
clawdata/
├─ main.py                       # 兼容入口，等同于 python -m clawdata
├─ app.py                        # 兼容入口，等同于 python -m clawdata.web
├─ clawdata/
│  ├─ core/                      # 项目路径、Cookie、深夜窗口、随机延迟
│  ├─ signing/                   # a_bogus 签名
│  ├─ collection/                # 热榜、关键词、账号、类目、浏览器采集、B 站解析
│  ├─ download/                  # 人味化下载器（B 站 DASH 自动合流）
│  ├─ storage/                   # SQLite 存储与资产同步
│  ├─ ai/                        # ComfyUI 客户端和视频打标队列
│  ├─ digest/                    # 视频文库：简介/字幕提取 + AI 总结 + 问询
│  ├─ vision/                    # 姿态识别、连续动作筛选
│  ├─ pipeline/                  # 采集 + 下载主流程 CLI
│  └─ web/                       # 本地面板 API 与前端
├─ config/
│  ├─ cookies.json               # 抖音登录 Cookie，不要提交
│  ├─ cookies.example.json       # Cookie 示例
│  ├─ cookies_bilibili.json      # B站登录 Cookie（可选），不要提交
│  ├─ cookies_bilibili.example.json
│  ├─ tagging.json               # ComfyUI、分类和提示词配置
│  ├─ digest.json                # 视频文库整理配置（可选，缺省用内置默认）
│  └─ tools.json                 # 连续动作筛选配置（可选）
├─ data/
│  ├─ clawdata.db                # 主要数据资产索引
│  ├─ hotlist/                   # 每日热榜快照
│  ├─ digests/                   # 视频文库生成的 Markdown 知识文档
│  └─ links/                     # 视频链接清单
├─ downloads/                    # 原视频和导出片段
├─ logs/                         # 运行日志
├─ models/                       # YOLOv8n-pose ONNX 模型
├─ out/                          # 热榜导出产物
├─ requirements.txt
├─ run_nightly.bat
└─ start_dashboard.bat
```

`main.py` 和 `app.py` 只保留给旧习惯和计划任务使用；推荐使用下面的模块入口。

## 安装

```bash
python -m venv .venv
. .venv/bin/activate        # Linux/macOS；Windows 是 .venv\Scripts\activate
python -m pip install -r requirements.txt
```

### Ubuntu / Linux 部署说明

- venv 激活路径是 `.venv/bin/activate`（Windows 才是 `.venv/Scripts/activate`）。
- 浏览器采集（抖音热点/搜索/主页兜底、抖音订阅翻页回退）复用本机 Edge/Chrome，Linux 需自行安装其一，例如 `sudo apt install chromium-browser` 或安装微软 Edge 的 .deb 包；非标准路径可用环境变量 `CLAWDATA_BROWSER` 指定可执行文件。
- `run_nightly.bat` 和 `start_dashboard.bat` 是 Windows 脚本，Linux 用等价命令：面板 `python -m clawdata.web --port 8000 --host 0.0.0.0`；夜间热榜 `python -m clawdata --hot-list-only --logfile logs/nightly.log`（可配 crontab）。
- 防火墙放行：`sudo ufw allow 8000/tcp`（Windows 用 netsh，见下文）。

连续动作筛选需要标准 YOLOv8n-pose ONNX 模型（约 13MB），放在：

```text
models/yolov8n-pose.onnx
```

文件缺失时会在首次使用连续动作筛选时**自动下载**（镜像依次尝试 hf-mirror.com 与 huggingface.co，需要外网可达；内网机器可先手动放置）。自动下载的模型输入为 640x640，本地手动导出的通常为 320x320，两者都能用，程序会按模型声明自适应。不想要自动下载可在 `config/tools.json` 里设 `"auto_download_model": false`。

## 配置登录态

具体视频接口需要登录 Cookie。把登录后的抖音 Cookie 写入：

```text
config/cookies.json
```

推荐格式：

```json
{"raw": "ttwid=...; sessionid=...; sid_guard=..."}
```

也支持 Cookie-Editor 导出的数组格式。若缺少 `sessionid`、`sid_guard` 等会话字段，接口可能返回“请先登录”。

## 常用命令

### 抓热榜

```bash
python -m clawdata.collection.douyin_hot --json
python -m clawdata.collection.douyin_hot --limit 20
python -m clawdata --hot-list-only
```

`--hot-list-only` 只刷新热点事件库，不采集或下载视频。

### 热点流水线

```bash
python -m clawdata --keywords 5 --per-word 3
python -m clawdata --keywords 10 --per-word 5 --list-only
python -m clawdata --quiet-start 23:00 --quiet-end 08:00
```

流水线会优先尝试浏览器采集，再尝试接口检索，最后读取 `data/links/links.txt` 兜底。

### 按链接或视频 ID 下载

```bash
python -m clawdata --links data/links/links.txt --list-only
python -m clawdata --links data/links/links.txt
```

每行一个视频 ID、分享链接或主页短链；`#` 开头的行会被忽略。

抖音链接需要登录 Cookie；B 站链接（`BV` 号、`av` 号、`bilibili.com` 视频页、`b23.tv` 短链）无需登录即可解析下载，DASH 音视频流会自动分别下载并用内置 ffmpeg 合流。匿名可下载 360P/480P；把 B 站登录 Cookie 放入 `config/cookies_bilibili.json`（格式见 `config/cookies_bilibili.example.json`）可提升清晰度。单个 B 站视频解析：

```bash
python -m clawdata.collection.bilibili "https://www.bilibili.com/video/BVxxxxxxxxxx"
```

### 按主题、类目、账号采集

```bash
python -m clawdata.collection.collect_theme --keyword 武术 --per-word 8
python -m clawdata.collection.collect_category --keyword 功夫 --category 单人武术展示
python -m clawdata.collection.collect_account --account "https://www.douyin.com/user/xxxx" --count 20
```

类目和账号采集可能弹出浏览器窗口，需要有人值守处理登录或验证码。

### 订阅博主下载（含历史回溯）

订阅刷新按博主作品列表**逐页**下载：一页视频全部下载完成后，随机停顿数秒再请求下一页，模拟真人浏览节奏，不做高频批量抓取；某页没有新视频即停止翻页。因此首次刷新会自动回溯订阅之前的历史作品（单次上限约 20 页：抖音约 400 条、B 站约 600 条，未追完下次刷新继续），日常增量通常一页内追平。抖音接口翻页不可用时自动回退浏览器采集最新一批。

### 数据迁移到其他机器

部署机换机（如 Windows → Linux）时，不用整库拷贝，按资产逐文件传输：一个 zip 包 = 一条视频（或一篇文库文档）+ 完整元数据，按视频 ID 去重、中断后重跑同一命令即续传。

```bash
# 源机面板对部署机可达（--host 0.0.0.0 启动）后，在部署机执行：
python -m clawdata.migrate pull --from http://<源机IP>:8000            # 全量
python -m clawdata.migrate pull --from http://<源机IP>:8000 --type digests --limit 10 --dry-run
```

迁移内容为下载视频（含 AI 标签、原始日期）与视频文库文档；订阅清单请在目标机重新添加，cookie 重新登录维护。

### 远程素材源（跨机共享素材库）

不搬运文件也能用另一台机器的素材：在面板左侧「远程素材」页填入远端地址（自动识别两类源——**clawdata 面板**，如 `http://10.168.1.105:8000`，远端以 `--host 0.0.0.0` 启动；或 **video-share 文件素材库**，可选 token 鉴权），即可在线浏览、搜索并直接播放对方素材库——视频经本机面板代理流式转发（支持拖进度条），不占本机磁盘，远端新增素材立即可见。看中的条目点「拉取到本地」即导入本机库（自动去重）。源清单存 `config/remote.json`，可配多个源；等价 API 见 `AGENTS.md` 的「远程素材源」一节。

### 整理订阅视频信息（视频文库）

```bash
python -m clawdata.digest                # 全部启用订阅，每订阅默认 5 条新视频
python -m clawdata.digest --sub 3        # 只整理订阅 3
python -m clawdata.digest --limit 2      # 每订阅最多 2 条
python -m clawdata.digest --ask "代码仓库地址是什么"
```

对订阅博主（复用「订阅博主」页的订阅列表）拉取最新视频：B 站取完整简介和 CC 字幕（AI 字幕需 B 站登录 Cookie），抖音取简介文案；正则提取网盘/GitHub/飞书等文档代码链接后，交局域网 ComfyUI（Qwen3-VL）总结，每个视频生成一份 Markdown 文档到 `data/digests/` 并入库。不下载视频，与订阅下载线互不影响。AI 不可用时结构化信息照常入库，可稍后「重新整理」。

### 启动面板

```bash
python -m clawdata.web --port 8000
```

然后打开：

```text
http://127.0.0.1:8000
```

部署到局域网供其他设备/Agent 使用：

```bash
python -m clawdata.web --port 8000 --host 0.0.0.0
```

`start_dashboard.bat` 即局域网模式。启动时会打印本机局域网访问地址；防火墙需放行对应端口（放行一次即可）：

```bash
# Windows（管理员执行）
netsh advfirewall firewall add rule name="clawdata-dashboard" dir=in action=allow protocol=TCP localport=8000
# Ubuntu
sudo ufw allow 8000/tcp
```

面板无鉴权，仅适合在可信局域网内自用，不要暴露到公网。SQLite 已启用 WAL 模式以支持多端并发访问。

面板支持当天/历史记录、每日热榜、视频播放、订阅博主、触发采集和下载、批量打标、标签修正、连续动作筛选、资产浏览和历史清理。资产画廊支持按“工具结果 / 原下载视频”、具体来源工具和 AI 标签筛选。

「视频文库」页（Ctrl+6）支持：选择订阅拉取最新视频并 AI 整理（进度轮询）、按平台/博主/关键词筛选文库、点开详情查看提取的链接表与 AI 要点、下载 Markdown、单条重新整理或删除，以及基于文库检索的知识问询（回答附参考文档）。整理时关注的要点（focus_points）可在页面上编辑，保存到 `config/digest.json`。

今日工作台额外提供两个跨平台入口：

- **链接快速下载**：粘贴抖音分享链接/视频 ID 与 B 站视频链接/BV 号（可混贴），解析后直接下载
- **B 站采集**：一键下载 B 站热门榜，或按关键词搜索下载

订阅页同时支持抖音博主与 B 站 UP 主（主页形如 `space.bilibili.com/数字`），按周期增量拉取新视频；点击订阅条目可回看该博主/UP 主的全部下载记录。B 站登录 Cookie 配好后（见上文），这些入口自动使用账号允许的最高清晰度。

资产画廊支持按来源平台（抖音/B站）筛选，卡片带平台徽章与「已切 N 段 / 未处理」状态；点击「详情」打开抽屉，可查看该资产的标签信息与处理链（原片 ↔ 衍生片段双向导航）。`config/assets.json` 的 `keep_categories` 支持 `["*"]` 表示收录全部分类的下载。

画廊还支持：分组视图切换（按工具/分类/日期/平台）、多选批量操作（批量设分类、批量删除、导出勾选项为 CSV）、自定义收藏夹（卡片 ☆ 收藏、多收藏夹管理、按收藏夹筛选）。处理工具页的运行记录带「查看产物」，一键跳到画廊并按该次运行筛选产物。

兼容入口仍然可用：

```bash
python main.py --list-only
python app.py --port 8000
```

## 自动任务

双击或调度 `run_nightly.bat`，它等同于：

```bash
python -m clawdata --hot-list-only --logfile D:\project\clawdata\logs\nightly.log
```

面板可使用 `start_dashboard.bat` 启动。

## 数据与文件规则

- 数据库：`data/clawdata.db`
- 热榜快照：`data/hotlist/YYYY-MM-DD.json`
- 视频清单：`data/links/*.txt`
- 文库文档：`data/digests/{platform}_{视频ID}.md`
- 原视频：`downloads/`
- 动作片段：`downloads/segments/`
- 面板产物：`out/`
- 日志：`logs/`

数据库中的文件路径通常保存为相对项目根的路径；移动整个项目目录后仍可直接使用。

## 内容理解

面板中的“内容理解”依赖 ComfyUI/Qwen3-VL。默认配置在：

```text
config/tagging.json
```

可修改 ComfyUI 地址、模型、类别表、提示词和是否自动跳过已打标视频。下载成功后，新记录会自动进入打标队列；批量结果会写回下载记录或资产记录。

## 连续动作筛选

工具页使用 ONNX Runtime、OpenCV 和 YOLOv8n-pose：

1. 逐帧提取人体关键点。
2. 比较相邻帧姿态相似度。
3. 在姿态突变处切分视频。
4. 按时长排序导出最长的若干片段。

可选配置文件为 `config/tools.json`。导出的片段会同步到数据库资产表，并在面板资产页展示。

## 开发约定

- 内部导入使用完整包路径，例如 `from clawdata.storage import store`。
- 所有新增默认路径先加入 `clawdata/core/paths.py`，不要在业务模块里散落 `"data/..."`。
- 采集、下载、存储、AI、视觉、Web 之间不要反向循环依赖。
- 修改源码后至少执行：

```bash
python -m compileall -q clawdata main.py app.py
```

## 安全与边界

- 不要提交或分享 `config/cookies.json` 和 `config/cookies_bilibili.json`。
- 自动化访问需遵守目标平台条款、版权和当地法律。
- 本项目默认只做本地素材管理和研究，不绕过付费或授权限制（B 站充电专属/付费内容不会解析出流地址）。
