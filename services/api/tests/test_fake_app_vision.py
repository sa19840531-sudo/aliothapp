from __future__ import annotations

import hashlib

import pytest

from services.api.app.application.app_screenshot_understanding import (
    UnderstandAppScreenshotRequest,
)
from services.api.app.domain.exploration import NormalizedBounds
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.app.infrastructure.app_vision.fake import (
    FakeAppVisionCandidate,
    FakeAppVisionProvider,
    FakeAppVisionScene,
)

PNG = b"deterministic-png"
SIGNATURE = f"sha256:{hashlib.sha256(PNG).hexdigest()}"


def _request() -> UnderstandAppScreenshotRequest:
    return UnderstandAppScreenshotRequest(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        screenshot_artifact_id="artifact-1",
        screenshot_png=PNG,
        image_signature=SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.target",
        activity=".MainActivity",
        allowed_actions=(InteractionOperation.CLICK,),
        max_candidates=5,
    )


@pytest.mark.asyncio
async def test_fake_provider_builds_candidate_bound_to_request_capture() -> None:
    provider = FakeAppVisionProvider(
        scenes={
            SIGNATURE: FakeAppVisionScene(
                visual_page_type="login",
                visual_summary="登录页",
                key_elements=("登录按钮",),
                candidates=(
                    FakeAppVisionCandidate(
                        semantic_name="登录按钮",
                        action_type=InteractionOperation.CLICK,
                        normalized_bounds=NormalizedBounds(0.2, 0.7, 0.8, 0.8),
                        confidence=0.98,
                        risk_level=InteractionRiskLevel.LOW,
                        purpose="进入系统",
                        expected_state_hint="首页",
                        frontier_scope="app",
                    ),
                ),
            )
        }
    )

    analysis = await provider.understand(_request())

    assert analysis.capture_id == "capture-1"
    assert analysis.candidates[0].source_capture_id == "capture-1"
    assert analysis.candidates[0].normalized_bounds == NormalizedBounds(0.2, 0.7, 0.8, 0.8)
    assert analysis.candidates[0].frontier_scope == "app"


@pytest.mark.asyncio
async def test_fake_provider_propagates_scene_surface_phase_facts() -> None:
    provider = FakeAppVisionProvider(
        scenes={
            SIGNATURE: FakeAppVisionScene(
                visual_page_type="home",
                visual_summary="首页被弹窗遮挡",
                page_state="popup",
                overlay_signature="overlay:popup-1",
                surface_facts={
                    "popup_visible": True,
                    "close_control_visible": True,
                },
            )
        }
    )

    analysis = await provider.understand(_request())

    assert analysis.page_state == "popup"
    assert analysis.overlay_signature == "overlay:popup-1"
    assert analysis.surface_facts == {
        "popup_visible": True,
        "close_control_visible": True,
    }


@pytest.mark.asyncio
async def test_fake_provider_does_not_guess_candidates_for_unknown_image() -> None:
    analysis = await FakeAppVisionProvider().understand(_request())

    assert analysis.visual_page_type is None
    assert analysis.visual_summary == "未配置视觉场景"
    assert analysis.page_state == "unknown"
    assert analysis.overlay_signature is None
    assert analysis.surface_facts == {}
    assert analysis.candidates == ()
