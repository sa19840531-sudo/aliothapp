"""Surface Session 与逻辑 Surface Registry 的稳定领域合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction.enums import SurfaceKind
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_text,
)

SURFACE_SESSION_PROTOCOL_VERSION = "surface-session.v1"
SURFACE_REFERENCE_PROTOCOL_VERSION = "surface-reference.v1"


class SurfaceSessionStatus(StrEnum):
    """Surface Session 的权威生命周期。"""

    STARTING = "starting"
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSING = "closing"
    CLOSED = "closed"
    LOST = "lost"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        return self in {
            SurfaceSessionStatus.CLOSED,
            SurfaceSessionStatus.LOST,
            SurfaceSessionStatus.FAILED,
        }


class SurfaceReferenceStatus(StrEnum):
    """一个逻辑 Tab、Page 或 App Screen 在会话中的资源状态。"""

    ACTIVE = "active"
    PINNED = "pinned"
    DRAINING = "draining"
    RELEASED = "released"
    LOST = "lost"

    @property
    def is_terminal(self) -> bool:
        return self in {SurfaceReferenceStatus.RELEASED, SurfaceReferenceStatus.LOST}


@dataclass(frozen=True, kw_only=True)
class SurfaceSessionSnapshot:
    """可跨模块传递的会话投影；明确不包含租约 token 或 Driver 句柄。"""

    session_id: str
    task_id: str
    surface_kind: SurfaceKind
    session_owner: str
    status: SurfaceSessionStatus
    command_sequence: int
    finalization_epoch: int
    row_version: int
    account_environment_ref: str | None
    platform_build_ref: str | None
    device_ref: str | None
    current_surface_ref: str | None
    current_capture_id: str | None
    started_at: datetime
    updated_at: datetime
    device_lease_id: str | None = None
    device_lease_fencing_token: int | None = None
    protocol_version: str = SURFACE_SESSION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("session_id", "task_id", "session_owner"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise ValueError("surface_kind 不受支持")
        if not isinstance(self.status, SurfaceSessionStatus):
            raise ValueError("status 不受支持")
        for field_name in ("command_sequence", "finalization_epoch", "row_version"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        for field_name in (
            "account_environment_ref",
            "platform_build_ref",
            "device_ref",
            "device_lease_id",
            "current_surface_ref",
            "current_capture_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        _require_aware_datetime(self.started_at, "started_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.started_at:
            raise ValueError("updated_at 不能早于 started_at")
        if self.device_lease_fencing_token is not None and (
            isinstance(self.device_lease_fencing_token, bool)
            or not isinstance(self.device_lease_fencing_token, int)
            or self.device_lease_fencing_token < 1
        ):
            raise ValueError("device_lease_fencing_token 必须为正整数")
        if (self.device_lease_id is None) != (
            self.device_lease_fencing_token is None
        ):
            raise ValueError("设备租约 ID 与 fencing token 必须同时提供")
        if self.protocol_version != SURFACE_SESSION_PROTOCOL_VERSION:
            raise ValueError("SurfaceSessionSnapshot 版本不受支持")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "surface_kind": self.surface_kind.value,
            "session_owner": self.session_owner,
            "status": self.status.value,
            "command_sequence": self.command_sequence,
            "finalization_epoch": self.finalization_epoch,
            "state_version": self.row_version,
            "account_environment_ref": self.account_environment_ref,
            "platform_build_ref": self.platform_build_ref,
            "device_ref": self.device_ref,
            "device_lease_id": self.device_lease_id,
            "device_lease_fencing_token": self.device_lease_fencing_token,
            "current_surface_ref": self.current_surface_ref,
            "current_capture_id": self.current_capture_id,
            "started_at": self.started_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SurfaceSessionSnapshot:
        known = {
            "protocol_version",
            "session_id",
            "task_id",
            "surface_kind",
            "session_owner",
            "status",
            "command_sequence",
            "finalization_epoch",
            "state_version",
            "account_environment_ref",
            "platform_build_ref",
            "device_ref",
            "device_lease_id",
            "device_lease_fencing_token",
            "current_surface_ref",
            "current_capture_id",
            "started_at",
            "updated_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known - {
                    "device_lease_id",
                    "device_lease_fencing_token",
                }),
                field_name="SurfaceSessionSnapshot",
            )
            return cls(
                session_id=values["session_id"],
                task_id=values["task_id"],
                surface_kind=SurfaceKind(values["surface_kind"]),
                session_owner=values["session_owner"],
                status=SurfaceSessionStatus(values["status"]),
                command_sequence=values["command_sequence"],
                finalization_epoch=values["finalization_epoch"],
                row_version=values["state_version"],
                account_environment_ref=values["account_environment_ref"],
                platform_build_ref=values["platform_build_ref"],
                device_ref=values["device_ref"],
                device_lease_id=values.get("device_lease_id"),
                device_lease_fencing_token=values.get("device_lease_fencing_token"),
                current_surface_ref=values["current_surface_ref"],
                current_capture_id=values["current_capture_id"],
                started_at=parse_aware_datetime(values["started_at"], "started_at"),
                updated_at=parse_aware_datetime(values["updated_at"], "updated_at"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


@dataclass(frozen=True, kw_only=True)
class SurfaceReference:
    """不泄漏平台句柄的逻辑 Surface 身份。"""

    surface_ref_id: str
    session_id: str
    task_id: str
    surface_kind: SurfaceKind
    status: SurfaceReferenceStatus
    surface_key: str
    row_version: int
    current_capture_id: str | None
    pin_reason: str | None
    created_at: datetime
    updated_at: datetime
    protocol_version: str = SURFACE_REFERENCE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("surface_ref_id", "session_id", "task_id", "surface_key"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise ValueError("surface_kind 不受支持")
        if not isinstance(self.status, SurfaceReferenceStatus):
            raise ValueError("status 不受支持")
        if isinstance(self.row_version, bool) or not isinstance(self.row_version, int):
            raise ValueError("row_version 必须为非负整数")
        if self.row_version < 0:
            raise ValueError("row_version 必须为非负整数")
        for field_name in ("current_capture_id", "pin_reason"):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if self.status is SurfaceReferenceStatus.PINNED and self.pin_reason is None:
            raise ValueError("Pinned Surface 必须说明 pin_reason")
        if self.status is not SurfaceReferenceStatus.PINNED and self.pin_reason is not None:
            raise ValueError("只有 Pinned Surface 可以携带 pin_reason")
        _require_aware_datetime(self.created_at, "created_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at 不能早于 created_at")
        if self.protocol_version != SURFACE_REFERENCE_PROTOCOL_VERSION:
            raise ValueError("SurfaceReference 版本不受支持")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "surface_ref_id": self.surface_ref_id,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "surface_kind": self.surface_kind.value,
            "status": self.status.value,
            "surface_key": self.surface_key,
            "current_capture_id": self.current_capture_id,
            "pin_reason": self.pin_reason,
            "state_version": self.row_version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> SurfaceReference:
        known = {
            "protocol_version",
            "surface_ref_id",
            "session_id",
            "task_id",
            "surface_kind",
            "status",
            "surface_key",
            "current_capture_id",
            "pin_reason",
            "state_version",
            "created_at",
            "updated_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="SurfaceReference",
            )
            return cls(
                surface_ref_id=values["surface_ref_id"],
                session_id=values["session_id"],
                task_id=values["task_id"],
                surface_kind=SurfaceKind(values["surface_kind"]),
                status=SurfaceReferenceStatus(values["status"]),
                surface_key=values["surface_key"],
                current_capture_id=values["current_capture_id"],
                pin_reason=values["pin_reason"],
                row_version=values["state_version"],
                created_at=parse_aware_datetime(values["created_at"], "created_at"),
                updated_at=parse_aware_datetime(values["updated_at"], "updated_at"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise ValueError(str(exc)) from exc


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc


def _require_aware_datetime(value: Any, field_name: str) -> None:
    try:
        require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise ValueError(str(exc)) from exc
