# airshow-reader

多播客全自动中英双语精读器（静态站发布于 [https://cassette-p.github.io/airshow-reader/](https://cassette-p.github.io/airshow-reader/)）。

当前已内置接入 **The Air Show**（航空商业与行业纵深）与 **Sinica Podcast**（中国议题与地缘政经深度访谈），并支持通过 `podcasts.json` 零代码扩展更多播客。每当播客更新时，GitHub Actions 自动抓取 RSS 新单集并清洗 Show Notes，调用 Gemini Paid Tier 双通道生成**中文核心摘要、关键洞察、带时间戳的精选原声（Highlights）以及术语对齐的中英双语逐字稿**，发布为电脑、iPad 与手机自适应的刊物级双语研读静态站。

---

## 核心特性

- **多播客配置驱动与零服务器巡检（v3.0）**：
  - 通过 `podcasts.json` 统一注册播客元信息、RSS 源、主题重音色与专属术语表路径（`glossaries/<show_id>.md`）。
  - GitHub Actions 每 6 小时自动巡检全部已启用播客，按时间窗口增量抓取新单集并自动提交，GitHub Pages 即刻生效；内置四级抗 403 音频下载器与 RSS Show Notes 清洗注入，准确识别常驻主播与流动嘉宾。
- **双通道分级模型路由（Paid Tier）**：
  - **通道 B（逐片转写 + 术语对齐翻译）**：音频按 180 秒切片锚定时间轴，默认通过 `gemini-3.8-flash`（低思考模式）流式转写并区分说话人，杜绝长音频时间戳漂移。
  - **通道 A（全局摘要 + 4–10 条 Highlights）**：将完整带时间戳逐字稿单次发给旗舰模型（默认 `gemini-4-carbon`，支持环境变量一键切换）提炼深度洞察。
- **三级按需懒加载架构**：
  - 首屏仅拉取全局清单 `docs/data/shows.json` 与当前节目的轻量目录 `docs/data/<show_id>/index.json`；点击单集后再按需懒加载 `docs/data/<show_id>/episodes/<safe_guid>.json`。
  - 前端内置两级内存缓存（`showIndexCache` / `episodeCache`）与请求序号防竞态锁，音频直链原站 MP3，不向仓库提交音频文件。
- **刊物级双语阅读与液态玻璃交互（v3.2）**：
  - **白天暖纸杂志 × 夜间暖黑夜读**：浅色采用暖米纸底（`#FAF8F4`），深色采用低光晕暖炭黑（`#151412`）与去荧光节目重音色；西文衬线（`Newsreader`）与中文无衬线（`Noto Sans SC` 可变字重）复合排印。
  - **去卡片化流畅阅读**：双栏线章节头、Standfirst 导语块、折页便签风 Highlights 与无边框连续纸面逐字稿流，配备 8 组确定性哈希嘉宾印章色标与动态五等分声学微刻度标尺。
  - **全端自适应与原生 Safari 玻璃透传**：支持 `320px` 极限手机屏、`744px` iPad Mini 平板与 `1440px` 桌面宽屏零溢出；顶栏与超薄单行底部播放胶囊采用悬浮液态玻璃材质，适配 iOS/iPadOS 27 Safari 状态栏与地址胶囊透传。
  - **效率研读控件**：Issue Archive 侧滑选集抽屉、双语/仅中/仅英切换、实时双语关键词过滤与 `<mark>` 高亮、点击段落精准跳播（`seekAndPlay`）、$O(\log N)$ 二分同步滚动高亮、倍速调节（`1.0x–2.0x`），以及快捷键盘操作（`/` 或 `⌘K` 搜索、`Esc` 退出/收起、`Space` 播放/暂停）。

---

## 目录结构

```text
airshow-reader/
├── README.md                             # 项目说明与使用指南（本文件）
├── SPEC.md                               # v3.2 技术规范、状态机、数据契约与前端视觉规范
├── ITERATION.md                          # 文档驱动开发里程碑总览与活跃工单
├── podcasts.json                         # 多播客核心配置注册表
├── glossaries/                           # 各播客专属主播名单与术语校对表（运行时注入）
│   ├── airshow.md
│   └── sinica.md
├── requirements.txt                      # Python 依赖
├── scripts/
│   └── process.py                        # 多播客 RSS 抓取、下载、切片、双通道 AI 与原子落盘脚本
├── docs/                                 # GitHub Pages 发布根目录
│   ├── index.html                        # 单文件零构建静态双语阅读器（HTML + CSS + Vanilla JS）
│   ├── favicon.svg                       # 「声波书页」矢量站点图标
│   └── data/
│       ├── shows.json                    # 全局播客总清单（供前端渲染节目切换与主题色）
│       └── <show_id>/                    # 按节目隔离的数据目录（如 airshow/、sinica/）
│           ├── index.json                # 单节目轻量总目录与核心摘要
│           └── episodes/
│               └── <safe_guid>.json      # 单集完整 Highlights 与双语逐字稿
└── .github/
    └── workflows/
        └── update.yml                    # 每 6 小时定时巡检 + 手动多节目补抓工作流
```

---

## 本地使用指南

### 1. 环境准备
- Python `3.11+`
- 系统已安装 `ffmpeg` 与 `ffprobe`（Windows 可通过 `winget install Gyan.FFmpeg` 安装）
- 安装 Python 依赖：
  ```bash
  pip install -r requirements.txt
  ```

### 2. 配置环境变量
```powershell
# PowerShell 示例
$env:GEMINI_API_KEY = "你的_Gemini_API_Key"

# 可选：临时覆盖通道模型、思考等级或片间休眠（秒）
# $env:GEMINI_MODEL_CH_B = "gemini-3.8-flash"
# $env:GEMINI_MODEL_CH_A = "gemini-4-neon"
# $env:GEMINI_THINKING_CH_B = "low"
# $env:CHUNK_SLEEP_SEC = "2"
```

### 3. 常用命令
```bash
# 1) 零 API 消耗演习模式（测试指定节目的 RSS、Show Notes 清洗、下载与 180s 切片）
python scripts/process.py --show sinica --dry-run --limit 1

# 2) 单切片冒烟测试（结果仅写 docs/data/<show_id>/_smoke_preview.json，不污染正式库）
python scripts/process.py --show airshow --limit 1 --max-chunks 1

# 3) 处理全部已启用播客的最新 1 期未入库单集并正式落盘
python scripts/process.py --show all --limit 1

# 4) 仅处理指定播客近 7 天内发布的新单集（定时任务模式）
python scripts/process.py --show airshow --limit 1 --max-age-days 7

# 5) 启动本地静态预览服务器（访问 http://127.0.0.1:8000）
python -m http.server 8000 --directory docs
```

---

## 云端自动化与运维

### GitHub Secrets 与 Variables 配置
在仓库 **Settings → Secrets and variables → Actions** 中配置：

| 类型 | 名称 | 必填 | 说明 |
| :--- | :--- | :---: | :--- |
| **Secret** | `GEMINI_API_KEY` | 是 | Google AI Studio API Key（建议绑定 Paid Tier 额度） |
| **Variable** | `GEMINI_MODEL_CH_A` | 否 | 置顶覆盖通道 A 摘要模型（默认 `gemini-4-carbon`，支持零代码切换） |
| **Variable** | `GEMINI_MODEL_CH_B` | 否 | 置顶覆盖通道 B 转写模型（默认 `gemini-3.8-flash`） |
| **Variable** | `GEMINI_THINKING_CH_B` | 否 | 通道 B 思考等级（默认 `low`） |
| **Variable** | `CHUNK_SLEEP_SEC` | 否 | 切片间休眠秒数（默认 `2.0`） |

### 手动补抓往期单集
进入仓库 **Actions → Scheduled Update and Ingestion → Run workflow**：
- `show`：选择目标播客 ID（如 `airshow`、`sinica`）或 `all`
- `limit`：本次补抓集数（建议单次 `1`–`3`）
- `max_age_days`：改为 **`0`**（关闭默认 7 天时间窗限制）
- `dry_run`：保持 `false`

### 零代码调教翻译与新增播客
- **调教术语与人名**：直接在 GitHub 网页端编辑 `glossaries/<show_id>.md` 并保存，下一期新单集处理时自动注入生效。
- **新增播客节目**：在 `podcasts.json` 中追加节目配置、新建对应 `glossaries/<new_show>.md`，运行一次 `--migrate-only` 或手动触发工作流即可完成目录初始化与前端接入。

> **本地修改前提醒**：由于 GitHub Actions 会自动向 `main` 分支提交新单集数据，每次在本地开发修改前请先执行 `git pull origin main` 同步最新状态。