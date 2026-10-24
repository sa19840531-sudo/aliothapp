"""H5/App 主证据投递运维合同测试；范围明确不含补采。"""

from datetime import UTC, datetime

import pytest

from services.api.app.application.delivery_operations import (
    DeliveryIncident,
    DeliveryOperationAction,
    DeliveryOperationCommand,
    DeliveryOperationResult,
    DeliveryOperationService,
    DeliveryPlatform,
)
from services.api.app.infrastructure.db.delivery_operation_repository import (
    _incident_from_row,
)


def _incident(
    *,
    platform: DeliveryPlatform = DeliveryPlatform.H5,
) -> DeliveryIncident:
    return DeliveryIncident(
        platform=platform,
        publication_id="1f098f61-a5d1-46c4-b2ec-d64767d3f011",
        task_id="9b98ef01-4d6e-4328-b8c8-4d060bc0dcc5",
        status="pending",
        row_version=3,
        finalization_epoch=2,
        ruleset_id="58cf94bc-aa44-4286-a560-417ddfcfa6ac",
        ruleset_version=4,
        ruleset_snapshot_hash="sha256:rules",
        source_identity={"page_capture_id": "capture-1"},
        outbox_pending_count=0,
        outbox_dead_letter_count=1,
        inbox_pending_count=0,
        inbox_dead_letter_count=1,
        linked_work_item_count=2,
        linked_work_active_count=1,
        linked_work_failed_count=0,
        failure_code="DELIVERY_DEAD_LETTER",
        failure_message="投递重试耗尽",
        allowed_actions=(
            DeliveryOperationAction.RECONCILE,
            DeliveryOperationAction.REQUEUE,
            DeliveryOperationAction.MARK_NEEDS_REVIEW,
        ),
        updated_at=datetime(2026, 8, 18, tzinfo=UTC),
    )


class FakeDeliveryOperationRepository:
    def __init__(self) -> None:
        self.incident = _incident()
        self.list_calls: list[tuple[str, int]] = []
        self.commands: list[DeliveryOperationCommand] = []

    async def list_incidents(
        self,
        *,
        task_id: str,
        limit: int,
    ) -> list[DeliveryIncident]:
        self.list_calls.append((task_id, limit))
        return [self.incident]

    async def execute(
        self,
        command: DeliveryOperationCommand,
    ) -> DeliveryOperationResult:
        self.commands.append(command)
        return DeliveryOperationResult(
            operation_id="f67ac6b5-75ec-46dc-b5e5-ce1650910c5a",
            action=command.action,
            platform=command.platform,
            publication_id=command.publication_id,
            task_id=self.incident.task_id,
            result="applied",
            affected_counts={"outbox_requeued": 1, "inbox_requeued": 1},
            incident=self.incident,
        )


async def test_service_lists_task_scoped_h5_app_incidents() -> None:
    repository = FakeDeliveryOperationRepository()
    service = DeliveryOperationService(repository)

    incidents = await service.list_incidents(task_id=" task-1 ", limit=50)

    assert incidents == [repository.incident]
    assert repository.list_calls == [("task-1", 50)]


async def test_service_builds_version_fenced_operator_command() -> None:
    repository = FakeDeliveryOperationRepository()
    service = DeliveryOperationService(repository)

    result = await service.execute(
        platform=DeliveryPlatform.APP,
        publication_id=" 1f098f61-a5d1-46c4-b2ec-d64767d3f011 ",
        action=DeliveryOperationAction.REQUEUE,
        expected_row_version=3,
        operator_user_id=" 2e012178-eb32-445a-9958-18e44501c04d ",
        reason=" Redis 已恢复，重放主证据投递 ",
    )

    command = repository.commands[0]
    assert command.platform is DeliveryPlatform.APP
    assert command.publication_id == "1f098f61-a5d1-46c4-b2ec-d64767d3f011"
    assert command.operator_user_id == "2e012178-eb32-445a-9958-18e44501c04d"
    assert command.reason == "Redis 已恢复，重放主证据投递"
    assert command.expected_row_version == 3
    assert result.result == "applied"


@pytest.mark.parametrize("limit", [0, 201])
async def test_service_rejects_unbounded_incident_queries(limit: int) -> None:
    service = DeliveryOperationService(FakeDeliveryOperationRepository())

    with pytest.raises(ValueError, match="limit 必须在 1 到 200 之间"):
        await service.list_incidents(task_id="task-1", limit=limit)


def test_command_requires_reason_and_positive_row_version() -> None:
    with pytest.raises(ValueError, match="expected_row_version"):
        DeliveryOperationCommand(
            platform=DeliveryPlatform.H5,
            publication_id="publication-1",
            action=DeliveryOperationAction.RECONCILE,
            expected_row_version=0,
            operator_user_id="operator-1",
            reason="恢复",
        )
    with pytest.raises(ValueError, match="reason"):
        DeliveryOperationCommand(
            platform=DeliveryPlatform.H5,
            publication_id="publication-1",
            action=DeliveryOperationAction.RECONCILE,
            expected_row_version=1,
            operator_user_id="operator-1",
            reason=" ",
        )


def test_published_publication_keeps_downstream_work_recovery_actions() -> None:
    incident = _incident_from_row(
        {
            "publication_id": "1f098f61-a5d1-46c4-b2ec-d64767d3f011",
            "task_id": "9b98ef01-4d6e-4328-b8c8-4d060bc0dcc5",
            "status": "published",
            "row_version": 7,
            "finalization_epoch": 2,
            "ruleset_id": "58cf94bc-aa44-4286-a560-417ddfcfa6ac",
            "ruleset_version": 4,
            "ruleset_snapshot_hash": "sha256:rules",
            "source_identity": {"page_capture_id": "capture-1"},
            "outbox_pending_count": 0,
            "outbox_dead_letter_count": 1,
            "inbox_pending_count": 0,
            "inbox_dead_letter_count": 1,
            "linked_work_item_count": 2,
            "linked_work_active_count": 1,
            "linked_work_failed_count": 0,
            "failure_code": None,
            "failure_message": None,
            "operations": [],
            "updated_at": datetime(2026, 8, 18, tzinfo=UTC),
        },
        DeliveryPlatform.H5,
    )

    assert incident.allowed_actions == (
        DeliveryOperationAction.RECONCILE,
        DeliveryOperationAction.REQUEUE,
        DeliveryOperationAction.MARK_NEEDS_REVIEW,
    )
