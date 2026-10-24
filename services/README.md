# services/ 后端服务

存放平台后端服务，技术栈 Python + FastAPI + PostgreSQL。

## 子项目

| 目录 | 说明 |
| --- | --- |
| [api/](api/README.md) | 唯一的后端单体应用：对外提供 FastAPI HTTP 接口，对内承载 Agent Loop（Playwright 采集 + 规则判定）后台任务处理 |

## 架构决策：单体优先，不过早微服务化

遵循 [AGENTS.md](../AGENTS.md) "保持稳定抽象，默认采用分层清晰的单体架构，不过早微服务化"的原则：

- `services/api` 是**唯一**的可部署 Python 项目，内部按技术层清晰分层（接口层 / 领域层 / 应用层 / AI Agent 层 / 基建层）。
- HTTP 服务（FastAPI）与后台任务 Worker（消费任务队列、驱动 Agent Loop）是**同一代码库的两个入口**（`main.py` 与 `worker.py`），共享同一套领域模型、Skill 与基建代码，避免出现两套重复实现。
- 未来若确有独立扩展/独立部署的必要（例如自建 OCR 服务），再新增为 `services/` 下的兄弟目录，当前不预留空目录。

## 与前端的关系

`app/admin` 与 `app/workstation` 均通过 `services/api` 暴露的 HTTP 接口交互，不直接访问数据库或对象存储。
