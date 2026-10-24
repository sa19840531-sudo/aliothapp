# agents/memory V4 记忆引擎

平台自我进化能力的载体，作为横切层叠加在 V1/V2+V3 运行时之上，当前处于设计阶段（V4），暂不参与 V1/V2+V3 的实现。

## 分层记忆

| 记忆类型 | 内容 | 存储 | 用途 |
| --- | --- | --- | --- |
| 工作记忆 | 单次任务上下文 | 内存/Redis | 任务内推理连贯（实际由 [runtime/](../runtime/README.md) 承载，此处不重复） |
| 情景记忆 | 历史质检案例（task + findings + 人审结果） | PostgreSQL + pgvector | Agent 检索类似案例 |
| 语义记忆 | 沉淀的领域经验、规则有效性统计、易错模式 | PostgreSQL + pgvector | 业务 Agent RAG、规则进化依据 |
| 反馈记忆 | 人审确认/驳回、误报/漏报标注 | PostgreSQL | 进化闭环的训练信号，来源于 [domain/review](../../domain/review/README.md) 的 `qc_review_item` |

## 自我进化闭环

采集 → 分析（误报/漏报/低效规则模式）→ 提案（规则优化/新规则/Prompt 调整）→ 离线回放评估 → 灰度验证 → 全量或回滚，重大变更需人工把关。

详见[总体设计文档第 8 节](../../../../../../docs/design/2026-06-30-alioth-platform-design.md)与[整合方案设计第 6 节](../../../../../../docs/design/2026-07-03-alioth-integrated-solution-design.md)。

## 说明

本目录的情景/语义/反馈记忆的数据模型与算法逻辑一体维护在此（不下沉到 `domain/`），因为记忆引擎是 V4 专属的横切能力，不是 V1～V3 共用的核心业务实体。
