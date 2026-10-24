"""App 验收矩阵（摘自统一报告）：导航目标数量与禁止项。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

APP_ACCEPTANCE_MATRIX_VERSION = "app-acceptance-matrix.v1"
NAV_TARGET_COUNT_CASES = (0, 9, 10, 11, 25)


def evaluate_nav_target_count_case(
    *,
    discovered_count: int,
    frontier_count: int,
    reused_web_menu_fields: bool = False,
    truncated_to_first_10: bool = False,
) -> dict[str, Any]:
    if discovered_count not in NAV_TARGET_COUNT_CASES:
        case_ok = True  # 非抽样点不强制
    else:
        case_ok = frontier_count >= discovered_count and not truncated_to_first_10
    forbidden = []
    if reused_web_menu_fields:
        forbidden.append("reused_web_menu_fields")
    if truncated_to_first_10:
        forbidden.append("fixed_first_10_truncation")
    return {
        "protocol_version": APP_ACCEPTANCE_MATRIX_VERSION,
        "case": "nav_target_counts",
        "discovered_count": discovered_count,
        "frontier_count": frontier_count,
        "passed": case_ok and not forbidden,
        "forbidden_observed": tuple(forbidden),
        "must_not": ("reused_web_menu_fields", "fixed_first_10_truncation"),
    }


def evaluate_deep_link_not_tap_pass(*, deep_link_counted_as_tap: bool) -> dict[str, Any]:
    return {
        "protocol_version": APP_ACCEPTANCE_MATRIX_VERSION,
        "case": "deep_link_not_native_tap",
        "passed": not deep_link_counted_as_tap,
        "must_not": ("deep_link_as_control_click_pass",),
    }


def evaluate_single_broker(*, concurrent_device_owners: bool) -> dict[str, Any]:
    return {
        "protocol_version": APP_ACCEPTANCE_MATRIX_VERSION,
        "case": "single_broker_owner",
        "passed": not concurrent_device_owners,
        "must_not": ("concurrent_device_owners",),
    }


def run_app_acceptance_suite(checks: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    items = [dict(x) for x in (checks or ()) if isinstance(x, Mapping)]
    passed = all(item.get("passed") is True for item in items) if items else False
    return {
        "protocol_version": APP_ACCEPTANCE_MATRIX_VERSION,
        "passed": passed,
        "checks": items,
    }
