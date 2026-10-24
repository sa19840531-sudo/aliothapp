"""从 APK 制品提取可审计的包身份（package / version）。

优先 aapt dump badging；无 aapt 时回退到 AndroidManifest 二进制扫描包名。
上传链路必须写入这些字段，禁止 verified 构建长期 package_name=null。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, kw_only=True)
class ApkIdentity:
    """APK 稳定身份摘要。"""

    package_name: str | None
    version_name: str | None
    version_code: int | None


def extract_apk_identity(apk_path: Path | str) -> ApkIdentity:
    """从本地 APK 路径提取包名与版本；解析失败时对应字段为 None。"""

    path = Path(apk_path)
    if not path.is_file():
        return ApkIdentity(package_name=None, version_name=None, version_code=None)

    from services.api.app.infrastructure.device.ldplayer import (
        try_extract_apk_version_from_aapt,
        try_extract_package_name_from_aapt,
    )

    package_name = try_extract_package_name_from_aapt(path)
    version = try_extract_apk_version_from_aapt(path)
    return ApkIdentity(
        package_name=(
            package_name.strip()
            if isinstance(package_name, str) and package_name.strip()
            else None
        ),
        version_name=(
            version.version_name.strip()
            if isinstance(version.version_name, str) and version.version_name.strip()
            else None
        ),
        version_code=version.version_code if isinstance(version.version_code, int) else None,
    )
