---
name: rule-matching
description: 当前页面候选规则匹配 Skill。仅当 page_detection_tool 返回 detection_status=recognized、rule_matching_allowed=true 且 page_type 为可匹配规则的 canonical code 时读取；随后调用 candidate_rules_tool 查询当前任务锁定规则集下的候选规则。
---

# rule-matching

## 职责

`rule-matching` 回答"当前页面本轮应该跑哪些规则"。它本身不做规则判定、不写数据库、不访问页面，只负责把页面识别结果转成规则执行清单。

## 何时读取

Main Agent 仅在 `page_detection_tool(page_capture_id)` 返回 `detection_status=recognized`、`rule_matching_allowed=true` 且 `page_type` 属于 Catalog 中可匹配规则的页面类型后读取本 Skill。`ambiguous`、`insufficient`、`error` 或 `line_loading` 等 `rule_matching_allowed=false` 的识别结果均禁止进入本步骤。进入任何具体规则 Skill 前，必须先完成本步骤。

## 输入

- 当前页面 URL。
- `page_capture_tool` 返回的当前页面 `page_capture_id`。
- `page_detection_tool` 输出的 `detection_status`、`rule_matching_allowed`、`page_type`、`page_state`、`candidate_actions`、`page_capture_id` 与 `evidence_artifact_ids`；该输出是唯一页面识别结论，不得由 Main Agent 补齐或改写。
- 系统注入的 Agent Run 上下文：`task_id`、`agent_run_id`。这些字段只由 Tool 使用，模型不得自行编造。
- 任务创建时锁定的规则集和渠道。实际查询由 `candidate_rules_tool` 通过 `task_id` 从后端服务获取。

## 必须调用的 Tool

### candidate_rules_tool

用途：查询当前 Agent Run 绑定任务在指定页面类型下的候选规则。

调用方式：

```json
{
  "page_type": "home",
  "page_capture_id": "d8d417ef-5118-4f0d-b1ce-a79440f9ad06"
}
```

注意：

- 不传 `task_id`。
- 不传 `ruleset_id`。
- 不直接调用 HTTP API。
- `page_type` 必填，且只能使用 page-detection 从 Catalog 得到的标准代码。
- `page_capture_id` 必填，且必须原样使用当前 Run 中 `page_capture_tool` 返回的页面采集 ID，不得编造或复用其他页面的 ID。
- `task_id`、`ruleset_id`、`channel` 由 Tool 内部通过运行上下文和后端服务获取。

## 处理步骤

1. **确认页面识别门禁**：同时检查 `detection_status=recognized`、`rule_matching_allowed=true` 且 `page_type` 为 Catalog 中可匹配规则的 canonical code。任一条件不满足都立即停止，不调用 `candidate_rules_tool`，也不得由 Main Agent 猜测、补齐或使用 `global` 等伪类型兜底。
2. **查询候选规则**：调用 `candidate_rules_tool(page_type, page_capture_id)`，获取当前任务锁定规则集下的候选规则列表和 `execution_plan`；Tool 返回的 `working_memory.current_page` 是本轮页面进度基线，`execution_plan.batches` 是本页唯一的批量执行顺序。
   - 每条候选规则的 `description`、`input_contract`、`check_point`、`judge_config`、`manual_review_policy` 和 `execution_route` 均来自任务锁定规则集，是规则判定与执行路由的唯一事实源。
   - 不得使用本 Skill 的示例、历史规则编号或模型记忆补齐、覆盖上述字段。
3. **按页面状态二次过滤**：
   - 当前页面明显不存在对应对象时，标记为 `skipped`，例如页面没有弹窗则弹窗关闭类规则跳过。
   - 页面状态不确定时优先保留规则，交给具体规则 Skill 判定，避免在匹配阶段误删。
   - 涉及登录、支付、提交订单等高风险动作时，不执行危险动作，转为 `skipped` 或 `manual_review`。
4. **按 category 路由**：

   | category | 后续处理 |
   | --- | --- |
   | `text` | 进入 text-rule |
   | `visual` | 进入 visual-rule |
   | `interaction` | 进入 interaction-rule |
   | `ad` | 进入 ad-rule（含弹窗、启动、遮罩、贴片等全部广告场景） |
   | `payment` | 进入 payment-rule（高风险入口，全程只读、不执行支付动作） |
   | `input_risk` | 进入 input-risk-rule |
   | `player` | 进入 player-rule |
   | `compliance` | 直接调用确定性检测 Tool 或进入人工复核 |

5. **确认执行路由**：逐条读取候选规则的 `execution_route`。只有 `execution_route.rule_engine_ready=true` 且 `tool_name=rule_judge_tool` 时才允许执行确定性规则；`ready=false` 时不得猜测执行，必须记录阻断原因并转 `manual_review` 或配置整改。
6. **按执行计划分批**：严格按 `execution_plan.batches` 顺序处理。候选规则超过一条时，`rule_engine` 批次使用 `rule_judge_batch_tool`；`llm/hybrid` 按 batch 的 `skill_name` 一次加载对应类别 Skill，并在同一次结构化推理中完成该批规则，不得为同一类别的每条规则重复加载 Skill。
7. **批量写回**：同一页面存在多条候选规则时使用 `record_rule_executions_batch_tool`，共享当前 `page_capture_id`；单条候选或批次中个别失败条目的补偿重试才使用 `record_rule_execution_tool`。批量 Tool 返回 `partial_success` 时只重试 `status=blocked` 的条目，禁止重跑已成功判定或写回的规则。
8. **输出执行清单**：把规则分成 `execute_rules`、`skipped_rules`、`deferred_rules` 三类，并原样保留 `execution_route` 和批次归属。

## 输出格式（示意）

```json
{
  "page_url": "https://example.com/activity",
  "page_type": "home",
  "execute_rules": [
    {
      "rule_id": "rule-001",
      "rule_code": "${rule_code}",
      "category": "ad",
      "skill": "ad-rule",
      "match_reason": "applicable_page_types",
      "execution_route": {
        "strategy": "hybrid",
        "ready": true,
        "rule_engine_ready": false,
        "tool_name": null,
        "handler": null,
        "required_input_fields": []
      }
    }
  ],
  "skipped_rules": [
    {
      "rule_id": "rule-002",
      "rule_code": "${skipped_rule_code}",
      "reason": "当前页面未发现对应页面元素"
    }
  ],
  "deferred_rules": []
}
```

字段命名以示意为准，最终写库字段以 `qc_rule_execution` 为准。

## 写回要求

- 匹配后真正执行的规则，必须由具体规则 Skill 或 evidence-report 调用 `record_rule_execution_tool` 写入 `pass`、`fail`、`warning`、`manual_review`、`evidence_insufficient` 或 `error`。
- 匹配阶段已确定不适用的规则，也必须调用 `record_rule_execution_tool` 写入 `skipped`，并给出明确原因。
- 同一页面有多条待写回规则时，上述两类结果统一放入 `record_rule_executions_batch_tool`；单条 Tool 只用于单规则页面或失败条目补偿。
- 不允许只在 Agent 最终文本中描述结果而不写入 `qc_rule_execution`。
- Worker 会在运行收尾校验候选规则写回完整性；已匹配但未写回的规则将被补记为 `evidence_insufficient` 并进入人工复核，不得依赖该兜底替代正常判定。
- 写回字段以 `qc_rule_execution` 落库 Schema 为准，单条结论示意：

  ```json
  {
    "rule_code": "${rule_code}",
    "execution_status": "fail",
    "page_capture_id": "d8d417ef-5118-4f0d-b1ce-a79440f9ad06",
    "judge_strategy": "rule_engine",
    "confidence": 0.93,
    "problem_desc": "单次点击弹窗广告后弹窗未关闭，蒙层持续阻挡首页操作",
    "evidence_artifact_ids": ["4c20e481-4c27-48af-b639-d73c26ab0b2f"]
  }
  ```

## 规则 Skill 公共执行契约

以下契约对所有 category 规则 Skill 生效；各规则 Skill 只写本类特有口径，不再重复本节。

- **判定路径按 `execution_route`**：候选规则返回值是执行路由的唯一事实源。只有 `rule_engine_ready=true` 时才调用返回的 `tool_name=rule_judge_tool`，并按 `handler`、`handler_tool_name`、`judge_input_source`、`required_input_fields` 和 `required_artifact_types` 准备输入；不得仅凭 `executor_type=rule_engine` 推断可执行。`judge_input_source=trusted_evidence` 时，模型不得总结、改写或补充对应字段，只传真实 `page_capture_id/evidence_artifact_ids`，由后端读取原文。`llm/hybrid` 按对应 category Skill 采证和结构化复核，judge 输入输出随写回留痕。
- **统一写回映射**：`rule_judge_tool` 返回的 `rule_id`、`rule_code`、`execution_status`、`page_capture_id`、`judge_strategy`、`confidence`、`result_summary`、`problem_desc`、`evidence_artifact_ids`、`judge_input`、`judge_output` 可直接映射到 `record_rule_execution_tool`。写回服务会根据任务锁定规则自动补齐 `judge_strategy`，若模型声明的策略与 `executor_type` 不一致则拒绝落库。
- **批量映射**：`rule_judge_batch_tool` 的每个 `status=ok` 条目沿用同一映射，去掉共享的 `page_capture_id` 后放入 `record_rule_executions_batch_tool.executions`；批量判定或写回的成功条目不得再次执行，只处理失败条目。
- **llm/hybrid 输出契约**：写入 `pass`/`fail`/`warning`/`manual_review` 时，`judge_input` 必须是非空结构化摘要，`judge_output` 必须包含 `schema_version: "1.0"`、与 `execution_status` 一致的 `conclusion`、非空 `reasoning_summary`；`hybrid` 还必须包含非空 `tool_results` 数组。两类策略都必须提供 `confidence`，低于任务锁定规则 `min_confidence` 时服务端自动把原结论转为 `manual_review`，并在 `proposed_conclusion` 留存原结论。
- **模型上下文载荷边界**：`llm/hybrid` 的 `judge_input/judge_output` 只保存结构化摘要和 Tool 结果摘要，单个对象不得超过 16 KiB，禁止写入完整 DOM、HTML、页面源码或截图 Base64；重载荷只通过 `page_capture_id/evidence_artifact_ids` 引用。
- **B 模式触发预算**：优先静态采集不触发；确需行为观察时才**单次最小触发**（一次点击 / 一次操作 / 一次故障注入）并在触发后立即观察，时序证据用多帧定时采集；同页同规则触发 / 注入 ≤ 1 次，证据复用优先于新触发；绝不跑完整业务流程、不造数据、不反复操作。
- **结论阶梯（不硬判）**：页面无判定对象 → `skipped`；证据不满足该规则 `input_contract.quality_gate` → `evidence_insufficient`，当前版本不得主动补采；模型不确定或置信度低于该规则 `manual_review_policy.min_confidence` → `manual_review`；命中判断标准反例 → `fail`；全部达标 → `pass`。`skipped` 也必须写回，不静默丢弃。
- **页面与证据绑定**：全部执行状态都必须传入同一次 `page_capture_tool` 返回的 `page_capture_id`，确保页面进度可以按候选规则收敛；`pass`/`fail`/`warning`/`manual_review` 还必须传入真实 `artifact_ids`。不同页面执行同一规则分别写回，同一页面同一规则重复写回按幂等更新处理。
- **规则关系以候选结果为准**：规则合并、停用或替代关系必须由任务锁定规则集和候选结果表达。候选清单中的每个 `rule_id` 均独立写回，Skill 不维护历史合并清单。
- **Tool 能力以 Manifest 为准**：只调用当前 Capability Manifest 返回且 `execution_route` 明确允许的 Tool，并严格遵守其输入 Schema。Skill 不维护 Tool 已上线能力清单。

## 边界情况

- **候选规则为空**：当前页面不执行规则，回到 page-exploration 选择下一个页面。
- **执行路由阻断**：`execution_route.ready=false` 时禁止调用 `rule_judge_tool`；记录 `blocked_reason.code/message`，把该规则转人工复核，并通知规则负责人补齐 Handler 或调整 `executor_type`。
- **可信证据阻断**：`rule_judge_tool` 返回 `TRUSTED_EVIDENCE_INVALID` 时，证据缺件直接写 `evidence_insufficient`；字段类型、参数范围、Handler 契约或同一证据内容无效均为不可重试终态。当前版本页面探索不主动调用 `page_capture_tool` 补采，也不发布 Evidence Request 或 Probe。禁止把“未见某错误词”等自然语言总结作为确定性扫描输入。
- **页面识别门禁未通过**：当前版本 `ambiguous`/`insufficient` 交回 workflow 按 `evidence_insufficient` 收口，不主动补采；`error` 记录失败并收敛或转人工复核。三种状态都不调用候选规则 Tool。
- **重复页面**：已完成同一 URL、同一规则的页面，不重复判定。
- **高风险动作**：涉及登录、支付、提交订单等动作时，不执行真实动作；规则写入 `skipped` 或 `manual_review`。

## 与其他 Skill/Tool 的关系

- **上游依赖**：page-detection 与通过门禁的 `page_detection_tool` 结构化结果。
- **下游交接**：各 category 规则 Skill 与 evidence-report。
- **必须使用的 Tool**：`candidate_rules_tool`。
- **结果写回 Tool**：`record_rule_executions_batch_tool`（多规则优先）与 `record_rule_execution_tool`（单条或失败补偿）。
- **不负责**：具体规则判定、页面操作、证据存储、任务级报告聚合。
