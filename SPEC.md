# airshow-reader 技术规范与数据契约（SPEC.md · v3.3）

> 数据契约严格保持 **v3.0 稳定版** 基线与 **v3.1 知识卡片向后兼容扩展**；前端视觉、PWA 移动端适配与精听交互规范已演进至 **v3.3**。

## 1. 项目定位与目录结构

个人多播客全自动双语精读静态站，支持多播客配置驱动与全自动化处理。抓取播客 RSS，提取嘉宾与章节 Show Notes，生成中文核心摘要、带时间戳 Highlight、知识卡片（地道表达与参考资源）及中英对照逐字稿，通过 GitHub Pages（`/docs`）零构建原生 ES Modules 与 CSS 架构发布并支持 PWA 原生体验。

- `README.md`：项目简介、本地使用与云端运维指南
- `SPEC.md`：核心架构、状态机、v3.0/v3.1 数据契约、校验门禁与 v3.3 前端规范（本文件）
- `podcasts.json`：多播客核心配置注册表（包含各节目 ID、标题、RSS、术语表路径、主题重音色与默认时效窗口）
- `glossaries/<show_id>.md`：各播客专属常驻主播名单、行业术语表与翻译校对红线（如 `glossaries/airshow.md`、`glossaries/sinica.md`）
- `ITERATION.md`：迭代里程碑总览、验收记录与后续规划
- `requirements.txt`：Python 依赖
- `.gitignore`：本地缓存与临时预览文件忽略规则（覆盖 `docs/data/**/_smoke_preview.json` 与 `docs/data/**/*.tmp`）
- `scripts/process.py`：主处理命令行入口（参数解析与流程调度分发）
- `scripts/validate_data.py`：数据契约与完整性校验门禁（集成于 GitHub Actions CI）
- `scripts/pipeline/`：后端流水线解耦模块目录
  - `__init__.py`
  - `config.py`：全局常量、目录路径与环境变量读取
  - `models.py`：Pydantic 数据契约与结构体模型
  - `storage.py`：JSON 原子读写、内存缓存与 Shows 清单同步
  - `audio.py`：四级抗 403 下载、ffprobe 检测与 180s 切片
  - `gemini.py`：Gemini SDK 客户端初始化、流式调用与长退避重试
  - `prompts.py`：通道 A、通道 B 与 Enrich 提示词生成
  - `rss.py`：RSS 抓取解析与 Show Notes 结构化清洗
  - `runner.py`：单集生命周期驱动、通道调度与补跑引擎
  - `hooks_tal.py`：This American Life 专属预处理钩子（期号提取、无广告归档音频替换、多幕分段与官网逐字稿花名册注入）
  - `migrate.py`：v2→v3 数据目录幂等迁移与新节目脚手架
- `docs/favicon.svg`：「声波书页（Acoustic Folio）」静态矢量站点图标
- `docs/manifest.json`：PWA 应用清单（相对路径 `start_url: "./"`、`scope: "./"`、`display: "standalone"`）
- `docs/sw.js`：PWA Service Worker（静态资源 `airshow-reader-v2` 缓存；`./data/**` 与媒体请求严格走 Network-Only）
- `docs/icons/icon-192.png`、`docs/icons/icon-512.png`：PWA 桌面标准 PNG 图标
- `docs/artwork-airshow-512.png`：The Air Show 专属 512px 节目封面
- `docs/artwork-sinica-512.png`：Sinica Podcast 专属 512px 节目封面
- `docs/artwork-tal-512.png`：This American Life 专属 512px 节目封面
- `docs/css/`：原生 CSS 分层架构（Zero-Build，禁止使用 `@import`）
  - `tokens.css`：主题色彩变量（浅色/深色）、字栈、节目品牌重音色
  - `layout.css`：液态玻璃顶栏/底栏宿主、PWA 安全区、响应式布局断点
  - `components.css`：刊物排版、知识扩展卡片、逐字稿流式排版、刮刮乐盲听、复读胶囊与抽屉
- `docs/js/`：原生 ES Modules 业务模块
  - `main.js`：启动引导、SW 注册、URL Hash 路由监听、顶层事件绑定
  - `state.js`：共享运行时状态、DOM 引用与防竞态锁
  - `storage.js`：localStorage 播放记忆、盲听档位读写
  - `api.js`：shows / index / episode 三级懒加载与缓存
  - `theme.js`：浅/深主题切换、Favicon 动态同步、PWA 探测
  - `player.js`：音频播放引擎、章节打点、单句复读、Media Session
  - `reader.js`：摘要/要点/卡片/逐字稿渲染、搜索高亮、刮刮乐显形
  - `archive.js`：节目 Tab、选集抽屉渲染与已听进度标记
  - `shortcuts.js`：全局键盘快捷键与帮助面板
  - `utils.js`：escapeHtml、时间格式化、二分查找、嘉宾哈希
- `docs/data/shows.json`：全局播客清单（供前端动态渲染多节目切换栏与主题色）
- `docs/data/<show_id>/index.json`：单节目轻量总目录（按 `pub_date` 降序，含核心摘要与要点，供前端首屏极速加载）
- `docs/data/<show_id>/episodes/<safe_guid>.json`：按单集独立存储的完整精读数据（含 `highlights`、`vocab_notes`、`references` 与全量 `transcript`，供前端按需懒加载）
- `docs/index.html`：零构建原生 ES Modules 双语精读器（`<link rel="stylesheet">` + `<script type="module" src="./js/main.js">`）
- `.github/workflows/update.yml`：GitHub Actions 定时巡检、Schema 校验与手动触发工作流

---

## 2. 后端处理状态机（`scripts/process.py`）

多播客处理按以下状态流转，支持 `--show <show_id>` 或 `--show all`（按顺序处理全部已启用节目）：

```text
[0. MIGRATE_IF_NEEDED] 启动自动检查或 --migrate-only（--dry-run 且全局清单已存在时跳过写盘）：
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
   ├─► [2.5 EXTRACT_SHOW_NOTES & HOOKS] 提取 RSS entry 的 content/summary/description，做 HTML 实体与标签清洗；
   │      │ 若配置了 scraper_hook（如 tal）：触发专属钩子抓取官网单集页解析 Act 分幕与纯净无赞助 Show Notes，
   │      │ 替换动态插播音频为官网永久无广告归档 MP3，并抓取官网 Transcript 提取 speaker_roster 花名册注入上下文
   │      ▼
   ├─► [2.8 CHECK_CH_B_CACHE] 检查断点缓存（.cache/ch_b/<show_id>__<safe_guid>.json）：
   │      │ 非 --dry-run 且 --max-chunks == 0 且无 --no-cache 时，若命中且 transcript 非空，
   │      │ 打印 [CACHE] Hit Channel B cache, skipping download/chunk/CH-B，直接跳过 [3]、[4] 与 [5]，直达 [6. CHANNEL_A]
   │      ▼
   ├─► [3. DOWNLOAD_AUDIO] 四级抗 403 下载器（Apple Podcasts UA → Overcast UA → curl_cffi → curl）下载 MP3 至临时目录；ffprobe 获取 duration_sec
   │      ▼
   ├─► [4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 180s）切片
   │      │ (若开启 --dry-run：打印切片清单与 Show Notes 预览后跳至 [8. CLEANUP])
   │      ▼
   ├─► [5. CHANNEL_B] 逐片上传 Gemini File API → 流式调用（低思考模式 + 注入专属术语表与 Show Notes）转写与翻译 → 立即删除云端文件
   │      │ (受 --max-chunks 限制；按 "MM:SS" 输出片内时间戳并做单片钳位，严禁跨片 sort()；
   │      │  所有 speaker 经 normalize_speaker() 剥离 Dr./Prof./Ambassador 等头衔前缀；片间休眠 CHUNK_SLEEP_SEC)
   │      │ (正式全量模式下转写全部完成后立即原子写盘缓存：.cache/ch_b/<show_id>__<safe_guid>.json)
   │      │ (单集内任一切片在候选池重试耗尽后仍失败：中止本集，跳转 [8. CLEANUP])
   │      ▼
   ├─► [6. CHANNEL_A] 输入序列化瘦身（严格仅传 [MM:SS] <speaker>: <en> 英文原声，剔除 zh 译文削减近半 Token 并记录 input_chars）；
   │      │ 显式延长 SDK 超时至 300 秒（300,000ms），遇到传输断连或超时立即关闭并重建 genai.Client；
   │      │ 在同一思考档位连续 2 次断连时自动顺降一级（xhigh → high → low → None），平滑容灾并一体生成摘要、要点、
   │      │ 4-10 条 Highlights、6-10 条 vocab_notes（自动纠偏 ASR 拼写）与 0-10 条 references（严禁收录商业赞助广告）；
   │      │ 落盘前完成 start_sec 范围钳制与升序排序（references 中 start_sec=None 稳定排在最末）
   │      ▼
   ├─► [7. ATOMIC_SAVE] 若 --max-chunks > 0：写入 docs/data/<show_id>/_smoke_preview.json（不碰正式库与缓存）
   │      │ 否则：原子写入 docs/data/<show_id>/episodes/<safe_guid>.json（.tmp → os.replace）
   │      │      原子更新 docs/data/<show_id>/index.json（按 pub_date 降序，不含知识卡片以保持轻量）
   │      │      原子同步 docs/data/shows.json（更新 episode_count 与 latest_pub_date）
   │      │      成功落盘后立即删除对应 .cache/ch_b/ 缓存文件（CH-A 失败则保留供无缝续跑）
   │      ▼
   └─► [8. CLEANUP] finally 块强制删除本地临时目录与残留云端文件

[退出码判定] 若本轮所有待处理单集全部失败，进程以非零退出码 1 退出；否则退出 0。
```

### 增量补跑状态机（`scripts/process.py --enrich`）

针对存量单集补跑知识卡片（`vocab_notes` 与 `references`）的轻量幂等通道：

```text
[ENRICH 增量补跑模式]
   │
   ├─► 扫描 docs/data/<show_id>/index.json 与 episodes/*.json（按 pub_date 降序）
   ├─► 筛选缺失 vocab_notes 或 references 键的单集（两者键均已存在即跳过，受 --limit 截断限制；--limit 0 为全量）
   ├─► 对有待补跑单集的节目，内存单次拉取 RSS 建立 {guid: show_notes} 字典（失败打 WARN 降级）
   ├─► 拼装带 [MM:SS] 时间戳逐字稿、Show Notes 与术语表，调用轻量模型池（默认 gemini-3.8-flash / low thinking）
   ├─► 流式生成 EnrichmentResponse (vocab_notes + references)：
   │      • 严禁收录播客商业广告/赞助口播（sponsor / ad read / promo discounts）
   │      • vocab_notes 自动订正逐字稿中的明显 ASR 拼写误差
   │      • 钳制 start_sec 合法区间并按 start_sec 升序排序（references 为 None 者置底）
   ├─► 追加合并写入原分集 JSON（.tmp → os.replace），原既有所有字段字节级不变；index.json 保持轻量不扩充
   └─► 若开启 --dry-run：打印候选清单、是否命中 Show Notes、预估字符数与模型池，不调 API 不落盘
```

---

## 3. CLI 参数、环境变量与可观测性规范

### 命令行参数

- `--show SHOW_ID`（str，默认 `"all"`）：指定处理的节目 ID（如 `airshow`、`sinica`），或 `"all"`（顺序处理全部 `enabled: true` 的节目）。若传入未知 `show_id` 立即报错退出。
- `--limit N`（int，默认 `1`）：每个节目单次最多处理的新单集数量（`--enrich` 模式下传 `0` 表示不截断全量扫描）。
- `--chunk-sec S`（int，默认 `180`）：音频切片时长（秒）；建议保持 `180`（最多 `240`），确保时间锚点高精度。
- `--max-age-days D`（int，可选）：仅处理最近 D 天内发布的未入库新单集；`0` 表示不限制发布时间；未显式指定时自动读取该节目在 `podcasts.json` 中的 `default_max_age_days`（默认 7 天）。
- `--dry-run`（flag，默认关闭）：演习模式。常规模式执行 `[1]→[4]` 后打印切片与 Show Notes 提取摘要，`--enrich` 模式打印待补跑清单；**不调用 Gemini API、不修改任何正式 JSON（含 `shows.json`）**，清理临时文件后退出。
- `--max-chunks M`（int，默认 `0` 表示全部）：仅处理前 M 个切片；当 `M > 0` 时进入冒烟预览模式，结果仅写入 `docs/data/<show_id>/_smoke_preview.json`，绝不修改正式库。
- `--migrate-only`（flag，默认关闭）：仅执行 v2→v3 本地数据拆分与清单初始化迁移，不访问网络、不调用 Gemini API，完成后退出 `0`。
- `--enrich`（flag，默认关闭）：增量补跑模式。仅针对存量单集中缺失 `vocab_notes` 或 `references` 的单集，利用轻量模型池补跑知识卡片；原既有摘要、Highlights 与逐字稿字节级保持不变。
- `--no-cache`（flag，默认关闭）：忽略已有 Channel B 断点缓存并强制重跑下载、切片与转写。

### 播客注册表扩展规范（`podcasts.json`）

- `content_style`（str，可选，默认 `"analytical"`）：
  - `"analytical"`：政经产业深度分析体（Air Show、Sinica），聚焦核心议题、观点分歧、数据与行业黑话；
  - `"narrative"`：非虚构叙事特稿体（This American Life），`summary_zh` 聚焦单集母主题与分幕展开，`key_takeaways_zh` 输出分幕导读（Act Guide），`vocab_notes` 聚焦美式口语与文化梗，`references` 输出叙事中引用的真实作品与报道。
- `scraper_hook`（str，可选）：
  - `"tal"`：This American Life 专属解耦钩子。从单集页抓取多幕结构化 Show Notes，提取官网无广告原版归档 MP3（绕过 CDN 动态插入商业广告），并抓取官网 Transcript 解析 `speaker_roster` 注入上下文；实施 GUID + 期号双重去重机制，自动识别并过滤重播节目。

### 环境变量与模型路由策略（Paid Tier 优先）

- `GEMINI_API_KEY`：必填（在 `main()` 开头校验，仅非 `--dry-run` 且非 `--migrate-only` 时校验）。
- **请求超时与连接复原**：SDK 初始化显式配置 `types.HttpOptions(timeout=300000)`（300 秒）。识别传输层断连与超时异常（`RemoteProtocolError`、`ReadTimeout`、`ConnectTimeout`、`ConnectionError`、`Server disconnected` 等），命中时立即关闭并重建 `genai.Client` 实例，严禁复用半关闭 Socket。
- **知识卡片增量补跑通道（Enrichment Channel）**：
  - 默认候选模型池：`["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]`
  - 环境变量覆盖：`GEMINI_MODEL_ENRICH`，非空时置顶并去重。
  - 思考等级：`GEMINI_THINKING_ENRICH`（默认 `"low"`），若模型不支持自动降级剥离重试。
- **通道 B（逐片转写与双语翻译）**：
  - 默认候选模型池：`["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]`
  - 环境变量覆盖：`GEMINI_MODEL_CH_B`（兼容 `GEMINI_MODEL`），非空时置顶并去重。
  - 思考等级：`GEMINI_THINKING_CH_B`（默认 `"low"`），若模型不支持自动降级剥离重试。
  - **断点缓存机制**：正式全量模式（`--max-chunks == 0` 且非 `--dry-run`）转写完成后落盘至 `.cache/ch_b/<show_id>__<safe_guid>.json`；单集入库前命中缓存可跳过下载与 CH-B，仅在 `[7. ATOMIC_SAVE]` 成功后删除缓存。
- **通道 A（全局摘要、Highlights 与知识卡片）**：
  - 默认候选模型池：`["gemini-4-carbon", "gemini-4-neon", "gemini-3.8-flash"]`
  - 环境变量覆盖：`GEMINI_MODEL_CH_A`，非空时置顶并去重。
  - **输入逐字稿瘦身**：严格仅向通道 A 传递 `[MM:SS] <speaker>: <en>`（英文原声文本），剔除 `zh` 中文译文以削减近半输入 Token、缩短首字节思考静默时间，调用前记录 `input_chars` 字符数。
  - 思考等级与自动适配：`GEMINI_THINKING_CH_A`（可选）。
    - 显式覆盖：若环境变量非空（`.strip()` 判空），返回其小写值（若显式设为 `"default"` 或 `"none"` 则返回 `None` 走模型默认）；
    - 模型默认适配：若未设置环境变量，按模型自动匹配：`gemini-4-neon` 返回 `"xhigh"`，`gemini-4-carbon` 返回 `"high"`，其他模型默认返回 `"high"`；
    - 容错与平滑降级链：若本地 SDK 校验或远端 API 调用因不支持该档位报错，按 **`xhigh` → `high` → `low` → `None`（移除 `thinking_config`）** 逐级降级并打印降级日志后立即重试；
    - 连续断连降级策略：在同一候选模型的重试循环中，当同一思考档位连续发生 2 次传输断连/超时时，自动将思考档位顺降一级（降级链：`xhigh` → `high` → `low` → `None`），重置断连计数并打印日志继续重试。
- **片间休眠**：`CHUNK_SLEEP_SEC`（默认 `"2.0"` 秒）。
- **容灾与长退避策略**：候选模型池顺序容灾，单模型最多连续重试 **5 次**，退避间隔 **`15s → 20s → 25s → 30s`**；遇 `404` / `NOT_FOUND` 立即跳过；切换下一模型前休眠 `10s`。

### 统一日志格式

- 后端标准输出统一前缀：`[<SHOW_ID_UPPER>][<STAGE>]`（例如 `[AIRSHOW][RSS]`、`[SINICA][DOWNLOAD]`、`[AIRSHOW][ENRICH]`）。
- 前端控制台日志统一前缀：`[AIRSHOW][UI]`。

---

## 4. v3.0 / v3.1 数据契约（Schema-First · 不可破坏）

严格锁定以下 `snake_case` 键名，前后端禁止私自增删或改名。

### 文件名与 Speaker 安全归一化规则

- **单集文件名（`safe_guid`）**：由 `guid` 经 `re.sub(r"[^A-Za-z0-9._-]+", "_", guid).strip("_")` 生成。
- **说话人清洗（`normalize_speaker`）**：去除首尾空白及开头学术/职业头衔前缀（`Dr.`、`Prof.`、`Professor`、`Ambassador`、`Mr.`、`Ms.`、`Mrs.`、`Sen.`、`Rep.`，大小写不敏感），杜绝同一嘉宾标签分裂。

### 4.1 全局播客清单：`docs/data/shows.json`

前端首屏优先拉取此清单，用于渲染顶部多播客切换栏与主题色：

```json
{
  "schema_version": "3.0",
  "site_title": "Podcast Bilingual Reader",
  "updated_at": "2026-11-23T10:00:00Z",
  "shows": [
    {
      "id": "airshow",
      "title": "The Air Show",
      "subtitle": "Aviation Business & Industry Analysis",
      "accent_color": "#0a2540",
      "index_file": "airshow/index.json",
      "episode_count": 11,
      "latest_pub_date": "2026-11-19T11:00:00Z"
    },
    {
      "id": "sinica",
      "title": "Sinica Podcast",
      "subtitle": "Current Affairs in China · Hosted by Kaiser Kuo",
      "accent_color": "#9b2226",
      "index_file": "sinica/index.json",
      "episode_count": 7,
      "latest_pub_date": "2026-11-20T05:00:00Z"
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
  "rss_url": "[https://feeds.buzzsprout.com/2329718.rss](https://feeds.buzzsprout.com/2329718.rss)",
  "updated_at": "2026-11-23T10:00:00Z",
  "episodes": [
    {
      "guid": "Buzzsprout-20148830",
      "title": "Singapore's Brand and Network Struggles",
      "pub_date": "2026-11-19T11:00:00Z",
      "duration_sec": 2120,
      "audio_url": "[https://www.buzzsprout.com/2329718/episodes/20148830-singapore-s-brand-and-network-struggles.mp3](https://www.buzzsprout.com/2329718/episodes/20148830-singapore-s-brand-and-network-struggles.mp3)",
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
  "audio_url": "[https://www.buzzsprout.com/2329718/episodes/20148830-singapore-s-brand-and-network-struggles.mp3](https://www.buzzsprout.com/2329718/episodes/20148830-singapore-s-brand-and-network-struggles.mp3)",
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
  "vocab_notes": [
    {
      "term": "fifth-freedom flight",
      "meaning_zh": "第五航权航班（航空公司在第三国经停并上下客货的航线经营权）",
      "note_zh": "民航界核心黑话，通常用于跨洲长途经停中转竞争，具有极高政策准入门槛",
      "start_sec": 412
    }
  ],
  "references": [
    {
      "title": "Flying Blind: The 737 MAX Tragedy and the Fall of Boeing",
      "creator": "Peter Robison",
      "category": "book",
      "note_zh": "嘉宾引用此书论述波音安全管理与质量文化转型中的长期系统性教训",
      "start_sec": 845
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

- `schema_version`：固定 `"3.0"`（向后完全兼容）。
- `show_id`：必须与对应节目在 `podcasts.json` 中的 `id` 一致。
- `pub_date` / `updated_at`：ISO-8601 UTC（`YYYY-MM-DDTHH:MM:SSZ`）；`index.json` 的 `episodes` 按 `pub_date` **降序**排列。
- `duration_sec` / `start_sec`：非负整数；所有 `start_sec` 不得超过 `duration_sec`。`transcript` 与 `highlights` 严格单调非递减；`vocab_notes` 按 `start_sec` 升序排列；`references` 按 `start_sec` 升序排列且 `null` 排在最末。
- `highlights`：4–10 条（适配 60–95 分钟长篇深度单集）；`transcript`：按 2–4 句自然语义块分段，`id` 从 `1` 连续递增。
- `vocab_notes`（v3.1 可选扩展字段）：6–10 条高价值英文词汇、习语或行业黑话，自动纠正逐字稿中明显的 ASR 拼写错误；包含 `term`、`meaning_zh`、`note_zh` 与 `start_sec`（`0 <= start_sec <= duration_sec`）。
- `references`（v3.1 可选扩展字段）：0–10 条书目、深度文章、研报、影视/播客或工具，**严禁收录商业赞助商/口播优惠码广告**；包含 `title`、`creator`（未知填 `""`）、`category`（`"book" | "article" | "media" | "other"`）、`note_zh` 与 `start_sec`（`0 <= start_sec <= duration_sec`，或 `null`）。
- `index.json`：保持轻量，**严禁**将 `vocab_notes` 与 `references` 写入 `index.json`。
- `index.json` 中的 `highlights_count`、`transcript_count`、`summary_zh`、`key_takeaways_zh` 必须与对应单集 JSON 文件完全一致。

### 4.4 数据契约校验门禁规范（`scripts/validate_data.py`）

为确保后端 AI 生成数据与前端渲染契约绝对一致，设立严格的自动化质量门禁：

- **校验范围**：
  - `podcasts.json` 与 `docs/data/shows.json` 节目元数据一致性与单集计数对齐；
  - 各节目 `index.json` 的字段完整性、GUID 唯一性、按 `pub_date` 降序排序、以及轻量化约束（严禁泄漏 `vocab_notes` / `references`）；
  - 各分集 `episodes/<safe_guid>.json` 的 Schema 合法性、单集引用对应一致性、`summary_zh` 字数下限、`highlights` 与 `transcript` 时间戳区间单调非递减性；
  - `vocab_notes`（`term`、`meaning_zh`、`note_zh`、`start_sec` 升序）与 `references`（`title`、`creator`、`category` 枚举合法性、`note_zh`、`start_sec` 升序且 `null` 排在最末）；
  - `index.json` 与分集 JSON 间 `summary_zh`、`key_takeaways_zh`、`highlights_count`、`transcript_count` 的双向完全对齐。
- **门禁执行**：本地执行 `python scripts/validate_data.py`；GitHub Actions 在每次处理完数据落盘后、执行 git commit 之前自动运行，若校验不通过立即抛出非零退出码阻断提交流程。

---

## 5. 前端视觉规范与交互架构（`docs/index.html` · v3.3）

前端遵循 **Magazine Paper（杂志纸面阅读）× OpenRouter Chrome（精密克制控件）× Liquid Glass（原生级透传玻璃外壳）** 三重设计语言，在零构建原生 CSS 与 ES Modules 架构下完成多播客精读、PWA 通勤播放与主动听力训练。

### 5.1 双模色彩体系（Warm Paper × Warm Obsidian）

- **Light Mode（Uncoated Warm Paper）**：
  - 纸面底色：`--bg-page: #FAF8F4`，次级便签/面板底：`--bg-surface-elevated: #F3EFE6`，悬停底：`--bg-subtle: #F2EDE3`，当前播放行高光：`--bg-active-row: #EFE9DC`。
  - 墨色阶梯：`--text-primary: #1C1B18`，`--text-secondary: #57534E`，`--text-muted: #857F76`。
  - 节目重音色：Air Show `#0A2540`；Sinica `#9B2226`；This American Life `#2D6A4F`（`--accent-tint` 与 `--accent-subtle` 随节目动态生成，杜绝跨节目串色）。
  - 声明 `color-scheme: light`。
- **Dark Mode（Warm Obsidian，低光晕长文夜读）**：
  - 暖炭黑底色：`--bg-page: #151412`，浮层底：`--bg-surface: #1C1A17`，便签底：`--bg-surface-elevated: #1E1C19`，当前播放行：`--bg-active-row: #28241F`。
  - 暖羊皮灰字阶（全员达标 WCAG AA）：`--text-primary: #E4E0D8`（杜绝纯白光晕），`--text-secondary: #9E988E`，`--text-muted: #928C82`（对比度 > 5.3:1），`--text-tertiary: #706B63`。
  - 去荧光低饱和重音色：Air Show `#78B2DF`（航空钢蓝）；Sinica `#DF7A74`（沉稳暖陶红）；This American Life `#74C69D`（薄荷青绿）。
  - 声明 `color-scheme: dark`。

### 5.2 排印分工、声学标尺与动态图标

- **西文衬线 + 中文无衬线复合标题字栈**：
  - Google Fonts 异步引入 `Newsreader`（含斜体，负责英文刊头标题、语料词头与原声引文）与 `Noto Sans SC:wght@400..700`（可变字重）。
  - `--font-editorial: "Newsreader", "Charter", "Georgia", "PingFang SC", "Noto Sans SC", "HarmonyOS Sans SC", "Microsoft YaHei", sans-serif`（严禁回退 `Songti SC` / `SimSun`，消除中文字形割裂）。
  - 中文正文字重变量：`--zh-body-weight` 浅色 `480` / 深色 `460`（防止暗底发胖），配合 `--content-max-w: 760px` 黄金单栏行长。
- **动态五等分声学微刻度标尺**：`.masthead-scale` 按当前单集 `duration_sec` 动态渲染 0%、25%、50%、75%、100% 等宽（`tabular-nums`）时间标尺。
- **「声波书页（Acoustic Folio）」Icon 与动态 Favicon**：内联顶栏 SVG 与 `docs/favicon.svg` 随深浅模式响应；`updateDynamicFavicon()` 通过 Data URI 实时同步浏览器标签页图标底色。

### 5.3 刊物化去卡片排版、知识卡片与嘉宾色标

- **章节双栏线**：`.section-header` 使用上粗下细双栏线，顶线采用柔化变量 `--section-rule-top`（Light `rgba(28, 27, 24, 0.82)` / Dark `rgba(228, 224, 216, 0.26)`）。
- **导语与要点区**：摘要区（`.summary-card`）去盒化为杂志 Standfirst（透明底 + 左侧 `3px` 重音竖线）；关键洞察（`.takeaways-card`）使用轻纸面底 + 极细上下边线。
- **Highlights 便签网格**：`repeat(auto-fill, minmax(min(100%, 330px), 1fr))`，保留微暖便签底色，原声引语使用 `var(--text-secondary)`。
- **可折叠知识卡片区（`#knowledge-section`，位于 Highlights 与 Transcript 之间）**：
  - 采用原生 HTML5 `<details>` / `<summary>` 折叠面板，默认折叠展示计数标题条（`地道表达与行业语料 · N 条`、`提及书单与参考资源 · N 项`），切换单集时自动重置为折叠态（`open = false`）。
  - **向后兼容与空态隐藏**：缺失或空数组时隐藏对应子面板，两者均空或空节目时隐藏整个 `#knowledge-section` 容器，Console 0 报错。
  - **地道表达卡（`vocab_notes`）**：桌面双列 / 移动端（`<= 640px`）单列网格；`Newsreader` 加粗英文词头 + 可点击时间戳胶囊 + 主色 `meaning_zh` + 次级 `note_zh`。
  - **参考资源卡（`references`）**：等宽分类徽章（`book`→`书籍`、`article`→`文章/报告`、`media`→`影音/播客`、`other`→`其他`）+ 加粗 `title` + 可选 `creator` + `note_zh` + 合法有效数字 `start_sec` 时才渲染的时间戳跳转胶囊。
  - 点击卡片时间戳调用 `seekAndPlay()` 并主动调用 `exitSentenceLoop()` 安全退出单句复读。
- **逐字稿彻底去卡片化**：`.transcript-item` 取消外框与圆角，改为纸面连续流式排版（底部分隔细线 + 激活态左侧 `2px` 重音竖线与暖米/暖炭高光）。
- **印章式说话人色标**：5 位常驻主播（Jon Ostrower、Brian Sumers、Brett Snyder、Kaiser Kuo、Ira Glass）固定低饱和墨色；Ira Glass 配备专属印章色标 `spk-ira`（Light `#2D6A4F` / Dark `#74C69D`）；流动嘉宾通过确定性字符串哈希映射至 **8 组**色板（`spk-dyn-0` ~ `spk-dyn-7`，取模 `% 8`）。

### 5.4 液态玻璃外壳与 iOS Safari / PWA 安全区透传架构

- **宿主与玻璃层分离（保持原生透明渐进模糊状态栏）**：
  - `.app-header`（`position: sticky`）与 `.audio-player-bar`（`position: fixed`）宿主容器严格维持 `background: transparent; pointer-events: none;`，严禁在宿主上铺设不透明背景遮罩或声明 `backdrop-filter` / `transform` / `will-change`，防止把 iOS 状态栏涂成纯白/纯黑横条。
  - 所有玻璃滤镜、高光边与投影统一由内部伪元素 `.header-inner::before` 与 `.player-container::before` 承载；内部交互控件恢复 `pointer-events: auto` 与半透明 `--chrome-pill-*` 材质。
  - 根视口保护：仅在 `body` 声明 `overflow-x: clip`，`html` 声明 `background-color: var(--bg-page)`；关闭态 `.archive-backdrop` 与 `.archive-drawer` 切换为 `display: none` 移出渲染树。
- **PWA 状态栏与安全区适配**：
  - `<head>` 声明 `viewport-fit=cover`、`apple-mobile-web-app-capable="yes"`、`apple-mobile-web-app-status-bar-style="default"` 以及 `<meta name="theme-color" content="#FAF8F4">`；`setTheme()` / `initTheme()` 通过 `updateThemeColorMeta()` 随浅/暗黑主题实时同步 `#FAF8F4` / `#151412`。
  - 通过 `@media (display-mode: standalone)` 与 JS 检测 `window.navigator.standalone` 挂载 `html.is-pwa` 双保险识别 PWA 独立模式，启用 `--pwa-top-extra: 24px`（非 PWA 保持 `0px`），使顶栏胶囊稳稳脱离 iOS 顶部渐进模糊带，同时联动全部 `scroll-margin-top` 与移动端展开搜索栏偏移。
  - 底部悬浮播放器采用固定底距（桌面 `14px` / 移动 `8px`）+ 外框 `padding-bottom: calc(0px + env(safe-area-inset-bottom, 0px))`，抬高胶囊避开 Home 指示条而不撑大胶囊本体。
- **清透光学参数**：
  - Light：`--bg-surface-header: rgba(250, 248, 244, 0.34)`，`--glass-filter: blur(2.5px) saturate(160%) brightness(1.08)`。
  - Dark：`--bg-surface-header: rgba(18, 17, 15, 0.38)`，`--glass-filter: blur(2.5px) saturate(150%) brightness(0.74)`。
  - 配备 `@supports not ((backdrop-filter: blur(1px)) or (-webkit-backdrop-filter: blur(1px)))` 实色回退。

### 5.5 响应式断点与超薄单行播放器（320px–1440px）

- **宽屏与桌面（`>= 861px`）**：
  - 顶栏悬浮玻璃胶囊（`52px`）：左侧站点名 + 节目切换 Tab + 中间选集抽屉入口 + 右侧三段双语控件 + 带 `/` 提示的搜索框 + 主题键。
  - 底部超薄单行播放胶囊（`--player-h: 42px`，`border-radius: 999px`）：`>= 1024px` 最大宽度 `900px`，进度条上限 `max-width: 260px`，集成章节刻度打点、实时章节胶囊（点击滚动闪烁聚焦至 Highlight）、跟随音频按钮与快捷键 `?` 入口。
- **平板中间态（`641px–860px`，覆盖 iPad Mini `744px`）**：
  - 隐藏 `.show-tab-count` 与内联搜索框，启用 `#mobile-search-btn` 弹出式悬浮搜索条；`.podcast-nav-tabs` 启用动态溢出检测（`updateTabsOverflowMask()`）。
- **移动端（`<= 640px`，零溢出下探 `320px`）**：
  - 顶栏单行紧凑悬浮胶囊（`44px`）：Brand Icon + 可横滑节目 Tab + 右侧 4 枚 `28×28` 图标按钮（选集抽屉、单字循环语言键 `双/中/英`、弹出式搜索、深浅主题）。
  - 底部单行超薄悬浮胶囊（`~38–40px`），隐藏实时章节胶囊与快捷键入口，`<= 375px` 隐藏总时长 `#time-total` 保障拖拽热区。

### 5.6 播放器增强、Media Session 与 PWA 离线策略（M7）

- **进度条章节打点**：按 `(hl.start_sec / duration) * 100%` 在 `.scrub-track-wrap` 叠加 `.chapter-marker`（容器 `pointer-events: none`、刻度点 `pointer-events: auto` 不阻挡拖拽），桌面端悬停展示 `MM:SS · title_zh` Tooltip，点击直接跳播。
- **断点续播与进度记忆**：使用 `localStorage['airshow_reader_progress_v1']` 按 `${showId}::${guid}` 存储 `{ position, duration, completed, updatedAt }`；播放期间 3 秒节流写入，并在 `pause` / `ended` / `visibilitychange` / `beforeunload` / 切集前即时落盘。进度 `>= 95%` 或剩余 `< 30s`（总长 `> 60s`）自动标为 `completed: true`；加载未完成单集（`position >= 5`）时弹出浮动续播提示条（支持「继续收听」与「从头播放」），并设 `userInteractedSeek` 门禁防止覆盖用户主动跳转；选集下拉框与 Issue Archive 抽屉实时同步 `[▶ xx%]` / `[✓ 已听完]` 状态标。
- **Media Session API**：写入 `metadata`（`artwork` 注册包含通用 `192x192` 与 `512x512` 图标，并根据当前节目动态注入专属 512px 封面 `artwork-airshow-512.png`、`artwork-sinica-512.png` 或 `artwork-tal-512.png` 保证锁屏高清渲染）；注册 `play`、`pause`、`seekbackward`（默认 10s）、`seekforward`（默认 10s）、`seekto`；`setPositionState` 经有限正数边界校验并包在 `try/catch` 中执行。
- **Service Worker（`docs/sw.js`）**：仅在 `https:` 或 `localhost`/`127.0.0.1` 注册；对 `./data/**` 与音频媒体请求严格采用 **Network-Only**，确保定时更新的新单集与补跑数据即时生效。

### 5.7 主动精听套件、防竞态与快捷键规范（M8-A）

- **数据与防竞态**：两级内存缓存（`showIndexCache` / `episodeCache`）+ `currentRequestSeq` + `currentLoadingShowId`；空节目安全隐藏正文、知识卡片与播放器并展示空状态卡片，Console 0 报错。
- **音频精准控制**：`seekAndPlay()` 在 `audio.readyState < 1` 时挂载一次性 `loadedmetadata` 后再定位播放；`timeupdate` 内执行 $O(\log N)$ 二分查找同步高亮逐字稿与 Highlight。
- **“刮刮乐”模糊盲听模式**：逐字稿头部工具栏提供 `清晰（off）/ 遮中文（zh）/ 全盲听（both）` 三档切换，持久化至 `localStorage['airshow_blur_mode']`；模糊块点击调用 `event.stopPropagation()` 切换 `.is-revealed` 单独刮开显形且不中断音频；关键词搜索命中自动附加 `.has-search-match` 显形；切换档位或单集重置显形状态。
- **单句复读**：每条逐字稿头部设 `btn-sentence-loop`，复读区间 `[startSec, endSec]` 取下一段 `start_sec` 或单集总长（差值 `< 1.5s` 兜底 `+3s`），`timeupdate` 抵达 `endSec - 0.15` 无缝回跳 `startSec`；底部浮现复读状态胶囊；再次点击、跳播其他段/Highlight/知识卡片、跨区间拖拽或切集时自动调用 `exitSentenceLoop()` 退出。
- **URL Hash 书签**：持久化 `#<show_id>/<encodeURIComponent(guid)>`（兼容 ART19 带斜杠 `gid://...` 与旧版无 `show_id` 书签）。
- **全局键盘快捷键**（严格过滤 `INPUT`/`TEXTAREA`/`SELECT`/`isContentEditable` 及 `Ctrl`/`Cmd`/`Alt` 组合键，空态屏蔽播放控制）：
  - `Space`：播放 / 暂停（阻止页面滚屏）。
  - `←` / `→`：后退 / 快进 5 秒（配合 `Shift` 为 ±15 秒）。
  - `R`：重听当前激活的逐字稿段落（无激活段时后退 5 秒）。
  - `L`：对当前激活段落一键开启 / 退出单句复读。
  - `B`：循环切换盲听模式（`清晰 → 遮中文 → 全盲听`）。
  - `/` 或 `Cmd/Ctrl + K`：聚焦 / 展开搜索栏。
  - `?`：打开 / 关闭快捷键说明弹窗（移动端 `<= 768px` 隐藏入口）。
  - `Esc`：按优先级关闭快捷键弹窗、选集抽屉或清空收起搜索框。
- **安全与性能**：全部外部 JSON 字段经 `textContent` 或 `escapeHtml()` 渲染杜绝 XSS；交互过渡限定离散 CSS 属性（禁止 `transition: all`），全局补齐 `:focus-visible` 焦点轮廓。