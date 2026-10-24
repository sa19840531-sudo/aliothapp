---
name: payment-rule
description: 支付与价格展示质检 Skill。适用于 candidate_rules_tool 返回 category=payment，且检查点涉及价格展示、优惠关系、货币或单位归一、会员或账号态价格、收银台只读信息等场景；指导 Main Agent 使用 page_capture_tool、deterministic_assertion_tool、rule_judge_tool 与结果写回 Tool，并强制保持只读，不内置规则编号或固定价格标准。
---

# payment-rule

## 职责与适用场景

仅处理 `candidate_rules_tool` 返回的 `category=payment` 候选规则，适合：

- 同一商品、服务或权益在页面不同位置的价格展示核对。
- 原价、优惠价、会员价、折扣、币种和计量单位之间的确定性比较。
- 价格随账号态、地区、渠道或时间变化时的同条件证据核对。
- 支付页、收银台或购买入口中无需提交交易即可观察的信息。

实际比较对象、价格关系、归一化方式、容差、账号前置、严重度和复核策略全部来自数据库候选规则。本 Skill 不保存任何商品规则、编号或价格阈值。

广告内价格素材交给 `ad-rule`；价格文案语义交给 `text-rule`；按钮反馈交给 `interaction-rule`。

## 最高安全边界

支付类质检全程只读、零交易触发：

- 绝不提交订单、确认支付、绑卡、充值、购买会员或输入支付凭证。
- 不点击会创建订单、锁定库存、发起扣款或改变账户权益的控件。
- 不为取得下游价格而推进真实交易流程。
- 若规则证据只能通过上述动作获得，按动态规则语义写 `skipped`、`manual_review` 或 `evidence_insufficient`，并说明安全阻断原因。

## 动态规则输入

逐条读取：

- `rule_id`、`rule_code`、`name`、`severity` 与适用页面信息。
- `check_point`、`steps`、`input_contract`、`judge_config`、`manual_review_policy`。
- `execution_route`、所需输入字段、证据类型和确定性判定 Tool。

数据库字段是唯一业务标准。规则缺少价格关系、单位处理或证据契约时，不得按常识补齐。

## 可用 Tool

| Tool | 支付质检用途 | 使用条件 |
| --- | --- | --- |
| `page_capture_tool` | 读取当前页面采集已有的 DOM、可见价格文本、截图与动作轨迹 | 当前版本页面探索不主动调用 `page_capture_tool` 补采；本 Skill 不推进支付链路 |
| `deterministic_assertion_tool` | `number_compare` 比较归一化数值，或使用文本、正则、DOM 存在性辅助断言 | 比较操作符、容差和期望关系必须来自 `judge_config/check_point` |
| `rule_judge_tool` | 执行单条已就绪的数据库确定性价格规则 | 仅按 `execution_route` 调用 |
| `rule_judge_batch_tool` | 批量执行同一页面已就绪的确定性支付规则 | 多条候选时优先使用 |
| `record_rule_execution_tool` | 写回单条或补偿失败项 | 使用候选规则标识，不自造严重度 |
| `record_rule_executions_batch_tool` | 批量写回同一页面多条支付结果 | 多规则页面优先使用 |

## 执行流程

1. 从 `execution_plan.batches` 取得 payment 批次，读取候选规则全部动态字段。
2. 检查账号态、页面态与 `input_contract`。账号或资格条件不同的价格不得直接混比；无法建立同条件证据时不硬判。
3. 只使用当前页面或流程中已安全取得的只读采集。若确定性 Handler 需要多个展示位置，必须按 `input_contract.required_capture_roles` 绑定 `primary/location_N` 等真实采集；确认商品身份、账号态、渠道、币种和采集时段可比，禁止把多个页面的文本拼成模型摘要代替可信证据。
4. 按 `judge_config` 对价格文本进行结构化提取和归一化。币种、单位、区间、起价等语义无法可靠归一时，按数据库复核策略收口。
5. `execution_route.rule_engine_ready=true` 时调用 `rule_judge_tool` 或批量入口；`hybrid` 可先调用 `deterministic_assertion_tool` 形成数值比较结果，再做结构化复核。
6. 置信度门禁与高风险人工复核条件读取 `manual_review_policy`，禁止使用 Skill 固定值。
7. 使用写回 Tool 保存规则原始身份、比较输入、Tool 输出摘要、结论和证据引用。

## 价格采证方法

- 保留价格值、货币、单位、展示位置、商品身份、账号态、渠道和采集时间等结构化上下文；跨位置价格关系由 `execution_route` 指定 Handler 从各角色的可信 `visible_text` 抽取。
- 归一化与比较只执行数据库规则明确要求的关系，不默认“应相等”“应更低”或其他业务结论。
- 只有一个展示位置、页面没有价格或价格被登录墙遮挡时，按候选规则语义处理，不把“未观察到”当作通过。
- 同一页面的多个候选规则可复用一次采集，但每条规则仍独立写回。

## 证据与写回边界

- `pass`、`fail`、`warning`、`manual_review` 必须有真实页面证据与结构化比较依据。
- 跨页面证据不得混入同一 `page_capture_id`；必要时将不同页面结果分别写回。
- 不确定的价格解析或不可比条件不硬判，使用数据库 `manual_review_policy`。
- Tool 权限阻断、交易安全边界或证据缺失必须在结果摘要中明确记录。

## 交接

- 上游：`rule-matching` 与 `candidate_rules_tool`。
- 下游：`evidence-report` 与规则结果写回 Tool。
- 公共执行与写回契约以 [rule-matching](../rule-matching/SKILL.md) 为准。
