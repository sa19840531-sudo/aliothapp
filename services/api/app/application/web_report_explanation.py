"""Web 报告有界公开解释；与稳定页画廊、覆盖计算和原判定严格分离。"""

import json
import math
import re

from services.api.app.application.web_readiness_contracts import WebReadinessStatus
from services.api.app.application.web_subject_region_evidence import SEMANTIC_TOKENS, SUMMARY_ROLES
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.application.web_subject_region_scopes import VENDOR_TOKENS
from services.api.app.domain.page.web_page_catalog import WEB_PAGE_TYPE_CODES

_READINESS_FIELDS = frozenset({"readiness_status", "readiness_exit_reason", "page_state"})
_READINESS_VALUES = frozenset(item.value for item in WebReadinessStatus)

_FIELDS = frozenset(
    """schema_version capture_id anchor_capture_id sequence_id purpose
    reason_code status publication_id rule_id rule_code work_item_id work_status
    contract_hash evidence_input_set_id input_scope input_set_hash input_set_status
    attempt_id observation_packet_id execution_status rule_execution_id consumer_agent_run_id
    selected_attempt_id selection retryable diagnostic_type finalization_epoch
    capture_role captured_at ordinal duration_ms sample_count target_id occurrence_id
    action_execution_id parent_capture_id direct_parent_capture_id scroll_count
    before_ad_coverage after_ad_coverage phase fallback_capture_id
    revision_id scope predicate account_state product_capability_status material_role
    classification_status page_type inspection_status canonical_status error_code
    source schema_version evidence_mode contract_version menu_entry_id source_capture_id
    before_capture_id after_capture_id evidence_capture_id stage attempt reused
    original_scroll_count""".split()
)
_OBJECTS = frozenset(
    {"selection", "canonical", "notification", "primary_content_capture", "capability_revision"}
)
_LISTS = frozenset({"sources", "frames", "attempts", "evidence_artifact_ids", "reason_codes"})


def _public(value, depth=0):
    if depth > 6:
        raise ValueError("WEB_REPORT_EXPLANATION_LIMIT_EXCEEDED")
    if isinstance(value, dict):
        return {
            key: (item if isinstance(item, str) and item in _READINESS_VALUES else None)
            if key in _READINESS_FIELDS else _public(item, depth + 1)
            for key, item in value.items()
            if key in _FIELDS | _OBJECTS | _LISTS | _READINESS_FIELDS
        }
    if isinstance(value, list):
        if len(value) > 100:
            raise ValueError("WEB_REPORT_EXPLANATION_LIMIT_EXCEEDED")
        return [_public(item, depth + 1) for item in value]
    if isinstance(value, str):
        return (
            value
            if re.fullmatch(r"[A-Za-z0-9_.:+-]{1,200}", value)
            and not value.startswith(("http:", "https:"))
            else None
        )
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:100]


def build_web_report_explanation(*, task_id, agent_run_id, epoch, rows):
    """只复制引用、枚举和统计，不公开 URL、模型正文、选择器或存储路径。"""
    result = {
        "schema_version": "web-report-explanation.v1",
        "task_id": task_id,
        "agent_run_id": agent_run_id,
        "finalization_epoch": epoch,
    }
    for section in (
        "notifications",
        "sequences",
        "rule_materials",
        "publication_rules",
        "primary_content",
    ):
        items = rows.get(section) or []
        if isinstance(items, str):
            items = json.loads(items)
        if len(items) > 1000:
            raise ValueError("WEB_REPORT_EXPLANATION_LIMIT_EXCEEDED")
        result[section] = [_public(dict(item)) for item in items]
    for item in result["rule_materials"]:
        item["material_role"] = (
            "sequence"
            if item.get("input_scope") == "web_sequence"
            and item.get("evidence_input_set_id")
            and item.get("sequence_id")
            else "stable"
        )
    roles = {
        (item.get("work_item_id"), item.get("rule_id")): item["material_role"]
        for item in result["rule_materials"]
    }
    for item in result["rule_materials"]:
        for source in (item.get("canonical") or {}).get("sources") or []:
            source["material_role"] = roles.get(
                (source.get("work_item_id"), source.get("rule_id")), "unknown"
            )
    logical_pages = rows.get("web_logical_pages") or []
    if isinstance(logical_pages, list):
        result["page_classifications"] = [
            {
                "capture_id": page.get("primary_capture_id"),
                "page_type": page.get("page_type"),
                "guide_version": _public(page.get("guide_version")),
                "structure_fingerprint": _public(page.get("structure_fingerprint")),
                "subject_region_facts": _public_subject_facts(page["subject_region_facts"]),
                "decision_source": (
                    page.get("decision_source")
                    if page.get("decision_source") in ("model", "deterministic") else None
                ),
                "model_result": _public_decision_result(page.get("model_result")),
                "deterministic_result": _public_decision_result(page.get("deterministic_result")),
            }
            for page in logical_pages
            if isinstance(page, dict) and isinstance(page.get("subject_region_facts"), dict)
        ]
        business_page_types: list[str] = []
        lifecycle_page_types: list[str] = []
        unknown_pages: list[str] = []
        for page in logical_pages:
            if not isinstance(page, dict):
                continue
            page_type = str(page.get("page_type") or "")
            coverage_group = page.get("coverage_group")
            if coverage_group == "business" and page_type:
                if page_type not in business_page_types:
                    business_page_types.append(page_type)
            elif coverage_group == "lifecycle" and page_type:
                if page_type not in lifecycle_page_types:
                    lifecycle_page_types.append(page_type)
            elif coverage_group == "neither" or page_type == "unknown":
                capture_id = str(page.get("primary_capture_id") or "")
                if capture_id:
                    unknown_pages.append(capture_id)
        result["page_type_summary"] = {
            "business_page_types": business_page_types,
            "lifecycle_page_types": lifecycle_page_types,
            "unknown_pages": unknown_pages,
            "ad_page_count": len(rows.get("web_ad_pages") or []),
            "guide_page_count": sum(
                1 for page in rows.get("web_guide_pages") or []
                if isinstance(page, dict) and page.get("status") == "confirmed"
                and page.get("page_type") == "guide_page"
            ),
        }
    return result


def _public_decision_result(value):
    """双方结果仅公开有限页型、分数和状态，规则细节留在冻结审计中。"""
    if not isinstance(value, dict):
        return None
    result = {}
    page_type, confidence = value.get("page_type"), value.get("confidence")
    if isinstance(page_type, str) and page_type in WEB_PAGE_TYPE_CODES:
        result["page_type"] = page_type
    if type(confidence) in (int, float) and 0 <= confidence <= 1 and math.isfinite(confidence):
        result["confidence"] = confidence
    if isinstance(value.get("status"), str) and value["status"] in {
        "available", "missing", "unavailable",
    }:
        result["status"] = value["status"]
    if type(value.get("needs_review")) is bool:
        result["needs_review"] = value["needs_review"]
    return result


def _public_subject_facts(value):
    """公开受控实际计数和已脱敏摘要，旧报告不重新计算。"""
    result = {}
    for key in WebSubjectRegionFacts.__dataclass_fields__:
        item = value.get(key)
        if key.endswith("_count") or key.endswith("_length"):
            if type(item) is int and item >= 0:
                result[key] = item
        elif key == "primary_subject_kind":
            if isinstance(item, str) and item in {
                "unknown",
                "article",
                "video",
                "gallery",
                "conflicting",
            }:
                result[key] = item
        elif key in {"selector_summaries", "token_summaries"}:
            groups = []
            for group in item[:16] if isinstance(item, (list, tuple)) else ():
                if not isinstance(group, (list, tuple)) or len(group) != 2:
                    continue
                role, tokens = group
                if (
                    not isinstance(role, str)
                    or role not in SUMMARY_ROLES
                    or not isinstance(tokens, (list, tuple))
                ):
                    continue
                safe = [
                    token
                    for token in tokens[:32]
                    if isinstance(token, str)
                    and (
                        (
                            re.fullmatch(r"[a-z-]{1,32}:sha256:[a-f0-9]{24}", token)
                            and token.split(":", 1)[0] in SEMANTIC_TOKENS | {"node"}
                        )
                        if key == "selector_summaries"
                        else token in SEMANTIC_TOKENS
                    )
                ]
                groups.append([role, safe])
            result[key] = groups
        elif key == "player_vendor_tokens" and isinstance(item, (tuple, list)):
            result[key] = [
                token for token in item[:32] if isinstance(token, str) and token in VENDOR_TOKENS
            ]
    return result


async def load_web_report_explanation(repository, *, task_id, agent_run_id, access_scope=None):
    reader = getattr(repository, "fetch_web_report_explanation", None)
    if not callable(reader):
        return None
    return await reader(task_id=task_id, agent_run_id=agent_run_id, access_scope=access_scope)
