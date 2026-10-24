"""V2 Web Capture/Interaction 生产适配器的边界测试。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote

import cv2
import numpy as np
import pytest

from services.api.app.application.capture_coordination import (
    CapturePhase,
    CaptureWriteRequest,
)
from services.api.app.application.capture_coordinator import (
    CaptureCoordinationCommand,
    CaptureCoordinator,
    ExecuteInteractionRequest,
    InProcessSessionCommandGate,
)
from services.api.app.application.h5_safety_interstitial import (
    H5_SAFETY_INTERSTITIAL_CAPABILITY,
    H5_SAFETY_INTERSTITIAL_SELECTOR,
    H5_SAFETY_INTERSTITIAL_TARGET,
    H5_SAFETY_INTERSTITIAL_TARGET_MODE,
)
from services.api.app.application.h5_structural_navigation import (
    h5_flutter_bottom_navigation_runtime_selector,
    h5_page_tab_target,
)
from services.api.app.application.interaction_runtime import (
    InMemoryActionEvidenceBuffer,
    LogicalTargetRuntimeBinding,
    ResolvedInteractionTarget,
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
    VisualNavigationRuntimeTarget,
)
from services.api.app.application.page_capture_persistence import (
    PersistedEvidenceArtifact,
    PersistedPageCapture,
)
from services.api.app.application.web_agent_page_capture import (
    AgentToolInteractionExecutor,
)
from services.api.app.domain.exploration.h5_logical_surface import (
    stable_h5_surface_hash,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    build_visual_navigation_capability,
    verify_visual_navigation_capability,
    visual_navigation_region_digests,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureProfile,
    InteractionOperation,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.browser import page_capture as page_capture_module
from services.api.app.infrastructure.browser.capture_runtime import (
    BrowserCaptureWriter,
    BrowserInteractionExecutor,
    _annotate_popup_clearance_trace_entry,
    _annotate_startup_ad_trace_entry,
    _logical_navigation_postcondition_error,
    _logical_target_runtime_binding,
    _merge_action_evidence,
    _public_state_diff,
    _result_final_obstruction_cleared,
)
from services.api.app.infrastructure.browser.page_capture import (
    POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR,
    BlockedPageCaptureActionError,
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    PageInteractionFailedError,
    PageTriggerAction,
    PlaywrightPageCaptureBrowser,
)
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
    _flutter_fullscreen_startup_skip_snapshot_matches,
)
from services.api.app.infrastructure.browser.ui_snapshot_reader import (
    PostgresTrustedUiSnapshotReader,
)
from services.api.tests.web_browser_test_support import launch_web_test_browser

_VISUAL_DB_TASK_ID = "11111111-1111-4111-8111-111111111111"
_VISUAL_DB_CAPTURE_ID = "22222222-2222-4222-8222-222222222222"
_VISUAL_DB_ARTIFACT_ID = "33333333-3333-4333-8333-333333333333"
_OTHER_DB_TASK_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
_OTHER_DB_CAPTURE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
_OTHER_DB_ARTIFACT_ID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


class StaticSnapshotReader:
    def __init__(
        self,
        snapshot,
        *,
        visual_png: bytes | None = None,
        visual_artifact_id: str = "artifact-visual",
    ) -> None:
        self.snapshot = snapshot
        self.visual_png = visual_png
        self.visual_artifact_id = visual_artifact_id
        self.calls = []
        self.visual_calls = []
        self.visual_region_calls = []
        self.visual_image_calls = []

    async def read_ui_snapshot(self, *, task_id, page_capture_id):
        self.calls.append((task_id, page_capture_id))
        return self.snapshot

    async def read_visual_evidence_checksum(
        self, *, task_id, page_capture_id, artifact_id, frame_index
    ):
        self.visual_calls.append((task_id, page_capture_id, artifact_id, frame_index))
        return self.snapshot["_capture_visual_checksum"]

    async def read_visual_evidence_region_checksum(
        self,
        *,
        task_id,
        page_capture_id,
        artifact_id,
        left_ratio,
        top_ratio,
        right_ratio,
        bottom_ratio,
    ):
        self.visual_region_calls.append(
            (
                task_id,
                page_capture_id,
                artifact_id,
                left_ratio,
                top_ratio,
                right_ratio,
                bottom_ratio,
            )
        )
        return self.snapshot["_capture_visual_region_checksum"]

    async def read_visual_evidence_image(
        self,
        *,
        task_id,
        page_capture_id,
        artifact_id,
        expected_sha256,
    ):
        self.visual_image_calls.append((task_id, page_capture_id, artifact_id, expected_sha256))
        if self.visual_png is None or artifact_id != self.visual_artifact_id:
            raise SemanticTargetResolutionError(
                "视觉导航来源截图不存在",
                error_code="VISUAL_SOURCE_ARTIFACT_MISSING",
            )
        return self.visual_png


class MappingSnapshotReader:
    def __init__(self, snapshots) -> None:
        self.snapshots = snapshots
        self.calls = []

    async def read_ui_snapshot(self, *, task_id, page_capture_id):
        self.calls.append((task_id, page_capture_id))
        return self.snapshots[page_capture_id]

    async def read_visual_evidence_checksum(
        self, *, task_id, page_capture_id, artifact_id, frame_index
    ):
        return self.snapshots[page_capture_id]["_capture_visual_checksum"]

    async def read_visual_evidence_region_checksum(
        self,
        *,
        task_id,
        page_capture_id,
        artifact_id,
        left_ratio,
        top_ratio,
        right_ratio,
        bottom_ratio,
    ):
        del task_id, artifact_id, left_ratio, top_ratio, right_ratio, bottom_ratio
        return self.snapshots[page_capture_id]["_capture_visual_region_checksum"]


def test_public_state_diff_preserves_optional_logical_surface_transition() -> None:
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": "logical_page_changed",
        "logical_transition_proof": "trusted",
    }
    result = PageCaptureResult(
        execution_status="success",
        mode="single_trigger",
        reachable=True,
        final_url="https://example.test/profile",
        title="个人中心",
        status_code=200,
        action_trace=[
            {
                "transition_kind": "same_page_interaction",
                "navigation": {
                    "before_url": "https://example.test/",
                    "after_url": "https://example.test/profile",
                    "current_url_changed": True,
                    "new_page_count": 0,
                },
                "state_diff": {"logical_surface_transition": transition},
            }
        ],
    )

    state_diff = _public_state_diff(result)

    assert state_diff["transition_kind"] == "same_page_interaction"
    assert state_diff["logical_surface_transition"] == transition


class RecordingResolver:
    def __init__(self, target: ResolvedInteractionTarget) -> None:
        self.target = target
        self.requests = []

    async def resolve(self, request):
        self.requests.append(request)
        return self.target


class RecordingBrowser:
    def __init__(self) -> None:
        self.requests = []

    async def capture(self, request):
        self.requests.append(request)
        call_no = len(self.requests)
        if request.bootstrap_blank_surface:
            result = _page_result(mode=request.mode, final_url="about:blank")
            return replace(
                result,
                artifacts=[
                    artifact
                    for artifact in result.artifacts
                    if request.capture_screenshot or artifact.artifact_type != "screenshot"
                ],
            )
        if call_no == 2:
            action_bound_artifacts = (
                [
                    PageCaptureArtifact(
                        "screenshot",
                        None,
                        {
                            "sequence": True,
                            "purpose": "ad_landing_consistency",
                            "frames": [
                                {"index": 0, "role": "source"},
                                {"index": 1, "role": "landing"},
                            ],
                        },
                    )
                ]
                if request.capture_ad_landing_evidence
                else []
            )
            return _page_result(
                mode=request.mode,
                final_url="https://example.com/home?token=secret#fragment",
                action_trace=[
                    {
                        "action": "click",
                        "selector": "#login",
                        "transition_kind": "navigation",
                        "navigation": {
                            "before_url": "https://example.com/?token=before",
                            "after_url": "https://example.com/home?token=secret",
                            "current_url_changed": True,
                            "new_page_count": 0,
                        },
                    }
                ],
                extra_artifacts=action_bound_artifacts,
            )
        return _page_result(mode=request.mode)


class RecordingPersistence:
    def __init__(self) -> None:
        self.calls = []

    async def persist(self, **kwargs):
        self.calls.append(kwargs)
        page_no = len(self.calls)
        artifacts = [
            PersistedEvidenceArtifact(
                artifact_id=f"artifact-{page_no}-{index}",
                artifact_type=artifact.artifact_type,
                storage_path=f"task-1/capture-{page_no}/artifact-{index}.json",
                content_type="application/json",
                checksum="sha256:test",
                size_bytes=1,
                metadata={},
            )
            for index, artifact in enumerate(kwargs["result"].artifacts, start=1)
        ]
        return PersistedPageCapture(
            page_capture_id=f"capture-{page_no}",
            page_seq=page_no,
            artifacts=artifacts,
        )


class NoopEvidenceExitGate:
    """该测试只验证浏览器适配器，门禁行为由专属测试覆盖。"""

    async def reserve(self, *, intent, permit):
        del intent, permit
        return None

    async def finalize(self, *, decision, status):
        del decision, status

    async def expire_stale(self, *, limit=100):
        del limit
        return 0


def _page_result(
    *,
    mode: PageCaptureMode,
    final_url: str = "https://example.com/",
    action_trace=None,
    extra_artifacts=None,
) -> PageCaptureResult:
    trace = list(action_trace or [])
    artifacts = [
        PageCaptureArtifact("screenshot", None, {"encoding": "base64", "data": ""}),
        PageCaptureArtifact("dom", None, "<main>登录</main>"),
        PageCaptureArtifact("visible_text", None, "登录"),
        PageCaptureArtifact(
            "ui_snapshot",
            None,
            {"schema_version": "ui-snapshot.v2", "elements": []},
        ),
    ]
    if trace:
        artifacts.extend(
            [
                PageCaptureArtifact("action_trace", None, trace),
                PageCaptureArtifact("har", None, {"entries": []}),
            ]
        )
    artifacts.extend(list(extra_artifacts or []))
    return PageCaptureResult(
        execution_status="success",
        mode=mode.value,
        reachable=True,
        final_url=final_url,
        title="首页",
        status_code=200,
        artifacts=artifacts,
        action_trace=trace,
        metrics={"page_settle_complete": True, "page_inspection_ready": True},
    )


def _intent(
    *,
    operation=InteractionOperation.CLICK,
    parameters=None,
    semantic_target="登录按钮",
    purpose="进入登录页",
    deadline=None,
) -> ActionIntent:
    return ActionIntent(
        task_id="task-1",
        session_id="session-1",
        intent_id="intent-1",
        idempotency_key="task-1:branch-1:intent-1",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-parent",
        action_type=operation,
        semantic_target=semantic_target,
        purpose=purpose,
        parameters=parameters or {"navigation_depth": 1},
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfile(
            profile_id="critical-action",
            before=CaptureDirective(
                role="before_action",
                artifact_types=("screenshot", "dom", "visible_text", "ui_snapshot"),
                inherit_parent_semantics=True,
            ),
            after=CaptureDirective(
                role="after_action",
                artifact_types=(
                    "screenshot",
                    "dom",
                    "visible_text",
                    "ui_snapshot",
                    "action_trace",
                ),
            ),
        ),
        deadline=deadline or datetime.now(UTC) + timedelta(minutes=1),
    )


async def test_web_runtime_links_action_trace_without_leaking_locator_to_result() -> None:
    browser = RecordingBrowser()
    persistence = RecordingPersistence()
    buffer = InMemoryActionEvidenceBuffer()
    resolver = RecordingResolver(ResolvedInteractionTarget(selector="#login"))
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser,
            persistence=persistence,
            evidence_buffer=buffer,
        ),
        interaction_executor=BrowserInteractionExecutor(
            browser=browser,
            target_resolver=resolver,
            evidence_buffer=buffer,
        ),
        evidence_exit_gate=NoopEvidenceExitGate(),
    )

    result = await coordinator.coordinate(
        CaptureCoordinationCommand(agent_run_id="run-1", intent=_intent())
    )

    assert [item.mode for item in browser.requests] == [
        PageCaptureMode.OBSERVE,
        PageCaptureMode.SINGLE_TRIGGER,
        PageCaptureMode.OBSERVE,
    ]
    assert browser.requests[1].trigger_action is PageTriggerAction.CLICK
    assert browser.requests[1].selector == "#login"
    assert browser.requests[1].capture_transient_messages is True
    assert resolver.requests[0].source_capture_id == "capture-1"
    assert result.before_capture.capture_kind is CaptureKind.PAGE
    assert result.before_capture.surface_kind is SurfaceKind.WEB
    assert result.before_capture.capture_id == "capture-1"
    assert result.after_capture.parent_capture_id == "capture-1"
    assert result.interaction.status is InteractionResultStatus.SUCCEEDED
    assert result.interaction.result_capture_id == "capture-2"
    assert result.interaction.state_summary["location"] == "https://example.com/home"
    public_result = str(result.interaction.to_dict())
    assert "#login" not in public_result
    assert "secret" not in public_result
    after_artifacts = persistence.calls[1]["result"].artifacts
    action_artifacts = [item for item in after_artifacts if item.artifact_type == "action_trace"]
    assert len(action_artifacts) == 1
    action_artifact = action_artifacts[0]
    assert action_artifact.content[0]["selector"] == "#login"
    assert await buffer.get(action_execution_id=result.interaction.action_execution_id) is None


async def test_detail_url_navigation_reaches_goto_capture_request() -> None:
    destination = "https://shape-shift.test/article/41"
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                destination_url=destination,
                target_source="web_detail_candidate_url",
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                operation=InteractionOperation.NAVIGATE,
                parameters={
                    "target_mode": "web_detail_candidate_url",
                    "web_detail_candidate_id": "web-candidate:random",
                    "web_detail_source_capture_id": "capture-list",
                    "web_detail_list_page_type": "content_list",
                    "web_detail_origin": "https://shape-shift.test",
                    "web_detail_current_url": "https://shape-shift.test/list",
                    "destination_url": destination,
                },
                semantic_target="打开当前列表可信详情",
                purpose="采集当前白名单菜单列表的一层可信详情",
            ),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert len(browser.requests) == 1
    assert browser.requests[0].mode is PageCaptureMode.GOTO
    assert browser.requests[0].url == destination
    assert browser.requests[0].trigger_action is None
    assert browser.requests[0].selector is None


async def test_h5_bootstrap_uses_fixed_viewport_before_session_is_created() -> None:
    """首个 before Capture 创建 Context，必须先收到 H5 视口而非只给动作。"""
    browser = RecordingBrowser()
    persistence = RecordingPersistence()
    buffer = InMemoryActionEvidenceBuffer()
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser,
            persistence=persistence,
            evidence_buffer=buffer,
        ),
        interaction_executor=BrowserInteractionExecutor(
            browser=browser,
            target_resolver=RecordingResolver(
                ResolvedInteractionTarget(destination_url="https://m.example.test/home")
            ),
            evidence_buffer=buffer,
        ),
        evidence_exit_gate=NoopEvidenceExitGate(),
    )
    intent = replace(
        _intent(
            operation=InteractionOperation.NAVIGATE,
            parameters={
                "destination_url": "https://m.example.test/home",
                "initial_surface_action": True,
                "viewport_width": 390,
                "viewport_height": 844,
            },
            semantic_target="H5 任务锁定入口",
            purpose="打开任务提交时锁定的 H5 目标 URL",
        ),
        source_capture_id="h5-entry:run-1",
    )

    await coordinator.coordinate(CaptureCoordinationCommand(agent_run_id="run-1", intent=intent))

    assert browser.requests[0].bootstrap_blank_surface is True
    assert [(request.viewport_width, request.viewport_height) for request in browser.requests] == [
        (390, 844),
        (390, 844),
        (390, 844),
    ]


async def test_initial_pristine_blank_before_action_omits_screenshot_but_keeps_audit() -> None:
    origin = "https://fresh-layout.test"

    class PristineBlankBrowser(RecordingBrowser):
        async def capture(self, request):
            self.requests.append(request)
            result = _page_result(
                mode=request.mode,
                final_url="about:blank" if request.bootstrap_blank_surface else origin,
            )
            if request.capture_screenshot:
                return result
            return replace(
                result,
                artifacts=[
                    artifact
                    for artifact in result.artifacts
                    if artifact.artifact_type != "screenshot"
                ],
            )

    browser = PristineBlankBrowser()
    persistence = RecordingPersistence()
    buffer = InMemoryActionEvidenceBuffer()
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser,
            persistence=persistence,
            evidence_buffer=buffer,
        ),
        interaction_executor=BrowserInteractionExecutor(
            browser=browser,
            target_resolver=RecordingResolver(
                ResolvedInteractionTarget(destination_url=f"{origin}/home")
            ),
            evidence_buffer=buffer,
        ),
        evidence_exit_gate=NoopEvidenceExitGate(),
    )
    intent = replace(
        _intent(
            operation=InteractionOperation.NAVIGATE,
            parameters={
                "destination_url": f"{origin}/home",
                "initial_surface_action": True,
            },
            semantic_target="Web 任务锁定入口",
            purpose="打开任务提交时锁定的桌面 Web 目标 URL",
        ),
        source_capture_id="web-entry:run-1",
    )

    await coordinator.coordinate(CaptureCoordinationCommand(agent_run_id="run-1", intent=intent))

    assert browser.requests[0].bootstrap_blank_surface is True
    assert browser.requests[0].capture_screenshot is False
    assert browser.requests[1].capture_screenshot is True
    assert browser.requests[2].capture_screenshot is True
    before_artifact_types = {
        artifact.artifact_type for artifact in persistence.calls[0]["result"].artifacts
    }
    assert "screenshot" not in before_artifact_types
    assert {"dom", "ui_snapshot"}.issubset(before_artifact_types)
    assert {artifact.artifact_type for artifact in persistence.calls[1]["result"].artifacts} >= {
        "screenshot",
        "dom",
        "ui_snapshot",
        "action_trace",
    }


async def test_initial_blank_fragment_fails_closed_before_persistence() -> None:
    class BlankFragmentBrowser:
        def __init__(self):
            self.requests = []

        async def capture(self, request):
            self.requests.append(request)
            result = _page_result(mode=request.mode, final_url="about:blank#unexpected")
            return replace(
                result,
                artifacts=[
                    artifact
                    for artifact in result.artifacts
                    if request.capture_screenshot or artifact.artifact_type != "screenshot"
                ],
            )

    browser = BlankFragmentBrowser()
    persistence = RecordingPersistence()
    writer = BrowserCaptureWriter(
        browser=browser,
        persistence=persistence,
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    with pytest.raises(RuntimeError, match="空白 Surface 已漂移"):
        await writer.capture(
            CaptureWriteRequest(
                task_id="task-1",
                agent_run_id="run-1",
                session_id="web:run-1",
                surface_kind=SurfaceKind.WEB,
                intent_id="web-entry:run-1",
                phase=CapturePhase.BEFORE_ACTION,
                directive=CaptureDirective(
                    role="before_action",
                    artifact_types=("screenshot", "dom", "ui_snapshot"),
                ),
                parent_capture_id=None,
                action_execution_id=None,
                session_command_sequence=1,
                finalization_epoch=0,
                deadline=datetime.now(UTC) + timedelta(minutes=1),
                bootstrap_blank_surface=True,
            )
        )

    assert browser.requests[0].capture_screenshot is False
    assert persistence.calls == []


async def test_real_chromium_pristine_blank_capture_has_dom_without_screenshot() -> None:
    class RealChromeCaptureBrowser(PlaywrightPageCaptureBrowser):
        async def _launch_browser(self, playwright):
            return await launch_web_test_browser(playwright.chromium, headless=True)

    browser = RealChromeCaptureBrowser(default_timeout_ms=10_000)
    session_id = "web:blank-browser-proof"
    try:
        result = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.OBSERVE,
                capture_dom=True,
                capture_visible_text=False,
                capture_screenshot=False,
                full_page=False,
                session_id=session_id,
                bootstrap_blank_surface=True,
                timeout_ms=10_000,
            )
        )
    finally:
        await browser.close_session(session_id)

    assert result.execution_status == "success"
    assert result.final_url == "about:blank"
    assert "dom" in {artifact.artifact_type for artifact in result.artifacts}
    assert "screenshot" not in {artifact.artifact_type for artifact in result.artifacts}


async def test_detail_navigate_uses_real_chromium_goto_without_click() -> None:
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b"<main><article>detail</article></main>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    destination = f"http://127.0.0.1:{server.server_port}/article/41"

    class RequestRecordingBrowser(PlaywrightPageCaptureBrowser):
        def __init__(self):
            super().__init__(default_timeout_ms=10_000)
            self.requests = []

        async def capture_with_timeout(self, request, *, timeout_seconds):
            self.requests.append(request)
            return await super().capture_with_timeout(request, timeout_seconds=timeout_seconds)

        async def _launch_browser(self, playwright):
            return await launch_web_test_browser(playwright.chromium, headless=True)

    browser = RequestRecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                destination_url=destination,
                target_source="web_detail_candidate_url",
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )
    try:
        result = await executor.execute(
            ExecuteInteractionRequest(
                intent=_intent(
                    operation=InteractionOperation.NAVIGATE,
                    parameters={
                        "target_mode": "web_detail_candidate_url",
                        "destination_url": destination,
                        "full_page": False,
                    },
                ),
                agent_run_id="run-real-detail-goto",
                source_capture_id="capture-before",
            )
        )
    finally:
        await browser.close_session("web:run-1")
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert not thread.is_alive()
    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert requests.count("/article/41") == 1
    assert len(browser.requests) == 1
    assert browser.requests[0].mode is PageCaptureMode.GOTO
    assert browser.requests[0].trigger_action is None
    assert browser.requests[0].selector is None


async def test_h5_bootstrap_persists_navigation_retry_audit_on_after_capture() -> None:
    class RetryAuditBrowser(RecordingBrowser):
        async def capture(self, request):
            result = await super().capture(request)
            if request.mode is PageCaptureMode.GOTO:
                return replace(
                    result,
                    metrics={
                        **result.metrics,
                        "navigation_retry": {
                            "schema_version": "navigation_retry.v1",
                            "max_attempts": 2,
                            "attempt_count": 2,
                            "retry_count": 1,
                            "retry_reasons": ["err_empty_response"],
                            "outcome": "succeeded_after_retry",
                        },
                    },
                )
            return result

    browser = RetryAuditBrowser()
    persistence = RecordingPersistence()
    buffer = InMemoryActionEvidenceBuffer()
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser,
            persistence=persistence,
            evidence_buffer=buffer,
        ),
        interaction_executor=BrowserInteractionExecutor(
            browser=browser,
            target_resolver=RecordingResolver(
                ResolvedInteractionTarget(destination_url="https://m.example.test/home")
            ),
            evidence_buffer=buffer,
        ),
        evidence_exit_gate=NoopEvidenceExitGate(),
    )
    intent = replace(
        _intent(
            operation=InteractionOperation.NAVIGATE,
            parameters={
                "destination_url": "https://m.example.test/home",
                "initial_surface_action": True,
            },
            semantic_target="H5 任务锁定入口",
            purpose="打开任务提交时锁定的 H5 目标 URL",
        ),
        source_capture_id="h5-entry:run-1",
    )

    await coordinator.coordinate(CaptureCoordinationCommand(agent_run_id="run-1", intent=intent))

    assert persistence.calls[1]["result"].metrics["navigation_retry"] == {
        "schema_version": "navigation_retry.v1",
        "max_attempts": 2,
        "attempt_count": 2,
        "retry_count": 1,
        "retry_reasons": ["err_empty_response"],
        "outcome": "succeeded_after_retry",
    }


@pytest.mark.parametrize("stage", ["web_entry_probe", "web_primary_content_evidence"])
async def test_web_bootstrap_threads_desktop_profile_through_dual_capture(stage) -> None:
    """首个 before Capture、导航动作和 after Capture 必须使用同一桌面配置。"""
    browser = RecordingBrowser()
    persistence = RecordingPersistence()
    buffer = InMemoryActionEvidenceBuffer()
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser,
            persistence=persistence,
            evidence_buffer=buffer,
        ),
        interaction_executor=BrowserInteractionExecutor(
            browser=browser,
            target_resolver=RecordingResolver(
                ResolvedInteractionTarget(destination_url="https://example.test/dashboard")
            ),
            evidence_buffer=buffer,
        ),
        evidence_exit_gate=NoopEvidenceExitGate(),
    )
    intent = replace(
        _intent(
            operation=InteractionOperation.NAVIGATE,
            parameters={
                "destination_url": "https://example.test/dashboard",
                "initial_surface_action": True,
                "viewport_width": 1920,
                "viewport_height": 1080,
                "screen_width": 1920,
                "screen_height": 1080,
                "device_scale_factor": 1.0,
                "is_mobile": False,
                "has_touch": False,
                "color_scheme": "light",
                "web_capture_stage": stage,
                "clear_obstructions": False,
            },
            semantic_target="Web 任务锁定入口",
            purpose="打开任务提交时锁定的桌面 Web 目标 URL",
        ),
        source_capture_id="web-entry:run-1",
    )

    await coordinator.coordinate(CaptureCoordinationCommand(agent_run_id="run-1", intent=intent))

    assert browser.requests[0].bootstrap_blank_surface is True
    assert [request.web_capture_stage for request in browser.requests] == [
        stage,
        stage,
        stage,
    ]
    assert all(request.clear_obstructions is False for request in browser.requests)
    assert [
        {
            "viewport": (request.viewport_width, request.viewport_height),
            "screen": (request.screen_width, request.screen_height),
            "device_scale_factor": request.device_scale_factor,
            "is_mobile": request.is_mobile,
            "has_touch": request.has_touch,
            "color_scheme": request.color_scheme,
        }
        for request in browser.requests
    ] == [
        {
            "viewport": (1920, 1080),
            "screen": (1920, 1080),
            "device_scale_factor": 1.0,
            "is_mobile": False,
            "has_touch": False,
            "color_scheme": "light",
        }
    ] * 3


async def test_visual_resolved_target_translates_to_coordinate_capture_request() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                click_x_ratio=0.5,
                click_y_ratio=0.45,
                left_ratio=0.1,
                top_ratio=0.4,
                right_ratio=0.9,
                bottom_ratio=0.5,
                visual_confidence=0.92,
                visual_source_url="https://example.test/profile",
                visual_entry_kind="profile_vip_payment",
                visual_expected_text="立即开通",
                screenshot_index=1,
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                parameters={"navigation_depth": 1},
                semantic_target="立即开通",
                purpose="探索我的页会员支付入口：立即开通",
            ),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    request = browser.requests[0]
    assert request.selector is None
    assert request.click_x_ratio == 0.5
    assert request.click_y_ratio == 0.45
    assert request.left_ratio == 0.1
    assert request.top_ratio == 0.4
    assert request.right_ratio == 0.9
    assert request.bottom_ratio == 0.5
    assert request.visual_confidence == 0.92
    assert request.visual_source_url == "https://example.test/profile"
    assert request.visual_entry_kind == "profile_vip_payment"
    assert request.visual_expected_text == "立即开通"
    assert request.screenshot_index == 1


async def test_verified_dom_click_is_forwarded_to_browser_request() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector="#snapshot-post-title",
                verified_card_activation="post_card",
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                semantic_target="当前列表首个内容卡片",
                purpose="从帖子列表点击标题进入详情",
            ),
            agent_run_id="run-1",
            source_capture_id="capture-post-list",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert browser.requests[0].selector == "#snapshot-post-title"
    assert browser.requests[0].verified_card_activation == "post_card"


async def test_bottom_navigation_identity_is_forwarded_to_browser_request() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector="ul.pages-foot-fix:visible > li:nth-of-type(2):visible"
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                semantic_target="社区",
                purpose="探索视频首页底部社区入口",
                parameters={
                    "target_mode": "bottom_navigation",
                    "navigation_depth": 1,
                    "navigation_family": "pages_foot",
                    "navigation_label": "社区",
                },
            ),
            agent_run_id="run-1",
            source_capture_id="capture-home",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert browser.requests[0].navigation_family == "pages_foot"
    assert browser.requests[0].navigation_label == "社区"


async def test_trusted_navigation_binding_is_forwarded_to_live_browser_preflight() -> None:
    browser = RecordingBrowser()
    identity = "sha256:" + "1" * 64
    state = "sha256:" + "2" * 64
    signature = "sha256:" + "3" * 64
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector="ul.pages-foot-fix:visible > li:nth-of-type(2):visible",
                target_source="dom",
                target_signature=signature,
                source_state_fingerprint=state,
                target_trust="trusted",
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                semantic_target="社区",
                purpose="探索视频首页底部社区入口",
                parameters={
                    "target_mode": "bottom_navigation",
                    "navigation_depth": 1,
                    "navigation_family": "pages_foot",
                    "navigation_label": "社区",
                    "logical_target": {
                        "protocol_version": "logical_target.v1",
                        "source_identity_fingerprint": identity,
                        "source_state_fingerprint": state,
                        "source_page_type": "home",
                        "expected_page_type": "content_list",
                        "expects_logical_navigation": True,
                    },
                },
            ),
            agent_run_id="run-1",
            source_capture_id="capture-home",
        )
    )

    binding = browser.requests[0]._logical_target_binding
    assert binding is not None
    assert binding.source_identity_fingerprint == identity
    assert binding.source_state_fingerprint == state
    assert binding.target_signature == signature


async def test_scroll_without_observed_movement_is_blocked() -> None:
    class NoEffectScrollBrowser:
        async def capture(self, request):
            return _page_result(
                mode=request.mode,
                action_trace=[
                    {
                        "action": "scroll",
                        "state_diff": {
                            "scroll_effect": {
                                "protocol_version": "scroll-effect.v1",
                                "observed": False,
                                "scroll_top_before": 300,
                                "scroll_top_after": 300,
                            }
                        },
                    }
                ],
            )

    state = "sha256:" + "2" * 64
    executor = BrowserInteractionExecutor(
        browser=NoEffectScrollBrowser(),
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                target_source="platform_action",
                target_signature="sha256:" + "3" * 64,
                source_state_fingerprint=state,
                target_trust="trusted",
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                operation=InteractionOperation.SCROLL,
                semantic_target="向下滚动一屏查找视频标题",
                purpose="滚动后重新观察当前列表",
                parameters={
                    "logical_target": {
                        "protocol_version": "logical_target.v1",
                        "source_identity_fingerprint": "sha256:" + "1" * 64,
                        "source_state_fingerprint": state,
                        "source_page_type": "video_list",
                        "expected_page_type": None,
                        "expects_logical_navigation": False,
                        "expects_scroll_effect": True,
                    }
                },
            ),
            agent_run_id="run-1",
            source_capture_id="capture-video-list",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "SCROLL_EFFECT_NOT_OBSERVED"


async def test_popup_dismissal_allows_verified_high_z_obstruction_handling() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector=".van-overlay:visible",
                allow_high_z_obstruction_hide=True,
                h5_popup_dismissal_strategy="safe_outside_then_close",
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                semantic_target="关闭当前弹窗",
                purpose="关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                parameters={
                    "target_mode": "popup_dismissal",
                    "navigation_depth": 0,
                    "popup_dismissal_capability": "dialog_overlay",
                },
            ),
            agent_run_id="run-1",
            source_capture_id="capture-popup",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert browser.requests[0].selector == ".van-overlay:visible"
    assert browser.requests[0].allow_high_z_obstruction_hide is True
    assert browser.requests[0].h5_popup_dismissal_strategy == "safe_outside_then_close"


async def test_verified_flutter_settings_back_uses_page_control_instead_of_history() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#settings-back")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                operation=InteractionOperation.BACK,
                semantic_target="browser_history",
                purpose="子页质检完成后返回来源页继续探索",
                parameters={"navigation_depth": 1},
            ),
            agent_run_id="run-1",
            source_capture_id="capture-settings",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert browser.requests[0].trigger_action is PageTriggerAction.CLICK
    assert browser.requests[0].selector == "#settings-back"


async def test_web_interaction_caps_long_task_deadline_to_action_budget() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#close")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        action_timeout_seconds=30.0,
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(deadline=datetime.now(UTC) + timedelta(hours=1)),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    timeout_ms = browser.requests[0].timeout_ms
    assert 29_000 <= timeout_ms <= 30_000


async def test_web_interaction_defaults_long_task_deadline_to_180_second_budget() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#close")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(deadline=datetime.now(UTC) + timedelta(hours=1)),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    timeout_ms = browser.requests[0].timeout_ms
    assert 179_000 <= timeout_ms <= 180_000


async def test_web_interaction_keeps_shorter_task_deadline() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#close")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        action_timeout_seconds=30.0,
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(deadline=datetime.now(UTC) + timedelta(seconds=5)),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    timeout_ms = browser.requests[0].timeout_ms
    assert 4_000 <= timeout_ms <= 5_000


async def test_web_interaction_action_budget_also_bounds_stalled_resolver() -> None:
    class StalledResolver:
        async def resolve(self, request):
            del request
            await asyncio.Event().wait()

    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=StalledResolver(),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        action_timeout_seconds=0.01,
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(deadline=datetime.now(UTC) + timedelta(hours=1)),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.TIMED_OUT
    assert result.error_code == "ACTION_TIMED_OUT"
    assert result.retryable is True
    assert browser.requests == []


@pytest.mark.parametrize("session_id", ["h5:run-1", "web-v2:run-1"])
async def test_failed_h5_popup_preserves_post_action_evidence_without_success(session_id):
    trace = [
        {
            "action": "click",
            "result": "popup_dismissal_not_observed",
            "dismissal_verified": False,
            "popup_dismissal_outcome": "original_layer_still_visible",
        }
    ]

    class BlockedBrowser:
        async def capture(self, request):
            error = BlockedPageCaptureActionError(
                "点击后原层未关闭",
                error_code="POPUP_DISMISSAL_NOT_OBSERVED",
            )
            error._h5_popup_action_trace = trace
            raise error

    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=BlockedBrowser(),
        evidence_buffer=buffer,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#close")),
    )
    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=replace(_intent(), session_id=session_id),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )
    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "POPUP_DISMISSAL_NOT_OBSERVED"
    evidence = await buffer.get(action_execution_id=result.action_execution_id)
    if session_id.startswith("h5:"):
        assert result.state_summary["post_action_evidence_available"] is True
        assert evidence.browser_result.action_trace == trace
    else:
        assert not result.state_summary.get("post_action_evidence_available")
        assert evidence is None


async def test_web_interaction_rejects_expired_task_deadline_before_browser_call() -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#close")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(deadline=datetime.now(UTC) - timedelta(seconds=1)),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.TIMED_OUT
    assert result.error_code == "ACTION_DEADLINE_EXCEEDED"
    assert browser.requests == []


@pytest.mark.parametrize(
    "error_code",
    [
        "POPUP_DISMISSAL_NOT_FOUND",
        "POPUP_DISMISSAL_NOT_OBSERVED",
        "STARTUP_AD_DISMISSAL_NOT_FOUND",
        "STARTUP_AD_DISMISSAL_NOT_OBSERVED",
        "TOPMOST_OBSTRUCTION_STILL_VISIBLE",
        "LOGICAL_SOURCE_STATE_STALE",
        "VISUAL_SOURCE_PAGE_STALE",
        "VISUAL_TARGET_NOT_AT_COORDINATE",
        "VISUAL_TARGET_SOURCE_STATE_STALE",
        "VISUAL_TARGET_NOT_UNIQUE",
    ],
)
async def test_obstruction_blocked_results_remain_retryable_for_h5_reobservation(
    error_code: str,
) -> None:
    class BlockedBrowser:
        async def capture(self, request):
            del request
            raise BlockedPageCaptureActionError("遮挡状态变化", error_code=error_code)

    executor = BrowserInteractionExecutor(
        browser=BlockedBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#close")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == error_code
    assert result.retryable is True


@pytest.mark.parametrize(
    "error_code",
    [
        "BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED",
        "FLUTTER_BOTTOM_NAVIGATION_NO_EFFECT",
    ],
)
async def test_bottom_navigation_no_effect_preserves_specific_nonretryable_block_code(
    error_code: str,
) -> None:
    """底栏无效果必须穿透 Runtime 保留专用码，不能被折叠为通用请求拒绝。"""

    class BlockedBrowser:
        async def capture(self, request):
            del request
            raise BlockedPageCaptureActionError(
                "底栏点击未观察到路由或激活状态变化",
                error_code=error_code,
            )

    executor = BrowserInteractionExecutor(
        browser=BlockedBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#tab")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == error_code
    assert result.retryable is False


async def test_click_target_obstruction_remains_retryable_for_h5_reobservation() -> None:
    class ObstructedBrowser:
        async def capture(self, request):
            del request
            raise PageInteractionFailedError(
                "点击目标被晚到弹窗遮挡",
                error_code="CLICK_TARGET_OBSTRUCTED",
                retryable=True,
            )

    executor = BrowserInteractionExecutor(
        browser=ObstructedBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#entry")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "CLICK_TARGET_OBSTRUCTED"
    assert result.retryable is True


async def test_verified_card_stale_preserves_controlled_runtime_diagnostics() -> None:
    """验签卡片 stale 应保留受控几何诊断，便于真实链路审计。"""

    class StaleCardBrowser:
        async def capture(self, request):
            del request
            raise PageInteractionFailedError(
                '验签结构变化：{"centerTargetTag": "DIV"}',
                error_code="VERIFIED_CARD_TARGET_STALE",
                retryable=True,
            )

    executor = BrowserInteractionExecutor(
        browser=StaleCardBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#entry")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "VERIFIED_CARD_TARGET_STALE"
    assert result.error_message == '验签结构变化：{"centerTargetTag": "DIV"}'
    assert result.retryable is True


async def test_input_intent_is_translated_to_fill_without_exposing_text_in_trace() -> None:
    browser = RecordingBrowser()
    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(selector="#comment", input_text="中性测试")
        ),
        evidence_buffer=buffer,
    )
    intent = _intent(
        operation=InteractionOperation.INPUT,
        parameters={"input_text": "中性测试"},
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=intent,
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert browser.requests[0].trigger_action is PageTriggerAction.FILL
    assert browser.requests[0].input_text == "中性测试"
    assert browser.requests[0].capture_transient_messages is True
    assert "中性测试" not in str(result.to_dict())


@pytest.mark.parametrize(
    "operation",
    [
        InteractionOperation.CLICK,
        InteractionOperation.TAP,
    ],
)
async def test_ad_outbound_intent_rejects_legacy_click_before_browser(operation) -> None:
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#ad")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )
    intent = _intent(
        operation=operation,
        parameters={
            "navigation_depth": 1,
            "capture_ad_landing_evidence": True,
            "ad_landing_capability": "ad_outbound",
        },
        semantic_target="广告外链入口",
        purpose="采集广告落地页",
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=intent,
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "WEB_AD_CLICK_FORBIDDEN"
    assert browser.requests == []


def test_browser_request_builder_cannot_generate_legacy_ad_click():
    from services.api.app.infrastructure.browser.capture_runtime import (
        _build_browser_action_request,
    )

    with pytest.raises(BlockedPageCaptureActionError) as caught:
        _build_browser_action_request(
            ExecuteInteractionRequest(
                intent=_intent(
                    parameters={
                        "capture_ad_landing_evidence": True,
                        "navigation_depth": 1,
                        "ad_landing_capability": "ad_outbound",
                    },
                    semantic_target="广告外链入口",
                    purpose="采集广告落地页",
                ),
                agent_run_id="run-1",
                source_capture_id="capture-before",
            ),
            target=ResolvedInteractionTarget(selector="#ad"),
            timeout_seconds=1,
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"


def test_h5_list_scroll_request_preserves_split_bounded_capture() -> None:
    from services.api.app.infrastructure.browser.capture_runtime import (
        _build_browser_action_request,
    )

    request = _build_browser_action_request(
        ExecuteInteractionRequest(
            intent=_intent(
                operation=InteractionOperation.SCROLL,
                parameters={
                    "target_mode": "h5_list_scroll_coverage",
                    "scroll_max_segments": 8,
                    "split_scroll_screenshots": True,
                },
                semantic_target="采集列表至底部",
                purpose="逐屏采集",
            ),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        ),
        target=ResolvedInteractionTarget(),
        timeout_seconds=20,
    )
    assert request.scroll_max_segments == 8
    assert request.split_scroll_screenshots is True


async def test_legacy_ad_click_never_persists_landing_sequence() -> None:
    from services.api.app.application.capture_coordinator import CaptureCoordinationError

    browser = RecordingBrowser()
    persistence = RecordingPersistence()
    buffer = InMemoryActionEvidenceBuffer()
    coordinator = CaptureCoordinator(
        capture_writer=BrowserCaptureWriter(
            browser=browser,
            persistence=persistence,
            evidence_buffer=buffer,
        ),
        interaction_executor=BrowserInteractionExecutor(
            browser=browser,
            target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#ad")),
            evidence_buffer=buffer,
        ),
        evidence_exit_gate=NoopEvidenceExitGate(),
    )

    with pytest.raises(CaptureCoordinationError) as caught:
        await coordinator.coordinate(
            CaptureCoordinationCommand(
                agent_run_id="run-1",
                intent=_intent(
                    parameters={
                        "navigation_depth": 1,
                        "capture_ad_landing_evidence": True,
                        "ad_landing_capability": "ad_outbound",
                    },
                    semantic_target="广告外链入口",
                    purpose="采集广告落地页",
                ),
            )
        )
    assert caught.value.error_code == "WEB_AD_CLICK_FORBIDDEN"
    assert len(persistence.calls) == 1
    assert browser.requests[0].mode is PageCaptureMode.OBSERVE


async def test_seek_media_intent_is_translated_to_bounded_browser_drag() -> None:
    browser = RecordingBrowser()
    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#video-progress")),
        evidence_buffer=buffer,
    )
    intent = _intent(
        operation=InteractionOperation.SEEK_MEDIA,
        parameters={"target_fraction": 0.5},
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=intent,
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    request = browser.requests[0]
    assert request.trigger_action is PageTriggerAction.SEEK_MEDIA
    assert request.selector == "#video-progress"
    assert request.seek_fraction == 0.5
    assert request.seek_time_seconds is None


async def test_driver_exception_details_do_not_leak_to_explorer_result() -> None:
    class FailingBrowser:
        async def capture(self, request):
            del request
            raise RuntimeError("selector=#private token=secret")

    executor = BrowserInteractionExecutor(
        browser=FailingBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#private")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "SURFACE_DRIVER_FAILED"
    assert result.error_message == "Surface Driver 执行失败"
    assert dict(result.state_summary) == {"cause_type": "RuntimeError"}
    assert "private" not in str(result.to_dict())
    assert "secret" not in str(result.to_dict())


async def test_playwright_navigation_connection_refused_is_not_reported_as_driver_failure() -> None:
    from playwright.async_api import Error as PlaywrightError

    class RefusingBrowser:
        async def capture(self, request):
            del request
            raise PlaywrightError(
                "Page.goto: net::ERR_CONNECTION_REFUSED at https://private.invalid/path"
            )

    executor = BrowserInteractionExecutor(
        browser=RefusingBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#next")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.FAILED
    assert result.error_code == "TARGET_CONNECTION_REFUSED"
    assert result.retryable is False
    assert result.error_message == "目标站点拒绝连接"
    assert dict(result.state_summary) == {
        "cause_type": "Error",
        "navigation_error": "ERR_CONNECTION_REFUSED",
    }
    assert "private.invalid" not in str(result.to_dict())


async def test_playwright_timeout_is_reported_as_retryable_action_timeout() -> None:
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    class TimingOutBrowser:
        async def capture(self, request):
            del request
            raise PlaywrightTimeoutError("Page.goto: Timeout 20000ms exceeded at /secret/path")

    executor = BrowserInteractionExecutor(
        browser=TimingOutBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#next")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.TIMED_OUT
    assert result.error_code == "ACTION_TIMED_OUT"
    assert result.retryable is True
    assert result.error_message == "浏览器动作超时"
    assert "/secret/path" not in str(result.to_dict())


async def test_semantic_resolver_requires_unique_current_snapshot_target() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {"selector": "#login", "tag": "button", "text": "登录"},
                {"selector": "#cancel", "tag": "button", "text": "取消"},
            ]
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="登录",
            parameters={},
        )
    )

    assert target.selector == "#login"
    assert reader.calls == [("task-1", "capture-1")]

    reader.snapshot = {
        "elements": [
            {"selector": "#login-a", "tag": "button", "text": "登录"},
            {"selector": "#login-b", "tag": "button", "text": "登录"},
        ]
    }
    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-2",
                action_type=InteractionOperation.CLICK,
                semantic_target="登录",
                parameters={},
            )
        )
    assert error.value.error_code == "SEMANTIC_TARGET_AMBIGUOUS"


async def test_semantic_resolver_prefers_compact_video_card_over_broad_panels() -> None:
    target_text = "大长腿学生妹被学长抓住把柄的"
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#outer-tab",
                    "tag": "div",
                    "role": "tabpanel",
                    "interactive": True,
                    "text": f"分类 导航 {target_text} 第一条内容 第二条内容 第三条内容",
                },
                {
                    "selector": "#inner-tab",
                    "tag": "div",
                    "role": "tabpanel",
                    "interactive": True,
                    "text": f"热门推荐 最新上架 {target_text} 第一条内容 第二条内容",
                },
                {
                    "selector": ".horizontalVideoBox.videoBox",
                    "tag": "div",
                    "class_tokens": ["horizontalVideoBox", "videoBox"],
                    "interactive": True,
                    "text": f"2311.8W 31:45 {target_text} 少女 评论162",
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    resolved = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=target_text,
            parameters={},
        )
    )

    assert resolved.selector == ".horizontalVideoBox.videoBox"


async def test_semantic_resolver_accepts_exact_btn_class_custom_click_surface() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": (
                        "div:nth-of-type(1) > div:nth-of-type(3) > "
                        "div:nth-of-type(2) > div > div:nth-of-type(2)"
                    ),
                    "tag": "div",
                    "text": "去签到",
                    "class_tokens": ["btn"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="去签到",
            parameters={},
        )
    )

    assert target.selector == (
        "div:nth-of-type(1) > div:nth-of-type(3) > div:nth-of-type(2) > div > div:nth-of-type(2)"
    )


async def test_semantic_resolver_accepts_below_fold_target_with_untrusted_hit_test() -> None:
    """视口下缘之下的元素 elementFromPoint 必然打到别处，命中测试不可信。

    真实回归：/mine 页「系统设置」rect.y=852 于 844 视口被误判遮挡，
    18 个 SEMANTIC_TARGET_NOT_FOUND 里的稳定入口全属此类；执行端
    Playwright 点击前会滚动到元素，签发安全。
    """
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "div > div:nth-of-type(2) > div:nth-of-type(9) > div > span",
                    "tag": "span",
                    "text": "系统设置",
                    "class_tokens": ["label"],
                    "interactive": True,
                    "rect": {"x": 65, "y": 852.55, "width": 48, "height": 13.8},
                    "hit_test": {"center_hits_target": False},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="系统设置",
            parameters={},
        )
    )

    assert target.selector == ("div > div:nth-of-type(2) > div:nth-of-type(9) > div > span")


async def test_semantic_resolver_still_rejects_occluded_in_viewport_target() -> None:
    """命中测试在视口内打到别处仍视为被遮挡，保持拒绝语义不放松。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#occluded-entry",
                    "tag": "span",
                    "text": "系统设置",
                    "class_tokens": ["label"],
                    "interactive": True,
                    "rect": {"x": 65, "y": 400, "width": 48, "height": 13.8},
                    "hit_test": {"center_hits_target": False},
                }
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-profile",
                action_type=InteractionOperation.CLICK,
                semantic_target="系统设置",
                parameters={},
            )
        )

    assert error.value.error_code == "SEMANTIC_TARGET_NOT_FOUND"


async def test_popup_virtual_target_requires_signed_capability_before_snapshot() -> None:
    """平台弹窗虚拟目标缺少能力参数时不得退化为普通文本查找。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#close",
                    "tag": "button",
                    "text": "关闭当前弹窗",
                    "interactive": True,
                }
            ]
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-popup",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前弹窗",
                parameters={},
            )
        )

    assert error.value.error_code == "ACTION_CAPABILITY_MISSING"
    assert error.value.retryable is True
    assert reader.calls == []


async def test_profile_settings_navigation_resolves_unique_btn_edit_element() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "header > span:nth-of-type(1)",
                    "tag": "span",
                    "text": "",
                    "class_tokens": ["avatar-action"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "header > span:nth-of-type(2)",
                    "tag": "span",
                    "text": "",
                    "class_tokens": ["btn-edit"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="span.btn-edit:visible",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "btn_edit",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        )
    )

    assert target.selector == "header > span:nth-of-type(2)"


async def test_profile_payment_navigation_resolves_same_node_banner_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#mine .vipBoxsBgImg img",
                    "tag": "img",
                    "text": "",
                    "class_tokens": [],
                    "ancestor_class_tokens": [
                        "animationImg",
                        "vipBoxsBgImg",
                        "vip_box",
                        "payment_me_tab",
                    ],
                    "interactive": False,
                    "rect": {"x": 16, "y": 202, "width": 356, "height": 71},
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心会员支付入口",
            parameters={
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "payment_me_tab_same_node_image",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        )
    )

    assert target.selector == "#mine .vipBoxsBgImg img"


async def test_profile_payment_navigation_resolves_vip_center_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".recommend > img",
                    "tag": "img",
                    "src": "https://example.test/static/img/my-vip-center-ad.png",
                    "class_tokens": ["img"],
                    "ancestor_class_tokens": ["recommend"],
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": ".img-menus-wrap > .left > img",
                    "tag": "img",
                    "src": "https://example.test/static/img/my-vip-center.a83f5aad.png",
                    "class_tokens": ["img"],
                    "ancestor_class_tokens": ["left", "img-menus-wrap"],
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-vip-center",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心会员支付入口",
            parameters={
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "profile_vip_center_image",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        )
    )

    assert target.selector == ".img-menus-wrap > .left > img"


async def test_profile_payment_navigation_resolves_site_three_immediate_open_text() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".mine-vip-banner > .mine-vip-banner__btn",
                    "tag": "div",
                    "text": "立即开通",
                    "class_tokens": ["mine-vip-banner__btn"],
                    "parent_class_tokens": ["vip-center", "mine-vip-banner"],
                    "parent_selector": ".vip-center.mine-vip-banner",
                    "ancestor_class_tokens": ["vip-center", "mine-vip-banner"],
                    "hit_test": {
                        "center_target": ".vip-center.mine-vip-banner",
                        "center_target_class_tokens": [
                            "vip-center",
                            "mine-vip-banner",
                        ],
                        "center_target_ancestor_class_tokens": ["mine-content"],
                        "center_target_inactive_launch_shell": False,
                        "center_hits_target": False,
                        "visual_occlusion_detected": False,
                        "pointer_interception": False,
                    },
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-site-three-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心会员支付入口",
            parameters={
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "mine_vip_banner_immediate_open_text",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        )
    )

    assert target.selector == ".vip-center.mine-vip-banner"


async def test_logical_profile_payment_binds_site_three_banner_parent() -> None:
    parent_selector = ".vip-center.mine-vip-banner"
    parent_rect = {"x": 16.0, "y": 246.0, "width": 358.0, "height": 55.0}
    reader = StaticSnapshotReader(
        {
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "identity_fingerprint": "sha256:" + "1" * 64,
                "state_fingerprint": "sha256:" + "2" * 64,
            },
            "elements": [
                {
                    "selector": ".mine-vip-banner > .mine-vip-banner__btn",
                    "tag": "div",
                    "text": "立即开通",
                    "class_tokens": ["mine-vip-banner__btn"],
                    "parent_class_tokens": ["vip-center", "mine-vip-banner"],
                    "parent_selector": parent_selector,
                    "ancestor_class_tokens": ["vip-center", "mine-vip-banner"],
                    "rect": {"x": 274.0, "y": 260.0, "width": 78.0, "height": 27.0},
                    "hit_test": {
                        "center_target": parent_selector,
                        "center_target_class_tokens": [
                            "vip-center",
                            "mine-vip-banner",
                        ],
                        "center_target_inactive_launch_shell": False,
                        "center_hits_target": False,
                        "visual_occlusion_detected": False,
                        "pointer_interception": False,
                    },
                },
                {
                    "selector": parent_selector,
                    "tag": "div",
                    "text": "立即开通",
                    "class_tokens": ["vip-center", "mine-vip-banner"],
                    "parent_class_tokens": ["mine-content"],
                    "ancestor_class_tokens": ["mine-content"],
                    "interactive": False,
                    "rect": parent_rect,
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=reader,
        logical_targets_enabled=True,
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-site-three-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心会员支付入口",
            parameters={
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "mine_vip_banner_immediate_open_text",
                "source_page_type": "profile",
                "expected_page_type": "payment",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == parent_selector
    assert target.expected_rect == parent_rect
    assert target.target_trust == "trusted"


async def test_profile_payment_navigation_resolves_site_three_member_center_icon() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".my-balance-wrap > img:nth-of-type(2)",
                    "tag": "img",
                    "src": "http://example.test/assets/mine-vip-icon-C6xv1WRC.png",
                    "class_tokens": [],
                    "parent_class_tokens": ["my-balance-wrap"],
                    "ancestor_class_tokens": ["my-balance-wrap", "mine-content"],
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-site-three-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心会员支付入口",
            parameters={
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "mine_vip_center_image",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        )
    )

    assert target.selector == ".my-balance-wrap > img:nth-of-type(2)"


@pytest.mark.parametrize("button_hit", [None, False, True])
async def test_profile_payment_navigation_requires_actual_button_in_truncated_banner(
    button_hit,
) -> None:
    """横幅命中不能代替按钮命中；裁剪后仍须有真实、当前可点击的叶节点。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#mine .header .aspect-ratio-overlay",
                    "tag": "div",
                    "text": "会员中心 限时优惠 23:44:21 后结束 新人VIP限时折扣 立即开通",
                    "class_tokens": ["aspect-ratio-overlay"],
                    "ancestor_class_tokens": [
                        "aspect-ratio",
                        "header",
                        "aspect-ratio-overlay",
                    ],
                    "interactive": False,
                    "rect": {"x": 12, "y": 228, "width": 365, "height": 70},
                    "hit_test": {"center_hits_target": True},
                }
            ],
            "truncated": True,
        }
    )

    if button_hit is not None:
        reader.snapshot["elements"].append(
            {
                "selector": "#mine .header .vip-section .button",
                "tag": "div",
                "text": "立即开通",
                "class_tokens": ["button"],
                "parent_class_tokens": ["vip-section"],
                "ancestor_class_tokens": ["header", "vip-section"],
                "interactive": True,
                "rect": {"x": 220, "y": 240, "width": 100, "height": 40},
                "hit_test": {"center_hits_target": button_hit},
            }
        )
    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-profile",
        action_type=InteractionOperation.CLICK,
        semantic_target="个人中心会员支付入口",
        parameters={
            "target_mode": "profile_payment_navigation",
            "payment_entry_capability": "vip_section_immediate_open_text",
            "source_page_type": "profile",
            "expected_page_type": "payment",
        },
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)
    if button_hit is True:
        target = await resolver.resolve(request)
        assert target.selector == "#mine .header .vip-section .button"
    else:
        with pytest.raises(SemanticTargetResolutionError) as error:
            await resolver.resolve(request)
        assert error.value.error_code == "PROFILE_PAYMENT_TARGET_NOT_FOUND"


async def test_profile_settings_navigation_resolves_options_right_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "div.options > div.left > img",
                    "tag": "img",
                    "class_tokens": [],
                    "ancestor_class_tokens": ["left", "options"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "div.options > div.right > img",
                    "tag": "img",
                    "class_tokens": [],
                    "ancestor_class_tokens": ["right", "options"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-options",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心设置图标",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "options_right_image",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        )
    )

    assert target.selector == "div.options > div.right > img"


async def test_profile_settings_navigation_resolves_setting_class_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "header > div.left > img.setting",
                    "tag": "img",
                    "class_tokens": ["setting"],
                    "ancestor_class_tokens": ["left", "top", "header"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-setting-class",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心类名设置图标",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "setting_image",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        )
    )

    assert target.selector == "header > div.left > img.setting"


async def test_profile_settings_navigation_resolves_top_right_icon_setting() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".recommend img.icon-setting",
                    "tag": "img",
                    "class_tokens": ["icon", "icon-setting"],
                    "ancestor_class_tokens": ["recommend"],
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": ".top-right-menu img.icon-setting",
                    "tag": "img",
                    "class_tokens": ["icon", "icon-setting"],
                    "ancestor_class_tokens": [
                        "top-right-menu",
                        "top-wrap",
                        "base-info",
                    ],
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-top-right-setting",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心右上设置图标",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "top_right_icon_setting",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        )
    )

    assert target.selector == ".top-right-menu img.icon-setting"


async def test_profile_settings_navigation_resolves_header_second_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".header-left > img:nth-of-type(1)",
                    "tag": "img",
                    "class_tokens": ["mr-15", "wh-24"],
                    "ancestor_class_tokens": ["header-left", "index-header"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": ".header-left > img:nth-of-type(2)",
                    "tag": "img",
                    "class_tokens": ["wh-24"],
                    "ancestor_class_tokens": ["header-left", "index-header"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-header-setting",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心头部设置图标",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "header_left_second_image",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        )
    )

    assert target.selector == ".header-left > img:nth-of-type(2)"


def _xingba_startup_snapshot(*, control_text: str, control_role: str | None = None) -> dict:
    """杏吧实测开屏快照：全屏可点层 → dialog 壳 → 右上角清障控件。"""
    return {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#flt-semantic-node-1",
                "tag": "flt-semantics",
                "role": None,
                "text": control_text,
                "interactive": True,
                "parent_semantics_selector": "#flt-semantic-node-0",
                "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                "hit_test": {"center_hits_target": True},
            },
            {
                "selector": "#flt-semantic-node-2",
                "tag": "flt-semantics",
                "role": "dialog",
                "text": control_text,
                "interactive": False,
                "parent_semantics_selector": "#flt-semantic-node-1",
                "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                "hit_test": {"center_hits_target": True},
            },
            {
                "selector": "#flt-semantic-node-7",
                "tag": "flt-semantics",
                "role": control_role,
                "text": control_text,
                "interactive": True,
                "parent_semantics_selector": "#flt-semantic-node-2",
                "parent_semantics_tappable": False,
                "rect": {"x": 328, "y": 45, "width": 46, "height": 56.82},
                "style": {"z_index": "2"},
                "hit_test": {"center_hits_target": True},
            },
        ],
    }


def test_flutter_startup_enter_control_resolves_through_dialog_shell() -> None:
    """杏吧实测：清障控件写「进入」且直接父层是不可交互的 dialog 壳，仍要验签通过。"""
    snapshot = _xingba_startup_snapshot(control_text="进入")

    matches = _flutter_fullscreen_startup_skip_snapshot_matches(
        snapshot=snapshot,
        elements=snapshot["elements"],
    )

    assert [item["selector"] for item in matches] == ["#flt-semantic-node-7"]


def test_flutter_startup_skip_control_with_text_role_still_resolves() -> None:
    """原「跳过」+ role=text 的结构不能因为放宽父层解析而失效。"""
    snapshot = _xingba_startup_snapshot(control_text="跳过", control_role="text")

    matches = _flutter_fullscreen_startup_skip_snapshot_matches(
        snapshot=snapshot,
        elements=snapshot["elements"],
    )

    assert [item["selector"] for item in matches] == ["#flt-semantic-node-7"]


def test_flutter_startup_snapshot_rejects_unrelated_corner_control() -> None:
    """同样几何位置但文案不是开屏清障词，不得验签成可点的清障控件。"""
    snapshot = _xingba_startup_snapshot(control_text="客服")

    matches = _flutter_fullscreen_startup_skip_snapshot_matches(
        snapshot=snapshot,
        elements=snapshot["elements"],
    )

    assert matches == []


async def test_profile_settings_navigation_resolves_flutter_settings_arrow() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#flutter-settings-row",
                    "tag": "flt-semantics",
                    "role": "group",
                    "aria_label": "设置",
                    "text": "设置",
                    "interactive": True,
                    "rect": {"x": 16.64, "y": 949.92, "width": 356.72, "height": 59.27},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#flutter-settings-leading-image",
                    "tag": "flt-semantics",
                    "role": "img",
                    "interactive": False,
                    "parent_semantics_selector": "#flutter-settings-row",
                    "parent_semantics_aria_label": "设置",
                    "parent_semantics_tappable": True,
                    "rect": {"x": 32.24, "y": 957.72, "width": 43.67, "height": 43.67},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#flutter-settings-arrow",
                    "tag": "flt-semantics",
                    "role": "img",
                    "interactive": False,
                    "parent_semantics_selector": "#flutter-settings-row",
                    "parent_semantics_aria_label": "设置",
                    "parent_semantics_tappable": True,
                    "rect": {"x": 336.96, "y": 969.16, "width": 20.8, "height": 20.8},
                    "hit_test": {"center_hits_target": False},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-flutter",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心 Flutter 设置入口",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "flutter_settings_arrow",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        )
    )

    assert target.selector == "#flutter-settings-arrow"


async def test_flutter_settings_page_back_resolves_unique_top_left_control() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#settings-root",
                    "tag": "flt-semantics",
                    "role": "group",
                    "aria_label": "设置",
                    "interactive": True,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#settings-back",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "aria_label": None,
                    "interactive": True,
                    "parent_semantics_selector": "#settings-root",
                    "parent_semantics_aria_label": "设置",
                    "parent_semantics_tappable": True,
                    "rect": {"x": 0, "y": 5.2, "width": 67.32, "height": 35.36},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-settings",
            action_type=InteractionOperation.BACK,
            semantic_target="browser_history",
            parameters={
                "target_mode": "settings_page_back",
                "settings_page_back_capability": "flutter_settings_page_back",
                "source_page_type": "settings",
            },
        )
    )

    assert target.selector == "#settings-back"


async def test_flutter_profile_favorites_back_resolves_unique_top_left_control() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#favorites-root",
                    "tag": "flt-semantics",
                    "role": "group",
                    "aria_label": "我的收藏",
                    "interactive": True,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#favorites-back",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "aria_label": None,
                    "interactive": True,
                    "parent_semantics_selector": "#favorites-root",
                    "parent_semantics_aria_label": "我的收藏",
                    "parent_semantics_tappable": True,
                    "rect": {"x": 0, "y": 5.2, "width": 67.32, "height": 35.36},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-favorites",
            action_type=InteractionOperation.BACK,
            semantic_target="browser_history",
            parameters={
                "target_mode": "profile_favorites_page_back",
                "profile_favorites_page_back_capability": ("flutter_profile_favorites_page_back"),
                "source_page_type": "video_list",
                "expected_page_type": "profile",
            },
        )
    )

    assert target.selector == "#favorites-back"


async def test_profile_activity_navigation_resolves_task_sign_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "main > img.task_sign",
                    "tag": "img",
                    "class_tokens": ["task_sign"],
                    "ancestor_class_tokens": ["minePage"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-activity",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心签到入口",
            parameters={
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "task_sign_image",
                "source_page_type": "profile",
                "expected_page_type": "activity",
            },
        )
    )

    assert target.selector == "main > img.task_sign"


async def test_profile_activity_navigation_resolves_checkin_entry() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "main > div.checkin-entry",
                    "tag": "div",
                    "text": "签到",
                    "class_tokens": ["checkin-entry"],
                    "ancestor_class_tokens": ["mine-page-wrap"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-checkin-entry",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心签到按钮",
            parameters={
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "checkin_entry",
                "source_page_type": "profile",
                "expected_page_type": "activity",
            },
        )
    )

    assert target.selector == "main > div.checkin-entry"


async def test_profile_activity_navigation_resolves_retained_checkin_icon() -> None:
    """Snapshot 裁掉父层时，保留的中心命中图标与祖先结构仍能唯一验签。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "main > div.checkin-entry > img.checkin-entry-icon",
                    "tag": "img",
                    "text": "",
                    "class_tokens": ["checkin-entry-icon"],
                    "ancestor_class_tokens": ["checkin-entry", "mine-page-wrap"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-checkin-icon",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心签到按钮",
            parameters={
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "checkin_entry",
                "source_page_type": "profile",
                "expected_page_type": "activity",
            },
        )
    )

    assert target.selector == "main > div.checkin-entry > img.checkin-entry-icon"


async def test_profile_activity_navigation_resolves_sigin_box_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".recommend > img",
                    "tag": "img",
                    "src": "https://example.test/static/img/m-signin-bg-ad.png",
                    "class_tokens": ["img"],
                    "ancestor_class_tokens": ["recommend"],
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": ".img-menus-wrap .sigin-box > img",
                    "tag": "img",
                    "src": "https://example.test/static/img/m-signin-bg.42f9.png",
                    "class_tokens": ["img"],
                    "ancestor_class_tokens": [
                        "sigin-box",
                        "right",
                        "img-menus-wrap",
                    ],
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-sigin-box",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心每日签到图片",
            parameters={
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "daily_signin_image",
                "source_page_type": "profile",
                "expected_page_type": "activity",
            },
        )
    )

    assert target.selector == ".img-menus-wrap .sigin-box > img"


async def test_profile_activity_navigation_resolves_five_site_signin_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "main img.sign-in-entry",
                    "tag": "img",
                    "alt": "签到",
                    "class_tokens": ["sign-in-entry"],
                    "ancestor_class_tokens": ["user-card", "mine-page"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-signin-image",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心签到图片",
            parameters={
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "profile_signin_image",
                "source_page_type": "profile",
                "expected_page_type": "activity",
            },
        )
    )

    assert target.selector == "main img.sign-in-entry"


async def test_profile_activity_navigation_resolves_site_three_signin_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".sign-in > img.d-img",
                    "tag": "img",
                    "alt": "",
                    "src": "http://example.test/assets/singi-MikFGLph.png",
                    "class_tokens": ["d-img"],
                    "parent_class_tokens": ["sign-in"],
                    "ancestor_class_tokens": ["sign-in", "mine-info", "mine-content"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-site-three-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心签到图片",
            parameters={
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "profile_signin_image",
                "source_page_type": "profile",
                "expected_page_type": "activity",
            },
        )
    )

    assert target.selector == ".sign-in > img.d-img"


async def test_profile_payment_navigation_rejects_site_three_nested_banner_button() -> None:
    element: dict[str, object] = {
        "selector": ".mine-vip-banner .nested .mine-vip-banner__btn",
        "tag": "div",
        "text": "立即开通",
        "class_tokens": ["mine-vip-banner__btn"],
        "parent_class_tokens": ["nested"],
        "parent_selector": ".mine-vip-banner .nested",
        "ancestor_class_tokens": ["nested", "vip-center", "mine-vip-banner"],
        "hit_test": {"center_hits_target": True},
    }
    reader = StaticSnapshotReader({"elements": [element]})

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-site-three-profile",
                action_type=InteractionOperation.CLICK,
                semantic_target="个人中心会员支付入口",
                parameters={
                    "target_mode": "profile_payment_navigation",
                    "payment_entry_capability": "mine_vip_banner_immediate_open_text",
                    "source_page_type": "profile",
                    "expected_page_type": "payment",
                },
            )
        )

    assert error.value.error_code == "PROFILE_PAYMENT_TARGET_NOT_FOUND"


async def test_profile_activity_navigation_rejects_site_three_nested_signin_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".sign-in .nested > img.d-img",
                    "tag": "img",
                    "src": "http://example.test/assets/singi-MikFGLph.png",
                    "class_tokens": ["d-img"],
                    "parent_class_tokens": ["nested"],
                    "ancestor_class_tokens": ["nested", "sign-in"],
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-site-three-profile",
                action_type=InteractionOperation.CLICK,
                semantic_target="个人中心签到图片",
                parameters={
                    "target_mode": "profile_activity_navigation",
                    "activity_entry_capability": "profile_signin_image",
                    "source_page_type": "profile",
                    "expected_page_type": "activity",
                },
            )
        )

    assert error.value.error_code == "PROFILE_ACTIVITY_TARGET_NOT_FOUND"


async def test_profile_payment_navigation_resolves_five_site_immediate_open_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".vip-promo img.vip-promo__button",
                    "tag": "img",
                    "alt": "立即开通",
                    "class_tokens": ["vip-promo__button"],
                    "ancestor_class_tokens": ["vip-promo", "mine-page"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-vip-promo",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心会员支付入口",
            parameters={
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "vip_promo_immediate_open_image",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        )
    )

    assert target.selector == ".vip-promo img.vip-promo__button"


async def test_profile_settings_navigation_resolves_five_site_header_action_image() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".mine-header__actions img[alt='设置']",
                    "tag": "img",
                    "alt": "设置",
                    "class_tokens": ["icon-button__img"],
                    "ancestor_class_tokens": [
                        "icon-button",
                        "mine-header__actions",
                        "mine-header",
                    ],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-actions-setting",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心操作区设置图标",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "mine_header_settings_image",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        )
    )

    assert target.selector == ".mine-header__actions img[alt='设置']"


async def test_startup_ad_dismissal_resolves_only_trusted_countdown_control() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > div > div:nth-of-type(1) > span",
                    "tag": "span",
                    "text": "1s(跳过)",
                    "class_tokens": ["down-time"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-startup-ad",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
            },
        )
    )

    assert target.selector == ".loading-page .down-time:visible"
    assert reader.calls == [("task-1", "capture-startup-ad")]


async def test_startup_ad_dismissal_resolves_trusted_global_launch_skip() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > div > div > span:nth-of-type(2)",
                    "tag": "span",
                    "text": "跳过",
                    "class_tokens": [],
                    "ancestor_class_tokens": [
                        "header-title",
                        "launchSwiperContent",
                        "globalLaunch",
                    ],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-global-launch",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
            },
        )
    )

    assert target.selector == ('.globalLaunch .header-title > span:text-is("跳过"):visible')


async def test_startup_ad_dismissal_resolves_flutter_fullscreen_top_right_skip() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#flt-semantic-node-3",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "跳过",
                    "interactive": True,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-7",
                    "tag": "flt-semantics",
                    "role": "text",
                    "text": "跳过",
                    "parent_semantics_selector": "#flt-semantic-node-3",
                    "parent_semantics_tappable": True,
                    "interactive": True,
                    "rect": {"x": 312.7, "y": 10.4, "width": 61.69, "height": 36.39},
                    "style": {"z_index": "2"},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-flutter-startup-ad",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
            },
        )
    )

    assert target.selector == "internal:flutter_fullscreen_startup_ad_skip"


async def test_startup_ad_dismissal_resolves_flutter_fullscreen_top_right_enter() -> None:
    """Flutter 开屏使用“进入”文案时仍须通过唯一右上角控件验签。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#flt-semantic-node-3",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "进入",
                    "interactive": True,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-7",
                    "tag": "flt-semantics",
                    "role": "text",
                    "text": "进入",
                    "parent_semantics_selector": "#flt-semantic-node-3",
                    "parent_semantics_tappable": True,
                    "interactive": True,
                    "rect": {"x": 312.7, "y": 10.4, "width": 61.69, "height": 36.39},
                    "style": {"z_index": "2"},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-flutter-startup-enter",
            action_type=InteractionOperation.CLICK,
            semantic_target="进入",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
            },
        )
    )

    assert target.selector == "internal:flutter_fullscreen_startup_ad_skip"


@pytest.mark.parametrize(
    ("element", "expected_selector"),
    [
        (
            {
                "selector": "body > div > div.jump_over",
                "tag": "div",
                "text": "跳过",
                "class_tokens": ["jump_over"],
                "ancestor_class_tokens": ["countdown", "wrapper"],
                "interactive": False,
                "hit_test": {"center_hits_target": True},
            },
            '.countdown > .jump_over:text-is("跳过"):visible',
        ),
        (
            {
                "selector": "body > div.van-popup > div.skip-wrap",
                "tag": "div",
                "text": "0s(跳过)",
                "class_tokens": ["skip-wrap"],
                "ancestor_class_tokens": ["van-popup"],
                "interactive": False,
                "hit_test": {"center_hits_target": True},
            },
            '.van-popup:visible .skip-wrap:has(> div:text-is("0s(跳过)")):visible',
        ),
        (
            {
                "selector": "body > div.van-popup > div > div.count-down",
                "tag": "div",
                "text": "跳过",
                "class_tokens": ["count-down"],
                "ancestor_class_tokens": ["van-popup"],
                "interactive": False,
                "hit_test": {"center_hits_target": True},
            },
            '.van-popup:visible .count-down:text-is("跳过"):visible',
        ),
        (
            {
                "selector": "body > div.absolute > div.count-down",
                "tag": "div",
                "text": "跳过",
                "class_tokens": ["flex-center", "count-down"],
                "ancestor_class_tokens": ["absolute", "right-16", "top-32"],
                "interactive": False,
                "hit_test": {"center_hits_target": True},
            },
            '.count-down:text-is("跳过"):visible',
        ),
        (
            {
                "selector": "body > div.launch-page > div.screen-ad > div.button",
                "tag": "div",
                "text": "进入",
                "class_tokens": ["button"],
                "ancestor_class_tokens": ["screen-ad", "launch-page"],
                "interactive": False,
                "hit_test": {"center_hits_target": True},
            },
            '.launch-page:visible .screen-ad > .button:has(> div:text-is("进入")):visible',
        ),
    ],
)
async def test_startup_ad_dismissal_resolves_trusted_site_variants(
    element: dict[str, object],
    expected_selector: str,
) -> None:
    reader = StaticSnapshotReader({"elements": [element]})

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-startup-ad",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
            },
        )
    )

    assert target.selector == expected_selector


async def test_logical_startup_ad_dismissal_binds_jump_over_leaf_not_text_parents() -> None:
    """同文案父层不得把唯一 jump_over 结构控件误判为逻辑目标歧义。"""
    expected_rect = {"x": 307.48, "y": 41.58, "width": 34.0, "height": 16.64}
    reader = StaticSnapshotReader(
        {
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "identity_fingerprint": "sha256:" + "1" * 64,
                "state_fingerprint": "sha256:" + "2" * 64,
            },
            "elements": [
                {
                    "selector": "#appView > div > div:nth-of-type(2) > div",
                    "tag": "div",
                    "text": "跳过",
                    "class_tokens": ["jump_over"],
                    "ancestor_class_tokens": ["countdown", "wrapper", "theme-default"],
                    "interactive": False,
                    "rect": expected_rect,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#appView > div",
                    "tag": "div",
                    "text": "跳过",
                    "class_tokens": ["wrapper"],
                    "ancestor_class_tokens": ["theme-default"],
                    "interactive": False,
                    "rect": {"x": 0.0, "y": 0.0, "width": 390.0, "height": 844.0},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#appView > div > div:nth-of-type(2)",
                    "tag": "div",
                    "text": "跳过",
                    "class_tokens": ["countdown"],
                    "ancestor_class_tokens": ["wrapper", "theme-default"],
                    "interactive": False,
                    "rect": {"x": 284.97, "y": 31.19, "width": 79.03, "height": 37.44},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=reader,
        logical_targets_enabled=True,
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-countdown-jump-over",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == '.countdown > .jump_over:text-is("跳过"):visible'
    assert target.expected_rect == expected_rect
    assert target.target_source == "dom"
    assert target.target_trust == "trusted"


async def test_logical_startup_ad_dismissal_binds_skip_wrap_not_dialog_text() -> None:
    """弹窗聚合文案不得覆盖唯一 skip-wrap 开屏退出结构。"""
    expected_rect = {"x": 283.22, "y": 15.59, "width": 81.83, "height": 28.47}
    reader = StaticSnapshotReader(
        {
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "identity_fingerprint": "sha256:" + "1" * 64,
                "state_fingerprint": "sha256:" + "2" * 64,
            },
            "elements": [
                {
                    "selector": "body > div.van-popup",
                    "tag": "div",
                    "role": "dialog",
                    "text": "0s(跳过)",
                    "class_tokens": ["van-popup", "van-popup--center"],
                    "interactive": True,
                    "rect": {"x": 0.0, "y": 0.0, "width": 390.0, "height": 844.0},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "body > div.van-popup > div.skip-wrap > div",
                    "tag": "div",
                    "text": "0s(跳过)",
                    "class_tokens": ["flex-center", "rounded-100", "btnBg"],
                    "ancestor_class_tokens": ["skip-wrap", "van-popup"],
                    "interactive": False,
                    "rect": expected_rect,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "body > div.van-popup > div.skip-wrap",
                    "tag": "div",
                    "text": "0s(跳过)",
                    "class_tokens": ["skip-wrap"],
                    "ancestor_class_tokens": ["van-popup"],
                    "interactive": False,
                    "rect": expected_rect,
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=reader,
        logical_targets_enabled=True,
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup-skip-wrap",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == (
        '.van-popup:visible .skip-wrap:has(> div:text-is("0s(跳过)")):visible'
    )
    assert target.expected_rect == expected_rect
    assert target.target_source == "dom"
    assert target.target_trust == "trusted"


async def test_safety_interstitial_bypass_resolves_only_trusted_checkbox() -> None:
    reader = StaticSnapshotReader(
        {
            "title": "Bot detection",
            "elements": [
                {
                    "selector": "body > div > div > div > h2",
                    "tag": "h2",
                    "text": "This site is safe, you can visit it with confidence.",
                    "interactive": False,
                },
                {
                    "selector": "div > div > div > div > input",
                    "tag": "input",
                    "type": "checkbox",
                    "text": "",
                    "interactive": True,
                    "disabled": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "div > div > div > div > p",
                    "tag": "p",
                    "text": "Click to visit now",
                    "interactive": False,
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-safety",
            action_type=InteractionOperation.CLICK,
            semantic_target=H5_SAFETY_INTERSTITIAL_TARGET,
            parameters={
                "target_mode": H5_SAFETY_INTERSTITIAL_TARGET_MODE,
                "safety_interstitial_capability": H5_SAFETY_INTERSTITIAL_CAPABILITY,
            },
        )
    )

    assert target.selector == H5_SAFETY_INTERSTITIAL_SELECTOR
    assert reader.calls == [("task-1", "capture-safety")]


async def test_safety_interstitial_bypass_rejects_plain_checkbox_without_safety_copy() -> None:
    reader = StaticSnapshotReader(
        {
            "title": "普通表单",
            "elements": [
                {
                    "selector": "#agree",
                    "tag": "input",
                    "type": "checkbox",
                    "text": "",
                    "interactive": True,
                    "disabled": False,
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-form",
                action_type=InteractionOperation.CLICK,
                semantic_target=H5_SAFETY_INTERSTITIAL_TARGET,
                parameters={
                    "target_mode": H5_SAFETY_INTERSTITIAL_TARGET_MODE,
                    "safety_interstitial_capability": H5_SAFETY_INTERSTITIAL_CAPABILITY,
                },
            )
        )

    assert error.value.error_code == "SAFETY_INTERSTITIAL_NOT_FOUND"


async def test_startup_ad_dismissal_accepts_deterministic_close_label() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > div > div:nth-of-type(1) > span",
                    "tag": "span",
                    "text": "关闭",
                    "class_tokens": ["down-time"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-startup-ad",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
            },
        )
    )

    assert target.selector == ".loading-page .down-time:visible"


@pytest.mark.parametrize(
    ("match_count", "expected_error_code"),
    [
        (0, "STARTUP_AD_DISMISSAL_NOT_FOUND"),
        (2, "STARTUP_AD_DISMISSAL_AMBIGUOUS"),
    ],
)
async def test_startup_ad_dismissal_requires_one_trusted_countdown_control(
    match_count: int,
    expected_error_code: str,
) -> None:
    elements = [
        {
            "selector": f"#startup-ad-close-{index}",
            "tag": "span",
            "text": "1s(跳过)",
            "class_tokens": ["down-time"],
            "interactive": True,
            "hit_test": {"center_hits_target": True},
        }
        for index in range(match_count)
    ]
    reader = StaticSnapshotReader({"elements": elements})

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-startup-ad",
                action_type=InteractionOperation.CLICK,
                semantic_target="跳过",
                parameters={
                    "target_mode": "startup_ad_dismissal",
                    "startup_ad_capability": "countdown_close",
                },
            )
        )

    assert error.value.error_code == expected_error_code


async def test_popup_dismissal_resolves_signed_dialog_content_capability() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > div > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "dialog",
                    "text": "推荐内容",
                    "class_tokens": ["van-dialog"],
                    "interactive": True,
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_content_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == (
        ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
        "> .btn:visible > img:visible, "
        ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
        "> :is(.btn-box.end, .btn-box.close):visible"
    )
    assert target.allow_high_z_obstruction_hide is True
    assert target.h5_popup_dismissal_strategy == "safe_outside_then_close"
    assert target.clear_obstructions is True
    assert reader.calls == [("task-1", "capture-popup")]


async def test_popup_dismissal_resolves_signed_home_suspend_capability() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "div:nth-of-type(3)",
                    "tag": "div",
                    "text": "底部悬浮广告",
                    "class_tokens": ["home-suspend"],
                    "interactive": False,
                },
                {
                    "selector": "div:nth-of-type(3) > div:nth-of-type(1)",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["btn-close"],
                    "interactive": True,
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "home_suspend_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == ".home-suspend:visible .btn-close:visible"
    assert target.allow_high_z_obstruction_hide is True
    assert reader.calls == [("task-1", "capture-popup")]


async def test_popup_dismissal_resolves_signed_persale_overlay_capability() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > main > div > div",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["van-overlay"],
                    "ancestor_class_tokens": ["persalePopup", "home"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == ".van-overlay:visible"
    assert target.allow_high_z_obstruction_hide is True
    assert reader.calls == [("task-1", "capture-popup")]


async def test_popup_dismissal_clicks_grid_dialog_image_close_button() -> None:
    """九宫格广告弹窗只能点内部纯图关闭键，不能点遮罩背景。

    实测性巴克（SF02）：留存 DOM 里 van-dialog 内唯一关闭控件是
    <div class="btn"><img alt=""></div>，无文字无 aria-label；能力固定
    选择器 .van-overlay:visible 点上去机械成功、弹窗不动，前沿随之饿死。
    """
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > div:nth-of-type(3) > div:nth-of-type(1)",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["van-overlay"],
                    "ancestor_class_tokens": ["page-wraper"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "body > div:nth-of-type(3) > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "dialog",
                    "text": "空降上门 同城上门 魅惑迷药",
                    "class_tokens": ["van-dialog"],
                    "ancestor_class_tokens": ["page-wraper"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": (
                        "body > div:nth-of-type(3) > div:nth-of-type(2)"
                        " > div > div > div:nth-of-type(2)"
                    ),
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["btn"],
                    "ancestor_class_tokens": [
                        "grid-main",
                        "van-dialog__content",
                        "van-dialog",
                        "page-wraper",
                    ],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-grid-dialog",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "e" * 64,
            },
        )
    )

    assert target.selector == (
        "body > div:nth-of-type(3) > div:nth-of-type(2) > div > div > div:nth-of-type(2)"
    )
    assert target.allow_high_z_obstruction_hide is True


async def test_popup_dismissal_ignores_btn_outside_grid_dialog() -> None:
    """弹窗外的同名 btn 不得被当成关闭键，仍回落遮罩语义。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > main > div",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["van-overlay"],
                    "ancestor_class_tokens": ["persalePopup", "home"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "body > main > section > div",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["btn"],
                    "ancestor_class_tokens": ["grid-main", "home"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-plain-btn",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "f" * 64,
            },
        )
    )

    assert target.selector == ".van-overlay:visible"


async def test_popup_dismissal_resolves_unique_bottom_drawer_overlay() -> None:
    """底部付费抽屉没有关闭按钮时，只允许点击抽屉外的唯一全屏遮罩。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#app > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "button",
                    "class_tokens": ["van-overlay"],
                    "interactive": True,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#app > div:nth-of-type(3)",
                    "tag": "div",
                    "role": "dialog",
                    "class_tokens": [
                        "van-popup",
                        "van-popup--bottom",
                        "vip-pop-main",
                    ],
                    "interactive": True,
                    "rect": {"x": 0, "y": 338, "width": 390, "height": 506},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-bottom-drawer",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "d" * 64,
            },
        )
    )

    assert target.selector == ".van-overlay:visible"
    assert target.expected_rect == {
        "x": 0.0,
        "y": 0.0,
        "width": 390.0,
        "height": 844.0,
    }
    assert target.allow_high_z_obstruction_hide is True


async def test_h5_probe_popup_dismissal_reauthorizes_unique_bottom_drawer() -> None:
    """补采请求没有主探索签名时，仍须由来源 Snapshot 重新证明当前遮挡。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#app > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "button",
                    "class_tokens": ["van-overlay"],
                    "interactive": True,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {"center_hits_target": False},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                },
                {
                    "selector": "#app > div:nth-of-type(3)",
                    "tag": "div",
                    "role": "dialog",
                    "class_tokens": ["van-popup", "van-popup--bottom"],
                    "interactive": True,
                    "rect": {"x": 0, "y": 338, "width": 390, "height": 506},
                    "hit_test": {"center_hits_target": True},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-bottom-drawer",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={"target_mode": "h5_probe_popup_dismissal"},
        )
    )

    assert target.selector == ".van-overlay:visible"
    assert target.clear_obstructions is True
    assert target.allow_high_z_obstruction_hide is True
    assert target.h5_popup_dismissal_strategy == "safe_outside_then_close"
    assert target.target_trust == "trusted"
    assert target.trusted_target_texts == ("关闭当前弹窗",)
    assert target.trusted_target_role == "dismiss"
    assert reader.calls == [("task-1", "capture-bottom-drawer")]


async def test_h5_probe_popup_dismissal_defers_dynamic_close_to_live_dom() -> None:
    """来源快照只证明阻断弹层时，实时 DOM 仍须重新证明唯一安全出口。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#recapture-overlay",
                    "tag": "div",
                    "role": "dialog",
                    "class_tokens": ["overlay"],
                    "ancestor_class_tokens": [],
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "visible": True,
                    "style": {
                        "display": "grid",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "topmost": True,
                        "pointer_interception": True,
                    },
                },
                {
                    "selector": "#recapture-overlay > section",
                    "tag": "section",
                    "class_tokens": ["dialog"],
                    "ancestor_class_tokens": ["overlay"],
                    "rect": {"x": 25, "y": 340, "width": 340, "height": 164},
                    "visible": True,
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                    "hit_test": {
                        "center_hits_target": True,
                        "topmost": True,
                        "pointer_interception": True,
                    },
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-dynamic-close",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={"target_mode": "h5_probe_popup_dismissal"},
        )
    )

    assert target.selector == "text=关闭"
    assert target.clear_obstructions is True
    assert target.allow_high_z_obstruction_hide is True
    assert target.h5_popup_dismissal_strategy == "safe_outside_then_close"
    assert target.target_trust == "trusted"
    assert reader.calls == [("task-1", "capture-dynamic-close")]


async def test_h5_probe_popup_dismissal_preserves_entitlement_collection() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": ".van-overlay",
                    "visible": True,
                    "class_tokens": ["van-overlay"],
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "style": {"overflow_x": "visible"},
                    "text_metrics": {
                        "scroll_width": 390,
                        "client_width": 390,
                        "horizontal_overflow": False,
                    },
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": ".limited-time-offer-popup-wrapper .swiper_content",
                    "visible": True,
                    "text": "新人特惠 最受欢迎 热销",
                    "class_tokens": [
                        "swiper",
                        "swiper_content",
                        "swiper-initialized",
                        "swiper-horizontal",
                    ],
                    "ancestor_class_tokens": [
                        "swiper_box",
                        "popup-scroll",
                        "limitedTimeOfferBox",
                        "van-popup",
                    ],
                    "rect": {"x": 16, "y": 270, "width": 373, "height": 208},
                    "style": {"overflow_x": "visible"},
                    "text_metrics": {
                        "scroll_width": 1358,
                        "client_width": 373,
                        "horizontal_overflow": True,
                    },
                    "hit_test": {"center_hits_target": True, "topmost": True},
                },
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as exc_info:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-entitlement-collection",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前弹窗",
                parameters={"target_mode": "h5_probe_popup_dismissal"},
            )
        )

    assert exc_info.value.error_code == "H5_PROBE_ENTITLEMENT_COLLECTION_PRESERVED"
    assert exc_info.value.retryable is False


async def test_h5_probe_popup_dismissal_rejects_nonblocking_floating_widget() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": ".floating-service",
                    "tag": "button",
                    "role": "button",
                    "class_tokens": ["floating-service", "close"],
                    "interactive": True,
                    "rect": {"x": 340, "y": 700, "width": 40, "height": 40},
                    "hit_test": {"center_hits_target": True},
                    "style": {
                        "display": "block",
                        "visibility": "visible",
                        "opacity": "1",
                    },
                }
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as exc_info:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-floating-widget",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前弹窗",
                parameters={"target_mode": "h5_probe_popup_dismissal"},
            )
        )

    assert exc_info.value.error_code == "H5_PROBE_POPUP_DISMISSAL_NOT_PROVEN"


async def test_popup_dismissal_resolves_signed_van_center_popup_close() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#home-main-content > div",
                    "tag": "div",
                    "role": "dialog",
                    "text": "",
                    "class_tokens": ["van-popup", "van-popup--center"],
                    "ancestor_class_tokens": ["component-advertises"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#home-main-content > div > div",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["close-img"],
                    "ancestor_class_tokens": [
                        "popup-form",
                        "van-popup",
                        "van-popup--center",
                        "component-advertises",
                    ],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#home-main-content > div > div > img",
                    "tag": "img",
                    "text": "",
                    "class_tokens": ["close-image", "on-dialog-close"],
                    "ancestor_class_tokens": [
                        "close-img",
                        "popup-form",
                        "van-popup",
                        "van-popup--center",
                        "component-advertises",
                    ],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == ".van-overlay:visible"
    assert target.allow_high_z_obstruction_hide is True
    assert reader.calls == [("task-1", "capture-popup")]


async def test_popup_dismissal_resolves_signed_account_credential_confirm() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#appView > div > div:nth-of-type(3)",
                    "tag": "div",
                    "role": "dialog",
                    "text": (
                        "账号凭证 ID 19011140 提示*可通过官方客服找回账号 "
                        "请勿删除此凭证 永久官方地址 https://91porn01.cc "
                        "我已保存 立即保存"
                    ),
                    "class_tokens": ["van-popup", "van-popup--center"],
                    "ancestor_class_tokens": ["main_wrapper"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": (
                        "#appView > div > div:nth-of-type(3) > "
                        "div:nth-of-type(2) > div:nth-of-type(1)"
                    ),
                    "tag": "div",
                    "text": "我已保存",
                    "class_tokens": ["promoteBtn", "btn1"],
                    "ancestor_class_tokens": [
                        "promoteBtnBox",
                        "van-popup",
                        "van-popup--center",
                        "main_wrapper",
                    ],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": (
                        "#appView > div > div:nth-of-type(3) > "
                        "div:nth-of-type(2) > div:nth-of-type(2)"
                    ),
                    "tag": "div",
                    "text": "立即保存",
                    "class_tokens": ["promoteBtn", "btn2"],
                    "ancestor_class_tokens": [
                        "promoteBtnBox",
                        "van-popup",
                        "van-popup--center",
                        "main_wrapper",
                    ],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == ".van-overlay:visible"
    assert target.allow_high_z_obstruction_hide is True
    assert reader.calls == [("task-1", "capture-popup")]


async def test_popup_dismissal_resolves_signed_homepage_payment_close() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > main > div > div > div > div.close",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": [
                        "imgWrapper",
                        "van-overlay",
                        "homepagePaymentPopup",
                        "home",
                    ],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "homepage_payment_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == (".homepagePaymentPopup .imgWrapper > .close:visible")
    assert target.allow_high_z_obstruction_hide is True
    assert reader.calls == [("task-1", "capture-popup")]


async def test_logical_popup_dismissal_binds_signed_homepage_payment_close() -> None:
    """H5 付费弹层关闭键在逻辑目标复核时仍应保持受信。"""
    identity = "sha256:" + "1" * 64
    state = "sha256:" + "2" * 64
    reader = StaticSnapshotReader(
        {
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "identity_fingerprint": identity,
                "state_fingerprint": state,
            },
            "elements": [
                {
                    "selector": "#appView > div.homepagePaymentPopup div.close",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": [
                        "imgWrapper",
                        "van-overlay",
                        "homepagePaymentPopup",
                        "home",
                    ],
                    "interactive": False,
                    "rect": {"x": 176.27, "y": 600.88, "width": 37.44, "height": 37.44},
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=reader,
        logical_targets_enabled=True,
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "homepage_payment_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
                "logical_target": {
                    "protocol_version": "logical_target.v1",
                    "source_identity_fingerprint": identity,
                    "source_state_fingerprint": state,
                    "expects_logical_navigation": True,
                },
            },
        )
    )

    assert target.selector == POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR
    assert target.target_trust == "trusted"
    assert target.source_state_fingerprint == state
    assert target.target_signature.startswith("sha256:")


async def test_homepage_payment_close_runtime_ignores_parallel_floating_popup() -> None:
    """已验签付费弹层应复核自身关闭键，不被并列常驻浮层抢占。"""
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import (
        _resolve_popup_dismissal_target,
    )

    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        try:
            await page.set_content(
                """
                <style>
                  .homepagePaymentPopup { position: relative; height: 0; z-index: 3000; }
                  .van-overlay { position: fixed; inset: 0; background: #0008; }
                  .imgWrapper { position: absolute; left: 45px; top: 160px;
                    width: 300px; height: 450px; background: white; }
                  .close { position: absolute; left: 130px; top: 400px;
                    width: 40px; height: 40px; background: red; }
                  .floatingPopup { position: fixed; right: 0; bottom: 80px;
                    width: 120px; height: 160px; z-index: 4000; background: orange; }
                </style>
                <div class="homepagePaymentPopup">
                  <div class="van-overlay">
                    <div class="imgWrapper">
                      <div class="popupContent">新人特惠</div>
                      <div class="close"></div>
                    </div>
                  </div>
                </div>
                <div class="floatingPopup">常驻活动浮层</div>
                """
            )

            target = await _resolve_popup_dismissal_target(
                page=page,
                selector=POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR,
            )
            assert target is not None
            assert target["dialog_signature"]
        finally:
            await browser.close()

    assert target is not None
    assert target["dialog_present"] is True
    assert target["kind"] == "close_control"
    assert target["selector"] == POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR
    assert target["dialog_selector"] == ".homepagePaymentPopup .imgWrapper"
    assert target["verification_required"] is True
    assert target["pointer_click_required"] is True


async def test_zero_height_homepage_payment_popup_can_close_and_verify() -> None:
    """零高度弹层宿主不应阻断已验签关闭键及关闭后复验。"""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import (
        _resolve_popup_dismissal_target,
        _wait_for_popup_dismissal,
    )

    async with async_playwright() as playwright:
        browser = await launch_web_test_browser(playwright.chromium, headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        try:
            await page.set_content(
                """
                <style>
                  .homepagePaymentPopup { position: relative; height: 0; z-index: 3000; }
                  .van-overlay { position: fixed; inset: 0; background: #0008; }
                  .imgWrapper { position: absolute; left: 45px; top: 160px;
                    width: 300px; height: 450px; background: white; }
                  .close { position: absolute; left: 130px; top: 400px;
                    width: 40px; height: 40px; background: red; }
                </style>
                <div class="homepagePaymentPopup">
                  <div class="van-overlay">
                    <div class="imgWrapper">
                      <div class="popupContent">新人特惠</div>
                      <div class="close"></div>
                    </div>
                  </div>
                </div>
                <script>
                  document.querySelector('.close').addEventListener('click', () => {
                    document.querySelector('.homepagePaymentPopup').remove();
                  });
                </script>
                """
            )

            target = await _resolve_popup_dismissal_target(
                page=page,
                selector=POPUP_HOMEPAGE_PAYMENT_CLOSE_SELECTOR,
            )
            assert target is not None
            await page.locator(target["selector"]).click(timeout=1_500)
            assert await _wait_for_popup_dismissal(
                page=page,
                popup_target=target,
                timeout_ms=3_000,
                timeout_error_type=PlaywrightTimeoutError,
            )
        finally:
            await browser.close()


async def test_popup_dismissal_rebinds_overlay_candidate_to_new_layer_close() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "body > main > div.van-overlay",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["van-overlay"],
                    "ancestor_class_tokens": ["homepagePaymentPopup", "home"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "body > main > div.van-overlay > div > div.close",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": [
                        "imgWrapper",
                        "van-overlay",
                        "homepagePaymentPopup",
                        "home",
                    ],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-next-popup-layer",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "b" * 64,
            },
        )
    )

    assert target.selector == (".homepagePaymentPopup .imgWrapper > .close:visible")
    assert target.h5_popup_dismissal_strategy == "safe_outside_then_close"
    assert reader.calls == [("task-1", "capture-next-popup-layer")]


async def test_popup_dismissal_resolves_signed_flutter_app_open_close() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#flt-semantic-node-label",
                    "tag": "flt-semantics",
                    "role": "text",
                    "text": "91PornAPP内打开",
                    "interactive": True,
                    "rect": {"x": 91, "y": 713.6, "width": 208, "height": 31.2},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-popup",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 36.4, "y": 216.6, "width": 317.2, "height": 353.6},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 176.8, "y": 591, "width": 36.4, "height": 36.4},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "flutter_app_open_popup_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == ("flt-semantics-host flt-semantics[flt-tappable]:visible")
    assert target.allow_high_z_obstruction_hide is True
    assert reader.calls == [("task-1", "capture-popup")]


async def test_logical_popup_dismissal_preserves_opaque_flutter_selector_and_rect() -> None:
    expected_rect = {"x": 176.8, "y": 591.0, "width": 36.4, "height": 36.4}
    reader = StaticSnapshotReader(
        {
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "identity_fingerprint": "sha256:" + "1" * 64,
                "state_fingerprint": "sha256:" + "2" * 64,
            },
            "elements": [
                {
                    "selector": "#flt-semantic-node-label",
                    "tag": "flt-semantics",
                    "role": "text",
                    "text": "91PornAPP内打开",
                    "interactive": True,
                    "rect": {"x": 91, "y": 713.6, "width": 208, "height": 31.2},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-popup",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 36.4, "y": 216.6, "width": 317.2, "height": 353.6},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": expected_rect,
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=reader,
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "flutter_app_open_popup_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == ("flt-semantics-host flt-semantics[flt-tappable]:visible")
    assert target.expected_rect == expected_rect
    assert target.target_source == "flutter_semantics"
    assert target.target_trust == "trusted"


@pytest.mark.parametrize(
    ("case", "expected_error_code", "expected_retryable"),
    [
        ("zero", "POPUP_DISMISSAL_NOT_FOUND", True),
        ("two", "POPUP_DISMISSAL_AMBIGUOUS", False),
        ("invalid_rect", "LOGICAL_TARGET_NOT_UNIQUE", True),
        ("duplicate_rect", "LOGICAL_TARGET_NOT_UNIQUE", False),
    ],
)
async def test_logical_popup_dismissal_ambiguity_never_calls_browser(
    case: str,
    expected_error_code: str,
    expected_retryable: bool,
) -> None:
    close_rect = {"x": 176.8, "y": 591.0, "width": 36.4, "height": 36.4}
    flutter_prefix = [
        {
            "selector": "#flt-semantic-node-label",
            "tag": "flt-semantics",
            "role": "text",
            "text": "91PornAPP内打开",
            "interactive": True,
            "rect": {"x": 91, "y": 713.6, "width": 208, "height": 31.2},
            "hit_test": {"center_hits_target": True},
        },
        {
            "selector": "#flt-semantic-node-popup",
            "tag": "flt-semantics",
            "role": None,
            "text": "",
            "interactive": True,
            "rect": {"x": 36.4, "y": 216.6, "width": 317.2, "height": 353.6},
            "hit_test": {"center_hits_target": True},
        },
    ]
    close = {
        "selector": "#flt-semantic-node-close",
        "tag": "flt-semantics",
        "role": None,
        "text": "",
        "interactive": True,
        "rect": close_rect,
        "hit_test": {"center_hits_target": True},
    }
    capability = "flutter_app_open_popup_close"
    elements = flutter_prefix
    if case == "two":
        elements = [
            *flutter_prefix,
            close,
            {
                **close,
                "rect": {"x": 176.8, "y": 615.0, "width": 36.4, "height": 36.4},
            },
        ]
    elif case == "invalid_rect":
        capability = "home_suspend_close"
        elements = [
            {
                "selector": "#home-suspend-close",
                "tag": "div",
                "class_tokens": ["home-suspend", "btn-close"],
                "interactive": True,
                "rect": {"x": "invalid", "y": 591, "width": 36.4, "height": 36.4},
                "hit_test": {"center_hits_target": True},
            }
        ]
    elif case == "duplicate_rect":
        elements = [
            *flutter_prefix,
            close,
            {
                "selector": "#flt-semantic-node-decoration",
                "tag": "flt-semantics",
                "role": "img",
                "text": "关闭图标装饰",
                "interactive": False,
                "rect": close_rect,
                "hit_test": {"center_hits_target": True},
            },
        ]

    reader = StaticSnapshotReader(
        {
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "shadow": False,
                "identity_fingerprint": "sha256:" + "1" * 64,
                "state_fingerprint": "sha256:" + "2" * 64,
            },
            "elements": elements,
        }
    )
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=UiSnapshotSemanticTargetResolver(
            snapshot_reader=reader,
            logical_targets_enabled=True,
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(
                semantic_target="关闭当前弹窗",
                parameters={
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": capability,
                    "obstruction_signature": "h5-obstruction:" + "a" * 64,
                    "logical_target": _logical_target_contract(),
                },
            ),
            agent_run_id="run-1",
            source_capture_id="capture-popup",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == expected_error_code
    assert result.retryable is expected_retryable
    assert browser.requests == []


async def test_popup_dismissal_rejects_truncated_flutter_announcement_snapshot() -> None:
    reader = StaticSnapshotReader(
        {
            "element_limit_reached": True,
            "elements": [
                {
                    "selector": "#background-card",
                    "tag": "flt-semantics",
                    "text": "首页背景卡片",
                    "interactive": True,
                }
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-popup",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前弹窗",
                parameters={
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": ("flutter_marketing_announcement_confirm"),
                    "obstruction_signature": "h5-obstruction:" + "a" * 64,
                },
            )
        )

    assert error.value.error_code == "POPUP_ACKNOWLEDGEMENT_RISK_BLOCKED"


async def test_popup_dismissal_resolves_generic_modal_local_safe_acknowledgement() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#modal-root",
                "tag": "div",
                "interactive": True,
                "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                "hit_test": {
                    "center_target": "#modal-copy",
                    "center_hits_target": False,
                },
            },
            {
                "selector": "#modal-copy",
                "tag": "div",
                "role": "text",
                "text": "版本更新公告，更多内容持续上线",
                "interactive": False,
                "rect": {"x": 50, "y": 280, "width": 290, "height": 220},
                "hit_test": {
                    "center_target": "#modal-copy",
                    "center_hits_target": True,
                },
            },
            {
                "selector": "#modal-ack",
                "tag": "button",
                "role": "button",
                "text": "确认",
                "interactive": True,
                "rect": {"x": 135, "y": 560, "width": 120, "height": 40},
                "hit_test": {
                    "center_target": "#modal-ack",
                    "center_hits_target": True,
                },
            },
        ],
    }
    from services.api.app.application.h5_modal_acknowledgement import (
        h5_modal_local_safe_acknowledgement,
    )

    proof = h5_modal_local_safe_acknowledgement(snapshot)
    assert proof is not None
    reader = StaticSnapshotReader(snapshot)

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "modal_local_safe_acknowledgement",
                "obstruction_signature": proof.obstruction_signature,
            },
        )
    )

    assert target.selector == "#modal-ack"
    assert target.allow_high_z_obstruction_hide is True
    assert target.clear_obstructions is True


async def test_popup_dismissal_resolves_signed_flutter_centered_ad_close() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#flt-semantic-node-popup",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 36.4, "y": 216.6, "width": 317.2, "height": 353.6},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 176.8, "y": 591, "width": 36.4, "height": 36.4},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "flutter_centered_ad_popup_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == "internal:flutter_centered_ad_popup_close"
    assert target.allow_high_z_obstruction_hide is True


async def test_popup_dismissal_resolves_signed_flutter_centered_ad_close_above() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#flt-semantic-node-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 178.1, "y": 269.9, "width": 33.8, "height": 33.8},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-popup",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 65, "y": 314.1, "width": 260, "height": 260},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": ("flutter_centered_ad_popup_close_above"),
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == "internal:flutter_centered_ad_popup_close_above"
    assert target.allow_high_z_obstruction_hide is True


async def test_popup_dismissal_resolves_signed_flutter_scroll_close_below() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#flt-semantic-node-scroll-popup",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": False,
                    "rect": {"x": 36.4, "y": 212.7, "width": 317.2, "height": 374.4},
                    "style": {"overflow_y": "scroll"},
                    "text_metrics": {"vertical_overflow": True},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#flt-semantic-node-scroll-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 178.1, "y": 607.9, "width": 33.8, "height": 33.8},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": ("flutter_centered_scroll_popup_close_below"),
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == "internal:flutter_centered_scroll_popup_close_below"
    assert target.allow_high_z_obstruction_hide is True


async def test_popup_dismissal_accepts_scroll_body_center_hitting_own_child() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#flt-semantic-node-390",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "内容入口",
                    "interactive": False,
                    "rect": {"x": 16.64, "y": 168.76, "width": 356.72, "height": 448.23},
                    "style": {"overflow_y": "scroll"},
                    "text_metrics": {"vertical_overflow": True},
                    "hit_test": {
                        "center_hits_target": True,
                        "center_target": "#flt-semantic-node-child",
                    },
                },
                {
                    "selector": "#flt-semantic-node-391",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 178.36, "y": 641.96, "width": 33.27, "height": 33.27},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": ("flutter_centered_scroll_popup_close_below"),
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == "internal:flutter_centered_scroll_popup_close_below"


async def test_popup_dismissal_resolves_signed_flutter_image_top_right_close() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#flt-semantic-node-image-popup",
                    "tag": "flt-semantics",
                    "role": "img",
                    "text": "",
                    "interactive": False,
                    "rect": {"x": 59.8, "y": 273.8, "width": 270.4, "height": 296.4},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#flt-semantic-node-image-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 288.6, "y": 289.4, "width": 26, "height": 26},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "flutter_image_popup_top_right_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == "internal:flutter_image_popup_top_right_close"
    assert target.allow_high_z_obstruction_hide is True


async def test_popup_dismissal_resolves_signed_flutter_account_notice_close() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#flt-semantic-node-account-notice",
                    "tag": "flt-semantics",
                    "role": "group",
                    "text": "立即保存",
                    "aria_label": (
                        "重要提示！！！首次安装APP请先保存此信息，可提高账号找回概率。"
                        "您的邀请码：abc"
                    ),
                    "interactive": False,
                    "rect": {"x": 40, "y": 246.14, "width": 310, "height": 351.72},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#flt-semantic-node-account-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "rect": {"x": 292.8, "y": 261.74, "width": 31.2, "height": 31.2},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": ("flutter_account_notice_top_right_close"),
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == "internal:flutter_account_notice_top_right_close"
    assert target.allow_high_z_obstruction_hide is True


async def test_bottom_navigation_resolves_signed_item_when_child_repeats_label() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#tabbar > div:nth-of-type(1)",
                    "tag": "div",
                    "role": "tab",
                    "text": "首页",
                    "class_tokens": ["van-tabbar-item"],
                    "ancestor_class_tokens": ["van-tabbar"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#tabbar > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "tab",
                    "text": "视频",
                    "class_tokens": ["van-tabbar-item"],
                    "ancestor_class_tokens": ["van-tabbar"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#tabbar > div:nth-of-type(2) > span",
                    "tag": "span",
                    "text": "视频",
                    "class_tokens": ["tabbar-item-text"],
                    "ancestor_class_tokens": ["van-tabbar-item", "van-tabbar"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)
    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=selector,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": "视频",
            },
        )
    )

    assert target.selector == selector
    assert reader.calls == [("task-1", "capture-home")]


async def test_page_tab_internal_target_resolves_to_current_snapshot_selector() -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#van-tabs-1-0",
                    "dom_sibling_index": 1,
                    "tag": "div",
                    "role": "tab",
                    "text": "最新",
                    "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#van-tabs-1-1",
                    "dom_sibling_index": 2,
                    "tag": "div",
                    "role": "tab",
                    "text": "推荐",
                    "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#van-tabs-1-2",
                    "dom_sibling_index": 3,
                    "tag": "div",
                    "role": "tab",
                    "text": "国产",
                    "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=h5_page_tab_target(index=3),
            parameters={
                "target_mode": "page_tab_navigation",
                "navigation_index": 3,
                "navigation_label": "国产",
            },
        )
    )

    assert target.selector == "#van-tabs-1-2"
    assert reader.calls == [("task-1", "capture-home")]


async def test_page_tab_internal_target_resolves_horizontally_offscreen_item() -> None:
    """横向 Tab 滚到末端后，精确的旧位置 Tab 仍可由浏览器滚回并点击。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#van-tabs-1-0",
                    "dom_sibling_index": 1,
                    "tag": "div",
                    "role": "tab",
                    "text": "最新",
                    "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "rect": {"x": -424.69, "y": 45.75, "width": 58.22, "height": 51.98},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#van-tabs-1-1",
                    "dom_sibling_index": 2,
                    "tag": "div",
                    "role": "tab",
                    "text": "推荐",
                    "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "rect": {"x": -366.47, "y": 45.75, "width": 58.22, "height": 51.98},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#van-tabs-1-2",
                    "dom_sibling_index": 3,
                    "tag": "div",
                    "role": "tab",
                    "text": "国产",
                    "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "rect": {"x": -308.25, "y": 45.75, "width": 58.22, "height": 51.98},
                    "hit_test": {"center_hits_target": False},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=h5_page_tab_target(index=2),
            parameters={
                "target_mode": "page_tab_navigation",
                "navigation_index": 2,
                "navigation_label": "推荐",
            },
        )
    )

    assert target.selector == "#van-tabs-1-1"
    assert reader.calls == [("task-1", "capture-home")]


async def test_page_tab_internal_target_rejects_in_viewport_interception() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#van-tabs-1-0",
                    "dom_sibling_index": 1,
                    "tag": "div",
                    "role": "tab",
                    "text": "最新",
                    "class_tokens": ["van-tab", "van-tab--line"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "rect": {"x": 10, "y": 45, "width": 58, "height": 52},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#van-tabs-1-1",
                    "dom_sibling_index": 2,
                    "tag": "div",
                    "role": "tab",
                    "text": "推荐",
                    "class_tokens": ["van-tab", "van-tab--line"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": "#hometab > div:nth-of-type(1) > div",
                    "interactive": True,
                    "rect": {"x": 68, "y": 45, "width": 58, "height": 52},
                    "hit_test": {
                        "center_hits_target": False,
                        "center_target_class_tokens": ["van-overlay"],
                    },
                },
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-home",
                action_type=InteractionOperation.CLICK,
                semantic_target=h5_page_tab_target(index=2),
                parameters={
                    "target_mode": "page_tab_navigation",
                    "navigation_index": 2,
                    "navigation_label": "推荐",
                },
            )
        )

    assert error.value.error_code == "SEMANTIC_TARGET_NOT_FOUND"


@pytest.mark.parametrize("navigation_index", [3, 6])
async def test_page_tab_internal_target_rejects_ambiguous_or_global_position_across_groups(
    navigation_index,
) -> None:
    elements = []
    for group in ("#primary-tabs", "#secondary-tabs"):
        for index, label in enumerate(("最新", "推荐", "国产"), start=1):
            elements.append(
                {
                    "selector": f"{group} > div[data-index='{index}']",
                    "dom_sibling_index": index,
                    "tag": "div",
                    "role": "tab",
                    "text": label,
                    "class_tokens": ["van-tab", "van-tab--line"],
                    "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                    "parent_selector": group,
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                }
            )
    reader = StaticSnapshotReader({"elements": elements})

    with pytest.raises(SemanticTargetResolutionError):
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-home",
                action_type=InteractionOperation.CLICK,
                semantic_target=h5_page_tab_target(index=navigation_index),
                parameters={
                    "target_mode": "page_tab_navigation",
                    "navigation_index": navigation_index,
                    "navigation_label": "国产",
                },
            )
        )


@pytest.mark.parametrize("navigation_index", [2, 14])
async def test_page_tab_internal_target_uses_own_group_position_across_groups(
    navigation_index,
) -> None:
    elements = []
    for index, label in enumerate(
        (
            "最新",
            "推荐",
            "国产",
            "乱伦",
            "动漫",
            "漫画",
            "日本AV",
            "欧美",
            "制服",
            "Only Fans",
            "华语原创",
            "三级片",
        ),
        start=1,
    ):
        elements.append(
            {
                "selector": f"#primary-tabs-{index}",
                "dom_sibling_index": index,
                "tag": "div",
                "role": "tab",
                "text": label,
                "class_tokens": ["van-tab", "van-tab--line"],
                "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                "parent_selector": "#primary-tabs",
                "interactive": True,
                "hit_test": {"center_hits_target": True},
            }
        )
    for index, label in enumerate(("热门推荐", "最新上架", "最新热评", "最多收藏"), start=1):
        elements.append(
            {
                "selector": f"#secondary-tabs-{index}",
                "dom_sibling_index": index,
                "tag": "div",
                "role": "tab",
                "text": label,
                "class_tokens": ["van-tab", "van-tab--line"],
                "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
                "parent_selector": "#secondary-tabs",
                "interactive": True,
                "hit_test": {"center_hits_target": True},
            }
        )
    reader = StaticSnapshotReader({"elements": elements})

    request = SemanticTargetResolutionRequest(
        task_id="task-1",
        session_id="session-1",
        source_capture_id="capture-home",
        action_type=InteractionOperation.CLICK,
        semantic_target=h5_page_tab_target(index=navigation_index),
        parameters={
            "target_mode": "page_tab_navigation",
            "navigation_index": navigation_index,
            "navigation_label": "最新上架",
        },
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)
    if navigation_index == 14:
        with pytest.raises(SemanticTargetResolutionError):
            await resolver.resolve(request)
        return
    target = await resolver.resolve(request)

    assert target.selector == "#secondary-tabs-2"


async def test_generic_spa_bottom_navigation_revalidates_snapshot_and_uses_live_selector() -> None:
    """自定义 SPA 候选只执行当前快照中结构、位置和文案均一致的节点。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#footer > li.footer-item:nth-of-type(2)",
                    "tag": "li",
                    "role": "tab",
                    "text": "视频",
                    "class_tokens": ["footer-item", "video"],
                    "ancestor_class_tokens": ["footer-wrap", "type", "color"],
                    # Vue 事件监听器不会投影为 onclick/cursor/role；精确的
                    # footer-item + footer-wrap 结构仍是平台签发的点击能力。
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )
    semantic_target = "internal:spa_bottom_navigation:nth-of-type(2)"

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=semantic_target,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": "视频",
                "navigation_family": "generic_spa",
            },
        )
    )

    assert target.selector == "#footer > li.footer-item:nth-of-type(2)"
    assert reader.calls == [("task-1", "capture-home")]


async def test_bottom_navigation_resolves_when_content_overlay_intercepts_entire_row() -> None:
    """整排 Vant 底栏被普通媒体卡片 overlay 截获时，交给执行端验效。"""
    labels = ["首页", "VLOG", "尤物圈", "社区", "我的"]
    elements = [
        {
            "selector": f"#tabbar > div:nth-of-type({index})",
            "tag": "div",
            "text": label,
            "class_tokens": ["van-tabbar-item"],
            "ancestor_class_tokens": ["van-tabbar"],
            "interactive": True,
            "rect": {"x": (index - 1) * 78, "y": 794, "width": 78, "height": 50},
            "hit_test": {
                "center_target": "#content-card > .aspect-ratio > .overlay",
                "center_hits_target": False,
                "center_target_class_tokens": ["overlay"],
                "center_target_ancestor_class_tokens": [
                    "aspect-ratio",
                    "video-item",
                ],
            },
        }
        for index, label in enumerate(labels, start=1)
    ]
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": elements,
        }
    )
    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(4):visible"

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=selector,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 4,
                "navigation_label": "社区",
            },
        )
    )

    assert target.selector == selector


async def test_bottom_navigation_rejects_modal_overlay_interception() -> None:
    """模态 overlay 即使伪装在内容结构下，也不得绕过底栏命中校验。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#tabbar > div:nth-of-type(4)",
                    "tag": "div",
                    "text": "社区",
                    "class_tokens": ["van-tabbar-item"],
                    "ancestor_class_tokens": ["van-tabbar"],
                    "interactive": True,
                    "rect": {"x": 234, "y": 794, "width": 78, "height": 50},
                    "hit_test": {
                        "center_target": "#modal > .overlay",
                        "center_hits_target": False,
                        "center_target_class_tokens": ["overlay"],
                        "center_target_ancestor_class_tokens": [
                            "aspect-ratio",
                            "video-item",
                            "van-overlay",
                        ],
                    },
                }
            ],
        }
    )
    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(4):visible"

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-home",
                action_type=InteractionOperation.CLICK,
                semantic_target=selector,
                parameters={
                    "target_mode": "bottom_navigation",
                    "navigation_index": 4,
                    "navigation_label": "社区",
                },
            )
        )

    assert error.value.error_code == "SEMANTIC_TARGET_NOT_FOUND"


async def test_pages_foot_navigation_resolves_unique_nonsemantic_li() -> None:
    selector = "ul.pages-foot-fix:visible > li:nth-of-type(6):visible"
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#app > div > ul > li:nth-of-type(6)",
                    "tag": "li",
                    "text": "我的",
                    "class_tokens": ["flex-column", "mine"],
                    "ancestor_class_tokens": ["pages-foot-fix", "pages-foot-box"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#app > div > ul > li:nth-of-type(6) > span",
                    "tag": "span",
                    "text": "我的",
                    "class_tokens": ["pages-foot-label"],
                    "ancestor_class_tokens": ["mine", "pages-foot-fix"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=selector,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 6,
                "navigation_label": "我的",
                "navigation_family": "pages_foot",
            },
        )
    )

    assert target.selector == selector


async def test_footer_wrap_navigation_resolves_unique_nonsemantic_li() -> None:
    selector = "div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(6):visible"
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#app > div.footer-wrap > div > ul > li:nth-of-type(6)",
                    "tag": "li",
                    "text": "我的",
                    "class_tokens": ["footer-item", "mine", "item-5"],
                    "ancestor_class_tokens": ["footer-wrap", "type", "color"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": (
                        "#app > div.footer-wrap > div > ul > li:nth-of-type(6) > div.text"
                    ),
                    "tag": "div",
                    "text": "我的",
                    "class_tokens": ["text"],
                    "ancestor_class_tokens": ["footer-item", "footer-wrap", "type"],
                    "interactive": False,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=selector,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 6,
                "navigation_label": "我的",
                "navigation_family": "footer_wrap",
            },
        )
    )

    assert target.selector == selector


async def test_flutter_bottom_navigation_resolves_signed_row_item() -> None:
    labels = ("视频", "抖阴", "海角", "暗网", "吃瓜", "我的")
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#nav-home-icon",
                    "tag": "flt-semantics",
                    "role": "",
                    "text": "",
                    "class_tokens": [],
                    "ancestor_class_tokens": [],
                    "structural_navigation_kind": "flutter_bottom_navigation",
                    "interactive": True,
                    "rect": {"x": 0, "y": 771, "width": 55, "height": 57},
                    "hit_test": {
                        "center_target": "flutter-view",
                        "center_hits_target": False,
                    },
                },
                *[
                    {
                        "selector": f"#nav-{index}",
                        "tag": "flt-semantics",
                        "role": "text",
                        "text": label,
                        "class_tokens": [],
                        "ancestor_class_tokens": [],
                        "structural_navigation_kind": "flutter_bottom_navigation",
                        "interactive": True,
                        "rect": {
                            "x": 55 * index,
                            "y": 771,
                            "width": 55,
                            "height": 57,
                        },
                        "hit_test": {
                            "center_target": "flutter-view",
                            "center_hits_target": False,
                        },
                    }
                    for index, label in enumerate(labels, start=1)
                ],
            ],
        }
    )
    semantic_target = "internal:flutter_bottom_navigation:nth-of-type(6)"

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=semantic_target,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 6,
                "navigation_label": "我的",
                "navigation_family": "flutter_semantics",
            },
        )
    )

    assert target.selector == h5_flutter_bottom_navigation_runtime_selector(
        index=6,
        label="我的",
    )


async def test_flutter_profile_login_group_resolves_controlled_internal_target() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#dynamic-profile-login",
                    "tag": "flt-semantics",
                    "role": "group",
                    "text": "微笑自行车 ID: 13228263 立即登录",
                    "aria_label": "微笑自行车\nID: 13228263\n立即登录",
                    "parent_semantics_selector": "#profile-root",
                    "parent_semantics_tappable": False,
                    "interactive": True,
                    "rect": {"x": 16, "y": 31, "width": 359, "height": 62},
                    "hit_test": {
                        "center_target": "flutter-view",
                        "center_hits_target": False,
                    },
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心 Flutter 登录入口",
            parameters={
                "target_mode": "flutter_profile_login_entry",
                "flutter_profile_login_capability": "profile_guest_login",
            },
        )
    )

    assert target.selector == "internal:flutter_profile_login_entry"


async def test_bottom_navigation_resolves_when_only_inactive_launch_shell_intercepts() -> None:
    """空启动壳截获底栏命中时，允许受签名底栏进入执行端效果验证。"""
    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "body > div:nth-of-type(1) > nav > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "tab",
                    "text": "视频",
                    "class_tokens": ["van-tabbar-item"],
                    "ancestor_class_tokens": ["van-tabbar"],
                    "interactive": True,
                    "rect": {"x": 65, "y": 794, "width": 65, "height": 50},
                    "hit_test": {
                        "center_target": "html > body > div:nth-of-type(2)",
                        "center_hits_target": False,
                    },
                },
                {
                    "selector": "html > body > div:nth-of-type(2)",
                    "tag": "div",
                    "text": "",
                    "class_tokens": ["launch-page"],
                    "interactive": False,
                    "inactive_launch_shell": True,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {
                        "center_target": "html > body > div:nth-of-type(2)",
                        "center_hits_target": True,
                    },
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=selector,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": "视频",
            },
        )
    )

    assert target.selector == selector


async def test_bottom_navigation_resolves_zero_rect_launch_shell_hit_profiles() -> None:
    """真实站点空壳无自身矩形，只能由整排底栏的一致命中剖面证明。"""
    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
    elements: list[dict[str, object]] = []
    labels = ["二次元", "视频", "萝莉岛", "情涩AI", "朋友圈", "我的"]
    for index, label in enumerate(labels, start=1):
        elements.append(
            {
                "selector": f"#tabbar > div:nth-of-type({index})",
                "tag": "div",
                "role": "tab",
                "text": label,
                "class_tokens": ["van-tabbar-item"],
                "ancestor_class_tokens": ["van-tabbar"],
                "interactive": True,
                "rect": {"x": 12.47 + (index - 1) * 60.84, "y": 755, "width": 60.84, "height": 69},
                "hit_test": {
                    "center_target": "html > body > div:nth-of-type(2)",
                    "center_hits_target": False,
                    "center_target_class_tokens": ["launch-page"],
                    "center_target_inactive_launch_shell": True,
                },
            }
        )
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": elements,
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=selector,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": "视频",
            },
        )
    )

    assert target.selector == selector


@pytest.mark.parametrize(
    "shell_override",
    [
        {"inactive_launch_shell": False},
        {"class_tokens": ["van-overlay"]},
        {"text": "进入"},
        {"rect": {"x": 100, "y": 100, "width": 100, "height": 100}},
    ],
)
async def test_bottom_navigation_rejects_untrusted_launch_shell_interception(
    shell_override: dict[str, object],
) -> None:
    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
    shell = {
        "selector": "html > body > div:nth-of-type(2)",
        "tag": "div",
        "text": "",
        "class_tokens": ["launch-page"],
        "interactive": False,
        "inactive_launch_shell": True,
        "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
        "hit_test": {
            "center_target": "html > body > div:nth-of-type(2)",
            "center_hits_target": True,
        },
    }
    shell.update(shell_override)
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#tabbar > div:nth-of-type(2)",
                    "tag": "div",
                    "role": "tab",
                    "text": "视频",
                    "class_tokens": ["van-tabbar-item"],
                    "ancestor_class_tokens": ["van-tabbar"],
                    "interactive": True,
                    "rect": {"x": 65, "y": 794, "width": 65, "height": 50},
                    "hit_test": {
                        "center_target": "html > body > div:nth-of-type(2)",
                        "center_hits_target": False,
                    },
                },
                shell,
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-home",
                action_type=InteractionOperation.CLICK,
                semantic_target=selector,
                parameters={
                    "target_mode": "bottom_navigation",
                    "navigation_index": 2,
                    "navigation_label": "视频",
                },
            )
        )

    assert error.value.error_code == "SEMANTIC_TARGET_NOT_FOUND"


async def test_list_detail_navigation_resolves_first_visible_snapshot_card_leaf() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#offscreen-cover",
                    "tag": "img",
                    "class_tokens": ["cover-img"],
                    "interactive": True,
                    "rect": {"x": -190, "y": 220, "width": 178, "height": 118},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#first-visible-cover",
                    "tag": "img",
                    "class_tokens": ["cover-img"],
                    "interactive": True,
                    "rect": {"x": 12, "y": 220, "width": 178, "height": 118},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#second-visible-cover",
                    "tag": "img",
                    "class_tokens": ["cover-img"],
                    "interactive": True,
                    "rect": {"x": 200, "y": 220, "width": 178, "height": 118},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".grid-item .movie-item:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#first-visible-cover"
    assert target.target_trust == "trusted"
    assert target.trusted_target_texts == ("当前列表首个内容卡片",)
    assert target.trusted_target_role == "navigation"
    assert reader.calls == [("task-1", "capture-video-list")]


async def test_list_detail_navigation_keeps_signed_semantics_when_card_text_is_risky() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-visible-title",
                    "tag": "p",
                    "role": "link",
                    "text": "立即支付解锁购买内容",
                    "class_tokens": ["text-overflow"],
                    "ancestor_class_tokens": ["post-box", "post", "post-text"],
                    "interactive": False,
                    "rect": {"x": 12, "y": 220, "width": 178, "height": 40},
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-post-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".post-box.post .post-text .text-overflow:visible",
                "source_page_type": "post_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#first-visible-title"
    assert target.trusted_target_texts == ("当前列表首个内容卡片",)
    assert target.trusted_target_role == "navigation"


async def test_list_detail_navigation_resolves_long_video_card() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-video",
                    "tag": "div",
                    "class_tokens": ["longVideoCard"],
                    "ancestor_class_tokens": ["item-wrapper", "row-four-card", "van-list"],
                    "interactive": True,
                    "rect": {"x": 12, "y": 220, "width": 178, "height": 150},
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".longVideoCard:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#first-video"


async def test_list_detail_navigation_resolves_leaf_inside_long_video_card() -> None:
    """容器未入快照时，允许完整视频流祖先链证明其标题叶节点。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-video-title",
                    "tag": "p",
                    "class_tokens": [],
                    "ancestor_class_tokens": [
                        "longVideoCard",
                        "item-wrapper",
                        "card-item",
                        "row-four-card",
                        "van-list",
                    ],
                    "interactive": False,
                    "rect": {"x": 16, "y": 487, "width": 174, "height": 37},
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".longVideoCard:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#first-video-title"


async def test_list_detail_navigation_rejects_incomplete_long_video_card_ancestor_chain() -> None:
    """仅出现通用列表祖先时不得把任意正文叶节点当作视频卡片。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#untrusted-title",
                    "tag": "p",
                    "class_tokens": [],
                    "ancestor_class_tokens": ["longVideoCard", "van-list"],
                    "interactive": False,
                    "rect": {"x": 16, "y": 487, "width": 174, "height": 37},
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    with pytest.raises(
        SemanticTargetResolutionError,
        match="当前 UI Snapshot 未证明可见的列表内容卡片",
    ):
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-video-list",
                action_type=InteractionOperation.CLICK,
                semantic_target="当前列表首个内容卡片",
                parameters={
                    "target_mode": "list_detail_navigation",
                    "list_detail_selector": ".longVideoCard:visible",
                    "source_page_type": "video_list",
                    "expected_page_type": "video_detail",
                },
            )
        )


async def test_list_detail_navigation_matches_card_by_ancestor_class_tokens() -> None:
    """裸 img 卡片（类名挂在 .img-box 包装层）应凭祖先记号获得结构证明。

    真实回归：/movie 列表卡为无类名 img，自身 class_tokens 为空，
    快照命中 0 元素导致 LIST_DETAIL_TARGET_NOT_FOUND×5 后跳过（任务 f535c867）。
    """
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#marquee-ad",
                    "tag": "img",
                    "class_tokens": [],
                    "ancestor_class_tokens": ["van-notice-bar__content", "van-notice-bar"],
                    "interactive": True,
                    "rect": {"x": 0, "y": 120, "width": 390, "height": 28},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#naked-card-cover",
                    "tag": "img",
                    "class_tokens": [],
                    "ancestor_class_tokens": ["img-box", "content", "layout5", "wraper"],
                    "interactive": True,
                    "rect": {"x": 10, "y": 465, "width": 355, "height": 197},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".video-section .layout5 .img-box:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#naked-card-cover"


async def test_content_list_detail_navigation_resolves_layout3_cover() -> None:
    """content_list 的 layout3 推荐封面必须带完整祖先证明后才可进入详情。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#current-layout3-cover",
                    "tag": "div",
                    "class_tokens": ["img-box"],
                    "ancestor_class_tokens": [
                        "content",
                        "layout3",
                        "layouts",
                        "video-section",
                    ],
                    "interactive": True,
                    "rect": {"x": 12, "y": 595, "width": 177, "height": 177},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#unrelated-img-box",
                    "tag": "div",
                    "class_tokens": ["img-box"],
                    "ancestor_class_tokens": ["advertisement"],
                    "interactive": True,
                    "rect": {"x": 201, "y": 595, "width": 177, "height": 177},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-content-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".video-section .layout3 .img-box:visible",
                "source_page_type": "content_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#current-layout3-cover"


async def test_home_list_detail_navigation_resolves_first_horizontal_video_card() -> None:
    """首页双类名视频流必须按可信矩形选中左上首卡。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#van-tab-25 > div > div:nth-of-type(2)",
                    "class_tokens": ["horizontalVideoBox", "videoBox"],
                    "interactive": True,
                    "rect": {"x": 198.63, "y": 334.81, "width": 180.95, "height": 178.83},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#van-tab-25 > div > div:nth-of-type(1)",
                    "class_tokens": ["horizontalVideoBox", "videoBox"],
                    "interactive": True,
                    "rect": {"x": 10.39, "y": 334.81, "width": 180.95, "height": 178.83},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".horizontalVideoBox.videoBox:visible",
                "source_page_type": "home",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#van-tab-25 > div > div:nth-of-type(1)"


async def test_video_item_navigation_activates_verified_card_container() -> None:
    """透明 launch-page 截获叶节点时仍应由验签模式激活视频卡片容器。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#video-overlay-leaf",
                    "tag": "div",
                    "class_tokens": ["overlay"],
                    "ancestor_class_tokens": ["video-item", "scroll-x-item"],
                    "rect": {"x": 12, "y": 900, "width": 178, "height": 118},
                    "hit_test": {
                        "center_hits_target": False,
                        "center_target_class_tokens": ["launch-page"],
                        "center_target_inactive_launch_shell": True,
                    },
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".video-item:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == ".video-item:visible"
    assert target.verified_card_activation == "video_item"


async def test_comic_name_navigation_rejects_inactive_shell_interception() -> None:
    """漫画标题被透明层截获时也不是当前无遮挡标题，必须失败关闭。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#comic-name-leaf",
                    "tag": "div",
                    "class_tokens": ["name", "van-multi-ellipsis--l2"],
                    "ancestor_class_tokens": ["comic-item", "scroll-x-item"],
                    "rect": {"x": 12, "y": 959, "width": 146, "height": 37},
                    "hit_test": {
                        "center_hits_target": False,
                        "center_target_class_tokens": ["launch-page"],
                        "center_target_inactive_launch_shell": True,
                    },
                },
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-content-list",
                action_type=InteractionOperation.CLICK,
                semantic_target="当前列表首个内容卡片",
                parameters={
                    "target_mode": "list_detail_navigation",
                    "list_detail_selector": ".comic-item .name:visible",
                    "source_page_type": "content_list",
                    "expected_page_type": "content_detail",
                },
            )
        )

    assert error.value.error_code == "LIST_DETAIL_TARGET_NOT_FOUND"


async def test_comic_name_navigation_rejects_truncated_cover_only_proof() -> None:
    """长列表只剩封面证明时不得冒充当前无遮挡漫画标题。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-comic-cover",
                    "tag": "div",
                    "class_tokens": ["aspect-ratio-content"],
                    "ancestor_class_tokens": [
                        "aspect-ratio",
                        "comic-item",
                        "scroll-x-item",
                    ],
                    "rect": {"x": 12, "y": 765, "width": 146, "height": 195},
                    "hit_test": {
                        "center_hits_target": False,
                        "center_target_class_tokens": ["launch-page"],
                        "center_target_inactive_launch_shell": True,
                    },
                },
            ],
            "truncated": True,
            "truncated_reason": "serialized_size_limit",
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-content-list",
                action_type=InteractionOperation.CLICK,
                semantic_target="当前列表首个内容卡片",
                parameters={
                    "target_mode": "list_detail_navigation",
                    "list_detail_selector": ".comic-item .name:visible",
                    "source_page_type": "content_list",
                    "expected_page_type": "content_detail",
                },
            )
        )

    assert error.value.error_code == "LIST_DETAIL_TARGET_NOT_FOUND"


async def test_list_detail_navigation_resolves_home_video_panel_grid_cover() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#unrelated-image",
                    "tag": "img",
                    "class_tokens": ["wh-full"],
                    "ancestor_class_tokens": ["banner", "home-video-panel"],
                    "interactive": False,
                    "rect": {"x": 10, "y": 180, "width": 370, "height": 120},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#first-grid-cover",
                    "tag": "img",
                    "class_tokens": ["wh-full"],
                    "ancestor_class_tokens": [
                        "pic",
                        "item",
                        "twogridlayout",
                        "home-video-panel__scroll",
                        "home-video-panel",
                    ],
                    "interactive": False,
                    "rect": {"x": 16, "y": 420, "width": 172, "height": 98},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": (".home-video-panel .TwoGridLayout .item .pic img:visible"),
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#first-grid-cover"


async def test_list_detail_navigation_resolves_card_title_by_own_name_token() -> None:
    """标题选择器 .layout5 .name 应命中自带 name 记号的标题叶节点。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#cover-img",
                    "tag": "img",
                    "class_tokens": [],
                    "ancestor_class_tokens": ["img-box", "layout5"],
                    "interactive": True,
                    "rect": {"x": 10, "y": 465, "width": 355, "height": 197},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#card-title",
                    "tag": "div",
                    "class_tokens": ["van-ellipsis", "name"],
                    "ancestor_class_tokens": ["content", "layout5"],
                    "interactive": True,
                    "rect": {"x": 10, "y": 668, "width": 355, "height": 22},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".layout5 .name:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#card-title"


async def test_manga_video_detail_navigation_requires_unobstructed_visible_title() -> None:
    """漫画视频标题被遮挡时不得滚动或点击其他标题进入详情。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#covered-title",
                    "tag": "div",
                    "class_tokens": ["duohang2", "text-11"],
                    "ancestor_class_tokens": ["item-box-comics", "box"],
                    "interactive": False,
                    "rect": {"x": 16, "y": 350, "width": 104, "height": 34},
                    "hit_test": {
                        "center_hits_target": False,
                        "topmost": False,
                        "pointer_interception": False,
                    },
                },
                {
                    "selector": "#below-fold-title",
                    "tag": "div",
                    "class_tokens": ["duohang2", "text-11"],
                    "ancestor_class_tokens": ["item-box-comics", "box"],
                    "interactive": False,
                    "rect": {"x": 16, "y": 920, "width": 104, "height": 34},
                    "hit_test": {
                        "center_hits_target": False,
                        "topmost": False,
                        "pointer_interception": False,
                    },
                },
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-video-list",
                action_type=InteractionOperation.CLICK,
                semantic_target="当前列表首个内容卡片",
                parameters={
                    "target_mode": "list_detail_navigation",
                    "list_detail_selector": ".item-box-comics .duohang2:visible",
                    "source_page_type": "video_list",
                    "expected_page_type": "video_detail",
                },
            )
        )

    assert error.value.error_code == "LIST_DETAIL_TARGET_NOT_FOUND"


async def test_manga_video_detail_navigation_resolves_first_unobstructed_title() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-visible-title",
                    "tag": "div",
                    "text": "漫画视频标题",
                    "class_tokens": ["duohang2", "text-11"],
                    "ancestor_class_tokens": ["item-box-comics", "box"],
                    "interactive": False,
                    "rect": {"x": 16, "y": 350, "width": 104, "height": 34},
                    "hit_test": {
                        "center_hits_target": True,
                        "topmost": True,
                        "pointer_interception": True,
                    },
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".item-box-comics .duohang2:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#first-visible-title"


async def test_long_video_card_detail_navigation_resolves_unobstructed_title() -> None:
    """首页长视频卡只解析当前视口内无遮挡的标题叶节点。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-long-video-title",
                    "tag": "p",
                    "text": "当前无遮挡视频标题",
                    "class_tokens": [],
                    "ancestor_class_tokens": [
                        "line-clamp-2",
                        "longVideoCard",
                        "item-wrapper",
                    ],
                    "interactive": False,
                    "rect": {"x": 16, "y": 488, "width": 175, "height": 37},
                    "hit_test": {
                        "center_hits_target": True,
                        "topmost": True,
                        "pointer_interception": True,
                    },
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": (".longVideoCard .line-clamp-2 > p:visible"),
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#first-long-video-title"


async def test_community_home_detail_navigation_resolves_unobstructed_title() -> None:
    """新版社区 feed 应解析正文标题，不再回退到重复昵称文本。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-community-title",
                    "tag": "div",
                    "text": "当前无遮挡帖子标题",
                    "class_tokens": ["poi_title", "!text-12px"],
                    "ancestor_class_tokens": ["van-list", "community_home_tab"],
                    "interactive": False,
                    "rect": {"x": 16, "y": 430, "width": 358, "height": 38},
                    "hit_test": {
                        "center_hits_target": True,
                        "topmost": True,
                        "pointer_interception": True,
                    },
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-post-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": (".community_home .van-list .poi_title:visible"),
                "source_page_type": "post_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#first-community-title"


async def test_content_list_community_home_title_accepts_captured_ancestor() -> None:
    """实际快照使用 community_home 祖先时，朋友圈标题也应可解析。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-moment-title",
                    "tag": "div",
                    "text": "朋友圈首条无遮挡标题",
                    "class_tokens": ["poi_title"],
                    "ancestor_class_tokens": ["van-list", "community_home"],
                    "interactive": False,
                    "rect": {"x": 16, "y": 430, "width": 358, "height": 38},
                    "hit_test": {
                        "center_hits_target": True,
                        "topmost": True,
                        "pointer_interception": True,
                    },
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-content-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": (".community_home .van-list .poi_title:visible"),
                "source_page_type": "content_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#first-moment-title"


async def test_list_detail_navigation_skips_unrelated_title_outside_card_scope() -> None:
    """通用 .title 记号必须落在选择器声明的卡片容器内，版块头等无关标题不得当选。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#section-header-title",
                    "tag": "div",
                    "class_tokens": ["title"],
                    "ancestor_class_tokens": ["section-header", "page-wraper"],
                    "interactive": True,
                    "rect": {"x": 10, "y": 180, "width": 200, "height": 28},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#comic-card-title",
                    "tag": "div",
                    "class_tokens": ["title"],
                    "ancestor_class_tokens": ["con", "comics-item"],
                    "interactive": True,
                    "rect": {"x": 10, "y": 420, "width": 170, "height": 24},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-content-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".comics-item .title:visible",
                "source_page_type": "content_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#comic-card-title"


async def test_list_detail_navigation_resolves_first_comic_item_name() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#section-name",
                    "tag": "div",
                    "class_tokens": ["name"],
                    "ancestor_class_tokens": ["section-header"],
                    "interactive": True,
                    "rect": {"x": 10, "y": 120, "width": 200, "height": 28},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#comic-name-second",
                    "tag": "div",
                    "class_tokens": ["name", "van-multi-ellipsis--l2"],
                    "ancestor_class_tokens": ["comic-item", "scroll-x-item"],
                    "interactive": True,
                    "rect": {"x": 205, "y": 430, "width": 170, "height": 40},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#comic-name-first",
                    "tag": "div",
                    "class_tokens": ["name", "van-multi-ellipsis--l2"],
                    "ancestor_class_tokens": ["comic-item", "scroll-x-item"],
                    "interactive": True,
                    "rect": {"x": 10, "y": 430, "width": 170, "height": 40},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-content-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".comic-item .name:visible",
                "source_page_type": "content_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#comic-name-first"


@pytest.mark.parametrize(
    (
        "source_page_type",
        "expected_page_type",
        "detail_selector",
        "class_tokens",
        "ancestor_class_tokens",
    ),
    [
        (
            "home",
            "video_detail",
            ".home-video-panel .TwoGridLayout .item .desc:visible",
            ["desc"],
            ["item", "TwoGridLayout", "home-video-panel__scroll"],
        ),
        (
            "content_list",
            "content_detail",
            ".modVideoBox .modCover_name:visible",
            ["modCover_name"],
            ["modVideoBox", "modBox"],
        ),
        (
            "post_list",
            "content_detail",
            ".post-box.post .post-text .text-overflow:visible",
            ["text-overflow"],
            ["post-text", "post-box", "post"],
        ),
        (
            "post_list",
            "content_detail",
            ".post-main-item .content-images-wrap .title:visible",
            ["title"],
            ["content-images-wrap", "post-main-item"],
        ),
        (
            "video_list",
            "video_detail",
            "li.big-list .decial:visible",
            ["decial"],
            ["decial-wrap", "big-list"],
        ),
    ],
)
async def test_list_detail_navigation_resolves_supplemental_real_titles(
    source_page_type: str,
    expected_page_type: str,
    detail_selector: str,
    class_tokens: list[str],
    ancestor_class_tokens: list[str],
) -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#supplemental-title",
                    "tag": "div",
                    "class_tokens": class_tokens,
                    "ancestor_class_tokens": ancestor_class_tokens,
                    "interactive": False,
                    "rect": {"x": 12, "y": 260, "width": 366, "height": 42},
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id=f"capture-{source_page_type}",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": detail_selector,
                "source_page_type": source_page_type,
                "expected_page_type": expected_page_type,
            },
        )
    )

    assert target.selector == "#supplemental-title"


async def test_list_detail_navigation_falls_back_to_below_fold_card_when_none_visible() -> None:
    """首屏内无可见卡片时兜底签发首屏下方卡片：命中测试在视口外不可信，
    执行端点击前会滚动到位（真实回归：LIST_DETAIL_TARGET_NOT_FOUND×5）。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#offscreen-left-cover",
                    "tag": "img",
                    "class_tokens": ["cover-img"],
                    "interactive": True,
                    "rect": {"x": -190, "y": 220, "width": 178, "height": 118},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#below-fold-cover",
                    "tag": "img",
                    "class_tokens": ["cover-img"],
                    "interactive": True,
                    "rect": {"x": 12, "y": 900, "width": 178, "height": 118},
                    "hit_test": {"center_hits_target": False},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-video-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".grid-item .movie-item:visible",
                "source_page_type": "video_list",
                "expected_page_type": "video_detail",
            },
        )
    )

    assert target.selector == "#below-fold-cover"


@pytest.mark.parametrize(
    (
        "source_page_type",
        "expected_page_type",
        "detail_selector",
        "proof_token",
        "expected_dom_dispatch",
    ),
    [
        (
            "post_list",
            "content_detail",
            ".dynamicData-box:visible .title:visible",
            "title",
            True,
        ),
        ("content_list", "content_detail", ".ratio-box:visible", "ratio-img", False),
    ],
)
async def test_list_detail_navigation_resolves_real_post_and_content_card_leaves(
    source_page_type: str,
    expected_page_type: str,
    detail_selector: str,
    proof_token: str,
    expected_dom_dispatch: bool,
) -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#first-card-leaf",
                    "tag": "div",
                    "class_tokens": [proof_token],
                    "interactive": False,
                    "rect": {"x": 12, "y": 270, "width": 366, "height": 146},
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id=f"capture-{source_page_type}",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": detail_selector,
                "source_page_type": source_page_type,
                "expected_page_type": expected_page_type,
            },
        )
    )

    assert target.selector == "#first-card-leaf"
    assert target.verified_card_activation == ("post_card" if expected_dom_dispatch else None)


async def test_list_detail_navigation_selects_direct_post_box_not_its_child() -> None:
    """卡片自身持有路由事件时，祖先记号不得让正文叶节点冒充卡片。"""
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#post-text",
                    "class_tokens": ["post-text"],
                    "ancestor_class_tokens": ["post-box", "post"],
                    "rect": {"x": 12, "y": 250, "width": 366, "height": 40},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#post-card",
                    "class_tokens": ["post-box", "post"],
                    "rect": {"x": 12, "y": 250, "width": 366, "height": 240},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-direct-post-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".post-box.post:visible",
                "source_page_type": "post_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#post-card"
    assert target.verified_card_activation is None


async def test_post_title_navigation_rejects_parent_and_unsafe_title_nodes() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#parent-content",
                    "class_tokens": ["dynamicData-content"],
                    "rect": {"x": 12, "y": 100, "width": 366, "height": 80},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#disabled-title",
                    "class_tokens": ["title"],
                    "disabled": True,
                    "rect": {"x": 12, "y": 190, "width": 366, "height": 32},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#offscreen-title",
                    "class_tokens": ["title"],
                    "rect": {"x": -400, "y": 230, "width": 366, "height": 32},
                    "hit_test": {"center_hits_target": False},
                },
                {
                    "selector": "#first-visible-title",
                    "class_tokens": ["title"],
                    "rect": {"x": 12, "y": 280, "width": 366, "height": 32},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#second-visible-title",
                    "class_tokens": ["title"],
                    "rect": {"x": 12, "y": 380, "width": 366, "height": 32},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-post-list",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".dynamicData-box:visible .title:visible",
                "source_page_type": "post_list",
                "expected_page_type": "content_detail",
            },
        )
    )

    assert target.selector == "#first-visible-title"


@pytest.mark.parametrize(
    ("match_count", "expected_error_code"),
    [
        (0, "POPUP_DISMISSAL_NOT_FOUND"),
        (2, "POPUP_DISMISSAL_AMBIGUOUS"),
    ],
)
async def test_popup_dismissal_requires_one_trusted_dialog(
    match_count: int,
    expected_error_code: str,
) -> None:
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": f"#dialog-{index}",
                    "tag": "div",
                    "role": "dialog",
                    "class_tokens": ["van-dialog"],
                    "interactive": True,
                }
                for index in range(match_count)
            ]
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-popup",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前弹窗",
                parameters={
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": "dialog_content_close",
                    "obstruction_signature": "h5-obstruction:" + "a" * 64,
                },
            )
        )

    assert error.value.error_code == expected_error_code


async def test_line_loading_login_retry_resolves_unique_confirm_button() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#login-failed-dialog",
                    "tag": "div",
                    "role": "dialog",
                    "text": "温馨提示 用户登陆失败 重试",
                    "class_tokens": ["van-popup", "van-dialog"],
                    "interactive": True,
                    "rect": {"x": 28, "y": 299, "width": 333, "height": 160},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#login-retry",
                    "tag": "button",
                    "text": "重试",
                    "class_tokens": ["van-button", "van-dialog__confirm"],
                    "ancestor_class_tokens": ["van-dialog"],
                    "interactive": True,
                    "disabled": False,
                    "rect": {"x": 54, "y": 406, "width": 283, "height": 37},
                    "hit_test": {"center_hits_target": True},
                },
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-line-login",
            action_type=InteractionOperation.CLICK,
            semantic_target="重试线路页登录",
            parameters={
                "target_mode": "line_loading_login_retry",
                "line_loading_login_retry_capability": "line_loading_login_retry",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == "#login-retry"


async def test_line_loading_login_retry_rejects_multiple_confirm_buttons() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": "#login-failed-dialog",
                    "tag": "div",
                    "role": "dialog",
                    "text": "用户登录失败 重试",
                    "class_tokens": ["van-dialog"],
                    "interactive": True,
                    "rect": {"x": 28, "y": 299, "width": 333, "height": 160},
                    "hit_test": {"center_hits_target": True},
                },
                *[
                    {
                        "selector": f"#login-retry-{index}",
                        "tag": "button",
                        "text": "重试",
                        "class_tokens": ["van-dialog__confirm"],
                        "ancestor_class_tokens": ["van-dialog"],
                        "interactive": True,
                        "disabled": False,
                        "rect": {
                            "x": 54,
                            "y": 400 + index * 40,
                            "width": 283,
                            "height": 37,
                        },
                        "hit_test": {"center_hits_target": True},
                    }
                    for index in range(2)
                ],
            ],
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-line-login",
                action_type=InteractionOperation.CLICK,
                semantic_target="重试线路页登录",
                parameters={
                    "target_mode": "line_loading_login_retry",
                    "line_loading_login_retry_capability": "line_loading_login_retry",
                    "obstruction_signature": "h5-obstruction:" + "a" * 64,
                },
            )
        )

    assert error.value.error_code == "LINE_LOADING_LOGIN_RETRY_AMBIGUOUS"


def _profile_visual_parameters() -> dict[str, object]:
    return {
        "target_mode": "visual_coordinate",
        "visual_entry_kind": "profile_vip_payment",
        "visual_source_capture_id": "capture-1",
        "visual_source_url": "https://example.test/profile#source",
        "screenshot_index": 1,
        "visual_source_artifact_id": "artifact-profile",
        "visual_source_sha256": "sha256:" + "1" * 64,
        "left_ratio": 0.1,
        "top_ratio": 0.4,
        "right_ratio": 0.9,
        "bottom_ratio": 0.5,
        "visual_confidence": 0.92,
    }


def _profile_visual_snapshot(**overrides) -> dict[str, object]:
    return {
        "url": "https://example.test/profile#current",
        "viewport": {
            "width": 390,
            "height": 844,
            "device_pixel_ratio": 3,
            "scroll_x": 0,
            "scroll_y": 0,
        },
        "document": {"scroll_width": 390, "scroll_height": 1688, "fullscreen": False},
        "structure_signature": "profile-structure-v1",
        "elements": [],
        "_capture_visual_checksum": "sha256:" + "1" * 64,
        "_capture_visual_region_checksum": "sha256:" + "3" * 64,
        **overrides,
    }


async def test_visual_profile_payment_target_resolves_without_dom_selector() -> None:
    reader = StaticSnapshotReader(_profile_visual_snapshot())
    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="立即开通",
            parameters=_profile_visual_parameters(),
        )
    )

    assert target.selector is None
    assert target.click_x_ratio == 0.5
    assert target.click_y_ratio == 0.45
    assert target.left_ratio == 0.1
    assert target.top_ratio == 0.4
    assert target.right_ratio == 0.9
    assert target.bottom_ratio == 0.5
    assert target.visual_confidence == 0.92
    assert target.visual_source_url == "https://example.test/profile#source"
    assert target.visual_entry_kind == "profile_vip_payment"
    assert target.visual_expected_text == "立即开通"
    assert target.screenshot_index == 1
    assert reader.calls == [("task-1", "capture-1")]
    assert reader.visual_calls == [("task-1", "capture-1", "artifact-profile", None)]
    assert reader.visual_region_calls == [
        ("task-1", "capture-1", "artifact-profile", 0.1, 0.4, 0.9, 0.5)
    ]


@pytest.mark.parametrize(
    ("semantic_target", "entry_kind"),
    [
        ("签到中心", "profile_activity_ocr"),
        ("设置", "profile_settings_image"),
        ("会员中心", "profile_vip_payment"),
    ],
)
async def test_visual_profile_entry_contract_resolves_bound_coordinate(
    semantic_target: str,
    entry_kind: str,
) -> None:
    parameters = _profile_visual_parameters()
    parameters["visual_entry_kind"] = entry_kind
    reader = StaticSnapshotReader(_profile_visual_snapshot())

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target=semantic_target,
            parameters=parameters,
        )
    )

    assert target.click_x_ratio == 0.5
    assert target.click_y_ratio == 0.45
    assert target.visual_entry_kind == entry_kind
    assert target.visual_expected_text == semantic_target


async def test_profile_credential_screenshot_popup_resolves_dedicated_button() -> None:
    from services.api.app.application.h5_profile_credential_popup import (
        h5_profile_credential_screenshot_confirm,
    )

    snapshot = {
        "elements": [
            {
                "selector": ".mine-pop .account-cert-popup .btn",
                "tag": "div",
                "text": "请截图保存",
                "class_tokens": ["btn"],
                "ancestor_class_tokens": [
                    "account-cert-popup",
                    "van-popup",
                    "van-popup--center",
                    "mine-pop",
                ],
                "rect": {"x": 59, "y": 634, "width": 271, "height": 41},
                "hit_test": {
                    "center_hits_target": True,
                    "topmost_at_center": True,
                },
            }
        ]
    }
    proof = h5_profile_credential_screenshot_confirm(snapshot)
    assert proof is not None

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": proof.capability,
                "obstruction_signature": proof.obstruction_signature,
            },
        )
    )

    assert target.selector == proof.selector
    assert target.clear_obstructions is True


@pytest.mark.parametrize("confirm_text", ["立即保存", "保存"])
@pytest.mark.parametrize("session_id", ["session-1", "h5:run-profile"])
async def test_profile_credential_save_popup_resolves_dedicated_button(
    confirm_text: str,
    session_id: str,
) -> None:
    """个人中心阻断弹窗保存按钮必须绑定当前 Snapshot 的唯一源元素。"""
    from services.api.app.application.h5_profile_credential_popup import (
        h5_profile_credential_screenshot_confirm,
    )

    snapshot = {
        "elements": [
            {
                "selector": ".van-popup.code-popup",
                "tag": "div",
                "role": "dialog",
                "text": f"当前内容可稍后查看，请先完成本地保存 {confirm_text}",
                "class_tokens": [
                    "van-popup",
                    "van-popup--round",
                    "van-popup--center",
                    "code-popup",
                ],
                "rect": {"x": 39, "y": 166, "width": 312, "height": 512},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
            {
                "selector": ".van-popup.code-popup .btn",
                "tag": "div",
                "text": confirm_text,
                "class_tokens": ["btn"],
                "ancestor_class_tokens": [
                    "code-popup",
                    "van-popup",
                    "van-popup--round",
                    "van-popup--center",
                ],
                "rect": {"x": 97, "y": 629, "width": 196, "height": 44},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
        ]
    }
    proof = h5_profile_credential_screenshot_confirm(snapshot)
    assert proof is not None

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id=session_id,
            source_capture_id="capture-profile-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": proof.capability,
                "obstruction_signature": proof.obstruction_signature,
            },
        )
    )

    assert target.selector == proof.selector
    assert target.expected_rect == {"x": 97.0, "y": 629.0, "width": 196.0, "height": 44.0}
    assert target.clear_obstructions is True
    from services.api.app.domain.interaction.command_policy import infer_resolved_target_risk

    if session_id.startswith("h5:"):
        assert target.trusted_target_role == "profile_credential_confirm"
        assert (
            infer_resolved_target_risk(
                trusted_target_texts=target.trusted_target_texts,
                target_role=target.trusted_target_role,
            )
            is InteractionRiskLevel.LOW
        )
    else:
        assert target.trusted_target_role != "profile_credential_confirm"


async def test_profile_promote_popup_resolves_immediate_save_button() -> None:
    """真实 promoteBtn.btn2 必须绑定 Snapshot 源元素和矩形。"""
    from services.api.app.application.h5_profile_credential_popup import (
        h5_profile_credential_screenshot_confirm,
    )

    snapshot = {
        "elements": [
            {
                "selector": "#appView > div > div:nth-of-type(3)",
                "tag": "div",
                "role": "dialog",
                "text": "账号凭证提示 我已保存 立即保存",
                "class_tokens": ["van-popup", "van-popup--center"],
                "rect": {"x": 41, "y": 140, "width": 308, "height": 564},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
            {
                "selector": "#appView .promoteBtn.btn1",
                "tag": "div",
                "text": "我已保存",
                "class_tokens": ["promoteBtn", "btn1"],
                "ancestor_class_tokens": [
                    "promoteBtnBox",
                    "van-popup",
                    "van-popup--center",
                ],
                "rect": {"x": 50, "y": 658, "width": 135, "height": 46},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
            {
                "selector": "#appView .promoteBtn.btn2",
                "tag": "div",
                "text": "立即保存",
                "class_tokens": ["promoteBtn", "btn2"],
                "ancestor_class_tokens": [
                    "promoteBtnBox",
                    "van-popup",
                    "van-popup--center",
                ],
                "rect": {"x": 204, "y": 658, "width": 135, "height": 46},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
        ]
    }
    proof = h5_profile_credential_screenshot_confirm(snapshot)
    assert proof is not None

    target = await UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot)
    ).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": proof.capability,
                "obstruction_signature": proof.obstruction_signature,
            },
        )
    )

    assert target.selector == proof.selector
    assert target.expected_rect == {"x": 204.0, "y": 658.0, "width": 135.0, "height": 46.0}


async def test_visual_profile_payment_accepts_fresh_matching_before_action_capture() -> None:
    snapshot = _profile_visual_snapshot()
    reader = MappingSnapshotReader({"capture-1": snapshot, "capture-before": dict(snapshot)})

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-before",
            action_type=InteractionOperation.CLICK,
            semantic_target="立即开通",
            parameters=_profile_visual_parameters(),
        )
    )

    assert target.click_x_ratio == 0.5
    assert reader.calls == [
        ("task-1", "capture-1"),
        ("task-1", "capture-before"),
    ]


async def test_visual_profile_payment_accepts_unrelated_dynamic_page_region() -> None:
    reader = MappingSnapshotReader(
        {
            "capture-1": _profile_visual_snapshot(),
            "capture-before": _profile_visual_snapshot(
                structure_signature="profile-structure-v2",
                _capture_visual_checksum="sha256:" + "2" * 64,
            ),
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-before",
            action_type=InteractionOperation.CLICK,
            semantic_target="立即开通",
            parameters=_profile_visual_parameters(),
        )
    )

    assert target.click_x_ratio == 0.5


@pytest.mark.parametrize(
    "current_overrides",
    [
        {
            "document": {
                "scroll_width": 390,
                "scroll_height": 2000,
                "fullscreen": False,
            }
        },
        {
            "viewport": {
                "width": 430,
                "height": 932,
                "device_pixel_ratio": 3,
                "scroll_x": 0,
                "scroll_y": 0,
            }
        },
        {"_capture_visual_region_checksum": "sha256:" + "4" * 64},
    ],
)
async def test_visual_profile_payment_rejects_same_url_stale_page_state(
    current_overrides: dict[str, object],
) -> None:
    reader = MappingSnapshotReader(
        {
            "capture-1": _profile_visual_snapshot(),
            "capture-before": _profile_visual_snapshot(**current_overrides),
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-before",
                action_type=InteractionOperation.CLICK,
                semantic_target="立即开通",
                parameters=_profile_visual_parameters(),
            )
        )

    assert error.value.error_code == "VISUAL_SOURCE_STATE_STALE"


async def test_postgres_ui_snapshot_reader_binds_primary_screenshot_checksum() -> None:
    payload = json.dumps(_profile_visual_snapshot()).encode("utf-8")
    row = {
        "storage_path": (
            f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.json"
        ),
        "checksum": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "visual_checksum": "sha256:" + "1" * 64,
    }

    class Connection:
        def __init__(self) -> None:
            self.query = ""

        async def fetchrow(self, query, *args):
            self.query = query
            assert args == (_VISUAL_DB_TASK_ID, _VISUAL_DB_CAPTURE_ID)
            return row

    class Acquire:
        def __init__(self, connection) -> None:
            self.connection = connection

        async def __aenter__(self):
            return self.connection

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def __init__(self, connection) -> None:
            self.connection = connection

        def acquire(self):
            return Acquire(self.connection)

    class ArtifactStore:
        async def read(self, storage_path):
            assert storage_path == row["storage_path"]
            return payload

    connection = Connection()
    snapshot = await PostgresTrustedUiSnapshotReader(
        pool=Pool(connection),
        artifact_store=ArtifactStore(),
    ).read_ui_snapshot(
        task_id=_VISUAL_DB_TASK_ID,
        page_capture_id=_VISUAL_DB_CAPTURE_ID,
    )

    assert snapshot["_capture_visual_checksum"] == row["visual_checksum"]
    assert "visual_evidence.artifact_type = 'screenshot'" in connection.query
    assert "visual_evidence.content_type = 'image/png'" in connection.query


async def test_postgres_ui_snapshot_reader_rejects_non_uuid_before_sql() -> None:
    class Pool:
        def acquire(self):
            raise AssertionError("非法数据库身份不应进入 SQL")

    reader = PostgresTrustedUiSnapshotReader(pool=Pool(), artifact_store=object())

    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_ui_snapshot(
            task_id="task-not-a-uuid",
            page_capture_id=_VISUAL_DB_CAPTURE_ID,
        )

    assert error.value.error_code == "UI_SNAPSHOT_SOURCE_STATE_STALE"
    assert str(error.value) == "UI Snapshot 数据库身份无效"


@pytest.mark.parametrize("failure_stage", ["database", "artifact_store"])
async def test_postgres_ui_snapshot_reader_redacts_adapter_failure(
    failure_stage: str,
) -> None:
    payload = json.dumps(_profile_visual_snapshot()).encode("utf-8")
    row = {
        "storage_path": (
            f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.json"
        ),
        "checksum": hashlib.sha256(payload).hexdigest(),
        "size_bytes": len(payload),
        "visual_checksum": "sha256:" + "1" * 64,
    }

    class ArtifactStore:
        async def read(self, storage_path: str) -> bytes:
            raise FileNotFoundError(f"C:\\private-evidence\\{storage_path}")

    reader = PostgresTrustedUiSnapshotReader(
        pool=_visual_evidence_pool(
            row=row,
            fetch_error=(
                RuntimeError("postgresql://admin:password@private-db/qc")
                if failure_stage == "database"
                else None
            ),
        ),
        artifact_store=ArtifactStore(),
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_ui_snapshot(
            task_id=_VISUAL_DB_TASK_ID,
            page_capture_id=_VISUAL_DB_CAPTURE_ID,
        )

    assert error.value.error_code == "UI_SNAPSHOT_UNAVAILABLE"
    assert str(error.value) == "UI Snapshot 暂不可用"
    serialized_error = str(error.value)
    assert "private-evidence" not in serialized_error
    assert "postgresql" not in serialized_error


async def test_postgres_ui_snapshot_reader_reads_exact_png_artifact_checksum() -> None:
    selected_png = b"\x89PNG\r\n\x1a\nselected"
    row = {
        "storage_path": (
            f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.png"
        ),
        "checksum": hashlib.sha256(selected_png).hexdigest(),
        "size_bytes": len(selected_png),
        "artifact_type": "screenshot",
        "content_type": "image/png",
    }

    class Connection:
        async def fetchrow(self, query, *args):
            assert "evidence.id = $3::uuid" in query
            assert args == (
                _VISUAL_DB_TASK_ID,
                _VISUAL_DB_CAPTURE_ID,
                _VISUAL_DB_ARTIFACT_ID,
            )
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class ArtifactStore:
        async def read(self, storage_path):
            assert storage_path == row["storage_path"]
            return selected_png

    checksum = await PostgresTrustedUiSnapshotReader(
        pool=Pool(),
        artifact_store=ArtifactStore(),
    ).read_visual_evidence_checksum(
        task_id=_VISUAL_DB_TASK_ID,
        page_capture_id=_VISUAL_DB_CAPTURE_ID,
        artifact_id=_VISUAL_DB_ARTIFACT_ID,
        frame_index=None,
    )

    assert checksum == f"sha256:{hashlib.sha256(selected_png).hexdigest()}"


async def test_postgres_ui_snapshot_reader_visual_evidence_image_returns_exact_png() -> None:
    pixels = np.zeros((6, 8, 4), dtype=np.uint8)
    pixels[:, :, 3] = 255
    encoded, buffer = cv2.imencode(".png", cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGRA))
    assert encoded is True
    selected_png = buffer.tobytes()
    expected_sha256 = f"sha256:{hashlib.sha256(selected_png).hexdigest()}"
    row = {
        "storage_path": (
            f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.png"
        ),
        "checksum": expected_sha256,
        "size_bytes": len(selected_png),
        "artifact_type": "screenshot",
        "content_type": "image/png",
    }

    class Connection:
        async def fetchrow(self, query, *args):
            assert "evidence.id = $3::uuid" in query
            assert "evidence.artifact_type = 'screenshot'" in query
            assert "evidence.content_type = 'image/png'" in query
            assert args == (
                _VISUAL_DB_TASK_ID,
                _VISUAL_DB_CAPTURE_ID,
                _VISUAL_DB_ARTIFACT_ID,
            )
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class ArtifactStore:
        async def read(self, storage_path):
            assert storage_path == row["storage_path"]
            return selected_png

    payload = await PostgresTrustedUiSnapshotReader(
        pool=Pool(),
        artifact_store=ArtifactStore(),
    ).read_visual_evidence_image(
        task_id=_VISUAL_DB_TASK_ID,
        page_capture_id=_VISUAL_DB_CAPTURE_ID,
        artifact_id=_VISUAL_DB_ARTIFACT_ID,
        expected_sha256=expected_sha256,
    )

    assert payload is selected_png


def _visual_evidence_row(
    payload: bytes,
    *,
    storage_path: str | None = None,
) -> dict[str, object]:
    return {
        "storage_path": storage_path
        or (f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.png"),
        "checksum": f"sha256:{hashlib.sha256(payload).hexdigest()}",
        "size_bytes": len(payload),
        "artifact_type": "screenshot",
        "content_type": "image/png",
    }


def _visual_evidence_pool(*, row=None, fetch_error: Exception | None = None):
    class Connection:
        async def fetchrow(self, query, *args):
            del query, args
            if fetch_error is not None:
                raise fetch_error
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    return Pool()


@pytest.mark.parametrize(
    "storage_path",
    [
        (f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/../{_OTHER_DB_CAPTURE_ID}/secret.png"),
        (f"/{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.png"),
        (
            f"C:\\private\\{_VISUAL_DB_TASK_ID}\\{_VISUAL_DB_CAPTURE_ID}\\"
            f"{_VISUAL_DB_ARTIFACT_ID}.png"
        ),
        (f"{_VISUAL_DB_TASK_ID}\\{_VISUAL_DB_CAPTURE_ID}\\{_VISUAL_DB_ARTIFACT_ID}.png"),
        (f"{_VISUAL_DB_TASK_ID}//{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.png"),
        (
            f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/"
            f"%2e%2e%2f{_OTHER_DB_CAPTURE_ID}%2fsecret.png"
        ),
        (f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/．．／{_OTHER_DB_CAPTURE_ID}／secret.png"),
        f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/secret.png",
    ],
)
async def test_visual_evidence_image_rejects_unsafe_storage_path_before_store_read(
    storage_path: str,
) -> None:
    selected_png = b"\x89PNG\r\n\x1a\nselected"

    class ArtifactStore:
        def __init__(self) -> None:
            self.read_paths: list[str] = []

        async def read(self, path: str) -> bytes:
            self.read_paths.append(path)
            return selected_png

    artifact_store = ArtifactStore()
    reader = PostgresTrustedUiSnapshotReader(
        pool=_visual_evidence_pool(
            row=_visual_evidence_row(selected_png, storage_path=storage_path)
        ),
        artifact_store=artifact_store,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_visual_evidence_image(
            task_id=_VISUAL_DB_TASK_ID,
            page_capture_id=_VISUAL_DB_CAPTURE_ID,
            artifact_id=_VISUAL_DB_ARTIFACT_ID,
            expected_sha256=f"sha256:{hashlib.sha256(selected_png).hexdigest()}",
        )

    assert error.value.error_code == "VISUAL_SOURCE_ARTIFACT_OWNERSHIP_MISMATCH"
    assert str(error.value) == "视觉坐标截图证据归属不匹配"
    assert artifact_store.read_paths == []
    assert "secret" not in str(error.value)


@pytest.mark.parametrize(
    ("task_id", "page_capture_id", "artifact_id"),
    [
        ("task-not-a-uuid", _VISUAL_DB_CAPTURE_ID, _VISUAL_DB_ARTIFACT_ID),
        (_VISUAL_DB_TASK_ID, "capture-not-a-uuid", _VISUAL_DB_ARTIFACT_ID),
        (_VISUAL_DB_TASK_ID, _VISUAL_DB_CAPTURE_ID, "artifact-not-a-uuid"),
    ],
)
async def test_visual_evidence_image_rejects_non_uuid_identifiers_before_sql(
    task_id: str,
    page_capture_id: str,
    artifact_id: str,
) -> None:
    class Pool:
        def acquire(self):
            raise AssertionError("非法数据库身份不应进入 SQL")

    reader = PostgresTrustedUiSnapshotReader(pool=Pool(), artifact_store=object())

    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_visual_evidence_image(
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=artifact_id,
            expected_sha256="sha256:" + "a" * 64,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert str(error.value) == "视觉导航来源数据库身份无效"


async def test_visual_evidence_image_redacts_database_adapter_failure() -> None:
    reader = PostgresTrustedUiSnapshotReader(
        pool=_visual_evidence_pool(
            fetch_error=RuntimeError(
                "postgresql://admin:password@private-db/qc storage_key=private-key"
            )
        ),
        artifact_store=object(),
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_visual_evidence_image(
            task_id=_VISUAL_DB_TASK_ID,
            page_capture_id=_VISUAL_DB_CAPTURE_ID,
            artifact_id=_VISUAL_DB_ARTIFACT_ID,
            expected_sha256="sha256:" + "a" * 64,
        )

    assert error.value.error_code == "VISUAL_SOURCE_ARTIFACT_UNAVAILABLE"
    assert str(error.value) == "视觉坐标截图证据暂不可用"
    assert "postgresql" not in str(error.value)
    assert "private-key" not in str(error.value)


@pytest.mark.parametrize(
    "store_error",
    [
        FileNotFoundError("C:\\private-evidence\\secret.png"),
        RuntimeError("MinIO bucket=secret-bucket key=private-key endpoint=s3.internal"),
    ],
)
async def test_visual_evidence_image_redacts_artifact_store_failure(
    store_error: Exception,
) -> None:
    selected_png = b"\x89PNG\r\n\x1a\nselected"

    class ArtifactStore:
        async def read(self, storage_path: str) -> bytes:
            del storage_path
            raise store_error

    reader = PostgresTrustedUiSnapshotReader(
        pool=_visual_evidence_pool(row=_visual_evidence_row(selected_png)),
        artifact_store=ArtifactStore(),
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_visual_evidence_image(
            task_id=_VISUAL_DB_TASK_ID,
            page_capture_id=_VISUAL_DB_CAPTURE_ID,
            artifact_id=_VISUAL_DB_ARTIFACT_ID,
            expected_sha256=f"sha256:{hashlib.sha256(selected_png).hexdigest()}",
        )

    assert error.value.error_code == "VISUAL_SOURCE_ARTIFACT_UNAVAILABLE"
    assert str(error.value) == "视觉坐标截图证据暂不可用"
    serialized_error = str(error.value)
    for secret in ("private-evidence", "secret-bucket", "private-key", "s3.internal"):
        assert secret not in serialized_error


async def test_visual_evidence_image_does_not_swallow_store_cancellation() -> None:
    selected_png = b"\x89PNG\r\n\x1a\nselected"

    class ArtifactStore:
        async def read(self, storage_path: str) -> bytes:
            del storage_path
            raise asyncio.CancelledError

    reader = PostgresTrustedUiSnapshotReader(
        pool=_visual_evidence_pool(row=_visual_evidence_row(selected_png)),
        artifact_store=ArtifactStore(),
    )

    with pytest.raises(asyncio.CancelledError):
        await reader.read_visual_evidence_image(
            task_id=_VISUAL_DB_TASK_ID,
            page_capture_id=_VISUAL_DB_CAPTURE_ID,
            artifact_id=_VISUAL_DB_ARTIFACT_ID,
            expected_sha256=f"sha256:{hashlib.sha256(selected_png).hexdigest()}",
        )


@pytest.mark.parametrize("through_agent_tool", [False, True])
async def test_visual_evidence_adapter_failure_never_leaks_from_runtime(
    through_agent_tool: bool,
) -> None:
    selected_png = b"\x89PNG\r\n\x1a\nselected"

    class ArtifactStore:
        async def read(self, storage_path: str) -> bytes:
            raise FileNotFoundError(f"C:\\private-evidence\\{storage_path}")

    reader = PostgresTrustedUiSnapshotReader(
        pool=_visual_evidence_pool(row=_visual_evidence_row(selected_png)),
        artifact_store=ArtifactStore(),
    )

    class ReaderBackedResolver:
        async def resolve(self, request):
            del request
            return await reader.read_visual_evidence_image(
                task_id=_VISUAL_DB_TASK_ID,
                page_capture_id=_VISUAL_DB_CAPTURE_ID,
                artifact_id=_VISUAL_DB_ARTIFACT_ID,
                expected_sha256=f"sha256:{hashlib.sha256(selected_png).hexdigest()}",
            )

    class Browser:
        async def capture(self, request):
            raise AssertionError(f"来源读取失败后不应调用浏览器：{request}")

    evidence_buffer = InMemoryActionEvidenceBuffer()
    runtime_executor = BrowserInteractionExecutor(
        browser=Browser(),
        target_resolver=ReaderBackedResolver(),
        evidence_buffer=evidence_buffer,
    )
    executor = (
        AgentToolInteractionExecutor(
            executor=runtime_executor,
            evidence_buffer=evidence_buffer,
        )
        if through_agent_tool
        else runtime_executor
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-1",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "VISUAL_SOURCE_ARTIFACT_UNAVAILABLE"
    assert result.error_message == "视觉坐标截图证据暂不可用"
    serialized_result = str(result.to_dict())
    assert "private-evidence" not in serialized_result
    assert _VISUAL_DB_ARTIFACT_ID not in serialized_result


@pytest.mark.parametrize(
    ("row_updates", "expected_sha256", "error_code"),
    [
        (
            {"size_bytes": 1},
            "actual",
            "VISUAL_SOURCE_ARTIFACT_SIZE_MISMATCH",
        ),
        (
            {"checksum": "sha256:" + "0" * 64},
            "actual",
            "VISUAL_SOURCE_ARTIFACT_CHECKSUM_MISMATCH",
        ),
        (
            {},
            "sha256:" + "f" * 64,
            "VISUAL_TARGET_SOURCE_STATE_STALE",
        ),
        (
            {"artifact_type": "dom"},
            "actual",
            "VISUAL_SOURCE_FRAME_INVALID",
        ),
        (
            {"content_type": "image/jpeg"},
            "actual",
            "VISUAL_SOURCE_FRAME_INVALID",
        ),
    ],
)
async def test_postgres_ui_snapshot_reader_visual_evidence_image_fails_closed(
    row_updates: dict[str, object],
    expected_sha256: str,
    error_code: str,
) -> None:
    pixels = np.zeros((6, 8, 4), dtype=np.uint8)
    pixels[:, :, 3] = 255
    encoded, buffer = cv2.imencode(".png", cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGRA))
    assert encoded is True
    selected_png = buffer.tobytes()
    actual_sha256 = f"sha256:{hashlib.sha256(selected_png).hexdigest()}"
    row = {
        "storage_path": (
            f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.png"
        ),
        "checksum": actual_sha256,
        "size_bytes": len(selected_png),
        "artifact_type": "screenshot",
        "content_type": "image/png",
        **row_updates,
    }

    class Connection:
        async def fetchrow(self, query, *args):
            del query, args
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class ArtifactStore:
        async def read(self, storage_path):
            assert storage_path == row["storage_path"]
            return selected_png

    reader = PostgresTrustedUiSnapshotReader(
        pool=Pool(),
        artifact_store=ArtifactStore(),
    )
    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_visual_evidence_image(
            task_id=_VISUAL_DB_TASK_ID,
            page_capture_id=_VISUAL_DB_CAPTURE_ID,
            artifact_id=_VISUAL_DB_ARTIFACT_ID,
            expected_sha256=(actual_sha256 if expected_sha256 == "actual" else expected_sha256),
        )

    assert error.value.error_code == error_code


@pytest.mark.parametrize(
    ("task_id", "page_capture_id", "artifact_id"),
    [
        (_OTHER_DB_TASK_ID, _VISUAL_DB_CAPTURE_ID, _VISUAL_DB_ARTIFACT_ID),
        (_VISUAL_DB_TASK_ID, _OTHER_DB_CAPTURE_ID, _VISUAL_DB_ARTIFACT_ID),
        (_VISUAL_DB_TASK_ID, _VISUAL_DB_CAPTURE_ID, _OTHER_DB_ARTIFACT_ID),
    ],
)
async def test_postgres_ui_snapshot_reader_visual_evidence_image_rejects_wrong_owner(
    task_id: str,
    page_capture_id: str,
    artifact_id: str,
) -> None:
    class Connection:
        async def fetchrow(self, query, *args):
            del query
            return (
                {"unexpected": True}
                if args
                == (
                    _VISUAL_DB_TASK_ID,
                    _VISUAL_DB_CAPTURE_ID,
                    _VISUAL_DB_ARTIFACT_ID,
                )
                else None
            )

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class ArtifactStore:
        async def read(self, storage_path):
            raise AssertionError(f"不应读取未归属证据：{storage_path}")

    reader = PostgresTrustedUiSnapshotReader(
        pool=Pool(),
        artifact_store=ArtifactStore(),
    )
    with pytest.raises(SemanticTargetResolutionError) as error:
        await reader.read_visual_evidence_image(
            task_id=task_id,
            page_capture_id=page_capture_id,
            artifact_id=artifact_id,
            expected_sha256="sha256:" + "a" * 64,
        )

    assert error.value.error_code == "VISUAL_SOURCE_ARTIFACT_MISSING"


async def test_postgres_ui_snapshot_reader_hashes_primary_png_target_region() -> None:
    pixels = np.zeros((4, 4, 3), dtype=np.uint8)
    pixels[:2, :2] = (3, 7, 11)
    encoded, buffer = cv2.imencode(".png", pixels)
    assert encoded is True
    primary_png = buffer.tobytes()
    row = {
        "storage_path": (
            f"{_VISUAL_DB_TASK_ID}/{_VISUAL_DB_CAPTURE_ID}/{_VISUAL_DB_ARTIFACT_ID}.png"
        ),
        "checksum": hashlib.sha256(primary_png).hexdigest(),
        "size_bytes": len(primary_png),
        "artifact_type": "screenshot",
        "content_type": "image/png",
    }

    class Connection:
        async def fetchrow(self, query, *args):
            assert "evidence.id = $3::uuid" not in query
            assert "ORDER BY evidence.created_at ASC" in query
            assert args == (_VISUAL_DB_TASK_ID, _VISUAL_DB_CAPTURE_ID)
            return row

    class Acquire:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    class Pool:
        def acquire(self):
            return Acquire()

    class ArtifactStore:
        async def read(self, storage_path):
            assert storage_path == row["storage_path"]
            return primary_png

    checksum = await PostgresTrustedUiSnapshotReader(
        pool=Pool(),
        artifact_store=ArtifactStore(),
    ).read_visual_evidence_region_checksum(
        task_id=_VISUAL_DB_TASK_ID,
        page_capture_id=_VISUAL_DB_CAPTURE_ID,
        artifact_id=None,
        left_ratio=0,
        top_ratio=0,
        right_ratio=0.5,
        bottom_ratio=0.5,
    )

    expected = hashlib.sha256()
    expected.update(b"2x2x3:uint8\0")
    expected.update(pixels[:2, :2].tobytes())
    assert checksum == f"sha256:{expected.hexdigest()}"


@pytest.mark.parametrize(
    ("request_overrides", "parameter_overrides", "snapshot_url", "error_code"),
    [
        (
            {},
            {"visual_source_capture_id": None},
            "https://example.test/profile",
            "VISUAL_SOURCE_CAPTURE_INVALID",
        ),
        (
            {},
            {"visual_source_artifact_id": None},
            "https://example.test/profile",
            "VISUAL_SOURCE_ARTIFACT_INVALID",
        ),
        (
            {},
            {"visual_source_sha256": None},
            "https://example.test/profile",
            "VISUAL_SOURCE_ARTIFACT_INVALID",
        ),
        (
            {},
            {"visual_source_url": "https://example.test/other"},
            "https://example.test/profile",
            "VISUAL_SOURCE_URL_MISMATCH",
        ),
        (
            {"semantic_target": "其他入口"},
            {},
            "https://example.test/profile",
            "VISUAL_TARGET_NOT_ALLOWED",
        ),
        (
            {},
            {"visual_confidence": 0.79},
            "https://example.test/profile",
            "VISUAL_BOUNDS_INVALID",
        ),
        (
            {},
            {
                "left_ratio": 0.0,
                "top_ratio": 0.0,
                "right_ratio": 0.9,
                "bottom_ratio": 0.9,
            },
            "https://example.test/profile",
            "VISUAL_BOUNDS_INVALID",
        ),
        (
            {"action_type": InteractionOperation.HOVER},
            {},
            "https://example.test/profile",
            "VISUAL_OPERATION_NOT_ALLOWED",
        ),
    ],
)
async def test_visual_profile_payment_target_rejects_untrusted_parameters(
    request_overrides: dict[str, object],
    parameter_overrides: dict[str, object],
    snapshot_url: str,
    error_code: str,
) -> None:
    parameters = {**_profile_visual_parameters(), **parameter_overrides}
    request_values = {
        "task_id": "task-1",
        "session_id": "session-1",
        "source_capture_id": "capture-1",
        "action_type": InteractionOperation.CLICK,
        "semantic_target": "会员中心",
        "parameters": parameters,
        **request_overrides,
    }
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(_profile_visual_snapshot(url=snapshot_url))
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(SemanticTargetResolutionRequest(**request_values))

    assert error.value.error_code == error_code


async def test_semantic_resolver_accepts_only_media_or_progress_target_for_seek() -> None:
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(
            {
                "elements": [
                    {
                        "selector": "#progress",
                        "tag": "div",
                        "role": "slider",
                        "aria_label": "视频播放进度条",
                    },
                    {
                        "selector": "#label",
                        "tag": "span",
                        "text": "视频播放进度条",
                    },
                ]
            }
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.SEEK_MEDIA,
            semantic_target="视频播放进度条",
            parameters={"target_fraction": 0.5},
        )
    )

    assert target.selector == "#progress"


async def test_semantic_resolver_uses_unique_unlabelled_media_for_generic_seek_target() -> None:
    """播放器没有暴露可访问进度条时，唯一媒体元素仍可承载有界 seek。"""
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(
            {
                "elements": [
                    {
                        "selector": "#player > video",
                        "tag": "video",
                        "text": "",
                        "aria_label": None,
                        "interactive": True,
                        "media": {"duration": 120.0, "ready_state": 4},
                    }
                ]
            }
        )
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.SEEK_MEDIA,
            semantic_target="播放进度条",
            parameters={"target_fraction": 0.5},
        )
    )

    assert target.selector == "#player > video"


async def test_semantic_resolver_rejects_noninteractive_match_and_unsafe_navigation() -> None:
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(
            {"elements": [{"selector": "p", "tag": "p", "text": "登录"}]}
        )
    )

    with pytest.raises(SemanticTargetResolutionError) as target_error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="登录",
                parameters={},
            )
        )
    assert target_error.value.error_code == "SEMANTIC_TARGET_NOT_FOUND"

    with pytest.raises(SemanticTargetResolutionError) as navigation_error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.NAVIGATE,
                semantic_target=None,
                parameters={"destination_url": "file:///tmp/private"},
            )
        )
    assert navigation_error.value.error_code == "NAVIGATION_TARGET_INVALID"


async def test_session_command_gate_serializes_full_action_boundary() -> None:
    gate = InProcessSessionCommandGate()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first() -> None:
        async with gate.hold(task_id="task-1", session_id="session-1"):
            first_entered.set()
            await release_first.wait()

    async def second() -> None:
        await first_entered.wait()
        async with gate.hold(task_id="task-1", session_id="session-1"):
            second_entered.set()

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    assert not second_entered.is_set()
    with pytest.raises(RuntimeError, match="仍有动作执行"):
        await gate.release_session(task_id="task-1", session_id="session-1")
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert second_entered.is_set()

    with pytest.raises(ValueError, match="禁止跨任务复用"):
        async with gate.hold(task_id="task-2", session_id="session-1"):
            pass


async def test_overlay_ad_close_capability_resolves_from_snapshot() -> None:
    """检测端 _OverlayAdPopupParser 已签发 overlay_ad_close，解析端却没有该能力，
    遮罩层广告站（91JAV 实测 20 次连败）永远关不掉弹窗。"""
    from services.api.app.infrastructure.browser.page_capture import (
        POPUP_OVERLAY_AD_CLOSE_SELECTOR,
    )

    # 结构取自 91JAV 首页真实 UI Snapshot：关闭控件无任何 dialog 标记，
    # 仅靠自身类名 + van-overlay 祖先可证。
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "html > body > div:nth-of-type(2) > div",
                    "tag": "div",
                    "class_tokens": ["van-overlay"],
                    "ancestor_class_tokens": [],
                },
                {
                    "selector": "div:nth-of-type(2) > div > div > div:nth-of-type(1)",
                    "tag": "div",
                    "class_tokens": ["close-img", "on-popup-close"],
                    "ancestor_class_tokens": [
                        "adaptation",
                        "advertise-box",
                        "popup-form",
                        "van-overlay",
                    ],
                },
                {
                    "selector": "div > div > div > div > img",
                    "tag": "img",
                    "class_tokens": ["close-image"],
                    "ancestor_class_tokens": ["close-img", "on-popup-close", "van-overlay"],
                },
            ]
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
            },
        )
    )

    assert target.selector == POPUP_OVERLAY_AD_CLOSE_SELECTOR
    assert target.allow_high_z_obstruction_hide is True


async def test_overlay_ad_close_resolves_adspop_close_not_external_ad_body() -> None:
    """adspop 只允许命中可逆关闭键，广告图片外链即使同框也不能成为动作目标。"""
    from services.api.app.infrastructure.browser.page_capture import (
        POPUP_OVERLAY_AD_CLOSE_SELECTOR,
    )

    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".adspop .popup-picture img",
                    "tag": "img",
                    "class_tokens": ["tjtagmanager"],
                    "ancestor_class_tokens": [
                        "popup-picture",
                        "popup-content",
                        "popup-container",
                        "adspop",
                    ],
                    "href": "https://ads.example/landing",
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": ".adspop .popup-close",
                    "tag": "div",
                    "class_tokens": ["popup-close"],
                    "ancestor_class_tokens": [
                        "popup-content",
                        "popup-container",
                        "adspop",
                    ],
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-adspop",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "8" * 64,
            },
        )
    )

    assert target.selector == POPUP_OVERLAY_AD_CLOSE_SELECTOR
    assert target.allow_high_z_obstruction_hide is True


async def test_overlay_ad_close_resolves_recommend_layer_close_not_ad_body() -> None:
    """推荐广告层只允许命中唯一 close-btn，广告图片链接不得成为动作目标。"""
    from services.api.app.infrastructure.browser.page_capture import (
        POPUP_OVERLAY_AD_CLOSE_SELECTOR,
    )

    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".recommend-layer .media-box img",
                    "tag": "img",
                    "class_tokens": ["tjtagmanager"],
                    "ancestor_class_tokens": [
                        "media-box",
                        "recommend-card",
                        "recommend-wrapper",
                        "recommend-layer",
                    ],
                    "data_url": "https://ads.example/landing",
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": ".recommend-layer .close-btn",
                    "tag": "div",
                    "class_tokens": ["close-btn"],
                    "ancestor_class_tokens": [
                        "recommend-card",
                        "recommend-wrapper",
                        "recommend-layer",
                    ],
                    "web_recommend_ad_close_direct_chain": True,
                    "navigation_payload_in_subtree": False,
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-recommend-layer",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "6" * 64,
            },
        )
    )

    assert target.selector == POPUP_OVERLAY_AD_CLOSE_SELECTOR
    assert target.allow_high_z_obstruction_hide is True


async def test_overlay_ad_close_rejects_navigable_recommend_layer_close() -> None:
    """复用 close-btn 类名的广告链接不得被解析成可逆关闭控件。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": ".recommend-layer a.close-btn",
                    "tag": "a",
                    "role": "link",
                    "class_tokens": ["close-btn"],
                    "ancestor_class_tokens": [
                        "recommend-card",
                        "recommend-wrapper",
                        "recommend-layer",
                    ],
                    "href": "https://ads.example/landing",
                    "web_recommend_ad_close_direct_chain": True,
                    "navigation_payload_in_subtree": True,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-recommend-layer-link",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前弹窗",
                parameters={
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": "overlay_ad_close",
                    "obstruction_signature": "h5-obstruction:" + "5" * 64,
                },
            )
        )

    assert error.value.error_code == "POPUP_DISMISSAL_NOT_FOUND"


async def test_overlay_ad_close_resolves_nine_grid_popup_close() -> None:
    """九宫格广告的通用 close 仅在遮罩弹窗作用域内才可作为关闭控件。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "div > div:nth-of-type(4) > div > div > div:nth-of-type(2)",
                    "tag": "div",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": [
                        "wrapper",
                        "van-overlay",
                        "nineGridPopup",
                        "home",
                    ],
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-nine-grid-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "d" * 64,
            },
        )
    )

    assert (
        ".nineGridPopup:visible > .van-overlay:visible > .wrapper:visible > .close:visible"
        in target.selector
    )
    assert target.allow_high_z_obstruction_hide is True


async def test_overlay_ad_close_resolves_announcement_popup_close() -> None:
    """公告广告的通用 close 仅在公告遮罩图片容器内才可作为关闭控件。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "div > div:nth-of-type(4) > div > div > div:nth-of-type(3)",
                    "tag": "div",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": [
                        "imgWrapper",
                        "van-overlay",
                        "announcementPopup",
                        "home",
                    ],
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-announcement-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "e" * 64,
            },
        )
    )

    assert (
        ".announcementPopup:visible > .van-overlay:visible > .imgWrapper:visible > .close:visible"
        in target.selector
    )
    assert target.allow_high_z_obstruction_hide is True


async def test_overlay_ad_close_resolves_center_text_popup_close() -> None:
    """居中公告层的 closeBtn 只在 textPopContent + van-popup 作用域内生效。"""
    from services.api.app.infrastructure.browser.page_capture import (
        POPUP_OVERLAY_AD_CLOSE_SELECTOR,
    )

    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#center-text-popup-close",
                    "tag": "div",
                    "class_tokens": ["closeBtn"],
                    "ancestor_class_tokens": [
                        "textPopContent",
                        "van-popup",
                        "van-popup--center",
                    ],
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#page-close",
                    "tag": "div",
                    "class_tokens": ["closeBtn"],
                    "ancestor_class_tokens": ["content-card"],
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-center-text-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "f" * 64,
            },
        )
    )

    assert target.selector == POPUP_OVERLAY_AD_CLOSE_SELECTOR


async def test_overlay_ad_close_resolves_center_image_popup_close() -> None:
    """居中图片广告层的通用 close 只在 van-popup 图片容器作用域内生效。"""
    from services.api.app.infrastructure.browser.page_capture import (
        POPUP_OVERLAY_AD_CLOSE_SELECTOR,
    )

    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#center-image-popup-close",
                    "tag": "div",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": [
                        "imgWrapper",
                        "van-popup",
                        "van-popup--center",
                    ],
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#page-close",
                    "tag": "div",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": ["content-card"],
                    "hit_test": {"center_hits_target": True},
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-center-image-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "7" * 64,
            },
        )
    )

    assert target.selector == POPUP_OVERLAY_AD_CLOSE_SELECTOR


async def test_overlay_ad_close_requires_overlay_ancestor() -> None:
    """同名类若不在遮罩层下（如页面内嵌的关闭图标）不得被当成弹窗关闭控件。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#inline-close",
                    "tag": "div",
                    "class_tokens": ["close-img"],
                    "ancestor_class_tokens": ["content-card"],
                }
            ]
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="关闭当前弹窗",
                parameters={
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": "overlay_ad_close",
                    "obstruction_signature": "h5-obstruction:" + "b" * 64,
                },
            )
        )

    assert error.value.error_code == "POPUP_DISMISSAL_NOT_FOUND"


async def test_overlay_ad_close_resolves_bootstrap_ad_dialog() -> None:
    """91porna 式广告弹窗的关闭控件类名很通用（close），须靠 ad-dialog 祖先限定。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "div.modal.dx-modal.ad-dialog.in > div > div > span",
                    "tag": "span",
                    "text": "×",
                    "class_tokens": ["close", "text-subtitle", "ml-auto"],
                    "ancestor_class_tokens": [
                        "modal",
                        "dx-modal",
                        "ad-dialog",
                        "in",
                        "modal-header",
                    ],
                },
                {
                    "selector": "#page-close",
                    "tag": "span",
                    "text": "×",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": ["content-card"],
                },
            ]
        }
    )
    resolver = UiSnapshotSemanticTargetResolver(snapshot_reader=reader)

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "overlay_ad_close",
                "obstruction_signature": "h5-obstruction:" + "c" * 64,
            },
        )
    )

    assert ".ad-dialog:visible" in target.selector


async def test_age_verification_resolves_unique_adult_confirm_and_never_exit() -> None:
    """年龄门禁必须从封存 Snapshot 唯一解析右侧成年确认控件。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#age-exit",
                    "tag": "div",
                    "class_tokens": ["btn-exit"],
                    "ancestor_class_tokens": [
                        "age-verify-buttons",
                        "age-verify-content",
                        "age-verify-popup",
                    ],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#age-confirm",
                    "tag": "div",
                    "class_tokens": ["btn-confirm"],
                    "ancestor_class_tokens": [
                        "age-verify-buttons",
                        "age-verify-content",
                        "age-verify-popup",
                    ],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#age-background",
                    "tag": "img",
                    "class_tokens": ["age-verify-bg"],
                    "ancestor_class_tokens": [
                        "age-verify-content",
                        "age-verify-popup",
                    ],
                    "src": "https://example.test/png/eighteenYearsBg.png",
                },
            ]
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-age-verification",
            action_type=InteractionOperation.CLICK,
            semantic_target="年满进入",
            parameters={
                "target_mode": "age_verification_confirm",
                "age_verification_capability": "h5_age_verification_confirm.v1",
            },
        )
    )

    assert target.selector == "#age-confirm"


async def test_age_verification_rejects_confirm_without_full_gate_structure() -> None:
    """页面内孤立同名 class 不得冒充年龄门禁确认按钮。"""
    reader = StaticSnapshotReader(
        {
            "elements": [
                {
                    "selector": "#unscoped-confirm",
                    "tag": "div",
                    "class_tokens": ["btn-confirm"],
                    "ancestor_class_tokens": ["content-card"],
                    "interactive": True,
                    "hit_test": {"center_hits_target": True},
                }
            ]
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-age-verification",
                action_type=InteractionOperation.CLICK,
                semantic_target="年满进入",
                parameters={
                    "target_mode": "age_verification_confirm",
                    "age_verification_capability": "h5_age_verification_confirm.v1",
                },
            )
        )

    assert error.value.error_code == "AGE_VERIFICATION_TARGET_NOT_FOUND"


def _logical_target_snapshot(
    *,
    identity: str = "sha256:" + "1" * 64,
    state: str = "sha256:" + "2" * 64,
) -> dict[str, object]:
    return {
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": identity,
            "state_fingerprint": state,
        },
        "elements": [
            {
                "selector": "#flt-semantic-node-101",
                "tag": "flt-semantics",
                "role": "button",
                "text": "我的",
                "semantics_node_id": 101,
                "interactive": True,
                "rect": {"x": 300, "y": 720, "width": 70, "height": 48},
                "hit_test": {"center_hits_target": True},
            }
        ],
    }


def _logical_target_contract(
    *,
    identity: str = "sha256:" + "1" * 64,
    state: str = "sha256:" + "2" * 64,
    expects_navigation: bool = True,
) -> dict[str, object]:
    return {
        "protocol_version": "logical_target.v1",
        "source_identity_fingerprint": identity,
        "source_state_fingerprint": state,
        "expects_logical_navigation": expects_navigation,
    }


def _visual_navigation_png() -> tuple[bytes, bytes, int, int]:
    image_height = 80
    image_width = 40
    y_index, x_index = np.indices((image_height, image_width), dtype=np.uint16)
    rgba = np.empty((image_height, image_width, 4), dtype=np.uint8)
    rgba[:, :, 0] = (x_index * 7 % 256).astype(np.uint8)
    rgba[:, :, 1] = (y_index * 5 % 256).astype(np.uint8)
    rgba[:, :, 2] = ((x_index + y_index) * 3 % 256).astype(np.uint8)
    rgba[:, :, 3] = ((x_index * 2 + y_index * 3) % 256).astype(np.uint8)
    encoded, buffer = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    assert encoded is True
    return buffer.tobytes(), rgba.tobytes(), image_width, image_height


def _visual_navigation_snapshot(
    png: bytes,
    *,
    identity: str = "sha256:" + "1" * 64,
    state: str = "sha256:" + "2" * 64,
    page_type_confidence: float = 0.92,
) -> dict[str, object]:
    image = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert image is not None
    image_height, image_width = image.shape[:2]
    screenshot_sha256 = f"sha256:{hashlib.sha256(png).hexdigest()}"
    render_profile = {
        "kind": "flutter_web",
        "status": "confirmed",
        "confidence": 0.96,
    }
    return {
        "viewport": {
            "visual_viewport_width": image_width,
            "visual_viewport_height": image_height,
            "visual_viewport_offset_left": 0,
            "visual_viewport_offset_top": 0,
            "visual_viewport_page_left": 0,
            "visual_viewport_page_top": 0,
            "visual_viewport_scale": 1,
        },
        "visual_navigation_capture": {
            "protocol_version": "visual_navigation_capture.v1",
            "purpose": "visual_navigation_viewport",
            "coordinate_space": "visual_viewport_css_pixels",
            "screenshot_sha256": screenshot_sha256,
            "image_width": image_width,
            "image_height": image_height,
        },
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": identity,
            "state_fingerprint": state,
            "render_profile": render_profile,
            "semantics_coverage": {"level": "semantics_sparse"},
            "page_type_binding": {
                "page_type": "home",
                "identity_fingerprint": identity,
                "page_type_confidence": page_type_confidence,
                "coverage_allowed": True,
            },
        },
        "elements": [],
    }


def _visual_navigation_capability(
    png: bytes,
    rgba_bytes: bytes,
    image_width: int,
    image_height: int,
    **updates: object,
) -> dict[str, object]:
    normalized_rect = {"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.3}
    region_digests = visual_navigation_region_digests(
        pixel_bytes=rgba_bytes,
        image_width=image_width,
        image_height=image_height,
        channel_count=4,
        normalized_rect=normalized_rect,
    )
    values: dict[str, object] = {
        "source_task_id": "task-visual",
        "source_capture_id": "capture-visual",
        "source_artifact_id": "artifact-visual",
        "source_artifact_sha256": f"sha256:{hashlib.sha256(png).hexdigest()}",
        "source_identity_fingerprint": "sha256:" + "1" * 64,
        "source_state_fingerprint": "sha256:" + "2" * 64,
        "source_page_type": "home",
        "source_page_type_confidence": 0.92,
        "source_identity_bound": True,
        "render_profile": {
            "kind": "flutter_web",
            "status": "confirmed",
            "confidence": 0.96,
        },
        "semantics_coverage_level": "semantics_sparse",
        "target_kind": "content_card",
        "label": "内容卡片",
        "expected_page_type": "content_detail",
        "visual_target_confidence": 0.93,
        "selected": False,
        "unique": True,
        "risk_flags": (),
        "normalized_rect": normalized_rect,
        "source_image_size": {"width": image_width, "height": image_height},
        "viewport_metrics": {
            "width": image_width,
            "height": image_height,
            "offset_left": 0,
            "offset_top": 0,
            "page_left": 0,
            "page_top": 0,
            "scale": 1,
        },
        "model_image_transform": {
            "kind": "identity",
            "scale_x": 1.0,
            "scale_y": 1.0,
            "offset_x": 0.0,
            "offset_y": 0.0,
        },
        **region_digests,
        **updates,
    }
    return build_visual_navigation_capability(**values)


def _resign_visual_navigation_capability(
    capability: dict[str, object],
    **updates: object,
) -> dict[str, object]:
    result = {**capability, **updates}
    result["target_signature"] = stable_h5_surface_hash(
        {key: result[key] for key in sorted(result) if key != "target_signature"}
    )
    return result


def _visual_navigation_resolution_request(
    capability: dict[str, object],
) -> SemanticTargetResolutionRequest:
    return SemanticTargetResolutionRequest(
        task_id="task-visual",
        session_id="session-visual",
        source_capture_id="capture-visual",
        action_type=InteractionOperation.CLICK,
        semantic_target="进入 content_detail 页面",
        parameters={
            "target_mode": "visual_navigation_coordinate",
            "visual_navigation_capability": capability,
        },
    )


def _visual_navigation_resolver(
    reader: StaticSnapshotReader,
) -> UiSnapshotSemanticTargetResolver:
    return UiSnapshotSemanticTargetResolver(
        snapshot_reader=reader,
        logical_targets_enabled=True,
        h5_visual_coordinate_mode="enforced",
    )


async def test_visual_navigation_resolver_returns_internal_verified_target() -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    reader = StaticSnapshotReader(
        _visual_navigation_snapshot(png),
        visual_png=png,
    )
    request = _visual_navigation_resolution_request(capability)

    target = await _visual_navigation_resolver(reader).resolve(request)

    runtime_target = target.visual_navigation_target
    assert runtime_target is not None
    assert type(runtime_target).__name__ == "VisualNavigationRuntimeTarget"
    assert runtime_target.capability.to_dict() == capability
    assert runtime_target.source_png is png
    assert runtime_target.source_image_sha256 == capability["source_artifact_sha256"]
    assert target.selector is None
    assert target.click_x_ratio is None
    assert request.parameters == {
        "target_mode": "visual_navigation_coordinate",
        "visual_navigation_capability": capability,
    }
    assert reader.calls == [("task-visual", "capture-visual")]
    assert reader.visual_image_calls == [
        (
            "task-visual",
            "capture-visual",
            "artifact-visual",
            capability["source_artifact_sha256"],
        )
    ]
    assert reader.visual_calls == []
    assert reader.visual_region_calls == []


async def test_visual_navigation_runtime_target_uses_private_browser_request_channel() -> None:
    """内部 PNG 目标若进入 repr 或公开结果，会突破 Task 5 的进程内边界。"""
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability_payload = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    runtime_target = VisualNavigationRuntimeTarget(
        capability=verify_visual_navigation_capability(capability_payload),
        source_png=png,
        source_image_sha256=capability_payload["source_artifact_sha256"],
    )
    browser = RecordingBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(visual_navigation_target=runtime_target)
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(parameters={}),
            agent_run_id="run-visual",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    browser_request = browser.requests[0]
    assert browser_request._visual_navigation_target is runtime_target
    assert browser_request.selector is None
    assert browser_request.full_page is False
    assert "VisualNavigationRuntimeTarget" not in repr(browser_request)
    assert "source_png" not in repr(browser_request)
    assert png.hex()[:32] not in repr(browser_request)
    assert "source_png" not in repr(result.to_dict())


async def test_visual_navigation_outer_deadline_records_nonretryable_commit_once() -> None:
    """外层 asyncio.timeout 取消清理后，幂等重放不得再次发出 click。"""
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability_payload = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    runtime_target = VisualNavigationRuntimeTarget(
        capability=verify_visual_navigation_capability(capability_payload),
        source_png=png,
        source_image_sha256=capability_payload["source_artifact_sha256"],
    )

    class Mouse:
        def __init__(self) -> None:
            self.clicks = 0

        async def click(self) -> None:
            self.clicks += 1

    class PostclickCleanupStalledBrowser:
        def __init__(self) -> None:
            self.mouse = Mouse()
            self.requests: list[PageCaptureRequest] = []

        async def capture_with_timeout(
            self,
            browser_request: PageCaptureRequest,
            *,
            timeout_seconds: float,
        ) -> PageCaptureResult:
            del timeout_seconds
            self.requests.append(browser_request)
            await self.mouse.click()
            page_capture_module._commit_visual_navigation_click(
                browser_request,
                resolved_by="validated_visual_navigation_coordinate",
                visual_target_relocated=False,
                visual_match_score=1.0,
                visual_audit_codes=(),
            )
            await asyncio.Event().wait()
            raise AssertionError("清理阻塞时不应完成 Capture")

    browser = PostclickCleanupStalledBrowser()
    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(visual_navigation_target=runtime_target)
        ),
        evidence_buffer=buffer,
        action_timeout_seconds=0.01,
    )
    execute_request = ExecuteInteractionRequest(
        intent=_intent(parameters={}),
        agent_run_id="run-visual-timeout",
        source_capture_id="capture-before",
        action_execution_id="visual-action-once",
    )

    first = await executor.execute(execute_request)
    second = await executor.execute(execute_request)

    assert first.status is InteractionResultStatus.FAILED
    assert first.error_code == "VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED"
    assert first.error_message == "视觉点击已提交，清理未完成，禁止自动重试"
    assert first.retryable is False
    assert first.state_diff == {"visual_action_committed": True}
    assert second == first
    assert browser.mouse.clicks == 1
    assert len(browser.requests) == 1
    assert await buffer.get(action_execution_id="visual-action-once") is not None
    serialized = repr(browser.requests[0])
    assert "click_committed" not in serialized
    assert "validated_visual_navigation_coordinate" not in serialized


async def test_real_playwright_request_clones_preserve_committed_click_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实两层请求克隆不得丢失已提交点击，避免外层超时重放。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    try:
        async with async_playwright() as playwright:
            probe = await playwright.chromium.launch(headless=True)
            await probe.close()
    except Exception as exc:  # noqa: BLE001 - 仅探测真实 Chromium 是否可用
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    class HolderTrackingBrowser(PlaywrightPageCaptureBrowser):
        def __init__(self) -> None:
            super().__init__(h5_visual_coordinate_mode="enforced")
            self.holder_chain: list[object] = []
            self.tracking = False

        async def capture_with_timeout(
            self,
            browser_request: PageCaptureRequest,
            *,
            timeout_seconds: float,
        ) -> PageCaptureResult:
            self.holder_chain = [browser_request._visual_action_commit_holder]
            self.tracking = True
            try:
                return await super().capture_with_timeout(
                    browser_request,
                    timeout_seconds=timeout_seconds,
                )
            finally:
                self.tracking = False

        async def _capture_on_page(self, **kwargs: object) -> PageCaptureResult:
            browser_request = kwargs["request"]
            assert isinstance(browser_request, PageCaptureRequest)
            if self.tracking:
                self.holder_chain.append(browser_request._visual_action_commit_holder)
            return await super()._capture_on_page(**kwargs)  # type: ignore[arg-type]

    browser = HolderTrackingBrowser()
    original_validate = page_capture_module._validate_url_mode_contract

    def record_innermost_clone(**kwargs: object) -> None:
        browser_request = kwargs["request"]
        assert isinstance(browser_request, PageCaptureRequest)
        if browser.tracking:
            browser.holder_chain.append(browser_request._visual_action_commit_holder)
        original_validate(**kwargs)  # type: ignore[arg-type]

    cleanup_started = asyncio.Event()

    async def stall_postclick_cleanup(tracker: object) -> None:
        del tracker
        cleanup_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        page_capture_module,
        "_validate_url_mode_contract",
        record_innermost_clone,
    )
    monkeypatch.setattr(
        page_capture_module,
        "_close_visual_navigation_cdp_tracker",
        stall_postclick_cleanup,
    )
    html = """
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      html, body { margin: 0; width: 240px; height: 320px; overflow: hidden; }
      #visual-target {
        position: absolute; left: 24px; top: 64px; width: 144px; height: 96px;
        border: 0;
        background: repeating-conic-gradient(#1273de 0 12deg, #f7d154 12deg 24deg);
      }
    </style>
    <button id="visual-target" onclick="window.__aliothClickCount += 1"></button>
    <script>window.__aliothClickCount = 0;</script>
    """
    session_id = "session-1"
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="data:text/html;charset=utf-8," + quote(html),
                session_id=session_id,
                viewport_width=240,
                viewport_height=320,
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=False,
            )
        )
        source_png = await browser._sessions[session_id].page.screenshot(
            full_page=False,
            type="png",
        )
        decoded = cv2.imdecode(
            np.frombuffer(source_png, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        assert decoded is not None
        image_height, image_width = decoded.shape[:2]
        rgba = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGBA)
        capability_payload = _visual_navigation_capability(
            source_png,
            rgba.tobytes(),
            image_width,
            image_height,
        )
        runtime_target = VisualNavigationRuntimeTarget(
            capability=verify_visual_navigation_capability(capability_payload),
            source_png=source_png,
            source_image_sha256=capability_payload["source_artifact_sha256"],
        )
        buffer = InMemoryActionEvidenceBuffer()
        executor = BrowserInteractionExecutor(
            browser=browser,
            target_resolver=RecordingResolver(
                ResolvedInteractionTarget(visual_navigation_target=runtime_target)
            ),
            evidence_buffer=buffer,
            action_timeout_seconds=1.0,
        )
        execute_request = ExecuteInteractionRequest(
            intent=_intent(
                parameters={
                    "navigation_depth": 1,
                    "viewport_width": 240,
                    "viewport_height": 320,
                }
            ),
            agent_run_id="run-real-visual-timeout",
            source_capture_id="capture-before",
            action_execution_id="real-visual-action-once",
        )

        first = await executor.execute(execute_request)
        second = await executor.execute(execute_request)

        assert cleanup_started.is_set() is True
        assert len(browser.holder_chain) == 3
        assert browser.holder_chain[0] is browser.holder_chain[1]
        assert browser.holder_chain[1] is browser.holder_chain[2]
        assert first.status is InteractionResultStatus.FAILED
        assert first.error_code == "VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED"
        assert first.retryable is False
        assert second == first
        assert (
            await browser._sessions[session_id].page.evaluate("() => window.__aliothClickCount")
            == 1
        )
        assert await buffer.get(action_execution_id="real-visual-action-once") is not None
    finally:
        await browser.close_session(session_id)


@pytest.mark.parametrize(
    "failure_stage",
    ("settle", "undeclared_navigation", "after_screenshot", "transition"),
)
async def test_real_visual_postclick_failures_are_idempotent_and_clear_private_frame(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    """点击后的任一采集异常都必须落幂等证据并清空实时前帧。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    try:
        async with async_playwright() as playwright:
            probe = await playwright.chromium.launch(headless=True)
            await probe.close()
    except Exception as exc:  # noqa: BLE001 - 仅探测真实 Chromium 是否可用
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    class RequestTrackingBrowser(PlaywrightPageCaptureBrowser):
        def __init__(self) -> None:
            super().__init__(h5_visual_coordinate_mode="enforced")
            self.action_requests: list[PageCaptureRequest] = []

        async def capture_with_timeout(
            self,
            browser_request: PageCaptureRequest,
            *,
            timeout_seconds: float,
        ) -> PageCaptureResult:
            if browser_request.mode is PageCaptureMode.SINGLE_TRIGGER:
                self.action_requests.append(browser_request)
            return await super().capture_with_timeout(
                browser_request,
                timeout_seconds=timeout_seconds,
            )

    browser = RequestTrackingBrowser()
    html = """
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      html, body { margin: 0; width: 240px; height: 320px; overflow: hidden; }
      #visual-target {
        position: absolute; left: 24px; top: 64px; width: 144px; height: 96px;
        border: 0;
        background: repeating-conic-gradient(#1273de 0 12deg, #f7d154 12deg 24deg);
      }
    </style>
    <button id="visual-target" onclick="window.__aliothClickCount += 1"></button>
    <script>window.__aliothClickCount = 0;</script>
    """
    session_id = "session-1"
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="data:text/html;charset=utf-8," + quote(html),
                session_id=session_id,
                viewport_width=240,
                viewport_height=320,
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=False,
            )
        )
        page = browser._sessions[session_id].page
        source_png = await page.screenshot(full_page=False, type="png")
        decoded = cv2.imdecode(
            np.frombuffer(source_png, dtype=np.uint8),
            cv2.IMREAD_COLOR,
        )
        assert decoded is not None
        image_height, image_width = decoded.shape[:2]
        rgba = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGBA)
        capability_payload = _visual_navigation_capability(
            source_png,
            rgba.tobytes(),
            image_width,
            image_height,
        )
        runtime_target = VisualNavigationRuntimeTarget(
            capability=verify_visual_navigation_capability(capability_payload),
            source_png=source_png,
            source_image_sha256=capability_payload["source_artifact_sha256"],
        )

        async def fail_stage(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError(f"postclick-{failure_stage}-private-detail")

        if failure_stage == "settle":
            monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", fail_stage)
        elif failure_stage == "undeclared_navigation":
            monkeypatch.setattr(
                page_capture_module,
                "_block_undeclared_click_navigation",
                fail_stage,
            )
        elif failure_stage == "after_screenshot":
            monkeypatch.setattr(
                page_capture_module,
                "_capture_visual_navigation_after_frames",
                fail_stage,
            )
        elif failure_stage == "transition":

            def fail_transition(*args: object, **kwargs: object) -> dict[str, object]:
                del args, kwargs
                raise RuntimeError("postclick-transition-private-detail")

            monkeypatch.setattr(
                page_capture_module,
                "assess_visual_navigation_transition",
                fail_transition,
            )
        else:
            raise AssertionError(f"未处理的失败阶段：{failure_stage}")

        buffer = InMemoryActionEvidenceBuffer()
        executor = BrowserInteractionExecutor(
            browser=browser,
            target_resolver=RecordingResolver(
                ResolvedInteractionTarget(visual_navigation_target=runtime_target)
            ),
            evidence_buffer=buffer,
            action_timeout_seconds=5.0,
        )
        execute_request = ExecuteInteractionRequest(
            intent=_intent(
                parameters={
                    "navigation_depth": 1,
                    "viewport_width": 240,
                    "viewport_height": 320,
                }
            ),
            agent_run_id=f"run-postclick-{failure_stage}",
            source_capture_id="capture-before",
            action_execution_id=f"visual-postclick-{failure_stage}",
        )

        first = await executor.execute(execute_request)
        second = await executor.execute(execute_request)

        assert first.status is InteractionResultStatus.FAILED
        assert first.error_code == "VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED"
        assert first.retryable is False
        assert first.state_diff == {"visual_action_committed": True}
        assert second == first
        assert len(browser.action_requests) == 1
        assert await page.evaluate("() => window.__aliothClickCount") == 1
        assert (
            browser.action_requests[0]._visual_navigation_frame_context.verified_before_frame
            is None
        )
        assert await buffer.get(action_execution_id=f"visual-postclick-{failure_stage}") is not None
        assert "private-detail" not in repr(first)
    finally:
        await browser.close_session(session_id)


async def test_uncommitted_outer_browser_deadline_remains_retryable_timeout() -> None:
    """浏览器在 click 前阻塞时，外层 deadline 仍返回原 ACTION_TIMED_OUT。"""

    class PreclickStalledBrowser:
        def __init__(self) -> None:
            self.requests: list[PageCaptureRequest] = []

        async def capture_with_timeout(
            self,
            browser_request: PageCaptureRequest,
            *,
            timeout_seconds: float,
        ) -> PageCaptureResult:
            del timeout_seconds
            self.requests.append(browser_request)
            await asyncio.Event().wait()
            raise AssertionError("超时后不可达")

    browser = PreclickStalledBrowser()
    executor = BrowserInteractionExecutor(
        browser=browser,
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#next")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
        action_timeout_seconds=0.01,
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            intent=_intent(),
            agent_run_id="run-preclick-timeout",
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.TIMED_OUT
    assert result.error_code == "ACTION_TIMED_OUT"
    assert result.retryable is True
    assert len(browser.requests) == 1


@pytest.mark.parametrize("current_mode", ["off", "shadow"])
async def test_visual_navigation_resolver_respects_runtime_kill_switch(
    current_mode: str,
) -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    reader = StaticSnapshotReader(
        _visual_navigation_snapshot(png),
        visual_png=png,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await UiSnapshotSemanticTargetResolver(
            snapshot_reader=reader,
            logical_targets_enabled=True,
            h5_visual_coordinate_mode=current_mode,
        ).resolve(_visual_navigation_resolution_request(capability))

    assert error.value.error_code == "VISUAL_TARGET_NOT_ALLOWLISTED"
    assert reader.calls == []
    assert reader.visual_image_calls == []


@pytest.mark.parametrize("tampered_target_mode", [None, "bottom_navigation"])
async def test_visual_navigation_resolver_never_downgrades_capability_to_legacy_text(
    tampered_target_mode: str | None,
) -> None:
    """视觉能力字段存在但模式标记被篡改时，Resolver 必须在读 Snapshot 前拒绝。"""
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    reader = StaticSnapshotReader(
        _visual_navigation_snapshot(png),
        visual_png=png,
    )
    request = _visual_navigation_resolution_request(capability)
    parameters = dict(request.parameters)
    if tampered_target_mode is None:
        parameters.pop("target_mode")
    else:
        parameters["target_mode"] = tampered_target_mode
    request = replace(request, parameters=parameters)

    with pytest.raises(SemanticTargetResolutionError) as error:
        await _visual_navigation_resolver(reader).resolve(request)

    assert error.value.error_code == "VISUAL_TARGET_INVALID"
    assert reader.calls == []
    assert reader.visual_image_calls == []


async def test_visual_navigation_resolver_accepts_canonical_source_page_confidence() -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    source_page_type_confidence = 0.92345
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
        source_page_type_confidence=source_page_type_confidence,
    )
    reader = StaticSnapshotReader(
        _visual_navigation_snapshot(
            png,
            page_type_confidence=source_page_type_confidence,
        ),
        visual_png=png,
    )

    target = await _visual_navigation_resolver(reader).resolve(
        _visual_navigation_resolution_request(capability)
    )

    assert target.visual_navigation_target is not None


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("wrong_task", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_capture", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_artifact", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_sha", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_identity", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_state", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("coverage_sufficient", "VISUAL_TARGET_NOT_ALLOWLISTED"),
        ("render_unconfirmed", "VISUAL_TARGET_NOT_ALLOWLISTED"),
        ("wrong_page_type", "VISUAL_TARGET_NOT_ALLOWLISTED"),
        ("wrong_viewport", "VISUAL_TARGET_COORDINATE_INVALID"),
        ("wrong_transform", "VISUAL_TARGET_COORDINATE_INVALID"),
        ("policy_bypass", "VISUAL_TARGET_NOT_ALLOWLISTED"),
    ],
)
async def test_visual_navigation_resolver_fails_closed(
    mutation: str,
    error_code: str,
) -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    if mutation == "wrong_task":
        capability = _visual_navigation_capability(
            png,
            rgba_bytes,
            image_width,
            image_height,
            source_task_id="task-other",
        )
    elif mutation == "wrong_capture":
        capability = _visual_navigation_capability(
            png,
            rgba_bytes,
            image_width,
            image_height,
            source_capture_id="capture-other",
        )
    elif mutation == "wrong_artifact":
        capability = _visual_navigation_capability(
            png,
            rgba_bytes,
            image_width,
            image_height,
            source_artifact_id="artifact-other",
        )
    elif mutation == "wrong_sha":
        capability = _visual_navigation_capability(
            png,
            rgba_bytes,
            image_width,
            image_height,
            source_artifact_sha256="sha256:" + "f" * 64,
        )
    elif mutation == "wrong_identity":
        capability = _visual_navigation_capability(
            png,
            rgba_bytes,
            image_width,
            image_height,
            source_identity_fingerprint="sha256:" + "8" * 64,
        )
    elif mutation == "wrong_state":
        capability = _visual_navigation_capability(
            png,
            rgba_bytes,
            image_width,
            image_height,
            source_state_fingerprint="sha256:" + "9" * 64,
        )
    elif mutation == "coverage_sufficient":
        capability = _resign_visual_navigation_capability(
            capability,
            semantics_coverage_level="semantics_sufficient",
        )
    elif mutation == "render_unconfirmed":
        capability = _resign_visual_navigation_capability(
            capability,
            render_profile={"kind": "flutter_web", "status": "inferred"},
        )
    elif mutation == "wrong_page_type":
        capability = _resign_visual_navigation_capability(
            capability,
            source_page_type="profile",
        )
    elif mutation == "wrong_viewport":
        capability = _resign_visual_navigation_capability(
            capability,
            viewport_metrics={
                **capability["viewport_metrics"],
                "width": image_width + 1,
            },
        )
    elif mutation == "wrong_transform":
        capability = _resign_visual_navigation_capability(
            capability,
            model_image_transform={
                "kind": "identity",
                "scale_x": 0.5,
                "scale_y": 1.0,
                "offset_x": 0.0,
                "offset_y": 0.0,
            },
        )
    elif mutation == "policy_bypass":
        capability = _resign_visual_navigation_capability(
            capability,
            risk_flags=["external"],
        )
    else:
        raise AssertionError(f"未处理的测试变体：{mutation}")
    reader = StaticSnapshotReader(
        _visual_navigation_snapshot(png),
        visual_png=png,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await _visual_navigation_resolver(reader).resolve(
            _visual_navigation_resolution_request(capability)
        )

    assert error.value.error_code == error_code
    if mutation in {
        "wrong_task",
        "wrong_capture",
        "wrong_identity",
        "wrong_state",
        "coverage_sufficient",
        "render_unconfirmed",
    }:
        assert reader.visual_image_calls == []


async def test_visual_navigation_resolver_rejects_tampered_signature_before_snapshot() -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    capability["expected_page_type"] = "video_detail"
    reader = StaticSnapshotReader(
        _visual_navigation_snapshot(png),
        visual_png=png,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await _visual_navigation_resolver(reader).resolve(
            _visual_navigation_resolution_request(capability)
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert reader.calls == []
    assert reader.visual_image_calls == []


@pytest.mark.parametrize(
    "reader_error_code",
    ["UI_SNAPSHOT_MISSING", "UI_SNAPSHOT_OWNERSHIP_MISMATCH"],
)
async def test_visual_navigation_resolver_maps_snapshot_reader_failure(
    reader_error_code: str,
) -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )

    class FailingSnapshotReader(StaticSnapshotReader):
        async def read_ui_snapshot(self, *, task_id, page_capture_id):
            self.calls.append((task_id, page_capture_id))
            raise SemanticTargetResolutionError(
                "来源 Snapshot 不可用",
                error_code=reader_error_code,
            )

    reader = FailingSnapshotReader(
        _visual_navigation_snapshot(png),
        visual_png=png,
    )
    with pytest.raises(SemanticTargetResolutionError) as error:
        await _visual_navigation_resolver(reader).resolve(
            _visual_navigation_resolution_request(capability)
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert reader.visual_image_calls == []


async def test_visual_navigation_resolver_rejects_rehashed_noncanonical_signature_early() -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    capability = _resign_visual_navigation_capability(
        capability,
        visual_target_confidence=0.93000001,
    )
    reader = StaticSnapshotReader(
        _visual_navigation_snapshot(png),
        visual_png=png,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await _visual_navigation_resolver(reader).resolve(
            _visual_navigation_resolution_request(capability)
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert reader.calls == []
    assert reader.visual_image_calls == []


@pytest.mark.parametrize(
    ("mutation", "error_code"),
    [
        ("malformed_digest", "VISUAL_TARGET_COORDINATE_INVALID"),
        ("wrong_digest", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_anchor_digest", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_region", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("wrong_image_size", "VISUAL_TARGET_COORDINATE_INVALID"),
        ("wrong_coordinate_space", "VISUAL_TARGET_COORDINATE_INVALID"),
        ("wrong_capture_sha", "VISUAL_TARGET_SOURCE_STATE_STALE"),
    ],
)
async def test_visual_navigation_resolver_validates_source_png_region_material(
    mutation: str,
    error_code: str,
) -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    snapshot = _visual_navigation_snapshot(png)
    if mutation == "malformed_digest":
        capability = _resign_visual_navigation_capability(
            capability,
            local_visual_digest="not-a-digest",
        )
    elif mutation == "wrong_digest":
        capability = _resign_visual_navigation_capability(
            capability,
            local_visual_digest="sha256:" + "f" * 64,
        )
    elif mutation == "wrong_anchor_digest":
        capability = _resign_visual_navigation_capability(
            capability,
            surrounding_anchor_digest="sha256:" + "f" * 64,
        )
    elif mutation == "wrong_region":
        capability = _resign_visual_navigation_capability(
            capability,
            normalized_rect={"x": 0.15, "y": 0.2, "width": 0.6, "height": 0.3},
        )
    elif mutation == "wrong_image_size":
        capability = _resign_visual_navigation_capability(
            capability,
            source_image_size={"width": image_width + 1, "height": image_height},
        )
    elif mutation == "wrong_coordinate_space":
        visual_capture = snapshot["visual_navigation_capture"]
        assert isinstance(visual_capture, dict)
        visual_capture["coordinate_space"] = "document_css_pixels"
    elif mutation == "wrong_capture_sha":
        visual_capture = snapshot["visual_navigation_capture"]
        assert isinstance(visual_capture, dict)
        visual_capture["screenshot_sha256"] = "sha256:" + "e" * 64
    else:
        raise AssertionError(f"未处理的测试变体：{mutation}")
    reader = StaticSnapshotReader(snapshot, visual_png=png)

    with pytest.raises(SemanticTargetResolutionError) as error:
        await _visual_navigation_resolver(reader).resolve(
            _visual_navigation_resolution_request(capability)
        )

    assert error.value.error_code == error_code


async def test_visual_navigation_resolver_never_falls_back_to_text_target() -> None:
    png, rgba_bytes, image_width, image_height = _visual_navigation_png()
    capability = _visual_navigation_capability(
        png,
        rgba_bytes,
        image_width,
        image_height,
    )
    capability["target_signature"] = "sha256:" + "0" * 64
    snapshot = _visual_navigation_snapshot(png)
    elements = snapshot["elements"]
    assert isinstance(elements, list)
    elements.append(
        {
            "selector": "#unsafe-text-fallback",
            "tag": "button",
            "role": "button",
            "text": "进入 content_detail 页面",
            "interactive": True,
        }
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await _visual_navigation_resolver(StaticSnapshotReader(snapshot, visual_png=png)).resolve(
            _visual_navigation_resolution_request(capability)
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"


async def test_logical_target_rejects_stale_source_state_before_action() -> None:
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(_logical_target_snapshot()),
        logical_targets_enabled=True,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="我的",
                parameters={"logical_target": _logical_target_contract(state="sha256:" + "3" * 64)},
            )
        )

    assert error.value.error_code == "LOGICAL_SOURCE_STATE_STALE"
    assert error.value.retryable is True


@pytest.mark.parametrize(
    ("capability", "elements", "expected_rect"),
    [
        (
            "dialog_overlay",
            [
                {
                    "selector": (
                        "#appView > div > div:nth-of-type(1) > div > div:nth-of-type(4) > div"
                    ),
                    "tag": "div",
                    "class_tokens": ["van-overlay"],
                    "ancestor_class_tokens": ["persalePopup", "home"],
                    "interactive": False,
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "hit_test": {"center_hits_target": True},
                }
            ],
            {"x": 0.0, "y": 0.0, "width": 390.0, "height": 844.0},
        ),
        (
            "overlay_ad_close",
            [
                {
                    "selector": (
                        "#appView > div > div:nth-of-type(1) > div > "
                        "div:nth-of-type(4) > div > div > div:nth-of-type(2)"
                    ),
                    "tag": "div",
                    "class_tokens": ["close"],
                    "ancestor_class_tokens": [
                        "wrapper",
                        "van-overlay",
                        "nineGridPopup",
                        "home",
                    ],
                    "interactive": False,
                    "rect": {
                        "x": 178.36,
                        "y": 649.77,
                        "width": 33.27,
                        "height": 33.27,
                    },
                    "hit_test": {"center_hits_target": True},
                }
            ],
            {"x": 178.36, "y": 649.77, "width": 33.27, "height": 33.27},
        ),
    ],
    ids=["persale-overlay", "nine-grid-close"],
)
async def test_logical_popup_dismissal_binds_registered_non_native_close_structure(
    capability: str,
    elements: list[dict[str, object]],
    expected_rect: dict[str, float],
) -> None:
    """真实弹窗关闭层是可命中的 div，逻辑绑定不得因非原生控件再次否决。"""
    snapshot = _logical_target_snapshot()
    snapshot["elements"] = elements
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": capability,
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
                "logical_target": _logical_target_contract(expects_navigation=False),
            },
        )
    )

    assert target.expected_rect == expected_rect
    assert target.target_trust == "trusted"


async def test_logical_dialog_generic_close_binds_wrapper_instead_of_dialog_body() -> None:
    """close_btn 与子图标近似同框时必须绑定包装层，不能签名整个 Dialog。"""
    snapshot = _logical_target_snapshot()
    snapshot["elements"] = [
        {
            "selector": "#modal > .van-popup",
            "tag": "div",
            "role": "dialog",
            "class_tokens": ["van-popup", "van-popup--center", "app-list-pop"],
            "ancestor_class_tokens": ["vue-nice-modal-root"],
            "interactive": True,
            "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
            "hit_test": {"center_hits_target": True},
        },
        {
            "selector": "#modal > .van-popup > ul",
            "tag": "ul",
            "class_tokens": [],
            "ancestor_class_tokens": ["van-popup", "van-popup--center"],
            "interactive": False,
            "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
            "hit_test": {"center_hits_target": True},
        },
        {
            "selector": "#modal > .van-popup > .close_btn",
            "tag": "div",
            "class_tokens": ["close_btn"],
            "ancestor_class_tokens": ["van-popup", "van-popup--center", "app-list-pop"],
            "interactive": False,
            "rect": {"x": 175, "y": 619, "width": 40, "height": 40.72},
            "hit_test": {"center_hits_target": True},
        },
        {
            "selector": "#modal > .van-popup > .close_btn > i",
            "tag": "i",
            "class_tokens": ["van-icon", "van-icon-close"],
            "ancestor_class_tokens": [
                "close_btn",
                "van-popup",
                "van-popup--center",
                "app-list-pop",
            ],
            "interactive": False,
            "rect": {"x": 175, "y": 619, "width": 40, "height": 40},
            "hit_test": {"center_hits_target": True},
        },
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_generic_close",
                "obstruction_signature": "h5-obstruction:" + "b" * 64,
                "logical_target": _logical_target_contract(expects_navigation=False),
            },
        )
    )

    assert target.expected_rect == {
        "x": 175.0,
        "y": 619.0,
        "width": 40.0,
        "height": 40.72,
    }
    assert target.target_trust == "trusted"


async def test_logical_startup_ad_allows_state_drift_after_structural_revalidation() -> None:
    """倒计时只改变状态指纹时，唯一受信跳过结构仍应允许动作前重签。"""
    snapshot = _logical_target_snapshot(state="sha256:" + "4" * 64)
    snapshot["elements"] = [
        {
            "selector": "#app > .launch-page > .jump_over",
            "tag": "div",
            "text": "跳过",
            "class_tokens": ["jump_over"],
            "ancestor_class_tokens": ["countdown", "wrapper"],
            "interactive": False,
            "rect": {"x": 320, "y": 30, "width": 50, "height": 32},
            "hit_test": {"center_hits_target": True},
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-startup",
            action_type=InteractionOperation.CLICK,
            semantic_target="跳过",
            parameters={
                "target_mode": "startup_ad_dismissal",
                "startup_ad_capability": "countdown_close",
                "navigation_depth": 0,
                "logical_target": _logical_target_contract(
                    state="sha256:" + "3" * 64,
                    expects_navigation=False,
                ),
            },
        )
    )

    assert target.expected_rect == {
        "x": 320.0,
        "y": 30.0,
        "width": 50.0,
        "height": 32.0,
    }
    assert target.source_state_fingerprint == "sha256:" + "4" * 64


async def test_logical_popup_allows_state_drift_after_structural_revalidation() -> None:
    """倒计时刷新不得让同一页面唯一受信弹窗关闭结构持续过期。"""
    snapshot = _logical_target_snapshot(state="sha256:" + "4" * 64)
    snapshot["elements"] = [
        {
            "selector": "#home > .persalePopup > .van-overlay",
            "tag": "div",
            "class_tokens": ["van-overlay"],
            "ancestor_class_tokens": ["persalePopup", "home"],
            "interactive": False,
            "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
            "hit_test": {"center_hits_target": True},
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_overlay",
                "obstruction_signature": "h5-obstruction:" + "a" * 64,
                "logical_target": _logical_target_contract(
                    state="sha256:" + "3" * 64,
                    expects_navigation=True,
                ),
            },
        )
    )

    assert target.target_trust == "trusted"
    assert target.source_state_fingerprint == "sha256:" + "4" * 64


def test_popup_runtime_binding_uses_revalidated_live_state() -> None:
    """浏览器最终校验必须绑定复验后的当前状态，而不是旧倒计时状态。"""
    target = ResolvedInteractionTarget(
        selector=".van-overlay:visible",
        target_trust="trusted",
        target_signature="sha256:" + "5" * 64,
        target_identity_signature="sha256:" + "7" * 64,
        source_state_fingerprint="sha256:" + "4" * 64,
        h5_popup_dismissal_strategy="safe_outside_then_close",
    )

    binding = _logical_target_runtime_binding(
        parameters={
            "target_mode": "popup_dismissal",
            "popup_dismissal_capability": "dialog_overlay",
            "obstruction_signature": "h5-obstruction:" + "a" * 64,
            "logical_target": _logical_target_contract(
                state="sha256:" + "3" * 64,
                expects_navigation=True,
            ),
        },
        target=target,
        operation=InteractionOperation.CLICK,
    )

    assert binding is not None
    assert binding.source_state_fingerprint == "sha256:" + "4" * 64


def test_h5_list_detail_binding_allows_final_structural_revalidation() -> None:
    """H5 动态列表仅在任务 Frontier 与唯一目标签名齐全时允许最终复验。"""
    target = ResolvedInteractionTarget(
        selector=".videoTitle:visible",
        target_trust="trusted",
        target_signature="sha256:" + "5" * 64,
        target_identity_signature="sha256:" + "7" * 64,
        source_state_fingerprint="sha256:" + "3" * 64,
        target_source="dom",
    )

    binding = _logical_target_runtime_binding(
        parameters={
            "target_mode": "list_detail_navigation",
            "frontier_snapshot_id": "h5-task-frontier:" + "6" * 64,
            "logical_target": _logical_target_contract(
                state="sha256:" + "3" * 64,
                expects_navigation=True,
            ),
        },
        target=target,
        operation=InteractionOperation.CLICK,
    )

    assert binding is not None
    assert binding.allow_dynamic_state_revalidation is True
    assert binding.allow_dynamic_geometry_revalidation is True
    assert binding.allow_horizontal_offscreen_revalidation is False


def test_h5_page_tab_binding_allows_horizontal_offscreen_revalidation() -> None:
    """横向离屏能力只随 H5 页签导航绑定下发。"""
    target = ResolvedInteractionTarget(
        selector="#van-tabs-1-1",
        target_trust="trusted",
        target_signature="sha256:" + "5" * 64,
        target_identity_signature="sha256:" + "7" * 64,
        source_state_fingerprint="sha256:" + "3" * 64,
        target_source="dom",
    )

    binding = _logical_target_runtime_binding(
        parameters={
            "target_mode": "page_tab_navigation",
            "frontier_snapshot_id": "h5-task-frontier:" + "6" * 64,
            "logical_target": _logical_target_contract(
                state="sha256:" + "3" * 64,
                expects_navigation=True,
            ),
        },
        target=target,
        operation=InteractionOperation.CLICK,
    )

    assert binding is not None
    assert binding.allow_dynamic_state_revalidation is True
    assert binding.allow_dynamic_geometry_revalidation is True
    assert binding.allow_horizontal_offscreen_revalidation is True


def test_non_h5_list_detail_binding_keeps_strict_state_validation() -> None:
    """缺少 H5 Frontier 证明的同名模式不得放宽 Web 或其他调用方。"""
    target = ResolvedInteractionTarget(
        selector=".videoTitle:visible",
        target_trust="trusted",
        target_signature="sha256:" + "5" * 64,
        target_identity_signature="sha256:" + "7" * 64,
        source_state_fingerprint="sha256:" + "3" * 64,
        target_source="dom",
    )

    binding = _logical_target_runtime_binding(
        parameters={
            "target_mode": "list_detail_navigation",
            "logical_target": _logical_target_contract(
                state="sha256:" + "3" * 64,
                expects_navigation=True,
            ),
        },
        target=target,
        operation=InteractionOperation.CLICK,
    )

    assert binding is not None
    assert binding.allow_dynamic_state_revalidation is False
    assert binding.allow_horizontal_offscreen_revalidation is False


def test_popup_clearance_annotation_accepts_revalidated_dynamic_source() -> None:
    """最终动作已复验同一结构时，动态状态漂移不应否定真实遮挡退场。"""
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom",
        allow_dynamic_state_revalidation=True,
    )
    entry = {
        "dismissal_verified": True,
        "popup_dismissal_outcome": "original_layer_absent",
        "logical_target": {
            "target_trust": "trusted",
            "target_signature": binding.target_signature,
            "source_state_fingerprint": binding.source_state_fingerprint,
        },
        "state_diff": {
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "logical_transition_proof": "trusted",
                "transition_kind": "surface_state_changed",
                "corroborating_signals": ["surface_state"],
                "before_identity_fingerprint": binding.source_identity_fingerprint,
                "after_identity_fingerprint": binding.source_identity_fingerprint,
                "before_state_fingerprint": "sha256:" + "4" * 64,
                "after_state_fingerprint": "sha256:" + "5" * 64,
            }
        },
    }

    _annotate_popup_clearance_trace_entry(entry, runtime_binding=binding)

    assert (
        entry["state_diff"]["logical_surface_transition"]["reason_code"]
        == "trusted_obstruction_clearance"
    )


def test_popup_clearance_annotation_records_h5_next_popup_layer() -> None:
    """H5 原弹层退场并露出下一层时，保留进展并继续阻断业务探索。"""
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom",
        allow_dynamic_state_revalidation=True,
    )
    entry = {
        "dismissal_verified": True,
        "popup_dismissal_outcome": "next_popup_layer_visible",
        "logical_target": {
            "target_trust": "trusted",
            "target_signature": binding.target_signature,
            "source_state_fingerprint": binding.source_state_fingerprint,
        },
        "state_diff": {
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "logical_transition_proof": "trusted",
                "transition_kind": "surface_state_changed",
                "corroborating_signals": ["surface_state"],
                "before_identity_fingerprint": binding.source_identity_fingerprint,
                "after_identity_fingerprint": binding.source_identity_fingerprint,
                "before_state_fingerprint": "sha256:" + "4" * 64,
                "after_state_fingerprint": "sha256:" + "5" * 64,
            }
        },
    }

    _annotate_popup_clearance_trace_entry(entry, runtime_binding=binding)

    transition = entry["state_diff"]["logical_surface_transition"]
    assert transition["reason_code"] == "trusted_obstruction_layer_advanced"
    assert (
        _logical_navigation_postcondition_error(
            parameters={
                "target_mode": "popup_dismissal",
                "logical_target": {"expects_logical_navigation": True},
            },
            state_diff=entry["state_diff"],
        )
        == "H5_POPUP_CHAIN_REMAINS_BLOCKED"
    )


def test_popup_clearance_annotation_prefers_final_cleared_snapshot() -> None:
    """多层弹窗最终已清空时，不得被最后一次点击的瞬时层状态误判为仍阻塞。"""
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom",
        allow_dynamic_state_revalidation=True,
    )
    entry = {
        "dismissal_verified": True,
        "popup_dismissal_outcome": "next_popup_layer_visible",
        "logical_target": {
            "target_trust": "trusted",
            "target_signature": binding.target_signature,
            "source_state_fingerprint": binding.source_state_fingerprint,
        },
        "state_diff": {
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "logical_transition_proof": "trusted",
                "transition_kind": "surface_state_changed",
                "corroborating_signals": ["surface_state"],
                "before_identity_fingerprint": binding.source_identity_fingerprint,
                "after_identity_fingerprint": binding.source_identity_fingerprint,
                "before_state_fingerprint": "sha256:" + "4" * 64,
                "after_state_fingerprint": "sha256:" + "5" * 64,
            }
        },
    }

    _annotate_popup_clearance_trace_entry(
        entry,
        runtime_binding=binding,
        final_obstruction_cleared=True,
    )

    transition = entry["state_diff"]["logical_surface_transition"]
    assert transition["reason_code"] == "trusted_obstruction_clearance"
    assert (
        _logical_navigation_postcondition_error(
            parameters={
                "target_mode": "popup_dismissal",
                "logical_target": {"expects_logical_navigation": True},
            },
            state_diff=entry["state_diff"],
        )
        is None
    )


def test_popup_clearance_reads_final_trusted_ui_snapshot() -> None:
    result = _page_result(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        extra_artifacts=[
            PageCaptureArtifact(
                "ui_snapshot",
                None,
                {
                    "logical_surface": {
                        "state_signals": {
                            "obstruction_count": "0",
                            "obstructions": [],
                        }
                    }
                },
            )
        ],
    )

    assert _result_final_obstruction_cleared(result) is True


def test_page_tab_navigation_accepts_trusted_same_page_state_change() -> None:
    """同页业务筛选的确定性激活证明应满足 H5 页签后置条件。"""
    assert (
        _logical_navigation_postcondition_error(
            parameters={
                "target_mode": "page_tab_navigation",
                "logical_target": {"expects_logical_navigation": True},
            },
            state_diff={
                "logical_surface_transition": {
                    "protocol_version": "logical_surface_transition.v1",
                    "logical_transition_proof": "trusted",
                    "transition_kind": "surface_state_changed",
                    "reason_code": "trusted_page_tab_state_change",
                    "corroborating_signals": [
                        "surface_state",
                        "page_tab_activation",
                    ],
                }
            },
        )
        is None
    )


def test_popup_clearance_annotation_rejects_non_h5_next_popup_layer() -> None:
    """非 H5 动态复验调用方不得把下一层弹窗当作可信进展。"""
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
        target_source="dom",
        allow_dynamic_state_revalidation=False,
    )
    entry = {
        "dismissal_verified": True,
        "popup_dismissal_outcome": "next_popup_layer_visible",
        "logical_target": {
            "target_trust": "trusted",
            "target_signature": binding.target_signature,
            "source_state_fingerprint": binding.source_state_fingerprint,
        },
        "state_diff": {
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "logical_transition_proof": "trusted",
                "transition_kind": "surface_state_changed",
                "corroborating_signals": ["surface_state"],
                "before_identity_fingerprint": binding.source_identity_fingerprint,
                "after_identity_fingerprint": binding.source_identity_fingerprint,
                "before_state_fingerprint": binding.source_state_fingerprint,
                "after_state_fingerprint": "sha256:" + "5" * 64,
            }
        },
    }

    _annotate_popup_clearance_trace_entry(entry, runtime_binding=binding)

    assert "reason_code" not in entry["state_diff"]["logical_surface_transition"]


async def test_logical_list_detail_binds_registered_non_native_title() -> None:
    """签发层已验真的视频标题 div 不得在逻辑绑定层因非原生控件再次否决。"""
    snapshot = _logical_target_snapshot()
    snapshot["viewport"] = {"width": 390, "height": 844}
    snapshot["elements"] = [
        {
            "selector": "#home-video > .item:nth-of-type(1) .videoTitle",
            "tag": "div",
            "text": "首个当前可见视频标题",
            "class_tokens": ["videoTitle"],
            "ancestor_class_tokens": [
                "videoTitleBox",
                "horizontalVideoBox",
                "videoBox",
                "moreMoviesBox",
                "van-list",
                "item",
            ],
            "interactive": False,
            "rect": {"x": 10.39, "y": 515.73, "width": 180.95, "height": 36.39},
            "hit_test": {"center_hits_target": True},
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "list_detail_selector": ".videoTitle:visible",
                "source_page_type": "home",
                "expected_page_type": "video_detail",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == "#home-video > .item:nth-of-type(1) .videoTitle"
    assert target.target_trust == "trusted"


async def test_logical_h5_list_detail_rebinds_current_target_after_state_drift() -> None:
    """动态列表变化后应按当前快照重选唯一卡片，不能先被旧状态指纹拦截。"""
    snapshot = _logical_target_snapshot(state="sha256:" + "4" * 64)
    snapshot["viewport"] = {"width": 390, "height": 844}
    snapshot["elements"] = [
        {
            "selector": "#home-video > .item:nth-of-type(1) .videoTitle",
            "tag": "div",
            "text": "当前刷新后的首个视频标题",
            "class_tokens": ["videoTitle"],
            "ancestor_class_tokens": [
                "videoTitleBox",
                "horizontalVideoBox",
                "videoBox",
                "moreMoviesBox",
                "van-list",
                "item",
            ],
            "interactive": False,
            "rect": {"x": 10.39, "y": 453.33, "width": 180.95, "height": 36.39},
            "hit_test": {"center_hits_target": True},
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target="当前列表首个内容卡片",
            parameters={
                "target_mode": "list_detail_navigation",
                "frontier_snapshot_id": "h5-task-frontier:" + "6" * 64,
                "list_detail_selector": ".videoTitle:visible",
                "source_page_type": "home",
                "expected_page_type": "video_detail",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == "#home-video > .item:nth-of-type(1) .videoTitle"
    assert target.source_state_fingerprint == "sha256:" + "4" * 64
    assert target.target_trust == "trusted"


async def test_logical_h5_page_tab_rebinds_horizontally_offscreen_after_state_drift() -> None:
    """动态内容变更后，唯一横向离屏页签仍可滚回并按当前状态重签。"""
    snapshot = _logical_target_snapshot(state="sha256:" + "4" * 64)
    snapshot["viewport"] = {"width": 390, "height": 844}
    snapshot["elements"] = [
        {
            "selector": "#van-tabs-1-0",
            "dom_sibling_index": 1,
            "tag": "div",
            "role": "tab",
            "text": "最新",
            "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
            "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
            "parent_selector": "#hometab > div:nth-of-type(1) > div",
            "interactive": True,
            "rect": {"x": -424.69, "y": 45.75, "width": 58.22, "height": 51.98},
            "hit_test": {"center_hits_target": False},
        },
        {
            "selector": "#van-tabs-1-1",
            "dom_sibling_index": 2,
            "tag": "div",
            "role": "tab",
            "text": "推荐",
            "class_tokens": ["van-tab", "van-tab--line", "van-tab--grow"],
            "ancestor_class_tokens": ["van-tabs", "van-tabs__nav"],
            "parent_selector": "#hometab > div:nth-of-type(1) > div",
            "interactive": True,
            "rect": {"x": -366.47, "y": 45.75, "width": 58.22, "height": 51.98},
            "hit_test": {"center_hits_target": False},
        },
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=h5_page_tab_target(index=2),
            parameters={
                "target_mode": "page_tab_navigation",
                "frontier_snapshot_id": "h5-task-frontier:" + "8" * 64,
                "navigation_index": 2,
                "navigation_label": "推荐",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == "#van-tabs-1-1"
    assert target.source_state_fingerprint == "sha256:" + "4" * 64
    assert target.target_trust == "trusted"


async def test_logical_list_detail_state_drift_without_h5_frontier_stays_blocked() -> None:
    """缺少 H5 Frontier 证明时，同名导航模式仍按严格状态合同拒绝。"""
    snapshot = _logical_target_snapshot(state="sha256:" + "4" * 64)
    snapshot["viewport"] = {"width": 390, "height": 844}
    snapshot["elements"] = [
        {
            "selector": "#home-video > .item:nth-of-type(1) .videoTitle",
            "tag": "div",
            "text": "当前刷新后的首个视频标题",
            "class_tokens": ["videoTitle"],
            "ancestor_class_tokens": ["videoTitleBox", "horizontalVideoBox"],
            "interactive": False,
            "rect": {"x": 10.39, "y": 453.33, "width": 180.95, "height": 36.39},
            "hit_test": {"center_hits_target": True},
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-home",
                action_type=InteractionOperation.CLICK,
                semantic_target="当前列表首个内容卡片",
                parameters={
                    "target_mode": "list_detail_navigation",
                    "list_detail_selector": ".videoTitle:visible",
                    "source_page_type": "home",
                    "expected_page_type": "video_detail",
                    "logical_target": _logical_target_contract(),
                },
            )
        )

    assert error.value.error_code == "LOGICAL_SOURCE_STATE_STALE"


@pytest.mark.parametrize(
    (
        "family",
        "semantic_target",
        "source_selector",
        "class_tokens",
        "ancestor_tokens",
        "label",
    ),
    [
        (
            "footer_wrap",
            ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(2):visible"),
            "#app > .footer-wrap > ul > li:nth-of-type(2)",
            ["footer-item", "video", "item-1"],
            ["type", "footer-wrap"],
            "视频",
        ),
        (
            "pages_foot",
            "ul.pages-foot-fix:visible > li:nth-of-type(2):visible",
            "#app > .pages-foot-fix > ul > li:nth-of-type(2)",
            ["flex-column", "community"],
            ["pages-foot-fix", "pages-foot-box"],
            "社区",
        ),
    ],
    ids=["footer-wrap", "pages-foot"],
)
async def test_logical_bottom_navigation_binds_registered_non_native_item(
    family: str,
    semantic_target: str,
    source_selector: str,
    class_tokens: list[str],
    ancestor_tokens: list[str],
    label: str,
) -> None:
    """已按底栏族、序号和标签验真的 li 即使无原生交互标记也应保持受信。"""
    snapshot = _logical_target_snapshot()
    snapshot["elements"] = [
        {
            "selector": source_selector,
            "tag": "li",
            "text": label,
            "class_tokens": class_tokens,
            "ancestor_class_tokens": ancestor_tokens,
            "interactive": False,
            "rect": {"x": 76.78, "y": 786.8, "width": 59.11, "height": 39.5},
            "hit_test": {"center_hits_target": True},
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=semantic_target,
            parameters={
                "target_mode": "bottom_navigation",
                "navigation_index": 2,
                "navigation_label": label,
                "navigation_family": family,
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.target_trust == "trusted"
    assert target.expected_rect == {
        "x": 76.78,
        "y": 786.8,
        "width": 59.11,
        "height": 39.5,
    }


async def test_logical_h5_bottom_navigation_rebinds_after_state_drift() -> None:
    """动态首页刷新后仍可按底栏族、序号和标签重签唯一底部导航。"""
    snapshot = _logical_target_snapshot(state="sha256:" + "4" * 64)
    snapshot["elements"] = [
        {
            "selector": "#app > .footer-wrap > ul > li:nth-of-type(2)",
            "tag": "li",
            "text": "视频",
            "class_tokens": ["footer-item", "video", "item-1"],
            "ancestor_class_tokens": ["type", "footer-wrap"],
            "interactive": False,
            "rect": {"x": 76.78, "y": 786.8, "width": 59.11, "height": 39.5},
            "hit_test": {"center_hits_target": True},
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-home",
            action_type=InteractionOperation.CLICK,
            semantic_target=(
                "div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(2):visible"
            ),
            parameters={
                "target_mode": "bottom_navigation",
                "frontier_snapshot_id": "h5-task-frontier:" + "7" * 64,
                "navigation_index": 2,
                "navigation_label": "视频",
                "navigation_family": "footer_wrap",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.source_state_fingerprint == "sha256:" + "4" * 64
    assert target.target_trust == "trusted"


async def test_logical_profile_credential_popup_binds_unique_confirm_button() -> None:
    """凭证弹窗正文包含按钮文案时仍只绑定专用 proof 的源按钮。"""
    from services.api.app.application.h5_profile_credential_popup import (
        h5_profile_credential_screenshot_confirm,
    )

    snapshot = _logical_target_snapshot()
    snapshot["elements"] = [
        {
            "selector": "#mine-pop > .account-cert-popup",
            "tag": "div",
            "text": "请妥善保存账号凭证 请截图保存",
            "class_tokens": ["account-cert-popup"],
            "ancestor_class_tokens": ["van-popup", "van-popup--center", "mine-pop"],
            "interactive": False,
            "rect": {"x": 34.48, "y": 147.66, "width": 321.03, "height": 527.88},
            "hit_test": {"center_hits_target": True},
        },
        {
            "selector": "#mine-pop > .account-cert-popup > .btn",
            "tag": "div",
            "text": "请截图保存",
            "class_tokens": ["btn"],
            "ancestor_class_tokens": [
                "account-cert-popup",
                "van-popup",
                "van-popup--center",
                "mine-pop",
            ],
            "interactive": False,
            "rect": {"x": 59.27, "y": 634.37, "width": 271.43, "height": 41.18},
            "hit_test": {
                "center_hits_target": True,
                "topmost_at_center": True,
            },
        },
    ]
    proof = h5_profile_credential_screenshot_confirm(snapshot)
    assert proof is not None
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile-popup",
            action_type=InteractionOperation.CLICK,
            semantic_target="关闭当前弹窗",
            parameters={
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": proof.capability,
                "obstruction_signature": proof.obstruction_signature,
                "logical_target": _logical_target_contract(expects_navigation=False),
            },
        )
    )

    assert target.target_trust == "trusted"
    assert target.expected_rect == {
        "x": 59.27,
        "y": 634.37,
        "width": 271.43,
        "height": 41.18,
    }


async def test_logical_profile_payment_binds_registered_non_native_vip_image() -> None:
    """签发层已验真的会员图片不得在逻辑绑定层因非原生控件再次否决。"""
    snapshot = _logical_target_snapshot()
    source_selector = (
        "div:nth-of-type(1) > main > div > div > div > "
        "div:nth-of-type(2) > div:nth-of-type(2) > img"
    )
    snapshot["elements"] = [
        {
            "selector": source_selector,
            "tag": "img",
            "src": "https://example.test/static/enable-vip-btn.png",
            "class_tokens": ["vip-right-btn"],
            "ancestor_class_tokens": ["vip-img", "mine"],
            "interactive": False,
            "disabled": False,
            "rect": {
                "x": 251.69,
                "y": 175.73,
                "width": 74.88,
                "height": 29.11,
            },
            "hit_test": {
                "center_hits_target": True,
                "topmost_at_center": True,
                "pointer_interception": True,
                "visual_occlusion": False,
            },
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心会员支付入口",
            parameters={
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "vip_right_button_image",
                "source_page_type": "profile",
                "expected_page_type": "payment",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == source_selector
    assert target.target_trust == "trusted"
    assert target.expected_rect == {
        "x": 251.69,
        "y": 175.73,
        "width": 74.88,
        "height": 29.11,
    }


async def test_logical_profile_activity_binds_registered_non_native_sign_image() -> None:
    """已验真的签到图片应在动作前复核时继续保持可信。"""
    snapshot = _logical_target_snapshot()
    source_selector = "main > img.task_sign"
    snapshot["elements"] = [
        {
            "selector": source_selector,
            "tag": "img",
            "class_tokens": ["task_sign"],
            "ancestor_class_tokens": ["minePage"],
            "interactive": False,
            "disabled": False,
            "rect": {"x": 20, "y": 184, "width": 350, "height": 72},
            "hit_test": {
                "center_hits_target": True,
                "topmost_at_center": True,
                "pointer_interception": True,
                "visual_occlusion": False,
            },
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心签到入口",
            parameters={
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "task_sign_image",
                "source_page_type": "profile",
                "expected_page_type": "activity",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == source_selector
    assert target.target_trust == "trusted"
    assert target.expected_rect == {
        "x": 20.0,
        "y": 184.0,
        "width": 350.0,
        "height": 72.0,
    }


async def test_logical_profile_settings_binds_registered_non_native_header_image() -> None:
    """已验真的个人中心头部设置图片不应被原生 interactive 属性误杀。"""
    snapshot = _logical_target_snapshot()
    source_selector = "header.index-header div.header-left > img.wh-24"
    snapshot["elements"] = [
        {
            "selector": source_selector,
            "tag": "img",
            "class_tokens": ["wh-24"],
            "ancestor_class_tokens": ["header-left", "index-header"],
            "interactive": False,
            "disabled": False,
            "rect": {"x": 18, "y": 18, "width": 24, "height": 24},
            "hit_test": {
                "center_hits_target": True,
                "topmost_at_center": True,
                "pointer_interception": True,
                "visual_occlusion": False,
            },
        }
    ]
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(snapshot),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-profile",
            action_type=InteractionOperation.CLICK,
            semantic_target="个人中心头部设置图标",
            parameters={
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "header_left_second_image",
                "source_page_type": "profile",
                "expected_page_type": "settings",
                "logical_target": _logical_target_contract(),
            },
        )
    )

    assert target.selector == source_selector
    assert target.target_trust == "trusted"
    assert target.expected_rect == {
        "x": 18.0,
        "y": 18.0,
        "width": 24.0,
        "height": 24.0,
    }


async def test_logical_target_binds_flutter_semantics_with_stable_audit() -> None:
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(_logical_target_snapshot()),
        logical_targets_enabled=True,
    )

    target = await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-1",
            action_type=InteractionOperation.CLICK,
            semantic_target="我的",
            parameters={"logical_target": _logical_target_contract()},
        )
    )

    assert target.selector == "#flt-semantic-node-101"
    assert target.target_source == "flutter_semantics"
    assert target.target_trust == "trusted"
    assert target.target_signature is not None
    assert target.target_signature.startswith("sha256:")
    assert target.source_state_fingerprint == "sha256:" + "2" * 64


async def test_logical_target_rejects_visual_only_click_without_structural_anchor() -> None:
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=StaticSnapshotReader(_logical_target_snapshot()),
        logical_targets_enabled=True,
    )

    with pytest.raises(SemanticTargetResolutionError) as error:
        await resolver.resolve(
            SemanticTargetResolutionRequest(
                task_id="task-1",
                session_id="session-1",
                source_capture_id="capture-1",
                action_type=InteractionOperation.CLICK,
                semantic_target="会员中心",
                parameters={
                    "target_mode": "visual_coordinate",
                    "logical_target": _logical_target_contract(),
                },
            )
        )

    assert error.value.error_code == "VISUAL_TARGET_REQUIRES_STRUCTURAL_ANCHOR"


async def test_logical_navigation_without_trusted_transition_keeps_failure_evidence() -> None:
    class UnverifiedTransitionBrowser:
        async def capture(self, request):
            return _page_result(
                mode=request.mode,
                action_trace=[
                    {
                        "action": "click",
                        "transition_kind": "same_page_interaction",
                        "navigation": {
                            "before_url": "https://example.test/",
                            "after_url": "https://example.test/",
                            "current_url_changed": False,
                            "new_page_count": 0,
                        },
                        "state_diff": {
                            "logical_surface_transition": {
                                "protocol_version": "logical_surface_transition.v1",
                                "transition_kind": "surface_state_changed",
                                "logical_transition_proof": "trusted",
                            }
                        },
                    }
                ],
            )

    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=UnverifiedTransitionBrowser(),
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector="#flt-semantic-node-101",
                target_source="flutter_semantics",
                target_signature="sha256:" + "4" * 64,
                source_state_fingerprint="sha256:" + "2" * 64,
                target_trust="trusted",
            )
        ),
        evidence_buffer=buffer,
    )
    intent = _intent(
        parameters={
            "navigation_depth": 1,
            "logical_target": _logical_target_contract(),
        },
        semantic_target="我的",
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-1",
            intent=intent,
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "LOGICAL_TRANSITION_UNVERIFIED"
    assert result.state_summary["post_action_evidence_available"] is True
    assert await buffer.get(action_execution_id=result.action_execution_id) is not None


async def test_popup_dismissal_accepts_trusted_surface_state_transition() -> None:
    """关闭遮挡只要求可信的同页状态变化，不要求伪造新逻辑页面。"""

    class TrustedPopupClearanceBrowser:
        async def capture(self, request):
            return _page_result(
                mode=request.mode,
                action_trace=[
                    {
                        "action": "click",
                        "transition_kind": "same_page_interaction",
                        "navigation": {
                            "before_url": "https://example.test/",
                            "after_url": "https://example.test/",
                            "current_url_changed": False,
                            "new_page_count": 0,
                        },
                        "state_diff": {
                            "logical_surface_transition": {
                                "protocol_version": "logical_surface_transition.v1",
                                "transition_kind": "surface_state_changed",
                                "logical_transition_proof": "trusted",
                                "reason_code": "trusted_obstruction_clearance",
                            }
                        },
                    }
                ],
            )

    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=TrustedPopupClearanceBrowser(),
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector="#popup-close",
                target_source="dom",
                target_signature="sha256:" + "4" * 64,
                source_state_fingerprint="sha256:" + "2" * 64,
                target_trust="trusted",
            )
        ),
        evidence_buffer=buffer,
    )
    intent = _intent(
        parameters={
            "target_mode": "popup_dismissal",
            "logical_target": _logical_target_contract(),
        },
        semantic_target="关闭当前弹窗",
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-1",
            intent=intent,
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code is None
    assert await buffer.get(action_execution_id=result.action_execution_id) is not None


async def test_payment_horizontal_collection_resolves_unique_overflow_container() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": ".vip-plan-list",
                    "visible": True,
                    "text": "月卡 季卡 年卡 永久VIP",
                    "class_tokens": ["vip-plan-list"],
                    "rect": {"x": 16, "y": 240, "width": 358, "height": 220},
                    "style": {"overflow_x": "auto"},
                    "text_metrics": {
                        "scroll_width": 980,
                        "client_width": 358,
                        "horizontal_overflow": True,
                    },
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-payment",
            action_type=InteractionOperation.SCROLL,
            semantic_target="浏览全部会员卡",
            parameters={
                "target_mode": "h5_payment_horizontal_collection",
                "horizontal_scroll_max_segments": 6,
                "split_scroll_screenshots": True,
                "source_page_type": "payment",
            },
        )
    )

    assert target.selector == ".vip-plan-list"


async def test_entitlement_horizontal_collection_resolves_unique_overflow_container() -> None:
    reader = StaticSnapshotReader(
        {
            "viewport": {"width": 390, "height": 844},
            "elements": [
                {
                    "selector": ".limited-time-offer-popup-wrapper .swiper_content",
                    "visible": True,
                    "text": "新人特惠 最受欢迎 热销 新人特惠",
                    "class_tokens": [
                        "swiper",
                        "swiper_content",
                        "swiper-initialized",
                        "swiper-horizontal",
                    ],
                    "ancestor_class_tokens": [
                        "swiper_box",
                        "popup-scroll",
                        "limitedTimeOfferBox",
                        "van-popup",
                        "limited-time-offer-popup-wrapper",
                    ],
                    "rect": {"x": 16, "y": 260, "width": 358, "height": 220},
                    "style": {"overflow_x": "visible"},
                    "text_metrics": {
                        "scroll_width": 980,
                        "client_width": 358,
                        "horizontal_overflow": True,
                    },
                    "hit_test": {"center_hits_target": True},
                }
            ],
        }
    )

    target = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id="task-1",
            session_id="session-1",
            source_capture_id="capture-entitlement",
            action_type=InteractionOperation.SCROLL,
            semantic_target="浏览全部会员卡",
            parameters={
                "target_mode": "h5_payment_horizontal_collection",
                "horizontal_scroll_max_segments": 6,
                "split_scroll_screenshots": True,
                "source_page_type": "video_detail",
            },
        )
    )

    assert target.selector == ".limited-time-offer-popup-wrapper .swiper_content"


@pytest.mark.parametrize(
    "role, required_types, expected",
    [
        (
            "h5_vertical_scroll_collection",
            {"screenshot", "ui_snapshot"},
            ["screenshot", "ui_snapshot", "page_coverage"],
        ),
        ("h5_vertical_scroll_collection", {"screenshot"}, ["screenshot", "page_coverage"]),
        ("h5_vertical_scroll_collection", {"ui_snapshot"}, []),
        ("page_coverage", {"screenshot", "ui_snapshot"}, []),
        ("unknown", {"screenshot", "ui_snapshot"}, []),
    ],
)
def test_h5_vertical_scroll_merge_keeps_requested_frames_and_coverage_gap(
    role,
    required_types,
    expected,
) -> None:
    """仅保留明确标记的 H5 滚动证据，不升级 Web 或未请求截图的采集。"""
    action_result = _page_result(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        extra_artifacts=[
            PageCaptureArtifact(kind, None, {"capture_role": role, "coverage_complete": False})
            for kind in ("screenshot", "ui_snapshot", "page_coverage")
        ],
    )
    capture_result = _page_result(mode=PageCaptureMode.OBSERVE)
    result = _merge_action_evidence(
        capture_result=capture_result,
        action_result=action_result,
        required_types=required_types,
    )
    preserved = [
        item
        for item in result.artifacts
        if isinstance(item.content, dict) and item.content.get("capture_role") == role
    ]
    assert [item.artifact_type for item in preserved] == expected
    assert all(item.content["coverage_complete"] is False for item in preserved)
    assert all(item in result.artifacts for item in capture_result.artifacts)


def test_payment_horizontal_action_artifacts_survive_after_capture_merge() -> None:
    action_result = _page_result(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        extra_artifacts=[
            PageCaptureArtifact(
                "screenshot",
                None,
                {
                    "capture_role": "h5_payment_horizontal_collection",
                    "frame_index": 1,
                },
            ),
            PageCaptureArtifact(
                "ui_snapshot",
                None,
                {"capture_role": "h5_payment_horizontal_collection", "frames": []},
            ),
            PageCaptureArtifact(
                "page_coverage",
                None,
                {"capture_role": "h5_payment_horizontal_collection", "axis": "horizontal"},
            ),
        ],
    )

    result = _merge_action_evidence(
        capture_result=_page_result(mode=PageCaptureMode.OBSERVE),
        action_result=action_result,
        required_types={"screenshot", "ui_snapshot", "page_coverage"},
    )

    preserved = [
        item
        for item in result.artifacts
        if isinstance(item.content, dict)
        and item.content.get("capture_role") == "h5_payment_horizontal_collection"
    ]
    assert [item.artifact_type for item in preserved] == [
        "screenshot",
        "ui_snapshot",
        "page_coverage",
    ]


async def test_startup_ad_dismissal_accepts_verified_intermediate_layer_clearance() -> None:
    """连续开屏广告只清掉当前层时应继续，而不能要求单击后立刻到首页。"""

    class TrustedStartupLayerClearanceBrowser:
        async def capture(self, request):
            return _page_result(
                mode=request.mode,
                action_trace=[
                    {
                        "action": "click",
                        "selector": page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
                        "dismissal_verified": True,
                        "transition_kind": "same_page_interaction",
                        "navigation": {
                            "before_url": "https://example.test/",
                            "after_url": "https://example.test/",
                            "current_url_changed": False,
                            "new_page_count": 0,
                        },
                        "state_diff": {
                            "logical_surface_transition": {
                                "protocol_version": "logical_surface_transition.v1",
                                "transition_kind": "surface_state_changed",
                                "logical_transition_proof": "trusted",
                                "before_identity_fingerprint": "sha256:" + "1" * 64,
                                "after_identity_fingerprint": "sha256:" + "1" * 64,
                                "before_state_fingerprint": "sha256:" + "2" * 64,
                                "after_state_fingerprint": "sha256:" + "3" * 64,
                                "corroborating_signals": ["surface_state"],
                            }
                        },
                    }
                ],
            )

    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=TrustedStartupLayerClearanceBrowser(),
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector=page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
                target_source="dom",
                target_signature="sha256:" + "4" * 64,
                source_state_fingerprint="sha256:" + "2" * 64,
                target_trust="trusted",
            )
        ),
        evidence_buffer=buffer,
    )
    intent = _intent(
        parameters={
            "target_mode": "startup_ad_dismissal",
            "startup_ad_capability": "countdown_close",
            "logical_target": _logical_target_contract(),
        },
        semantic_target="跳过",
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-1",
            intent=intent,
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code is None
    assert result.state_diff["logical_surface_transition"]["reason_code"] == (
        "trusted_startup_ad_layer_clearance"
    )
    assert await buffer.get(action_execution_id=result.action_execution_id) is not None


async def test_startup_ad_dismissal_rejects_unverified_surface_state_change() -> None:
    """轮播自动变化不能冒充已点击并验真的开屏层退场。"""

    class UnverifiedStartupLayerBrowser:
        async def capture(self, request):
            return _page_result(
                mode=request.mode,
                action_trace=[
                    {
                        "action": "click",
                        "selector": page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
                        "dismissal_verified": False,
                        "transition_kind": "same_page_interaction",
                        "navigation": {
                            "before_url": "https://example.test/",
                            "after_url": "https://example.test/",
                            "current_url_changed": False,
                            "new_page_count": 0,
                        },
                        "state_diff": {
                            "logical_surface_transition": {
                                "protocol_version": "logical_surface_transition.v1",
                                "transition_kind": "surface_state_changed",
                                "logical_transition_proof": "trusted",
                                "before_identity_fingerprint": "sha256:" + "1" * 64,
                                "after_identity_fingerprint": "sha256:" + "1" * 64,
                                "before_state_fingerprint": "sha256:" + "2" * 64,
                                "after_state_fingerprint": "sha256:" + "3" * 64,
                                "corroborating_signals": ["surface_state"],
                            }
                        },
                    }
                ],
            )

    executor = BrowserInteractionExecutor(
        browser=UnverifiedStartupLayerBrowser(),
        target_resolver=RecordingResolver(
            ResolvedInteractionTarget(
                selector=page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
                target_source="dom",
                target_signature="sha256:" + "4" * 64,
                source_state_fingerprint="sha256:" + "2" * 64,
                target_trust="trusted",
            )
        ),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )
    intent = _intent(
        parameters={
            "target_mode": "startup_ad_dismissal",
            "startup_ad_capability": "countdown_close",
            "logical_target": _logical_target_contract(),
        },
        semantic_target="跳过",
    )

    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-1",
            intent=intent,
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == "LOGICAL_TRANSITION_UNVERIFIED"


@pytest.mark.parametrize(
    "invalid_fact",
    [
        "unregistered_selector",
        "untrusted_target",
        "source_state_mismatch",
        "identity_changed",
        "state_unchanged",
        "surface_state_signal_missing",
    ],
)
def test_startup_ad_layer_clearance_requires_all_trusted_facts(
    invalid_fact: str,
) -> None:
    """单层广告退场缺少任一受信事实时，都不能放宽逻辑导航后置条件。"""
    entry = {
        "action": "click",
        "selector": page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
        "dismissal_verified": True,
        "logical_target": {
            "target_trust": "trusted",
            "source_state_fingerprint": "sha256:" + "2" * 64,
        },
        "state_diff": {
            "logical_surface_transition": {
                "protocol_version": "logical_surface_transition.v1",
                "transition_kind": "surface_state_changed",
                "logical_transition_proof": "trusted",
                "before_identity_fingerprint": "sha256:" + "1" * 64,
                "after_identity_fingerprint": "sha256:" + "1" * 64,
                "before_state_fingerprint": "sha256:" + "2" * 64,
                "after_state_fingerprint": "sha256:" + "3" * 64,
                "corroborating_signals": ["surface_state"],
            }
        },
    }
    transition = entry["state_diff"]["logical_surface_transition"]
    if invalid_fact == "unregistered_selector":
        entry["selector"] = ".model-invented-skip"
    elif invalid_fact == "untrusted_target":
        entry["logical_target"]["target_trust"] = "untrusted"
    elif invalid_fact == "source_state_mismatch":
        entry["logical_target"]["source_state_fingerprint"] = "sha256:" + "9" * 64
    elif invalid_fact == "identity_changed":
        transition["after_identity_fingerprint"] = "sha256:" + "8" * 64
    elif invalid_fact == "state_unchanged":
        transition["after_state_fingerprint"] = "sha256:" + "2" * 64
    elif invalid_fact == "surface_state_signal_missing":
        transition["corroborating_signals"] = []

    _annotate_startup_ad_trace_entry(entry)

    assert entry["state_diff"]["logical_surface_transition"].get("reason_code") != (
        "trusted_startup_ad_layer_clearance"
    )


@pytest.mark.parametrize(
    ("visual_proof", "expected_error"),
    [
        (
            {
                "protocol_version": "visual_navigation_transition_proof.v1",
                "status": "insufficient",
                "after_frame_count": 2,
                "after_frames_stable": False,
                "main_structure_changed": True,
                "after_identity_bound": True,
                "reason_codes": ["after_frames_inconsistent"],
            },
            "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
        ),
        (
            {
                "protocol_version": "visual_navigation_transition_proof.v1",
                "status": "no_effect",
                "after_frame_count": 2,
                "after_frames_stable": True,
                "main_structure_changed": False,
                "after_identity_bound": True,
                "reason_codes": ["visual_navigation_no_effect"],
            },
            "VISUAL_NAVIGATION_NO_EFFECT",
        ),
        (
            {
                "protocol_version": "visual_navigation_transition_proof.v1",
                "status": "stale",
                "after_frame_count": 2,
                "after_frames_stable": True,
                "main_structure_changed": True,
                "after_identity_bound": True,
                "reason_codes": ["visual_source_state_mismatch"],
            },
            "VISUAL_TRANSITION_EVIDENCE_INSUFFICIENT",
        ),
    ],
)
async def test_visual_candidate_requires_trusted_bound_visual_postcondition(
    visual_proof: dict[str, object],
    expected_error: str,
) -> None:
    """视觉候选没有绑定当前 after identity 的主证明时不得标记已到达。"""
    after_identity = "sha256:" + "8" * 64

    class VisualTransitionBrowser:
        async def capture(self, request):
            page_capture_module._commit_visual_navigation_click(
                request,
                resolved_by="validated_visual_navigation_coordinate",
                visual_target_relocated=False,
                visual_match_score=1.0,
                visual_audit_codes=(),
            )
            return _page_result(
                mode=request.mode,
                action_trace=[
                    {
                        "action": "click",
                        "transition_kind": "same_page_interaction",
                        "navigation": {
                            "before_url": "https://example.test/",
                            "after_url": "https://example.test/",
                            "current_url_changed": False,
                            "new_page_count": 0,
                        },
                        "state_diff": {
                            "logical_surface_transition": {
                                "protocol_version": "logical_surface_transition.v1",
                                "transition_kind": "uncertain",
                                "logical_transition_proof": "uncertain",
                                "after_identity_fingerprint": after_identity,
                                "visual_proof": visual_proof,
                            }
                        },
                    }
                ],
            )

    buffer = InMemoryActionEvidenceBuffer()
    executor = BrowserInteractionExecutor(
        browser=VisualTransitionBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#visual")),
        evidence_buffer=buffer,
    )
    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-visual-postcondition",
            intent=_intent(
                parameters={
                    "navigation_depth": 1,
                    "target_mode": "visual_navigation_coordinate",
                    "logical_target": _logical_target_contract(),
                }
            ),
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.BLOCKED
    assert result.error_code == expected_error
    assert result.retryable is True
    assert result.state_diff["visual_action_committed"] is True
    assert await buffer.get(action_execution_id=result.action_execution_id) is not None


async def test_visual_candidate_accepts_only_trusted_proof_bound_to_after_identity() -> None:
    """受信两帧 proof 与当前 after identity 同处一份 transition 时才通过 Runtime。"""
    after_identity = "sha256:" + "8" * 64

    class TrustedVisualTransitionBrowser:
        async def capture(self, request):
            return _page_result(
                mode=request.mode,
                action_trace=[
                    {
                        "action": "click",
                        "transition_kind": "same_page_interaction",
                        "navigation": {
                            "before_url": "https://example.test/",
                            "after_url": "https://example.test/",
                            "current_url_changed": False,
                            "new_page_count": 0,
                        },
                        "state_diff": {
                            "logical_surface_transition": {
                                "protocol_version": "logical_surface_transition.v1",
                                "transition_kind": "logical_page_changed",
                                "logical_transition_proof": "trusted",
                                "after_identity_fingerprint": after_identity,
                                "visual_proof": {
                                    "protocol_version": ("visual_navigation_transition_proof.v1"),
                                    "status": "trusted",
                                    "after_frame_count": 2,
                                    "after_frames_stable": True,
                                    "main_structure_changed": True,
                                    "after_identity_bound": True,
                                    "destination_structure": "detail",
                                    "reason_codes": [],
                                },
                            }
                        },
                    }
                ],
            )

    executor = BrowserInteractionExecutor(
        browser=TrustedVisualTransitionBrowser(),
        target_resolver=RecordingResolver(ResolvedInteractionTarget(selector="#visual")),
        evidence_buffer=InMemoryActionEvidenceBuffer(),
    )
    result = await executor.execute(
        ExecuteInteractionRequest(
            agent_run_id="run-trusted-visual-postcondition",
            intent=_intent(
                parameters={
                    "navigation_depth": 1,
                    "target_mode": "visual_navigation_coordinate",
                    "logical_target": _logical_target_contract(),
                }
            ),
            source_capture_id="capture-before",
        )
    )

    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert result.error_code is None
