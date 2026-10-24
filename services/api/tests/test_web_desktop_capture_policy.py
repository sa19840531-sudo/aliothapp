"""Web-only 首页、逐页清障与 1920×1080 PNG 体积压缩合同。"""

import base64
import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from services.api.app.application.web_desktop_navigation import (
    WEB_NAVIGATION_SOURCE_SELECTOR,
)
from services.api.app.application.web_exploration_workflow import WebCaptureStage
from services.api.app.infrastructure.browser import capture_runtime as capture_runtime_module
from services.api.app.infrastructure.browser import page_capture as page_capture_module
from services.api.app.infrastructure.browser.capture_runtime import (
    build_default_web_capture_runtime,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
)
from services.api.app.infrastructure.browser.web_desktop_capture_policy import (
    WebDesktopCapturePolicyBrowser,
    WebScreenshotContractError,
    _compress_screenshot_content,
    _enforce_web_desktop_request,
)


def _uncompressed_desktop_png() -> bytes:
    image = Image.new("RGB", (1920, 1080), color=(30, 90, 160))
    buffer = BytesIO()
    image.save(buffer, format="PNG", compress_level=0)
    return buffer.getvalue()


def _png(width: int, height: int) -> bytes:
    image = Image.new("RGB", (width, height), color=(30, 90, 160))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("width", [1920, 1930])
async def test_web_direct_full_page_preserves_tall_png_without_viewport_compression(
    width: int,
) -> None:
    original = _png(width, 3500)

    class Browser:
        async def capture(self, request):
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/article",
                title="Article",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        artifact_type="screenshot",
                        artifact_id=None,
                        content={
                            "purpose": "web_direct_full_page",
                            "full_page": True,
                            "encoding": "base64",
                            "data": base64.b64encode(original).decode("ascii"),
                        },
                    )
                ],
            )

    result = await WebDesktopCapturePolicyBrowser(browser=Browser()).capture(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            web_capture_stage="web_full_page_supplemental",
            session_id="web:direct-full-page",
            viewport_width=1920,
            viewport_height=1080,
        )
    )

    assert result.execution_status == "success"
    screenshot = next(item for item in result.artifacts if item.artifact_type == "screenshot")
    assert screenshot.content["purpose"] == "web_direct_full_page"
    assert screenshot.content["full_page"] is True
    assert base64.b64decode(screenshot.content["data"], validate=True) == original
    assert "compression" not in screenshot.content


@pytest.mark.asyncio
@pytest.mark.parametrize("use_timeout", [False, True])
@pytest.mark.parametrize("url", ["about:blank", "about:blank#loading"])
async def test_web_blank_capture_never_returns_screenshot(url, use_timeout):
    requests = []

    class Browser:
        async def capture(self, request):
            requests.append(request)
            return PageCaptureResult(
                execution_status="success", mode=request.mode.value, reachable=True,
                final_url=url, title="", status_code=None,
                artifacts=[
                    PageCaptureArtifact(artifact_type="dom", artifact_id=None, content="<html/>"),
                    PageCaptureArtifact(artifact_type="screenshot", artifact_id=None,
                                        content={"data": "invalid-blank-image"}),
                ],
            )

    browser = WebDesktopCapturePolicyBrowser(browser=Browser())
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE, bootstrap_blank_surface=True,
        session_id="web:blank-test", capture_screenshot=True,
    )
    result = (
        await browser.capture_with_timeout(request, timeout_seconds=5)
        if use_timeout else await browser.capture(request)
    )
    assert requests[0].capture_screenshot is False
    assert result.final_url == url
    assert "dom" in {item.artifact_type for item in result.artifacts}
    assert "screenshot" not in {item.artifact_type for item in result.artifacts}


@pytest.mark.parametrize("url, expected", [
    ("about:blank", False), ("about:blank#loading", False),
    ("https://example.test/about:blank", True), (None, True),
])
def test_web_blank_screenshot_policy_does_not_disable_business_captures(url, expected):
    request = PageCaptureRequest(mode=PageCaptureMode.OBSERVE, url=url)
    assert _enforce_web_desktop_request(request).capture_screenshot is expected
    assert request.capture_screenshot is True


@pytest.mark.asyncio
async def test_real_web_bootstrap_never_calls_screenshot(monkeypatch):
    from playwright.async_api import Page

    from services.api.app.infrastructure.browser.page_capture import PlaywrightPageCaptureBrowser
    from services.api.tests.web_browser_test_support import launch_web_test_browser

    screenshot_calls = []
    original = Page.screenshot

    async def screenshot(page, *args, **kwargs):
        screenshot_calls.append(page.url)
        return await original(page, *args, **kwargs)

    class Browser(PlaywrightPageCaptureBrowser):
        async def _launch_browser(self, playwright):
            return await launch_web_test_browser(playwright.chromium, headless=True)

    monkeypatch.setattr(Page, "screenshot", screenshot)
    browser = WebDesktopCapturePolicyBrowser(browser=Browser(default_timeout_ms=10_000))
    try:
        result = await browser.capture(PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE, bootstrap_blank_surface=True,
            session_id="web:blank-policy-proof", timeout_ms=10_000,
            viewport_width=1920, viewport_height=1080,
        ))
    finally:
        await browser.close_session("web:blank-policy-proof")
    assert result.execution_status == "success", result.error_message
    assert result.final_url == "about:blank"
    assert screenshot_calls == []
    assert "screenshot" not in {item.artifact_type for item in result.artifacts}
    assert "dom" in {item.artifact_type for item in result.artifacts}


@pytest.mark.asyncio
async def test_web_scrolled_viewport_preserves_original_png_and_manifest() -> None:
    raw_png = _uncompressed_desktop_png()

    class Browser:
        async def capture(self, request):
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/list",
                title="List",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        artifact_type="screenshot",
                        artifact_id=None,
                        content={
                            "purpose": "web_scrolled_viewport",
                            "encoding": "base64",
                            "data": base64.b64encode(raw_png).decode("ascii"),
                        },
                    ),
                    PageCaptureArtifact(
                        artifact_type="page_coverage",
                        artifact_id=None,
                        content={
                            "protocol_version": "web-scrolled-viewport-manifest.v1",
                            "capture_mode": "scroll_offset_viewport",
                            "document_height": 10000,
                            "requested_scroll_offset_px": 1920,
                            "actual_scroll_y": 1920,
                            "viewport_top_css_px": 1920,
                            "viewport_bottom_css_px": 3000,
                            "screenshot": {
                                "size_bytes": len(raw_png),
                                "sha256": "sha256:" + hashlib.sha256(raw_png).hexdigest(),
                            },
                        },
                    ),
                ],
            )

    result = await WebDesktopCapturePolicyBrowser(browser=Browser()).capture(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="web:run-coverage",
            viewport_width=1920,
            viewport_height=1080,
        )
    )

    screenshot = next(item for item in result.artifacts if item.artifact_type == "screenshot")
    manifest = next(item for item in result.artifacts if item.artifact_type == "page_coverage")
    stored = base64.b64decode(screenshot.content["data"], validate=True)
    assert stored == raw_png
    assert "compression" not in screenshot.content
    assert "compressed_size_bytes" not in screenshot.content
    assert manifest.content["screenshot"] == {
        "size_bytes": len(raw_png),
        "sha256": "sha256:" + hashlib.sha256(raw_png).hexdigest(),
    }


def _navigation_snapshot(*entries: tuple[str, str]) -> dict[str, object]:
    return {
        "schema_version": "ui-snapshot.v2",
        "elements": [
            {
                "tag": "a",
                "text": label,
                "href": href,
                "interactive": True,
                "visible": True,
                "hit_test": {
                    "center_hits_target": True,
                    "visual_occlusion_detected": False,
                    "pointer_interception": False,
                },
            }
            for label, href in entries
        ],
    }


@pytest.mark.asyncio
async def test_web_entry_probe_preserves_gate_and_keeps_dimensions() -> None:
    raw_png = _uncompressed_desktop_png()

    class Browser:
        def __init__(self) -> None:
            self.request = None

        async def capture(self, request):
            self.request = request
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/home",
                title="Home",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        artifact_type="screenshot",
                        artifact_id=None,
                        content={
                            "encoding": "base64",
                            "data": base64.b64encode(raw_png).decode("ascii"),
                        },
                    ),
                    PageCaptureArtifact(
                        artifact_type="dom",
                        artifact_id=None,
                        content=(
                            '<ul class="main-navigation"><li><a href="/reports">'
                            '报表</a></li><li><a href="/metrics">指标</a></li></ul>'
                        ),
                    ),
                    PageCaptureArtifact(
                        artifact_type="ui_snapshot",
                        artifact_id=None,
                        content=_navigation_snapshot(("报表", "/reports"), ("指标", "/metrics")),
                    ),
                ],
            )

    browser = Browser()
    policy = WebDesktopCapturePolicyBrowser(browser=browser)

    result = await policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url="https://example.test/home",
            session_id="web:run-1",
            viewport_width=1920,
            viewport_height=1080,
            web_capture_stage=WebCaptureStage.ENTRY_PROBE.value,
        )
    )

    assert browser.request.full_page is False
    assert browser.request.startup_policy == "direct_home"
    assert browser.request.clear_obstructions is False
    screenshot = next(item for item in result.artifacts if item.artifact_type == "screenshot")
    compressed = base64.b64decode(screenshot.content["data"])
    assert len(compressed) < len(raw_png)
    assert screenshot.content["compression"] == "png_lossless_optimize"
    with Image.open(BytesIO(compressed)) as image:
        assert image.size == (1920, 1080)
    snapshot = next(item for item in result.artifacts if item.artifact_type == "ui_snapshot")
    assert snapshot.content["web_capture_stage"] == "web_entry_probe"
    navigation = snapshot.content["web_navigation"]["candidates"]
    assert len(navigation) == 2
    assert navigation[0] == {
        "entry_id": navigation[0]["entry_id"],
        "label": "报表",
        "url": "https://example.test/reports",
        "menu_path": ["报表"],
        "dom_fingerprint": navigation[0]["dom_fingerprint"],
        "source_signal": "class_or_id_contains_nav",
        "page_type_key": "nav-class:报表",
        "source_selector": WEB_NAVIGATION_SOURCE_SELECTOR,
        "menu_position": 1,
        "source_capture_id": None,
        "source_kind": "anchor",
        "target_hint": "/reports",
        "source_epoch": 1,
        "visibility": "visible",
        "actionability": "actionable",
        "risk_level": "low",
        "allowlist_match": False,
        "navigation_container_id": navigation[0]["navigation_container_id"],
        "navigation_container_restore_seed": navigation[0]["navigation_container_restore_seed"],
        "navigation_evidence": ["WEB_NAVIGATION_CONTAINER_PROVEN"],
        "parent_menu_entry_id": None,
        "parent_logical_target_identity": None,
        "menu_depth": 1,
        "exploration_path": ["报表"],
        "logical_target_identity": navigation[0]["logical_target_identity"],
        "source_page_url": "https://example.test/home",
        "frontier_route_shape": None,
        "frontier_dom_role": None,
        "frontier_structure_fingerprint": None,
        "frontier_structure_cluster_key": None,
        "frontier_site_origin": None,
    }
    assert navigation[0]["entry_id"].startswith("web-nav-entry:")
    assert navigation[0]["dom_fingerprint"].startswith("web-nav-dom:")


def test_bootstrap_blank_surface_skips_navigation_extraction() -> None:
    """首跳 before 证据允许 about:blank，但不能把它当成正式网页解析菜单。"""

    policy = WebDesktopCapturePolicyBrowser(browser=object())
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        session_id="web:run-blank",
        bootstrap_blank_surface=True,
        viewport_width=1920,
        viewport_height=1080,
    )

    result = policy._apply_result_policy(
        request,
        PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="about:blank",
            title="",
            status_code=None,
            artifacts=[
                PageCaptureArtifact("dom", None, "<html><body></body></html>"),
                PageCaptureArtifact(
                    "ui_snapshot",
                    None,
                    {"schema_version": "ui-snapshot.v1", "elements": []},
                ),
            ],
        ),
    )

    assert policy.navigation_candidates("web:run-blank") == ()
    snapshot = next(item for item in result.artifacts if item.artifact_type == "ui_snapshot")
    assert snapshot.content["web_navigation"]["candidates"] == []


@pytest.mark.parametrize(
    "raw",
    [b"not-a-png", _png(1280, 720)],
    ids=["non-png", "wrong-dimensions"],
)
def test_web_screenshot_contract_fails_closed(raw: bytes) -> None:
    with pytest.raises(WebScreenshotContractError):
        _compress_screenshot_content(
            {
                "encoding": "base64",
                "data": base64.b64encode(raw).decode("ascii"),
            }
        )


def test_web_screenshot_sequence_requires_at_least_one_png_frame() -> None:
    with pytest.raises(WebScreenshotContractError, match="每个序列帧必须恰好包含一张 PNG"):
        _compress_screenshot_content({"sequence": True, "frames": [{"index": 0}]})


def test_web_screenshot_sequence_rejects_partially_empty_frames() -> None:
    raw = _uncompressed_desktop_png()

    with pytest.raises(WebScreenshotContractError, match="每个序列帧必须恰好包含一张 PNG"):
        _compress_screenshot_content(
            {
                "sequence": True,
                "frames": [
                    {
                        "index": 0,
                        "encoding": "base64",
                        "data": base64.b64encode(raw).decode("ascii"),
                    },
                    {"index": 1},
                ],
            }
        )


def test_web_screenshot_sequence_rejects_png_outside_frames() -> None:
    raw = _uncompressed_desktop_png()

    with pytest.raises(WebScreenshotContractError, match="PNG 只能位于 frames"):
        _compress_screenshot_content(
            {
                "sequence": True,
                "encoding": "base64",
                "data": base64.b64encode(raw).decode("ascii"),
                "frames": [
                    {
                        "index": 0,
                        "encoding": "base64",
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                ],
            }
        )


def test_web_screenshot_sequence_rejects_wrong_size_frame() -> None:
    wrong_size = _png(1280, 720)

    with pytest.raises(WebScreenshotContractError, match="1920×1080"):
        _compress_screenshot_content(
            {
                "sequence": True,
                "frames": [
                    {
                        "index": 0,
                        "encoding": "base64",
                        "data": base64.b64encode(wrong_size).decode("ascii"),
                    }
                ],
            }
        )


def test_web_screenshot_sequence_compresses_every_explicit_png_frame() -> None:
    first = _uncompressed_desktop_png()
    second = _uncompressed_desktop_png()

    compressed = _compress_screenshot_content(
        {
            "schema_version": "screenshot-sequence.v1",
            "sequence": True,
            "frames": [
                {
                    "index": 0,
                    "encoding": "base64",
                    "data": base64.b64encode(first).decode("ascii"),
                },
                {
                    "index": 1,
                    "encoding": "base64",
                    "data": base64.b64encode(second).decode("ascii"),
                },
            ],
        }
    )

    assert compressed["schema_version"] == "screenshot-sequence.v1"
    assert [frame["index"] for frame in compressed["frames"]] == [0, 1]
    assert all(
        frame["compression"] == "png_lossless_optimize"
        and frame["pixel_width"] == 1920
        and frame["pixel_height"] == 1080
        for frame in compressed["frames"]
    )


def test_h5_capture_defaults_are_unchanged() -> None:
    request = PageCaptureRequest(mode=PageCaptureMode.GOTO)

    assert request.startup_policy == "h5"
    assert request.web_capture_stage is None


def test_ready_baseline_never_uses_hidden_obstruction_clearing() -> None:
    request = _enforce_web_desktop_request(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            clear_obstructions=True,
            web_capture_stage="web_page_ready_baseline",
        )
    )

    assert request.clear_obstructions is False


def test_navigation_candidates_accumulate_across_pages_until_session_close() -> None:
    policy = WebDesktopCapturePolicyBrowser(browser=object())
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        session_id="web:run-1",
        web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
    )

    for final_url, dom, entries in (
        (
            "https://example.test/home",
            '<nav><ul><li><a href="/first">第一菜单</a></li>'
            '<li><a href="/common">公共分类</a></li></ul></nav>',
            (("第一菜单", "/first"), ("公共分类", "/common")),
        ),
        (
            "https://example.test/first",
            '<div class="section-nav"><ul><li><a href="/second">第二菜单</a>'
            '</li><li><a href="/common">公共分类</a></li></ul></div>',
            (("第二菜单", "/second"), ("公共分类", "/common")),
        ),
    ):
        policy._apply_result_policy(
            request,
            PageCaptureResult(
                execution_status="success",
                mode="observe",
                reachable=True,
                final_url=final_url,
                title="页面",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact("dom", None, dom),
                    PageCaptureArtifact("ui_snapshot", None, _navigation_snapshot(*entries)),
                ],
            ),
        )

    candidates = policy.navigation_candidates("web:run-1")
    assert [item.url for item in candidates] == [
        "https://example.test/first",
        "https://example.test/common",
        "https://example.test/second",
    ]
    assert request.clear_obstructions is False


@pytest.mark.asyncio
@pytest.mark.parametrize("page_role", ["home", "menu", "detail"])
async def test_deferred_navigation_materializes_for_same_bound_authorized_capture(
    page_role: str,
) -> None:
    class Browser:
        async def capture(self, request):
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/home",
                title="首页",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        "dom",
                        None,
                        '<nav><a href="/menu">菜单</a><a href="/other">其他</a></nav>',
                    ),
                    PageCaptureArtifact(
                        "ui_snapshot",
                        None,
                        _navigation_snapshot(("菜单", "/menu"), ("其他", "/other")),
                    ),
                ],
            )

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    policy.defer_navigation_discovery_until_authorized()
    captured = await policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="web:run-authorized",
            web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
        )
    )
    snapshot = next(
        artifact.content
        for artifact in captured.artifacts
        if artifact.artifact_type == "ui_snapshot"
    )
    assert [item["url"] for item in snapshot["web_navigation"]["candidates"]] == [
        "https://example.test/menu",
        "https://example.test/other",
    ]
    assert policy.navigation_candidates("web:run-authorized") == ()
    policy.bind_latest_navigation_capture("web:run-authorized", "capture-authorized")

    candidates = policy.authorized_navigation_candidates(
        "web:run-authorized",
        capture_id="capture-authorized",
        page_role=page_role,
    )

    assert [item.url for item in candidates] == [
        "https://example.test/menu",
        "https://example.test/other",
    ]
    assert all(item.source_capture_id == "capture-authorized" for item in candidates)


@pytest.mark.asyncio
async def test_supplemental_full_page_capture_does_not_clear_deferred_home_hrefs() -> None:
    class Browser:
        calls = 0

        async def capture(self, request):
            self.calls += 1
            artifacts = []
            if self.calls == 1:
                artifacts = [
                    PageCaptureArtifact(
                        "dom",
                        None,
                        '<nav><a href="/menu">菜单</a><a href="/other">其他</a></nav>',
                    ),
                    PageCaptureArtifact(
                        "ui_snapshot",
                        None,
                        _navigation_snapshot(("菜单", "/menu"), ("其他", "/other")),
                    ),
                ]
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/home",
                title="首页",
                status_code=200,
                artifacts=artifacts,
            )

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    policy.defer_navigation_discovery_until_authorized()
    await policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="web:run-supplemental",
            web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
        )
    )
    policy.bind_latest_navigation_capture("web:run-supplemental", "capture-ready")

    await policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="web:run-supplemental",
            web_capture_stage="web_full_page_supplemental",
        )
    )

    candidates = policy.authorized_navigation_candidates(
        "web:run-supplemental",
        capture_id="capture-ready",
        page_role="home",
    )
    assert [item.url for item in candidates] == [
        "https://example.test/menu",
        "https://example.test/other",
    ]


@pytest.mark.asyncio
async def test_primary_content_capture_does_not_replace_deferred_ready_home_hrefs() -> None:
    class Browser:
        calls = 0

        async def capture(self, request):
            self.calls += 1
            artifacts = (
                [
                    PageCaptureArtifact(
                        "dom",
                        None,
                        '<nav><a href="/menu">菜单</a><a href="/other">其他</a></nav>',
                    ),
                    PageCaptureArtifact(
                        "ui_snapshot",
                        None,
                        _navigation_snapshot(("菜单", "/menu"), ("其他", "/other")),
                    ),
                ]
                if self.calls == 1
                else [PageCaptureArtifact("dom", None, "<main>正文下半部分</main>")]
            )
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://example.test/home",
                title="首页",
                status_code=200,
                artifacts=artifacts,
            )

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    policy.defer_navigation_discovery_until_authorized()
    await policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="web:run-primary-deferred",
            web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
        )
    )
    policy.bind_latest_navigation_capture("web:run-primary-deferred", "capture-ready")

    await policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            session_id="web:run-primary-deferred",
            trigger_action="scroll",
            web_capture_stage="web_primary_content_evidence",
        )
    )

    candidates = policy.authorized_navigation_candidates(
        "web:run-primary-deferred",
        capture_id="capture-ready",
        page_role="home",
    )
    assert [item.url for item in candidates] == [
        "https://example.test/menu",
        "https://example.test/other",
    ]


@pytest.mark.asyncio
async def test_page_probe_cannot_materialize_home_navigation_before_stable_baseline() -> None:
    class Browser:
        async def capture(self, request):
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url="https://stable-home-only.test/",
                title="仍在清障",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        "dom",
                        None,
                        '<header><nav><a href="/a">分类甲</a>'
                        '<a href="/b">分类乙</a></nav></header>',
                    ),
                    PageCaptureArtifact(
                        "ui_snapshot",
                        None,
                        _navigation_snapshot(("分类甲", "/a"), ("分类乙", "/b")),
                    ),
                ],
            )

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    policy.defer_navigation_discovery_until_authorized()
    await policy.capture(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="web:run-not-ready",
            web_capture_stage=WebCaptureStage.PAGE_PROBE.value,
        )
    )
    policy.bind_latest_navigation_capture("web:run-not-ready", "capture-page-probe")

    assert (
        policy.authorized_navigation_candidates(
            "web:run-not-ready",
            capture_id="capture-page-probe",
            page_role="home",
        )
        == ()
    )


@pytest.mark.asyncio
async def test_rejected_deferred_capture_cannot_pollute_next_authorized_capture() -> None:
    class Browser:
        calls = 0

        async def capture(self, request):
            self.calls += 1
            entries, final_url = (
                (
                    (
                        ("旧危险菜单", "https://example.test/leaked"),
                        ("旧危险菜单二", "https://example.test/leaked-two"),
                    ),
                    "https://outside.test/",
                )
                if self.calls == 1
                else (
                    (("当前菜单", "/current"), ("当前菜单二", "/current-two")),
                    "https://example.test/home",
                )
            )
            return PageCaptureResult(
                execution_status="success",
                mode=request.mode.value,
                reachable=True,
                final_url=final_url,
                title=entries[0][0],
                status_code=200,
                artifacts=[
                    PageCaptureArtifact(
                        "dom",
                        None,
                        "<nav>"
                        + "".join(f'<a href="{href}">{label}</a>' for label, href in entries)
                        + "</nav>",
                    ),
                    PageCaptureArtifact(
                        "ui_snapshot",
                        None,
                        _navigation_snapshot(*entries),
                    ),
                ],
            )

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())
    policy.defer_navigation_discovery_until_authorized()
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        session_id="web:run-rejected",
        web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
    )
    await policy.capture(request)
    policy.bind_latest_navigation_capture("web:run-rejected", "capture-rejected")
    await policy.capture(request)
    policy.bind_latest_navigation_capture("web:run-rejected", "capture-current")

    candidates = policy.authorized_navigation_candidates(
        "web:run-rejected",
        capture_id="capture-current",
        page_role="home",
    )

    assert [item.url for item in candidates] == [
        "https://example.test/current",
        "https://example.test/current-two",
    ]
    assert all(item.source_capture_id == "capture-current" for item in candidates)


def test_navigation_candidates_keep_discovery_capture_and_refresh_action_capture() -> None:
    policy = WebDesktopCapturePolicyBrowser(browser=object())
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        session_id="web:run-1",
        web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
    )
    first_dom = (
        '<nav><ul><li><a href="/same">同一菜单</a></li>'
        '<li><a href="/common">公共分类</a></li></ul></nav>'
    )
    shifted_dom = (
        '<nav><a href="/other">占位</a><ul><li><a href="/same">同一菜单</a></li></ul></nav>'
    )

    for capture_id, dom, entries in (
        ("capture-discovery", first_dom, (("同一菜单", "/same"), ("公共分类", "/common"))),
        ("capture-action-latest", shifted_dom, (("占位", "/other"), ("同一菜单", "/same"))),
    ):
        policy._apply_result_policy(
            request,
            PageCaptureResult(
                execution_status="success",
                mode="observe",
                reachable=True,
                final_url="https://example.test/home",
                title="页面",
                status_code=200,
                artifacts=[
                    PageCaptureArtifact("dom", None, dom),
                    PageCaptureArtifact("ui_snapshot", None, _navigation_snapshot(*entries)),
                ],
            ),
        )
        policy.bind_latest_navigation_capture("web:run-1", capture_id)

    same = [
        item
        for item in policy.navigation_candidates("web:run-1")
        if item.url == "https://example.test/same"
    ]
    assert len(same) == 1
    assert same[0].discovery_capture_id == "capture-discovery"
    assert same[0].source_capture_id == "capture-action-latest"
    assert same[0].source_epoch == 2


def test_primary_content_capture_keeps_latest_navigation_binding() -> None:
    """同页主体补图没有可见导航时，不能清空下一菜单的权威来源绑定。"""

    policy = WebDesktopCapturePolicyBrowser(browser=object())
    session_id = "web:run-primary"
    policy._apply_result_policy(
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id=session_id,
            web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
        ),
        PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="https://example.test/home",
            title="首页",
            status_code=200,
            artifacts=[
                PageCaptureArtifact(
                    "dom",
                    None,
                    '<nav><ul><li><a href="/menu">菜单</a></li>'
                    '<li><a href="/other">其他</a></li></ul></nav><main>正文</main>',
                ),
                PageCaptureArtifact(
                    "ui_snapshot", None, _navigation_snapshot(("菜单", "/menu"), ("其他", "/other"))
                ),
            ],
        ),
    )
    policy.bind_latest_navigation_capture(session_id, "capture-ready")

    policy._apply_result_policy(
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            session_id=session_id,
            trigger_action="scroll",
            web_capture_stage="web_primary_content_evidence",
        ),
        PageCaptureResult(
            execution_status="success",
            mode="single_trigger",
            reachable=True,
            final_url="https://example.test/home",
            title="首页主体",
            status_code=None,
            artifacts=[PageCaptureArtifact("dom", None, "<main>正文下半部分</main>")],
        ),
    )
    policy.bind_latest_navigation_capture(session_id, "capture-primary")

    candidates = policy.navigation_candidates(session_id)
    assert [item.url for item in candidates] == [
        "https://example.test/menu",
        "https://example.test/other",
    ]
    candidate = next(item for item in candidates if item.url == "https://example.test/menu")
    assert candidate.discovery_capture_id == "capture-ready"
    assert candidate.source_capture_id == "capture-primary"


def test_primary_content_capture_does_not_rebind_navigation_from_another_page() -> None:
    """主体补图若已换页，不得把旧页面导航伪装成当前可执行控件。"""

    policy = WebDesktopCapturePolicyBrowser(browser=object())
    session_id = "web:run-cross-page-primary"
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        session_id=session_id,
        web_capture_stage=WebCaptureStage.PAGE_READY_BASELINE.value,
    )
    policy._apply_result_policy(
        request,
        PageCaptureResult(
            execution_status="success",
            mode="observe",
            reachable=True,
            final_url="https://example.test/home",
            title="首页",
            status_code=200,
            artifacts=[
                PageCaptureArtifact(
                    "dom",
                    None,
                    '<nav><ul><li><a href="/menu">菜单</a></li>'
                    '<li><a href="/other">其他</a></li></ul></nav>',
                ),
                PageCaptureArtifact(
                    "ui_snapshot", None, _navigation_snapshot(("菜单", "/menu"), ("其他", "/other"))
                ),
            ],
        ),
    )
    policy.bind_latest_navigation_capture(session_id, "capture-home")

    policy._apply_result_policy(
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            session_id=session_id,
            trigger_action="scroll",
            web_capture_stage="web_primary_content_evidence",
        ),
        PageCaptureResult(
            execution_status="success",
            mode="single_trigger",
            reachable=True,
            final_url="https://example.test/untrusted",
            title="其他页面",
            status_code=None,
            artifacts=[PageCaptureArtifact("dom", None, "<main>其他正文</main>")],
        ),
    )
    policy.bind_latest_navigation_capture(session_id, "capture-other")

    candidate = next(
        item
        for item in policy.navigation_candidates(session_id)
        if item.url == "https://example.test/menu"
    )
    assert candidate.source_capture_id == "capture-home"


@pytest.mark.asyncio
async def test_web_policy_delegates_session_close() -> None:
    calls: list[str] = []

    class Browser:
        async def close_session(self, session_id):
            calls.append(session_id)

    policy = WebDesktopCapturePolicyBrowser(browser=Browser())

    await policy.close_session("web:run-1")

    assert calls == ["web:run-1"]


def test_capture_runtime_enables_policy_only_for_explicit_web_v2_composition() -> None:
    raw_browser = object()

    web_runtime = build_default_web_capture_runtime(
        browser=raw_browser,
        session_owner="web-v2-workflow",
        include_h5_exploration=False,
        logical_surface_enabled=False,
        logical_surface_shadow=False,
        logical_targets_enabled=False,
        web_desktop_policy_enabled=True,
    )
    legacy_runtime = build_default_web_capture_runtime(
        browser=raw_browser,
        web_desktop_policy_enabled=False,
    )

    assert isinstance(
        web_runtime.borrow_browser_for_legacy_agent_tool(),
        WebDesktopCapturePolicyBrowser,
    )
    assert legacy_runtime.borrow_browser_for_legacy_agent_tool() is raw_browser


def test_capture_runtime_selects_web_proxy_without_changing_h5(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web V2 与 H5 Runtime 必须分别消费各自的代理设置。"""
    browser_arguments: list[dict[str, object]] = []

    class Browser:
        def __init__(self, **kwargs) -> None:
            browser_arguments.append(dict(kwargs))

    workflow = SimpleNamespace(
        h5_visual_coordinate_runtime_mode="off",
        h5_logical_surface_enabled=False,
        h5_logical_surface_shadow=False,
        h5_logical_targets_effective=False,
        h5_browser_proxy_server="http://h5-proxy.internal:8080",
        h5_browser_proxy_bypass="h5.internal",
        web_browser_proxy_server="http://host.docker.internal:7897",
        web_browser_proxy_bypass="localhost,127.0.0.1",
    )
    monkeypatch.setattr(capture_runtime_module, "PlaywrightPageCaptureBrowser", Browser)
    monkeypatch.setattr(
        capture_runtime_module,
        "build_evidence_artifact_store",
        lambda _settings: object(),
    )
    monkeypatch.setattr(
        capture_runtime_module,
        "get_settings",
        lambda: SimpleNamespace(
            workflow=workflow,
            evidence_storage=object(),
            quality_optimization=SimpleNamespace(web_interaction_timeout_seconds=60.0),
        ),
    )

    build_default_web_capture_runtime(
        session_owner="web-v2-workflow",
        include_h5_exploration=False,
        h5_visual_coordinate_mode="off",
        logical_surface_enabled=False,
        logical_surface_shadow=False,
        logical_targets_enabled=False,
        web_desktop_policy_enabled=True,
    )
    build_default_web_capture_runtime(web_desktop_policy_enabled=False)

    assert browser_arguments == [
        {
            "logical_surface_enabled": False,
            "logical_surface_shadow": False,
            "h5_visual_coordinate_mode": "off",
            "proxy_server": "http://host.docker.internal:7897",
            "proxy_bypass": "localhost,127.0.0.1",
        },
        {
            "logical_surface_enabled": False,
            "logical_surface_shadow": False,
            "h5_visual_coordinate_mode": "off",
            "proxy_server": "http://h5-proxy.internal:8080",
            "proxy_bypass": "h5.internal",
        },
    ]


@pytest.mark.asyncio
async def test_direct_home_does_not_wait_for_h5_line_loading_text(monkeypatch) -> None:
    class Page:
        async def inner_text(self, selector, timeout):
            del selector, timeout
            return "正在获取最佳线路"

    async def _monitor(_page):
        return 0

    async def _readiness(_page):
        return {
            "core_present": True,
            "necessary_media_ready": True,
            "visible_loader": False,
            "transition_active": False,
            "flutter_bootstrap_pending": False,
            "dom_quiet_ms": 1000,
        }

    async def _wait(**_kwargs):
        return 0

    monkeypatch.setattr(page_capture_module, "_install_page_state_monitor", _monitor)
    monkeypatch.setattr(page_capture_module, "_read_page_readiness", _readiness)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_state_change", _wait)

    settled = await page_capture_module._wait_for_page_settle(
        page=Page(),
        max_wait_ms=50,
        poll_interval_ms=1,
        stable_samples=1,
        startup_policy="direct_home",
    )

    assert settled is True


@pytest.mark.asyncio
async def test_page_entry_detects_first_popup_before_remaining_chain(monkeypatch) -> None:
    calls: list[str] = []

    async def _resolve(**_kwargs):
        calls.append("detect")
        return {"dialog_present": True, "kind": "close_control"}

    async def _trigger(**_kwargs):
        calls.append("dismiss_first")

    async def _remaining(**_kwargs):
        calls.append("clear_remaining")
        return {
            "enabled": True,
            "dismissal_count": 1,
            "outcome": "stable",
            "remaining_obstruction": False,
            "llm_fallback_required": False,
        }

    monkeypatch.setattr(
        page_capture_module,
        "_resolve_first_popup_dismissal_target",
        _resolve,
    )
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", _trigger)
    monkeypatch.setattr(page_capture_module, "_clear_remaining_obstructions", _remaining)

    metrics = await page_capture_module._clear_page_entry_obstructions(
        page=object(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url="https://example.test/home",
            capture_screenshot=False,
        ),
        action_trace=[],
        obstruction_frames=[],
        timeout_ms=5000,
        timeout_error_type=TimeoutError,
    )

    assert calls == ["detect", "dismiss_first", "clear_remaining"]
    assert metrics["detected"] is True
