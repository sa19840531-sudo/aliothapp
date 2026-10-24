"""主体分类审计与不含站点身份的结构指纹。"""

import hashlib
import json
from dataclasses import asdict, replace

from services.api.app.application.web_subject_region_evidence import SEMANTIC_TOKENS, SUMMARY_ROLES
from services.api.app.application.web_subject_region_extraction import extract_regions
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.application.web_subject_region_scopes import VENDOR_TOKENS


def subject_structure_fingerprint(facts: WebSubjectRegionFacts) -> str:
    """结构簇只比较区域类型、数量和语义证据，不比较选择器或文本。"""
    shape = asdict(facts)
    shape.pop("selector_summaries")
    shape.pop("player_vendor_tokens")
    shape.pop("breadcrumb_count")
    shape.pop("detail_navigation_source_count")
    shape["token_summaries"] = [
        (role, tuple(sorted(set(values) & (SEMANTIC_TOKENS - VENDOR_TOKENS))))
        for role, values in facts.token_summaries
        if role in SUMMARY_ROLES - {"breadcrumb", "detail_navigation_source"}
    ]
    encoded = json.dumps(shape, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def subject_classification_audit(facts: WebSubjectRegionFacts | None) -> dict[str, object]:
    """同一可信事实用于评估载荷、元数据与报告；历史缺失保持缺失。"""
    if facts is None:
        return {}
    return {
        "subject_region_facts": asdict(facts),
        "structure_fingerprint": subject_structure_fingerprint(facts),
    }


def with_detail_navigation_source(
    facts: WebSubjectRegionFacts | None, selector: str
) -> WebSubjectRegionFacts | None:
    """仅由已完成可信详情导航的调用方附加来源，不凭 URL 或模型提示推断。"""
    if facts is None:
        return None
    source = extract_regions(
        [], navigation_sources=({"selector": selector, "candidate_role": "detail"},)
    )
    summaries = {}
    for field in ("selector_summaries", "token_summaries"):
        groups = dict(getattr(facts, field))
        groups["detail_navigation_source"] = dict(getattr(source, field))[
            "detail_navigation_source"
        ]
        summaries[field] = tuple(sorted(groups.items()))
    return replace(
        facts, detail_navigation_source_count=source.detail_navigation_source_count, **summaries
    )
