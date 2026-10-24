# 任务级质检证据输入集合设计

日期：2026-08-14

## 1. 决策结论

任务级规则采用独立的 `EvidenceInputSet` 作为不可变输入边界，不扩张 `journey` 字段承载全部跨页事实，也不选择第一张或最后一张 `page_capture` 充当任务级锚点。

现有单页 `InspectionWorkItem -> ObservationPacket -> RuleExecution` 路径保持兼容。任务级规则只有在权威候选快照完成、全部候选 Capture 均存在 PASS Manifest、`EvidenceInputSet` 已封存后，才允许发布一份任务级工作项。

## 2. 为什么不能只扩展 journey

`journey` 是单页 Observation Packet 中的有界辅助事实，不具备以下任务级保证：

1. 无法证明任务内全部符合条件的 Capture 已纳入；
2. 没有独立的规则合同、规则集快照与 `finalization_epoch` 身份；
3. 不能对跨页集合形成稳定幂等哈希；
4. 不能阻止任务封存后旧 Worker 继续追加输入；
5. 不能表达同一逻辑页面的多环境矩阵成员。

因此 journey 仍可作为成员内部的上下文，但不得作为任务级证据闭包本身。

## 3. 协议模型

`evidence-input-set.v1` 包含：

- 任务与运行身份：`task_id`、`source_agent_run_id`、`finalization_epoch`；
- 规则身份：`rule_id`、`rule_code`、`rule_contract_hash`、`ruleset_snapshot_hash`；
- 范围身份：`scope`、`scope_key`；
- 权威选择快照：`eligibility_snapshot_id`、`eligibility_snapshot_hash`、`selection_policy_hash`；
- 闭包：有序 `eligible_capture_ids` 与逐项 `members`；
- Manifest 血缘：每个成员绑定 `manifest_id/version/hash`、附件 ID 与 Packet ID；
- 可选维度：`journey_execution_id`、`environment_profile_id`；
- 幂等身份：不受重试时间影响的 `input_set_hash` 与 `input_set_id`。

范围规则：

- `page`：只能有一个 Capture，继续服务现有单页语义；
- `journey`：所有成员必须属于同一 `JourneyExecution`；
- `task`：覆盖任务候选快照列出的全部 Capture，Toast 样式统一使用此范围；
- `environment_matrix`：至少两个成员，每个成员绑定唯一 `environment_profile_id`。

## 4. 封存门禁

封存必须同时满足：

1. 候选 Capture 集合与权威 `eligible_capture_ids` 完全相等；
2. 每个 Manifest 与任务归属一致且 `quality_status=pass`；
3. 成员顺序由权威候选顺序决定，ordinal 从 1 连续递增；
4. 范围专用约束全部成立；
5. 输入哈希与所有身份、快照和成员血缘一致。

缺成员、重复 Capture、非 PASS Manifest、跨任务 Manifest、旧 epoch、旅程混用或环境重复均失败关闭，不生成任务级 Judge 工作项。

## 5. 持久化状态与工作项兼容

迁移 `20260814_0060` 已新增独立的 `qc_evidence_input_set` 主表与有序成员表；PostgreSQL 仓储在 `task-finalization:{task_id}` 事务锁内校验当前 `session_draining + finalization_epoch`、任务/Run/规则集快照、Capture 角色、PASS Manifest 精确版本/哈希及 artifact/packet 闭包。稳定身份重试只复用首次封存时间，不重复写成员。

迁移 `20260814_0061` 进一步新增独立的 `qc_aggregate_inspection_work_item` 与 attempt 审计表：一份 EvidenceInputSet 已绑定唯一规则，因此一个聚合工作项只执行该规则；旧 `qc_inspection_work_item.page_capture_id` 继续保持非空，单页 publish/claim/result 链没有被放宽。聚合工作项已具备幂等发布、`SKIP LOCKED` 领取、租约续期、过期回收、attempt 审计及 result hash 一致性约束。Completion Gate 已把当前 `finalization_epoch` 的聚合父工作、候选终态数和运行中 attempt 与单页工作合并计数；Worker 收口会优先从当前终态 attempt 恢复父工作，再废弃悬挂 attempt，并把仍无结果的正式聚合工作失败关闭、转人工复核。

后续仍需：

1. 任务末发布器按规则选择快照创建 EvidenceInputSet 与聚合工作项；
2. 聚合 Consumer 从 Manifest 血缘读取事实并写入 attempt/work result；
3. 报告和 Task Insight 展示任务级规则结果，不把它投影成任意页面结果。

数据库迁移必须基于当时唯一的线性 Alembic head 单独提审；禁止制造并行 head，或把其他批次的未完成迁移夹带进任务级协议提交。

## 6. 四类需求映射

- Toast 样式统一：`scope=task`，成员覆盖任务候选快照中的全部相关 Capture；成员内保存瞬态 Toast 事实。
- 进度条时间检查：默认 `scope=journey`，成员绑定拖动前、动作与拖动后媒体证据。
- 广告有效性：`scope=journey`，成员绑定广告来源、Transition 与落地页 Capture。
- 多环境矩阵：`scope=environment_matrix`，成员按逻辑页面与环境身份组织，规则层只消费封存后的矩阵。

## 7. 当前仍不宣称完成的能力

- PostgreSQL 输入集合、聚合工作项、Completion Gate 与 Worker 失败收口已实现，但尚未接入任务末发布器和 Consumer；
- 尚未启用 Toast 任务级规则；
- 尚未实现多环境浏览器会话；
- 自动测试和 Alembic 离线 SQL 只证明仓库合同，不等同于真实 PostgreSQL 并发、真实 Worker 恢复或生产任务验收。
