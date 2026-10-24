"""Evidence Request 的恢复策略与不可变计划。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.inspection.evidence_request import (
    EvidenceReplayPolicy,
    EvidenceRequest,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

RESTORATION_PLAN_PROTOCOL_VERSION = "restoration-plan.v1"


class RestorationValidationError(ValueError):
    """恢复计划或状态等价证明不满足安全约束。"""


class RestorationStrategy(StrEnum):
    """HTML 架构规定的恢复优先级。"""

    ORIGINAL_SURFACE_PIN = "original_surface_pin"
    SAME_CONTEXT_NEW_SURFACE = "same_context_new_surface"
    SAME_APP_SESSION = "same_app_session"
    NEW_SESSION_REPLAY = "new_session_replay"
    UNFULFILLABLE = "unfulfillable"


@dataclass(frozen=True, kw_only=True)
class RestorationPlan:
    """Orchestrator 锁定的恢复边界，不包含 Driver 动作。"""

    plan_id: str
    plan_hash: str
    request_id: str
    task_id: str
    original_capture_id: str
    source_session_id: str
    target_session_id: str
    surface_kind: SurfaceKind
    strategy: RestorationStrategy
    deadline: datetime
    replay_execution_plan_id: str | None = None
    normalization_step_ids: tuple[str, ...] = ()
    protocol_version: str = RESTORATION_PLAN_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "plan_id",
            "plan_hash",
            "request_id",
            "task_id",
            "original_capture_id",
            "source_session_id",
            "target_session_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware_datetime(self.deadline, "deadline")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise RestorationValidationError("surface_kind 不受支持")
        if not isinstance(self.strategy, RestorationStrategy):
            raise RestorationValidationError("strategy 不受支持")
        if (
            self.strategy is RestorationStrategy.SAME_CONTEXT_NEW_SURFACE
            and self.surface_kind is not SurfaceKind.WEB
        ):
            raise RestorationValidationError("同 Context 新 Surface 只适用于 Web")
        if (
            self.strategy is RestorationStrategy.SAME_APP_SESSION
            and self.surface_kind is SurfaceKind.WEB
        ):
            raise RestorationValidationError("同 App Session 恢复不适用于 Web")
        steps = _unique_text_tuple(self.normalization_step_ids, "normalization_step_ids")
        if self.replay_execution_plan_id is not None:
            _require_text(self.replay_execution_plan_id, "replay_execution_plan_id")
        requires_replay = self.strategy in {
            RestorationStrategy.SAME_CONTEXT_NEW_SURFACE,
            RestorationStrategy.SAME_APP_SESSION,
            RestorationStrategy.NEW_SESSION_REPLAY,
        }
        if requires_replay != (self.replay_execution_plan_id is not None):
            raise RestorationValidationError("恢复策略与 replay_execution_plan_id 不一致")
        if self.strategy is RestorationStrategy.NEW_SESSION_REPLAY:
            if self.target_session_id == self.source_session_id:
                raise RestorationValidationError("新 Session 重放必须使用不同 Session")
        elif self.target_session_id != self.source_session_id:
            raise RestorationValidationError("非新 Session 策略不能切换 Session")
        if self.protocol_version != RESTORATION_PLAN_PROTOCOL_VERSION:
            raise RestorationValidationError("RestorationPlan protocol_version 不受支持")
        expected_hash = _restoration_plan_hash(
            request_id=self.request_id,
            task_id=self.task_id,
            original_capture_id=self.original_capture_id,
            source_session_id=self.source_session_id,
            target_session_id=self.target_session_id,
            surface_kind=self.surface_kind,
            strategy=self.strategy,
            replay_execution_plan_id=self.replay_execution_plan_id,
            normalization_step_ids=steps,
            deadline=self.deadline,
        )
        if self.plan_hash != expected_hash:
            raise RestorationValidationError("plan_hash 与恢复计划内容不一致")
        if self.plan_id != f"restoration-plan:{expected_hash}":
            raise RestorationValidationError("plan_id 与 plan_hash 不一致")
        object.__setattr__(self, "normalization_step_ids", steps)

    @property
    def source_grade(self) -> EvidenceSourceGrade:
        return source_grade_for_strategy(self.strategy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "plan_id": self.plan_id,
            "plan_hash": self.plan_hash,
            "request_id": self.request_id,
            "task_id": self.task_id,
            "original_capture_id": self.original_capture_id,
            "source_session_id": self.source_session_id,
            "target_session_id": self.target_session_id,
            "surface_kind": self.surface_kind.value,
            "strategy": self.strategy.value,
            "replay_execution_plan_id": self.replay_execution_plan_id,
            "normalization_step_ids": list(self.normalization_step_ids),
            "deadline": self.deadline.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RestorationPlan:
        known = {
            "protocol_version",
            "plan_id",
            "plan_hash",
            "request_id",
            "task_id",
            "original_capture_id",
            "source_session_id",
            "target_session_id",
            "surface_kind",
            "strategy",
            "replay_execution_plan_id",
            "normalization_step_ids",
            "deadline",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="RestorationPlan",
            )
            return cls(
                plan_id=values["plan_id"],
                plan_hash=values["plan_hash"],
                request_id=values["request_id"],
                task_id=values["task_id"],
                original_capture_id=values["original_capture_id"],
                source_session_id=values["source_session_id"],
                target_session_id=values["target_session_id"],
                surface_kind=SurfaceKind(values["surface_kind"]),
                strategy=RestorationStrategy(values["strategy"]),
                replay_execution_plan_id=values["replay_execution_plan_id"],
                normalization_step_ids=require_sequence(
                    values["normalization_step_ids"],
                    "normalization_step_ids",
                ),
                deadline=parse_aware_datetime(values["deadline"], "deadline"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise RestorationValidationError(str(exc)) from exc


def build_restoration_plan(
    *,
    request: EvidenceRequest,
    source_session_id: str,
    target_session_id: str,
    surface_kind: SurfaceKind,
    strategy: RestorationStrategy,
    deadline: datetime,
    replay_execution_plan_id: str | None = None,
    normalization_step_ids: Sequence[str] = (),
) -> RestorationPlan:
    """按 Evidence Request 的四个正交维度验证并锁定恢复计划。"""
    if not isinstance(request, EvidenceRequest):
        raise RestorationValidationError("request 必须为 EvidenceRequest")
    _validate_strategy_for_request(
        request=request,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
        strategy=strategy,
    )
    if deadline > request.expires_at:
        raise RestorationValidationError("恢复 deadline 不能晚于 Evidence Request 过期时间")
    normalized_steps = _unique_text_tuple(normalization_step_ids, "normalization_step_ids")
    plan_hash = _restoration_plan_hash(
        request_id=request.request_id,
        task_id=request.task_id,
        original_capture_id=request.target_capture_id,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
        surface_kind=surface_kind,
        strategy=strategy,
        replay_execution_plan_id=replay_execution_plan_id,
        normalization_step_ids=normalized_steps,
        deadline=deadline,
    )
    return RestorationPlan(
        plan_id=f"restoration-plan:{plan_hash}",
        plan_hash=plan_hash,
        request_id=request.request_id,
        task_id=request.task_id,
        original_capture_id=request.target_capture_id,
        source_session_id=source_session_id,
        target_session_id=target_session_id,
        surface_kind=surface_kind,
        strategy=strategy,
        replay_execution_plan_id=replay_execution_plan_id,
        normalization_step_ids=normalized_steps,
        deadline=deadline,
    )


def source_grade_for_strategy(strategy: RestorationStrategy) -> EvidenceSourceGrade:
    return {
        RestorationStrategy.ORIGINAL_SURFACE_PIN: EvidenceSourceGrade.A1,
        RestorationStrategy.SAME_CONTEXT_NEW_SURFACE: EvidenceSourceGrade.B,
        RestorationStrategy.SAME_APP_SESSION: EvidenceSourceGrade.B,
        RestorationStrategy.NEW_SESSION_REPLAY: EvidenceSourceGrade.C,
        RestorationStrategy.UNFULFILLABLE: EvidenceSourceGrade.D,
    }[strategy]


def grade_meets_minimum(
    actual: EvidenceSourceGrade,
    minimum: EvidenceSourceGrade,
) -> bool:
    order = {
        EvidenceSourceGrade.A0: 0,
        EvidenceSourceGrade.A1: 1,
        EvidenceSourceGrade.B: 2,
        EvidenceSourceGrade.C: 3,
        EvidenceSourceGrade.D: 4,
    }
    return actual is not EvidenceSourceGrade.D and order[actual] <= order[minimum]


def grade_supports(
    grade: EvidenceSourceGrade,
    required: EvidenceRequiredEquivalence,
) -> bool:
    allowed = {
        EvidenceRequiredEquivalence.ORIGINAL_ATOMIC: {EvidenceSourceGrade.A0},
        EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION: {
            EvidenceSourceGrade.A0,
            EvidenceSourceGrade.A1,
        },
        EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT: {
            EvidenceSourceGrade.A0,
            EvidenceSourceGrade.A1,
            EvidenceSourceGrade.B,
            EvidenceSourceGrade.C,
        },
    }
    return grade in allowed[required]


def _validate_strategy_for_request(
    *,
    request: EvidenceRequest,
    source_session_id: str,
    target_session_id: str,
    strategy: RestorationStrategy,
) -> None:
    _require_text(source_session_id, "source_session_id")
    _require_text(target_session_id, "target_session_id")
    if not isinstance(strategy, RestorationStrategy):
        raise RestorationValidationError("strategy 不受支持")
    if strategy is RestorationStrategy.UNFULFILLABLE:
        return
    if (
        request.replay_policy is EvidenceReplayPolicy.FORBIDDEN
        and strategy is not RestorationStrategy.ORIGINAL_SURFACE_PIN
    ):
        raise RestorationValidationError("Evidence Request 禁止重放")
    if request.required_equivalence is EvidenceRequiredEquivalence.ORIGINAL_ATOMIC:
        raise RestorationValidationError("original_atomic 缺口不能通过恢复补采满足")
    if (
        request.side_effect_policy is EvidenceSideEffectPolicy.PROHIBITED
        and request.required_interaction is not None
    ):
        raise RestorationValidationError("禁止副作用的请求不能恢复后执行交互")
    if (
        request.session_dependency is EvidenceSessionDependency.ACTIVE_SESSION
        and target_session_id != source_session_id
    ):
        raise RestorationValidationError("Active Session 请求不能跨 Session 恢复")
    if request.replay_policy is EvidenceReplayPolicy.SAME_SURFACE_ONLY:
        if strategy is not RestorationStrategy.ORIGINAL_SURFACE_PIN:
            raise RestorationValidationError("same_surface_only 只能使用原 Surface Pin")
    elif (
        request.replay_policy is EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED
        and strategy is RestorationStrategy.NEW_SESSION_REPLAY
    ):
        raise RestorationValidationError("same_context_allowed 禁止新 Session 重放")
    if (
        request.required_equivalence is EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION
        and strategy is not RestorationStrategy.ORIGINAL_SURFACE_PIN
    ):
        raise RestorationValidationError("same_surface_continuation 只能使用原 Surface Pin")


def _restoration_plan_hash(
    *,
    request_id: str,
    task_id: str,
    original_capture_id: str,
    source_session_id: str,
    target_session_id: str,
    surface_kind: SurfaceKind,
    strategy: RestorationStrategy,
    replay_execution_plan_id: str | None,
    normalization_step_ids: Sequence[str],
    deadline: datetime,
) -> str:
    return stable_protocol_hash(
        {
            "request_id": request_id,
            "task_id": task_id,
            "original_capture_id": original_capture_id,
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "surface_kind": surface_kind.value,
            "strategy": strategy.value,
            "replay_execution_plan_id": replay_execution_plan_id,
            "normalization_step_ids": list(normalization_step_ids),
            "deadline": deadline.isoformat(),
        }
    )


def _unique_text_tuple(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_require_text(value, field_name) for value in values)
    if len(normalized) != len(set(normalized)):
        raise RestorationValidationError(f"{field_name} 不能重复")
    return normalized


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise RestorationValidationError(str(exc)) from exc


def _require_aware_datetime(value: Any, field_name: str) -> None:
    try:
        require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise RestorationValidationError(str(exc)) from exc
