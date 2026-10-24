"""Explorer Agent 到 Interaction Runtime 的 Action Intent 合同。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from services.api.app.domain.interaction.captures import CaptureProfile
from services.api.app.domain.interaction.enums import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.validation import (
    ACTION_PARAMETER_FORBIDDEN_KEYS,
    InteractionValidationError,
    freeze_mapping,
    require_text,
    thaw_mapping,
    validate_semantic_target,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_mapping,
)

ACTION_INTENT_PROTOCOL_VERSION = "action-intent.v1"
_TARGETED_OPERATIONS = frozenset(
    {
        InteractionOperation.NAVIGATE,
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
        InteractionOperation.INPUT,
        InteractionOperation.HOVER,
        InteractionOperation.SEEK_MEDIA,
    }
)


@dataclass(frozen=True, kw_only=True)
class ActionIntent:
    """Explorer Agent 可创建、但不能自行执行的完整动作意图。"""

    task_id: str
    session_id: str
    intent_id: str
    idempotency_key: str
    surface_kind: SurfaceKind
    source_capture_id: str
    action_type: InteractionOperation
    semantic_target: str | None
    purpose: str
    capture_profile: CaptureProfile
    deadline: datetime
    parameters: Mapping[str, Any] = field(default_factory=dict)
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW
    protocol_version: str = ACTION_INTENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "session_id",
            "intent_id",
            "idempotency_key",
            "source_capture_id",
            "purpose",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.protocol_version != ACTION_INTENT_PROTOCOL_VERSION:
            raise InteractionValidationError("ActionIntent protocol_version 不受支持")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise InteractionValidationError("surface_kind 不受支持")
        if not isinstance(self.action_type, InteractionOperation):
            raise InteractionValidationError("action_type 不受支持")
        if not isinstance(self.risk_level, InteractionRiskLevel):
            raise InteractionValidationError("risk_level 不受支持")
        if self.action_type in _TARGETED_OPERATIONS:
            validate_semantic_target(self.semantic_target, "semantic_target")
        elif self.semantic_target is not None:
            validate_semantic_target(self.semantic_target, "semantic_target")
        if not isinstance(self.capture_profile, CaptureProfile):
            raise InteractionValidationError("capture_profile 类型不正确")
        if not isinstance(self.deadline, datetime) or self.deadline.utcoffset() is None:
            raise InteractionValidationError("deadline 必须是带时区的 datetime")
        object.__setattr__(
            self,
            "parameters",
            freeze_mapping(
                self.parameters,
                "parameters",
                forbidden_keys=ACTION_PARAMETER_FORBIDDEN_KEYS,
            ),
        )
        if self.action_type is InteractionOperation.SEEK_MEDIA:
            _validate_seek_parameters(self.parameters)
        _validate_ad_landing_parameters(
            action_type=self.action_type,
            semantic_target=self.semantic_target,
            purpose=self.purpose,
            parameters=self.parameters,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "intent_id": self.intent_id,
            "idempotency_key": self.idempotency_key,
            "surface_kind": self.surface_kind.value,
            "source_capture_id": self.source_capture_id,
            "action_type": self.action_type.value,
            "semantic_target": self.semantic_target,
            "parameters": thaw_mapping(self.parameters),
            "purpose": self.purpose,
            "risk_level": self.risk_level.value,
            "capture_profile": self.capture_profile.to_dict(),
            "deadline": self.deadline.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ActionIntent:
        """兼容解析当前版本并忽略未来生产者增加的未知字段。"""
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(
                    {
                        "protocol_version",
                        "task_id",
                        "session_id",
                        "intent_id",
                        "idempotency_key",
                        "surface_kind",
                        "source_capture_id",
                        "action_type",
                        "semantic_target",
                        "parameters",
                        "purpose",
                        "risk_level",
                        "capture_profile",
                        "deadline",
                    }
                ),
                required_fields=frozenset(
                    {
                        "protocol_version",
                        "task_id",
                        "session_id",
                        "intent_id",
                        "idempotency_key",
                        "surface_kind",
                        "source_capture_id",
                        "action_type",
                        "purpose",
                        "risk_level",
                        "capture_profile",
                        "deadline",
                    }
                ),
                field_name="ActionIntent",
            )
            return cls(
                task_id=values["task_id"],
                session_id=values["session_id"],
                intent_id=values["intent_id"],
                idempotency_key=values["idempotency_key"],
                surface_kind=SurfaceKind(values["surface_kind"]),
                source_capture_id=values["source_capture_id"],
                action_type=InteractionOperation(values["action_type"]),
                semantic_target=values.get("semantic_target"),
                parameters=require_mapping(values.get("parameters", {}), "parameters"),
                purpose=values["purpose"],
                risk_level=InteractionRiskLevel(values["risk_level"]),
                capture_profile=CaptureProfile.from_dict(
                    require_mapping(values["capture_profile"], "capture_profile")
                ),
                deadline=parse_aware_datetime(values["deadline"], "deadline"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise InteractionValidationError(str(exc)) from exc


def _validate_seek_parameters(parameters: Mapping[str, Any]) -> None:
    """限制播放器跳转为单次、有界的语义动作。"""
    target_fraction = parameters.get("target_fraction")
    target_time_seconds = parameters.get("target_time_seconds")
    if (target_fraction is None) == (target_time_seconds is None):
        raise InteractionValidationError(
            "seek_media 必须且只能提供 target_fraction 或 target_time_seconds"
        )
    if target_fraction is not None:
        if (
            isinstance(target_fraction, bool)
            or not isinstance(target_fraction, (int, float))
            or not math.isfinite(float(target_fraction))
            or not 0.05 <= float(target_fraction) <= 0.95
        ):
            raise InteractionValidationError(
                "target_fraction 必须在 0.05 到 0.95 之间"
            )
    if target_time_seconds is not None:
        if (
            isinstance(target_time_seconds, bool)
            or not isinstance(target_time_seconds, (int, float))
            or not math.isfinite(float(target_time_seconds))
            or not 0 < float(target_time_seconds) <= 86_400
        ):
            raise InteractionValidationError(
                "target_time_seconds 必须在 0 到 86400 秒之间"
            )


def _validate_ad_landing_parameters(
    *,
    action_type: InteractionOperation,
    semantic_target: str | None,
    purpose: str,
    parameters: Mapping[str, Any],
) -> None:
    """只允许可信广告外链候选开启源页/落地页证据窗口。"""
    if "capture_ad_landing_evidence" not in parameters:
        return
    enabled = parameters.get("capture_ad_landing_evidence")
    if not isinstance(enabled, bool):
        raise InteractionValidationError(
            "capture_ad_landing_evidence 必须是布尔值"
        )
    if not enabled:
        return
    if action_type not in {InteractionOperation.CLICK, InteractionOperation.TAP}:
        raise InteractionValidationError(
            "广告落地页证据只允许 CLICK 或 TAP 动作"
        )
    navigation_depth = parameters.get("navigation_depth")
    if (
        isinstance(navigation_depth, bool)
        or not isinstance(navigation_depth, int)
        or navigation_depth < 0
    ):
        raise InteractionValidationError(
            "广告落地页证据必须声明非负 navigation_depth"
        )
    del semantic_target, purpose
    if parameters.get("ad_landing_capability") != "ad_outbound":
        raise InteractionValidationError(
            "广告落地页证据必须由 ad_outbound 结构化能力候选开启"
        )
