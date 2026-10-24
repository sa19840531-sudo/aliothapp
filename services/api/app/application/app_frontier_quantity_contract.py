"""App Frontier 数量合同：业务目标不限量，运行资源有界。

与统一设计报告对齐：导航／页面／分集目标不设固定上限作为正常完成条件；
deadline、max_actions、no-progress、单 Owner 等资源边界仍保留。
page_limit / max_pages 若存在，只作可选安全提示，不得截断动态 Frontier 候选，
也不得把「达到页数帽」写成正常完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

APP_FRONTIER_QUANTITY_CONTRACT_VERSION = "app-frontier-quantity.v1"

# 动态 Frontier 默认启用数量不限量；playbook 课纲仍可保留抽样帽。
_UNBOUNDED_EXPLORATION_MODES = frozenset({"dynamic_frontier"})


def exploration_mode_is_quantity_unbounded(exploration_mode: object) -> bool:
    """dynamic_frontier 业务目标数量无固定上限。"""

    return str(exploration_mode or "").strip() == "dynamic_frontier"


def policy_is_quantity_unbounded(policy: Mapping[str, Any] | None) -> bool:
    """从运行策略 dict／快照判断是否启用数量不限量。"""

    if not isinstance(policy, Mapping):
        return False
    flag = policy.get("quantity_unbounded")
    if flag is True:
        return True
    if flag is False:
        return False
    return exploration_mode_is_quantity_unbounded(policy.get("exploration_mode"))


def page_limit_may_truncate_navigation_targets(*, quantity_unbounded: bool) -> bool:
    """不限量策略下禁止用 page_limit 截断导航／页面候选。"""

    return not quantity_unbounded


def page_limit_blocks_new_page_candidates(
    *,
    quantity_unbounded: bool,
    page_limit: object,
    covered_count: object,
) -> bool:
    """是否应因页数帽停止发布新的 page／app 范围候选。

    不限量时永远 False；有限量时保持历史 first_n／抽样语义。
    """

    if quantity_unbounded:
        return False
    if (
        isinstance(page_limit, bool)
        or not isinstance(page_limit, int)
        or page_limit <= 0
        or isinstance(covered_count, bool)
        or not isinstance(covered_count, int)
    ):
        return False
    return covered_count >= page_limit


def build_quantity_contract_snapshot(
    *,
    exploration_mode: str,
    page_limit: int | None,
) -> dict[str, Any]:
    """写入策略快照的数量合同摘要。"""

    unbounded = exploration_mode_is_quantity_unbounded(exploration_mode)
    return {
        "protocol_version": APP_FRONTIER_QUANTITY_CONTRACT_VERSION,
        "quantity_unbounded": unbounded,
        "page_limit": page_limit,
        "page_limit_role": (
            "optional_safety_hint_not_completion"
            if unbounded
            else "sampling_cap"
        ),
        "normal_complete_forbids": (
            ("page_count_cap", "navigation_target_cap", "fixed_first_n_complete")
            if unbounded
            else ()
        ),
        "resource_bounds_remain": (
            "deadline",
            "max_actions",
            "max_navigation_depth",
            "no_progress",
            "single_broker_owner",
        ),
    }
