"""App Settle 影子 Oracle（M1）：展示层／身份三明治／证据角色诊断。

正式 ``formal_settle_oracle`` 默认关闭；本模块只写入可审计影子结论，
不得单独把非 stable 帧提升为业务稳定证据。

#5 加深：页面稳定 quorum（前台／窗口／结构／连续帧）与网络能力缺口明示；
缺网时禁止伪造 ``network_stable=true``。
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

APP_CAPTURE_READINESS_DIAGNOSIS_VERSION = "app-capture-readiness-diagnosis.v4"
APP_SETTLE_SHADOW_VERSION = "app-settle-shadow.v2"
APP_ENTRY_LAYER_VERSION = "app-entry-layer.v1"
APP_PAGE_STABILITY_ASSESSMENT_VERSION = "app-page-stability-assessment.v1"
APP_SETTLE_ENFORCEMENT_PREVIEW_VERSION = "app-settle-enforcement-preview.v1"
_APP_STABILITY_FRAME_DIGEST_KEEP = 3


class SettleReadinessProfile(StrEnum):
    """动作后／再观察用途的固定就绪诊断剖面；非正式六层 Oracle。"""

    ACTION_AFTER = "ACTION_AFTER"
    REOBSERVE = "REOBSERVE"


_OVERLAY_STATES = frozenset(
    {
        "popup",
        "modal",
        "overlay",
        "startup_ad",
        "countdown",
        "auth_gate",
        "login_required",
        "system_dialog",
        "permission_dialog",
    }
)
_TRANSITIONAL_STATES = frozenset({"loading", "transitional", "unknown"})
_STARTUP_ENTRY_STATES = frozenset({"startup_ad", "countdown", "splash"})
_AUTH_ENTRY_STATES = frozenset({"auth_gate", "login_required"})
_SYSTEM_ENTRY_STATES = frozenset({"system_dialog", "permission_dialog"})


def classify_app_entry_layer(
    *,
    page_state: str | None,
    overlay_signature: str | None = None,
    surface_facts: Mapping[str, Any] | None = None,
    package_within_boundary: bool = True,
    presentation_layer: str | None = None,
) -> dict[str, object]:
    """APP_READY 后入口层协议：阻断层分型；禁止把就绪冒充业务首页。

    ``app_ready_equals_business_home`` 恒为 false；``underlying_home_fabricated`` 恒为
    false——底层业务页未知时不得伪造首页节点。
    """

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    state = (page_state or "unknown").strip().casefold() or "unknown"
    layer = presentation_layer or classify_app_presentation_layer(
        page_state=state,
        overlay_signature=overlay_signature,
        surface_facts={
            **dict(facts),
            "outside_target_package": not package_within_boundary,
        },
    )
    kind = "unknown"
    blocks_business_navigation = True
    if not package_within_boundary or layer == "external":
        kind = "external"
    elif state in _STARTUP_ENTRY_STATES:
        kind = state if state in {"startup_ad", "countdown"} else "splash"
    elif state in _AUTH_ENTRY_STATES or facts.get("auth_gate_visible") is True:
        kind = "auth_gate" if state != "login_required" else "login_required"
    elif state in _SYSTEM_ENTRY_STATES:
        kind = state
    elif layer == "overlay" or state in {"popup", "modal", "overlay"} or overlay_signature:
        kind = "overlay"
    elif layer == "transitional" or state in _TRANSITIONAL_STATES:
        kind = "transitional"
    elif layer == "business_surface" and state == "stable":
        kind = "business_surface"
        blocks_business_navigation = False
    return {
        "protocol_version": APP_ENTRY_LAYER_VERSION,
        "kind": kind,
        "page_state": state,
        "presentation_layer": layer,
        "blocks_business_navigation": blocks_business_navigation,
        # 合同硬约束：APP_READY 只表示设备／会话就绪，不等于业务首页到达。
        "app_ready_equals_business_home": False,
        "underlying_home_fabricated": False,
        "counts_as_business_home_arrival": False,
        "reason_codes": _entry_layer_reasons(
            kind=kind,
            state=state,
            overlay_signature=overlay_signature,
            package_within_boundary=package_within_boundary,
        ),
    }


def _entry_layer_reasons(
    *,
    kind: str,
    state: str,
    overlay_signature: str | None,
    package_within_boundary: bool,
) -> list[str]:
    reasons: list[str] = ["app_ready_not_business_home"]
    if not package_within_boundary:
        reasons.append("outside_target_package")
    if kind in {"startup_ad", "countdown", "splash"}:
        reasons.append("startup_entry_blocks_home")
    if kind in {"auth_gate", "login_required"}:
        reasons.append("auth_entry_blocks_home")
    if kind in {"system_dialog", "permission_dialog"}:
        reasons.append("system_entry_blocks_home")
    if kind == "overlay":
        reasons.append("overlay_blocks_home")
    if kind == "transitional":
        reasons.append(f"page_state_{state}")
    if overlay_signature:
        reasons.append("overlay_signature_present")
    if kind == "business_surface":
        reasons.append("business_surface_visible_not_proven_home")
    return reasons


def classify_app_presentation_layer(
    *,
    page_state: str | None,
    overlay_signature: str | None = None,
    surface_facts: Mapping[str, Any] | None = None,
) -> str:
    """当前真实展示层：business_surface / overlay / transitional / external / unknown。"""

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    state = (page_state or "unknown").strip().casefold() or "unknown"
    if facts.get("outside_target_package") is True:
        return "external"
    if state in _OVERLAY_STATES or overlay_signature or facts.get("popup_visible") is True:
        return "overlay"
    if state in _TRANSITIONAL_STATES:
        return "transitional"
    if state == "stable":
        return "business_surface"
    return "unknown"


def build_app_identity_sandwich(
    *,
    identity_before: Mapping[str, Any] | None = None,
    identity_after: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """动作／采集前后前台身份核对影子；缺测记 unavailable，不伪造一致。"""

    before = identity_before if isinstance(identity_before, Mapping) else None
    after = identity_after if isinstance(identity_after, Mapping) else None
    if before is None and after is None:
        return {
            "status": "unavailable",
            "matched": None,
            "before": None,
            "after": None,
            "reason_codes": ["identity_sample_missing"],
        }
    if before is None or after is None:
        return {
            "status": "partial",
            "matched": None,
            "before": dict(before) if before else None,
            "after": dict(after) if after else None,
            "reason_codes": ["identity_sample_incomplete"],
        }
    before_pkg = str(before.get("package_name") or "").strip()
    after_pkg = str(after.get("package_name") or "").strip()
    before_act = str(before.get("activity") or "").strip()
    after_act = str(after.get("activity") or "").strip()
    matched = before_pkg == after_pkg and (
        not before_act or not after_act or before_act == after_act
    )
    reasons: list[str] = []
    if before_pkg != after_pkg:
        reasons.append("package_drift")
    if before_act and after_act and before_act != after_act:
        reasons.append("activity_drift")
    return {
        "status": "matched" if matched else "drifted",
        "matched": matched,
        "before": {
            "package_name": before_pkg or None,
            "activity": before_act or None,
        },
        "after": {
            "package_name": after_pkg or None,
            "activity": after_act or None,
        },
        "reason_codes": reasons,
    }


def recommend_app_evidence_role(
    *,
    presentation_layer: str,
    ready_for_business_evidence: bool,
) -> str:
    if ready_for_business_evidence and presentation_layer == "business_surface":
        return "stable_business_capture"
    if presentation_layer == "overlay":
        return "overlay_occurrence"
    if presentation_layer == "external":
        return "external_landing"
    if presentation_layer == "transitional":
        return "transition_witness"
    return "diagnostic_only"


def resolve_app_ui_structure_status(
    surface_facts: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """UIA／受信控件是否可用；失败记 unavailable，不伪造空层级。"""

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    explicit = facts.get("ui_hierarchy_available")
    if explicit is False:
        return {
            "status": "unavailable",
            "meaningful": False,
            "reason_codes": ["ui_hierarchy_unavailable"],
        }
    if explicit is True:
        return {
            "status": "available",
            "meaningful": True,
            "reason_codes": [],
        }
    controls = facts.get("trusted_accessibility_controls")
    if isinstance(controls, Mapping):
        node_count = controls.get("node_count")
        if isinstance(node_count, int) and not isinstance(node_count, bool):
            meaningful = node_count > 0
            return {
                "status": "available" if meaningful else "unavailable",
                "meaningful": meaningful,
                "reason_codes": [] if meaningful else ["ui_hierarchy_empty"],
            }
        for key in ("bottom_navigation", "skip_controls", "dismiss_controls"):
            raw = controls.get(key)
            if isinstance(raw, (list, tuple)) and raw:
                return {
                    "status": "available",
                    "meaningful": True,
                    "reason_codes": [],
                }
        count = controls.get("bottom_navigation_count")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
            return {
                "status": "available",
                "meaningful": True,
                "reason_codes": [],
            }
        # 有控件对象但无可信项：视为贫瘠结构（Flutter／WebView 壳常见），非伪造成功。
        return {
            "status": "unavailable",
            "meaningful": False,
            "reason_codes": ["ui_structure_sparse"],
        }
    return {
        "status": "unknown",
        "meaningful": False,
        "reason_codes": ["ui_structure_not_sampled"],
    }


def compare_app_stability_frames(
    *,
    current_image_signature: str | None,
    prior_frame_digests: Sequence[str] | None = None,
    keep: int = _APP_STABILITY_FRAME_DIGEST_KEEP,
) -> dict[str, object]:
    """连续帧对比：同摘要视为静止，不同则 transitional；样本不足单独标明。"""

    current = str(current_image_signature or "").strip() or None
    prior: list[str] = []
    for item in prior_frame_digests or ():
        text = str(item or "").strip()
        if text and (not prior or prior[-1] != text):
            prior.append(text)
    if current is None:
        # 未传入摘要＝本诊断未采样连续帧，记缺口但不冒充「帧已对比失败」。
        return {
            "status": "not_sampled",
            "matched_previous": None,
            "sample_count": len(prior),
            "frame_digests": prior[-keep:],
            "reason_codes": ["frame_digest_not_sampled"],
        }
    if not prior:
        digests = [current]
        return {
            "status": "insufficient_samples",
            "matched_previous": None,
            "sample_count": 1,
            "frame_digests": digests[-keep:],
            "reason_codes": ["frame_quorum_needs_more_samples"],
        }
    previous = prior[-1]
    matched = previous == current
    digests = [*prior, current]
    # 去重尾部重复签名，保留时间序最近 keep 个。
    compact: list[str] = []
    for item in digests:
        if not compact or compact[-1] != item:
            compact.append(item)
    return {
        "status": "stable" if matched else "changed",
        "matched_previous": matched,
        "sample_count": len(compact),
        "frame_digests": compact[-keep:],
        "reason_codes": [] if matched else ["consecutive_frame_digest_changed"],
    }


def build_app_network_capability_record(
    *,
    network_observable: bool | None | str = None,
) -> dict[str, object]:
    """网络能力记录：不可得时写缺口，``network_stable`` 恒不得伪 true。"""

    raw = network_observable
    if raw is True or raw == "observable":
        status = "observable"
        # 本轮无 HTTPS 生命周期观测时，即使通道声明可观测也不伪造 idle／stable。
        network_stable = False
        gaps = ["network_lifecycle_not_proven"]
        reasons = ["network_observable_without_idle_proof"]
    elif raw is False or raw == "not_observable":
        status = "not_observable"
        network_stable = False
        gaps = ["network_lifecycle_unavailable"]
        reasons = ["network_not_observable"]
    else:
        status = "unknown"
        network_stable = False
        gaps = ["network_lifecycle_unavailable"]
        reasons = ["network_capability_unsampled"]
    return {
        "status": status,
        "network_stable": network_stable,
        "https_lifecycle_assumed": False,
        "forged_network_stable": False,
        "capability_gaps": gaps,
        "reason_codes": reasons,
    }


def build_app_page_stability_assessment(
    *,
    package_name: str | None = None,
    activity: str | None = None,
    page_state: str | None = None,
    overlay_signature: str | None = None,
    surface_facts: Mapping[str, Any] | None = None,
    package_within_boundary: bool = True,
    has_confirmed_visual_identity: bool = False,
    image_signature: str | None = None,
    prior_frame_digests: Sequence[str] | None = None,
    network_observable: bool | None | str = None,
    identity_before: Mapping[str, Any] | None = None,
    identity_after: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """App 页面稳定 quorum：前台＋窗口＋结构＋连续帧；网络缺口单独结算。

    - 有前台身份即可诊断截图（``screenshot_permitted``）。
    - ``stable_business_candidate`` 才可作业务稳定证据候选。
    - 缺网只记能力缺口，禁止 ``network_stable=true``。
    """

    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    pkg = str(package_name or "").strip() or None
    act = str(activity or "").strip() or None
    if identity_after is None and (pkg or act):
        identity_after = {"package_name": pkg, "activity": act}
    state = (page_state or "unknown").strip().casefold() or "unknown"
    presentation_layer = classify_app_presentation_layer(
        page_state=state,
        overlay_signature=overlay_signature,
        surface_facts={
            **dict(facts),
            "outside_target_package": not package_within_boundary,
        },
    )
    entry_layer = classify_app_entry_layer(
        page_state=state,
        overlay_signature=overlay_signature,
        surface_facts=facts,
        package_within_boundary=package_within_boundary,
        presentation_layer=presentation_layer,
    )
    identity_sandwich = build_app_identity_sandwich(
        identity_before=identity_before,
        identity_after=identity_after,
    )
    structure = resolve_app_ui_structure_status(facts)
    frames = compare_app_stability_frames(
        current_image_signature=image_signature,
        prior_frame_digests=prior_frame_digests,
    )
    network = build_app_network_capability_record(network_observable=network_observable)

    reasons: list[str] = []
    capability_gaps: list[str] = list(network["capability_gaps"])  # type: ignore[arg-type]
    if structure["status"] in {"unavailable", "unknown"}:
        capability_gaps.extend(
            str(item) for item in (structure.get("reason_codes") or []) if item
        )
        if structure["status"] == "unavailable":
            capability_gaps.append("ui_structure_unavailable")
        else:
            capability_gaps.append("ui_structure_unknown")
    if frames["status"] == "not_sampled":
        capability_gaps.append("frame_digest_not_sampled")
    elif frames["status"] == "unavailable":
        capability_gaps.append("frame_digest_unavailable")

    foreground_sampled = bool(pkg) or bool(
        isinstance(identity_after, Mapping) and identity_after.get("package_name")
    )
    foreground_ok = (bool(pkg) and package_within_boundary) if foreground_sampled else True
    window_ok = foreground_ok
    if not package_within_boundary:
        reasons.append("outside_target_package")
        screenshot_permitted = False
        foreground_ok = False
        window_ok = False
    elif foreground_sampled:
        screenshot_permitted = foreground_ok
    else:
        # 未采样前台时仍允许诊断截图路径；正式业务稳定另看 presentation／身份。
        screenshot_permitted = True
        reasons.append("foreground_not_sampled")
    if state in _TRANSITIONAL_STATES:
        reasons.append(f"page_state_{state}")
    if state in _OVERLAY_STATES or overlay_signature:
        reasons.append("display_layer_blocks_business")
    if identity_sandwich.get("status") == "drifted":
        reasons.append("identity_sandwich_drift")
    if frames["status"] == "changed":
        reasons.append("consecutive_frame_digest_changed")
    if frames["status"] == "insufficient_samples":
        reasons.append("frame_quorum_needs_more_samples")
    reasons.extend(str(item) for item in (network.get("reason_codes") or []) if item)

    # 去重保序
    capability_gaps = list(dict.fromkeys(capability_gaps))
    reasons = list(dict.fromkeys(reasons))

    frame_ok_for_stable = frames["status"] in {
        "stable",
        "insufficient_samples",
        "not_sampled",
    }
    stable_business_candidate = (
        screenshot_permitted
        and package_within_boundary
        and state == "stable"
        and presentation_layer == "business_surface"
        and entry_layer.get("kind") == "business_surface"
        and not overlay_signature
        and facts.get("popup_visible") is not True
        and facts.get("dialog_visible") is not True
        and facts.get("auth_gate_visible") is not True
        and has_confirmed_visual_identity
        and identity_sandwich.get("matched") is not False
        and frame_ok_for_stable
    )
    # 结构贫瘠不单独否决（Flutter 视觉降级），但记缺口。
    if identity_sandwich.get("matched") is False:
        stable_business_candidate = False

    if not package_within_boundary or (
        foreground_sampled and not screenshot_permitted
    ):
        stability_status = "blocked"
    elif presentation_layer in {"overlay", "external"} or state in _OVERLAY_STATES:
        stability_status = "blocked"
    elif state in _TRANSITIONAL_STATES or frames["status"] == "changed":
        stability_status = "transitional"
    elif stable_business_candidate:
        stability_status = "stable_candidate"
    else:
        stability_status = "insufficient"

    return {
        "protocol_version": APP_PAGE_STABILITY_ASSESSMENT_VERSION,
        "stability_status": stability_status,
        "screenshot_permitted": screenshot_permitted,
        "stable_business_candidate": stable_business_candidate,
        "foreground": {
            "package_name": pkg,
            "within_boundary": package_within_boundary,
            "ok": foreground_ok,
        },
        "current_window": {
            "activity": act,
            "ok": window_ok,
        },
        "ui_structure": structure,
        "frame_comparison": {
            "status": frames["status"],
            "matched_previous": frames["matched_previous"],
            "sample_count": frames["sample_count"],
            "reason_codes": list(frames.get("reason_codes") or []),
        },
        "frame_digests": list(frames.get("frame_digests") or []),
        "network": network,
        "network_stable": False if network.get("network_stable") is not True else True,
        "capability_gaps": capability_gaps,
        "presentation_layer": presentation_layer,
        "entry_layer": entry_layer,
        "identity_sandwich": identity_sandwich,
        "reason_codes": reasons,
        "formal_settle_oracle": False,
        "gate_consumes_settle": False,
    }


def build_app_capture_readiness_diagnosis(
    *,
    page_state: str | None,
    overlay_signature: str | None = None,
    surface_facts: Mapping[str, Any] | None = None,
    package_within_boundary: bool = True,
    has_confirmed_visual_identity: bool = False,
    network_observable: bool | None = None,
    identity_before: Mapping[str, Any] | None = None,
    identity_after: Mapping[str, Any] | None = None,
    milestone_policy: Mapping[str, Any] | None = None,
    profile: SettleReadinessProfile | str | None = None,
    package_name: str | None = None,
    activity: str | None = None,
    image_signature: str | None = None,
    prior_frame_digests: Sequence[str] | None = None,
    page_stability: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """按动作取证用途输出诊断与 Settle 影子；默认不启用正式 Oracle 权威。"""

    policy = (
        dict(milestone_policy)
        if isinstance(milestone_policy, Mapping)
        else build_app_bounded_exploration_milestone_policy()
    )
    resolved_profile: SettleReadinessProfile | None = None
    if profile is not None:
        resolved_profile = (
            profile
            if isinstance(profile, SettleReadinessProfile)
            else SettleReadinessProfile(str(profile))
        )
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    reasons: list[str] = []
    state = (page_state or "unknown").strip().casefold() or "unknown"
    # 缺测／剖面默认：网络不可得，禁止伪造可观测或 network_stable。
    effective_network: bool | None | str = network_observable
    if network_observable is None:
        effective_network = "not_observable"
        reasons.append("network_not_observable")
    elif network_observable is False:
        effective_network = "not_observable"
        reasons.append("network_not_observable")
    network_record = build_app_network_capability_record(
        network_observable=effective_network
    )
    stability = (
        dict(page_stability)
        if isinstance(page_stability, Mapping)
        else build_app_page_stability_assessment(
            package_name=package_name
            or (
                str(identity_after.get("package_name")).strip()
                if isinstance(identity_after, Mapping) and identity_after.get("package_name")
                else None
            ),
            activity=activity
            or (
                str(identity_after.get("activity")).strip()
                if isinstance(identity_after, Mapping) and identity_after.get("activity")
                else None
            ),
            page_state=state,
            overlay_signature=overlay_signature,
            surface_facts=facts,
            package_within_boundary=package_within_boundary,
            has_confirmed_visual_identity=has_confirmed_visual_identity,
            image_signature=image_signature,
            prior_frame_digests=prior_frame_digests,
            network_observable=effective_network,
            identity_before=identity_before,
            identity_after=identity_after,
        )
    )
    if not package_within_boundary:
        reasons.append("outside_target_package")
    if state in _TRANSITIONAL_STATES:
        reasons.append(f"page_state_{state}")
    if state in _OVERLAY_STATES:
        reasons.append("display_layer_blocks_business")
    if overlay_signature:
        reasons.append("overlay_signature_present")
    if facts.get("popup_visible") is True or facts.get("dialog_visible") is True:
        reasons.append("popup_or_dialog_visible")
    if facts.get("auth_gate_visible") is True:
        reasons.append("auth_gate_visible")
    if not has_confirmed_visual_identity and state == "stable":
        reasons.append("business_identity_unconfirmed")

    presentation_layer = classify_app_presentation_layer(
        page_state=state,
        overlay_signature=overlay_signature,
        surface_facts={
            **dict(facts),
            "outside_target_package": not package_within_boundary,
        },
    )
    entry_layer = classify_app_entry_layer(
        page_state=state,
        overlay_signature=overlay_signature,
        surface_facts=facts,
        package_within_boundary=package_within_boundary,
        presentation_layer=presentation_layer,
    )
    identity_sandwich = build_app_identity_sandwich(
        identity_before=identity_before,
        identity_after=identity_after,
    )
    if identity_sandwich.get("status") == "drifted":
        reasons.append("identity_sandwich_drift")
    if stability.get("stability_status") == "transitional":
        reasons.append("page_stability_transitional")
    for gap in stability.get("capability_gaps") or ():
        if isinstance(gap, str) and gap.strip():
            reasons.append(f"capability_gap:{gap.strip()}")

    ready_for_business_evidence = (
        package_within_boundary
        and state == "stable"
        and presentation_layer == "business_surface"
        and not overlay_signature
        and facts.get("popup_visible") is not True
        and facts.get("dialog_visible") is not True
        and facts.get("auth_gate_visible") is not True
        and has_confirmed_visual_identity
        and identity_sandwich.get("status") in {"matched", "unavailable", "partial"}
        and entry_layer.get("blocks_business_navigation") is False
        and stability.get("stable_business_candidate") is True
    )
    # 身份漂移时不可作业务稳定证据。
    if identity_sandwich.get("matched") is False:
        ready_for_business_evidence = False
    # 入口阻断层不得冒充业务稳定取证。
    if entry_layer.get("kind") not in {"business_surface"}:
        ready_for_business_evidence = False
    # 缺网不得伪造网络稳定；不依赖网络的业务 ready 仍可由展示层／身份／帧 quorum 决定。
    if network_record.get("network_stable") is True:
        # 合同硬约束：本函数路径永不允许伪造成功网络稳定。
        network_record = {
            **network_record,
            "network_stable": False,
            "forged_network_stable": False,
            "capability_gaps": list(
                dict.fromkeys(
                    [
                        *(network_record.get("capability_gaps") or []),
                        "network_stable_claim_rejected",
                    ]
                )
            ),
        }

    evidence_role = recommend_app_evidence_role(
        presentation_layer=presentation_layer,
        ready_for_business_evidence=ready_for_business_evidence,
    )
    formal = milestone_authority_enabled(policy, "M1")
    shadow = milestone_shadow_enabled(policy, "M1")
    capability_gaps = list(
        dict.fromkeys(
            [
                *(stability.get("capability_gaps") or []),
                *(network_record.get("capability_gaps") or []),
            ]
        )
    )
    reasons = list(dict.fromkeys(reasons))
    settle_shadow = {
        "protocol_version": APP_SETTLE_SHADOW_VERSION,
        "active": shadow,
        "profile": resolved_profile.value if resolved_profile is not None else None,
        "presentation_layer": presentation_layer,
        "entry_layer": entry_layer,
        "identity_sandwich": identity_sandwich,
        "evidence_role": evidence_role,
        "ready_for_business_evidence": ready_for_business_evidence,
        "reason_codes": list(reasons),
        "network_observable": effective_network,
        "network_stable": False,
        "capability_gaps": capability_gaps,
        "page_stability_status": stability.get("stability_status"),
        "gate_shadow_only": not formal,
        "formal_settle_oracle": bool(formal),
        "gate_consumes_settle": False,
    }
    diagnosis = {
        "protocol_version": APP_CAPTURE_READINESS_DIAGNOSIS_VERSION,
        "profile": resolved_profile.value if resolved_profile is not None else None,
        "page_state": state,
        "ready_for_business_evidence": ready_for_business_evidence,
        "reason_codes": reasons,
        "network_observable": effective_network,
        "network_stable": False,
        "capability_gaps": capability_gaps,
        "page_stability": stability,
        "presentation_layer": presentation_layer,
        "entry_layer": entry_layer,
        "evidence_role": evidence_role,
        "identity_sandwich": identity_sandwich,
        "settle_shadow": settle_shadow,
        "formal_settle_oracle": bool(formal),
        "milestone_policy_version": str(policy.get("protocol_version") or ""),
    }
    diagnosis["settle_enforcement_preview"] = build_m1_settle_enforcement_preview(
        diagnosis
    )
    return diagnosis


def build_m1_settle_enforcement_preview(
    diagnosis: Mapping[str, Any] | None,
) -> dict[str, object]:
    """M1 正式 Oracle 预览：可审计 would_block；Gate SQL 永不由此打开。"""

    raw = diagnosis if isinstance(diagnosis, Mapping) else {}
    ready = bool(raw.get("ready_for_business_evidence"))
    formal = bool(raw.get("formal_settle_oracle"))
    reasons: list[str] = []
    for item in raw.get("reason_codes") or ():
        if isinstance(item, str) and item.strip():
            reasons.append(item.strip())
    for item in raw.get("capability_gaps") or ():
        if isinstance(item, str) and item.strip():
            code = f"capability_gap:{item.strip()}"
            if code not in reasons:
                reasons.append(code)
    stability = raw.get("page_stability") if isinstance(raw.get("page_stability"), Mapping) else {}
    status = stability.get("stability_status")
    if isinstance(status, str) and status.strip() and status != "stable_candidate":
        extra = f"page_stability:{status.strip()}"
        if extra not in reasons:
            reasons.append(extra)
    would_block = not ready
    return {
        "protocol_version": APP_SETTLE_ENFORCEMENT_PREVIEW_VERSION,
        "formal_settle_oracle": formal,
        "active": True,
        "would_block_business_evidence": would_block,
        "would_block_if_enforced": would_block and formal,
        "would_block_reasons": reasons if would_block else [],
        "gate_consumes_settle": False,
        "mutates_completion_gate_sql": False,
    }


def build_settle_shadow_manifest_audit(
    diagnosis: Mapping[str, Any] | None,
) -> dict[str, object]:
    """供 Manifest／审计可读栏位消费的影子摘要；Gate 不得据此放行。"""

    raw = diagnosis if isinstance(diagnosis, Mapping) else {}
    settle = raw.get("settle_shadow") if isinstance(raw.get("settle_shadow"), Mapping) else {}
    entry = (
        settle.get("entry_layer")
        if isinstance(settle.get("entry_layer"), Mapping)
        else raw.get("entry_layer")
        if isinstance(raw.get("entry_layer"), Mapping)
        else {}
    )
    return {
        "protocol_version": APP_SETTLE_SHADOW_VERSION,
        "formal_settle_oracle": False,
        "gate_consumes_settle": False,
        "profile": settle.get("profile") or raw.get("profile"),
        "presentation_layer": settle.get("presentation_layer") or raw.get("presentation_layer"),
        "entry_layer": dict(entry) if entry else None,
        "evidence_role": settle.get("evidence_role") or raw.get("evidence_role"),
        "ready_for_business_evidence": bool(
            settle.get("ready_for_business_evidence")
            if "ready_for_business_evidence" in settle
            else raw.get("ready_for_business_evidence")
        ),
        "identity_sandwich": settle.get("identity_sandwich") or raw.get("identity_sandwich"),
        "network_observable": settle.get("network_observable", raw.get("network_observable")),
        "network_stable": False,
        "capability_gaps": list(
            settle.get("capability_gaps")
            if isinstance(settle.get("capability_gaps"), list)
            else raw.get("capability_gaps") or []
        ),
        "page_stability_status": settle.get("page_stability_status")
        or (
            raw.get("page_stability", {}).get("stability_status")
            if isinstance(raw.get("page_stability"), Mapping)
            else None
        ),
        "reason_codes": list(
            settle.get("reason_codes")
            if isinstance(settle.get("reason_codes"), list)
            else raw.get("reason_codes") or []
        ),
        "app_ready_equals_business_home": False,
        "underlying_home_fabricated": False,
    }


def build_m1_settle_audit_bundle(
    *,
    action_after: Mapping[str, Any] | None,
    reobserve: Mapping[str, Any] | None,
    entry_layer: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    """M1 加深：双剖面 + 入口层合并为可审计捆；正式 Oracle／Gate 不消费。"""

    after_audit = build_settle_shadow_manifest_audit(action_after)
    reobserve_audit = build_settle_shadow_manifest_audit(reobserve)
    entry = (
        dict(entry_layer)
        if isinstance(entry_layer, Mapping)
        else after_audit.get("entry_layer") or reobserve_audit.get("entry_layer")
    )
    return {
        "protocol_version": "app-m1-settle-audit-bundle.v1",
        "formal_settle_oracle": False,
        "gate_consumes_settle": False,
        "app_ready_equals_business_home": False,
        "underlying_home_fabricated": False,
        "entry_layer": entry,
        "action_after": after_audit,
        "reobserve": reobserve_audit,
    }
