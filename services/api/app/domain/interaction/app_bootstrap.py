"""App 首屏前安装与打开的稳定领域端口。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    optional_text,
    require_text,
)

APP_BOOTSTRAP_PROTOCOL_VERSION = "app-bootstrap.v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AppInstallReplacePolicy(StrEnum):
    """安装覆盖策略由 Workflow 锁定，Explorer 无权修改。"""

    SAME_CHECKSUM_ONLY = "same_checksum_only"
    REPLACE_IF_DIFFERENT = "replace_if_different"


@dataclass(frozen=True, kw_only=True)
class AppInstallRequest:
    """使用构建制品引用发起安装，不携带 APK 二进制。"""

    task_id: str
    session_id: str
    platform_build_ref: str
    sha256: str
    package_name: str | None = None
    replace_policy: AppInstallReplacePolicy = AppInstallReplacePolicy.SAME_CHECKSUM_ONLY
    protocol_version: str = APP_BOOTSTRAP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "platform_build_ref"):
            require_text(getattr(self, field_name), field_name)
        if self.protocol_version != APP_BOOTSTRAP_PROTOCOL_VERSION:
            raise InteractionValidationError("App Bootstrap protocol_version 不受支持")
        if not isinstance(self.sha256, str) or not _SHA256_PATTERN.fullmatch(
            self.sha256.lower()
        ):
            raise InteractionValidationError("sha256 必须为 64 位十六进制摘要")
        object.__setattr__(self, "sha256", self.sha256.lower())
        optional_text(self.package_name, "package_name")
        if not isinstance(self.replace_policy, AppInstallReplacePolicy):
            raise InteractionValidationError("replace_policy 不受支持")

    @property
    def idempotency_key(self) -> str:
        return f"{self.session_id}:{self.sha256}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "platform_build_ref": self.platform_build_ref,
            "sha256": self.sha256,
            "package_name": self.package_name,
            "replace_policy": self.replace_policy.value,
            "idempotency_key": self.idempotency_key,
        }


@dataclass(frozen=True, kw_only=True)
class AppInstallResult:
    """安装操作的最终结果，不表达外部异步 Run 状态。"""

    request_id: str
    session_id: str
    succeeded: bool
    retryable: bool
    installed_package: str | None = None
    reused_existing: bool = False
    error_code: str | None = None

    def __post_init__(self) -> None:
        _validate_result(
            request_id=self.request_id,
            session_id=self.session_id,
            succeeded=self.succeeded,
            retryable=self.retryable,
            error_code=self.error_code,
        )
        optional_text(self.installed_package, "installed_package")
        if self.succeeded and self.installed_package is None:
            raise InteractionValidationError("安装成功必须返回 installed_package")
        if not isinstance(self.reused_existing, bool):
            raise InteractionValidationError("reused_existing 必须为布尔值")


@dataclass(frozen=True, kw_only=True)
class AppOpenRequest:
    """在已安装会话内显式打开目标包。"""

    task_id: str
    session_id: str
    package_name: str
    cold_start: bool = True
    activity: str | None = None
    protocol_version: str = APP_BOOTSTRAP_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "package_name"):
            require_text(getattr(self, field_name), field_name)
        optional_text(self.activity, "activity")
        if not isinstance(self.cold_start, bool):
            raise InteractionValidationError("cold_start 必须为布尔值")
        if self.protocol_version != APP_BOOTSTRAP_PROTOCOL_VERSION:
            raise InteractionValidationError("App Bootstrap protocol_version 不受支持")

    @property
    def idempotency_key(self) -> str:
        policy = "cold" if self.cold_start else "warm"
        return f"{self.session_id}:{self.package_name}:{policy}"


@dataclass(frozen=True, kw_only=True)
class AppOpenResult:
    """打开 App 的最终结果和可选前台确认。"""

    request_id: str
    session_id: str
    succeeded: bool
    retryable: bool
    package_name: str
    foreground_verified: bool
    activity: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _validate_result(
            request_id=self.request_id,
            session_id=self.session_id,
            succeeded=self.succeeded,
            retryable=self.retryable,
            error_code=self.error_code,
        )
        require_text(self.package_name, "package_name")
        optional_text(self.activity, "activity")
        if not isinstance(self.foreground_verified, bool):
            raise InteractionValidationError("foreground_verified 必须为布尔值")


class AppBootstrapPort(Protocol):
    """青云 Workflow 调用的安装和打开端口。"""

    async def ensure_installed(self, request: AppInstallRequest) -> AppInstallResult: ...

    async def open_app(self, request: AppOpenRequest) -> AppOpenResult: ...


def _validate_result(
    *,
    request_id: str,
    session_id: str,
    succeeded: bool,
    retryable: bool,
    error_code: str | None,
) -> None:
    require_text(request_id, "request_id")
    require_text(session_id, "session_id")
    if not isinstance(succeeded, bool) or not isinstance(retryable, bool):
        raise InteractionValidationError("succeeded/retryable 必须为布尔值")
    optional_text(error_code, "error_code")
    if succeeded == (error_code is not None):
        raise InteractionValidationError("成功状态与 error_code 不一致")
