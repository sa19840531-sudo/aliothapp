"""冻结探索期限耗尽后只能对账收口，不能产生新的浏览器副作用。"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordination import (
    CaptureCoordinationError,
    CaptureCoordinationStage,
)
from services.api.app.application.web_run_stage_orchestrator import WebStageFailure
from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
from services.api.tests.test_web_runtime_stage_effects import Evidence, command, cursor
from services.api.tests.web_runtime_recovery_fixture import RecoveryWorld


@pytest.mark.parametrize(
    "stage",
    [
        WebRunStage.BOOTSTRAP_NAVIGATION,
        WebRunStage.OBSTRUCTION_CLEAR,
        WebRunStage.READINESS,
        WebRunStage.PRIMARY_CAPTURE,
        WebRunStage.FULL_PAGE_CAPTURE,
        WebRunStage.CLASSIFICATION,
        WebRunStage.HREF_DISCOVERY,
        WebRunStage.CANDIDATE_NAVIGATION,
        WebRunStage.AD_ISOLATED_NAVIGATION,
        WebRunStage.AD_CAPTURE,
    ],
)
async def test_expired_stage_stops_before_browser_or_model_access(stage):
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(),
        evidence=Evidence(),
        command=replace(command(), deadline=datetime.now(UTC) - timedelta(seconds=1)),
    )
    with pytest.raises(WebStageFailure) as failure:
        await effects.execute(cursor(stage))
    assert failure.value.code == "WEB_EXPLORATION_DEADLINE_EXHAUSTED"
    assert failure.value.transient is False


@pytest.mark.parametrize("expires_during_capture", [False, True])
async def test_readiness_coordination_failure_remains_a_stage_gap(expires_during_capture):
    class Readiness:
        async def prepare(self, **_kwargs):
            if expires_during_capture:
                effects._command = replace(
                    effects._command, deadline=datetime.now(UTC) - timedelta(seconds=1)
                )
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.AFTER_CAPTURE,
                error_code="after_capture_failed",
                message="不可写入审计的异常正文",
                cause_type="TimeoutError",
                retryable=True,
            )

    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(page_readiness=Readiness()),
        evidence=Evidence(),
        command=command(),
    )
    with pytest.raises(WebStageFailure) as failure:
        await effects.execute(cursor(WebRunStage.READINESS))
    assert failure.value.code == (
        "WEB_EXPLORATION_DEADLINE_EXHAUSTED"
        if expires_during_capture
        else "WEB_READINESS_AFTER_CAPTURE_FAILED"
    )
    assert failure.value.transient is not expires_during_capture
    assert failure.value.detail["raw_error_code"] == "after_capture_failed"
    assert failure.value.detail["cause_type"] == "TimeoutError"
    assert "不可写入" not in str(failure.value.detail)


@pytest.mark.parametrize(
    "stage",
    [
        WebRunStage.FRONTIER_DRAIN,
        WebRunStage.EVIDENCE_RECONCILIATION,
        WebRunStage.COVERAGE_SNAPSHOT,
        WebRunStage.REPORT_FINALIZATION,
        WebRunStage.SESSION_CLOSE,
    ],
)
async def test_expired_cleanup_preserves_reconciliation_without_scheduling(stage):
    actions = []

    class Runtime:
        async def read_persisted_exploration(self, request):
            actions.append("summary")

        async def close(self, **_kwargs):
            actions.append("close")

    class Frontier:
        async def reconcile(self, request, current):
            actions.append("reconcile")

    effects = WebRuntimeStageEffects(
        runtime=Runtime(),
        evidence=Evidence(),
        frontier=Frontier(),
        command=replace(command(), deadline=datetime.now(UTC) - timedelta(seconds=1)),
    )
    current = cursor(stage)
    if stage is WebRunStage.EVIDENCE_RECONCILIATION:
        current = replace(current, logical_page_key="menu-test")
    await effects.execute(current)
    assert (
        actions
        == {
            WebRunStage.FRONTIER_DRAIN: [],
            WebRunStage.EVIDENCE_RECONCILIATION: ["reconcile"],
            WebRunStage.COVERAGE_SNAPSHOT: ["summary"],
            WebRunStage.REPORT_FINALIZATION: ["summary"],
            WebRunStage.SESSION_CLOSE: ["close"],
        }[stage]
    )


@pytest.mark.parametrize("resume_stage", ["bootstrap_navigation", "readiness", "frontier_drain"])
async def test_expired_real_runner_drains_and_closes_without_new_navigation(
    monkeypatch, resume_stage
):
    world = RecoveryWorld(monkeypatch, with_candidates=True)
    await world.run(stop_at=resume_stage)
    prior_actions = tuple(world.actions)
    prior_captures = tuple(world.captures)
    request = replace(command(), deadline=world.now - timedelta(seconds=1))
    runner = world.runner()
    await runner.initialize(request, adult_self_attestation=None)
    for _ in range(30):
        due = await runner.next_due(request.agent_run_id)
        if due is None:
            break
        await runner.advance_once(request.agent_run_id, due.logical_page_key)
    else:
        pytest.fail("过期阶段未能在固定图内收口")
    assert all(row.is_terminal for row in world.rows.values())
    assert tuple(world.captures) == prior_captures
    assert world.actions == [*prior_actions, "budget_sealed", "session_close"]
    assert world.closed
    assert set(world.rows) == {"home"}


async def test_capture_timeout_after_deadline_uses_real_gap_reconciliation(monkeypatch):
    world = RecoveryWorld(monkeypatch)
    request = replace(command(), deadline=world.now + timedelta(seconds=1))

    class Clock:
        @staticmethod
        def now(_tz):
            return world.now

    class Readiness:
        async def prepare(self, **_kwargs):
            world.now += timedelta(seconds=2)
            raise CaptureCoordinationError(
                stage=CaptureCoordinationStage.AFTER_CAPTURE,
                error_code="after_capture_failed",
                message="Capture deadline 已到",
                cause_type="TimeoutError",
                retryable=True,
            )

    monkeypatch.setattr("services.api.app.application.web_runtime_stage_effects.datetime", Clock)
    world.runtime.page_readiness = Readiness()
    runner = world.runner()
    await runner.initialize(request, adult_self_attestation=None)
    for _ in range(30):
        due = await runner.next_due(request.agent_run_id)
        if due is None:
            break
        await runner.advance_once(request.agent_run_id, due.logical_page_key)
    else:
        pytest.fail("采集超时未能完成阶段收口")
    result = await runner.final_result(request.agent_run_id)
    assert result.gap_codes == ("WEB_EXPLORATION_DEADLINE_EXHAUSTED",)
    assert world.rows["home"].is_terminal
    assert world.actions == ["bootstrap", "budget_sealed", "session_close"]
    assert world.records["home"].diagnostic["error_code"] == "WEB_EXPLORATION_DEADLINE_EXHAUSTED"
    assert result.exploration.page_timeline


async def test_ad_deadline_rechecked_after_waiting_for_session_gate():
    from services.api.app.application.web_ad_landing_evidence import WebAdLandingCaptureError
    from services.api.app.infrastructure.browser.capture_runtime import WebAdLandingCaptureAdapter

    request = SimpleNamespace(
        task_id="task",
        session_id="web:run",
        source_capture_id="capture",
        source_url="https://example.test/",
        destination_url="https://ads.example.test/",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    class Gate:
        @asynccontextmanager
        async def hold(self, **_kwargs):
            request.deadline = datetime.now(UTC) - timedelta(seconds=1)
            yield object()

    adapter = WebAdLandingCaptureAdapter(
        browser=SimpleNamespace(),
        persistence=SimpleNamespace(),
        session_command_gate=Gate(),
    )
    with pytest.raises(WebAdLandingCaptureError) as failure:
        await adapter.capture(request)
    assert failure.value.reason_code == "WEB_EXPLORATION_DEADLINE_EXHAUSTED"


async def test_classification_finishing_after_deadline_keeps_fact_without_new_branches():
    from services.api.tests.test_web_runtime_stage_effects import CAPTURE

    class Ready:
        async def read(self, **_kwargs):
            return SimpleNamespace(capture_id=CAPTURE)

    class Classifier:
        async def classify(self, ready):
            effects._command = replace(command(), deadline=datetime.now(UTC) - timedelta(seconds=1))
            return SimpleNamespace(page_type="home")

    evidence = Evidence()
    effects = WebRuntimeStageEffects(
        runtime=SimpleNamespace(
            stage_ready_capture_reader=Ready(), page_type_classifier=Classifier()
        ),
        evidence=evidence,
        command=command(),
        checkpoints=SimpleNamespace(),
        frontier=SimpleNamespace(),
    )
    result = await effects.execute(cursor(WebRunStage.CLASSIFICATION))
    assert result.cursor_payload["capture_id"] == CAPTURE
    assert len(evidence.saved) == 2


@pytest.mark.parametrize("reserved", [False, True])
async def test_deadline_menu_reconciles_only_actual_reservations(reserved):
    records = []

    class Ledger:
        async def read_summary(self, **_kwargs):
            return SimpleNamespace(
                runtime_discovered_ledger=(
                    SimpleNamespace(
                        entry_id="menu-test",
                        execution_slot=1 if reserved else None,
                        persisted_status="menu_reserved"
                        if reserved
                        else "runtime_discovered_unreviewed",
                        execution=SimpleNamespace() if reserved else None,
                    ),
                )
            )

        async def finalize(self, record):
            assert reserved, "未领取额度不得伪造导航失败"
            records.append(record)

    current = replace(cursor(WebRunStage.READINESS), logical_page_key="menu-test")
    current = replace(
        current, cursor_payload={**current.cursor_payload, "menu_ledger_id": "menu-test"}
    )
    current = current.record_gap_and_advance(error_code="WEB_EXPLORATION_DEADLINE_EXHAUSTED")
    frontier = WebRuntimeStageFrontier(
        ledger=Ledger(),
        candidate_source=None,
        navigator=None,
        site=SimpleNamespace(site_id="runtime"),
    )
    await frontier.reconcile(command(), current)
    assert len(records) == int(reserved)
    if reserved:
        assert records[0].status is WebMenuTerminalStatus.NAVIGATION_FAILED


async def test_expired_initialization_without_budget_still_closes(monkeypatch):
    world = RecoveryWorld(monkeypatch)

    async def has_budget(**_kwargs):
        return False

    async def absent_summary(*_args, **_kwargs):
        raise AssertionError("无预算时不得读摘要或伪造账本终态")

    world.runtime.budget_ledger.has_budget = has_budget
    world.runtime.budget_ledger.finalize = absent_summary
    world.runtime.read_persisted_exploration = absent_summary
    request = replace(command(), deadline=world.now - timedelta(seconds=1))
    runner = world.runner()
    await runner.initialize(request, adult_self_attestation=None)
    for _ in range(20):
        due = await runner.next_due(request.agent_run_id)
        if due is None:
            break
        await runner.advance_once(request.agent_run_id, due.logical_page_key)
    result = await runner.final_result(request.agent_run_id)
    assert result.exploration.page_timeline == ()
    assert result.gap_codes == ("WEB_EXPLORATION_DEADLINE_EXHAUSTED",)
    assert world.actions == ["session_close"]
    assert world.rows["home"].is_terminal
