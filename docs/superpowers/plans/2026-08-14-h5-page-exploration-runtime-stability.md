# H5 页面探索需求集发布与循环收敛实施计划

> 设计依据：`docs/superpowers/specs/2026-08-14-h5-page-exploration-runtime-stability-design.md`

## Task 1：建立 RequirementSet 并发发布 RED

修改：

- `services/api/tests/test_evidence_exit_gate_persistence.py`
- `services/api/tests/test_evidence_exit_requirement_service.py`

步骤：

1. 增加仓储测试：库内 latest 与待写快照稳定 ID 相同、`created_at` 不同时应返回库内对象且不执行 INSERT。
2. 增加应用服务测试：首次 add 模拟 revision 竞争并推进 latest，服务必须重新读取并发布连续 revision。
3. 增加非冲突持久化异常测试，断言不得重试。
4. 运行两个测试文件的聚焦用例并确认失败原因只来自缺少幂等/重试语义。

## Task 2：实现 RequirementSet 有界冲突恢复

修改：

- `services/api/app/application/evidence_exit_gate.py`
- `services/api/app/application/evidence_exit_requirements.py`
- `services/api/app/infrastructure/db/evidence_exit_gate_repository.py`

步骤：

1. 在应用端口添加 `EvidenceExitRequirementRevisionConflict`。
2. 仓储锁内按稳定 ID 幂等返回；只把 stale/same-revision 竞争映射为类型化冲突。
3. 应用服务把单次发布抽成可重算尝试，仅捕获类型化冲突，最多尝试 3 次。
4. 运行 Task 1 用例、两个完整测试文件与 Ruff。

## Task 3：建立证据就绪失败与页面收敛 RED

修改：

- `services/api/tests/test_web_evidence_exit_gate.py`
- `services/api/tests/test_page_detection_tools.py`
- `services/api/tests/test_page_inspection_memory_middleware.py`

步骤：

1. readiness 测试断言发布失败返回稳定错误码、不可重试且 failure_reason 不含底层异常正文。
2. page detection 测试注入发布失败 readiness，断言识别结果改为页面错误、关闭规则匹配并标记基础设施失败。
3. WorkingMemory 中间件测试断言该错误关闭当前页并保留具体错误码。
4. 增加失败后的两条路径测试：带 `exploration_action_id` 的 Frontier capture 放行；重复失败 detection 或无 Frontier 盲采锁存 `infrastructure_unavailable`。
5. 运行聚焦用例并确认 RED。

## Task 4：实现页面探索失败显式收敛

修改：

- `services/api/app/application/capture_evidence_readiness.py`
- `services/api/app/agents/tools/page_detection_tools.py`
- `services/api/app/agents/runtime/page_inspection_memory.py`
- `services/api/app/agents/runtime/working_memory.py`
- `services/api/app/agents/runtime/business_middlewares/page_inspection_memory_middleware.py`

步骤：

1. 扩展 readiness 稳定失败合同并移除对 Agent 可见的底层异常正文。
2. 让 finalize helper 返回 readiness，并在 RequirementSet 发布失败时构造静态不可重试页面错误。
3. 让页面状态记录接受明确的 failure code/message/retryable，保证审计快照保留真实控制面失败。
4. 记录阻断当前无身份盲采的页面流失败；允许真实 Frontier 身份继续。模型重复失败 capture/detection 时锁存既有基础设施终止原因。
5. 运行 Task 3 用例、相关完整测试文件与 Ruff。

## Task 5：规则文档、验证与复审

修改：

- `docs/专项能力与高频回归规则.md`

步骤：

1. 追加 H5 页面探索 RequirementSet 并发发布与失败收敛规则。
2. 运行相关 6 文件 pytest；运行扩展页面探索/WorkingMemory 回归。
3. 运行目标 Python 文件 Ruff、compileall、`git diff --check`。
4. 使用 `requesting-code-review` 进行完成前复审；所有 Critical/Important 发现先补 RED 再修复。
5. 使用 `verification-before-completion` 新鲜复验，按 AGENTS.md 格式提交功能分支。

## Task 6：合并与交付

步骤：

1. 确认主工作区 `dev_1.5` 无用户未提交修改并拉取远程最新状态。
2. 将功能分支以非交互方式合并到 `dev_1.5`；如有冲突，只处理本次目标文件并复验。
3. 在合并后的 `dev_1.5` 重跑聚焦回归、Ruff、compileall 和差异检查。
4. 推送 `dev_1.5` 到远程；报告提交、测试证据和真实 PostgreSQL/Playwright 多 Worker 联合验收风险。
