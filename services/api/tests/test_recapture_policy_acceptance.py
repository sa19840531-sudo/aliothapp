"""排期「校验补采页面状态并追加结果」验收：按规则声明判断必须原状态、允许等价、允许重新观察还是转人工。

标准：03 专项 §5.3（exact/equivalent/fresh/manual 与恢复约束）、§5.2（人工缺口只记录转人工）、
§4/§8「新 Capture、InputSet 和规则尝试只追加，旧结论不覆盖」。这里用真实的缺口→请求→恢复计划→
等价门禁链路逐条验证，不打桩策略映射。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.agents.rule_judge.current import normalize_rule_judge_outcome
from services.api.app.application.app_restoration_executor import (
    probe_result_to_snapshot,
    snapshot_from_target_invariants,
)
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    EvidenceReplayPolicy,
    EvidenceRequiredEquivalence,
    EvidenceSessionDependency,
    EvidenceSideEffectPolicy,
    EvidenceSourceGrade,
    InspectionHandoff,
    InspectionRuleTarget,
    RestorationStrategy,
    RestorationValidationError,
    build_evidence_request,
    build_evidence_supplement,
    build_restoration_plan,
)
from services.api.app.domain.inspection.evidence_gap import (
    GapDisposition,
    RecapturePolicy,
    build_evidence_gap,
    materialize_evidence_request,
)
from services.api.app.domain.interaction import SurfaceKind

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
TASK = "task-1"
CAPTURE = "capture-original"


def _handoff() -> InspectionHandoff:
    return InspectionHandoff(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id=TASK,
        agent_run_id="consumer-run-1",
        input_hash="input-1",
        batch_key="hybrid:visual:snapshot",
        strategy="hybrid",
        category="visual",
        session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id=CAPTURE,
            producer_agent_run_id="producer-run-1",
        ),
        rules=(
            InspectionRuleTarget(
                rule_id="rule-1", rule_code="alioth_1", ordinal=0, contract_hash="contract-1"
            ),
        ),
    )


def _gap(kind: str, fallback: str | None = None):
    declaration = {"kind": kind} | ({"fallback": fallback} if fallback else {})
    return build_evidence_gap(
        task_id=TASK,
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"recapture_policy": declaration},
        agent_run_id="consumer-run-1",
        page_capture_id=CAPTURE,
        page={
            "page_capture_id": CAPTURE,
            "page_type": "video_detail",
            "page_state": "stable",
            "surface_kind": "h5",
        },
        missing_artifact_types=("screenshot",),
        reason_code="EVIDENCE_INSUFFICIENT",
        request_expires_at=NOW + timedelta(minutes=10),
    )


def _materialize(kind: str, fallback: str | None = None):
    gap = _gap(kind, fallback)
    request = materialize_evidence_request(
        gap=gap.to_dict(),
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id=TASK,
        rule_id="rule-1",
        rule_code="alioth_1",
        page_capture_id=CAPTURE,
        agent_run_id="consumer-run-1",
    )
    return gap, request


# ---------- 判断：规则声明决定恢复约束 ----------


@pytest.mark.parametrize(
    ("kind", "session", "replay", "equivalence", "grade"),
    [
        (
            "exact_state",
            EvidenceSessionDependency.ACTIVE_SESSION,
            EvidenceReplayPolicy.SAME_SURFACE_ONLY,
            EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
            EvidenceSourceGrade.A1,
        ),
        (
            "equivalent_state",
            EvidenceSessionDependency.ACTIVE_SESSION,
            EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
            EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            EvidenceSourceGrade.B,
        ),
        (
            "fresh_observation",
            EvidenceSessionDependency.SNAPSHOT,
            EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
            EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            EvidenceSourceGrade.C,
        ),
    ],
)
def test_declared_policy_fixes_recovery_constraints(kind, session, replay, equivalence, grade):
    """必须原状态 / 允许等价 / 允许重新观察三档各自锁死会话、重放、等价与最低证据等级。"""
    gap, request = _materialize(kind, "manual_only")

    assert gap.disposition is GapDisposition.RECORDED_ONLY
    assert gap.recapture_fallback is RecapturePolicy.MANUAL
    assert request is not None
    assert request.session_dependency is session
    assert request.replay_policy is replay
    assert request.required_equivalence is equivalence
    assert request.minimum_source_grade is grade
    assert request.side_effect_policy is EvidenceSideEffectPolicy.READ_ONLY
    assert request.target_capture_id == CAPTURE
    # 同一缺口再物化一次得到同一个请求身份：只追加、不重复造请求
    assert _materialize(kind, "manual_only")[1].request_id == request.request_id


def test_manual_policy_records_gap_and_goes_to_human_without_request() -> None:
    """转人工：不生成任何补采请求，缺口按证据不足收口并标成必须人工处理。"""
    gap, request = _materialize("manual_only")

    assert request is None
    assert gap.disposition is GapDisposition.MANUAL_REVIEW_REQUIRED

    outcome = normalize_rule_judge_outcome(
        handoff=_handoff(),
        raw={
            "rule_code": "alioth_1",
            "execution_status": "evidence_insufficient",
            "judge_output": {"evidence_gap": gap.to_dict()},
        },
    )

    assert outcome.evidence_requests == ()
    assert outcome.deferred_decisions == ()
    decision = outcome.decisions[0]
    assert decision.execution_status == "evidence_insufficient"
    recorded = decision.payload["judge_output"]["evidence_gap"]
    assert recorded["recapture_policy"] == "manual"
    assert recorded["disposition"] == "manual_review_required"


# ---------- 校验：等价门禁按声明分档 ----------


def _request(kind: RecapturePolicy):
    constraints = {
        RecapturePolicy.EXACT: (
            EvidenceSessionDependency.ACTIVE_SESSION,
            EvidenceReplayPolicy.SAME_SURFACE_ONLY,
            EvidenceRequiredEquivalence.SAME_SURFACE_CONTINUATION,
            EvidenceSourceGrade.A1,
        ),
        RecapturePolicy.EQUIVALENT: (
            EvidenceSessionDependency.ACTIVE_SESSION,
            EvidenceReplayPolicy.SAME_CONTEXT_ALLOWED,
            EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            EvidenceSourceGrade.B,
        ),
        RecapturePolicy.FRESH: (
            EvidenceSessionDependency.SNAPSHOT,
            EvidenceReplayPolicy.NEW_SESSION_ALLOWED,
            EvidenceRequiredEquivalence.LOGICAL_EQUIVALENT,
            EvidenceSourceGrade.C,
        ),
    }[kind]
    session, replay, equivalence, grade = constraints
    return build_evidence_request(
        work_item_id="work-1",
        attempt_id="attempt-1",
        task_id=TASK,
        rule_id="rule-1",
        rule_code="alioth_1",
        target_capture_id=CAPTURE,
        reason_code="EVIDENCE_INSUFFICIENT",
        evidence_goal="补采视频详情页证据",
        required_artifact_types=("screenshot", "ui_snapshot"),
        session_dependency=session,
        replay_policy=replay,
        required_equivalence=equivalence,
        side_effect_policy=EvidenceSideEffectPolicy.READ_ONLY,
        target_invariants={
            "environment": {
                "surface_kind": "android",
                "deep_link": "app://video/1",
                "lifecycle_phase": "foreground",
            },
            "business": {
                "package_name": "com.example.app",
                "activity": ".Video",
                "route": "app://com.example.app/.Video",
            },
            "semantic": {"page_state": "stable", "overlay": "none"},
            "structure": {
                "screen_id": "screen:video-1",
                "state_version": "v1",
                "structure_signature": "sha256:video",
            },
        },
        minimum_source_grade=grade,
        expires_at=NOW + timedelta(hours=1),
    )


def _restored(*, session_id: str, capture_id: str, screen_id: str = "screen:video-1"):
    return probe_result_to_snapshot(
        task_id=TASK,
        session_id=session_id,
        capture_id=capture_id,
        surface_kind=SurfaceKind.ANDROID,
        package_name="com.example.app",
        activity=".Video",
        screen_id=screen_id,
        structure_signature="sha256:video",
        image_signature="v1",
        deep_link="app://video/1",
        page_state="stable",
        lifecycle_phase="foreground",
        captured_at=NOW + timedelta(seconds=2),
    )


def _assess(request, plan, restored):
    original = snapshot_from_target_invariants(
        request=request,
        session_id=plan.source_session_id,
        surface_kind=SurfaceKind.ANDROID,
        captured_at=NOW,
    )
    return StateEquivalenceGate(clock=lambda: NOW + timedelta(seconds=3)).assess(
        request=request,
        plan=plan,
        restoration_attempt_id="attempt-acceptance",
        original=original,
        restored=restored,
    )


def _plan(request, strategy, *, target_session: str):
    # 重放类策略必须绑定一份旅程执行计划，钉住原表面则不能带
    replay_plan = (
        "execution-plan-acceptance"
        if strategy is not RestorationStrategy.ORIGINAL_SURFACE_PIN
        else None
    )
    return build_restoration_plan(
        request=request,
        source_session_id="session-1",
        target_session_id=target_session,
        surface_kind=SurfaceKind.ANDROID,
        strategy=strategy,
        deadline=NOW + timedelta(minutes=10),
        replay_execution_plan_id=replay_plan,
    )


def test_exact_policy_only_passes_on_the_original_surface() -> None:
    """必须原状态：原会话原表面钉住恢复可过；换会话重放连计划都不允许建。"""
    request = _request(RecapturePolicy.EXACT)
    pin = _plan(request, RestorationStrategy.ORIGINAL_SURFACE_PIN, target_session="session-1")
    assert pin.source_grade is EvidenceSourceGrade.A1

    passed = _assess(request, pin, _restored(session_id="session-1", capture_id="capture-pin"))
    assert passed.passed is True

    with pytest.raises(RestorationValidationError):
        _plan(request, RestorationStrategy.NEW_SESSION_REPLAY, target_session="session-2")


def test_equivalent_policy_accepts_logical_equivalence_but_not_a_different_object() -> None:
    """允许等价：同上下文新会话按业务与结构不变量过门禁；对象变了（screen 不同）就不过。"""
    request = _request(RecapturePolicy.EQUIVALENT)
    plan = _plan(request, RestorationStrategy.SAME_APP_SESSION, target_session="session-1")
    assert plan.source_grade is EvidenceSourceGrade.B

    same_object = _assess(
        request, plan, _restored(session_id="session-1", capture_id="capture-equivalent")
    )
    assert same_object.passed is True

    other_object = _assess(
        request,
        plan,
        _restored(session_id="session-1", capture_id="capture-other", screen_id="screen:video-9"),
    )
    assert other_object.passed is False
    assert any(
        not item.matched and item.key == "screen_id" for item in other_object.comparisons
    )


def test_fresh_policy_allows_a_new_session_observation() -> None:
    """允许重新观察：新会话重放（C 级）也能过门禁，只要业务对象与结构对得上。"""
    request = _request(RecapturePolicy.FRESH)
    plan = _plan(request, RestorationStrategy.NEW_SESSION_REPLAY, target_session="session-2")
    assert plan.source_grade is EvidenceSourceGrade.C

    assessment = _assess(
        request, plan, _restored(session_id="session-2", capture_id="capture-fresh")
    )
    assert assessment.passed is True


def test_exact_policy_rejects_lower_grade_even_when_invariants_match() -> None:
    """必须原状态的最低等级是 A1：B 级恢复即使字段全对也不能当原状态用。"""
    request = _request(RecapturePolicy.EXACT)
    try:
        plan = _plan(request, RestorationStrategy.SAME_APP_SESSION, target_session="session-1")
    except RestorationValidationError:
        return  # 计划层就拒绝了，同样满足「不能用等价替代原状态」
    assessment = _assess(request, plan, _restored(session_id="session-1", capture_id="capture-b"))
    assert assessment.passed is False


# ---------- 追加：补采证明有稳定身份，只追加不覆盖 ----------


def test_supplement_identity_is_stable_and_append_only() -> None:
    request = _request(RecapturePolicy.EQUIVALENT)
    first = build_evidence_supplement(
        task_id=TASK,
        request_id=request.request_id,
        manifest_id="manifest-1",
        probe_work_id="probe-1",
        restoration_attempt_id="attempt-1",
        equivalence_assessment_id="assessment-1",
        source_grade=EvidenceSourceGrade.B,
        created_at=NOW,
    )
    again = build_evidence_supplement(
        task_id=TASK,
        request_id=request.request_id,
        manifest_id="manifest-1",
        probe_work_id="probe-1",
        restoration_attempt_id="attempt-1",
        equivalence_assessment_id="assessment-1",
        source_grade=EvidenceSourceGrade.B,
        created_at=NOW + timedelta(minutes=1),
    )
    second_manifest = build_evidence_supplement(
        task_id=TASK,
        request_id=request.request_id,
        manifest_id="manifest-2",
        probe_work_id="probe-2",
        restoration_attempt_id="attempt-2",
        equivalence_assessment_id="assessment-2",
        source_grade=EvidenceSourceGrade.B,
        created_at=NOW + timedelta(minutes=2),
    )

    # 同一份证明再算一次是同一个身份（幂等），换一份证明是新身份（追加），旧的不被改写
    assert again.supplement_id == first.supplement_id
    assert second_manifest.supplement_id != first.supplement_id
    assert first.manifest_id == "manifest-1"
    with pytest.raises((AttributeError, TypeError)):
        first.manifest_id = "manifest-9"  # type: ignore[misc]
