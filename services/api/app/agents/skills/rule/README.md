# agents/skills/rule 规则 Skill

指导 Agent 处理不同类别的质检规则，主体按 `qc_rule.category` 对应；复杂场景可在同一 category 下拆分更具体的规则 Skill。遵循 [Agent Skills 规范](https://agentskills.io)。

各 category Skill 只维护场景识别、Tool 选择、稳定证据边界和执行流程，具体业务规则由 `candidate_rules_tool` 从数据库动态返回。规则编号、Handler 映射、输入字段、证据类型、采集角色和阈值只以候选规则的 `execution_route/input_contract/judge_config` 为准。

## 规划的 Skill 清单

| Skill | 关联 category | 常用 Tool |
| --- | --- | --- |
| rule-matching | 全部（决定当前页面要跑哪些规则） | candidate_rules_tool |
| text-rule | text 文案 | 敏感词检测、品牌词检测、知识库检索 |
| visual-rule | visual 视觉 | page_capture_tool（多帧截图）、deterministic_assertion_tool、record_rule_execution_tool |
| interaction-rule | interaction 交互 | page_capture_tool、deterministic_assertion_tool、qr_link_validation_tool、record_rule_execution_tool |
| ad-rule | ad 广告（全部场景，2026-07-15 并入原 ad-popup-rule） | page_capture_tool（单次触发/故障注入/多帧）、deterministic_assertion_tool、record_rule_execution_tool |
| payment-rule | payment 支付 | page_capture_tool（只读采集）、deterministic_assertion_tool、record_rule_execution_tool |
| input-risk-rule | input_risk 用户输入风险 | sensitive_word_detection_tool、qr_link_validation_tool、deterministic_assertion_tool、record_rule_execution_tool |
| player-rule | player 播放器 | 页面操作、截图比对 |

详见[最终整合方案设计 4.4 节](../../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)（含 3.1 节 category 细化；历史文档中称为"规则 Playbook"/"工具型 Skill"，术语沿革见 [docs/专项能力与高频回归规则.md](../../../../../../docs/专项能力与高频回归规则.md)）。

## 说明

- 敏感词/品牌词检测不是独立分类，而是被 `text`/`input_risk`/`compliance` 等规则 Skill 共同调用的 Tool（能力复用，不重复建模）。
- 本次修改范围内的场景清单只用于触发与 Tool 选择，不写成数据库规则清单或固定判定表。
- V2+V3 阶段调用者变为 Rule Judge Agent，Skill 内容本身不变。

## V1 实现（2026-07，2026-07-05 术语更正）

[visual-rule/SKILL.md](visual-rule/SKILL.md)、[interaction-rule/SKILL.md](interaction-rule/SKILL.md)、[ad-rule/SKILL.md](ad-rule/SKILL.md)、[payment-rule/SKILL.md](payment-rule/SKILL.md)、[input-risk-rule/SKILL.md](input-risk-rule/SKILL.md) 从 `candidate_rules_tool` 读取当前数据库规则定义，按 `execution_route` 选择统一判定 Tool，并通过单条或批量写回 Tool 落入 `qc_rule_execution`。`rule-matching`、`text-rule` 与 `player-rule` 同样不得成为规则编号或固定阈值的第二事实源。
