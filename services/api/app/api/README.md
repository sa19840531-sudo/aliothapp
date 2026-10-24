# app/api 接口层

FastAPI 路由与 HTTP 协议转换层，是前端（`app/admin`、`app/workstation`）与 Telegram Bot Webhook 的唯一入口。

## 职责

- 定义 HTTP 路由，按业务域分组（规则、任务、审核、Agent 配置、鉴权、集成等）。
- 请求/响应模型（Pydantic Schema），与 `domain/` 内部实体解耦，避免内部结构变化直接暴露给外部。
- 鉴权与权限校验中间件、依赖注入（数据库会话、当前用户等）。
- 参数校验、统一异常处理、统一响应格式。

## 接口命名约定

遵循 [AGENTS.md](../../../../AGENTS.md) "接口命名一致"原则：

- 路由按版本前缀组织（见 [v1/](v1/README.md)）。
- 资源命名使用复数、小写下划线风格，与 `domain/` 的业务域一一对应（如 `/rules`、`/tasks`、`/review-items`）。
- 业务逻辑不下沉到路由函数内，路由函数只做参数解析 + 调用 `application/` 层用例 + 返回响应。

## 子目录

- [v1/](v1/README.md)：当前 API 版本，后续如有不兼容变更再新增 `v2/`。
