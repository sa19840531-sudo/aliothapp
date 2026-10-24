---
name: qc-workflow
description: 质检任务从启动到报告生成的总控执行流程 Skill：定义 Main Agent 在一次质检任务生命周期内，任务规划、页面探索、页面识别、规则匹配、规则判断、证据记录、循环终止、报告生成各阶段的先后顺序与调度关系，说明每个阶段该读取哪个阶段/规则 Skill、该调用哪类 Tool。是唯一必须在任何质检任务开始时最先读取的 Skill，不是具体某类规则的判断标准。
---

# qc-workflow

## 职责

`qc-workflow` 回答"一次质检任务从开始到结束，Main Agent 应该按什么顺序做什么"，是任务级执行流程的总控说明。它本身**不判断任何具体规则**、**不识别任何具体页面类型**、**不定义任何证据存储细节**——这些职责分别下沉到 [task-planning](../phase/task-planning/SKILL.md)、page-exploration/page-detection/navigation-boundary-skill/evidence-report（详见 [phase/README.md](../phase/README.md)）、[rule-matching](../rule/rule-matching/SKILL.md)与各 category 规则 Skill（详见 [rule/README.md](../rule/README.md)）。`qc-workflow` 只负责把这些 Skill 与 Tool 串成一条完整、可循环、可终止的执行链路。

## 何时读取

**必读，且必须最先读取**：Main Agent 收到任何质检任务后，进入具体阶段之前，必须先加载本 Skill 一次，确定完整执行顺序，再依次调度其余 Skill/Tool，不得跳过或自行调整顺序。这是 [instructions.py](../../instructions.py) 的 `MAIN_AGENT_INSTRUCTIONS`（经 [bootstrap.py](../../bootstrap.py) 原样重新导出）中显式要求的强制读取项，不是"命中触发词才读"的普通 Skill。

V2+V3 阶段由 Agent Orchestrator 调度多个专职 Agent 时，本 Skill 定义的阶段顺序作为 Orchestrator 编排各 Agent 先后关系（谁先运行、依赖关系）的依据，具体读取者可能变为 Orchestrator 本身或由其分发给各专职 Agent，Skill 内容本身不变（沿革方式与 [rule-matching](../rule/rule-matching/SKILL.md) 一致）。

## 输入

- 任务启动参数：目标 URL/入口、可选账号、备注、巡检深度。
- 任务创建时已锁定的规则集：`qc_task.ruleset_id` 由后端任务提交链路确定，Agent 不自行选择规则集；页面级候选规则通过 `candidate_rules_tool` 查询。

## 处理步骤

1. **确认任务上下文**：确认当前 Agent Run 已绑定 `task_id` 与 `agent_run_id`，规则集由任务创建阶段锁定，后续规则查询统一交给 `candidate_rules_tool`。
2. **任务规划**：读取 [task-planning](../phase/task-planning/SKILL.md)，拆分执行阶段、设置边界、安排优先级与失败重试策略，产出页面队列的初始入口。
3. **Android APP 分支（若 `target_platform=app` / android）**：在通用页面探索前，先读取 [app-product-orchestration](../phase/app-product-orchestration/SKILL.md)，按 `package_name` 加载 Product Profile；会员拦截/弹窗/底栏等产品差异以 Profile 时序与落点为准，调用 `app_*` 设备 Tool 或语义 ActionIntent，**不要**再跑各产品 `*_script_override` / Airtest 固定脚本作为主路径。
4. **进入主循环，直到页面队列清空为止**，每一轮依次：
   1. 读取 page-exploration 与 navigation-boundary-skill（详见 [phase/README.md](../phase/README.md)）：决定下一个候选页面/入口；同一 `page_type` 的候选页面按 `page_sampling_policy`（`all`/`first_n`/`random_n`/`percentage`）抽样，不要求逐一全量访问。Android 任务同时对照 Product Profile 的 `timing_rules` / `taps`。
   2. 调用 `page_capture_tool` 打开或操作页面，要求采集 DOM 与截图。`single_trigger` 的动作与目标必须分开传递：普通动作使用 `click/back/scroll/hover`；`network_fault` 必须提供只匹配目标广告资源的 `fault_url_pattern`；`submit_probe` 只允许 `ALIOTH_RATE_PROBE_` / `ALIOTH_COMMENT_PROBE_` 中性文本，并分别提供输入框 `selector` 与 `submit_selector`。例如关闭按钮使用 `trigger_action="click", selector="text=关闭"`，禁止把 `click text=关闭` 整体放入 `trigger_action`。若本次动作来自 `working_memory.exploration_frontier.pending_actions`，必须把对应 `exploration_action_id` 传给 `page_capture_tool`；`line_loading`、启动广告倒计时、Flutter 启动壳等瞬态生命周期页只允许执行平台签发的同会话重观察或受信关闭候选，不得重新 `goto` 或凭模型自造动作。完整证据由采集链路持久化，Tool 只返回摘要、`page_capture_id` 与 `artifact_ids`；保存同一次返回的 ID，作为页面识别和规则写回引用。
   3. 读取 page-detection（详见 [phase/README.md](../phase/README.md)），然后调用 `page_detection_tool(page_capture_id)`。该 Tool 内部读取同一次采集的完整语义 DOM、全部截图及可选 OCR，加载固定 Skill 与页面目录并调用独立多模态模型；其结构化结果是本轮页面识别的唯一结论，Main Agent 不得二次猜测或改写：
      - `detection_status=recognized`、`rule_matching_allowed=true` 且 `page_type` 为 Catalog 中可匹配规则的 canonical code：进入步骤 3.4。
      - `detection_status=recognized` 但 `rule_matching_allowed=false`（典型如 `line_loading`）：不得调用 `candidate_rules_tool`，不得把入口首屏当成最终业务页，也不得用“首屏无弹窗”判定连续弹窗不存在。`line_loading` 是生命周期瞬态，应由 Frontier 签发 `same_session_reobserve` 候选，在同一浏览器会话等待线路完成后重新采集；若随后进入 `startup_ad` 或首页弹窗，继续按对应阶段处理。只有同会话重观察预算耗尽仍停留在线路加载态时，才按证据不足或人工复核收口。
      - 识别到 `startup_ad`：先完成该开屏广告页面的候选规则与结果写回，再执行可审计的跳过/关闭或后续低风险页面发现动作进入首页；不得为了尽快进入首页而丢失开屏广告质检结果。
      - 首页存在多层弹窗广告：必须在完成 `line_loading`、`startup_ad` 等前置生命周期阶段并进入业务首页后再判断；首层仍按当前真实采集完成一次弹窗相关规则匹配；可信关闭动作随后由 `page_capture_tool` 在同一次调用中进入有界清障，逐层重新解析最上层真实关闭控件，最多关闭 12 层、最多执行 45 秒，并为每层保留视口截图与 `action_trace`。中间层不得重复执行底层业务内容规则或通用输入风险规则；只有安全结构无法证明时才返回新采集交给 LLM 重新识别。达到上限仍有遮挡时转人工复核，不得继续消耗任务步数。
      - `ambiguous` 或 `insufficient`：当前版本暂不执行补采；不得以相同或派生参数重复调用 `page_detection_tool`，由运行时将对应页面和未完成规则按 `evidence_insufficient` 收口。
      - `error`：阻断常规规则匹配，记录页面识别失败并收敛或转人工复核，不调用 `candidate_rules_tool`。
   4. 仅在步骤 3.3 门禁通过后读取 [rule-matching](../rule/rule-matching/SKILL.md)：使用步骤 3.2 返回的同一个 `page_capture_id` 调用 `candidate_rules_tool(page_type, page_capture_id)`，产出当前页面本轮适用、跳过或后置的规则清单；不得把多个页面的采集 ID 与识别结果交叉使用。
   5. 按 `category` 读取对应规则 Skill（`text-rule`/`visual-rule`/`interaction-rule`/`ad-rule`/`payment-rule`/`input-risk-rule`/`player-rule`；`compliance` 无专属 Skill，直接使用 Tool，完整清单见 [rule/README.md](../rule/README.md)）。
   6. 按每条候选规则的 `execution_route` 选择判定方式（三种 `strategy` 取值的历史设计依据见文末"参考"小节）：
      - `rule_engine`：仅当 `rule_engine_ready=true` 且 `tool_name=rule_judge_tool` 时调用确定性判定，并严格按返回的 Handler、`judge_input_source`、必需输入字段和证据类型准备参数；`trusted_evidence` 输入只传证据引用，由后端读取原文，模型不得用总结文本替代；`ready=false` 时禁止调用并转人工复核。
      - `llm`：Main Agent 基于页面上下文做结构化推理（文案柔顺度、视觉质量、语义违规等模糊判断）。
      - `hybrid`：先调用 Tool 采证，再基于证据做结构化复核并汇总。
   7. 该页面的规则按 `candidate_rules_tool.execution_plan.batches` 执行：候选超过一条时，确定性规则使用 `rule_judge_batch_tool`，`llm/hybrid` 按 category 在一次结构化推理中完成同批规则；禁止逐条重复加载同一 Skill。结构化输出异常或推理不确定时最多重新推理 2 次；仍失败则标记对应条目为 `manual_review`，不阻塞其他批次。
   8. 读取 evidence-report（详见 [phase/README.md](../phase/README.md)）确定证据规范。同一页面多条结果使用 `record_rule_executions_batch_tool` 一次写回，单条候选或批量失败条目的补偿才使用 `record_rule_execution_tool`。全部状态都必须绑定同一次 `page_capture_tool` 返回的 `page_capture_id`；写入 `pass`/`fail`/`warning`/`manual_review` 时，还必须提供真实 `evidence_artifact_ids`。`llm/hybrid` 结论按 rule-matching 的统一 `judge_input/judge_output` 契约写入并提供 `confidence`。批量返回 `partial_success` 时只重试失败条目，禁止重新判定或写回成功条目；收到证据门禁阻断后不得以相同参数重试，没有有效证据时写 `evidence_insufficient`。
   9. 每次候选匹配与规则写回后读取 Tool 返回的 `working_memory` 进度；完成门禁必须同时检查页面规则状态与 `exploration_frontier`：活跃 `current_page` / `pending_pages` 未完成规则写回，或 frontier 仍有 `candidate` / `selected` 动作时，本 Run 不得判为完成。`superseded` / `abandoned` 采集只保留审计，不计为待完成页面。只有 `current_page` 的候选规则全部写回、页面进入 `inspected_pages`，且 frontier 无待探索动作后，才视为当前分支完成。单次 Run 内不得重复处理 `inspected_pages` 中相同的 `page_capture_id`；跨 Run 重启时以数据库中的 `qc_page_capture` / `qc_rule_execution` 重建进度，不能只依赖上一次 Run 的内存对象。
   10. 判断页面队列与 frontier 是否清空：若 `exploration_frontier.pending_action_count > 0`，回到 3.1，由 page-exploration 选择一个动作并在下一次 `page_capture_tool` 调用中传入对应 `exploration_action_id`；`high`/`critical` 风险动作进入 frontier 时会自动标记为 `blocked`，只保留审计，不计为待探索动作。若因页面抽样、风险边界、最大页面数、最大深度、超时或其他运行预算决定不再执行剩余项，必须调用 `exploration_closure_tool`：将动作写为 `blocked`/`skipped`/`failed`，将尚未开始候选规则匹配的页面写为 `abandoned`。已开始候选规则匹配但尚未全部写回结果的页面会被代码级拒绝关闭，必须先完成规则执行。只有页面队列和 frontier 的待处理计数都为 0 才可进入覆盖评估。
   11. 页面队列和 frontier 收口后、生成报告前，必须再次调用 `exploration_closure_tool` 写入 `page_type_assessments`，用 `assessment_scope_page_types` 固化当前产品显式锁定或实际发现的业务页面范围，并设置 `assessment_scope_complete=true`。不得把运行时 Catalog 全量页型伪造成单个产品的必须目标；`covered` 必须引用同一次采集的 `page_capture_id` 与真实 `evidence_artifact_id`，`not_applicable`、`requires_auth`、`blocked_by_safety` 必须引用可审计页面或对应终态探索动作，无法证明时只能写 `not_found` 或 `evidence_insufficient`。`line_loading` 与 `startup_ad` 仍保留为 Catalog 生命周期观测，不进入产品业务覆盖率分母。
4. **任务收尾**：触发任务级去重与排序，调用「报告生成 Tool」产出结构化报告，更新任务状态，移交人工审核（人工审核流程属于 Review 环节，超出本 Skill 范围）。

## 输出格式（示意）

本 Skill 不直接产出数据结构，而是驱动其余 Skill/Tool 产出各自的结构化结果（规则清单、判定结论、报告）。任务收尾时的整体状态示意：

```json
{
  "task_id": "task-001",
  "rule_set_version": "v20260701",
  "pages_visited": 42,
  "pages_skipped_by_sampling": 8,
  "rule_executions": { "pass": 120, "fail": 6, "manual_review": 3 },
  "termination_reason": "completed"
}
```

字段命名以示意为准，最终字段与 `qc_agent_run`/`qc_rule_execution` 的落库 Schema 对齐（核心数据模型设计的历史依据见文末"参考"小节）。

## 边界情况

- **页面队列一开始就为空**（入口不可达/无候选页面）：跳过主循环，直接进入任务收尾，报告中注明覆盖为空的原因，不视为异常终止。
- **规则清单为空**：跳过该页面的判断阶段（步骤 3.5-3.8），直接回到 3.1 处理下一个候选页面。
- **接近循环护栏预算**：在预算真正耗尽前调用 `exploration_closure_tool` 显式关闭剩余可跳过项，再生成报告。若已经命中最大步数、卡死或超时护栏，则运行时按异常终止，报告中标注未完成项并交由人工审核决定是否重跑，不能伪造完整完成。
- **候选规则查询失败**：`candidate_rules_tool` 返回错误时，记录当前页面规则匹配失败，相关规则不得凭空生成结论；必要时将任务转人工复核。
- **规则执行路由不可用**：候选规则返回 `execution_route.ready=false` 时不得调用 `rule_judge_tool`，应记录 `blocked_reason` 并转人工复核；该情况属于规则配置缺陷，不能由模型猜测补齐。
- **页面识别证据不足或冲突**：禁止对同一个证据缺口原参数无限调用 `page_detection_tool`；仅对 Frontier 明确签发的同会话重观察候选执行有界补采，超过预算应按 `evidence_insufficient` 收口并继续其他可执行页面探索分支。
- **页面识别错误**：`page_detection_tool` 返回 `error` 时不得进入 rule-matching；记录 `error_code` 与 `page_capture_id` 后按任务预算收敛或转人工复核。

## 与其他 Skill/Tool 的关系

- **上游依赖**：无——本 Skill 是任务启动后第一个被强制读取的 Skill。
- **下游调度**：依次驱动 [task-planning](../phase/task-planning/SKILL.md) → page-exploration/navigation-boundary-skill → `page_capture_tool` → page-detection → `page_detection_tool(page_capture_id)` → [rule-matching](../rule/rule-matching/SKILL.md) → 各 category 规则 Skill（详见 [rule/README.md](../rule/README.md)） → evidence-report（详见 [phase/README.md](../phase/README.md)）；页面识别门禁通过后才可调用 `candidate_rules_tool`，预算或风险收口使用 `exploration_closure_tool`，其余 Tool 详见 [tools/README.md](../../tools/README.md)。
- **不负责**：具体规则判断标准本身（各规则 Skill 职责）、页面类型识别细节（page-detection 职责）、证据存储路径规范（evidence-report 职责）、循环护栏的具体实现（[runtime/](../../runtime/README.md) 职责）、人工审核流程（Review 环节，超出 Main Agent 范围）。

## 参考

> 以下三份文档仅存在于源码仓库 `docs/design/` 目录，不随部署产物分发（Docker 镜像仅打包 `services/` 目录）；仅供研发人员查阅历史设计背景，Agent 运行时不需要也不应尝试读取：`2026-07-03-alioth-integrated-solution-design.md`（执行循环与执行时序图，本 Skill 的直接依据；4.7 节说明 `executor_type` 三种取值的选型依据）、`2026-07-01-alioth-qc-workflow-detail.md`（批量质检循环细则、人工审核衔接）、`2026-07-03-alioth-core-schema-design.md`（`qc_agent_run`/`qc_rule_execution` 落库 Schema）。

- [agents/README.md](../../README.md)（Skill 与 Tool 协同机制）
- [runtime/README.md](../../runtime/README.md)（循环护栏实现）
