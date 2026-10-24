"""H5 页面覆盖权威恢复。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from services.api.app.application.h5_coverage_authority import (
    H5CoverageAuthorityCandidate,
    H5CoverageAuthorityReconciler,
)

pytestmark = pytest.mark.asyncio

_TASK_ID = "22222222-2222-2222-2222-222222222222"
_RUN_ID = "33333333-3333-3333-3333-333333333333"


def _candidate(
    *, persisted_adapter_result: dict[str, object] | None = None
) -> H5CoverageAuthorityCandidate:
    return H5CoverageAuthorityCandidate(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5-session-1",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        location="https://example.test/video/1",
        page_type="video_detail",
        page_state="stable",
        detection_confidence=0.92,
        detection_result={
            "page_capture_id": "44444444-4444-4444-4444-444444444444",
            "page_url": "https://example.test/video/1",
            "page_type": "video_detail",
            "page_state": "stable",
            "detection_status": "recognized",
            "confidence": 0.92,
            "evidence_artifact_ids": ["artifact-1"],
        },
        persisted_adapter_result=persisted_adapter_result,
    )


class Candidates:
    def __init__(self, records: list[H5CoverageAuthorityCandidate]) -> None:
        self.records = records
        self.calls: list[tuple[str, str, int]] = []

    async def list_candidates(
        self, *, task_id: str, agent_run_id: str, limit: int
    ) -> list[H5CoverageAuthorityCandidate]:
        self.calls.append((task_id, agent_run_id, limit))
        return self.records


class Assessments:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def upsert(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return object()


class Snapshots:
    def __init__(self, *, promoted: bool = True) -> None:
        self.promoted = promoted
        self.calls: list[tuple[H5CoverageAuthorityCandidate, dict[str, object]]] = []

    async def promote(
        self,
        *,
        candidate: H5CoverageAuthorityCandidate,
        assessment: dict[str, object],
    ) -> bool:
        self.calls.append((candidate, assessment))
        return self.promoted


async def test_reconciler_builds_missing_assessment_and_promotes_capture() -> None:
    candidates = Candidates([_candidate()])
    assessments = Assessments()
    snapshots = Snapshots()
    reconciler = H5CoverageAuthorityReconciler(
        candidates=candidates,
        assessments=assessments,
        snapshots=snapshots,
        now=lambda: datetime(2026, 9, 14, tzinfo=UTC),
    )

    promoted = await reconciler.reconcile_available(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
    )

    assert promoted == 1
    assert candidates.calls == [(_TASK_ID, _RUN_ID, 50)]
    assert len(assessments.calls) == 1
    assert assessments.calls[0]["page_capture_id"] == (
        "44444444-4444-4444-4444-444444444444"
    )
    assert len(snapshots.calls) == 1
    assert snapshots.calls[0][1]["execution_status"] == "accepted"


async def test_reconciler_reuses_persisted_assessment_without_rewriting() -> None:
    persisted = {
        "execution_status": "accepted",
        "page_type_assessment": {
            "page_type": "video_detail",
            "status": "covered",
        },
    }
    assessments = Assessments()
    snapshots = Snapshots()
    reconciler = H5CoverageAuthorityReconciler(
        candidates=Candidates([_candidate(persisted_adapter_result=persisted)]),
        assessments=assessments,
        snapshots=snapshots,
    )

    promoted = await reconciler.reconcile_available(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        limit=1,
    )

    assert promoted == 1
    assert assessments.calls == []
    assert snapshots.calls[0][1] == persisted


async def test_reconciler_does_not_promote_blocked_assessment() -> None:
    blocked = {
        "execution_status": "blocked",
        "error_code": "PAGE_TYPE_ASSESSMENT_EVIDENCE_REQUIRED",
    }
    snapshots = Snapshots()
    reconciler = H5CoverageAuthorityReconciler(
        candidates=Candidates([_candidate(persisted_adapter_result=blocked)]),
        assessments=Assessments(),
        snapshots=snapshots,
    )

    promoted = await reconciler.reconcile_available(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
    )

    assert promoted == 0
    assert snapshots.calls == []


async def test_reconciler_rejects_nonpositive_limit() -> None:
    reconciler = H5CoverageAuthorityReconciler(
        candidates=Candidates([]),
        assessments=Assessments(),
        snapshots=Snapshots(),
    )

    with pytest.raises(ValueError, match="limit"):
        await reconciler.reconcile_available(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            limit=0,
        )
