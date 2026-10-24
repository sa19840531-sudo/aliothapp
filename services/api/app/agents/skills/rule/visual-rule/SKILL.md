---
name: visual-rule
description: 视觉呈现类质检 Skill。适用于 candidate_rules_tool 返回 category=visual，且检查点涉及已渲染页面的媒体显示、布局完整性、空白或兜底状态、视觉错误、静态或多帧变化等场景；指导 Main Agent 使用 page_capture_tool、deterministic_assertion_tool、rule_judge_tool 与结果写回 Tool，不内置规则编号、阈值或数据库规则内容。
---

# visual-rule

## 职责与适用场景

仅处理 `candidate_rules_tool` 返回的 `category=visual` 候选规则，常见能力场景包括：

- 图片、视频封面、图标等媒体元素的清晰度、比例、裁切、边界和遮挡观测。
- 页面模块、占位、骨架屏、缺省态、错误态和布局完整性观测。
- 需要对静态截图、DOM 几何摘要或多帧页面变化进行复核的视觉问题。
- 页面可见技术异常与渲染结果之间的交叉核对。

这些场景只用于判断“是否加载本 Skill”和“选择什么 Tool”。实际检查点、通过/失败条件、阈值、严重度、适用页面和复核策略必须逐条读取数据库候选规则。

不处理文案语义（交给 `text-rule`）、广告素材或广告行为（交给 `ad-rule`）、需要改变业务状态的交互验证（交给 `interaction-rule`）。

## 动态规则输入

对本批每条候选规则读取并原样保留：

- `rule_id`、`rule_code`、`name`、`category`、`severity`。
- `check_point`、`steps`、`input_contract`、`judge_config`、`manual_review_policy`。
- `execution_route` 及其中的判定策略、所需输入字段、证据类型和 Tool 路由。

Skill 不保存任何具体规则编号或固定判定值。候选规则缺少完成判定所需的数据库配置时，不得凭经验补齐，应按执行路由阻断或人工复核处理。

## 可用 Tool

| Tool | 视觉质检用途 | 使用条件 |
| --- | --- | --- |
| `page_capture_tool` | 读取当前页面采集已有的 DOM、可见文本、截图、网络与控制台摘要 | 当前版本页面探索不主动调用 `page_capture_tool` 补采；视觉类默认只读 |
| `deterministic_assertion_tool` | 执行正则、文本包含、数值比较、DOM 存在性等可复现辅助断言 | 仅使用候选规则 `judge_config` 给出的模式、操作符或期望值；不能自造视觉阈值 |
| `rule_judge_tool` | 执行单条已就绪的数据库确定性规则 | 仅当 `execution_route.rule_engine_ready=true` 且路由指定该 Tool |
| `rule_judge_batch_tool` | 批量执行同一页面已就绪的确定性视觉规则 | 同一页面的确定性候选超过一条时优先使用 |
| `record_rule_execution_tool` | 写回单条候选或补偿批量失败项 | 使用候选结果中的动态规则标识 |
| `record_rule_executions_batch_tool` | 批量写回同一页面多条视觉规则结果 | 多规则页面优先使用，共享当前 `page_capture_id` |

未在 Capability Manifest 注册的 OCR、截图差异或专用视觉 Tool 不得假设可用。候选规则强依赖缺失能力或当前证据缺件时，直接写 `evidence_insufficient` 或按数据库策略转 `manual_review`。

## 执行流程

1. 从 `execution_plan.batches` 取得 visual 批次，核对每条规则的动态字段，不在 Skill 中查找规则编号。
2. 按 `input_contract.required_artifacts` 和 `quality_gate` 检查当前页面采集，只复用已有产物；时序或闪烁证据缺失时直接写 `evidence_insufficient`。
3. 先从 DOM、可见文本、网络摘要和截图提取与 `check_point` 对应的结构化观测；不要对候选规则未要求的视觉特征追加判定。
4. 若 `execution_route.rule_engine_ready=true`，按路由调用 `rule_judge_tool` 或 `rule_judge_batch_tool`。若为 `hybrid`，可用 `deterministic_assertion_tool` 形成辅助信号，再依据数据库 `check_point/judge_config` 做视觉复核；若为 `llm`，只基于已采证据做结构化判断。
5. 结论置信度与人工复核条件读取 `manual_review_policy`；禁止使用 Skill 固定阈值。
6. 将 `judge_input`、`judge_output`、真实证据 ID 和动态规则标识交给写回 Tool。多条规则优先批量写回。

## 视觉观测方法

- **静态媒体与布局**：使用截图观察渲染结果，使用 DOM 与数值断言验证元素存在性、尺寸或位置条件；具体容差取自规则配置。
- **模块与兜底状态**：结合 DOM 结构、可见文本、网络失败摘要和截图判断当前状态；“空白是否违规”由 `check_point` 定义。
- **可见错误状态**：若规则路由已就绪，优先交给 `rule_judge_tool`；辅助正则只能使用 `judge_config` 中的模式。
- **多帧变化**：仅消费当前采集已有的 `timed_capture` 产物；缺少多帧证据时写 `evidence_insufficient`，不主动创建采集动作。

## 布局几何与截断

仅当候选规则在 `input_contract`、`judge_config` 或 `execution_route` 中声明需要布局几何、边距、重叠、图片比例或文本截断证据时，才进入本节流程。未声明该能力的 visual 规则不得额外采集、不得额外调用几何或截断相关能力，也不得把本节示例当作默认检查项。

- **几何证据**：优先使用同一次 `page_capture_tool` 返回的 `ui_snapshot`、元素边界、视口信息和截图引用；若候选规则路由到确定性 Handler，则按 `execution_route` 调用，不在 Skill 中维护 Handler 名称或阈值。
- **截断证据**：只读取采集端已经提供的 `scroll_width`、`client_width`、`text-overflow`、`white-space`、`line-clamp` 等摘要字段；字段缺失且规则强依赖时写 `evidence_insufficient` 或按 `manual_review_policy` 转人工复核。
- **判断边界**：元素是否“核心信息受损”属于规则语义判断，必须结合 `check_point/judge_config` 和真实证据；Skill 只能组织证据和 Tool 调用，不自行定义文字长度、像素容差或失败等级。
- **证据引用**：几何或截断结论必须绑定触发该判断的 `page_capture_id`、截图或 UI 快照 artifact；不得在 `judge_output` 内嵌完整 DOM、完整截图或未截断文本。

## 安全与证据边界

- 默认只读：不点击、不滚动触发、不悬停触发、不改变页面状态。
- 广告元素与普通内容元素无法区分时，不跨类别硬判，转人工复核或证据不足。
- DOM、截图与可见文本冲突时记录冲突，不能挑选单一证据强行下结论。
- `pass`、`fail`、`warning`、`manual_review` 必须绑定当前页面真实附件；重载荷只通过证据 ID 引用。
- 页面没有候选规则要求的对象时，按动态规则语义写 `skipped`，不得静默丢弃。

## 交接

- 上游：`rule-matching` 与 `candidate_rules_tool`。
- 下游：`evidence-report` 与规则结果写回 Tool。
- 公共执行与写回契约以 [rule-matching](../rule-matching/SKILL.md) 为准。
