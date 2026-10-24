"""桌面 Web 独立页面类型目录。"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from services.api.app.domain.page.catalog import _parse_catalog
from services.api.app.domain.page.registry import PageTypeRegistry
from services.api.app.domain.page.web_page_knowledge import (
    WebCollapsedAndVisibleNav,
    WebCommonSiteObservations,
    WebDetailLinkSignals,
    WebNavigationSignals,
    WebPageTypeCatalog,
    WebPageTypeGroups,
    parse_web_page_recognition_rules,
)

WEB_LEGACY_PAGE_TYPE_CODES = ("content_list", "content_detail", "video_list", "video_detail")
WEB_V3_BUSINESS_PAGE_TYPE_CODES = (
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
)
_WEB_V10_BUSINESS_PAGE_TYPE_CODES = (*WEB_V3_BUSINESS_PAGE_TYPE_CODES, "activity", "search_page")
WEB_BUSINESS_PAGE_TYPE_CODES = (*_WEB_V10_BUSINESS_PAGE_TYPE_CODES, "collection_page")
WEB_LIFECYCLE_PAGE_TYPE_CODES = ("age_gate",)
WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES = ("ad_page", "recharge_page", "topup_page", "guide_page")
WEB_NEITHER_PAGE_TYPE_CODES = ("unknown",)
WEB_PAGE_TYPE_CODES = (
    *WEB_BUSINESS_PAGE_TYPE_CODES,
    *WEB_LIFECYCLE_PAGE_TYPE_CODES,
    *WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES,
    *WEB_NEITHER_PAGE_TYPE_CODES,
)
WEB_HOME_PAGE_TYPES = frozenset({"home"})

_WEB_V5_TO_V7_TYPE_CODES = WEB_V3_BUSINESS_PAGE_TYPE_CODES
_WEB_V8_TYPE_CODES = (
    *_WEB_V10_BUSINESS_PAGE_TYPE_CODES,
    *WEB_LIFECYCLE_PAGE_TYPE_CODES,
    *WEB_NEITHER_PAGE_TYPE_CODES,
)
_WEB_V8_VERSION = "2026-09-13.web.v8"
_WEB_V9_VERSION = "2026-09-14.web.v9"
_WEB_V10_VERSION = "2026-09-15.web.v10"
_WEB_V11_VERSION = "2026-09-19.web.v11"
_WEB_V12_VERSION = "2026-09-26.web.v12"
_WEB_V13_VERSION = "2026-09-28.web.v13"
_WEB_V14_VERSION = "2026-09-29.web.v14"
_WEB_V15_VERSION = "2026-09-29.web.v15"
_WEB_V16_VERSION = "2026-09-30.web.v16"
_WEB_V17_VERSION = "2026-09-30.web.v17"
CURRENT_WEB_CATALOG_VERSION = "2026-09-30.web.v18"


def web_page_type_codes_for_catalog_version(version: str) -> tuple[str, ...]:
    """按冻结目录版本返回允许持久化的 Web 页型。"""

    if version == _WEB_V8_VERSION:
        return _WEB_V8_TYPE_CODES
    excluded_by_version = {
        _WEB_V9_VERSION: {"collection_page", "recharge_page", "topup_page", "guide_page"},
        _WEB_V10_VERSION: {"collection_page", "recharge_page", "topup_page", "guide_page"},
        _WEB_V11_VERSION: {"recharge_page", "topup_page", "guide_page"},
        _WEB_V12_VERSION: {"topup_page", "guide_page"},
        _WEB_V13_VERSION: {"guide_page"},
        _WEB_V14_VERSION: set(),
        _WEB_V15_VERSION: set(),
        _WEB_V16_VERSION: set(),
        _WEB_V17_VERSION: set(),
        CURRENT_WEB_CATALOG_VERSION: set(),
    }
    if version not in excluded_by_version:
        raise WebPageTypeCatalogError("Web 页面目录版本不受支持")
    return tuple(
        code for code in WEB_PAGE_TYPE_CODES
        if code not in excluded_by_version[version]
    )
_WEB_V5_TO_V7_VERSION_PATTERN = re.compile(r"2026-09-(11|12|13)\.web\.v[5-7]")
_WEB_V8_CATALOG_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "version",
        "platform",
        "source",
        "page_types",
        "navigation_signals",
        "detail_link_signals",
        "common_site_observations",
        "page_type_groups",
        "media_rules",
        "detail_classification_principles",
        "notes",
    }
)
_WEB_V5_TO_V7_CATALOG_ROOT_FIELDS = frozenset(
    {
        "schema_version",
        "version",
        "platform",
        "source",
        "page_types",
        "navigation_signals",
        "detail_link_signals",
        "common_site_observations",
    }
)
_WEB_NAVIGATION_SIGNAL_FIELDS = (
    "common_positive_signals",
    "supporting_signals",
    "collapsed_and_visible_nav",
    "strong_negative_signals",
    "distinguish_from_breadcrumb",
    "usage_notes",
)
_WEB_LEGACY_NAVIGATION_SIGNAL_FIELDS = (
    "common_positive_signals",
    "supporting_signals",
    "strong_negative_signals",
    "usage_notes",
)
_WEB_COLLAPSED_NAVIGATION_FIELDS = ("description", "rules")
_WEB_NAVIGATION_BREADCRUMB_FIELDS = ("menu_nav", "breadcrumb")
_WEB_DETAIL_LINK_SIGNAL_FIELDS = (
    "purpose",
    "required_positive_signals",
    "supporting_signals",
    "strong_negative_signals",
    "distinguish_from_nav_and_ad",
    "content_vs_video_detail_link",
    "usage_notes",
)
_WEB_DETAIL_NAV_AND_AD_FIELDS = ("detail_link", "menu_nav", "ad_or_promo")
_WEB_CONTENT_VIDEO_DETAIL_LINK_FIELDS = ("to_content_detail", "to_video_detail")
_WEB_SITE_OBSERVATION_FIELDS = (
    "age_gate",
    "ad_overlay",
    "mixed_content",
    "navigation",
    "player",
)
_WEB_PAGE_SIGNAL_FIELDS = (
    "ocr_keywords",
    "dom_hints",
    "layout_hints",
)
_WEB_DETAIL_MEDIA_RULE_KEYS = {
    "content_detail": frozenset(
        {
            "image_gallery_or_viewer",
            "embedded_media_in_body",
            "player_shell_without_resource",
            "confirmed_primary_video_resource",
            "top_level_obstruction",
        }
    ),
    "video_detail": frozenset(
        {
            "single_primary_video_subject",
            "confirmed_video_resource",
            "player_shell_without_resource",
            "background_ad_or_preview_video",
            "top_level_obstruction",
        }
    ),
}
_WEB_DETAIL_CLASSIFICATION_PRINCIPLES = (
    "按主内容判定，不按页面中是否出现过 player 类名或 video 标签的全局计数判定",
    "图片查看器、图集、漫画翻页 → content_detail",
    "正文内嵌少量视频或图片，通常仍为 content_detail",
    "真实视频资源且为页面主体 → video_detail",
    "播放器外壳、类名或按钮不等于已确认视频资源",
    "证据不足或冲突 → unknown，不强判",
)


class WebPageTypeCatalogError(ValueError):
    """Web 页面目录缺失、损坏或超出正式业务闭集。"""


def load_web_page_type_catalog(path: Path | None = None) -> WebPageTypeCatalog:
    """加载 Web 专属目录；失败时禁止回退到 H5 默认目录。"""

    if path is None:
        return _load_default_web_page_type_catalog()
    return _load_and_validate(Path(path))


def _load_default_web_page_type_catalog() -> WebPageTypeCatalog:
    return _load_and_validate(Path(__file__).with_name("web_page_type_catalog.json"))


def _load_and_validate(path: Path) -> WebPageTypeCatalog:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return parse_web_page_type_catalog(payload)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise WebPageTypeCatalogError(f"Web 页面目录加载失败：{exc}") from exc


def parse_web_page_type_catalog(
    payload: Mapping[str, object], *, legacy_frozen: bool = False
) -> WebPageTypeCatalog:
    """复用既有解析器验证冻结正文，不读取当前磁盘目录。"""
    if (
        legacy_frozen
        and isinstance(payload, Mapping)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}\.web\.v[1-4]", str(payload.get("version", "")))
    ):
        return _parse_legacy_web_catalog(payload)
    if (
        legacy_frozen
        and isinstance(payload, Mapping)
        and _WEB_V5_TO_V7_VERSION_PATTERN.fullmatch(str(payload.get("version", "")))
    ):
        return _parse_v5_to_v7_web_catalog(payload)
    try:
        if not isinstance(payload, Mapping):
            raise WebPageTypeCatalogError("Web 页面目录必须为对象")
        legacy_v8 = legacy_frozen and payload.get("version") == _WEB_V8_VERSION
        expected_version = (
            str(payload.get("version"))
            if legacy_frozen
            and payload.get("version")
            in {
                _WEB_V8_VERSION, _WEB_V9_VERSION, _WEB_V10_VERSION,
                _WEB_V11_VERSION, _WEB_V12_VERSION, _WEB_V13_VERSION,
                _WEB_V14_VERSION, _WEB_V15_VERSION, _WEB_V16_VERSION,
                _WEB_V17_VERSION,
            }
            else CURRENT_WEB_CATALOG_VERSION
        )
        expected_codes = web_page_type_codes_for_catalog_version(expected_version)
        if payload.get("version") != expected_version:
            raise WebPageTypeCatalogError(f"Web 页面目录版本必须为 {expected_version}")
        actual_root_fields = set(payload)
        if actual_root_fields != _WEB_V8_CATALOG_ROOT_FIELDS:
            missing = sorted(_WEB_V8_CATALOG_ROOT_FIELDS - actual_root_fields)
            extra = sorted(actual_root_fields - _WEB_V8_CATALOG_ROOT_FIELDS)
            raise WebPageTypeCatalogError(
                f"Web 页面目录顶层字段不完整或包含未知字段：missing={missing}, extra={extra}"
            )
        if payload.get("platform") != "web":
            raise WebPageTypeCatalogError("Web 页面目录 platform 必须为 web")
        definitions = payload.get("page_types")
        if not isinstance(definitions, list) or not all(
            isinstance(item, Mapping) for item in definitions
        ):
            raise WebPageTypeCatalogError("Web 页面目录 page_types 必须为对象数组")
        input_codes = tuple(str(item.get("code")) for item in definitions)
        if input_codes != expected_codes:
            type_count_label = f"{len(expected_codes)} 类页面"
            raise WebPageTypeCatalogError(
                f"Web 页面目录必须按固定顺序定义{type_count_label}：" + ", ".join(expected_codes)
            )
        page_type_groups = _parse_page_type_groups(
            payload.get("page_type_groups"), legacy_v8=legacy_v8
        )
        if page_type_groups.all_codes != expected_codes:
            raise WebPageTypeCatalogError("Web 页面目录 page_type_groups 必须覆盖全部固定页面类型")
        media_rules = _parse_media_rules(payload.get("media_rules"))
        detail_classification_principles = _non_empty_unique_strings(
            payload.get("detail_classification_principles"),
            "detail_classification_principles",
        )
        if detail_classification_principles != _WEB_DETAIL_CLASSIFICATION_PRINCIPLES:
            raise WebPageTypeCatalogError(
                "Web 页面目录 detail_classification_principles 必须为固定六条"
            )
        notes = _non_empty_unique_strings(payload.get("notes"), "notes")
        navigation_signals = _parse_navigation_signals(payload.get("navigation_signals"))
        detail_link_signals = _parse_detail_link_signals(payload.get("detail_link_signals"))
        common_site_observations = _parse_site_observations(payload.get("common_site_observations"))
        recognition_rules = {}
        for definition in definitions:
            recognition_rules[definition["code"]] = parse_web_page_recognition_rules(
                definition.get("recognition_rules")
            )
            status = definition.get("status")
            if not isinstance(status, str) or status not in {"supported", "candidate"}:
                raise WebPageTypeCatalogError("Web 页面目录 status 必须为 supported 或 candidate")
            category = definition.get("category")
            expected_category = _category_for_code(definition["code"], page_type_groups)
            if category != expected_category:
                raise WebPageTypeCatalogError(
                    f"Web 页面目录 {definition['code']}.category 必须为 {expected_category}"
                )
            if not isinstance(definition.get("coverage_required"), bool):
                raise WebPageTypeCatalogError("Web 页面目录 coverage_required 必须为布尔值")
            if (
                definition["code"]
                in (
                    "activity",
                    "search_page",
                    "collection_page",
                    *page_type_groups.lifecycle,
                    *page_type_groups.evidence_only,
                    *page_type_groups.neither,
                )
                and definition["coverage_required"]
            ):
                raise WebPageTypeCatalogError("Web 非必达或非业务页面不得要求业务覆盖")
            signals = definition.get("signals")
            if not isinstance(signals, Mapping) or set(signals) != set(_WEB_PAGE_SIGNAL_FIELDS):
                raise WebPageTypeCatalogError("Web 页面目录 signals 必须包含完整字符串数组字段")
            for signal_name in _WEB_PAGE_SIGNAL_FIELDS:
                _non_empty_unique_strings(signals[signal_name], f"signals.{signal_name}")
            navigation_aliases = definition.get("navigation_aliases", [])
            if not isinstance(navigation_aliases, list) or not all(
                isinstance(item, Mapping) for item in navigation_aliases
            ):
                raise WebPageTypeCatalogError("Web 页面目录 navigation_aliases 必须为对象数组")
            if not isinstance(definition.get("purpose"), str) or not definition["purpose"].strip():
                raise WebPageTypeCatalogError("Web 页面目录 purpose 必须为非空字符串")
            for name in (
                "required_positive_signals",
                "supporting_signals",
                "strong_negative_signals",
                "confusable_with",
                "missing_evidence_actions",
            ):
                value = definition.get(name)
                if (
                    not isinstance(value, list)
                    or not value
                    or not all(isinstance(item, str) and item.strip() for item in value)
                ):
                    raise WebPageTypeCatalogError(f"Web 页面目录 {name} 必须为非空规则数组")
                if len(value) != len(set(value)):
                    raise WebPageTypeCatalogError(f"Web 页面目录 {name} 规则标识冲突")
        if payload.get("schema_version") != "page-type-catalog.v2":
            raise WebPageTypeCatalogError("Web 页面目录 schema_version 不受支持")
        core_payload = {
            "schema_version": payload["schema_version"],
            "version": payload["version"],
            "source": payload["source"],
            "page_types": [dict(item) for item in definitions],
        }
        core = _parse_catalog(core_payload)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise WebPageTypeCatalogError(f"Web 页面目录加载失败：{exc}") from exc
    if core.codes != expected_codes:
        raise WebPageTypeCatalogError(
            "Web 页面目录必须且只能定义固定页面类型：" + ", ".join(expected_codes)
        )
    required_guide_fields = (
        "purpose",
        "required_positive_signals",
        "supporting_signals",
        "strong_negative_signals",
        "confusable_with",
        "missing_evidence_actions",
    )
    for definition in core.page_types:
        if set(definition.required_positive_signals) & set(definition.strong_negative_signals):
            raise WebPageTypeCatalogError(f"Web 页面目录 {definition.code} 正反证规则冲突")
        for field_name in required_guide_fields:
            if not getattr(definition, field_name):
                raise WebPageTypeCatalogError(f"Web 页面目录 {definition.code} 缺少 {field_name}")
        if definition.code in definition.confusable_with or not set(
            definition.confusable_with
        ).issubset(expected_codes):
            raise WebPageTypeCatalogError(f"Web 页面目录 {definition.code} 的 confusable_with 非法")
    return WebPageTypeCatalog(
        core=core,
        platform="web",
        navigation_signals=navigation_signals,
        detail_link_signals=detail_link_signals,
        common_site_observations=common_site_observations,
        page_type_groups=page_type_groups,
        media_rules=media_rules,
        detail_classification_principles=detail_classification_principles,
        notes=notes,
        recognition_rules=MappingProxyType(recognition_rules),
    )


def _parse_legacy_web_catalog(payload: Mapping[str, object]) -> WebPageTypeCatalog:
    """按原发布正文恢复旧四类；缺失的新知识字段保持缺失，不读当前配置。"""
    core_fields = {"schema_version", "version", "source", "page_types"}
    legacy_root_fields = _WEB_V5_TO_V7_CATALOG_ROOT_FIELDS - {"detail_link_signals"}
    if set(payload) not in (core_fields, legacy_root_fields):
        raise WebPageTypeCatalogError("历史 Web 目录根字段不符合冻结版本")
    if "platform" in payload and payload["platform"] != "web":
        raise WebPageTypeCatalogError("历史 Web platform 必须为 web")
    core = _parse_catalog({key: payload[key] for key in core_fields})
    PageTypeRegistry(core)
    if core.codes != ("video_list", "video_detail", "content_list", "content_detail"):
        raise WebPageTypeCatalogError("历史 Web 目录必须为固定四类")
    return WebPageTypeCatalog(
        core=core,
        platform="web",
        navigation_signals=(
            _parse_legacy_navigation_signals(payload["navigation_signals"])
            if "navigation_signals" in payload
            else None
        ),
        detail_link_signals=None,
        common_site_observations=(
            _parse_site_observations(payload["common_site_observations"])
            if "common_site_observations" in payload
            else None
        ),
    )


def _parse_v5_to_v7_web_catalog(payload: Mapping[str, object]) -> WebPageTypeCatalog:
    """按冻结正文恢复 v5 至 v7 的五类目录，不引入 v8 专属知识。"""
    legacy_root_fields = _WEB_V5_TO_V7_CATALOG_ROOT_FIELDS - {"detail_link_signals"}
    if set(payload) not in (_WEB_V5_TO_V7_CATALOG_ROOT_FIELDS, legacy_root_fields):
        raise WebPageTypeCatalogError("历史 Web 五类目录根字段不符合冻结版本")
    if payload.get("platform") != "web":
        raise WebPageTypeCatalogError("历史 Web platform 必须为 web")
    core = _parse_catalog(
        {
            "schema_version": payload["schema_version"],
            "version": payload["version"],
            "source": payload["source"],
            "page_types": payload["page_types"],
        }
    )
    # 历史冻结目录没有现代识别指南字段，按发布时的五类结构校验。
    if core.schema_version != "page-type-catalog.v2":
        raise WebPageTypeCatalogError("历史 Web 五类目录 schema_version 无效")
    if any(
        definition.status != "supported"
        or definition.category != "business"
        or not definition.coverage_required
        or not all(
            (
                definition.signals.ocr_keywords,
                definition.signals.dom_hints,
                definition.signals.layout_hints,
            )
        )
        for definition in core.page_types
    ):
        raise WebPageTypeCatalogError("历史 Web 五类目录定义无效")
    if core.codes != _WEB_V5_TO_V7_TYPE_CODES:
        raise WebPageTypeCatalogError("历史 Web 五类目录必须为固定五类")
    return WebPageTypeCatalog(
        core=core,
        platform="web",
        navigation_signals=_parse_navigation_signals(payload["navigation_signals"]),
        detail_link_signals=(
            _parse_detail_link_signals(payload["detail_link_signals"])
            if "detail_link_signals" in payload
            else None
        ),
        common_site_observations=_parse_site_observations(payload["common_site_observations"]),
        recognition_rules=MappingProxyType(
            {
                definition["code"]: parse_web_page_recognition_rules(
                    definition["recognition_rules"]
                )
                for definition in payload["page_types"]
            }
        ),
    )


def _parse_navigation_signals(value: object) -> WebNavigationSignals:
    if not isinstance(value, Mapping) or set(value) != set(_WEB_NAVIGATION_SIGNAL_FIELDS):
        raise WebPageTypeCatalogError("Web 页面目录 navigation_signals 字段无效")
    parsed = {}
    for field_name in (
        "common_positive_signals",
        "supporting_signals",
        "strong_negative_signals",
        "usage_notes",
    ):
        parsed[field_name] = _non_empty_unique_strings(
            value[field_name], f"navigation_signals.{field_name}"
        )
    collapsed = value["collapsed_and_visible_nav"]
    if not isinstance(collapsed, Mapping) or set(collapsed) != set(
        _WEB_COLLAPSED_NAVIGATION_FIELDS
    ):
        raise WebPageTypeCatalogError(
            "Web 页面目录 navigation_signals.collapsed_and_visible_nav 字段无效"
        )
    description = collapsed["description"]
    if not isinstance(description, str) or not description.strip():
        raise WebPageTypeCatalogError(
            "Web 页面目录 navigation_signals.collapsed_and_visible_nav.description 必须为非空字符串"
        )
    parsed["collapsed_and_visible_nav"] = WebCollapsedAndVisibleNav(
        description=description,
        rules=_non_empty_unique_strings(
            collapsed["rules"], "navigation_signals.collapsed_and_visible_nav.rules"
        ),
    )
    parsed["distinguish_from_breadcrumb"] = _parse_string_tuple_mapping(
        value["distinguish_from_breadcrumb"],
        field_name="navigation_signals.distinguish_from_breadcrumb",
        expected_fields=_WEB_NAVIGATION_BREADCRUMB_FIELDS,
    )
    return WebNavigationSignals(**parsed)


def _parse_legacy_navigation_signals(value: object) -> WebNavigationSignals | None:
    """校验旧导航合同，但不把缺失的 v6 知识合成到冻结目录。"""

    if isinstance(value, Mapping) and set(value) == set(_WEB_LEGACY_NAVIGATION_SIGNAL_FIELDS):
        for field_name in _WEB_LEGACY_NAVIGATION_SIGNAL_FIELDS:
            _non_empty_unique_strings(value[field_name], f"navigation_signals.{field_name}")
        return None
    return _parse_navigation_signals(value)


def _parse_detail_link_signals(value: object) -> WebDetailLinkSignals:
    if not isinstance(value, Mapping) or set(value) != set(_WEB_DETAIL_LINK_SIGNAL_FIELDS):
        raise WebPageTypeCatalogError("Web 页面目录 detail_link_signals 字段无效")
    purpose = value["purpose"]
    if not isinstance(purpose, str) or not purpose.strip():
        raise WebPageTypeCatalogError("Web 页面目录 detail_link_signals.purpose 必须为非空字符串")
    return WebDetailLinkSignals(
        purpose=purpose,
        required_positive_signals=_non_empty_unique_strings(
            value["required_positive_signals"], "detail_link_signals.required_positive_signals"
        ),
        supporting_signals=_non_empty_unique_strings(
            value["supporting_signals"], "detail_link_signals.supporting_signals"
        ),
        strong_negative_signals=_non_empty_unique_strings(
            value["strong_negative_signals"], "detail_link_signals.strong_negative_signals"
        ),
        distinguish_from_nav_and_ad=_parse_string_tuple_mapping(
            value["distinguish_from_nav_and_ad"],
            field_name="detail_link_signals.distinguish_from_nav_and_ad",
            expected_fields=_WEB_DETAIL_NAV_AND_AD_FIELDS,
        ),
        content_vs_video_detail_link=_parse_string_tuple_mapping(
            value["content_vs_video_detail_link"],
            field_name="detail_link_signals.content_vs_video_detail_link",
            expected_fields=_WEB_CONTENT_VIDEO_DETAIL_LINK_FIELDS,
        ),
        usage_notes=_non_empty_unique_strings(
            value["usage_notes"], "detail_link_signals.usage_notes"
        ),
    )


def _parse_page_type_groups(value: object, *, legacy_v8: bool = False) -> WebPageTypeGroups:
    expected_fields = (
        ("business", "lifecycle", "neither")
        if legacy_v8
        else ("business", "lifecycle", "evidence_only", "neither")
    )
    if not isinstance(value, Mapping) or set(value) != set(expected_fields):
        raise WebPageTypeCatalogError("Web 页面目录 page_type_groups 字段无效")
    groups = WebPageTypeGroups(
        business=_non_empty_unique_strings(value["business"], "page_type_groups.business"),
        lifecycle=_non_empty_unique_strings(value["lifecycle"], "page_type_groups.lifecycle"),
        evidence_only=(
            ()
            if legacy_v8
            else _non_empty_unique_strings(value["evidence_only"], "page_type_groups.evidence_only")
        ),
        neither=_non_empty_unique_strings(value["neither"], "page_type_groups.neither"),
    )
    if len(groups.all_codes) != len(set(groups.all_codes)):
        raise WebPageTypeCatalogError("Web 页面目录 page_type_groups 不得包含重复页面类型")
    return groups


def _category_for_code(code: str, groups: WebPageTypeGroups) -> str:
    if code in groups.business:
        return "business"
    if code in groups.lifecycle:
        return "lifecycle"
    if code in groups.evidence_only:
        return "evidence_only"
    if code in groups.neither:
        return "neither"
    raise WebPageTypeCatalogError(f"Web 页面目录 {code} 未被 page_type_groups 覆盖")


def _parse_media_rules(value: object) -> Mapping[str, Mapping[str, str]]:
    if not isinstance(value, Mapping) or set(value) != set(_WEB_DETAIL_MEDIA_RULE_KEYS):
        raise WebPageTypeCatalogError("Web 页面目录 media_rules 必须包含两个详情页面")
    parsed: dict[str, Mapping[str, str]] = {}
    for code, expected_rule_keys in _WEB_DETAIL_MEDIA_RULE_KEYS.items():
        rules = value[code]
        if not isinstance(rules, Mapping) or set(rules) != expected_rule_keys:
            raise WebPageTypeCatalogError(f"Web 页面目录 media_rules.{code} 必须包含固定完整规则键")
        if not all(
            isinstance(key, str) and key.strip() and isinstance(rule, str) and rule.strip()
            for key, rule in rules.items()
        ):
            raise WebPageTypeCatalogError(f"Web 页面目录 media_rules.{code} 必须为非空字符串映射")
        parsed[code] = MappingProxyType(dict(rules))
    return MappingProxyType(parsed)


def _parse_string_tuple_mapping(
    value: object,
    *,
    field_name: str,
    expected_fields: tuple[str, ...],
) -> Mapping[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or set(value) != set(expected_fields):
        raise WebPageTypeCatalogError(f"Web 页面目录 {field_name} 字段无效")
    return MappingProxyType(
        {
            name: _non_empty_unique_strings(value[name], f"{field_name}.{name}")
            for name in expected_fields
        }
    )


def _parse_site_observations(value: object) -> WebCommonSiteObservations:
    if not isinstance(value, Mapping) or set(value) != set(_WEB_SITE_OBSERVATION_FIELDS):
        raise WebPageTypeCatalogError("Web 页面目录 common_site_observations 字段无效")
    parsed: dict[str, str] = {}
    for field_name in _WEB_SITE_OBSERVATION_FIELDS:
        field_value = value[field_name]
        if not isinstance(field_value, str) or not field_value.strip():
            raise WebPageTypeCatalogError(
                f"Web 页面目录 common_site_observations.{field_name} 必须为非空字符串"
            )
        parsed[field_name] = field_value
    return WebCommonSiteObservations(**parsed)


def _non_empty_unique_strings(value: object, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item.strip() for item in value)
        or len(value) != len(set(value))
    ):
        raise WebPageTypeCatalogError(f"Web 页面目录 {field_name} 必须为非空且不重复的字符串数组")
    return tuple(value)


__all__ = [
    "WEB_BUSINESS_PAGE_TYPE_CODES",
    "WEB_EVIDENCE_ONLY_PAGE_TYPE_CODES",
    "WEB_LIFECYCLE_PAGE_TYPE_CODES",
    "WEB_NEITHER_PAGE_TYPE_CODES",
    "WEB_PAGE_TYPE_CODES",
    "WEB_V3_BUSINESS_PAGE_TYPE_CODES",
    "WebPageTypeCatalog",
    "WebPageTypeCatalogError",
    "load_web_page_type_catalog",
    "parse_web_page_type_catalog",
]
