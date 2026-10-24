"""页面类型知识库加载器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

PageTypeStatus = Literal["supported", "candidate"]
PageTypeCategory = Literal["business", "lifecycle"]


class InvalidPageTypeCodeError(ValueError):
    """运行时传入的页面类型代码不属于Catalog。"""

    def __init__(self, page_type: str, allowed_page_types: tuple[str, ...]) -> None:
        self.page_type = page_type
        self.allowed_page_types = allowed_page_types
        super().__init__(
            f"未知页面类型代码：{page_type}；允许值：{', '.join(allowed_page_types)}"
        )


@dataclass(frozen=True)
class SourceMetadata:
    workbook_name: str
    sheets: tuple[str, ...]
    extraction_method: str


@dataclass(frozen=True)
class PageSignals:
    ocr_keywords: tuple[str, ...]
    dom_hints: tuple[str, ...]
    layout_hints: tuple[str, ...]


@dataclass(frozen=True)
class EntryHint:
    from_page_type: str
    action_intent: str
    target_keywords: tuple[str, ...]


@dataclass(frozen=True)
class NextPageCandidate:
    page_type: str
    probability: str
    action_intent: str
    target_keywords: tuple[str, ...]
    risk_level: str


@dataclass(frozen=True)
class NavigationAliasDefinition:
    subtype: str | None
    terms: tuple[str, ...]
    source_category: str
    confidence: float


@dataclass(frozen=True)
class PageTypeDefinition:
    code: str
    name: str
    status: PageTypeStatus
    category: PageTypeCategory
    coverage_required: bool
    runtime_traits: tuple[str, ...]
    aliases: tuple[str, ...]
    signals: PageSignals
    entry_hints: tuple[EntryHint, ...]
    exit_hints: tuple[str, ...]
    next_page_candidates: tuple[NextPageCandidate, ...]
    risk_actions: tuple[str, ...]
    related_rules: tuple[str, ...]
    rule_matchable: bool
    route_prefixes: tuple[str, ...]
    navigation_aliases: tuple[NavigationAliasDefinition, ...]
    default_detail_page_type: str | None
    source: str
    remark: str
    purpose: str = ""
    required_positive_signals: tuple[str, ...] = ()
    supporting_signals: tuple[str, ...] = ()
    strong_negative_signals: tuple[str, ...] = ()
    confusable_with: tuple[str, ...] = ()
    missing_evidence_actions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageTypeCatalog:
    schema_version: str
    version: str
    source: SourceMetadata
    page_types: tuple[PageTypeDefinition, ...]

    @property
    def supported_page_types(self) -> dict[str, PageTypeDefinition]:
        return {item.name: item for item in self.page_types if item.status == "supported"}

    @property
    def candidate_page_types(self) -> dict[str, PageTypeDefinition]:
        return {item.name: item for item in self.page_types if item.status == "candidate"}

    @property
    def codes(self) -> tuple[str, ...]:
        """返回Catalog定义的标准页面类型代码。"""
        return tuple(item.code for item in self.page_types)

    def require(self, name: str) -> PageTypeDefinition:
        for item in self.page_types:
            if item.name == name or name in item.aliases:
                return item
        raise KeyError(f"未知页面类型: {name}")

    def require_code(self, code: str) -> PageTypeDefinition:
        """严格按标准代码查询，不接受名称或历史别名。"""
        for item in self.page_types:
            if item.code == code:
                return item
        raise InvalidPageTypeCodeError(code, self.codes)


def load_default_page_type_catalog() -> PageTypeCatalog:
    """兼容旧调用方，目录加载统一委托给默认 Registry。"""
    from services.api.app.domain.page.registry import get_default_page_type_registry

    return get_default_page_type_registry().catalog


def load_page_type_catalog(path: Path) -> PageTypeCatalog:
    """从指定 JSON 文件加载页面类型目录，供 Registry 与测试复用。"""
    with path.open(encoding="utf-8") as catalog_file:
        payload = json.load(catalog_file)
    return _parse_catalog(payload)


def _parse_catalog(payload: dict[str, Any]) -> PageTypeCatalog:
    source_payload = payload["source"]
    return PageTypeCatalog(
        schema_version=str(payload["schema_version"]),
        version=str(payload["version"]),
        source=SourceMetadata(
            workbook_name=str(source_payload["workbook_name"]),
            sheets=_as_tuple(source_payload.get("sheets")),
            extraction_method=str(source_payload["extraction_method"]),
        ),
        page_types=tuple(_parse_page_type(item) for item in payload["page_types"]),
    )


def _parse_page_type(payload: dict[str, Any]) -> PageTypeDefinition:
    return PageTypeDefinition(
        code=str(payload["code"]),
        name=str(payload["name"]),
        status=payload["status"],
        category=payload["category"],
        coverage_required=_read_bool(payload, "coverage_required"),
        runtime_traits=_as_tuple(payload.get("runtime_traits")),
        aliases=_as_tuple(payload.get("aliases")),
        signals=PageSignals(
            ocr_keywords=_as_tuple(payload["signals"].get("ocr_keywords")),
            dom_hints=_as_tuple(payload["signals"].get("dom_hints")),
            layout_hints=_as_tuple(payload["signals"].get("layout_hints")),
        ),
        entry_hints=tuple(_parse_entry_hint(item) for item in payload.get("entry_hints", [])),
        exit_hints=_as_tuple(payload.get("exit_hints")),
        next_page_candidates=tuple(
            _parse_next_page_candidate(item) for item in payload.get("next_page_candidates", [])
        ),
        risk_actions=_as_tuple(payload.get("risk_actions")),
        related_rules=_as_tuple(payload.get("related_rules")),
        rule_matchable=_read_bool(payload, "rule_matchable", default=True),
        route_prefixes=_as_tuple(payload.get("route_prefixes")),
        navigation_aliases=tuple(
            _parse_navigation_alias(item) for item in payload.get("navigation_aliases", [])
        ),
        default_detail_page_type=(
            str(payload["default_detail_page_type"])
            if payload.get("default_detail_page_type") is not None
            else None
        ),
        source=str(payload.get("source", "")),
        remark=str(payload.get("remark", "")),
        purpose=str(payload.get("purpose", "")),
        required_positive_signals=_as_tuple(payload.get("required_positive_signals")),
        supporting_signals=_as_tuple(payload.get("supporting_signals")),
        strong_negative_signals=_as_tuple(payload.get("strong_negative_signals")),
        confusable_with=_as_tuple(payload.get("confusable_with")),
        missing_evidence_actions=_as_tuple(payload.get("missing_evidence_actions")),
    )


def _parse_entry_hint(payload: dict[str, Any]) -> EntryHint:
    return EntryHint(
        from_page_type=str(payload["from_page_type"]),
        action_intent=str(payload["action_intent"]),
        target_keywords=_as_tuple(payload.get("target_keywords")),
    )


def _parse_next_page_candidate(payload: dict[str, Any]) -> NextPageCandidate:
    return NextPageCandidate(
        page_type=str(payload["page_type"]),
        probability=str(payload["probability"]),
        action_intent=str(payload["action_intent"]),
        target_keywords=_as_tuple(payload.get("target_keywords")),
        risk_level=str(payload.get("risk_level", "low")),
    )


def _parse_navigation_alias(payload: dict[str, Any]) -> NavigationAliasDefinition:
    return NavigationAliasDefinition(
        subtype=(str(payload["subtype"]) if payload.get("subtype") is not None else None),
        terms=_as_tuple(payload.get("terms")),
        source_category=str(payload.get("source_category", "")),
        confidence=float(payload.get("confidence", 0.95)),
    )


def _read_bool(payload: dict[str, Any], key: str, *, default: bool | None = None) -> bool:
    if key not in payload:
        if default is None:
            raise ValueError(f"{key} 为必填布尔字段")
        return default
    value = payload[key]
    if not isinstance(value, bool):
        raise ValueError(f"{key} 必须为布尔值")
    return value


def _as_tuple(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)
