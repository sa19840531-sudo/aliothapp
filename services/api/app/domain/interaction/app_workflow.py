"""可恢复 App Workflow 的版本化状态与交接合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.interaction.capabilities import SurfaceCapabilityManifest
from services.api.app.domain.interaction.captures import CaptureKind, CaptureRef
from services.api.app.domain.interaction.enums import SurfaceKind
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    optional_text,
    require_text,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_mapping,
)

APP_WORKFLOW_PROTOCOL_VERSION = "app-workflow.v1"
APP_READY_HANDOFF_PROTOCOL_VERSION = "app-ready-handoff.v2"


class AppBootstrapStage(StrEnum):
    """App 首屏就绪前可持久化、可重放的阶段。"""

    VALIDATING = "validating"
    ACQUIRING_SESSION = "acquiring_session"
    RESOLVING_BUILD = "resolving_build"
    INSTALLING = "installing"
    INSTALLED = "installed"
    OPENING = "opening"
    OPENED = "opened"
    BOOTSTRAP_CAPTURING = "bootstrap_capturing"
    APP_READY = "app_ready"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            AppBootstrapStage.APP_READY,
            AppBootstrapStage.FAILED,
            AppBootstrapStage.CANCELED,
        }


@dataclass(frozen=True, kw_only=True)
class AppBootstrapSnapshot:
    """数据库 Bootstrap 检查点的公共只读投影。"""

    task_id: str
    session_id: str
    platform_build_ref: str
    stage: AppBootstrapStage
    install_attempt: int
    open_attempt: int
    row_version: int
    started_at: datetime
    updated_at: datetime
    install_request_id: str | None = None
    installed_package: str | None = None
    open_request_id: str | None = None
    bootstrap_capture_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    ready_at: datetime | None = None
    failed_at: datetime | None = None
    protocol_version: str = APP_WORKFLOW_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "platform_build_ref"):
            require_text(getattr(self, field_name), field_name)
        if not isinstance(self.stage, AppBootstrapStage):
            raise InteractionValidationError("App Bootstrap stage 不受支持")
        if self.protocol_version != APP_WORKFLOW_PROTOCOL_VERSION:
            raise InteractionValidationError("App Workflow protocol_version 不受支持")
        for field_name in ("install_attempt", "open_attempt", "row_version"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise InteractionValidationError(f"{field_name} 必须为非负整数")
        for field_name in (
            "install_request_id",
            "installed_package",
            "open_request_id",
            "bootstrap_capture_id",
            "error_code",
            "error_message",
        ):
            optional_text(getattr(self, field_name), field_name)
        _require_aware_datetime(self.started_at, "started_at")
        _require_aware_datetime(self.updated_at, "updated_at")
        if self.updated_at < self.started_at:
            raise InteractionValidationError("updated_at 不能早于 started_at")
        for field_name in ("ready_at", "failed_at"):
            value = getattr(self, field_name)
            if value is not None:
                _require_aware_datetime(value, field_name)
                if value < self.started_at:
                    raise InteractionValidationError(f"{field_name} 不能早于 started_at")
        self._validate_stage_fields()

    def _validate_stage_fields(self) -> None:
        if self.stage is AppBootstrapStage.APP_READY:
            if (
                self.install_request_id is None
                or self.installed_package is None
                or self.open_request_id is None
                or self.bootstrap_capture_id is None
                or self.ready_at is None
            ):
                raise InteractionValidationError(
                    "APP_READY 必须包含安装、打开、首屏 Capture 和 ready_at"
                )
        elif self.ready_at is not None:
            raise InteractionValidationError("只有 APP_READY 可以包含 ready_at")
        if self.stage is AppBootstrapStage.FAILED:
            if self.error_code is None or self.failed_at is None:
                raise InteractionValidationError("FAILED 必须包含 error_code 和 failed_at")
        elif self.error_code is not None or self.error_message is not None:
            raise InteractionValidationError("只有 FAILED 可以包含失败信息")
        if self.stage is not AppBootstrapStage.FAILED and self.failed_at is not None:
            raise InteractionValidationError("只有 FAILED 可以包含 failed_at")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "platform_build_ref": self.platform_build_ref,
            "stage": self.stage.value,
            "install_request_id": self.install_request_id,
            "install_attempt": self.install_attempt,
            "installed_package": self.installed_package,
            "open_request_id": self.open_request_id,
            "open_attempt": self.open_attempt,
            "bootstrap_capture_id": self.bootstrap_capture_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "state_version": self.row_version,
            "started_at": self.started_at.isoformat(),
            "ready_at": self.ready_at.isoformat() if self.ready_at is not None else None,
            "failed_at": self.failed_at.isoformat() if self.failed_at is not None else None,
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppBootstrapSnapshot:
        known = {
            "protocol_version",
            "task_id",
            "session_id",
            "platform_build_ref",
            "stage",
            "install_request_id",
            "install_attempt",
            "installed_package",
            "open_request_id",
            "open_attempt",
            "bootstrap_capture_id",
            "error_code",
            "error_message",
            "state_version",
            "started_at",
            "ready_at",
            "failed_at",
            "updated_at",
        }
        required = {
            "protocol_version",
            "task_id",
            "session_id",
            "platform_build_ref",
            "stage",
            "install_attempt",
            "open_attempt",
            "state_version",
            "started_at",
            "updated_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(required),
                field_name="AppBootstrapSnapshot",
            )
            return cls(
                task_id=values["task_id"],
                session_id=values["session_id"],
                platform_build_ref=values["platform_build_ref"],
                stage=AppBootstrapStage(values["stage"]),
                install_request_id=values.get("install_request_id"),
                install_attempt=values["install_attempt"],
                installed_package=values.get("installed_package"),
                open_request_id=values.get("open_request_id"),
                open_attempt=values["open_attempt"],
                bootstrap_capture_id=values.get("bootstrap_capture_id"),
                error_code=values.get("error_code"),
                error_message=values.get("error_message"),
                row_version=values["state_version"],
                started_at=parse_aware_datetime(values["started_at"], "started_at"),
                ready_at=_parse_optional_datetime(values.get("ready_at"), "ready_at"),
                failed_at=_parse_optional_datetime(values.get("failed_at"), "failed_at"),
                updated_at=parse_aware_datetime(values["updated_at"], "updated_at"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise InteractionValidationError(str(exc)) from exc


@dataclass(frozen=True, kw_only=True)
class AppReadyHandoff:
    """Bootstrap 就绪后交给截图探索的引用型消息。"""

    task_id: str
    agent_run_id: str
    session_id: str
    capture: CaptureRef
    capability_manifest: SurfaceCapabilityManifest
    bootstrap_row_version: int
    installed_package: str | None = None
    protocol_version: str = APP_READY_HANDOFF_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "session_id"):
            require_text(getattr(self, field_name), field_name)
        if self.protocol_version not in {
            APP_WORKFLOW_PROTOCOL_VERSION,
            APP_READY_HANDOFF_PROTOCOL_VERSION,
        }:
            raise InteractionValidationError("App Ready Handoff protocol_version 不受支持")
        if self.protocol_version == APP_READY_HANDOFF_PROTOCOL_VERSION:
            require_text(self.installed_package, "installed_package")
        else:
            # 仅为读取升级前已落 Outbox 的 v1 payload；新 Handoff 必须携带安装身份。
            optional_text(self.installed_package, "installed_package")
        if not isinstance(self.capture, CaptureRef):
            raise InteractionValidationError("capture 必须为 CaptureRef")
        if self.capture.task_id != self.task_id or self.capture.session_id != self.session_id:
            raise InteractionValidationError("CaptureRef 与 App Ready 身份不一致")
        if (
            self.capture.capture_kind is not CaptureKind.APP
            or self.capture.surface_kind is not SurfaceKind.ANDROID
            or self.capture.role != "bootstrap"
        ):
            raise InteractionValidationError("App Ready 必须引用 Android Bootstrap Capture")
        if not isinstance(self.capability_manifest, SurfaceCapabilityManifest):
            raise InteractionValidationError("capability_manifest 类型不正确")
        if self.capability_manifest.surface_kind is not SurfaceKind.ANDROID:
            raise InteractionValidationError("App Ready Capability 必须属于 Android")
        if (
            isinstance(self.bootstrap_row_version, bool)
            or not isinstance(self.bootstrap_row_version, int)
            or self.bootstrap_row_version < 0
        ):
            raise InteractionValidationError("bootstrap_row_version 必须为非负整数")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "session_id": self.session_id,
            "capture": self.capture.to_dict(),
            "capability_manifest": self.capability_manifest.to_dict(),
            "bootstrap_row_version": self.bootstrap_row_version,
            "installed_package": self.installed_package,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> AppReadyHandoff:
        known = {
            "protocol_version",
            "task_id",
            "agent_run_id",
            "session_id",
            "capture",
            "capability_manifest",
            "bootstrap_row_version",
            "installed_package",
        }
        required = known - {"installed_package"}
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(required),
                field_name="AppReadyHandoff",
            )
            capture = require_mapping(values["capture"], "capture")
            manifest = require_mapping(values["capability_manifest"], "capability_manifest")
            return cls(
                task_id=values["task_id"],
                agent_run_id=values["agent_run_id"],
                session_id=values["session_id"],
                capture=CaptureRef.from_dict(capture),
                capability_manifest=SurfaceCapabilityManifest.from_dict(manifest),
                bootstrap_row_version=values["bootstrap_row_version"],
                installed_package=values.get("installed_package"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise InteractionValidationError(str(exc)) from exc


class AppWorkflowFailure(RuntimeError):
    """带稳定阶段和重试语义的 App Workflow 失败。"""

    def __init__(
        self,
        message: str,
        *,
        stage: AppBootstrapStage,
        error_code: str,
        retryable: bool,
    ) -> None:
        require_text(message, "message")
        if not isinstance(stage, AppBootstrapStage):
            raise InteractionValidationError("stage 不受支持")
        require_text(error_code, "error_code")
        if not isinstance(retryable, bool):
            raise InteractionValidationError("retryable 必须为布尔值")
        super().__init__(message)
        self.stage = stage
        self.error_code = error_code
        self.retryable = retryable


def _require_aware_datetime(value: datetime, field_name: str) -> None:
    try:
        require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise InteractionValidationError(str(exc)) from exc


def _parse_optional_datetime(value: Any, field_name: str) -> datetime | None:
    if value is None:
        return None
    return parse_aware_datetime(value, field_name)
