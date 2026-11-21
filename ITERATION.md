# airshow-reader 迭代工单（ITERATION.md）

> 维护规则：仅保留里程碑总览 + 当前活跃里程碑的详细验收标准；已完成阶段压缩为一行结论。

## 里程碑总览（Vertical Slicing）

- [x] **M1**：后端管道骨架 + `--dry-run` 演习验收（2026-11-20 通过）
- [x] **M2**：Gemini 双通道精读与全量落盘（2026-11-20 通过，已生成 *Singapore's Brand and Network Struggles* 191 条双语逐字稿并同步至 `docs/data/episodes.json`）
- [x] **M2.5**：流式抗断连与分通道路由升级（2026-11-21 通过，完成 `generate_content_stream` 拼接与校验、分通道模型池 Channel B [`gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`] / Channel A [`gemini-3.8-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite`]、5 次 `[15s, 20s, 25s, 30s]` 长退避策略，全量 12 片及 Channel A 摘要验证落盘完成）
- [x] **M3**：自适应静态双语阅读器完成（2026-11-20 通过，完成航空财经杂志风单页阅读器、二分查找段落高亮、seekAndPlay 健壮跳转播放、双语模式切换与关键词搜索过滤）
- [x] **M4**：自动化工作流与上线准备完成（2026-11-21 通过，完成 `--max-age-days` 防历史倒灌机制、根目录 `.gitignore`、`.github/workflows/update.yml` 每 6 小时定时巡检与 `workflow_dispatch` 手动补抓/演习触发、原子变更检测与 GitHub Actions Bot 自动 Commit/Push 闭环，全流程归档完成）
- [x] **M5-A**：v2.0 后端分集存储与 Paid Tier 路由（2026-11-21 通过，完成 `docs/data/` 单一数据源与 `episodes/<safe_guid>.json` + `index.json` 分集拆分存储、`--migrate-only` 幂等数据迁移、Paid Tier 候选模型池路由与 Channel B 低思考配置及 GitHub Actions 变量注入）
- [x] **M5-B**：前端 `docs/index.html` 适配 v2.0 懒加载（2026-11-21 通过，完成首屏仅加载 `index.json` 轻量总目录、单集元数据与摘要即刻渲染、内存 `Map` 详情按需懒加载、防快速切集竞态校验、搜索联动及 URL Hash 状态同步）
- [x] **M6-A**：v3.0 多播客配置化后端与无损迁移（2026-11-21 通过，完成 `podcasts.json` 注册表与 `--show` 参数调度、`GLOSSARY.md` 迁移至 `glossaries/airshow.md`、旧版 8 期数据 100% 无损迁移至 `docs/data/airshow/` 并升级 schema_version 为 3.0、`docs/data/shows.json` 全局清单生成、空节目目录防 404 初始化、RSS Show Notes 动态清洗注入与去硬编码、Channel A highlights 放宽至 4-10 条、Sinica ART19 抓取与 4 级防 403 下载及 180s 切片演习验证通过）
- [x] **M6-B**：前端 `docs/index.html` 多节目切换与动态呈现（2026-11-21 通过，完成首屏 `shows.json` 清单拉取、顶部 Tab 动态胶囊切换与单集数徽章、节目专属主题重音色 `--show-accent` 动态切换与深浅模式适配、两级缓存 `showIndexCache` 与 `episodeCache`、跨节目与切集防竞态锁、Sinica 空状态优雅降级防 404/防 JS 报错、8 种流动嘉宾确定性哈希色标与 4 位常驻主播专属色标、URL Hash `#<show_id>/<encodeURIComponent(guid)>` 及旧版平滑兼容、本地静态服务 HTTP 200 与 `node --check` 语法校验通过）
- [x] **M7**：Unslop 前端视觉重构与刊物级双语研读器（2026-11-21 通过，根据 5 行 Direction 与 5 项约束完成九轮 Unslopping：合并 52px 单层顶栏、构建 Issue Archive 往期选集抽屉方案 A 并下线原生 `<select>`、消除硬编码 `--accent-blue` 建立高对比度亮暗模式主题色、重塑 4px 内敛说话人印刷体徽章、构建衬线扉页/原声引文与无衬线正文字体栈、引入纯 CSS 声学微刻度标尺、消除全员 9999px 药丸与发光阴影，经浏览器子代理在 1440px 桌面端、390px 移动端、Air Show 与 Sinica 双播客深浅双模式下实机验收通过，控制台 0 报错，彻底核销 unslop_audit 15 项 AI Slop 问题）
- [x] **M7.1**：中文高分屏字重优化 + 声波书页 Icon + 2 处审出瑕疵修复（2026-11-21 通过，完成 Google Fonts Noto Sans SC 可变字体引入与 `--zh-body-weight` [亮 480 / 暗 460] 覆盖中文核心阅读元素、次级文字对比度微调；创建 `docs/favicon.svg` 并替换顶栏 Monogram 为「声波书页」内联 SVG，实现浏览器标签页 Favicon 随播客节目实时变色；修复 Sinica 模式下 Tint/Subtle 色串回天蓝瑕疵；修复声学标尺刻度为 5 等分动态渲染；本地静态服务 HTTP 200 与 Playwright 真实浏览器全场景验收通过，控制台 0 报错）

---

## 阶段性总结：v3.1.1 精修版刊物级双语研读器已全面落地

- **M6-A（后端 & 数据无损迁移）**：已完成 `podcasts.json` 注册表驱动、`scripts/process.py` 支持 `--show` 调度与 RSS Show Notes 深度清洗注入、8 期已有单集完整无损迁移至 `docs/data/airshow/`、`docs/data/shows.json` 全局清单生成、`docs/data/sinica/` 防 404 初始化、Sinica ART19 抓取与 33 片切片演习验证。
- **M6-B（前端多播客自适应阅读器）**：已完成首屏清单拉取、顶部胶囊 Tab 切换、动态主题重音色注入、双层内存缓存与请求防竞态锁、空节目友好空状态、常驻主播与流动嘉宾 8 色确定性色标、URL Hash 编码与向后兼容。
- **M7（Unslop 前端出版物视觉重塑）**：完成 `docs/index.html` 从 AI 模板原型到权威刊物级界面蜕变（52px 单层顶栏、Issue Archive 选集抽屉、高对比度主题色、4px 内敛徽章、Serif/Sans 排印分工、CSS 声学微刻度标尺）。
- **M7.1（精修补丁）**：
  1. **中文正文字重升级**：异步加载 Google Fonts `Noto Sans SC:wght@400..700`，`--font-sans` 补充高质量中文备用字体；引入 `--zh-body-weight`（亮色 480 / 暗色 460 防暗底白字发胖），覆盖逐字稿、摘要、Takeaways 与目录卡片；微调次级对比度（`#3f3f46` / `#c4c4cc`），根治 4K 100% 缩放下中文细笔画发虚；
  2. **「声波书页（Acoustic Folio）」标志性 Icon**：新建 `docs/favicon.svg`（左 3 声波脉冲 + 右 3 行段落线），顶栏 Brand Monogram 升级为内联 SVG 并响应品牌主题色；新增 `updateDynamicFavicon(bgHex)` 借助 Data URI SVG 实现浏览器标签页图标随选中节目（Air Show `#0a2540` / Sinica `#9b2226`）实时同步变色；
  3. **修复多节目 Tint 串色**：在 `applyShowTheme(show)` 中根据激活主题色动态生成 `--accent-tint`（亮 0.10 / 暗 0.15）与 `--accent-subtle`（亮 0.05 / 暗 0.07），Sinica 模式下搜索高亮与「跟随音频」激活态彻底摆脱蓝色残留；
  4. **修复声学标尺刻度写死**：在 `renderArticleMeta` 中按单集时长 `ep.duration_sec` 动态渲染 0%、25%、50%、75%、100% 五等分刻度，完美兼容 35 分钟短节目与 97 分钟深度长单集；
  5. **工程验收**：提取内联 JS 经 `node --check` 校验 0 错误；本地静态服务器验证 `/index.html` 与 `/favicon.svg` 均返回 HTTP 200；Playwright 真实浏览器在切换节目与深浅主题下实机运行，控制台 0 错误 0 警告。
