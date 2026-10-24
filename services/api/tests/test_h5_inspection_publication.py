"""H5 Inspection Publication 聚合与版本化消息合同。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.inspection.h5_publication import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
    H5InspectionPublication,
    H5InspectionPublicationState,
    H5InspectionPublicationValidationError,
    build_h5_inspection_publication_available,
    build_h5_inspection_publication_id,
    finish_h5_inspection_publication,
    lease_h5_inspection_publication,
    retry_h5_inspection_publication,
)


def _publication(**changes: object) -> H5InspectionPublication:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    values: dict[str, object] = {
        "publication_id": "11111111-1111-5111-8111-111111111111",
        "task_id": "22222222-2222-4222-8222-222222222222",
        "source_agent_run_id": "33333333-3333-4333-8333-333333333333",
        "page_capture_id": "44444444-4444-4444-8444-444444444444",
        "page_type_assessment_id": "assessment-1",
        "assessment_hash": "a" * 64,
        "ruleset_id": "55555555-5555-4555-8555-555555555555",
        "ruleset_version": 1,
        "ruleset_snapshot_hash": "b" * 64,
        "finalization_epoch": 0,
        "input_hash": "c" * 64,
        "status": H5InspectionPublicationState.PENDING,
        "attempt_count": 0,
        "max_attempts": 3,
        "next_attempt_at": now,
        "row_version": 1,
        "created_at": now,
        "updated_at": now,
    }
    values.update(changes)
    return H5InspectionPublication(**values)


def test_publication_identity_is_stable_and_epoch_scoped() -> None:
    first = build_h5_inspection_publication_id(
        task_id="task-1",
        finalization_epoch=2,
        page_capture_id="capture-1",
        assessment_id="assessment-1",
        ruleset_snapshot_hash="ruleset-hash",
    )
    same = build_h5_inspection_publication_id(
        task_id="task-1",
        finalization_epoch=2,
        page_capture_id="capture-1",
        assessment_id="assessment-1",
        ruleset_snapshot_hash="ruleset-hash",
    )
    changed = build_h5_inspection_publication_id(
        task_id="task-1",
        finalization_epoch=3,
        page_capture_id="capture-1",
        assessment_id="assessment-1",
        ruleset_snapshot_hash="ruleset-hash",
    )

    assert first == same
    assert first != changed


def test_lease_requires_expected_message_version_and_sets_fencing_token() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    leased = lease_h5_inspection_publication(
        _publication(),
        consumer_id="worker-1",
        expected_version=1,
        lease_token="66666666-6666-4666-8666-666666666666",
        lease_expires_at=now + timedelta(seconds=60),
        now=now,
    )

    assert leased.status is H5InspectionPublicationState.LEASED
    assert leased.lease_owner == "worker-1"
    assert leased.attempt_count == 1

    with pytest.raises(H5InspectionPublicationValidationError, match="版本"):
        lease_h5_inspection_publication(
            _publication(),
            consumer_id="worker-1",
            expected_version=2,
            lease_token="66666666-6666-4666-8666-666666666666",
            lease_expires_at=now + timedelta(seconds=60),
            now=now,
        )


def test_retry_creates_new_message_version_and_clears_lease() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    leased = lease_h5_inspection_publication(
        _publication(),
        consumer_id="worker-1",
        expected_version=1,
        lease_token="66666666-6666-4666-8666-666666666666",
        lease_expires_at=now + timedelta(seconds=60),
        now=now,
    )

    retried = retry_h5_inspection_publication(
        leased,
        lease_token=leased.lease_token or "",
        next_attempt_at=now + timedelta(seconds=10),
        error_code="H5_PUBLICATION_PLAN_FAILED",
        error_message="规则计划暂时不可用",
        now=now,
    )

    assert retried.status is H5InspectionPublicationState.RETRY_WAIT
    assert retried.row_version == 2
    assert retried.lease_token is None
    event = build_h5_inspection_publication_available(retried, occurred_at=now)
    assert event["message_type"] == H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
    assert event["aggregate_version"] == 2
    assert event["payload"] == {
        "protocol_version": "h5-inspection-publication.v1",
        "publication_id": retried.publication_id,
        "task_id": retried.task_id,
        "finalization_epoch": retried.finalization_epoch,
        "message_version": 2,
    }


def test_terminal_transition_requires_lease_and_sets_finished_at() -> None:
    now = datetime(2026, 8, 17, tzinfo=UTC)
    leased = lease_h5_inspection_publication(
        _publication(),
        consumer_id="worker-1",
        expected_version=1,
        lease_token="66666666-6666-4666-8666-666666666666",
        lease_expires_at=now + timedelta(seconds=60),
        now=now,
    )

    finished = finish_h5_inspection_publication(
        leased,
        lease_token=leased.lease_token or "",
        status=H5InspectionPublicationState.PUBLISHED,
        candidate_rule_count=3,
        expected_batch_count=2,
        published_work_item_count=2,
        excluded_active_session_rule_count=0,
        consumer_agent_run_id="77777777-7777-4777-8777-777777777777",
        now=now,
    )

    assert finished.finished_at == now
    assert finished.status is H5InspectionPublicationState.PUBLISHED
    with pytest.raises(H5InspectionPublicationValidationError, match="终态"):
        finish_h5_inspection_publication(
            finished,
            lease_token="old",
            status=H5InspectionPublicationState.FAILED,
            candidate_rule_count=3,
            expected_batch_count=2,
            published_work_item_count=2,
            excluded_active_session_rule_count=0,
            consumer_agent_run_id=None,
            now=now,
        )
