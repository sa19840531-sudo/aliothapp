"""MaMa 一步一步執行 mama.py。每步之後由系統截圖。"""

import asyncio
from pathlib import Path

import pytest

from services.api.app.application.app_mama_script import (
    MAMA_SCRIPT_ATTEMPTED_KEY,
    MAMA_SCRIPT_DONE_KEY,
    MAMA_SCRIPT_INDEX_KEY,
    MAMA_SCRIPT_REASON_CODE,
    apply_mama_script_progress,
    run_mama_recording_script,
    script_mama_override,
    serial_from_android_driver,
    verify_mama_recording_shots,
)
from services.api.app.application.app_ui_tree_features import (
    resolve_product_orchestrator_id,
)
from services.api.app.domain.exploration import AppExplorationGoal
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind
from services.api.app.infrastructure.device.android_surface_driver import (
    AndroidAdbSurfaceDriver,
)
from services.api.tests.recording_script_fixture import recording_script_fixture
from services.api.tests.test_maf_explorer_agent import _work

recorded_steps = recording_script_fixture("MAMA_SCRIPT", "mama.py")


def _mama_work(**metadata: object):
    return _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="ready",
        observation_metadata={
            "package_name": "com.dzu4.app",
            **metadata,
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.dzu4.app",),
        ),
        allowed_operations=(InteractionOperation.OBSERVE, InteractionOperation.CLICK),
    )


def test_mama_package_uses_its_own_orchestrator() -> None:
    assert resolve_product_orchestrator_id(package_name="com.dzu4.app") == "mama"
    assert resolve_product_orchestrator_id(package_name="com.huangguo.drama") is None


def test_mama_uses_workspace_copy_when_airtest_path_is_absent(monkeypatch) -> None:
    monkeypatch.delenv("MAMA_SCRIPT", raising=False)
    monkeypatch.setattr(
        "services.api.app.application.app_mama_script.DEFAULT_MAMA_SCRIPT",
        Path("C:/missing/mama.py"),
    )

    decision = script_mama_override(_mama_work())

    assert decision is not None
    assert decision.parameters["mama_script"] is True
    assert decision.parameters["product_script_orchestrated"] is True
    assert "mama.py" in decision.purpose


def test_mama_agent_runs_one_step_then_the_system_screenshots() -> None:
    decision = script_mama_override(_mama_work())

    assert decision is not None
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.parameters["mama_script"] is True
    assert decision.parameters["require_screenshot_evidence"] is True
    assert decision.parameters["product_script_orchestrated"] is True
    assert "mama_execute_recording_script" not in decision.parameters
    assert str(decision.parameters["candidate_id"]).startswith("mama-script:0:")
    assert "留證截圖" in decision.purpose
    assert "normalized_bounds" not in decision.parameters


def test_other_packages_stay_on_the_generic_agent() -> None:
    other = _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        observation_metadata={"package_name": "com.huangguo.drama"},
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.huangguo.drama",),
        ),
    )
    assert script_mama_override(other) is None


def test_finished_script_is_recorded_and_not_rerun() -> None:
    done = script_mama_override(_mama_work(**{MAMA_SCRIPT_DONE_KEY: True}))
    assert done is not None
    assert done.reason_code == MAMA_SCRIPT_REASON_CODE
    again = script_mama_override(_mama_work(**{MAMA_SCRIPT_ATTEMPTED_KEY: True}))
    assert again is not None
    assert again.parameters["require_screenshot_evidence"] is True


def test_mama_old_script_cannot_override_generic_agent() -> None:
    """2.0 保留历史脚本单元，但生产调度不再选择这些动作。"""
    from services.api.app.application.app_product_orchestration_policy import (
        try_product_script_override,
    )
    assert try_product_script_override(_mama_work()) is None


def test_mama_click_success_counts_as_completed_action_for_coverage() -> None:
    """點擊成功即使觀察合同未 completed，也要記入 completed_action 以推進索引。"""

    from services.api.app.application.app_screenshot_exploration import (
        _product_script_completed_action_for_coverage,
    )
    from services.api.app.domain.exploration import CandidateAction
    from services.api.app.domain.interaction import InteractionRiskLevel

    click = script_mama_override(
        _mama_work(**{MAMA_SCRIPT_INDEX_KEY: 2}),
    )
    assert click is not None
    assert click.action_type is InteractionOperation.CLICK
    candidate_id = str(click.parameters["candidate_id"])
    assert candidate_id.startswith("mama-script:2:")
    selected = CandidateAction(
        candidate_id=candidate_id,
        semantic_target=str(click.semantic_target or "點"),
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": click.purpose,
            "product_script_orchestrated": True,
            "mama_script": True,
        },
    )
    assert (
        _product_script_completed_action_for_coverage(
            selected=selected,
            action_succeeded=True,
            observation_completed=False,
            observation_reason=None,
        )
        is selected
    )
    assert (
        _product_script_completed_action_for_coverage(
            selected=selected,
            action_succeeded=False,
            observation_completed=False,
            observation_reason=None,
        )
        is None
    )


def test_progress_advances_one_step_and_ignores_other_taps() -> None:
    decision = script_mama_override(_mama_work())
    result: dict[str, object] = {}
    apply_mama_script_progress(
        result,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": decision.parameters["candidate_id"]}},
        )(),
        action_succeeded=True,
    )
    assert result[MAMA_SCRIPT_INDEX_KEY] == 1
    assert result[MAMA_SCRIPT_DONE_KEY] is False
    failed: dict[str, object] = {}
    apply_mama_script_progress(
        failed,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": decision.parameters["candidate_id"]}},
        )(),
        action_succeeded=False,
    )
    assert failed[MAMA_SCRIPT_INDEX_KEY] == 0
    untouched: dict[str, object] = {}
    apply_mama_script_progress(
        untouched,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": "generic-tap"}},
        )(),
        action_succeeded=True,
    )
    assert untouched[MAMA_SCRIPT_DONE_KEY] is False
    assert untouched[MAMA_SCRIPT_INDEX_KEY] == 0


def test_runner_uses_the_script_file_and_only_verifies_shots(tmp_path: Path) -> None:
    script = tmp_path / "mama.py"
    script.write_text("print('recording')\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["env_serial"] = kwargs["env"]["MAMA_SERIAL"]
        shots = Path(kwargs["cwd"]) / "shots"
        shots.mkdir()
        for name in ("01", "22"):
            (shots / f"{name}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 16)
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    recording = run_mama_recording_script(
        serial="172.104.172.119:30660",
        script_path=script,
        runner=runner,
    )
    assert seen["command"][-2:] == [str(script), "172.104.172.119:30660"]
    assert seen["env_serial"] == "172.104.172.119:30660"
    assert recording["ok"] is True
    verification = recording["verification"]
    assert verification["verified"] is True
    assert verification["point_selection"] == "recording_script"
    assert verification["shot_names"] == ["01", "22"]
    assert verification["missing_required"] == []


def test_missing_required_shot_stays_a_verification_result() -> None:
    report = verify_mama_recording_shots(({"name": "01"},))
    assert report["verified"] is False
    assert report["missing_required"] == ["22"]
    assert report["point_selection"] == "recording_script"


def test_serial_comes_from_the_bound_device(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ALIOTH_ADB_SERIAL", "ANDROID_SERIAL", "MAMA_SERIAL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError):
        serial_from_android_driver(object())
    driver = type(
        "Driver",
        (),
        {"_cloud_phone_controller": type("Ctl", (), {"endpoint": "10.0.0.8:30615"})()},
    )()
    monkeypatch.setenv("ANDROID_SERIAL", "172.104.172.119:30660")
    assert serial_from_android_driver(driver) == "10.0.0.8:30615"


def test_driver_hands_screenshots_back_without_resolving_a_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = AndroidAdbSurfaceDriver(backend="cloud_phone")
    driver._resolver = object()
    driver._cloud_phone_controller = type(
        "Ctl",
        (),
        {"endpoint": "10.0.0.8:30615"},
    )()

    def fake_run(*, serial: str, script_path=None, runner=None):
        assert serial == "10.0.0.8:30615"
        assert script_path is None
        assert runner is None
        return {
            "ok": True,
            "error_code": None,
            "serial": serial,
            "script_path": "mama.py",
            "shots": [{"name": "01", "path": "01.png", "bytes": 20}],
            "verification": {
                "recorded": 1,
                "shot_names": ["01"],
                "missing_required": ["22"],
                "verified": False,
                "point_selection": "recording_script",
            },
        }

    monkeypatch.setattr(
        "services.api.app.application.app_mama_script.run_mama_recording_script",
        fake_run,
    )
    result = asyncio.run(
        driver._execute_mama_recording_script(action_execution_id="act-mama")
    )
    assert result.succeeded is False
    assert result.error_code == "MAMA_SCRIPT_EVIDENCE_MISSING"
    assert result.payload["mama_recording"]["verification"]["point_selection"] == (
        "recording_script"
    )


def test_mama_airtest_script_parses_cloud_back_twice_as_one_back_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application.app_mama_script import _BUNDLED_MAMA_SCRIPT
    from services.api.app.application.app_script_playback import (
        parse_script_playback_steps,
    )

    monkeypatch.delenv("MAMA_SCRIPT", raising=False)
    path = _BUNDLED_MAMA_SCRIPT
    if not path.is_file():
        pytest.skip("mama.py not available")
    steps = parse_script_playback_steps(path)
    twice = [
        step
        for step in steps
        if step.action is InteractionOperation.BACK
        and "cloud_back_twice" in step.purpose
    ]
    assert len(twice) == 1
    assert "中间不截图" in twice[0].purpose
