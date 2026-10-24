# page_capture_tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建 `page_capture_tool`，让 Agent 通过稳定契约调用浏览器采集能力，同时保持 Playwright 独立在 `infrastructure/browser` 层。

**Architecture:** Tool 层只负责参数、安全边界和结构化返回；浏览器层通过 `PageCaptureBrowser` 协议隔离 Playwright 细节。V1 直接返回 DOM、可见文本、截图等产物，并为每个 artifact 预留 `artifact_id=None`。

**Tech Stack:** Python 3.12、FastAPI 后端、Microsoft Agent Framework Tool Registry、Playwright Python、pytest。

## Global Constraints

- 后端固定使用 Python FastAPI。
- AI 框架固定使用 Microsoft Agent Framework。
- Tool 层不得直接暴露 Playwright API 给 Agent。
- Playwright 必须位于独立浏览器处理层：`services/api/app/infrastructure/browser`。
- 本轮只做“直接返回 + 预留 artifact_id 字段”，不接对象存储、不新增数据库迁移。
- 所有新增代码注释必须使用中文 UTF-8。

---

### Task 1: 浏览器采集契约与请求结果模型

**Files:**
- Create: `services/api/app/infrastructure/browser/page_capture.py`
- Test: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Produces: `PageCaptureMode`、`PageTriggerAction`、`PageCaptureRequest`、`PageCaptureArtifact`、`PageCaptureResult`、`PageCaptureBrowser`、`capture_page(request, browser)`
- Consumes: 无

- [ ] **Step 1: Write the failing test**

```python
async def test_capture_page_returns_artifact_id_placeholder_from_browser_result() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(mode=PageCaptureMode.OBSERVE, url="https://example.com")

    result = await capture_page(request=request, browser=browser)

    assert browser.requests == [request]
    assert result.execution_status == "success"
    assert result.artifacts[0].artifact_type == "screenshot"
    assert result.artifacts[0].artifact_id is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_capture_page_returns_artifact_id_placeholder_from_browser_result -q`
Expected: FAIL because `services.api.app.infrastructure.browser.page_capture` does not exist.

- [ ] **Step 3: Write minimal implementation**

Create dataclasses/enums and `capture_page()` that delegates to the injected browser.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_capture_page_returns_artifact_id_placeholder_from_browser_result -q`
Expected: PASS.

---

### Task 2: 单次触发安全边界

**Files:**
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Test: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Consumes: `PageCaptureRequest`
- Produces: `BlockedPageCaptureActionError`

- [ ] **Step 1: Write the failing test**

```python
async def test_single_trigger_rejects_high_risk_action_before_browser_call() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        url="https://example.com",
        trigger_action="submit",
    )

    with pytest.raises(BlockedPageCaptureActionError, match="submit"):
        await capture_page(request=request, browser=browser)

    assert browser.requests == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_single_trigger_rejects_high_risk_action_before_browser_call -q`
Expected: FAIL because high-risk action validation is not implemented.

- [ ] **Step 3: Write minimal implementation**

Reject `single_trigger` actions outside `click`、`scroll`、`hover`、`network_fault` before calling the browser.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_single_trigger_rejects_high_risk_action_before_browser_call -q`
Expected: PASS.

---

### Task 3: Agent Tool 注册入口

**Files:**
- Create: `services/api/app/agents/tools/page_capture_tools.py`
- Modify: `services/api/app/agents/bootstrap.py`
- Test: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Consumes: `capture_page(request, browser)`
- Produces: `page_capture_tool(...) -> dict[str, Any]`

- [ ] **Step 1: Write the failing test**

```python
def test_page_capture_tool_is_registered_as_sensitive() -> None:
    metadata = default_registry.get("page_capture_tool")

    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SENSITIVE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_page_capture_tool_is_registered_as_sensitive -q`
Expected: FAIL because `page_capture_tool` is not registered.

- [ ] **Step 3: Write minimal implementation**

Create `page_capture_tools.py` with `@tool(name="page_capture_tool", category="页面采集", permission_level=PermissionLevel.SENSITIVE)` and import it in `bootstrap.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_page_capture_tool_is_registered_as_sensitive -q`
Expected: PASS.

---

### Task 4: Playwright 独立浏览器处理层

**Files:**
- Modify: `services/api/app/infrastructure/browser/page_capture.py`
- Modify: `pyproject.toml`
- Test: `services/api/tests/test_page_capture_tool.py`

**Interfaces:**
- Produces: `PlaywrightPageCaptureBrowser.capture(request) -> PageCaptureResult`

- [ ] **Step 1: Write the failing test**

```python
def test_playwright_browser_class_is_importable_without_running_browser() -> None:
    browser = PlaywrightPageCaptureBrowser()

    assert browser.default_timeout_ms == 10000
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_playwright_browser_class_is_importable_without_running_browser -q`
Expected: FAIL because `PlaywrightPageCaptureBrowser` is not implemented.

- [ ] **Step 3: Write minimal implementation**

Add Playwright dependency and implement a browser class that imports Playwright lazily inside `capture()`, so unit tests do not require browser binaries.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py::test_playwright_browser_class_is_importable_without_running_browser -q`
Expected: PASS.

---

### Task 5: 文档与回归规则同步

**Files:**
- Modify: `services/api/app/agents/tools/README.md`
- Modify: `services/api/app/infrastructure/browser/README.md`
- Modify: `docs/专项能力与高频回归规则.md`

**Interfaces:**
- Consumes: 已实现的 `page_capture_tool`
- Produces: 规则文档同步说明

- [ ] **Step 1: Update docs**

Document that `page_capture_tool` is the unified Agent-facing capture entry, while Playwright remains in `infrastructure/browser`.

- [ ] **Step 2: Run verification**

Run: `python -m pytest services/api/tests/test_page_capture_tool.py -q`
Expected: PASS.

- [ ] **Step 3: Run lint**

Run: `python -m ruff check services/api`
Expected: PASS.
