"""Surface 阶段确定性重新观察的稳定合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from services.api.app.domain.interaction.captures import CaptureDirective
from services.api.app.domain.interaction.enums import SurfaceKind
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    require_text,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    parse_aware_datetime,
    read_known_fields,
    require_mapping,
    stable_protocol_hash,
    thaw_json_mapping,
)

TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION = "timed-observation-intent.v1"
TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V2 = "timed-observation-intent.v2"
TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V3 = "timed-observation-intent.v3"
_SUPPORTED_PROTOCOL_VERSIONS = {
    TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION,
    TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V2,
    TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V3,
}


@dataclass(frozen=True, kw_only=True)
class TimedObservationIntent:
    """等待后重新采集当前 App Surface，不表达或伪造用户动作。"""

    kind: ClassVar[str] = "timed_observation"

    task_id: str
    session_id: str
    phase_run_id: str
    decision_id: str
    observation_id: str
    source_capture_id: str
    surface_kind: SurfaceKind
    delay_ms: int
    directive: CaptureDirective
    deadline: datetime
    phase_key: str | None = None
    context_ref: str | None = None
    policy_ref: str | None = None
    capability_ref: str | None = None
    workflow_state: Mapping[str, Any] | None = None
    protocol_version: str = TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        if self.workflow_state is not None:
            object.__setattr__(
                self, "workflow_state", freeze_json_mapping(self.workflow_state, "workflow_state")
            )
        for field_name in (
            "task_id",
            "session_id",
            "phase_run_id",
            "decision_id",
            "observation_id",
            "source_capture_id",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.protocol_version not in _SUPPORTED_PROTOCOL_VERSIONS:
            raise InteractionValidationError("TimedObservationIntent 版本不受支持")
        if self.protocol_version == TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V2:
            require_text(self.phase_key, "phase_key")
        elif self.protocol_version == TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION:
            if self.phase_key is not None:
                raise InteractionValidationError("v1 TimedObservationIntent 不支持 phase_key")
        elif self.protocol_version == TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V3:
            # v3：phase_key 仍可选；三端合同字段可选，首期仍仅 App Surface。
            for ref_name in ("context_ref", "policy_ref", "capability_ref"):
                value = getattr(self, ref_name)
                if value is not None:
                    require_text(value, ref_name)
        if self.protocol_version != TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V3:
            if any(
                getattr(self, name) is not None
                for name in ("context_ref", "policy_ref", "capability_ref")
            ):
                raise InteractionValidationError(
                    "context_ref／policy_ref／capability_ref 仅 TimedObservation v3 可用"
                )
        if self.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
            raise InteractionValidationError("TimedObservationIntent 只支持 App Surface")
        if (
            isinstance(self.delay_ms, bool)
            or not isinstance(self.delay_ms, int)
            or self.delay_ms < 0
        ):
            raise InteractionValidationError("delay_ms 必须为非负整数")
        if not isinstance(self.directive, CaptureDirective):
            raise InteractionValidationError("directive 类型不正确")
        if self.directive.role != "observation":
            raise InteractionValidationError("重新观察 Capture role 必须为 observation")
        if not isinstance(self.deadline, datetime) or self.deadline.utcoffset() is None:
            raise InteractionValidationError("deadline 必须是带时区的 datetime")

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "protocol_version": self.protocol_version,
            "kind": self.kind,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "phase_run_id": self.phase_run_id,
            "decision_id": self.decision_id,
            "observation_id": self.observation_id,
            "source_capture_id": self.source_capture_id,
            "surface_kind": self.surface_kind.value,
            "delay_ms": self.delay_ms,
            "directive": self.directive.to_dict(),
            "deadline": self.deadline.isoformat(),
        }
        if self.phase_key is not None:
            payload["phase_key"] = self.phase_key
        if self.workflow_state is not None:
            payload["workflow_state"] = thaw_json_mapping(self.workflow_state)
        if self.context_ref is not None:
            payload["context_ref"] = self.context_ref
        if self.policy_ref is not None:
            payload["policy_ref"] = self.policy_ref
        if self.capability_ref is not None:
            payload["capability_ref"] = self.capability_ref
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TimedObservationIntent:
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(
                    {
                        "protocol_version",
                        "kind",
                        "task_id",
                        "session_id",
                        "phase_run_id",
                        "decision_id",
                        "observation_id",
                        "source_capture_id",
                        "surface_kind",
                        "delay_ms",
                        "directive",
                        "deadline",
                        "phase_key",
                        "context_ref",
                        "policy_ref",
                        "capability_ref",
                        "workflow_state",
                    }
                ),
                required_fields=frozenset(
                    {
                        "protocol_version",
                        "task_id",
                        "session_id",
                        "phase_run_id",
                        "decision_id",
                        "observation_id",
                        "source_capture_id",
                        "surface_kind",
                        "delay_ms",
                        "directive",
                        "deadline",
                    }
                ),
                field_name="TimedObservationIntent",
            )
            if values.get("kind", cls.kind) != cls.kind:
                raise InteractionValidationError("TimedObservationIntent kind 不受支持")
            return cls(
                task_id=values["task_id"],
                session_id=values["session_id"],
                phase_run_id=values["phase_run_id"],
                decision_id=values["decision_id"],
                observation_id=values["observation_id"],
                source_capture_id=values["source_capture_id"],
                surface_kind=SurfaceKind(values["surface_kind"]),
                delay_ms=values["delay_ms"],
                directive=CaptureDirective.from_dict(
                    require_mapping(values["directive"], "directive")
                ),
                deadline=parse_aware_datetime(values["deadline"], "deadline"),
                phase_key=values.get("phase_key"),
                context_ref=values.get("context_ref"),
                policy_ref=values.get("policy_ref"),
                capability_ref=values.get("capability_ref"),
                workflow_state=values.get("workflow_state"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, TypeError, ValueError) as exc:
            raise InteractionValidationError(str(exc)) from exc


def build_timed_observation_intent_v3(
    *,
    task_id: str,
    session_id: str,
    phase_run_id: str,
    decision_id: str,
    observation_id: str,
    source_capture_id: str,
    surface_kind: SurfaceKind,
    delay_ms: int,
    directive: CaptureDirective,
    deadline: datetime,
    writer_supports_v3: bool,
    reader_supports_v3: bool,
    scheduler_supports_v3: bool,
    adapter_supports_v3: bool,
    phase_key: str | None = None,
    context_ref: str | None = None,
    policy_ref: str | None = None,
    capability_ref: str | None = None,
) -> TimedObservationIntent:
    """构造 TimedObservation v3；四端均支持时才放行（T23）。首期仍仅 App Surface。"""

    from services.api.app.domain.exploration.flow_foundation_evidence import (
        TimedObservationProtocolVersion,
        assert_timed_observation_v3_enabled,
    )

    assert_timed_observation_v3_enabled(
        writer_supports_v3=writer_supports_v3,
        reader_supports_v3=reader_supports_v3,
        scheduler_supports_v3=scheduler_supports_v3,
        adapter_supports_v3=adapter_supports_v3,
        requested_version=TimedObservationProtocolVersion.V3,
    )
    return TimedObservationIntent(
        task_id=task_id,
        session_id=session_id,
        phase_run_id=phase_run_id,
        decision_id=decision_id,
        observation_id=observation_id,
        source_capture_id=source_capture_id,
        surface_kind=surface_kind,
        delay_ms=delay_ms,
        directive=directive,
        deadline=deadline,
        phase_key=phase_key,
        context_ref=context_ref,
        policy_ref=policy_ref,
        capability_ref=capability_ref,
        protocol_version=TIMED_OBSERVATION_INTENT_PROTOCOL_VERSION_V3,
    )


def build_timed_observation_id(
    *,
    phase_run_id: str,
    decision_id: str,
    source_capture_id: str,
) -> str:
    """按阶段决策生成重试稳定的 Observation 身份。"""

    for field_name, value in (
        ("phase_run_id", phase_run_id),
        ("decision_id", decision_id),
        ("source_capture_id", source_capture_id),
    ):
        require_text(value, field_name)
    return "timed-observation:" + stable_protocol_hash(
        {
            "phase_run_id": phase_run_id,
            "decision_id": decision_id,
            "source_capture_id": source_capture_id,
        }
    )
