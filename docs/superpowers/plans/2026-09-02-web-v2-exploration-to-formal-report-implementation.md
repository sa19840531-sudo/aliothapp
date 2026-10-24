# Web V2 探索到正式报告闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一次最小 Web `POST /api/v2/tasks` 请求在同一个 `task_id` 下完成“探索 → 标准页型 → 增量正式质检 → Coverage Gap → Completion Gate → 正式报告”，并在任务中心正确展示。

**Architecture:** Web Runtime 在每个可信 Capture 分类后，通过 Web 专属页型桥接器写入标准页型和 Assessment，再适配现有 Publication、Outbox、Inspection Work Item、Judge、Rule Execution 与报告基础设施。探索终态由 Web 专属 Reconciliation 补发遗漏通知、排空在途工作、冻结不可变 Coverage Snapshot、写入结构化 Gap 结果，最后复用 Completion Gate 和报告绑定；H5/App 的入口与运行分支保持不变。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/Alembic、Redis、Microsoft Agent Framework、pytest、Vue 3、Vite、TypeScript、Vitest。

**Spec:** `docs/superpowers/specs/2026-09-02-web-v2-exploration-to-formal-report-design.md`

## Global Constraints

- 只改变 `target_platform=web` 的 V2 路径；H5、App 的入口、运行时、页型、规则匹配、Completion Gate 和报告语义必须保持不变。
- Web 正式任务始终使用同一个 `task_id`、`agent_run_id` 和 `finalization_epoch`；不得创建第二个质检任务。
- `/api/v2/tasks` 的最小 Web target 只要求 `platform` 与 `url`；服务端锁定 `full_inspection`、`web_inspection_notification_mode=enforced` 和 `desktop_fhd_1920x1080_v1`。
- `/api/v2/exploration-tasks` 始终锁定 `exploration_only`，不得创建 Publication、正式 Rule Execution 或正式报告。
- 页型映射仅允许 `home→home`、`web_content_list→content_list`、`web_content_detail→content_detail`、`web_video_list→video_list`、`web_video_detail→video_detail`，协议版本固定为 `web-page-type-normalization.v1`；未知类型不得猜测。
- `qc_page_capture.page_type` 只保存标准页型；Web 原始类型继续保留在 Web Ledger，并在 Capture metadata 中审计原始类型、标准类型、置信度、分类器和协议版本。
- 单 Capture 只有在 Capture、Artifact、Manifest、recognized Assessment、标准页型、当前 Run/Epoch、开放 Task 和有效 Session 全部满足时，才能幂等创建 Publication 与 Outbox。
- 探索过程中不得产生 Coverage Gap；仅探索终态完成最终补发和排空后，才能冻结 Snapshot 并写 Gap。
- Coverage Gap 写正式 `evidence_insufficient`，`page_capture_id=NULL`，不创建 Work Item、不调用 LLM、不创建人工审核项。
- 数据库只新增 `qc_web_coverage_snapshot` 与 `qc_rule_execution.source_idempotency_key`；现有 Publication、Outbox、Work Item、Rule Execution 和报告表继续复用。
- 不新增任务状态或 Finalization 枚举；覆盖不全使用已有 `completed_with_gaps`，基础设施/首页不可达等基础失败仍为 `failed`。
- 所有注释使用中文 UTF-8；文档放在 `docs`；每个生产行为必须先有可观察的失败测试，再写最小实现。
- 后端测试使用容器内 Python 3.12：`docker exec alioth-api python -m pytest ...`；当前主机 Python 3.11rc2 不作为验证环境。

---

### Task 1: Web 正式任务最小入口与能力门禁

**Files:**
- Modify: `services/api/app/api/v2/schemas.py`
- Modify: `services/api/app/application/v2_task_entries.py`
- Modify: `services/api/app/api/v2/capabilities.py`
- Modify: `services/api/app/domain/task/targets.py`
- Test: `services/api/tests/test_v2_task_entrypoints.py`
- Test: `services/api/tests/test_v2_capabilities_api.py`
- Test: `services/api/tests/test_task_target_contracts.py`

**Interfaces:**
- Consumes: `V2TaskSubmissionRequest`、`Settings.workflow.web_v2_mode`、Worker roles 和生产依赖配置。
- Produces: 最小 Web target 的正式 `TaskSubmissionResult`，以及与真实可运行组合一致的 Web `execution_modes`。

- [ ] **Step 1: 写最小 Web target 与锁定配置失败测试**

  增加只含 `{"platform":"web","url":"https://example.com/"}` 的请求，断言 Schema 自动使用默认 Profile，正式入口写 `execution_mode=full_inspection`、`web_inspection_notification_mode=enforced`、1920×1080 Profile 快照，仍进入 Web 专属队列；仅探索入口固定 `exploration_only` 且通知模式关闭。

- [ ] **Step 2: 写能力门禁失败测试**

  覆盖 `off`、`exploration_only`、缺少 `web_explorer` 和缺少 `judge`；只有 `on + web_explorer + judge` 公布 `full_inspection`。PostgreSQL Repository、Redis Queue、持久证据存储与 Completion Gate 是当前 V2 组合根的固定生产实现，由 Worker 组合测试证明，不得通过 URL 字符串或无关的 Web Evidence Exit Gate 开关猜测。断言 H5/App capability 投影不变。

- [ ] **Step 3: 运行 RED 测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_v2_capabilities_api.py -q -k "web and (minimal or notification or capability)"`

  Expected: FAIL，因为 Web target/领域目标仍要求成人声明，Web 配置仍借用 H5 通知键。

- [ ] **Step 4: 最小实现入口合同**

  将 Web 成人声明改为可选；未提供时领域目标保存 `None`，不伪造用户声明，后续运行时仅在确实检测到年龄门禁时要求有效声明。统一在服务端解析默认 Profile。给 Web task config 写专属强制通知键；为兼容现有共享 Publication 读取器，仅在 Web config 内保留必要的内部兼容投影，不改变 H5 配置函数。

- [ ] **Step 5: 收敛能力发现**

  由 Web mode 与 Worker roles 计算可公开模式，并明确返回未满足原因；`exploration_only` 模式不得公布完整质检。保持旧 `web` 兼容字段与 `platforms` 投影一致；固定生产组合的真实性在 Task 3/6 的组合测试验证。

- [ ] **Step 6: 运行 GREEN 与入口非回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_v2_capabilities_api.py services/api/tests/test_task_target_contracts.py services/api/tests/test_app_exploration_mode_parity.py -q`

  Expected: 全部通过。

- [ ] **Step 7: 提交 Task 1**

  Commit: `[AI编程] feat(web): 锁定V2正式任务入口；无数据库迁移；无需前端菜单路由；生产依赖连通性由部署验收覆盖`

### Task 2: Web 页型桥接与可信 Capture Assessment

**Files:**
- Create: `services/api/app/application/web_page_type_bridge.py`
- Modify: `services/api/app/application/web_page_classification.py`
- Modify: `services/api/app/application/web_runtime_evidence.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Test: `services/api/tests/test_web_page_type_bridge.py`
- Test: `services/api/tests/test_web_page_classification.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_capture_evidence_repository.py`

**Interfaces:**
- Consumes: Web 分类器输出、Capture 身份、置信度、分类器信息和当前 Run/Session。
- Produces: `WebPageTypeBridge.persist_recognized(...)`，原子持久化标准 `qc_page_capture.page_type`、Web 审计 metadata 和可供正式发布读取的 recognized Assessment。

- [ ] **Step 1: 写显式五类映射与未知类型测试**

  断言五个允许值精确映射；大小写和首尾空白先规范化；任何未列入映射的 `web_*` 或普通字符串返回 unrecognized，绝不通过删除前缀猜测。

- [ ] **Step 2: 写持久化门禁失败测试**

  使用仓储假连接/集成 fixture 断言 Task、Run、Session、Capture 任一身份不匹配或 Session 非有效状态时拒绝更新；成功时标准页型、metadata 和 Assessment 共享同一事务并带 `web-page-type-normalization.v1`。

- [ ] **Step 3: 写 Home、菜单和详情统一桥接测试**

  断言 `prepare_home`、菜单访问和详情采集都调用同一个桥接器；Web Ledger 仍保留原始分类值，正式 Capture 只保存标准值。

- [ ] **Step 4: 运行 RED 测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_type_bridge.py services/api/tests/test_web_capture_evidence_repository.py services/api/tests/test_web_runtime_composition.py -q -k "page_type_bridge or canonical or assessment"`

  Expected: FAIL，因为当前分类仅保留在 Web summary/ledger，未形成正式 Capture Assessment。

- [ ] **Step 5: 实现桥接器和仓储事务**

  新模块只承担显式归一化与审计载荷；仓储验证 Web V2 当前身份后，同事务更新 Capture 和 Assessment。复用现有 Assessment 表/合同，不新增第三套评估表。

- [ ] **Step 6: 接入 Web Runtime 三个分类出口**

  在分类成功且 Capture 已提交后桥接；unrecognized 只写审计/账本，不写猜测标准页型或 recognized Assessment。`exploration_only` 也持久化标准页型和 Assessment，但不通知质检。

- [ ] **Step 7: 运行 GREEN 与分类回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_type_bridge.py services/api/tests/test_web_capture_evidence_repository.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_page_classification.py -q`

  Expected: 全部通过。

- [ ] **Step 8: 提交 Task 2**

  Commit: `[AI编程] feat(web): 持久化标准页型与评估桥接；无数据库迁移；无需前端菜单路由；实库事务并发需集成验收`

### Task 3: 单 Capture 可靠通知与探索并行质检

**Files:**
- Create: `services/api/app/application/web_page_inspection_notification.py`
- Modify: `services/api/app/infrastructure/db/web_capture_evidence_repository.py`
- Modify: `services/api/app/application/web_runtime_composition.py`
- Modify: `services/api/app/worker.py`
- Test: `services/api/tests/test_web_page_inspection_notification.py`
- Test: `services/api/tests/test_web_runtime_composition.py`
- Test: `services/api/tests/test_web_workflow_dispatch.py`
- Test: `services/api/tests/test_worker_web_v2_route.py`

**Interfaces:**
- Consumes: 已桥接 Capture、Manifest/Artifact 事实、Web task/run/epoch/session 状态。
- Produces: `WebPageInspectionNotification.notify_ready_capture(...)`，适配现有 Handoff/Live Publisher，在持久事务中幂等建立 Publication + Outbox，再唤醒正式质检消费者。

- [ ] **Step 1: 写完整六项门禁表格测试**

  分别缺失 Capture、Artifact、pass Manifest、recognized Assessment、标准页型、当前 Run/Epoch/open Task/有效 Session，断言不发布且给出稳定原因；全部满足才发布。

- [ ] **Step 2: 写幂等和 Redis 失败补偿测试**

  同一 Capture/输入哈希重复通知只产生一个 Publication/Outbox/Work Item；持久化完成后 Redis 唤醒失败不删除证据或 Publication，后续重投成功。

- [ ] **Step 3: 写并行时序测试**

  断言第一个 Capture 通知返回后 Web Explorer 可继续第二个菜单，Judge 消费不成为页面导航的同步阻塞；`exploration_only` 不调用通知器。

- [ ] **Step 4: 运行 RED 测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_inspection_notification.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_workflow_dispatch.py services/api/tests/test_worker_web_v2_route.py -q -k "notification or publication or incremental"`

  Expected: FAIL，因为现有 Web Capture 没有适配正式发布合同，Worker 的 Web formal 依赖也未完整注册。

- [ ] **Step 5: 实现 Web 专属证据读取与通知适配器**

  使用 Web 实际 Session 身份 `web:{run_id}` / `web-v2-workflow` 读取 Capture；复用既有 Publication、Outbox、Inventory、Manifest finalizer 和 Live Publisher 端口，不修改 H5 reader 的会话语义。

- [ ] **Step 6: 在 Runtime 分类提交后触发通知**

  仅 `full_inspection + enforced` 触发；通知异常保留可信 Capture 并记录可重试诊断，不回滚探索证据。把 Web Live Publisher 注册到 worker 的 composite publisher，消除重复/遮蔽的 Web loop 组合代码。

- [ ] **Step 7: 运行 GREEN 与 H5/App 通知非回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_page_inspection_notification.py services/api/tests/test_web_runtime_composition.py services/api/tests/test_web_workflow_dispatch.py services/api/tests/test_worker_web_v2_route.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_app_exploration_mode_parity.py -q`

  Expected: 全部通过。

- [ ] **Step 8: 提交 Task 3**

  Commit: `[AI编程] feat(web): 增量发布正式质检工作；无数据库迁移；无需前端菜单路由；Redis故障恢复需实库注入验证`

### Task 4: 不可变 Web Coverage Snapshot 与来源幂等键

**Files:**
- Create: `services/api/app/domain/exploration/web_coverage_snapshot.py`
- Create: `services/api/app/infrastructure/db/web_coverage_snapshot_repository.py`
- Create: `services/api/migrations/versions/20260902_0084_web_formal_inspection_closure.py`
- Modify: `services/api/app/application/rule_execution.py`
- Modify: `services/api/app/infrastructure/db/task_repository.py`
- Test: `services/api/tests/test_web_coverage_snapshot.py`
- Test: `services/api/tests/test_web_coverage_snapshot_repository.py`
- Test: `services/api/tests/test_rule_execution.py`
- Test: `services/api/tests/test_task_repository.py`

**Interfaces:**
- Consumes: 终态 required/observed/unrecognized 标准页型集合、task/run/epoch、可选 `source_idempotency_key`。
- Produces: 稳定哈希的 immutable `WebCoverageSnapshot`，以及向后兼容的 Rule Execution 来源幂等写入。

- [ ] **Step 1: 写领域排序、状态与哈希测试**

  不同输入顺序产生相同规范集合和 hash；missing 自动由 required-observed 得出；状态只允许 `complete`、`completed_with_gaps`、`failed`；未知原始页型进入单独集合。

- [ ] **Step 2: 写仓储不可变重放测试**

  同一 `(task_id,run,epoch)` 相同 hash 返回原记录，不同 hash 抛冲突；非探索终态拒绝创建；Snapshot 创建后没有 update 路径。

- [ ] **Step 3: 写 Rule Execution 来源幂等兼容测试**

  相同 `(task,rule,source_idempotency_key)` 只写一次；不同缺失页型键可各写一条且 `page_capture_id=NULL`；未传键的 H5/App 现有 `(task,rule,page_capture)` 行为不变。

- [ ] **Step 4: 运行 RED 测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py services/api/tests/test_rule_execution.py services/api/tests/test_task_repository.py -q -k "coverage_snapshot or source_idempotency"`

  Expected: FAIL，因为专属表、领域对象和来源幂等字段尚不存在。

- [ ] **Step 5: 增加向后兼容迁移**

  新建 `qc_web_coverage_snapshot` 及唯一约束/状态检查；给 `qc_rule_execution` 增加 nullable 字段和 `WHERE source_idempotency_key IS NOT NULL` 的部分唯一索引。`downgrade` 只撤销本迁移对象。

- [ ] **Step 6: 实现领域和仓储**

  Snapshot 仓储在一个事务内锁定当前 Task/Run/Epoch 并校验探索终态；Rule Execution 命令和仓储新增可选键，不改变旧调用者序列化和冲突路径。

- [ ] **Step 7: 运行 GREEN、迁移升级/降级和旧结果回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_coverage_snapshot.py services/api/tests/test_web_coverage_snapshot_repository.py services/api/tests/test_rule_execution.py services/api/tests/test_task_repository.py -q`

  Run: `docker exec alioth-api python -m alembic -c services/api/migrations/alembic.ini upgrade head`

  Expected: 测试和升级通过；在隔离测试数据库验证 downgrade 到前一 revision 后再 upgrade head。

- [ ] **Step 8: 提交 Task 4**

  Commit: `[AI编程] feat(web): 新增不可变覆盖快照与来源幂等；包含数据库迁移；无需前端菜单路由；降级会移除新增快照数据`

### Task 5: 探索终态 Reconciliation、Coverage Gap 与 Completion Gate

**Files:**
- Create: `services/api/app/application/web_inspection_reconciliation.py`
- Modify: `services/api/app/infrastructure/db/web_coverage_snapshot_repository.py`
- Modify: `services/api/app/infrastructure/db/task_finalization_repository.py`
- Modify: `services/api/app/application/web_evidence_bundle.py`
- Modify: `services/api/app/application/web_evidence_exit_gate.py`
- Test: `services/api/tests/test_web_inspection_reconciliation.py`
- Test: `services/api/tests/test_task_completion_gate.py`
- Test: `services/api/tests/test_web_evidence_bundle.py`
- Test: `services/api/tests/test_web_evidence_exit_gate.py`

**Interfaces:**
- Consumes: 探索终态、已持久化 Capture/Publication/Work 事实、Ruleset 中面向 WEB 的候选规则。
- Produces: `WebInspectionReconciliation.reconcile(...)`，按固定顺序完成补发、排空、Snapshot、Gap Rule Execution 和 Gate-ready 事实。

- [ ] **Step 1: 写终态前禁止 Gap 测试**

  `open/running` 或仍有 frontier/待提交 Capture 时不得冻结 Snapshot 或写 Gap；首页不可达、Manifest 无法可靠落库等基础失败返回 failed，不降级成 coverage gap。

- [ ] **Step 2: 写补发与 active-session 排空顺序测试**

  断言先停止新探索并裁决 Manifest，再补发 snapshot Publication；Session 关闭前完成/终结 active-session work；所有 Publication/Work 终态后才冻结 Snapshot。

- [ ] **Step 3: 写 Gap 正式结果测试**

  对每个 missing canonical page type 只匹配 `target_clients=WEB` 且 `applicable_page_types` 精确命中的规则，写 `evidence_insufficient`、NULL capture 和 `web-coverage-gap:{snapshot_id}:{page_type}:{rule_code}`；不建 Work Item、LLM 调用或人工审核项。

- [ ] **Step 4: 写 Completion Gate 候选库存测试**

  Web Gate 同时统计有 Capture 的正式规则候选和 Gap 结果；Snapshot 缺失、Epoch 不匹配、Publication/Work 未终态或候选数不一致时阻断；`completed_with_gaps` 可在所有终态事实满足后通过。H5 CTE/计算保持原样。

- [ ] **Step 5: 运行 RED 测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_inspection_reconciliation.py services/api/tests/test_task_completion_gate.py services/api/tests/test_web_evidence_bundle.py services/api/tests/test_web_evidence_exit_gate.py -q -k "web or coverage_gap"`

  Expected: FAIL，因为当前 Web 使用 H5 coverage 投影，没有终态 Reconciliation 和 Gap 候选库存。

- [ ] **Step 6: 实现 Reconciliation 和 Gap writer**

  编排固定顺序，所有步骤可按 task/run/epoch 和输入 hash 重放；Gap writer 通过正式 RuleExecutionService 落库，并显式禁止 review item。Evidence bundle 增加标准页型/Snapshot/Gap 摘要；exit gate 区分基础失败和允许报告的 coverage gap。

- [ ] **Step 7: 扩展 Gate 的 Web 分支**

  为 Web 读取专属不可变 Snapshot 和 Gap inventory；不要改写 H5 shared coverage 规则。把 snapshot id/hash/epoch 纳入 gate facts 和阻断原因。

- [ ] **Step 8: 运行 GREEN 与 H5/App Gate 非回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_inspection_reconciliation.py services/api/tests/test_task_completion_gate.py services/api/tests/test_web_evidence_bundle.py services/api/tests/test_web_evidence_exit_gate.py -q`

  Expected: 全部通过；若测试文件名与仓库现状不同，使用对应现有 Gate 测试文件且在报告中记录实际命令。

- [ ] **Step 9: 提交 Task 5**

  Commit: `[AI编程] feat(web): 完成覆盖缺口收口与Gate库存；使用既有数据库迁移；无需前端菜单路由；高并发终态需故障注入复验`

### Task 6: Worker 正式闭环与报告读取模型

**Files:**
- Modify: `services/api/app/worker.py`
- Modify: `services/api/app/application/report_generation.py`
- Modify: `services/api/app/application/report_insight.py`
- Modify: `services/api/app/infrastructure/db/task_insight_repository.py`
- Test: `services/api/tests/test_worker_web_v2_route.py`
- Test: `services/api/tests/test_web_workflow_dispatch.py`
- Test: `services/api/tests/test_report_generation_tool.py`
- Test: `services/api/tests/test_report_insight.py`
- Test: `services/api/tests/test_worker_h5_v2_route.py`

**Interfaces:**
- Consumes: Web Runtime result、Web-specific handoff/live publisher/reconciliation、通过的 Gate Snapshot。
- Produces: 单 Run 的 Web finalization、绑定 Gate Snapshot 的正式报告和 Web coverage 报告摘要。

- [ ] **Step 1: 写 full_inspection Runtime/Worker 路由失败测试**

  断言 Web Runtime 接受锁定的 `full_inspection`；Worker 给 Web 注入 Web reader、handoff、live drain、reconciliation、finalization 和 report 依赖，不使用 H5 session identity；Web-only worker 缺少 judge 时仍只受理 exploration。

- [ ] **Step 2: 写中断恢复时序测试**

  覆盖 Publication 已写但未唤醒、Snapshot 已写但 Gap 未齐、规则已写但 Gate 未过、报告首次失败四个恢复点；重复消息最终只得到一组 Rule Execution 和一份有效报告。

- [ ] **Step 3: 写正式报告摘要测试**

  报告读取专属 Web Snapshot，展示 observed/missing/unrecognized/normalization、Gap 规则与各状态计数；全为 Gap 时整体为 `unknown`，真实 fail 优先于 Gap；未绑定通过 Gate 时报告不可见。

- [ ] **Step 4: 运行 RED 测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_worker_web_v2_route.py services/api/tests/test_web_workflow_dispatch.py services/api/tests/test_report_generation_tool.py services/api/tests/test_report_insight.py -q -k "web and (full_inspection or recovery or report)"`

  Expected: FAIL，因为 Runtime 拒绝 full、Web loop 的正式依赖被重复组合代码遮蔽，报告仍读取内存 summary/共享 H5 coverage。

- [ ] **Step 5: 接通 Worker 收口**

  保留现有队列隔离；Web explorer 完成后执行最终 handoff/reconciliation，等待 Publication、active-session/snapshot Work 和 Rule Execution 终态，再运行 Completion Gate、报告生成、绑定和 sealed。删除重复定义造成的依赖丢失，不改变 H5/App loop 条件。

- [ ] **Step 6: 接通报告读取模型**

  Repository 增加 `fetch_web_coverage_snapshot`；仅 `target_platform=web` 使用。正式报告从数据库快照和正式规则结果构建，不以进程内 exploration summary 作为覆盖权威。

- [ ] **Step 7: 运行 GREEN、故障恢复与 H5 非回归**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_worker_web_v2_route.py services/api/tests/test_web_workflow_dispatch.py services/api/tests/test_report_generation_tool.py services/api/tests/test_report_insight.py services/api/tests/test_worker_h5_v2_route.py -q`

  Expected: 全部通过。

- [ ] **Step 8: 提交 Task 6**

  Commit: `[AI编程] feat(web): 接通Worker收口与正式报告；使用既有数据库迁移；无需前端菜单路由；外部LLM超时需端到端复验`

### Task 7: 任务创建页与任务中心闭环展示

**Files:**
- Modify: `app/workstation/src/views/TaskCreateView.vue`
- Modify: `app/workstation/src/views/TaskCreateView.test.ts`
- Modify: `app/workstation/src/views/TaskDetailView.vue`
- Modify: `app/workstation/src/views/TaskDetailView.test.ts`
- Modify: `app/workstation/src/api/tasks.ts`
- Modify: `app/workstation/src/types/task.ts`
- Modify: `services/api/app/application/workstation_tasks.py`
- Modify: `services/api/app/infrastructure/db/workstation_query_repository.py`
- Test: `services/api/tests/test_workstation_tasks_api.py`

**Interfaces:**
- Consumes: capabilities、Web task execution/finalization/report 状态。
- Produces: 正式/仅探索两个明确入口，以及任务中心的探索、规则质检、报告三阶段和报告门禁。

- [ ] **Step 1: 固化当前前端编译失败并写创建行为测试**

  先记录当前 `npm.cmd run build` 的重复属性/重复导入/未定义变量错误；增加能力含 full 时 Web 默认提交 `/api/v2/tasks`，只含 exploration 时提交 `/api/v2/exploration-tasks` 的测试。正式请求 target 不发送 Profile 或成人声明字段。

- [ ] **Step 2: 写任务中心状态和报告门禁测试**

  `completed_with_gaps` 显示“已完成，有证据缺口”；Web 详情显示探索、规则质检、报告生成三阶段；Gate 未通过或报告未绑定时不显示报告入口，绑定后显示。

- [ ] **Step 3: 运行 RED 测试**

  Run: `npm.cmd run test -- TaskCreateView.test.ts TaskDetailView.test.ts`

  Run: `npm.cmd run build`

  Expected: 测试/构建失败，因为 Web 被强制设为 exploration、创建页有重复定义，详情没有 Web 正式阶段和 gap 文案。

- [ ] **Step 4: 修复创建页并对齐最小请求**

  去掉重复 import/option/type/无效变量；根据 capability 选择 endpoint 和模式。保持 H5/App 表单、请求结构、菜单和路由不变。

- [ ] **Step 5: 扩展后端任务中心投影与详情页**

  只增加 Web 闭环所需 execution/finalization/coverage/report 字段，保持旧字段兼容；前端据此渲染阶段与动作，不用普通 task status 猜测报告是否可用。

- [ ] **Step 6: 运行 GREEN 与前端全量验证**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_workstation_tasks_api.py -q`

  Run: `npm.cmd run test`

  Run: `npm.cmd run build`

  Expected: 全部通过且 `vue-tsc` 无错误。

- [ ] **Step 7: 提交 Task 7**

  Commit: `[AI编程] feat(web): 展示正式质检与证据缺口；无数据库迁移；已同步前端任务页无需新增菜单路由；浏览器兼容性需人工验收`

### Task 8: PostgreSQL 闭环验收、专项规则与全量非回归

**Files:**
- Create: `services/api/tests/test_web_v2_formal_report_e2e.py`
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/plans/2026-09-02-web-v2-exploration-to-formal-report-implementation.md`

**Interfaces:**
- Consumes: Tasks 1-7 的 API、数据库、Worker 和前端实现。
- Produces: 一次单 task_id 的正式闭环证据、故障恢复证据、H5/App 非回归证据和可复用专项规则。

- [x] **Step 1: 写 PostgreSQL 端到端测试**

  最小 Web 请求创建一个 task/run，模拟 home + list + detail Capture 后断言标准页型、Publication/Outbox/Work、正式 Rule Execution、不可变 Snapshot、Gap、Gate、单份绑定报告和任务中心投影串在同一 task/run/epoch。另测仅探索入口没有任何正式副作用。

- [x] **Step 2: 写关键故障注入测试**

  覆盖 Redis 唤醒丢失、增量通知失败由最终交接补发、Worker 在 Publication/Snapshot/Rule/Gate/Report 边界退出再恢复，断言无重复结果和报告。

- [x] **Step 3: 运行 Web 专项与 PostgreSQL 集成测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_web_v2_formal_report_e2e.py services/api/tests/test_v2_task_entrypoints.py services/api/tests/test_web_page_type_bridge.py services/api/tests/test_web_page_inspection_notification.py services/api/tests/test_web_inspection_reconciliation.py services/api/tests/test_worker_web_v2_route.py -q`

  Expected: 全部通过。

- [x] **Step 4: 运行 H5/App 非回归测试**

  Run: `docker exec alioth-api python -m pytest services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_app_exploration_mode_parity.py services/api/tests/test_task_completion_gate.py -q -k "h5 or app"`

  Expected: 全部通过；若仓库测试文件名不同，使用对应现有文件并在验证记录中列明。

- [ ] **Step 5: 运行后端全量、静态检查和前端全量**

  Run: `docker exec alioth-api python -m pytest services/api/tests -q`

  Run: `docker exec alioth-api python -m ruff check services/api/app services/api/tests`

  Run: `npm.cmd run test`（目录 `app/workstation`）

  Run: `npm.cmd run build`（目录 `app/workstation`）

  Expected: 0 failed、0 lint errors、前端测试与生产构建通过。

- [x] **Step 6: 更新专项规则文档**

  固化 Web 原始/标准页型边界、逐 Capture 可靠通知、探索终态后才生成 Gap、不可变 Snapshot、Gap 不建人工审核、Web/H5 Session 身份隔离、Gate/报告绑定和故障恢复回归规则。

- [ ] **Step 7: 请求整分支代码审查并修复**

  使用 `superpowers:requesting-code-review` 对本设计、本计划和基线 commit `d448be57` 到 HEAD 做完整审查。Critical/Important 必须修复并重跑覆盖测试；Minor 记录取舍。

- [ ] **Step 8: 提交 Task 8**

  Commit: `[AI编程] test(web): 固化探索到正式报告闭环；包含数据库迁移；已同步前端任务页无需新增菜单路由；真实外站与LLM链路仍需灰度验收`

#### Task 8 实际验证记录（2026-09-03）

- 真实 PostgreSQL 用例硬锁隔离库 `alioth_web_v2_migration_test_20260902`，最终为
  `3 passed, 2 warnings in 4.59s`。最小入口与仅探索用例以外层事务回滚；正式恢复
  用例使用真实 `asyncpg` pool（至少两个连接）、阶段提交、新服务实例重放，并在
  `finally` 按随机 tenant/task/ruleset UUID 精确清理。
- Fix round Web/架构/通用统一组为 `313 passed, 3 warnings`，Common Capture SQL 合同补跑
  `6 passed`；H5/App 非回归为 `43 passed, 23 deselected`，未修改其队列、Session 或 epoch 合同。
- 后端全量已在 fix round 实际运行至结束：`9 failed, 7662 passed, 66 skipped, 29 warnings`。
  失败恰为已知基线：`capture_coordinator` 1 项、`page_capture_tool` 7 项、
  `page_type_catalog_single_source` 1 项；无本分支新增失败。因 Expected 要求 0 failed，
  Step 5 继续保持未勾，未把基线失败虚报为通过。
- 全量 Ruff 为 5 项已知基线错误：`capture_coordinator` 2 项、`task_insight` 3 项；本分支
  新增错误为 0。Fix round changed-file Ruff 与 `compileall` 全绿。
- 工作台全量 Vitest 为 `14 files / 72 tests passed`；`vue-tsc -b && vite build`
  成功。`git diff --check` 无空白错误，仅有 Git 的 LF/CRLF 提示。
- 根据任务“不得派生子代理”约束，本步先完成整分支差异自审与失败归属审计；Step 5/7/8
  保持未勾选，等待控制器决定广域失败修复、全量转绿与最终提交。
- Fix round 1 已将双 Alembic head 以无 DDL merge revision `20260903_0086` 收口；隔离库
  `alembic heads/current` 均为唯一 `20260903_0086 (head) (mergepoint)`。同时补齐 Web
  反向依赖、Completion Gate SQL 所有权和 H5/Web Worker 静态分支合同；首批目标回归
  `3 passed, 5 deselected` 与 `63 passed`，最终统一目标组为 `313 passed, 3 warnings`。

## Plan Self-Review

- Spec coverage: 最小 API、Web 专属队列、五类页型桥接、逐 Capture 可靠通知、最终交接、不可变 Coverage、Gap 正式结果、Completion Gate、报告绑定、任务中心、恢复和 H5/App 非回归均有对应任务。
- Placeholder scan: 计划不含 TBD、TODO 或未定义的后续处理；所有既有测试文件名均已按仓库现状核对，新测试文件在对应 Task 明确创建。
- Type consistency: 全链路权威身份统一为 `task_id + agent_run_id + finalization_epoch`；页型协议统一为 `web-page-type-normalization.v1`；Gap 幂等键统一为 `web-coverage-gap:{coverage_snapshot_id}:{missing_page_type}:{rule_code}`。
- Boundary consistency: Web 使用实际 Session 身份 `web:{run_id}` / `web-v2-workflow`；H5 reader、H5 notification mode 和 App/H5 Worker 分支不被重命名或重构。
- Baseline: 分支创建后后端目标基线 143 passed；前端构建基线因 `TaskCreateView.vue` / `.test.ts` 的重复定义与未定义变量失败，已纳入 Task 7 的 RED 证据。
