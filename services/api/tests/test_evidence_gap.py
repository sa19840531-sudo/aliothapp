"""证据缺口协议：页面、状态、区域、关闭前后、规则声明与请求预览。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.inspection.evidence_gap import (
    EVIDENCE_GAP_PROTOCOL_VERSION,
    GapDisposition,
    OverlayPhase,
    RecapturePolicy,
    assert_gap_has_no_operation_details,
    build_evidence_gap,
    build_evidence_request_from_gap,
    materialize_evidence_request,
    parse_recapture_policy,
)
from services.api.app.domain.inspection.evidence_request import EvidenceSessionDependency
from services.api.app.domain.interaction.enums import InteractionOperation

EXPIRES = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)


def _gap(**overrides):
    payload = {
        "task_id": "task-1",
        "rule_id": "rule-1",
        "rule_code": "alioth_5",
        "rule_name": "页面模块空白检查",
        "category": "visual",
        "check_point": "不应出现内容未上传导致的模块空白",
        "page_capture_id": "capture-1",
        "page": {
            "page_capture_id": "capture-1",
            "url": "https://example.com/home",
            "title": "首页",
            "page_type": "home",
            "page_state": "stable",
            "surface_kind": "h5",
        },
        "reason_code": "EVIDENCE_INSUFFICIENT",
        "request_expires_at": EXPIRES,
    }
    payload.update(overrides)
    return build_evidence_gap(**payload)


def test_occluded_base_page_gap_asks_for_after_close_evidence() -> None:
    gap = _gap(
        presentation_code="BASE_PAGE_OCCLUDED",
        presentation_reason="最上层是充值弹窗，底层页面不可见",
        judge_statements=["关闭遮挡后、底层页面真实可见的当前页面证据"],
    )

    assert gap.protocol_version == EVIDENCE_GAP_PROTOCOL_VERSION
    assert gap.target.page_type == "home"
    assert gap.target.observed_page_state == "stable"
    assert gap.target.region_kind == "page"
    assert gap.target.region_label.startswith("页面可见内容：")
    assert gap.overlay_phase is OverlayPhase.AFTER_CLOSE
    assert gap.missing.presentation_code == "BASE_PAGE_OCCLUDED"
    assert "弹窗关闭后" in gap.evidence_goal
    assert "home" in gap.evidence_goal
    # 规则没声明补采方式：只记录，转人工，不产生请求。
    assert gap.recapture_policy is RecapturePolicy.MANUAL
    assert gap.disposition is GapDisposition.MANUAL_REVIEW_REQUIRED
    assert gap.requested_request is None


def test_overlay_rule_missing_before_role_asks_for_before_close_evidence() -> None:
    gap = _gap(
        rule_code="alioth_21",
        category="ad",
        check_point="弹窗广告关闭按钮应清晰可点",
        layer_target="any",
        required_page_states=["popup", "modal"],
        page={"page_capture_id": "capture-1", "page_type": "home", "page_state": "stable"},
        missing_capture_roles=["before"],
    )

    assert gap.target.region_kind == "overlay"
    assert gap.target.region_label.startswith("广告位或弹窗浮层：")
    assert gap.target.required_page_states == ("popup", "modal")
    assert gap.missing.page_states == ("popup", "modal")
    assert gap.missing.capture_roles == ("before",)
    assert gap.overlay_phase is OverlayPhase.BEFORE_CLOSE
    assert "弹窗关闭前" in gap.evidence_goal
    assert "popup/modal" in gap.evidence_goal


def test_declared_policy_produces_deterministic_request_preview() -> None:
    contract = {"recapture_policy": "equivalent"}
    first = _gap(input_contract=contract, missing_artifact_types=["screenshot"])
    second = _gap(
        input_contract=contract,
        missing_artifact_types=["screenshot"],
        request_expires_at=EXPIRES + timedelta(hours=1),
    )

    assert first.recapture_policy is RecapturePolicy.EQUIVALENT
    assert first.disposition is GapDisposition.RECORDED_ONLY
    assert first.requested_request is not None
    assert first.requested_request["request_id"].startswith("evidence-request:")
    assert first.requested_request["replay_policy"] == "same_context_allowed"
    assert first.requested_request["required_equivalence"] == "logical_equivalent"
    assert first.requested_request["side_effect_policy"] == "read_only"
    assert first.requested_request["required_artifact_types"] == ["screenshot"]
    assert first.requested_request["disposition"] == "recorded_only"
    # 请求身份只由目标采集、规则和要求决定，与过期时间无关；缺口编号同样稳定。
    assert first.requested_request["request_id"] == second.requested_request["request_id"]
    assert first.gap_id == second.gap_id


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("exact", RecapturePolicy.EXACT),
        ("Equivalent", RecapturePolicy.EQUIVALENT),
        ({"kind": "fresh"}, RecapturePolicy.FRESH),
        ("manual", RecapturePolicy.MANUAL),
        # 总设计 §19.1 的正式写法
        ("exact_state", RecapturePolicy.EXACT),
        ("equivalent_state", RecapturePolicy.EQUIVALENT),
        ("fresh_observation", RecapturePolicy.FRESH),
        ("manual_only", RecapturePolicy.MANUAL),
        ("whatever", RecapturePolicy.MANUAL),
        (None, RecapturePolicy.MANUAL),
    ],
)
def test_recapture_policy_is_read_from_rule_contract_and_never_widened(declared, expected) -> None:
    contract = {"recapture_policy": declared} if declared is not None else {}

    assert parse_recapture_policy(contract) is expected


def test_declaration_with_fallback_is_carried_but_never_widens_primary() -> None:
    from services.api.app.domain.inspection.evidence_gap import parse_recapture_declaration

    assert parse_recapture_declaration(
        {"recapture_policy": {"kind": "equivalent_state", "fallback": "fresh_observation"}}
    ) == (RecapturePolicy.EQUIVALENT, RecapturePolicy.FRESH)
    # 备选和首选相同等于没有备选；首选非法时备选也不生效。
    assert parse_recapture_declaration(
        {"recapture_policy": {"kind": "manual_only", "fallback": "manual_only"}}
    ) == (RecapturePolicy.MANUAL, None)
    assert parse_recapture_declaration(
        {"recapture_policy": {"kind": "bogus", "fallback": "fresh_observation"}}
    ) == (RecapturePolicy.MANUAL, None)

    gap = _gap(
        input_contract={"recapture_policy": {"kind": "exact_state", "fallback": "manual_only"}}
    )
    assert gap.recapture_policy is RecapturePolicy.EXACT
    assert gap.recapture_fallback is RecapturePolicy.MANUAL
    assert gap.to_dict()["recapture_fallback"] == "manual"
    assert gap.requested_request["replay_policy"] == "same_surface_only"


def test_exact_and_fresh_map_to_distinct_recovery_constraints() -> None:
    exact = _gap(input_contract={"recapture_policy": "exact"})
    fresh = _gap(input_contract={"recapture_policy": "fresh"})

    assert exact.requested_request["session_dependency"] == "active_session"
    assert exact.requested_request["replay_policy"] == "same_surface_only"
    assert exact.requested_request["minimum_source_grade"] == "A1"
    assert fresh.requested_request["session_dependency"] == "snapshot"
    assert fresh.requested_request["replay_policy"] == "new_session_allowed"
    assert fresh.requested_request["minimum_source_grade"] == "C"
    assert exact.requested_request["request_id"] != fresh.requested_request["request_id"]


def test_materialized_request_uses_current_handoff_identity() -> None:
    gap = _gap(
        input_contract={"recapture_policy": "equivalent_state"},
        missing_artifact_types=["screenshot"],
    )

    request = materialize_evidence_request(
        gap=gap.to_dict(),
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_5",
        page_capture_id="capture-1",
    )

    assert request is not None
    assert request.work_item_id == "work-1"
    assert request.attempt_id == "attempt-1"
    assert request.target_invariants["business"]["page_type"] == "home"
    assert request.request_id == gap.requested_request["request_id"]


def test_materialized_request_preserves_missing_capture_roles() -> None:
    gap = _gap(
        input_contract={"recapture_policy": "equivalent_state"},
        missing_capture_roles=["primary", "refreshed"],
    )

    request = materialize_evidence_request(
        gap=gap.to_dict(),
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_5",
        page_capture_id="capture-1",
    )

    assert request is not None
    assert request.target_invariants["semantic"]["capture_roles"] == (
        "primary",
        "refreshed",
    )


def test_materialized_request_rejects_preview_drift() -> None:
    payload = _gap(
        input_contract={"recapture_policy": "equivalent_state"},
        missing_artifact_types=["screenshot"],
    ).to_dict()
    payload["requested_request"]["minimum_source_grade"] = "C"

    with pytest.raises(ValueError, match="请求预览"):
        materialize_evidence_request(
            gap=payload,
            work_item_id="work-1",
            attempt_id="attempt-1",
            task_id="task-1",
            rule_id="rule-1",
            rule_code="alioth_5",
            page_capture_id="capture-1",
        )


def test_materialized_request_rejects_gap_fact_drift() -> None:
    payload = _gap(
        input_contract={"recapture_policy": "fresh_observation"},
        missing_artifact_types=["screenshot"],
    ).to_dict()
    payload["missing"]["artifact_types"] = ["dom"]

    with pytest.raises(ValueError, match="gap_id"):
        materialize_evidence_request(
            gap=payload,
            work_item_id="work-1",
            attempt_id="attempt-1",
            task_id="task-1",
            rule_id="rule-1",
            rule_code="alioth_5",
            page_capture_id="capture-1",
        )


def test_gap_without_packet_page_still_names_capture_and_keeps_judge_statements() -> None:
    gap = _gap(
        page={"page_capture_id": "capture-1"},
        judge_statements=["缺少播放器加载完成后的截图", "缺少点击后的页面状态"],
        reason="规则目标已发现，但执行证据不足",
    )

    assert gap.target.page_capture_id == "capture-1"
    assert gap.target.page_type == ""
    assert gap.missing.judge_statements == (
        "缺少播放器加载完成后的截图",
        "缺少点击后的页面状态",
    )
    assert "当前页面" in gap.evidence_goal
    assert "缺少播放器加载完成后的截图" in gap.evidence_goal
    assert gap.overlay_phase is OverlayPhase.NOT_APPLICABLE


def test_gap_payload_is_serializable_and_carries_no_operation_details() -> None:
    gap = _gap(missing_capture_roles=["refreshed"], quality_gate_sources=["ui_snapshot"])
    payload = gap.to_dict()

    assert payload["target"]["region"] == {
        "kind": "page",
        "label": gap.target.region_label,
        "layer_target": "base_page",
    }
    assert payload["missing"]["capture_roles"] == ["refreshed"]
    assert payload["missing"]["quality_gate_sources"] == ["ui_snapshot"]
    assert payload["overlay_phase"] == "not_applicable"
    assert payload["requested_request"] is None
    assert_gap_has_no_operation_details(payload)


def test_operation_details_are_rejected_from_gap_payloads() -> None:
    with pytest.raises(ValueError, match="selector"):
        assert_gap_has_no_operation_details({"target": {"selector": "#close"}})
    with pytest.raises(ValueError, match="coordinate"):
        assert_gap_has_no_operation_details({"steps": [{"coordinate": [1, 2]}]})


def test_occluded_active_session_gap_asks_to_close_the_overlay() -> None:
    """要关掉弹窗才能取到的证据，必须写成动作，不能只写进中文说明。"""

    gap = _gap(
        input_contract={"recapture_policy": "equivalent_state"},
        presentation_code="BASE_PAGE_OCCLUDED",
        presentation_reason="最上层是充值弹窗，底层页面不可见",
        missing_artifact_types=["screenshot"],
    )
    request = build_evidence_request_from_gap(
        policy=gap.recapture_policy,
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_5",
        page_capture_id="capture-1",
        target=gap.target,
        missing=gap.missing,
        overlay_phase=gap.overlay_phase,
        reason_code=gap.reason_code,
        evidence_goal=gap.evidence_goal,
        expires_at=EXPIRES,
        work_item_id="work-1",
        attempt_id="attempt-1",
    )

    assert gap.overlay_phase is OverlayPhase.AFTER_CLOSE
    assert request.session_dependency is EvidenceSessionDependency.ACTIVE_SESSION
    assert request.required_interaction is not None
    assert request.required_interaction.operation is InteractionOperation.CLICK
    assert request.required_interaction.semantic_target == "遮挡底层页面的弹窗关闭控件"
    assert "home" in request.required_interaction.purpose
    # 语义目标不是定位器：选哪个元素仍然归运行面。
    assert_gap_has_no_operation_details(request.to_dict())


def test_snapshot_recapture_stays_observe_only() -> None:
    """纯快照补采会重新走常规清障，不需要定向交互。"""

    gap = _gap(
        input_contract={"recapture_policy": "fresh_observation"},
        presentation_code="BASE_PAGE_OCCLUDED",
        presentation_reason="最上层是充值弹窗，底层页面不可见",
    )
    request = build_evidence_request_from_gap(
        policy=gap.recapture_policy,
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_5",
        page_capture_id="capture-1",
        target=gap.target,
        missing=gap.missing,
        overlay_phase=gap.overlay_phase,
        reason_code=gap.reason_code,
        evidence_goal=gap.evidence_goal,
        expires_at=EXPIRES,
        work_item_id="work-1",
        attempt_id="attempt-1",
    )

    assert gap.overlay_phase is OverlayPhase.AFTER_CLOSE
    assert request.session_dependency is EvidenceSessionDependency.SNAPSHOT
    assert request.required_interaction is None


@pytest.mark.parametrize(
    ("overrides", "expected_phase"),
    [
        (
            {
                "layer_target": "any",
                "category": "ad",
                "required_page_states": ["popup"],
                "missing_capture_roles": ["before"],
            },
            OverlayPhase.BEFORE_CLOSE,
        ),
        ({"quality_gate_sources": ["ui_snapshot"]}, OverlayPhase.NOT_APPLICABLE),
    ],
)
def test_non_occlusion_gaps_keep_null_interaction(overrides, expected_phase) -> None:
    """浮层自身的证据靠观察就能拍到，缺的是时机不是动作。"""

    gap = _gap(input_contract={"recapture_policy": "equivalent_state"}, **overrides)
    request = build_evidence_request_from_gap(
        policy=gap.recapture_policy,
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_5",
        page_capture_id="capture-1",
        target=gap.target,
        missing=gap.missing,
        overlay_phase=gap.overlay_phase,
        reason_code=gap.reason_code,
        evidence_goal=gap.evidence_goal,
        expires_at=EXPIRES,
        work_item_id="work-1",
        attempt_id="attempt-1",
    )

    assert gap.overlay_phase is expected_phase
    assert request.required_interaction is None


def test_materialized_request_carries_close_interaction() -> None:
    """正式链路生成的请求同样带交互要求，且请求预览不因此漂移。"""

    gap = _gap(
        input_contract={"recapture_policy": "equivalent_state"},
        presentation_code="BASE_PAGE_OCCLUDED",
        presentation_reason="最上层是充值弹窗，底层页面不可见",
        missing_artifact_types=["screenshot"],
    )
    # 预览只记录恢复约束，不记录执行动作：存量缺口的预览格式因此保持不变。
    assert "required_interaction" not in gap.requested_request

    request = materialize_evidence_request(
        gap=gap.to_dict(),
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id="task-1",
        rule_id="rule-1",
        rule_code="alioth_5",
        page_capture_id="capture-1",
    )

    assert request is not None
    assert request.required_interaction is not None
    assert request.required_interaction.operation is InteractionOperation.CLICK
    assert request.request_id == gap.requested_request["request_id"]
