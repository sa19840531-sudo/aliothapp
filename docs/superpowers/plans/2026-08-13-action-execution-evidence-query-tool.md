# Action Execution Evidence Query Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增一个 Agent 可直接调用的只读 Tool，按当前任务下的 `action_execution_id` 最多等待 180 秒，并在 before/after 页面类型、DOM、截图和 UI Snapshot 完整后返回 `action-evidence-bundle.v1`。

**Architecture:** 生产代码只在 `services/api/app/agents/tools` 内变化。新 Tool 文件内部以 Action Execution 为驱动执行一个只读 PostgreSQL 投影查询，将数据库行映射为有界证据包，并用 500ms 轮询状态机处理 ready、timeout、terminal failure、blocked、invalid request 和 query error；`tools/__init__.py` 仅负责触发注册。

**Tech Stack:** Python 3.12、Microsoft Agent Framework `FunctionTool`、asyncio、asyncpg/PostgreSQL、pytest、pytest-asyncio、ruff。

## Global Constraints

- 生产代码仅新增 `services/api/app/agents/tools/action_evidence_tools.py`，并最小修改 `services/api/app/agents/tools/__init__.py`。
- 允许新增 `services/api/tests/test_action_evidence_tools.py`。
- 不修改领域层、应用层、仓储层、Agent Bootstrap、数据库模型、迁移、前端菜单或路由。
- Tool 不接受 `task_id`，只能从当前 `AgentRunContext` 绑定任务身份。
- Tool 不执行或重放浏览器动作，不读取 DOM、截图或 UI Snapshot 正文。
- 默认等待 60 秒，最大等待 180 秒，轮询间隔固定为 500ms。
- `ready` 必须同时满足 Action Execution 为 `completed`，before/after 页面身份正确、URL 与页面类型非空，且两侧均有 DOM、Screenshot、UI Snapshot Artifact。
- 所有注释使用中文，所有文件使用 UTF-8，单文件内按私有函数分隔职责。
- Git 提交说明必须以 `[AI编程]` 开头，并交代模块、数据库迁移、前端菜单/路由和测试风险。

## File Structure

- `services/api/app/agents/tools/action_evidence_tools.py`
  - 唯一生产实现文件。
  - 负责 Tool 契约、输入规范化、Agent Run 上下文门禁、PostgreSQL 只读投影、证据映射、完整性检查、轮询和结构化错误。
- `services/api/app/agents/tools/__init__.py`
  - 仅导入并导出 `action_evidence_tools`，触发默认 Tool Registry 注册。
- `services/api/tests/test_action_evidence_tools.py`
  - 覆盖纯映射、完整性、等待状态机、上下文隔离、SQL 合同、Function Tool 注册和异常收敛。

---

### Task 1: 固化证据包映射与完整性合同

**Files:**
- Create: `services/api/app/agents/tools/action_evidence_tools.py`
- Create: `services/api/tests/test_action_evidence_tools.py`

**Interfaces:**
- Consumes: 数据库投影字典，包含 Action Execution、before/after Capture、页面记录和 Artifact 数组。
- Produces: `_build_bundle(snapshot: Mapping[str, Any], *, timeout_seconds: int, elapsed_ms: int, outcome: str, error_code: str | None = None, error_message: str | None = None) -> dict[str, Any]`。
- Produces: `_missing_requirements(snapshot: Mapping[str, Any] | None) -> list[str]`。
- Produces: `_is_ready(snapshot: Mapping[str, Any] | None) -> bool`。
- Produces: `_is_terminal_failure(snapshot: Mapping[str, Any] | None) -> bool`。

- [ ] **Step 1: 写入证据包映射与完整性失败测试**

在 `services/api/tests/test_action_evidence_tools.py` 建立共享完整快照，并精确断言协议映射：

```python
from datetime import UTC, datetime

from services.api.app.agents.tools import action_evidence_tools


TASK_ID = "11111111-1111-1111-1111-111111111111"
BEFORE_ID = "22222222-2222-2222-2222-222222222222"
AFTER_ID = "33333333-3333-3333-3333-333333333333"


def _artifact(artifact_type: str, suffix: str, *, capture_id: str) -> dict[str, object]:
    return {
        "artifact_id": f"artifact-{suffix}",
        "artifact_type": artifact_type,
        "storage_path": f"evidence/{suffix}",
        "task_id": TASK_ID,
        "page_capture_id": capture_id,
        "created_at": datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
    }


def _complete_snapshot() -> dict[str, object]:
    return {
        "action_execution_id": "action-001",
        "task_id": TASK_ID,
        "status": "completed",
        "error_code": None,
        "error_message": None,
        "intent_payload": {
            "action_type": "click",
            "semantic_target": "商品详情",
            "purpose": "进入商品详情页",
        },
        "before": {
            "capture_id": BEFORE_ID,
            "task_id": TASK_ID,
            "surface_kind": "web",
            "action_execution_id": "action-001",
            "capture_role": "before_action",
            "capture_status": "captured",
            "url": "https://example.com/list",
            "page_type": "product_list",
            "captured_at": datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
            "action_log": [],
            "artifacts": [
                _artifact("dom", "before-dom", capture_id=BEFORE_ID),
                _artifact("screenshot", "before-shot", capture_id=BEFORE_ID),
                _artifact("ui_snapshot", "before-ui", capture_id=BEFORE_ID),
            ],
        },
        "after": {
            "capture_id": AFTER_ID,
            "task_id": TASK_ID,
            "surface_kind": "web",
            "action_execution_id": "action-001",
            "capture_role": "after_action",
            "capture_status": "captured",
            "url": "https://example.com/detail/1001",
            "page_type": "product_detail",
            "captured_at": datetime(2026, 8, 13, 10, 0, 2, tzinfo=UTC),
            "action_log": [{
                "action": "click",
                "selector": "a.product-detail",
                "before_url": "https://example.com/list",
                "after_url": "https://example.com/detail/1001",
                "transition_kind": "business_navigation",
                "new_page_urls": [],
                "target_before": {"text": "商品详情"},
                "target_after": {"text": "商品详情"},
                "state_diff": {"change_count": 1},
            }],
            "artifacts": [
                _artifact("dom", "after-dom", capture_id=AFTER_ID),
                _artifact("screenshot", "after-shot", capture_id=AFTER_ID),
                _artifact("ui_snapshot", "after-ui", capture_id=AFTER_ID),
            ],
        },
    }


def test_build_bundle_maps_complete_action_evidence() -> None:
    payload = action_evidence_tools._build_bundle(
        _complete_snapshot(),
        timeout_seconds=60,
        elapsed_ms=2150,
        outcome="ready",
    )

    assert payload["protocol_version"] == "action-evidence-bundle.v1"
    assert payload["wait"] == {
        "outcome": "ready",
        "timeout_seconds": 60,
        "elapsed_ms": 2150,
        "missing_requirements": [],
    }
    assert payload["before"]["page_type"] == "product_list"
    assert payload["before"]["dom"] == {
        "artifact_id": "artifact-before-dom",
        "storage_path": "evidence/before-dom",
    }
    assert payload["action"]["selector"] == "a.product-detail"
    assert payload["action"]["target"] == "商品详情"
    assert payload["action"]["transition_kind"] == "business_navigation"
    assert payload["after"]["page_type"] == "product_detail"
    assert payload["diff"]["url_changed"] is True
    assert payload["diff"]["state_diff"] == {"change_count": 1}


def test_missing_requirements_reports_page_and_artifact_gaps_in_stable_order() -> None:
    snapshot = _complete_snapshot()
    snapshot["before"]["artifacts"] = [
        item for item in snapshot["before"]["artifacts"]
        if item["artifact_type"] == "screenshot"
    ]
    snapshot["after"]["page_type"] = None

    assert action_evidence_tools._missing_requirements(snapshot) == [
        "before.dom",
        "before.ui_snapshot",
        "after.page_type",
    ]
    assert action_evidence_tools._is_ready(snapshot) is False


def test_identity_mismatch_hides_capture_evidence() -> None:
    snapshot = _complete_snapshot()
    snapshot["before"]["capture_role"] = "observation"

    payload = action_evidence_tools._build_bundle(
        snapshot,
        timeout_seconds=60,
        elapsed_ms=1,
        outcome="timeout",
        error_code="ACTION_EVIDENCE_WAIT_TIMEOUT",
        error_message="等待动作证据超时",
    )

    assert "before.identity" in payload["wait"]["missing_requirements"]
    assert payload["before"] is None


def test_artifact_selection_is_deterministic() -> None:
    snapshot = _complete_snapshot()
    snapshot["after"]["artifacts"].append({
        **_artifact("screenshot", "after-shot-later", capture_id=AFTER_ID),
        "created_at": datetime(2026, 8, 13, 10, 0, 3, tzinfo=UTC),
    })

    payload = action_evidence_tools._build_bundle(
        snapshot,
        timeout_seconds=60,
        elapsed_ms=1,
        outcome="ready",
    )

    assert payload["after"]["screenshot"]["artifact_id"] == "artifact-after-shot"
```

- [ ] **Step 2: 运行测试确认按预期失败**

Run:

```powershell
python -m pytest services/api/tests/test_action_evidence_tools.py -q
```

Expected: FAIL，原因是 `action_evidence_tools` 尚不存在。

- [ ] **Step 3: 实现最小证据映射与完整性函数**

在 `action_evidence_tools.py` 中增加：

```python
"""按 Action Execution 查询完整前后页面证据的只读 Agent Tool。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any


PROTOCOL_VERSION = "action-evidence-bundle.v1"
REQUIRED_ARTIFACT_TYPES = ("dom", "screenshot", "ui_snapshot")
TERMINAL_FAILURE_STATUSES = frozenset(
    {"blocked", "failed", "timed_out", "evidence_failed"}
)


def _missing_requirements(snapshot: Mapping[str, Any] | None) -> list[str]:
    if snapshot is None:
        return ["action_execution"]
    missing: list[str] = []
    if snapshot.get("status") != "completed":
        missing.append("action_execution.completed")
    for side, expected_role in (("before", "before_action"), ("after", "after_action")):
        capture = snapshot.get(side)
        if not isinstance(capture, Mapping):
            missing.append(side)
            continue
        if not _capture_identity_valid(snapshot, capture, expected_role=expected_role):
            missing.append(f"{side}.identity")
            continue
        if not _nonempty(capture.get("url")):
            missing.append(f"{side}.url")
        if not _nonempty(capture.get("page_type")):
            missing.append(f"{side}.page_type")
        artifacts = _valid_artifacts(snapshot, capture)
        for artifact_type in REQUIRED_ARTIFACT_TYPES:
            if not any(item.get("artifact_type") == artifact_type for item in artifacts):
                missing.append(f"{side}.{artifact_type}")
    return missing


def _is_ready(snapshot: Mapping[str, Any] | None) -> bool:
    return bool(snapshot) and snapshot.get("status") == "completed" and not _missing_requirements(snapshot)


def _is_terminal_failure(snapshot: Mapping[str, Any] | None) -> bool:
    return bool(snapshot) and snapshot.get("status") in TERMINAL_FAILURE_STATUSES
```

同时实现 `_capture_identity_valid`、`_valid_artifacts`、`_primary_artifact`、
`_latest_action_trace`、`_action_payload`、`_capture_payload`、`_diff_payload`、
`_isoformat_utc` 与 `_build_bundle`。关键规则必须逐字落实：

```python
def _capture_identity_valid(
    snapshot: Mapping[str, Any],
    capture: Mapping[str, Any],
    *,
    expected_role: str,
) -> bool:
    return (
        capture.get("task_id") == snapshot.get("task_id")
        and capture.get("surface_kind") == "web"
        and capture.get("action_execution_id") == snapshot.get("action_execution_id")
        and capture.get("capture_role") == expected_role
        and capture.get("capture_status") == "captured"
        and _nonempty(capture.get("capture_id"))
    )


def _valid_artifacts(
    snapshot: Mapping[str, Any], capture: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    raw = capture.get("artifacts")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    expected_task_id = snapshot.get("task_id")
    expected_capture_id = capture.get("capture_id")
    return [
        item
        for item in raw
        if isinstance(item, Mapping)
        and item.get("task_id") == expected_task_id
        and item.get("page_capture_id") == expected_capture_id
    ]
```

`_build_bundle` 必须始终输出顶层
`protocol_version/action_execution_id/status/wait/before/action/after/diff`；非 ready 时
只对身份正确的 Capture 返回已有字段，身份错误一侧返回 `None`。`wait` 只在存在
错误时追加 `error_code/error_message`，不得放入 `None` 占位键。

- [ ] **Step 4: 运行映射测试并修正到全部通过**

Run:

```powershell
python -m pytest services/api/tests/test_action_evidence_tools.py -q
python -m ruff check services/api/app/agents/tools/action_evidence_tools.py services/api/tests/test_action_evidence_tools.py
```

Expected: 当前 Task 1 测试全部 PASS，ruff 无问题。

- [ ] **Step 5: 提交证据包映射合同**

```powershell
git add -- services/api/app/agents/tools/action_evidence_tools.py services/api/tests/test_action_evidence_tools.py
git commit -m "[AI编程] feat: 固化Action证据包映射；模块：Agent Tools/Action Evidence；数据库迁移：无；前端菜单路由：无需同步；测试风险：尚未接入PG轮询"
```

---

### Task 2: 实现任务隔离的 PostgreSQL 投影与等待状态机

**Files:**
- Modify: `services/api/app/agents/tools/action_evidence_tools.py`
- Modify: `services/api/tests/test_action_evidence_tools.py`

**Interfaces:**
- Consumes: Task 1 的 `_build_bundle`、`_missing_requirements`、`_is_ready`、`_is_terminal_failure`。
- Produces: `_query_action_evidence(*, task_id: str, action_execution_id: str) -> dict[str, Any] | None`。
- Produces: `_wait_for_action_evidence(*, task_id: str, action_execution_id: str, timeout_seconds: int) -> dict[str, Any]`。
- Produces: `get_action_execution_evidence(*, action_execution_id: str, timeout_seconds: int = 60) -> dict[str, Any]`，供 Function Tool 包装和测试调用。

- [ ] **Step 1: 写入 SQL 归属、轮询、超时与异常失败测试**

增加可控连接与状态序列测试。SQL 合同测试使用假 Pool 捕获实际查询和参数：

```python
class _Acquire:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def __aenter__(self) -> object:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Pool:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)


class _Connection:
    def __init__(self, row: dict[str, object] | None) -> None:
        self.row = row
        self.query = ""
        self.args: tuple[object, ...] = ()

    async def fetchrow(self, query: str, *args: object) -> dict[str, object] | None:
        self.query = " ".join(query.split())
        self.args = args
        return self.row


async def test_query_is_driven_by_action_execution_and_scoped_to_task(monkeypatch) -> None:
    connection = _Connection(None)

    async def fake_get_pool(database_url=None):
        return _Pool(connection)

    monkeypatch.setattr(action_evidence_tools, "get_postgres_pool", fake_get_pool)

    result = await action_evidence_tools._query_action_evidence(
        task_id=TASK_ID,
        action_execution_id="action-001",
    )

    assert result is None
    assert "FROM qc_action_execution AS action" in connection.query
    assert "action.action_execution_id = $1" in connection.query
    assert "action.task_id = $2::uuid" in connection.query
    assert "LEFT JOIN qc_capture AS before_capture" in connection.query
    assert "LEFT JOIN qc_capture AS after_capture" in connection.query
    assert connection.args == ("action-001", TASK_ID)
```

轮询测试替换 `_query_action_evidence` 与 `_sleep`，避免真实等待：

```python
async def test_wait_requeries_until_completed_evidence_is_ready(monkeypatch) -> None:
    incomplete = _complete_snapshot()
    incomplete["after"]["page_type"] = None
    states = iter((incomplete, _complete_snapshot()))
    sleeps: list[float] = []

    async def fake_query(*, task_id: str, action_execution_id: str):
        assert task_id == TASK_ID
        assert action_execution_id == "action-001"
        return next(states)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    ticks = iter((100.0, 100.1, 100.6, 100.7))
    monkeypatch.setattr(action_evidence_tools, "_query_action_evidence", fake_query)
    monkeypatch.setattr(action_evidence_tools, "_sleep", fake_sleep)
    monkeypatch.setattr(action_evidence_tools, "_monotonic", lambda: next(ticks))

    result = await action_evidence_tools._wait_for_action_evidence(
        task_id=TASK_ID,
        action_execution_id="action-001",
        timeout_seconds=60,
    )

    assert result["wait"]["outcome"] == "ready"
    assert sleeps == [0.5]


async def test_wait_returns_terminal_failure_without_sleep(monkeypatch) -> None:
    failed = _complete_snapshot()
    failed["status"] = "evidence_failed"
    failed["error_code"] = "capture_failed"
    failed["error_message"] = "动作后证据写入失败"

    async def fake_query(**kwargs):
        return failed

    async def fail_sleep(seconds: float) -> None:
        raise AssertionError("失败终态不应继续等待")

    monkeypatch.setattr(action_evidence_tools, "_query_action_evidence", fake_query)
    monkeypatch.setattr(action_evidence_tools, "_sleep", fail_sleep)

    result = await action_evidence_tools._wait_for_action_evidence(
        task_id=TASK_ID,
        action_execution_id="action-001",
        timeout_seconds=60,
    )

    assert result["wait"]["outcome"] == "terminal_failure"
    assert result["wait"]["error_code"] == "ACTION_EVIDENCE_TERMINAL_FAILURE"


async def test_wait_timeout_returns_current_status_and_gaps(monkeypatch) -> None:
    incomplete = _complete_snapshot()
    incomplete["after"]["page_type"] = None

    async def fake_query(**kwargs):
        return incomplete

    async def fake_sleep(seconds: float) -> None:
        return None

    ticks = iter((10.0, 10.0, 11.0))
    monkeypatch.setattr(action_evidence_tools, "_query_action_evidence", fake_query)
    monkeypatch.setattr(action_evidence_tools, "_sleep", fake_sleep)
    monkeypatch.setattr(action_evidence_tools, "_monotonic", lambda: next(ticks))

    result = await action_evidence_tools._wait_for_action_evidence(
        task_id=TASK_ID,
        action_execution_id="action-001",
        timeout_seconds=1,
    )

    assert result["status"] == "completed"
    assert result["wait"]["outcome"] == "timeout"
    assert result["wait"]["error_code"] == "ACTION_EVIDENCE_WAIT_TIMEOUT"
    assert "after.page_type" in result["wait"]["missing_requirements"]
```

再增加以下具名测试，不得合并成参数含义不清的巨型测试：

- `test_wait_missing_action_never_queries_without_task_scope`
- `test_timeout_normalization_defaults_to_60_and_caps_at_180`
- `test_invalid_action_id_returns_invalid_request_without_query`
- `test_timeout_below_one_returns_invalid_request_without_query`
- `test_missing_agent_context_returns_blocked_without_query`
- `test_query_exception_returns_generic_error_without_driver_details`
- `test_query_maps_json_text_and_datetime_fields`
- `test_wrong_artifact_owner_reports_identity_gap`

- [ ] **Step 2: 运行新增测试确认按预期失败**

Run:

```powershell
python -m pytest services/api/tests/test_action_evidence_tools.py -q
```

Expected: FAIL，缺少查询、轮询和公开服务函数。

- [ ] **Step 3: 实现单次 PostgreSQL 只读投影**

在 Tool 文件中导入：

```python
import asyncio
import json
import time

from services.api.app.infrastructure.db.pool import get_postgres_pool
```

实现模块级可替换等待函数与常量：

```python
DEFAULT_TIMEOUT_SECONDS = 60
MAX_TIMEOUT_SECONDS = 180
POLL_INTERVAL_SECONDS = 0.5
_sleep = asyncio.sleep
_monotonic = time.monotonic
```

`_query_action_evidence` 使用一个 `fetchrow` 查询。查询必须从
`qc_action_execution AS action` 出发，before/after 全部使用 LEFT JOIN，并同时限定：

```sql
WHERE action.action_execution_id = $1
  AND action.task_id = $2::uuid
```

选择字段必须包含：

```sql
action.action_execution_id,
action.task_id::text,
action.status,
action.intent_payload,
action.error_code,
action.error_message,
before_capture.id::text,
before_capture.task_id::text,
before_capture.surface_kind,
before_capture.action_execution_id,
before_capture.capture_role,
before_capture.capture_status,
before_capture.captured_at,
before_page.url,
before_page.page_type,
before_page.action_log,
after_capture.id::text,
after_capture.task_id::text,
after_capture.surface_kind,
after_capture.action_execution_id,
after_capture.capture_role,
after_capture.capture_status,
after_capture.captured_at,
after_page.url,
after_page.page_type,
after_page.action_log
```

Artifact 使用两个 `LEFT JOIN LATERAL` 子查询分别聚合，避免 before/after Artifact
形成笛卡尔积。每个 Artifact 对象包含
`artifact_id/artifact_type/storage_path/task_id/page_capture_id/created_at`，聚合顺序为
`created_at ASC, id ASC`。子查询按 Capture ID 读取全部关联 Artifact，以便映射层发现
错误的 `task_id`；不得在 SQL 中预先过滤掉错误归属。

行解析辅助函数必须兼容 asyncpg 已解码 JSON 和测试中的 JSON 字符串：

```python
def _json_value(value: Any, *, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value
```

- [ ] **Step 4: 实现等待状态机与公开服务函数**

实现输入规范化：

```python
def _normalize_request(
    action_execution_id: Any, timeout_seconds: Any
) -> tuple[str | None, int | None]:
    action_id = action_execution_id.strip() if isinstance(action_execution_id, str) else ""
    if not action_id or len(action_id) > 96:
        return None, None
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int):
        return action_id, None
    if timeout_seconds < 1:
        return action_id, None
    return action_id, min(timeout_seconds, MAX_TIMEOUT_SECONDS)
```

`_wait_for_action_evidence` 的每轮顺序固定为：查询 → terminal failure → ready →
检查截止时间 → sleep。超时时用最后一次快照构造结果；未找到 Action 时
`missing_requirements == ["action_execution"]`。查询异常只返回：

```json
{
  "outcome": "error",
  "error_code": "ACTION_EVIDENCE_QUERY_FAILED",
  "error_message": "动作证据查询失败"
}
```

`get_action_execution_evidence` 先校验请求，再调用 `require_agent_run_context()`；捕获其
`RuntimeError` 并返回 `blocked/ACTION_EVIDENCE_CONTEXT_REQUIRED`，不得访问数据库。
其他异常只允许由 `_wait_for_action_evidence` 收敛为 `error`。

- [ ] **Step 5: 运行查询与状态机测试并修正到全部通过**

Run:

```powershell
python -m pytest services/api/tests/test_action_evidence_tools.py -q
python -m ruff check services/api/app/agents/tools/action_evidence_tools.py services/api/tests/test_action_evidence_tools.py
```

Expected: 全部 PASS，ruff 无问题。

- [ ] **Step 6: 提交只读查询与等待状态机**

```powershell
git add -- services/api/app/agents/tools/action_evidence_tools.py services/api/tests/test_action_evidence_tools.py
git commit -m "[AI编程] feat: 增加Action证据等待查询；模块：Agent Tools/Action Evidence；数据库迁移：无；前端菜单路由：无需同步；测试风险：真实PG时序待集成验证"
```

---

### Task 3: 注册 Function Tool 并完成回归验证

**Files:**
- Modify: `services/api/app/agents/tools/action_evidence_tools.py`
- Modify: `services/api/app/agents/tools/__init__.py`
- Modify: `services/api/tests/test_action_evidence_tools.py`

**Interfaces:**
- Consumes: Task 2 的 `get_action_execution_evidence`。
- Produces: 默认注册表中的 `get_action_execution_evidence_tool` Function Tool，参数只有 `action_execution_id` 和可选 `timeout_seconds`。

- [ ] **Step 1: 写入 Function Tool 注册与调用失败测试**

```python
import json

from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools.registry import PermissionLevel, default_registry


def test_action_evidence_tool_is_registered_as_safe() -> None:
    metadata = default_registry.get("get_action_execution_evidence_tool")

    assert metadata is not None
    assert metadata.category == "动作证据查询"
    assert metadata.permission_level is PermissionLevel.SAFE
    parameters = metadata.tool.parameters()
    assert parameters["required"] == ["action_execution_id"]
    assert set(parameters["properties"]) == {"action_execution_id", "timeout_seconds"}


async def test_registered_tool_uses_bound_task_context(monkeypatch) -> None:
    calls: list[tuple[str, str, int]] = []

    async def fake_wait(*, task_id: str, action_execution_id: str, timeout_seconds: int):
        calls.append((task_id, action_execution_id, timeout_seconds))
        return action_evidence_tools._build_bundle(
            _complete_snapshot(),
            timeout_seconds=timeout_seconds,
            elapsed_ms=0,
            outcome="ready",
        )

    monkeypatch.setattr(action_evidence_tools, "_wait_for_action_evidence", fake_wait)
    metadata = default_registry.get("get_action_execution_evidence_tool")

    with bind_agent_run_context(AgentRunContext(task_id=TASK_ID, agent_run_id="run-1")):
        content = await metadata.tool.invoke(arguments={"action_execution_id": "action-001"})

    payload = json.loads(content[0].text)
    assert payload["wait"]["outcome"] == "ready"
    assert calls == [(TASK_ID, "action-001", 60)]
```

- [ ] **Step 2: 运行注册测试确认按预期失败**

Run:

```powershell
python -m pytest services/api/tests/test_action_evidence_tools.py -q
```

Expected: FAIL，默认注册表尚不存在该 Tool。

- [ ] **Step 3: 添加 Tool 装饰器和根包注册**

在 `action_evidence_tools.py` 导入现有注册器：

```python
from services.api.app.agents.tools.registry import PermissionLevel, tool
```

添加唯一公开 Function Tool：

```python
@tool(
    name="get_action_execution_evidence_tool",
    description=(
        "按当前任务下的 action_execution_id 等待动作完成与前后页面证据齐备，"
        "返回页面类型、DOM/截图/UI Snapshot 引用、动作和差异摘要。"
    ),
    category="动作证据查询",
    permission_level=PermissionLevel.SAFE,
)
async def get_action_execution_evidence_tool(
    action_execution_id: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """查询当前 Agent Run 所属任务的完整动作证据包。"""
    return await get_action_execution_evidence(
        action_execution_id=action_execution_id,
        timeout_seconds=timeout_seconds,
    )
```

在 `tools/__init__.py` 的导入组与 `__all__` 中加入 `action_evidence_tools`，除此之外不
修改任何生产文件。

- [ ] **Step 4: 运行定向 Tool 测试与现有注册回归**

Run:

```powershell
python -m pytest services/api/tests/test_action_evidence_tools.py services/api/tests/test_tools_registry.py services/api/tests/test_tools_permissions.py services/api/tests/test_agent_runtime_concurrency.py -q
```

Expected: 全部 PASS。

- [ ] **Step 5: 运行静态检查和相关 Agent 回归**

Run:

```powershell
python -m ruff check services/api/app/agents/tools/action_evidence_tools.py services/api/app/agents/tools/__init__.py services/api/tests/test_action_evidence_tools.py
python -m pytest services/api/tests/test_capability_manifest.py services/api/tests/test_agent_runtime_integration.py -q
```

Expected: ruff 无问题，相关 Agent 测试全部 PASS。

- [ ] **Step 6: 提交 Tool 注册**

```powershell
git add -- services/api/app/agents/tools/action_evidence_tools.py services/api/app/agents/tools/__init__.py services/api/tests/test_action_evidence_tools.py
git commit -m "[AI编程] feat: 注册Action证据查询Tool；模块：Agent Tools/Action Evidence；数据库迁移：无；前端菜单路由：无需同步；测试风险：真实PG与对象存储路径待冒烟"
```

---

### Task 4: Worktree 最终验证并合并到 dev_1.5

**Files:**
- Verify only: `services/api/app/agents/tools/action_evidence_tools.py`
- Verify only: `services/api/app/agents/tools/__init__.py`
- Verify only: `services/api/tests/test_action_evidence_tools.py`

**Interfaces:**
- Consumes: Tasks 1–3 的完整 Function Tool。
- Produces: 已验证并合并到本地主分支 `dev_1.5` 的实现提交。

- [ ] **Step 1: 在 feature worktree 中确认变更范围**

Run:

```powershell
git status --short
git diff dev_1.5...HEAD --name-status
```

Expected: 除计划/设计文档外，生产代码仅包含
`action_evidence_tools.py` 与 `tools/__init__.py`，测试仅包含
`test_action_evidence_tools.py`；没有数据库迁移、领域、应用、仓储或前端变更。

- [ ] **Step 2: 运行最终完整定向验证**

Run:

```powershell
python -m ruff check services/api/app/agents/tools/action_evidence_tools.py services/api/app/agents/tools/__init__.py services/api/tests/test_action_evidence_tools.py
python -m pytest services/api/tests/test_action_evidence_tools.py services/api/tests/test_tools_registry.py services/api/tests/test_tools_permissions.py services/api/tests/test_agent_runtime_concurrency.py services/api/tests/test_capability_manifest.py services/api/tests/test_agent_runtime_integration.py -q
```

Expected: 两条命令均以 0 退出。

- [ ] **Step 3: 使用 verification-before-completion 复核新鲜输出**

确认最终验证不是旧日志；记录测试数量、耗时与任何跳过项。若失败，留在 feature
worktree 修复并重新执行 Step 2，不得在 `dev_1.5` 上直接修代码。

- [ ] **Step 4: 从主工作区合并 feature 分支**

在 `D:\Project\SSC\alioth` 主工作区确认当前分支为 `dev_1.5` 且用户原有未跟踪文件
不受影响，然后执行非交互式合并：

```powershell
git merge --no-ff codex/action-evidence-query-tool
```

Expected: 合并成功且不覆盖用户已有未跟踪文件。

- [ ] **Step 5: 在合并后的 dev_1.5 重跑验证**

Run:

```powershell
python -m ruff check services/api/app/agents/tools/action_evidence_tools.py services/api/app/agents/tools/__init__.py services/api/tests/test_action_evidence_tools.py
python -m pytest services/api/tests/test_action_evidence_tools.py services/api/tests/test_tools_registry.py services/api/tests/test_tools_permissions.py services/api/tests/test_agent_runtime_concurrency.py services/api/tests/test_capability_manifest.py services/api/tests/test_agent_runtime_integration.py -q
```

Expected: 合并后的两条命令均以 0 退出。

- [ ] **Step 6: 清理已合并 worktree**

先验证目标 worktree 绝对路径位于仓库专用 `.worktrees` 目录，再使用 Git 的 worktree
移除功能清单。只有合并后验证通过时才能删除 feature 分支；不得使用递归删除命令
直接清理 worktree。
