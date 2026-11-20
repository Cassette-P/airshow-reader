# airshow-reader 迭代工单（ITERATION.md）

> 维护规则：仅保留里程碑总览 + 当前活跃里程碑的详细验收标准；已完成阶段压缩为一行结论。

## 里程碑总览（Vertical Slicing）

- [x] **M1**：后端管道骨架 + `--dry-run` 演习验收（2026-11-20 通过，已验证 4 切片与临时目录清理）
- [x] **M2**：Gemini 双通道精读与真实数据落盘（2026-11-20 通过，已跑通 1 集 4 切片 191 条逐字稿双语转译与摘要，100% 通过 SPEC 契约校验）
- [ ] **M3（当前活跃）**：基于真实 `docs/data/episodes.json` 构建自适应前端阅读器 `docs/index.html`
- [ ] **M4**：配置 `.github/workflows/update.yml` 定时巡检与自动 Commit 闭环

---

## 当前任务：M3 — 自适应静态单页阅读器（`docs/index.html`）

### 交付范围（严格禁越界）

- 仅修改：`docs/index.html`、`ITERATION.md`
- 消费数据：`docs/data/episodes.json`
- 禁止修改：`scripts/process.py`
- 禁止创建：`.github/workflows/update.yml`

### M3 核心实现要求

1. **零外部构建依赖**：纯 HTML5/CSS3/Vanilla JS 单页实现（或使用轻量 Tailwind CDN），无需 npm 构建体系。
2. **多端自适应布局**：
   - 桌面端：左侧单集列表 + 右侧双栏/折叠内容区（摘要、Key Takeaways、Highlights、中英双语逐字稿对照）。
   - 移动端：流畅自适应单栏，提供快捷章节跳转与中英文显示切换（中英对照 / 仅看中文 / 仅看英文）。
3. **播放器联动机制**：
   - 内置 HTML5 `<audio>` 音频播放器，加载 `audio_url`。
   - 点击任何 Highlight 或逐字稿时间戳（`start_sec`）时，自动跳转播放并高亮当前对应段落。
4. **快速搜索与过滤**：支持对单集标题、摘要及逐字稿内容的即时关键字过滤。

### M3 验收标准

1. 本地启动静态服务器访问，页面能在 1 秒内无白屏加载并渲染 `docs/data/episodes.json` 最新单集。
2. 时间戳跳转播放功能正常，中英双语视图切换顺畅。
3. 移动端与桌面端视口测试无任何水平溢出或排版破损。
