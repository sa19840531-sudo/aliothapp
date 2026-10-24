"""App Screenshot Artifact 的可信读取与视觉理解边界。"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from services.api.app.application.app_age_confirm_visual import (
    analysis_has_age_gate_evidence,
    build_age_confirm_visual_fact,
    resolve_age_confirm_visual_bounds,
)
from services.api.app.application.app_capture_persistence import (
    PersistedAppCapture,
    VerifiedAppUiTree,
    _read_png_dimensions,
)
from services.api.app.application.app_navigation_entry_provenance import (
    DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    DISCOVERY_SOURCE_UIA_ALIGNED,
    DISCOVERY_SOURCE_UIA_ISSUED,
    DISCOVERY_SOURCE_VISION,
    with_visual_candidate_discovery_source,
)
from services.api.app.application.app_surface_evidence import (
    find_trusted_obstruction_dismiss_targets,
    find_trusted_post_publish_choice_target,
    find_trusted_post_publish_entry_target,
    find_unique_trusted_action_target_by_label,
    find_unique_trusted_age_confirm_target,
    find_unique_trusted_navigation_target_by_label,
    find_unique_trusted_skip_target,
    infer_hierarchy_screen_size,
    inspect_trusted_app_accessibility_controls,
    resolve_ui_tree_screen_size,
)
from services.api.app.application.app_visual_bottom_nav_calibration import (
    calibrate_bottom_navigation_candidates,
)
from services.api.app.application.app_visual_close_calibration import (
    calibrate_popup_close_candidates,
)
from services.api.app.application.app_visual_content_fingerprint import (
    compute_app_business_content_fingerprint,
)
from services.api.app.application.app_visual_home_top_category_calibration import (
    calibrate_home_top_category_candidates,
)
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    AppVisualValidationError,
    NormalizedBounds,
)
from services.api.app.domain.exploration.age_confirm import is_age_confirm_candidate
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.interaction.validation import optional_text, require_text
from services.api.app.domain.page.registry import get_default_page_type_registry

DEFAULT_MAX_VISUAL_CANDIDATES = 20
APP_VISUAL_ALLOWED_ACTIONS = (
    InteractionOperation.CLICK,
    InteractionOperation.SCROLL,
    InteractionOperation.BACK,
    InteractionOperation.OBSERVE,
)
_LOGGER = logging.getLogger(__name__)
_PAGE_TYPE_REGISTRY = get_default_page_type_registry()
_VISUAL_NAVIGATION_SELECTION_CONTRACT = "app-visual-navigation-selection.v1"
_VISUAL_NAVIGATION_TYPES = frozenset(
    {
        "home",
        "content_list",
        "video_list",
        "post_list",
        "activity",
        "profile",
    }
)
_APP_CANDIDATE_FUSION_PROTOCOL = "app-candidate-fusion.v1"
_APP_CANDIDATE_ROLE_PRIORITY = {
    "obstruction": 0,
    # 已明确指向目标页的入口保留在底栏之前，兼容恢复循环的第一候选语义；
    # 二者都高于普通内容卡，且最终仍受同一个总配额约束。
    "explicit_goal": 10,
    "global_navigation": 20,
    "other": 30,
    "content_card": 40,
}


@dataclass(frozen=True, kw_only=True)
class UnderstandAppScreenshotRequest:
    """只在受信任 Provider 调用边界短暂携带 Screenshot 字节。"""

    task_id: str
    session_id: str
    capture_id: str
    screenshot_artifact_id: str
    screenshot_png: bytes
    image_signature: str
    width: int
    height: int
    orientation: str
    package_name: str | None
    activity: str | None
    allowed_actions: tuple[InteractionOperation, ...]
    max_candidates: int

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "session_id",
            "capture_id",
            "screenshot_artifact_id",
            "image_signature",
        ):
            require_text(getattr(self, field_name), field_name)
        optional_text(self.package_name, "package_name")
        optional_text(self.activity, "activity")
        if not isinstance(self.screenshot_png, bytes) or not self.screenshot_png:
            raise ValueError("screenshot_png 必须为非空 bytes")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.width, self.height)
        ):
            raise ValueError("width/height 必须为正整数")
        if self.orientation not in {"portrait", "landscape", "unknown"}:
            raise ValueError("orientation 不受支持")
        actions = tuple(self.allowed_actions)
        if not actions or any(item not in APP_VISUAL_ALLOWED_ACTIONS for item in actions):
            raise ValueError("allowed_actions 包含 App 不支持的动作")
        if len(set(actions)) != len(actions):
            raise ValueError("allowed_actions 不能重复")
        if (
            isinstance(self.max_candidates, bool)
            or not isinstance(self.max_candidates, int)
            or not 1 <= self.max_candidates <= 100
        ):
            raise ValueError("max_candidates 必须在 1 到 100 之间")
        object.__setattr__(self, "screenshot_png", bytes(self.screenshot_png))
        object.__setattr__(self, "allowed_actions", actions)


@dataclass(frozen=True, kw_only=True)
class AppScreenshotCaptureContext:
    """由真实 Capture 读取并校验的视觉复用上下文。"""

    capture: CaptureRef
    screenshot_artifact_id: str
    image_signature: str
    width: int
    height: int
    orientation: str
    package_name: str | None
    activity: str | None
    screenshot_png: bytes | None = None
    content_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _validate_capture_ref(self.capture)
        require_text(self.screenshot_artifact_id, "screenshot_artifact_id")
        require_text(self.image_signature, "image_signature")
        if self.screenshot_artifact_id not in self.capture.artifact_ids:
            raise ValueError("视觉上下文 Screenshot Artifact 不属于 Capture")
        if (
            isinstance(self.width, bool)
            or not isinstance(self.width, int)
            or self.width <= 0
            or isinstance(self.height, bool)
            or not isinstance(self.height, int)
            or self.height <= 0
        ):
            raise ValueError("视觉上下文 width/height 必须为正整数")
        if self.orientation not in {"portrait", "landscape", "unknown"}:
            raise ValueError("视觉上下文 orientation 不受支持")
        optional_text(self.package_name, "package_name")
        optional_text(self.activity, "activity")
        if self.screenshot_png is not None:
            if not isinstance(self.screenshot_png, (bytes, bytearray)) or not self.screenshot_png:
                raise ValueError("screenshot_png 必须为非空 bytes")
            object.__setattr__(self, "screenshot_png", bytes(self.screenshot_png))
        if self.content_fingerprint is not None:
            require_text(self.content_fingerprint, "content_fingerprint")


class AppScreenshotSourceRepository(Protocol):
    """根据公共 Capture 身份读取 App Screenshot 元数据。"""

    async def load(self, *, capture: CaptureRef) -> PersistedAppCapture | None: ...

    async def load_ui_tree(self, *, capture: CaptureRef) -> VerifiedAppUiTree | None: ...


class ScreenshotArtifactReader(Protocol):
    """只在应用层视觉边界读取 Evidence Artifact 正文。"""

    async def read(self, storage_path: str) -> bytes: ...


class ScreenshotUnderstandingProvider(Protocol):
    """把可信 PNG 转换为有界视觉事实，不执行任何动作。"""

    async def understand(
        self,
        request: UnderstandAppScreenshotRequest,
    ) -> AppVisualAnalysis: ...


class AppScreenshotUnderstandingError(RuntimeError):
    """截图缺失、校验失败或 Provider 输出无效。"""

    def __init__(self, message: str, *, error_code: str, retryable: bool = False) -> None:
        super().__init__(require_text(message, "message"))
        self.error_code = require_text(error_code, "error_code")
        if not isinstance(retryable, bool):
            raise ValueError("retryable 必须为布尔值")
        self.retryable = retryable


class AppScreenshotUnderstandingService:
    """在调用视觉 Provider 前完成 Screenshot 的完整性与归属校验。"""

    def __init__(
        self,
        *,
        sources: AppScreenshotSourceRepository,
        artifacts: ScreenshotArtifactReader,
        provider: ScreenshotUnderstandingProvider,
        allowed_actions: tuple[InteractionOperation, ...] = APP_VISUAL_ALLOWED_ACTIONS,
        max_candidates: int = DEFAULT_MAX_VISUAL_CANDIDATES,
    ) -> None:
        self._sources = sources
        self._artifacts = artifacts
        self._provider = provider
        self._allowed_actions = tuple(allowed_actions)
        self._max_candidates = max_candidates

    async def _load_verified_screenshot(
        self,
        capture: CaptureRef,
        *,
        loaded_png: bytes | None = None,
    ) -> tuple[PersistedAppCapture, bytes, str]:
        """读取并校验 Screenshot 正文；允许复用同一次取样已读字节。"""

        _validate_capture_ref(capture)
        source = await self._sources.load(capture=capture)
        if source is None or source.capture != capture:
            raise AppScreenshotUnderstandingError(
                "App Screenshot Capture 不存在或归属不一致",
                error_code="SCREENSHOT_ARTIFACT_MISSING",
            )
        screenshot = source.screenshot
        if (
            screenshot.artifact_id not in capture.artifact_ids
            or screenshot.artifact_type != "screenshot"
            or screenshot.content_type != "image/png"
        ):
            raise AppScreenshotUnderstandingError(
                "App Screenshot Artifact 不存在或类型不受支持",
                error_code="SCREENSHOT_ARTIFACT_MISSING",
            )
        if loaded_png is not None:
            raw = bytes(loaded_png)
        else:
            try:
                content = await self._artifacts.read(screenshot.storage_path)
            except Exception as exc:  # noqa: BLE001 - 统一为结构化读取失败
                raise AppScreenshotUnderstandingError(
                    "App Screenshot Artifact 正文不存在",
                    error_code="SCREENSHOT_ARTIFACT_MISSING",
                ) from exc
            if not isinstance(content, (bytes, bytearray)) or not content:
                raise AppScreenshotUnderstandingError(
                    "App Screenshot Artifact 正文不存在",
                    error_code="SCREENSHOT_ARTIFACT_MISSING",
                )
            raw = bytes(content)
        actual_signature = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if (
            len(raw) != screenshot.size_bytes
            or actual_signature != screenshot.checksum
            or actual_signature != source.image_signature
        ):
            raise AppScreenshotUnderstandingError(
                "App Screenshot checksum 校验失败",
                error_code="SCREENSHOT_CHECKSUM_MISMATCH",
            )
        try:
            actual_width, actual_height = _read_png_dimensions(raw)
        except (TypeError, ValueError) as exc:
            raise AppScreenshotUnderstandingError(
                "App Screenshot Artifact 不是合法 PNG",
                error_code="SCREENSHOT_ARTIFACT_MISSING",
            ) from exc
        if (actual_width, actual_height) != (source.width, source.height):
            raise AppScreenshotUnderstandingError(
                "App Screenshot 尺寸与已持久化事实不一致",
                error_code="SCREENSHOT_CHECKSUM_MISMATCH",
            )
        return source, raw, actual_signature

    async def resolve_capture_visual_context(
        self,
        capture: CaptureRef,
    ) -> AppScreenshotCaptureContext:
        """读取并校验真实 Capture 的视觉身份，不调用视觉 LLM。"""

        source, raw, actual_signature = await self._load_verified_screenshot(capture)
        fingerprint = compute_app_business_content_fingerprint(
            raw,
            width=source.width,
            height=source.height,
        )
        return AppScreenshotCaptureContext(
            capture=capture,
            screenshot_artifact_id=source.screenshot.artifact_id,
            image_signature=actual_signature,
            width=source.width,
            height=source.height,
            orientation=source.orientation,
            package_name=source.package_name,
            activity=source.activity,
            screenshot_png=raw,
            content_fingerprint=(
                str(fingerprint["value"])
                if isinstance(fingerprint, Mapping) and fingerprint.get("value")
                else None
            ),
        )

    async def resolve_capture_image_signature(self, capture: CaptureRef) -> str:
        """读取已封存 Capture 的 image_signature，不调用视觉 LLM。"""

        _validate_capture_ref(capture)
        source = await self._sources.load(capture=capture)
        if source is None or source.capture != capture:
            raise AppScreenshotUnderstandingError(
                "App Screenshot Capture 不存在或归属不一致",
                error_code="SCREENSHOT_ARTIFACT_MISSING",
            )
        signature = source.image_signature
        if not isinstance(signature, str) or not signature.strip():
            raise AppScreenshotUnderstandingError(
                "App Screenshot image_signature 缺失",
                error_code="SCREENSHOT_ARTIFACT_MISSING",
            )
        return signature

    async def understand_from_context(
        self,
        context: AppScreenshotCaptureContext,
    ) -> AppVisualAnalysis:
        """复用已读取的截图正文，避免预检失败后再下载一次。"""

        if not isinstance(context, AppScreenshotCaptureContext):
            raise TypeError("context 必须为 AppScreenshotCaptureContext")
        return await self.understand(
            context.capture,
            loaded_png=context.screenshot_png,
        )

    async def understand(
        self,
        capture: CaptureRef,
        *,
        loaded_png: bytes | None = None,
    ) -> AppVisualAnalysis:
        started = time.perf_counter()
        source, content, actual_signature = await self._load_verified_screenshot(
            capture,
            loaded_png=loaded_png,
        )
        screenshot = source.screenshot
        request = UnderstandAppScreenshotRequest(
            task_id=capture.task_id,
            session_id=capture.session_id,
            capture_id=capture.capture_id,
            screenshot_artifact_id=screenshot.artifact_id,
            screenshot_png=content,
            image_signature=actual_signature,
            width=source.width,
            height=source.height,
            orientation=source.orientation,
            package_name=source.package_name,
            activity=source.activity,
            allowed_actions=self._allowed_actions,
            max_candidates=self._max_candidates,
        )
        try:
            analysis = await self._provider.understand(request)
        except AppVisualValidationError as exc:
            raise AppScreenshotUnderstandingError(
                "Screenshot Understanding Provider 输出无效",
                error_code="INVALID_VISUAL_OUTPUT",
            ) from exc
        except AppScreenshotUnderstandingError:
            raise
        except Exception as exc:
            raise AppScreenshotUnderstandingError(
                "Screenshot Understanding Provider 调用失败",
                error_code="VISUAL_UNDERSTANDING_FAILED",
                retryable=True,
            ) from exc
        _validate_analysis(analysis, request)
        analysis = calibrate_popup_close_candidates(
            analysis=analysis,
            screenshot_png=content,
        )
        _validate_analysis(analysis, request)
        analysis = await _append_trusted_accessibility_candidates(
            analysis=analysis,
            source=source,
            sources=self._sources,
            max_candidates=self._max_candidates,
            screenshot_png=content,
        )
        _validate_analysis(analysis, request)
        analysis = _append_visual_age_confirm_candidate(
            analysis=analysis,
            screenshot_png=content,
            max_candidates=self._max_candidates,
        )
        _validate_analysis(analysis, request)
        # 底栏无障碍常空（WebView／自绘）：同帧像素槽位校正／补齐 Tab 落点。
        from services.api.app.application.app_ui_tree_features import (
            hjll_ordered_bottom_tab_labels,
        )

        analysis = calibrate_bottom_navigation_candidates(
            analysis=analysis,
            screenshot_png=content,
            ordered_labels=hjll_ordered_bottom_tab_labels(),
            max_candidates=self._max_candidates,
        )
        _validate_analysis(analysis, request)
        # 上方分类：与底栏同一套「同帧 PNG 像素槽位」校准，禁止只靠 JSON 插值。
        from services.api.app.application.app_ui_tree_features import (
            home_top_category_tabs_ui_tree_features,
            ordered_home_top_category_clickable_slots,
        )

        top_slots = ordered_home_top_category_clickable_slots(
            package_name=str(getattr(analysis, "package_name", "") or "") or None,
        )
        if top_slots:
            top_spec = home_top_category_tabs_ui_tree_features(
                package_name=str(getattr(analysis, "package_name", "") or "") or None,
            )
            layout = top_spec.get("layout") if isinstance(top_spec, Mapping) else None
            slot_count = None
            if isinstance(layout, Mapping):
                raw_count = layout.get("slot_count")
                if isinstance(raw_count, int) and not isinstance(raw_count, bool):
                    slot_count = raw_count
            analysis = calibrate_home_top_category_candidates(
                analysis=analysis,
                screenshot_png=content,
                ordered_clickable=top_slots,
                slot_count=slot_count,
                max_candidates=self._max_candidates,
            )
            _validate_analysis(analysis, request)
        # UI Tree 与 Vision 均已完成提取后再做同帧导航判定；否则候选已满时
        # 会漏掉可信底栏，也会让弱 Vision 页型先一步写入错误事实。
        analysis = _attach_trusted_visual_navigation_selection(
            analysis=analysis,
            screenshot_png=content,
        )
        _validate_analysis(analysis, request)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        analysis = _attach_visual_latency(
            analysis,
            elapsed_ms=elapsed_ms,
            reused=False,
        )
        analysis = _attach_content_fingerprint(analysis, png=content)
        _LOGGER.info(
            "App 视觉理解完成 capture_id=%s reused=false elapsed_ms=%s signature_prefix=%s",
            capture.capture_id,
            elapsed_ms,
            _image_signature_prefix(analysis.image_signature),
        )
        return analysis


def _attach_content_fingerprint(
    analysis: AppVisualAnalysis,
    *,
    png: bytes,
    fingerprint: Mapping[str, Any] | None = None,
) -> AppVisualAnalysis:
    """把去状态栏业务指纹写入 surface_facts，供复用与报告折叠。"""

    fact = fingerprint or compute_app_business_content_fingerprint(
        png,
        width=analysis.width,
        height=analysis.height,
    )
    if not isinstance(fact, Mapping) or not fact.get("value"):
        return analysis
    surface_facts = dict(analysis.surface_facts)
    surface_facts["content_fingerprint"] = dict(fact)
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=analysis.candidates,
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=surface_facts,
    )


def _image_signature_prefix(image_signature: str, *, length: int = 20) -> str:
    text = str(image_signature or "")
    return text[:length] if len(text) > length else text


def _attach_trusted_visual_navigation_selection(
    *,
    analysis: AppVisualAnalysis,
    screenshot_png: bytes,
) -> AppVisualAnalysis:
    """用同帧成组底栏与唯一高亮像素纠正 Vision 串栏。

    候选几何仍来自当前封存截图，不能跨 Capture 或使用 APK 固定坐标；只有
    至少四个同基线 App 级导航候选形成完整底栏，且唯一候选的高饱和像素显著
    高于其余候选时才签发纠错事实。该事实只纠正同层导航页型，不覆盖详情页。
    """

    if not isinstance(screenshot_png, bytes) or not screenshot_png:
        return analysis
    navigation: list[tuple[AppVisualCandidate, str, str]] = []
    for candidate in analysis.candidates:
        bounds = candidate.normalized_bounds
        expected = str(candidate.expected_state_hint or "").strip().casefold()
        label = _visual_navigation_label(candidate.semantic_name)
        if (
            candidate.action_type is not InteractionOperation.CLICK
            or candidate.frontier_scope != "app"
            or bounds is None
            or expected not in _VISUAL_NAVIGATION_TYPES
            or label is None
            or bounds.top < 0.84
            or bounds.bottom < 0.9
            or bounds.bottom - bounds.top > 0.16
            or not 0.07 <= bounds.right - bounds.left <= 0.3
        ):
            continue
        navigation.append((candidate, expected, label))
    if len(navigation) < 4:
        return analysis
    tops = tuple(item[0].normalized_bounds.top for item in navigation if item[0].normalized_bounds)
    bottoms = tuple(
        item[0].normalized_bounds.bottom for item in navigation if item[0].normalized_bounds
    )
    left = min(item[0].normalized_bounds.left for item in navigation if item[0].normalized_bounds)
    right = max(item[0].normalized_bounds.right for item in navigation if item[0].normalized_bounds)
    if max(tops) - min(tops) > 0.035 or max(bottoms) - min(bottoms) > 0.035:
        return analysis
    if right - left < 0.55:
        return analysis
    try:
        import cv2
        import numpy as np
    except ImportError:
        return analysis
    encoded = np.frombuffer(screenshot_png, dtype=np.uint8)
    image = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
    if image is None or image.shape[:2] != (analysis.height, analysis.width):
        return analysis
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    scored: list[tuple[float, AppVisualCandidate, str, str]] = []
    for candidate, expected, label in navigation:
        bounds = candidate.normalized_bounds
        if bounds is None:
            continue
        crop = hsv[
            int(bounds.top * analysis.height) : int(bounds.bottom * analysis.height),
            int(bounds.left * analysis.width) : int(bounds.right * analysis.width),
        ]
        if crop.size == 0:
            continue
        score = float(((crop[:, :, 1] >= 70) & (crop[:, :, 2] >= 100)).mean())
        scored.append((score, candidate, expected, label))
    if len(scored) < 4:
        return analysis
    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, selected_candidate, selected_page_type, selected_label = scored[0]
    second_score = scored[1][0]
    if best_score < 0.02 or best_score < second_score + 0.015 or best_score < second_score * 1.75:
        return analysis
    resolved_page_type = _prefer_trusted_visual_page_type(
        vision_page_type=analysis.visual_page_type,
        trusted_page_type=selected_page_type,
        trusted_selected_page_type=selected_page_type,
    )
    surface_facts = dict(analysis.surface_facts)
    surface_facts["trusted_visual_navigation_selection"] = {
        "contract_id": _VISUAL_NAVIGATION_SELECTION_CONTRACT,
        "selected_page_type": selected_page_type,
        "selected_label": selected_label,
        "candidate_id": selected_candidate.candidate_id,
        "navigation_candidate_count": len(scored),
        "selected_score": round(best_score, 6),
        "runner_up_score": round(second_score, 6),
        "source_capture_id": analysis.capture_id,
        "screenshot_artifact_id": analysis.screenshot_artifact_id,
    }
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=resolved_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=tuple(
            dict.fromkeys((*analysis.key_elements, "pixel:selected_visual_navigation"))
        ),
        candidates=analysis.candidates,
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=surface_facts,
    )


def _visual_navigation_label(value: object) -> str | None:
    """从 Provider 底栏候选名称提取可审计短标签。"""

    label = str(value or "").strip()
    if not label:
        return None
    label = re.sub(r"^(?:底部导航|底栏导航|底部|底栏)\s*", "", label, flags=re.I)
    label = re.sub(r"^(?:tab|页签|选项卡)\s*", "", label, flags=re.I)
    label = label.strip(" _-—:：|/\\「」『』【】[]()（）")
    return label[:18] if label else None


def _attach_visual_latency(
    analysis: AppVisualAnalysis,
    *,
    elapsed_ms: int,
    reused: bool,
) -> AppVisualAnalysis:
    """把最小视觉耗时审计写入 surface_facts，并保留 Provider 分段埋点。"""

    surface_facts: dict[str, Any] = dict(analysis.surface_facts)
    existing = (
        dict(surface_facts["visual_latency"])
        if isinstance(surface_facts.get("visual_latency"), Mapping)
        else {}
    )
    existing.update(
        {
            "elapsed_ms": int(elapsed_ms),
            "reused": bool(reused),
            "capture_id": analysis.capture_id,
            "image_signature_prefix": _image_signature_prefix(analysis.image_signature),
        }
    )
    surface_facts["visual_latency"] = existing
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=analysis.candidates,
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=surface_facts,
    )


def _fuse_app_visual_candidates(
    *,
    candidates: tuple[AppVisualCandidate, ...],
    trusted_candidate_ids: set[str],
    max_candidates: int,
    selected_navigation_labels: frozenset[str] = frozenset(),
) -> tuple[tuple[AppVisualCandidate, ...], dict[str, object]]:
    """按来源和语义角色融合 Vision/UI Tree，再执行统一配额裁剪。

    Provider 先返回的候选不是优先级保证：当 Vision 恰好占满配额时，仍必须
    读取并提取同帧 UI Tree。清障、全局导航和明确页型入口拥有固定的角色优先级，
    普通内容卡最后裁剪；优先级只读取候选语义和当前 Capture 事实，不使用坐标
    或包名 Playbook。
    """

    selected_labels = {
        label.strip().casefold() for label in selected_navigation_labels if label.strip()
    }
    records: list[tuple[int, int, int, AppVisualCandidate, str, str, str]] = []
    dropped_selected_navigation = 0
    for index, candidate in enumerate(candidates):
        role = _app_candidate_fusion_role(candidate)
        if role == "global_navigation" and selected_labels:
            label = _visual_navigation_label(candidate.semantic_name)
            if label is not None and label.strip().casefold() in selected_labels:
                dropped_selected_navigation += 1
                continue
        source = "ui_tree" if candidate.candidate_id in trusted_candidate_ids else "vision"
        # 同一角色保持 Vision 原有顺序，避免多个 UI Tree 关闭控件在语义歧义时
        # 抢到模型原候选之前；UI Tree 仍参与完整提取，且跨角色优先级先于来源。
        source_priority = 0 if source == "vision" else 1
        if source == "ui_tree":
            discovery = (
                candidate.discovery_source
                if candidate.discovery_source
                in {DISCOVERY_SOURCE_UIA_ALIGNED, DISCOVERY_SOURCE_UIA_ISSUED}
                else DISCOVERY_SOURCE_UIA_ALIGNED
            )
        else:
            discovery = candidate.discovery_source or DISCOVERY_SOURCE_VISION
        tagged_candidate = with_visual_candidate_discovery_source(candidate, discovery)
        records.append(
            (
                _APP_CANDIDATE_ROLE_PRIORITY[role],
                source_priority,
                index,
                tagged_candidate,
                source,
                role,
                discovery,
            )
        )
    records.sort(key=lambda item: (item[0], item[1], item[2]))
    retained = records[:max_candidates]
    fused = tuple(item[3] for item in retained)

    def _counts(
        items: list[tuple[int, int, int, AppVisualCandidate, str, str, str]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            source_or_role = item[4]
            counts[source_or_role] = counts.get(source_or_role, 0) + 1
        return counts

    def _role_counts(
        items: list[tuple[int, int, int, AppVisualCandidate, str, str, str]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            role = item[5]
            counts[role] = counts.get(role, 0) + 1
        return counts

    def _discovery_counts(
        items: list[tuple[int, int, int, AppVisualCandidate, str, str, str]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            discovery = item[6]
            counts[discovery] = counts.get(discovery, 0) + 1
        return counts

    return fused, {
        "protocol_version": _APP_CANDIDATE_FUSION_PROTOCOL,
        "max_candidates": max_candidates,
        "candidate_count_before_crop": len(records) + dropped_selected_navigation,
        "candidate_count_after_crop": len(fused),
        "dropped_candidate_count": len(records) - len(retained) + dropped_selected_navigation,
        "dropped_selected_navigation_count": dropped_selected_navigation,
        "source_counts_before_crop": _counts(records),
        "source_counts_after_crop": _counts(retained),
        "role_counts_before_crop": _role_counts(records),
        "role_counts_after_crop": _role_counts(retained),
        "discovery_source_counts_before_crop": _discovery_counts(records),
        "discovery_source_counts_after_crop": _discovery_counts(retained),
    }


def _app_candidate_fusion_role(candidate: AppVisualCandidate) -> str:
    """按候选语义识别清障、导航、明确入口和普通内容卡角色。"""

    expected = str(candidate.expected_state_hint or "").strip().casefold()
    control_role = str(candidate.control_role or "").strip().casefold()
    semantic = " ".join(
        (str(candidate.semantic_name or ""), str(candidate.purpose or ""))
    ).casefold()
    if control_role in {"dismiss", "skip"} or expected in {"close_popup_x", "tap_skip"}:
        return "obstruction"
    if any(marker in semantic for marker in ("普通内容卡片", "普通内容", "内容卡片")):
        return "content_card"
    if candidate.frontier_scope == "app":
        return "global_navigation"
    if expected in set(_PAGE_TYPE_REGISTRY.coverage_required_codes):
        return "explicit_goal"
    return "other"


def _append_visual_age_confirm_candidate(
    *,
    analysis: AppVisualAnalysis,
    screenshot_png: bytes,
    max_candidates: int,
) -> AppVisualAnalysis:
    """WebView／自绘年龄门无 UIA 时，签发「我年满18岁-进入」清障候选。"""

    already_has_age = any(
        is_age_confirm_candidate(
            action_type=item.action_type,
            risk_level=item.risk_level,
            semantic_target=item.semantic_name,
            purpose=item.purpose,
            expected_state_hint=item.expected_state_hint,
        )
        for item in analysis.candidates
    )
    if already_has_age:
        return analysis

    semantic = analysis_has_age_gate_evidence(
        page_state=analysis.page_state,
        key_elements=analysis.key_elements,
        visual_summary=analysis.visual_summary,
        surface_facts=analysis.surface_facts,
        candidate_texts=tuple(str(item.semantic_name or "") for item in analysis.candidates),
    )
    try:
        visual_bounds, bounds_source = resolve_age_confirm_visual_bounds(
            screenshot_png=screenshot_png,
            allow_default_layout=True,
            require_semantic_evidence=semantic,
        )
    except ValueError:
        return analysis

    age_label = "我年满18岁-进入"
    visual_candidate = AppVisualCandidate.build(
        source_capture_id=analysis.capture_id,
        source_image_signature=analysis.image_signature,
        source_width=analysis.width,
        source_height=analysis.height,
        source_orientation=analysis.orientation,
        semantic_name=age_label,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=visual_bounds,
        confidence=0.9 if bounds_source == "orange_blob" else 0.72,
        risk_level=InteractionRiskLevel.LOW,
        purpose="确认已满18岁并进入应用",
        control_role="dismiss",
        expected_state_hint="confirm_age_18",
        frontier_scope="local",
        discovery_source=DISCOVERY_SOURCE_CLEARANCE_SYNTHETIC,
    )
    candidates = list(analysis.candidates)
    if all(item.candidate_id != visual_candidate.candidate_id for item in candidates):
        candidates.insert(0, visual_candidate)
    if max_candidates > 0:
        candidates = candidates[: max(1, max_candidates)]
    surface_facts = dict(analysis.surface_facts)
    surface_facts["trusted_accessibility_age_confirm"] = build_age_confirm_visual_fact(
        bounds=visual_bounds,
        source=bounds_source,
    )
    surface_facts["auth_gate_visible"] = True
    surface_facts["dialog_visible"] = True
    surface_facts["dialog_kind"] = "age_verification"
    surface_facts["dismiss_control_visible"] = True
    surface_facts["dismiss_control_enabled"] = True
    surface_facts["dismiss_semantic_target"] = age_label
    key_elements = list(analysis.key_elements)
    key_elements.append(f"visual:age_confirm:{bounds_source}")
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=tuple(dict.fromkeys(key_elements)),
        candidates=tuple(candidates),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state="auth_gate",
        overlay_signature=analysis.overlay_signature or "visual:age_gate",
        surface_facts=surface_facts,
    )


async def _append_trusted_accessibility_candidates(
    *,
    analysis: AppVisualAnalysis,
    source: PersistedAppCapture,
    sources: AppScreenshotSourceRepository,
    max_candidates: int,
    screenshot_png: bytes,
) -> AppVisualAnalysis:
    """用已封存 UI Tree 补充受信控件，不改变 Provider 的安全判定事实。"""

    if source.package_name is None:
        return analysis
    loader = getattr(sources, "load_ui_tree", None)
    if not callable(loader):
        return analysis
    try:
        verified = await loader(capture=source.capture)
    except Exception as exc:  # noqa: BLE001 - 辅助证据失败时不得放出触控候选
        _LOGGER.warning(
            "忽略不可验证的 App UI Tree，Capture=%s，错误=%s",
            source.capture_id,
            type(exc).__name__,
        )
        return analysis
    if not isinstance(verified, VerifiedAppUiTree):
        return analysis
    hierarchy_size = infer_hierarchy_screen_size(verified.xml_text)
    # Accessibility 根节点可能裁掉系统导航栏；同时兼容保持宽高比的缩略图。
    screen_width, screen_height = resolve_ui_tree_screen_size(
        hierarchy_size,
        capture_width=source.width,
        capture_height=source.height,
    )
    if (
        not isinstance(screen_width, int)
        or not isinstance(screen_height, int)
        or screen_width <= 0
        or screen_height <= 0
    ):
        return analysis
    candidates = list(analysis.candidates)
    trusted_candidate_ids: set[str] = set()
    aligned_candidate_count = 0
    for index, candidate in enumerate(tuple(candidates)):
        if (
            candidate.action_type is not InteractionOperation.CLICK
            or candidate.risk_level
            not in {InteractionRiskLevel.LOW, InteractionRiskLevel.MEDIUM}
        ):
            continue
        target = None
        if candidate.risk_level is InteractionRiskLevel.LOW:
            target = find_unique_trusted_action_target_by_label(
                verified.xml_text,
                package_name=source.package_name,
                semantic_name=candidate.semantic_name,
                screen_width=screen_width,
                screen_height=screen_height,
            )
            if target is None:
                target = find_unique_trusted_navigation_target_by_label(
                    verified.xml_text,
                    package_name=source.package_name,
                    semantic_name=candidate.semantic_name,
                    expected_page_type=candidate.expected_state_hint,
                    screen_width=screen_width,
                    screen_height=screen_height,
                )
        if (
            target is None
            and candidate.normalized_bounds is not None
        ):
            target = find_trusted_post_publish_choice_target(
                verified.xml_text,
                package_name=source.package_name,
                semantic_name=candidate.semantic_name,
                expected_page_type=candidate.expected_state_hint,
                normalized_bounds=candidate.normalized_bounds.to_dict(),
                screen_width=screen_width,
                screen_height=screen_height,
            )
        if (
            target is None
            and candidate.risk_level is InteractionRiskLevel.LOW
            and candidate.normalized_bounds is not None
        ):
            target = find_trusted_post_publish_entry_target(
                verified.xml_text,
                package_name=source.package_name,
                semantic_name=candidate.semantic_name,
                expected_page_type=candidate.expected_state_hint,
                normalized_bounds=candidate.normalized_bounds.to_dict(),
                screen_width=screen_width,
                screen_height=screen_height,
            )
        if target is None:
            continue
        bounds = target.get("bounds")
        if not isinstance(bounds, dict):
            continue
        aligned = AppVisualCandidate.build(
            source_capture_id=candidate.source_capture_id,
            source_image_signature=candidate.source_image_signature,
            source_width=candidate.source_width,
            source_height=candidate.source_height,
            source_orientation=candidate.source_orientation,
            semantic_name=candidate.semantic_name,
            action_type=candidate.action_type,
            normalized_bounds=NormalizedBounds(
                left=int(bounds["left"]) / screen_width,
                top=int(bounds["top"]) / screen_height,
                right=int(bounds["right"]) / screen_width,
                bottom=int(bounds["bottom"]) / screen_height,
            ),
            confidence=1.0,
            risk_level=candidate.risk_level,
            purpose=candidate.purpose,
            control_role=candidate.control_role,
            expected_state_hint=candidate.expected_state_hint,
            frontier_scope=candidate.frontier_scope,
            discovery_source=DISCOVERY_SOURCE_UIA_ALIGNED,
        )
        if aligned.candidate_id == candidate.candidate_id:
            trusted_candidate_ids.add(aligned.candidate_id)
            candidates[index] = with_visual_candidate_discovery_source(
                candidate,
                DISCOVERY_SOURCE_UIA_ALIGNED,
            )
            continue
        candidates[index] = aligned
        trusted_candidate_ids.add(aligned.candidate_id)
        aligned_candidate_count += 1
    skip_target = find_unique_trusted_skip_target(
        verified.xml_text,
        package_name=source.package_name,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    age_confirm_target = find_unique_trusted_age_confirm_target(
        verified.xml_text,
        package_name=source.package_name,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    # 开屏／全屏广告常被 Vision 标成 splash／startup_ad／unknown，而不是 popup；
    # 只要同帧 a11y 已证明唯一安全「关闭」类标签，就必须签发，不能等 popup_surface。
    dismiss_targets = find_trusted_obstruction_dismiss_targets(
        verified.xml_text,
        package_name=source.package_name,
        screen_width=screen_width,
        screen_height=screen_height,
    )
    # 同一控件不得既作开屏 skip 又作弹层 dismiss（如唯一「跳过」）。
    if skip_target is not None:
        skip_signature = str((skip_target.get("bounds") or {}).get("signature") or "")
        skip_label = str(skip_target.get("label") or "").casefold()
        dismiss_targets = tuple(
            item
            for item in dismiss_targets
            if str((item.get("bounds") or {}).get("signature") or "") != skip_signature
            and str(item.get("label") or "").casefold() != skip_label
        )
    controls = inspect_trusted_app_accessibility_controls(
        verified.xml_text,
        package_name=source.package_name,
        screen_width=screen_width,
        screen_height=screen_height,
        screenshot_png=screenshot_png,
    )
    if (
        skip_target is None
        and age_confirm_target is None
        and not dismiss_targets
        and controls is None
        and aligned_candidate_count == 0
    ):
        return analysis

    def _append_candidate(
        *,
        target: dict[str, object],
        semantic_name: str,
        purpose: str,
        expected_state_hint: str,
        control_role: str | None = None,
        frontier_scope: str = "local",
    ) -> None:
        bounds = target["bounds"]
        if not isinstance(bounds, dict):
            return
        candidate = AppVisualCandidate.build(
            source_capture_id=analysis.capture_id,
            source_image_signature=analysis.image_signature,
            source_width=analysis.width,
            source_height=analysis.height,
            source_orientation=analysis.orientation,
            semantic_name=semantic_name,
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(
                left=int(bounds["left"]) / screen_width,
                top=int(bounds["top"]) / screen_height,
                right=int(bounds["right"]) / screen_width,
                bottom=int(bounds["bottom"]) / screen_height,
            ),
            confidence=1.0,
            risk_level=InteractionRiskLevel.LOW,
            purpose=purpose,
            control_role=control_role,
            expected_state_hint=expected_state_hint,
            frontier_scope=frontier_scope,
            discovery_source=DISCOVERY_SOURCE_UIA_ISSUED,
        )
        trusted_candidate_ids.add(candidate.candidate_id)
        if all(item.candidate_id != candidate.candidate_id for item in candidates):
            candidates.append(candidate)

    surface_facts = dict(analysis.surface_facts)
    key_elements = list(analysis.key_elements)
    trusted_overlay_signature = None
    if aligned_candidate_count:
        surface_facts["trusted_accessibility_candidate_alignment"] = {
            "artifact_id": verified.artifact.artifact_id,
            "aligned_candidate_count": aligned_candidate_count,
            "screen_width": screen_width,
            "screen_height": screen_height,
        }
        key_elements.append("accessibility:aligned_low_risk_action")
    if skip_target is not None:
        exit_label = str(skip_target["label"])
        skip_bounds = skip_target["bounds"]
        normalized_skip_bounds: dict[str, float] | None = None
        if isinstance(skip_bounds, dict) and screen_width > 0 and screen_height > 0:
            try:
                normalized_skip_bounds = {
                    "left": int(skip_bounds["left"]) / screen_width,
                    "top": int(skip_bounds["top"]) / screen_height,
                    "right": int(skip_bounds["right"]) / screen_width,
                    "bottom": int(skip_bounds["bottom"]) / screen_height,
                }
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                normalized_skip_bounds = None
        _append_candidate(
            target=skip_target,
            semantic_name=f"跳过广告：{exit_label}",
            purpose="点击目标包唯一可访问性开屏退出控件，跳过广告进入业务页",
            expected_state_hint="tap_skip",
            control_role="skip",
        )
        trusted_skip_fact: dict[str, object] = {
            "artifact_id": verified.artifact.artifact_id,
            "package_name": skip_target["package_name"],
            "label": skip_target["label"],
            "screen_width": screen_width,
            "screen_height": screen_height,
        }
        if normalized_skip_bounds is not None:
            trusted_skip_fact["normalized_bounds"] = normalized_skip_bounds
        surface_facts["trusted_accessibility_skip"] = trusted_skip_fact

    if age_confirm_target is not None:
        age_label = str(age_confirm_target["label"])
        age_bounds = age_confirm_target["bounds"]
        normalized_age_bounds: dict[str, float] | None = None
        if isinstance(age_bounds, dict) and screen_width > 0 and screen_height > 0:
            try:
                normalized_age_bounds = {
                    "left": int(age_bounds["left"]) / screen_width,
                    "top": int(age_bounds["top"]) / screen_height,
                    "right": int(age_bounds["right"]) / screen_width,
                    "bottom": int(age_bounds["bottom"]) / screen_height,
                }
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                normalized_age_bounds = None
        _append_candidate(
            target=age_confirm_target,
            semantic_name=age_label,
            purpose="确认已满18岁并进入应用",
            expected_state_hint="confirm_age_18",
            control_role="dismiss",
        )
        trusted_age_fact: dict[str, object] = {
            "artifact_id": verified.artifact.artifact_id,
            "package_name": age_confirm_target["package_name"],
            "label": age_label,
            "screen_width": screen_width,
            "screen_height": screen_height,
        }
        if normalized_age_bounds is not None:
            trusted_age_fact["normalized_bounds"] = normalized_age_bounds
        surface_facts["trusted_accessibility_age_confirm"] = trusted_age_fact
        surface_facts["auth_gate_visible"] = True
        surface_facts["dialog_visible"] = True
        surface_facts["dialog_kind"] = "age_verification"
        surface_facts["dismiss_control_visible"] = True
        surface_facts["dismiss_control_enabled"] = True
        surface_facts["dismiss_semantic_target"] = age_label
        key_elements.append(f"accessibility:age_confirm:{age_label[:24]}")
        page_state_override = "auth_gate"
    else:
        page_state_override = None

    if dismiss_targets:
        for item in dismiss_targets:
            label = str(item["label"])
            _append_candidate(
                target=item,
                semantic_name=f"关闭弹层：{label}",
                purpose=f"点击目标包当前弹层的可信关闭控件「{label}」并继续清障",
                expected_state_hint="close_popup_x",
                control_role="dismiss",
            )
        surface_facts["popup_visible"] = True
        key_elements.append("accessibility:labeled_obstruction_dismiss")
        signature_terms = "|".join(
            f"{item['label']}:{item['bounds']['signature']}" for item in dismiss_targets
        )
        trusted_overlay_signature = (
            "accessibility:dismiss:" + hashlib.sha256(signature_terms.encode("utf-8")).hexdigest()
        )
        if len(dismiss_targets) == 1:
            only = dismiss_targets[0]
            only_bounds = only.get("bounds")
            trusted_dismiss_fact: dict[str, object] = {
                "artifact_id": verified.artifact.artifact_id,
                "package_name": only["package_name"],
                "label": only["label"],
                "screen_width": screen_width,
                "screen_height": screen_height,
            }
            if (
                isinstance(only_bounds, dict)
                and screen_width > 0
                and screen_height > 0
            ):
                try:
                    trusted_dismiss_fact["normalized_bounds"] = {
                        "left": int(only_bounds["left"]) / screen_width,
                        "top": int(only_bounds["top"]) / screen_height,
                        "right": int(only_bounds["right"]) / screen_width,
                        "bottom": int(only_bounds["bottom"]) / screen_height,
                    }
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    pass
            surface_facts["trusted_accessibility_dismiss"] = trusted_dismiss_fact

    trusted_page_type = None
    if controls is not None:
        overlay_close = controls["overlay_close"]
        navigation = controls["bottom_navigation"]
        content_cards = controls["content_cards"]
        trusted_page_type = controls["page_type"]
        overlay_pixel_bounds: Mapping[str, object] | None = None
        if isinstance(overlay_close, dict):
            _append_candidate(
                target=overlay_close,
                semantic_name="关闭弹层",
                purpose="仅关闭当前可验证弹层遮挡，不点击弹层主体或外部链接",
                expected_state_hint="close_popup_x",
                control_role="dismiss",
            )
            surface_facts["popup_visible"] = True
            key_elements.append("accessibility:overlay_close")
            raw_overlay_bounds = overlay_close.get("bounds")
            if isinstance(raw_overlay_bounds, dict):
                overlay_pixel_bounds = raw_overlay_bounds
                signature = raw_overlay_bounds.get("signature")
                if isinstance(signature, str) and signature:
                    trusted_overlay_signature = f"accessibility:{signature}"
        # 弹层与底栏可并存：仅签发中心不落在蒙层／顶层广告内的未选中 Tab。
        if isinstance(navigation, tuple):
            pixel_selected_labels = controls.get("pixel_selected_labels")
            selected_labels = (
                {
                    str(value).strip().casefold()
                    for value in pixel_selected_labels
                    if isinstance(value, str) and value.strip()
                }
                if isinstance(pixel_selected_labels, (list, tuple))
                else set()
            )
            for item in navigation:
                label = str(item["label"])
                if item.get("selected") is True or label.strip().casefold() in selected_labels:
                    # Flutter 经常不暴露 selected=true；同帧像素唯一高亮同样是
                    # 当前页签的直接证据，不能把它再次加入 Frontier。
                    continue
                if item.get("action_allowed") is not True:
                    # PageType Catalog 或高风险文案无法证明该导航是低风险探索，
                    # 仅保留结构事实，不生成可执行点击候选。
                    continue
                if not _bottom_nav_center_clears_overlay(
                    target=item,
                    overlay_pixel_bounds=overlay_pixel_bounds,
                    surface_facts=surface_facts,
                    screen_width=screen_width,
                    screen_height=screen_height,
                ):
                    continue
                page_type = item.get("page_type")
                # 有 Catalog 页型时写入 expected_state_hint，供覆盖寻页／覆盖统计对齐。
                if isinstance(page_type, str) and page_type.strip():
                    expected_hint = page_type.strip()
                    purpose = (
                        f"打开可见底栏导航「{label}」并重新截图，预期页面类型:{expected_hint}"
                    )
                else:
                    expected_hint = f"tab:{label}"
                    purpose = f"打开可见底栏导航「{label}」并重新截图"
                _append_candidate(
                    target=item,
                    semantic_name=f"底栏「{label}」",
                    purpose=purpose,
                    expected_state_hint=expected_hint,
                    frontier_scope="app",
                )
            if navigation:
                key_elements.append("accessibility:bottom_navigation")
        # 有 overlay_close 时仍禁止签发中部内容卡，避免点穿广告主体。
        if overlay_close is None and isinstance(content_cards, tuple):
            list_page = (
                trusted_page_type
                if isinstance(trusted_page_type, str) and trusted_page_type.strip()
                else (
                    analysis.visual_page_type
                    if isinstance(analysis.visual_page_type, str)
                    else None
                )
            )
            detail_hint = "content_detail"
            if isinstance(list_page, str) and list_page.strip():
                mapped_detail = _PAGE_TYPE_REGISTRY.default_detail_type(list_page.strip())
                if isinstance(mapped_detail, str) and mapped_detail.strip():
                    detail_hint = mapped_detail.strip()
            for item in content_cards:
                _append_candidate(
                    target=item,
                    semantic_name="普通内容卡片",
                    purpose=(
                        "打开已验证含图片和长标题的普通内容卡片，已排除广告下载充值语义"
                        f"；预期页面类型:{detail_hint}"
                    ),
                    expected_state_hint=detail_hint,
                    frontier_scope="page",
                )
            if content_cards:
                key_elements.append("accessibility:ordinary_content_card")
        surface_facts["trusted_accessibility_controls"] = {
            "artifact_id": verified.artifact.artifact_id,
            "contract_id": controls["contract_id"],
            "package_name": controls["package_name"],
            "bottom_navigation_count": len(navigation) if isinstance(navigation, tuple) else 0,
            "bottom_navigation": (
                tuple(dict(item) for item in navigation) if isinstance(navigation, tuple) else ()
            ),
            "content_card_count": (
                len(content_cards)
                if overlay_close is None and isinstance(content_cards, tuple)
                else 0
            ),
            "labeled_dismiss_count": len(dismiss_targets),
            "overlay_close": isinstance(overlay_close, dict),
            "page_type": trusted_page_type,
            "selected_page_type": controls.get("selected_page_type"),
            "pixel_selected_page_type": controls.get("pixel_selected_page_type"),
            "pixel_selected_labels": controls.get("pixel_selected_labels"),
        }

    resolved_page_type = _prefer_trusted_visual_page_type(
        vision_page_type=analysis.visual_page_type,
        trusted_page_type=trusted_page_type if isinstance(trusted_page_type, str) else None,
        trusted_selected_page_type=_trusted_bottom_navigation_selected_page_type(controls),
    )
    fused_candidates, fusion_facts = _fuse_app_visual_candidates(
        candidates=tuple(candidates),
        trusted_candidate_ids=trusted_candidate_ids,
        max_candidates=max_candidates,
        selected_navigation_labels=_trusted_bottom_navigation_selected_labels(controls),
    )
    surface_facts["candidate_fusion"] = fusion_facts
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=resolved_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=tuple(dict.fromkeys(key_elements)),
        candidates=fused_candidates,
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=(
            page_state_override
            if page_state_override is not None
            else ("popup" if trusted_overlay_signature is not None else analysis.page_state)
        ),
        overlay_signature=analysis.overlay_signature or trusted_overlay_signature,
        surface_facts=surface_facts,
    )


def _bottom_nav_center_clears_overlay(
    *,
    target: Mapping[str, object],
    overlay_pixel_bounds: Mapping[str, object] | None,
    surface_facts: Mapping[str, object],
    screen_width: int,
    screen_height: int,
) -> bool:
    """底栏 Tab 中心不得落在关层控件或顶层广告矩形内。"""

    raw_bounds = target.get("bounds")
    if not isinstance(raw_bounds, Mapping):
        return False
    try:
        left = float(raw_bounds["left"])
        top = float(raw_bounds["top"])
        right = float(raw_bounds["right"])
        bottom = float(raw_bounds["bottom"])
    except (KeyError, TypeError, ValueError):
        return False
    if right <= left or bottom <= top or screen_width <= 0 or screen_height <= 0:
        return False
    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    if isinstance(overlay_pixel_bounds, Mapping):
        try:
            o_left = float(overlay_pixel_bounds["left"])
            o_top = float(overlay_pixel_bounds["top"])
            o_right = float(overlay_pixel_bounds["right"])
            o_bottom = float(overlay_pixel_bounds["bottom"])
        except (KeyError, TypeError, ValueError):
            o_left = o_top = o_right = o_bottom = None  # type: ignore[assignment]
        else:
            if o_left <= center_x <= o_right and o_top <= center_y <= o_bottom:
                return False
    topmost = surface_facts.get("topmost_ad_popup")
    if isinstance(topmost, Mapping) and topmost.get("is_ad") is True:
        raw_ad = topmost.get("normalized_bounds")
        if isinstance(raw_ad, Mapping):
            try:
                ad_bounds = NormalizedBounds.from_dict(raw_ad)
            except (TypeError, ValueError):
                ad_bounds = None
            if ad_bounds is not None:
                nx = center_x / float(screen_width)
                ny = center_y / float(screen_height)
                if (
                    ad_bounds.left <= nx <= ad_bounds.right
                    and ad_bounds.top <= ny <= ad_bounds.bottom
                ):
                    return False
    return True


def _trusted_bottom_navigation_selected_page_type(
    controls: Mapping[str, Any] | None,
) -> str | None:
    """只从可信底栏自身的 selected 或同帧高亮提取选中页型。"""

    if not isinstance(controls, Mapping):
        return None
    navigation = controls.get("bottom_navigation")
    if not isinstance(navigation, tuple) or not navigation:
        return None
    pixel_selected_labels = controls.get("pixel_selected_labels")
    selected_labels = (
        {
            str(value).strip().casefold()
            for value in pixel_selected_labels
            if isinstance(value, str) and value.strip()
        }
        if isinstance(pixel_selected_labels, (list, tuple))
        else set()
    )
    selected_page_types: list[str] = []
    for item in navigation:
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip().casefold()
        if item.get("selected") is not True and label not in selected_labels:
            continue
        page_type = item.get("page_type")
        if isinstance(page_type, str) and page_type.strip():
            selected_page_types.append(page_type.strip())
    unique_page_types = tuple(dict.fromkeys(selected_page_types))
    return unique_page_types[0] if len(unique_page_types) == 1 else None


def _trusted_bottom_navigation_selected_labels(
    controls: Mapping[str, Any] | None,
) -> frozenset[str]:
    """只返回真实底栏的 selected／同帧像素高亮标签。"""

    if not isinstance(controls, Mapping):
        return frozenset()
    navigation = controls.get("bottom_navigation")
    if not isinstance(navigation, tuple) or not navigation:
        return frozenset()
    pixel_selected_labels = controls.get("pixel_selected_labels")
    selected_labels = {
        str(value).strip().casefold()
        for value in pixel_selected_labels
        if isinstance(value, str) and value.strip()
    } if isinstance(pixel_selected_labels, (list, tuple)) else set()
    selected_labels.update(
        str(item.get("label") or "").strip().casefold()
        for item in navigation
        if isinstance(item, Mapping) and item.get("selected") is True
    )
    return frozenset(label for label in selected_labels if label)


def _prefer_trusted_visual_page_type(
    *,
    vision_page_type: str | None,
    trusted_page_type: str | None,
    trusted_selected_page_type: str | None = None,
) -> str | None:
    """底栏选中事实可纠正 Vision 的同层页型误判，其他推断只纠正 home。"""

    trusted = (trusted_page_type or "").strip()
    selected = (trusted_selected_page_type or "").strip()
    vision = (vision_page_type or "").strip()
    stable_navigation_types = {
        "home",
        "content_list",
        "video_list",
        "post_list",
        "activity",
        "profile",
    }
    if selected:
        if not vision:
            return selected
        if (
            vision.casefold() in stable_navigation_types
            and selected.casefold() in stable_navigation_types
        ):
            # selected 属性或同帧像素唯一高亮比模型分类更直接，纠正
            # “视频-profile”这类页面名称正确但页型串栏的问题。
            return selected
    if not trusted:
        return vision_page_type
    if not vision:
        return trusted
    if vision.casefold() == trusted.casefold():
        return vision
    if vision.casefold() == "home" and trusted.casefold() in {
        "content_list",
        "content_detail",
        "video_list",
        "video_detail",
        "post_list",
        "activity",
        "profile",
        "settings",
        "payment",
    }:
        return trusted
    return vision


def _validate_capture_ref(capture: CaptureRef) -> None:
    if not isinstance(capture, CaptureRef):
        raise TypeError("capture 必须为 CaptureRef")
    if capture.capture_kind is not CaptureKind.APP or capture.surface_kind is SurfaceKind.WEB:
        raise AppScreenshotUnderstandingError(
            "Screenshot Understanding 只接受 App Capture",
            error_code="SCREENSHOT_ARTIFACT_MISSING",
        )


def _validate_analysis(
    analysis: AppVisualAnalysis,
    request: UnderstandAppScreenshotRequest,
) -> None:
    if not isinstance(analysis, AppVisualAnalysis):
        raise AppScreenshotUnderstandingError(
            "Screenshot Understanding Provider 返回类型无效",
            error_code="INVALID_VISUAL_OUTPUT",
        )
    if (
        analysis.visual_page_type is not None
        and analysis.visual_page_type not in get_default_page_type_registry().all_codes
    ):
        raise AppScreenshotUnderstandingError(
            "Screenshot Understanding Provider 返回未知页面类型",
            error_code="INVALID_VISUAL_OUTPUT",
        )
    if (
        analysis.capture_id != request.capture_id
        or analysis.screenshot_artifact_id != request.screenshot_artifact_id
        or analysis.image_signature != request.image_signature
        or analysis.width != request.width
        or analysis.height != request.height
        or analysis.orientation != request.orientation
        or analysis.package_name != request.package_name
        or analysis.activity != request.activity
        or len(analysis.candidates) > request.max_candidates
        or any(item.action_type not in request.allowed_actions for item in analysis.candidates)
    ):
        raise AppScreenshotUnderstandingError(
            "Screenshot Understanding Provider 输出与请求身份不一致",
            error_code="INVALID_VISUAL_OUTPUT",
        )
