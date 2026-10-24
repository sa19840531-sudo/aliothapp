# H5 站点 3/5 失败诊断与重新探索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 查明站点 3 与站点 5 未进入业务页的直接原因，使用原始 URL 创建全新 Alioth 任务重新探索，并输出过程、页型、截图和 HTML 报告。

**Architecture:** 将证据分为目标站 HTTP/资源层、Playwright 浏览器渲染层、Alioth Capture/Detection/Frontier 层和任务完成层。先用只读诊断定位断点，再提交两个正式任务；诊断对照不写入正式覆盖结果。

**Tech Stack:** PowerShell、Docker Compose、Python 3.12/FastAPI 容器、Playwright/Chromium、PostgreSQL、Alioth H5 Explorer。

**Spec:** 用户本轮请求；历史基线 `docs/reports/2026-09-01-h5-five-site-review-rerun-report.html`。

## Global Constraints

- 正式复跑 URL 必须分别保持 `http://d24jdtq5qz8ff4.cloudfront.net` 与 `https://d8zz39epttv3v.cloudfront.net/?/?devTest=123`。
- 使用 `alioth_first_ruleset` v1、`scan_mode=full`、`channel=workbench` 和原产品信息。
- 不修改业务代码，不执行 Docker build，不把预期页型当作实际覆盖。
- 截图与报告写入 `docs/reports`，令牌和密码不得落盘或进入报告。
- 所有新增文本文件使用 UTF-8。

---

### Task 1: 冻结运行基线与旧失败证据

**Files:**
- Read: `docs/reports/2026-09-01-h5-five-site-review-rerun-report.html`
- Read: `docs/reports/assets/2026-09-01-h5-five-site-review-rerun/site-3/`
- Read: `docs/reports/assets/2026-09-01-h5-five-site-review-rerun/site-5/`

**Interfaces:**
- Consumes: 上轮 Task/Run ID、截图、页型与错误摘要。
- Produces: 本轮对比基线和当前 Docker/API 健康事实。

- [x] **Step 1: 记录分支、HEAD、工作树和 Docker 镜像/健康状态**
- [x] **Step 2: 读取上轮站点 3/5 的 Detection、Frontier、Coverage 与终态数据**
- [x] **Step 3: 记录旧截图 checksum、尺寸和可见状态，作为视觉对比基线**

### Task 2: 定位站点 3 的加载断点

**Files:**
- Read: `services/api/app/infrastructure/browser/page_capture.py`
- Read: `services/api/app/agents/tools/page_detection_tools.py`

**Interfaces:**
- Consumes: 站点 3 原始 URL 和上轮空白壳证据。
- Produces: HTTP 入口、脚本/样式资源、浏览器请求失败、console/page error、DOM/截图时间序列。

- [x] **Step 1: 从 API 容器请求入口 HTML 并枚举静态脚本/样式 URL**
- [x] **Step 2: 对每个入口资源记录状态码、Content-Type、大小和失败原因**
- [x] **Step 3: 使用容器内 Playwright 以与 Explorer 相同移动视口加载 0/5/15/30/60 秒**
- [x] **Step 4: 记录导航链、failed request、HTTP 4xx/5xx、console error、page error、#app 子节点数和 nprogress 状态**
- [x] **Step 5: 将当前证据与仓库旧报告中曾成功到达的路由对比，形成单一根因假设**

### Task 3: 定位站点 5 的 URL/启动壳断点

**Files:**
- Read: `services/api/app/application/h5_exploration_observation.py`
- Read: `services/api/app/agents/tools/page_detection_tools.py`

**Interfaces:**
- Consumes: 原始双 `/?/` URL 和 line_loading/Splash 证据。
- Produces: 原始 URL 与最小规范 URL 对照的资源、路由和渲染差异。

- [x] **Step 1: 对原始 URL 记录 HTTP 入口、静态资源和浏览器 0/5/15/30/60 秒时间序列**
- [x] **Step 2: 仅在诊断浏览器中分别测试 `/?devTest=123` 与 `/?/?devTest=123`，一次只改变 URL**
- [x] **Step 3: 比较 history/location、请求参数、#SplashScreenID、#app 子节点和失败资源**
- [x] **Step 4: 判断失败属于 URL 路由、目标后端/资源、浏览器等待策略或其组合**

### Task 4: 创建两个正式 fresh rerun 任务

**Files:**
- Create: `docs/reports/assets/2026-09-01-h5-site-3-5-diagnosis-rerun/`

**Interfaces:**
- Consumes: 原始用户请求参数；诊断不改变正式 URL。
- Produces: 两个新的 Task ID、Run ID 与实时探索轨迹。

- [x] **Step 1: 调用 `/api/v1/auth/login` 获取只在进程内使用的新令牌**
- [x] **Step 2: 向 `/api/v2/tasks` 提交“站3重新探索”和“站5重新探索”**
- [x] **Step 3: 每 20～30 秒读取 workstation、agent-run 和 page-timeline，仅输出状态变化**
- [x] **Step 4: 等待两个任务进入 review/done/failed/canceled 终态**

### Task 5: 归档正式复跑证据

**Files:**
- Create: `docs/reports/assets/2026-09-01-h5-site-3-5-diagnosis-rerun/site-3/`
- Create: `docs/reports/assets/2026-09-01-h5-site-3-5-diagnosis-rerun/site-5/`

**Interfaces:**
- Consumes: 新 Task/Run 的 timeline、rule-results、inspection-work-items、screenshots archive。
- Produces: 可离线复核的去重截图、动作链、页型、Detection 原因、规则与成本统计。

- [x] **Step 1: 下载并解压两个截图 ZIP**
- [x] **Step 2: 提取所有动作、页型 Capture、URL、confidence、reason_code 与 failure_code**
- [x] **Step 3: 提取规则结果、检查闭环、AI 调用/token 和完成门禁**
- [x] **Step 4: 视觉检查每个实际页型的代表截图**

### Task 6: 生成并验证 HTML 报告

**Files:**
- Create: `docs/reports/2026-09-01-h5-site-3-5-failure-diagnosis-rerun-report.html`

**Interfaces:**
- Consumes: 诊断证据、正式复跑事实和截图资产。
- Produces: 用户可直接打开的最终探索报告。

- [x] **Step 1: 写入根因结论、旧/新对比、正式任务参数与终态**
- [x] **Step 2: 写入完整动作序列、页型矩阵、Detection/Frontier/规则统计**
- [x] **Step 3: 嵌入相对路径截图画廊，并标注诊断截图与正式截图的边界**
- [x] **Step 4: 验证 HTML 标签、所有图片引用、UTF-8、敏感信息和 `git diff --check`**
- [x] **Step 5: 重新查询 API/Docker 状态并逐项核对本计划要求**
