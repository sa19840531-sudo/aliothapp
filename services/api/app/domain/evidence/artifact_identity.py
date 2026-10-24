"""证据附件可信身份的领域校验。"""

from __future__ import annotations

import re

_SHA256_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


class EvidenceArtifactIdentityError(ValueError):
    """证据附件身份不满足正式落库要求。"""

    def __init__(self, message: str, *, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(message)


def validate_evidence_artifact_identity(
    *,
    storage_path: str,
    checksum: str,
    size_bytes: int,
) -> None:
    """校验正式证据附件可验证、可定位的最小身份。"""
    if (
        not isinstance(storage_path, str)
        or not storage_path.strip()
        or storage_path != storage_path.strip()
    ):
        raise EvidenceArtifactIdentityError(
            "证据存储路径不能为空或包含首尾空白",
            reason_code="storage_path_blank",
        )
    if not isinstance(checksum, str) or _SHA256_PATTERN.fullmatch(checksum) is None:
        raise EvidenceArtifactIdentityError(
            "证据 checksum 必须为规范 SHA-256",
            reason_code="checksum_invalid",
        )
    if isinstance(size_bytes, bool) or not isinstance(size_bytes, int) or size_bytes < 0:
        raise EvidenceArtifactIdentityError(
            "证据 size_bytes 必须为非负整数",
            reason_code="size_bytes_invalid",
        )
