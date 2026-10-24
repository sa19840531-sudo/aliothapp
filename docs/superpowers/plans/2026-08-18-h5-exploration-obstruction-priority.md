# H5 Exploration Obstruction Priority Implementation Plan

> **For agentic workers:** Execute this plan incrementally with test-first checkpoints and retain runtime evidence for every target URL.

**Goal:** 修复长内容页弹窗目标被 UI 快照截断，以及开屏广告被更低层弹窗错误抢占导致的探索循环。

**Architecture:** 保持现有可信 UI Snapshot 和结构化 DOM 能力签发边界。浏览器侧先采集遮挡层及其关闭控件，再采集导航与普通内容；检测侧以开屏层 `z-index: 3000` 为相对基线，仅允许更高层的结构化弹窗抢占开屏关闭动作。

**Tech Stack:** Python、pytest、Playwright、FastAPI Worker、PostgreSQL/pgvector、Docker Compose。

---

### Task 1: 锁定 UI 快照截断回归

**Files:**
- Modify: `services/api/tests/test_page_capture_tool.py`
- Modify: `services/api/app/infrastructure/browser/page_capture.py`

1. 新增长列表之后出现 Vant 弹窗的 Playwright 测试，要求快照保留遮挡层、Dialog 和关闭控件。
2. 运行单测并确认旧实现失败。
3. 在浏览器快照脚本中将遮挡层关闭控件和遮挡层后代置于普通候选之前，保持总量与序列化上限不变。
4. 运行单测并确认通过。

### Task 2: 锁定开屏相对层级回归

**Files:**
- Modify: `services/api/tests/test_page_detection_tools.py`
- Modify: `services/api/app/agents/tools/page_detection_tools.py`

1. 新增真实层级结构测试：开屏层为 3000、广告 Dialog 为 2003 时必须先关闭开屏；Dialog 为 3003 时继续先关闭 Dialog。
2. 运行单测并确认 2003 场景在旧实现失败。
3. 为高层结构证明增加可配置最小层级，开屏分支使用 3001，其他调用保持原 500 门槛。
4. 运行相关测试并确认通过。

### Task 3: 固化规则并完成真实链路复测

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`

1. 记录长列表快照优先保留遮挡层、开屏按相对层级排序的回归要求。
2. 重启 Worker/API，不执行 build。
3. 自动登录，复查或重跑七个 URL，采集任务、Run、Action、Capture、页面类型与向量检索审计。
4. 汇总每个站点实际探索页面、覆盖率、Worker 效果和不可由代码修复的外部网络阻断。
