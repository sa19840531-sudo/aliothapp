# services/api 后端单体应用

Alioth 平台唯一的后端可部署单元，基于 FastAPI + PostgreSQL 构建，内部承载 API 网关、领域业务逻辑与 AI Agent 编排。

## 两个入口，一套代码

| 入口 | 用途 |
| --- | --- |
| `app/main.py` | FastAPI HTTP 服务入口，供前端 / TG Webhook 调用 |
| `worker.py` | 后台任务 Worker 入口，消费 Redis 任务队列，驱动 Main Agent 执行质检任务，并写回 `qc_agent_run` / `qc_rule_execution` 等运行状态 |

两个入口共享 `app/` 下的全部分层代码，避免维护两套业务逻辑。

## 内部分层

| 目录 | 定位 |
| --- | --- |
| [app/api/](app/api/README.md) | 接口层：路由、请求响应模型、依赖注入、鉴权中间件 |
| [app/domain/](app/domain/README.md) | 领域层：核心实体、值对象、领域服务、仓储接口，按业务域拆分子目录 |
| [app/application/](app/application/README.md) | 应用层：用例编排，协调领域层与基建层完成具体业务流程 |
| [app/agents/](app/agents/README.md) | AI Agent 层：Main Agent、Agent Orchestrator、阶段/规则 Skill、Tool、Agent Loop Runtime、V4 记忆引擎 |
| [app/infrastructure/](app/infrastructure/README.md) | 基建层：数据库访问、对象存储、任务队列、LLM 网关、Playwright 驱动、外部系统集成 |
| [app/core/](app/core/README.md) | 公共基础：配置、日志、异常、通用基类与中间件 |
| [migrations/](migrations/README.md) | 数据库迁移脚本 |
| [tests/](tests/README.md) | 测试代码 |

## 依赖方向

```
api → application → domain
                   ↘ agents → domain
application/agents → infrastructure（通过 domain 定义的仓储接口访问）
```

领域层不依赖应用层、AI Agent 层与基建层的具体实现；基建层实现领域层定义的接口（依赖倒置），保证核心业务规则不受外部技术选型变化影响。

## 关联设计文档

- [总体设计文档](../../docs/design/2026-06-30-alioth-platform-design.md)
- [模块交互流程细化设计](../../docs/design/2026-07-01-alioth-qc-workflow-detail.md)
- [Agent Loop 编排设计](../../docs/design/2026-07-01-alioth-agent-loop-design.md)
- [最终整合方案设计（权威版本）](../../docs/design/2026-07-03-alioth-integrated-solution-design.md)
