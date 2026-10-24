"""证据生命周期策略与服务测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from services.api.app.application.evidence_lifecycle import EvidenceLifecycleService
from services.api.app.domain.evidence.lifecycle import (
    EvidenceArtifactLifecycleView,
    EvidenceLifecycleDecision,
    EvidenceLifecycleEventType,
    EvidenceLifecyclePolicy,
    EvidenceLifecycleStatus,
    evaluate_artifact_lifecycle,
)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


class MemoryLifecycleRepository:
    def __init__(self) -> None:
        self.holds: set[str] = set()
        self.artifacts: dict[str, EvidenceArtifactLifecycleView] = {}
        self.events: list[dict[str, Any]] = []
        self.evaluations: list[Any] = []

    async def has_active_legal_hold(self, *, task_id: str) -> bool:
        return task_id in self.holds

    async def apply_legal_hold(self, *, task_id: str, reason: str, held_by: str, held_at):
        self.holds.add(task_id)
        self.events.append({"type": "hold_apply", "task_id": task_id, "reason": reason})
        return "hold-1"

    async def release_legal_hold(
        self,
        *,
        task_id: str,
        release_reason: str,
        released_at,
        released_by: str | None = None,
    ):
        self.holds.discard(task_id)
        self.events.append(
            {
                "type": "hold_release",
                "task_id": task_id,
                "reason": release_reason,
                "released_by": released_by,
            }
        )
        return 1

    async def fetch_artifact(self, *, artifact_id: str):
        return self.artifacts.get(artifact_id)

    async def list_candidates(self, *, limit: int):
        items = [
            item
            for item in self.artifacts.values()
            if item.lifecycle_status is not EvidenceLifecycleStatus.DELETED
        ]
        return tuple(sorted(items, key=lambda item: item.created_at)[:limit])

    async def transition_status(
        self,
        *,
        artifact_id: str,
        from_status: EvidenceLifecycleStatus,
        to_status: EvidenceLifecycleStatus,
        actor: str,
        event_type: EvidenceLifecycleEventType,
        policy_version: str,
        detail: dict,
    ) -> bool:
        current = self.artifacts.get(artifact_id)
        if current is None or current.lifecycle_status is not from_status:
            return False
        self.artifacts[artifact_id] = EvidenceArtifactLifecycleView(
            artifact_id=current.artifact_id,
            task_id=current.task_id,
            created_at=current.created_at,
            lifecycle_status=to_status,
            storage_path=current.storage_path,
        )
        self.events.append(
            {
                "type": event_type.value,
                "artifact_id": artifact_id,
                "to": to_status.value,
                "actor": actor,
                "policy_version": policy_version,
                "detail": detail,
            }
        )
        return True

    async def record_evaluation(self, *, evaluation, actor: str) -> None:
        self.evaluations.append((evaluation, actor))


def _artifact(
    *,
    created_at: datetime,
    status: EvidenceLifecycleStatus = EvidenceLifecycleStatus.ACTIVE,
    artifact_id: str = "artifact-1",
    task_id: str = "task-1",
) -> EvidenceArtifactLifecycleView:
    return EvidenceArtifactLifecycleView(
        artifact_id=artifact_id,
        task_id=task_id,
        created_at=created_at,
        lifecycle_status=status,
        storage_path="task-1/cap-1/artifact-1.png",
    )


def test_legal_hold_blocks_gc_even_when_enabled() -> None:
    evaluation = evaluate_artifact_lifecycle(
        artifact=_artifact(created_at=NOW - timedelta(days=120)),
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=True),
        legal_hold_active=True,
        now=NOW,
    )

    assert evaluation.decision is EvidenceLifecycleDecision.BLOCKED_BY_HOLD
    assert evaluation.would_mutate is False


def test_expired_artifact_only_evaluates_when_gc_disabled() -> None:
    evaluation = evaluate_artifact_lifecycle(
        artifact=_artifact(created_at=NOW - timedelta(days=120)),
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=False),
        legal_hold_active=False,
        now=NOW,
    )

    assert evaluation.decision is EvidenceLifecycleDecision.BLOCKED_BY_POLICY
    assert evaluation.would_mutate is False


def test_enabled_gc_archives_then_marks_delete() -> None:
    active = evaluate_artifact_lifecycle(
        artifact=_artifact(created_at=NOW - timedelta(days=120)),
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=True),
        legal_hold_active=False,
        now=NOW,
    )
    archived = evaluate_artifact_lifecycle(
        artifact=_artifact(
            created_at=NOW - timedelta(days=120),
            status=EvidenceLifecycleStatus.ARCHIVED,
        ),
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=True),
        legal_hold_active=False,
        now=NOW,
    )

    assert active.decision is EvidenceLifecycleDecision.ARCHIVE
    assert archived.decision is EvidenceLifecycleDecision.MARK_DELETE


@pytest.mark.asyncio
async def test_sweep_mutates_only_when_gc_enabled() -> None:
    repo = MemoryLifecycleRepository()
    repo.artifacts["artifact-1"] = _artifact(created_at=NOW - timedelta(days=120))
    disabled = EvidenceLifecycleService(
        repository=repo,
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=False),
    )
    dry = await disabled.sweep(now=NOW)
    assert dry.mutated == 0
    assert dry.blocked_by_policy == 1
    assert repo.artifacts["artifact-1"].lifecycle_status is EvidenceLifecycleStatus.ACTIVE

    enabled = EvidenceLifecycleService(
        repository=repo,
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=True),
    )
    wet = await enabled.sweep(now=NOW)
    assert wet.mutated == 1
    assert repo.artifacts["artifact-1"].lifecycle_status is EvidenceLifecycleStatus.ARCHIVED
    assert any(event["type"] == "archive" for event in repo.events)


@pytest.mark.asyncio
async def test_sweep_skips_held_tasks() -> None:
    repo = MemoryLifecycleRepository()
    repo.holds.add("task-1")
    repo.artifacts["artifact-1"] = _artifact(created_at=NOW - timedelta(days=200))
    service = EvidenceLifecycleService(
        repository=repo,
        policy=EvidenceLifecyclePolicy(retention_days=30, gc_enabled=True),
    )

    result = await service.sweep(now=NOW)

    assert result.blocked_by_hold == 1
    assert result.mutated == 0


@pytest.mark.asyncio
async def test_restore_archived_artifact_to_active() -> None:
    from services.api.app.application.evidence_lifecycle import EvidenceLifecycleError

    repo = MemoryLifecycleRepository()
    repo.artifacts["artifact-1"] = _artifact(
        created_at=NOW - timedelta(days=100),
        status=EvidenceLifecycleStatus.ARCHIVED,
    )
    service = EvidenceLifecycleService(
        repository=repo,
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=True),
    )
    result = await service.restore_artifact(
        artifact_id="artifact-1",
        actor="ops-user",
        reason="audit_restore",
    )
    assert result.changed is True
    assert result.to_status is EvidenceLifecycleStatus.ACTIVE
    assert repo.artifacts["artifact-1"].lifecycle_status is EvidenceLifecycleStatus.ACTIVE
    assert any(event["type"] == "restore" for event in repo.events)

    with pytest.raises(EvidenceLifecycleError):
        await service.restore_artifact(
            artifact_id="missing",
            actor="ops-user",
        )

    repo.artifacts["artifact-2"] = _artifact(
        artifact_id="artifact-2",
        created_at=NOW,
        status=EvidenceLifecycleStatus.DELETED,
    )
    with pytest.raises(EvidenceLifecycleError):
        await service.restore_artifact(
            artifact_id="artifact-2",
            actor="ops-user",
        )


@pytest.mark.asyncio
async def test_release_legal_hold_records_actor() -> None:
    repo = MemoryLifecycleRepository()
    repo.holds.add("task-1")
    service = EvidenceLifecycleService(
        repository=repo,
        policy=EvidenceLifecyclePolicy(retention_days=90, gc_enabled=True),
    )
    released = await service.release_legal_hold(
        task_id="task-1",
        release_reason="case_closed",
        released_by="auditor-1",
    )
    assert released == 1
    assert "task-1" not in repo.holds
    assert any(
        event.get("released_by") == "auditor-1" for event in repo.events
    )
