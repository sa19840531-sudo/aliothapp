"""从可信 Web 首屏快照提取有界 App 引导入口，不执行源页动作。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from services.api.app.application.web_ad_outbound_policy import AdOutboundDenied, validate_ad_url

_APP_SEMANTIC = re.compile(r"(?<![a-z])app(?![a-z])|客户端|安卓|苹果下载|应用下载|手机端", re.I)
_DOWNLOAD_SEMANTIC = re.compile(r"下载|download|获取|安装", re.I)
_RESOURCE_SUFFIX = re.compile(r"\.(?:apk|xapk|ipa|aab|dmg|exe|zip)$", re.I)
_AD_SEMANTIC = re.compile(r"广告|赞助|sponsor|\bad[_-]|[_-]ad\b|banner", re.I)
_ELEMENT_FIELDS = ("text", "aria_label", "alt", "title", "class_name", "id")


@dataclass(frozen=True, slots=True)
class WebGuideCandidate:
    candidate_id: str
    source_capture_id: str
    source_url: str
    destination_url: str
    label: str
    kind: str


def select_web_guide_candidates(
    *, source_capture_id: str, source_url: str, snapshot: Mapping[str, object],
    limit: int = 2,
) -> tuple[WebGuideCandidate, ...]:
    """入口文字与 URL 只产生候选，不能把来源页判为引导页。"""

    if not source_capture_id or not source_url or not 1 <= limit <= 2:
        return ()
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return ()
    pages: list[WebGuideCandidate] = []
    resources: list[WebGuideCandidate] = []
    seen: set[str] = set()
    for raw in elements:
        if not isinstance(raw, Mapping) or raw.get("visible") is False:
            continue
        href = raw.get("href")
        if not isinstance(href, str) or not href.strip():
            continue
        class_tokens = raw.get("class_tokens")
        classes = (
            " ".join(str(token) for token in class_tokens[:12])
            if isinstance(class_tokens, list) else ""
        )
        semantic = " ".join(str(raw.get(key) or "") for key in _ELEMENT_FIELDS)
        semantic = f"{semantic} {classes}"
        if raw.get("is_advertisement") is True or _AD_SEMANTIC.search(semantic):
            continue
        try:
            destination = validate_ad_url(urljoin(source_url, href), previous_url=source_url)
        except (AdOutboundDenied, ValueError):
            continue
        path = urlsplit(destination).path
        url_app_cue = bool(re.search(r"(?:^|[/_.-])app(?:[/_.-]|$)", path, re.I))
        if not (
            _APP_SEMANTIC.search(semantic)
            or (url_app_cue and _DOWNLOAD_SEMANTIC.search(semantic))
        ):
            continue
        if destination in seen:
            continue
        seen.add(destination)
        kind = "download_resource" if _RESOURCE_SUFFIX.search(path) else "page_candidate"
        candidate = WebGuideCandidate(
            candidate_id=hashlib.sha256(
                f"{source_capture_id}\n{destination}".encode()
            ).hexdigest()[:32],
            source_capture_id=source_capture_id,
            source_url=source_url,
            destination_url=destination,
            label=str(raw.get("text") or raw.get("aria_label") or "")[:120],
            kind=kind,
        )
        (resources if kind == "download_resource" else pages).append(candidate)
        if len(pages) == limit:
            break
    return tuple((pages + resources)[:limit])
