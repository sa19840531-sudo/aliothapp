# 附件审查建议全量整改实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 按附件审查报告修复当前分支的安全阻断、公共 Capture 数据链、App 完成门禁、移动 Demo 真实性与工程质量问题，并完成不调用远程 API 的本地代码审核。

**Architecture:** 保持分层单体架构，使用 `qc_capture` 作为 Web/App 共用事实源，所有设备动作由 Interaction Runtime/CaptureCoordinator 持有和执行；App Worker 只在 Inspection、Rule Judge、Review 与 Completion Gate 全部收口后完成任务。

**Tech Stack:** Python 3.12、FastAPI、PostgreSQL/Alembic、pytest/pytest-asyncio、Ruff、Vite/Vue/TypeScript。

## Global Constraints

- 默认保持 `ALIOTH_APP_QC_ENABLED=False`，在真实 PostgreSQL 链路通过前不得开启正式 App V2 流量。
- 默认不挂载 `mobile-demo`；显式开发开关和 loopback/隔离网络条件必须同时满足。
- 所有通用动作/Gate 查询以 `qc_capture` 为主表；Web 特有字段连接 `qc_page_capture`，App 特有字段连接 `qc_app_capture`。
- 所有代码注释使用中文，所有文件使用 UTF-8；不调用远程 API，不把模拟测试写成真实设备验收。
- 每个行为修改必须先有失败测试；最终必须执行全量后端测试、Ruff、Python 编译、前端测试/构建和 diff 检查。

---

### Task 1: 建立本地测试基线与协议回归门禁

**Files:**
- Modify: `pyproject.toml`
- Create: `services/api/tests/test_review_regression_contracts.py`
- Create: `docs/superpowers/sdd/2026-08-06-full-review-remediation-plan/progress.md`

**Interfaces:**
- Produces deterministic local test commands using the bundled Python runtime plus `.venv/Lib/site-packages`.
- Produces static contract checks for `CaptureRef`, mobile-demo route mounting, no external API execution, and current migration dependency order.

- [ ] **Step 1: Write failing contract tests** covering default Demo disabled, `capture_id` as the only current runtime field, and explicit Pillow test dependency.
- [ ] **Step 2: Run the focused tests with workspace-local `--basetemp` and record the expected failures.**
- [ ] **Step 3: Add only the missing dependency/configuration and contract assertions.**
- [ ] **Step 4: Re-run focused tests and preserve the baseline result in the ledger.**

### Task 2: Close the mobile-demo security boundary

**Files:**
- Modify: `services/api/app/core/config.py`
- Modify: `services/api/app/main.py`
- Modify: `services/api/app/api/v1/router.py`
- Modify: `services/api/app/api/v1/mobile_demo.py`
- Modify: `services/api/app/application/app_screenshot_artifacts.py`
- Create: `services/api/tests/test_mobile_demo_security_boundary.py`

**Interfaces:**
- `WorkflowSettings.mobile_demo_enabled` defaults to `False`.
- `create_app()` conditionally mounts `mobile_demo_router` only after explicit configuration; the access dependency rejects non-loopback requests in development and every request in production without authenticated operations policy.
- Upload validation consumes an async stream and returns an opaque artifact/run response without `storage_path`.

- [ ] **Step 1: Add failing tests for default 404, non-loopback rejection, oversized/invalid APK rejection, streamed staging cleanup, and path-redacted responses.**
- [ ] **Step 2: Run the focused security tests and verify they fail for the current anonymous route and one-shot upload.**
- [ ] **Step 3: Implement settings, conditional route mounting, loopback guard, streamed APK staging/validation, and response redaction.**
- [ ] **Step 4: Run security tests plus existing Demo API tests and fix only regressions caused by the boundary.**

### Task 3: Repair public Capture SQL and persistence idempotence

**Files:**
- Modify: `services/api/app/infrastructure/db/action_execution_repository.py`
- Modify: `services/api/app/infrastructure/db/evidence_exit_gate_repository.py`
- Modify: `services/api/app/infrastructure/db/app_action_capture_repository.py`
- Modify: `services/api/app/application/app_capture_persistence.py`
- Modify: `services/api/app/domain/task/targets.py`
- Modify: `services/api/app/api/v1/tasks.py`
- Modify: `services/api/app/infrastructure/db/task_repository.py`
- Modify: `services/api/migrations/versions/20260804_0034_common_capture_identity.py`
- Modify: `services/api/migrations/versions/20260804_0035_app_workflow_runtime.py`
- Create: `services/api/tests/test_common_capture_runtime_sql_contract.py`
- Modify: `services/api/tests/test_app_postgres_integration.py`

**Interfaces:**
- SQL joins begin at `qc_capture`, constrain `surface_kind` and `capture_status`, and add the relevant Web/App detail join only when needed.
- App Capture persistence accepts stable `action_execution_id + capture_role` identity and returns the existing fact on an identical retry; content drift raises a persistence conflict.
- The integration test is opt-in through explicit `ALIOTH_TEST_DATABASE_URL`; it never contacts a remote database.

- [ ] **Step 1: Add failing SQL/static and fake-connection tests for App Capture through Gate/Action before/after and duplicate retry.**
- [ ] **Step 2: Run the focused tests and confirm they detect `qc_page_capture` consumers and random retry IDs.**
- [ ] **Step 3: Update SQL, constraints/indexes, and persistence idempotence with transaction/lease checks.**
- [ ] **Step 4: Run focused fake tests and the opt-in local PostgreSQL test when a local URL is available; otherwise record an explicit skip.**

### Task 4: Fix App completion semantics and restart recovery

**Files:**
- Modify: `services/api/app/application/app_runtime_composition.py`
- Modify: `services/api/app/worker.py`
- Modify: `services/api/app/application/app_workflow.py`
- Modify: `services/api/app/infrastructure/app_runtime/official_api.py`
- Modify: `services/api/app/infrastructure/app_runtime/fake.py`
- Modify: `services/api/app/application/app_interaction_runtime.py`
- Create or modify: `services/api/tests/test_app_completion_gate_integration.py`
- Modify: `services/api/tests/test_app_workflow_runtime_migration.py`
- Modify: `services/api/tests/test_official_app_api_contract.py`

**Interfaces:**
- Explorer terminal results update exploration state only.
- App Worker routes terminal work through the existing Inspection/Rule Judge/Review/Completion Gate ports and writes completed only with `gate_snapshot_id` and `finalization_epoch`.
- Official and Fake App Runtime expose list/validate/reattach contracts; resume failure is retryable recovery state.
- Capture command IDs include action execution identity and phase; same action retry is idempotent while the next action gets a new key.

- [ ] **Step 1: Add failing tests proving Explorer terminal alone cannot complete a Run, multi-round screenshot keys differ, and restart can reattach a persisted session.**
- [ ] **Step 2: Run the focused tests to capture current false completion, fixed screenshot keys, and in-process-only recovery.**
- [ ] **Step 3: Implement the shared App completion path, reattach contract, and per-action command identity.**
- [ ] **Step 4: Run App workflow, completion gate, runtime, and worker tests.**

### Task 5: Make Android/Demo execution truthful and bounded

**Files:**
- Modify: `services/api/app/api/v1/mobile_demo_agent_ad_capture.py`
- Modify: `services/api/app/api/v1/mobile_demo.py`
- Modify: `services/api/app/infrastructure/device/android_capture_runtime.py`
- Modify: `services/api/app/infrastructure/device/adb_ops.py`
- Modify: `services/api/app/application/app_screenshot_artifacts.py`
- Create or modify: `services/api/tests/test_mobile_demo_truthful_completion.py`
- Modify: `services/api/tests/test_mobile_demo_matrix.py`
- Modify: `services/api/tests/test_android_capture_runtime.py`
- Modify: `services/api/tests/test_agent_ad_capture_uses_runtime.py`

**Interfaces:**
- Every current `CaptureRef` consumer uses `capture_id`; a static test rejects runtime `page_capture_id` usage.
- Matrix execution calls the worker independently for each profile and records per-device evidence/status.
- Agent success requires explicit `done` and complete evidence; missing/invalid coordinates, ADB command failures, stale screenshots, and after-Capture failures cannot become success.

- [ ] **Step 1: Add failing tests for the old CaptureRef consumer, matrix first-device replication, false success after a failed later step, coordinate bounds, and ADB return-code handling.**
- [ ] **Step 2: Run the focused tests and verify the known four regressions plus new safety failures.**
- [ ] **Step 3: Implement truthful status aggregation, per-profile execution, unique session/action screenshot paths, hard coordinate validation, and compensating Artifact writes.**
- [ ] **Step 4: Run all Android/Demo tests and inspect responses for path leakage.**

### Task 6: Enforce engineering quality and local verification

**Files:**
- Modify: `pyproject.toml`
- Modify: `.gitlab-ci.yml`
- Modify: `docs/专项能力与高频回归规则.md`
- Create: `docs/2026-08-06-附件审查整改与本地验收记录.md`

**Interfaces:**
- Ruff is a mandatory local/CI check for all maintained Python files.
- The local acceptance record lists every attachment blocker/P1/P2 item, code/test evidence, and external checks intentionally not run.

- [ ] **Step 1: Add failing CI/config tests for the Ruff gate and declared test dependencies.**
- [ ] **Step 2: Run Ruff on the maintained Demo/Adapter modules and capture the complete error set.**
- [ ] **Step 3: Fix Ruff issues in maintained modules, add the dependency declarations, and keep generated/binary fixture policy explicit.**
- [ ] **Step 4: Run backend full suite, Ruff, compile checks, frontend test/build, and `git diff --check`.**
- [ ] **Step 5: Write the acceptance record from command output, not from intent.**

### Task 7: Final local code review

**Files:**
- Review: all changes from Tasks 1–6
- Modify: only files required by review findings

**Interfaces:**
- Reviewer receives the plan, ledger, and complete diff package.
- Critical/Important findings are fixed and re-reviewed before completion; external API/device checks remain explicitly out of scope.

- [ ] **Step 1: Generate a complete local review package from the branch base to `HEAD`.**
- [ ] **Step 2: Run the reviewer against security, SQL/data consistency, completion semantics, recovery/idempotence, and test evidence.**
- [ ] **Step 3: Fix and re-review all load-bearing findings.**
- [ ] **Step 4: Re-run final verification and update the ledger/acceptance record.**
