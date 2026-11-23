# airshow-reader 迭代工单（ITERATION.md）

> 维护规则：仅保留里程碑总览 + 当前活跃里程碑的详细验收标准；已完成阶段压缩为一行结论。

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
- [x] **M7**：核心播放器体验升级与 PWA 化（2026-11-23 通过，完成进度条章节打点、断点续播与单集进度记忆、Media Session API 耳机线控/锁屏控制及离线安全 PWA 接入）
- [x] **M8-A**：精听模式套件（2026-11-23 通过，完成清晰/遮中文/全盲听三档刮刮乐模糊盲听、单句复读循环驱动与状态胶囊、桌面端键盘快捷键及说明弹窗）
- [x] **M8-B1**：Schema v3.1 知识卡片后端 + 便宜模型 `--enrich` 增量补跑通道（2026-11-23 通过，完成 `VocabNote` / `ReferenceItem` Schema 契约、Channel A 一体地产出、`--enrich` 幂等增量补跑引擎及 Actions 工作流集成）
- [ ] **M8-B2**：前端知识卡片展示与深度研读联动（下一活跃工单）

---

## 当前活跃工单：M8-B2

- **工单目标**：在 `docs/index.html` 中呈现单集专属的「重点词汇（Vocabulary Notes）」与「推荐参考（Referenced Works/Media）」知识卡片，联动音频定位与逐字稿高亮，打造纸面杂志深度研读体验。
- **技术要点与验收标准**：
  1. **纸面杂志卡片 UI**：延续 Magazine Paper × Liquid Glass 质感，Light/Dark 双模深度适配，排版优雅克制；
  2. **重点词汇卡片（Vocabulary Notes）**：展示英文表达原词、精准中文语境释义、地道用法/背景点拨，附带声学校准时间戳跳转按钮；
  3. **推荐参考卡片（References）**：分类展示书目（book）、文章（article）、媒体（media）或工具（other），呈现准确原名、创作者及推荐理由；
  4. **时间戳声学联动**：点击卡片上的时间戳秒数，调用 `seekAndPlay()` 瞬间定位至音频对应秒数，并联动触发逐字稿自动滚动高亮；
  5. **平滑降级与向前兼容**：当查看尚未补跑知识卡片的存量单集（缺失 `vocab_notes` 或 `references`，或为空列表）时，平滑隐藏板块或优雅降级，控制台保持 0 错误。
