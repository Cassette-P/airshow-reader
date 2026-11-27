# airshow-reader 迭代工单（ITERATION.md）

> 维护规则：仅保留里程碑总览 + 当前活跃里程碑或最新基线验收状态；已完成阶段压缩为一行结论。

## 里程碑总览（Vertical Slicing）

- [x] **M1**：后端管道骨架 + `--dry-run` 演习验收（2026-11-20 通过）
- [x] **M2**：Gemini 双通道精读与全量落盘（2026-11-20 通过，生成 *Singapore's Brand and Network Struggles* 双语逐字稿）
- [x] **M2.5**：流式抗断连与分通道路由升级（2026-11-21 通过，`generate_content_stream` 拼接校验与 5 次长退避策略落盘）
- [x] **M3**：自适应静态双语阅读器首版完成（2026-11-20 通过，完成二分同步高亮、`seekAndPlay`、双语模式切换与关键词检索）
- [x] **M4**：自动化工作流与上线准备完成（2026-11-21 通过，`--max-age-days` 防历史倒灌、6 小时定时巡检与 GitHub Actions Bot 自动提交闭环）
- [x] **M5-A**：v2.0 后端分集存储与 Paid Tier 路由（2026-11-21 通过，`index.json` + `episodes/<safe_guid>.json` 拆分与 `--migrate-only` 幂等迁移）
- [x] **M5-B**：前端 `docs/index.html` 适配 v2.0 懒加载（2026-11-21 通过，首屏目录轻载、内存 `Map` 缓存与防竞态校验）
- [x] **M6-A**：v3.0 多播客配置化后端与无损迁移（2026-11-21 通过，`podcasts.json` 注册表、`glossaries/<show_id>.md`、`shows.json` 清单、Show Notes 动态注入、Sinica ART19 抓取与四级抗 403 下载）
- [x] **M6-B**：前端 `docs/index.html` 多节目切换与动态呈现（2026-11-21 通过，多节目 Tab、动态 `--show-accent`、两级缓存、空状态降级与编码 Hash 书签）
- [x] **M6.5**：18 期全量质检加固与 Channel A 思考等级路由（2026-11-23 通过，完成双节目词表扩充与防膨胀审校、`normalize_speaker()` 去头衔清洗、7 处存量硬伤订正及 Channel A `thinking_level` 按模型自动适配与 `xhigh → high → None` 平滑降级）
- [x] **M7**：核心播放器体验升级与 PWA 化（2026-11-23 通过，完成进度条章节打点、断点续播与进度记忆、Media Session 锁屏/线控（10s 步长）、Network-Only `sw.js` PWA，以及 iOS 独立模式透明状态栏与 `--pwa-top-extra: 24px` 安全区热修）
- [x] **M8-A**：精听模式套件（2026-11-23 通过，完成清晰/遮中文/全盲听三档刮刮乐模糊盲听、单句复读循环驱动与状态胶囊、桌面端全套键盘快捷键及说明弹窗）
- [x] **M8-B1**：Schema v3.1 知识卡片后端 + `--enrich` 增量补跑通道（2026-11-23 通过，完成 `VocabNote` / `ReferenceItem` 契约、商业广告过滤红线、ASR 拼写纠偏、`start_sec` 升序排序、Actions `enrich_only` 集成及 18 期存量单集全量补跑）
- [x] **M8-B2**：前端知识卡片展示与深度研读联动（2026-11-23 通过，完成杂志风原生 `<details>` 折叠面板、地道表达双列网格与时间戳跳转、书单/参考资源分类徽章渲染、切集重置与平滑空态降级）
- [x] **M9-A**：Schema 校验器与 CI 门禁（2026-11-23 通过，完成 `scripts/validate_data.py`，全量覆盖 `shows.json`、`index.json` 与全部 18 期 `episodes/*.json` 的 Schema 合法性、时间戳单调性与双向对齐校验，集成进 GitHub Actions 工作流作为发布阻断门禁）
- [x] **M9-B**：后端流水线分包解耦（2026-11-23 通过，将 1400+ 行单文件重构拆分为 `scripts/pipeline/` 9 个职责单一的模块，主入口 `process.py` 精简至 200 行，100% 保持 CLI 参数与环境变量向前兼容）
- [x] **M9-C**：前端零构建原生 ES Modules + CSS 拆分（2026-11-23 通过，将 212 KB 单文件拆分为 3 个原生 CSS 文件与 10 个原生 ES Modules，彻底清除内联事件处理，升级 `sw.js` 缓存版本至 `airshow-reader-v2`（后续 M10 接入 TAL 封面已升级为 `airshow-reader-v3`），Headless 浏览器 CDP 真实 E2E 验证全量通过）
- [x] **M9-D**：全量文档收口与规范对齐（2026-11-23 通过，`README.md`、`SPEC.md` 与 `ITERATION.md` 全面对齐 v3.3 最新工程基线，同步真实目录树、校验门禁与 Media Session artwork 规范）
- [x] **M10**：第 3 档播客 *This American Life* 接入与后端长音频抗断连加固（2026-11-24 通过）
  - **后端稳定性 3 项加固**：
    1. **Channel B 断点缓存**：全量转写完成后原子落盘至 `.cache/ch_b/<show_id>__<safe_guid>.json`，重试命中直接跳过下载、切片与 CH-B，原子落盘成功后自动清理；支持 `--no-cache` 与冒烟隔离。
    2. **Channel A 逐字稿瘦身**：严格仅向旗舰模型传递 `[MM:SS] <speaker>: <en>` 英文原声，剔除 `zh` 译文削减近半 Token 并记录 `input_chars` 观测。
    3. **传输断连容灾与连续断连降级**：显式配置 300 秒超时（300,000ms），断连/超时立即重建 `genai.Client` 杜绝复用半关 Socket；同一思考档位连续 2 次断连触发平滑降级链（`xhigh` → `high` → `low` → `None`）。
  - **This American Life 叙事特稿体接入**：
    - `podcasts.json` 注册表扩展 `content_style: "narrative"` 与 `scraper_hook: "tal"`；
    - `scripts/pipeline/hooks_tal.py` 实现官网无广告归档 MP3 替换、多幕纯净 Show Notes 与官网 Transcript 27 位嘉宾花名册注入，配合 GUID + 期号双重去重机制；
    - `glossaries/tal.md` 编制常驻主持 Ira Glass 及特稿翻译红线；
    - 前端浅深色主题（`#2D6A4F` / `#74C69D`）、`Ira Glass` 专属印章色标（`spk-ira`）、高清封面 `artwork-tal-512.png` 与带空格/URL 的 Hash 路由深度支持；
  - **验收落地**：第 436 期 *The Psychopath Test* 全量入库，19 期单集严格通过 `validate_data.py --strict` 门禁，13 项稳定性单元测试全通，Browser Agent 多端质检 Console 0 报错。

- [x] **M11**：Channel A 传输层升级与 Interactions API 异步轮询加固（2026-11-24 通过）
  - **双模传输架构**：
    1. **主路径（`interactions_bg`）**：默认锁定 `gemini-4-carbon @ high`，采用 `client.interactions.create(background=True, store=True)` 异步提交 + 短连接轮询（默认 10s 间隔，上限 900s 可由 `CH_A_BG_TIMEOUT_SEC` 覆盖）；
    2. **入参严格对齐 v1 规范**：彻底移除顶层 `response_mime_type`，采用 `v1` 多态 `response_format={"type": "text", "mime_type": "application/json", "schema": ...}`，保留 `max_output_tokens=65536` 防止长思考挤占输出；
    3. **轮询局部容错**：轮询遇偶发网络断连仅重建 Client 继续轮询原 `interaction.id`（最多容忍 5 次连续异常），绝不重交计费；
    4. **非 thinking 类 400 快速失败回退**：遇到无效参数/不支持等错误立即打 WARN 日志并快速回退至 `stream_with_thoughts`（`include_thoughts=True` 心跳保活），杜绝无效空转重试；
  - **验收落地**：
    - `requirements.txt` 升级 `google-genai>=3.6.1`；
    - 19 项稳定性单元测试 100% 通过（涵盖 Interactions 提交/轮询/完成、轮询断连 Client 重建、思维分片跳过、超时外层重试与 400 快速回退）；
    - `validate_data.py --strict` 零报错通过；
    - 真实冒烟实测一次性命中 `gemini-4-carbon @ high`，`transport=interactions_bg`，根除 GitHub Actions 与本地代理断流回退 `gemini-3.8-flash` 隐患。

- [x] **M12**：参考资源多源直达外链、存量全量回填与 Channel B 模型升级（2026-11-27 通过，Schema v3.2）
  - **后端确定性解析层与 `--enrich-links`**：
    1. `rss.py` 与 `hooks_tal.py` 清洗 HTML 时保留正文有效超链接为 Markdown `[锚文本](URL)`，自动剥离 `utm_*` 等追踪参数并过滤商业赞助/短链；
    2. 新增 `scripts/pipeline/links.py`，按内容细类与语种路由对接 MediaWiki Action API、Goodreads `book/auto_complete`、豆瓣 `j/subject_suggest`、Apple iTunes Search API 与 RSS `<channel><link>`，内置同名实体主创/体裁强消歧、防维基消歧义页及反搜索页/反短链红线；
    3. 扩展 `models.py`（`ReferenceLink` 与 `normalize_reference_links` 固定优先级排序、去重、上限 4 条）、`prompts.py` 与 `runner.py` / `process.py`（接入主流程 `[6.5]`、`--enrich` 与免 `GEMINI_API_KEY` 的 `--enrich-links` / `--force-links` 幂等补链通道）；
    4. Channel B 默认首选模型升级至 `gemini-4-hydrogen`（候选池 `["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"]`）。
  - **校验门禁、单元测试与前端呈现**：
    - `scripts/validate_data.py` 升级至 v3.2 门禁（强校验 `source`/`label` 映射、平台直达路径正则、上限 4 条、反重复、硬阻断搜索页/短链，`--strict` 下缺失 `links` 升级阻断）；
    - 新增 `tests/test_reference_links.py`，全量 **47 项单元测试** 100% 通过；
    - `docs/js/reader.js` 与 `docs/css/components.css` 落地刊物风 `.ref-links-row` / `a.ref-link-pill` 外链胶囊（`target="_blank"`、`rel="noopener noreferrer"`、11×11 箭头 SVG、空数组零空容器），`docs/sw.js` 升级至 `airshow-reader-v10`。
  - **存量 20 期全量补链、人工级质检与验收**：
    - 全程零 Gemini API 调用完成存量 20 期 / 72 条参考资源补链与逐条人工级 HTTP/标题核验（命中 **70/72 条、103 枚** 真实直达外链，2 条不可达站点依宁缺毋滥留 `[]`，零同名误配/搜索页/死链），二次执行 `--enrich-links` 严格幂等（`Attempted: 0, Skipped: 20`）；
    - `validate_data.py --strict`（0 错 0 告警）与桌面 1280px / 移动 375px / 窄屏 320px 浅深双模浏览器验证全通（Console 0 报错、零横向溢出）。

- [x] **M13**：Channel B 独立 Free Tier API 路由、5 RPM 滑动窗口限速与 300s 切片升级（2026-11-28 通过）
  - **Free-First + Paid-Overflow 双 Key 架构**：
    1. 新增 `GEMINI_API_KEY_CH_B`（独立无绑卡 GCP 项目 Free Tier Key）及 `CH_B_CHUNK_SEC`（默认 `300s`）、`CH_B_FREE_RPM`（默认 `5`）、`CH_B_FREE_RPD`（默认 `20`）、`CH_B_FREE_OVERFLOW_TO_PAID`（默认 `true`），未配置 Free Key 时 100% 向后兼容单 Key Paid 模式；
    2. 切片默认时长由 `180s` 升级为 `300s`（同步更新 CH-B Prompt 时间锚点指令并显式设定 `max_output_tokens=32768`），使三档播客单集（*Air Show* ≤11、*TAL* ≤12、最长 99 分钟 *Sinica* ≤20）全部落入 `gemini-4-hydrogen` 20 RPD 免费额度内；
    3. 新增 `SlidingWindowRateLimiter`（65s 滑动窗口 ≤5 次 + 12.4s 最小起始间隔双保险）、429 RPM/RPD 精准分类器（`PerMinute` 解析 `retryDelay` 等待重试，`PerDay`/`limit: 0` 零重试立即熔断并跨项目重传至 Paid Client 以同模型续跑）及 Free 端 3 次快重试（`15s → 20s`）+ 连续 2 片 `503 UNAVAILABLE` 拥堵熔断；
    4. 同步透传新 Secret 与 Variables 至 `.github/workflows/update.yml` 并对齐 `README.md` / `SPEC.md`。

---

## 当前稳定基线：v3.7 Channel B Free Tier 双 Key 路由与多源外链基线

- **当前线上资产规模**：3 档活跃播客（*The Air Show* 11 期 + *Sinica Podcast* 7 期 + *This American Life* 2 期，共 20 期 / 72 条参考资源 / 103 枚核验直达外链），全量覆盖 v3.2 参考资源多源外链、v3.1 知识卡片与分幕导读，CI 门禁与定时巡检持续自动运行。
- **架构演进成果**：
  - 双通道 Gemini 4 + Free/Paid 成本优化：Channel A 默认 Paid Tier `gemini-4-carbon @ high`（Interactions API 异步轮询 + `stream_with_thoughts` 心跳回退），Channel B 默认 `gemini-4-hydrogen`（`300s` 切片、`GEMINI_API_KEY_CH_B` Free Tier 5 RPM / 20 RPD 优先 + Paid 同模型溢出兜底 + 断点缓存 `.cache/ch_b/`）；
  - 确定性多源外链引擎：`scripts/pipeline/links.py` 联合 Show Notes 一手链接、MediaWiki / Goodreads / 豆瓣 / iTunes 公开接口与主创消歧规则自动挂链，严禁 LLM 凭记忆盲猜数字 ID；
  - 弹性容灾与门禁：300s 超时、Socket 断连重建、思考档位平滑降级与 `validate_data.py --strict` 发布阻断门禁；
  - 风格自适应：`analytical` 深度分析体与 `narrative` 特稿分幕体双引擎并存驱动。
- **后续可选演进池（Backlog，按需排期）**：
  1. **跨单集全局检索**：直接索引全部 `index.json`，支持跨播客、跨期一键搜话题直达；
  2. **金句剪藏与笔记导出**：单句收藏与批注持久化，支持一键导出带时间戳的中英 Markdown 至 Obsidian / Notion。
