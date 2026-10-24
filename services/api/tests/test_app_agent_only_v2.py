"""2.0 全产品通用 Agent：旧策略、旧候选与旧动作均不能重新启用固定脚本。"""

from dataclasses import replace

import pytest

from services.api.app.agents.explorer import MafExplorerAgent
from services.api.app.application.app_interaction_runtime import (
    AppCandidateResolver,
    AppInteractionRuntimeError,
)
from services.api.app.application.app_product_orchestration_policy import (
    product_fixed_script_enabled,
    profile_allows_product_orchestrator,
    try_product_script_override,
    uses_agent_generic_contract,
)
from services.api.app.application.app_ui_tree_features import _PACKAGE_PLAYBOOK
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionRef
from services.api.app.infrastructure.device.android_surface_driver import AndroidAdbSurfaceDriver
from services.api.tests.test_app_interaction_runtime import _candidate, _request
from services.api.tests.test_app_three_contract_driver import NoModel, candidate, work_for


@pytest.mark.parametrize("package", [*_PACKAGE_PLAYBOOK, "com.unknown.app"])
@pytest.mark.parametrize("method", ["decide_next", "decide_deterministic"])
async def test_all_products_choose_current_visual_candidate(package, method):
    watch = candidate("立即看", expected_state_hint="video_detail")
    legacy = replace(watch, candidate_id="mama-script:1:fixed", exploration_value=100)
    work = work_for(package=package, candidates=(legacy, watch))
    result = await getattr(MafExplorerAgent(agent=NoModel()), method)(work)
    assert result.parameters["candidate_id"] == watch.candidate_id
    assert not any(key.endswith("_script") for key in result.parameters)


@pytest.mark.parametrize(
    "mode", ["", "agent", "profile", "legacy_script", "script", "airtest", "1"]
)
def test_old_configuration_cannot_enable_scripts(monkeypatch, mode):
    monkeypatch.setenv("ALIOTH_APP_EXECUTION_MODE", mode)
    assert product_fixed_script_enabled() is False
    for package in _PACKAGE_PLAYBOOK:
        assert profile_allows_product_orchestrator(package) is False
        assert uses_agent_generic_contract(package) is True
        assert try_product_script_override(work_for(package=package)) is None


@pytest.mark.parametrize("parameters", [
    {"candidate_id": "mama-script:0:001"},
    {"candidate_id": "hjll-script:step:1"},
    {"product_script_orchestrated": True},
    {"pc51_script": True},
    {"mama_recording_script": True},
    {"hjll_orchestrated": True},
    {"candidate_id": "hjll-orchestrated-top-sample:home"},
])
async def test_legacy_action_blocked_before_resolution_or_device_access(parameters):
    request = _request(_candidate(), parameters=parameters)
    resolver = AppCandidateResolver(visual_observations=object(), captures=object())
    with pytest.raises(AppInteractionRuntimeError) as caught:
        await resolver.resolve(request)
    assert caught.value.error_code == "LEGACY_PRODUCT_SCRIPT_DISABLED"
    driver = AndroidAdbSurfaceDriver(backend="cloud_phone")
    session = SurfaceSessionRef(
        task_id=request.intent.task_id, session_id=request.intent.session_id,
        surface_kind=SurfaceKind.ANDROID, primary_surface_ref="test:offline",
    )
    result = await driver.execute(
        session=session, intent=request.intent, action_execution_id="blocked-test",
    )
    assert result.succeeded is False
    assert result.error_code == "LEGACY_PRODUCT_SCRIPT_DISABLED"
