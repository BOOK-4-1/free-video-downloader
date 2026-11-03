# 秒下 VideoGrab · 万能视频下载网站

> 粘贴一个链接，把全网视频存到本地。B 站、抖音、小红书、快手、微博、YouTube 等 1700+ 站点，一条链接搞定。

基于开源项目 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 搭建的轻量视频下载站：**单进程 FastAPI 后端 + 零构建纯前端**（HTML / CSS / 原生 JS），没有数据库、没有 node_modules、没有 Redis，解压即跑。

---

## 目录

- [一、项目介绍](#一项目介绍)
- [二、快速开始](#二快速开始)
- [三、使用方法](#三使用方法)
- [四、技术栈](#四技术栈)
- [五、目录结构](#五目录结构)
- [六、开源引用](#六开源引用)
- [七、已知限制](#七已知限制)
- [八、免责声明](#八免责声明)

---

## 一、项目介绍

### 它解决什么问题

想保存一个视频，通常要经历：找在线解析站 → 弹广告 → 下载下来带水印 → 清晰度还不由你选。
**秒下** 把这一串压缩成三步：粘贴链接 → 选清晰度 → 点下载。

### 功能一览

| 功能 | 说明 |
|---|---|
| **多平台解析** | 底层用 yt-dlp，自带 1700+ 站点解析器；B 站、抖音、小红书、快手、微博、西瓜、YouTube、X、Vimeo 等已做平台标识 |
| **清晰度自选** | 把平台上十几条音视频流归并成「1080P / 720P / 仅音频」这样人话档位，显示真实体积 |
| **默认 H.264** | 默认优先输出 H.264 (MP4)，Windows / 手机 / 电视 / 剪辑软件都能直接播；平台没有 H.264 时自动兜底，不会下载失败 |
| **抖音免 Cookie** | 抖音走免登录移动端通道，直接给 1080P **无水印**，不用登录、不用装插件 |
| **小红书支持** | 支持带 `xsec_token` 的完整笔记链接 + 登录 Cookie；解析失败会直接告诉你该怎么改链接 |
| **仅音频** | 一键提取 m4a，做播客 / 听书 / 背景音 |
| **合集 / 多 P 批量下载** | 粘贴合集或分 P 链接，勾选后一次提交，逐条进度条，可整批打包成 zip |
| **整段分享文案** | 抖音、小红书那种「7.94 复制打开抖音…」一长段话，直接整段粘贴，自动抠出链接 |
| **在线预览** | 下载前后都能在页面里直接播放确认，不用先落盘 |
| **Cookie 导入** | 页面右上角「导入 Cookie」，支持请求头 / Netscape cookie.txt，可先用真实链接验证再保存；只存本机浏览器 |
| **自动清理** | 临时文件 30 分钟自动删除，不占硬盘 |

### 界面

- 首页：链接解析主流程 + 功能说明 + 使用步骤 + 会员方案 + 常见问题
- 卡密兑换页：`VG-XXXX-XXXX-XXXX` 格式卡密兑换（当前为前端演示）
- 视觉：极简白底 + 单一蓝色主色 `#1777FF` + 超大圆角胶囊 + 细 ring 边框 + 大留白

---

## 二、快速开始

### 环境要求

- Python **3.10+**（开发环境为 3.13）
- Windows / macOS / Linux 均可（Windows 已实测）
- 无需 Node.js、无需数据库

### 1. 安装依赖

```bash
cd free-video-downloader

python -m venv .venv

# Windows
.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS / Linux
# ./venv/bin/pip install -r requirements.txt
```

`requirements.txt` 里已包含 `yt-dlp`。如果你更想把解析器源码放在项目里（方便手动升级某个版本），也可以下载
[yt-dlp 官方源码](https://github.com/yt-dlp/yt-dlp) 解压到项目根目录的 `yt-dlp/` 目录 —— 程序会**优先使用本地目录**，其次才用 pip 装的那份。

### 2. 准备 ffmpeg（高清合并必需）

高清档位是「视频流 + 音频流」分离的，需要用 ffmpeg 合并。程序按以下顺序查找：

1. `tools/ffmpeg/ffmpeg.exe`（项目内置）
2. 系统 `PATH` 里的 `ffmpeg`
3. `imageio-ffmpeg` 自带的二进制

任选一种即可：

```bash
# 方式一：把 ffmpeg 放进项目内置目录（Windows 示例）
#   下载 https://www.gyan.dev/ffmpeg/builds/ 的 essentials 版本
#   把 ffmpeg.exe 放到 tools/ffmpeg/ffmpeg.exe

# 方式二：直接用系统已装的 ffmpeg
ffmpeg -version

# 方式三：交给 pip 包管理
.venv/Scripts/python.exe -m pip install imageio-ffmpeg
```

> 缺 ffmpeg 也能跑，但「高分辨率视频 + 音频」的合并档位会失败；「仅音频」和平台自带的单文件档不受影响。

### 3. 启动

```bash
# Windows
.venv\Scripts\python.exe run.py --port 8000

# macOS / Linux
./.venv/bin/python run.py --port 8000
```

打开 **http://127.0.0.1:8000** 即可使用。端口被占用时会自动往后找一个空闲端口（控制台会打印实际地址）。

常用启动参数：

| 参数 | 说明 |
|---|---|
| `--port 8000` | 端口，默认 8000，也可用环境变量 `PORT` |
| `--host 127.0.0.1` | 监听地址。想让局域网其他设备访问就改 `0.0.0.0` |
| `--reload` | 开发模式，改代码自动重启 |

### 4. 可调环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FILE_TTL` | `1800` | 下载好的临时文件保留秒数（30 分钟） |
| `TASK_TIMEOUT` | `1200` | 单个任务最长下载秒数 |
| `MAX_CONCURRENT_TASKS` | `3` | 同时下载的任务数 |
| `BATCH_MAX` | `20` | 一次批量下载最多提交多少条 |
| `PARSE_TIMEOUT` | `45` | 单次解析超时秒数 |
| `PLAYLIST_MAX` | `50` | 合集最多列出多少条 |

---

## 三、使用方法

### 下载单个视频

1. 复制视频的分享链接（B 站、抖音等可以**直接粘整段分享文案**）；
2. 粘贴到首页搜索框，点「解析」；
3. 解析出标题 / 封面 / 时长 / 作者后，在档位列表里选清晰度（默认已选中推荐档）；
4. 点「下载所选清晰度」，等进度条跑完；
5. 点「保存到本地」，浏览器开始下载，文件名为视频标题。

解析结果右侧可以直接点「预览」在线播放，确认是不是要的那一条。

### 批量下载合集 / 多 P

1. 粘贴合集链接（如 `bilibili.com/list/...`）或多 P 视频链接并解析；
2. 页面会列出清单，勾选需要的条目；
3. 点「批量下载」，每条独立进度条；
4. 全部完成后可以点「打包下载 zip」，一次拿到全部文件（按 `01-标题.mp4` 编号命名）。

> 单次批量上限默认 20 条，可用 `BATCH_MAX` 调整。

### 关于 Cookie（只有部分平台需要）

| 平台 | 是否需要 | 说明 |
|---|---|---|
| 抖音 | **不需要** | 走免登录通道，直接 1080P 无水印 |
| B 站 | 可选 | 不登录一般最高 720P；导入 `SESSDATA` 后可拿 1080P / 4K |
| 小红书 | **需要** | 要登录 Cookie（`web_session`）**且**链接必须带 `xsec_token` |
| 其他站点 | 视平台 | 提示「需要 Cookie」时再导入即可 |

导入步骤：

1. 浏览器登录目标站点；
2. F12 → Network → 随便点一个请求 → 复制 `Cookie` 请求头整段；
3. 页面右上角「导入 Cookie」→ 粘贴 → （可选）附一条视频链接点「验证」；
4. 验证通过后点「保存」。

Cookie 只保存在**你本机浏览器的 localStorage**，除了发给你自己启动的这个服务，不会上传到任何第三方。

**小红书特别注意**：分享链接里的 `/explore/<id>` 单独拿出来是取不到数据的，必须带上 `?xsec_token=...`。在网页版打开笔记后直接复制地址栏完整网址即可；只粘 id 时页面会给出提示，不会让你对着「解析失败」猜原因。

### 会员 / 卡密（当前为前端演示）

- 首页「会员」区块：免费版 / 会员版 / 团队版对比；
- `/store` 卡密兑换页：格式校验 + 自动补分隔符，演示卡密 `VG-DEMO-8888-2026`，兑换后权益写入本机 localStorage。

真实校验（卡密签名、支付回调、下载权限拦截）在下一阶段接入后端。

### 接口（想自己接脚本时看）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查：yt-dlp 版本、ffmpeg 是否就绪、磁盘剩余 |
| POST | `/api/parse` | 解析链接，返回标题/封面/时长/清晰度档位/字幕 |
| POST | `/api/download` | 提交下载任务，返回 `task_id` |
| GET | `/api/tasks/{id}` | 单个任务进度（percent / speed / eta / status） |
| POST | `/api/download/batch` | 批量提交，返回 `group_id` |
| GET | `/api/tasks/group/{gid}` | 整批进度汇总 |
| GET | `/api/group/{gid}/zip` | 整批打包成 zip |
| GET | `/api/file/{id}?inline=1` | 取文件，`inline=1` 为在线预览 |
| GET | `/api/thumb?url=` | 封面代理（绕过第三方防盗链） |
| POST | `/api/cookies/verify` | 校验 Cookie：识别字段 / 用真实链接试解析 |
| GET | `/api/stats` | 站点数、已完成任务数、TTL、磁盘剩余 |

启动后也可以直接打开 **http://127.0.0.1:8000/docs** 看自动生成的接口文档。

```bash
# 解析
curl -X POST http://127.0.0.1:8000/api/parse \
  -H "Content-Type: application/json" \
  -d '{"url":"https://www.bilibili.com/video/BV1xx411c7mD"}'
```

---

## 四、技术栈

### 后端

| 技术 | 版本 | 用途 |
|---|---|---|
| [Python](https://www.python.org/) | 3.13（兼容 3.10+） | 运行时 |
| [FastAPI](https://fastapi.tiangolo.com/) | 0.162 | Web 框架、自动接口文档 |
| [Uvicorn](https://www.uvicorn.org/) | 0.54 | ASGI 服务器 |
| [Pydantic](https://docs.pydantic.dev/) | FastAPI 依赖 | 请求体校验 |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | 2026.10.26 | 核心解析 / 下载引擎 |
| [requests](https://requests.readthedocs.io/) | ≥2.32 | 抖音通道、直链下载 |
| [httpx](https://www.python-httpx.org/) | 0.28 | 封面代理（异步） |
| [FFmpeg](https://ffmpeg.org/) | 6.0 | 音视频合并、格式转换 |

### 前端

| 技术 | 说明 |
|---|---|
| 原生 HTML5 | 无模板引擎、无构建步骤，直接由 FastAPI 托管静态文件 |
| 原生 CSS | 单一 CSS 文件 + CSS 变量设计令牌，无预处理器 |
| 原生 JavaScript (ES5+) | IIFE 封装，无框架、无打包、无依赖 |
| Fetch API + 轮询 | 解析、下载、进度查询 |
| localStorage | Cookie 与会员状态仅存本机 |

### 存储与并发

| 项 | 方案 |
|---|---|
| 数据库 | **不用**。任务状态放进程内存字典，文件放 `runtime/downloads` |
| 消息队列 | **不用**。`concurrent.futures.ThreadPoolExecutor` 直接跑任务 |
| 缓存 | **不用**。封面 1 小时 HTTP 缓存头即可 |
| 过期清理 | 后台 asyncio 协程每 2 分钟扫一次，按 TTL 删文件、删任务 |

> 设计取向是「轻量、能跑、易读」：单机单进程，`python run.py` 起来就是一个完整站点。

---

## 五、目录结构

```
free-video-downloader/
├─ app/
│  ├─ config.py          # 路径、TTL、并发数、ffmpeg 定位
│  ├─ downloader.py      # yt-dlp 封装：解析、清晰度归并、错误翻译、Cookie 转换
│  ├─ douyin.py          # 抖音免登录通道（yt-dlp 的抖音提取器服务端必然 403，单独实现）
│  ├─ tasks.py           # 内存任务表 + 线程池 + 进度回调 + 过期清理
│  ├─ api.py             # /api/* 全部接口
│  └─ main.py            # FastAPI 应用、页面路由、静态资源挂载、生命周期清理
├─ web/
│  ├─ index.html         # 首页（下载主流程 + 功能 + 步骤 + 定价 + FAQ）
│  ├─ store/index.html   # 卡密兑换页
│  └─ assets/
│     ├─ css/style.css   # 全部样式与设计令牌
│     ├─ js/app.js       # 下载主流程交互
│     ├─ js/header-ui.js # Cookie 导入弹窗（两页共用）
│     ├─ js/nav.js       # 导航高亮（两页共用）
│     └─ img/favicon.svg
├─ tools/ffmpeg/         # ffmpeg.exe（可选，需自行放置）
├─ yt-dlp/               # yt-dlp 源码（可选，优先于 pip 安装的那份）
├─ runtime/              # 运行产物：下载文件、临时 Cookie（自动清理，不入库）
├─ requirements.txt
└─ run.py                # 启动入口
```

---

## 六、开源引用

本项目站在这些项目的肩膀上，特此致谢：

| 项目 | 许可证 | 本项目如何使用 |
|---|---|---|
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense（公共领域） | 核心解析与下载引擎，**不修改其源码**，仅通过 `sys.path` 引入官方 Python API |
| [FFmpeg](https://ffmpeg.org/) | LGPL / GPL（依编译选项） | 音视频合并与格式转换，以独立可执行文件方式调用 |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT | Web 框架 |
| [Starlette](https://github.com/encode/starlette) | BSD-3-Clause | ASGI 基础设施（FastAPI 依赖） |
| [Uvicorn](https://github.com/encode/uvicorn) | BSD-3-Clause | ASGI 服务器 |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | 数据校验 |
| [requests](https://github.com/psf/requests) | Apache-2.0 | HTTP 客户端 |
| [httpx](https://github.com/encode/httpx) | BSD-3-Clause | 异步 HTTP 客户端 |
| [Jinja2](https://github.com/pallets/jinja2) | BSD-3-Clause | FastAPI 生态依赖 |

界面视觉参考了 [ai.codefather.cn/painting](https://ai.codefather.cn/painting) 的极简风格（白底、单一蓝色主色、大圆角胶囊、细 ring 边框），仅作设计语言借鉴，未使用其任何代码或素材。

> 抖音解析通道参考了其移动端分享页的公开页面结构（页面内联的播放地址），不含任何逆向签名或绕过付费的内容。

---

## 七、已知限制

| 项 | 说明 |
|---|---|
| 无数据库 | 任务表在内存，服务重启后历史任务丢失；成品文件 30 分钟后清理 |
| 单机部署 | 默认 3 任务并发；未做多机任务分片、未做用户体系与限流 |
| 网络依赖 | 需要能访问目标平台。开发环境无法直连 `youtube.com`，此类链接会超时并给出中文提示 |
| 抖音 | 免登录通道偶尔需同 Session 重试（已内置重试）；私密 / 仅好友可见内容仍下不了 |
| 小红书 | 必须带 `xsec_token` 的链接 + 登录 Cookie；图文笔记本身没有视频，无法下载 |
| 付费 | 仅前端页面演示，未接真实支付 / 卡密校验 |
| 移动端 | 本期只做桌面网页版，未做移动端适配 |
| 非商用 | 见下方免责声明 |

---

## 八、免责声明

- 本项目仅供 **个人学习、技术研究与自用备份**，不得用于任何商业用途或侵权传播。
- 下载的内容版权归原作者及平台所有。请遵守目标网站的服务条款与当地法律法规，不要下载、传播无权获取的内容。
- 请合理控制请求频率。高频批量抓取可能触发平台风控甚至封号，风险由使用者自行承担。
- 使用本项目产生的任何后果由使用者自行负责。

如果这个项目对你有帮助，点个 Star 支持一下 ⭐
