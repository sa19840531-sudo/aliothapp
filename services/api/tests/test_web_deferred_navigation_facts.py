"""延迟授权不应丢失同 Capture 的顶部导航分类事实。"""

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
)
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
    WebDesktopCapturePolicyBrowser,
)
from services.api.tests.test_web_desktop_capture_policy import _navigation_snapshot


@pytest.mark.parametrize("hit,hidden,want", [(True, False, 1), (False, False, 0), (True, True, 0)])
async def test_deferred_capture_keeps_navigation_facts_without_authorizing_actions(
    hit, hidden, want,
):
    snapshot = _navigation_snapshot(("分类甲", "/first"), ("分类乙", "/second"))
    for item in snapshot["elements"]:
        item.update(within_navigation=True, within_web_navigation=False)
        item["hit_test"]["center_hits_target"] = hit
    attr = ' style="display:none"' if hidden else ""
    dom = f'<header><div class="category-nav"{attr}><a href="/first">分类甲</a>'
    dom += '<a href="/second">分类乙</a></div></header>'

    class Browser:
        async def capture(self, request):
            return PageCaptureResult(
                execution_status="success", mode=request.mode.value, reachable=True,
                final_url="https://example.test/", title="测试", status_code=200,
                artifacts=[PageCaptureArtifact("dom", None, dom),
                           PageCaptureArtifact("ui_snapshot", None, snapshot)],
            )

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    policy.defer_navigation_discovery_until_authorized()
    result = await policy.capture(PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE, session_id="web:test-nav-facts",
        web_capture_stage="web_page_ready_baseline",
    ))
    stored = next(a.content for a in result.artifacts if a.artifact_type == "ui_snapshot")
    facts = web_page_structure_facts(stored, origin="https://example.test", current_url="https://example.test/")
    assert facts.top_navigation_container_count == want
    assert facts.visible_category_entry_count == 2 * want
    assert facts.navigation_element_count == 2 * want
    assert policy.navigation_candidates("web:test-nav-facts") == ()
    assert all(e["within_web_navigation"] is False for e in stored["elements"])
