# domain/evidence 证据与判定域

质检证据的采集、组装与规则判定结果，是质检结论可追溯、可复现的核心。

## 核心实体

- `qc_evidence_artifact`（V2+V3）：证据产物，字段包含 `artifact_type`（screenshot/dom/ocr_text/har/console_log/media_state/ai_io）、`storage_path`、`source`（baseline/rule_supplement/ai_call）。
- `qc_observation_packet`（V2+V3）：把若干证据产物组装为规则判定的唯一输入，字段包含 `evidence_artifact_ids`、`quality_gate_status`、`quality_gate_reason`。
- `qc_rule_execution`（V2+V3，`qc_finding` 的演进版）：规则执行结果，`execution_status` 覆盖 `pass/fail/warning/manual_review/evidence_insufficient/skipped/error` 七态，字段包含 `confidence`、`review_status`、`review_reason`。
- `qc_finding`（V1 沿用）：V1 阶段仅记录命中项的轻量版本，字段包含 `hit`、`problem_desc`、`evidence`、`screenshot_url`、`review_status`。
- `EvidenceInputSet`（领域协议 `evidence-input-set.v1`）：封存 page/journey/task/environment_matrix 范围的不可变规则输入；显式绑定权威候选快照、规则合同、finalization epoch 与完整 Manifest 成员，不以任意页面冒充任务级锚点。持久化表与任务级工作项协议将在独立迁移批次落地。

## 职责

- 证据的两级分类存储路径与命名规范（页面层 baseline / 规则层 rules），详见[模块交互流程细化设计 3.1 节](../../../../../docs/design/2026-07-01-alioth-qc-workflow-detail.md)。
- Quality Gate 校验规则：证据完整性、清晰度、时效性、关联性、可追溯性、AI 可复现性。
- `qc_finding` → `qc_rule_execution` 的字段演进与历史数据迁移（迁移方案见整合方案设计第 9 节待确认事项）。

## 关联

- 引用 [domain/task](../task/README.md) 的 `qc_page_capture`。
- 引用 [domain/rule](../rule/README.md) 的 `qc_rule`。
- 被 [domain/review](../review/README.md) 引用（`qc_review_item` 关联 `qc_rule_execution`）。
