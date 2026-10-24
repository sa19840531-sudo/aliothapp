"""App 图恢复与补采（M6）：Route Planner、等价门禁、CaptureSeries；Lab 可开 runtime_ready。

生产默认 ``authority.M6=false``：
- 只生成可审计补采计划影子；
- ``runtime_ready=false``／``can_execute=false``／``executes_probe=false``。

Lab／灰度 ``build_lab_m6_recapture_milestone_policy()``：
- 计划可声明 ``runtime_ready=true``（复用 ``build_lab_supplemental_evidence_policy``）；
- 仅在等价门禁通过且路线可用时 ``can_execute=true``；
- **仍不自动派发 Probe**；禁止跨 epoch 坐标重放；fresh 不得填 exact。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any

from services.api.app.application.app_bounded_exploration_milestones import (
    build_app_bounded_exploration_milestone_policy,
    milestone_authority_enabled,
    milestone_shadow_enabled,
)
from services.api.app.application.page_graph_projection import build_recovery_routes
from services.api.app.application.supplemental_evidence_policy import (
    SUPPLEMENTAL_EVIDENCE_POLICY_VERSION,
    build_current_supplemental_evidence_policy,
    build_lab_supplemental_evidence_policy,
)
from services.api.app.domain.protocol import freeze_json_mapping

APP_GRAPH_RECAPTURE_VERSION = "app-graph-recapture.v2"
APP_CAPTURE_SERIES_VERSION = "app-capture-series.v1"
APP_EQUIVALENCE_POLICY_VERSION = "app-recapture-equivalence.v1"

_HIGH_RISK_RULE_MARKERS = frozenset(
    {
        "payment",
        "pay",
        "login",
        "auth",
        "password",
        "credential",
        "purchase",
        "checkout",
        "otp",
        "支付",
        "登录",
        "授权",
        "密码",
    }
)


class RecaptureEquivalenceMode(StrEnum):
    """规则预声明的补采等价模式。"""

    EXACT = "exact"
    EQUIVALENT = "equivalent"
    FRESH = "fresh"
    MANUAL = "manual"


def build_lab_m6_recapture_milestone_policy(
    overrides: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Lab／灰度显式打开 M6 补采 runtime_ready 声明；生产工厂不得调用。"""

    raw = dict(overrides) if isinstance(overrides, Mapping) else {}
    authority = dict(raw.get("authority") or {})
    shadow = dict(raw.get("shadow") or {})
    authority["M4"] = True
    authority["M5"] = True
    authority["M6"] = True
    shadow["M4"] = True
    shadow["M5"] = True
    shadow["M6"] = True
    return build_app_bounded_exploration_milestone_policy(
        {**raw, "authority": authority, "shadow": shadow},
        apply_production_env=False,
    )


def resolve_app_m6_recapture_active(
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """M6 影子／计划生成是否启用。"""

    return milestone_shadow_enabled(policy, "M6")


def resolve_app_m6_runtime_ready(
    policy: Mapping[str, Any] | None = None,
) -> bool:
    """是否允许 Lab 补采 runtime_ready；生产默认 false。"""

    return milestone_authority_enabled(policy, "M6")


def plan_app_graph_recovery_routes(
    *,
    graph: Mapping[str, Any] | None,
    source_state_id: str | None = None,
    target_identity: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Route Planner：复用图投影最短路线，强制逐步重验证；不执行设备。"""

    if not isinstance(graph, Mapping) or not graph:
        return {
            "protocol_version": APP_GRAPH_RECAPTURE_VERSION,
            "status": "unavailable",
            "reason_code": "graph_missing",
            "routes": [],
            "requires_stepwise_reverification": True,
            "executes_device_actions": False,
        }
    raw = build_recovery_routes(
        dict(graph),
        source_state_id=source_state_id,
        target_identity=target_identity,
        limit=limit,
    )
    routes = list(raw.get("routes") or [])
    scored = []
    for route in routes:
        steps = list(route.get("steps") or [])
        scored.append(
            {
                **dict(route),
                "step_count": len(steps),
                "cost": len(steps),
                "replay_coordinates_forbidden": True,
            }
        )
    scored.sort(key=lambda item: (int(item["cost"]), str(item.get("target_state_id") or "")))
    return {
        "protocol_version": APP_GRAPH_RECAPTURE_VERSION,
        "status": raw.get("status") or "unavailable",
        "reason_code": raw.get("reason_code"),
        "routes": scored,
        "requires_stepwise_reverification": True,
        "route_policy": raw.get("route_policy"),
        "executes_device_actions": False,
        "cross_epoch_coordinate_replay_forbidden": True,
    }


def assess_recapture_equivalence_gate(
    *,
    declared_mode: str | RecaptureEquivalenceMode | None,
    rule_code: str | None = None,
    high_risk: bool | None = None,
    filling_exact_gap: bool = False,
) -> dict[str, Any]:
    """等价门禁：fresh 不得填 exact 缺口；高风险默认 exact/manual。"""

    mode_raw = (
        declared_mode.value
        if isinstance(declared_mode, RecaptureEquivalenceMode)
        else str(declared_mode or "").strip().casefold()
    )
    try:
        mode = RecaptureEquivalenceMode(mode_raw) if mode_raw else RecaptureEquivalenceMode.EXACT
    except ValueError:
        mode = RecaptureEquivalenceMode.MANUAL

    rule = str(rule_code or "").strip().casefold()
    risky = high_risk
    if risky is None:
        risky = any(marker in rule for marker in _HIGH_RISK_RULE_MARKERS)

    reasons: list[str] = []
    allowed = True
    if filling_exact_gap and mode is RecaptureEquivalenceMode.FRESH:
        allowed = False
        reasons.append("fresh_cannot_fill_exact_gap")
    if risky and mode is RecaptureEquivalenceMode.EQUIVALENT:
        allowed = False
        reasons.append("high_risk_rejects_equivalent")
    if risky and mode is RecaptureEquivalenceMode.FRESH:
        allowed = False
        reasons.append("high_risk_rejects_fresh")
    if mode is RecaptureEquivalenceMode.MANUAL:
        allowed = False
        reasons.append("manual_requires_human")
    if allowed:
        reasons.append(f"mode_{mode.value}_accepted")

    return {
        "protocol_version": APP_EQUIVALENCE_POLICY_VERSION,
        "declared_mode": mode.value,
        "allowed": allowed,
        "high_risk": bool(risky),
        "filling_exact_gap": filling_exact_gap,
        "reason_codes": reasons,
        "overwrites_prior_conclusion": False,
    }


def build_app_capture_series_lineage(
    *,
    parent_capture_id: str | None,
    frame_capture_ids: Sequence[str] | None = None,
    evidence_role: str = "long_page_scroll_series",
    gap_codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """长页 CaptureSeries 血缘：派生帧必须指向父 Capture，不得冒充独立到达。"""

    parent = str(parent_capture_id or "").strip() or None
    frames = [str(item).strip() for item in (frame_capture_ids or ()) if str(item).strip()]
    return {
        "protocol_version": APP_CAPTURE_SERIES_VERSION,
        "parent_capture_id": parent,
        "frame_capture_ids": frames,
        "evidence_role": str(evidence_role or "long_page_scroll_series"),
        "gap_codes": [str(item) for item in (gap_codes or ())],
        "counts_as_independent_business_arrival": False,
        "requires_parent_lineage": True,
    }


def _capture_series_lineage_ok(capture_series: Mapping[str, Any] | None) -> bool:
    if capture_series is None:
        return True
    if not isinstance(capture_series, Mapping):
        return False
    if capture_series.get("counts_as_independent_business_arrival") is True:
        return False
    if capture_series.get("requires_parent_lineage") is True:
        return bool(str(capture_series.get("parent_capture_id") or "").strip())
    return True


def build_app_recapture_plan_shadow(
    *,
    evidence_request_id: str | None = None,
    target_identity: str | None = None,
    declared_mode: str | RecaptureEquivalenceMode | None = None,
    rule_code: str | None = None,
    routes: Mapping[str, Any] | None = None,
    capture_series: Mapping[str, Any] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    filling_exact_gap: bool = True,
) -> dict[str, Any]:
    """生成补采计划；生产 runtime_ready 恒 false，Lab authority 才可声明可执行。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    active = resolve_app_m6_recapture_active(policy)
    lab_authority = resolve_app_m6_runtime_ready(policy)
    supplemental = (
        build_lab_supplemental_evidence_policy(enabled=True)
        if lab_authority
        else build_current_supplemental_evidence_policy(enabled=True)
    )
    equivalence = assess_recapture_equivalence_gate(
        declared_mode=declared_mode or RecaptureEquivalenceMode.EXACT,
        rule_code=rule_code,
        filling_exact_gap=filling_exact_gap,
    )
    route_status = (
        str(routes.get("status") or "unavailable") if isinstance(routes, Mapping) else "unavailable"
    )
    lineage_ok = _capture_series_lineage_ok(
        dict(capture_series) if isinstance(capture_series, Mapping) else None
    )
    runtime_ready = bool(lab_authority and supplemental.runtime_ready)
    can_execute = bool(
        active
        and runtime_ready
        and equivalence.get("allowed") is True
        and route_status == "available"
        and lineage_ok
    )
    # 计划可执行 ≠ 自动派发 Probe；正式派发仍须独立调用链与副作用门禁。
    executes_probe = False

    reasons: list[str] = []
    if not active:
        reasons.append("m6_shadow_inactive")
    if not lab_authority:
        reasons.append("runtime_ready_false")
        reasons.append("m6_shadow_plan_only")
    else:
        reasons.append("lab_m6_runtime_ready_declared")
    if not equivalence.get("allowed"):
        reasons.append("equivalence_gate_blocked")
    if route_status not in {"available"}:
        reasons.append(f"route_{route_status}")
    if not lineage_ok:
        reasons.append("capture_series_lineage_invalid")
    if can_execute:
        reasons.append("lab_m6_plan_executable")
        reasons.append("probe_not_auto_dispatched")
    elif lab_authority and not can_execute:
        reasons.append("lab_m6_plan_not_executable")

    payload = {
        "protocol_version": APP_GRAPH_RECAPTURE_VERSION,
        "active": active,
        "milestone": "M6",
        "evidence_request_id": str(evidence_request_id or "").strip() or None,
        "target_identity": str(target_identity or "").strip() or None,
        "equivalence": equivalence,
        "routes": dict(routes) if isinstance(routes, Mapping) else None,
        "capture_series": dict(capture_series) if isinstance(capture_series, Mapping) else None,
        "supplemental_policy": {
            "enabled": supplemental.enabled,
            "runtime_ready": supplemental.runtime_ready,
            "policy_version": SUPPLEMENTAL_EVIDENCE_POLICY_VERSION,
            "lab_injected": lab_authority,
        },
        "executes_probe": executes_probe,
        "runtime_ready": runtime_ready,
        "can_execute": can_execute,
        "reason_codes": reasons,
        "overwrites_prior_conclusion": False,
        "cross_epoch_coordinate_replay_forbidden": True,
    }
    return dict(freeze_json_mapping(payload, "app_recapture_plan_shadow"))


def attach_app_m6_recapture_shadow(
    coverage_metadata: Mapping[str, Any] | None,
    *,
    graph: Mapping[str, Any] | None = None,
    source_state_id: str | None = None,
    target_identity: str | None = None,
    evidence_request_id: str | None = None,
    declared_mode: str | RecaptureEquivalenceMode | None = None,
    rule_code: str | None = None,
    parent_capture_id: str | None = None,
    frame_capture_ids: Sequence[str] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    filling_exact_gap: bool = True,
) -> dict[str, Any]:
    """写入 M6 图恢复／补采计划；生产不执行 Probe，Lab 仅声明可执行性。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    meta = dict(coverage_metadata) if isinstance(coverage_metadata, Mapping) else {}
    # 元数据可覆盖里程碑（与 M5 Lab 注入方式一致）。
    raw_override = meta.get("app_bounded_exploration_milestone_policy")
    if isinstance(raw_override, Mapping) and milestone_policy is None:
        policy = build_app_bounded_exploration_milestone_policy(raw_override)

    active = resolve_app_m6_recapture_active(policy)
    routes = plan_app_graph_recovery_routes(
        graph=graph if isinstance(graph, Mapping) else meta.get("app_page_graph_snapshot"),
        source_state_id=source_state_id,
        target_identity=target_identity
        or str(meta.get("business_page_identity") or "").strip()
        or None,
    )
    series = None
    if parent_capture_id or frame_capture_ids:
        series = build_app_capture_series_lineage(
            parent_capture_id=parent_capture_id,
            frame_capture_ids=frame_capture_ids,
        )
    plan = build_app_recapture_plan_shadow(
        evidence_request_id=evidence_request_id,
        target_identity=target_identity
        or str(meta.get("business_page_identity") or "").strip()
        or None,
        declared_mode=declared_mode,
        rule_code=rule_code,
        routes=routes,
        capture_series=series,
        milestone_policy=policy,
        filling_exact_gap=filling_exact_gap,
    )
    plan = {**plan, "active": active and bool(plan.get("active"))}
    meta["app_graph_recapture_shadow"] = plan
    return meta
