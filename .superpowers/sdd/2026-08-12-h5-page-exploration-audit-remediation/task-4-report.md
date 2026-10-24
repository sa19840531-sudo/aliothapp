# Task 4 实施报告：H5 全局 Frontier 与来源分支恢复

## 结果

已将 H5 单次页面的 local frontier 接入版本化任务级 Frontier 快照。首 Work 与每个动作后继 Work 都在 `observation.metadata["h5_task_frontier"]` 持久化完整快照；已执行候选、未执行 parent sibling、当前页面 child candidates 与恢复尝试状态均由不可变领域转换生成。

恢复编排新增在 `services/api/app/application/h5_exploration_recovery.py`。它仅依赖最小 H5 浏览器端口：来源状态为历史祖先时优先 `back()`，其他同源来源使用 `navigate(destination_url)`；两条路径均强制按 `capture → detect → assess` rebaseline 后才返回可执行结果。模块不依赖 Playwright 基础设施，避免新增第二条浏览器实现路径；运行时可复用既有 H5 Capture Coordinator、页面检测和 Assessment 端口提供该适配。

恢复尝试上限为 2。新增的 `H5TaskFrontierSnapshot.record_restoration_attempt(...)` 是计划中 Task 3 已有快照接口未列出的最小直接领域扩展：它会重算快照 hash，并在 Work round-trip 后保留计数。达到上限时 `apply_h5_frontier_restoration_result(...)` 以稳定原因 `h5_frontier_restore_exhausted` 将 selected sibling 标记为 `failed`。

## RED

执行命令（环境没有 `python` 命令，改用 Codex 运行时解释器）：

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_acceptance_closure.py -q
```

结果：`4 failed, 32 passed`。

- `record_restoration_attempt` 不存在；
- successor Work 未携带 `h5_task_frontier`；
- `h5_exploration_recovery` 模块不存在。

这些失败均对应缺失功能，而非测试装配错误。

## GREEN 与验证

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_acceptance_closure.py -q
```

结果：`37 passed`（有 2 条 Agent Framework 既有 experimental warning）。

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py -q
```

结果：`43 passed`（有相同的 2 条既有 warning）。

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff check services/api/app/domain/exploration/h5_frontier.py services/api/app/application/h5_exploration_recovery.py services/api/app/application/h5_exploration_workflow.py services/api/app/application/h5_exploration_turn.py services/api/app/infrastructure/db/explorer_work_repository.py services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py
git diff --check
```

结果：Ruff `All checks passed!`；`git diff --check` 无输出。

## 变更

- `h5_frontier.py`：增加不可变恢复尝试计数 API。
- `h5_exploration_workflow.py`：创建/合并/选择任务级快照，后继只暴露 snapshot 的 selected candidate，同时保留其他 sibling 与 child candidates。
- `h5_exploration_turn.py`：提交前校验局部候选与任务快照 selected entry 一致。
- `h5_exploration_recovery.py`：新增同源来源恢复、强制 rebaseline、两次上限与失败写回。
- `explorer_work_repository.py`：从数据库 payload 回读 Work 时验证 H5 元数据快照，确保 metadata 不被无效 payload 静默接受。
- 测试覆盖：parent sibling、child candidate、Work 序列化回读、ancestor back 顺序、同源恢复上限、耗尽后 stable failed reason、恢复尝试 hash/round-trip。

## 风险与环境门禁

- 实际 Playwright 历史栈与 `back()` 可用性的行为尚需浏览器 E2E 验证。
- 本任务的恢复模块是端口化编排；生产运行时应注入既有 Capture Coordinator、H5 页面检测和 Assessment 适配，不能绕开它们直接驱动 Playwright。
- PostgreSQL 的真实 JSONB 往返与并发 successor 提交仍需集成环境验证；代码层已通过 `ExplorationWork` 序列化/回读及 repository payload 校验。

## Fix round 1：独立审查闭环

### RED

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_exploration_turn.py::test_h5_runner_restores_selected_sibling_before_explorer_action services/api/tests/test_h5_exploration_turn.py::test_restoration_rejects_same_url_when_rebaseline_state_differs services/api/tests/test_h5_runtime_composition.py::test_h5_completion_wrapper_rejects_delegate_candidate_other_than_selected_frontier_entry -q
```

结果：`3 failed`。

- Runner 不接受 recovery 端口，来源不同的 selected sibling 会直接进入 Explorer；
- 同 URL 的登录/弹窗状态仍被恢复模块认定成功；
- delegate 可返回非 selected candidate 的 `ActionIntent`。

补充 RED（评审对 accepted 的语义要求）：

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_exploration_turn.py::test_restoration_rejects_accepted_assessment_with_auth_page_status -q
```

结果：`1 failed, 11 passed`。适配本身已完成（`execution_status=accepted`）但其 `page_type_assessment.status=requires_auth` 时，旧逻辑会错误放行。

### GREEN

- 新增 `H5CoordinatorFrontierRecovery`，由 `PostgresH5RuntimeProvider` 注入 `H5ExplorationTurnRunner`。恢复 BACK/NAVIGATE 均使用 `CaptureCoordinator` 的 `ActionIntent`，因而继承 Session Gate 和 before/action/after 持久化证据边界；没有借用 `_browser`。
- Runner 在 Explorer 副作用前检测来源状态不同的 selected entry；恢复成功才把 rebaseline Capture 构成的 in-memory Work 交给 Explorer。新的 ActionIntent 会以 rebaseline capture 为 source。
- 恢复失败更新仍处于持有租约的 Work payload；首次失败进入 `retry_wait`，第二次快照变为 `failed` 并以 `h5_frontier_restore_exhausted` 进入 Work failed，二者均不执行 Explorer candidate。
- 恢复后依据 `build_h5_frontier(...)` 的同一 state-version 规则比较 entry 来源状态；同 URL 状态漂移、assessment blocked、或 accepted 对应的页面状态不是 `covered` 时均拒绝；`skipped` 保留既有的非业务覆盖页面语义。
- `H5CompletionAwareExplorer` 在 delegate 返回后校验 metadata selected entry、唯一 snapshot selected entry、局部候选和 `ActionIntent.parameters.candidate_id` 完全一致；`_selected_entry_id` 不再盲信 metadata。
- PostgreSQL repository 新增受 status、row version、lease token 和有效租约共同保护的 `update_payload`，用于恢复尝试跨 Work round-trip 持久化。

最终命令：

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py services/api/tests/test_h5_acceptance_closure.py -q
```

结果：`89 passed`，仅有 2 条既有 Agent Framework experimental warning。

真实 Playwright 历史栈、真实 PostgreSQL 并发更新仍为环境门禁；本轮未将代码测试表述为生产验收。

## Fix round 2：恢复审计持久化与完整语义边界

### RED

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_exploration_turn.py::test_h5_runner_restores_selected_sibling_before_explorer_action services/api/tests/test_h5_exploration_turn.py::test_h5_runner_persists_successful_recovery_before_terminal_transition -q
```

结果：`2 failed`。成功恢复仅替换内存 Work，未在 Explorer 副作用或 terminal transition 前写回 payload，后续提交仍使用旧 row version。

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_exploration_turn.py::test_restoration_rejects_accepted_assessment_without_payload services/api/tests/test_h5_exploration_turn.py::test_restoration_rejects_covered_popup_rebaseline -q
```

结果：`2 failed`。缺少 assessment payload 的 accepted 和 overlay popup 都被旧恢复路径放行。

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_runtime_composition.py::test_h5_completion_wrapper_rejects_selected_id_with_changed_action_semantics -q
```

结果：`3 failed`。同一 `candidate_id` 可篡改 `action_type`、`semantic_target` 或 navigate 的 `destination_url` 后进入协调器。

### GREEN

- `H5ExplorationTurnRunner` 在成功恢复后、任何 Explorer 副作用前，使用活跃 `RUNNING` 租约的 `update_payload` 持久化完整 rebaseline Work；返回的 row version 被统一传入后续 successor 提交、terminal transition 和异常转移。
- 保留失败恢复既有 writeback；成功/失败路径均写入 snapshot 的恢复次数。repository 契约测试确认 JSONB payload 含新的 `source_capture_id` 和 `restoration_attempt_count`。
- assessment 改为 fail-closed：未知/缺失状态拒绝；accepted 必须有 `covered` 且带页面类型、原因、页面 Capture、证据、页面状态等审计字段；skipped 仍经状态匹配及 auth/overlay gate。overlay 或 `requires_auth` 统一返回稳定 `h5_frontier_restore_rebaseline_blocked`。
- 副作用前校验扩展为完整 CandidateAction 语义：候选 ID、操作、目标、风险、Surface、深度、受控结构化参数、期望页面类型，以及 NAVIGATE 的非空且完全匹配 `destination_url`。未扩展共享 executor 或 App 行为。

最终命令：

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py services/api/tests/test_h5_acceptance_closure.py -q
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff check services/api/app/application/h5_exploration_recovery.py services/api/app/application/h5_exploration_turn.py services/api/app/application/h5_runtime_composition.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py
git diff --check
```

最终结果：`97 passed`，仅有 2 条既有 Agent Framework experimental warning；Ruff 与 `git diff --check` 通过。仍需以真实 Playwright 历史栈和 PostgreSQL 并发租约进行环境级验证。

## Fix round 3：封闭 H5 ActionIntent 参数绑定

### RED

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_runtime_composition.py::test_h5_completion_wrapper_rejects_selected_id_with_changed_action_semantics services/api/tests/test_maf_explorer_agent.py::test_h5_maf_explorer_uses_selected_candidate_purpose_not_model_text -q
```

结果：`5 failed, 3 passed`。

- 相同候选 ID、操作、目标和风险仍可附加 `clear_obstructions=true` 或 `capture_screenshot=false`；
- 候选声明期望页面类型时，intent 可省略该字段；
- intent purpose 可被模型改写；
- H5 Agent 仍把模型 purpose 放进最终 ActionIntent。

### GREEN

- 以 CandidateAction 的真实持久化字段构造封闭参数集：`candidate_id`、当前 `frontier_snapshot_id`、`navigation_depth`；候选声明时强制 `expected_next_page_type`；仅 NAVIGATE 强制且精确匹配 `destination_url`。任何额外、遗漏或值漂移均在 `H5CompletionAwareExplorer` 返回意图前拒绝，因此不会到达既有 Coordinator。
- H5 页面判定候选只持久化 purpose、target_keywords、expected page type 与可选 destination；未持久化 `action_parameters`，故不能由模型或手工构造的 CandidateAction 授权执行/解析参数。
- H5 Agent 对 task frontier Work 从持久化 candidate 取 purpose（缺失时使用固定安全默认），并在候选声明期望页面类型时生成该参数；非 H5 Work 保留模型 purpose 行为。
- 新增合法闭合标准 intent 通过测试；未改 App 执行路径或共享 executor。

最终命令：

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py services/api/tests/test_h5_acceptance_closure.py services/api/tests/test_maf_explorer_agent.py -q
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff check services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_runtime_composition.py services/api/app/agents/explorer/maf_agent.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_maf_explorer_agent.py
git diff --check
```

最终结果：`122 passed`；Ruff 与 `git diff --check` 将在提交前复跑。真实浏览器与数据库并发环境风险保持不变。

## Fix round 4：闭合参数集变异回归合同

### RED 逻辑证据

本轮复审已确认运行时代码满足闭合参数绑定，故遵循审查允许项，未为制造 RED
故意退化生产校验。新增测试按变异思路分别锁定下列退化：只检查参数键是否存在而不比较
`frontier_snapshot_id`、`navigation_depth` 或 `risk_level` 的真实值；以及允许未知嵌套参数或
目标解析替代键。任一该类旧退化校验都会让对应测试返回可执行 `ActionIntent`，从而使本轮
`pytest.raises(ValueError)` 合同失败。

### GREEN

新增独立参数化反例：错误 `frontier_snapshot_id`、错误 `navigation_depth`、风险等级漂移、
嵌套 `clear_obstructions`，以及替代目标解析键 `target_selector`。它们均在
`H5CompletionAwareExplorer` 返回决策前被拒绝，因而不会向下游 Coordinator 交付可执行
intent。原有的 action type、target、destination、遗漏期望页面类型和 purpose 反例继续
覆盖；同时分别保留闭合 CLICK 与 NAVIGATE 正向用例。

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_runtime_composition.py -q
```

结果：`22 passed`，仅有 2 条 Agent Framework 既有 experimental warning。

本轮仅补强测试合同，未改生产行为、数据库迁移或前端菜单/路由。

最终回归命令复跑：

```powershell
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m pytest services/api/tests/test_h5_task_frontier.py services/api/tests/test_h5_exploration_observation.py services/api/tests/test_h5_exploration_workflow.py services/api/tests/test_h5_exploration_turn.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_v2_control_plane_repositories.py services/api/tests/test_h5_acceptance_closure.py services/api/tests/test_maf_explorer_agent.py -q
& 'C:\Users\40389\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe' -m ruff check services/api/app/application/h5_exploration_observation.py services/api/app/application/h5_runtime_composition.py services/api/app/agents/explorer/maf_agent.py services/api/tests/test_h5_runtime_composition.py services/api/tests/test_maf_explorer_agent.py
git diff --check
```

结果：`127 passed`，仅有 2 条既有 Agent Framework experimental warning；Ruff 通过，
`git diff --check` 无错误（仅 Windows 工作区的 LF/CRLF 提示）。真实 Playwright 与
PostgreSQL 并发环境仍需 E2E 验证。
