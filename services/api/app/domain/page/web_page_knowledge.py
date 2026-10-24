"""Web 页面目录的专属知识模型。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from services.api.app.domain.page.catalog import (
    PageTypeCatalog,
    PageTypeDefinition,
    SourceMetadata,
)


@dataclass(frozen=True, kw_only=True)
class WebCollapsedAndVisibleNav:
    """默认收起与当前可见导航的判定边界。"""

    description: str
    rules: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class WebNavigationSignals:
    """跨页型共享的 Web 导航识别知识。"""

    common_positive_signals: tuple[str, ...]
    supporting_signals: tuple[str, ...]
    collapsed_and_visible_nav: WebCollapsedAndVisibleNav
    strong_negative_signals: tuple[str, ...]
    distinguish_from_breadcrumb: Mapping[str, tuple[str, ...]]
    usage_notes: tuple[str, ...]


@dataclass(frozen=True, kw_only=True)
class WebDetailLinkSignals:
    """列表或菜单页中的详情入口识别知识。"""

    purpose: str
    required_positive_signals: tuple[str, ...]
    supporting_signals: tuple[str, ...]
    strong_negative_signals: tuple[str, ...]
    distinguish_from_nav_and_ad: Mapping[str, tuple[str, ...]]
    content_vs_video_detail_link: Mapping[str, tuple[str, ...]]
    usage_notes: tuple[str, ...]


@dataclass(frozen=True)
class WebCommonSiteObservations:
    """Web 站点采集阶段需要解释的常见现象。"""

    age_gate: str
    ad_overlay: str
    mixed_content: str
    navigation: str
    player: str


@dataclass(frozen=True, slots=True)
class WebPageTypeGroups:
    """Web 页面类型的业务、生命周期、证据专用和兜底分组。"""

    business: tuple[str, ...]
    lifecycle: tuple[str, ...]
    evidence_only: tuple[str, ...]
    neither: tuple[str, ...]

    @property
    def all_codes(self) -> tuple[str, ...]:
        return (*self.business, *self.lifecycle, *self.evidence_only, *self.neither)


WebPageMediaRules = Mapping[str, Mapping[str, str]]


@dataclass(frozen=True)
class WebPageTypeCatalog:
    """组合通用页型目录与 Web 专属根知识。"""

    core: PageTypeCatalog
    platform: Literal["web"]
    navigation_signals: WebNavigationSignals | None
    detail_link_signals: WebDetailLinkSignals | None
    common_site_observations: WebCommonSiteObservations | None
    page_type_groups: WebPageTypeGroups | None = None
    media_rules: WebPageMediaRules = field(default_factory=lambda: MappingProxyType({}))
    detail_classification_principles: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    recognition_rules: Mapping[str, WebPageRecognitionRules] = field(
        default_factory=lambda: MappingProxyType({})
    )

    @property
    def schema_version(self) -> str:
        return self.core.schema_version

    @property
    def version(self) -> str:
        return self.core.version

    @property
    def source(self) -> SourceMetadata:
        return self.core.source

    @property
    def page_types(self) -> tuple[PageTypeDefinition, ...]:
        return self.core.page_types

    @property
    def codes(self) -> tuple[str, ...]:
        return self.core.codes

    def require(self, name: str) -> PageTypeDefinition:
        return self.core.require(name)

    def require_code(self, code: str) -> PageTypeDefinition:
        return self.core.require_code(code)


WEB_NUMERIC_STRUCTURE_FACTS = frozenset(
    {
        "navigation_element_count",
        "repeated_link_count",
        "video_element_count",
        "single_subject_element_count",
        "audio_element_count",
        "primary_detail_link_count",
        "repeated_primary_card_count",
        "media_card_count",
        "article_body_count",
        "paragraph_text_length",
        "player_root_count",
        "advertisement_element_count",
        "transaction_element_count",
        "top_level_obstruction_count",
        "top_navigation_container_count",
        "visible_category_entry_count",
        "homepage_section_count",
        "repeated_subject_element_count",
        "continuous_video_feed_count",
        "ranked_video_row_count",
        "primary_video_element_count",
        "image_gallery_root_count",
        "image_viewer_control_count",
        "confirmed_primary_video_resource_count",
        "player_shell_without_resource_count",
        "opaque_primary_media_count",
        "search_input_count",
        "search_trigger_count",
        "query_result_group_count",
        "activity_primary_region_count",
        "activity_action_count",
        "primary_subject_count",
        "primary_article_body_count",
        "primary_article_text_length",
        "primary_player_count",
        "inline_article_media_count",
        "secondary_recommendation_card_count",
        "breadcrumb_count",
        "detail_navigation_source_count",
    }
)
WebFactOperator = Literal["eq", "gte", "lte", "contains_exact"]


@dataclass(frozen=True)
class WebFactPredicate:
    """只引用受控事实，禁止执行表达式。"""

    fact: str
    operator: WebFactOperator
    value: bool | int | str

    def __post_init__(self) -> None:
        if self.fact in WEB_NUMERIC_STRUCTURE_FACTS:
            valid = (
                self.operator in {"eq", "gte", "lte"}
                and type(self.value) is int
                and self.value >= 0
            )
        elif self.fact in {"is_canonical_root_url", "active_home_navigation"}:
            valid = self.operator == "eq" and type(self.value) is bool
        elif self.fact == "primary_subject_kind":
            valid = self.operator == "eq" and self.value in {
                "unknown", "conflicting", "video", "article", "gallery"
            }
        elif self.fact in {"dom_tokens", "primary_subject_dom_tokens"}:
            valid = (
                self.operator == "contains_exact"
                and isinstance(self.value, str)
                and bool(self.value.strip())
            )
        else:
            valid = False
        if not valid:
            raise ValueError("Web 结构规则事实、操作符或值类型不受支持")


@dataclass(frozen=True)
class WebPredicateGroup:
    """同一强冲突的全部结构条件。"""

    reason_code: str
    all_of: tuple[WebFactPredicate, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reason_code, str) or not self.reason_code.strip() or not self.all_of:
            raise ValueError("Web 强冲突规则必须包含原因及非空条件")


@dataclass(frozen=True)
class WebPageRecognitionRules:
    """页面组合门槛及可审计的补充、否定事实。"""

    required_all: tuple[WebFactPredicate, ...]
    required_any: tuple[WebFactPredicate, ...]
    supporting: tuple[WebFactPredicate, ...]
    strong_negative_any: tuple[WebPredicateGroup, ...]


def parse_web_page_recognition_rules(value: object) -> WebPageRecognitionRules:
    """拒绝未知键、空门槛和不受控的类型组合。"""

    expected = {"required_all", "required_any", "supporting", "strong_negative_any"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("recognition_rules 必须为完整规则对象")

    def predicates(raw: object, *, allow_empty: bool = False) -> tuple[WebFactPredicate, ...]:
        if not isinstance(raw, list) or (not raw and not allow_empty):
            raise ValueError("结构规则必须为非空条件数组")
        result = []
        for item in raw:
            if not isinstance(item, Mapping) or set(item) != {"fact", "operator", "value"}:
                raise ValueError("结构规则条件字段无效")
            result.append(WebFactPredicate(**item))
        return tuple(result)

    negatives = value["strong_negative_any"]
    if not isinstance(negatives, list):
        raise ValueError("strong_negative_any 必须为数组")
    groups = []
    for item in negatives:
        if not isinstance(item, Mapping) or set(item) != {"reason_code", "all_of"}:
            raise ValueError("强冲突规则字段无效")
        groups.append(WebPredicateGroup(item["reason_code"], predicates(item["all_of"])))
    if len({item.reason_code for item in groups}) != len(groups):
        raise ValueError("强冲突规则原因码不得重复")
    required_all = predicates(value["required_all"])
    required_any = predicates(value["required_any"])
    if any(
        item.fact in {"dom_tokens", "primary_subject_dom_tokens"}
        and item.value in {"card", "item", "detail", "video", "source", "player"}
        for item in (*required_all, *required_any)
    ):
        raise ValueError("宽泛 DOM token 只能作为辅助规则")
    return WebPageRecognitionRules(
        required_all=required_all,
        required_any=required_any,
        supporting=predicates(value["supporting"], allow_empty=True),
        strong_negative_any=tuple(groups),
    )


__all__ = [
    "WebCollapsedAndVisibleNav",
    "WebCommonSiteObservations",
    "WebDetailLinkSignals",
    "WebNavigationSignals",
    "WebPageMediaRules",
    "WebPageTypeCatalog",
    "WebPageTypeGroups",
]
