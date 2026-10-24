"""统一 SurfaceDriver 的 Web 生产 Adapter 契约测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.api.app.application.interaction_runtime import (
    InMemoryActionEvidenceBuffer,
    ResolvedInteractionTarget,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceDriver,
    SurfaceKind,
    SurfaceSessionSpec,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureResult,
)
from services.api.app.infrastructure.browser.surface_driver import (
    PlaywrightSurfaceDriverAdapter,
)


class RecordingBrowser:
    def __init__(self) -> None:
        self.requests = []
        self.closed = []

    async def capture(self, request):
        self.requests.append(request)
        return PageCaptureResult(
            execution_status="success",
            mode=str(request.mode.value),
            reachable=True,
            final_url="https://example.com/home",
            title="首页",
            status_code=200,
            artifacts=[
                PageCaptureArtifact("screenshot", None, {"encoding": "base64", "data": ""}),
                PageCaptureArtifact("dom", None, "<main>首页</main>"),
                PageCaptureArtifact("visible_text", None, "首页"),
                PageCaptureArtifact("action_trace", None, [{"action": "click"}]),
            ],
            action_trace=[{"action": "click"}],
            metrics={"page_settle_complete": True},
        )

    async def close_session(self, session_id):
        self.closed.append(session_id)


class StaticResolver:
    async def resolve(self, request):
        del request
        return ResolvedInteractionTarget(selector="#login")


def _profile() -> CaptureProfile:
    return CaptureProfile(
        profile_id="web-contract",
        before=CaptureDirective(
            role="before_action",
            artifact_types=("screenshot", "dom", "visible_text"),
        ),
        after=CaptureDirective(
            role="after_action",
            artifact_types=("screenshot", "dom", "visible_text", "action_trace"),
        ),
    )


async def test_playwright_adapter_implements_common_surface_driver_contract() -> None:
    browser = RecordingBrowser()
    adapter: SurfaceDriver = PlaywrightSurfaceDriverAdapter(
        browser=browser,
        target_resolver=StaticResolver(),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )
    session = await adapter.start_session(
        SurfaceSessionSpec(
            task_id="task-1",
            session_id="session-1",
            surface_kind=SurfaceKind.WEB,
        )
    )
    intent = ActionIntent(
        task_id=session.task_id,
        session_id=session.session_id,
        intent_id="intent-1",
        idempotency_key="task:branch:intent",
        surface_kind=session.surface_kind,
        source_capture_id="capture-1",
        action_type=InteractionOperation.CLICK,
        semantic_target="登录按钮",
        purpose="进入登录页",
        capture_profile=_profile(),
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    observation = await adapter.observe(session=session, profile=_profile())
    action = await adapter.execute(
        session=session,
        intent=intent,
        action_execution_id="action-execution:contract",
    )
    artifacts = await adapter.capture(session=session, profile=_profile())
    capabilities = adapter.describe_capabilities()
    await adapter.close_session(session=session, reason="contract_test_completed")

    assert observation.session_id == session.session_id
    assert action.succeeded is True
    assert action.action_execution_id == "action-execution:contract"
    assert {item.artifact_type for item in artifacts.artifacts} >= {
        "screenshot",
        "dom",
        "visible_text",
        "action_trace",
    }
    assert capabilities.surface_kind is SurfaceKind.WEB
    assert InteractionOperation.CLICK.value in capabilities.action_types
    assert InteractionOperation.SEEK_MEDIA.value in capabilities.action_types
    assert "media_state" in capabilities.artifact_types
    assert browser.closed == ["session-1"]
