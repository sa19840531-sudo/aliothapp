# app/application 应用层

用例编排层（Application Service / Use Case），协调 [domain/](../domain/README.md) 与 [agents/](../agents/README.md)、[infrastructure/](../infrastructure/README.md) 完成具体业务流程，是 `api/` 路由与底层能力之间的粘合层。

## 职责

- 编排一次完整的业务用例（如"提交质检任务"：校验参数 → 锁定规则集版本 → 创建 `qc_task` → 派发给 Agent 层执行）。
- 事务边界控制（一个用例内跨多个仓储的写操作保持一致性）。
- 不包含核心业务规则（规则本身在 `domain/`），不直接处理 HTTP 细节（在 `api/`），不直接实现具体技术细节（在 `infrastructure/`）。

## 与 agents/ 的边界

- 应用层负责"业务用例何时该触发 Agent 执行"（如任务提交后派发到任务队列），不负责"Agent 内部如何思考决策"。
- Agent 内部的自主编排逻辑（Main Agent Loop、Agent Orchestrator 调度）封装在 [agents/](../agents/README.md) 内部，对应用层只暴露"启动任务"、"查询任务状态"等粗粒度接口。

## 内部约定（实现阶段落地）

- 按业务域拆分用例模块，与 `domain/` 的子域命名保持一致（如 `rule_service.py`、`task_service.py`）。
- 每个用例是一个清晰的输入输出函数/类方法，避免臃肿的"上帝服务类"。

## 页面采集持久化

- `page_capture_persistence.py` 协调页面采集产物存储与数据库元数据写入。
- 完整 DOM、截图、可见文本和浏览器日志不得进入 LLM 上下文；应用层返回真实 `artifact_id`，供规则结果引用。
- 文件存储失败或数据库写入失败时，必须清理本次已写入的文件，避免留下无数据库引用的证据产物。
