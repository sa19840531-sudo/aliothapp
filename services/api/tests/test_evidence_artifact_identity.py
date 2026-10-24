"""证据附件可信身份校验测试。"""

import pytest

from services.api.app.domain.evidence.artifact_identity import (
    EvidenceArtifactIdentityError,
    validate_evidence_artifact_identity,
)


def test_identity_accepts_real_empty_sha256() -> None:
    validate_evidence_artifact_identity(
        storage_path="task/capture/empty.txt",
        checksum=(
            "sha256:e3b0c44298fc1c149afbf4c8996fb924"
            "27ae41e4649b934ca495991b7852b855"
        ),
        size_bytes=0,
    )


@pytest.mark.parametrize(
    ("storage_path", "checksum", "size_bytes", "reason"),
    [
        (" ", "sha256:" + "a" * 64, 1, "storage_path_blank"),
        (" leading", "sha256:" + "a" * 64, 1, "storage_path_blank"),
        ("trailing ", "sha256:" + "a" * 64, 1, "storage_path_blank"),
        ("a", "", 1, "checksum_invalid"),
        ("a", "sha256:ABC", 1, "checksum_invalid"),
        ("a", "sha256:" + "a" * 63, 1, "checksum_invalid"),
        ("a", "sha256:" + "a" * 64, -1, "size_bytes_invalid"),
        ("a", "sha256:" + "a" * 64, True, "size_bytes_invalid"),
    ],
)
def test_identity_rejects_untrusted_values(
    storage_path: str,
    checksum: str,
    size_bytes: int,
    reason: str,
) -> None:
    with pytest.raises(EvidenceArtifactIdentityError) as error:
        validate_evidence_artifact_identity(
            storage_path=storage_path,
            checksum=checksum,
            size_bytes=size_bytes,
        )

    assert error.value.reason_code == reason
