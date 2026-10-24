"""证据生命周期应用服务：评估、合法留存、恢复与可审计推进。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from services.api.app.domain.evidence.lifecycle import (
    EvidenceArtifactLifecycleView,
    EvidenceLifecycleDecision,
    EvidenceLifecycleEvaluation,
    EvidenceLifecycleEventType,
    EvidenceLifecyclePolicy,
    EvidenceLifecycleStatus,
    evaluate_artifact_lifecycle,
)

logger = logging.getLogger(__name__)


class EvidenceLifecycleError(ValueError):
    """生命周期业务错误。"""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = status_code


class EvidenceLifecycleRepository(Protocol):
    """生命周期仓储契约。"""

    async def has_active_legal_hold(self, *, task_id: str) -> bool: ...

    async def apply_legal_hold(
        self,
        *,
        task_id: str,
        reason: str,
        held_by: str,
        held_at: datetime,
    ) -> str: ...

    async def release_legal_hold(
        self,
        *,
        task_id: str,
        release_reason: str,
        released_at: datetime,
        released_by: str | None = None,
    ) -> int: ...

    async def list_candidates(
        self,
        *,
        limit: int,
    ) -> Sequence[EvidenceArtifactLifecycleView]: ...

    async def fetch_artifact(
        self,
        *,
        artifact_id: str,
    ) -> EvidenceArtifactLifecycleView | None: ...

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
    ) -> bool: ...

    async def record_evaluation(
        self,
        *,
        evaluation: EvidenceLifecycleEvaluation,
        actor: str,
    ) -> None: ...


@dataclass(frozen=True, kw_only=True)
class EvidenceLifecycleSweepResult:
    """一次有界扫描摘要。"""

    evaluated: int
    mutated: int
    blocked_by_hold: int
    blocked_by_policy: int
    not_yet_expired: int


@dataclass(frozen=True, kw_only=True)
class EvidenceRestoreResult:
    """制品恢复结果。"""

    artifact_id: str
    task_id: str
    from_status: EvidenceLifecycleStatus
    to_status: EvidenceLifecycleStatus
    changed: bool


class EvidenceLifecycleService:
    """按策略评估并（在启用时）推进证据生命周期。"""

    def __init__(
        self,
        *,
        repository: EvidenceLifecycleRepository,
        policy: EvidenceLifecyclePolicy,
        actor: str = "evidence-lifecycle",
    ) -> None:
        self._repository = repository
        self._policy = policy
        self._actor = actor

    @property
    def policy(self) -> EvidenceLifecyclePolicy:
        return self._policy

    async def apply_legal_hold(
        self,
        *,
        task_id: str,
        reason: str,
        held_by: str,
        held_at: datetime | None = None,
    ) -> str:
        return await self._repository.apply_legal_hold(
            task_id=task_id,
            reason=reason.strip() or "legal_hold",
            held_by=held_by.strip() or self._actor,
            held_at=held_at or datetime.now(UTC),
        )

    async def release_legal_hold(
        self,
        *,
        task_id: str,
        release_reason: str,
        released_by: str,
        released_at: datetime | None = None,
    ) -> int:
        return await self._repository.release_legal_hold(
            task_id=task_id,
            release_reason=release_reason.strip() or "hold_release",
            released_at=released_at or datetime.now(UTC),
            released_by=released_by.strip() or self._actor,
        )

    async def restore_artifact(
        self,
        *,
        artifact_id: str,
        actor: str,
        reason: str = "manual_restore",
    ) -> EvidenceRestoreResult:
        """将 archived／pending_delete 恢复为 active；deleted 拒绝。"""

        artifact = await self._repository.fetch_artifact(artifact_id=artifact_id)
        if artifact is None:
            raise EvidenceLifecycleError("未找到证据制品", status_code=404)
        current = artifact.lifecycle_status
        if current is EvidenceLifecycleStatus.ACTIVE:
            return EvidenceRestoreResult(
                artifact_id=artifact.artifact_id,
                task_id=artifact.task_id,
                from_status=current,
                to_status=current,
                changed=False,
            )
        if current is EvidenceLifecycleStatus.DELETED:
            raise EvidenceLifecycleError(
                "已删除证据不可恢复（缺少冷备／对象还原能力）",
                status_code=409,
            )
        if current not in {
            EvidenceLifecycleStatus.ARCHIVED,
            EvidenceLifecycleStatus.PENDING_DELETE,
        }:
            raise EvidenceLifecycleError(
                f"当前状态不可恢复：{current.value}",
                status_code=409,
            )
        detail = {
            "reason": reason.strip() or "manual_restore",
            "from_status": current.value,
            "to_status": EvidenceLifecycleStatus.ACTIVE.value,
        }
        changed = await self._repository.transition_status(
            artifact_id=artifact.artifact_id,
            from_status=current,
            to_status=EvidenceLifecycleStatus.ACTIVE,
            actor=actor.strip() or self._actor,
            event_type=EvidenceLifecycleEventType.RESTORE,
            policy_version=self._policy.policy_version,
            detail=detail,
        )
        if not changed:
            raise EvidenceLifecycleError("恢复失败：状态已被并发修改", status_code=409)
        return EvidenceRestoreResult(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            from_status=current,
            to_status=EvidenceLifecycleStatus.ACTIVE,
            changed=True,
        )

    async def evaluate_one(
        self,
        artifact: EvidenceArtifactLifecycleView,
        *,
        now: datetime | None = None,
    ) -> EvidenceLifecycleEvaluation:
        held = await self._repository.has_active_legal_hold(task_id=artifact.task_id)
        evaluation = evaluate_artifact_lifecycle(
            artifact=artifact,
            policy=self._policy,
            legal_hold_active=held,
            now=now,
        )
        await self._repository.record_evaluation(
            evaluation=evaluation,
            actor=self._actor,
        )
        return evaluation

    async def apply_evaluation(
        self,
        evaluation: EvidenceLifecycleEvaluation,
        *,
        current_status: EvidenceLifecycleStatus,
    ) -> bool:
        """仅在决策允许变更且 GC 启用时推进状态。"""

        if not evaluation.would_mutate:
            return False
        target = _target_status(evaluation.decision)
        if target is None:
            return False
        event_type = _event_type(evaluation.decision)
        return await self._repository.transition_status(
            artifact_id=evaluation.artifact_id,
            from_status=current_status,
            to_status=target,
            actor=self._actor,
            event_type=event_type,
            policy_version=evaluation.policy_version,
            detail=evaluation.to_audit_payload(),
        )

    async def sweep(self, *, now: datetime | None = None) -> EvidenceLifecycleSweepResult:
        """有界扫描到期制品；GC 关闭时只写评估审计。"""

        current = now or datetime.now(UTC)
        candidates = await self._repository.list_candidates(limit=self._policy.batch_limit)
        mutated = 0
        blocked_hold = 0
        blocked_policy = 0
        not_expired = 0
        for artifact in candidates:
            evaluation = await self.evaluate_one(artifact, now=current)
            if evaluation.decision is EvidenceLifecycleDecision.BLOCKED_BY_HOLD:
                blocked_hold += 1
            elif evaluation.decision is EvidenceLifecycleDecision.BLOCKED_BY_POLICY:
                blocked_policy += 1
            elif evaluation.decision is EvidenceLifecycleDecision.NOT_YET_EXPIRED:
                not_expired += 1
            elif evaluation.would_mutate:
                if await self.apply_evaluation(
                    evaluation,
                    current_status=artifact.lifecycle_status,
                ):
                    mutated += 1
        return EvidenceLifecycleSweepResult(
            evaluated=len(candidates),
            mutated=mutated,
            blocked_by_hold=blocked_hold,
            blocked_by_policy=blocked_policy,
            not_yet_expired=not_expired,
        )


class EvidenceLifecycleSweeper:
    """周期执行证据生命周期扫描；失败只告警。"""

    def __init__(
        self,
        *,
        service: EvidenceLifecycleService,
        interval_seconds: float = 300.0,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须为正数")
        self._service = service
        self._interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    async def sweep_once(self) -> EvidenceLifecycleSweepResult | None:
        try:
            return await self._service.sweep()
        except Exception as exc:  # noqa: BLE001 — 清扫失败不得终止 Worker
            logger.warning("证据生命周期扫描失败：%s", exc, exc_info=True)
            return None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            result = await self.sweep_once()
            if result is not None and (result.mutated or result.evaluated):
                logger.info(
                    "证据生命周期扫描 evaluated=%s mutated=%s hold=%s policy=%s",
                    result.evaluated,
                    result.mutated,
                    result.blocked_by_hold,
                    result.blocked_by_policy,
                )


def _target_status(
    decision: EvidenceLifecycleDecision,
) -> EvidenceLifecycleStatus | None:
    if decision is EvidenceLifecycleDecision.ARCHIVE:
        return EvidenceLifecycleStatus.ARCHIVED
    if decision is EvidenceLifecycleDecision.MARK_DELETE:
        return EvidenceLifecycleStatus.PENDING_DELETE
    if decision is EvidenceLifecycleDecision.DELETE:
        return EvidenceLifecycleStatus.DELETED
    return None


def _event_type(decision: EvidenceLifecycleDecision) -> EvidenceLifecycleEventType:
    if decision is EvidenceLifecycleDecision.ARCHIVE:
        return EvidenceLifecycleEventType.ARCHIVE
    if decision is EvidenceLifecycleDecision.MARK_DELETE:
        return EvidenceLifecycleEventType.MARK_DELETE
    if decision is EvidenceLifecycleDecision.DELETE:
        return EvidenceLifecycleEventType.DELETE
    return EvidenceLifecycleEventType.EVALUATE
