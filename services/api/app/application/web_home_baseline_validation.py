"""按冻结页型版本验证 Web 首页基线成功形状。"""

from collections.abc import Mapping

from services.api.app.domain.exploration import WEB_DETAIL_PAGE_TYPES, WEB_LIST_PAGE_TYPES

_LEGACY_HOME_PAGE_TYPE_VERSIONS = frozenset(
    {
        "web-page-type-normalization.v1",
        "web-page-type-assessment.v1",
        "web-page-type-assessment.v2",
    }
)


def persisted_home_baseline_has_valid_shape(
    row: Mapping[str, object],
    *,
    normalization_version: str,
) -> bool:
    """v3/v4 首页必须为正式 home；冻结 v1/v2 保留旧四类兼容。"""
    page_type = row.get("menu_page_type")
    if normalization_version in {
        "web-page-type-assessment.v3",
        "web-page-type-assessment.v4",
    }:
        page_type_is_valid = page_type == "home"
    elif normalization_version in _LEGACY_HOME_PAGE_TYPE_VERSIONS:
        page_type_is_valid = page_type in (WEB_LIST_PAGE_TYPES | WEB_DETAIL_PAGE_TYPES)
    else:
        return False
    detail_selection = row.get("detail_selection")
    detail_selection_is_empty = detail_selection in (None, "{}") or detail_selection == {}
    status = getattr(row.get("status"), "value", row.get("status"))
    return bool(
        status == "home_baseline"
        and row.get("decision") == "home_baseline"
        and row.get("execution_slot") is None
        and int(row.get("menu_action_count") or 0) == 0
        and int(row.get("detail_action_count") or 0) == 0
        and page_type_is_valid
        and str(row.get("menu_probe_capture_id") or "").strip()
        and str(row.get("menu_ready_capture_id") or "").strip()
        and row.get("menu_probe_capture_id") != row.get("menu_ready_capture_id")
        and all(
            not str(row.get(field_name) or "").strip()
            for field_name in (
                "menu_intent_id",
                "menu_source_capture_id",
                "menu_action_execution_id",
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
        and detail_selection_is_empty
    )
