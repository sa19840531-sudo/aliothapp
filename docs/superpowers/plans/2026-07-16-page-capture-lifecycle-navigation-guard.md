# Page Capture Lifecycle And Navigation Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复补采成功后旧采集仍阻塞 Run 完成，以及非 goto 模式携带新 URL 绕过导航深度/页面数量门禁两个 P1 问题。

**Architecture:** 页面采集生命周期归属 `services/api/app/agents/runtime/page_inspection_memory.py`，以 `page_capture_id` 继续作为唯一键，但增加 active / superseded / abandoned 状态，使旧补采快照保留审计且不参与完成门禁。导航门禁归属 `TaskExecutionPolicyMiddleware`，把“是否携带新 URL”作为导航判定核心，而不是只按 `mode=goto` 或 `single_trigger+click` 分支；浏览器层同步收紧非 goto URL 语义，避免绕过中间件。

**Tech Stack:** Python 3.12, FastAPI service layer, Microsoft Agent Framework middleware, pytest.

## Global Constraints

- 不新增数据库迁移：`qc_agent_run.working_memory` 已是 JSONB。
- 不要求前端同步菜单或路由。
- 所有新增注释使用中文 UTF-8。
- 代码硬门禁优先于 Skill 文档约束；文档只描述行为，不承担安全兜底。
- 不修改当前未提交的 `services/api/tests/test_workstation_task_service.py`。

---

## File Structure

- Modify `services/api/app/agents/runtime/page_inspection_memory.py`
  - 增加页面采集生命周期字段、补采替代方法、完成门禁过滤和审计快照输出。
- Modify `services/api/app/agents/runtime/working_memory.py`
  - 暴露必要的生命周期只读/写入方法，保持门面职责单一。
- Modify `services/api/app/agents/runtime/business_middlewares/page_inspection_memory_middleware.py`
  - 在页面识别返回 ambiguous / insufficient 后保留不可规则匹配状态；新采集成功时由 WorkingMemory 自动 supersede 旧采集。
- Modify `services/api/app/agents/runtime/task_execution_policy.py`
  - 将任何携带新 URL 的 `page_capture_tool` 调用纳入导航页数和深度门禁。
- Modify `services/api/app/infrastructure/browser/page_capture.py`
  - 明确非 goto 模式不得打开新 URL；observe/timed_capture/hover/scroll 只能作用于已有会话当前页。
- Modify `services/api/app/agents/tools/page_capture_tools.py`
  - 更新参数说明和阻断结果错误码/消息，提示“新 URL 请使用 goto 并声明 navigation_depth”。
- Modify `services/api/app/agents/skills/phase/page-detection/SKILL.md`
  - 明确补采成功后旧 ambiguous / insufficient 采集标记为 superseded，不再参与完成门禁。
- Modify `services/api/app/agents/skills/qc-workflow/SKILL.md`
  - 在补采和收尾步骤补充生命周期规则。
- Modify `services/api/app/agents/skills/navigation-boundary-skill/SKILL.md`
  - 明确任何新 URL 导航必须走 goto 或显式导航门禁；非 goto 不承载新 URL。
- Modify `services/api/app/agents/runtime/README.md`
  - 记录工作记忆生命周期状态和 Run 完成判定。
- Modify `services/api/app/agents/tools/README.md`
  - 记录 page_capture_tool 的 URL 模式契约。
- Modify `docs/design/2026-07-16-working-memory-page-inspection-design.md`
  - 同步设计说明，区分 active / superseded / abandoned 与 pending。
- Modify `docs/专项能力与高频回归规则.md`
  - 增加本次两个 P1 的回归规则，防止同类绕过。
- Modify tests:
  - `services/api/tests/test_working_memory.py`
  - `services/api/tests/test_page_inspection_memory_middleware.py`
  - `services/api/tests/test_agent_runtime_integration.py`
  - `services/api/tests/test_task_execution_policy.py`
  - `services/api/tests/test_page_capture_tool.py`

## Task 1: Capture Lifecycle State

**Files:**
- Modify: `services/api/app/agents/runtime/page_inspection_memory.py`
- Modify: `services/api/app/agents/runtime/working_memory.py`
- Test: `services/api/tests/test_working_memory.py`

**Interfaces:**
- Produces: `PageCaptureMemory.lifecycle_status: str`, values `active | superseded | abandoned`
- Produces: `PageCaptureMemory.superseded_by_page_capture_id: str | None`
- Produces: `PageCaptureMemory.abandon_reason: str | None`
- Produces: `PageInspectionState.has_incomplete_page_inspection()` ignores inactive captures

- [ ] **Step 1: Write failing lifecycle test**

Add this test to `services/api/tests/test_working_memory.py`:

```python
def test_supplemental_capture_supersedes_unmatchable_capture_without_blocking_completion() -> None:
    memory = _memory_with_capture("capture-1")
    memory.remember_page_detection(
        page_capture_id="capture-1",
        detection_status="insufficient",
        page_type=None,
        rule_matching_allowed=False,
    )

    memory.remember_page_capture(
        page_capture_id="capture-2",
        page_url="https://example.com/capture-2",
        page_title="补采页面",
        page_seq=2,
        artifact_ids=["artifact-2"],
    )
    memory.remember_page_detection(
        page_capture_id="capture-2",
        detection_status="recognized",
        page_type="home",
        rule_matching_allowed=True,
    )
    memory.start_page_inspection(
        page_capture_id="capture-2",
        page_type="home",
        candidate_rules=[{"rule_id": "rule-1", "rule_code": "alioth_1"}],
    )
    memory.record_page_rule_result(
        page_capture_id="capture-2",
        result={"rule_id": "rule-1", "execution_status": "pass"},
        arguments={"evidence_artifact_ids": ["artifact-2"]},
    )

    snapshot = memory.audit_snapshot()
    assert memory.has_incomplete_page_inspection() is False
    assert snapshot["pending_page_count"] == 0
    assert snapshot["inactive_page_count"] == 1
    assert snapshot["inactive_pages"][0]["page_capture_id"] == "capture-1"
    assert snapshot["inactive_pages"][0]["status"] == "superseded"
    assert snapshot["inactive_pages"][0]["superseded_by_page_capture_id"] == "capture-2"
```

- [ ] **Step 2: Run test and confirm it fails**

Run:

```bash
pytest services/api/tests/test_working_memory.py::test_supplemental_capture_supersedes_unmatchable_capture_without_blocking_completion -q
```

Expected: FAIL because `inactive_pages` does not exist and `capture-1` still makes `has_incomplete_page_inspection()` true.

- [ ] **Step 3: Add lifecycle fields and serialization**

In `PageCaptureMemory`, add:

```python
lifecycle_status: str = "active"
superseded_by_page_capture_id: str | None = None
abandon_reason: str | None = None
```

Extend `to_dict()`:

```python
"lifecycle_status": self.lifecycle_status,
"superseded_by_page_capture_id": self.superseded_by_page_capture_id,
"abandon_reason": self.abandon_reason,
```

- [ ] **Step 4: Supersede unmatchable current capture on new capture**

In `PageInspectionState.remember_page_capture()`, before replacing `_current_page_capture_id`, call a helper:

```python
def _supersede_unmatchable_current_capture(self, new_page_capture_id: str) -> None:
    current_id = self._current_page_capture_id
    if current_id is None or current_id == new_page_capture_id:
        return
    current = self._page_captures.get(current_id)
    if current is None or current.lifecycle_status != "active":
        return
    if current_id in self._page_inspections:
        return
    detection = current.page_detection
    if detection is None or detection.detection_status not in {"ambiguous", "insufficient"}:
        return
    current.lifecycle_status = "superseded"
    current.superseded_by_page_capture_id = new_page_capture_id
```

Call it as:

```python
self._supersede_unmatchable_current_capture(page_capture_id)
```

- [ ] **Step 5: Filter completion and pending state**

Add:

```python
def _is_active_capture(self, page_capture_id: str) -> bool:
    capture = self._page_captures.get(page_capture_id)
    return capture is not None and capture.lifecycle_status == "active"
```

Update `has_incomplete_page_inspection()`:

```python
if self._current_page_capture_id is not None and self._is_active_capture(self._current_page_capture_id):
    return True
return any(
    not inspection.completed and self._is_active_capture(capture_id)
    for capture_id, inspection in self._page_inspections.items()
)
```

Update `pending_pages` to include only active incomplete inspections.

- [ ] **Step 6: Keep inactive captures in audit snapshot**

Add inactive pages to `snapshot()`:

```python
inactive_pages = [
    {
        **capture.to_dict(),
        "page_type": None,
        "status": capture.lifecycle_status,
        "candidate_rule_count": 0,
        "completed_rule_count": 0,
        "candidate_rules": [],
        "result": {"overall_status": capture.lifecycle_status, "status_counts": {}, "rules": []},
    }
    for capture in self._page_captures.values()
    if capture.lifecycle_status != "active"
]
```

Return `inactive_pages` and `inactive_page_count`. Keep `schema_version` as `1` if only additive fields are used; bump only if downstream consumers require strict schema versioning.

- [ ] **Step 7: Run lifecycle tests**

Run:

```bash
pytest services/api/tests/test_working_memory.py -q
```

Expected: PASS.

## Task 2: Middleware And Agent Completion Regression

**Files:**
- Modify: `services/api/tests/test_page_inspection_memory_middleware.py`
- Modify: `services/api/tests/test_agent_runtime_integration.py`

**Interfaces:**
- Consumes: Task 1 lifecycle fields.
- Produces: regression coverage for补采后完成判定.

- [ ] **Step 1: Add middleware regression**

Add a test where `page_detection_tool(capture-1)` returns `insufficient`, then `page_capture_tool` returns `capture-2`, then `capture-2` completes all rules. Assert `working_memory.pending_page_count == 0` and inactive `capture-1.status == "superseded"`.

- [ ] **Step 2: Add real Agent loop regression**

Add fake sequential capture tools in `test_agent_runtime_integration.py`:

```python
@tool(name="page_capture_tool", approval_mode="never_require")
def fake_sequential_page_capture_tool(mode: str) -> dict:
    fake_sequential_page_capture_tool.calls += 1
    capture_id = f"capture-{fake_sequential_page_capture_tool.calls}"
    return {
        "execution_status": "success",
        "page_capture_id": capture_id,
        "final_url": f"https://example.com/{capture_id}",
        "title": "测试页面",
        "page_seq": fake_sequential_page_capture_tool.calls,
        "artifact_ids": [f"artifact-{fake_sequential_page_capture_tool.calls}"],
    }
```

Script responses: capture-1 -> detection insufficient -> capture-2 -> detection recognized -> candidate rules -> record rule -> final text. Assert `TerminationReason.COMPLETED`.

- [ ] **Step 3: Run targeted tests**

Run:

```bash
pytest services/api/tests/test_page_inspection_memory_middleware.py services/api/tests/test_agent_runtime_integration.py -q
```

Expected: PASS.

## Task 3: URL Navigation Guard In Runtime Policy

**Files:**
- Modify: `services/api/app/agents/runtime/task_execution_policy.py`
- Test: `services/api/tests/test_task_execution_policy.py`

**Interfaces:**
- Produces: any `page_capture_tool` call with a new `url` is checked by `_navigation_block()`.
- Preserves: same-page click without URL remains allowed after page sampling limit.

- [ ] **Step 1: Write failing policy tests**

Add:

```python
async def test_page_policy_blocks_observe_with_new_url_after_sampling_limit() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={"max_pages": 1},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {"execution_status": "success", "final_url": "https://example.com/home"}

    await middleware.process(entry, _entry_next)

    observe = _context(
        "page_capture_tool",
        {"mode": "observe", "url": "https://example.com/detail", "navigation_depth": 1},
    )
    await middleware.process(observe, lambda: _unexpected_call())

    assert observe.result["error_code"] == "PAGE_SAMPLING_LIMIT_REACHED"
```

Add:

```python
async def test_page_policy_requires_depth_for_non_goto_new_url() -> None:
    middleware = TaskExecutionPolicyMiddleware(
        task_config={"max_depth": 2},
        page_sampling_policy={},
        loop_guard={},
    )
    entry = _context(
        "page_capture_tool",
        {"mode": "goto", "url": "https://example.com/home", "navigation_depth": 0},
    )

    async def _entry_next() -> None:
        entry.result = {"execution_status": "success", "final_url": "https://example.com/home"}

    await middleware.process(entry, _entry_next)

    hover = _context(
        "page_capture_tool",
        {"mode": "single_trigger", "trigger_action": "hover", "url": "https://example.com/detail"},
    )
    await middleware.process(hover, lambda: _unexpected_call())

    assert hover.result["error_code"] == "NAVIGATION_DEPTH_REQUIRED"
```

- [ ] **Step 2: Route URL-bearing calls through navigation block**

Replace the initial navigation detection with:

```python
is_page_capture = tool_name == self._page_capture_tool
requested_url = _normalize_url(arguments.get("url"))
is_url_navigation = is_page_capture and requested_url is not None
is_click_trigger = arguments.get("mode") == "single_trigger" and arguments.get("trigger_action") == "click"
is_declared_click_navigation = is_click_trigger and arguments.get("navigation_depth") is not None

if is_page_capture and (is_url_navigation or is_declared_click_navigation):
    async with self._navigation_lock:
        if blocked := self._navigation_block(
            arguments,
            click_may_navigate=is_declared_click_navigation and not is_url_navigation,
        ):
            context.result = blocked
            return
        await call_next()
        self._remember_successful_navigation(arguments, context.result)
    return
```

Keep the post-click branch only for `single_trigger click` without URL and without declared `navigation_depth`.

- [ ] **Step 3: Run policy tests**

Run:

```bash
pytest services/api/tests/test_task_execution_policy.py -q
```

Expected: PASS.

## Task 4: Browser Layer URL Mode Contract

**Files:**
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `services/api/app/agents/tools/page_capture_tools.py`
- Test: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Produces: non-goto mode cannot open a different URL inside an existing session.
- Produces: blocked result message tells Agent to use `mode="goto"` plus `navigation_depth`.

- [ ] **Step 1: Add browser regression**

Add a fake page currently at `https://example.com/home`; call `_capture_on_page()` with `PageCaptureMode.OBSERVE` and `url="https://example.com/detail"`. Assert `BlockedPageCaptureActionError` and `page.goto_calls == []`.

- [ ] **Step 2: Add URL mode guard**

Before the existing `if request.url:` block in `_capture_on_page()`, add:

```python
_validate_url_mode_contract(page=page, request=request)
```

Implement:

```python
def _validate_url_mode_contract(*, page: Any, request: PageCaptureRequest) -> None:
    if request.url is None or request.mode == PageCaptureMode.GOTO:
        return
    current_url = _normalize_url(getattr(page, "url", None))
    requested_url = _normalize_url(request.url)
    if requested_url is None or requested_url == current_url:
        return
    raise BlockedPageCaptureActionError(
        "非 goto 模式不得打开新的 URL；请使用 mode=goto 并按任务边界传入 navigation_depth。"
    )
```

Add a local `_normalize_url()` helper or move the existing runtime normalizer to a shared utility only if duplication grows beyond this guard.

- [ ] **Step 3: Adjust tool schema text**

In `NavigationDepthParameter` description and `page_capture_tool` docstring, state:

```text
新 URL 导航必须使用 goto；observe/timed_capture/single_trigger 只能作用于当前会话页面。
```

- [ ] **Step 4: Update affected tests**

Adjust existing tests that used `observe` or `single_trigger` with URL only as convenience. For fake browser unit tests, either switch to `mode="goto"` when opening a URL, or omit `url` when testing same-session observe/trigger semantics.

- [ ] **Step 5: Run page capture tests**

Run:

```bash
pytest services/api/tests/test_page_capture_tool.py -q
```

Expected: PASS.

## Task 5: Documentation And Regression Rules

**Files:**
- Modify: `services/api/app/agents/skills/phase/page-detection/SKILL.md`
- Modify: `services/api/app/agents/skills/qc-workflow/SKILL.md`
- Modify: `services/api/app/agents/skills/navigation-boundary-skill/SKILL.md`
- Modify: `services/api/app/agents/runtime/README.md`
- Modify: `services/api/app/agents/tools/README.md`
- Modify: `docs/design/2026-07-16-working-memory-page-inspection-design.md`
- Modify: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Consumes: Tasks 1-4 behavior.
- Produces: synchronized runtime contract and project regression checklist.

- [ ] **Step 1: Update page-detection**

Add a paragraph after the补采 rule:

```markdown
当 `ambiguous` / `insufficient` 的旧采集通过补采产生新 `page_capture_id` 后，旧采集生命周期标记为 `superseded`，不再参与 Run 完成门禁；审计快照仍保留旧采集、识别状态、缺失证据和 `superseded_by_page_capture_id`。
```

- [ ] **Step 2: Update qc-workflow**

In step 3.3 and 3.9, state that superseded captures are audit-only and do not count as pending; only active current/pending captures can make Run `incomplete`.

- [ ] **Step 3: Update navigation-boundary-skill**

Add:

```markdown
任何携带新 URL 的页面采集都视为导航，必须先通过页面数量、深度和来源边界检查；`observe`、`timed_capture`、`hover`、`scroll` 等非 goto 模式不得用 URL 打开新页面。
```

- [ ] **Step 4: Update runtime and tools README**

Record the active / superseded / abandoned lifecycle and URL mode contract in the module tables.

- [ ] **Step 5: Update project regression rules**

Add two rules:

```markdown
- 页面补采：`ambiguous` / `insufficient` 后的新采集完成规则闭环时，旧采集必须进入 `superseded` 或 `abandoned` 审计状态，不得继续使 Run 判定为 `incomplete`。
- 页面导航：任何携带新 URL 的 `page_capture_tool` 调用都必须经过页数、深度和来源门禁；非 goto 模式不得用 URL 打开新页面。
```

- [ ] **Step 6: Run doc and focused test verification**

Run:

```bash
pytest services/api/tests/test_working_memory.py services/api/tests/test_page_inspection_memory_middleware.py services/api/tests/test_agent_runtime_integration.py services/api/tests/test_task_execution_policy.py services/api/tests/test_page_capture_tool.py -q
```

Expected: PASS.

## Self-Review

**Spec coverage:** The plan covers both screenshot P1 issues: old supplemental captures no longer block completion, and URL-bearing non-goto calls cannot bypass navigation guards.

**Placeholder scan:** No TBD/TODO placeholders remain; each task lists concrete files, expected tests, and behavior.

**Type consistency:** Lifecycle fields are consistently named `lifecycle_status`, `superseded_by_page_capture_id`, and `abandon_reason`; navigation behavior consistently uses `navigation_depth`.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-16-page-capture-lifecycle-navigation-guard.md`. Two execution options:

1. Subagent-Driven (recommended) - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. Inline Execution - execute tasks in this session using executing-plans, batch execution with checkpoints.
