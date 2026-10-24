---
name: text-rule
description: 文案类规则判断 Skill。负责在 rule-matching 选出 qc_rule.category = text 的规则后，基于页面文本证据与数据库判定配置完成结构化判断，并按 execution_plan 写回结果。
---

# text-rule

## 职责

`text-rule` 回答"当前页面上的文案类规则应该如何判定"。它只定义判定规程、证据要求和结果写回口径，不直接访问页面、不直接查询规则、不直接写数据库。

适用范围固定为 `qc_rule.category = text`（候选规则中表现为 `category=text`）。如果规则不属于 `text`，应回到 rule-matching 重新路由，不得在本 Skill 中强行判定。

## 何时读取

负责判断的 Agent 在 rule-matching 输出 `execute_rules` 后，遇到任意 `category = text` 的规则时读取本 Skill。读取前应已经具备：

- 当前页面 URL 与 `page_type`。
- page-detection 输出的 `page_state`、`candidate_actions`。
- 页面操作或采集 Tool 返回的可见文本、DOM 文本、OCR 文本、截图引用。
- `candidate_rules_tool` 输出的待执行 text 规则清单。

## 输入

- 规则字段：`rule_id`、`rule_code`、`name`、`description`、`executor_type`、`severity`、`steps`、`input_contract`、`check_point`、`judge_config`、`manual_review_policy`、`execution_route`。
- 页面观测：页面 URL、页面标题、可见文本块、OCR 文本块、元素定位信息、截图或证据引用。
- 运行上下文：`task_id`、`agent_run_id` 只由 Tool 内部读取，模型不得自行编造。
- 上游结果：rule-matching 给出的 `match_reason`、page-detection 给出的 `page_type` 与 `page_state`。

## 可调用 Tool

### sensitive_word_detection_tool

用于处理违禁词、低俗词、监管敏感词、黑名单词类规则。输入应是页面可见文本与 OCR 文本的合并文本，输出命中词、位置、类别和白名单状态。

### brand_word_detection_tool

用于处理品牌标准写法、错拼、混用、竞品误用和别名归一类规则。仅当候选规则的 `input_contract` 或 `judge_config` 明确要求品牌词证据时调用。

### knowledge_retrieval_tool

用于处理需要业务口径、监管口径或知识库依据的文案语义规则。只把检索摘要写入 `judge_output`，不得把知识库长文本复制进规则结果。

### rule_judge_tool

当 `execution_route.rule_engine_ready=true` 且路由指定确定性 Handler 时调用。Handler 输入只能来自候选规则和同一次页面证据，不得由 Skill 维护规则编号、阈值或 Handler 映射。

### Capability Manifest 边界

- 当前 Agent Run 的 Capability Manifest 是 Tool 名称、可用状态和输入 Schema 的唯一事实源。只有 Manifest 返回的 Tool 才允许调用，Skill 不维护版本范围或上下线清单。
- 规则要求的 OCR、词库、品牌或知识证据无法由当前 Capability Manifest 提供时，优先复用页面采集已有证据；仍不满足 `input_contract` 时写回 `evidence_insufficient` 或 `manual_review`，不得编造 Tool 或结果。

### 规则结果写回 Tool

按 `execution_plan.write_tool_name` 写回 text 规则最终结果：单条规则使用 `record_rule_execution_tool`，同页多条规则使用 `record_rule_executions_batch_tool`。任何已进入本 Skill 的规则都必须写回一次，不允许只在最终文本中描述结论。

## 判定步骤

当候选规则返回 `execution_route.rule_engine_ready=true` 时，直接按路由指定 Handler 判定，不进入下方 LLM/hybrid 语义步骤。`collection_constraint` 的结构化集合只能逐项抄录同一次可信页面证据；`content_lifecycle_state` 必须先由独立 `source` 证明同一受控实体已失效，再检查 `primary/entry_N` 消费入口。缺少共同实体标识时写 `evidence_insufficient`，不得凭“页面上没看到”推断已隐藏。具体规则编号与 Handler 映射只以候选规则返回的 `execution_route` 为准，本通用 Skill 不维护第二份清单。

1. **归并文本证据**：合并可见文本、DOM 文本与 OCR 文本，保留来源、位置、截图引用。重复文本可以去重，但不得丢失来源引用。
2. **确认输入是否足够**：
   - 页面没有任何可用文案，且规则要求必须检查文案时，写回 `evidence_insufficient`。
   - 页面存在文案但缺少 OCR 或截图，且规则只依赖可见文本时，可以继续判定。
   - 规则要求图片内文字但 OCR 缺失时，写回 `evidence_insufficient`；当前版本页面探索不主动调用 `page_capture_tool` 补采，不得凭空推断。
3. **按 executor_type 分流**：
   - `rule_engine`：优先调用确定性 Tool；Tool 明确命中违规时写回 `fail`，明确无命中时写回 `pass`。
   - `llm`：基于文本证据和规则说明进行结构化判断，适用于错别字、语病、柔顺度、语义不一致等模糊规则。
   - `hybrid`：先调用 Capability Manifest 中真实可用的 Tool 获取证据，再基于证据做结构化复核；依赖未注册 Tool 时转人工复核或证据不足。
4. **按数据库判定条件执行**：以下能力仅在当前候选规则的 `check_point`、`input_contract` 或 `judge_config` 明确要求时执行，不得把场景示例扩展为独立规则。
   - 敏感词类：未命中或命中白名单为 `pass`；命中有效敏感词为 `fail`；命中疑似词且上下文不足为 `manual_review`。
   - 品牌词类：标准写法一致为 `pass`；错拼、混用、竞品误用为 `fail`；别名或缩写无法确认时为 `manual_review`。
   - 文案质量类：明显错别字、语病、标点滥用、大小写不一致可写 `warning` 或 `fail`，严重级别按规则 `severity` 与 `judge_config` 执行。
   - 语义合规类：与知识库口径一致为 `pass`；明确冲突为 `fail`；知识库证据不足或相似度不足为 `manual_review`。
5. **形成 evidence_refs**：每个结论必须列出支撑证据引用，至少包含文本片段、来源类型、页面 URL、截图或 OCR 引用中的一种。
6. **写回结果**：按 `execution_plan.write_tool_name` 选择单条或批量写回 Tool，写入执行状态、摘要、问题描述、证据引用、判定输入摘要和判定输出摘要。

## 输出格式（示意）

```json
{
  "rule_id": "${rule_id}",
  "rule_code": "${rule_code}",
  "execution_status": "fail",
  "judge_strategy": "hybrid",
  "confidence": 0.86,
  "severity": "high",
  "result_summary": "页面活动文案命中敏感词",
  "problem_desc": "页面文案包含非白名单敏感词",
  "page_capture_id": "capture-001",
  "evidence_artifact_ids": ["artifact-001"],
  "judge_input": {
    "page_type": "activity",
    "observation_summary": "活动页可见文本中出现敏感词示例",
    "text_source_count": 3
  },
  "judge_output": {
    "schema_version": "1.0",
    "conclusion": "fail",
    "reasoning_summary": "敏感词检测 Tool 命中非白名单词条",
    "tool_results": [
      {
        "tool_name": "sensitive_word_detection_tool",
        "status": "ok",
        "summary": "命中非白名单敏感词示例"
      }
    ],
    "hit_type": "sensitive_word_hit"
  }
}
```

`llm/hybrid` 的 `judge_output` 公共字段必须严格遵守上述结构；重载荷通过 `page_capture_id/evidence_artifact_ids` 引用，不得内嵌完整文本、DOM 或截图 Base64。

## 写回要求

- `pass`：证据充分，未发现规则问题。
- `fail`：证据充分，明确违反规则。
- `warning`：存在轻微文案质量问题，但未达到失败级别。
- `manual_review`：存在疑似问题或上下文不足，需要人工确认。
- `evidence_insufficient`：缺少完成判断所需文本、OCR、截图或知识库证据。
- `error`：Tool 调用异常、结构化输出异常或判定过程不可恢复。

写回时必须包含：

- `rule_id` 或 `rule_code`。
- `execution_status`。
- `result_summary`。
- `page_capture_id` 与 `evidence_artifact_ids`。
- `judge_input` 与 `judge_output` 摘要。

## 边界情况

- **候选文本为空**：不得直接判定为通过，应按规则需要写回 `evidence_insufficient` 或 `skipped`。
- **同一问题多处出现**：同一规则只写一条执行结果，证据中列出多个命中位置。
- **规则描述与 judge_config 冲突**：以 `judge_config` 为准，并在 `judge_output` 记录冲突说明。
- **敏感词命中白名单**：不作为失败依据，但应在 `judge_output` 中记录白名单命中。
- **高风险语义判断**：涉及监管、支付、隐私、未成年人等场景，无法确定时优先 `manual_review`。

## 与其他 Skill/Tool 的关系

- **上游依赖**：rule-matching、page-detection、页面操作或采集 Tool。
- **下游交接**：evidence-report 复用本 Skill 的证据引用和写回结果做报告聚合。
- **必须使用的 Tool**：`execution_plan.write_tool_name` 指定的单条或批量规则结果写回 Tool。
- **按需使用的 Tool**：仅限当前 Capability Manifest 返回且规则输入契约要求的 Tool。
- **不负责**：候选规则查询、页面跳转、截图采集、证据文件存储、任务级报告生成。
