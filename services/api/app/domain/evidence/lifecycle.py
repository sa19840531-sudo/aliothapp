"""证据对象生命周期：保留、合法留存、归档与 GC 评估合同。

冻结依据：
- 设计文档「对象存储产物保留周期与清理策略」待办项
- 审计要求：版本化策略、legal hold、删除审计、归档与恢复可测

默认 fail-closed：`gc_enabled=false` 时只评估、不物理删除；
任务处于合法留存时禁止任何删除／归档推进。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

EVIDENCE_LIFECYCLE_POLICY_VERSION = "evidence-lifecycle.v1"


class EvidenceLifecycleStatus(StrEnum):
    """证据制品生命周期状态。"""

    ACTIVE = "active"
    ARCHIVED = "archived"
    PENDING_DELETE = "pending_delete"
    DELETED = "deleted"


class EvidenceLifecycleEventType(StrEnum):
    """生命周期审计事件类型。"""

    EVALUATE = "evaluate"
    ARCHIVE = "archive"
    MARK_DELETE = "mark_delete"
    DELETE = "delete"
    RESTORE = "restore"
    HOLD_APPLY = "hold_apply"
    HOLD_RELEASE = "hold_release"


class EvidenceLifecycleDecision(StrEnum):
    """一次 GC／归档评估结论。"""

    KEEP = "keep"
    ARCHIVE = "archive"
    MARK_DELETE = "mark_delete"
    DELETE = "delete"
    BLOCKED_BY_HOLD = "blocked_by_legal_hold"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    NOT_YET_EXPIRED = "not_yet_expired"


@dataclass(frozen=True, kw_only=True)
class EvidenceLifecyclePolicy:
    """版本化生命周期策略；生产配置必须显式声明。"""

    policy_version: str = EVIDENCE_LIFECYCLE_POLICY_VERSION
    retention_days: int = 90
    gc_enabled: bool = False
    batch_limit: int = 100

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version.strip():
            raise ValueError("policy_version 必须为非空字符串")
        if (
            isinstance(self.retention_days, bool)
            or not isinstance(self.retention_days, int)
            or self.retention_days < 1
        ):
            raise ValueError("retention_days 必须为正整数")
        if not isinstance(self.gc_enabled, bool):
            raise ValueError("gc_enabled 必须为布尔值")
        if (
            isinstance(self.batch_limit, bool)
            or not isinstance(self.batch_limit, int)
            or self.batch_limit < 1
        ):
            raise ValueError("batch_limit 必须为正整数")

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "retention_days": self.retention_days,
            "gc_enabled": self.gc_enabled,
            "batch_limit": self.batch_limit,
        }


@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactLifecycleView:
    """参与生命周期评估的制品投影。"""

    artifact_id: str
    task_id: str
    created_at: datetime
    lifecycle_status: EvidenceLifecycleStatus = EvidenceLifecycleStatus.ACTIVE
    storage_path: str | None = None

    def __post_init__(self) -> None:
        if not self.artifact_id.strip() or not self.task_id.strip():
            raise ValueError("artifact_id 与 task_id 必须非空")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at 必须带时区")


@dataclass(frozen=True, kw_only=True)
class EvidenceLifecycleEvaluation:
    """单制品生命周期评估结果。"""

    artifact_id: str
    task_id: str
    decision: EvidenceLifecycleDecision
    policy_version: str
    expires_at: datetime
    reason: str
    would_mutate: bool

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "decision": self.decision.value,
            "policy_version": self.policy_version,
            "expires_at": self.expires_at.isoformat(),
            "reason": self.reason,
            "would_mutate": self.would_mutate,
        }


def evaluate_artifact_lifecycle(
    *,
    artifact: EvidenceArtifactLifecycleView,
    policy: EvidenceLifecyclePolicy,
    legal_hold_active: bool,
    now: datetime | None = None,
) -> EvidenceLifecycleEvaluation:
    """按策略与合法留存评估制品；默认不因评估本身改写状态。"""

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now 必须带时区")
    expires_at = artifact.created_at + timedelta(days=policy.retention_days)
    if legal_hold_active:
        return EvidenceLifecycleEvaluation(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            decision=EvidenceLifecycleDecision.BLOCKED_BY_HOLD,
            policy_version=policy.policy_version,
            expires_at=expires_at,
            reason="任务处于合法留存，禁止归档或删除",
            would_mutate=False,
        )
    if artifact.lifecycle_status is EvidenceLifecycleStatus.DELETED:
        return EvidenceLifecycleEvaluation(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            decision=EvidenceLifecycleDecision.KEEP,
            policy_version=policy.policy_version,
            expires_at=expires_at,
            reason="制品已删除，无需再次处理",
            would_mutate=False,
        )
    if current < expires_at:
        return EvidenceLifecycleEvaluation(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            decision=EvidenceLifecycleDecision.NOT_YET_EXPIRED,
            policy_version=policy.policy_version,
            expires_at=expires_at,
            reason="未到保留期截止时间",
            would_mutate=False,
        )
    if not policy.gc_enabled:
        return EvidenceLifecycleEvaluation(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            decision=EvidenceLifecycleDecision.BLOCKED_BY_POLICY,
            policy_version=policy.policy_version,
            expires_at=expires_at,
            reason="GC 未启用，仅记录到期评估",
            would_mutate=False,
        )
    if artifact.lifecycle_status is EvidenceLifecycleStatus.ACTIVE:
        return EvidenceLifecycleEvaluation(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            decision=EvidenceLifecycleDecision.ARCHIVE,
            policy_version=policy.policy_version,
            expires_at=expires_at,
            reason="保留期已过，可归档",
            would_mutate=True,
        )
    if artifact.lifecycle_status is EvidenceLifecycleStatus.ARCHIVED:
        return EvidenceLifecycleEvaluation(
            artifact_id=artifact.artifact_id,
            task_id=artifact.task_id,
            decision=EvidenceLifecycleDecision.MARK_DELETE,
            policy_version=policy.policy_version,
            expires_at=expires_at,
            reason="已归档且保留期已过，可标记待删",
            would_mutate=True,
        )
    return EvidenceLifecycleEvaluation(
        artifact_id=artifact.artifact_id,
        task_id=artifact.task_id,
        decision=EvidenceLifecycleDecision.DELETE,
        policy_version=policy.policy_version,
        expires_at=expires_at,
        reason="待删制品可执行物理删除",
        would_mutate=True,
    )
