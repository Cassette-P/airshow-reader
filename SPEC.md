# airshow-reader 技术规范与数据契约（SPEC.md · v3.2）

> 数据契约严格保持 **v3.0 稳定版**（向后完全兼容）；前端视觉体系与交互规范已演进至 **v3.2**。

## 1. 项目定位与目录结构

个人多播客全自动双语精读静态站，支持多播客配置驱动与全自动化处理。抓取播客 RSS，提取嘉宾与章节 Show Notes，生成中文核心摘要、带时间戳 Highlight 与中英对照逐字稿，通过 GitHub Pages（`/docs`）零构建发布。

- `README.md`：项目简介、本地使用与云端运维指南
- `SPEC.md`：核心架构、状态机、v3.0 数据契约与 v3.2 前端规范（本文件）
- `podcasts.json`：多播客核心配置注册表（包含各节目 ID、标题、RSS、术语表路径、主题重音色与默认时效窗口）
- `glossaries/<show_id>.md`：各播客专属常驻主播名单、行业术语表与翻译校对红线（如 `glossaries/airshow.md`、`glossaries/sinica.md`）
- `ITERATION.md`：迭代里程碑总览、当前活跃工单与交接记录
- `requirements.txt`：Python 依赖
- `.gitignore`：本地缓存与临时预览文件忽略规则（覆盖 `docs/data/**/_smoke_preview.json` 与 `docs/data/**/*.tmp`）
- `scripts/process.py`：主处理引擎（支持 `--show` 参数调度、v2→v3 幂等数据迁移、Show Notes 动态清洗注入与 Paid Tier 双通道流式抗断连）
- `docs/favicon.svg`：「声波书页（Acoustic Folio）」静态矢量站点图标
- `docs/data/shows.json`：全局播客清单（供前端动态渲染多节目切换栏与主题色）
- `docs/data/<show_id>/index.json`：单节目轻量总目录（按 `pub_date` 降序，含核心摘要与要点，供前端首屏极速加载）
- `docs/data/<show_id>/episodes/<safe_guid>.json`：按单集独立存储的完整精读数据（含 `highlights` 与全量 `transcript`，供前端按需懒加载）
- `docs/index.html`：单文件零构建自适应双语阅读器（内联 CSS + 原生 Vanilla JS，v3.2 刊物与液态玻璃架构）
- `.github/workflows/update.yml`：GitHub Actions 定时巡检与手动多节目触发工作流（超时设为 45 分钟）

---

## 2. 后端处理状态机（`scripts/process.py`）

多播客处理按以下状态流转，支持 `--show <show_id>` 或 `--show all`（按顺序处理全部已启用节目）：

```text
[0. MIGRATE_IF_NEEDED] 启动自动检查或 --migrate-only：
   │ 1. 若根目录存在 GLOSSARY.md 且 glossaries/airshow.md 不存在：移至 glossaries/airshow.md
   │ 2. 若旧版 docs/data/index.json 存在且 docs/data/airshow/index.json 不存在：
   │    将 docs/data/episodes/*.json 迁入 docs/data/airshow/episodes/ 并补齐 schema_version=3.0 与 show_id=airshow
   │    更新 index.json 并原子写入 docs/data/airshow/index.json，校验完整后清理旧文件
   │ 3. 为已启用但尚未初始化的节目（如 sinica）初始化空 index.json 和 episodes/ 目录，防前端 404
   │ 4. 原子生成/更新 docs/data/shows.json 全局清单
   │ (若开启 --migrate-only：完成后直接正常退出 0)
   ▼
[遍历目标节目列表 (target_shows)]
   │
   ├─► [1. FETCH_RSS] 抓取当前节目的 rss_url
   │      │ (失败：记录 ERROR 并以非零码退出)
   │      ▼
   ├─► [2. FILTER_NEW] 对比 docs/data/<show_id>/index.json 已有 guid，按 --max-age-days 筛选未入库单集（--limit）
   │      │ (当前节目无新单集：记录日志并流转至下一个节目)
   │      ▼
   ├─► [2.5 EXTRACT_SHOW_NOTES] 提取 RSS entry 的 content/summary/description，做 HTML 实体与标签清洗
   │      │ 保留嘉宾姓名、头衔与章节时间戳，供通道 B/A 动态注入识别流动嘉宾
   │      ▼
   ├─► [3. DOWNLOAD_AUDIO] 四级抗 403 下载器（Apple Podcasts UA → Overcast UA → curl_cffi → curl）下载 MP3 至临时目录；ffprobe 获取 duration_sec
   │      ▼
   ├─► [4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 180s）切片
   │      │ (若开启 --dry-run：打印切片清单与 Show Notes 预览后跳至 [8. CLEANUP])
   │      ▼
   ├─► [5. CHANNEL_B] 逐片上传 Gemini File API → 流式调用（低思考模式 + 注入专属术语表与 Show Notes）转写与翻译 → 立即删除云端文件
   │      │ (受 --max-chunks 限制；按 "MM:SS" 输出片内时间戳并做单片钳位，严禁跨片 sort()；片间休眠 CHUNK_SLEEP_SEC)
   │      │ (单集内任一切片在候选池重试耗尽后仍失败：中止本集，跳转 [8. CLEANUP])
   │      ▼
   ├─► [6. CHANNEL_A] 将带时间戳逐字稿流式发给旗舰模型，生成摘要、要点与 4-10 条 Highlights（注入 Show Notes）
   │      ▼
   ├─► [7. ATOMIC_SAVE] 若 --max-chunks > 0：写入 docs/data/<show_id>/_smoke_preview.json（不碰正式库）
   │      │ 否则：原子写入 docs/data/<show_id>/episodes/<safe_guid>.json（.tmp → os.replace）
   │      │      原子更新 docs/data/<show_id>/index.json（按 pub_date 降序，.tmp → os.replace）
   │      │      原子同步 docs/data/shows.json（更新 episode_count 与 latest_pub_date）
   │      ▼
   └─► [8. CLEANUP] finally 块强制删除本地临时目录与残留云端文件

[退出码判定] 若本轮所有待处理单集全部失败，进程以非零退出码 1 退出；否则退出 0。
```

---

## 3. CLI 参数、环境变量与可观测性规范

### 命令行参数

- `--show SHOW_ID`（str，默认 `"all"`）：指定处理的节目 ID（如 `airshow`、`sinica`），或 `"all"`（顺序处理全部 `enabled: true` 的节目）。若传入未知 `show_id` 立即报错退出。
- `--limit N`（int，默认 `1`）：每个节目单次最多处理的新单集数量。
- `--chunk-sec S`（int，默认 `180`）：音频切片时长（秒）；建议保持 `180`（最多 `240`），确保时间锚点高精度。
- `--max-age-days D`（int，可选）：仅处理最近 D 天内发布的未入库新单集；`0` 表示不限制发布时间；未显式指定时自动读取该节目在 `podcasts.json` 中的 `default_max_age_days`（默认 7 天）。
- `--dry-run`（flag，默认关闭）：演习模式。执行 `[1]→[4]` 后打印切片与 Show Notes 提取摘要，**不调用 Gemini API、不修改正式 JSON**，清理临时文件后退出。
- `--max-chunks M`（int，默认 `0` 表示全部）：仅处理前 M 个切片；当 `M > 0` 时进入冒烟预览模式，结果仅写入 `docs/data/<show_id>/_smoke_preview.json`，绝不修改正式库。
- `--migrate-only`（flag，默认关闭）：仅执行 v2→v3 本地数据拆分与清单初始化迁移，不访问网络、不调用 Gemini API，完成后退出 `0`。

### 环境变量与模型路由策略（Paid Tier 优先）

- `GEMINI_API_KEY`：必填（在 `main()` 开头校验，仅非 `--dry-run` 且非 `--migrate-only` 时校验）。
- **通道 B（逐片转写与双语翻译）**：
  - 默认候选模型池：`["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]`
  - 环境变量覆盖：`GEMINI_MODEL_CH_B`（兼容 `GEMINI_MODEL`），非空时置顶并去重。
  - 思考等级：`GEMINI_THINKING_CH_B`（默认 `"low"`），若模型不支持自动降级剥离重试。
- **通道 A（全局摘要与 Highlights）**：
  - 默认候选模型池：`["gemini-4-carbon", "gemini-4-neon", "gemini-3.8-flash"]`
  - 环境变量覆盖：`GEMINI_MODEL_CH_A`，非空时置顶并去重。
- **片间休眠**：`CHUNK_SLEEP_SEC`（默认 `"2.0"` 秒）。
- **容灾与长退避策略**：候选模型池顺序容灾，单模型最多连续重试 **5 次**，退避间隔 **`15s → 20s → 25s → 30s`**；遇 `404` / `NOT_FOUND` 立即跳过；切换下一模型前休眠 `10s`。

### 统一日志格式

- 后端标准输出统一前缀：`[<SHOW_ID_UPPER>][<STAGE>]`（例如 `[AIRSHOW][RSS]`、`[SINICA][DOWNLOAD]`、`[AIRSHOW][MIGRATE]`）。
- 前端控制台日志统一前缀：`[AIRSHOW][UI]`。

---

## 4. v3.0 数据契约（Schema-First · 不可破坏）

严格锁定以下 `snake_case` 键名，前后端禁止私自增删或改名。

### 文件名安全归一化规则（`safe_guid`）
单集文件名由 `guid` 经 `re.sub(r"[^A-Za-z0-9._-]+", "_", guid).strip("_")` 生成。

### 4.1 全局播客清单：`docs/data/shows.json`

前端首屏优先拉取此清单，用于渲染顶部多播客切换栏与主题色：

```json
{
  "schema_version": "3.0",
  "site_title": "Podcast Bilingual Reader",
  "updated_at": "2026-11-22T10:00:00Z",
  "shows": [
    {
      "id": "airshow",
      "title": "The Air Show",
      "subtitle": "Aviation Business & Industry Analysis",
      "accent_color": "#0a2540",
      "index_file": "airshow/index.json",
      "episode_count": 10,
      "latest_pub_date": "2026-11-19T11:00:00Z"
    },
    {
      "id": "sinica",
      "title": "Sinica Podcast",
      "subtitle": "Current Affairs in China · Hosted by Kaiser Kuo",
      "accent_color": "#9b2226",
      "index_file": "sinica/index.json",
      "episode_count": 6,
      "latest_pub_date": "2026-11-20T08:00:00Z"
    }
  ]
}
```

### 4.2 单播客轻量总目录：`docs/data/<show_id>/index.json`

前端选定某一节目时按相对路径拉取（`fetch('./data/' + show.index_file)`）：

```json
{
  "schema_version": "3.0",
  "show_id": "airshow",
  "podcast_title": "The Air Show",
  "rss_url": "https://feeds.buzzsprout.com/2329718.rss",
  "updated_at": "2026-11-22T10:00:00Z",
  "episodes": [
    {
      "guid": "Buzzsprout-20148830",
      "title": "Singapore's Brand and Network Struggles",
      "pub_date": "2026-11-19T11:00:00Z",
      "duration_sec": 2120,
      "audio_url": "https://www.buzzsprout.com/2329718/episodes/20148830-singapore-s-brand-and-network-struggles.mp3",
      "file": "episodes/Buzzsprout-20148830.json",
      "summary_zh": "200-350 字中文核心摘要，概括核心议题、观点分歧与行业结论。",
      "key_takeaways_zh": [
        "核心要点 1（一句话结论）",
        "核心要点 2（一句话结论）",
        "核心要点 3（一句话结论）"
      ],
      "highlights_count": 6,
      "transcript_count": 185
    }
  ]
}
```

### 4.3 单集完整精读文件：`docs/data/<show_id>/episodes/<safe_guid>.json`

前端选中某期单集时按需懒加载（`fetch('./data/' + show.id + '/' + epMeta.file)`）：

```json
{
  "schema_version": "3.0",
  "show_id": "airshow",
  "guid": "Buzzsprout-20148830",
  "title": "Singapore's Brand and Network Struggles",
  "pub_date": "2026-11-19T11:00:00Z",
  "duration_sec": 2120,
  "audio_url": "https://www.buzzsprout.com/2329718/episodes/20148830-singapore-s-brand-and-network-struggles.mp3",
  "summary_zh": "200-350 字中文核心摘要，概括核心议题、观点分歧与行业结论。",
  "key_takeaways_zh": [
    "核心要点 1（一句话结论）",
    "核心要点 2（一句话结论）",
    "核心要点 3（一句话结论）"
  ],
  "highlights": [
    {
      "start_sec": 202,
      "title_zh": "精简中文标题（15 字以内）",
      "summary_zh": "2-3 句中文解读，说明该片段的核心论点或关键数据。",
      "quote_en": "Short original English quote from this segment."
    }
  ],
  "transcript": [
    {
      "id": 1,
      "start_sec": 2,
      "speaker": "Jon Ostrower",
      "en": "Welcome back to The Air Show...",
      "zh": "欢迎回到 The Air Show……"
    }
  ]
}
```

### 字段校验硬约束

- `schema_version`：固定 `"3.0"`。
- `show_id`：必须与对应节目在 `podcasts.json` 中的 `id` 一致。
- `pub_date` / `updated_at`：ISO-8601 UTC（`YYYY-MM-DDTHH:MM:SSZ`）；`index.json` 的 `episodes` 按 `pub_date` **降序**排列。
- `duration_sec` / `start_sec`：非负整数；所有 `start_sec` 不得超过 `duration_sec` 且保持**单调非递减**。
- `highlights`：4–10 条（适配 60–95 分钟长篇深度单集）；`transcript`：按 2–4 句自然语义块分段，`id` 从 `1` 连续递增。
- `index.json` 中的 `highlights_count`、`transcript_count`、`summary_zh`、`key_takeaways_zh` 必须与对应单集 JSON 文件完全一致。

---

## 5. 前端视觉规范与交互架构（`docs/index.html` · v3.2）

前端遵循 **Magazine Paper（杂志纸面阅读）× OpenRouter Chrome（精密克制控件）× Liquid Glass（原生级透传玻璃外壳）** 三重设计语言，在零构建单文件下完成高质感阅读体验。

### 5.1 双模色彩体系（Warm Paper × Warm Obsidian）

- **Light Mode（Uncoated Warm Paper）**：
  - 纸面底色：`--bg-page: #FAF8F4`，次级便签/面板底：`--bg-surface-elevated: #F3EFE6`，悬停底：`--bg-subtle: #F2EDE3`，当前播放行高光：`--bg-active-row: #EFE9DC`。
  - 墨色阶梯：`--text-primary: #1C1B18`，`--text-secondary: #57534E`，`--text-muted: #857F76`。
  - 节目重音色：Air Show `#0A2540`；Sinica `#9B2226`（`--accent-tint` 与 `--accent-subtle` 随节目动态生成，杜绝跨节目串色）。
  - 声明 `color-scheme: light`。
- **Dark Mode（Warm Obsidian，低光晕长文夜读）**：
  - 暖炭黑底色：`--bg-page: #151412`，浮层底：`--bg-surface: #1C1A17`，便签底：`--bg-surface-elevated: #1E1C19`，当前播放行：`--bg-active-row: #28241F`。
  - 暖羊皮灰字阶（全员达标 WCAG AA）：`--text-primary: #E4E0D8`（杜绝纯白光晕），`--text-secondary: #9E988E`，`--text-muted: #928C82`（对比度 > 5.3:1），`--text-tertiary: #706B63`。
  - 去荧光低饱和重音色：Air Show `#78B2DF`（航空钢蓝）；Sinica `#DF7A74`（沉稳暖陶红）。
  - 声明 `color-scheme: dark`。

### 5.2 排印分工、声学标尺与动态图标

- **西文衬线 + 中文无衬线复合标题字栈**：
  - Google Fonts 异步引入 `Newsreader`（含斜体，负责英文刊头标题与原声引文）与 `Noto Sans SC:wght@400..700`（可变字重）。
  - `--font-editorial: "Newsreader", "Charter", "Georgia", "PingFang SC", "Noto Sans SC", "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif`（严禁回退 `Songti SC` / `SimSun`，消除中文字形割裂）。
  - 中文正文字重变量：`--zh-body-weight` 浅色 `480` / 深色 `460`（防止暗底发胖），配合 `--content-max-w: 760px` 黄金单栏行长。
- **动态五等分声学微刻度标尺**：`.masthead-scale` 按当前单集 `duration_sec` 动态渲染 0%、25%、50%、75%、100% 等宽（`tabular-nums`）时间标尺。
- **「声波书页（Acoustic Folio）」Icon 与动态 Favicon**：内联顶栏 SVG 与 `docs/favicon.svg` 随深浅模式响应；`updateDynamicFavicon()` 通过 Data URI 实时同步浏览器标签页图标底色。

### 5.3 刊物化去卡片排版与嘉宾色标

- **章节双栏线**：`.section-header` 使用上粗下细双栏线，顶线采用柔化变量 `--section-rule-top`（Light `rgba(28, 27, 24, 0.82)` / Dark `rgba(228, 224, 216, 0.26)`），避免暗黑模式下过亮白杠。
- **导语与要点区**：摘要区（`.summary-card`）去盒化为杂志 Standfirst（透明底 + 左侧 `3px` 重音竖线）；关键洞察（`.takeaways-card`）使用轻纸面底 + 极细上下边线。
- **Highlights 便签网格**：`repeat(auto-fill, minmax(min(100%, 330px), 1fr))`，保留微暖便签底色，原声引语使用 `var(--text-secondary)` 保证双模清晰度。
- **逐字稿彻底去卡片化**：`.transcript-container` 移除顶部重复细线，`.transcript-item` 取消外框与圆角，改为纸面连续流式排版（仅底部分隔细线 + 激活态左侧 `2px` 重音竖线与暖米/暖炭高光）。
- **印章式说话人色标**：4 位常驻主播（Jon Ostrower、Brian Sumers、Brett Snyder、Kaiser Kuo）固定低饱和墨色；流动嘉宾通过确定性字符串哈希映射至 **8 组**色板（`spk-dyn-0` ~ `spk-dyn-7`，取模 `% 8`）。

### 5.4 液态玻璃外壳与 iOS/iPadOS 27 Safari 透传架构

- **宿主与玻璃层分离（绕过 WebKit 边缘纯色锁定）**：
  - `.app-header`（`position: sticky; top: 8px`）与 `.audio-player-bar`（`position: fixed; bottom: calc(14px + env(safe-area-inset-bottom))`，移动端 `8px + safe-area`）宿主容器本身严格维持 `background: transparent; pointer-events: none;`，严禁在宿主上声明 `backdrop-filter`、`transform` 或 `will-change`。
  - 所有玻璃滤镜、高光边与投影统一由内部伪元素 `.header-inner::before` 与 `.player-container::before` 承载；内部交互控件恢复 `pointer-events: auto` 与半透明 `--chrome-pill-*` 材质。
  - 根视口保护：仅在 `body` 声明 `overflow-x: clip`（不阻塞 `html` 根视口透传），`html` 声明 `background-color: var(--bg-page)` 保持弹性回弹色一致；`<head>` 不写静态 `<meta name="theme-color">`。
  - 关闭态 `.archive-backdrop` 与 `.archive-drawer` 必须切换为 `display: none` 移出渲染树，防止隐藏 `fixed` 遮罩污染 Safari 顶部/底部采样。
- **清透光学参数（匹配原生 Safari 柔焦轮廓）**：
  - Light：`--bg-surface-header: rgba(250, 248, 244, 0.34)`，`--glass-filter: blur(2.5px) saturate(160%) brightness(1.08)`。
  - Dark：`--bg-surface-header: rgba(18, 17, 15, 0.38)`，`--glass-filter: blur(2.5px) saturate(150%) brightness(0.74)`。
  - 配备 `@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)))` 实色回退。

### 5.5 响应式断点与超薄单行播放器（320px–1440px）

- **宽屏与桌面（`>= 861px`）**：
  - 顶栏悬浮玻璃胶囊（`52px`）：左侧固定显示站点名（不与当前节目 Tab 重复）+ 节目切换 Tab + 中间选集抽屉入口 + 右侧三段双语控件（带透明常驻边框防抖动）+ 带 `/` 提示的搜索框 + 主题键。
  - 底部超薄单行播放胶囊（`--player-h: 42px`，`border-radius: 999px`）：`>= 1024px` 最大宽度放宽至 `900px`，进度条限制上限 `max-width: 260px`，充分展示单集标题（含 `title` 悬停全称）与当前说话人；「跟随音频」全端收拢为正圆图标按钮。
- **平板中间态（`641px–860px`，覆盖 iPad Mini `744px`）**：
  - 隐藏 `.show-tab-count` 与内联搜索框，启用 `#mobile-search-btn` 弹出式悬浮搜索条；`.podcast-nav-tabs` 启用动态溢出检测（`updateTabsOverflowMask()`），仅实际溢出时施加右侧 `.is-overflow-right` 渐隐遮罩。
- **移动端（`<= 640px`，零溢出下探 `320px`）**：
  - 顶栏保持单行紧凑悬浮胶囊（`44px`）：Brand Icon + 可横滑节目 Tab + 右侧 4 枚 `28×28` 图标按钮（选集抽屉、单字循环语言键 `双/中/英`、弹出式搜索、深浅主题）。
  - 底部保持单行超薄悬浮胶囊（`~38–40px`），`<= 375px` 隐藏总时长 `#time-total` 确保 `320px` 下进度条仍具备充足拖拽热区。

### 5.6 核心交互、防退化逻辑与快捷键

- **数据与防竞态**：两级内存缓存（`showIndexCache` / `episodeCache`）+ `currentRequestSeq` + `currentLoadingShowId`；空节目（0 单集）安全隐藏正文与播放器并展示空状态卡片，Console 0 报错。
- **音频精准控制**：`seekAndPlay()` 在 `audio.readyState < 1` 时挂载一次性 `loadedmetadata` 后再定位播放；`timeupdate` 内执行 $O(\log N)$ 二分查找同步高亮逐字稿与 Highlight。
- **选集与书签**：Issue Archive 侧滑抽屉（保留隐藏 `<select id="episode-select">` 兼容垫片）；URL Hash 持久化 `#<show_id>/<encodeURIComponent(guid)>`（兼容 ART19 带斜杠 `gid://...` 与旧版书签）。
- **键盘快捷键**：
  - `/` 或 `Cmd/Ctrl + K`：聚焦/展开搜索栏（通过 `isCompactSearchActive()` 自适应桌面与紧凑态）。
  - `Esc`：关闭选集抽屉，或清空并收起搜索框。
  - `Space`：播放 / 暂停音频（严格避开输入框、下拉框、抽屉打开态与播放器外部按钮/链接，并阻止页面意外滚屏）。
- **安全与性能**：全部外部 JSON 字段经 `textContent` 或 `escapeHtml()` 渲染杜绝 XSS；交互过渡限定离散 CSS 属性（禁止 `transition: all`），全局补齐 `:focus-visible` 焦点轮廓。