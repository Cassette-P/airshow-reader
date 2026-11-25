# airshow-reader

多播客全自动中英双语精读器（静态站发布于 https://cassette-p.github.io/airshow-reader/）。

当前已内置接入 **The Air Show**（航空商业与行业纵深）、**Sinica Podcast**（中国议题与地缘政经深度访谈）与 **This American Life**（传奇非虚构叙事特稿与分幕导读），支持通过 `podcasts.json` 零代码扩展更多播客。每当播客更新时，GitHub Actions 自动抓取 RSS 并清洗 Show Notes，调用 Gemini Paid Tier 双通道生成**中文核心摘要、关键洞察、带时间戳的精选原声（Highlights）、知识卡片（地道表达与参考资源）以及术语对齐的中英双语逐字稿**，经 `scripts/validate_data.py` 自动化契约校验后，零构建发布为支持桌面、iPad 与手机 PWA 体验的刊物级精听静态站。

---

## 核心特性

- **多播客配置驱动与全自动巡检（v3.0）**
  - 通过 `podcasts.json` 统一管理节目元信息、RSS 源、默认抓取窗口、主题重音色与专属术语表（`glossaries/<show_id>.md`）。
  - GitHub Actions 每 6 小时自动巡检并增量入库；内置四级抗 403 音频下载器、RSS Show Notes 结构化清洗注入与说话人去头衔归一化。
- **Paid Tier 分级模型路由、断点缓存与双模传输（v3.5）**
  - **通道 B（逐片转写 + 术语对齐翻译）**：180 秒音频切片锚定时间轴，默认 `gemini-3.8-flash`（低思考模式）流式转写，完成后原子落盘断点缓存至 `.cache/ch_b/`（支持 `--no-cache` 强制重跑），防止长音频时间漂移与重复消耗。
  - **通道 A（全局摘要 + Highlights + 知识卡片）**：默认锁定 `gemini-4-carbon @ high`（回退 `gemini-4-neon @ high`、`gemini-3.8-flash @ high`），主路径走 Interactions API `background=True` 异步轮询（超时默认 `900s`，可由 `CH_A_BG_TIMEOUT_SEC` 覆盖），不可用时零耗时回退 `stream_with_thoughts` 心跳保活；一体生成摘要、要点、4–10 条 Highlights、6–10 条地道表达（`vocab_notes`）与参考资源（`references`，自动过滤商业赞助与广告口播）。
  - **补跑通道（`--enrich`）**：支持对存量单集按需低成本补齐知识卡片，既有摘要与逐字稿字节级不变。
- **严格 Schema 契约校验与 CI 门禁（`scripts/validate_data.py --strict`）**
  - 内置基于 JSON Schema 与业务完整性的双层自动化校验器。
  - 严格校验 `shows.json`、`index.json` 与各单集 JSON 间的元数据引用一致性、时间戳单调性、字段范围钳制与分类合法性，已以 `--strict` 模式集成进 GitHub Actions 工作流作为发布前的阻断式门禁。
- **零构建原生模块化架构与 PWA 体验（v3.3）**
  - **Zero-Build 原生解耦**：前端彻底告别单文件堆砌，分为语义 HTML 骨架、3 个独立原生 CSS 与 10 个 ES 模块，无 npm、无 Webpack/Vite 打包依赖，直接被浏览器原生解析运行。
  - **三级懒加载**：首屏仅拉取 `shows.json` 与当前节目的轻量 `index.json`，选中单集后按需懒加载单集完整 JSON；音频直链原站 MP3。
  - **PWA 原生化**：内置 `manifest.json` 与 `sw.js`（`./data/**` 与音频流严格走 Network-Only，保障新单集零延迟可见），采用 `airshow-reader-v3` 静态缓存策略，深度适配 iOS/iPadOS Safari 液态玻璃透传与安全区。
- **刊物级视觉与主动精听套件（M7 / M8-A）**
  - **双模排印**：浅色暖纸（`#FAF8F4`）× 深色暖炭黑（`#151412`），`Newsreader` 西文衬线与 `Noto Sans SC` 复合字栈，可折叠知识卡片与 8 组确定性哈希嘉宾色标。
  - **进度与锁屏控制**：进度条章节打点、`localStorage` 断点续播记忆、选集抽屉已听进度标，以及支持节目专属 512px 封面与 10s 步长的 Media Session 锁屏播放控制。
  - **精听训练**：三档“刮刮乐”盲听模式（清晰 / 遮中文 / 全盲听，点击单段刮开显形且不跳播）、单句区间自动复读与状态胶囊、URL Hash 直达书签，以及全套键盘快捷键（`Space` 播停、`←/→` ±5s、`Shift+←/→` ±15s、`R` 重听本段、`L` 单句复读、`B` 切换盲听、`/` 或 `⌘K` 搜索、`?` 帮助、`Esc` 退出）。

---

## 目录结构

```text
airshow-reader/
├── README.md                             # 项目说明与运维指南（本文件）
├── SPEC.md                               # v3.3 技术规范、状态机、数据契约与前端规范
├── ITERATION.md                          # 迭代里程碑总览与验收记录
├── podcasts.json                         # 多播客核心配置注册表
├── glossaries/                           # 各播客专属常驻主播名单与术语校对表
│   ├── airshow.md
│   ├── sinica.md
│   └── tal.md
├── requirements.txt                      # Python 依赖
├── tests/                                # 稳定性加固单元测试（断点缓存、瘦身与断连降级）
│   ├── __init__.py
│   └── test_stability.py
├── scripts/
│   ├── process.py                        # 主处理命令行入口（参数解析与流程调度）
│   ├── validate_data.py                  # 数据契约与内容完整性校验门禁
│   └── pipeline/                         # 后端流水线解耦模块
│       ├── __init__.py
│       ├── config.py                     # 全局常量、目录定义与环境变量加载
│       ├── models.py                     # Pydantic 数据契约与响应模型定义
│       ├── storage.py                    # JSON 文件原子读写、缓存与 Shows 清单同步
│       ├── audio.py                      # 抗 403 下载、ffprobe 检测与 180s 切片
│       ├── gemini.py                     # SDK 初始化、CH-A Interactions 异步轮询与双模容灾
│       ├── prompts.py                    # 通道 A/B 与 Enrich 专用 Prompt 模板生成
│       ├── hooks_tal.py                  # TAL 专属钩子（期号提取、无广告音频与花名册）
│       ├── rss.py                        # RSS 抓取解析与 Show Notes 结构化清洗
│       ├── runner.py                     # 单集全流程驱动、通道 A/B 调度与补跑引擎
│       └── migrate.py                    # v2→v3 数据目录幂等迁移与新节目脚手架
├── docs/                                 # GitHub Pages 静态发布根目录
│   ├── index.html                        # 语义化骨架（按序引用 CSS 与 ES Modules 入口）
│   ├── manifest.json                     # PWA 应用清单（独立显示模式）
│   ├── sw.js                             # PWA Service Worker（airshow-reader-v3 缓存）
│   ├── favicon.svg                       # 「声波书页」矢量站点图标
│   ├── artwork-airshow-512.png           # The Air Show 专属 512px 专辑封面
│   ├── artwork-sinica-512.png            # Sinica Podcast 专属 512px 专辑封面
│   ├── artwork-tal-512.png               # This American Life 专属 512px 专辑封面
│   ├── icons/                            # PWA 桌面通用标准图标
│   │   ├── icon-192.png
│   │   └── icon-512.png
│   ├── css/                              # 原生 CSS 样式分层架构（Zero-Build）
│   │   ├── tokens.css                    # 主题色彩变量、字栈、品牌重音色
│   │   ├── layout.css                    # 液态玻璃顶栏/底栏、PWA 安全区、响应式布局
│   │   └── components.css                # 刊物排版、知识卡片、逐字稿、盲听、抽屉与弹窗
│   ├── js/                               # 原生 ES Modules 业务模块
│   │   ├── main.js                       # 启动引导、SW 注册、URL Hash 路由监听
│   │   ├── state.js                      # 共享运行时状态、DOM 引用与防竞态锁
│   │   ├── storage.js                    # localStorage 播放记忆、盲听档位读写
│   │   ├── api.js                        # shows / index / episode 三级懒加载与缓存
│   │   ├── theme.js                      # 浅/深主题切换、Favicon 动态同步、PWA 探测
│   │   ├── player.js                     # 音频播放、章节打点、单句复读、Media Session
│   │   ├── reader.js                     # 摘要/要点/卡片/逐字稿渲染、搜索高亮、刮刮乐
│   │   ├── archive.js                    # 节目 Tab、选集抽屉渲染与已听进度标记
│   │   ├── shortcuts.js                  # 全局键盘快捷键与帮助面板
│   │   └── utils.js                      # escapeHtml、时间格式化、二分查找、嘉宾哈希
│   └── data/
│       ├── shows.json                    # 全局播客清单与单集计数
│       └── <show_id>/
│           ├── index.json                # 单节目轻量目录与核心摘要
│           └── episodes/
│               └── <safe_guid>.json      # 单集完整精读、知识卡片与双语逐字稿
└── .github/
    └── workflows/
        └── update.yml                    # 定时巡检、Schema 校验与手动触发工作流
```

---

## 本地使用指南

### 1. 环境准备

- Python `3.11+`
- 已安装 `ffmpeg` 与 `ffprobe`（Windows 可运行 `winget install Gyan.FFmpeg`，macOS 运行 `brew install ffmpeg`）
- 安装依赖：

```bash
pip install -r requirements.txt
```

### 2. 环境变量配置

```powershell
# 必填（--dry-run、--migrate-only 与 validate_data.py 除外）
$env:GEMINI_API_KEY = "你的_Gemini_API_Key"

# 可选覆盖项
# $env:GEMINI_MODEL_CH_B = "gemini-3.8-flash"    # 亦向后兼容 GEMINI_MODEL
# $env:GEMINI_THINKING_CH_B = "low"
# $env:GEMINI_MODEL_CH_A = "gemini-4-carbon"
# $env:GEMINI_THINKING_CH_A = "high"             # 留空默认 Carbon/Neon/Flash 均为 high
# $env:CH_A_BG_TIMEOUT_SEC = "900"               # CH-A Interactions 异步轮询超时秒数
# $env:GEMINI_MODEL_ENRICH = "gemini-3.8-flash"
# $env:GEMINI_THINKING_ENRICH = "low"
# $env:CHUNK_SLEEP_SEC = "2.0"
```

### 3. 常用命令

```bash
# 1) 零 API 消耗演习（测试 RSS、Show Notes 清洗、下载与 180s 切片）
python scripts/process.py --show sinica --dry-run --limit 1

# 2) 单切片冒烟预览（仅输出 _smoke_preview.json，不修改正式库）
python scripts/process.py --show airshow --limit 1 --max-chunks 1

# 3) 处理全部已启用播客的最新 1 期未入库单集（可加 --no-cache 忽略 CH-B 断点缓存重跑）
python scripts/process.py --show all --limit 1

# 4) 仅对存量缺失知识卡片的单集执行增量补跑（--limit 0 表示全量扫描）
python scripts/process.py --show all --enrich --limit 0

# 5) 演习查看待补跑清单（不调 API、不写盘）
python scripts/process.py --show all --enrich --limit 0 --dry-run

# 6) 仅执行 v2→v3 目录迁移与新节目初始化
python scripts/process.py --migrate-only

# 7) 运行本地严格数据契约与 Schema 完整性校验门禁（与 CI 一致）
python scripts/validate_data.py --strict

# 8) 运行后端稳定性单元测试
python -m unittest discover -s tests -v

# 9) 启动本地静态预览服务（访问 http://127.0.0.1:8000）
python -m http.server 8000 --directory docs
```

---

## 云端自动化与运维

### GitHub Secrets 与 Variables

在仓库 **Settings → Secrets and variables → Actions** 中配置：

| **类型** | **名称** | **必填** | **说明** |
| :--- | :--- | :--- | :--- |
| **Secret** | `GEMINI_API_KEY` | 是 | Google AI Studio API Key（建议绑定 Paid Tier） |
| **Variable** | `GEMINI_MODEL_CH_A` | 否 | 置顶覆盖通道 A 模型（默认 `gemini-4-carbon`） |
| **Variable** | `GEMINI_THINKING_CH_A` | 否 | 通道 A 思考等级（留空默认 `carbon`/`neon`/`flash` 均为 `high`） |
| **Variable** | `CH_A_BG_TIMEOUT_SEC` | 否 | 通道 A Interactions 后台轮询超时秒数（默认 `900`） |
| **Variable** | `GEMINI_MODEL_CH_B` | 否 | 置顶覆盖通道 B 转写模型（默认 `gemini-3.8-flash`） |
| **Variable** | `GEMINI_THINKING_CH_B` | 否 | 通道 B 思考等级（默认 `low`） |
| **Variable** | `GEMINI_MODEL_ENRICH` | 否 | 置顶覆盖增量补跑模型（默认 `gemini-3.8-flash`） |
| **Variable** | `GEMINI_THINKING_ENRICH` | 否 | 增量补跑思考等级（默认 `low`） |
| **Variable** | `CHUNK_SLEEP_SEC` | 否 | 切片间休眠秒数（默认 `2.0`） |

### 手动触发工作流

进入 **Actions → Scheduled Update and Ingestion → Run workflow**：

- **补抓往期单集**：选择 `show`（具体 ID 如 `airshow`、`sinica`、`tal` 或 `all`），设置 `limit`（建议 `1`–`3`），将 `max_age_days` 设为 **`0`**（不限制发布天数；留空则自动按 `podcasts.json` 各节目默认窗口）。
- **增量补跑知识卡片**：勾选 `enrich_only: true`，配合 `show` 与 `limit` 仅对缺失 `vocab_notes` / `references` 的单集补跑。
- **演习运行**：勾选 `dry_run: true`，测试抓取解析与下载切片逻辑，不消耗 API 配额。

### 零代码调教与扩展

- **修正人名与术语**：直接编辑 `glossaries/<show_id>.md`，下期新单集处理或补跑时自动注入生效。
- **接入新播客**：在 `podcasts.json` 新增配置并添加对应 `glossaries/<new_show>.md`，运行 `--migrate-only` 或手动触发工作流完成目录脚手架初始化。

> **本地开发提醒**：GitHub Actions 会持续向 `main` 分支自动提交新单集数据，本地修改前请先执行 `git pull origin main`。
