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

---

## 阶段总结与交付状态

全部里程碑（M1 ~ M5-B）均已完成并验收通过。项目已全面具备单一数据源（`docs/data/`）、分集按需懒加载、Paid Tier 双通道流式抗断连生成与 GitHub Pages 静态极速呈现能力。
