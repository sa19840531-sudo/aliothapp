"""首页系统返回保护：不让旧候选或调度兜底把 APP 退到桌面。"""

import pytest

from services.api.app.application.app_interaction_runtime import AppInteractionRuntimeError
from services.api.app.domain.interaction import InteractionOperation, InteractionResultStatus
from services.api.tests.test_app_interaction_runtime import (
    _analysis,
    _candidate,
    _executor,
    _request,
)


@pytest.mark.parametrize("state", ["stable", "loaded"])
async def test_home_back_never_reaches_device(state):
    back = _candidate(action_type=InteractionOperation.BACK, semantic_name="系统 BACK")
    executor, driver, _ = _executor(
        candidate=back, analysis=_analysis(back, visual_page_type="home", page_state=state),
    )
    with pytest.raises(AppInteractionRuntimeError) as caught:
        await executor.execute(_request(back))
    assert caught.value.error_code == "APP_HOME_BACK_FORBIDDEN"
    assert driver.execute_count == 0


async def test_detail_back_remains_available():
    back = _candidate(action_type=InteractionOperation.BACK, semantic_name="系统 BACK")
    executor, driver, _ = _executor(
        candidate=back, analysis=_analysis(back, visual_page_type="video_detail"),
    )
    result = await executor.execute(_request(back))
    assert result.status is InteractionResultStatus.EVIDENCE_PENDING
    assert driver.execute_count == 1


async def test_root_with_accessibility_bottom_tabs_blocks_back_before_device():
    back = _candidate(action_type=InteractionOperation.BACK, semantic_name="系统 BACK")
    analysis = _analysis(back, visual_page_type="content_list", page_state="stable",
                       surface_facts={"trusted_accessibility_controls": {
                           "bottom_navigation": [{"label": "精选"}, {"label": "我的"}],
                       }})
    executor, driver, _ = _executor(candidate=back, analysis=analysis)
    with pytest.raises(AppInteractionRuntimeError) as caught:
        await executor.execute(_request(back))
    assert caught.value.error_code == "APP_HOME_BACK_FORBIDDEN"
    assert driver.execute_count == 0


async def test_pixel_strip_cannot_invent_comic_category():
    from services.api.app.application.app_screenshot_understanding import (
        AppScreenshotUnderstandingService,
    )
    from services.api.tests.test_app_screenshot_understanding import (
        ArtifactReader,
        FixedCandidatesProvider,
        SourceRepository,
        _matched_source,
    )
    from services.api.tests.test_app_visual_home_top_category_calibration import (
        _png_with_seven_tabs,
    )

    png, width, height = _png_with_seven_tabs(bright_slot=3)
    source, _ = _matched_source(
        width=width, height=height, png=png, package_name="com.alz7xm2.drama",
    )
    result = await AppScreenshotUnderstandingService(
        sources=SourceRepository(source), artifacts=ArtifactReader(png),
        provider=FixedCandidatesProvider(({
            "semantic_name": "上方分类「糖果原创」", "top": .11, "bottom": .16,
            "expected_state_hint": "home_top_tab:糖果原创",
        },)),
    ).understand(source.capture)
    labels = [item.semantic_name for item in result.candidates]
    assert "上方分类「糖果原创」" in labels
    assert not any("漫画" in label or "最新" in label for label in labels)
    assert "pixel_home_top_category_calibration" not in result.surface_facts
