"""测试 page_detection_tool：完整证据进入独立多模态请求。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import math
import re
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import cv2
import numpy as np
import pytest
from agent_framework import Content, Message
from PIL import Image
from urllib3.exceptions import ProtocolError

from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools import page_detection_tools  # noqa: F401
from services.api.app.agents.tools.h5_visual_navigation_detection import (
    VisualNavigationModelOutput,
)
from services.api.app.agents.tools.page_detection_tools import (
    ConfiguredEvidenceArtifactReader,
    PageDetectionModelOutput,
    PageDetectionResult,
    PageEvidenceArtifact,
    PageEvidenceBundle,
    PostgresPageEvidenceLoader,
    _bind_logical_transition_to_surface,
    _build_multimodal_messages,
    _decode_screenshot_artifact,
    _deterministic_transient_page_state,
    _has_flutter_fullscreen_startup_ad_skip,
    _is_rule_matchable_page_state,
    _logical_surface_with_recomputed_ocr_coverage,
    _page_type_confidence,
    _sanitize_and_chunk_dom,
    _verified_bottom_navigation_effect,
    detect_page_for_agent,
    page_detection_tool,
    persist_page_detection_for_current_run,
)
from services.api.app.agents.tools.registry import PermissionLevel, default_registry
from services.api.app.application.capture_evidence_readiness import CaptureEvidenceReadiness
from services.api.app.application.h5_list_detail_navigation import (
    h5_list_detail_selector_allowed,
    h5_list_detail_selector_requires_visible_title,
    h5_list_detail_snapshot_element_matches,
)
from services.api.app.application.h5_logical_surface_evidence import (
    build_h5_logical_surface,
)
from services.api.app.application.page_exploration_profile import H5ExplorationProfile, H5TabbarItem
from services.api.app.core.config import EvidenceStorageSettings
from services.api.app.domain.exploration.h5_logical_surface import (
    stable_h5_surface_hash,
)
from services.api.app.domain.page.catalog import load_default_page_type_catalog
from services.api.app.domain.page.nav_alias import (
    PageNavAliasMatch,
    PageNavAliasResolver,
)
from services.api.app.domain.page.registry import recognition_signal_references


def _test_png(
    *,
    width: int = 390,
    height: int = 844,
    local_color: tuple[int, int, int] = (40, 80, 120),
    anchor_color: tuple[int, int, int] = (20, 30, 40),
) -> bytes:
    """生成可被生产 PNG 解码器读取的确定性视口测试图。"""
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = anchor_color
    image[
        math.floor(height * 0.2) : math.ceil(height * 0.5),
        math.floor(width * 0.08) : math.ceil(width * 0.92),
    ] = local_color
    encoded, payload = cv2.imencode(".png", image)
    assert encoded is True
    return payload.tobytes()


def _mode_png(
    mode: str,
    *,
    width: int = 390,
    height: int = 844,
    local_alpha: int = 180,
    anchor_alpha: int = 240,
    compress_level: int = 6,
) -> bytes:
    """生成像素相同但通道/Alpha/压缩可控的真实 PNG。"""
    if mode == "L":
        pixels = np.full((height, width), 40, dtype=np.uint8)
        pixels[
            math.floor(height * 0.2) : math.ceil(height * 0.5),
            math.floor(width * 0.08) : math.ceil(width * 0.92),
        ] = 80
    elif mode == "LA":
        pixels = np.empty((height, width, 2), dtype=np.uint8)
        pixels[:, :] = (40, anchor_alpha)
        pixels[
            math.floor(height * 0.2) : math.ceil(height * 0.5),
            math.floor(width * 0.08) : math.ceil(width * 0.92),
        ] = (80, local_alpha)
    elif mode == "RGB":
        pixels = np.full((height, width, 3), 40, dtype=np.uint8)
        pixels[
            math.floor(height * 0.2) : math.ceil(height * 0.5),
            math.floor(width * 0.08) : math.ceil(width * 0.92),
        ] = (80, 80, 80)
    elif mode == "RGBA":
        pixels = np.empty((height, width, 4), dtype=np.uint8)
        pixels[:, :] = (40, 40, 40, anchor_alpha)
        pixels[
            math.floor(height * 0.2) : math.ceil(height * 0.5),
            math.floor(width * 0.08) : math.ceil(width * 0.92),
        ] = (80, 80, 80, local_alpha)
    else:
        raise AssertionError(f"不支持的测试 PNG mode：{mode}")
    buffer = io.BytesIO()
    Image.fromarray(pixels, mode=mode).save(
        buffer,
        format="PNG",
        compress_level=compress_level,
    )
    return buffer.getvalue()


PNG_BYTES = _test_png()


def test_page_detection_emits_navigate_with_destination_url() -> None:
    candidate = page_detection_tools.normalize_h5_detection_candidate(
        {"action": "goto", "url": "/products"}
    )

    assert candidate["action"] == "navigate"
    assert candidate["destination_url"] == "/products"


def test_refined_page_type_uses_selected_candidate_self_score() -> None:
    payload = _recognized_output("home").model_dump()
    payload.update(
        confidence=0.92,
        page_type_candidates=[
            {"page_type": "content_list", "confidence": 0.45, "matched_signal_count": 2}
        ],
    )
    output = PageDetectionModelOutput.model_validate(payload)

    confidence, source = _page_type_confidence(
        model_output=output,
        result_page_type="content_list",
    )

    assert confidence == 0.45
    assert source == "model_candidate_self_report"


def test_unmatched_page_type_refinement_keeps_half_confidence_cap() -> None:
    output = _recognized_output("home")

    confidence, source = _page_type_confidence(
        model_output=output,
        result_page_type="profile",
    )

    assert confidence == 0.5
    assert source == "deterministic_refinement_cap"


def test_logical_transition_must_bind_current_after_identity() -> None:
    surface = {
        "identity_fingerprint": "current-identity",
        "logical_transition_proof": "trusted",
    }

    bound = _bind_logical_transition_to_surface(
        surface,
        {
            "protocol_version": "logical_surface_transition.v1",
            "after_identity_fingerprint": "other-identity",
            "logical_transition_proof": "trusted",
        },
    )

    assert bound["logical_transition_proof"] == "insufficient"
    assert bound["transition_binding_error"] == "after_identity_mismatch"


class FakeEvidenceLoader:
    def __init__(self, bundle: PageEvidenceBundle | None) -> None:
        self.bundle = bundle
        self.calls: list[tuple[str, str]] = []

    async def load(self, *, task_id: str, page_capture_id: str) -> PageEvidenceBundle | None:
        self.calls.append((task_id, page_capture_id))
        return self.bundle


class FakeArtifactReader:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    async def read(self, artifact: PageEvidenceArtifact) -> bytes:
        self.calls.append(artifact.artifact_id)
        return self.payloads[artifact.artifact_id]


class FakeNavAliasResolver:
    def __init__(self, matches: dict[str, PageNavAliasMatch]) -> None:
        self.matches = matches
        self.calls: list[str] = []

    async def resolve(self, label: str) -> PageNavAliasMatch | None:
        self.calls.append(label)
        return self.matches.get(label)


class VectorNavAliasRepository:
    async def find_confirmed_exact(self, normalized_text: str) -> PageNavAliasMatch | None:
        return None

    async def find_confirmed_vector_match(
        self,
        *,
        embedding: tuple[float, ...],
        embedding_model: str,
        min_similarity: float,
    ) -> PageNavAliasMatch | None:
        return PageNavAliasMatch(
            canonical_code="content_list",
            page_subtype_code="comic_anime",
            matched_term="漫画",
            source_category="漫画",
            confidence=0.88,
            match_type="vector",
        )


class FakeChatClient:
    def __init__(self, value: PageDetectionModelOutput) -> None:
        self.value = value
        self.calls: list[dict[str, Any]] = []

    async def get_response(
        self,
        messages: list[Message],
        *,
        options: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"messages": messages, "options": options})
        return SimpleNamespace(value=self.value)


class FakePageCaptureRepository:
    def __init__(self, *, updated: bool = True) -> None:
        self.updated = updated
        self.calls: list[dict[str, Any]] = []

    async def update_detection_result(self, **kwargs: Any) -> bool:
        self.calls.append(kwargs)
        return self.updated


def _bundle(
    *artifacts: PageEvidenceArtifact,
    capture_metadata: dict[str, Any] | None = None,
    page_url: str = "https://example.com/home",
    page_title: str = "首页",
) -> PageEvidenceBundle:
    return PageEvidenceBundle(
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
        page_url=page_url,
        page_title=page_title,
        artifacts=tuple(artifacts),
        capture_metadata=capture_metadata,
    )


def _artifact(
    artifact_id: str,
    artifact_type: str,
    *,
    content_type: str,
) -> PageEvidenceArtifact:
    return PageEvidenceArtifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        storage_path=f"task/capture/{artifact_id}",
        content_type=content_type,
    )


def _recognized_output(page_type: str = "home") -> PageDetectionModelOutput:
    return PageDetectionModelOutput(
        detection_status="recognized",
        page_type=page_type,
        page_state="stable",
        confidence=0.94,
        reason="DOM 包含首页导航，截图显示首页内容区。",
        matched_catalog_signals={"dom_hints": ["tabbar"], "layout_hints": ["首页内容区"]},
        page_type_candidates=[
            {"page_type": page_type, "confidence": 0.94, "matched_signal_count": 2}
        ],
        candidate_actions=[],
        next_page_hypotheses=[],
        risk_flags=[],
        missing_sources=[],
        conflicts=[],
    )


def _assert_semantic_popup_action(
    action: dict[str, object] | page_detection_tools.CandidateAction,
    capability: str,
) -> None:
    payload = action if isinstance(action, dict) else action.model_dump()
    assert payload["action"] == "click"
    assert payload["target"] == "关闭当前弹窗"
    assert payload["risk_level"] == "low"
    assert payload["navigation_depth"] == 0
    assert payload["obstruction_capability"] == capability
    assert payload["target_keywords"][:3] == [
        "popup_dismissal",
        "close_control",
        capability,
    ]
    signature = payload["obstruction_signature"]
    assert isinstance(signature, str)
    assert signature.startswith("h5-obstruction:")
    assert len(signature) == len("h5-obstruction:") + 64


def test_page_detection_tool_is_the_only_registered_page_detection_tool() -> None:
    metadata = default_registry.get("page_detection_tool")

    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SAFE
    assert default_registry.get("page_type_catalog_tool") is None
    assert default_registry.get("page_detection_result_tool") is None
    parameters = page_detection_tool.parameters()
    assert set(parameters["properties"]) == {"page_capture_id"}
    assert parameters["required"] == ["page_capture_id"]


async def test_page_detection_binds_flutter_surface_without_confidence_cross_raise() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    ui_snapshot = _artifact(
        "snapshot-1",
        "ui_snapshot",
        content_type="application/json",
    )
    surface = build_h5_logical_surface(
        ui_snapshot={
            "url": "https://example.com/",
            "title": "个人中心",
            "flutter_semantics_activation": "enabled",
            "document": {"time_origin_ms": 1000.0},
            "elements": [
                {
                    "selector": "flt-semantics#101",
                    "tag": "flt-semantics",
                    "role": "button",
                    "text": "我的",
                    "class_tokens": ["active"],
                    "structural_navigation_kind": "flutter_bottom_navigation",
                    "interactive": True,
                    "rect": {"x": 270, "y": 720, "width": 90, "height": 60},
                },
                {
                    "selector": "flt-semantics#102",
                    "tag": "flt-semantics",
                    "role": "heading",
                    "text": "个人中心",
                    "class_tokens": [],
                    "rect": {"x": 0, "y": 0, "width": 200, "height": 50},
                },
            ],
        },
        page_url="https://example.com/",
        surface_session_id="session-1",
        document_token="document-1",
    )
    surface["shadow"] = False
    surface["logical_transition_proof"] = "trusted"
    loader = FakeEvidenceLoader(
        _bundle(dom, screenshot, ui_snapshot, page_url="https://example.com/")
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                "<html><body><main><h1>个人中心</h1><button>设置</button></main></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
            "snapshot-1": json.dumps(
                {"schema_version": "ui-snapshot.v2", "logical_surface": surface},
                ensure_ascii=False,
            ).encode(),
        }
    )
    client = FakeChatClient(_recognized_output("profile"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "profile"
    assert result["confidence"] == 0.94
    assert result["logical_surface"]["confidence_domains"] == {
        "page_type_confidence": 0.94,
        "render_profile_confidence": 0.96,
    }
    assert result["logical_surface"]["page_type_binding"]["coverage_allowed"] is True
    assert result["rule_matching_allowed"] is True


async def test_invalid_optional_ui_snapshot_does_not_block_legacy_detection() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    ui_snapshot = _artifact(
        "snapshot-invalid",
        "ui_snapshot",
        content_type="application/json",
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot, ui_snapshot)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-1": b"<html><body><main>home</main></body></html>",
                    "shot-1": PNG_BYTES,
                    "snapshot-invalid": b"{invalid-json",
                }
            ),
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="固定页面识别流程",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["rule_matching_allowed"] is True
    assert "logical_surface" not in result


async def test_h5_recognition_guide_downgrades_model_result_without_signal_references() -> None:
    dom = _artifact("dom-guide", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-guide", "screenshot", content_type="image/png")

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-guide-missing",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-guide": (
                        b"<html><body><main>home navigation complete content</main></body></html>"
                    ),
                    "shot-guide": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="固定页面识别流程",
            nav_alias_resolver=FakeNavAliasResolver({}),
            recognition_guide_enforced=True,
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["model_page_type"] == "home"
    assert result["recognition_guide_audit"]["reason_code"] == ("required_positive_signal_missing")


async def test_h5_recognition_guide_accepts_complete_model_signal_references() -> None:
    dom = _artifact("dom-guide", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-guide", "screenshot", content_type="image/png")
    catalog = load_default_page_type_catalog()
    home = catalog.require_code("home")
    references = recognition_signal_references(home)
    output_payload = _recognized_output("home").model_dump()
    output_payload.update(
        matched_catalog_signals={
            "required_positive_signal_ids": [
                item["signal_id"] for item in references["required_positive_signals"]
            ],
            "excluded_confusable_page_types": list(home.confusable_with),
        },
    )
    output = PageDetectionModelOutput.model_validate(output_payload)

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-guide-complete",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-guide": b"<html><body><main>home navigation content</main></body></html>",
                    "shot-guide": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(output),
            skill_text="固定页面识别流程",
            nav_alias_resolver=FakeNavAliasResolver({}),
            recognition_guide_enforced=True,
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["rule_matching_allowed"] is True
    assert result["recognition_guide_audit"]["reason_code"] == ("recognition_guide_satisfied")


def test_dom_is_sanitized_and_fully_chunked_without_summary_truncation() -> None:
    dom = "<html><body>BEGIN" + ("甲" * 9000) + "<script>恶意指令</script>END</body></html>"

    chunks = _sanitize_and_chunk_dom(dom, chunk_size=512)
    rebuilt = "".join(chunks)

    assert len(chunks) > 10
    assert "BEGIN" in rebuilt
    assert "END" in rebuilt
    assert rebuilt.count("甲") == 9000
    assert "恶意指令" not in rebuilt


def test_visible_loader_deterministically_blocks_rule_matching() -> None:
    state = _deterministic_transient_page_state(
        page_type="home",
        dom_chunks=['<main>首页内容<div class="loader"><span>加载中</span></div></main>'],
    )
    assert state == "loading"
    assert _is_rule_matchable_page_state(page_type="home", page_state=state) is False


def test_hidden_loader_does_not_downgrade_stable_page() -> None:
    state = _deterministic_transient_page_state(
        page_type="content_detail",
        dom_chunks=['<main>详情内容<div class="loader" style="display:none">加载中</div></main>'],
    )
    assert state is None
    assert (
        _is_rule_matchable_page_state(
            page_type="content_detail",
            page_state="detail",
        )
        is True
    )
    assert (
        _is_rule_matchable_page_state(
            page_type="content_detail",
            page_state="loading",
        )
        is False
    )


def test_computed_ready_dynamic_page_ignores_local_transition_and_loader_classes() -> None:
    state = _deterministic_transient_page_state(
        page_type="content_detail",
        dom_chunks=[
            '<main>详情内容<div class="carousel-enter-active loading">轮播推荐</div></main>'
        ],
        computed_page_ready=True,
    )

    assert state is None


def test_computed_ready_page_still_blocks_explicit_line_loading_text() -> None:
    state = _deterministic_transient_page_state(
        page_type="content_detail",
        dom_chunks=["<main>正在读取最快线路</main>"],
        computed_page_ready=True,
    )

    assert state == "line_loading"


def test_startup_ad_countdown_text_deterministically_blocks_stable_state() -> None:
    state = _deterministic_transient_page_state(
        page_type="startup_ad",
        dom_chunks=['<flt-semantics role="text">广告倒计时: 6</flt-semantics>'],
        computed_page_ready=True,
    )

    assert state == "countdown"


def test_passive_fullscreen_startup_splash_requests_same_session_reobservation() -> None:
    state = _deterministic_transient_page_state(
        page_type="startup_ad",
        dom_chunks=[
            '<div id="app"></div><div id="SplashScreenID"><img src="/startup.jpg" alt=""></div>'
        ],
        computed_page_ready=True,
    )

    assert state == "countdown"


async def test_nprogress_busy_empty_launch_shell_emits_trusted_structure_fact() -> None:
    """真实 NProgress 空启动壳只签发确定性结构事实，不猜测页面类型。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={
                "pipeline_metrics": {
                    "page_settle_complete": True,
                    "page_inspection_ready": True,
                }
            },
            page_url="https://example.com/booting",
            page_title="",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b'<html class="theme-dark nprogress-busy"><body>'
                b'<div id="app" class="nprogress-custom-parent">'
                b'<div class="app-theme"><!----><!----></div>'
                b'<div id="nprogress"><div class="bar" role="bar" '
                b'style="transform: translate3d(-42%, 0, 0)">'
                b'<div class="peg"></div></div></div></div>'
                b"</body></html>"
            ),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(
        PageDetectionModelOutput(
            detection_status="ambiguous",
            page_type=None,
            page_state="loading",
            confidence=0.42,
            reason="只看到空白启动壳和顶部进度条。",
            matched_catalog_signals={},
            page_type_candidates=[],
            candidate_actions=[],
            next_page_hypotheses=[],
            risk_flags=[],
            missing_sources=[],
            conflicts=[],
        )
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-nprogress-launch-shell",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["page_state"] == "loading"
    assert result["candidate_actions"] == []
    assert result["metrics"]["nprogress_busy_empty_launch_shell_structure"] is True
    assert "deterministic_nprogress_busy_empty_launch_shell" in result["risk_flags"]


@pytest.mark.parametrize(
    "dom",
    [
        pytest.param(
            '<html class="nprogress-busy"><body>'
            '<div id="app" class="nprogress-custom-parent">'
            '<main class="home-page">业务首页</main></div>'
            '<div id="nprogress"><div class="bar" role="bar"></div></div>'
            "</body></html>",
            id="business-content",
        ),
        pytest.param(
            '<html class="nprogress-busy"><body>'
            '<div id="app" class="nprogress-custom-parent"></div>'
            '<div id="nprogress"><div class="bar" role="bar"></div>'
            '<div class="bar" role="bar"></div></div>'
            "</body></html>",
            id="multiple-bars",
        ),
        pytest.param(
            '<html class="nprogress-busy"><body>'
            '<div id="app" class="nprogress-custom-parent"></div>'
            '<div id="nprogress"><div class="bar" role="bar" '
            'style="display:none"></div></div>'
            "</body></html>",
            id="hidden-bar",
        ),
        pytest.param(
            '<html><body><div id="app" class="nprogress-custom-parent"></div>'
            '<div id="nprogress"><div class="bar" role="bar"></div></div>'
            "</body></html>",
            id="html-not-busy",
        ),
    ],
)
def test_nprogress_background_or_inexact_structure_is_not_launch_shell(dom: str) -> None:
    """业务内容、重复或隐藏进度条均不得冒充空启动壳。"""
    assert page_detection_tools._has_nprogress_busy_empty_launch_shell_structure(dom) is False


@pytest.mark.parametrize(
    "dom",
    [
        '<style>#SplashScreenID{position:fixed}</style><div id="app"></div>',
        '<div id="SplashScreenID" style="display:none"><img src="/startup.jpg"></div>',
        '<div id="SplashScreenID"><img src="/content.jpg"></div>',
    ],
)
def test_incomplete_or_hidden_startup_splash_does_not_request_reobservation(
    dom: str,
) -> None:
    assert (
        _deterministic_transient_page_state(
            page_type="startup_ad",
            dom_chunks=[dom],
            computed_page_ready=True,
        )
        is None
    )


async def test_unsettled_capture_skips_model_and_requests_reobservation() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={"pipeline_metrics": {"page_settle_complete": False}},
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": b"<html><body><main>partial</main></body></html>",
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["detection_status"] == "insufficient"
    assert result["page_state"] == "needs_more_observation"
    assert result["missing_sources"] == ["page_stability"]
    assert client.calls == []


async def test_empty_flutter_bootstrap_marks_bounded_reobservation_fact() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={
                "pipeline_metrics": {
                    "page_settle_complete": False,
                    "page_inspection_ready": False,
                    "page_settle_diagnostics": {"readiness": {"flutter_bootstrap_pending": True}},
                }
            },
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b"<html><head><title>51\xe8\x8c\xb6\xe9\xa6\x86</title></head><body>"
                b'<script src="flutter_bootstrap.js" async></script>'
                b"</body></html>"
            ),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["detection_status"] == "insufficient"
    assert result["page_state"] == "needs_more_observation"
    assert "deterministic_flutter_bootstrap_pending" in result["risk_flags"]
    assert client.calls == []


async def test_safety_interstitial_is_detected_without_model_call() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    visible_text = _artifact("text-1", "visible_text", content_type="text/plain")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            visible_text,
            screenshot,
            page_url="https://rd.mhhyj29j.top/",
            page_title="Bot detection",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b"<html><title>Bot detection</title><body>"
                b"This site is safe, you can visit it with confidence."
                b"<button>Click to visit now</button></body></html>"
            ),
            "text-1": (
                b"rd.mhhyj29j.top\n"
                b"This site is safe, you can visit it with confidence.\n"
                b"Click to visit now"
            ),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["page_state"] == "safety_interstitial"
    assert result["rule_matching_allowed"] is False
    assert "deterministic_safety_interstitial" in result["risk_flags"]
    assert "target_blocked_by_safety_interstitial" in result["conflicts"]
    assert result["metrics"]["visible_text_character_count"] > 0
    assert client.calls == []


async def test_robot_image_verification_is_safety_interstitial_without_model_call() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    visible_text = _artifact("text-1", "visible_text", content_type="text/plain")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            visible_text,
            screenshot,
            page_url="https://example.test/",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                "<html><body><div class='captcha-dialog'>"
                "<h2>机器人验证</h2>"
                "<input placeholder='请输入图片中的文字'>"
                "<button>换一个</button><button>确定</button>"
                "</div></body></html>"
            ).encode(),
            "text-1": "机器人验证\n换一个\n确定".encode(),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("startup_ad"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["page_state"] == "safety_interstitial"
    assert result["candidate_actions"] == []
    assert result["rule_matching_allowed"] is False
    assert "deterministic_safety_interstitial" in result["risk_flags"]
    assert client.calls == []


async def test_download_button_in_hidden_dom_does_not_terminate_content_page() -> None:
    """撸啊撸实测回归：内容站页脚藏着下载按钮，不能被当成下载引导页终止探索。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    visible_text = _artifact("text-1", "visible_text", content_type="text/plain")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            visible_text,
            screenshot,
            page_url="https://rkxyoy.thornway.vip/",
            page_title="撸啊撸",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                "<html><title>撸啊撸</title><body><main>"
                "<div class='tabs'><span>精选</span><span>黑料</span><span>伦理</span></div>"
                "<div class='list'><a href='/v/1'><img src='/1.jpg'>福利姬合集</a>"
                "<a href='/v/2'><img src='/2.jpg'>校园反差</a></div>"
                "<footer><a class='hidden' id='downloadBtn2'><span>安卓版下载</span></a>"
                "</footer></main></body></html>"
            ).encode(),
            "text-1": "精选\n黑料\n伦理\n淫妻\n校园".encode(),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("video_list"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["page_type"] != "guide_page"
    assert "guide_page_terminal" not in result["risk_flags"]


async def test_single_visible_download_cta_does_not_terminate_content_page() -> None:
    """页脚只挂一个下载按钮的内容站，缺少落地页佐证，不得终止探索。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    visible_text = _artifact("text-1", "visible_text", content_type="text/plain")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            visible_text,
            screenshot,
            page_url="https://example.com/",
            page_title="示例内容站",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                "<html><title>示例内容站</title><body><main>"
                "<div class='list'><a href='/v/1'>视频一</a><a href='/v/2'>视频二</a></div>"
                "<footer><a><span>安卓版下载</span></a></footer></main></body></html>"
            ).encode(),
            "text-1": "精选\n最新\n排行\n视频一\n视频二\n安卓版下载".encode(),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("video_list"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["page_type"] != "guide_page"


async def test_multi_platform_download_landing_page_still_terminates() -> None:
    """91bbs.net 实测正样本：多端安装包 + 装不上怎么办，仍按下载引导页终止。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    visible_text = _artifact("text-1", "visible_text", content_type="text/plain")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            visible_text,
            screenshot,
            page_url="https://91bbs.net/",
            page_title="黑料网",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                "<html><title>黑料网</title><body><main>"
                "<a>安卓手机下载</a><a>苹果手机下载</a>"
                "<p>安卓安装包报毒解决方案</p></main></body></html>"
            ).encode(),
            "text-1": (
                "安卓安装包报毒解决方案\n下载后开启飞行模式安装\n"
                "安卓手机下载\n苹果手机下载\n官网地址\n常见问题"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["page_type"] == "guide_page"
    assert result["rule_matching_allowed"] is False
    assert "guide_page_terminal" in result["risk_flags"]
    assert client.calls == []


def _flutter_startup_dom(
    *,
    control_text: str,
    control_style: str,
    wrap_in_dialog: bool,
    control_role: str = "",
) -> str:
    """按杏吧实测结构拼 Flutter 开屏语义树：全屏可点层 → (可选 dialog 壳) → 右上角控件。"""
    role_attr = f' role="{control_role}"' if control_role else ""
    control = (
        f'<flt-semantics id="flt-semantic-node-7" tabindex="0" flt-tappable=""{role_attr} '
        f'style="{control_style}"><span>{control_text}</span></flt-semantics>'
    )
    inner = (
        f'<flt-semantics id="flt-semantic-node-2" role="dialog" '
        f'style="position: absolute; width: 390px; height: 844px; pointer-events: none;">'
        f"<flt-semantics-container>{control}</flt-semantics-container></flt-semantics>"
        if wrap_in_dialog
        else control
    )
    return (
        "<html><body><flt-semantics-host>"
        '<flt-semantics id="flt-semantic-node-1" flt-tappable="" '
        'style="position: absolute; width: 390px; height: 844px; pointer-events: all;">'
        f"<flt-semantics-container>{inner}</flt-semantics-container>"
        "</flt-semantics></flt-semantics-host></body></html>"
    )


_FLUTTER_CORNER_CONTROL_STYLE = (
    "position: absolute; width: 46px; height: 46px; "
    "transform: matrix(1, 0, 0, 1, 328, 45); pointer-events: all; z-index: 2;"
)


def test_flutter_startup_enter_control_behind_dialog_shell_is_dismissable() -> None:
    """杏吧实测：开屏控件写「进入」且父层夹了 dialog 壳，仍要认成可清障的开屏跳过控件。"""
    dom = _flutter_startup_dom(
        control_text="进入",
        control_style=_FLUTTER_CORNER_CONTROL_STYLE,
        wrap_in_dialog=True,
    )

    assert _has_flutter_fullscreen_startup_ad_skip(dom) is True


def test_flutter_startup_skip_control_without_dialog_shell_still_dismissable() -> None:
    """原「跳过」直连全屏层、带 role=text 的结构不能因为放宽父层解析而失效。"""
    dom = _flutter_startup_dom(
        control_text="跳过",
        control_style=_FLUTTER_CORNER_CONTROL_STYLE,
        wrap_in_dialog=False,
        control_role="text",
    )

    assert _has_flutter_fullscreen_startup_ad_skip(dom) is True


def test_flutter_startup_skip_control_under_fullscreen_group_is_dismissable() -> None:
    """黑料网实测：全屏可点根声明 role=group 时仍应签发右上角跳过能力。"""
    dom = _flutter_startup_dom(
        control_text="跳过",
        control_style=(
            "position: absolute; width: 61.5002px; height: 35px; "
            "transform: matrix(1, 0, 0, 1, 313.5, 20); "
            "pointer-events: all; z-index: 2;"
        ),
        wrap_in_dialog=False,
        control_role="text",
    ).replace(
        '<flt-semantics id="flt-semantic-node-1" flt-tappable="" ',
        '<flt-semantics id="flt-semantic-node-1" flt-tappable="" role="group" ',
        1,
    )

    assert _has_flutter_fullscreen_startup_ad_skip(dom) is True


def test_flutter_startup_skip_structure_must_be_verified_on_raw_dom() -> None:
    """杏吧实测：开屏结构只能在原始 DOM 边界验签，清洗后的模型视图签不出来。

    贴在 (0,0) 的全屏开屏层 Flutter 不发 transform，清洗层会连宽高一起抹掉；
    早先把结构证明放在清洗后的 dom_chunks 上，开屏清障能力在真实管道里永远
    签不出——只有喂原始 DOM 才像是好的。与「我的收藏/设置」返回结构同一套边界。
    """
    dom = _flutter_startup_dom(
        control_text="进入",
        control_style=_FLUTTER_CORNER_CONTROL_STYLE,
        wrap_in_dialog=True,
    )
    sanitized = "".join(_sanitize_and_chunk_dom(dom))

    assert _has_flutter_fullscreen_startup_ad_skip(dom) is True
    assert _has_flutter_fullscreen_startup_ad_skip(sanitized) is False
    assert (
        page_detection_tools._startup_ad_dismissal_selector(
            page_type="startup_ad",
            dom=sanitized,
            has_verified_flutter_startup_ad_skip=True,
        )
        == page_detection_tools.FLUTTER_FULLSCREEN_STARTUP_AD_DISMISSAL_SELECTOR
    )
    assert (
        page_detection_tools._startup_ad_dismissal_selector(
            page_type="startup_ad",
            dom=sanitized,
            has_verified_flutter_startup_ad_skip=False,
        )
        is None
    )


def test_flutter_corner_control_with_unrelated_text_is_not_startup_skip() -> None:
    """右上角同样几何但文案不是开屏清障词，不得当成开屏跳过控件点下去。"""
    dom = _flutter_startup_dom(
        control_text="客服",
        control_style=_FLUTTER_CORNER_CONTROL_STYLE,
        wrap_in_dialog=True,
    )

    assert _has_flutter_fullscreen_startup_ad_skip(dom) is False


def test_flutter_center_placed_enter_control_is_not_startup_skip() -> None:
    """不在右上角的「进入」是页面里的普通按钮，几何不符就不能清障。"""
    dom = _flutter_startup_dom(
        control_text="进入",
        control_style=(
            "position: absolute; width: 46px; height: 46px; "
            "transform: matrix(1, 0, 0, 1, 120, 400); pointer-events: all; z-index: 2;"
        ),
        wrap_in_dialog=True,
    )

    assert _has_flutter_fullscreen_startup_ad_skip(dom) is False


async def test_h5_age_verification_gate_signs_only_exact_adult_confirm_structure() -> None:
    """图片型年龄门禁只允许精确结构中的成年确认，不得交给模型猜按钮。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            page_url="https://da8ttmrfqqhqf.cloudfront.net/?devTest=123",
            page_title="",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b'<html><body><div class="age-verify-popup">'
                b'<div class="age-verify-overlay"></div>'
                b'<div class="age-verify-content">'
                b'<img class="age-verify-bg" src="/png/eighteenYearsBg.png">'
                b'<div class="age-verify-buttons">'
                b'<div class="btn-exit"></div><div class="btn-confirm"></div>'
                b"</div></div></div></body></html>"
            ),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["page_state"] == "age_verification_blocking"
    assert result["rule_matching_allowed"] is False
    assert result["risk_flags"] == [
        "age_restricted_content",
        "age_verification_required",
        "underlying_page_obscured",
        "deterministic_h5_age_verification",
    ]
    assert result["candidate_actions"] == [
        {
            "action": "click",
            "target": "年满进入",
            "target_keywords": [
                "age_verification",
                "adult_confirm",
                "evidence:structure",
            ],
            "risk_level": "low",
            "purpose": "确认已成年并进入当前 H5 业务页面",
            "expected_next_page_type": None,
            "navigation_depth": 0,
            "obstruction_capability": "h5_age_verification_confirm.v1",
            "obstruction_signature": None,
        }
    ]
    assert client.calls == []


async def test_h5_age_verification_gate_rejects_unscoped_confirm_class() -> None:
    """孤立的同名 class 不能获得年龄确认执行能力。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-1": b'<html><body><div class="btn-confirm"></div></body></html>',
                    "shot-1": PNG_BYTES,
                }
            ),
            chat_client=client,
            skill_text="固定页面识别流程",
        )

    assert result["page_type"] == "home"
    assert result["page_state"] != "age_verification_blocking"
    assert len(client.calls) == 1


async def test_inspectable_dynamic_capture_runs_model_after_settle_timeout() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={
                "pipeline_metrics": {
                    "page_settle_complete": False,
                    "page_inspection_ready": True,
                    "page_settle_diagnostics": {
                        "outcome": "inspectable_dynamic",
                        "dynamic_reasons": ["dom_continues_changing"],
                    },
                }
            },
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b"<html><body><main>product list<div class='carousel-enter-active'>"
                b"rotation</div></main></body></html>"
            ),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("content_list"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="fixed page detection flow",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "content_list"
    assert result["page_state"] == "stable"
    assert "dynamic_page_inspection_fallback" in result["risk_flags"]
    assert len(client.calls) == 1


async def test_completed_line_shell_is_refined_to_closable_startup_ad() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={
                "pipeline_metrics": {
                    "page_settle_complete": True,
                    "page_inspection_ready": True,
                }
            },
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b'<html><body><div class="loading-page"><div class="start-up">'
                b'<span class="down-time">Close</span></div>'
                b'<div class="loadingText">loading finished</div></div>'
                b'<main class="home-page">Home content</main></body></html>'
            ),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="fixed page detection flow",
        )

    assert result["page_type"] == "startup_ad"
    assert result["page_state"] == "countdown"
    assert result["rule_matching_allowed"] is True
    assert [item["target"] for item in result["candidate_actions"]] == ["跳过"]


async def test_unsettled_line_loading_shell_uses_deterministic_result_without_model() -> None:
    dom = _artifact("dom-loading", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-loading", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={"pipeline_metrics": {"page_settle_complete": False}},
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-loading": b'<html><body><div class="loading-page">loading</div></body></html>',
            "shot-loading": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="fixed page detection flow",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "line_loading"
    assert result["rule_matching_allowed"] is False
    assert result["candidate_actions"] == []
    assert client.calls == []
    assert "deterministic_h5_profile_before_stability_gate" in result["risk_flags"]
    assert "deterministic_h5_line_loading_page_type_refinement" in result["risk_flags"]
    assert result["metrics"]["h5_profile_shell"] == "line_loading"


async def test_unsettled_startup_ad_shell_keeps_rule_matching_and_close_action() -> None:
    dom = _artifact("dom-startup-ad", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-startup-ad", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={
                "pipeline_metrics": {
                    "page_settle_complete": False,
                    "page_inspection_ready": True,
                }
            },
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-startup-ad": (
                '<html><body><div class="loading-page"><div class="start-up">'
                '<span class="down-time">8s</span></div>'
                '<div class="loadingText">线路读取完成，请稍后</div></div>'
                '<main class="home-page">推荐</main></body></html>'
            ).encode(),
            "shot-startup-ad": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="fixed page detection flow",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "startup_ad"
    assert result["page_state"] == "countdown"
    assert result["rule_matching_allowed"] is True
    assert [item["target"] for item in result["candidate_actions"]] == ["跳过"]
    assert client.calls == []
    assert "deterministic_h5_profile_before_stability_gate" in result["risk_flags"]
    assert "deterministic_startup_ad_page_type_refinement" in result["risk_flags"]
    assert result["metrics"]["h5_profile_shell"] == "startup_ad"


async def test_unsettled_popup_startup_ad_projects_trusted_dismissal_capability() -> None:
    """未稳定的真实开屏结构也必须签发关闭能力，不能退化为裸文本动作。"""
    dom = _artifact(
        "dom-unsettled-popup-startup-ad",
        "dom",
        content_type="text/html; charset=utf-8",
    )
    screenshot = _artifact(
        "shot-unsettled-popup-startup-ad",
        "screenshot",
        content_type="image/png",
    )
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={
                "pipeline_metrics": {
                    "page_settle_complete": False,
                    "page_inspection_ready": True,
                }
            },
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-unsettled-popup-startup-ad": (
                '<html><body><div class="startup-gate-mask"></div>'
                '<div role="dialog" class="van-popup van-popup--center" '
                'style="z-index: 2002">'
                '<div class="van-swipe">广告素材</div>'
                '<div class="skip-wrap"><div>0s(跳过)</div></div>'
                "</div></body></html>"
            ).encode(),
            "shot-unsettled-popup-startup-ad": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="fixed page detection flow",
        )

    assert result["page_type"] == "startup_ad"
    assert result["page_state"] == "countdown"
    assert client.calls == []
    action = result["candidate_actions"][0]
    assert action["obstruction_capability"] == "startup_ad_dismissal"
    assert action["target_keywords"] == [
        "startup_ad",
        "close_control",
        "startup_ad_dismissal",
    ]
    assert action["obstruction_signature"].startswith("h5-obstruction:")


async def test_unsettled_business_dom_uses_model_with_degraded_stability_flag() -> None:
    dom = _artifact("dom-business", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-business", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            capture_metadata={"pipeline_metrics": {"page_settle_complete": False}},
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-business": (
                b'<html><body><main class="home-page">'
                b'<div class="van-tabbar">'
                b'<div class="van-tabbar-item van-tabbar-item--active">Home</div>'
                b'<div class="van-tabbar-item">Video</div>'
                b'<div class="van-tabbar-item">Profile</div>'
                b"</div></main></body></html>"
            ),
            "shot-business": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="fixed page detection flow",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["rule_matching_allowed"] is True
    assert len(client.calls) == 1
    assert "deterministic_h5_profile_before_stability_gate" in result["risk_flags"]
    assert "deterministic_h5_stability_degraded" in result["risk_flags"]


def test_multimodal_messages_contain_skill_catalog_all_dom_chunks_and_real_images() -> None:
    messages = _build_multimodal_messages(
        skill_text="# page-detection\n唯一识别规范",
        catalog_payload={"version": "v-test", "page_types": [{"code": "home"}]},
        page_capture_id="capture-1",
        page_url="https://example.com/home",
        page_title="首页",
        dom_chunks=["DOM-CHUNK-FIRST", "DOM-CHUNK-LAST"],
        image_payloads=[("image-1", PNG_BYTES, "image/png"), ("image-2", PNG_BYTES, "image/png")],
        ocr_chunks=["OCR-CHUNK"],
    )

    assert [message.role for message in messages] == ["system", "user"]
    system_text = "\n".join(
        content.text for content in messages[0].contents if content.type == "text"
    )
    assert "# page-detection" in system_text
    assert '"code": "home"' in system_text
    assert "DOM-CHUNK-FIRST" not in system_text
    assert "OCR-CHUNK" not in system_text
    user_text = "\n".join(
        content.text for content in messages[1].contents if content.type == "text"
    )
    assert "DOM-CHUNK-FIRST" in user_text
    assert "DOM-CHUNK-LAST" in user_text
    assert "OCR-CHUNK" in user_text
    assert "唯一识别规范" not in user_text
    assert '"code": "home"' not in user_text
    images = [content for content in messages[1].contents if content.type == "data"]
    assert len(images) == 2
    assert all(isinstance(content, Content) for content in images)
    assert [content.media_type for content in images] == ["image/png", "image/png"]
    assert all(content.uri.startswith("data:image/png;base64,") for content in images)


def test_timed_capture_sequence_uses_representative_real_image_frames() -> None:
    payload = json.dumps(
        {
            "sequence": True,
            "interval_ms": 250,
            "frames": [
                {
                    "index": 0,
                    "encoding": "base64",
                    "data": base64.b64encode(PNG_BYTES + b"frame-0").decode(),
                },
                {
                    "index": 1,
                    "encoding": "base64",
                    "data": base64.b64encode(PNG_BYTES + b"frame-1").decode(),
                },
            ],
        }
    ).encode()
    artifact = _artifact("sequence", "screenshot", content_type="application/json")

    frames = _decode_screenshot_artifact(artifact, payload)

    assert frames == [
        ("sequence#frame-0", PNG_BYTES + b"frame-0", "image/png"),
        ("sequence#frame-1", PNG_BYTES + b"frame-1", "image/png"),
    ]


def test_long_timed_capture_sequence_keeps_first_middle_and_last_frame() -> None:
    payload = json.dumps(
        {
            "sequence": True,
            "frames": [
                {
                    "index": index,
                    "encoding": "base64",
                    "data": base64.b64encode(PNG_BYTES + f"frame-{index}".encode()).decode(),
                }
                for index in range(8)
            ],
        }
    ).encode()
    artifact = _artifact("sequence", "screenshot", content_type="application/json")

    frames = _decode_screenshot_artifact(artifact, payload)

    assert [item[0] for item in frames] == [
        "sequence#frame-0",
        "sequence#frame-4",
        "sequence#frame-7",
    ]


async def test_detection_reads_current_task_evidence_and_calls_structured_multimodal_model() -> (
    None
):
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {"dom-1": b"<html><main>FIRST HOME LAST</main></html>", "shot-1": PNG_BYTES}
    )
    client = FakeChatClient(_recognized_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="# page-detection\n测试规范",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert loader.calls == [
        (
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        )
    ]
    assert reader.calls == ["dom-1", "shot-1"]
    assert len(client.calls) == 1
    assert client.calls[0]["options"] == {"response_format": PageDetectionModelOutput}
    user_contents = client.calls[0]["messages"][1].contents
    assert any(
        content.type == "text" and "FIRST HOME LAST" in content.text for content in user_contents
    )
    assert any(
        content.type == "data" and content.media_type == "image/png" for content in user_contents
    )
    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["rule_matching_allowed"] is True
    assert result["evidence_artifact_ids"] == ["dom-1", "shot-1"]
    assert result["image_count"] == 1
    assert result["model"] == "vision-test"
    encoded = json.dumps(result, ensure_ascii=False)
    assert "FIRST HOME LAST" not in encoded
    assert "data:image/png;base64" not in encoded


async def test_line_loading_detection_does_not_allow_rule_matching() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": "<html><body>正在读取最快路线</body></html>".encode(),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("line_loading"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "line_loading"
    assert result["rule_matching_allowed"] is False
    assert result["candidate_actions"] == []


async def test_h5_local_loading_preserves_model_home_type() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><main class="home-page">推荐</main>'
                '<div class="loading-page">'
                '<div class="loadingText">正在获取最佳线路</div>'
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("home"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["rule_matching_allowed"] is True
    assert result["candidate_actions"] == []
    assert result["metrics"]["h5_profile_shell"] == "stable"
    assert "deterministic_h5_line_loading_page_type_refinement" not in result["risk_flags"]


async def test_authentication_required_page_skips_rule_matching() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="login-dialog" role="dialog">'
                "请先登录后继续</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("profile").model_copy(update={"page_state": "login_required"})

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "profile"
    assert result["rule_matching_allowed"] is False
    assert "authentication_required" in result["risk_flags"]


async def test_hidden_stale_login_modal_does_not_block_visible_stable_tabbar_page() -> None:
    dom = _artifact("dom-stale-login", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-stale-login", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/movie"))
    reader = FakeArtifactReader(
        {
            "dom-stale-login": (
                '<html><body><main class="video-list">影片内容</main>'
                '<div class="qr-dialog" role="dialog" aria-hidden="true">'
                '请先登录后继续<input type="password"></div>'
                '<div class="van-tabbar">'
                '<div class="van-tabbar-item">热门</div>'
                '<div class="van-tabbar-item van-tabbar-item--active">影片</div>'
                '<div class="van-tabbar-item">社区</div>'
                "</div></body></html>"
            ).encode(),
            "shot-stale-login": PNG_BYTES,
        }
    )
    output = _recognized_output("startup_ad").model_copy(
        update={
            "page_state": "login_required",
            "reason": "截图时序中残留二维码登录弹窗。",
            "risk_flags": ["authentication_required"],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "video_list"
    assert result["page_state"] == "stable"
    assert result["rule_matching_allowed"] is True
    assert "authentication_required" not in result["risk_flags"]
    assert "deterministic_stale_authentication_marker_refinement" in result["risk_flags"]
    assert [item["target"] for item in result["candidate_actions"]] == [
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(1):visible",
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(3):visible",
    ]


async def test_visible_login_dialog_overrides_generic_popup_state() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="van-dialog" role="dialog">'
                "登录后查看<button>立即登录</button></div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("content_detail").model_copy(
        update={
            "page_state": "popup",
            "risk_flags": [
                "authentication_available_but_not_required",
                "authentication_required",
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["rule_matching_allowed"] is False
    assert "authentication_required" in result["risk_flags"]


async def test_authentication_not_required_does_not_skip_ad_popup() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><main class="home-page">热门推荐</main>'
                '<div class="van-dialog" role="dialog">'
                '<div class="ad-grid">广告推荐</div>'
                '<button class="app-btn">我知道了</button>'
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "risk_flags": ["popup_ad", "authentication_not_required"],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["rule_matching_allowed"] is True
    assert "authentication_not_required" in result["risk_flags"]
    assert "authentication_required" not in result["risk_flags"]
    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_acknowledgement")


async def test_guest_profile_optional_login_marker_overrides_contradictory_required_flag() -> None:
    """游客个人中心可展示登录入口，但不能因此被误判成登录墙。"""

    dom = _artifact("dom-guest-profile", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-guest-profile", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-guest-profile": (
                '<html><body><main class="profile-page">'
                '<button class="profile-login-entry">立即登录</button>'
                "<section>我的收藏 我的缓存 我的发布 签到福利</section>"
                '<nav class="business-tabbar">首页 视频 海角 吃瓜</nav>'
                "</main></body></html>"
            ).encode(),
            "shot-guest-profile": PNG_BYTES,
        }
    )
    output = _recognized_output("profile").model_copy(
        update={
            "page_state": "stable_guest_profile",
            "risk_flags": [
                "authentication_available_but_not_required",
                "authentication_required",
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "profile"
    assert result["page_state"] == "stable"
    assert result["rule_matching_allowed"] is True
    assert "authentication_available_but_not_required" in result["risk_flags"]
    assert "authentication_required" not in result["risk_flags"]
    assert (
        page_detection_tools._canonical_business_page_state(
            page_type="home",
            page_state="stable_guest_profile",
        )
        == "stable_guest_profile"
    )


async def test_popup_state_adds_low_risk_close_candidate_when_model_omits_it() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": "<html><body><button>我知道了</button></body></html>".encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(update={"page_state": "popup"})

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert len(result["candidate_actions"]) == 1
    _assert_semantic_popup_action(result["candidate_actions"][0], "visible_text_acknowledgement")


def test_flutter_marketing_announcement_projects_signed_confirm_capability() -> None:
    dom = (
        '<flt-semantics id="notice" style="width:317px;height:260px">'
        '<flt-semantics role="text">TIKTOK重磅来袭，精彩等你解锁</flt-semantics>'
        '<flt-semantics role="text">官方推荐 Telegram 交流群 品牌推广</flt-semantics>'
        '<flt-semantics id="confirm" flt-tappable="" role="text">确认</flt-semantics>'
        "</flt-semantics>"
    )

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    _assert_semantic_popup_action(
        actions[0],
        "flutter_marketing_announcement_confirm",
    )


async def test_visible_home_sticky_banner_is_closed_before_tabbar_exploration() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="home-suspend">'
                '<div class="b-item"><div class="get-btn">进入</div></div>'
                '<div class="btn-close"><i class="van-icon van-icon-close"></i></div>'
                '</div><div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">热门</div>'
                '<div class="van-tabbar-item">影片</div>'
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output("home").model_copy(update={"page_state": "stable"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert len(result["candidate_actions"]) == 1
    _assert_semantic_popup_action(result["candidate_actions"][0], "home_suspend_close")
    assert result["candidate_actions"][0]["purpose"] == (
        "关闭当前遮挡弹窗并在同一浏览器会话重新采集"
    )


def test_nonblocking_home_sticky_banner_does_not_create_dismissal_candidate() -> None:
    dom = (
        '<div class="home-suspend">'
        '<div class="b-item"><div class="get-btn">进入</div></div>'
        '<div class="btn-close"><i class="van-icon van-icon-close"></i></div>'
        "</div>"
    )
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "tag": "div",
                "class_tokens": ["home-suspend"],
                "rect": {"x": 318, "y": 690, "width": 62, "height": 104},
                "style": {"display": "block", "visibility": "visible", "opacity": "1"},
                "hit_test": {
                    "center_hits_target": False,
                    "pointer_interception": False,
                },
            }
        ],
    }

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert actions == []


async def test_center_text_popup_close_is_signed_before_underlying_tabbar() -> None:
    """xv 式居中公告层存在时只签发 closeBtn，不得重生被遮挡底栏候选。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    popup_dom = (
        '<html><body><main class="home">content</main>'
        '<div><div class="van-overlay" style="z-index:2001"></div>'
        '<div class="van-popup van-popup--center" style="z-index:2002">'
        '<div class="textPopContent"><div class="textPopAdBg">公告</div>'
        '<div class="closeBtn"><i class="van-icon van-icon-close"></i></div>'
        "</div></div></div>"
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">推荐</div>'
        '<div class="van-tabbar-item">视频</div>'
        '<div class="van-tabbar-item">特权</div>'
        '<div class="van-tabbar-item">我的</div>'
        "</div></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": popup_dom, "shot-1": PNG_BYTES}),
            chat_client=FakeChatClient(
                _recognized_output("home").model_copy(update={"page_state": "stable"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert len(result["candidate_actions"]) == 1
    _assert_semantic_popup_action(result["candidate_actions"][0], "overlay_ad_close")


async def test_center_image_popup_close_is_signed_before_underlying_tabbar() -> None:
    """H5 首页中心图片广告的通用 close 应作为可信弹窗关闭目标。"""
    dom = _artifact("dom-center-image-popup", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-center-image-popup", "screenshot", content_type="image/png")
    popup_dom = (
        '<html><body><main class="home">content</main>'
        '<div class="van-popup van-popup--center" style="z-index:3002">'
        '<div class="imgWrapper"><img class="ad-img" src="/ad.png">'
        '<div class="close"><i class="van-icon van-icon-close"></i></div>'
        "</div></div>"
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
        '<div class="van-tabbar-item">AI科技</div>'
        '<div class="van-tabbar-item">我的</div>'
        "</div></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-center-image-popup": popup_dom,
                    "shot-center-image-popup": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(
                _recognized_output("home").model_copy(update={"page_state": "stable"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert len(result["candidate_actions"]) == 1
    _assert_semantic_popup_action(result["candidate_actions"][0], "overlay_ad_close")


def test_visible_dialog_is_closed_before_underlying_home_sticky_banner() -> None:
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div class="home-suspend">'
            '<div class="btn-close"><i class="van-icon-close"></i></div></div>'
            '<div class="van-overlay"></div><div role="dialog" '
            'class="van-dialog"><div class="van-dialog__content">'
            '<div class="grid-main">广告</div><div class="btn"><img></div>'
            "</div></div>"
            "</body></html>"
        ],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_content_close")


def test_overlay_rooted_ad_popup_without_dialog_markers_yields_dismissal() -> None:
    # 实测 91JAV 首页：广告弹窗挂在 van-overlay 下且不带 role=dialog / van-dialog，
    # dialog 子树解析对它完全不可见，导致零关闭候选 + 遮挡感知可见文本归零。
    dom = (
        '<html><body><div id="popup-window" class="">'
        '<div class="van-overlay"><div id="advertise-1" class="advertise-box">'
        '<div class="adaptation popup-form">'
        '<div class="close-img on-popup-close"><img class="close-image" src="/a.png"></div>'
        '<div class="popup-img"><a href="https://ad.example" class="popup-ad-link">'
        '<img class="zximg"></a></div>'
        "</div></div></div></div></body></html>"
    )
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "overlay_ad_close")


def test_persale_popup_overlay_yields_verified_overlay_dismissal() -> None:
    dom = (
        '<html><body><main class="home">'
        '<div class="persalePopup"><div class="van-overlay" style="z-index: 1998;">'
        '<div class="wrapper"><div class="animationImg"><img src="/presale.png"></div>'
        "</div></div></div></main></body></html>"
    )

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_overlay")


def test_homepage_payment_popup_yields_exact_close_dismissal() -> None:
    dom = (
        '<html><body><main class="home">'
        '<div class="homepagePaymentPopup">'
        '<div class="van-overlay" style="z-index: 2001;">'
        '<div class="imgWrapper"><div class="popupContent">'
        '<img src="/payment.png"><div class="countdownTimer">23:56:35</div>'
        '</div><div class="close"></div></div></div></div>'
        "</main></body></html>"
    )

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "homepage_payment_close")


def test_topmost_recharge_dialog_overlay_precedes_underlying_homepage_payment_close() -> None:
    dom = (
        '<html><body><main class="home">'
        '<div class="homepagePaymentPopup">'
        '<div class="van-overlay" style="z-index: 2001;">'
        '<div class="imgWrapper"><div class="popupContent">优惠弹窗</div>'
        '<div class="close"></div></div></div></div>'
        '<div class="recharge-box">'
        '<div class="van-overlay" role="button" tabindex="0" style="z-index: 2002;"></div>'
        '<div role="dialog" tabindex="0" class="van-popup van-popup--bottom recharge-popup" '
        'style="z-index: 2002;"><div>选择支付方式</div></div>'
        "</div></main></body></html>"
    )

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_overlay")


def test_flutter_app_open_popup_yields_exact_semantics_close_dismissal() -> None:
    dom = """
    <flt-semantics-host>
      <flt-semantics flt-tappable role="text"
        style="position:absolute;width:208px;height:31.2px;
        transform:matrix(1,0,0,1,91,713.6);z-index:2">
        91PornAPP内打开
      </flt-semantics>
      <flt-semantics flt-tappable
        style="position:absolute;width:317.2px;height:353.6px;
        transform:matrix(1,0,0,1,36.4,216.6);z-index:2"></flt-semantics>
      <flt-semantics flt-tappable
        style="position:absolute;width:36.4px;height:36.4px;
        transform:matrix(1,0,0,1,176.8,591);z-index:3"></flt-semantics>
    </flt-semantics-host>
    """

    dom_chunks = page_detection_tools._sanitize_and_chunk_dom(dom)

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=dom_chunks,
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "flutter_app_open_popup_close")


def test_flutter_centered_ad_popup_without_app_label_yields_exact_dismissal() -> None:
    dom = """
    <flt-semantics-host>
      <flt-semantics flt-tappable
        style="position:absolute;width:317.2px;height:353.6px;
        transform:matrix(1,0,0,1,36.4,216.6);z-index:2"></flt-semantics>
      <flt-semantics flt-tappable
        style="position:absolute;width:36.4px;height:36.4px;
        transform:matrix(1,0,0,1,176.8,591);z-index:3"></flt-semantics>
    </flt-semantics-host>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="payment",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "flutter_centered_ad_popup_close")


def test_flutter_centered_ad_popup_with_close_above_yields_exact_dismissal() -> None:
    """关闭节点位于广告上方时也必须签发独立的可信清障能力。"""
    dom = """
    <flt-semantics-host>
      <flt-semantics flt-tappable
        style="position:absolute;width:33.8px;height:33.8px;
        transform:matrix(1,0,0,1,178.1,269.9);z-index:2"></flt-semantics>
      <flt-semantics flt-tappable
        style="position:absolute;width:260px;height:260px;
        transform:matrix(1,0,0,1,65,314.1);z-index:3"></flt-semantics>
    </flt-semantics-host>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(
        actions[0],
        "flutter_centered_ad_popup_close_above",
    )


def test_flutter_centered_scroll_popup_with_close_below_yields_exact_dismissal() -> None:
    """可滚动内容容器不是按钮，仍应凭唯一相邻几何关系签发独立能力。"""
    dom = """
    <flt-semantics-host>
      <flt-semantics
        style="position:absolute;touch-action:none;overflow-y:scroll;
        width:317.2px;height:374.4px;
        transform:matrix(1,0,0,1,36.4,212.7);pointer-events:none;z-index:2">
        <flt-semantics flt-tappable role="text"
          style="position:absolute;width:67.6px;height:90.1px;pointer-events:all">
          内容入口
        </flt-semantics>
      </flt-semantics>
      <flt-semantics flt-tappable
        style="position:absolute;width:33.8px;height:33.8px;
        transform:matrix(1,0,0,1,178.1,607.9);pointer-events:all;z-index:3">
      </flt-semantics>
    </flt-semantics-host>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="profile",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(
        actions[0],
        "flutter_centered_scroll_popup_close_below",
    )


def test_flutter_nested_scroll_popup_with_outer_sibling_close_yields_dismissal() -> None:
    """滚动区嵌在弹窗外壳内时，应以外壳与同级 X 的绝对几何验签。"""
    dom = """
    <flt-semantics-host>
      <flt-semantics id="popup-shell"
        style="position:absolute;width:356.72px;height:448.24px;
        transform:matrix(1,0,0,1,16.64,168.76);pointer-events:none;z-index:2">
        <flt-semantics-container>
          <flt-semantics id="popup-scroll"
            style="position:absolute;touch-action:none;overflow-y:scroll;
            width:356.72px;height:448.24px;pointer-events:none">
            <flt-semantics flt-tappable role="text"
              style="position:absolute;width:49.92px;height:73.84px;pointer-events:all">
              内容入口
            </flt-semantics>
          </flt-semantics>
        </flt-semantics-container>
      </flt-semantics>
      <flt-semantics id="popup-close" flt-tappable
        style="position:absolute;width:33.28px;height:33.28px;
        transform:matrix(1,0,0,1,178.36,641.96);pointer-events:all;z-index:3">
      </flt-semantics>
    </flt-semantics-host>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(
        actions[0],
        "flutter_centered_scroll_popup_close_below",
    )


def test_flutter_image_popup_with_top_right_close_yields_exact_dismissal() -> None:
    """图片弹窗右上角唯一 X 必须签发独立能力，不能退化为任意空节点。"""
    dom = """
    <flt-semantics-host>
      <flt-semantics role="img"
        style="position:absolute;width:270.4px;height:296.4px;
        transform:matrix(1,0,0,1,59.8,273.8);pointer-events:all;z-index:2">
      </flt-semantics>
      <flt-semantics flt-tappable
        style="position:absolute;width:26px;height:26px;
        transform:matrix(1,0,0,1,288.6,289.4);pointer-events:all;z-index:5">
      </flt-semantics>
    </flt-semantics-host>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(
        actions[0],
        "flutter_image_popup_top_right_close",
    )


def test_flutter_account_notice_top_right_close_yields_exact_dismissal() -> None:
    """账号找回提示只允许关闭右上角 X，不得执行“立即保存”。"""
    dom = """
    <flt-semantics-host>
      <flt-semantics role="group"
        aria-label="重要提示！！！首次安装APP请先保存此信息，可提高账号找回概率。您的邀请码：abc"
        style="position:absolute;width:310px;height:351.72px;
        transform:matrix(1,0,0,1,40,246.14);pointer-events:none">
        <flt-semantics-container>
          <flt-semantics flt-tappable
            style="position:absolute;width:31.2px;height:31.2px;
            transform:matrix(1,0,0,1,252.8,15.6);pointer-events:all;z-index:2">
          </flt-semantics>
          <flt-semantics flt-tappable role="text"
            style="position:absolute;width:258px;height:41.6px;
            transform:matrix(1,0,0,1,26,294.52);pointer-events:all;z-index:1">
            立即保存
          </flt-semantics>
        </flt-semantics-container>
      </flt-semantics>
    </flt-semantics-host>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="profile",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(
        actions[0],
        "flutter_account_notice_top_right_close",
    )


def test_queued_hidden_ad_boxes_do_not_count_as_visible_obstruction() -> None:
    # 轮候广告位用 class="hidden" 收起而非 hidden 属性；若按属性判隐藏，
    # 会把下一条待播广告的关闭控件误当成当前可见遮挡而空点。
    dom = (
        '<html><body><div id="popup-window" class="">'
        '<div class="van-overlay">'
        '<div id="advertise-2" class="advertise-box hidden">'
        '<div class="close-img on-popup-close"><img class="close-image"></div>'
        "</div>"
        '<div id="advertise-3" class="advertise-box hidden">'
        '<div class="close-img on-popup-close"><img class="close-image"></div>'
        "</div></div></div></body></html>"
    )

    assert page_detection_tools._structural_obstruction_selector(dom) is None


def test_adspop_image_ad_gets_structural_overlay_close_capability() -> None:
    """首页广告须由广告容器、外链图片和关闭控件共同证明，不能保留绝对路径。"""
    dom = (
        '<div class="adspop"><div class="popup-container"><div class="popup-content">'
        '<div class="popup-picture"><img data-uri="https://ads.example/landing"></div>'
        '<div class="popup-close"><img src="/assets/close.png" alt=""></div>'
        "</div></div></div>"
    )

    selector = page_detection_tools._structural_obstruction_selector(dom)
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert selector == page_detection_tools._OVERLAY_AD_CLOSE_SELECTOR
    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "overlay_ad_close")


def test_recommend_layer_image_ad_gets_structural_overlay_close_capability() -> None:
    """无 dialog 标记的推荐广告层也只能凭完整容器结构签发关闭能力。"""
    dom = (
        '<div class="recommend-layer"><div class="recommend-wrapper">'
        '<div class="recommend-card"><div class="media-box">'
        '<img data-uri="https://ads.example/landing" data-event="ad_click">'
        '</div><div class="close-btn"><img src="/assets/ads-close.png"></div>'
        "</div></div></div>"
    )

    selector = page_detection_tools._structural_obstruction_selector(dom)
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert selector == page_detection_tools._OVERLAY_AD_CLOSE_SELECTOR
    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "overlay_ad_close")


def test_inline_recommend_card_close_is_not_structurally_authorized() -> None:
    """普通推荐卡片即使带 close-btn，也不能获得弹窗关闭能力。"""
    dom = (
        '<main><div class="recommend-card"><div class="media-box">正文推荐</div>'
        '<div class="close-btn">×</div></div></main>'
    )

    assert page_detection_tools._structural_obstruction_selector(dom) is None


def test_recommend_layer_with_extra_wrapper_is_not_structurally_authorized() -> None:
    """检测层不得签发执行选择器无法命中的非直接父子结构。"""
    dom = (
        '<div class="recommend-layer"><div class="recommend-wrapper">'
        '<div class="unexpected-wrapper"><div class="recommend-card">'
        '<div class="close-btn">×</div></div></div></div></div>'
    )

    assert page_detection_tools._structural_obstruction_selector(dom) is None


@pytest.mark.parametrize(
    "close_markup",
    [
        '<a class="close-btn" href="https://ads.example/landing">×</a>',
        '<div class="close-btn" data-url="https://ads.example/landing">×</div>',
        (
            '<div class="close-btn"><a href="https://ads.example/landing">'
            "<img></a></div>"
        ),
    ],
)
def test_navigable_recommend_layer_close_is_not_structurally_authorized(
    close_markup: str,
) -> None:
    dom = (
        '<div class="recommend-layer"><div class="recommend-wrapper">'
        f'<div class="recommend-card">{close_markup}</div></div></div>'
    )

    assert page_detection_tools._structural_obstruction_selector(dom) is None


def test_hidden_or_ambiguous_recommend_layer_close_is_not_authorized() -> None:
    hidden = (
        '<div class="recommend-layer" style="display:none">'
        '<div class="recommend-wrapper"><div class="recommend-card">'
        '<div class="close-btn">×</div></div></div></div>'
    )
    ambiguous = (
        '<div class="recommend-layer"><div class="recommend-wrapper">'
        '<div class="recommend-card"><div class="close-btn">×</div>'
        '<div class="close-btn">×</div></div></div></div>'
    )

    assert page_detection_tools._structural_obstruction_selector(hidden) is None
    assert page_detection_tools._structural_obstruction_selector(ambiguous) is None


def test_navigable_recommend_close_inside_van_overlay_is_not_authorized() -> None:
    """推荐关闭分支不得被外层 van-overlay 的通用快路重新签发。"""
    dom = (
        '<div class="van-overlay"><div class="recommend-layer">'
        '<div class="recommend-wrapper"><div class="recommend-card">'
        '<div class="close-btn"><a class="btn-close" '
        'href="https://ads.example/landing">关闭</a></div>'
        "</div></div></div></div>"
    )

    assert page_detection_tools._structural_obstruction_selector(dom) is None


def test_popup_close_outside_adspop_is_not_structurally_authorized() -> None:
    """正文卡片或支付区的同名关闭类不得继承首页广告关闭权限。"""
    dom = (
        '<main><a href="https://pay.example/checkout">立即支付</a>'
        '<div class="popup-content"><div class="popup-close">×</div></div></main>'
    )

    assert page_detection_tools._structural_obstruction_selector(dom) is None


def test_real_dialog_still_wins_over_overlay_ad_close() -> None:
    dom = (
        '<html><body><div id="popup-window">'
        '<div class="van-overlay">'
        '<div class="close-img on-popup-close"><img class="close-image"></div>'
        "</div>"
        '<div role="dialog" class="van-dialog"><div class="van-dialog__content">'
        '<div class="grid-main">广告</div><div class="btn"><img></div>'
        "</div></div></div></body></html>"
    )

    assert page_detection_tools._structural_obstruction_selector(dom) == (
        ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
        "> .btn:visible > img:visible, "
        ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
        "> :is(.btn-box.end, .btn-box.close):visible"
    )


def test_structural_popup_candidate_is_semantic_and_has_stable_trusted_signature() -> None:
    dialog = (
        '<div class="van-overlay"></div><div role="dialog" class="van-dialog">'
        '<div class="van-dialog__content"><div class="grid-main">广告</div>'
        '<div class="btn"><img src="data:image/png;base64,AAAA"></div>'
        "</div></div>"
    )
    first = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[f'<html><body><div class="carousel">第一页</div>{dialog}</body></html>'],
    )
    second = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[f'<html><body><div class="carousel">第二页</div>{dialog}</body></html>'],
    )
    selector = page_detection_tools._structural_obstruction_selector(dialog)

    assert len(first) == 1
    assert selector == (
        ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
        "> .btn:visible > img:visible, "
        ':is([role="dialog"], .van-dialog):visible > .van-dialog__content '
        "> :is(.btn-box.end, .btn-box.close):visible"
    )
    assert first[0].target == "关闭当前弹窗"
    assert first[0].navigation_depth == 0
    assert first[0].target_keywords == [
        "popup_dismissal",
        "close_control",
        "dialog_content_close",
    ]
    assert first[0].obstruction_capability == "dialog_content_close"
    assert first[0].obstruction_signature.startswith("h5-obstruction:")
    assert first[0].obstruction_signature == second[0].obstruction_signature
    assert not any(
        marker in first[0].target for marker in ("css=", ":visible", ".van-dialog", "[role=")
    )


def test_trusted_dialog_close_is_not_promoted_by_download_text_in_popup_purpose() -> None:
    """“下载”描述弹窗内容时，不得把可信的 X 关闭控件误判为下载动作。"""
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_list",
        page_state="popup",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target=".btn",
                target_keywords=["btn", "关闭", "X"],
                risk_level="low",
                purpose="关闭官方推荐下载广告弹窗",
                expected_next_page_type="video_list",
            )
        ],
        dom_chunks=[
            '<html><body><div class="van-overlay"></div>'
            '<div role="dialog" class="van-dialog">'
            '<div class="van-dialog__content"><div class="grid-main">官方推荐下载</div>'
            '<div class="btn"><img alt="X"></div></div></div></body></html>'
        ],
    )

    actions = page_detection_tools._with_deterministic_high_risk_actions(actions)

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_content_close")
    assert actions[0].risk_level == "low"
    assert actions[0].purpose == "关闭当前遮挡弹窗并在同一浏览器会话重新采集"
    assert actions[0].expected_next_page_type == "video_list"


def test_visible_active_ad_close_image_is_used_after_first_dialog_closes() -> None:
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div class="van-overlay"></div>'
            '<div role="dialog" class="van-dialog">'
            '<div class="van-dialog__content"><div class="ad-list">'
            '<div class="ad-item"><div class="b-item"><img></div>'
            '<div class="btn-box"><span class="btn-close">'
            '<img class="b-img active"></span></div></div>'
            '<div class="ad-item" style="display:none">'
            '<div class="btn-box"><img class="b-img"></div></div>'
            "</div></div></div></body></html>"
        ],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_active_ad_close")


def test_visible_text_ad_close_button_is_used_before_overlay() -> None:
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_detail",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div class="van-overlay"></div>'
            '<div role="dialog" class="van-dialog">'
            '<div class="van-dialog__content"><div class="ad-list">'
            '<div class="banner-item"><img></div></div>'
            '<div class="btn-box end"> 关闭 </div>'
            "</div></div></body></html>"
        ],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_text_ad_close")


def test_stable_video_detail_discards_model_close_without_obstruction_proof() -> None:
    """播放器内联广告关闭不应冒充业务弹窗候选，阻断详情返回。"""
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="关闭",
                target_keywords=["关闭"],
                risk_level="low",
                purpose="关闭播放器区域广告遮罩",
                expected_next_page_type="video_detail",
            )
        ],
        dom_chunks=[
            '<html><body><div id="MoviePlayer"></div><div class="movie-ad-box">'
            '<div class="download"><span>关闭</span></div></div></body></html>'
        ],
    )

    assert actions == []


def test_popup_video_detail_discards_inline_close_without_dialog_proof() -> None:
    """模型误报 popup 时，播放器内联文本仍不能获得弹窗关闭授权。"""
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_detail",
        page_state="popup",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="关闭",
                target_keywords=["关闭"],
                risk_level="low",
                purpose="关闭播放器区域广告遮罩",
                expected_next_page_type="video_detail",
            )
        ],
        dom_chunks=[
            '<html><body><div id="MoviePlayer"></div><div class="movie-ad-box">'
            '<div class="download"><span>关闭</span></div></div></body></html>'
        ],
    )

    assert actions == []


def test_video_detail_ignores_low_z_dialog_without_dismissal_control() -> None:
    """详情页低层级容器没有关闭控件时不应阻断浏览器返回。"""
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_detail",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><header style="z-index: 9999">播放器工具栏</header>'
            '<div id="MoviePlayer"></div>'
            '<div class="van-overlay" style="z-index: 499"></div>'
            '<div role="dialog" class="van-dialog" style="z-index: 500">'
            "播放器推荐区域</div></body></html>"
        ],
    )

    assert actions == []


def test_video_detail_keeps_low_z_dialog_state_when_nonblocking_is_unproven() -> None:
    page_state = page_detection_tools._refined_page_state_after_obstruction_check(
        page_type="video_detail",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><header style="z-index: 9999">播放器工具栏</header>'
            '<div id="MoviePlayer"></div>'
            '<div class="van-overlay" style="z-index: 499"></div>'
            '<div role="dialog" class="van-dialog" style="z-index: 500">'
            "播放器推荐区域</div></body></html>"
        ],
    )

    assert page_state == "popup"


def test_hidden_active_ad_close_image_falls_back_to_safe_overlay_click() -> None:
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_list",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div class="van-overlay"></div>'
            '<div role="dialog" class="van-dialog">'
            '<div class="van-dialog__content"><div class="ad-list">'
            '<div class="ad-item" style="display:none">'
            '<div class="btn-box"><span class="btn-close">'
            '<img class="b-img active"></span></div></div>'
            "</div></div></div></body></html>"
        ],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_overlay")


def test_startup_ad_prefers_its_visible_countdown_close_control() -> None:
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target=".van-overlay:visible",
                target_keywords=["close_control"],
                risk_level="low",
                purpose="关闭当前遮挡弹窗",
            )
        ],
        dom_chunks=[
            '<html><body><div class="loading-page"><span class="down-time">关闭</span>'
            '</div><div class="van-overlay"></div><div role="dialog" '
            'class="van-dialog">广告</div></body></html>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == "跳过"
    assert actions[0].expected_next_page_type == "home"
    assert actions[0].purpose == "关闭开屏广告并在同一浏览器会话进入首页"


def test_lower_z_ad_dialog_does_not_preempt_visible_startup_ad() -> None:
    """低于开屏层的广告弹窗不能抢占当前可点击的开屏关闭控件。"""
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div class="loading-page"><div class="start-up">'
            '<span class="down-time">关闭</span></div></div>'
            '<div class="van-overlay" style="z-index: 2002"></div>'
            '<div role="dialog" class="van-dialog" style="z-index: 2003">'
            '<div class="van-dialog__content"><div class="grid-main">广告</div>'
            '<div class="btn"><img alt=""></div></div></div></body></html>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == "跳过"
    assert actions[0].expected_next_page_type == "home"


def test_nonzero_startup_countdown_does_not_become_generic_popup_dismissal() -> None:
    """同层开屏广告尚未倒数到零时只能等待，不能伪装成普通弹窗去点击。"""
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="popup",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="关闭当前弹窗",
                target_keywords=["popup_dismissal", "close_control"],
                risk_level="low",
                purpose="关闭当前遮挡弹窗",
            )
        ],
        dom_chunks=[
            '<html><body><div class="van-overlay" style="z-index: 2999"></div>'
            '<div role="dialog" class="van-popup" style="z-index: 3000">'
            '<div class="skip-wrap"><div>1s(跳过)</div></div>'
            "广告内容</div></body></html>"
        ],
    )

    assert actions == []


def test_nonzero_startup_countdown_keeps_independent_higher_dialog_dismissal() -> None:
    """真正位于开屏层之上的独立对话框仍应先关闭，不被倒计时等待吞掉。"""
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div role="dialog" class="van-popup" '
            'style="z-index: 3000"><div class="skip-wrap"><div>1s(跳过)</div>'
            '</div>广告内容</div><div class="van-overlay" style="z-index: 3002">'
            '</div><div role="dialog" class="van-dialog" style="z-index: 3003">'
            '<div class="van-dialog__header">提示</div>'
            '<div class="van-dialog__message">登录失败，请刷新重试</div>'
            '<button class="van-button van-dialog__cancel">取消</button>'
            '<button class="van-button van-dialog__confirm">确认</button>'
            "</div></body></html>"
        ],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_overlay")


def test_high_z_dialog_is_closed_before_visible_startup_ad_countdown() -> None:
    """晚到的顶层对话框必须先于已被遮挡的开屏关闭控件处理。"""
    dom = (
        '<html><body><div class="loading-page"><span class="down-time">关闭</span>'
        '</div><div class="van-overlay" style="z-index: 3002"></div>'
        '<div role="dialog" class="van-dialog" style="z-index: 3003">'
        '<div class="van-dialog__header">提示</div>'
        '<div class="van-dialog__message">登录失败，请刷新重试</div>'
        '<button class="van-button van-dialog__cancel">取消</button>'
        '<button class="van-button van-dialog__confirm">确认</button>'
        "</div></body></html>"
    )
    dom_chunks = page_detection_tools._sanitize_and_chunk_dom(dom)

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="countdown",
        candidate_actions=[],
        dom_chunks=dom_chunks,
    )
    state = page_detection_tools._deterministic_transient_page_state(
        page_type="startup_ad",
        dom_chunks=dom_chunks,
        computed_page_ready=True,
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_overlay")
    assert state == "popup"


def test_line_loading_login_failure_signs_exact_retry_candidate() -> None:
    """线路壳只允许精确“登录失败 + 重试确认按钮”获得一次恢复能力。"""
    dom = (
        '<html><body><div role="dialog" class="van-popup van-dialog" '
        'style="z-index: 2002"><div class="van-dialog__header">温馨提示</div>'
        '<div class="van-dialog__message">用户登陆失败</div>'
        '<button class="van-button van-dialog__confirm">'
        '<span class="van-button__text">重试</span></button></div></body></html>'
    )
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="line_loading",
        page_state="login_required",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.target == "重试线路页登录"
    assert action.risk_level == "low"
    assert action.navigation_depth == 0
    assert action.obstruction_capability == "line_loading_login_retry"
    assert action.obstruction_signature is not None
    assert {
        "line_loading_login_retry",
        "authentication_retry",
        "evidence:structure",
    }.issubset(action.target_keywords)
    assert (
        page_detection_tools._without_unexecutable_candidate_actions(
            candidate_actions=actions,
            dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
            page_type="line_loading",
        )
        == actions
    )


@pytest.mark.parametrize(
    ("message", "button_class", "button_text"),
    [
        ("网络异常，请重试", "van-dialog__confirm", "重试"),
        ("用户登录失败", "retry-button", "重试"),
        ("用户登录失败", "van-dialog__confirm", "确定"),
    ],
)
def test_line_loading_login_retry_rejects_incomplete_structure(
    message: str,
    button_class: str,
    button_text: str,
) -> None:
    dom = (
        '<html><body><div role="dialog" class="van-dialog">'
        f'<div class="van-dialog__message">{message}</div>'
        f'<button class="{button_class}"><span>{button_text}</span></button>'
        "</div></body></html>"
    )

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="line_loading",
        page_state="login_required",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert actions == []


def test_hidden_startup_ad_countdown_does_not_override_normal_dialog_recovery() -> None:
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div class="loading-page" style="display:none">'
            '<span class="down-time">关闭</span></div><div class="van-overlay"></div>'
            '<div role="dialog" class="van-dialog"><div class="van-dialog__content">'
            '<div class="grid-main">广告</div><div class="btn"><img></div>'
            "</div></div></body></html>"
        ],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "dialog_content_close")


async def test_visible_startup_ad_structure_overrides_underlying_home_popup() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="loading-page"><div class="start-up">'
                '<span class="down-time">关闭</span></div></div>'
                '<main><div class="van-overlay"></div><div role="dialog" '
                'class="van-dialog">首页广告</div><div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">热门</div>'
                '<div class="van-tabbar-item">影片</div></div></main></body></html>'
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    model_output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target=".van-overlay:visible",
                    target_keywords=["close_control"],
                    risk_level="low",
                    purpose="关闭首页弹窗",
                )
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(model_output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "startup_ad"
    assert result["rule_matching_allowed"] is True
    assert [item["target"] for item in result["candidate_actions"]] == ["跳过"]
    assert "deterministic_startup_ad_page_type_refinement" in result["risk_flags"]
    assert "deterministic_active_tab_page_type_refinement" not in result["risk_flags"]


def test_stylesheet_startup_ad_tokens_do_not_refine_home_page_type() -> None:
    page_type = page_detection_tools._refined_page_type_from_startup_ad_structure(
        page_type="home",
        dom_chunks=[
            "<html><head><style>.loading-page .down-time{display:block}</style></head>"
            '<body><div class="van-tabbar">首页</div></body></html>'
        ],
    )

    assert page_type == "home"


def test_candidate_action_without_dom_target_is_filtered() -> None:
    actions = page_detection_tools._without_unexecutable_candidate_actions(
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="点击首个低风险帖子卡片",
                target_keywords=["帖子"],
                risk_level="low",
                purpose="探索帖子详情",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="text=真实帖子",
                target_keywords=["帖子"],
                risk_level="low",
                purpose="探索帖子详情",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="底部导航 '影片'",
                target_keywords=["影片"],
                risk_level="low",
                purpose="探索影片导航",
            ),
        ],
        dom_chunks=["<html><body><article>真实帖子</article><nav>影片</nav></body></html>"],
    )

    assert [action.target for action in actions] == ["text=真实帖子"]


def test_startup_ad_dismissal_semantic_cannot_cross_page_type_boundary() -> None:
    actions = page_detection_tools._without_unexecutable_candidate_actions(
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="跳过",
                target_keywords=["startup_ad", "close_control"],
                risk_level="low",
                purpose="关闭开屏广告并进入首页",
                expected_next_page_type="home",
            )
        ],
        dom_chunks=['<div class="loading-page"><span class="down-time">8s</span></div>'],
        page_type="home",
    )

    assert actions == []


def test_global_launch_skip_is_a_trusted_startup_ad_dismissal() -> None:
    dom = (
        '<html><body><div class="globalLaunch">'
        '<div class="launchSwiperContent"><div class="header-title">'
        "<span>广告</span><span>跳过</span>"
        "</div></div></div></body></html>"
    )

    selector = page_detection_tools._startup_ad_dismissal_selector(
        page_type="startup_ad",
        dom=dom,
    )
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert selector == '.globalLaunch .header-title > span:text-is("跳过"):visible'
    assert [action.target for action in actions] == ["跳过"]


@pytest.mark.parametrize(
    ("dom", "expected_selector"),
    [
        (
            '<div class="wrapper"><div class="countdown">'
            '<div class="jump_over">跳过</div></div></div>',
            '.countdown > .jump_over:text-is("跳过"):visible',
        ),
        (
            '<div role="dialog" class="van-popup"><div class="skip-wrap">'
            "<div>0s(跳过)</div></div></div>",
            '.van-popup:visible .skip-wrap:has(> div:text-is("0s(跳过)")):visible',
        ),
        (
            '<div role="dialog" class="van-popup"><div class="count-down">跳过</div></div>',
            '.van-popup:visible .count-down:text-is("跳过"):visible',
        ),
        (
            '<div class="absolute right-16 top-32">'
            '<div class="flex-center count-down">跳过</div></div>',
            '.count-down:text-is("跳过"):visible',
        ),
        (
            '<div class="launch-page"><div class="screen-ad">'
            '<div class="button"><div>进入</div></div></div></div>',
            '.launch-page:visible .screen-ad > .button:has(> div:text-is("进入")):visible',
        ),
    ],
)
def test_known_startup_ad_exit_variants_are_trusted(
    dom: str,
    expected_selector: str,
) -> None:
    assert (
        page_detection_tools._startup_ad_dismissal_selector(
            page_type="startup_ad",
            dom=dom,
        )
        == expected_selector
    )


def test_global_count_down_requires_one_exact_visible_skip_control() -> None:
    dom = '<div class="count-down">跳过</div><div class="count-down">跳过</div>'

    assert (
        page_detection_tools._startup_ad_dismissal_selector(
            page_type="startup_ad",
            dom=dom,
        )
        is None
    )


def test_launch_entry_replaces_model_exit_with_trusted_startup_dismissal() -> None:
    dom = (
        '<div class="launch-page"><div class="screen-ad">'
        '<div class="button"><div>进入</div></div></div></div>'
    )
    model_exit = page_detection_tools.CandidateAction(
        action="click",
        target="进入",
        target_keywords=["进入"],
        risk_level="low",
        purpose="离开启动广告并进入底层产品首页",
        expected_next_page_type="home",
    )

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="startup_ad",
        page_state="ad_visible",
        candidate_actions=[model_exit],
        dom_chunks=[dom],
    )

    assert [action.target for action in actions] == ["跳过"]
    assert actions[0].target_keywords == [
        "startup_ad",
        "close_control",
        "startup_ad_dismissal",
    ]


def test_flutter_fullscreen_startup_skip_is_a_trusted_dismissal() -> None:
    dom = """
    <flutter-view style="width:390px;height:844px">
      <flt-semantics-host>
        <flt-semantics flt-tappable
          style="position:absolute;width:390px;height:844px;pointer-events:none">
          <flt-semantics-container>
            <flt-semantics flt-tappable role="text"
              style="position:absolute;width:61.7px;height:36.4px;
              transform:matrix(1,0,0,1,312.7,10.4);pointer-events:all;z-index:2">
              跳过
            </flt-semantics>
          </flt-semantics-container>
        </flt-semantics>
      </flt-semantics-host>
    </flutter-view>
    """

    assert page_detection_tools._has_visible_startup_ad_structure([dom]) is True
    assert (
        page_detection_tools._startup_ad_dismissal_selector(
            page_type="startup_ad",
            dom=dom,
        )
        == "internal:flutter_fullscreen_startup_ad_skip"
    )


def test_flutter_fullscreen_bare_number_is_trusted_as_startup_countdown() -> None:
    """纯数字只有位于全屏开屏层右上角时才可作为倒计时证据。"""
    dom = """
    <flutter-view style="width:390px;height:844px">
      <flt-semantics-host>
        <flt-semantics flt-tappable
          style="position:absolute;width:390px;height:844px;pointer-events:none">
          <flt-semantics-container>
            <flt-semantics flt-tappable role="text"
              style="position:absolute;width:40px;height:36px;
              transform:matrix(1,0,0,1,334,10);pointer-events:all;z-index:2">
              3
            </flt-semantics>
          </flt-semantics-container>
        </flt-semantics>
      </flt-semantics-host>
    </flutter-view>
    """

    assert page_detection_tools._has_visible_startup_ad_structure([dom]) is True
    assert (
        page_detection_tools._startup_ad_dismissal_selector(
            page_type="startup_ad",
            dom=dom,
        )
        is None
    )


@pytest.mark.parametrize(
    ("parent_width", "parent_height", "x", "y"),
    [
        (390, 120, 312.7, 10.4),
        (390, 844, 120, 10.4),
        (390, 844, 312.7, 240),
    ],
)
def test_flutter_skip_text_without_fullscreen_top_right_structure_is_not_trusted(
    parent_width: int,
    parent_height: int,
    x: float,
    y: float,
) -> None:
    dom = f"""
    <flt-semantics-host>
      <flt-semantics flt-tappable
        style="position:absolute;width:{parent_width}px;height:{parent_height}px">
        <flt-semantics flt-tappable role="text"
          style="position:absolute;width:61.7px;height:36.4px;
          transform:matrix(1,0,0,1,{x},{y});z-index:2">跳过</flt-semantics>
      </flt-semantics>
    </flt-semantics-host>
    """

    assert page_detection_tools._has_visible_startup_ad_structure([dom]) is False


def test_visual_bounds_rejects_inverted_box() -> None:
    with pytest.raises(ValueError, match="视觉矩形边界必须严格递增"):
        page_detection_tools.VisualBounds(
            screenshot_index=1,
            left_ratio=0.8,
            top_ratio=0.2,
            right_ratio=0.3,
            bottom_ratio=0.4,
            confidence=0.9,
        )


def test_profile_credential_screenshot_popup_signs_dedicated_close_capability() -> None:
    dom = """
    <div class="van-overlay" style="z-index: 2000"></div>
    <div class="van-popup van-popup--center mine-pop" style="z-index: 2001">
      <div class="account-cert-popup">
        <div class="btn">请截图保存</div>
      </div>
    </div>
    """
    snapshot = {
        "truncated": True,
        "element_limit_reached": True,
        "elements": [
            {
                "tag": "div",
                "selector": ".mine-pop .account-cert-popup .btn",
                "text": "请截图保存",
                "class_tokens": ["btn"],
                "ancestor_class_tokens": [
                    "account-cert-popup",
                    "van-popup",
                    "van-popup--center",
                    "mine-pop",
                ],
                "rect": {"x": 59.0, "y": 634.0, "width": 271.0, "height": 41.0},
                "hit_test": {
                    "center_hits_target": True,
                    "topmost": True,
                },
            }
        ],
    }

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="profile",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].target == "关闭当前弹窗"
    assert actions[0].obstruction_capability == "profile_credential_screenshot_confirm"
    assert actions[0].obstruction_signature is not None


@pytest.mark.parametrize("confirm_text", ["立即保存", "保存"])
def test_profile_credential_save_popup_signs_dedicated_close_capability(
    confirm_text: str,
) -> None:
    """个人中心活动阻断弹窗可按唯一保存按钮签发关闭能力。"""
    dom = f"""
    <div class="van-overlay" role="button" style="z-index: 2000"></div>
    <div class="van-popup van-popup--round van-popup--center code-popup"
         role="dialog" style="z-index: 2001">
      <div class="content">当前内容可稍后查看，请先完成本地保存</div>
      <div class="btn">{confirm_text}</div>
    </div>
    """
    snapshot = {
        "elements": [
            {
                "tag": "div",
                "role": "dialog",
                "selector": ".van-popup.code-popup",
                "text": f"当前内容可稍后查看，请先完成本地保存 {confirm_text}",
                "class_tokens": [
                    "van-popup",
                    "van-popup--round",
                    "van-popup--center",
                    "code-popup",
                ],
                "ancestor_class_tokens": ["main_wrapper"],
                "rect": {"x": 39, "y": 166, "width": 312, "height": 512},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
            {
                "tag": "div",
                "selector": ".van-popup.code-popup .btn",
                "text": confirm_text,
                "class_tokens": ["btn"],
                "ancestor_class_tokens": [
                    "code-popup",
                    "van-popup",
                    "van-popup--round",
                    "van-popup--center",
                ],
                "rect": {"x": 97, "y": 629, "width": 196, "height": 44},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
        ]
    }

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="profile",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].target == "关闭当前弹窗"
    assert actions[0].obstruction_capability == "profile_credential_screenshot_confirm"
    assert actions[0].obstruction_signature is not None


def test_profile_promote_popup_signs_unique_immediate_save_button() -> None:
    """真实 promoteBtn 双按钮弹窗只签发“立即保存”，不把“我已保存”计为歧义。"""
    dom = """
    <div class="van-overlay" role="button" style="z-index: 2000"></div>
    <div class="van-popup van-popup--center" role="dialog" style="z-index: 2001">
      <div>账号凭证提示，可通过官方客服找回账号</div>
      <div class="promoteBtnBox">
        <div class="promoteBtn btn1">我已保存</div>
        <div class="promoteBtn btn2">立即保存</div>
      </div>
    </div>
    """
    snapshot = {
        "elements": [
            {
                "tag": "div",
                "role": "dialog",
                "selector": "#appView > div > div:nth-of-type(3)",
                "text": "账号凭证提示，可通过官方客服找回账号 我已保存 立即保存",
                "class_tokens": ["van-popup", "van-popup--center"],
                "rect": {"x": 41, "y": 140, "width": 308, "height": 564},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
            {
                "tag": "div",
                "selector": "#appView .promoteBtn.btn1",
                "text": "我已保存",
                "class_tokens": ["promoteBtn", "btn1"],
                "ancestor_class_tokens": [
                    "promoteBtnBox",
                    "van-popup",
                    "van-popup--center",
                ],
                "rect": {"x": 50, "y": 658, "width": 135, "height": 46},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
            {
                "tag": "div",
                "selector": "#appView .promoteBtn.btn2",
                "text": "立即保存",
                "class_tokens": ["promoteBtn", "btn2"],
                "ancestor_class_tokens": [
                    "promoteBtnBox",
                    "van-popup",
                    "van-popup--center",
                ],
                "rect": {"x": 204, "y": 658, "width": 135, "height": 46},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
        ]
    }

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="profile",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].obstruction_capability == "profile_credential_screenshot_confirm"


@pytest.mark.parametrize(
    "page_type",
    [
        "home",
        "content_list",
        "content_detail",
        "video_list",
        "video_detail",
        "profile",
        "settings",
        "payment",
        "post_list",
        "activity",
    ],
)
def test_business_payment_popup_without_close_only_gets_overlay(
    page_type: str,
) -> None:
    """业务页交易 CTA 不可执行；无关闭按钮时只能签发严格蒙版。"""
    dom = """
    <div class="van-overlay" role="button" style="z-index: 2000"></div>
    <div class="van-popup van-popup--center code-popup" role="dialog"
         style="z-index: 2001">
      <div class="content">充值订单 金额 100 元</div>
      <div class="btn">立即支付</div>
    </div>
    """
    snapshot = {
        "elements": [
            {
                "tag": "div",
                "role": "dialog",
                "selector": ".van-popup.code-popup",
                "text": "充值订单 金额 100 元 立即支付",
                "class_tokens": [
                    "van-popup",
                    "van-popup--center",
                    "code-popup",
                ],
                "rect": {"x": 39, "y": 166, "width": 312, "height": 512},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
            {
                "tag": "div",
                "selector": ".van-popup.code-popup .btn",
                "text": "立即支付",
                "class_tokens": ["btn"],
                "ancestor_class_tokens": [
                    "code-popup",
                    "van-popup",
                    "van-popup--center",
                ],
                "rect": {"x": 97, "y": 629, "width": 196, "height": 44},
                "hit_test": {"center_hits_target": True, "topmost_at_center": True},
            },
        ]
    }

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type=page_type,
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].obstruction_capability == "dialog_overlay"


def test_default_page_popup_without_close_only_gets_overlay() -> None:
    """“设置默认页面”没有关闭控件时，只允许签发严格蒙版关闭能力。"""
    dom = """
    <div class="van-overlay" style="z-index: 2000"></div>
    <div class="van-popup van-popup--center default-page-popup"
         style="z-index: 2001">
      <div class="title">设置默认页面</div>
      <div class="content">是否将当前页面设置为默认页面</div>
    </div>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=None,
    )

    assert len(actions) == 1
    assert actions[0].target == "关闭当前弹窗"
    assert actions[0].obstruction_capability == "dialog_overlay"


@pytest.mark.parametrize("page_type", ["line_loading", "startup_ad"])
def test_lifecycle_page_does_not_use_business_payment_overlay_policy(
    page_type: str,
) -> None:
    """线路壳和开屏广告不得消费业务页交易弹窗蒙版合同。"""
    dom = """
    <div class="van-overlay" role="button" style="z-index: 2000"></div>
    <div class="van-popup van-popup--center payment-popup" role="dialog"
         style="z-index: 2001">
      <div class="content">充值订单 金额 100 元</div>
      <div class="btn">立即支付</div>
    </div>
    """

    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type=page_type,
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=None,
    )

    assert actions == []


def test_profile_activity_visual_ocr_candidate_is_controlled_and_binds_screenshot() -> None:
    image = PNG_BYTES + b"profile-activity"
    visual = page_detection_tools.CandidateAction(
        action="click",
        target="签到中心",
        target_keywords=["ocr:签到中心"],
        risk_level="low",
        purpose="模型原始目的不得进入受控候选",
        visual_bounds=page_detection_tools.VisualBounds(
            screenshot_index=1,
            left_ratio=0.72,
            top_ratio=0.18,
            right_ratio=0.94,
            bottom_ratio=0.27,
            confidence=0.93,
        ),
    )

    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[visual],
        dom_chunks=["<main>个人中心</main>"],
        image_payloads=[("profile-shot", image, "image/png")],
    )

    assert len(actions) == 1
    assert actions[0].target == "签到中心"
    assert actions[0].expected_next_page_type == "activity"
    assert actions[0].risk_level == "medium"
    assert "evidence:screenshot" in actions[0].target_keywords
    payload = page_detection_tools._result_visual_bounds(actions[0])
    assert payload["artifact_id"] == "profile-shot"
    assert payload["sha256"] == f"sha256:{hashlib.sha256(image).hexdigest()}"


def test_profile_settings_image_candidate_is_controlled_and_binds_screenshot() -> None:
    image = PNG_BYTES + b"profile-settings"
    visual = page_detection_tools.CandidateAction(
        action="click",
        target="设置",
        target_keywords=["image:设置按钮"],
        risk_level="low",
        purpose="模型原始目的不得进入受控候选",
        visual_bounds=page_detection_tools.VisualBounds(
            screenshot_index=1,
            left_ratio=0.86,
            top_ratio=0.04,
            right_ratio=0.96,
            bottom_ratio=0.10,
            confidence=0.94,
        ),
    )

    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[visual],
        dom_chunks=["<main>个人中心</main>"],
        page_url="https://example.test/Mine",
        image_payloads=[("profile-shot", image, "image/png")],
    )

    assert len(actions) == 1
    assert actions[0].target == "设置"
    assert actions[0].expected_next_page_type == "settings"
    assert actions[0].risk_level == "medium"
    assert "evidence:screenshot" in actions[0].target_keywords
    payload = page_detection_tools._result_visual_bounds(actions[0])
    assert payload["artifact_id"] == "profile-shot"
    assert payload["sha256"] == f"sha256:{hashlib.sha256(image).hexdigest()}"


def test_profile_payment_dom_prefers_immediate_open() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<button data-testid="member-center">会员中心</button>'
            '<button data-testid="open-now">立即开通</button>'
        ],
        page_url="https://example.test/profile",
        image_count=1,
    )

    payment_actions = [item for item in actions if item.expected_next_page_type == "payment"]
    assert [item.target for item in payment_actions] == ['button[data-testid="open-now"]:visible']
    assert payment_actions[0].risk_level == "low"


def test_profile_payment_accepts_h5_vip_section_immediate_open_div() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main><div class="vip-section"><div class="info">'
            '<div class="button">立即开通</div></div></div></main>'
        ),
        page_url="https://h5.bkh052.com/mine",
        image_count=1,
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心会员支付入口"
    assert actions[0].expected_next_page_type == "payment"
    assert "evidence:structure" in actions[0].target_keywords
    assert "capability:vip_section_immediate_open_text" in actions[0].target_keywords


def test_profile_payment_dom_excludes_same_keyword_visual_fallback() -> None:
    visual = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        target_keywords=["evidence:screenshot"],
        risk_level="low",
        purpose="模型原始目的不得进入受控候选",
        visual_bounds=page_detection_tools.VisualBounds(
            screenshot_index=1,
            left_ratio=0.1,
            top_ratio=0.4,
            right_ratio=0.9,
            bottom_ratio=0.5,
            confidence=0.92,
        ),
    )

    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[visual],
        dom_chunks=['<button data-testid="open-now">立即开通</button>'],
        page_url="https://example.test/profile",
        image_count=1,
    )

    assert [item.target for item in actions] == ['button[data-testid="open-now"]:visible']
    assert actions[0].visual_bounds is None
    assert actions[0].risk_level == "low"


def test_profile_payment_visual_immediate_beats_member_center_dom() -> None:
    visual = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        target_keywords=["evidence:screenshot"],
        risk_level="low",
        purpose="模型原始目的不得进入受控候选",
        visual_bounds=page_detection_tools.VisualBounds(
            screenshot_index=1,
            left_ratio=0.1,
            top_ratio=0.4,
            right_ratio=0.9,
            bottom_ratio=0.5,
            confidence=0.92,
        ),
    )

    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[visual],
        dom_chunks=['<button data-testid="member-center">会员中心</button>'],
        page_url="https://example.test/profile",
        image_count=1,
    )

    assert [item.target for item in actions] == ["立即开通"]
    assert actions[0].risk_level == "medium"
    assert actions[0].purpose == "探索我的页会员支付入口：立即开通"


def test_profile_payment_visual_binds_selected_screenshot_identity() -> None:
    selected_image = PNG_BYTES + b"selected"
    image_label = "shot-2"
    visual = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        target_keywords=["evidence:screenshot"],
        risk_level="low",
        purpose="模型原始目的不得进入受控候选",
        visual_bounds=page_detection_tools.VisualBounds(
            screenshot_index=2,
            left_ratio=0.1,
            top_ratio=0.4,
            right_ratio=0.9,
            bottom_ratio=0.5,
            confidence=0.92,
        ),
    )

    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[visual],
        dom_chunks=["<main>个人资料</main>"],
        page_url="https://example.test/profile",
        image_payloads=[
            ("shot-1", PNG_BYTES + b"first", "image/png"),
            (image_label, selected_image, "image/png"),
        ],
    )
    payload = page_detection_tools._result_payload(
        page_capture_id="capture-1",
        page_url="https://example.test/profile",
        detection_status="recognized",
        page_type="profile",
        page_state="stable",
        confidence=0.95,
        reason="测试受控视觉候选",
        candidate_actions=actions,
        catalog_version="test",
        model="test",
    )

    bounds = payload["candidate_actions"][0]["visual_bounds"]
    assert bounds["artifact_id"] == "shot-2"
    assert "frame_index" not in bounds
    assert bounds["sha256"] == f"sha256:{hashlib.sha256(selected_image).hexdigest()}"


def test_profile_payment_visual_rejects_timed_capture_frame() -> None:
    visual = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        target_keywords=["evidence:screenshot"],
        risk_level="low",
        purpose="模型原始目的不得进入受控候选",
        visual_bounds=page_detection_tools.VisualBounds(
            screenshot_index=2,
            left_ratio=0.1,
            top_ratio=0.4,
            right_ratio=0.9,
            bottom_ratio=0.5,
            confidence=0.92,
        ),
    )

    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[visual],
        dom_chunks=["<main>个人资料</main>"],
        page_url="https://example.test/profile",
        image_payloads=[
            ("sequence#frame-0", PNG_BYTES + b"frame-0", "image/png"),
            ("sequence#frame-4", PNG_BYTES + b"frame-4", "image/png"),
        ],
    )

    assert actions == []


def test_profile_payment_invalid_visual_falls_back_to_member_center_dom() -> None:
    low_confidence_visual = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        target_keywords=["evidence:screenshot"],
        risk_level="low",
        purpose="低置信度视觉候选",
        visual_bounds=page_detection_tools.VisualBounds(
            screenshot_index=2,
            left_ratio=0.1,
            top_ratio=0.4,
            right_ratio=0.9,
            bottom_ratio=0.5,
            confidence=0.79,
        ),
    )

    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[low_confidence_visual],
        dom_chunks=['<button data-testid="member-center">会员中心</button>'],
        page_url="https://example.test/profile",
        image_count=1,
    )

    assert [item.target for item in actions] == ['button[data-testid="member-center"]:visible']


def test_profile_payment_entry_is_not_added_outside_profile() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="充值",
                target_keywords=["充值"],
                risk_level="low",
                purpose="模型泛化支付入口",
            )
        ],
        dom_chunks=['<button data-testid="open-now">立即开通</button>'],
        page_url="https://example.test/",
        image_count=1,
    )

    assert actions == []


def test_profile_payment_discards_generic_model_payment_candidate() -> None:
    generic = page_detection_tools.CandidateAction(
        action="click",
        target="充值",
        target_keywords=["充值"],
        risk_level="low",
        purpose="模型泛化支付入口",
    )

    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[generic],
        dom_chunks=["<main>个人资料</main>"],
        page_url="https://example.test/profile",
        image_count=1,
    )

    assert actions == []


def _ambiguous_output() -> PageDetectionModelOutput:
    return _recognized_output("home").model_copy(
        update={
            "detection_status": "ambiguous",
            "page_type": None,
            "confidence": 0.61,
            "reason": "模型无法在候选页面间收敛。",
            "page_type_candidates": [],
        }
    )


async def _detect_vip_case(
    *,
    page_url: str,
    dom_html: str,
    output: PageDetectionModelOutput,
) -> dict[str, Any]:
    dom = _artifact("dom-vip", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-vip", "screenshot", content_type="image/png")
    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-vip",
        )
    ):
        return await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot, page_url=page_url)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-vip": dom_html.encode("utf-8"),
                    "shot-vip": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )


async def test_vip_path_with_visible_open_vip_text_confirms_payment() -> None:
    result = await _detect_vip_case(
        page_url="https://example.test/vip?from=profile",
        dom_html="<html><body><main>开通VIP</main></body></html>",
        output=_ambiguous_output(),
    )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "payment"
    assert "开通VIP" in result["matched_catalog_signals"]["dom_hints"]
    assert result["rule_matching_allowed"] is True


async def test_vip_path_without_open_vip_text_downgrades_model_payment() -> None:
    result = await _detect_vip_case(
        page_url="https://example.test/vip",
        dom_html="<html><body><main>会员方案</main></body></html>",
        output=_recognized_output("payment"),
    )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["rule_matching_allowed"] is False


async def test_open_vip_text_outside_vip_path_does_not_trigger_special_confirmation() -> None:
    result = await _detect_vip_case(
        page_url="https://example.test/profile",
        dom_html="<html><body><main>开通VIP</main></body></html>",
        output=_ambiguous_output(),
    )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None


@pytest.mark.parametrize("path", ["/vip/", "/VIP"])
async def test_non_exact_vip_path_does_not_trigger_payment_confirmation(path: str) -> None:
    result = await _detect_vip_case(
        page_url=f"https://example.test{path}",
        dom_html="<html><body><main>开通VIP</main></body></html>",
        output=_ambiguous_output(),
    )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None


async def test_vip_path_accepts_model_screenshot_text_signal() -> None:
    output = _ambiguous_output().model_copy(
        update={
            "matched_catalog_signals": page_detection_tools.MatchedCatalogSignals(
                ocr_keywords=["开通VIP"]
            )
        }
    )

    result = await _detect_vip_case(
        page_url="https://example.test/vip#plan",
        dom_html="<html><body><main>会员方案</main></body></html>",
        output=output,
    )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "payment"
    assert "开通VIP" in result["matched_catalog_signals"]["ocr_keywords"]


async def test_existing_non_vip_payment_recognition_is_unchanged() -> None:
    result = await _detect_vip_case(
        page_url="https://example.test/payment",
        dom_html="<html><body><main>支付方式与价格</main></body></html>",
        output=_recognized_output("payment"),
    )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "payment"


def test_immediate_open_is_high_risk_only_on_payment_page() -> None:
    action = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        risk_level="low",
        purpose="点击立即开通",
    )

    assert (
        page_detection_tools._with_deterministic_high_risk_actions([action], page_type="profile")[
            0
        ].risk_level
        == "low"
    )
    assert (
        page_detection_tools._with_deterministic_high_risk_actions([action], page_type="payment")[
            0
        ].risk_level
        == "high"
    )


@pytest.mark.parametrize("target", ["确认开通", "立即支付", "提交订单"])
def test_payment_confirmation_actions_are_always_high_risk(target: str) -> None:
    action = page_detection_tools.CandidateAction(
        action="click",
        target=target,
        risk_level="low",
        purpose=target,
    )

    assert (
        page_detection_tools._with_deterministic_high_risk_actions([action], page_type="profile")[
            0
        ].risk_level
        == "high"
    )


@pytest.mark.parametrize("page_state", ["content_list", "stable_content"])
async def test_model_business_state_alias_is_normalized_before_rule_and_frontier_gates(
    page_state: str,
) -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    output = _recognized_output("video_list").model_copy(
        update={"page_state": page_state, "candidate_actions": []}
    )
    business_dom = (
        b'<html><body><main class="masonry-grid"><article class="grid-item">'
        b'<div class="movie-item"><div class="video-cover">movie</div>'
        b'<div class="video-title">title</div></div>'
        b"</article></main></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": business_dom, "shot-1": PNG_BYTES}),
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "video_list"
    assert result["page_state"] == "stable"
    assert result["rule_matching_allowed"] is True
    assert any(
        action["expected_next_page_type"] == "video_detail"
        for action in result["candidate_actions"]
    )
    detail_action = next(
        action
        for action in result["candidate_actions"]
        if action["expected_next_page_type"] == "video_detail"
    )
    assert detail_action["target"] == "当前列表首个内容卡片"
    assert detail_action["list_detail_selector"] == (
        ".masonry-grid .movie-item .video-title:visible"
    )
    assert "deterministic_business_page_state_alias" in result["risk_flags"]


@pytest.mark.parametrize(
    "page_state",
    [
        "stable_with_broken_images",
        "partially_obstructed",
        "stable_with_ad",
        "stable_with_ad_banner",
        "stable_with_advertisements",
        "stable_with_advertising",
        "stable_with_banner",
        "stable_with_bottom_ad",
        "stable_with_bottom_ad_banner",
        "stable_with_bottom_banner",
        "stable_with_app_open_banner",
        "stable_with_floating_ad",
        "stable_with_floating_promotional_content",
        "stable_with_inline_promotional_content",
        "stable_with_promotional_elements",
        "stable_with_sticky_ad",
        "stable_content_with_transient_ad",
        "stable_with_transient_content_loading",
    ],
)
async def test_stable_state_with_local_quality_degradation_keeps_navigation(
    page_state: str,
) -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    output = _recognized_output("home").model_copy(
        update={"page_state": page_state, "candidate_actions": []}
    )
    business_dom = (
        b'<html><body><div class="van-tabbar">'
        b'<div class="van-tabbar-item van-tabbar-item--active">home</div>'
        b'<div class="van-tabbar-item">comic</div>'
        b"</div></body></html>"
    )
    resolver = FakeNavAliasResolver(
        {
            "home": PageNavAliasMatch(
                canonical_code="home",
                page_subtype_code=None,
                matched_term="home",
                source_category="home",
                confidence=1.0,
                match_type="static",
            ),
            "comic": PageNavAliasMatch(
                canonical_code="content_list",
                page_subtype_code="comic_anime",
                matched_term="comic",
                source_category="comic",
                confidence=0.95,
                match_type="exact",
            ),
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": business_dom, "shot-1": PNG_BYTES}),
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=resolver,
        )

    assert result["page_state"] == "stable"
    assert result["rule_matching_allowed"] is True
    assert any(
        action["expected_next_page_type"] == "content_list"
        for action in result["candidate_actions"]
    )


@pytest.mark.parametrize("page_state", ["stable", "feed", "loaded", "notice"])
def test_content_list_does_not_sign_card_without_title(page_state: str) -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state=page_state,
        candidate_actions=[],
        dom_chunks=[
            '<div class="masonry-grid"><div class="grid-item">'
            '<div class="data-item">内容</div></div></div>'
            '<div class="van-tabbar">'
            '<div class="van-tabbar-item">首页</div>'
            '<div class="van-tabbar-item van-tabbar-item--active">漫画</div>'
            "</div>"
        ],
    )

    assert actions == []


def test_content_list_prefers_layout3_cover_over_cached_masonry_items() -> None:
    """同页缓存离屏 masonry 时，应签发当前 layout3 的无遮挡标题。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="video-section"><div class="layout3 layouts">'
            '<div class="content"><div><div class="img-box">'
            '<img alt="当前推荐"></div><div class="name">当前推荐</div>'
            "</div></div></div></div>"
            '<div class="masonry-grid"><div class="grid-item">'
            '<div class="data-item">缓存离屏卡片</div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".video-section .layout3 .name:visible"
    assert actions[0].expected_next_page_type == "content_detail"


def test_content_list_uses_explicit_detail_entry_card() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<main><button class="card secondary detail-entry" type="button">文章一</button></main>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".detail-entry:visible"
    assert actions[0].expected_next_page_type == "content_detail"
    assert h5_list_detail_selector_allowed(
        source_page_type="content_list",
        selector=actions[0].target,
    )


def test_deterministic_structure_recognizes_content_list_with_detail_entry() -> None:
    match = page_detection_tools._deterministic_page_type_from_structure(
        [
            '<main class="content-list">'
            '<button class="detail-entry" type="button">文章一</button>'
            "</main>"
        ],
        include_business_route=False,
    )

    assert match is not None
    assert match.page_type == "content_list"


def test_deterministic_structure_recognizes_profile_with_explicit_risk_entries() -> None:
    match = page_detection_tools._deterministic_page_type_from_structure(
        [
            '<main class="profile-page">'
            '<button data-risk-action="payment">充值购买</button>'
            '<button data-risk-action="post_publish">发布内容</button>'
            '<button data-risk-action="upload">上传文件</button>'
            "</main>"
        ],
        include_business_route=False,
    )

    assert match is not None
    assert match.page_type == "profile"


def test_content_list_uses_mod_video_title_for_content_detail() -> None:
    """站点一、二的模块内容卡必须通过标题进入规范内容详情。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="modBox"><div class="modVideoBox">'
            '<div class="modCover"></div>'
            '<div class="modCover_name">内容标题</div>'
            "</div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == (".modVideoBox .modCover_name:visible")
    assert actions[0].expected_next_page_type == "content_detail"


@pytest.mark.parametrize(
    ("dom", "expected_selector"),
    [
        (
            '<article class="post-box post"><div class="post-text">'
            '<div class="text-overflow"><span class="overEllipsis">帖子标题</span>'
            "</div></div></article>",
            ".post-box.post .post-text .text-overflow:visible",
        ),
        (
            '<div class="post-main-item"><div class="content-images-wrap">'
            '<div class="title">社区帖子标题</div></div></div>',
            ".post-main-item .content-images-wrap .title:visible",
        ),
    ],
)
def test_post_list_real_titles_enter_content_detail(
    dom: str,
    expected_selector: str,
) -> None:
    """帖子标题统一进入 content_detail，不引入 post_detail 页型。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    assert actions[0].target == expected_selector
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_title_parent_is_used_when_child_hit_lands_on_parent() -> None:
    """标题子节点命中父容器时，必须点击真实承接事件的标题容器。"""
    dom = (
        '<article class="post-box post"><div class="post-text">'
        '<div class="text-overflow"><span class="overEllipsis">帖子标题</span>'
        "</div></div></article>"
    )
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "tag": "span",
                "class_tokens": ["overEllipsis"],
                "ancestor_class_tokens": [
                    "text-overflow",
                    "post-text",
                    "post-box",
                    "post",
                ],
                "rect": {"x": 20, "y": 180, "width": 320, "height": 32},
                "hit_test": {
                    "center_hits_target": False,
                    "center_target_class_tokens": ["text-overflow"],
                },
            },
            {
                "tag": "div",
                "class_tokens": ["text-overflow"],
                "ancestor_class_tokens": ["post-text", "post-box", "post"],
                "rect": {"x": 20, "y": 180, "width": 320, "height": 32},
                "hit_test": {"center_hits_target": True},
            },
        ],
    }

    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].target == ".post-box.post .post-text .text-overflow:visible"
    assert actions[0].expected_next_page_type == "content_detail"


def test_content_list_replaces_model_detail_actions_with_single_dom_candidate() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="content title",
                target_keywords=["content title"],
                risk_level="low",
                purpose="enter content detail",
                expected_next_page_type="content_detail",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="other title",
                target_keywords=["other title"],
                risk_level="low",
                purpose="enter another detail",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="filter",
                target_keywords=["filter"],
                risk_level="low",
                purpose="switch filter",
            ),
        ],
        dom_chunks=[
            '<div class="data-list"><div class="masonry-grid">'
            '<div class="grid-item"><div class="data-item">content title</div></div>'
            "</div></div>"
        ],
    )

    assert [action.target for action in actions] == ["filter"]


@pytest.mark.parametrize("action", ["click", "goto", "navigate"])
def test_missing_registered_title_removes_all_model_detail_navigation(
    action: str,
) -> None:
    """无登记标题时，任何动作形态都不得绕过详情入口合同。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action=action,
                target="模型猜测的详情入口",
                target_keywords=["video_detail"],
                risk_level="low",
                purpose="进入视频详情",
                expected_next_page_type="video_detail",
                destination_url="/video/1" if action != "click" else None,
            )
        ],
        dom_chunks=['<div class="video-list"><div class="movie-card">封面</div></div>'],
    )

    assert actions == []


def test_stable_content_post_list_replaces_ambiguous_model_title() -> None:
    """模型把稳定态写成 stable_content 时仍应改用结构化帖子首卡。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable_content",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="重复帖子标题",
                target_keywords=["重复帖子标题"],
                risk_level="low",
                purpose="打开当前社区内容流中的首个可见内容卡片",
                expected_next_page_type="content_detail",
                navigation_depth=1,
            )
        ],
        dom_chunks=[
            '<div class="dynamicData-box"><div class="dynamicData-content">'
            '<div class="title">重复帖子标题</div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".dynamicData-box:visible .title:visible"
    assert actions[0].target_keywords[:2] == [
        "nested_content",
        "list_detail_navigation",
    ]


def test_video_list_prefers_masonry_movie_cover_detail_action() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="video-section"><div class="layout5">'
            '<div class="img-box"><img alt="ad or hidden section"></div></div></div>'
            '<div class="masonry-grid"><div class="grid-item">'
            '<div class="movie-item"><div class="video-cover pc">'
            '<img class="cover-img" alt="video"></div>'
            '<div class="video-title">真实视频</div></div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".masonry-grid .movie-item .video-title:visible"
    assert actions[0].expected_next_page_type == "video_detail"


def test_video_list_adds_long_video_card_detail_action() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="van-list"><div class="row-four-card">'
            '<div class="item-wrapper"><div class="longVideoCard">'
            "<p>真实视频</p><span>03:21</span>"
            "</div></div></div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".longVideoCard:visible"
    assert actions[0].expected_next_page_type == "video_detail"


async def test_content_list_model_output_is_refined_by_long_video_feed_structure() -> None:
    page_detection_tools._PAGE_DETECTION_RESULT_CACHE.clear()
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    feed_dom = (
        b'<html><body><div class="van-list"><div class="row-four-card">'
        b'<div class="item-wrapper"><div class="longVideoCard">video 03:21</div>'
        b"</div></div></div></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": feed_dom, "shot-1": PNG_BYTES}),
            chat_client=FakeChatClient(_recognized_output("content_list")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "video_list"
    assert result["model_page_type"] == "content_list"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert result["reason"] == "可信 DOM 命中完整视频流卡片结构，修正为视频列表页。"
    assert "deterministic_h5_long_video_feed_page_type_refinement" in result["risk_flags"]
    assert any(
        action["list_detail_selector"] == ".longVideoCard:visible"
        and action["expected_next_page_type"] == "video_detail"
        for action in result["candidate_actions"]
    )


async def test_model_failure_uses_long_video_feed_structure() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    page_detection_tools._PAGE_DETECTION_RESULT_CACHE.clear()
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    feed_dom = (
        b'<html><body><div class="van-list"><div class="row-four-card">'
        b'<div class="item-wrapper"><div class="longVideoCard">video 03:21</div>'
        b"</div></div></div></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": feed_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "video_list"
    assert result["confidence_source"] == "deterministic_structure"
    assert "deterministic_page_structure_provider_fallback" in result["risk_flags"]


def test_video_list_does_not_sign_movie_card_without_title() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="masonry-grid">'
            '<div class="data-item banner">广告横幅</div>'
            '<div class="grid-item"><div class="movie-item">影片</div></div>'
            "</div>"
        ],
    )

    assert actions == []


def test_video_list_does_not_sign_layout_cover_without_title() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="video-section"><div class="movie-list-wraper">'
            '<div class="layout5"><div class="content">'
            '<div class="img-box"><img alt="video"></div>'
            "</div></div></div></div>"
        ],
    )

    assert actions == []


def test_video_list_prefers_card_title_over_dead_cover_img() -> None:
    """layout5 结构无 video-section 时应优先标题 .name（封面 img 点击无路由）。

    真实回归：测试站 /movie 卡片封面点击零导航（任务 06666a1d seq26），
    路由绑在 .van-ellipsis.name 标题上。
    """
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="wraper"><div class="layout5"><div class="content">'
            '<div><div class="img-box"><img alt="video"></div>'
            '<div class="van-ellipsis name">极品视频标题</div></div>'
            "</div></div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".layout5 .name:visible"
    assert actions[0].expected_next_page_type == "video_detail"


def test_video_list_title_outranks_video_section_cover() -> None:
    """同页兼有 video-section 封面与 .name 标题时应优先标题（封面常无路由）。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="video-section"><div class="layout5"><div class="content">'
            '<div><div class="img-box"><img alt="video"></div>'
            '<div class="van-ellipsis name">视频标题</div></div>'
            "</div></div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".layout5 .name:visible"
    assert actions[0].expected_next_page_type == "video_detail"


def test_video_list_manga_title_is_used_for_video_detail_navigation() -> None:
    """漫画内容列表归为视频列表后，只能通过当前无遮挡标题进入视频详情。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="box"><div class="item-box-comics">'
            '<div class="md-img"><img alt="cover"></div>'
            '<div class="duohang2 text-11">漫画视频标题</div>'
            "</div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".item-box-comics .duohang2:visible"
    assert actions[0].expected_next_page_type == "video_detail"


def test_video_list_long_video_card_title_is_used_for_video_detail_navigation() -> None:
    """首页长视频流只能通过当前无遮挡标题进入视频详情。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="row-four-card"><div class="card-item">'
            '<div class="item-wrapper"><div class="longVideoCard">'
            '<div class="item-bg"><img alt="cover"></div>'
            '<div class="line-clamp-2 text-12 font-500 mt-3 h-36">'
            "<p>当前无遮挡视频标题</p></div>"
            "</div></div></div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == (".longVideoCard .line-clamp-2 > p:visible")
    assert actions[0].target_keywords == [
        "nested_content",
        "list_detail_navigation",
        "unobstructed_title",
        "page_type:video_list",
    ]
    assert actions[0].purpose == "通过当前无遮挡标题进入视频详情"


def test_video_list_below_fold_big_list_scrolls_before_clicking_title() -> None:
    """首屏没有可命中标题时先滚动一屏，禁止派发离屏标题点击。"""
    dom = (
        '<main class="home-video"><section class="big-video-section">'
        '<li class="big-list"><div class="decial-wrap">'
        '<div class="decial">第二屏视频标题</div></div></li>'
        "</section></main>"
    )
    snapshot = {
        "viewport": {"width": 390, "height": 844, "scroll_y": 0},
        "document": {"scroll_height": 1800},
        "elements": [
            {
                "tag": "div",
                "selector": ".big-list .decial",
                "class_tokens": ["decial"],
                "ancestor_class_tokens": ["decial-wrap", "big-list"],
                "rect": {"x": 20, "y": 980, "width": 350, "height": 50},
                "hit_test": {"center_hits_target": False},
            }
        ],
    }

    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )
    actions = page_detection_tools._with_list_viewport_scroll_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=actions,
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].action == "scroll"
    assert actions[0].target == "向下滚动一屏查找视频标题"
    assert "list_viewport_scroll" in actions[0].target_keywords


def test_video_list_scrolled_big_list_clicks_visible_title() -> None:
    """滚动后的新快照证明标题可命中时，才签发视频详情点击。"""
    dom = (
        '<li class="big-list"><div class="decial-wrap">'
        '<div class="decial">当前可见视频标题</div></div></li>'
    )
    snapshot = {
        "viewport": {"width": 390, "height": 844, "scroll_y": 760},
        "document": {"scroll_height": 1800},
        "elements": [
            {
                "tag": "div",
                "selector": ".big-list .decial",
                "class_tokens": ["decial"],
                "ancestor_class_tokens": ["decial-wrap", "big-list"],
                "rect": {"x": 20, "y": 180, "width": 350, "height": 50},
                "hit_test": {"center_hits_target": True},
            }
        ],
    }

    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[dom],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].action == "click"
    assert actions[0].target == "li.big-list .decial:visible"
    assert actions[0].expected_next_page_type == "video_detail"


def test_h5_detail_internal_overflow_adds_two_screen_coverage_candidate() -> None:
    """详情页存在真实内部纵向溢出时，签发一次两屏覆盖动作。"""
    snapshot = {
        "viewport": {"width": 390, "height": 844, "scroll_y": 0},
        "document": {"scroll_height": 844},
        "elements": [
            {
                "selector": "#contentId",
                "visible": True,
                "rect": {"x": 0, "y": 0, "width": 390, "height": 761},
                "style": {"overflow_y": "auto"},
                "text_metrics": {
                    "scroll_height": 4530,
                    "client_height": 761,
                    "vertical_overflow": True,
                },
            }
        ],
    }

    actions = page_detection_tools._with_detail_scroll_coverage_candidate(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=['<main id="contentId">详情正文</main>'],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].action == "scroll"
    assert actions[0].target == "采集详情页第二屏内容"
    assert actions[0].target_keywords == [
        "detail_scroll_coverage",
        "two_screen_sequence",
        "page_type:video_detail",
    ]


def test_h5_detail_without_vertical_overflow_does_not_add_scroll_candidate() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844, "scroll_y": 0},
        "document": {"scroll_height": 844},
        "elements": [],
    }

    assert page_detection_tools._with_detail_scroll_coverage_candidate(
        page_type="content_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<main>单屏详情</main>"],
        ui_snapshot=snapshot,
    ) == []


def test_h5_list_coverage_requires_real_overflow_and_stable_page() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844, "scroll_y": 0},
        "document": {"scroll_height": 4200},
        "elements": [],
    }
    actions = page_detection_tools._with_list_scroll_coverage_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<main>列表</main>"],
        ui_snapshot=snapshot,
    )
    assert len(actions) == 1
    assert actions[0].target_keywords == ["list_scroll_coverage", "bounded_to_bottom"]
    assert page_detection_tools._with_list_scroll_coverage_candidate(
        page_type="content_list",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=["<main>列表</main>"],
        ui_snapshot=snapshot,
    ) == []
    snapshot["viewport"]["scroll_y"] = 500
    assert page_detection_tools._with_list_scroll_coverage_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<main>列表</main>"],
        ui_snapshot=snapshot,
    ) == []


def test_h5_home_with_real_overflow_collects_bounded_scroll_evidence() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844, "scroll_y": 0},
        "document": {"scroll_height": 4200},
        "elements": [],
    }

    actions = page_detection_tools._with_list_scroll_coverage_candidate(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<main>首页内容</main>"],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].target == "采集列表至底部"


def test_h5_detail_journey_uses_only_unique_visible_safe_controls() -> None:
    snapshot = {
        "elements": [
            {"tag": "video", "visible": True,
             "media": {"paused": True, "ended": False}},
            {"text": "播放", "interactive": True, "visible": True,
             "hit_test": {"center_hits_target": True}, "external": False},
            {"text": "下一集", "interactive": True, "visible": True,
             "hit_test": {"center_hits_target": True}, "external": False},
            {"text": "分享", "interactive": True, "visible": True,
             "hit_test": {"center_hits_target": True}, "external": False},
            {"text": "分享", "interactive": True, "visible": True,
             "hit_test": {"center_hits_target": False}, "external": False},
        ],
    }
    actions = page_detection_tools._with_detail_journey_candidates(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<video controls></video>"],
        ui_snapshot=snapshot,
    )
    assert [(item.action, item.target) for item in actions] == [
        ("click", "播放"),
        ("click", "下一集"),
        ("click", "分享"),
    ]
    snapshot["elements"][0]["media"]["paused"] = False
    playing_actions = page_detection_tools._with_detail_journey_candidates(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<video controls></video>"],
        ui_snapshot=snapshot,
    )
    assert [(item.action, item.target) for item in playing_actions] == [
        ("click", "下一集"),
        ("click", "分享"),
        ("timed_capture", "采集视频播放多帧"),
    ]
    snapshot["elements"][3]["external"] = True
    external_actions = page_detection_tools._with_detail_journey_candidates(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<video controls></video>"],
        ui_snapshot=snapshot,
    )
    assert all(item.target != "分享" for item in external_actions)


def test_h5_reader_journey_avoids_ambiguous_next_page() -> None:
    snapshot = {
        "elements": [
            {"text": "下一页", "interactive": True, "visible": True,
             "hit_test": {"center_hits_target": True}},
            {"text": "下一页", "interactive": True, "visible": True,
             "hit_test": {"center_hits_target": True}},
            {"text": "分享", "interactive": True, "visible": True,
             "hit_test": {"center_hits_target": True}},
        ],
    }
    actions = page_detection_tools._with_detail_journey_candidates(
        page_type="content_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=["<article>正文</article>"],
        ui_snapshot=snapshot,
    )
    assert [(item.action, item.target) for item in actions] == [("click", "分享")]


def test_h5_episode_selector_clicks_only_proven_next_episode() -> None:
    snapshot = {"elements": [
        {"text": "第1集", "interactive": True, "visible": True,
         "aria_selected": "true", "hit_test": {"center_hits_target": True}},
        {"text": "第2集", "interactive": True, "visible": True,
         "hit_test": {"center_hits_target": True}},
        {"text": "第3集", "interactive": True, "visible": True,
         "hit_test": {"center_hits_target": True}},
    ]}
    actions = page_detection_tools._with_detail_journey_candidates(
        page_type="video_detail", page_state="popup", candidate_actions=[],
        dom_chunks=['<div role="dialog">选集</div>'], ui_snapshot=snapshot,
    )
    assert [(item.action, item.target) for item in actions] == [("click", "第2集")]
    snapshot["elements"][0].pop("aria_selected")
    assert page_detection_tools._with_detail_journey_candidates(
        page_type="video_detail", page_state="popup", candidate_actions=[],
        dom_chunks=['<div role="dialog">选集</div>'], ui_snapshot=snapshot,
    ) == []


def test_video_list_adds_home_video_panel_grid_detail_action() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<section class="home-video-panel">'
            '<div class="home-video-panel__scroll">'
            '<div class="TwoGridLayout"><div class="item">'
            '<div class="pic"><img class="wh-full" alt="video"></div>'
            '<div class="desc">视频标题</div>'
            '<p class="play-time">00:09:07</p>'
            "</div></div></div></section>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == (".home-video-panel .TwoGridLayout .item .desc:visible")
    assert actions[0].expected_next_page_type == "video_detail"


def test_home_main_tab_video_grid_title_enters_video_detail() -> None:
    """首页承载视频主 Tab 时，应通过当前无遮挡标题进入视频详情。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.com/?mainTab=video",
        dom_chunks=[
            '<section class="home-video-panel">'
            '<div class="home-video-panel__scroll">'
            '<div class="TwoGridLayout"><div class="item">'
            '<div class="pic"><img class="wh-full" alt="video"></div>'
            '<div class="desc">首页视频标题</div>'
            '<p class="play-time">00:01</p></div></div></div></section>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == (".home-video-panel .TwoGridLayout .item .desc:visible")
    assert actions[0].expected_next_page_type == "video_detail"


def test_home_video_title_class_enters_video_detail() -> None:
    """首页存在 videoTitle 标题结构时，应生成唯一视频详情入口。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=['<section><div class="videoTitle">首页视频标题</div></section>'],
    )

    assert len(actions) == 1
    assert actions[0].target == ".videoTitle:visible"
    assert actions[0].target_keywords == [
        "nested_content",
        "list_detail_navigation",
        "unobstructed_title",
        "page_type:home",
    ]
    assert actions[0].expected_next_page_type == "video_detail"


@pytest.mark.parametrize(
    ("selector", "element"),
    [
        (
            ".home-video-panel .TwoGridLayout .item .desc:visible",
            {
                "class_tokens": ["desc"],
                "ancestor_class_tokens": [
                    "item",
                    "TwoGridLayout",
                    "home-video-panel__scroll",
                ],
            },
        ),
        (
            ".videoTitle:visible",
            {"class_tokens": ["videoTitle"], "ancestor_class_tokens": []},
        ),
    ],
)
def test_home_video_title_selector_is_executable_and_requires_visibility(
    selector: str,
    element: dict[str, object],
) -> None:
    assert h5_list_detail_selector_allowed(
        source_page_type="home",
        selector=selector,
    )
    assert h5_list_detail_selector_requires_visible_title(selector)
    assert h5_list_detail_snapshot_element_matches(
        element=element,
        source_page_type="home",
        selector=selector,
    )


def test_home_adds_horizontal_video_card_detail_action() -> None:
    """首页只有整卡而没有已登记标题时不得签发详情入口。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div id="van-tab-25"><div class="horizontalVideoBox videoBox">'
            '<img alt="video"><span>大长腿学生妹被学长抓住把柄的</span>'
            "</div></div>"
        ],
    )

    assert actions == []


def test_content_list_prefers_comics_item_title_detail_action() -> None:
    """comics-item 漫画卡结构应以标题 .title 作为详情入口（/2d 实测 /workDetail）。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="comics-item"><div class="con">'
            '<div class="float"><img alt="cover"></div>'
            '<div class="title">秘密教学</div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".comics-item .title:visible"
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_supports_dynamic_box_title_variant() -> None:
    """dynamic-box 是 dynamicData-box 的站点变体，标题 .title 进详情（/ground 实测）。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="dynamic-box"><div class="dynamic-content">'
            '<div class="title">吃瓜爆料</div><div class="pictures"></div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".dynamic-box .title:visible"
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_adds_dom_backed_nested_detail_action() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="normal",
        candidate_actions=[],
        dom_chunks=[
            '<div class="movie-list"><div class="media-item">'
            '<div class="media-title">帖子</div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == (".movie-list .media-item:visible .media-title:visible")
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_supports_real_dynamic_data_title_structure() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="dynamicData-box"><div class="dynamicData-content">'
            '<div class="title">帖子标题</div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".dynamicData-box:visible .title:visible"
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_supports_community_feed_poi_title_structure() -> None:
    """社区长列表应以 feed 首帖标题签发结构化详情能力。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="国庆狂欢限时开启",
                target_keywords=["国庆狂欢限时开启"],
                risk_level="low",
                purpose="点击首个可见帖子进入帖子详情页",
                expected_next_page_type="content_detail",
            )
        ],
        dom_chunks=[
            '<div class="community_home"><div role="feed" class="van-list">'
            '<div class="item_container"><div class="poi_title">'
            "国庆狂欢限时开启</div></div></div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".van-list .item_container .poi_title:visible"
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_supports_community_home_title_without_item_container() -> None:
    """新版社区 feed 无 item_container 时仍须点击当前无遮挡正文标题。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="贡品微",
                target_keywords=["贡品微"],
                risk_level="low",
                purpose="点击首个低风险帖子卡片进入详情",
                expected_next_page_type="content_detail",
            )
        ],
        dom_chunks=[
            '<div class="community_home"><div role="feed" class="van-list">'
            '<div><div class="header-info"><li class="nickname">贡品微</li></div>'
            '<div class="poi_title !text-12px">当前无遮挡帖子标题</div>'
            "</div></div></div>"
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".community_home .van-list .poi_title:visible"
    assert actions[0].target_keywords == [
        "nested_content",
        "list_detail_navigation",
        "unobstructed_title",
        "page_type:post_list",
    ]
    assert actions[0].purpose == "通过当前无遮挡标题进入内容详情"
    assert actions[0].expected_next_page_type == "content_detail"


@pytest.mark.parametrize(
    ("dom", "expected_selector"),
    [
        (
            '<div class="dynamicData-box"><div class="dynamicData-content">'
            '<div class="title">朋友圈正文标题</div></div></div>',
            ".dynamicData-box:visible .title:visible",
        ),
        (
            '<div class="community_home"><div class="van-list">'
            '<div class="poi_title">朋友圈正文标题</div></div></div>',
            ".community_home .van-list .poi_title:visible",
        ),
    ],
)
def test_content_list_reuses_registered_post_titles_after_moments_reclassification(
    dom: str,
    expected_selector: str,
) -> None:
    """朋友圈归入内容列表后，仍须从已登记标题进入内容详情。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.com/post",
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    assert actions[0].target == expected_selector
    assert actions[0].expected_next_page_type == "content_detail"
    assert h5_list_detail_selector_allowed(
        source_page_type="content_list",
        selector=expected_selector,
    )


def test_content_list_prefers_comic_item_name_detail_action() -> None:
    """comic-item 横滑漫画卡应以结构化 name 叶节点进入详情。"""
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="重复漫画标题",
                target_keywords=["content title"],
                risk_level="low",
                purpose="进入内容详情",
                expected_next_page_type="content_detail",
            )
        ],
        dom_chunks=[
            '<div class="comic-item scroll-x-item scroll-x-item-vertical">'
            '<div class="aspect-ratio"><img alt="cover"></div>'
            '<div class="name van-multi-ellipsis--l2">重复漫画标题</div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".comic-item .name:visible"
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_supports_direct_post_box_card_through_dom_sanitizer() -> None:
    """只有整卡、没有标题时不得签发新的内容详情入口。"""
    dom = (
        '<div class="post-list"><div class="post-box post">'
        '<div class="post-text">帖子正文</div><div class="post-media"></div>'
        "</div></div>"
    )

    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(dom),
    )

    assert actions == []


def test_post_list_does_not_fallback_to_dynamic_data_whole_card() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="dynamicData-box"><div class="dynamicData-content">无标题卡片</div></div>'
        ],
    )

    assert actions == []


def test_content_list_does_not_sign_ratio_cover_without_title() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=['<div class="ratio-box"><img class="ratio-img"></div>'],
    )

    assert actions == []


def test_content_list_supports_ai_page_card_title_structure() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="content_list",
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="图片换脸",
                target_keywords=["图片换脸"],
                risk_level="low",
                purpose="进入图片换脸详情",
                expected_next_page_type="content_detail",
            )
        ],
        dom_chunks=[
            '<div class="ai-page-card-list"><div class="ai-page-card">'
            '<div class="cover"><img alt=""></div>'
            '<div class="ai-page-card-title">图片换脸</div></div></div>'
        ],
    )

    assert len(actions) == 1
    assert actions[0].target == ".ai-page-card .ai-page-card-title:visible"
    assert actions[0].target_keywords[:2] == ["nested_content", "list_detail_navigation"]
    assert actions[0].expected_next_page_type == "content_detail"


def test_post_list_does_not_sign_masonry_card_without_title() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="box"><div class="grid-item">'
            '<div class="ad-box"><div class="banner-item">广告</div></div>'
            "</div></div>"
            '<div class="data-list"><div class="masonry-grid">'
            '<div class="grid-item"><div class="data-item">内容卡片</div></div>'
            "</div></div>"
        ],
    )

    assert actions == []


def test_post_list_does_not_sign_anchor_without_registered_title_structure() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.test/",
        dom_chunks=[
            '<nav><a href="/category/new">New</a></nav>'
            '<a href="https://ads.example/archives/999">Ad</a>'
            '<main><a href="/archives/123456"><img alt="First post"></a></main>'
        ],
    )

    assert actions == []


@pytest.mark.parametrize(
    "selector",
    [
        page_detection_tools.H5_AI_PAGE_CARD_TITLE_SELECTOR,
        page_detection_tools.H5_POST_TITLE_SELECTOR,
        ".dynamic-box .title:visible",
        ".movie-list .media-item:visible .media-title:visible",
    ],
)
def test_all_new_detail_title_selectors_require_current_unobstructed_title(
    selector: str,
) -> None:
    assert page_detection_tools.h5_list_detail_selector_requires_visible_title(selector)


def test_post_list_does_not_treat_category_route_as_detail() -> None:
    actions = page_detection_tools._with_visible_nested_content_candidate(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.test/",
        dom_chunks=['<a href="/post/list">Post list</a>'],
    )

    assert actions == []


def test_post_list_adds_distinct_same_origin_business_navigation_branches() -> None:
    actions = page_detection_tools._with_visible_business_navigation_candidates(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.test/",
        dom_chunks=[
            '<nav><a href="/community">Community</a>'
            '<a href="/product">Product</a>'
            '<a href="https://ads.example/movie">External movie</a></nav>'
            '<main><a href="/category/new">Latest posts</a></main>'
            '<footer><a href="/movie">Movie</a>'
            '<a href="/videos">Duplicate video branch</a></footer>'
        ],
    )

    assert [action.target for action in actions] == [
        "https://example.test/community",
        "https://example.test/product",
        "https://example.test/category/new",
        "https://example.test/movie",
        "https://example.test/videos",
    ]
    assert [action.expected_next_page_type for action in actions] == [
        "post_list",
        "content_list",
        "post_list",
        "video_list",
        "video_list",
    ]
    assert all(action.action == "navigate" for action in actions)
    assert [action.destination_url for action in actions] == [
        "https://example.test/community",
        "https://example.test/product",
        "https://example.test/category/new",
        "https://example.test/movie",
        "https://example.test/videos",
    ]
    assert all(action.navigation_depth == 1 for action in actions)
    assert actions[1].target_keywords == [
        "business_navigation",
        "internal_navigation",
        "path:/product",
    ]


def test_business_navigation_keeps_distinct_paths_with_same_page_type() -> None:
    actions = page_detection_tools._with_visible_business_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.test/",
        dom_chunks=[
            '<nav><a href="/movie">影片</a>'
            '<a href="/videos">视频</a></nav>'
        ],
    )

    assert [action.target for action in actions] == [
        "https://example.test/movie",
        "https://example.test/videos",
    ]
    assert [action.expected_next_page_type for action in actions] == [
        "video_list",
        "video_list",
    ]


def test_profile_adds_rule_target_routes_from_visible_navigation_labels() -> None:
    actions = page_detection_tools._with_visible_business_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.test/mine",
        dom_chunks=[
            '<nav><a href="/leaderboard">排行榜</a>'
            '<a href="/publish">发布</a>'
            '<a href="/vip">支付</a>'
            '<a href="https://ads.example/ranking">外部榜单</a></nav>'
        ],
    )

    assert [action.target for action in actions] == ["https://example.test/publish"]
    assert [action.expected_next_page_type for action in actions] == ["post_publish"]


def test_home_does_not_add_vip_link_as_payment_navigation() -> None:
    actions = page_detection_tools._with_visible_business_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.test/",
        dom_chunks=['<nav><a href="/vip">立即开通</a></nav>'],
    )

    assert actions == []


def test_business_navigation_does_not_bypass_title_only_detail_entry() -> None:
    """详情页必须经无遮挡标题进入，通用同源链接不得旁路。"""
    actions = page_detection_tools._with_visible_business_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        page_url="https://example.test/",
        dom_chunks=[
            '<a href="/movie/detail/123">视频详情</a>'
            '<a href="/post/detail/123">内容详情</a>'
            '<a href="/post/settings">未知帖子子路由</a>'
        ],
    )

    assert actions == []


def test_stale_popup_state_is_refined_when_tabbar_is_visible_and_unobstructed() -> None:
    page_state = page_detection_tools._refined_page_state_after_obstruction_check(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<main class="home-feed">内容</main><div class="van-tabbar">'
            '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
            '<div class="van-tabbar-item">影片</div></div>'
        ],
    )

    assert page_state == "stable"


def test_popup_state_is_kept_while_visible_dialog_exists() -> None:
    page_state = page_detection_tools._refined_page_state_after_obstruction_check(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<div class="van-dialog" role="dialog">公告</div>'
            '<div class="van-tabbar">'
            '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
            '<div class="van-tabbar-item">影片</div></div>'
        ],
    )

    assert page_state == "popup"


def test_stable_state_is_refined_to_popup_while_visible_dialog_exists() -> None:
    page_state = page_detection_tools._refined_page_state_after_obstruction_check(
        page_type="video_list",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="van-overlay"></div><div class="van-dialog" role="dialog">'
            '<div class="van-dialog__content"><div class="btn">关闭</div></div>'
            "</div>"
        ],
    )

    assert page_state == "popup"


def test_dialog_accessible_close_control_replaces_ambiguous_close_text() -> None:
    selector = page_detection_tools._structural_obstruction_selector(
        '<div class="overlay"><section role="dialog" aria-modal="true">'
        "<p>这是可安全关闭的说明文字</p>"
        '<button id="ad-close" class="dialog-close" aria-label="关闭广告">×</button>'
        "</section></div>"
    )

    assert selector == page_detection_tools._DIALOG_ACCESSIBLE_CLOSE_SELECTOR
    assert '[aria-label*="关闭"]' in selector


def test_dialog_leaf_text_close_control_is_preferred_over_overlay() -> None:
    selector = page_detection_tools._structural_obstruction_selector(
        '<div class="van-overlay"></div>'
        '<div role="dialog" class="van-dialog">'
        '<div class="van-dialog__content"><div class="ad-list"><img></div>'
        '<div class="btn-box end"> 关闭 </div></div></div>'
    )

    assert selector == page_detection_tools._DIALOG_TEXT_AD_CLOSE_SELECTOR
    assert ".btn-box.end" in selector


def test_chunked_dom_reassembly_preserves_dialog_ancestry_at_tag_boundary() -> None:
    dom = (
        '<div class="van-overlay"></div>'
        '<div role="dialog" class="van-dialog">'
        '<div class="van-dialog__content">'
        '<div class="grid-main"><div class="grid">'
        '<div class="item">广告</div></div>'
        '<div class="btn"><img alt="关闭"></div>'
        "</div></div></div>"
    )
    boundary = dom.index('<div class="item">') + 1
    chunks = page_detection_tools._sanitize_and_chunk_dom(dom, chunk_size=boundary)

    assert chunks[0].endswith("<")
    assert chunks[1].startswith('div class="item"')
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="profile",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=chunks,
    )

    _assert_semantic_popup_action(actions[0], "dialog_overlay")
    assert page_detection_tools._structural_obstruction_selector(
        "".join(chunks)
    ) == page_detection_tools._structural_obstruction_selector(
        page_detection_tools._join_dom_chunks(chunks)
    )


def test_same_origin_anchor_gets_deterministic_navigation_depth() -> None:
    actions = page_detection_tools._with_internal_anchor_navigation_depth(
        page_url="https://example.com/index.html",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="内容详情",
                target_keywords=["内容详情"],
                risk_level="low",
                purpose="进入内容详情页",
                expected_next_page_type="content_detail",
            )
        ],
        dom_chunks=['<nav><a href="./detail.html">内容详情</a></nav>'],
    )

    assert len(actions) == 1
    assert actions[0].target == 'a[href="./detail.html"]:visible'
    assert actions[0].navigation_depth == 1
    assert "internal_navigation" in actions[0].target_keywords


def test_duplicate_responsive_anchors_to_same_path_are_one_navigation_target() -> None:
    actions = page_detection_tools._with_internal_anchor_navigation_depth(
        page_url="https://example.com/product",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="Community",
                target_keywords=["Community"],
                risk_level="low",
                purpose="Open community",
                expected_next_page_type="post_list",
            )
        ],
        dom_chunks=[
            '<nav class="mobile"><a href="/community">Community</a></nav>'
            '<nav class="desktop"><a href="/community">Community</a></nav>'
        ],
    )

    assert actions[0].target == 'a[href="/community"]:visible'
    assert actions[0].navigation_depth == 1
    assert "path:/community" in actions[0].target_keywords


def test_external_anchor_does_not_receive_navigation_authority() -> None:
    actions = page_detection_tools._with_internal_anchor_navigation_depth(
        page_url="https://example.com/index.html",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="外部帮助",
                target_keywords=["外部帮助"],
                risk_level="low",
                purpose="打开外部帮助",
            )
        ],
        dom_chunks=['<a href="https://outside.example.org/help">外部帮助</a>'],
    )

    assert actions[0].target == "外部帮助"
    assert actions[0].navigation_depth is None
    # 外链单击已获准入做安全取证（执行端自动开证据窗口并回位），
    # 定级 medium：前沿可低优先消费，但不授予业务导航深度。
    assert actions[0].risk_level == "medium"
    assert "external_navigation" in actions[0].target_keywords


def test_video_detail_with_player_gains_seek_probe_candidate() -> None:
    """视频详情页存在 <video> 时补充单条确定性拖动进度条探针。"""
    actions = page_detection_tools._with_video_playback_probe_candidate(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=['<div class="player"><video src="/a.m3u8"></video></div>'],
    )

    assert len(actions) == 1
    assert actions[0].action == "seek_media"
    assert actions[0].target == "播放进度条"
    assert actions[0].risk_level == "low"


def test_video_detail_seek_probe_precedes_return_candidate() -> None:
    """详情页必须先完成本地播放器探针，再暴露离开页面的返回动作。"""
    back = page_detection_tools.CandidateAction(
        action="back",
        target="browser_history",
        target_keywords=["nested_return"],
        risk_level="low",
        purpose="返回来源页",
        navigation_depth=1,
    )

    actions = page_detection_tools._with_video_playback_probe_candidate(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[back],
        dom_chunks=['<div class="player"><video src="/a.m3u8"></video></div>'],
    )

    assert [action.action for action in actions] == ["seek_media", "back"]
    assert actions[0].target == "播放进度条"


@pytest.mark.parametrize(
    ("page_type", "page_state", "dom"),
    [
        # 非视频详情页不补
        ("video_list", "stable", "<video></video>"),
        # 弹窗/倒计时遮挡态不补，先清障
        ("video_detail", "popup", "<video></video>"),
        ("video_detail", "countdown", "<video></video>"),
        ("video_detail", "playback_ended_paywall", "<video></video>"),
        # 页面没有真实播放器不补
        ("video_detail", "stable", "<div>no player</div>"),
    ],
)
def test_seek_probe_candidate_is_not_added_outside_playable_context(
    page_type: str, page_state: str, dom: str
) -> None:
    actions = page_detection_tools._with_video_playback_probe_candidate(
        page_type=page_type,
        page_state=page_state,
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert actions == []


def test_seek_probe_candidate_is_not_duplicated() -> None:
    existing = page_detection_tools.CandidateAction(
        action="seek_media",
        target="播放进度条",
        target_keywords=[],
        risk_level="low",
        purpose="模型已提议的拖动",
    )

    actions = page_detection_tools._with_video_playback_probe_candidate(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[existing],
        dom_chunks=["<video></video>"],
    )

    assert actions == [existing]


def test_logout_candidate_is_forced_to_high_risk() -> None:
    actions = page_detection_tools._with_deterministic_high_risk_actions(
        [
            page_detection_tools.CandidateAction(
                action="click",
                target="退出登录",
                target_keywords=["logout"],
                risk_level="low",
                purpose="测试退出功能",
            )
        ]
    )

    assert actions[0].risk_level == "high"


@pytest.mark.parametrize(
    "target",
    [
        "去绑定",
        "绑定手机号",
        "去找回",
        "找回账号",
        "找回密码",
        "Forgot password",
    ],
)
def test_sensitive_account_candidate_is_forced_to_high_risk(target: str) -> None:
    actions = page_detection_tools._with_deterministic_high_risk_actions(
        [
            page_detection_tools.CandidateAction(
                action="click",
                target=target,
                target_keywords=[target],
                risk_level="low",
                purpose=f"测试账号动作：{target}",
            )
        ]
    )

    assert actions[0].risk_level == "high"


def test_read_only_binding_status_candidate_remains_low_risk() -> None:
    actions = page_detection_tools._with_deterministic_high_risk_actions(
        [
            page_detection_tools.CandidateAction(
                action="click",
                target="查看绑定状态",
                target_keywords=["绑定状态"],
                risk_level="low",
                purpose="查看账号绑定状态说明",
            )
        ]
    )

    assert actions[0].risk_level == "low"


def test_activity_drops_same_page_signin_side_effect_but_keeps_profile_navigation() -> None:
    activity_signin = page_detection_tools.CandidateAction(
        action="click",
        target="立即签到",
        target_keywords=["签到"],
        risk_level="low",
        purpose="执行每日签到",
        expected_next_page_type="activity",
    )
    profile_navigation = page_detection_tools.CandidateAction(
        action="click",
        target="去签到",
        target_keywords=["activity"],
        risk_level="low",
        purpose="进入签到活动页面",
        expected_next_page_type="activity",
    )

    assert (
        page_detection_tools._without_read_only_side_effect_candidates(
            page_type="activity",
            candidate_actions=[activity_signin],
        )
        == []
    )
    assert page_detection_tools._without_read_only_side_effect_candidates(
        page_type="profile",
        candidate_actions=[profile_navigation],
    ) == [profile_navigation]


def test_accessible_close_selector_is_not_misread_as_submit_action() -> None:
    actions = page_detection_tools._with_deterministic_high_risk_actions(
        [
            page_detection_tools.CandidateAction(
                action="click",
                target=page_detection_tools._DIALOG_ACCESSIBLE_CLOSE_SELECTOR,
                target_keywords=["close_control"],
                risk_level="low",
                purpose="关闭当前遮挡弹窗并重新采集",
            )
        ]
    )

    assert actions[0].risk_level == "low"


@pytest.mark.parametrize(
    "page_state",
    ["stable", "loaded", "detail", "playing", "playback_ended_paywall"],
)
def test_detail_page_adds_browser_history_return_action(page_state: str) -> None:
    actions = page_detection_tools._with_detail_return_candidate(
        page_type="video_detail",
        page_state=page_state,
        candidate_actions=[],
    )

    assert len(actions) == 1
    assert actions[0].action == "back"
    assert actions[0].target == "browser_history"
    assert actions[0].target_keywords == ["nested_return"]
    assert actions[0].navigation_depth == 1


def test_detail_page_replaces_model_back_with_canonical_history_return() -> None:
    model_back = page_detection_tools.CandidateAction(
        action="back",
        target="返回",
        target_keywords=["返回", "后退"],
        risk_level="low",
        purpose="从视频详情返回来源列表以继续其他分支探索",
        expected_next_page_type="video_list",
        navigation_depth=None,
    )

    actions = page_detection_tools._with_detail_return_candidate(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[model_back],
    )

    assert len(actions) == 1
    assert actions[0].target == "browser_history"
    assert actions[0].target_keywords == ["nested_return"]
    assert actions[0].navigation_depth == 1


@pytest.mark.parametrize("page_type", ["content_detail", "video_detail"])
def test_detail_capture_discards_all_business_actions_and_exits_immediately(
    page_type: str,
) -> None:
    """详情截图与 DOM 已由当前捕获完成，检测后只能立即返回来源列表。"""
    actions = page_detection_tools._with_detail_return_candidate(
        page_type=page_type,
        page_state="stable",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="seek_media",
                target="播放进度条",
                target_keywords=["video_playback_probe"],
                risk_level="low",
                purpose="继续探索详情页播放器",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="相关推荐",
                target_keywords=["related_content"],
                risk_level="low",
                purpose="继续探索详情页推荐内容",
            ),
        ],
    )

    assert [action.action for action in actions] == ["back"]
    assert actions[0].target == "browser_history"
    assert actions[0].target_keywords == ["nested_return"]


@pytest.mark.parametrize("page_type", ["content_detail", "video_detail"])
def test_detail_popup_keeps_trusted_dismissal_before_history_exit(
    page_type: str,
) -> None:
    """详情弹窗必须先循环关闭，稳定采集后才能退出详情。"""
    dismissal = page_detection_tools.CandidateAction(
        action="click",
        target="关闭当前弹窗",
        target_keywords=["popup_dismissal", "close_control"],
        risk_level="low",
        purpose="关闭当前遮挡弹窗并重新采集",
    )

    actions = page_detection_tools._with_detail_return_candidate(
        page_type=page_type,
        page_state="popup",
        candidate_actions=[dismissal],
    )

    assert actions == [dismissal]


def test_detail_popup_without_trusted_dismissal_uses_canonical_history_escape() -> None:
    actions = page_detection_tools._with_detail_return_candidate(
        page_type="video_detail",
        page_state="popup",
        candidate_actions=[],
    )

    assert len(actions) == 1
    assert actions[0].action == "back"
    assert actions[0].target == "browser_history"
    assert actions[0].navigation_depth == 1


def test_entitlement_locked_detail_keeps_only_verified_membership_collection() -> None:
    membership_collection = page_detection_tools.CandidateAction(
        action="scroll",
        target="浏览全部会员卡",
        target_keywords=["payment_horizontal_collection", "read_only_collection"],
        risk_level="low",
        purpose="横向浏览当前详情页权益层全部会员套餐",
    )
    model_action = page_detection_tools.CandidateAction(
        action="click",
        target="立即开通",
        target_keywords=["membership_purchase"],
        risk_level="high",
        purpose="开通会员",
    )
    model_back = page_detection_tools.CandidateAction(
        action="back",
        target="返回",
        target_keywords=["返回"],
        risk_level="low",
        purpose="返回来源页面",
    )

    actions = page_detection_tools._with_detail_return_candidate(
        page_type="video_detail",
        page_state="entitlement_locked",
        candidate_actions=[model_action, membership_collection, model_back],
    )

    assert actions == [membership_collection]


@pytest.mark.parametrize("page_type", ["content_detail", "video_detail"])
def test_loading_detail_page_returns_through_browser_history(page_type: str) -> None:
    actions = page_detection_tools._with_detail_return_candidate(
        page_type=page_type,
        page_state="loading",
        candidate_actions=[],
    )

    assert len(actions) == 1
    assert actions[0].action == "back"
    assert actions[0].target == "browser_history"
    assert actions[0].target_keywords == ["nested_return"]


@pytest.mark.parametrize("page_type", ["activity", "settings", "payment"])
def test_profile_child_without_live_bottom_navigation_returns_through_history(
    page_type: str,
) -> None:
    stale_bottom_navigation = page_detection_tools.CandidateAction(
        action="click",
        target="div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(1):visible",
        target_keywords=["bottom_navigation", "导航站", "home"],
        risk_level="low",
        purpose="探索稳定页面中可见的底部导航业务分支",
        expected_next_page_type="home",
        navigation_depth=1,
    )

    actions = page_detection_tools._with_detail_return_candidate(
        page_type=page_type,
        page_state="stable",
        candidate_actions=[stale_bottom_navigation],
        has_visible_bottom_navigation=False,
    )

    assert len(actions) == 1
    assert actions[0].action == "back"
    assert actions[0].target == "browser_history"
    assert actions[0].target_keywords == ["nested_return"]


def test_profile_child_keeps_confirmed_live_bottom_navigation() -> None:
    bottom_navigation = page_detection_tools.CandidateAction(
        action="click",
        target="div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(1):visible",
        target_keywords=["bottom_navigation", "导航站", "home"],
        risk_level="low",
        purpose="探索稳定页面中可见的底部导航业务分支",
        expected_next_page_type="home",
        navigation_depth=1,
    )

    actions = page_detection_tools._with_detail_return_candidate(
        page_type="activity",
        page_state="stable",
        candidate_actions=[bottom_navigation],
        has_visible_bottom_navigation=True,
    )

    assert actions == [bottom_navigation]


def test_flutter_settings_page_back_adds_structure_capability() -> None:
    dom = """
    <flt-semantics aria-label="设置" flt-tappable role="group"
      style="position:absolute;width:390px;height:844px;pointer-events:none">
      <flt-semantics flt-tappable
        style="position:absolute;width:67.32px;height:35.36px;
        transform:matrix(1,0,0,1,0,5.2);z-index:3"></flt-semantics>
      <flt-semantics role="text"
        style="position:absolute;width:390px;height:48px;
        transform:matrix(1,0,0,1,0,45.76);z-index:1">头像</flt-semantics>
    </flt-semantics>
    """

    actions = page_detection_tools._with_detail_return_candidate(
        page_type="settings",
        page_state="stable",
        candidate_actions=[],
        has_visible_bottom_navigation=False,
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    assert actions[0].action == "back"
    assert actions[0].target == "browser_history"
    assert actions[0].target_keywords == [
        "nested_return",
        "flutter_settings_page_back",
        "evidence:structure",
    ]


def test_flutter_profile_favorites_page_adds_second_back_capability() -> None:
    """设置返回若落到“我的收藏”，必须继续页面内 back 回个人中心。"""
    dom = """
    <flt-semantics aria-label="我的收藏" flt-tappable role="group"
      style="position:absolute;width:390px;height:844px;pointer-events:none">
      <flt-semantics flt-tappable
        style="position:absolute;width:67.32px;height:35.36px;
        transform:matrix(1,0,0,1,0,5.2);z-index:4"></flt-semantics>
      <flt-semantics role="text"
        style="position:absolute;width:54px;height:48px;
        transform:matrix(1,0,0,1,17,45.76);z-index:1">雅间</flt-semantics>
    </flt-semantics>
    """

    actions = page_detection_tools._with_detail_return_candidate(
        page_type="video_list",
        page_state="empty_state",
        candidate_actions=[],
        has_visible_bottom_navigation=False,
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    assert actions[0].action == "back"
    assert actions[0].target == "browser_history"
    assert actions[0].expected_next_page_type == "profile"
    assert actions[0].target_keywords == [
        "nested_return",
        "flutter_profile_favorites_page_back",
        "evidence:structure",
    ]


def test_flutter_profile_favorites_content_list_adds_second_back_capability() -> None:
    """收藏空页被模型归类为内容列表时仍须按强结构返回个人中心。"""
    dom = """
    <flt-semantics aria-label="我的收藏" flt-tappable role="group"
      style="position:absolute;width:390px;height:844px;pointer-events:none">
      <flt-semantics flt-tappable
        style="position:absolute;width:67.32px;height:35.36px;
        transform:matrix(1,0,0,1,0,5.2);z-index:4"></flt-semantics>
    </flt-semantics>
    """

    actions = page_detection_tools._with_detail_return_candidate(
        page_type="content_list",
        page_state="empty",
        candidate_actions=[],
        has_visible_bottom_navigation=False,
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    assert actions[0].action == "back"
    assert actions[0].expected_next_page_type == "profile"


def test_flutter_profile_favorites_back_survives_model_dom_sanitization() -> None:
    """“我的收藏”返回结构必须在原始 DOM 边界提取后再签发。"""
    dom = """
    <flt-semantics aria-label="我的收藏" flt-tappable role="group"
      style="position:absolute;width:390px;height:844px;pointer-events:none">
      <flt-semantics flt-tappable
        style="position:absolute;width:67.32px;height:35.36px;
        transform:matrix(1,0,0,1,0,5.2);z-index:4"></flt-semantics>
    </flt-semantics>
    """
    sanitized_chunks = page_detection_tools._sanitize_and_chunk_dom(dom)

    assert page_detection_tools._has_flutter_profile_favorites_page_back_structure(dom)
    assert not page_detection_tools._has_flutter_profile_favorites_page_back_structure(
        "".join(sanitized_chunks)
    )
    actions = page_detection_tools._with_detail_return_candidate(
        page_type="video_list",
        page_state="empty_state",
        candidate_actions=[],
        dom_chunks=sanitized_chunks,
        has_verified_profile_favorites_page_back_structure=True,
    )

    assert actions[0].expected_next_page_type == "profile"
    assert "flutter_profile_favorites_page_back" in actions[0].target_keywords


async def test_flutter_settings_page_back_survives_model_dom_sanitization() -> None:
    """原始 DOM 的可信返回结构不能因模型视图清洗而丢失。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": b"""
            <html><body><flt-semantics
              aria-label="\xe8\xae\xbe\xe7\xbd\xae" flt-tappable role="group"
              style="position:absolute;width:390px;height:844px;pointer-events:none">
              <flt-semantics flt-tappable
                style="position:absolute;width:67.32px;height:35.36px;
                transform:matrix(1,0,0,1,0,5.2);z-index:3"></flt-semantics>
              <flt-semantics role="text"
                style="position:absolute;width:390px;height:48px;
                transform:matrix(1,0,0,1,0,45.76);z-index:1">\xe5\xa4\xb4\xe5\x83\x8f</flt-semantics>
            </flt-semantics></body></html>
            """,
            "shot-1": PNG_BYTES,
        }
    )
    client = FakeChatClient(_recognized_output("settings"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    back_action = next(
        action for action in result["candidate_actions"] if action["action"] == "back"
    )
    assert back_action["target_keywords"] == [
        "nested_return",
        "flutter_settings_page_back",
        "evidence:structure",
    ]


async def test_popup_state_discards_nonexistent_target_without_explicit_close_control() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b'<html><body><div role="dialog" class="van-dialog">'
                b'<div class="btn"><img alt=""></div></div></body></html>'
            ),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="text=Close",
                    target_keywords=["close"],
                    risk_level="low",
                    purpose="关闭弹窗",
                )
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["candidate_actions"] == []


async def test_popup_state_replaces_hidden_text_target_with_visible_dialog_control() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div role="dialog" class="van-dialog">'
                '<div class="ad-item" style="display:none">'
                '<div class="app-btn">我知道了</div></div>'
                '<div class="ad-item"><span class="btn-close"></span></div>'
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="text=我知道了",
                    target_keywords=["我知道了"],
                    risk_level="low",
                    purpose="关闭弹窗",
                )
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_generic_close")


async def test_popup_state_replaces_visible_non_dismissal_target_when_purpose_is_close() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div role="dialog" class="van-dialog">'
                '<span class="btn-close"></span>'
                "<button>保存二维码</button>"
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="保存二维码",
                    target_keywords=["保存二维码"],
                    risk_level="low",
                    purpose="关闭登录提示弹窗",
                )
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_generic_close")


async def test_ambiguous_page_keeps_structurally_proven_popup_dismissal() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b'<html><body><div class="van-overlay"></div>'
                b'<div role="dialog" class="van-popup van-popup--center">'
                b'<div class="ads-image"></div>'
                b'<div class="close_btn"><i class="van-icon van-icon-close"></i></div>'
                b"</div></body></html>"
            ),
            "shot-1": PNG_BYTES,
        }
    )
    output = _ambiguous_output().model_copy(update={"page_state": "popup", "candidate_actions": []})

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_generic_close")


async def test_popup_acknowledgement_uses_visible_dialog_scoped_control() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div role="dialog" class="van-dialog">'
                '<div class="ad-item" style="display:none">'
                '<div class="app-btn">我知道了</div></div>'
                '<div class="ad-item"><div class="app-btn">我知道了</div></div>'
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="我知道了",
                    target_keywords=["我知道了"],
                    risk_level="low",
                    purpose="关闭当前广告弹窗",
                )
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_acknowledgement")


def test_dialog_acknowledgement_precedes_generic_close_control() -> None:
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="video_list",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[
            '<html><body><div class="van-overlay"></div>'
            '<div role="dialog" class="van-dialog">'
            '<div class="van-dialog__content">'
            '<div class="ad-list">'
            '<div class="ad-item" style="display:none">'
            '<span class="btn-close"></span></div>'
            '<div class="ad-item"><span class="btn-close"></span></div>'
            '</div><div class="content"><p>合作联系</p></div>'
            '<div class="a-btn-list"><div class="app-btn">我知道了</div></div>'
            "</div></div></body></html>"
        ],
    )

    _assert_semantic_popup_action(actions[0], "dialog_acknowledgement")


async def test_qr_dialog_uses_visible_overlay_instead_of_hidden_old_close_control() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="van-overlay"></div>'
                '<div role="dialog" class="qr-dialog van-dialog">'
                "<button>保存二维码</button></div>"
                '<div role="dialog" style="display:none">'
                '<span class="btn-close"></span></div>'
                "</body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="保存二维码",
                    target_keywords=["保存二维码"],
                    risk_level="low",
                    purpose="关闭登录提示弹窗",
                )
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_overlay")


async def test_dialog_without_close_control_uses_visible_overlay_recovery() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="van-overlay"></div>'
                '<div role="dialog" class="van-dialog"><div class="grid-main">广告</div></div>'
                "</body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(update={"page_state": "popup"})

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_overlay")


async def test_small_fallthrough_notification_does_not_block_business_navigation() -> None:
    """小型短命提示不应制造“有遮挡但无安全候选”的空 Frontier。"""
    dom = _artifact("dom-notification", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-notification", "screenshot", content_type="image/png")
    snapshot = _artifact(
        "snapshot-notification",
        "ui_snapshot",
        content_type="application/json",
    )
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, snapshot))
    reader = FakeArtifactReader(
        {
            "dom-notification": (
                '<html><body><main class="home">首页内容</main>'
                '<div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
                '<div class="van-tabbar-item">视频</div>'
                '</div><div role="dialog" class="van-popup van-toast">网络错误</div>'
                "</body></html>"
            ).encode(),
            "shot-notification": PNG_BYTES,
            "snapshot-notification": json.dumps(
                {
                    "viewport": {"width": 390, "height": 844},
                    "elements": [
                        {
                            "role": "dialog",
                            "text": "网络错误",
                            "class_tokens": ["van-popup", "van-toast"],
                            "rect": {"x": 135, "y": 404, "width": 120, "height": 36},
                            "style": {
                                "display": "flex",
                                "visibility": "visible",
                                "opacity": "1",
                            },
                            "hit_test": {"center_hits_target": False},
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode(),
        }
    )
    output = _recognized_output("home")

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_state"] == "stable"
    assert "deterministic_nonblocking_notification_refinement" in result["risk_flags"]
    assert result["obstruction_facts"] == {
        "schema_version": "h5-obstruction-fact.v1",
        "kind": "nonblocking_notification",
        "blocking": False,
        "notification_count": 1,
        "blocking_element_count": 0,
        "full_viewport_gate_count": 0,
        "snapshot_evidence_status": "ready",
        "modal_census_complete": False,
        "modal_census_count": None,
        "trusted_safe_action_available": False,
        "obstruction_without_safe_action": False,
    }
    assert any(
        action["expected_next_page_type"] == "video_list" for action in result["candidate_actions"]
    )


@pytest.mark.parametrize("census_state", [
    "clear", "missing", "incomplete", "modal", "covered", "collection_error", "capture_degraded",
])
async def test_h5_rendered_navigation_not_blocked_by_floating_ad(census_state) -> None:
    dom = _artifact("dom-float", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-float", "screenshot", content_type="image/png")
    snapshot_artifact = _artifact("snapshot-float", "ui_snapshot", content_type="application/json")
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "h5_modal_census_selectors": [],
        "h5_modal_census_truncated": False,
        "truncated": True,
        "truncated_reason": "serialized_size_limit",
        "elements": [
            {
                "selector": f"#nav-{index}",
                "parent_selector": "#navigation",
                "tag": "div",
                "text": label,
                "class_tokens": ["van-tabbar-item", *(["active"] if index == 0 else [])],
                "visible": True,
                "interactive": True,
                "fixed_or_sticky_ancestor": True,
                "rect": {"x": index * 65, "y": 794, "width": 65, "height": 50},
                "hit_test": {"center_hits_target": census_state != "covered"},
            }
            for index, label in enumerate(["首页", "视频", "栏目甲", "栏目乙", "社区", "我的"])
        ],
    }
    if census_state == "missing":
        snapshot.pop("h5_modal_census_truncated")
    elif census_state == "incomplete":
        snapshot["h5_modal_census_truncated"] = True
    elif census_state in {"collection_error", "capture_degraded"}:
        snapshot[census_state] = True
    elif census_state == "modal":
        snapshot["h5_modal_census_selectors"] = ["#dialog"]
        snapshot["elements"].append({
            "selector": "#dialog", "role": "dialog", "aria_modal": True,
            "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
            "hit_test": {"center_hits_target": True},
        })
    reader = FakeArtifactReader({
        "dom-float": (
            '<html><body><main>首页业务内容</main><div class="van-tabbar">'
            '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
            '<div class="van-tabbar-item">视频</div></div>'
            '<div class="home-suspend"><span class="btn-close">关闭</span>'
            '<img src="banner.png"></div></body></html>'
        ).encode(),
        "shot-float": PNG_BYTES,
        "snapshot-float": json.dumps(snapshot, ensure_ascii=False).encode(),
    })
    output = _recognized_output("home").model_copy(update={"page_state": "popup"})
    with bind_agent_run_context(AgentRunContext(
        task_id="11111111-1111-1111-1111-111111111111", agent_run_id="run-float",
    )):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot, snapshot_artifact)),
            artifact_reader=reader, chat_client=FakeChatClient(output),
            skill_text="# page-detection", catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )
    facts = result["obstruction_facts"]
    assert facts["snapshot_evidence_status"] == "incomplete"
    assert facts["modal_census_complete"] is (census_state in {"clear", "modal", "covered"})
    if census_state in {"collection_error", "capture_degraded"}:
        return
    if census_state == "clear":
        assert result["page_state"] == "stable"
        assert "deterministic_unobstructed_navigation_refinement" in result["risk_flags"]
        assert any(
            action["expected_next_page_type"] == "video_list"
            for action in result["candidate_actions"]
        )
        assert not any(
            action.get("obstruction_capability") for action in result["candidate_actions"]
        )
    else:
        assert result["page_state"] == "popup"
        assert "deterministic_unobstructed_navigation_refinement" not in result["risk_flags"]


async def test_full_viewport_warning_gate_cannot_publish_underlying_navigation() -> None:
    dom = _artifact("dom-warning-gate", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-warning-gate", "screenshot", content_type="image/png")
    snapshot = _artifact(
        "snapshot-warning-gate",
        "ui_snapshot",
        content_type="application/json",
    )
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, snapshot))
    reader = FakeArtifactReader(
        {
            "dom-warning-gate": (
                '<html><body><main class="home">首页内容</main>'
                '<div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
                '<div class="van-tabbar-item">视频</div>'
                '</div><div class="warning-box"><img src="/gate.png"></div>'
                "</body></html>"
            ).encode(),
            "shot-warning-gate": PNG_BYTES,
            "snapshot-warning-gate": json.dumps(
                {
                    "viewport": {"width": 390, "height": 844},
                    "elements": [
                        {
                            "tag": "img",
                            "class_tokens": [],
                            "ancestor_class_tokens": ["warning-box", "home"],
                            "rect": {"x": 0, "y": 0, "width": 390, "height": 788},
                            "effective_z_index": 1000,
                            "style": {
                                "display": "block",
                                "visibility": "visible",
                                "opacity": "1",
                            },
                            "hit_test": {
                                "center_hits_target": True,
                                "pointer_interception": True,
                                "topmost": True,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode(),
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_state"] == "entitlement_locked"
    assert result["candidate_actions"] == []
    assert "deterministic_full_viewport_gate_lock" in result["risk_flags"]
    assert result["obstruction_facts"]["full_viewport_gate_count"] == 1
    assert result["obstruction_facts"]["obstruction_without_safe_action"] is True


async def test_unknown_bottom_navigation_locked_arrival_does_not_reuse_home_identity() -> None:
    """未知栏目已验签到达但被整屏门禁遮挡时，不能沿用共享首页壳的页型。"""

    dom = _artifact("dom-unknown-locked", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-unknown-locked", "screenshot", content_type="image/png")
    snapshot = _artifact(
        "snapshot-unknown-locked",
        "ui_snapshot",
        content_type="application/json",
    )
    capture_id = "22222222-2222-2222-2222-222222222222"
    arrival_context = {
        "action_status": "completed",
        "action_type": "click",
        "semantic_target": "internal:flutter_bottom_navigation:nth-of-type(4)",
        "risk_level": "low",
        "parameters": {
            "target_mode": "bottom_navigation",
            "navigation_family": "flutter_semantics",
            "navigation_index": 4,
            "navigation_label": "神秘专区",
            "candidate_id": "h5-candidate:" + "a" * 64,
            "frontier_snapshot_id": "h5-task-frontier:" + "b" * 64,
        },
        "result_payload": {
            "execution_outcome": "succeeded",
            "result_capture_id": capture_id,
            "state_diff": {
                "before_location": "https://example.test/home",
                "after_location": "https://example.test/locked-zone",
                "transition_kind": "navigation",
            },
        },
        "navigation_effect_verified": True,
        "requested_source_page_type": "home",
        "requested_source_page_state": "stable",
        "requested_source_url": "https://example.test/home",
    }
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            snapshot,
            page_url="https://example.test/locked-zone",
            capture_metadata={
                "capture_role": "after_action",
                "_arrival_action_context": arrival_context,
            },
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-unknown-locked": (
                '<html><body><main class="home">共享业务壳</main>'
                '<div class="warning-box"><img src="/gate.png"></div>'
                "</body></html>"
            ).encode(),
            "shot-unknown-locked": PNG_BYTES,
            "snapshot-unknown-locked": json.dumps(
                {
                    "viewport": {"width": 390, "height": 844},
                    "elements": [
                        {
                            "tag": "img",
                            "class_tokens": [],
                            "ancestor_class_tokens": ["warning-box", "home"],
                            "rect": {"x": 0, "y": 0, "width": 390, "height": 788},
                            "effective_z_index": 1000,
                            "style": {
                                "display": "block",
                                "visibility": "visible",
                                "opacity": "1",
                            },
                            "hit_test": {
                                "center_hits_target": True,
                                "pointer_interception": True,
                                "topmost": True,
                            },
                        }
                    ],
                },
                ensure_ascii=False,
            ).encode(),
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-unknown-locked",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id=capture_id,
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["page_state"] == "entitlement_locked"
    assert result["rule_matching_allowed"] is False
    assert "verified_unknown_navigation_locked_identity" in result["risk_flags"]


async def test_payment_page_information_modal_uses_generic_safe_acknowledgement() -> None:
    """页面类型不得全局屏蔽信息公告，实际风险只由顶层实时事实决定。"""

    dom = _artifact("dom-payment-modal", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-payment-modal", "screenshot", content_type="image/png")
    snapshot = _artifact(
        "snapshot-payment-modal",
        "ui_snapshot",
        content_type="application/json",
    )
    ui_snapshot = {
        "schema_version": "ui-snapshot.v2",
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#confirm-payment",
                "tag": "button",
                "role": "button",
                "text": "确认充值",
                "interactive": True,
                "rect": {"x": 20, "y": 740, "width": 350, "height": 48},
                "hit_test": {
                    "center_target": "#modal-root",
                    "center_hits_target": False,
                },
            },
            {
                "selector": "#modal-root",
                "tag": "div",
                "text": "",
                "interactive": True,
                "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                "hit_test": {
                    "center_target": "#modal-copy",
                    "center_hits_target": False,
                },
            },
            {
                "selector": "#modal-copy",
                "tag": "div",
                "role": "text",
                "text": "版本更新公告，更多内容持续上线",
                "interactive": False,
                "rect": {"x": 50, "y": 280, "width": 290, "height": 220},
                "hit_test": {
                    "center_target": "#modal-copy",
                    "center_hits_target": True,
                },
            },
            {
                "selector": "#modal-ack",
                "tag": "button",
                "role": "button",
                "text": "确认",
                "interactive": True,
                "rect": {"x": 135, "y": 560, "width": 120, "height": 40},
                "hit_test": {
                    "center_target": "#modal-ack",
                    "center_hits_target": True,
                },
            },
        ],
    }
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, snapshot))
    reader = FakeArtifactReader(
        {
            "dom-payment-modal": (
                "<html><body><main><h1>开通VIP</h1><button>确认充值</button>"
                '<div id="modal-root"><p>版本更新公告，更多内容持续上线</p>'
                '<button id="modal-ack">确认</button></div></main></body></html>'
            ).encode(),
            "shot-payment-modal": PNG_BYTES,
            "snapshot-payment-modal": json.dumps(
                ui_snapshot,
                ensure_ascii=False,
            ).encode(),
        }
    )
    output = _recognized_output("payment").model_copy(update={"page_state": "popup"})

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    _assert_semantic_popup_action(
        result["candidate_actions"][0],
        "modal_local_safe_acknowledgement",
    )
    assert result["obstruction_facts"]["trusted_safe_action_available"] is True
    assert result["obstruction_facts"]["obstruction_without_safe_action"] is False


def test_page_detection_cache_key_changes_with_ui_snapshot_hit_test() -> None:
    """DOM 与截图不变时，实时命中事实变化也必须使检测缓存失效。"""

    common = {
        "task_id": "task-1",
        "page_url": "https://example.test",
        "dom_chunks": ["<main>same</main>"],
        "image_payloads": [("shot", PNG_BYTES, "image/png")],
        "skill_digest": "skill",
        "catalog_version": "catalog",
    }
    blocking = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#toast",
                "hit_test": {"center_hits_target": True},
            }
        ],
    }
    fallthrough = json.loads(json.dumps(blocking))
    fallthrough["elements"][0]["hit_test"]["center_hits_target"] = False

    assert page_detection_tools._page_detection_cache_key(
        **common,
        ui_snapshot=blocking,
    ) != page_detection_tools._page_detection_cache_key(
        **common,
        ui_snapshot=fallthrough,
    )


async def test_uncancellable_unlock_gate_is_skipped_instead_of_popup_dismissal() -> None:
    """图片型“立即解锁”权益层没有关闭能力时不得把 overlay 当关闭按钮。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><main class="video-list">'
                '<div class="video-card">视频内容</div>'
                '<div class="van-overlay" role="button" tabindex="0" '
                'style="z-index: 2014;"></div>'
                '<div role="dialog" tabindex="0" '
                'class="van-popup van-popup--center dark-waring-popup" '
                'style="z-index: 2014;">'
                '<div class="dark-mask-click"><img src="/unlock.png"></div>'
                "</div></main></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("video_list").model_copy(
        update={
            "page_state": "popup",
            # 图片文字可能在单帧模型输出中缺失；固定结构仍必须确定性识别。
            "risk_flags": [],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "video_list"
    assert result["page_state"] == "entitlement_locked"
    assert result["candidate_actions"] == []
    assert "deterministic_uncancellable_entitlement_gate" in result["risk_flags"]


async def test_limited_time_offer_locks_video_detail_without_payment_refinement() -> None:
    """非 profile 来源的限时权益层不得计为 payment，也不得当普通弹窗关闭。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            page_url="https://dvgsyzqus02v5.cloudfront.net/movieDetails?vid=1",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><main class="video-detail">'
                '<div class="newUserBg paymentPlayPageBg"></div>'
                '<div class="van-overlay" role="button" tabindex="0" '
                'style="z-index: 2001;"></div>'
                '<div role="dialog" tabindex="0" '
                'class="van-popup van-popup--round van-popup--bottom '
                'limited-time-offer-popup-wrapper" style="z-index: 2001;">'
                '<div class="limitedTimeOfferBox">'
                '<div class="title">新人特惠</div>'
                '<div class="price_special">限时特价 ¥100</div>'
                '<div class="privilegePanel"><div>更多权益</div>'
                "<div>VIP影片</div><div>会员免广告</div></div>"
                '<div class="recharge-box">¥100/立即开通</div>'
                "</div></div></main></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("video_detail").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="关闭当前弹窗",
                    target_keywords=["popup_dismissal"],
                    risk_level="low",
                    purpose="关闭当前遮挡弹窗",
                    expected_next_page_type=None,
                )
            ],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "video_detail"
    assert result["page_state"] == "entitlement_locked"
    assert "deterministic_uncancellable_entitlement_gate" in result["risk_flags"]
    assert all(
        "popup_dismissal" not in action.get("target_keywords", ())
        for action in result["candidate_actions"]
    )
    assert result["candidate_actions"] == []


@pytest.mark.parametrize(
    ("page_type", "page_state"),
    [
        ("home", "stable"),
        ("home", "feed"),
        ("content_list", "normal"),
        ("content_list", "loaded"),
    ],
)
async def test_unobstructed_business_page_adds_non_active_visible_tabbar_actions(
    page_type: str,
    page_state: str,
) -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">热门</div>'
                '<div class="van-tabbar-item">影片</div>'
                '<div class="van-tabbar-item">社区</div>'
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output(page_type).model_copy(update={"page_state": page_state})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert [item["target"] for item in result["candidate_actions"]] == [
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible",
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(3):visible",
    ]
    assert all(item["navigation_depth"] == 1 for item in result["candidate_actions"])


async def test_h5_profile_page_tabs_add_deterministic_navigation_candidates() -> None:
    dom = _artifact("dom-tabs", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-tabs", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-tabs": (
                b'<html><body><main class="content-list">'
                b'<div class="van-tabs">'
                b'<div class="van-tab van-tab--active">home</div>'
                b'<div class="van-tab">video</div>'
                b'<div class="van-tab">top</div>'
                b"</div></main></body></html>"
            ),
            "shot-tabs": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("content_list")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert [item["target"] for item in result["candidate_actions"]] == [
        ".van-tabs:visible .van-tab:nth-of-type(2):visible",
        ".van-tabs:visible .van-tab:nth-of-type(3):visible",
    ]
    assert [item["expected_next_page_type"] for item in result["candidate_actions"]] == [
        "video_list",
        "home",
    ]
    assert all(
        "page_tab_navigation" in item["target_keywords"] for item in result["candidate_actions"]
    )
    assert result["candidate_actions"][1]["target_keywords"][-1] == "same_surface_variant"
    assert result["candidate_actions"][1]["navigation_knowledge_source"] == "contextual"


async def test_active_home_page_tab_refines_model_video_list_to_home() -> None:
    dom = _artifact("dom-home-tab", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-home-tab", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-home-tab": (
                '<html><body><main class="movie-page">'
                '<div class="van-tabs">'
                '<div class="van-tab van-tab--active"><span>\u63a8\u8350</span></div>'
                '<div class="van-tab"><span>\u5f71\u7247</span></div>'
                "</div>"
                '<div class="movie-list"><div class="media-item">\u5185\u5bb9</div></div>'
                "</main></body></html>"
            ).encode(),
            "shot-home-tab": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output("video_list").model_copy(update={"page_state": "stable"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "home"


async def test_bottom_home_tab_candidate_carries_expected_page_type() -> None:
    dom = _artifact("dom-bottom-home", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-bottom-home", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-bottom-home": (
                '<html><body><main class="video-list">'
                '<div class="media-item">\u5185\u5bb9</div>'
                '<div class="van-tabbar">'
                '<div class="van-tabbar-item">\u63a8\u8350</div>'
                '<div class="van-tabbar-item van-tabbar-item--active">\u89c6\u9891</div>'
                "</div></main></body></html>"
            ).encode(),
            "shot-bottom-home": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output("video_list").model_copy(update={"page_state": "stable"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    home_action = next(
        item
        for item in result["candidate_actions"]
        if item["target"] == "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(1):visible"
    )
    assert home_action["expected_next_page_type"] == "home"


def test_current_page_tabbar_alias_is_not_requeued_without_active_class() -> None:
    actions = page_detection_tools._with_visible_tabbar_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="van-tabbar">'
            '<div class="van-tabbar-item">导航站</div>'
            '<div class="van-tabbar-item">视频</div>'
            "</div>"
        ],
    )

    assert [item.expected_next_page_type for item in actions] == ["video_list"]


def test_distinct_same_type_tabbar_branches_are_kept_without_active_class() -> None:
    """多个同页型栏目不能因为缺少激活类名被一起当成当前项。"""

    actions = page_detection_tools._with_visible_tabbar_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<div class="van-tabbar">'
            '<div class="van-tabbar-item">推荐</div>'
            '<div class="van-tabbar-item">二次元</div>'
            '<div class="van-tabbar-item">视频</div>'
            "</div>"
        ],
    )

    assert [item.target_keywords[1] for item in actions] == ["推荐", "二次元", "视频"]
    assert [item.expected_next_page_type for item in actions] == [
        "home",
        "home",
        "video_list",
    ]


async def test_91_site_tabbar_candidates_carry_expected_page_types() -> None:
    dom = _artifact("dom-91-tabbar", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-91-tabbar", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-91-tabbar": (
                '<html><body><main class="video-list">'
                '<div class="media-item">内容</div>'
                '<div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">91看片</div>'
                '<div class="van-tabbar-item">91娱乐</div>'
                '<div class="van-tabbar-item">91私库</div>'
                "</div></main></body></html>"
            ).encode(),
            "shot-91-tabbar": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output("video_list").model_copy(update={"page_state": "stable"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    actions_by_target = {item["target"]: item for item in result["candidate_actions"]}
    entertainment_action = actions_by_target[
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible"
    ]
    profile_action = actions_by_target[
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(3):visible"
    ]
    assert entertainment_action["expected_next_page_type"] == "home"
    assert "91娱乐" in entertainment_action["target_keywords"]
    assert profile_action["expected_next_page_type"] == "profile"
    assert "91私库" in profile_action["target_keywords"]


def test_91_private_library_active_tab_refines_to_profile() -> None:
    page_type = page_detection_tools._refined_page_type_from_active_tabbar(
        page_type="home",
        dom_chunks=[
            '<div class="van-tabbar">'
            '<div class="van-tabbar-item">91看片</div>'
            '<div class="van-tabbar-item">91娱乐</div>'
            '<div class="van-tabbar-item van-tabbar-item--active">91私库</div>'
            "</div>"
        ],
    )

    assert page_type == "profile"


def test_visible_tabbar_navigation_keeps_unknown_structural_entries_without_type_guess() -> None:
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">91看片</div>'
        '<div class="van-tabbar-item">unknown-tab</div>'
        '<div class="van-tabbar-item">profile</div>'
        "</div>"
    )

    assert [action.target for action in actions] == [
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible",
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(3):visible",
    ]
    assert actions[0].expected_next_page_type is None
    assert actions[1].expected_next_page_type == "profile"


def test_jha_bottom_navigation_keeps_short_drama_and_dark_web_entries() -> None:
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">主页</div>'
        '<div class="van-tabbar-item">短剧</div>'
        '<div class="van-tabbar-item">暗网</div>'
        '<div class="van-tabbar-item">AI</div>'
        '<div class="van-tabbar-item">社区</div>'
        '<div class="van-tabbar-item">我的</div>'
        "</div>",
        current_page_type="home",
    )

    assert [action.target for action in actions[:2]] == [
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(2):visible",
        "div.van-tabbar:visible > div.van-tabbar-item:nth-of-type(3):visible",
    ]
    assert [action.target_keywords[1] for action in actions] == [
        "短剧",
        "暗网",
        "ai",
        "社区",
        "我的",
    ]


def test_custom_spa_footer_navigation_adds_all_structural_candidates() -> None:
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<div class="footer-wrap"><ul class="type color">'
        '<li class="footer-item home active"><span class="text">二次元</span></li>'
        '<li class="footer-item video"><span class="text">视频</span></li>'
        '<li class="footer-item ysm"><img alt=""></li>'
        '<li class="footer-item square"><span class="text">广场</span></li>'
        '<li class="footer-item mine"><span class="text">我的</span></li>'
        '<li class="footer-item unknown"><span class="text">未知入口</span></li>'
        "</ul></div>",
        current_page_type="home",
    )

    assert [action.target for action in actions] == [
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(2):visible"),
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(4):visible"),
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(5):visible"),
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(6):visible"),
    ]
    assert [action.expected_next_page_type for action in actions] == [
        "video_list",
        "post_list",
        "profile",
        None,
    ]
    assert all(action.risk_level == "low" for action in actions)
    assert all(action.navigation_depth == 1 for action in actions)
    assert all("bottom_navigation" in action.target_keywords for action in actions)
    assert all(action.target != "未知入口" for action in actions)


def test_custom_spa_four_tab_layout_resolves_ambiguous_content_label() -> None:
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<nav class="bottom-navigation">'
        '<button class="bottom-nav-item active">首页</button>'
        '<button class="bottom-nav-item">内容</button>'
        '<button class="bottom-nav-item">视频</button>'
        '<button class="bottom-nav-item">我的</button>'
        "</nav>",
        current_page_type="home",
    )

    assert [action.expected_next_page_type for action in actions] == [
        "content_list",
        "video_list",
        "profile",
    ]
    enriched = page_detection_tools._with_navigation_knowledge_candidate_metadata(
        candidate_actions=actions,
        nav_alias_matches={},
    )
    assert [action.navigation_knowledge_source for action in enriched] == [
        "contextual",
        "contextual",
        "contextual",
    ]


def _visual_navigation_output() -> VisualNavigationModelOutput:
    return VisualNavigationModelOutput.model_validate(
        {
            "detector_status": "recognized",
            "predictions": [
                {
                    "target_kind": "content_card",
                    "recognized_label": "内容卡片",
                    "label_source": "visual_ocr",
                    "expected_page_type": "content_detail",
                    "visual_target_confidence": 0.93,
                    "selected": False,
                    "bounds": {
                        "screenshot_index": 1,
                        "left_ratio": 0.08,
                        "top_ratio": 0.20,
                        "right_ratio": 0.92,
                        "bottom_ratio": 0.50,
                        "confidence": 0.94,
                    },
                    "risk_flags": [],
                }
            ],
            "visible_ocr_character_count": 4,
            "conflicts": [],
        }
    )


def _visual_detection_bundle(
    *,
    render_kind: str,
    coverage: str,
    render_status: str = "confirmed",
    ocr_text: str | None = None,
    elements: list[dict[str, Any]] | None = None,
    dedicated_png: bytes | None = None,
    declared_image_width: int = 390,
    declared_image_height: int = 844,
    coordinate_space: str = "visual_viewport_css_pixels",
) -> tuple[PageEvidenceBundle, FakeArtifactReader, dict[str, Any]]:
    resolved_dedicated_png = dedicated_png or PNG_BYTES
    dom = _artifact("visual-dom", "dom", content_type="text/html; charset=utf-8")
    ordinary = _artifact("ordinary-shot", "screenshot", content_type="image/png")
    dedicated = _artifact("dedicated-shot", "screenshot", content_type="image/png")
    snapshot = _artifact("visual-snapshot", "ui_snapshot", content_type="application/json")
    ocr = _artifact("visual-ocr", "ocr_text", content_type="text/plain") if ocr_text else None
    surface: dict[str, Any] = {
        "protocol_version": "logical_surface.v1",
        "render_profile": {
            "kind": render_kind,
            "status": render_status,
            "confidence": 0.96,
        },
        "document_identity": "sha256:" + "1" * 64,
        "identity_fingerprint": "sha256:" + "2" * 64,
        "state_fingerprint": "sha256:" + "3" * 64,
        "identity_signals": {},
        "state_signals": {"semantics_coverage": {"level": coverage}},
        "evidence_sources": ["flutter_semantics"],
        "evidence_quality": "medium",
        "degraded_reasons": [],
        "semantics_coverage": {"level": coverage},
        "logical_transition_proof": "trusted",
        "shadow": False,
    }
    ui_snapshot: dict[str, Any] = {
        "schema_version": "ui-snapshot.v2",
        "logical_surface": surface,
        "elements": elements
        or [
            {
                "tag": "flt-semantics",
                "selector": "flt-semantics#node-1",
                "role": "button",
                "text": "首页",
                "interactive": True,
                "visible": True,
            }
        ],
        "visual_navigation_capture": {
            "protocol_version": "visual_navigation_capture.v1",
            "purpose": "visual_navigation_viewport",
            "screenshot_index": 2,
            "screenshot_sha256": "sha256:" + hashlib.sha256(resolved_dedicated_png).hexdigest(),
            "image_width": declared_image_width,
            "image_height": declared_image_height,
            "coordinate_space": coordinate_space,
        },
        "viewport": {
            "width": 390,
            "height": 844,
            "device_pixel_ratio": 1,
            "scroll_x": 0,
            "scroll_y": 0,
            "visual_viewport_width": 390,
            "visual_viewport_height": 844,
            "visual_viewport_offset_left": 0,
            "visual_viewport_offset_top": 0,
            "visual_viewport_page_left": 0,
            "visual_viewport_page_top": 0,
            "visual_viewport_scale": 1,
            "flutter_host_transform": [1, 0, 0, 1, 0, 0],
        },
    }
    artifacts = [dom, ordinary, dedicated, snapshot]
    payloads = {
        "visual-dom": b"<html><body><main>home</main></body></html>",
        "ordinary-shot": PNG_BYTES,
        "dedicated-shot": resolved_dedicated_png,
        "visual-snapshot": json.dumps(ui_snapshot, ensure_ascii=False).encode(),
    }
    if ocr is not None:
        artifacts.append(ocr)
        payloads["visual-ocr"] = ocr_text.encode()
    return _bundle(*artifacts), FakeArtifactReader(payloads), ui_snapshot


@pytest.mark.parametrize(
    ("render_kind", "coverage", "render_status", "expected_calls"),
    [
        ("dom_spa", "semantics_sparse", "confirmed", 0),
        ("flutter_web", "semantics_sufficient", "confirmed", 0),
        ("flutter_web", "semantics_sparse", "confirmed", 1),
        ("flutter_web", "semantics_partial", "confirmed", 1),
        ("flutter_web", "semantics_unavailable", "confirmed", 1),
        ("flutter_web", "future_semantics_level", "confirmed", 1),
        ("hybrid", "semantics_sparse", "confirmed", 1),
        ("flutter_web", "semantics_sparse", "suspected", 0),
    ],
)
async def test_visual_detector_runs_only_for_sparse_confirmed_flutter(
    render_kind: str,
    coverage: str,
    render_status: str,
    expected_calls: int,
) -> None:
    bundle, reader, _ = _visual_detection_bundle(
        render_kind=render_kind,
        coverage=coverage,
        render_status=render_status,
    )
    visual_client = FakeChatClient(_visual_navigation_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-gate",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=visual_client,
            visual_coordinate_mode="shadow",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert len(visual_client.calls) == expected_calls
    assert ("visual_navigation_audit" in result) is bool(expected_calls)


@pytest.mark.parametrize("mode", ["shadow", "enforced"])
async def test_visual_detector_skips_popup_surface_with_fixed_audit(
    mode: str,
) -> None:
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    primary_output = _recognized_output("home").model_copy(
        update={
            "page_state": "popup",
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="scroll",
                    target="先向下浏览",
                    target_keywords=["scroll", "first"],
                    risk_level="low",
                    purpose="保留第一个既有候选",
                ),
                page_detection_tools.CandidateAction(
                    action="scroll",
                    target="再向下浏览",
                    target_keywords=["scroll", "second"],
                    risk_level="low",
                    purpose="保留第二个既有候选",
                ),
            ],
        }
    )
    visual_client = FakeChatClient(_visual_navigation_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id=f"run-visual-popup-{mode}",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(primary_output),
            visual_navigation_chat_client=visual_client,
            visual_coordinate_mode=mode,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert [action["target"] for action in result["candidate_actions"]] == [
        "先向下浏览",
        "再向下浏览",
    ]
    assert visual_client.calls == []
    audit = result["visual_navigation_audit"]
    assert "model_usage" not in audit
    assert audit == {
        "protocol_version": "visual_navigation_audit.v1",
        "detector_status": "insufficient",
        "prediction_count": 0,
        "target_kind_counts": {},
        "visible_ocr_character_count": 0,
        "conflict_codes": ["visual_source_obstructed"],
        "evaluated_prediction_count": 0,
        "allowed_prediction_count": 0,
        "signed_capability_count": 0,
        "verified_capability_count": 0,
        "policy_rejection_counts": {},
        "enforced_candidate_count": 0,
    }


@pytest.mark.parametrize("mode", ["shadow", "enforced"])
@pytest.mark.parametrize(
    "page_state",
    [
        "unknown",
        "transitional",
        "entitlement_locked",
        "login_required",
        "popup_obstructed",
        "popup-overlay",
        "modal.loading",
    ],
)
async def test_visual_detector_skips_nonfinal_and_composite_states(
    mode: str,
    page_state: str,
) -> None:
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    visual_client = FakeChatClient(_visual_navigation_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id=f"run-visual-nonfinal-{mode}-{page_state}",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output("home").model_copy(update={"page_state": page_state})
            ),
            visual_navigation_chat_client=visual_client,
            visual_coordinate_mode=mode,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["page_state"] == page_state
    assert visual_client.calls == []
    assert result["visual_navigation_audit"]["detector_status"] == "insufficient"
    assert result["visual_navigation_audit"]["conflict_codes"] == ["visual_source_obstructed"]
    assert "model_usage" not in result["visual_navigation_audit"]


@pytest.mark.parametrize("mode", ["shadow", "enforced"])
async def test_visual_detector_keeps_stable_nonblocking_notification(
    mode: str,
) -> None:
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
        elements=[
            {
                "tag": "flt-semantics",
                "selector": "flt-semantics#node-1",
                "role": "button",
                "text": "首页",
                "interactive": True,
                "visible": True,
            },
            {
                "tag": "flt-semantics",
                "selector": "flt-semantics#toast",
                "role": "status",
                "text": "加载完成",
                "class_tokens": ["toast"],
                "interactive": False,
                "visible": True,
                "rect": {"x": 120, "y": 80, "width": 150, "height": 36},
                "style": {"pointer_events": "none"},
                "hit_test": {"center_hits_target": False},
            },
        ],
    )
    primary_output = _recognized_output("home").model_copy(update={"page_state": "popup"})
    visual_client = FakeChatClient(_visual_navigation_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id=f"run-visual-notification-{mode}",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(primary_output),
            visual_navigation_chat_client=visual_client,
            visual_coordinate_mode=mode,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["page_state"] == "stable"
    assert result["obstruction_facts"]["kind"] == "nonblocking_notification"
    assert result["obstruction_facts"]["blocking"] is False
    assert len(visual_client.calls) == 1
    assert result["visual_navigation_audit"]["model_usage"]["request_count"] == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"page_type": "line_loading", "page_state": "stable"},
        {"page_type": "startup_ad", "page_state": "stable"},
        {"page_type": "home", "page_state": "popup"},
        {"page_type": "home", "page_state": "modal"},
        {"page_type": "home", "page_state": "overlay"},
        {"page_type": "home", "page_state": "dialog"},
        {"page_type": "home", "page_state": "obstructed"},
        {"page_type": "home", "page_state": "countdown"},
        {"page_type": "home", "page_state": "loading"},
        {"page_type": "home", "page_state": "needs_more_observation"},
        {"page_type": "home", "page_state": "transitioning"},
        {"page_type": "home", "page_state": "transient_loading"},
        {"page_type": "home", "page_state": "partially_loaded"},
        {"page_type": "home", "page_state": "unknown"},
        {"page_type": "home", "page_state": "transitional"},
        {"page_type": "home", "page_state": "entitlement_locked"},
        {"page_type": "home", "page_state": "login_required"},
        {"page_type": "home", "page_state": "popup_obstructed"},
        {"page_type": "home", "page_state": "popup-overlay"},
        {"page_type": "home", "page_state": "modal.loading"},
        {
            "page_type": "home",
            "page_state": "stable",
            "obstruction_facts": {"blocking": True},
        },
        {
            "page_type": "home",
            "page_state": "stable",
            "obstruction_facts": {"trusted_safe_action_available": True},
        },
        {
            "page_type": "home",
            "page_state": "stable",
            "obstruction_facts": {"obstruction_without_safe_action": True},
        },
    ],
)
def test_visual_source_gate_rejects_unstable_and_obstructed_surfaces(
    payload: dict[str, Any],
) -> None:
    assert page_detection_tools._visual_source_is_obstructed_or_unstable(payload) is True


def test_visual_source_gate_allows_stable_nonblocking_notification() -> None:
    assert (
        page_detection_tools._visual_source_is_obstructed_or_unstable(
            {
                "page_type": "home",
                "page_state": "stable",
                "obstruction_facts": {
                    "kind": "nonblocking_notification",
                    "blocking": False,
                    "trusted_safe_action_available": False,
                    "obstruction_without_safe_action": False,
                },
            }
        )
        is False
    )


async def test_visual_detector_receives_production_nav_alias_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未显式传入 Resolver 时，页面检测装配仍应为视觉链提供向量知识。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    resolver = PageNavAliasResolver(repository=VectorNavAliasRepository())
    monkeypatch.setattr(
        page_detection_tools,
        "_get_page_nav_alias_resolver",
        lambda: resolver,
    )
    visual_output = VisualNavigationModelOutput.model_validate(
        {
            "detector_status": "recognized",
            "predictions": [
                {
                    "target_kind": "primary_navigation",
                    "recognized_label": "神秘短剧",
                    "label_source": "visual_ocr",
                    "expected_page_type": "content_list",
                    "visual_target_confidence": 0.93,
                    "selected": False,
                    "bounds": {
                        "screenshot_index": 1,
                        "left_ratio": 0.25,
                        "top_ratio": 0.90,
                        "right_ratio": 0.45,
                        "bottom_ratio": 0.99,
                        "confidence": 0.94,
                    },
                    "risk_flags": [],
                }
            ],
            "visible_ocr_character_count": 4,
            "conflicts": [],
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-default-resolver",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=FakeChatClient(visual_output),
            visual_coordinate_mode="shadow",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    knowledge = result["visual_navigation_audit"]["navigation_knowledge"]
    assert knowledge["match_type_counts"] == {"static": 0, "exact": 0, "vector": 1}
    assert knowledge["accepted_prediction_count"] == 1


@pytest.mark.parametrize(
    ("scenario", "coverage_level", "ocr_chunks", "has_snapshot", "expected_level"),
    [
        ("no_ocr", "semantics_partial", [], True, "semantics_sparse"),
        ("no_snapshot", "semantics_unavailable", ["视觉 OCR"], False, "semantics_sparse"),
        ("partial", "semantics_partial", [], True, "semantics_sparse"),
        ("unavailable", "semantics_unavailable", [], True, "semantics_sparse"),
        ("unknown", "future_semantics_level", [], True, "semantics_sparse"),
        ("sufficient", "semantics_sufficient", [], True, "semantics_sufficient"),
        (
            "ocr_classifier_unavailable",
            "semantics_sufficient",
            ["视觉页面存在大量文本" * 8],
            True,
            "semantics_sparse",
        ),
    ],
)
def test_final_semantics_coverage_is_always_binary_without_mutating_snapshot(
    scenario: str,
    coverage_level: str,
    ocr_chunks: list[str],
    has_snapshot: bool,
    expected_level: str,
) -> None:
    """最终检测结果只能保留 sufficient 或 sparse，且两处覆盖事实必须同步。"""
    surface = {
        "protocol_version": "logical_surface.v1",
        "semantics_coverage": {"level": coverage_level, "source": scenario},
        "state_signals": {
            "semantics_coverage": {
                "level": "semantics_sufficient",
                "source": "persisted_state",
            }
        },
    }
    persisted_surface = json.loads(json.dumps(surface))
    ui_snapshot = {"elements": []} if has_snapshot else None

    result = _logical_surface_with_recomputed_ocr_coverage(
        logical_surface=surface,
        ui_snapshot=ui_snapshot,
        ocr_chunks=ocr_chunks,
    )

    assert result is not None
    assert result["semantics_coverage"]["level"] == expected_level
    assert result["state_signals"]["semantics_coverage"]["level"] == expected_level
    assert surface == persisted_surface


async def test_shadow_isolates_viewport_png_and_preserves_primary_result() -> None:
    """Shadow 与 off 的主页型证据及业务结果必须逐字段一致。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )

    async def run(mode: str) -> tuple[dict[str, Any], FakeChatClient, FakeChatClient]:
        primary_client = FakeChatClient(_recognized_output("home"))
        visual_client = FakeChatClient(_visual_navigation_output())
        with bind_agent_run_context(
            AgentRunContext(
                task_id="11111111-1111-1111-1111-111111111111",
                agent_run_id=f"run-visual-{mode}",
            )
        ):
            result = await detect_page_for_agent(
                page_capture_id="22222222-2222-2222-2222-222222222222",
                evidence_loader=FakeEvidenceLoader(bundle),
                artifact_reader=reader,
                chat_client=primary_client,
                visual_navigation_chat_client=visual_client,
                visual_coordinate_mode=mode,
                skill_text="# page-detection",
                catalog=load_default_page_type_catalog(),
                model_name="vision-test",
                nav_alias_resolver=FakeNavAliasResolver({}),
            )
        return result, primary_client, visual_client

    off, off_primary, off_visual = await run("off")
    shadow, shadow_primary, shadow_visual = await run("shadow")

    off_images = [
        content.uri
        for content in off_primary.calls[0]["messages"][1].contents
        if content.type == "data"
    ]
    shadow_images = [
        content.uri
        for content in shadow_primary.calls[0]["messages"][1].contents
        if content.type == "data"
    ]
    assert off_images == shadow_images
    assert len(off_images) == 1
    for key in (
        "page_type",
        "confidence",
        "rule_matching_allowed",
        "candidate_actions",
        "logical_surface",
    ):
        assert shadow[key] == off[key]
    assert shadow["image_count"] == off["image_count"] == 1
    assert shadow["evidence_artifact_ids"] == off["evidence_artifact_ids"]
    assert len(off_visual.calls) == 0
    assert len(shadow_visual.calls) == 1
    assert "visual_navigation_audit" not in off
    schema_payload = dict(shadow)
    schema_payload.pop("obstruction_facts")
    schema_payload.pop("navigation_knowledge")
    schema_payload.pop("metrics")
    assert (
        PageDetectionResult.model_validate(schema_payload).visual_navigation_audit
        == (shadow["visual_navigation_audit"])
    )
    encoded_audit = json.dumps(shadow["visual_navigation_audit"], ensure_ascii=False)
    assert "内容卡片" not in encoded_audit
    assert "left_ratio" not in encoded_audit


async def test_enforced_appends_only_policy_signed_visual_candidate() -> None:
    """Enforced 只能在既有候选尾部附加可信代码重建并验签的视觉能力。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )

    async def run(mode: str) -> dict[str, Any]:
        with bind_agent_run_context(
            AgentRunContext(
                task_id="11111111-1111-1111-1111-111111111111",
                agent_run_id=f"run-visual-signed-{mode}",
            )
        ):
            return await detect_page_for_agent(
                page_capture_id="22222222-2222-2222-2222-222222222222",
                evidence_loader=FakeEvidenceLoader(bundle),
                artifact_reader=reader,
                chat_client=FakeChatClient(_recognized_output("home")),
                visual_navigation_chat_client=FakeChatClient(_visual_navigation_output()),
                visual_coordinate_mode=mode,
                skill_text="# page-detection",
                catalog=load_default_page_type_catalog(),
                model_name="vision-test",
                nav_alias_resolver=FakeNavAliasResolver({}),
            )

    off = await run("off")
    shadow = await run("shadow")
    enforced = await run("enforced")

    assert shadow["candidate_actions"] == off["candidate_actions"]
    assert enforced["candidate_actions"][:-1] == off["candidate_actions"]
    visual = enforced["candidate_actions"][-1]
    assert visual["target"] == "进入 content_detail 页面"
    assert visual["purpose"] == "受限视觉导航"
    assert set(visual["visual_navigation_capability"]) >= {
        "source_capture_id",
        "source_artifact_id",
        "source_artifact_sha256",
        "source_identity_fingerprint",
        "source_state_fingerprint",
        "source_image_size",
        "viewport_metrics",
        "model_image_transform",
        "normalized_label_digest",
        "local_visual_digest",
        "surrounding_anchor_digest",
        "target_signature",
    }
    capability = visual["visual_navigation_capability"]
    assert capability["protocol_version"] == "visual_navigation_coordinate.v1"
    assert capability["source_task_id"] == "11111111-1111-1111-1111-111111111111"
    assert capability["source_capture_id"] == ("22222222-2222-2222-2222-222222222222")
    assert capability["source_artifact_id"] == "dedicated-shot"
    assert capability["model_image_transform"] == {
        "kind": "identity",
        "scale_x": 1.0,
        "scale_y": 1.0,
        "offset_x": 0.0,
        "offset_y": 0.0,
    }
    serialized_audit = json.dumps(enforced["visual_navigation_audit"], ensure_ascii=False)
    assert "visual_navigation_capability" not in serialized_audit
    assert "target_signature" not in serialized_audit
    assert "local_visual_digest" not in serialized_audit

    first_round_trip = PageDetectionResult.model_validate(enforced)
    trusted_action = first_round_trip.candidate_actions[-1]
    assert trusted_action._trusted_visual_navigation_capability == capability
    second_round_trip = PageDetectionResult.model_validate(first_round_trip.model_dump(mode="json"))
    assert (
        second_round_trip.candidate_actions[-1]._trusted_visual_navigation_capability == capability
    )
    with pytest.raises(ValueError):
        PageDetectionResult.model_validate(dict(enforced))
    persisted = json.loads(json.dumps(enforced, ensure_ascii=False))
    with pytest.raises(ValueError):
        PageDetectionResult.model_validate(persisted)


def test_visual_capability_has_no_public_string_based_trust_restorer() -> None:
    """普通调用者不能用一组可拼接字符串恢复视觉 PrivateAttr。"""
    assert not hasattr(
        page_detection_tools,
        "trust_page_detection_visual_capabilities",
    )


@pytest.mark.parametrize(
    "conflict_code",
    [
        "selected_state_conflict",
        "ocr_label_conflict",
        "ocr_visual_mismatch",
        "expected_page_type_conflict",
    ],
)
@pytest.mark.parametrize("mode", ["shadow", "enforced"])
async def test_any_valid_visual_conflict_fails_closed_before_signing(
    conflict_code: str,
    mode: str,
) -> None:
    """全局冲突无法安全归属候选，Shadow 和 Enforced 都必须零签发。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    output = _visual_navigation_output().model_dump(mode="json")
    output["conflicts"] = [conflict_code]

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id=f"run-visual-conflict-{mode}-{conflict_code}",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=FakeChatClient(
                VisualNavigationModelOutput.model_validate(output)
            ),
            visual_coordinate_mode=mode,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=PageNavAliasResolver(repository=None),
        )

    assert result["candidate_actions"] == []
    audit = result["visual_navigation_audit"]
    assert audit["conflict_codes"] == ["visual_model_conflict"]
    assert audit["allowed_prediction_count"] == 0
    assert audit["signed_capability_count"] == 0
    assert audit["verified_capability_count"] == 0
    assert audit["policy_rejection_counts"] == {"visual_model_conflict": 1}


async def test_primary_aliases_resolving_to_same_destination_are_all_rejected() -> None:
    """不同标签解析到同一 canonical 主导航时，不能规避目的地唯一性。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    output = _visual_navigation_output().model_dump(mode="json")
    output["predictions"] = [
        {
            "target_kind": "primary_navigation",
            "recognized_label": "我的",
            "label_source": "visual_ocr",
            "expected_page_type": "profile",
            "visual_target_confidence": 0.93,
            "selected": False,
            "bounds": {
                "screenshot_index": 1,
                "left_ratio": 0.02,
                "top_ratio": 0.9,
                "right_ratio": 0.18,
                "bottom_ratio": 0.98,
                "confidence": 0.94,
            },
            "risk_flags": [],
        },
        {
            "target_kind": "primary_navigation",
            "recognized_label": "个人中心",
            "label_source": "visual_ocr",
            "expected_page_type": "profile",
            "visual_target_confidence": 0.94,
            "selected": False,
            "bounds": {
                "screenshot_index": 1,
                "left_ratio": 0.76,
                "top_ratio": 0.9,
                "right_ratio": 0.96,
                "bottom_ratio": 0.98,
                "confidence": 0.95,
            },
            "risk_flags": [],
        },
    ]

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-canonical-duplicate",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=FakeChatClient(
                VisualNavigationModelOutput.model_validate(output)
            ),
            visual_coordinate_mode="enforced",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=PageNavAliasResolver(repository=None),
        )

    assert result["candidate_actions"] == []
    assert result["visual_navigation_audit"]["policy_rejection_counts"] == {
        "visual_target_not_unique": 2
    }


@pytest.mark.parametrize(
    ("bundle_updates", "expected_signed_count"),
    [
        ({"declared_image_width": 391}, 0),
        ({"declared_image_width": 390.0}, 0),
        ({"declared_image_height": 843}, 0),
        ({"coordinate_space": "document_css_pixels"}, 0),
        ({}, 1),
    ],
)
async def test_visual_capability_requires_decoded_png_dimensions_and_coordinate_space(
    bundle_updates: dict[str, object],
    expected_signed_count: int,
) -> None:
    """Snapshot 尺寸或坐标系不能替代真实 PNG 解码事实。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
        **bundle_updates,
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-png-contract",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=FakeChatClient(_visual_navigation_output()),
            visual_coordinate_mode="enforced",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=PageNavAliasResolver(repository=None),
        )

    assert result["visual_navigation_audit"]["signed_capability_count"] == (expected_signed_count)
    assert len(result["candidate_actions"]) == expected_signed_count


async def test_visual_capability_digests_come_from_local_and_anchor_png_pixels() -> None:
    """真实 PNG 的目标区域和周边区域变化必须分别改变对应密封摘要。"""

    async def detect(dedicated_png: bytes, run_id: str) -> dict[str, object]:
        bundle, reader, _ = _visual_detection_bundle(
            render_kind="flutter_web",
            coverage="semantics_sparse",
            dedicated_png=dedicated_png,
        )
        with bind_agent_run_context(
            AgentRunContext(
                task_id="11111111-1111-1111-1111-111111111111",
                agent_run_id=run_id,
            )
        ):
            return await detect_page_for_agent(
                page_capture_id="22222222-2222-2222-2222-222222222222",
                evidence_loader=FakeEvidenceLoader(bundle),
                artifact_reader=reader,
                chat_client=FakeChatClient(_recognized_output("home")),
                visual_navigation_chat_client=FakeChatClient(_visual_navigation_output()),
                visual_coordinate_mode="enforced",
                skill_text="# page-detection",
                catalog=load_default_page_type_catalog(),
                model_name="vision-test",
                nav_alias_resolver=PageNavAliasResolver(repository=None),
            )

    baseline = await detect(_test_png(), "run-visual-pixels-baseline")
    local_changed = await detect(
        _test_png(local_color=(200, 10, 20)),
        "run-visual-pixels-local",
    )
    anchor_changed = await detect(
        _test_png(anchor_color=(90, 100, 110)),
        "run-visual-pixels-anchor",
    )
    baseline_capability = baseline["candidate_actions"][-1]["visual_navigation_capability"]
    local_capability = local_changed["candidate_actions"][-1]["visual_navigation_capability"]
    anchor_capability = anchor_changed["candidate_actions"][-1]["visual_navigation_capability"]

    assert local_capability["local_visual_digest"] != baseline_capability["local_visual_digest"]
    assert (
        local_capability["surrounding_anchor_digest"]
        == baseline_capability["surrounding_anchor_digest"]
    )
    assert anchor_capability["local_visual_digest"] == baseline_capability["local_visual_digest"]
    assert (
        anchor_capability["surrounding_anchor_digest"]
        != baseline_capability["surrounding_anchor_digest"]
    )


async def test_visual_capability_normalizes_png_channels_and_preserves_alpha() -> None:
    """灰度/Gray-Alpha/RGB/RGBA 应规范化，且仅 Alpha 变化也必须进入摘要。"""

    async def detect(dedicated_png: bytes, run_id: str) -> dict[str, object]:
        bundle, reader, _ = _visual_detection_bundle(
            render_kind="flutter_web",
            coverage="semantics_sparse",
            dedicated_png=dedicated_png,
        )
        with bind_agent_run_context(
            AgentRunContext(
                task_id="11111111-1111-1111-1111-111111111111",
                agent_run_id=run_id,
            )
        ):
            return await detect_page_for_agent(
                page_capture_id="22222222-2222-2222-2222-222222222222",
                evidence_loader=FakeEvidenceLoader(bundle),
                artifact_reader=reader,
                chat_client=FakeChatClient(_recognized_output("home")),
                visual_navigation_chat_client=FakeChatClient(_visual_navigation_output()),
                visual_coordinate_mode="enforced",
                skill_text="# page-detection",
                catalog=load_default_page_type_catalog(),
                model_name="vision-test",
                nav_alias_resolver=PageNavAliasResolver(repository=None),
            )

    grayscale = await detect(_mode_png("L"), "run-visual-gray")
    gray_alpha = await detect(
        _mode_png("LA", local_alpha=255, anchor_alpha=255),
        "run-visual-gray-alpha",
    )
    rgb = await detect(_mode_png("RGB"), "run-visual-rgb")
    rgba = await detect(
        _mode_png("RGBA", local_alpha=255, anchor_alpha=255),
        "run-visual-rgba",
    )
    local_alpha_changed = await detect(
        _mode_png("RGBA", local_alpha=30, anchor_alpha=255),
        "run-visual-alpha-local",
    )
    anchor_alpha_changed = await detect(
        _mode_png("LA", local_alpha=255, anchor_alpha=30),
        "run-visual-alpha-anchor",
    )

    def digests(result: dict[str, object]) -> tuple[str, str]:
        capability = result["candidate_actions"][-1]["visual_navigation_capability"]
        return (
            capability["local_visual_digest"],
            capability["surrounding_anchor_digest"],
        )

    baseline = digests(grayscale)
    assert digests(gray_alpha) == baseline
    assert digests(rgb) == baseline
    assert digests(rgba) == baseline
    assert digests(local_alpha_changed)[0] != baseline[0]
    assert digests(local_alpha_changed)[1] == baseline[1]
    assert digests(anchor_alpha_changed)[0] == baseline[0]
    assert digests(anchor_alpha_changed)[1] != baseline[1]


async def test_visual_capability_digests_ignore_png_compression_bytes() -> None:
    """编码压缩不同但解码像素相同的 PNG 必须产生相同区域摘要。"""
    results: list[dict[str, object]] = []
    for compress_level in (0, 9):
        png = _mode_png(
            "RGBA",
            local_alpha=200,
            anchor_alpha=230,
            compress_level=compress_level,
        )
        bundle, reader, _ = _visual_detection_bundle(
            render_kind="flutter_web",
            coverage="semantics_sparse",
            dedicated_png=png,
        )
        with bind_agent_run_context(
            AgentRunContext(
                task_id="11111111-1111-1111-1111-111111111111",
                agent_run_id=f"run-visual-compress-{compress_level}",
            )
        ):
            results.append(
                await detect_page_for_agent(
                    page_capture_id="22222222-2222-2222-2222-222222222222",
                    evidence_loader=FakeEvidenceLoader(bundle),
                    artifact_reader=reader,
                    chat_client=FakeChatClient(_recognized_output("home")),
                    visual_navigation_chat_client=FakeChatClient(_visual_navigation_output()),
                    visual_coordinate_mode="enforced",
                    skill_text="# page-detection",
                    catalog=load_default_page_type_catalog(),
                    model_name="vision-test",
                    nav_alias_resolver=PageNavAliasResolver(repository=None),
                )
            )

    capabilities = [
        result["candidate_actions"][-1]["visual_navigation_capability"] for result in results
    ]
    assert capabilities[0]["source_artifact_sha256"] != capabilities[1]["source_artifact_sha256"]
    assert capabilities[0]["local_visual_digest"] == capabilities[1]["local_visual_digest"]
    assert (
        capabilities[0]["surrounding_anchor_digest"] == capabilities[1]["surrounding_anchor_digest"]
    )


async def test_visual_capability_rejects_png_with_empty_anchor_sample() -> None:
    """1x1 PNG 无法同时提供目标与周边锚点像素，必须零签发。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
        dedicated_png=_mode_png("RGBA", width=1, height=1),
        declared_image_width=1,
        declared_image_height=1,
    )
    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-empty-anchor",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=FakeChatClient(_visual_navigation_output()),
            visual_coordinate_mode="enforced",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=PageNavAliasResolver(repository=None),
        )

    assert result["visual_navigation_audit"]["signed_capability_count"] == 0
    assert result["candidate_actions"] == []


async def test_overlapping_visual_predictions_are_rejected_before_signing() -> None:
    """重叠框无法证明区域唯一性，Enforced 不得挑一条继续签发。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    output = _visual_navigation_output().model_dump(mode="json")
    duplicate = json.loads(json.dumps(output["predictions"][0]))
    duplicate["recognized_label"] = "另一内容卡片"
    duplicate["bounds"].update(
        left_ratio=0.10,
        top_ratio=0.25,
        right_ratio=0.90,
        bottom_ratio=0.72,
    )
    output["predictions"].append(duplicate)

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-overlap",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=FakeChatClient(
                VisualNavigationModelOutput.model_validate(output)
            ),
            visual_coordinate_mode="enforced",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["candidate_actions"] == []
    assert result["visual_navigation_audit"]["policy_rejection_counts"] == {
        "visual_target_not_unique": 2
    }
    assert result["visual_navigation_audit"]["signed_capability_count"] == 0


def test_visual_model_cannot_supply_sealed_capability_fields() -> None:
    """模型 Schema 不接收来源 SHA、签名或其他可信代码密封字段。"""
    output = _visual_navigation_output().model_dump(mode="json")
    output["predictions"][0]["target_signature"] = "sha256:" + "f" * 64

    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        VisualNavigationModelOutput.model_validate(output)


async def test_invalid_model_conflict_is_replaced_by_fixed_safe_audit() -> None:
    """模型冲突字段解析失败时，结果只能暴露固定 blocked 审计。"""
    secret = "UNIQUE_OCR_SECRET_sha256_deadbeef"
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    malicious_output = _visual_navigation_output().model_dump(mode="json")
    malicious_output["conflicts"] = [secret]
    visual_client = FakeChatClient(malicious_output)

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-conflict-redaction",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=visual_client,
            visual_coordinate_mode="shadow",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    audit = dict(result["visual_navigation_audit"])
    model_usage = audit.pop("model_usage")
    assert model_usage["request_count"] == 1
    assert 0 <= model_usage["elapsed_ms"] <= 2_147_483_647
    assert audit == {
        "protocol_version": "visual_navigation_audit.v1",
        "detector_status": "blocked",
        "prediction_count": 0,
        "target_kind_counts": {},
        "visible_ocr_character_count": 0,
        "conflict_codes": ["visual_detector_failed"],
        "navigation_knowledge": {
            "match_type_counts": {"static": 0, "exact": 0, "vector": 0},
            "repository_degraded_count": 0,
            "accepted_prediction_count": 0,
            "rejected_prediction_count": 0,
            "rejection_reason_counts": {},
        },
        "evaluated_prediction_count": 0,
        "allowed_prediction_count": 0,
        "signed_capability_count": 0,
        "verified_capability_count": 0,
        "policy_rejection_counts": {},
        "enforced_candidate_count": 0,
    }
    assert secret not in json.dumps(result, ensure_ascii=False)


async def test_real_ocr_recomputes_coverage_on_result_copy_before_visual_gate() -> None:
    """真实 OCR 低覆盖只能降低结果副本，不得反写 UI Snapshot Artifact。"""
    elements = [
        {
            "tag": "flt-semantics",
            "selector": f"flt-semantics#node-{index}",
            "role": "button",
            "text": "首页",
            "interactive": True,
            "visible": True,
        }
        for index in range(12)
    ]
    ocr_text = "这是视觉页面中存在但语义树没有覆盖的长文本内容" * 8
    bundle, reader, original_snapshot = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sufficient",
        ocr_text=ocr_text,
        elements=elements,
    )
    visual_client = FakeChatClient(_visual_navigation_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-ocr",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            visual_navigation_chat_client=visual_client,
            visual_coordinate_mode="shadow",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["logical_surface"]["semantics_coverage"]["level"] == "semantics_sparse"
    assert result["logical_surface"]["state_signals"]["semantics_coverage"]["level"] == (
        "semantics_sparse"
    )
    assert original_snapshot["logical_surface"]["semantics_coverage"]["level"] == (
        "semantics_sufficient"
    )
    assert len(visual_client.calls) == 1


async def test_deterministic_lifecycle_result_skips_visual_model_with_audit() -> None:
    """确定性生命周期页仍走统一后处理，但不得同步调用视觉模型。"""
    bundle, reader, _ = _visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
    )
    ocr = _artifact("guide-ocr", "ocr_text", content_type="text/plain; charset=utf-8")
    deterministic_bundle = PageEvidenceBundle(
        task_id=bundle.task_id,
        page_capture_id=bundle.page_capture_id,
        page_url=bundle.page_url,
        page_title=bundle.page_title,
        artifacts=(*bundle.artifacts, ocr),
        capture_metadata=bundle.capture_metadata,
    )
    reader.payloads["visual-dom"] = (
        b'<html><body><main class="download-guide">'
        b"<h1>App</h1><button>Windows\xe4\xb8\x8b\xe8\xbd\xbd</button>"
        b"</main></body></html>"
    )
    reader.payloads["guide-ocr"] = "安卓下载\n苹果手机下载".encode()
    primary_client = FakeChatClient(_recognized_output("home"))
    visual_client = FakeChatClient(_visual_navigation_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-visual-deterministic",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(deterministic_bundle),
            artifact_reader=reader,
            chat_client=primary_client,
            visual_navigation_chat_client=visual_client,
            visual_coordinate_mode="shadow",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
        )

    assert result["page_type"] == "guide_page"
    assert primary_client.calls == []
    assert visual_client.calls == []
    audit = result["visual_navigation_audit"]
    assert audit["detector_status"] == "insufficient"
    assert audit["conflict_codes"] == ["visual_source_obstructed"]
    assert audit["prediction_count"] == 0
    assert "model_usage" not in audit


def test_custom_spa_navigation_keeps_popup_loading_and_unknown_states_fail_closed() -> None:
    dom = (
        '<nav class="bottom-nav"><div class="item active" role="tab">首页</div>'
        '<div class="item" role="tab">视频</div></nav>'
    )

    assert (
        page_detection_tools._with_visible_tabbar_navigation_candidates(
            page_type="home",
            page_state="popup",
            candidate_actions=[],
            dom_chunks=[dom],
        )
        == []
    )
    assert (
        page_detection_tools._with_visible_tabbar_navigation_candidates(
            page_type="home",
            page_state="line_loading",
            candidate_actions=[],
            dom_chunks=[dom],
        )
        == []
    )
    assert (
        page_detection_tools._with_visible_tabbar_navigation_candidates(
            page_type=None,
            page_state="stable",
            candidate_actions=[],
            dom_chunks=[dom],
        )
        == []
    )


def test_custom_spa_active_tab_can_refine_home_page_type() -> None:
    page_type = page_detection_tools._refined_page_type_from_active_tabbar(
        page_type="home",
        dom_chunks=[
            '<div class="footer-wrap"><ul class="type color">'
            '<li class="footer-item home"><span>二次元</span></li>'
            '<li class="footer-item mine active"><span>我的</span></li>'
            "</ul></div>"
        ],
    )

    assert page_type == "profile"


def test_pages_foot_navigation_adds_controlled_community_and_profile_candidates() -> None:
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<div class="pages-foot-box"><ul class="pages-foot-fix justify-around">'
        '<li class="active home"><span class="pages-foot-label">首页</span></li>'
        '<li class="community"><span class="pages-foot-label">社区</span></li>'
        '<li class="anime"><span class="pages-foot-label">二次元</span></li>'
        '<li class="mine"><span class="pages-foot-label">我的</span></li>'
        "</ul></div>"
    )

    assert [action.target for action in actions] == [
        "ul.pages-foot-fix:visible > li:nth-of-type(2):visible",
        "ul.pages-foot-fix:visible > li:nth-of-type(3):visible",
        "ul.pages-foot-fix:visible > li:nth-of-type(4):visible",
    ]
    assert [action.expected_next_page_type for action in actions] == [
        "post_list",
        "home",
        "profile",
    ]


def test_footer_wrap_navigation_keeps_dom_positions_and_clears_stale_popup() -> None:
    dom = (
        '<main class="home-feed">内容</main><div class="footer-wrap"><div>'
        '<ul class="type color">'
        '<li class="footer-item home item-0"><div class="ico-act"></div>'
        '<div class="text">二次元</div></li>'
        '<li class="footer-item video item-1"><div class="text">视频</div></li>'
        '<li class="footer-item ysm item-2"><img alt=""></li>'
        '<li class="footer-item ai item-6"><div class="text">AI女友</div></li>'
        '<li class="footer-item square item-3"><div class="text">广场</div></li>'
        '<li class="footer-item mine item-5"><div class="text">我的</div></li>'
        "</ul></div></div>"
    )

    actions = page_detection_tools._visible_tabbar_navigation_actions(dom)
    page_state = page_detection_tools._refined_page_state_after_obstruction_check(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert [action.target for action in actions] == [
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(2):visible"),
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(4):visible"),
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(5):visible"),
        ("div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(6):visible"),
    ]
    assert [action.expected_next_page_type for action in actions] == [
        "video_list",
        "content_list",
        "post_list",
        "profile",
    ]
    assert page_state == "stable"


def test_flutter_bottom_navigation_adds_controlled_business_candidates() -> None:
    dom = (
        '<flt-semantics-host><flt-semantics style="width:390px;height:844px">'
        '<flt-semantics id="nav-home" flt-tappable="" '
        'style="width:55.7px;height:57.2px;transform:matrix(1, 0, 0, 1, 0, 771.2)"></flt-semantics>'
        '<flt-semantics id="nav-video" flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1, 0, 0, 1, 55.7, 771.2)">'
        "视频</flt-semantics>"
        '<flt-semantics id="nav-short-video" flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1, 0, 0, 1, 111.4, 771.2)">'
        "抖阴</flt-semantics>"
        '<flt-semantics id="nav-community" flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1, 0, 0, 1, 167.1, 771.2)">'
        "海角</flt-semantics>"
        '<flt-semantics id="nav-news" flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1, 0, 0, 1, 278.6, 771.2)">'
        "吃瓜</flt-semantics>"
        '<flt-semantics id="nav-profile" flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1, 0, 0, 1, 334.3, 771.2)">'
        "我的</flt-semantics>"
        "</flt-semantics></flt-semantics-host>"
    )

    actions = page_detection_tools._visible_tabbar_navigation_actions(dom)

    assert [action.target for action in actions] == [
        "internal:flutter_bottom_navigation:nth-of-type(1)",
        "internal:flutter_bottom_navigation:nth-of-type(2)",
        "internal:flutter_bottom_navigation:nth-of-type(3)",
        "internal:flutter_bottom_navigation:nth-of-type(4)",
        "internal:flutter_bottom_navigation:nth-of-type(5)",
    ]
    assert [action.expected_next_page_type for action in actions] == [
        "video_list",
        "video_list",
        "post_list",
        "post_list",
        "profile",
    ]


def test_flutter_bottom_navigation_keeps_nonempty_label_index_when_active_tab_is_blank() -> None:
    """激活项保留 role=text 但清空文案时，不得挤偏后续底栏标签序号。"""
    dom = (
        '<flt-semantics-host><flt-semantics style="width:390px;height:844px">'
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,0,771.2)">'
        "首页</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,55.7,771.2)">'
        "</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,111.4,771.2)">'
        "抖阴</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,167.1,771.2)">'
        "海角</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,222.8,771.2)">'
        "暗网</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,278.6,771.2)">'
        "吃瓜</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,334.3,771.2)">'
        "我的</flt-semantics>"
        "</flt-semantics></flt-semantics-host>"
    )

    actions = page_detection_tools._visible_tabbar_navigation_actions(
        dom,
        current_page_type="video_list",
    )

    by_label = {action.target_keywords[1]: action for action in actions}
    assert by_label["海角"].target == ("internal:flutter_bottom_navigation:nth-of-type(3)")
    assert by_label["海角"].expected_next_page_type == "post_list"


@pytest.mark.parametrize("model_page_type", ["home", "video_detail"])
async def test_verified_flutter_bottom_navigation_arrival_refines_model_page_type(
    model_page_type: str,
) -> None:
    """已验效的视频底栏到达必须覆盖同 URL SPA 的首页或伪详情误报。"""
    dom = _artifact("dom-flutter-video", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-flutter-video", "screenshot", content_type="image/png")
    capture_id = "22222222-2222-2222-2222-222222222222"
    arrival_context = {
        "action_status": "completed",
        "action_type": "click",
        "semantic_target": "internal:flutter_bottom_navigation:nth-of-type(1)",
        "risk_level": "low",
        "parameters": {
            "target_mode": "bottom_navigation",
            "navigation_family": "flutter_semantics",
            "navigation_index": 1,
            "navigation_label": "视频",
            "expected_next_page_type": "video_list",
            "candidate_id": "h5-candidate:" + "a" * 64,
            "frontier_snapshot_id": "h5-task-frontier:" + "b" * 64,
        },
        "result_payload": {
            "execution_outcome": "succeeded",
            "result_capture_id": capture_id,
            "state_diff": {
                "before_location": "https://p1.example/",
                "after_location": "https://p1.example/",
                "transition_kind": "same_page_interaction",
            },
        },
        "navigation_effect_verified": True,
        "requested_source_page_type": "home",
        "requested_source_page_state": "stable",
        "requested_source_url": "https://p1.example/",
    }
    flutter_video_dom = (
        f'<html><body data-model-case="{model_page_type}"><flt-semantics-host>'
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,0,771.2)">'
        "首页</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,55.7,771.2)">'
        "</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,111.4,771.2)">'
        "抖阴</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,167.1,771.2)">'
        "海角</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,222.8,771.2)">'
        "暗网</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,278.6,771.2)">'
        "吃瓜</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text" '
        'style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,334.3,771.2)">'
        "我的</flt-semantics>"
        '<flt-semantics flt-tappable="" role="text">我知道了</flt-semantics>'
        "</flt-semantics-host></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id=capture_id,
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://p1.example/",
                    capture_metadata={
                        "capture_role": "after_action",
                        "_arrival_action_context": arrival_context,
                    },
                )
            ),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-flutter-video": flutter_video_dom,
                    "shot-flutter-video": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(
                _recognized_output(model_page_type).model_copy(
                    update={
                        "page_state": "popup",
                        "candidate_actions": [
                            page_detection_tools.CandidateAction(
                                action="click",
                                target="我知道了",
                                target_keywords=["我知道了"],
                                risk_level="low",
                                purpose="关闭当前引导弹窗以继续浏览视频列表",
                                expected_next_page_type="home",
                                navigation_depth=1,
                            )
                        ],
                    }
                )
            ),
            skill_text="固定技能",
            catalog=load_default_page_type_catalog(),
            nav_alias_resolver=FakeNavAliasResolver(
                {
                    "视频": PageNavAliasMatch(
                        canonical_code="video_list",
                        page_subtype_code=None,
                        matched_term="视频",
                        source_category="视频",
                        confidence=1.0,
                        match_type="static",
                    )
                }
            ),
        )

    assert result["page_type"] == "video_list"
    assert result["model_page_type"] == model_page_type
    acknowledgement = next(
        action for action in result["candidate_actions"] if action["target"] == "我知道了"
    )
    assert acknowledgement["expected_next_page_type"] == "video_list"


async def test_verified_popup_dismissal_inherits_requested_source_page_type() -> None:
    """同 URL 弹窗关闭完成后必须保留已识别来源页型，不能回退成 home。"""
    dom = _artifact("dom-after-popup", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-after-popup", "screenshot", content_type="image/png")
    capture_id = "22222222-2222-2222-2222-222222222222"
    arrival_context = {
        "action_status": "completed",
        "action_type": "click",
        "semantic_target": "我知道了",
        "purpose": "关闭当前引导弹窗以继续浏览视频列表",
        "risk_level": "low",
        "parameters": {
            "navigation_depth": 1,
            "expected_next_page_type": "video_list",
            "candidate_id": "h5-candidate:" + "c" * 64,
            "frontier_snapshot_id": "h5-task-frontier:" + "d" * 64,
        },
        "result_payload": {
            "execution_outcome": "succeeded",
            "result_capture_id": capture_id,
            "state_diff": {
                "before_location": "https://p1.example/",
                "after_location": "https://p1.example/",
                "transition_kind": "same_page_interaction",
            },
        },
        "requested_source_page_type": "video_list",
        "requested_source_page_state": "popup",
        "requested_source_url": "https://p1.example/",
    }

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id=capture_id,
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://p1.example/",
                    capture_metadata={
                        "capture_role": "after_action",
                        "_arrival_action_context": arrival_context,
                    },
                )
            ),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-after-popup": "<html><body>视频列表</body></html>".encode(),
                    "shot-after-popup": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="固定技能",
            catalog=load_default_page_type_catalog(),
        )

    assert result["page_type"] == "video_list"
    assert result["model_page_type"] == "home"


async def test_flutter_app_open_banner_keeps_navigation_and_vector_knowledge() -> None:
    dom = _artifact("dom-flutter-nav", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-flutter-nav", "screenshot", content_type="image/png")
    navigation_nodes = "".join(
        f"""<flt-semantics flt-tappable="" role="text"
        style="width:55.7px;height:57.2px;transform:matrix(1,0,0,1,{x},771.2)">
        {label}</flt-semantics>"""
        for x, label in (
            (55.7, "视频"),
            (111.4, "抖阴"),
            (167.1, "海角"),
            (222.8, "暗网"),
            (278.6, "吃瓜"),
            (334.3, "我的"),
        )
    )
    flutter_dom = (
        "<html><body><flutter-view><flt-semantics-host>"
        f"{navigation_nodes}"
        '<flt-semantics flt-tappable="" role="text" style="width:210px;height:30px;'
        'transform:matrix(1,0,0,1,90,720)">91PornAPP内打开</flt-semantics>'
        "</flt-semantics-host></flutter-view></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://flutter.example/")
            ),
            artifact_reader=FakeArtifactReader(
                {"dom-flutter-nav": flutter_dom, "shot-flutter-nav": PNG_BYTES}
            ),
            chat_client=FakeChatClient(
                _recognized_output("home").model_copy(
                    update={
                        "page_state": "stable_with_app_open_prompt",
                        "candidate_actions": [
                            page_detection_tools.CandidateAction(
                                action="click",
                                target="91PornAPP内打开",
                                target_keywords=["APP内打开", "external_navigation"],
                                risk_level="medium",
                                purpose="尝试在应用内打开当前内容，可能触发外部应用或跳转",
                                navigation_depth=1,
                            )
                        ],
                    }
                )
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=PageNavAliasResolver(repository=VectorNavAliasRepository()),
        )

    assert result["page_state"] == "stable"
    assert all("APP内打开" not in item["target"] for item in result["candidate_actions"])
    audit = result["navigation_knowledge"]
    assert audit["visible_label_count"] == 6
    assert audit["repository_configured"] is True
    assert audit["vector_lookup_attempt_count"] == 1
    assert audit["match_type_counts"] == {"static": 5, "exact": 0, "vector": 1}
    profile_action = next(
        item
        for item in result["candidate_actions"]
        if item.get("expected_next_page_type") == "profile"
    )
    assert profile_action["target"] == ("internal:flutter_bottom_navigation:nth-of-type(6)")
    vector_action = next(
        item
        for item in result["candidate_actions"]
        if item.get("navigation_knowledge_source") == "vector"
    )
    assert vector_action["expected_next_page_type"] == "content_list"


def test_native_app_open_candidate_is_removed_without_model_intent_hint() -> None:
    """精确 APP 打开文案本身已足以证明它不是 H5 页面类型入口。"""
    actions = [
        page_detection_tools.CandidateAction(
            action="click",
            target="91PornAPP内打开",
            target_keywords=[],
            risk_level="high",
            purpose="观察当前横幅入口",
            navigation_depth=1,
        ),
        page_detection_tools.CandidateAction(
            action="click",
            target="我的",
            target_keywords=["bottom_navigation", "profile"],
            risk_level="low",
            purpose="探索个人中心页面",
            expected_next_page_type="profile",
            navigation_depth=1,
        ),
    ]

    retained = page_detection_tools._without_native_app_open_call_to_action_candidates(actions)

    assert [action.target for action in retained] == ["我的"]


@pytest.mark.parametrize(
    ("label", "expected_page_type"),
    [
        ("导航", "home"),
        ("应用", "home"),
        ("抖音", "video_list"),
        ("短片", "video_list"),
        ("圈子", "post_list"),
        ("私密圈", "post_list"),
        ("AI科技", "content_list"),
        ("二次元", "home"),
        ("我的窝", "profile"),
    ],
)
def test_attachment_tab_aliases_map_to_existing_page_types(
    label: str,
    expected_page_type: str,
) -> None:
    assert (
        page_detection_tools._mapped_page_type_from_active_tabbar_label(label) == expected_page_type
    )


@pytest.mark.parametrize(
    "label",
    ["main", "mail", "paid", "homework", "videojs", "\u6211\u7684\u5e94\u7528"],
)
def test_attachment_tab_aliases_do_not_match_short_ascii_inside_words(
    label: str,
) -> None:
    assert page_detection_tools._mapped_page_type_from_active_tabbar_label(label) is None


def test_attachment_tab_aliases_enhance_visible_tabbar_navigation() -> None:
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
        '<div class="van-tabbar-item">抖音</div>'
        '<div class="van-tabbar-item">圈子</div>'
        '<div class="van-tabbar-item">AI科技</div>'
        '<div class="van-tabbar-item">我的窝</div>'
        "</div>"
    )

    assert [action.expected_next_page_type for action in actions] == [
        "video_list",
        "post_list",
        "content_list",
        "profile",
    ]
    assert [action.target_keywords[-1] for action in actions] == [
        "video_list",
        "post_list",
        "content_list",
        "profile",
    ]


def test_six_tab_manga_site_uses_contextual_navigation_types() -> None:
    """完整六栏结构覆盖全局“二次元=home”旧别名，保留真实漫画入口。"""
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">精选</div>'
        '<div class="van-tabbar-item">二次元</div>'
        '<div class="van-tabbar-item">禁区</div>'
        '<div class="van-tabbar-item">AI生成</div>'
        '<div class="van-tabbar-item">社区</div>'
        '<div class="van-tabbar-item">我的</div>'
        "</div>",
        current_page_type="home",
    )

    assert [action.expected_next_page_type for action in actions] == [
        "content_list",
        "video_list",
        "content_list",
        "post_list",
        "profile",
    ]
    assert [action.target for action in actions] == [
        f"div.van-tabbar:visible > div.van-tabbar-item:nth-of-type({index}):visible"
        for index in range(2, 7)
    ]


def test_six_tab_contextual_navigation_issues_trusted_knowledge_metadata() -> None:
    """完整六栏结构的上下文映射必须签发独立受信导航知识。"""
    actions = page_detection_tools._with_navigation_knowledge_candidate_metadata(
        candidate_actions=page_detection_tools._visible_tabbar_navigation_actions(
            '<div class="van-tabbar">'
            '<div class="van-tabbar-item van-tabbar-item--active">精选</div>'
            '<div class="van-tabbar-item">二次元</div>'
            '<div class="van-tabbar-item">禁区</div>'
            '<div class="van-tabbar-item">AI生成</div>'
            '<div class="van-tabbar-item">社区</div>'
            '<div class="van-tabbar-item">我的</div>'
            "</div>",
            current_page_type="home",
        ),
        nav_alias_matches={},
    )

    contextual_actions = [
        action for action in actions if action.target_keywords[1] in {"二次元", "ai生成"}
    ]
    assert [action.expected_next_page_type for action in contextual_actions] == [
        "content_list",
        "content_list",
    ]
    assert [action.navigation_knowledge_source for action in contextual_actions] == [
        "contextual",
        "contextual",
    ]
    assert [action.navigation_knowledge_confidence for action in contextual_actions] == [
        1.0,
        1.0,
    ]


def test_model_text_actions_for_visible_tabbar_labels_are_removed() -> None:
    actions = page_detection_tools._without_model_tabbar_navigation_actions(
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="影片",
                target_keywords=["底部导航"],
                risk_level="low",
                purpose="切换底部导航",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="text=社区",
                target_keywords=["底部导航"],
                risk_level="low",
                purpose="切换底部导航",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="text=首个视频卡片",
                target_keywords=["视频"],
                risk_level="low",
                purpose="探索详情",
            ),
        ],
        dom_chunks=[
            '<html><body><div class="van-tabbar">'
            '<div class="van-tabbar-item van-tabbar-item--active"><span>热门</span></div>'
            '<div class="van-tabbar-item"><span>影片</span></div>'
            '<div class="van-tabbar-item"><span>社区</span></div>'
            "</div><button>首个视频卡片</button></body></html>"
        ],
    )

    assert [action.target for action in actions] == ["text=首个视频卡片"]


def test_model_text_actions_for_visible_page_tab_labels_are_removed() -> None:
    profile = H5ExplorationProfile(
        page_tab_items=[
            H5TabbarItem(
                label="AI图片换脸",
                selector=".van-tabs:visible .van-tab:nth-of-type(2):visible",
                active=False,
                expected_page_type="content_list",
            )
        ]
    )
    actions = page_detection_tools._without_model_tabbar_navigation_actions(
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target="AI图片换脸",
                target_keywords=["AI图片换脸"],
                risk_level="low",
                purpose="切换到 AI 图片换脸功能",
                expected_next_page_type="content_list",
            ),
            page_detection_tools.CandidateAction(
                action="click",
                target="查看详情",
                target_keywords=["详情"],
                risk_level="low",
                purpose="探索内容详情",
                expected_next_page_type="content_detail",
            ),
        ],
        dom_chunks=[],
        profile=profile,
    )

    assert [action.target for action in actions] == ["查看详情"]


@pytest.mark.parametrize(
    ("active_label", "expected_page_type"),
    [
        ("热门", "home"),
        ("影片", "video_list"),
        ("次元", "content_list"),
        ("社区", "post_list"),
        ("AI", "content_list"),
        ("我的", "profile"),
    ],
)
def test_home_page_type_is_refined_by_unique_active_tabbar_label(
    active_label: str,
    expected_page_type: str,
) -> None:
    page_type = page_detection_tools._refined_page_type_from_active_tabbar(
        page_type="home",
        dom_chunks=[
            '<div class="van-tabbar">'
            '<div class="van-tabbar-item">热门</div>'
            f'<div class="van-tabbar-item van-tabbar-item--active">{active_label}</div>'
            '<div class="van-tabbar-item">社区</div>'
            "</div>"
        ],
    )

    assert page_type == expected_page_type


async def test_ambiguous_main_tab_video_panel_is_promoted_to_deterministic_home() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    page_url = "https://example.com/?devTest=123&mainTab=video"
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url=page_url))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                b'<html><body><section class="home-video-panel">'
                b'<div class="home-video-panel__scroll">'
                b'<div class="home-video-panel__tab-title '
                b'home-video-panel__tab-title--active">video</div>'
                b'<div class="TwoGridLayout"><div class="item">'
                b'<div class="pic"><img class="wh-full" alt="video"></div>'
                b'<div class="desc">video title</div>'
                b'<p class="play-time"><span>00:09:07</span></p>'
                b"</div></div></div></section></body></html>"
            ),
            "shot-1": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _ambiguous_output().model_copy(update={"page_state": "stable"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert result["candidate_actions"][0]["list_detail_selector"] == (
        ".home-video-panel .TwoGridLayout .item .desc:visible"
    )
    assert "deterministic_active_tab_page_type_refinement" in result["risk_flags"]
    assert "deterministic_business_route_page_type_refinement" in result["risk_flags"]


@pytest.mark.parametrize(
    ("page_url", "dom"),
    [
        (
            "https://example.com/?mainTab=home",
            '<section class="home-video-panel"><div class="home-video-panel__scroll">'
            '<div class="TwoGridLayout"><div class="item"><div class="pic">'
            '<img></div><div class="desc">video</div><p class="play-time">00:01</p>'
            "</div></div></div></section>",
        ),
        (
            "https://example.com/?mainTab=video",
            '<section class="home-video-panel"><div class="home-video-panel__scroll">'
            '<div class="TwoGridLayout"></div></div></section>',
        ),
    ],
)
def test_main_tab_video_refinement_requires_query_and_complete_grid(
    page_url: str,
    dom: str,
) -> None:
    assert (
        page_detection_tools._refined_page_type_from_active_tabbar(
            page_type=None,
            dom_chunks=[dom],
            page_url=page_url,
        )
        is None
    )


def test_main_tab_video_structure_does_not_override_recognized_home() -> None:
    """根路由已识别为首页时，视频主 Tab 结构不得改写成视频列表。"""
    dom = (
        '<section class="home-video-panel"><div class="home-video-panel__scroll">'
        '<div class="home-video-panel__tab-title '
        'home-video-panel__tab-title--active">video</div>'
        '<div class="TwoGridLayout"><div class="item"><div class="pic">'
        '<img></div><div class="desc">video</div><p class="play-time">00:01</p>'
        "</div></div></div></section>"
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">视频</div>'
        '<div class="van-tabbar-item">我的</div></div>'
    )

    assert (
        page_detection_tools._refined_page_type_from_active_tabbar(
            page_type="home",
            dom_chunks=[dom],
            page_url="https://example.com/?mainTab=video",
        )
        == "home"
    )


async def test_main_tab_video_root_forces_deterministic_home_confidence() -> None:
    """根地址视频主 Tab 必须覆盖模型的视频列表判断，并按确定性首页计分。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    model_payload = _recognized_output("video_list").model_dump()
    model_payload.update(
        confidence=0.93,
        page_type_candidates=[
            {
                "page_type": "video_list",
                "confidence": 0.93,
                "matched_signal_count": 4,
            },
            {
                "page_type": "home",
                "confidence": 0.42,
                "matched_signal_count": 2,
            },
        ],
    )
    model_output = PageDetectionModelOutput.model_validate(model_payload)

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/?mainTab=video",
                )
            ),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-1": b"<html><body><main>video feed</main></body></html>",
                    "shot-1": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(model_output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert "deterministic_business_route_page_type_refinement" in result["risk_flags"]


@pytest.mark.parametrize("model_status", ["ambiguous", "insufficient"])
async def test_main_tab_video_root_promotes_empty_model_result_to_home(
    model_status: str,
) -> None:
    """模型未给出页型时，根地址视频主 Tab 仍必须确定性提升为首页。"""
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    model_output = _ambiguous_output().model_copy(
        update={
            "detection_status": model_status,
            "missing_sources": (["模型未能收敛页面类型"] if model_status == "insufficient" else []),
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/?mainTab=video",
                )
            ),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-1": b"<html><body><main>feed</main></body></html>",
                    "shot-1": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(model_output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert result["model_page_type"] is None
    assert result["rule_matching_allowed"] is True
    assert "deterministic_business_route_page_type_refinement" in result["risk_flags"]


@pytest.mark.parametrize(
    ("page_url", "model_page_type", "expected_page_type"),
    [
        ("https://example.com/", "video_list", "home"),
        ("https://example.com/?mainTab=video", "home", "home"),
        ("https://example.com/?mainTab=video", "video_list", "home"),
        ("https://example.com/?mainTab=video", "startup_ad", "startup_ad"),
        ("https://example.com/main_page/tab/index", "video_list", "home"),
        ("https://example.com/product", "home", "home"),
        ("https://example.com/category/new", "home", "post_list"),
        ("https://example.com/Community", "home", "post_list"),
        ("https://example.com/post", "post_list", "content_list"),
        ("https://example.com/post/detail/123", "post_list", "content_detail"),
        (
            "https://example.com/square/post/post-decial/123",
            "post_list",
            "content_detail",
        ),
        ("https://example.com/post/settings", "home", "home"),
        ("https://example.com/movie", "home", "video_list"),
        ("https://example.com/movie/detail/123", "video_list", "video_detail"),
        ("https://example.com/shortVideo", "video_detail", "video_list"),
        ("https://example.com/home-video", "home", "video_list"),
        ("https://example.com/twoDimensional", "home", "video_list"),
        ("https://example.com/movie/123", "video_list", "video_detail"),
        ("https://example.com/home-video/123", "video_list", "video_detail"),
        ("https://example.com/twoDimensional/123", "video_list", "video_detail"),
        ("https://example.com/play?videoId=123", "video_list", "video_detail"),
        ("https://example.com/archives/123", "post_list", "content_detail"),
        ("https://example.com/product", "startup_ad", "startup_ad"),
        ("https://example.com/unknown", "home", "home"),
    ],
)
def test_business_route_refines_obvious_page_type_confusion(
    page_url: str,
    model_page_type: str | None,
    expected_page_type: str,
) -> None:
    assert (
        page_detection_tools._refined_page_type_from_business_route(
            page_type=model_page_type,
            page_url=page_url,
        )
        == expected_page_type
    )


def test_unknown_post_subroute_has_no_deterministic_page_type() -> None:
    """只登记精确 post 列表和详情路径，不扩展未知 post 子路由。"""
    assert (
        page_detection_tools._deterministic_page_type_from_business_route(
            "https://example.com/post/settings"
        )
        is None
    )


def test_square_post_detail_route_has_deterministic_content_detail_fallback() -> None:
    match = page_detection_tools._deterministic_page_type_from_business_route(
        "https://example.com/square/post/post-decial/123"
    )

    assert match is not None
    assert match.page_type == "content_detail"


async def test_anchor_detail_ambiguous_result_is_promoted_to_content_detail() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(
            dom,
            screenshot,
            page_url="https://example.com/anchorDetail?actorId=1&pageType=%2FnudeChat",
        )
    )
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<main class="anchor-detail"><button class="back">返回</button>'
                "<h1>主播详情</h1><section>服务项目与价格</section></main>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = PageDetectionModelOutput(
        detection_status="ambiguous",
        page_type=None,
        page_state="stable",
        confidence=0.42,
        reason="页面介于内容详情和个人主页之间。",
        matched_catalog_signals={"dom_hints": ["detail", "content"]},
        page_type_candidates=[
            {"page_type": "content_detail", "confidence": 0.48, "matched_signal_count": 3},
            {"page_type": "profile", "confidence": 0.35, "matched_signal_count": 2},
        ],
        candidate_actions=[],
        next_page_hypotheses=[],
        risk_flags=["catalog_gap_anchor_detail"],
        missing_sources=[],
        conflicts=["无法区分内容详情和个人主页"],
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="固定页面识别流程",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "content_detail"
    assert result["model_page_type"] is None
    assert result["rule_matching_allowed"] is True
    assert "deterministic_business_route_page_type_refinement" in result["risk_flags"]


def test_unknown_detail_route_is_not_deterministically_promoted() -> None:
    assert (
        page_detection_tools._deterministic_detail_page_type_from_business_route(
            "https://example.com/unknownDetail?id=1"
        )
        is None
    )


async def test_product_route_does_not_override_model_home_in_detection_audit() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/product"))
    reader = FakeArtifactReader(
        {
            "dom-1": ('<html><body><a href="/archives/123">首个内容</a></body></html>').encode(),
            "shot-1": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output("home").model_copy(update={"page_state": "normal"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "home"
    assert result["model_page_type"] == "home"
    assert "deterministic_business_route_page_type_refinement" not in result["risk_flags"]


async def test_refined_post_tab_adds_nested_detail_candidate() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="movie-list">'
                '<div class="media-item"><div class="media-title">'
                "首个帖子</div></div></div>"
                '<div class="van-tabbar">'
                '<div class="van-tabbar-item">热门</div>'
                '<div class="van-tabbar-item van-tabbar-item--active">社区</div>'
                "</div></body></html>"
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(
                _recognized_output("home").model_copy(update={"page_state": "normal"})
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "post_list"
    assert result["candidate_actions"][0]["target"] == "当前列表首个内容卡片"
    assert result["candidate_actions"][0]["list_detail_selector"] == (
        ".movie-list .media-item:visible .media-title:visible"
    )
    assert result["candidate_actions"][0]["expected_next_page_type"] == ("content_detail")
    assert "deterministic_active_tab_page_type_refinement" in result["risk_flags"]


async def test_model_cannot_assign_navigation_depth_to_discovered_action() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": "<html><body><button>详情</button></body></html>".encode(),
            "shot-1": PNG_BYTES,
        }
    )
    output = _recognized_output("home").model_copy(
        update={
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="text=详情",
                    risk_level="low",
                    purpose="探索详情",
                    navigation_depth=0,
                )
            ]
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["candidate_actions"][0]["navigation_depth"] is None


async def test_visible_dialog_prevents_tabbar_actions_on_stable_page() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot))
    reader = FakeArtifactReader(
        {
            "dom-1": (
                '<html><body><div class="van-dialog">广告</div>'
                '<div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">热门</div>'
                '<div class="van-tabbar-item">影片</div></div></body></html>'
            ).encode(),
            "shot-1": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["candidate_actions"] == []


async def test_recognized_detection_is_persisted_before_rule_matching() -> None:
    repository = FakePageCaptureRepository()
    navigation_knowledge = {
        "match_type_counts": {"static": 1, "exact": 0, "vector": 0},
        "vector_lookup_attempt_count": 0,
        "repository_degraded": False,
    }

    detection_result = {
        "detection_status": "recognized",
        "page_capture_id": "22222222-2222-2222-2222-222222222222",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.94,
        "catalog_version": "v1",
        "catalog_sha256": "sha256:test-catalog",
        "navigation_knowledge": navigation_knowledge,
        "matched_catalog_signals": {"required_positive_signal_ids": ["home-nav"]},
        "recognition_guide_audit": {"reason_code": "recognition_guide_satisfied"},
        "rule_matching_allowed": True,
    }

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="33333333-3333-3333-3333-333333333333",
        )
    ):
        result = await persist_page_detection_for_current_run(
            result=detection_result,
            repository=repository,
        )

    assert result == detection_result
    assert repository.calls == [
        {
            "task_id": "11111111-1111-1111-1111-111111111111",
            "agent_run_id": "33333333-3333-3333-3333-333333333333",
            "page_capture_id": "22222222-2222-2222-2222-222222222222",
            "page_type": "home",
            "page_state": "stable",
            "detection_status": "recognized",
            "confidence": 0.94,
            "catalog_version": "v1",
            "catalog_sha256": "sha256:test-catalog",
            "navigation_knowledge": navigation_knowledge,
            "matched_catalog_signals": {"required_positive_signal_ids": ["home-nav"]},
            "recognition_guide_audit": {"reason_code": "recognition_guide_satisfied"},
        }
    ]


async def test_h5_pure_exploration_persists_detection_without_evidence_readiness(
    monkeypatch,
) -> None:
    detection_result = {
        "detection_status": "recognized",
        "page_capture_id": "22222222-2222-2222-2222-222222222222",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.94,
        "catalog_version": "v1",
        "catalog_sha256": "sha256:test-catalog",
        "rule_matching_allowed": True,
    }

    async def _unexpected_readiness(**kwargs):
        del kwargs
        raise AssertionError("H5 pure exploration must not invoke evidence readiness")

    monkeypatch.setattr(
        page_detection_tools,
        "_finalize_capture_evidence_readiness",
        _unexpected_readiness,
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="33333333-3333-3333-3333-333333333333",
        )
    ):
        result = await persist_page_detection_for_current_run(
            result=detection_result,
            repository=FakePageCaptureRepository(),
            finalize_evidence_readiness=False,
        )

    assert result == detection_result


async def test_recognized_detection_blocks_matching_when_persistence_fails() -> None:
    detection_result = {
        "detection_status": "recognized",
        "page_capture_id": "22222222-2222-2222-2222-222222222222",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.94,
        "catalog_version": "v1",
        "catalog_sha256": "sha256:test-catalog",
        "rule_matching_allowed": True,
    }

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="33333333-3333-3333-3333-333333333333",
        )
    ):
        result = await persist_page_detection_for_current_run(
            result=detection_result,
            repository=FakePageCaptureRepository(updated=False),
        )

    assert result["detection_status"] == "error"
    assert result["rule_matching_allowed"] is False
    assert result["error_code"] == "PAGE_DETECTION_PERSIST_FAILED"


async def test_recognized_detection_blocks_matching_when_requirement_publish_fails(
    monkeypatch,
) -> None:
    detection_result = {
        "detection_status": "recognized",
        "page_capture_id": "22222222-2222-2222-2222-222222222222",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.94,
        "catalog_version": "v1",
        "catalog_sha256": "sha256:test-catalog",
        "rule_matching_allowed": True,
    }

    async def _failed_readiness(**kwargs):
        return CaptureEvidenceReadiness(
            capture_id=kwargs["capture_id"],
            failure_reason="requirement_publish_failed",
            failure_code="EVIDENCE_REQUIREMENT_PUBLISH_FAILED",
            retryable=False,
        )

    monkeypatch.setattr(
        page_detection_tools,
        "_finalize_capture_evidence_readiness",
        _failed_readiness,
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="33333333-3333-3333-3333-333333333333",
        )
    ):
        result = await persist_page_detection_for_current_run(
            result=detection_result,
            repository=FakePageCaptureRepository(),
        )

    assert result["detection_status"] == "error"
    assert result["rule_matching_allowed"] is False
    assert result["error_code"] == "EVIDENCE_REQUIREMENT_PUBLISH_FAILED"
    assert result["failure_kind"] == "infrastructure"
    assert result["retryable"] is False


async def test_missing_dom_or_screenshot_returns_insufficient_without_model_call() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    loader = FakeEvidenceLoader(_bundle(dom))
    reader = FakeArtifactReader({"dom-1": b"<html>only dom</html>"})
    client = FakeChatClient(_recognized_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "insufficient"
    assert result["rule_matching_allowed"] is False
    assert result["missing_sources"] == ["screenshot"]
    assert client.calls == []


async def test_unknown_or_cross_task_capture_returns_error_without_model_call() -> None:
    loader = FakeEvidenceLoader(None)
    client = FakeChatClient(_recognized_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="33333333-3333-3333-3333-333333333333",
            evidence_loader=loader,
            artifact_reader=FakeArtifactReader({}),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "error"
    assert result["error_code"] == "PAGE_CAPTURE_NOT_FOUND"
    assert result["rule_matching_allowed"] is False
    assert client.calls == []


async def test_catalog_external_page_type_is_skipped_without_rule_matching() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    client = FakeChatClient(_recognized_output("unknown_page"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>test</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "unrecognized"
    assert result["page_type"] is None
    assert result["candidate_actions"] == []
    assert result["catalog_disposition"] == "skipped"
    assert result["catalog_skip_reason"] == "uncatalogued_page_type"
    assert result.get("error_code") is None
    assert result["rule_matching_allowed"] is False


async def test_h5_download_guide_page_short_circuits_model_and_exploration() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    ocr = _artifact("ocr-1", "ocr_text", content_type="text/plain; charset=utf-8")
    client = FakeChatClient(
        _recognized_output("home").model_copy(
            update={
                "candidate_actions": [
                    {
                        "action": "click",
                        "target": "安卓下载",
                        "target_keywords": ["下载"],
                        "risk_level": "low",
                        "purpose": "下载 App",
                        "expected_next_page_type": None,
                        "navigation_depth": 1,
                    }
                ]
            }
        )
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot, ocr)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-1": (
                        b'<html><body><main class="download-guide">'
                        b"<h1>App</h1><button>Windows\xe4\xb8\x8b\xe8\xbd\xbd</button>"
                        b"</main></body></html>"
                    ),
                    "shot-1": PNG_BYTES,
                    # 真·下载引导页会把多端安装包摆在明面上；证据取截图 OCR 而非 DOM，
                    # 因为内容站页脚同样藏着下载按钮，DOM 命中不足以判定页面用途。
                    "ocr-1": "安卓下载\n苹果手机下载".encode(),
                }
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert client.calls == []
    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "guide_page"
    assert result["page_state"] == "stable"
    assert result["candidate_actions"] == []
    assert result["rule_matching_allowed"] is False
    assert result["confidence_source"] == "deterministic_structure"
    assert "deterministic_guide_page" in result["risk_flags"]


async def test_missing_primary_page_type_uses_strong_structure_fallback() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    output = _recognized_output("home").model_copy(update={"page_type": None})
    startup_dom = (
        b'<html><body><div class="loading-page">'
        b'<span class="down-time">close</span></div></body></html>'
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": startup_dom, "shot-1": PNG_BYTES}),
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "startup_ad"
    assert result["page_state"] == "countdown"
    assert result["confidence_source"] == "deterministic_structure"
    assert result["model_page_type"] is None
    assert result["rule_matching_allowed"] is True
    assert "deterministic_page_structure_provider_fallback" in result["risk_flags"]


async def test_invalid_optional_action_page_type_does_not_reject_valid_primary_type() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    payload = _recognized_output("content_list").model_dump()
    payload["candidate_actions"] = [
        {
            "action": "click",
            "target": "a.product",
            "target_keywords": ["商品"],
            "risk_level": "low",
            "purpose": "open_list_item",
            "expected_next_page_type": "unknown_navigation_type",
            "navigation_depth": 1,
        }
    ]
    client = FakeChatClient(PageDetectionModelOutput.model_validate(payload))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://example.com/catalog")
            ),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-1": b'<html><body><a class="product">Product</a></body></html>',
                    "shot-1": PNG_BYTES,
                }
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "content_list"
    assert result["rule_matching_allowed"] is True
    assert "uncatalogued_expected_page_type_dropped" in result["risk_flags"]
    assert all(item["target"] != "a.product" for item in result["candidate_actions"])


def test_unknown_expected_page_type_drops_entire_candidate() -> None:
    output = _recognized_output("home").model_copy(
        update={
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="未知入口",
                    risk_level="low",
                    purpose="验证目录外候选清洗",
                    expected_next_page_type="removed_page_type",
                )
            ]
        }
    )

    sanitized = page_detection_tools._sanitize_optional_catalog_references(
        output,
        load_default_page_type_catalog(),
    )

    assert sanitized.candidate_actions == []
    assert "uncatalogued_expected_page_type_dropped" in sanitized.risk_flags


def test_catalogued_and_untyped_actions_survive_optional_reference_sanitization() -> None:
    output = _recognized_output("home").model_copy(
        update={
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="我的",
                    risk_level="low",
                    purpose="验证目录内候选保留",
                    expected_next_page_type="profile",
                ),
                page_detection_tools.CandidateAction(
                    action="click",
                    target="关闭当前弹窗",
                    risk_level="low",
                    purpose="验证无页型弹窗候选保留",
                    expected_next_page_type=None,
                ),
            ]
        }
    )

    sanitized = page_detection_tools._sanitize_optional_catalog_references(
        output,
        load_default_page_type_catalog(),
    )

    assert [action.target for action in sanitized.candidate_actions] == [
        "我的",
        "关闭当前弹窗",
    ]


def test_skills_make_tool_result_the_only_decision_and_gate_rule_matching() -> None:
    root = Path(__file__).resolve().parents[1]
    detection = (root / "app/agents/skills/phase/page-detection/SKILL.md").read_text("utf-8")
    exploration = (root / "app/agents/skills/phase/page-exploration/SKILL.md").read_text("utf-8")
    workflow = (root / "app/agents/skills/qc-workflow/SKILL.md").read_text("utf-8")
    matching = (root / "app/agents/skills/rule/rule-matching/SKILL.md").read_text("utf-8")

    assert "page_detection_tool(page_capture_id)" in detection
    assert "唯一" in detection
    assert all(
        status in detection for status in ("recognized", "ambiguous", "insufficient", "error")
    )
    assert "page_detection_tool(page_capture_id)" in workflow
    assert workflow.index("page_detection_tool(page_capture_id)") < workflow.index(
        "candidate_rules_tool(page_type, page_capture_id)"
    )
    assert "rule_matching_allowed" in workflow
    assert "line_loading" in workflow
    assert "暂不执行补采" in workflow
    assert "evidence_insufficient" in workflow
    assert "当前版本不补采" in detection
    assert "不得重新 `goto`" in exploration
    assert "不得为补采主动追加" in exploration
    assert "detection_status" in matching
    assert "rule_matching_allowed" in matching
    assert "不主动补采" in matching


async def test_postgres_loader_double_guards_task_and_preserves_artifact_order() -> None:
    class FakeConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, tuple[str, str]]] = []

        async def fetchrow(self, query: str, *args: str) -> dict[str, Any]:
            self.calls.append(("fetchrow", query, args))
            return {
                "id": "capture-1",
                "task_id": "task-1",
                "url": "https://example.com",
                "metadata": {
                    "title": "首页",
                    "artifact_ids": ["dom-first", "shot-second"],
                },
                "action_status": "completed",
                "intent_payload": {
                    "action_type": "click",
                    "semantic_target": "internal:flutter_bottom_navigation:nth-of-type(1)",
                    "purpose": "进入视频列表",
                    "risk_level": "low",
                    "parameters": {
                        "target_mode": "bottom_navigation",
                        "navigation_family": "flutter_semantics",
                        "navigation_index": 1,
                        "navigation_label": "视频",
                        "expected_next_page_type": "video_list",
                    },
                },
                "result_payload": {
                    "execution_outcome": "succeeded",
                    "result_capture_id": "capture-1",
                    "state_diff": {
                        "before_location": "https://example.com",
                        "after_location": "https://example.com",
                        "transition_kind": "same_page_interaction",
                    },
                },
                "action_log": [
                    {
                        "action": "click",
                        "resolved_by": "flutter_bottom_navigation_verified_native_click",
                        "requested_selector": (
                            "internal:flutter_bottom_navigation_item:1:" + "a" * 16
                        ),
                        "before_url": "https://example.com",
                        "after_url": "https://example.com",
                        "target_before": {"text": "视频"},
                        "transition_kind": "same_page_interaction",
                        "navigation_effect_verified": True,
                    }
                ],
                "requested_source_page_type": "video_list",
                "requested_source_page_state": "popup",
                "requested_source_url": "https://example.com",
            }

        async def fetch(self, query: str, *args: str) -> list[dict[str, Any]]:
            self.calls.append(("fetch", query, args))
            return [
                {
                    "id": "shot-second",
                    "task_id": "task-1",
                    "page_capture_id": "capture-1",
                    "artifact_type": "screenshot",
                    "storage_path": "task/capture/shot.png",
                    "content_type": "image/png",
                    "checksum": None,
                    "size_bytes": 3,
                    "created_at": None,
                },
                {
                    "id": "dom-first",
                    "task_id": "task-1",
                    "page_capture_id": "capture-1",
                    "artifact_type": "dom",
                    "storage_path": "task/capture/dom.html",
                    "content_type": "text/html; charset=utf-8",
                    "checksum": None,
                    "size_bytes": 10,
                    "created_at": None,
                },
            ]

    class FakeAcquire:
        def __init__(self, connection: FakeConnection) -> None:
            self.connection = connection

        async def __aenter__(self) -> FakeConnection:
            return self.connection

        async def __aexit__(self, *args: Any) -> None:
            return None

    class FakePool:
        def __init__(self, connection: FakeConnection) -> None:
            self.connection = connection

        def acquire(self) -> FakeAcquire:
            return FakeAcquire(self.connection)

    connection = FakeConnection()
    loader = PostgresPageEvidenceLoader(pool=FakePool(connection))

    bundle = await loader.load(task_id="task-1", page_capture_id="capture-1")

    assert bundle is not None
    evidence_query = next(query for operation, query, _ in connection.calls if operation == "fetch")
    assert "'ui_snapshot'" in evidence_query
    assert [artifact.artifact_id for artifact in bundle.artifacts] == [
        "dom-first",
        "shot-second",
    ]
    assert bundle.capture_metadata is not None
    assert bundle.capture_metadata["_arrival_action_context"] == {
        "action_status": "completed",
        "action_type": "click",
        "semantic_target": "internal:flutter_bottom_navigation:nth-of-type(1)",
        "purpose": "进入视频列表",
        "risk_level": "low",
        "parameters": {
            "target_mode": "bottom_navigation",
            "navigation_family": "flutter_semantics",
            "navigation_index": 1,
            "navigation_label": "视频",
            "expected_next_page_type": "video_list",
        },
        "result_payload": {
            "execution_outcome": "succeeded",
            "result_capture_id": "capture-1",
            "state_diff": {
                "before_location": "https://example.com",
                "after_location": "https://example.com",
                "transition_kind": "same_page_interaction",
            },
        },
        "navigation_effect_verified": True,
        "requested_source_page_type": "video_list",
        "requested_source_page_state": "popup",
        "requested_source_url": "https://example.com",
    }
    capture_query = connection.calls[0][1]
    artifact_query = connection.calls[1][1]
    assert "id = $1::uuid" in capture_query
    assert "task_id = $2::uuid" in capture_query
    assert "requested_source_capture_id" in capture_query
    assert "result_capture_id = capture.id" in capture_query
    assert "capture.action_log" in capture_query
    assert "capture.task_id = $2::uuid" in artifact_query
    assert "evidence.task_id = $2::uuid" in artifact_query
    assert "'ui_snapshot'" in artifact_query
    assert "evidence.page_capture_id = $1::uuid" in artifact_query
    assert connection.calls[0][2] == ("capture-1", "task-1")
    assert connection.calls[1][2] == ("capture-1", "task-1")


def _sealed_dom_bottom_navigation_case() -> tuple[
    list[dict[str, Any]], dict[str, Any], dict[str, Any]
]:
    before_url = "https://example.com/"
    after_url = "https://example.com/home-video"
    before_identity = "sha256:" + "a" * 64
    after_identity = "sha256:" + "b" * 64
    selector = "div.footer-wrap:visible ul.type:visible > li.footer-item:nth-of-type(2):visible"
    proof = {
        "protocol_version": "bottom_navigation_transition_proof.v1",
        "status": "trusted",
        "effect": "same_origin_url_change",
        "before_location_hash": stable_h5_surface_hash(before_url),
        "after_location_hash": stable_h5_surface_hash(after_url),
        "origin_hash": stable_h5_surface_hash("https://example.com:443"),
        "navigation_family": "footer_wrap",
        "navigation_index": 2,
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
    }
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": "logical_page_changed",
        "logical_transition_proof": "trusted",
        "reason_code": "trusted_bottom_navigation_url_change",
        "before_identity_fingerprint": before_identity,
        "after_identity_fingerprint": after_identity,
        "bottom_navigation_proof": proof,
    }
    action_log = [
        {
            "action": "click",
            "selector": selector,
            "navigation_effect_verified": True,
            "navigation_effect_reason": "same_origin_url_change",
            "before_url": before_url,
            "after_url": after_url,
            "state_diff": {"logical_surface_transition": transition},
        }
    ]
    intent_payload = {
        "semantic_target": selector,
        "parameters": {
            "target_mode": "bottom_navigation",
            "navigation_family": "footer_wrap",
            "navigation_index": 2,
            "navigation_label": "视频",
        },
    }
    result_payload = {
        "state_diff": {
            "before_location": before_url,
            "after_location": after_url,
            "location_changed": True,
            "logical_surface_transition": transition,
        }
    }
    return action_log, intent_payload, result_payload


def test_verified_dom_bottom_navigation_effect_accepts_sealed_url_proof() -> None:
    action_log, intent_payload, result_payload = _sealed_dom_bottom_navigation_case()

    assert (
        _verified_bottom_navigation_effect(
            action_log=action_log,
            intent_payload=intent_payload,
            result_payload=result_payload,
        )
        is True
    )


@pytest.mark.parametrize(
    "tamper",
    [
        pytest.param("navigation_family", id="family"),
        pytest.param("after_location_hash", id="location-hash"),
        pytest.param("after_identity_fingerprint", id="identity-binding"),
    ],
)
def test_verified_dom_bottom_navigation_effect_rejects_tampered_proof(
    tamper: str,
) -> None:
    action_log, intent_payload, result_payload = _sealed_dom_bottom_navigation_case()
    result_payload = deepcopy(result_payload)
    transition = result_payload["state_diff"]["logical_surface_transition"]
    proof = transition["bottom_navigation_proof"]
    if tamper == "navigation_family":
        proof[tamper] = "vant"
    elif tamper == "after_location_hash":
        proof[tamper] = stable_h5_surface_hash("https://example.com/tampered")
    else:
        transition[tamper] = "sha256:" + "c" * 64

    assert (
        _verified_bottom_navigation_effect(
            action_log=action_log,
            intent_payload=intent_payload,
            result_payload=result_payload,
        )
        is False
    )


async def test_local_reader_rejects_path_outside_evidence_root(tmp_path: Path) -> None:
    reader = ConfiguredEvidenceArtifactReader(
        EvidenceStorageSettings(backend="local", local_root=str(tmp_path))
    )
    artifact = PageEvidenceArtifact(
        artifact_id="outside",
        artifact_type="dom",
        storage_path="../outside.html",
        content_type="text/html",
    )

    with pytest.raises(ValueError, match="越出本地证据根目录"):
        await reader.read(artifact)


async def test_minio_reader_retries_transient_read_error() -> None:
    class FakeMinioResponse:
        def __init__(self, content: bytes) -> None:
            self._content = content

        def read(self) -> bytes:
            return self._content

        def close(self) -> None:
            return None

        def release_conn(self) -> None:
            return None

    class FakeMinioClient:
        def __init__(self) -> None:
            self.calls = 0

        def get_object(self, bucket: str, object_name: str) -> FakeMinioResponse:
            assert bucket == "alioth"
            assert object_name == "task/capture/dom.html"
            self.calls += 1
            if self.calls == 1:
                raise ProtocolError("connection reset")
            return FakeMinioResponse(b"<html>ok</html>")

    reader = ConfiguredEvidenceArtifactReader(
        EvidenceStorageSettings(
            backend="minio",
            minio_endpoint="minio:9000",
            minio_access_key="access",
            minio_secret_key="secret",
            minio_bucket="alioth",
        )
    )
    client = FakeMinioClient()
    reader._minio_client = client
    artifact = PageEvidenceArtifact(
        artifact_id="dom",
        artifact_type="dom",
        storage_path="task/capture/dom.html",
        content_type="text/html",
    )

    assert await reader.read(artifact) == b"<html>ok</html>"
    assert client.calls == 2


async def test_corrupt_timed_capture_blocks_model_and_requests_recapture() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    sequence = _artifact("sequence", "screenshot", content_type="application/json")
    reader = FakeArtifactReader(
        {
            "dom-1": b"<html><main>home</main></html>",
            "sequence": json.dumps(
                {
                    "sequence": True,
                    "frames": [{"index": 0, "encoding": "base64", "data": "%%%invalid%%%"}],
                }
            ).encode(),
        }
    )
    client = FakeChatClient(_recognized_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, sequence)),
            artifact_reader=reader,
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "insufficient"
    assert result["rule_matching_allowed"] is False
    assert "unreadable_artifact:sequence" in result["missing_sources"]
    assert "screenshot" in result["missing_sources"]
    assert client.calls == []


async def test_model_failure_returns_error_and_blocks_rule_matching() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed with secret response")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "error"
    assert result["error_code"] == "MODEL_CALL_FAILED"
    assert result["rule_matching_allowed"] is False
    assert "secret response" not in json.dumps(result)


@pytest.mark.parametrize("navigation_effect_verified", [True, False])
async def test_model_failure_preserves_only_fully_verified_flutter_arrival(
    navigation_effect_verified: bool,
) -> None:
    """模型失败不能抹掉已验签到达，但该弱页型仍不得进入规则匹配。"""

    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider unavailable")

    capture_id = "22222222-2222-2222-2222-222222222222"
    dom = _artifact("dom-arrival", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-arrival", "screenshot", content_type="image/png")
    arrival_context = {
        "action_status": "completed",
        "action_type": "click",
        "semantic_target": "internal:flutter_bottom_navigation:nth-of-type(3)",
        "risk_level": "low",
        "parameters": {
            "target_mode": "bottom_navigation",
            "navigation_family": "flutter_semantics",
            "navigation_index": 3,
            "navigation_label": "海角",
            "expected_next_page_type": "post_list",
            "candidate_id": "h5-candidate:" + "a" * 64,
            "frontier_snapshot_id": "h5-task-frontier:" + "b" * 64,
        },
        "result_payload": {
            "execution_outcome": "succeeded",
            "result_capture_id": capture_id,
            "state_diff": {
                "before_location": "https://p1.example/",
                "after_location": "https://p1.example/",
                "transition_kind": "same_page_interaction",
            },
        },
        "navigation_effect_verified": navigation_effect_verified,
        "requested_source_page_type": "video_list",
        "requested_source_page_state": "stable",
        "requested_source_url": "https://p1.example/",
    }
    flutter_dom = (
        '<html><body><flt-semantics-host><flt-semantics flt-tappable="" role="text">'
        "海角</flt-semantics></flt-semantics-host></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id=capture_id,
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://p1.example/",
                    capture_metadata={
                        "capture_role": "after_action",
                        "_arrival_action_context": arrival_context,
                    },
                )
            ),
            artifact_reader=FakeArtifactReader(
                {"dom-arrival": flutter_dom, "shot-arrival": PNG_BYTES}
            ),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            nav_alias_resolver=FakeNavAliasResolver(
                {
                    "海角": PageNavAliasMatch(
                        canonical_code="post_list",
                        page_subtype_code=None,
                        matched_term="海角",
                        source_category="帖子",
                        confidence=1.0,
                        match_type="static",
                    )
                }
            ),
        )

    if not navigation_effect_verified:
        assert result["detection_status"] == "recognized"
        assert result["page_type"] == "home"
        assert result["confidence_source"] == "deterministic_structure"
        assert "verified_arrival_after_model_failure" not in result["risk_flags"]
        return

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "post_list"
    assert result["model_page_type"] is None
    assert result["confidence"] == 0.0
    assert result["confidence_source"] == "deterministic_refinement_cap"
    assert result["rule_matching_allowed"] is False
    assert "page_type_model" in result["missing_sources"]
    assert "verified_arrival_after_model_failure" in result["risk_flags"]


async def test_model_failure_keeps_structurally_proven_popup_dismissal() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider unavailable")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    dom_html = (
        b'<html><body><div class="van-overlay"></div>'
        b'<div role="dialog" class="van-popup van-popup--center">'
        b'<div class="ad-image"></div>'
        b'<div class="close_btn"><i class="van-icon van-icon-close"></i></div>'
        b"</div></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": dom_html, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "ambiguous"
    assert result["page_type"] is None
    assert result["page_state"] == "popup"
    assert result["rule_matching_allowed"] is False
    _assert_semantic_popup_action(result["candidate_actions"][0], "dialog_generic_close")
    assert "deterministic_provider_obstruction_fallback" in result["risk_flags"]


async def test_model_timeout_is_bounded_and_blocks_rule_matching() -> None:
    class SlowChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            await asyncio.sleep(0.05)
            return _recognized_output()

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=SlowChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            model_timeout_seconds=0.01,
        )

    assert result["detection_status"] == "error"
    assert result["error_code"] == "MODEL_CALL_TIMEOUT"
    assert result["rule_matching_allowed"] is False
    assert result["metrics"]["model_timeout_seconds"] == 0.01


async def test_model_timeout_uses_flutter_home_navigation_and_popup_structure() -> None:
    class SlowChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            await asyncio.sleep(0.05)
            return _recognized_output()

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    navigation_nodes = "".join(
        f"""<flt-semantics flt-tappable role="text"
        style="position:absolute;width:47.32px;height:74.88px;
        transform:matrix(1,0,0,1,{x},769.12);z-index:{index}">{label}</flt-semantics>"""
        for index, (x, label) in enumerate(
            (
                (8.84, "首页"),
                (73.84, "大厅"),
                (138.84, "雅间"),
                (203.84, "消息"),
                (268.84, "发布"),
                (333.84, "我的"),
            ),
            start=1,
        )
    )
    flutter_dom = f"""
    <html><body><flutter-view><flt-semantics-host>
      {navigation_nodes}
      <flt-semantics flt-tappable
        style="position:absolute;width:33.8px;height:33.8px;
        transform:matrix(1,0,0,1,178.1,269.9);z-index:2"></flt-semantics>
      <flt-semantics flt-tappable
        style="position:absolute;width:260px;height:260px;
        transform:matrix(1,0,0,1,65,314.1);z-index:3"></flt-semantics>
    </flt-semantics-host></flutter-view></body></html>
    """.encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://flutter.example/")
            ),
            artifact_reader=FakeArtifactReader({"dom-1": flutter_dom, "shot-1": PNG_BYTES}),
            chat_client=SlowChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            model_timeout_seconds=0.01,
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["page_state"] == "popup"
    assert result["confidence_source"] == "deterministic_structure"
    assert result["rule_matching_allowed"] is True
    assert "page_detection_model_timeout_fallback" in result["risk_flags"]
    assert len(result["candidate_actions"]) == 1
    candidate = result["candidate_actions"][0]
    assert candidate["action"] == "click"
    assert candidate["target"] == "关闭当前弹窗"
    assert candidate["obstruction_capability"] == ("flutter_centered_ad_popup_close_above")
    assert re.fullmatch(
        r"h5-obstruction:[0-9a-f]{64}",
        candidate["obstruction_signature"],
    )


async def test_model_timeout_uses_flutter_root_home_with_unlabeled_active_slot() -> None:
    class SlowChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            await asyncio.sleep(0.05)
            return _recognized_output()

    dom = _artifact("dom-home", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-home", "screenshot", content_type="image/png")
    navigation_nodes = [
        """<flt-semantics flt-tappable
        style="position:absolute;width:55.71px;height:57.2px;
        transform:matrix(1,0,0,1,0,771.2);z-index:1"></flt-semantics>"""
    ]
    navigation_nodes.extend(
        f"""<flt-semantics flt-tappable role="text"
        style="position:absolute;width:55.71px;height:57.2px;
        transform:matrix(1,0,0,1,{x},771.2);z-index:{index}">{label}</flt-semantics>"""
        for index, (x, label) in enumerate(
            (
                (55.71, "视频"),
                (111.43, "抖阴"),
                (167.14, "海角"),
                (222.86, "暗网"),
                (278.57, "吃瓜"),
                (334.29, "我的"),
            ),
            start=2,
        )
    )
    flutter_dom = f"""
    <html><body><flutter-view><flt-semantics-host>
      {"".join(navigation_nodes)}
    </flt-semantics-host></flutter-view></body></html>
    """.encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://flutter.example/")
            ),
            artifact_reader=FakeArtifactReader({"dom-home": flutter_dom, "shot-home": PNG_BYTES}),
            chat_client=SlowChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            model_timeout_seconds=0.01,
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["page_state"] == "stable"
    assert result["confidence_source"] == "deterministic_structure"
    assert result["rule_matching_allowed"] is True
    assert "page_detection_model_timeout_fallback" in result["risk_flags"]
    navigation = [
        item
        for item in result["candidate_actions"]
        if "bottom_navigation" in item["target_keywords"]
    ]
    assert navigation
    assert any(item["expected_next_page_type"] == "profile" for item in navigation)


async def test_model_timeout_uses_flutter_profile_navigation_and_settings_structure() -> None:
    class SlowChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            await asyncio.sleep(0.05)
            return _recognized_output()

    dom = _artifact("dom-profile", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-profile", "screenshot", content_type="image/png")
    navigation_nodes = "".join(
        f"""<flt-semantics flt-tappable role="text"
        style="position:absolute;width:47.32px;height:74.88px;
        transform:matrix(1,0,0,1,{x},769.12);z-index:{index}">{label}</flt-semantics>"""
        for index, (x, label) in enumerate(
            (
                (8.84, "首页"),
                (73.84, "大厅"),
                (138.84, "雅间"),
                (203.84, "消息"),
                (268.84, "发布"),
                (333.84, "我的"),
            ),
            start=1,
        )
    )
    flutter_dom = f"""
    <html><body><flutter-view><flt-semantics-host>
      {navigation_nodes}
      <flt-semantics aria-label="设置" flt-tappable role="group"
        style="position:absolute;width:356.72px;height:59.28px;
        transform:matrix(1,0,0,1,16.64,903.12);pointer-events:none;z-index:16">
        <flt-semantics-container style="position:absolute;pointer-events:none">
          <flt-semantics role="img" style="position:absolute;width:43.68px;
          height:43.68px;transform:matrix(1,0,0,1,15.6,7.8);
          pointer-events:all;z-index:1"></flt-semantics>
          <flt-semantics role="img" style="position:absolute;width:20.8px;
          height:20.8px;transform:matrix(1,0,0,1,320.32,19.24);
          pointer-events:all;z-index:2"></flt-semantics>
        </flt-semantics-container>
      </flt-semantics>
    </flt-semantics-host></flutter-view></body></html>
    """.encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://flutter.example/")
            ),
            artifact_reader=FakeArtifactReader(
                {"dom-profile": flutter_dom, "shot-profile": PNG_BYTES}
            ),
            chat_client=SlowChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            model_timeout_seconds=0.01,
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "profile"
    assert result["page_state"] == "stable"
    assert result["confidence_source"] == "deterministic_structure"
    assert "page_detection_model_timeout_fallback" in result["risk_flags"]
    settings = [
        item
        for item in result["candidate_actions"]
        if item["expected_next_page_type"] == "settings"
    ]
    assert len(settings) == 1
    assert settings[0]["target"] == "个人中心 Flutter 设置入口"


async def test_model_timeout_budget_includes_evidence_preparation() -> None:
    class SlowEvidenceLoader(FakeEvidenceLoader):
        async def load(self, *, task_id: str, page_capture_id: str) -> PageEvidenceBundle | None:
            await asyncio.sleep(0.02)
            return await super().load(task_id=task_id, page_capture_id=page_capture_id)

    class SlowChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            await asyncio.sleep(0.02)
            return SimpleNamespace(value=_recognized_output())

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=SlowEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=SlowChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=FakeNavAliasResolver({}),
            model_timeout_seconds=0.03,
        )

    assert result["detection_status"] == "error"
    assert result["error_code"] == "MODEL_CALL_TIMEOUT"
    assert result["rule_matching_allowed"] is False


async def test_model_timeout_does_not_wait_for_cancellation_resistant_client() -> None:
    class CancellationResistantChatClient:
        def __init__(self) -> None:
            self.finished = asyncio.Event()

        async def get_response(self, *_: Any, **__: Any) -> Any:
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                await asyncio.sleep(0.2)
                return SimpleNamespace(value=_recognized_output())
            finally:
                self.finished.set()

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    client = CancellationResistantChatClient()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            model_timeout_seconds=0.01,
        )

    assert result["detection_status"] == "error"
    assert result["error_code"] == "MODEL_CALL_TIMEOUT"
    assert client.finished.is_set() is False
    await asyncio.wait_for(client.finished.wait(), timeout=0.5)


async def test_model_failure_uses_deterministic_startup_ad_structure() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    startup_dom = (
        b'<html><body><div class="loading-page">'
        b'<span class="down-time">8s</span></div></body></html>'
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": startup_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "startup_ad"
    assert result["page_state"] == "countdown"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert result["model_confidence"] is None
    assert result["rule_matching_allowed"] is True
    assert "deterministic_startup_ad_provider_fallback" in result["risk_flags"]


async def test_model_stable_passive_splash_is_refined_to_bounded_reobservation() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    dom_html = (
        b'<html><body><div id="app"></div><div id="SplashScreenID">'
        b'<img src="/startup.jpg" alt=""></div></body></html>'
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": dom_html, "shot-1": PNG_BYTES}),
            chat_client=FakeChatClient(_recognized_output("startup_ad")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "startup_ad"
    assert result["page_state"] == "countdown"
    assert "deterministic_passive_startup_splash_reobserve" in result["risk_flags"]


async def test_model_failure_uses_video_player_route_and_structure() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    player_dom = (
        b'<html><body><main class="video-content">'
        b'<section class="video-player-box"><div class="xgplay"></div></section>'
        b'<div class="video-info"><h1 class="video-title">Movie</h1></div>'
        b"</main></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url=("https://example.com/moviePlayer?videoId=2067444648255741952"),
                )
            ),
            artifact_reader=FakeArtifactReader({"dom-1": player_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "video_detail"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert result["rule_matching_allowed"] is True
    assert "deterministic_page_structure_provider_fallback" in result["risk_flags"]


async def test_model_failure_uses_community_masonry_structure() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    community_dom = (
        b'<html><body><main class="data-list">'
        b'<section class="masonry-grid">'
        b'<article class="data-item">Community card</article>'
        b"</section></main></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/square",
                )
            ),
            artifact_reader=FakeArtifactReader({"dom-1": community_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "post_list"
    assert result["confidence_source"] == "deterministic_structure"
    assert result["rule_matching_allowed"] is True
    assert result["candidate_actions"] == []


async def test_model_failure_uses_unique_active_tabbar_structure() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    business_dom = (
        b'<html><body><main class="video-list">content</main>'
        b'<div class="van-tabbar">'
        b'<div class="van-tabbar-item">home</div>'
        b'<div class="van-tabbar-item van-tabbar-item--active">video</div>'
        b"</div></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": business_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "video_list"
    assert result["page_state"] == "stable"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert result["model_confidence"] is None
    assert result["rule_matching_allowed"] is True
    assert "deterministic_page_structure_provider_fallback" in result["risk_flags"]
    assert "deterministic_startup_ad_provider_fallback" not in result["risk_flags"]


async def test_model_failure_uses_home_product_structure_without_active_tabbar() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    business_dom = (
        '<html><body><div class="product-page flex-container">'
        '<div class="page-tabs van-tabs van-tabs--line">'
        '<div class="van-tabs__nav van-tabs__nav--line">'
        '<div role="tab" aria-selected="true" class="van-tab van-tab--active">'
        '<span>推荐</span></div><div role="tab" class="van-tab">直播</div>'
        "</div></div>"
        '<div class="van-tabbar van-tabbar--fixed">'
        '<div class="van-tabbar-item"><span>导航站</span></div>'
        '<div class="van-tabbar-item"><span>视频</span></div>'
        '<div class="van-tabbar-item"><span>广场</span></div>'
        '<div class="van-tabbar-item"><span>我的</span></div>'
        "</div></div></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://example.com/")
            ),
            artifact_reader=FakeArtifactReader({"dom-1": business_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["page_state"] == "stable"
    assert result["confidence_source"] == "deterministic_structure"
    assert result["rule_matching_allowed"] is True


async def test_model_failure_uses_root_url_for_home_without_business_navigation() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    business_dom = (
        '<html><body><div class="product-page">'
        '<div class="van-tabs__nav"><div role="tab" aria-selected="true">推荐</div></div>'
        '<div class="van-tabbar"><div class="van-tabbar-item">unknown</div></div>'
        "</div></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://example.com/")
            ),
            artifact_reader=FakeArtifactReader({"dom-1": business_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert result["page_state"] == "stable"
    assert result["confidence_source"] == "deterministic_structure"


async def test_model_failure_uses_business_route_for_video_list() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    business_dom = (
        '<html><body><main class="video-list">'
        '<a class="movie-card" href="/movie/123">video</a>'
        '</main><div class="van-tabbar">'
        '<div class="van-tabbar-item">91娱乐</div>'
        '<div class="van-tabbar-item">91看片</div>'
        '<div class="van-tabbar-item">91私库</div>'
        "</div></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(dom, screenshot, page_url="https://example.com/movie")
            ),
            artifact_reader=FakeArtifactReader({"dom-1": business_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "video_list"
    assert result["page_state"] == "stable"
    assert result["confidence_source"] == "deterministic_structure"
    assert not any(
        action.get("expected_next_page_type") == "video_detail"
        for action in result["candidate_actions"]
    )
    assert "业务路由" in result["reason"]
    assert result["matched_catalog_signals"]["dom_hints"] == ["business_route:movie"]
    assert "deterministic_page_structure_provider_fallback" in result["risk_flags"]


async def test_model_failure_uses_query_video_route_for_video_detail() -> None:
    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    detail_dom = (
        '<html><body><main class="video-content">'
        "<h1>视频标题</h1><div>播放线路01</div><section>推荐视频</section>"
        "</main></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/play?videoId=123&categoryId=9",
                )
            ),
            artifact_reader=FakeArtifactReader({"dom-1": detail_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "video_detail"
    assert result["page_state"] == "stable"
    assert result["rule_matching_allowed"] is True
    assert "deterministic_page_structure_provider_fallback" in result["risk_flags"]


async def test_model_failure_uses_catalog_detail_route_and_returns_to_source() -> None:
    """目录登记的独立详情路由应在模型失败时恢复识别并签发安全返回。"""

    class FailingChatClient:
        async def get_response(self, *_: Any, **__: Any) -> Any:
            raise RuntimeError("provider failed")

    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    detail_dom = (
        '<html><body><main class="square-detail">'
        "<h1>帖子标题</h1><section>评论</section>"
        "</main></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/squareDetail?dynamicId=123",
                )
            ),
            artifact_reader=FakeArtifactReader({"dom-1": detail_dom, "shot-1": PNG_BYTES}),
            chat_client=FailingChatClient(),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "content_detail"
    assert result["page_state"] == "stable"
    assert result["confidence_source"] == "deterministic_structure"
    assert result["rule_matching_allowed"] is True
    assert result["candidate_actions"][0] == {
        "action": "back",
        "target": "browser_history",
        "target_keywords": ["nested_return"],
        "risk_level": "low",
        "purpose": "详情截图与 DOM 采集完成后立即返回来源页",
        "expected_next_page_type": None,
        "navigation_depth": 1,
        "obstruction_capability": None,
        "obstruction_signature": None,
    }


async def test_content_policy_failure_retries_without_raw_dom_text() -> None:
    class ContentPolicyThenSuccessClient:
        def __init__(self) -> None:
            self.calls: list[list[Message]] = []

        async def get_response(
            self,
            messages: list[Message],
            **__: Any,
        ) -> Any:
            self.calls.append(messages)
            if len(self.calls) == 1:
                raise RuntimeError("content_policy_violation")
            return SimpleNamespace(value=_recognized_output("home"))

    client = ContentPolicyThenSuccessClient()
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    raw_dom = (
        b'<html><body class="home-shell"><nav role="navigation">'
        b"RAW_POLICY_BLOCKED_TEXT</nav></body></html>"
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader({"dom-1": raw_dom, "shot-1": PNG_BYTES}),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "home"
    assert "provider_content_policy_fallback" in result["risk_flags"]
    assert len(client.calls) == 2
    fallback_text = "\n".join(
        content.text or ""
        for message in client.calls[1]
        for content in message.contents
        if content.type == "text"
    )
    assert "RAW_POLICY_BLOCKED_TEXT" not in fallback_text
    assert "home-shell" in fallback_text
    assert any(
        content.type == "data" for message in client.calls[1] for content in message.contents
    )


def test_non_object_timed_capture_sequence_is_rejected() -> None:
    artifact = _artifact("sequence", "screenshot", content_type="application/json")

    with pytest.raises(ValueError, match="截图序列包结构无效"):
        _decode_screenshot_artifact(artifact, b"[]")


async def test_invalid_png_blocks_model_call() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    client = FakeChatClient(_recognized_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home</html>", "shot-1": b"not-a-png"}
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "insufficient"
    assert "unreadable_artifact:shot-1" in result["missing_sources"]
    assert client.calls == []


async def test_invalid_utf8_dom_blocks_model_call() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    client = FakeChatClient(_recognized_output())

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home\xff</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "insufficient"
    assert "unreadable_artifact:dom-1" in result["missing_sources"]
    assert client.calls == []


async def test_model_cannot_echo_raw_dom_or_base64_into_tool_result() -> None:
    class RawChatClient:
        def __init__(self, value: dict[str, Any]) -> None:
            self.value = value

        async def get_response(self, *_: Any, **__: Any) -> Any:
            return SimpleNamespace(value=self.value)

    malicious_output = _recognized_output().model_dump(mode="json")
    malicious_output["reason"] = "data:image/png;base64,LEAKED_IMAGE"
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=RawChatClient(malicious_output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    encoded = json.dumps(result, ensure_ascii=False)
    assert result["detection_status"] == "error"
    assert result["error_code"] == "MODEL_CALL_FAILED"
    assert "LEAKED_IMAGE" not in encoded
    assert "data:image" not in encoded


async def test_catalog_skip_does_not_echo_invalid_model_value() -> None:
    dom = _artifact("dom-1", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-1", "screenshot", content_type="image/png")
    client = FakeChatClient(_recognized_output("unknown_page"))

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {"dom-1": b"<html>home</html>", "shot-1": PNG_BYTES}
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "unrecognized"
    assert result["catalog_disposition"] == "skipped"
    assert result.get("error_code") is None
    assert "unknown_page" not in json.dumps(result, ensure_ascii=False)


async def test_page_detection_does_not_send_over_budget_request(monkeypatch) -> None:
    dom = _artifact("dom-budget", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-budget", "screenshot", content_type="image/png")
    client = FakeChatClient(_recognized_output("home"))
    monkeypatch.setattr(
        page_detection_tools,
        "get_settings",
        lambda: SimpleNamespace(
            quality_optimization=SimpleNamespace(
                prompt_input_token_budget=1_000,
                pipeline_mode="off",
            )
        ),
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-budget",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, screenshot)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-budget": ("<main>" + "页面内容" * 100_000 + "</main>").encode(),
                    "shot-budget": PNG_BYTES,
                }
            ),
            chat_client=client,
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "insufficient"
    assert result["missing_sources"] == ["prompt_input_token_budget"]
    assert client.calls == []


async def test_active_tabbar_refinement_uses_injected_nav_alias_resolver() -> None:
    dom = _artifact("dom-alias", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-alias", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/app"))
    reader = FakeArtifactReader(
        {
            "dom-alias": (
                '<html><body><div class="van-tabbar">'
                '<div class="van-tabbar-item">首页</div>'
                '<div class="van-tabbar-item van-tabbar-item--active">神秘短剧</div>'
                "</div></body></html>"
            ).encode(),
            "shot-alias": PNG_BYTES,
        }
    )
    resolver = FakeNavAliasResolver(
        {
            "神秘短剧": PageNavAliasMatch(
                canonical_code="content_list",
                page_subtype_code="short_drama",
                matched_term="神秘短剧",
                source_category="短剧",
                confidence=0.93,
                match_type="exact",
            )
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=resolver,
        )

    assert result["page_type"] == "content_list"
    assert "神秘短剧" in resolver.calls


@pytest.mark.parametrize(
    ("detection_status", "page_state"),
    [
        ("ambiguous", "transient_loading"),
        ("insufficient", "partially_loaded"),
    ],
)
async def test_ready_business_page_promotes_uncertain_dynamic_active_tab(
    detection_status: str,
    page_state: str,
) -> None:
    dom = _artifact("dom-ai", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-ai", "screenshot", content_type="image/png")
    output = _ambiguous_output().model_copy(
        update={
            "detection_status": detection_status,
            "page_state": page_state,
            "candidate_actions": [
                page_detection_tools.CandidateAction(
                    action="click",
                    target="提交",
                    target_keywords=["提交"],
                    risk_level="high",
                    purpose="提交图片处理请求",
                )
            ],
        }
    )
    business_dom = (
        '<html><body><main class="ai-tool-page">'
        '<form><input type="file"><button>提交</button></form></main>'
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item">首页</div>'
        '<div class="van-tabbar-item">视频</div>'
        '<div class="van-tabbar-item">广场</div>'
        '<div class="van-tabbar-item van-tabbar-item--active">AI科技</div>'
        '<div class="van-tabbar-item">我的</div>'
        "</div></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-ai",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/ai",
                    capture_metadata={
                        "pipeline_metrics": {
                            "page_settle_complete": False,
                            "page_inspection_ready": True,
                        }
                    },
                )
            ),
            artifact_reader=FakeArtifactReader({"dom-ai": business_dom, "shot-ai": PNG_BYTES}),
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "content_list"
    assert result["page_state"] == "stable"
    assert result["rule_matching_allowed"] is True
    assert "deterministic_active_tab_page_type_refinement" in result["risk_flags"]
    assert "deterministic_ready_business_page_state_refinement" in result["risk_flags"]
    assert any(
        action["expected_next_page_type"] == "profile" for action in result["candidate_actions"]
    )


async def test_ai_undress_route_promotes_content_list_when_model_is_ambiguous() -> None:
    dom = _artifact("dom-ai-undress", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-ai-undress", "screenshot", content_type="image/png")
    output = _ambiguous_output().model_copy(update={"page_state": "stable"})
    business_dom = (
        '<html><body><main class="ai-tool-page">'
        '<section class="content-list"><article class="card">AI 脱衣</article></section>'
        "</main></body></html>"
    ).encode()

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-ai-undress",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/aiUndress2",
                    page_title="AI 脱衣",
                )
            ),
            artifact_reader=FakeArtifactReader(
                {"dom-ai-undress": business_dom, "shot-ai-undress": PNG_BYTES}
            ),
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "content_list"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert result["rule_matching_allowed"] is True
    assert "deterministic_business_route_page_type_refinement" in result["risk_flags"]


async def test_short_video_route_overrides_conflicting_model_with_deterministic_confidence(
) -> None:
    dom = _artifact("dom-short-video", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-short-video", "screenshot", content_type="image/png")
    model_payload = _recognized_output("video_detail").model_dump()
    model_payload["confidence"] = 0.93
    model_payload["page_type_candidates"] = [
        {
            "page_type": "video_detail",
            "confidence": 0.93,
            "matched_signal_count": 4,
        },
        {
            "page_type": "video_list",
            "confidence": 0.28,
            "matched_signal_count": 2,
        },
    ]

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-short-video",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(
                _bundle(
                    dom,
                    screenshot,
                    page_url="https://example.com/shortVideo",
                )
            ),
            artifact_reader=FakeArtifactReader(
                {
                    "dom-short-video": (
                        b'<main class="short-video-list"><article class="video-card">'
                        b"video</article></main>"
                    ),
                    "shot-short-video": PNG_BYTES,
                }
            ),
            chat_client=FakeChatClient(
                PageDetectionModelOutput.model_validate(model_payload)
            ),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "video_list"
    assert result["confidence"] == 1.0
    assert result["confidence_source"] == "deterministic_structure"
    assert "deterministic_business_route_page_type_refinement" in result["risk_flags"]


async def test_page_detection_audits_vector_navigation_knowledge_and_candidate_effect() -> None:
    dom = _artifact("dom-vector", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-vector", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/app"))
    reader = FakeArtifactReader(
        {
            "dom-vector": (
                '<html><body><div class="van-tabbar">'
                '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
                '<div class="van-tabbar-item">神秘漫画馆</div>'
                "</div></body></html>"
            ).encode(),
            "shot-vector": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=PageNavAliasResolver(repository=VectorNavAliasRepository()),
        )

    audit = result.get("navigation_knowledge")
    assert isinstance(audit, dict)
    assert audit == {
        "visible_label_count": 2,
        "match_count": 2,
        "match_type_counts": {"static": 1, "exact": 0, "vector": 1},
        "repository_configured": True,
        "exact_lookup_attempt_count": 1,
        "vector_lookup_attempt_count": 1,
        "repository_degraded": False,
        "degraded_events": [],
    }
    vector_candidate = next(
        item
        for item in result["candidate_actions"]
        if item.get("expected_next_page_type") == "content_list"
    )
    assert vector_candidate["navigation_knowledge_source"] == "vector"
    assert vector_candidate["navigation_knowledge_confidence"] == 0.88
    assert result["metrics"]["nav_alias_vector_match_count"] == 1
    assert result["metrics"]["nav_alias_vector_lookup_attempt_count"] == 1


def test_candidate_navigation_knowledge_cannot_be_self_asserted_by_model() -> None:
    forged = page_detection_tools.CandidateAction(
        action="click",
        target="未知入口",
        risk_level="low",
        purpose="模型自报知识来源",
        expected_next_page_type="content_list",
        navigation_knowledge_source="contextual",
        navigation_knowledge_confidence=1.0,
    )

    result = page_detection_tools._with_navigation_knowledge_candidate_metadata(
        candidate_actions=[forged],
        nav_alias_matches={},
    )

    assert result[0].navigation_knowledge_source is None
    assert result[0].navigation_knowledge_confidence is None


def test_visible_tabbar_navigation_uses_resolved_alias_snapshot() -> None:
    actions = page_detection_tools._visible_tabbar_navigation_actions(
        '<div class="van-tabbar">'
        '<div class="van-tabbar-item van-tabbar-item--active">首页</div>'
        '<div class="van-tabbar-item">神秘短剧</div>'
        "</div>",
        nav_alias_matches={
            "神秘短剧": PageNavAliasMatch(
                canonical_code="content_list",
                page_subtype_code="short_drama",
                matched_term="神秘短剧",
                source_category="短剧",
                confidence=0.93,
                match_type="vector",
            )
        },
    )

    assert len(actions) == 1
    assert actions[0].expected_next_page_type == "content_list"
    assert "神秘短剧" in actions[0].target_keywords


def test_h5_page_tab_navigation_uses_resolved_alias_snapshot() -> None:
    profile = H5ExplorationProfile(
        page_tab_items=[
            H5TabbarItem(
                label="短剧",
                selector=".van-tabs:visible .van-tab:nth-of-type(2):visible",
                active=False,
                expected_page_type=None,
            )
        ]
    )

    actions = page_detection_tools._with_h5_profile_page_tab_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        profile=profile,
        nav_alias_matches={
            "短剧": PageNavAliasMatch(
                canonical_code="content_list",
                page_subtype_code="short_drama",
                matched_term="短剧",
                source_category="短剧",
                confidence=0.87,
                match_type="vector",
            )
        },
    )

    assert len(actions) == 1
    assert actions[0].expected_next_page_type == "content_list"
    assert actions[0].target_keywords[-1] == "content_list"


def test_h5_page_tab_navigation_keeps_same_page_type_business_variant() -> None:
    profile = H5ExplorationProfile(
        page_tab_items=[
            H5TabbarItem(
                label="国产",
                selector=".van-tabs:visible .van-tab:nth-of-type(3):visible",
                active=False,
                expected_page_type=None,
            )
        ]
    )

    actions = page_detection_tools._with_h5_profile_page_tab_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        profile=profile,
    )
    actions = page_detection_tools._with_navigation_knowledge_candidate_metadata(
        candidate_actions=actions,
        nav_alias_matches={},
    )

    assert len(actions) == 1
    assert actions[0].expected_next_page_type == "home"
    assert actions[0].target_keywords == [
        "page_tab_navigation",
        "国产",
        "home",
        "same_surface_variant",
    ]
    assert actions[0].navigation_knowledge_source == "contextual"
    assert actions[0].navigation_knowledge_confidence == 1.0


def test_h5_profile_page_tab_does_not_duplicate_adaptive_candidate() -> None:
    """同一现场页签已由自适应结构签发后，Profile 不得再次投影。"""
    adaptive = page_detection_tools.CandidateAction(
        action="click",
        target="internal:page_tab:nth-of-type(3)",
        target_keywords=[
            "page_tab_navigation",
            "国产",
            "adaptive_page_tab",
            "interaction_pattern:candidate",
            "home",
        ],
        risk_level="low",
        purpose="探索当前页面现场识别出的页内栏目分支",
        expected_next_page_type="home",
        navigation_depth=1,
        interaction_pattern_signature="a" * 64,
        interaction_pattern_status="candidate",
    )
    profile = H5ExplorationProfile(
        page_tab_items=[
            H5TabbarItem(
                label="国产",
                selector=".van-tabs:visible .van-tab:nth-of-type(3):visible",
                active=False,
                expected_page_type="home",
            )
        ]
    )

    actions = page_detection_tools._with_h5_profile_page_tab_navigation_candidates(
        page_type="home",
        page_state="stable",
        candidate_actions=[adaptive],
        profile=profile,
    )

    assert actions == [adaptive]


@pytest.mark.parametrize(
    ("page_type", "page_url", "dom_payload"),
    [
        (
            "home",
            "https://example.com/home",
            (
                '<html><body><main class="home-page">'
                '<div class="heroSearchBox" role="button" aria-label="Open search"></div>'
                "</main></body></html>"
            ),
        ),
        (
            "video_list",
            "https://example.com/videos",
            (
                '<html><body><main class="video-list">'
                '<button data-testid="search-btn" class="toolbarIcon magnifier"></button>'
                "</main></body></html>"
            ),
        ),
        (
            "home",
            "https://example.com/home",
            (
                '<html><body><main class="home-page">'
                '<search role="button" aria-label="open"></search>'
                "</main></body></html>"
            ),
        ),
        (
            "video_list",
            "https://example.com/videos",
            (
                '<html><body><main class="video-list">'
                '<i class="van-icon van-icon-search"></i>'
                "</main></body></html>"
            ),
        ),
    ],
)
async def test_search_attribute_entry_does_not_add_ranking_candidate(
    page_type: str,
    page_url: str,
    dom_payload: str,
) -> None:
    dom = _artifact("dom-ranking-entry", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-ranking-entry", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url=page_url))
    reader = FakeArtifactReader(
        {
            "dom-ranking-entry": dom_payload.encode(),
            "shot-ranking-entry": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output(page_type)),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(item["expected_next_page_type"] != "ranking" for item in result["candidate_actions"])


@pytest.mark.parametrize(
    ("page_url", "dom_payload"),
    [
        (
            "https://example.com/home",
            (
                '<html><body><main class="home-page">'
                '<a href="/ranking" aria-label="视频排行榜">排行榜</a>'
                "</main></body></html>"
            ),
        ),
        (
            "https://example.com/videos",
            (
                '<html><body><main class="video-list">'
                '<button class="leaderboard-entry">视频榜单</button>'
                "</main></body></html>"
            ),
        ),
    ],
)
async def test_explicit_ranking_entry_does_not_add_removed_page_type_candidate(
    page_url: str,
    dom_payload: str,
) -> None:
    dom = _artifact("dom-ranking-entry", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-ranking-entry", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url=page_url))
    reader = FakeArtifactReader(
        {
            "dom-ranking-entry": dom_payload.encode(),
            "shot-ranking-entry": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(item["expected_next_page_type"] != "ranking" for item in result["candidate_actions"])


def test_semantic_entry_class_selector_drops_malformed_tag_prefix() -> None:
    element = page_detection_tools._VisibleSemanticEntryElement(
        tag="di",
        attributes={"class": "van-safe-area-top"},
        text="",
        order=0,
    )

    assert page_detection_tools._semantic_entry_class_selector(element) == (
        ".van-safe-area-top:visible"
    )


async def test_layout_safe_area_top_does_not_add_ranking_candidate() -> None:
    dom = _artifact("dom-safe-area-top", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-safe-area-top", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/home"))
    reader = FakeArtifactReader(
        {
            "dom-safe-area-top": (
                b'<html><body><main class="home-page">'
                b'<div class="van-safe-area-top"></div>'
                b"</main></body></html>"
            ),
            "shot-safe-area-top": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(item["expected_next_page_type"] != "ranking" for item in result["candidate_actions"])


@pytest.mark.parametrize(
    "class_name",
    ["photo-filter", "desktop-mode"],
)
async def test_short_ranking_markers_do_not_match_inside_unrelated_attribute_words(
    class_name: str,
) -> None:
    dom = _artifact("dom-short-marker", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-short-marker", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/videos"))
    reader = FakeArtifactReader(
        {
            "dom-short-marker": (
                f'<html><body><main class="video-list">'
                f'<button class="{class_name}">Filter</button>'
                f"</main></body></html>"
            ).encode(),
            "shot-short-marker": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("video_list")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(item["expected_next_page_type"] != "ranking" for item in result["candidate_actions"])


async def test_metadata_keyword_tag_does_not_add_ranking_candidate() -> None:
    dom = _artifact("dom-meta-keywords", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-meta-keywords", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/home"))
    reader = FakeArtifactReader(
        {
            "dom-meta-keywords": (
                b'<html><head><meta name="keywords" content="hot search top"></head>'
                b'<body><main class="home-page">Home</main></body></html>'
            ),
            "shot-meta-keywords": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(item["expected_next_page_type"] != "ranking" for item in result["candidate_actions"])


async def test_disabled_search_attribute_entry_does_not_add_ranking_candidate() -> None:
    dom = _artifact("dom-disabled-search", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-disabled-search", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/home"))
    reader = FakeArtifactReader(
        {
            "dom-disabled-search": (
                b'<html><body><main class="home-page">'
                b'<button class="search-btn" aria-disabled="true"></button>'
                b'<input type="search" disabled placeholder="search">'
                b"</main></body></html>"
            ),
            "shot-disabled-search": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(item["expected_next_page_type"] != "ranking" for item in result["candidate_actions"])


async def test_profile_high_risk_setting_like_entry_does_not_add_settings_candidate() -> None:
    dom = _artifact("dom-risky-settings", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-risky-settings", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/profile"))
    reader = FakeArtifactReader(
        {
            "dom-risky-settings": (
                b'<html><body><main class="profile-page">'
                b'<button class="settings-payment wallet-entry">Account settings payment</button>'
                b'<button class="setting-logout">settings logout</button>'
                b"</main></body></html>"
            ),
            "shot-risky-settings": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(
        item["expected_next_page_type"] != "settings" for item in result["candidate_actions"]
    )


async def test_search_attribute_entry_on_non_source_page_does_not_add_ranking_candidate() -> None:
    dom = _artifact("dom-content-search", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-content-search", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/content"))
    reader = FakeArtifactReader(
        {
            "dom-content-search": (
                b'<html><body><main class="content-list">'
                b'<button class="search-btn" aria-label="Open search"></button>'
                b"</main></body></html>"
            ),
            "shot-content-search": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("content_list")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert all(item["expected_next_page_type"] != "ranking" for item in result["candidate_actions"])


async def test_profile_setting_attribute_entry_adds_settings_candidate() -> None:
    dom = _artifact("dom-settings-entry", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-settings-entry", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/profile"))
    reader = FakeArtifactReader(
        {
            "dom-settings-entry": (
                b'<html><body><main class="profile-page">'
                b'<button class="userGear iconSetting" aria-label="account settings"></button>'
                b'<a href="/profile/edit" data-testid="profile-edit-link">Edit profile</a>'
                b"</main></body></html>"
            ),
            "shot-settings-entry": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    settings_action = next(
        item
        for item in result["candidate_actions"]
        if item["expected_next_page_type"] == "settings"
    )
    assert settings_action["action"] == "click"
    assert "settings_entry" in settings_action["target_keywords"]
    assert "profile_local_action" in settings_action["target_keywords"]
    assert "evidence:attributes" in settings_action["target_keywords"]


async def test_profile_btn_edit_attribute_entry_adds_settings_candidate() -> None:
    dom = _artifact("dom-btn-edit-entry", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-btn-edit-entry", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/profile"))
    reader = FakeArtifactReader(
        {
            "dom-btn-edit-entry": (
                b'<html><body><main class="profile-page">'
                b'<span class="btn-edit"><span class="icon-edit"></span></span>'
                b"</main></body></html>"
            ),
            "shot-btn-edit-entry": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    settings_action = next(
        item
        for item in result["candidate_actions"]
        if item["expected_next_page_type"] == "settings"
    )
    assert settings_action["action"] == "click"
    assert "settings_entry" in settings_action["target_keywords"]
    assert "profile_local_action" in settings_action["target_keywords"]
    assert "evidence:attributes" in settings_action["target_keywords"]


def test_profile_options_right_image_outranks_hidden_svg_setting_symbol() -> None:
    """SVG 定义不是入口；实测设置事件绑定在 options/right 的 img 自身。"""
    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<svg><defs><symbol id="icon-acg_image_setting"><path></path></symbol></defs></svg>'
            '<div class="options"><div class="left"><img></div>'
            '<div class="right"><img alt=""></div></div>'
        ),
        page_url="https://example.test/mine",
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心设置图标"
    assert actions[0].expected_next_page_type == "settings"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_setting_class_image_adds_structural_settings_candidate() -> None:
    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine"><header class="header"><div class="top">'
            '<div class="left"><img class="chat"><img class="setting"></div>'
            "</div></header></main>"
        ),
        page_url="https://h5.bkh052.com/mine",
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心类名设置图标"
    assert actions[0].expected_next_page_type == "settings"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_top_right_icon_setting_adds_structural_settings_candidate() -> None:
    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="page-wraper"><div class="page-container">'
            '<section class="base-info"><div class="top-wrap">'
            '<div class="top-right-menu"><img class="icon icon-setting" '
            'src="/static/img/setting.png"></div></div></section>'
            '<section class="recommend"><img class="icon icon-setting" '
            'src="/static/img/setting-ad.png"></section></div></main>'
        ),
        page_url="https://35.229.238.103:2018/profile",
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心右上设置图标"
    assert actions[0].expected_next_page_type == "settings"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_payment_accepts_dxbb_same_node_payment_image_structure() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main><div class="vip_box payment_me_tab">'
            '<div class="lazy_img vipBoxsBgImg"><div><img src="/vip.png"></div></div>'
            '<div class="payment_countdown"><div role="timer">23:56:55</div></div>'
            "</div></main>"
        ),
        page_url="https://dxbbmcw6gltgg.cloudfront.net/mine",
        image_count=1,
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心会员支付入口"
    assert actions[0].expected_next_page_type == "payment"
    assert "evidence:structure" in actions[0].target_keywords
    assert "capability:payment_me_tab_same_node_image" in actions[0].target_keywords


def test_profile_payment_accepts_vip_center_image_structure() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main><div class="img-menus-wrap"><div class="left">'
            '<img class="img" src="/static/img/my-vip-center.a83f5aad.png">'
            '</div><div class="right"><img class="img" '
            'src="/static/img/my-wallet.52ce.png"></div></div>'
            '<div class="recommend"><img class="img" '
            'src="/static/img/my-vip-center-ad.png"></div></main>'
        ),
        page_url="https://35.229.238.103:2018/profile",
        image_count=3,
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心会员支付入口"
    assert actions[0].expected_next_page_type == "payment"
    assert "evidence:structure" in actions[0].target_keywords
    assert "capability:profile_vip_center_image" in actions[0].target_keywords


def test_profile_five_site_prefers_immediate_open_image_over_vip_center() -> None:
    """站点五同时存在两个会员入口时，按固定优先级选择“立即开通”。"""
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-page"><section class="vip-promo">'
            '<img class="vip-promo__button" alt="立即开通" src="/open-vip.png">'
            '</section><section class="quick-card-row">'
            '<img class="quick-card" alt="会员中心" src="/vip-center.png">'
            '<img class="quick-card" alt="钻石钱包" src="/wallet.png">'
            "</section></main>"
        ),
        page_url="https://example.test/mine",
        image_count=3,
    )

    assert len(actions) == 1
    assert actions[0].expected_next_page_type == "payment"
    assert "entry:立即开通" in actions[0].target_keywords
    assert "capability:vip_promo_immediate_open_image" in actions[0].target_keywords


def test_profile_site_three_prefers_real_immediate_open_structure_over_vip_icon() -> None:
    """站3真实 DOM 同时存在两个会员入口时，优先签发“立即开通”。"""
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-content"><div class="vip-center mine-vip-banner">'
            '<img class="mine-vip-banner__dec" src="/assets/vip-dec.png" alt="">'
            '<div class="mine-vip-banner__btn">立即开通</div></div>'
            '<div class="my-balance-wrap">'
            '<img src="/assets/mine-gold-icon-BYUQdoS-.png">'
            '<img src="/assets/mine-vip-icon-C6xv1WRC.png"></div></main>'
        ),
        page_url="http://d24jdtq5qz8ff4.cloudfront.net/profile",
        image_count=3,
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心会员支付入口"
    assert "entry:立即开通" in actions[0].target_keywords
    assert "capability:mine_vip_banner_immediate_open_text" in actions[0].target_keywords


def test_profile_site_three_accepts_real_member_center_icon_structure() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-content"><div class="my-balance-wrap">'
            '<img src="/assets/mine-gold-icon-BYUQdoS-.png">'
            '<img src="/assets/mine-vip-icon-C6xv1WRC.png"></div></main>'
        ),
        page_url="http://d24jdtq5qz8ff4.cloudfront.net/profile",
        image_count=2,
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心会员支付入口"
    assert "entry:会员中心" in actions[0].target_keywords
    assert "capability:mine_vip_center_image" in actions[0].target_keywords


def test_profile_site_three_rejects_nested_immediate_open_button() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-content"><div class="vip-center mine-vip-banner">'
            '<div class="nested"><div class="mine-vip-banner__btn">立即开通</div>'
            "</div></div></main>"
        ),
        page_url="http://d24jdtq5qz8ff4.cloudfront.net/profile",
        image_count=1,
    )

    assert actions == []


def test_profile_site_three_hidden_immediate_open_text_is_not_detected() -> None:
    actions = page_detection_tools._with_profile_payment_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-content"><div class="vip-center mine-vip-banner">'
            '<div class="mine-vip-banner__btn"><span hidden>立即开通</span></div>'
            "</div></main>"
        ),
        page_url="http://d24jdtq5qz8ff4.cloudfront.net/profile",
        image_count=0,
    )

    assert actions == []


def test_profile_setting_class_image_outside_header_is_not_trusted() -> None:
    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-page"><section class="recommend">'
            '<img class="setting"></section></main>'
        ),
        page_url="https://example.test/mine",
    )

    assert actions == []


def test_profile_header_left_second_image_adds_structural_settings_candidate() -> None:
    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-page-wrap"><div class="index-header">'
            '<div class="mine-msg-entry"><img class="wh-24"></div>'
            '<div class="header-left"><img class="mr-15 wh-24">'
            '<img class="wh-24"></div></div></main>'
        ),
        page_url="https://d3jsxm6ahe66k1.cloudfront.net/Mine",
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心头部设置图标"
    assert actions[0].expected_next_page_type == "settings"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_five_site_header_settings_image_adds_candidate() -> None:
    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<header class="mine-header"><div class="mine-header__actions">'
            '<button class="icon-button"><img class="icon-button__img" alt="客服"></button>'
            '<button class="icon-button"><img class="icon-button__img" alt="设置"></button>'
            "</div></header>"
        ),
        page_url="https://example.test/mine",
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心操作区设置图标"
    assert actions[0].expected_next_page_type == "settings"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_flutter_settings_row_adds_structural_settings_candidate() -> None:
    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<flt-semantics aria-label="设置" flt-tappable role="group" '
            'style="position:absolute;width:356.72px;height:59.28px;'
            'transform:matrix(1,0,0,1,16.64,903.12);pointer-events:none">'
            '<flt-semantics-container style="position:absolute;pointer-events:none">'
            '<flt-semantics role="img" style="position:absolute;width:43.68px;'
            "height:43.68px;transform:matrix(1,0,0,1,15.6,7.8);"
            'pointer-events:all"></flt-semantics>'
            '<flt-semantics role="img" style="position:absolute;width:20.8px;'
            "height:20.8px;transform:matrix(1,0,0,1,320.32,19.24);"
            'pointer-events:all"></flt-semantics>'
            "</flt-semantics-container></flt-semantics>"
        ),
        page_url="https://example.test/mine",
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心 Flutter 设置入口"
    assert actions[0].expected_next_page_type == "settings"
    assert "evidence:structure" in actions[0].target_keywords


async def test_profile_visible_go_signin_without_structure_is_not_executable() -> None:
    dom = _artifact("dom-profile-activity", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-profile-activity", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/mine"))
    reader = FakeArtifactReader(
        {
            "dom-profile-activity": (
                b'<html><body><main class="profile-page">'
                b'<div class="box"><div class="btn">'
                b"<div>\xe5\x8e\xbb\xe7\xad\xbe\xe5\x88\xb0</div></div></div>"
                b"</main></body></html>"
            ),
            "shot-profile-activity": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    activity_actions = [
        item
        for item in result["candidate_actions"]
        if item["expected_next_page_type"] == "activity"
    ]
    assert activity_actions == []


def test_profile_svg_signin_definition_does_not_add_activity_candidate() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[
            '<main><svg><symbol id="home-signin">'
            '<g data-name="主页-签到"><text>去签到</text></g>'
            "</symbol></svg></main>"
        ],
    )

    assert actions == []


@pytest.mark.parametrize("page_type", ["activity", "settings", "payment"])
def test_profile_local_navigation_is_removed_outside_profile(page_type: str) -> None:
    actions = page_detection_tools._with_profile_local_navigation_trust_boundary(
        page_type="home",
        candidate_actions=[
            page_detection_tools.CandidateAction(
                action="click",
                target=f"打开{page_type}",
                target_keywords=["profile_local_action", f"page_type:{page_type}"],
                risk_level="low",
                purpose="模型误报的个人中心专属入口",
                expected_next_page_type=page_type,
            )
        ],
    )

    assert actions == []


def test_unstable_profile_cannot_keep_model_forged_activity_candidate() -> None:
    forged = page_detection_tools.CandidateAction(
        action="click",
        target="个人中心签到入口",
        target_keywords=[
            "activity_entry",
            "profile_local_action",
            "page_type:activity",
            "evidence:structure",
        ],
        risk_level="low",
        purpose="模型伪造的签到结构候选",
        expected_next_page_type="activity",
    )

    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="loading",
        candidate_actions=[forged],
        dom_chunks=["<main></main>"],
    )

    assert actions == []


def test_profile_without_settings_structure_drops_model_forged_candidate() -> None:
    forged = page_detection_tools.CandidateAction(
        action="click",
        target="个人中心右上设置图标",
        target_keywords=[
            "settings_entry",
            "profile_local_action",
            "page_type:settings",
            "evidence:structure",
        ],
        risk_level="low",
        purpose="模型伪造的设置结构候选",
        expected_next_page_type="settings",
    )

    actions = page_detection_tools._with_semantic_entry_navigation_candidates(
        page_type="profile",
        page_state="stable",
        candidate_actions=[forged],
        dom_chunks=["<main class='profile-page'></main>"],
        page_url="https://example.test/mine",
    )

    assert actions == []


def test_profile_task_sign_image_adds_structural_activity_candidate() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="minePage"><img class="task_sign" src="data:image/png;base64,AA"></main>'
        ),
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心签到入口"
    assert actions[0].expected_next_page_type == "activity"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_checkin_entry_adds_structural_activity_candidate() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-page-wrap"><div class="checkin-entry">'
            '<img class="checkin-entry-icon"><div class="checkin-entry-text">签到</div>'
            "</div></main>"
        ),
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心签到按钮"
    assert actions[0].expected_next_page_type == "activity"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_sigin_box_image_adds_structural_activity_candidate() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main><div class="img-menus-wrap"><div class="right">'
            '<div class="sigin-box"><img class="img" '
            'src="/static/img/m-signin-bg.42f9.png">'
            '<span class="sigin-num">您已经累积签到0天了</span></div></div></div>'
            '<div class="recommend"><img class="img" '
            'src="/static/img/m-signin-bg-ad.png"></div></main>'
        ),
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心每日签到图片"
    assert actions[0].expected_next_page_type == "activity"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_five_site_signin_image_adds_structural_activity_candidate() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-page"><section class="user-card">'
            '<img class="sign-in-entry" alt="签到" src="/sign-in.png">'
            "</section></main>"
        ),
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心签到图片"
    assert actions[0].expected_next_page_type == "activity"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_site_three_real_signin_image_adds_structural_activity_candidate() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-content"><div class="mine-info">'
            '<div class="sign-in"><img class="d-img" '
            'src="/assets/singi-MikFGLph.png" alt=""></div>'
            '<div class="recommend"><img class="d-img" '
            'src="/assets/singi-ad.png" alt=""></div></div></main>'
        ),
    )

    assert len(actions) == 1
    assert actions[0].target == "个人中心签到图片"
    assert actions[0].expected_next_page_type == "activity"
    assert "evidence:structure" in actions[0].target_keywords


def test_profile_site_three_nested_signin_image_is_not_detected() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=page_detection_tools._sanitize_and_chunk_dom(
            '<main class="mine-content"><div class="sign-in"><div class="nested">'
            '<img class="d-img" src="/assets/singi-MikFGLph.png" alt="">'
            "</div></div></main>"
        ),
    )

    assert actions == []


def test_profile_plain_signin_does_not_add_low_risk_activity_candidate() -> None:
    actions = page_detection_tools._with_profile_activity_entry_navigation_candidate(
        page_type="profile",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=['<main class="profile-page"><button>签到</button></main>'],
    )

    assert actions == []


@pytest.mark.parametrize(
    ("page_url", "dom_payload", "expected_page_type", "expected_dom_hint"),
    [
        (
            "https://35.185.140.139:98/editInfo",
            (
                '<html><body><main class="profile-edit-page">'
                "<h1>个人资料</h1><label>头像</label><label>昵称</label>"
                "</main></body></html>"
            ),
            "settings",
            "semantic-result:settings",
        ),
    ],
)
async def test_recognized_missing_page_type_uses_semantic_result_structure_fallback(
    page_url: str,
    dom_payload: str,
    expected_page_type: str,
    expected_dom_hint: str,
) -> None:
    dom = _artifact("dom-semantic-result", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-semantic-result", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url=page_url))
    reader = FakeArtifactReader(
        {
            "dom-semantic-result": dom_payload.encode(),
            "shot-semantic-result": PNG_BYTES,
        }
    )
    model_output = _recognized_output("home").model_copy(update={"page_type": None})

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(model_output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == expected_page_type
    assert result["model_page_type"] is None
    assert result["confidence_source"] == "deterministic_structure"
    assert result["matched_catalog_signals"]["dom_hints"] == [expected_dom_hint]
    assert "deterministic_page_structure_provider_fallback" in result["risk_flags"]
    assert "deterministic_invalid_page_type_fallback" in result["risk_flags"]


@pytest.mark.parametrize(
    ("page_url", "dom_payload"),
    [
        (
            "https://example.com/ranking?mediaType=video",
            '<html><body><main class="ranking-page">Ranking result</main></body></html>',
        ),
        (
            "https://example.com/home",
            '<html><body><main class="leaderboard-page">Leaderboard</main></body></html>',
        ),
    ],
)
async def test_explicit_ranking_result_signals_do_not_promote_removed_page_type(
    page_url: str,
    dom_payload: str,
) -> None:
    dom = _artifact("dom-ranking-result", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-ranking-result", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url=page_url))
    reader = FakeArtifactReader(
        {
            "dom-ranking-result": dom_payload.encode(),
            "shot-ranking-result": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "home"
    assert "deterministic_semantic_result_ranking_page_type_refinement" not in result["risk_flags"]


@pytest.mark.parametrize(
    ("page_url", "dom_payload"),
    [
        (
            "https://example.com/search?mediaType=1",
            (
                '<html><body><main class="video-list search-page">'
                '<input class="search-input" placeholder="请输入搜索关键词">'
                '<article class="video-card">视频结果</article>'
                "</main></body></html>"
            ),
        ),
        (
            "https://example.com/result?keyword=video",
            (
                '<html><body><main class="video-list search-page">'
                '<h2>热门搜索</h2><button class="search-input" aria-label="find"></button>'
                "</main></body></html>"
            ),
        ),
        (
            "https://example.com/hot",
            (
                '<html><body><main class="video-list hot-page">'
                '<h2>热门</h2><button class="search-input" aria-label="find"></button>'
                "</main></body></html>"
            ),
        ),
    ],
)
async def test_search_and_hot_result_signals_do_not_promote_ranking_page_type(
    page_url: str,
    dom_payload: str,
) -> None:
    dom = _artifact("dom-search-hot-result", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-search-hot-result", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url=page_url))
    reader = FakeArtifactReader(
        {
            "dom-search-hot-result": dom_payload.encode(),
            "shot-search-hot-result": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("video_list")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "video_list"
    assert "deterministic_semantic_result_ranking_page_type_refinement" not in result["risk_flags"]


async def test_search_page_with_active_video_tab_rejects_removed_model_page_type() -> None:
    dom = _artifact("dom-search-video-list", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-search-video-list", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(
        _bundle(dom, screenshot, page_url="https://example.com/search?mediaType=1")
    )
    reader = FakeArtifactReader(
        {
            "dom-search-video-list": (
                '<html><body><main class="video-list search-page">'
                '<input class="search-input" placeholder="请输入搜索关键词">'
                '<article class="video-card">视频结果</article>'
                "</main>"
                '<div role="dialog" aria-modal="true"><h2>系统公告</h2></div>'
                '<nav class="van-tabbar">'
                '<div class="van-tabbar-item">首页</div>'
                '<div class="van-tabbar-item van-tabbar-item--active">视频</div>'
                "</nav></body></html>"
            ).encode(),
            "shot-search-video-list": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("ranking")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "video_list"
    assert "deterministic_invalid_page_type_fallback" in result["risk_flags"]


async def test_plain_home_hot_text_does_not_promote_ranking_page_type() -> None:
    dom = _artifact("dom-home-hot-tab", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-home-hot-tab", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/home"))
    reader = FakeArtifactReader(
        {
            "dom-home-hot-tab": (
                '<html><body><main class="home-page">'
                '<div class="van-tabbar-item">热门</div>'
                "</main></body></html>"
            ).encode(),
            "shot-home-hot-tab": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "home"


async def test_plain_home_search_and_hot_text_does_not_promote_ranking_page_type() -> None:
    dom = _artifact("dom-home-search-hot", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-home-search-hot", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/home"))
    reader = FakeArtifactReader(
        {
            "dom-home-search-hot": (
                '<html><body><main class="home-page">'
                '<div class="heroSearchBox" role="button"></div><div>热门</div>'
                "</main></body></html>"
            ).encode(),
            "shot-home-search-hot": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("home")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "home"


@pytest.mark.parametrize("guide_enforced,complete,expected", [
    (True, True, "profile"), (True, False, "settings"), (False, True, "settings"),
])
async def test_profile_edit_legacy_refinement_does_not_override_current_h5_guide(
    guide_enforced, complete, expected,
) -> None:
    page_detection_tools._PAGE_DETECTION_RESULT_CACHE.clear()
    catalog = load_default_page_type_catalog()
    profile = catalog.require_code("profile")
    refs = recognition_signal_references(profile)
    output = _recognized_output("profile").model_copy(update={
        "confidence": 0.93,
        "page_type_candidates": [page_detection_tools.PageTypeCandidate(
            page_type="settings", confidence=0.22,
        )],
        "matched_catalog_signals": page_detection_tools.MatchedCatalogSignals(
            required_positive_signal_ids=(
                [item["signal_id"] for item in refs["required_positive_signals"]]
                if complete else []
            ),
            excluded_confusable_page_types=list(profile.confusable_with),
        ),
    })
    dom = _artifact("dom-profile-guide", "dom", content_type="text/html; charset=utf-8")
    shot = _artifact("shot-profile-guide", "screenshot", content_type="image/png")
    with bind_agent_run_context(AgentRunContext(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id=f"run-profile-guide-{guide_enforced}-{complete}",
    )):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, shot, page_url="https://fixture.test/editInfo")),
            artifact_reader=FakeArtifactReader({
                "dom-profile-guide": (
                    '<html><body><main class="profile-edit-page"><h1>个人资料</h1>'
                    '<label>头像</label><label>昵称</label><label>签名</label></main></body></html>'
                ).encode(), "shot-profile-guide": PNG_BYTES,
            }),
            chat_client=FakeChatClient(output), skill_text="# page-detection", catalog=catalog,
            model_name="vision-test", recognition_guide_enforced=guide_enforced,
        )
    assert result["page_type"] == expected
    if expected == "profile":
        assert result["confidence"] == 0.93
        assert result["confidence_source"] == "model_self_report"
        assert result["recognition_guide_audit"]["authority_source"] == "model_guide"
    else:
        assert result["confidence"] == 0.22


async def test_profile_edit_keywords_promote_settings_page_type() -> None:
    dom = _artifact("dom-settings-result", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-settings-result", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/profile"))
    reader = FakeArtifactReader(
        {
            "dom-settings-result": (
                '<html><body><main class="profile-edit-page">'
                "<h1>个人资料</h1><label>头像</label><label>昵称</label>"
                "</main></body></html>"
            ).encode(),
            "shot-settings-result": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "settings"
    assert "deterministic_semantic_result_settings_page_type_refinement" in result["risk_flags"]


async def test_ambiguous_profile_edit_result_is_promoted_to_recognized_settings() -> None:
    dom = _artifact("dom-ambiguous-settings", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact(
        "shot-ambiguous-settings",
        "screenshot",
        content_type="image/png",
    )
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/editInfo"))
    reader = FakeArtifactReader(
        {
            "dom-ambiguous-settings": (
                '<html><body><main class="profile-edit-page">'
                "<h1>个人资料</h1><label>头像</label><label>昵称</label>"
                "</main></body></html>"
            ).encode(),
            "shot-ambiguous-settings": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_ambiguous_output()),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["detection_status"] == "recognized"
    assert result["page_type"] == "settings"
    assert result["rule_matching_allowed"] is True
    assert result["confidence_source"] == "deterministic_refinement_cap"
    assert "deterministic_semantic_result_settings_page_type_refinement" in result["risk_flags"]


async def test_profile_avatar_nickname_with_setting_icon_is_not_promoted_to_settings() -> None:
    dom = _artifact("dom-profile-avatar-setting", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-profile-avatar-setting", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/profile"))
    reader = FakeArtifactReader(
        {
            "dom-profile-avatar-setting": (
                '<html><body><main class="profile-page">'
                '<button class="userGear iconSetting" aria-label="account settings"></button>'
                '<section class="user-card"><span>头像</span><span>昵称</span></section>'
                "</main></body></html>"
            ).encode(),
            "shot-profile-avatar-setting": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "profile"


async def test_hidden_profile_edit_template_is_not_promoted_to_settings() -> None:
    dom = _artifact("dom-hidden-profile-edit", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-hidden-profile-edit", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/profile"))
    reader = FakeArtifactReader(
        {
            "dom-hidden-profile-edit": (
                '<html><body><main class="profile-page">我的</main>'
                '<template><section class="profile-edit-page">'
                "<label>头像</label><label>昵称</label><label>个人资料</label>"
                "</section></template></body></html>"
            ).encode(),
            "shot-hidden-profile-edit": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "profile"


async def test_profile_page_with_only_setting_icon_is_not_promoted_to_settings() -> None:
    dom = _artifact("dom-profile-setting-icon", "dom", content_type="text/html; charset=utf-8")
    screenshot = _artifact("shot-profile-setting-icon", "screenshot", content_type="image/png")
    loader = FakeEvidenceLoader(_bundle(dom, screenshot, page_url="https://example.com/profile"))
    reader = FakeArtifactReader(
        {
            "dom-profile-setting-icon": (
                b'<html><body><main class="profile-page">'
                b'<button class="userGear iconSetting" aria-label="account settings"></button>'
                b"</main></body></html>"
            ),
            "shot-profile-setting-icon": PNG_BYTES,
        }
    )

    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=loader,
            artifact_reader=reader,
            chat_client=FakeChatClient(_recognized_output("profile")),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )

    assert result["page_type"] == "profile"


def test_h5_publish_page_tab_is_a_safe_entry_without_authorizing_submit() -> None:
    profile = H5ExplorationProfile(
        page_tab_items=[
            H5TabbarItem(
                label="发布作品",
                selector=".van-tabs:visible .van-tab:nth-of-type(3):visible",
                active=False,
                expected_page_type="post_publish",
            )
        ]
    )

    actions = page_detection_tools._with_h5_profile_page_tab_navigation_candidates(
        page_type="post_list",
        page_state="stable",
        candidate_actions=[],
        profile=profile,
    )

    assert len(actions) == 1
    assert actions[0].action == "click"
    assert actions[0].expected_next_page_type == "post_publish"
    assert actions[0].risk_level == "low"
    assert actions[0].purpose == "探索稳定页面中的页内 Tab 业务分支"

    submit = page_detection_tools._with_deterministic_high_risk_actions(
        [
            page_detection_tools.CandidateAction(
                action="click",
                target="提交发布",
                target_keywords=["submit", "publish"],
                risk_level="low",
                purpose="提交发布作品",
            )
        ]
    )
    assert submit[0].risk_level == "high"


def test_bootstrap_ad_dialog_without_overlay_yields_dismissal() -> None:
    # 实测 91porna 首页：广告弹窗是 Bootstrap 式 .modal.dx-modal.ad-dialog.in，
    # 既无 dialog 标记也无 van-overlay，此前零关闭候选、可见文本塌到 38 字。
    dom = (
        '<html><body><div class="modal dx-modal ad-dialog in">'
        '<div class="modal-dialog"><div class="modal-content">'
        '<div class="modal-header flex items-center">'
        '<span class="close text-subtitle ml-auto">×</span></div>'
        '<div class="modal-body"><div class="ad-title md:text-2xl">推广</div></div>'
        "</div></div></div></body></html>"
    )
    actions = page_detection_tools._with_obstruction_dismissal_candidate(
        page_type="home",
        page_state="popup",
        candidate_actions=[],
        dom_chunks=[dom],
    )

    assert len(actions) == 1
    _assert_semantic_popup_action(actions[0], "overlay_ad_close")


def test_inactive_bootstrap_ad_dialog_is_not_visible_obstruction() -> None:
    # 轮候广告位是同样的 .modal.dx-modal 但缺展开态（in/show），
    # 若不看展开态会把未播出的广告关闭控件误当成当前遮挡而空点。
    dom = (
        '<html><body><div class="modal fade dx-modal ad-dialog">'
        '<div class="modal-content"><span class="close text-subtitle">×</span></div>'
        "</div></body></html>"
    )

    assert page_detection_tools._structural_obstruction_selector(dom) is None


def test_every_signed_popup_capability_is_accepted_by_all_downstream_allowlists() -> None:
    """弹窗关闭能力在检测端签发后要穿过四道白名单才能落成动作参数；
    任一处漏登记都会让候选退化成自由文本、被执行端以未找到语义目标拒绝
    （overlay_ad_close 实测漏了三处，91JAV/91porna 连续 20 次空点）。"""
    from services.api.app.application.h5_exploration_observation import (
        _POPUP_DISMISSAL_CAPABILITIES as OBSERVATION_ALLOWLIST,
    )
    from services.api.app.application.h5_runtime_composition import (
        _POPUP_DISMISSAL_CAPABILITIES as RUNTIME_ALLOWLIST,
    )
    from services.api.app.infrastructure.browser.semantic_target_resolver import (
        _POPUP_DISMISSAL_SELECTORS as RESOLVER_SELECTORS,
    )

    signed = set(page_detection_tools._POPUP_CAPABILITY_BY_SELECTOR.values())
    signed |= set(page_detection_tools._TEXT_POPUP_CAPABILITY_BY_SELECTOR.values())

    assert signed <= OBSERVATION_ALLOWLIST, signed - OBSERVATION_ALLOWLIST
    assert signed <= RUNTIME_ALLOWLIST, signed - RUNTIME_ALLOWLIST
    assert signed <= set(RESOLVER_SELECTORS), signed - set(RESOLVER_SELECTORS)


def test_payment_horizontal_membership_collection_adds_read_only_scroll() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": ".vip-plan-list",
                "visible": True,
                "text": "月卡 季卡 年卡 永久VIP",
                "class_tokens": ["vip-plan-list"],
                "rect": {"x": 16, "y": 240, "width": 358, "height": 220},
                "style": {"overflow_x": "auto"},
                "text_metrics": {
                    "scroll_width": 980,
                    "client_width": 358,
                    "horizontal_overflow": True,
                },
                "hit_test": {"center_hits_target": True},
            }
        ],
    }

    actions = page_detection_tools._with_payment_horizontal_collection_candidate(
        page_type="payment",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=['<section class="vip-plan-list">月卡 季卡 年卡 永久VIP</section>'],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].action == "scroll"
    assert actions[0].target == "浏览全部会员卡"
    assert actions[0].target_keywords == [
        "payment_horizontal_collection",
        "read_only_collection",
        "page_type:payment",
    ]


def test_entitlement_locked_detail_adds_read_only_membership_scroll() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": ".entitlement-modal .vip-plan-list",
                "visible": True,
                "text": "月卡 季卡 年卡 永久VIP",
                "ancestor_class_tokens": ["entitlement-modal", "membership"],
                "rect": {"x": 16, "y": 260, "width": 358, "height": 220},
                "style": {"overflow_x": "auto"},
                "text_metrics": {
                    "scroll_width": 980,
                    "client_width": 358,
                    "horizontal_overflow": True,
                },
                "hit_test": {"center_hits_target": True},
            }
        ],
    }

    actions = page_detection_tools._with_payment_horizontal_collection_candidate(
        page_type="video_detail",
        page_state="entitlement_locked",
        candidate_actions=[],
        dom_chunks=['<div role="dialog">月卡 季卡 年卡 永久VIP</div>'],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].target_keywords == [
        "payment_horizontal_collection",
        "read_only_collection",
        "page_type:video_detail",
    ]
    assert actions[0].purpose == "横向浏览当前详情页权益层全部会员套餐"


def test_entitlement_locked_detail_accepts_trusted_swiper_membership_collection() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": ".limited-time-offer-popup-wrapper .swiper_content",
                "visible": True,
                "text": "新人特惠 最受欢迎 热销 新人特惠",
                "class_tokens": [
                    "swiper",
                    "swiper_content",
                    "swiper-initialized",
                    "swiper-horizontal",
                ],
                "ancestor_class_tokens": [
                    "swiper_box",
                    "popup-scroll",
                    "limitedTimeOfferBox",
                    "van-popup",
                ],
                "rect": {"x": 8, "y": 272, "width": 373, "height": 163},
                "style": {"overflow_x": "visible"},
                "text_metrics": {
                    "scroll_width": 1465,
                    "client_width": 373,
                    "horizontal_overflow": True,
                },
                "hit_test": {"center_hits_target": True, "topmost": True},
            }
        ],
    }

    actions = page_detection_tools._with_payment_horizontal_collection_candidate(
        page_type="video_detail",
        page_state="entitlement_locked",
        candidate_actions=[],
        dom_chunks=[],
        ui_snapshot=snapshot,
    )

    assert len(actions) == 1
    assert actions[0].target == "浏览全部会员卡"


def test_horizontal_swiper_without_membership_semantics_is_not_collection() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": ".banner-swiper",
                "visible": True,
                "text": "热门 推荐 最新",
                "class_tokens": ["swiper", "swiper-initialized", "swiper-horizontal"],
                "rect": {"x": 8, "y": 120, "width": 373, "height": 163},
                "style": {"overflow_x": "hidden"},
                "text_metrics": {
                    "scroll_width": 1465,
                    "client_width": 373,
                    "horizontal_overflow": True,
                },
                "hit_test": {"center_hits_target": True},
            }
        ],
    }

    assert page_detection_tools._with_payment_horizontal_collection_candidate(
        page_type="video_detail",
        page_state="entitlement_locked",
        candidate_actions=[],
        dom_chunks=[],
        ui_snapshot=snapshot,
    ) == []


def test_non_entitlement_detail_does_not_add_membership_scroll() -> None:
    assert page_detection_tools._with_payment_horizontal_collection_candidate(
        page_type="video_detail",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=[],
        ui_snapshot={"viewport": {"width": 390, "height": 844}, "elements": []},
    ) == []


def test_payment_horizontal_navigation_strip_is_not_membership_collection() -> None:
    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": ".top-navigation",
                "visible": True,
                "text": "首页 推荐 国产 动漫 漫画",
                "class_tokens": ["top-navigation"],
                "rect": {"x": 0, "y": 0, "width": 390, "height": 52},
                "style": {"overflow_x": "auto"},
                "text_metrics": {
                    "scroll_width": 760,
                    "client_width": 390,
                    "horizontal_overflow": True,
                },
                "hit_test": {"center_hits_target": True},
            }
        ],
    }

    assert page_detection_tools._with_payment_horizontal_collection_candidate(
        page_type="payment",
        page_state="stable",
        candidate_actions=[],
        dom_chunks=['<nav class="top-navigation">首页 推荐 国产 动漫 漫画</nav>'],
        ui_snapshot=snapshot,
    ) == []
