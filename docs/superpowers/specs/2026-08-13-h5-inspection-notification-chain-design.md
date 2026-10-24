# H5 证据到规则质检可靠通知链设计

日期：2026-08-13

状态：已确认设计固化稿

基线分支：`dev_1.5`

基线提交：`770be7d8a6dbc8914f2fae5edb1637bd713aa344`

分析输入：`2026-08-13-page-exploration-evidence-rule-inspection-flow.html`

适用平台：仅 H5

## 1. 执行结论

附件提出的主方向正确：PostgreSQL 是业务事实源，Transactional Outbox 负责把已提交事实转换为可靠唤醒，Redis Stream 只负责短期加速，Consumer 必须回 PostgreSQL 领取工作，Rule Judge 只消费后端组装并验真的 Observation Packet。

但当前 H5 实现尚未形成“证据提交后必然创建规则工作”的完整通知闭环：

1. `alioth.v2.evidence-bundle.ready` 已能进入 inspection Stream，但现有 `InspectionWakeupHandler` 只尝试排空已经存在的 Inspection Work Item，不根据证据事件创建 H5 工作项。
2. H5 工作项仍由 `worker.py` 在 H5 Runtime 结束后同步调用 `H5EvidenceHandoffService.prepare()` 创建。若进程在证据已提交、同步发布器调用前崩溃，证据事件可以被 Inbox 确认，而对应 Inspection Work Item 从未形成。
3. 现有 Inspection Handler 在找不到运行中的 Agent Run 时直接确认消息。探索 Run 已结束但质检仍待执行时，消息确认与业务待办生命周期可能脱节。
4. Work Item 进入 `retry_wait` 后只更新 `next_attempt_at`，没有必然产生一条新的版本化唤醒消息。旧消息若已被 Inbox 标记完成，重复投递仍会被去重，无法代表下一轮合法重试。

本设计采用 **H5 专属持久化质检发布请求 + 共享 Inspection Work Item 执行链**：

```text
H5 after_action Capture
  -> H5 证据交接门禁
  -> H5 Inspection Publication
  -> h5-inspection-publication.available Outbox
  -> H5 Publication Consumer
  -> 锁定规则匹配与稳定分批
  -> 共享 Inspection Work Item + Rule Members
  -> inspection-work.available Outbox
  -> 共享 Execution Consumer
  -> Attempt -> Observation Packet -> Rule Judge
  -> Result Writer -> Completion Gate
```

方案只修复 H5 通知链，不修改 App sealed Index、App Publisher、App Worker、App 数据结构或 App 行为；从共享 Inspection Work Item 开始继续复用当前控制面。

## 2. 目标与成功标准

### 2.1 目标

1. 合格 H5 Capture 在当前任务周期内最终必然形成可审计的质检发布事实。
2. H5 证据门禁与规则工作创建解耦，任一进程崩溃后均可仅凭 PostgreSQL 恢复。
3. 重复、乱序和迟到通知不会创建重复工作项或重复正式规则结果。
4. Redis 清空、重启或暂时不可用只影响处理延迟，不造成 H5 待办丢失。
5. H5 质检执行不依赖探索 Run 始终处于 `running`。
6. Completion Gate 能区分“证据就绪、通知已送达、工作已领取、质检已完成”，不会把 Redis ACK 当作业务完成。
7. 工作台可以沿稳定身份链定位 H5 通知停滞点。

### 2.2 成功标准

- 每个符合发布条件的 H5 `after_action Capture` 都存在当前 `finalization_epoch` 的 Publication。
- 每个存在可执行 snapshot 批次的 Publication 都按 `expected_batch_count` 关联唯一 Inspection Work Item；即使同一 Publication 因排除 active-session 规则而处于 `needs_review`，已发布的 snapshot 批次也不能丢失。
- 同一锁定规则批次最多产生一份正式 `qc_rule_execution`。
- Publication、Outbox、Inbox、Work Item、Attempt 和 Rule Execution 可完整对账。
- H5 通知未收口时 Completion Gate 必须阻断最终完成。
- App 路径的创建、唤醒、消费、收口和查询行为保持不变。

## 3. 范围

### 3.1 包含

- H5 `full_inspection` 模式下，从证据门禁通过到共享 Inspection Work Item 创建的可靠通知。
- H5 专属 Publication 领域对象、Repository、租约、状态机、Outbox 事件和 Consumer。
- H5 Publication Sweeper，以及直接影响 H5 的 Work Item 到期重唤醒。
- H5 专属持久化逻辑 `rule_judge` Run。
- H5 Completion Gate 事实、任务详情投影、时间线和运维指标。
- 任务提交时锁定的 H5 通知模式 `h5_inspection_notification_mode = off / shadow / enforced`。
- H5-only 单元、PostgreSQL、Redis、对象存储和端到端验收。
- `off -> shadow -> enforced` 灰度与历史运行中 H5 任务迁移。

### 3.2 不包含

- App sealed Evidence Index、App Capture Publisher、App Worker 或 App 数据迁移。
- 面向用户的站内信、邮件、短信、Webhook 或第三方即时通信通知。
- 新增前端菜单或路由。
- 把分层单体拆成微服务，或更换 Redis Stream。
- 让 Rule Judge 直接订阅消息、扫描数据库、读取对象存储或操作浏览器。
- H5 证据补采运行时；本版本仍按既定策略把不可满足的活动会话规则转为证据不足或人工复核。
- 自动账号密码登录、验证码、短信、MFA 或扫码授权。

### 3.3 依赖

本设计依赖 H5 页面探索整改方案中已经确认但在当前基线尚未完全落地的以下事实：

- 可长期读取的 H5 PageTypeAssessment；
- 以预期动作为基准的 H5 Evidence Inventory；
- before/after Artifact 字节、归属和 checksum 验证；
- 任务锁定规则集快照与 `finalization_epoch`。

若实施时 PageTypeAssessment 尚未有独立表，必须先完成其迁移与 Repository；禁止用 Worker 内存中的页面类型代替 Publication 锁定输入。

## 4. 当前实现与可靠性断点

### 4.1 已有可复用能力

| 能力 | 当前代码 | 结论 |
| --- | --- | --- |
| H5 证据交接门禁 | `application/h5_evidence_handoff.py` | 可复用验证规则，但需要把“直接创建工作项”改成“持久化 Publication” |
| 页面类型到规则批次 | `application/app_inspection_publisher.py`、`application/inspection_candidate_publish.py` | 算法可抽为平台中立编译器；H5 不应继续依赖 App 命名组合根 |
| Work Item 与成员 | `application/inspection_work_items.py`、`infrastructure/db/inspection_work_item_repository.py` | 继续作为共享质检事实源 |
| Transactional Outbox | `application/outbox.py`、`infrastructure/db/outbox_repository.py` | 继续复用，新增 H5 Publication 消息类型和路由 |
| Redis Stream | `infrastructure/queue/redis_stream.py` | 继续只传引用；为 Publication 增加 H5 专属 Stream |
| PostgreSQL Inbox | `application/event_inbox.py`、`infrastructure/db/event_inbox_repository.py` | 继续核验 Outbox 身份和防重复 |
| Inspection 消费 | `agents/runtime/inspection_wakeup.py`、`inspection_work_item_consumer.py` | 继续复用执行链；H5 必须绑定持久化逻辑消费 Run |
| Judge 与写回 | `agents/rule_judge`、`application/observation_packet.py`、`inspection_result_writer.py` | 不改变 Judge 权限与结果写回边界 |
| Completion Gate | `application/task_finalization.py`、`infrastructure/db/task_finalization_repository.py` | 增加 H5 Publication 事实，不能只统计已创建 Work Item |

### 4.2 断点一：证据事件不负责补建 H5 工作项

当前 `EvidenceBundleReady` 与 `CaptureCommitted` 虽然路由到 inspection Stream，但 Handler 只查找活动 Run 并执行 `drain_available()`。如果数据库中尚无 Inspection Work Item，消息会被正常确认。H5 工作项创建仍依赖 `worker.py` 中 Runtime 结束后的同步收口代码。

因此现状只能证明“证据事件可以唤醒质检消费者”，不能证明“证据事件最终必然产生 H5 质检工作”。

### 4.3 断点二：消费身份依附探索 Run

当前 `find_active_consumer_run()` 只查任务内任意 `status='running'` 的 Agent Run。探索 Run 已结束时，Handler 会把通知视为可以安全确认；但 H5 质检待办可能仍处于 `pending`、`retry_wait` 或尚未创建。

### 4.4 断点三：业务重试没有新消息身份

现有 Work Item 可以从 `running` 进入 `retry_wait`，但状态迁移本身不写新 Outbox。旧 Stream 消息完成后，对应 Inbox 已是 `completed`。因此下一轮业务重试必须产生新 `message_id + aggregate_version + idempotency_key`，不能依赖旧消息重投。

### 4.5 断点四：Completion Gate 看不到“应发布但尚未发布”

当前 Gate 会统计未终态 Work Item、Attempt 和 Outbox，但如果 H5 合格 Capture 尚未创建任何 Work Item 与 Publication，Gate 缺少一个明确的发布待办事实。仅靠候选规则数量或 Capture metadata 不能稳定表达该中间状态。

## 5. 方案比较

### 5.1 方案 A：在现有 Handler 中直接补丁

收到 `EvidenceBundleReady` 后直接调用 H5 Publisher，并增加 `retry_wait` 定时扫描。

优点：改动量小。

缺点：证据事件到达时不一定已具备完整 H5 Assessment、Evidence Inventory 和任务阶段门禁；Handler 会同时承担事件解释、平台判断、规则匹配、批次创建和重试恢复，边界继续恶化。

结论：不采用。

### 5.2 方案 B：H5 持久化 Publication，复用共享执行链

H5 证据门禁通过后只创建 Publication；独立确定性 Consumer 再把 Publication 编译为共享 Work Item。Publication 和 Work Item 分别具备 PostgreSQL 身份、租约、Outbox 和恢复语义。

优点：可靠性边界清晰；可从任意事务边界恢复；只新增 H5 领域事实；最大程度复用当前 Inspection 控制面。

缺点：需要新增迁移、Consumer、Sweeper、投影与灰度对账。

结论：采用。

### 5.3 方案 C：在 Manifest 事务内直接创建 Work Item

证据 Manifest 与规则工作项同事务创建。

优点：单次提交原子性最强。

缺点：单个 Manifest 提交时不一定已形成 H5 after_action Assessment、完整 before/after 库存和稳定规则发布条件；会把 Evidence、Rule 和 Inspection 聚合强耦合，也不适合一个 Capture 产生多个稳定批次。

结论：不采用。

## 6. 目标领域边界

| 模块 | 负责 | 明确禁止 |
| --- | --- | --- |
| H5 Evidence Gate | 校验 H5 Capture、Assessment、Artifact、动作谱系与执行模式 | 匹配规则、创建 Work Item、调用 Judge |
| H5 Inspection Publication | 保存“该 Capture 应生成哪些规则工作的待办身份”及其发布生命周期 | 保存截图、DOM、规则正文或执行 Judge |
| H5 Publication Consumer | 领取 Publication、校验锁定输入、匹配规则、稳定分批、原子创建工作项 | 信任 Redis 载荷作为业务事实、访问浏览器 |
| Shared Inspection Work Item | 保存规则批次、成员、租约、Attempt 与恢复状态 | 持有大证据载荷 |
| Shared Execution Consumer | 领取工作项、创建 Attempt、构造 Handoff、协调 Judge 和写回 | 修改规则契约、自由扫描证据 |
| Rule Judge | 基于可信 Observation Packet 输出结构化结论 | 访问数据库、对象存储、Redis 或浏览器 |
| Result Writer | 幂等写正式结果并同步成员状态 | 再次判定规则 |
| Completion Gate | 依据 PostgreSQL 事实判断能否完成 | 依据 Redis backlog、日志或 Worker 内存判断完成 |

H5 与 App 的边界如下：

```text
H5 after_action Evidence Gate
  -> H5 Publication                   # H5 专属
  -> Shared Inspection Work Item      # 开始复用
  -> Shared Attempt / Judge / Result

App sealed Evidence Index
  -> Existing App Publisher           # 保持原状
  -> Shared Inspection Work Item
  -> Shared Attempt / Judge / Result
```

## 7. H5 Publication 数据模型

### 7.1 主表

新增 `qc_h5_inspection_publication`：

| 字段 | 类型/约束 | 说明 |
| --- | --- | --- |
| `id` | uuid PK | `publication_id` |
| `task_id` | uuid FK, not null | 必须指向 `target_platform='h5'` 的任务 |
| `source_agent_run_id` | uuid FK, not null | 产生 Capture 的 H5 探索 Run |
| `page_capture_id` | uuid FK, not null | 合格 `after_action Capture` |
| `page_type_assessment_id` | varchar FK, not null | 独立持久化的页面判定事实；沿用 `page-type-assessment:{hash}` 身份 |
| `assessment_hash` | varchar, not null | 锁定页面类型、状态和判定输入摘要 |
| `ruleset_id` | uuid FK, not null | 任务锁定规则集 |
| `ruleset_version` | integer, not null | 锁定版本 |
| `ruleset_snapshot_hash` | varchar, nullable | 与当前 Work Item 契约一致 |
| `finalization_epoch` | integer, not null | 隔离取消、重跑和迟到写入 |
| `input_hash` | varchar, not null | Publication 业务幂等摘要 |
| `status` | varchar, not null | Publication 状态机 |
| `lease_owner` | varchar, nullable | Worker 实例身份，不作为业务主体 |
| `lease_token` | uuid, nullable | 防陈旧 Worker 写回 |
| `lease_expires_at` | timestamptz, nullable | 短租约到期时间 |
| `attempt_count` | integer, not null | 发布尝试次数 |
| `max_attempts` | integer, not null | 有界重试上限 |
| `next_attempt_at` | timestamptz, not null | 下一次允许领取时间 |
| `row_version` | integer, not null | 状态与消息聚合版本 |
| `candidate_rule_count` | integer, nullable | 锁定计划中的候选规则数 |
| `expected_batch_count` | integer, nullable | 稳定分批后的预期批次数 |
| `published_work_item_count` | integer, not null | 已关联批次数 |
| `excluded_active_session_rule_count` | integer, not null | Session 关闭后不能执行、需复核的规则数 |
| `consumer_agent_run_id` | uuid FK, nullable | H5 持久化逻辑质检 Run |
| `failure_code` | varchar, nullable | 稳定错误码 |
| `failure_message` | text, nullable | 有界且脱敏的错误摘要 |
| `created_at/updated_at` | timestamptz | 审计时间 |
| `finished_at` | timestamptz, nullable | 终态时间 |

唯一约束：

```text
UNIQUE (task_id, page_capture_id, finalization_epoch)
UNIQUE (input_hash)
```

同一 `(task_id, page_capture_id, finalization_epoch)` 再次提交时：

- `input_hash` 相同：返回已有 Publication；
- `input_hash` 不同：报告身份漂移并转人工复核，禁止覆盖旧输入。

`input_hash` 至少覆盖：

```text
protocol_version
task_id
source_agent_run_id
page_capture_id
page_type_assessment_id
assessment_hash
ruleset_id
ruleset_version
ruleset_snapshot_hash
finalization_epoch
execution_mode
h5_inspection_notification_mode
```

### 7.2 Publication 与 Work Item 关联

新增 `qc_h5_inspection_publication_work_item`：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `publication_id` | FK, not null | H5 Publication |
| `work_item_id` | FK, not null | 共享 Inspection Work Item |
| `batch_key` | varchar, not null | 稳定批次身份 |
| `created_at` | timestamptz, not null | 关联时间 |

约束：

```text
PRIMARY KEY (publication_id, work_item_id)
UNIQUE (publication_id, batch_key)
```

该关联表只保存 H5 来源，不向 App 工作项增加 H5 字段，也不改变 App 查询语义。

### 7.3 状态机

```text
pending
  -> leased
       -> published
       -> not_applicable
       -> needs_review
       -> retry_wait -> leased
       -> failed
  -> cancelled
  -> superseded
```

状态定义：

| 状态 | 是否终态 | 语义 |
| --- | --- | --- |
| `pending` | 否 | Publication 已落库，等待领取 |
| `leased` | 否 | Consumer 持有有效短租约，正在编译与发布 |
| `retry_wait` | 否 | 可恢复失败，等待新的到期唤醒 |
| `published` | 是 | 所有预期 Work Item、成员、关联与 Outbox 已原子提交 |
| `not_applicable` | 是 | 页面、平台和状态匹配后没有任何适用规则；若存在 active-session 规则则必须是 `needs_review`，不能记为零规则 |
| `needs_review` | 是 | 只能依赖活动 Session、锁定事实漂移或证据可信性不足；可以同时关联已经发布的 snapshot Work Item |
| `failed` | 是 | 不可恢复错误或尝试次数耗尽 |
| `cancelled` | 是 | 当前任务周期被明确取消 |
| `superseded` | 是 | `finalization_epoch` 已变化，旧请求失效 |

所有状态迁移必须同时校验旧状态、`row_version`、租约 token 和当前任务 epoch。终态不得回到非终态；需要重新处理时必须由新 epoch 创建新 Publication。

## 8. 协议与 Redis 路由

### 8.1 Publication 可用事件

冻结协议：

```text
message_type: alioth.v2.h5-inspection-publication.available
payload_protocol_version: h5-inspection-publication.v1
aggregate_type: h5_inspection_publication
aggregate_id: publication_id
aggregate_version: publication.row_version
idempotency_key: h5-inspection-publication:{publication_id}:{row_version}
correlation_id: task_id
```

Redis 消息只包含现有统一信封中的引用字段：

- `message_id`
- `message_type`
- `task_id`
- `aggregate_type / aggregate_id / aggregate_version`
- `idempotency_key`

不得携带截图、DOM、可见文本、规则全文、Cookie、Token、租约凭证或对象存储地址。

### 8.2 专属 Stream 与 Consumer Group

Publication 使用独立路由，避免把 H5 发布命令交给现有 Inspection Work Item Handler：

```text
stream: alioth:v2:wakeup:h5-inspection-publication
consumer_group: alioth-v2-h5-inspection-publication-consumer
```

H5 Publication Consumer 完成工作项发布后，仍向现有共享 Stream 写：

```text
message_type: alioth.v2.inspection-work.available
stream: alioth:v2:wakeup:inspection
```

### 8.3 一轮业务尝试对应一条新消息

同一 `message_id` 只代表同一聚合版本的一次可重复投递。以下动作必须先增加 `row_version`，再写新的 Outbox：

- Publication `retry_wait` 到期后重新开放；
- Publication 租约过期后回收；
- H5 Work Item `retry_wait` 到期；
- H5 Work Item 租约过期；
- 人工证据决策允许重新开放。

不得修改已完成 Inbox 的身份，也不得用旧 Outbox `message_id` 表达新一轮业务重试。

## 9. H5 通知产生

### 9.1 发布条件

仅为同时满足以下条件的 Capture 创建 Publication：

1. `qc_task.target_platform = 'h5'`；
2. 任务锁定 `execution_mode = 'full_inspection'`；
3. 任务锁定 `h5_inspection_notification_mode = 'enforced'`；
4. 任务仍处于当前 `finalization_epoch` 的可发布阶段；
5. Capture 属于当前任务和 H5 探索 Run；
6. `capture_role = 'after_action'`，且 Capture 已留存；
7. `detection_status = 'recognized'`；
8. 存在稳定 `page_type` 和 `page_state`；
9. 存在 `action_execution_id`、`parent_capture_id`、before/after URL；
10. before/after 均具备 screenshot 与 DOM；
11. Artifact 归属、可信来源、对象路径前缀、MIME、大小和 SHA-256 均校验通过；
12. 存在独立持久化 PageTypeAssessment 及其稳定 hash；
13. 存在任务锁定规则集快照。

`exploration_only` 不创建 Publication，也不受本设计的通知门禁约束。

### 9.2 快路径事务

H5 Evidence Gate 通过后，在同一 PostgreSQL 事务内：

1. 获取 `task-finalization:{task_id}` advisory transaction lock；
2. 重新读取任务平台、执行模式、状态和 epoch；
3. 锁定 Capture、Assessment 与规则集快照身份；
4. 幂等插入 `qc_h5_inspection_publication`；
5. 插入 `alioth.v2.h5-inspection-publication.available` Outbox；
6. 追加审计时间线事实；
7. 提交事务。

只有事务提交后 Relay 才能看到 Outbox。API 或 Worker 禁止在提交后“顺手”直接调用 Redis。

### 9.3 对账兜底

快路径不是唯一可靠性来源。H5 Publication Sweeper 周期扫描：

```text
target_platform = h5
AND execution_mode = full_inspection
AND h5_inspection_notification_mode = enforced
AND task/finalization_epoch 仍有效
AND after_action Capture 已满足证据与 Assessment 门禁
AND 当前 epoch 不存在 Publication
```

Sweeper 使用与快路径完全相同的 Repository 命令幂等补建 Publication 与 Outbox。这样即使进程在证据提交后、Publication 写入前崩溃，也不会永久漏单。

证据未满足门禁时不发送空通知。Evidence Inventory 必须保存明确缺口，并使任务进入证据不足或人工复核，而不是等待一个永远不可执行的 Work Item。

## 10. Publication Consumer

### 10.1 处理顺序

1. Redis Dispatcher 把消息交给 PostgreSQL Inbox Guard。
2. Inbox 以完整事件身份核验权威 Outbox。
3. Consumer 按 `publication_id + aggregate_version` 领取 Publication 短租约。
4. 重新核对 H5 平台、full inspection、任务状态、epoch、Capture、Assessment hash 和 ruleset hash。
5. 从任务锁定规则集匹配候选规则。
6. 按 `required_page_state`、目标平台和执行路由过滤。
7. 按 strategy、category、session dependency、contract hash 和批次上限稳定分批。
8. snapshot 规则进入 Work Item；active_session 规则不在 Session 关闭后执行并计入 `excluded_active_session_rule_count`。
9. 原子创建逻辑消费 Run、Work Item、成员、H5 关联和 Work Item Outbox。
10. 没有 active-session 缺口时 Publication 推进到 `published`；只有 active-session 规则或 snapshot 与 active-session 混合时推进到 `needs_review`，但混合场景仍必须原子提交全部 snapshot Work Item；没有任何适用规则时推进到 `not_applicable`。
11. 提交成功后 Inbox 标记完成，Dispatcher 才允许 ACK Redis。

### 10.2 锁定输入

Publication Consumer 不得基于当前可编辑规则重新解释历史请求。必须同时校验：

- `ruleset_id`
- `ruleset_version`
- `ruleset_snapshot_hash`
- 每个规则的 `contract_hash`
- `assessment_hash`
- `finalization_epoch`

任一锁定身份漂移都禁止自动覆盖；根据错误类型进入 `needs_review` 或 `superseded`。

### 10.3 原子工作项发布

当前共享 Repository 的 `publish()` 以单个批次为事务边界。实施时应抽取可复用的事务内插入能力，并新增 H5 Publication 原子批量方法，使以下写入在一个事务完成：

1. 创建或复用 H5 逻辑 `rule_judge` Run；
2. 创建或复用全部预期 Inspection Work Item；
3. 写入全部 Rule Members；
4. 写入全部 Publication–Work Item 关联；
5. 为每个新聚合版本写 `inspection-work.available` Outbox；
6. 更新 Publication 计划统计和终态。

任何一步失败，批量发布事务整体回滚；随后 Consumer 使用原 Publication 租约在独立的受保护状态事务中释放租约并进入 `retry_wait`。若失败状态写回也失败，则保留 `leased`，由租约过期回收。不得形成“Publication 已 published，但只落了一部分预期批次”的状态。

已有 Work Item 的 `input_hash` 和唯一约束仍是幂等权威；重复执行批量发布只会补齐关联，不会创建第二份工作项。

## 11. H5 持久化逻辑质检 Run

### 11.1 身份

每个 `(task_id, finalization_epoch)` 使用一个持久化逻辑消费 Run：

```text
agent_type = rule_judge
agent_name = h5-inspection-consumer
parent_agent_run_id = source H5 exploration run
```

Run ID 使用命名空间 UUID 从以下稳定字符串派生：

```text
h5-inspection-consumer:{task_id}:{finalization_epoch}
```

通过显式 UUID 和 `INSERT ... ON CONFLICT` 保证并发创建幂等，不依赖 Worker 进程身份。

### 11.2 所有权

- Work Item 的 `producer_agent_run_id` 仍是产生 Capture 的 H5 探索 Run，保持 `page_capture.agent_run_id = producer.id` 的现有约束。
- H5 逻辑 `rule_judge` Run 作为 `consumer_agent_run_id` 参与 Work Item claim 和 Attempt 审计。
- Redis Worker 实例只写入 `lease_owner`，不是业务消费主体。

共享 Inspection Handler 处理 H5 Work Item 时，应按 Work Item 来源关联解析当前 epoch 的 H5 逻辑消费 Run；不得退化为“任意任务内 running Run”。App Work Item 继续走原有 Run 解析逻辑。

### 11.3 生命周期

只有以下事实全部成立时，H5 逻辑质检 Run 才转为 `done`：

- 当前 epoch 所有 H5 Publication 已终态；
- 所有关联 Work Item 已终态；
- 没有 `running` 或 `judged` Attempt；
- 没有到期前的合法重试；
- 没有 H5 通知 Outbox/Inbox 待处理或死信。

Run 关闭由 PostgreSQL Reconciler 驱动，不依赖探索 Worker 的 finally 块。取消时转 `canceled`；不可恢复链路失败时转 `failed`。

## 12. 故障恢复

### 12.1 Publication Sweeper

周期执行以下对账：

| 数据库事实 | 恢复动作 |
| --- | --- |
| 合格 H5 Capture 缺 Publication | 幂等补建 Publication + Outbox |
| Publication=`pending` 且没有有效当前版本 Outbox | 写当前版本 Outbox |
| Publication=`retry_wait` 且已到期 | `row_version + 1`，转 pending，写新 Outbox |
| Publication=`leased` 且租约过期 | 清租约、增加版本，转 pending 或 retry_wait，写新 Outbox |
| Publication 尝试耗尽 | 转 failed，记录稳定原因，阻断 Completion Gate |
| Publication epoch 落后于任务 | 转 superseded，不再唤醒 |

### 12.2 H5 Work Item Reconciler

仅处理能通过 `qc_h5_inspection_publication_work_item` 证明来源的 Work Item：

| 数据库事实 | 恢复动作 |
| --- | --- |
| Work Item=`pending` 且当前版本无有效 Outbox | 写版本化 Work Item Outbox |
| Work Item=`retry_wait` 且到期 | 增加 row version，转 pending，写新 Outbox |
| Work Item=`leased/running` 且租约过期 | 旧 Attempt abandoned，增加版本后重新唤醒 |
| Attempt=`judged` | 重放已保存结果，不再调用模型 |
| 正式结果已存在但成员/工作项未收口 | `sync_formal_results` 对账收口 |
| Work Item epoch 已过期 | superseded/cancelled，拒绝迟到写回 |

该 Reconciler 不扫描或改变没有 H5 Publication 关联的 App Work Item。

### 12.3 故障矩阵

| 故障点 | PostgreSQL 事实 | 恢复结果 |
| --- | --- | --- |
| 对象上传成功、Capture/Manifest 未提交 | 无可信引用 | 不创建 Publication；孤儿对象按既有策略清理 |
| Capture 已提交、Publication 前崩溃 | 合格 Capture 存在 | Publication Sweeper 补建 |
| Publication 与 Outbox 已提交、Redis 失败 | Outbox 未发布/待重试 | Relay 重试或 Outbox Sweeper 重排 |
| Redis Stream 被清空 | Publication/Work Item 仍在 PG | Reconciler 生成新版本唤醒 |
| Publication 消息重复 | 同一 Outbox 与 Inbox 身份 | Inbox 完成态直接 ACK，不重复发布 |
| Publication Consumer 事务中崩溃 | 整批事务回滚或完整提交 | 租约回收后幂等重试 |
| Work Item 领取后崩溃 | 租约和 Attempt 可见 | 租约过期后新 Worker 领取 |
| Judge 已返回、写回前崩溃 | Attempt=`judged` 且有结果载荷 | 只重放写回 |
| 正式结果已写、工作项未终态 | Rule Execution 存在 | 对账成员和 Work Item，不再判定 |
| 任务取消后迟到消息 | epoch/任务状态不匹配 | Publication/Work Item cancelled 或 superseded |

## 13. Completion Gate

### 13.1 启用条件

新增 H5 Publication 门禁只在以下条件启用：

```text
target_platform = h5
AND execution_mode = full_inspection
AND h5_inspection_notification_mode = enforced
```

App、H5 `exploration_only` 以及通知模式为 `off/shadow` 的任务不读取这些新增门禁条件。

### 13.2 新增事实

Gate 至少新增：

- `expected_h5_publication_count`
- `terminal_h5_publication_count`
- `nonterminal_h5_publication_count`
- `failed_h5_publication_count`
- `h5_publication_inventory_complete`
- `h5_notification_outbox_unsettled_count`
- `h5_notification_inbox_unsettled_count`
- `h5_notification_dead_letter_count`

“预期 Publication”必须从合格 H5 after_action Capture 与 Evidence Inventory 计算，不能只统计 Publication 表已有行，否则漏建 Publication 仍会被误判为空集合完成。

### 13.3 放行条件

H5 full inspection 只有同时满足以下条件才能完成：

1. 所有预期动作均形成终态 Evidence Inventory；
2. 每个合格 after_action Capture 都存在当前 epoch Publication；
3. 所有 Publication 已终态；
4. 所有 Publication 关联的 Work Item 均已终态；`needs_review` Publication 已创建的 snapshot Work Item 同样必须收口；
5. 所有候选规则成员均已终态：正式结果包含 `manual_review` 时成员仍为 `written`，没有正式结果的成员只能明确为 `evidence_insufficient / skipped / error`；
6. 不存在 `running/judged` Attempt；
7. 不存在当前 H5 通知的未发布、重试中或死信 Outbox/Inbox；
8. H5 逻辑质检 Run 已可安全关闭；
9. 人工复核策略已经满足。

Publication 终态映射：

| Publication 状态 | 任务处理 |
| --- | --- |
| `published` | 等待关联工作项与规则成员终态 |
| `not_applicable` | 明确的零适用规则终态 |
| `needs_review` | 等待已关联 snapshot Work Item 收口，生成或关联复核项，任务进入 needs_review |
| `failed` | 阻断完成，报告通知链失败 |
| `cancelled/superseded` | 仅在与任务当前周期语义一致时接受 |

不得依据 Redis backlog、Consumer 内存计数或日志判断 Gate 已通过。

## 14. 错误分类

| 错误码 | 含义 | 策略 |
| --- | --- | --- |
| `H5_PUBLICATION_CAPTURE_NOT_READY` | Capture 或核心 Artifact 尚未完整 | 有界重试 |
| `H5_PUBLICATION_CHECKSUM_MISMATCH` | 对象字节与元数据不一致 | needs_review |
| `H5_PUBLICATION_ASSESSMENT_MISSING` | 缺少持久化 PageTypeAssessment | 有界重试，耗尽后 failed |
| `H5_PUBLICATION_ASSESSMENT_DRIFT` | Assessment hash 漂移 | needs_review |
| `H5_PUBLICATION_RULESET_DRIFT` | 规则快照身份或 hash 漂移 | needs_review |
| `H5_PUBLICATION_EPOCH_STALE` | 任务进入新 epoch | superseded |
| `H5_PUBLICATION_PLAN_FAILED` | 规则匹配或批次编译失败 | 有界重试 |
| `H5_PUBLICATION_WORK_WRITE_FAILED` | 原子工作项发布失败 | 有界重试 |
| `H5_PUBLICATION_ATTEMPTS_EXHAUSTED` | Publication 尝试耗尽 | failed |
| `H5_NOTIFICATION_OUTBOX_DEAD_LETTER` | H5 通知发布耗尽 | failed，阻断 Gate |
| `H5_INSPECTION_CONSUMER_UNAVAILABLE` | H5 逻辑消费 Run 无法建立或恢复 | 有界重试 |

错误消息限制长度并只保存稳定业务摘要。禁止保存截图、DOM、Cookie、Token、对象存储签名地址、完整模型输入或异常堆栈。

## 15. 通知阶段与工作台投影

### 15.1 阶段模型

“H5 通知成功”不是单一布尔值，冻结为四级：

```text
publication_persisted
  -> wakeup_delivered
  -> work_claimed
  -> inspection_completed
```

| 阶段 | 权威事实 |
| --- | --- |
| `publication_persisted` | Publication + Publication Outbox 已提交 |
| `wakeup_delivered` | Outbox Stream 回执 + Inbox 处理事实 |
| `work_claimed` | Work Item lease + Attempt |
| `inspection_completed` | Rule Execution + Work Item 终态 + Completion Gate |

只有 `inspection_completed` 可以对用户显示“质检完成”。

### 15.2 现有页面扩展

不新增菜单或路由。在现有 H5 任务详情、时间线和探索结果中增加：

- 当前通知阶段；
- Publication 总数及各状态数量；
- 最早待处理时间和排队时长；
- Work Item、Attempt、Rule Execution 数量；
- 最近稳定错误码和简化说明；
- 等待重试、等待人工复核或已失败状态；
- 完整追踪链：

```text
task_id
  -> source_agent_run_id
  -> action_execution_id
  -> page_capture_id
  -> page_type_assessment_id
  -> publication_id
  -> work_item_id
  -> attempt_id
  -> observation_packet_id
  -> rule_execution_id
```

页面不展示 Redis entry、lease token、对象路径或内部堆栈。

## 16. 运维指标与告警

建议指标：

- `h5_publication_pending_total`
- `h5_publication_oldest_age_seconds`
- `h5_publication_retry_total`
- `h5_publication_failed_total`
- `h5_notification_outbox_dead_letter_total`
- `h5_notification_inbox_retry_total`
- `h5_inspection_work_retry_wait_total`
- `h5_inspection_attempt_replay_total`
- `h5_notification_end_to_end_seconds`

建议告警：

- Publication pending 超过配置的正常发布窗口；
- H5 Publication Outbox 或 Inbox 出现死信；
- H5 Work Item 长时间没有 Attempt；
- `judged` Attempt 长时间未写回；
- 当前 epoch 的合格 Capture 缺 Publication；
- Completion Gate 连续被 H5 通知事实阻断；
- Publication 计划批次数与关联 Work Item 数不一致。

所有指标必须带受控标签，至少包含环境和错误分类；不得以 `task_id`、`capture_id` 作为高基数常驻指标标签，具体身份进入结构化日志和审计查询。

## 17. 数据库迁移

新增迁移包含：

1. `qc_h5_inspection_publication`；
2. `qc_h5_inspection_publication_work_item`；
3. Publication 状态、租约完整性、尝试次数和终态一致性约束；
4. Publication 领取、Sweeper、任务投影和 Gate 查询索引；
5. H5 Publication Outbox 消息类型与版本约束；
6. Completion Gate 快照所需 H5 Publication 统计列；
7. 任务提交配置锁定 `h5_inspection_notification_mode`；缺失该键的历史任务按 `off` 解析；
8. 如前置整改尚未落地，则同时协调 H5 PageTypeAssessment 的独立持久化迁移，但保持其表所有权属于 H5 探索领域。

迁移不回填 App 数据，不向 App 专属表写入任何记录。

### 17.1 历史运行中 H5 任务

历史任务默认保持 `off`，不因部署配置切换而自动改走新链。只有经受控迁移显式写入 `h5_inspection_notification_mode='enforced'`、且迁移事务确认尚未被旧同步路径发布的运行中 H5 full inspection 任务，才允许执行以下补建：

- 合格 Capture 且锁定 Assessment/规则快照可证明：Sweeper 幂等补建 Publication；
- 已有相同 Work Item `input_hash`：建立 H5 关联，不重复创建；
- 无法证明 Assessment 或规则快照身份：转人工复核；
- 已最终封存任务：不自动重开，不补跑模型。

## 18. 灰度与回退

H5 专属开关：

```text
off -> shadow -> enforced
```

该模式在任务提交时写入不可变任务快照。运行中的任务不得因部署配置切换而改变模式；灰度切换只影响切换后新建的任务，避免同一 `(task_id, capture_id, epoch)` 同时由旧路径和 Publication 路径发布。

### 18.1 off

- 保持当前 H5 Worker 同步调用 Publisher；
- 不创建正式 Publication；
- App 始终保持原路径。

### 18.2 shadow

- 由现有 `H5WorkflowShadowRecorder` 或等价只读审计投影生成“若 enforced 将创建的 Publication 身份”和稳定发布计划，不写权威 `qc_h5_inspection_publication`；
- 不创建 enforced Work Item，不调用 Judge；
- 与旧 H5 同步路径按 Capture、规则集合、批次、contract hash 和 `input_hash` 对账；
- Shadow 审计事实不得阻断生产 Completion Gate，也不得被 H5 Publication Sweeper 补建为权威 Publication。

### 18.3 enforced

- H5 Publication Consumer 成为 H5 创建共享 Work Item 的唯一入口；
- 删除或关闭 H5 Worker 收口阶段对 `H5EvidenceHandoffService.prepare()` 中同步 Publisher 的依赖；
- H5 Gate、Sweeper、投影和告警全部启用；
- App 路径不读取该开关。

### 18.4 enforced 准入条件

- Shadow 不漏合格 Capture；
- 候选规则、批次顺序、contract hash 和 Work Item input hash 一致；
- 重复通知不产生重复工作项；
- Redis 清空后可以从 PostgreSQL 恢复；
- Publication、Work Item 与 Gate 统计一致；
- H5 与 App 并行运行不串扰；
- 真实 PostgreSQL、Redis 和对象存储集成验收通过。

### 18.5 回退

从 enforced 回退到 shadow/off 时：

- 不删除已创建 Publication、Work Item、Outbox 或 Inbox；
- 已发布 Work Item 继续由共享执行链收口；
- 已锁定 `h5_inspection_notification_mode='enforced'` 的运行中任务继续走 Publication 链，不随部署开关回退；禁止重新启用旧同步路径去发布当前 epoch 已有 Publication 的 Capture；
- 通过 H5 Publication 是否存在和 Work Item `input_hash` 防止双发。

## 19. 测试设计

### 19.1 单元测试

- Publication `input_hash`、唯一身份和冲突语义；
- H5/App、full inspection/exploration only 和 epoch 隔离；
- 状态转换、租约续期、过期回收和陈旧 token 拒绝；
- Assessment、ruleset 和 checksum 漂移；
- 候选规则过滤与稳定分批；
- active_session 规则转人工边界；
- H5 Publication–Work Item 关联；
- 新聚合版本消息与 Inbox 去重；
- `judged` Attempt 只重放、不重复调用 Judge；
- H5 逻辑消费 Run 的确定性身份和关闭条件。

### 19.2 PostgreSQL 集成测试

- Publication 与 Outbox 原子提交；
- 全部 Work Item、成员、关联、Outbox 和 Publication 终态原子提交；
- 并发 Consumer 只能领取一次；
- Sweeper 并发补建不产生重复 Publication；
- Publication/Work Item `retry_wait` 到期产生新版本消息；
- 租约过期拒绝旧 Worker 写回；
- Completion Gate 对漏 Publication、非终态、失败和死信的阻断；
- 历史任务补建及已有 Work Item 关联；
- H5 Reconciler 不修改 App Work Item。

### 19.3 Redis 集成测试

- Publication Relay 发布成功后保存 Stream 回执；
- 发布失败退避重试；
- 重复消息被 Inbox 幂等处理；
- Consumer 崩溃后通过 `XAUTOCLAIM` 恢复；
- Stream 清空后由 PostgreSQL Reconciler 重建新版本唤醒；
- Redis 伪造消息无法通过 Outbox 权威核验；
- Publication Stream 与 Inspection Stream 路由严格隔离。

### 19.4 端到端场景

1. H5 正常探索、Publication、质检和报告完成。
2. Capture/Manifest 提交后、Publication 写入前进程崩溃。
3. Publication 已提交但 Redis 不可用。
4. Publication Consumer 批量事务执行中崩溃。
5. Work Item 创建成功但 ACK 前进程崩溃。
6. Judge 返回后、正式结果写回前崩溃。
7. Work Item 进入 `retry_wait` 后没有其他新业务消息。
8. 任务取消或 epoch 提升后收到迟到通知。
9. 无适用规则与活动会话规则需复核。
10. Redis Stream 被清空后重新收敛。
11. PostgreSQL、Redis、Worker 分别重启后继续执行。
12. H5 与 App 同时运行且没有消息、Run、Work Item 或 Gate 串扰。

## 20. 验收标准

### 20.1 代码级验收

- 所有新增领域状态和错误码有契约测试；
- 新增注释为中文 UTF-8；
- H5 Publication、Consumer、Sweeper、投影各自保持单一职责；
- `rg` 可证明 App Worker、App Publisher 和 App sealed Index 无行为修改；
- Completion Gate SQL 有 H5-only 条件和漏 Publication 反向测试；
- 所有幂等身份和版本化消息有并发测试。

### 20.2 集成验收

- 清空 Redis 后，未完成 H5 Publication 和 Work Item 可从 PostgreSQL 恢复；
- 任一事务边界崩溃不会漏 Capture、重复 Work Item 或重复正式结果；
- 当前 epoch 全链可追踪：

```text
task -> capture -> assessment -> publication
     -> work item -> attempt -> observation packet -> rule execution
```

- 通知失败明确阻断 Completion Gate；
- 工作台能显示停滞阶段、稳定错误和下一步处理方式；
- App 并行任务结果与基线一致。

### 20.3 生产放量前验收

- 真实 PostgreSQL 并发租约与事务故障注入；
- 真实 Redis Stream、Consumer Group、Pending List 与 `XAUTOCLAIM`；
- 真实对象存储字节和 checksum 校验；
- 真实 H5 浏览器探索形成 before/after Capture；
- 真实 LLM Judge 的超时、限流、成功和结构化输出失败；
- P95/P99 通知延迟、重试率、模型重放率和死信率达到运营阈值。

代码级测试通过不能表述为生产环境联合验收完成。

## 21. 预计代码落点

### 21.1 新增 H5 专属文件

- `domain/inspection/h5_publication.py`：Publication 状态、身份与迁移规则。
- `application/h5_inspection_publication.py`：创建、领取、转换、恢复端口。
- `application/h5_inspection_publication_planner.py`：锁定规则过滤与稳定批次编译。
- `infrastructure/db/h5_inspection_publication_repository.py`：事务、租约、原子批量工作项发布。
- `agents/runtime/h5_inspection_publication_wakeup.py`：Publication 数据库权威 Handler。
- `infrastructure/db/h5_inspection_publication_reconciler.py`：漏建、到期与失联对账。
- 对应 migration、单元和集成测试。

### 21.2 最小修改共享文件

- `application/outbox.py`：注册 H5 Publication 可路由消息。
- `infrastructure/queue/redis_stream.py`：新增 H5 专属 Stream 路由。
- `worker.py`：启动 H5 Publication Consumer/Reconciler；enforced 时关闭 H5 同步 Publisher。
- `infrastructure/db/inspection_work_item_repository.py`：抽取事务内批量写入能力，并为 H5 来源解析逻辑消费 Run。
- `agents/runtime/inspection_wakeup.py`：仅对 H5 来源工作项使用持久化 H5 consumer Run；App 保持旧分支。
- `infrastructure/db/task_finalization_repository.py`：增加 H5 Publication Gate 事实。
- 现有工作台 Task Insight 查询和 Vue 详情组件：增加 H5 通知投影，不新增路由。

所有共享修改必须有 App 非回归测试，并以 H5 来源关联或 `target_platform='h5'` 作为显式分支条件。

## 22. 已冻结决策

1. 只做 H5 内部质检通知，不做 App，不做用户消息通知。
2. PostgreSQL 是 Publication、Work Item、Attempt 和完成门禁的唯一事实源。
3. Redis 只负责短期唤醒，不承载证据、规则正文或业务租约。
4. 新增 H5 专属 Publication 聚合，不让 Evidence 事件直接调用 Judge。
5. Publication 与 Work Item 是不同聚合，分别解决“应否创建工作”和“工作如何执行”。
6. H5 Publication 使用独立 Stream；共享 Work Item 继续使用现有 inspection Stream。
7. H5 使用持久化逻辑 `rule_judge` Run，不依赖探索 Run 继续运行。
8. 业务重试必须产生新聚合版本和新 Outbox 身份。
9. Completion Gate 必须反向检查合格 Capture 是否漏 Publication。
10. App 创建、唤醒、消费、收口和数据结构不因本设计改变。
11. 不新增前端菜单或路由，只扩展现有 H5 任务详情投影。
12. 采用 `off -> shadow -> enforced` 灰度，未经真实依赖验收不得直接放量。
