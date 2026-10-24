"""Capture Profile 与不可变证据引用。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction.enums import SurfaceKind
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    as_tuple,
    optional_text,
    require_text,
    validate_unique_texts,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    read_known_fields,
    require_mapping,
    require_sequence,
)

CAPTURE_PROFILE_PROTOCOL_VERSION = "capture-profile.v1"
PAGE_CAPTURE_REFERENCE_VERSION = "page-capture.v1"
CAPTURE_REFERENCE_VERSION = "capture-ref.v1"


class CaptureKind(StrEnum):
    """Capture 平台明细类型。"""

    PAGE = "page"
    APP = "app"


@dataclass(frozen=True, kw_only=True)
class CaptureDirective:
    """一次动作前或动作后的最小证据采集要求。"""

    role: str
    artifact_types: tuple[str, ...]
    inherit_parent_semantics: bool = False

    def __post_init__(self) -> None:
        require_text(self.role, "capture.role")
        artifact_types = as_tuple(self.artifact_types, "capture.artifact_types")
        validate_unique_texts(artifact_types, "capture.artifact_types")
        if not artifact_types:
            raise InteractionValidationError("capture.artifact_types 不能为空")
        if not isinstance(self.inherit_parent_semantics, bool):
            raise InteractionValidationError("inherit_parent_semantics 必须为布尔值")
        object.__setattr__(self, "artifact_types", artifact_types)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "artifact_types": list(self.artifact_types),
            "inherit_parent_semantics": self.inherit_parent_semantics,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CaptureDirective:
        values = _read_known(
            payload,
            known={"role", "artifact_types", "inherit_parent_semantics"},
            required={"role", "artifact_types"},
            field_name="CaptureDirective",
        )
        try:
            artifact_types = require_sequence(values["artifact_types"], "artifact_types")
        except ProtocolContractError as exc:
            raise InteractionValidationError(str(exc)) from exc
        return cls(
            role=values["role"],
            artifact_types=artifact_types,
            inherit_parent_semantics=values.get("inherit_parent_semantics", False),
        )


@dataclass(frozen=True, kw_only=True)
class CaptureProfile:
    """Action Intent 锁定的动作前后采集策略。"""

    profile_id: str
    before: CaptureDirective
    after: CaptureDirective
    protocol_version: str = CAPTURE_PROFILE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_text(self.profile_id, "capture_profile.profile_id")
        if not isinstance(self.before, CaptureDirective):
            raise InteractionValidationError("capture_profile.before 类型不正确")
        if not isinstance(self.after, CaptureDirective):
            raise InteractionValidationError("capture_profile.after 类型不正确")
        if self.protocol_version != CAPTURE_PROFILE_PROTOCOL_VERSION:
            raise InteractionValidationError("CaptureProfile protocol_version 不受支持")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "profile_id": self.profile_id,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CaptureProfile:
        values = _read_known(
            payload,
            known={"protocol_version", "profile_id", "before", "after"},
            required={"protocol_version", "profile_id", "before", "after"},
            field_name="CaptureProfile",
        )
        try:
            before = require_mapping(values["before"], "before")
            after = require_mapping(values["after"], "after")
        except ProtocolContractError as exc:
            raise InteractionValidationError(str(exc)) from exc
        return cls(
            profile_id=values["profile_id"],
            before=CaptureDirective.from_dict(before),
            after=CaptureDirective.from_dict(after),
            protocol_version=values["protocol_version"],
        )


@dataclass(frozen=True, kw_only=True)
class CaptureRef:
    """Web/App 共用的已持久化 Capture 引用。"""

    task_id: str
    session_id: str
    capture_id: str
    surface_kind: SurfaceKind
    capture_kind: CaptureKind
    role: str
    artifact_ids: tuple[str, ...] = ()
    parent_capture_id: str | None = None
    action_execution_id: str | None = None
    # §15 capture_ref 协议字段；缺省时由 role 推导
    state_type: str | None = None
    version: str = CAPTURE_REFERENCE_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "capture_id", "role"):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise InteractionValidationError("surface_kind 不受支持")
        if not isinstance(self.capture_kind, CaptureKind):
            raise InteractionValidationError("capture_kind 不受支持")
        if self.version != CAPTURE_REFERENCE_VERSION:
            raise InteractionValidationError("CaptureRef version 不受支持")
        if (
            self.capture_kind is CaptureKind.PAGE
            and self.surface_kind is not SurfaceKind.WEB
        ):
            raise InteractionValidationError("Page Capture 必须属于 Web Surface")
        if (
            self.capture_kind is CaptureKind.APP
            and self.surface_kind is SurfaceKind.WEB
        ):
            raise InteractionValidationError("App Capture 不能属于 Web Surface")
        artifact_ids = as_tuple(self.artifact_ids, "artifact_ids")
        validate_unique_texts(artifact_ids, "artifact_ids")
        optional_text(self.parent_capture_id, "parent_capture_id")
        optional_text(self.action_execution_id, "action_execution_id")
        state_type = self.state_type
        if state_type is None or not str(state_type).strip():
            state_type = _default_state_type(self.role)
        else:
            state_type = require_text(state_type, "state_type")
        object.__setattr__(self, "artifact_ids", artifact_ids)
        object.__setattr__(self, "state_type", state_type)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "capture_id": self.capture_id,
            "surface_kind": self.surface_kind.value,
            "capture_kind": self.capture_kind.value,
            "role": self.role,
            "state_type": self.state_type,
            "artifact_ids": list(self.artifact_ids),
            "parent_capture_id": self.parent_capture_id,
            "action_execution_id": self.action_execution_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CaptureRef:
        values = _read_known(
            payload,
            known={
                "version",
                "task_id",
                "session_id",
                "capture_id",
                "surface_kind",
                "capture_kind",
                "role",
                "state_type",
                "artifact_ids",
                "parent_capture_id",
                "action_execution_id",
            },
            required={
                "version",
                "task_id",
                "session_id",
                "capture_id",
                "surface_kind",
                "capture_kind",
                "role",
            },
            field_name="CaptureRef",
        )
        try:
            artifact_ids = require_sequence(values.get("artifact_ids", ()), "artifact_ids")
            surface_kind = SurfaceKind(values["surface_kind"])
            capture_kind = CaptureKind(values["capture_kind"])
        except (ProtocolContractError, TypeError, ValueError) as exc:
            raise InteractionValidationError(str(exc)) from exc
        return cls(
            version=values["version"],
            task_id=values["task_id"],
            session_id=values["session_id"],
            capture_id=values["capture_id"],
            surface_kind=surface_kind,
            capture_kind=capture_kind,
            role=values["role"],
            state_type=values.get("state_type"),
            artifact_ids=artifact_ids,
            parent_capture_id=values.get("parent_capture_id"),
            action_execution_id=values.get("action_execution_id"),
        )


def default_capture_state_type(role: str) -> str:
    """由 capture_role 推导一等 state_type（落库与重建共用）。"""
    normalized = str(role or "").strip().casefold()
    mapping = {
        "before_action": "before_action",
        "after_action": "after_action",
        "baseline": "baseline",
        "bootstrap": "baseline",
        "probe": "probe",
        "restored": "restored",
        "observation": "observation",
        "exploration_terminal": "exploration_terminal",
        "evidence_insufficient": "evidence_insufficient",
    }
    return mapping.get(normalized, normalized or "observation")


def _default_state_type(role: str) -> str:
    return default_capture_state_type(role)


@dataclass(frozen=True, kw_only=True)
class CaptureReference:
    """旧版 Web Page Capture 引用，仅用于历史协议反序列化兼容。"""

    task_id: str
    session_id: str
    page_capture_id: str
    role: str
    artifact_ids: tuple[str, ...] = ()
    parent_page_capture_id: str | None = None
    action_execution_id: str | None = None
    version: str = PAGE_CAPTURE_REFERENCE_VERSION

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        require_text(self.session_id, "session_id")
        require_text(self.page_capture_id, "page_capture_id")
        require_text(self.role, "capture.role")
        if self.version != PAGE_CAPTURE_REFERENCE_VERSION:
            raise InteractionValidationError("CaptureReference version 不受支持")
        artifact_ids = as_tuple(self.artifact_ids, "artifact_ids")
        validate_unique_texts(artifact_ids, "artifact_ids")
        optional_text(self.parent_page_capture_id, "parent_page_capture_id")
        optional_text(self.action_execution_id, "action_execution_id")
        object.__setattr__(self, "artifact_ids", artifact_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "page_capture_id": self.page_capture_id,
            "role": self.role,
            "artifact_ids": list(self.artifact_ids),
            "parent_page_capture_id": self.parent_page_capture_id,
            "action_execution_id": self.action_execution_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> CaptureReference:
        values = _read_known(
            payload,
            known={
                "version",
                "task_id",
                "session_id",
                "page_capture_id",
                "role",
                "artifact_ids",
                "parent_page_capture_id",
                "action_execution_id",
            },
            required={
                "version",
                "task_id",
                "session_id",
                "page_capture_id",
                "role",
            },
            field_name="CaptureReference",
        )
        try:
            artifact_ids = require_sequence(values.get("artifact_ids", ()), "artifact_ids")
        except ProtocolContractError as exc:
            raise InteractionValidationError(str(exc)) from exc
        return cls(
            task_id=values["task_id"],
            session_id=values["session_id"],
            page_capture_id=values["page_capture_id"],
            role=values["role"],
            artifact_ids=artifact_ids,
            parent_page_capture_id=values.get("parent_page_capture_id"),
            action_execution_id=values.get("action_execution_id"),
            version=values["version"],
        )


def _read_known(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str],
    field_name: str,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(required),
            field_name=field_name,
        )
    except ProtocolContractError as exc:
        raise InteractionValidationError(str(exc)) from exc
