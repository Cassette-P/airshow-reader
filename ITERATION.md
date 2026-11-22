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
- [x] **M7**：Unslop 前端视觉重构与刊物级双语研读器（2026-11-21 通过，52px 单层顶栏、Issue Archive 选集抽屉、声学微刻度标尺与 15 项 AI Slop 核销）
- [x] **M7.1**：中文高分屏字重优化 + 声波书页 Icon + Tint/标尺修复（2026-11-21 通过，`Noto Sans SC` 可变字重、`docs/favicon.svg` 动态 Favicon、五等分动态刻度）
- [x] **M7.2**：Magazine Paper × OpenRouter Chrome 视觉重塑、全断点溢出清零与 Unslope 专项审计（2026-11-22 通过，完成白天暖米纸 `#FAF8F4` / 夜间暖炭黑 `#151412` 去荧光色盘、`Newsreader` 西衬 + `Noto Sans SC` 中无排印、逐字稿彻底去卡片化、8 组嘉宾印章色板与 `[AIRSHOW][UI]` 日志对齐、`320px` 极限手机屏单行顶栏与 `641px–860px` iPad Mini 平板中间态溢出修复、动态标签溢出渐隐遮罩及 Unslope WCAG AA / 无抖动 / 离散过渡审计）
- [x] **M7.3**：iOS/iPadOS 27 Safari 原生液态玻璃透传、超薄单行播放胶囊与桌面播控体验收尾（2026-11-22 通过，完成脱离 top: 0 状态栏透传、全端跟随音频正圆图标化、>= 1024px 宽屏 900px 胶囊与 >= 960px 标题展宽、全局 Space 播放防冲突快捷键）

---

## 当前活跃里程碑：M7.3（液态玻璃透传 + 超薄胶囊 + 快捷键收尾）

### 范围边界
- 仅修改 `docs/index.html`，严禁改动后端脚本（`scripts/process.py`）、数据文件（`docs/data/**`）、`podcasts.json` 与 `.github/workflows/update.yml`。
- 严禁破坏 v3.0 三级数据契约、两级缓存与防竞态锁、`seekAndPlay`、二分高亮、URL Hash 持久化与 XSS 防御。

### 已验证完成项
- [x] **iPadOS 27 Safari 深浅双模玻璃透传架构**：拆离透明宿主（`.app-header` / `.audio-player-bar`）与内部 `::before` 玻璃伪元素；移除 `html` 的 `overflow-x: clip` 并补齐 `color-scheme`；关闭态抽屉与遮罩改用 `display: none` 彻底消除 Safari 采样污染。
- [x] **清透液态玻璃光学调校**：`--bg-surface-header` 调优至 Light `0.34` / Dark `0.38`，滤镜收敛至 `blur(2.5px)` 配合明度压制（Light `1.08` / Dark `0.74`），补齐镜面高光边与顶底栏半透明 `--chrome-pill-*` 控件。
- [x] **iOS 27 底部胶囊抬离透传 & 单行超薄化**：`.audio-player-bar` 抬离 `bottom: 0` 突破 Safari 贴底纯色兜底阈值；播放器合并为 `~38–44px` 单行全圆角液态玻璃胶囊（`border-radius: 999px`），实机确认底部网址胶囊后方正文正常透传。

### 待落盘与最终验收项（本轮收尾批处理）
- [x] **解锁 iOS 27 顶部状态栏透明透传**：`.app-header` 脱离 `top: 0` 改为 `top: 8px`（`body` 补 `padding-top: 8px`），移动端（`<= 640px`）同步改为 `44px` 悬浮液态玻璃胶囊，实机验证 iPhone 滚动时状态栏出现原生柔焦透传且 `320px` 零溢出。
- [x] **「跟随音频」按钮全端图标化**：移除 `#btn-autoscroll` 文字标签，统一为正圆居中图标按钮（`28×28` / 移动端 `26×26`），激活态保留重音高亮与 `title`/`aria-label`。
- [x] **宽屏底部胶囊标题展宽**：`>= 1024px` 放宽 `.player-container` 至 `900px`，进度条限制 `max-width: 260px`，单集标题与说话人以细竖线分隔并完整可读（悬停展示 `title` 全称）。
- [x] **Space 空格键播放/暂停快捷键**：支持全局 `Space` 切换播放/暂停且阻止误滚屏；在搜索框/输入控件、选集抽屉打开态、播放器隐藏或焦点位于外部按钮/链接时安全放行不冲突。
- [x] **终审回归检查**：`git status -s` 仅改动 `docs/index.html`；在 `320px`（iPhone）、`744px`（iPad Mini）、`1440px`（桌面）及深浅双模式下验证无横向溢出、双播客切换正常、控制台 0 报错。