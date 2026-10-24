"""App 运行时产品能力评估影子（M2）：静态声明 ≠ 入口后真实能力。

不得因「没找到」静默缩小冻结必检分母；正式能力权威仍在目标冻结／Catalog。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

APP_RUNTIME_CAPABILITY_SHADOW_VERSION = "app-runtime-capability-shadow.v1"


def build_app_runtime_capability_shadow(
    *,
    declared_required_page_types: Sequence[str] = (),
    observed_page_type: str | None = None,
    entry_layer_kind: str | None = None,
    presentation_layer: str | None = None,
    package_within_boundary: bool = True,
) -> dict[str, object]:
    """入口后暂定能力影子；只解释探索优先级，不改写冻结分母。"""

    declared = tuple(
        str(item).strip().casefold()
        for item in declared_required_page_types
        if isinstance(item, str) and item.strip()
    )
    observed = (observed_page_type or "").strip().casefold() or None
    entry = (entry_layer_kind or "unknown").strip().casefold() or "unknown"
    presentation = (presentation_layer or "unknown").strip().casefold() or "unknown"
    reasons: list[str] = []
    if not package_within_boundary:
        reasons.append("outside_target_package")
    if entry in {"startup_ad", "countdown", "splash", "auth_gate", "login_required", "overlay"}:
        reasons.append("entry_layer_blocks_capability_confirmation")
    if presentation in {"overlay", "transitional", "external"}:
        reasons.append(f"presentation_{presentation}_not_business_confirmation")

    observed_capabilities: list[str] = []
    if (
        package_within_boundary
        and observed
        and entry in {"business_surface", "unknown"}
        and presentation == "business_surface"
    ):
        observed_capabilities.append(observed)
    elif observed and entry not in {"business_surface"}:
        reasons.append("observed_page_type_under_blocking_entry")

    unconfirmed = [item for item in declared if item not in observed_capabilities]
    from services.api.app.application.app_network_proxy import get_app_network_proxy_hub

    network_fault = get_app_network_proxy_hub().network_fault_status()

    # 硬约束：未观察 ≠ 不适用；分母保持 declared。
    return {
        "protocol_version": APP_RUNTIME_CAPABILITY_SHADOW_VERSION,
        "authority": False,
        "shrinks_frozen_denominator": False,
        "static_portrait_equals_runtime_capability": False,
        "declared_required_page_types": list(declared),
        "observed_capabilities": observed_capabilities,
        "unconfirmed_required_page_types": unconfirmed,
        "entry_layer_kind": entry,
        "presentation_layer": presentation,
        "missing_entry_means_not_applicable": False,
        "reason_codes": reasons,
        "network_fault": network_fault,
    }


def capability_shadow_from_coverage_inputs(
    *,
    goal_required_page_types: Sequence[str] | None,
    analysis_page_type: str | None,
    entry_layer: Mapping[str, object] | None,
    presentation_layer: str | None = None,
    package_within_boundary: bool = True,
) -> dict[str, object]:
    """从覆盖回合输入构造能力影子，供 metadata 审计。"""

    entry = entry_layer if isinstance(entry_layer, Mapping) else {}
    return build_app_runtime_capability_shadow(
        declared_required_page_types=goal_required_page_types or (),
        observed_page_type=analysis_page_type,
        entry_layer_kind=str(entry.get("kind") or "unknown"),
        presentation_layer=presentation_layer or str(entry.get("presentation_layer") or "unknown"),
        package_within_boundary=package_within_boundary,
    )
