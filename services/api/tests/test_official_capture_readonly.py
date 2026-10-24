"""采集不暗触屏；可信播放器动作仍走 Coordinator 双采和正式执行上下文。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from services.api.app.application.app_interaction_runtime import AppInteractionExecutor
from services.api.app.application.capture_coordinator import (
    CaptureCoordinationCommand,
    CaptureCoordinator,
)
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.domain.exploration import NormalizedBounds
from services.api.app.domain.interaction import (
    AppInstallRequest,
    AppOpenRequest,
    CaptureKind,
    CaptureRef,
    SurfaceKind,
    SurfaceSessionSpec,
    build_action_execution_id,
)
from services.api.app.domain.task import TaskTargetPlatform
from services.api.app.infrastructure.app_runtime.official import OfficialAppRuntime
from services.api.app.infrastructure.app_runtime.official_api import (
    OfficialActionResponse,
    OfficialUiTreeResponse,
)
from services.api.tests.test_app_interaction_runtime import (
    BEFORE_CAPTURE_ID,
    OBSERVATION_CAPTURE_ID,
    RUN_ID,
    SESSION_ID,
    TASK_ID,
    StaticSessionRepository,
    _analysis,
    _before_visual_analysis,
    _candidate,
    _executor,
    _request,
)
from services.api.tests.test_capture_coordinator import (
    RecordingActionExecutionRepository,
    RecordingEvidenceExitGate,
)
from services.api.tests.test_official_app_runtime import (
    PNG,
    FakeOfficialAppApiClient,
    _capture,
    _opened_runtime,
)

pytestmark = pytest.mark.asyncio


class StrictMediaClient(FakeOfficialAppApiClient):
    """模拟正式边界：只有 Coordinator 上下文才能发设备动作。"""

    def __init__(self, *, player=False):
        super().__init__(capture=_capture(width=1080, height=1920), include_auxiliary=True)
        self.player = player
        self.attempts = []
        self.events = []
        self.expected_candidate_id = None

    async def capture_png(self, session, profile):
        self.events.append("capture")
        return await super().capture_png(session, profile)

    async def capture_ui_tree(self, session, profile):
        return OfficialUiTreeResponse(
            content_type="application/xml",
            xml_text=(
                '<hierarchy><node package="com.example.target" '
                'class="android.widget.VideoView" bounds="[0,200][1080,1100]" '
                'clickable="true" content-desc="播放器" /></hierarchy>'
                if self.player
                else '<hierarchy><node package="com.example.target" '
                'class="android.app.Dialog" text="广告弹窗" bounds="[0,200][1080,1100]" '
                'clickable="true" /></hierarchy>'
            ),
        )

    async def execute(self, session, intent, action_execution_id, *, execution_context=None):
        self.attempts.append(execution_context)
        assert execution_context, "禁止采集内伪造或绕过 Coordinator 的隐藏触屏"
        assert execution_context["source_capture_id"] == BEFORE_CAPTURE_ID
        assert execution_context["action_execution_id"] == action_execution_id
        assert execution_context["resolved_candidate_id"] == self.expected_candidate_id
        self.events.append("execute")
        self.executions.append((session, intent, action_execution_id))
        self.capture_response = _capture(
            width=1080,
            height=1920,
            png_bytes=PNG + b"after",
            declared_sha256=sha256(PNG + b"after").hexdigest(),
        )
        return OfficialActionResponse(
            request_id="controlled-player-action",
            action_execution_id=action_execution_id,
            succeeded=True,
            retryable=False,
            action_trace=({"action": "click", "semantic_target": intent.semantic_target},),
        )


@pytest.mark.parametrize("player", [False, True])
async def test_media_capture_never_touches_popup_or_actual_player(player):
    client = StrictMediaClient(player=player)
    runtime, session = await _opened_runtime(capture=_capture(), client=client)
    captured = await runtime.capture(
        session=session,
        profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
    )
    artifacts = {item.artifact_type: item.content for item in captured.artifacts}
    assert client.attempts == []
    assert client.executions == []
    assert "player_wake_before_capture" not in captured.metadata
    assert "player_wake_before_capture" not in artifacts["media_state"]
    assert artifacts["action_trace"] == ()
    assert artifacts["screenshot"] == PNG
    assert artifacts["media_state"]["playback_state_available"] is False
    assert artifacts["media_state"]["item_count"] == int(player)


async def test_trusted_player_click_keeps_real_coordinator_pair_context_and_trace():
    client = StrictMediaClient(player=True)
    runtime = OfficialAppRuntime(api_client=client, max_screenshot_bytes=1024)
    session = await runtime.start_session(
        SurfaceSessionSpec(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            surface_kind=SurfaceKind.ANDROID,
            platform_build_ref="build-1",
        )
    )
    await runtime.ensure_installed(
        AppInstallRequest(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            platform_build_ref="build-1",
            sha256="a" * 64,
            package_name="com.example.target",
        )
    )
    await runtime.open_app(
        AppOpenRequest(
            task_id=TASK_ID,
            session_id=SESSION_ID,
            package_name="com.example.target",
        )
    )
    image_signature = "sha256:" + sha256(PNG).hexdigest()
    candidate = _candidate(
        semantic_name="播放器",
        purpose="轻触可信播放器区域显示控件",
        source_image_signature=image_signature,
        normalized_bounds=NormalizedBounds(0.2, 0.2, 0.8, 0.4),
    )
    intent = replace(
        _request(candidate).intent,
        source_capture_id=OBSERVATION_CAPTURE_ID,
        capture_profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )
    before_analysis = _before_visual_analysis(
        candidate, visual_page_type="player", image_signature=image_signature
    )
    client.expected_candidate_id = before_analysis.candidates[0].candidate_id
    executor, _driver, _candidate_ref = _executor(
        candidate=candidate,
        analysis=_analysis(candidate, visual_page_type="player", image_signature=image_signature),
        before_signature=image_signature,
        before_analysis=before_analysis,
        driver=runtime,
        before_action_execution_id=build_action_execution_id(intent),
    )

    class RuntimeSessionRepository(StaticSessionRepository):
        """持久化替身返回当前真实 Official Session 的引用。"""

        async def get(self, *, session_id):
            snapshot = await super().get(session_id=session_id)
            return replace(snapshot, current_surface_ref=session.primary_surface_ref)

    executor = AppInteractionExecutor(
        driver=runtime,
        surface_sessions=RuntimeSessionRepository(),
        candidate_resolver=executor._candidate_resolver,
        allowed_packages=("com.example.target",),
        command_validator=executor._command_validator,
    )

    class MediaWriter:
        """只替换持久化端；两次采集均经过真实 Official Runtime。"""

        def __init__(self):
            self.requests, self.captured = [], []

        async def capture(self, request):
            self.requests.append(request)
            captured = await runtime.capture(
                session=session,
                profile=CaptureProfilePolicy.for_platform(TaskTargetPlatform.APP),
            )
            self.captured.append(captured)
            before = len(self.captured) == 1
            return CaptureRef(
                task_id=request.task_id,
                session_id=request.session_id,
                capture_id=BEFORE_CAPTURE_ID if before else "capture-player-after",
                surface_kind=SurfaceKind.ANDROID,
                capture_kind=CaptureKind.APP,
                role=request.directive.role,
                artifact_ids=("before-png" if before else "after-png",),
                parent_capture_id=request.parent_capture_id,
                action_execution_id=request.action_execution_id,
            )

    writer = MediaWriter()
    coordinator = CaptureCoordinator(
        capture_writer=writer,
        interaction_executor=executor,
        evidence_exit_gate=RecordingEvidenceExitGate(),
        action_execution_repository=RecordingActionExecutionRepository([]),
    )
    result = await coordinator.coordinate(
        CaptureCoordinationCommand(agent_run_id=RUN_ID, intent=intent)
    )
    assert result.interaction.status.value == "succeeded", (
        result.interaction.error_code,
        result.interaction.error_message,
        client.attempts,
        client.events,
    )
    assert client.events == ["capture", "execute", "capture"]
    assert len(client.attempts) == 1
    assert result.after_capture.parent_capture_id == result.before_capture.capture_id
    assert result.after_capture.action_execution_id == result.interaction.action_execution_id
    before = {item.artifact_type: item.content for item in writer.captured[0].artifacts}
    after = {item.artifact_type: item.content for item in writer.captured[1].artifacts}
    assert before["screenshot"] == PNG and after["screenshot"] == PNG + b"after"
    assert before["action_trace"] == ()
    assert len(after["action_trace"]) == 1
    assert after["action_trace"][0]["semantic_target"] == "播放器"
    assert "player_wake_before_capture" not in after["media_state"]
