"""通用材料导出验证真实字节、保守派生边界与整组失败的原子性。"""

import asyncio
import json
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

import pytest
from PIL import Image

from services.api.app.api.v1.auth import _permissions_by_role
from services.api.app.application.evidence_archive import (
    REDACTION_POLICY,
    EvidenceArchiveError,
    build_evidence_archive,
)
from services.api.app.domain.iam.access_scope import AccessScope


def entry(content, media="text/plain", artifact_id="evidence"):
    return (
        "001/001/before_action/001",
        {
            "artifact_id": artifact_id,
            "artifact_type": "visible_text",
            "content_type": media,
            "size_bytes": len(content),
            "checksum": "sha256:" + sha256(content).hexdigest(),
        },
        {"capture_id": "capture", "manifest_id": "manifest", "manifest_version": 1},
    )


@pytest.mark.parametrize(
    "media,content,strategy",
    [
        ("text/plain", b"Alice phone: 13800138000", "full_text_masked"),
        ("text/html", b'<input value="secret"><p>alice@example.com</p>', "full_document_masked"),
        (
            "application/json",
            b'{"secret-key": {"cookie": "token"}, "phone": [13800138000]}',
            "all_json_keys_and_values_masked",
        ),
    ],
)
async def test_masked_registered_text_formats_have_explicit_non_reviewable_receipt(
    media,
    content,
    strategy,
):
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": content}))
    result = await build_evidence_archive(
        entries=[entry(content, media)], receipt={}, reader=reader, mode="redacted"
    )
    with ZipFile(BytesIO(result.content)) as archive:
        receipt = json.loads(archive.read("receipt.json"))
        material = receipt["materials"][0]
        derivative = archive.read(material["path"])
        assert b"secret" not in derivative and b"Alice" not in derivative
        assert b"alice@example.com" not in derivative and b"13800138000" not in derivative
        assert material["redaction_strategy"] == strategy
        assert receipt["policy_version"] == REDACTION_POLICY
        assert receipt["business_reviewable"] is False
        assert material["original_checksum"] == entry(content)[1]["checksum"]
        assert material["checksum"] == "sha256:" + sha256(derivative).hexdigest()
    assert reader.evidence_file.return_value["content"] == content


@pytest.mark.parametrize(
    "fmt,media", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")]
)
async def test_masked_image_reencodes_only_pixels_and_never_source_metadata(fmt, media):
    output = BytesIO()
    Image.new("RGB", (30, 20), "red").save(output, format=fmt, comment=b"private-original")
    original = output.getvalue()
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": original}))
    result = await build_evidence_archive(
        entries=[entry(original, media)], receipt={}, reader=reader, mode="redacted"
    )
    with ZipFile(BytesIO(result.content)) as archive:
        material = result.materials[0]
        derivative = archive.read(material["path"])
        assert material["path"].endswith(".png")
        assert b"private-original" not in derivative
        with Image.open(BytesIO(derivative)) as masked:
            assert masked.size == (30, 20) and masked.format == "PNG"
            assert not masked.info
            assert set(masked.getdata()) == {(32, 32, 32)}


@pytest.mark.parametrize(
    "media,content,code",
    [
        ("application/octet-stream", b"original", "REDACTION_FORMAT_UNSUPPORTED"),
        ("image/png", b"not-a-png", "REDACTION_SOURCE_INVALID"),
        ("application/json", b"not-json", "REDACTION_SOURCE_INVALID"),
        ("text/plain", b"\xff\xfe", "REDACTION_SOURCE_INVALID"),
    ],
)
async def test_unknown_or_invalid_source_has_no_false_redaction(media, content, code):
    reader = SimpleNamespace(evidence_file=AsyncMock(return_value={"content": content}))
    with pytest.raises(EvidenceArchiveError) as failure:
        await build_evidence_archive(
            entries=[entry(content, media)], receipt={}, reader=reader, mode="redacted"
        )
    assert failure.value.code == code and failure.value.artifact_id == "evidence"


async def test_bounded_storage_wait_is_a_located_failure():
    async def delayed(*_args, **_kwargs):
        await asyncio.sleep(10)

    reader = SimpleNamespace(evidence_file=delayed)
    with pytest.raises(EvidenceArchiveError) as failure:
        await build_evidence_archive(
            entries=[entry(b"evidence")], receipt={}, reader=reader, read_timeout=0.001
        )
    assert failure.value.code == "ARTIFACT_READ_TIMEOUT"
    assert failure.value.artifact_id == "evidence"


async def test_no_zip_is_created_until_every_original_has_been_verified():
    content = b"first"
    reader = SimpleNamespace(
        evidence_file=AsyncMock(
            side_effect=[
                {"content": content},
                FileNotFoundError("private-path"),
            ]
        )
    )
    with patch("services.api.app.application.evidence_archive.ZipFile") as archive:
        with pytest.raises(EvidenceArchiveError):
            await build_evidence_archive(
                entries=[entry(content), entry(content, artifact_id="late")],
                receipt={},
                reader=reader,
            )
        archive.assert_not_called()


@pytest.mark.parametrize("role", ["平台管理员", "公司管理员", "审计员", "规则配置员"])
def test_export_mode_capabilities_keep_existing_object_access_boundary(role):
    permissions = frozenset(_permissions_by_role(role))
    assert {"evidenceExports:original", "evidenceExports:redacted"} <= permissions
    scope = AccessScope(actor_user_id="owner", tenant_id="tenant", permissions=permissions)
    if role != "平台管理员":
        assert not scope.can_read_resource(owner_id="other", tenant_id="foreign")
