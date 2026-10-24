# agents/orchestrator Agent Orchestrator（V2+V3）

全局调度"哪个 Agent 何时运行"，与 [runtime/](../runtime/README.md) 的边界是：Orchestrator 管全局，Runtime 管单个 Agent 内部的下一步。

## 职责

- 生成并执行 Agent DAG（Planner 产出执行计划后，创建 Explorer/Execution/Evidence/Rule Judge 等 Agent 节点）。
- 控制依赖、重试、超时、取消、降级策略。
- 从 Agent 注册表（[domain/integration](../../domain/integration/README.md)）发现动态配置的 Agent，与内置核心 Agent（Planner/Explorer/Rule Judge 等）并存调度。
- 对低置信、高风险、证据不足、Loop 超预算、Loop 无进展的规则执行结果，创建 `qc_review_item` 交由 Review Agent 处理。

## 专职 Agent 清单（调度对象）

| Agent | 说明 |
| --- | --- |
| Planner Agent | 任务级规划，产出 Agent DAG |
| Explorer Agent | 页面探索策略决策 |
| Execution Agent | 受控执行浏览器操作，不做自由 Agent Loop |
| Evidence Agent 群 | 证据采集与 Observation Packet 组装 |
| Rule Judge Agent | 规则判定与补采决策 |
| Review Agent | 人工审核内建 |
| Report Agent | 报告组织与生成 |

详见[最终整合方案设计第 5 节](../../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)。

## 与 V1 的关系

V1 阶段不需要本目录（单一 Main Agent 自行决定一切，无需全局调度）。本目录是 V2+V3 阶段的新增能力，实现时机见整合方案设计 M0～M4 路线。
