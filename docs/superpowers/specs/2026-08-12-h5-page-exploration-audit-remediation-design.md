# H5 页面探索审计整改设计

日期：2026-08-12

状态：已批准设计固化稿

基线分支：`dev_1.5`

基线提交：`3e0b470b9776f186926b88e2fd139803b9f53da6`

审计输入：`2026-08-12-h5-app-page-exploration-audit-report.html`

## 1. 目标

仅整改审计报告中的 H5 页面探索问题，以及共享控制面中直接影响 H5 正确性、可恢复性和工作台回读的问题。整改后，H5 V2 必须从“受控单链页面采集器”升级为以下闭环：

1. 同源导航候选合同完整，缺少目标或越界时可审计拒绝。
2. 任务级 Frontier 不因单后继 Work 丢失父级或 sibling 分支。
3. quick、full、custom 的预算、深度、抽样和旅程目标由任务快照决定。
4. 运行中取消可以停止新动作、取消数据库工作项并阻止完成态覆盖。
5. 页面判定、动作、Frontier、Assessment 和证据库存能由工作台直接回读。
6. 证据交接按预期动作逐项核对，部分证据不足不抹掉合格库存。
7. H5-only 部署也必须通过 Completion Gate 启动配置校验。
8. 浏览器崩溃具有有界恢复语义，不重放结果未知的动作。
9. 交接前重新校验对象字节、归属和 checksum。
10. 代码整改完成后逐条对照原审计报告复审，并区分代码闭环与真实环境验收。

## 2. 范围

### 2.1 包含

- H5 `goto`、`navigate`、`destination_url` 合同统一。
- H5 任务级 Frontier、分支恢复、去重、耗尽和恢复后续跑。
- H5 Runtime 对任务预算、页面抽样和旅程目标的消费。
- H5 运行中取消和终态竞态保护。
- H5 V2 Work、Action、Capture、Assessment、Evidence Inventory 的读模型。
- H5 证据交接库存、对象完整性校验和部分证据终态。
- H5 PageTypeAssessment 持久化。
- H5 浏览器崩溃恢复。
- H5-only Completion Gate 配置门禁。
- H5 无消费者 Explorer/Probe 唤醒处理。
- H5 exploration-only 报告状态、平台识别和 Inspection 元数据对齐。
- H5 相关单元、Repository、迁移、Worker、前端和隔离环境验收。
- `docs/专项能力与高频回归规则.md` 与最终专项复审报告更新。

### 2.2 不包含

- App 包名边界、视觉观察持久化、最终观察和 App 封存问题。
- App exploration-only、下载 URL 导入或 App V1/V2 入口统一。
- 自动账号密码登录、验证码、短信、MFA 或扫码授权。
- 删除 H5 V1、历史表或历史查询兼容层。
- 将单体拆分为微服务，或全面重构 App/H5 共享控制面。
- 在缺少真实依赖时把代码级验证表述为生产放量验收。

## 3. 已确认的问题

### 3.1 发布阻断

1. Page Detection 生成 `goto`，交互枚举只支持 `navigate`；适配后的导航候选缺少 `destination_url`。
2. 每个父 Work 只提交一个后继，动作后仅保留新页面候选，父页面未执行 sibling 不再是持久化事实。
3. Runtime 丢弃 `settings`，固定使用 28 actions、depth 8、600 秒预算和 900 秒 deadline。
4. H5 Runtime 进入循环后不再读取 Redis 取消信号，也不调用 PostgreSQL `cancel_nonterminal`。
5. H5 V2 写新 Work/Capture，但 Timeline、报告和探索摘要仍依赖旧页面节点、旧动作和近乎空的 WorkingMemory。
6. 证据查询以完整 before/after 内连接为入口，缺失一侧的预期动作会从库存中消失；单条证据不足会整批抛错。
7. Completion Gate 启动校验只检查 App QC，遗漏 H5 V2 on 且 App off 的部署组合。

### 3.2 重要缺口

1. H5 PageTypeAssessment 构造后未形成独立长期事实。
2. ACTION 或 AFTER_CAPTURE 阶段浏览器崩溃没有 reopen、rebaseline 和有界恢复。
3. 证据交接只按 artifact type 判断，没有重新读取对象字节并校验 checksum。
4. H5 Explorer/Probe Outbox 会写入无人消费的 Redis Stream。
5. exploration-only 任务完成后仍出现报告状态矛盾。
6. H5/App 阶段识别没有统一使用正式 `target_platform`。
7. Inspection 发布写入嵌套 JSON，Timeline 查询读取顶层字段。

## 4. 方案比较

### 4.1 方案 A：逐项补丁

直接修正导航字符串、增加少量取消检查并给读模型加兜底。

优点：改动量小。

缺点：单后继、空 WorkingMemory、证据库存消失和恢复事实缺失仍然存在；问题会以新的边界条件再次出现。

结论：不采用。

### 4.2 方案 B：H5 专属闭环升级，最小修正共享组件

复用现有 V2 Work、Capture、Action Execution、Inspection 和 Completion Gate；增加 H5 任务级 Frontier、Assessment 库存、Evidence Inventory、取消与恢复编排，并只修改直接影响 H5 的共享门禁和读模型。

优点：保持现有领域合同和单体分层，H5 事实闭环完整，不把 App 专属整改带入本次范围。

缺点：需要数据库迁移，并增加 H5 专属领域对象与 Repository。

结论：采用。

### 4.3 方案 C：重构全部 V2 控制面

统一 App/H5 Frontier、取消、封存和读模型。

优点：长期抽象最统一。

缺点：明显超出仅处理 H5 的授权范围，改动面和回归风险过大。

结论：不采用。

## 5. 目标架构

```text
V2 H5 Task Submission
  -> Locked Task Runtime Settings
  -> H5 Runtime / Surface Session
  -> before + NAVIGATE + after
  -> Page Detection
  -> Persisted PageTypeAssessment
  -> Local Frontier + H5 Task Frontier Snapshot
  -> PostgreSQL Exploration Work
  -> Cancel Gate
  -> Explorer Decision or Deterministic Restoration
  -> Cancel Gate
  -> before + Action + after
  -> Transition Verification
  -> Page Detection + Assessment
  -> Merge Task Frontier
  -> Atomic Successor Work
  -> Frontier Exhaustion / Bounded Terminal
  -> Session Close
  -> Expected Action Evidence Inventory
  -> Object Bytes / Checksum Verification
  -> exploration_only: Result Projection
  -> full_inspection: Passing Captures -> Inspection -> Rule Judge
  -> Completion Gate
  -> Workstation V2 Projection / Sealed Report
```

## 6. 任务运行策略

### 6.1 唯一输入

新增类型化 H5 任务运行设置，由以下已有快照构建：

- `record.task_config`
- `record.loop_guard`
- `record.page_sampling_policy`
- `settings.agent_runtime`

优先复用现有 `resolve_task_runtime_settings()` 的解析和硬上限逻辑，不在 H5 Runtime 建立第二套默认值。

### 6.2 映射规则

- `max_actions`：取任务解析后的 `max_steps`。
- `max_depth`：取 `page_sampling_policy.depth_limit`、`loop_guard.max_depth`、`task_config.max_depth` 的最小有效值。
- `remaining_duration_ms`：取任务解析后的 `max_duration_seconds * 1000`。
- `deadline`：由 Run 启动时间加任务时长构造，不再使用固定 900 秒。
- quick：沿用提交侧 40 步、深度 2、15 分钟、最多 3 页。
- full：沿用全局运行时硬上限、深度 4、60 分钟，不采用 quick 页面上限。
- custom：沿用提交侧深度 3 和 60 分钟，并将 `journey_steps` 纳入探索目标与优先级。
- 页面抽样的 include、exclude、rule target 和 max pages 进入任务级 Frontier 终态判断。

解析失败必须在创建浏览器 Session 前 fail closed。

## 7. 导航合同

1. H5 Page Detection 只生成 `navigate`，禁止继续生产 `goto`。
2. `target` 保留候选的语义目标；真实 URL 写入受控 metadata `destination_url`。
3. H5 Observation Adapter 必须完整透传 `destination_url`。
4. Explorer eligibility 必须验证：
   - URL 存在；
   - HTTP(S)；
   - 与任务入口同源；
   - 未超过允许 route prefix；
   - 导航深度未超过预算。
5. 缺 URL、跨域、越界和非法协议分别记录稳定拒绝码，不能静默丢弃后让任务误判完成。

## 8. 任务级 Frontier

### 8.1 领域对象

新增 H5 专属 `H5TaskFrontierSnapshot` 和 `H5TaskFrontierEntry`。每个条目至少包含：

- 稳定 entry ID 和 candidate ID；
- CandidateAction 的完整安全载荷；
- 来源 capture ID、state version、location 和 navigation depth；
- 父状态和分支谱系；
- pending、selected、executed、blocked、failed 状态；
- 恢复尝试次数和最后失败原因；
- 创建、选择和终结时间。

任务快照还包含：

- 当前活动状态；
- visited state IDs；
- executed candidate IDs；
- action fingerprints；
- 已覆盖页面数和页面类型；
- pending、blocked、failed 计数；
- 抽样策略摘要；
- schema version 和稳定内容哈希。

### 8.2 持久化位置

任务级 Frontier 作为 H5 Work payload 中的平台专属、版本化快照随每个后继 Work 持久化。`qc_v2_work_item` 仍是唯一 Explorer Work 事实源，不新增第二套工作队列。

最新非终态 Work 携带当前权威快照；已完成 Work 保留历史快照，形成可审计演进链。

### 8.3 合并规则

动作完成后：

1. 把已选候选标记为 executed，保留父页其他 pending sibling。
2. 对新页面候选按 state version + operation + semantic target + expected page type 去重。
3. 超出深度、风险或抽样范围的候选记录 blocked 原因，不直接删除。
4. 当前分支有安全候选时继续有限深度优先。
5. 当前分支耗尽后，从最近可恢复的 pending sibling 开始广度补齐。
6. 只有当前候选和任务级 pending 都耗尽，且不存在 requires_auth、失败恢复或未终态后台 Work 时，才能形成 completed。

### 8.4 分支恢复

- 来源状态是当前状态的祖先且浏览器历史可用时，优先生成确定性 `BACK`。
- 其他同源可恢复状态使用受控 `NAVIGATE(destination_url)`。
- 恢复动作不交由模型自由改写。
- 恢复后重新采集、识别并校验状态指纹；匹配后重新暴露来源 sibling。
- 单个条目的恢复次数有上限；耗尽后记录 failed/blocked，并继续处理其他可达条目。
- 所有 pending 都不可恢复时进入 `incomplete/needs_review`，不伪装 completed。

## 9. 取消与终态竞争

### 9.1 取消端口

为 H5 Runtime 注入基于现有 Redis Agent Run Queue 的取消读取端口。检查点包括：

- Session 创建前；
- 每次 claim 前；
- Explorer 决策后；
- 页面动作前；
- before/action/after 证据边界完成后；
- 后继提交前；
- Probe claim 和执行前；
- 证据交接与 Completion Gate 前。

### 9.2 原子边界

一旦动作开始，优先完成当前动作的 after Capture 审计边界，再响应取消；取消不能让已经发生的页面动作失去证据。动作开始前命中取消则不得执行动作。

取消处理必须：

1. 调用 PostgreSQL `cancel_nonterminal` 取消当前任务的 Exploration/Probe 非终态 Work。
2. 关闭或标记丢失 Surface Session。
3. 返回 `TerminationReason.CANCELED`。
4. 通过任务状态、finalization epoch、父 Work 活租约和 row version 阻止后继 Work 或 COMPLETED 覆盖取消态。

## 10. 浏览器崩溃恢复

### 10.1 阶段语义

- BEFORE_CAPTURE 失败：动作尚未开始，允许恢复 Session 后重试原 Work。
- ACTION 失败且执行结果未知：禁止自动重放原动作；该动作进入 evidence insufficient。
- AFTER_CAPTURE 失败：保留已发生的 Action Execution，禁止把动作写成普通成功，也禁止盲目重放。

### 10.2 恢复流程

1. 将旧 Session 标记 lost，并关闭残余句柄。
2. 基于最后可信同源 location 建立新 Session。
3. 执行受控导航并生成 rebaseline before/after Capture。
4. 重新进行 Page Detection 和 PageTypeAssessment。
5. 使用现有 Restoration/State Equivalence 事实记录原状态、恢复状态和验证结果。
6. 等价验证通过后，将失败 Work 标记 superseded，并发布携带原任务级 Frontier 的恢复 Work。
7. 验证失败或超过恢复上限时进入 `incomplete/needs_review`。

恢复不重置已消费动作、时长和页面预算。

## 11. PageTypeAssessment 持久化

新增 `qc_h5_page_type_assessment` 表，至少保存：

- assessment ID；
- task ID、agent run ID、session ID、capture ID；
- detection status、page type、page state；
- covered、requires_auth、evidence_insufficient 等状态；
- reason codes 和安全摘要；
- schema version、payload hash；
- created/updated timestamps。

以 capture ID + schema version 建立幂等约束。H5 Workflow 在发布首 Work 或后继 Work 前必须先成功写入 Assessment；写入失败不得继续扩大探索事实。

## 12. 证据交接库存

### 12.1 预期集合

以当前 task/run 下的预期 `qc_action_execution` 为基准，左连接 before Capture、after Capture、Page Detection 和 Evidence Artifact。禁止继续以“已经完整的 Capture 对”为查询入口。

首次任务入口 NAVIGATE 同样属于预期 Action Execution，必须进入库存。

### 12.2 库存表

新增 `qc_h5_evidence_inventory_item` 表，至少保存：

- inventory item ID；
- task ID、agent run ID、session ID、action execution ID；
- before/after capture ID；
- page type、page state；
- `pass`、`insufficient`、`failed`；
- reason codes；
- required/found artifact 摘要；
- ownership、bytes、size、checksum 校验摘要；
- published inspection count；
- schema version、payload hash 和时间戳。

每个 Action Execution 只有一个当前版本库存项，重复交接必须幂等。

### 12.3 结果分类

- `pass`：前后 Capture、动作身份、URL、recognized page type、Screenshot、DOM、对象字节、size、checksum 和 ownership 全部一致。
- `insufficient`：缺 Capture、缺制品、页面未识别或证据内容不完整。
- `failed`：对象损坏、checksum 不匹配、跨任务归属或不可解释的身份冲突。

### 12.4 收口规则

- 合格项始终保留，并可在 full inspection 下发布快照质检 Work。
- 任一 insufficient：Run 进入 `incomplete/needs_review`，不生成 sealed Report。
- 任一 failed：Run 明确标记证据完整性失败，同时保留所有逐项库存和已完成的合格事实。
- Handoff 基础设施整体不可用时才使用系统级异常；不能把业务证据不足包装成通用 UNKNOWN ERROR。

## 13. H5 V2 读模型

### 13.1 唯一事实源

H5 V2 Timeline 和探索摘要直接投影：

- `qc_v2_work_item`
- `qc_action_execution`
- `qc_capture` / `qc_page_capture`
- `qc_h5_page_type_assessment`
- `qc_h5_evidence_inventory_item`
- Completion Gate 和 Inspection Work 事实

WorkingMemory 只保留短期兼容用途，不再作为 H5 V2 页面、动作、Assessment 或覆盖率的正式来源。

### 13.2 工作台输出

至少提供：

- 当前和历史页面状态；
- 动作前后 Capture 与动作结果；
- pending、blocked、failed、executed Frontier 计数；
- 覆盖页面与页面类型；
- Assessment 状态；
- Evidence Inventory 状态；
- 终态原因和恢复历史。

### 13.3 exploration-only

execution mode 为 exploration-only 时：

- 报告状态为 `not_applicable`；
- 保留“查看探索结果”和证据下载能力；
- 不提供“查看正式报告”的误导动作；
- 不发布 Rule Judge、不写正式规则结果、不执行 Completion Gate。

## 14. 共享控制面的 H5 最小修正

### 14.1 Completion Gate 配置

启动校验条件与 Finalization Service 构建条件保持一致：

```text
app_qc_enabled OR h5_v2_mode == on
```

上述任一条件成立时，必须要求 PostgreSQL State Store、数据库 URL 和 Finalization Service。

### 14.2 Explorer/Probe 唤醒

当前 H5 Runtime 在同一进程内按 PostgreSQL 直接 claim，不依赖 Redis Explorer/Probe Stream。H5 Web Work 不再写入无人消费的 Explorer/Probe Outbox 事件；Inspection Outbox 和现有消费链保持不变。

该调整必须通过显式配置或 H5/Web work 类型判断实现，不改变 App 本次范围外的执行语义。

### 14.3 平台识别

工作台上下文查询必须读取 `qc_task.target_platform`。H5/App 阶段判断以正式字段为准，产品码、URL 是否为空仅允许用于明确标记的历史兼容数据。

### 14.4 Inspection 元数据

H5 发布和 Timeline 查询统一正式字段或统一 JSON 路径。优先写入 `qc_page_capture.inspection_status` 等正式列；兼容读取嵌套 `metadata.inspection.status`，但禁止发布侧和读取侧继续使用不同 schema。

## 15. 数据迁移

新增一条 Alembic migration，创建：

1. `qc_h5_page_type_assessment`
2. `qc_h5_evidence_inventory_item`

迁移要求：

- 外键绑定 task、run、session、capture/action 的真实归属；
- 状态、JSON 形状、row version 和 hash 有数据库约束；
- 常用 task/run/capture/action 查询有索引；
- downgrade 在存在 H5 新事实时拒绝破坏性降级，或显式要求先清理数据；
- 不回填伪造的历史 Assessment 或 Inventory；历史 H5 V2 数据读侧使用“未评估”语义。

## 16. 终态定义

### 16.1 completed

同时满足：

- 任务级 Frontier 无 runnable pending；
- 无非终态 Exploration/Probe Work；
- 无 requires_auth、恢复失败或未解释 blocked；
- 页面抽样与预算按策略正常结束；
- Evidence Inventory 全部 pass；
- full inspection 的 Inspection/Rule Execution 完成且 Completion Gate 通过。

### 16.2 incomplete / needs_review

包括：

- budget exhausted；
- requires_auth；
- no progress 或分支不可恢复；
- 部分证据 insufficient；
- 浏览器恢复耗尽；
- 活动 Session 规则无法在快照模式执行。

### 16.3 canceled

用户取消已被 Runtime 确认，数据库非终态 Work 已取消，Session 已关闭或标记 lost。该状态不能被后续 completed 覆盖。

### 16.4 failed

仅用于数据库一致性、证据完整性、不可恢复基础设施或违反安全边界的错误。业务证据不足不得映射为通用 failed。

## 17. 测试设计

### 17.1 导航合同

- Page Detection 只生成 `navigate`。
- 同源 `destination_url` 可执行。
- 缺 URL、跨域、越界 route 和非法协议分别拒绝。
- 点击候选行为保持不变。

### 17.2 任务级 Frontier

- 根页三个 sibling 全部可恢复并耗尽。
- 二级页有子分支时先有限下钻，再回父级补齐。
- BACK 恢复和同源 NAVIGATE 恢复。
- 相同状态/candidate 跨 Capture 去重。
- Worker/Repository round-trip 后 pending sibling 不丢失。
- 恢复失败有界，不无限循环。
- Frontier 未耗尽时模型终态不能把任务标记 completed。

### 17.3 预算和抽样

- quick 的 40 步、深度 2、15 分钟、3 页上限真实生效。
- full 的深度 4、60 分钟和运行时硬上限真实生效。
- custom 深度 3，journey steps 进入目标和优先级。
- 后继 Work 与恢复 Work 保留剩余动作、时长和页面计数。

### 17.4 取消

- Session 创建前取消。
- claim 前、模型决策后、动作前、动作后和 Probe 前取消。
- 取消后 PostgreSQL Work 变为 cancelled。
- 取消与 commit successor、Completion Gate 竞争时取消态不被覆盖。

### 17.5 Assessment 与证据库存

- covered、requires_auth、evidence_insufficient 的数据库 round-trip。
- expected action 缺 before、缺 after、缺 Screenshot、缺 DOM。
- 多项中部分 pass、部分 insufficient 时合格项保留。
- 对象不存在、size 不符、checksum 不符和 ownership 不符。
- 重复 Handoff 幂等，不重复发布 Inspection Work。

### 17.6 崩溃恢复

- BEFORE_CAPTURE 崩溃可安全重试。
- ACTION 结果未知时不重放。
- AFTER_CAPTURE 崩溃保留动作事实并标记证据不足。
- rebaseline 状态等价通过后继续 Frontier。
- 状态不等价和恢复次数耗尽进入 incomplete。

### 17.7 控制面与工作台

- H5 on/App off + noop/postgres/缺 Service 配置矩阵。
- H5 Web Work 不产生无人消费 Explorer/Probe Outbox。
- Inspection Outbox 不受影响。
- 空 WorkingMemory 下 H5 V2 Timeline、动作和覆盖仍可回读。
- exploration-only 显示报告不适用和探索结果入口。
- `target_platform=h5` 不被误判为 App。
- Inspection 元数据 PostgreSQL round-trip 一致。

### 17.8 验证命令类别

- H5 领域、应用和 Worker 定向 pytest。
- 迁移与 Repository pytest。
- H5 相关回归 pytest。
- Workstation Vitest 与生产构建。
- 变更 Python 文件 Ruff。
- `git diff --check`。
- 在具备条件时执行 PostgreSQL、Redis、对象存储、Playwright 和真实 H5 URL 的隔离 E2E。

## 18. 真实环境验收门禁

生产放量前必须验证：

1. Alembic upgrade 到新 head，索引、约束和 Repository round-trip 正常。
2. H5 off/shadow/on 模式和错误码正确。
3. quick/full/custom 的真实动作数、深度、抽样和 deadline 正确。
4. 根页和二级页 sibling 可恢复、去重并耗尽。
5. 同源 navigate 可执行，越界和缺 URL 可审计拒绝。
6. 运行中取消不再执行新动作，也不被完成态覆盖。
7. 浏览器 crash、timeout 和 lease expiry 可恢复或明确 incomplete。
8. MinIO bytes、checksum、ownership 与数据库一致。
9. Timeline、探索摘要、证据库存和报告与 PostgreSQL 事实一致。
10. full inspection 只有全部门禁通过时生成 sealed Report。

真实依赖不可用时，最终结论最多为“代码闭环已验证，生产环境门禁未验证”。

## 19. 最终复审

实现完成后新增 `docs/reviews/2026-08-12-h5-page-exploration-audit-remediation-review.md`，逐条记录：

- 原审计问题和优先级；
- 修改后的事实源与代码位置；
- 对应测试和命令结果；
- 完成、部分完成或未完成；
- 未验证的真实依赖；
- GO、CONDITIONAL GO 或 NO-GO 结论。

复审必须覆盖原报告 H5 4.2、4.3、共享控制面中直接影响 H5 的条目，以及生产放量最低验收清单中的 H5 项。

## 20. 完成标准

- 本设计范围内的 H5 P1/P2 和直接影响 H5 的共享问题均有代码或明确的部署验收结论。
- 所有新增事实具有稳定身份、归属、版本、hash、幂等和长期查询入口。
- 不依赖空 WorkingMemory 生成 H5 V2 探索完成度。
- 取消、崩溃、部分证据不足和完整性失败不会互相覆盖真实终态。
- 新增和相关回归测试通过，变更文件静态检查与前端构建通过。
- `docs/专项能力与高频回归规则.md` 已更新。
- 最终专项复审报告已对照附件逐条给出证据和结论。
- 未完成真实环境 E2E 时不宣称生产已闭环或可放量。
