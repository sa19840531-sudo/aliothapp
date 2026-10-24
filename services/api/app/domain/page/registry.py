"""页面类型目录的统一只读出口。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from services.api.app.domain.page.catalog import (
    PageTypeCatalog,
    PageTypeDefinition,
    load_page_type_catalog,
)

_SUPPORTED_SCHEMA_VERSION = "page-type-catalog.v2"
_PAGE_TYPE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
_RECOGNITION_GUIDE_FIELDS = (
    "required_positive_signals",
    "supporting_signals",
    "strong_negative_signals",
    "confusable_with",
    "missing_evidence_actions",
)
_RECOGNITION_SIGNAL_FIELDS = (
    "required_positive_signals",
    "supporting_signals",
    "strong_negative_signals",
)


class InvalidPageTypeCatalogError(ValueError):
    """页面类型目录结构或跨引用无效。"""


@dataclass(frozen=True)
class PageTypeAliasMatch:
    canonical_code: str
    page_subtype_code: str | None
    matched_term: str
    source_category: str
    confidence: float


class PageTypeRegistry:
    """从唯一 Catalog 派生页面类型分类，避免运行时代码维护重复集合。"""

    def __init__(self, catalog: PageTypeCatalog) -> None:
        self.catalog = catalog
        self._validate()
        normalized = json.dumps(
            asdict(catalog),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.content_sha256 = f"sha256:{hashlib.sha256(normalized).hexdigest()}"

    @classmethod
    def from_path(cls, path: Path) -> PageTypeRegistry:
        return cls(load_page_type_catalog(path))

    @property
    def all_codes(self) -> tuple[str, ...]:
        return tuple(page_type.code for page_type in self.catalog.page_types)

    @property
    def business_codes(self) -> tuple[str, ...]:
        return tuple(
            page_type.code
            for page_type in self.catalog.page_types
            if page_type.category == "business"
        )

    def require_code(self, code: str):
        return self.catalog.require_code(code)

    def resolve_alias(self, label: str) -> PageTypeAliasMatch | None:
        normalized_label = _normalize_alias(label)
        if not normalized_label:
            return None
        for page_type in self.catalog.page_types:
            for alias in page_type.navigation_aliases:
                for term in alias.terms:
                    if normalized_label == _normalize_alias(term):
                        return PageTypeAliasMatch(
                            canonical_code=page_type.code,
                            page_subtype_code=alias.subtype,
                            matched_term=term,
                            source_category=alias.source_category,
                            confidence=alias.confidence,
                        )
        return None

    def resolve_route_prefix(self, prefix: str) -> str | None:
        normalized = prefix.strip().casefold()
        for page_type in self.catalog.page_types:
            if normalized in {item.casefold() for item in page_type.route_prefixes}:
                return page_type.code
        return None

    def default_detail_type(self, code: str) -> str | None:
        return self.require_code(code).default_detail_page_type

    def build_agent_context(self) -> dict[str, object]:
        return {
            "catalog_version": self.catalog.version,
            "catalog_sha256": self.content_sha256,
            "all_page_types": list(self.all_codes),
            "coverage_required_page_types": list(self.coverage_required_codes),
            "lifecycle_page_types": list(self.lifecycle_codes),
            "rule_matchable_page_types": list(self.rule_matchable_codes),
        }

    @property
    def lifecycle_codes(self) -> tuple[str, ...]:
        return tuple(
            page_type.code
            for page_type in self.catalog.page_types
            if page_type.category == "lifecycle"
        )

    @property
    def coverage_required_codes(self) -> tuple[str, ...]:
        return tuple(
            page_type.code for page_type in self.catalog.page_types if page_type.coverage_required
        )

    @property
    def transient_codes(self) -> tuple[str, ...]:
        return tuple(
            page_type.code
            for page_type in self.catalog.page_types
            if "transient" in page_type.runtime_traits
        )

    @property
    def rule_matchable_codes(self) -> tuple[str, ...]:
        return tuple(
            page_type.code for page_type in self.catalog.page_types if page_type.rule_matchable
        )

    def _validate(self) -> None:
        if self.catalog.schema_version != _SUPPORTED_SCHEMA_VERSION:
            raise InvalidPageTypeCatalogError(
                "不支持的 schema_version："
                f"{self.catalog.schema_version}；预期 {_SUPPORTED_SCHEMA_VERSION}"
            )
        codes = self.all_codes
        if not codes:
            raise InvalidPageTypeCatalogError("页面类型目录不能为空")
        duplicate_codes = sorted({code for code in codes if codes.count(code) > 1})
        if duplicate_codes:
            raise InvalidPageTypeCatalogError(f"页面类型 code 重复：{', '.join(duplicate_codes)}")
        allowed_codes = set(codes)
        route_targets: dict[str, str] = {}
        alias_targets: dict[str, str] = {}
        for page_type in self.catalog.page_types:
            if _PAGE_TYPE_CODE_PATTERN.fullmatch(page_type.code) is None:
                raise InvalidPageTypeCatalogError(
                    f"页面类型 code 格式无效：{page_type.code}"
                )
            if page_type.category not in {"business", "lifecycle"}:
                raise InvalidPageTypeCatalogError(
                    f"{page_type.code}.category 无效：{page_type.category}"
                )
            if page_type.coverage_required and page_type.category != "business":
                raise InvalidPageTypeCatalogError(
                    f"{page_type.code}.coverage_required 只能用于 business 类型"
                )
            if "transient" in page_type.runtime_traits and page_type.coverage_required:
                raise InvalidPageTypeCatalogError(
                    f"{page_type.code} 具有 transient 特征，不能要求业务覆盖"
                )
            if not page_type.purpose.strip():
                raise InvalidPageTypeCatalogError(f"{page_type.code}.purpose 不能为空")
            for field_name in _RECOGNITION_GUIDE_FIELDS:
                values = getattr(page_type, field_name)
                if not values or any(not value.strip() for value in values):
                    raise InvalidPageTypeCatalogError(
                        f"{page_type.code}.{field_name} 必须为非空规则数组"
                    )
                if len(values) != len(set(values)):
                    raise InvalidPageTypeCatalogError(
                        f"{page_type.code}.{field_name} 存在重复规则"
                    )
            positive_signals = set(page_type.required_positive_signals) | set(
                page_type.supporting_signals
            )
            conflicting_signals = positive_signals & set(page_type.strong_negative_signals)
            if conflicting_signals:
                raise InvalidPageTypeCatalogError(
                    f"{page_type.code} 的正证与强反证冲突："
                    + ", ".join(sorted(conflicting_signals))
                )
            signal_references = recognition_signal_references(page_type)
            signal_ids = [
                item["signal_id"]
                for references in signal_references.values()
                for item in references
            ]
            if len(signal_ids) != len(set(signal_ids)):
                raise InvalidPageTypeCatalogError(f"{page_type.code} 的判别信号规范化后重复")
            if page_type.code in page_type.confusable_with or not set(
                page_type.confusable_with
            ).issubset(allowed_codes):
                raise InvalidPageTypeCatalogError(
                    f"{page_type.code}.confusable_with 引用了自身或未知类型"
                )
            detail_type = page_type.default_detail_page_type
            if detail_type is not None and detail_type not in allowed_codes:
                raise InvalidPageTypeCatalogError(
                    f"{page_type.code}.default_detail_page_type 引用了未知类型：{detail_type}"
                )
            for entry_hint in page_type.entry_hints:
                if (
                    entry_hint.from_page_type != "external"
                    and entry_hint.from_page_type not in allowed_codes
                ):
                    raise InvalidPageTypeCatalogError(
                        f"{page_type.code}.entry_hints 引用了未知来源类型："
                        f"{entry_hint.from_page_type}"
                    )
            for candidate in page_type.next_page_candidates:
                if candidate.page_type not in allowed_codes:
                    raise InvalidPageTypeCatalogError(
                        f"{page_type.code}.next_page_candidates 引用了未知类型："
                        f"{candidate.page_type}"
                    )
            for route_prefix in page_type.route_prefixes:
                normalized_route = route_prefix.strip().casefold()
                previous_route_target = route_targets.setdefault(normalized_route, page_type.code)
                if previous_route_target != page_type.code:
                    raise InvalidPageTypeCatalogError(
                        f"路由前缀 {route_prefix} 同时映射到 {previous_route_target}"
                        f" 和 {page_type.code}"
                    )
            for alias in page_type.navigation_aliases:
                for term in alias.terms:
                    normalized_alias = _normalize_alias(term)
                    previous_alias_target = alias_targets.setdefault(
                        normalized_alias, page_type.code
                    )
                    if previous_alias_target != page_type.code:
                        raise InvalidPageTypeCatalogError(
                            f"导航别名 {term} 同时映射到 {previous_alias_target}"
                            f" 和 {page_type.code}"
                        )


def recognition_signal_references(
    page_type: PageTypeDefinition,
) -> dict[str, list[dict[str, str]]]:
    """为页面类型判别信号生成与数组顺序无关的稳定引用。"""
    references: dict[str, list[dict[str, str]]] = {}
    for field_name in _RECOGNITION_SIGNAL_FIELDS:
        references[field_name] = [
            {
                "signal_id": _recognition_signal_id(
                    page_type_code=page_type.code,
                    field_name=field_name,
                    signal=signal,
                ),
                "text": signal,
            }
            for signal in getattr(page_type, field_name)
        ]
    return references


def _recognition_signal_id(
    *,
    page_type_code: str,
    field_name: str,
    signal: str,
) -> str:
    normalized_signal = re.sub(r"\s+", " ", signal.strip()).casefold()
    digest = hashlib.sha256(f"{field_name}\0{normalized_signal}".encode()).hexdigest()[:16]
    return f"{page_type_code}.{field_name}.{digest}"


def _normalize_alias(value: str) -> str:
    normalized = re.sub(r"^text\s*=\s*", "", value.strip(), flags=re.IGNORECASE)
    normalized = normalized.strip("'\"")
    return re.sub(r"\s+", "", normalized).casefold()


@lru_cache
def get_default_page_type_registry() -> PageTypeRegistry:
    catalog_path = Path(__file__).with_name("page_type_catalog.json")
    return PageTypeRegistry.from_path(catalog_path)
