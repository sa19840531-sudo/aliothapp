"""复盘中的页型收益、现场交接和整任务有界合同。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application import h5_runtime_composition as composition
from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.application.h5_live_inspection_publish import (
    H5ActiveSessionDrainState,
    H5ActiveSessionInspectionDrainService,
)
from services.api.app.core.config import Settings
from services.api.app.domain.exploration.h5_frontier import (
    H5TaskFrontierSnapshot,
    select_next_h5_entry,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.tests.test_h5_exploration_observation import _detection
from services.api.tests.test_h5_live_inspection_publish import (
    _ActiveConsumer,
    _DrainStateReader,
    _LivePublisher,
)
from services.api.tests.test_h5_runtime_composition import (
    _completed_outcome,
    _h5_record,
    _runtime_settings,
)
from services.api.tests.test_h5_task_frontier import _candidate


async def test_resume_uses_frozen_start_and_remaining_duration():
    started = datetime(2026, 10, 9, tzinfo=UTC)
    record = _h5_record()
    record.started_at = started
    commands = []

    async def start(command):
        commands.append(command)

    runtime = SimpleNamespace(
        runtime_settings=_runtime_settings(),
        start=start,
        run_one_turn=AsyncMock(return_value=_completed_outcome()),
        close=AsyncMock(),
    )
    await composition.ProductionH5V2RuntimeExecutor(
        worker_instance_id="test",
        now=lambda: started + timedelta(seconds=90),
    ).run(runtime, record=record, settings=Settings())
    assert commands[0].deadline == started + timedelta(seconds=120)
    assert commands[0].budget.remaining_duration_ms == 30_000


async def test_expired_run_never_opens_session_or_claims_work():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    record = _h5_record()
    record.started_at = now - timedelta(seconds=121)
    runtime = SimpleNamespace(
        runtime_settings=_runtime_settings(),
        start=AsyncMock(),
        run_one_turn=AsyncMock(return_value=_completed_outcome()),
        close=AsyncMock(),
    )
    result = await composition.ProductionH5V2RuntimeExecutor(
        worker_instance_id="test",
        now=lambda: now,
    ).run(runtime, record=record, settings=Settings())
    runtime.start.assert_not_awaited()
    runtime.run_one_turn.assert_not_awaited()
    assert result.error_message == "h5_task_deadline_exhausted"


@pytest.mark.parametrize("remote", [False, True])
def test_uncovered_business_type_precedes_more_same_type_list_samples(remote):
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="old",
        source_state_version="old",
        source_location="https://example.test/list",
        navigation_depth=0,
        candidates=(_candidate("first", page_type="content_list"),),
    )
    first = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(first.entry_id).mark_executed(first.entry_id)
    new_type = _candidate("account-entry", page_type="profile")
    snapshot = snapshot.merge(
        source_capture_id="previous",
        source_state_version="previous",
        source_location="https://example.test/list",
        navigation_depth=0,
        candidates=(new_type,) if remote else (),
    )
    scroll = replace(
        _candidate("more-list", page_type="content_list"),
        action_type=InteractionOperation.SCROLL,
        metadata={
            "expected_next_page_type": "content_list",
            "source_page_type": "content_list",
            "target_keywords": ["list_scroll_coverage"],
            "action_parameters": {"target_mode": "h5_list_scroll_coverage"},
        },
    )
    snapshot = snapshot.merge(
        source_capture_id="current",
        source_state_version="current",
        source_location="https://example.test/list",
        navigation_depth=0,
        candidates=(scroll,) if remote else (scroll, new_type),
    )
    selected = select_next_h5_entry(snapshot, active_state_version="current")
    assert selected.candidate.candidate_id == "account-entry"


def test_popup_without_candidates_reuses_bounded_fresh_observation():
    detection = _detection(capture_id="popup", evidence_id="popup-image")
    detection.update(
        page_state="popup",
        candidate_actions=[],
        rule_matching_allowed=False,
        obstruction_facts={
            "blocking": True,
            "snapshot_evidence_status": "ready",
            "modal_census_complete": True,
            "modal_census_count": 1,
        },
    )
    first = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 10, 9, tzinfo=UTC),
        navigation_depth=0,
    )
    assert len(first.candidates) == 1
    assert first.candidates[0].action_type is InteractionOperation.OBSERVE
    assert first.candidates[0].metadata["capability_projection_reobserve_attempt"] == 1
    exhausted = build_h5_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 10, 9, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"capability_projection_reobserve_count": 3},
    )
    assert not exhausted.candidates
    assert exhausted.observation.metadata["capability_projection_reobserve_exhausted"] is True


async def test_checkpoint_reconciles_new_probe_page_before_returning_to_explorer():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    authority = SimpleNamespace(reconcile_available=AsyncMock(return_value=1))
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader(
            [H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0)]
        ),
        coverage_authority=authority,
        probe_drain=SimpleNamespace(drain_one=AsyncMock(return_value=True)),
        now=lambda: now,
    )
    await service.run_current_surface_checkpoint(
        task_id="task", agent_run_id="run", deadline=now + timedelta(minutes=1)
    )
    authority.reconcile_available.assert_awaited()


@pytest.mark.parametrize("checkpoint", [False, True])
async def test_attempted_probe_without_evidence_does_not_reset_idle_limit(checkpoint):
    now = datetime(2026, 10, 9, tzinfo=UTC)
    clock = [now]

    async def probe(**kwargs):
        clock[0] += timedelta(seconds=10)
        return True

    async def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=_ActiveConsumer(),
        state_reader=_DrainStateReader(
            [H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=1)]
        ),
        probe_drain=SimpleNamespace(drain_one=probe),
        now=lambda: clock[0],
        sleep=sleep,
        grace_seconds=20,
        checkpoint_max_wait_seconds=20,
        max_wait_seconds=100,
    )
    method = service.run_current_surface_checkpoint if checkpoint else service.drain_before_close
    with pytest.raises(RuntimeError):
        await method(task_id="task", agent_run_id="run", deadline=now + timedelta(hours=1))
    assert (clock[0] - now).total_seconds() <= 40


async def test_close_deadline_does_not_start_more_model_or_browser_work():
    now = datetime(2026, 10, 9, tzinfo=UTC)
    publisher = _LivePublisher([0])
    consumer = _ActiveConsumer()
    service = H5ActiveSessionInspectionDrainService(
        publisher=publisher,
        consumer=consumer,
        state_reader=_DrainStateReader(
            [H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0)]
        ),
        now=lambda: now,
    )
    await service.drain_before_close(task_id="task", agent_run_id="run", deadline=now)
    assert not publisher.calls
    assert not consumer.calls


async def test_same_immutable_capture_reuses_detection_but_new_capture_does_not(monkeypatch):
    from services.api.tests.test_h5_exploration_turn import _capture_for_turn, _work

    detect = AsyncMock(return_value={"page_capture_id": "first", "page_state": "stable"})
    persist = AsyncMock(side_effect=lambda **kwargs: kwargs["result"])
    monkeypatch.setattr(composition, "detect_page_for_agent", detect)
    monkeypatch.setattr(composition, "persist_page_detection_for_current_run", persist)
    service = composition.PersistingH5PageDetector()
    capture = _capture_for_turn(_work(), "first")
    first = await service.detect(capture=capture, task_id="task", agent_run_id="run")
    first["page_state"] = "corrupted"
    second = await service.detect(capture=capture, task_id="task", agent_run_id="run")
    assert second["page_state"] == "stable"
    assert detect.await_count == 1
    await service.detect(
        capture=replace(capture, capture_id="next"), task_id="task", agent_run_id="run"
    )
    assert detect.await_count == 2


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "overlay",
        "external",
        "unrecognized",
        "known",
        "operation_denied",
        "excluded",
        "max_pages",
        "route_denied",
        "deadline",
        "duration",
    ],
)
async def test_probe_new_type_handoff_preserves_original_goal_and_takes_normal_observation(fault):
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.tests.test_h5_exploration_turn import _capture_for_turn, _work

    work = _work()
    if fault != "operation_denied":
        work = replace(
            work,
            boundaries=replace(
                work.boundaries,
                allowed_operations=(
                    *work.boundaries.allowed_operations,
                    InteractionOperation.OBSERVE,
                ),
            ),
        )
    if fault == "route_denied":
        work = replace(
            work, boundaries=replace(work.boundaries, allowed_route_prefixes=("/content",))
        )
    elif fault == "deadline":
        work = replace(work, deadline=work.frontier.observation.observed_at)
    elif fault == "duration":
        work = replace(work, budget=replace(work.budget, remaining_duration_ms=0))
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=1 if fault == "max_pages" else None,
        sampling_policy_summary={"exclude_page_types": ["profile"]} if fault == "excluded" else {},
    ).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=work.frontier.candidates,
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    if fault in {"known", "max_pages"}:
        snapshot = snapshot.record_observed_page_type(
            source_capture_id="earlier",
            source_state_version="earlier",
            source_location=work.frontier.observation.location,
            navigation_depth=0,
            page_type="profile" if fault == "known" else "home",
        )
    frontier = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
    capture = replace(_capture_for_turn(work, "probe"), role="probe_result_replay")
    detection = _detection(capture_id="probe", evidence_id="probe-shot")
    detection.update(
        page_type="profile",
        candidate_actions=[],
        page_url="https://external.test/"
        if fault == "external"
        else "https://m.example.test/account",
        page_state="popup" if fault == "overlay" else "stable",
        detection_status="insufficient" if fault == "unrecognized" else "recognized",
    )
    session = SimpleNamespace(
        task_id=work.task_id,
        surface_kind=work.surface_kind,
        status=SimpleNamespace(value="active"),
        session_owner="h5-workflow",
        current_capture_id="probe",
        finalization_epoch=0,
        command_sequence=3,
    )
    service = composition.H5CoordinatorFrontierRecovery(
        capture_coordinator=object(),
        detector=SimpleNamespace(detect=AsyncMock(return_value=detection)),
        assessment_repository=object(),
        surface_sessions=SimpleNamespace(get=AsyncMock(return_value=session)),
        captures=SimpleNamespace(
            get_verified_observation_capture=AsyncMock(
                return_value=SimpleNamespace(capture=capture)
            )
        ),
        now=lambda: work.frontier.observation.observed_at,
    )
    result = await service.synchronize(work=work, agent_run_id="run-h5")
    updated = H5TaskFrontierSnapshot.from_dict(
        result.frontier.observation.metadata["h5_task_frontier"]
    )
    original = next(
        item for item in updated.entries if item.entry_id == snapshot.entries[0].entry_id
    )
    if fault is not None:
        assert original.status.value == "selected"
        assert result.frontier.observation.metadata["h5_frontier_source_recovery_pending"] is True
        return
    assert original.status.value == "pending"
    assert original.source_capture_id == work.source_capture_id
    assert len(result.frontier.candidates) == 1
    candidate = result.frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.OBSERVE
    assert candidate.metadata["action_parameters"]["target_mode"] == "h5_recapture_business_handoff"
    assert result.budget == work.budget and result.deadline == work.deadline
    assert (
        result.frontier.observation.metadata.get("h5_frontier_source_recovery_pending") is not True
    )
    assert composition._trusted_h5_capability_candidate_parameters(
        work=result,
        candidate=candidate,
    ) == {"target_mode": "h5_recapture_business_handoff"}
    decision = await composition.H5PlatformSelectedExplorer().decide_next(result)
    assert decision.source_capture_id == "probe"
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.deadline == work.deadline
    assert decision.capture_profile.after.role == "after_action"
    assert "supplemental_evidence_source" not in decision.parameters


async def test_exhausted_task_usage_prevents_even_bootstrap_detection():
    runtime = SimpleNamespace(
        runtime_settings=_runtime_settings(max_ai_calls=2),
        start=AsyncMock(),
        run_one_turn=AsyncMock(),
        close=AsyncMock(),
    )
    reader = SimpleNamespace(usage_totals_for_task=AsyncMock(return_value=(2, 100)))
    result = await composition.ProductionH5V2RuntimeExecutor(
        worker_instance_id="test",
        audited_usage_reader=reader,
    ).run(runtime, record=_h5_record(), settings=Settings())
    assert result.error_message == "h5_llm_call_budget_exhausted"
    runtime.start.assert_not_awaited()
    runtime.close.assert_not_awaited()


async def test_detection_cache_does_not_cross_run_or_reuse_failed_persistence(monkeypatch):
    from services.api.tests.test_h5_exploration_turn import _capture_for_turn, _work

    capture = _capture_for_turn(_work(), "same")
    detect = AsyncMock(return_value={"page_capture_id": "same"})
    persist = AsyncMock(
        side_effect=[
            RuntimeError("storage"),
            {"page_capture_id": "same"},
            {"page_capture_id": "same"},
        ]
    )
    monkeypatch.setattr(composition, "detect_page_for_agent", detect)
    monkeypatch.setattr(composition, "persist_page_detection_for_current_run", persist)
    service = composition.PersistingH5PageDetector()
    with pytest.raises(RuntimeError, match="storage"):
        await service.detect(capture=capture, task_id="task", agent_run_id="first")
    await service.detect(capture=capture, task_id="task", agent_run_id="first")
    await service.detect(capture=capture, task_id="task", agent_run_id="second")
    assert detect.await_count == 3


async def test_task_usage_includes_separate_consumer_runs():
    from services.api.app.infrastructure.db.llm_usage_event_repository import (
        PostgresLLMUsageEventRepository,
    )

    connection = SimpleNamespace(
        fetchrow=AsyncMock(return_value={"ai_calls": 96, "total_tokens": 450})
    )
    lease = AsyncMock()
    lease.__aenter__.return_value = connection
    pool = SimpleNamespace(acquire=lambda: lease)
    totals = await PostgresLLMUsageEventRepository(pool=pool).usage_totals_for_task(task_id="task")
    assert totals == (96, 450)
    query, task = connection.fetchrow.call_args.args
    assert "WHERE task_id = $1::uuid" in query
    assert "WHERE agent_run_id" not in query
    assert task == "task"


async def test_h5_inflight_wait_stops_at_deadline_without_even_reading_backlog():
    from services.api.app import worker

    now = datetime.now(UTC)
    service = SimpleNamespace(count_inflight_leased_work=AsyncMock(return_value=1))
    waited = await worker._wait_for_inflight_inspection_work(
        service,
        task_id="task",
        absolute_deadline=now - timedelta(seconds=1),
    )
    assert waited == 0
    service.count_inflight_leased_work.assert_not_awaited()


async def test_h5_wait_measures_database_time_not_only_poll_sleep():
    import asyncio

    from services.api.app import worker

    async def slow_read(**kwargs):
        await asyncio.sleep(0.03)
        return 1

    service = SimpleNamespace(
        count_inflight_leased_work=AsyncMock(side_effect=slow_read),
        find_active_consumer_run=AsyncMock(return_value=None),
    )
    waited = await worker._wait_for_inflight_inspection_work(
        service,
        task_id="task",
        absolute_deadline=datetime.now(UTC) + timedelta(seconds=1),
        max_wait_seconds=0.02,
        poll_seconds=0.01,
    )
    assert service.count_inflight_leased_work.await_count == 1
    assert waited >= 0.02


@pytest.mark.parametrize("checkpoint", [False, True])
async def test_slow_judge_crossing_deadline_cannot_start_probe(checkpoint):
    now = datetime(2026, 10, 9, tzinfo=UTC)
    clock = [now]

    async def judge(**kwargs):
        clock[0] += timedelta(seconds=20)
        return SimpleNamespace(claimed_count=1, completed_count=1)

    probe = SimpleNamespace(drain_one=AsyncMock(return_value=False))
    service = H5ActiveSessionInspectionDrainService(
        publisher=_LivePublisher([0]),
        consumer=SimpleNamespace(drain_available=judge),
        state_reader=_DrainStateReader(
            [
                H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=1),
                H5ActiveSessionDrainState(pending_capture_count=0, nonterminal_work_item_count=0),
            ]
        ),
        probe_drain=probe,
        now=lambda: clock[0],
    )
    method = service.run_current_surface_checkpoint if checkpoint else service.drain_before_close
    await method(task_id="task", agent_run_id="run", deadline=now + timedelta(seconds=10))
    probe.drain_one.assert_not_awaited()


async def test_hanging_session_close_has_bounded_failure_not_false_completion(monkeypatch):
    import asyncio

    now = datetime(2026, 10, 9, tzinfo=UTC)
    closed = asyncio.Event()

    async def close(**kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(composition, "_H5_SHUTDOWN_TIMEOUT_SECONDS", 0.01, raising=False)
    runtime = SimpleNamespace(
        runtime_settings=_runtime_settings(),
        start=AsyncMock(),
        run_one_turn=AsyncMock(return_value=_completed_outcome()),
        close=close,
    )
    result = await asyncio.wait_for(
        composition.ProductionH5V2RuntimeExecutor(
            worker_instance_id="test",
            now=lambda: now,
        ).run(runtime, record=_h5_record(), settings=Settings()),
        timeout=0.2,
    )
    assert closed.is_set()
    assert result.execution_outcome.value == "needs_review"


async def test_expired_h5_snapshot_drain_settles_without_new_judge_or_recapture(monkeypatch):
    from services.api.app import worker
    from services.api.app.infrastructure.queue.redis_queue import AgentRunQueueStatus

    consumer = SimpleNamespace(drain_snapshot=AsyncMock())
    service = SimpleNamespace(
        find_active_consumer_run=AsyncMock(return_value="consumer"),
        reopen_waiting_evidence=AsyncMock(return_value=0),
        settle_stalled_work_for_closure=AsyncMock(
            return_value=SimpleNamespace(
                settled=False, settled_work_item_count=0, settled_rule_count=0
            )
        ),
        finish_h5_consumer_run_if_idle=AsyncMock(),
    )
    recapture = AsyncMock(return_value=0)
    monkeypatch.setattr(worker, "_drain_h5_closed_snapshots", recapture)
    monkeypatch.setattr(worker, "PostgresInspectionWorkItemRepository", object)
    monkeypatch.setattr(worker, "InspectionWorkItemService", lambda **kwargs: service)
    monkeypatch.setattr(worker, "InspectionWorkItemConsumer", lambda **kwargs: consumer)
    monkeypatch.setattr(
        worker,
        "PostgresH5ProbeTerminalSettlementRepository",
        lambda **kwargs: SimpleNamespace(
            settle_requests_after_session_close=AsyncMock(return_value=0)
        ),
    )
    record = _h5_record()
    record.status = AgentRunQueueStatus.COMPLETED
    record.started_at = datetime.now(UTC) - timedelta(days=1)
    await worker._drain_snapshot_inspection_work(
        record, settings=Settings(), worker_instance_id="test"
    )
    consumer.drain_snapshot.assert_not_awaited()
    recapture.assert_not_awaited()
    service.settle_stalled_work_for_closure.assert_awaited_once()
    assert record.usage["h5_snapshot_deadline_exhausted"] is True


@pytest.mark.parametrize("platform", ["h5", "app", "web"])
def test_frozen_finalization_deadline_is_h5_only(platform):
    from services.api.app import worker

    record = _h5_record()
    record.task_config["_task_target"]["platform"] = platform
    record.started_at = datetime(2026, 10, 9, tzinfo=UTC)
    record.loop_guard["max_duration_seconds"] = 120
    deadline = worker._h5_inspection_deadline(record)
    assert deadline == (record.started_at + timedelta(seconds=120) if platform == "h5" else None)
