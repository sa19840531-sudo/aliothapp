"""Android APK 构建制品领域合同。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class PlatformBuildStatus(StrEnum):
    """构建制品持久化状态。"""

    UPLOADING = "uploading"
    VERIFIED = "verified"
    REJECTED = "rejected"
    DELETED = "deleted"


@dataclass(frozen=True, kw_only=True)
class PlatformBuild:
    """任务可引用的 Android APK 制品。"""

    build_id: str
    platform: str
    original_filename: str
    storage_path: str | None
    sha256: str
    size_bytes: int
    status: PlatformBuildStatus
    created_by: str
    package_name: str | None = None
    version_name: str | None = None
    version_code: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("build_id", "original_filename", "created_by"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
        if self.platform != "android":
            raise ValueError("platform 只支持 android")
        if not isinstance(self.sha256, str) or not _SHA256_PATTERN.fullmatch(self.sha256):
            raise ValueError("sha256 必须为 64 位小写十六进制摘要")
        if not isinstance(self.size_bytes, int) or self.size_bytes <= 0:
            raise ValueError("size_bytes 必须大于 0")
        if not isinstance(self.status, PlatformBuildStatus):
            raise ValueError("status 不受支持")
        if self.status is PlatformBuildStatus.VERIFIED and not self.storage_path:
            raise ValueError("verified 构建必须提供 storage_path")
        for field_name in ("storage_path", "package_name", "version_name"):
            value = getattr(self, field_name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{field_name} 不能为空字符串")
        if self.version_code is not None and self.version_code < 0:
            raise ValueError("version_code 不能为负数")

    @property
    def platform_build_ref(self) -> str:
        return f"app-build:{self.build_id}"
