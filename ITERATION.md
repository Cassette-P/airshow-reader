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

---

## 阶段性总结：v3.0 多播客聚合阅读器已全面落地

- **M6-A（后端 & 数据无损迁移）**：已完成 `podcasts.json` 注册表驱动、`scripts/process.py` 支持 `--show` 调度与 RSS Show Notes 深度清洗注入、8 期已有单集完整无损迁移至 `docs/data/airshow/`、`docs/data/shows.json` 全局清单生成、`docs/data/sinica/` 防 404 初始化、Sinica ART19 抓取与 33 片切片演习验证。
- **M6-B（前端多播客自适应阅读器）**：已完成首屏清单拉取、顶部胶囊 Tab 切换、动态主题重音色注入、双层内存缓存与请求防竞态锁、空节目友好空状态、常驻主播与流动嘉宾 8 色确定性色标、URL Hash 编码与向后兼容。所有资源经静态服务本地校验返回 HTTP 200，内联 JS 经 Node.js 语法校验 0 错误。
