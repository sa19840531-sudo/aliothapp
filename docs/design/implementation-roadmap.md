# Alioth 实现落地路线

> 日期：2026-07-03
> 状态：M0 实施基线
> 依据：[2026-07-03-alioth-integrated-solution-design.md](2026-07-03-alioth-integrated-solution-design.md)

## 1. 落地原则

当前仓库已经完成目录骨架初始化，下一步目标不是继续扩写架构，而是把设计收口为可运行工程基线。

落地采用“执行形态先轻、数据模型先稳”的方式：

| 原则 | 说明 |
| --- | --- |
| V1 先轻量执行 | 先以单 Main Agent / Runtime mock / Skill mock 跑通链路，不直接实现完整多 Agent Orchestrator |
| 结果模型先统一 | V1 起直接使用 `qc_rule_execution` 记录规则完整执行状态，`qc_finding` 仅作兼容展示或历史别名 |
| 证据链路先标准化 | 先建 `qc_evidence_artifact`、`qc_observation_packet`、`qc_rule_execution`，再接真实 Playwright 采集 |
| Agent 留痕先落库 | 所有 Agent 能力选择、Skill 读取或 Tool 调用写入 `qc_agent_run` 与 `qc_agent_action_trace`，不依赖模型上下文保存状态 |
| 前端先做工作台 | 先实现 `workstation` 的任务、结果、审核视图，`admin` 配置后台后置 |

## 2. M0 文档与契约收口

目标：让开发者明确第一版要建哪些表、哪些接口、哪些页面。

任务：

| 任务 | 代码落点 | 验收标准 |
| --- | --- | --- |
| 修正整合设计文档表格 | `docs/design/2026-07-03-alioth-integrated-solution-design.md` | 0.1 表格渲染正确 |
| 明确 V1 结果表策略 | 同上 | 文档明确 V1 主写 `qc_rule_execution`，不再新增 `qc_finding` 主写路径 |
| 新增落地路线文档 | `docs/design/implementation-roadmap.md` | M0 至 M6 任务、落点、验收标准清晰 |
| 更新设计文档索引 | `docs/design/README.md` | 索引能直接找到本路线文档 |

## 3. M1 后端工程基线

目标：让 `services/api` 从 README 骨架变成可启动 FastAPI 项目。

任务：

| 任务 | 代码落点 | 验收标准 |
| --- | --- | --- |
| 初始化 Python 项目配置 | `pyproject.toml` | 明确 FastAPI、Pydantic、pytest、SQLAlchemy、Alembic 等基础依赖 |
| 创建 FastAPI 入口 | `services/api/app/main.py` | 应用可启动 |
| 创建 v1 路由聚合 | `services/api/app/api/v1/router.py` | `/api/v1` 统一挂载 |
| 创建公共配置 | `services/api/app/core/config.py` | 环境变量有统一读取入口 |
| 创建健康检查 | `services/api/app/api/v1/health.py` | `GET /api/v1/health` 返回正常 |
| 创建最小测试 | `services/api/tests/test_health.py` | `pytest` 可跑通 |

## 4. M2 数据库核心表

目标：先落任务、规则、证据、审核、Agent trace 主链。

第一批表：

| 表 | 所属领域 | 说明 |
| --- | --- | --- |
| `qc_ruleset` | rule | 规则集版本快照单位 |
| `qc_rule` | rule | 质检规则主表，包含 `category`、`executor_type`、`page_type`、`applicable_page_types`、`check_point` |
| `qc_task` | task | 质检任务主表 |
| `qc_page_capture` | task | 页面基线采集记录 |
| `qc_evidence_artifact` | evidence | 截图、DOM、OCR、HAR、console、AI IO 等证据产物 |
| `qc_observation_packet` | evidence | 规则判定的标准证据输入包 |
| `qc_rule_execution` | evidence | 每条规则的完整执行结果 |
| `qc_review_item` | review | 人工审核项 |
| `qc_agent_run` | task | Agent 运行实例 |
| `qc_agent_action_trace` | task | Agent 能力选择与单步执行留痕 |
| `qc_agent_capability_registry` | integration | Agent 可见能力名称、类型、Schema、权限、版本 |

验收标准：

| 验收项 | 要求 |
| --- | --- |
| 迁移可执行 | Alembic migration 能创建第一批核心表 |
| 状态枚举明确 | `qc_rule_execution.execution_status` 覆盖 `pass/fail/warning/manual_review/evidence_insufficient/skipped/error` |
| 留痕字段完整 | `qc_agent_action_trace` 至少包含 step、selected_capability、capability_input、capability_output、memory_delta、cost、status |
| Agent 能力权限可控 | `qc_agent_capability_registry.permission_level` 支持 `low/medium/high_risk` |

## 5. M3 最小 API 闭环

目标：先不接真实 Agent，也能通过 API 模拟任务执行结果。

第一批接口：

| 接口 | 说明 |
| --- | --- |
| `POST /api/v1/rulesets` | 创建规则集 |
| `POST /api/v1/rules` | 创建规则 |
| `POST /api/v1/tasks` | 提交质检任务 |
| `GET /api/v1/tasks` | 查询任务列表 |
| `GET /api/v1/tasks/{task_id}` | 查询任务详情 |
| `GET /api/v1/tasks/{task_id}/rule-executions` | 查询规则执行结果 |
| `GET /api/v1/tasks/{task_id}/agent-traces` | 查询 Agent 执行留痕 |
| `GET /api/v1/review-items` | 查询审核队列 |
| `POST /api/v1/review-items/{id}/approve` | 审核确认 |
| `POST /api/v1/review-items/{id}/reject` | 审核驳回 |
| `POST /api/v1/review-items/{id}/needs-recheck` | 标记需复核 |

验收标准：

| 验收项 | 要求 |
| --- | --- |
| 任务链路可模拟 | 能创建规则、创建任务、写入规则执行结果、生成审核项 |
| 接口职责清晰 | 路由只做协议转换，业务逻辑进入 `application/` |
| 前端可对接 | Workstation 能基于接口展示任务状态、结果和审核项 |

## 6. M4 Agent Runtime 最小版

目标：先实现受控 Agent Loop，不接真实大模型和 Playwright。

任务：

| 任务 | 代码落点 | 验收标准 |
| --- | --- | --- |
| Runtime 状态模型 | `services/api/app/agents/runtime/` | 定义 Working Memory、Loop Guard、Action Trace |
| Mock Tool 执行器 | `services/api/app/agents/tools/` | 能按 `qc_agent_capability_registry` 契约执行 mock tool |
| Skill 加载 | `services/api/app/agents/skills/` | 能加载 phase/rule Markdown |
| 任务触发 Runtime | `services/api/app/application/` | 创建任务后可启动 mock runtime |
| Trace 写入 | `domain/task` + `infrastructure/db` | 每一步写 `qc_agent_action_trace` |

验收标准：

| 验收项 | 要求 |
| --- | --- |
| Mock 链路可跑 | 一个 mock task 能执行 3 至 5 步 |
| 护栏生效 | 超过 `max_steps` 自动停止并标记状态 |
| 无进展可检测 | 连续相同能力 + 相同参数且无新信息时标记 `no_progress` |

## 7. M5 Playwright 与证据采集

目标：接入真实页面采集，但保持受控工具边界。

任务：

| 任务 | 代码落点 | 验收标准 |
| --- | --- | --- |
| Browser 封装 | `services/api/app/infrastructure/browser/` | 页面操作通过受控接口执行 |
| Storage 封装 | `services/api/app/infrastructure/storage/` | 证据产物统一返回对象路径 |
| Baseline Evidence | `domain/task` + `domain/evidence` | 截图、DOM、可见文本、console log 可落库 |
| Supplemental Evidence | `domain/evidence` | 规则补采证据可关联 `qc_rule_execution` |
| Observation Packet | `domain/evidence` | Rule Judge 只消费标准证据包 |

验收标准：

| 验收项 | 要求 |
| --- | --- |
| URL 可采集 | 给定 URL 能生成页面 baseline 证据 |
| 证据可追溯 | 数据库能追到对象存储路径 |
| 判定输入统一 | `qc_observation_packet` 能引用证据集合 |

## 8. M6 Workstation 前端闭环

目标：先让质检任务闭环可视化，管理后台后置。

第一批页面：

| 页面 | 说明 |
| --- | --- |
| `tasks/new` | 提交任务 |
| `tasks` | 任务列表 |
| `tasks/:id` | 任务详情 |
| `tasks/:id/rule-executions` | 规则执行结果 |
| `tasks/:id/agent-traces` | Agent 执行留痕 |
| `review-items` | 审核队列 |

`app/shared` 第一批能力：

| 能力 | 说明 |
| --- | --- |
| API client | 统一请求封装 |
| 类型定义 | 对齐后端响应模型 |
| 枚举映射 | 任务状态、规则状态、审核状态 |
| 证据预览组件 | 截图、文本、DOM、日志入口 |

验收标准：

| 验收项 | 要求 |
| --- | --- |
| 能提交任务 | 前端能调用任务提交接口 |
| 能看执行状态 | 任务详情展示状态、规则结果、Agent trace |
| 能处理审核项 | 审核队列支持确认、驳回、需复核 |

## 9. 暂缓范围

以下内容不进入第一批实现，避免 MVP 过重：

| 暂缓项 | 原因 |
| --- | --- |
| 完整多 Agent Orchestrator DAG | 先验证 V1 Runtime 与数据模型 |
| 完整 IAM 权限体系 | 第一批只保留接口与表设计入口 |
| 完整 Agent 配置后台 | 先用静态内置 Agent/Skill 配置 |
| MCP 动态接入 | 等 Agent Capability Registry 稳定后再接 |
| V4 记忆引擎 | 依赖足够多的审核反馈与历史任务 |
| 禅道自动建单 | 依赖审核闭环稳定 |
