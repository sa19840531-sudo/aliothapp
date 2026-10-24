# app/domain 领域层

平台核心业务规则的所在地，不依赖 FastAPI、SQLAlchemy 等具体框架，只表达"业务是什么"，不表达"技术上怎么实现"。

## 领域边界划分

遵循 [AGENTS.md](../../../../AGENTS.md) "领域边界清晰"的最高优先级，按业务域拆分子目录：

| 子域 | 目录 | 核心实体（对应设计文档数据模型） |
| --- | --- | --- |
| 规则与数据源域 | [rule/](rule/README.md) | `qc_ruleset`、`qc_rule`、敏感词库、品牌词库、知识库文档 |
| 任务与执行域 | [task/](task/README.md) | `qc_task`、`qc_page_capture`、`qc_agent_run`、`qc_agent_action_trace` |
| 证据与判定域 | [evidence/](evidence/README.md) | `qc_evidence_artifact`、`qc_observation_packet`、`qc_rule_execution`（`qc_finding` 的演进版） |
| 页面类型知识域 | [page/](page/README.md) | `qc_page_type`、页面识别信号、页面转移候选、动作意图知识 |
| 人审域 | [review/](review/README.md) | `qc_review_item` |
| 权限与账号域 | [iam/](iam/README.md) | 用户、角色、权限、操作审计日志 |
| 集成与配置域 | [integration/](integration/README.md) | Agent 配置、Agent 注册表、Agent Capability Registry、MCP 注册、凭证库、禅道/Telegram 集成配置 |
| APP 产品 Profile 域 | [app_product/](app_product/README.md) | 按包名的产品专属编排知识（弹窗/时序/落点），供 Agent 替代产品固定脚本（见 `docs/design/2026-10-21-app-product-agent-orchestration.md`） |

## 每个子域内部约定（实现阶段落地，当前骨架暂不预建）

- `entities.py` 或 `entities/`：核心实体与值对象。
- `repositories.py` 或 `repositories/`：仓储接口（端口），由 `infrastructure/db` 实现。
- `services.py` 或 `services/`：领域服务（跨实体的业务规则，不涉及外部 IO）。

## 依赖规则

- 领域层之间可以互相引用（如 `evidence` 域引用 `rule` 域的 `qc_rule`），但不能反向依赖 `application/`、`agents/`、`infrastructure/`。
- 领域层是"稳定抽象"的核心，数据结构变更需谨慎评估对下游（前端、报告、Agent 判定）的影响。
