"""H5 已识别 Capture 的页面评估与覆盖权威恢复。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.application.h5_page_type_assessment_adapter import (
    adapt_h5_page_detection_to_assessment,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class H5CoverageAuthorityCandidate:
    """已完成识别、但尚未进入当前权威覆盖快照的 H5 Capture。"""

    task_id: str
    agent_run_id: str
    session_id: str
    page_capture_id: str
    location: str
    page_type: str
    page_state: str
    detection_confidence: float
    detection_result: Mapping[str, object]
    persisted_adapter_result: Mapping[str, object] | None = None


class H5CoverageAuthorityCandidateReader(Protocol):
    async def list_candidates(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
    ) -> Sequence[H5CoverageAuthorityCandidate]: ...


class H5CoverageAuthorityAssessmentWriter(Protocol):
    async def upsert(self, **kwargs: object) -> object: ...


class H5CoverageAuthoritySnapshotWriter(Protocol):
    async def promote(
        self,
        *,
        candidate: H5CoverageAuthorityCandidate,
        assessment: Mapping[str, object],
    ) -> bool: ...


class H5CoverageAuthorityReconciler:
    """复用 Capture 中的识别事实，补齐 Assessment 并晋升覆盖快照。"""

    def __init__(
        self,
        *,
        candidates: H5CoverageAuthorityCandidateReader,
        assessments: H5CoverageAuthorityAssessmentWriter,
        snapshots: H5CoverageAuthoritySnapshotWriter,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._candidates = candidates
        self._assessments = assessments
        self._snapshots = snapshots
        self._now = now or (lambda: datetime.now(UTC))

    async def reconcile_available(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int = 50,
    ) -> int:
        if limit <= 0:
            raise ValueError("limit 必须大于 0")
        records = await self._candidates.list_candidates(
            task_id=task_id,
            agent_run_id=agent_run_id,
            limit=limit,
        )
        promoted = 0
        for candidate in records:
            adapter_result = candidate.persisted_adapter_result
            if adapter_result is None:
                assessment = adapt_h5_page_detection_to_assessment(
                    task_id=candidate.task_id,
                    agent_run_id=candidate.agent_run_id,
                    detection_result=candidate.detection_result,
                    assessed_at=self._now(),
                )
                adapter_result = assessment.to_dict()
                await self._assessments.upsert(
                    task_id=candidate.task_id,
                    agent_run_id=candidate.agent_run_id,
                    session_id=candidate.session_id,
                    page_capture_id=candidate.page_capture_id,
                    detection_result=candidate.detection_result,
                    assessment_payload=adapter_result,
                )
            if adapter_result.get("execution_status") != "accepted":
                continue
            if await self._snapshots.promote(
                candidate=candidate,
                assessment=adapter_result,
            ):
                promoted += 1
        return promoted


__all__ = [
    "H5CoverageAuthorityCandidate",
    "H5CoverageAuthorityReconciler",
]
