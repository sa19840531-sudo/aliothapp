"""质检任务目标平台与稳定引用合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

from services.api.app.domain.interaction.enums import SurfaceKind


class TaskTargetValidationError(ValueError):
    """任务目标不满足平台边界。"""


class TaskTargetPlatform(StrEnum):
    """业务任务入口支持的平台。"""

    H5 = "h5"
    APP = "app"
    WEB = "web"


ADULT_SELF_ATTESTATION_STATEMENT_VERSION = "adult-v1"


@dataclass(frozen=True, kw_only=True)
class AdultSelfAttestation:
    """由服务端冻结的操作者成年自我声明。"""

    declared: bool
    statement_version: str
    declared_by: str
    declared_at: datetime

    def __post_init__(self) -> None:
        if self.declared is not True:
            raise TaskTargetValidationError("Web 操作者必须主动声明成年")
        if self.statement_version != ADULT_SELF_ATTESTATION_STATEMENT_VERSION:
            raise TaskTargetValidationError("成年声明版本不受支持")
        if not isinstance(self.declared_by, str) or not self.declared_by.strip():
            raise TaskTargetValidationError("成年声明操作者不能为空")
        if not isinstance(self.declared_at, datetime) or self.declared_at.tzinfo is None:
            raise TaskTargetValidationError("成年声明时间必须包含时区")
        object.__setattr__(self, "declared_by", self.declared_by.strip())
        object.__setattr__(self, "declared_at", self.declared_at.astimezone(UTC))

    def to_dict(self) -> dict[str, object]:
        return {
            "declared": self.declared,
            "statement_version": self.statement_version,
            "declared_by": self.declared_by,
            "declared_at": self.declared_at.isoformat().replace("+00:00", "Z"),
        }

    @classmethod
    def from_dict(cls, payload: object) -> AdultSelfAttestation:
        """从已冻结的任务快照恢复声明，缺失或篡改时立即失败。"""

        if not isinstance(payload, Mapping):
            raise TaskTargetValidationError("Web 任务必须提供成年声明快照")
        raw_declared_at = payload.get("declared_at")
        if isinstance(raw_declared_at, datetime):
            declared_at = raw_declared_at
        elif isinstance(raw_declared_at, str):
            try:
                declared_at = datetime.fromisoformat(
                    raw_declared_at.replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise TaskTargetValidationError("成年声明时间格式无效") from exc
        else:
            raise TaskTargetValidationError("成年声明时间格式无效")
        return cls(
            declared=payload.get("declared") is True,
            statement_version=str(payload.get("statement_version") or ""),
            declared_by=str(payload.get("declared_by") or ""),
            declared_at=declared_at,
        )


@dataclass(frozen=True, kw_only=True)
class AppTaskConfig:
    """App 目标只保存制品与设备能力引用。"""

    platform_build_ref: str
    package_name: str | None = None
    device_backend: str = "cloud_phone"
    device_profile_id: str = "default"

    def __post_init__(self) -> None:
        for field_name in (
            "platform_build_ref",
            "device_backend",
            "device_profile_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise TaskTargetValidationError(f"{field_name} 不能为空")
            object.__setattr__(self, field_name, value.strip())
        if self.package_name is not None:
            if not isinstance(self.package_name, str) or not self.package_name.strip():
                raise TaskTargetValidationError("package_name 不能为空字符串")
            object.__setattr__(self, "package_name", self.package_name.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "platform_build_ref": self.platform_build_ref,
            "package_name": self.package_name,
            "device": {
                "backend": self.device_backend,
                "profile_id": self.device_profile_id,
            },
        }


@dataclass(frozen=True, kw_only=True)
class WebTaskConfig:
    """Web 目标保存 Browser Profile 引用和成年声明快照。"""

    browser_profile_id: str
    adult_self_attestation: AdultSelfAttestation | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.browser_profile_id, str) or not self.browser_profile_id.strip():
            raise TaskTargetValidationError("browser_profile_id 不能为空")
        object.__setattr__(self, "browser_profile_id", self.browser_profile_id.strip())

    def to_dict(self) -> dict[str, object]:
        return {
            "browser_profile_id": self.browser_profile_id,
            "adult_self_attestation": (
                self.adult_self_attestation.to_dict()
                if self.adult_self_attestation is not None
                else None
            ),
        }


@dataclass(frozen=True, kw_only=True)
class TaskTarget:
    """统一任务目标；业务平台与运行时 Surface 保持分离。"""

    platform: TaskTargetPlatform
    target_url: str | None = None
    app: AppTaskConfig | None = None
    web: WebTaskConfig | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.platform, TaskTargetPlatform):
            raise TaskTargetValidationError("target_platform 不受支持")
        if self.platform is TaskTargetPlatform.APP:
            if self.target_url is not None:
                raise TaskTargetValidationError("App 任务的 target_url 必须为空")
            if not isinstance(self.app, AppTaskConfig):
                raise TaskTargetValidationError("App 任务必须提供 platform_build_ref")
            if self.web is not None:
                raise TaskTargetValidationError("App 任务不能携带 Web 配置")
            return

        if self.app is not None:
            raise TaskTargetValidationError("H5/Web 任务不能携带 App 配置")
        if not _is_http_url(self.target_url):
            raise TaskTargetValidationError("H5/Web 任务必须提供 HTTP(S) target_url")
        object.__setattr__(self, "target_url", self.target_url.strip())
        if self.platform is TaskTargetPlatform.WEB:
            if not isinstance(self.web, WebTaskConfig):
                raise TaskTargetValidationError("Web 任务必须提供 Browser Profile 配置")
            return
        if self.web is not None:
            raise TaskTargetValidationError("H5 任务不能携带 Web 配置")

    @property
    def surface_kind(self) -> SurfaceKind:
        if self.platform is TaskTargetPlatform.APP:
            return SurfaceKind.ANDROID
        return SurfaceKind.WEB

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "platform": self.platform.value,
            "target_url": self.target_url,
            "app": self.app.to_dict() if self.app is not None else None,
        }
        if self.web is not None:
            payload["web"] = self.web.to_dict()
        return payload


def _is_http_url(value: str | None) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlsplit(value.strip())
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
