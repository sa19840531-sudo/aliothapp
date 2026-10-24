# App 探索 Workflow 优化设计

> 日期：2026-08-11
>
> 状态：已完成方案确认，待实施计划
>
> 适用范围：正式 App 质检任务中的 App Exploration 内部子流程

## 1. 背景

Alioth 当前正式 App 质检入口同时承担构建上传、设备探索、证据采集、规则质检、报告与审核。附件中的目标流程要求 App 探索先形成完整、不可变、可审计的证据结果，再由下游规则质检只读消费。

当前实现已具备 Surface Session、Bootstrap、Surface Phase、Explorer Work、动作前后取证和 Evidence Manifest 等基础能力，但仍存在以下关键偏差：

- 探索截图会直接发布正式 Inspection，探索与规则质检职责耦合。
- App 正常路径仍会排空 Probe 补采工作，封存前后的边界不明确。
- 启动广告倒计时被当作禁用补采而直接结束，无法完成有界重新观察。
- 动作失败可能在取得 After Capture 后被软化为成功，执行事实失真。
- LLM 可以提出终止，但缺少覆盖率、Frontier、循环和预算共同决定的确定性终止规则。
- Bootstrap 与动作证据的 Manifest 处理不一致，Evidence Index 尚未形成。
- 探索结束后没有统一执行证据封存和 Surface Session 正常关闭。

## 2. 目标与非目标

### 2.1 目标

本次优化将 App Exploration 建设为边界完整、可以独立收口的内部子流程：

1. 完成 Bootstrap、Surface Readiness、页面探索、最终观察、证据校验、证据封存和会话关闭。
2. 将启动广告倒计时、弹窗过渡等行为建模为有界重新观察，而不是 Probe 或规则补采。
3. 以真实的执行结果和效果验证结果记录每次动作。
4. 使用确定性领域规则决定探索完成、证据不足或技术失败。
5. 生成不可变 Evidence Index，作为后续正式规则质检的唯一 App 证据入口。
6. 保持现有正式 App 质检 API 入口不变，并向任务 Insight 与前端追加探索状态。

### 2.2 非目标

本次不包含：

- 重写正式规则 Judge。
- 重写报告生成与人工审核。
- 新增独立的 App 探索任务 API 或第二套任务模型。
- 对历史任务强制回填 Evidence Index。
- 允许正式规则质检反向唤醒设备或触发补采。

## 3. 方案选择

### 3.1 方案 A：最小补丁

仅恢复启动广告倒计时观察并移除 Probe。改动小，但无法解决探索和质检耦合、动作事实失真、证据封存与会话关闭问题，因此不采用。

### 3.2 方案 B：边界完整、分阶段落地

在现有正式 App 质检任务内部保留 App Exploration 子流程，完整收口设备行为和证据，再将封存结果交给下游。该方案复用现有任务、运行时和页面结构，同时建立稳定职责边界。

本设计采用方案 B。

### 3.3 方案 C：独立探索任务

新增探索任务类型、API 和前端入口。边界最干净，但会形成第二套任务模型并带来较高迁移成本，当前阶段不采用。

## 4. 总体架构与职责边界

正式入口保持不变：

~~~text
/api/v1/app-builds
    → /api/v1/tasks（target_platform=app）
    → App Exploration Workflow
    → 正式规则质检
    → 报告与审核
~~~

App Exploration Workflow 由以下职责组成：

~~~text
AppExplorationWorkflow
├─ BootstrapCoordinator
│  └─ 会话创建、能力发现、安装、启动、基线取证
├─ SurfaceReadinessCoordinator
│  └─ 系统弹窗、授权、启动广告、弹窗链、稳定页判定
├─ ExplorationLoop
│  └─ 页面理解、候选生成、动作决策、执行、效果验证
├─ ExplorationTerminalEvaluator
│  └─ 目标完成、无进展、循环、预算、环境失败判定
└─ ExplorationClosureService
   └─ 最终观察、证据校验、Evidence Index 封存、会话关闭
~~~

探索阶段允许：

- 设备安装、启动和交互。
- 页面截图、UI Tree 与设备状态采集。
- 有界等待和重新观察。
- 页面与控件语义理解。
- 动作执行及效果验证。
- 证据持久化、Evidence Index 封存和 Surface Session 关闭。

探索阶段禁止：

- 正式规则判定和 Inspection 发布。
- 创建或排空 Probe 补采工作。
- 生成正式质检报告。
- Evidence Index 封存后重新连接设备补采。

现有代码的职责调整：

- ProductionAppV2RuntimeExecutor 只编排探索生命周期，不再排空 Probe。
- AppScreenshotExplorationWorkflow 不再调用 AppCaptureInspectionPublisher。
- AppExplorationTurnRunner 的 App 正常路径只处理 Exploration Work。
- Surface 定时截图改为确定性重新观察，不归类为补充证据请求。
- Worker 只有取得已封存探索结果后才允许进入正式规则质检。

## 5. 主状态机

~~~text
CREATED
  → BOOTSTRAPPING
  → SURFACE_RESOLVING
  → APP_READY
  → EXPLORING
  → FINAL_OBSERVING
  → EVIDENCE_VALIDATING
  → EVIDENCE_SEALING
  → SESSION_DRAINING
  → EXPLORATION_COMPLETED
~~~

证据不满足最低要求时：

~~~text
FINAL_OBSERVING
  → EVIDENCE_VALIDATING
  → EVIDENCE_INSUFFICIENT
  → SESSION_DRAINING
~~~

设备断开、租约丢失、数据库提交失败、对象存储失败等进入独立技术失败状态，不与业务结果混淆。

探索子流程只生成两类业务结果：

- exploration_completed
- evidence_insufficient

技术失败记录在 Run 状态和审计事件中，不生成伪造的探索成功结果。

## 6. Surface Readiness

Surface Phase 固定为：

~~~text
SYSTEM_DIALOG
  → AUTH_GATE
  → STARTUP_AD
  → POPUP_CHAIN
  → STABLE_PAGE
~~~

启动广告倒计时、关闭弹窗后的页面过渡统一使用有界重新观察：

~~~text
识别倒计时或过渡状态
  → 检查等待预算
  → 等待策略允许的时间
  → 重新截图并保存证据
  → 重新识别当前 Surface Phase
~~~

重新观察不是 Probe，也不是规则补采。只有等待预算耗尽且仍不能进入稳定页面时，才结束为 evidence_insufficient。

APP_READY 必须在稳定页面证据提交成功后原子写入，不得先发布状态再补写证据。

## 7. 单次动作状态机

~~~text
PLAN
  → VALIDATE_INTENT
  → CAPTURE_BEFORE
  → EXECUTE_ACTION
  → CAPTURE_AFTER
  → VERIFY_EFFECT
  → PERSIST_RESULT
  → ADVANCE_FRONTIER
~~~

约束如下：

- Before Capture 失败时不得执行动作。
- 动作失败后可以尽力取得 After Capture，但不得因此改写执行结果。
- After Capture 失败时保留动作执行结果，效果标记为未确认。
- 动作结果和证据未持久化前不得创建下一项 Exploration Work。
- FINAL_OBSERVING 之后不得创建新动作。

动作结果拆分为两个正交维度：

~~~text
execution_outcome:
- succeeded
- failed
- rejected
- timeout

verification_outcome:
- effect_confirmed
- no_effect
- effect_unexpected
- unverified
~~~

## 8. ActionIntent 安全与新鲜度

ActionIntent 必须携带：

- surface_session_id
- source_capture_id
- page_identity
- candidate_id
- candidate_source_signature
- action_type
- target
- expected_effect
- model_risk_hint

领域层在执行前独立校验：

1. Session 身份、状态和租约有效。
2. 当前页面与 source_capture 对应。
3. candidate 仍存在、未过期且没有超过重试阈值。
4. UI Tree 节点、坐标和截图来源一致。
5. 动作没有越过风险策略。
6. 已获得 Session Command Permit。

LLM 风险提示不是可信决策。领域策略至少拦截支付、购买、转账、删除账号或数据、对外发送消息、提交敏感信息、安装其他应用、跳转不受控外部应用和高风险系统权限修改。

## 9. Timed Observation

重新观察使用独立请求，不创建 Action Execution：

~~~text
TimedObservationRequest
- session_id
- reason
- wait_seconds
- source_capture_id
- expected_surface_phase
- observation_attempt
- observation_budget
~~~

执行过程只包含等待、截图或 UI Tree、证据持久化和 Surface Phase 重判定。每个 Phase 有最大重新观察次数，并受总探索时长约束。

## 10. 确定性终止

LLM 只能提出 TerminalIntent，最终结果由 ExplorationTerminalEvaluator 决定。

exploration_completed 必须同时满足：

- 已进入稳定页面。
- 基线证据完整。
- 达到最低探索覆盖要求。
- 没有未完成动作和未持久化证据。
- Frontier 已耗尽，或剩余候选均不可执行、已访问或超出风险策略。

evidence_insufficient 适用于：

- 无法进入稳定页面。
- 必需证据缺失或校验失败。
- 预算耗尽但未达到最低探索覆盖。
- 最终页面状态无法确认。
- 证据与动作来源无法建立可信关联。

循环检测至少组合页面身份、截图特征、UI Tree 特征、已执行候选 ID 和动作效果。同一页面、候选和效果组合达到策略阈值后停止继续执行。

预算至少包含：

- 最大总探索时长。
- 最大动作数量。
- 单候选最大尝试次数。
- 单 Surface Phase 最大重新观察次数。
- 连续无页面变化阈值。
- 相同页面、候选、效果组合重复阈值。

## 11. 探索结果契约

~~~text
AppExplorationResult
- task_id
- run_id
- surface_session_id
- outcome
- terminal_reason_code
- evidence_index_id
- evidence_index_hash
- coverage_summary
- evidence_gaps
- started_at
- completed_at
~~~

其中 outcome 只允许 exploration_completed 和 evidence_insufficient。

## 12. Evidence Index

采用索引头与有序条目结构：

~~~text
AppEvidenceIndex
- id
- task_id
- run_id
- build_id
- surface_session_id
- status: building / sealed
- outcome
- terminal_reason_code
- content_sha256
- sealed_at
- created_at

AppEvidenceIndexEntry
- index_id
- sequence_no
- capture_id
- evidence_manifest_id
- evidence_manifest_version
- semantic_role
- surface_phase
- exploration_work_id
- action_execution_id
- candidate_id
- page_identity
- captured_at
- metadata
~~~

semantic_role 使用稳定枚举：

- bootstrap
- surface_before
- surface_after
- surface_reobserve
- action_before
- action_after
- final_observation

底层 Runtime 的 before_action 和 after_action 保持兼容，丰富业务角色只保存在 Evidence Index。Artifact 类型在领域层统一为 screenshot、ui_tree 和 device_snapshot；ui_snapshot 只允许在兼容适配层出现。

证据处理链：

~~~text
Artifact
  → Capture
  → Evidence Manifest PENDING
  → 校验对象、大小、摘要、来源和关联
  → 追加 PASS 或 FAIL Manifest 版本
  → PASS 版本进入 Evidence Index
~~~

Bootstrap、Surface、Action 和 Final Observation 使用同一套 Manifest 校验。

Final Observation 是结束探索后的独立审计动作。若总探索 deadline 刚好耗尽，关闭编排只为该最终观察提供最多 30 秒的独立宽限窗口；该窗口不能继续执行候选动作、Surface Phase 或补采，也不能改变已确定的业务 outcome。

Evidence Index 封存前必须满足：

- 没有未完成 Exploration Work。
- 没有执行中 Action Execution。
- 没有 PENDING Manifest。
- 所有条目均引用 PASS Manifest。
- 必需证据角色已满足，或已生成明确 Evidence Gap。
- 条目顺序与引用关系校验通过。

封存时按 sequence_no 规范化序列化并计算 SHA-256，再原子写入 outcome、terminal reason、hash 和 sealed_at。封存后禁止修改索引头、增删条目或替换 Artifact 引用。

幂等要求：

- 每个 Exploration Run 只能有一个 Evidence Index。
- 重复封存返回原有结果。
- 相同 sequence_no 与证据引用不能重复写入。
- Session 关闭失败不撤销已经封存的 Evidence Index。

## 13. Surface Session 关闭

关闭顺序固定为：

~~~text
Evidence Index SEALED
  → Session ACTIVE 转为 DRAINING
  → 检查未完成命令和关闭就绪性
  → Seal Session
  → 调用设备 Runtime 关闭
  → CLOSED 或 LOST
~~~

关闭失败必须记录 session_lost 或 close_failed 审计事实。已封存证据保持有效，但前端和任务 Insight 必须展示会话关闭异常。

封存成功但关闭失败时，Run 以 `incomplete/needs_review` 收口而不是伪装成功；Worker 仍须调用 sealed-only Inspection Publisher 消费已封存快照，最终报告保留关闭异常。

## 14. 下游交接门禁

正式规则质检只有在以下条件成立时才可开始：

- AppExplorationResult 已持久化。
- Evidence Index 状态为 sealed。
- Evidence Index 哈希校验通过。
- 业务结果允许进入规则质检策略。

下游只能读取 Evidence Index 引用的证据，不得调用 SurfaceDriver、创建 Probe 或重新打开 Session。

## 15. API 与前端投影

正式任务创建接口保持不变。任务 Insight 追加 app_exploration：

~~~text
app_exploration:
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

Evidence Index 提供只读视图：

~~~text
evidence_index:
  id
  status
  outcome
  content_sha256
  sealed_at
  entries[]
  gaps[]
~~~

旧任务没有 Evidence Index 时返回 null，不伪造数据。只有 sealed Evidence Index 可以对下游表现为可用证据。

兼容读取以“是否存在新探索事实”为准：Evidence Index、当前 Session 的 Exploration Work、Surface Phase 或 Action 均不存在时，`app_exploration` 必须为 null；不能仅因历史 Surface Session 存在就推断新流程已运行。

AppStagePanel 调整为：

~~~text
1. Build Validation
2. Bootstrap
3. Surface Readiness
4. Page Exploration
5. Evidence Validation
6. Evidence Sealing
7. Session Closure
8. Formal Inspection
9. Report / Review
~~~

Probe 不再展示为 App 正常阶段。前端必须区分进行中、正常完成、证据不足、技术失败和会话关闭失败，并展示终止原因、覆盖摘要、证据缺口、Evidence Index 哈希和 Session 最终状态。

## 16. 审计事件

至少记录：

- app_exploration_started
- app_ready_committed
- surface_reobserved
- action_intent_rejected
- action_execution_recorded
- action_effect_verified
- exploration_terminal_decided
- evidence_index_sealed
- surface_session_draining
- surface_session_closed
- surface_session_lost

审计事件只描述已经发生并持久化的事实。日志不能替代 Artifact、Manifest 或 Evidence Index。

## 17. 数据库迁移与兼容

- 新增 Evidence Index 头表和条目表。
- 新增必要的唯一性、外键、顺序和不可变约束。
- 旧任务不强制回填，查询返回 null。
- 新任务冻结 app_exploration_v2 Workflow 版本。
- 部署前已经运行中的旧任务继续使用原版本，避免中途切换状态机。
- 迁移只新增结构，不删除已有 Evidence、Manifest、Probe 或 Inspection 数据。
- API 新字段使用追加式兼容策略。

## 18. 测试策略

实现严格采用测试驱动开发：先编写失败测试，再实现最小变更，最后重构。

### 18.1 领域单元测试

- Surface Phase 转移和有界重新观察。
- 确定性终止判定。
- 动作风险与新鲜度判定。
- 循环与预算检测。
- Evidence Index 封存规则和不可变性。

### 18.2 应用层测试

- Bootstrap 到 Session Closure 的完整编排。
- 动作执行事实和效果验证事实分离。
- Timed Observation 不产生 Probe 或 Inspection。
- Manifest 校验与 Evidence Index 构建。
- 下游消费门禁。

### 18.3 数据库集成测试

- 每个 Run 的 Evidence Index 唯一性。
- 条目顺序和引用约束。
- 原子封存与重复请求幂等。
- 并发封存冲突。
- 封存后不可修改。

### 18.4 Worker 集成测试

- 探索期间不发布 Inspection。
- App 正常路径不创建或排空 Probe。
- 未封存 Evidence Index 不进入正式质检。
- Session 关闭顺序正确。

### 18.5 前端测试

- AppStagePanel 新阶段。
- evidence_insufficient 展示。
- session close_failed 展示。
- 旧任务字段为空时兼容。

## 19. 关键验收场景

1. 启动广告存在倒计时：预算内等待并重新观察，进入稳定页面，不产生 Probe 或 Inspection。
2. 启动广告超过等待预算：返回 evidence_insufficient，保留已有证据，不标记为普通 blocked。
3. 连续弹窗：每次操作有前后证据，最终进入 STABLE_PAGE。
4. 动作失败：After Capture 成功也不能把 execution_outcome 改为 succeeded。
5. After Capture 失败：动作执行结果保持不变，verification_outcome 为 unverified。
6. 候选过期：ActionIntent 被拒绝，不向设备发送动作。
7. 页面循环：达到阈值后确定性终止。
8. Frontier 耗尽且最低覆盖达成：返回 exploration_completed。
9. 预算耗尽且最低覆盖未达成：返回 evidence_insufficient。
10. 存在 PENDING 或 FAIL Manifest：禁止封存 Evidence Index。
11. Evidence Index 已封存：禁止增删改，重复封存幂等返回原结果。
12. 正常关闭：先封存 Evidence Index，再 DRAINING，最后 CLOSED。
13. 封存成功但设备关闭失败：证据保持有效，Session 标记 LOST 或 close_failed。
14. 数据库或对象存储失败：不生成 exploration_completed，不启动正式规则质检。
15. 下游门禁：只有 SEALED Evidence Index 可以进入正式规则质检，且质检不能反向触发补采。

## 20. 完成标准

- 所有新增测试先失败、实现后通过。
- 相关现有回归测试通过。
- App 探索路径不再依赖 Inspection Publisher。
- App 正常探索路径不再创建或排空 Probe。
- 启动广告倒计时验收通过。
- 失败动作不再被改写。
- Evidence Index 可验证、可封存、不可变。
- Surface Session 可以确定性关闭。
- 后端、前端、数据库迁移和接口文档保持一致。
- 更新 docs/专项能力与高频回归规则.md。
- 实现完成后进行代码审核、修正并再次验证。
