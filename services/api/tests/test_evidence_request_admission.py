"""Evidence Request 独立受理测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionService,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceNotReadyError,
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import (
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    build_evidence_request,
)


def _request(*, suffix: str):
    return build_evidence_request(
        work_item_id=f"work-{suffix}",
        attempt_id=f"attempt-{suffix}",
        task_id="task-1",
        rule_id=f"rule-{suffix}",
        rule_code=f"rule_{suffix}",
        target_capture_id="capture-1",
        reason_code="TRUSTED_EVIDENCE_INVALID",
        evidence_goal="补采详情页截图",
        required_artifact_types=("screenshot",),
        session_dependency=EvidenceSessionDependency.ACTIVE_SESSION,
        replay_policy=EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
        required_equivalence=EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={"business": {"page_type": "content_detail"}},
        minimum_source_grade=EvidenceSourceGrade.B,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


async def test_admission_persists_requests_without_probe_publication() -> None:
    repository = SimpleNamespace(upsert=AsyncMock())
    service = EvidenceRequestAdmissionService(
        repository=repository,
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=True),
    )
    requests = (_request(suffix="1"), _request(suffix="2"))

    result = await service.admit(requests=requests)

    assert result.admitted_request_ids == tuple(item.request_id for item in requests)
    assert repository.upsert.await_count == 2
    assert repository.upsert.await_args_list[0].kwargs == {"request": requests[0]}
    assert repository.upsert.await_args_list[1].kwargs == {"request": requests[1]}


async def test_admission_rejects_not_ready_policy_before_database() -> None:
    repository = SimpleNamespace(upsert=AsyncMock())
    service = EvidenceRequestAdmissionService(
        repository=repository,
        policy=SupplementalEvidencePolicy(enabled=True, runtime_ready=False),
    )

    with pytest.raises(SupplementalEvidenceNotReadyError) as error:
        await service.admit(requests=(_request(suffix="1"),))

    assert error.value.operation.value == "produce_request"
    repository.upsert.assert_not_awaited()
