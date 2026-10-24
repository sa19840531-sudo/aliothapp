"""从 H5 / App 已有采集事实推导展示层评估，并为规则消费提供层级门禁输入。

写入口（H5 采集持久化、App 视觉观察落库）与消费口（判定包、聚合封存）都只调用本模块，
保证同一份材料在任何链路上得到同一份评估。本模块不操作浏览器与设备，不改动采集协议。
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from services.api.app.application.h5_obstruction_facts import assess_h5_ui_obstruction
from services.api.app.domain.evidence.presentation import (
    PRESENTATION_METADATA_KEY,
    BasePageVisibility,
    BlockingScope,
    DiagnosticOperation,
    EvidenceLayerTarget,
    MutationMode,
    PresentationAssessment,
    TopmostLayerKind,
    evaluate_presentation_for_rule,
    unknown_presentation,
)

# ---- H5 action_log 中已有的诊断操作痕迹 -------------------------------------------
# 采集端只在 action_trace 条目里留了这些字样（fallback / resolved_by / dispatch 尝试），
# 这里把它们归一化为诊断操作，不再让每个消费者各自猜字符串。
_STYLE_HIDE_FALLBACKS = frozenset({"verified_high_z_obstruction_hide"})
_JS_DISPATCH_RESOLUTIONS = frozenset(
    {
        "overlay_direct_dispatch_after_pointer_interception",
        "popup_close_direct_dispatch_after_actionability_timeout",
        "startup_ad_direct_dispatch_after_pointer_interception",
        "flutter_bottom_navigation_verified_dispatch",
    }
)

# ---- 页面状态词表（H5 识别与 App 视觉共用） -----------------------------------------
_SYSTEM_DIALOG_STATES = frozenset({"system_dialog", "permission_dialog", "browser_permission"})
_OVERLAY_STATES = frozenset(
    {
        "popup",
        "modal",
        "overlay",
        "dialog",
        "obstructed",
        "startup_ad",
        "ad",
        "advertisement",
        "login_prompt",
        "purchase_prompt",
        "update_prompt",
        "system_notice",
        "countdown",
        "auth_gate",
        "login_required",
        "requires_auth",
        "blocked",
    }
)
_STABLE_STATES = frozenset({"stable", "normal", "loaded", "content_available", "ready"})
_SEMANTIC_KIND_BY_STATE = {
    "startup_ad": "advertisement",
    "ad": "advertisement",
    "advertisement": "advertisement",
    "countdown": "advertisement",
    "login_prompt": "authentication",
    "auth_gate": "authentication",
    "login_required": "authentication",
    "requires_auth": "authentication",
    "purchase_prompt": "recharge_offer",
    "update_prompt": "version_update",
    "system_notice": "announcement",
    "system_dialog": "system_dialog",
    "permission_dialog": "permission_request",
    "browser_permission": "permission_request",
}
# App 视觉 surface_facts 里表示"最上层有遮挡"的布尔键及其语义。
_APP_OVERLAY_FACT_KINDS = {
    "popup_visible": "unknown",
    "dialog_visible": "unknown",
    "auth_gate_visible": "authentication",
    "login_form_visible": "authentication",
    "startup_ad_visible": "advertisement",
    "countdown_visible": "advertisement",
}
_APP_SYSTEM_FACT_KEYS = ("system_dialog_visible", "permission_dialog_visible")

# 整页遮挡的面积阈值：与 H5 遮挡判定里"整屏遮罩"的口径一致。
_FULL_PAGE_COVERAGE = 0.5

_OVERLAY_TARGET_HINT = re.compile(r"弹窗|蒙层|蒙板|遮罩|浮层|启动广告|开屏")

H5_SNAPSHOT_SOURCE = "h5_ui_snapshot"
H5_PAGE_STATE_SOURCE = "page_state"
H5_ACTION_LOG_SOURCE = "h5_action_log"
APP_VISUAL_SOURCE = "app_visual_observation"
SESSION_LINEAGE_SOURCE = "surface_session_lineage"


# ---- 证据清单显式字段（采集侧声明，读侧直接取值） -------------------------------------
# 采集侧在采集元数据里写出这三项后，读侧按声明取值，不再靠页面结构、识别状态去推。
DECLARED_OBSTRUCTED_KEY = "is_obstructed"
DECLARED_DIAGNOSTIC_KEY = "diagnostic"
DECLARED_SESSION_AUTH_KEY = "session_auth"
CAPTURE_DECLARED_SOURCE = "capture_declared"
DECLARED_OVERRIDES_DERIVED = "declared_obstruction_overrides_derived"
SESSION_AUTH_VALUES = frozenset({"guest", "authenticated", "unknown"})


HIT_TEST_AVAILABLE = "available"
HIT_TEST_DEGRADED = "degraded"
HIT_TEST_UNAVAILABLE = "unavailable"
PAGE_STATE_REPORTS_OVERLAY = "page_state_reports_overlay"
# Flutter 语义树激活状态：只有"不适用"（普通 DOM 页面）时结构命中测试才对遮挡有权威。
_DOM_STRUCTURE_ACTIVATION_STATES = frozenset({"", "not_applicable"})


def h5_hit_test_status(ui_snapshot: Mapping[str, Any] | None) -> str:
    """H5 结构快照对"最上层是谁"的证明力：DOM 页面可用，Flutter 画布降级，无快照不可用。"""
    if not isinstance(ui_snapshot, Mapping) or not isinstance(ui_snapshot.get("elements"), list):
        return HIT_TEST_UNAVAILABLE
    activation = _text(ui_snapshot.get("flutter_semantics_activation")).casefold()
    if activation in _DOM_STRUCTURE_ACTIVATION_STATES:
        return HIT_TEST_AVAILABLE
    return HIT_TEST_DEGRADED


def diagnostic_operations_from_action_log(
    action_log: Sequence[Any] | None,
) -> tuple[DiagnosticOperation, ...]:
    """从 H5 动作记录里找出改动过页面的操作（隐藏弹窗、脚本派发点击）。"""
    operations: list[DiagnosticOperation] = []
    for entry in action_log or ():
        if not isinstance(entry, Mapping):
            continue
        fallback = _text(entry.get("fallback"))
        if fallback in _STYLE_HIDE_FALLBACKS:
            operations.append(DiagnosticOperation(kind="style_hide_obstruction", source=fallback))
        resolved_by = _text(entry.get("resolved_by"))
        if resolved_by in _JS_DISPATCH_RESOLUTIONS:
            operations.append(DiagnosticOperation(kind="js_click_dispatch", source=resolved_by))
        attempts = entry.get("fallback_dispatch_attempts")
        if isinstance(attempts, (list, tuple)) and attempts:
            operations.append(
                DiagnosticOperation(kind="js_click_dispatch", source="fallback_dispatch_attempts")
            )
    return _dedupe(operations)


def document_epoch_from_snapshot(ui_snapshot: Mapping[str, Any] | None) -> float | None:
    """UI Snapshot 里的文档起始时间（document.time_origin_ms = performance.timeOrigin）。

    整页重新加载会产生新文档、新的起始时间；SPA 页内路由、切 Tab、开弹层都在同一文档内。
    没有快照或字段缺失时返回 None，继承逻辑按保守口径处理（照常继承）。
    """
    if not isinstance(ui_snapshot, Mapping):
        return None
    document = ui_snapshot.get("document")
    if not isinstance(document, Mapping):
        return None
    value = document.get("time_origin_ms")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return float(value)


def derive_h5_presentation(
    *,
    ui_snapshot: Mapping[str, Any] | None,
    page_state: str | None = None,
    action_log: Sequence[Any] | None = None,
    inherited_operations: Sequence[DiagnosticOperation] = (),
) -> PresentationAssessment:
    """由 H5 UI Snapshot 的命中测试事实、识别出的页面状态与动作记录推导展示层。

    UI Snapshot 是结构证据（真实矩形与 hit-test），页面状态是模型对截图的解释；
    两者任一给出遮挡即认定最上层有遮挡物，避免 Flutter 画布应用因结构缺失被判成"无遮挡"。
    """
    sources: list[str] = []
    uncertainty: list[str] = []
    state = _text(page_state).casefold()
    state_overlay = state in _OVERLAY_STATES
    state_system = state in _SYSTEM_DIALOG_STATES
    if state:
        sources.append(H5_PAGE_STATE_SOURCE)

    assessment = assess_h5_ui_obstruction(ui_snapshot)
    snapshot_available = isinstance(ui_snapshot, Mapping) and isinstance(
        ui_snapshot.get("elements"), list
    )
    if snapshot_available:
        sources.append(H5_SNAPSHOT_SOURCE)
    hit_test_status = h5_hit_test_status(ui_snapshot)

    if assessment.kind == "blocking_overlay":
        coverage = assessment.max_blocking_coverage
        full_page = coverage >= _FULL_PAGE_COVERAGE
        presentation = PresentationAssessment(
            topmost_layer_kind=(
                TopmostLayerKind.SYSTEM_DIALOG if state_system else TopmostLayerKind.OVERLAY
            ),
            base_page_visibility=(
                BasePageVisibility.FULLY_OCCLUDED
                if full_page
                else BasePageVisibility.PARTIALLY_OCCLUDED
            ),
            blocking_scope=BlockingScope.PAGE if full_page else BlockingScope.REGION,
            overlay_semantic_kind=_SEMANTIC_KIND_BY_STATE.get(state, "unknown"),
            overlay_coverage=coverage,
            hit_test_status=hit_test_status,
            evidence_sources=tuple(sources),
        )
    elif (
        state_overlay
        and not state_system
        and assessment.kind == "none"
        and hit_test_status == HIT_TEST_AVAILABLE
    ):
        # DOM 结构已正面扫过、没有任何阻断元素，而识别结果说是 popup：实测这多半是
        # 悬浮推广条一类局部浮层被模型标成弹窗，底页其实完整可见。以结构为准，
        # 但把分歧记进不确定项，判定器能看到"识别曾报告弹窗"。
        uncertainty.append(PAGE_STATE_REPORTS_OVERLAY)
        presentation = PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.PAGE,
            base_page_visibility=BasePageVisibility.VISIBLE,
            blocking_scope=BlockingScope.REGION,
            overlay_semantic_kind=_SEMANTIC_KIND_BY_STATE.get(state, "unknown"),
            hit_test_status=hit_test_status,
            evidence_sources=tuple(sources),
            uncertainty=tuple(uncertainty),
        )
    elif state_overlay or state_system:
        # 结构不可信（Flutter 画布 / 无快照）而识别结果说有遮挡：保守认定最上层有
        # 遮挡物，可见程度未知。
        if assessment.kind == "none":
            uncertainty.append("structure_snapshot_disagrees_with_page_state")
        else:
            uncertainty.append("obstruction_geometry_unavailable")
        presentation = PresentationAssessment(
            topmost_layer_kind=(
                TopmostLayerKind.SYSTEM_DIALOG if state_system else TopmostLayerKind.OVERLAY
            ),
            base_page_visibility=BasePageVisibility.UNKNOWN,
            blocking_scope=BlockingScope.SYSTEM if state_system else BlockingScope.UNKNOWN,
            overlay_semantic_kind=_SEMANTIC_KIND_BY_STATE.get(state, "unknown"),
            hit_test_status=hit_test_status,
            evidence_sources=tuple(sources),
            uncertainty=tuple(uncertainty),
        )
    elif assessment.kind == "nonblocking_notification":
        presentation = PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.PAGE,
            base_page_visibility=BasePageVisibility.VISIBLE,
            blocking_scope=BlockingScope.REGION,
            overlay_semantic_kind="notification",
            hit_test_status=hit_test_status,
            evidence_sources=tuple(sources),
        )
    elif assessment.kind == "none":
        presentation = PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.PAGE,
            base_page_visibility=BasePageVisibility.VISIBLE,
            blocking_scope=BlockingScope.NONE,
            hit_test_status=hit_test_status,
            evidence_sources=tuple(sources),
        )
    else:
        # 没有结构快照、页面状态也不说明遮挡：不宣称可见，也不宣称被遮挡。
        uncertainty.append("ui_snapshot_unavailable")
        presentation = PresentationAssessment(
            topmost_layer_kind=(
                TopmostLayerKind.PAGE if state in _STABLE_STATES else TopmostLayerKind.UNKNOWN
            ),
            base_page_visibility=BasePageVisibility.UNKNOWN,
            blocking_scope=BlockingScope.UNKNOWN,
            hit_test_status=hit_test_status,
            evidence_sources=tuple(sources),
            uncertainty=tuple(uncertainty),
        )

    presentation = replace(
        presentation, document_epoch_ms=document_epoch_from_snapshot(ui_snapshot)
    )
    own_operations = diagnostic_operations_from_action_log(action_log)
    if own_operations:
        presentation = _with_source(presentation, H5_ACTION_LOG_SOURCE)
    if inherited_operations:
        presentation = _with_source(presentation, SESSION_LINEAGE_SOURCE)
    return presentation.with_diagnostic_operations((*own_operations, *inherited_operations))


def derive_app_presentation(
    *,
    page_state: str | None,
    overlay_signature: str | None,
    surface_facts: Mapping[str, Any] | None,
) -> PresentationAssessment:
    """由 App 视觉观察（页面状态、覆盖层签名、表面事实）推导展示层。"""
    facts = surface_facts if isinstance(surface_facts, Mapping) else {}
    state = _text(page_state).casefold()
    signature = _text(overlay_signature) or None
    sources = (APP_VISUAL_SOURCE,)

    system_dialog = state in _SYSTEM_DIALOG_STATES or any(
        facts.get(key) is True for key in _APP_SYSTEM_FACT_KEYS
    )
    semantic_kind = _SEMANTIC_KIND_BY_STATE.get(state)
    overlay_present = state in _OVERLAY_STATES or bool(signature)
    for key, kind in _APP_OVERLAY_FACT_KINDS.items():
        if facts.get(key) is True:
            overlay_present = True
            if semantic_kind is None and kind != "unknown":
                semantic_kind = kind
    ad_popup = facts.get("topmost_ad_popup")
    coverage: float | None = None
    if isinstance(ad_popup, Mapping) and ad_popup.get("is_ad") is True:
        overlay_present = True
        semantic_kind = semantic_kind or "advertisement"
        coverage = _normalized_bounds_coverage(ad_popup.get("normalized_bounds"))

    if system_dialog:
        return PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.SYSTEM_DIALOG,
            base_page_visibility=BasePageVisibility.FULLY_OCCLUDED,
            blocking_scope=BlockingScope.SYSTEM,
            overlay_signature=signature,
            overlay_semantic_kind=semantic_kind or "system_dialog",
            hit_test_status="unavailable",
            evidence_sources=sources,
        )
    if overlay_present:
        partial = coverage is not None and coverage < _FULL_PAGE_COVERAGE
        return PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.OVERLAY,
            base_page_visibility=(
                BasePageVisibility.PARTIALLY_OCCLUDED
                if partial
                else BasePageVisibility.FULLY_OCCLUDED
            ),
            blocking_scope=BlockingScope.REGION if partial else BlockingScope.PAGE,
            overlay_signature=signature,
            overlay_semantic_kind=semantic_kind or "unknown",
            overlay_coverage=coverage,
            hit_test_status="unavailable",
            evidence_sources=sources,
        )
    if state in _STABLE_STATES:
        return PresentationAssessment(
            topmost_layer_kind=TopmostLayerKind.PAGE,
            base_page_visibility=BasePageVisibility.VISIBLE,
            blocking_scope=BlockingScope.NONE,
            hit_test_status="unavailable",
            evidence_sources=sources,
        )
    return unknown_presentation(
        uncertainty=(f"page_state_{state or 'missing'}",),
        evidence_sources=sources,
    )


def snapshot_for_presentation(value: Any) -> Mapping[str, Any] | None:
    """把 UI Snapshot 产物归一成单帧：时序包取尾帧（页面稳定后的状态），字符串按 JSON 解析。"""
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if not isinstance(value, Mapping):
        return None
    if value.get("sequence") is True:
        frames = value.get("frames")
        if not isinstance(frames, list):
            return None
        for frame in reversed(frames):
            if isinstance(frame, Mapping) and isinstance(frame.get("elements"), list):
                return frame
        return None
    return value


def presentation_from_capture_result(
    *,
    artifacts: Sequence[Any],
    action_trace: Sequence[Any] | None,
    page_state: str | None = None,
) -> PresentationAssessment:
    """H5 写入口：从本次采集的 UI Snapshot 产物与动作记录推导展示层评估。"""
    snapshot: Mapping[str, Any] | None = None
    for artifact in artifacts:
        if getattr(artifact, "artifact_type", None) != "ui_snapshot":
            continue
        snapshot = snapshot_for_presentation(getattr(artifact, "content", None))
        if snapshot is not None:
            break
    return derive_h5_presentation(
        ui_snapshot=snapshot,
        page_state=page_state,
        action_log=action_trace,
    )


def reconcile_presentation_with_page_state(
    presentation: PresentationAssessment | None,
    *,
    page_state: str | None,
) -> PresentationAssessment | None:
    """采集时还没有页型识别；识别出的页面状态说明有遮挡时，在读侧补上这一层结论。"""
    state = _text(page_state).casefold()
    if presentation is None or not state:
        return presentation
    if presentation.occluding_layer_present or presentation.is_diagnostic:
        return presentation
    if state not in _OVERLAY_STATES and state not in _SYSTEM_DIALOG_STATES:
        return presentation
    if (
        state not in _SYSTEM_DIALOG_STATES
        and presentation.topmost_layer_kind is TopmostLayerKind.PAGE
        and presentation.hit_test_status == HIT_TEST_AVAILABLE
    ):
        # 采集时 DOM 结构已确认无阻断：识别状态只作为分歧记录，不改层级结论。
        # 系统对话框（权限、浏览器提示）不在 DOM 里，结构扫不到不算反证，不走这条。
        if PAGE_STATE_REPORTS_OVERLAY in presentation.uncertainty:
            return presentation
        return replace(
            presentation,
            evidence_sources=tuple(
                dict.fromkeys((*presentation.evidence_sources, H5_PAGE_STATE_SOURCE))
            ),
            uncertainty=(*presentation.uncertainty, PAGE_STATE_REPORTS_OVERLAY),
        )
    system = state in _SYSTEM_DIALOG_STATES
    uncertainty = (
        "structure_snapshot_disagrees_with_page_state"
        if presentation.topmost_layer_kind is TopmostLayerKind.PAGE
        else "obstruction_geometry_unavailable"
    )
    return PresentationAssessment(
        topmost_layer_kind=(TopmostLayerKind.SYSTEM_DIALOG if system else TopmostLayerKind.OVERLAY),
        base_page_visibility=BasePageVisibility.UNKNOWN,
        blocking_scope=BlockingScope.SYSTEM if system else BlockingScope.UNKNOWN,
        overlay_semantic_kind=_SEMANTIC_KIND_BY_STATE.get(state, "unknown"),
        hit_test_status=presentation.hit_test_status,
        evidence_sources=tuple(
            dict.fromkeys((*presentation.evidence_sources, H5_PAGE_STATE_SOURCE))
        ),
        uncertainty=tuple(dict.fromkeys((*presentation.uncertainty, uncertainty))),
    )


def declared_evidence_flags(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """取出采集元数据里显式声明的清单字段；没声明或取值不合法的项不出现在结果里。

    is_obstructed 只认布尔；diagnostic 认 true、单个操作名或操作名数组（false 视同没声明，
    平台记录在案的诊断操作不因一句"没有"被抹掉）；
    session_auth 只认 guest / authenticated / unknown。
    """
    if not isinstance(metadata, Mapping):
        return {}
    flags: dict[str, Any] = {}
    obstructed = metadata.get(DECLARED_OBSTRUCTED_KEY)
    if isinstance(obstructed, bool):
        flags[DECLARED_OBSTRUCTED_KEY] = obstructed
    diagnostic = metadata.get(DECLARED_DIAGNOSTIC_KEY)
    if diagnostic is True:
        flags[DECLARED_DIAGNOSTIC_KEY] = ["declared"]
    elif isinstance(diagnostic, str) and diagnostic.strip():
        flags[DECLARED_DIAGNOSTIC_KEY] = [diagnostic.strip()]
    elif isinstance(diagnostic, (list, tuple)):
        kinds = list(dict.fromkeys(_text(item) for item in diagnostic if _text(item)))
        if kinds:
            flags[DECLARED_DIAGNOSTIC_KEY] = kinds
    session_auth = _text(metadata.get(DECLARED_SESSION_AUTH_KEY)).casefold()
    if session_auth in SESSION_AUTH_VALUES:
        flags[DECLARED_SESSION_AUTH_KEY] = session_auth
    return flags


def apply_declared_evidence_flags(
    presentation: PresentationAssessment | None,
    flags: Mapping[str, Any] | None,
) -> PresentationAssessment | None:
    """采集侧显式声明了遮挡 / 诊断时以声明为准；没声明的那一项保持推导结果。

    遮挡：声明覆盖推导（两个方向都覆盖），推导与声明不一致时把分歧记进不确定项。
    诊断：声明只增不减，和动作记录里推出来的诊断操作合并。
    """
    if not flags:
        return presentation
    obstructed = flags.get(DECLARED_OBSTRUCTED_KEY)
    kinds = flags.get(DECLARED_DIAGNOSTIC_KEY) or ()
    if not isinstance(obstructed, bool) and not kinds:
        return presentation
    result = presentation or unknown_presentation()
    if isinstance(obstructed, bool):
        derived_obstructed = (
            None
            if presentation is None or presentation.topmost_layer_kind is TopmostLayerKind.UNKNOWN
            else presentation.occluding_layer_present
        )
        uncertainty = tuple(
            item
            for item in result.uncertainty
            if item not in {"presentation_unavailable", DECLARED_OVERRIDES_DERIVED}
        )
        if derived_obstructed is not None and derived_obstructed != obstructed:
            uncertainty = (*uncertainty, DECLARED_OVERRIDES_DERIVED)
        if obstructed:
            preserve_measured_geometry = (
                result.occluding_layer_present
                and result.base_page_visibility is BasePageVisibility.PARTIALLY_OCCLUDED
                and result.blocking_scope is BlockingScope.REGION
                and result.overlay_coverage is not None
            )
            result = replace(
                result,
                topmost_layer_kind=(
                    result.topmost_layer_kind
                    if result.occluding_layer_present
                    else TopmostLayerKind.OVERLAY
                ),
                base_page_visibility=(
                    result.base_page_visibility
                    if preserve_measured_geometry
                    else BasePageVisibility.FULLY_OCCLUDED
                ),
                blocking_scope=(
                    result.blocking_scope
                    if preserve_measured_geometry
                    or result.blocking_scope
                    in {BlockingScope.PAGE, BlockingScope.APPLICATION, BlockingScope.SYSTEM}
                    else BlockingScope.PAGE
                ),
                uncertainty=uncertainty,
            )
        else:
            result = replace(
                result,
                topmost_layer_kind=TopmostLayerKind.PAGE,
                base_page_visibility=BasePageVisibility.VISIBLE,
                blocking_scope=BlockingScope.NONE,
                uncertainty=uncertainty,
            )
    result = _with_source(result, CAPTURE_DECLARED_SOURCE)
    return result.with_diagnostic_operations(
        tuple(DiagnosticOperation(kind=kind, source=CAPTURE_DECLARED_SOURCE) for kind in kinds)
    )


def presentation_from_capture_row(
    *,
    surface_kind: str | None,
    metadata: Mapping[str, Any] | None,
    page_state: str | None,
    visual_page_state: str | None = None,
    visual_overlay_signature: str | None = None,
    visual_surface_facts: Mapping[str, Any] | None = None,
) -> PresentationAssessment | None:
    """读侧统一入口：H5 读采集行持久化的评估并用识别状态校正；App 由视觉观察推导。

    采集元数据里显式声明了遮挡 / 诊断的，最后按声明取值。
    """
    flags = declared_evidence_flags(metadata)
    if str(surface_kind or "").casefold() in {"android", "ios"}:
        if (
            visual_page_state is None
            and visual_overlay_signature is None
            and visual_surface_facts is None
        ):
            return apply_declared_evidence_flags(None, flags)
        return apply_declared_evidence_flags(
            derive_app_presentation(
                page_state=visual_page_state,
                overlay_signature=visual_overlay_signature,
                surface_facts=visual_surface_facts,
            ),
            flags,
        )
    return apply_declared_evidence_flags(
        reconcile_presentation_with_page_state(
            presentation_from_capture_metadata(metadata),
            page_state=page_state,
        ),
        flags,
    )


def presentation_from_capture_metadata(
    metadata: Mapping[str, Any] | None,
) -> PresentationAssessment | None:
    """读取采集元数据里持久化的展示层评估；没有或不合法时返回 None。"""
    if not isinstance(metadata, Mapping):
        return None
    return PresentationAssessment.from_dict(metadata.get(PRESENTATION_METADATA_KEY))


def rule_evidence_layer_target(
    *,
    category: str | None,
    judge_config: Mapping[str, Any] | None = None,
    check_point: str | None = None,
    rule_name: str | None = None,
) -> EvidenceLayerTarget:
    """规则要求证据落在哪一层。

    优先级：规则配置显式声明 > 广告类规则（弹窗本身就是目标） >
    检查点文案提到弹窗/蒙层/遮罩（目标可能在浮层上） > 默认要求底层页面可见。
    专项四的规则范围计划落地后，显式声明应成为唯一来源。
    """
    config = judge_config if isinstance(judge_config, Mapping) else {}
    declared = _text(config.get("evidence_layer_target")).casefold()
    if declared:
        try:
            return EvidenceLayerTarget(declared)
        except ValueError:
            pass
    if _text(category).casefold() == "ad":
        return EvidenceLayerTarget.ANY
    hint_text = f"{_text(rule_name)} {_text(check_point)}"
    if _OVERLAY_TARGET_HINT.search(hint_text):
        return EvidenceLayerTarget.ANY
    return EvidenceLayerTarget.BASE_PAGE


def app_presentation_ready_for_rule(
    presentation: PresentationAssessment | None,
    rule: Mapping[str, Any],
    *,
    paired_before: bool = False,
    page_state: str | None = None,
) -> bool:
    """业务页被遮挡时先清障；广告、浮层及已配对的前后帧规则仍可发布。"""

    config = rule.get("judge_config")
    config = config if isinstance(config, Mapping) else {}
    target = rule_evidence_layer_target(
        category=str(rule.get("category") or "") or None,
        judge_config=config,
        check_point=str(rule.get("check_point") or "") or None,
        rule_name=str(rule.get("name") or "") or None,
    )
    engine = config.get("engine_config")
    states = engine.get("required_page_states") if isinstance(engine, Mapping) else None
    # 显式要求当前浮层状态的规则可以检查该浮层；显式 base_page 声明仍优先。
    state_targeted = (
        isinstance(states, (list, tuple))
        and page_state in states
        and page_state in (_OVERLAY_STATES | _SYSTEM_DIALOG_STATES | {"login_form"})
        and _text(config.get("evidence_layer_target")).casefold() != "base_page"
    )
    verdict = evaluate_presentation_for_rule(
        presentation,
        layer_target=EvidenceLayerTarget.ANY if paired_before or state_targeted else target,
        accepts_diagnostic=rule_accepts_diagnostic_evidence(config),
    )
    if not verdict.accepted:
        return False
    # 部分可见也不足以证明滚动、目录切换等底层业务旅程；此处只延后发布，
    # 不更改展示层门禁，不生成执行或不适用结论。未知展示层没有正向遮挡依据。
    return not (
        presentation is not None
        and presentation.occluding_layer_present
        and target is EvidenceLayerTarget.BASE_PAGE
        and not paired_before
        and not state_targeted
    )


def rule_accepts_diagnostic_evidence(judge_config: Mapping[str, Any] | None) -> bool:
    """只有规则配置显式声明 accepts_diagnostic_evidence 才接受诊断材料。"""
    config = judge_config if isinstance(judge_config, Mapping) else {}
    return config.get("accepts_diagnostic_evidence") is True


def inherited_operations_from_presentation(
    presentation: PresentationAssessment | None,
    *,
    origin_capture_id: str,
) -> tuple[DiagnosticOperation, ...]:
    """把某次采集的诊断操作改写为可被后续采集继承的形式。"""
    if presentation is None:
        return ()
    if presentation.mutation_mode is not MutationMode.INSTRUMENTED_DIAGNOSTIC:
        return ()
    return tuple(
        item.inherited_by(origin_capture_id=origin_capture_id)
        for item in presentation.diagnostic_operations
    )


def _normalized_bounds_coverage(value: Any) -> float | None:
    if not isinstance(value, Mapping):
        return None
    try:
        left = float(value.get("left"))
        top = float(value.get("top"))
        right = float(value.get("right"))
        bottom = float(value.get("bottom"))
    except (TypeError, ValueError):
        return None
    width = min(max(right, 0.0), 1.0) - min(max(left, 0.0), 1.0)
    height = min(max(bottom, 0.0), 1.0) - min(max(top, 0.0), 1.0)
    if width <= 0 or height <= 0:
        return None
    return min(width * height, 1.0)


def _with_source(presentation: PresentationAssessment, source: str) -> PresentationAssessment:
    if source in presentation.evidence_sources:
        return presentation
    # 用 replace 而不是逐字段重建，新增字段（如 document_epoch_ms）不会在这里被丢掉。
    return replace(presentation, evidence_sources=(*presentation.evidence_sources, source))


def _dedupe(operations: Sequence[DiagnosticOperation]) -> tuple[DiagnosticOperation, ...]:
    seen: dict[tuple[str, str, str | None], DiagnosticOperation] = {}
    for item in operations:
        seen.setdefault((item.kind, item.source, item.origin_capture_id), item)
    return tuple(seen.values())


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""
