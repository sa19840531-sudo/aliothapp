# Third Screenshot Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining P1 findings from the third screenshot review so `dev_1.5` no longer publishes unrecoverable reports, unsafe Android completion, or broken production Compose examples.

**Architecture:** Keep the existing monolith and local module boundaries. Completion Gate fixes stay in `worker.py` and the PostgreSQL finalization repository; Android safety stays in runtime observation and mobile demo agent guard code; deployment fixes stay under `infra`.

**Tech Stack:** Python FastAPI backend, PostgreSQL finalization repository, Redis queue short-term cache, Android UIAutomator parsing, Docker Compose, pytest, Ruff.

## Global Constraints

- All comments must be Chinese and files must remain UTF-8.
- No database migration is introduced in this plan.
- Redis is not a business fact source; recovery must be driven by PostgreSQL where possible.
- Model JSON is not a UI fact source; Android completion and dangerous tap release must use runtime observation.
- Production Compose examples must pass `docker compose config` with the checked-in example env file.
- Each batch must run targeted tests, inspect `git diff`, and only commit after self-review finds no blocking issue.

---

### Task 1: Completion Gate Recovery and Final Report Binding

**Files:**
- Modify: `services/api/app/worker.py`
- Modify: `services/api/app/infrastructure/db/task_finalization_repository.py`
- Test: `services/api/tests/test_worker.py`
- Test: `services/api/tests/test_task_finalization_persistence.py`

**Interfaces:**
- Consumes: `TaskFinalizationService.list_recoverable_finalizations(limit: int)`, `AgentRunStateStore.list_finished_records(task_ids: list[str], limit: int)`, `_generate_final_report(..., finalized=True)`.
- Produces: `_retry_pending_task_finalizations(...)` behavior that recovers `open`, `exploration_draining`, `session_draining`, and `sealed` tasks without binding preview reports.

- [ ] **Step 1: Write failing recovery tests**

Add tests that prove:

```python
async def test_final_report_failure_keeps_open_task_recoverable(...):
    # final report generation fails after preview; retry scanner must see the task again
    # and not require Redis-only state to recover it.
```

```python
async def test_completion_gate_retry_regenerates_final_for_sealed_preview_report(...):
    # existing sealed task with preview report must call generator finalized=True
    # before binding; preview report must not be upgraded by metadata mutation.
```

```python
def test_recovery_scan_includes_open_tasks_without_report_binding() -> None:
    # inspect repository source and assert finalization_state = 'open' is included.
```

- [ ] **Step 2: Run targeted tests and verify red**

Run:

```bash
pytest services/api/tests/test_worker.py::test_final_report_failure_keeps_open_task_recoverable services/api/tests/test_worker.py::test_completion_gate_retry_regenerates_final_for_sealed_preview_report services/api/tests/test_task_finalization_persistence.py::test_recovery_scan_includes_open_tasks_without_report_binding -q
```

Expected: new tests fail because open is not scanned and sealed preview is bound directly.

- [ ] **Step 3: Implement recovery fix**

Update `list_recoverable_finalizations` to include open tasks that have a finished Agent Run without a bound report, keep `exploration_draining`, `session_draining`, and `sealed` cases. In `_retry_pending_task_finalizations`, when snapshot is `SEALED`, regenerate final report unless `record.final_report` is a dict with `report_stage == "final"` and `is_final is True`; never call `_attach_completion_gate` on a preview-only payload before regeneration.

- [ ] **Step 4: Run targeted tests and self-review**

Run the tests from Step 2 plus:

```bash
pytest services/api/tests/test_worker.py::test_completion_gate_retry_uses_durable_session_draining_backlog services/api/tests/test_worker.py::test_report_binding_failure_keeps_sealed_report_recoverable services/api/tests/test_task_finalization_persistence.py::test_recovery_scan_includes_sealed_tasks_without_report_binding -q
ruff check services/api/app/worker.py services/api/app/infrastructure/db/task_finalization_repository.py services/api/tests/test_worker.py services/api/tests/test_task_finalization_persistence.py
git diff --check
```

Review the diff for: no sealed post-write, no preview-to-final mutation, no Redis-only recovery dependency.

- [ ] **Step 5: Commit Task 1**

```bash
git add services/api/app/worker.py services/api/app/infrastructure/db/task_finalization_repository.py services/api/tests/test_worker.py services/api/tests/test_task_finalization_persistence.py
git commit -m "[AI编程] fix: 修复Completion Gate恢复与报告绑定；模块：Worker/Task Finalization；数据库迁移：否；前端菜单路由：否；测试风险：真实PG恢复矩阵需现场验证"
```

### Task 2: Android Runtime Completion and Tap Guard

**Files:**
- Modify: `services/api/app/api/v1/mobile_demo_agent_ad_capture.py`
- Modify: `services/api/app/infrastructure/device/android_runtime_observation.py`
- Test: `services/api/tests/test_mobile_demo_agent_ad_capture.py`
- Test: `services/api/tests/test_android_runtime_observation.py`

**Interfaces:**
- Consumes: `AndroidRuntimeObservation`, `AndroidUiNode`, `parse_android_ui_hierarchy`.
- Produces: package-aware and content-aware runtime observation; broader forbidden tap semantics.

- [ ] **Step 1: Write failing Android tests**

Add tests that prove:

```python
def test_block_purchase_and_subscribe_taps() -> None:
    assert action_blocked({"action": "tap", "target_hint": "purchase"}) is not None
    assert action_blocked({"action": "tap", "reason": "立即购买"}) is not None
```

```python
def test_webview_only_observation_does_not_exhaust_tabs() -> None:
    # a single android.webkit.WebView root with no visible content node must not be safe_tabs_exhausted.
```

```python
def test_completion_gate_rejects_runtime_observation_from_other_package() -> None:
    # state based on non-target package cannot complete.
```

- [ ] **Step 2: Run targeted tests and verify red**

Run:

```bash
pytest services/api/tests/test_mobile_demo_agent_ad_capture.py::test_block_purchase_and_subscribe_taps services/api/tests/test_android_runtime_observation.py::test_webview_only_observation_does_not_exhaust_tabs services/api/tests/test_mobile_demo_agent_ad_capture.py::test_completion_gate_rejects_runtime_observation_from_other_package -q
```

Expected: new tests fail because semantics and runtime observation are too permissive.

- [ ] **Step 3: Implement Android fix**

Add purchase/subscribe/buy terms to forbidden hints. Extend `AndroidRuntimeObservation` with enough facts to distinguish no-footer content from an empty WebView or foreign package. Make `_update_state_after_runtime_observation` accept the target package and only mark `no_footer` when the observation is from the target package and has visible meaningful content.

- [ ] **Step 4: Run targeted tests and self-review**

Run:

```bash
pytest services/api/tests/test_mobile_demo_agent_ad_capture.py services/api/tests/test_android_runtime_observation.py services/api/tests/test_android_capture_runtime.py -q
ruff check services/api/app/api/v1/mobile_demo_agent_ad_capture.py services/api/app/infrastructure/device/android_runtime_observation.py services/api/tests/test_mobile_demo_agent_ad_capture.py services/api/tests/test_android_runtime_observation.py
git diff --check
```

Review the diff for: no model self-attestation, no package-blind completion, no unsafe tap fallback.

- [ ] **Step 5: Commit Task 2**

```bash
git add services/api/app/api/v1/mobile_demo_agent_ad_capture.py services/api/app/infrastructure/device/android_runtime_observation.py services/api/tests/test_mobile_demo_agent_ad_capture.py services/api/tests/test_android_runtime_observation.py
git commit -m "[AI编程] fix: 收紧Android完成门禁与高风险Tap；模块：Mobile Demo/Android Runtime；数据库迁移：否；前端菜单路由：否；测试风险：真实设备UIAutomator需现场验证"
```

### Task 3: Compose Examples, Sandbox Token, and Rules

**Files:**
- Modify: `infra/env/compose.production.example`
- Modify: `infra/env/.env.emulator-sandbox.example`
- Modify: `scripts/start-emulator-sandbox.sh`
- Modify: `docs/专项能力与高频回归规则.md`
- Test: existing shell/Compose checks

**Interfaces:**
- Consumes: `ALIOTH_MOBILE_DEMO_TRUSTED_PROXY_ADDRESSES`, `ALIOTH_MOBILE_DEMO_TRUSTED_PROXY_NETWORKS`, `ALIOTH_MOBILE_DEMO_HOST_TOKEN`.
- Produces: checked-in examples that fail early for placeholders but pass Compose expansion.

- [ ] **Step 1: Write or identify config checks**

Use shell syntax and Compose config as the regression checks:

```bash
bash -n scripts/start-emulator-sandbox.sh
docker compose --env-file infra/env/compose.production.example -f infra/docker-compose.production.yml config --quiet
```

- [ ] **Step 2: Implement config and rule updates**

Add proxy variables to `compose.production.example`. Make sandbox token checks strip single quotes, double quotes, and whitespace before comparing placeholders. Update `docs/专项能力与高频回归规则.md` with the new reusable rules for final-report recovery, Android no-footer evidence, purchase/subscribe tap blocking, and production proxy examples.

- [ ] **Step 3: Run config checks and self-review**

Run:

```bash
bash -n scripts/start-emulator-sandbox.sh
docker compose --env-file infra/env/compose.production.example -f infra/docker-compose.production.yml config --quiet
docker compose --env-file infra/env/.env.emulator-sandbox.example -f infra/docker-compose.emulator-sandbox.yml config --quiet
git diff --check
```

Review the diff for: no real secrets, no example self-reference, no broken Compose interpolation.

- [ ] **Step 4: Commit Task 3**

```bash
git add infra/env/compose.production.example infra/env/.env.emulator-sandbox.example scripts/start-emulator-sandbox.sh docs/专项能力与高频回归规则.md
git commit -m "[AI编程] fix: 同步生产与沙盒配置门禁；模块：Infra/Sandbox/专项规则；数据库迁移：否；前端菜单路由：否；测试风险：真实生产网络需现场验证"
```

### Task 4: Final Verification

**Files:**
- Read-only verification of the branch.

**Interfaces:**
- Consumes: all commits from Tasks 1-3.
- Produces: final evidence summary.

- [ ] **Step 1: Run full backend tests**

```bash
pytest services/api/tests -q
```

- [ ] **Step 2: Run frontend checks if package exists**

```bash
npm.cmd test
npm.cmd run build
```

- [ ] **Step 3: Run final static checks**

```bash
ruff check services scripts
git status --short --branch
git log --oneline -5
```

- [ ] **Step 4: Report residual risk**

Report any untracked user files, tests not run, and remaining risks requiring real PostgreSQL, real Android devices, or production networks.
