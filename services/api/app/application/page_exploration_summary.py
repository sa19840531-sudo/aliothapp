"""动态页面探索汇总：统一 Worker、任务洞察与报告的统计口径。"""

import json
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from typing import Any

from services.api.app.application.h5_page_type_assessment_adapter import (
    assess_after_visual_navigation,
)
from services.api.app.domain.page.registry import (
    PageTypeRegistry,
    get_default_page_type_registry,
)

_TERMINAL_PAGE_STATUSES = {"completed", "degraded", "failed"}
_PENDING_ACTION_STATUSES = {"candidate", "selected"}
_INACTIVE_ACTION_STATUSES = {"blocked", "skipped", "failed"}
_TERMINAL_ACTION_STATUSES = {"explored", "blocked", "skipped", "failed"}
_ASSESSMENT_STATUSES = {
    "covered",
    "requires_auth",
    "blocked_by_safety",
    "not_found",
    "evidence_insufficient",
    "not_applicable",
}
_UNCERTAIN_ASSESSMENT_STATUSES = {"not_found", "evidence_insufficient"}
_SAFETY_BLOCKED_PAGE_STATES = {
    "automation_blocked",
    "bot_detection",
    "captcha_blocked",
    "safety_interstitial",
    "security_check",
}
_SAFETY_BLOCKED_RISK_FLAGS = {
    "automation_blocked",
    "bot_detection",
    "captcha_blocked",
    "deterministic_safety_interstitial",
    "safety_interstitial",
}
_PAGE_TYPE_REGISTRY = get_default_page_type_registry()
_LIFECYCLE_PAGE_TYPES = set(_PAGE_TYPE_REGISTRY.lifecycle_codes)
_BUSINESS_ACTIONS = {"click", "hover", "goto", "back"}
_CATALOG_PAGE_TYPES = _PAGE_TYPE_REGISTRY.all_codes
_BUSINESS_PAGE_TYPES = _PAGE_TYPE_REGISTRY.coverage_required_codes
_VISUAL_NAVIGATION_MODES = {"shadow", "enforced"}
_VISUAL_NAVIGATION_TARGET_KINDS = {
    "primary_navigation",
    "content_card",
    "video_card",
    "profile_settings",
    "profile_payment",
    "profile_activity",
}
_VISUAL_NAVIGATION_POLICY_REASONS = {
    "visual_target_not_allowlisted",
    "visual_target_expected_page_mismatch",
    "visual_profile_source_required",
    "visual_profile_source_confidence_low",
    "visual_profile_identity_required",
    "visual_target_confidence_low",
    "visual_target_already_selected",
    "visual_target_not_unique",
    "visual_target_risk_flagged",
    "visual_target_coordinate_invalid",
    "visual_target_full_page_rect",
    "visual_target_geometry_invalid",
    "visual_primary_navigation_not_bottom_band",
    "visual_bounds_wrong_screenshot",
    "visual_model_conflict",
    "navigation_knowledge_invalid",
    "navigation_knowledge_miss",
    "navigation_repository_degraded",
    "navigation_expected_page_mismatch",
    "navigation_safe_alias_miss",
}
_VISUAL_NAVIGATION_ERROR_REASONS = {
    "visual_model_conflict",
    "visual_detector_failed",
    "visual_source_obstructed",
    "stable_after_frame_pair_required",
    "visual_frame_invalid",
    "visual_destination_obstructed",
    "after_frames_inconsistent",
    "main_structure_not_changed",
    "visual_layout_identity_unbound",
    "destination_intent_mismatch",
    "source_page_type_mismatch",
    "target_navigation_selection_not_changed",
    "detail_destination_structure_insufficient",
    "destination_page_frame_unverified",
    "profile_source_required",
    "profile_destination_structure_missing",
    "visual_target_kind_unsupported",
    "visual_navigation_no_effect",
    "visual_source_artifact_mismatch",
    "visual_source_artifact_unbound",
    "visual_source_identity_mismatch",
    "visual_source_identity_unbound",
    "visual_source_state_mismatch",
    "visual_source_state_unbound",
    "visual_source_layout_stale",
    "VISUAL_TARGET_CONFIDENCE_LOW",
    "VISUAL_TARGET_NOT_ALLOWLISTED",
    "VISUAL_TARGET_NOT_UNIQUE",
    "VISUAL_TARGET_SOURCE_STATE_STALE",
    "VISUAL_TARGET_COORDINATE_INVALID",
    "VISUAL_TARGET_OBSTRUCTED",
    "VISUAL_NAVIGATION_NO_EFFECT",
    "VISUAL_NAVIGATION_UNSAFE_DESTINATION",
    "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
    "VISUAL_NAVIGATION_MODE_DISABLED",
    "VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED",
    "VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED",
    "VISUAL_TARGET_INVALID",
}
_VISUAL_MODEL_USAGE_FIELDS = (
    "request_count",
    "elapsed_ms",
    "input_tokens",
    "output_tokens",
    "cache_hit_tokens",
    "total_tokens",
)
_MAX_VISUAL_REPORT_COUNT = 2_147_483_647


def lifecycle_page_types(*, registry: PageTypeRegistry | None = None) -> set[str]:
    """返回 Catalog 声明的生命周期页面类型。"""
    resolved = registry or _PAGE_TYPE_REGISTRY
    return set(resolved.lifecycle_codes)


def apply_app_page_type_applicability(
    summary: dict[str, Any],
    *,
    app_coverage: Mapping[str, Any],
) -> None:
    """把 App Runtime 的证据驱动适用性投影为报告中心统一业务分母。"""

    applicability = app_coverage.get("page_type_applicability")
    coverage = summary.get("coverage")
    if not isinstance(applicability, Mapping) or not isinstance(coverage, dict):
        return
    required_fields = (
        "covered_page_types",
        "known_uncovered_page_types",
        "not_applicable_page_types",
        "unresolved_applicability_page_types",
        "applicable_page_types",
    )
    if any(not isinstance(applicability.get(field), (list, tuple)) for field in required_fields):
        return
    not_observed = applicability.get("not_observed_page_types", ())
    if not isinstance(not_observed, (list, tuple)):
        return
    scope_verified = applicability.get("assessment_scope_verified") is True
    unresolved = list(applicability["unresolved_applicability_page_types"])
    coverage["product_business"] = {
        "authority_source": applicability.get("authority_source"),
        "assessment_available": True,
        "assessment_scope_complete": (applicability.get("assessment_scope_complete") is True),
        "assessment_scope_verified": scope_verified,
        "assessment_complete": scope_verified and not unresolved,
        "missing_assessment_page_types": unresolved,
        "applicable_page_types": list(applicability["applicable_page_types"]),
        "covered_page_types": list(applicability["covered_page_types"]),
        "known_uncovered_page_types": list(applicability["known_uncovered_page_types"]),
        "unresolved_page_types": unresolved,
        "not_applicable_page_types": list(applicability["not_applicable_page_types"]),
        "not_observed_page_types": list(not_observed),
        "assessment_scope_kind": applicability.get("assessment_scope_kind"),
        "covered_count": applicability.get("covered_count"),
        "applicable_count": applicability.get("applicable_count"),
        "coverage_rate": applicability.get("coverage_rate"),
    }


def build_page_exploration_summary(
    *,
    pages: Iterable[Mapping[str, Any]],
    actions: Iterable[Mapping[str, Any]],
    tracking_available: bool | None = None,
    page_type_assessments: Iterable[Mapping[str, Any]] = (),
    assessment_scope_complete: bool = False,
    visual_navigation_mode: str | None = None,
    visual_model_usage: Mapping[str, Any] | None = None,
    required_page_types: Iterable[str] | None = None,
    inspection_scope: Collection[str] = (),
) -> dict[str, Any]:
    """按真实页面、Frontier 动作和显式适用性评估生成双轨覆盖汇总。

    ``inspection_scope`` 是任务选的质检范围：为找目标经过的页面只留在动作路线里，
    不进逐页结果、页数和页面覆盖。为空表示不限制。
    """
    scope = frozenset(inspection_scope)
    page_items = [
        item
        for item in _deduplicated(pages, "page_capture_id")
        if not scope or _page_type(item) in scope
    ]
    action_items = _deduplicated(actions, "exploration_action_id")
    # 断言不能在这里按页型去重：_deduplicated 是后写覆盖，会在归一化之前
    # 随机丢掉同一页型的其余断言。哪条留下取决于 page_capture_id 的 UUID
    # 字典序，等于让覆盖数字取决于运气。收敛交给 _normalized_assessments，
    # 它按状态优先级取最优。
    assessment_items = [
        item
        for item in page_type_assessments
        if not scope or _normalized_page_type(item.get("page_type")) in scope
    ]
    page_statuses = [_text(item.get("inspection_status")) or "unknown" for item in page_items]
    lifecycle_statuses = [
        _text(item.get("inspection_lifecycle_status") or item.get("lifecycle_status")) or "unknown"
        for item in page_items
    ]
    action_statuses = [_text(item.get("status")) or "unknown" for item in action_items]
    page_status_counts = Counter(page_statuses)
    lifecycle_status_counts = Counter(lifecycle_statuses)
    action_status_counts = Counter(action_statuses)

    if tracking_available is None:
        tracking_available = bool(
            action_items
            or any(status != "unknown" for status in page_statuses)
            or any(status != "unknown" for status in lifecycle_statuses)
        )

    active_page_statuses = [
        status
        for status, lifecycle in zip(page_statuses, lifecycle_statuses, strict=True)
        if lifecycle in {"active", "unknown"}
    ]
    pending_page_count = sum(
        status not in _TERMINAL_PAGE_STATUSES for status in active_page_statuses
    )
    pending_action_count = sum(status in _PENDING_ACTION_STATUSES for status in action_statuses)
    pending_work_count = pending_page_count + pending_action_count
    completed_page_count = active_page_statuses.count("completed")
    degraded_page_count = active_page_statuses.count("degraded")
    failed_page_count = active_page_statuses.count("failed")
    terminal_page_count = sum(status in _TERMINAL_PAGE_STATUSES for status in active_page_statuses)
    failed_action_count = action_statuses.count("failed")
    zero_effective_coverage = (
        bool(page_items) and pending_work_count == 0 and terminal_page_count == 0
    )
    requires_review = any(
        (
            degraded_page_count,
            failed_page_count,
            failed_action_count,
            zero_effective_coverage,
        )
    )

    if not tracking_available:
        complete: bool | None = None
        quality_status = "unknown"
    else:
        complete = pending_work_count == 0
        if not complete:
            quality_status = "in_progress"
        elif requires_review:
            quality_status = "needs_review"
        else:
            quality_status = "complete"

    summary = {
        "tracking_available": tracking_available,
        "complete": complete,
        "quality_status": quality_status,
        "requires_review": requires_review,
        "page_total": len(page_items),
        "active_page_count": sum(
            lifecycle in {"active", "unknown"} for lifecycle in lifecycle_statuses
        ),
        "terminal_page_count": terminal_page_count,
        "completed_page_count": completed_page_count,
        "degraded_page_count": degraded_page_count,
        "failed_page_count": failed_page_count,
        "pending_page_count": pending_page_count,
        "superseded_page_count": lifecycle_statuses.count("superseded"),
        "abandoned_page_count": lifecycle_statuses.count("abandoned"),
        "evidence_only_page_count": lifecycle_statuses.count("evidence_only"),
        "page_status_counts": dict(page_status_counts),
        "page_lifecycle_counts": dict(lifecycle_status_counts),
        "action_total": len(action_items),
        "pending_action_count": pending_action_count,
        "explored_action_count": action_statuses.count("explored"),
        "inactive_action_count": sum(
            status in _INACTIVE_ACTION_STATUSES for status in action_statuses
        ),
        "failed_action_count": failed_action_count,
        "blocked_action_count": action_statuses.count("blocked"),
        "skipped_action_count": action_statuses.count("skipped"),
        "action_status_counts": dict(action_status_counts),
        "pending_work_count": pending_work_count,
        "coverage": _coverage_summary(
            page_items=page_items,
            action_items=action_items,
            assessment_items=assessment_items,
            assessment_scope_complete=assessment_scope_complete,
            required_page_types=required_page_types,
            inspection_scope=scope,
        ),
        "page_results": [_page_result(item) for item in page_items],
        "action_results": [_action_result(item) for item in action_items],
    }
    logical_surface = _logical_surface_summary(
        page_items=page_items,
        action_items=action_items,
    )
    if logical_surface is not None:
        summary["logical_surface"] = logical_surface
    visual_navigation = _visual_navigation_summary(
        page_items=page_items,
        action_items=action_items,
        explicit_mode=visual_navigation_mode,
        external_model_usage=visual_model_usage,
    )
    if visual_navigation is not None:
        summary["visual_navigation"] = visual_navigation
    return summary


def project_page_evidence_results(
    pages: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """投影仅用于动作证据核验的 Capture，不参与页面覆盖统计。"""

    return [_page_result(item) for item in _deduplicated(pages, "page_capture_id")]


def project_h5_page_type_assessments(
    assessments: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """把 H5 V2 正式 Assessment 行投影为统一覆盖汇总合同。"""

    projections: list[dict[str, Any]] = []
    for row in assessments:
        raw_payload = row.get("assessment_payload")
        payload = dict(raw_payload) if isinstance(raw_payload, Mapping) else {}
        reason_codes = row.get("reason_codes")
        reasons = list(reason_codes) if isinstance(reason_codes, list | tuple) else []
        capture_id = _text(row.get("page_capture_id"))
        projection = {
            **payload,
            "assessment_id": row.get("assessment_id"),
            "page_capture_id": capture_id,
            "page_capture_ids": payload.get("page_capture_ids")
            or ([capture_id] if capture_id else []),
            "page_type": row.get("page_type"),
            "page_state": row.get("page_state"),
            "status": row.get("status"),
            "reason_codes": reasons,
            "reason_code": payload.get("reason_code")
            or (reasons[0] if reasons else "unspecified"),
        }
        detection_result = payload.get("detection_result")
        if isinstance(detection_result, Mapping):
            projection["detection_result"] = dict(detection_result)
        projections.append(projection)
    return projections


def count_pending_frontier_actions(actions: Iterable[Mapping[str, Any]]) -> int:
    """待探索 Frontier 动作数（candidate/selected），与覆盖汇总同口径。"""
    action_items = _deduplicated(actions, "exploration_action_id")
    return sum(
        (_text(item.get("status")) or "unknown") in _PENDING_ACTION_STATUSES
        for item in action_items
    )


def extract_page_type_assessment_context(
    working_memory: Mapping[str, Any] | None,
) -> tuple[list[Mapping[str, Any]], bool]:
    """从 Run 工作内存提取统一的产品业务页面类型评估。"""
    if not isinstance(working_memory, Mapping):
        return [], False
    raw_assessments = working_memory.get("page_type_assessments")
    assessments = (
        [item for item in raw_assessments if isinstance(item, Mapping)]
        if isinstance(raw_assessments, list)
        else []
    )
    return assessments, working_memory.get("page_type_assessment_scope_complete") is True


def extract_page_type_assessment_scope_page_types(
    working_memory: Mapping[str, Any] | None,
) -> list[str]:
    """从 WorkingMemory 提取代码已固化的产品动态覆盖分母。"""

    if not isinstance(working_memory, Mapping):
        return []
    raw_page_types = working_memory.get("page_type_assessment_scope_page_types")
    if not isinstance(raw_page_types, list | tuple):
        return []
    return [
        page_type
        for item in raw_page_types
        if (page_type := _normalized_page_type(item)) in _BUSINESS_PAGE_TYPES
    ]


def summarize_working_memory(
    working_memory: Mapping[str, Any],
    *,
    page_type_assessments: Iterable[Mapping[str, Any]] | None = None,
    assessment_scope_complete: bool | None = None,
    required_page_types: Iterable[str] | None = None,
) -> dict[str, Any]:
    """从 Run 最终 WorkingMemory 直接汇总，避免报告生成等待数据库检查点。"""
    pages: list[Mapping[str, Any]] = []
    current_page = working_memory.get("current_page")
    if isinstance(current_page, Mapping):
        pages.append(current_page)
    for key in ("inspected_pages", "terminal_pages", "pending_pages", "inactive_pages"):
        raw_pages = working_memory.get(key)
        if isinstance(raw_pages, list):
            pages.extend(item for item in raw_pages if isinstance(item, Mapping))

    actions: list[Mapping[str, Any]] = []
    frontier = working_memory.get("exploration_frontier")
    if isinstance(frontier, Mapping):
        for key in ("pending_actions", "explored_actions", "inactive_actions"):
            raw_actions = frontier.get(key)
            if isinstance(raw_actions, list):
                actions.extend(item for item in raw_actions if isinstance(item, Mapping))

    stored_assessments, stored_scope_complete = extract_page_type_assessment_context(working_memory)
    stored_scope_page_types = extract_page_type_assessment_scope_page_types(working_memory)
    if page_type_assessments is None:
        page_type_assessments = stored_assessments
    if assessment_scope_complete is None:
        assessment_scope_complete = stored_scope_complete

    schema_version = working_memory.get("schema_version")
    tracking_available = schema_version == 2 or bool(pages or actions)
    return build_page_exploration_summary(
        pages=pages,
        actions=actions,
        tracking_available=tracking_available,
        page_type_assessments=page_type_assessments,
        assessment_scope_complete=assessment_scope_complete,
        visual_navigation_mode=_visual_mode_from_working_memory(working_memory),
        required_page_types=(
            required_page_types
            if required_page_types is not None
            else stored_scope_page_types
        ),
    )


def _visual_navigation_summary(
    *,
    page_items: list[Mapping[str, Any]],
    action_items: list[Mapping[str, Any]],
    explicit_mode: str | None,
    external_model_usage: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """只聚合固定枚举和有界数值；任何视觉原始材料都不会复制到结果。"""
    audits = [
        audit
        for item in page_items
        if (audit := _visual_navigation_audit_from_page(item)) is not None
    ]
    visual_actions = [item for item in action_items if _is_visual_navigation_action(item)]
    mode = _visual_navigation_mode(
        explicit_mode=explicit_mode,
        audits=audits,
        has_visual_action=bool(visual_actions),
    )
    has_external_usage = bool(
        isinstance(external_model_usage, Mapping)
        and any(
            _bounded_nonnegative_int(external_model_usage.get(field)) > 0
            for field in _VISUAL_MODEL_USAGE_FIELDS
        )
    )
    if not audits and not visual_actions and mode is None and not has_external_usage:
        return None

    target_kind_counts: Counter[str] = Counter()
    policy_rejection_counts: Counter[str] = Counter()
    error_reason_counts: Counter[str] = Counter()
    navigation_hits: Counter[str] = Counter()
    model_usage = {field: 0 for field in _VISUAL_MODEL_USAGE_FIELDS}
    insufficient_capture_count = 0
    prediction_count = 0
    allowed_prediction_count = 0
    signed_capability_count = 0
    verified_capability_count = 0
    enforced_candidate_count = 0
    repository_degraded_count = 0

    for audit in audits:
        if _text(audit.get("detector_status")) in {"insufficient", "blocked", "failed"}:
            insufficient_capture_count = _bounded_count_sum(
                insufficient_capture_count, 1
            )
        prediction_count = _bounded_count_sum(
            prediction_count,
            _bounded_nonnegative_int(audit.get("prediction_count")),
        )
        allowed_prediction_count = _bounded_count_sum(
            allowed_prediction_count,
            _bounded_nonnegative_int(audit.get("allowed_prediction_count")),
        )
        signed_capability_count = _bounded_count_sum(
            signed_capability_count,
            _bounded_nonnegative_int(audit.get("signed_capability_count")),
        )
        verified_capability_count = _bounded_count_sum(
            verified_capability_count,
            _bounded_nonnegative_int(audit.get("verified_capability_count")),
        )
        enforced_candidate_count = _bounded_count_sum(
            enforced_candidate_count,
            _bounded_nonnegative_int(audit.get("enforced_candidate_count")),
        )
        _update_allowlisted_counter(
            target_kind_counts,
            audit.get("target_kind_counts"),
            allowed_keys=_VISUAL_NAVIGATION_TARGET_KINDS,
        )
        _update_allowlisted_counter(
            policy_rejection_counts,
            audit.get("policy_rejection_counts"),
            allowed_keys=_VISUAL_NAVIGATION_POLICY_REASONS,
        )
        conflict_codes = audit.get("conflict_codes")
        if isinstance(conflict_codes, (list, tuple)):
            for reason in conflict_codes:
                if reason in _VISUAL_NAVIGATION_ERROR_REASONS:
                    error_reason_counts[str(reason)] = _bounded_count_sum(
                        error_reason_counts[str(reason)], 1
                    )

        knowledge = audit.get("navigation_knowledge")
        knowledge = knowledge if isinstance(knowledge, Mapping) else {}
        _update_allowlisted_counter(
            navigation_hits,
            knowledge.get("match_type_counts"),
            allowed_keys={"static", "exact", "vector"},
        )
        repository_degraded_count = _bounded_count_sum(
            repository_degraded_count,
            _bounded_nonnegative_int(knowledge.get("repository_degraded_count")),
        )
        _merge_visual_model_usage(model_usage, audit.get("model_usage"))

    trusted_transition_count = 0
    executed_action_count = 0
    trusted_actions: list[Mapping[str, Any]] = []
    for action in visual_actions:
        if _visual_action_click_was_committed(action):
            executed_action_count = _bounded_count_sum(executed_action_count, 1)
        failure_code = _text(action.get("failure_code"))
        if failure_code in _VISUAL_NAVIGATION_ERROR_REASONS:
            error_reason_counts[failure_code] = _bounded_count_sum(
                error_reason_counts[failure_code], 1
            )
        transition = _visual_navigation_transition_from_action(action)
        if transition is None:
            continue
        reasons = transition.get("reason_codes")
        if isinstance(reasons, (list, tuple)):
            for reason in reasons:
                if reason in _VISUAL_NAVIGATION_ERROR_REASONS:
                    error_reason_counts[str(reason)] = _bounded_count_sum(
                        error_reason_counts[str(reason)], 1
                    )
        if _visual_action_completed(action) and _visual_transition_is_trusted(
            transition
        ):
            trusted_transition_count = _bounded_count_sum(trusted_transition_count, 1)
            trusted_actions.append(action)

    if isinstance(external_model_usage, Mapping):
        for field in _VISUAL_MODEL_USAGE_FIELDS:
            value = _bounded_nonnegative_int(external_model_usage.get(field))
            if external_model_usage.get(field) is None:
                continue
            if field == "request_count":
                model_usage[field] = max(model_usage[field], value)
            elif field != "elapsed_ms":
                model_usage[field] = value

    page_types_added = (
        _visual_navigation_page_types_added(
            page_items=page_items,
            trusted_actions=trusted_actions,
        )
        if mode == "enforced"
        else []
    )
    return {
        "mode": mode or "shadow",
        "eligible_capture_count": _bounded_nonnegative_int(len(audits)),
        "insufficient_capture_count": insufficient_capture_count,
        "prediction_count": prediction_count,
        "allowed_prediction_count": allowed_prediction_count,
        "signed_capability_count": signed_capability_count,
        "verified_capability_count": verified_capability_count,
        "enforced_candidate_count": enforced_candidate_count,
        "executed_action_count": executed_action_count,
        "trusted_transition_count": trusted_transition_count,
        "target_kind_counts": dict(sorted(target_kind_counts.items())),
        "policy_rejection_counts": dict(sorted(policy_rejection_counts.items())),
        "error_reason_counts": dict(sorted(error_reason_counts.items())),
        "navigation_knowledge": {
            "static_hit_count": navigation_hits["static"],
            "exact_hit_count": navigation_hits["exact"],
            "vector_hit_count": navigation_hits["vector"],
            "repository_degraded_count": repository_degraded_count,
        },
        "model_usage": model_usage,
        "page_types_added": page_types_added,
    }


def _visual_navigation_audit_from_page(
    item: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    detection = item.get("page_detection")
    detection = detection if isinstance(detection, Mapping) else {}
    audit = item.get("visual_navigation_audit") or detection.get(
        "visual_navigation_audit"
    )
    if not isinstance(audit, Mapping):
        return None
    if audit.get("protocol_version") != "visual_navigation_audit.v1":
        return None
    return audit


def _visual_navigation_mode(
    *,
    explicit_mode: str | None,
    audits: list[Mapping[str, Any]],
    has_visual_action: bool,
) -> str | None:
    normalized_explicit = _text(explicit_mode)
    if normalized_explicit in _VISUAL_NAVIGATION_MODES:
        return normalized_explicit
    audit_modes = {
        mode
        for audit in audits
        if (mode := _text(audit.get("mode"))) in _VISUAL_NAVIGATION_MODES
    }
    if "enforced" in audit_modes or has_visual_action:
        return "enforced"
    if "shadow" in audit_modes or audits:
        return "shadow"
    return None


def _visual_mode_from_working_memory(working_memory: Mapping[str, Any]) -> str | None:
    frontier = working_memory.get("exploration_frontier")
    frontier = frontier if isinstance(frontier, Mapping) else {}
    metadata = frontier.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    for value in (
        working_memory.get("h5_visual_coordinate_mode"),
        metadata.get("h5_visual_coordinate_mode"),
    ):
        mode = _text(value)
        if mode in _VISUAL_NAVIGATION_MODES:
            return mode
    return None


def _is_visual_navigation_action(item: Mapping[str, Any]) -> bool:
    parameters = item.get("parameters")
    parameters = parameters if isinstance(parameters, Mapping) else {}
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    return bool(
        item.get("is_visual_navigation_provenance") is True
        or _text(parameters.get("target_mode") or item.get("target_mode"))
        == "visual_navigation_coordinate"
        or "visual_navigation_capability" in parameters
        or parameters.get("visual_navigation_provenance") is not None
        or parameters.get("visual_target_kind") is not None
        or item.get("visual_navigation_provenance") is not None
        or item.get("visual_navigation_target") is True
        or item.get("visual_target_kind") is not None
        or metadata.get("visual_navigation_provenance") is not None
        or metadata.get("visual_navigation_target") is True
        or metadata.get("visual_target_kind") is not None
    )


def _visual_action_completed(item: Mapping[str, Any]) -> bool:
    return (
        (_text(item.get("status")) or "").casefold() == "explored"
        and (_text(item.get("execution_status")) or "completed").casefold()
        == "completed"
    )


def _visual_action_click_was_committed(item: Mapping[str, Any]) -> bool:
    """成功终态或持久化安全布尔可证明视觉点击已经提交。"""
    if _visual_action_completed(item):
        return True
    result_payload = item.get("result_payload")
    result_payload = result_payload if isinstance(result_payload, Mapping) else {}
    state_diff = result_payload.get("state_diff")
    state_diff = state_diff if isinstance(state_diff, Mapping) else {}
    return state_diff.get("visual_action_committed") is True


def _visual_navigation_transition_from_action(
    item: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    direct = item.get("visual_navigation_transition")
    if isinstance(direct, Mapping):
        return direct
    logical = _logical_transition_from_action(item)
    if not isinstance(logical, Mapping):
        return None
    visual = logical.get("visual_proof")
    if not isinstance(visual, Mapping):
        return None
    return {
        **visual,
        "logical_transition_proof": logical.get("logical_transition_proof"),
    }


def _visual_transition_is_trusted(transition: Mapping[str, Any]) -> bool:
    return bool(
        transition.get("protocol_version")
        in {
            "visual_navigation_transition.v1",
            "visual_navigation_transition_proof.v1",
        }
        and transition.get("status") == "trusted"
        and transition.get("logical_transition_proof") == "trusted"
        and transition.get("after_identity_bound") is True
    )


def _visual_navigation_page_types_added(
    *,
    page_items: list[Mapping[str, Any]],
    trusted_actions: list[Mapping[str, Any]],
) -> list[str]:
    pages_by_capture = {
        capture_id: item
        for item in page_items
        if (capture_id := _text(item.get("page_capture_id"))) is not None
    }
    added: set[str] = set()
    for action in trusted_actions:
        if not _visual_action_completed(action):
            continue
        result_capture_id = _text(action.get("result_page_capture_id"))
        page = pages_by_capture.get(result_capture_id or "")
        transition = _visual_navigation_transition_from_action(action)
        if (
            page is None
            or transition is None
            or not _visual_after_page_passes_coverage_gate(
                page,
                transition=transition,
            )
        ):
            continue
        actual_page_type = _page_type(page)
        source_page_type = _normalized_page_type(action.get("source_page_type"))
        if (
            actual_page_type in _BUSINESS_PAGE_TYPES
            and actual_page_type != source_page_type
        ):
            added.add(actual_page_type)
    return sorted(added)


def _visual_after_page_passes_coverage_gate(
    item: Mapping[str, Any],
    *,
    transition: Mapping[str, Any],
) -> bool:
    detection = item.get("page_detection")
    detection = detection if isinstance(detection, Mapping) else {}
    detection_status = _detection_status(item) or ""
    confidence = _bounded_report_confidence(
        item.get("detection_confidence")
        if item.get("detection_confidence") is not None
        else detection.get("confidence")
    )
    page_type = _page_type(item)
    surface = _logical_surface_from_page(item)
    if confidence is None or page_type is None or surface is None:
        return False
    identity = _logical_fingerprint(surface.get("identity_fingerprint"))
    binding = surface.get("page_type_binding")
    if identity is None or not isinstance(binding, Mapping):
        return False
    identity_bound = bool(
        _logical_fingerprint(binding.get("identity_fingerprint")) == identity
        and _normalized_page_type(binding.get("page_type")) == page_type
    )
    assessment = assess_after_visual_navigation(
        logical_transition_proof=_text(transition.get("logical_transition_proof"))
        or "",
        detection_status=detection_status,
        page_type_confidence=confidence,
        identity_bound=identity_bound,
    )
    return bool(
        assessment.coverage_allowed and binding.get("coverage_allowed") is True
    )


def _update_allowlisted_counter(
    counter: Counter[str],
    values: Any,
    *,
    allowed_keys: set[str],
) -> None:
    if not isinstance(values, Mapping):
        return
    for key in allowed_keys:
        if key not in values:
            continue
        value = _bounded_nonnegative_int(values.get(key))
        if value <= 0:
            continue
        counter[key] = _bounded_count_sum(
            counter[key], value
        )


def _merge_visual_model_usage(target: dict[str, int], values: Any) -> None:
    if not isinstance(values, Mapping):
        return
    for field in _VISUAL_MODEL_USAGE_FIELDS:
        target[field] = _bounded_count_sum(
            target[field], _bounded_nonnegative_int(values.get(field))
        )


def _bounded_nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return min(max(value, 0), _MAX_VISUAL_REPORT_COUNT)
    if isinstance(value, float) and value.is_integer():
        return min(max(int(value), 0), _MAX_VISUAL_REPORT_COUNT)
    return 0


def _bounded_count_sum(left: int, right: int) -> int:
    return min(max(left, 0) + max(right, 0), _MAX_VISUAL_REPORT_COUNT)


def _logical_surface_summary(
    *,
    page_items: list[Mapping[str, Any]],
    action_items: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """聚合非敏感逻辑页面审计；没有逻辑证据时保持旧响应不变。"""
    surfaces: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for item in page_items:
        surface = _logical_surface_from_page(item)
        if surface is not None:
            surfaces.append((item, surface))
    transitions = [
        transition
        for item in action_items
        if (transition := _logical_transition_from_action(item)) is not None
    ]
    if not surfaces and not transitions:
        return None

    render_profiles: Counter[str] = Counter()
    degraded_reasons: Counter[str] = Counter()
    blocked_reasons: Counter[str] = Counter()
    identities: dict[str, dict[str, Any]] = {}
    state_fingerprints: set[str] = set()
    flutter_surface_count = 0
    semantics_activated_count = 0
    semantics_unavailable_count = 0
    visual_supplement_count = 0
    ocr_supplement_count = 0
    shadow_capture_count = 0

    for page, surface in surfaces:
        profile = surface.get("render_profile")
        profile = profile if isinstance(profile, Mapping) else {}
        profile_kind = _text(profile.get("kind") or surface.get("render_profile"))
        if profile_kind is not None:
            render_profiles[profile_kind] += 1
        reasons = _string_list(surface.get("degraded_reasons"))
        degraded_reasons.update(reasons)
        if "interactive_exploration_blocked" in reasons:
            blocked_reasons["interactive_exploration_blocked"] += 1
        sources = set(_string_list(surface.get("evidence_sources")))
        if profile_kind in {"flutter_web", "hybrid"}:
            flutter_surface_count += 1
            if (
                "flutter_semantics" in sources
                and "flutter_semantics_unavailable" not in reasons
            ):
                semantics_activated_count += 1
            if "flutter_semantics_unavailable" in reasons:
                semantics_unavailable_count += 1
        visual_supplement_count += "visual" in sources
        ocr_supplement_count += "ocr" in sources
        shadow_capture_count += surface.get("shadow") is True

        identity = _logical_fingerprint(surface.get("identity_fingerprint"))
        state = _logical_fingerprint(surface.get("state_fingerprint"))
        if state is not None:
            state_fingerprints.add(state)
        if identity is None:
            continue
        entry = identities.setdefault(
            identity,
            {
                "page_types": set(),
                "page_type_confidences": set(),
                "confidence_sources": set(),
                "state_fingerprints": set(),
                "capture_count": 0,
                "conflict": False,
            },
        )
        entry["capture_count"] += 1
        if state is not None:
            entry["state_fingerprints"].add(state)
        binding = surface.get("page_type_binding")
        binding = binding if isinstance(binding, Mapping) else {}
        page_type = _normalized_page_type(binding.get("page_type")) or _page_type(page)
        if page_type is not None:
            entry["page_types"].add(page_type)
        confidence = _bounded_report_confidence(
            binding.get("page_type_confidence")
            if binding.get("page_type_confidence") is not None
            else page.get("detection_confidence")
        )
        if confidence is not None:
            entry["page_type_confidences"].add(confidence)
        confidence_source = _text(
            binding.get("confidence_source") or page.get("confidence_source")
        )
        if confidence_source is not None:
            entry["confidence_sources"].add(confidence_source)
        entry["conflict"] = bool(
            entry["conflict"]
            or _string_list(page.get("conflicts"))
            or binding.get("reason_code") == "logical_identity_page_type_conflict"
        )

    for item in page_items:
        failure_code = _text(
            item.get("inspection_failure_code") or item.get("failure_code")
        )
        if failure_code is not None:
            blocked_reasons[failure_code] += 1
    for item in action_items:
        status = (_text(item.get("status")) or "").casefold()
        if status not in {"blocked", "failed"}:
            continue
        reason = _text(item.get("failure_code") or item.get("failure_reason"))
        if reason is not None:
            blocked_reasons[reason] += 1

    transition_counts = Counter(
        transition_kind
        for transition in transitions
        if (transition_kind := _text(transition.get("transition_kind"))) is not None
    )
    same_url_logical_page_change_count = sum(
        _logical_transition_same_url(item)
        for item in action_items
        if _logical_transition_from_action(item) is not None
    )
    page_types_by_capture = {
        capture_id: page_type
        for item in page_items
        if (capture_id := _text(item.get("page_capture_id"))) is not None
        and (page_type := _page_type(item)) is not None
    }
    mismatches = _logical_page_type_mismatches(
        action_items=action_items,
        page_types_by_capture=page_types_by_capture,
    )
    logical_pages = []
    for identity, entry in sorted(identities.items()):
        page_types = sorted(entry["page_types"])
        confidences = sorted(entry["page_type_confidences"])
        logical_pages.append(
            {
                "identity_fingerprint": identity,
                "page_type": page_types[0] if len(page_types) == 1 else None,
                "page_types": page_types,
                "page_type_confidence": max(confidences) if confidences else None,
                "confidence_sources": sorted(entry["confidence_sources"]),
                "conflict": bool(entry["conflict"] or len(page_types) > 1),
                "observed_state_count": len(entry["state_fingerprints"]),
                "capture_count": entry["capture_count"],
            }
        )
    return {
        "unique_logical_page_count": len(identities),
        "observed_surface_state_count": len(state_fingerprints),
        "render_profile_counts": dict(sorted(render_profiles.items())),
        "transition_counts": dict(sorted(transition_counts.items())),
        "same_url_logical_page_change_count": same_url_logical_page_change_count,
        "semantics_activation": {
            "flutter_or_hybrid_capture_count": flutter_surface_count,
            "activated_capture_count": semantics_activated_count,
            "unavailable_capture_count": semantics_unavailable_count,
            "activation_rate": (
                round(semantics_activated_count / flutter_surface_count, 4)
                if flutter_surface_count
                else None
            ),
        },
        "visual_supplement_capture_count": visual_supplement_count,
        "ocr_supplement_capture_count": ocr_supplement_count,
        "visual_ocr_degradation_count": sum(
            count
            for reason, count in degraded_reasons.items()
            if "visual" in reason.casefold() or "ocr" in reason.casefold()
        ),
        "degraded_reason_counts": dict(sorted(degraded_reasons.items())),
        "blocked_reason_counts": dict(sorted(blocked_reasons.items())),
        "shadow_capture_count": shadow_capture_count,
        "logical_pages": logical_pages,
        "page_type_mismatches": mismatches,
    }


def _logical_surface_from_page(
    item: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    detection = item.get("page_detection")
    detection = detection if isinstance(detection, Mapping) else {}
    surface = item.get("logical_surface") or detection.get("logical_surface")
    if isinstance(surface, str):
        try:
            surface = json.loads(surface)
        except json.JSONDecodeError:
            return None
    return (
        surface
        if isinstance(surface, Mapping)
        and surface.get("protocol_version") == "logical_surface.v1"
        else None
    )


def _logical_transition_from_action(
    item: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    direct = item.get("logical_surface_transition")
    result_payload = item.get("result_payload")
    result_payload = result_payload if isinstance(result_payload, Mapping) else {}
    state_diff = item.get("state_diff")
    state_diff = (
        state_diff
        if isinstance(state_diff, Mapping)
        else result_payload.get("state_diff")
    )
    nested = (
        state_diff.get("logical_surface_transition")
        if isinstance(state_diff, Mapping)
        else None
    )
    transition = direct if isinstance(direct, Mapping) else nested
    return (
        transition
        if isinstance(transition, Mapping)
        and transition.get("protocol_version") == "logical_surface_transition.v1"
        else None
    )


def _logical_transition_same_url(item: Mapping[str, Any]) -> bool:
    transition = _logical_transition_from_action(item)
    if (
        transition is None
        or transition.get("transition_kind") != "logical_page_changed"
    ):
        return False
    result_payload = item.get("result_payload")
    result_payload = result_payload if isinstance(result_payload, Mapping) else {}
    state_diff = item.get("state_diff")
    state_diff = state_diff if isinstance(state_diff, Mapping) else result_payload.get("state_diff")
    if not isinstance(state_diff, Mapping):
        return False
    before = _text(state_diff.get("before_location"))
    after = _text(state_diff.get("after_location"))
    return bool(
        state_diff.get("location_changed") is False
        or (before is not None and before == after)
    )


def _logical_page_type_mismatches(
    *,
    action_items: list[Mapping[str, Any]],
    page_types_by_capture: Mapping[str, str],
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for item in action_items:
        expected = _normalized_page_type(item.get("expected_next_page_type"))
        result_capture_id = _text(item.get("result_page_capture_id"))
        actual = page_types_by_capture.get(result_capture_id or "")
        if expected is None or actual is None or expected == actual:
            continue
        mismatches.append(
            {
                "exploration_action_id": _text(
                    item.get("exploration_action_id")
                    or item.get("action_execution_id")
                ),
                "result_page_capture_id": result_capture_id,
                "expected_page_type": expected,
                "actual_page_type": actual,
            }
        )
    return mismatches


def _logical_fingerprint(value: Any) -> str | None:
    normalized = _text(value)
    if normalized is None or not re.fullmatch(r"sha256:[0-9a-f]{64}", normalized):
        return None
    return normalized


def _bounded_report_confidence(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(min(max(float(value), 0.0), 1.0), 4)


def _coverage_summary(
    *,
    page_items: list[Mapping[str, Any]],
    action_items: list[Mapping[str, Any]],
    assessment_items: list[Mapping[str, Any]],
    assessment_scope_complete: bool,
    required_page_types: Iterable[str] | None,
    inspection_scope: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    evidence_by_page_type = _evidence_by_page_type(page_items)
    observed_set = set(evidence_by_page_type)
    observed_page_types = _catalog_order(observed_set)
    business_observed_page_types = [
        page_type for page_type in observed_page_types if page_type not in _LIFECYCLE_PAGE_TYPES
    ]
    lifecycle_observed_page_types = [
        page_type for page_type in observed_page_types if page_type in _LIFECYCLE_PAGE_TYPES
    ]
    normalized_assessments = _normalized_assessments(
        assessment_items,
        evidence_by_page_type=evidence_by_page_type,
        action_items=action_items,
    )
    dynamic_required_page_types = tuple(
        page_type
        for page_type in dict.fromkeys(
            [
                *(required_page_types or ()),
                *(
                    _normalized_page_type(item.get("expected_next_page_type"))
                    for item in action_items
                    if _is_business_frontier_action(item)
                ),
            ]
        )
        # 经过的页面类型不进分母
        if not inspection_scope or page_type in inspection_scope
    )
    product_business = _product_business_summary(
        normalized_assessments,
        assessment_scope_complete=assessment_scope_complete,
        required_page_types=dynamic_required_page_types,
    )
    exploration = _exploration_summary(action_items)
    page_work_complete = all(
        _text(item.get("inspection_lifecycle_status") or item.get("lifecycle_status"))
        not in {None, "active", "unknown"}
        or (_text(item.get("inspection_status")) or "unknown") in _TERMINAL_PAGE_STATUSES
        for item in page_items
    )
    business_rate = product_business["coverage_rate"]
    exploration_rate = exploration["terminal_rate"]
    if not product_business["assessment_available"] or exploration_rate is None:
        acceptance_complete: bool | None = None
    else:
        acceptance_complete = bool(
            product_business["assessment_complete"]
            and business_rate is not None
            and business_rate >= 0.8
            and exploration_rate >= 0.9
            and page_work_complete
        )

    return {
        "catalog_observation": {
            "catalog_page_types": list(_CATALOG_PAGE_TYPES),
            "catalog_page_type_total": len(_CATALOG_PAGE_TYPES),
            "observed_page_types": observed_page_types,
            "observed_page_type_count": len(observed_page_types),
            "business_observed_page_types": business_observed_page_types,
            "missing_catalog_page_types": [
                page_type for page_type in _CATALOG_PAGE_TYPES if page_type not in observed_set
            ],
            "raw_coverage_rate": round(
                len(observed_page_types) / len(_CATALOG_PAGE_TYPES),
                4,
            ),
        },
        "lifecycle_shells": {
            "observed_page_types": lifecycle_observed_page_types,
            "strict_line_loading_observed": "line_loading" in observed_set,
            "startup_ad_observed": "startup_ad" in observed_set,
            "loading_state_observed": any(
                _page_state(item) in {"loading", "line_loading"}
                and _page_type(item) != "line_loading"
                for item in page_items
                if _page_has_strict_evidence(item, require_line_loading_state=False)
            ),
        },
        "product_business": product_business,
        "exploration": exploration,
        "gaps": normalized_assessments,
        "acceptance_complete": acceptance_complete,
    }


def _evidence_by_page_type(
    page_items: list[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    evidence: dict[str, dict[str, Any]] = {}
    for item in page_items:
        if not _page_has_strict_evidence(item, require_line_loading_state=True):
            continue
        page_type = _page_type(item)
        capture_id = _text(item.get("page_capture_id"))
        if page_type is None or capture_id is None:
            continue
        artifact_ids = set(_artifact_ids(item))
        entry = evidence.setdefault(
            page_type,
            {
                "page_capture_ids": set(),
                "evidence_artifact_ids": set(),
                "artifacts_by_capture": {},
            },
        )
        entry["page_capture_ids"].add(capture_id)
        entry["evidence_artifact_ids"].update(artifact_ids)
        entry["artifacts_by_capture"][capture_id] = artifact_ids
    return evidence


def _page_has_strict_evidence(
    item: Mapping[str, Any],
    *,
    require_line_loading_state: bool,
) -> bool:
    page_type = _page_type(item)
    if page_type not in _CATALOG_PAGE_TYPES:
        return False
    if _detection_status(item) != "recognized":
        return False
    if _text(item.get("page_capture_id")) is None or not _artifact_ids(item):
        return False
    return not (
        require_line_loading_state
        and page_type == "line_loading"
        and _page_state(item) != "line_loading"
    )


def _normalized_assessments(
    assessment_items: list[Mapping[str, Any]],
    *,
    evidence_by_page_type: dict[str, dict[str, Any]],
    action_items: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    artifacts_by_capture = {
        capture_id: set(artifact_ids)
        for evidence in evidence_by_page_type.values()
        for capture_id, artifact_ids in evidence["artifacts_by_capture"].items()
    }
    actions_by_id = {
        action_id: item
        for item in action_items
        if (action_id := _text(item.get("exploration_action_id"))) is not None
    }
    for item in assessment_items:
        page_type = _normalized_page_type(item.get("page_type"))
        status = (_text(item.get("status")) or "").casefold()
        if page_type not in _BUSINESS_PAGE_TYPES or status not in _ASSESSMENT_STATUSES:
            continue
        page_capture_ids = _string_list(item.get("page_capture_ids"))
        exploration_action_ids = _string_list(item.get("exploration_action_ids"))
        evidence_artifact_ids = _string_list(item.get("evidence_artifact_ids"))
        reason_code = _text(item.get("reason_code")) or "unspecified"
        if status == "covered":
            evidence = evidence_by_page_type.get(page_type)
            if not _has_matching_capture_artifact_pair(
                page_capture_ids=page_capture_ids,
                evidence_artifact_ids=evidence_artifact_ids,
                artifacts_by_capture=(
                    evidence["artifacts_by_capture"] if evidence is not None else {}
                ),
            ):
                status = "evidence_insufficient"
                reason_code = "covered_without_matching_evidence"
        elif status in {"not_applicable", "requires_auth", "blocked_by_safety"}:
            if not _has_auditable_assessment_reference(
                page_type=page_type,
                status=status,
                page_capture_ids=page_capture_ids,
                evidence_artifact_ids=evidence_artifact_ids,
                exploration_action_ids=exploration_action_ids,
                artifacts_by_capture=artifacts_by_capture,
                actions_by_id=actions_by_id,
            ):
                invalid_status = status
                status = "evidence_insufficient"
                reason_code = f"{invalid_status}_without_auditable_reference"
        candidate = {
            "page_type": page_type,
            "status": status,
            "reason_code": reason_code,
            "page_capture_ids": page_capture_ids,
            "exploration_action_ids": exploration_action_ids,
            "evidence_artifact_ids": evidence_artifact_ids,
        }
        # 同一页型往往有多条断言（一次探索可能多次到达同一页型）。此处原为
        # 后写覆盖，谁排在最后谁说了算，而读取顺序是 page_capture_id 的
        # UUID 字典序 —— 等于让覆盖数字取决于运气。实测十八汇（SF-129）
        # content_list 六条里三条取证齐备，只因排最后的一条配不上就整个页型
        # 判成未决；video_list 同样三通过三不通过，只因最后一条恰好通过就算
        # 覆盖。改为取最优：任何一条带齐取证的 covered 都足以证明该页型到达过。
        # 取证不齐的 covered 在上面已降级为 evidence_insufficient，排在最低，
        # 不会盖掉真正有证据的那条，fail-closed 判据本身不受影响。
        previous = normalized.get(page_type)
        if previous is None or _assessment_status_rank(status) > _assessment_status_rank(
            str(previous["status"])
        ):
            normalized[page_type] = candidate
    return [normalized[page_type] for page_type in _BUSINESS_PAGE_TYPES if page_type in normalized]


# 覆盖 > 有原因的未覆盖 > 本产品不适用 > 未找到 > 证据不足
_ASSESSMENT_STATUS_RANK = {
    "covered": 4,
    "requires_auth": 3,
    "blocked_by_safety": 3,
    "not_applicable": 2,
    "not_found": 1,
    "evidence_insufficient": 0,
}


def _assessment_status_rank(status: str) -> int:
    return _ASSESSMENT_STATUS_RANK.get(status.casefold(), 0)


def _has_matching_capture_artifact_pair(
    *,
    page_capture_ids: list[str],
    evidence_artifact_ids: list[str],
    artifacts_by_capture: Mapping[str, set[str]],
) -> bool:
    referenced_artifacts = set(evidence_artifact_ids)
    return any(
        referenced_artifacts.intersection(artifacts_by_capture.get(capture_id, set()))
        for capture_id in page_capture_ids
    )


def _has_auditable_assessment_reference(
    *,
    page_type: str,
    status: str,
    page_capture_ids: list[str],
    evidence_artifact_ids: list[str],
    exploration_action_ids: list[str],
    artifacts_by_capture: Mapping[str, set[str]],
    actions_by_id: Mapping[str, Mapping[str, Any]],
) -> bool:
    has_capture_evidence = _has_matching_capture_artifact_pair(
        page_capture_ids=page_capture_ids,
        evidence_artifact_ids=evidence_artifact_ids,
        artifacts_by_capture=artifacts_by_capture,
    )
    matching_actions = [
        action
        for action_id in exploration_action_ids
        if (action := actions_by_id.get(action_id)) is not None
        and _normalized_page_type(action.get("expected_next_page_type")) == page_type
    ]
    if status == "not_applicable":
        return has_capture_evidence or bool(matching_actions)
    if status == "blocked_by_safety":
        return any(
            (_text(action.get("status")) or "").casefold() in _TERMINAL_ACTION_STATUSES
            and (
                (_text(action.get("risk_level")) or "").casefold() in {"high", "critical"}
                or "safety" in (_text(action.get("failure_code")) or "").casefold()
                or "high_risk" in (_text(action.get("failure_reason")) or "").casefold()
            )
            for action in matching_actions
        )
    return has_capture_evidence or any(
        (_text(action.get("status")) or "").casefold() in _TERMINAL_ACTION_STATUSES
        for action in matching_actions
    )


def _product_business_summary(
    assessments: list[dict[str, Any]],
    *,
    assessment_scope_complete: bool,
    required_page_types: Iterable[str] | None,
) -> dict[str, Any]:
    target_page_types = _target_business_page_types(
        required_page_types,
        assessed_page_types=(item["page_type"] for item in assessments),
    )
    target_page_type_set = set(target_page_types)
    target_assessments = [
        item for item in assessments if item["page_type"] in target_page_type_set
    ]
    assessed_page_types = {item["page_type"] for item in assessments}
    missing_assessment_page_types = [
        page_type for page_type in target_page_types if page_type not in assessed_page_types
    ]
    # 声明为目标却整轮没走到的页型：既不是「本产品不适用」也不是「不确定」，
    # 这一轮就是没覆盖到。此前它们只落在 missing_assessment_page_types，
    # 既不进分母也不进任何结论桶，于是 scope_verified 恒假、比率永远留空
    # —— 探索越不完整，那个唯一对外的比率越是空的。改为按「已知未覆盖」
    # 计入分母，比率因此总能算出来，且没探到只会拉低而不会虚高。
    # 只在本轮确有断言时补判；一条断言都没有说明这轮压根没跑起来，不臆断。
    never_reached_page_types = (
        tuple(missing_assessment_page_types) if assessment_scope_complete else ()
    )
    assessment_scope_verified = bool(assessment_scope_complete)
    applicable = [
        item for item in target_assessments if item["status"] != "not_applicable"
    ]
    applicable_page_types = _catalog_order(
        {item["page_type"] for item in applicable} | set(never_reached_page_types)
    )
    covered_page_types = _catalog_order(
        {item["page_type"] for item in applicable if item["status"] == "covered"}
    )
    unresolved_page_types = _catalog_order(
        {
            item["page_type"]
            for item in applicable
            if item["status"] in _UNCERTAIN_ASSESSMENT_STATUSES
        }
    )
    known_uncovered_page_types = _catalog_order(
        {
            item["page_type"]
            for item in applicable
            if item["status"] in {"requires_auth", "blocked_by_safety"}
        }
        | set(never_reached_page_types)
    )
    not_applicable_page_types = _catalog_order(
        {
            item["page_type"]
            for item in target_assessments
            if item["status"] == "not_applicable"
        }
    )
    assessment_available = bool(target_assessments)
    assessment_complete = bool(
        assessment_available
        and assessment_scope_verified
        and applicable_page_types
        and not unresolved_page_types
    )
    applicable_count = len(applicable_page_types) if assessment_available else None
    coverage_rate = (
        round(len(covered_page_types) / len(applicable_page_types), 4)
        if assessment_complete and applicable_page_types
        else None
    )
    return {
        "assessment_available": assessment_available,
        "assessment_scope_complete": assessment_scope_complete,
        "assessment_scope_verified": assessment_scope_verified,
        "assessment_complete": assessment_complete,
        "missing_assessment_page_types": missing_assessment_page_types,
        "applicable_page_types": (applicable_page_types if assessment_available else None),
        "covered_page_types": covered_page_types,
        "known_uncovered_page_types": known_uncovered_page_types,
        "unresolved_page_types": unresolved_page_types,
        "not_applicable_page_types": not_applicable_page_types,
        "covered_count": len(covered_page_types),
        "applicable_count": applicable_count,
        "coverage_rate": coverage_rate,
    }


def _target_business_page_types(
    required_page_types: Iterable[str] | None,
    *,
    assessed_page_types: Iterable[str] = (),
) -> tuple[str, ...]:
    required = {
        normalized
        for item in (required_page_types or ())
        if (normalized := _normalized_page_type(item)) in _BUSINESS_PAGE_TYPES
    }
    if not required:
        required = {
            normalized
            for item in assessed_page_types
            if (normalized := _normalized_page_type(item)) in _BUSINESS_PAGE_TYPES
        }
    return tuple(page_type for page_type in _BUSINESS_PAGE_TYPES if page_type in required)


def _exploration_summary(action_items: list[Mapping[str, Any]]) -> dict[str, Any]:
    eligible_actions = [item for item in action_items if _is_business_frontier_action(item)]
    statuses = [_text(item.get("status")) or "unknown" for item in eligible_actions]
    terminal_count = sum(status in _TERMINAL_ACTION_STATUSES for status in statuses)
    action_total = len(eligible_actions)
    return {
        "action_total": action_total,
        "terminal_count": terminal_count,
        "pending_count": sum(status in _PENDING_ACTION_STATUSES for status in statuses),
        "explored_count": statuses.count("explored"),
        "blocked_count": statuses.count("blocked"),
        "skipped_count": statuses.count("skipped"),
        "failed_count": statuses.count("failed"),
        "terminal_rate": (round(terminal_count / action_total, 4) if action_total else None),
        "explored_rate": (
            round(statuses.count("explored") / action_total, 4) if action_total else None
        ),
    }


def _is_business_frontier_action(item: Mapping[str, Any]) -> bool:
    source_page_type = _normalized_page_type(item.get("source_page_type"))
    expected_page_type = _normalized_page_type(item.get("expected_next_page_type"))
    purpose = (_text(item.get("purpose")) or "").casefold()
    return bool(
        (_text(item.get("origin")) or "").casefold() == "discovered"
        and (_text(item.get("risk_level")) or "").casefold() == "low"
        and (_text(item.get("action")) or "").casefold() in _BUSINESS_ACTIONS
        and expected_page_type in _CATALOG_PAGE_TYPES
        and expected_page_type != source_page_type
        and item.get("mandatory") is not True
        and item.get("blocks_page_inspection") is not True
        and item.get("side_evidence_only") is not True
        and not purpose.startswith(
            (
                "rule_evidence:",
                "restore_source_",
                "transient_recovery:",
                "post_interaction_page_observation",
            )
        )
    )


def _catalog_order(page_types: set[str]) -> list[str]:
    return [page_type for page_type in _CATALOG_PAGE_TYPES if page_type in page_types]


def _normalized_page_type(value: Any) -> str | None:
    text = _text(value)
    return text.casefold() if text is not None else None


def _page_type(item: Mapping[str, Any]) -> str | None:
    detection = item.get("page_detection")
    detection = detection if isinstance(detection, Mapping) else {}
    return _normalized_page_type(item.get("page_type") or detection.get("page_type"))


def _page_state(item: Mapping[str, Any]) -> str | None:
    detection = item.get("page_detection")
    detection = detection if isinstance(detection, Mapping) else {}
    value = _text(item.get("page_state") or detection.get("page_state"))
    return value.casefold() if value is not None else None


def _detection_status(item: Mapping[str, Any]) -> str | None:
    detection = item.get("page_detection")
    detection = detection if isinstance(detection, Mapping) else {}
    value = _text(
        item.get("detection_status") or detection.get("detection_status") or detection.get("status")
    )
    return value.casefold() if value is not None else None


def _artifact_ids(item: Mapping[str, Any]) -> list[str]:
    return _string_list(item.get("artifact_ids"))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = None
        value = decoded if isinstance(decoded, list) else [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    return [text for item in value if (text := _text(item)) is not None]


def _deduplicated(items: Iterable[Mapping[str, Any]], identifier: str) -> list[Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    anonymous: list[Mapping[str, Any]] = []
    for item in items:
        item_id = _text(item.get(identifier))
        if item_id is None:
            anonymous.append(item)
        else:
            result[item_id] = item
    return [*result.values(), *anonymous]


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _page_result(item: Mapping[str, Any]) -> dict[str, Any]:
    """把数据库行与 WorkingMemory 页面快照归一化为同一个逐页结果契约。"""
    detection = item.get("page_detection")
    detection = detection if isinstance(detection, Mapping) else {}
    failure = item.get("failure")
    failure = failure if isinstance(failure, Mapping) else {}
    result = {
        "page_capture_id": _text(item.get("page_capture_id")),
        "parent_page_capture_id": _text(item.get("parent_page_capture_id")),
        "supersedes_page_capture_id": _text(item.get("supersedes_page_capture_id")),
        "page_seq": item.get("page_seq"),
        "page_type": _page_type(item),
        "page_url": _text(item.get("page_url") or item.get("url")),
        "page_title": _text(item.get("page_title")),
        "captured_at": item.get("captured_at"),
        "detection_status": _detection_status(item),
        "lifecycle_status": _text(
            item.get("inspection_lifecycle_status") or item.get("lifecycle_status")
        )
        or "unknown",
        "inspection_status": _text(item.get("inspection_status")) or "unknown",
        "failure_stage": _text(item.get("inspection_failure_stage") or failure.get("stage")),
        "failure_code": _text(item.get("inspection_failure_code") or failure.get("code")),
        "failure_message": _text(item.get("inspection_failure_message") or failure.get("message")),
        "failure_retryable": item.get("inspection_failure_retryable")
        if item.get("inspection_failure_retryable") is not None
        else failure.get("retryable"),
        "abandon_reason": _text(
            item.get("inspection_abandon_reason") or item.get("abandon_reason")
        ),
    }
    page_state = _page_state(item)
    if page_state is not None:
        result["page_state"] = page_state
    risk_flags = _string_list(item.get("risk_flags") or detection.get("risk_flags"))
    if risk_flags:
        result["risk_flags"] = risk_flags
    conflicts = _string_list(item.get("conflicts") or detection.get("conflicts"))
    if conflicts:
        result["conflicts"] = conflicts
    if (
        page_state in _SAFETY_BLOCKED_PAGE_STATES
        or set(flag.casefold() for flag in risk_flags) & _SAFETY_BLOCKED_RISK_FLAGS
    ):
        result["target_access_status"] = "blocked_by_safety_interstitial"
    app_metadata = {
        "package_name": _text(item.get("package_name")),
        "activity": _text(item.get("activity")),
        "width": item.get("width"),
        "height": item.get("height"),
    }
    if any(value is not None for value in app_metadata.values()):
        result.update(app_metadata)
    artifact_ids = _artifact_ids(item)
    if artifact_ids:
        result["artifact_ids"] = artifact_ids
    logical_surface = _logical_surface_from_page(item)
    if logical_surface is not None:
        result["logical_surface"] = _public_logical_surface(logical_surface)
    return result


def _action_result(item: Mapping[str, Any]) -> dict[str, Any]:
    """投影可核验的 Frontier 动作身份，不复制浏览器正文或模型载荷。"""

    parameters = item.get("parameters")
    parameters = dict(parameters) if isinstance(parameters, Mapping) else {}
    result = {
        "exploration_action_id": _text(
            item.get("exploration_action_id") or item.get("action_execution_id")
        ),
        "action": _text(item.get("action")),
        "target": (
            None
            if _is_visual_navigation_action(item)
            else _text(item.get("target"))
        ),
        "purpose": _text(item.get("purpose")),
        "risk_level": _text(item.get("risk_level")),
        "status": _text(item.get("status")) or "unknown",
        "execution_status": _text(item.get("execution_status")),
        "command_sequence": item.get("command_sequence"),
        "source_page_capture_id": _text(item.get("source_page_capture_id")),
        "requested_source_capture_id": _text(item.get("requested_source_capture_id")),
        "result_page_capture_id": _text(item.get("result_page_capture_id")),
        "expected_next_page_type": _normalized_page_type(item.get("expected_next_page_type")),
        "failure_code": _text(item.get("failure_code")),
        "failure_message": _text(item.get("failure_message")),
        "parameters": {
            key: parameters[key]
            for key in (
                "target_mode",
                "popup_dismissal_capability",
                "startup_ad_capability",
                "navigation_depth",
            )
            if key in parameters
        },
    }
    transition = _logical_transition_from_action(item)
    if transition is not None:
        result["logical_surface_transition"] = _public_logical_transition(
            transition
        )
    return result


def _public_logical_surface(surface: Mapping[str, Any]) -> dict[str, Any]:
    """逐页结果仅公开审计摘要，禁止复制身份信号中的页面文字。"""
    profile = surface.get("render_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    binding = surface.get("page_type_binding")
    binding = binding if isinstance(binding, Mapping) else {}
    return {
        "protocol_version": "logical_surface.v1",
        "render_profile": {
            "kind": _text(profile.get("kind")),
            "status": _text(profile.get("status")),
            "confidence": _bounded_report_confidence(profile.get("confidence")),
        },
        "document_identity": _logical_fingerprint(surface.get("document_identity")),
        "identity_fingerprint": _logical_fingerprint(
            surface.get("identity_fingerprint")
        ),
        "state_fingerprint": _logical_fingerprint(surface.get("state_fingerprint")),
        "evidence_sources": _string_list(surface.get("evidence_sources")),
        "evidence_quality": _text(surface.get("evidence_quality")),
        "degraded_reasons": _string_list(surface.get("degraded_reasons")),
        "shadow": surface.get("shadow") is True,
        "page_type_binding": {
            "page_type": _normalized_page_type(binding.get("page_type")),
            "page_type_confidence": _bounded_report_confidence(
                binding.get("page_type_confidence")
            ),
            "confidence_source": _text(binding.get("confidence_source")),
            "coverage_allowed": binding.get("coverage_allowed") is True,
            "rule_matching_allowed": binding.get("rule_matching_allowed") is True,
            "reason_code": _text(binding.get("reason_code")),
        },
    }


def _public_logical_transition(
    transition: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": _text(transition.get("transition_kind")),
        "logical_transition_proof": _text(
            transition.get("logical_transition_proof")
        ),
        "before_identity_fingerprint": _logical_fingerprint(
            transition.get("before_identity_fingerprint")
        ),
        "after_identity_fingerprint": _logical_fingerprint(
            transition.get("after_identity_fingerprint")
        ),
        "before_state_fingerprint": _logical_fingerprint(
            transition.get("before_state_fingerprint")
        ),
        "after_state_fingerprint": _logical_fingerprint(
            transition.get("after_state_fingerprint")
        ),
        "corroborating_signals": _string_list(
            transition.get("corroborating_signals")
        ),
        "reason_code": _text(transition.get("reason_code")),
    }
