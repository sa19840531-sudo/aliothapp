"""版本化事件身份与 Outbox 冲突语义测试。"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.api.app.domain.protocol import build_protocol_envelope
from services.api.app.infrastructure.db.outbox_writer import (
    OutboxWriteConflictError,
    insert_outbox_event,
)

_NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
_MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260805_0035_versioned_event_identity.py"
)


class _OutboxConnection:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, query: str, *args: Any) -> str | None:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        event = {
            "message_id": args[0],
            "task_id": args[1],
            "aggregate_type": args[2],
            "aggregate_id": args[3],
            "aggregate_version": args[4],
            "message_type": args[5],
            "protocol_version": args[6],
            "payload_protocol_version": args[7],
            "idempotency_key": args[8],
            "envelope_payload": json.loads(args[9]),
            "occurred_at": args[10],
        }
        if any(_shares_unique_identity(existing, event) for existing in self.events):
            return None
        self.events.append(event)
        return str(event["message_id"])

    async def fetchrow(self, query: str, *args: Any) -> dict[str, int]:
        compact = " ".join(query.split())
        self.calls.append((compact, args))
        expected = {
            "message_id": args[0],
            "task_id": args[1],
            "aggregate_type": args[2],
            "aggregate_id": args[3],
            "aggregate_version": args[4],
            "message_type": args[5],
            "protocol_version": args[6],
            "payload_protocol_version": args[7],
            "idempotency_key": args[8],
            "envelope_payload": json.loads(args[9]),
        }
        candidates = [
            event for event in self.events if _shares_unique_identity(event, expected)
        ]
        exact = [event for event in candidates if _same_event_without_time(event, expected)]
        return {
            "candidate_count": len(candidates),
            "exact_count": len(exact),
        }


def _envelope(
    *,
    occurred_at: datetime = _NOW,
    payload: dict[str, Any] | None = None,
    idempotency_key: str = "work-1:create",
    aggregate_version: int = 0,
):
    return build_protocol_envelope(
        message_type="alioth.v2.exploration-work.available",
        payload_protocol_version="exploration-work.v1",
        idempotency_key=idempotency_key,
        task_id="task-1",
        aggregate_type="explorer_work",
        aggregate_id="work-1",
        aggregate_version=aggregate_version,
        occurred_at=occurred_at,
        payload=payload or {"work_id": "work-1", "objective": "检查登录入口"},
    )


def _shares_unique_identity(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["message_id"] == right["message_id"]
        or (
            left["message_type"] == right["message_type"]
            and left["idempotency_key"] == right["idempotency_key"]
        )
        or (
            left["message_type"] == right["message_type"]
            and left["aggregate_type"] == right["aggregate_type"]
            and left["aggregate_id"] == right["aggregate_id"]
            and left["aggregate_version"] == right["aggregate_version"]
        )
    )


def _same_event_without_time(left: dict[str, Any], right: dict[str, Any]) -> bool:
    scalar_fields = (
        "message_id",
        "task_id",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
        "message_type",
        "protocol_version",
        "payload_protocol_version",
        "idempotency_key",
    )
    if any(left[field] != right[field] for field in scalar_fields):
        return False
    left_payload = dict(left["envelope_payload"])
    right_payload = dict(right["envelope_payload"])
    left_payload.pop("occurred_at", None)
    right_payload.pop("occurred_at", None)
    return left_payload == right_payload


async def test_outbox_replay_accepts_exact_same_event_with_different_time() -> None:
    connection = _OutboxConnection()

    await insert_outbox_event(
        connection,
        aggregate_type="explorer_work",
        aggregate_id="work-1",
        envelope=_envelope(),
    )
    await insert_outbox_event(
        connection,
        aggregate_type="explorer_work",
        aggregate_id="work-1",
        envelope=_envelope(occurred_at=_NOW + timedelta(minutes=1)),
    )

    assert len(connection.events) == 1
    insert_query = connection.calls[0][0]
    replay_query = connection.calls[2][0]
    assert "aggregate_version" in insert_query
    assert "ON CONFLICT DO NOTHING" in insert_query
    assert "event.idempotency_key = $9" in replay_query
    assert "envelope_payload - 'occurred_at'" in replay_query


async def test_outbox_rejects_same_event_identity_with_changed_payload() -> None:
    connection = _OutboxConnection()
    await insert_outbox_event(
        connection,
        aggregate_type="explorer_work",
        aggregate_id="work-1",
        envelope=_envelope(),
    )

    with pytest.raises(OutboxWriteConflictError, match="发生漂移"):
        await insert_outbox_event(
            connection,
            aggregate_type="explorer_work",
            aggregate_id="work-1",
            envelope=_envelope(
                payload={"work_id": "work-1", "objective": "被篡改的目标"},
            ),
        )

    assert len(connection.events) == 1


async def test_outbox_rejects_same_idempotency_key_with_changed_aggregate_version() -> None:
    """同一幂等键复播必须保持聚合版本等身份不变，否则按漂移拒写。

    真实回归：发布通道 complete 复播工作项可用性公告时若改用行版本生成
    新 message_id，同 input_hash 幂等键即触发本冲突（任务 1422821d 19 连败）。
    """
    connection = _OutboxConnection()
    await insert_outbox_event(
        connection,
        aggregate_type="explorer_work",
        aggregate_id="work-1",
        envelope=_envelope(),
    )

    with pytest.raises(OutboxWriteConflictError, match="发生漂移"):
        await insert_outbox_event(
            connection,
            aggregate_type="explorer_work",
            aggregate_id="work-1",
            envelope=_envelope(aggregate_version=3),
        )

    assert len(connection.events) == 1


async def test_outbox_rejects_column_and_envelope_aggregate_mismatch() -> None:
    connection = _OutboxConnection()

    with pytest.raises(ValueError, match="聚合列"):
        await insert_outbox_event(
            connection,
            aggregate_type="inspection_work_item",
            aggregate_id="work-1",
            envelope=_envelope(),
        )

    assert connection.calls == []


def test_versioned_event_identity_migration_has_database_backstops() -> None:
    migration = _MIGRATION_PATH.read_text(encoding="utf-8")

    assert 'revision = "20260805_0035"' in migration
    assert 'down_revision = "20260805_0034"' in migration
    assert "ADD COLUMN aggregate_version bigint NOT NULL DEFAULT 0" in migration
    assert "ck_qc_outbox_event_v2_identity" in migration
    assert "envelope_payload ?& ARRAY[" in migration
    assert "jsonb_typeof(envelope_payload->'aggregate_version') = 'number'" in migration
    assert "uq_qc_outbox_event_aggregate_version" in migration
    assert "message-envelope.v1" in migration
    assert "message-envelope.v2" in migration
