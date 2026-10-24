"""测试 page_capture_tool：统一页面采集入口与独立 Playwright 浏览器处理层。"""

import asyncio
import base64
import hashlib
import json
import pickle
from collections.abc import Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

import cv2
import numpy as np
import pytest

from services.api.app.agents.runtime.action_trace import InMemoryActionTraceSink
from services.api.app.agents.runtime.agent_factory import (
    build_main_agent,
    run_main_agent,
)
from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.runtime.tool_failure_policy import classify_tool_exception
from services.api.app.agents.tools import page_capture_tools  # noqa: F401  # 触发 Tool 注册
from services.api.app.agents.tools.page_capture_tools import (
    _safe_ui_summary,
    capture_page_for_agent,
    page_capture_tool,
)
from services.api.app.agents.tools.registry import PermissionLevel, default_registry
from services.api.app.application.h5_adaptive_navigation import (
    discover_h5_adaptive_navigation_patterns,
)
from services.api.app.application.h5_logical_surface_evidence import (
    build_h5_logical_surface,
)
from services.api.app.application.h5_structural_navigation import (
    h5_flutter_bottom_navigation_runtime_selector,
)
from services.api.app.application.interaction_runtime import (
    LogicalTargetRuntimeBinding,
    VisualNavigationRuntimeTarget,
    logical_target_snapshot_identity_signature,
    logical_target_snapshot_signature,
)
from services.api.app.application.page_capture_persistence import (
    PersistedEvidenceArtifact,
    PersistedPageCapture,
)
from services.api.app.core.config import AgentRuntimeSettings
from services.api.app.domain.exploration.age_confirm import (
    AGE_CONFIRM_ENTER_SELECTOR,
)
from services.api.app.domain.exploration.h5_logical_surface import (
    stable_h5_surface_hash,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    build_visual_navigation_capability,
    classify_flutter_semantics_coverage,
    verify_visual_navigation_capability,
    visual_navigation_region_digests,
)
from services.api.app.infrastructure.browser import page_capture as page_capture_module
from services.api.app.infrastructure.browser.page_capture import (
    BlockedPageCaptureActionError,
    BrowserSessionCrashedError,
    PageCaptureArtifact,
    PageCaptureMode,
    PageCaptureRequest,
    PageCaptureResult,
    PageInteractionFailedError,
    PageTriggerAction,
    PlaywrightPageCaptureBrowser,
    _artifact_payload_metrics,
    _assess_capture_boundary_consistency,
    _assess_page_inspection_readiness,
    _assess_scroll_segment_continuity,
    _attach_observers,
    _browser_context_options,
    _browser_viewport_options,
    _click_startup_ad_with_pointer_interception_fallback,
    _collect_media_state,
    _collect_runtime_artifacts,
    _collect_timed_artifacts,
    _collect_ui_snapshot,
    _compact_scroll_ui_snapshot,
    _contains_active_countdown,
    _goto_with_retry,
    _read_capture_boundary_identity,
    _record_navigation_outcome,
    _record_scroll_effect,
    _run_single_trigger,
    _sanitize_and_bound_ui_snapshot,
    _single_segment_coverage,
    _ui_structure_signature,
    _validate_live_logical_target_binding,
    _validate_request,
    _wait_for_page_settle,
    capture_page,
)
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_function_call_response,
    make_text_response,
)


@pytest.mark.asyncio
async def test_browser_launch_keeps_proxy_absent_by_default() -> None:
    calls: list[dict[str, Any]] = []
    launched = object()

    class Chromium:
        async def launch(self, **kwargs):  # noqa: ANN003
            calls.append(dict(kwargs))
            return launched

    browser = PlaywrightPageCaptureBrowser()

    result = await browser._launch_browser(SimpleNamespace(chromium=Chromium()))

    assert result is launched
    assert calls == [{"headless": True}]


def test_live_logical_navigation_preflight_rejects_stale_page_state() -> None:
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature="sha256:" + "3" * 64,
    )
    live_snapshot = {
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": binding.source_identity_fingerprint,
            "state_fingerprint": "sha256:" + "4" * 64,
        }
    }

    with pytest.raises(BlockedPageCaptureActionError) as error:
        _validate_live_logical_target_binding(
            snapshot=live_snapshot,
            binding=binding,
        )

    assert error.value.error_code == "LOGICAL_SOURCE_STATE_STALE"


def test_live_h5_popup_preflight_accepts_dynamic_state_with_same_target() -> None:
    element = {
        "selector": "#app > .persalePopup > .van-overlay",
        "tag": "div",
        "role": None,
        "text": "",
        "parent_selector": "#app > .persalePopup",
        "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
        "visible": True,
        "hit_test": {"center_hits_target": True},
    }
    signature = logical_target_snapshot_signature(element, target_source="dom")
    assert signature is not None
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature=signature,
        target_source="dom",
        allow_dynamic_state_revalidation=True,
    )
    live_snapshot = {
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": binding.source_identity_fingerprint,
            "state_fingerprint": "sha256:" + "4" * 64,
        },
        "elements": [element],
    }

    _validate_live_logical_target_binding(snapshot=live_snapshot, binding=binding)


def test_live_h5_popup_preflight_rejects_changed_target_structure() -> None:
    source_element = {
        "selector": "#app > .persalePopup > .van-overlay",
        "tag": "div",
        "role": None,
        "text": "",
        "parent_selector": "#app > .persalePopup",
        "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
        "visible": True,
        "hit_test": {"center_hits_target": True},
    }
    signature = logical_target_snapshot_signature(source_element, target_source="dom")
    assert signature is not None
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature=signature,
        target_source="dom",
        allow_dynamic_state_revalidation=True,
    )
    changed_element = {
        **source_element,
        "parent_selector": "#app > .otherPopup",
    }
    live_snapshot = {
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": binding.source_identity_fingerprint,
            "state_fingerprint": "sha256:" + "4" * 64,
        },
        "elements": [changed_element],
    }

    with pytest.raises(BlockedPageCaptureActionError) as error:
        _validate_live_logical_target_binding(
            snapshot=live_snapshot,
            binding=binding,
        )

    assert error.value.error_code == "LOGICAL_SOURCE_STATE_STALE"


def test_live_h5_navigation_preflight_accepts_same_target_after_layout_shift() -> None:
    source_element = {
        "selector": "#list > .card:nth-of-type(1) > .videoTitle",
        "tag": "div",
        "role": None,
        "text": "固定视频标题",
        "parent_selector": "#list > .card:nth-of-type(1)",
        "rect": {"x": 10, "y": 516, "width": 181, "height": 36},
        "visible": True,
        "hit_test": {"center_hits_target": True},
    }
    signature = logical_target_snapshot_signature(source_element, target_source="dom")
    identity_signature = logical_target_snapshot_identity_signature(
        source_element,
        target_source="dom",
    )
    assert signature is not None
    assert identity_signature is not None
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature=signature,
        target_source="dom",
        target_identity_signature=identity_signature,
        allow_dynamic_state_revalidation=True,
        allow_dynamic_geometry_revalidation=True,
    )
    shifted_element = {
        **source_element,
        "rect": {"x": 10, "y": 454, "width": 181, "height": 36},
    }
    live_snapshot = {
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": binding.source_identity_fingerprint,
            "state_fingerprint": "sha256:" + "4" * 64,
        },
        "elements": [shifted_element],
    }

    _validate_live_logical_target_binding(snapshot=live_snapshot, binding=binding)


def test_live_h5_page_tab_preflight_accepts_horizontal_offscreen_target() -> None:
    """横向滚动栏页签可在最终点击前按唯一稳定身份复验。"""
    source_element = {
        "selector": "#van-tabs-1-1",
        "tag": "div",
        "role": "tab",
        "text": "推荐",
        "parent_selector": "#hometab > div:nth-of-type(1) > div",
        "rect": {"x": -366.47, "y": 45.75, "width": 58.22, "height": 51.98},
        "visible": True,
        "hit_test": {"center_hits_target": False},
    }
    signature = logical_target_snapshot_signature(source_element, target_source="dom")
    identity_signature = logical_target_snapshot_identity_signature(
        source_element,
        target_source="dom",
    )
    assert signature is not None
    assert identity_signature is not None
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature=signature,
        target_source="dom",
        target_identity_signature=identity_signature,
        allow_dynamic_state_revalidation=True,
        allow_dynamic_geometry_revalidation=True,
        allow_horizontal_offscreen_revalidation=True,
    )
    live_snapshot = {
        "viewport": {"width": 390, "height": 844},
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": binding.source_identity_fingerprint,
            "state_fingerprint": "sha256:" + "4" * 64,
        },
        "elements": [source_element],
    }

    _validate_live_logical_target_binding(snapshot=live_snapshot, binding=binding)


def test_live_h5_page_tab_preflight_rejects_intercepted_in_viewport_target() -> None:
    """能力位不得把视口内被遮挡的页签误当成横向离屏页签。"""
    source_element = {
        "selector": "#van-tabs-1-1",
        "tag": "div",
        "role": "tab",
        "text": "推荐",
        "parent_selector": "#hometab > div:nth-of-type(1) > div",
        "rect": {"x": 100, "y": 45.75, "width": 58.22, "height": 51.98},
        "visible": True,
        "hit_test": {"center_hits_target": False},
    }
    signature = logical_target_snapshot_signature(source_element, target_source="dom")
    identity_signature = logical_target_snapshot_identity_signature(
        source_element,
        target_source="dom",
    )
    assert signature is not None
    assert identity_signature is not None
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature=signature,
        target_source="dom",
        target_identity_signature=identity_signature,
        allow_dynamic_state_revalidation=True,
        allow_dynamic_geometry_revalidation=True,
        allow_horizontal_offscreen_revalidation=True,
    )
    live_snapshot = {
        "viewport": {"width": 390, "height": 844},
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": binding.source_identity_fingerprint,
            "state_fingerprint": "sha256:" + "4" * 64,
        },
        "elements": [source_element],
    }

    with pytest.raises(BlockedPageCaptureActionError) as error:
        _validate_live_logical_target_binding(snapshot=live_snapshot, binding=binding)

    assert error.value.error_code == "LOGICAL_SOURCE_STATE_STALE"


def test_live_h5_navigation_preflight_rejects_changed_content_after_layout_shift() -> None:
    source_element = {
        "selector": "#list > .card:nth-of-type(1) > .videoTitle",
        "tag": "div",
        "role": None,
        "text": "原视频标题",
        "parent_selector": "#list > .card:nth-of-type(1)",
        "rect": {"x": 10, "y": 516, "width": 181, "height": 36},
        "visible": True,
        "hit_test": {"center_hits_target": True},
    }
    signature = logical_target_snapshot_signature(source_element, target_source="dom")
    identity_signature = logical_target_snapshot_identity_signature(
        source_element,
        target_source="dom",
    )
    assert signature is not None
    assert identity_signature is not None
    binding = LogicalTargetRuntimeBinding(
        source_identity_fingerprint="sha256:" + "1" * 64,
        source_state_fingerprint="sha256:" + "2" * 64,
        target_signature=signature,
        target_source="dom",
        target_identity_signature=identity_signature,
        allow_dynamic_state_revalidation=True,
        allow_dynamic_geometry_revalidation=True,
    )
    changed_element = {
        **source_element,
        "text": "已换成另一条视频",
        "rect": {"x": 10, "y": 454, "width": 181, "height": 36},
    }
    live_snapshot = {
        "logical_surface": {
            "protocol_version": "logical_surface.v1",
            "shadow": False,
            "identity_fingerprint": binding.source_identity_fingerprint,
            "state_fingerprint": "sha256:" + "4" * 64,
        },
        "elements": [changed_element],
    }

    with pytest.raises(BlockedPageCaptureActionError) as error:
        _validate_live_logical_target_binding(snapshot=live_snapshot, binding=binding)

    assert error.value.error_code == "LOGICAL_SOURCE_STATE_STALE"


def test_scroll_effect_keeps_prior_movement_when_final_segment_reaches_bottom() -> None:
    action_trace = [{"action": "scroll", "selector": None}]

    _record_scroll_effect(
        action_trace=action_trace,
        before={"container_selector": "document", "scroll_top": 0},
        after={"container_selector": "document", "scroll_top": 600},
    )
    _record_scroll_effect(
        action_trace=action_trace,
        before={"container_selector": "document", "scroll_top": 600},
        after={"container_selector": "document", "scroll_top": 600},
    )

    effect = action_trace[-1]["state_diff"]["scroll_effect"]
    assert effect["observed"] is True
    assert effect["segment_observed"] is False
    assert effect["scroll_top_before"] == 0
    assert effect["scroll_top_after"] == 600


@pytest.mark.asyncio
async def test_browser_launch_passes_configured_proxy_and_bypass() -> None:
    calls: list[dict[str, Any]] = []
    launched = object()

    class Chromium:
        async def launch(self, **kwargs):  # noqa: ANN003
            calls.append(dict(kwargs))
            return launched

    browser = PlaywrightPageCaptureBrowser(
        proxy_server="http://host.docker.internal:7897",
        proxy_bypass="localhost,127.0.0.1",
    )

    result = await browser._launch_browser(SimpleNamespace(chromium=Chromium()))

    assert result is launched
    assert calls == [
        {
            "headless": True,
            "proxy": {
                "server": "http://host.docker.internal:7897",
                "bypass": "localhost,127.0.0.1",
            },
        }
    ]


@pytest.mark.asyncio
async def test_web_profile_creates_non_touch_desktop_context_at_1920x1080() -> None:
    context_options: list[dict[str, Any]] = []

    class Page:
        def on(self, event: str, callback: object) -> None:
            del event, callback

    class Context:
        async def new_page(self) -> Page:
            return Page()

    class Browser:
        async def new_context(self, **kwargs):  # noqa: ANN003
            context_options.append(dict(kwargs))
            return Context()

    class Chromium:
        async def launch(self, **kwargs):  # noqa: ANN003
            del kwargs
            return Browser()

    class Playwright:
        chromium = Chromium()

        async def stop(self) -> None:
            return None

    class Starter:
        async def start(self) -> Playwright:
            return Playwright()

    browser = PlaywrightPageCaptureBrowser()
    await browser._create_session(
        lambda: Starter(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url="https://example.test",
            session_id="run-web-desktop",
            viewport_width=1920,
            viewport_height=1080,
            screen_width=1920,
            screen_height=1080,
            device_scale_factor=1.0,
            is_mobile=False,
            has_touch=False,
            color_scheme="light",
        ),
    )

    assert context_options == [
        {
            "ignore_https_errors": True,
            "viewport": {"width": 1920, "height": 1080},
            "screen": {"width": 1920, "height": 1080},
            "device_scale_factor": 1.0,
            "is_mobile": False,
            "has_touch": False,
            "color_scheme": "light",
        }
    ]


def test_runtime_artifacts_persist_empty_har_and_action_trace_as_negative_evidence() -> None:
    artifacts = _collect_runtime_artifacts(
        console_logs=[],
        network_requests=[],
        action_trace=[],
    )

    assert [item.artifact_type for item in artifacts] == ["har", "action_trace"]
    assert artifacts[0].content == {"format": "network_events", "entries": []}
    assert artifacts[1].content == []


def test_artifact_token_estimate_excludes_binary_screenshot_payload() -> None:
    artifacts = [
        PageCaptureArtifact(
            artifact_type="visible_text",
            artifact_id="text-1",
            content="页面正文",
        ),
        PageCaptureArtifact(
            artifact_type="screenshot",
            artifact_id="shot-1",
            content={"encoding": "base64", "data": "A" * 20_000},
        ),
    ]

    metrics = _artifact_payload_metrics(artifacts)

    assert metrics["binary_evidence_bytes_total"] == metrics["evidence_bytes"]["screenshot"]
    assert metrics["estimated_text_tokens"] < 100


def test_dom_artifact_compaction_removes_inline_resources_and_keeps_structure() -> None:
    """DOM 证据应保留语义结构，但不得把内联图片和脚本按百 MB 落盘。"""
    huge_resource = "A" * (3 * 1024 * 1024)
    raw_dom = (
        "<!DOCTYPE html><html><head>"
        f"<style>.hero{{background:url(data:image/png;base64,{huge_resource})}}</style>"
        f"<script>window.payload='{huge_resource}'</script>"
        "</head><body><main class='home'>首页</main>"
        f"<img class='poster' src='data:image/png;base64,{huge_resource}'>"
        "<div class='van-tabbar'><div class='van-tabbar-item'>我的</div></div>"
        "</body></html>"
    )

    compacted = page_capture_module._compact_dom_artifact(raw_dom)

    assert len(compacted.encode("utf-8")) <= page_capture_module.MAX_DOM_ARTIFACT_BYTES
    assert huge_resource not in compacted
    assert "class='home'" in compacted
    assert "van-tabbar-item" in compacted
    assert "alioth:inline-resource-redacted" in compacted


def test_dom_artifact_compaction_bounds_large_semantic_dom_with_audit_marker() -> None:
    """去资源后仍超限的异常 DOM 必须保留首尾证据及原文摘要。"""
    repeated = "<article class='card'>正文节点</article>" * 100_000
    raw_dom = f"<html><body><header>页首</header>{repeated}<footer>页尾</footer></body></html>"

    compacted = page_capture_module._compact_dom_artifact(raw_dom)

    assert len(compacted.encode("utf-8")) <= page_capture_module.MAX_DOM_ARTIFACT_BYTES
    assert "页首" in compacted
    assert "页尾" in compacted
    assert "alioth:dom-truncated" in compacted
    assert "original_bytes=" in compacted
    assert "sha256=" in compacted


def test_active_countdown_is_detected_before_page_identification() -> None:
    assert _contains_active_countdown("8s后关闭\n线路读取完成，请稍后") is True
    assert _contains_active_countdown("3 秒后跳过") is True
    assert _contains_active_countdown("关闭\n首页内容") is False


async def test_flutter_fullscreen_bare_number_countdown_is_detected_live() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>html, body { margin: 0; }</style>
    <flutter-view style="width:390px;height:844px;display:block;position:relative">
      <flt-semantics-host>
        <flt-semantics id="startup-layer" flt-tappable
          style="position:absolute;width:390px;height:844px;pointer-events:none">
          <flt-semantics id="startup-countdown" flt-tappable role="text"
            style="position:absolute;width:40px;height:36px;
            transform:matrix(1,0,0,1,334,10);pointer-events:all;z-index:2">3</flt-semantics>
        </flt-semantics>
      </flt-semantics-host>
    </flutter-view>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)

        result = await page_capture_module._resolve_flutter_fullscreen_startup_countdown_target(
            page
        )

        assert result["match_count"] == 1
        assert result["selector"] == "#startup-countdown"
        await browser.close()


async def test_image_only_dialog_close_is_resolved_as_close_control_live() -> None:
    """九宫格广告弹窗的纯图关闭键要判成 close_control，不能退化成点遮罩。

    尺寸取自性巴克（SF02）实站实测：关闭图标 40x40，广告位 79x79。
    该关闭键无文字、无 aria-label，类名只是通用 btn，关键词打分恒为 0；
    此前既不在弹窗内候选清单里也拿不到分，只能回落 kind=overlay 去点
    遮罩背景，而该站不支持点遮罩关闭 —— 每帧机械成功、弹窗纹丝不动。
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>html, body { margin: 0; }</style>
    <div class="van-overlay" style="position:fixed;inset:0;z-index:2001;
      background:rgba(0,0,0,0.86)"></div>
    <div role="dialog" class="van-dialog" style="position:fixed;left:0;top:147px;
      width:390px;z-index:2002;background:#fff">
      <div class="van-dialog__content">
        <div class="grid-main">
          <div class="grid" style="display:grid;
            grid-template-columns:repeat(4,79px);justify-content:center">
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告1</div>
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告2</div>
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告3</div>
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告4</div>
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告5</div>
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告6</div>
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告7</div>
        <div class="banner-item" style="width:79px;height:79px">
          <img style="width:79px;height:79px"></div>
        <div class="item-title" style="display:none">广告8</div>
          </div>
          <div class="btn" style="width:40px;height:44px;margin:0 auto">
            <img style="width:40px;height:40px" alt="">
          </div>
        </div>
      </div>
    </div>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)

        result = await page_capture_module._resolve_popup_dismissal_target(
            page=page,
            selector=".van-overlay:visible",
        )

        assert result is not None
        assert result["kind"] == "close_control"
        # 广告位是 79x79，落不进 20~64 的图标几何，不得被当成关闭键
        assert "banner-item" not in str(result["selector"])
        await browser.close()


async def test_h5_popup_prefers_safe_overlay_and_keeps_close_control_fallback_live() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>html, body { margin: 0; }</style>
    <div class="van-overlay" style="position:fixed;inset:0;z-index:2001">
      <div role="dialog" class="van-dialog" style="position:absolute;left:45px;top:160px;
        width:300px;height:420px;background:#fff">
        <div>活动公告</div>
        <button class="btn-close">关闭</button>
      </div>
    </div>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)

        result = await page_capture_module._resolve_popup_dismissal_target(
            page=page,
            selector=".van-overlay:visible",
            prefer_safe_outside=True,
        )

        assert result is not None
        assert result["kind"] == "overlay"
        assert result["fallback_close_target"]["kind"] == "close_control"
        assert result["fallback_close_target"]["selector"] == "button.btn-close"
        await browser.close()


async def test_flutter_marketing_announcement_binds_sibling_confirm_live() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>html, body { margin: 0; }</style>
    <flutter-view style="width:390px;height:844px;display:block;position:relative">
      <flt-semantics-host>
        <flt-semantics id="page-root"
          style="position:absolute;width:390px;height:844px;pointer-events:none">
          <flt-semantics-container style="position:absolute;pointer-events:none">
            <flt-semantics id="announcement-body"
              style="position:absolute;width:317.2px;height:259.6px;
              transform:matrix(1,0,0,1,36.4,317.3);pointer-events:none">
              <flt-semantics-container style="position:absolute;pointer-events:none">
                <flt-semantics role="text"
                  style="position:absolute;width:280px;height:220px;pointer-events:all">
                  TIKTOK重磅来袭 官方推荐 官方 Telegram 交流群 品牌推广 收藏关注
                </flt-semantics>
              </flt-semantics-container>
            </flt-semantics>
            <flt-semantics id="announcement-confirm" flt-tappable role="text"
              style="position:absolute;width:114.4px;height:37.4px;
              transform:matrix(1,0,0,1,137.8,592.6);pointer-events:all">确认</flt-semantics>
          </flt-semantics-container>
        </flt-semantics>
      </flt-semantics-host>
    </flutter-view>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)

        result = await page_capture_module._resolve_flutter_marketing_announcement_confirm_target(
            page=page
        )

        assert result is not None
        assert result["dialog_present"] is True
        assert result["selector"] == "#announcement-confirm"
        assert result["dialog_selector"] == "#announcement-body"
        await browser.close()


async def test_dense_flutter_page_retains_bottom_navigation_in_snapshot() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    dense_items = "".join(
        f'<flt-semantics id="card-{index}" flt-tappable role="text" '
        f'style="position:absolute;width:56px;height:84px;left:10px;top:{900 + index}px">'
        f"内容{index}</flt-semantics>"
        for index in range(340)
    )
    nav_items = "".join(
        f'<flt-semantics id="nav-{index}" flt-tappable role="text" '
        f'style="position:fixed;width:55px;height:57px;left:{index * 55}px;top:771px">'
        f"{label}</flt-semantics>"
        for index, label in enumerate(("视频", "抖阴", "海角", "暗网", "吃瓜", "我的"), start=1)
    )
    html = (
        '<flutter-view style="width:390px;height:844px;display:block;position:relative">'
        f"<flt-semantics-host>{dense_items}{nav_items}</flt-semantics-host>"
        "</flutter-view>"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)

        snapshot = await _collect_ui_snapshot(page)

        retained = {
            item.get("text"): item
            for item in snapshot["elements"]
            if item.get("structural_navigation_kind") == "flutter_bottom_navigation"
        }
        assert {"视频", "抖阴", "海角", "暗网", "吃瓜", "我的"}.issubset(retained)
        assert retained["我的"]["selector"] == "#nav-6"
        await browser.close()


async def test_flutter_bottom_navigation_click_revalidates_and_observes_effect() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    nav_items = "".join(
        f'<flt-semantics id="nav-{index}" flt-tappable role="text" '
        f'style="position:fixed;width:55px;height:57px;left:{index * 55}px;top:771px" '
        f"onclick=\"document.getElementById('page-content').textContent='{label}页面'\">"
        f"{label}</flt-semantics>"
        for index, label in enumerate(("视频", "抖阴", "海角", "暗网", "吃瓜", "我的"), start=1)
    )
    html = (
        '<flutter-view style="width:390px;height:844px;display:block;position:relative">'
        f'<flt-semantics-host><flt-semantics id="page-content">首页</flt-semantics>'
        f"{nav_items}</flt-semantics-host></flutter-view>"
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)
        trace: list[dict[str, Any]] = []

        await _run_single_trigger(
            page=page,
            context=SimpleNamespace(pages=[page]),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector=h5_flutter_bottom_navigation_runtime_selector(
                    index=6,
                    label="我的",
                ),
            ),
            action_trace=trace,
            timeout_ms=5_000,
        )

        assert await page.locator("#page-content").inner_text() == "我的页面"
        assert trace[-1]["navigation_effect_verified"] is True
        await browser.close()


async def test_flutter_profile_login_entry_revalidates_and_observes_effect() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <flutter-view style="width:390px;height:844px;display:block;position:relative">
      <flt-semantics-host>
        <flt-semantics id="profile-root">
          <flt-semantics id="profile-login" flt-tappable role="group"
            aria-label="微笑自行车\nID: 13228263\n立即登录"
            style="position:absolute;width:359px;height:62px;left:16px;top:31px"
            onclick="this.remove();document.getElementById('login-form').textContent='注册账号'">
            微笑自行车 ID: 13228263 立即登录
          </flt-semantics>
        </flt-semantics>
        <flt-semantics id="login-form">个人中心</flt-semantics>
      </flt-semantics-host>
    </flutter-view>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)
        trace: list[dict[str, Any]] = []

        await _run_single_trigger(
            page=page,
            context=SimpleNamespace(pages=[page]),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector="internal:flutter_profile_login_entry",
            ),
            action_trace=trace,
            timeout_ms=5_000,
        )

        assert await page.locator("#login-form").inner_text() == "注册账号"
        assert trace[-1]["navigation_effect_verified"] is True
        await browser.close()


def test_snapshot_size_pressure_retains_profile_navigation_structures() -> None:
    large_text = "广告内容" * 800
    ordinary = [
        {
            "selector": f"#ad-{index}",
            "tag": "div",
            "text": large_text,
            "class_tokens": ["ad-item"],
            "ancestor_class_tokens": [],
            "interactive": False,
            "rect": {"x": 0, "y": index * 20, "width": 390, "height": 20},
        }
        for index in range(280)
    ]
    protected = [
        {
            "selector": "#profile-setting",
            "tag": "img",
            "text": "",
            "class_tokens": ["setting"],
            "ancestor_class_tokens": ["header-left", "index-header"],
            "interactive": False,
            "rect": {"x": 340, "y": 12, "width": 24, "height": 24},
        },
        {
            "selector": "#profile-checkin",
            "tag": "div",
            "text": "签到",
            "class_tokens": ["checkin-entry"],
            "ancestor_class_tokens": ["top-box"],
            "interactive": False,
            "rect": {"x": 317, "y": 50, "width": 73, "height": 32},
        },
        {
            "selector": "#profile-vip",
            "tag": "img",
            "text": "",
            "class_tokens": ["vip-right-btn"],
            "ancestor_class_tokens": ["vip-img"],
            "interactive": False,
            "rect": {"x": 270, "y": 176, "width": 94, "height": 40},
        },
        {
            "selector": "#profile-top-right-setting",
            "tag": "img",
            "text": "",
            "class_tokens": ["icon", "icon-setting"],
            "ancestor_class_tokens": ["top-right-menu", "top-wrap", "base-info"],
            "interactive": False,
            "rect": {"x": 344, "y": 0, "width": 30, "height": 30},
        },
        {
            "selector": "#profile-vip-center",
            "tag": "img",
            "text": "",
            "src": "https://example.test/static/img/my-vip-center.a83f5aad.png",
            "class_tokens": ["img"],
            "ancestor_class_tokens": ["left", "img-menus-wrap"],
            "interactive": False,
            "rect": {"x": 15, "y": 194, "width": 171, "height": 73},
        },
        {
            "selector": "#profile-daily-signin",
            "tag": "img",
            "text": "",
            "src": "https://example.test/static/img/m-signin-bg.42f9.png",
            "class_tokens": ["img"],
            "ancestor_class_tokens": ["sigin-box", "right", "img-menus-wrap"],
            "interactive": False,
            "rect": {"x": 194, "y": 234, "width": 181, "height": 33},
        },
        {
            "selector": "#site-three-signin",
            "tag": "img",
            "text": "",
            "src": "https://example.test/assets/singi-MikFGLph.png",
            "class_tokens": ["d-img"],
            "parent_class_tokens": ["sign-in"],
            "ancestor_class_tokens": ["sign-in", "mine-info", "mine-content"],
            "interactive": False,
            "rect": {"x": 322, "y": 44, "width": 46, "height": 22},
        },
        {
            "selector": "#site-three-immediate-open",
            "tag": "div",
            "text": "立即开通",
            "class_tokens": ["mine-vip-banner__btn"],
            "parent_class_tokens": ["vip-center", "mine-vip-banner"],
            "ancestor_class_tokens": ["vip-center", "mine-vip-banner"],
            "interactive": False,
            "rect": {"x": 282, "y": 169, "width": 72, "height": 28},
        },
        {
            "selector": "#site-three-member-center",
            "tag": "img",
            "text": "",
            "src": "https://example.test/assets/mine-vip-icon-C6xv1WRC.png",
            "class_tokens": [],
            "parent_class_tokens": ["my-balance-wrap"],
            "ancestor_class_tokens": ["my-balance-wrap", "mine-content"],
            "interactive": False,
            "rect": {"x": 200, "y": 245, "width": 160, "height": 68},
        },
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.test/Mine",
            "viewport": {"width": 390, "height": 844},
            "elements": [*ordinary, *protected],
        }
    )

    selectors = {item["selector"] for item in snapshot["elements"]}
    assert {
        "#profile-setting",
        "#profile-checkin",
        "#profile-vip",
        "#profile-top-right-setting",
        "#profile-vip-center",
        "#profile-daily-signin",
        "#site-three-signin",
        "#site-three-immediate-open",
        "#site-three-member-center",
    }.issubset(selectors)


async def test_timed_capture_collects_screenshot_sequence_and_temporal_trace() -> None:
    class TimedPage:
        def __init__(self) -> None:
            self.text_index = 0

        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            return b"frame"

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            assert selector == "body"
            self.text_index += 1
            return f"倒计时 {4 - self.text_index}"

    artifacts = await _collect_timed_artifacts(
        page=TimedPage(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.TIMED_CAPTURE,
            capture_screenshot=False,
            full_page=False,
            frame_count=3,
            interval_ms=1,
        ),
    )

    assert [item.artifact_type for item in artifacts] == [
        "screenshot",
        "ui_snapshot",
        "temporal_trace",
    ]
    assert len(artifacts[0].content["frames"]) == 3
    assert len(artifacts[1].content["frames"]) == 3
    trace = artifacts[2].content
    assert trace["clock"] == "monotonic"
    assert [item["visible_text"] for item in trace["samples"]] == [
        "倒计时 3",
        "倒计时 2",
        "倒计时 1",
    ]
    offsets = [item["offset_ms"] for item in trace["samples"]]
    assert offsets == sorted(offsets)


async def test_timed_capture_stops_after_countdown_end_event(monkeypatch) -> None:
    class TimedPage:
        async def screenshot(self, *, full_page: bool) -> bytes:
            return b"frame"

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            return "跳过广告"

    event_reads = 0

    async def install_monitor(page: Any) -> int:
        return 1

    async def wait_for_change(**kwargs: Any) -> int:
        return int(kwargs["previous_event_count"]) + 1

    async def read_events(page: Any) -> list[dict[str, Any]]:
        nonlocal event_reads
        event_reads += 1
        if event_reads == 1:
            return [{"kind": "start", "offset_ms": 0, "value": 1}]
        return [
            {"kind": "start", "offset_ms": 0, "value": 1},
            {"kind": "end", "offset_ms": 10, "value": 0},
        ]

    monkeypatch.setattr(page_capture_module, "_install_countdown_monitor", install_monitor)
    monkeypatch.setattr(page_capture_module, "_wait_for_countdown_change", wait_for_change)
    monkeypatch.setattr(page_capture_module, "_read_countdown_events", read_events)

    artifacts = await _collect_timed_artifacts(
        page=TimedPage(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.TIMED_CAPTURE,
            frame_count=8,
            interval_ms=1,
            full_page=False,
        ),
    )

    screenshots = artifacts[0].content["frames"]
    trace = artifacts[2].content
    assert len(screenshots) == 3
    assert trace["countdown_events"][-1]["kind"] == "end"


@pytest.mark.parametrize(
    ("frame_count", "interval_ms", "message"),
    [
        (101, 1000, "frame_count 必须在"),
        (3, 60_001, "interval_ms 必须在"),
        (True, 1000, "frame_count 必须为整数"),
    ],
)
async def test_timed_capture_rejects_unbounded_request(
    frame_count: int,
    interval_ms: int,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await capture_page(
            request=PageCaptureRequest(
                mode=PageCaptureMode.TIMED_CAPTURE,
                frame_count=frame_count,
                interval_ms=interval_ms,
            ),
            browser=FakePageCaptureBrowser(),
        )


async def test_ui_snapshot_collects_browser_geometry_and_isolates_failure() -> None:
    expected = {
        "schema_version": "1.0",
        "viewport": {"width": 390, "height": 844},
        "truncated": False,
        "elements": [
            {
                "element_index": 0,
                "tag": "button",
                "role": None,
                "text": "跳过",
                "aria_label": "跳过广告",
                "disabled": False,
                "hit_bounds": {"x": 10, "y": 20, "width": 48, "height": 48},
                "visual_bounds": {"x": 18, "y": 28, "width": 32, "height": 20},
            }
        ],
    }

    class SnapshotPage:
        async def evaluate(self, script: str) -> dict[str, Any]:
            assert "getBoundingClientRect" in script
            return expected

    class FailingSnapshotPage:
        async def evaluate(self, script: str) -> dict[str, Any]:
            raise RuntimeError("执行上下文已销毁")

    collected = await _collect_ui_snapshot(SnapshotPage())
    assert collected["schema_version"] == "ui-snapshot.v2"
    assert collected["viewport"] == expected["viewport"]
    assert collected["element_count"] == 1
    assert collected["elements"][0]["text"] == "跳过"
    assert len(collected["structure_signature"]) == 64
    failed = await _collect_ui_snapshot(FailingSnapshotPage())
    assert failed["schema_version"] == "ui-snapshot.v2"
    assert failed["elements"] == []
    assert failed["collection_error"] == "执行上下文已销毁"


async def test_ui_snapshot_collects_visual_viewport_and_flutter_host_transform() -> None:
    """视觉执行坐标必须来自浏览器视口事实和完整 Flutter host 变换。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>html, body { margin: 0; }</style>
    <flutter-view>
      <flt-semantics-host style="transform: matrix(0.5, 0.1, -0.2, 0.75, 12, 34)">
        <flt-semantics flt-tappable role="button">首页</flt-semantics>
      </flt-semantics-host>
    </flutter-view>
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)

        snapshot = await _collect_ui_snapshot(page, logical_surface_enabled=True)

        viewport = snapshot["viewport"]
        assert viewport["visual_viewport_width"] == 390
        assert viewport["visual_viewport_height"] == 844
        assert viewport["visual_viewport_offset_left"] == 0
        assert viewport["visual_viewport_offset_top"] == 0
        assert viewport["visual_viewport_page_left"] == 0
        assert viewport["visual_viewport_page_top"] == 0
        assert viewport["visual_viewport_scale"] == 1
        assert viewport["flutter_host_transform"] == [0.5, 0.1, -0.2, 0.75, 12, 34]
        await browser.close()


async def test_ui_snapshot_omits_logical_surface_when_disabled() -> None:
    raw_snapshot = {
        "schema_version": "ui-snapshot.v2",
        "url": "https://example.test/",
        "title": "Flutter 测试页",
        "document": {
            "time_origin_ms": 1000.0,
            "flutter_root_present": True,
        },
        "elements": [
            {
                "selector": "flt-semantics#101",
                "tag": "flt-semantics",
                "role": "button",
                "text": "首页",
                "class_tokens": ["active"],
                "structural_navigation_kind": "flutter_bottom_navigation",
                "interactive": True,
                "rect": {"x": 0, "y": 720, "width": 90, "height": 60},
            },
            {
                "selector": "flt-semantics#102",
                "tag": "flt-semantics",
                "role": "button",
                "text": "我的",
                "class_tokens": [],
                "structural_navigation_kind": "flutter_bottom_navigation",
                "interactive": True,
                "rect": {"x": 270, "y": 720, "width": 90, "height": 60},
            },
        ],
    }

    class SnapshotPage:
        url = "https://example.test/"

        async def evaluate(self, script: str) -> dict[str, Any]:
            return raw_snapshot

    snapshot = await _collect_ui_snapshot(SnapshotPage())

    assert "logical_surface" not in snapshot
    assert "flutter_root_present" not in snapshot["document"]


async def test_ui_snapshot_shadow_collects_logical_surface_and_document_facts() -> None:
    raw_snapshot = {
        "schema_version": "ui-snapshot.v2",
        "url": "https://example.test/",
        "title": "Flutter 测试页",
        "document": {
            "time_origin_ms": 1000.0,
            "history_length": 2,
            "history_api_observed": True,
        },
        "elements": [
            {
                "selector": "flt-semantics#101",
                "tag": "flt-semantics",
                "role": "button",
                "text": "首页",
                "class_tokens": ["active"],
                "structural_navigation_kind": "flutter_bottom_navigation",
                "interactive": True,
                "rect": {"x": 0, "y": 720, "width": 90, "height": 60},
            },
            {
                "selector": "flt-semantics#102",
                "tag": "flt-semantics",
                "role": "button",
                "text": "我的",
                "class_tokens": [],
                "structural_navigation_kind": "flutter_bottom_navigation",
                "interactive": True,
                "rect": {"x": 270, "y": 720, "width": 90, "height": 60},
            },
        ],
    }

    class SnapshotPage:
        url = "https://example.test/"

        async def evaluate(self, script: str) -> dict[str, Any]:
            return raw_snapshot

    snapshot = await _collect_ui_snapshot(
        SnapshotPage(),
        logical_surface_enabled=True,
        logical_surface_shadow=True,
        surface_session_id="session-1",
    )

    surface = snapshot["logical_surface"]
    assert surface["protocol_version"] == "logical_surface.v1"
    assert surface["shadow"] is True
    assert surface["render_profile"]["kind"] == "flutter_web"
    assert surface["document_identity"].startswith("sha256:")
    assert snapshot["document"]["time_origin_ms"] == 1000.0


async def test_semantics_activation_failure_keeps_logical_capture_successful(
    monkeypatch,
) -> None:
    class SnapshotPage:
        url = "https://example.test/"

        async def evaluate(self, script: str) -> dict[str, Any]:
            return {
                "document": {"time_origin_ms": 1000.0},
                "elements": [],
            }

    async def fail_activation(page: Any) -> str:
        return "failed"

    monkeypatch.setattr(
        page_capture_module,
        "_enable_flutter_semantics_if_available",
        fail_activation,
    )

    snapshot = await _collect_ui_snapshot(
        SnapshotPage(),
        logical_surface_enabled=True,
        logical_surface_shadow=True,
        surface_session_id="session-1",
    )

    assert snapshot["schema_version"] == "ui-snapshot.v2"
    assert "flutter_semantics_unavailable" in snapshot["logical_surface"]["degraded_reasons"]


def test_action_trace_contains_compatible_logical_surface_transition() -> None:
    before_surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "测试站",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [
                {
                    "selector": "#home",
                    "tag": "button",
                    "role": "tab",
                    "text": "首页",
                    "class_tokens": ["active"],
                    "structural_navigation_kind": "bottom_navigation",
                    "interactive": True,
                    "rect": {"x": 0, "y": 720, "width": 90, "height": 60},
                },
                {
                    "selector": "#heading",
                    "tag": "h1",
                    "role": "heading",
                    "text": "首页推荐",
                    "class_tokens": ["page-heading"],
                    "rect": {"x": 0, "y": 0, "width": 200, "height": 50},
                },
            ],
        },
        page_url="https://example.test/",
        surface_session_id="session-1",
        document_token="document-1",
    )
    after_surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "测试站",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [
                {
                    "selector": "#profile",
                    "tag": "button",
                    "role": "tab",
                    "text": "我的",
                    "class_tokens": ["active"],
                    "structural_navigation_kind": "bottom_navigation",
                    "interactive": True,
                    "rect": {"x": 270, "y": 720, "width": 90, "height": 60},
                },
                {
                    "selector": "#heading",
                    "tag": "h1",
                    "role": "heading",
                    "text": "个人中心",
                    "class_tokens": ["page-heading"],
                    "rect": {"x": 0, "y": 0, "width": 200, "height": 50},
                },
            ],
        },
        page_url="https://example.test/",
        surface_session_id="session-1",
        document_token="document-1",
    )
    exact_bottom_navigation_selector = (
        "div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(2):visible"
    )
    action_trace = [
        {
            "action": "click",
            "selector": exact_bottom_navigation_selector,
            "navigation_effect_verified": True,
            "navigation_effect_reason": "same_origin_url_change",
        }
    ]

    page_capture_module._complete_action_trace(
        action_trace,
        before_url="https://example.test/",
        after_url="https://example.test/home-video",
        before_page_urls=["https://example.test/"],
        after_page_urls=["https://example.test/"],
        before_snapshot={"elements": [], "logical_surface": before_surface},
        after_snapshot={"elements": [], "logical_surface": after_surface},
    )

    transition = action_trace[-1]["state_diff"]["logical_surface_transition"]
    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["before_identity_fingerprint"] != transition["after_identity_fingerprint"]
    assert transition["reason_code"] == "trusted_bottom_navigation_url_change"
    assert transition["bottom_navigation_proof"]["navigation_family"] == ("footer_wrap")
    assert transition["bottom_navigation_proof"]["navigation_index"] == 2


def test_complete_action_trace_builds_trusted_page_tab_transition() -> None:
    """精确页签由未选中变为选中时应形成可验签的逻辑页面转换。"""
    selector = "#van-tabs-1-0"
    before_target = {
        "selector": selector,
        "tag": "div",
        "role": "tab",
        "text": "最新",
        "aria_selected": "false",
        "class_tokens": ["van-tab", "van-tab--line"],
        "interactive": True,
        "rect": {"x": 0, "y": 40, "width": 60, "height": 40},
    }
    after_target = {
        **before_target,
        # 部分 SPA 激活 Tab 后会省略重复文本；同 selector 和角色仍可绑定目标。
        "text": "",
        "aria_selected": "true",
        "class_tokens": ["van-tab", "van-tab--line", "van-tab--active"],
    }
    before_surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "测试站",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [
                before_target,
                {
                    "selector": "#heading",
                    "tag": "h1",
                    "role": "heading",
                    "text": "首页内容",
                    "class_tokens": ["page-heading"],
                    "rect": {"x": 0, "y": 100, "width": 200, "height": 50},
                },
                {
                    "selector": "#recommended-grid",
                    "tag": "section",
                    "class_tokens": ["recommended-grid"],
                    "rect": {"x": 0, "y": 160, "width": 390, "height": 600},
                },
            ],
        },
        page_url="https://example.test/home",
        surface_session_id="session-1",
        document_token="document-1",
    )
    after_surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "测试站",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [
                after_target,
                {
                    "selector": "#heading",
                    "tag": "h1",
                    "role": "heading",
                    "text": "首页内容",
                    "class_tokens": ["page-heading"],
                    "rect": {"x": 0, "y": 100, "width": 200, "height": 50},
                },
                {
                    "selector": "#latest-list",
                    "tag": "section",
                    "class_tokens": ["latest-list"],
                    "rect": {"x": 0, "y": 160, "width": 390, "height": 600},
                },
            ],
        },
        page_url="https://example.test/home",
        surface_session_id="session-1",
        document_token="document-1",
    )
    after_surface["identity_signals"]["main_structure"] = [
        {"category": "latest_list"}
    ]
    after_surface["identity_fingerprint"] = stable_h5_surface_hash(
        after_surface["identity_signals"]
    )
    after_surface["state_fingerprint"] = stable_h5_surface_hash(
        [after_surface["identity_fingerprint"], after_surface["state_signals"]]
    )
    action_trace = [{"action": "click", "selector": selector}]

    page_capture_module._complete_action_trace(
        action_trace,
        before_url="https://example.test/home",
        after_url="https://example.test/home",
        before_page_urls=["https://example.test/home"],
        after_page_urls=["https://example.test/home"],
        before_snapshot={"elements": [before_target], "logical_surface": before_surface},
        after_snapshot={"elements": [after_target], "logical_surface": after_surface},
        logical_surface_transition={
            "protocol_version": "logical_surface_transition.v1",
            "transition_kind": "surface_state_changed",
            "logical_transition_proof": "uncertain",
            "reason_code": "visual_transition_untrusted",
        },
    )

    transition = action_trace[-1]["state_diff"]["logical_surface_transition"]
    assert transition["transition_kind"] == "logical_page_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_page_tab_activation_change"
    assert transition["page_tab_proof"]["activation_before_sha256"] != (
        transition["page_tab_proof"]["activation_after_sha256"]
    )
    assert action_trace[-1]["target_before"]["aria_selected"] == "false"
    assert action_trace[-1]["target_after"]["aria_selected"] == "true"


def test_page_tab_transition_rejects_changed_target_label() -> None:
    proof = page_capture_module._page_tab_transition_proof(
        action_entry={"action": "click", "selector": "#van-tabs-1-0"},
        before_url="https://example.test/home",
        after_url="https://example.test/home",
        before_target={
            "selector": "#van-tabs-1-0",
            "role": "tab",
            "text": "推荐",
            "aria_selected": "false",
        },
        after_target={
            "selector": "#van-tabs-1-0",
            "role": "tab",
            "text": "最新",
            "aria_selected": "true",
        },
        before_surface={"identity_fingerprint": "sha256:" + "1" * 64},
        after_surface={"identity_fingerprint": "sha256:" + "2" * 64},
    )

    assert proof is None


def test_page_tab_transition_builds_proof_when_same_origin_route_changes() -> None:
    """SPA 页签变更查询参数时，精确目标激活与同源 URL 都须保留。"""
    before_target = {
        "selector": "#tabs-1",
        "role": "tab",
        "text": "分类",
        "aria_selected": "false",
    }
    proof = page_capture_module._page_tab_transition_proof(
        action_entry={"action": "click", "selector": "#tabs-1"},
        before_url="https://example.test/list?type=1",
        after_url="https://example.test/list?type=2",
        before_target=before_target,
        after_target={**before_target, "aria_selected": "true"},
        before_surface={
            "identity_fingerprint": "sha256:" + "1" * 64,
            "state_fingerprint": "sha256:" + "2" * 64,
        },
        after_surface={
            "identity_fingerprint": "sha256:" + "1" * 64,
            "state_fingerprint": "sha256:" + "3" * 64,
        },
    )

    assert proof is not None
    assert proof["effect"] == "route_activation_change"
    assert proof["after_location"] == "https://example.test/list?type=2"


def test_complete_action_trace_trusts_same_origin_route_changing_tab() -> None:
    """真实动作轨迹经完整证明链后应让新路由进入业务覆盖。"""
    before_target = {
        "selector": "#tabs-1",
        "tag": "div",
        "role": "tab",
        "text": "分类",
        "aria_selected": "false",
        "class_tokens": ["van-tab"],
        "rect": {"x": 90, "y": 0, "width": 60, "height": 40},
    }
    after_target = {
        **before_target,
        "aria_selected": "true",
        "class_tokens": ["van-tab", "van-tab--active"],
    }
    before_url = "https://example.test/list?type=1"
    after_url = "https://example.test/list?type=2"
    content_elements = [
        {
            "selector": "#heading",
            "tag": "h1",
            "role": "heading",
            "text": "分类视频",
            "rect": {"x": 0, "y": 70, "width": 200, "height": 40},
        },
        {
            "selector": "#list",
            "tag": "section",
            "class_tokens": ["video-list"],
            "rect": {"x": 0, "y": 120, "width": 390, "height": 600},
        },
    ]
    before_surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "列表",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [before_target, *content_elements],
        },
        page_url=before_url,
        surface_session_id="session-1",
        document_token="document-1",
    )
    after_surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "列表",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [after_target, *content_elements],
        },
        page_url=after_url,
        surface_session_id="session-1",
        document_token="document-1",
    )
    trace = [{"action": "click", "selector": "#tabs-1"}]

    page_capture_module._complete_action_trace(
        trace,
        before_url=before_url,
        after_url=after_url,
        before_page_urls=[before_url],
        after_page_urls=[after_url],
        before_snapshot={
            "elements": [before_target, *content_elements],
            "logical_surface": before_surface,
        },
        after_snapshot={
            "elements": [after_target, *content_elements],
            "logical_surface": after_surface,
        },
    )

    transition = trace[0]["state_diff"]["logical_surface_transition"]
    assert transition["reason_code"] == "trusted_page_tab_route_change", transition
    assert transition["logical_transition_proof"] == "trusted"


def test_page_tab_transition_builds_proof_for_same_page_filter_state() -> None:
    """同页筛选保持逻辑身份时，激活变化与状态变化仍应形成确定性证明。"""
    proof = page_capture_module._page_tab_transition_proof(
        action_entry={"action": "click", "selector": "#van-tabs-19-1"},
        before_url="https://example.test/home",
        after_url="https://example.test/home",
        before_target={
            "selector": "#van-tabs-19-1",
            "role": "tab",
            "text": "最新上架",
            "aria_selected": "false",
            "class_tokens": ["van-tab", "van-tab--line"],
        },
        after_target={
            "selector": "#van-tabs-19-1",
            "role": "tab",
            "text": "最新上架",
            "aria_selected": "true",
            "class_tokens": ["van-tab", "van-tab--line", "van-tab--active"],
        },
        before_surface={
            "identity_fingerprint": "sha256:" + "1" * 64,
            "state_fingerprint": "sha256:" + "2" * 64,
        },
        after_surface={
            "identity_fingerprint": "sha256:" + "1" * 64,
            "state_fingerprint": "sha256:" + "3" * 64,
        },
    )

    assert proof is not None
    assert proof["before_identity_fingerprint"] == proof["after_identity_fingerprint"]
    assert proof["activation_before_sha256"] != proof["activation_after_sha256"]


def test_complete_action_trace_proves_normalized_same_page_content_replacement() -> None:
    """同页列表被逻辑指纹降噪时，应以 Tab 激活和大块内容替换形成证明。"""
    selector = "#page-tab-latest"
    before_target = {
        "selector": selector,
        "role": "tab",
        "text": "最新上架",
        "aria_selected": "false",
        "class_tokens": ["tab"],
    }
    after_target = {
        **before_target,
        "aria_selected": "true",
        "class_tokens": ["tab", "is-active"],
    }
    before_panel = {
        "selector": "#recommended-panel",
        "text": "推荐内容一 推荐内容二 推荐内容三 推荐内容四 推荐内容五",
        "rect": {"x": 0, "y": 200, "width": 390, "height": 600},
    }
    after_panel = {
        "selector": "#latest-panel",
        "text": "最新内容一 最新内容二 最新内容三 最新内容四 最新内容五",
        "rect": {"x": 0, "y": 200, "width": 390, "height": 600},
    }
    surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "测试站",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [before_target, before_panel],
        },
        page_url="https://example.test/home",
        surface_session_id="session-1",
        document_token="document-1",
    )
    action_trace = [{"action": "click", "selector": selector}]

    page_capture_module._complete_action_trace(
        action_trace,
        before_url="https://example.test/home",
        after_url="https://example.test/home",
        before_page_urls=["https://example.test/home"],
        after_page_urls=["https://example.test/home"],
        before_snapshot={
            "elements": [before_target, before_panel],
            "logical_surface": surface,
        },
        after_snapshot={
            "elements": [after_target, after_panel],
            "logical_surface": deepcopy(surface),
        },
    )

    transition = action_trace[-1]["state_diff"]["logical_surface_transition"]
    assert transition["transition_kind"] == "surface_state_changed"
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["reason_code"] == "trusted_page_tab_state_change"
    assert transition["page_tab_proof"]["content_change_count"] == 2


def test_page_tab_activation_does_not_treat_marquee_motion_as_content_change() -> None:
    """跑马灯仅发生位移时，不得为同页 Tab 伪造内容切换证明。"""
    selector = "#page-tab-latest"
    before_target = {
        "selector": selector,
        "role": "tab",
        "text": "最新上架",
        "aria_selected": "false",
        "class_tokens": ["tab"],
    }
    after_target = {
        **before_target,
        "aria_selected": "true",
        "class_tokens": ["tab", "is-active"],
    }
    marquee_before = {
        "selector": "#announcement",
        "text": "这是一段持续滚动但业务内容没有变化的公告消息",
        "rect": {"x": -500, "y": 80, "width": 1200, "height": 30},
    }
    marquee_after = {
        **marquee_before,
        "rect": {"x": -200, "y": 80, "width": 1200, "height": 30},
    }
    surface = build_h5_logical_surface(
        ui_snapshot={
            "title": "测试站",
            "document": {"history_api_observed": True, "spa_root_present": True},
            "elements": [before_target, marquee_before],
        },
        page_url="https://example.test/home",
        surface_session_id="session-1",
        document_token="document-1",
    )
    action_trace = [{"action": "click", "selector": selector}]

    page_capture_module._complete_action_trace(
        action_trace,
        before_url="https://example.test/home",
        after_url="https://example.test/home",
        before_page_urls=["https://example.test/home"],
        after_page_urls=["https://example.test/home"],
        before_snapshot={
            "elements": [before_target, marquee_before],
            "logical_surface": surface,
        },
        after_snapshot={
            "elements": [after_target, marquee_after],
            "logical_surface": deepcopy(surface),
        },
    )

    transition = action_trace[-1]["state_diff"]["logical_surface_transition"]
    assert transition["transition_kind"] == "unchanged"
    assert "page_tab_proof" not in transition


async def test_ui_snapshot_keeps_fixed_navigation_after_long_content_list() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    list_actions = "".join(
        f'<button class="content-action">内容 {index}</button>' for index in range(320)
    )
    html = f"""
    <style>
      .content-action {{ display: block; width: 100%; height: 32px; }}
      .van-tabbar {{ position: fixed; inset: auto 0 0; height: 56px; display: flex; }}
      .van-tabbar-item {{ flex: 1; }}
    </style>
    <main class="van-tabs">
      <div class="van-tabs__content">{list_actions}</div>
    </main>
    <nav class="van-tabbar" role="navigation">
      <button class="van-tabbar-item">导航站</button>
      <button class="van-tabbar-item">视频</button>
      <button class="van-tabbar-item">广场</button>
      <button class="van-tabbar-item">我的</button>
    </nav>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert snapshot["element_limit_reached"] is True
    navigation_elements = [
        element
        for element in snapshot["elements"]
        if "van-tabbar" in element.get("class_tokens", [])
        or "van-tabbar-item" in element.get("class_tokens", [])
    ]
    assert {element.get("text") for element in navigation_elements} >= {
        "导航站",
        "视频",
        "广场",
        "我的",
    }


async def test_real_browser_snapshot_discovers_semantic_top_page_tabs() -> None:
    """顶部栏目必须从真实 DOM 进入 Snapshot 和自适应导航发现。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { margin: 0; width: 390px; height: 844px; }
      main { min-height: 1200px; padding-top: 48px; }
      nav { display: flex; width: 100%; height: 52px; }
      [role="tab"] { flex: 1; min-width: 60px; height: 44px; }
    </style>
    <main>
      <nav aria-label="内容分类">
        <button role="tab" aria-selected="true">推荐</button>
        <button role="tab" aria-selected="false">国产</button>
        <button role="tab" aria-selected="false">动漫</button>
        <button role="tab" aria-selected="false">漫画</button>
      </nav>
      <section>业务内容</section>
    </main>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    patterns = discover_h5_adaptive_navigation_patterns(snapshot)

    assert len(patterns) == 1
    assert patterns[0].pattern_kind == "page_tab_navigation"
    assert [item.label for item in patterns[0].items] == [
        "推荐",
        "国产",
        "动漫",
        "漫画",
    ]


async def test_ui_snapshot_collects_div_van_tabbar_items_as_navigation_targets() -> None:
    """Vant 底栏可点击父项是普通 div 时，也必须进入密封 Snapshot。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    items = "".join(
        f'<div class="van-tabbar-item" style="cursor:pointer">'
        f'<div class="van-tabbar-item__icon"><img alt="{label}"></div>'
        f'<div class="van-tabbar-item__text"><span>{label}</span></div>'
        "</div>"
        for label in ("导航", "视频", "禁漫", "AI", "圈子", "我的")
    )
    html = f"""
    <style>
      .van-tabbar {{ position: fixed; inset: auto 0 0; height: 56px; display: flex; }}
      .van-tabbar-item {{ flex: 1; height: 56px; }}
    </style>
    <nav class="van-tabbar">{items}</nav>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    navigation_items = [
        element
        for element in snapshot["elements"]
        if "van-tabbar-item" in element.get("class_tokens", [])
    ]
    assert [element.get("text") for element in navigation_items] == [
        "导航",
        "视频",
        "禁漫",
        "AI",
        "圈子",
        "我的",
    ]


async def test_ui_snapshot_records_intercepting_content_overlay_ancestry() -> None:
    """底栏被普通内容卡片截获时，Snapshot 必须保留截获节点的结构作用域。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .van-tabbar { position: fixed; inset: auto 0 0; z-index: 1; height: 50px; display: flex; }
      .van-tabbar-item { flex: 1; cursor: pointer; }
      .video-item { position: fixed; inset: auto 0 0; z-index: 2; height: 50px; }
      .aspect-ratio, .overlay { width: 100%; height: 100%; }
    </style>
    <nav class="van-tabbar">
      <div class="van-tabbar-item">首页</div>
      <div class="van-tabbar-item">视频</div>
      <div class="van-tabbar-item">社区</div>
      <div class="van-tabbar-item">我的</div>
    </nav>
    <div class="video-item"><div class="aspect-ratio"><div class="overlay"></div></div></div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    navigation_item = next(
        element
        for element in snapshot["elements"]
        if element.get("text") == "社区" and "van-tabbar-item" in element.get("class_tokens", [])
    )
    assert navigation_item["hit_test"]["center_hits_target"] is False
    assert navigation_item["hit_test"]["center_target_class_tokens"] == ["overlay"]
    assert {"aspect-ratio", "video-item"}.issubset(
        set(navigation_item["hit_test"]["center_target_ancestor_class_tokens"])
    )


async def test_ui_snapshot_uses_effective_ancestor_z_index() -> None:
    """子控件自身为 auto 时，必须继承定位祖先的阻断层级事实。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      #gate { position: fixed; inset: 0; z-index: 900; }
      #adult-confirm { position: relative; z-index: auto; }
    </style>
    <div id="gate" role="dialog">
      <button id="adult-confirm">我已年满18岁并进入</button>
    </div>
    """
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    candidate = next(item for item in snapshot["elements"] if item["selector"] == "#adult-confirm")
    assert candidate["style"]["z_index"] == "auto"
    assert candidate["effective_z_index"] == 900
    assert candidate["fixed_or_sticky_ancestor"] is True
    assert candidate["visible"] is True
    assert candidate["hit_test"]["center_hits_target"] is True


def test_ui_snapshot_retention_protects_van_tabbar_items() -> None:
    score = page_capture_module._ui_element_retention_score(
        {
            "tag": "div",
            "text": "视频",
            "class_tokens": ["van-tabbar-item"],
            "ancestor_class_tokens": ["van-tabbar"],
            "rect": {"x": 65, "y": 788, "width": 65, "height": 56},
            "interactive": True,
        }
    )

    assert score[2] == 1


@pytest.mark.parametrize(
    ("class_tokens", "ancestor_tokens", "alt"),
    [
        (["sign-in-entry"], ["user-card", "mine-page"], "签到"),
        (["vip-promo__button"], ["vip-promo", "mine-page"], "立即开通"),
        (["quick-card"], ["quick-card-row", "mine-page"], "会员中心"),
        (
            ["icon-button__img"],
            ["icon-button", "mine-header__actions", "mine-header"],
            "设置",
        ),
        (["desc"], ["item", "TwoGridLayout", "home-video-panel__scroll"], ""),
        (["modCover_name"], ["modVideoBox"], ""),
        (["text-overflow"], ["post-text", "post-box", "post"], ""),
        (["title"], ["content-images-wrap", "post-main-item"], ""),
        (["decial"], ["decial-wrap", "big-list"], ""),
    ],
)
def test_ui_snapshot_retention_protects_supplemental_navigation_structures(
    class_tokens: list[str],
    ancestor_tokens: list[str],
    alt: str,
) -> None:
    score = page_capture_module._ui_element_retention_score(
        {
            "tag": "img" if alt else "div",
            "alt": alt,
            "class_tokens": class_tokens,
            "ancestor_class_tokens": ancestor_tokens,
            "rect": {"x": 12, "y": 200, "width": 180, "height": 42},
            "interactive": False,
        }
    )

    assert score[2] == 1


async def test_ui_snapshot_keeps_five_site_profile_entries_after_dense_content() -> None:
    """站点五个人中心入口必须在密集 DOM 中优先进入可信快照。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    dense_content = "".join(
        f'<button class="content-action">内容 {index}</button>' for index in range(320)
    )
    html = f"""
    <style>
      .content-action {{ display: block; width: 100%; height: 28px; }}
      .profile-fixtures {{
        position: fixed; left: 8px; top: 8px; z-index: 20;
        display: flex; gap: 6px;
      }}
      .profile-fixtures img {{ display: block; width: 72px; height: 36px; }}
    </style>
    <main>{dense_content}</main>
    <section class="profile-fixtures mine-page">
      <div class="user-card"><img class="sign-in-entry" alt="签到"></div>
      <div class="vip-promo"><img class="vip-promo__button" alt="立即开通"></div>
      <div class="quick-card-row"><img class="quick-card" alt="会员中心"></div>
      <header class="mine-header"><div class="mine-header__actions">
        <button class="icon-button"><img class="icon-button__img" alt="设置"></button>
      </div></header>
    </section>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    retained_alts = {
        element.get("alt")
        for element in snapshot["elements"]
        if element.get("alt") in {"签到", "立即开通", "会员中心", "设置"}
    }
    assert snapshot["element_limit_reached"] is True
    assert retained_alts == {"签到", "立即开通", "会员中心", "设置"}


async def test_ui_snapshot_keeps_site_three_real_profile_entries_after_dense_content() -> None:
    """站3无 alt/role 的真实入口也必须优先进入可信快照。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    dense_content = "".join(
        f'<button class="content-action">内容 {index}</button>' for index in range(320)
    )
    html = f"""
    <style>
      .content-action {{ display: block; width: 100%; height: 28px; }}
      .site-three-profile {{
        position: fixed; left: 8px; top: 8px; z-index: 20;
        display: flex; gap: 6px;
      }}
      .site-three-profile img, .mine-vip-banner__btn {{
        display: block; width: 72px; height: 36px;
      }}
    </style>
    <main>{dense_content}</main>
    <section class="site-three-profile mine-content">
      <div class="sign-in"><img class="d-img" src="/assets/singi-MikFGLph.png"></div>
      <div class="vip-center mine-vip-banner">
        <div class="mine-vip-banner__btn">立即开通</div>
      </div>
      <div class="my-balance-wrap">
        <img src="/assets/mine-vip-icon-C6xv1WRC.png">
      </div>
    </section>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    retained = {
        element.get("selector")
        for element in snapshot["elements"]
        if "sign-in" in set(element.get("ancestor_class_tokens") or [])
        or "mine-vip-banner__btn" in set(element.get("class_tokens") or [])
        or str(element.get("src") or "").endswith("mine-vip-icon-C6xv1WRC.png")
    }
    assert snapshot["element_limit_reached"] is True
    assert len(retained) == 3
    retained_by_selector = {
        element["selector"]: element
        for element in snapshot["elements"]
        if element.get("selector") in retained
    }
    assert any(
        "sign-in" in set(element.get("parent_class_tokens") or [])
        for element in retained_by_selector.values()
    )
    assert any(
        {"vip-center", "mine-vip-banner"}.issubset(set(element.get("parent_class_tokens") or []))
        for element in retained_by_selector.values()
    )
    banner_button = next(
        element
        for element in retained_by_selector.values()
        if "mine-vip-banner__btn" in set(element.get("class_tokens") or [])
    )
    banner_parent_selector = str(banner_button.get("parent_selector") or "")
    banner_parents = [
        element
        for element in snapshot["elements"]
        if element.get("selector") == banner_parent_selector
    ]
    assert len(banner_parents) == 1
    assert {"vip-center", "mine-vip-banner"}.issubset(
        set(banner_parents[0].get("class_tokens") or [])
    )
    assert any(
        "my-balance-wrap" in set(element.get("parent_class_tokens") or [])
        for element in retained_by_selector.values()
    )


async def test_ui_snapshot_filters_site_three_decoys_before_priority_limit() -> None:
    """相同容器的错误资源和错误文案不得挤掉站3真实入口。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    dense_content = "".join(
        f'<button class="content-action">内容 {index}</button>' for index in range(320)
    )
    decoys = "".join(
        (
            '<div class="sign-in"><img class="d-img" '
            f'src="/assets/banner-{index}.png"></div>'
            '<div class="vip-center mine-vip-banner">'
            '<div class="mine-vip-banner__btn">查看详情</div></div>'
            '<div class="my-balance-wrap">'
            f'<img src="/assets/mine-gold-icon-{index}.png"></div>'
        )
        for index in range(25)
    )
    hidden_exact_decoys = "".join(
        (
            '<div class="sign-in" hidden><img class="d-img" '
            f'src="/assets/singi-hidden-{index}.png"></div>'
            '<div class="vip-center mine-vip-banner" style="display:none">'
            '<div class="mine-vip-banner__btn">立即开通</div></div>'
            '<div class="my-balance-wrap" hidden>'
            f'<img src="/assets/mine-vip-icon-hidden-{index}.png"></div>'
        )
        for index in range(25)
    )
    html = f"""
    <style>
      .content-action, img, .mine-vip-banner__btn {{
        display: block; width: 72px; height: 28px;
      }}
    </style>
    {decoys}{hidden_exact_decoys}
    <main>{dense_content}</main>
    <section class="mine-content">
      <div class="sign-in"><img id="real-signin" class="d-img"
        src="/assets/singi-MikFGLph.png"></div>
      <div class="vip-center mine-vip-banner" id="real-vip-banner">
        <div class="mine-vip-banner__btn" id="real-immediate-open">立即开通</div>
      </div>
      <div class="my-balance-wrap">
        <img id="real-member-center" src="/assets/mine-vip-icon-C6xv1WRC.png">
      </div>
    </section>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    selectors = {str(element.get("selector") or "") for element in snapshot["elements"]}
    assert snapshot["element_limit_reached"] is True
    assert {
        "#real-signin",
        "#real-vip-banner",
        "#real-immediate-open",
        "#real-member-center",
    }.issubset(selectors)


async def test_ui_snapshot_keeps_first_community_post_after_dense_content() -> None:
    """社区首帖结构不得被前置 300 个普通元素挤出可信快照。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    dense_content = "".join(
        f'<button class="content-action">内容 {index}</button>' for index in range(320)
    )
    html = f"""
    <style>
      .content-action {{ display: block; width: 100%; height: 32px; }}
      .item_container {{ width: 360px; min-height: 120px; }}
      .poi_title {{ width: 320px; height: 32px; }}
    </style>
    <main>{dense_content}</main>
    <section class="community_home">
      <div role="feed" class="van-list">
        <div class="item_container">
          <div class="poi_title">国庆狂欢限时开启</div>
        </div>
      </div>
    </section>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert snapshot["element_limit_reached"] is True
    post_titles = [
        element
        for element in snapshot["elements"]
        if "poi_title" in element.get("class_tokens", [])
    ]
    assert len(post_titles) == 1
    assert "item_container" in post_titles[0].get("ancestor_class_tokens", [])
    assert post_titles[0]["text"] == "国庆狂欢限时开启"


async def test_ui_snapshot_prioritizes_onscreen_data_item_over_offscreen_clones() -> None:
    """横向离屏克隆先出现在 DOM 时，仍须优先封存当前可见内容卡片。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    offscreen_cards = "".join(
        f'<div class="grid-item" style="position:absolute;left:-{400 + index * 190}px;top:120px">'
        f'<div class="data-item"><img alt="离屏卡片{index}"></div></div>'
        for index in range(24)
    )
    filler = "".join(f"<button>填充元素 {index}</button>" for index in range(360))
    html = f"""
    <style>
      .masonry-grid {{ position: relative; width: 390px; min-height: 1200px; }}
      .data-item {{ width: 177px; height: 220px; cursor: pointer; }}
      .data-item img {{ width: 177px; height: 177px; }}
      .visible-card {{ position: absolute; left: 12px; top: 480px; }}
    </style>
    <div class="masonry-grid">
      {offscreen_cards}
      <div class="grid-item visible-card">
        <div class="data-item"><img alt="当前可见卡片"></div>
      </div>
    </div>
    {filler}
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    visible_cards = [
        element
        for element in snapshot["elements"]
        if "data-item" in element.get("class_tokens", [])
        and element.get("rect", {}).get("x", -1) >= 0
    ]
    assert len(visible_cards) == 1
    assert visible_cards[0]["hit_test"]["center_hits_target"] is True


async def test_ui_snapshot_prioritizes_visible_layout3_cover_over_cached_masonry() -> None:
    """可见 layout3 推荐卡必须优先于同页大量离屏 masonry 缓存。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    offscreen_cards = "".join(
        '<div class="data-item" '
        f'style="position:absolute;left:-{400 + index * 190}px;top:120px">'
        f"缓存 {index}</div>"
        for index in range(60)
    )
    filler = "".join(
        f'<button class="filler" style="top:{index}px">填充元素 {index}</button>'
        for index in range(360)
    )
    html = f"""
    <style>
      .data-item {{ width: 177px; height: 220px; }}
      .filler {{ position: absolute; left: -1000px; }}
      .video-section .layout3 .img-box {{ width: 177px; height: 177px; cursor: pointer; }}
      .video-section .layout3 .img-box img {{ width: 100%; height: 100%; }}
    </style>
    <div class="masonry-grid">{offscreen_cards}</div>
    {filler}
    <div class="video-section"><div class="movie-list-wraper"><div class="wraper">
      <div class="layout3 layouts"><div class="content">
        <div><div class="img-box"><img alt="当前推荐"></div><div class="name">当前推荐</div></div>
      </div></div>
    </div></div></div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    covers = [
        element
        for element in snapshot["elements"]
        if "img-box" in element.get("class_tokens", [])
        and element.get("rect", {}).get("x", -1) >= 0
    ]
    assert len(covers) == 1
    assert {"layout3", "video-section"}.issubset(set(covers[0].get("ancestor_class_tokens", [])))
    assert covers[0]["hit_test"]["center_hits_target"] is True


async def test_ui_snapshot_prioritizes_modal_close_after_dense_popup_buttons() -> None:
    """广告弹窗中的大量 ``.btn`` 不得把文档尾部关闭控件挤出可信快照。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    ad_buttons = "".join(f'<div class="btn">广告 {index}</div>' for index in range(120))
    html = f"""
    <style>
      .app-list-pop {{ width: 390px; height: 844px; }}
      .btn {{ width: 60px; height: 20px; }}
      .close_btn {{
        position: fixed;
        left: 171px;
        bottom: 100px;
        width: 48px;
        height: 48px;
        cursor: pointer;
      }}
      .van-icon-close {{ display: block; width: 40px; height: 40px; }}
    </style>
    <div role="dialog" class="van-popup app-list-pop">
      {ad_buttons}
      <div class="close_btn"><i class="van-icon van-icon-close"></i></div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    close_elements = [
        element
        for element in snapshot["elements"]
        if {"close_btn", "van-icon-close"} & set(element.get("class_tokens", []))
    ]
    assert close_elements
    assert any(element.get("hit_test", {}).get("center_hits_target") for element in close_elements)


async def test_ui_snapshot_keeps_pages_foot_navigation_after_long_community_list() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    list_actions = "".join(
        f'<button class="content-action">社区内容 {index}</button>' for index in range(520)
    )
    html = f"""
    <style>
      .content-action {{ display: block; width: 100%; height: 32px; }}
      .pages-foot-fix {{ position: fixed; inset: auto 0 0; height: 56px; display: flex; }}
      .pages-foot-fix > li {{ flex: 1; list-style: none; }}
    </style>
    <main>{list_actions}</main>
    <div class="pages-foot-box">
      <ul class="pages-foot-fix justify-around">
        <li class="home"><span class="pages-foot-label">首页</span></li>
        <li class="active community"><span class="pages-foot-label">社区</span></li>
        <li class="anime"><span class="pages-foot-label">二次元</span></li>
        <li class="mine"><span class="pages-foot-label">我的</span></li>
      </ul>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert snapshot["element_limit_reached"] is True
    navigation_items = [
        element
        for element in snapshot["elements"]
        if element.get("tag") == "li"
        and "pages-foot-fix" in element.get("ancestor_class_tokens", [])
    ]
    assert {element.get("text") for element in navigation_items} == {
        "首页",
        "社区",
        "二次元",
        "我的",
    }


async def test_ui_snapshot_keeps_registered_footer_wrap_after_long_home_feed() -> None:
    """检测器允许的自定义 SPA 底栏必须在长页面动作前快照中保持可复核。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    list_actions = "".join(
        f'<button class="content-action">首页内容 {index}</button>' for index in range(520)
    )
    html = f"""
    <style>
      .content-action {{ display: block; width: 100%; height: 32px; }}
      .footer-wrap {{ position: fixed; inset: auto 0 0; height: 56px; }}
      .footer-wrap ul.type {{ display: flex; margin: 0; padding: 0; }}
      .footer-wrap li.footer-item {{ flex: 1; list-style: none; }}
    </style>
    <main>{list_actions}</main>
    <div class="footer-wrap"><div><ul class="type color">
      <li class="footer-item home item-0"><div class="ico-act"></div><div>二次元</div></li>
      <li class="footer-item video item-1"><div>视频</div></li>
      <li class="footer-item ysm item-2"><img alt=""></li>
      <li class="footer-item ai item-6"><div>AI女友</div></li>
      <li class="footer-item square item-3"><div>广场</div></li>
      <li class="footer-item mine item-5"><div>我的</div></li>
    </ul></div></div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert snapshot["element_limit_reached"] is True
    navigation_items = [
        element
        for element in snapshot["elements"]
        if element.get("tag") == "li"
        and "footer-item" in element.get("class_tokens", [])
        and "footer-wrap" in element.get("ancestor_class_tokens", [])
    ]
    assert [element.get("text") for element in navigation_items] == [
        "二次元",
        "视频",
        "",
        "AI女友",
        "广场",
        "我的",
    ]
    navigation_selectors = [str(element.get("selector") or "") for element in navigation_items]
    assert navigation_selectors[-2].endswith("li:nth-of-type(5)")
    assert navigation_selectors[-1].endswith("li:nth-of-type(6)")


async def test_ui_snapshot_records_ancestor_class_tokens_for_naked_card_img() -> None:
    """裸 img 卡片（类名在 .img-box 包装层）应带上祖先记号供列表详情解析。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .img-box { width: 178px; height: 118px; }
      .img-box img { width: 100%; height: 100%; cursor: pointer; }
    </style>
    <div class="wraper">
      <div class="layout5">
        <div class="content">
          <div>
            <div class="img-box"><img src="/covers/first-card.png" alt=""></div>
            <div class="van-ellipsis name">极品视频标题</div>
          </div>
        </div>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    card_imgs = [
        element
        for element in snapshot["elements"]
        if element.get("tag") == "img" and "img-box" in element.get("ancestor_class_tokens", [])
    ]
    assert card_imgs, "裸 img 卡片应携带 img-box 祖先记号"
    assert "layout5" in card_imgs[0]["ancestor_class_tokens"]
    title_divs = [
        element
        for element in snapshot["elements"]
        if element.get("tag") == "div" and "name" in element.get("class_tokens", [])
    ]
    assert title_divs, "div.name 标题应进入快照候选（详情路由绑在标题上）"


async def test_ui_snapshot_keeps_home_suspend_close_after_long_content_list() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    list_actions = "".join(
        f'<button class="content-action">内容 {index}</button>' for index in range(360)
    )
    html = f"""
    <style>
      .content-action {{ display: block; width: 100%; height: 32px; }}
      .home-suspend {{
        position: fixed;
        left: 12px;
        right: 12px;
        bottom: 80px;
        height: 72px;
        background: #222;
      }}
      .btn-close {{
        position: absolute;
        right: 8px;
        top: 8px;
        width: 24px;
        height: 24px;
      }}
    </style>
    <main>{list_actions}</main>
    <div class="home-suspend">
      <span>底部悬浮广告</span>
      <div class="btn-close"><i class="van-icon van-icon-close"></i></div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    class_token_sets = [set(element.get("class_tokens", [])) for element in snapshot["elements"]]
    assert any("home-suspend" in tokens for tokens in class_token_sets)
    assert any("btn-close" in tokens for tokens in class_token_sets)


async def test_ui_snapshot_collects_class_token_btn_as_interactive_target() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>.btn { width: 120px; height: 40px; cursor: pointer; }</style>
    <div class="sign-in-row">
      <span>签到</span>
      <div class="btn"><div>去签到</div></div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    targets = [element for element in snapshot["elements"] if element.get("text") == "去签到"]
    assert len(targets) == 1
    assert targets[0]["class_tokens"] == ["btn"]
    assert targets[0]["interactive"] is True


async def test_ui_snapshot_collects_layout7_video_card_image_box() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .video-section, .layout7, .content { width: 390px; min-height: 420px; }
      .current { width: 366px; height: 260px; }
      .img-box { width: 366px; height: 210px; cursor: pointer; }
      .img-box img { width: 100%; height: 100%; }
      .name { width: 366px; height: 32px; }
    </style>
    <div class="video-section">
      <div class="movie-list-wraper">
        <div class="layout7">
          <div class="content">
            <div class="current">
              <div class="img-box"><img alt=""></div>
              <div class="van-ellipsis name">首个视频标题</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    image_boxes = [
        element for element in snapshot["elements"] if "img-box" in element.get("class_tokens", [])
    ]
    assert len(image_boxes) == 1
    assert image_boxes[0]["hit_test"]["center_hits_target"] is True


async def test_ui_snapshot_collects_horizontal_video_card_as_interactive_target() -> None:
    """Vue 事件绑定的视频卡片没有 onclick，也应由受控结构进入可点击快照。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .horizontalVideoBox.videoBox { width: 180px; height: 170px; }
      .coverBox { width: 180px; height: 102px; }
      .videoTitleBox { width: 180px; height: 60px; }
    </style>
    <div class="moreMoviesBox">
      <div class="horizontalVideoBox videoBox">
        <div class="coverBox"></div>
        <div class="videoTitleBox">2311.8W 大长腿学生妹被学长抓住把柄的 少女</div>
      </div>
    </div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)
        snapshot = await _collect_ui_snapshot(page)
        await browser.close()

    cards = [
        element
        for element in snapshot["elements"]
        if {"horizontalVideoBox", "videoBox"}.issubset(set(element.get("class_tokens", [])))
    ]
    assert len(cards) == 1
    assert cards[0]["interactive"] is True
    assert "大长腿学生妹被学长抓住把柄的" in cards[0]["text"]
    assert cards[0]["hit_test"]["center_hits_target"] is True


async def test_ui_snapshot_collects_home_video_title_for_detail_navigation() -> None:
    """首页 videoTitle 标题必须进入快照，供标题点击进入视频详情。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>.videoTitle { display: block; width: 220px; height: 36px; }</style>
    <div class="videoTitle">无遮挡的视频标题</div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)
        snapshot = await _collect_ui_snapshot(page)
        await browser.close()

    titles = [
        element
        for element in snapshot["elements"]
        if "videoTitle" in element.get("class_tokens", [])
    ]
    assert len(titles) == 1
    assert titles[0]["text"] == "无遮挡的视频标题"
    assert titles[0]["hit_test"]["center_hits_target"] is True


async def test_ui_snapshot_card_selector_anchors_to_nearest_ancestor_id() -> None:
    """深层卡片路径必须锚定最近的唯一祖先，不能退化为全页多匹配路径。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <div id="van-tab-22">
      <div class="van-tab__panel">
        <div class="item">
          <div class="van-list">
            <div class="moreMoviesBox">
              <div class="horizontalVideoBox videoBox">目标视频卡片</div>
              <div class="horizontalVideoBox videoBox">第二视频卡片</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        await page.set_content(html)
        snapshot = await _collect_ui_snapshot(page)
        target = next(
            element
            for element in snapshot["elements"]
            if element.get("text") == "目标视频卡片"
            and "horizontalVideoBox" in element.get("class_tokens", [])
        )
        match_count = await page.locator(target["selector"]).count()
        await browser.close()

    assert target["selector"].startswith("#van-tab-22")
    assert match_count == 1


async def test_ui_snapshot_collects_ai_page_card_title_leaf() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .ai-page-card { width: 180px; height: 240px; }
      .cover { width: 180px; height: 190px; }
      .ai-page-card-title { width: 180px; height: 32px; text-align: center; }
    </style>
    <div class="ai-page-card-list">
      <div class="ai-page-card">
        <div class="cover"><img alt=""></div>
        <div class="ai-page-card-title">图片换脸</div>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    titles = [
        element
        for element in snapshot["elements"]
        if "ai-page-card-title" in element.get("class_tokens", [])
    ]
    assert len(titles) == 1
    assert titles[0]["text"] == "图片换脸"
    assert titles[0]["interactive"] is False
    assert titles[0]["hit_test"]["center_hits_target"] is True


async def test_ui_snapshot_collects_title_leaves_with_ancestor_context() -> None:
    """所有 .title 叶节点均可入快照（详情路由常绑在标题上），由祖先记号区分归属。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .dynamicData-box, .unrelated { width: 360px; min-height: 80px; }
      .title { display: block; width: 320px; height: 32px; }
    </style>
    <div class="dynamicData-box">
      <div class="dynamicData-content"><div class="title">目标帖子标题</div></div>
    </div>
    <div class="unrelated"><div class="title">无关普通标题</div></div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    title_elements = [
        element for element in snapshot["elements"] if "title" in element.get("class_tokens", [])
    ]
    assert {element["text"] for element in title_elements} == {"目标帖子标题", "无关普通标题"}
    post_title = next(element for element in title_elements if element["text"] == "目标帖子标题")
    assert "dynamicData-content" in post_title.get("ancestor_class_tokens", [])


def test_ui_structure_signature_changes_when_visible_geometry_moves() -> None:
    element = {
        "selector": "#open-vip",
        "tag": "button",
        "role": "button",
        "class_tokens": ["vip-entry"],
        "text": "立即开通",
        "rect": {"x": 10, "y": 20, "width": 120, "height": 44},
        "hit_bounds": {"x": 10, "y": 20, "width": 120, "height": 44},
        "visual_bounds": {"x": 10, "y": 20, "width": 120, "height": 44},
        "style": {"display": "block", "visibility": "visible", "opacity": "1"},
    }
    moved = {
        **element,
        "rect": {"x": 10, "y": 320, "width": 120, "height": 44},
        "hit_bounds": {"x": 10, "y": 320, "width": 120, "height": 44},
        "visual_bounds": {"x": 10, "y": 320, "width": 120, "height": 44},
    }

    assert _ui_structure_signature([element]) != _ui_structure_signature([moved])


def test_safe_ui_summary_is_bounded_and_keeps_element_index() -> None:
    summary = _safe_ui_summary(
        {
            "truncated": False,
            "document_state": {
                "fullscreen_active": True,
                "fullscreen_tag": "video" * 20,
            },
            "media": [
                {
                    "media_index": 0,
                    "tag": "video",
                    "class_tokens": ["fullscreen" * 20],
                    "bounds": {"x": 0, "y": 0, "width": 390, "height": 844},
                    "viewport_coverage": 1.0,
                    "state": {
                        "paused": False,
                        "ended": False,
                        "muted": True,
                        "controls": True,
                        "current_time": 12.4,
                        "duration": 120.0,
                        "ready_state": 4,
                    },
                }
            ],
            "elements": [
                {
                    "element_index": index,
                    "tag": "button",
                    "text": "按钮" * 100,
                    "hit_bounds": {"x": 0, "y": 0, "width": 48, "height": 48},
                    "visual_bounds": {"x": 8, "y": 8, "width": 32, "height": 32},
                    "style": {
                        "text_overflow": "ellipsis",
                        "white_space": "nowrap",
                        "webkit_line_clamp": "2",
                    },
                    "text_metrics": {
                        "scroll_width": 160,
                        "client_width": 80,
                        "scroll_height": 40,
                        "client_height": 20,
                        "horizontal_overflow": True,
                        "vertical_overflow": True,
                        "text_overflow_ellipsis": True,
                        "line_clamp": "2",
                    },
                }
                for index in range(120)
            ],
        }
    )

    assert summary["count"] == 120
    assert summary["truncated"] is True
    assert len(summary["elements"]) == 100
    assert summary["elements"][99]["element_index"] == 99
    assert len(summary["elements"][0]["text"]) == 100
    assert summary["document_state"] == {
        "fullscreen_active": True,
        "fullscreen_tag": ("video" * 20)[:32],
    }
    assert len(summary["media"][0]["class_tokens"][0]) == 100
    assert summary["media"][0]["state"] == {
        "paused": False,
        "ended": False,
        "muted": True,
        "controls": True,
        "current_time": 12.4,
        "duration": 120.0,
        "ready_state": 4,
    }
    assert summary["elements"][0]["text_metrics"]["scroll_width"] == 160
    assert summary["elements"][0]["truncation"] == {
        "horizontal_overflow": True,
        "vertical_overflow": True,
        "text_overflow": "ellipsis",
        "white_space": "nowrap",
        "line_clamp": "2",
        "clipped": True,
    }


def test_ui_snapshot_replaces_data_uri_and_enforces_serialized_limit() -> None:
    payload = b"a" * 400_000
    encoded = base64.b64encode(payload).decode("ascii")
    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/" + "x" * 4_000,
            "elements": [
                {
                    "selector": "#" + "s" * 1_000,
                    "tag": "img",
                    "class_tokens": ["c" * 200],
                    "src": (
                        f"data:image/png;base64,{encoded}"
                        if index == 0
                        else f"https://example.com/{index}.png"
                    ),
                    "text": "内容" * 4_000,
                    "rect": {"y": 5_000 + index, "height": 20},
                }
                for index in range(500)
            ],
        }
    )

    serialized = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
    assert len(serialized) <= 512 * 1024
    assert snapshot["url"] is not None and len(snapshot["url"]) == 2_048
    assert snapshot["truncated_reason"] == "serialized_size_limit"
    assert all(len(item["selector"]) <= 512 for item in snapshot["elements"])
    assert all(len(item["class_tokens"][0]) <= 100 for item in snapshot["elements"])
    summarized_resources = snapshot.get("resource_summaries", [])
    assert any(item.get("byte_length") == 400_000 for item in summarized_resources)


def test_page_coverage_stops_after_two_segments_without_new_content() -> None:
    browser = PlaywrightPageCaptureBrowser()
    before = {
        "url": "https://example.com/list",
        "container_selector": "#list",
        "scroll_top": 0,
    }
    after = {
        "url": "https://example.com/list",
        "container_selector": "#list",
        "scroll_top": 700,
        "scroll_height": 4_000,
        "client_height": 800,
        "at_bottom": False,
        "content_fingerprints": ["same"],
        "content_fingerprint": "segment",
    }

    first = browser._update_page_coverage(coverage_key="run-1", before=before, after=after)
    second = browser._update_page_coverage(coverage_key="run-1", before=after, after=after)
    third = browser._update_page_coverage(coverage_key="run-1", before=after, after=after)

    assert first["termination_reason"] is None
    assert second["termination_reason"] is None
    assert third["termination_reason"] == "two_segments_without_new_content"
    assert third["collection_complete"] is True
    assert third["coverage_complete"] is False
    assert third["coverage_status"] == "bounded_incomplete"
    assert third["coverage_gap_reason"] == "two_segments_without_new_content"


def test_scroll_segment_continuity_detects_reordered_overlap() -> None:
    result = _assess_scroll_segment_continuity(
        before={"scroll_top": 0, "client_height": 800},
        after={"scroll_top": 680, "client_height": 800},
        previous_visible_fingerprints=["a", "b", "c"],
        current_visible_fingerprints=["c", "b", "d"],
    )

    assert result == {
        "status": "content_order_changed",
        "scroll_progressed": True,
        "overlap_item_count": 2,
        "starts_new_segment": True,
    }


def test_scroll_segment_continuity_rejects_viewport_gap() -> None:
    result = _assess_scroll_segment_continuity(
        before={"scroll_top": 0, "client_height": 800},
        after={"scroll_top": 900, "client_height": 800},
        previous_visible_fingerprints=["a", "b"],
        current_visible_fingerprints=["b", "c"],
    )

    assert result["status"] == "scroll_gap_detected"
    assert result["starts_new_segment"] is True


def test_page_coverage_only_completes_after_bottom_with_continuous_overlap() -> None:
    browser = PlaywrightPageCaptureBrowser()
    before = {
        "url": "https://example.com/list",
        "container_selector": "#list",
        "scroll_top": 0,
        "scroll_height": 1_400,
        "client_height": 800,
        "at_bottom": False,
        "content_fingerprints": ["a", "b"],
        "content_fingerprint": "before",
    }
    after = {
        **before,
        "scroll_top": 600,
        "at_bottom": True,
        "content_fingerprints": ["b", "c"],
        "content_fingerprint": "after",
    }

    coverage = browser._update_page_coverage(
        coverage_key="run-bottom",
        before=before,
        after=after,
    )

    assert coverage["termination_reason"] == "bottom_reached"
    assert coverage["collection_complete"] is True
    assert coverage["bottom_reached"] is True
    assert coverage["coverage_complete"] is True
    assert coverage["coverage_status"] == "complete"
    assert coverage["continuity_gap_count"] == 0
    assert coverage["coverage_gap_reason"] is None


def test_single_segment_budget_stop_is_not_full_page_coverage() -> None:
    coverage = _single_segment_coverage(
        state={
            "url": "https://example.com/list",
            "container_selector": "#list",
            "scroll_top": 0,
            "scroll_height": 4_000,
            "client_height": 800,
            "at_bottom": False,
            "content_fingerprints": ["a", "b"],
            "content_fingerprint": "baseline",
        },
        termination_reason="segment_budget_exhausted",
    )

    assert coverage["schema_version"] == "page-coverage.v2"
    assert coverage["collection_complete"] is True
    assert coverage["coverage_complete"] is False
    assert coverage["coverage_gap_reason"] == "segment_budget_exhausted"


def test_single_segment_identity_drift_blocks_bottom_coverage() -> None:
    coverage = _single_segment_coverage(
        state={
            "url": "https://example.com/list",
            "container_selector": "#list",
            "scroll_top": 0,
            "scroll_height": 800,
            "client_height": 800,
            "at_bottom": True,
            "content_fingerprints": ["a"],
            "content_fingerprint": "baseline",
            "capture_consistency": {
                "schema_version": "capture-consistency.v1",
                "status": "identity_drift",
                "changed_signals": ["top_layer_sha256"],
                "unavailable_signals": ["account_identity"],
            },
        },
        termination_reason="bottom_reached",
    )

    assert coverage["bottom_reached"] is True
    assert coverage["coverage_complete"] is False
    assert coverage["coverage_gap_reason"] == "capture_identity_drift"
    assert coverage["segments"][0]["capture_consistency"]["changed_signals"] == ["top_layer_sha256"]


def test_scroll_snapshot_declares_compaction_derivation() -> None:
    snapshot = _compact_scroll_ui_snapshot(
        {"elements": [{"selector": f"#item-{index}", "visible": True} for index in range(90)]}
    )

    assert len(snapshot["elements"]) == 80
    assert snapshot["truncated"] is True
    assert snapshot["truncated_reason"] == "scroll_segment_semantic_compaction"
    assert snapshot["derivation"] == {
        "kind": "visible_semantic_projection",
        "source_element_count": 90,
        "retained_element_count": 80,
        "element_limit": 80,
    }


async def test_scroll_coverage_finishes_in_one_capture_with_linked_frames(
    monkeypatch,
) -> None:
    states = [
        {
            "url": "https://example.com/list",
            "container_selector": "#list",
            "scroll_top": index * 680,
            "scroll_height": 4_000,
            "client_height": 800,
            "at_bottom": False,
            "content_fingerprints": ["same"],
            "content_fingerprint": f"segment-{index}",
        }
        for index in range(3)
    ]
    state_index = 0

    async def collect_state(page):
        nonlocal state_index
        value = states[min(state_index, len(states) - 1)]
        state_index += 1
        return value

    async def run_trigger(**kwargs):
        kwargs["action_trace"].append({"action": "scroll"})

    async def wait_for_settle(**kwargs):
        return None

    async def collect_snapshot(page, **kwargs):
        del kwargs
        return {
            "schema_version": "ui-snapshot.v2",
            "url": "https://example.com/list",
            "viewport": {"width": 390, "height": 800},
            "structure_signature": "signature",
            "elements": [{"selector": "#item", "visible": True}],
        }

    monkeypatch.setattr(page_capture_module, "_collect_scroll_state", collect_state)
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", run_trigger)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", wait_for_settle)
    monkeypatch.setattr(page_capture_module, "_collect_ui_snapshot", collect_snapshot)

    class Page:
        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            return b"viewport"

    browser = PlaywrightPageCaptureBrowser()
    action_trace: list[dict[str, Any]] = []
    artifacts = await browser._collect_scroll_coverage_artifacts(
        page=Page(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="scroll",
            session_id="run-scroll",
        ),
        action_trace=action_trace,
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
        baseline_ui_snapshot=await collect_snapshot(None),
    )

    coverage = next(item.content for item in artifacts if item.artifact_type == "page_coverage")
    screenshots = next(item.content for item in artifacts if item.artifact_type == "screenshot")
    ui_snapshots = next(item.content for item in artifacts if item.artifact_type == "ui_snapshot")
    assert coverage["termination_reason"] == "two_segments_without_new_content"
    assert coverage["collection_complete"] is True
    assert coverage["coverage_complete"] is False
    assert coverage["coverage_status"] == "bounded_incomplete"
    assert action_trace[-1]["state_diff"]["scroll_effect"]["observed"] is True
    assert action_trace[-1]["state_diff"]["scroll_effect"]["scroll_top_before"] == 0
    assert action_trace[-1]["state_diff"]["scroll_effect"]["scroll_top_after"] == 1360
    assert len(coverage["segments"]) == 3
    assert [item["screenshot_frame_index"] for item in coverage["segments"]] == [
        0,
        1,
        2,
    ]
    assert coverage["terminal_capture_role"] == "scroll_last_observed"
    assert coverage["terminal_ui_snapshot_frame_index"] == 2
    assert coverage["terminal_screenshot_frame_index"] == 2
    assert coverage["terminal_scroll_state"] == {
        "scroll_top_after": 1360,
        "scroll_height": 4000,
        "viewport_height": 800,
    }
    assert len(screenshots["frames"]) == 3
    assert screenshots["frames"][-1]["capture_role"] == "scroll_last_observed"
    assert screenshots["frames"][-1]["collection_terminal"] is True
    assert screenshots["frames"][-1]["terminal"] is False
    assert ui_snapshots["frames"][-1]["capture_role"] == "scroll_last_observed"
    assert ui_snapshots["frames"][-1]["collection_terminal"] is True
    assert ui_snapshots["frames"][-1]["terminal"] is False
    assert len(action_trace) == 2


async def test_h5_detail_scroll_persists_two_independent_screenshot_frames(
    monkeypatch,
) -> None:
    states = [
        {
            "url": "https://example.com/detail",
            "container_selector": "#contentId",
            "scroll_top": index * 680,
            "scroll_height": 4_000,
            "client_height": 800,
            "at_bottom": False,
            "content_fingerprints": [f"item-{index}"],
            "content_fingerprint": f"segment-{index}",
        }
        for index in range(2)
    ]
    state_index = 0

    async def collect_state(page):
        nonlocal state_index
        value = states[min(state_index, len(states) - 1)]
        state_index += 1
        return value

    async def run_trigger(**kwargs):
        kwargs["action_trace"].append({"action": "scroll"})

    async def wait_for_settle(**kwargs):
        return None

    async def collect_snapshot(page, **kwargs):
        del kwargs
        return {
            "schema_version": "ui-snapshot.v2",
            "url": "https://example.com/detail",
            "viewport": {"width": 390, "height": 800},
            "structure_signature": "signature",
            "elements": [{"selector": "#contentId", "visible": True}],
        }

    monkeypatch.setattr(page_capture_module, "_collect_scroll_state", collect_state)
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", run_trigger)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", wait_for_settle)
    monkeypatch.setattr(page_capture_module, "_collect_ui_snapshot", collect_snapshot)

    class Page:
        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            return b"viewport"

    artifacts = await PlaywrightPageCaptureBrowser()._collect_scroll_coverage_artifacts(
        page=Page(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="scroll",
            session_id="h5-detail-scroll",
            scroll_max_segments=2,
            split_scroll_screenshots=True,
        ),
        action_trace=[],
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
        baseline_ui_snapshot=await collect_snapshot(None),
    )

    screenshots = [item for item in artifacts if item.artifact_type == "screenshot"]
    coverage = next(item.content for item in artifacts if item.artifact_type == "page_coverage")
    assert len(screenshots) == 2
    assert all(item.content["encoding"] == "base64" for item in screenshots)
    assert coverage["termination_reason"] == "segment_budget_exhausted"
    assert len(coverage["segments"]) == 2


async def test_h5_detail_scroll_does_not_publish_duplicate_frame_without_motion(
    monkeypatch,
) -> None:
    state = {
        "url": "https://example.com/detail",
        "container_selector": "#contentId",
        "scroll_top": 0,
        "scroll_height": 4_000,
        "client_height": 800,
        "at_bottom": False,
        "content_fingerprints": ["item-0"],
        "content_fingerprint": "segment-0",
    }

    async def collect_state(page):
        del page
        return dict(state)

    async def run_trigger(**kwargs):
        kwargs["action_trace"].append({"action": "scroll"})

    async def wait_for_settle(**kwargs):
        return None

    async def collect_snapshot(page, **kwargs):
        del page, kwargs
        return {
            "schema_version": "ui-snapshot.v2",
            "url": "https://example.com/detail",
            "viewport": {"width": 390, "height": 800},
            "elements": [{"selector": "#contentId", "visible": True}],
        }

    monkeypatch.setattr(page_capture_module, "_collect_scroll_state", collect_state)
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", run_trigger)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", wait_for_settle)
    monkeypatch.setattr(page_capture_module, "_collect_ui_snapshot", collect_snapshot)

    class Page:
        screenshot_count = 0

        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            self.screenshot_count += 1
            return b"same-viewport"

    page = Page()
    artifacts = await PlaywrightPageCaptureBrowser()._collect_scroll_coverage_artifacts(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="scroll",
            session_id="h5-detail-no-effect",
            scroll_max_segments=2,
            split_scroll_screenshots=True,
        ),
        action_trace=[],
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
        baseline_ui_snapshot=await collect_snapshot(None),
    )

    screenshots = [item for item in artifacts if item.artifact_type == "screenshot"]
    coverage = next(item.content for item in artifacts if item.artifact_type == "page_coverage")
    assert page.screenshot_count == 1
    assert len(screenshots) == 1
    assert coverage["termination_reason"] == "scroll_no_effect"
    assert coverage["terminal_screenshot_frame_index"] is None


async def test_navigation_outcome_records_and_closes_new_window() -> None:
    class MainPage:
        url = "https://product.example/home"

    class NewPage:
        url = "https://advertiser.example/landing"

        def __init__(self) -> None:
            self.waited = False
            self.closed = False

        async def wait_for_load_state(self, state: str, *, timeout: int) -> None:
            assert state == "domcontentloaded"
            assert timeout == 1000
            self.waited = True

        async def close(self) -> None:
            self.closed = True

    main_page = MainPage()
    new_page = NewPage()
    context = SimpleNamespace(pages=[main_page, new_page])
    action_trace = [{"action": "click", "selector": "a.external"}]

    await _record_navigation_outcome(
        page=main_page,
        context=context,
        before_url=main_page.url,
        pages_before=[main_page],
        observed_new_pages=[new_page],
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert new_page.waited is True
    assert new_page.closed is True
    assert action_trace[0]["navigation"] == {
        "before_url": "https://product.example/home",
        "after_url": "https://product.example/home",
        "current_url_changed": False,
        "new_page_count": 1,
        "new_page_urls": ["https://advertiser.example/landing"],
        "new_pages_truncated": False,
        "closed_after_observation_count": 1,
    }


async def test_navigation_outcome_captures_same_tab_external_landing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MainPage:
        url = "https://advertiser.example/landing?token=secret"

    captured: list[tuple[Any, int]] = []

    async def capture_landing(*, page: Any, index: int) -> dict[str, Any]:
        captured.append((page, index))
        return {"url": page.url, "frame": {"data": "landing"}}

    monkeypatch.setattr(
        page_capture_module,
        "_capture_ad_landing_observation",
        capture_landing,
    )
    page = MainPage()
    action_trace = [{"action": "click", "selector": "#external"}]

    observation = await _record_navigation_outcome(
        page=page,
        context=SimpleNamespace(pages=[page]),
        before_url="https://product.example/home",
        pages_before=[page],
        observed_new_pages=[],
        action_trace=action_trace,
        timeout_ms=30_000,
        capture_ad_landing_evidence=True,
    )

    assert observation == {"url": page.url, "frame": {"data": "landing"}}
    assert captured == [(page, 1)]


async def test_external_new_page_transition_is_not_downgraded_to_same_page() -> None:
    page = SimpleNamespace(url="https://product.example/home")
    action_trace = [
        {
            "action": "click",
            "navigation": {
                "before_url": page.url,
                "after_url": page.url,
                "new_page_urls": ["https://advertiser.example/landing"],
            },
        }
    ]

    outcome = await page_capture_module._block_undeclared_click_navigation(
        page=page,
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="click",
            selector="#dynamic-external",
        ),
        before_url=page.url,
        timeout_ms=30_000,
        action_trace=action_trace,
    )

    assert outcome == "external_landing_observed"


async def test_external_same_tab_return_failure_is_blocked_and_audited() -> None:
    class MainPage:
        url = "https://advertiser.example/landing"

        async def go_back(self, **_: Any) -> None:
            return None

        async def goto(self, *_: Any, **__: Any) -> None:
            return None

    page = MainPage()
    action_trace = [
        {
            "action": "click",
            "navigation": {
                "before_url": "https://product.example/home",
                "after_url": page.url,
            },
        }
    ]

    with pytest.raises(BlockedPageCaptureActionError) as exc_info:
        await page_capture_module._block_undeclared_click_navigation(
            page=page,
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector="#external",
            ),
            before_url="https://product.example/home",
            timeout_ms=30_000,
            ad_landing_window=True,
            action_trace=action_trace,
        )

    assert exc_info.value.error_code == "EXTERNAL_NAVIGATION_RETURN_FAILED"
    assert action_trace[0]["navigation"]["return"] == {
        "attempted": True,
        "restored": False,
        "final_url": "https://advertiser.example/landing",
    }


async def test_external_same_tab_return_is_verified_and_audited() -> None:
    class MainPage:
        def __init__(self) -> None:
            self.url = "https://advertiser.example/landing"
            self.goto_calls = 0

        async def go_back(self, **_: Any) -> None:
            self.url = "https://product.example/home#restored"

        async def goto(self, *_: Any, **__: Any) -> None:
            self.goto_calls += 1

    page = MainPage()
    action_trace = [{"action": "click", "navigation": {}}]

    outcome = await page_capture_module._block_undeclared_click_navigation(
        page=page,
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="click",
            selector="#external",
        ),
        before_url="https://product.example/home",
        timeout_ms=30_000,
        ad_landing_window=True,
        action_trace=action_trace,
    )

    assert outcome == "external_landing_observed"
    assert page.goto_calls == 0
    assert action_trace[0]["navigation"]["return"] == {
        "attempted": True,
        "restored": True,
        "final_url": "https://product.example/home#restored",
    }


async def test_navigation_outcome_blocks_when_new_page_cannot_be_closed() -> None:
    class MainPage:
        url = "https://product.example/home"

    class NewPage:
        url = "https://advertiser.example/landing"

        async def close(self) -> None:
            raise RuntimeError("close failed")

    main_page = MainPage()
    new_page = NewPage()
    action_trace = [{"action": "click", "selector": "#external"}]

    with pytest.raises(BlockedPageCaptureActionError) as exc_info:
        await _record_navigation_outcome(
            page=main_page,
            context=SimpleNamespace(pages=[main_page, new_page]),
            before_url=main_page.url,
            pages_before=[main_page],
            observed_new_pages=[new_page],
            action_trace=action_trace,
            timeout_ms=30_000,
        )

    assert exc_info.value.error_code == "EXTERNAL_NAVIGATION_ISOLATION_FAILED"
    assert action_trace[0]["navigation"]["new_page_close_failed_count"] == 1


async def test_visual_profile_payment_follows_single_same_origin_vip_new_page() -> None:
    class MainPage:
        def __init__(self) -> None:
            self.url = "https://example.test/profile"
            self.goto_calls: list[str] = []

        async def goto(self, url: str, **_: Any) -> None:
            self.goto_calls.append(url)
            self.url = url

    class NewPage:
        url = "https://example.test/vip"

        def __init__(self) -> None:
            self.closed = False

        async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    main_page = MainPage()
    new_page = NewPage()
    action_trace = [
        {
            "action": "click",
            "selector": None,
            "resolved_by": "validated_visual_coordinate",
        }
    ]

    await _record_navigation_outcome(
        page=main_page,
        context=SimpleNamespace(pages=[main_page, new_page]),
        before_url=main_page.url,
        pages_before=[main_page],
        observed_new_pages=[new_page],
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert main_page.goto_calls == ["https://example.test/vip"]
    assert main_page.url == "https://example.test/vip"
    assert new_page.closed is True
    assert action_trace[0]["navigation"]["followed_visual_vip_new_page"] is True


@pytest.mark.parametrize(
    "new_page_urls",
    [
        ["https://example.test/vip", "https://example.test/vip"],
        ["https://outside.test/vip"],
        ["https://example.test/vip-extra"],
    ],
)
async def test_visual_profile_payment_does_not_follow_untrusted_new_pages(
    new_page_urls: list[str],
) -> None:
    class MainPage:
        def __init__(self) -> None:
            self.url = "https://example.test/profile"
            self.goto_calls: list[str] = []

        async def goto(self, url: str, **_: Any) -> None:
            self.goto_calls.append(url)
            self.url = url

    class NewPage:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False

        async def wait_for_load_state(self, *_: Any, **__: Any) -> None:
            return None

        async def close(self) -> None:
            self.closed = True

    main_page = MainPage()
    new_pages = [NewPage(url) for url in new_page_urls]
    action_trace = [
        {
            "action": "click",
            "selector": None,
            "resolved_by": "validated_visual_coordinate",
        }
    ]

    await _record_navigation_outcome(
        page=main_page,
        context=SimpleNamespace(pages=[main_page, *new_pages]),
        before_url=main_page.url,
        pages_before=[main_page],
        observed_new_pages=new_pages,
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert main_page.goto_calls == []
    assert all(page.closed for page in new_pages)
    assert action_trace[0]["navigation"]["followed_visual_vip_new_page"] is False


class FakePageCaptureBrowser:
    def __init__(self) -> None:
        self.requests: list[PageCaptureRequest] = []

    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        self.requests.append(request)
        return PageCaptureResult(
            execution_status="success",
            mode=request.mode.value,
            reachable=True,
            final_url=request.url,
            title="示例页面",
            status_code=200,
            artifacts=[
                PageCaptureArtifact(
                    artifact_type="screenshot",
                    artifact_id=None,
                    content={"encoding": "base64", "data": "abc"},
                )
            ],
            console_logs=[],
            network_requests=[],
            action_trace=[],
            post_action_snapshot=None,
        )


class DelayedPageCaptureBrowser(FakePageCaptureBrowser):
    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        await asyncio.sleep(0.05)
        return await super().capture(request)


class UnprovenDismissalPageCaptureBrowser:
    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        return PageCaptureResult(
            execution_status="success",
            mode=request.mode.value,
            reachable=True,
            final_url=request.url,
            title="开屏广告",
            status_code=200,
            artifacts=[],
            console_logs=[],
            network_requests=[],
            action_trace=[
                {
                    "action": "click",
                    "selector": request.selector,
                    "result": "popup_dismissal_target_not_proven",
                }
            ],
            post_action_snapshot=None,
        )


class LargePageCaptureBrowser:
    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        return PageCaptureResult(
            execution_status="success",
            mode=request.mode.value,
            reachable=True,
            final_url=request.url,
            title="大页面",
            status_code=200,
            artifacts=[
                PageCaptureArtifact("dom", None, "<html>" + "x" * 10000 + "</html>"),
                PageCaptureArtifact("visible_text", None, "可见文本" * 2000),
                PageCaptureArtifact(
                    "screenshot",
                    None,
                    {"encoding": "base64", "data": "SECRET_SCREENSHOT_BASE64" * 1000},
                ),
            ],
            console_logs=[{"type": "error", "text": "console error"}],
            network_requests=[{"event": "response", "status": 200, "url": request.url}],
            action_trace=[],
            post_action_snapshot=None,
        )


class FakePageCapturePersistenceService:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def persist(
        self, *, task_id: str, agent_run_id: str, result: Any, **kwargs: Any
    ) -> PersistedPageCapture:
        self.calls.append(
            {
                "task_id": task_id,
                "agent_run_id": agent_run_id,
                "result": result,
                **kwargs,
            }
        )
        return PersistedPageCapture(
            page_capture_id="capture-1",
            page_seq=1,
            artifacts=[
                PersistedEvidenceArtifact(
                    artifact_id="artifact-dom",
                    artifact_type="dom",
                    storage_path="task/capture/artifact-dom.html",
                    content_type="text/html; charset=utf-8",
                    checksum="sha256:dom",
                    size_bytes=10013,
                    metadata={},
                ),
                PersistedEvidenceArtifact(
                    artifact_id="artifact-visible-text",
                    artifact_type="visible_text",
                    storage_path="task/capture/artifact-visible-text.txt",
                    content_type="text/plain; charset=utf-8",
                    checksum="sha256:visible-text",
                    size_bytes=24000,
                    metadata={},
                ),
                PersistedEvidenceArtifact(
                    artifact_id="artifact-screenshot",
                    artifact_type="screenshot",
                    storage_path="task/capture/artifact-screenshot.png",
                    content_type="image/png",
                    checksum="sha256:screenshot",
                    size_bytes=1024,
                    metadata={},
                ),
            ],
        )


class ConcurrentPageCapturePersistenceService:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.sequence = 0

    async def persist(
        self, *, task_id: str, agent_run_id: str, result: Any, **kwargs: Any
    ) -> PersistedPageCapture:
        del task_id, agent_run_id, result, kwargs
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.sequence += 1
        try:
            await asyncio.sleep(0.03)
            return PersistedPageCapture(
                page_capture_id=f"capture-{self.sequence}",
                page_seq=self.sequence,
                artifacts=[],
            )
        finally:
            self.active -= 1


class HangingPageCaptureBrowser:
    async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
        await asyncio.Event().wait()
        raise AssertionError(f"页面采集悬挂不应自然返回：{request.mode}")


class HangingPageCapturePersistenceService:
    async def persist(
        self, *, task_id: str, agent_run_id: str, result: Any
    ) -> PersistedPageCapture:
        await asyncio.Event().wait()
        raise AssertionError(f"证据持久化悬挂不应自然返回：{task_id=}, {agent_run_id=}, {result=}")


class FailingPageCapturePersistenceService:
    async def persist(
        self, *, task_id: str, agent_run_id: str, result: Any
    ) -> PersistedPageCapture:
        raise OSError(f"证据存储不可用：{task_id=}, {agent_run_id=}, {result.mode=}")


async def test_capture_page_returns_artifact_id_placeholder_from_browser_result() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(mode=PageCaptureMode.GOTO, url="https://example.com")

    result = await capture_page(request=request, browser=browser)

    assert browser.requests == [request]
    assert result.execution_status == "success"
    assert result.artifacts[0].artifact_type == "screenshot"
    assert result.artifacts[0].artifact_id is None


async def test_playwright_browser_reuses_page_within_agent_run() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    # 无 Chromium 二进制时跳过，避免环境缺失变成假失败
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 仅探测可执行文件是否可用
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    browser = PlaywrightPageCaptureBrowser()
    session_id = "run-browser-session"
    target_url = "data:text/html,<title>Alioth Session</title><main>ready</main>"

    try:
        first = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=target_url,
                session_id=session_id,
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=False,
            )
        )
        second = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.OBSERVE,
                session_id=session_id,
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=False,
            )
        )
    finally:
        await browser.close_session(session_id)

    assert first.title == "Alioth Session"
    assert second.title == "Alioth Session"
    assert second.final_url == target_url


async def test_capture_boundary_identity_does_not_persist_plaintext_url() -> None:
    class Page:
        url = "https://example.com/private?token=secret"

        async def evaluate(self, script: str) -> dict[str, Any]:
            del script
            return {
                "time_origin_ms": 1234.5,
                "visibility_state": "visible",
                "top_layer": None,
            }

    page = Page()
    context = SimpleNamespace(pages=[page])

    identity = await _read_capture_boundary_identity(page=page, context=context)

    serialized = json.dumps(identity, ensure_ascii=False)
    assert "https://example.com" not in serialized
    assert "secret" not in serialized
    assert identity["page_url_sha256"].startswith("sha256:")
    assert identity["document_time_origin_ms"] == 1234.5
    assert identity["active_page_index"] == 0


def test_temporal_capture_keeps_identity_drift_as_time_series() -> None:
    before = {
        "page_url_sha256": "sha256:before",
        "context_page_urls_sha256": ["sha256:before"],
        "active_page_index": 0,
        "document_time_origin_ms": 1,
        "visibility_state": "visible",
        "top_layer_sha256": None,
        "document_observation_available": True,
        "account_identity_available": False,
    }
    after = {**before, "page_url_sha256": "sha256:after"}

    result = _assess_capture_boundary_consistency(
        before=before,
        after=after,
        temporal_capture=True,
    )

    assert result["status"] == "time_series"
    assert result["changed_signals"] == ["page_url_sha256"]
    assert result["unavailable_signals"] == ["account_identity"]


def test_static_capture_rejects_document_and_top_layer_drift() -> None:
    before = {
        "page_url_sha256": "sha256:url",
        "context_page_urls_sha256": ["sha256:url"],
        "active_page_index": 0,
        "document_time_origin_ms": 1,
        "visibility_state": "visible",
        "top_layer_sha256": None,
        "document_observation_available": True,
        "account_identity_available": False,
    }
    after = {
        **before,
        "document_time_origin_ms": 2,
        "top_layer_sha256": "sha256:popup",
    }

    result = _assess_capture_boundary_consistency(
        before=before,
        after=after,
        temporal_capture=False,
    )

    assert result["status"] == "identity_drift"
    assert result["changed_signals"] == [
        "document_time_origin_ms",
        "top_layer_sha256",
    ]


async def test_capture_marks_cross_page_artifacts_as_identity_drift(monkeypatch) -> None:
    class Page:
        def __init__(self) -> None:
            self.url = "https://example.com/before"

        def set_default_timeout(self, timeout: int) -> None:
            self.timeout = timeout

        async def evaluate(self, script: str) -> dict[str, Any]:
            del script
            return {
                "time_origin_ms": 1234.5,
                "visibility_state": "visible",
                "top_layer": None,
            }

        async def title(self) -> str:
            return "漂移后的页面"

    page = Page()
    context = SimpleNamespace(pages=[page])

    async def settle(**kwargs: Any) -> bool:
        del kwargs
        return True

    async def collect(page: Any, request: Any, **kwargs: Any) -> list[PageCaptureArtifact]:
        del request, kwargs
        page.url = "https://example.com/after"
        return [PageCaptureArtifact("visible_text", None, "采集中的页面发生跳转")]

    monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", settle)
    monkeypatch.setattr(page_capture_module, "_collect_artifacts", collect)
    browser = PlaywrightPageCaptureBrowser()

    result = await browser._capture_on_page(
        request=PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="capture-identity-drift",
            startup_policy="direct_home",
            capture_dom=False,
            capture_visible_text=True,
            capture_screenshot=False,
        ),
        context=context,
        page=page,
        console_logs=[],
        network_requests=[],
        timeout_error_type=TimeoutError,
    )

    assert result.metrics["page_settle_complete"] is False
    assert result.metrics["page_inspection_ready"] is False
    assert result.metrics["page_settle_diagnostics"]["outcome"] == ("capture_identity_drift")
    consistency = result.metrics["capture_consistency"]
    assert consistency["status"] == "identity_drift"
    assert consistency["changed_signals"] == [
        "page_url_sha256",
        "context_page_urls_sha256",
    ]


async def test_playwright_single_trigger_captures_transient_message_style_and_frame() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    try:
        async with async_playwright() as playwright:
            probe = await playwright.chromium.launch(headless=True)
            await probe.close()
    except Exception as exc:  # noqa: BLE001 - 仅探测可执行文件是否可用
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    html = """
    <button id="show-toast">保存</button>
    <script>
      document.querySelector("#show-toast").addEventListener("click", () => {
        setTimeout(() => {
          const toast = document.createElement("div");
          toast.className = "product-toast";
          toast.setAttribute("role", "status");
          toast.textContent = "保存成功";
          Object.assign(toast.style, {
            position: "fixed",
            top: "24px",
            left: "50%",
            color: "rgb(255, 255, 255)",
            backgroundColor: "rgba(0, 0, 0, 0.72)",
            fontSize: "16px",
            borderRadius: "8px",
            padding: "8px 12px",
          });
          document.body.appendChild(toast);
          setTimeout(() => toast.remove(), 600);
        }, 150);
      });
    </script>
    """
    browser = PlaywrightPageCaptureBrowser()
    session_id = "transient-message-session"
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="data:text/html;charset=utf-8," + quote(html),
                session_id=session_id,
                capture_screenshot=False,
            )
        )
        result = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector="#show-toast",
                session_id=session_id,
                capture_transient_messages=True,
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=True,
                full_page=False,
            )
        )
    finally:
        await browser.close_session(session_id)

    observation = result.action_trace[-1]["transient_message_observation"]
    message = result.action_trace[-1]["transient_messages"][0]
    assert observation == {
        "schema_version": "transient-message-observation.v1",
        "status": "observed",
        "message_count": 1,
    }
    assert message["kind"] == "toast"
    assert message["text"] == "保存成功"
    assert message["position_zone"] == "top_center"
    assert message["style"]["font_size"] == "16px"
    assert message["style"]["background_opacity"] == 0.72
    transient_frames = [
        item
        for item in result.artifacts
        if item.artifact_type == "screenshot"
        and isinstance(item.content, dict)
        and item.content.get("purpose") == "transient_message"
    ]
    assert len(transient_frames) == 1
    assert transient_frames[0].content["frames"][0]["message_event_ids"] == ["transient-1"]


async def test_ad_landing_capture_keeps_source_and_landing_frames_in_one_action_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def serve_page(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        request = await reader.read(4096)
        path = request.split(b" ", 2)[1] if b" " in request else b"/"
        body = (
            b"<title>Summer Campaign</title><main><h1>Summer Campaign</h1><p>Learn more</p></main>"
            if path.startswith(b"/landing")
            else b'<title>Content</title><main><a id="ad" href="/landing">'
            b"Summer Campaign</a></main>"
        )
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve_page, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    source_url = f"http://127.0.0.1:{port}/source"
    landing_url = "https://ads.other.test/landing"

    @asynccontextmanager
    async def isolated(*, browser, url, **_kwargs):
        assert url == landing_url
        isolated_context = await browser.new_context()
        try:
            await isolated_context.route(
                landing_url,
                lambda route: route.fulfill(
                    body=(
                        "<title>Summer Campaign</title><main><h1>Summer Campaign</h1>"
                        "<p>Learn more</p></main>"
                    ),
                    content_type="text/html",
                ),
            )
            landing_page = await isolated_context.new_page()
            await landing_page.goto(landing_url)
            yield landing_page, SimpleNamespace(denied_reasons=set())
        finally:
            await isolated_context.close()

    from services.api.app.infrastructure.browser import ad_isolated_page

    monkeypatch.setattr(ad_isolated_page, "isolated_ad_page", isolated)
    browser = PlaywrightPageCaptureBrowser()
    session_id = "ad-landing-consistency-session"
    try:
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=source_url,
                session_id=session_id,
                capture_screenshot=False,
            )
        )
        result = await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url=landing_url,
                ad_landing_url=landing_url,
                session_id=session_id,
                navigation_depth=0,
                capture_ad_landing_evidence=True,
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=True,
                full_page=False,
            )
        )
    finally:
        await browser.close_session(session_id)
        server.close()
        await server.wait_closed()

    evidence = result.action_trace[-1]["ad_navigation_evidence"]
    assert evidence["status"] == "observed"
    assert evidence["navigation_mode"] == "new_page"
    assert evidence["pair_complete"] is True
    assert result.action_trace[-1]["action"] == "navigate"
    assert result.action_trace[-1]["navigation_mode"] == "isolated_context_goto"
    assert evidence["landing"]["title"] == "Summer Campaign"
    assert evidence["landing"]["dom_status"] == "readable"
    sequences = [
        item.content
        for item in result.artifacts
        if item.artifact_type == "screenshot"
        and isinstance(item.content, dict)
        and item.content.get("purpose") == "ad_landing_consistency"
    ]
    assert len(sequences) == 1
    assert [frame["role"] for frame in sequences[0]["frames"]] == [
        "source",
        "landing",
    ]


async def test_playwright_browser_limits_concurrent_capture_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = PlaywrightPageCaptureBrowser(max_concurrent_captures=2)
    active = 0
    max_active = 0

    async def capture_with_slot(request: PageCaptureRequest) -> PageCaptureResult:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return PageCaptureResult(
            execution_status="success",
            mode=request.mode.value,
            reachable=True,
            final_url=request.url,
            title="并发采集",
            status_code=200,
            artifacts=[],
            console_logs=[],
            network_requests=[],
            action_trace=[],
            post_action_snapshot=None,
        )

    monkeypatch.setattr(browser, "_capture_with_slot", capture_with_slot)

    results = await asyncio.gather(
        *[
            browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.GOTO,
                    url=f"https://example.com/{index}",
                    session_id=f"run-{index}",
                )
            )
            for index in range(4)
        ]
    )

    assert len(results) == 4
    assert max_active == 2


async def test_playwright_browser_starts_operation_timeout_after_slot_acquisition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = PlaywrightPageCaptureBrowser(max_concurrent_captures=1)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def capture_with_slot(request: PageCaptureRequest) -> PageCaptureResult:
        if request.session_id == "queued-run-0":
            first_started.set()
            await release_first.wait()
        return PageCaptureResult(
            execution_status="success",
            mode=request.mode.value,
            reachable=True,
            final_url=request.url,
            title="排队采集",
            status_code=200,
            artifacts=[],
            console_logs=[],
            network_requests=[],
            action_trace=[],
            post_action_snapshot=None,
        )

    monkeypatch.setattr(browser, "_capture_with_slot", capture_with_slot)
    requests = [
        PageCaptureRequest(
            mode=PageCaptureMode.GOTO,
            url=f"https://example.com/{index}",
            session_id=f"queued-run-{index}",
        )
        for index in range(2)
    ]

    first_task = asyncio.create_task(browser.capture_with_timeout(requests[0], timeout_seconds=0.2))
    await first_started.wait()
    queued_task = asyncio.create_task(
        browser.capture_with_timeout(requests[1], timeout_seconds=0.03)
    )
    await asyncio.sleep(0.05)

    assert not queued_task.done()

    release_first.set()
    results = await asyncio.gather(first_task, queued_task)

    assert len(results) == 2


async def test_playwright_browser_discards_crashed_bound_session(monkeypatch) -> None:
    browser = PlaywrightPageCaptureBrowser()
    closed: list[str] = []

    class Closable:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            closed.append(self.name)

        async def stop(self) -> None:
            closed.append(self.name)

    session_id = "run-crashed-session"
    browser._sessions[session_id] = page_capture_module._PlaywrightSession(
        playwright=Closable("playwright"),
        browser=Closable("browser"),
        context=SimpleNamespace(),
        page=SimpleNamespace(),
    )

    async def crash_capture(**kwargs) -> PageCaptureResult:
        del kwargs
        raise RuntimeError("Page.content: Target crashed")

    monkeypatch.setattr(browser, "_capture_on_page", crash_capture)

    with pytest.raises(BrowserSessionCrashedError, match="Target crashed"):
        await browser.capture(
            PageCaptureRequest(
                mode=PageCaptureMode.OBSERVE,
                session_id=session_id,
            )
        )

    assert session_id not in browser._sessions
    assert closed == ["browser", "playwright"]


def test_browser_session_crash_is_classified_as_infrastructure_failure() -> None:
    failure = classify_tool_exception(
        "page_capture_tool",
        BrowserSessionCrashedError("Page.content: Target crashed"),
    )

    assert failure is not None
    assert failure.code == "PAGE_CAPTURE_SESSION_CRASHED"
    assert failure.retryable is True


async def test_single_trigger_rejects_high_risk_action_before_browser_call() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        url="https://example.com",
        trigger_action="submit",
    )

    with pytest.raises(BlockedPageCaptureActionError, match="submit"):
        await capture_page(request=request, browser=browser)

    assert browser.requests == []


async def test_obstruction_chain_clear_requires_trusted_close_click() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        selector=".content-card",
        trigger_action="click",
        clear_obstructions=True,
    )

    with pytest.raises(BlockedPageCaptureActionError, match="弹窗链清障"):
        await capture_page(request=request, browser=browser)

    assert browser.requests == []


async def test_obstruction_chain_clear_accepts_only_frozen_age_confirmation_selector() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        selector=AGE_CONFIRM_ENTER_SELECTOR,
        trigger_action="click",
        clear_obstructions=True,
    )

    await capture_page(request=request, browser=browser)

    assert browser.requests == [request]


async def test_obstruction_chain_clear_rejects_age_gate_leave_selector() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        selector="#age-gate .age-gate-leave:visible",
        trigger_action="click",
        clear_obstructions=True,
    )

    with pytest.raises(BlockedPageCaptureActionError, match="弹窗链清障"):
        await capture_page(request=request, browser=browser)

    assert browser.requests == []


def test_age_confirmation_is_not_replayed_as_popup_close_selector() -> None:
    selector = AGE_CONFIRM_ENTER_SELECTOR

    assert page_capture_module._obstruction_chain_dismissal_selectors(selector) == ("text=关闭",)


def test_age_confirmation_uses_proven_next_popup_target() -> None:
    selector = AGE_CONFIRM_ENTER_SELECTOR

    resolved = page_capture_module._nested_obstruction_click_selector(
        request_selector=selector,
        popup_target={
            "dialog_present": True,
            "kind": "close_control",
            "selector": "html > body > div.popup-form > div.close-img",
        },
    )

    assert resolved == "html > body > div.popup-form > div.close-img"


def test_age_confirmation_preserves_safe_overlay_resolution() -> None:
    selector = AGE_CONFIRM_ENTER_SELECTOR

    resolved = page_capture_module._nested_obstruction_click_selector(
        request_selector=selector,
        popup_target={
            "dialog_present": True,
            "kind": "overlay",
            "selector": "html > body > div.van-overlay",
            "position": {"x": 2, "y": 2},
        },
    )

    assert resolved == "text=关闭"


async def test_signed_popup_binding_accepts_opaque_platform_selector() -> None:
    browser = FakePageCaptureBrowser()
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        selector="flt-semantics-host flt-semantics[flt-tappable]:visible",
        trigger_action="click",
        clear_obstructions=True,
        allow_high_z_obstruction_hide=True,
    )

    await capture_page(request=request, browser=browser)

    assert browser.requests == [request]


async def test_obstruction_chain_clear_is_forwarded_to_browser_request() -> None:
    browser = FakePageCaptureBrowser()

    await capture_page_for_agent(
        mode="single_trigger",
        selector="text=关闭",
        trigger_action="click",
        clear_obstructions=True,
        browser=browser,
    )

    assert browser.requests[0].clear_obstructions is True


async def test_obstruction_chain_clear_dismisses_remaining_layers_with_one_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        {
            "dialog_present": True,
            "kind": "close_control",
            "selector": "html > body > div:nth-of-type(2) > i",
        },
        {
            "dialog_present": True,
            "kind": "overlay",
            "selector": "html > body > div:nth-of-type(3)",
        },
        {"dialog_present": False},
    ]

    async def _resolve(*, page: Any, selector: str) -> dict[str, Any]:
        assert selector == "text=关闭"
        return targets.pop(0)

    async def _trigger(
        *,
        page: Any,
        context: Any,
        request: PageCaptureRequest,
        action_trace: list[dict[str, Any]],
        timeout_ms: int,
        timeout_error_type: type[Exception],
    ) -> None:
        assert request.clear_obstructions is False
        assert request.selector == "text=关闭"
        action_trace.append({"action": "click"})

    class PopupPage:
        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            return b"popup"

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", _resolve)
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", _trigger)
    action_trace = [{"action": "click"}]
    frames: list[dict[str, Any]] = []

    metrics = await page_capture_module._clear_remaining_obstructions(
        page=PopupPage(),
        context=SimpleNamespace(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
            clear_obstructions=True,
        ),
        action_trace=action_trace,
        obstruction_frames=frames,
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
    )

    assert metrics["outcome"] == "stable"
    assert metrics["dismissal_count"] == 3
    assert metrics["remaining_obstruction"] is False
    assert [item["obstruction_chain_index"] for item in action_trace] == [1, 2, 3]
    assert len(frames) == 2


async def test_obstruction_chain_waits_for_delayed_next_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr(
        page_capture_module, "time", SimpleNamespace(perf_counter=lambda: clock[0]),
    )

    class DelayedPopupPage:
        def __init__(self) -> None:
            self.wait_count = 0
            self.waited_ms: list[int] = []

        async def wait_for_timeout(self, timeout_ms: int) -> None:
            self.waited_ms.append(timeout_ms)
            self.wait_count += 1
            clock[0] += timeout_ms / 1000

    page = DelayedPopupPage()

    async def _resolve(*, page: Any, selector: str) -> dict[str, Any]:
        assert selector == "text=关闭"
        if page.wait_count == 1:
            return {"dialog_present": True, "kind": "close_control"}
        return {"dialog_present": False}

    async def _trigger(
        *,
        page: Any,
        context: Any,
        request: PageCaptureRequest,
        action_trace: list[dict[str, Any]],
        timeout_ms: int,
        timeout_error_type: type[Exception],
    ) -> None:
        del page, context, timeout_ms, timeout_error_type
        assert request.clear_obstructions is False
        action_trace.append({"action": "click"})

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", _resolve)
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", _trigger)
    action_trace = [{"action": "click"}]

    metrics = await page_capture_module._clear_remaining_obstructions(
        page=page,
        context=SimpleNamespace(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
            capture_screenshot=False,
            clear_obstructions=True,
        ),
        action_trace=action_trace,
        obstruction_frames=[],
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
    )

    assert metrics["outcome"] == "stable"
    assert metrics["dismissal_count"] == 2
    assert page.wait_count >= 2
    assert page.waited_ms[0] == page_capture_module.OBSTRUCTION_CHAIN_LAYER_SETTLE_MS
    assert [item["obstruction_chain_index"] for item in action_trace] == [1, 2]


async def test_obstruction_chain_clear_stops_at_dismissal_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolve(*, page: Any, selector: str) -> dict[str, Any]:
        return {"dialog_present": True, "kind": "close_control"}

    async def _trigger(
        *,
        page: Any,
        context: Any,
        request: PageCaptureRequest,
        action_trace: list[dict[str, Any]],
        timeout_ms: int,
        timeout_error_type: type[Exception],
    ) -> None:
        action_trace.append({"action": "click"})

    monkeypatch.setattr(page_capture_module, "MAX_OBSTRUCTION_CHAIN_DISMISSALS", 2)
    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", _resolve)
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", _trigger)
    action_trace = [{"action": "click"}]

    metrics = await page_capture_module._clear_remaining_obstructions(
        page=SimpleNamespace(),
        context=SimpleNamespace(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
            capture_screenshot=False,
            clear_obstructions=True,
        ),
        action_trace=action_trace,
        obstruction_frames=[],
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
    )

    assert metrics["outcome"] == "dismissal_limit_reached"
    assert metrics["dismissal_count"] == 2
    assert metrics["remaining_obstruction"] is True


async def test_capture_page_for_agent_returns_dict_payload() -> None:
    browser = FakePageCaptureBrowser()

    result = await capture_page_for_agent(
        mode="observe",
        url="https://example.com",
        browser=browser,
    )

    assert result["execution_status"] == "success"
    assert result["mode"] == "observe"
    assert result["artifacts"][0]["artifact_id"] is None


async def test_capture_page_for_agent_applies_task_viewport_to_browser_request() -> None:
    browser = FakePageCaptureBrowser()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="task-1",
            agent_run_id="run-1",
            task_config={"viewport": "390x844"},
        )
    ):
        await capture_page_for_agent(
            mode="goto",
            url="https://example.com",
            browser=browser,
            persistence_service=FakePageCapturePersistenceService(),
        )

    request = browser.requests[0]
    assert request.viewport_width == 390
    assert request.viewport_height == 844
    assert _browser_viewport_options(request) == {"viewport": {"width": 390, "height": 844}}


def test_browser_context_options_support_fixed_desktop_profile() -> None:
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        viewport_width=1920,
        viewport_height=1080,
        screen_width=1920,
        screen_height=1080,
        device_scale_factor=1.0,
        is_mobile=False,
        has_touch=False,
    )

    assert _browser_context_options(request) == {
        "viewport": {"width": 1920, "height": 1080},
        "screen": {"width": 1920, "height": 1080},
        "device_scale_factor": 1.0,
        "is_mobile": False,
        "has_touch": False,
    }


def test_browser_context_options_preserve_h5_touch_defaults() -> None:
    request = PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE,
        viewport_width=390,
        viewport_height=844,
    )

    assert _browser_context_options(request) == {
        "viewport": {"width": 390, "height": 844},
        "device_scale_factor": 1.0,
        "is_mobile": True,
        "has_touch": True,
    }


@pytest.mark.parametrize(
    "raw_url",
    [None, "", "  ", "<目标URL>", "原始任务上下文中的目标 URL"],
)
async def test_capture_page_for_agent_resolves_trusted_task_url_placeholder(
    raw_url: str | None,
) -> None:
    browser = FakePageCaptureBrowser()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="task-1",
            agent_run_id="run-1",
            task_config={"_trusted_target_url": "https://example.com/product"},
        )
    ):
        await capture_page_for_agent(
            mode="goto",
            url=raw_url,
            browser=browser,
            persistence_service=FakePageCapturePersistenceService(),
        )

    assert browser.requests[0].url == "https://example.com/product"


def test_network_request_remains_inflight_until_request_finished() -> None:
    class EventPage:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def on(self, event: str, handler: Any) -> None:
            self.handlers[event] = handler

    page = EventPage()
    inflight: dict[int, dict[str, str]] = {}
    network: list[dict[str, Any]] = []
    _attach_observers(page, [], network, inflight)
    request = SimpleNamespace(
        url="https://example.com/api/list",
        resource_type="xhr",
        method="GET",
    )
    response = SimpleNamespace(
        url=request.url,
        status=200,
        request=request,
    )

    page.handlers["request"](request)
    page.handlers["response"](response)
    assert id(request) in inflight

    page.handlers["requestfinished"](request)
    assert id(request) not in inflight


def test_secret_window_request_response_stays_suppressed_after_observers_resume() -> None:
    class EventPage:
        def __init__(self) -> None:
            self.handlers: dict[str, Any] = {}

        def on(self, event: str, handler: Any) -> None:
            self.handlers[event] = handler

    page = EventPage()
    muted = True
    network: list[dict[str, Any]] = []
    suppressed: set[int] = set()
    _attach_observers(
        page,
        [],
        network,
        {},
        is_muted=lambda: muted,
        suppressed_request_ids=suppressed,
    )
    request = SimpleNamespace(
        url="https://example.com/login?phone=SECRET_PHONE",
        resource_type="xhr",
        method="POST",
    )
    response = SimpleNamespace(url=request.url, status=200, request=request)

    page.handlers["request"](request)
    muted = False
    page.handlers["response"](response)
    page.handlers["requestfinished"](request)

    assert network == []
    assert suppressed == set()


async def test_page_capture_tool_never_returns_full_dom_or_screenshot() -> None:
    result = await capture_page_for_agent(
        mode="goto",
        url="https://example.com",
        browser=LargePageCaptureBrowser(),
    )

    encoded = json.dumps(result, ensure_ascii=False)
    assert "SECRET_SCREENSHOT_BASE64" not in encoded
    assert "<html>" not in encoded
    assert len(result["visible_text_excerpt"]) <= 4000
    assert result["dom_summary"]["character_count"] == 10013
    assert result["screenshot_summary"]["count"] == 1
    assert result["content_fingerprint"].startswith("sha256:")
    assert len(result["content_fingerprint"]) == 71
    assert result["visible_text_fingerprint"].startswith("sha256:")
    assert len(result["visible_text_fingerprint"]) == 71
    assert len(encoded.encode("utf-8")) < 20000


async def test_page_capture_tool_persists_artifacts_for_bound_run() -> None:
    service = FakePageCapturePersistenceService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await capture_page_for_agent(
            mode="goto",
            url="https://example.com",
            browser=LargePageCaptureBrowser(),
            persistence_service=service,
        )

    assert service.calls[0]["task_id"] == "task-1"
    assert service.calls[0]["agent_run_id"] == "run-1"
    assert result["page_capture_id"] == "capture-1"
    assert result["artifact_ids"] == [
        "artifact-dom",
        "artifact-visible-text",
        "artifact-screenshot",
    ]
    assert result["artifacts"] == [
        {
            "artifact_type": "dom",
            "artifact_id": "artifact-dom",
            "content_type": "text/html; charset=utf-8",
            "size_bytes": 10013,
        },
        {
            "artifact_type": "visible_text",
            "artifact_id": "artifact-visible-text",
            "content_type": "text/plain; charset=utf-8",
            "size_bytes": 24000,
        },
        {
            "artifact_type": "screenshot",
            "artifact_id": "artifact-screenshot",
            "content_type": "image/png",
            "size_bytes": 1024,
        },
    ]


async def test_page_capture_tool_persists_explicit_journey_role_and_semantics() -> None:
    service = FakePageCapturePersistenceService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await capture_page_for_agent(
            mode="goto",
            url="https://example.com/detail",
            supersedes_page_capture_id="capture-before",
            capture_role="refreshed",
            inherit_parent_semantics=True,
            browser=LargePageCaptureBrowser(),
            persistence_service=service,
        )

    assert service.calls[0]["parent_page_capture_id"] == "capture-before"
    assert service.calls[0]["capture_role"] == "refreshed"
    assert service.calls[0]["inherit_parent_semantics"] is True


async def test_bound_run_uses_agent_run_id_as_browser_session() -> None:
    browser = FakePageCaptureBrowser()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await capture_page_for_agent(
            mode="goto",
            url="https://example.com",
            browser=browser,
            persistence_service=FakePageCapturePersistenceService(),
        )

    assert browser.requests[0].session_id == "run-1"


async def test_shadow_capture_reuses_same_state_without_repeating_business_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ShadowPersistenceService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def persist(self, **kwargs: Any) -> PersistedPageCapture:
            self.calls.append(kwargs)
            capture_index = len(self.calls)
            return PersistedPageCapture(
                page_capture_id=f"capture-{capture_index}",
                page_seq=capture_index,
                artifacts=[],
            )

    class ShadowComparisonService:
        def __init__(self) -> None:
            self.comparisons: list[Any] = []

        async def record(self, comparison: Any) -> None:
            self.comparisons.append(comparison)

    monkeypatch.setattr(
        page_capture_tools,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                pipeline_mode="shadow",
                shadow_sample_rate=1.0,
            )
        ),
    )
    browser = FakePageCaptureBrowser()
    persistence = ShadowPersistenceService()
    comparison_service = ShadowComparisonService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await capture_page_for_agent(
            mode="goto",
            url="https://example.com",
            browser=browser,
            persistence_service=persistence,
            shadow_service=comparison_service,
        )

    assert result["page_capture_id"] == "capture-1"
    assert [request.mode for request in browser.requests] == [
        PageCaptureMode.GOTO,
        PageCaptureMode.OBSERVE,
    ]
    assert browser.requests[1].url is None
    assert browser.requests[1].trigger_action is None
    assert persistence.calls[1]["parent_page_capture_id"] == "capture-1"
    assert persistence.calls[1]["capture_role"] == "shadow_candidate"
    assert comparison_service.comparisons[0].summary["business_action_repeated"] is False


async def test_capture_page_for_agent_passes_navigation_depth_to_browser() -> None:
    browser = FakePageCaptureBrowser()

    await capture_page_for_agent(
        mode="single_trigger",
        url="https://example.com",
        selector="a.detail",
        trigger_action="click",
        navigation_depth=1,
        browser=browser,
    )

    assert browser.requests[0].navigation_depth == 1


async def test_capture_page_for_agent_returns_exploration_metadata() -> None:
    browser = FakePageCaptureBrowser()

    result = await capture_page_for_agent(
        mode="observe",
        exploration_action_id="exploration-1",
        supersedes_page_capture_id="capture-1",
        browser=browser,
    )

    assert result["exploration_action_id"] == "exploration-1"
    assert result["supersedes_page_capture_id"] == "capture-1"


async def test_capture_page_for_agent_isolates_composite_browser_timeout() -> None:
    result = await capture_page_for_agent(
        mode="single_trigger",
        trigger_action="click",
        selector=".detail-card",
        exploration_action_id="exploration-timeout",
        browser=HangingPageCaptureBrowser(),
        browser_operation_timeout_seconds=0.01,
    )

    assert result["execution_status"] == "error"
    assert result["handled"] is True
    assert result["failure_scope"] == "page"
    assert result["failure_stage"] == "browser_capture"
    assert result["error_code"] == "PAGE_CAPTURE_TIMEOUT"
    assert result["retryable"] is False
    assert result["failure_isolated"] is True
    assert result["exploration_action_id"] == "exploration-timeout"


async def test_capture_page_for_agent_marks_transient_goto_failure_retryable() -> None:
    class ConnectionClosedBrowser:
        async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
            raise RuntimeError(f"Page.goto: net::ERR_CONNECTION_CLOSED at {request.url}")

    result = await capture_page_for_agent(
        mode="goto",
        url="https://example.com",
        exploration_action_id="exploration-connection-closed",
        browser=ConnectionClosedBrowser(),
    )

    assert result["execution_status"] == "error"
    assert result["error_code"] == "PAGE_CAPTURE_FAILED"
    assert result["retryable"] is True
    assert result["failure_kind"] == "infrastructure"
    assert result["exploration_action_id"] == "exploration-connection-closed"


async def test_capture_page_for_agent_uses_configured_composite_browser_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        page_capture_tools,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                page_capture_timeout_seconds=0.01,
                pipeline_mode="off",
            )
        ),
    )

    result = await capture_page_for_agent(
        mode="observe",
        browser=DelayedPageCaptureBrowser(),
    )

    assert result["execution_status"] == "error"
    assert result["error_code"] == "PAGE_CAPTURE_TIMEOUT"


async def test_capture_page_for_agent_limits_capture_and_persistence_as_one_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        page_capture_tools,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                page_capture_timeout_seconds=1.0,
                page_capture_concurrency=1,
                pipeline_mode="off",
            )
        ),
    )
    persistence = ConcurrentPageCapturePersistenceService()
    browser = FakePageCaptureBrowser()

    async def run_capture(run_id: str) -> dict[str, Any]:
        with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id=run_id)):
            return await capture_page_for_agent(
                mode="goto",
                url=f"https://example.com/{run_id}",
                browser=browser,
                persistence_service=persistence,
            )

    results = await asyncio.gather(run_capture("run-1"), run_capture("run-2"))

    assert all(result["execution_status"] == "success" for result in results)
    assert persistence.max_active == 1


async def test_capture_page_for_agent_isolates_persistence_timeout() -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await capture_page_for_agent(
            mode="goto",
            url="https://example.com",
            exploration_action_id="exploration-persistence-timeout",
            browser=FakePageCaptureBrowser(),
            persistence_service=HangingPageCapturePersistenceService(),
            persistence_operation_timeout_seconds=0.01,
        )

    assert result["execution_status"] == "error"
    assert result["failure_stage"] == "artifact_persistence"
    assert result["failure_kind"] == "infrastructure"
    assert result["error_code"] == "PAGE_CAPTURE_PERSISTENCE_TIMEOUT"
    assert result["retryable"] is False
    assert result["exploration_action_id"] == "exploration-persistence-timeout"


async def test_capture_page_for_agent_isolates_persistence_failure() -> None:
    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await capture_page_for_agent(
            mode="goto",
            url="https://example.com",
            exploration_action_id="exploration-persistence-failure",
            browser=FakePageCaptureBrowser(),
            persistence_service=FailingPageCapturePersistenceService(),
        )

    assert result["execution_status"] == "error"
    assert result["failure_stage"] == "artifact_persistence"
    assert result["failure_kind"] == "infrastructure"
    assert result["error_code"] == "PAGE_CAPTURE_PERSISTENCE_FAILED"
    assert result["retryable"] is False
    assert result["exploration_action_id"] == "exploration-persistence-failure"


async def test_overlay_dismissal_clicks_a_safe_overlay_corner() -> None:
    class OverlayLocator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    class OverlayPage:
        def __init__(self) -> None:
            self.selector: str | None = None
            self.overlay = OverlayLocator()

        def locator(self, selector: str) -> OverlayLocator:
            self.selector = selector
            return self.overlay

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"遮罩关闭不应通过页面中心点击：{selector=}, {timeout=}")

    page = OverlayPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.selector == ".van-overlay:visible"
    assert page.overlay.calls == [{"position": {"x": 1, "y": 1}, "timeout": 30000}]
    assert action_trace == [
        {
            "action": "click",
            "selector": ".van-overlay:visible",
            "position": {"x": 1, "y": 1},
        }
    ]


async def test_overlay_dismissal_directly_dispatches_when_child_intercepts_pointer() -> None:
    class PointerInterceptedTimeout(TimeoutError):
        pass

    class LoadingLocator:
        async def count(self) -> int:
            return 0

    class OverlayLocator:
        def __init__(self) -> None:
            self.visible = True
            self.click_calls: list[dict[str, Any]] = []
            self.evaluate_calls: list[str] = []

        async def count(self) -> int:
            return 1 if self.visible else 0

        async def click(self, **kwargs: Any) -> None:
            self.click_calls.append(kwargs)
            raise PointerInterceptedTimeout("child subtree intercepts pointer events")

        async def evaluate(self, script: str) -> None:
            self.evaluate_calls.append(script)
            self.visible = False

    class OverlayPage:
        def __init__(self) -> None:
            self.loading = LoadingLocator()
            self.overlay = OverlayLocator()

        def locator(self, selector: str) -> LoadingLocator | OverlayLocator:
            if selector == ".loading-page:visible":
                return self.loading
            assert selector == ".van-overlay:visible"
            return self.overlay

    page = OverlayPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
        timeout_error_type=PointerInterceptedTimeout,
    )

    assert page.overlay.click_calls == [{"position": {"x": 1, "y": 1}, "timeout": 30000}]
    assert len(page.overlay.evaluate_calls) == 1
    assert action_trace[-1]["resolved_by"] == ("overlay_direct_dispatch_after_pointer_interception")
    assert action_trace[-1]["dismissal_verified"] is True


@pytest.mark.parametrize("dismissed", [True, False])
async def test_startup_ad_close_requires_loading_layer_to_disappear(
    dismissed: bool,
) -> None:
    class LoadingLayer:
        async def count(self) -> int:
            return 1

        async def wait_for(self, **kwargs: Any) -> None:
            assert kwargs == {"state": "hidden", "timeout": 5000}
            if not dismissed:
                raise TimeoutError("开屏广告仍然可见")

    class StartupPage:
        def __init__(self) -> None:
            self.clicked = False

        async def click(self, selector: str, *, timeout: int) -> None:
            assert selector == ".loading-page .down-time:visible"
            assert timeout == 30000
            self.clicked = True

        def locator(self, selector: str) -> LoadingLayer:
            assert selector in {
                ".loading-page .down-time:visible",
                ".loading-page:visible",
            }
            return LoadingLayer()

    page = StartupPage()
    action_trace: list[dict[str, Any]] = []
    request = PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        selector=".loading-page .down-time:visible",
        trigger_action="click",
    )

    if dismissed:
        await _run_single_trigger(
            page=page,
            context=object(),
            request=request,
            action_trace=action_trace,
            timeout_ms=30000,
        )
        assert action_trace[0]["dismissal_verified"] is True
    else:
        with pytest.raises(BlockedPageCaptureActionError) as exc_info:
            await _run_single_trigger(
                page=page,
                context=object(),
                request=request,
                action_trace=action_trace,
                timeout_ms=30000,
            )
        assert exc_info.value.error_code == "STARTUP_AD_DISMISSAL_NOT_OBSERVED"
        assert action_trace[0]["dismissal_verified"] is False
        assert action_trace[0]["result"] == "startup_ad_dismissal_not_observed"
    assert page.clicked is True


async def test_h5_startup_ad_close_retries_verified_direct_dispatch() -> None:
    class StartupControl:
        async def count(self) -> int:
            return 1

        async def evaluate(self, script: str, arg: Any = None) -> Any:
            if arg is not None:
                return {
                    "visible": True,
                    "enabled": True,
                    "center_hits_target": True,
                    "element_token": "startup-element-1",
                    "layer_signature": "fnv1a:12345678",
                    "x": 340,
                    "y": 40,
                    "width": 40,
                    "height": 24,
                }
            assert script == "(element) => element.click()"
            page.dismissed = True
            return None

    class StartupLayer:
        async def count(self) -> int:
            return 0 if page.dismissed else 1

        async def wait_for(self, **kwargs: Any) -> None:
            assert kwargs == {"state": "hidden", "timeout": 5000}
            if not page.dismissed:
                raise TimeoutError("首次点击未关闭")

    class StartupPage:
        def __init__(self) -> None:
            self.dismissed = False
            self.control = StartupControl()
            self.layer = StartupLayer()

        async def click(self, selector: str, *, timeout: int) -> None:
            assert selector == ".loading-page .down-time:visible"
            assert timeout == 30000

        def locator(self, selector: str) -> StartupControl | StartupLayer:
            if selector == ".loading-page .down-time:visible":
                return self.control
            assert selector == ".loading-page:visible"
            return self.layer

    page = StartupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".loading-page .down-time:visible",
            trigger_action="click",
            session_id="h5:run-1",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert action_trace[0]["startup_ad_direct_dispatch_retried"] is True
    assert action_trace[0]["startup_ad_direct_dispatch_verified"] is True
    assert action_trace[0]["dismissal_verified"] is True


async def test_global_count_down_skip_requires_control_to_disappear() -> None:
    class GlobalCountDownLayer:
        async def count(self) -> int:
            return 1

        async def wait_for(self, **kwargs: Any) -> None:
            assert kwargs == {"state": "hidden", "timeout": 5000}

    class StartupPage:
        def __init__(self) -> None:
            self.clicked_selector: str | None = None

        async def click(self, selector: str, *, timeout: int) -> None:
            assert timeout == 30000
            self.clicked_selector = selector

        def locator(self, selector: str) -> GlobalCountDownLayer:
            assert selector == page_capture_module.GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR
            return GlobalCountDownLayer()

    page = StartupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=page_capture_module.GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR,
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.clicked_selector == page_capture_module.GLOBAL_COUNT_DOWN_DISMISSAL_SELECTOR
    assert action_trace[-1]["dismissal_verified"] is True


async def test_startup_van_popup_replacement_verifies_original_layer_identity() -> None:
    """开屏关闭后若出现另一层同类 van-popup，不能把新弹层当成原层未关闭。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .van-popup {
        position: fixed;
        inset: 40px;
        z-index: 10;
        background: white;
      }
      #followup { display: none; z-index: 11; }
    </style>
    <script>
      window.dismissStartup = () => {
        document.querySelector('#startup').style.display = 'none';
        document.querySelector('#followup').style.display = 'block';
      };
    </script>
    <div id="startup" class="van-popup">
      <strong>开屏广告</strong>
      <button class="count-down" onclick="window.dismissStartup()">跳过</button>
    </div>
    <div id="followup" class="van-popup">
      <strong>后续业务弹层</strong>
      <button class="ordinary-close">关闭</button>
    </div>
    """

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境没有浏览器二进制时跳过
            if "Executable doesn't exist" in str(exc):
                pytest.skip(f"Playwright Chromium 不可用：{exc}")
            raise
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            action_trace: list[dict[str, Any]] = []

            await _run_single_trigger(
                page=page,
                context=object(),
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    selector=page_capture_module.POPUP_COUNT_DOWN_DISMISSAL_SELECTOR,
                    trigger_action="click",
                ),
                action_trace=action_trace,
                timeout_ms=3_000,
                timeout_error_type=TimeoutError,
            )

            assert await page.locator("#startup").is_hidden()
            assert await page.locator("#followup").is_visible()
            assert action_trace[-1]["dismissal_verified"] is True
        finally:
            await browser.close()


async def test_startup_van_popup_same_structure_replacement_is_not_dismissed() -> None:
    """后继弹层仍复用开屏倒计时结构时，必须保守判定原开屏未可证实消失。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .van-popup {
        position: fixed;
        inset: 40px;
        z-index: 10;
        background: white;
      }
      #followup { display: none; z-index: 11; }
    </style>
    <script>
      window.dismissStartup = () => {
        document.querySelector('#startup').style.display = 'none';
        document.querySelector('#followup').style.display = 'block';
      };
    </script>
    <div id="startup" class="van-popup">
      <strong>开屏广告</strong>
      <button class="count-down" onclick="window.dismissStartup()">跳过</button>
    </div>
    <div id="followup" class="van-popup">
      <strong>后继开屏广告结构</strong>
      <button class="count-down">跳过</button>
    </div>
    """

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境没有浏览器二进制时跳过
            if "Executable doesn't exist" in str(exc):
                pytest.skip(f"Playwright Chromium 不可用：{exc}")
            raise
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            action_trace: list[dict[str, Any]] = []

            with pytest.raises(BlockedPageCaptureActionError) as exc_info:
                await _run_single_trigger(
                    page=page,
                    context=object(),
                    request=PageCaptureRequest(
                        mode=PageCaptureMode.SINGLE_TRIGGER,
                        selector=page_capture_module.POPUP_COUNT_DOWN_DISMISSAL_SELECTOR,
                        trigger_action="click",
                    ),
                    action_trace=action_trace,
                    timeout_ms=1_000,
                    timeout_error_type=TimeoutError,
                )

            assert exc_info.value.error_code == "STARTUP_AD_DISMISSAL_NOT_OBSERVED"
            assert action_trace[-1]["dismissal_verified"] is False
            assert await page.locator("#startup").is_hidden()
            assert await page.locator("#followup").is_visible()
        finally:
            await browser.close()


async def test_startup_skip_wrap_countdown_reset_does_not_fake_dismissal() -> None:
    """同一开屏层把 0s 重置为 6s 时，不能因点击 selector 消失就判定关闭。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .van-popup {
        position: fixed;
        inset: 0;
        z-index: 10;
        background: black;
      }
      .skip-wrap { position: absolute; right: 24px; top: 15px; color: white; }
    </style>
    <div id="startup" class="van-popup">
      <div class="skip-wrap" onclick="this.firstElementChild.textContent='6s(跳过)'">
        <div>0s(跳过)</div>
      </div>
    </div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            action_trace: list[dict[str, Any]] = []

            with pytest.raises(BlockedPageCaptureActionError) as exc_info:
                await _run_single_trigger(
                    page=page,
                    context=object(),
                    request=PageCaptureRequest(
                        mode=PageCaptureMode.SINGLE_TRIGGER,
                        selector=page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
                        trigger_action="click",
                    ),
                    action_trace=action_trace,
                    timeout_ms=1_000,
                    timeout_error_type=TimeoutError,
                )

            assert exc_info.value.error_code == "STARTUP_AD_DISMISSAL_NOT_OBSERVED"
            assert action_trace[-1]["dismissal_verified"] is False
            assert await page.locator("#startup").is_visible()
            assert await page.locator(".skip-wrap").inner_text() == "6s(跳过)"
        finally:
            await browser.close()


async def test_startup_skip_wrap_brief_hide_then_recreate_is_not_dismissed() -> None:
    """旧层短暂 hidden 后重建同结构开屏时，必须在稳定窗口内识别复现。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .van-popup {
        position: fixed;
        inset: 0;
        z-index: 10;
        background: black;
      }
      #followup { display: none; z-index: 11; }
      .skip-wrap { position: absolute; right: 24px; top: 15px; color: white; }
    </style>
    <script>
      window.replaceStartup = () => {
        document.querySelector('#startup').style.display = 'none';
        window.setTimeout(() => {
          document.querySelector('#followup').style.display = 'block';
        }, 150);
      };
    </script>
    <div id="startup" class="van-popup">
      <div class="skip-wrap" onclick="window.replaceStartup()"><div>0s(跳过)</div></div>
    </div>
    <div id="followup" class="van-popup">
      <div class="skip-wrap"><div>0s(跳过)</div></div>
    </div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            action_trace: list[dict[str, Any]] = []

            with pytest.raises(BlockedPageCaptureActionError) as exc_info:
                await _run_single_trigger(
                    page=page,
                    context=object(),
                    request=PageCaptureRequest(
                        mode=PageCaptureMode.SINGLE_TRIGGER,
                        selector=page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
                        trigger_action="click",
                    ),
                    action_trace=action_trace,
                    timeout_ms=2_000,
                    timeout_error_type=TimeoutError,
                )

            assert exc_info.value.error_code == "STARTUP_AD_DISMISSAL_NOT_OBSERVED"
            assert action_trace[-1]["dismissal_verified"] is False
            assert await page.locator("#startup").is_hidden()
            assert await page.locator("#followup").is_visible()
        finally:
            await browser.close()


async def test_startup_ad_close_directly_dispatches_when_overlay_intercepts_pointer() -> None:
    class PointerInterceptedTimeout(TimeoutError):
        pass

    class CloseControl:
        def __init__(self, page: "StartupPage") -> None:
            self.page = page
            self.evaluate_calls: list[str] = []

        async def evaluate(self, script: str) -> None:
            self.evaluate_calls.append(script)
            self.page.loading_visible = False

    class LoadingLayer:
        def __init__(self, page: "StartupPage") -> None:
            self.page = page

        async def count(self) -> int:
            return 1 if self.page.loading_visible else 0

    class StartupPage:
        def __init__(self) -> None:
            self.loading_visible = True
            self.close_control = CloseControl(self)
            self.loading_layer = LoadingLayer(self)

        async def click(self, selector: str, *, timeout: int) -> None:
            assert selector == ".loading-page .down-time:visible"
            assert timeout == 30000
            raise PointerInterceptedTimeout(".van-overlay intercepts pointer events")

        def locator(self, selector: str) -> CloseControl | LoadingLayer:
            if selector == ".loading-page:visible":
                return self.loading_layer
            assert selector == ".loading-page .down-time:visible"
            return self.close_control

    page = StartupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".loading-page .down-time:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
        timeout_error_type=PointerInterceptedTimeout,
    )

    assert len(page.close_control.evaluate_calls) == 1
    assert action_trace[-1]["resolved_by"] == (
        "startup_ad_direct_dispatch_after_pointer_interception"
    )
    assert action_trace[-1]["dismissal_verified"] is True


@pytest.mark.parametrize(
    ("selector", "match_count", "proof_overrides", "expected_clicks"),
    [
        (
            page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
            1,
            {},
            [(330.0, 35.0)],
        ),
        (
            page_capture_module.GLOBAL_LAUNCH_DISMISSAL_SELECTOR,
            1,
            {},
            [(330.0, 35.0)],
        ),
        (page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR, 2, {}, []),
        (
            page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
            1,
            {"center_hits_target": False},
            [],
        ),
        (
            page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
            1,
            {"visible": False},
            [],
        ),
        ("text=跳过", 1, {}, []),
    ],
)
async def test_startup_ad_locator_failure_uses_only_unique_verified_text_coordinates(
    selector: str,
    match_count: int,
    proof_overrides: dict[str, bool],
    expected_clicks: list[tuple[float, float]],
) -> None:
    """已登记开屏文字控件可用受信中心坐标后备，歧义或命中失败必须拒绝。"""

    class LocatorFailure(TimeoutError):
        pass

    class StartupLocator:
        async def count(self) -> int:
            return match_count

        async def evaluate(
            self,
            script: str,
            text_contract: dict[str, Any],
        ) -> dict[str, Any]:
            assert "elementFromPoint" in script
            if selector == page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR:
                assert text_contract == {
                    "expected_text": "0s(跳过)",
                    "use_direct_child": True,
                }
            elif selector == page_capture_module.GLOBAL_LAUNCH_DISMISSAL_SELECTOR:
                assert text_contract == {
                    "expected_text": "跳过",
                    "use_direct_child": False,
                }
            return {
                "visible": True,
                "enabled": True,
                "center_hits_target": True,
                "element_token": "startup-element-1",
                "layer_signature": "startup-layer-1",
                "x": 300.0,
                "y": 20.0,
                "width": 60.0,
                "height": 30.0,
                **proof_overrides,
            }

    class Mouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class StartupPage:
        def __init__(self) -> None:
            self.mouse = Mouse()
            self.target = StartupLocator()

        async def click(self, selector: str, *, timeout: int) -> None:
            assert selector == expected_selector
            assert timeout == 30_000
            raise LocatorFailure("locator actionability check timed out")

        def locator(self, selector: str) -> StartupLocator:
            assert selector == expected_selector
            return self.target

    expected_selector = selector
    page = StartupPage()
    action_trace: list[dict[str, Any]] = [{}]

    if expected_clicks:
        await _click_startup_ad_with_pointer_interception_fallback(
            page=page,
            selector=selector,
            timeout_ms=30_000,
            action_trace=action_trace,
            timeout_error_type=LocatorFailure,
        )
        assert action_trace[-1]["resolved_by"] == (
            "startup_ad_verified_text_coordinate_after_locator_failure"
        )
        assert action_trace[-1]["click_coordinates"] == {"x": 330.0, "y": 35.0}
    else:
        with pytest.raises(LocatorFailure, match="actionability"):
            await _click_startup_ad_with_pointer_interception_fallback(
                page=page,
                selector=selector,
                timeout_ms=30_000,
                action_trace=action_trace,
                timeout_error_type=LocatorFailure,
            )

    assert page.mouse.clicks == expected_clicks


async def test_startup_ad_coordinate_fallback_rejects_replaced_layer_after_timeout() -> None:
    """Locator 超时期间若原层已被替换，禁止坐标后备再点击下一层广告。"""

    class LocatorFailure(TimeoutError):
        pass

    class StartupLocator:
        def __init__(self) -> None:
            self.proof_count = 0

        async def count(self) -> int:
            return 1

        async def evaluate(
            self,
            script: str,
            text_contract: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            assert "elementFromPoint" in script
            self.proof_count += 1
            return {
                "visible": True,
                "enabled": True,
                "center_hits_target": True,
                "element_token": f"startup-element-{self.proof_count}",
                "layer_signature": f"startup-layer-{self.proof_count}",
                "x": 300.0,
                "y": 20.0,
                "width": 60.0,
                "height": 30.0,
            }

    class Mouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class StartupPage:
        def __init__(self) -> None:
            self.mouse = Mouse()
            self.target = StartupLocator()

        async def click(self, selector: str, *, timeout: int) -> None:
            raise LocatorFailure("locator actionability check timed out")

        def locator(self, selector: str) -> StartupLocator:
            return self.target

    page = StartupPage()

    with pytest.raises(LocatorFailure, match="actionability"):
        await _click_startup_ad_with_pointer_interception_fallback(
            page=page,
            selector=page_capture_module.POPUP_SKIP_WRAP_DISMISSAL_SELECTOR,
            timeout_ms=30_000,
            action_trace=[{}],
            timeout_error_type=LocatorFailure,
        )

    assert page.target.proof_count == 2
    assert page.mouse.clicks == []


async def test_popup_close_text_is_resolved_to_real_accessible_control() -> None:
    class CloseLocator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    class PopupPage:
        def __init__(self) -> None:
            self.locators: dict[str, CloseLocator] = {}
            self.verification_args: dict[str, Any] | None = None

        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert "dialog_present" in script
            assert "semanticLeafControls" in script
            assert args == {
                "requestedSelector": "text=关闭",
                "selector": "text=关闭",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": "#ad-close",
                "dialog_selector": "#ad-dialog",
                "dialog_signature": "dialog-signature",
                "verification_required": True,
            }

        def locator(self, selector: str) -> CloseLocator:
            return self.locators.setdefault(selector, CloseLocator())

        async def wait_for_function(
            self,
            script: str,
            *,
            arg: dict[str, Any],
            timeout: int,
            polling: int,
        ) -> None:
            assert "currentSignature" in script
            assert timeout == 2300
            assert polling == 100
            self.verification_args = arg

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"含糊关闭文本不得直接点击：{selector=}, {timeout=}")

    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.locators["#ad-close"].calls == [{"timeout": 30000}]
    assert page.verification_args == {
        "selector": "#ad-dialog",
        "signature": "dialog-signature",
        "stableMs": 800,
    }
    assert action_trace == [
        {
            "action": "click",
            "requested_selector": "text=关闭",
            "selector": "#ad-close",
            "resolved_by": "visible_popup_structure",
            "dismissal_kind": "close_control",
            "dismissal_verified": True,
            "popup_dismissal_outcome": "popup_dismissal_observation_unavailable",
        }
    ]


async def test_dense_van_popup_clicks_close_wrapper_before_decorative_icon() -> None:
    """d24 式弹窗的图标禁用指针事件时，应点击承载事件的 ``.close_btn``。"""
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    ad_buttons = "".join(f'<div class="btn">广告 {index}</div>' for index in range(120))
    html = f"""
    <style>
      .app-list-pop {{ position: fixed; inset: 0; z-index: 2003; }}
      .btn {{ width: 60px; height: 20px; }}
      .close_btn {{
        position: fixed;
        left: 171px;
        bottom: 100px;
        width: 48px;
        height: 48px;
        cursor: pointer;
      }}
      .van-icon-close {{
        display: block;
        width: 40px;
        height: 40px;
        pointer-events: none;
      }}
    </style>
    <div role="dialog" class="van-popup app-list-pop">
      {ad_buttons}
      <div class="close_btn" onclick="this.closest('[role=dialog]').style.display='none'">
        <i class="van-icon van-icon-close"></i>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            action_trace: list[dict[str, Any]] = []
            await _run_single_trigger(
                page=page,
                context=object(),
                request=PageCaptureRequest(
                    mode=PageCaptureMode.SINGLE_TRIGGER,
                    selector=page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
                    trigger_action="click",
                    allow_high_z_obstruction_hide=True,
                ),
                action_trace=action_trace,
                timeout_ms=2_000,
                timeout_error_type=PlaywrightTimeoutError,
            )
            dialog_visible = await page.locator('[role="dialog"]').is_visible()
            await browser.close()
    except PageInteractionFailedError:
        raise
    except Exception as exc:  # noqa: BLE001 - 仅浏览器二进制缺失可跳过
        if "Executable doesn't exist" in str(exc):
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise

    assert dialog_visible is False
    assert action_trace[0]["selector"] == "div.close_btn"
    assert action_trace[-1]["dismissal_verified"] is True


async def test_camel_case_popup_without_safe_exit_is_detected_as_blocked() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .page-content { min-height: 844px; }
      .goldPopup {
        position: fixed;
        z-index: 1900;
        left: 20px;
        right: 20px;
        top: 250px;
        min-height: 220px;
        background: white;
      }
    </style>
    <main class="page-content">视频详情</main>
    <section class="goldPopup">
      <div class="title">试看结束</div>
      <div class="btnBox">
        <div class="btn">80金币解锁</div>
        <div class="btn primary">立即开通免费观看</div>
      </div>
    </section>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            target = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector="text=关闭",
                prefer_safe_outside=True,
            )
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 仅浏览器二进制缺失可跳过
        if "Executable doesn't exist" in str(exc):
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise

    assert target is not None
    assert target["dialog_present"] is True
    assert target["kind"] == "blocked"
    assert target["selector"] is None


async def test_small_floating_widget_is_not_a_popup_chain_layer() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .page-content { min-height: 844px; }
      .home-suspend {
        position: fixed;
        z-index: 2200;
        right: 12px;
        bottom: 120px;
        width: 62px;
        height: 78px;
      }
    </style>
    <main class="page-content">可正常浏览的首页</main>
    <aside class="home-suspend"><img alt="活动入口"></aside>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            target = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector="text=关闭",
                prefer_safe_outside=True,
            )
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 仅浏览器二进制缺失可跳过
        if "Executable doesn't exist" in str(exc):
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise

    assert target == {"dialog_present": False}


async def test_small_camel_case_float_popup_is_not_a_popup_chain_layer() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .page-content { min-height: 844px; }
      .floatPopup {
        position: fixed;
        z-index: 2200;
        right: 12px;
        bottom: 120px;
        width: 90px;
        height: 90px;
      }
    </style>
    <main class="page-content">可正常浏览的首页</main>
    <aside class="floatPopup"><img alt="活动入口"></aside>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            target = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector="text=关闭",
                prefer_safe_outside=True,
            )
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 仅浏览器二进制缺失可跳过
        if "Executable doesn't exist" in str(exc):
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        raise

    assert target == {"dialog_present": False}


async def test_signed_popup_reobserves_when_live_target_temporarily_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来源快照已验签弹窗若始终不重现，仍不得退化成普通 selector 点击。"""
    resolve_calls = 0

    async def resolve(*, page: Any, selector: str) -> None:
        nonlocal resolve_calls
        del page
        assert selector == page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR
        resolve_calls += 1
        return None

    class PopupPage:
        def __init__(self) -> None:
            self.waits: list[int] = []

        async def wait_for_timeout(self, timeout: int) -> None:
            self.waits.append(timeout)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"不得点击失去实时结构证明的弹窗目标：{selector=}, {timeout=}")

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    action_trace: list[dict[str, Any]] = []
    page = PopupPage()

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
                trigger_action="click",
                allow_high_z_obstruction_hide=True,
            ),
            action_trace=action_trace,
            timeout_ms=5_000,
        )

    assert error.value.error_code == "POPUP_DISMISSAL_NOT_FOUND"
    assert action_trace[-1]["result"] == "popup_dismissal_target_not_proven"
    assert resolve_calls == 9
    assert page.waits == [250] * 8


async def test_signed_popup_reacquires_delayed_live_structure_before_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已验签的高层弹窗可短时重获，但仍只点击重新证明的关闭控件。"""
    resolve_calls = 0

    async def resolve(*, page: Any, selector: str) -> dict[str, Any] | None:
        nonlocal resolve_calls
        del page
        assert selector == page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR
        resolve_calls += 1
        if resolve_calls == 1:
            return None
        return {
            "dialog_present": True,
            "kind": "close_control",
            "selector": "#verified-popup-close",
            "verification_required": False,
        }

    class CloseLocator:
        def __init__(self) -> None:
            self.clicks: list[int] = []

        async def click(self, *, timeout: int) -> None:
            self.clicks.append(timeout)

    class PopupPage:
        def __init__(self) -> None:
            self.waits: list[int] = []
            self.close = CloseLocator()

        async def wait_for_timeout(self, timeout: int) -> None:
            self.waits.append(timeout)

        def locator(self, selector: str) -> CloseLocator:
            assert selector == "#verified-popup-close"
            return self.close

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"不得点击原始含糊目标：{selector=}, {timeout=}")

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
            trigger_action="click",
            allow_high_z_obstruction_hide=True,
        ),
        action_trace=action_trace,
        timeout_ms=5_000,
    )

    assert resolve_calls == 2
    assert page.waits == [250]
    assert page.close.clicks == [5_000]
    assert action_trace[-1]["popup_structure_reacquired"] is True
    assert action_trace[-1]["popup_structure_reacquire_attempts"] == 1
    assert action_trace[-1]["selector"] == "#verified-popup-close"


async def test_signed_popup_retries_blocked_structure_until_safe_overlay_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dialog 已出现但尚无安全出口时，也必须在预算内继续重获。"""
    resolve_calls = 0

    async def resolve(*, page: Any, selector: str) -> dict[str, Any]:
        nonlocal resolve_calls
        del page
        assert selector == page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR
        resolve_calls += 1
        if resolve_calls < 3:
            return {
                "dialog_present": True,
                "kind": "blocked",
                "selector": None,
                "verification_required": True,
            }
        return {
            "dialog_present": True,
            "kind": "overlay",
            "selector": ".verified-overlay",
            "position": {"x": 2, "y": 2},
            "verification_required": False,
        }

    class OverlayLocator:
        def __init__(self) -> None:
            self.clicks: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.clicks.append(kwargs)

    class PopupPage:
        def __init__(self) -> None:
            self.waits: list[int] = []
            self.overlay = OverlayLocator()

        async def wait_for_timeout(self, timeout: int) -> None:
            self.waits.append(timeout)

        def locator(self, selector: str) -> OverlayLocator:
            assert selector == ".verified-overlay"
            return self.overlay

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
            trigger_action="click",
            allow_high_z_obstruction_hide=True,
        ),
        action_trace=action_trace,
        timeout_ms=5_000,
    )

    assert resolve_calls == 3
    assert page.waits == [250, 250]
    assert page.overlay.clicks == [{"position": {"x": 2, "y": 2}, "timeout": 5_000}]
    assert action_trace[-1]["popup_structure_reacquired"] is True
    assert action_trace[-1]["popup_structure_reacquire_attempts"] == 2
    assert action_trace[-1]["dismissal_kind"] == "overlay"


async def test_popup_dismissal_blocks_when_live_dialog_has_no_close_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实时顶层弹窗没有可证明出口时必须重观察，不能静默记为动作成功。"""

    async def resolve(*, page: Any, selector: str) -> dict[str, Any]:
        del page, selector
        return {
            "dialog_present": True,
            "kind": "blocked",
            "selector": None,
            "verification_required": True,
        }

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    action_trace: list[dict[str, Any]] = []

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=object(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
                trigger_action="click",
            ),
            action_trace=action_trace,
            timeout_ms=5_000,
        )

    assert error.value.error_code == "POPUP_DISMISSAL_NOT_FOUND"
    assert action_trace[-1]["result"] == "popup_dismissal_target_not_proven"


async def test_overlay_selector_is_resolved_to_visible_dialog_close_control() -> None:
    class CloseLocator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    class PopupPage:
        def __init__(self) -> None:
            self.locators: dict[str, CloseLocator] = {}

        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert ".btn-box" in script
            assert args == {
                "requestedSelector": ".van-overlay:visible",
                "selector": ".van-overlay:visible",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": "#ad-close",
            }

        def locator(self, selector: str) -> CloseLocator:
            return self.locators.setdefault(selector, CloseLocator())

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"遮罩应先解析到真实关闭控件：{selector=}, {timeout=}")

    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.locators["#ad-close"].calls == [{"timeout": 30000}]
    assert action_trace == [
        {
            "action": "click",
            "requested_selector": ".van-overlay:visible",
            "selector": "#ad-close",
            "resolved_by": "visible_popup_structure",
            "dismissal_kind": "close_control",
        }
    ]


async def test_offscreen_popup_close_control_uses_verified_direct_dispatch() -> None:
    class CloseLocator:
        def __init__(self) -> None:
            self.click_calls: list[dict[str, Any]] = []
            self.scroll_calls: list[dict[str, Any]] = []
            self.evaluate_calls: list[str] = []

        async def click(self, **kwargs: Any) -> None:
            self.click_calls.append(kwargs)

        async def bounding_box(self) -> dict[str, int]:
            return {"x": 450, "y": 1200, "width": 100, "height": 100}

        async def scroll_into_view_if_needed(self, **kwargs: Any) -> None:
            self.scroll_calls.append(kwargs)
            raise TimeoutError("element is outside of the viewport")

        async def evaluate(self, script: str) -> None:
            self.evaluate_calls.append(script)

    class PopupPage:
        viewport_size = {"width": 1280, "height": 720}

        def __init__(self) -> None:
            self.close = CloseLocator()

        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert ".btn-box" in script
            assert args == {
                "requestedSelector": "text=关闭",
                "selector": "text=关闭",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": "#offscreen-close",
                "dialog_selector": "#ad-dialog",
                "dialog_signature": "dialog-signature",
                "verification_required": True,
            }

        def locator(self, selector: str) -> CloseLocator:
            assert selector == "#offscreen-close"
            return self.close

        async def wait_for_function(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.close.click_calls == []
    assert page.close.scroll_calls == [{"timeout": 5000}]
    assert page.close.evaluate_calls == ["(element) => element.click()"]
    assert action_trace[0]["resolved_by"] == (
        "popup_close_direct_dispatch_after_actionability_timeout"
    )
    assert action_trace[0]["dismissal_verified"] is True


async def test_onscreen_popup_close_control_uses_direct_dispatch_after_timeout() -> None:
    """顶层弹窗关闭控件已验签但原生点击超时时，应受限派发元素点击。"""

    class CloseLocator:
        def __init__(self) -> None:
            self.click_calls: list[dict[str, Any]] = []
            self.evaluate_calls: list[str] = []

        async def click(self, **kwargs: Any) -> None:
            self.click_calls.append(kwargs)
            raise TimeoutError("另一元素截获了指针事件")

        async def bounding_box(self) -> dict[str, int]:
            return {"x": 171, "y": 696, "width": 48, "height": 48}

        async def evaluate(self, script: str) -> None:
            self.evaluate_calls.append(script)

    class PopupPage:
        viewport_size = {"width": 390, "height": 844}

        def __init__(self) -> None:
            self.close = CloseLocator()

        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert ".close_btn" in script
            assert args == {
                "requestedSelector": "text=关闭",
                "selector": "text=关闭",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": "#onscreen-close",
                "dialog_selector": "#ad-dialog",
                "dialog_signature": "dialog-signature",
                "verification_required": True,
            }

        def locator(self, selector: str) -> CloseLocator:
            assert selector == "#onscreen-close"
            return self.close

        async def wait_for_function(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert page.close.click_calls == [{"timeout": 30_000}]
    assert page.close.evaluate_calls == ["(element) => element.click()"]
    assert action_trace[0]["resolved_by"] == (
        "popup_close_direct_dispatch_after_actionability_timeout"
    )
    assert action_trace[0]["dismissal_verified"] is True


async def test_popup_close_is_blocked_when_original_dialog_remains_visible() -> None:
    class MissingLoadingLayer:
        async def count(self) -> int:
            return 0

    class CloseLocator:
        async def click(self, **kwargs: Any) -> None:
            assert kwargs == {"timeout": 30000}

    class PopupPage:
        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            del script
            assert args == {
                "requestedSelector": ".van-overlay:visible",
                "selector": ".van-overlay:visible",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": ".btn-box.end",
                "dialog_selector": ".van-dialog",
                "dialog_signature": "same-dialog",
                "verification_required": True,
            }

        def locator(self, selector: str) -> CloseLocator:
            if selector == ".loading-page:visible":
                return MissingLoadingLayer()  # type: ignore[return-value]
            assert selector == ".btn-box.end"
            return CloseLocator()

        async def wait_for_function(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs
            raise TimeoutError

    action_trace: list[dict[str, Any]] = []

    with pytest.raises(
        BlockedPageCaptureActionError,
        match="原弹窗仍然可见",
    ) as exc_info:
        await _run_single_trigger(
            page=PopupPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=".van-overlay:visible",
                trigger_action="click",
            ),
            action_trace=action_trace,
            timeout_ms=30000,
        )

    assert exc_info.value.error_code == "POPUP_DISMISSAL_NOT_OBSERVED"
    assert action_trace[0]["selector"] == ".btn-box.end"
    assert action_trace[0]["dismissal_verified"] is False
    assert action_trace[0]["result"] == "popup_dismissal_not_observed"


async def test_popup_close_uses_verified_high_z_hide_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MissingLoadingLayer:
        async def count(self) -> int:
            return 0

    class CloseLocator:
        async def click(self, **kwargs: Any) -> None:
            assert kwargs == {"timeout": 30000}

    class PopupPage:
        def locator(self, selector: str) -> CloseLocator | MissingLoadingLayer:
            if selector == ".loading-page:visible":
                return MissingLoadingLayer()
            assert selector == ".btn-box.end"
            return CloseLocator()

    async def resolve(*, page: Any, selector: str) -> dict[str, Any]:
        del page
        assert selector == ".van-overlay:visible"
        return {
            "dialog_present": True,
            "kind": "close_control",
            "selector": ".btn-box.end",
            "dialog_selector": ".van-dialog",
            "dialog_signature": "same-dialog",
            "verification_required": True,
        }

    verification_results = iter((False, True))

    async def verify(**kwargs: Any) -> bool:
        del kwargs
        return next(verification_results)

    async def hide(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["popup_target"]["dialog_signature"] == "same-dialog"
        return {"hidden": True, "hidden_count": 2, "max_z_index": 2001}

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    monkeypatch.setattr(page_capture_module, "_wait_for_popup_dismissal", verify)
    monkeypatch.setattr(page_capture_module, "_hide_verified_high_z_popup_obstruction", hide)
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=PopupPage(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
            allow_high_z_obstruction_hide=True,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert action_trace[0]["fallback"] == "verified_high_z_obstruction_hide"
    assert action_trace[0]["fallback_hidden_count"] == 2
    assert action_trace[0]["dismissal_verified"] is True


async def test_h5_popup_uses_visible_close_after_safe_overlay_without_dom_hide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clicked: list[tuple[str, dict[str, Any]]] = []

    class MissingLoadingLayer:
        async def count(self) -> int:
            return 0

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def click(self, **kwargs: Any) -> None:
            clicked.append((self.selector, kwargs))

    class PopupPage:
        def locator(self, selector: str) -> Locator | MissingLoadingLayer:
            if selector == ".loading-page:visible":
                return MissingLoadingLayer()
            return Locator(selector)

    async def resolve(
        *,
        page: Any,
        selector: str,
        prefer_safe_outside: bool = False,
    ) -> dict[str, Any]:
        del page
        assert selector == ".van-overlay:visible"
        assert prefer_safe_outside is True
        return {
            "dialog_present": True,
            "kind": "overlay",
            "selector": ".van-overlay",
            "position": {"x": 2, "y": 2},
            "dialog_selector": ".van-dialog",
            "dialog_signature": "same-dialog",
            "verification_required": True,
            "fallback_close_target": {
                "kind": "close_control",
                "selector": ".btn-close",
            },
        }

    verification_results = iter((False, True))

    async def verify(**kwargs: Any) -> bool:
        del kwargs
        return next(verification_results)

    async def hide(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"H5 弹窗关闭不得修改页面 DOM：{kwargs}")

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    monkeypatch.setattr(page_capture_module, "_wait_for_popup_dismissal", verify)
    monkeypatch.setattr(page_capture_module, "_hide_verified_high_z_popup_obstruction", hide)
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=PopupPage(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
            allow_high_z_obstruction_hide=True,
            h5_popup_dismissal_strategy="safe_outside_then_close",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert clicked == [
        (".van-overlay", {"position": {"x": 2, "y": 2}, "timeout": 30000}),
        (".btn-close", {"timeout": 30000}),
    ]
    assert action_trace[0]["fallback"] == "visible_close_control"
    assert action_trace[0]["dismissal_verified"] is True


async def test_h5_popup_tries_bounded_safe_outside_points_until_layer_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clicked: list[dict[str, Any]] = []

    class MissingLoadingLayer:
        async def count(self) -> int:
            return 0

    class Locator:
        async def click(self, **kwargs: Any) -> None:
            clicked.append(kwargs)

    class PopupPage:
        def locator(self, selector: str) -> Locator | MissingLoadingLayer:
            if selector == ".loading-page:visible":
                return MissingLoadingLayer()
            assert selector == ".van-overlay"
            return Locator()

    async def resolve(**kwargs: Any) -> dict[str, Any]:
        assert kwargs["prefer_safe_outside"] is True
        return {
            "dialog_present": True,
            "kind": "overlay",
            "selector": ".van-overlay",
            "position": {"x": 2, "y": 2},
            "positions": [
                {"x": 2, "y": 2},
                {"x": 388, "y": 2},
                {"x": 2, "y": 842},
                {"x": 388, "y": 842},
                {"x": 195, "y": 42},
            ],
            "dialog_selector": ".van-dialog",
            "dialog_signature": "first-layer",
            "verification_required": True,
        }

    verification_results = iter((False, False, True))

    async def verify(**kwargs: Any) -> bool:
        del kwargs
        return next(verification_results)

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    monkeypatch.setattr(page_capture_module, "_wait_for_popup_dismissal", verify)
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=PopupPage(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
            allow_high_z_obstruction_hide=True,
            h5_popup_dismissal_strategy="safe_outside_then_close",
        ),
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert clicked == [
        {"position": {"x": 2, "y": 2}, "timeout": 30_000},
        {"position": {"x": 388, "y": 2}, "timeout": 30_000},
        {"position": {"x": 2, "y": 842}, "timeout": 30_000},
    ]
    assert action_trace[0]["safe_outside_attempts"] == [
        {"position": {"x": 2, "y": 2}, "dismissal_verified": False},
        {"position": {"x": 388, "y": 2}, "dismissal_verified": False},
        {"position": {"x": 2, "y": 842}, "dismissal_verified": True},
    ]
    assert action_trace[0]["dismissal_verified"] is True


async def test_h5_popup_chain_rejects_capture_when_next_layer_has_no_safe_exit() -> None:
    metrics = {
        "enabled": True,
        "dismissal_count": 1,
        "outcome": "llm_fallback_required",
        "remaining_obstruction": True,
        "llm_fallback_required": True,
    }
    action_trace = [{"action": "click", "dismissal_verified": True}]

    with pytest.raises(BlockedPageCaptureActionError) as error:
        page_capture_module._require_h5_obstruction_chain_cleared(
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=".van-overlay:visible",
                trigger_action="click",
                clear_obstructions=True,
                h5_popup_dismissal_strategy="safe_outside_then_close",
            ),
            metrics=metrics,
            action_trace=action_trace,
        )

    assert error.value.error_code == "H5_POPUP_CHAIN_REMAINS_BLOCKED"
    assert action_trace[-1]["result"] == "h5_popup_chain_remains_blocked"


def test_non_h5_capture_keeps_existing_obstruction_chain_contract() -> None:
    page_capture_module._require_h5_obstruction_chain_cleared(
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
            clear_obstructions=True,
        ),
        metrics={"remaining_obstruction": True},
        action_trace=[],
    )


def test_h5_main_exploration_keeps_next_popup_layer_for_frontier() -> None:
    action_trace = [{"action": "click", "dismissal_verified": True}]

    page_capture_module._require_h5_obstruction_chain_cleared(
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
            clear_obstructions=True,
            h5_popup_dismissal_strategy="safe_outside_then_close",
            allow_h5_popup_layer_continuation=True,
        ),
        metrics={
            "remaining_obstruction": True,
            "outcome": "llm_fallback_required",
        },
        action_trace=action_trace,
    )

    assert action_trace == [{"action": "click", "dismissal_verified": True}]


async def test_verified_high_z_popup_closes_before_waiting_for_startup_layer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LoadingLayer:
        async def count(self) -> int:
            return 1

        async def wait_for(self, **kwargs: Any) -> None:
            raise AssertionError(f"已验签高层弹窗不得等待底层开屏壳：{kwargs}")

    class CloseLocator:
        def __init__(self) -> None:
            self.click_calls: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.click_calls.append(kwargs)

    class PopupPage:
        viewport_size = {"width": 390, "height": 844}

        def __init__(self) -> None:
            self.loading_layer = LoadingLayer()
            self.close = CloseLocator()

        def locator(self, selector: str) -> LoadingLayer | CloseLocator:
            if selector == ".loading-page:visible":
                return self.loading_layer
            assert selector == ".van-dialog__confirm"
            return self.close

    async def resolve(*, page: Any, selector: str) -> dict[str, Any]:
        del page
        assert selector == ".van-overlay:visible"
        return {
            "dialog_present": True,
            "kind": "close_control",
            "selector": ".van-dialog__confirm",
            "dialog_selector": ".van-dialog",
            "dialog_signature": "verified-high-z-dialog",
            "verification_required": True,
        }

    async def verify(**kwargs: Any) -> bool:
        del kwargs
        return True

    monkeypatch.setattr(page_capture_module, "_resolve_popup_dismissal_target", resolve)
    monkeypatch.setattr(page_capture_module, "_wait_for_popup_dismissal", verify)
    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
            allow_high_z_obstruction_hide=True,
        ),
        action_trace=action_trace,
        timeout_ms=3_500_000,
    )

    assert page.close.click_calls == [{"timeout": 3_500_000}]
    assert action_trace[0]["selector"] == ".van-dialog__confirm"
    assert action_trace[0]["dismissal_verified"] is True
    assert "waited_for" not in action_trace[0]


async def test_high_z_hide_fallback_requires_verified_dialog_and_threshold() -> None:
    class PopupPage:
        async def evaluate(self, script: str, parameters: dict[str, Any]) -> dict[str, Any]:
            assert "signatureFor(dialog) !== signature" in script
            assert "maxZIndex < minZIndex" in script
            assert parameters == {
                "selector": ".van-dialog",
                "signature": "dialog-signature",
                "minZIndex": 500,
            }
            return {"hidden": True, "hidden_count": 2, "max_z_index": 2001}

    result = await page_capture_module._hide_verified_high_z_popup_obstruction(
        page=PopupPage(),
        popup_target={
            "dialog_selector": ".van-dialog",
            "dialog_signature": "dialog-signature",
        },
    )

    assert result == {"hidden": True, "hidden_count": 2, "max_z_index": 2001}


async def test_popup_without_close_control_clicks_proven_overlay_outside_dialog() -> None:
    class OverlayLocator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    class PopupPage:
        def __init__(self) -> None:
            self.overlay = OverlayLocator()

        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert "elementFromPoint" in script
            assert args == {
                "requestedSelector": "text=关闭",
                "selector": "text=关闭",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            return {
                "dialog_present": True,
                "kind": "overlay",
                "selector": "#dialog-overlay",
                "position": {"x": 2, "y": 2},
            }

        def locator(self, selector: str) -> OverlayLocator:
            assert selector == "#dialog-overlay"
            return self.overlay

    page = PopupPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=关闭",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.overlay.calls == [{"position": {"x": 2, "y": 2}, "timeout": 30000}]
    assert action_trace[0]["dismissal_kind"] == "overlay"
    assert action_trace[0]["selector"] == "#dialog-overlay"
    assert action_trace[0]["position"] == {"x": 2, "y": 2}


async def test_ambiguous_text_click_uses_unique_interactive_control() -> None:
    class TargetLocator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    class InteractivePage:
        def __init__(self) -> None:
            self.target = TargetLocator()

        async def evaluate(self, script: str, value: str) -> dict[str, Any]:
            if "dialog_present" in script:
                return {"dialog_present": False}
            assert value == "收藏"
            return {
                "matched": True,
                "selector": "#favorite",
                "actionable": True,
            }

        def locator(self, selector: str) -> TargetLocator:
            assert selector == "#favorite"
            return self.target

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"含糊文本不得交给 Playwright 子串选择器：{selector=}, {timeout=}")

    page = InteractivePage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="text=收藏",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.target.calls == [{"timeout": 30000}]
    assert action_trace == [
        {
            "action": "click",
            "requested_selector": "text=收藏",
            "selector": "#favorite",
            "resolved_by": "exact_visible_interactive_text",
        }
    ]


async def test_exact_text_click_reports_structured_obstruction() -> None:
    class ObstructedPage:
        async def evaluate(self, script: str, value: str) -> dict[str, Any]:
            if "dialog_present" in script:
                return {"dialog_present": False}
            assert value == "设置"
            return {
                "matched": True,
                "selector": "#settings",
                "actionable": False,
                "obstruction": {
                    "tag": "img",
                    "role": None,
                    "class_tokens": ["floating-ad"],
                },
            }

    action_trace: list[dict[str, Any]] = []

    with pytest.raises(PageInteractionFailedError) as exc_info:
        await _run_single_trigger(
            page=ObstructedPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector="text=设置",
                trigger_action="click",
            ),
            action_trace=action_trace,
            timeout_ms=30000,
        )

    assert exc_info.value.error_code == "CLICK_TARGET_OBSTRUCTED"
    assert exc_info.value.retryable is True
    assert action_trace[0]["result"] == "interactive_target_obstructed"
    assert action_trace[0]["obstruction"]["class_tokens"] == ["floating-ad"]


async def test_generic_click_timeout_distinguishes_missing_target() -> None:
    class MissingLocator:
        async def count(self) -> int:
            return 0

        def nth(self, index: int) -> "MissingLocator":
            del index
            return self

    class MissingPage:
        async def evaluate(self, script: str, value: str) -> dict[str, Any]:
            del script, value
            return {"dialog_present": False}

        def locator(self, selector: str) -> MissingLocator:
            assert selector == ".tabs .tab:nth-of-type(12):visible"
            return MissingLocator()

        async def click(self, selector: str, *, timeout: int) -> None:
            assert selector == ".tabs .tab:nth-of-type(12):visible"
            assert timeout == 30000
            raise TimeoutError

    action_trace: list[dict[str, Any]] = []

    with pytest.raises(PageInteractionFailedError) as exc_info:
        await _run_single_trigger(
            page=MissingPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=".tabs .tab:nth-of-type(12):visible",
                trigger_action="click",
            ),
            action_trace=action_trace,
            timeout_ms=30000,
        )

    assert exc_info.value.error_code == "CLICK_TARGET_NOT_FOUND"
    assert action_trace[0]["result"] == "click_target_not_found"
    assert action_trace[0]["click_diagnostics"] == {
        "match_count": 0,
        "visible_count": 0,
    }


async def test_ambiguous_selector_click_prefers_viewport_intersecting_match() -> None:
    class CandidateLocator:
        def __init__(
            self,
            page: "MultiMatchPage",
            *,
            index: int | None = None,
        ) -> None:
            self.page = page
            self.index = index

        async def count(self) -> int:
            return len(self.page.boxes)

        def nth(self, index: int) -> "CandidateLocator":
            return CandidateLocator(self.page, index=index)

        async def bounding_box(self) -> dict[str, int]:
            assert self.index is not None
            return self.page.boxes[self.index]

        async def click(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.clicked.append({"index": self.index, **kwargs})

    class MultiMatchPage:
        viewport_size = {"width": 390, "height": 720}

        def __init__(self) -> None:
            self.boxes = [
                {"x": 20, "y": -260, "width": 160, "height": 120},
                {"x": 24, "y": 180, "width": 160, "height": 120},
            ]
            self.clicked: list[dict[str, Any]] = []

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            assert args == (".video-section .layout5 .img-box:visible",)
            assert "dialog_present" in script
            return {"dialog_present": False}

        def locator(self, selector: str) -> CandidateLocator:
            assert selector == ".video-section .layout5 .img-box:visible"
            return CandidateLocator(self)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"多匹配 selector 不应直接点击首项：{selector=}, {timeout=}")

    page = MultiMatchPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".video-section .layout5 .img-box:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.clicked == [{"index": 1, "timeout": 5000}]
    assert action_trace[0]["resolved_by"] == "first_viewport_intersecting_match"
    assert action_trace[0]["match_index"] == 1
    assert action_trace[0]["match_count"] == 2


async def test_ambiguous_selector_click_uses_expected_rect_geometry_match() -> None:
    """带解析矩形时应点几何最吻合的命中，而非视口内第一个容器级命中。

    真实回归：/movie 标题的位置 selector 同时命中广告容器(nth0,进视口)与
    卡片标题(nth2)，索引序点中容器导致零路由（任务 17d6cec4 seq26）。
    """

    class CandidateLocator:
        def __init__(self, page: "AmbiguousPage", *, index: int | None = None) -> None:
            self.page = page
            self.index = index

        async def count(self) -> int:
            return len(self.page.boxes)

        def nth(self, index: int) -> "CandidateLocator":
            return CandidateLocator(self.page, index=index)

        async def bounding_box(self) -> dict[str, float]:
            assert self.index is not None
            return self.page.boxes[self.index]

        async def click(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.clicked.append({"index": self.index, **kwargs})

    class AmbiguousPage:
        viewport_size = {"width": 390, "height": 844}

        def __init__(self) -> None:
            self.boxes = [
                # nth0：广告容器，占满首屏（索引序会先点它）
                {"x": 0, "y": 98, "width": 390, "height": 696},
                # nth1：卡片标题（解析目标）
                {"x": 16, "y": 685, "width": 358, "height": 20},
            ]
            self.clicked: list[dict[str, Any]] = []

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            return {"dialog_present": False}

        def locator(self, selector: str) -> CandidateLocator:
            return CandidateLocator(self)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError("多匹配 selector 不应直接点击首项")

    page = AmbiguousPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="div:nth-of-type(1) > div > div:nth-of-type(2)",
            trigger_action="click",
            expected_rect={"x": 16.0, "y": 685.44, "width": 358.0, "height": 19.5},
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert [item["index"] for item in page.clicked] == [1]
    assert action_trace[0]["resolved_by"] == "expected_rect_geometry_match"
    assert action_trace[0]["match_index"] == 1


async def test_verified_post_title_activates_owning_card_on_viewport_match() -> None:
    class CandidateLocator:
        def __init__(
            self,
            page: "MovingPostListPage",
            *,
            index: int | None = None,
        ) -> None:
            self.page = page
            self.index = index

        async def count(self) -> int:
            return len(self.page.boxes)

        def nth(self, index: int) -> "CandidateLocator":
            return CandidateLocator(self.page, index=index)

        async def bounding_box(self) -> dict[str, int]:
            assert self.index is not None
            return self.page.boxes[self.index]

        async def click(self, **kwargs: Any) -> None:
            del kwargs
            self.page.url = "https://example.test/homepage?userId=author-1"

        async def dispatch_event(self, event: str) -> None:
            assert self.index is not None
            self.page.title_dispatches.append({"index": self.index, "event": event})
            self.page.url = "https://example.test/homepage?userId=author-1"

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            assert self.index is not None
            assert "closest('.dynamicData-box')" in script
            assert args == ("post_card",)
            self.page.card_activations.append(self.index)
            self.page.url = "https://example.test/squareDetail?mediaId=post-1"
            return {"activated": True}

    class MovingPostListPage:
        viewport_size = {"width": 390, "height": 844}

        def __init__(self) -> None:
            self.url = "https://example.test/square"
            self.boxes = [
                {"x": 12, "y": -180, "width": 366, "height": 20},
                {"x": 12, "y": 270, "width": 366, "height": 20},
            ]
            self.title_dispatches: list[dict[str, Any]] = []
            self.card_activations: list[int] = []

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            assert args == ("#snapshot-post-title",)
            assert "dialog_present" in script
            return {"dialog_present": False}

        def locator(self, selector: str) -> CandidateLocator:
            assert selector == "#snapshot-post-title"
            return CandidateLocator(self)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"验签标题不应退回坐标点击：{selector=}, {timeout=}")

    page = MovingPostListPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="#snapshot-post-title",
            trigger_action="click",
            verified_card_activation="post_card",
        ),
        action_trace=action_trace,
        timeout_ms=3_600_000,
    )

    assert page.url == "https://example.test/squareDetail?mediaId=post-1"
    assert page.title_dispatches == []
    assert page.card_activations == [1]
    assert action_trace[0]["resolved_by"] == (
        "first_viewport_intersecting_verified_post_card_click"
    )


async def test_ambiguous_selector_click_scrolls_nearest_match_when_all_offscreen() -> None:
    class CandidateLocator:
        def __init__(
            self,
            page: "OffscreenMultiMatchPage",
            index: int | None = None,
        ) -> None:
            self.page = page
            self.index = index

        async def count(self) -> int:
            return len(self.page.boxes)

        def nth(self, index: int) -> "CandidateLocator":
            return CandidateLocator(self.page, index=index)

        async def bounding_box(self) -> dict[str, int]:
            assert self.index is not None
            return self.page.boxes[self.index]

        async def scroll_into_view_if_needed(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.scrolled.append({"index": self.index, **kwargs})
            self.page.boxes[self.index]["y"] = 180

        async def click(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.clicked.append({"index": self.index, **kwargs})

    class OffscreenMultiMatchPage:
        viewport_size = {"width": 390, "height": 720}

        def __init__(self) -> None:
            self.boxes = [
                {"x": 24, "y": -520, "width": 160, "height": 120},
                {"x": 24, "y": 780, "width": 160, "height": 120},
            ]
            self.scrolled: list[dict[str, Any]] = []
            self.clicked: list[dict[str, Any]] = []

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            assert args == (".masonry-grid .movie-item .video-cover:visible",)
            assert "dialog_present" in script
            return {"dialog_present": False}

        def locator(self, selector: str) -> CandidateLocator:
            assert selector == ".masonry-grid .movie-item .video-cover:visible"
            return CandidateLocator(self)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"离屏多匹配 selector 不应回退首项：{selector=}, {timeout=}")

    page = OffscreenMultiMatchPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".masonry-grid .movie-item .video-cover:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.scrolled == [{"index": 1, "timeout": 5000}]
    assert page.clicked == [{"index": 1, "timeout": 5000}]
    assert action_trace[0]["resolved_by"] == "nearest_match_scrolled_into_view"
    assert action_trace[0]["match_index"] == 1
    assert action_trace[0]["match_count"] == 2


async def test_ambiguous_selector_waits_for_dynamic_matches_before_resolving() -> None:
    class CandidateLocator:
        def __init__(
            self,
            page: "DynamicMultiMatchPage",
            index: int | None = None,
        ) -> None:
            self.page = page
            self.index = index

        async def count(self) -> int:
            return len(self.page.boxes) if self.page.ready else 0

        @property
        def first(self) -> "CandidateLocator":
            return CandidateLocator(self.page, index=0)

        async def wait_for(self, **kwargs: Any) -> None:
            self.page.waited.append(kwargs)
            self.page.ready = True

        def nth(self, index: int) -> "CandidateLocator":
            return CandidateLocator(self.page, index=index)

        async def bounding_box(self) -> dict[str, int]:
            assert self.index is not None
            return self.page.boxes[self.index]

        async def scroll_into_view_if_needed(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.scrolled.append({"index": self.index, **kwargs})
            self.page.boxes[self.index]["y"] = 180

        async def click(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.clicked.append({"index": self.index, **kwargs})

    class DynamicMultiMatchPage:
        viewport_size = {"width": 390, "height": 720}

        def __init__(self) -> None:
            self.ready = False
            self.boxes = [
                {"x": 24, "y": -520, "width": 160, "height": 120},
                {"x": 24, "y": 780, "width": 160, "height": 120},
            ]
            self.waited: list[dict[str, Any]] = []
            self.scrolled: list[dict[str, Any]] = []
            self.clicked: list[dict[str, Any]] = []

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            assert args == (".masonry-grid .movie-item .video-cover:visible",)
            assert "dialog_present" in script
            return {"dialog_present": False}

        def locator(self, selector: str) -> CandidateLocator:
            assert selector == ".masonry-grid .movie-item .video-cover:visible"
            return CandidateLocator(self)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"动态多匹配 selector 不应回退首项：{selector=}, {timeout=}")

    page = DynamicMultiMatchPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".masonry-grid .movie-item .video-cover:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.waited == [{"state": "visible", "timeout": 3000}]
    assert page.scrolled == [{"index": 1, "timeout": 5000}]
    assert page.clicked == [{"index": 1, "timeout": 5000}]
    assert action_trace[0]["resolved_by"] == "nearest_match_scrolled_into_view"
    assert action_trace[0]["match_index"] == 1
    assert action_trace[0]["match_count"] == 2


async def test_ambiguous_selector_activates_carousel_tab_for_offscreen_match() -> None:
    class CandidateLocator:
        def __init__(
            self,
            page: "CarouselMultiMatchPage",
            index: int | None = None,
        ) -> None:
            self.page = page
            self.index = index

        async def count(self) -> int:
            return len(self.page.boxes)

        def nth(self, index: int) -> "CandidateLocator":
            return CandidateLocator(self.page, index=index)

        async def bounding_box(self) -> dict[str, int]:
            assert self.index is not None
            return self.page.boxes[self.index]

        async def scroll_into_view_if_needed(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.scrolled.append({"index": self.index, **kwargs})

        async def evaluate(self, script: str) -> dict[str, Any]:
            assert self.index is not None
            assert ".van-swipe-item" in script
            self.page.carousel_evaluations.append(self.index)
            for box in self.page.boxes:
                box["x"] += 366
            return {"dispatched": True, "slide_index": 0}

        async def click(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.clicked.append({"index": self.index, **kwargs})

    class CarouselMultiMatchPage:
        viewport_size = {"width": 390, "height": 844}

        def __init__(self) -> None:
            self.boxes = [
                {"x": -354, "y": 180, "width": 178, "height": 100},
                {"x": -166, "y": 180, "width": 178, "height": 100},
            ]
            self.scrolled: list[dict[str, Any]] = []
            self.carousel_evaluations: list[int] = []
            self.waited: list[int] = []
            self.clicked: list[dict[str, Any]] = []

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            assert args == (".masonry-grid .movie-item .video-cover:visible",)
            assert "dialog_present" in script
            return {"dialog_present": False}

        def locator(self, selector: str) -> CandidateLocator:
            assert selector == ".masonry-grid .movie-item .video-cover:visible"
            return CandidateLocator(self)

        async def wait_for_timeout(self, timeout: int) -> None:
            self.waited.append(timeout)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"轮播离屏 selector 不应回退首项：{selector=}, {timeout=}")

    page = CarouselMultiMatchPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".masonry-grid .movie-item .video-cover:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.scrolled == [{"index": 1, "timeout": 5000}]
    assert page.carousel_evaluations == [1]
    assert page.waited == [300]
    assert page.clicked == [{"index": 1, "timeout": 5000}]
    assert action_trace[0]["resolved_by"] == ("carousel_tab_activation_then_viewport_click")
    assert action_trace[0]["carousel_tab_index"] == 0
    assert action_trace[0]["match_index"] == 1
    assert action_trace[0]["match_count"] == 2


async def test_ambiguous_selector_click_tries_next_viewport_match_after_failure() -> None:
    class CandidateLocator:
        def __init__(
            self,
            page: "RecoveringMultiMatchPage",
            *,
            index: int | None = None,
        ) -> None:
            self.page = page
            self.index = index

        async def count(self) -> int:
            return len(self.page.boxes)

        def nth(self, index: int) -> "CandidateLocator":
            return CandidateLocator(self.page, index=index)

        async def bounding_box(self) -> dict[str, int]:
            assert self.index is not None
            return self.page.boxes[self.index]

        async def click(self, **kwargs: Any) -> None:
            assert self.index is not None
            self.page.click_attempts.append({"index": self.index, **kwargs})
            if self.index == 0:
                raise TimeoutError("element is outside of the viewport")

    class RecoveringMultiMatchPage:
        viewport_size = {"width": 390, "height": 720}

        def __init__(self) -> None:
            self.boxes = [
                {"x": 24, "y": 20, "width": 160, "height": 120},
                {"x": 24, "y": 180, "width": 160, "height": 120},
            ]
            self.click_attempts: list[dict[str, Any]] = []

        async def evaluate(self, script: str, *args: Any) -> dict[str, Any]:
            assert args == (".video-section .layout5 .img-box:visible",)
            assert "dialog_present" in script
            return {"dialog_present": False}

        def locator(self, selector: str) -> CandidateLocator:
            assert selector == ".video-section .layout5 .img-box:visible"
            return CandidateLocator(self)

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"已找到视口候选时不应回退原始点击：{selector=}, {timeout=}")

    page = RecoveringMultiMatchPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".video-section .layout5 .img-box:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.click_attempts == [
        {"index": 0, "timeout": 5000},
        {"index": 1, "timeout": 5000},
    ]
    assert action_trace[0]["match_index"] == 1
    assert action_trace[0]["viewport_click_failures"] == 1


async def test_bottom_navigation_click_falls_back_when_visible_filter_misses() -> None:
    class BottomNavigationLocator:
        def __init__(self, *, count: int) -> None:
            self.match_count = count
            self.calls: list[dict[str, Any]] = []

        async def count(self) -> int:
            return self.match_count

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

        def locator(self, selector: str) -> "BottomNavigationLocator":
            assert selector == ".van-tabbar-item__text"
            return self

        async def dispatch_event(self, event: str) -> None:
            self.calls.append({"dispatch_event": event})

    class BottomNavigationPage:
        def __init__(self) -> None:
            self.visible = BottomNavigationLocator(count=0)
            self.unique_dom_item = BottomNavigationLocator(count=1)
            self.locator_calls: list[str] = []

        def locator(self, selector: str) -> BottomNavigationLocator:
            self.locator_calls.append(selector)
            if selector == ("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"):
                return self.visible
            return self.unique_dom_item

        async def click(self, selector: str, *, timeout: int) -> None:
            raise AssertionError(f"唯一底部导航不应回退到长等待：{selector=}, {timeout=}")

    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"
    page = BottomNavigationPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.locator_calls == [
        selector,
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5)",
    ]
    assert page.unique_dom_item.calls == [{"dispatch_event": "click"}]
    assert action_trace == [
        {
            "action": "click",
            "selector": selector,
            "fallback": "unique_bottom_navigation_text_dispatch",
            "fallback_reason": "visible_filter_miss",
            "fallback_selector": ("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5)"),
        }
    ]


async def test_bottom_navigation_click_keeps_normal_visible_actionability() -> None:
    class VisibleBottomNavigationLocator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def count(self) -> int:
            return 1

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    class VisibleBottomNavigationPage:
        def __init__(self) -> None:
            self.item = VisibleBottomNavigationLocator()

        def locator(self, selector: str) -> VisibleBottomNavigationLocator:
            return self.item

    page = VisibleBottomNavigationPage()

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"),
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=[],
        timeout_ms=30000,
    )

    assert page.item.calls == [{"timeout": 5000}]


async def test_bottom_navigation_click_falls_back_after_visible_actionability_timeout() -> None:
    class TimedOutVisibleLocator:
        async def count(self) -> int:
            return 1

        async def click(self, **kwargs: Any) -> None:
            assert kwargs == {"timeout": 5000}
            raise TimeoutError("visible actionability timeout")

    class UniqueBottomNavigationLocator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def count(self) -> int:
            return 1

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

        def locator(self, selector: str) -> "UniqueBottomNavigationLocator":
            assert selector == ".van-tabbar-item__text"
            return self

        async def dispatch_event(self, event: str) -> None:
            self.calls.append({"dispatch_event": event})

    class BottomNavigationPage:
        def __init__(self) -> None:
            self.visible = TimedOutVisibleLocator()
            self.unique_dom_item = UniqueBottomNavigationLocator()

        def locator(self, selector: str) -> TimedOutVisibleLocator | UniqueBottomNavigationLocator:
            if selector == ("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"):
                return self.visible
            return self.unique_dom_item

    selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"
    page = BottomNavigationPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.unique_dom_item.calls == [{"dispatch_event": "click"}]
    assert action_trace == [
        {
            "action": "click",
            "selector": selector,
            "fallback": "unique_bottom_navigation_text_dispatch",
            "fallback_reason": "visible_click_timeout",
            "fallback_selector": ("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5)"),
        }
    ]


async def test_bottom_navigation_accepts_url_change_while_native_click_is_pending() -> None:
    selector = "ul.pages-foot-fix:visible > li:nth-of-type(2):visible"

    class NavigatingPendingLocator:
        def __init__(self, page: "NavigatingPage") -> None:
            self.page = page
            self.cancelled = False

        async def count(self) -> int:
            return 1

        async def click(self, *, timeout: int) -> None:
            assert timeout == 5000
            self.page.url = "https://example.com/community"
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def get_attribute(self, _: str) -> str:
            return ""

    class NavigatingPage:
        def __init__(self) -> None:
            self.url = "https://example.com/?mainTab=video"
            self.locator_calls: list[str] = []
            self.item = NavigatingPendingLocator(self)

        def locator(self, requested_selector: str) -> NavigatingPendingLocator:
            self.locator_calls.append(requested_selector)
            if requested_selector != selector:
                raise AssertionError("URL 已变化后不应进入底栏回退分支")
            return self.item

    page = NavigatingPage()
    action_trace: list[dict[str, Any]] = []

    await asyncio.wait_for(
        _run_single_trigger(
            page=page,
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=selector,
                trigger_action="click",
                navigation_depth=1,
            ),
            action_trace=action_trace,
            timeout_ms=180000,
        ),
        timeout=1,
    )

    assert page.url == "https://example.com/community"
    assert page.locator_calls == [selector]
    assert page.item.cancelled is True
    assert action_trace[0]["resolved_by"] == "bottom_navigation_url_change_during_native_click"
    assert action_trace[0]["navigation_effect_verified"] is True
    assert action_trace[0]["navigation_effect_reason"] == "same_origin_url_change"


async def test_pages_foot_bottom_navigation_uses_bounded_click_and_unique_fallback() -> None:
    full_selector = "ul.pages-foot-fix:visible > li:nth-of-type(2):visible"
    parent_visible_selector = "ul.pages-foot-fix:visible > li:nth-of-type(2)"

    class TimedOutVisibleLocator:
        def __init__(self) -> None:
            self.click_timeouts: list[int] = []

        async def count(self) -> int:
            return 1

        async def click(self, *, timeout: int) -> None:
            self.click_timeouts.append(timeout)
            raise TimeoutError("pages_foot visible actionability timeout")

    class ChildLocator:
        async def count(self) -> int:
            return 0

    class UniquePagesFootLocator:
        def __init__(self, page: "PagesFootPage") -> None:
            self.page = page
            self.events: list[str] = []

        async def count(self) -> int:
            return 1

        def locator(self, _: str) -> ChildLocator:
            return ChildLocator()

        async def dispatch_event(self, event: str) -> None:
            self.events.append(event)

        async def get_attribute(self, _: str) -> str:
            return "community active"

    class EmptyLocator:
        async def count(self) -> int:
            return 0

    class PagesFootPage:
        def __init__(self) -> None:
            self.url = "https://example.com/?mainTab=video"
            self.visible = TimedOutVisibleLocator()
            self.unique = UniquePagesFootLocator(self)
            self.page_click_calls: list[tuple[str, int]] = []

        def locator(
            self,
            selector: str,
        ) -> TimedOutVisibleLocator | UniquePagesFootLocator | EmptyLocator:
            if selector == full_selector:
                return self.visible
            if selector == parent_visible_selector:
                return self.unique
            return EmptyLocator()

        async def click(self, selector: str, *, timeout: int) -> None:
            self.page_click_calls.append((selector, timeout))
            raise AssertionError("pages_foot 底栏不得进入普通长等待点击")

    page = PagesFootPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=full_selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.visible.click_timeouts == [5000]
    assert page.page_click_calls == []
    assert page.unique.events == ["click"]
    assert action_trace[0]["fallback_reason"] == "visible_click_timeout"
    assert action_trace[0]["fallback_selector"] == parent_visible_selector


async def test_main_tab_video_pages_foot_community_uses_scoped_exact_label_dom_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    full_selector = "ul.pages-foot-fix:visible > li:nth-of-type(2):visible"

    async def forbidden_generic_resolver(**_: Any) -> None:
        raise AssertionError("已验签 CSS 底栏不得进入通用弹窗或正文文本解析")

    monkeypatch.setattr(
        page_capture_module,
        "_resolve_popup_dismissal_target",
        forbidden_generic_resolver,
    )
    monkeypatch.setattr(
        page_capture_module,
        "_resolve_exact_interactive_text_target",
        forbidden_generic_resolver,
    )

    class ScopedCommunityLocator:
        def __init__(self) -> None:
            self.click_timeouts: list[int] = []

        async def count(self) -> int:
            return 1

        async def click(self, *, timeout: int) -> None:
            self.click_timeouts.append(timeout)
            raise TimeoutError("浏览器动作性误判")

    class MainTabVideoPage:
        def __init__(self) -> None:
            self.url = "https://example.com/?mainTab=video"
            self.community = ScopedCommunityLocator()
            self.locator_calls: list[str] = []
            self.evaluate_calls: list[str] = []
            self.community_active = False

        def locator(self, selector: str) -> ScopedCommunityLocator:
            self.locator_calls.append(selector)
            return self.community

        async def evaluate(self, expression: str, _: str) -> dict[str, Any]:
            self.evaluate_calls.append(expression)
            if "item.click()" in expression:
                self.community_active = True
                return {"match_count": 1, "clicked": True}
            return {
                "match_count": 1,
                "class_name": "community active" if self.community_active else "community",
            }

    page = MainTabVideoPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=full_selector,
            trigger_action="click",
            navigation_depth=1,
            navigation_family="pages_foot",
            navigation_label="社区",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.community.click_timeouts == [3000]
    assert page.locator_calls == [
        (
            "div.pages-foot-box:visible ul.pages-foot-fix:visible > "
            'li:has(> .pages-foot-label:text-is("社区")):visible'
        )
    ]
    assert len(page.evaluate_calls) == 2
    assert action_trace[0]["fallback"] == "scoped_pages_foot_dom_click"
    assert action_trace[0]["navigation_label"] == "社区"


async def test_main_tab_video_pages_foot_no_effect_uses_specific_block_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """社区底栏点击后 URL 与 active 均未变化时必须保留专用无效果诊断码。"""
    full_selector = "ul.pages-foot-fix:visible > li:nth-of-type(2):visible"

    async def no_effect(**_: Any) -> bool:
        return False

    monkeypatch.setattr(
        page_capture_module,
        "_pages_foot_community_effect_observed",
        no_effect,
    )

    class CommunityLocator:
        async def count(self) -> int:
            return 1

        async def click(self, **_: Any) -> None:
            return None

    class NoEffectCommunityPage:
        def __init__(self) -> None:
            self.url = "https://example.com/?mainTab=video"
            self.community = CommunityLocator()

        def locator(self, _: str) -> CommunityLocator:
            return self.community

        async def evaluate(self, *_: Any) -> dict[str, Any]:
            return {"match_count": 1, "clicked": True}

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=NoEffectCommunityPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=full_selector,
                trigger_action="click",
                navigation_depth=1,
                navigation_family="pages_foot",
                navigation_label="社区",
            ),
            action_trace=[],
            timeout_ms=1,
        )

    assert error.value.error_code == "BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED"


async def test_bottom_navigation_click_falls_back_when_native_click_has_no_effect() -> None:
    full_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible"
    parent_visible_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6)"

    class NoEffectVisibleLocator:
        def __init__(self) -> None:
            self.click_count = 0

        async def count(self) -> int:
            return 1

        async def click(self, **_: Any) -> None:
            self.click_count += 1

        async def get_attribute(self, _: str) -> str:
            return "van-tabbar-item"

    class FallbackLocator:
        def __init__(self, page: "NoEffectPage", selector: str) -> None:
            self.page = page
            self.selector = selector
            self.events: list[str] = []

        async def count(self) -> int:
            return 1 if self.selector == parent_visible_selector else 0

        def locator(self, selector: str) -> "FallbackLocator":
            assert selector == ".van-tabbar-item__text"
            return self

        async def dispatch_event(self, event: str) -> None:
            self.events.append(event)
            self.page.url = "https://example.com/mine"

        async def get_attribute(self, _: str) -> str:
            return "van-tabbar-item van-tabbar-item--active"

    class NoEffectPage:
        def __init__(self) -> None:
            self.url = "https://example.com/ai"
            self.visible = NoEffectVisibleLocator()
            self.locators: dict[str, FallbackLocator] = {}

        def locator(self, selector: str) -> NoEffectVisibleLocator | FallbackLocator:
            if selector == full_selector:
                return self.visible
            return self.locators.setdefault(selector, FallbackLocator(self, selector))

    page = NoEffectPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=full_selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=1,
    )

    assert page.visible.click_count == 1
    assert page.locators[parent_visible_selector].events == ["click"]
    assert action_trace[0]["fallback_reason"] == "visible_click_no_effect"
    assert action_trace[0]["fallback"] == "unique_bottom_navigation_text_dispatch"


async def test_bottom_navigation_dispatch_no_effect_uses_specific_block_code() -> None:
    """通用底栏所有受限事件目标都无效果时不得退化成通用请求拒绝。"""
    full_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible"

    class NoEffectLocator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        async def count(self) -> int:
            return 1

        async def click(self, **_: Any) -> None:
            return None

        async def get_attribute(self, _: str) -> str:
            return "van-tabbar-item"

        def locator(self, selector: str) -> "NoEffectLocator":
            return NoEffectLocator(selector)

        async def dispatch_event(self, _: str) -> None:
            return None

    class NoEffectPage:
        def __init__(self) -> None:
            self.url = "https://example.com/ai"

        def locator(self, selector: str) -> NoEffectLocator:
            return NoEffectLocator(selector)

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=NoEffectPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=full_selector,
                trigger_action="click",
                navigation_depth=1,
            ),
            action_trace=[],
            timeout_ms=1,
        )

    assert error.value.error_code == "BOTTOM_NAVIGATION_TRANSITION_NOT_OBSERVED"


async def test_adaptive_bottom_navigation_requires_unique_target_and_observed_effect() -> None:
    class AdaptiveLocator:
        def __init__(self, page: "AdaptivePage") -> None:
            self.page = page

        async def count(self) -> int:
            return 1

        async def click(self, **_: Any) -> None:
            self.page.selected = True

        async def get_attribute(self, name: str) -> str | None:
            if name == "class":
                return "opaque-item"
            if name == "aria-selected":
                return "true" if self.page.selected else "false"
            return None

    class AdaptivePage:
        def __init__(self) -> None:
            self.url = "https://example.com/"
            self.selected = False
            self.target = AdaptiveLocator(self)

        def locator(self, _: str) -> AdaptiveLocator:
            return self.target

    action_trace: list[dict[str, Any]] = []
    await _run_single_trigger(
        page=AdaptivePage(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector="#runtime-random-selector",
            trigger_action="click",
            navigation_family="adaptive_snapshot",
            navigation_index=2,
            navigation_label="短剧",
            interaction_pattern_signature="a" * 64,
        ),
        action_trace=action_trace,
        timeout_ms=10,
    )

    assert action_trace[0]["navigation_effect_verified"] is True
    assert action_trace[0]["navigation_effect_reason"] == "activation_state_change"
    assert action_trace[0]["activation_before_sha256"].startswith("sha256:")
    assert action_trace[0]["activation_after_sha256"].startswith("sha256:")
    assert (
        action_trace[0]["activation_before_sha256"]
        != action_trace[0]["activation_after_sha256"]
    )


async def test_adaptive_bottom_navigation_rejects_ambiguous_runtime_target() -> None:
    class AmbiguousLocator:
        async def count(self) -> int:
            return 2

    class AmbiguousPage:
        url = "https://example.com/"

        def locator(self, _: str) -> AmbiguousLocator:
            return AmbiguousLocator()

    with pytest.raises(PageInteractionFailedError) as error:
        await _run_single_trigger(
            page=AmbiguousPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=".runtime-selector",
                trigger_action="click",
                navigation_family="adaptive_snapshot",
                navigation_index=2,
                navigation_label="短剧",
                interaction_pattern_signature="a" * 64,
            ),
            action_trace=[],
            timeout_ms=10,
        )

    assert error.value.error_code == "SEMANTIC_TARGET_AMBIGUOUS"


async def test_bottom_navigation_dispatch_tries_child_target_when_text_has_no_effect() -> None:
    full_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible"
    parent_visible_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6)"

    class NoEffectVisibleLocator:
        def __init__(self) -> None:
            self.click_count = 0

        async def count(self) -> int:
            return 1

        async def click(self, **_: Any) -> None:
            self.click_count += 1

        async def get_attribute(self, _: str) -> str:
            return "van-tabbar-item"

    class DispatchChildLocator:
        def __init__(self, page: "ChildDispatchPage", selector: str) -> None:
            self.page = page
            self.selector = selector

        async def count(self) -> int:
            return 1

        async def dispatch_event(self, event: str) -> None:
            self.page.dispatch_events.append({"selector": self.selector, "event": event})
            if self.selector == ".van-tabbar-item__text > *":
                self.page.url = "https://example.com/mine"

    class FallbackLocator:
        def __init__(self, page: "ChildDispatchPage", selector: str) -> None:
            self.page = page
            self.selector = selector

        async def count(self) -> int:
            return 1 if self.selector == parent_visible_selector else 0

        def locator(self, selector: str) -> DispatchChildLocator:
            return DispatchChildLocator(self.page, selector)

        async def dispatch_event(self, event: str) -> None:
            self.page.dispatch_events.append({"selector": self.selector, "event": event})

        async def get_attribute(self, _: str) -> str:
            return "van-tabbar-item"

    class ChildDispatchPage:
        def __init__(self) -> None:
            self.url = "https://example.com/product"
            self.visible = NoEffectVisibleLocator()
            self.dispatch_events: list[dict[str, str]] = []
            self.locators: dict[str, FallbackLocator] = {}

        def locator(self, selector: str) -> NoEffectVisibleLocator | FallbackLocator:
            if selector == full_selector:
                return self.visible
            return self.locators.setdefault(selector, FallbackLocator(self, selector))

    page = ChildDispatchPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=full_selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=1,
    )

    assert page.visible.click_count == 1
    assert page.dispatch_events == [
        {"selector": ".van-tabbar-item__text", "event": "click"},
        {"selector": ".van-tabbar-item__text > *", "event": "click"},
    ]
    assert page.url == "https://example.com/mine"
    assert action_trace[0]["fallback_dispatch_attempts"] == [
        {"target": "text", "status": "no_effect", "count": 1},
        {"target": "text_child", "status": "success", "count": 1},
    ]


async def test_bottom_navigation_click_does_not_force_ambiguous_hidden_routes() -> None:
    class AmbiguousLocator:
        def __init__(self, count: int) -> None:
            self.match_count = count

        async def count(self) -> int:
            return self.match_count

        async def click(self, **_: Any) -> None:
            raise AssertionError("非唯一底部导航不得强制点击")

    class AmbiguousBottomNavigationPage:
        def locator(self, selector: str) -> AmbiguousLocator:
            if selector.endswith(":visible"):
                return AmbiguousLocator(0)
            if selector.startswith("div.van-tabbar:visible"):
                return AmbiguousLocator(0)
            return AmbiguousLocator(2)

    with pytest.raises(ValueError, match=r"candidate_counts=\[0, 2\]"):
        await _run_single_trigger(
            page=AmbiguousBottomNavigationPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=("div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"),
                trigger_action="click",
                navigation_depth=1,
            ),
            action_trace=[],
            timeout_ms=30000,
        )


async def test_bottom_navigation_click_recovers_from_delayed_detail_navigation() -> None:
    full_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"
    parent_visible_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5)"

    class RecoverableLocator:
        def __init__(self, page: "DelayedNavigationPage", selector: str) -> None:
            self.page = page
            self.selector = selector
            self.calls: list[dict[str, Any]] = []

        async def count(self) -> int:
            if not self.page.recovered:
                return 0
            return 1 if self.selector == parent_visible_selector else 0

        async def click(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

        def locator(self, selector: str) -> "RecoverableLocator":
            assert selector == ".van-tabbar-item__text"
            return self

        async def dispatch_event(self, event: str) -> None:
            self.calls.append({"dispatch_event": event})

    class DelayedNavigationPage:
        def __init__(self) -> None:
            self.url = "https://example.com/delayed-detail"
            self.recovered = False
            self.locators: dict[str, RecoverableLocator] = {}
            self.back_calls: list[dict[str, Any]] = []
            self.wait_calls: list[int] = []

        def locator(self, selector: str) -> RecoverableLocator:
            return self.locators.setdefault(selector, RecoverableLocator(self, selector))

        async def go_back(self, **kwargs: Any) -> None:
            self.back_calls.append(kwargs)
            self.recovered = True
            self.url = "https://example.com/list"

        async def wait_for_timeout(self, timeout: int) -> None:
            self.wait_calls.append(timeout)

    page = DelayedNavigationPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=full_selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.back_calls == [{"wait_until": "domcontentloaded", "timeout": 5000}]
    assert page.wait_calls == [500]
    assert page.locators[parent_visible_selector].calls == [{"dispatch_event": "click"}]
    assert action_trace[0]["fallback_reason"] == "browser_history_recovery"
    assert action_trace[0]["history_recovery"] == {
        "from_url": "https://example.com/delayed-detail",
        "to_url": "https://example.com/list",
        "candidate_counts": [1],
        "attempt_count": 1,
        "steps": [
            {
                "attempt": 1,
                "from_url": "https://example.com/delayed-detail",
                "to_url": "https://example.com/list",
                "candidate_counts": [1],
            }
        ],
    }


async def test_bottom_navigation_click_recovers_across_same_url_history_entry() -> None:
    full_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6):visible"
    parent_visible_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(6)"

    class RecoverableLocator:
        def __init__(self, page: "SameUrlHistoryPage", selector: str) -> None:
            self.page = page
            self.selector = selector
            self.events: list[str] = []

        async def count(self) -> int:
            if self.page.back_count < 2:
                return 0
            return 1 if self.selector == parent_visible_selector else 0

        def locator(self, selector: str) -> "RecoverableLocator":
            assert selector == ".van-tabbar-item__text"
            return self

        async def dispatch_event(self, event: str) -> None:
            self.events.append(event)

    class SameUrlHistoryPage:
        def __init__(self) -> None:
            self.url = "https://example.com/ai-tool"
            self.back_count = 0
            self.locators: dict[str, RecoverableLocator] = {}

        def locator(self, selector: str) -> RecoverableLocator:
            return self.locators.setdefault(selector, RecoverableLocator(self, selector))

        async def go_back(self, **_: Any) -> None:
            self.back_count += 1
            if self.back_count == 2:
                self.url = "https://example.com/ai"

        async def wait_for_timeout(self, _: int) -> None:
            return None

    page = SameUrlHistoryPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=full_selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.back_count == 2
    assert page.locators[parent_visible_selector].events == ["click"]
    recovery = action_trace[0]["history_recovery"]
    assert recovery["attempt_count"] == 2
    assert recovery["from_url"] == "https://example.com/ai-tool"
    assert recovery["to_url"] == "https://example.com/ai"
    assert recovery["steps"][0]["to_url"] == "https://example.com/ai-tool"
    assert recovery["steps"][1]["candidate_counts"] == [1]


async def test_bottom_navigation_text_dispatch_waits_for_real_route_change() -> None:
    full_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5):visible"
    parent_visible_selector = "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(5)"

    class DispatchLocator:
        def __init__(self, *, count: int) -> None:
            self.match_count = count
            self.events: list[str] = []

        async def count(self) -> int:
            return self.match_count

        async def click(self, **_: Any) -> None:
            raise AssertionError("可见性缺失时不应走父节点 click")

        def locator(self, selector: str) -> "DispatchLocator":
            assert selector == ".van-tabbar-item__text"
            return self

        async def dispatch_event(self, event: str) -> None:
            self.events.append(event)

    class DispatchPage:
        def __init__(self) -> None:
            self.url = "https://example.com/ground"
            self.missing = DispatchLocator(count=0)
            self.unique = DispatchLocator(count=1)
            self.wait_calls: list[dict[str, Any]] = []

        def locator(self, selector: str) -> DispatchLocator:
            return self.unique if selector == parent_visible_selector else self.missing

        async def wait_for_url(self, predicate: Any, *, timeout: int) -> None:
            self.wait_calls.append({"timeout": timeout})
            self.url = "https://example.com/ai"
            assert predicate(self.url) is True

    page = DispatchPage()
    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=full_selector,
            trigger_action="click",
            navigation_depth=1,
        ),
        action_trace=[],
        timeout_ms=30000,
    )

    assert page.unique.events == ["click"]
    assert page.wait_calls == [{"timeout": 10000}]
    assert page.url == "https://example.com/ai"


async def test_single_trigger_can_return_through_browser_history() -> None:
    class HistoryPage:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def go_back(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    page = HistoryPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="back",
            navigation_depth=1,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.calls == [{"wait_until": "domcontentloaded", "timeout": 30000}]
    assert action_trace == [{"action": "back", "selector": None}]


async def test_single_trigger_rejects_unscoped_browser_history_back() -> None:
    class HistoryPage:
        async def go_back(self, **kwargs: Any) -> None:
            raise AssertionError("未声明来源深度时不得执行浏览器历史返回")

    with pytest.raises(BlockedPageCaptureActionError, match="同页弹窗关闭不得使用 back"):
        await _run_single_trigger(
            page=HistoryPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="back",
            ),
            action_trace=[],
            timeout_ms=30000,
        )


@pytest.mark.parametrize(
    ("returned_url", "error_code"),
    [
        ("about:blank", "BROWSER_HISTORY_EMPTY"),
        ("", "BROWSER_HISTORY_EMPTY"),
        ("https://outside.example/path", "BROWSER_HISTORY_OUT_OF_SCOPE"),
    ],
)
async def test_browser_history_back_rejects_untrusted_destination(
    returned_url: str,
    error_code: str,
) -> None:
    class InvalidHistoryPage:
        def __init__(self) -> None:
            self.url = "https://example.com/profile"

        async def go_back(self, **_: Any) -> None:
            self.url = returned_url

    with pytest.raises(BlockedPageCaptureActionError) as raised:
        await _run_single_trigger(
            page=InvalidHistoryPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="back",
                navigation_depth=1,
            ),
            action_trace=[],
            timeout_ms=30000,
        )

    assert raised.value.error_code == error_code


async def test_overlay_dismissal_waits_for_loading_layer_and_skips_gone_overlay() -> None:
    class LoadingLayer:
        def __init__(self) -> None:
            self.wait_calls: list[dict[str, Any]] = []

        async def count(self) -> int:
            return 1

        async def wait_for(self, **kwargs: Any) -> None:
            self.wait_calls.append(kwargs)

    class MissingOverlay:
        async def count(self) -> int:
            return 0

        async def click(self, **kwargs: Any) -> None:
            raise AssertionError("加载壳退出后不存在的遮罩不应继续点击")

    class LoadingPage:
        def __init__(self) -> None:
            self.loading_layer = LoadingLayer()
            self.overlay = MissingOverlay()

        def locator(self, selector: str) -> LoadingLayer | MissingOverlay:
            if selector == ".loading-page:visible":
                return self.loading_layer
            assert selector == ".van-overlay:visible"
            return self.overlay

    page = LoadingPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            selector=".van-overlay:visible",
            trigger_action="click",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.loading_layer.wait_calls == [{"state": "hidden", "timeout": 30000}]
    assert action_trace == [
        {
            "action": "click",
            "selector": ".van-overlay:visible",
            "waited_for": ".loading-page:visible",
            "result": "overlay_absent_after_loading_transition",
        }
    ]


async def test_overlay_dismissal_keeps_capture_when_loading_layer_stays_visible() -> None:
    class LoadingLayer:
        async def count(self) -> int:
            return 1

        async def wait_for(self, **kwargs: Any) -> None:
            raise TimeoutError("线路仍在加载")

    class ExistingOverlay:
        async def count(self) -> int:
            return 1

        async def click(self, **kwargs: Any) -> None:
            raise AssertionError("加载壳未退出时不得点击底层遮罩")

    class LoadingPage:
        def __init__(self) -> None:
            self.loading_layer = LoadingLayer()
            self.overlay = ExistingOverlay()

        def locator(self, selector: str) -> LoadingLayer | ExistingOverlay:
            if selector == ".loading-page:visible":
                return self.loading_layer
            assert selector == ".van-overlay:visible"
            return self.overlay

    action_trace: list[dict[str, Any]] = []

    with pytest.raises(
        BlockedPageCaptureActionError,
        match="顶层加载或开屏广告仍然可见",
    ) as exc_info:
        await _run_single_trigger(
            page=LoadingPage(),
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                selector=".van-overlay:visible",
                trigger_action="click",
            ),
            action_trace=action_trace,
            timeout_ms=30000,
        )

    assert exc_info.value.error_code == "TOPMOST_OBSTRUCTION_STILL_VISIBLE"
    assert action_trace == [
        {
            "action": "click",
            "selector": ".van-overlay:visible",
            "waited_for": ".loading-page:visible",
            "result": "loading_layer_still_visible",
        }
    ]


async def test_single_trigger_waits_for_dialog_leave_transition_before_capture() -> None:
    class ClosingDialogPage:
        def __init__(self) -> None:
            self.url = "https://example.com/home"
            self.clicked = False
            self.inner_text_calls = 0

        def set_default_timeout(self, timeout: int) -> None:
            self.timeout = timeout

        async def click(self, selector: str, *, timeout: int) -> None:
            assert selector == "text=关闭"
            self.clicked = True

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            assert selector == "body"
            self.inner_text_calls += 1
            if not self.clicked:
                return "广告弹窗"
            texts = ["广告弹窗正在关闭", "首页内容", "首页内容", "首页内容"]
            return texts[min(self.inner_text_calls - 1, len(texts) - 1)]

        async def title(self) -> str:
            return "首页"

    browser = PlaywrightPageCaptureBrowser()
    page = ClosingDialogPage()

    result = await browser._capture_on_page(
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            session_id="run-dialog-leave",
            selector="text=关闭",
            trigger_action="click",
            capture_dom=False,
            capture_visible_text=False,
            capture_screenshot=False,
        ),
        context=object(),
        page=page,
        console_logs=[],
        network_requests=[],
        timeout_error_type=TimeoutError,
    )

    assert result.execution_status == "success"
    assert page.clicked is True
    assert page.inner_text_calls >= 4


async def test_undeclared_click_navigation_is_blocked_and_restores_session() -> None:
    class NavigatingPage:
        def __init__(self) -> None:
            self.url = "https://example.com/home"
            self.go_back_calls = 0
            self.goto_calls: list[str] = []

        def set_default_timeout(self, timeout: int) -> None:
            self.timeout = timeout

        async def click(self, selector: str, timeout: int) -> None:
            self.clicked_selector = selector
            self.url = "https://example.com/detail"

        async def go_back(self, *, wait_until: str, timeout: int) -> object:
            self.go_back_calls += 1
            self.url = "https://example.com/home"
            return object()

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> object:
            self.goto_calls.append(url)
            self.url = url
            return SimpleNamespace(status=200)

        async def title(self) -> str:
            return "Home" if self.url == "https://example.com/home" else "Detail"

    browser = PlaywrightPageCaptureBrowser()
    page = NavigatingPage()

    with pytest.raises(BlockedPageCaptureActionError, match="navigation_depth"):
        await browser._capture_on_page(
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                session_id="run-click-restore",
                selector="#detail",
                trigger_action="click",
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=False,
            ),
            context=object(),
            page=page,
            console_logs=[],
            network_requests=[],
            timeout_error_type=TimeoutError,
        )

    assert page.url == "https://example.com/home"
    assert page.go_back_calls == 1
    assert page.goto_calls == []


async def test_non_goto_mode_rejects_different_url_before_navigation() -> None:
    class StablePage:
        def __init__(self) -> None:
            self.url = "https://example.com/home"
            self.goto_calls: list[str] = []

        def set_default_timeout(self, timeout: int) -> None:
            self.timeout = timeout

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> object:
            self.goto_calls.append(url)
            self.url = url
            return SimpleNamespace(status=200)

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            return "稳定内容"

        async def title(self) -> str:
            return "Home"

    browser = PlaywrightPageCaptureBrowser()
    page = StablePage()

    with pytest.raises(BlockedPageCaptureActionError, match="非 goto 模式"):
        await browser._capture_on_page(
            request=PageCaptureRequest(
                mode=PageCaptureMode.OBSERVE,
                url="https://example.com/detail",
                session_id="run-observe-url",
                capture_dom=False,
                capture_visible_text=False,
                capture_screenshot=False,
            ),
            context=object(),
            page=page,
            console_logs=[],
            network_requests=[],
            timeout_error_type=TimeoutError,
        )

    assert page.goto_calls == []


async def test_observe_with_current_url_does_not_reload_page() -> None:
    class StablePage:
        def __init__(self) -> None:
            self.url = "https://example.com/home"
            self.goto_calls: list[str] = []
            self.inner_text_calls = 0

        def set_default_timeout(self, timeout: int) -> None:
            self.timeout = timeout

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> object:
            self.goto_calls.append(url)
            self.url = url
            return SimpleNamespace(status=200)

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.inner_text_calls += 1
            return "稳定内容"

        async def title(self) -> str:
            return "Home"

    browser = PlaywrightPageCaptureBrowser()
    page = StablePage()

    result = await browser._capture_on_page(
        request=PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            url="https://example.com/home",
            session_id="run-observe-current-url",
            capture_dom=False,
            capture_visible_text=False,
            capture_screenshot=False,
        ),
        context=object(),
        page=page,
        console_logs=[],
        network_requests=[],
        timeout_error_type=TimeoutError,
    )

    assert result.final_url == "https://example.com/home"
    assert page.goto_calls == []
    assert page.inner_text_calls >= 1


async def test_popup_dismissal_signature_tracks_visible_ad_item_state() -> None:
    class PopupPage:
        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert args == {
                "requestedSelector": "text=close",
                "selector": "text=close",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            assert "ad-item" in script
            assert "b-img" in script
            assert "querySelectorAll('.ad-item, .btn-close, .b-img, [class*=\"active\"]')" in script
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": ".btn-close",
                "dialog_selector": ".van-dialog",
                "dialog_signature": "same-dialog-with-active-ad",
                "verification_required": True,
            }

        async def wait_for_function(self, script: str, **kwargs: Any) -> None:
            assert "ad-item" in script
            assert "b-img" in script
            assert "performance.now() - dismissedAt >= stableMs" in script
            assert "__aliothPopupDismissalSinceBySignature" in script
            assert "querySelectorAll('.ad-item, .btn-close, .b-img, [class*=\"active\"]')" in script
            assert kwargs["arg"] == {
                "selector": ".van-dialog",
                "signature": "same-dialog-with-active-ad",
                "stableMs": 800,
            }
            assert kwargs["timeout"] == 2300
            assert kwargs["polling"] == 100

    result = await page_capture_module._resolve_popup_dismissal_target(
        page=PopupPage(),
        selector="text=close",
    )

    assert result is not None
    assert result["verification_required"] is True
    assert await page_capture_module._wait_for_popup_dismissal(
        page=PopupPage(),
        popup_target=result,
        timeout_ms=30000,
        timeout_error_type=TimeoutError,
    )


@pytest.mark.parametrize(
    ("browser_outcome", "expected"),
    [
        ("original_layer_absent", "original_layer_absent"),
        ("original_layer_hidden", "original_layer_hidden"),
        ("next_popup_layer_visible", "next_popup_layer_visible"),
        ("original_layer_still_visible", "original_layer_still_visible"),
    ],
)
async def test_popup_dismissal_outcome_is_preserved_for_action_audit(
    browser_outcome: str,
    expected: str,
) -> None:
    class PopupPage:
        async def evaluate(self, script: str, argument: dict[str, str]) -> str:
            assert "original_layer_absent" in script
            assert "next_popup_layer_visible" in script
            assert argument == {
                "selector": ".van-dialog",
                "signature": "popup-signature-1",
                "layerMedia": None,
            }
            return browser_outcome

    outcome = await page_capture_module._observe_popup_dismissal_outcome(
        page=PopupPage(),
        popup_target={
            "dialog_selector": ".van-dialog",
            "dialog_signature": "popup-signature-1",
        },
    )

    assert outcome == expected


async def test_popup_dismissal_samples_strict_overlay_hit_beyond_four_corners() -> None:
    """四角被安全区层占用时，仍可寻找 Dialog 外且严格命中遮罩的点。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        try:
            await page.set_content(
                """
                <style>
                  html, body { margin: 0; width: 100%; height: 100%; }
                  .van-overlay {
                    position: fixed; inset: 0; z-index: 2014;
                    background: rgba(0, 0, 0, .45);
                  }
                  .code-popup {
                    position: fixed; left: 39px; top: 166px;
                    width: 312px; height: 512px; z-index: 2014;
                    background: white;
                  }
                  .safe-area-blocker {
                    position: fixed; width: 12px; height: 12px; z-index: 2015;
                  }
                  .top-left { left: 0; top: 0; }
                  .top-right { right: 0; top: 0; }
                  .bottom-left { left: 0; bottom: 0; }
                  .bottom-right { right: 0; bottom: 0; }
                </style>
                <div class="van-overlay" role="button"></div>
                <div class="code-popup" role="dialog">
                  <button>立即保存</button>
                </div>
                <div class="safe-area-blocker top-left"></div>
                <div class="safe-area-blocker top-right"></div>
                <div class="safe-area-blocker bottom-left"></div>
                <div class="safe-area-blocker bottom-right"></div>
                """
            )

            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.POPUP_DIALOG_GENERIC_CLOSE_SELECTOR,
            )

            assert result is not None
            assert result["kind"] == "overlay"
            assert result["selector"]
            position = result["position"]
            hit_is_overlay = await page.locator(result["selector"]).evaluate(
                """
                (overlay, position) => {
                  const rect = overlay.getBoundingClientRect();
                  return document.elementFromPoint(
                    rect.left + position.x,
                    rect.top + position.y,
                  ) === overlay;
                }
                """,
                position,
            )
            assert hit_is_overlay is True
        finally:
            await page.close()
            await browser.close()


async def test_payment_popup_without_close_uses_overlay_not_immediate_payment() -> None:
    """支付弹窗没有关闭按钮时只能严格点击蒙版，绝不能点击立即支付。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        try:
            await page.set_content(
                """
                <style>
                  html, body { margin: 0; width: 100%; height: 100%; }
                  .van-overlay {
                    position: fixed; inset: 0; z-index: 2000;
                    background: rgba(0, 0, 0, .45);
                  }
                  .payment-popup {
                    position: fixed; left: 39px; top: 166px;
                    width: 312px; height: 512px; z-index: 2001;
                    background: white;
                  }
                </style>
                <script>
                  window.paymentClicked = false;
                  window.overlayClicked = false;
                </script>
                <div class="van-overlay" role="button"
                     onclick="window.overlayClicked = true"></div>
                <div class="van-popup van-popup--center payment-popup" role="dialog">
                  <div>选择支付方式</div>
                  <button onclick="window.paymentClicked = true">立即支付</button>
                </div>
                """
            )

            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=".van-overlay:visible",
            )

            assert result is not None
            assert result["kind"] == "overlay"
            assert "position" in result
            await page.locator(result["selector"]).click(position=result["position"])
            assert await page.evaluate("() => window.overlayClicked") is True
            assert await page.evaluate("() => window.paymentClicked") is False
        finally:
            await page.close()
            await browser.close()


async def test_profile_credential_save_button_closes_current_popup() -> None:
    """个人中心阻断弹窗“立即保存”必须按专用 selector 点击并验证退场。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})
        try:
            await page.set_content(
                """
                <style>
                  html, body { margin: 0; width: 100%; height: 100%; }
                  .van-overlay {
                    position: fixed; inset: 0; z-index: 2000;
                    background: rgba(0, 0, 0, .45);
                  }
                  .profile-save-popup {
                    position: fixed; left: 39px; top: 166px;
                    width: 312px; height: 512px; z-index: 2001;
                    background: white;
                  }
                  .promoteBtn { display: inline-block; width: 135px; height: 46px; }
                </style>
                <script>
                  window.saveClicked = false;
                  window.dismissCredential = () => {
                    window.saveClicked = true;
                    document.querySelector('.profile-save-popup').style.display = 'none';
                    document.querySelector('.van-overlay').style.display = 'none';
                  };
                </script>
                <div class="van-overlay" role="button"></div>
                <div class="van-popup van-popup--center profile-save-popup"
                     role="dialog">
                  <div class="content">
                    当前内容可稍后查看，请先完成本地保存
                  </div>
                  <div class="promoteBtnBox">
                    <div class="promoteBtn btn1">我已保存</div>
                    <div class="promoteBtn btn2"
                         onclick="window.dismissCredential()">立即保存</div>
                  </div>
                </div>
                """
            )

            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.H5_PROFILE_CREDENTIAL_SAVE_CONFIRM_SELECTOR,
            )

            assert result is not None
            assert result["kind"] == "close_control"
            await page.locator(result["selector"]).click()
            assert await page.evaluate("() => window.saveClicked") is True
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30000,
                timeout_error_type=TimeoutError,
            )
        finally:
            await page.close()
            await browser.close()


async def test_popup_dismissal_resolver_supports_van_popup_center_close_image() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .van-overlay {
        position: fixed;
        inset: 0;
        z-index: 99;
        background: rgba(0, 0, 0, 0.6);
      }
      .van-popup.van-popup--center {
        position: fixed;
        z-index: 100;
        left: 19px;
        top: 166px;
        width: 352px;
        height: 511px;
        background: white;
      }
      .popup-img {
        width: 352px;
        height: 432px;
      }
      .popup-img img {
        width: 100%;
        height: 100%;
      }
      .close-img {
        width: 352px;
        height: 79px;
        display: flex;
        align-items: center;
        justify-content: center;
      }
      .close-image {
        width: 40px;
        height: 40px;
      }
    </style>
    <script>
      window.dismissPopup = () => {
        window.closeClicked = true;
        document.querySelector('.van-popup').style.display = 'none';
        document.querySelector('.van-overlay').style.display = 'none';
      };
    </script>
    <div class="van-overlay"></div>
    <div class="van-popup van-popup--center">
      <div class="component-advertises-0">
        <div class="popup-form">
          <div class="popup-img tjtagmanager"><a class="img"><img alt=""></a></div>
          <div class="close-img" onclick="window.dismissPopup()">
            <img class="close-image on-dialog-close" alt="关闭">
          </div>
        </div>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=".van-overlay:visible",
            )
            assert result is not None
            assert result["kind"] == "close_control"
            assert result["verification_required"] is True
            await page.locator(result["selector"]).click()
            assert await page.evaluate("() => window.closeClicked === true")
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30000,
                timeout_error_type=TimeoutError,
            )
            await browser.close()
    except (AssertionError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")


async def test_adspop_dismissal_closes_modal_without_activating_external_ad() -> None:
    """普通广告弹窗只点关闭键；广告主体的跨域链接保持零交互。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .adspop { position: fixed; inset: 0; z-index: 100; }
      .popup-container { position: fixed; inset: 0; background: rgba(0,0,0,.7); }
      .popup-content { position: fixed; left: 600px; top: 100px; width: 640px; height: 780px; }
      .popup-picture { width: 640px; height: 700px; background: white; }
      .popup-picture img { width: 100%; height: 100%; }
      .popup-close { width: 42px; height: 42px; margin: 10px auto; }
      .popup-close img { width: 42px; height: 42px; }
    </style>
    <script>
      window.adActivated = false;
      window.dismissed = false;
      window.dismissPopup = () => {
        window.dismissed = true;
        document.querySelector('.adspop').style.display = 'none';
      };
    </script>
    <div class="adspop">
      <div class="popup-container"><div class="popup-content">
        <a class="popup-picture" href="https://ads.example/landing"
           onclick="window.adActivated = true"><img alt="广告主体"></a>
        <div class="popup-close" onclick="window.dismissPopup()"><img alt=""></div>
      </div></div>
    </div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.POPUP_OVERLAY_AD_CLOSE_SELECTOR,
            )
            assert result is not None
            assert result["kind"] == "close_control"
            assert "popup-close" in result["selector"]
            await page.locator(result["selector"]).click()
            assert await page.evaluate("() => window.dismissed") is True
            assert await page.evaluate("() => window.adActivated") is False
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30_000,
                timeout_error_type=TimeoutError,
            )
        finally:
            await page.close()
            await browser.close()


async def test_recommend_layer_dismissal_closes_modal_without_activating_external_ad() -> None:
    """推荐广告层通过委托事件关闭时，不得触发同层广告链接。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .recommend-layer { position: fixed; inset: 0; z-index: 10000;
        background: rgba(0,0,0,.7); }
      .recommend-wrapper { position: fixed; inset: 0; display: grid; place-items: center; }
      .recommend-card { width: 640px; height: 800px; }
      .media-box { display: block; width: 640px; height: 740px; background: white; }
      .close-btn { width: 42px; height: 42px; margin: 10px auto; cursor: pointer; }
    </style>
    <script>
      window.adActivated = false;
      window.dismissed = false;
      document.addEventListener('click', (event) => {
        if (!event.target.closest('.close-btn')) return;
        window.dismissed = true;
        document.querySelector('.recommend-layer').style.display = 'none';
      });
    </script>
    <div class="recommend-layer"><div class="recommend-wrapper">
      <div class="recommend-card">
        <a class="media-box" href="https://ads.example/landing"
           onclick="window.adActivated = true">广告主体</a>
        <div class="close-btn"><img alt=""></div>
      </div>
    </div></div>
    """

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境未安装浏览器时跳过集成回归
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.POPUP_OVERLAY_AD_CLOSE_SELECTOR,
            )
            assert result is not None
            assert result["kind"] == "close_control"
            assert "close-btn" in result["selector"]
            await page.locator(result["selector"]).click()
            assert await page.evaluate("() => window.dismissed") is True
            assert await page.evaluate("() => window.adActivated") is False
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30_000,
                timeout_error_type=TimeoutError,
            )
        finally:
            await page.close()
            await browser.close()


async def test_recommend_layer_close_is_blocked_by_higher_stacking_context() -> None:
    """更高层登录框遮挡关闭键时不得向下层推荐广告程序化派发点击。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .security-layer { position: fixed; inset: 0; z-index: 20000; background: white; }
      .security-layer [role="dialog"] { width: 500px; height: 500px; margin: 100px auto; }
      .recommend-layer { position: fixed; inset: 0; z-index: 10000; }
      .recommend-wrapper { position: fixed; inset: 0; display: grid; place-items: center; }
      .recommend-card { width: 640px; height: 800px; }
      .close-btn { width: 42px; height: 42px; margin: 740px auto 0; cursor: pointer; }
    </style>
    <script>
      window.dismissed = false;
      document.addEventListener('click', (event) => {
        if (event.target.closest('.close-btn')) window.dismissed = true;
      });
    </script>
    <div class="security-layer"><div role="dialog">请登录后继续</div></div>
    <div class="recommend-layer"><div class="recommend-wrapper">
      <div class="recommend-card"><div class="close-btn">×</div></div>
    </div></div>
    """

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境未安装浏览器时跳过集成回归
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.POPUP_OVERLAY_AD_CLOSE_SELECTOR,
            )
            assert result is not None
            assert result.get("kind") != "close_control"
            assert await page.evaluate("() => window.dismissed") is False
        finally:
            await page.close()
            await browser.close()


async def test_recommend_layer_close_rejects_descendant_ad_link() -> None:
    """关闭包装层的中心命中子链接时，实时层必须拒绝而不是触发外跳。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .recommend-layer { position: fixed; inset: 0; z-index: 10000; }
      .recommend-wrapper { position: fixed; inset: 0; display: grid; place-items: center; }
      .recommend-card { width: 640px; height: 800px; }
      .close-btn { width: 42px; height: 42px; margin: 740px auto 0; }
      .close-btn a { display: block; width: 42px; height: 42px; }
    </style>
    <script>window.adActivated = false;</script>
    <div class="recommend-layer"><div class="recommend-wrapper">
      <div class="recommend-card"><div class="close-btn">
        <a class="close-link" href="https://ads.example/landing"
           onclick="window.adActivated = true">关闭</a>
      </div></div>
    </div></div>
    """

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境未安装浏览器时跳过集成回归
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.POPUP_OVERLAY_AD_CLOSE_SELECTOR,
            )
            assert result is not None
            assert result.get("kind") != "close_control"
            assert await page.evaluate("() => window.adActivated") is False
        finally:
            await page.close()
            await browser.close()


async def test_accessible_dialog_anchor_close_keeps_existing_contract() -> None:
    """广告专用导航拒绝不得误伤普通 accessible dialog 的锚点关闭键。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      [role="dialog"] { position: fixed; inset: 100px; z-index: 2000; background: white; }
      .dialog-close { display: block; width: 42px; height: 42px; }
    </style>
    <div role="dialog">
      <a class="dialog-close" href="javascript:;" aria-label="关闭"
         onclick="this.closest('[role=dialog]').style.display='none'">
        <img alt="关闭" width="42" height="42">
      </a>
    </div>
    """

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境未安装浏览器时跳过集成回归
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.POPUP_DIALOG_ACCESSIBLE_CLOSE_SELECTOR,
            )
            assert result is not None
            assert result.get("kind") == "close_control"
            await page.locator(str(result["selector"])).click()
            assert await page.locator('[role="dialog"]').is_hidden()
        finally:
            await page.close()
            await browser.close()


async def test_recommend_layer_uses_real_layer_inside_persistent_popup_host() -> None:
    """常驻 popup host 不得替代真正会消失的推荐广告层作为复验根。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .popup-host { position: static; width: 1px; min-height: 1px; }
      .recommend-layer { position: fixed; inset: 0; z-index: 10000; }
      .recommend-wrapper { position: fixed; inset: 0; display: grid; place-items: center; }
      .recommend-card { width: 640px; height: 800px; }
      .close-btn { width: 42px; height: 42px; margin: 740px auto 0; cursor: pointer; }
    </style>
    <div class="popup-host"><div class="recommend-layer"><div class="recommend-wrapper">
      <div class="recommend-card"><div class="close-btn">×</div></div>
    </div></div></div>
    <script>
      document.addEventListener('click', (event) => {
        if (event.target.closest('.close-btn')) {
          document.querySelector('.recommend-layer').style.display = 'none';
        }
      });
    </script>
    """

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - 环境未安装浏览器时跳过集成回归
            pytest.skip(f"Playwright Chromium 不可用：{exc}")
        page = await browser.new_page(viewport={"width": 1920, "height": 1080})
        try:
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=page_capture_module.POPUP_OVERLAY_AD_CLOSE_SELECTOR,
            )
            assert result is not None
            assert result.get("kind") == "close_control"
            assert "recommend-layer" in str(result.get("dialog_signature"))
            await page.locator(str(result["selector"])).click()
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30_000,
                timeout_error_type=TimeoutError,
            )
            assert await page.locator(".popup-host").count() == 1
        finally:
            await page.close()
            await browser.close()


async def test_adspop_noop_close_fails_closed_after_verified_click() -> None:
    """关闭事件无效时必须报错，不能把未变化页面伪装成已清障。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .adspop { position: fixed; inset: 0; z-index: 100; }
      .popup-container { position: fixed; inset: 0; background: rgba(0,0,0,.7); }
      .popup-content { position: fixed; left: 600px; top: 100px; width: 640px; height: 780px; }
      .popup-picture { width: 640px; height: 700px; background: white; }
      .popup-close { width: 42px; height: 42px; margin: 10px auto; }
      .popup-close img { width: 42px; height: 42px; }
    </style>
    <div class="adspop"><div class="popup-container"><div class="popup-content">
      <a class="popup-picture" href="https://pay.example/checkout">广告主体</a>
      <div class="popup-close"><img alt=""></div>
    </div></div></div>
    """

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        try:
            await page.set_content(html)
            trace: list[dict[str, Any]] = []
            with pytest.raises(BlockedPageCaptureActionError) as error:
                await _run_single_trigger(
                    page=page,
                    context=context,
                    request=PageCaptureRequest(
                        mode=PageCaptureMode.SINGLE_TRIGGER,
                        trigger_action=PageTriggerAction.CLICK,
                        selector=page_capture_module.POPUP_OVERLAY_AD_CLOSE_SELECTOR,
                        allow_high_z_obstruction_hide=True,
                    ),
                    action_trace=trace,
                    timeout_ms=3_000,
                )
            assert error.value.error_code == "POPUP_DISMISSAL_NOT_OBSERVED"
            assert await page.evaluate("() => location.href") == "about:blank"
            assert trace[-1]["result"] == "popup_dismissal_not_observed"
        finally:
            await context.close()
            await browser.close()


async def test_popup_dismissal_resolver_supports_account_credential_confirm() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .van-overlay {
        position: fixed;
        inset: 0;
        z-index: 2000;
        background: rgba(0, 0, 0, 0.65);
      }
      .van-popup.van-popup--center {
        position: fixed;
        z-index: 2001;
        left: 42px;
        top: 141px;
        width: 306px;
        min-height: 460px;
        border-radius: 18px;
        background: #6b5135;
        color: white;
      }
      .van-popup .promote_box {
        padding: 32px 20px 96px;
        text-align: center;
      }
      .van-popup .promoteBtnBox {
        position: absolute;
        left: 12px;
        right: 12px;
        bottom: 16px;
        display: flex;
        gap: 16px;
      }
      .van-popup .promoteBtn {
        width: 134px;
        height: 44px;
        border-radius: 22px;
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
      }
    </style>
    <script>
      window.dismissCredential = () => {
        window.savedClicked = true;
        document.querySelector('.van-popup').style.display = 'none';
        document.querySelector('.van-overlay').style.display = 'none';
      };
    </script>
    <div class="van-overlay"></div>
    <div class="van-popup van-popup--center" role="dialog">
      <div class="promote_box flex-vcenter">
        <div class="promote_code">账号凭证</div>
        <p>ID 19009966</p>
        <div class="invit_quantity">提示*可通过官方客服找回账号</div>
        <div class="promote_info">APP更新可能会导致会员账号掉线，请勿删除此凭证</div>
        <div class="promote_info">永久官方地址 https://91porn01.cc</div>
      </div>
      <div class="promoteBtnBox">
        <div class="promoteBtn btn1" onclick="window.dismissCredential()">我已保存</div>
        <div class="promoteBtn btn2">立即保存</div>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=".van-overlay:visible",
            )
            assert result is not None
            assert result["kind"] == "close_control"
            assert result["verification_required"] is True
            await page.locator(result["selector"]).click()
            assert await page.evaluate("() => window.savedClicked === true")
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30000,
                timeout_error_type=TimeoutError,
            )
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")


async def test_popup_dismissal_resolver_supports_nine_grid_overlay_close() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .nineGridPopup .van-overlay {
        position: fixed;
        inset: 0;
        z-index: 2001;
        background: rgba(0, 0, 0, 0.8);
      }
      .nineGridPopup .wrapper {
        position: relative;
        width: 390px;
        height: 844px;
      }
      .nineGridPopup .appList {
        position: absolute;
        left: 0;
        top: 140px;
        width: 390px;
        height: 500px;
      }
      .nineGridPopup .close {
        position: absolute;
        left: 178px;
        top: 649px;
        width: 34px;
        height: 34px;
        border-radius: 50%;
      }
      .nineGridPopup .close::before {
        content: "x";
      }
    </style>
    <script>
      window.dismissPopup = () => {
        window.closeClicked = true;
        document.querySelector('.van-overlay').style.display = 'none';
      };
    </script>
    <div class="nineGridPopup">
      <div class="van-overlay">
        <div class="wrapper">
          <div class="appList">同城约炮 开元电子 春药迷药 澳门新葡京 免费看片 PG娱乐</div>
          <div class="close" onclick="window.dismissPopup()"></div>
        </div>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=".van-overlay:visible",
            )
            assert result is not None
            assert result["kind"] == "close_control"
            assert result["verification_required"] is True
            await page.locator(result["selector"]).click()
            assert await page.evaluate("() => window.closeClicked === true")
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30000,
                timeout_error_type=TimeoutError,
            )
            await browser.close()
    except (AssertionError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")


async def test_popup_dismissal_runtime_script_contains_nine_grid_overlay_contract() -> None:
    class NineGridPage:
        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert args == {
                "requestedSelector": ".van-overlay:visible",
                "selector": ".van-overlay:visible",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            assert ".nineGridPopup > .van-overlay > .wrapper" in script
            assert ".nineGridPopup .van-overlay .wrapper > .close" in script
            return {"dialog_present": False}

    assert await page_capture_module._resolve_popup_dismissal_target(
        page=NineGridPage(),
        selector=".van-overlay:visible",
    ) == {"dialog_present": False}


async def test_popup_dismissal_resolver_supports_announcement_overlay_close() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      .announcementPopup .van-overlay {
        position: fixed;
        inset: 0;
        z-index: 2001;
        background: rgba(0, 0, 0, 0.8);
      }
      .announcementPopup .imgWrapper {
        position: relative;
        width: 390px;
        height: 844px;
      }
      .announcementPopup .imgBox {
        position: absolute;
        left: 39px;
        top: 193px;
        width: 312px;
        height: 358px;
      }
      .announcementPopup .close {
        position: absolute;
        left: 176px;
        top: 582px;
        width: 38px;
        height: 38px;
        border-radius: 50%;
      }
      .announcementPopup .close::before {
        content: "x";
      }
    </style>
    <script>
      window.dismissPopup = () => {
        window.closeClicked = true;
        document.querySelector('.van-overlay').style.display = 'none';
      };
    </script>
    <div class="announcementPopup">
      <div class="van-overlay">
        <div class="imgWrapper">
          <div class="imgBox">注册送88元</div>
          <div class="close" onclick="window.dismissPopup()"></div>
        </div>
      </div>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            result = await page_capture_module._resolve_popup_dismissal_target(
                page=page,
                selector=".van-overlay:visible",
            )
            assert result is not None
            assert result["kind"] == "close_control"
            assert result["verification_required"] is True
            await page.locator(result["selector"]).click()
            assert await page.evaluate("() => window.closeClicked === true")
            assert await page_capture_module._wait_for_popup_dismissal(
                page=page,
                popup_target=result,
                timeout_ms=30000,
                timeout_error_type=TimeoutError,
            )
            await browser.close()
    except (AssertionError, KeyError):
        raise
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")


async def test_popup_dismissal_runtime_script_contains_announcement_overlay_contract() -> None:
    class AnnouncementPage:
        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert args == {
                "requestedSelector": ".van-overlay:visible",
                "selector": ".van-overlay:visible",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            assert ".announcementPopup > .van-overlay > .imgWrapper" in script
            assert ".announcementPopup .van-overlay .imgWrapper > .close" in script
            return {"dialog_present": False}

    assert await page_capture_module._resolve_popup_dismissal_target(
        page=AnnouncementPage(),
        selector=".van-overlay:visible",
    ) == {"dialog_present": False}


async def test_popup_dismissal_resolver_keeps_exact_homepage_payment_close_in_chain() -> None:
    class HomepagePaymentPage:
        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert args == {
                "requestedSelector": "text=关闭",
                "selector": "text=关闭",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            assert "exactDismissals" in script
            assert ".homepagePaymentPopup .imgWrapper > .close" in script
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": "body > div.homepagePaymentPopup > div > div > div.close",
            }

    result = await page_capture_module._resolve_popup_dismissal_target(
        page=HomepagePaymentPage(),
        selector="text=关闭",
    )

    assert result == {
        "dialog_present": True,
        "kind": "close_control",
        "selector": "body > div.homepagePaymentPopup > div > div > div.close",
    }


async def test_popup_dismissal_resolver_supports_startup_down_time() -> None:
    class StartupPage:
        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert args == {
                "requestedSelector": ".loading-page .down-time:visible",
                "selector": ".loading-page .down-time:visible",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            assert ".loading-page" in script
            assert ".down-time" in script
            assert "down-time|countdown" in script
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": "body > div.loading-page > span.down-time",
            }

    result = await page_capture_module._resolve_popup_dismissal_target(
        page=StartupPage(),
        selector=".loading-page .down-time:visible",
    )

    assert result == {
        "dialog_present": True,
        "kind": "close_control",
        "selector": "body > div.loading-page > span.down-time",
    }


async def test_popup_dismissal_resolver_prefers_config_error_confirm() -> None:
    class ConfigErrorPage:
        async def evaluate(self, script: str, args: dict[str, Any]) -> dict[str, Any]:
            assert args == {
                "requestedSelector": ".loading-page .down-time:visible",
                "selector": ".loading-page .down-time:visible",
                "prefer_safe_outside": False,
                "strictNavigationSubtree": False,
            }
            assert "dialogRequestsRetry" in script
            assert "van-dialog__confirm" in script
            assert "配置数据失败" in script
            return {
                "dialog_present": True,
                "kind": "close_control",
                "selector": "body > div.van-dialog > button.van-dialog__confirm",
            }

    result = await page_capture_module._resolve_popup_dismissal_target(
        page=ConfigErrorPage(),
        selector=".loading-page .down-time:visible",
    )

    assert result == {
        "dialog_present": True,
        "kind": "close_control",
        "selector": "body > div.van-dialog > button.van-dialog__confirm",
    }


async def test_capture_page_for_agent_fails_unproven_popup_dismissal() -> None:
    browser = UnprovenDismissalPageCaptureBrowser()
    persistence = FakePageCapturePersistenceService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await capture_page_for_agent(
            mode="single_trigger",
            trigger_action="click",
            selector=".loading-page .down-time:visible",
            exploration_action_id="exploration-2",
            browser=browser,
            persistence_service=persistence,
        )

    assert result["execution_status"] == "error"
    assert result["handled"] is True
    assert result["failure_scope"] == "page"
    assert result["failure_stage"] == "browser_interaction"
    assert result["error_code"] == "PAGE_OBSTRUCTION_DISMISSAL_NOT_PROVEN"
    assert result["exploration_action_id"] == "exploration-2"
    assert persistence.calls == []


async def test_capture_page_for_agent_preserves_interaction_failure_code() -> None:
    class FailedInteractionBrowser:
        async def capture(self, request: PageCaptureRequest) -> PageCaptureResult:
            del request
            raise PageInteractionFailedError(
                "当前页面未找到点击目标",
                error_code="CLICK_TARGET_NOT_FOUND",
            )

    result = await capture_page_for_agent(
        mode="single_trigger",
        trigger_action="click",
        selector=".missing",
        exploration_action_id="exploration-3",
        browser=FailedInteractionBrowser(),
    )

    assert result["execution_status"] == "error"
    assert result["handled"] is True
    assert result["failure_stage"] == "browser_interaction"
    assert result["failure_kind"] == "platform_capability"
    assert result["error_code"] == "CLICK_TARGET_NOT_FOUND"
    assert result["exploration_action_id"] == "exploration-3"


async def test_capture_page_for_agent_converts_blocked_action_to_structured_result() -> None:
    browser = FakePageCaptureBrowser()

    result = await capture_page_for_agent(
        mode="single_trigger",
        url="https://example.com",
        trigger_action="submit",
        browser=browser,
    )

    assert result["execution_status"] == "blocked"
    assert result["mode"] == "single_trigger"
    assert "submit" in result["error_message"]
    assert browser.requests == []


async def test_capture_page_for_agent_rejects_combined_action_and_selector() -> None:
    browser = FakePageCaptureBrowser()

    result = await capture_page_for_agent(
        mode="single_trigger",
        trigger_action="click text=关闭",
        browser=browser,
    )

    assert result["execution_status"] == "blocked"
    assert result["error_code"] == "PAGE_CAPTURE_ACTION_BLOCKED"
    assert "动作和目标必须分开传递" in result["message"]
    assert "trigger_action='click'" in result["message"]
    assert "selector='text=关闭'" in result["message"]
    assert browser.requests == []


async def test_capture_page_for_agent_requires_selector_for_click() -> None:
    browser = FakePageCaptureBrowser()

    result = await capture_page_for_agent(
        mode="single_trigger",
        trigger_action="click",
        browser=browser,
    )

    assert result["execution_status"] == "blocked"
    assert "click 触发必须单独提供 selector" in result["message"]
    assert browser.requests == []


async def test_registered_page_capture_tool_keeps_action_and_selector_separate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakePageCaptureBrowser()
    monkeypatch.setattr(
        page_capture_tools,
        "_get_page_capture_browser",
        lambda: browser,
    )

    result = await page_capture_tool.invoke(
        arguments={
            "mode": "single_trigger",
            "trigger_action": "click",
            "selector": "text=关闭",
        }
    )

    assert result
    assert len(browser.requests) == 1
    assert browser.requests[0].trigger_action == "click"
    assert browser.requests[0].selector == "text=关闭"


async def test_registered_page_capture_tool_blocks_controlled_probe_when_supplemental_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakePageCaptureBrowser()
    monkeypatch.setattr(
        page_capture_tools,
        "_get_page_capture_browser",
        lambda: browser,
    )

    result = await page_capture_tool.invoke(
        arguments={
            "mode": "single_trigger",
            "trigger_action": "submit_probe",
            "selector": "textarea.comment",
            "submit_selector": "button.submit",
            "probe_text": "ALIOTH_RATE_PROBE_run_001",
        }
    )

    assert result
    assert browser.requests == []


async def test_registered_page_capture_tool_blocks_fault_probe_when_supplemental_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = FakePageCaptureBrowser()
    monkeypatch.setattr(
        page_capture_tools,
        "_get_page_capture_browser",
        lambda: browser,
    )

    result = await page_capture_tool.invoke(
        arguments={
            "mode": "single_trigger",
            "trigger_action": "network_fault",
            "fault_url_pattern": r"ads\.example\.com/",
        }
    )

    assert result
    assert browser.requests == []


async def test_capture_page_for_agent_returns_disabled_status_for_supplemental_probe() -> None:
    browser = FakePageCaptureBrowser()

    result = await capture_page_for_agent(
        mode="single_trigger",
        trigger_action="submit_probe",
        selector="textarea.comment",
        submit_selector="button.submit",
        probe_text="ALIOTH_RATE_PROBE_run_001",
        browser=browser,
    )

    assert result["execution_status"] == "blocked"
    assert result["error_code"] == "SUPPLEMENTAL_EVIDENCE_DISABLED"
    assert browser.requests == []


async def test_submit_probe_fills_and_submits_without_recording_raw_text() -> None:
    class ProbeLocator:
        def __init__(self) -> None:
            self.fills: list[tuple[str, int]] = []
            self.clicks: list[int] = []

        async def fill(self, value: str, *, timeout: int) -> None:
            self.fills.append((value, timeout))

        async def click(self, *, timeout: int) -> None:
            self.clicks.append(timeout)

    class ProbePage:
        def __init__(self) -> None:
            self.locators: dict[str, ProbeLocator] = {}

        def locator(self, selector: str) -> ProbeLocator:
            return self.locators.setdefault(selector, ProbeLocator())

    page = ProbePage()
    action_trace: list[dict[str, Any]] = []
    probe_text = "ALIOTH_RATE_PROBE_run_001"

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="submit_probe",
            selector="textarea.comment",
            submit_selector="button.submit",
            probe_text=probe_text,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.locators["textarea.comment"].fills == [(probe_text, 30000)]
    assert page.locators["button.submit"].clicks == [30000]
    assert action_trace[0]["action"] == "submit_probe"
    assert action_trace[0]["result"] == "probe_submitted"
    assert action_trace[0]["probe_text_length"] == len(probe_text)
    assert probe_text not in json.dumps(action_trace)


async def test_fill_records_only_input_digest_and_length() -> None:
    class InputLocator:
        def __init__(self) -> None:
            self.fills: list[tuple[str, int]] = []

        async def fill(self, value: str, *, timeout: int) -> None:
            self.fills.append((value, timeout))

    class InputPage:
        def __init__(self) -> None:
            self.locator_instance = InputLocator()

        def locator(self, selector: str) -> InputLocator:
            assert selector == "textarea.comment"
            return self.locator_instance

    page = InputPage()
    action_trace: list[dict[str, Any]] = []
    input_text = "这是一段中性测试内容"

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="fill",
            selector="textarea.comment",
            input_text=input_text,
        ),
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert page.locator_instance.fills == [(input_text, 30_000)]
    assert action_trace[0]["action"] == "fill"
    assert action_trace[0]["result"] == "input_filled"
    assert action_trace[0]["input_text_length"] == len(input_text)
    assert len(action_trace[0]["input_text_sha256"]) == 64
    assert input_text not in json.dumps(action_trace, ensure_ascii=False)


async def test_seek_media_uses_one_pointer_drag_and_records_media_truth() -> None:
    class SeekMouse:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        async def move(self, x: float, y: float, *, steps: int | None = None) -> None:
            self.calls.append(("move", x, y, steps))

        async def down(self) -> None:
            self.calls.append(("down",))

        async def up(self) -> None:
            self.calls.append(("up",))

    class SeekPage:
        def __init__(self) -> None:
            self.mouse = SeekMouse()
            self.evaluate_calls = 0
            self.wait_args: dict[str, Any] | None = None

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            del script, argument
            self.evaluate_calls += 1
            if self.evaluate_calls == 1:
                return {
                    "status": "ready",
                    "start": {"x": 20, "y": 10},
                    "end": {"x": 100, "y": 10},
                    "target_fraction": 0.5,
                    "target_time_seconds": 60,
                    "before": {
                        "current_time": 10,
                        "duration": 120,
                        "paused": False,
                    },
                }
            return {
                "current_time": 60.4,
                "duration": 120,
                "paused": False,
                "seeking": False,
            }

        async def wait_for_function(
            self,
            script: str,
            *,
            arg: dict[str, Any],
            timeout: int,
        ) -> None:
            del script
            self.wait_args = {"arg": arg, "timeout": timeout}

    page = SeekPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="seek_media",
            selector="#progress",
            seek_fraction=0.5,
        ),
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert page.mouse.calls == [
        ("move", 20.0, 10.0, None),
        ("down",),
        ("move", 100.0, 10.0, 8),
        ("up",),
    ]
    assert page.wait_args == {
        "arg": {"selector": "#progress", "target_time_seconds": 60},
        "timeout": 5_000,
    }
    assert action_trace[0]["result"] == "media_seek_confirmed"
    assert action_trace[0]["media_before"]["current_time"] == 10
    assert action_trace[0]["media_after"]["current_time"] == 60.4


async def test_seek_media_rejects_unseekable_target_before_pointer_action() -> None:
    class SeekMouse:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

    class SeekPage:
        def __init__(self) -> None:
            self.mouse = SeekMouse()

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            del script, argument
            return {"status": "blocked", "reason": "media_not_seekable"}

    page = SeekPage()

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=object(),
            request=PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="seek_media",
                selector="#progress",
                seek_fraction=0.5,
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "SEEK_MEDIA_UNSEEKABLE"
    assert page.mouse.calls == []


async def test_media_state_capture_is_bounded_and_keeps_display_candidates() -> None:
    class MediaPage:
        url = "https://example.com/video"

        async def evaluate(self, script: str) -> dict[str, Any]:
            del script
            return {
                "schema_version": "future",
                "url": self.url,
                "items": [
                    {
                        "media_index": index,
                        "selector": f"#video-{index}",
                        "current_time": index,
                        "duration": 100,
                    }
                    for index in range(25)
                ],
                "display_candidates": [{"selector": "#clock" + "x" * 600, "text": "01:00 / 02:00"}],
            }

    state = await _collect_media_state(MediaPage())

    assert state["schema_version"] == "media-state.v1"
    assert len(state["items"]) == 20
    assert state["display_candidates"][0]["text"] == "01:00 / 02:00"
    assert len(state["display_candidates"][0]["selector"]) == 512


def _visual_capture_request(**overrides: Any) -> PageCaptureRequest:
    values = {
        "mode": PageCaptureMode.SINGLE_TRIGGER,
        "trigger_action": "click",
        "selector": None,
        "click_x_ratio": 0.5,
        "click_y_ratio": 0.45,
        "left_ratio": 0.1,
        "top_ratio": 0.4,
        "right_ratio": 0.9,
        "bottom_ratio": 0.5,
        "visual_confidence": 0.92,
        "visual_source_url": "https://example.test/profile#source",
        "visual_entry_kind": "profile_vip_payment",
        "visual_expected_text": "立即开通",
        "screenshot_index": 1,
        **overrides,
    }
    return PageCaptureRequest(**values)


async def test_visual_coordinate_click_maps_full_page_ratio_once_and_traces() -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class VisualPage:
        def __init__(self) -> None:
            self.url = "https://example.test/profile#current"
            self.mouse = RecordingMouse()
            self.evaluate_calls: list[dict[str, float]] = []

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            assert "scrollTo" in script
            self.evaluate_calls.append(argument)
            return {
                "x": 195,
                "y": 422,
                "document_x": 195,
                "document_y": 760,
                "scroll_x": 0,
                "scroll_y": 338,
                "viewport_width": 390,
                "viewport_height": 844,
                "target_matched": True,
                "matched_depth": 0,
                "match_kind": "exact_text",
                "hit_tag": "button",
            }

    page = VisualPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=SimpleNamespace(),
        request=_visual_capture_request(),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.mouse.clicks == [(195, 422)]
    assert page.evaluate_calls == [
        {
            "xRatio": 0.5,
            "yRatio": 0.45,
            "leftRatio": 0.1,
            "topRatio": 0.4,
            "rightRatio": 0.9,
            "bottomRatio": 0.5,
            "expectedText": "立即开通",
            "entryKind": "profile_vip_payment",
        }
    ]
    assert action_trace == [
        {
            "action": "click",
            "selector": None,
            "resolved_by": "validated_visual_coordinate",
            "visual_entry_kind": "profile_vip_payment",
            "screenshot_index": 1,
            "visual_bounds": {
                "left_ratio": 0.1,
                "top_ratio": 0.4,
                "right_ratio": 0.9,
                "bottom_ratio": 0.5,
            },
            "visual_confidence": 0.92,
            "visual_target_verified": True,
            "visual_expected_text_sha256": hashlib.sha256("立即开通".encode()).hexdigest(),
            "visual_hit_tag": "button",
            "visual_match_kind": "exact_text",
            "visual_match_depth": 0,
            "document_point": {"x": 195, "y": 760},
            "viewport_point": {"x": 195, "y": 422},
            "scroll_offset": {"x": 0, "y": 338},
        }
    ]


async def test_visual_coordinate_click_accepts_profile_vip_banner_image_lineage() -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class VisualPage:
        url = "https://example.test/profile"

        def __init__(self) -> None:
            self.mouse = RecordingMouse()

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            assert "meTabPaymentBanner" in script
            assert "profile_vip_banner" in script
            assert argument["entryKind"] == "profile_vip_payment"
            return {
                "x": 318,
                "y": 257,
                "document_x": 318,
                "document_y": 257,
                "scroll_x": 0,
                "scroll_y": 0,
                "viewport_width": 390,
                "viewport_height": 844,
                "target_matched": True,
                "target_adjusted": False,
                "matched_depth": 5,
                "match_kind": "profile_vip_banner",
                "hit_tag": "img",
            }

    page = VisualPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=SimpleNamespace(),
        request=_visual_capture_request(),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.mouse.clicks == [(318, 257)]
    assert action_trace[0]["visual_match_kind"] == "profile_vip_banner"
    assert action_trace[0]["visual_match_depth"] == 5


@pytest.mark.parametrize(
    ("entry_kind", "target", "script_marker", "match_kind"),
    [
        (
            "profile_activity_ocr",
            "签到中心",
            "profileActivityContractFor",
            "profile_activity_contract",
        ),
        (
            "profile_settings_image",
            "设置",
            "profileSettingsContractFor",
            "profile_settings_contract",
        ),
    ],
)
async def test_visual_coordinate_click_accepts_registered_profile_visual_contract(
    entry_kind: str,
    target: str,
    script_marker: str,
    match_kind: str,
) -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class VisualPage:
        url = "https://example.test/profile"

        def __init__(self) -> None:
            self.mouse = RecordingMouse()

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            assert script_marker in script
            assert argument["entryKind"] == entry_kind
            return {
                "x": 330,
                "y": 82,
                "document_x": 330,
                "document_y": 82,
                "scroll_x": 0,
                "scroll_y": 0,
                "viewport_width": 390,
                "viewport_height": 844,
                "target_matched": True,
                "target_adjusted": False,
                "matched_depth": 0,
                "match_kind": match_kind,
                "hit_tag": "img",
            }

    page = VisualPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=SimpleNamespace(),
        request=_visual_capture_request(
            visual_entry_kind=entry_kind,
            visual_expected_text=target,
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.mouse.clicks == [(330, 82)]
    assert action_trace[0]["visual_match_kind"] == match_kind


async def test_visual_coordinate_click_rejects_arbitrary_profile_avatar_image() -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class VisualPage:
        url = "https://example.test/profile"

        def __init__(self) -> None:
            self.mouse = RecordingMouse()

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            assert "profileActivityContractFor" in script
            assert "profileSettingsContractFor" in script
            assert argument["entryKind"] == "profile_activity_ocr"
            return {
                "x": 40,
                "y": 70,
                "document_x": 40,
                "document_y": 70,
                "scroll_x": 0,
                "scroll_y": 0,
                "viewport_width": 390,
                "viewport_height": 844,
                "target_matched": False,
                "matched_depth": None,
                "match_kind": None,
                "hit_tag": "img",
            }

    page = VisualPage()
    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=SimpleNamespace(),
            request=_visual_capture_request(
                visual_entry_kind="profile_activity_ocr",
                visual_expected_text="签到中心",
            ),
            action_trace=[],
            timeout_ms=30000,
        )

    assert error.value.error_code == "VISUAL_TARGET_NOT_AT_COORDINATE"
    assert page.mouse.clicks == []


async def test_visual_coordinate_click_rejects_wrong_live_target_before_mouse_click() -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class VisualPage:
        url = "https://example.test/profile"

        def __init__(self) -> None:
            self.mouse = RecordingMouse()

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            del script, argument
            return {
                "x": 195,
                "y": 380,
                "document_x": 195,
                "document_y": 760,
                "scroll_x": 0,
                "scroll_y": 380,
                "viewport_width": 390,
                "viewport_height": 844,
                "target_matched": False,
                "matched_depth": None,
                "hit_tag": "div",
            }

    page = VisualPage()

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=SimpleNamespace(),
            request=_visual_capture_request(),
            action_trace=[],
            timeout_ms=30000,
        )

    assert error.value.error_code == "VISUAL_TARGET_NOT_AT_COORDINATE"
    assert page.mouse.clicks == []


async def test_visual_coordinate_click_uses_unique_nearby_exact_live_target() -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class VisualPage:
        url = "https://example.test/profile"

        def __init__(self) -> None:
            self.mouse = RecordingMouse()

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            assert "nearbyMatches" in script
            del argument
            return {
                "x": 328,
                "y": 209,
                "document_x": 328,
                "document_y": 209,
                "scroll_x": 0,
                "scroll_y": 0,
                "viewport_width": 390,
                "viewport_height": 844,
                "target_matched": True,
                "target_adjusted": True,
                "matched_depth": 0,
                "match_kind": "exact_text",
                "hit_tag": "button",
            }

    page = VisualPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=SimpleNamespace(),
        request=_visual_capture_request(),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.mouse.clicks == [(328, 209)]
    assert action_trace[0]["visual_coordinate_adjusted"] is True


async def test_visual_coordinate_click_uses_unique_live_payment_banner_when_bounds_drift() -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    class VisualPage:
        url = "https://example.test/profile"

        def __init__(self) -> None:
            self.mouse = RecordingMouse()

        async def evaluate(self, script: str, argument: dict[str, Any]) -> dict[str, Any]:
            assert "paymentBannerMatches" in script
            del argument
            return {
                "x": 309,
                "y": 238,
                "document_x": 309,
                "document_y": 238,
                "scroll_x": 0,
                "scroll_y": 0,
                "viewport_width": 390,
                "viewport_height": 844,
                "target_matched": True,
                "target_adjusted": True,
                "matched_depth": 1,
                "match_kind": "profile_vip_banner",
                "hit_tag": "img",
            }

    page = VisualPage()
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=SimpleNamespace(),
        request=_visual_capture_request(),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert page.mouse.clicks == [(309, 238)]
    assert action_trace[0]["visual_coordinate_adjusted"] is True
    assert action_trace[0]["visual_match_kind"] == "profile_vip_banner"


@pytest.mark.parametrize(
    "payment_html",
    [
        (
            '<div class="vip-img" onclick="window.clicked=true">'
            '<img class="vip-right-btn" alt="VIP按钮" '
            'style="position:absolute;right:8px;top:12px;width:94px;height:40px">'
            "</div>"
        ),
        (
            '<div class="payment_me_tab"><div class="vip_box" '
            'onclick="window.clicked=true"><div class="vipBoxsBgImg">'
            '<img style="width:100%;height:100%"></div></div></div>'
        ),
        (
            '<div class="vip_box payment_me_tab" onclick="window.clicked=true">'
            '<div class="vipBoxsBgImg">'
            '<img style="width:100%;height:100%"></div></div>'
        ),
    ],
)
async def test_visual_coordinate_click_revalidates_site_payment_image_structures(
    payment_html: str,
) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = f"""
    <style>
      html, body {{ margin: 0; width: 390px; height: 844px; }}
      .vip-img, .vip_box {{
        position: absolute; left: 20px; top: 100px; width: 350px; height: 80px;
      }}
      .vipBoxsBgImg {{ width: 100%; height: 100%; }}
    </style>
    <script>window.clicked = false;</script>
    {payment_html}
    """
    source_url = "https://example.test/profile#source"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})

        async def fulfill(route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route("**/*", fulfill)
        await page.goto(source_url)
        action_trace: list[dict[str, Any]] = []

        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_capture_request(visual_source_url=source_url),
            action_trace=action_trace,
            timeout_ms=30000,
        )

        assert await page.evaluate("window.clicked") is True
        assert action_trace[0]["visual_coordinate_adjusted"] is True
        assert action_trace[0]["visual_match_kind"] == "profile_vip_banner"
        await browser.close()


@pytest.mark.parametrize(
    ("entry_kind", "target", "request_overrides", "expected_kind", "clicked"),
    [
        (
            "profile_activity_ocr",
            "签到中心",
            {
                "click_x_ratio": 0.5,
                "click_y_ratio": 0.45,
                "left_ratio": 0.3,
                "top_ratio": 0.35,
                "right_ratio": 0.7,
                "bottom_ratio": 0.55,
            },
            "profile_activity_contract",
            "activity",
        ),
        (
            "profile_settings_image",
            "设置",
            {
                "click_x_ratio": 0.88,
                "click_y_ratio": 0.055,
                "left_ratio": 0.78,
                "top_ratio": 0.0,
                "right_ratio": 0.98,
                "bottom_ratio": 0.11,
            },
            "profile_settings_contract",
            "settings",
        ),
    ],
)
async def test_visual_coordinate_click_revalidates_profile_activity_and_settings_contracts(
    entry_kind: str,
    target: str,
    request_overrides: dict[str, float],
    expected_kind: str,
    clicked: str,
) -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <meta charset="utf-8">
    <style>
      html, body { margin: 0; width: 390px; height: 844px; }
      .checkin-entry {
        position: absolute; left: 140px; top: 340px; width: 140px; height: 100px;
      }
      .checkin-entry-text { width: 100%; height: 100%; }
      .index-header { position: absolute; inset: 0 0 auto 0; height: 80px; }
      .header-left { position: absolute; left: 300px; top: 30px; width: 70px; height: 24px; }
      .header-left img { position: absolute; top: 0; width: 24px; height: 24px; }
      .header-left img:first-child { left: 0; }
      .header-left img:last-child { left: 40px; }
    </style>
    <script>document.addEventListener('click', (event) => {
      const target = event.target.closest('[data-clicked]');
      if (target) document.body.dataset.clicked = target.dataset.clicked;
    });</script>
    <div class="checkin-entry" data-clicked="activity">
      <img class="checkin-entry-icon"><div class="checkin-entry-text">签到中心</div>
    </div>
    <div class="index-header"><div class="header-left">
      <img class="mr-15 wh-24"><img class="wh-24" alt="设置" data-clicked="settings">
    </div></div>
    """
    source_url = "https://example.test/profile#source"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})

        async def fulfill(route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route("**/*", fulfill)
        await page.goto(source_url)
        action_trace: list[dict[str, Any]] = []
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_capture_request(
                visual_source_url=source_url,
                visual_entry_kind=entry_kind,
                visual_expected_text=target,
                **request_overrides,
            ),
            action_trace=action_trace,
            timeout_ms=30000,
        )

        assert await page.evaluate("document.body.dataset.clicked") == clicked
        assert action_trace[0]["visual_match_kind"] == expected_kind
        await browser.close()


async def test_visual_coordinate_click_rejects_profile_avatar_in_real_browser() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <meta charset="utf-8">
    <style>
      html, body { margin: 0; width: 390px; height: 844px; }
      .avatar { position: absolute; left: 20px; top: 40px; width: 80px; height: 80px; }
    </style>
    <script>window.avatarClicked = false;</script>
    <img class="avatar" alt="签到中心" onclick="window.avatarClicked = true">
    """
    source_url = "https://example.test/profile#source"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 390, "height": 844})

        async def fulfill(route) -> None:
            await route.fulfill(status=200, content_type="text/html", body=html)

        await page.route("**/*", fulfill)
        await page.goto(source_url)
        with pytest.raises(BlockedPageCaptureActionError) as error:
            await _run_single_trigger(
                page=page,
                context=page.context,
                request=_visual_capture_request(
                    visual_source_url=source_url,
                    visual_entry_kind="profile_activity_ocr",
                    visual_expected_text="签到中心",
                    click_x_ratio=0.15,
                    click_y_ratio=0.095,
                    left_ratio=0.0,
                    top_ratio=0.03,
                    right_ratio=0.3,
                    bottom_ratio=0.16,
                ),
                action_trace=[],
                timeout_ms=30000,
            )

        assert error.value.error_code == "VISUAL_TARGET_NOT_AT_COORDINATE"
        assert await page.evaluate("window.avatarClicked") is False
        await browser.close()


async def test_visual_coordinate_click_rejects_stale_page_before_mouse_click() -> None:
    class RecordingMouse:
        def __init__(self) -> None:
            self.clicks: list[tuple[float, float]] = []

        async def click(self, x: float, y: float) -> None:
            self.clicks.append((x, y))

    page = SimpleNamespace(
        url="https://example.test/other",
        mouse=RecordingMouse(),
    )

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=SimpleNamespace(),
            request=_visual_capture_request(),
            action_trace=[],
            timeout_ms=30000,
        )

    assert error.value.error_code == "VISUAL_SOURCE_PAGE_STALE"
    assert page.mouse.clicks == []


@pytest.mark.parametrize(
    "capture_request",
    [
        _visual_capture_request(selector="#open-now"),
        _visual_capture_request(bottom_ratio=None),
        _visual_capture_request(trigger_action="hover"),
        _visual_capture_request(visual_expected_text="其他入口"),
    ],
)
def test_visual_coordinate_request_requires_complete_click_only_xor_contract(
    capture_request: PageCaptureRequest,
) -> None:
    with pytest.raises(BlockedPageCaptureActionError, match="视觉坐标"):
        _validate_request(capture_request)


@pytest.mark.parametrize(
    "capture_request",
    [
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="seek_media",
            selector="#progress",
        ),
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="seek_media",
            selector="#progress",
            seek_fraction=0.5,
            seek_time_seconds=30,
        ),
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="click",
            selector="#progress",
            seek_fraction=0.5,
        ),
    ],
)
def test_seek_media_request_rejects_ambiguous_or_misbound_parameters(
    capture_request: PageCaptureRequest,
) -> None:
    with pytest.raises(BlockedPageCaptureActionError, match="seek_"):
        _validate_request(capture_request)


@pytest.mark.parametrize(
    "capture_request",
    [
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            capture_transient_messages=True,
        ),
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="click",
            selector="#save",
            capture_transient_messages="yes",
        ),
    ],
)
def test_transient_message_monitor_must_be_boolean_and_action_bound(
    capture_request: PageCaptureRequest,
) -> None:
    with pytest.raises((ValueError, BlockedPageCaptureActionError), match="瞬态|布尔"):
        _validate_request(capture_request)


async def test_network_fault_only_aborts_matching_requests_and_restores_route() -> None:
    class FaultRoute:
        def __init__(self, url: str) -> None:
            self.request = SimpleNamespace(url=url)
            self.action: str | None = None

        async def abort(self) -> None:
            self.action = "abort"

        async def continue_(self) -> None:
            self.action = "continue"

    class FaultContext:
        def __init__(self) -> None:
            self.handler = None
            self.unroute_calls: list[tuple[str, object]] = []

        async def route(self, pattern: str, handler: object) -> None:
            assert pattern == "**/*"
            self.handler = handler

        async def unroute(self, pattern: str, handler: object) -> None:
            self.unroute_calls.append((pattern, handler))
            self.handler = None

    class FaultPage:
        def __init__(self, context: FaultContext) -> None:
            self.context = context
            self.routes = [
                FaultRoute("https://cdn.example.com/app.js"),
                FaultRoute("https://ads.example.com/pre-roll.mp4?token=secret#fragment"),
            ]

        async def reload(self, **_: Any) -> None:
            assert self.context.handler is not None
            for route in self.routes:
                await self.context.handler(route)

    context = FaultContext()
    page = FaultPage(context)
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=context,
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="network_fault",
            fault_url_pattern=r"ads\.example\.com/",
        ),
        action_trace=action_trace,
        timeout_ms=30000,
    )

    assert [route.action for route in page.routes] == ["continue", "abort"]
    assert len(context.unroute_calls) == 1
    assert context.handler is None
    assert action_trace[0]["matched_request_count"] == 1
    assert action_trace[0]["matched_request_urls"] == ["https://ads.example.com/pre-roll.mp4"]
    assert r"ads\.example\.com/" not in json.dumps(action_trace)
    assert action_trace[0]["fault_url_pattern_length"] == len(r"ads\.example\.com/")
    assert action_trace[0]["route_restored"] is True


@pytest.mark.parametrize(
    "capture_request, message",
    [
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="submit_probe",
                selector="textarea",
                submit_selector="button",
                probe_text="普通评论",
            ),
            "有界中性文本",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="submit_probe",
                selector="textarea",
                submit_selector="button",
                probe_text="ALIOTH_RATE_PROBE_run_001",
                navigation_depth=1,
            ),
            "不允许声明跨页导航",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="network_fault",
                fault_url_pattern=".*",
            ),
            "不得匹配全部请求",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="network_fault",
                fault_url_pattern=r"https?://",
            ),
            "不得匹配全部请求",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector="button",
                probe_text="ALIOTH_RATE_PROBE_run_001",
            ),
            "仅允许用于 submit_probe",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector="button",
                fault_url_pattern=r"ads\.example\.com/",
            ),
            "仅允许用于 network_fault",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.GOTO,
                url="https://example.com",
                probe_text="ALIOTH_RATE_PROBE_run_001",
            ),
            "仅允许用于 single_trigger",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="fill",
                selector="textarea",
            ),
            "必须提供非空 input_text",
        ),
        (
            PageCaptureRequest(
                mode=PageCaptureMode.SINGLE_TRIGGER,
                trigger_action="click",
                selector="button",
                input_text="不应被 click 接收",
            ),
            "仅允许用于 fill",
        ),
    ],
)
def test_sensitive_trigger_contract_rejects_unsafe_parameters(
    capture_request: PageCaptureRequest,
    message: str,
) -> None:
    with pytest.raises(BlockedPageCaptureActionError, match=message):
        _validate_request(capture_request)


def test_page_capture_tool_is_registered_as_sensitive() -> None:
    metadata = default_registry.get("page_capture_tool")

    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SENSITIVE


def test_playwright_browser_class_is_importable_without_running_browser() -> None:
    browser = PlaywrightPageCaptureBrowser()

    assert browser.default_timeout_ms == 30000


def test_page_capture_tool_schema_restricts_mode_and_uses_safe_timeout() -> None:
    parameters = page_capture_tool.parameters()

    mode_description = parameters["properties"]["mode"]["description"]
    assert parameters["properties"]["mode"]["default"] == "goto"
    assert all(
        mode in mode_description for mode in ("goto", "observe", "timed_capture", "single_trigger")
    )
    trigger_action = parameters["properties"]["trigger_action"]
    action_enum = next(item["enum"] for item in trigger_action["anyOf"] if "enum" in item)
    assert action_enum == [
        "click",
        "back",
        "scroll",
        "hover",
        "network_fault",
        "submit_probe",
    ]
    assert "浏览器历史返回" in trigger_action["description"]
    assert "click/hover 目标放 selector" in trigger_action["description"]
    assert "network_fault 目标放 fault_url_pattern" in trigger_action["description"]
    assert "submit_probe 另传" in trigger_action["description"]
    assert "click text=关闭" in trigger_action["description"]
    assert "probe_text" in parameters["properties"]
    assert "submit_selector" in parameters["properties"]
    assert "fault_url_pattern" in parameters["properties"]
    selector_description = parameters["properties"]["selector"]["description"]
    assert "trigger_action='click'" in selector_description
    assert "selector='text=关闭'" in selector_description
    assert parameters["properties"]["timeout_ms"]["default"] == 30000
    assert "exploration_action_id" in parameters["properties"]
    assert "supersedes_page_capture_id" in parameters["properties"]


async def test_capture_page_for_agent_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError, match="navigate"):
        await capture_page_for_agent(mode="navigate", url="https://example.com")


async def test_invalid_mode_is_recorded_as_action_trace_error() -> None:
    settings = AgentRuntimeSettings(max_steps=5, max_iterations=5, max_duration_seconds=5)
    client = ScriptedChatClient(
        [
            make_function_call_response(
                "invalid-mode",
                "page_capture_tool",
                '{"mode":"navigate","url":"https://example.com"}',
            ),
            make_text_response("参数错误，已停止页面采集。"),
        ]
    )
    agent = build_main_agent(
        client=client,
        instructions="test",
        runtime_settings=settings,
        tools=[page_capture_tool],
    )
    sink = InMemoryActionTraceSink()

    await run_main_agent(agent, "测试非法模式", runtime_settings=settings, sink=sink)

    assert len(sink.entries) == 1
    assert sink.entries[0].status == "error"
    assert "navigate" in (sink.entries[0].error_message or "")


async def test_page_navigation_retries_once_after_timeout() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self) -> None:
            self.attempts = 0

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise NavigationTimeoutError("首次导航超时")
            return f"{url}:{wait_until}:{timeout}"

    page = FakePage()
    audit: dict[str, Any] = {}

    response = await _goto_with_retry(
        page=page,
        url="https://example.com",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
        audit=audit,
    )

    assert response == "https://example.com:domcontentloaded:15000"
    assert page.attempts == 2
    assert audit == {
        "schema_version": "navigation_retry.v1",
        "max_attempts": 2,
        "attempt_count": 2,
        "retry_count": 1,
        "retry_reasons": ["timeout"],
        "outcome": "succeeded_after_retry",
    }


async def test_page_navigation_keeps_inspectable_document_after_dom_timeout() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class InspectableCommittedPage:
        def __init__(self) -> None:
            self.attempts = 0
            self.url = "about:blank"

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            del wait_until, timeout
            self.attempts += 1
            self.url = url
            raise NavigationTimeoutError("DOMContentLoaded 长时间未触发")

        async def content(self) -> str:
            return "<html><head><title>PH</title></head><body><div id='app'></div></body></html>"

    page = InspectableCommittedPage()

    response = await _goto_with_retry(
        page=page,
        url="https://example.com/app",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
    )

    assert response is None
    assert page.attempts == 1


async def test_page_navigation_retries_blank_committed_document_after_dom_timeout() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class Page:
        def __init__(self) -> None:
            self.attempts = 0
            self.url = "about:blank"

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str | None:
            del wait_until, timeout
            self.attempts += 1
            self.url = url
            if self.attempts == 1:
                raise NavigationTimeoutError("DOMContentLoaded 长时间未触发")
            return url

        async def content(self) -> str:
            return "<html><head></head><body></body></html>"

    async def validate(page: Page) -> str | None:
        return "blank_document" if page.attempts == 1 else None

    page = Page()
    audit: dict[str, Any] = {}

    response = await _goto_with_retry(
        page=page,
        url="https://example.com/app",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
        audit=audit,
        post_navigation_validator=validate,
    )

    assert response == "https://example.com/app"
    assert page.attempts == 2
    assert audit["retry_reasons"] == ["blank_document"]
    assert audit["outcome"] == "succeeded_after_retry"


async def test_page_navigation_retry_shares_total_navigation_budget() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class SlowPage:
        def __init__(self) -> None:
            self.timeouts: list[int] = []

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:
            self.timeouts.append(timeout)
            await asyncio.sleep(timeout / 1_000)
            if len(self.timeouts) == 1:
                raise NavigationTimeoutError("首次导航超时")
            return f"{url}:{wait_until}"

    page = SlowPage()
    response = await asyncio.wait_for(
        _goto_with_retry(
            page=page,
            url="https://example.com",
            timeout_ms=120,
            timeout_error_type=NavigationTimeoutError,
        ),
        timeout=0.18,
    )

    assert response == "https://example.com:domcontentloaded"
    assert page.timeouts == [60, 60]


async def test_page_navigation_retries_once_after_connection_closed() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self) -> None:
            self.attempts = 0

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError(f"Page.goto: net::ERR_CONNECTION_CLOSED at {url}")
            return f"{url}:{wait_until}:{timeout}"

    page = FakePage()
    audit: dict[str, Any] = {}

    response = await _goto_with_retry(
        page=page,
        url="https://example.com",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
        audit=audit,
    )

    assert response == "https://example.com:domcontentloaded:15000"
    assert page.attempts == 2
    assert audit["retry_count"] == 1
    assert audit["retry_reasons"] == ["err_connection_closed"]
    assert audit["outcome"] == "succeeded_after_retry"


async def test_page_navigation_retries_once_when_settled_document_is_blank() -> None:
    """DOMContentLoaded 成功但业务文档仍为空时，必须在同一导航预算内重试一次。"""

    class NavigationTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self) -> None:
            self.attempts = 0

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:
            self.attempts += 1
            return f"{url}:{wait_until}:{timeout}:attempt-{self.attempts}"

    async def validate(page: FakePage) -> str | None:
        return "blank_document" if page.attempts == 1 else None

    page = FakePage()
    audit: dict[str, Any] = {}

    response = await _goto_with_retry(
        page=page,
        url="https://example.com",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
        audit=audit,
        post_navigation_validator=validate,
    )

    assert response.endswith("attempt-2")
    assert page.attempts == 2
    assert audit["retry_reasons"] == ["blank_document"]
    assert audit["outcome"] == "succeeded_after_retry"


async def test_page_navigation_retries_after_browser_error_document() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self) -> None:
            self.attempts = 0

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError(
                    f'Page.goto: Navigation to "{url}" is interrupted by another '
                    'navigation to "chrome-error://chromewebdata/"'
                )
            return f"{url}:{wait_until}:{timeout}"

    page = FakePage()
    audit: dict[str, Any] = {}

    response = await _goto_with_retry(
        page=page,
        url="https://example.com",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
        audit=audit,
    )

    assert response == "https://example.com:domcontentloaded:15000"
    assert page.attempts == 2
    assert audit["retry_reasons"] == ["browser_error_document"]
    assert audit["outcome"] == "succeeded_after_retry"


async def test_page_navigation_retries_after_browser_error_document_without_is() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self) -> None:
            self.attempts = 0

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError(
                    f'Page.goto: Navigation to "{url}" interrupted by another '
                    'navigation to "chrome-error://chromewebdata/"'
                )
            return f"{url}:{wait_until}:{timeout}"

    page = FakePage()
    audit: dict[str, Any] = {}

    response = await _goto_with_retry(
        page=page,
        url="https://example.com",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
        audit=audit,
    )

    assert response == "https://example.com:domcontentloaded:15000"
    assert page.attempts == 2
    assert audit["retry_reasons"] == ["browser_error_document"]
    assert audit["outcome"] == "succeeded_after_retry"


async def test_page_navigation_retry_waits_for_late_browser_error_document() -> None:
    """错误文档迟到时，第二次导航要等它提交后再开始，否则会被它打断而失败。"""

    class NavigationTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self) -> None:
            self.attempts = 0
            self.waits = 0
            self.url = "about:blank"

        async def wait_for_timeout(self, timeout: int) -> None:
            self.waits += 1
            if self.waits == 4:
                self.url = "chrome-error://chromewebdata/"

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> str:
            self.attempts += 1
            if self.attempts == 1:
                raise RuntimeError(f"Page.goto: net::ERR_EMPTY_RESPONSE at {url}")
            if not self.url.startswith("chrome-error://"):
                raise RuntimeError(
                    f'Page.goto: Navigation to "{url}" is interrupted by another '
                    'navigation to "chrome-error://chromewebdata/"'
                )
            self.url = url
            return f"{url}:{wait_until}:{timeout}"

    page = FakePage()
    audit: dict[str, Any] = {}

    response = await _goto_with_retry(
        page=page,
        url="https://example.com",
        timeout_ms=30000,
        timeout_error_type=NavigationTimeoutError,
        audit=audit,
    )

    assert response == "https://example.com:domcontentloaded:15000"
    assert page.attempts == 2
    assert audit["retry_reasons"] == ["err_empty_response"]
    assert audit["outcome"] == "succeeded_after_retry"


async def test_page_navigation_does_not_retry_non_transient_navigation_error() -> None:
    class NavigationTimeoutError(Exception):
        pass

    class FakePage:
        def __init__(self) -> None:
            self.attempts = 0

        async def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
            self.attempts += 1
            raise RuntimeError(f"Page.goto: net::ERR_CERT_AUTHORITY_INVALID at {url}")

    page = FakePage()

    with pytest.raises(RuntimeError, match="ERR_CERT_AUTHORITY_INVALID"):
        await _goto_with_retry(
            page=page,
            url="https://example.com",
            timeout_ms=30000,
            timeout_error_type=NavigationTimeoutError,
        )

    assert page.attempts == 1


def _stable_page_readiness() -> dict[str, Any]:
    return {
        "core_present": True,
        "necessary_media_ready": True,
        "visible_loader": False,
        "transition_active": False,
        "flutter_bootstrap_pending": False,
        "dom_quiet_ms": 1000,
    }


async def test_page_settle_waits_until_visible_text_stops_changing(monkeypatch) -> None:
    class RedirectingPage:
        """模拟中转页：文本先变化两次（加载壳→跳转后内容），随后趋于稳定。"""

        def __init__(self) -> None:
            self.texts = [
                "正在读取最快线路",
                "正在获取最佳线路",
                "正文内容",
                "正文内容",
                "正文内容",
            ]
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            index = min(self.calls, len(self.texts) - 1)
            self.calls += 1
            return self.texts[index]

    async def read_readiness(page: Any) -> dict[str, Any]:
        return _stable_page_readiness()

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    page = RedirectingPage()

    await _wait_for_page_settle(page=page, max_wait_ms=1000, poll_interval_ms=0)

    assert page.calls == 5


async def test_page_settle_does_not_treat_stable_line_loading_as_terminal(monkeypatch) -> None:
    class DelayedLineLoadingPage:
        def __init__(self) -> None:
            self.texts = [
                "正在读取最快路线",
                "正在读取最快路线",
                "正在读取最快路线",
                "首页内容",
                "首页内容",
                "首页内容",
            ]
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            index = min(self.calls, len(self.texts) - 1)
            self.calls += 1
            return self.texts[index]

    async def read_readiness(page: Any) -> dict[str, Any]:
        return _stable_page_readiness()

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    page = DelayedLineLoadingPage()

    await _wait_for_page_settle(page=page, max_wait_ms=1000, poll_interval_ms=0)

    assert page.calls == 6


async def test_read_page_readiness_treats_zero_rect_core_wrapper_as_present() -> None:
    """首个匹配的核心容器是零盒（display:contents 等）时，只要页面已渲染可见内容，
    就不能误报 core 缺失——否则图片型落地页会被 core_content_missing 饿死探索。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>.content { display: contents; }</style>
    <div class="content">
      <h1>海角社区</h1>
      <img src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
           width="200" height="120">
      <a href="#download">下载</a>
    </div>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            readiness = await page_capture_module._read_page_readiness(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert readiness is not None
    assert readiness["document_ready_state"] == "complete"
    assert readiness["core_present"] is True
    assert readiness["visible_media_count"] == 1
    assert readiness["ready_media_count"] == 1
    assert readiness["visible_loader_count"] == 0
    assert readiness["blocking_loader_count"] == 0
    assert readiness["active_transition_count"] == 0
    assert readiness["blocking_transition_count"] == 0


async def test_read_page_readiness_rejects_empty_body_as_blank_document() -> None:
    """body 的视口矩形不是业务内容；纯空白文档不得签发 core_present。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(
                "<html><head><style>body{margin:0}</style></head><body></body></html>"
            )
            readiness = await page_capture_module._read_page_readiness(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert readiness is not None
    assert readiness["blank_document"] is True
    assert readiness["core_present"] is False


async def test_page_readiness_ignores_hidden_and_offscreen_media() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      body { margin: 0; }
      img { width: 120px; height: 120px; }
      #hidden { visibility: hidden; }
      #transparent { opacity: 0; }
      #offscreen { position: fixed; left: 1000px; top: 20px; }
    </style>
    <main>
      <img
        id="visible"
        src="data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw=="
      >
      <img id="hidden" src="/unavailable-hidden.png">
      <img id="transparent" src="/unavailable-transparent.png">
      <img id="offscreen" src="/unavailable-offscreen.png">
    </main>
    """

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content(html)
            readiness = await page_capture_module._read_page_readiness(page)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert readiness is not None
    assert readiness["visible_media_count"] == 1
    assert readiness["ready_media_count"] == 1
    assert readiness["necessary_media_ready"] is True


async def test_page_settle_waits_for_visible_loader_to_disappear(monkeypatch) -> None:
    class LoadingPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            return "首页内容"

    readiness = [
        {
            "core_present": True,
            "necessary_media_ready": True,
            "visible_loader": True,
            "transition_active": False,
            "dom_quiet_ms": 1000,
        },
        {
            "core_present": True,
            "necessary_media_ready": True,
            "visible_loader": False,
            "transition_active": False,
            "dom_quiet_ms": 1000,
        },
    ]

    async def install_monitor(page: Any) -> int:
        return 0

    async def read_readiness(page: Any) -> dict[str, Any]:
        return readiness.pop(0) if len(readiness) > 1 else readiness[0]

    async def wait_for_change(**kwargs: Any) -> int:
        return int(kwargs["previous_revision"])

    monkeypatch.setattr(page_capture_module, "_install_page_state_monitor", install_monitor)
    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_state_change", wait_for_change)
    page = LoadingPage()

    await _wait_for_page_settle(
        page=page,
        max_wait_ms=1000,
        poll_interval_ms=0,
        stable_samples=2,
    )

    assert page.calls == 3


async def test_page_settle_waits_until_document_is_interactive(monkeypatch) -> None:
    class LoadingDocumentPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            return "首页内容"

    readiness = [
        {**_stable_page_readiness(), "document_ready_state": "loading"},
        {**_stable_page_readiness(), "document_ready_state": "interactive"},
    ]

    async def read_readiness(page: Any) -> dict[str, Any]:
        return readiness.pop(0) if len(readiness) > 1 else readiness[0]

    async def wait_for_change(**kwargs: Any) -> int:
        return int(kwargs["previous_revision"])

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_state_change", wait_for_change)
    page = LoadingDocumentPage()

    settled = await _wait_for_page_settle(
        page=page,
        max_wait_ms=1000,
        poll_interval_ms=0,
        stable_samples=1,
    )

    assert settled is True
    assert page.calls == 2


async def test_page_settle_does_not_block_on_unrelated_stylesheet_and_font_requests(
    monkeypatch,
) -> None:
    """无关 font/stylesheet 在途不得单独阻断合法 stable settle。"""

    class StyledPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            return "首页内容"

    network_inflight = {
        1: {"resource_type": "stylesheet", "url": "https://example.com/app.css"},
        2: {"resource_type": "font", "url": "https://example.com/app.woff2"},
    }

    async def read_readiness(page: Any) -> dict[str, Any]:
        return _stable_page_readiness()

    async def wait_for_change(**kwargs: Any) -> int:
        # settle 循环首轮需要推进采样；不得要求清掉无关样式/字体。
        return int(kwargs["previous_revision"]) + 1

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_state_change", wait_for_change)
    page = StyledPage()

    settled = await _wait_for_page_settle(
        page=page,
        network_inflight=network_inflight,
        max_wait_ms=1000,
        poll_interval_ms=0,
        stable_samples=1,
    )

    assert settled is True
    assert network_inflight  # 仍在途，但未阻断


async def test_page_settle_still_waits_for_critical_script_request(monkeypatch) -> None:
    class ScriptPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            return "首页内容"

    network_inflight = {
        1: {"resource_type": "script", "url": "https://example.com/app.js"},
    }

    async def read_readiness(page: Any) -> dict[str, Any]:
        return _stable_page_readiness()

    async def wait_for_change(**kwargs: Any) -> int:
        network_inflight.clear()
        return int(kwargs["previous_revision"])

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_state_change", wait_for_change)
    page = ScriptPage()

    settled = await _wait_for_page_settle(
        page=page,
        network_inflight=network_inflight,
        max_wait_ms=1000,
        poll_interval_ms=0,
        stable_samples=1,
    )

    assert settled is True
    assert page.calls == 2


async def test_page_settle_keeps_startup_ad_countdown_evidence_without_waiting_away() -> None:
    class StartupAdPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            return "开屏广告 5秒后跳过"

    page = StartupAdPage()

    await _wait_for_page_settle(page=page, max_wait_ms=1000, poll_interval_ms=0)

    assert page.calls == 1


async def test_page_settle_treats_completed_line_shell_with_close_as_startup_ad() -> None:
    class CloseLocator:
        async def evaluate_all(self, expression: str) -> bool:
            assert "node.closest('.loading-page')" in expression
            assert "rect.right >= innerWidth - 2" in expression
            return True

    class CompletedLineStartupPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            return "关闭\n线路读取完成，请稍后\n广告内容"

        def locator(self, selector: str) -> CloseLocator:
            assert selector == ".loading-page .down-time:visible"
            return CloseLocator()

    page = CompletedLineStartupPage()

    settled = await _wait_for_page_settle(page=page, max_wait_ms=1000, poll_interval_ms=0)

    assert settled is True
    assert page.calls == 1


async def test_page_settle_gives_up_after_max_wait_when_text_keeps_changing() -> None:
    class CountdownPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            return f"动态内容 {self.calls}"

    page = CountdownPage()

    settled = await _wait_for_page_settle(
        page=page,
        max_wait_ms=50,
        poll_interval_ms=10,
    )

    assert page.calls >= 1
    assert settled is False


async def test_page_settle_fails_closed_when_readiness_is_unavailable(monkeypatch) -> None:
    class UnobservablePage:
        url = "https://example.com/home"

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            return "稳定首页内容"

    async def install_monitor(page: Any) -> int:
        return 0

    async def read_readiness(page: Any) -> None:
        return None

    async def wait_for_change(**kwargs: Any) -> int:
        await asyncio.sleep(0.001)
        return int(kwargs["previous_revision"])

    monkeypatch.setattr(page_capture_module, "_install_page_state_monitor", install_monitor)
    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_state_change", wait_for_change)

    diagnostics: dict[str, Any] = {}
    settled = await _wait_for_page_settle(
        page=UnobservablePage(),
        max_wait_ms=10,
        poll_interval_ms=1,
        stable_samples=1,
        diagnostics=diagnostics,
    )

    assert settled is False
    assert diagnostics["outcome"] == "settle_timeout"


async def test_page_settle_quorum_does_not_cross_page_identity(monkeypatch) -> None:
    class NavigatingPage:
        def __init__(self) -> None:
            self.calls = 0
            self.url = "https://example.com/old"

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            if self.calls >= 2:
                self.url = "https://example.com/new"
            return "相同页面文案"

    async def install_monitor(page: Any) -> int:
        return 0

    async def read_readiness(page: Any) -> dict[str, Any]:
        return _stable_page_readiness()

    async def wait_for_change(**kwargs: Any) -> int:
        return int(kwargs["previous_revision"])

    monkeypatch.setattr(page_capture_module, "_install_page_state_monitor", install_monitor)
    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_state_change", wait_for_change)
    page = NavigatingPage()

    diagnostics: dict[str, Any] = {}
    settled = await _wait_for_page_settle(
        page=page,
        max_wait_ms=1000,
        poll_interval_ms=0,
        stable_samples=2,
        diagnostics=diagnostics,
    )

    assert settled is True
    assert page.calls == 4
    assert diagnostics["page_url_sha256"] == (
        "sha256:" + hashlib.sha256(b"https://example.com/new").hexdigest()
    )
    assert diagnostics["stable_sample_count"] == 2
    assert "page_url_stable" in diagnostics["satisfied_signals"]


async def test_unsettled_dynamic_page_with_dense_evidence_remains_inspectable(
    monkeypatch,
) -> None:
    async def read_readiness(page: Any) -> dict[str, Any]:
        return {
            "core_present": True,
            "necessary_media_ready": False,
            "visible_loader": True,
            "blocking_loader": False,
            "transition_active": True,
            "blocking_transition": False,
            "dom_quiet_ms": 120,
        }

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    artifacts = [
        PageCaptureArtifact("dom", None, "<main><section>商品列表</section></main>"),
        PageCaptureArtifact("visible_text", None, "商品列表 推荐内容 热门内容"),
        PageCaptureArtifact("screenshot", None, {"encoding": "base64", "data": "aW1hZ2U="}),
        PageCaptureArtifact(
            "ui_snapshot",
            None,
            {"elements": [{"tag": "div", "text": str(index)} for index in range(20)]},
        ),
    ]

    ready, diagnostics = await _assess_page_inspection_readiness(
        page=object(),
        artifacts=artifacts,
        network_inflight={1: {"resource_type": "xhr", "url": "/poll"}},
        wait_diagnostics={
            "sample_count": 5,
            "stable_sample_count": 1,
            "page_url_sha256": "sha256:page-identity",
        },
    )

    assert ready is True
    assert diagnostics["outcome"] == "inspectable_dynamic"
    assert diagnostics["sample_count"] == 5
    assert diagnostics["stable_sample_count"] == 1
    assert diagnostics["page_url_sha256"] == "sha256:page-identity"
    assert diagnostics["blocking_reasons"] == []
    assert diagnostics["dynamic_reasons"] == [
        "localized_loader_visible",
        "localized_transition_active",
        "visible_media_still_loading",
        "dom_continues_changing",
        "background_requests_pending",
    ]
    assert diagnostics["evidence"]["ui_element_count"] == 20


async def test_unsettled_sparse_page_with_blocking_loader_is_not_inspectable(
    monkeypatch,
) -> None:
    async def read_readiness(page: Any) -> dict[str, Any]:
        return {
            "core_present": True,
            "necessary_media_ready": True,
            "visible_loader": True,
            "blocking_loader": True,
            "transition_active": False,
            "blocking_transition": False,
            "dom_quiet_ms": 1000,
        }

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    artifacts = [
        PageCaptureArtifact("dom", None, "<main><div class='loader'>加载</div></main>"),
        PageCaptureArtifact("visible_text", None, "加载"),
        PageCaptureArtifact("screenshot", None, {"encoding": "base64", "data": "aW1hZ2U="}),
        PageCaptureArtifact("ui_snapshot", None, {"elements": []}),
    ]

    ready, diagnostics = await _assess_page_inspection_readiness(page=object(), artifacts=artifacts)

    assert ready is False
    assert diagnostics["outcome"] == "blocked"
    assert "blocking_loader_visible" in diagnostics["blocking_reasons"]


async def test_completed_line_shell_with_close_is_inspectable_startup_ad(
    monkeypatch,
) -> None:
    async def read_readiness(page: Any) -> dict[str, Any]:
        return {
            "core_present": True,
            "necessary_media_ready": False,
            "visible_loader": False,
            "blocking_loader": False,
            "transition_active": False,
            "blocking_transition": False,
            "dom_quiet_ms": 100,
        }

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    async def trusted_dismissal(page: Any) -> bool:
        return True
    monkeypatch.setattr(page_capture_module, "_has_visible_startup_ad_dismissal", trusted_dismissal)
    artifacts = [
        PageCaptureArtifact(
            "dom",
            None,
            '<div class="loading-page"><span class="down-time">关闭</span></div>',
        ),
        PageCaptureArtifact("visible_text", None, "关闭\n线路读取完成，请稍后\n广告内容"),
        PageCaptureArtifact("screenshot", None, {"encoding": "base64", "data": "aW1hZ2U="}),
        PageCaptureArtifact(
            "ui_snapshot",
            None,
            {
                "elements": [
                    {
                        "tag": "span",
                        "text": "关闭",
                        "class_tokens": ["down-time"],
                    }
                ]
            },
        ),
    ]

    ready, diagnostics = await _assess_page_inspection_readiness(page=object(), artifacts=artifacts)

    assert ready is True
    assert diagnostics["blocking_reasons"] == []
    assert "startup_ad_ready_after_line_loading" in diagnostics["dynamic_reasons"]
    assert diagnostics["evidence"]["startup_ad_dismissal_visible"] is True


async def test_page_settle_treats_read_errors_as_unstable(monkeypatch) -> None:
    class FlakyPage:
        def __init__(self) -> None:
            self.calls = 0

        async def inner_text(self, selector: str, timeout: int | None = None) -> str:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("execution context was destroyed")
            return "稳定内容"

    async def read_readiness(page: Any) -> dict[str, Any]:
        return _stable_page_readiness()

    monkeypatch.setattr(page_capture_module, "_read_page_readiness", read_readiness)
    page = FlakyPage()

    await _wait_for_page_settle(page=page, max_wait_ms=1000, poll_interval_ms=0)

    assert page.calls == 4


def test_page_capture_tool_result_keeps_required_artifact_types() -> None:
    payload: dict[str, Any] = PageCaptureArtifact(
        artifact_type="visible_text",
        artifact_id=None,
        content="页面文案",
    ).to_dict()

    assert payload == {
        "artifact_type": "visible_text",
        "artifact_id": None,
        "content": "页面文案",
    }


def test_ui_snapshot_walker_collects_modal_obstruction_before_element_limit() -> None:
    """内容重的页面若按文档序收集，body 尾部的模态弹窗会被 300 上限截掉，
    执行端将以 POPUP_DISMISSAL_NOT_FOUND 拒绝所有清障动作并死循环。"""
    import inspect

    from services.api.app.infrastructure.browser import page_capture

    source = inspect.getsource(page_capture)
    assert "priorityModalDismissalCandidates" in source
    assert "priorityModalCandidates" in source
    assert "[role='dialog']" in source
    assert "[class~='van-dialog']" in source
    dismissal_start = source.index("...priorityModalDismissalCandidates")
    set_literal_start = source.index("...priorityModalCandidates")
    assert dismissal_start < set_literal_start
    assert set_literal_start < source.index("...priorityObstructionCandidates")
    assert set_literal_start < source.index("...priorityNavigationCandidates")


def test_ui_snapshot_walker_collects_ad_href_before_dense_content_limit() -> None:
    """密集业务页必须先采集带 href 的广告锚点，不能只留下无链接广告容器。"""
    import inspect

    from services.api.app.infrastructure.browser import page_capture

    source = inspect.getsource(page_capture)
    assert "priorityWebAdCandidates" in source
    predicate_start = source.index("const withinWebAdvertisement")
    candidate_definition = source.index("const priorityWebAdCandidates")
    assert predicate_start < candidate_definition
    ad_start = source.index("...priorityWebAdCandidates")
    assert ad_start < source.index("...priorityWebSubjectCandidates")
    assert ad_start < source.index("...priorityNavigationCandidates")


async def test_ui_snapshot_collects_ad_href_without_javascript_initialization_error() -> None:
    """广告候选谓词必须在浏览器脚本使用前初始化，不能让整个快照降级为空。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <main>
      <article class="ad-item">
        <a id="ad-link" href="https://ads.example/landing">推广</a>
      </article>
    </main>
    """
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1920, "height": 1080})
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page, web_detail_candidate_priority=True)
            await browser.close()
    except Exception as exc:  # noqa: BLE001 - 环境没有浏览器时跳过集成回归
        pytest.skip(f"Playwright Chromium 不可用：{exc}")

    assert "collection_error" not in snapshot
    candidate = next(item for item in snapshot["elements"] if item["selector"] == "#ad-link")
    assert candidate["href"] == "https://ads.example/landing"
    assert candidate["is_advertisement"] is True


def test_ui_snapshot_size_bounding_retains_external_ad_href() -> None:
    """512 KiB 裁剪后仍须保留广告锚点，供独立广告页调度读取 href。"""
    dense_content = [
        {
            "selector": f"#content-{index}",
            "tag": "a",
            "class_tokens": ["content-card"],
            "text": "正文" * 2_000,
            "href": f"https://example.com/detail/{index}",
            "rect": {"y": index * 20, "height": 20},
        }
        for index in range(300)
    ]
    ad_link = {
        "selector": ".ad-item > a",
        "tag": "a",
        "class_tokens": ["campaign-link"],
        "ancestor_class_tokens": ["ad-item", "post-card-ads"],
        "text": "",
        "href": "https://ads.example/landing",
        "is_advertisement": True,
        "interactive": True,
        "rect": {"y": 4_000, "height": 300},
    }

    snapshot = _sanitize_and_bound_ui_snapshot(
        {"url": "https://example.com/home", "elements": [*dense_content, ad_link]}
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept = {item["selector"]: item for item in snapshot["elements"]}
    assert kept[".ad-item > a"]["href"] == "https://ads.example/landing"


def test_ui_snapshot_size_bounding_evicts_dialog_elements_last() -> None:
    """体积驱逐若先扔掉弹窗证明元素，执行端将失去唯一弹窗证据。"""
    filler = [
        {
            "selector": f"#item-{index}",
            "tag": "a",
            "class_tokens": ["card"],
            "text": "内容" * 2_000,
            "rect": {"y": 100, "height": 20},
        }
        for index in range(300)
    ]
    dialog = {
        "selector": "#promo-dialog",
        "tag": "div",
        "role": "dialog",
        "class_tokens": ["van-dialog"],
        "text": "",
        "rect": {"y": 5_000, "height": 300},
    }
    snapshot = _sanitize_and_bound_ui_snapshot(
        {"url": "https://example.com/home", "elements": [*filler, dialog]}
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert "#promo-dialog" in kept_selectors


def test_ui_snapshot_size_bounding_retains_nested_modal_close_wrapper() -> None:
    """D24 式密集弹窗触发 512 KiB 裁剪后仍须保留尾部关闭包装层。"""
    close_wrapper = {
        "selector": "[role='dialog'] .close_btn",
        "tag": "div",
        "class_tokens": ["close_btn"],
        "ancestor_class_tokens": ["van-popup", "app-list-pop"],
        "text": "",
        "interactive": True,
        "rect": {"y": 780, "height": 48},
        "hit_test": {"center_hits_target": True},
    }
    dense_buttons = [
        {
            "selector": f"[role='dialog'] .btn:nth-of-type({index + 1})",
            "tag": "div",
            "class_tokens": ["btn"],
            "ancestor_class_tokens": ["van-popup", "app-list-pop"],
            "text": f"广告 {index}" + "内容" * 2_000,
            "interactive": True,
            "rect": {"y": index * 20, "height": 20},
            "hit_test": {"center_hits_target": True},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/home",
            "elements": [*dense_buttons, close_wrapper],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert "[role='dialog'] .close_btn" in kept_selectors


def test_ui_snapshot_size_bounding_retains_web_obstruction_close_contract() -> None:
    """Web 普通广告弹层的结构化关闭合同不得被密集页面体积裁剪驱逐。"""
    close_control = {
        "selector": ".ad-dialog .close",
        "tag": "button",
        "class_tokens": ["close", "text-subtitle"],
        "ancestor_class_tokens": ["modal-header", "ad-dialog", "modal"],
        "text": "",
        "interactive": True,
        "web_obstruction_layer": ".ad-dialog",
        "web_obstruction_layer_kind": "dialog",
        "web_obstruction_layer_structural": True,
        "web_obstruction_close_semantics": True,
        "web_obstruction_close_basis": "own_close_token",
        "web_obstruction_close_control_unique": True,
        "web_obstruction_close_hit_safe": True,
        "rect": {"y": 748, "height": 40},
        "hit_test": {"center_hits_target": True},
    }
    dense_navigation = [
        {
            "selector": f"#navigation-{index}",
            "tag": "a",
            "class_tokens": ["app-nav-item"],
            "ancestor_class_tokens": ["app-nav"],
            "text": f"密集菜单 {index}" + "内容" * 2_000,
            "interactive": True,
            "rect": {"y": index * 20, "height": 20},
            "hit_test": {"center_hits_target": False},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/home",
            "elements": [*dense_navigation, close_control],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert close_control["selector"] in kept_selectors


def test_ui_snapshot_size_bounding_retains_shared_startup_dismissal_capability() -> None:
    """密集页面裁剪后，检测已登记的开屏退出结构仍须可被执行端验签。"""

    startup_dismissal = {
        "selector": "body > div.van-popup > div.count-down",
        "tag": "div",
        "class_tokens": ["count-down"],
        "ancestor_class_tokens": ["van-popup"],
        "text": "跳过",
        "interactive": False,
        "rect": {"y": 20, "height": 32},
        "hit_test": {"center_hits_target": True},
    }
    filler = [
        {
            "selector": f"#startup-noise-{index}",
            "tag": "button",
            "class_tokens": ["content-action"],
            "text": "普通内容" * 2_000,
            "interactive": True,
            "rect": {"y": index * 20, "height": 20},
            "hit_test": {"center_hits_target": True},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/launch",
            "elements": [*filler, startup_dismissal],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert startup_dismissal["selector"] in kept_selectors


def test_ui_snapshot_size_bounding_retains_click_bound_div() -> None:
    """体积驱逐必须保留采集器已证明可交互的自定义 div 卡片。"""
    interactive_card = {
        "selector": ".horizontalVideoBox.videoBox",
        "tag": "div",
        "class_tokens": ["horizontalVideoBox", "videoBox"],
        "text": "首个视频卡片",
        "interactive": True,
        "rect": {"y": 360, "height": 220},
        "hit_test": {"center_hits_target": True},
    }
    filler = [
        {
            "selector": f"#section-{index}",
            "tag": "div",
            "class_tokens": ["section"],
            "text": "容器文本" * 2_000,
            "rect": {"y": 100, "height": 20},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/home",
            "elements": [interactive_card, *filler],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert ".horizontalVideoBox.videoBox" in kept_selectors


def test_ui_snapshot_size_bounding_retains_home_video_title() -> None:
    """首页 videoTitle 是详情入口，体积裁剪后仍须保留。"""
    video_title = {
        "selector": ".videoTitle",
        "tag": "div",
        "class_tokens": ["videoTitle"],
        "text": "无遮挡的视频标题",
        "rect": {"y": 360, "height": 36},
        "hit_test": {"center_hits_target": True},
    }
    filler = [
        {
            "selector": f"#section-{index}",
            "tag": "div",
            "class_tokens": ["section"],
            "text": "容器文本" * 2_000,
            "rect": {"y": 100, "height": 20},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/?mainTab=video",
            "elements": [*filler, video_title],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert ".videoTitle" in kept_selectors


def test_ui_snapshot_size_bounding_retains_community_post_title() -> None:
    """社区首帖标题是详情能力的密封证明，512 KiB 裁剪后仍须保留。"""
    post_title = {
        "selector": ".community_home .van-list .item_container:first-child .poi_title",
        "tag": "div",
        "class_tokens": ["poi_title"],
        "ancestor_class_tokens": ["item_container", "van-list", "community_home"],
        "text": "国庆狂欢限时开启",
        "rect": {"y": 420, "height": 32},
        "hit_test": {"center_hits_target": True},
    }
    filler = [
        {
            "selector": f"#section-{index}",
            "tag": "div",
            "class_tokens": ["section"],
            "text": "容器文本" * 2_000,
            "rect": {"y": 100, "height": 20},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/community",
            "elements": [*filler, post_title],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert post_title["selector"] in kept_selectors


def _visual_navigation_runtime_frame(
    *,
    rect: tuple[int, int, int, int],
    extra_rect: tuple[int, int, int, int] | None = None,
) -> tuple[bytes, bytes]:
    image = np.full((240, 240, 4), 238, dtype=np.uint8)
    image[:, :, 3] = 255
    for x in range(12, 240, 37):
        cv2.line(image, (x, 0), (x, 239), (224, 230, 236, 255), 1)
    for y in range(9, 240, 41):
        cv2.line(image, (0, y), (239, y), (230, 222, 228, 255), 1)

    def draw(target_rect: tuple[int, int, int, int]) -> None:
        left, top, right, bottom = target_rect
        cv2.rectangle(
            image,
            (left, top),
            (right - 1, bottom - 1),
            (38, 62, 186, 255),
            -1,
        )
        cv2.rectangle(
            image,
            (left + 3, top + 3),
            (right - 4, bottom - 4),
            (246, 199, 44, 255),
            2,
        )
        cv2.line(
            image,
            (left + 7, top + 8),
            (right - 8, bottom - 9),
            (250, 250, 250, 255),
            3,
        )
        cv2.line(
            image,
            (right - 9, top + 7),
            (left + 8, bottom - 8),
            (22, 24, 28, 255),
            2,
        )

    draw(rect)
    if extra_rect is not None:
        draw(extra_rect)
    encoded, payload = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA))
    assert encoded is True
    return payload.tobytes(), image.tobytes()


def _visual_navigation_runtime_target(
    *,
    source_rect: tuple[int, int, int, int],
) -> VisualNavigationRuntimeTarget:
    source_png, _ = _visual_navigation_runtime_frame(rect=source_rect)
    return _visual_navigation_runtime_target_from_png(
        source_png=source_png,
        source_rect=source_rect,
    )


def _visual_navigation_runtime_target_from_png(
    *,
    source_png: bytes,
    source_rect: tuple[int, int, int, int],
    source_surface: Mapping[str, Any] | None = None,
) -> VisualNavigationRuntimeTarget:
    image = cv2.imdecode(
        np.frombuffer(source_png, dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert image is not None
    image_height, image_width = image.shape[:2]
    if image.shape[2] == 4:
        rgba = cv2.cvtColor(image, cv2.COLOR_BGRA2RGBA).tobytes()
    else:
        rgba = cv2.cvtColor(image, cv2.COLOR_BGR2RGBA).tobytes()
    left, top, right, bottom = source_rect
    normalized_rect = {
        "x": left / image_width,
        "y": top / image_height,
        "width": (right - left) / image_width,
        "height": (bottom - top) / image_height,
    }
    digests = visual_navigation_region_digests(
        pixel_bytes=rgba,
        image_width=image_width,
        image_height=image_height,
        channel_count=4,
        normalized_rect=normalized_rect,
    )
    capability = build_visual_navigation_capability(
        source_task_id="task-visual-runtime",
        source_capture_id="capture-visual-runtime",
        source_artifact_id="artifact-visual-runtime",
        source_artifact_sha256=f"sha256:{hashlib.sha256(source_png).hexdigest()}",
        source_identity_fingerprint=(
            str(source_surface.get("identity_fingerprint") or "")
            if source_surface is not None
            else "sha256:" + "1" * 64
        ),
        source_state_fingerprint=(
            str(source_surface.get("state_fingerprint") or "")
            if source_surface is not None
            else "sha256:" + "2" * 64
        ),
        source_page_type="home",
        source_page_type_confidence=0.92,
        source_identity_bound=True,
        render_profile={
            "kind": "flutter_web",
            "status": "confirmed",
            "confidence": 0.96,
        },
        semantics_coverage_level="semantics_sparse",
        target_kind="content_card",
        label="内容卡片",
        expected_page_type="content_detail",
        visual_target_confidence=0.93,
        selected=False,
        unique=True,
        risk_flags=(),
        normalized_rect=normalized_rect,
        source_image_size={"width": image_width, "height": image_height},
        viewport_metrics={
            "width": image_width,
            "height": image_height,
            "offset_left": 0,
            "offset_top": 0,
            "page_left": 0,
            "page_top": 0,
            "scale": 1,
        },
        model_image_transform={
            "kind": "identity",
            "scale_x": 1.0,
            "scale_y": 1.0,
            "offset_x": 0.0,
            "offset_y": 0.0,
        },
        **digests,
    )
    return VisualNavigationRuntimeTarget(
        capability=verify_visual_navigation_capability(capability),
        source_png=source_png,
        source_image_sha256=capability["source_artifact_sha256"],
    )


def _visual_navigation_click_request(
    target: VisualNavigationRuntimeTarget,
    *,
    full_page: bool = False,
) -> PageCaptureRequest:
    return PageCaptureRequest(
        mode=PageCaptureMode.SINGLE_TRIGGER,
        trigger_action=PageTriggerAction.CLICK,
        selector=None,
        full_page=full_page,
        _visual_navigation_target=target,
    )


class _VisualNavigationRecordingMouse:
    def __init__(
        self,
        *,
        event_log: list[str] | None = None,
        click_error: BaseException | None = None,
    ) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.event_log = event_log
        self.click_error = click_error

    async def click(self, x: float, y: float) -> None:
        if self.event_log is not None:
            self.event_log.append("mouse.click")
        if self.click_error is not None:
            raise self.click_error
        self.clicks.append((x, y))


def _visual_navigation_page_state(
    *,
    dom_mutation_generation: int = 0,
    history_state_digest: str = "v1:" + "0" * 64,
    history_state_complete: bool = True,
    observer_active: bool = True,
    expired: bool = False,
    url: str = "https://example.com/home",
    time_origin_ms: float = 123_456,
    viewport_update: Mapping[str, object] | None = None,
) -> dict[str, object]:
    viewport = {
        "layout_width": 240,
        "layout_height": 240,
        "width": 240,
        "height": 240,
        "offset_left": 0,
        "offset_top": 0,
        "page_left": 0,
        "page_top": 0,
        "scale": 1,
        "device_pixel_ratio": 1,
        "full_page": False,
    }
    viewport.update(viewport_update or {})
    return {
        "url": url,
        "time_origin_ms": time_origin_ms,
        "history_length": 2,
        "dom_mutation_generation": dom_mutation_generation,
        "history_state_digest": history_state_digest,
        "history_state_complete": history_state_complete,
        "observer_active": observer_active,
        "expired": expired,
        "viewport": viewport,
    }


class _VisualNavigationRuntimeCdpSession:
    def __init__(
        self,
        page_states: list[Mapping[str, object]],
        *,
        detach_error: BaseException | None = None,
        loader_ids: list[str] | None = None,
        send_error_method: str | None = None,
        send_error: BaseException | None = None,
        send_error_count: int | None = None,
        dispose_error: BaseException | None = None,
        release_error: BaseException | None = None,
        event_log: list[str] | None = None,
        cancel_during_detach: bool = False,
        detach_gate: asyncio.Event | None = None,
    ) -> None:
        self.page_states = page_states
        self.detach_error = detach_error
        self.loader_ids = loader_ids or ["loader-1"]
        self.send_error_method = send_error_method
        self.send_error = send_error
        self.send_error_count = send_error_count
        self.dispose_error = dispose_error
        self.release_error = release_error
        self.event_log = event_log
        self.cancel_during_detach = cancel_during_detach
        self.detach_gate = detach_gate
        self.send_calls: list[tuple[str, Mapping[str, object] | None]] = []
        self.sample_index = 0
        self.frame_tree_index = 0
        self.detached = False

    async def send(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        self.send_calls.append((method, params))
        event = {
            "Page.getFrameTree": "cdp.frame",
            "Page.createIsolatedWorld": "cdp.create_world",
            "Runtime.evaluate": "cdp.create_tracker",
            "Runtime.releaseObject": "cdp.release",
        }.get(method)
        if method == "Runtime.callFunctionOn":
            event = (
                "cdp.dispose"
                if params is not None and "dispose" in str(params.get("functionDeclaration"))
                else "cdp.sample"
            )
        if method == "Runtime.evaluate" and params is not None and "body_markup" in str(
            params.get("expression")
        ):
            event = "cdp.frame_origin"
        if self.event_log is not None and event is not None:
            self.event_log.append(event)
        if (
            method == self.send_error_method
            and self.send_error is not None
            and (self.send_error_count is None or self.send_error_count > 0)
        ):
            if self.send_error_count is not None:
                self.send_error_count -= 1
            raise self.send_error
        if method == "Page.getFrameTree":
            loader_index = min(self.frame_tree_index, len(self.loader_ids) - 1)
            self.frame_tree_index += 1
            return {
                "frameTree": {
                    "frame": {
                        "id": "main-frame",
                        "loaderId": self.loader_ids[loader_index],
                        "url": "https://example.com/home",
                    }
                }
            }
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 73}
        if method == "Runtime.evaluate":
            return {"result": {"objectId": "tracker-object", "type": "object"}}
        if method == "Runtime.callFunctionOn":
            if params is not None and "dispose" in str(params.get("functionDeclaration")):
                if self.dispose_error is not None:
                    raise self.dispose_error
                return {"result": {"type": "undefined"}}
            index = min(self.sample_index, len(self.page_states) - 1)
            self.sample_index += 1
            return {
                "result": {
                    "type": "object",
                    "value": dict(self.page_states[index]),
                }
            }
        if method == "Runtime.releaseObject":
            if self.release_error is not None:
                raise self.release_error
            return {}
        raise AssertionError(f"未处理的 CDP 方法：{method}")

    async def detach(self) -> None:
        if self.event_log is not None:
            self.event_log.append("cdp.detach")
        if self.cancel_during_detach:
            current_task = asyncio.current_task()
            assert current_task is not None
            current_task.cancel()
            await asyncio.sleep(0)
        if self.detach_gate is not None:
            await self.detach_gate.wait()
        if self.detach_error is not None:
            raise self.detach_error
        self.detached = True


class _VisualNavigationRuntimeContext:
    def __init__(
        self,
        session: _VisualNavigationRuntimeCdpSession,
        *,
        attach_error: BaseException | None = None,
    ) -> None:
        self.session = session
        self.attach_error = attach_error
        self.attach_calls = 0

    async def new_cdp_session(self, page: object) -> _VisualNavigationRuntimeCdpSession:
        self.attach_calls += 1
        if self.attach_error is not None:
            raise self.attach_error
        return self.session


class _VisualNavigationRuntimePage:
    def __init__(
        self,
        current_png: bytes | list[bytes],
        *,
        page_states: list[Mapping[str, object]] | None = None,
    ) -> None:
        self.current_pngs = list(current_png) if isinstance(current_png, list) else [current_png]
        self.page_states = list(page_states or [_visual_navigation_page_state()])
        self.event_log: list[str] = []
        self.cdp_session = _VisualNavigationRuntimeCdpSession(
            self.page_states,
            event_log=self.event_log,
        )
        self.context = _VisualNavigationRuntimeContext(self.cdp_session)
        self.mouse = _VisualNavigationRecordingMouse(event_log=self.event_log)
        self.screenshot_calls: list[dict[str, object]] = []
        self.evaluate_calls = 0

    async def screenshot(self, **kwargs: object) -> bytes:
        self.screenshot_calls.append(dict(kwargs))
        index = min(len(self.screenshot_calls) - 1, len(self.current_pngs) - 1)
        return self.current_pngs[index]

    async def evaluate(self, script: str) -> dict[str, object]:
        assert "visualViewport" in script
        assert "performance.timeOrigin" in script
        assert "history" in script
        self.evaluate_calls += 1
        index = min(self.evaluate_calls - 1, len(self.page_states) - 1)
        return dict(self.page_states[index])


async def test_visual_navigation_browser_revalidates_latest_viewport_and_clicks_once() -> None:
    """省略最新 viewport PNG 复验或重复 click 都会破坏单次动作合同。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    action_trace: list[dict[str, Any]] = []

    await _run_single_trigger(
        page=page,
        context=page.context,
        request=_visual_navigation_click_request(
            _visual_navigation_runtime_target(source_rect=rect)
        ),
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert page.screenshot_calls == [
        {"full_page": False, "type": "png"},
        {"full_page": False, "type": "png"},
    ]
    assert page.evaluate_calls == 1
    assert page.context.attach_calls == 1
    assert page.cdp_session.detached is True
    assert any(method == "Page.createIsolatedWorld" for method, _ in page.cdp_session.send_calls)
    assert any(
        method == "Runtime.callFunctionOn"
        and params is not None
        and "dispose" in str(params.get("functionDeclaration"))
        for method, params in page.cdp_session.send_calls
    )
    assert page.mouse.clicks == [(112.0, 122.0)]
    assert page.event_log == [
        "cdp.frame",
        "cdp.create_world",
        "cdp.create_tracker",
        "cdp.frame",
        "cdp.sample",
        "cdp.frame",
        "cdp.sample",
        "cdp.frame_origin",
        "cdp.frame",
        "cdp.sample",
        "cdp.frame",
        "cdp.sample",
        "cdp.frame",
        "cdp.sample",
        "mouse.click",
        "cdp.dispose",
        "cdp.release",
        "cdp.detach",
    ]
    assert action_trace == [
        {
            "action": "click",
            "selector": None,
            "resolved_by": "validated_visual_navigation_coordinate",
            "visual_target_verified": True,
            "visual_target_relocated": False,
            "visual_match_score": 1.0,
            "visual_audit_codes": [],
            "preclick_frame_origin": {
                "status": "unavailable",
                "reason": "invalid_dom_diagnostic",
                "screenshot_sha256": hashlib.sha256(current_png).hexdigest(),
            },
        }
    ]
    serialized = repr(action_trace)
    assert "target_signature" not in serialized
    assert "source_png" not in serialized
    assert "digest" not in serialized


async def test_verified_second_preclick_frame_uses_clone_shared_private_context() -> None:
    """已验签 live before 帧必须跨请求克隆保留，且不进入公开或序列化材料。"""
    rect = (82, 92, 142, 152)
    source_png, _ = _visual_navigation_runtime_frame(rect=rect)
    current_image = cv2.imdecode(
        np.frombuffer(source_png, dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert current_image is not None
    current_image[20, 20] = (17, 31, 47, 255)
    encoded, payload = cv2.imencode(".png", current_image)
    assert encoded is True
    live_before_png = payload.tobytes()
    page = _VisualNavigationRuntimePage([live_before_png, live_before_png])
    request = _visual_navigation_click_request(
        _visual_navigation_runtime_target_from_png(
            source_png=source_png,
            source_rect=rect,
        )
    )
    clone_1 = page_capture_module._clone_page_capture_request(request)
    clone_2 = page_capture_module._clone_page_capture_request(clone_1)

    await _run_single_trigger(
        page=page,
        context=page.context,
        request=clone_2,
        action_trace=[],
        timeout_ms=30_000,
    )

    context = getattr(request, "_visual_navigation_frame_context", None)
    assert context is not None
    assert context is clone_1._visual_navigation_frame_context
    assert context is clone_2._visual_navigation_frame_context
    verified = context.verified_before_frame
    assert verified is not None
    assert verified.viewport_png == live_before_png
    assert verified.viewport == {
        "width": 240.0,
        "height": 240.0,
        "offset_left": 0.0,
        "offset_top": 0.0,
        "page_left": 0.0,
        "page_top": 0.0,
        "scale": 1.0,
        "device_pixel_ratio": 1.0,
    }
    assert verified.source_image_sha256 == (request._visual_navigation_target.source_image_sha256)
    assert "viewport_png" not in repr(request)
    serialized_context = pickle.dumps(context)
    assert live_before_png not in serialized_context
    assert pickle.loads(serialized_context).verified_before_frame is None


async def test_visual_navigation_binds_snapshot_from_inside_tracker_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """动作前 DOM/Semantics 与第二张 live PNG 必须来自同一 tracker 世代。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    protected_snapshot = {
        "schema_version": "ui-snapshot.v2",
        "url": "https://example.com/home",
        "elements": [{"tag": "flt-semantics", "aria_label": "B 页面"}],
        "element_count": 1,
    }
    snapshot_calls = 0

    async def collect_protected_snapshot(
        current_page: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal snapshot_calls
        assert current_page is page
        assert kwargs["defer_logical_surface"] is True
        assert page.event_log.count("cdp.sample") == 1
        snapshot_calls += 1
        page.event_log.append("protected.snapshot")
        return protected_snapshot

    monkeypatch.setattr(
        page_capture_module,
        "_collect_ui_snapshot",
        collect_protected_snapshot,
    )
    request = _visual_navigation_click_request(_visual_navigation_runtime_target(source_rect=rect))

    await _run_single_trigger(
        page=page,
        context=page.context,
        request=request,
        action_trace=[],
        timeout_ms=30_000,
    )

    verified = request._visual_navigation_frame_context.verified_before_frame
    assert snapshot_calls == 1
    assert verified is not None
    assert verified.ui_snapshot is protected_snapshot
    assert page.event_log.index("cdp.sample") < page.event_log.index("protected.snapshot")
    assert "B 页面" not in repr(request)


async def test_visual_navigation_snapshot_generation_change_is_stale_before_png(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tracker 内采 Snapshot 期间换代必须在截图与 click 前失败关闭。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(
        current_png,
        page_states=[
            _visual_navigation_page_state(dom_mutation_generation=0),
            _visual_navigation_page_state(dom_mutation_generation=1),
        ],
    )
    snapshot_calls = 0

    async def collect_during_generation_change(
        current_page: object,
        **kwargs: object,
    ) -> dict[str, Any]:
        nonlocal snapshot_calls
        del kwargs
        assert current_page is page
        snapshot_calls += 1
        return {
            "schema_version": "ui-snapshot.v2",
            "url": "https://example.com/home",
            "elements": [],
            "element_count": 0,
        }

    monkeypatch.setattr(
        page_capture_module,
        "_collect_ui_snapshot",
        collect_during_generation_change,
    )

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert snapshot_calls == 1
    assert page.screenshot_calls == []
    assert page.mouse.clicks == []


@pytest.mark.parametrize(
    ("failure_kind", "failure"),
    [
        ("unsupported", None),
        ("attach", RuntimeError("cdp-attach-private-detail")),
        ("create_world", RuntimeError("cdp-world-private-detail")),
        ("create_tracker", RuntimeError("cdp-tracker-private-detail")),
        ("sample", RuntimeError("cdp-sample-private-detail")),
    ],
)
async def test_visual_navigation_cdp_lifecycle_failure_is_stale_and_never_clicks(
    failure_kind: str,
    failure: BaseException | None,
) -> None:
    """CDP 不可用、创建、采样或释放失败都必须脱敏关闭。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    if failure_kind == "unsupported":
        page.context = SimpleNamespace()
    elif failure_kind == "attach":
        page.context = _VisualNavigationRuntimeContext(
            page.cdp_session,
            attach_error=failure,
        )
    elif failure_kind == "create_world":
        page.cdp_session.send_error_method = "Page.createIsolatedWorld"
        page.cdp_session.send_error = failure
    elif failure_kind == "create_tracker":
        page.cdp_session.send_error_method = "Runtime.evaluate"
        page.cdp_session.send_error = failure
    elif failure_kind == "sample":
        page.cdp_session.send_error_method = "Runtime.callFunctionOn"
        page.cdp_session.send_error = failure
    else:
        raise AssertionError(f"未处理的失败阶段：{failure_kind}")

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert "private-detail" not in str(error.value)
    assert page.mouse.clicks == []


@pytest.mark.parametrize("failure_kind", ["attach", "create_tracker", "sample"])
async def test_visual_navigation_cdp_cancellation_is_not_swallowed(
    failure_kind: str,
) -> None:
    """CDP 生命周期中的任务取消必须保留 asyncio 控制流语义。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    cancellation = asyncio.CancelledError()
    if failure_kind == "attach":
        page.context = _VisualNavigationRuntimeContext(
            page.cdp_session,
            attach_error=cancellation,
        )
    elif failure_kind == "create_tracker":
        page.cdp_session.send_error_method = "Runtime.evaluate"
        page.cdp_session.send_error = cancellation
    elif failure_kind == "sample":
        page.cdp_session.send_error_method = "Runtime.callFunctionOn"
        page.cdp_session.send_error = cancellation
    else:
        raise AssertionError(f"未处理的取消阶段：{failure_kind}")

    with pytest.raises(asyncio.CancelledError):
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert page.mouse.clicks == []


async def test_visual_navigation_cancellation_wins_over_cleanup_failure() -> None:
    """动作取消不得被随后发生的普通 detach 错误改写为业务失败。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    page.cdp_session.send_error_method = "Runtime.callFunctionOn"
    page.cdp_session.send_error = asyncio.CancelledError()
    page.cdp_session.send_error_count = 1
    page.cdp_session.detach_error = RuntimeError("cleanup-private-detail")

    with pytest.raises(asyncio.CancelledError):
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert page.mouse.clicks == []


async def test_visual_navigation_preclick_cleanup_cancellation_keeps_control_flow() -> None:
    """点击前普通失败遇到清理取消时，仍必须保留项目的取消优先策略。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    page.cdp_session.send_error_method = "Runtime.callFunctionOn"
    page.cdp_session.send_error = RuntimeError("preclick-private-detail")
    page.cdp_session.send_error_count = 1
    page.cdp_session.dispose_error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert page.mouse.clicks == []
    assert page.event_log[-3:] == [
        "cdp.dispose",
        "cdp.release",
        "cdp.detach",
    ]


@pytest.mark.parametrize(
    ("failure_kind", "failure"),
    [
        ("dispose", RuntimeError("dispose-private-object-id")),
        ("release", RuntimeError("release-private-object-id")),
        ("detach", RuntimeError("detach-private-session")),
    ],
    ids=(
        "dispose-error",
        "release-error",
        "detach-error",
    ),
)
async def test_visual_navigation_postclick_cleanup_failure_keeps_completed_action(
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
    failure: BaseException,
) -> None:
    """click 已成功返回后，清理故障不得改写为可重试结果或再次点击。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    if failure_kind == "dispose":
        page.cdp_session.dispose_error = failure
    elif failure_kind == "release":
        page.cdp_session.release_error = failure
    elif failure_kind == "detach":
        page.cdp_session.detach_error = failure
    else:
        raise AssertionError(f"未处理的清理阶段：{failure_kind}")

    with caplog.at_level("WARNING", logger=page_capture_module.__name__):
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert page.mouse.clicks == [(112.0, 122.0)]
    assert page.event_log[-4:] == [
        "mouse.click",
        "cdp.dispose",
        "cdp.release",
        "cdp.detach",
    ]
    assert "视觉点击已完成，隔离 tracker 清理未完整结束" in caplog.text
    assert "private" not in caplog.text
    assert "object-id" not in caplog.text
    cleanup_record = next(
        record
        for record in caplog.records
        if record.message == "视觉点击已完成，隔离 tracker 清理未完整结束"
    )
    assert cleanup_record.click_committed is True
    assert cleanup_record.resolved_by == "validated_visual_navigation_coordinate"
    assert cleanup_record.visual_match_score == 1.0
    assert cleanup_record.visual_audit_codes == ()


async def test_visual_navigation_click_failure_keeps_failure_and_cleans_tracker() -> None:
    """mouse.click 自身失败仍保留原语义，且不会尝试第二次点击。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    click_error = RuntimeError("click-dispatch-failed")
    page.mouse.click_error = click_error

    with pytest.raises(RuntimeError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value is click_error
    assert page.mouse.clicks == []
    assert page.event_log[-4:] == [
        "mouse.click",
        "cdp.dispose",
        "cdp.release",
        "cdp.detach",
    ]


async def test_visual_navigation_real_task_cancellation_during_postclick_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """点击成功后的真实 task.cancel 只能结束清理，不能制造重试或第二次点击。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    page.cdp_session.cancel_during_detach = True

    with (
        caplog.at_level("WARNING", logger=page_capture_module.__name__),
        pytest.raises(asyncio.CancelledError),
    ):
        action_task = asyncio.create_task(
            _run_single_trigger(
                page=page,
                context=page.context,
                request=_visual_navigation_click_request(
                    _visual_navigation_runtime_target(source_rect=rect)
                ),
                action_trace=[],
                timeout_ms=30_000,
            )
        )
        await action_task

    assert action_task.cancelled() is True
    assert action_task.cancelling() == 1
    assert page.mouse.clicks == [(112.0, 122.0)]
    assert page.event_log[-4:] == [
        "mouse.click",
        "cdp.dispose",
        "cdp.release",
        "cdp.detach",
    ]
    assert "视觉点击已完成，隔离 tracker 清理被控制流中断" in caplog.text


async def test_visual_navigation_inner_timeout_preserves_committed_click_fact() -> None:
    """wait_for 把清理取消转成超时时，父任务仍须识别 click 已提交。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    page.cdp_session.detach_gate = asyncio.Event()
    request = _visual_navigation_click_request(_visual_navigation_runtime_target(source_rect=rect))

    class Browser:
        async def capture(self, current_request: PageCaptureRequest) -> PageCaptureResult:
            await _run_single_trigger(
                page=page,
                context=page.context,
                request=current_request,
                action_trace=[],
                timeout_ms=30_000,
            )
            raise AssertionError("清理阻塞时不应完成 Capture")

    with pytest.raises(Exception) as error:
        await page_capture_module.capture_page_with_timeout(
            request=request,
            browser=Browser(),
            timeout_seconds=0.01,
        )

    assert type(error.value).__name__ == ("VisualActionCommittedCleanupInterruptedError")
    assert str(error.value) == "视觉点击已提交，清理未完成，禁止自动重试"
    assert page.mouse.clicks == [(112.0, 122.0)]
    assert page_capture_module._visual_action_was_committed(request) is True
    assert "click_committed" not in repr(request)
    assert "validated_visual_navigation_coordinate" not in repr(request)
    serialized_holder = pickle.dumps(request._visual_action_commit_holder)
    assert b"click_committed" not in serialized_holder
    assert b"validated_visual_navigation_coordinate" not in serialized_holder
    restored_holder = pickle.loads(serialized_holder)
    assert restored_holder.commit_record is None

    independent_request = _visual_navigation_click_request(
        _visual_navigation_runtime_target(source_rect=rect)
    )
    assert page_capture_module._visual_action_was_committed(independent_request) is False
    assert request == independent_request


async def test_dedicated_visual_png_is_attached_before_single_final_logical_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """专用 PNG 必须先进入内存 Snapshot，再只附加一次最终逻辑身份。"""
    image = np.full((320, 240, 3), 245, dtype=np.uint8)
    cv2.rectangle(image, (12, 12), (227, 45), (40, 40, 40), 2)
    cv2.rectangle(image, (18, 70), (221, 240), (40, 40, 40), 3)
    encoded, payload = cv2.imencode(".png", image)
    assert encoded is True
    viewport_png = payload.tobytes()

    class SparseFlutterPage:
        url = "https://example.test/flutter"

        def __init__(self) -> None:
            self.screenshot_options: list[dict[str, Any]] = []

        async def evaluate(self, script: str) -> dict[str, Any]:
            del script
            return {
                "schema_version": "ui-snapshot.v2",
                "url": self.url,
                "title": "稀疏 Flutter",
                "viewport": {
                    "width": 240,
                    "height": 320,
                    "device_pixel_ratio": 1,
                    "scroll_x": 0,
                    "scroll_y": 0,
                    "visual_viewport_width": 240,
                    "visual_viewport_height": 320,
                    "visual_viewport_offset_left": 0,
                    "visual_viewport_offset_top": 0,
                    "visual_viewport_page_left": 0,
                    "visual_viewport_page_top": 0,
                    "visual_viewport_scale": 1,
                    "flutter_host_transform": [1, 0, 0, 1, 0, 0],
                },
                "document": {
                    "time_origin_ms": 1000,
                    "flutter_root_present": True,
                    "history_length": 1,
                },
                "elements": [
                    {
                        "selector": "flt-semantics#home",
                        "tag": "flt-semantics",
                        "role": "button",
                        "text": "首页",
                        "interactive": True,
                        "visible": True,
                        "hit_test": {"center_hits_target": True},
                        "rect": {"x": 0, "y": 280, "width": 60, "height": 40},
                    }
                ],
            }

        async def screenshot(self, **options: Any) -> bytes:
            self.screenshot_options.append(dict(options))
            if options.get("full_page") is False:
                return viewport_png
            return b"existing-screenshot"

    visual_evidence = {
        "protocol_version": "visual_layout_evidence.v1",
        "identity": {
            "top_structure": {"category": "app_bar", "segment_count": 1},
            "bottom_navigation": {"slot_count": 4, "active_slot": 0},
            "main_layout_category": "grid",
        },
        "state_audit": {
            "main_density_bucket": "medium",
            "animation_bucket": "unknown",
            "full_frame_change_bucket": "not_compared",
        },
    }
    monkeypatch.setattr(
        page_capture_module,
        "get_settings",
        lambda: SimpleNamespace(
            workflow=SimpleNamespace(h5_visual_coordinate_runtime_mode="shadow")
        ),
    )
    monkeypatch.setattr(
        page_capture_module,
        "extract_visual_layout_evidence",
        lambda *, viewport_png: visual_evidence,
        raising=False,
    )
    original_attach = page_capture_module._attach_logical_surface
    attach_inputs: list[tuple[bool, bool]] = []

    def track_final_attach(snapshot: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        attach_inputs.append(
            (
                "visual_navigation_capture" in snapshot,
                "visual_layout_evidence" in snapshot,
            )
        )
        return original_attach(snapshot, **kwargs)

    monkeypatch.setattr(
        page_capture_module,
        "_attach_logical_surface",
        track_final_attach,
    )

    page = SparseFlutterPage()
    artifacts = await page_capture_module._collect_artifacts(
        page,
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            capture_dom=False,
            capture_visible_text=False,
        ),
        logical_surface_enabled=True,
        h5_visual_coordinate_mode="shadow",
    )

    snapshot = next(item.content for item in artifacts if item.artifact_type == "ui_snapshot")
    assert attach_inputs == [(True, True)]
    assert snapshot["visual_layout_evidence"] == visual_evidence
    assert (
        snapshot["logical_surface"]["identity_signals"]["visual_layout"]
        == (visual_evidence["identity"])
    )
    assert [item.artifact_type for item in artifacts].count("ui_snapshot") == 1

    attach_inputs.clear()
    off_page = SparseFlutterPage()
    off_artifacts = await page_capture_module._collect_artifacts(
        off_page,
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            capture_dom=False,
            capture_visible_text=False,
        ),
        logical_surface_enabled=True,
        h5_visual_coordinate_mode="off",
    )
    off_snapshot = next(
        item.content for item in off_artifacts if item.artifact_type == "ui_snapshot"
    )
    assert "visual_navigation_capture" not in off_snapshot
    assert "visual_layout_evidence" not in off_snapshot
    assert off_page.screenshot_options == [{"full_page": True}]
    assert not any(options.get("full_page") is False for options in off_page.screenshot_options)


@pytest.mark.parametrize("current_mode", ["off", "shadow"])
@pytest.mark.asyncio
async def test_visual_navigation_browser_kill_switch_rejects_before_session_or_click(
    current_mode: str,
) -> None:
    """Browser 最后一层门禁必须在创建页面会话前拒绝旧视觉请求。"""
    browser = PlaywrightPageCaptureBrowser(
        h5_visual_coordinate_mode=current_mode,
    )

    with pytest.raises(ValueError, match="视觉导航执行模式"):
        await browser.capture(
            _visual_navigation_click_request(
                _visual_navigation_runtime_target(
                    source_rect=(82, 92, 142, 152),
                )
            )
        )

    assert browser._sessions == {}


async def test_visual_navigation_after_frames_use_short_stability_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点击 settle 后必须采两张 after，且只在两帧之间等待既有短间隔。"""
    calls: list[tuple[str, Any]] = []

    class Page:
        async def screenshot(self, **options: Any) -> bytes:
            calls.append(("screenshot", dict(options)))
            return b"after-1" if len(calls) == 1 else b"after-2"

    async def record_sleep(seconds: float) -> None:
        calls.append(("sleep", seconds))

    monkeypatch.setattr(page_capture_module.asyncio, "sleep", record_sleep)

    frames = await page_capture_module._capture_visual_navigation_after_frames(Page())

    assert frames == (b"after-1", b"after-2")
    assert calls == [
        ("screenshot", {"full_page": False, "type": "png"}),
        (
            "sleep",
            page_capture_module.PAGE_POST_TRIGGER_SETTLE_POLL_INTERVAL_MS / 1000,
        ),
        ("screenshot", {"full_page": False, "type": "png"}),
    ]


async def test_real_browser_visual_click_binds_stable_after_frames_to_action_trace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """真实 Browser 点击必须把稳定两帧主证明绑定到最终 after identity。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { margin: 0; width: 240px; height: 320px; overflow: hidden; }
      canvas { display: block; width: 240px; height: 320px; }
      flt-glass-pane { position: fixed; width: 1px; height: 1px; pointer-events: none; }
      flt-semantics { position: fixed; left: 5px; top: 280px; width: 50px; height: 40px;
        opacity: 0.01; pointer-events: auto; }
    </style>
    <canvas id="surface" width="240" height="320"></canvas>
    <flt-glass-pane></flt-glass-pane>
    <flt-semantics-host>
      <flt-semantics flt-tappable role="button" aria-label="内容卡片"></flt-semantics>
      <flt-semantics flt-tappable role="button" aria-label="隐藏节点"
        style="display:none"></flt-semantics>
      <flt-semantics flt-tappable role="button" aria-label="零面积节点"
        style="width:0;height:0"></flt-semantics>
    </flt-semantics-host>
    <script>
      const canvas = document.querySelector('#surface');
      const ctx = canvas.getContext('2d');
      const stroke = '#2d2d2d';
      function chrome(active) {
        ctx.strokeStyle = stroke; ctx.lineWidth = 2;
        ctx.strokeRect(12, 12, 216, 34);
        ctx.beginPath(); ctx.moveTo(0, 279); ctx.lineTo(239, 279); ctx.stroke();
        for (let index = 0; index < 4; index += 1) {
          const center = 30 + index * 60;
          ctx.beginPath(); ctx.arc(center, 294, 7, 0, Math.PI * 2); ctx.stroke();
          ctx.beginPath(); ctx.moveTo(center - 10, 306); ctx.lineTo(center + 10, 306);
          ctx.stroke();
          if (index === active) ctx.fillRect(center - 18, 313, 36, 4);
        }
      }
      function grid() {
        canvas.dataset.view = 'grid';
        ctx.fillStyle = '#f5f5f5'; ctx.fillRect(0, 0, 240, 320); chrome(0);
        ctx.strokeStyle = stroke; ctx.lineWidth = 2;
        for (const [left, top] of [[18, 72], [124, 72], [18, 128], [124, 128]]) {
          ctx.strokeRect(left, top, 97, 46);
        }
        ctx.beginPath(); ctx.moveTo(28, 82); ctx.lineTo(64, 108);
        ctx.moveTo(64, 82); ctx.lineTo(28, 108); ctx.stroke();
      }
      function detail() {
        canvas.dataset.view = 'detail';
        ctx.fillStyle = '#f5f5f5'; ctx.fillRect(0, 0, 240, 320); chrome(0);
        ctx.strokeStyle = stroke; ctx.lineWidth = 5;
        ctx.strokeRect(20, 65, 200, 85);
        ctx.fillStyle = stroke;
        ctx.fillRect(22, 178, 194, 5);
        ctx.fillRect(22, 198, 176, 5);
        ctx.fillRect(22, 218, 202, 5);
        ctx.fillRect(22, 238, 148, 5);
      }
      document.addEventListener('click', (event) => {
        if (event.clientX >= 18 && event.clientX <= 115
          && event.clientY >= 72 && event.clientY <= 118) detail();
      });
      grid();
    </script>
    """

    resolve = page_capture_module.resolve_visual_navigation_coordinate
    resolutions = []

    def checked_resolution(**kwargs):
        resolution = resolve(**kwargs)
        index = len(resolutions)
        (tmp_path / f"source-{index}.png").write_bytes(kwargs["source_png"])
        (tmp_path / f"current-{index}.png").write_bytes(kwargs["current_png"])
        resolutions.append({
            "audit_codes": resolution.audit_codes,
            "source_sha256": hashlib.sha256(kwargs["source_png"]).hexdigest(),
            "current_sha256": hashlib.sha256(kwargs["current_png"]).hexdigest(),
            "match_score": resolution.match_score,
            "viewport": kwargs["current_viewport"],
        })
        (tmp_path / "resolutions.json").write_text(json.dumps(resolutions), encoding="utf-8")
        # 保留真实帧和拒绝码，不替换解析结果或降低复验阈值。
        assert not resolution.audit_codes, {"frames": str(tmp_path), **resolutions[-1]}
        return resolution

    monkeypatch.setattr(
        page_capture_module, "resolve_visual_navigation_coordinate", checked_resolution
    )
    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            await page.evaluate(
                "() => new Promise((resolve) => requestAnimationFrame("
                "() => requestAnimationFrame(resolve)))"
            )
            (tmp_path / "source-dom.html").write_text(await page.content(), encoding="utf-8")
            canvas_state = await page.evaluate("""() => {
                const canvas = document.querySelector('#surface');
                return {url: location.href, view: canvas?.dataset.view,
                    width: canvas?.width, height: canvas?.height,
                    context_lost: canvas ? canvas.getContext('2d').isContextLost() : null,
                    sample: canvas ? Array.from(canvas.getContext('2d').getImageData(0,0,1,1).data)
                        : null};
            }""")
            (tmp_path / "canvas-state.json").write_text(
                json.dumps(canvas_state), encoding="utf-8",
            )
            assert canvas_state["view"] == "grid", canvas_state
            source_png = await page.screenshot(full_page=False, type="png")
            (tmp_path / "initial-source.png").write_bytes(source_png)
            source_snapshot = await page_capture_module._collect_visual_navigation_snapshot(
                page,
                viewport_png=source_png,
                logical_surface_enabled=True,
                logical_surface_shadow=False,
                surface_session_id=None,
            )
            target = _visual_navigation_runtime_target_from_png(
                source_png=source_png,
                source_rect=(18, 72, 115, 118),
                source_surface=source_snapshot["logical_surface"],
            )
            request = _visual_navigation_click_request(target)

            result = await PlaywrightPageCaptureBrowser(
                logical_surface_enabled=True,
            )._capture_on_page(
                request=request,
                context=context,
                page=page,
                console_logs=[],
                network_requests=[],
                timeout_error_type=TimeoutError,
            )
            final_view = await page.get_attribute("#surface", "data-view")
        finally:
            await context.close()
            await chromium.close()

    assert final_view == "detail"
    assert request._visual_navigation_frame_context.verified_before_frame is None
    transition = result.action_trace[-1]["state_diff"]["logical_surface_transition"]
    snapshot = next(
        artifact.content for artifact in result.artifacts if artifact.artifact_type == "ui_snapshot"
    )
    surface = snapshot["logical_surface"]
    semantic_elements = [
        item for item in snapshot["elements"] if item.get("tag") == "flt-semantics"
    ]
    assert len(semantic_elements) == 1, semantic_elements
    assert semantic_elements[0]["aria_label"] == "内容卡片"
    assert semantic_elements[0].get("visible") is True, semantic_elements[0]
    assert semantic_elements[0].get("interactive") is True, semantic_elements[0]
    assert surface["render_profile"]["status"] == "confirmed", surface
    assert surface["semantics_coverage"]["level"] == "semantics_sparse", surface[
        "semantics_coverage"
    ]
    assert surface["semantics_coverage"]["usable_node_count"] == 1
    assert "visual_layout" in surface["identity_signals"], surface
    assert transition["visual_proof"]["reason_codes"] == [], json.dumps(
        snapshot["visual_layout_evidence"],
        ensure_ascii=False,
        sort_keys=True,
    )
    assert transition["visual_proof"]["status"] == "trusted", transition["visual_proof"]
    assert transition["logical_transition_proof"] == "trusted"
    assert transition["visual_proof"]["after_frame_count"] == 2
    assert transition["visual_proof"]["after_frames_stable"] is True
    assert transition["visual_proof"]["after_identity_bound"] is True
    screenshots = [
        artifact for artifact in result.artifacts if artifact.artifact_type == "screenshot"
    ]
    assert len(screenshots) == 2


async def test_real_browser_tracker_switch_uses_current_dom_and_live_png_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """tracker 建立前由 A 切到 B 时不得把旧 A Snapshot 与实时 B PNG 拼接。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { margin: 0; width: 240px; height: 320px; overflow: hidden; }
      canvas { display: block; width: 240px; height: 320px; }
      flt-semantics { position: fixed; left: 18px; top: 72px; width: 97px;
        height: 46px; opacity: .01; }
    </style>
    <canvas id="surface" width="240" height="320"></canvas>
    <flt-glass-pane></flt-glass-pane>
    <flt-semantics-host>
      <flt-semantics id="semantic-target" flt-tappable role="button"
        aria-label="A 页面卡片"></flt-semantics>
    </flt-semantics-host>
    <script>
      window.__aliothClickCount = 0;
      const canvas = document.querySelector('#surface');
      const ctx = canvas.getContext('2d');
      ctx.fillStyle = '#f5f5f5'; ctx.fillRect(0, 0, 240, 320);
      ctx.strokeStyle = '#2d2d2d'; ctx.lineWidth = 2;
      ctx.strokeRect(12, 12, 216, 34);
      for (const [left, top] of [[18, 72], [124, 72], [18, 128], [124, 128]]) {
        ctx.strokeRect(left, top, 97, 46);
      }
      ctx.beginPath(); ctx.moveTo(28, 82); ctx.lineTo(64, 108);
      ctx.moveTo(64, 82); ctx.lineTo(28, 108); ctx.stroke();
      document.addEventListener('click', () => { window.__aliothClickCount += 1; });
    </script>
    """
    original_start_tracker = page_capture_module._start_visual_navigation_cdp_tracker

    async def switch_to_b_before_tracker(**kwargs: object) -> object:
        tracker_page = kwargs["page"]
        await tracker_page.locator("#semantic-target").evaluate(
            "element => {"
            " element.setAttribute('aria-label', 'B 页面标题');"
            " element.setAttribute('role', 'heading');"
            "}"
        )
        return await original_start_tracker(**kwargs)

    monkeypatch.setattr(
        page_capture_module,
        "_start_visual_navigation_cdp_tracker",
        switch_to_b_before_tracker,
    )

    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            source_png = await page.screenshot(full_page=False, type="png")
            source_snapshot = await page_capture_module._collect_visual_navigation_snapshot(
                page,
                viewport_png=source_png,
                logical_surface_enabled=True,
                logical_surface_shadow=False,
                surface_session_id=None,
            )
            target = _visual_navigation_runtime_target_from_png(
                source_png=source_png,
                source_rect=(18, 72, 115, 118),
                source_surface=source_snapshot["logical_surface"],
            )
            result = await PlaywrightPageCaptureBrowser(
                logical_surface_enabled=True,
            )._capture_on_page(
                request=_visual_navigation_click_request(target),
                context=context,
                page=page,
                console_logs=[],
                network_requests=[],
                timeout_error_type=TimeoutError,
            )
            click_count = await page.evaluate("() => window.__aliothClickCount")
        finally:
            await context.close()
            await chromium.close()

    proof = result.action_trace[-1]["state_diff"]["logical_surface_transition"]["visual_proof"]
    assert click_count == 1
    assert proof["status"] == "stale"
    assert "visual_source_identity_mismatch" in proof["reason_codes"]
    assert "visual_source_state_mismatch" in proof["reason_codes"]


async def test_real_browser_semantics_usability_requires_ancestor_visibility_and_hit() -> None:
    """祖先隐藏或无关顶层遮挡必须排除，同一 Semantics 父子命中仍可用。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { margin: 0; width: 240px; height: 320px; }
      flt-semantics { position: absolute; display: block; }
      #valid-parent { left: 12px; top: 12px; width: 100px; height: 44px; }
      #valid-child { inset: 0; pointer-events: none; }
      #opacity-parent { opacity: 0; }
      #opacity-child { left: 12px; top: 72px; width: 100px; height: 36px; }
      #aria-parent { position: absolute; left: 12px; top: 116px; }
      #aria-child { left: 0; top: 0; width: 100px; height: 36px; }
      #covered { left: 12px; top: 164px; width: 100px; height: 36px; }
      #unrelated-overlay { position: absolute; left: 0; top: 154px; width: 130px;
        height: 60px; z-index: 100; background: rgba(0, 0, 0, .2); }
      #display-none { display: none; left: 12px; top: 220px; width: 100px; height: 36px; }
      #zero-area { left: 12px; top: 264px; width: 0; height: 0; }
    </style>
    <flt-glass-pane></flt-glass-pane>
    <flt-semantics-host>
      <flt-semantics id="valid-parent" role="group" aria-label="合法父节点">
        <flt-semantics id="valid-child" flt-tappable role="button"
          aria-label="合法子节点"></flt-semantics>
      </flt-semantics>
      <div id="opacity-parent">
        <flt-semantics id="opacity-child" flt-tappable role="button"
          aria-label="祖先透明"></flt-semantics>
      </div>
      <div id="aria-parent" aria-hidden="true">
        <flt-semantics id="aria-child" flt-tappable role="button"
          aria-label="祖先语义隐藏"></flt-semantics>
      </div>
      <flt-semantics id="covered" flt-tappable role="button"
        aria-label="被无关遮挡"></flt-semantics>
      <flt-semantics id="display-none" flt-tappable role="button"
        aria-label="display none"></flt-semantics>
      <flt-semantics id="zero-area" flt-tappable role="button"
        aria-label="零面积"></flt-semantics>
    </flt-semantics-host>
    <div id="unrelated-overlay"></div>
    """

    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
        finally:
            await context.close()
            await chromium.close()

    semantics = {
        str(item.get("aria_label") or ""): item
        for item in snapshot["elements"]
        if item.get("tag") == "flt-semantics"
    }
    assert semantics["合法子节点"]["visible"] is True
    assert semantics["合法子节点"]["hit_test"]["center_hits_target"] is True
    assert "祖先透明" not in semantics
    assert "祖先语义隐藏" not in semantics
    assert "display none" not in semantics
    assert "零面积" not in semantics
    assert "被无关遮挡" not in semantics or semantics["被无关遮挡"]["visible"] is False
    coverage = classify_flutter_semantics_coverage(
        elements=[item for item in semantics.values()],
        ocr_text=None,
    )
    assert coverage["usable_node_count"] == 2


async def test_real_browser_pointer_transparent_semantics_uses_visual_occlusion_gate() -> None:
    """pointer-none Flutter Semantics 可命中画布，但无关透明指针遮罩仍须排除。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { margin: 0; width: 240px; height: 320px; }
      canvas { position: fixed; inset: 0; width: 240px; height: 320px; }
      flt-semantics-host { position: fixed; inset: 0; pointer-events: none; }
      flt-semantics { position: absolute; display: block; width: 100px; height: 40px;
        opacity: .01; pointer-events: none; }
      #canvas-hit { left: 20px; top: 30px; }
      #visually-covered { left: 20px; top: 110px; }
      .visual-overlay { position: fixed; left: 10px; top: 100px; width: 120px;
        height: 60px; z-index: 100; pointer-events: none;
        background: rgba(20, 20, 20, .55); }
    </style>
    <flutter-view>
      <flt-glass-pane>
        <canvas id="flutter-canvas" width="240" height="320"></canvas>
        <flt-semantics-host>
          <flt-semantics id="canvas-hit" flt-tappable role="button"
            aria-label="画布合法命中"></flt-semantics>
          <flt-semantics id="visually-covered" flt-tappable role="button"
            aria-label="透明指针遮挡"></flt-semantics>
        </flt-semantics-host>
      </flt-glass-pane>
    </flutter-view>
    <div class="visual-overlay"></div>
    """

    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
        finally:
            await context.close()
            await chromium.close()

    semantics = {
        str(item.get("aria_label") or ""): item
        for item in snapshot["elements"]
        if item.get("tag") == "flt-semantics"
    }
    assert semantics["画布合法命中"]["visible"] is True
    assert semantics["画布合法命中"]["hit_test"]["center_hits_target"] is True
    assert semantics["画布合法命中"]["hit_test"]["visual_occlusion_detected"] is False
    assert "透明指针遮挡" not in semantics or semantics["透明指针遮挡"]["visible"] is False
    if "透明指针遮挡" in semantics:
        assert semantics["透明指针遮挡"]["hit_test"]["visual_occlusion_detected"] is True
    coverage = classify_flutter_semantics_coverage(
        elements=[item for item in semantics.values()],
        ocr_text=None,
    )
    assert coverage["usable_node_count"] == 1


async def test_real_browser_offscreen_semantics_cannot_borrow_edge_canvas() -> None:
    """完全离屏的 Semantics 不能把中心点夹到视口边缘借用同根画布。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { margin: 0; width: 240px; height: 320px; }
      flutter-view, flt-glass-pane, canvas, flt-semantics-host {
        position: fixed; inset: 0; width: 240px; height: 320px;
      }
      flt-semantics-host { pointer-events: none; }
      flt-semantics { position: absolute; display: block; width: 100px;
        height: 40px; opacity: .01; pointer-events: none; }
      #offscreen { left: -220px; top: 40px; }
    </style>
    <flutter-view id="root">
      <flt-glass-pane>
        <canvas id="surface" width="240" height="320"></canvas>
        <flt-semantics-host>
          <flt-semantics id="offscreen" flt-tappable role="button"
            aria-label="完全离屏"></flt-semantics>
        </flt-semantics-host>
      </flt-glass-pane>
    </flutter-view>
    """

    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
        finally:
            await context.close()
            await chromium.close()

    semantics = {
        str(item.get("aria_label") or ""): item
        for item in snapshot["elements"]
        if item.get("tag") == "flt-semantics"
    }
    assert (
        "完全离屏" not in semantics
        or semantics["完全离屏"]["hit_test"]["center_hits_target"] is False
    )
    coverage = classify_flutter_semantics_coverage(
        elements=list(semantics.values()),
        ocr_text=None,
    )
    assert coverage["usable_node_count"] == 0


async def test_real_browser_semantics_cannot_borrow_other_flutter_root_canvas() -> None:
    """pointer-none Semantics 只能借用自身 Flutter root 的渲染表面。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    html = """
    <style>
      html, body { margin: 0; width: 240px; height: 320px; }
      flutter-view, flt-glass-pane, flt-semantics-host {
        position: fixed; inset: 0; width: 240px; height: 320px;
      }
      flt-semantics-host { pointer-events: none; }
      flt-semantics { position: absolute; display: block; width: 100px;
        height: 40px; opacity: .01; pointer-events: none; }
      #cross-root { left: 20px; top: 40px; }
      #same-root { left: 20px; top: 120px; }
      #root-b canvas { position: fixed; inset: 0; width: 240px; height: 320px; }
    </style>
    <flutter-view id="root-a">
      <flt-glass-pane>
        <flt-semantics-host>
          <flt-semantics id="cross-root" flt-tappable role="button"
            aria-label="跨根借画布"></flt-semantics>
        </flt-semantics-host>
      </flt-glass-pane>
    </flutter-view>
    <flutter-view id="root-b">
      <flt-glass-pane>
        <canvas id="root-b-surface" width="240" height="320"></canvas>
        <flt-semantics-host>
          <flt-semantics id="same-root" flt-tappable role="button"
            aria-label="同根借画布"></flt-semantics>
        </flt-semantics-host>
      </flt-glass-pane>
    </flutter-view>
    """

    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
        finally:
            await context.close()
            await chromium.close()

    semantics = {
        str(item.get("aria_label") or ""): item
        for item in snapshot["elements"]
        if item.get("tag") == "flt-semantics"
    }
    assert semantics["跨根借画布"]["hit_test"]["center_hits_target"] is False
    assert semantics["同根借画布"]["hit_test"]["center_hits_target"] is True
    coverage = classify_flutter_semantics_coverage(
        elements=list(semantics.values()),
        ocr_text=None,
    )
    assert coverage["usable_node_count"] == 1


@pytest.mark.parametrize("root_kind", ["modern", "legacy"])
async def test_real_browser_semantics_rejects_same_root_painted_div_mask(
    root_kind: str,
) -> None:
    """同根普通 painted div 不是 Flutter renderer surface，必须判定视觉遮挡。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    inner = """
      <canvas id="surface" width="240" height="320"></canvas>
      <flt-semantics-host>
        <flt-semantics id="target" flt-tappable role="button"
          aria-label="同根普通遮罩"></flt-semantics>
      </flt-semantics-host>
      <div id="painted-blocker"></div>
    """
    root = (
        f'<flutter-view id="root"><flt-glass-pane>{inner}</flt-glass-pane></flutter-view>'
        if root_kind == "modern"
        else f'<flt-glass-pane id="legacy-root">{inner}</flt-glass-pane>'
    )
    html = f"""
    <style>
      html, body {{ margin: 0; width: 240px; height: 320px; }}
      flutter-view, flt-glass-pane, canvas, flt-semantics-host {{
        position: fixed; inset: 0; width: 240px; height: 320px;
      }}
      flt-semantics-host {{ pointer-events: none; }}
      flt-semantics {{ position: absolute; display: block; left: 20px; top: 40px;
        width: 100px; height: 40px; opacity: .01; pointer-events: none; }}
      #painted-blocker {{ position: fixed; left: 10px; top: 30px; width: 120px;
        height: 60px; z-index: 100; background: rgb(40, 50, 60); }}
    </style>
    {root}
    """

    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
        finally:
            await context.close()
            await chromium.close()

    semantics = next(
        item for item in snapshot["elements"] if item.get("aria_label") == "同根普通遮罩"
    )
    assert semantics["visible"] is False
    assert semantics["hit_test"]["center_hits_target"] is False
    assert semantics["hit_test"]["visual_occlusion_detected"] is True


@pytest.mark.parametrize(
    ("root_kind", "platform_view_tag"),
    [("modern", "flt-platform-view"), ("legacy", "flt-platform-view-slot")],
)
async def test_real_browser_semantics_rejects_painted_platform_view_mask(
    root_kind: str,
    platform_view_tag: str,
) -> None:
    """同根 Platform View 承载真实绘制内容，不能冒充透明 renderer surface。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    inner = f"""
      <canvas id="surface" width="240" height="320"></canvas>
      <flt-semantics-host>
        <flt-semantics id="target" flt-tappable role="button"
          aria-label="平台视图遮挡"></flt-semantics>
      </flt-semantics-host>
      <{platform_view_tag} id="painted-platform-view"></{platform_view_tag}>
    """
    root = (
        f'<flutter-view id="root"><flt-glass-pane>{inner}</flt-glass-pane></flutter-view>'
        if root_kind == "modern"
        else f'<flt-glass-pane id="legacy-root">{inner}</flt-glass-pane>'
    )
    html = f"""
    <style>
      html, body {{ margin: 0; width: 240px; height: 320px; }}
      flutter-view, flt-glass-pane, canvas, flt-semantics-host {{
        position: fixed; inset: 0; width: 240px; height: 320px;
      }}
      flt-semantics-host {{ pointer-events: none; }}
      flt-semantics {{ position: absolute; display: block; left: 20px; top: 40px;
        width: 100px; height: 40px; opacity: .01; pointer-events: none; }}
      #painted-platform-view {{ position: fixed; left: 10px; top: 30px;
        width: 120px; height: 60px; z-index: 100;
        background: rgb(90, 40, 120); }}
    </style>
    {root}
    """

    async with async_playwright() as playwright:
        chromium = await playwright.chromium.launch(headless=True)
        context = await chromium.new_context(viewport={"width": 240, "height": 320})
        page = await context.new_page()
        try:
            await page.set_content(html)
            snapshot = await _collect_ui_snapshot(page)
        finally:
            await context.close()
            await chromium.close()

    semantics = next(
        item for item in snapshot["elements"] if item.get("aria_label") == "平台视图遮挡"
    )
    assert semantics["visible"] is False
    assert semantics["hit_test"]["center_hits_target"] is False
    assert semantics["hit_test"]["visual_occlusion_detected"] is True


async def test_final_after_artifact_reuses_second_stable_frame_without_public_intermediate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """after_1 只参与摘要，既有截图协议只持久化最终 after_2。"""
    after_1 = b"private-intermediate-frame"
    image = np.full((320, 240, 3), 245, dtype=np.uint8)
    cv2.rectangle(image, (16, 60), (223, 250), (40, 40, 40), 3)
    encoded, payload = cv2.imencode(".png", image)
    assert encoded is True
    after_2 = payload.tobytes()

    class Page:
        url = "https://example.test/flutter-after"

        async def screenshot(self, **options: Any) -> bytes:
            assert options == {"full_page": False}
            return b"existing-final-screenshot"

    monkeypatch.setattr(
        page_capture_module,
        "get_settings",
        lambda: SimpleNamespace(
            workflow=SimpleNamespace(h5_visual_coordinate_runtime_mode="enforced")
        ),
    )
    prepared_snapshot = {
        "schema_version": "ui-snapshot.v2",
        "url": Page.url,
        "visual_navigation_capture": {
            "protocol_version": "visual_navigation_capture.v1",
            "purpose": "visual_navigation_viewport",
            "coordinate_space": "visual_viewport_css_pixels",
            "screenshot_index": 2,
        },
        "visual_layout_evidence": _visual_layout_evidence_for_page_capture_test(),
        "logical_surface": {"protocol_version": "logical_surface.v1"},
        "elements": [],
    }

    artifacts = await page_capture_module._collect_artifacts(
        Page(),
        PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            capture_dom=False,
            capture_visible_text=False,
            full_page=False,
        ),
        logical_surface_enabled=True,
        prepared_visual_snapshot=prepared_snapshot,
        prepared_visual_viewport_png=after_2,
    )

    screenshots = [item for item in artifacts if item.artifact_type == "screenshot"]
    assert len(screenshots) == 2
    assert base64.b64decode(screenshots[1].content["data"]) == after_2
    serialized = repr([item.to_dict() for item in artifacts])
    assert after_1.decode() not in serialized
    assert sum(item.artifact_type == "ui_snapshot" for item in artifacts) == 1


def _visual_layout_evidence_for_page_capture_test() -> dict[str, Any]:
    return {
        "protocol_version": "visual_layout_evidence.v1",
        "identity": {
            "top_structure": {"category": "app_bar", "segment_count": 1},
            "bottom_navigation": {"slot_count": 4, "active_slot": 1},
            "main_layout_category": "detail",
        },
        "state_audit": {
            "main_density_bucket": "medium",
            "animation_bucket": "unknown",
            "full_frame_change_bucket": "not_compared",
        },
    }


async def test_uncommitted_inner_timeout_keeps_timeout_error() -> None:
    """未发出 click 的普通采集超时仍保持既有可重试超时语义。"""
    request = PageCaptureRequest(mode=PageCaptureMode.OBSERVE)

    class Browser:
        async def capture(self, current_request: PageCaptureRequest) -> PageCaptureResult:
            del current_request
            await asyncio.Event().wait()
            raise AssertionError("不可达")

    with pytest.raises(TimeoutError):
        await page_capture_module.capture_page_with_timeout(
            request=request,
            browser=Browser(),
            timeout_seconds=0.01,
        )

    assert page_capture_module._visual_action_was_committed(request) is False


async def test_committed_direct_cancellation_is_not_converted_to_timeout() -> None:
    """非 deadline 发起的 CancelledError 必须穿透 timeout 包装且不二击。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    page.cdp_session.cancel_during_detach = True
    request = _visual_navigation_click_request(_visual_navigation_runtime_target(source_rect=rect))

    class Browser:
        async def capture(self, current_request: PageCaptureRequest) -> PageCaptureResult:
            await _run_single_trigger(
                page=page,
                context=page.context,
                request=current_request,
                action_trace=[],
                timeout_ms=30_000,
            )
            raise AssertionError("任务取消后不可达")

    with pytest.raises(asyncio.CancelledError):
        await page_capture_module.capture_page_with_timeout(
            request=request,
            browser=Browser(),
            timeout_seconds=1.0,
        )

    assert page_capture_module._visual_action_was_committed(request) is True
    assert page.mouse.clicks == [(112.0, 122.0)]


def test_visual_action_commit_holder_cannot_be_injected_by_request_parameters() -> None:
    """Agent 侧普通 PageCaptureRequest 参数不得构造或复用内部提交槽。"""
    with pytest.raises(TypeError):
        PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            _visual_action_commit_holder=object(),  # type: ignore[call-arg]
        )


@pytest.mark.parametrize(
    ("failure_kind", "control_flow"),
    [
        ("dispose", SystemExit("dispose-control-private")),
        ("release", KeyboardInterrupt("release-control-private")),
    ],
    ids=("system-exit", "keyboard-interrupt"),
)
async def test_visual_navigation_postclick_cleanup_control_flow_is_propagated(
    caplog: pytest.LogCaptureFixture,
    failure_kind: str,
    control_flow: BaseException,
) -> None:
    """点击提交后，进程控制流必须原样传播且不能诱发第二次 click。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    if failure_kind == "dispose":
        page.cdp_session.dispose_error = control_flow
    elif failure_kind == "release":
        page.cdp_session.release_error = control_flow
    else:
        raise AssertionError(f"未处理的控制流阶段：{failure_kind}")

    with (
        caplog.at_level("WARNING", logger=page_capture_module.__name__),
        pytest.raises(type(control_flow)) as error,
    ):
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value is control_flow
    assert page.mouse.clicks == [(112.0, 122.0)]
    assert page.event_log.count("mouse.click") == 1
    assert "视觉点击已完成，隔离 tracker 清理被控制流中断" in caplog.text
    assert "private" not in caplog.text


async def test_visual_navigation_main_frame_loader_change_never_clicks() -> None:
    """同 URL、同 viewport 的 main frame loader 换代也必须作废能力。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png)
    page.cdp_session.loader_ids = ["loader-1", "loader-1", "loader-2"]

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert page.cdp_session.detached is True
    assert page.mouse.clicks == []


async def test_visual_navigation_incomplete_history_summary_never_clicks() -> None:
    """History 摘要一旦命中预算上限，必须在视觉解析和 click 前失败关闭。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(
        current_png,
        page_states=[_visual_navigation_page_state(history_state_complete=False)],
    )

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert page.screenshot_calls == []
    assert page.mouse.clicks == []


@pytest.mark.parametrize(
    ("observer_active", "expired"),
    [
        (False, True),
        (False, False),
        (True, True),
    ],
    ids=("expired", "inactive", "contradictory"),
)
async def test_visual_navigation_inactive_observer_never_clicks(
    observer_active: bool,
    expired: bool,
) -> None:
    """隔离 observer 非 active 或已 expired 时必须在截图前固定 stale。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(
        current_png,
        page_states=[
            _visual_navigation_page_state(
                observer_active=observer_active,
                expired=expired,
            )
        ],
    )

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert page.screenshot_calls == []
    assert page.mouse.clicks == []


@pytest.mark.parametrize(
    "page_states",
    [
        [
            _visual_navigation_page_state(),
            _visual_navigation_page_state(dom_mutation_generation=1),
        ],
        [
            _visual_navigation_page_state(),
            _visual_navigation_page_state(time_origin_ms=654_321),
        ],
        [
            _visual_navigation_page_state(),
            _visual_navigation_page_state(history_state_digest="v1:" + "1" * 64),
        ],
        [
            _visual_navigation_page_state(),
            _visual_navigation_page_state(url="https://example.com/other"),
        ],
    ],
    ids=(
        "same-viewport-generation-change",
        "same-url-document-replacement",
        "history-state-generation-change",
        "url-change",
    ),
)
async def test_visual_navigation_browser_rejects_page_generation_change_after_first_frame(
    page_states: list[Mapping[str, object]],
) -> None:
    """首帧截图窗口内即使 viewport 相同，页面换代也必须零点击。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png, page_states=page_states)

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert page.mouse.clicks == []


@pytest.mark.parametrize(
    "page_states",
    [
        [
            _visual_navigation_page_state(),
            _visual_navigation_page_state(),
            _visual_navigation_page_state(dom_mutation_generation=1),
        ],
        [
            _visual_navigation_page_state(),
            _visual_navigation_page_state(),
            _visual_navigation_page_state(),
            _visual_navigation_page_state(dom_mutation_generation=1),
        ],
        [
            _visual_navigation_page_state(),
            _visual_navigation_page_state(),
            _visual_navigation_page_state(
                viewport_update={"offset_top": 1},
            ),
        ],
    ],
    ids=(
        "generation-change-before-second-frame-read",
        "generation-change-after-second-frame-read",
        "viewport-change-after-second-frame",
    ),
)
async def test_visual_navigation_browser_rejects_state_change_around_second_frame(
    page_states: list[Mapping[str, object]],
) -> None:
    """第二帧前后或最终紧邻点击复核发生变化时必须零点击。"""
    rect = (82, 92, 142, 152)
    current_png, _ = _visual_navigation_runtime_frame(rect=rect)
    page = _VisualNavigationRuntimePage(current_png, page_states=page_states)

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert page.mouse.clicks == []


async def test_visual_navigation_browser_rejects_target_change_between_preclick_frames() -> None:
    """第二张点击前帧目标被遮挡时，不得把首帧成功当成点击授权。"""
    rect = (82, 92, 142, 152)
    first_png, _ = _visual_navigation_runtime_frame(rect=rect)
    second_image = cv2.imdecode(
        np.frombuffer(first_png, dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert second_image is not None
    cv2.rectangle(second_image, (82, 92), (141, 151), (116, 116, 116, 255), -1)
    encoded, payload = cv2.imencode(".png", second_image)
    assert encoded is True
    page = _VisualNavigationRuntimePage([first_png, payload.tobytes()])

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == "VISUAL_TARGET_OBSTRUCTED"
    assert page.mouse.clicks == []
    assert len(page.screenshot_calls) == 2


@pytest.mark.parametrize(
    ("current_kind", "expected_code"),
    [
        ("moved", "VISUAL_TARGET_SOURCE_STATE_STALE"),
        ("duplicate", "VISUAL_TARGET_NOT_UNIQUE"),
        ("covered", "VISUAL_TARGET_OBSTRUCTED"),
        ("low_match", "VISUAL_TARGET_SOURCE_STATE_STALE"),
    ],
)
async def test_visual_navigation_browser_failure_paths_never_click(
    current_kind: str,
    expected_code: str,
) -> None:
    """每个复验失败分支都必须在 mouse.click 之前关闭。"""
    source_rect = (82, 92, 142, 152)
    if current_kind == "moved":
        current_png, _ = _visual_navigation_runtime_frame(rect=(90, 100, 150, 160))
    elif current_kind == "duplicate":
        current_png, _ = _visual_navigation_runtime_frame(
            rect=source_rect,
            extra_rect=(162, 92, 222, 152),
        )
    elif current_kind == "covered":
        current_png, _ = _visual_navigation_runtime_frame(rect=source_rect)
        image = cv2.imdecode(np.frombuffer(current_png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        assert image is not None
        cv2.rectangle(image, (82, 92), (141, 151), (116, 116, 116, 255), -1)
        encoded, payload = cv2.imencode(".png", image)
        assert encoded is True
        current_png = payload.tobytes()
    elif current_kind == "low_match":
        rng = np.random.default_rng(20260829)
        noise = rng.integers(0, 256, size=(240, 240, 4), dtype=np.uint8)
        noise[:, :, 3] = 255
        encoded, payload = cv2.imencode(".png", noise)
        assert encoded is True
        current_png = payload.tobytes()
    else:
        raise AssertionError(f"未处理的实时帧变体：{current_kind}")
    page = _VisualNavigationRuntimePage(current_png)

    with pytest.raises(BlockedPageCaptureActionError) as error:
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=source_rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert error.value.error_code == expected_code
    assert page.mouse.clicks == []


def test_visual_navigation_browser_rejects_full_page_click_frame() -> None:
    """视觉导航请求不得借用 full_page 截图作为动作坐标帧。"""
    target = _visual_navigation_runtime_target(source_rect=(82, 92, 142, 152))

    with pytest.raises(BlockedPageCaptureActionError) as error:
        _validate_request(_visual_navigation_click_request(target, full_page=True))

    assert error.value.error_code == "VISUAL_TARGET_COORDINATE_INVALID"


_VISUAL_NAVIGATION_LIVE_HTML = """
<style>
  html, body { margin: 0; width: 240px; height: 240px; overflow: hidden; }
  body {
    background-color: rgb(238, 238, 238);
    background-image:
      linear-gradient(90deg, transparent 36px, rgb(224, 230, 236) 37px),
      linear-gradient(transparent 40px, rgb(230, 222, 228) 41px);
    background-size: 37px 41px;
  }
  #visual-target {
    position: absolute; left: 82px; top: 92px; width: 60px; height: 60px;
    box-sizing: border-box; border: 4px solid rgb(246, 199, 44);
    background:
      linear-gradient(45deg, transparent 44%, white 45%, white 51%, transparent 52%),
      linear-gradient(-45deg, transparent 43%, rgb(22, 24, 28) 44%,
        rgb(22, 24, 28) 49%, transparent 50%),
      rgb(38, 62, 186);
  }
</style>
<button id="visual-target" onclick="window.visualClickCount += 1"></button>
<script>window.visualClickCount = 0;</script>
"""


def _visual_navigation_tracker_expression_for_contract_test(
    *,
    history_state_expression: str,
    observer_ttl_ms: int,
) -> str:
    expression = page_capture_module._VISUAL_NAVIGATION_ISOLATED_TRACKER_EXPRESSION
    assert "__ALIOTH_TRACKER_NONCE__" in expression
    assert "__ALIOTH_HISTORY_STATE__" in expression
    assert "__ALIOTH_OBSERVER_TTL_MS__" in expression
    return (
        expression.replace("__ALIOTH_TRACKER_NONCE__", "contract-test-nonce")
        .replace("__ALIOTH_HISTORY_STATE__", history_state_expression)
        .replace("__ALIOTH_OBSERVER_TTL_MS__", str(observer_ttl_ms))
    )


async def _visual_navigation_call_tracker_sample(
    session: Any,
    object_id: str,
) -> Mapping[str, object]:
    response = await session.send(
        "Runtime.callFunctionOn",
        {
            "functionDeclaration": "async function () { return await this.sample(); }",
            "objectId": object_id,
            "returnByValue": True,
            "awaitPromise": True,
            "silent": True,
        },
    )
    result = response["result"]
    assert isinstance(result, Mapping)
    value = result["value"]
    assert isinstance(value, Mapping)
    return value


async def test_visual_navigation_live_history_summary_has_global_work_budget() -> None:
    """超大容器不得被全展开，Object getter 和容器 iterator 工作量必须有上限。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    variants = [
        (
            "huge-map",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            const state = new Map();
            for (let index = 0; index < 20000; index += 1) {
              state.set(index, `value-${index}`);
            }
            const originalEntries = state.entries.bind(state);
            state.entries = () => {
              const iterator = originalEntries();
              return {
                next() {
                  globalThis.__aliothBudgetCounter += 1;
                  return iterator.next();
                },
                [Symbol.iterator]() { return this; },
              };
            };
            globalThis.__aliothTestState = state;
            """,
            0,
        ),
        (
            "huge-set",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            const state = new Set();
            for (let index = 0; index < 20000; index += 1) state.add(index);
            const originalValues = state.values.bind(state);
            state.values = () => {
              const iterator = originalValues();
              return {
                next() {
                  globalThis.__aliothBudgetCounter += 1;
                  return iterator.next();
                },
                [Symbol.iterator]() { return this; },
              };
            };
            globalThis.__aliothTestState = state;
            """,
            0,
        ),
        (
            "bounded-map-iterator",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            const state = new Map();
            Object.defineProperty(state, "size", { value: 1024 });
            state.entries = () => ({
              next() {
                globalThis.__aliothBudgetCounter += 1;
                if (globalThis.__aliothBudgetCounter > 1025) {
                  throw new Error("Map iterator 超出 N+1 预算");
                }
                return { done: false, value: [0, 0] };
              },
              [Symbol.iterator]() { return this; },
            });
            globalThis.__aliothTestState = state;
            """,
            1025,
        ),
        (
            "bounded-set-iterator",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            const state = new Set();
            Object.defineProperty(state, "size", { value: 1024 });
            state.values = () => ({
              next() {
                globalThis.__aliothBudgetCounter += 1;
                if (globalThis.__aliothBudgetCounter > 1025) {
                  throw new Error("Set iterator 超出 N+1 预算");
                }
                return { done: false, value: 0 };
              },
              [Symbol.iterator]() { return this; },
            });
            globalThis.__aliothTestState = state;
            """,
            1025,
        ),
        (
            "huge-object",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            const state = {};
            for (let index = 0; index < 20000; index += 1) {
              Object.defineProperty(state, `key-${index}`, {
                enumerable: true,
                get() {
                  globalThis.__aliothBudgetCounter += 1;
                  return index;
                },
              });
            }
            globalThis.__aliothOriginalObjectKeys = Object.keys;
            globalThis.__aliothOriginalArraySort = Array.prototype.sort;
            Object.keys = () => { throw new Error("Object.keys 禁止全量枚举"); };
            Array.prototype.sort = () => { throw new Error("超限对象禁止排序"); };
            globalThis.__aliothTestState = state;
            """,
            1024,
        ),
        (
            "huge-array",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            const state = new Array(1000000);
            Object.defineProperty(state, 0, {
              get() {
                globalThis.__aliothBudgetCounter += 1;
                return "should-not-be-read";
              },
            });
            globalThis.__aliothTestState = state;
            """,
            0,
        ),
        (
            "deep-object",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            let state = { leaf: true };
            for (let depth = 0; depth < 100; depth += 1) state = { child: state };
            globalThis.__aliothTestState = state;
            """,
            0,
        ),
        (
            "giant-string",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            globalThis.__aliothTestState = "x".repeat(1000000);
            """,
            0,
        ),
        (
            "huge-typed-data",
            r"""
            globalThis.__aliothBudgetCounter = 0;
            globalThis.__aliothTestState = new Uint8Array(1000000);
            """,
            0,
        ),
    ]

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("data:text/html,<main>history-budget</main>")
        session = await page.context.new_cdp_session(page)
        frame_tree = await session.send("Page.getFrameTree")
        frame_id = frame_tree["frameTree"]["frame"]["id"]
        world = await session.send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": "alioth_visual_navigation_budget_contract",
                "grantUniveralAccess": False,
            },
        )
        context_id = world["executionContextId"]
        try:
            for variant, setup_script, maximum_counter in variants:
                await session.send(
                    "Runtime.evaluate",
                    {
                        "expression": setup_script,
                        "contextId": context_id,
                        "returnByValue": True,
                    },
                )
                created = await session.send(
                    "Runtime.evaluate",
                    {
                        "expression": (
                            _visual_navigation_tracker_expression_for_contract_test(
                                history_state_expression=("globalThis.__aliothTestState"),
                                observer_ttl_ms=30_000,
                            )
                        ),
                        "contextId": context_id,
                        "returnByValue": False,
                        "awaitPromise": True,
                    },
                )
                object_id = created["result"]["objectId"]
                sample = await _visual_navigation_call_tracker_sample(
                    session,
                    object_id,
                )
                counter_response = await session.send(
                    "Runtime.evaluate",
                    {
                        "expression": "globalThis.__aliothBudgetCounter",
                        "contextId": context_id,
                        "returnByValue": True,
                    },
                )
                assert sample["history_state_complete"] is False, variant
                assert counter_response["result"]["value"] <= maximum_counter, variant
                await session.send(
                    "Runtime.evaluate",
                    {
                        "expression": r"""
                        if (globalThis.__aliothOriginalObjectKeys) {
                          Object.keys = globalThis.__aliothOriginalObjectKeys;
                          delete globalThis.__aliothOriginalObjectKeys;
                        }
                        if (globalThis.__aliothOriginalArraySort) {
                          Array.prototype.sort = globalThis.__aliothOriginalArraySort;
                          delete globalThis.__aliothOriginalArraySort;
                        }
                        """,
                        "contextId": context_id,
                        "returnByValue": True,
                    },
                )
                await session.send(
                    "Runtime.callFunctionOn",
                    {
                        "functionDeclaration": "function () { this.dispose(); }",
                        "objectId": object_id,
                        "returnByValue": True,
                    },
                )
                await session.send(
                    "Runtime.releaseObject",
                    {"objectId": object_id},
                )
        finally:
            await session.detach()
            await browser.close()


async def test_visual_navigation_live_tracker_self_expires_after_lost_response() -> None:
    """客户端即使拿不到创建响应，隔离 observer 也必须按 TTL 自动 disconnect。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<main id='state'>before</main>")
        session = await page.context.new_cdp_session(page)
        frame_tree = await session.send("Page.getFrameTree")
        frame_id = frame_tree["frameTree"]["frame"]["id"]
        world = await session.send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": "alioth_visual_navigation_ttl_contract",
                "grantUniveralAccess": False,
            },
        )
        context_id = world["executionContextId"]
        created = await session.send(
            "Runtime.evaluate",
            {
                "expression": _visual_navigation_tracker_expression_for_contract_test(
                    history_state_expression="null",
                    observer_ttl_ms=300,
                ),
                "contextId": context_id,
                "returnByValue": False,
                "awaitPromise": True,
            },
        )
        object_id = created["result"]["objectId"]
        baseline = await _visual_navigation_call_tracker_sample(session, object_id)
        await page.evaluate("document.querySelector('#state').textContent = 'first'")
        await asyncio.sleep(0.03)
        changed = await _visual_navigation_call_tracker_sample(session, object_id)
        await asyncio.sleep(0.35)
        await page.evaluate("document.querySelector('#state').textContent = 'second'")
        await asyncio.sleep(0.03)
        expired = await _visual_navigation_call_tracker_sample(session, object_id)

        assert baseline["observer_active"] is True
        assert baseline["expired"] is False
        assert changed["observer_active"] is True
        assert changed["expired"] is False
        assert expired["observer_active"] is False
        assert expired["expired"] is True
        assert changed["dom_mutation_generation"] > baseline["dom_mutation_generation"]
        assert expired["dom_mutation_generation"] == changed["dom_mutation_generation"]
        assert not await page.evaluate(
            "Reflect.ownKeys(globalThis).some((key) => "
            "String(key).includes('alioth_visual_navigation_ttl_contract'))"
        )
        await session.send(
            "Runtime.releaseObject",
            {"objectId": object_id},
        )
        await session.detach()
        await browser.close()


async def test_visual_navigation_live_tracker_sample_slides_expiry_window() -> None:
    """总时长超过 TTL 时，只要每次 sample 间隔未超限，observer 就必须持续 active。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<main>sliding-ttl</main>")
        session = await page.context.new_cdp_session(page)
        frame_tree = await session.send("Page.getFrameTree")
        frame_id = frame_tree["frameTree"]["frame"]["id"]
        world = await session.send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": "alioth_visual_navigation_sliding_ttl_contract",
                "grantUniveralAccess": False,
            },
        )
        context_id = world["executionContextId"]
        created = await session.send(
            "Runtime.evaluate",
            {
                "expression": _visual_navigation_tracker_expression_for_contract_test(
                    history_state_expression="null",
                    observer_ttl_ms=300,
                ),
                "contextId": context_id,
                "returnByValue": False,
                "awaitPromise": True,
            },
        )
        object_id = created["result"]["objectId"]
        first = await _visual_navigation_call_tracker_sample(session, object_id)
        await asyncio.sleep(0.19)
        second = await _visual_navigation_call_tracker_sample(session, object_id)
        await asyncio.sleep(0.19)
        third = await _visual_navigation_call_tracker_sample(session, object_id)

        assert first["observer_active"] is True
        assert second["observer_active"] is True
        assert third["observer_active"] is True
        assert third["expired"] is False
        await session.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": "function () { this.dispose(); }",
                "objectId": object_id,
                "returnByValue": True,
            },
        )
        await session.send(
            "Runtime.releaseObject",
            {"objectId": object_id},
        )
        await session.detach()
        await browser.close()


async def test_visual_navigation_live_tracker_rejects_overdue_sample_before_timer_runs() -> None:
    """事件循环阻塞超过 TTL 后，sample 必须按隔离单调 deadline 拒绝抢先续期。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.set_content("<main>deadline-ttl</main>")
        session = await page.context.new_cdp_session(page)
        frame_tree = await session.send("Page.getFrameTree")
        frame_id = frame_tree["frameTree"]["frame"]["id"]
        world = await session.send(
            "Page.createIsolatedWorld",
            {
                "frameId": frame_id,
                "worldName": "alioth_visual_navigation_deadline_ttl_contract",
                "grantUniveralAccess": False,
            },
        )
        context_id = world["executionContextId"]
        created = await session.send(
            "Runtime.evaluate",
            {
                "expression": _visual_navigation_tracker_expression_for_contract_test(
                    history_state_expression="null",
                    observer_ttl_ms=200,
                ),
                "contextId": context_id,
                "returnByValue": False,
                "awaitPromise": True,
            },
        )
        object_id = created["result"]["objectId"]
        response = await session.send(
            "Runtime.callFunctionOn",
            {
                "functionDeclaration": r"""
                async function () {
                  const blockedUntil = performance.now() + 300;
                  while (performance.now() < blockedUntil) {}
                  return await this.sample();
                }
                """,
                "objectId": object_id,
                "returnByValue": True,
                "awaitPromise": True,
                "silent": True,
            },
        )
        overdue = response["result"]["value"]

        assert overdue["observer_active"] is False
        assert overdue["expired"] is True
        await session.send(
            "Runtime.releaseObject",
            {"objectId": object_id},
        )
        await session.detach()
        await browser.close()


async def test_visual_navigation_live_request_timeout_expires_tracker_before_click(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """生产视觉路径必须按本次硬动作预算派生 TTL，过期后稳定图像也不得点击。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    monkeypatch.setattr(
        page_capture_module,
        "_VISUAL_NAVIGATION_OBSERVER_TTL_SAFETY_MARGIN_MS",
        30,
        raising=False,
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 240, "height": 240})
        await page.set_content(_VISUAL_NAVIGATION_LIVE_HTML)
        source_png = await page.screenshot(full_page=False, type="png")
        original_screenshot = page.screenshot
        screenshot_count = 0

        async def delay_first_preclick_frame(**kwargs: object) -> bytes:
            nonlocal screenshot_count
            payload = await original_screenshot(**kwargs)
            screenshot_count += 1
            if screenshot_count == 1:
                await asyncio.sleep(0.12)
            return payload

        monkeypatch.setattr(page, "screenshot", delay_first_preclick_frame)
        with pytest.raises(BlockedPageCaptureActionError) as error:
            await _run_single_trigger(
                page=page,
                context=page.context,
                request=_visual_navigation_click_request(
                    _visual_navigation_runtime_target_from_png(
                        source_png=source_png,
                        source_rect=(82, 92, 142, 152),
                    )
                ),
                action_trace=[],
                timeout_ms=30,
            )

        assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
        assert await page.evaluate("window.visualClickCount") == 0
        await browser.close()


async def test_visual_navigation_live_chromium_hides_tracker_from_main_world(
    monkeypatch,
) -> None:
    """真实 Chromium 主 world 不得发现视觉点击 tracker 的 seal 或状态。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 240, "height": 240})
        await page.set_content(_VISUAL_NAVIGATION_LIVE_HTML)
        source_png = await page.screenshot(full_page=False, type="png")
        original_screenshot = page.screenshot
        tracker_visibility: list[bool] = []

        async def observe_main_world(**kwargs: object) -> bytes:
            payload = await original_screenshot(**kwargs)
            tracker_visibility.append(
                await page.evaluate(
                    r"""
                    () => Reflect.ownKeys(globalThis).some((key) => (
                      typeof key === "symbol"
                      && String(key).includes("alioth.visualNavigation")
                    ))
                    """
                )
            )
            return payload

        monkeypatch.setattr(page, "screenshot", observe_main_world)
        await _run_single_trigger(
            page=page,
            context=page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target_from_png(
                    source_png=source_png,
                    source_rect=(82, 92, 142, 152),
                )
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

        assert tracker_visibility == [False, False]
        assert await page.evaluate("window.visualClickCount") == 1
        await browser.close()


@pytest.mark.parametrize(
    "attack_script",
    [
        r"""
        () => {
          const oldKey = Symbol.for("alioth.visualNavigation.preClickGeneration");
          const exposed = globalThis[oldKey];
          if (exposed) {
            exposed.page_generation = 0;
            exposed.history_generation = 0;
          }
          history.pushState = () => undefined;
          History.prototype.replaceState.call(
            history,
            { phase: "same-url-adversarial-state" },
            "",
            location.href,
          );
        }
        """,
        r"""
        () => {
          document.body.dataset.visualGeneration = "changed";
        }
        """,
    ],
    ids=("old-symbol-history-prototype-bypass", "dom-mutation"),
)
async def test_visual_navigation_live_chromium_adversarial_state_change_never_clicks(
    monkeypatch,
    attack_script: str,
) -> None:
    """主 world 绕过旧包装或修改 DOM 都必须由隔离事实识别。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        pytest.skip("playwright 未安装")

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 240, "height": 240})
        await page.set_content(_VISUAL_NAVIGATION_LIVE_HTML)
        source_png = await page.screenshot(full_page=False, type="png")
        original_screenshot = page.screenshot
        screenshot_count = 0

        async def attack_after_first_frame(**kwargs: object) -> bytes:
            nonlocal screenshot_count
            payload = await original_screenshot(**kwargs)
            screenshot_count += 1
            if screenshot_count == 1:
                await page.evaluate(attack_script)
            return payload

        monkeypatch.setattr(page, "screenshot", attack_after_first_frame)
        with pytest.raises(BlockedPageCaptureActionError) as error:
            await _run_single_trigger(
                page=page,
                context=page.context,
                request=_visual_navigation_click_request(
                    _visual_navigation_runtime_target_from_png(
                        source_png=source_png,
                        source_rect=(82, 92, 142, 152),
                    )
                ),
                action_trace=[],
                timeout_ms=30_000,
            )

        assert error.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
        assert await page.evaluate("window.visualClickCount") == 0
        await browser.close()


async def test_new_capability_clicks_once_while_old_capability_stays_stale() -> None:
    """测试模拟新 Capture 重签；执行端本批不得自动重签或二次 relocate。"""
    old_rect = (74, 84, 134, 144)
    new_rect = (82, 92, 142, 152)
    next_rect = (90, 100, 150, 160)
    new_png, _ = _visual_navigation_runtime_frame(rect=new_rect)
    next_png, _ = _visual_navigation_runtime_frame(rect=next_rect)
    stale_page = _VisualNavigationRuntimePage(new_png)
    new_page = _VisualNavigationRuntimePage(new_png)
    second_stale_page = _VisualNavigationRuntimePage(next_png)

    with pytest.raises(BlockedPageCaptureActionError) as first_stale:
        await _run_single_trigger(
            page=stale_page,
            context=stale_page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=old_rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )
    await _run_single_trigger(
        page=new_page,
        context=new_page.context,
        request=_visual_navigation_click_request(
            _visual_navigation_runtime_target(source_rect=new_rect)
        ),
        action_trace=[],
        timeout_ms=30_000,
    )
    with pytest.raises(BlockedPageCaptureActionError) as second_stale:
        await _run_single_trigger(
            page=second_stale_page,
            context=second_stale_page.context,
            request=_visual_navigation_click_request(
                _visual_navigation_runtime_target(source_rect=new_rect)
            ),
            action_trace=[],
            timeout_ms=30_000,
        )

    assert first_stale.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert stale_page.mouse.clicks == []
    assert new_page.mouse.clicks == [(112.0, 122.0)]
    assert second_stale.value.error_code == "VISUAL_TARGET_SOURCE_STATE_STALE"
    assert second_stale_page.mouse.clicks == []


def test_ui_snapshot_size_bounding_retains_community_home_post_title() -> None:
    """新版社区无 item_container 时，正文标题在裁剪后仍须保留。"""
    post_title = {
        "selector": ".community_home .van-list > div:first-child .poi_title",
        "tag": "div",
        "class_tokens": ["poi_title", "!text-12px"],
        "ancestor_class_tokens": ["van-list", "community_home_tab"],
        "text": "当前无遮挡帖子标题",
        "rect": {"x": 16, "y": 430, "width": 358, "height": 38},
        "hit_test": {
            "center_hits_target": True,
            "topmost": True,
            "pointer_interception": True,
        },
    }
    filler = [
        {
            "selector": f"#section-{index}",
            "tag": "div",
            "class_tokens": ["section"],
            "text": "容器文本" * 2_000,
            "rect": {"y": 100, "height": 20},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/Community",
            "elements": [*filler, post_title],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert post_title["selector"] in kept_selectors


def test_ui_snapshot_size_bounding_retains_manga_video_title() -> None:
    """漫画视频标题是详情点击的密封证明，512 KiB 裁剪后仍须保留。"""
    manga_title = {
        "selector": ".item-box-comics:first-child .duohang2",
        "tag": "div",
        "class_tokens": ["duohang2", "text-11"],
        "ancestor_class_tokens": ["item-box-comics", "box"],
        "text": "漫画视频标题",
        "rect": {"x": 16, "y": 350, "width": 104, "height": 34},
        "hit_test": {
            "center_hits_target": True,
            "topmost": True,
            "pointer_interception": True,
        },
    }
    filler = [
        {
            "selector": f"#section-{index}",
            "tag": "div",
            "class_tokens": ["section"],
            "text": "容器文本" * 2_000,
            "rect": {"y": 100, "height": 20},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/twoDimensional",
            "elements": [*filler, manga_title],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert manga_title["selector"] in kept_selectors


def test_ui_snapshot_size_bounding_retains_long_video_card_title() -> None:
    """首页长视频标题是详情点击的密封证明，裁剪后仍须保留。"""
    video_title = {
        "selector": ".longVideoCard:first-child .line-clamp-2 > p",
        "tag": "p",
        "class_tokens": [],
        "ancestor_class_tokens": [
            "line-clamp-2",
            "longVideoCard",
            "item-wrapper",
        ],
        "text": "当前无遮挡视频标题",
        "rect": {"x": 16, "y": 488, "width": 175, "height": 37},
        "hit_test": {
            "center_hits_target": True,
            "topmost": True,
            "pointer_interception": True,
        },
    }
    filler = [
        {
            "selector": f"#section-{index}",
            "tag": "div",
            "class_tokens": ["section"],
            "text": "容器文本" * 2_000,
            "rect": {"y": 100, "height": 20},
        }
        for index in range(300)
    ]

    snapshot = _sanitize_and_bound_ui_snapshot(
        {
            "url": "https://example.com/",
            "elements": [*filler, video_title],
        }
    )

    assert snapshot["truncated_reason"] == "serialized_size_limit"
    kept_selectors = {item["selector"] for item in snapshot["elements"]}
    assert video_title["selector"] in kept_selectors


async def test_h5_payment_horizontal_collection_captures_each_viewport_until_end(
    monkeypatch,
) -> None:
    states = [
        {
            "url": "https://example.com/vip",
            "container_selector": ".vip-plan-list",
            "scroll_left": 0,
            "scroll_width": 900,
            "client_width": 300,
            "at_end": False,
        },
        {
            "url": "https://example.com/vip",
            "container_selector": ".vip-plan-list",
            "scroll_left": 255,
            "scroll_width": 900,
            "client_width": 300,
            "at_end": False,
        },
        {
            "url": "https://example.com/vip",
            "container_selector": ".vip-plan-list",
            "scroll_left": 600,
            "scroll_width": 900,
            "client_width": 300,
            "at_end": True,
        },
    ]
    state_index = 0

    async def collect_state(page, *, selector):
        nonlocal state_index
        assert selector == ".vip-plan-list"
        value = states[min(state_index, len(states) - 1)]
        state_index += 1
        return value

    async def run_trigger(**kwargs):
        kwargs["action_trace"].append({"action": "scroll"})

    async def wait_for_settle(**kwargs):
        return None

    async def collect_snapshot(page, **kwargs):
        del kwargs
        return {
            "schema_version": "ui-snapshot.v2",
            "url": "https://example.com/vip",
            "viewport": {"width": 390, "height": 844},
            "structure_signature": "payment-signature",
            "elements": [{"selector": ".vip-plan-list", "visible": True}],
        }

    monkeypatch.setattr(
        page_capture_module,
        "_collect_horizontal_scroll_state",
        collect_state,
    )
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", run_trigger)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", wait_for_settle)
    monkeypatch.setattr(page_capture_module, "_collect_ui_snapshot", collect_snapshot)

    class Page:
        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            return b"viewport"

    artifacts = await PlaywrightPageCaptureBrowser()._collect_horizontal_scroll_artifacts(
        page=Page(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="scroll",
            selector=".vip-plan-list",
            session_id="h5-payment-horizontal",
            horizontal_scroll_max_segments=6,
            split_scroll_screenshots=True,
        ),
        action_trace=[],
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
        baseline_ui_snapshot=await collect_snapshot(None),
    )

    screenshots = [item for item in artifacts if item.artifact_type == "screenshot"]
    coverage = next(
        item.content for item in artifacts if item.artifact_type == "page_coverage"
    )
    assert len(screenshots) == 3
    assert coverage["axis"] == "horizontal"
    assert coverage["termination_reason"] == "end_reached"
    assert coverage["coverage_complete"] is True
    assert [item["scroll_left_after"] for item in coverage["segments"]] == [0, 255, 600]


async def test_h5_payment_horizontal_collection_stops_after_no_effect(monkeypatch) -> None:
    states = [
        {
            "url": "https://example.com/vip",
            "container_selector": ".vip-plan-list",
            "scroll_left": 0,
            "scroll_width": 900,
            "client_width": 300,
            "at_end": False,
        },
        {
            "url": "https://example.com/vip",
            "container_selector": ".vip-plan-list",
            "scroll_left": 0,
            "scroll_width": 900,
            "client_width": 300,
            "at_end": False,
        },
    ]

    async def collect_state(page, *, selector):
        del page
        assert selector == ".vip-plan-list"
        return states.pop(0)

    async def run_trigger(**kwargs):
        kwargs["action_trace"].append({"action": "scroll"})

    async def wait_for_settle(**kwargs):
        return None

    async def collect_snapshot(page, **kwargs):
        del page, kwargs
        return {"url": "https://example.com/vip", "elements": []}

    monkeypatch.setattr(
        page_capture_module,
        "_collect_horizontal_scroll_state",
        collect_state,
    )
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", run_trigger)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", wait_for_settle)
    monkeypatch.setattr(page_capture_module, "_collect_ui_snapshot", collect_snapshot)

    artifacts = await PlaywrightPageCaptureBrowser()._collect_horizontal_scroll_artifacts(
        page=SimpleNamespace(screenshot=None),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="scroll",
            selector=".vip-plan-list",
            capture_screenshot=False,
            horizontal_scroll_max_segments=6,
        ),
        action_trace=[],
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
        baseline_ui_snapshot={"url": "https://example.com/vip", "elements": []},
    )

    coverage = next(
        item.content for item in artifacts if item.artifact_type == "page_coverage"
    )
    assert coverage["termination_reason"] == "scroll_no_effect"
    assert coverage["coverage_complete"] is False
    assert len(coverage["segments"]) == 1


async def test_h5_payment_horizontal_trigger_dispatches_swiper_slide_next() -> None:
    class Locator:
        async def evaluate(self, script: str) -> str:
            assert "swiper.slideNext(0)" in script
            return "swiper_slide_next"

    class Page:
        def locator(self, selector: str) -> Locator:
            assert selector == ".swiper_content"
            return Locator()

    action_trace: list[dict[str, Any]] = []
    await _run_single_trigger(
        page=Page(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="scroll",
            selector=".swiper_content",
            horizontal_scroll_max_segments=6,
        ),
        action_trace=action_trace,
        timeout_ms=30_000,
    )

    assert action_trace == [
        {
            "action": "scroll",
            "selector": ".swiper_content",
            "result": "horizontal_collection_scroll_dispatched",
            "horizontal_dispatch_mode": "swiper_slide_next",
        }
    ]


async def test_h5_payment_swiper_collection_uses_active_index_until_end(monkeypatch) -> None:
    states = [
        {
            "url": "https://example.com/vip",
            "container_selector": ".swiper_content",
            "scroll_left": 0,
            "scroll_width": 1465,
            "client_width": 373,
            "scroll_mode": "swiper",
            "swiper_active_index": 0,
            "swiper_slide_count": 3,
            "swiper_is_end": False,
            "at_end": False,
        },
        {
            "url": "https://example.com/vip",
            "container_selector": ".swiper_content",
            "scroll_left": 0,
            "scroll_width": 1465,
            "client_width": 373,
            "scroll_mode": "swiper",
            "swiper_active_index": 1,
            "swiper_slide_count": 3,
            "swiper_is_end": False,
            "at_end": False,
        },
        {
            "url": "https://example.com/vip",
            "container_selector": ".swiper_content",
            "scroll_left": 0,
            "scroll_width": 1465,
            "client_width": 373,
            "scroll_mode": "swiper",
            "swiper_active_index": 2,
            "swiper_slide_count": 3,
            "swiper_is_end": True,
            "at_end": True,
        },
    ]

    async def collect_state(page, *, selector):
        del page
        assert selector == ".swiper_content"
        return states.pop(0)

    async def run_trigger(**kwargs):
        kwargs["action_trace"].append({"action": "scroll"})

    async def wait_for_settle(**kwargs):
        return None

    async def collect_snapshot(page, **kwargs):
        del page, kwargs
        return {"url": "https://example.com/vip", "elements": []}

    monkeypatch.setattr(page_capture_module, "_collect_horizontal_scroll_state", collect_state)
    monkeypatch.setattr(page_capture_module, "_run_single_trigger", run_trigger)
    monkeypatch.setattr(page_capture_module, "_wait_for_page_settle", wait_for_settle)
    monkeypatch.setattr(page_capture_module, "_collect_ui_snapshot", collect_snapshot)

    class Page:
        async def screenshot(self, *, full_page: bool) -> bytes:
            assert full_page is False
            return b"swiper-viewport"

    artifacts = await PlaywrightPageCaptureBrowser()._collect_horizontal_scroll_artifacts(
        page=Page(),
        context=object(),
        request=PageCaptureRequest(
            mode=PageCaptureMode.SINGLE_TRIGGER,
            trigger_action="scroll",
            selector=".swiper_content",
            horizontal_scroll_max_segments=6,
            split_scroll_screenshots=True,
        ),
        action_trace=[],
        timeout_ms=30_000,
        timeout_error_type=TimeoutError,
        baseline_ui_snapshot={"url": "https://example.com/vip", "elements": []},
    )

    screenshots = [item for item in artifacts if item.artifact_type == "screenshot"]
    coverage = next(
        item.content for item in artifacts if item.artifact_type == "page_coverage"
    )
    assert len(screenshots) == 3
    assert coverage["termination_reason"] == "end_reached"
    assert coverage["coverage_complete"] is True
    assert [item["swiper_active_index_after"] for item in coverage["segments"]] == [
        0,
        1,
        2,
    ]
