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
- [x] **M9-C**：前端零构建原生 ES Modules + CSS 拆分（2026-11-23 通过，将 212 KB 单文件拆分为 3 个原生 CSS 文件与 10 个原生 ES Modules，彻底清除内联事件处理，升级 `sw.js` 缓存版本至 `airshow-reader-v2`，Headless 浏览器 CDP 真实 E2E 验证全量通过）
- [x] **M9-D**：全量文档收口与规范对齐（2026-11-23 通过，`README.md`、`SPEC.md` 与 `ITERATION.md` 全面对齐 v3.3 最新工程基线，同步真实目录树、校验门禁与 Media Session artwork 规范）

---

## 当前稳定基线：v3.3 工程减负与全量里程碑（M1–M9）均已验收通过

- **当前线上资产规模**：2 档活跃播客（*The Air Show* 11 期 + *Sinica Podcast* 7 期，共 18 期），全量完成 v3.1 知识卡片补跑与术语质检，CI 门禁与定时巡检每 6 小时自动运行。
- **架构演进成果**：
  - 后端模块化：`scripts/process.py` + `scripts/pipeline/`，逻辑解耦，高内聚低耦合；
  - 自动化门禁：`scripts/validate_data.py` 保障每次自动落盘数据 100% 契约保真；
  - 前端零构建：`docs/css/` + `docs/js/`，完全保持原生 Zero-Build 特性，开箱即用，消灭单文件维护负担。
- **运维备忘**：2026-12-01 起可在 GitHub Actions Variables 新增 `GEMINI_MODEL_CH_A=gemini-4-neon`（`GEMINI_THINKING_CH_A` 留空自动适配 `xhigh` 并带降级保护），零代码无缝切换新旗舰。
- **后续可选演进池（Backlog，按需排期）**：
  1. **跨单集全局检索**：直接索引全部 `index.json`，支持跨播客、跨期一键搜话题直达；
  2. **金句剪藏与笔记导出**：单句收藏与批注持久化，支持一键导出带时间戳的中英 Markdown 至 Obsidian / Notion；
  3. **接入第 3 档播客**：在 `podcasts.json`、`glossaries/` 与 `update.yml` 零代码扩展新节目。
