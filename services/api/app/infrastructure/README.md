# app/infrastructure 基建层

领域层仓储接口的具体实现，以及所有外部系统的技术细节封装。是唯一允许直接依赖具体技术选型（数据库驱动、对象存储 SDK、浏览器自动化库、LLM SDK）的层。

## 子目录

| 目录 | 定位 |
| --- | --- |
| [db/](db/README.md) | PostgreSQL 访问、ORM 模型、仓储实现 |
| [storage/](storage/README.md) | 对象存储客户端（截图、DOM 快照、报告等产物） |
| [queue/](queue/README.md) | 任务队列（异步质检任务、Worker 消费） |
| [llm_gateway/](llm_gateway/README.md) | LLM/VLM 统一调用网关 |
| [browser/](browser/README.md) | Playwright 浏览器自动化驱动封装 |
| [integrations/](integrations/README.md) | 外部系统集成客户端（禅道、Telegram 等） |

## 依赖规则

- 本层实现 [domain/](../domain/README.md) 定义的仓储接口（依赖倒置），供 `application/` 与 `agents/` 通过接口调用，不反向依赖上层业务逻辑。
- 更换具体技术选型（如更换对象存储供应商、更换 LLM 供应商）只需修改本层实现，不影响 `domain/`、`application/`、`agents/` 的代码。
