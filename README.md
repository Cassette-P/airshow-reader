# airshow-reader

*The Air Show*（Jon Ostrower、Brian Sumers、Brett Snyder 主持的民航商业播客）个人全自动双语精读器。

每当播客更新时，通过 GitHub Actions 自动抓取 RSS 新单集，调用 Gemini API 生成**中文核心摘要、核心要点、带时间戳的 Highlights 以及航空术语对齐的中英双语逐字稿**，并发布为电脑与手机自适应的静态网页，支持点击任意 Highlight 或双语段落精准跳转原音频播放。

---

## 核心特性

- **零服务器全自动更新**：GitHub Actions 每 6 小时自动巡检 Buzzsprout RSS，仅拉取近 7 天内未入库的新单集并自动提交，GitHub Pages 即刻更新。
- **双通道分级模型路由（v2.0）**：
  - **通道 B（转写 + 术语对齐翻译）**：音频按 180 秒切片锚定时间轴，默认使用 `gemini-3.8-flash`（低思考模式）流式转写并区分三位常驻主播声纹，杜绝长音频时间戳漂移。
  - **通道 A（深度摘要 + Highlights）**：将完整带时间戳纯文本逐字稿单次发送给旗舰模型（默认 `gemini-4-carbon`，可通过仓库变量一键切换 `gemini-4-neon`），提炼行业洞察。
- **按单集懒加载架构（v2.0）**：总目录 `docs/data/index.json` 与分集正文 `docs/data/episodes/<guid>.json` 彻底分离，即便归档上百期节目，手机端首屏依然毫秒级打开；音频直接外链原地址，不向仓库提交 MP3。
- **航空财经杂志风阅读体验**：单文件零构建 `docs/index.html`，支持深浅色切换、双语/仅中/仅英模式、实时关键词检索高亮、悬浮音频条倍速控制与二分查找同步滚动高亮。

---

## 目录结构

```text
airshow-reader/
├── README.md                        # 项目说明（本文件）
├── SPEC.md                          # v2.0 架构规范、状态机与 JSON 数据契约
├── GLOSSARY.md                      # 常驻主播声纹映射与航空专业术语表（运行时注入）
├── ITERATION.md                     # 文档驱动开发里程碑与验收记录
├── requirements.txt                 # Python 依赖
├── scripts/
│   └── process.py                   # RSS 抓取、抗 403 下载、切片、双通道 AI 与原子落盘脚本
├── docs/                            # GitHub Pages 发布根目录
│   ├── index.html                   # 自适应静态双语阅读器
│   └── data/
│       ├── index.json               # 全量单集轻量总目录与核心摘要
│       └── episodes/
│           └── <safe_guid>.json     # 单集完整 Highlights 与双语逐字稿
└── .github/
    └── workflows/
        └── update.yml               # 每 6 小时定时巡检 + 手动补抓工作流
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

# 可选：临时覆盖通道模型或片间休眠（秒）
# $env:GEMINI_MODEL_CH_B = "gemini-3.8-flash"
# $env:GEMINI_MODEL_CH_A = "gemini-4-neon"
# $env:CHUNK_SLEEP_SEC = "2"
```

### 3. 常用命令
```bash
# 1) 零 API 消耗演习模式（测试 RSS、音频下载与 180s 切片）
python scripts/process.py --dry-run --limit 1

# 2) 单切片冒烟测试（结果仅写 docs/data/_smoke_preview.json，不污染正式库）
python scripts/process.py --limit 1 --max-chunks 1

# 3) 处理最新 1 期未入库单集并正式落盘
python scripts/process.py --limit 1

# 4) 仅处理最近 7 天内发布的新单集（定时任务模式）
python scripts/process.py --limit 1 --max-age-days 7

# 5) 启动本地预览服务器（访问 http://127.0.0.1:8000）
python -m http.server 8000 --directory docs
```

---

## 云端自动化与运维

### GitHub Secrets 与 Variables 配置
在仓库 **Settings → Secrets and variables → Actions** 中配置：

| 类型 | 名称 | 必填 | 说明 |
| :--- | :--- | :---: | :--- |
| **Secret** | `GEMINI_API_KEY` | 是 | Google AI Studio API Key（建议绑定 Paid Tier 赠金额度） |
| **Variable** | `GEMINI_MODEL_CH_A` | 否 | 置顶覆盖通道 A 摘要模型（如 12/1 优惠结束后设为 `gemini-4-neon` 即可零代码切换） |
| **Variable** | `GEMINI_MODEL_CH_B` | 否 | 置顶覆盖通道 B 转写模型（默认 `gemini-3.8-flash`） |
| **Variable** | `GEMINI_THINKING_CH_B` | 否 | 通道 B 思考等级（默认 `low`） |
| **Variable** | `CHUNK_SLEEP_SEC` | 否 | 切片间休眠秒数（默认 `2`） |

### 手动补抓往期单集
进入仓库 **Actions → Scheduled Update and Ingestion → Run workflow**：
- `limit`：本次想补抓的集数（建议单次 `1`–`3`）
- `max_age_days`：改为 **`0`**（关闭 7 天时间窗限制）
- `dry_run`：保持 `false`

### 零代码调教翻译质量
无需改动任何脚本，直接在 GitHub 网页端编辑根目录的 `GLOSSARY.md`（补充航空缩写、机型纠正或人名规范）并保存，下一期新单集处理时将自动生效。

> **本地修改前提醒**：由于 GitHub Actions 会自动向 `main` 分支提交新单集数据，每次在本地开发修改前请先执行 `git pull origin main` 同步最新数据。