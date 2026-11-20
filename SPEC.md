# airshow-reader 技术规范与数据契约（SPEC.md）

## 1. 项目定位与目录结构

个人播客自动双语精读工具，抓取 *The Air Show* RSS，生成中文摘要、带时间戳 Highlight 与中英对照逐字稿，通过 GitHub Pages 发布。

- `SPEC.md`：核心架构、状态机与数据契约（本文件）
- `GLOSSARY.md`：常驻主播名单与航空专业术语表（由脚本运行时读取注入 Prompt）
- `ITERATION.md`：当前迭代里程碑、验收标准与交接记录
- `requirements.txt`：Python 依赖
- `scripts/process.py`：主处理脚本
- `data/episodes.json`：已处理单集全量数据（按 `pub_date` 降序）
- `docs/data/episodes.json`：`data/episodes.json` 的同步副本（供 Pages 相对路径读取）
- `docs/index.html`：静态单页阅读器
- `.github/workflows/update.yml`：GitHub Actions 定时任务

---

## 2. 后端处理状态机（`scripts/process.py`）

单集处理必须严格按以下状态流转，禁止跳过清理或半成品落盘：

```text
[1. FETCH_RSS] 抓取 [https://feeds.buzzsprout.com/2329718.rss](https://feeds.buzzsprout.com/2329718.rss)
   │ (失败：记录 ERROR 并以非零码退出)
   ▼
[2. FILTER_NEW] 对比 data/episodes.json 已有 guid，筛选最新未处理的 N 集（--limit）
   │ (无新单集：打印日志并正常退出 0)
   ▼
[3. DOWNLOAD_AUDIO] 流式下载 MP3 至系统临时目录；ffprobe 获取 duration_sec
   ▼
[4. CHUNK_AUDIO] ffmpeg 转为单声道 64k mp3 并按 --chunk-sec（默认 600s）切片
   │ (若开启 --dry-run：打印切片清单后直接跳至 [8. CLEANUP])
   ▼
[5. CHANNEL_B] 逐片上传 Gemini File API → 注入 GLOSSARY.md 转写+翻译 → 立即删除云端文件
   │ (受 --max-chunks 限制；遇 429/503 指数退避最多 5 次；片间休眠 10s)
   │ (单集内任一切片最终失败：中止本集，不写入 JSON，跳转 [8. CLEANUP])
   ▼
[6. CHANNEL_A] 将拼合的纯文本带时间戳逐字稿单次发给 Gemini，生成摘要与 Highlights
   ▼
[7. ATOMIC_SAVE] 合并数据、按 pub_date 降序 → 写 .tmp → os.replace 覆盖 data/episodes.json → 同步复制到 docs/data/episodes.json
   ▼
[8. CLEANUP] finally 块强制删除本地临时目录与残留云端文件
```

---

## 3. CLI 参数与可观测性规范（Observability）

### 必需命令行参数

- `--limit N`（int，默认 `1`）：单次最多处理的新单集数量。
- `--chunk-sec S`（int，默认 `600`）：音频切片时长（秒）。
- `--dry-run`（flag，默认关闭）：演习模式。执行 `[1]→[4]` 后打印切片与计划摘要，**不调用 Gemini API、不修改 JSON 文件**，随后清理临时文件退出。
- `--max-chunks M`（int，默认 `0` 表示全部）：仅处理前 M 个切片，用于低成本快速验证 AI 链路。

### 统一日志格式

所有标准输出必须带统一前缀，方便人工验收：

- `[AIRSHOW][RSS] ...`
- `[AIRSHOW][FILTER] ...`
- `[AIRSHOW][DOWNLOAD] ...`
- `[AIRSHOW][FFMPEG] ...`
- `[AIRSHOW][CH-B] ...`
- `[AIRSHOW][CH-A] ...`
- `[AIRSHOW][SAVE] ...`
- `[AIRSHOW][CLEANUP] ...`
- `[AIRSHOW][ERROR] ...`

### 模型与 SDK 约束

- 强制使用 `google-genai`（`from google import genai`），禁止使用旧版 `google-generativeai`。
- 环境变量：`GEMINI_API_KEY`（必填，在 `main()` 开头校验，仅非 `--dry-run` 时校验）、`GEMINI_MODEL`（默认 `"gemini-3.5-flash-lite"`，若指定了 `GEMINI_FALLBACK_MODEL` 则在主模型重试耗尽后自动切换）。

---

## 4. `data/episodes.json` 数据契约（Schema-First）

严格锁定以下 `snake_case` 键名，前后端禁止私自增删或改名：

```json
{
  "schema_version": "1.0",
  "podcast_title": "The Air Show",
  "rss_url": "[https://feeds.buzzsprout.com/2329718.rss](https://feeds.buzzsprout.com/2329718.rss)",
  "updated_at": "2026-11-20T08:00:00Z",
  "episodes": [
    {
      "guid": "Buzzsprout-16000000",
      "title": "Episode Title Here",
      "pub_date": "2026-11-18T10:00:00Z",
      "duration_sec": 3420,
      "audio_url": "[https://www.buzzsprout.com/2329718/episodes/16000000.mp3](https://www.buzzsprout.com/2329718/episodes/16000000.mp3)",
      "summary_zh": "200-350 字中文核心摘要，概括核心议题、观点分歧与行业结论。",
      "key_takeaways_zh": [
        "核心要点 1（一句话结论）",
        "核心要点 2（一句话结论）",
        "核心要点 3（一句话结论）"
      ],
      "highlights": [
        {
          "start_sec": 412,
          "title_zh": "精简中文标题（15 字以内）",
          "summary_zh": "2-3 句中文解读，说明该片段的核心论点或关键数据。",
          "quote_en": "Short original English quote from this segment."
        }
      ],
      "transcript": [
        {
          "id": 1,
          "start_sec": 0,
          "speaker": "Jon Ostrower",
          "en": "Welcome back to The Air Show...",
          "zh": "欢迎回到 The Air Show……"
        }
      ]
    }
  ]
}
```

### 字段校验硬约束

- `schema_version`：固定 `"1.0"`。
- `pub_date` / `updated_at`：ISO-8601 UTC（`YYYY-MM-DDTHH:MM:SSZ`）；`episodes` 按 `pub_date` **降序**。
- `duration_sec` / `start_sec`：非负整数；所有 `start_sec` 不得超过 `duration_sec` 且按升序排列。
- `highlights`：4–8 条；`transcript`：按 2–4 句自然语义块分段，`id` 从 `1` 连续递增。
