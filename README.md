# airshow-reader

多播客全自动中英双语精读器（静态站发布于 https://cassette-p.github.io/airshow-reader/）。

当前已内置接入 **The Air Show**（航空商业与行业纵深）与 **Sinica Podcast**（中国议题与地缘政经深度访谈），支持通过 `podcasts.json` 零代码扩展更多播客。每当播客更新时，GitHub Actions 自动抓取 RSS 并清洗 Show Notes，调用 Gemini Paid Tier 双通道生成**中文核心摘要、关键洞察、带时间戳的精选原声（Highlights）、知识卡片（地道表达与参考书单）以及术语对齐的中英双语逐字稿**，零构建发布为支持桌面、iPad 与手机 PWA 体验的刊物级精听静态站。

---

## 核心特性

- **多播客配置驱动与全自动巡检（v3.0）**
  - 通过 `podcasts.json` 统一管理节目元信息、RSS 源、默认抓取窗口、主题重音色与专属术语表（`glossaries/<show_id>.md`）。
  - GitHub Actions 每 6 小时自动巡检并增量入库；内置四级抗 403 音频下载器、RSS Show Notes 清洗注入与说话人去头衔归一化。
- **Paid Tier 分级模型路由与增量补跑（v3.1）**
  - **通道 B（逐片转写 + 术语对齐翻译）**：180 秒音频切片锚定时间轴，默认 `gemini-3.8-flash`（低思考模式）流式转写，防止长音频时间漂移。
  - **通道 A（全局摘要 + Highlights + 知识卡片）**：默认旗舰模型池（`gemini-4-carbon` / `gemini-4-neon`）支持思考等级自动适配与平滑降级，一次输出摘要、要点、4–10 条 Highlights、6–10 条地道表达（`vocab_notes`）与参考资源（`references`，自动过滤商业广告）。
  - **补跑通道（`--enrich`）**：支持对存量单集按需低成本补齐知识卡片，既有摘要与逐字稿字节级不变。
- **三级懒加载与 PWA 原生体验（v3.3）**
  - 首屏仅拉取 `shows.json` 与当前节目的轻量 `index.json`，选中单集后按需懒加载单集完整 JSON；音频直链原站 MP3。
  - 内置 `manifest.json` 与 `sw.js`（`./data/**` 与音频流严格走 Network-Only，保障新单集零延迟可见），深度适配 iOS/iPadOS Safari 液态玻璃透传与安全区。
- **刊物级视觉与主动精听套件（M7 / M8-A）**
  - **双模排印**：浅色暖纸（`#FAF8F4`）× 深色暖炭黑（`#151412`），`Newsreader` 西文衬线与 `Noto Sans SC` 复合字栈，可折叠知识卡片与 8 组确定性哈希嘉宾色标。
  - **进度与锁屏控制**：进度条章节打点、`localStorage` 断点续播记忆、已听进度标识与 Media Session 锁屏播放控制。
  - **精听训练**：三档“刮刮乐”盲听模式（清晰 / 遮中文 / 全盲听，点击单段刮开显形）、单句区间自动复读、URL Hash 直达书签，以及全套键盘快捷键（`Space` 播停、`←/→` ±5s、`Shift+←/→` ±15s、`R` 重听本段、`L` 单句复读、`B` 切换盲听、`/` 或 `⌘K` 搜索、`?` 帮助、`Esc` 退出）。

---

## 目录结构

```text
airshow-reader/
├── README.md                             # 项目说明与运维指南（本文件）
├── SPEC.md                               # v3.3 技术规范、状态机、数据契约与前端规范
├── ITERATION.md                          # 迭代里程碑总览与验收记录
├── podcasts.json                         # 多播客核心配置注册表
├── glossaries/                           # 各播客专属主播名单与术语校对表
│   ├── airshow.md
│   └── sinica.md
├── requirements.txt                      # Python 依赖
├── scripts/
│   └── process.py                        # RSS 抓取、下载切片、双通道 AI、增量补跑与原子落盘
├── docs/                                 # GitHub Pages 发布根目录
│   ├── index.html                        # 单文件零构建静态双语阅读器（v3.3）
│   ├── manifest.json                     # PWA 应用清单
│   ├── sw.js                             # PWA Service Worker（数据与媒体 Network-Only）
│   ├── favicon.svg                       # 「声波书页」矢量站点图标
│   ├── artwork-airshow-512.png           # The Air Show 节目封面
│   ├── artwork-sinica-512.png            # Sinica Podcast 节目封面
│   ├── icons/                            # PWA 桌面与锁屏标准图标（192 / 512）
│   └── data/
│       ├── shows.json                    # 全局播客清单
│       └── <show_id>/
│           ├── index.json                # 单节目轻量目录与核心摘要
│           └── episodes/
│               └── <safe_guid>.json      # 单集完整精读、知识卡片与双语逐字稿
└── .github/
    └── workflows/
        └── update.yml                    # 定时巡检、手动补抓与 enrich_only 补跑工作流
```

## 本地使用指南

### 1. 环境准备

- Python `3.11+`

- 已安装 `ffmpeg` 与 `ffprobe`（Windows 可运行 `winget install Gyan.FFmpeg`）

- 安装依赖：

  Bash

  ```
  pip install -r requirements.txt
  ```

### 2. 环境变量配置

PowerShell

```
# 必填（--dry-run 与 --migrate-only 除外）
$env:GEMINI_API_KEY = "你的_Gemini_API_Key"

# 可选覆盖项
# $env:GEMINI_MODEL_CH_B = "gemini-3.8-flash"
# $env:GEMINI_THINKING_CH_B = "low"
# $env:GEMINI_MODEL_CH_A = "gemini-4-carbon"
# $env:GEMINI_THINKING_CH_A = "high"      # 留空则按模型自动适配并平滑降级
# $env:GEMINI_MODEL_ENRICH = "gemini-3.8-flash"
# $env:GEMINI_THINKING_ENRICH = "low"
# $env:CHUNK_SLEEP_SEC = "2.0"
```

### 3. 常用命令

Bash

```
# 1) 零 API 消耗演习（测试 RSS、Show Notes 清洗、下载与 180s 切片）
python scripts/process.py --show sinica --dry-run --limit 1

# 2) 单切片冒烟预览（仅输出 _smoke_preview.json，不修改正式库）
python scripts/process.py --show airshow --limit 1 --max-chunks 1

# 3) 处理全部已启用播客的最新 1 期未入库单集
python scripts/process.py --show all --limit 1

# 4) 仅对存量缺失知识卡片的单集执行增量补跑（--limit 0 表示全量扫描）
python scripts/process.py --show all --enrich --limit 0

# 5) 演习查看待补跑清单（不调 API、不写盘）
python scripts/process.py --show all --enrich --limit 0 --dry-run

# 6) 仅执行 v2→v3 目录迁移与新节目初始化
python scripts/process.py --migrate-only

# 7) 启动本地预览服务（访问 [http://127.0.0.1:8000](http://127.0.0.1:8000)）
python -m http.server 8000 --directory docs
```

## 云端自动化与运维

### GitHub Secrets 与 Variables

在仓库 **Settings → Secrets and variables → Actions** 中配置：

| **类型**     | **名称**                 | **必填** | **说明**                                                             |
| ------------ | ------------------------ | -------- | -------------------------------------------------------------------- |
| **Secret**   | `GEMINI_API_KEY`         | 是       | Google AI Studio API Key（建议绑定 Paid Tier）                       |
| **Variable** | `GEMINI_MODEL_CH_A`      | 否       | 置顶覆盖通道 A 模型（默认 `gemini-4-carbon`）                        |
| **Variable** | `GEMINI_THINKING_CH_A`   | 否       | 通道 A 思考等级（留空自动适配：`4-neon`→`xhigh`、`4-carbon`→`high`） |
| **Variable** | `GEMINI_MODEL_CH_B`      | 否       | 置顶覆盖通道 B 转写模型（默认 `gemini-3.8-flash`）                   |
| **Variable** | `GEMINI_THINKING_CH_B`   | 否       | 通道 B 思考等级（默认 `low`）                                        |
| **Variable** | `GEMINI_MODEL_ENRICH`    | 否       | 置顶覆盖增量补跑模型（默认 `gemini-3.8-flash`）                      |
| **Variable** | `GEMINI_THINKING_ENRICH` | 否       | 增量补跑思考等级（默认 `low`）                                       |
| **Variable** | `CHUNK_SLEEP_SEC`        | 否       | 切片间休眠秒数（默认 `2.0`）                                         |

### 手动触发工作流

进入 **Actions → Scheduled Update and Ingestion → Run workflow**：

- **补抓往期单集**：选择 `show`（具体 ID 或 `all`），设置 `limit`（建议 `1`–`3`），将 `max_age_days` 设为 **`0`**。

- **增量补跑知识卡片**：开启 `enrich_only: true`，配合 `show` 与 `limit` 仅补齐缺失的 `vocab_notes` / `references`。

### 零代码调教与扩展

- **修正人名与术语**：直接编辑 `glossaries/<show_id>.md`，下期新单集处理或补跑时自动注入生效。

- **接入新播客**：在 `podcasts.json` 新增配置并添加对应 `glossaries/<new_show>.md`，运行 `--migrate-only` 或手动触发工作流完成初始化。

> **本地开发提醒**：GitHub Actions 会持续向 `main` 分支自动提交新单集数据，本地修改前请先执行 `git pull origin main`。