# airshow-reader 技术规范与数据契约（SPEC.md · v2.0）

## 1. 项目定位与目录结构

个人播客自动双语精读工具，抓取 *The Air Show* RSS，生成中文核心摘要、带时间戳 Highlight 与中英对照逐字稿，通过 GitHub Pages（`/docs`）发布。

v2.0 架构升级为**单一数据源 + 按单集拆分懒加载**，取消原有 `data/episodes.json` 冗余双副本：

- `README.md`：项目简介、本地使用与云端运维指南
- `SPEC.md`：核心架构、状态机与 v2.0 数据契约（本文件）
- `GLOSSARY.md`：常驻主播名单、航空专业术语表与翻译校对红线（由脚本运行时读取注入）
- `ITERATION.md`：迭代里程碑总览、当前活跃工单与交接记录
- `requirements.txt`：Python 依赖
- `.gitignore`：本地缓存与临时预览文件忽略规则
- `scripts/process.py`：主处理脚本（含 v1→v2 幂等数据迁移能力）
- `docs/data/index.json`：全量单集轻量总目录（按 `pub_date` 降序，含核心摘要与要点，供前端首屏加载）
- `docs/data/episodes/<safe_guid>.json`：按单集独立存储的完整精读数据（含 `highlights` 与全量 `transcript`，供前端按需懒加载）
- `docs/index.html`：静态自适应单页阅读器
- `.github/workflows/update.yml`：GitHub Actions 定时巡检与手动触发工作流

---

## 2. 后端处理状态机（`scripts/process.py`）

单集处理必须严格按以下状态流转，禁止跳过清理或半成品落盘：

```text
[0. MIGRATE_IF_NEEDED] 若 docs/data/index.json 不存在且旧版 v1 文件（docs/data/episodes.json 或 data/episodes.json）存在：
   │ 自动拆分迁移至 docs/data/episodes/<safe_guid>.json + docs/data/index.json，并清理旧版单文件
   │ (若开启 --migrate-only：迁移完成后直接正常退出 0)
   ▼
[1. FETCH_RSS] 抓取 https://feeds.buzzsprout.com/2329718.rss
   │ (失败：记录 ERROR 并以非零码退出)
   ▼
[2. FILTER_NEW] 对比 docs/data/index.json 已有 guid，叠加 --max-age-days 时间窗筛选最新未处理的 N 集（--limit）
   │ (无符合条件新单集：打印日志并正常退出 0)
   ▼
[3. DOWNLOAD_AUDIO] 四级抗 403 下载器（Apple Podcasts UA → Overcast UA → curl_cffi → curl）流式下载 MP3 至临时目录；ffprobe 获取 duration_sec
   ▼
[4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 180s）切片
   │ (若开启 --dry-run：打印切片清单后直接跳至 [8. CLEANUP])
   ▼
[5. CHANNEL_B] 逐片上传 Gemini File API → 流式调用（低思考模式 + 注入 GLOSSARY.md）转写与翻译 → 立即删除云端文件
   │ (受 --max-chunks 限制；按 "MM:SS" 输出片内时间戳并做单片钳位，严禁跨片 sort()；片间休眠 CHUNK_SLEEP_SEC，默认 2s)
   │ (单集内任一切片在候选池重试耗尽后仍失败：中止本集，不写入 JSON，跳转 [8. CLEANUP])
   ▼
[6. CHANNEL_A] 将拼合的纯文本带时间戳逐字稿流式单次发给旗舰模型，生成摘要、要点与 Highlights
   ▼
[7. ATOMIC_SAVE] 若 --max-chunks > 0：仅写入 docs/data/_smoke_preview.json（不碰正式库）
   │ 否则：原子写入 docs/data/episodes/<safe_guid>.json（.tmp → os.replace）
   │      再原子更新 docs/data/index.json（按 pub_date 降序，.tmp → os.replace）
   ▼
[8. CLEANUP] finally 块强制删除本地临时目录与残留云端文件
   │ (若本轮所有待处理单集全部失败，进程以非零退出码 1 退出)
```

---

## 3. CLI 参数、环境变量与可观测性规范

### 必需命令行参数

- `--limit N`（int，默认 `1`）：单次最多处理的新单集数量。
- `--chunk-sec S`（int，默认 `180`）：音频切片时长（秒）；建议保持 `180`（最多 `240`），确保时间锚点高精度。
- `--max-age-days D`（int，默认 `0`）：仅处理最近 D 天内发布且未入库的新单集；`0` 表示不限制发布时间（用于手动补抓历史节目）。
- `--dry-run`（flag，默认关闭）：演习模式。执行 `[1]→[4]` 后打印切片与计划摘要，**不调用 Gemini API、不修改 JSON 文件**，清理临时文件后退出。
- `--max-chunks M`（int，默认 `0` 表示全部）：仅处理前 M 个切片；当 `M > 0` 时进入冒烟预览模式，结果仅写入 `docs/data/_smoke_preview.json`，绝不修改正式库。
- `--migrate-only`（flag，默认关闭）：仅执行 v1→v2 本地数据拆分迁移，不访问网络、不调用 Gemini API，完成后退出 `0`。

### 环境变量与模型路由策略（Paid Tier 优先）

> **空串兜底原则**：GitHub Actions 未配置对应 Repository Variable 时会注入空字符串 `""`。代码读取以下全部环境变量时必须做 `.strip()` 判空，空字符串视同未设置并回退到默认值。

- **SDK 与流式约束**：强制使用新版 `google-genai`（`from google import genai`）及流式接口 `generate_content_stream`，禁止使用已废弃的 `google-generativeai`。
- `GEMINI_API_KEY`：必填（在 `main()` 开头校验，仅非 `--dry-run` 且非 `--migrate-only` 时校验）。
- **通道 B（逐片转写与双语翻译）**：
  - 默认候选模型池：`["gemini-3.8-flash", "gemini-4-hydrogen", "gemini-3.5-flash-lite"]`
  - 环境变量覆盖：`GEMINI_MODEL_CH_B`（兼容旧变量 `GEMINI_MODEL`），非空时置顶并去重。
  - 思考等级：`GEMINI_THINKING_CH_B`（默认 `"low"`），通过 `types.ThinkingConfig(thinking_level=...)` 传入以控制成本与延迟；若回退到不支持该参数的模型报错，自动剥离 `thinking_config` 重试。
- **通道 A（全局摘要与 Highlights）**：
  - 默认候选模型池：`["gemini-4-carbon", "gemini-4-neon", "gemini-3.8-flash"]`
  - 环境变量覆盖：`GEMINI_MODEL_CH_A`，非空时置顶并去重（Carbon 优惠到期后，只需在 GitHub Variables 中将 `GEMINI_MODEL_CH_A` 设为 `gemini-4-neon` 即可零代码切换）。
  - 思考等级：保持模型默认高思考，不限制。
- **片间休眠**：`CHUNK_SLEEP_SEC`（默认 `"2"`，浮点数/整数秒），Paid Tier 下缩短片间等待时间。
- **容灾与长退避策略**：按顺序遍历候选模型池，单个模型最多连续尝试 **5 次**，失败后的冷却间隔依次为 **`15s → 20s → 25s → 30s`**；遇 `404` / `NOT_FOUND` 立即跳过当前模型不等待；切换至池内下一个模型前额外休眠 `10s`。

### 统一日志格式

所有标准输出必须带统一前缀：
`[AIRSHOW][MIGRATE]`、`[AIRSHOW][RSS]`、`[AIRSHOW][FILTER]`、`[AIRSHOW][DOWNLOAD]`、`[AIRSHOW][FFMPEG]`、`[AIRSHOW][CH-B]`、`[AIRSHOW][CH-A]`、`[AIRSHOW][SAVE]`、`[AIRSHOW][CLEANUP]`、`[AIRSHOW][ERROR]`。

---

## 4. v2.0 数据契约（Schema-First）

严格锁定以下 `snake_case` 键名，前后端禁止私自增删或改名。

### 文件名安全归一化规则（`safe_guid`）
单集文件名由 `guid` 经 `re.sub(r"[^A-Za-z0-9._-]+", "_", guid).strip("_")` 生成，例如 `Buzzsprout-20148830` → `episodes/Buzzsprout-20148830.json`。

### 4.1 轻量总目录：`docs/data/index.json`

前端首屏仅拉取此文件（每集约 1.5 KB），用于渲染单集列表、摘要预览与跨集检索：

```json
{
  "schema_version": "2.0",
  "podcast_title": "The Air Show",
  "rss_url": "https://feeds.buzzsprout.com/2329718.rss",
  "updated_at": "2026-11-21T08:00:00Z",
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

### 4.2 单集完整文件：`docs/data/episodes/<safe_guid>.json`

前端选中某期单集时按需 `fetch('./data/' + item.file)` 并缓存于内存 `Map`：

```json
{
  "schema_version": "2.0",
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

- `schema_version`：固定 `"2.0"`。
- `pub_date` / `updated_at`：ISO-8601 UTC（`YYYY-MM-DDTHH:MM:SSZ`）；`index.json` 的 `episodes` 按 `pub_date` **降序**排列。
- `duration_sec` / `start_sec`：非负整数；所有 `start_sec` 不得超过 `duration_sec` 且保持**单调非递减**（通道 B 须以 `"MM:SS"` 解析并按单片时长钳位，禁止跨片重排 `sort()`）。
- `highlights`：4–8 条；`transcript`：按 2–4 句自然语义块分段，`id` 从 `1` 连续递增。
- `index.json` 中的 `highlights_count`、`transcript_count`、`summary_zh`、`key_takeaways_zh` 必须与对应单集 JSON 文件完全一致。