# App 探索 Workflow 优化实施计划

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**目标：** 将正式 App 质检任务中的探索子流程调整为“确定性探索 → 最终观察 → Evidence Index 封存 → Surface Session 关闭 → 下游只读质检”，并消除探索阶段发布 Inspection、排空 Probe、补采开关误伤重新观察、动作失败失真和无确定性终止的问题。

**架构：** 保留现有 /api/v1/app-builds、/api/v1/tasks、AppV2Runtime 和 Worker 路由。App Exploration 内部新增第一类重新观察、探索终止评估、Evidence Index 和关闭服务；现有 AppCaptureInspectionPublisher 只由封存证据消费者在设备关闭后调用。PostgreSQL 是探索结果与证据索引事实源，Redis 仍只承担唤醒与短期投影。

**技术栈：** Python 3.12、FastAPI、asyncpg、Alembic、pytest/pytest-asyncio、Vue 3、TypeScript、Vitest。

**设计基线：** docs/superpowers/specs/2026-08-11-app-exploration-workflow-optimization-design.md

**最新代码基线：** origin/dev_1.5@69ab553；本计划编制时本地 HEAD 为 ff4ee48。

## 实施原则

- 每一项生产代码变更前先增加失败测试。
- 每个任务只提交其列出的文件，保留工作区其他未跟踪文件。
- 规则补采继续受 SupplementalEvidencePolicy 全局门禁保护。
- Surface 重新观察不得携带 evidence_request_id、restoration_attempt_id、equivalence_assessment_id 或 supplemental_evidence_source。
- 已封存 Evidence Index 后不允许再调用 SurfaceDriver。
- 失败动作可以附加 After Capture，但执行结果不得变成 succeeded。
- 每个任务完成后运行目标测试；阶段完成后运行聚合回归。

## Task 1：断开探索与 Inspection / Probe 的运行时耦合

**文件：**

- 修改：services/api/app/application/app_screenshot_exploration.py
- 修改：services/api/app/application/app_v2_runtime.py
- 修改：services/api/app/application/app_runtime_composition.py
- 修改：services/api/tests/test_app_screenshot_exploration.py
- 修改：services/api/tests/test_app_runtime_composition.py
- 修改：services/api/tests/test_app_v2_runtime.py
- 修改：services/api/tests/test_app_architecture_boundaries.py

**Step 1：先写失败测试**

增加以下断言：

~~~python
assert "app_inspection_publisher" not in app_screenshot_exploration_source
assert "ExplorerWorkType.PROBE" not in production_executor_source
assert runtime_dependencies 不再要求 inspection_publisher
assert exploration.run(...) 只保存视觉事实并发布 ExplorationWork
~~~

更新现有探索测试，删除“每次截图立即发布 Inspection”的期待，改为确认探索工作流没有 Inspection 端口。

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_app_v2_runtime.py services/api/tests/test_app_architecture_boundaries.py -q
~~~

预期：测试因默认 AppCaptureInspectionPublisher、_publish_inspection 和 Probe drain 仍存在而失败。

**Step 3：最小实现**

- 从 AppScreenshotExplorationWorkflow 删除 AppCaptureInspectionPort、构造参数、字段和两次 _publish_inspection 调用。
- 从 AppV2RuntimeDependencies 和 build_app_v2_runtime 删除 Inspection Publisher 依赖与默认构造。
- 从 ProductionAppV2RuntimeExecutor 删除 App 正常路径 Probe drain。
- 保留 SupplementalEvidencePolicy 对真正 Probe 端口的保护，不删除通用 Probe 领域合同。

**Step 4：验证**

运行 Step 2 命令并确认通过。

**Step 5：提交**

~~~powershell
git add -- services/api/app/application/app_screenshot_exploration.py services/api/app/application/app_v2_runtime.py services/api/app/application/app_runtime_composition.py services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_app_v2_runtime.py services/api/tests/test_app_architecture_boundaries.py
git commit -m "[AI编程] refactor: 解耦App探索与质检补采；模块：App/V2 Workflow；数据库迁移：否；前端菜单路由：否；测试风险：正式质检发布移至封存消费者后需联调"
~~~

## Task 2：恢复 Surface Phase 的有界重新观察决策

**文件：**

- 修改：services/api/app/domain/exploration/startup_ad_countdown_phase.py
- 修改：services/api/app/domain/exploration/popup_chain_phase.py
- 修改：services/api/app/domain/exploration/auth_gate_phase.py
- 修改：services/api/app/domain/exploration/system_dialog_phase.py
- 修改：services/api/app/application/surface_phase_turn_orchestrator.py
- 修改：services/api/app/application/surface_phase_action_handoff.py
- 修改：services/api/tests/test_startup_ad_countdown_phase.py
- 修改：services/api/tests/test_popup_chain_phase.py
- 修改：services/api/tests/test_auth_gate_phase.py
- 修改：services/api/tests/test_system_dialog_phase.py
- 修改：services/api/tests/test_surface_phase_turn_orchestrator.py
- 修改：services/api/tests/test_surface_phase_action_handoff.py

**Step 1：先写失败测试**

覆盖：

- countdown_remaining_ms > 0 且未耗尽次数时返回 TIMED_CAPTURE。
- 倒计时等待时间受剩余倒计时和单次最大等待策略约束。
- 关闭广告后页面仍在过渡时返回 TIMED_CAPTURE。
- Popup/Auth/System Dialog 操作后需要稳定确认时返回 TIMED_CAPTURE。
- 达到各自 reobserve 上限后返回明确 evidence_insufficient 原因。
- SurfacePhaseTurnOrchestrator 不再把所有 TIMED_CAPTURE 改写为 supplemental_evidence_disabled。
- Timed Capture Handoff 使用 screenshot + ui_tree，不再产生 ui_snapshot。

示例：

~~~python
assert decision.next_action.action_type is InteractionOperation.TIMED_CAPTURE
assert decision.next_action.delay_ms == 5_000
assert decision.reason_code == "countdown_reobserve_required"
assert handoff.kind is SurfacePhaseActionHandoffKind.TIMED_CAPTURE
assert handoff.timed_capture.directive.artifact_types == ("screenshot", "ui_tree")
~~~

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_startup_ad_countdown_phase.py services/api/tests/test_popup_chain_phase.py services/api/tests/test_auth_gate_phase.py services/api/tests/test_system_dialog_phase.py services/api/tests/test_surface_phase_turn_orchestrator.py services/api/tests/test_surface_phase_action_handoff.py -q
~~~

**Step 3：最小实现**

- 恢复领域阶段机的 TIMED_CAPTURE 输出，但使用 reobserve 语义原因码。
- 只在次数、总预算或关闭尝试上限耗尽时阻断。
- 删除 SurfacePhaseTurnOrchestrator 的 TIMED_CAPTURE 统一阻断分支。
- SurfacePhaseTimedCaptureRequest 保持无补采血缘字段。
- 将 App Artifact 规范统一为 ui_tree。

**Step 4：验证并提交**

~~~powershell
python -m pytest services/api/tests/test_startup_ad_countdown_phase.py services/api/tests/test_popup_chain_phase.py services/api/tests/test_auth_gate_phase.py services/api/tests/test_system_dialog_phase.py services/api/tests/test_surface_phase_turn_orchestrator.py services/api/tests/test_surface_phase_action_handoff.py -q
git add -- services/api/app/domain/exploration/startup_ad_countdown_phase.py services/api/app/domain/exploration/popup_chain_phase.py services/api/app/domain/exploration/auth_gate_phase.py services/api/app/domain/exploration/system_dialog_phase.py services/api/app/application/surface_phase_turn_orchestrator.py services/api/app/application/surface_phase_action_handoff.py services/api/tests/test_startup_ad_countdown_phase.py services/api/tests/test_popup_chain_phase.py services/api/tests/test_auth_gate_phase.py services/api/tests/test_system_dialog_phase.py services/api/tests/test_surface_phase_turn_orchestrator.py services/api/tests/test_surface_phase_action_handoff.py
git commit -m "[AI编程] fix: 恢复App Surface有界重新观察；模块：App/Surface Phase；数据库迁移：否；前端菜单路由：否；测试风险：真实设备等待时长需联调"
~~~

## Task 3：建立无 Action Execution 的重新观察采集链

**文件：**

- 新增：services/api/app/domain/interaction/timed_observation.py
- 修改：services/api/app/domain/interaction/__init__.py
- 修改：services/api/app/application/explorer_turn.py
- 修改：services/api/app/application/surface_phase_explorer.py
- 修改：services/api/app/application/capture_coordinator.py
- 修改：services/api/app/application/app_interaction_runtime.py
- 修改：services/api/app/application/app_capture_persistence.py
- 修改：services/api/app/application/app_screenshot_exploration.py
- 修改：services/api/app/application/app_exploration_turn.py
- 修改：services/api/app/infrastructure/db/app_capture_repository.py
- 修改：services/api/tests/test_explorer_turn.py
- 修改：services/api/tests/test_surface_phase_explorer.py
- 修改：services/api/tests/test_capture_coordinator.py
- 修改：services/api/tests/test_app_interaction_runtime.py
- 修改：services/api/tests/test_app_capture_persistence.py
- 修改：services/api/tests/test_app_screenshot_exploration.py
- 修改：services/api/tests/test_app_exploration_turn.py

**Step 1：先写失败测试**

测试第一类 TimedObservationIntent：

~~~python
assert turn.decision.kind == "timed_observation"
assert turn.coordination is None
assert turn.observation_capture.role == "observation"
assert turn.observation_capture.action_execution_id is None
assert successor.source_capture_id == turn.observation_capture.capture_id
~~~

数据库适配测试确认：

- qc_capture.capture_role = observation。
- parent_capture_id 指向重新观察来源。
- action_execution_id 为 NULL。
- session_command_sequence 与 finalization_epoch 受 Session Permit 保护。
- 重试使用 phase_run_id + decision_id 生成稳定 capture_id。

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_explorer_turn.py services/api/tests/test_surface_phase_explorer.py services/api/tests/test_capture_coordinator.py services/api/tests/test_app_interaction_runtime.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_app_exploration_turn.py -q
~~~

**Step 3：实现 TimedObservationIntent**

稳定字段：

~~~text
task_id
session_id
phase_run_id
decision_id
observation_id
source_capture_id
surface_kind
delay_ms
directive
deadline
~~~

该合同不得包含任何补采血缘字段。

**Step 4：实现采集与后继**

- SurfacePhaseAwareExplorer 将 Timed Capture Handoff 映射为 TimedObservationIntent。
- ExplorerTurnExecutor 在预算内等待后调用 CaptureCoordinator.capture_observation。
- capture_observation 在 Session Command Gate 内创建 observation Capture。
- AppCaptureWriter 和 AppCapturePersistenceService 增加 observation 分支。
- AppCaptureRepository 保存 observation Capture，不绑定 Action Execution。
- AppScreenshotExplorationWorkflow 理解 observation Capture 并准备后继 Work。
- 后继 lineage 保留 surface_phase_run_id、decision_id 与 semantic_role=surface_reobserve。

**Step 5：验证 SupplementalEvidencePolicy 没有被绕过**

新增测试同时证明：

- TimedObservationIntent 正常执行。
- 带 evidence_request_id 的 Probe 动作仍返回 SUPPLEMENTAL_EVIDENCE_DISABLED。

**Step 6：提交**

~~~powershell
git add -- services/api/app/domain/interaction/timed_observation.py services/api/app/domain/interaction/__init__.py services/api/app/application/explorer_turn.py services/api/app/application/surface_phase_explorer.py services/api/app/application/capture_coordinator.py services/api/app/application/app_interaction_runtime.py services/api/app/application/app_capture_persistence.py services/api/app/application/app_screenshot_exploration.py services/api/app/application/app_exploration_turn.py services/api/app/infrastructure/db/app_capture_repository.py services/api/tests/test_explorer_turn.py services/api/tests/test_surface_phase_explorer.py services/api/tests/test_capture_coordinator.py services/api/tests/test_app_interaction_runtime.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_app_screenshot_exploration.py services/api/tests/test_app_exploration_turn.py
git commit -m "[AI编程] feat: 接通App确定性重新观察链；模块：App/Interaction Runtime；数据库迁移：否；前端菜单路由：否；测试风险：设备端ui_tree降级需真机验证"
~~~

## Task 4：保留动作执行真实性和独立效果状态

**文件：**

- 修改：services/api/app/domain/interaction/enums.py
- 修改：services/api/app/domain/interaction/interaction_result.py
- 修改：services/api/app/domain/interaction/__init__.py
- 修改：services/api/app/application/capture_coordinator.py
- 修改：services/api/app/application/action_executions.py
- 修改：services/api/app/infrastructure/db/action_execution_repository.py
- 新增：services/api/tests/test_interaction_runtime_contracts.py
- 修改：services/api/tests/test_capture_coordinator.py
- 新增：services/api/tests/test_action_execution_repository.py

**Step 1：先写失败测试**

增加 execution_outcome 与 verification_outcome：

~~~text
execution_outcome: succeeded / failed / rejected / timeout
verification_outcome: pending / effect_confirmed / no_effect / effect_unexpected / unverified
~~~

关键断言：

~~~python
assert failed_with_after.status is InteractionResultStatus.FAILED
assert failed_with_after.result_capture_id == after_capture.capture_id
assert failed_with_after.execution_outcome == FAILED
assert failed_with_after.verification_outcome == UNVERIFIED
assert ledger.status is ActionExecutionStatus.FAILED
assert ledger.result_capture_id == after_capture.capture_id
~~~

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_interaction_runtime_contracts.py services/api/tests/test_capture_coordinator.py services/api/tests/test_action_execution_repository.py -q
~~~

**Step 3：最小实现**

- InteractionResult 为旧 v1 载荷推导缺失的新字段，保持反序列化兼容。
- 删除 _maybe_app_soft_continue_pending 和 _try_app_action_soft_continue。
- Driver 返回 FAILED/BLOCKED/TIMED_OUT 时先按真实状态写 Action Execution，再尽力执行 After Capture。
- 新增 attach_capture_preserving_execution 方法。
- ActionExecutionRepository 新增 attach_terminal_evidence，写入 result_capture_id、result_payload、evidence_finished_at，但不改变 FAILED/BLOCKED/TIMED_OUT。
- 成功动作仍由 evidence_pending → completed。
- After Capture 失败时成功动作进入 evidence_failed；原本已失败动作保持失败并记录 verification_outcome=unverified。

**Step 4：验证并提交**

~~~powershell
python -m pytest services/api/tests/test_interaction_runtime_contracts.py services/api/tests/test_capture_coordinator.py services/api/tests/test_action_execution_repository.py services/api/tests/test_app_interaction_runtime.py -q
git add -- services/api/app/domain/interaction/enums.py services/api/app/domain/interaction/interaction_result.py services/api/app/domain/interaction/__init__.py services/api/app/application/capture_coordinator.py services/api/app/application/action_executions.py services/api/app/infrastructure/db/action_execution_repository.py services/api/tests/test_interaction_runtime_contracts.py services/api/tests/test_capture_coordinator.py services/api/tests/test_action_execution_repository.py services/api/tests/test_app_interaction_runtime.py
git commit -m "[AI编程] fix: 保留App动作执行真实结果；模块：App/Action Execution；数据库迁移：否；前端菜单路由：否；测试风险：旧result_payload兼容已覆盖"
~~~

## Task 5：增加候选访问账本、循环检测与确定性终止

**文件：**

- 修改：services/api/app/domain/exploration/snapshots.py
- 新增：services/api/app/domain/exploration/app_terminal.py
- 修改：services/api/app/domain/exploration/__init__.py
- 修改：services/api/app/application/app_screenshot_exploration.py
- 修改：services/api/app/agents/explorer/maf_agent.py
- 修改：services/api/app/application/app_runtime_composition.py
- 新增：services/api/tests/test_exploration_snapshot_contracts.py
- 新增：services/api/tests/test_app_exploration_terminal.py
- 新增：services/api/tests/test_explorer_maf_agent.py
- 修改：services/api/tests/test_app_screenshot_exploration_cycle.py
- 修改：services/api/tests/test_app_runtime_composition.py

**Step 1：先写失败测试**

FrontierSnapshot 追加兼容字段：

~~~text
visited_candidate_ids
visited_action_fingerprints
repeated_state_count
~~~

测试：

- 相同页面状态、动作类型、语义目标形成相同 action fingerprint。
- 已访问 fingerprint 不再进入 eligible candidates。
- 连续相同 state_version 达到阈值后返回 loop_detected。
- 没有可执行候选、已进入稳定页且最低唯一状态覆盖达成时返回 COMPLETED。
- 预算耗尽但最低覆盖不足时返回 evidence_insufficient，不返回普通成功。
- LLM TerminalIntent 只能作为输入，最终 outcome 由 AppExplorationTerminalEvaluator 决定。

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_exploration_snapshot_contracts.py services/api/tests/test_app_exploration_terminal.py services/api/tests/test_explorer_maf_agent.py services/api/tests/test_app_screenshot_exploration_cycle.py services/api/tests/test_app_runtime_composition.py -q
~~~

**Step 3：实现**

- 旧 Frontier v1 载荷缺少字段时按空集合和 0 读取。
- prepare_after_action / prepare_after_observation 追加访问账本。
- Explorer eligibility 过滤已执行 action fingerprint。
- 领域终止评估器输出 exploration_completed、evidence_insufficient 或 technical_failed 分类。
- ProductionAppV2RuntimeExecutor 不再仅依赖 LLM 的 completed；使用领域评估结果收口。

**Step 4：提交**

~~~powershell
git add -- services/api/app/domain/exploration/snapshots.py services/api/app/domain/exploration/app_terminal.py services/api/app/domain/exploration/__init__.py services/api/app/application/app_screenshot_exploration.py services/api/app/agents/explorer/maf_agent.py services/api/app/application/app_runtime_composition.py services/api/tests/test_exploration_snapshot_contracts.py services/api/tests/test_app_exploration_terminal.py services/api/tests/test_explorer_maf_agent.py services/api/tests/test_app_screenshot_exploration_cycle.py services/api/tests/test_app_runtime_composition.py
git commit -m "[AI编程] feat: 增加App探索确定性终止；模块：App/Explorer Domain；数据库迁移：否；前端菜单路由：否；测试风险：默认最低覆盖策略需业务样本持续校准"
~~~

## Task 6：增加 Manifest 完整校验和不可变 Evidence Index

**文件：**

- 新增：services/api/app/domain/evidence/app_index.py
- 修改：services/api/app/domain/evidence/__init__.py
- 新增：services/api/app/application/app_exploration_evidence.py
- 修改：services/api/app/application/evidence_manifests.py
- 修改：services/api/app/application/app_evidence_manifest.py
- 修改：services/api/app/application/app_capture_persistence.py
- 新增：services/api/app/infrastructure/db/app_evidence_index_repository.py
- 修改：services/api/app/infrastructure/db/evidence_manifest_repository.py
- 新增：services/api/migrations/versions/20260811_0046_app_exploration_evidence_index.py
- 新增：services/api/tests/test_app_evidence_index.py
- 新增：services/api/tests/test_app_exploration_evidence.py
- 新增：services/api/tests/test_app_evidence_index_repository.py
- 新增：services/api/tests/test_app_exploration_workflow_migration.py
- 修改：services/api/tests/test_app_capture_persistence.py
- 新增：services/api/tests/test_evidence_manifest_repository.py

**Step 1：先写领域合同测试**

覆盖 AppExplorationResult、AppEvidenceIndex、AppEvidenceIndexEntry、AppEvidenceGap、semantic_role、规范化哈希和 sealed 不可变规则。

**Step 2：先写迁移失败测试**

迁移新增：

~~~text
qc_app_evidence_index
qc_app_evidence_index_entry
~~~

约束：

- 每个 agent_run_id 只能有一个 Index。
- status 只允许 building / sealed。
- sequence_no 在 index 内唯一且为正数。
- Entry 只能引用同任务 Capture 与 Manifest。
- sealed 必须有 content_sha256 和 sealed_at。
- 通过触发器或 Repository 门禁禁止 sealed 后增删改 Entry。

**Step 3：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_app_evidence_index.py services/api/tests/test_app_exploration_evidence.py services/api/tests/test_app_evidence_index_repository.py services/api/tests/test_app_exploration_workflow_migration.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_evidence_manifest_repository.py -q
~~~

**Step 4：实现 Manifest 完整链**

- persist_bootstrap 与 observation Capture 也写 PENDING Manifest。
- EvidenceManifestRepository 增加按 task/session 列出每个 Capture 最新版本的方法。
- AppEvidenceManifestFinalizer 从 Artifact Store 读取对象，核验 size_bytes 与 checksum。
- 校验通过追加 PASS 版本；失败追加 FAIL 版本并形成 EvidenceGap。
- 只有 PASS Manifest 可以生成 Index Entry。

**Step 5：实现 Evidence Index**

- 按 captured_at、capture_id、semantic_role 生成稳定 sequence_no。
- semantic_role 映射 bootstrap、surface_before、surface_after、surface_reobserve、action_before、action_after、final_observation。
- 规范化序列化后计算 SHA-256。
- seal 使用事务、行锁和幂等比较；重复相同请求返回原结果，内容不同则拒绝。

**Step 6：验证并提交**

~~~powershell
python -m pytest services/api/tests/test_app_evidence_index.py services/api/tests/test_app_exploration_evidence.py services/api/tests/test_app_evidence_index_repository.py services/api/tests/test_app_exploration_workflow_migration.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_evidence_manifest_repository.py -q
git add -- services/api/app/domain/evidence/app_index.py services/api/app/domain/evidence/__init__.py services/api/app/application/app_exploration_evidence.py services/api/app/application/evidence_manifests.py services/api/app/application/app_evidence_manifest.py services/api/app/application/app_capture_persistence.py services/api/app/infrastructure/db/app_evidence_index_repository.py services/api/app/infrastructure/db/evidence_manifest_repository.py services/api/migrations/versions/20260811_0046_app_exploration_evidence_index.py services/api/tests/test_app_evidence_index.py services/api/tests/test_app_exploration_evidence.py services/api/tests/test_app_evidence_index_repository.py services/api/tests/test_app_exploration_workflow_migration.py services/api/tests/test_app_capture_persistence.py services/api/tests/test_evidence_manifest_repository.py
git commit -m "[AI编程] feat: 增加App探索Evidence Index；模块：App/Evidence；数据库迁移：是，新增0045索引头与条目表；前端菜单路由：否；测试风险：对象存储真实摘要需集成环境回归"
~~~

## Task 7：接通最终观察、Evidence Seal 与 Session Closure

**文件：**

- 新增：services/api/app/application/app_exploration_closure.py
- 修改：services/api/app/application/app_v2_runtime.py
- 修改：services/api/app/application/app_runtime_composition.py
- 修改：services/api/app/infrastructure/db/surface_session_repository.py
- 新增：services/api/tests/test_app_exploration_closure.py
- 修改：services/api/tests/test_app_v2_runtime.py
- 修改：services/api/tests/test_app_runtime_composition.py
- 修改：services/api/tests/test_surface_session_repository.py

**Step 1：先写失败测试**

验证严格顺序：

~~~text
final observation
→ finalize manifests
→ seal Evidence Index
→ begin_draining
→ close_readiness
→ seal_for_close
→ driver.close_session
→ mark_closed
~~~

另测：

- Evidence Index 封存失败时不得进入 Session 关闭和正式质检。
- Driver close 失败时调用 mark_lost，不撤销 Evidence Index。
- readiness 非零时不调用 Driver close。
- FINAL_OBSERVING 后不创建新 Exploration Work。

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_app_exploration_closure.py services/api/tests/test_app_v2_runtime.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_surface_session_repository.py -q
~~~

**Step 3：实现**

- AppExplorationClosureService 生成 final_observation Capture。
- 根据领域终止判定生成 exploration_completed 或 evidence_insufficient。
- 封存 Evidence Index 后使用现有 SurfaceSessionRepository 关闭 API。
- AppV2Runtime 增加 close_exploration 入口。
- ProductionAppV2RuntimeExecutor 在所有业务终止路径调用关闭入口；技术失败尝试 mark_lost。
- close_readiness 增加 pending Manifest / building Evidence Index 的关闭门禁。

**Step 4：提交**

~~~powershell
git add -- services/api/app/application/app_exploration_closure.py services/api/app/application/app_v2_runtime.py services/api/app/application/app_runtime_composition.py services/api/app/infrastructure/db/surface_session_repository.py services/api/tests/test_app_exploration_closure.py services/api/tests/test_app_v2_runtime.py services/api/tests/test_app_runtime_composition.py services/api/tests/test_surface_session_repository.py
git commit -m "[AI编程] feat: 收口App探索证据与设备会话；模块：App/Workflow Closure；数据库迁移：复用0045；前端菜单路由：否；测试风险：官方Runtime关闭异常需集成验证"
~~~

## Task 8：封存后只读发布正式 Inspection

**文件：**

- 新增：services/api/app/application/app_sealed_inspection_publisher.py
- 修改：services/api/app/worker.py
- 修改：services/api/app/application/app_inspection_publisher.py
- 新增：services/api/tests/test_app_sealed_inspection_publisher.py
- 修改：services/api/tests/test_worker.py
- 修改：services/api/tests/test_worker_app_route.py
- 修改：services/api/tests/test_inspection_wakeup.py

**Step 1：先写失败测试**

验证：

- building Evidence Index 不发布 Inspection。
- sealed + hash 校验通过后，按 Index Entry 顺序发布。
- Publisher 只接收 Index 中的 Capture，不查询或调用 SurfaceDriver。
- evidence_insufficient 根据下游策略只生成可判定证据工作，不触发补采。
- Session 仍为 active/draining 时拒绝正式发布。
- 重复 Worker 收口保持幂等。

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_app_sealed_inspection_publisher.py services/api/tests/test_worker.py services/api/tests/test_worker_app_route.py services/api/tests/test_inspection_wakeup.py -q
~~~

**Step 3：实现**

- AppSealedEvidenceInspectionPublisher 只读取 sealed Index、PASS Manifest 和已保存视觉事实。
- 在 _process_app_one 中，dispatcher 完成后先验证探索结果和 Session 终态，再发布 Inspection。
- 然后复用现有 _drain_snapshot_inspection_work、规则收口与报告流程。
- 任何未封存、哈希不符或 Session 未关闭情况都不进入正式质检。

**Step 4：提交**

~~~powershell
git add -- services/api/app/application/app_sealed_inspection_publisher.py services/api/app/worker.py services/api/app/application/app_inspection_publisher.py services/api/tests/test_app_sealed_inspection_publisher.py services/api/tests/test_worker.py services/api/tests/test_worker_app_route.py services/api/tests/test_inspection_wakeup.py
git commit -m "[AI编程] refactor: 正式质检只读消费封存App证据；模块：App/Worker Inspection；数据库迁移：否；前端菜单路由：否；测试风险：规则覆盖率需端到端样本回归"
~~~

## Task 9：扩展 Task Insight 与 AppStagePanel

**文件：**

- 修改：services/api/app/infrastructure/db/task_insight_repository.py
- 修改：services/api/app/application/task_insight.py
- 修改：services/api/app/api/v1/workstation/task_insight.py
- 修改：services/api/tests/test_task_insight_repository.py
- 修改：services/api/tests/test_task_insight_service.py
- 修改：services/api/tests/test_task_insight_api.py
- 修改：app/workstation/src/types/task-insight.ts
- 修改：app/workstation/src/modules/tasks/components/AppStagePanel.vue
- 新增：app/workstation/src/modules/tasks/components/AppStagePanel.test.ts

**Step 1：先写失败测试**

后端 app_exploration 投影包含：

~~~text
status
surface_phase
current_page_identity
completed_work_count
pending_work_count
action_count
reobservation_count
coverage_summary
terminal_reason_code
outcome
evidence_index_id
evidence_index_hash
evidence_gap_count
surface_session_status
started_at
completed_at
~~~

前端测试确认：

- 不再显示 Probe 卡片。
- 显示 Surface Readiness、Evidence Validation、Evidence Sealing、Session Closure。
- evidence_insufficient 与 technical_failed 使用不同状态。
- close_failed / lost 不隐藏在完成状态中。
- 旧任务 app_exploration=null 时兼容。

**Step 2：运行失败测试**

~~~powershell
python -m pytest services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py -q
npm --prefix app/workstation test -- AppStagePanel.test.ts
~~~

**Step 3：实现并验证**

- fetch_app_stage 聚合 Index、Entry、Surface Session、Phase Run、Work 和 Action Execution。
- Service 统一时间格式和空值。
- TypeScript 追加稳定类型，删除 probe_work 正常展示。
- AppStagePanel 按设计九阶段展示。

**Step 4：提交**

~~~powershell
git add -- services/api/app/infrastructure/db/task_insight_repository.py services/api/app/application/task_insight.py services/api/app/api/v1/workstation/task_insight.py services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py app/workstation/src/types/task-insight.ts app/workstation/src/modules/tasks/components/AppStagePanel.vue app/workstation/src/modules/tasks/components/AppStagePanel.test.ts
git commit -m "[AI编程] feat: 展示App探索封存与会话状态；模块：Workstation/App Insight；数据库迁移：复用0045；前端菜单路由：无新增路由；测试风险：大任务聚合查询性能需压测"
~~~

## Task 10：文档、聚合回归与代码审核

**文件：**

- 修改：docs/专项能力与高频回归规则.md
- 新增：docs/App质检API接口.md（基于用户附件固化为仓库内正式 App API 文档）
- 修改：docs/superpowers/specs/2026-08-11-app-exploration-workflow-optimization-design.md（仅当实现细节需要同步）

**Step 1：更新专项回归规则**

沉淀：

- Surface reobserve 与 supplemental evidence 的边界。
- 探索阶段禁止 Inspection/Probe。
- 失败动作不得因 After Capture 改写。
- Evidence Index 封存和 Session 关闭顺序。
- 正式 App 质检只能消费 sealed Index。

**Step 2：运行后端聚合回归**

~~~powershell
python -m pytest services/api/tests/test_app_* services/api/tests/test_surface_* services/api/tests/test_capture_coordinator.py services/api/tests/test_action_execution_repository.py services/api/tests/test_evidence_manifest_repository.py services/api/tests/test_task_insight_api.py services/api/tests/test_worker.py services/api/tests/test_worker_app_route.py -q
~~~

若 PowerShell 不展开通配符，则改为 pytest 目录配合 -k：

~~~powershell
python -m pytest services/api/tests -q -k "app or surface or capture_coordinator or evidence_manifest or task_insight or worker"
~~~

**Step 3：运行前端回归**

~~~powershell
npm --prefix app/workstation test
npm --prefix app/workstation run build
~~~

**Step 4：运行静态与迁移校验**

~~~powershell
python -m compileall services/api/app
python -m pytest services/api/tests/test_app_exploration_workflow_migration.py services/api/tests/test_app_architecture_boundaries.py -q
git diff --check
~~~

**Step 5：按 requesting-code-review 做自审**

由于本任务未请求子代理，使用同一主任务执行结构化审核：

- 对照设计文档逐条检查 15 个验收场景。
- 检查所有失败路径是否保持事实真实性。
- 检查 sealed 后是否仍存在 Driver、Probe 或补采调用。
- 检查数据库唯一性、并发封存和幂等。
- 检查旧任务与旧 InteractionResult 兼容。
- 修正发现的问题后重新运行受影响测试和全部聚合回归。

**Step 6：提交**

~~~powershell
git add -- docs/专项能力与高频回归规则.md docs/App质检API接口.md docs/superpowers/specs/2026-08-11-app-exploration-workflow-optimization-design.md
git commit -m "[AI编程] docs: 沉淀App探索封存回归规则；模块：App/V2 Workflow；数据库迁移：是，新增0045；前端菜单路由：无新增路由；测试风险：真机与对象存储集成项已列明"
~~~

## 最终完成门禁

只有同时满足以下条件才能宣称完成：

- AppScreenshotExplorationWorkflow 不导入或调用 AppCaptureInspectionPublisher。
- ProductionAppV2RuntimeExecutor 不领取 ExplorerWorkType.PROBE。
- Timed Observation 不含补采血缘、不创建 Action Execution。
- SupplementalEvidencePolicy 继续阻断真正 Probe。
- 失败动作附加 After Capture 后仍保持失败。
- 访问账本和循环阈值能够确定性结束探索。
- Bootstrap、Action、Observation、Final Observation 均有最终 PASS/FAIL Manifest。
- Evidence Index 已封存、哈希可重算、封存后不可变。
- Session 在 Evidence Index 之后关闭，失败时明确 LOST/close_failed。
- Worker 只在 sealed Index 且 Session 终态后发布正式 Inspection。
- AppStagePanel 不再把 Probe 展示为正常 App 阶段。
- 所有目标测试、聚合回归、前端测试和构建通过。
- docs/专项能力与高频回归规则.md 已更新。
