# WorkingMemory 页面质检记忆设计

## 1. 需求结论

需求合理，但不能只增加 `current_page_type` 与 `visited_pages` 两个简单字段，否则会产生四类错误：

1. 同一页面类型的多个页面实例被合并。
2. 同一 URL 的动态页面状态被误认为已经完成。
3. 页面只执行了部分候选规则，却提前进入“已质检”。
4. 内存状态被误当作业务结论主表，破坏跨 Run 审计和人工复核。

因此，页面记忆以 `page_capture_id` 为唯一关联键，以候选规则的完整写回作为页面完成条件，并明确区分单次 Run 进度索引与数据库事实源。

## 2. 领域边界

| 层级 | 职责 | 数据来源 |
| --- | --- | --- |
| WorkingMemory | 单次 Agent Run 内的当前页、待恢复页、已质检页和紧凑结果索引 | Tool 的结构化输入输出 |
| `qc_page_capture` | 页面采集快照、页面序号与证据归属 | `page_capture_tool` 持久化链路 |
| `qc_rule_execution` | 每条规则的正式执行状态、证据与人工审核状态 | `record_rule_execution_tool` 持久化链路 |
| `qc_agent_run.working_memory` | Run 终态的工作记忆审计快照 | Worker 收尾写入 |

WorkingMemory 不写业务表，不从模型最终文本推断页面状态，也不承担跨 Run 恢复的事实源职责。

## 3. 页面状态模型

一次页面质检按以下结构化事件推进：

1. `page_capture_tool` 成功并返回 `page_capture_id`：建立 `current_page`，状态为 `captured`。
2. `candidate_rules_tool(page_type, page_capture_id)` 成功：建立候选规则清单；有候选规则时状态为 `inspecting`，无候选规则时直接完成并标记 `no_applicable_rules`。
3. `record_rule_execution_tool` 成功：按 `page_capture_id + rule_id/rule_code` 更新页面规则结果；为保证所有候选状态都能回收到页面，Agent Tool 契约要求七种执行状态均传入 `page_capture_id`。
4. 所有候选规则均存在结构化结果：页面从 `current_page` 移入 `inspected_pages`。
5. 当前页面未完成就切换到新的采集：旧页面保留在 `pending_pages`，不得静默丢失。
6. `ambiguous` / `insufficient` 的旧采集被补采产生的新 `page_capture_id` 替代后，旧采集进入 `superseded` 审计状态，不再参与 Run 完成门禁；主动放弃的采集使用 `abandoned`。

候选规则匹配必须引用当前 Run 的真实 `page_capture_id`；页面规则写回不得跳过候选规则匹配阶段。

## 4. 页面结果聚合

活跃页面保留每条规则的 `execution_id`、规则标识、执行状态、摘要、问题描述、置信度、严重级别和证据 ID。页面级 `overall_status` 仅用于进度和展示：

- 候选规则未全部写回：`in_progress`。
- 无候选规则：`no_applicable_rules`。
- 存在 `manual_review` / `evidence_insufficient` / `error`：`needs_review`。
- 否则存在 `fail`：`fail`。
- 否则存在 `warning`：`warning`。
- 全部跳过：`skipped`。
- 其余已完成场景：`pass`。

该聚合值不替代任务报告和规则执行主表中的正式状态。

## 5. 模型可见性与审计

页面采集、候选规则、规则写回和报告生成 Tool 的成功返回会附加紧凑 `working_memory`：包含当前页、待恢复页数量、已质检页及规则状态摘要。模型据此决定继续当前页还是进入下一页，不需要重新扫描完整动作历史。

Worker 结束时把完整 `audit_snapshot` 写入 Redis 的 `AgentRunRecord.working_memory`，并由状态 Store 写入 `qc_agent_run.working_memory`；同步调试接口和异步 Run 查询接口也返回该字段。

## 6. 当前不覆盖的范围

- 运行中每一步把 WorkingMemory 增量写入 Redis / PostgreSQL。
- Worker 崩溃后从数据库恢复同一个 Run。
- 新 Run 启动时从历史 `qc_page_capture` / `qc_rule_execution` 自动重建页面队列。
- 多 Agent 之间共享页面进度。

上述能力需要独立的 Checkpoint 与恢复协议，不能通过继续扩张单进程可变对象隐式实现。

## 7. 兼容性

- 不新增数据库迁移：`qc_agent_run.working_memory` 已是 JSONB。
- 不要求前端同步菜单或路由。
- `candidate_rules_tool` 增加必填 `page_capture_id`，`record_rule_execution_tool` 对全部页面执行状态要求必填 `page_capture_id`；两者都属于 Agent 内部 Tool 契约变更，必须同步 qc-workflow、rule-matching、Tool 文档和 Schema 回归测试。
