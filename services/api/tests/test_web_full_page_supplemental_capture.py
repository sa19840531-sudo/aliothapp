"""Web 全页补充截图必须只读、可降级且每个来源 Capture 只尝试一次。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
import zlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    _validate_request,
)


def _png_chunk(chunk_type: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(chunk_type)
    checksum = zlib.crc32(payload, checksum)
    return struct.pack(">I", len(payload)) + chunk_type + payload + struct.pack(">I", checksum)


def _png(*, width: int = 1920, height: int = 1080, ancillary_bytes: int = 0) -> bytes:
    """构造真实带 CRC 的 PNG；大体积用合法 ancillary chunk 避免伪造字节。"""
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        (
            _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)),
            _png_chunk(b"tEXt", b"x" * ancillary_bytes) if ancillary_bytes else b"",
            _png_chunk(b"IDAT", zlib.compress(b"\x00\x00")),
            _png_chunk(b"IEND", b""),
        )
    )


class _Browser:
    def __init__(self) -> None:
        self.requests = []
        self.result = _result()
        self.error: BaseException | None = None

    async def capture(self, request):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return self.result


class _Persistence:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.error: BaseException | None = None

    async def persist(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            page_capture_id="supplemental-capture",
            artifacts=(
                SimpleNamespace(artifact_id="supplemental-screenshot", artifact_type="screenshot"),
            ),
        )


class _Claims:
    def __init__(self) -> None:
        self.claimed = True
        self.finished: list[dict[str, object]] = []
        self.finish_error: BaseException | None = None
        self.terminal_result = None

    async def claim(self, *, command, permit):
        return "phase-1" if self.claimed else None

    async def finish(self, **kwargs) -> None:
        self.finished.append(kwargs)
        if self.finish_error is not None:
            raise self.finish_error
        if kwargs["status"] == "failed":
            from services.api.app.application.web_full_page_supplemental_capture import (
                WebFullPageSupplementalResult,
            )

            self.terminal_result = WebFullPageSupplementalResult(
                status="failed",
                source_capture_id="source-capture",
                supplemental_capture_id=None,
                screenshot_artifact_id=None,
                reason_code=kwargs["reason_code"],
            )

    async def read_terminal(self, *, command):
        return self.terminal_result


class _Gate:
    def __init__(self, *, current_capture_id: str = "source-capture") -> None:
        self.current_capture_id = current_capture_id

    @asynccontextmanager
    async def hold(self, *, task_id, session_id, surface_kind=None, allow_draining=False):
        yield SessionCommandPermit(
            task_id=task_id,
            session_id=session_id,
            surface_kind=SurfaceKind.WEB,
            lease_owner="test-worker",
            lease_token="00000000-0000-0000-0000-000000000001",
            command_sequence=1,
            finalization_epoch=0,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
            current_capture_id=self.current_capture_id,
        )


async def test_completed_full_page_is_reused_before_session_gate_reacquisition():
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalCaptureService,
        WebFullPageSupplementalResult,
    )

    class ClosedGate:
        @asynccontextmanager
        async def hold(self, **_kwargs):
            raise RuntimeError("已完成证据恢复不应重新领取 Session")
            yield

    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    claims.terminal_result = WebFullPageSupplementalResult(
        status="captured",
        source_capture_id="source-capture",
        supplemental_capture_id="supplemental-capture",
        screenshot_artifact_id="supplemental-screenshot",
        reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
    )
    service = WebFullPageSupplementalCaptureService(
        browser=browser,
        persistence=persistence,
        claims=claims,
        session_command_gate=ClosedGate(),
    )
    result = await service.capture(_command())
    assert result.screenshot_artifact_id == "supplemental-screenshot"
    assert browser.requests == []
    assert persistence.calls == []


class _ClaimRecordingConnection:
    def __init__(self) -> None:
        self.claim_arguments = ()
        self.claim_queries: list[str] = []

    async def fetchval(self, query, *arguments):
        self.claim_queries.append(" ".join(query.split()))
        self.claim_arguments = arguments
        return "phase-1"


async def test_checkpoint_second_attempt_reopens_only_the_existing_failed_full_page_phase():
    from dataclasses import replace

    from services.api.app.domain.exploration.web_run_stage_checkpoint import (
        WebRunStage,
        WebStageCursor,
    )
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    run_id = "00000000-0000-0000-0000-000000000006"
    source_id = "00000000-0000-0000-0000-000000000007"
    now = datetime.now(UTC)
    stage = WebStageCursor.start(
        run_id=run_id,
        logical_page_key="home",
        stage=WebRunStage.FULL_PAGE_CAPTURE,
        cursor_payload={
            "session_id": f"web:{run_id}",
            "finalization_epoch": 0,
            "capture_id": source_id,
        },
    ).claim(worker_id="worker", now=now)
    stage = stage.schedule_retry(error_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT", now=now)
    stage = stage.claim(worker_id="worker", now=now + timedelta(seconds=2))
    request = replace(
        _command(),
        agent_run_id=run_id,
        session_id=f"web:{run_id}",
        source_capture_id=source_id,
        stage_cursor=stage,
    )

    class RetryConnection(_ClaimRecordingConnection):
        async def fetchval(self, query, *arguments):
            self.claim_queries.append(query)
            if "INSERT INTO" in query:
                return None
            assert "UPDATE qc_surface_phase_run" in query
            assert stage.claim_token in arguments
            assert "checkpoint.lease_expires_at>clock_timestamp()" in query
            return "phase-1"

    connection = RetryConnection()
    repository = PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(connection)
    )
    async with _Gate(current_capture_id=source_id).hold(
        task_id=request.task_id, session_id=request.session_id
    ) as permit:
        result = await repository.claim(command=request, permit=permit)
    assert result == "phase-1"
    assert len(connection.claim_queries) == 2


class _ClaimRecordingAcquire:
    def __init__(self, connection: _ClaimRecordingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _ClaimRecordingConnection:
        return self._connection

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class _ClaimRecordingPool:
    def __init__(self, connection: _ClaimRecordingConnection) -> None:
        self._connection = connection

    def acquire(self) -> _ClaimRecordingAcquire:
        return _ClaimRecordingAcquire(self._connection)


class _TerminalConnection(_ClaimRecordingConnection):
    def __init__(self, rows) -> None:
        super().__init__()
        self.rows = list(rows)
        self.fetchrow_queries: list[str] = []

    async def fetchrow(self, query, *arguments):
        self.fetchrow_queries.append(" ".join(query.split()))
        return self.rows.pop(0) if self.rows else None


def _result(
    *,
    final_url: str = "https://example.test/",
    full_page: bool = True,
    screenshot: bytes = _png(),
):
    return PageCaptureResult(
        execution_status="completed",
        mode="observe",
        reachable=True,
        final_url=final_url,
        title="Example",
        status_code=200,
        artifacts=[
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={
                    "purpose": "web_direct_full_page",
                    "encoding": "base64",
                    "data": base64.b64encode(screenshot).decode("ascii"),
                    "full_page": full_page,
                },
            )
        ],
        metrics={
            "web_readiness": {
                "identity_before": "https://example.test/",
                "identity_after": final_url,
                "artifacts_same_epoch": True,
                "structure_before": "structure-a",
                "structure_after": "structure-a",
                "capture_consistent": True,
                "capture_consistency": {"status": "consistent"},
            }
        },
    )


def _segmented_result():
    segments = (_png(), _png())
    manifest_segments = [
        {
            "index": index,
            "top_css_px": index * 1080,
            "bottom_css_px": (index + 1) * 1080,
            "viewport_height_css_px": 1080,
            "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
            "size_bytes": len(payload),
        }
        for index, payload in enumerate(segments)
    ]
    result = _result()
    return replace(
        result,
        artifacts=[
            *[
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content={
                        "purpose": "web_full_page_segment",
                        "segment_index": index,
                        "encoding": "base64",
                        "data": base64.b64encode(payload).decode("ascii"),
                    },
                )
                for index, payload in enumerate(segments)
            ],
            PageCaptureArtifact(
                artifact_type="page_coverage",
                artifact_id=None,
                content={
                    "protocol_version": "web-full-page-segment-manifest.v1",
                    "document_height": 2160,
                    "viewport_height": 1080,
                    "segment_count": 2,
                    "segments": manifest_segments,
                },
            ),
        ],
    )


def _bounded_segmented_result():
    result = _segmented_result()
    manifest = next(
        item for item in result.artifacts if item.artifact_type == "page_coverage"
    )
    return replace(
        result,
        artifacts=[
            *[item for item in result.artifacts if item.artifact_type != "page_coverage"],
            replace(
                manifest,
                content={
                    **manifest.content,
                    "document_height": 9000,
                    "captured_height": 2160,
                    "truncated": True,
                },
            ),
        ],
    )


def test_new_supplemental_rejects_bounded_segment_manifest():
    from services.api.app.application.web_full_page_supplemental_capture import (
        _full_page_result_failure_reason,
    )

    assert _full_page_result_failure_reason(
        result=_bounded_segmented_result(), command=_command()
    ) == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"


def test_new_supplemental_rejects_scrolled_viewport_manifest():
    from services.api.app.application.web_full_page_supplemental_capture import (
        _full_page_result_failure_reason,
    )

    payload = _png(width=1920, height=1080)
    result = _result(screenshot=payload)
    result = replace(
        result,
        artifacts=[
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={
                    "purpose": "web_scrolled_viewport",
                    "encoding": "base64",
                    "data": base64.b64encode(payload).decode("ascii"),
                },
            ),
            PageCaptureArtifact(
                artifact_type="page_coverage",
                artifact_id=None,
                content={
                    "protocol_version": "web-scrolled-viewport-manifest.v1",
                    "capture_mode": "scroll_offset_viewport",
                    "document_height": 9000,
                    "requested_scroll_offset_px": 1920,
                    "actual_scroll_y": 1920,
                    "viewport_top_css_px": 1920,
                    "viewport_bottom_css_px": 3000,
                    "screenshot": {
                        "size_bytes": len(payload),
                        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    },
                },
            ),
        ],
    )

    assert _full_page_result_failure_reason(result=result, command=_command()) == (
        "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    )


def test_scrolled_viewport_cannot_replace_new_direct_full_page_when_structure_drifts():
    """新补采即使取得有效视口 PNG，也不得当成整页证据。"""
    from services.api.app.application.web_full_page_supplemental_capture import (
        _full_page_result_failure_reason,
        _full_page_result_validation_failures,
    )

    payload = _png(width=1920, height=1080)
    result = replace(
        _result(screenshot=payload),
        artifacts=[
            PageCaptureArtifact(
                "screenshot",
                None,
                {
                    "purpose": "web_scrolled_viewport",
                    "encoding": "base64",
                    "data": base64.b64encode(payload).decode("ascii"),
                },
            ),
            PageCaptureArtifact(
                "page_coverage",
                None,
                {
                    "protocol_version": "web-scrolled-viewport-manifest.v1",
                    "capture_mode": "scroll_offset_viewport",
                    "document_height": 9000,
                    "requested_scroll_offset_px": 1920,
                    "actual_scroll_y": 1920,
                    "viewport_top_css_px": 1920,
                    "viewport_bottom_css_px": 3000,
                    "screenshot": {
                        "size_bytes": len(payload),
                        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    },
                },
            ),
        ],
        metrics={
            "web_full_page_stabilization": {
                "capture_mode": "scroll_offset_viewport",
                "stable": True,
                "scroll_restore_status": "error",
            },
            "web_readiness": {
                **_result().metrics["web_readiness"],
                "structure_after": "structure-b",
                "capture_consistent": False,
            },
        },
    )

    assert _full_page_result_failure_reason(result=result, command=_command()) == (
        "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    )
    assert _full_page_result_validation_failures(result=result, command=_command()) == (
        "direct_full_page_contract",
    )


def test_direct_full_page_keeps_saved_png_when_content_changes_in_same_document():
    from services.api.app.application.web_full_page_supplemental_capture import (
        _full_page_result_failure_reason,
        _full_page_result_validation_failures,
    )

    original = _result(screenshot=_png(height=3500))
    screenshot = original.artifacts[0]
    result = replace(
        original,
        artifacts=[
            replace(screenshot, content={**screenshot.content, "purpose": "web_direct_full_page"})
        ],
        metrics={
            "web_readiness": {
                **original.metrics["web_readiness"],
                "structure_after": "structure-b",
                "capture_consistent": False,
                "capture_consistency": {"status": "changed", "changed_signals": ["structure"]},
            }
        },
    )
    assert _full_page_result_failure_reason(result=result, command=_command()) is None
    assert _full_page_result_validation_failures(result=result, command=_command()) == ()
    altered = replace(
        result,
        metrics={
            "web_readiness": {
                **result.metrics["web_readiness"],
                "artifacts_same_epoch": False,
            }
        },
    )
    assert _full_page_result_failure_reason(result=altered, command=_command()) == (
        "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    )


def test_scrolled_viewport_with_companion_visual_navigation_is_still_rejected():
    """伴随视觉导航图不能把滚动视口结果提升为整页证据。"""
    from services.api.app.application.web_full_page_supplemental_capture import (
        _full_page_result_failure_reason,
    )

    payload = _png(width=1920, height=1080)
    companion = _png(width=1280, height=720)
    result = replace(
        _result(screenshot=payload),
        artifacts=[
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={
                    "purpose": "web_scrolled_viewport",
                    "encoding": "base64",
                    "data": base64.b64encode(payload).decode("ascii"),
                },
            ),
            PageCaptureArtifact(
                artifact_type="page_coverage",
                artifact_id=None,
                content={
                    "protocol_version": "web-scrolled-viewport-manifest.v1",
                    "capture_mode": "scroll_offset_viewport",
                    "document_height": 9000,
                    "requested_scroll_offset_px": 1920,
                    "actual_scroll_y": 1920,
                    "viewport_top_css_px": 1920,
                    "viewport_bottom_css_px": 3000,
                    "screenshot": {
                        "size_bytes": len(payload),
                        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    },
                },
            ),
            PageCaptureArtifact(
                artifact_type="screenshot",
                artifact_id=None,
                content={
                    "purpose": "visual_navigation_viewport",
                    "encoding": "base64",
                    "data": base64.b64encode(companion).decode("ascii"),
                },
            ),
        ],
    )

    assert _full_page_result_failure_reason(result=result, command=_command()) == (
        "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    )


@pytest.mark.asyncio
async def test_scrolled_viewport_is_not_persisted_as_new_supplemental_capture():
    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    payload = _png(width=1920, height=1080)
    companion = _png(width=1280, height=720)
    browser.result = replace(
        _result(screenshot=payload),
        artifacts=[
            PageCaptureArtifact(
                "screenshot",
                None,
                {
                    "purpose": "web_scrolled_viewport",
                    "encoding": "base64",
                    "data": base64.b64encode(payload).decode("ascii"),
                },
            ),
            PageCaptureArtifact(
                "page_coverage",
                None,
                {
                    "protocol_version": "web-scrolled-viewport-manifest.v1",
                    "capture_mode": "scroll_offset_viewport",
                    "document_height": 9000,
                    "requested_scroll_offset_px": 1920,
                    "actual_scroll_y": 1920,
                    "viewport_top_css_px": 1920,
                    "viewport_bottom_css_px": 3000,
                    "screenshot": {
                        "size_bytes": len(payload),
                        "sha256": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    },
                },
            ),
            PageCaptureArtifact(
                "screenshot",
                None,
                {
                    "purpose": "visual_navigation_viewport",
                    "encoding": "base64",
                    "data": base64.b64encode(companion).decode("ascii"),
                },
            ),
        ],
    )

    async def persist(**kwargs):
        persistence.calls.append(kwargs)
        return SimpleNamespace(
            page_capture_id="supplemental-capture",
            artifacts=(
                SimpleNamespace(artifact_id="coverage-shot", artifact_type="screenshot"),
                SimpleNamespace(artifact_id="coverage-manifest", artifact_type="page_coverage"),
                SimpleNamespace(artifact_id="visual-shot", artifact_type="screenshot"),
            ),
        )

    persistence.persist = persist
    result = await _service(
        browser=browser,
        persistence=persistence,
        claims=claims,
    ).capture(_command())

    assert result.status == "failed"
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert persistence.calls == []
    assert claims.finished[0]["artifact_ids"] == ()


@pytest.mark.asyncio
async def test_failed_result_validation_persists_only_safe_failed_check_names():
    browser, claims = _Browser(), _Claims()
    browser.result = replace(
        _result(),
        metrics={
            "web_full_page_stabilization": {"stable": True, "actual_scroll_y": 1920},
            "web_readiness": {
                **_result().metrics["web_readiness"],
                "artifacts_same_epoch": False,
            },
        },
    )

    result = await _service(browser=browser, claims=claims).capture(_command())

    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert claims.finished[0]["stabilization_audit"]["result_validation_failures"] == [
        "document_epoch",
    ]


def _service(*, browser=None, persistence=None, claims=None, gate=None):
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalCaptureService,
    )

    return WebFullPageSupplementalCaptureService(
        browser=browser or _Browser(),
        persistence=persistence or _Persistence(),
        claims=claims or _Claims(),
        session_command_gate=gate or _Gate(),
    )


def _command():
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalCommand,
    )

    return WebFullPageSupplementalCommand(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        source_capture_id="source-capture",
        source_url="https://example.test/",
        deadline=datetime.now(UTC) + timedelta(seconds=10),
    )


def test_explicit_supplemental_stage_exposes_supplemental_readiness_role() -> None:
    """移除 stage 白名单或角色映射会令长图请求无法经过浏览器边界。"""
    from services.api.app.infrastructure.browser.page_capture import _web_readiness_metrics

    _validate_request(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            web_capture_stage="web_full_page_supplemental",
        )
    )
    readiness = _web_readiness_metrics(
        web_capture_stage="web_full_page_supplemental",
        identity_before="https://example.test/",
        identity_after="https://example.test/",
        page_settle_complete=True,
        page_inspection_ready=True,
        readiness={},
        network_observable=True,
        critical_network_pending=0,
        settle_diagnostics={"settle_complete": True},
        capture_consistency={
            "before": {"document_observation_available": True, "document_time_origin_ms": 1},
            "after": {"document_observation_available": True, "document_time_origin_ms": 1},
        },
    )
    assert readiness is not None
    assert readiness["role"] == "supplemental_classification"


@pytest.mark.asyncio
async def test_claim_binds_existing_formal_ready_roles_to_query_boundary() -> None:
    """primary-only 角色集合会拒绝真实 after_action ready Capture。"""
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    connection = _ClaimRecordingConnection()
    permit = SessionCommandPermit(
        task_id="task-1",
        session_id="web:run-1",
        surface_kind=SurfaceKind.WEB,
        lease_owner="test-worker",
        lease_token="00000000-0000-0000-0000-000000000001",
        command_sequence=1,
        finalization_epoch=0,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        current_capture_id="capture-1",
    )
    command = SimpleNamespace(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        source_capture_id="capture-1",
        source_url="https://example.test/",
    )

    phase_id = await PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(connection)
    ).claim(command=command, permit=permit)

    assert phase_id == "phase-1"
    assert connection.claim_arguments[-2] == ("after_action", "primary")
    assert connection.claim_arguments[-1] == "formal_ready"
    query = connection.claim_queries[-1]
    assert "pipeline_metrics,web_readiness,settle_complete" in query
    assert "pipeline_metrics,web_readiness,capture_consistent" in query
    assert "web_evidence_admission" not in query
    assert "web_evidence_admission,kind" not in query
    assert "can_enter_formal_bundle" not in query
    assert "artifact.artifact_type='screenshot'" in query
    assert "artifact.artifact_type='dom'" in query


@pytest.mark.asyncio
async def test_pre_gate_claim_is_explicit_and_does_not_authorize_arbitrary_probe() -> None:
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    connection = _ClaimRecordingConnection()
    permit = SessionCommandPermit(
        task_id="task-1",
        session_id="web:run-1",
        surface_kind=SurfaceKind.WEB,
        lease_owner="test-worker",
        lease_token="00000000-0000-0000-0000-000000000001",
        command_sequence=1,
        finalization_epoch=0,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        current_capture_id="capture-1",
    )
    repository = PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(connection)
    )
    command = SimpleNamespace(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        source_capture_id="capture-1",
        source_url="https://example.test/",
        source_eligibility="trusted_pre_gate",
    )

    assert await repository.claim(command=command, permit=permit) == "phase-1"
    query = connection.claim_queries[-1]
    assert "trusted_pre_gate" in query
    assert "pipeline_metrics,web_readiness,settle_complete" in query
    assert "web_evidence_admission" not in query
    assert "capture.capture_role=ANY" in query
    assert "artifact.artifact_type='screenshot'" in query
    assert "artifact.artifact_type='dom'" in query

    with pytest.raises(ValueError, match="source_eligibility"):
        await repository.claim(
            command=SimpleNamespace(**{**command.__dict__, "source_eligibility": "probe"}),
            permit=permit,
        )


@pytest.mark.asyncio
async def test_captures_one_full_page_child_without_advancing_primary() -> None:
    browser, persistence = _Browser(), _Persistence()
    result = await _service(browser=browser, persistence=persistence).capture(_command())

    request = browser.requests[0]
    assert request.mode is PageCaptureMode.OBSERVE
    assert request.capture_dom is False
    assert request.capture_visible_text is False
    assert request.capture_screenshot is True
    assert request.full_page is True
    assert request.web_capture_stage == "web_full_page_supplemental"
    assert persistence.calls[0]["parent_page_capture_id"] == "source-capture"
    assert persistence.calls[0]["capture_role"] == "supplemental"
    assert persistence.calls[0]["capture_purpose"] == "web_full_page_supplemental"
    assert persistence.calls[0]["inherit_parent_semantics"] is False
    assert result.status == "captured"
    assert result.supplemental_capture_id == "supplemental-capture"
    assert result.screenshot_artifact_id == "supplemental-screenshot"


@pytest.mark.asyncio
async def test_segmented_full_page_is_not_persisted_as_new_supplemental_capture() -> None:
    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    browser.result = _segmented_result()

    async def persist(**kwargs):
        persistence.calls.append(kwargs)
        return SimpleNamespace(
            page_capture_id="supplemental-capture",
            artifacts=(
                SimpleNamespace(artifact_id="segment-0", artifact_type="screenshot"),
                SimpleNamespace(artifact_id="segment-1", artifact_type="screenshot"),
                SimpleNamespace(artifact_id="segment-manifest", artifact_type="page_coverage"),
            ),
        )

    persistence.persist = persist

    result = await _service(
        browser=browser,
        persistence=persistence,
        claims=claims,
    ).capture(_command())

    assert result.status == "failed"
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert persistence.calls == []
    assert claims.finished[0]["artifact_ids"] == ()


@pytest.mark.asyncio
async def test_account_identity_capability_gap_does_not_reject_stable_full_page() -> None:
    """通用 Web 无账号读取能力时，只要其余边界稳定，长图仍应可留存。"""

    browser, persistence = _Browser(), _Persistence()
    consistency = browser.result.metrics["web_readiness"]["capture_consistency"]
    consistency.update(
        {
            "status": "consistent_with_capability_gaps",
            "changed_signals": [],
            "unavailable_signals": ["account_identity"],
        }
    )

    result = await _service(browser=browser, persistence=persistence).capture(_command())

    assert result.status == "captured"
    assert len(persistence.calls) == 1


@pytest.mark.asyncio
async def test_direct_full_page_keeps_png_when_window_signal_is_unavailable() -> None:
    browser, persistence = _Browser(), _Persistence()
    consistency = browser.result.metrics["web_readiness"]["capture_consistency"]
    consistency.update(
        {
            "status": "consistent_with_capability_gaps",
            "changed_signals": [],
            "unavailable_signals": ["window_set"],
        }
    )

    result = await _service(browser=browser, persistence=persistence).capture(_command())

    assert result.status == "captured"
    assert len(persistence.calls) == 1


@pytest.mark.asyncio
async def test_state_drift_fails_without_persisting_or_retrying() -> None:
    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    browser.result = _result(final_url="https://example.test/changed", full_page=True)
    service = _service(browser=browser, persistence=persistence, claims=claims)

    first = await service.capture(_command())
    claims.claimed = False
    second = await service.capture(_command())

    assert first.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert second.status == "failed"
    assert second.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert persistence.calls == []
    assert claims.finished == [
        {
            "phase_id": "phase-1",
            "status": "failed",
            "reason_code": "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT",
            "artifact_ids": (),
            "stabilization_audit": {
                "result_validation_failures": [
                    "final_url_identity",
                    "source_url_identity",
                ]
            },
        }
    ]


@pytest.mark.asyncio
async def test_repeat_read_restores_original_captured_terminal_without_browsing_again() -> None:
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalResult,
    )

    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    service = _service(browser=browser, persistence=persistence, claims=claims)

    first = await service.capture(_command())
    claims.claimed = False
    claims.terminal_result = WebFullPageSupplementalResult(
        status="captured",
        source_capture_id="source-capture",
        supplemental_capture_id="supplemental-capture",
        screenshot_artifact_id="supplemental-screenshot",
        reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
    )
    second = await service.capture(_command())

    assert second == first
    assert len(browser.requests) == 1
    assert len(persistence.calls) == 1


@pytest.mark.asyncio
async def test_finish_interruption_is_recoverable_on_next_read_without_second_capture() -> None:
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalResult,
    )

    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    claims.finish_error = RuntimeError("finish interrupted")
    service = _service(browser=browser, persistence=persistence, claims=claims)

    with pytest.raises(RuntimeError, match="finish interrupted"):
        await service.capture(_command())

    claims.claimed = False
    claims.finish_error = None
    claims.terminal_result = WebFullPageSupplementalResult(
        status="captured",
        source_capture_id="source-capture",
        supplemental_capture_id="supplemental-capture",
        screenshot_artifact_id="supplemental-screenshot",
        reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
    )
    recovered = await service.capture(_command())

    assert recovered.status == "captured"
    assert len(browser.requests) == 1
    assert len(persistence.calls) == 1


@pytest.mark.asyncio
async def test_cancellation_during_completed_finish_retries_completed_binding() -> None:
    class Claims(_Claims):
        def __init__(self) -> None:
            super().__init__()
            self.cancel_completed_once = True

        async def finish(self, **kwargs) -> None:
            self.finished.append(kwargs)
            if kwargs["status"] == "completed" and self.cancel_completed_once:
                self.cancel_completed_once = False
                raise asyncio.CancelledError()

    claims = Claims()

    with pytest.raises(asyncio.CancelledError):
        await _service(claims=claims).capture(_command())

    assert [call["status"] for call in claims.finished] == ["completed", "completed"]
    assert claims.finished[1]["artifact_ids"] == ("supplemental-screenshot",)


@pytest.mark.asyncio
async def test_postgres_repeat_read_restores_failed_terminal_reason() -> None:
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    connection = _TerminalConnection(
        [
            {
                "status": "failed",
                "terminal_reason_code": "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                "supplemental_capture_id": None,
                "screenshot_artifact_id": None,
            }
        ]
    )
    result = await PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(connection)
    ).read_terminal(command=_command())

    assert result is not None
    assert result.status == "failed"
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"


@pytest.mark.asyncio
async def test_postgres_repeat_read_reconciles_persisted_child_after_finish_interruption() -> None:
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    connection = _TerminalConnection(
        [
            {
                "status": "running",
                "terminal_reason_code": None,
                "supplemental_capture_id": "supplemental-capture",
                "screenshot_artifact_id": "supplemental-screenshot",
            },
            {"phase_run_id": "phase-1"},
            {
                "status": "completed",
                "terminal_reason_code": "WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                "supplemental_capture_id": "supplemental-capture",
                "screenshot_artifact_id": "supplemental-screenshot",
            },
        ]
    )
    result = await PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(connection)
    ).read_terminal(command=_command())

    assert result is not None
    assert result.status == "captured"
    assert result.supplemental_capture_id == "supplemental-capture"
    assert result.screenshot_artifact_id == "supplemental-screenshot"
    assert any("UPDATE qc_surface_phase_run" in query for query in connection.fetchrow_queries)


@pytest.mark.asyncio
async def test_postgres_repeat_read_reconciles_ordered_segments_and_manifest() -> None:
    """若恢复路径退回单图，分类会静默丢掉后续长页分段。"""

    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    recovered = {
        "status": "running",
        "terminal_reason_code": None,
        "supplemental_capture_id": "supplemental-capture",
        "screenshot_artifact_ids": '["segment-0", "segment-1"]',
        "manifest_artifact_id": "segment-manifest",
    }
    completed = {
        **recovered,
        "status": "completed",
        "terminal_reason_code": "WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
    }
    connection = _TerminalConnection([recovered, {"phase_run_id": "phase-1"}, completed])

    result = await PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(connection)
    ).read_terminal(command=_command())

    assert result is not None
    assert result.screenshot_artifact_ids == ("segment-0", "segment-1")
    assert result.manifest_artifact_id == "segment-manifest"
    finish_query = connection.fetchrow_queries[1]
    assert "page_coverage" in finish_query
    assert "web-scrolled-viewport-manifest.v1" in finish_query
    assert "scroll_offset_viewport" in finish_query


@pytest.mark.asyncio
async def test_postgres_finish_fails_closed_when_phase_update_matches_no_row() -> None:
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    connection = _TerminalConnection([None])
    repository = PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(connection)
    )

    with pytest.raises(RuntimeError, match="WEB_FULL_PAGE_SUPPLEMENTAL_FINISH_SCOPE_INVALID"):
        await repository.finish(
            phase_id="phase-1",
            status="failed",
            reason_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
            artifact_ids=(),
        )


@pytest.mark.asyncio
async def test_postgres_supplemental_child_queries_bind_phase_epoch_and_sequence() -> None:
    """完成与恢复都必须拒绝借用同会话旧 epoch/sequence 的补图子节点。"""

    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    finish_connection = _TerminalConnection([{"phase_run_id": "phase-1"}])
    await PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(finish_connection)
    ).finish(
        phase_id="phase-1",
        status="completed",
        reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
        artifact_ids=("supplemental-screenshot",),
    )
    finish_sql = " ".join(finish_connection.fetchrow_queries[0].split())

    read_connection = _TerminalConnection(
        [
            {
                "status": "failed",
                "terminal_reason_code": "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
                "supplemental_capture_id": None,
                "screenshot_artifact_id": None,
            }
        ]
    )
    await PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(read_connection)
    ).read_terminal(command=_command())
    read_sql = " ".join(read_connection.fetchrow_queries[0].split())

    assert "min(child.id)" not in read_sql.lower()
    assert "min(artifact.id)" not in read_sql.lower()
    assert "valid_candidates AS" in read_sql
    assert "(SELECT count(*) FROM valid_candidates)=1" in read_sql
    assert "artifact.metadata->>'purpose'='web_direct_full_page'" in finish_sql
    assert "artifact.metadata->>'purpose'='web_direct_full_page'" in read_sql
    assert "artifact.metadata->>'purpose' IS NULL" in read_sql

    for sql in (finish_sql, read_sql):
        assert "source_page.session_command_sequence=source.session_command_sequence" in sql
        assert "child.session_command_sequence=phase.command_sequence" in sql
        assert "page.session_command_sequence=phase.command_sequence" in sql
        assert "source.metadata->>'finalization_epoch'=phase.finalization_epoch::text" in sql
        assert "source_page.metadata->>'finalization_epoch'=phase.finalization_epoch::text" in sql
        assert "child.metadata->>'finalization_epoch'=phase.finalization_epoch::text" in sql
        assert "page.metadata->>'finalization_epoch'=phase.finalization_epoch::text" in sql
        for alias in ("source", "source_page", "child", "page"):
            assert f"jsonb_typeof({alias}.metadata->'finalization_epoch')='number'" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "reason_code"),
    [
        (TimeoutError(), "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"),
        (
            RuntimeError("Screenshot is too large: maximum pixels exceeded"),
            "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED",
        ),
    ],
)
async def test_browser_errors_close_claim_without_persisting(error, reason_code) -> None:
    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    browser.error = error
    result = await _service(browser=browser, persistence=persistence, claims=claims).capture(
        _command()
    )

    assert result.status == "failed"
    assert result.reason_code == reason_code
    assert persistence.calls == []
    assert claims.finished[0]["reason_code"] == reason_code


@pytest.mark.asyncio
@pytest.mark.parametrize("finish_fails", (False, True))
async def test_cancellation_closes_full_page_claim_then_preserves_original_cancel(
    finish_fails: bool,
) -> None:
    browser, claims = _Browser(), _Claims()
    browser.error = asyncio.CancelledError()
    if finish_fails:
        claims.finish_error = RuntimeError("finish unavailable")

    with pytest.raises(asyncio.CancelledError):
        await _service(browser=browser, claims=claims).capture(_command())

    assert claims.finished == [
        {
            "phase_id": "phase-1",
            "status": "failed",
            "reason_code": "WEB_FULL_PAGE_SCREENSHOT_CANCELLED",
            "artifact_ids": (),
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "screenshot",
    (
        pytest.param(b"not-a-png", id="invalid_png"),
        pytest.param(_png(width=1919, height=2160), id="width_contract"),
        pytest.param(_png(width=1920, height=1079), id="height_contract"),
        pytest.param(_png(width=32769), id="dimension_limit"),
        pytest.param(_png(ancillary_bytes=16 * 1024 * 1024 + 1), id="file_size_limit"),
    ),
)
async def test_invalid_or_over_limit_actual_png_closes_claim_before_persisting(
    screenshot: bytes,
) -> None:
    """移除 PNG 字节、尺寸、像素或文件上限校验会错误落盘不可用长图。"""
    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    browser.result = _result(screenshot=screenshot)

    result = await _service(browser=browser, persistence=persistence, claims=claims).capture(
        _command()
    )

    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
    assert persistence.calls == []
    assert claims.finished[0]["reason_code"] == "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"


@pytest.mark.asyncio
async def test_playwright_timeout_error_closes_claim_as_timeout() -> None:
    """移除真实 Playwright 超时类型识别会把浏览器超时误报为状态漂移。"""
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    browser.error = PlaywrightTimeoutError("Page.screenshot: Timeout 30000ms exceeded.")

    result = await _service(browser=browser, persistence=persistence, claims=claims).capture(
        _command()
    )

    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert persistence.calls == []
    assert claims.finished[0]["reason_code"] == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"


@pytest.mark.asyncio
async def test_expired_deadline_closes_claim_as_timeout_without_browsing() -> None:
    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    command = _command()
    object.__setattr__(command, "deadline", datetime.now(UTC) - timedelta(seconds=1))

    result = await _service(browser=browser, persistence=persistence, claims=claims).capture(
        command
    )

    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"
    assert browser.requests == []
    assert persistence.calls == []
    assert claims.finished[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_persistence_failure_closes_claim() -> None:
    persistence, claims = _Persistence(), _Claims()
    persistence.error = RuntimeError("storage unavailable")
    result = await _service(persistence=persistence, claims=claims).capture(_command())

    assert result.status == "failed"
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED"
    assert claims.finished[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_consumed_claim_skips_browser_and_persistence() -> None:
    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    claims.claimed = False
    result = await _service(browser=browser, persistence=persistence, claims=claims).capture(
        _command()
    )

    assert result.status == "already_attempted"
    assert browser.requests == []
    assert persistence.calls == []


@pytest.mark.asyncio
async def test_rejects_non_web_session_without_browsing() -> None:
    browser = _Browser()
    command = _command()
    object.__setattr__(command, "session_id", "h5:run-1")
    result = await _service(browser=browser).capture(command)

    assert result.status == "failed"
    assert browser.requests == []


@pytest.mark.asyncio
async def test_rejects_source_capture_that_is_not_current_without_browsing() -> None:
    browser, claims = _Browser(), _Claims()
    result = await _service(
        browser=browser, claims=claims, gate=_Gate(current_capture_id="newer-capture")
    ).capture(_command())

    assert result.status == "failed"
    assert result.reason_code == "WEB_FULL_PAGE_SCREENSHOT_STATE_DRIFT"
    assert browser.requests == []
    assert claims.finished[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_ready_reader_attaches_supplemental_without_replacing_primary_capture() -> None:
    """补图若替换主 Capture，将破坏当前导航指针和主图的可点击性证据。"""

    import services.api.app.application.web_full_page_supplemental_capture as module
    from services.api.app.application.web_page_classification import (
        WebPageStructureFacts,
        WebReadyPageCapture,
    )

    class Delegate:
        async def read(self, *, capture_id, page_role):
            return WebReadyPageCapture(
                capture_id=capture_id,
                web_capture_stage="web_page_ready_baseline",
                screenshot_artifact_id="primary-screenshot",
                dom_artifact_id="primary-dom",
                visible_text_artifact_id="primary-visible-text",
                page_role=page_role,
                url="https://example.test/catalog",
                structure_facts=WebPageStructureFacts(
                    navigation_element_count=1,
                    repeated_link_count=3,
                    video_element_count=0,
                    single_subject_element_count=0,
                    dom_tokens=("content-list",),
                ),
            )

        async def read_current(self, *, page_role):
            return await self.read(capture_id="primary-capture", page_role=page_role)

    class SupplementalService:
        def __init__(self) -> None:
            self.commands = []

        async def capture(self, command):
            self.commands.append(command)
            return module.WebFullPageSupplementalResult(
                status="captured",
                source_capture_id="primary-capture",
                supplemental_capture_id="supplemental-capture",
                screenshot_artifact_id="full-page-screenshot",
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            )

    service = SupplementalService()
    reader_type = module.WebFullPageSupplementingReadyCaptureReader
    reader = reader_type(
        delegate=Delegate(),
        supplemental_capture_service=service,
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    ready = await reader.read(capture_id="primary-capture", page_role="menu")

    assert ready.capture_id == "primary-capture"
    assert ready.screenshot_artifact_id == "primary-screenshot"
    assert ready.supplemental_screenshot_artifact_ids == ("full-page-screenshot",)
    assert ready.supplemental_evidence_status == "captured"
    assert service.commands[0].source_capture_id == "primary-capture"
    assert service.commands[0].source_url == "https://example.test/catalog"


@pytest.mark.asyncio
async def test_ready_reader_records_supplemental_failure_as_gap() -> None:
    """失败补图只能形成可审计 gap，不能降级或改写已就绪的主证据。"""

    import services.api.app.application.web_full_page_supplemental_capture as module
    from services.api.app.application.web_page_classification import (
        WebPageStructureFacts,
        WebReadyPageCapture,
    )

    class Delegate:
        async def read_current(self, *, page_role):
            return WebReadyPageCapture(
                capture_id="primary-capture",
                web_capture_stage="web_page_ready_baseline",
                screenshot_artifact_id="primary-screenshot",
                dom_artifact_id="primary-dom",
                visible_text_artifact_id="primary-visible-text",
                page_role=page_role,
                url="https://example.test/catalog",
                structure_facts=WebPageStructureFacts(
                    navigation_element_count=1,
                    repeated_link_count=3,
                    video_element_count=0,
                    single_subject_element_count=0,
                    dom_tokens=("content-list",),
                ),
            )

    class SupplementalService:
        async def capture(self, command):
            return module.WebFullPageSupplementalResult(
                status="failed",
                source_capture_id=command.source_capture_id,
                supplemental_capture_id=None,
                screenshot_artifact_id=None,
                reason_code="WEB_FULL_PAGE_SCREENSHOT_TIMEOUT",
            )

    reader_type = module.WebFullPageSupplementingReadyCaptureReader
    reader = reader_type(
        delegate=Delegate(),
        supplemental_capture_service=SupplementalService(),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    ready = await reader.read_current(page_role="menu")

    assert ready.capture_id == "primary-capture"
    assert ready.screenshot_artifact_id == "primary-screenshot"
    assert ready.supplemental_screenshot_artifact_ids == ()
    assert ready.supplemental_evidence_status == "failed"
    assert ready.supplemental_evidence_reason_code == "WEB_FULL_PAGE_SCREENSHOT_TIMEOUT"


def _ready_capture_for_supplemental_reader(*, page_role: str):
    from services.api.app.application.web_page_classification import (
        WebPageStructureFacts,
        WebReadyPageCapture,
    )

    return WebReadyPageCapture(
        capture_id="primary-capture",
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="primary-screenshot",
        dom_artifact_id="primary-dom",
        visible_text_artifact_id="primary-visible-text",
        page_role=page_role,
        url="https://example.test/catalog",
        structure_facts=WebPageStructureFacts(
            navigation_element_count=1,
            repeated_link_count=3,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("content-list",),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("production_path", "page_role"),
    [
        pytest.param("menu", "menu", id="menu"),
        pytest.param("detail", "detail", id="detail"),
        pytest.param("search", "menu", id="search"),
        pytest.param("activity", "menu", id="activity"),
        pytest.param("recovery", "menu", id="recovery"),
    ],
)
async def test_production_reader_repeat_restores_captured_terminal_for_every_web_path(
    production_path,
    page_role,
) -> None:
    """搜索、活动和恢复复用 menu role，但都必须恢复同一补图终态。"""

    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementalResult,
        WebFullPageSupplementingReadyCaptureReader,
    )

    class Delegate:
        async def read_current(self, *, page_role):
            return replace(
                _ready_capture_for_supplemental_reader(page_role=page_role),
                capture_id="source-capture",
                url="https://example.test/",
            )

    class Claims(_Claims):
        async def finish(self, **kwargs) -> None:
            await super().finish(**kwargs)
            if kwargs["status"] == "completed":
                self.terminal_result = WebFullPageSupplementalResult(
                    status="captured",
                    source_capture_id="source-capture",
                    supplemental_capture_id="supplemental-capture",
                    screenshot_artifact_id="supplemental-screenshot",
                    reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                )

    browser, persistence, claims = _Browser(), _Persistence(), Claims()
    reader = WebFullPageSupplementingReadyCaptureReader(
        delegate=Delegate(),
        supplemental_capture_service=_service(
            browser=browser,
            persistence=persistence,
            claims=claims,
        ),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    first = await reader.read_current(page_role=page_role)
    claims.claimed = False
    repeated = await reader.read_current(page_role=page_role)

    assert first.supplemental_screenshot_artifact_ids == ("supplemental-screenshot",), (
        production_path
    )
    assert repeated == first
    assert len(browser.requests) == 1
    assert len(persistence.calls) == 1


@pytest.mark.asyncio
async def test_ready_reader_degrades_supplemental_runtime_error_to_stable_gap() -> None:
    """session gate 等补图基础设施故障不能中断已就绪主 Capture 的分类。"""

    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementingReadyCaptureReader,
    )

    class Delegate:
        async def read_current(self, *, page_role):
            return _ready_capture_for_supplemental_reader(page_role=page_role)

    class SupplementalService:
        async def capture(self, command):
            raise RuntimeError("session gate unavailable")

    ready = await WebFullPageSupplementingReadyCaptureReader(
        delegate=Delegate(),
        supplemental_capture_service=SupplementalService(),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    ).read_current(page_role="menu")

    assert ready.capture_id == "primary-capture"
    assert ready.screenshot_artifact_id == "primary-screenshot"
    assert ready.supplemental_screenshot_artifact_ids == ()
    assert ready.supplemental_evidence_status == "failed"
    assert ready.supplemental_evidence_reason_code == "WEB_FULL_PAGE_SCREENSHOT_CLAIM_REJECTED"


@pytest.mark.asyncio
async def test_ready_reader_propagates_supplemental_cancellation() -> None:
    """吞掉协程取消会让探索任务在已终止后继续运行。"""

    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementingReadyCaptureReader,
    )

    class Delegate:
        async def read_current(self, *, page_role):
            return _ready_capture_for_supplemental_reader(page_role=page_role)

    class SupplementalService:
        async def capture(self, command):
            raise asyncio.CancelledError()

    reader = WebFullPageSupplementingReadyCaptureReader(
        delegate=Delegate(),
        supplemental_capture_service=SupplementalService(),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    with pytest.raises(asyncio.CancelledError):
        await reader.read_current(page_role="menu")


@pytest.mark.asyncio
async def test_invalid_dimensions_degrade_across_service_reader_and_loader() -> None:
    from services.api.app.application.web_assessment_evidence import (
        StoredWebAssessmentArtifact,
        VerifiedWebAssessmentEvidenceLoader,
    )
    from services.api.app.application.web_full_page_supplemental_capture import (
        WebFullPageSupplementingReadyCaptureReader,
    )

    browser, persistence, claims = _Browser(), _Persistence(), _Claims()
    browser.result = _result(screenshot=_png(width=1919, height=2160))

    class StableDelegate:
        async def read_current(self, *, page_role):
            return replace(
                _ready_capture_for_supplemental_reader(page_role=page_role),
                capture_id="source-capture",
                url="https://example.test/",
            )

    ready = await WebFullPageSupplementingReadyCaptureReader(
        delegate=StableDelegate(),
        supplemental_capture_service=_service(
            browser=browser,
            persistence=persistence,
            claims=claims,
        ),
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=1),
    ).read_current(page_role="menu")

    viewport = _png()
    dom = b"<main>primary remains classifiable</main>"

    def stored(artifact_id, artifact_type, payload, content_type):
        return StoredWebAssessmentArtifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            task_id="task-1",
            capture_id="source-capture",
            storage_path=f"memory://{artifact_id}",
            content_type=content_type,
            checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
            size_bytes=len(payload),
        )

    artifacts = (
        stored("primary-screenshot", "screenshot", viewport, "image/png"),
        stored("primary-dom", "dom", dom, "text/html"),
    )

    class MetadataReader:
        async def read_many(self, *, artifact_ids):
            return tuple(item for item in artifacts if item.artifact_id in artifact_ids)

    class ArtifactStore:
        async def read(self, storage_path):
            return {
                "memory://primary-screenshot": viewport,
                "memory://primary-dom": dom,
            }[storage_path]

    evidence = await VerifiedWebAssessmentEvidenceLoader(
        metadata_reader=MetadataReader(),
        artifact_store=ArtifactStore(),
    ).load(
        screenshot_artifact_id=ready.screenshot_artifact_id,
        dom_artifact_id=ready.dom_artifact_id,
        supplemental_screenshot_artifact_ids=ready.supplemental_screenshot_artifact_ids,
    )

    assert ready.supplemental_evidence_status == "failed"
    assert ready.supplemental_screenshot_artifact_ids == ()
    assert evidence.screenshot_bytes == viewport
    assert "primary remains classifiable" in evidence.dom_text
    assert persistence.calls == []


async def test_checkpoint_full_page_finish_cannot_use_expired_stage_token():
    from services.api.app.domain.exploration.web_run_stage_checkpoint import WebRunStage
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )
    from services.api.tests.test_web_runtime_stage_effects import cursor

    class Connection:
        async def fetchrow(self, sql, *args):
            assert "checkpoint.claim_token" in sql and "clock_timestamp()" in sql
            return None

    repository = PostgresWebFullPageSupplementalClaimRepository(
        pool=_ClaimRecordingPool(Connection())
    )
    with pytest.raises(RuntimeError, match="FINISH"):
        await repository.finish(
            phase_id="phase",
            status="failed",
            reason_code="TIMEOUT",
            artifact_ids=(),
            stage_cursor=cursor(WebRunStage.FULL_PAGE_CAPTURE),
        )
