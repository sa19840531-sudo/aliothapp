"""识别有正向图像依据的空白采集，避免把未进入业务页当作通过。"""

import re
from io import BytesIO
from typing import Any

from PIL import Image, UnidentifiedImageError

_CLOUDFLARE_ERROR_DESCRIPTIONS = {
    "500": "internal server error",
    "502": "bad gateway",
    "503": "service unavailable",
    "504": "gateway time-out",
    "520": "web server is returning an unknown error",
    "521": "web server is down",
    "522": "connection timed out",
    "523": "origin is unreachable",
    "524": "a timeout occurred",
    "525": "ssl handshake failed",
    "526": "invalid ssl certificate",
    "530": "origin dns error",
}
_CLOUDFLARE_ERROR_HOST = r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z0-9-]+(?::\d{1,5})?"


def _canonical_cloudflare_error_title(title: str) -> bool:
    """只接受源站故障页的完整规范标题，排错文章前后附加文字不匹配。"""
    prefix = re.fullmatch(
        rf"{_CLOUDFLARE_ERROR_HOST}\s*\|\s*(?P<code>5\d\d):\s*(?P<description>.+)",
        title,
    )
    if prefix is not None:
        return _CLOUDFLARE_ERROR_DESCRIPTIONS.get(prefix["code"]) == prefix["description"]
    suffix = re.fullmatch(
        rf"(?P<description>.+)\s*\|\s*{_CLOUDFLARE_ERROR_HOST}\s*\|\s*cloudflare",
        title,
    )
    return suffix is not None and suffix["description"].strip() in (
        _CLOUDFLARE_ERROR_DESCRIPTIONS.values()
    )


def target_document_unavailability_reason(*, title: str, visible_text: str) -> str | None:
    """识别明确的源站故障和防护挑战文档，业务正文里的错误词不触发。"""
    normalized_title = re.sub(r"\s+", " ", title).strip().casefold()
    text = re.sub(r"\s+", " ", visible_text).strip().casefold()
    cloudflare_error_title = _canonical_cloudflare_error_title(normalized_title)
    cloudflare_error_text = (
        "cloudflare" in text
        and (
            "ray id" in text
            or all(
                marker in text
                for marker in (
                    "browser",
                    "working",
                    "host",
                    "error",
                )
            )
        )
        and re.search(r"error(?:\s+code)?\s*[:：]?\s*5\d\d\b", text) is not None
    )
    if cloudflare_error_title and cloudflare_error_text:
        return "目标文档为 Cloudflare 源站错误页面，未获取被检业务正文"
    challenge_title = normalized_title.rstrip(". …") in {
        "just a moment",
        "attention required! | cloudflare",
        "checking your browser",
    }
    challenge_text = any(
        marker in text
        for marker in (
            "verifying you are human",
            "verify you are human",
            "checking your browser",
            "enable javascript and cookies",
            "performing security verification",
        )
    ) and any(marker in text for marker in ("cloudflare", "ray id", "security verification"))
    if challenge_title and challenge_text:
        return "目标文档为访问防护验证页面，尚未获取被检业务正文"
    gateway_title = re.fullmatch(
        r"(?:http(?:\s+error)?\s*)?"
        r"(?:502 bad gateway|503 service unavailable|504 gateway time-out)",
        normalized_title,
    )
    if gateway_title and normalized_title in text and re.search(r"\b(?:nginx|openresty)\b", text):
        return "目标文档为网关故障页面，未获取被检业务正文"
    return None


def web_document_facts(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """投影已持久化的正文分类及发布就绪事实，不用页型标签推断正文已获取。"""
    facts: dict[str, Any] = {}
    readiness = metadata.get("web_inspection_readiness")
    if isinstance(readiness, dict):
        facts["readiness"] = {
            key: readiness[key]
            for key in ("role", "status", "blocking_reasons")
            if key in readiness
        }
    assessment = metadata.get("web_page_type_assessment")
    if isinstance(assessment, dict):
        facts["classification"] = {
            key: assessment[key]
            for key in (
                "classification_status",
                "final_page_type",
                "needs_review",
                "reason_code",
            )
            if key in assessment
        }
        subject = assessment.get("subject_region_facts")
        if isinstance(subject, dict):
            facts["primary_subject"] = {
                key: subject[key]
                for key in (
                    "primary_subject_count",
                    "primary_subject_kind",
                    "primary_article_body_count",
                    "primary_article_text_length",
                    "primary_player_count",
                )
                if key in subject
            }
    return facts or None


def is_empty_observed_surface(
    *,
    visible_text: str,
    ui_snapshot: dict[str, Any] | None,
    image_payloads: tuple[tuple[str, bytes, str], ...],
) -> bool:
    """仅在主页面无文本、无可见元素且真实图像全为单色时确认空白。"""
    if visible_text.strip() or (ui_snapshot or {}).get("elements") or not image_payloads:
        return False
    for _, payload, _ in image_payloads:
        try:
            with Image.open(BytesIO(payload)) as image:
                extrema = image.convert("RGB").getextrema()
                if any(high - low > 2 for low, high in extrema):
                    return False
        except (OSError, ValueError, UnidentifiedImageError):
            # 解码失败由材料完整性链路处理，不能猜测成空白业务页。
            return False
    return True
