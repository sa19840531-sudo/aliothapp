# H5 V2 Explorer Workflow 实施计划

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Every behavior change follows superpowers:test-driven-development. Before completion use superpowers:verification-before-completion and superpowers:requesting-code-review.

**Goal:** 将锁定为 `workflow_version=v2` 的 H5 任务通过 `off / shadow / on` 灰度模式迁移到独立 V2 Explorer 链路，并保证首次与每次动作后的截图、DOM、URL、可见文本、动作证据、页面识别和动态 Frontier 可追溯。

**Architecture:** 保留通用 `ExplorationWork`、`MafExplorerAgent`、`CaptureCoordinator`、Web Interaction Runtime、PostgreSQL Explorer Work、Inspection Work Item 和 Completion Gate；新增 H5 专属 Bootstrap、Capture/Detection/Frontier 适配、运行时组合根和 Worker Handler。`off` 继续执行 V1，`shadow` 仍由 V1 执行动作并只记录 V2 决策差异，`on` 只执行 V2。

**Tech Stack:** Python 3.12、FastAPI、Pydantic、Microsoft Agent Framework、Playwright、PostgreSQL、pytest。

---

## 全局约束

- 不修改 App V2 行为，不启用桌面 Web 路由，不实现账号密码、验证码、短信或 MFA 自动登录。
- 不新增数据库迁移：复用 `qc_surface_session`、Explorer Work、Action Execution、Page Capture、Artifact、Page Detection、Inspection Work Item、Pipeline Shadow 和 Completion Gate 现有事实表。
- URL 只进入 Observation 元数据；Capture ID、state version、candidate ID 和 work ID 使用稳定协议身份。
- H5 Explorer 不接收 Playwright、Driver、数据库或规则写回 Tool。
- 每个状态变更动作均经 `CaptureCoordinator` 完成 before/action/after；页面判定只消费持久化 after Capture。
- 所有新增注释和文档使用 UTF-8 中文；不改动与本任务无关的未跟踪文件。

### Task 1: H5 V2 灰度路由与配置

**Files:**

- Modify: `services/api/app/core/config.py`
- Modify: `services/api/app/application/app_workflow_router.py`
- Modify: `services/api/app/application/app_workflow_dispatch.py`
- Test: `services/api/tests/test_app_workflow_router.py`
- Test: `services/api/tests/test_app_workflow_dispatch.py`
- Test: `services/api/tests/test_config.py`

**Step 1: 写失败测试**

- 断言 `WorkflowSettings.h5_v2_mode` 默认为 `off`，环境变量 `ALIOTH_H5_V2_MODE` 只接受 `off/shadow/on`，未知值回退 `off`。
- 断言 H5 `workflow_version=v1` 在任意模式始终返回 `WorkflowRoute.H5`。
- 断言 H5 `workflow_version=v2` 分别返回 `H5`、`H5_SHADOW`、`H5_V2`。
- 断言 H5 未知锁定版本 fail closed。

**Step 2: 运行定向测试并确认失败**

Run: `python -m pytest services/api/tests/test_app_workflow_router.py services/api/tests/test_app_workflow_dispatch.py services/api/tests/test_config.py -q`

Expected: 新枚举、配置字段和路由断言失败。

**Step 3: 最小实现**

- 为 `WorkflowSettings` 增加 `h5_v2_mode: Literal["off", "shadow", "on"] = "off"`。
- `_build_workflow_settings()` 读取并安全归一化 `ALIOTH_H5_V2_MODE`。
- `WorkflowRoute` 增加 `H5_SHADOW="h5_shadow"`、`H5_V2="h5_v2"`。
- `AppWorkflowRouter.__init__` 显式接收 `h5_v2_mode`；`route()` 先尊重任务锁定版本，再应用部署模式。
- `AppWorkflowDispatcher.route()` 从 `settings.workflow.h5_v2_mode` 构造或调用已配置 Router，不从 URL 推断模式。

**Step 4: 运行测试并提交**

Run: `python -m pytest services/api/tests/test_app_workflow_router.py services/api/tests/test_app_workflow_dispatch.py services/api/tests/test_config.py -q`

Commit: `[AI编程] feat: 增加H5 V2灰度路由；模块：H5 Workflow；数据库迁移：否；前端菜单路由：否；测试风险：尚未接入V2运行时`

### Task 2: H5 Explorer 独立 Skill 与生产装配

**Files:**

- Add: `services/api/app/agents/skills/h5-explorer-workflow/SKILL.md`
- Modify: `services/api/app/agents/skills/loader.py`
- Modify: `services/api/app/agents/explorer/bootstrap.py`
- Add: `services/api/tests/test_h5_explorer_bootstrap.py`
- Modify: `services/api/tests/test_app_architecture_boundaries.py`

**Step 1: 写失败测试**

- Spy `MafExplorerAgent`，断言 H5 构建入口只要求 `h5-explorer-workflow`、`page-exploration`、`navigation-boundary-skill`，不含 `app-workflow`、`app-script-rhythm`。
- 断言 Provider 可完整加载 H5 Skill，且正文声明 Explorer 只输出 `ActionIntent/ExplorationTerminal`、不执行页面和规则判定。
- 断言 Explorer 包不导入 Playwright、数据库仓储或规则 Judge。

**Step 2: 运行失败测试**

Run: `python -m pytest services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_app_architecture_boundaries.py -q`

**Step 3: 最小实现**

- 新增 `build_h5_explorer_context_provider()`，只扫描 `h5-explorer-workflow`、`phase/page-exploration` 和 `navigation-boundary-skill` 所在父目录。
- 新增 `build_default_h5_explorer_agent()`，复用 `MafExplorerAgent` 和统一 LLM Client，不注册任何业务 Tool。
- H5 Skill 固化动态 Frontier、深度/广度选择、认证边界、before/after Capture、页面判定回环和局部终态纪律。

**Step 4: 运行测试并提交**

Run: `python -m pytest services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_app_workflow_skill.py services/api/tests/test_maf_explorer_agent.py services/api/tests/test_app_architecture_boundaries.py -q`

Commit: `[AI编程] feat: 隔离H5 V2 Explorer装配；模块：Explorer Agent；数据库迁移：否；前端菜单路由：否；测试风险：尚未执行浏览器集成`

### Task 3: H5 Capture/Detection 到 Observation/Frontier 的纯适配

**Files:**

- Add: `services/api/app/application/h5_exploration_observation.py`
- Add: `services/api/tests/test_h5_exploration_observation.py`

**Step 1: 写失败测试**

- 输入 `page-detection.v2` 结果，断言输出 `SurfaceObservation(surface_kind=WEB)` 与 `FrontierSnapshot`。
- 断言 `location` 保留 URL 但 `state_version` 不依赖 Capture ID；相同页面结构的两次 Capture 得到相同状态版本。
- 断言候选 ID 基于动作/语义目标/预期页面稳定去重；未知动作、非法风险和高风险动作按协议过滤或保留为受边界约束候选。
- 断言 evidence artifact IDs、置信度、检测器版本、风险标记写入 Observation metadata，不写入原始 DOM/截图/Base64。

**Step 2: 运行失败测试**

Run: `python -m pytest services/api/tests/test_h5_exploration_observation.py -q`

**Step 3: 最小实现**

- 实现 `build_h5_frontier(detection_result, observed_at, navigation_depth, visited_state_ids, lineage_metadata)`。
- 映射 H5 detection 候选到通用 `CandidateAction`；只允许通用 `InteractionOperation`，风险映射到 `InteractionRiskLevel`。
- 使用 `stable_protocol_hash` 生成 state version、candidate ID 和 frontier snapshot ID。

**Step 4: 运行测试并提交**

Run: `python -m pytest services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_page_type_assessment_adapter.py -q`

Commit: `[AI编程] feat: 增加H5检测到动态Frontier适配；模块：H5探索领域；数据库迁移：否；前端菜单路由：否；测试风险：候选定位依赖现有UI Snapshot`

### Task 4: H5 V2 Bootstrap 与后继探索工作流

**Files:**

- Add: `services/api/app/application/h5_exploration_workflow.py`
- Add: `services/api/tests/test_h5_exploration_workflow.py`

**Step 1: 写失败测试**

- 首轮创建 Web Surface Session，经 `CaptureCoordinator` 执行受信任 Navigate，并以 after Capture 调用页面判定。
- 断言首轮发布 `ExplorationWork`，Surface 为 WEB，Frontier source capture 等于 Navigate after capture。
- 动作轮必须以 `coordination.after_capture` 重新判定并准备后继 Work；remaining actions 减一，duration 有界减少，visited state 追加父状态。
- `recognized/requires_auth/insufficient/error` 均形成显式 Observation/终态语义，不绕过持久化。

**Step 2: 运行失败测试**

Run: `python -m pytest services/api/tests/test_h5_exploration_workflow.py -q`

**Step 3: 最小实现**

- 定义 `RunH5WorkflowCommand`、`ContinueH5ExplorationCommand`、`H5ExplorationResult`。
- 构造固定只读 Capture Profile：before/after 均要求 screenshot、dom、visible_text、ui_snapshot，after 额外要求 action_trace。
- 首轮：注册 Session → Navigate → after Capture → page detection 持久化 → H5 assessment 适配 → `QcOrchestrator.start_exploration()`。
- 后继：after Capture → page detection → assessment → `QcOrchestrator.prepare_exploration()`。

**Step 4: 运行测试并提交**

Run: `python -m pytest services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_explorer_turn.py -q`

Commit: `[AI编程] feat: 接通H5首采判定与后继工作；模块：H5 V2 Runtime；数据库迁移：否；前端菜单路由：否；测试风险：真实浏览器依赖另行集成回归`

### Task 5: H5 Explorer 租约循环与生产组合根

**Files:**

- Add: `services/api/app/application/h5_exploration_turn.py`
- Add: `services/api/app/application/h5_runtime_composition.py`
- Add: `services/api/tests/test_h5_exploration_turn.py`
- Add: `services/api/tests/test_h5_runtime_composition.py`

**Step 1: 写失败测试**

- 断言 H5 Runner 只领取 WEB Exploration/Probe Work，拒绝 App Work。
- 断言 Explorer 动作只经 `ExplorerTurnExecutor` 和 Web `CaptureCoordinator` 执行。
- 断言成功动作与后继 Work 在父租约下原子提交；终态只关闭当前 Work，不宣告任务完成。
- 断言执行器按预算循环，处理 retry_wait/Probe，并返回统一 `RunResult`。

**Step 2: 运行失败测试**

Run: `python -m pytest services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py -q`

**Step 3: 最小实现**

- H5 Runner 复用现有 Explorer Work Queue、租约守护、状态机和 `ExplorerTurnExecutor`，仅增加 WEB 类型校验与 H5 后继适配。
- 生产 Provider 组合现有 Web Capture Runtime、PostgreSQL Surface Session、Session Gate、Action Execution、Evidence Exit Gate、Explorer Work Repository、`QcOrchestrator` 和 H5 Explorer。
- 执行器在 `bind_agent_run_context` 内运行首轮、动作轮、Probe drain 和 Session 有序关闭。

**Step 4: 运行测试并提交**

Run: `python -m pytest services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py -q`

Commit: `[AI编程] feat: 组装H5 V2 Explorer生产循环；模块：H5 V2 Runtime；数据库迁移：否；前端菜单路由：否；测试风险：PostgreSQL/Playwright端到端待环境验证`

### Task 6: Worker 正式切流与只读 Shadow

**Files:**

- Modify: `services/api/app/application/app_workflow_dispatch.py`
- Add: `services/api/app/application/h5_workflow_shadow.py`
- Modify: `services/api/app/worker.py`
- Modify: `services/api/tests/test_app_workflow_dispatch.py`
- Modify: `services/api/tests/test_worker_app_route.py`
- Add: `services/api/tests/test_h5_workflow_shadow.py`
- Add: `services/api/tests/test_worker_h5_v2_route.py`

**Step 1: 写失败测试**

- `H5_V2` 必须在构建 V1 Main Agent 前分流到显式 H5 Handler。
- Handler 缺失时 fail closed，不回退 V1。
- `H5_SHADOW` 仍只执行一次 V1 页面动作；完成后从 WorkingMemory 不可变审计快照构造 V2 只读决策输入并写 Pipeline Shadow，不发布 Work、不调用 Coordinator。
- `off` 和锁定 V1 的行为保持不变。

**Step 2: 运行失败测试**

Run: `python -m pytest services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_h5_workflow_shadow.py services/api/tests/test_worker_app_route.py -q`

**Step 3: 最小实现**

- Dispatcher 增加 `H5WorkflowHandler`、`RuntimeBackedH5WorkflowHandler` 和 `dispatch_h5_v2()`。
- Worker 生命周期构建并向所有消费循环注入同一个 H5 Handler。
- `_process_one()` 对 `H5_V2` 调用专用执行函数；对 `H5_SHADOW` 走 V1，V1 完成后调用只读 Shadow Recorder。
- Shadow 使用 `PipelineShadowService` 保存页面集合、候选选择、阻断原因、终态、耗时和成本摘要；严格清理 DOM/截图/Base64。

**Step 4: 运行测试并提交**

Run: `python -m pytest services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_h5_workflow_shadow.py services/api/tests/test_worker_app_route.py services/api/tests/test_worker.py -q`

Commit: `[AI编程] feat: 切换H5 V2 Worker并增加只读Shadow；模块：Worker/H5 Workflow；数据库迁移：否；前端菜单路由：否；测试风险：灰度样本量门禁依赖生产数据`

### Task 7: 文档、专项规则与纯文本 Workflow

**Files:**

- Modify: `docs/专项能力与高频回归规则.md`
- Add: `docs/workflows/h5-v2-explorer-workflow.txt`
- Add: `docs/reviews/2026-08-11-h5-v2-explorer-workflow-audit.md`

**Step 1: 写文档检查测试**

- 使用 `rg` 断言文本 Workflow 包含 Router、Orchestrator、Explorer、before/action/after、Detection、Frontier、Judge、Completion Gate、off/shadow/on 和 requires_auth。
- 断言文本明确“不自动输入账号密码、不处理验证码/MFA”。

**Step 2: 更新文档**

- 专项规则增加 H5 V2 路由、Explorer 隔离、双采、页面重判、Shadow 单动作源和认证边界回归项。
- 纯文本文件输出最终 Workflow，不使用 Mermaid/HTML。
- 审核报告逐条对照批准设计、代码、测试和残余风险，给出高/中/低优先级结论。

**Step 3: 检查并提交**

Run: `rg -n "H5_V2|Explorer|before Capture|after Capture|Page Detection|Frontier|Rule Judge|Completion Gate|off|shadow|on|requires_auth|验证码|MFA" docs/workflows/h5-v2-explorer-workflow.txt docs/专项能力与高频回归规则.md`

Commit: `[AI编程] docs: 固化H5 V2工作流与回归规则；模块：H5 V2文档；数据库迁移：否；前端菜单路由：否；测试风险：生产Shadow样本仍需持续观察`

### Task 8: 全量审核与验证

**Files:**

- Review: all files changed by Tasks 1-7

**Step 1: 运行格式与静态检查**

Run: `python -m ruff check services/api/app services/api/tests`

Run: `python -m mypy services/api/app`

若仓库未配置对应工具，记录为环境限制，不伪造通过；继续执行项目实际可用检查。

**Step 2: 运行分层测试**

Run: `python -m pytest services/api/tests/test_app_workflow_router.py services/api/tests/test_app_workflow_dispatch.py services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_workflow_shadow.py services/api/tests/test_worker_h5_v2_route.py -q`

Run: `python -m pytest services/api/tests/test_page_capture.py services/api/tests/test_web_capture_runtime.py services/api/tests/test_page_detection_tools.py services/api/tests/test_h5_page_type_assessment_adapter.py services/api/tests/test_explorer_turn.py services/api/tests/test_task_finalization.py -q`

**Step 3: 代码审核**

- 检查 Explorer 是否直接导入 Driver/DB/Judge。
- 检查 `H5_V2` 是否可能回落到 `build_default_main_agent/run_main_agent`。
- 检查每次动作是否产生 before/after Capture 且 after 再判定。
- 检查 Shadow 是否可能执行第二次页面动作或污染正式完成事实。
- 检查 Session 关闭、异常写回、预算、租约和幂等身份。

**Step 4: 最终提交**

Run: `git diff --check`

Run: `git status --short`

Commit: `[AI编程] test: 完成H5 V2 Explorer链路审核；模块：H5 V2全链路；数据库迁移：否；前端菜单路由：否；测试风险：生产环境需继续观测Shadow门禁`

