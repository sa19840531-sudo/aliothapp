---
name: task-planning
description: URL 质检任务的任务级规划 Skill。Main Agent 在读取 qc-workflow 后、进入页面探索前读取，用于把一个入口 URL 拆成可执行阶段，明确页面探索顺序、规则优先级、停止边界和失败兜底策略。
---

# task-planning

## 职责

`task-planning` 回答"一个 URL 质检任务应该怎么拆阶段、按什么顺序执行、什么时候停止"。它只负责规划执行路径，不负责页面类型识别、规则匹配、具体规则判定或结果写库。

## 何时读取

Main Agent 在读取 [qc-workflow](../../qc-workflow/SKILL.md) 后、首次打开目标 URL 前读取本 Skill。后续页面探索过程中，如果遇到登录拦截、入口不可达、页面结构变化或循环护栏临近上限，可重新读取本 Skill 调整后续计划。

## 输入

- `target_url`：任务入口 URL。
- `task_id` / `agent_run_id`：系统运行上下文，仅供 Tool 使用，不要求模型自行传入。
- `ruleset_id` / `channel`：任务创建时锁定的规则集与渠道。
- `page_sampling_policy`：页面抽样策略。
- `loop_guard`：最大步数、最大访问页面数、卡死检测等护栏配置。

## 规划步骤

1. **确认入口阶段**：把 `target_url` 作为第一访问节点，优先完成页面可达性、跳转后 URL、标题、首屏文本和基础截图采集。
2. **设置探索顺序**：默认按"入口页 → 弹窗/浮层 → 主内容区 → 导航入口 → 安全下级页面"顺序推进；遇到登录、支付、提交订单等高风险入口时停止深入。
3. **设定探索阶段优先级**：在尚未取得候选规则前，优先探索低风险、低依赖页面。`candidate_rules_tool` 返回候选规则后，必须严格使用数据库 `sort_order`、`severity` 和 `execution_plan`，本 Skill 的默认探索顺序不得覆盖规则执行顺序。
4. **声明停止边界**：同一 URL 不重复处理；不跨域深入；达到页面数量、点击次数或时间预算后停止；无新候选规则时停止当前页面。
5. **定义异常兜底**：入口不可达时任务失败；登录拦截时相关规则 `skipped`；页面元素缺失导致无法判定时标记 `evidence_insufficient` 或 `error`；不确定时进入 `manual_review`。
6. **输出执行计划**：给出页面队列、每个阶段的目标、预期调用的 Skill/Tool、停止条件和异常处理方式。

## 输出格式（示意）

```json
{
  "entry_url": "https://example.com/activity",
  "phases": [
    {
      "name": "入口页观测",
      "goal": "打开 URL，采集页面基础状态并识别 page_type",
      "skills": ["page-detection"],
      "tools": ["页面操作 Tool"],
      "stop_when": "入口不可达或 page_type 已识别"
    },
    {
      "name": "当前页面规则闭环",
      "goal": "匹配候选规则并写入规则执行结果",
      "skills": ["rule-matching", "具体规则 Skill", "evidence-report"],
      "tools": ["candidate_rules_tool", "record_rule_execution_tool"],
      "stop_when": "当前页面适用规则均已写入 qc_rule_execution"
    }
  ],
  "boundary": {
    "same_domain_only": true,
    "forbidden_actions": ["login", "payment", "submit_order"],
    "skip_repeated_url": true
  }
}
```

字段命名以示意为准，实际执行时以运行时上下文和 Tool 返回结构为准。

## 与其他 Skill/Tool 的关系

- **上游依赖**：[qc-workflow](../../qc-workflow/SKILL.md)。
- **下游交接**：page-exploration/page-detection 负责页面探索与识别；[rule-matching](../../rule/rule-matching/SKILL.md) 负责候选规则匹配；各规则 Skill 负责具体判定；evidence-report 负责证据与报告口径。
- **会规划但不直接调用的 Tool**：`candidate_rules_tool`、`record_rule_execution_tool`、页面操作 Tool、截图/证据 Tool。
- **不负责**：SQL 查询、规则写库、页面点击实现、LLM 判定细节。
