"""Web 只读判定模型消费的实际证据载荷合同。"""

from __future__ import annotations

import hashlib
import math
import re
import struct
import zlib
from bisect import bisect_right
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import unquote, urlsplit

_MAX_SCREENSHOT_BYTES = 16 * 1024 * 1024
_WEB_FULL_PAGE_SUPPLEMENTAL_MIN_WIDTH = 1920
_WEB_FULL_PAGE_SUPPLEMENTAL_MIN_HEIGHT = 1080
_WEB_FULL_PAGE_SUPPLEMENTAL_MAX_DIMENSION = 32768
_WEB_FULL_PAGE_SUPPLEMENTAL_MAX_PIXELS = 100_000_000
_MAX_DOM_BYTES = 2 * 1024 * 1024
_MAX_DOM_CHARACTERS = 240_000
_MAX_VISIBLE_TEXT_BYTES = 512 * 1024
_MAX_VISIBLE_TEXT_CHARACTERS = 120_000
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PUBLIC_EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_PUBLIC_MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_PUBLIC_CONTEXT_PHONE_PATTERN = re.compile(
    r"(?i)(?P<label>(?:联系电话|客服电话|客服热线|电话|热线|"
    r"phone|telephone|tel|contact)\s*[:：]?\s*)"
    r"(?P<number>\+?\d[\d\s().-]{6,}\d)"
)
_PUBLIC_CONTACT_PATTERNS = (
    _PUBLIC_EMAIL_PATTERN,
    _PUBLIC_MOBILE_PATTERN,
    _PUBLIC_CONTEXT_PHONE_PATTERN,
)
_HEX_SECRET_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])")
_EPISODE_MARKER_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:ep|eps|episode|episodes|剧集)(?![A-Za-z0-9])"
)


@dataclass(frozen=True, kw_only=True, slots=True)
class StoredWebAssessmentArtifact:
    """已由数据库限定为同一受信 Capture 的证据元数据。"""

    artifact_id: str
    artifact_type: str
    task_id: str
    capture_id: str
    storage_path: str
    content_type: str
    checksum: str
    size_bytes: int
    parent_capture_id: str | None = None
    capture_role: str = "after_action"
    capture_purpose: str | None = None
    supplemental_phase_protocol_version: str | None = None


class WebAssessmentArtifactMetadataReader(Protocol):
    async def read_many(
        self,
        *,
        artifact_ids: tuple[str, ...],
    ) -> tuple[StoredWebAssessmentArtifact, ...]: ...


class WebAssessmentArtifactStore(Protocol):
    async def read(self, storage_path: str) -> bytes: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class WebAssessmentEvidence:
    """由受信仓储加载并完成类型校验的截图、DOM 与可见文本。"""

    screenshot_artifact_id: str
    screenshot_bytes: bytes
    screenshot_media_type: str
    dom_artifact_id: str
    dom_text: str
    visible_text_artifact_id: str | None = None
    visible_text: str | None = None
    redaction_categories: tuple[str, ...] = ()
    supplemental_screenshot_artifact_ids: tuple[str, ...] = ()
    supplemental_screenshot_bytes: tuple[bytes, ...] = ()
    supplemental_screenshot_media_types: tuple[str, ...] = ()
    supplemental_evidence_gap_reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("screenshot_artifact_id", "dom_artifact_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} 不能为空")
        if not isinstance(self.screenshot_bytes, bytes) or not self.screenshot_bytes:
            raise ValueError("screenshot_bytes 不能为空")
        if self.screenshot_media_type != "image/png":
            raise ValueError("Web 判定截图必须为 image/png")
        if not isinstance(self.dom_text, str) or not self.dom_text.strip():
            raise ValueError("dom_text 不能为空")
        if (self.visible_text_artifact_id is None) != (self.visible_text is None):
            raise ValueError("visible_text_artifact_id 与 visible_text 必须同时提供")
        if self.visible_text_artifact_id is not None and (
            not isinstance(self.visible_text_artifact_id, str)
            or not self.visible_text_artifact_id.strip()
            or not isinstance(self.visible_text, str)
        ):
            raise ValueError("visible_text 证据无效")
        supplemental_fields = (
            self.supplemental_screenshot_artifact_ids,
            self.supplemental_screenshot_bytes,
            self.supplemental_screenshot_media_types,
        )
        if (
            any(not isinstance(value, tuple) for value in supplemental_fields)
            or len(set(map(len, supplemental_fields))) != 1
            or any(
                not isinstance(item, str) or not item.strip()
                for item in self.supplemental_screenshot_artifact_ids
            )
            or any(
                not isinstance(item, bytes) or not item
                for item in self.supplemental_screenshot_bytes
            )
            or any(item != "image/png" for item in self.supplemental_screenshot_media_types)
        ):
            raise ValueError("supplemental 截图证据无效")
        if not isinstance(self.supplemental_evidence_gap_reason_codes, tuple) or any(
            not isinstance(item, str) or not item.strip()
            for item in self.supplemental_evidence_gap_reason_codes
        ):
            raise ValueError("supplemental 证据缺口无效")


class WebAssessmentEvidenceLoader(Protocol):
    """按受信 artifact ID 闭集加载模型可读证据。"""

    async def load(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        visible_text_artifact_id: str | None = None,
        supplemental_screenshot_artifact_ids: tuple[str, ...] = (),
    ) -> WebAssessmentEvidence: ...


class VerifiedWebAssessmentEvidenceLoader:
    """读取实际证据并在进入 LLM 前执行归属、完整性和脱敏校验。"""

    def __init__(
        self,
        *,
        metadata_reader: WebAssessmentArtifactMetadataReader,
        artifact_store: WebAssessmentArtifactStore,
    ) -> None:
        self._metadata_reader = metadata_reader
        self._artifact_store = artifact_store

    async def validate_same_capture_artifacts(
        self,
        *,
        artifact_types: Mapping[str, str],
    ) -> tuple[str, str]:
        """验证模型引用闭集属于同一 Task/Capture，且制品类型未漂移。"""

        expected = dict(artifact_types)
        if not expected or any(
            not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or not isinstance(artifact_type, str)
            or not artifact_type.strip()
            for artifact_id, artifact_type in expected.items()
        ):
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_IDS_INVALID")
        artifacts = await self._metadata_reader.read_many(
            artifact_ids=tuple(expected),
        )
        if len(artifacts) != len(expected) or {item.artifact_id for item in artifacts} != set(
            expected
        ):
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_SET_MISMATCH")
        if any(item.artifact_type != expected[item.artifact_id] for item in artifacts):
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_TYPE_MISMATCH")
        ownership = {(item.task_id, item.capture_id) for item in artifacts}
        if len(ownership) != 1:
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACTS_NOT_SAME_CAPTURE")
        return next(iter(ownership))

    async def load(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        visible_text_artifact_id: str | None = None,
        supplemental_screenshot_artifact_ids: tuple[str, ...] = (),
    ) -> WebAssessmentEvidence:
        primary_expected = {
            screenshot_artifact_id: "screenshot",
            dom_artifact_id: "dom",
        }
        if visible_text_artifact_id is not None:
            primary_expected[visible_text_artifact_id] = "visible_text"
        if (
            len(primary_expected) not in {2, 3}
            or any(not isinstance(item, str) or not item.strip() for item in primary_expected)
            or not isinstance(supplemental_screenshot_artifact_ids, tuple)
            or len(set(supplemental_screenshot_artifact_ids))
            != len(supplemental_screenshot_artifact_ids)
            or any(
                not isinstance(item, str) or not item.strip()
                for item in supplemental_screenshot_artifact_ids
            )
            or set(primary_expected).intersection(supplemental_screenshot_artifact_ids)
        ):
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_IDS_INVALID")
        artifacts = await self._metadata_reader.read_many(artifact_ids=tuple(primary_expected))
        by_id = {item.artifact_id: item for item in artifacts}
        primary_rows = [item for item in artifacts if item.artifact_id in primary_expected]
        if set(item.artifact_id for item in primary_rows) != set(primary_expected) or len(
            primary_rows
        ) != len(primary_expected):
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_SET_MISMATCH")
        if any(
            by_id[artifact_id].artifact_type != artifact_type
            for artifact_id, artifact_type in primary_expected.items()
        ):
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_TYPE_MISMATCH")
        primary_artifacts = tuple(by_id[artifact_id] for artifact_id in primary_expected)
        ownership = {(item.task_id, item.capture_id) for item in primary_artifacts}
        if len(ownership) != 1:
            raise RuntimeError("WEB_ASSESSMENT_ARTIFACTS_NOT_SAME_CAPTURE")

        payloads: dict[str, bytes] = {}
        for artifact_id in primary_expected:
            artifact = by_id[artifact_id]
            payload = await self._artifact_store.read(artifact.storage_path)
            _verify_payload(artifact, payload)
            payloads[artifact_id] = payload

        screenshot = payloads[screenshot_artifact_id]
        _validate_png(screenshot)
        primary_task_id, primary_capture_id = next(iter(ownership))
        supplemental_screenshots: list[bytes] = []
        accepted_supplemental_ids: list[str] = []
        supplemental_gap_reason_codes: list[str] = []
        for artifact_id in supplemental_screenshot_artifact_ids:
            try:
                matching_rows = await self._metadata_reader.read_many(artifact_ids=(artifact_id,))
            except Exception as exc:  # noqa: BLE001 - 可选长图元数据缺失不能破坏主证据。
                supplemental_gap_reason_codes.append(
                    str(exc)
                    if isinstance(exc, RuntimeError) and str(exc).startswith("WEB_")
                    else "WEB_ASSESSMENT_SUPPLEMENTAL_UNAVAILABLE"
                )
                continue
            if len(matching_rows) != 1:
                supplemental_gap_reason_codes.append("WEB_ASSESSMENT_ARTIFACT_SET_MISMATCH")
                continue
            artifact = matching_rows[0]
            try:
                if artifact.artifact_type != "screenshot":
                    raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_TYPE_MISMATCH")
                payload = await self._artifact_store.read(artifact.storage_path)
                _verify_payload(artifact, payload)
                _validate_supplemental_lineage(
                    artifact,
                    primary_task_id=primary_task_id,
                    primary_capture_id=primary_capture_id,
                )
                _validate_full_page_supplemental_png(artifact, payload)
            except Exception as exc:  # noqa: BLE001 - 长图失败只能降级，主证据仍须可用。
                supplemental_gap_reason_codes.append(
                    str(exc)
                    if isinstance(exc, RuntimeError) and str(exc).startswith("WEB_")
                    else "WEB_ASSESSMENT_SUPPLEMENTAL_UNAVAILABLE"
                )
                continue
            accepted_supplemental_ids.append(artifact_id)
            supplemental_screenshots.append(payload)
        dom_text = _decode_text(
            by_id[dom_artifact_id],
            payloads[dom_artifact_id],
            maximum_bytes=_MAX_DOM_BYTES,
        )
        visible_text_raw = None
        if visible_text_artifact_id is not None:
            visible_text_raw = _decode_text(
                by_id[visible_text_artifact_id],
                payloads[visible_text_artifact_id],
                maximum_bytes=_MAX_VISIBLE_TEXT_BYTES,
            )
        sanitized_dom_full, dom_secret_content_present, redaction_categories = (
            _sanitize_dom_for_assessment(dom_text)
        )
        if dom_secret_content_present or (
            visible_text_raw is not None and _contains_secret_content(visible_text_raw)
        ):
            # 公开邮箱和手机号只做脱敏；真正凭据仍拒绝整次证据送模。
            # script/style/template 已被删除，不能用其隐藏配置误拒绝页面。
            raise RuntimeError("WEB_ASSESSMENT_SENSITIVE_CONTENT_PRESENT")
        sanitized_dom = sanitized_dom_full[:_MAX_DOM_CHARACTERS].strip()
        visible_text = (
            _redact_text(visible_text_raw)[:_MAX_VISIBLE_TEXT_CHARACTERS]
            if visible_text_raw is not None
            else None
        )

        return WebAssessmentEvidence(
            screenshot_artifact_id=screenshot_artifact_id,
            screenshot_bytes=screenshot,
            screenshot_media_type="image/png",
            dom_artifact_id=dom_artifact_id,
            dom_text=sanitized_dom,
            visible_text_artifact_id=visible_text_artifact_id,
            visible_text=visible_text,
            redaction_categories=redaction_categories,
            supplemental_screenshot_artifact_ids=tuple(accepted_supplemental_ids),
            supplemental_screenshot_bytes=tuple(supplemental_screenshots),
            supplemental_screenshot_media_types=("image/png",) * len(supplemental_screenshots),
            supplemental_evidence_gap_reason_codes=tuple(supplemental_gap_reason_codes),
        )


def _verify_payload(artifact: StoredWebAssessmentArtifact, payload: bytes) -> None:
    if not isinstance(payload, bytes):
        raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_PAYLOAD_INVALID")
    if len(payload) != artifact.size_bytes:
        raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_SIZE_MISMATCH")
    checksum = f"sha256:{hashlib.sha256(payload).hexdigest()}"
    if checksum != artifact.checksum:
        raise RuntimeError("WEB_ASSESSMENT_ARTIFACT_CHECKSUM_MISMATCH")


def _validate_png(payload: bytes) -> None:
    if len(payload) > _MAX_SCREENSHOT_BYTES:
        raise RuntimeError("WEB_ASSESSMENT_SCREENSHOT_TOO_LARGE")
    if len(payload) < 24 or payload[:8] != _PNG_SIGNATURE or payload[12:16] != b"IHDR":
        raise RuntimeError("WEB_ASSESSMENT_SCREENSHOT_NOT_PNG")
    width, height = struct.unpack(">II", payload[16:24])
    if (width, height) != (1920, 1080):
        raise RuntimeError("WEB_ASSESSMENT_SCREENSHOT_DIMENSIONS_INVALID")


def _validate_supplemental_lineage(
    artifact: StoredWebAssessmentArtifact,
    *,
    primary_task_id: str,
    primary_capture_id: str,
) -> None:
    if artifact.task_id != primary_task_id:
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_NOT_SAME_TASK")
    if artifact.parent_capture_id != primary_capture_id:
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_NOT_DIRECT_CHILD")
    if artifact.capture_role != "supplemental":
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_ROLE_INVALID")
    if artifact.capture_purpose != "web_full_page_supplemental":
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_PURPOSE_INVALID")
    if artifact.supplemental_phase_protocol_version != "web-full-page-supplemental.v1":
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_PHASE_INVALID")


def validate_web_full_page_supplemental_png(
    payload: bytes,
    *,
    content_type: str = "image/png",
) -> None:
    """按采集端与读取端共享的 Web 长图合同校验完整 PNG。"""

    if content_type.split(";", 1)[0].strip().casefold() != "image/png":
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_NOT_PNG")
    if len(payload) > _MAX_SCREENSHOT_BYTES:
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_TOO_LARGE")
    if len(payload) < 24 or payload[:8] != _PNG_SIGNATURE or payload[12:16] != b"IHDR":
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_NOT_PNG")
    if not _is_complete_png(payload):
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_NOT_PNG")
    width, height = struct.unpack(">II", payload[16:24])
    if (
        width > _WEB_FULL_PAGE_SUPPLEMENTAL_MAX_DIMENSION
        or height > _WEB_FULL_PAGE_SUPPLEMENTAL_MAX_DIMENSION
    ):
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_DIMENSION_LIMIT_EXCEEDED")
    if width * height > _WEB_FULL_PAGE_SUPPLEMENTAL_MAX_PIXELS:
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_PIXELS_EXCEEDED")
    # 页面横向溢出时，浏览器整页 PNG 可以宽于固定的 1920px 视口。
    if width < _WEB_FULL_PAGE_SUPPLEMENTAL_MIN_WIDTH:
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_WIDTH_INVALID")
    if height < _WEB_FULL_PAGE_SUPPLEMENTAL_MIN_HEIGHT:
        raise RuntimeError("WEB_ASSESSMENT_SUPPLEMENTAL_HEIGHT_INVALID")


def _validate_full_page_supplemental_png(
    artifact: StoredWebAssessmentArtifact, payload: bytes
) -> None:
    validate_web_full_page_supplemental_png(
        payload,
        content_type=artifact.content_type,
    )


def _is_complete_png(payload: bytes) -> bool:
    """有界验证 PNG 分块、CRC 和必要图像结束标记，不解析像素内容。"""
    offset = len(_PNG_SIGNATURE)
    has_ihdr = False
    has_idat = False
    while offset < len(payload):
        if offset + 12 > len(payload):
            return False
        chunk_size = struct.unpack(">I", payload[offset : offset + 4])[0]
        chunk_type = payload[offset + 4 : offset + 8]
        chunk_end = offset + 12 + chunk_size
        if chunk_end > len(payload):
            return False
        chunk_payload = payload[offset + 8 : offset + 8 + chunk_size]
        expected_crc = struct.unpack(">I", payload[offset + 8 + chunk_size : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(chunk_payload, actual_crc) & 0xFFFFFFFF
        if actual_crc != expected_crc:
            return False
        if not has_ihdr:
            if chunk_type != b"IHDR" or chunk_size != 13:
                return False
            has_ihdr = True
        elif chunk_type == b"IHDR":
            return False
        if chunk_type == b"IDAT":
            has_idat = True
        if chunk_type == b"IEND":
            return chunk_size == 0 and has_ihdr and has_idat and chunk_end == len(payload)
        offset = chunk_end
    return False


def _decode_text(
    artifact: StoredWebAssessmentArtifact,
    payload: bytes,
    *,
    maximum_bytes: int,
) -> str:
    if len(payload) > maximum_bytes:
        raise RuntimeError("WEB_ASSESSMENT_TEXT_TOO_LARGE")
    content_type = artifact.content_type.split(";", 1)[0].strip().casefold()
    allowed = {
        "dom": {"text/html", "application/xhtml+xml"},
        "visible_text": {"text/plain"},
    }[artifact.artifact_type]
    if content_type not in allowed:
        raise RuntimeError("WEB_ASSESSMENT_TEXT_CONTENT_TYPE_INVALID")
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError("WEB_ASSESSMENT_TEXT_NOT_UTF8") from exc


@dataclass(slots=True)
class _EpisodeControl:
    group_id: int | None
    tag: str
    semantic: bool
    text_parts: list[str]


class _SafeDomParser(HTMLParser):
    """只保留页面结构与有限无敏感属性，删除脚本和表单值。"""

    _DROP_TAGS = frozenset({"script", "style", "noscript", "template"})
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "dd",
            "div",
            "dl",
            "dt",
            "fieldset",
            "figcaption",
            "figure",
            "footer",
            "form",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tbody",
            "td",
            "tfoot",
            "th",
            "thead",
            "tr",
            "ul",
        }
    )
    _EPISODE_GROUP_TAGS = frozenset({"div", "nav", "ol", "section", "ul"})
    _VOID_TAGS = frozenset({
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    })
    _ALLOWED_ATTRIBUTES = frozenset(
        {
            "id",
            "class",
            "role",
            "aria-label",
            "aria-modal",
            "aria-hidden",
            "type",
            "name",
            "href",
            "rel",
            "alt",
            "title",
        }
    )
    _VISIBLE_VALUE_INPUT_TYPES = frozenset(
        {
            "",
            "button",
            "date",
            "datetime-local",
            "email",
            "month",
            "number",
            "reset",
            "search",
            "submit",
            "tel",
            "text",
            "time",
            "url",
            "week",
        }
    )
    _PLACEHOLDER_INPUT_TYPES = frozenset(
        {"", "email", "number", "password", "search", "tel", "text", "url"}
    )
    _KNOWN_INPUT_TYPES = frozenset(
        {
            "button",
            "checkbox",
            "color",
            "date",
            "datetime-local",
            "email",
            "file",
            "hidden",
            "image",
            "month",
            "number",
            "password",
            "radio",
            "range",
            "reset",
            "search",
            "submit",
            "tel",
            "text",
            "time",
            "url",
            "week",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.rendered_text_segments: list[tuple[str, int | None]] = []
        self._segment_control_ids: list[int | None] = []
        self._open_elements: list[tuple[str, int | None, int | None]] = []
        self._episode_controls: dict[int, _EpisodeControl] = {}
        self._next_group_id = 0
        self._next_control_id = 0
        self._drop_depth = 0
        self.sensitive_content_present = False
        self.redaction_categories: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self._DROP_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if normalized_tag in self._BLOCK_TAGS:
            self._append_rendered_separator()
        normalized_attrs = [(raw_name.casefold(), raw_value) for raw_name, raw_value in attrs]
        self._track_open_tag(normalized_tag, normalized_attrs)
        if _is_public_preview_image_identifier(normalized_tag, normalized_attrs):
            # 资源标识先脱敏，不能借成人声明或预览标记放行任何真实凭据。
            if any(
                _contains_secret_content(value)
                for name, value in normalized_attrs
                if name != "alt" and value
            ):
                self.sensitive_content_present = True
            normalized_attrs = [
                (name, "[redacted-image-resource-id]" if name == "alt" else value)
                for name, value in normalized_attrs
            ]
            self.redaction_categories.add("image_alt_resource_identifier")
        self._record_rendered_form_sensitive_fact(
            tag=normalized_tag,
            attrs=normalized_attrs,
        )
        safe_attrs: list[str] = []
        for name, raw_value in normalized_attrs:
            if name not in self._ALLOWED_ATTRIBUTES or raw_value is None:
                continue
            collapsed_value = " ".join(raw_value.split())
            if name in {"aria-label", "alt", "title"} and _contains_secret_content(collapsed_value):
                self.sensitive_content_present = True
            value = _safe_attribute_value(name, raw_value)
            if value:
                safe_attrs.append(f'{name}="{escape(value, quote=True)}"')
        suffix = f" {' '.join(safe_attrs)}" if safe_attrs else ""
        self.parts.append(f"<{normalized_tag}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in self._DROP_TAGS or self._drop_depth:
            return
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        normalized_tag = tag.casefold()
        if normalized_tag in self._DROP_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if not self._drop_depth:
            self.parts.append(f"</{normalized_tag}>")
            if normalized_tag in self._BLOCK_TAGS:
                self._append_rendered_separator()
            for index in range(len(self._open_elements) - 1, -1, -1):
                if self._open_elements[index][0] == normalized_tag:
                    del self._open_elements[index:]
                    break

    def handle_data(self, data: str) -> None:
        if self._drop_depth or not data:
            return
        if not data.strip():
            self.rendered_text_segments.append((data, None))
            self._segment_control_ids.append(self._active_control_id())
            return
        part_index = len(self.parts)
        redacted = _redact_text(data)
        if _contains_secret_content(data):
            self.sensitive_content_present = True
        self.parts.append(escape(redacted, quote=False))
        self.rendered_text_segments.append((data, part_index))
        control_id = self._active_control_id()
        self._segment_control_ids.append(control_id)
        if control_id is not None:
            self._episode_controls[control_id].text_parts.append(data)

    def rendered_visible_text(self) -> str:
        """按浏览器内联邻接、块级分隔语义构造完整可见文本投影。"""

        return "".join(text for text, _part_index in self.rendered_text_segments)

    def sensitive_scan_text(self) -> str:
        """仅排除已证明属于同一剧集组的连续短序号匹配。"""

        projection = self.rendered_visible_text()
        if not _HEX_SECRET_PATTERN.search(projection):
            return projection
        segments: list[tuple[int, int, int | None]] = []
        offset = 0
        for (text, _part_index), control_id in zip(
            self.rendered_text_segments, self._segment_control_ids, strict=True
        ):
            end = offset + len(text)
            segments.append((offset, end, control_id))
            offset = end
        segment_ends = [end for _start, end, _control_id in segments]

        def redact_episode_match(match: re.Match[str]) -> str:
            index = bisect_right(segment_ends, match.start())
            touched = []
            while index < len(segments) and segments[index][0] < match.end():
                touched.append(segments[index])
                index += 1
            if not touched or touched[0][0] != match.start() or touched[-1][1] != match.end():
                return match.group()
            control_ids = []
            for _start, _end, control_id in touched:
                if control_id is None:
                    return match.group()
                if not control_ids or control_ids[-1] != control_id:
                    control_ids.append(control_id)
            if len(control_ids) < 16 or len(control_ids) != len(set(control_ids)):
                return match.group()
            controls = [self._episode_controls[control_id] for control_id in control_ids]
            group_id = controls[0].group_id
            if (
                group_id is None
                or any(
                    control.group_id != group_id
                    or control.tag != controls[0].tag
                    or not control.semantic
                    for control in controls
                )
            ):
                return match.group()
            labels = ["".join(control.text_parts).strip() for control in controls]
            if not all(re.fullmatch(r"[0-9]{2}", label) for label in labels):
                return match.group()
            numbers = [int(label) for label in labels]
            if any(right != left + 1 for left, right in zip(numbers, numbers[1:], strict=False)):
                return match.group()
            return "[episode-sequence]"

        return _HEX_SECRET_PATTERN.sub(redact_episode_match, projection)

    def _track_open_tag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag in self._VOID_TAGS:
            return
        by_name = dict(attrs)
        group_id = self._open_elements[-1][1] if self._open_elements else None
        control_id = self._active_control_id()
        if tag in self._EPISODE_GROUP_TAGS and _EPISODE_MARKER_PATTERN.search(
            " ".join((by_name.get("class") or "", by_name.get("id") or ""))
        ):
            self._next_group_id += 1
            group_id = self._next_group_id
        if tag in {"a", "button"}:
            self._next_control_id += 1
            control_id = self._next_control_id
            self._episode_controls[control_id] = _EpisodeControl(
                group_id=group_id,
                tag=tag,
                semantic=bool(_EPISODE_MARKER_PATTERN.search(
                    " ".join((by_name.get("class") or "", by_name.get("id") or ""))
                )),
                text_parts=[],
            )
        self._open_elements.append((tag, group_id, control_id))

    def _active_control_id(self) -> int | None:
        return self._open_elements[-1][2] if self._open_elements else None

    def redact_cross_node_public_contacts(self) -> None:
        """把跨内联文本节点命中的公开联系方式回写为保守脱敏占位。"""

        projection = self.rendered_visible_text()
        redacted_part_indexes: set[int] = set()
        offset = 0
        indexed_segments: list[tuple[int, int, int | None]] = []
        for text, part_index in self.rendered_text_segments:
            end = offset + len(text)
            indexed_segments.append((offset, end, part_index))
            offset = end
        for pattern in _PUBLIC_CONTACT_PATTERNS:
            for match in pattern.finditer(projection):
                touched = {
                    part_index
                    for start, end, part_index in indexed_segments
                    if part_index is not None and start < match.end() and end > match.start()
                }
                if len(touched) > 1:
                    redacted_part_indexes.update(touched)
        for part_index in redacted_part_indexes:
            self.parts[part_index] = "[redacted-contact]"

    def _append_rendered_separator(self) -> None:
        if not self.rendered_text_segments or not self.rendered_text_segments[-1][0].endswith("\n"):
            self.rendered_text_segments.append(("\n", None))
            self._segment_control_ids.append(None)

    def _record_rendered_form_sensitive_fact(
        self,
        *,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        """在丢弃表单值前记录可能真实渲染到截图的敏感事实。"""

        by_name = {name: value for name, value in attrs if value is not None}
        raw_input_type = " ".join(by_name.get("type", "text").split()).casefold()
        input_type = raw_input_type if raw_input_type in self._KNOWN_INPUT_TYPES else "text"
        inspected_names: set[str] = set()
        if tag == "input":
            if input_type in self._VISIBLE_VALUE_INPUT_TYPES:
                inspected_names.add("value")
            if input_type in self._PLACEHOLDER_INPUT_TYPES:
                inspected_names.add("placeholder")
        elif tag == "textarea":
            inspected_names.add("placeholder")
        for name in inspected_names:
            raw_value = by_name.get(name)
            if raw_value is None:
                continue
            collapsed_value = " ".join(raw_value.split())
            if _contains_secret_content(collapsed_value):
                self.sensitive_content_present = True


def _safe_attribute_value(name: str, value: str) -> str:
    value = " ".join(value.split())[:500]
    if name != "href":
        return _redact_text(value)
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"", "http", "https"}:
        return ""
    path = parsed.path or "/"
    return "/".join(_safe_href_path_segment(segment) for segment in path.split("/")) or "/"


def _safe_href_path_segment(value: str) -> str:
    decoded = value
    for _attempt in range(3):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if unquote(decoded) != decoded:
        return "[redacted-segment]"
    if _redact_text(decoded) != decoded or _is_high_entropy_token(decoded):
        return "[redacted-segment]"
    return value


def _is_public_preview_image_identifier(
    tag: str,
    attrs: list[tuple[str, str | None]],
) -> bool:
    """仅识别图片加载/预览节点上的纯十六进制资源标识，不认可其它凭据。"""
    by_name = dict(attrs)
    if (
        tag != "img"
        or len(by_name) != len(attrs)
        or not re.fullmatch(r"[A-Fa-f0-9]{32}", by_name.get("alt") or "")
        or "data-image-preview" not in by_name
        or "data-image-zoom" not in by_name
    ):
        return False
    try:
        resource = urlsplit(by_name.get("z-image-loader-url") or "")
        return (
            resource.scheme in {"http", "https"}
            and bool(resource.hostname)
            and resource.username is None
            and resource.password is None
            and not resource.query
            and not resource.fragment
        )
    except ValueError:
        return False


def _sanitize_dom_for_assessment(value: str) -> tuple[str, bool, tuple[str, ...]]:
    parser = _SafeDomParser()
    parser.feed(value)
    parser.close()
    combined_visible_text = parser.sensitive_scan_text()
    parser.redact_cross_node_public_contacts()
    sanitized = "".join(parser.parts).strip()
    if not sanitized:
        raise RuntimeError("WEB_ASSESSMENT_DOM_EMPTY_AFTER_SANITIZE")
    return (
        sanitized,
        (
            parser.sensitive_content_present
            or _contains_secret_content(combined_visible_text)
            or _contains_secret_content(sanitized)
        ),
        tuple(sorted(parser.redaction_categories)),
    )


def _sanitize_dom_with_sensitive_fact(value: str) -> tuple[str, bool]:
    sanitized, sensitive, _categories = _sanitize_dom_for_assessment(value)
    return sanitized, sensitive


def _sanitize_dom(value: str) -> str:
    """保留供局部校验使用的净化 DOM 文本投影。"""

    sanitized, _sensitive_content_present = _sanitize_dom_with_sensitive_fact(value)
    return sanitized


def _redact_text(value: str) -> str:
    redacted = _PUBLIC_EMAIL_PATTERN.sub("[redacted-email]", value)
    redacted = _PUBLIC_MOBILE_PATTERN.sub("[redacted-phone]", redacted)
    redacted = _PUBLIC_CONTEXT_PHONE_PATTERN.sub(
        r"\g<label>[redacted-phone]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(?<![A-Za-z0-9_-])[\"']?authorization[\"']?\s*[:=]\s*"
        r"[\"']?\s*(?:[A-Za-z][A-Za-z0-9_-]*\s+)?[^\s\"'<>;,}{]+",
        "authorization=[redacted-credential]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)(?<![A-Za-z0-9_-])[\"']?"
        r"(?:set-cookie|cookie|token|password|passwd|secret)[\"']?\s*[:=]\s*"
        r"[\"']?\s*[^\s\"'<>;,}{]+",
        "credential=[redacted-credential]",
        redacted,
    )
    redacted = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer [redacted]",
        redacted,
    )
    redacted = re.sub(
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\."
        r"[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])",
        "[redacted-jwt]",
        redacted,
    )
    return re.sub(
        r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])",
        "[redacted-high-entropy]",
        redacted,
    )


def _contains_secret_content(value: str) -> bool:
    patterns = (
        r"(?i)(?<![A-Za-z0-9_-])[\"']?authorization[\"']?\s*[:=]\s*"
        r"[\"']?\s*(?:[A-Za-z][A-Za-z0-9_-]*\s+)?[^\s\"'<>;,}{]+",
        r"(?i)(?<![A-Za-z0-9_-])[\"']?"
        r"(?:set-cookie|cookie|token|password|passwd|secret)[\"']?\s*[:=]\s*"
        r"[\"']?\s*[^\s\"'<>;,}{]+",
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+",
        r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\."
        r"[A-Za-z0-9_-]{12,}(?![A-Za-z0-9_-])",
        r"(?<![A-Za-z0-9])[A-Fa-f0-9]{32,}(?![A-Za-z0-9])",
    )
    return any(re.search(pattern, value) is not None for pattern in patterns)


class WebModelReasonRejected(ValueError):
    """固定拒绝类别，不携带不可信模型原文。"""

    def __init__(self, category: str):
        self.category = category
        super().__init__("reason 不能为空" if category == "empty_reason" else "reason 包含认证凭据")


def sanitize_web_model_reason(value: str) -> str:
    """把不可信模型理由收敛为可入账的单行脱敏文本。"""

    if not isinstance(value, str) or not value.strip():
        raise WebModelReasonRejected("empty_reason")
    decoded = value
    for _attempt in range(3):
        next_decoded = unquote(decoded)
        if next_decoded == decoded:
            break
        decoded = next_decoded
    if unquote(decoded) != decoded:
        raise WebModelReasonRejected("excessive_encoding")
    if _contains_secret_content(decoded):
        raise WebModelReasonRejected("credential_content")
    if _contains_high_entropy_content(decoded):
        raise WebModelReasonRejected("high_entropy_content")
    sanitized = " ".join(_redact_text(decoded).split())[:1000]
    if not sanitized:
        raise WebModelReasonRejected("empty_reason")
    return sanitized


def _contains_high_entropy_content(value: str) -> bool:
    candidates = re.findall(r"(?<![A-Za-z0-9_~+/=-])[A-Za-z0-9_~+/=-]{24,}", value)
    return any(_is_high_entropy_token(candidate) for candidate in candidates)


def _is_high_entropy_token(value: str) -> bool:
    decoded = unquote(value)
    if len(decoded) < 24 or not re.fullmatch(r"[A-Za-z0-9_~+/=-]+", decoded):
        return False
    if re.fullmatch(r"[A-Fa-f0-9]{24,}", decoded):
        return True
    frequencies = Counter(decoded)
    entropy = -sum(
        (count / len(decoded)) * math.log2(count / len(decoded)) for count in frequencies.values()
    )
    character_classes = sum(
        (
            any(character.islower() for character in decoded),
            any(character.isupper() for character in decoded),
            any(character.isdigit() for character in decoded),
            any(not character.isalnum() for character in decoded),
        )
    )
    unique_ratio = len(frequencies) / len(decoded)
    return entropy >= 3.5 and (character_classes >= 3 or (entropy >= 4.0 and unique_ratio >= 0.55))


__all__ = [
    "StoredWebAssessmentArtifact",
    "VerifiedWebAssessmentEvidenceLoader",
    "WebAssessmentArtifactMetadataReader",
    "WebAssessmentArtifactStore",
    "WebAssessmentEvidence",
    "WebAssessmentEvidenceLoader",
    "sanitize_web_model_reason",
    "validate_web_full_page_supplemental_png",
]
