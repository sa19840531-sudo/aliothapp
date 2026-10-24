# Task 2：消费锁定的 H5 Runtime Settings

## 状态

已完成并通过自审。未引入数据库迁移，未涉及前端菜单或路由。

## 交付

- 新增 `H5TaskRuntimeSettings`，以 `record.task_config`、`record.loop_guard`、`record.page_sampling_policy` 和全局默认值构建本次 H5 Run 的唯一运行设置。
- 复用 `resolve_task_runtime_settings(defaults, loop_guard, page_sampling_policy)`，校验 scan mode、深度、页数、文本列表和页型策略矛盾；非法策略在 Worker 分派与浏览器 Runtime 工厂调用前失败。
- 移除 H5 私有 28/8/600/900 预算；执行器从 mandatory runtime settings 构造动作数、深度、持续时间和 run-start deadline，并将旅程步骤加入探索目标。
- 将 include/exclude/rule-target 页型摘要经 `RunH5WorkflowCommand` 写入首轮与后继 Frontier observation metadata，供 Task 3 消费；未提前实现 Task 3 调度。

## TDD 证据

RED：

```text
python -m pytest services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_worker_h5_v2_route.py -q --basetemp .codex-test-tmp
2 errors：缺少 services.api.app.application.h5_task_runtime_settings
```

GREEN：

```text
20 passed, 2 warnings
```

## 回归与自审

```text
python -m pytest services/api/tests -q --basetemp .codex-test-tmp-full-final
2867 passed, 21 skipped, 30 warnings in 271.05s
```

`git diff --check` 通过。独立审查发现 sampling policy summary 未传递至 Frontier；已补齐首轮和后继链路并以 workflow 测试覆盖。既有警告来自 Agent Framework 实验性 API、Starlette/httpx 弃用和测试 JWT 密钥长度。

## 风险

真实浏览器长时间执行的超时边界仍需隔离环境 E2E 验证；本任务已覆盖运行设置与组合根合同。

## Fix round 1/5：独立审查修复

### RED

```text
python -m pytest services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_exploration_workflow.py -q --basetemp .codex-test-tmp-fix1-red
3 failed
```

失败符合预期：旧 `_consume` 调用 6 次，突破 `max_actions=3` 的共享动作预算；首轮和后继 Frontier metadata 均缺少 `journey_steps`。

### GREEN

```text
python -m pytest services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_worker_h5_v2_route.py -q --basetemp .codex-test-tmp-fix1-green-2
21 passed, 2 warnings

python -m pytest services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_explorer_bootstrap.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_task_runtime_settings.py services/api/tests/test_worker_h5_v2_route.py services/api/tests/test_task_execution_policy.py -q --basetemp .codex-test-tmp-fix1-regression
54 passed, 2 warnings
```

探索和 Probe 现在以单一 `consumed < max_actions` 预算循环运行，claimed 总数不会超过锁定上限。`journey_steps` 同时进入结构化 sampling policy summary，以及首轮和后继 Frontier metadata；未实现 Task 3 调度。
