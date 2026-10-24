"""展示层评估：值对象合同、两端推导与规则层级门禁。"""

from __future__ import annotations

import pytest

from services.api.app.application.evidence_presentation import (
    CAPTURE_DECLARED_SOURCE,
    DECLARED_OVERRIDES_DERIVED,
    apply_declared_evidence_flags,
    declared_evidence_flags,
    derive_app_presentation,
    derive_h5_presentation,
    diagnostic_operations_from_action_log,
    inherited_operations_from_presentation,
    presentation_from_capture_metadata,
    presentation_from_capture_row,
    rule_accepts_diagnostic_evidence,
    rule_evidence_layer_target,
)
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_METADATA_KEY,
    PRESENTATION_PROTOCOL_VERSION,
    BasePageVisibility,
    BlockingScope,
    DiagnosticOperation,
    EvidenceLayerTarget,
    MutationMode,
    PresentationAssessment,
    PresentationRejectionCode,
    TopmostLayerKind,
    evaluate_presentation_for_rule,
    unknown_presentation,
)


def _element(**overrides):
    base = {
        "selector": "div.content",
        "role": None,
        "class_tokens": ["content"],
        "rect": {"x": 0, "y": 0, "width": 390, "height": 300},
        "style": {"display": "block", "visibility": "visible", "opacity": "1"},
        "hit_test": {"center_hits_target": True, "pointer_interception": False},
    }
    base.update(overrides)
    return base


def _snapshot(*elements):
    return {"viewport": {"width": 390, "height": 844}, "elements": list(elements)}


def _fullscreen_dialog():
    return _element(
        selector="div.van-popup",
        role="dialog",
        class_tokens=["van-popup", "van-overlay"],
        rect={"x": 0, "y": 0, "width": 390, "height": 844},
    )


def _small_modal():
    return _element(
        selector="div.small-modal",
        role="dialog",
        class_tokens=["modal"],
        rect={"x": 60, "y": 300, "width": 270, "height": 200},
        hit_test={"center_hits_target": True, "pointer_interception": True},
    )


def _toast():
    return _element(
        selector="div.van-toast",
        role="status",
        class_tokens=["van-toast"],
        rect={"x": 100, "y": 700, "width": 190, "height": 40},
        style={"display": "block", "pointer_events": "none"},
        hit_test={"center_hits_target": False},
    )


# ---- 值对象 ----------------------------------------------------------------------


def test_assessment_round_trips_through_dict() -> None:
    original = PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.OVERLAY,
        base_page_visibility=BasePageVisibility.FULLY_OCCLUDED,
        blocking_scope=BlockingScope.PAGE,
        mutation_mode=MutationMode.INSTRUMENTED_DIAGNOSTIC,
        overlay_signature="sig-1",
        overlay_semantic_kind="advertisement",
        overlay_coverage=0.93,
        hit_test_status="available",
        evidence_sources=("h5_ui_snapshot",),
        diagnostic_operations=(
            DiagnosticOperation(
                kind="style_hide_obstruction", source="verified_high_z_obstruction_hide"
            ),
            DiagnosticOperation(
                kind="js_click_dispatch", source="x", origin_capture_id="cap-0"
            ),
        ),
        uncertainty=("a",),
        document_epoch_ms=1788845247999.7,
    )
    payload = original.to_dict()
    assert payload["protocol_version"] == PRESENTATION_PROTOCOL_VERSION
    assert payload["document_epoch_ms"] == 1788845247999.7
    assert PresentationAssessment.from_dict(payload) == original
    # 历史评估没有这个字段、或字段非法时不影响读取。
    legacy = {key: value for key, value in payload.items() if key != "document_epoch_ms"}
    assert PresentationAssessment.from_dict(legacy).document_epoch_ms is None
    assert PresentationAssessment.from_dict({**payload, "document_epoch_ms": "x"}).document_epoch_ms is None
    assert PresentationAssessment.from_dict({**payload, "document_epoch_ms": True}).document_epoch_ms is None


def test_from_dict_rejects_foreign_protocol_and_tolerates_unknown_values() -> None:
    assert PresentationAssessment.from_dict({"protocol_version": "other"}) is None
    assert PresentationAssessment.from_dict("nope") is None
    loose = PresentationAssessment.from_dict(
        {
            "protocol_version": PRESENTATION_PROTOCOL_VERSION,
            "topmost_layer_kind": "martian",
            "overlay_coverage": "1.7",
            "mutation_mode": "instrumented_diagnostic",
        }
    )
    assert loose is not None
    assert loose.topmost_layer_kind is TopmostLayerKind.UNKNOWN
    assert loose.base_page_visibility is BasePageVisibility.UNKNOWN
    assert loose.overlay_coverage == 1.0
    # 诊断模式但没有操作记录：补一条 unrecorded，不能悄悄退回自然模式
    assert loose.is_diagnostic
    assert loose.diagnostic_operations[0].source == "unrecorded"


def test_assessment_invariants() -> None:
    with pytest.raises(ValueError):
        PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.PAGE,
            base_page_visibility=BasePageVisibility.VISIBLE,
            blocking_scope=BlockingScope.NONE,
            mutation_mode=MutationMode.INSTRUMENTED_DIAGNOSTIC,
        )
    with pytest.raises(ValueError):
        PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.PAGE,
            base_page_visibility=BasePageVisibility.VISIBLE,
            blocking_scope=BlockingScope.NONE,
            diagnostic_operations=(DiagnosticOperation(kind="x", source="y"),),
        )
    with pytest.raises(ValueError):
        PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.PAGE,
            base_page_visibility=BasePageVisibility.VISIBLE,
            blocking_scope=BlockingScope.NONE,
            overlay_coverage=1.2,
        )


def test_with_diagnostic_operations_switches_mode_and_dedupes() -> None:
    natural = unknown_presentation()
    op = DiagnosticOperation(kind="js_click_dispatch", source="s")
    marked = natural.with_diagnostic_operations((op, op))
    assert marked.is_diagnostic
    assert marked.diagnostic_operations == (op,)
    assert natural.with_diagnostic_operations(()) is natural


# ---- 规则层级门禁 ------------------------------------------------------------------


def _occluded(**overrides) -> PresentationAssessment:
    payload = dict(
        topmost_layer_kind=TopmostLayerKind.OVERLAY,
        base_page_visibility=BasePageVisibility.FULLY_OCCLUDED,
        blocking_scope=BlockingScope.PAGE,
        overlay_signature="ad-1",
    )
    payload.update(overrides)
    return PresentationAssessment(**payload)


def test_missing_presentation_never_rejects() -> None:
    verdict = evaluate_presentation_for_rule(None, layer_target=EvidenceLayerTarget.BASE_PAGE)
    assert verdict.accepted and verdict.reason_code is None


def test_base_page_rule_rejects_fully_occluded_frame_but_overlay_rule_accepts_it() -> None:
    occluded = _occluded()
    rejected = evaluate_presentation_for_rule(
        occluded, layer_target=EvidenceLayerTarget.BASE_PAGE
    )
    assert not rejected.accepted
    assert rejected.reason_code is PresentationRejectionCode.BASE_PAGE_OCCLUDED
    assert "ad-1" in (rejected.reason or "")
    assert rejected.facts["topmost_layer_kind"] == "overlay"
    for target in (EvidenceLayerTarget.ANY, EvidenceLayerTarget.OVERLAY):
        assert evaluate_presentation_for_rule(occluded, layer_target=target).accepted


def test_partial_occlusion_and_unknown_page_do_not_reject_base_page_rules() -> None:
    partial = _occluded(
        base_page_visibility=BasePageVisibility.PARTIALLY_OCCLUDED,
        blocking_scope=BlockingScope.REGION,
    )
    assert evaluate_presentation_for_rule(
        partial, layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted
    # 最上层是页面本身、可见度未知（没有结构快照）：没有正向遮挡依据，不拒绝
    assert evaluate_presentation_for_rule(
        unknown_presentation(), layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted
    unknown_visibility_under_overlay = _occluded(
        base_page_visibility=BasePageVisibility.UNKNOWN,
        blocking_scope=BlockingScope.UNKNOWN,
    )
    assert not evaluate_presentation_for_rule(
        unknown_visibility_under_overlay, layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted


def test_diagnostic_material_is_rejected_for_every_target_unless_declared() -> None:
    diagnostic = unknown_presentation().with_diagnostic_operations(
        (
            DiagnosticOperation(
                kind="style_hide_obstruction", source="verified_high_z_obstruction_hide"
            ),
        )
    )
    for target in EvidenceLayerTarget:
        verdict = evaluate_presentation_for_rule(diagnostic, layer_target=target)
        assert not verdict.accepted
        assert verdict.reason_code is PresentationRejectionCode.DIAGNOSTIC_EVIDENCE_EXCLUDED
        assert "用样式隐藏弹窗" in (verdict.reason or "")
    assert evaluate_presentation_for_rule(
        diagnostic, layer_target=EvidenceLayerTarget.BASE_PAGE, accepts_diagnostic=True
    ).accepted


def test_external_surface_is_not_business_page_evidence() -> None:
    external = _occluded(
        topmost_layer_kind=TopmostLayerKind.EXTERNAL,
        base_page_visibility=BasePageVisibility.UNKNOWN,
        blocking_scope=BlockingScope.UNKNOWN,
        overlay_signature=None,
    )
    verdict = evaluate_presentation_for_rule(external, layer_target=EvidenceLayerTarget.BASE_PAGE)
    assert verdict.reason_code is PresentationRejectionCode.EXTERNAL_SURFACE


# ---- H5 推导 ---------------------------------------------------------------------


def test_h5_fullscreen_dialog_means_base_page_fully_occluded() -> None:
    presentation = derive_h5_presentation(
        ui_snapshot=_snapshot(_element(), _fullscreen_dialog()),
        page_state="popup",
    )
    assert presentation.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert presentation.base_page_visibility is BasePageVisibility.FULLY_OCCLUDED
    assert presentation.blocking_scope is BlockingScope.PAGE
    assert presentation.overlay_coverage == 1.0
    assert presentation.hit_test_status == "available"
    assert presentation.evidence_sources == ("page_state", "h5_ui_snapshot")
    assert not presentation.is_diagnostic


def test_h5_business_player_overlay_leaves_base_page_visible() -> None:
    presentation = derive_h5_presentation(
        ui_snapshot=_snapshot(
            _element(
                selector="div.drama-player-card > div.overlay",
                class_tokens=["overlay"],
                ancestor_class_tokens=["drama-player-card", "swiper-slide-active"],
                rect={"x": 0, "y": 0, "width": 390, "height": 787},
                style={
                    "display": "block",
                    "visibility": "visible",
                    "opacity": "1",
                    "pointer_events": "none",
                },
                hit_test={
                    "center_hits_target": False,
                    "pointer_interception": False,
                    "topmost": False,
                },
            ),
            _element(
                selector="p.title",
                class_tokens=["title"],
                ancestor_class_tokens=["overlay", "drama-player-card"],
                rect={"x": 16, "y": 630, "width": 220, "height": 24},
                hit_test={
                    "center_hits_target": True,
                    "pointer_interception": True,
                    "topmost": True,
                },
            ),
        ),
        page_state="stable",
    )

    assert presentation.topmost_layer_kind is TopmostLayerKind.PAGE
    assert presentation.base_page_visibility is BasePageVisibility.VISIBLE
    assert presentation.blocking_scope is BlockingScope.NONE


def test_h5_small_modal_that_intercepts_pointer_is_partial_occlusion() -> None:
    presentation = derive_h5_presentation(
        ui_snapshot=_snapshot(_element(), _small_modal()),
        page_state="stable",
    )
    assert presentation.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert presentation.base_page_visibility is BasePageVisibility.PARTIALLY_OCCLUDED
    assert presentation.blocking_scope is BlockingScope.REGION
    assert presentation.overlay_coverage == pytest.approx(270 * 200 / (390 * 844))


def test_h5_toast_leaves_page_visible() -> None:
    presentation = derive_h5_presentation(ui_snapshot=_snapshot(_element(), _toast()))
    assert presentation.topmost_layer_kind is TopmostLayerKind.PAGE
    assert presentation.base_page_visibility is BasePageVisibility.VISIBLE
    assert presentation.blocking_scope is BlockingScope.REGION
    assert presentation.overlay_semantic_kind == "notification"


def test_h5_clean_page_is_visible_and_dom_structure_outranks_popup_page_state() -> None:
    clean = derive_h5_presentation(ui_snapshot=_snapshot(_element()), page_state="stable")
    assert clean.topmost_layer_kind is TopmostLayerKind.PAGE
    assert clean.base_page_visibility is BasePageVisibility.VISIBLE
    # 普通 DOM 页面：结构正面扫过没有阻断元素，识别却说 popup（实测多为底部悬浮推广条）
    # —— 以结构为准，底页可见，只把分歧记进不确定项
    disputed = derive_h5_presentation(ui_snapshot=_snapshot(_element()), page_state="popup")
    assert disputed.topmost_layer_kind is TopmostLayerKind.PAGE
    assert disputed.base_page_visibility is BasePageVisibility.VISIBLE
    assert disputed.blocking_scope is BlockingScope.REGION
    assert "page_state_reports_overlay" in disputed.uncertainty
    assert evaluate_presentation_for_rule(
        disputed, layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted


def test_h5_flutter_canvas_defers_to_popup_page_state() -> None:
    # Flutter 画布：语义树被平台激活过，结构对遮挡没有权威 —— 识别说 popup 就保守认定有遮挡
    snapshot = {**_snapshot(_element()), "flutter_semantics_activation": "enabled"}
    canvas = derive_h5_presentation(ui_snapshot=snapshot, page_state="popup")
    assert canvas.hit_test_status == "degraded"
    assert canvas.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert canvas.base_page_visibility is BasePageVisibility.UNKNOWN
    assert "structure_snapshot_disagrees_with_page_state" in canvas.uncertainty
    assert not evaluate_presentation_for_rule(
        canvas, layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted
    # 画布应用真的有弹窗结构时仍按结构判整页遮挡
    blocked = derive_h5_presentation(
        ui_snapshot={
            **_snapshot(_element(), _fullscreen_dialog()),
            "flutter_semantics_activation": "enabled",
        },
        page_state="popup",
    )
    assert blocked.base_page_visibility is BasePageVisibility.FULLY_OCCLUDED


def test_h5_without_snapshot_is_unknown_not_occluded() -> None:
    presentation = derive_h5_presentation(ui_snapshot=None, page_state=None)
    assert presentation.topmost_layer_kind is TopmostLayerKind.UNKNOWN
    assert "ui_snapshot_unavailable" in presentation.uncertainty
    assert presentation.hit_test_status == "unavailable"
    stable = derive_h5_presentation(ui_snapshot=None, page_state="stable")
    assert stable.topmost_layer_kind is TopmostLayerKind.PAGE
    assert stable.base_page_visibility is BasePageVisibility.UNKNOWN
    assert evaluate_presentation_for_rule(
        stable, layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted


def test_h5_system_permission_state_is_system_dialog() -> None:
    presentation = derive_h5_presentation(
        ui_snapshot=_snapshot(_element()), page_state="browser_permission"
    )
    assert presentation.topmost_layer_kind is TopmostLayerKind.SYSTEM_DIALOG
    assert presentation.overlay_semantic_kind == "permission_request"


def test_h5_action_log_diagnostic_markers_become_operations() -> None:
    action_log = [
        {"action": "click", "resolved_by": "visible_popup_structure"},
        {"action": "click", "fallback": "verified_high_z_obstruction_hide"},
        {
            "action": "click",
            "resolved_by": "popup_close_direct_dispatch_after_actionability_timeout",
        },
        {"action": "click", "fallback_dispatch_attempts": [{"target": "a", "status": "ok"}]},
        "not-a-mapping",
    ]
    operations = diagnostic_operations_from_action_log(action_log)
    assert [item.kind for item in operations] == [
        "style_hide_obstruction",
        "js_click_dispatch",
        "js_click_dispatch",
    ]
    assert diagnostic_operations_from_action_log(None) == ()
    natural = diagnostic_operations_from_action_log(
        [{"action": "scroll", "result": "primary_content_scrolled_into_view"}]
    )
    assert natural == ()


def test_h5_presentation_records_document_epoch_from_snapshot() -> None:
    """文档起始时间来自 UI Snapshot 的 document.time_origin_ms，随评估落库供继承查询划界。"""
    with_document = {**_snapshot(_element()), "document": {"time_origin_ms": 1788845247999.7}}
    presentation = derive_h5_presentation(
        ui_snapshot=with_document,
        page_state="stable",
        action_log=[{"fallback": "verified_high_z_obstruction_hide"}],
    )
    assert presentation.document_epoch_ms == 1788845247999.7
    assert presentation.is_diagnostic
    assert presentation.to_dict()["document_epoch_ms"] == 1788845247999.7

    assert derive_h5_presentation(ui_snapshot=_snapshot(_element())).document_epoch_ms is None
    assert derive_h5_presentation(ui_snapshot=None).document_epoch_ms is None
    broken = {**_snapshot(_element()), "document": {"time_origin_ms": "soon"}}
    assert derive_h5_presentation(ui_snapshot=broken).document_epoch_ms is None


def test_h5_presentation_inherits_session_diagnostic_lineage() -> None:
    first = derive_h5_presentation(
        ui_snapshot=_snapshot(_element()),
        page_state="stable",
        action_log=[{"fallback": "verified_high_z_obstruction_hide"}],
    )
    assert first.is_diagnostic
    assert first.diagnostic_operations[0].origin_capture_id is None
    assert "h5_action_log" in first.evidence_sources
    inherited = inherited_operations_from_presentation(first, origin_capture_id="cap-1")
    assert inherited[0].origin_capture_id == "cap-1"
    second = derive_h5_presentation(
        ui_snapshot=_snapshot(_element()),
        page_state="stable",
        action_log=[],
        inherited_operations=inherited,
    )
    assert second.is_diagnostic
    assert second.diagnostic_operations == inherited
    assert "surface_session_lineage" in second.evidence_sources
    assert inherited_operations_from_presentation(None, origin_capture_id="x") == ()
    assert inherited_operations_from_presentation(second, origin_capture_id="cap-2") == inherited


# ---- App 推导 --------------------------------------------------------------------


def test_app_stable_state_without_overlay_is_visible_page() -> None:
    presentation = derive_app_presentation(
        page_state="stable", overlay_signature=None, surface_facts={"popup_visible": False}
    )
    assert presentation.topmost_layer_kind is TopmostLayerKind.PAGE
    assert presentation.base_page_visibility is BasePageVisibility.VISIBLE
    assert presentation.evidence_sources == ("app_visual_observation",)


def test_app_popup_state_or_signature_or_fact_means_overlay() -> None:
    by_state = derive_app_presentation(page_state="popup", overlay_signature=None, surface_facts={})
    by_signature = derive_app_presentation(
        page_state="stable", overlay_signature="sig-9", surface_facts={}
    )
    by_fact = derive_app_presentation(
        page_state="stable", overlay_signature=None, surface_facts={"auth_gate_visible": True}
    )
    for item in (by_state, by_signature, by_fact):
        assert item.topmost_layer_kind is TopmostLayerKind.OVERLAY
        assert item.base_page_visibility is BasePageVisibility.FULLY_OCCLUDED
    assert by_signature.overlay_signature == "sig-9"
    assert by_fact.overlay_semantic_kind == "authentication"


def test_app_topmost_ad_popup_bounds_decide_partial_or_full_occlusion() -> None:
    small = derive_app_presentation(
        page_state="stable",
        overlay_signature=None,
        surface_facts={
            "topmost_ad_popup": {
                "is_ad": True,
                "confidence": 0.9,
                "normalized_bounds": {"left": 0.1, "top": 0.3, "right": 0.9, "bottom": 0.7},
            }
        },
    )
    assert small.overlay_semantic_kind == "advertisement"
    assert small.overlay_coverage == pytest.approx(0.32)
    assert small.base_page_visibility is BasePageVisibility.PARTIALLY_OCCLUDED
    full = derive_app_presentation(
        page_state="startup_ad",
        overlay_signature=None,
        surface_facts={
            "topmost_ad_popup": {
                "is_ad": True,
                "normalized_bounds": {"left": 0, "top": 0, "right": 1, "bottom": 1},
            }
        },
    )
    assert full.base_page_visibility is BasePageVisibility.FULLY_OCCLUDED
    assert full.blocking_scope is BlockingScope.PAGE


def test_app_system_dialog_and_unknown_states() -> None:
    system = derive_app_presentation(
        page_state="permission_dialog", overlay_signature=None, surface_facts={}
    )
    assert system.topmost_layer_kind is TopmostLayerKind.SYSTEM_DIALOG
    assert system.blocking_scope is BlockingScope.SYSTEM
    loading = derive_app_presentation(
        page_state="loading", overlay_signature=None, surface_facts=None
    )
    assert loading.topmost_layer_kind is TopmostLayerKind.UNKNOWN
    assert "page_state_loading" in loading.uncertainty


# ---- 元数据与规则层级 ---------------------------------------------------------------


def test_presentation_from_capture_metadata() -> None:
    presentation = derive_h5_presentation(ui_snapshot=_snapshot(_element()), page_state="stable")
    metadata = {PRESENTATION_METADATA_KEY: presentation.to_dict(), "url": "x"}
    assert presentation_from_capture_metadata(metadata) == presentation
    assert presentation_from_capture_metadata({}) is None
    assert presentation_from_capture_metadata(None) is None


def test_rule_layer_target_resolution_order() -> None:
    assert (
        rule_evidence_layer_target(
            category="text", judge_config={"evidence_layer_target": "overlay"}
        )
        is EvidenceLayerTarget.OVERLAY
    )
    assert (
        rule_evidence_layer_target(category="text", judge_config={"evidence_layer_target": "bogus"})
        is EvidenceLayerTarget.BASE_PAGE
    )
    assert rule_evidence_layer_target(category="ad") is EvidenceLayerTarget.ANY
    assert (
        rule_evidence_layer_target(
            category="interaction",
            check_point="我的页面/身份凭证卡模块/弹窗模块中展示的二维码和网址应真实有效",
        )
        is EvidenceLayerTarget.ANY
    )
    assert (
        rule_evidence_layer_target(category="visual", check_point="图片应当高清、不模糊")
        is EvidenceLayerTarget.BASE_PAGE
    )
    assert rule_accepts_diagnostic_evidence({"accepts_diagnostic_evidence": True})
    assert not rule_accepts_diagnostic_evidence({"accepts_diagnostic_evidence": "yes"})
    assert not rule_accepts_diagnostic_evidence(None)


def test_presentation_from_capture_row_is_the_single_read_side_entry() -> None:
    stored = derive_h5_presentation(ui_snapshot=_snapshot(_element()), page_state="stable")
    h5 = presentation_from_capture_row(
        surface_kind="web",
        metadata={PRESENTATION_METADATA_KEY: stored.to_dict()},
        page_state="popup",
    )
    assert h5 is not None
    # DOM 结构确认可见，识别报 popup 只记分歧
    assert h5.topmost_layer_kind is TopmostLayerKind.PAGE
    assert "page_state_reports_overlay" in h5.uncertainty

    app = presentation_from_capture_row(
        surface_kind="android",
        metadata={},
        page_state=None,
        visual_page_state="popup",
        visual_overlay_signature="sig",
        visual_surface_facts={},
    )
    assert app is not None and app.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert (
        presentation_from_capture_row(surface_kind="android", metadata={}, page_state=None)
        is None
    )
    assert (
        presentation_from_capture_row(surface_kind="web", metadata={}, page_state="popup")
        is None
    )


# ---- 证据清单显式字段 ---------------------------------------------------------------


def test_declared_evidence_flags_only_accepts_well_formed_values() -> None:
    assert declared_evidence_flags(None) == {}
    assert declared_evidence_flags({"is_obstructed": "yes", "session_auth": "vip"}) == {}
    # diagnostic=false 视同没声明：平台记录在案的诊断操作不因一句"没有"被抹掉
    assert declared_evidence_flags({"diagnostic": False}) == {}
    assert declared_evidence_flags(
        {
            "is_obstructed": False,
            "diagnostic": ["style_hide_obstruction", "", "style_hide_obstruction"],
            "session_auth": "Guest",
            "url": "x",
        }
    ) == {
        "is_obstructed": False,
        "diagnostic": ["style_hide_obstruction"],
        "session_auth": "guest",
    }
    assert declared_evidence_flags({"diagnostic": True}) == {"diagnostic": ["declared"]}
    assert declared_evidence_flags({"diagnostic": "js_click_dispatch"}) == {
        "diagnostic": ["js_click_dispatch"]
    }


def test_declared_obstruction_overrides_derived_guess_in_both_directions() -> None:
    # Flutter 画布上识别猜出来的 popup（底页可见程度未知）→ 采集侧声明没遮挡，以声明为准
    guessed = derive_h5_presentation(ui_snapshot=None, page_state="popup")
    assert evaluate_presentation_for_rule(
        guessed, layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted is False
    cleared = apply_declared_evidence_flags(guessed, {"is_obstructed": False})
    assert cleared is not None
    assert cleared.topmost_layer_kind is TopmostLayerKind.PAGE
    assert cleared.base_page_visibility is BasePageVisibility.VISIBLE
    assert cleared.blocking_scope is BlockingScope.NONE
    assert CAPTURE_DECLARED_SOURCE in cleared.evidence_sources
    assert DECLARED_OVERRIDES_DERIVED in cleared.uncertainty
    assert evaluate_presentation_for_rule(
        cleared, layer_target=EvidenceLayerTarget.BASE_PAGE
    ).accepted is True

    # 结构上看不出遮挡 → 采集侧声明被遮挡，以声明为准
    clean = derive_h5_presentation(ui_snapshot=_snapshot(_element()), page_state="stable")
    blocked = apply_declared_evidence_flags(clean, {"is_obstructed": True})
    assert blocked is not None
    assert blocked.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert blocked.base_page_visibility is BasePageVisibility.FULLY_OCCLUDED
    assert DECLARED_OVERRIDES_DERIVED in blocked.uncertainty
    verdict = evaluate_presentation_for_rule(blocked, layer_target=EvidenceLayerTarget.BASE_PAGE)
    assert verdict.reason_code is PresentationRejectionCode.BASE_PAGE_OCCLUDED

    # 声明和推导一致时不记分歧；系统对话框这类更具体的层级保留
    system = derive_h5_presentation(ui_snapshot=None, page_state="permission_dialog")
    agreed = apply_declared_evidence_flags(system, {"is_obstructed": True})
    assert agreed is not None
    assert agreed.topmost_layer_kind is TopmostLayerKind.SYSTEM_DIALOG
    assert DECLARED_OVERRIDES_DERIVED not in agreed.uncertainty


def test_declared_obstruction_preserves_measured_partial_overlay_geometry() -> None:
    partial = derive_h5_presentation(
        ui_snapshot=_snapshot(_small_modal()),
        page_state="popup",
    )
    assert partial.base_page_visibility is BasePageVisibility.PARTIALLY_OCCLUDED
    assert partial.blocking_scope is BlockingScope.REGION

    declared = apply_declared_evidence_flags(partial, {"is_obstructed": True})

    assert declared is not None
    assert declared.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert declared.base_page_visibility is BasePageVisibility.PARTIALLY_OCCLUDED
    assert declared.blocking_scope is BlockingScope.REGION
    assert declared.overlay_coverage == partial.overlay_coverage
    assert DECLARED_OVERRIDES_DERIVED not in declared.uncertainty


def test_declared_diagnostic_adds_to_recorded_operations_and_never_removes() -> None:
    clean = derive_h5_presentation(ui_snapshot=_snapshot(_element()), page_state="stable")
    declared = apply_declared_evidence_flags(clean, {"diagnostic": ["style_hide_obstruction"]})
    assert declared is not None and declared.is_diagnostic
    assert declared.diagnostic_operations == (
        DiagnosticOperation(kind="style_hide_obstruction", source=CAPTURE_DECLARED_SOURCE),
    )
    verdict = evaluate_presentation_for_rule(declared, layer_target=EvidenceLayerTarget.ANY)
    assert verdict.reason_code is PresentationRejectionCode.DIAGNOSTIC_EVIDENCE_EXCLUDED

    recorded = derive_h5_presentation(
        ui_snapshot=_snapshot(_element()),
        page_state="stable",
        action_log=[{"fallback": "verified_high_z_obstruction_hide"}],
    )
    assert apply_declared_evidence_flags(recorded, {"is_obstructed": False}).is_diagnostic

    # 没有任何声明：原样返回（含 None）
    assert apply_declared_evidence_flags(clean, {}) is clean
    assert apply_declared_evidence_flags(None, {"session_auth": "guest"}) is None


def test_capture_row_reads_declared_fields_for_every_surface() -> None:
    stored = derive_h5_presentation(ui_snapshot=None, page_state="popup")
    h5 = presentation_from_capture_row(
        surface_kind="web",
        metadata={PRESENTATION_METADATA_KEY: stored.to_dict(), "is_obstructed": False},
        page_state="popup",
    )
    assert h5 is not None and h5.base_page_visibility is BasePageVisibility.VISIBLE

    # App 行没有视觉观察也没有持久化评估时，有声明就按声明出评估
    app = presentation_from_capture_row(
        surface_kind="android",
        metadata={"is_obstructed": True, "diagnostic": True},
        page_state=None,
    )
    assert app is not None
    assert app.topmost_layer_kind is TopmostLayerKind.OVERLAY
    assert app.base_page_visibility is BasePageVisibility.FULLY_OCCLUDED
    assert app.is_diagnostic
    assert "presentation_unavailable" not in app.uncertainty

    app_visual = presentation_from_capture_row(
        surface_kind="android",
        metadata={"is_obstructed": False},
        page_state=None,
        visual_page_state="popup",
        visual_overlay_signature="sig",
        visual_surface_facts={},
    )
    assert app_visual is not None and app_visual.topmost_layer_kind is TopmostLayerKind.PAGE
