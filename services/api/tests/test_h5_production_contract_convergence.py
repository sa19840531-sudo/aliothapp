"""170654 批恢复与目标裁剪的生产合同回归，保留不安全动作反例。"""

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from services.api.app.application.evidence_exit_gate import trusted_h5_browser_recovery_identity
from services.api.app.application.h5_profile_payment_navigation import (
    H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY,
    h5_profile_payment_snapshot_selector,
)
from services.api.app.application.h5_viewport import h5_viewport_parameters
from services.api.app.infrastructure.browser import page_capture
from services.api.tests.test_capture_coordinator import _recovery_intent


async def test_actual_recovery_composition_builds_authorized_intent():
    from services.api.app.application.h5_exploration_recovery import (
        H5FailureStage,
        H5RecoveryContext,
        build_h5_recovery_session_id,
    )
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
    from services.api.tests.test_h5_exploration_turn import _work
    from services.api.tests.test_h5_runtime_composition import _runtime_module

    work = _work()
    context = H5RecoveryContext(
        task_id=work.task_id,
        agent_run_id="run-h5",
        session_id=work.session_id,
        stage=H5FailureStage.ACTION,
        last_safe_location=work.frontier.observation.location,
        action_execution_id="lost-action",
        frontier_snapshot=H5TaskFrontierSnapshot.create(max_pages=3),
        recovery_attempt_count=0,
        max_recovery_attempts=2,
        failed_work=work,
    )
    checked = []

    async def coordinate(command):
        checked.append(command.intent)
        assert trusted_h5_browser_recovery_identity(command.intent) is not None
        return SimpleNamespace()

    module = _runtime_module()
    runtime = module._H5BrowserLossRuntime(
        capture_runtime=SimpleNamespace(
            h5_exploration_coordinator=SimpleNamespace(coordinate=coordinate)
        ),
        detector=SimpleNamespace(),
        workflow=SimpleNamespace(),
        cancellation=module.H5CancellationCheckpoint(),
        now=lambda: work.frontier.observation.observed_at,
    )
    runtime._target_session_id = build_h5_recovery_session_id(
        agent_run_id="run-h5",
        work_id=work.work_id,
        stage=context.stage,
        recovery_attempt_count=1,
    )
    await runtime.navigate(context=context, destination_url=context.last_safe_location)
    assert len(checked) == 1


def test_production_recovery_viewport_contract_is_trusted():
    intent = _recovery_intent()
    intent = replace(intent, parameters={**intent.parameters, **h5_viewport_parameters()})
    assert trusted_h5_browser_recovery_identity(intent) is not None


@pytest.mark.parametrize(
    "viewport",
    [
        {"viewport_width": 390},
        {"viewport_width": True, "viewport_height": 844},
        {"viewport_width": 391, "viewport_height": 844},
        {"viewport_width": 390, "viewport_height": 844, "unsafe": True},
    ],
)
def test_recovery_rejects_partial_forged_or_extra_viewport(viewport):
    intent = _recovery_intent()
    intent = replace(intent, parameters={**intent.parameters, **viewport})
    assert trusted_h5_browser_recovery_identity(intent) is None


@pytest.mark.parametrize("stage", ["serialization", "logical_surface"])
@pytest.mark.parametrize("hit", [True, False, None])
def test_yc_payment_target_survives_bounding_without_granting_click(stage, hit, monkeypatch):
    monkeypatch.setattr(page_capture, "MAX_UI_SNAPSHOT_BYTES", 4000)
    target = {
        "selector": ".vip-section .button",
        "tag": "div",
        "text": "立即开通",
        "class_tokens": ["button"],
        "parent_class_tokens": ["vip-section"],
        "ancestor_class_tokens": ["header"],
        "hit_test": {"center_hits_target": hit},
    }
    snapshot = {
        "h5_modal_census_selectors": [],
        "elements": [
            *[
                {"selector": f"#nav{i}", "tag": "a", "text": "栏目" * 300, "interactive": True}
                for i in range(12)
            ],
            target,
        ],
    }
    if stage == "serialization":
        bounded = page_capture._sanitize_and_bound_ui_snapshot(snapshot)
    else:
        bounded = deepcopy(snapshot)
        page_capture._bound_snapshot_after_logical_surface(bounded)
    assert target["selector"] in {element["selector"] for element in bounded["elements"]}
    assert page_capture._json_size_bytes(bounded) <= 4000
    assert bool(
        h5_profile_payment_snapshot_selector(
            target,
            capability=H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY,
        )
    ) is (hit is True)
