---
name: interaction-rule
description: 页面交互行为质检 Skill。适用于 candidate_rules_tool 返回 category=interaction，且检查点涉及链接导航、按钮反馈、状态变化、弹窗或全屏、二维码与网址、账号入口等交互结果的场景；指导 Main Agent 使用 page_capture_tool、deterministic_assertion_tool、qr_link_validation_tool、rule_judge_tool 与结果写回 Tool，不内置规则编号或固定业务标准。
---

# interaction-rule

## 职责与适用场景

仅处理 `candidate_rules_tool` 返回的 `category=interaction` 候选规则，适合以下能力场景：

- 链接打开方式、页面跳转与返回后的页面状态。
- 按钮点击后的局部反馈、计数或状态变化、刷新后的状态保持。
- 弹窗、遮罩、全屏容器、媒体预览等单次交互结果。
- 二维码解码、网址可达性与展示地址一致性。
- 登录态下账号相关入口的存在性检查，但不执行会破坏会话的动作。

场景分类只帮助选择采证方式。具体交互对象、动作、预期结果、阈值、前置账号态与人工复核条件均来自候选规则的数据库字段。

广告交互交给 `ad-rule`；用户产生内容的风险判断交给 `input-risk-rule`；文案语义交给 `text-rule`。

## 动态规则输入

逐条读取候选规则的：

- `rule_id`、`rule_code`、`name`、`severity` 与适用页面信息。
- `check_point`、`steps`、`input_contract`、`judge_config`、`manual_review_policy`。
- `execution_route`、所需输入字段、证据类型和判定 Tool。

不得根据历史规则编号、Skill 示例或页面自述补充业务标准。数据库规则缺少动作、预期或证据契约时，按路由阻断或人工复核处理。

## 可用 Tool

| Tool | 交互质检用途 | 使用条件 |
| --- | --- | --- |
| `page_capture_tool` | 读取当前页面采集已有的基线、动作轨迹与动作后证据 | 当前版本页面探索不主动调用 `page_capture_tool` 补采，也不为规则执行 click/hover/scroll 或 `submit_probe` |
| `deterministic_assertion_tool` | DOM 存在性、文本、正则、数值变化等辅助断言 | 参数必须来自规则配置或结构化观测，不自造业务阈值 |
| `qr_link_validation_tool` | 解码二维码、校验 URL 可达性和跳转异常 | 仅当候选规则检查点涉及二维码或网址时；浏览器不得直接访问可疑地址 |
| `rule_judge_tool` | 执行单条已就绪的确定性规则 | 仅按 `execution_route` 调用 |
| `rule_judge_batch_tool` | 批量执行同一页面已就绪的确定性规则 | 同一批有多条规则时优先使用 |
| `record_rule_execution_tool` | 写回单条或补偿失败项 | 动态规则标识必须来自候选结果 |
| `record_rule_executions_batch_tool` | 批量写回同一页面多条结果 | 多规则页面优先使用 |

## 执行流程

1. 从 `execution_plan.batches` 取得 interaction 批次，逐条读取数据库规则定义。
2. 检查 `input_contract` 与前置条件。规则要求登录态但当前没有可用测试账号时，不执行注册或登录流程，按规则语义写 `skipped`、`manual_review` 或 `evidence_insufficient`。
3. 复用当前 `page_capture_id` 已有的交互前基线、动作后和刷新后采集；不得为补齐采集角色主动创建新动作。
4. 规则明确要求行为观察但现有 Action Trace 或对应角色缺失时，直接写 `evidence_insufficient`。未来恢复补采能力后仍只能执行单次最小触发。
5. 需要二维码或网址验证时调用 `qr_link_validation_tool`；需要确定性辅助判断时调用 `deterministic_assertion_tool`。
6. `execution_route.rule_engine_ready=true` 时使用 `rule_judge_tool` 或批量入口；`llm/hybrid` 只按 `check_point/judge_config` 对真实观测做结构化复核。
7. 置信度门禁和人工复核触发条件读取 `manual_review_policy`，不在 Skill 固定。
8. 使用单条或批量写回 Tool 记录结论、动作轨迹、判定输入输出和证据引用。

## 交互采证方法

- **链接与导航**：只检查已有 DOM 属性、当前路由和动作证据；静态证据不足时写 `evidence_insufficient`，不主动点击。
- **状态变化**：保留触发前基线、触发后状态和规则要求的刷新观测；Action Trace 只能证明动作发生，状态文本、数值变化或刷新后保持必须由对应角色的可信证据证明。
- **容器与全屏**：采集触发前后的 `ui_snapshot`、动作轨迹和可见状态；元素索引、命中框、原生全屏状态与媒体视口覆盖率只能从同一 Observation Packet 读取。
- **二维码与网址**：将二维码截图或 URL 交给专用 Tool；Tool 不可用时不得用模型猜测网络可达性。
- **账号入口**：只做候选规则要求的存在性观测；退出登录、删号、改密等破坏会话或账号状态的动作一律不执行。

## 滚动与瞬时提示

仅当候选规则在 `steps`、`input_contract`、`judge_config` 或 `execution_route` 中声明需要滚动覆盖、触底反馈、Toast、Snackbar、轻提示或瞬时状态证据时，才进入本节流程。未声明该能力的 interaction 规则不得额外滚动、不得延长等待，也不得把未捕获到提示直接判定为失败。

- **滚动证据**：仅消费已有 `page_coverage_summary` 和 `scroll_terminal` 终态角色；缺失时写 `evidence_insufficient`，不主动滚动补采。
- **触底反馈**：触底是否应该出现加载更多、没有更多、回到顶部或空状态提示，只能由候选规则的 `check_point/judge_config` 决定；Skill 不自行定义“列表必须有提示”。
- **瞬时提示**：Toast、Snackbar 等短时反馈必须使用规则声明的等待窗口或采集帧；只凭动作轨迹不能证明提示出现或未出现，缺少时序证据时写 `evidence_insufficient` 或按策略转人工复核。
- **状态收口**：滚动或瞬时提示验证完成后，写回时必须说明使用的是基线帧、动作后帧、终态帧还是时序帧；不得复用其他页面或其他动作的证据。

## 安全与状态边界

- 同一页面同一规则最多执行一次单次最小触发；证据复用优先于再次触发。
- 不提交订单、不支付、不下载、不授权、不注册账号，不输入敏感或违规内容。
- 触发造成点赞、关注、评论等数据变化时，不做第二次反向操作来“恢复”；在 `action_trace` 留痕。
- 评论类探针只能在专用测试账号和可清理区域使用；Action Trace 不保存探针原文，只保留长度与摘要。
- 页面无目标对象时按规则语义写 `skipped`；关键前后证据缺失时写 `evidence_insufficient`；模型拿不准时按数据库策略转人工复核。
- 写回的 `page_capture_id` 与附件必须来自同一次有效页面采集。

## 交接

- 上游：`rule-matching` 与 `candidate_rules_tool`。
- 下游：`evidence-report` 与规则结果写回 Tool。
- 公共执行与写回契约以 [rule-matching](../rule-matching/SKILL.md) 为准。
