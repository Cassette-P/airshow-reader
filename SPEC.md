# airshow-reader 技术规范与数据契约（SPEC.md · v3.0）

## 1. 项目定位与目录结构

播客自动双语精读工具，支持多播客配置驱动与全自动化处理。抓取播客 RSS，提取嘉宾与章节 Show Notes，生成中文核心摘要、带时间戳 Highlight 与中英对照逐字稿，通过 GitHub Pages（`/docs`）发布。

v3.0 架构升级为**多播客配置驱动 + 目录分层隔离 + 全局清单索引**：

- `README.md`：项目简介、本地使用与云端运维指南
- `SPEC.md`：核心架构、状态机与 v3.0 数据契约（本文件）
- `podcasts.json`：多播客核心配置注册表（包含各节目 ID、标题、RSS、术语表路径、主题重音色与默认时效窗口）
- `glossaries/<show_id>.md`：各播客专属常驻主播名单、行业术语表与翻译校对红线（例如 `glossaries/airshow.md`、`glossaries/sinica.md`）
- `ITERATION.md`：迭代里程碑总览、当前活跃工单与交接记录
- `requirements.txt`：Python 依赖
- `.gitignore`：本地缓存与临时预览文件忽略规则（覆盖 `docs/data/**/_smoke_preview.json` 与 `docs/data/**/*.tmp`）
- `scripts/process.py`：主处理引擎（支持 `--show` 参数调度、v2→v3 幂等数据迁移、Show Notes 动态清洗注入与 Paid Tier 双通道流式抗断连）
- `docs/data/shows.json`：全局播客清单（供前端动态渲染多节目切换栏与主题色）
- `docs/data/<show_id>/index.json`：单节目轻量总目录（按 `pub_date` 降序，含核心摘要与要点，供前端首屏极速加载）
- `docs/data/<show_id>/episodes/<safe_guid>.json`：按单集独立存储的完整精读数据（含 `highlights` 与全量 `transcript`，供前端按需懒加载）
- `docs/index.html`：静态自适应双语阅读器（将在 M6-B 升级支持多节目切换）
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

所有标准输出统一前缀：`[<SHOW_ID_UPPER>][<STAGE>]`（例如 `[AIRSHOW][RSS]`、`[SINICA][DOWNLOAD]`、`[AIRSHOW][MIGRATE]` 等）。

---

## 4. v3.0 数据契约（Schema-First）

严格锁定以下 `snake_case` 键名，前后端禁止私自增删或改名。

### 文件名安全归一化规则（`safe_guid`）
单集文件名由 `guid` 经 `re.sub(r"[^A-Za-z0-9._-]+", "_", guid).strip("_")` 生成。

### 4.1 全局播客清单：`docs/data/shows.json`

前端首屏优先拉取此清单，用于渲染顶部多播客切换栏与主题色：

```json
{
  "schema_version": "3.0",
  "site_title": "Podcast Bilingual Reader",
  "updated_at": "2026-11-21T10:42:28Z",
  "shows": [
    {
      "id": "airshow",
      "title": "The Air Show",
      "subtitle": "Aviation Business & Industry Analysis",
      "accent_color": "#0a2540",
      "index_file": "airshow/index.json",
      "episode_count": 8,
      "latest_pub_date": "2026-11-19T11:00:00Z"
    },
    {
      "id": "sinica",
      "title": "Sinica Podcast",
      "subtitle": "Current Affairs in China · Hosted by Kaiser Kuo",
      "accent_color": "#9b2226",
      "index_file": "sinica/index.json",
      "episode_count": 0,
      "latest_pub_date": ""
    }
  ]
}
```

### 4.2 单播客轻量总目录：`docs/data/<show_id>/index.json`

前端选定某一节目时拉取（例如 `fetch('./data/' + show.index_file)`）：

```json
{
  "schema_version": "3.0",
  "show_id": "airshow",
  "podcast_title": "The Air Show",
  "rss_url": "https://feeds.buzzsprout.com/2329718.rss",
  "updated_at": "2026-11-21T10:42:28Z",
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

前端选中某期单集时懒加载拉取：

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

## 5. 前端视觉规范与 Unslop 刊物化架构（docs/index.html · v3.1）

针对 AI 原型界面的“发光、药丸化、彩虹糖、机器文案、双层臃肿顶栏”等 Slop 异味，前端于 2026-11 实施 Unslopping 全量重塑，确立出版物级研读体验：

### 5.1 空间布局与视口释放
- **单层 52px 紧凑期刊顶栏**：
  - 合并原双层 Header，消除对垂直视口的过度挤占；
  - 顶栏容器宽 `--header-max-w: 1240px`，确保大屏下 Brand Monogram、Show Tabs、Issue Archive 按钮与右侧语言/搜索/主题控制栏呼吸舒展且互不遮挡；
  - 正文排版宽度固定为 `--content-max-w: 880px`，贴合学术与财经期刊最佳阅读字频行长；
  - 底部吸底音频播放器紧凑收缩为 60px（`--player-h: 60px`）。

### 5.2 目录抽屉交互（Issue Archive Drawer · 方案 A）
- **彻底移除原生 `<select>` 下拉菜单**：
  - 改造为刊物式侧边抽屉（`#archive-drawer`），大屏最大宽度 440px，窄屏（<=640px）自适应 100vw 全屏展开；
  - 列表以逆序（`#08` ~ `#01`）排布单集，每张单集卡片聚合期号徽章、发布日期、音频时长、句段数、英文标题与中文导读摘要；
  - 当前收听单集呈现左侧 3px 品牌主题重音边框与微底色激活态；
  - 交互支持 `Esc` 键监听、背景遮罩（`#archive-backdrop`）点击关闭与显式关闭按钮；
  - 保留隐藏式 `<select id="episode-select">` 兼容垫片，确保任何历史选择器依赖 100% 安全。

### 5.3 亮/暗双模式品牌重音架构（Dynamic Contrast Tokens）
- **清除硬编码 `--accent-blue`**：
  - 全站动态透传 `--show-accent` 至 `--accent-primary`、`--accent-hover`、`--accent-subtle`，全面接管音频播放器滑轨与播放按钮、精选原声引用线、摘要卡片重音与搜索高亮；
  - **双模式高可读性对比度规范**：
    - **Air Show**：亮色模式墨蓝 `#0a2540`，暗色模式天青 `#38bdf8`；
    - **Sinica Podcast**：亮色模式印泥朱红 `#9b2226`，暗色模式高对比珊瑚粉 `#f87171`（实测暗色石墨底对比度 > 5.2:1，杜绝深底暗红发黑看不清）。

### 5.4 说话人印刷体排版（Restrained Editorial Badges）
- **杜绝全员 9999px 药丸与 8 色彩虹糖失控**：
  - 说话人徽章半径收敛为 4px（`var(--radius-sm)`）；
  - 摒弃高饱和荧光彩色背景，采用无底色或极浅中性微底（4%~6% 不透明度），搭配细致的 1px 印刷墨色边框；
  - 说话人姓名右侧跟随 `tabular-nums` 时间戳，保证多人对谈时视线扫读极其沉静且易于辨识。

### 5.5 字体排印分工策略（Typography Stack）与声学图标规范
- **单集扉页（Masthead）与原声引文（Pull-quote）**：
  - 采用经典出版物衬线体栈：`Charter, 'Bitstream Charter', 'Sitka Text', Cambria, serif`；
- **双语逐字稿正文与中文可变字重（M7.1 精修）**：
  - 异步加载 Google Fonts `Noto Sans SC:wght@400..700` 可变字体，无缝根治 4K 100% 缩放下中文细笔画发虚；
  - 字体栈精修：`--font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans SC", "PingFang SC", "HarmonyOS Sans SC", "MiSans", "Hiragino Sans GB", "Microsoft YaHei", sans-serif`；
  - 中文正文字重精细化变量：亮色模式 `--zh-body-weight: 480`，暗黑模式 `--zh-body-weight: 460`（杜绝暗底白字笔画发胖膨胀）；
  - 精准覆盖阅读元素：`.transcript-zh`（`1.03125rem` / 16.5px，`font-weight: var(--zh-body-weight)`，`line-height: 1.78`）、`.summary-lead`（`1.03125rem`）、`.takeaway-item`、`.highlight-summary`（`0.875rem`）、`.archive-item-desc`；
  - 次级文字对比度微调：亮色模式 `--text-secondary: #3f3f46`，暗色模式 `--text-secondary: #c4c4cc`。
- **时间码、期号与数字**：
  - 全站时间戳（`MM:SS`）、期号（`#08`）、时长与统计数字全面启用 `font-variant-numeric: tabular-nums` 等宽对齐；
- **声学微刻度标尺（Pass 8 Signature）动态化**：
  - 题头下方嵌入纯 CSS 绘制的声学时间微刻度标尺（`.masthead-scale`），纯依靠排版微细线条展现录音室音频工程质感；
  - 在 `renderArticleMeta` 中按单集时长 `ep.duration_sec` 动态渲染 0%、25%、50%、75%、100% 五等分刻度，自适应 35 分钟短片与 97 分钟超长单集。
- **「声波书页（Acoustic Folio）」标志性 Icon 与动态 Favicon**：
  - 静态图标文件 `docs/favicon.svg`：32×32 viewBox，左侧 3 道等宽声波脉冲柱 + 右侧 3 行出版物印刷段落线，16×16px 极限视口下锐利不糊；
  - 顶栏内联图标 `<svg class="brand-logo-svg">`：底色跟随 `var(--accent-primary)`，内部几何图形跟随 `var(--accent-contrast)`，平滑过渡无缝响应深浅主题；
  - 浏览器标签页 Favicon 动态同步：通过 `updateDynamicFavicon(bgHex)` 动态生成带当前播客 lightAccent 主题色（Air Show `#0a2540` / Sinica `#9b2226`）的 SVG Data URI，实现浏览器标签页图标随选中节目实时变色。

