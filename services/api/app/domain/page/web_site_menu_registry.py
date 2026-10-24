"""历史 Web 站点目录与账本数据结构；旧 Registry 仅用于只读审计解析。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

from services.api.app.domain.page.web_origin import (
    canonical_web_host,
    canonical_web_origin,
    web_origin_matches,
)
from services.api.app.domain.page.web_page_catalog import WEB_BUSINESS_PAGE_TYPE_CODES
from services.api.app.domain.page.web_url_identity import DEFAULT_WEB_URL_IDENTITY_POLICY

_EXPECTED_POLICY: dict[str, object] = {
    "match_scope": "site_origin_and_business_url_v2",
    "host_wildcards_allowed": False,
    "menu_label_is_authoritative": False,
    "fragment_ignored": False,
    "identity_policy_version": "web-url-identity.v2",
    "query_is_part_of_identity": True,
    "http_to_https_same_host_upgrade_allowed": True,
    "https_to_http_downgrade_allowed": False,
    "redirect_must_remain_same_host": True,
    "runtime_dom_presence_required": True,
    "runtime_discovered_unlisted_action": "record_only_needs_review",
    "cross_origin_action": "block_and_record_terminal",
    "home_action": "use_existing_home_baseline",
    "allowlisted_action": "safe_get_observe_only",
}


class WebSiteMenuCatalogError(ValueError):
    """Web 菜单目录结构或安全策略无效。"""


class WebSiteMenuCatalogNotFoundError(LookupError):
    """目标站点没有已发布的菜单目录。"""


class WebMenuDecision(StrEnum):
    """生产菜单目录允许的三个静态决策。"""

    HOME_BASELINE = "home_baseline"
    ALLOWLISTED_OBSERVE_ONLY = "allowlisted_observe_only"
    BLOCKED_CROSS_ORIGIN = "blocked_cross_origin"


class WebCatalogStatus(StrEnum):
    """站点与条目的目录生命周期。"""

    ACTIVE = "active"
    DEPRECATED = "deprecated"
    OBSERVE_ONLY = "observe-only"


@dataclass(frozen=True, kw_only=True)
class WebPageClassificationHint:
    """菜单和 URL 提供的非权威页型候选。"""

    expected_page_type: str
    confidence: float
    source: str
    mixed_content: bool = False
    authority: str = "advisory"


@dataclass(frozen=True, kw_only=True)
class WebCollectionEligibility:
    """菜单是否有资格进入业务页面采集分母。"""

    eligible: bool
    reason: str | None = None


@dataclass(frozen=True, kw_only=True)
class WebSiteMenuEntry:
    """一个菜单身份；相同 URL 的不同路径作为别名保留，由运行时合并访问。"""

    entry_id: str
    menu_path: tuple[str, ...]
    href: str
    canonical_url: str
    decision: WebMenuDecision
    is_same_origin: bool
    classification_hint: WebPageClassificationHint | None = None
    collection_eligibility: WebCollectionEligibility = WebCollectionEligibility(eligible=True)
    catalog_version: str = "unversioned"
    status: WebCatalogStatus = WebCatalogStatus.OBSERVE_ONLY
    verification: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, kw_only=True)
class WebSiteMenuCatalog:
    """单个目标站点的版本化菜单目录。"""

    site_id: str
    origin: str
    navigation_evidence: Mapping[str, object]
    entries: tuple[WebSiteMenuEntry, ...]
    catalog_version: str = "unversioned"
    status: WebCatalogStatus = WebCatalogStatus.ACTIVE
    verification: Mapping[str, object] = field(default_factory=dict)
    required_page_types: tuple[str, ...] = ()
    optional_page_types: tuple[str, ...] = ()
    not_applicable_page_types: tuple[str, ...] = ()

    def contains_url_origin(self, url: str) -> bool:
        """复用目录的同源与仅 HTTP 到 HTTPS 升级规则。"""
        return _target_matches_site(
            _parse_http_url(url, field_name="url"),
            _parse_http_url(self.origin, field_name="origin"),
        )

    def advisory_entries_for_url(self, runtime_url: str) -> tuple[WebSiteMenuEntry, ...]:
        """按规范 URL 返回白名单提示；结果只供排序和分类提示，不证明到达。"""

        runtime = _parse_http_url(runtime_url, field_name="runtime_url")
        return tuple(
            entry
            for entry in self.entries
            if entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY
            and entry.status is not WebCatalogStatus.DEPRECATED
            and entry.collection_eligibility.eligible
            and _catalog_runtime_url_matches(entry.canonical_url, runtime)
        )


@dataclass(frozen=True)
class WebSiteMenuContext:
    """静态先验或由规范站点 origin 建立的可恢复首页上下文。"""

    site: WebSiteMenuCatalog
    source_kind: Literal["catalog", "runtime"]
    content_sha256: str


def canonical_menu_url(href: str, *, site_origin: str) -> str:
    """解析菜单 URL，复用 Web v2 业务身份并保留路由 Hash。"""

    origin = _parse_http_url(site_origin, field_name="site_origin")
    resolved = _parse_http_url(
        urljoin(f"{_canonical_origin(origin)}/", href),
        field_name="href",
    )
    if _is_https_downgrade(origin, resolved):
        raise WebSiteMenuCatalogError("HTTPS 站点菜单禁止降级到 HTTP")
    return DEFAULT_WEB_URL_IDENTITY_POLICY.canonical(resolved.geturl())


class WebSiteMenuRegistry:
    """加载并严格校验历史菜单 Catalog，不得注入当前运行时。"""

    def __init__(
        self,
        path: Path | None = None,
        *,
        payload: Mapping[str, object] | None = None,
        legacy_frozen: bool = False,
    ) -> None:
        catalog_path = path or Path(__file__).with_name("web_site_menu_catalog.json")
        payload = (
            _load_payload(catalog_path) if payload is None else json.loads(json.dumps(payload))
        )
        self._payload_json = json.dumps(payload, ensure_ascii=False)
        self.schema_version = _required_text(payload, "schema_version")
        self.catalog_id = _required_text(payload, "catalog_id")
        self.version = _required_text(payload, "version")
        if self.schema_version != "web-menu-allowlist.v4":
            raise WebSiteMenuCatalogError("Web 菜单目录 schema_version 不受支持")
        _validate_policy(payload.get("policy"))
        self.source_attachment_sha256 = _source_attachment_sha256(payload.get("source"))
        self.sites = _parse_sites(
            payload.get("sites"), catalog_version=self.version, legacy_frozen=legacy_frozen
        )
        _validate_summary(payload.get("summary"), self.sites)
        self.summary = MappingProxyType(dict(payload["summary"]))
        self.catalog_content_sha256 = _catalog_content_sha256(
            schema_version=self.schema_version,
            catalog_id=self.catalog_id,
            version=self.version,
            sites=self.sites,
        )

    def to_payload(self) -> dict[str, object]:
        """完整导出发布正文的独立副本，包含先验与来源审计。"""
        return json.loads(self._payload_json)

    def resolve_site(self, target_url: str) -> WebSiteMenuCatalog:
        """只返回与目标 host 和安全 scheme 精确匹配的单站目录。"""

        target = _parse_http_url(target_url, field_name="target_url")
        for site in self.sites:
            origin = _parse_http_url(site.origin, field_name="origin")
            if _target_matches_site(target, origin):
                return site
        raise WebSiteMenuCatalogNotFoundError(
            f"目标站点没有已发布的 Web 菜单目录：{_canonical_origin(target)}"
        )

    def resolve_site_context(self, target_url: str) -> WebSiteMenuContext:
        """静态目录仅提供先验；合法未登记 origin 建立独立首页基线。"""

        try:
            site = self.resolve_site(target_url)
        except WebSiteMenuCatalogNotFoundError:
            return _runtime_home_context(target_url)
        return WebSiteMenuContext(site, "catalog", self.catalog_content_sha256)


class RuntimeWebSiteMenuRegistry:
    """新运行仅初始化首页身份，后续菜单和详情完全来自当前页面事实。"""

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": "web-runtime-navigation.v1"}

    def resolve_site_context(self, target_url: str) -> WebSiteMenuContext:
        return _runtime_home_context(target_url)

    def resolve_site(self, target_url: str) -> WebSiteMenuCatalog:
        return self.resolve_site_context(target_url).site


def _runtime_home_context(target_url: str) -> WebSiteMenuContext:
    origin = _canonical_origin(_parse_http_url(target_url, field_name="target_url"))
    site_id = hashlib.sha256(origin.encode("utf-8")).hexdigest()
    version = "web-runtime-site.v1"
    site = WebSiteMenuCatalog(
        site_id=site_id,
        origin=origin,
        navigation_evidence=MappingProxyType({"source_kind": "runtime", "origin": origin}),
        catalog_version=version,
        entries=(WebSiteMenuEntry(
            entry_id=f"{site_id}:home",
            menu_path=("首页",),
            href=f"{origin}/",
            canonical_url=f"{origin}/",
            decision=WebMenuDecision.HOME_BASELINE,
            is_same_origin=True,
            catalog_version=version,
            status=WebCatalogStatus.ACTIVE,
        ),),
    )
    digest = _catalog_content_sha256(
        schema_version=version, catalog_id=site_id, version=version, sites=(site,)
    )
    return WebSiteMenuContext(site, "runtime", digest)


def _load_payload(path: Path) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as catalog_file:
            payload = json.load(catalog_file)
    except (OSError, json.JSONDecodeError) as exc:
        raise WebSiteMenuCatalogError(f"Web 菜单目录加载失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise WebSiteMenuCatalogError("Web 菜单目录根节点必须为对象")
    return payload


def _validate_policy(value: object) -> None:
    if not isinstance(value, dict) or value != _EXPECTED_POLICY:
        raise WebSiteMenuCatalogError("Web 菜单目录 policy 与只读安全基线不一致")


def _source_attachment_sha256(value: object) -> str:
    if not isinstance(value, dict) or value.get("kind") != "operator_provided_navigation_html":
        raise WebSiteMenuCatalogError("Web 菜单目录 source.kind 不受支持")
    digest = value.get("attachment_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdefABCDEF" for character in digest)
    ):
        raise WebSiteMenuCatalogError("Web 菜单目录 source.attachment_sha256 无效")
    return digest.upper()


def _parse_sites(
    value: object,
    *,
    catalog_version: str,
    legacy_frozen: bool = False,
) -> tuple[WebSiteMenuCatalog, ...]:
    if not isinstance(value, list) or not value:
        raise WebSiteMenuCatalogError("Web 菜单目录至少需要一个站点")
    sites: list[WebSiteMenuCatalog] = []
    site_ids: set[str] = set()
    origins: set[str] = set()
    global_entry_ids: set[str] = set()
    for raw_site in value:
        if not isinstance(raw_site, dict):
            raise WebSiteMenuCatalogError("站点目录必须为对象")
        site_id = _required_text(raw_site, "site_id")
        origin_parts = _parse_http_url(
            _required_text(raw_site, "origin"),
            field_name="origin",
        )
        origin = _canonical_origin(origin_parts)
        if "*" in origin:
            raise WebSiteMenuCatalogError("Web 菜单目录禁止主机通配")
        if site_id in site_ids or origin in origins:
            raise WebSiteMenuCatalogError("Web 菜单目录的 site_id 和 origin 必须唯一")
        site_ids.add(site_id)
        origins.add(origin)
        site_version = _required_text(raw_site, "catalog_version")
        if site_version != catalog_version:
            raise WebSiteMenuCatalogError(f"{site_id}.catalog_version 与根版本不一致")
        site_status = _catalog_status(raw_site.get("status"), field_name=f"{site_id}.status")
        if site_status is not WebCatalogStatus.ACTIVE:
            raise WebSiteMenuCatalogError(f"{site_id}.status 必须为 active")
        site_verification = _verification(
            raw_site.get("verification"),
            field_name=f"{site_id}.verification",
        )
        applicability = _site_applicability(
            raw_site.get("applicability"),
            field_name=f"{site_id}.applicability",
            legacy_frozen=legacy_frozen,
        )
        entry_defaults = raw_site.get("entry_defaults")
        if not isinstance(entry_defaults, Mapping):
            raise WebSiteMenuCatalogError(f"{site_id}.entry_defaults 必须为对象")
        default_version = _required_text(entry_defaults, "version")
        if default_version != catalog_version:
            raise WebSiteMenuCatalogError(f"{site_id}.entry_defaults.version 与根版本不一致")
        default_status = _catalog_status(
            entry_defaults.get("status"),
            field_name=f"{site_id}.entry_defaults.status",
        )
        default_verification = _verification(
            entry_defaults.get("verification"),
            field_name=f"{site_id}.entry_defaults.verification",
        )
        entry_overrides = raw_site.get("entry_overrides")
        if not isinstance(entry_overrides, Mapping):
            raise WebSiteMenuCatalogError(f"{site_id}.entry_overrides 必须为对象")
        entries = _parse_entries(
            raw_site.get("entries"),
            site_origin=origin,
            global_entry_ids=global_entry_ids,
            catalog_version=catalog_version,
            default_status=default_status,
            default_verification=default_verification,
            entry_overrides=entry_overrides,
        )
        evidence = raw_site.get("navigation_evidence")
        if not isinstance(evidence, dict):
            raise WebSiteMenuCatalogError(f"{site_id}.navigation_evidence 必须为对象")
        active_anchor_count = evidence.get("active_anchor_count")
        if active_anchor_count != len(entries):
            raise WebSiteMenuCatalogError(f"{site_id}.active_anchor_count 与 entries 数量不一致")
        sites.append(
            WebSiteMenuCatalog(
                site_id=site_id,
                origin=origin,
                navigation_evidence=MappingProxyType(dict(evidence)),
                entries=entries,
                catalog_version=site_version,
                status=site_status,
                verification=site_verification,
                required_page_types=applicability[0],
                optional_page_types=applicability[1],
                not_applicable_page_types=applicability[2],
            )
        )
    return tuple(sites)


def _parse_entries(
    value: object,
    *,
    site_origin: str,
    global_entry_ids: set[str],
    catalog_version: str,
    default_status: WebCatalogStatus,
    default_verification: Mapping[str, object],
    entry_overrides: Mapping[str, object],
) -> tuple[WebSiteMenuEntry, ...]:
    if not isinstance(value, list) or not value:
        raise WebSiteMenuCatalogError("每个站点至少需要一个菜单条目")
    origin_parts = _parse_http_url(site_origin, field_name="site_origin")
    entries: list[WebSiteMenuEntry] = []
    home_count = 0
    for raw_entry in value:
        if not isinstance(raw_entry, dict):
            raise WebSiteMenuCatalogError("菜单条目必须为对象")
        entry_id = _required_text(raw_entry, "entry_id")
        if entry_id in global_entry_ids:
            raise WebSiteMenuCatalogError(f"菜单 entry_id 重复：{entry_id}")
        global_entry_ids.add(entry_id)
        menu_path = _required_menu_path(raw_entry.get("menu_path"), entry_id=entry_id)
        href = _required_text(raw_entry, "href")
        try:
            decision = WebMenuDecision(_required_text(raw_entry, "decision"))
        except ValueError as exc:
            raise WebSiteMenuCatalogError(f"{entry_id}.decision 不受支持") from exc
        canonical_url = canonical_menu_url(href, site_origin=site_origin)
        target_parts = _parse_http_url(canonical_url, field_name="canonical_url")
        is_same_origin = _target_matches_site(target_parts, origin_parts)
        if decision is WebMenuDecision.BLOCKED_CROSS_ORIGIN:
            if is_same_origin:
                raise WebSiteMenuCatalogError(f"{entry_id} 同源菜单不能标记为跨域阻断")
        elif not is_same_origin:
            raise WebSiteMenuCatalogError(f"{entry_id} 非同源菜单必须标记为跨域阻断")
        if decision is WebMenuDecision.HOME_BASELINE:
            home_count += 1
        classification_hint = _classification_hint(raw_entry, entry_id=entry_id)
        collection_eligibility = _collection_eligibility(
            raw_entry,
            entry_id=entry_id,
        )
        status, verification = _entry_lifecycle(
            entry_id=entry_id,
            default_status=default_status,
            default_verification=default_verification,
            entry_overrides=entry_overrides,
        )
        if status is WebCatalogStatus.DEPRECATED:
            collection_eligibility = WebCollectionEligibility(
                eligible=False,
                reason="catalog_entry_deprecated",
            )
        if collection_eligibility.eligible and classification_hint is None:
            raise WebSiteMenuCatalogError(f"{entry_id} 可采集菜单必须提供 classification_hint")
        if decision is WebMenuDecision.BLOCKED_CROSS_ORIGIN and collection_eligibility.eligible:
            raise WebSiteMenuCatalogError(f"{entry_id} 跨域阻断菜单不能进入采集分母")
        entries.append(
            WebSiteMenuEntry(
                entry_id=entry_id,
                menu_path=menu_path,
                href=href,
                canonical_url=canonical_url,
                decision=decision,
                is_same_origin=is_same_origin,
                classification_hint=classification_hint,
                collection_eligibility=collection_eligibility,
                catalog_version=catalog_version,
                status=status,
                verification=verification,
            )
        )
    unknown_overrides = set(entry_overrides) - {entry.entry_id for entry in entries}
    if unknown_overrides:
        raise WebSiteMenuCatalogError("entry_overrides 引用了不存在的菜单条目")
    if home_count != 1:
        raise WebSiteMenuCatalogError("每个站点必须且只能有一个 home_baseline")
    return tuple(entries)


def _validate_summary(value: object, sites: tuple[WebSiteMenuCatalog, ...]) -> None:
    if not isinstance(value, dict):
        raise WebSiteMenuCatalogError("Web 菜单目录 summary 必须为对象")
    entries = tuple(entry for site in sites for entry in site.entries)
    expected = {
        "site_count": len(sites),
        "active_anchor_count": len(entries),
        "home_baseline_count": sum(
            entry.decision is WebMenuDecision.HOME_BASELINE for entry in entries
        ),
        "allowlisted_navigation_count": sum(
            entry.decision is WebMenuDecision.ALLOWLISTED_OBSERVE_ONLY for entry in entries
        ),
        "blocked_cross_origin_count": sum(
            entry.decision is WebMenuDecision.BLOCKED_CROSS_ORIGIN for entry in entries
        ),
        "unique_same_origin_target_count": len(
            {entry.canonical_url for entry in entries if entry.is_same_origin}
        ),
        "active_entry_count": sum(entry.status is WebCatalogStatus.ACTIVE for entry in entries),
        "observe_only_entry_count": sum(
            entry.status is WebCatalogStatus.OBSERVE_ONLY for entry in entries
        ),
        "deprecated_entry_count": sum(
            entry.status is WebCatalogStatus.DEPRECATED for entry in entries
        ),
    }
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise WebSiteMenuCatalogError(f"summary.{key} 与实际目录不一致")
    ignored_count = value.get("ignored_commented_anchor_count")
    if not isinstance(ignored_count, int) or isinstance(ignored_count, bool) or ignored_count < 0:
        raise WebSiteMenuCatalogError("summary.ignored_commented_anchor_count 必须为非负整数")


def _catalog_content_sha256(
    *,
    schema_version: str,
    catalog_id: str,
    version: str,
    sites: tuple[WebSiteMenuCatalog, ...],
) -> str:
    """摘要冻结规范化菜单内容，不混用操作方附件摘要。"""

    payload = {
        "schema_version": schema_version,
        "catalog_id": catalog_id,
        "version": version,
        "policy": _EXPECTED_POLICY,
        "sites": [
            {
                "site_id": site.site_id,
                "origin": site.origin,
                "navigation_evidence": dict(site.navigation_evidence),
                "catalog_version": site.catalog_version,
                "status": site.status.value,
                "verification": dict(site.verification),
                "applicability": {
                    "required_page_types": list(site.required_page_types),
                    "optional_page_types": list(site.optional_page_types),
                    "not_applicable_page_types": list(site.not_applicable_page_types),
                },
                "entries": [
                    {
                        "entry_id": entry.entry_id,
                        "menu_path": list(entry.menu_path),
                        "href": entry.href,
                        "canonical_url": entry.canonical_url,
                        "decision": entry.decision.value,
                        "is_same_origin": entry.is_same_origin,
                        "catalog_version": entry.catalog_version,
                        "status": entry.status.value,
                        "verification": dict(entry.verification),
                        "classification_hint": (
                            {
                                "expected_page_type": entry.classification_hint.expected_page_type,
                                "confidence": entry.classification_hint.confidence,
                                "source": entry.classification_hint.source,
                                "mixed_content": entry.classification_hint.mixed_content,
                                "authority": entry.classification_hint.authority,
                            }
                            if entry.classification_hint is not None
                            else None
                        ),
                        "collection_eligibility": {
                            "eligible": entry.collection_eligibility.eligible,
                            "reason": entry.collection_eligibility.reason,
                        },
                    }
                    for entry in site.entries
                ],
            }
            for site in sites
        ],
    }
    normalized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WebSiteMenuCatalogError(f"{key} 不能为空")
    return value.strip()


def _site_applicability(
    value: object,
    *,
    field_name: str,
    legacy_frozen: bool = False,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """读取独立于导航提示的站点级页面类型适用性声明。"""

    from services.api.app.domain.page.web_page_catalog import (
        WEB_BUSINESS_PAGE_TYPE_CODES,
        WEB_LEGACY_PAGE_TYPE_CODES,
        WEB_V3_BUSINESS_PAGE_TYPE_CODES,
    )

    if not isinstance(value, Mapping):
        raise WebSiteMenuCatalogError(f"{field_name} 必须为对象")
    partitions: list[tuple[str, ...]] = []
    for key in (
        "required_page_types",
        "optional_page_types",
        "not_applicable_page_types",
    ):
        raw = value.get(key)
        if not isinstance(raw, list):
            raise WebSiteMenuCatalogError(f"{field_name}.{key} 必须为数组")
        normalized = tuple(
            sorted({_required_text({"value": item}, "value").casefold() for item in raw})
        )
        partitions.append(normalized)
    required, optional, not_applicable = partitions
    actual = set(required) | set(optional) | set(not_applicable)
    canonical = (
        WEB_LEGACY_PAGE_TYPE_CODES
        if legacy_frozen
        else WEB_V3_BUSINESS_PAGE_TYPE_CODES
        if actual == set(WEB_V3_BUSINESS_PAGE_TYPE_CODES)
        # 历史静态站点目录保持原业务闭集，不自动给旧目录添加集合页。
        else tuple(code for code in WEB_BUSINESS_PAGE_TYPE_CODES if code != "collection_page")
        if actual == set(WEB_BUSINESS_PAGE_TYPE_CODES) - {"collection_page"}
        else WEB_BUSINESS_PAGE_TYPE_CODES
    )
    unsupported = actual - set(canonical)
    if unsupported:
        raise WebSiteMenuCatalogError(f"{field_name} 包含非 Web 标准页型")
    if (
        set(required) & set(optional)
        or set(required) & set(not_applicable)
        or set(optional) & set(not_applicable)
    ):
        raise WebSiteMenuCatalogError(f"{field_name} 页面类型分区不能交叉")
    if set(required) | set(optional) | set(not_applicable) != set(canonical):
        raise WebSiteMenuCatalogError(f"{field_name} 必须完整覆盖版本内 Web 业务页型")
    if not required:
        raise WebSiteMenuCatalogError(f"{field_name}.required_page_types 不能为空")
    return required, optional, not_applicable


def _required_menu_path(value: object, *, entry_id: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise WebSiteMenuCatalogError(f"{entry_id}.menu_path 不能为空")
    path = tuple(str(item).strip() for item in value)
    if any(not item for item in path):
        raise WebSiteMenuCatalogError(f"{entry_id}.menu_path 不能包含空层级")
    return path


def _classification_hint(
    payload: Mapping[str, object],
    *,
    entry_id: str,
) -> WebPageClassificationHint | None:
    value = payload.get("classification_hint")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise WebSiteMenuCatalogError(f"{entry_id}.classification_hint 必须为对象")
    expected = _required_text(value, "expected_page_type").casefold()
    if expected not in WEB_BUSINESS_PAGE_TYPE_CODES:
        raise WebSiteMenuCatalogError(
            f"{entry_id}.classification_hint.expected_page_type 不在七类业务闭集"
        )
    confidence = value.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, int | float)
        or not 0 <= float(confidence) <= 1
    ):
        raise WebSiteMenuCatalogError(f"{entry_id}.classification_hint.confidence 无效")
    mixed_content = value.get("mixed_content", False)
    if not isinstance(mixed_content, bool):
        raise WebSiteMenuCatalogError(f"{entry_id}.classification_hint.mixed_content 无效")
    return WebPageClassificationHint(
        expected_page_type=expected,
        confidence=float(confidence),
        source=_required_text(value, "source"),
        mixed_content=mixed_content,
        authority="advisory",
    )


def _catalog_status(value: object, *, field_name: str) -> WebCatalogStatus:
    try:
        return WebCatalogStatus(_required_text({"value": value}, "value"))
    except (TypeError, ValueError) as exc:
        raise WebSiteMenuCatalogError(f"{field_name} 不受支持") from exc


def _verification(value: object, *, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WebSiteMenuCatalogError(f"{field_name} 必须为对象")
    normalized = {
        "verified_at": _required_text(value, "verified_at"),
        "evidence_ref": _required_text(value, "evidence_ref"),
        "result": _required_text(value, "result"),
    }
    if not normalized["evidence_ref"].startswith("docs/"):
        raise WebSiteMenuCatalogError(f"{field_name}.evidence_ref 必须引用仓库 docs")
    return MappingProxyType(normalized)


def _entry_lifecycle(
    *,
    entry_id: str,
    default_status: WebCatalogStatus,
    default_verification: Mapping[str, object],
    entry_overrides: Mapping[str, object],
) -> tuple[WebCatalogStatus, Mapping[str, object]]:
    override = entry_overrides.get(entry_id)
    if override is None:
        return default_status, default_verification
    if not isinstance(override, Mapping):
        raise WebSiteMenuCatalogError(f"{entry_id}.entry_overrides 必须为对象")
    status = _catalog_status(
        override.get("status", default_status.value),
        field_name=f"{entry_id}.status",
    )
    verification_value = override.get("verification")
    verification = (
        default_verification
        if verification_value is None
        else _verification(
            verification_value,
            field_name=f"{entry_id}.verification",
        )
    )
    return status, verification


def _collection_eligibility(
    payload: Mapping[str, object],
    *,
    entry_id: str,
) -> WebCollectionEligibility:
    value = payload.get("collection_eligibility")
    if not isinstance(value, Mapping) or not isinstance(value.get("eligible"), bool):
        raise WebSiteMenuCatalogError(f"{entry_id}.collection_eligibility 无效")
    reason_value = value.get("reason")
    reason = str(reason_value).strip() if reason_value is not None else None
    if not value["eligible"] and not reason:
        raise WebSiteMenuCatalogError(f"{entry_id} 排除采集时必须提供原因")
    return WebCollectionEligibility(eligible=value["eligible"], reason=reason)


def _parse_http_url(value: str, *, field_name: str):
    try:
        parsed = urlsplit(value.strip())
        _ = parsed.port
    except (AttributeError, ValueError) as exc:
        raise WebSiteMenuCatalogError(f"{field_name} 不是合法 URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise WebSiteMenuCatalogError(f"{field_name} 必须为无凭据的 HTTP(S) URL")
    return parsed


def _canonical_origin(parts: object) -> str:
    return canonical_web_origin(parts.geturl())


def _target_matches_site(target: object, origin: object) -> bool:
    return web_origin_matches(target.geturl(), origin.geturl(), allow_http_upgrade=True)


def _catalog_runtime_url_matches(catalog_url: str, runtime: object) -> bool:
    catalog = _parse_http_url(catalog_url, field_name="catalog_url")
    if (
        (catalog.path or "/") != (runtime.path or "/")
        or catalog.query != runtime.query
        or catalog.fragment != runtime.fragment
    ):
        return False
    return web_origin_matches(runtime.geturl(), catalog.geturl(), allow_http_upgrade=True)


def _is_https_downgrade(origin: object, target: object) -> bool:
    return (
        canonical_web_host(origin.hostname) == canonical_web_host(target.hostname)
        and str(origin.scheme).casefold() == "https"
        and str(target.scheme).casefold() == "http"
    )


@lru_cache
def get_default_web_site_menu_registry() -> WebSiteMenuRegistry:
    return WebSiteMenuRegistry()


__all__ = [
    "WebCatalogStatus",
    "WebMenuDecision",
    "WebCollectionEligibility",
    "WebPageClassificationHint",
    "WebSiteMenuCatalog",
    "WebSiteMenuContext",
    "WebSiteMenuCatalogError",
    "WebSiteMenuCatalogNotFoundError",
    "WebSiteMenuEntry",
    "WebSiteMenuRegistry",
    "canonical_menu_url",
    "get_default_web_site_menu_registry",
]
