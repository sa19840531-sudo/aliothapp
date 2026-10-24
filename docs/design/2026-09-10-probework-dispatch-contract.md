# ProbeWork 派发与消费合同（公共层）

日期：2026-09-10。范围：`evidence_request_routing` 产出 ProbeWork 之后，到平台执行器领到活为止的这一段。
**不含各平台 Runtime 内部**——怎么开浏览器、怎么驱动设备、怎么重放路线，由 H5/Web/App 各自的 Runtime 决定，本合同不做要求。

按方案 [03-targeted-recapture](../../《9月方案明细》) §5 的职责划分：Judge 只提出请求，Runtime/Broker 才能操作设备、浏览器和外部 Surface。派发这一段属于公共编排层，所以收敛在这里，三端共用一份。

## 1. 为什么需要这份合同

补采 Probe 和探索 Exploration 共用同一张工作表 `qc_v2_work_item`。领取（`claim`）原来只按 `work_type` + `task_id` 过滤，**不看界面类型**。

单端时期没暴露：Probe 只有 App 在领，H5 的 `h5_exploration_turn` 把 `work_type` 写死成 `EXPLORATION`，两边碰不上。但一旦 H5/Web 接上 Probe 消费，Web 执行器就会领到 `surface_kind=android` 的恢复路线——两端的执行器现在都还留着「领到之后才发现不是自己的活」的事后校验（`app_exploration_turn` 认 android/ios、`h5_exploration_turn` 认 web），那是补救，不是防线。

本合同把界面类型提到领取条件里：**领不到不属于自己的活**。

## 2. 派发（谁写、写什么）

ProbeWork 只有一个合法产出点：`RestoredProbePublicationService.publish()`。前提是这条请求的 `RestorationAttempt` 已经完成且 `status=equivalent`、状态等价评估已经持久化——`qc_v2_work_item` 的外键 `fk_qc_v2_work_item_restoration_assessment` 与 CHECK `ck_qc_v2_work_item_probe_proof` 锁死了这一点，任何「先发 Probe、评估后补」的写法（包括 09-12 之前路由层那个 `state-equivalence:pending:*` 占位 ID）落库即拒。

`evidence_request_routing` 只做受理：补采闸门、恢复策略、页面图选路、确定性不可补采的结算。受理通过的请求保持 `pending`，由本端恢复执行器领取后按固定顺序走：

1. **恢复，拿到一张新 Capture**。原 Capture 仍是当前面 → 只重新观察一次、不做动作（`EvidenceRecoveryReplayExecutor._observe_original_surface`）；需要导航 → 按规划路线逐边重放到达。两种情况都不能拿原 Capture 充当恢复结果：`StateEquivalenceGate` 直接拒（「恢复状态快照必须使用新的 Capture」），方案 03 §16 也要求 restored 来自独立实际状态。
2. **等价评估并持久化**。`RestorationCoordinator` 用原 Capture 快照与新 Capture 快照过 `StateEquivalenceGate`，终态写 `qc_restoration_attempt`。
3. **发布**。`RestoredProbePublicationService` 用真实 `restoration_attempt_id` / `equivalence_assessment_id` 产出 `ProbeWork`，其中 `surface_kind` 是**派发依据**，不是提示字段。

这三步的编排（`RestorationCoordinator` → `TargetedRecaptureRestorationService` → `RestoredProbePublicationService`）是公共层的，三端共用。各端只需要实现两个 Runtime 端口：`EvidenceRecoveryRuntime.observe()` / `.execute()`（重新观察 / 逐边执行，产出新 Capture）和「从 Capture 构造 `StateInvariantSnapshot`」的读取器。App 端参考 `app_targeted_recapture_composition.py` + `app_capture_state_invariants.py`；H5/Web 端待接（见 §8）。

发布走 `PostgresProbeWorkPublisher.publish()`：

1. 写入 `qc_v2_work_item`，`work_payload` 里带完整 ProbeWork（含 `surface_kind`）。
2. 同事务写 Outbox 唤醒事件，消息类型：

   | 工作类型 | message_type |
   | --- | --- |
   | probe | `alioth.v2.probe-work.available` |
   | exploration | `alioth.v2.exploration-work.available` |

3. 发布受补采闸门管：`SupplementalEvidenceOperation.PUBLISH_PROBE`，闸门关着不写。

**幂等**：Outbox envelope 的 `idempotency_key` 就是 `work_id`，`aggregate_type=explorer_work`、`aggregate_id=work_id`。同一个 work 重复发布不会产生第二条唤醒。

**被拒时的原因码**：发布被拒抛 `ExplorerWorkPersistenceError`，`reason_code` 按 INSERT 的前置条件顺序只报第一条不满足的（诊断只读；拿不到事实时为 `None`、文案退回原来的「归属无效」）：

| reason_code | 含义 |
| --- | --- |
| `TASK_NOT_RUNNABLE` | 任务不存在、不在 queued/running，或当前收口阶段不接受这类工作 |
| `SOURCE_CAPTURE_INVALID` | 来源 Capture 不存在、不属于该任务或未采集成功 |
| `EVIDENCE_REQUEST_INVALID` | 请求不存在、不属于该任务或已进入终态 |
| `RESTORATION_ATTEMPT_MISSING` | 没有这条恢复尝试——「先发 Probe 再补评估」落在这里 |
| `RESTORATION_NOT_EQUIVALENT` | 恢复尝试存在但没通过状态等价门禁 |
| `EQUIVALENCE_PROOF_MISMATCH` | 恢复尝试属于别的请求，或评估 ID / 来源等级 / 计划哈希 / 原 Capture / 恢复 Capture 对不上（文案点名哪几项） |
| `WORK_IDENTITY_DRIFT` | 同一 `work_id` 已存在但内容不同 |

创建恢复尝试（`PostgresRestorationAttemptRepository.start`）被拒同样带 `reason_code`：`EVIDENCE_REQUEST_MISSING` / `EVIDENCE_REQUEST_INVALID` / `EVIDENCE_REQUEST_EXPIRED` / `TASK_NOT_RUNNABLE` / `ORIGINAL_CAPTURE_INVALID` / `REPLAY_PLAN_MISSING` / `ATTEMPT_IDENTITY_DRIFT`。

## 3. 消费（谁领、怎么领）

平台执行器用 `ClaimExplorerWorkCommand` 领活，四个围栏缺一不可：

| 字段 | 作用 |
| --- | --- |
| `work_type` | 领 probe 还是 exploration |
| `surface_kinds` | **本执行器能跑的界面类型**，留空表示不过滤（既有单端行为） |
| `task_id` | 限定任务，`None` 表示跨任务领 |
| `lease_owner` / `lease_seconds` | 租约归属与时长 |

各端声明：

```
App  执行器：surface_kinds=(ANDROID, IOS)
H5   执行器：surface_kinds=(WEB,)
Web  执行器：surface_kinds=(WEB,)
```

H5 与 Web 同为 `web`，本合同不再细分——两者要区分请用 `task_id` 或 `lease_owner`，不要往 `SurfaceKind` 里加值。

**领取还必须同时满足**（SQL 里已有，不需要调用方传）：

- 任务 `status='running'`；
- `work_item.finalization_epoch = task.finalization_epoch`（见 §5）；
- 未过 `deadline`、`attempt_count < max_attempts`；
- 状态是 `pending`/`retry_wait` 且到了 `next_attempt_at`，或者 `leased`/`running` 但租约已过期（可接管）；
- probe 在 `exploration_draining` / `session_draining` 阶段还能领，但要求对应 Evidence Request 是活会话依赖且未终态——这是补采排空窗口的边界，不是常规领取。

领取受闸门管：`SupplementalEvidenceOperation.CLAIM_PROBE`。

## 4. 成功证明

执行成功后**不要自己写请求状态**，交回公共入口 `evidence_request_consumption`：

```
ProbeCompletionProof(六字段) → attach_supplement(task_id, proof)
```

六个字段缺一不可：`request_id`、`result_capture_id`、`work_id`、`restoration_attempt_id`、`equivalence_assessment_id`、`source_grade`。缺任一项不给挂——补采证据必须能追溯到是哪次请求、哪次恢复尝试、哪次等价评估产出的。

`attach_supplement` 做三件事：结果 Capture 取 PASS Manifest（只有待定版本时先定稿）→ 校验请求身份与状态 → 写 Evidence Supplement。**写 Supplement 的同一条语句会把请求原子置为 satisfied**，所以成功路径到此为止，不要再调 `settle_terminal`。

工作项自身的终态用 `TransitionExplorerWorkCommand` 推到 `succeeded`。

## 5. Run/Epoch 对齐

`finalization_epoch` 是收口世代号。派发时写进工作项，领取时与任务当前 epoch 比对，**不等就领不到**。

含义：任务收口重开一轮（epoch+1）后，上一轮遗留的工作项自动失效，不会被新一轮的执行器捡起来跑。执行器不需要自己判断，也**不要**把旧 epoch 的活「顺手做掉」。

同理，补采结果回挂时 `attach_supplement` 会校验请求与任务归属一致，跨执行域直接拒。

## 6. 失败与重试

工作项状态机：`pending → leased → running →` 终态（`succeeded` / `failed` / `blocked` / `bounded` / `budget_exhausted` / `no_progress`），可重试的失败回到 `retry_wait`。

约定：

- **重试次数**由 `attempt_count` / `max_attempts` 管，领取时 `attempt_count` 自增。超出上限领不到，由收口按事实结算，**不要在执行器里自己加次数**。
- **重试间隔**用 `next_attempt_at`，回 `retry_wait` 时设定。
- **租约过期即可接管**：`leased`/`running` 但 `lease_expires_at <= now()` 的工作项允许被新 owner 领走。接管方必须先只读核对未决动作与副作用，再决定是否重跑——这是方案 §5 对 owner 切换的硬要求，不能从旧路径索引自动重放。
- **不可重试的失败**直接推终态并写清 `last_error_code` / `last_error_message`，不要留在 `retry_wait` 空转。

补采请求侧的重试是另一套（`release_lease` 安排下次尝试、耗尽自动转终态），与工作项重试分开计数，不要互相顶替。

## 7. 幂等

| 环节 | 幂等键 | 行为 |
| --- | --- | --- |
| Outbox 唤醒 | `work_id` | 重复发布不产生第二条事件 |
| 工作项发布 | `work_id` | 已存在则返回 `created=False`，不覆盖 |
| 证据挂接 | `request_id` + `manifest_id` | 重复挂接不重复计入血缘；请求已 satisfied 时按同一 manifest 幂等通过 |
| 请求终态 | 状态本身 | `settle_terminal` 遇到已终态直接返回既成终态，不重复流转 |

## 8. 接入清单

| 事项 | 归属 | 现状 |
| --- | --- | --- |
| `surface_kinds` 进领取条件 | 立川（公共层） | 09-10 已做 |
| App 执行器声明 android/ios | 立川（公共层） | 09-10 已做 |
| H5 执行器声明 web | 立川（公共层） | 09-10 已做 |
| 路由层伪造 `state-equivalence:pending:*` 占位评估 ID | 立川（公共层） | 09-12 已删：路由只受理选路、不发布 Probe；源码守卫测试锁住 |
| Probe 发布 / 恢复尝试创建被拒的稳定原因码 | 立川（公共层） | 09-12 已做（§2 表） |
| 真库整链测试 EvidenceRequest → RestorationAttempt → EquivalenceAssessment → ProbeWork | 立川（公共层） | 09-12 已做：`tests/test_targeted_recapture_chain_postgres.py` |
| H5/Web 领取 Probe 并执行（本合同 §3）、成功后交回 `attach_supplement` | 天枢 | 09-11 已接（`h5_probe_work`） |
| **H5/Web 恢复执行端口**：`EvidenceRecoveryRuntime.observe()`（重新观察一张新 Capture）、`.execute()`（逐边重放）、Capture → `StateInvariantSnapshot` 读取器，以及领 pending 请求的消费者接线（参考 `AppTargetedRecaptureConsumer`） | 天枢 / 青云客（Runtime） | **待做**——没有它，Web 请求受理后一直 pending，不会有 ProbeWork 可领 |
| 补采闸门 `ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED` | 运维/观星 | **默认 false，worker 未设置**；不开这条链整体不动 |
