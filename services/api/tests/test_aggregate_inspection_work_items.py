"""EvidenceInputSet 聚合质检工作项应用合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.aggregate_inspection_work_items import (
    AggregateInspectionAttemptStatus,
    AggregateInspectionWorkItemConflictError,
    AggregateInspectionWorkItemFinalization,
    AggregateInspectionWorkItemLease,
    AggregateInspectionWorkItemService,
    AggregateInspectionWorkItemStatus,
    AggregateInspectionWorkItemValidationError,
    ClaimAggregateInspectionWorkItemCommand,
    PublishAggregateInspectionWorkItemCommand,
    PublishedAggregateInspectionWorkItem,
    RenewAggregateInspectionWorkItemLeaseCommand,
    StartAggregateInspectionAttemptCommand,
    TransitionAggregateInspectionAttemptCommand,
    TransitionAggregateInspectionWorkItemCommand,
)
from services.api.app.domain.evidence import EvidenceInputScope
from services.api.app.domain.protocol import stable_protocol_hash

NOW = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)


def _publish_command(
    *,
    scope: EvidenceInputScope = EvidenceInputScope.TASK,
) -> PublishAggregateInspectionWorkItemCommand:
    return PublishAggregateInspectionWorkItemCommand(
        task_id="task-1",
        evidence_input_set_id="evidence-input-set:" + "a" * 64,
        producer_agent_run_id="run-1",
        rule_id="rule-1",
        rule_code="alioth_spec_48",
        rule_contract_hash="contract-hash",
        scope=scope,
        scope_key="toast_style",
        finalization_epoch=3,
    )


class FakeRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.transition_result: int | None = 2
        self.attempt_transition_result = True

    async def publish(self, *, command):
        self.calls.append(("publish", command))
        return PublishedAggregateInspectionWorkItem(
            work_item_id="work-1",
            input_hash="a" * 64,
            status=AggregateInspectionWorkItemStatus.PENDING,
            created=True,
        )

    async def claim(self, *, command, lease_token):
        self.calls.append(("claim", (command, lease_token)))
        return AggregateInspectionWorkItemLease(
            work_item_id="work-1",
            task_id=command.task_id,
            evidence_input_set_id="evidence-input-set:" + "a" * 64,
            input_hash="a" * 64,
            rule_id="rule-1",
            rule_code="alioth_spec_48",
            rule_contract_hash="contract-hash",
            scope=EvidenceInputScope.TASK,
            scope_key="toast_style",
            finalization_epoch=3,
            lease_token=lease_token,
            attempt_no=1,
            row_version=1,
        )

    async def transition(self, *, command, result_hash):
        self.calls.append(("transition", (command, result_hash)))
        return self.transition_result

    async def renew_lease(self, *, command):
        self.calls.append(("renew_lease", command))
        return self.transition_result

    async def start_attempt(self, *, command):
        self.calls.append(("start_attempt", command))
        return "attempt-1"

    async def transition_attempt(self, *, command, result_hash):
        self.calls.append(("transition_attempt", (command, result_hash)))
        return self.attempt_transition_result

    async def has_nonterminal_work(self, *, task_id):
        self.calls.append(("has_nonterminal_work", task_id))
        return True

    async def finalize_task_work(self, **kwargs):
        self.calls.append(("finalize_task_work", kwargs))
        return AggregateInspectionWorkItemFinalization(
            task_id=kwargs["task_id"],
            recovered_work_item_count=1,
            failed_work_item_count=2,
            abandoned_attempt_count=3,
        )


async def test_publish_rejects_page_scope_and_keeps_task_scope_separate() -> None:
    repository = FakeRepository()
    service = AggregateInspectionWorkItemService(repository=repository)

    published = await service.publish(_publish_command())

    assert published.created is True
    with pytest.raises(AggregateInspectionWorkItemValidationError, match="page scope"):
        await service.publish(_publish_command(scope=EvidenceInputScope.PAGE))


async def test_claim_generates_fencing_token_and_returns_input_set_lease() -> None:
    repository = FakeRepository()
    service = AggregateInspectionWorkItemService(repository=repository)

    lease = await service.claim(
        ClaimAggregateInspectionWorkItemCommand(
            task_id="task-1",
            consumer_agent_run_id="run-consumer",
            lease_owner="worker-1",
            lease_seconds=30,
        )
    )

    assert lease is not None
    assert lease.evidence_input_set_id.startswith("evidence-input-set:")
    assert lease.scope is EvidenceInputScope.TASK
    assert len(lease.lease_token) == 36


async def test_attempt_and_work_terminal_outputs_use_same_stable_result_hash() -> None:
    repository = FakeRepository()
    service = AggregateInspectionWorkItemService(repository=repository)
    payload = {"conclusion": "fail", "style_group_count": 2}

    attempt_id = await service.start_attempt(
        StartAggregateInspectionAttemptCommand(
            work_item_id="work-1",
            consumer_agent_run_id="run-consumer",
            attempt_no=1,
            lease_token="lease-1",
            input_hash="a" * 64,
        )
    )
    await service.transition_attempt(
        TransitionAggregateInspectionAttemptCommand(
            attempt_id=attempt_id,
            expected_status=AggregateInspectionAttemptStatus.RUNNING,
            target_status=AggregateInspectionAttemptStatus.SUCCEEDED,
            lease_token="lease-1",
            result_payload=payload,
        )
    )
    version = await service.transition(
        TransitionAggregateInspectionWorkItemCommand(
            work_item_id="work-1",
            expected_status=AggregateInspectionWorkItemStatus.RUNNING,
            target_status=AggregateInspectionWorkItemStatus.SUCCEEDED,
            row_version=1,
            lease_token="lease-1",
            attempt_id=attempt_id,
            result_payload=payload,
        )
    )

    assert version == 2
    expected_hash = stable_protocol_hash(payload)
    attempt_call = next(item for item in repository.calls if item[0] == "transition_attempt")
    work_call = next(item for item in repository.calls if item[0] == "transition")
    assert attempt_call[1][1] == expected_hash
    assert work_call[1][1] == expected_hash


async def test_renew_lease_uses_consumer_token_and_row_version() -> None:
    repository = FakeRepository()
    service = AggregateInspectionWorkItemService(repository=repository)

    version = await service.renew_lease(
        RenewAggregateInspectionWorkItemLeaseCommand(
            work_item_id="work-1",
            consumer_agent_run_id="run-consumer",
            lease_token="lease-1",
            row_version=1,
            lease_seconds=30,
        )
    )

    assert version == 2
    assert repository.calls[-1][0] == "renew_lease"


async def test_retry_requires_time_and_terminal_result_is_required() -> None:
    service = AggregateInspectionWorkItemService(repository=FakeRepository())

    with pytest.raises(AggregateInspectionWorkItemValidationError, match="attempt_id"):
        await service.transition(
            TransitionAggregateInspectionWorkItemCommand(
                work_item_id="work-1",
                expected_status=AggregateInspectionWorkItemStatus.LEASED,
                target_status=AggregateInspectionWorkItemStatus.RUNNING,
                row_version=1,
                lease_token="lease-1",
            )
        )
    with pytest.raises(AggregateInspectionWorkItemValidationError, match="next_attempt_at"):
        await service.transition(
            TransitionAggregateInspectionWorkItemCommand(
                work_item_id="work-1",
                expected_status=AggregateInspectionWorkItemStatus.RUNNING,
                target_status=AggregateInspectionWorkItemStatus.RETRY_WAIT,
                row_version=1,
                lease_token="lease-1",
                attempt_id="attempt-1",
            )
        )
    with pytest.raises(AggregateInspectionWorkItemValidationError, match="result_payload"):
        await service.transition(
            TransitionAggregateInspectionWorkItemCommand(
                work_item_id="work-1",
                expected_status=AggregateInspectionWorkItemStatus.RUNNING,
                target_status=AggregateInspectionWorkItemStatus.SUCCEEDED,
                row_version=1,
                lease_token="lease-1",
                attempt_id="attempt-1",
            )
        )

    repository = FakeRepository()
    repository.transition_result = None
    service = AggregateInspectionWorkItemService(repository=repository)
    with pytest.raises(AggregateInspectionWorkItemConflictError):
        await service.transition(
            TransitionAggregateInspectionWorkItemCommand(
                work_item_id="work-1",
                expected_status=AggregateInspectionWorkItemStatus.RUNNING,
                target_status=AggregateInspectionWorkItemStatus.RETRY_WAIT,
                row_version=1,
                lease_token="lease-1",
                attempt_id="attempt-1",
                next_attempt_at=NOW + timedelta(seconds=5),
            )
        )


async def test_finalize_task_work_validates_and_delegates() -> None:
    repository = FakeRepository()
    service = AggregateInspectionWorkItemService(repository=repository)

    result = await service.finalize_task_work(
        task_id="task-1",
        error_code="AGGREGATE_FINALIZATION_INCOMPLETE",
        error_message="任务级规则缺少正式结果",
    )

    assert result.recovered_work_item_count == 1
    assert result.failed_work_item_count == 2
    assert result.abandoned_attempt_count == 3
    assert repository.calls[-1][0] == "finalize_task_work"

    with pytest.raises(AggregateInspectionWorkItemValidationError, match="error_code"):
        await service.finalize_task_work(
            task_id="task-1",
            error_code=" ",
            error_message="任务级规则缺少正式结果",
        )
