"""黃桃短劇一步一步執行 xingba.py。每步之後由系統截圖。"""

from pathlib import Path

from services.api.app.application.app_script_playback import parse_script_playback_steps
from services.api.app.application.app_ui_tree_features import (
    resolve_product_orchestrator_id,
)
from services.api.app.application.app_xingba_script import (
    XINGBA_SCRIPT_ATTEMPTED_KEY,
    XINGBA_SCRIPT_DONE_KEY,
    XINGBA_SCRIPT_INDEX_KEY,
    XINGBA_SCRIPT_REASON_CODE,
    apply_xingba_script_progress,
    resolve_xingba_script_path,
    run_xingba_recording_script,
    script_xingba_override,
)
from services.api.app.domain.exploration import AppExplorationGoal
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind
from services.api.tests.recording_script_fixture import recording_script_fixture
from services.api.tests.test_maf_explorer_agent import _work

recorded_steps = recording_script_fixture("XINGBA_SCRIPT", "xingba.py")


def _xingba_work(**metadata: object):
    return _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="ready",
        observation_metadata={
            "package_name": "com.androidfmuw.zvgeok",
            **metadata,
        },
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.androidfmuw.zvgeok",),
        ),
        allowed_operations=(InteractionOperation.OBSERVE, InteractionOperation.CLICK),
    )


def test_huangtao_package_uses_the_xingba_orchestrator() -> None:
    assert resolve_product_orchestrator_id(
        package_name="com.androidfmuw.zvgeok"
    ) == "xingba"


def test_xingba_agent_runs_one_step_then_the_system_screenshots() -> None:
    decision = script_xingba_override(_xingba_work())

    assert decision is not None
    assert decision.action_type is InteractionOperation.OBSERVE
    assert decision.parameters["xingba_script"] is True
    assert decision.parameters["require_screenshot_evidence"] is True
    assert decision.parameters["product_script_orchestrated"] is True
    assert decision.parameters["product_expected_package"] == "com.androidfmuw.zvgeok"
    assert "xingba_execute_recording_script" not in decision.parameters
    assert str(decision.parameters["candidate_id"]).startswith("xingba-script:0:")
    assert "留證截圖" in decision.purpose
    assert "normalized_bounds" not in decision.parameters


def test_script_clicks_keep_the_recorded_points() -> None:
    steps = parse_script_playback_steps(resolve_xingba_script_path())
    clicks = [step for step in steps if step.action is InteractionOperation.CLICK]
    assert clicks
    assert clicks[0].x == round(1309 / 1440, 6) or abs(clicks[0].x - 1309 / 1440) < 1e-6
    assert abs(clicks[0].y - 232 / 3040) < 1e-6
    assert abs(clicks[-1].x - 82 / 1440) < 1e-6
    assert abs(clicks[-1].y - 240 / 3040) < 1e-6


def test_old_xingba_package_does_not_run_huangtao_points() -> None:
    other = _work(
        candidates=(),
        surface_kind=SurfaceKind.ANDROID,
        observation_metadata={"package_name": "com.androidcxgy.fosjhc"},
        app_goal=AppExplorationGoal(
            required_page_types=("home",),
            required_milestones=("surface:home",),
            minimum_business_pages=1,
            allowed_packages=("com.androidcxgy.fosjhc",),
        ),
    )
    assert script_xingba_override(other) is None


def test_finished_script_is_recorded_and_not_rerun() -> None:
    done = script_xingba_override(_xingba_work(**{XINGBA_SCRIPT_DONE_KEY: True}))
    assert done is not None
    assert done.reason_code == XINGBA_SCRIPT_REASON_CODE
    again = script_xingba_override(
        _xingba_work(**{XINGBA_SCRIPT_ATTEMPTED_KEY: True})
    )
    assert again is not None
    assert again.parameters["require_screenshot_evidence"] is True


def test_progress_advances_one_step_and_ignores_other_taps() -> None:
    decision = script_xingba_override(_xingba_work())
    result: dict[str, object] = {}
    apply_xingba_script_progress(
        result,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": decision.parameters["candidate_id"]}},
        )(),
        action_succeeded=True,
    )
    assert result[XINGBA_SCRIPT_INDEX_KEY] == 1
    assert result[XINGBA_SCRIPT_DONE_KEY] is False
    failed: dict[str, object] = {}
    apply_xingba_script_progress(
        failed,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": decision.parameters["candidate_id"]}},
        )(),
        action_succeeded=False,
    )
    assert failed[XINGBA_SCRIPT_INDEX_KEY] == 0
    untouched: dict[str, object] = {}
    apply_xingba_script_progress(
        untouched,
        prior={},
        completed_action=type(
            "Action",
            (),
            {"parameters": {"candidate_id": "generic-tap"}},
        )(),
        action_succeeded=True,
    )
    assert untouched[XINGBA_SCRIPT_DONE_KEY] is False
    assert untouched[XINGBA_SCRIPT_INDEX_KEY] == 0


def test_runner_uses_the_script_file_and_only_verifies_shots(tmp_path: Path) -> None:
    script = tmp_path / "xingba.py"
    script.write_text("print('recording')\n", encoding="utf-8")
    seen: dict[str, object] = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["env_serial"] = kwargs["env"]["XINGBA_SERIAL"]
        shots = Path(kwargs["cwd"]) / "shots"
        shots.mkdir(exist_ok=True)
        for name in ("01", "28"):
            (shots / f"{name}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 16)
        return type("Completed", (), {"returncode": 0, "stderr": ""})()

    recording = run_xingba_recording_script(
        serial="172.104.172.119:30660",
        script_path=script,
        runner=runner,
    )
    assert seen["command"][-2:] == [str(script), "172.104.172.119:30660"]
    assert seen["env_serial"] == "172.104.172.119:30660"
    assert recording["ok"] is True
    assert recording["verification"]["missing_required"] == []
