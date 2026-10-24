"""Evidence Exit Gate 的事实快照、离场结论与审计身份。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1 = "evidence-exit-gate-snapshot.v1"
EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V2 = "evidence-exit-gate-snapshot.v2"
EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION = "evidence-exit-gate-snapshot.v3"
EVIDENCE_EXIT_GATE_DECISION_PROTOCOL_VERSION = "evidence-exit-gate-decision.v1"
EVIDENCE_EXIT_BLOCKED_OUTCOME_PROTOCOL_VERSION = "evidence-exit-blocked-outcome.v1"


class EvidenceExitBlockReason(StrEnum):
    """与 HTML 中四项布尔条件一一对应的阻断原因。"""

    BASELINE_EVIDENCE_INCOMPLETE = "baseline_evidence_incomplete"
    ACTIVE_SESSION_REQUIREMENTS_NONTERMINAL = "active_session_requirements_nonterminal"
    INLINE_EVIDENCE_REQUEST_PENDING = "inline_evidence_request_pending"
    EVIDENCE_COMMIT_IN_PROGRESS = "evidence_commit_in_progress"


class EvidenceExitReservationStatus(StrEnum):
    """允许离场后到动作结束之间的短期审计预留。"""

    BLOCKED = "blocked"
    RESERVED = "reserved"
    CONSUMED = "consumed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class EvidenceExitBusinessOutcome(StrEnum):
    """门禁阻断对生产编排暴露的稳定业务分类。"""

    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    EVIDENCE_PENDING = "evidence_pending"
    CONTROL_PLANE_INCOMPLETE = "control_plane_incomplete"
    EVIDENCE_INTEGRITY_FAILED = "evidence_integrity_failed"


@dataclass(frozen=True, kw_only=True)
class EvidenceExitGateSnapshot:
    """一次门禁计算使用的数据库事实投影。"""

    task_id: str
    session_id: str
    source_capture_id: str
    requirement_set_id: str | None
    baseline_pass_manifest_count: int
    nonterminal_active_session_requirement_count: int
    nonterminal_inline_evidence_request_count: int
    evidence_commit_in_progress_count: int
    evaluated_at: datetime
    # 兼容早期无 Manifest 的 App 截图：只有已经出现 Manifest 时，才要求至少一条 PASS。
    baseline_manifest_total_count: int = 0
    # None／web：严格 HTML 四项；android／ios：App 截图探索异步质检放宽
    surface_kind: str | None = None
    latest_manifest_id: str | None = None
    latest_manifest_version: int | None = None
    latest_manifest_hash: str | None = None
    latest_manifest_quality_status: str | None = None
    latest_manifest_quality_reason: str | None = None
    lineage_integrity_failed: bool = False
    protocol_version: str = EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "source_capture_id"):
            _require_text(getattr(self, field_name), field_name)
        if self.requirement_set_id is not None:
            _require_text(self.requirement_set_id, "requirement_set_id")
        if self.surface_kind is not None:
            _require_text(self.surface_kind, "surface_kind")
        for field_name in (
            "baseline_pass_manifest_count",
            "baseline_manifest_total_count",
            "nonterminal_active_session_requirement_count",
            "nonterminal_inline_evidence_request_count",
            "evidence_commit_in_progress_count",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        _require_aware_datetime(self.evaluated_at, "evaluated_at")
        if self.protocol_version not in {
            EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1,
            EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V2,
            EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION,
        }:
            raise ValueError("Evidence Exit Gate Snapshot 版本不受支持")
        if not isinstance(self.lineage_integrity_failed, bool):
            raise ValueError("lineage_integrity_failed 必须为布尔值")
        lineage = (
            self.latest_manifest_id,
            self.latest_manifest_version,
            self.latest_manifest_hash,
            self.latest_manifest_quality_status,
        )
        if any(value is not None for value in lineage):
            if any(value is None for value in lineage):
                raise ValueError("Manifest 身份字段必须同时提供")
            _require_text(self.latest_manifest_id, "latest_manifest_id")
            _require_text(self.latest_manifest_hash, "latest_manifest_hash")
            if (
                isinstance(self.latest_manifest_version, bool)
                or not isinstance(self.latest_manifest_version, int)
                or self.latest_manifest_version <= 0
            ):
                raise ValueError("latest_manifest_version 必须为正整数")
            if self.latest_manifest_quality_status not in {"pending", "pass", "fail"}:
                raise ValueError("latest_manifest_quality_status 不受支持")
            if (
                self.latest_manifest_quality_status == "fail"
                and self.latest_manifest_quality_reason is None
            ):
                raise ValueError("失败 Manifest 必须提供 quality_reason")
        if self.latest_manifest_quality_reason is not None:
            _require_text(
                self.latest_manifest_quality_reason,
                "latest_manifest_quality_reason",
            )
        if (
            self.protocol_version == EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1
            and (any(value is not None for value in lineage) or self.lineage_integrity_failed)
        ):
            raise ValueError("v1 Snapshot 不允许携带 Manifest 血缘")
        if (
            self.protocol_version
            in {
                EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1,
                EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V2,
            }
            and self.baseline_manifest_total_count != 0
        ):
            raise ValueError("历史 Snapshot 不允许携带 Manifest 总数")

    @property
    def _is_app_surface(self) -> bool:
        return (self.surface_kind or "").casefold() in {"android", "ios"}

    @property
    def baseline_evidence_complete(self) -> bool:
        if self.lineage_integrity_failed:
            return False
        if self.baseline_pass_manifest_count > 0:
            return True
        if self.baseline_manifest_total_count > 0:
            return False
        # App：基线 Manifest pass 常因异步质检／规则不适用滞后，不阻塞课纲离场
        return self._is_app_surface

    @property
    def active_session_requirements_terminal(self) -> bool:
        if self.nonterminal_active_session_requirement_count != 0:
            return False
        if self.requirement_set_id is not None:
            return True
        # Web：尚无 requirement_set 视为未收敛；App：无需求快照即可离场
        return self._is_app_surface

    @property
    def no_inline_evidence_request(self) -> bool:
        # App：规则 inline／补采常与课纲探索并行，未结清不得拦后续导航，
        # 否则详情／会员页一触发补证就会 EVIDENCE_CONTROL_PLANE_INCOMPLETE 提前收口。
        if self._is_app_surface:
            return True
        return self.nonterminal_inline_evidence_request_count == 0

    @property
    def no_evidence_commit_in_progress(self) -> bool:
        return self.evidence_commit_in_progress_count == 0

    @property
    def allow_leave(self) -> bool:
        return (
            self.baseline_evidence_complete
            and self.active_session_requirements_terminal
            and self.no_inline_evidence_request
            and self.no_evidence_commit_in_progress
        )

    @property
    def block_reasons(self) -> tuple[EvidenceExitBlockReason, ...]:
        reasons: list[EvidenceExitBlockReason] = []
        if not self.baseline_evidence_complete:
            reasons.append(EvidenceExitBlockReason.BASELINE_EVIDENCE_INCOMPLETE)
        if not self.active_session_requirements_terminal:
            reasons.append(EvidenceExitBlockReason.ACTIVE_SESSION_REQUIREMENTS_NONTERMINAL)
        if not self.no_inline_evidence_request:
            reasons.append(EvidenceExitBlockReason.INLINE_EVIDENCE_REQUEST_PENDING)
        if not self.no_evidence_commit_in_progress:
            reasons.append(EvidenceExitBlockReason.EVIDENCE_COMMIT_IN_PROGRESS)
        return tuple(reasons)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "source_capture_id": self.source_capture_id,
            "requirement_set_id": self.requirement_set_id,
            "baseline_pass_manifest_count": self.baseline_pass_manifest_count,
            "nonterminal_active_session_requirement_count": (
                self.nonterminal_active_session_requirement_count
            ),
            "nonterminal_inline_evidence_request_count": (
                self.nonterminal_inline_evidence_request_count
            ),
            "evidence_commit_in_progress_count": self.evidence_commit_in_progress_count,
            "evaluated_at": self.evaluated_at.isoformat(),
            "surface_kind": self.surface_kind,
        }
        if self.protocol_version != EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1:
            payload.update(
                {
                    "latest_manifest_id": self.latest_manifest_id,
                    "latest_manifest_version": self.latest_manifest_version,
                    "latest_manifest_hash": self.latest_manifest_hash,
                    "latest_manifest_quality_status": self.latest_manifest_quality_status,
                    "latest_manifest_quality_reason": self.latest_manifest_quality_reason,
                    "lineage_integrity_failed": self.lineage_integrity_failed,
                }
            )
        if self.protocol_version == EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION:
            payload["baseline_manifest_total_count"] = self.baseline_manifest_total_count
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceExitGateSnapshot:
        known = {
            "protocol_version",
            "task_id",
            "session_id",
            "source_capture_id",
            "requirement_set_id",
            "baseline_pass_manifest_count",
            "baseline_manifest_total_count",
            "nonterminal_active_session_requirement_count",
            "nonterminal_inline_evidence_request_count",
            "evidence_commit_in_progress_count",
            "evaluated_at",
            "surface_kind",
            "latest_manifest_id",
            "latest_manifest_version",
            "latest_manifest_hash",
            "latest_manifest_quality_status",
            "latest_manifest_quality_reason",
            "lineage_integrity_failed",
        }
        old_fields = known - {
            "baseline_manifest_total_count",
            "latest_manifest_id",
            "latest_manifest_version",
            "latest_manifest_hash",
            "latest_manifest_quality_status",
            "latest_manifest_quality_reason",
            "lineage_integrity_failed",
        }
        required = set(old_fields)
        if payload.get("protocol_version") == EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION:
            required.add("baseline_manifest_total_count")
        values = _read(
            payload,
            known=known,
            required=required,
            field_name="EvidenceExitGateSnapshot",
        )
        try:
            return cls(
                task_id=values["task_id"],
                session_id=values["session_id"],
                source_capture_id=values["source_capture_id"],
                requirement_set_id=values["requirement_set_id"],
                baseline_pass_manifest_count=values["baseline_pass_manifest_count"],
                baseline_manifest_total_count=values.get(
                    "baseline_manifest_total_count",
                    0,
                ),
                nonterminal_active_session_requirement_count=values[
                    "nonterminal_active_session_requirement_count"
                ],
                nonterminal_inline_evidence_request_count=values[
                    "nonterminal_inline_evidence_request_count"
                ],
                evidence_commit_in_progress_count=values["evidence_commit_in_progress_count"],
                evaluated_at=parse_aware_datetime(values["evaluated_at"], "evaluated_at"),
                surface_kind=values.get("surface_kind"),
                latest_manifest_id=values.get("latest_manifest_id"),
                latest_manifest_version=values.get("latest_manifest_version"),
                latest_manifest_hash=values.get("latest_manifest_hash"),
                latest_manifest_quality_status=values.get(
                    "latest_manifest_quality_status"
                ),
                latest_manifest_quality_reason=values.get("latest_manifest_quality_reason"),
                lineage_integrity_failed=values.get("lineage_integrity_failed", False),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


@dataclass(frozen=True, kw_only=True)
class EvidenceExitGateDecision:
    """固定到某条 ActionIntent 的可审计离场结论。"""

    evaluation_id: str
    action_intent_id: str
    snapshot: EvidenceExitGateSnapshot
    allow_leave: bool
    block_reasons: tuple[EvidenceExitBlockReason, ...]
    protocol_version: str = EVIDENCE_EXIT_GATE_DECISION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_text(self.evaluation_id, "evaluation_id")
        _require_text(self.action_intent_id, "action_intent_id")
        if not isinstance(self.snapshot, EvidenceExitGateSnapshot):
            raise ValueError("snapshot 类型不正确")
        if not isinstance(self.allow_leave, bool):
            raise ValueError("allow_leave 必须为布尔值")
        reasons = tuple(self.block_reasons)
        if any(not isinstance(item, EvidenceExitBlockReason) for item in reasons):
            raise ValueError("block_reasons 类型不正确")
        if reasons != self.snapshot.block_reasons:
            raise ValueError("block_reasons 与事实快照不一致")
        if self.allow_leave != self.snapshot.allow_leave:
            raise ValueError("allow_leave 与 HTML 门禁公式不一致")
        if self.allow_leave == bool(reasons):
            raise ValueError("允许离场状态与阻断原因不一致")
        if self.protocol_version != EVIDENCE_EXIT_GATE_DECISION_PROTOCOL_VERSION:
            raise ValueError("Evidence Exit Gate Decision 版本不受支持")
        expected_id = build_evidence_exit_evaluation_id(
            action_intent_id=self.action_intent_id,
            snapshot=self.snapshot,
        )
        if self.evaluation_id != expected_id:
            raise ValueError("evaluation_id 与 ActionIntent 和事实快照不一致")
        object.__setattr__(self, "block_reasons", reasons)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "evaluation_id": self.evaluation_id,
            "action_intent_id": self.action_intent_id,
            "snapshot": self.snapshot.to_dict(),
            "allow_leave": self.allow_leave,
            "block_reasons": [item.value for item in self.block_reasons],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceExitGateDecision:
        known = {
            "protocol_version",
            "evaluation_id",
            "action_intent_id",
            "snapshot",
            "allow_leave",
            "block_reasons",
        }
        values = _read(payload, known=known, field_name="EvidenceExitGateDecision")
        try:
            return cls(
                evaluation_id=values["evaluation_id"],
                action_intent_id=values["action_intent_id"],
                snapshot=EvidenceExitGateSnapshot.from_dict(
                    _mapping(values["snapshot"], "snapshot")
                ),
                allow_leave=values["allow_leave"],
                block_reasons=tuple(
                    EvidenceExitBlockReason(item)
                    for item in _sequence(values["block_reasons"], "block_reasons")
                ),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


def build_evidence_exit_gate_decision(
    *,
    action_intent_id: str,
    snapshot: EvidenceExitGateSnapshot,
) -> EvidenceExitGateDecision:
    """只按 HTML 四项合取式生成结论，不接受 Agent 或调用方覆写。"""
    return EvidenceExitGateDecision(
        evaluation_id=build_evidence_exit_evaluation_id(
            action_intent_id=action_intent_id,
            snapshot=snapshot,
        ),
        action_intent_id=action_intent_id,
        snapshot=snapshot,
        allow_leave=snapshot.allow_leave,
        block_reasons=snapshot.block_reasons,
    )


def build_evidence_exit_evaluation_id(
    *,
    action_intent_id: str,
    snapshot: EvidenceExitGateSnapshot,
) -> str:
    identity = {
            "action_intent_id": _require_text(action_intent_id, "action_intent_id"),
            # evaluated_at 只描述首次落库时间，不参与幂等身份；同一动作面对
            # 相同事实只能生成一条离场预留，事实计数变化后才会生成新评估。
            "task_id": snapshot.task_id,
            "session_id": snapshot.session_id,
            "source_capture_id": snapshot.source_capture_id,
            "requirement_set_id": snapshot.requirement_set_id,
            "baseline_pass_manifest_count": snapshot.baseline_pass_manifest_count,
            "nonterminal_active_session_requirement_count": (
                snapshot.nonterminal_active_session_requirement_count
            ),
            "nonterminal_inline_evidence_request_count": (
                snapshot.nonterminal_inline_evidence_request_count
            ),
            "evidence_commit_in_progress_count": snapshot.evidence_commit_in_progress_count,
            "surface_kind": snapshot.surface_kind,
    }
    if snapshot.protocol_version == EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION:
        identity["baseline_manifest_total_count"] = snapshot.baseline_manifest_total_count
    if snapshot.protocol_version != EVIDENCE_EXIT_GATE_SNAPSHOT_PROTOCOL_VERSION_V1:
        identity.update(
            {
                "latest_manifest_id": snapshot.latest_manifest_id,
                "latest_manifest_version": snapshot.latest_manifest_version,
                "latest_manifest_hash": snapshot.latest_manifest_hash,
                "latest_manifest_quality_status": snapshot.latest_manifest_quality_status,
                "latest_manifest_quality_reason": snapshot.latest_manifest_quality_reason,
                "lineage_integrity_failed": snapshot.lineage_integrity_failed,
            }
        )
    return "exit-gate-evaluation:" + stable_protocol_hash(identity)


@dataclass(frozen=True, kw_only=True)
class EvidenceExitBlockedOutcome:
    """已持久化门禁快照导出的结构化阻断业务结果。"""

    outcome_id: str
    outcome: EvidenceExitBusinessOutcome
    evaluation_id: str
    action_intent_id: str
    task_id: str
    session_id: str
    source_capture_id: str
    requirement_set_id: str | None
    latest_manifest_id: str | None
    latest_manifest_version: int | None
    latest_manifest_hash: str | None
    latest_manifest_quality_status: str | None
    latest_manifest_quality_reason: str | None
    block_reasons: tuple[EvidenceExitBlockReason, ...]
    error_code: str
    retryable: bool
    created_at: datetime
    protocol_version: str = EVIDENCE_EXIT_BLOCKED_OUTCOME_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "outcome_id",
            "evaluation_id",
            "action_intent_id",
            "task_id",
            "session_id",
            "source_capture_id",
            "error_code",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.outcome, EvidenceExitBusinessOutcome):
            raise ValueError("outcome 类型不正确")
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable 必须为布尔值")
        if not self.block_reasons:
            raise ValueError("阻断业务结果必须包含 block_reasons")
        if any(not isinstance(item, EvidenceExitBlockReason) for item in self.block_reasons):
            raise ValueError("block_reasons 类型不正确")
        _require_aware_datetime(self.created_at, "created_at")
        if self.protocol_version != EVIDENCE_EXIT_BLOCKED_OUTCOME_PROTOCOL_VERSION:
            raise ValueError("Evidence Exit Blocked Outcome 版本不受支持")
        expected_id = build_evidence_exit_outcome_id(
            evaluation_id=self.evaluation_id,
            outcome=self.outcome,
            latest_manifest_id=self.latest_manifest_id,
            latest_manifest_version=self.latest_manifest_version,
            latest_manifest_hash=self.latest_manifest_hash,
            requirement_set_id=self.requirement_set_id,
        )
        if self.outcome_id != expected_id:
            raise ValueError("outcome_id 与门禁评估血缘不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "outcome_id": self.outcome_id,
            "outcome": self.outcome.value,
            "evaluation_id": self.evaluation_id,
            "action_intent_id": self.action_intent_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "source_capture_id": self.source_capture_id,
            "requirement_set_id": self.requirement_set_id,
            "latest_manifest_id": self.latest_manifest_id,
            "latest_manifest_version": self.latest_manifest_version,
            "latest_manifest_hash": self.latest_manifest_hash,
            "latest_manifest_quality_status": self.latest_manifest_quality_status,
            "latest_manifest_quality_reason": self.latest_manifest_quality_reason,
            "block_reasons": [item.value for item in self.block_reasons],
            "error_code": self.error_code,
            "retryable": self.retryable,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceExitBlockedOutcome:
        known = {
            "protocol_version",
            "outcome_id",
            "outcome",
            "evaluation_id",
            "action_intent_id",
            "task_id",
            "session_id",
            "source_capture_id",
            "requirement_set_id",
            "latest_manifest_id",
            "latest_manifest_version",
            "latest_manifest_hash",
            "latest_manifest_quality_status",
            "latest_manifest_quality_reason",
            "block_reasons",
            "error_code",
            "retryable",
            "created_at",
        }
        values = _read(payload, known=known, field_name="EvidenceExitBlockedOutcome")
        try:
            return cls(
                protocol_version=values["protocol_version"],
                outcome_id=values["outcome_id"],
                outcome=EvidenceExitBusinessOutcome(values["outcome"]),
                evaluation_id=values["evaluation_id"],
                action_intent_id=values["action_intent_id"],
                task_id=values["task_id"],
                session_id=values["session_id"],
                source_capture_id=values["source_capture_id"],
                requirement_set_id=values["requirement_set_id"],
                latest_manifest_id=values["latest_manifest_id"],
                latest_manifest_version=values["latest_manifest_version"],
                latest_manifest_hash=values["latest_manifest_hash"],
                latest_manifest_quality_status=values["latest_manifest_quality_status"],
                latest_manifest_quality_reason=values["latest_manifest_quality_reason"],
                block_reasons=tuple(
                    EvidenceExitBlockReason(item)
                    for item in _sequence(values["block_reasons"], "block_reasons")
                ),
                error_code=values["error_code"],
                retryable=values["retryable"],
                created_at=parse_aware_datetime(values["created_at"], "created_at"),
            )
        except (ProtocolContractError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


def classify_evidence_exit_business_outcome(
    snapshot: EvidenceExitGateSnapshot,
) -> EvidenceExitBusinessOutcome:
    """只依据持久化快照分类阻断原因，不触发补采或重新判质。"""
    if snapshot.lineage_integrity_failed:
        return EvidenceExitBusinessOutcome.EVIDENCE_INTEGRITY_FAILED
    if (
        snapshot.latest_manifest_quality_status == "pending"
        or snapshot.evidence_commit_in_progress_count > 0
    ):
        return EvidenceExitBusinessOutcome.EVIDENCE_PENDING
    # App 常无 requirement_set；不得把「仅有 inline 补证未结」误判成控制面残缺。
    missing_requirement_set = snapshot.requirement_set_id is None and (
        not snapshot._is_app_surface
    )
    if snapshot.latest_manifest_id is None or missing_requirement_set:
        return EvidenceExitBusinessOutcome.CONTROL_PLANE_INCOMPLETE
    if snapshot.latest_manifest_quality_status == "fail":
        return EvidenceExitBusinessOutcome.EVIDENCE_INSUFFICIENT
    if snapshot.nonterminal_active_session_requirement_count > 0:
        return EvidenceExitBusinessOutcome.EVIDENCE_PENDING
    # Web 仍可能因 inline 未结被拦；App 离场公式已忽略 inline，不应再落到此分支。
    if (
        not snapshot._is_app_surface
        and snapshot.nonterminal_inline_evidence_request_count > 0
    ):
        return EvidenceExitBusinessOutcome.EVIDENCE_PENDING
    return EvidenceExitBusinessOutcome.CONTROL_PLANE_INCOMPLETE


def build_evidence_exit_outcome_id(
    *,
    evaluation_id: str,
    outcome: EvidenceExitBusinessOutcome,
    latest_manifest_id: str | None,
    latest_manifest_version: int | None,
    latest_manifest_hash: str | None,
    requirement_set_id: str | None,
) -> str:
    return "evidence-exit-outcome:" + stable_protocol_hash(
        {
            "evaluation_id": _require_text(evaluation_id, "evaluation_id"),
            "outcome": outcome.value,
            "latest_manifest_id": latest_manifest_id,
            "latest_manifest_version": latest_manifest_version,
            "latest_manifest_hash": latest_manifest_hash,
            "requirement_set_id": requirement_set_id,
        }
    )


def _read(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str] | None = None,
    field_name: str,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(known if required is None else required),
            field_name=field_name,
        )
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field_name} 必须为对象")
    return value


def _sequence(value: Any, field_name: str) -> tuple[Any, ...]:
    try:
        return require_sequence(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc


def _require_aware_datetime(value: Any, field_name: str) -> datetime:
    try:
        return require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc
