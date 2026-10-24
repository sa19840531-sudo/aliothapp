# App Skill 路由与生产入口实施计划

> 设计依据：`docs/superpowers/specs/2026-08-05-app-skill-routing-design.md`

## 实施顺序

### 1. 建立 Workflow → Skill 的确定性策略

**文件：**

- `services/api/app/application/workflow_skill_routing.py`
- `services/api/tests/test_workflow_skill_routing.py`

**步骤：**

1. 先写测试：H5 返回 `qc-workflow`，App 返回 `app-workflow`，未知路由拒绝。
2. 实现不可变的 `WorkflowSkillSelection`/选择函数，不读取 URL、文件名或环境猜测平台。
3. 保持 `build_skills_context_provider(sub_dirs=...)` 作为唯一 Skill Provider 构造入口。

### 2. 让 Main Agent 和 Explorer 显式挂载正确 Skill

**文件：**

- `services/api/app/agents/bootstrap.py`
- `services/api/app/agents/instructions.py`
- `services/api/app/agents/explorer/bootstrap.py`
- `services/api/app/agents/explorer/maf_agent.py`
- `services/api/tests/test_agents_bootstrap.py`（按现有测试文件名调整）
- `services/api/tests/test_explorer_bootstrap.py`

**步骤：**

1. 先写测试，断言 App Explorer 的 framework Agent 收到 `app-workflow` Provider，通用 H5 Main Agent 仍收到 `qc-workflow`。
2. 为 `MafExplorerAgent`/内部 MAF Agent builder 增加 `context_providers` 注入参数。
3. `build_default_explorer_agent()` 固定传入 App Skill Provider，不允许调用默认全量目录。
4. 将 Main Agent 的默认入口明确命名为 H5/Web 入口；若保留通用构造函数，增加 target-aware 的显式参数，禁止 App 复用默认值。
5. 更新系统指令，说明当前入口的目标平台和强制 Skill，避免模型自行切换 Workflow。

### 3. 定义 App Worker Handler 与 Runtime Provider seam

**文件：**

- `services/api/app/application/app_workflow_handler.py`
- `services/api/app/application/app_runtime_provider.py`（或合并到 handler，按现有分层最小化文件）
- `services/api/tests/test_app_workflow_handler.py`

**步骤：**

1. 先写测试：注入 fake runtime 时按 Bootstrap → turn 顺序调用；取消/租约失败不调用外部 Driver；Provider 未配置时抛出配置错误。
2. 定义 `AppRuntimeProvider` Protocol，生产实现只允许返回已组合的 `AppV2Runtime`。
3. Handler 只编排 Run/Runtime，不直接导入或调用 `SurfaceDriver`；每次外部调用前执行取消和 lease 检查。
4. 统一把 Runtime 异常映射到现有 Run 失败收敛模型，禁止新增 App 专属终态。

### 4. 把 Router 接入生产 Worker

**文件：**

- `services/api/app/worker.py`
- `services/api/tests/test_worker_app_routing.py`
- `services/api/tests/test_app_architecture_boundaries.py`

**步骤：**

1. 先写测试：H5 Run 仍调用 `run_main_agent`；App Run 调用 App Handler 且不构造 Main Agent；未知/禁用路由显式失败。
2. 从锁定的 `record.task_config["_task_target"]` 还原 `TaskTarget`，校验 `workflow_version` 后调用 `AppWorkflowRouter`。
3. 将现有 `_process_one` 的 Agent 构造与执行抽成 H5 分支；App 分支调用注入的 `AppWorkflowHandler`。
4. 增加 `app_workflow_handler`/`app_runtime_provider` 可选依赖，默认从 Worker composition root 读取；未配置时返回 fail-closed 失败。
5. 保留现有 `state_store.record_started/record_finished`、取消、资源统计和进度记录逻辑。
6. 调整旧架构测试：不再断言 Worker 绝不出现 App 符号，改为断言 App 分支不得调用通用 Main Agent/Driver。

### 5. 回归、静态边界和文档

**文件：**

- `services/api/tests/test_app_workflow_skill.py`
- `services/api/tests/test_skills_loader.py`
- `docs/专项能力与高频回归规则.md`（如出现可复用边界规则）

**步骤：**

1. 运行新增测试并修复失败。
2. 运行 App 领域、Worker、架构边界和全量后端测试。
3. 运行 Ruff；确认 `alembic heads` 不受影响（本阶段不新增迁移）。
4. 检查代码搜索结果：App Worker 路径没有 `run_main_agent`，Explorer 没有 Driver import，Skill Provider 只从 loader 注入。
5. 提交时说明模块、无数据库迁移、无前端路由变化，以及 Official Adapter 集成测试尚未覆盖的风险。

## 验证命令

```powershell
cd services/api
pytest -q tests/test_workflow_skill_routing.py tests/test_explorer_bootstrap.py tests/test_app_workflow_handler.py tests/test_worker_app_routing.py tests/test_app_architecture_boundaries.py
pytest -q
ruff check app tests
alembic heads
```

