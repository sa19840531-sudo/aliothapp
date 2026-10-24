"""动态探索不启用产品固定点；每次关闭只依据当前动作前帧。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from services.api.app.agents.explorer.maf_agent import (
    _deterministic_app_coverage_decision,
    _is_hjll_airtest_work,
    _popup_close_candidate_for_obstruction,
    _synthetic_hjll_airtest_popup_close_candidate,
    _synthetic_pixel_popup_close_candidate,
)
from services.api.app.application.app_interaction_runtime import AppInteractionRuntimeError
from services.api.app.application.app_synthetic_touch_contracts import popup_close_corner_contract
from services.api.app.domain.exploration import AppExplorationGoal, NormalizedBounds
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionResultStatus,
    SurfaceKind,
)
from services.api.tests.test_app_interaction_runtime import (
    BEFORE_CAPTURE_ID,
    _analysis,
    _before_visual_analysis,
    _candidate,
    _executor,
    _request,
)
from services.api.tests.test_maf_explorer_agent import _work


def _popup_work(*, dynamic: bool, bounds: dict[str, float] | None = None):
    facts: dict[str, object] = {"popup_visible": True, "dialog_kind": "ad_popup"}
    if bounds is not None:
        facts["pixel_cross_close_calibration"] = {
            "normalized_bounds": bounds,
            "close_kind": "bottom_circular_x",
        }
    return _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="popup",
        observation_metadata={
            "surface_facts": facts,
            "package_name": "com.hj.ttthree",
            "app_runtime_policy": {
                "exploration_mode": "dynamic_frontier" if dynamic else "explicit_playbook",
            },
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.hj.ttthree",),
        ),
        allowed_operations=(InteractionOperation.CLICK, InteractionOperation.OBSERVE),
    )


def test_dynamic_hjll_package_does_not_enable_fixed_airtest_workflow() -> None:
    assert _is_hjll_airtest_work(_popup_work(dynamic=True)) is False
    assert _is_hjll_airtest_work(_popup_work(dynamic=False)) is True


def test_dynamic_hjll_allowlist_does_not_enable_fixed_airtest_workflow() -> None:
    work = _popup_work(dynamic=True)
    metadata = {**work.frontier.observation.metadata, "package_name": "com.external.overlay"}
    work = replace(work, frontier=replace(work.frontier, observation=replace(
        work.frontier.observation, metadata=metadata,
    )))
    assert _is_hjll_airtest_work(work) is False


def test_dynamic_popup_without_current_x_never_gets_airtest_fixed_fallback() -> None:
    work = _popup_work(dynamic=True)
    assert _synthetic_hjll_airtest_popup_close_candidate(work=work) is None
    assert _popup_close_candidate_for_obstruction(work=work, eligible=()) is None
    decision = _deterministic_app_coverage_decision(work=work, eligible=())
    assert not isinstance(decision, ActionIntent) or (
        decision.action_type is not InteractionOperation.CLICK
        and decision.parameters.get("app_popup_close_corner") is None
    )


def test_dynamic_pixel_x_uses_actual_bounds_outside_legacy_airtest_sweet_spot() -> None:
    bounds = {"left": 0.75, "top": 0.68, "right": 0.80, "bottom": 0.73}
    dynamic = _synthetic_pixel_popup_close_candidate(work=_popup_work(dynamic=True, bounds=bounds))
    assert dynamic is not None
    assert dynamic.metadata["normalized_bounds"] == bounds
    assert _synthetic_pixel_popup_close_candidate(
        work=_popup_work(dynamic=False, bounds=bounds),
    ) is None


def test_dynamic_current_pixel_x_is_used_without_script_orchestration() -> None:
    bounds = {"left": 0.48465277777777777, "top": 0.7022697368421053,
              "right": 0.5167361111111111, "bottom": 0.7174671052631579}
    decision = _deterministic_app_coverage_decision(
        work=_popup_work(dynamic=True, bounds=bounds), eligible=(),
    )
    assert isinstance(decision, ActionIntent)
    assert decision.parameters["app_popup_close_corner"]["normalized_bounds"] == bounds
    assert decision.parameters.get("hjll_orchestrated") is not True


def test_dynamic_same_x_without_effect_reobserves_instead_of_fixed_retry() -> None:
    work = _popup_work(dynamic=True, bounds={
        "left": 0.46, "top": 0.69, "right": 0.54, "bottom": 0.73,
    })
    metadata = {
        **work.frontier.observation.metadata,
        "surface_phase_same_popup_attempt_count": 2,
        "dismiss_noop_streak": 2,
    }
    work = replace(work, frontier=replace(work.frontier, observation=replace(
        work.frontier.observation, metadata=metadata,
    )))
    decision = _deterministic_app_coverage_decision(work=work, eligible=())
    assert isinstance(decision, ActionIntent)
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.parameters.get("app_popup_close_corner") is None


def test_legacy_airtest_fixed_point_still_available_without_dynamic_policy() -> None:
    work = _popup_work(dynamic=False)
    metadata = dict(work.frontier.observation.metadata)
    metadata.pop("app_runtime_policy")
    work = replace(work, frontier=replace(work.frontier, observation=replace(
        work.frontier.observation, metadata=metadata,
    )))
    candidate = _synthetic_hjll_airtest_popup_close_candidate(work=work)
    assert candidate is not None
    assert candidate.metadata["normalized_bounds"] == {
        "left": 0.45, "top": 0.775, "right": 0.55, "bottom": 0.8250000000000001,
    }


def _close_runtime(*, current_x: bool, script: bool = False):
    source_bounds = NormalizedBounds(0.46, 0.69, 0.54, 0.73)
    source = _candidate(
        semantic_name="关闭广告弹窗", purpose="关闭当前广告弹层",
        expected_state_hint="close_popup_x", control_role="dismiss",
        normalized_bounds=source_bounds,
    )
    current_bounds = NormalizedBounds(0.45, 0.70, 0.53, 0.74)
    current = _before_visual_analysis(
        source, image_signature="sha256:" + "b" * 64,
        visual_page_type="home", page_state="popup", normalized_bounds=current_bounds,
        purpose="关闭当前广告弹层；当前截图唯一孤立 X 像素结构校准",
        surface_facts={
            "popup_visible": True,
            **({"pixel_cross_close_calibration": {
                "normalized_bounds": current_bounds.to_dict(),
                "close_kind": "bottom_circular_x",
            }} if current_x else {}),
        },
        candidates=None if current_x else (),
    )
    executor, driver, _ = _executor(
        candidate=source,
        analysis=_analysis(source, visual_page_type="home", page_state="popup",
                           surface_facts={"popup_visible": True}),
        before_analysis=current,
        before_signature=current.image_signature,
    )
    request = _request(source, parameters={
        "app_popup_close_corner": popup_close_corner_contract(
            bounds=source_bounds, purpose=source.purpose, confidence=0.95,
        ),
        **({"hjll_script": True} if script else {}),
    })
    return executor, driver, request, current_bounds


async def test_current_frame_without_x_cannot_rebind_old_popup_contract() -> None:
    executor, driver, request, _ = _close_runtime(current_x=False)
    result = await executor.execute(request)
    assert result.status is InteractionResultStatus.FAILED
    assert result.retryable is True
    assert driver.execute_count == 0


async def test_current_frame_true_x_rebinds_to_its_own_point() -> None:
    executor, driver, request, bounds = _close_runtime(current_x=True)
    result = await executor.execute(request)
    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1
    target = driver.last_intent.parameters["resolved_target"]
    assert driver.last_intent.source_capture_id == BEFORE_CAPTURE_ID
    assert target["x"] == round(bounds.center[0] * 1080)
    assert target["y"] == round(bounds.center[1] * 1920)


async def test_explicit_script_is_rejected_when_product_scripts_disabled() -> None:
    """aliothapp2.0 已停用产品固定脚本：带 hjll_script 的关层不得执行。"""
    executor, driver, request, _ = _close_runtime(current_x=False, script=True)
    with pytest.raises(AppInteractionRuntimeError, match="已停用产品固定脚本"):
        await executor.execute(request)
    assert driver.execute_count == 0
