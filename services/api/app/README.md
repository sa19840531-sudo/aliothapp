# app/ 应用代码包

`services/api` 的核心 Python 包，包含全部分层代码。

## 分层一览

| 目录 | 层 | 职责 |
| --- | --- | --- |
| `api/` | 接口层 | HTTP 路由、请求/响应模型（DTO）、依赖注入、鉴权中间件 |
| `domain/` | 领域层 | 核心实体、值对象、领域服务、仓储接口（端口），按业务域拆分 |
| `application/` | 应用层 | 用例编排（Use Case / Application Service），协调领域层与基建层 |
| `agents/` | AI Agent 层 | Main Agent、Agent Orchestrator、Skill、Tool、Agent Loop Runtime、记忆引擎 |
| `infrastructure/` | 基建层 | 领域层仓储接口的具体实现、外部系统客户端 |
| `core/` | 公共基础 | 配置、日志、异常、通用基类、中间件 |

## 入口文件

- `main.py`：FastAPI 应用入口，注册 `api/` 下的路由。
- `worker.py`：后台任务 Worker 入口，消费 Redis 任务队列（[infrastructure/queue](infrastructure/queue/README.md)）并驱动 `agents/` 下的 Agent Loop，与 `main.py` 共享同一套分层代码，仅入口不同。

## 分层调用约定

- `api/` 只做协议转换（HTTP ↔ 内部调用），业务逻辑不写在路由函数里，统一委托给 `application/`。
- `application/` 编排 `domain/` 与 `agents/`，不直接处理 HTTP 细节，也不直接依赖具体数据库/存储实现。
- `domain/` 保持纯粹，不依赖 FastAPI、SQLAlchemy 等框架细节，只定义业务规则与仓储接口。
- `agents/` 依赖 `domain/` 的实体与仓储接口，通过 `infrastructure/` 提供的实现访问数据库、对象存储、LLM 网关、Playwright 等外部能力。
- `infrastructure/` 实现 `domain/` 定义的接口，可自由替换具体技术选型（如更换向量数据库、更换 LLM 供应商）而不影响上层。
