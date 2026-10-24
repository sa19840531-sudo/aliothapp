# domain/review 人审域

人工审核是一等公民（不是事后补丁），由 V2+V3 阶段的 Review Agent 内建驱动。

## 核心实体

- `qc_review_item`：审核项，字段包含：
  - `trigger_reason`：触发原因，取值 `low_confidence`/`high_risk`/`evidence_insufficient`/`loop_budget_exceeded`/`no_progress`/`manual_flag`。
  - `status`：`pending`/`approved`/`rejected`/`needs_recheck`。
  - `reviewer`、`review_reason`。

## 职责

- 审核队列排序规则（严重级别 + 置信度，低置信/需复核项优先置顶）。
- 三态处理流转：确认 / 驳回 / 需复核（`needs_recheck` 触发该规则重新采集或重新判定）。
- 驳回原因与确认结果的沉淀，作为 V4 记忆引擎反馈记忆的输入。
- 审核通过问题的下游对接（禅道自动建 Bug/提单）。

## 关联

- 引用 [domain/evidence](../evidence/README.md) 的 `qc_rule_execution`。
- 输出反哺 `agents/memory`（V4 反馈记忆）与 [domain/integration](../integration/README.md)（禅道对接）。
