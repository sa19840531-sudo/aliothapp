"""App 五項優化：Lab 補採灰度、影子雙讀、Settle／ADB／Plan 回歸。"""

from __future__ import annotations

from pathlib import Path

from services.api.app.application.app_capture_readiness_diagnosis import (
    SettleReadinessProfile,
    build_app_capture_readiness_diagnosis,
    build_settle_shadow_manifest_audit,
)
from services.api.app.application.app_interaction_plan import (
    build_scroll_rebind_click_plan,
    mark_plan_after_scroll,
    mark_plan_click_ready,
    mark_plan_completed,
    mark_plan_rebind_failed,
    plan_requires_fresh_capture_for_click,
)
from services.api.app.application.page_graph_projection import (
    attach_app_coverage_page_graph_shadow,
    reconcile_coverage_with_page_graph,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceNotReadyError,
    SupplementalEvidenceOperation,
    build_current_supplemental_evidence_policy,
    build_lab_supplemental_evidence_policy,
    resolve_supplemental_evidence_policy,
)
from services.api.app.infrastructure.device.android_device_ops import (
    AdbProvider,
    ShadowAndroidDeviceOps,
)
from services.api.app.infrastructure.device.android_surface_driver import (
    AndroidAdbSurfaceDriver,
)


def test_lab_supplemental_policy_passes_runtime_ready_gate() -> None:
    lab = build_lab_supplemental_evidence_policy(enabled=True)
    assert lab.runtime_ready is True
    lab.validate_runtime_ready()
    decision = lab.decide(SupplementalEvidenceOperation.EXECUTE_PROBE)
    assert decision.allowed is True

    resolved = resolve_supplemental_evidence_policy(
        enabled=True,
        lab_runtime_ready=True,
    )
    assert resolved.runtime_ready is True


def test_production_factory_keeps_runtime_ready_false() -> None:
    policy = build_current_supplemental_evidence_policy(enabled=True)
    assert policy.runtime_ready is False
    try:
        policy.validate_runtime_ready()
    except SupplementalEvidenceNotReadyError:
        pass
    else:
        raise AssertionError("生产工厂开启补采时必须因 runtime_ready=false 拒启")

    resolved = resolve_supplemental_evidence_policy(
        enabled=True,
        lab_runtime_ready=False,
    )
    assert resolved.runtime_ready is False


def test_coverage_page_graph_shadow_attach_never_changes_authority() -> None:
    empty = attach_app_coverage_page_graph_shadow(
        {"covered_business_state_ids": ("page:a",)},
        graph=None,
    )
    shadow = empty["app_coverage_page_graph_shadow"]
    assert shadow["coverage_authority_changed"] is False
    assert shadow["schedules_from_graph"] is False
    assert shadow["gate_consumes_graph"] is False
    # M2 双写会把 Coverage 身份写入影子图后再对账，故无外部图时也可对齐。
    assert shadow["aligned"] is True
    assert empty["covered_business_state_ids"] == ("page:a",)

    matched = attach_app_coverage_page_graph_shadow(
        {"covered_business_state_ids": ("page:home",)},
        graph={
            "task_id": "t",
            "agent_run_id": "r",
            "finalization_epoch": 1,
            "states": [
                {
                    "task_id": "t",
                    "agent_run_id": "r",
                    "finalization_epoch": 1,
                    "state_id": "c1",
                    "logical_page_key": "page:home",
                    "state_fingerprint": "s",
                    "identity_status": "confirmed",
                    "counts_as_business_page": True,
                    "is_overlay": False,
                    "is_external": False,
                }
            ],
            "transitions": [],
        },
    )
    assert matched["app_coverage_page_graph_shadow"]["aligned"] is True
    assert matched["app_coverage_page_graph_shadow"]["coverage_authority_changed"] is False
    # 权威字段仍只来自 Coverage metadata，不因图对账改写。
    assert matched["covered_business_state_ids"] == ("page:home",)


def test_reconcile_mismatch_summary_does_not_affect_gate_fields() -> None:
    coverage = {
        "covered_business_state_ids": ("page:a", "page:b"),
        "coverage_complete": True,
    }
    summary = reconcile_coverage_with_page_graph(coverage_metadata=coverage, graph=None)
    assert summary["coverage_authority_changed"] is False
    assert coverage["coverage_complete"] is True


def test_settle_action_after_and_reobserve_profiles_write_audit() -> None:
    action_after = build_app_capture_readiness_diagnosis(
        page_state="stable",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.ACTION_AFTER,
    )
    assert action_after["profile"] == "ACTION_AFTER"
    assert action_after["network_observable"] == "not_observable"
    assert action_after["formal_settle_oracle"] is False
    assert "network_not_observable" in action_after["reason_codes"]

    reobserve = build_app_capture_readiness_diagnosis(
        page_state="stable",
        has_confirmed_visual_identity=True,
        profile=SettleReadinessProfile.REOBSERVE,
    )
    audit = build_settle_shadow_manifest_audit(reobserve)
    assert audit["profile"] == "REOBSERVE"
    assert audit["formal_settle_oracle"] is False
    assert audit["gate_consumes_settle"] is False


def test_shadow_adb_provider_records_zero_device_input() -> None:
    ops = ShadowAndroidDeviceOps()
    assert isinstance(ops, AdbProvider)
    ops.tap(10, 20)
    ops.swipe(0, 0, 0, 100, ms=200)
    ops.back()
    ops.type_text("hello")
    trace = ops.screencap(Path("shadow.png"))
    assert trace.path_mode == "shadow"
    assert [item["operation"] for item in ops.commands] == [
        "tap",
        "swipe",
        "back",
        "type_text",
        "screencap",
    ]


def test_android_driver_rejects_second_ops_owner() -> None:
    driver = AndroidAdbSurfaceDriver(backend="ldplayer", ops=ShadowAndroidDeviceOps())
    try:
        driver.bind_ops(ShadowAndroidDeviceOps(), owner_token="other-owner")
    except RuntimeError as exc:
        assert "双 Owner" in str(exc)
    else:
        raise AssertionError("第二 Owner 绑定必须被拒绝")


def test_scroll_rebind_click_plan_success_and_failure_paths() -> None:
    plan = build_scroll_rebind_click_plan(
        click_candidate_id="cand-1",
        source_capture_id="cap-before",
    )
    assert plan.status == "scroll_pending"
    after = mark_plan_after_scroll(plan, after_scroll_capture_id="cap-after")
    assert after.status == "rebind_pending"
    assert plan_requires_fresh_capture_for_click(after) is True
    ready = mark_plan_click_ready(after)
    assert ready.status == "click_pending"
    done = mark_plan_completed(ready)
    assert done.status == "completed"

    failed = mark_plan_rebind_failed(after)
    assert failed.status == "failed_rebind"
    assert failed.failure_reason == "POST_SCROLL_REBIND_REQUIRED"
