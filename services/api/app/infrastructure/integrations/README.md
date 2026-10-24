# infrastructure/integrations 外部系统集成

第三方系统的客户端封装。

## 规划的集成

| 集成 | 用途 |
| --- | --- |
| 禅道（Zentao） | 审核通过的问题自动建 Bug/提单（带截图、严重级别、复现步骤） |
| Telegram Bot API | TG 聊天入口，接收质检任务提交、返回质检结果 |
| MCP 客户端 | 对接后台配置的 MCP 工具（V2+V3 Agent 配置能力） |

## 职责

- 各集成的鉴权、请求封装、错误处理、重试策略。
- 配置参数来源于 [domain/integration](../../domain/integration/README.md)，本层只负责运行时调用，不负责配置的增删改查。
