from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace

import pytest
from PIL import Image, ImageDraw

from services.api.app.application.app_capture_persistence import (
    PersistedAppCapture,
    VerifiedAppUiTree,
)
from services.api.app.application.app_screenshot_understanding import (
    AppScreenshotUnderstandingError,
    AppScreenshotUnderstandingService,
    UnderstandAppScreenshotRequest,
    _attach_trusted_visual_navigation_selection,
    _prefer_trusted_visual_page_type,
)
from services.api.app.application.app_visual_close_calibration import (
    calibrate_popup_close_candidates,
    detect_isolated_cross_controls,
)
from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)
SIGNATURE = f"sha256:{hashlib.sha256(PNG_1X1).hexdigest()}"

# 按尺寸缓存纯色 PNG，避免多用例重复编码且保证与 PersistedAppCapture 事实一致
_PNG_BY_SIZE: dict[tuple[int, int], bytes] = {(1, 1): PNG_1X1}


def _solid_png(*, width: int, height: int) -> bytes:
    """生成与声明宽高一致的最小合法 PNG。"""

    image = Image.new("RGB", (width, height), color=(8, 8, 8))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _fixture_png(*, width: int, height: int) -> bytes:
    key = (width, height)
    cached = _PNG_BY_SIZE.get(key)
    if cached is None:
        cached = _solid_png(width=width, height=height)
        _PNG_BY_SIZE[key] = cached
    return cached


def _matched_source(
    *,
    width: int,
    height: int,
    capture: CaptureRef | None = None,
    package_name: str = "com.example.target",
    activity: str = ".MainActivity",
    png: bytes | None = None,
) -> tuple[PersistedAppCapture, bytes]:
    """构造 PNG 实际尺寸与持久化 width/height/checksum 一致的 Capture。"""

    content = png if png is not None else _fixture_png(width=width, height=height)
    checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
    capt = capture if capture is not None else _capture_ref()
    source = PersistedAppCapture(
        capture=capt,
        agent_run_id="run-1",
        screenshot=PersistedEvidenceArtifact(
            artifact_id="artifact-1",
            artifact_type="screenshot",
            storage_path="task-1/capture-1/artifact-1.png",
            content_type="image/png",
            checksum=checksum,
            size_bytes=len(content),
            metadata={"encoding": "binary"},
        ),
        width=width,
        height=height,
        orientation="portrait",
        image_signature=checksum,
        package_name=package_name,
        activity=activity,
        bootstrap_row_version=7,
    )
    return source, content


def test_selected_bottom_tab_corrects_cross_tab_visual_page_type() -> None:
    """受信底栏选中事实应纠正“视频-profile”这类模型串栏。"""

    assert (
        _prefer_trusted_visual_page_type(
            vision_page_type="profile",
            trusted_page_type="video_list",
            trusted_selected_page_type="video_list",
        )
        == "video_list"
    )


def test_inferred_content_list_does_not_override_non_home_visual_type() -> None:
    """仅由内容卡推断的弱事实仍不得覆盖非 home 的模型分类。"""

    assert (
        _prefer_trusted_visual_page_type(
            vision_page_type="profile",
            trusted_page_type="content_list",
        )
        == "profile"
    )


@pytest.mark.asyncio
async def test_understanding_keeps_post_list_without_bottom_navigation() -> None:
    """页面内选中“推荐”且无底栏时，不得把帖子列表覆盖成首页。"""

    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(
        width=1080,
        height=1920,
        capture=capture,
        package_name="uk.pnixc.juywgn",
    )
    xml = """<hierarchy>
      <node package="uk.pnixc.juywgn" content-desc="推荐" selected="true"
            clickable="true" enabled="true" bounds="[30,180][300,280]"/>
    </hierarchy>"""

    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(visual_page_type="post_list"),
    ).understand(capture)

    assert analysis.visual_page_type == "post_list"
    facts = analysis.surface_facts["trusted_accessibility_controls"]
    assert facts["bottom_navigation_count"] == 0
    assert facts["bottom_navigation"] == ()
    assert facts["selected_page_type"] == "home"


@pytest.mark.asyncio
async def test_understanding_uses_selected_real_bottom_navigation_to_correct_page_type() -> None:
    """可信底栏真实选中项仍可纠正 Vision 的同层页型。"""

    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(
        width=1080,
        height=1920,
        capture=capture,
        package_name="uk.pnixc.juywgn",
    )
    xml = """<hierarchy>
      <node package="uk.pnixc.juywgn" content-desc="首页"
            clickable="true" enabled="true" bounds="[0,1760][360,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="视频" selected="true"
            clickable="true" enabled="true" bounds="[360,1760][720,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="我的"
            clickable="true" enabled="true" bounds="[720,1760][1080,1920]"/>
    </hierarchy>"""

    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(visual_page_type="post_list"),
    ).understand(capture)

    assert analysis.visual_page_type == "video_list"
    facts = analysis.surface_facts["trusted_accessibility_controls"]
    assert facts["bottom_navigation_count"] == 3
    assert facts["selected_page_type"] == "video_list"


def _popup_cross_png(*, second_cross: bool = False, twin_equal: bool = False) -> bytes:
    image = Image.new("RGB", (240, 400), color=(64, 64, 64))
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 80, 216, 260), fill=(95, 70, 70))
    draw.line((108, 288, 132, 312), fill=(255, 255, 255), width=5)
    draw.line((132, 288, 108, 312), fill=(255, 255, 255), width=5)
    if twin_equal:
        # 与主 X 分数接近，用于歧义 fail-closed
        draw.line((108, 248, 132, 272), fill=(255, 255, 255), width=5)
        draw.line((132, 248, 108, 272), fill=(255, 255, 255), width=5)
    elif second_cross:
        draw.line((28, 328, 52, 352), fill=(255, 255, 255), width=5)
        draw.line((52, 328, 28, 352), fill=(255, 255, 255), width=5)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _non_square_multi_cross_png() -> bytes:
    """非方形截图中同时包含内容字母 X 与公告关闭 X。"""

    image = Image.new("RGB", (1440, 3040), color=(32, 32, 32))
    draw = ImageDraw.Draw(image)
    # 左上 X 模拟内容卡标题中的字母，故意使用非方形笔画框。
    for start, end in (
        ((11, 1143), (34, 1173)),
        ((34, 1143), (11, 1173)),
    ):
        draw.line((*start, *end), fill=(255, 255, 255), width=4)
    # 右下 X 模拟 d266 底部公告条的真实关闭控件，物理尺寸为正方形。
    for start, end in (
        ((1342, 2585), (1377, 2620)),
        ((1377, 2585), (1342, 2620)),
    ):
        draw.line((*start, *end), fill=(255, 255, 255), width=4)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _popup_circular_close_png(*, second_button: bool = False) -> bytes:
    """白底黑 X 圆形关闭钮（X 不在亮色连通域上）。"""

    image = Image.new("RGB", (240, 400), color=(32, 32, 32))
    draw = ImageDraw.Draw(image)
    draw.rectangle((28, 70, 212, 270), fill=(88, 64, 64))
    buttons = [(120, 298, 16)]
    if second_button:
        # 次级圆钮偏右下，分数应低于居中主钮，且避开广告主体保护区
        buttons.append((200, 310, 14))
    for center_x, center_y, radius in buttons:
        draw.ellipse(
            (
                center_x - radius,
                center_y - radius,
                center_x + radius,
                center_y + radius,
            ),
            fill=(250, 250, 250),
        )
        arm = max(6, radius // 2)
        draw.line(
            (center_x - arm, center_y - arm, center_x + arm, center_y + arm),
            fill=(12, 12, 12),
            width=3,
        )
        draw.line(
            (center_x + arm, center_y - arm, center_x - arm, center_y + arm),
            fill=(12, 12, 12),
            width=3,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _navigation_accent_png(*, accent_bounds: tuple[int, int, int, int]) -> bytes:
    image = Image.new("RGB", (1080, 1920), color=(248, 248, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle(accent_bounds, fill=(255, 74, 170))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _visual_bottom_navigation_analysis(*, screenshot_png: bytes) -> AppVisualAnalysis:
    signature = f"sha256:{hashlib.sha256(screenshot_png).hexdigest()}"
    navigation = (
        ("底部Tab二次元", "home", 0.00, 0.16),
        ("底部Tab视频", "video_list", 0.16, 0.32),
        ("底部Tab萝莉岛", "content_list", 0.32, 0.48),
        ("底部Tab活动", "activity", 0.48, 0.64),
        ("底部Tab朋友圈", "post_list", 0.64, 0.80),
        ("底部Tab我的", "profile", 0.80, 0.98),
    )
    candidates = tuple(
        AppVisualCandidate.build(
            source_capture_id="capture-visual-navigation",
            source_image_signature=signature,
            source_width=1080,
            source_height=1920,
            source_orientation="portrait",
            semantic_name=semantic_name,
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(
                left=left,
                top=0.92,
                right=right,
                bottom=0.99,
            ),
            confidence=0.9,
            risk_level=InteractionRiskLevel.LOW,
            purpose="切换底栏页面",
            expected_state_hint=page_type,
            frontier_scope="app",
        )
        for semantic_name, page_type, left, right in navigation
    )
    return AppVisualAnalysis.build(
        capture_id="capture-visual-navigation",
        screenshot_artifact_id="artifact-visual-navigation",
        image_signature=signature,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="模型误判为首页",
        key_elements=("底栏",),
        candidates=candidates,
        provider_id="recording-provider",
        model_id="recording-model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="stable",
    )


def test_current_visual_bottom_navigation_accent_corrects_model_page_type() -> None:
    screenshot_png = _navigation_accent_png(
        accent_bounds=(190, 1780, 315, 1885),
    )
    analysis = _visual_bottom_navigation_analysis(screenshot_png=screenshot_png)

    corrected = _attach_trusted_visual_navigation_selection(
        analysis=analysis,
        screenshot_png=screenshot_png,
    )

    assert corrected.visual_page_type == "video_list"
    selection = corrected.surface_facts["trusted_visual_navigation_selection"]
    assert selection["contract_id"] == "app-visual-navigation-selection.v1"
    assert selection["selected_page_type"] == "video_list"
    assert selection["selected_label"] == "视频"
    assert selection["source_capture_id"] == analysis.capture_id


def test_ambiguous_visual_bottom_navigation_accent_does_not_override_model() -> None:
    image = Image.new("RGB", (1080, 1920), color=(248, 248, 248))
    draw = ImageDraw.Draw(image)
    draw.rectangle((190, 1780, 315, 1885), fill=(255, 74, 170))
    draw.rectangle((365, 1780, 490, 1885), fill=(255, 74, 170))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    screenshot_png = buffer.getvalue()
    analysis = _visual_bottom_navigation_analysis(screenshot_png=screenshot_png)

    corrected = _attach_trusted_visual_navigation_selection(
        analysis=analysis,
        screenshot_png=screenshot_png,
    )

    assert corrected is analysis


def _popup_analysis_for_pixel_calibration(
    *, semantic_name: str = "关闭广告弹窗", purpose: str = "关闭当前广告并继续探索"
) -> AppVisualAnalysis:
    signature = "sha256:" + "b" * 64
    wrong_close = AppVisualCandidate.build(
        source_capture_id="capture-pixel-x",
        source_image_signature=signature,
        source_width=240,
        source_height=400,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(
            left=0.45,
            top=0.2,
            right=0.55,
            bottom=0.26,
        ),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose=purpose,
        control_role="dismiss",
        expected_state_hint="home",
    )
    ad_body = AppVisualCandidate.build(
        source_capture_id="capture-pixel-x",
        source_image_signature=signature,
        source_width=240,
        source_height=400,
        source_orientation="portrait",
        semantic_name="点击广告主体",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(
            left=0.1,
            top=0.2,
            right=0.9,
            bottom=0.65,
        ),
        confidence=0.9,
        risk_level=InteractionRiskLevel.HIGH,
        purpose="打开广告落地页",
    )
    return AppVisualAnalysis.build(
        capture_id="capture-pixel-x",
        screenshot_artifact_id="artifact-pixel-x",
        image_signature=signature,
        width=240,
        height=400,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="首页被广告弹窗遮挡",
        key_elements=("广告弹窗",),
        candidates=(wrong_close, ad_body),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="popup",
        overlay_signature="center-ad",
        surface_facts={"popup_visible": True},
    )


@pytest.mark.parametrize("label", ["关闭", "Fermer", "Schließen", "×"])
def test_pixel_cross_calibration_corrects_close_without_touching_ad_body(label: str) -> None:
    screenshot = _popup_cross_png()
    analysis = _popup_analysis_for_pixel_calibration(semantic_name=label, purpose=label)

    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=240,
        expected_height=400,
    )
    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert len(detected) == 1
    close_candidate = calibrated.candidates[0]
    assert close_candidate.normalized_bounds is not None
    assert close_candidate.normalized_bounds.center == pytest.approx((0.5, 0.75), abs=0.02)
    assert close_candidate.semantic_name == "关闭广告弹窗"
    assert close_candidate.control_role == "dismiss"
    assert close_candidate.expected_state_hint == "close_popup_x"
    assert calibrated.candidates[1] is analysis.candidates[1]
    assert calibrated.candidates[1].semantic_name == "点击广告主体"
    assert calibrated.candidates[1].risk_level is InteractionRiskLevel.HIGH
    assert calibrated.candidates[1].normalized_bounds == analysis.candidates[1].normalized_bounds
    assert "当前截图唯一孤立 X 像素结构校准" in close_candidate.purpose
    assert "pixel_cross_close_calibration" in calibrated.surface_facts
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["precise_tap_refined"] is True
    round_tripped = AppVisualAnalysis.from_dict(calibrated.to_dict())
    assert round_tripped.result_hash == calibrated.result_hash
    assert round_tripped.candidates[0].control_role == "dismiss"


def test_pixel_cross_calibration_canonicalizes_already_aligned_provider_close() -> None:
    analysis = _popup_analysis_for_pixel_calibration()
    original = analysis.candidates[0]
    aligned = AppVisualCandidate.build(
        source_capture_id=original.source_capture_id,
        source_image_signature=original.source_image_signature,
        source_width=original.source_width,
        source_height=original.source_height,
        source_orientation=original.source_orientation,
        semantic_name="关闭活动弹窗",
        action_type=original.action_type,
        normalized_bounds=NormalizedBounds(
            left=0.45,
            top=0.71,
            right=0.55,
            bottom=0.77,
        ),
        confidence=original.confidence,
        risk_level=original.risk_level,
        purpose="关闭促销弹层回到首页",
        control_role=original.control_role,
        expected_state_hint="home",
        frontier_scope=original.frontier_scope,
    )

    aligned_analysis = AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=(aligned, analysis.candidates[1]),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=analysis.surface_facts,
    )

    calibrated = calibrate_popup_close_candidates(
        analysis=aligned_analysis,
        screenshot_png=_popup_cross_png(),
    )

    close_candidate = calibrated.candidates[0]
    assert close_candidate.semantic_name == "关闭广告弹窗"
    assert close_candidate.control_role == "dismiss"
    assert close_candidate.expected_state_hint == "close_popup_x"
    assert "当前截图唯一孤立 X 像素结构校准" in close_candidate.purpose
    assert "pixel_cross_close_calibration" in calibrated.surface_facts


def test_pixel_cross_calibration_issues_top_scored_when_cross_scores_are_close() -> None:
    """双 X 分数接近时仍签发 top-1 精准点，不得整段放弃校准。"""

    analysis = _popup_analysis_for_pixel_calibration()

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=_popup_cross_png(twin_equal=True),
    )

    assert calibrated is not analysis
    close_candidate = calibrated.candidates[0]
    assert close_candidate.normalized_bounds is not None
    # 下方主 X（约 y=0.75）应压过上方孪生 X
    assert close_candidate.normalized_bounds.center[1] == pytest.approx(0.75, abs=0.06)
    fact = calibrated.surface_facts["pixel_cross_close_calibration"]
    assert fact["ambiguity_count"] == 2
    assert fact["precise_tap_refined"] is True
    assert "detected_bounds" in fact


def test_pixel_cross_calibration_picks_top_scored_when_secondary_cross_weaker() -> None:
    analysis = _popup_analysis_for_pixel_calibration()

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=_popup_cross_png(second_cross=True),
    )

    assert calibrated is not analysis
    close_candidate = calibrated.candidates[0]
    assert close_candidate.normalized_bounds is not None
    assert close_candidate.normalized_bounds.center == pytest.approx((0.5, 0.75), abs=0.05)
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["ambiguity_count"] == 2


def test_pixel_cross_calibration_uses_pixel_aspect_on_non_square_multi_cross() -> None:
    """非方形截图的物理正方形关闭 X 应胜过内容字母 X。"""

    screenshot = _non_square_multi_cross_png()
    signature = "sha256:" + hashlib.sha256(screenshot).hexdigest()
    close = AppVisualCandidate.build(
        source_capture_id="capture-non-square-crosses",
        source_image_signature=signature,
        source_width=1440,
        source_height=3040,
        source_orientation="portrait",
        semantic_name="提示条关闭",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.90, 0.88, 0.98, 0.91),
        confidence=0.8,
        risk_level=InteractionRiskLevel.LOW,
        purpose="关闭底部提示条",
        control_role="dismiss",
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-non-square-crosses",
        screenshot_artifact_id="artifact-non-square-crosses",
        image_signature=signature,
        width=1440,
        height=3040,
        orientation="portrait",
        visual_page_type="post_list",
        visual_summary="社区帖子列表",
        key_elements=("底部公告条",),
        candidates=(close,),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="stable",
    )

    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=analysis.width,
        expected_height=analysis.height,
    )
    assert len(detected) == 2
    assert detected[0].center[0] < 0.1
    assert detected[1].center == pytest.approx((0.944, 0.856), abs=0.02)
    assert (detected[1].right - detected[1].left) * analysis.width == pytest.approx(
        (detected[1].bottom - detected[1].top) * analysis.height,
        abs=1,
    )

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert calibrated is not analysis
    assert calibrated.candidates[0].normalized_bounds is not None
    assert calibrated.candidates[0].normalized_bounds.center == pytest.approx(
        (0.944, 0.856),
        abs=0.02,
    )
    fact = calibrated.surface_facts["pixel_cross_close_calibration"]
    assert fact["ambiguity_count"] == 2
    assert fact["detected_bounds"]["left"] == pytest.approx(1341 / 1440, abs=0.01)


def test_circular_close_button_detects_and_rebinds_provider_close() -> None:
    screenshot = _popup_circular_close_png()
    analysis = _popup_analysis_for_pixel_calibration()

    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=240,
        expected_height=400,
    )
    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert len(detected) == 1
    assert detected[0].center == pytest.approx((0.5, 0.745), abs=0.04)
    close_candidate = calibrated.candidates[0]
    assert close_candidate.normalized_bounds is not None
    assert close_candidate.normalized_bounds.center == pytest.approx(
        (0.5, 0.745),
        abs=0.04,
    )
    assert close_candidate.semantic_name == "关闭广告弹窗"
    assert close_candidate.control_role == "dismiss"
    assert "当前截图唯一孤立 X 像素结构校准" in close_candidate.purpose
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["mode"] == "rebind"


def test_circular_close_button_issues_dismiss_when_provider_has_no_close() -> None:
    screenshot = _popup_circular_close_png()
    analysis = _popup_analysis_for_pixel_calibration()
    ad_only = AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=(analysis.candidates[1],),
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=analysis.surface_facts,
    )

    calibrated = calibrate_popup_close_candidates(
        analysis=ad_only,
        screenshot_png=screenshot,
    )

    assert len(calibrated.candidates) == 2
    issued = calibrated.candidates[0]
    assert issued.semantic_name == "关闭广告弹窗"
    assert issued.control_role == "dismiss"
    assert issued.expected_state_hint == "close_popup_x"
    assert issued.discovery_source == "clearance_synthetic"
    assert issued.normalized_bounds is not None
    assert issued.normalized_bounds.center == pytest.approx((0.5, 0.745), abs=0.04)
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["mode"] == "issue"
    assert "pixel:issued_isolated_cross_close" in calibrated.key_elements
    assert calibrated.candidates[1] is ad_only.candidates[0]


def test_circular_close_issues_even_when_vision_marks_home_without_popup() -> None:
    """Vision 误标 home 且无 popup 事实时，唯一圆形 X 仍须签发关闭。"""

    screenshot = _popup_circular_close_png()
    signature = "sha256:" + "c" * 64
    feed = AppVisualCandidate.build(
        source_capture_id="capture-home-miss",
        source_image_signature=signature,
        source_width=240,
        source_height=400,
        source_orientation="portrait",
        semantic_name="点击推荐视频",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.2, 0.3, 0.8, 0.55),
        confidence=0.88,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="打开推荐内容",
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-home-miss",
        screenshot_artifact_id="artifact-home-miss",
        image_signature=signature,
        width=240,
        height=400,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="模型误判为稳定首页",
        key_elements=("推荐流",),
        candidates=(feed,),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert calibrated.page_state == "popup"
    assert calibrated.surface_facts.get("popup_visible") is True
    assert calibrated.candidates[0].semantic_name == "关闭广告弹窗"
    assert calibrated.candidates[0].control_role == "dismiss"
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["mode"] == "issue"


def test_dual_circular_close_picks_top_scored_primary() -> None:
    screenshot = _popup_circular_close_png(second_button=True)
    analysis = _popup_analysis_for_pixel_calibration()

    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=240,
        expected_height=400,
    )
    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert len(detected) >= 2
    assert calibrated is not analysis
    close_candidate = calibrated.candidates[0]
    assert close_candidate.normalized_bounds is not None
    assert close_candidate.normalized_bounds.center == pytest.approx((0.5, 0.745), abs=0.05)
    assert calibrated.surface_facts["pixel_cross_close_calibration"]["ambiguity_count"] >= 2


def test_medium_large_frame_covering_x_still_calibrates_circular_close() -> None:
    """MEDIUM 大框包住 X 时不得整段跳过；仅广告主体／超大框才保护。"""

    screenshot = _popup_circular_close_png()
    signature = "sha256:" + "d" * 64
    medium_cover = AppVisualCandidate.build(
        source_capture_id="capture-medium-cover",
        source_image_signature=signature,
        source_width=240,
        source_height=400,
        source_orientation="portrait",
        semantic_name="推荐卡片",
        action_type=InteractionOperation.CLICK,
        # 面积约 0.36，但非广告主体文案且非超大保护阈值的「误伤」场景：
        # 旧逻辑凡 MEDIUM 即保护；新逻辑 MEDIUM 需 area>=0.18 才会保护。
        # 这里用面积 0.12 的 MEDIUM 框盖住 X 中心，验证不应剔除。
        normalized_bounds=NormalizedBounds(0.35, 0.65, 0.65, 0.85),
        confidence=0.9,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="打开推荐",
    )
    analysis = AppVisualAnalysis.build(
        capture_id="capture-medium-cover",
        screenshot_artifact_id="artifact-medium-cover",
        image_signature=signature,
        width=240,
        height=400,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="首页被弹层遮挡",
        key_elements=("弹层",),
        candidates=(medium_cover,),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="popup",
        overlay_signature="center-ad",
        surface_facts={"popup_visible": True},
    )

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )

    assert calibrated is not analysis
    assert calibrated.candidates[0].semantic_name == "关闭广告弹窗"
    assert calibrated.candidates[0].control_role == "dismiss"


def _tall_ad_circular_close_png() -> bytes:
    """春药商城式大图广告：圆 X 落在 y≈0.58（旧 mid_band 0.66 会误杀）。"""

    image = Image.new("RGB", (240, 400), color=(24, 24, 24))
    draw = ImageDraw.Draw(image)
    # 高广告主体，底部约在 0.52。
    draw.rectangle((36, 40, 204, 210), fill=(96, 72, 88))
    center_x, center_y, radius = 120, 232, 16
    draw.ellipse(
        (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        ),
        fill=(250, 250, 250),
    )
    arm = 8
    draw.line(
        (center_x - arm, center_y - arm, center_x + arm, center_y + arm),
        fill=(12, 12, 12),
        width=3,
    )
    draw.line(
        (center_x + arm, center_y - arm, center_x - arm, center_y + arm),
        fill=(12, 12, 12),
        width=3,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _gray_grid_circular_close_png() -> bytes:
    """宫格遮罩浅灰圆 X（mean≈155），旧 bright_mean>=220 会漏检。"""

    image = Image.new("RGB", (240, 400), color=(8, 8, 8))
    draw = ImageDraw.Draw(image)
    # 宫格主体
    for row in range(3):
        for col in range(4):
            x0 = 16 + col * 54
            y0 = 36 + row * 70
            draw.rectangle((x0, y0, x0 + 48, y0 + 58), fill=(48, 40, 44))
    center_x, center_y, radius = 120, 322, 15
    draw.ellipse(
        (
            center_x - radius,
            center_y - radius,
            center_x + radius,
            center_y + radius,
        ),
        fill=(168, 168, 168),
    )
    arm = 8
    draw.line(
        (center_x - arm, center_y - arm, center_x + arm, center_y + arm),
        fill=(24, 24, 24),
        width=2,
    )
    draw.line(
        (center_x + arm, center_y - arm, center_x - arm, center_y + arm),
        fill=(24, 24, 24),
        width=2,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_gray_grid_circular_x_calibrates_near_y080() -> None:
    """浅灰宫格下方圆 X（y≈0.80）须检出并签发，避免连点大框空处。"""

    screenshot = _gray_grid_circular_close_png()
    signature = "sha256:" + "a" * 64
    analysis = AppVisualAnalysis.build(
        capture_id="capture-gray-grid-x",
        screenshot_artifact_id="artifact-gray-grid-x",
        image_signature=signature,
        width=240,
        height=400,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="广告宫格弹层，底部圆X可关闭",
        key_elements=("弹层",),
        candidates=(),
        provider_id="provider",
        model_id="model",
        package_name="dark.NVYCy.AmBPu",
        activity=".MainActivity",
        page_state="popup",
        overlay_signature="grid-ad",
        surface_facts={
            "popup_visible": True,
            "dialog_kind": "ad_popup",
            "topmost_ad_popup": {
                "is_ad": True,
                "normalized_bounds": {
                    "left": 0.05,
                    "top": 0.08,
                    "right": 0.95,
                    "bottom": 0.72,
                },
            },
        },
    )
    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=240,
        expected_height=400,
    )
    assert len(detected) >= 1
    assert any(0.76 <= item.center[1] <= 0.86 for item in detected)
    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )
    assert calibrated is not analysis
    bounds = calibrated.candidates[0].normalized_bounds
    assert bounds is not None
    cy = (bounds.top + bounds.bottom) / 2.0
    assert 0.76 <= cy <= 0.86
    fact = calibrated.surface_facts["pixel_cross_close_calibration"]
    assert fact.get("close_kind") == "bottom_circular_x"


def test_tall_ad_circular_x_around_y058_still_calibrates() -> None:
    """大图广告下方圆 X（y≈0.58）须签发，不得当中部假 X 拒签。"""

    screenshot = _tall_ad_circular_close_png()
    signature = "sha256:" + "e" * 64
    analysis = AppVisualAnalysis.build(
        capture_id="capture-tall-ad-x",
        screenshot_artifact_id="artifact-tall-ad-x",
        image_signature=signature,
        width=240,
        height=400,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="首页被大图广告遮挡",
        key_elements=("弹层",),
        candidates=(),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="popup",
        overlay_signature="tall-center-ad",
        surface_facts={"popup_visible": True},
    )

    detected = detect_isolated_cross_controls(
        screenshot,
        expected_width=240,
        expected_height=400,
    )
    assert len(detected) >= 1
    assert any(0.52 <= item.center[1] <= 0.64 for item in detected)

    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )
    assert calibrated is not analysis
    assert calibrated.candidates[0].control_role == "dismiss"
    assert calibrated.candidates[0].normalized_bounds is not None
    cy = (
        calibrated.candidates[0].normalized_bounds.top
        + calibrated.candidates[0].normalized_bounds.bottom
    ) / 2.0
    assert 0.52 <= cy <= 0.64
    fact = calibrated.surface_facts["pixel_cross_close_calibration"]
    assert fact["mode"] == "issue"
    assert fact.get("close_kind") == "bottom_circular_x"
    assert fact.get("close_zone") in {
        "panel_below",
        "panel_bottom_edge",
        "screen_bottom_banner",
    }


def test_promo_purchase_with_bottom_x_still_calibrates() -> None:
    """「立即抢购」促销层若有面板下方真圆 X，须签发，不得整层禁像素。"""

    from services.api.app.application.app_visual_close_calibration import (
        is_no_close_auto_dismiss_promo,
        is_payment_or_paywall_surface,
    )

    screenshot = _tall_ad_circular_close_png()
    signature = "sha256:" + "d" * 64
    analysis = AppVisualAnalysis.build(
        capture_id="capture-promo-x",
        screenshot_artifact_id="artifact-promo-x",
        image_signature=signature,
        width=240,
        height=400,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="会员促销弹窗遮挡，立即抢购，底部圆形关闭",
        key_elements=("立即抢购", "禁果全网通"),
        candidates=(),
        provider_id="provider",
        model_id="model",
        package_name="dark.NVYCy.AmBPu",
        activity=".MainActivity",
        page_state="popup",
        overlay_signature="promo-purchase",
        surface_facts={
            "popup_visible": True,
            "dialog_kind": "promo_purchase",
            "topmost_ad_popup": {
                "is_ad": True,
                "normalized_bounds": {
                    "left": 0.15,
                    "top": 0.10,
                    "right": 0.85,
                    "bottom": 0.52,
                },
            },
        },
    )
    assert is_payment_or_paywall_surface(analysis) is True
    calibrated = calibrate_popup_close_candidates(
        analysis=analysis,
        screenshot_png=screenshot,
    )
    assert calibrated is not analysis
    fact = calibrated.surface_facts["pixel_cross_close_calibration"]
    assert fact.get("close_kind") == "bottom_circular_x"
    assert fact.get("close_zone") in {
        "panel_below",
        "panel_bottom_edge",
        "screen_bottom_banner",
    }
    # 已有像素签发后，不应再当「无 X 等自消」
    assert is_no_close_auto_dismiss_promo(calibrated) is False


def test_classify_close_zone_follows_panel_not_absolute_y() -> None:
    """同一相对位置（面板正下方）在不同绝对 Y 都应可点；中部内容区应拒。"""

    from services.api.app.application.app_visual_close_calibration import (
        classify_detected_close_zone,
    )

    signature = "sha256:" + "f" * 64
    panel = {"left": 0.12, "top": 0.18, "right": 0.88, "bottom": 0.48}
    analysis = AppVisualAnalysis.build(
        capture_id="capture-panel-rel",
        screenshot_artifact_id="artifact-panel-rel",
        image_signature=signature,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="弹层",
        key_elements=("弹层",),
        candidates=(),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="popup",
        overlay_signature="panel-rel",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "normalized_bounds": panel,
            },
        },
    )
    # 紧贴矮面板下方 → 绝对 Y 约 0.55，相对面板仍是 below
    low_panel_x = NormalizedBounds(left=0.46, top=0.50, right=0.54, bottom=0.58)
    ok, zone = classify_detected_close_zone(low_panel_x, analysis=analysis)
    assert ok is True
    assert zone == "panel_below"

    # 更高面板：X 绝对 Y≈0.78 仍相对 below
    tall_panel = {"left": 0.10, "top": 0.12, "right": 0.90, "bottom": 0.70}
    tall_analysis = AppVisualAnalysis.build(
        capture_id="capture-panel-rel-tall",
        screenshot_artifact_id="artifact-panel-rel-tall",
        image_signature=signature,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="home",
        visual_summary="弹层",
        key_elements=("弹层",),
        candidates=(),
        provider_id="provider",
        model_id="model",
        package_name="com.example.target",
        activity=".MainActivity",
        page_state="popup",
        overlay_signature="panel-rel-tall",
        surface_facts={
            "popup_visible": True,
            "topmost_ad_popup": {
                "is_ad": True,
                "normalized_bounds": tall_panel,
            },
        },
    )
    high_y_x = NormalizedBounds(left=0.46, top=0.74, right=0.54, bottom=0.82)
    ok2, zone2 = classify_detected_close_zone(high_y_x, analysis=tall_analysis)
    assert ok2 is True
    assert zone2 == "panel_below"

    # 面板内容中部假 X → 拒
    fake_mid = NormalizedBounds(left=0.46, top=0.30, right=0.54, bottom=0.38)
    ok3, zone3 = classify_detected_close_zone(fake_mid, analysis=analysis)
    assert ok3 is False
    assert zone3 == "panel_content_reject"


class SourceRepository:
    def __init__(
        self,
        source: PersistedAppCapture,
        *,
        ui_tree: VerifiedAppUiTree | None = None,
    ) -> None:
        self.source = source
        self.ui_tree = ui_tree

    async def load(self, *, capture: CaptureRef) -> PersistedAppCapture | None:
        del capture
        return self.source

    async def load_ui_tree(self, *, capture: CaptureRef) -> VerifiedAppUiTree | None:
        assert capture == self.source.capture
        return self.ui_tree


class ArtifactReader:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.reads = 0

    async def read(self, storage_path: str) -> bytes:
        self.reads += 1
        assert storage_path == "task-1/capture-1/artifact-1.png"
        return self.content


class RecordingProvider:
    def __init__(
        self,
        *,
        visual_page_type: str | None = "home",
        page_state: str = "unknown",
    ) -> None:
        self.requests: list[UnderstandAppScreenshotRequest] = []
        self.visual_page_type = visual_page_type
        self.page_state = page_state

    async def understand(self, request: UnderstandAppScreenshotRequest) -> AppVisualAnalysis:
        self.requests.append(request)
        return AppVisualAnalysis.build(
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            width=request.width,
            height=request.height,
            orientation=request.orientation,
            visual_page_type=self.visual_page_type,
            visual_summary="登录页",
            key_elements=("登录按钮",),
            candidates=(),
            provider_id="recording-provider",
            model_id="recording-model",
            package_name=request.package_name,
            activity=request.activity,
            page_state=self.page_state,
        )


class FixedCandidatesProvider:
    """向融合回归测试提供指定角色的 Vision 候选。"""

    def __init__(
        self,
        specs: tuple[dict[str, object], ...],
        *,
        visual_page_type: str | None = "home",
        page_state: str = "stable",
    ) -> None:
        self.specs = specs
        self.visual_page_type = visual_page_type
        self.page_state = page_state

    async def understand(self, request: UnderstandAppScreenshotRequest) -> AppVisualAnalysis:
        candidates = tuple(
            AppVisualCandidate.build(
                source_capture_id=request.capture_id,
                source_image_signature=request.image_signature,
                source_width=request.width,
                source_height=request.height,
                source_orientation=request.orientation,
                semantic_name=str(spec["semantic_name"]),
                action_type=InteractionOperation.CLICK,
                normalized_bounds=NormalizedBounds(
                    left=float(spec.get("left", 0.05)),
                    top=float(spec.get("top", 0.1)),
                    right=float(spec.get("right", 0.2)),
                    bottom=float(spec.get("bottom", 0.2)),
                ),
                confidence=0.8,
                risk_level=InteractionRiskLevel.LOW,
                purpose=str(spec.get("purpose", "Vision 候选")),
                control_role=(
                    str(spec["control_role"]) if spec.get("control_role") is not None else None
                ),
                expected_state_hint=(
                    str(spec["expected_state_hint"])
                    if spec.get("expected_state_hint") is not None
                    else None
                ),
                frontier_scope=str(spec.get("frontier_scope", "page")),
            )
            for spec in self.specs
        )
        return AppVisualAnalysis.build(
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            width=request.width,
            height=request.height,
            orientation=request.orientation,
            visual_page_type=self.visual_page_type,
            visual_summary="固定候选测试页",
            key_elements=("候选",),
            candidates=candidates,
            provider_id="fixed-candidates-provider",
            model_id="fixed-candidates-model",
            package_name=request.package_name,
            activity=request.activity,
            page_state=self.page_state,
        )


class MisalignedDismissProvider:
    def __init__(self, *, semantic_name: str = "我知道了") -> None:
        self.semantic_name = semantic_name
        self.original_candidate: AppVisualCandidate | None = None

    async def understand(self, request: UnderstandAppScreenshotRequest) -> AppVisualAnalysis:
        candidate = AppVisualCandidate.build(
            source_capture_id=request.capture_id,
            source_image_signature=request.image_signature,
            source_width=request.width,
            source_height=request.height,
            source_orientation=request.orientation,
            semantic_name=self.semantic_name,
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(
                left=0.562,
                top=0.294,
                right=0.852,
                bottom=0.319,
            ),
            confidence=0.99,
            risk_level=InteractionRiskLevel.LOW,
            purpose="关闭当前通知弹窗",
            control_role="dismiss",
            expected_state_hint="home",
        )
        self.original_candidate = candidate
        return AppVisualAnalysis.build(
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            width=request.width,
            height=request.height,
            orientation=request.orientation,
            visual_page_type="home",
            visual_summary="首页显示重要通知弹窗",
            key_elements=(self.semantic_name,),
            candidates=(candidate,),
            provider_id="misaligned-provider",
            model_id="misaligned-model",
            package_name=request.package_name,
            activity=request.activity,
            page_state="popup",
        )


class MisalignedNavigationProvider:
    def __init__(self) -> None:
        self.original_candidate: AppVisualCandidate | None = None

    async def understand(self, request: UnderstandAppScreenshotRequest) -> AppVisualAnalysis:
        candidate = AppVisualCandidate.build(
            source_capture_id=request.capture_id,
            source_image_signature=request.image_signature,
            source_width=request.width,
            source_height=request.height,
            source_orientation=request.orientation,
            semantic_name="top_tab_小说",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(
                left=0.28,
                top=0.05,
                right=0.4,
                bottom=0.09,
            ),
            confidence=0.85,
            risk_level=InteractionRiskLevel.LOW,
            purpose="切换小说频道",
            expected_state_hint="content_list",
            frontier_scope="app",
        )
        self.original_candidate = candidate
        return AppVisualAnalysis.build(
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            width=request.width,
            height=request.height,
            orientation=request.orientation,
            visual_page_type="home",
            visual_summary="漫画首页",
            key_elements=("小说",),
            candidates=(candidate,),
            provider_id="misaligned-navigation-provider",
            model_id="misaligned-navigation-model",
            package_name=request.package_name,
            activity=request.activity,
            page_state="stable",
        )


class MisalignedPostPublishChoiceProvider:
    def __init__(self) -> None:
        self.original_candidate: AppVisualCandidate | None = None

    async def understand(self, request: UnderstandAppScreenshotRequest) -> AppVisualAnalysis:
        candidate = AppVisualCandidate.build(
            source_capture_id=request.capture_id,
            source_image_signature=request.image_signature,
            source_width=request.width,
            source_height=request.height,
            source_orientation=request.orientation,
            semantic_name="图文帖子",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(
                left=0.28,
                top=0.71,
                right=0.52,
                bottom=0.78,
            ),
            confidence=0.93,
            risk_level=InteractionRiskLevel.MEDIUM,
            purpose="进入图文发帖",
            expected_state_hint="post_publish",
            frontier_scope="page",
        )
        self.original_candidate = candidate
        return AppVisualAnalysis.build(
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            width=request.width,
            height=request.height,
            orientation=request.orientation,
            visual_page_type="post_list",
            visual_summary="社区页显示发帖类型选择层",
            key_elements=("图文帖子", "视频帖子"),
            candidates=(candidate,),
            provider_id="misaligned-post-publish-provider",
            model_id="misaligned-post-publish-model",
            package_name=request.package_name,
            activity=request.activity,
            page_state="popup",
        )


class MisalignedPostPublishEntryProvider:
    def __init__(self) -> None:
        self.original_candidate: AppVisualCandidate | None = None

    async def understand(self, request: UnderstandAppScreenshotRequest) -> AppVisualAnalysis:
        candidate = AppVisualCandidate.build(
            source_capture_id=request.capture_id,
            source_image_signature=request.image_signature,
            source_width=request.width,
            source_height=request.height,
            source_orientation=request.orientation,
            semantic_name="发布加号",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(
                left=0.84,
                top=0.74,
                right=0.96,
                bottom=0.82,
            ),
            confidence=0.91,
            risk_level=InteractionRiskLevel.LOW,
            purpose="打开发帖入口",
            expected_state_hint="post_publish",
            frontier_scope="page",
        )
        self.original_candidate = candidate
        return AppVisualAnalysis.build(
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            width=request.width,
            height=request.height,
            orientation=request.orientation,
            visual_page_type="post_list",
            visual_summary="朋友圈页面右下角显示发帖加号",
            key_elements=("发布加号",),
            candidates=(candidate,),
            provider_id="misaligned-post-entry-provider",
            model_id="misaligned-post-entry-model",
            package_name=request.package_name,
            activity=request.activity,
            page_state="stable",
        )


def _capture_ref() -> CaptureRef:
    return CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="bootstrap",
        artifact_ids=("artifact-1",),
    )


def _source(*, checksum: str = SIGNATURE) -> PersistedAppCapture:
    return PersistedAppCapture(
        capture=_capture_ref(),
        agent_run_id="run-1",
        screenshot=PersistedEvidenceArtifact(
            artifact_id="artifact-1",
            artifact_type="screenshot",
            storage_path="task-1/capture-1/artifact-1.png",
            content_type="image/png",
            checksum=checksum,
            size_bytes=len(PNG_1X1),
            metadata={"encoding": "binary"},
        ),
        width=1,
        height=1,
        orientation="portrait",
        image_signature=checksum,
        package_name="com.example.target",
        activity=".MainActivity",
        bootstrap_row_version=7,
    )


def _verified_ui_tree(xml: str) -> VerifiedAppUiTree:
    return VerifiedAppUiTree(
        artifact=PersistedEvidenceArtifact(
            artifact_id="ui-tree-1",
            artifact_type="ui_tree",
            storage_path="task-1/capture-1/ui-tree-1.xml",
            content_type="application/xml; charset=utf-8",
            checksum=f"sha256:{hashlib.sha256(xml.encode()).hexdigest()}",
            size_bytes=len(xml.encode()),
            metadata={"encoding": "utf-8"},
        ),
        xml_text=xml,
    )


@pytest.mark.asyncio
async def test_understanding_verifies_checksum_before_provider_call() -> None:
    provider = RecordingProvider()
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(_source(checksum="sha256:" + "0" * 64)),
        artifacts=ArtifactReader(PNG_1X1),
        provider=provider,
    )

    with pytest.raises(AppScreenshotUnderstandingError) as exc_info:
        await service.understand(_capture_ref())

    assert exc_info.value.error_code == "SCREENSHOT_CHECKSUM_MISMATCH"
    assert provider.requests == []


@pytest.mark.asyncio
async def test_understanding_returns_bounded_analysis_without_image_bytes() -> None:
    provider = RecordingProvider()
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(_source()),
        artifacts=ArtifactReader(PNG_1X1),
        provider=provider,
    )

    analysis = await service.understand(_capture_ref())

    assert provider.requests[0].screenshot_png == PNG_1X1
    assert analysis.capture_id == "capture-1"
    assert len(analysis.result_hash) == 64
    assert set(analysis.result_hash) <= set("0123456789abcdef")
    latency = analysis.surface_facts["visual_latency"]
    assert latency["reused"] is False
    assert latency["capture_id"] == "capture-1"
    assert isinstance(latency["elapsed_ms"], int)
    assert latency["elapsed_ms"] >= 0
    assert latency["image_signature_prefix"] == SIGNATURE[:20]
    serialized = json.dumps(analysis.to_dict(), ensure_ascii=False)
    assert "screenshot_png" not in serialized
    assert "iVBOR" not in serialized


@pytest.mark.asyncio
async def test_resolve_capture_image_signature_skips_provider() -> None:
    provider = RecordingProvider()
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(_source()),
        artifacts=ArtifactReader(PNG_1X1),
        provider=provider,
    )

    signature = await service.resolve_capture_image_signature(_capture_ref())

    assert signature == SIGNATURE
    assert provider.requests == []


@pytest.mark.asyncio
async def test_understanding_rejects_provider_page_type_outside_shared_catalog() -> None:
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(_source()),
        artifacts=ArtifactReader(PNG_1X1),
        provider=RecordingProvider(visual_page_type="custom_app_home"),
    )

    with pytest.raises(AppScreenshotUnderstandingError) as exc_info:
        await service.understand(_capture_ref())

    assert exc_info.value.error_code == "INVALID_VISUAL_OUTPUT"


@pytest.mark.asyncio
async def test_understanding_adds_only_verified_unique_target_package_skip() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy><node package="com.example.target" content-desc="跳过"
        clickable="true" enabled="true" bounds="[860,101][1037,202]"/></hierarchy>"""
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(
            source,
            ui_tree=_verified_ui_tree(xml),
        ),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(),
    )

    analysis = await service.understand(capture)

    assert len(analysis.candidates) == 1
    candidate = analysis.candidates[0]
    assert candidate.semantic_name == "跳过广告：跳过"
    assert candidate.risk_level.value == "low"
    assert candidate.control_role == "skip"
    assert candidate.expected_state_hint == "tap_skip"
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx(
        ((860 + 1037) / 2 / 1080, (101 + 202) / 2 / 1920)
    )
    assert analysis.surface_facts["trusted_accessibility_skip"]["artifact_id"] == ("ui-tree-1")
    assert analysis.surface_facts["trusted_accessibility_skip"]["normalized_bounds"] == {
        "left": pytest.approx(860 / 1080),
        "top": pytest.approx(101 / 1920),
        "right": pytest.approx(1037 / 1080),
        "bottom": pytest.approx(202 / 1920),
    }


@pytest.mark.asyncio
async def test_understanding_adds_real_startup_enter_as_persisted_exit_candidate() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy><node package="com.example.target" content-desc="进入"
        clickable="true" enabled="true" bounds="[855,87][1050,177]"/></hierarchy>"""
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(
            visual_page_type="startup_ad",
            page_state="startup_ad",
        ),
    )

    analysis = await service.understand(capture)

    assert [item.semantic_name for item in analysis.candidates] == ["跳过广告：进入"]
    candidate = analysis.candidates[0]
    assert candidate.expected_state_hint == "tap_skip"
    assert candidate.control_role == "skip"
    assert candidate.normalized_bounds is not None
    assert analysis.surface_facts["trusted_accessibility_skip"]["label"] == "进入"


@pytest.mark.asyncio
async def test_understanding_adds_labeled_popup_dismiss_without_provider_candidate() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy>
      <node package="com.example.target" content-desc="下次再说" clickable="true"
            enabled="true" bounds="[138,1355][528,1472]"/>
      <node package="com.example.target" content-desc="立即升级" clickable="true"
            enabled="true" bounds="[552,1341][942,1485]"/>
    </hierarchy>"""
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(page_state="popup"),
    )

    analysis = await service.understand(capture)

    assert [item.semantic_name for item in analysis.candidates] == ["关闭弹层：下次再说"]
    assert analysis.page_state == "popup"
    assert analysis.overlay_signature is not None
    assert analysis.surface_facts["popup_visible"] is True
    candidate = analysis.candidates[0]
    assert candidate.control_role == "dismiss"
    assert candidate.risk_level is InteractionRiskLevel.LOW
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx(
        ((138 + 528) / 2 / 1080, (1355 + 1472) / 2 / 1920)
    )
    assert analysis.surface_facts["trusted_accessibility_dismiss"]["label"] == "下次再说"
    assert analysis.surface_facts["trusted_accessibility_dismiss"]["normalized_bounds"] == {
        "left": pytest.approx(138 / 1080),
        "top": pytest.approx(1355 / 1920),
        "right": pytest.approx(528 / 1080),
        "bottom": pytest.approx(1472 / 1920),
    }


@pytest.mark.asyncio
async def test_understanding_adds_splash_close_without_popup_page_state() -> None:
    """开屏全屏广告仅有 a11y「关闭」时，不要求 Vision 先标成 popup。"""

    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=3040, height=1440, capture=capture)
    xml = """<hierarchy rotation="3">
      <node package="com.example.target" class="android.widget.FrameLayout"
            bounds="[0,0][2730,1440]">
        <node package="com.example.target" content-desc="关闭" clickable="true"
              enabled="true" bounds="[2569,182][2730,308]"/>
      </node>
    </hierarchy>"""
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(
            visual_page_type="startup_ad",
            page_state="startup_ad",
        ),
    )

    analysis = await service.understand(capture)

    assert [item.semantic_name for item in analysis.candidates] == ["关闭弹层：关闭"]
    assert analysis.surface_facts["popup_visible"] is True
    assert analysis.surface_facts["trusted_accessibility_dismiss"]["label"] == "关闭"
    candidate = analysis.candidates[0]
    assert candidate.control_role == "dismiss"
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx(
        ((2569 + 2730) / 2 / 3040, (182 + 308) / 2 / 1440)
    )


@pytest.mark.asyncio
async def test_understanding_aligns_unique_low_risk_dismiss_candidate_to_ui_tree() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy>
      <node package="com.example.target" class="android.widget.Button"
            content-desc="应用中心" clickable="true" enabled="true"
            bounds="[156,1494][469,1638]"/>
      <node package="com.example.target" class="android.widget.Button"
            content-desc="我知道了" clickable="true" enabled="true"
            bounds="[612,1494][924,1638]"/>
    </hierarchy>"""
    provider = MisalignedDismissProvider()
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=provider,
    )

    analysis = await service.understand(capture)

    candidate = analysis.candidates[0]
    assert provider.original_candidate is not None
    assert candidate.candidate_id != provider.original_candidate.candidate_id
    assert candidate.control_role == "dismiss"
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx(
        ((612 + 924) / 2 / 1080, (1494 + 1638) / 2 / 1920)
    )
    assert candidate.confidence == 1.0
    assert candidate.risk_level is InteractionRiskLevel.LOW
    expected_candidate = AppVisualCandidate.build(
        source_capture_id=candidate.source_capture_id,
        source_image_signature=candidate.source_image_signature,
        source_width=candidate.source_width,
        source_height=candidate.source_height,
        source_orientation=candidate.source_orientation,
        semantic_name=candidate.semantic_name,
        action_type=candidate.action_type,
        normalized_bounds=candidate.normalized_bounds,
        confidence=candidate.confidence,
        risk_level=candidate.risk_level,
        purpose=candidate.purpose,
        control_role="dismiss",
        expected_state_hint=candidate.expected_state_hint,
        frontier_scope=candidate.frontier_scope,
    )
    assert candidate.candidate_id == expected_candidate.candidate_id
    assert analysis.surface_facts["trusted_accessibility_candidate_alignment"] == {
        "artifact_id": "ui-tree-1",
        "aligned_candidate_count": 1,
        "screen_width": 1080,
        "screen_height": 1920,
    }
    assert "accessibility:aligned_low_risk_action" in analysis.key_elements


@pytest.mark.asyncio
async def test_understanding_aligns_unique_low_risk_navigation_candidate_to_ui_tree() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy>
      <node package="com.example.target" content-desc="动漫" clickable="true"
            enabled="true" bounds="[246,120][389,189]"/>
      <node package="com.example.target" content-desc="小说" clickable="true"
            enabled="true" bounds="[389,120][531,189]"/>
      <node package="com.example.target" content-desc="剧场" clickable="true"
            enabled="true" bounds="[531,120][674,189]"/>
    </hierarchy>"""
    provider = MisalignedNavigationProvider()
    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=provider,
    ).understand(capture)

    candidate = analysis.candidates[0]
    assert provider.original_candidate is not None
    assert candidate.candidate_id != provider.original_candidate.candidate_id
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx(
        ((389 + 531) / 2 / 1080, (120 + 189) / 2 / 1920)
    )
    assert candidate.expected_state_hint == "content_list"
    assert analysis.surface_facts["trusted_accessibility_candidate_alignment"] == {
        "artifact_id": "ui-tree-1",
        "aligned_candidate_count": 1,
        "screen_width": 1080,
        "screen_height": 1920,
    }


@pytest.mark.asyncio
async def test_understanding_aligns_post_publish_choice_to_unique_clickable_row() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy>
      <node package="com.example.target" class="android.view.View"
            clickable="true" enabled="true" bounds="[0,0][1080,1920]"/>
      <node NAF="true" package="com.example.target" class="android.widget.ImageView"
            clickable="true" enabled="true" bounds="[30,1440][300,1710]"/>
      <node NAF="true" package="com.example.target" class="android.widget.ImageView"
            clickable="true" enabled="true" bounds="[60,1578][1020,1710]"/>
      <node NAF="true" package="com.example.target" class="android.widget.ImageView"
            clickable="true" enabled="true" bounds="[780,1350][1050,1710]"/>
    </hierarchy>"""
    provider = MisalignedPostPublishChoiceProvider()
    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=provider,
    ).understand(capture)

    candidate = analysis.candidates[0]
    assert provider.original_candidate is not None
    assert candidate.candidate_id != provider.original_candidate.candidate_id
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx(
        (((0.28 + 0.52) / 2), (1578 + 1710) / 2 / 1920)
    )
    assert candidate.confidence == 1.0
    assert candidate.risk_level is InteractionRiskLevel.MEDIUM
    assert candidate.expected_state_hint == "post_publish"
    assert analysis.surface_facts["trusted_accessibility_candidate_alignment"] == {
        "artifact_id": "ui-tree-1",
        "aligned_candidate_count": 1,
        "screen_width": 1080,
        "screen_height": 1920,
    }


@pytest.mark.asyncio
async def test_understanding_aligns_post_publish_entry_above_bottom_navigation() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy>
      <node package="com.example.target" content-desc="首页"
            clickable="true" enabled="true" bounds="[0,1743][180,1890]"/>
      <node package="com.example.target" content-desc="视频"
            clickable="true" enabled="true" bounds="[180,1743][360,1890]"/>
      <node package="com.example.target" content-desc="发现"
            clickable="true" enabled="true" bounds="[360,1743][540,1890]"/>
      <node package="com.example.target" content-desc="福利"
            clickable="true" enabled="true" bounds="[540,1743][720,1890]"/>
      <node package="com.example.target" content-desc="朋友圈" selected="true"
            clickable="true" enabled="true" bounds="[720,1743][900,1890]"/>
      <node package="com.example.target" content-desc="我的"
            clickable="true" enabled="true" bounds="[900,1743][1080,1890]"/>
    </hierarchy>"""
    provider = MisalignedPostPublishEntryProvider()

    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=provider,
    ).understand(capture)

    candidate = analysis.candidates[0]
    assert provider.original_candidate is not None
    assert candidate.candidate_id != provider.original_candidate.candidate_id
    assert candidate.normalized_bounds is not None
    assert candidate.normalized_bounds.center == pytest.approx((0.9, 1647 / 1920))
    assert candidate.normalized_bounds.bottom < 1743 / 1920
    assert candidate.confidence == 1.0


@pytest.mark.asyncio
async def test_understanding_does_not_align_ambiguous_or_risky_labeled_actions() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    ambiguous_xml = """<hierarchy>
      <node package="com.example.target" content-desc="我知道了"
            clickable="true" enabled="true" bounds="[100,1400][400,1550]"/>
      <node package="com.example.target" content-desc="我知道了"
            clickable="true" enabled="true" bounds="[600,1400][900,1550]"/>
    </hierarchy>"""
    ambiguous_provider = MisalignedDismissProvider()
    ambiguous = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(ambiguous_xml)),
        artifacts=ArtifactReader(png),
        provider=ambiguous_provider,
    ).understand(capture)

    assert ambiguous_provider.original_candidate is not None
    assert (
        ambiguous.candidates[0].candidate_id == ambiguous_provider.original_candidate.candidate_id
    )
    assert "trusted_accessibility_candidate_alignment" not in ambiguous.surface_facts

    risky_xml = """<hierarchy>
      <node package="com.example.target" content-desc="取消支付"
            clickable="true" enabled="true" bounds="[600,1400][900,1550]"/>
    </hierarchy>"""
    risky_provider = MisalignedDismissProvider(semantic_name="取消支付")
    risky = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(risky_xml)),
        artifacts=ArtifactReader(png),
        provider=risky_provider,
    ).understand(capture)

    assert risky_provider.original_candidate is not None
    assert risky.candidates[0].candidate_id == risky_provider.original_candidate.candidate_id
    assert "trusted_accessibility_candidate_alignment" not in risky.surface_facts


@pytest.mark.asyncio
async def test_understanding_adds_dynamic_overlay_close_without_underlay_controls() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(width=1080, height=1920, capture=capture)
    xml = """<hierarchy>
      <node package="uk.pnixc.juywgn" class="android.view.View" clickable="true"
            enabled="true" bounds="[0,0][1080,1920]">
        <node package="uk.pnixc.juywgn" class="android.view.View" clickable="true"
              enabled="true" bounds="[0,0][1080,1920]"/>
        <node NAF="true" package="uk.pnixc.juywgn" class="android.widget.ImageView"
              clickable="true" enabled="true" bounds="[101,391][979,1370]"/>
        <node NAF="true" package="uk.pnixc.juywgn" class="android.widget.ImageView"
              clickable="true" enabled="true" bounds="[490,1428][590,1529]"/>
      </node>
    </hierarchy>"""
    source = replace(source, package_name="uk.pnixc.juywgn")
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(),
    )

    analysis = await service.understand(capture)

    assert [item.semantic_name for item in analysis.candidates] == ["关闭弹层"]
    assert analysis.candidates[0].expected_state_hint == "close_popup_x"
    assert analysis.candidates[0].control_role == "dismiss"
    assert analysis.page_state == "popup"
    assert analysis.overlay_signature == "accessibility:490,1428,590,1529"
    assert analysis.surface_facts["popup_visible"] is True
    facts = analysis.surface_facts["trusted_accessibility_controls"]
    assert facts["overlay_close"] is True
    assert facts["bottom_navigation_count"] == 0


@pytest.mark.asyncio
async def test_understanding_issues_bottom_nav_alongside_overlay_close_when_strip_clear() -> None:
    """有关层控件时，中心不落在蒙层内的底栏 Tab 仍须签发。"""

    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(
        width=1080,
        height=1920,
        capture=capture,
        package_name="uk.pnixc.juywgn",
    )
    # 关层沿用受信 a11y 合同样例；底栏 5 格铺满底部并与关层并存。
    xml = """<hierarchy>
      <node package="uk.pnixc.juywgn" class="android.view.View" clickable="true"
            enabled="true" bounds="[0,0][1080,1920]">
        <node package="uk.pnixc.juywgn" class="android.view.View" clickable="true"
              enabled="true" bounds="[0,0][1080,1920]"/>
        <node NAF="true" package="uk.pnixc.juywgn" class="android.widget.ImageView"
              clickable="true" enabled="true" bounds="[101,391][979,1370]"/>
        <node NAF="true" package="uk.pnixc.juywgn" class="android.widget.ImageView"
              clickable="true" enabled="true" bounds="[490,1428][590,1529]"/>
      </node>
      <node package="uk.pnixc.juywgn" content-desc="视频" clickable="true"
            enabled="true" selected="true" bounds="[0,1762][216,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="动态" clickable="true"
            enabled="true" bounds="[216,1762][432,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="交友" clickable="true"
            enabled="true" bounds="[432,1762][648,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="应用" clickable="true"
            enabled="true" bounds="[648,1762][864,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="我的" clickable="true"
            enabled="true" bounds="[864,1762][1080,1920]"/>
    </hierarchy>"""
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(visual_page_type="video_list"),
    )

    analysis = await service.understand(capture)

    names = [item.semantic_name for item in analysis.candidates]
    assert "关闭弹层" in names
    assert any(name.startswith("底栏「") for name in names)
    assert "底栏「交友」" in names or "底栏「我的」" in names
    # 已选中的视频 Tab 不得再次签发。
    assert "底栏「视频」" not in names
    facts = analysis.surface_facts["trusted_accessibility_controls"]
    assert facts["overlay_close"] is True
    assert facts["bottom_navigation_count"] >= 2


@pytest.mark.asyncio
async def test_understanding_adds_complete_dynamic_navigation_and_ordinary_content() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(
        width=1080,
        height=1920,
        capture=capture,
        package_name="uk.pnixc.juywgn",
    )
    xml = """<hierarchy>
      <node package="uk.pnixc.juywgn" content-desc="首页&#10;第 1 个标签，共 30 个"
            clickable="true" enabled="true" selected="true" bounds="[37,199][182,324]"/>
      <node package="uk.pnixc.juywgn" class="android.view.View"
            content-desc="热&#10;一个长度足够的普通新闻内容标题用于回归测试"
            clickable="true" enabled="true" bounds="[0,799][1080,1352]">
        <node package="uk.pnixc.juywgn" class="android.widget.ImageView"
              clickable="false" enabled="true" bounds="[37,799][1043,1180]"/>
      </node>
      <node package="uk.pnixc.juywgn" content-desc="吃瓜" clickable="true"
            enabled="true" bounds="[0,1762][180,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="AI科技" clickable="true"
            enabled="true" bounds="[180,1762][360,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="社区" clickable="true"
            enabled="true" bounds="[360,1762][540,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="视频" clickable="true"
            enabled="true" bounds="[540,1762][720,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="福利" clickable="true"
            enabled="true" bounds="[720,1762][900,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="我的" clickable="true"
            enabled="true" bounds="[900,1762][1080,1920]"/>
    </hierarchy>"""
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(visual_page_type=None),
    )

    analysis = await service.understand(capture)

    assert analysis.visual_page_type == "home"
    assert [item.semantic_name for item in analysis.candidates] == [
        "底栏「吃瓜」",
        "底栏「AI科技」",
        "底栏「社区」",
        "底栏「视频」",
        "底栏「福利」",
        "底栏「我的」",
        "普通内容卡片",
    ]
    assert analysis.candidates[-1].expected_state_hint == "content_detail"
    facts = analysis.surface_facts["trusted_accessibility_controls"]
    assert facts["bottom_navigation_count"] == 6
    assert facts["content_card_count"] == 1
    assert facts["page_type"] == "home"


@pytest.mark.asyncio
async def test_understanding_fuses_ui_tree_after_vision_reaches_candidate_cap() -> None:
    """Vision 占满配额时仍提取清障和未选中的完整底栏，再按角色裁剪。"""

    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(
        width=1080,
        height=1920,
        capture=capture,
        package_name="com.example.target",
    )
    xml = """<hierarchy>
      <node package="com.example.target" content-desc="跳过"
            clickable="true" enabled="true" bounds="[860,101][1037,202]"/>
      <node package="com.example.target" content-desc="首页" selected="true"
            clickable="true" enabled="true" bounds="[0,1762][180,1920]"/>
      <node package="com.example.target" content-desc="视频"
            clickable="true" enabled="true" bounds="[180,1762][360,1920]"/>
      <node package="com.example.target" content-desc="发现"
            clickable="true" enabled="true" bounds="[360,1762][540,1920]"/>
      <node package="com.example.target" content-desc="福利"
            clickable="true" enabled="true" bounds="[540,1762][720,1920]"/>
      <node package="com.example.target" content-desc="朋友圈"
            clickable="true" enabled="true" bounds="[720,1762][900,1920]"/>
      <node package="com.example.target" content-desc="我的"
            clickable="true" enabled="true" bounds="[900,1762][1080,1920]"/>
    </hierarchy>"""
    specs = tuple(
        {
            "semantic_name": f"普通内容卡片 {index}",
            "expected_state_hint": "content_detail",
            "left": 0.05,
            "top": 0.2 + index * 0.02,
            "right": 0.25,
            "bottom": 0.3 + index * 0.02,
        }
        for index in range(20)
    )

    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=FixedCandidatesProvider(specs),
        max_candidates=20,
    ).understand(capture)

    names = {item.semantic_name for item in analysis.candidates}
    assert len(analysis.candidates) == 20
    assert "跳过广告：跳过" in names
    assert {
        "底栏「视频」",
        "底栏「发现」",
        "底栏「福利」",
        "底栏「朋友圈」",
        "底栏「我的」",
    } <= names
    assert "底栏「首页」" not in names
    fusion = analysis.surface_facts["candidate_fusion"]
    assert fusion["candidate_count_before_crop"] == 26
    assert fusion["candidate_count_after_crop"] == 20
    assert fusion["source_counts_before_crop"] == {"vision": 20, "ui_tree": 6}
    assert fusion["role_counts_before_crop"] == {
        "content_card": 20,
        "obstruction": 1,
        "global_navigation": 5,
    }


@pytest.mark.asyncio
async def test_understanding_keeps_explicit_goal_entry_ahead_of_content_cards() -> None:
    """统一配额下，明确缺页入口不得被普通内容卡片挤掉。"""

    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(
        width=1080,
        height=1920,
        capture=capture,
        package_name="com.example.target",
    )
    xml = """<hierarchy>
      <node package="com.example.target" content-desc="跳过"
            clickable="true" enabled="true" bounds="[860,101][1037,202]"/>
      <node package="com.example.target" content-desc="首页" selected="true"
            clickable="true" enabled="true" bounds="[0,1762][360,1920]"/>
      <node package="com.example.target" content-desc="视频"
            clickable="true" enabled="true" bounds="[360,1762][720,1920]"/>
      <node package="com.example.target" content-desc="我的"
            clickable="true" enabled="true" bounds="[720,1762][1080,1920]"/>
    </hierarchy>"""
    specs = (
        {
            "semantic_name": "普通内容卡片",
            "expected_state_hint": "content_detail",
            "left": 0.05,
            "top": 0.3,
            "right": 0.45,
            "bottom": 0.5,
        },
        {
            "semantic_name": "打开设置入口",
            "expected_state_hint": "settings",
            "left": 0.5,
            "top": 0.3,
            "right": 0.8,
            "bottom": 0.4,
        },
    )

    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=FixedCandidatesProvider(specs),
        max_candidates=3,
    ).understand(capture)

    assert [item.semantic_name for item in analysis.candidates] == [
        "跳过广告：跳过",
        "打开设置入口",
        "底栏「视频」",
    ]
    assert analysis.surface_facts["candidate_fusion"]["dropped_candidate_count"] == 2


@pytest.mark.asyncio
async def test_understanding_prefers_pixel_selected_video_navigation_over_home() -> None:
    screenshot = _navigation_accent_png(
        accent_bounds=(570, 1800, 690, 1840),
    )
    signature = f"sha256:{hashlib.sha256(screenshot).hexdigest()}"
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    base_source = _source(checksum=signature)
    source = replace(
        base_source,
        capture=capture,
        screenshot=replace(
            base_source.screenshot,
            checksum=signature,
            size_bytes=len(screenshot),
        ),
        width=1080,
        height=1920,
        image_signature=signature,
        package_name="uk.pnixc.juywgn",
    )
    xml = """<hierarchy>
      <node package="uk.pnixc.juywgn" content-desc="首页" clickable="true"
            enabled="true" selected="true" bounds="[37,199][182,324]"/>
      <node package="uk.pnixc.juywgn" content-desc="吃瓜" clickable="true"
            enabled="true" bounds="[0,1762][180,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="AI科技" clickable="true"
            enabled="true" bounds="[180,1762][360,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="社区" clickable="true"
            enabled="true" bounds="[360,1762][540,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="视频" clickable="true"
            enabled="true" bounds="[540,1762][720,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="福利" clickable="true"
            enabled="true" bounds="[720,1762][900,1920]"/>
      <node package="uk.pnixc.juywgn" content-desc="我的" clickable="true"
            enabled="true" bounds="[900,1762][1080,1920]"/>
    </hierarchy>"""

    analysis = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(screenshot),
        provider=RecordingProvider(visual_page_type="home", page_state="stable"),
    ).understand(capture)

    assert analysis.visual_page_type == "video_list"
    assert "底栏「视频」" not in {item.semantic_name for item in analysis.candidates}
    facts = analysis.surface_facts["trusted_accessibility_controls"]
    assert facts["pixel_selected_page_type"] == "video_list"
    assert facts["pixel_selected_labels"] == ("视频",)


@pytest.mark.asyncio
async def test_understanding_projects_dynamic_content_detail_without_clicks() -> None:
    capture = replace(_capture_ref(), artifact_ids=("artifact-1", "ui-tree-1"))
    source, png = _matched_source(
        width=1080,
        height=1920,
        capture=capture,
        package_name="uk.pnixc.juywgn",
    )
    xml = """<hierarchy>
      <node package="uk.pnixc.juywgn" class="android.widget.FrameLayout"
            clickable="false" enabled="true" bounds="[0,0][1080,1920]">
        <node package="uk.pnixc.juywgn" class="android.view.View" text="黑料网"
              clickable="false" enabled="true" bounds="[465,100][615,169]"/>
        <node package="uk.pnixc.juywgn" class="android.widget.ScrollView"
              clickable="false" enabled="true" bounds="[0,199][1080,1751]">
          <node package="uk.pnixc.juywgn" class="android.view.View"
                text="一个长度足够并且已经进入目标包详情页面的文章标题"
                clickable="false" enabled="true" bounds="[37,242][1043,446]"/>
          <node package="uk.pnixc.juywgn" class="android.view.View"
                text="黑料网-作者•2026年08月21日" clickable="false" enabled="true"
                bounds="[37,460][428,505]"/>
        </node>
      </node>
    </hierarchy>"""
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source, ui_tree=_verified_ui_tree(xml)),
        artifacts=ArtifactReader(png),
        provider=RecordingProvider(visual_page_type=None),
    )

    analysis = await service.understand(capture)

    assert analysis.visual_page_type == "content_detail"
    assert analysis.candidates == ()
    facts = analysis.surface_facts["trusted_accessibility_controls"]
    assert facts["page_type"] == "content_detail"
    assert facts["bottom_navigation_count"] == 0


@pytest.mark.asyncio
async def test_understanding_rejects_repository_identity_drift() -> None:
    source = _source()
    wrong_capture = CaptureRef(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-other",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="bootstrap",
        artifact_ids=("artifact-1",),
    )
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(source),
        artifacts=ArtifactReader(PNG_1X1),
        provider=RecordingProvider(),
    )

    with pytest.raises(AppScreenshotUnderstandingError) as exc_info:
        await service.understand(wrong_capture)

    assert exc_info.value.error_code == "SCREENSHOT_ARTIFACT_MISSING"


@pytest.mark.asyncio
async def test_understand_from_context_does_not_reread_artifact() -> None:
    artifacts = ArtifactReader(PNG_1X1)
    service = AppScreenshotUnderstandingService(
        sources=SourceRepository(_source()),
        artifacts=artifacts,
        provider=RecordingProvider(),
    )

    context = await service.resolve_capture_visual_context(_capture_ref())
    assert artifacts.reads == 1
    analysis = await service.understand_from_context(context)
    assert artifacts.reads == 1
    assert analysis.image_signature == SIGNATURE
    await service.understand(_capture_ref())
    assert artifacts.reads == 2

