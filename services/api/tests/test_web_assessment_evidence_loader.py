"""Web 模型实际证据加载、归属、完整性与脱敏合同。"""

import hashlib
import io
import struct
import zlib
from dataclasses import dataclass

import pytest
from PIL import Image

from services.api.app.application.web_assessment_evidence import (
    StoredWebAssessmentArtifact,
    VerifiedWebAssessmentEvidenceLoader,
    _safe_attribute_value,
)


def png_bytes(*, size: tuple[int, int] = (1920, 1080)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color=(30, 40, 50)).save(buffer, format="PNG")
    return buffer.getvalue()


class MetadataReader:
    def __init__(self, artifacts):
        self.artifacts = artifacts

    async def read_many(self, *, artifact_ids):
        by_id = {item.artifact_id: item for item in self.artifacts}
        return tuple(by_id[item] for item in artifact_ids if item in by_id)


class ArtifactStore:
    def __init__(self, payloads):
        self.payloads = payloads

    async def read(self, storage_path):
        return self.payloads[storage_path]


@pytest.mark.asyncio
async def test_ad_model_loader_rejects_landing_artifacts_from_different_captures() -> None:
    screenshot = png_bytes()
    dom = b"<main>isolated ad landing</main>"
    artifacts = (
        stored(
            "landing-shot",
            "screenshot",
            screenshot,
            capture_id="capture-ad-a",
            content_type="image/png",
        ),
        stored(
            "landing-dom",
            "dom",
            dom,
            capture_id="capture-ad-b",
            content_type="text/html",
        ),
    )

    with pytest.raises(RuntimeError, match="WEB_ASSESSMENT_ARTIFACTS_NOT_SAME_CAPTURE"):
        await VerifiedWebAssessmentEvidenceLoader(
            metadata_reader=MetadataReader(artifacts),
            artifact_store=ArtifactStore(
                {
                    artifacts[0].storage_path: screenshot,
                    artifacts[1].storage_path: dom,
                }
            ),
        ).load(
            screenshot_artifact_id="landing-shot",
            dom_artifact_id="landing-dom",
        )


@pytest.mark.asyncio
async def test_ad_model_loader_rejects_auxiliary_ref_from_different_capture() -> None:
    payload = b"{}"
    artifacts = (
        stored("landing-shot", "screenshot", png_bytes(), content_type="image/png"),
        stored("landing-dom", "dom", payload, content_type="text/html"),
        stored("landing-ui", "ui_snapshot", payload, content_type="application/json"),
        stored("landing-har", "har", payload, content_type="application/json"),
        stored(
            "landing-trace",
            "action_trace",
            payload,
            capture_id="capture-other",
            content_type="application/json",
        ),
    )
    loader = VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore({item.storage_path: payload for item in artifacts}),
    )

    with pytest.raises(RuntimeError, match="WEB_ASSESSMENT_ARTIFACTS_NOT_SAME_CAPTURE"):
        await loader.validate_same_capture_artifacts(
            artifact_types={
                "landing-shot": "screenshot",
                "landing-dom": "dom",
                "landing-ui": "ui_snapshot",
                "landing-har": "har",
                "landing-trace": "action_trace",
            }
        )


class FilteringMetadataConnection:
    """按真实仓储的 ID 过滤语义返回当前存在的元数据行。"""

    def __init__(self, rows):
        self.rows = {row["id"]: row for row in rows}
        self.calls = []

    async def fetch(self, query, artifact_ids):
        self.calls.append(tuple(artifact_ids))
        return [self.rows[item] for item in artifact_ids if item in self.rows]


class MetadataPool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        connection = self.connection

        class Acquire:
            async def __aenter__(self):
                return connection

            async def __aexit__(self, exc_type, exc, traceback):
                return None

        return Acquire()


def stored(
    artifact_id: str,
    artifact_type: str,
    payload: bytes,
    *,
    capture_id: str = "capture-1",
    content_type: str,
) -> StoredWebAssessmentArtifact:
    return StoredWebAssessmentArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        task_id="task-1",
        capture_id=capture_id,
        storage_path=f"evidence/{artifact_id}",
        content_type=content_type,
        checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size_bytes=len(payload),
    )


def metadata_row(
    artifact_id: str,
    artifact_type: str,
    payload: bytes,
    *,
    content_type: str,
):
    return {
        "id": artifact_id,
        "task_id": "task-1",
        "page_capture_id": "capture-1",
        "parent_capture_id": None,
        "capture_role": "after_action",
        "capture_purpose": "web_page_ready_baseline",
        "supplemental_phase_protocol_version": None,
        "artifact_type": artifact_type,
        "storage_path": f"evidence/{artifact_id}",
        "content_type": content_type,
        "checksum": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "size_bytes": len(payload),
    }


@pytest.mark.asyncio
async def test_production_metadata_reader_missing_supplement_keeps_primary_and_gap() -> None:
    """生产仓储过滤掉可选补图时，主 screenshot+DOM 仍必须进入分类。"""

    from services.api.app.infrastructure.db.web_assessment_evidence_repository import (
        PostgresWebAssessmentArtifactMetadataReader,
    )

    screenshot = png_bytes()
    dom = b"<main>primary remains available</main>"
    connection = FilteringMetadataConnection(
        [
            metadata_row("viewport", "screenshot", screenshot, content_type="image/png"),
            metadata_row("dom", "dom", dom, content_type="text/html"),
        ]
    )
    evidence = await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=PostgresWebAssessmentArtifactMetadataReader(pool=MetadataPool(connection)),
        artifact_store=ArtifactStore({"evidence/viewport": screenshot, "evidence/dom": dom}),
    ).load(
        screenshot_artifact_id="viewport",
        dom_artifact_id="dom",
        supplemental_screenshot_artifact_ids=("missing-supplement",),
    )

    assert evidence.screenshot_bytes == screenshot
    assert "primary remains available" in evidence.dom_text
    assert evidence.supplemental_screenshot_artifact_ids == ()
    assert evidence.supplemental_evidence_gap_reason_codes == (
        "WEB_ASSESSMENT_ARTIFACT_METADATA_MISSING",
    )
    assert connection.calls == [("viewport", "dom"), ("missing-supplement",)]


@pytest.mark.asyncio
async def test_production_metadata_reader_missing_primary_still_fails_closed() -> None:
    """可选补图降级不能把缺失主 DOM 也转换成 supplemental gap。"""

    from services.api.app.infrastructure.db.web_assessment_evidence_repository import (
        PostgresWebAssessmentArtifactMetadataReader,
    )

    screenshot = png_bytes()
    connection = FilteringMetadataConnection(
        [metadata_row("viewport", "screenshot", screenshot, content_type="image/png")]
    )
    loader = VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=PostgresWebAssessmentArtifactMetadataReader(pool=MetadataPool(connection)),
        artifact_store=ArtifactStore({"evidence/viewport": screenshot}),
    )

    with pytest.raises(RuntimeError, match="WEB_ASSESSMENT_ARTIFACT_METADATA_MISSING"):
        await loader.load(
            screenshot_artifact_id="viewport",
            dom_artifact_id="missing-dom",
            supplemental_screenshot_artifact_ids=("missing-supplement",),
        )

    assert connection.calls == [("viewport", "missing-dom")]


@dataclass(frozen=True)
class SupplementalStoredArtifact:
    """模拟仓储返回的补图完整元数据，避免让测试夹带生产实现。"""

    artifact_id: str
    artifact_type: str
    task_id: str
    capture_id: str
    parent_capture_id: str | None
    capture_role: str
    capture_purpose: str | None
    supplemental_phase_protocol_version: str | None
    storage_path: str
    content_type: str
    checksum: str
    size_bytes: int


def supplemental_stored(
    artifact_id: str,
    payload: bytes,
    *,
    task_id: str = "task-1",
    parent_capture_id: str | None = "capture-1",
    capture_role: str = "supplemental",
    capture_purpose: str | None = "web_full_page_supplemental",
    supplemental_phase_protocol_version: str | None = "web-full-page-supplemental.v1",
) -> SupplementalStoredArtifact:
    return SupplementalStoredArtifact(
        artifact_id=artifact_id,
        artifact_type="screenshot",
        task_id=task_id,
        capture_id="capture-full-page",
        parent_capture_id=parent_capture_id,
        capture_role=capture_role,
        capture_purpose=capture_purpose,
        supplemental_phase_protocol_version=supplemental_phase_protocol_version,
        storage_path=f"evidence/{artifact_id}",
        content_type="image/png",
        checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size_bytes=len(payload),
    )


async def load_with_supplemental(*, supplemental: StoredWebAssessmentArtifact, payload: bytes):
    screenshot = png_bytes()
    dom = "<main>公开页面正文</main>".encode()
    artifacts = (
        stored("viewport", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
        supplemental,
    )
    return await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore(
            {
                artifacts[0].storage_path: screenshot,
                artifacts[1].storage_path: dom,
                supplemental.storage_path: payload,
            }
        ),
    ).load(
        screenshot_artifact_id="viewport",
        dom_artifact_id="dom",
        supplemental_screenshot_artifact_ids=(supplemental.artifact_id,),
    )


@pytest.mark.asyncio
async def test_loads_primary_and_parent_bound_full_page_supplemental() -> None:
    """替换长图为其他 Capture 或丢失二进制内容时必须不能进入模型证据。"""

    full_page = png_bytes(size=(1920, 2160))
    evidence = await load_with_supplemental(
        supplemental=supplemental_stored("full-page", full_page),
        payload=full_page,
    )

    assert evidence.supplemental_screenshot_artifact_ids == ("full-page",)
    assert evidence.supplemental_screenshot_bytes == (full_page,)
    assert evidence.supplemental_screenshot_media_types == ("image/png",)


@pytest.mark.asyncio
async def test_full_page_with_horizontal_document_overflow_keeps_original_png() -> None:
    full_page = png_bytes(size=(1930, 2160))
    evidence = await load_with_supplemental(
        supplemental=supplemental_stored("wide-full-page", full_page),
        payload=full_page,
    )

    assert evidence.supplemental_screenshot_artifact_ids == ("wide-full-page",)
    assert evidence.supplemental_screenshot_bytes == (full_page,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected_code"),
    [
        ({"task_id": "other-task"}, "WEB_ASSESSMENT_SUPPLEMENTAL_NOT_SAME_TASK"),
        (
            {"parent_capture_id": "capture-grandparent"},
            "WEB_ASSESSMENT_SUPPLEMENTAL_NOT_DIRECT_CHILD",
        ),
        ({"capture_role": "after_action"}, "WEB_ASSESSMENT_SUPPLEMENTAL_ROLE_INVALID"),
        (
            {"capture_purpose": "web_page_ready_baseline"},
            "WEB_ASSESSMENT_SUPPLEMENTAL_PURPOSE_INVALID",
        ),
        (
            {"supplemental_phase_protocol_version": None},
            "WEB_ASSESSMENT_SUPPLEMENTAL_PHASE_INVALID",
        ),
        (
            {"supplemental_phase_protocol_version": "web-full-page-supplemental.v2"},
            "WEB_ASSESSMENT_SUPPLEMENTAL_PHASE_INVALID",
        ),
    ],
)
async def test_supplemental_lineage_failure_degrades_to_gap(kwargs, expected_code) -> None:
    """错误的任务、父节点、角色或用途只剔除长图，主证据继续分类。"""

    full_page = png_bytes(size=(1920, 2160))
    evidence = await load_with_supplemental(
        supplemental=supplemental_stored("full-page", full_page, **kwargs),
        payload=full_page,
    )

    assert evidence.supplemental_screenshot_artifact_ids == ()
    assert evidence.supplemental_evidence_gap_reason_codes == (expected_code,)


def _png_with_dimensions(*, width: int, height: int) -> bytes:
    payload = bytearray(png_bytes())
    payload[16:24] = width.to_bytes(4, "big") + height.to_bytes(4, "big")
    payload[29:33] = zlib.crc32(payload[12:29]).to_bytes(4, "big")
    return bytes(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        pytest.param(
            _png_with_dimensions(width=1919, height=2160),
            "WEB_ASSESSMENT_SUPPLEMENTAL_WIDTH_INVALID",
            id="width",
        ),
        pytest.param(
            _png_with_dimensions(width=1920, height=1079),
            "WEB_ASSESSMENT_SUPPLEMENTAL_HEIGHT_INVALID",
            id="height",
        ),
        pytest.param(
            _png_with_dimensions(width=32769, height=2160),
            "WEB_ASSESSMENT_SUPPLEMENTAL_DIMENSION_LIMIT_EXCEEDED",
            id="width-cap",
        ),
        pytest.param(
            _png_with_dimensions(width=1920, height=32769),
            "WEB_ASSESSMENT_SUPPLEMENTAL_DIMENSION_LIMIT_EXCEEDED",
            id="height-cap",
        ),
        pytest.param(
            _png_with_dimensions(width=5000, height=20001),
            "WEB_ASSESSMENT_SUPPLEMENTAL_PIXELS_EXCEEDED",
            id="pixel-cap",
        ),
        pytest.param(
            b"\x89PNG\r\n\x1a\n" + b"x" * (16 * 1024 * 1024 + 1),
            "WEB_ASSESSMENT_SUPPLEMENTAL_TOO_LARGE",
            id="byte-cap",
        ),
    ],
)
async def test_invalid_supplemental_png_degrades_to_gap_without_losing_primary(
    payload, expected_code
) -> None:
    """长图尺寸失败只删除该补充证据，主截图与 DOM 仍可继续分类。"""

    evidence = await load_with_supplemental(
        supplemental=supplemental_stored("full-page", payload),
        payload=payload,
    )

    assert evidence.screenshot_artifact_id == "viewport"
    assert evidence.dom_artifact_id == "dom"
    assert evidence.supplemental_screenshot_artifact_ids == ()
    assert evidence.supplemental_evidence_gap_reason_codes == (expected_code,)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum)
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def _full_page_png_without_image_data() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1920, 2160, 8, 2, 0, 0, 0)),
            _png_chunk(b"IEND", b""),
        )
    )


def _full_page_png_with_bad_ihdr_crc() -> bytes:
    payload = bytearray(png_bytes(size=(1920, 2160)))
    payload[29] ^= 0xFF
    return bytes(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "case"),
    [
        pytest.param(png_bytes(size=(1920, 2160))[:-12], "truncated", id="truncated"),
        pytest.param(_full_page_png_with_bad_ihdr_crc(), "ihdr-crc", id="ihdr-crc"),
        pytest.param(_full_page_png_without_image_data(), "missing-idat", id="missing-idat"),
    ],
)
async def test_incomplete_supplemental_png_becomes_gap(payload, case) -> None:
    """仅伪造签名和 IHDR 的长图不能进入模型，但不丢失主图。"""

    evidence = await load_with_supplemental(
        supplemental=supplemental_stored(f"full-page-{case}", payload),
        payload=payload,
    )

    assert evidence.supplemental_screenshot_artifact_ids == ()
    assert evidence.supplemental_evidence_gap_reason_codes == (
        "WEB_ASSESSMENT_SUPPLEMENTAL_NOT_PNG",
    )


async def load_dom_only(dom: bytes):
    screenshot = png_bytes()
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
    )
    return await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore(
            {
                artifacts[0].storage_path: screenshot,
                artifacts[1].storage_path: dom,
            }
        ),
    ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")


@pytest.mark.asyncio
async def test_public_preview_image_identifier_is_redacted_before_secret_detection() -> None:
    evidence = await load_dom_only(
        b'<main><img alt="0123456789abcdef0123456789abcdef" '
        b'data-image-zoom src="https://images.example.test/picture.jpg" '
        b'z-image-loader-url="https://images.example.test/source.jpg" '
        b"data-image-preview></main>"
    )

    assert 'alt="[redacted-image-resource-id]"' in evidence.dom_text
    assert "0123456789abcdef0123456789abcdef" not in evidence.dom_text
    assert evidence.redaction_categories == ("image_alt_resource_identifier",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "markup",
    [
        '<img alt="0123456789abcdef0123456789abcdef">',
        '<div alt="0123456789abcdef0123456789abcdef" data-image-preview></div>',
        '<img title="0123456789abcdef0123456789abcdef" data-image-preview>',
        '<img alt="Bearer real-test-credential" data-image-preview>',
        '<img alt="password=real-test-credential" data-image-preview>',
        '<img alt="password=real-test-credential" '
        'alt="0123456789abcdef0123456789abcdef" data-image-preview>',
        '<img alt="0123456789abcdef0123456789abcdef" '
        'data-image-preview title="token=real-test-credential">',
        '<img alt="0123456789abcdef0123456789abcdef" '
        'data-image-preview data-key="token=real-test-credential">',
        '<img alt="0123456789abcdef0123456789abcdef" '
        "data-image-preview><p>Authorization: Bearer real-test-credential</p>",
    ],
)
async def test_preview_marker_never_authorizes_other_sensitive_values(markup: str) -> None:
    markup = markup.replace(
        "data-image-preview",
        "data-image-preview data-image-zoom "
        'z-image-loader-url="https://images.example.test/source.jpg"',
    )
    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(f"<main>{markup}</main>".encode())


@pytest.mark.asyncio
async def test_loads_same_capture_and_redacts_untrusted_dom_and_text() -> None:
    screenshot = png_bytes()
    dom = (
        "<html><script>unsafe script</script>"
        '<input value="private-value" class="login-field">'
        '<a class="content-card" href="/article/'
        '0123456789abcdef0123456789abcdef?token=secret">标题</a></html>'
    ).encode()
    visible = "页面标题".encode()
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html; charset=utf-8"),
        stored(
            "visible",
            "visible_text",
            visible,
            content_type="text/plain; charset=utf-8",
        ),
    )

    evidence = await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore(
            {
                item.storage_path: payload
                for item, payload in zip(
                    artifacts,
                    (screenshot, dom, visible),
                    strict=True,
                )
            }
        ),
    ).load(
        screenshot_artifact_id="shot",
        dom_artifact_id="dom",
        visible_text_artifact_id="visible",
    )

    assert evidence.screenshot_bytes == screenshot
    assert "content-card" in evidence.dom_text
    assert "unsafe script" not in evidence.dom_text
    assert "private-value" not in evidence.dom_text
    assert "token=secret" not in evidence.dom_text
    assert "0123456789abcdef0123456789abcdef" not in evidence.dom_text
    assert evidence.visible_text == "页面标题"


@pytest.mark.asyncio
async def test_redacts_public_contact_data_and_continues_assessment() -> None:
    screenshot = png_bytes()
    dom = ('<main aria-label="联系 public@example.com">公开联系电话 13812345678</main>').encode()
    visible = "联系 public@example.com 或 13812345678".encode()
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
        stored("visible", "visible_text", visible, content_type="text/plain"),
    )

    evidence = await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore(
            {
                item.storage_path: payload
                for item, payload in zip(
                    artifacts,
                    (screenshot, dom, visible),
                    strict=True,
                )
            }
        ),
    ).load(
        screenshot_artifact_id="shot",
        dom_artifact_id="dom",
        visible_text_artifact_id="visible",
    )

    assert "public@example.com" not in evidence.dom_text
    assert "13812345678" not in evidence.dom_text
    assert "[redacted-email]" in evidence.dom_text
    assert "[redacted-phone]" in evidence.dom_text
    assert evidence.visible_text == ("联系 [redacted-email] 或 [redacted-phone]")


@pytest.mark.asyncio
async def test_rejects_detected_personal_data_before_unredacted_screenshot_reaches_llm() -> None:
    screenshot = png_bytes()
    dom = b"<main>contact private@example.com</main>"
    visible = "Authorization: Bearer secret-value; 电话 13812345678".encode()
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
        stored("visible", "visible_text", visible, content_type="text/plain"),
    )

    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await VerifiedWebAssessmentEvidenceLoader(
            metadata_reader=MetadataReader(artifacts),
            artifact_store=ArtifactStore(
                {
                    item.storage_path: payload
                    for item, payload in zip(
                        artifacts,
                        (screenshot, dom, visible),
                        strict=True,
                    )
                }
            ),
        ).load(
            screenshot_artifact_id="shot",
            dom_artifact_id="dom",
            visible_text_artifact_id="visible",
        )


@pytest.mark.asyncio
async def test_ignores_sensitive_markers_removed_with_hidden_script() -> None:
    """不可见脚本不会随 DOM 投影送模，也不能据此误拒绝整张页面截图。"""

    screenshot = png_bytes()
    dom = (
        "<html><script>Authorization: Bearer hidden-script-value; "
        "contact hidden@example.com</script><main>公开页面</main></html>"
    ).encode()
    visible = "公开页面".encode()
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
        stored("visible", "visible_text", visible, content_type="text/plain"),
    )

    evidence = await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore(
            {
                item.storage_path: payload
                for item, payload in zip(
                    artifacts,
                    (screenshot, dom, visible),
                    strict=True,
                )
            }
        ),
    ).load(
        screenshot_artifact_id="shot",
        dom_artifact_id="dom",
        visible_text_artifact_id="visible",
    )

    assert "hidden-script-value" not in evidence.dom_text
    assert "hidden@example.com" not in evidence.dom_text
    assert "公开页面" in evidence.dom_text


@pytest.mark.asyncio
async def test_rejects_bearer_token_in_dom_without_visible_text() -> None:
    screenshot = png_bytes()
    dom = b"<main>Authorization: Bearer secret-value</main>"
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
    )

    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await VerifiedWebAssessmentEvidenceLoader(
            metadata_reader=MetadataReader(artifacts),
            artifact_store=ArtifactStore(
                {
                    artifacts[0].storage_path: screenshot,
                    artifacts[1].storage_path: dom,
                }
            ),
        ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")


@pytest.mark.asyncio
async def test_rejects_cookie_credential_in_dom_without_visible_text() -> None:
    screenshot = png_bytes()
    dom = b"<main>Cookie: session_id=secret-value</main>"
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
    )

    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await VerifiedWebAssessmentEvidenceLoader(
            metadata_reader=MetadataReader(artifacts),
            artifact_store=ArtifactStore(
                {
                    artifacts[0].storage_path: screenshot,
                    artifacts[1].storage_path: dom,
                }
            ),
        ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dom",
    (
        b"<main>Cookie=session_id=credential-value</main>",
        b'<main>{"password":"credential-value"}</main>',
        b'<main>password: "credential-value"</main>',
        b"<main><span>Cookie:</span><span>session_id=credential-value</span></main>",
    ),
)
async def test_rejects_quoted_equals_and_fragmented_credentials(dom: bytes) -> None:
    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(dom)


@pytest.mark.asyncio
async def test_separate_episode_controls_do_not_form_one_hex_credential() -> None:
    episodes = "".join(
        f'<button class="player-episodes__item">{number:02d}</button>'
        for number in range(1, 22)
    )
    episode_links = "".join(
        f'<a class="content-episode" href="/video/{number}/">{number:02d}</a>'
        for number in range(1, 22)
    )

    evidence = await load_dom_only(
        (
            f'<main><div class="player-episodes__row">{episodes}</div>'
            f'<div class="content-episode-grid">{episode_links}</div></main>'
        ).encode()
    )

    assert '<button class="player-episodes__item">01</button>' in evidence.dom_text
    assert '<a class="content-episode" href="/video/21/">21</a>' in evidence.dom_text


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ("button", "a"))
async def test_adjacent_controls_do_not_hide_split_hex_credential(control: str) -> None:
    fragments = "".join(
        f"<{control}>0123456789abcdef</{control}>" for _ in range(2)
    )

    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(f"<main>{fragments}</main>".encode())


@pytest.mark.asyncio
async def test_episode_semantic_without_ordered_sequence_does_not_bypass_secret_scan() -> None:
    labels = [f"{number:02d}" for number in range(1, 22)]
    labels[8], labels[9] = labels[9], labels[8]
    controls = "".join(
        f'<button class="player-episodes__item">{label}</button>'
        for label in labels
    )

    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(
            f'<main><div class="player-episodes__row">{controls}</div></main>'.encode()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ("button", "a"))
async def test_fragmented_credential_inside_one_episode_control_is_rejected(
    control: str,
) -> None:
    fragments = "".join(f"<span>{number:02x}</span>" for number in range(21))

    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(f"<main><{control}>{fragments}</{control}></main>".encode())


@pytest.mark.asyncio
async def test_rejects_cookie_after_model_projection_limit() -> None:
    dom = (
        "<main>" + ("公开页面内容" * 50_000) + '<p>{"password":"tail-credential-value"}</p></main>'
    ).encode()

    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(dom)


@pytest.mark.asyncio
async def test_redacts_public_email_after_model_projection_limit_without_failing() -> None:
    screenshot = png_bytes()
    dom = (
        "<main>" + ("公开页面内容" * 50_000) + "<p>contact tail-private@example.com</p></main>"
    ).encode()
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
    )

    evidence = await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore(
            {
                artifacts[0].storage_path: screenshot,
                artifacts[1].storage_path: dom,
            }
        ),
    ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")

    assert "tail-private@example.com" not in evidence.dom_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phone",
    (
        "010-12345678",
        "400-123-4567",
        "+86 10 1234 5678",
        "+1 (212) 555-0198",
    ),
)
async def test_redacts_contextual_public_phone_and_continues(phone: str) -> None:
    evidence = await load_dom_only(f"<main>联系电话：{phone}</main>".encode())

    assert phone not in evidence.dom_text
    assert "[redacted-phone]" in evidence.dom_text


@pytest.mark.asyncio
async def test_rejects_credential_keyword_split_across_inline_nodes() -> None:
    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(b"<main><span>pass</span><span>word: credential-value</span></main>")


@pytest.mark.asyncio
async def test_redacts_contextual_phone_split_across_inline_nodes() -> None:
    evidence = await load_dom_only(
        "<main><span>客服电话：</span><span>400-123-4567</span><p>公开正文</p></main>".encode()
    )

    assert "400-123-4567" not in evidence.dom_text
    assert "[redacted-contact]" in evidence.dom_text
    assert "公开正文" in evidence.dom_text


@pytest.mark.asyncio
async def test_redacts_email_split_across_inline_nodes() -> None:
    evidence = await load_dom_only(
        b"<main><span>public@</span><span>example.com</span><p>public body</p></main>"
    )

    assert "public@" not in evidence.dom_text
    assert "example.com" not in evidence.dom_text
    assert "[redacted-contact]" in evidence.dom_text
    assert "public body" in evidence.dom_text


@pytest.mark.asyncio
async def test_does_not_redact_date_or_plain_catalog_number_as_phone() -> None:
    evidence = await load_dom_only(
        "<main>发布日期 2026-08-31，目录编号 010-12345678</main>".encode()
    )

    assert "2026-08-31" in evidence.dom_text
    assert "目录编号 010-12345678" in evidence.dom_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dom",
    (
        b'<main><input type="email" value="private@example.com"></main>',
        '<main><input type="text" placeholder="联系 13812345678"></main>'.encode(),
        b'<main><input value="private@example.com"></main>',
        b'<main><input type="TEXT" value="private@example.com"></main>',
        b'<main><input type="custom" value="private@example.com"></main>',
    ),
)
async def test_drops_public_contact_form_values_without_blocking_assessment(
    dom: bytes,
) -> None:
    evidence = await load_dom_only(dom)

    assert "private@example.com" not in evidence.dom_text
    assert "13812345678" not in evidence.dom_text
    assert "secret-value" not in evidence.dom_text


@pytest.mark.asyncio
async def test_rejects_visible_form_credential_without_visible_text() -> None:
    with pytest.raises(RuntimeError, match="SENSITIVE_CONTENT_PRESENT"):
        await load_dom_only(
            b'<main><input type="password" placeholder="Bearer secret-value"></main>'
        )


@pytest.mark.asyncio
async def test_masked_password_value_is_dropped_without_blocking_screenshot() -> None:
    evidence = await load_dom_only(
        b'<main><input type="password" value="Bearer secret-value">safe</main>'
    )

    assert "secret-value" not in evidence.dom_text
    assert "safe" in evidence.dom_text


@pytest.mark.asyncio
async def test_rejects_artifacts_from_different_capture() -> None:
    screenshot = png_bytes()
    dom = b"<main>ok</main>"
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored(
            "dom",
            "dom",
            dom,
            capture_id="capture-other",
            content_type="text/html",
        ),
    )

    with pytest.raises(RuntimeError, match="SAME_CAPTURE"):
        await VerifiedWebAssessmentEvidenceLoader(
            metadata_reader=MetadataReader(artifacts),
            artifact_store=ArtifactStore(
                {
                    item.storage_path: payload
                    for item, payload in zip(
                        artifacts,
                        (screenshot, dom),
                        strict=True,
                    )
                }
            ),
        ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")


@pytest.mark.asyncio
async def test_rejects_wrong_size_png_and_checksum_mismatch() -> None:
    screenshot = png_bytes(size=(1280, 720))
    dom = b"<main>ok</main>"
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
    )
    store = ArtifactStore(
        {
            artifacts[0].storage_path: screenshot,
            artifacts[1].storage_path: dom,
        }
    )
    with pytest.raises(RuntimeError, match="SCREENSHOT_DIMENSIONS"):
        await VerifiedWebAssessmentEvidenceLoader(
            metadata_reader=MetadataReader(artifacts),
            artifact_store=store,
        ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")


@pytest.mark.asyncio
async def test_self_closing_dropped_tag_does_not_hide_following_dom() -> None:
    screenshot = png_bytes()
    dom = b"<script/><main class='content-detail'>safe</main>"
    artifacts = (
        stored("shot", "screenshot", screenshot, content_type="image/png"),
        stored("dom", "dom", dom, content_type="text/html"),
    )

    evidence = await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(artifacts),
        artifact_store=ArtifactStore(
            {
                artifacts[0].storage_path: screenshot,
                artifacts[1].storage_path: dom,
            }
        ),
    ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")

    assert "script" not in evidence.dom_text
    assert "content-detail" in evidence.dom_text


@pytest.mark.asyncio
async def test_rejects_checksum_mismatch() -> None:
    good_screenshot = png_bytes()
    dom = b"<main>ok</main>"

    corrupted = (
        stored("shot", "screenshot", good_screenshot, content_type="image/png"),
        stored("dom", "dom", b"x" * len(dom), content_type="text/html"),
    )
    with pytest.raises(RuntimeError, match="CHECKSUM"):
        await VerifiedWebAssessmentEvidenceLoader(
            metadata_reader=MetadataReader(corrupted),
            artifact_store=ArtifactStore(
                {
                    corrupted[0].storage_path: good_screenshot,
                    corrupted[1].storage_path: dom,
                }
            ),
        ).load(screenshot_artifact_id="shot", dom_artifact_id="dom")


@pytest.mark.parametrize(
    "href",
    (
        "/article/aB7kP9qR2sT4uV6wX8yZ0mN1",
        "/asset/QWxpb3RoV2ViVjJUb2tlbjEyMw==",
        "/redirect/aB7kP9qR%2FsT4uV6wX8yZ0mN1%2B",
    ),
)
def test_href_sanitizer_redacts_short_and_percent_encoded_high_entropy_tokens(
    href: str,
) -> None:
    sanitized = _safe_attribute_value("href", href)

    assert "[redacted-segment]" in sanitized
    assert "Alioth" not in sanitized


def test_href_sanitizer_preserves_human_readable_slug() -> None:
    assert _safe_attribute_value("href", "/article/web-exploration-report") == (
        "/article/web-exploration-report"
    )


@pytest.mark.parametrize(
    "href",
    (
        "/redirect/Bearer%20secret-value",
        "/redirect/abcdefghijkl%2Emnopqrstuvwx%2Eyz0123456789",
        "/redirect/Bearer%25252520secret-value",
    ),
)
def test_href_sanitizer_redacts_percent_encoded_credentials(href: str) -> None:
    sanitized = _safe_attribute_value("href", href)

    assert sanitized == "/redirect/[redacted-segment]"
