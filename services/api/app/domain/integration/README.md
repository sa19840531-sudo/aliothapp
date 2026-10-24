# domain/integration 集成与配置域

平台可扩展性相关的配置数据与外部系统集成，覆盖 V2+V3 阶段新增能力。

## 核心实体

- Agent 配置：后台创建的 Agent 定义（系统提示词、挂载的 Skill/Tool/知识库/MCP）。
- Agent 注册表：Orchestrator 运行时发现可调度 Agent 的来源。
- Agent Capability Registry：`capability_name`/`capability_kind`（tool/skill）/`input_schema`/`output_schema`/`permission_level`/`owner_team`/`version`，供 Agent 调用前做参数与权限校验。
- MCP 注册：可用 MCP 工具的注册信息。
- 凭证库（V2+）：质检目标站点的登录态账号凭证，需加密存储。
- 禅道集成配置：对接参数（用于审核通过问题自动建 Bug/提单）。
- Telegram 集成配置：Bot Token、Webhook 配置（TG 作为质检任务提交入口之一）。

## 职责

- 为 `agents/orchestrator` 提供 Agent 与 Skill/Tool 的可发现性数据源。
- 凭证的加密存储与安全访问（避免明文凭证泄露，遵循 OWASP 安全要求）。
- 外部系统（禅道、Telegram）集成参数的维护，具体调用客户端实现在 [infrastructure/integrations](../../infrastructure/integrations/README.md)。

## 关联

- 被 `agents/orchestrator`、`agents/tools`（Tool 注册表）在运行时读取。
- 与 [domain/iam](../iam/README.md) 共同构成权限与配置的管理面（管理后台维护入口）。

## 与 infrastructure/integrations 的区别

本域只承载集成所需的**配置数据**与业务规则（如凭证如何加密、Agent 如何注册），具体对接第三方系统的 HTTP 客户端实现在 [infrastructure/integrations](../../infrastructure/integrations/README.md)。
