"""F04／F06／F12：能力事实无坐标、课纲严格推进、App Skill 定向挂载。"""

from __future__ import annotations

from services.api.app.agents.bootstrap import main_agent_tool_names
from services.api.app.agents.tools import app_device_tools  # noqa: F401
from services.api.app.application.app_product_orchestration_policy import (
    product_fixed_script_enabled,
)
from services.api.app.application.app_schedule_gap_closure import (
    advance_journey_curriculum,
    ensure_journey_curriculum,
)
from services.api.app.application.app_workflow_router import WorkflowRoute
from services.api.app.application.workflow_skill_routing import skill_sub_dirs_for_route
from services.api.app.domain.app_product.profiles import (
    build_app_product_capability_facts,
    load_profile,
)


def test_default_disables_product_orchestrator(monkeypatch) -> None:
    """2.0 默认通用 Agent，旧环境变量不能重新启用固定脚本。"""

    monkeypatch.delenv("ALIOTH_APP_EXECUTION_MODE", raising=False)
    assert product_fixed_script_enabled() is False
    monkeypatch.setenv("ALIOTH_APP_EXECUTION_MODE", "agent")
    assert product_fixed_script_enabled() is False


def test_f04_capability_facts_forbid_coordinates() -> None:
    facts = build_app_product_capability_facts("com.dzu4.app")
    assert facts["ok"] is True
    assert facts["profile_present"] is True
    assert facts["grants_absolute_coordinates"] is False
    for blocked in ("taps", "key_taps", "cloud_back_tap", "resolution", "gesture_y_max"):
        assert blocked not in facts
    unknown = build_app_product_capability_facts("com.example.unknown.app")
    assert unknown["ok"] is False
    assert unknown["profile_present"] is False
    assert unknown["grants_absolute_coordinates"] is False


def test_f04_profile_raw_taps_still_loadable_for_legacy_only() -> None:
    profile = load_profile("com.dzu4.app")
    assert profile is not None
    assert "me_tab" in profile.key_taps
    summary = profile.to_agent_summary()
    assert "me_tab" not in str(summary.get("semantic_recovery_hints") or [])
    assert "key_taps" not in summary


def test_f06_requires_capture_and_full_target_on_first_pending() -> None:
    coverage = ensure_journey_curriculum(
        {},
        curriculum=[
            {
                "definition_id": "a",
                "step_id": "s1",
                "semantic_target": "开通会员入口",
                "status": "pending",
                "capture_id": "",
            },
            {
                "definition_id": "a",
                "step_id": "s2",
                "semantic_target": "会员档位",
                "status": "pending",
                "capture_id": "",
            },
        ],
    )
    assert (
        advance_journey_curriculum(
            coverage, semantic_target="会员", capture_id="c1"
        )["app_journey_curriculum"][0]["status"]
        == "pending"
    )
    assert (
        advance_journey_curriculum(
            coverage, semantic_target="开通会员入口", capture_id=""
        )["app_journey_curriculum"][0]["status"]
        == "pending"
    )
    assert (
        advance_journey_curriculum(
            coverage, semantic_target="会员档位", capture_id="c2"
        )["app_journey_curriculum"][1]["status"]
        == "pending"
    )
    done = advance_journey_curriculum(
        coverage, semantic_target="点击开通会员入口", capture_id="c3"
    )
    assert done["app_journey_curriculum"][0]["status"] == "done"
    assert done["app_journey_curriculum"][0]["capture_id"] == "c3"


def test_f12_app_route_excludes_phase_and_script_rhythm() -> None:
    dirs = skill_sub_dirs_for_route(WorkflowRoute.APP)
    assert dirs == (
        "app-workflow",
        "app-product-capability",
        "green-content-safety",
        "green-process-evidence",
    )
    assert "phase" not in dirs
    assert "app-script-rhythm" not in dirs


def test_f12_main_still_excludes_direct_adb() -> None:
    main_names = main_agent_tool_names()
    for name in (
        "app_screenshot_tool",
        "app_tap_tool",
        "app_cloud_back_tool",
        "app_cloud_back_twice_tool",
        "app_tap_from_profile_tool",
    ):
        assert name not in main_names
    assert "load_app_product_profile_tool" in main_names
