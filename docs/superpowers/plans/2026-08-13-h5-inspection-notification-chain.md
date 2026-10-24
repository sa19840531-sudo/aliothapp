# H5 Inspection Notification Chain Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan.

**Goal:** 将 H5 已持久化、可判定的页面证据通过独立、可恢复、可审计的通知链路转换为规则质检工作项，并让完成门禁与任务详情能够证明链路没有丢失、重复或静默失败。

**Architecture:** 在现有 H5 证据交接与通用 `qc_inspection_work_item` 之间增加 H5 Publication 持久化领域。生产侧在同一事务写 Publication、成员快照与版本化 Outbox；消费侧使用独立 Redis Stream、权威 Inbox 和确定性逻辑 `rule_judge` Run，原子生成全部工作项并记录关联；Reconciler 只修复 H5 Publication 及其关联工作项。任务快照锁定 `off|shadow|enforced`，历史缺省为 `off`，App 质检链路保持不变。

**Tech Stack:** Python 3.12、FastAPI、asyncpg、PostgreSQL/Alembic、Redis Streams、pytest/pytest-asyncio、Vite、Vue 3、TypeScript、Vitest。

## Global Constraints

- 设计基线以 [2026-08-13-h5-inspection-notification-chain-design.md](../specs/2026-08-13-h5-inspection-notification-chain-design.md) 为准；实现不得把 H5 Publication 泛化到 App。
- 所有新注释和运维提示使用中文 UTF-8；单文件保持单一职责。
- `h5_inspection_notification_mode` 在任务创建时写入 `config_snapshot`，后续只读；历史快照缺字段必须解析为 `off`。
- `off` 保留当前同步交接；`shadow` 只写审计投影，不生成权威 Publication/Work Item；`enforced` 只走新链路，禁止同步发布与同步 drain 双写。
- Publication 的创建、成员冻结、首次 Outbox 必须同事务；每次重试或租约回收必须创建新版本 Outbox，不能复用已完成 Inbox 的旧消息。
- 单个 Publication 的全部 Work Item、规则成员、Publication-Work Item 关联和终态写入必须同事务；任一批失败不得留下部分权威结果。
- H5 消费者只认当前任务 epoch 和确定性逻辑 Run；不得依赖探索 Run 仍为 `running`。
- Completion Gate 只对 `enforced` H5 任务增加阻断；App 与历史 H5 任务的既有判定保持一致。
- 不修改无关未跟踪文件；每个提交只包含当前任务文件，提交说明遵循仓库 `[AI编程]` 约定。
- 本次按 `superpowers:executing-plans` 在当前会话内执行，不启用子代理。

---

### Task 1: Lock the H5 notification mode into task snapshots

**Files:**
- Create: `services/api/app/domain/task/h5_inspection_notification_mode.py`
- Modify: `services/api/app/domain/task/__init__.py`
- Modify: `services/api/app/core/config.py`
- Modify: `services/api/app/application/v2_task_entries.py`
- Modify: `.env.example`
- Test: `services/api/tests/test_h5_inspection_notification_mode.py`
- Test: `services/api/tests/test_config_env_loading.py`
- Test: `services/api/tests/test_v2_task_entrypoints.py`

**Interfaces:**
- `H5InspectionNotificationMode(StrEnum)`: `OFF`, `SHADOW`, `ENFORCED`。
- `parse_h5_inspection_notification_mode(task_config) -> H5InspectionNotificationMode`：缺字段返回 `OFF`，非法持久化值拒绝执行。
- `lock_h5_inspection_notification_mode(task_config, mode) -> dict[str, Any]`：只允许首次锁定，同值幂等，不允许改写。
- `WorkflowSettings.h5_inspection_notification_mode` 由 `ALIOTH_H5_INSPECTION_NOTIFICATION_MODE` 读取，默认 `off`。

**Steps:**

- [ ] 在领域测试覆盖合法枚举、历史缺省 `off`、首次锁定、同值幂等与改写拒绝。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_notification_mode.py -q`，确认因模块缺失失败。
- [ ] 实现独立模式领域对象并从 `domain.task` 导出。
- [ ] 扩展配置测试，覆盖环境变量合法值与非法值回退 `off`；先运行目标测试确认失败，再最小修改 `core/config.py` 和 `.env.example`。
- [ ] 扩展 V2 任务入口测试，断言 H5 任务快照写入部署模式、App 快照恒为 `off`，已有快照不可改写；先确认失败，再修改 `_task_config()` 的显式参数和调用点。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_notification_mode.py services/api/tests/test_config_env_loading.py services/api/tests/test_v2_task_entrypoints.py -q`。
- [ ] 提交：`[AI编程] feat(h5): 锁定H5通知模式快照；无数据库迁移；无需新增菜单路由；风险为历史快照兼容已覆盖单测`。

### Task 2: Add the assessment and publication persistence schema

**Files:**
- Create: `services/api/migrations/versions/20260813_0050_h5_inspection_notification.py`
- Create: `services/api/tests/test_h5_inspection_notification_migration.py`

**Interfaces and schema:**
- `qc_h5_page_type_assessment`：以 varchar `assessment_id` 为主键，保存 task/run/page type/status/reason/catalog 快照、引用 ID JSONB、协议版本与时间。
- `qc_h5_inspection_publication`：保存 publication identity、task/epoch/source run/capture/assessment/snapshot/mode/state、租约、尝试计数、消息版本、错误诊断和终态时间。
- `qc_h5_inspection_publication_rule`：冻结规则快照与执行模式，唯一键 `(publication_id, rule_id)`。
- `qc_h5_inspection_publication_work_item`：关联 Publication 和通用 Work Item，唯一约束防止重复关联。
- 必要的状态、模式、正数计数、租约一致性 CHECK，以及 task/epoch/state、lease、source capture、work item 索引。

**Steps:**

- [ ] 先写迁移合同测试，断言 `down_revision == "20260813_0049"`、四张表、FK、唯一键、CHECK、索引和完整 downgrade。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_notification_migration.py -q`，确认因迁移缺失失败。
- [ ] 参考现有 `0049` 的证据身份约束与工作项迁移风格实现 `upgrade()`/`downgrade()`；JSONB 默认和 enum-like CHECK 保持显式。
- [ ] 运行目标迁移测试，并运行 `.venv\Scripts\python.exe -m alembic -c services/api/alembic.ini heads`，确认单一 head。
- [ ] 提交：`[AI编程] feat(h5): 新增H5通知持久化模型；引入0050数据库迁移；无需前端菜单路由；风险为真实PG升级待集成验证`。

### Task 3: Persist page-type assessments and build qualified H5 inventory

**Files:**
- Create: `services/api/app/infrastructure/db/h5_page_type_assessment_repository.py`
- Modify: `services/api/app/application/h5_evidence_handoff.py`
- Modify: `services/api/app/infrastructure/db/h5_evidence_handoff_repository.py`
- Modify: `services/api/app/worker.py`
- Create: `services/api/tests/test_h5_page_type_assessment_repository.py`
- Modify: `services/api/tests/test_h5_evidence_handoff.py`

**Interfaces:**
- `PostgresH5PageTypeAssessmentRepository.save_set(assessment_set)`：同一 assessment identity 幂等 upsert，内容漂移时报错。
- `H5EvidenceCapture` 增加 `page_type_assessment_id`、证据制品 ID/校验和摘要和 `source_epoch`，不携带 DOM/截图原文。
- `H5EvidenceHandoffSummary` 返回不可变的合格 Capture inventory，供后续 Publication planner 使用。

**Steps:**

- [ ] 为 assessment repository 写事务、幂等、身份冲突和 JSONB 序列化测试；先运行确认失败。
- [ ] 最小实现 repository，并把探索闭环生成的 `PageTypeAssessmentSet` 持久化接入 worker 既有闭包保存点。
- [ ] 扩展 H5 evidence reader 测试：只选择 `after_action + recognized + screenshot + dom + before/after` 完整记录，要求 assessment 引用和当前 epoch，返回稳定排序与证据摘要。
- [ ] 运行 `test_h5_evidence_handoff.py` 确认新增断言先失败；修改 SQL reader 和 handoff contracts，不改变 `off` 模式同步 publisher 行为。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_page_type_assessment_repository.py services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_worker_h5_v2_route.py -q`。
- [ ] 提交：`[AI编程] feat(h5): 持久化页面类型评估并生成合格证据清单；复用0050迁移；无需前端菜单路由；风险为探索闭包异常路径已做回归`。

### Task 4: Define the H5 Publication domain state machine

**Files:**
- Create: `services/api/app/domain/inspection/h5_publication.py`
- Modify: `services/api/app/domain/inspection/__init__.py`
- Create: `services/api/tests/test_h5_inspection_publication.py`

**Interfaces:**
- `H5InspectionPublicationState`: `pending|leased|retry_wait|published|not_applicable|needs_review|failed|cancelled|superseded`。
- `H5InspectionPublicationIdentity` 和 `build_h5_inspection_publication_id(...)`：由 task/epoch/capture/assessment/ruleset snapshot/mode 生成稳定 ID。
- `H5InspectionPublication`：验证状态、租约、attempt/message version、源 identity 与终态字段一致性。
- `H5InspectionPublicationAvailable`：消息类型 `alioth.v2.h5-inspection-publication.available`，payload 只包含 publication/task/epoch/version/occurred_at/protocol_version。
- 显式状态转换函数拒绝越权跳转和旧消息版本。

**Steps:**

- [ ] 写领域测试覆盖稳定 identity、序列化、所有合法转换、非法转换、租约一致性和消息版本单调性。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_publication.py -q`，确认失败。
- [ ] 实现最小领域合同，复用 `domain.protocol` 的时间与稳定哈希工具。
- [ ] 运行目标测试及 `.venv\Scripts\python.exe -m ruff check services/api/app/domain/inspection/h5_publication.py services/api/tests/test_h5_inspection_publication.py`。
- [ ] 提交：`[AI编程] feat(h5): 定义通知发布状态机与消息合同；无新增数据库迁移；无需前端菜单路由；风险为状态转换穷举已覆盖单测`。

### Task 5: Implement atomic Publication creation, leasing, retry, and completion

**Files:**
- Create: `services/api/app/application/h5_inspection_publication.py`
- Create: `services/api/app/infrastructure/db/h5_inspection_publication_repository.py`
- Create: `services/api/tests/test_h5_inspection_publication_repository.py`

**Interfaces:**
- `H5InspectionPublicationRepository.create_with_outbox(...)`：Publication、冻结成员、`message_version=1` Outbox 同事务。
- `claim(publication_id, consumer_id, expected_version, now, lease_seconds)`：使用行锁，校验当前 epoch/mode/state/version，返回 lease token。
- `mark_published(...)`、`mark_not_applicable(...)`、`mark_needs_review(...)`：要求有效 lease。
- `schedule_retry(...)`：在同事务增加 attempt/version、写全新 Outbox，并清理 lease。
- `mark_failed_safely(...)`：业务事务回滚后使用独立受保护事务记录诊断；重复调用幂等。
- `find_reconcilable(now, limit)`：返回缺通知、到期 retry 和租约过期记录。

**Steps:**

- [ ] 用 fake transaction/cursor 写 repository 测试，先覆盖创建原子性与同 identity 幂等，再覆盖租约 CAS、旧版本拒绝、重试新 Outbox 和失败落账。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_publication_repository.py -q`，确认失败。
- [ ] 实现 repository SQL 和 application port/service；Outbox event ID 必须包含 publication ID 与 message version。
- [ ] 增加并发声明测试：同一 publication 只有一个 consumer 获得 lease；过期 lease 可回收但版本递增。
- [ ] 运行目标测试和现有 `test_outbox_writer.py`，确认没有改变通用 Outbox 语义。
- [ ] 提交：`[AI编程] feat(h5): 实现通知发布原子仓储与租约重试；复用0050迁移；无需前端菜单路由；风险为并发租约已用仓储合同测试覆盖`。

### Task 6: Atomically compile a Publication into inspection work items

**Files:**
- Modify: `services/api/app/application/inspection_candidate_publish.py`
- Modify: `services/api/app/infrastructure/db/inspection_work_item_repository.py`
- Create: `services/api/app/application/h5_inspection_publication_consumer.py`
- Modify: `services/api/app/infrastructure/db/h5_inspection_publication_repository.py`
- Create: `services/api/tests/test_h5_inspection_publication_consumer.py`
- Modify: `services/api/tests/test_inspection_work_item_publish.py`
- Modify: `services/api/tests/test_inspection_work_item_repository.py`

**Interfaces:**
- 从现有 candidate publisher 提取无副作用的 `compile_capture_batches(...)`，App 调用结果不变。
- `publish_publication_work_items(...)`：一个数据库事务写确定性逻辑 `qc_agent_run`、所有 Work Item/规则成员、Publication-Work Item 关联及 Publication 终态。
- 逻辑 Run identity：`h5-inspection-consumer:{task_id}:{epoch}`；已有一致记录幂等复用，跨任务/epoch 冲突拒绝。
- 规则 member 的 `manual_review` 仍是规则执行状态；有 snapshot 和 active-session 混合成员时 Publication 可终止为 `needs_review`，已生成 snapshot Work Item 必须保留关联。

**Steps:**

- [ ] 先为纯编译器写测试，固定规则排序、批次 identity、snapshot-only 过滤和 App 原行为；运行确认失败后再重构。
- [ ] 写 consumer 测试：零规则 `not_applicable`、全 snapshot `published`、混合 `needs_review`、中途异常全回滚、重放不重复、逻辑 Run 确定性。
- [ ] 运行 consumer 测试确认失败；实现 application consumer 与 repository 原子方法。
- [ ] 修改通用 Work Item 仓储，使“生产 Run 必须等于 Capture Run”的约束只通过明确的 H5 Publication 原子入口放宽，普通/App `publish()` 仍维持原约束。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_publication_consumer.py services/api/tests/test_inspection_work_item_publish.py services/api/tests/test_inspection_work_item_repository.py services/api/tests/test_app_inspection_work_item_publish.py -q`。
- [ ] 提交：`[AI编程] feat(h5): 原子生成通知质检工作项；无新增数据库迁移；无需前端菜单路由；风险为App发布行为已纳入回归`。

### Task 7: Add the dedicated Redis Stream, Inbox consumer, and worker loop

**Files:**
- Modify: `services/api/app/application/outbox.py`
- Modify: `services/api/app/infrastructure/db/outbox_repository.py`
- Modify: `services/api/app/infrastructure/queue/redis_stream.py`
- Create: `services/api/app/agents/runtime/h5_inspection_publication_wakeup.py`
- Modify: `services/api/app/worker.py`
- Modify: `services/api/tests/test_redis_stream_delivery.py`
- Modify: `services/api/tests/test_outbox_relay.py`
- Create: `services/api/tests/test_h5_inspection_publication_wakeup.py`
- Modify: `services/api/tests/test_v2_delivery_worker_loop.py`

**Interfaces:**
- Stream：`alioth:v2:wakeup:h5-inspection-publication`；稳定 consumer group：`alioth-v2-h5-inspection-publication-consumer`。
- `H5InspectionPublicationWakeupHandler.handle(payload)`：校验权威 Outbox/Inbox 后按 publication ID 与 version claim，再调用 consumer。
- Inbox completion 只在 Publication 达到本消息对应的可确认状态后发生；可重试错误不 ACK 业务完成状态。
- worker loop 支持 `XREADGROUP`、`XAUTOCLAIM`、停机取消和独立健康日志，不复用 exploration/inspection group。

**Steps:**

- [ ] 扩展路由测试，先断言新消息只进入专属 Stream，旧 inspection 消息路由不变；运行确认失败后修改白名单和路由。
- [ ] 写 wakeup handler 测试：合法消费、旧版本幂等、非权威消息拒绝、可重试异常 schedule retry、永久异常安全落账。
- [ ] 运行 handler 测试确认失败后实现 handler，并复用现有 `InboxGuardedWakeupHandler` 的权威 Outbox 校验。
- [ ] 扩展 worker loop 测试，覆盖建组、收取、autoclaim、consumer name、取消；先失败再接入构建函数和后台任务。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_redis_stream_delivery.py services/api/tests/test_outbox_relay.py services/api/tests/test_h5_inspection_publication_wakeup.py services/api/tests/test_v2_delivery_worker_loop.py -q`。
- [ ] 提交：`[AI编程] feat(h5): 接入独立通知Stream与Inbox消费者；无新增数据库迁移；无需前端菜单路由；风险为Redis故障恢复已覆盖重领测试`。

### Task 8: Switch H5 handoff by locked mode without dual-writing

**Files:**
- Modify: `services/api/app/application/h5_evidence_handoff.py`
- Modify: `services/api/app/worker.py`
- Modify: `services/api/tests/test_h5_evidence_handoff.py`
- Modify: `services/api/tests/test_worker_h5_v2_route.py`
- Modify: `services/api/tests/test_worker_h5_v2_finalization.py`

**Interfaces:**
- `off`：保持现有逐 Capture 同步 publisher 与同步 drain。
- `shadow`：只返回/记录非权威审计差异，不调用 `create_with_outbox()`，不生成 Work Item，不影响 Completion Gate。
- `enforced`：按合格 capture inventory 创建权威 Publication；worker 不直接调用 capture publisher，也不立即 drain。
- mode 必须从任务 `config_snapshot` 解析，禁止运行时环境变量覆盖已创建任务。

**Steps:**

- [ ] 把 H5 handoff 测试参数化为三种模式，断言 publisher/publication writer 调用互斥；先运行确认新断言失败。
- [ ] 修改 handoff service 端口和实现，把权威 Publication 创建委托给 Task 5 service；shadow 只返回差异统计。
- [ ] 扩展 worker route/finalization 测试，断言 enforced 不同步 drain 且任务暂不完成、off 保持现状、App route 无调用。
- [ ] 修改 worker 组合与 `_process_h5_v2_one()` 分支，确保只读取锁定快照；删除任何基于部署设置动态切换既有任务的路径。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_evidence_handoff.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_worker_h5_v2_finalization.py services/api/tests/test_worker_app_route.py -q`。
- [ ] 提交：`[AI编程] feat(h5): 按任务快照切换通知链路并消除双写；无新增数据库迁移；无需前端菜单路由；风险为三模式与App隔离已覆盖回归`。

### Task 9: Reconcile missing publications, retries, leases, and H5-linked work

**Files:**
- Create: `services/api/app/application/h5_inspection_publication_reconciler.py`
- Modify: `services/api/app/infrastructure/db/h5_inspection_publication_repository.py`
- Modify: `services/api/app/worker.py`
- Create: `services/api/tests/test_h5_inspection_publication_reconciler.py`

**Interfaces:**
- `reconcile_missing_publications(limit)`：仅扫描 `enforced`、当前 epoch、合格 after-action capture，补建不存在的 Publication。
- `reconcile_delivery(limit, now)`：为缺 Outbox、到期 `retry_wait`、过期 lease 写新版本 Outbox。
- `reconcile_linked_work(limit)`：仅处理 `qc_h5_inspection_publication_work_item` 关联的 H5 Work Item；普通/App Work Item 不在范围内。
- “已判定但 completion 未落账”重放必须调用既有幂等结果终结路径，不重复写规则执行。

**Steps:**

- [ ] 写 reconciler 测试覆盖 capture 后崩溃补建、relay 前崩溃补消息、租约过期、新版本 Inbox 可消费、已判定重放、非 H5 工作项不触碰。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_publication_reconciler.py -q`，确认失败。
- [ ] 实现三类有上限的 reconcile 方法，每批独立事务并记录结构化计数，不在循环中持有长事务。
- [ ] 在 worker 可靠投递后台循环中以独立周期调用 reconciler；停机和单批异常不能终止其它 relay/sweeper。
- [ ] 运行目标测试以及 `test_inspection_work_item_execution.py`、`test_worker.py`。
- [ ] 提交：`[AI编程] feat(h5): 增加通知与关联工作项对账恢复；无新增数据库迁移；无需前端菜单路由；风险为跨进程并发依赖PG锁语义已做合同测试`。

### Task 10: Extend the H5-only Completion Gate

**Files:**
- Modify: `services/api/app/domain/task/completion.py`
- Modify: `services/api/app/infrastructure/db/task_finalization_repository.py`
- Modify: `services/api/tests/test_task_completion_gate.py`
- Modify: `services/api/tests/test_task_finalization_persistence.py`

**Interfaces:**
- 新 facts：`h5_notification_enforced`、`qualified_h5_capture_count`、`h5_publication_count`、`h5_nonterminal_publication_count`、`h5_failed_publication_count`、`h5_unlinked_publication_count`、`h5_pending_delivery_count`。
- 新阻断原因分别表达缺 Publication、未终态、失败/人工复核、缺 Work Item 关联与未完成投递；不要把诊断压成一个模糊原因。
- SQL 在任务 finalization advisory lock 内按当前 epoch 和锁定模式统计；历史/off/shadow/App 返回零影响。

**Steps:**

- [ ] 扩展纯领域 gate tests：enforced 的 expected-vs-actual、各种非终态/失败/缺关联/未投递阻断，以及 off/shadow/App 不阻断。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_task_completion_gate.py -q`，确认失败后实现 facts、协议序列化和决策顺序。
- [ ] 扩展 persistence 测试，断言 `_GATE_FACTS_SQL` 使用 task/epoch/mode 统计新表和 Outbox/Inbox，行解析字段完整；先失败再修改 repository。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_task_completion_gate.py services/api/tests/test_task_finalization_persistence.py services/api/tests/test_worker_h5_v2_finalization.py -q`。
- [ ] 提交：`[AI编程] feat(h5): 完成门禁校验通知链路闭环；无新增数据库迁移；无需前端菜单路由；风险为历史与App兼容已覆盖单测`。

### Task 11: Project H5 notification status into the existing task detail page

**Files:**
- Modify: `services/api/app/application/task_insight.py`
- Modify: `services/api/app/infrastructure/db/task_insight_repository.py`
- Modify: `services/api/tests/test_task_insight_service.py`
- Modify: `services/api/tests/test_task_insight_repository.py`
- Modify: `services/api/tests/test_task_insight_api.py`
- Modify: `app/workstation/src/types/task-insight.ts`
- Modify: `app/workstation/src/modules/tasks/pages/TaskDetailView.vue`
- Create: `app/workstation/src/modules/tasks/pages/TaskDetailView.test.ts`

**Interfaces:**
- 复用 `GET /api/v1/workstation/tasks/{task_id}/inspection-work-items`，在响应中增加 `h5_notification`，不新增路由。
- 投影字段：锁定模式、qualified/publication/published/nonterminal/needs_review/failed/pending_delivery 计数、最近错误码和最近更新时间；不返回证据原文。
- 现有“持久化质检工作项”面板增加 H5 通知摘要、状态 badge 和可操作诊断；非 H5/off 任务不显示该块。

**Steps:**

- [ ] 扩展 repository/service/API 测试，先断言新投影、空态、control mode 和敏感字段不泄露，运行确认失败。
- [ ] 实现单次聚合 SQL 和 service 映射；保持现有 `summary/items` 合同兼容。
- [ ] 扩展 TypeScript 类型并编写 Vue 测试：enforced 正常、待投递、needs_review/failed、off 隐藏；先运行 `pnpm --dir app/workstation test -- TaskDetailView.test.ts` 确认失败。
- [ ] 最小修改现有面板与样式，不新增菜单和路由。
- [ ] 运行 `.venv\Scripts\python.exe -m pytest services/api/tests/test_task_insight_repository.py services/api/tests/test_task_insight_service.py services/api/tests/test_task_insight_api.py -q` 和 `pnpm --dir app/workstation test -- TaskDetailView.test.ts`。
- [ ] 提交：`[AI编程] feat(workstation): 展示H5通知闭环状态；无数据库迁移；复用现有任务详情无需菜单路由；风险为极端长诊断已做截断展示`。

### Task 12: Document operations and verify the complete chain

**Files:**
- Modify: `docs/专项能力与高频回归规则.md`
- Modify: `docs/superpowers/specs/2026-08-13-h5-inspection-notification-chain-design.md` only if implementation discovered a verified contract correction
- Create: `services/api/tests/test_h5_inspection_notification_chain.py`

**Scenario coverage:**
- 正常 capture → assessment → publication/outbox → relay → inbox → logical run → work item → rule execution → gate。
- capture 提交后进程退出、Outbox relay 前退出、消费 lease 中退出、规则已判定但 Publication 未终结。
- 重复消息、旧版本消息、并发 consumer、任务 epoch 变化、任务取消、needs_review、永久失败。
- `off` 兼容、`shadow` 无权威副作用、App 链路完全不变。

**Steps:**

- [ ] 先写跨层 scenario tests，至少让故障注入场景在缺少最后接线时失败，再补齐遗漏的组合逻辑。
- [ ] 将“版本化重投、确定性逻辑 Run、H5-only reconciler、完成门禁 expected-vs-actual、模式快照不可变”写入专项回归规则。
- [ ] 运行 H5/通知目标套件：`.venv\Scripts\python.exe -m pytest services/api/tests/test_h5_inspection_notification_chain.py services/api/tests/test_h5_inspection_publication.py services/api/tests/test_h5_inspection_publication_repository.py services/api/tests/test_h5_inspection_publication_consumer.py services/api/tests/test_h5_inspection_publication_wakeup.py services/api/tests/test_h5_inspection_publication_reconciler.py -q`。
- [ ] 运行后端全量：`.venv\Scripts\python.exe -m pytest services/api/tests -q`。
- [ ] 运行静态检查：`.venv\Scripts\python.exe -m ruff check services/api/app services/api/tests` 和 `.venv\Scripts\python.exe -m compileall -q services/api/app`。
- [ ] 运行前端全量：`pnpm --dir app/workstation test` 和 `pnpm --dir app/workstation build`。
- [ ] 运行迁移校验：`.venv\Scripts\python.exe -m alembic -c services/api/alembic.ini heads`；若本地可用测试 PG，再执行 upgrade/downgrade/upgrade 往返并记录结果。
- [ ] 检查 `git diff --check`、`git status --short` 和提交文件清单，确保没有无关文件。
- [ ] 提交：`[AI编程] test(h5): 固化通知链路故障恢复回归；复用0050迁移；无需新增菜单路由；风险为外部Redis与PG生产拓扑需部署后观测`。

### Task 13: Review, merge to dev_1.5, and verify the merged branch

**Files:**
- Review all files changed by this plan; do not add generated build outputs.

**Steps:**

- [ ] 使用 `superpowers:requesting-code-review` 的检查清单对照设计逐项审查，修复所有 P0/P1/P2 问题并重跑受影响测试。
- [ ] 使用 `superpowers:verification-before-completion` 获取新鲜的后端、前端、迁移和 diff 证据；不复用早期测试输出作完成声明。
- [ ] 使用 `superpowers:finishing-a-development-branch` 检查功能分支提交历史和基线差异。
- [ ] 在主工作区确认当前分支为 `dev_1.5` 且无相关未提交改动；保留用户已有的无关未跟踪文件。
- [ ] 使用非破坏性 merge 将 `codex/h5-inspection-notification` 合并至 `dev_1.5`；发生冲突时只解决本方案文件，不覆盖用户改动。
- [ ] 在合并后的 `dev_1.5` 至少重跑 H5 通知目标后端套件、前端 TaskDetailView 测试、Alembic heads、`git diff --check`。
- [ ] 输出合并提交、测试证据、数据库迁移编号、模式启用方式和仍需部署环境验证的边界。
