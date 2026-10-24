---
name: player-rule
description: 播放器类规则判断 Skill。负责在 rule-matching 选出 qc_rule.category = player 的规则后，基于页面采集与数据库判定配置完成结构化判断，并按 execution_plan 写回结果。
---

# player-rule

## 职责

`player-rule` 回答"播放器类规则应该如何判定"。它只定义判定规程、证据要求和结果写回口径，不直接控制浏览器、不直接读取数据库、不直接写库。

适用范围固定为 `qc_rule.category = player`（候选规则中表现为 `category=player`）。如果规则不属于 `player`，应回到 rule-matching 重新路由，不得在本 Skill 中强行判定。

## 何时读取

负责判断的 Agent 在 rule-matching 输出 `execute_rules` 后，遇到任意 `category = player` 的规则时读取本 Skill。读取前应已经具备：

- 当前页面 URL 与 `page_type`。
- page-detection 输出的 `page_state`、`candidate_actions`。
- page-capture 返回的可见文本、DOM 摘要、截图或多帧截图、控制台日志、网络状态。
- `candidate_rules_tool` 输出的待执行 player 规则清单。
- 规则要求的播放入口、播放历史或贴片广告观测结果。

## 输入

- 规则字段：`rule_id`、`rule_code`、`name`、`description`、`executor_type`、`severity`、`steps`、`input_contract`、`check_point`、`judge_config`、`manual_review_policy`、`execution_route`。
- 页面观测：播放器区域截图、播放按钮状态、剧集/章节信息、当前播放时间、进度条位置、错误提示、贴片广告状态。
- 交互结果：进入播放页、播放/暂停、关闭贴片、等待贴片结束、刷新或再次进入后的页面状态。
- 上游结果：rule-matching 给出的 `match_reason`，page-detection 给出的 `page_type`、`page_state`、`candidate_actions`。

## 可调用 Tool

### page_capture_tool

仅用于读取当前页面采集已有的播放器区域、截图、可见文本、控制台日志和网络状态。当前版本页面探索不主动调用 `page_capture_tool` 补采，也不为规则触发播放、刷新或等待动作；缺少行为或多帧证据时写 `evidence_insufficient`。

### deterministic_assertion_tool

用于处理确定性判断，例如播放按钮是否存在、错误文案是否出现、进度数值是否变化、续播集数或时间点是否符合预期、贴片广告结束后是否进入正片。

### rule_judge_tool

当 `execution_route.rule_engine_ready=true` 且路由指定确定性 Handler 时调用。Handler 输入只能来自候选规则和同一次播放器证据，不得由 Skill 维护规则编号、阈值或 Handler 映射。

### 规则结果写回 Tool

按 `execution_plan.write_tool_name` 写回播放器规则最终结果：单条规则使用 `record_rule_execution_tool`，同页多条规则使用 `record_rule_executions_batch_tool`。任何进入本 Skill 的规则都必须写回一次，不允许只在最终文本中描述结论。

## 判定步骤

1. **确认数据库判定条件**：只执行当前候选规则 `check_point`、`input_contract` 和 `judge_config` 明确要求的播放器能力，不得把本 Skill 中的能力示例视为每条规则都必须执行的流程。
2. **确认播放器对象存在**：根据 DOM、可见文本、截图和候选动作确认当前页面是否存在播放器、播放入口、进度条或剧集入口。对象不存在时按规则语义写回 `skipped`、`evidence_insufficient` 或 `fail`。
3. **采集基线证据**：记录当前 URL、页面标题及 `input_contract` 要求的播放器证据。
4. **按需验证播放入口可用性**：仅当规则要求播放入口或播放状态时执行。
   - 播放按钮可见且单次触发后进入播放态，判定为 `pass`。
   - 播放按钮缺失、不可点击、触发后无变化或出现错误提示，判定为 `fail`。
   - 需要登录、付费或授权才能继续时，不继续执行，写回 `manual_review` 或 `skipped`。
5. **按需验证播放进度恢复**：仅当规则要求历史进度恢复时执行。
   - 存在历史进度证据时，再次进入播放页应恢复到最近观看集数、章节或时间点。
   - 无历史记录或新用户场景，应从第一集、第一章或起始时间播放。
   - 恢复到错误集数、错误章节、明显回退或丢失进度时，判定为 `fail`。
6. **按需验证续播行为**：仅当规则要求续播时执行。
   - 连续内容播放完成后，应进入下一集、下一章或推荐的连续内容。
   - 续播卡死、重复播放同一内容、停在空白页或错误页时，判定为 `fail`。
   - 证据不足以确认是否已播放完成时，写回 `evidence_insufficient` 或 `manual_review`。
7. **按需验证贴片广告后正片播放**：仅当规则要求贴片与正片衔接时执行。
   - 规则涉及贴片广告时，先采集贴片广告状态，再观察贴片结束或关闭后的播放状态。
   - 贴片广告结束后正常进入正片，判定为 `pass`。
   - 贴片广告失败、结束后白屏、遮罩残留、无法进入正片，判定为 `fail`。
8. **形成 evidence_refs**：每个结论必须包含支撑证据引用，至少覆盖截图、多帧截图、播放状态、进度文本、URL、控制台日志或网络状态中的一种。
9. **写回结果**：按 `execution_plan.write_tool_name` 选择单条或批量写回 Tool，写入执行状态、摘要、问题描述、证据引用、判定输入摘要和判定输出摘要。

## 进度条

仅当候选规则在 `steps`、`input_contract`、`judge_config` 或 `execution_route` 中声明需要验证播放器进度条、播放时间展示、拖动后状态或媒体真实时间时，才进入本节流程。未声明进度条能力的 player 规则不得主动拖动进度条、不得等待播放进度变化，也不得用静态截图推断播放行为通过。

- **只读进度证据**：优先读取同一次 `page_capture_tool` 返回的媒体状态、可见时间文本、进度条元素和截图引用；若采集端未提供媒体 `currentTime/duration` 等字段且规则强依赖，写 `evidence_insufficient`。
- **拖动旅程**：只有规则步骤明确声明可执行拖动或跳转动作时，才通过受控页面操作执行一次最小动作；动作前后必须分别绑定真实 `page_capture_id` 和 artifact，不能反复拖动寻找可通过结果。
- **时间一致性**：页面展示时间、进度条百分比和媒体真实时间之间的允许误差只读取 `judge_config`；Skill 不固定秒级容差，也不自行定义“播放中”的阈值。
- **失败收口**：播放器对象不存在、媒体状态不可读、自动播放被浏览器策略拦截或需要登录/付费时，按 `manual_review_policy` 写 `skipped`、`manual_review` 或 `evidence_insufficient`，不得误判业务失败。

## 输出格式（示意）

```json
{
  "rule_id": "${rule_id}",
  "rule_code": "${rule_code}",
  "execution_status": "fail",
  "judge_strategy": "hybrid",
  "confidence": 0.84,
  "severity": "high",
  "result_summary": "播放历史进度未恢复",
  "problem_desc": "用户再次进入视频播放页后未定位到最近观看进度，页面从第一集起播",
  "page_capture_id": "capture-001",
  "evidence_artifact_ids": ["artifact-001", "artifact-002"],
  "judge_input": {
    "page_type": "video_detail",
    "observation_summary": "再次进入播放页后显示第 1 集 00:00",
    "expected_progress": "episode_3_00:12:30"
  },
  "judge_output": {
    "schema_version": "1.0",
    "conclusion": "fail",
    "reasoning_summary": "进度比较结果与多帧观测均表明历史进度未恢复",
    "tool_results": [
      {
        "tool_name": "deterministic_assertion_tool",
        "status": "ok",
        "summary": "实际播放进度与预期历史进度不一致"
      }
    ],
    "hit_type": "playback_progress_not_restored",
    "actual_progress": "episode_1_00:00:00"
  }
}
```

`llm/hybrid` 的 `judge_output` 公共字段必须严格遵守上述结构；重载荷通过 `page_capture_id/evidence_artifact_ids` 引用，不得内嵌完整文本、DOM 或截图 Base64。

## 写回要求

- `pass`：证据充分，播放器行为符合规则要求。
- `fail`：证据充分，明确存在播放入口、续播、播放进度或贴片后正片播放问题。
- `warning`：存在轻微体验问题，但未达到失败级别。
- `manual_review`：涉及登录、付费、授权、账号历史或上下文不足，需要人工确认。
- `evidence_insufficient`：缺少多帧截图、播放状态、历史进度或关键页面证据。
- `skipped`：当前页面没有规则要求的播放器对象，或任务配置不允许执行该播放路径。
- `error`：Tool 调用异常、页面操作异常或结构化输出异常。

写回时必须包含：

- `rule_id` 或 `rule_code`。
- `execution_status`。
- `result_summary`。
- `page_capture_id` 与 `evidence_artifact_ids`。
- `judge_input` 与 `judge_output` 摘要。

## 边界情况

- **需要账号历史**：没有可用测试账号或没有历史进度时，不伪造历史，写回 `manual_review` 或 `evidence_insufficient`。
- **需要付费播放**：不执行真实支付、不购买会员、不提交订单；只采集可见提示和规则允许范围内的入口状态。
- **自动播放被浏览器策略拦截**：记录浏览器策略提示，优先写 `manual_review` 或 `evidence_insufficient`，不得误判业务失败。
- **贴片广告涉及外链或下载**：不点击下载、不跳外部高风险链路；只观测贴片结束、关闭、失败兜底和正片恢复。
- **多帧观测超时**：按规则 `judge_config` 或 page-capture 默认等待策略结束，不无限等待。

## 与其他 Skill/Tool 的关系

- **上游依赖**：rule-matching、page-detection、page-exploration。
- **下游交接**：evidence-report 复用本 Skill 的证据引用和写回结果做报告聚合。
- **必须使用的 Tool**：`execution_plan.write_tool_name` 指定的 `record_rule_execution_tool` 或 `record_rule_executions_batch_tool`。
- **按需使用的 Tool**：page_capture_tool、deterministic_assertion_tool。
- **不负责**：候选规则查询、浏览器会话管理、证据文件存储、任务级报告生成、人工审核流程。
