"""把现有 Playwright 运行时暴露为统一 SurfaceDriver 契约。"""

from __future__ import annotations

from services.api.app.application.capture_coordination import ExecuteInteractionRequest
from services.api.app.application.interaction_command_validation import (
    InteractionCommandValidator,
)
from services.api.app.application.interaction_runtime import (
    ActionEvidenceBuffer,
    SemanticTargetResolver,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CapabilityDescriptor,
    CapabilityStatus,
    CaptureProfile,
    InteractionOperation,
    InteractionResultStatus,
    RawActionResult,
    RawArtifact,
    RawArtifactSet,
    RawSurfaceObservation,
    SurfaceCapabilityManifest,
    SurfaceKind,
    SurfaceSessionRef,
    SurfaceSessionSpec,
)
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.infrastructure.browser.capture_runtime import (
    BrowserInteractionExecutor,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
    capture_page_with_timeout,
)


class PlaywrightSurfaceDriverAdapter:
    """统一 Web Adapter；会话在首次 Navigate 时由底层 Playwright 延迟建立。"""

    def __init__(
        self,
        *,
        browser: PlaywrightPageCaptureBrowser,
        target_resolver: SemanticTargetResolver,
        evidence_buffer: ActionEvidenceBuffer,
        command_validator: InteractionCommandValidator | None = None,
    ) -> None:
        self._browser = browser
        self._executor = BrowserInteractionExecutor(
            browser=browser,
            target_resolver=target_resolver,
            evidence_buffer=evidence_buffer,
            command_validator=command_validator,
        )

    async def start_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        if spec.surface_kind is not SurfaceKind.WEB:
            raise ValueError("Playwright Adapter 只支持 Web Surface")
        return SurfaceSessionRef(
            task_id=spec.task_id,
            session_id=spec.session_id,
            surface_kind=spec.surface_kind,
            primary_surface_ref=web_primary_surface_ref(spec.session_id),
        )

    async def reattach_session(self, spec: SurfaceSessionSpec) -> SurfaceSessionRef:
        return await self.start_session(spec)

    async def observe(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawSurfaceObservation:
        _require_web_session(session)
        directive = profile.before
        result = await _capture_raw(
            browser=self._browser,
            session=session,
            artifact_types=set(directive.artifact_types),
        )
        return RawSurfaceObservation(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_kind=session.surface_kind,
            surface_ref=session.primary_surface_ref,
            payload={
                "execution_status": result.execution_status,
                "url": result.final_url,
                "title": result.title,
                "reachable": result.reachable,
                "status_code": result.status_code,
                "metrics": result.metrics,
            },
        )

    async def execute(
        self,
        *,
        session: SurfaceSessionRef,
        intent: ActionIntent,
        action_execution_id: str,
    ) -> RawActionResult:
        _require_web_session(session)
        if (
            intent.task_id != session.task_id
            or intent.session_id != session.session_id
            or intent.surface_kind is not session.surface_kind
        ):
            raise ValueError("ActionIntent 与 Surface Session 不一致")
        result = await self._executor.execute(
            ExecuteInteractionRequest(
                agent_run_id="surface-driver-adapter",
                intent=intent,
                source_capture_id=intent.source_capture_id,
                action_execution_id=action_execution_id,
            )
        )
        succeeded = result.status is InteractionResultStatus.EVIDENCE_PENDING
        return RawActionResult(
            action_execution_id=result.action_execution_id,
            succeeded=succeeded,
            retryable=result.retryable,
            error_code=result.error_code,
            payload={
                "status": result.status.value,
                "state_summary": dict(result.state_summary),
                "state_diff": dict(result.state_diff),
            },
        )

    async def capture(
        self,
        *,
        session: SurfaceSessionRef,
        profile: CaptureProfile,
    ) -> RawArtifactSet:
        _require_web_session(session)
        result = await _capture_raw(
            browser=self._browser,
            session=session,
            artifact_types=set(profile.after.artifact_types),
        )
        if result.execution_status != "success":
            raise RuntimeError(result.error_message or "Web Capture 未成功")
        return RawArtifactSet(
            task_id=session.task_id,
            session_id=session.session_id,
            surface_ref=session.primary_surface_ref,
            artifacts=tuple(
                RawArtifact(artifact_type=item.artifact_type, content=item.content)
                for item in result.artifacts
            ),
            metadata={
                "url": result.final_url,
                "title": result.title,
                "status_code": result.status_code,
                "metrics": result.metrics,
            },
        )

    def describe_capabilities(self) -> SurfaceCapabilityManifest:
        ready = CapabilityDescriptor(status=CapabilityStatus.READY)
        return SurfaceCapabilityManifest(
            adapter_id="playwright",
            adapter_version="surface-driver.v1",
            surface_kind=SurfaceKind.WEB,
            platform="web",
            backends=("playwright",),
            actions={
                "session": ready,
                "close": ready,
                **{item.value: ready for item in InteractionOperation},
            },
            observations={
                name: ready
                for name in (
                    "screenshot",
                    "dom",
                    "visible_text",
                    "ui_snapshot",
                    "media_state",
                )
            },
            artifacts={
                name: ready
                for name in (
                    "screenshot",
                    "dom",
                    "visible_text",
                    "ui_snapshot",
                    "media_state",
                    "action_trace",
                    "har",
                    "console_log",
                    "temporal_trace",
                    "page_coverage",
                )
            },
            features={
                "lazy_session_start": True,
                "same_context_new_surface": False,
                "surface_pin": False,
            },
        )

    async def close_session(self, *, session: SurfaceSessionRef, reason: str) -> None:
        _require_web_session(session)
        if not reason.strip():
            raise ValueError("reason 不能为空")
        await self._browser.close_session(session.session_id)


async def _capture_raw(
    *,
    browser: PlaywrightPageCaptureBrowser,
    session: SurfaceSessionRef,
    artifact_types: set[str],
):
    mode = (
        PageCaptureMode.TIMED_CAPTURE
        if "temporal_trace" in artifact_types
        else PageCaptureMode.OBSERVE
    )
    return await capture_page_with_timeout(
        request=PageCaptureRequest(
            mode=mode,
            capture_dom="dom" in artifact_types,
            capture_visible_text="visible_text" in artifact_types,
            capture_screenshot="screenshot" in artifact_types,
            capture_media_state="media_state" in artifact_types,
            session_id=session.session_id,
        ),
        browser=browser,
        timeout_seconds=30,
    )


def _require_web_session(session: SurfaceSessionRef) -> None:
    if not isinstance(session, SurfaceSessionRef) or session.surface_kind is not SurfaceKind.WEB:
        raise ValueError("Playwright Adapter 只接受 Web Surface Session")
