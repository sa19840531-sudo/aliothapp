"""由图片签名驱动的确定性 App Screenshot Understanding Provider。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from services.api.app.application.app_screenshot_understanding import (
    UnderstandAppScreenshotRequest,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.exploration.app_visual import APP_VISUAL_CONTROL_ROLES
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.interaction.validation import optional_text, require_text


@dataclass(frozen=True, kw_only=True)
class FakeAppVisionCandidate:
    """Fake 场景中的语义候选模板，身份由请求 Capture 补齐。"""

    semantic_name: str
    action_type: InteractionOperation
    normalized_bounds: NormalizedBounds | None
    confidence: float
    risk_level: InteractionRiskLevel
    purpose: str
    control_role: str | None = None
    expected_state_hint: str | None = None
    frontier_scope: str = "local"

    def __post_init__(self) -> None:
        require_text(self.semantic_name, "semantic_name")
        require_text(self.purpose, "purpose")
        optional_text(self.control_role, "control_role")
        optional_text(self.expected_state_hint, "expected_state_hint")
        if self.control_role is not None and self.control_role not in APP_VISUAL_CONTROL_ROLES:
            raise ValueError("control_role 不受支持")
        if self.frontier_scope not in {"local", "page", "app"}:
            raise ValueError("frontier_scope 不受支持")
        if not isinstance(self.action_type, InteractionOperation):
            raise ValueError("action_type 不受支持")
        if self.normalized_bounds is not None and not isinstance(
            self.normalized_bounds,
            NormalizedBounds,
        ):
            raise ValueError("normalized_bounds 类型不正确")
        if not isinstance(self.risk_level, InteractionRiskLevel):
            raise ValueError("risk_level 不受支持")


@dataclass(frozen=True, kw_only=True)
class FakeAppVisionScene:
    """一个截图签名对应的确定性视觉结果。"""

    visual_page_type: str | None
    visual_summary: str
    page_state: str = "unknown"
    overlay_signature: str | None = None
    surface_facts: Mapping[str, Any] = field(default_factory=dict)
    key_elements: tuple[str, ...] = ()
    candidates: tuple[FakeAppVisionCandidate, ...] = ()

    def __post_init__(self) -> None:
        optional_text(self.visual_page_type, "visual_page_type")
        require_text(self.page_state, "page_state")
        optional_text(self.overlay_signature, "overlay_signature")
        if not isinstance(self.surface_facts, Mapping):
            raise ValueError("surface_facts 必须为对象")
        require_text(self.visual_summary, "visual_summary")
        key_elements = tuple(self.key_elements)
        for value in key_elements:
            require_text(value, "key_elements")
        candidates = tuple(self.candidates)
        if any(not isinstance(item, FakeAppVisionCandidate) for item in candidates):
            raise ValueError("candidates 类型不正确")
        object.__setattr__(self, "key_elements", key_elements)
        object.__setattr__(self, "candidates", candidates)


class FakeAppVisionProvider:
    """未配置图片签名时返回空 Frontier，绝不猜测候选。"""

    def __init__(self, *, scenes: Mapping[str, FakeAppVisionScene] | None = None) -> None:
        self._scenes = dict(scenes or {})
        if any(not isinstance(scene, FakeAppVisionScene) for scene in self._scenes.values()):
            raise ValueError("scenes 必须映射到 FakeAppVisionScene")

    async def understand(
        self,
        request: UnderstandAppScreenshotRequest,
    ) -> AppVisualAnalysis:
        if not isinstance(request, UnderstandAppScreenshotRequest):
            raise TypeError("request 必须为 UnderstandAppScreenshotRequest")
        scene = self._scenes.get(request.image_signature)
        if scene is None:
            return AppVisualAnalysis.build(
                capture_id=request.capture_id,
                screenshot_artifact_id=request.screenshot_artifact_id,
                image_signature=request.image_signature,
                width=request.width,
                height=request.height,
                orientation=request.orientation,
                visual_page_type=None,
                visual_summary="未配置视觉场景",
                key_elements=(),
                candidates=(),
                provider_id="fake-app-vision",
                model_id="deterministic-v1",
                package_name=request.package_name,
                activity=request.activity,
                page_state="unknown",
            )
        candidates = tuple(
            AppVisualCandidate.build(
                source_capture_id=request.capture_id,
                source_image_signature=request.image_signature,
                source_width=request.width,
                source_height=request.height,
                source_orientation=request.orientation,
                semantic_name=item.semantic_name,
                action_type=item.action_type,
                normalized_bounds=item.normalized_bounds,
                confidence=item.confidence,
                risk_level=item.risk_level,
                purpose=item.purpose,
                control_role=item.control_role,
                expected_state_hint=item.expected_state_hint,
                frontier_scope=item.frontier_scope,
            )
            for item in scene.candidates
            if item.action_type in request.allowed_actions
        )[: request.max_candidates]
        return AppVisualAnalysis.build(
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            width=request.width,
            height=request.height,
            orientation=request.orientation,
            visual_page_type=scene.visual_page_type,
            visual_summary=scene.visual_summary,
            key_elements=scene.key_elements,
            candidates=candidates,
            provider_id="fake-app-vision",
            model_id="deterministic-v1",
            package_name=request.package_name,
            activity=request.activity,
            page_state=scene.page_state,
            overlay_signature=scene.overlay_signature,
            surface_facts=scene.surface_facts,
        )
