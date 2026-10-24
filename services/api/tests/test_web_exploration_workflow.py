"""Web V2 首跳探索与 H5 移动语义隔离测试。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.web_exploration_workflow import (
    RunWebWorkflowCommand,
    WebCaptureStage,
    WebSurfaceBootstrapper,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    SurfaceKind,
    SurfaceSessionRef,
)


@pytest.mark.asyncio
async def test_web_bootstrap_uses_dedicated_desktop_profile_and_intent() -> None:
    calls: dict[str, object] = {}

    class Driver:
        async def start_session(self, spec):
            calls["spec"] = spec
            return SurfaceSessionRef(
                task_id=spec.task_id,
                session_id=spec.session_id,
                surface_kind=spec.surface_kind,
                primary_surface_ref="web-primary:web:run-web",
            )

    class Sessions:
        async def register_active(self, *, spec, session_owner, primary_surface_ref):
            calls["registration"] = (spec, session_owner, primary_surface_ref)

    class Coordinator:
        async def coordinate(self, command):
            calls["coordination_command"] = command
            return "web-bootstrap-result"

    bootstrapper = WebSurfaceBootstrapper(
        driver=Driver(),
        surface_sessions=Sessions(),
        capture_coordinator=Coordinator(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-worker-1",
        target_url="https://example.test/dashboard",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    result = await bootstrapper.start(command)

    assert result == "web-bootstrap-result"
    spec, owner, primary = calls["registration"]
    assert spec.surface_kind is SurfaceKind.WEB
    assert owner == "web-worker-1"
    assert primary == "web-primary:web:run-web"
    assert dict(spec.options) == {
        "target_url": "https://example.test/dashboard",
        "viewport_width": 1920,
        "viewport_height": 1080,
        "screen_width": 1920,
        "screen_height": 1080,
        "device_scale_factor": 1.0,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": "light",
        "browser_profile_id": "desktop_fhd_1920x1080_v1",
    }
    intent = calls["coordination_command"].intent
    assert intent.action_type is InteractionOperation.NAVIGATE
    assert intent.semantic_target == "Web 任务锁定入口"
    assert intent.purpose == "打开任务提交时锁定的桌面 Web 目标 URL"
    assert intent.parameters["initial_surface_action"] is True
    assert intent.parameters["web_capture_stage"] == WebCaptureStage.ENTRY_PROBE.value
    assert intent.parameters["clear_obstructions"] is False
    assert intent.parameters["viewport_width"] == 1920
    assert intent.parameters["viewport_height"] == 1080
    assert intent.parameters["is_mobile"] is False
    assert intent.parameters["has_touch"] is False
    assert intent.capture_profile.profile_id == "web-v2-state-transition.v1"


@pytest.mark.asyncio
async def test_bootstrap_order_exposes_session_registration_before_home_coordinate() -> None:
    events: list[str] = []

    class Driver:
        async def start_session(self, spec):
            events.append("driver.start_session")
            return SurfaceSessionRef(
                task_id=spec.task_id,
                session_id=spec.session_id,
                surface_kind=spec.surface_kind,
                primary_surface_ref="web-primary:web:run-web",
            )

    class Sessions:
        async def register_active(self, *, spec, session_owner, primary_surface_ref):
            del spec, session_owner, primary_surface_ref
            events.append("session.register_active")

    class Coordinator:
        async def coordinate(self, command):
            del command
            events.append("home.coordinate")
            return "home-result"

    bootstrapper = WebSurfaceBootstrapper(
        driver=Driver(),
        surface_sessions=Sessions(),
        capture_coordinator=Coordinator(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-worker-1",
        target_url="https://example.test/dashboard",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    await bootstrapper.open_session(command)
    events.append("ledger.bootstrap")
    result = await bootstrapper.navigate_home(command)

    assert result == "home-result"
    assert events == [
        "driver.start_session",
        "session.register_active",
        "ledger.bootstrap",
        "home.coordinate",
    ]


@pytest.mark.parametrize("failure", (RuntimeError("register failed"), asyncio.CancelledError()))
@pytest.mark.asyncio
async def test_open_session_closes_started_driver_session_when_registration_does_not_finish(
    failure: BaseException,
) -> None:
    events: list[str] = []

    class Driver:
        async def start_session(self, spec):
            events.append("driver.start_session")
            return SurfaceSessionRef(
                task_id=spec.task_id,
                session_id=spec.session_id,
                surface_kind=spec.surface_kind,
                primary_surface_ref="web-primary:web:run-web",
            )

        async def close_session(self, *, session, reason):
            assert session.session_id == "web:run-web"
            assert reason == "surface_session_registration_failed"
            events.append("driver.close_session")

    class Sessions:
        async def register_active(self, **kwargs):
            del kwargs
            events.append("session.register_active")
            raise failure

    bootstrapper = WebSurfaceBootstrapper(
        driver=Driver(),
        surface_sessions=Sessions(),
        capture_coordinator=object(),
    )
    command = RunWebWorkflowCommand(
        task_id="task-web",
        agent_run_id="run-web",
        session_id="web:run-web",
        session_owner="web-worker-1",
        target_url="https://example.test/dashboard",
        browser_profile_id="desktop_fhd_1920x1080_v1",
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    with pytest.raises(type(failure)):
        await bootstrapper.open_session(command)

    assert events == [
        "driver.start_session",
        "session.register_active",
        "driver.close_session",
    ]
