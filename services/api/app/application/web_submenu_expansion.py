"""无 URL 菜单展开的独立前后证据门，普通同 URL 点击不能借此放行。"""

from collections.abc import Mapping

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_runtime_discovery import discovery_safety_reason
from services.api.app.domain.exploration import WEB_LIST_PAGE_TYPES


def _candidates(snapshot):
    navigation = snapshot.get("web_navigation") if isinstance(snapshot, Mapping) else None
    raw = navigation.get("candidates") if isinstance(navigation, Mapping) else None
    if not isinstance(raw, (list, tuple)):
        return ()
    try:
        return tuple(
            WebNavigationCandidate(**{**item, "menu_path": tuple(item["menu_path"])})
            for item in raw
        )
    except (KeyError, TypeError, ValueError):
        return ()


def proven_submenu_children(*, before_snapshot, after_snapshot, candidate, entry_id, site_origin):
    """父控件必须明确从折叠变展开，并出现同容器、正确父链的新可信子菜单。"""
    before = _candidates(before_snapshot)
    after = _candidates(after_snapshot)
    parents_before = [
        item for item in before if item.logical_target_identity == candidate.logical_target_identity
    ]
    parents_after = [
        item for item in after if item.logical_target_identity == candidate.logical_target_identity
    ]
    if len(parents_before) != 1 or len(parents_after) != 1:
        return ()
    if (
        "WEB_NAVIGATION_COLLAPSED" not in parents_before[0].navigation_evidence
        or "WEB_NAVIGATION_EXPANDED" not in parents_after[0].navigation_evidence
        or discovery_safety_reason(parents_after[0], site_origin=site_origin)
        or not candidate.navigation_container_id
    ):
        return ()
    before_ids = {item.logical_target_identity for item in before}
    return tuple(
        item.logical_target_identity
        for item in after
        if (
            item.logical_target_identity not in before_ids
            and item.parent_menu_entry_id == entry_id
            and item.parent_logical_target_identity == candidate.logical_target_identity
            and item.menu_depth == candidate.menu_depth + 1
            and item.exploration_path[: len(candidate.exploration_path)]
            == candidate.exploration_path
            and item.navigation_container_id == candidate.navigation_container_id
            and discovery_safety_reason(item, site_origin=site_origin) is None
        )
    )


def is_proven_submenu_expansion(result):
    """展开成功需要审计标记及完整交互成功证据，不能只依靠状态文本。"""
    outcome = result.menu_interaction_outcome
    return bool(
        result.diagnostic_type == "WEB_SUBMENU_EXPANDED"
        and result.final_capture_id
        and result.status.value == "menu_page_captured"
        and outcome is not None
        and outcome.transition_arrived
        and outcome.postcondition_satisfied
    )


def _persisted_menu_page_diagnostic_types(
    row: Mapping[str, object],
) -> tuple[str | None, str | None]:
    diagnostic = row.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
    top_level = row.get("diagnostic_type")
    nested = diagnostic.get("type")
    return (
        top_level.strip() if isinstance(top_level, str) and top_level.strip() else None,
        nested.strip() if isinstance(nested, str) and nested.strip() else None,
    )


def persisted_menu_page_diagnostic_is_consistent(row: Mapping[str, object]) -> bool:
    """两个持久诊断来源同时存在时必须完全一致。"""
    top_level, nested = _persisted_menu_page_diagnostic_types(row)
    return not (top_level is not None and nested is not None and top_level != nested)


def persisted_menu_page_is_submenu_expansion(row: Mapping[str, object]) -> bool:
    """任一持久诊断来源声明展开，都不能回退为普通列表页。"""
    return "WEB_SUBMENU_EXPANDED" in _persisted_menu_page_diagnostic_types(row)


def persisted_ordinary_menu_page_has_valid_shape(row: Mapping[str, object]) -> bool:
    """普通列表或集合菜单终态与子菜单展开终态互斥。"""
    status = getattr(row.get("status"), "value", row.get("status"))
    return bool(
        status == "menu_page_captured"
        and persisted_menu_page_diagnostic_is_consistent(row)
        and not persisted_menu_page_is_submenu_expansion(row)
        and int(row.get("menu_action_count") or 0) == 1
        and row.get("execution_slot") is not None
        and str(row.get("menu_intent_id") or "").strip()
        and str(row.get("menu_source_capture_id") or "").strip()
        and int(row.get("detail_action_count") or 0) == 0
        and row.get("menu_page_type") in WEB_LIST_PAGE_TYPES | {"collection_page"}
        and all(
            not str(row.get(field_name) or "").strip()
            for field_name in (
                "detail_intent_id",
                "detail_source_capture_id",
                "detail_candidate_id",
                "detail_destination_sha256",
                "detail_action_execution_id",
                "detail_probe_capture_id",
                "detail_ready_capture_id",
                "detail_page_type",
            )
        )
        and not row.get("detail_selection")
        and str(row.get("menu_action_execution_id") or "").strip()
        and str(row.get("menu_probe_capture_id") or "").strip()
        and str(row.get("menu_ready_capture_id") or "").strip()
        and row.get("menu_probe_capture_id") != row.get("menu_ready_capture_id")
    )


def persisted_submenu_expansion_has_valid_shape(row: Mapping[str, object]) -> bool:
    """只校验账本行内形状；Action/Capture 关系必须由仓储或证据层另行验证。"""
    diagnostic = row.get("diagnostic")
    diagnostic = diagnostic if isinstance(diagnostic, Mapping) else {}
    outcome = row.get("menu_interaction_outcome")
    if not isinstance(outcome, Mapping):
        outcome = diagnostic.get("menu_interaction_outcome")
    if not isinstance(outcome, Mapping):
        return False
    status = getattr(row.get("status"), "value", row.get("status"))
    ready_capture_id = str(row.get("menu_ready_capture_id") or "").strip()
    return bool(
        status == "menu_page_captured"
        and persisted_menu_page_diagnostic_is_consistent(row)
        and persisted_menu_page_is_submenu_expansion(row)
        and int(row.get("menu_action_count") or 0) == 1
        and int(row.get("detail_action_count") or 0) == 0
        and all(
            not str(row.get(field_name) or "").strip()
            for field_name in (
                "detail_intent_id",
                "detail_source_capture_id",
                "detail_candidate_id",
                "detail_destination_sha256",
                "detail_action_execution_id",
                "detail_probe_capture_id",
                "detail_ready_capture_id",
                "detail_page_type",
            )
        )
        and not row.get("detail_selection")
        and str(row.get("menu_intent_id") or "").strip()
        and str(row.get("menu_action_execution_id") or "").strip()
        and "menu_page_type" in row
        and row.get("menu_page_type") is None
        and "detail_page_type" in row
        and row.get("detail_page_type") is None
        and ready_capture_id
        and str(row.get("menu_probe_capture_id") or "").strip() == ready_capture_id
        and outcome.get("command_accepted") is True
        and outcome.get("effect_observed") is True
        and outcome.get("postcondition_satisfied") is True
        and outcome.get("transition_arrived") is True
        and "failure_reason" in outcome
        and outcome.get("failure_reason") is None
        and outcome.get("evidence_mode") == "natural"
        and str(outcome.get("after_capture_id") or "").strip() == ready_capture_id
    )
