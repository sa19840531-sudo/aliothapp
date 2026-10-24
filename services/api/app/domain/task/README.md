# domain/task 任务与执行域

质检任务的生命周期与 Agent 执行留痕。

## 核心实体

- `qc_task`：质检任务。字段包含 `target_url`、`ruleset_id`、`channel`（workbench/tg）、`status`（queued/running/review/done/failed）。
- `qc_page_capture`：页面采集记录（一级分类：页面基线快照），字段包含 `page_seq`、`page_slug`、`url`、DOM/文本/截图的对象存储路径、`action_log`、`status`。
- `qc_agent_run`（V2+V3）：Agent 运行实例，字段包含 `agent_type`（planner/explorer/execution/evidence/rule_judge/review/report）、`parent_agent_run_id`（支持嵌套）、`loop_guard`（护栏配置）、`status`。
- `qc_agent_action_trace`（V2+V3）：Agent 单步执行留痕，字段包含 `step_seq`、`thought_summary`、`selected_capability`、`capability_kind`、`capability_input`/`capability_output`、`memory_delta`、`cost`、`status`。

## 职责

- 任务状态机流转（queued → running → review → done/failed）。
- 页面采集顺序规划与两级分类命名规则（业务命名 `page_slug`）。
- Agent 执行留痕的写入与查询，支撑审计回放（呼应 [AGENTS.md](../../../../../AGENTS.md) "审计链路完整"）。

## 关联

- 引用 [domain/rule](../rule/README.md) 的 `qc_ruleset`。
- 被 [domain/evidence](../evidence/README.md) 引用（`qc_evidence_artifact` 关联 `qc_page_capture`）。
- 是 `agents/orchestrator` 与 `agents/runtime` 的主要数据落点（Agent Loop 的执行状态不放在模型记忆里，必须落库到本域）。
