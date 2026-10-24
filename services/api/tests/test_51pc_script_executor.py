"""51pc 一步一步執行 51pc.py。每步之後由系統截圖。"""

import asyncio
from pathlib import Path

import pytest

from services.api.app.application.app_51pc_script import (
    PC51_SCRIPT_ATTEMPTED_KEY,
    PC51_SCRIPT_DONE_KEY,
    PC51_SCRIPT_INDEX_KEY,
    PC51_SCRIPT_REASON_CODE,
    apply_51pc_script_progress,
    run_51pc_recording_script,
    script_51pc_override,
    serial_from_android_driver,
    verify_51pc_recording_shots,
)
from services.api.app.application.app_ui_tree_features import (
    resolve_product_orchestrator_id,
)
from services.api.app.domain.exploration import AppExplorationGoal, ExplorationTerminalStatus
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind
from services.api.app.infrastructure.device.android_surface_driver import (
    AndroidAdbSurfaceDriver,
)
from services.api.tests.recording_script_fixture import recording_script_fixture
from services.api.tests.test_maf_explorer_agent import _work

recorded_steps = recording_script_fixture("PC51_SCRIPT", "51pc.py")


def _pc51_work(**metadata: object):
    return _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="ready",
        observation_metadata={
            "package_name": "tv.tifpv.ydtenm",
            **metadata,
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("tv.tifpv.ydtenm",),
        ),
        allowed_operations=(InteractionOperation.OBSERVE, InteractionOperation.CLICK),
    )


def test_51pc_package_uses_its_own_orchestrator() -> None:
    assert resolve_product_orchestrator_id(package_name="tv.tifpv.ydtenm") == "51pc"
    assert resolve_product_orchestrator_id(package_name="uk.trycq.tfkapn") == "pc51"


@pytest.mark.parametrize("completed", [False, True])
def test_missing_script_cannot_claim_completion_or_fallback_to_generic(
    monkeypatch, tmp_path, completed,
):
    monkeypatch.setenv("PC51_SCRIPT", str(tmp_path / "missing.py"))
    result = script_51pc_override(_pc51_work(**{PC51_SCRIPT_DONE_KEY: completed}))
    assert result.status is ExplorationTerminalStatus.BLOCKED
    assert result.coverage_summary[PC51_SCRIPT_DONE_KEY] is False


def test_51pc_agent_runs_one_step_then_the_system_screenshots() -> None:
    decision = script_51pc_override(_pc51_work())

    assert decision is not None
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.parameters["pc51_script"] is True
    assert decision.parameters["require_screenshot_evidence"] is True
    assert decision.parameters["product_script_orchestrated"] is True
    assert "pc51_execute_recording_script" not in decision.parameters
    assert str(decision.parameters["candidate_id"]).startswith("pc51-script:0:")
    assert "留證截圖" in decision.purpose
    assert "normalized_bounds" not in decision.parameters


def test_other_packages_stay_on_the_generic_agent() -> None:
    other = _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        observation_metadata={"package_name": "com.hj.ttthree"},
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.hj.ttthree",),
        ),
    )
    assert script_51pc_override(other) is None


def test_finished_script_is_recorded_and_not_rerun() -> None:
    done = script_51pc_override(_pc51_work(**{PC51_SCRIPT_DONE_KEY: True}))
    assert done is not None
    assert done.reason_code == PC51_SCRIPT_REASON_CODE
    again = script_51pc_override(_pc51_work(**{PC51_SCRIPT_ATTEMPTED_KEY: True}))
    assert again is not None
    assert again.parameters["require_screenshot_evidence"] is True


def test_progress_advances_one_step_and_ignores_other_taps() -> None:
    decision = script_51pc_override(_pc51_work())
    result: dict[str, object] = {}
    apply_51pc_script_progress(
        result,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": decision.parameters["candidate_id"]}},
        )(),
        action_succeeded=True,
    )
    assert result[PC51_SCRIPT_INDEX_KEY] == 1
    assert result[PC51_SCRIPT_DONE_KEY] is False
    failed: dict[str, object] = {}
    apply_51pc_script_progress(
        failed,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": decision.parameters["candidate_id"]}},
        )(),
        action_succeeded=False,
    )
    assert failed[PC51_SCRIPT_INDEX_KEY] == 0
    untouched: dict[str, object] = {}
    apply_51pc_script_progress(
        untouched,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": "generic-tap"}},
        )(),
        action_succeeded=True,
    )
    assert untouched[PC51_SCRIPT_DONE_KEY] is False
    assert untouched[PC51_SCRIPT_INDEX_KEY] == 0


def test_runner_uses_the_script_file_and_only_verifies_shots(tmp_path: Path) -> None:
    script = tmp_path / "51pc.py"
    script.write_text("print('recording')\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["env_serial"] = kwargs["env"]["PC51_SERIAL"]
        shots = Path(kwargs["cwd"]) / "shots"
        shots.mkdir()
        (shots / "35.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 16)
        (shots / "36.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"b" * 16)
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    recording = run_51pc_recording_script(
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
    assert verification["shot_names"] == ["35", "36"]


def test_missing_required_shot_stays_a_verification_result() -> None:
    report = verify_51pc_recording_shots(({"name": "34"},))
    assert report["verified"] is False
    assert report["missing_required"] == ["35", "36"]
    assert report["point_selection"] == "recording_script"


def test_serial_comes_from_the_bound_device(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("ALIOTH_ADB_SERIAL", "ANDROID_SERIAL", "PC51_SERIAL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError):
        serial_from_android_driver(object())
    driver = type(
        "Driver",
        (),
        {"_cloud_phone_controller": type("Ctl", (), {"endpoint": "10.0.0.8:30660"})()},
    )()
    monkeypatch.setenv("ANDROID_SERIAL", "172.104.172.119:30615")
    assert serial_from_android_driver(driver) == "10.0.0.8:30660"


def test_driver_hands_screenshots_back_without_resolving_a_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = AndroidAdbSurfaceDriver(backend="cloud_phone")
    driver._resolver = object()
    driver._cloud_phone_controller = type(
        "Ctl",
        (),
        {"endpoint": "10.0.0.8:30660"},
    )()

    def fake_run(*, serial: str, script_path=None, runner=None):
        assert serial == "10.0.0.8:30660"
        assert script_path is None
        assert runner is None
        return {
            "ok": True,
            "error_code": None,
            "serial": serial,
            "script_path": "51pc.py",
            "shots": [{"name": "36", "path": "36.png", "bytes": 20}],
            "verification": {
                "recorded": 1,
                "shot_names": ["36"],
                "missing_required": ["35"],
                "verified": False,
                "point_selection": "recording_script",
            },
        }

    monkeypatch.setattr(
        "services.api.app.application.app_51pc_script.run_51pc_recording_script",
        fake_run,
    )
    result = asyncio.run(
        driver._execute_51pc_recording_script(action_execution_id="act-51pc")
    )
    assert result.succeeded is True
    assert result.payload["pc51_recording"]["verification"]["point_selection"] == (
        "recording_script"
    )
