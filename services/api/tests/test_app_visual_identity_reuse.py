"""App 视觉身份复用的真实 Capture 边界。"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.app_capture_persistence import PersistedAppCapture
from services.api.app.application.app_screenshot_exploration import (
    ensure_app_capture_visual_observation,
)
from services.api.app.application.app_screenshot_understanding import (
    AppScreenshotCaptureContext,
    AppScreenshotUnderstandingError,
    AppScreenshotUnderstandingService,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.exploration.app_visual_page_identity import (
    build_app_visual_page_facts,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    SavedAppVisualObservation,
)

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)
SIGNATURE = f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}"
NOW = datetime(2026, 9, 7, 10, 0, tzinfo=UTC)


def _anchor(text: str, *, top: float, kind: str = "label") -> dict[str, Any]:
    return {
        "text": text,
        "kind": kind,
        "confidence": 0.95,
        "normalized_bounds": {
            "left": 0.1,
            "top": top,
            "right": 0.5,
            "bottom": top + 0.06,
        },
    }


def _identity_observation() -> dict[str, Any]:
    return {
        "page_role": "collection",
        "confidence": 0.94,
        "location": _anchor("作品目录", top=0.1),
        "function": _anchor("搜索作品", top=0.3),
        "return_semantics": "back",
        "return_control": _anchor("返回", top=0.01, kind="icon"),
        "business_object": [],
    }


def _capture(
    capture_id: str,
    artifact_id: str,
    *,
    role: str = "bootstrap",
) -> CaptureRef:
    return CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id=capture_id,
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role=role,
        artifact_ids=(artifact_id,),
        parent_capture_id="source-capture" if role != "bootstrap" else None,
        action_execution_id="action-1" if role != "bootstrap" else None,
    )


def _analysis(
    capture: CaptureRef,
    *,
    artifact_id: str,
    image_signature: str = SIGNATURE,
    package_name: str = "com.example.target",
    activity: str = ".MainActivity",
    with_identity: bool = True,
    extra_surface_facts: dict[str, Any] | None = None,
) -> AppVisualAnalysis:
    facts: dict[str, Any] = {}
    if with_identity:
        facts = build_app_visual_page_facts(
            identity_observation=_identity_observation(),
            local_state_observation={"page_index": [_anchor("第1页", top=0.5)]},
            capture_id=capture.capture_id,
            screenshot_artifact_id=artifact_id,
            image_signature=image_signature,
            package_name=package_name,
            activity=activity,
            page_state="stable",
            overlay_signature=None,
            surface_facts={},
        )
    if extra_surface_facts:
        facts = {**facts, **extra_surface_facts}
    candidate = AppVisualCandidate.build(
        source_capture_id=capture.capture_id,
        source_image_signature=image_signature,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="搜索作品",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.1, 0.3, 0.5, 0.36),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="搜索作品",
    )
    return AppVisualAnalysis.build(
        capture_id=capture.capture_id,
        screenshot_artifact_id=artifact_id,
        image_signature=image_signature,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="content_list",
        visual_summary="作品目录",
        key_elements=("作品目录",),
        candidates=(candidate,),
        provider_id="fake-provider",
        model_id="fake-model",
        package_name=package_name,
        activity=activity,
        page_state="stable",
        surface_facts=facts,
    )


class _Observations:
    def __init__(self) -> None:
        self.saved: dict[str, SavedAppVisualObservation] = {}

    async def get_by_capture_id(self, *, capture_id: str):
        return self.saved.get(capture_id)

    async def save(self, analysis: AppVisualAnalysis) -> SavedAppVisualObservation:
        saved = SavedAppVisualObservation(
            observation_id=f"observation-{analysis.capture_id}",
            analysis=analysis,
            observed_at=NOW,
            created=True,
        )
        self.saved[analysis.capture_id] = saved
        return saved


class _ContextUnderstanding:
    def __init__(
        self,
        context: AppScreenshotCaptureContext,
        understood: AppVisualAnalysis,
    ) -> None:
        self.context = context
        self.understood = understood
        self.understand_calls: list[str] = []

    async def resolve_capture_visual_context(self, capture: CaptureRef):
        return self.context

    async def understand(self, capture: CaptureRef) -> AppVisualAnalysis:
        self.understand_calls.append(capture.capture_id)
        return self.understood


class _NoContextUnderstanding:
    def __init__(self, understood: AppVisualAnalysis) -> None:
        self.understood = understood
        self.understand_calls: list[str] = []

    async def understand(self, capture: CaptureRef) -> AppVisualAnalysis:
        self.understand_calls.append(capture.capture_id)
        return self.understood


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["before_action", "after_action"])
async def test_same_frame_identity_reuse_rebuilds_before_and_after_binding(role: str) -> None:
    source_capture = _capture("source-capture", "source-artifact")
    target_capture = _capture(f"target-{role}", f"target-{role}-artifact", role=role)
    source = _analysis(source_capture, artifact_id="source-artifact")
    target_context = AppScreenshotCaptureContext(
        capture=target_capture,
        screenshot_artifact_id=target_capture.artifact_ids[0],
        image_signature=source.image_signature,
        width=source.width,
        height=source.height,
        orientation=source.orientation,
        package_name=source.package_name,
        activity=source.activity,
    )
    understanding = _ContextUnderstanding(
        target_context,
        _analysis(target_capture, artifact_id=target_capture.artifact_ids[0]),
    )

    saved = await ensure_app_capture_visual_observation(
        capture=target_capture,
        understanding=understanding,
        observations=_Observations(),
        mismatch_message="visual mismatch",
        reuse_from=source,
        reuse_reason="same_image_signature",
    )

    analysis = saved.analysis
    assert understanding.understand_calls == []
    assert analysis.capture_id == target_capture.capture_id
    assert analysis.screenshot_artifact_id == target_capture.artifact_ids[0]
    assert analysis.surface_facts["vision_reuse"]["source_capture_id"] == source_capture.capture_id
    identity = analysis.surface_facts["app_page_identity"]
    assert identity["capture_id"] == target_capture.capture_id
    assert identity["screenshot_artifact_id"] == target_capture.artifact_ids[0]
    assert identity["image_signature"] == source.image_signature
    local_state = analysis.surface_facts["app_local_state_observation"]
    assert local_state["capture_id"] == target_capture.capture_id
    assert local_state["screenshot_artifact_id"] == target_capture.artifact_ids[0]
    assert analysis.candidates[0].source_capture_id == target_capture.capture_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("package_name", "activity"),
    [
        ("com.other.target", ".MainActivity"),
        ("com.example.target", ".OtherActivity"),
    ],
)
async def test_package_or_activity_mismatch_does_not_reuse_identity(
    package_name: str,
    activity: str,
) -> None:
    source_capture = _capture("source-capture", "source-artifact")
    target_capture = _capture("target-capture", "target-artifact", role="before_action")
    source = _analysis(source_capture, artifact_id="source-artifact")
    mismatched_context = AppScreenshotCaptureContext(
        capture=target_capture,
        screenshot_artifact_id="target-artifact",
        image_signature=source.image_signature,
        width=source.width,
        height=source.height,
        orientation=source.orientation,
        package_name=package_name,
        activity=activity,
    )
    understood = _analysis(
        target_capture,
        artifact_id="target-artifact",
        package_name=package_name,
        activity=activity,
    )
    understanding = _ContextUnderstanding(mismatched_context, understood)
    observations = _Observations()

    saved = await ensure_app_capture_visual_observation(
        capture=target_capture,
        understanding=understanding,
        observations=observations,
        mismatch_message="visual mismatch",
        reuse_from=source,
        reuse_reason="same_image_signature",
    )

    assert understanding.understand_calls == [target_capture.capture_id]
    assert "vision_reuse" not in saved.analysis.surface_facts
    assert saved.analysis.package_name == package_name
    assert saved.analysis.activity == activity


@pytest.mark.asyncio
async def test_identity_source_without_context_reader_reunderstands() -> None:
    source_capture = _capture("source-capture", "source-artifact")
    target_capture = _capture("target-capture", "target-artifact", role="before_action")
    source = _analysis(source_capture, artifact_id="source-artifact")
    understood = _analysis(target_capture, artifact_id="target-artifact")
    understanding = _NoContextUnderstanding(understood)

    saved = await ensure_app_capture_visual_observation(
        capture=target_capture,
        understanding=understanding,
        observations=_Observations(),
        mismatch_message="visual mismatch",
        reuse_from=source,
        reuse_reason="same_image_signature",
    )

    assert understanding.understand_calls == [target_capture.capture_id]
    assert "vision_reuse" not in saved.analysis.surface_facts


@pytest.mark.asyncio
@pytest.mark.parametrize("with_context", [False, True])
async def test_visual_binding_is_checked_before_observation_save(with_context: bool) -> None:
    source_capture = _capture("source-capture", "source-artifact")
    target_capture = _capture("target-capture", "target-artifact", role="after_action")
    source = _analysis(source_capture, artifact_id="source-artifact")
    if with_context:
        context = AppScreenshotCaptureContext(
            capture=target_capture,
            screenshot_artifact_id="target-artifact",
            image_signature=source.image_signature,
            width=source.width,
            height=source.height,
            orientation=source.orientation,
            package_name="com.other.target",
            activity=source.activity,
        )
        # 当前上下文已变，重新理解却返回旧包名；必须拒绝写入。
        understanding = _ContextUnderstanding(
            context, _analysis(target_capture, artifact_id="target-artifact")
        )
    else:
        # 无复用能力的理解器返回了别的 Capture，也不得先落库再拒绝。
        understanding = _NoContextUnderstanding(source)
    observations = _Observations()
    with pytest.raises(ValueError, match="visual mismatch"):
        await ensure_app_capture_visual_observation(
            capture=target_capture,
            understanding=understanding,
            observations=observations,
            mismatch_message="visual mismatch",
            reuse_from=source if with_context else None,
            reuse_reason="same_image_signature" if with_context else None,
        )
    assert understanding.understand_calls == [target_capture.capture_id]
    assert observations.saved == {}


class _SourceRepository:
    def __init__(self, source: PersistedAppCapture) -> None:
        self.source = source

    async def load(self, *, capture: CaptureRef) -> PersistedAppCapture:
        return self.source


class _ArtifactReader:
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def read(self, storage_path: str) -> bytes:
        del storage_path
        return self.content


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def understand(self, request: object) -> AppVisualAnalysis:
        self.calls.append(request)
        raise AssertionError("拒绝的 Capture 不应调用视觉 Provider")


def _persisted_capture(
    *,
    capture: CaptureRef,
    width: int = 1,
    height: int = 1,
) -> PersistedAppCapture:
    checksum = f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}"
    return PersistedAppCapture(
        capture=capture,
        agent_run_id="run-1",
        screenshot=PersistedEvidenceArtifact(
            artifact_id=capture.artifact_ids[0],
            artifact_type="screenshot",
            storage_path="capture.png",
            content_type="image/png",
            checksum=checksum,
            size_bytes=len(PNG_1X1),
            metadata={"encoding": "binary"},
        ),
        width=width,
        height=height,
        orientation="portrait",
        image_signature=checksum,
        package_name="com.example.target",
        activity=".MainActivity",
        bootstrap_row_version=1 if capture.role == "bootstrap" else None,
    )


@pytest.mark.asyncio
async def test_real_reuse_context_rejects_hash_or_capture_ownership_error() -> None:
    capture = _capture("capture-1", "artifact-1")
    source = _persisted_capture(capture=capture)
    service = AppScreenshotUnderstandingService(
        sources=_SourceRepository(source),
        artifacts=_ArtifactReader(b"not-a-png"),
        provider=object(),
    )
    with pytest.raises(AppScreenshotUnderstandingError) as hash_error:
        await service.resolve_capture_visual_context(capture)
    assert hash_error.value.error_code == "SCREENSHOT_CHECKSUM_MISMATCH"

    foreign_capture = replace(capture, capture_id="foreign-capture")
    service = AppScreenshotUnderstandingService(
        sources=_SourceRepository(source),
        artifacts=_ArtifactReader(PNG_1X1),
        provider=object(),
    )
    with pytest.raises(AppScreenshotUnderstandingError) as ownership_error:
        await service.resolve_capture_visual_context(foreign_capture)
    assert ownership_error.value.error_code == "SCREENSHOT_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_real_reuse_context_rejects_persisted_dimensions_not_matching_png() -> None:
    capture = _capture("capture-1", "artifact-1")
    source = _persisted_capture(capture=capture, width=2)
    service = AppScreenshotUnderstandingService(
        sources=_SourceRepository(source),
        artifacts=_ArtifactReader(PNG_1X1),
        provider=object(),
    )

    with pytest.raises(AppScreenshotUnderstandingError) as error:
        await service.resolve_capture_visual_context(capture)
    assert error.value.error_code == "SCREENSHOT_CHECKSUM_MISMATCH"


@pytest.mark.asyncio
async def test_workflow_does_not_understand_or_save_after_real_context_rejection() -> None:
    source_capture = _capture("source-capture", "source-artifact")
    target_capture = _capture("target-capture", "target-artifact", role="before_action")
    source = _persisted_capture(capture=source_capture)
    provider = _RecordingProvider()
    service = AppScreenshotUnderstandingService(
        sources=_SourceRepository(source),
        artifacts=_ArtifactReader(PNG_1X1),
        provider=provider,
    )
    observations = _Observations()

    with pytest.raises(AppScreenshotUnderstandingError) as error:
        await ensure_app_capture_visual_observation(
            capture=target_capture,
            understanding=service,
            observations=observations,
            mismatch_message="visual mismatch",
            reuse_from=_analysis(source_capture, artifact_id="source-artifact"),
            reuse_reason="same_image_signature",
        )

    assert error.value.error_code == "SCREENSHOT_ARTIFACT_MISSING"
    assert provider.calls == []
    assert observations.saved == {}


@pytest.mark.asyncio
async def test_business_content_fingerprint_reuses_when_only_status_bar_differs() -> None:
    fingerprint = "sha256:" + "e" * 64
    source_capture = _capture("source-capture", "source-artifact")
    target_capture = _capture("target-capture", "target-artifact", role="before_action")
    source = _analysis(
        source_capture,
        artifact_id="source-artifact",
        extra_surface_facts={
            "content_fingerprint": {
                "protocol_version": "app-business-content-fingerprint.v1",
                "value": fingerprint,
            }
        },
    )
    target_signature = "sha256:" + "f" * 64
    target_context = AppScreenshotCaptureContext(
        capture=target_capture,
        screenshot_artifact_id="target-artifact",
        image_signature=target_signature,
        width=source.width,
        height=source.height,
        orientation=source.orientation,
        package_name=source.package_name,
        activity=source.activity,
        content_fingerprint=fingerprint,
    )
    understood = _analysis(
        target_capture,
        artifact_id="target-artifact",
        image_signature=target_signature,
    )
    understanding = _ContextUnderstanding(target_context, understood)

    saved = await ensure_app_capture_visual_observation(
        capture=target_capture,
        understanding=understanding,
        observations=_Observations(),
        mismatch_message="visual mismatch",
        reuse_from=source,
        reuse_reason="same_image_signature",
    )

    assert understanding.understand_calls == []
    assert saved.analysis.capture_id == target_capture.capture_id
    assert saved.analysis.image_signature == target_signature
    assert saved.analysis.surface_facts["vision_reuse"]["reason"] == (
        "same_business_content_fingerprint"
    )
    assert saved.analysis.surface_facts["content_fingerprint"]["value"] == fingerprint
