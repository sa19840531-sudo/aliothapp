"""关闭后的补采组合入口遵守原预算、互斥、取消和清理顺序。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from services.api.app.application.h5_closed_session_recapture import (
    drain_h5_closed_session_snapshots,
)
from services.api.app.domain.exploration import ExplorationBudget, ExplorationWork


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "empty",
        "locked",
        "expired",
        "failure",
        "cancel",
        "stalled",
        "timeout",
        "failure_cleanup",
        "cancel_cleanup",
    ],
)
async def test_closed_drain_composition_cleanup_and_frozen_budget(monkeypatch, mode):
    from services.api.app.application import (
        h5_live_inspection_publish,
        h5_runtime_composition,
        h5_task_runtime_settings,
    )
    from services.api.app.infrastructure.browser import capture_runtime
    from services.api.app.infrastructure.db import (
        h5_closed_session_context,
        llm_usage_event_repository,
    )
    from services.api.app.infrastructure.db import pool as pool_module

    deadline = datetime.now(UTC) + timedelta(seconds=-1 if mode == "expired" else 60)
    budget = ExplorationBudget(
        max_actions=10, remaining_actions=4, max_depth=3, remaining_duration_ms=90000
    )
    frozen_work = SimpleNamespace(
        task_id="task-h5",
        surface_kind=SimpleNamespace(value="web"),
        deadline=deadline,
        budget=budget,
    )
    monkeypatch.setattr(ExplorationWork, "from_dict", lambda _: frozen_work)
    contexts = SimpleNamespace(
        frozen_frontier=AsyncMock(return_value={"work_payload": {}, "finalization_epoch": 4}),
        has_closed_snapshot_requests=AsyncMock(side_effect=[mode != "empty", True, False]),
        check_budget=AsyncMock(),
    )
    if mode in {"stalled", "timeout"}:
        contexts.has_closed_snapshot_requests.side_effect = None
        contexts.has_closed_snapshot_requests.return_value = True
        monkeypatch.setattr(
            h5_live_inspection_publish, "DEFAULT_ACTIVE_SESSION_DRAIN_GRACE_SECONDS", 0.01
        )
    monkeypatch.setattr(
        h5_closed_session_context, "PostgresH5ClosedSessionContextReader", lambda: contexts
    )
    connection = SimpleNamespace(
        fetchval=AsyncMock(return_value=mode != "locked"), execute=AsyncMock()
    )

    @asynccontextmanager
    async def acquire():
        yield connection

    monkeypatch.setattr(
        pool_module, "get_postgres_pool", AsyncMock(return_value=SimpleNamespace(acquire=acquire))
    )
    monkeypatch.setattr(
        h5_task_runtime_settings,
        "build_h5_task_runtime_settings",
        lambda **_: SimpleNamespace(max_actions=8),
    )
    audit = SimpleNamespace(usage_totals_for_task=AsyncMock(return_value={}))
    monkeypatch.setattr(
        llm_usage_event_repository, "PostgresLLMUsageEventRepository", lambda: audit
    )
    monkeypatch.setattr(h5_runtime_composition, "_h5_usage_budget_error", lambda *_, **__: None)
    monkeypatch.setattr(h5_runtime_composition, "PersistingH5PageDetector", lambda **_: object())
    browser_factory = Mock(return_value=object())
    monkeypatch.setattr(capture_runtime, "build_default_web_capture_runtime", browser_factory)
    drain = SimpleNamespace(close_recapture_sessions=AsyncMock())
    if mode.endswith("_cleanup"):
        drain.close_recapture_sessions.side_effect = RuntimeError("回收失败")
    built = {}

    async def drain_once(**_):
        from services.api.app.agents.runtime.run_context import get_agent_run_context

        assert get_agent_run_context().h5_llm_budget is True
        await built["closed_session_checkpoint"]()
        if mode in {"failure", "failure_cleanup"}:
            raise RuntimeError("现场失败")
        if mode in {"cancel", "cancel_cleanup"}:
            raise asyncio.CancelledError()
        if mode == "timeout":
            await asyncio.sleep(1)
        if mode == "stalled":
            return False
        return True

    drain.drain_closed_session_once = AsyncMock(side_effect=drain_once)

    def build(**kwargs):
        built.update(kwargs)
        return drain

    monkeypatch.setattr(
        h5_live_inspection_publish,
        "build_default_h5_active_session_inspection_drain_service",
        build,
    )
    record = SimpleNamespace(
        task_id="task-h5",
        run_id="run-h5",
        loop_guard={},
        page_sampling_policy={},
        task_config={"workflow_version": "v2", "_task_target": {"platform": "h5"}},
    )
    settings = SimpleNamespace(
        supplemental_evidence=SimpleNamespace(enabled=True),
        agent_runtime=object(),
        workflow=SimpleNamespace(h5_visual_coordinate_runtime_mode="enforced", h5_v2_mode="on"),
        inspection_work_items=SimpleNamespace(retry_base_seconds=1),
    )
    if mode in {"failure", "cancel", "failure_cleanup", "cancel_cleanup"}:
        with pytest.raises(
            RuntimeError if mode.startswith("failure") else asyncio.CancelledError
        ) as error:
            await drain_h5_closed_session_snapshots(
                record=record, settings=settings, worker_instance_id="worker"
            )
        if mode.endswith("_cleanup"):
            assert any("回收失败" in note for note in error.value.__notes__)
        if mode.startswith("failure"):
            assert str(error.value) == "现场失败"
    else:
        result = await drain_h5_closed_session_snapshots(
            record=record, settings=settings, worker_instance_id="worker"
        )
        assert result == (1 if mode == "success" else 0)
    if mode in {
        "success",
        "failure",
        "cancel",
        "stalled",
        "timeout",
        "failure_cleanup",
        "cancel_cleanup",
    }:
        assert built["closed_session_deadline"] == deadline
        assert built["recapture_budget"].remaining_actions == 4
        assert built["recapture_budget"].remaining_duration_ms <= 60000
        assert built["supplemental_evidence_policy"].runtime_ready
        contexts.check_budget.assert_awaited_once_with(
            task_id="task-h5",
            agent_run_id="run-h5",
            finalization_epoch=4,
            max_actions=8,
            deadline=deadline,
        )
        drain.close_recapture_sessions.assert_awaited_once()
    else:
        browser_factory.assert_not_called()
    if mode not in {"locked", "expired"}:
        connection.execute.assert_awaited_once_with(
            "SELECT pg_advisory_unlock(hashtext($1))",
            "h5-new-session-recapture:task-h5",
        )
    else:
        connection.execute.assert_not_awaited()


@pytest.mark.parametrize("platform,enabled", [("web", True), ("app", True), ("h5", False)])
async def test_other_surfaces_and_disabled_feature_do_not_construct_recapture(platform, enabled):
    record = SimpleNamespace(
        task_id="task",
        task_config={"workflow_version": "v2", "_task_target": {"platform": platform}},
    )
    settings = SimpleNamespace(supplemental_evidence=SimpleNamespace(enabled=enabled))
    assert (
        await drain_h5_closed_session_snapshots(
            record=record,
            settings=settings,
            worker_instance_id="worker",
        )
        == 0
    )


@pytest.mark.parametrize("platform,enabled", [("h5", True), ("web", True), ("h5", False)])
async def test_worker_settles_orphan_scope_before_opening_new_session(
    monkeypatch, platform, enabled
):
    from services.api.app import worker
    from services.api.app.application import h5_closed_session_recapture

    calls = []

    class Settlement:
        def __init__(self, *, allow_new_session_recovery):
            assert allow_new_session_recovery

        async def settle_requests_after_session_close(self, **_):
            calls.append("settle")

    async def drain(**_):
        calls.append("drain")
        return 1

    monkeypatch.setattr(worker, "PostgresH5ProbeTerminalSettlementRepository", Settlement)
    monkeypatch.setattr(h5_closed_session_recapture, "drain_h5_closed_session_snapshots", drain)
    result = await worker._drain_h5_closed_snapshots(
        SimpleNamespace(
            task_id="task",
            run_id="main",
            task_config={"workflow_version": "v2", "_task_target": {"platform": platform}},
        ),
        settings=SimpleNamespace(
            supplemental_evidence=SimpleNamespace(enabled=enabled),
            workflow=SimpleNamespace(h5_v2_mode="on"),
        ),
        worker_instance_id="worker",
    )
    assert calls == (["settle", "drain"] if platform == "h5" and enabled else [])
    assert result == (1 if calls else 0)


@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_closed_recapture_cannot_execute_when_h5_mode_is_not_on(mode):
    from services.api.app import worker

    record = SimpleNamespace(
        task_id="task",
        task_config={"workflow_version": "v2", "_task_target": {"platform": "h5"}},
    )
    settings = SimpleNamespace(
        supplemental_evidence=SimpleNamespace(enabled=True),
        workflow=SimpleNamespace(h5_v2_mode=mode),
    )
    assert (
        await drain_h5_closed_session_snapshots(
            record=record,
            settings=settings,
            worker_instance_id="worker",
        )
        == 0
    )
    assert (
        await worker._drain_h5_closed_snapshots(
            record,
            settings=settings,
            worker_instance_id="worker",
        )
        == 0
    )
