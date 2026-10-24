"""Web 交互计划、滚动重绑定与四层结果合同。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.web_exploration_workflow import web_capture_profile
from services.api.app.application.web_interaction_precision import (
    WebInteractionEvidenceMode,
    WebInteractionPlan,
    WebInteractionPostcondition,
    WebTargetBindingStatus,
    WebTargetObservation,
    assess_web_interaction_outcome,
    assess_web_target_binding,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


def _intent() -> ActionIntent:
    return ActionIntent(
        task_id="task-1",
        session_id="web:run-1",
        intent_id="web-click:1",
        idempotency_key="web-click:1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-before",
        action_type=InteractionOperation.CLICK,
        semantic_target="打开详情",
        purpose="验证详情到达",
        parameters={},
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=web_capture_profile("web-interaction-precision.v1"),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def _plan(*, max_scroll_steps: int = 3) -> WebInteractionPlan:
    return WebInteractionPlan(
        intent=_intent(),
        source_capture_id="capture-before",
        source_epoch="epoch-1",
        target_ref="https://example.test/detail/1",
        max_scroll_steps=max_scroll_steps,
        deadline_seconds=30.0,
        postcondition=WebInteractionPostcondition.DETAIL_ARRIVAL,
    )


def test_offscreen_target_requests_one_bounded_scroll_step() -> None:
    result = assess_web_target_binding(
        _plan(max_scroll_steps=2),
        WebTargetObservation(
            capture_id="capture-after-scroll-1",
            source_epoch="epoch-2",
            matching_target_count=1,
            target_visible=False,
            target_enabled=True,
            target_hit_test_passed=False,
            scroll_offset=720.0,
            previous_scroll_offset=0.0,
            at_scroll_boundary=False,
        ),
        completed_scroll_steps=1,
    )

    assert result.status is WebTargetBindingStatus.SCROLL_REQUIRED
    assert result.rebound_capture_id == "capture-after-scroll-1"


@pytest.mark.parametrize(
    ("count", "status"),
    (
        (0, WebTargetBindingStatus.TARGET_MISSING),
        (2, WebTargetBindingStatus.TARGET_AMBIGUOUS),
    ),
)
def test_scroll_rebind_rejects_missing_or_ambiguous_target(count, status) -> None:
    result = assess_web_target_binding(
        _plan(),
        WebTargetObservation(
            capture_id="capture-rebound",
            source_epoch="epoch-2",
            matching_target_count=count,
            target_visible=False,
            target_enabled=False,
            target_hit_test_passed=False,
            scroll_offset=720.0,
            previous_scroll_offset=0.0,
            at_scroll_boundary=False,
        ),
        completed_scroll_steps=1,
    )

    assert result.status is status
    assert result.click_authorized is False


def test_visible_target_requires_enabled_and_hit_test_after_rebind() -> None:
    blocked = assess_web_target_binding(
        _plan(),
        WebTargetObservation(
            capture_id="capture-rebound",
            source_epoch="epoch-2",
            matching_target_count=1,
            target_visible=True,
            target_enabled=True,
            target_hit_test_passed=False,
            scroll_offset=720.0,
            previous_scroll_offset=0.0,
            at_scroll_boundary=False,
        ),
        completed_scroll_steps=1,
    )

    assert blocked.status is WebTargetBindingStatus.TARGET_BLOCKED
    assert blocked.click_authorized is False


def test_scroll_budget_or_no_movement_terminates_without_click() -> None:
    budget = assess_web_target_binding(
        _plan(max_scroll_steps=1),
        WebTargetObservation(
            capture_id="capture-rebound",
            source_epoch="epoch-2",
            matching_target_count=1,
            target_visible=False,
            target_enabled=True,
            target_hit_test_passed=False,
            scroll_offset=720.0,
            previous_scroll_offset=0.0,
            at_scroll_boundary=False,
        ),
        completed_scroll_steps=1,
    )
    stationary = assess_web_target_binding(
        _plan(max_scroll_steps=3),
        WebTargetObservation(
            capture_id="capture-rebound",
            source_epoch="epoch-2",
            matching_target_count=1,
            target_visible=False,
            target_enabled=True,
            target_hit_test_passed=False,
            scroll_offset=720.0,
            previous_scroll_offset=720.0,
            at_scroll_boundary=False,
        ),
        completed_scroll_steps=1,
    )

    assert budget.status is WebTargetBindingStatus.SCROLL_LIMIT_REACHED
    assert stationary.status is WebTargetBindingStatus.SCROLL_STALLED


def test_command_success_without_effect_or_postcondition_is_not_arrival() -> None:
    outcome = assess_web_interaction_outcome(
        plan=_plan(),
        command_accepted=True,
        before_url="https://example.test/list",
        after_url="https://example.test/list",
        before_content_identity="list-v1",
        after_content_identity="list-v1",
        postcondition_satisfied=False,
        after_capture_id="capture-after",
    )

    assert outcome.command_accepted is True
    assert outcome.effect_observed is False
    assert outcome.postcondition_satisfied is False
    assert outcome.transition_arrived is False
    assert outcome.failure_reason == "effect_not_observed"


def test_same_url_detail_content_transition_can_prove_arrival() -> None:
    outcome = assess_web_interaction_outcome(
        plan=_plan(),
        command_accepted=True,
        before_url="https://example.test/list",
        after_url="https://example.test/list",
        before_content_identity="list-v1",
        after_content_identity="detail-v2",
        postcondition_satisfied=True,
        after_capture_id="capture-after",
        readiness_accepted=True,
        identity_verified=True,
        business_layer_visible=True,
    )

    assert outcome.effect_observed is True
    assert outcome.transition_arrived is True
    assert outcome.failure_reason is None


def test_diagnostic_click_never_becomes_trusted_arrival() -> None:
    outcome = assess_web_interaction_outcome(
        plan=_plan(),
        command_accepted=True,
        before_url="https://example.test/list",
        after_url="https://example.test/detail/1",
        before_content_identity="list-v1",
        after_content_identity="detail-v2",
        postcondition_satisfied=True,
        after_capture_id="capture-after",
        evidence_mode=WebInteractionEvidenceMode.DIAGNOSTIC,
    )

    assert outcome.effect_observed is True
    assert outcome.postcondition_satisfied is True
    assert outcome.transition_arrived is False
    assert outcome.failure_reason == "diagnostic_evidence_not_trusted"


def test_missing_trust_facts_never_proves_arrival() -> None:
    outcome = assess_web_interaction_outcome(
        plan=_plan(),
        command_accepted=True,
        before_url="https://example.test/list",
        after_url="https://example.test/detail/1",
        before_content_identity="list",
        after_content_identity="detail",
        postcondition_satisfied=True,
        after_capture_id="after",
    )
    assert outcome.command_accepted is True
    assert outcome.effect_observed is True
    assert outcome.postcondition_satisfied is True
    assert outcome.transition_arrived is False


@pytest.mark.parametrize(
    "missing", ["readiness_accepted", "identity_verified", "business_layer_visible"]
)
def test_each_trust_fact_is_required_independently(missing) -> None:
    facts = dict(readiness_accepted=True, identity_verified=True, business_layer_visible=True)
    facts[missing] = False
    outcome = assess_web_interaction_outcome(
        plan=_plan(),
        command_accepted=True,
        before_url="https://example.test/list",
        after_url="https://example.test/detail/1",
        before_content_identity="list",
        after_content_identity="detail",
        postcondition_satisfied=True,
        after_capture_id="after",
        **facts,
    )
    assert outcome.effect_observed is True
    assert outcome.postcondition_satisfied is True
    assert outcome.transition_arrived is False


def test_scroll_can_arrive_without_url_or_content_change() -> None:
    from dataclasses import replace

    plan = replace(_plan(), postcondition=WebInteractionPostcondition.TARGET_VISIBLE)
    outcome = assess_web_interaction_outcome(
        plan=plan,
        command_accepted=True,
        before_url="https://example.test/list",
        after_url="https://example.test/list",
        before_content_identity="list",
        after_content_identity="list",
        effect_observed=True,
        postcondition_satisfied=True,
        after_capture_id="after",
        readiness_accepted=True,
        identity_verified=True,
        business_layer_visible=True,
    )
    assert outcome.transition_arrived is True
