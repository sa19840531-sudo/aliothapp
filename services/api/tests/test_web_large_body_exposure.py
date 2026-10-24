"""大正文只读补证不能沿用小控件中心曝光条件。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_interaction_facts import web_target_exposed
from services.api.app.application.web_primary_content_capture import WebPrimaryContentCaptureService
from services.api.app.application.web_primary_content_exposure import web_primary_content_exposed
from services.api.tests.test_web_primary_content_capture import _element, _ReadyEvidence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tag", "height", "after_x", "after_y", "page_type", "want"),
    [
        ("main", 10806.75, 630, -0.2, "content_detail", "captured"),
        ("main", 18942.2, 630, -0.2, "content_detail", "captured"),
        ("article", 12000, 630, -0.2, "content_detail", "captured"),
        ("main", 12000, 630, 1079, "content_detail", "blocked"),
        ("main", 12000, 1919, -0.2, "content_detail", "blocked"),
        ("main", 12000, 630, 1080, "content_detail", "blocked"),
        ("video", 12000, 630, -0.2, "video_detail", "blocked"),
    ],
)
async def test_large_body_capture_requires_substantial_visible_content(
    tag, height, after_x, after_y, page_type, want
):
    before = {
        "viewport": {"width": 1920, "height": 1080, "scroll_y": 0},
        "elements": [_element(
            "#subject", tag=tag, is_repeated_card=False,
            src="https://example.com/video.mp4" if tag == "video" else None,
            rect={"x": 630, "y": 76.8, "width": 660, "height": height},
        )],
    }
    after = {
        "viewport": {"width": 1920, "height": 1080, "scroll_y": 77},
        "elements": [{
            **before["elements"][0],
            "rect": {"x": after_x, "y": after_y, "width": 660, "height": height},
        }],
    }

    class Evidence(_ReadyEvidence):
        async def read_snapshot(self, *, capture_id):
            return before if capture_id == "baseline" else after

        async def read_url(self, *, capture_id):
            return "https://example.com/article/42"

    class Coordinator:
        count = 0

        async def coordinate(self, command):
            self.count += 1
            sequence = self.count
            return SimpleNamespace(
                before_capture=SimpleNamespace(
                    capture_id=f"before-{sequence}",
                    parent_capture_id=command.intent.source_capture_id,
                    action_execution_id=f"scroll-{sequence}",
                ),
                after_capture=SimpleNamespace(
                    capture_id=f"after-{sequence}",
                    parent_capture_id=f"before-{sequence}",
                    action_execution_id=f"scroll-{sequence}",
                ),
                interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
            )

    class Writer:
        saved = None

        async def mark_primary_content_capture(self, **kwargs):
            self.saved = kwargs

    coordinator, writer = Coordinator(), Writer()
    result = await WebPrimaryContentCaptureService(
        coordinator=coordinator, evidence_source=Evidence(), metadata_writer=writer,
    ).capture(
        task_id="task", agent_run_id="run", session_id="web:run",
        capture_id="baseline", page_url="https://example.com/article/42",
        page_type=page_type, deadline=datetime.now(UTC) + timedelta(minutes=1),
    )

    assert result.status == want
    # 普通动作曝光函数保持严格，正文补证不能向点击目标扩散。
    assert web_target_exposed(after, selector="#subject") is False
    if want == "captured":
        assert coordinator.count == result.scroll_count == 1
        assert result.evidence_capture_id == "after-1"
        assert result.interaction_outcomes[0].transition_arrived is True
        assert writer.saved["parent_capture_id"] == "baseline"
        assert writer.saved["capture_id"] == "after-1"
    else:
        assert coordinator.count <= 2
        assert writer.saved is None
        assert result.evidence_capture_id is None


@pytest.mark.parametrize(
    ("rect", "viewport", "want"),
    [
        ({"x": 630, "y": -0.2, "width": 660, "height": 12000},
         {"width": 1920, "height": 1080}, True),
        ({"x": 630, "y": 541, "width": 660, "height": 12000},
         {"width": 1920, "height": 1080}, False),
        ({"x": 630, "y": 0, "width": 660, "height": float("inf")},
         {"width": 1920, "height": 1080}, False),
        ({"x": 630, "y": 0, "width": True, "height": 100},
         {"width": 1920, "height": 1080}, False),
        ({"x": 630, "y": -0.2, "width": 1, "height": 12000},
         {"width": 1920, "height": 1080}, False),
        ({"x": 630, "y": 0, "width": 660, "height": 12000},
         {"width": 1920, "height": 1080, "visual_viewport_width": 960}, False),
        ({"x": 0, "y": -5500, "width": 660, "height": 12000},
         {"width": 1920, "height": 1080, "visual_viewport_width": 800,
          "visual_viewport_height": 400, "visual_viewport_offset_left": 1000,
          "visual_viewport_offset_top": 600}, False),
    ],
)
def test_body_exposure_rejects_invalid_and_outside_visual_viewport(rect, viewport, want):
    snapshot = {"viewport": viewport, "elements": [_element("#subject", tag="main", rect=rect)]}
    assert web_primary_content_exposed(
        snapshot, selector="#subject", page_type="content_detail"
    ) is want


@pytest.mark.asyncio
async def test_real_browser_oversized_article_is_readable_without_center_visible():
    from playwright.async_api import async_playwright

    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
    from services.api.tests.web_browser_test_support import (
        WebBrowserUnavailable,
        launch_web_test_browser,
    )

    try:
        async with async_playwright() as playwright:
            browser = await launch_web_test_browser(playwright.chromium, headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1920, "height": 1080})
                await page.set_content(
                    '<style>body{margin:0}main{margin:77px auto 0;width:660px;'
                    'height:12000px;background:#eee}</style>'
                    '<main id="subject"><p>这是受控测试正文，不访问真实站点。</p></main>'
                )
                await page.locator("#subject").scroll_into_view_if_needed()
                snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            finally:
                await browser.close()
    except WebBrowserUnavailable as exc:
        pytest.skip(str(exc))
    assert web_target_exposed(snapshot, selector="#subject") is False
    assert web_primary_content_exposed(
        snapshot, selector="#subject", page_type="content_detail"
    ) is True


@pytest.mark.asyncio
async def test_readonly_overlay_census_cannot_override_primary_business_visibility():
    snapshot = {
        "viewport": {"width": 1920, "height": 1080},
        "web_detail_candidate_priority_enabled": True,
        "elements": [_element("#subject", tag="main", is_repeated_card=False)],
        "web_overlay_geometry": {
            "schema_version": "web-overlay-geometry.v1", "complete": True,
            "subjects": [{"x": 0, "y": 200, "width": 600, "height": 300}],
            "layers": [{"ordinary": True, "rect": {
                "x": 0, "y": 200, "width": 60, "height": 30,
            }}],
        },
    }
    service = WebPrimaryContentCaptureService(coordinator=None, evidence_source=None)
    reason, model_cleared = await service._post_scroll_obstruction_decision(
        capture_id="after", ui_snapshot=snapshot, ready_capture=None, page_role="detail",
    )
    assert reason is None
    # 只读几何不得在未调用模型时覆盖 ready_facts.business_layer_visible。
    assert model_cleared is False
