"""Web V2 页面类型动态适用性。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from urllib.parse import unquote, urlsplit

from services.api.app.application.web_product_capability_assessment import (
    WebProductCapability,
)
from services.api.app.domain.page.web_page_catalog import (
    WEB_BUSINESS_PAGE_TYPE_CODES,
    load_web_page_type_catalog,
)


class WebPageApplicabilityError(ValueError):
    """Web 页面类型适用性请求无效。"""


_SUPPORTED_REQUEST_MODES = frozenset(
    {None, "runtime_dynamic", "catalog_declared", "strict_catalog", "strict_four"}
)
_LEGACY_REQUEST_MODES = frozenset({"catalog_declared", "strict_catalog", "strict_four"})
_CATALOG_PATH = Path(__file__).resolve().parents[1] / "domain/page/web_page_type_catalog.json"
_ENTRY_TYPE_PATTERNS = (
    ("search_page", re.compile(r"(?:搜索|查找|检索|\bsearch\b|/search(?:[/?#]|$))", re.I)),
    ("activity", re.compile(r"(?:活动|专题|促销|优惠|event|campaign|promotion)", re.I)),
    ("video_list", re.compile(r"(?:视频|影视|电影|剧集|动漫|\bvideos?\b|\bmovies?\b)", re.I)),
    (
        "content_list",
        re.compile(r"(?:新闻|资讯|文章|博客|爆料|吃瓜|\bnews\b|\barticles?\b|\bposts?\b)", re.I),
    ),
)


@dataclass(frozen=True, slots=True)
class WebPageApplicabilitySnapshot:
    """任务或收口时冻结的动态页面类型分区与目录身份。"""

    required_page_types: tuple[str, ...]
    optional_page_types: tuple[str, ...]
    not_applicable_page_types: tuple[str, ...]
    applicability_mode: str
    catalog_version: str
    catalog_hash: str
    resolution_reason: str
    product_capabilities: tuple[str, ...] = ()
    page_type_statuses: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        statuses = {
            str(key).strip().casefold(): str(value).strip().casefold()
            for key, value in self.page_type_statuses.items()
        }
        object.__setattr__(self, "page_type_statuses", MappingProxyType(statuses))

    def to_dict(self) -> dict[str, object]:
        return {
            "required_page_types": list(self.required_page_types),
            "optional_page_types": list(self.optional_page_types),
            "not_applicable_page_types": list(self.not_applicable_page_types),
            "applicability_mode": self.applicability_mode,
            "catalog_version": self.catalog_version,
            "catalog_hash": self.catalog_hash,
            "resolution_reason": self.resolution_reason,
            "product_capabilities": list(self.product_capabilities),
            "page_type_statuses": dict(self.page_type_statuses),
        }


def revise_web_page_applicability(
    snapshot: WebPageApplicabilitySnapshot,
    *,
    capabilities: tuple[WebProductCapability, ...],
) -> WebPageApplicabilitySnapshot:
    """追加产品能力修订，但不删除已经由运行证据证明的适用页型。"""

    if not isinstance(snapshot, WebPageApplicabilitySnapshot):
        raise TypeError("snapshot 必须为 WebPageApplicabilitySnapshot")
    if not isinstance(capabilities, tuple) or any(
        not isinstance(item, WebProductCapability) for item in capabilities
    ):
        raise TypeError("capabilities 必须为 WebProductCapability 元组")
    return replace(
        snapshot,
        product_capabilities=tuple(dict.fromkeys(item.value for item in capabilities)),
        resolution_reason="product_capability_revision_no_denominator_shrink",
    )


def resolve_web_page_applicability(
    *,
    target_url: str,
    requested_mode: str | None,
    requested_required_page_types: tuple[str, ...],
) -> WebPageApplicabilitySnapshot:
    """创建新 Web 任务；站点是否在历史目录中不影响结果。"""

    _require_http_target(target_url)
    if requested_mode not in _SUPPORTED_REQUEST_MODES:
        raise WebPageApplicabilityError("Web applicability_mode 不受支持")
    requested = _normalize_page_types(requested_required_page_types)
    # 选了页面范围时只按所选的算（首页没选就不算）；没选时从首页开始、按实际发现的页面算
    required = tuple(sorted(requested)) if requested else ("home",)
    optional = tuple(sorted(set(WEB_BUSINESS_PAGE_TYPE_CODES) - set(required)))
    statuses = {
        page_type: (
            "required"
            if page_type == "home" and page_type in required
            else "applicable_uncovered"
            if page_type in required
            else "not_observed"
        )
        for page_type in WEB_BUSINESS_PAGE_TYPE_CODES
    }
    catalog_version, catalog_hash = _current_catalog_identity()
    return WebPageApplicabilitySnapshot(
        required_page_types=required,
        optional_page_types=optional,
        not_applicable_page_types=(),
        applicability_mode="runtime_dynamic",
        catalog_version=catalog_version,
        catalog_hash=catalog_hash,
        resolution_reason=(
            "legacy_request_mode_dynamic_alias"
            if requested_mode in _LEGACY_REQUEST_MODES
            else "client_explicit_runtime_targets"
            if requested
            else "runtime_evidence_initial"
        ),
        page_type_statuses=statuses,
    )


def finalize_web_page_applicability(
    snapshot: WebPageApplicabilitySnapshot,
    *,
    applicable_page_types: tuple[str, ...],
    covered_page_types: tuple[str, ...],
    not_applicable_page_types: tuple[str, ...] = (),
    inspection_scope: frozenset[str] = frozenset(),
) -> WebPageApplicabilitySnapshot:
    """按本 Run 入口与正式分类证据冻结最终覆盖分母。

    任务选了质检范围时分母就是所选页面类型：经过的页面、首页（没选时）和从入口名猜出的页面类型都不算。
    """

    if not isinstance(snapshot, WebPageApplicabilitySnapshot):
        raise TypeError("snapshot 必须为 WebPageApplicabilitySnapshot")
    covered = set(_normalize_business_evidence(covered_page_types))
    explicit_not_applicable = set(_normalize_business_evidence(not_applicable_page_types))
    if inspection_scope:
        applicable = set(_normalize_business_evidence(tuple(sorted(inspection_scope))))
        covered &= applicable
    else:
        applicable = set(_normalize_business_evidence(applicable_page_types))
        applicable.update(covered)
        applicable.update(snapshot.required_page_types)
        applicable.add("home")
    explicit_not_applicable -= applicable
    required = tuple(sorted(applicable))
    optional = tuple(
        sorted(set(WEB_BUSINESS_PAGE_TYPE_CODES) - applicable - explicit_not_applicable)
    )
    statuses = {
        page_type: (
            "covered"
            if page_type in covered
            else "applicable_uncovered"
            if page_type in applicable and page_type != "home"
            else "required"
            if page_type == "home" and page_type in applicable
            else "not_applicable"
            if page_type in explicit_not_applicable
            else "not_observed"
        )
        for page_type in WEB_BUSINESS_PAGE_TYPE_CODES
    }
    return replace(
        snapshot,
        required_page_types=required,
        optional_page_types=optional,
        not_applicable_page_types=tuple(sorted(explicit_not_applicable)),
        resolution_reason="runtime_evidence_finalized",
        page_type_statuses=statuses,
    )


def infer_runtime_entry_page_type(*, label: str, url: str | None) -> str | None:
    """只用明确入口语义推断适用页型，不把弱路径提示当页面分类结论。"""

    normalized_label = " ".join(str(label).split())
    normalized_url = unquote(str(url or ""))
    if normalized_url:
        path = urlsplit(normalized_url).path.rstrip("/")
        if not path and re.search(r"(?:首页|主页|\bhome\b)", normalized_label, re.I):
            return "home"
    value = f"{normalized_label} {normalized_url}"
    for page_type, pattern in _ENTRY_TYPE_PATTERNS:
        if pattern.search(value):
            return page_type
    return None


def _current_catalog_identity() -> tuple[str, str]:
    catalog = load_web_page_type_catalog()
    payload = json.loads(_CATALOG_PATH.read_text(encoding="utf-8"))
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return catalog.version, hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_http_target(target_url: str) -> None:
    parsed = urlsplit(str(target_url).strip())
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise WebPageApplicabilityError("Web target_url 必须为 HTTP(S) URL")


def _normalize_business_evidence(values: tuple[str, ...]) -> tuple[str, ...]:
    if isinstance(values, str | bytes):
        raise WebPageApplicabilityError("页面类型证据必须为集合")
    allowed = set(WEB_BUSINESS_PAGE_TYPE_CODES)
    return tuple(
        sorted(
            {
                str(value).strip().casefold()
                for value in values
                if str(value).strip().casefold() in allowed
            }
        )
    )


def _normalize_page_types(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(
        sorted({str(value).strip().casefold() for value in values if str(value).strip()})
    )
    unsupported = set(normalized) - set(WEB_BUSINESS_PAGE_TYPE_CODES)
    if unsupported:
        raise WebPageApplicabilityError("required_page_types 包含非 Web 标准页型")
    return normalized


__all__ = [
    "WebPageApplicabilityError",
    "WebPageApplicabilitySnapshot",
    "finalize_web_page_applicability",
    "infer_runtime_entry_page_type",
    "revise_web_page_applicability",
    "resolve_web_page_applicability",
]
