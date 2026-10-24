---
name: ad-rule
description: 广告呈现与广告交互质检 Skill。适用于 candidate_rules_tool 返回 category=ad，且检查点涉及广告素材、落地页、倒计时、跳过控件、弹窗或蒙层、连续展示、加载失败兜底、贴片广告等场景；指导 Main Agent 使用 page_capture_tool、deterministic_assertion_tool、rule_judge_tool 与结果写回 Tool，不内置规则编号、合并关系或固定阈值。
---

# ad-rule

## 职责与适用场景

仅处理 `candidate_rules_tool` 返回的 `category=ad` 候选规则，适合以下能力场景：

- 广告素材的比例、裁切、清晰度和页面内呈现。
- 广告内容与落地页之间的跳转和主题一致性。
- 倒计时、首帧、连续弹窗或其他需要多帧时序观测的广告行为。
- 跳过、关闭、蒙层和广告热区等控件交互。
- 广告资源失败后的页面、播放器或遮罩兜底状态。
- 开屏、弹窗、悬浮、贴片等广告载体。

这些场景只用于加载 Skill 与选择 Tool。具体广告规则、合并关系、期望行为、阈值、等待时间、适用页面、严重度和复核策略全部来自数据库候选规则。

非广告媒体质量交给 `visual-rule`；非广告链接或普通页面交互交给 `interaction-rule`；广告文案语义以候选规则 category 路由为准，不能在本 Skill 越类判定。

## 动态规则输入

逐条读取并保留：

- `rule_id`、`rule_code`、`name`、`severity`、页面适用信息。
- `check_point`、`steps`、`input_contract`、`judge_config`、`manual_review_policy`。
- `execution_route`、证据要求、确定性 Handler 与 Tool 路由。

Skill 不记忆历史规则编号，也不解释数据库里的规则合并关系。若多条候选规则检查同一广告动作，分别保留各自身份，但尽量复用一次采证和触发结果。

## 可用 Tool

| Tool | 广告质检用途 | 使用条件 |
| --- | --- | --- |
| `page_capture_tool` | 读取当前页面采集已有的广告、时序帧和动作后证据 | 当前版本页面探索不主动调用 `page_capture_tool` 补采；规则缺少行为或时序证据时直接写 `evidence_insufficient` |
| `deterministic_assertion_tool` | DOM 存在性、文本/正则、数值比较等辅助断言 | 只使用规则配置中的阈值、模式和期望值 |
| `rule_judge_tool` | 单条确定性广告规则判定 | 仅当 `execution_route.rule_engine_ready=true` |
| `rule_judge_batch_tool` | 批量判定同一页面的确定性广告规则 | 多条已就绪规则优先使用 |
| `record_rule_execution_tool` | 写回单条候选或补偿批量失败项 | 使用动态规则标识和真实证据 |
| `record_rule_executions_batch_tool` | 批量写回同一页面多条广告规则 | 多规则页面优先使用 |

专用截图比对或 OCR Tool 未注册时不得编造调用。规则强依赖缺失能力且现有采集不能满足时，写 `evidence_insufficient` 或按数据库策略转 `manual_review`。

## 执行流程

1. 从 `execution_plan.batches` 取得 ad 批次，按候选规则 `sort_order` 和批次顺序执行。
2. 识别当前广告对象和载体，核对 `input_contract`。页面没有候选规则要求的广告对象时，按该规则语义写 `skipped`。
3. 仅复用已有静态页面采集、时序帧和 Action Trace；活动倒计时缺少完整序列时直接写 `evidence_insufficient`。
4. 规则要求模拟广告网络失败但当前证据没有已完成的受控故障轨迹时，写 `evidence_insufficient`，不得主动执行 `network_fault`。
5. 规则要求点击广告、关闭控件或蒙层但当前证据没有对应动作前后角色时，写 `evidence_insufficient`；页面生命周期关闭动作仍由 Explorer 阶段机负责。
6. 按 `execution_route` 选择 `rule_judge_tool`、批量判定或 `llm/hybrid` 结构化复核；`session_dependency=active_session` 的规则必须先具备契约要求的动作、时序或播放器事实，禁止拿被动静态首帧反复调用 Judge。页面不存在目标时写 `skipped`；目标存在但安全动作无法执行时按规则策略转 `manual_review/evidence_insufficient`，不得重试同一组参数。
7. 从 `manual_review_policy` 读取置信度与复核条件，使用写回 Tool 记录每条动态候选规则结果。

## 广告采证方法

- **素材与布局**：采集广告 DOM、截图与必要的尺寸摘要；比例容差和判定条件取自规则配置。
- **落地页**：只读取已有 URL、标题、截图和动作轨迹；缺少动作后证据时写 `evidence_insufficient`。
- **时序行为**：只读取已有 `timed_capture` / `temporal_trace`；帧数或关键区间不足时写 `evidence_insufficient`。
- **跳过、关闭、蒙层**：按 `required_artifact_types` 记录触发前后 DOM、截图、URL、`action_trace` 与 `ui_snapshot`；多层广告弹窗由一次有界清障动作产生逐层视口截图和带序号的关闭轨迹，同一证据链可支撑多条候选规则，但采集角色必须按 `input_contract` 分别绑定。
- **故障兜底**：仅拦截候选规则指定的广告请求范围，不影响业务请求；Action Trace 只保留故障正则摘要、脱敏后的命中 URL 与命中数量，观察结束后必须解除路由且不得继续注入。

## 关闭按钮可点性

仅当候选规则在 `steps`、`input_contract` 或 `judge_config` 中声明需要验证广告关闭、跳过或蒙层关闭控件时，才执行关闭按钮可点性流程。未声明关闭验证的广告规则不得为了补充证据主动点击关闭控件，也不得把可见关闭按钮自动推断为可点击通过。

- **交互前证据**：只复用已有关闭前 `page_capture_id`，记录关闭控件位置、可见状态、页面载体、倒计时或禁用状态。
- **动作执行**：本 Skill 当前不为规则主动执行关闭动作；Explorer 的页面生命周期动作可以保留，但不得被提升成规则补采动作。
- **交互后证据**：只比较已有动作后页面中的遮罩、广告容器、关闭控件、URL 和业务内容可见性；无法证明广告已关闭时写 `evidence_insufficient`。
- **多层弹窗**：连续弹窗由阶段机或有界清障链路逐层处理，每层都要有 before/after 证据和动作轨迹；本 Skill 不绕过阶段机直接循环点击。

## 安全边界

- 绝不反复点击刷广告，不重复触发，不为凑证据制造展示次数。连续弹窗清障最多 12 层或 45 秒；只点击每轮重新证明的最上层关闭控件，无法证明时退回 LLM，达到上限转人工复核。
- 开屏广告存在“质检完成后关闭/跳转”的必经动作时，原页全部规则必须先收敛；不得把剩余静态 Judge 放入后台后抢先推进页面，避免迟到结果丢失原页归属。
- `network_fault` 禁止 `.*` 等全请求匹配；没有实际命中目标请求时属于证据不足，不能把“调用过故障动作”当成已成功注入。
- 不下载安装广告应用，不填写信息，不登录落地页，不支付，不授权。
- 落地页要求登录、支付、下载或授权才能继续时立即停止，并按动态规则语义收口。
- 疑似欺骗性跳过或高风险热区默认只读；除非数据库规则明确允许且安全边界通过，否则不点击验证。
- 所有结论绑定当前页面真实 `page_capture_id` 与附件，证据不足不硬判。

## 交接

- 上游：`rule-matching` 与 `candidate_rules_tool`。
- 下游：`evidence-report` 与规则结果写回 Tool。
- 公共执行与写回契约以 [rule-matching](../rule-matching/SKILL.md) 为准。
