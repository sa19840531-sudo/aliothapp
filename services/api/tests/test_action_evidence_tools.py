"""Action Execution 完整证据查询 Tool 测试。"""

import json
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools import action_evidence_tools
from services.api.app.agents.tools.registry import PermissionLevel, default_registry

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
            "action_log": [
                {
                    "action": "click",
                    "selector": "a.product-detail",
                    "before_url": "https://example.com/list",
                    "after_url": "https://example.com/detail/1001",
                    "transition_kind": "business_navigation",
                    "new_page_urls": [],
                    "target_before": {"text": "商品详情"},
                    "target_after": {"text": "商品详情"},
                    "state_diff": {"change_count": 1},
                }
            ],
            "artifacts": [
                _artifact("dom", "after-dom", capture_id=AFTER_ID),
                _artifact("screenshot", "after-shot", capture_id=AFTER_ID),
                _artifact("ui_snapshot", "after-ui", capture_id=AFTER_ID),
            ],
        },
    }


def test_build_bundle_maps_complete_action_evidence() -> None:
    """删除任一主协议映射时应破坏 Agent 可消费的完整证据包。"""
    payload = action_evidence_tools._build_bundle(
        _complete_snapshot(),
        timeout_seconds=60,
        elapsed_ms=2150,
        outcome="ready",
    )

    assert payload["protocol_version"] == "action-evidence-bundle.v1"
    assert payload["action_execution_id"] == "action-001"
    assert payload["status"] == "completed"
    assert payload["wait"] == {
        "outcome": "ready",
        "timeout_seconds": 60,
        "elapsed_ms": 2150,
        "missing_requirements": [],
    }
    assert payload["before"] == {
        "capture_id": BEFORE_ID,
        "url": "https://example.com/list",
        "page_type": "product_list",
        "captured_at": "2026-08-13T10:00:00Z",
        "dom": {
            "artifact_id": "artifact-before-dom",
            "storage_path": "evidence/before-dom",
        },
        "screenshot": {
            "artifact_id": "artifact-before-shot",
            "storage_path": "evidence/before-shot",
        },
        "ui_snapshot": {
            "artifact_id": "artifact-before-ui",
            "storage_path": "evidence/before-ui",
        },
        # 证据来源标注：本用例快照未带清单，故为 None（回退附件表）
        "evidence_source": None,
    }
    assert payload["action"] == {
        "type": "click",
        "target": "商品详情",
        "selector": "a.product-detail",
        "purpose": "进入商品详情页",
        "before_url": "https://example.com/list",
        "after_url": "https://example.com/detail/1001",
        "transition_kind": "business_navigation",
        "new_page_urls": [],
    }
    assert payload["after"]["page_type"] == "product_detail"
    assert payload["diff"] == {
        "url_changed": True,
        "state_diff": {"change_count": 1},
        "target_before": {"text": "商品详情"},
        "target_after": {"text": "商品详情"},
    }


def test_missing_requirements_reports_page_and_artifact_gaps_in_stable_order() -> None:
    """漏检页面类型或必需 Artifact 时不应错误进入 ready。"""
    snapshot = deepcopy(_complete_snapshot())
    before = snapshot["before"]
    assert isinstance(before, dict)
    before["artifacts"] = [
        item
        for item in before["artifacts"]
        if isinstance(item, dict) and item["artifact_type"] == "screenshot"
    ]
    after = snapshot["after"]
    assert isinstance(after, dict)
    after["page_type"] = None

    assert action_evidence_tools._missing_requirements(snapshot) == [
        "before.dom",
        "before.ui_snapshot",
        "after.page_type",
    ]
    assert action_evidence_tools._is_ready(snapshot) is False


def test_identity_mismatch_hides_capture_evidence() -> None:
    """角色或动作归属错误时不应向 Agent 暴露该侧证据。"""
    snapshot = deepcopy(_complete_snapshot())
    before = snapshot["before"]
    assert isinstance(before, dict)
    before["capture_role"] = "observation"

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
    assert payload["wait"]["error_code"] == "ACTION_EVIDENCE_WAIT_TIMEOUT"


def test_wrong_artifact_owner_reports_identity_gap() -> None:
    """Artifact 任务归属错误时不应退化成普通类型缺失。"""
    snapshot = deepcopy(_complete_snapshot())
    after = snapshot["after"]
    assert isinstance(after, dict)
    artifacts = after["artifacts"]
    assert isinstance(artifacts, list)
    artifacts[0]["task_id"] = "99999999-9999-9999-9999-999999999999"

    assert "after.identity" in action_evidence_tools._missing_requirements(snapshot)


def test_artifact_selection_is_deterministic() -> None:
    """查询返回顺序波动时主 Artifact 仍应按时间和 ID 稳定选择。"""
    snapshot = deepcopy(_complete_snapshot())
    after = snapshot["after"]
    assert isinstance(after, dict)
    artifacts = after["artifacts"]
    assert isinstance(artifacts, list)
    artifacts.insert(
        0,
        {
            **_artifact("screenshot", "after-shot-later", capture_id=AFTER_ID),
            "created_at": datetime(2026, 8, 13, 10, 0, 3, tzinfo=UTC),
        },
    )

    payload = action_evidence_tools._build_bundle(
        snapshot,
        timeout_seconds=60,
        elapsed_ms=1,
        outcome="ready",
    )

    assert payload["after"]["screenshot"]["artifact_id"] == "artifact-after-shot"


def test_action_fields_fall_back_to_navigation_and_intent() -> None:
    """Trace 顶层字段缺失时仍应使用导航子对象和 Intent 构造真实摘要。"""
    snapshot = deepcopy(_complete_snapshot())
    after = snapshot["after"]
    assert isinstance(after, dict)
    after["action_log"] = [
        {
            "action": "click",
            "requested_selector": "button.detail",
            "navigation": {
                "before_url": "https://example.com/list",
                "after_url": "https://example.com/detail/1001",
                "new_page_urls": ["https://example.com/help"],
            },
        }
    ]

    payload = action_evidence_tools._build_bundle(
        snapshot,
        timeout_seconds=60,
        elapsed_ms=1,
        outcome="ready",
    )

    assert payload["action"]["target"] == "商品详情"
    assert payload["action"]["selector"] == "button.detail"
    assert payload["action"]["new_page_urls"] == ["https://example.com/help"]
    assert payload["diff"]["url_changed"] is True


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

    async def fetchrow(
        self, query: str, *args: object
    ) -> dict[str, object] | None:
        self.query = " ".join(query.split())
        self.args = args
        return self.row


async def test_query_is_driven_by_action_execution_and_scoped_to_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """移除 Action ID 或任务 ID 条件时应破坏跨任务隔离合同。"""
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


async def test_query_maps_json_text_and_datetime_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON 驱动差异或字符串解码变化时应破坏投影映射。"""
    row = {
        "action_execution_id": "action-001",
        "task_id": TASK_ID,
        "status": "completed",
        "intent_payload": (
            '{"action_type":"click","semantic_target":"商品详情",'
            '"purpose":"进入商品详情页"}'
        ),
        "error_code": None,
        "error_message": None,
        "before_capture_id": BEFORE_ID,
        "before_task_id": TASK_ID,
        "before_surface_kind": "web",
        "before_action_execution_id": "action-001",
        "before_capture_role": "before_action",
        "before_capture_status": "captured",
        "before_captured_at": datetime(2026, 8, 13, 10, 0, tzinfo=UTC),
        "before_url": "https://example.com/list",
        "before_page_type": "product_list",
        "before_action_log": "[]",
        "before_artifacts": (
            '[{"artifact_id":"artifact-before-dom","artifact_type":"dom",'
            '"storage_path":"evidence/before-dom",'
            f'"task_id":"{TASK_ID}","page_capture_id":"{BEFORE_ID}",'
            '"created_at":"2026-08-13T10:00:00+00:00"},'
            '{"artifact_id":"artifact-before-shot","artifact_type":"screenshot",'
            '"storage_path":"evidence/before-shot",'
            f'"task_id":"{TASK_ID}","page_capture_id":"{BEFORE_ID}",'
            '"created_at":"2026-08-13T10:00:00+00:00"},'
            '{"artifact_id":"artifact-before-ui","artifact_type":"ui_snapshot",'
            '"storage_path":"evidence/before-ui",'
            f'"task_id":"{TASK_ID}","page_capture_id":"{BEFORE_ID}",'
            '"created_at":"2026-08-13T10:00:00+00:00"}]'
        ),
        "after_capture_id": AFTER_ID,
        "after_task_id": TASK_ID,
        "after_surface_kind": "web",
        "after_action_execution_id": "action-001",
        "after_capture_role": "after_action",
        "after_capture_status": "captured",
        "after_captured_at": datetime(2026, 8, 13, 10, 0, 2, tzinfo=UTC),
        "after_url": "https://example.com/detail/1001",
        "after_page_type": "product_detail",
        "after_action_log": '[{"action":"click","selector":"a.product-detail"}]',
        "after_artifacts": (
            '[{"artifact_id":"artifact-after-dom","artifact_type":"dom",'
            '"storage_path":"evidence/after-dom",'
            f'"task_id":"{TASK_ID}","page_capture_id":"{AFTER_ID}",'
            '"created_at":"2026-08-13T10:00:02+00:00"},'
            '{"artifact_id":"artifact-after-shot","artifact_type":"screenshot",'
            '"storage_path":"evidence/after-shot",'
            f'"task_id":"{TASK_ID}","page_capture_id":"{AFTER_ID}",'
            '"created_at":"2026-08-13T10:00:02+00:00"},'
            '{"artifact_id":"artifact-after-ui","artifact_type":"ui_snapshot",'
            '"storage_path":"evidence/after-ui",'
            f'"task_id":"{TASK_ID}","page_capture_id":"{AFTER_ID}",'
            '"created_at":"2026-08-13T10:00:02+00:00"}]'
        ),
    }
    connection = _Connection(row)

    async def fake_get_pool(database_url=None):
        return _Pool(connection)

    monkeypatch.setattr(action_evidence_tools, "get_postgres_pool", fake_get_pool)

    result = await action_evidence_tools._query_action_evidence(
        task_id=TASK_ID,
        action_execution_id="action-001",
    )

    assert result is not None
    assert result["intent_payload"]["action_type"] == "click"
    assert result["before"]["captured_at"] == datetime(
        2026, 8, 13, 10, 0, tzinfo=UTC
    )
    assert result["after"]["action_log"][0]["selector"] == "a.product-detail"
    assert result["after"]["artifacts"][0]["artifact_type"] == "dom"


async def test_wait_requeries_until_completed_evidence_is_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """completed 但证据不全时不应提前返回。"""
    incomplete = deepcopy(_complete_snapshot())
    after = incomplete["after"]
    assert isinstance(after, dict)
    after["page_type"] = None
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


@pytest.mark.parametrize(
    "status",
    ["blocked", "failed", "timed_out", "evidence_failed"],
)
async def test_wait_returns_terminal_failure_without_sleep(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """失败终态不应无意义等待到超时。"""
    failed = deepcopy(_complete_snapshot())
    failed["status"] = status
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


async def test_wait_timeout_returns_current_status_and_gaps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """超时时应保留当前事实和精确缺口。"""
    incomplete = deepcopy(_complete_snapshot())
    after = incomplete["after"]
    assert isinstance(after, dict)
    after["page_type"] = None

    async def fake_query(**kwargs):
        return incomplete

    async def fake_sleep(seconds: float) -> None:
        return None

    ticks = iter((10.0, 10.0, 11.0, 11.0))
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


async def test_wait_missing_action_never_queries_without_task_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动作不存在时仍不得退化成仅按动作 ID 探测。"""
    calls: list[tuple[str, str]] = []

    async def fake_query(*, task_id: str, action_execution_id: str):
        calls.append((task_id, action_execution_id))
        return None

    async def fake_sleep(seconds: float) -> None:
        return None

    ticks = iter((1.0, 1.0, 2.0, 2.0))
    monkeypatch.setattr(action_evidence_tools, "_query_action_evidence", fake_query)
    monkeypatch.setattr(action_evidence_tools, "_sleep", fake_sleep)
    monkeypatch.setattr(action_evidence_tools, "_monotonic", lambda: next(ticks))

    result = await action_evidence_tools._wait_for_action_evidence(
        task_id=TASK_ID,
        action_execution_id="action-unknown",
        timeout_seconds=1,
    )

    assert calls and set(calls) == {(TASK_ID, "action-unknown")}
    assert result["wait"]["missing_requirements"] == ["action_execution"]


@pytest.mark.parametrize(
    ("requested", "expected"),
    [(60, 60), (181, 180), (999, 180)],
)
async def test_timeout_normalization_defaults_to_60_and_caps_at_180(
    monkeypatch: pytest.MonkeyPatch,
    requested: int,
    expected: int,
) -> None:
    """等待值超过硬上限时不应延长 Agent Run。"""
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

    with bind_agent_run_context(AgentRunContext(task_id=TASK_ID, agent_run_id="run-1")):
        result = await action_evidence_tools.get_action_execution_evidence(
            action_execution_id="action-001",
            timeout_seconds=requested,
        )

    assert calls == [(TASK_ID, "action-001", expected)]
    assert result["wait"]["timeout_seconds"] == expected


@pytest.mark.parametrize(
    ("action_execution_id", "timeout_seconds"),
    [("", 60), ("   ", 60), ("x" * 97, 60), ("action-001", 0)],
)
async def test_invalid_request_returns_without_query(
    monkeypatch: pytest.MonkeyPatch,
    action_execution_id: str,
    timeout_seconds: int,
) -> None:
    """无效请求不应占用数据库连接。"""
    async def fail_wait(**kwargs):
        raise AssertionError("无效请求不应查询数据库")

    monkeypatch.setattr(action_evidence_tools, "_wait_for_action_evidence", fail_wait)

    result = await action_evidence_tools.get_action_execution_evidence(
        action_execution_id=action_execution_id,
        timeout_seconds=timeout_seconds,
    )

    assert result["wait"]["outcome"] == "invalid_request"
    assert result["wait"]["error_code"] == "ACTION_EVIDENCE_INVALID_REQUEST"


async def test_boolean_timeout_returns_stable_integer_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_wait(**kwargs):
        raise AssertionError("无效布尔超时不应查询数据库")

    monkeypatch.setattr(action_evidence_tools, "_wait_for_action_evidence", fail_wait)

    result = await action_evidence_tools.get_action_execution_evidence(
        action_execution_id="action-001",
        timeout_seconds=True,
    )

    assert result["wait"]["outcome"] == "invalid_request"
    assert result["wait"]["timeout_seconds"] == 60
    assert type(result["wait"]["timeout_seconds"]) is int


async def test_missing_agent_context_returns_blocked_without_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有任务上下文时不应执行任何跨任务查询。"""
    async def fail_wait(**kwargs):
        raise AssertionError("缺少上下文时不应查询数据库")

    monkeypatch.setattr(action_evidence_tools, "_wait_for_action_evidence", fail_wait)

    result = await action_evidence_tools.get_action_execution_evidence(
        action_execution_id="action-001"
    )

    assert result["wait"]["outcome"] == "blocked"
    assert result["wait"]["error_code"] == "ACTION_EVIDENCE_CONTEXT_REQUIRED"


async def test_query_exception_returns_generic_error_without_driver_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """数据库异常不应泄露连接串或驱动消息。"""
    async def failed_query(**kwargs):
        raise RuntimeError("postgres://secret@db/internal table qc_action_execution")

    monkeypatch.setattr(action_evidence_tools, "_query_action_evidence", failed_query)

    result = await action_evidence_tools._wait_for_action_evidence(
        task_id=TASK_ID,
        action_execution_id="action-001",
        timeout_seconds=60,
    )

    assert result["wait"]["outcome"] == "error"
    assert result["wait"]["error_code"] == "ACTION_EVIDENCE_QUERY_FAILED"
    assert result["wait"]["error_message"] == "动作证据查询失败"
    assert "secret" not in str(result)


def test_action_evidence_tool_is_registered_as_safe() -> None:
    """遗漏注册或错误提升权限时 Agent 不应获得该只读能力。"""
    metadata = default_registry.get("get_action_execution_evidence_tool")

    assert metadata is not None
    assert metadata.category == "动作证据查询"
    assert metadata.permission_level is PermissionLevel.SAFE
    parameters = metadata.tool.parameters()
    assert parameters["required"] == ["action_execution_id"]
    assert set(parameters["properties"]) == {"action_execution_id", "timeout_seconds"}
    assert "task_id" not in parameters["properties"]


async def test_registered_tool_uses_bound_task_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模型调用 Tool 时不得自行提供或绕过当前任务身份。"""
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
    assert metadata is not None

    with bind_agent_run_context(AgentRunContext(task_id=TASK_ID, agent_run_id="run-1")):
        content = await metadata.tool.invoke(
            arguments={"action_execution_id": "action-001"}
        )

    payload = json.loads(content[0].text)
    assert payload["wait"]["outcome"] == "ready"
    assert calls == [(TASK_ID, "action-001", 60)]
