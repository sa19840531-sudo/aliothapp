# agents/skills/phase 阶段 Skill

指导 Main Agent（V1）/ 各专职 Agent（V2+V3）“流程怎么走”，不涉及具体规则判定标准。遵循 [Agent Skills 规范](https://agentskills.io)。

## Skill 清单

| Skill | 状态 | 职责 | 关键决策点 |
| --- | --- | --- | --- |
| task-planning | 已补全 | 任务级规划 | 如何拆阶段、设置边界、安排优先级、失败重试策略 |
| page-exploration | 已补全 | 页面探索策略 | 如何打开入口、发现 Tab/列表/详情/广告/支付/设置等候选页面，并控制 V1 探索边界 |
| page-detection | 已补全 | 页面识别调用与标准 | 如何在采集后调用 page_detection_tool(page_capture_id)，由 Tool 使用完整 DOM/截图生成唯一结构化结论，并按四态控制 rule-matching |
| navigation-boundary-skill | 已补全 | 导航边界 | 最大深度、外链、登录、支付、下载等高风险操作的限制；同类页面的识别门禁（`page_sampling_policy` 抽样规则）；支持用户主动输入覆盖默认门禁 |
| evidence-report | 已补全 | 证据与报告规范 | 每条规则要保存什么证据，页面基线证据如何复用，报告如何组织 |
| app-product-orchestration | 已补全 | Android 产品专属编排 | 按包名加载 Product Profile，时序/落点驱动设备 Tool；禁止新产品固定脚本主路径 |

详见[最终整合方案设计 4.3 节](../../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)（历史文档中称为“阶段 Playbook”，术语沿革见 [docs/专项能力与高频回归规则.md](../../../../../../docs/专项能力与高频回归规则.md)）。

- [app-product-orchestration/SKILL.md](app-product-orchestration/SKILL.md)：Android 任务于通用探索前读取；产品差异回写 Profile，见 `docs/design/2026-10-21-app-product-agent-orchestration.md`。

## V2+V3 阶段的调用者拆分

- `task-planning` → Planner Agent
- `page-exploration` / `page-detection` / `navigation-boundary-skill` → Explorer Agent
- `evidence-report` → Evidence Agent 群 / Report Agent

Skill 内容本身不随调用者拆分而改变，只是“谁来读取”发生变化。

## V1 实现（2026-07，2026-07-05 术语更正）

- [task-planning/SKILL.md](task-planning/SKILL.md) 已补全为 URL 质检任务规划 Skill，负责入口 URL 阶段拆解、探索顺序、停止边界和异常兜底。
- [page-exploration/SKILL.md](page-exploration/SKILL.md)、[page-detection/SKILL.md](page-detection/SKILL.md)、[evidence-report/SKILL.md](evidence-report/SKILL.md) 已按阶段 Skill 口径补全；page-detection 明确调用独立多模态 Tool，并以 Tool 结果作为唯一页面识别结论。
- `navigation-boundary-skill/SKILL.md` 已落地导航边界策略，V1 不再由 `page-exploration` 内联承担覆盖规则。
