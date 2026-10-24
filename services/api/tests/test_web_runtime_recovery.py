"""阶段恢复必须复用持久证据，并以真实领域状态机验证重试和租约。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.web_run_stage_orchestrator import (
    WebRunStageOrchestrator,
    WebStageEffectResult,
    WebStageFailure,
)
from services.api.app.domain.exploration.web_run_stage_checkpoint import (
    WebRunStage,
    WebStageCursor,
    WebStageStatus,
)
from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
    WebStageCheckpointConflict,
)

RUN_ID = "00000000-0000-0000-0000-000000000006"
CAPTURE_ID = "00000000-0000-0000-0000-000000000007"
ARTIFACT_ID = "00000000-0000-0000-0000-000000000008"
NOW = datetime(2026, 9, 15, tzinfo=UTC)


class Checkpoints:
    """只替换数据库 I/O，保留状态、身份、到期和 fencing 语义。"""

    def __init__(self, stage, *, running=False):
        self.now = NOW
        self.active = True
        self.commits = []
        self.cursor = WebStageCursor.start(
            run_id=RUN_ID,
            logical_page_key="page:1",
            stage=stage,
            cursor_payload={
                "session_id": f"web:{RUN_ID}",
                "finalization_epoch": 2,
                "capture_id": CAPTURE_ID,
            },
        )
        if running:
            self.cursor = self.cursor.claim(worker_id="dead", now=NOW - timedelta(seconds=61))

    async def get(self, **_kwargs):
        return self.cursor

    async def claim(self, *, expected_row_version, worker_id, **_kwargs):
        if not self.active or expected_row_version != self.cursor.row_version:
            raise WebStageCheckpointConflict("失效")
        self.cursor = replace(
            self.cursor.claim(worker_id=worker_id, now=self.now),
            row_version=self.cursor.row_version + 1,
        )
        self.commits.append("claim")
        return self.cursor

    def fence(self, cursor):
        if (
            not self.active
            or cursor.claim_token != self.cursor.claim_token
            or cursor.row_version != self.cursor.row_version
            or cursor.lease_expires_at <= self.now
        ):
            raise WebStageCheckpointConflict("旧租约禁止回写")

    async def mark_succeeded(self, cursor, *, artifact_ids=(), cursor_payload=None):
        self.fence(cursor)
        self.cursor = replace(
            cursor.mark_succeeded(artifact_ids=artifact_ids),
            cursor_payload={**cursor.cursor_payload, **(cursor_payload or {})},
            row_version=cursor.row_version + 1,
        )
        self.commits.append("success")
        return self.cursor

    async def schedule_retry(self, cursor, **kwargs):
        self.fence(cursor)
        self.cursor = replace(
            cursor.schedule_retry(now=self.now, **kwargs), row_version=cursor.row_version + 1
        )
        self.commits.append("retry")
        return self.cursor

    async def record_gap_and_advance(self, cursor, **kwargs):
        self.fence(cursor)
        self.cursor = replace(
            cursor.record_gap_and_advance(**kwargs), row_version=cursor.row_version + 1
        )
        self.commits.append("gap")
        return self.cursor


class Effects:
    def __init__(self, checkpoints):
        self.checkpoints = checkpoints
        self.completed = {}
        self.executed = []
        self.failure = None
        self.interrupt = False

    async def find_completed(self, cursor):
        return self.completed.get(cursor.idempotency_key)

    async def execute(self, cursor):
        assert self.checkpoints.commits[-1] == "claim"
        self.executed.append(cursor.current_stage)
        if self.failure:
            raise self.failure
        result = WebStageEffectResult(artifact_ids=(ARTIFACT_ID,))
        self.completed[cursor.idempotency_key] = result
        if self.interrupt:
            raise asyncio.CancelledError
        return result


def setup(stage, *, running=False):
    checkpoints = Checkpoints(stage, running=running)
    effects = Effects(checkpoints)
    return (
        checkpoints,
        effects,
        WebRunStageOrchestrator(checkpoints=checkpoints, effects=effects, worker_id="worker-6"),
    )


async def test_classification_completed_capture_mismatch_becomes_explicit_gap():
    checkpoints = Checkpoints(WebRunStage.CLASSIFICATION)

    class MismatchedEffects(Effects):
        async def find_completed(self, _cursor):
            raise WebStageFailure("WEB_CLASSIFICATION_CAPTURE_MISMATCH")

        async def execute(self, _cursor):
            raise AssertionError("错配完成标记不能进入分类执行")

    result = await WebRunStageOrchestrator(
        checkpoints=checkpoints,
        effects=MismatchedEffects(checkpoints),
        worker_id="worker-6",
    ).advance_once(RUN_ID, "page:1")

    assert checkpoints.commits == ["claim", "gap"]
    assert result.cursor.gaps[-1].error_code == "WEB_CLASSIFICATION_CAPTURE_MISMATCH"


async def test_resume_after_full_page_reuses_completed_surface_phase():
    checkpoints, effects, orchestrator = setup(WebRunStage.FULL_PAGE_CAPTURE, running=True)
    effects.completed[checkpoints.cursor.idempotency_key] = WebStageEffectResult(
        artifact_ids=(ARTIFACT_ID,)
    )
    result = await orchestrator.advance_once(RUN_ID, "page:1")
    assert result.next_stage == "classification"
    assert result.reused is True
    assert result.cursor.artifact_ids == (ARTIFACT_ID,)
    assert effects.executed == []
    assert checkpoints.commits == ["claim", "success"]


async def test_full_page_timeout_exhaustion_continues_classification_next_call():
    checkpoints, effects, orchestrator = setup(WebRunStage.FULL_PAGE_CAPTURE)
    effects.failure = TimeoutError()
    first = await orchestrator.advance_once(RUN_ID, "page:1")
    assert first.cursor.status == WebStageStatus.RETRY_WAIT
    checkpoints.now += timedelta(seconds=2)
    second = await orchestrator.advance_once(RUN_ID, "page:1")
    assert second.next_stage == "classification"
    assert second.cursor.last_error_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert effects.executed == [WebRunStage.FULL_PAGE_CAPTURE] * 2
    effects.failure = None
    third = await orchestrator.advance_once(RUN_ID, "page:1")
    assert third.cursor.current_stage == WebRunStage.CLASSIFICATION
    assert third.next_stage == "href_discovery"


async def test_navigation_exhaustion_advances_to_frontier_without_aborting_run():
    checkpoints, effects, orchestrator = setup(WebRunStage.CANDIDATE_NAVIGATION)
    effects.failure = ConnectionError()
    for _ in range(3):
        result = await orchestrator.advance_once(RUN_ID, "page:1")
        checkpoints.now += timedelta(seconds=4)
    assert result.next_stage == "frontier_drain"
    assert result.cursor.gap_codes == ("WEB_NAVIGATION_RETRY_EXHAUSTED",)
    effects.failure = None
    result = await orchestrator.advance_once(RUN_ID, "page:1")
    assert result.cursor.current_stage == WebRunStage.FRONTIER_DRAIN
    assert result.cursor.status == WebStageStatus.SUCCEEDED


async def test_expired_claim_budget_gap_does_not_authorize_side_effect():
    checkpoints, effects, orchestrator = setup(WebRunStage.CLASSIFICATION, running=True)
    result = await orchestrator.advance_once(RUN_ID, "page:1")
    assert result.cursor.status == WebStageStatus.GAPPED
    assert effects.executed == []
    assert checkpoints.commits == ["claim"]


async def test_worker_interruption_preserves_effect_for_next_worker():
    checkpoints, effects, orchestrator = setup(WebRunStage.FULL_PAGE_CAPTURE)
    effects.interrupt = True
    with pytest.raises(asyncio.CancelledError):
        await orchestrator.advance_once(RUN_ID, "page:1")
    assert checkpoints.cursor.status == WebStageStatus.RUNNING
    checkpoints.now += timedelta(seconds=61)
    result = await orchestrator.advance_once(RUN_ID, "page:1")
    assert result.reused
    assert result.next_stage == "classification"
    assert effects.executed == [WebRunStage.FULL_PAGE_CAPTURE]


@pytest.mark.parametrize("stage", [WebRunStage.AD_CLOSEOUT, WebRunStage.SESSION_CLOSE])
async def test_terminal_close_checkpoint_never_reexecutes(stage):
    checkpoints, effects, orchestrator = setup(stage)
    await orchestrator.advance_once(RUN_ID, "page:1")
    result = await orchestrator.advance_once(RUN_ID, "page:1")
    assert result.cursor.is_terminal
    assert effects.executed == [stage]


async def test_cas_conflict_is_not_reclassified_as_retryable_effect_failure():
    checkpoints, effects, orchestrator = setup(WebRunStage.FULL_PAGE_CAPTURE)
    original = effects.execute

    async def lose_lease(cursor):
        result = await original(cursor)
        checkpoints.now += timedelta(seconds=61)
        return result

    effects.execute = lose_lease
    with pytest.raises(WebStageCheckpointConflict):
        await orchestrator.advance_once(RUN_ID, "page:1")
    assert checkpoints.commits == ["claim"]


async def test_obstruction_retry_budget_is_per_fingerprint():
    checkpoints, effects, orchestrator = setup(WebRunStage.OBSTRUCTION_CLEAR)
    effects.failure = WebStageFailure(
        "WEB_OBSTRUCTION_STATE_DRIFT",
        transient=True,
        detail={"obstruction_fingerprint": "popup:one"},
    )
    await orchestrator.advance_once(RUN_ID, "page:1")
    checkpoints.now += timedelta(seconds=2)
    result = await orchestrator.advance_once(RUN_ID, "page:1")
    assert result.cursor.gap_codes == ("WEB_OBSTRUCTION_UNRESOLVED",)
    assert result.next_stage == "readiness"


async def test_sealed_or_cancelled_run_stops_before_effect():
    checkpoints, effects, orchestrator = setup(WebRunStage.FULL_PAGE_CAPTURE)
    checkpoints.active = False
    with pytest.raises(WebStageCheckpointConflict):
        await orchestrator.advance_once(RUN_ID, "page:1")
    assert effects.executed == []


async def test_stage_runner_prioritizes_unfinished_child_before_parent_closeout():
    from services.api.app.application.web_runtime_stage_runner import WebRuntimeStageRunner

    checkpoints, effects, _ = setup(WebRunStage.SESSION_CLOSE)
    parent = checkpoints.cursor
    child = replace(
        parent,
        logical_page_key="child",
        current_stage=WebRunStage.CLASSIFICATION,
        created_at=parent.created_at + timedelta(seconds=1),
    )

    async def list_for_run(*, run_id):
        assert run_id == RUN_ID
        return (parent, child)

    checkpoints.list_for_run = list_for_run
    runner = WebRuntimeStageRunner(checkpoints=checkpoints, effects=effects, worker_id="worker")
    assert (await runner.next_due(RUN_ID)).logical_page_key == "child"
