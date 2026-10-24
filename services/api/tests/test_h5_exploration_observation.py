"""H5 页面判定到通用 Observation/Frontier 的纯适配测试。"""

import json
from datetime import UTC, datetime
from importlib import import_module

import pytest

from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools.page_detection_tools import (
    PageDetectionResult,
    detect_page_for_agent,
)
from services.api.app.domain.exploration.h5_visual_navigation import (
    build_visual_navigation_capability,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.page.catalog import load_default_page_type_catalog
from services.api.tests import test_page_detection_tools as detection_test_support

_VISUAL_TASK_ID = "11111111-1111-1111-1111-111111111111"
_VISUAL_CAPTURE_ID = "22222222-2222-2222-2222-222222222222"


def _detection(*, capture_id: str, evidence_id: str) -> dict[str, object]:
    return {
        "schema_version": "page-detection.v2",
        "page_capture_id": capture_id,
        "page_url": "https://m.example.test/home?campaign=7",
        "detection_status": "recognized",
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.92,
        "confidence_source": "model_self_report",
        "reason": "底部导航与首页推荐区同时存在",
        "matched_catalog_signals": {
            "ocr_keywords": ["首页", "我的"],
            "dom_hints": ["nav", "main"],
            "layout_hints": ["bottom-tabs"],
        },
        "candidate_actions": [
            {
                "action": "click",
                "target": "我的",
                "target_keywords": ["我的", "个人中心"],
                "risk_level": "low",
                "purpose": "补齐个人中心覆盖",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "我的",
                "target_keywords": ["个人中心"],
                "risk_level": "low",
                "purpose": "重复候选不应形成第二条 Frontier",
                "expected_next_page_type": "profile",
                "navigation_depth": 1,
            },
            {
                "action": "click",
                "target": "立即购买",
                "target_keywords": ["购买"],
                "risk_level": "critical",
                "purpose": "高副作用候选只保留阻断审计",
                "expected_next_page_type": "order_confirm",
                "navigation_depth": 1,
            },
            {
                "action": "execute_script",
                "target": "window.open",
                "target_keywords": [],
                "risk_level": "low",
                "purpose": "非法动作不得进入 Frontier",
                "expected_next_page_type": None,
                "navigation_depth": 1,
            },
        ],
        "next_page_hypotheses": [],
        "risk_flags": ["purchase_entry_visible"],
        "missing_sources": [],
        "conflicts": [],
        "rule_matching_allowed": True,
        "evidence_artifact_ids": [evidence_id, "artifact-dom"],
        "catalog_version": "catalog.v1",
        "catalog_sha256": "catalog-sha",
        "model": "detector-test",
        "skill_sha256": "skill-sha",
    }


def _build_frontier(**kwargs):
    module = import_module("services.api.app.application.h5_exploration_observation")
    return module.build_h5_frontier(**kwargs)


def test_list_capture_deduplicates_visits_but_not_different_selected_tabs():
    from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot

    detection = _detection(capture_id="capture-list", evidence_id="artifact-list")
    detection["candidate_actions"] = [{
        "action": "scroll", "target": "采集列表至底部",
        "target_keywords": ["list_scroll_coverage", "bounded_to_bottom"],
        "risk_level": "low", "purpose": "逐屏采集", "navigation_depth": 0,
    }]
    detection["logical_surface"] = {
        "protocol_version": "logical_surface.v1", "shadow": False,
        "render_profile": {"kind": "dom_spa", "status": "confirmed", "confidence": 0.94},
        "logical_transition_proof": "trusted",
        "evidence_quality": "high", "identity_fingerprint": "sha256:" + "1" * 64,
        "state_fingerprint": "sha256:" + "2" * 64,
        "page_type_binding": {
            "identity_fingerprint": "sha256:" + "1" * 64, "page_type": "home",
        },
        "state_signals": {"selected_tab": "甲栏目", "tab_key": "#tabs"},
    }
    def frontier():
        return _build_frontier(
            detection_result=detection, observed_at=datetime(2026, 10, 7, tzinfo=UTC),
            navigation_depth=0, logical_surface_effective=True,
        )
    first = frontier()
    detection["logical_surface"]["state_fingerprint"] = "sha256:" + "3" * 64
    revisit = frontier()
    assert first.candidates[0].candidate_id == revisit.candidates[0].candidate_id
    detection["logical_surface"]["state_signals"]["selected_tab"] = "乙栏目"
    second = frontier()
    assert second.candidates[0].candidate_id != first.candidates[0].candidate_id
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for frame in (first, revisit, second):
        snapshot = snapshot.merge(
            source_capture_id=frame.observation.capture_id,
            source_state_version=frame.observation.state_version,
            source_location=frame.observation.location, navigation_depth=0,
            candidates=frame.candidates,
        )
    assert len(snapshot.entries) == 2


def test_detail_scroll_candidate_projects_bounded_h5_capture_parameters() -> None:
    detection = _detection(capture_id="capture-detail", evidence_id="artifact-detail")
    detection.update(
        {
            "page_url": "https://m.example.test/video/42",
            "page_type": "video_detail",
            "page_state": "stable",
            "candidate_actions": [
                {
                    "action": "scroll",
                    "target": "采集详情页第二屏内容",
                    "target_keywords": [
                        "detail_scroll_coverage",
                        "two_screen_sequence",
                        "page_type:video_detail",
                    ],
                    "risk_level": "low",
                    "purpose": "补充详情页第二屏截图证据",
                    "expected_next_page_type": None,
                    "navigation_depth": 0,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 14, tzinfo=UTC),
        navigation_depth=2,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "h5_detail_scroll_coverage",
        "scroll_max_segments": 2,
        "split_scroll_screenshots": True,
        "source_page_type": "video_detail",
    }


def test_list_scroll_and_video_frames_project_bounded_execution_contracts() -> None:
    detection = _detection(capture_id="capture-list", evidence_id="artifact-list")
    detection.update({
        "page_type": "content_list",
        "candidate_actions": [{
            "action": "scroll", "target": "采集列表至底部",
            "target_keywords": ["list_scroll_coverage", "bounded_to_bottom"],
            "risk_level": "low", "purpose": "逐屏采集", "navigation_depth": 0,
        }],
    })
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 28, tzinfo=UTC),
        navigation_depth=0,
    )
    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "h5_list_scroll_coverage",
        "scroll_max_segments": 8,
        "split_scroll_screenshots": True,
        "source_page_type": "content_list",
    }

    detection["page_type"] = "home"
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 28, tzinfo=UTC),
        navigation_depth=0,
    )
    assert frontier.candidates[0].metadata["action_parameters"]["source_page_type"] == "home"

    detection.update({
        "page_type": "video_detail",
        "candidate_actions": [{
            "action": "timed_capture", "target": "采集视频播放多帧",
            "target_keywords": ["video_timed_capture"],
            "risk_level": "low", "purpose": "连续采集", "navigation_depth": 0,
        }],
    })
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 28, tzinfo=UTC),
        navigation_depth=0,
    )
    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "h5_video_timed_capture",
        "frame_count": 3,
        "interval_ms": 1000,
    }
    first_candidate_id = frontier.candidates[0].candidate_id
    detection["page_capture_id"] = "capture-after-banner-change"
    detection["reason"] = "页面轮播文案已变化"
    refreshed = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 28, tzinfo=UTC),
        navigation_depth=0,
        executed_candidate_ids=(first_candidate_id,),
    )
    assert first_candidate_id not in {
        candidate.candidate_id for candidate in refreshed.candidates
    }


def test_page_tab_selector_from_detector_is_admitted_as_structural_navigation() -> None:
    detection = _detection(capture_id="capture-home", evidence_id="artifact-home")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": ".van-tabs:visible .van-tab:nth-of-type(14):visible",
            "target_keywords": ["page_tab_navigation", "最新上架"],
            "risk_level": "low",
            "purpose": "探索当前页面中的内容分类分支",
            "expected_next_page_type": "home",
            "navigation_depth": 1,
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 1.0,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 23, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].semantic_target == "internal:page_tab:nth-of-type(14)"
    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "page_tab_navigation",
        "navigation_index": 14,
        "navigation_label": "最新上架",
    }


def test_adaptive_page_tab_preserves_pattern_signature_in_execution_capability() -> None:
    detection = _detection(capture_id="capture-home", evidence_id="artifact-home")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "internal:page_tab:nth-of-type(2)",
            "target_keywords": ["page_tab_navigation", "国产", "adaptive_page_tab"],
            "risk_level": "low",
            "purpose": "探索当前页面现场识别出的页内栏目分支",
            "expected_next_page_type": "home",
            "navigation_depth": 1,
            "navigation_knowledge_source": "contextual",
            "navigation_knowledge_confidence": 1.0,
            "interaction_pattern_signature": "a" * 64,
            "interaction_pattern_status": "candidate",
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 24, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "page_tab_navigation",
        "navigation_index": 2,
        "navigation_label": "国产",
        "interaction_pattern_signature": "a" * 64,
    }


def _detection_with_logical_surface(
    *,
    capture_id: str,
    page_type: str,
    identity_fingerprint: str,
    state_fingerprint: str,
) -> dict[str, object]:
    detection = _detection(capture_id=capture_id, evidence_id=f"artifact-{capture_id}")
    detection["page_type"] = page_type
    detection["page_url"] = "https://m.example.test/app"
    detection["logical_surface"] = {
        "protocol_version": "logical_surface.v1",
        "shadow": False,
        "render_profile": {
            "kind": "dom_spa",
            "status": "confirmed",
            "confidence": 0.94,
        },
        "identity_fingerprint": identity_fingerprint,
        "state_fingerprint": state_fingerprint,
        "logical_transition_proof": "trusted",
        "page_type_binding": {
            "page_type": page_type,
            "identity_fingerprint": identity_fingerprint,
            "page_type_confidence": 0.92,
            "coverage_allowed": True,
        },
    }
    return detection


def _visual_navigation_raw_detection(
    *,
    source_task_id: str = "task-visual",
    capture_id: str = "capture-visual",
    state_fingerprint: str = "sha256:" + "3" * 64,
    source_artifact_sha256: str = "sha256:" + "4" * 64,
    local_visual_digest: str = "sha256:" + "5" * 64,
) -> dict[str, object]:
    identity_fingerprint = "sha256:" + "2" * 64
    detection = _detection_with_logical_surface(
        capture_id=capture_id,
        page_type="home",
        identity_fingerprint=identity_fingerprint,
        state_fingerprint=state_fingerprint,
    )
    logical_surface = detection["logical_surface"]
    assert isinstance(logical_surface, dict)
    logical_surface["render_profile"] = {
        "kind": "flutter_web",
        "status": "confirmed",
        "confidence": 0.96,
    }
    logical_surface["semantics_coverage"] = {"level": "semantics_sparse"}
    capability = build_visual_navigation_capability(
        source_task_id=source_task_id,
        source_capture_id=capture_id,
        source_artifact_id="artifact-visual",
        source_artifact_sha256=source_artifact_sha256,
        source_identity_fingerprint=identity_fingerprint,
        source_state_fingerprint=state_fingerprint,
        source_page_type="home",
        source_page_type_confidence=0.92,
        source_identity_bound=True,
        render_profile=logical_surface["render_profile"],
        semantics_coverage_level="semantics_sparse",
        target_kind="content_card",
        label="内容卡片",
        expected_page_type="content_detail",
        visual_target_confidence=0.93,
        selected=False,
        unique=True,
        risk_flags=(),
        normalized_rect={"x": 0.1, "y": 0.2, "width": 0.6, "height": 0.3},
        source_image_size={"width": 390, "height": 844},
        viewport_metrics={
            "width": 390,
            "height": 844,
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
        local_visual_digest=local_visual_digest,
        surrounding_anchor_digest="sha256:" + "6" * 64,
    )
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "进入 content_detail 页面",
            "target_keywords": ["visual_navigation", "content_card", "content_detail"],
            "risk_level": "low",
            "purpose": "受限视觉导航",
            "expected_next_page_type": "content_detail",
            "visual_navigation_capability": capability,
        }
    ]
    return detection


def _page_detection_with_manual_visual_private_attr() -> PageDetectionResult:
    """仅为失败关闭用例构造没有 ownership context 的手工 PrivateAttr。"""
    detection = _visual_navigation_raw_detection()
    raw_candidate = dict(detection["candidate_actions"][0])
    trusted_capability = raw_candidate.pop("visual_navigation_capability")
    detection["candidate_actions"] = [raw_candidate]
    result = PageDetectionResult.model_validate(detection)
    result.candidate_actions[0]._trusted_visual_navigation_capability = dict(
        trusted_capability
    )
    return result


async def _owned_visual_navigation_detection(
    *,
    dedicated_png: bytes | None = None,
    state_fingerprint: str = "sha256:" + "3" * 64,
) -> PageDetectionResult:
    """通过真实 ownership 校验与可信 finalizer 生成视觉检测结果。"""
    bundle, reader, ui_snapshot = detection_test_support._visual_detection_bundle(
        render_kind="flutter_web",
        coverage="semantics_sparse",
        dedicated_png=dedicated_png,
    )
    logical_surface = ui_snapshot["logical_surface"]
    assert isinstance(logical_surface, dict)
    logical_surface["state_fingerprint"] = state_fingerprint
    reader.payloads["visual-snapshot"] = json.dumps(
        ui_snapshot,
        ensure_ascii=False,
    ).encode()
    with bind_agent_run_context(
        AgentRunContext(
            task_id=_VISUAL_TASK_ID,
            agent_run_id="run-observation-owned-visual",
        )
    ):
        result = await detect_page_for_agent(
            page_capture_id=_VISUAL_CAPTURE_ID,
            evidence_loader=detection_test_support.FakeEvidenceLoader(bundle),
            artifact_reader=reader,
            chat_client=detection_test_support.FakeChatClient(
                detection_test_support._recognized_output("home")
            ),
            visual_navigation_chat_client=detection_test_support.FakeChatClient(
                detection_test_support._visual_navigation_output()
            ),
            visual_coordinate_mode="enforced",
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
            nav_alias_resolver=detection_test_support.FakeNavAliasResolver({}),
        )
    return PageDetectionResult.model_validate(result)


def test_multipage_frontier_is_byte_compatible_when_feature_disabled() -> None:
    observed_at = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    baseline_detection = _detection(
        capture_id="capture-compatible",
        evidence_id="artifact-compatible",
    )
    detection_with_surface = dict(baseline_detection)
    detection_with_surface["logical_surface"] = {
        "protocol_version": "logical_surface.v1",
        "identity_fingerprint": "identity-ignored",
    }

    baseline = _build_frontier(
        detection_result=baseline_detection,
        observed_at=observed_at,
        navigation_depth=0,
    )
    disabled = _build_frontier(
        detection_result=detection_with_surface,
        observed_at=observed_at,
        navigation_depth=0,
        logical_surface_effective=False,
    )

    assert disabled.to_dict() == baseline.to_dict()


def test_shadow_visual_audit_does_not_change_frontier() -> None:
    """Shadow 仅增加聚合审计时，候选、顺序、状态和快照必须与 off 相同。"""
    observed_at = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    off_detection = _detection(
        capture_id="capture-shadow-compatible",
        evidence_id="artifact-shadow-compatible",
    )
    shadow_detection = dict(off_detection)
    shadow_detection["visual_navigation_audit"] = {
        "protocol_version": "visual_navigation_audit.v1",
        "detector_status": "recognized",
        "prediction_count": 1,
        "signed_capability_count": 1,
    }

    off = _build_frontier(
        detection_result=off_detection,
        observed_at=observed_at,
        navigation_depth=0,
    )
    shadow = _build_frontier(
        detection_result=shadow_detection,
        observed_at=observed_at,
        navigation_depth=0,
    )

    assert shadow.to_dict() == off.to_dict()


async def test_signed_visual_navigation_projects_only_nested_capability_parameters() -> None:
    """Frontier 只投影新 target_mode 与完整嵌套能力，不复用旧视觉字段。"""
    detection = await _owned_visual_navigation_detection()
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 1, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.semantic_target == "进入 content_detail 页面"
    serialized_parameters = candidate.to_dict()["metadata"]["action_parameters"]
    assert serialized_parameters["target_mode"] == "visual_navigation_coordinate"
    assert serialized_parameters["visual_navigation_capability"][
        "source_task_id"
    ] == _VISUAL_TASK_ID


@pytest.mark.asyncio
async def test_visual_frontier_modes_preserve_off_shadow_and_gate_enforced() -> None:
    """若 Frontier 忽略有效视觉模式，Shadow 会错误获得可执行坐标候选。"""
    detection = await _owned_visual_navigation_detection()
    observed_at = datetime(2026, 8, 28, 9, 1, tzinfo=UTC)

    frontiers = {
        mode: _build_frontier(
            detection_result=detection,
            observed_at=observed_at,
            navigation_depth=0,
            logical_surface_effective=True,
            logical_targets_effective=True,
            h5_visual_coordinate_mode=mode,
            source_task_id=_VISUAL_TASK_ID,
        )
        for mode in ("off", "shadow", "enforced")
    }

    assert frontiers["off"].candidates == ()
    assert frontiers["shadow"].candidates == ()
    assert frontiers["shadow"].observation.state_version == (
        frontiers["off"].observation.state_version
    )
    assert frontiers["shadow"].observation.metadata.get("coverage") == (
        frontiers["off"].observation.metadata.get("coverage")
    )
    assert len(frontiers["enforced"].candidates) == 1
    assert frontiers["enforced"].candidates[0].metadata["action_parameters"][
        "target_mode"
    ] == "visual_navigation_coordinate"


async def test_tampered_visual_navigation_capability_has_no_text_click_fallback() -> None:
    """能力篡改后候选必须消失，不能退化成同名文本点击。"""
    detection = await _owned_visual_navigation_detection()
    trusted_action = detection.candidate_actions[0]
    capability = trusted_action._trusted_visual_navigation_capability
    assert isinstance(capability, dict)
    trusted_action._trusted_visual_navigation_capability = {
        **capability,
        "expected_page_type": "video_detail",
    }

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 2, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )

    assert frontier.candidates == ()
    serialized_frontier = _build_frontier(
        detection_result=detection.model_dump(mode="json"),
        observed_at=datetime(2026, 8, 28, 9, 2, 1, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )
    assert serialized_frontier.candidates == ()


def test_rehashed_raw_visual_capability_cannot_create_frontier_trust() -> None:
    """任意外部 mapping 即使含可重算签名，也不能自行获得可信附着。"""
    raw_detection = _visual_navigation_raw_detection()

    frontier = _build_frontier(
        detection_result=raw_detection,
        observed_at=datetime(2026, 8, 28, 9, 2, 1, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
    )

    assert frontier.candidates == ()


def test_manual_visual_private_attr_without_sealed_source_context_is_untrusted() -> None:
    """普通 PageDetectionResult 手写合法 PrivateAttr 也不能绕过 ownership finalizer。"""
    detection = _page_detection_with_manual_visual_private_attr()

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 2, 2, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id="task-visual",
    )

    assert frontier.candidates == ()


async def test_visual_capability_requires_logical_targets_switch() -> None:
    """逻辑目标开关关闭时，受信视觉能力也不能进入 Frontier。"""
    detection = await _owned_visual_navigation_detection()
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 2, 2, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=False,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )

    assert frontier.candidates == ()


async def test_trusted_visual_payload_without_current_task_context_fails_closed() -> None:
    """可信载荷不能用自身 task 字段替代独立当前调用上下文。"""
    detection = await _owned_visual_navigation_detection()
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 2, 3, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
    )

    assert frontier.candidates == ()


async def test_visual_capability_projects_with_matching_current_task_context() -> None:
    """独立当前 task 与能力 task 一致时才允许投影视觉候选。"""
    detection = await _owned_visual_navigation_detection()
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 2, 4, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )

    assert len(frontier.candidates) == 1


async def test_visual_capability_rejects_mismatched_current_task_context() -> None:
    """可信附件绑定的任务与当前 Frontier 上下文不一致时必须失败关闭。"""
    detection = await _owned_visual_navigation_detection()
    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 2, 3, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id="another-task",
    )

    assert frontier.candidates == ()


async def test_visual_candidate_identity_ignores_dynamic_image_seals() -> None:
    """同一逻辑状态下像素摘要和签名变化不能制造新的业务候选身份。"""
    first_detection = await _owned_visual_navigation_detection()
    second_detection = await _owned_visual_navigation_detection(
        dedicated_png=detection_test_support._test_png(
            local_color=(200, 10, 20),
        )
    )
    first = _build_frontier(
        detection_result=first_detection,
        observed_at=datetime(2026, 8, 28, 9, 3, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )
    second = _build_frontier(
        detection_result=second_detection,
        observed_at=datetime(2026, 8, 28, 9, 4, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )

    assert first.candidates[0].candidate_id == second.candidates[0].candidate_id


async def test_visual_candidate_identity_still_binds_source_state_version() -> None:
    """同一语义目标来自不同逻辑状态时仍必须形成不同 occurrence。"""
    first_detection = await _owned_visual_navigation_detection()
    second_detection = await _owned_visual_navigation_detection(
        state_fingerprint="sha256:" + "9" * 64,
    )
    first = _build_frontier(
        detection_result=first_detection,
        observed_at=datetime(2026, 8, 28, 9, 5, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )
    second = _build_frontier(
        detection_result=second_detection,
        observed_at=datetime(2026, 8, 28, 9, 6, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
        h5_visual_coordinate_mode="enforced",
        source_task_id=_VISUAL_TASK_ID,
    )

    assert first.observation.state_version != second.observation.state_version
    assert first.candidates[0].candidate_id != second.candidates[0].candidate_id


def test_confirmed_spa_same_url_uses_identity_fingerprint_in_state_version() -> None:
    observed_at = datetime(2026, 8, 28, 9, 0, tzinfo=UTC)
    home_identity = "sha256:" + "1" * 64
    home_state = "sha256:" + "2" * 64
    profile_identity = "sha256:" + "3" * 64
    profile_state = "sha256:" + "4" * 64
    home = _build_frontier(
        detection_result=_detection_with_logical_surface(
            capture_id="capture-spa-home",
            page_type="home",
            identity_fingerprint=home_identity,
            state_fingerprint=home_state,
        ),
        observed_at=observed_at,
        navigation_depth=0,
        logical_surface_effective=True,
    )
    profile = _build_frontier(
        detection_result=_detection_with_logical_surface(
            capture_id="capture-spa-profile",
            page_type="profile",
            identity_fingerprint=profile_identity,
            state_fingerprint=profile_state,
        ),
        observed_at=observed_at,
        navigation_depth=1,
        logical_surface_effective=True,
    )

    assert home.observation.location == profile.observation.location
    assert home.observation.state_version != profile.observation.state_version
    assert profile.observation.metadata["logical_surface_identity_fingerprint"] == (
        profile_identity
    )
    assert profile.observation.metadata["logical_surface_state_fingerprint"] == (
        profile_state
    )
    assert profile.candidates[0].metadata["source_identity_fingerprint"] == (
        profile_identity
    )
    assert profile.candidates[0].metadata["source_state_fingerprint"] == profile_state


def test_logical_target_flag_is_neutral_until_third_switch_is_enabled() -> None:
    detection = _detection_with_logical_surface(
        capture_id="capture-logical-target-compatible",
        page_type="home",
        identity_fingerprint="sha256:" + "c" * 64,
        state_fingerprint="sha256:" + "d" * 64,
    )
    arguments = {
        "detection_result": detection,
        "observed_at": datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        "navigation_depth": 0,
        "logical_surface_effective": True,
    }

    baseline = _build_frontier(**arguments)
    disabled = _build_frontier(**arguments, logical_targets_effective=False)

    assert disabled.to_dict() == baseline.to_dict()


@pytest.mark.parametrize("expected_page_type", ["activity", "settings", "payment"])
def test_profile_local_logical_targets_bind_actual_profile_source(
    expected_page_type: str,
) -> None:
    identity = "sha256:" + "e" * 64
    state = "sha256:" + "f" * 64
    detection = _detection_with_logical_surface(
        capture_id=f"capture-profile-{expected_page_type}",
        page_type="profile",
        identity_fingerprint=identity,
        state_fingerprint=state,
    )
    candidate_by_type = {
        "activity": {
            "target": "个人中心每日签到图片",
            "target_keywords": [
                "activity_entry",
                "profile_local_action",
                "page_type:activity",
                "evidence:structure",
            ],
            "purpose": "进入每日签到活动入口",
        },
        "settings": {
            "target": "个人中心右上设置图标",
            "target_keywords": [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:structure",
            ],
            "purpose": "探索我的页头部设置入口",
        },
        "payment": {
            "target": "个人中心会员支付入口",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:structure",
                "capability:profile_vip_center_image",
            ],
            "purpose": "探索我的页会员支付入口：立即开通",
        },
    }
    detection["candidate_actions"] = [
        {
            "action": "click",
            **candidate_by_type[expected_page_type],
            "risk_level": "low",
            "expected_next_page_type": expected_page_type,
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        logical_targets_effective=True,
    )

    candidate = frontier.candidates[0]
    assert frontier.observation.metadata["logical_targets_effective"] is True
    assert candidate.metadata["logical_target_required"] is True
    assert candidate.metadata["source_page_type"] == "profile"
    assert candidate.metadata["source_identity_fingerprint"] == identity
    assert candidate.metadata["source_state_fingerprint"] == state


def test_uncertain_logical_transition_uses_bounded_same_session_reobserve() -> None:
    detection = _detection_with_logical_surface(
        capture_id="capture-spa-uncertain",
        page_type="home",
        identity_fingerprint="sha256:" + "5" * 64,
        state_fingerprint="sha256:" + "6" * 64,
    )
    detection["logical_surface"]["logical_transition_proof"] = "uncertain"

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        lineage_metadata={"logical_reobserve_count": 1},
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.OBSERVE
    assert candidate.metadata["purpose"] == "复验 SPA/Flutter 逻辑页面转换"
    assert candidate.metadata["logical_reobserve_attempt"] == 2
    assert frontier.observation.metadata["logical_reobserve_count"] == 1
    assert frontier.observation.metadata["logical_reobserve_exhausted"] is False


def test_trusted_restoration_still_reobserves_while_logical_surface_is_loading() -> None:
    """恢复到正确 URL 后仍有加载骨架时，不得立即签发旧结构上的业务点击。"""
    detection = _detection_with_logical_surface(
        capture_id="capture-restored-loading",
        page_type="home",
        identity_fingerprint="sha256:" + "5" * 64,
        state_fingerprint="sha256:" + "6" * 64,
    )
    detection["logical_surface"]["logical_transition_proof"] = "uncertain"
    detection["logical_surface"]["state_signals"] = {"loading": True}

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 25, 1, 0, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        trusted_source_restoration=True,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.OBSERVE
    assert candidate.metadata["logical_reobserve_attempt"] == 1


def test_trusted_logical_surface_does_not_publish_navigation_while_loading() -> None:
    detection = _detection_with_logical_surface(
        capture_id="capture-trusted-loading",
        page_type="home",
        identity_fingerprint="sha256:" + "5" * 64,
        state_fingerprint="sha256:" + "6" * 64,
    )
    detection["logical_surface"]["state_signals"] = {"loading": True}

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 28, 1, 0, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        trusted_source_restoration=True,
    )

    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
    assert frontier.candidates[0].metadata["logical_reobserve_attempt"] == 1


def test_trusted_loading_reobserve_budget_is_bounded_despite_identity_change() -> None:
    detection = _detection_with_logical_surface(
        capture_id="capture-trusted-loading-exhausted",
        page_type="home",
        identity_fingerprint="sha256:" + "7" * 64,
        state_fingerprint="sha256:" + "8" * 64,
    )
    detection["logical_surface"]["state_signals"] = {"loading": True}

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 28, 1, 1, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        trusted_source_restoration=True,
        lineage_metadata={
            "logical_reobserve_count": 2,
            "logical_reobserve_identity_fingerprint": "sha256:" + "9" * 64,
        },
    )

    assert frontier.candidates == ()
    assert frontier.observation.metadata["logical_reobserve_exhausted"] is True


def test_trusted_restoration_can_resume_when_logical_surface_is_not_loading() -> None:
    """稳定的可信恢复保持原合同，避免给所有 sibling 无条件增加复验。"""
    detection = _detection_with_logical_surface(
        capture_id="capture-restored-stable",
        page_type="home",
        identity_fingerprint="sha256:" + "7" * 64,
        state_fingerprint="sha256:" + "8" * 64,
    )
    detection["logical_surface"]["logical_transition_proof"] = "uncertain"
    detection["logical_surface"]["state_signals"] = {"loading": False}

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 25, 1, 1, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        trusted_source_restoration=True,
    )

    assert all(
        candidate.action_type is not InteractionOperation.OBSERVE
        for candidate in frontier.candidates
    )


def test_uncertain_logical_transition_stops_after_two_reobservations() -> None:
    detection = _detection_with_logical_surface(
        capture_id="capture-spa-exhausted",
        page_type="home",
        identity_fingerprint="sha256:" + "7" * 64,
        state_fingerprint="sha256:" + "8" * 64,
    )
    detection["logical_surface"]["logical_transition_proof"] = "insufficient"

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        lineage_metadata={"logical_reobserve_count": 2},
    )

    assert frontier.candidates == ()
    assert frontier.observation.metadata["logical_reobserve_exhausted"] is True
    assert frontier.observation.metadata["objective_satisfied"] is False


def test_logical_reobserve_budget_resets_for_a_different_identity() -> None:
    current_identity = "sha256:" + "9" * 64
    detection = _detection_with_logical_surface(
        capture_id="capture-spa-new-identity",
        page_type="profile",
        identity_fingerprint=current_identity,
        state_fingerprint="sha256:" + "a" * 64,
    )
    detection["logical_surface"]["logical_transition_proof"] = "uncertain"

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        navigation_depth=1,
        logical_surface_effective=True,
        lineage_metadata={
            "logical_reobserve_count": 2,
            "logical_reobserve_identity_fingerprint": "sha256:" + "b" * 64,
        },
    )

    assert frontier.observation.metadata["logical_reobserve_count"] == 0
    assert frontier.candidates[0].metadata["logical_reobserve_attempt"] == 1


def test_h5_frontier_preserves_navigation_destination() -> None:
    detection = _detection(capture_id="capture-navigation", evidence_id="artifact-navigation")
    detection["candidate_actions"] = [
        {
            "action": "navigate",
            "target": "商品页",
            "destination_url": "https://example.test/products",
            "target_keywords": [],
            "risk_level": "low",
            "purpose": "探索商品页",
            "expected_next_page_type": "content_list",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["destination_url"] == "https://example.test/products"


def test_video_list_viewport_scroll_projects_as_bounded_frontier_action() -> None:
    detection = _detection(capture_id="capture-video-list", evidence_id="artifact-ui")
    detection["page_type"] = "video_list"
    detection["page_url"] = "https://m.example.test/video"
    detection["candidate_actions"] = [
        {
            "action": "scroll",
            "target": "向下滚动一屏查找视频标题",
            "target_keywords": [
                "list_viewport_scroll",
                "once_from_top",
                "page_type:video_list",
            ],
            "risk_level": "low",
            "purpose": "滚动一屏后重新采集证据，再点击当前可见视频标题",
            "expected_next_page_type": None,
            "navigation_depth": 0,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.SCROLL
    assert candidate.semantic_target == "向下滚动一屏查找视频标题"
    assert candidate.navigation_depth == 0
    assert "list_viewport_scroll" in candidate.metadata["target_keywords"]


def test_video_list_scroll_binds_effective_logical_source_state() -> None:
    identity = "sha256:" + "1" * 64
    state = "sha256:" + "2" * 64
    detection = _detection_with_logical_surface(
        capture_id="capture-video-list-logical",
        page_type="video_list",
        identity_fingerprint=identity,
        state_fingerprint=state,
    )
    detection["candidate_actions"] = [
        {
            "action": "scroll",
            "target": "向下滚动一屏查找视频标题",
            "target_keywords": ["list_viewport_scroll", "once_from_top"],
            "risk_level": "low",
            "purpose": "滚动后重新采集当前列表",
            "expected_next_page_type": None,
            "navigation_depth": 0,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 7, 9, 0, tzinfo=UTC),
        navigation_depth=0,
        logical_surface_effective=True,
        logical_targets_effective=True,
    )

    candidate = frontier.candidates[0]
    assert candidate.metadata["logical_target_required"] is True
    assert candidate.metadata["source_identity_fingerprint"] == identity
    assert candidate.metadata["source_state_fingerprint"] == state


def test_entitlement_locked_reobservation_skips_popup_absence_observe() -> None:
    """动作前已确认不可关闭权益层时直接收口当前分支，不再多做 Observe。"""
    detection = _detection(
        capture_id="capture-entitlement-locked",
        evidence_id="artifact-entitlement-locked",
    )
    detection.update(
        {
            "page_type": "video_list",
            "page_state": "entitlement_locked",
            "candidate_actions": [],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        navigation_depth=1,
        lineage_metadata={
            "action_reobserve_error_code": "POPUP_DISMISSAL_NOT_FOUND",
            "action_reobservation_retryable": True,
            "h5_action_reobservation": {
                "schema_version": "h5-action-reobservation.v1",
                "before_capture_id": "capture-entitlement-locked",
            },
            "action_reobservation_candidate_identity": {
                "action_type": "click",
                "semantic_target": "关闭当前弹窗",
                "expected_next_page_type": None,
            },
        },
    )

    assert frontier.observation.page_state == "entitlement_locked"
    assert frontier.candidates == ()


def test_popup_state_reobserve_does_not_remove_a_still_observed_layer() -> None:
    """观察结束不等于退场，同签名和新层均须由当前事实继续验签。"""
    detection = _detection(
        capture_id="capture-popup-absence",
        evidence_id="artifact-popup-absence",
    )
    signature = "h5-obstruction:" + "a" * 64
    detection.update(
        {
            "page_type": "video_list",
            "page_state": "popup",
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "关闭当前弹窗",
                    "target_keywords": [
                        "popup_dismissal",
                        "close_control",
                        "dialog_overlay",
                    ],
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                    "expected_next_page_type": None,
                    "navigation_depth": 0,
                    "obstruction_capability": "dialog_overlay",
                    "obstruction_signature": signature,
                }
            ],
        }
    )
    failure_metadata = {
        "action_reobserve_error_code": "POPUP_DISMISSAL_NOT_FOUND",
        "action_reobservation_retryable": True,
        "popup_dismissal_reobserve_state": {
            "signature": signature,
            "count": 1,
            "streak": 1,
        },
        "h5_action_reobservation": {
            "schema_version": "h5-action-reobservation.v1",
            "before_capture_id": "capture-popup-absence",
        },
        "action_reobservation_candidate_identity": {
            "action_type": "click",
            "semantic_target": "关闭当前弹窗",
            "expected_next_page_type": None,
        },
    }

    absence = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata=failure_metadata,
    )

    assert absence.candidates[0].semantic_target == "重新核对弹窗状态"
    assert absence.candidates[0].metadata["popup_absence_obstruction_signature"] == signature

    def popup_action(popup_signature: str) -> dict[str, object]:
        return {
            "action": "click",
            "target": "关闭当前弹窗",
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                "dialog_overlay",
            ],
            "risk_level": "low",
            "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
            "expected_next_page_type": None,
            "navigation_depth": 0,
            "obstruction_capability": "dialog_overlay",
            "obstruction_signature": popup_signature,
        }

    other_signature = "h5-obstruction:" + "b" * 64
    completed_detection = dict(detection)
    completed_detection["page_capture_id"] = "capture-popup-absence-after"
    completed_detection["candidate_actions"] = [
        popup_action(signature),
        popup_action(other_signature),
    ]
    completed = _build_frontier(
        detection_result=completed_detection,
        observed_at=datetime(2026, 8, 27, 9, 1, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={
            "popup_absence_reobserve_completed": True,
            "popup_absence_obstruction_signature": signature,
        },
    )

    assert [
        candidate.metadata["obstruction_signature"]
        for candidate in completed.candidates
        if candidate.semantic_target == "关闭当前弹窗"
    ] == [signature, other_signature]


def test_action_reobservation_occurrence_only_marks_failed_candidate() -> None:
    """动作重观察只应重新放行失败候选，不得复活同页已完成的兄弟入口。"""
    detection = _detection(
        capture_id="capture-profile-reobserve",
        evidence_id="artifact-profile-reobserve",
    )
    detection["page_type"] = "profile"
    detection["candidate_actions"] = [
            {
                "action": "click",
                "target": "个人中心签到入口",
                "target_keywords": [
                    "activity_entry",
                    "profile_local_action",
                    "page_type:activity",
                    "evidence:structure",
                ],
                "risk_level": "low",
                "purpose": "进入活动页",
                "expected_next_page_type": "activity",
            "navigation_depth": 1,
        },
            {
                "action": "click",
                "target": "个人中心会员支付入口",
                "target_keywords": [
                    "profile_vip_payment_entry",
                    "profile_local_action",
                    "page_type:payment",
                    "entry:立即开通",
                    "evidence:structure",
                    "capability:profile_vip_center_image",
                ],
                "risk_level": "low",
                "purpose": "探索我的页会员支付入口：立即开通",
                "expected_next_page_type": "payment",
            "navigation_depth": 1,
        },
            {
                "action": "click",
                "target": "个人中心右上设置图标",
                "target_keywords": [
                    "settings_entry",
                    "profile_local_action",
                    "page_type:settings",
                    "evidence:structure",
                ],
            "risk_level": "low",
            "purpose": "进入设置页",
            "expected_next_page_type": "settings",
            "navigation_depth": 1,
        },
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={
            "h5_action_reobservation": {
                "schema_version": "h5-action-reobservation.v1",
                "before_capture_id": "capture-profile-reobserve",
            },
            "action_reobservation_candidate_identity": {
                    "action_type": "click",
                    "semantic_target": "个人中心会员支付入口",
                "expected_next_page_type": "payment",
            },
        },
    )

    candidates = {candidate.semantic_target: candidate for candidate in frontier.candidates}
    assert candidates["个人中心会员支付入口"].metadata[
        "action_reobservation_capture_id"
    ] == (
        "capture-profile-reobserve"
    )
    assert "action_reobservation_capture_id" not in candidates["个人中心签到入口"].metadata
    assert "action_reobservation_capture_id" not in candidates["个人中心右上设置图标"].metadata


def test_action_reobservation_exhaustion_filters_only_repeated_failed_candidate() -> None:
    """同错误的第 3 次失败应永久关闭该候选，但保留同页兄弟入口。"""
    detection = _detection(
        capture_id="capture-profile-reobserve-exhausted",
        evidence_id="artifact-profile-reobserve-exhausted",
    )
    detection["page_type"] = "profile"
    detection["candidate_actions"] = [
            {
                "action": "click",
                "target": "个人中心会员支付入口",
                "target_keywords": [
                    "profile_vip_payment_entry",
                    "profile_local_action",
                    "page_type:payment",
                    "entry:立即开通",
                    "evidence:structure",
                    "capability:profile_vip_center_image",
                ],
                "risk_level": "low",
                "purpose": "探索我的页会员支付入口：立即开通",
            "expected_next_page_type": "payment",
            "navigation_depth": 1,
        },
            {
                "action": "click",
                "target": "个人中心右上设置图标",
                "target_keywords": [
                    "settings_entry",
                    "profile_local_action",
                    "page_type:settings",
                    "evidence:structure",
                ],
            "risk_level": "low",
            "purpose": "进入设置页",
            "expected_next_page_type": "settings",
            "navigation_depth": 1,
        },
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
        navigation_depth=1,
        lineage_metadata={
            "action_reobserve_error_code": "VISUAL_TARGET_NOT_AT_COORDINATE",
            "action_reobservation_retryable": True,
            "action_reobservation_failure_count": 3,
            "h5_action_reobservation": {
                "schema_version": "h5-action-reobservation.v1",
                "before_capture_id": "capture-profile-reobserve-exhausted",
            },
            "action_reobservation_candidate_identity": {
                    "action_type": "click",
                    "semantic_target": "个人中心会员支付入口",
                "expected_next_page_type": "payment",
            },
        },
    )

    assert [candidate.semantic_target for candidate in frontier.candidates] == [
        "个人中心右上设置图标"
    ]
    assert frontier.observation.metadata["action_reobservation_exhausted"] is True


def test_h5_frontier_projects_catalog_skip_metadata() -> None:
    detection = _detection(capture_id="capture-unknown", evidence_id="artifact-unknown")
    detection.update(
        {
            "detection_status": "unrecognized",
            "page_type": None,
            "candidate_actions": [],
            "catalog_disposition": "skipped",
            "catalog_skip_reason": "uncatalogued_page_type",
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.observation.metadata["catalog_disposition"] == "skipped"
    assert frontier.observation.metadata["catalog_skip_reason"] == (
        "uncatalogued_page_type"
    )
    assert frontier.candidates == ()


def test_h5_frontier_never_explores_guide_page_download_actions() -> None:
    detection = _detection(capture_id="capture-guide", evidence_id="artifact-guide")
    detection.update(
        {
            "page_type": "guide_page",
            "page_state": "stable",
            # 终止整条分支只认检测器由完整证据签发的引导页，所以这里带确定性标记。
            "confidence_source": "deterministic_structure",
            "risk_flags": ["deterministic_guide_page", "guide_page_terminal"],
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "安卓下载",
                    "target_keywords": ["guide_page", "download"],
                    "risk_level": "low",
                    "purpose": "下载 App",
                    "expected_next_page_type": None,
                    "navigation_depth": 1,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates == ()
    assert frontier.observation.metadata["guide_page_terminal"] is True


def test_h5_frontier_keeps_exploring_model_reported_guide_page() -> None:
    """撸啊撸实测回归：模型自报引导页不足以掐断整条分支。

    内容站页脚普遍挂着下载入口，模型会因此给出 guide_page；没有确定性证据就
    清空候选，等于整个产品覆盖度归零。验证码与年龄门禁两段同样要求确定性标记。
    """
    detection = _detection(capture_id="capture-guide-model", evidence_id="artifact-guide-model")
    detection.update(
        {
            "page_type": "guide_page",
            "page_state": "stable",
            "confidence_source": "model_self_report",
            "risk_flags": [],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 25, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates != ()
    assert frontier.observation.metadata["guide_page_terminal"] is False


def test_h5_frontier_preserves_navigation_knowledge_candidate_audit() -> None:
    detection = _detection(capture_id="capture-knowledge", evidence_id="artifact-knowledge")
    raw_actions = detection["candidate_actions"]
    assert isinstance(raw_actions, list)
    raw_action = raw_actions[0]
    assert isinstance(raw_action, dict)
    raw_action["navigation_knowledge_source"] = "vector"
    raw_action["navigation_knowledge_confidence"] = 0.88

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    candidate = frontier.candidates[0]
    assert candidate.metadata["navigation_knowledge_source"] == "vector"
    assert candidate.metadata["navigation_knowledge_confidence"] == 0.88


def test_h5_frontier_platform_signs_source_page_type_for_every_candidate() -> None:
    """若信任模型自报来源类型，动态父页恢复可被伪造候选错误放行。"""
    detection = _detection(capture_id="capture-source-type", evidence_id="artifact-source-type")
    raw_actions = detection["candidate_actions"]
    assert isinstance(raw_actions, list)
    raw_action = raw_actions[0]
    assert isinstance(raw_action, dict)
    raw_action["source_page_type"] = "profile"

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["source_page_type"] == "home"


def test_h5_frontier_signs_deterministic_bottom_navigation_capability() -> None:
    detection = _detection(capture_id="capture-bottom-nav", evidence_id="artifact-bottom-nav")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": (
                "div.van-tabbar:visible > "
                "div.van-tabbar-item:nth-of-type(2):visible"
            ),
            "target_keywords": ["bottom_navigation", "视频", "video_list"],
            "risk_level": "low",
            "purpose": "探索稳定页面中可见的底部导航业务分支",
            "expected_next_page_type": "video_list",
            "navigation_depth": 1,
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "bottom_navigation",
        "navigation_index": 2,
        "navigation_label": "视频",
    }
    assert frontier.observation.metadata["global_bottom_navigation_indices"] == (2,)


def test_h5_frontier_signs_contextual_bottom_navigation_capability() -> None:
    """完整六栏上下文映射签发的底栏候选必须保留结构化执行合同。"""
    detection = _detection(
        capture_id="capture-contextual-bottom-nav",
        evidence_id="artifact-contextual-bottom-nav",
    )
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": (
                "div.van-tabbar:visible > "
                "div.van-tabbar-item:nth-of-type(2):visible"
            ),
            "target_keywords": ["bottom_navigation", "二次元", "content_list"],
            "risk_level": "low",
            "purpose": "探索稳定页面中可见的底部导航业务分支",
            "expected_next_page_type": "content_list",
            "navigation_depth": 1,
            "navigation_knowledge_source": "contextual",
            "navigation_knowledge_confidence": 1.0,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "bottom_navigation",
        "navigation_index": 2,
        "navigation_label": "二次元",
    }
    assert frontier.candidates[0].metadata["navigation_knowledge_source"] == "contextual"


def test_h5_frontier_signs_unknown_bottom_navigation_without_type_guess() -> None:
    detection = _detection(
        capture_id="capture-unknown-bottom-nav",
        evidence_id="artifact-unknown-bottom-nav",
    )
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": (
                "div.van-tabbar:visible > "
                "div.van-tabbar-item:nth-of-type(3):visible"
            ),
            "target_keywords": ["bottom_navigation", "暗网"],
            "risk_level": "low",
            "purpose": "探索稳定页面中可见的底部导航业务分支",
            "expected_next_page_type": None,
            "navigation_depth": 1,
            "navigation_knowledge_source": "contextual",
            "navigation_knowledge_confidence": 1.0,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 14, 16, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["expected_next_page_type"] is None
    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "bottom_navigation",
        "navigation_index": 3,
        "navigation_label": "暗网",
    }


def test_h5_frontier_signs_pages_foot_bottom_navigation_family() -> None:
    detection = _detection(capture_id="capture-pages-foot", evidence_id="artifact-pages-foot")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "ul.pages-foot-fix:visible > li:nth-of-type(6):visible",
            "target_keywords": ["bottom_navigation", "我的", "profile"],
            "risk_level": "low",
            "purpose": "探索稳定页面中可见的底部导航业务分支",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "bottom_navigation",
        "navigation_index": 6,
        "navigation_label": "我的",
        "navigation_family": "pages_foot",
    }


def test_h5_frontier_signs_generic_spa_bottom_navigation_family() -> None:
    detection = _detection(capture_id="capture-generic-spa", evidence_id="artifact-generic-spa")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "internal:spa_bottom_navigation:nth-of-type(2)",
            "target_keywords": [
                "bottom_navigation",
                "视频",
                "spa_bottom_navigation",
                "video_list",
            ],
            "risk_level": "low",
            "purpose": "探索稳定页面中可见的自定义 SPA 底部导航业务分支",
            "expected_next_page_type": "video_list",
            "navigation_depth": 1,
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 27, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "bottom_navigation",
        "navigation_index": 2,
        "navigation_label": "视频",
        "navigation_family": "generic_spa",
    }


def test_h5_frontier_signs_flutter_bottom_navigation_family() -> None:
    detection = _detection(capture_id="capture-flutter-nav", evidence_id="artifact-flutter-nav")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "internal:flutter_bottom_navigation:nth-of-type(6)",
            "target_keywords": ["bottom_navigation", "我的", "profile"],
            "risk_level": "low",
            "purpose": "探索 Flutter 底部导航业务分支",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.95,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "bottom_navigation",
        "navigation_index": 6,
        "navigation_label": "我的",
        "navigation_family": "flutter_semantics",
    }


def test_h5_frontier_signs_deterministic_list_detail_capability() -> None:
    detection = _detection(capture_id="capture-video-list", evidence_id="artifact-video-list")
    detection.update({"page_type": "video_list", "page_state": "stable"})
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "当前列表首个内容卡片",
            "target_keywords": ["nested_content", "list_detail_navigation"],
            "risk_level": "low",
            "purpose": "探索当前业务列表首个真实可见的低风险内容卡片",
            "expected_next_page_type": "video_detail",
            "navigation_depth": 2,
            "list_detail_selector": ".grid-item .movie-item:visible",
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "list_detail_navigation",
        "list_detail_selector": ".grid-item .movie-item:visible",
        "source_page_type": "video_list",
        "expected_page_type": "video_detail",
    }


def test_h5_frontier_signs_profile_settings_navigation_capability() -> None:
    detection = _detection(capture_id="capture-profile", evidence_id="artifact-profile")
    detection.update({"page_type": "profile", "page_state": "stable"})
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "span.btn-edit:visible",
            "target_keywords": [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:class",
                "evidence:attributes",
            ],
            "risk_level": "low",
            "purpose": "探索我的页中由设置/编辑语义属性暴露的设置入口",
            "expected_next_page_type": "settings",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "profile_settings_navigation",
        "settings_entry_capability": "btn_edit",
        "source_page_type": "profile",
        "expected_page_type": "settings",
    }


def test_h5_frontier_signs_profile_options_settings_image_capability() -> None:
    detection = _detection(capture_id="capture-profile", evidence_id="artifact-profile")
    detection.update({"page_type": "profile", "page_state": "stable"})
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "个人中心设置图标",
            "target_keywords": [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:structure",
            ],
            "risk_level": "low",
            "purpose": "探索我的页中由设置/编辑语义属性暴露的设置入口",
            "expected_next_page_type": "settings",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "profile_settings_navigation",
        "settings_entry_capability": "options_right_image",
        "source_page_type": "profile",
        "expected_page_type": "settings",
    }


def test_h5_frontier_signs_flutter_profile_settings_capability() -> None:
    detection = _detection(capture_id="capture-profile", evidence_id="artifact-profile")
    detection.update({"page_type": "profile", "page_state": "stable"})
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "个人中心 Flutter 设置入口",
            "target_keywords": [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:structure",
            ],
            "risk_level": "low",
            "purpose": "探索 Flutter 我的页中的设置入口",
            "expected_next_page_type": "settings",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "profile_settings_navigation",
        "settings_entry_capability": "flutter_settings_arrow",
        "source_page_type": "profile",
        "expected_page_type": "settings",
    }


def test_h5_frontier_signs_flutter_settings_page_back_capability() -> None:
    detection = _detection(capture_id="capture-settings", evidence_id="artifact-settings")
    detection.update({"page_type": "settings", "page_state": "stable"})
    detection["candidate_actions"] = [
        {
            "action": "back",
            "target": "browser_history",
            "target_keywords": [
                "nested_return",
                "flutter_settings_page_back",
                "evidence:structure",
            ],
            "risk_level": "low",
            "purpose": "子页质检完成后返回来源页继续探索",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "settings_page_back",
        "settings_page_back_capability": "flutter_settings_page_back",
        "source_page_type": "settings",
    }


def test_h5_frontier_signs_flutter_profile_favorites_back_capability() -> None:
    detection = _detection(capture_id="capture-favorites", evidence_id="artifact-favorites")
    detection.update({"page_type": "video_list", "page_state": "empty_state"})
    detection["candidate_actions"] = [
        {
            "action": "back",
            "target": "browser_history",
            "target_keywords": [
                "nested_return",
                "flutter_profile_favorites_page_back",
                "evidence:structure",
            ],
            "risk_level": "low",
            "purpose": "收藏子页质检完成后返回个人中心继续探索",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "profile_favorites_page_back",
        "profile_favorites_page_back_capability": (
            "flutter_profile_favorites_page_back"
        ),
        "source_page_type": "video_list",
        "expected_page_type": "profile",
    }


def test_h5_frontier_signs_content_list_profile_favorites_back_capability() -> None:
    detection = _detection(capture_id="capture-favorites", evidence_id="artifact-favorites")
    detection.update({"page_type": "content_list", "page_state": "empty"})
    detection["candidate_actions"] = [
        {
            "action": "back",
            "target": "browser_history",
            "target_keywords": [
                "nested_return",
                "flutter_profile_favorites_page_back",
                "evidence:structure",
            ],
            "risk_level": "low",
            "purpose": "收藏子页质检完成后返回个人中心继续探索",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "profile_favorites_page_back",
        "profile_favorites_page_back_capability": (
            "flutter_profile_favorites_page_back"
        ),
        "source_page_type": "content_list",
        "expected_page_type": "profile",
    }


def test_h5_frontier_signs_profile_task_sign_activity_capability() -> None:
    detection = _detection(capture_id="capture-profile", evidence_id="artifact-profile")
    detection.update({"page_type": "profile", "page_state": "stable"})
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "个人中心签到入口",
            "target_keywords": [
                "activity_entry",
                "profile_local_action",
                "page_type:activity",
                "evidence:structure",
            ],
            "risk_level": "low",
            "purpose": "进入签到或活动相关入口",
            "expected_next_page_type": "activity",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "profile_activity_navigation",
        "activity_entry_capability": "task_sign_image",
        "source_page_type": "profile",
        "expected_page_type": "activity",
    }


@pytest.mark.parametrize(
    ("target", "target_keywords", "purpose", "expected_parameters"),
    [
        (
            "个人中心右上设置图标",
            [
                "settings_entry",
                "profile_local_action",
                "page_type:settings",
                "evidence:structure",
            ],
            "探索我的页头部设置入口",
            {
                "target_mode": "profile_settings_navigation",
                "settings_entry_capability": "top_right_icon_setting",
                "source_page_type": "profile",
                "expected_page_type": "settings",
            },
        ),
        (
            "个人中心会员支付入口",
            [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:structure",
                "capability:profile_vip_center_image",
            ],
            "探索我的页会员支付入口：立即开通",
            {
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "profile_vip_center_image",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        ),
        (
            "个人中心会员支付入口",
            [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:structure",
                "capability:mine_vip_banner_immediate_open_text",
            ],
            "探索我的页会员支付入口：立即开通",
            {
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "mine_vip_banner_immediate_open_text",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        ),
        (
            "个人中心会员支付入口",
            [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:会员中心",
                "evidence:structure",
                "capability:mine_vip_center_image",
            ],
            "探索我的页会员支付入口：会员中心",
            {
                "target_mode": "profile_payment_navigation",
                "payment_entry_capability": "mine_vip_center_image",
                "source_page_type": "profile",
                "expected_page_type": "payment",
            },
        ),
        (
            "个人中心每日签到图片",
            [
                "activity_entry",
                "profile_local_action",
                "page_type:activity",
                "evidence:structure",
            ],
            "进入每日签到活动入口",
            {
                "target_mode": "profile_activity_navigation",
                "activity_entry_capability": "daily_signin_image",
                "source_page_type": "profile",
                "expected_page_type": "activity",
            },
        ),
    ],
)
def test_h5_frontier_signs_image_only_profile_navigation_capabilities(
    target: str,
    target_keywords: list[str],
    purpose: str,
    expected_parameters: dict[str, str],
) -> None:
    detection = _detection(capture_id="capture-profile", evidence_id="artifact-profile")
    detection.update({"page_type": "profile", "page_state": "stable"})
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": target,
            "target_keywords": target_keywords,
            "risk_level": "low",
            "purpose": purpose,
            "expected_next_page_type": expected_parameters["expected_page_type"],
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == expected_parameters


def test_h5_frontier_normalizes_seek_media_to_bounded_halfway_target() -> None:
    detection = _detection(capture_id="capture-video", evidence_id="artifact-video")
    detection["candidate_actions"] = [
        {
            "action": "seek_media",
            "target": "视频播放进度条",
            "target_fraction": 9,
            "risk_level": "low",
            "purpose": "采集拖动后的媒体时间事实",
            "expected_next_page_type": "video_detail",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    candidate = frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.SEEK_MEDIA
    assert candidate.metadata["target_fraction"] == 0.5
    assert candidate.metadata["action_parameters"] == {"target_fraction": 0.5}


def test_h5_frontier_does_not_add_quality_capture_parameters_to_ad_candidate() -> None:
    detection = _detection(capture_id="capture-ad", evidence_id="artifact-ad")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "外链广告入口",
            "target_keywords": ["ad_outbound"],
            "risk_level": "low",
            "purpose": "采集广告落地页",
            "navigation_depth": 1,
        },
        {
            "action": "click",
            "target": "广告关闭按钮",
            "target_keywords": ["广告", "关闭"],
            "risk_level": "low",
            "purpose": "关闭广告",
            "navigation_depth": 0,
        },
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert "action_parameters" not in frontier.candidates[0].metadata
    assert "action_parameters" not in frontier.candidates[1].metadata


def test_h5_candidate_identity_ignores_quality_capture_language() -> None:
    detection = _detection(capture_id="capture-ad-identity", evidence_id="artifact-ad")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "推广入口",
            "risk_level": "low",
            "purpose": "打开活动详情",
            "navigation_depth": 1,
        },
        {
            "action": "click",
            "target": "推广入口",
            "target_keywords": ["ad_outbound"],
            "risk_level": "low",
            "purpose": "采集广告落地页",
            "navigation_depth": 1,
        },
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    assert "action_parameters" not in frontier.candidates[0].metadata


def _profile_visual_detection() -> dict[str, object]:
    detection = _detection(capture_id="capture-1", evidence_id="artifact-profile")
    detection["page_url"] = "https://example.test/profile"
    detection["page_type"] = "profile"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "立即开通",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:screenshot",
            ],
            "risk_level": "medium",
            "purpose": "探索我的页会员支付入口：立即开通",
            "expected_next_page_type": "payment",
            "navigation_depth": 1,
            "visual_bounds": {
                "screenshot_index": 1,
                "artifact_id": "artifact-profile",
                "sha256": "sha256:" + "1" * 64,
                "left_ratio": 0.1,
                "top_ratio": 0.4,
                "right_ratio": 0.9,
                "bottom_ratio": 0.5,
                "confidence": 0.92,
            },
        }
    ]
    return detection


def test_profile_vip_structure_candidate_projects_controlled_action_parameters() -> None:
    detection = _detection(capture_id="capture-1", evidence_id="artifact-profile")
    detection["page_url"] = "https://example.test/mine"
    detection["page_type"] = "profile"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "个人中心会员支付入口",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:structure",
                "capability:payment_me_tab_same_node_image",
            ],
            "risk_level": "low",
            "purpose": "探索我的页会员支付入口：立即开通",
            "expected_next_page_type": "payment",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.metadata["action_parameters"] == {
        "target_mode": "profile_payment_navigation",
        "payment_entry_capability": "payment_me_tab_same_node_image",
        "source_page_type": "profile",
        "expected_page_type": "payment",
    }


def test_profile_vip_text_structure_projects_controlled_action_parameters() -> None:
    detection = _detection(capture_id="capture-1", evidence_id="artifact-profile")
    detection["page_url"] = "https://example.test/mine"
    detection["page_type"] = "profile"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "个人中心会员支付入口",
            "target_keywords": [
                "profile_vip_payment_entry",
                "profile_local_action",
                "page_type:payment",
                "entry:立即开通",
                "evidence:structure",
                "capability:vip_section_immediate_open_text",
            ],
            "risk_level": "low",
            "purpose": "探索我的页会员支付入口：立即开通",
            "expected_next_page_type": "payment",
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 24, 13, 30, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "profile_payment_navigation",
        "payment_entry_capability": "vip_section_immediate_open_text",
        "source_page_type": "profile",
        "expected_page_type": "payment",
    }


def test_profile_vip_visual_candidate_without_structure_enters_frontier() -> None:
    frontier = _build_frontier(
        detection_result=_profile_visual_detection(),
        observed_at=datetime(2026, 8, 14, 10, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.semantic_target == "立即开通"
    assert candidate.metadata["action_parameters"]["target_mode"] == "visual_coordinate"
    assert candidate.metadata["action_parameters"]["visual_entry_kind"] == (
        "profile_vip_payment"
    )


@pytest.mark.parametrize(
    ("target", "expected_page_type", "entry_kind", "entry_keyword"),
    [
        ("签到中心", "activity", "profile_activity_ocr", "profile_activity_entry"),
        ("设置", "settings", "profile_settings_image", "profile_settings_entry"),
        ("会员中心", "payment", "profile_vip_payment", "profile_vip_payment_entry"),
    ],
)
def test_profile_visual_entry_projects_controlled_action_parameters(
    target: str,
    expected_page_type: str,
    entry_kind: str,
    entry_keyword: str,
) -> None:
    detection = _profile_visual_detection()
    raw_actions = detection["candidate_actions"]
    assert isinstance(raw_actions, list)
    raw = raw_actions[0]
    assert isinstance(raw, dict)
    raw.update(
        {
            "target": target,
            "target_keywords": [
                entry_keyword,
                "profile_local_action",
                f"page_type:{expected_page_type}",
                f"entry:{target}",
                "evidence:screenshot",
            ],
            "purpose": f"探索我的页视觉入口：{target}",
            "expected_next_page_type": expected_page_type,
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 29, 11, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    parameters = frontier.candidates[0].metadata["action_parameters"]
    assert parameters["target_mode"] == "visual_coordinate"
    assert parameters["visual_entry_kind"] == entry_kind


@pytest.mark.parametrize(
    "mutation",
    [
        {"target": "其他入口"},
        {"risk_level": "low"},
        {"page_type": "home"},
        {"visual_bounds": None},
    ],
)
def test_profile_vip_visual_candidate_rejects_untrusted_projection(
    mutation: dict[str, object],
) -> None:
    detection = _profile_visual_detection()
    raw_actions = detection["candidate_actions"]
    assert isinstance(raw_actions, list)
    raw = raw_actions[0]
    assert isinstance(raw, dict)
    if "page_type" in mutation:
        detection["page_type"] = mutation["page_type"]
    else:
        raw.update(mutation)

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 14, 10, 1, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates == ()


def test_multiple_profile_vip_visual_guesses_do_not_enter_frontier() -> None:
    detection = _profile_visual_detection()
    raw_actions = detection["candidate_actions"]
    assert isinstance(raw_actions, list)
    original = raw_actions[0]
    assert isinstance(original, dict)
    shifted = {
        **original,
        "visual_bounds": {
            "screenshot_index": 1,
            "artifact_id": "artifact-profile",
            "sha256": "sha256:" + "2" * 64,
            "left_ratio": 0.12,
            "top_ratio": 0.42,
            "right_ratio": 0.88,
            "bottom_ratio": 0.52,
            "confidence": 0.93,
        },
    }
    detection["candidate_actions"] = [original, shifted]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 14, 10, 2, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates == ()


@pytest.mark.parametrize("expected_page_type", ["activity", "settings", "payment"])
def test_profile_local_page_types_do_not_enter_frontier_from_home(
    expected_page_type: str,
) -> None:
    detection = _detection(capture_id="capture-home", evidence_id="artifact-home")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": f"打开{expected_page_type}",
            "target_keywords": [f"page_type:{expected_page_type}"],
            "risk_level": "low",
            "purpose": "模型误报的个人中心专属入口",
            "expected_next_page_type": expected_page_type,
            "navigation_depth": 1,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates == ()


def test_h5_frontier_compatibly_normalizes_legacy_goto_url() -> None:
    detection = _detection(capture_id="capture-legacy-goto", evidence_id="artifact-legacy-goto")
    detection["candidate_actions"] = [
        {
            "action": "goto",
            "url": "https://example.test/products",
            "risk_level": "low",
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    candidate = frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.NAVIGATE
    assert candidate.semantic_target == "https://example.test/products"
    assert candidate.metadata["destination_url"] == "https://example.test/products"


@pytest.mark.parametrize(
    ("destination_url", "reason_code"),
    [
        (None, "h5_navigation_missing_url"),
        ("javascript:alert(1)", "h5_navigation_invalid_scheme"),
        ("https://other.test/products", "h5_navigation_cross_origin"),
        ("https://example.test/admin", "h5_navigation_route_out_of_scope"),
    ],
)
def test_h5_navigation_rejection_has_stable_reason(
    destination_url: str | None,
    reason_code: str,
) -> None:
    module = import_module("services.api.app.application.h5_exploration_observation")

    decision = module.evaluate_h5_navigation_candidate(
        destination_url=destination_url,
        entry_url="https://example.test/shop",
        allowed_route_prefixes=("/shop",),
        navigation_depth=1,
        max_depth=2,
    )

    assert decision.eligible is False
    assert decision.reason_code == reason_code


def test_h5_navigation_depth_rejection_is_auditable() -> None:
    module = import_module("services.api.app.application.h5_exploration_observation")

    decision = module.evaluate_h5_navigation_candidate(
        destination_url="https://example.test/shop/products",
        entry_url="https://example.test/shop",
        allowed_route_prefixes=("/shop",),
        navigation_depth=3,
        max_depth=2,
    )

    assert decision.reason_code == "h5_navigation_depth_exceeded"


def test_detection_builds_web_observation_and_bounded_frontier() -> None:
    frontier = _build_frontier(
        detection_result=_detection(capture_id="capture-1", evidence_id="artifact-shot-1"),
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    observation = frontier.observation
    assert observation.capture_id == "capture-1"
    assert observation.surface_kind is SurfaceKind.WEB
    assert observation.location == "https://m.example.test/home?campaign=7"
    assert observation.page_type == "home"
    assert observation.page_state == "stable"
    assert observation.key_elements == ("首页", "我的", "nav", "main", "bottom-tabs")
    assert observation.metadata["evidence_artifact_ids"] == (
        "artifact-shot-1",
        "artifact-dom",
    )
    assert "dom" not in observation.metadata
    assert "screenshot" not in observation.metadata

    assert len(frontier.candidates) == 2
    assert frontier.candidates[0].action_type is InteractionOperation.CLICK
    assert frontier.candidates[0].risk_level is InteractionRiskLevel.LOW
    assert frontier.candidates[1].risk_level is InteractionRiskLevel.HIGH
    assert frontier.blocked_candidate_ids == (frontier.candidates[1].candidate_id,)


def test_platform_locator_candidates_are_skipped_not_fatal() -> None:
    """模型吐平台定位器时只丢该候选；整轮观察不能因此崩掉、把任务判失败。"""
    detection = _detection(capture_id="capture-locator", evidence_id="artifact-locator")
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "#login-btn",
            "target_keywords": [],
            "risk_level": "low",
            "purpose": "CSS id 选择器",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
        },
        {
            "action": "click",
            "target": "//button[@type='submit']",
            "target_keywords": [],
            "risk_level": "low",
            "purpose": "XPath",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
        },
        {
            "action": "click",
            "target": "css=.btn-primary",
            "target_keywords": [],
            "risk_level": "low",
            "purpose": "带前缀的 CSS 定位器",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
        },
        {
            "action": "click",
            "target": "我的",
            "target_keywords": ["个人中心"],
            "risk_level": "low",
            "purpose": "同批次里的合法语义目标必须保留",
            "expected_next_page_type": "profile",
            "navigation_depth": 1,
        },
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 14, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )

    assert [candidate.semantic_target for candidate in frontier.candidates] == ["我的"]


def test_trusted_startup_ad_dismissal_projects_semantic_capability() -> None:
    """确定性开屏关闭候选必须语义化，且只投影固定的平台能力。"""
    detection = _detection(capture_id="capture-startup-ad", evidence_id="artifact-ad")
    detection["page_type"] = "startup_ad"
    detection["page_state"] = "countdown"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "跳过",
            "target_keywords": [
                "startup_ad",
                "close_control",
                "startup_ad_dismissal",
            ],
            "risk_level": "low",
            "purpose": "关闭开屏广告并在同一浏览器会话进入首页",
            "expected_next_page_type": "home",
            "navigation_depth": 0,
            "obstruction_capability": "startup_ad_dismissal",
            "obstruction_signature": "h5-obstruction:" + "1" * 64,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.semantic_target == "跳过"
    assert candidate.metadata["action_parameters"] == {
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
        "obstruction_signature": "h5-obstruction:" + "1" * 64,
    }


def test_unsigned_startup_ad_skip_uses_bounded_reobservation() -> None:
    detection = _detection(capture_id="capture-startup-ad", evidence_id="artifact-ad")
    detection.update(
        {
            "page_type": "startup_ad",
            "page_state": "countdown",
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "跳过",
                    "target_keywords": ["startup_ad", "close_control"],
                    "risk_level": "low",
                    "purpose": "模型推测关闭开屏广告",
                    "expected_next_page_type": "home",
                    "navigation_depth": 0,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 28, 9, 1, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
    assert frontier.candidates[0].metadata["action_parameters"]["target_mode"] == (
        "startup_ad_countdown_reobserve"
    )


def test_startup_ad_enter_control_projects_same_dismissal_capability() -> None:
    """杏吧实测：开屏角标写「进入」，和「跳过」是同一个离开开屏的控件。"""
    detection = _detection(capture_id="capture-startup-enter", evidence_id="artifact-enter")
    detection["page_type"] = "startup_ad"
    detection["page_state"] = "countdown"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "进入",
            "target_keywords": [
                "startup_ad",
                "close_control",
                "startup_ad_dismissal",
            ],
            "risk_level": "low",
            "purpose": "关闭开屏广告并在同一浏览器会话进入首页",
            "expected_next_page_type": "home",
            "navigation_depth": 0,
            "obstruction_capability": "startup_ad_dismissal",
            "obstruction_signature": "h5-obstruction:" + "2" * 64,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
        "obstruction_signature": "h5-obstruction:" + "2" * 64,
    }


def test_startup_ad_unrelated_corner_target_is_not_dismissal_capability() -> None:
    """开屏上的其它文案不能借开屏清障能力点下去，只能回到有界重观察。"""
    detection = _detection(capture_id="capture-startup-other", evidence_id="artifact-other")
    detection["page_type"] = "startup_ad"
    detection["page_state"] = "countdown"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "领取奖励",
            "target_keywords": ["startup_ad", "close_control"],
            "risk_level": "low",
            "purpose": "关闭开屏广告并在同一浏览器会话进入首页",
            "expected_next_page_type": "home",
            "navigation_depth": 0,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert all(
        candidate.metadata.get("action_parameters", {}).get("target_mode")
        != "startup_ad_dismissal"
        for candidate in frontier.candidates
    )


def test_consecutive_startup_ads_keep_distinct_dismissal_occurrences() -> None:
    """成功跳过一层后，新 Capture 上的下一层开屏广告必须仍可继续关闭。"""
    first_detection = _detection(
        capture_id="capture-startup-ad-1",
        evidence_id="artifact-ad-1",
    )
    first_detection.update(
        {
            "page_type": "startup_ad",
            "page_state": "countdown",
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "跳过",
                    "target_keywords": [
                        "startup_ad",
                        "close_control",
                        "startup_ad_dismissal",
                    ],
                    "risk_level": "low",
                    "purpose": "关闭当前开屏广告并继续进入首页",
                    "expected_next_page_type": "home",
                    "navigation_depth": 0,
                    "obstruction_capability": "startup_ad_dismissal",
                    "obstruction_signature": "h5-obstruction:" + "3" * 64,
                }
            ],
        }
    )
    second_detection = dict(first_detection)
    second_detection["page_capture_id"] = "capture-startup-ad-2"
    second_detection["evidence_artifact_ids"] = ["artifact-ad-2"]

    first = _build_frontier(
        detection_result=first_detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )
    second = _build_frontier(
        detection_result=second_detection,
        observed_at=datetime(2026, 8, 15, 9, 1, tzinfo=UTC),
        navigation_depth=0,
        executed_candidate_ids=(first.candidates[0].candidate_id,),
        lineage_metadata={"startup_ad_dismissal_count": 1},
    )

    assert first.observation.state_version == second.observation.state_version
    assert second.candidates[0].semantic_target == "跳过"
    assert second.candidates[0].candidate_id != first.candidates[0].candidate_id
    assert second.observation.metadata["startup_ad_dismissal_count"] == 1


def test_consecutive_startup_ad_dismissal_has_a_hard_limit() -> None:
    """连续开屏广告异常轮播时必须有界收口，不能消耗全部任务动作预算。"""
    detection = _detection(capture_id="capture-startup-limit", evidence_id="artifact-ad")
    detection.update(
        {
            "page_type": "startup_ad",
            "page_state": "countdown",
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "跳过",
                    "target_keywords": ["startup_ad", "close_control"],
                    "risk_level": "low",
                    "purpose": "关闭当前开屏广告并继续进入首页",
                    "expected_next_page_type": "home",
                    "navigation_depth": 0,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 2, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"startup_ad_dismissal_count": 10},
    )

    assert frontier.candidates == ()
    assert frontier.observation.metadata["startup_ad_dismissal_exhausted"] is True


def test_trusted_popup_dismissal_projects_semantic_capability_and_stable_state() -> None:
    """可信弹窗关闭能力必须保持纯语义，并以 DOM 指纹稳定同层状态。"""
    first_detection = _detection(capture_id="capture-popup-1", evidence_id="artifact-popup-1")
    first_detection["page_state"] = "popup"
    first_detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "关闭当前弹窗",
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                "dialog_content_close",
            ],
            "risk_level": "low",
            "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
            "expected_next_page_type": None,
            "navigation_depth": 0,
            "obstruction_capability": "dialog_content_close",
            "obstruction_signature": "h5-obstruction:" + "a" * 64,
        }
    ]
    second_detection = dict(first_detection)
    second_detection["page_capture_id"] = "capture-popup-2"
    second_detection["matched_catalog_signals"] = {
        "ocr_keywords": ["模型输出发生波动"],
        "dom_hints": ["different-model-hint"],
        "layout_hints": [],
    }
    second_detection["risk_flags"] = ["volatile-model-flag"]

    first = _build_frontier(
        detection_result=first_detection,
        observed_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )
    second = _build_frontier(
        detection_result=second_detection,
        observed_at=datetime(2026, 8, 15, 9, 1, tzinfo=UTC),
        navigation_depth=0,
    )

    assert first.observation.state_version == second.observation.state_version
    assert first.observation.overlay_signature == second.observation.overlay_signature
    assert first.candidates[0].candidate_id != second.candidates[0].candidate_id
    candidate = first.candidates[0]
    assert candidate.semantic_target == "关闭当前弹窗"
    assert candidate.metadata["source_page_type"] == "home"
    assert candidate.metadata["obstruction_signature"] == "h5-obstruction:" + "a" * 64
    assert candidate.metadata["action_parameters"] == {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": "dialog_content_close",
        "obstruction_signature": "h5-obstruction:" + "a" * 64,
    }

    repeated = _build_frontier(
        detection_result=second_detection,
        observed_at=datetime(2026, 8, 15, 9, 1, tzinfo=UTC),
        navigation_depth=0,
        executed_candidate_ids=(candidate.candidate_id,),
    )
    assert len(repeated.candidates) == 1
    assert repeated.candidates[0].candidate_id == second.candidates[0].candidate_id


@pytest.mark.parametrize("page_state", ["popup_obstructed", "popup_overlay"])
def test_trusted_popup_dismissal_projects_compound_overlay_state(
    page_state: str,
) -> None:
    """模型复合遮挡态不得让已验签关闭能力在 Frontier 投影时丢参。"""
    detection = _detection(
        capture_id=f"capture-{page_state}",
        evidence_id=f"artifact-{page_state}",
    )
    detection["page_state"] = page_state
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "关闭当前弹窗",
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                "flutter_centered_ad_popup_close",
            ],
            "risk_level": "low",
            "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
            "expected_next_page_type": None,
            "navigation_depth": 0,
            "obstruction_capability": "flutter_centered_ad_popup_close",
            "obstruction_signature": "h5-obstruction:" + "d" * 64,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 27, 22, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.observation.overlay_signature is not None
    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": "flutter_centered_ad_popup_close",
        "obstruction_signature": "h5-obstruction:" + "d" * 64,
    }


def test_rejected_popup_capability_reobserves_without_generic_text_fallback() -> None:
    """弹窗能力合同失败后只能重观察，不能退化为“关闭当前弹窗”文本点击。"""
    detection = _detection(
        capture_id="capture-popup-projection-rejected",
        evidence_id="artifact-popup-projection-rejected",
    )
    detection["page_state"] = "popup_overlay"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "关闭当前弹窗",
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                "flutter_centered_ad_popup_close",
            ],
            "risk_level": "low",
            "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
            "expected_next_page_type": None,
            "navigation_depth": 0,
            "obstruction_capability": "flutter_centered_ad_popup_close",
            "obstruction_signature": "invalid-signature",
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 27, 22, 5, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.OBSERVE
    assert candidate.semantic_target == "重新观察能力投影失败页面"
    assert candidate.metadata["action_parameters"] == {
        "target_mode": "capability_projection_reobserve",
        "capability_projection_reobserve_attempt": 1,
    }
    assert tuple(
        dict(item)
        for item in frontier.observation.metadata["capability_projection_rejections"]
    ) == (
        {
            "target_mode": "popup_dismissal",
            "semantic_target": "关闭当前弹窗",
            "reason_code": "popup_capability_contract_invalid",
        },
    )


def test_rejected_popup_capability_reobserve_is_bounded() -> None:
    detection = _detection(
        capture_id="capture-popup-projection-exhausted",
        evidence_id="artifact-popup-projection-exhausted",
    )
    detection["page_state"] = "popup_obstructed"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "关闭当前弹窗",
            "target_keywords": ["popup_dismissal", "close_control"],
            "risk_level": "low",
            "navigation_depth": 0,
            "obstruction_capability": "unknown_popup_close",
            "obstruction_signature": "h5-obstruction:" + "e" * 64,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 27, 22, 6, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"capability_projection_reobserve_count": 3},
    )

    assert frontier.candidates == ()
    assert (
        frontier.observation.metadata["capability_projection_reobserve_exhausted"]
        is True
    )


def test_challenge_popup_rejection_never_reobserves_or_falls_back_to_click() -> None:
    detection = _detection(
        capture_id="capture-verification-popup",
        evidence_id="artifact-verification-popup",
    )
    detection["page_state"] = "verification_popup"
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "关闭当前弹窗",
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                "dialog_overlay",
            ],
            "risk_level": "low",
            "navigation_depth": 0,
            "obstruction_capability": "dialog_overlay",
            "obstruction_signature": "h5-obstruction:" + "f" * 64,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 27, 22, 7, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates == ()
    assert (
        frontier.observation.metadata["capability_projection_reobserve_exhausted"]
        is False
    )


def test_captcha_verification_never_projects_generic_popup_dismissal() -> None:
    """验证码必须等待人工验证，不能作为普通弹窗跨过或反复尝试关闭。"""
    detection = _detection(capture_id="capture-captcha", evidence_id="artifact-captcha")
    detection.update(
        {
            "detection_status": "insufficient",
            "page_type": None,
            "page_state": "verification_required",
            "risk_flags": ["captcha_required", "verification_blocking"],
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "关闭当前弹窗",
                    "target_keywords": [
                        "popup_dismissal",
                        "close_control",
                        "dialog_overlay",
                    ],
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                    "expected_next_page_type": None,
                    "navigation_depth": 0,
                    "obstruction_capability": "dialog_overlay",
                    "obstruction_signature": "h5-obstruction:" + "c" * 64,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 15, 9, 3, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.candidates == ()
    assert frontier.observation.metadata["captcha_verification_required"] is True


@pytest.mark.parametrize(
    "capability",
    [
        "homepage_payment_close",
        "flutter_app_open_popup_close",
        "flutter_centered_ad_popup_close",
        "flutter_centered_ad_popup_close_above",
        "flutter_centered_scroll_popup_close_below",
        "flutter_image_popup_top_right_close",
        "flutter_account_notice_top_right_close",
        "modal_local_safe_acknowledgement",
    ],
)
def test_exact_popup_close_projects_signed_popup_action_parameters(
    capability: str,
) -> None:
    """精确结构签发的站点关闭能力必须完整进入 Frontier。"""
    detection = _detection(
        capture_id="capture-homepage-payment",
        evidence_id="artifact-homepage-payment",
    )
    detection.update({"page_type": "home", "page_state": "popup"})
    detection["obstruction_facts"] = {
        "kind": "blocking_overlay",
        "blocking": True,
        "snapshot_evidence_status": "ready",
        "modal_census_complete": True,
        "modal_census_count": 1,
        "obstruction_without_safe_action": False,
    }
    detection["candidate_actions"] = [
        {
            "action": "click",
            "target": "关闭当前弹窗",
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                capability,
            ],
            "risk_level": "low",
            "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
            "expected_next_page_type": "home",
            "navigation_depth": 0,
            "obstruction_capability": capability,
            "obstruction_signature": "h5-obstruction:" + "b" * 64,
        }
    ]

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 20, 16, 13, tzinfo=UTC),
        navigation_depth=0,
    )

    candidate = frontier.candidates[0]
    assert frontier.observation.metadata["obstruction_kind"] == "blocking_overlay"
    assert frontier.observation.metadata["obstruction_blocking"] is True
    assert frontier.observation.metadata["obstruction_modal_census_complete"] is True
    assert frontier.observation.metadata["obstruction_modal_census_count"] == 1
    assert (
        frontier.observation.metadata["obstruction_snapshot_evidence_status"]
        == "ready"
    )
    assert frontier.observation.metadata["obstruction_without_safe_action"] is False
    assert candidate.metadata["obstruction_signature"] == "h5-obstruction:" + "b" * 64
    assert candidate.metadata["action_parameters"] == {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": capability,
        "obstruction_signature": "h5-obstruction:" + "b" * 64,
    }


def test_candidate_depth_keeps_detection_absolute_depth_or_current_depth() -> None:
    detection = _detection(capture_id="capture-depth", evidence_id="artifact-depth")
    detection["candidate_actions"][1]["target"] = "展开筛选"
    detection["candidate_actions"][1]["expected_next_page_type"] = None
    detection["candidate_actions"][1]["navigation_depth"] = None

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        navigation_depth=2,
    )

    assert frontier.candidates[0].navigation_depth == 1
    assert frontier.candidates[1].navigation_depth == 2


def test_state_and_candidate_identity_do_not_depend_on_capture_id() -> None:
    first = _build_frontier(
        detection_result=_detection(capture_id="capture-1", evidence_id="artifact-shot-1"),
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        navigation_depth=1,
    )
    second = _build_frontier(
        detection_result=_detection(capture_id="capture-2", evidence_id="artifact-shot-2"),
        observed_at=datetime(2026, 8, 11, 9, 1, tzinfo=UTC),
        navigation_depth=1,
    )

    assert first.observation.state_version == second.observation.state_version
    assert [item.candidate_id for item in first.candidates] == [
        item.candidate_id for item in second.candidates
    ]
    assert first.snapshot_id != second.snapshot_id


def test_same_semantic_action_on_different_source_states_has_distinct_identity() -> None:
    home_detection = _detection(capture_id="capture-home", evidence_id="artifact-home")
    profile_detection = _detection(
        capture_id="capture-profile",
        evidence_id="artifact-profile",
    )
    profile_detection["page_type"] = "profile"

    home = _build_frontier(
        detection_result=home_detection,
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )
    profile = _build_frontier(
        detection_result=profile_detection,
        observed_at=datetime(2026, 8, 11, 9, 1, tzinfo=UTC),
        navigation_depth=1,
    )

    assert home.candidates[0].candidate_id != profile.candidates[0].candidate_id


def test_detection_lineage_is_preserved_without_raw_page_payload() -> None:
    frontier = _build_frontier(
        detection_result=_detection(capture_id="capture-3", evidence_id="artifact-shot-3"),
        observed_at=datetime(2026, 8, 11, 9, 2, tzinfo=UTC),
        navigation_depth=2,
        visited_state_ids=("state-parent",),
        lineage_metadata={"parent_capture_id": "capture-2", "action_execution_id": "action-1"},
    )

    assert frontier.visited_state_ids == ("state-parent",)
    assert frontier.observation.metadata["parent_capture_id"] == "capture-2"
    assert frontier.observation.metadata["action_execution_id"] == "action-1"
    assert frontier.observation.metadata["detector_model"] == "detector-test"
    assert frontier.observation.metadata["detection_confidence"] == 0.92


def test_executed_candidates_are_removed_when_root_state_is_revisited() -> None:
    first = _build_frontier(
        detection_result=_detection(capture_id="capture-1", evidence_id="artifact-shot-1"),
        observed_at=datetime(2026, 8, 11, 9, 0, tzinfo=UTC),
        navigation_depth=0,
    )
    executed_id = first.candidates[0].candidate_id

    revisited = _build_frontier(
        detection_result=_detection(capture_id="capture-2", evidence_id="artifact-shot-2"),
        observed_at=datetime(2026, 8, 11, 9, 1, tzinfo=UTC),
        navigation_depth=0,
        executed_candidate_ids=(executed_id,),
    )

    assert executed_id not in {item.candidate_id for item in revisited.candidates}
    assert revisited.observation.metadata["executed_candidate_ids"] == (executed_id,)


def test_only_empty_root_frontier_can_emit_objective_satisfied_signal() -> None:
    detection = _detection(capture_id="capture-empty", evidence_id="artifact-empty")
    detection["candidate_actions"] = []

    root = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 2, tzinfo=UTC),
        navigation_depth=0,
    )
    nested = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 3, tzinfo=UTC),
        navigation_depth=2,
    )
    popup_detection = dict(detection)
    popup_detection["page_state"] = "popup"
    popup = _build_frontier(
        detection_result=popup_detection,
        observed_at=datetime(2026, 8, 11, 9, 3, 30, tzinfo=UTC),
        navigation_depth=0,
    )
    auth_detection = dict(detection)
    auth_detection["page_state"] = "login_wall"
    auth = _build_frontier(
        detection_result=auth_detection,
        observed_at=datetime(2026, 8, 11, 9, 4, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"requires_auth": False, "objective_satisfied": True},
    )

    assert root.observation.metadata["objective_satisfied"] is True
    assert nested.observation.metadata["objective_satisfied"] is False
    assert popup.observation.metadata["objective_satisfied"] is False
    assert auth.observation.metadata["requires_auth"] is True
    assert auth.observation.metadata["objective_satisfied"] is False


def test_home_popup_with_trusted_close_candidate_does_not_require_auth() -> None:
    detection = _detection(capture_id="capture-home-popup", evidence_id="artifact-popup")
    detection.update(
        {
            "page_state": "popup",
            "risk_flags": ["authentication_required", "ads_present"],
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "关闭当前弹窗",
                    "target_keywords": [
                        "popup_dismissal",
                        "close_control",
                        "dialog_overlay",
                    ],
                    "risk_level": "low",
                    "purpose": "关闭当前遮挡弹窗并在同一浏览器会话重新采集",
                    "expected_next_page_type": "home",
                    "navigation_depth": 0,
                    "obstruction_capability": "dialog_overlay",
                    "obstruction_signature": (
                        "h5-obstruction:"
                        "85ee927720e98ac1c7e9002bef17ff28980f548837f56b80b0c471f8a3d9b407"
                    ),
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 24, 10, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.observation.metadata["requires_auth"] is False
    assert frontier.observation.page_state == "popup"
    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.semantic_target == "关闭当前弹窗"
    assert candidate.metadata["action_parameters"]["target_mode"] == "popup_dismissal"


def test_line_loading_injects_bounded_same_session_observe_candidate() -> None:
    detection = _detection(capture_id="capture-line", evidence_id="artifact-line")
    detection.update(
        {
            "page_type": "line_loading",
            "page_state": "line_loading",
            "candidate_actions": [],
        }
    )

    first = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 5, tzinfo=UTC),
        navigation_depth=0,
    )
    third = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 6, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"line_loading_reobserve_count": 2},
    )
    exhausted = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 7, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"line_loading_reobserve_count": 3},
    )

    assert len(first.candidates) == 1
    assert first.candidates[0].action_type is InteractionOperation.OBSERVE
    assert first.candidates[0].navigation_depth == 0
    assert first.candidates[0].metadata["line_loading_reobserve_attempt"] == 1
    assert third.candidates[0].metadata["line_loading_reobserve_attempt"] == 3
    assert first.candidates[0].candidate_id != third.candidates[0].candidate_id
    assert exhausted.candidates == ()
    assert exhausted.observation.metadata["objective_satisfied"] is False
    assert exhausted.observation.metadata["line_loading_reobserve_exhausted"] is True


def test_line_loading_login_failure_prefers_structural_retry_before_reobserve() -> None:
    detection = _detection(capture_id="capture-login-retry", evidence_id="artifact-login")
    detection.update(
        {
            "page_type": "line_loading",
            "page_state": "login_required",
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "重试线路页登录",
                    "target_keywords": [
                        "line_loading_login_retry",
                        "authentication_retry",
                        "evidence:structure",
                    ],
                    "risk_level": "low",
                    "purpose": "重试线路页匿名登录并在当前浏览器会话重新采集",
                    "expected_next_page_type": None,
                    "navigation_depth": 0,
                    "obstruction_capability": "line_loading_login_retry",
                    "obstruction_signature": "h5-obstruction:" + "a" * 64,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 5, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    candidate = frontier.candidates[0]
    assert candidate.action_type is InteractionOperation.CLICK
    assert candidate.semantic_target == "重试线路页登录"
    assert candidate.metadata["action_parameters"] == {
        "target_mode": "line_loading_login_retry",
        "line_loading_login_retry_capability": "line_loading_login_retry",
        "obstruction_signature": "h5-obstruction:" + "a" * 64,
    }


def test_startup_ad_countdown_injects_bounded_same_session_observe_candidate() -> None:
    detection = _detection(capture_id="capture-countdown", evidence_id="artifact-countdown")
    detection.update(
        {
            "page_type": "startup_ad",
            "page_state": "countdown",
            "candidate_actions": [],
        }
    )

    first = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 8, tzinfo=UTC),
        navigation_depth=0,
    )
    tenth = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 9, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"startup_ad_countdown_reobserve_count": 9},
    )
    exhausted = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 10, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"startup_ad_countdown_reobserve_count": 10},
    )

    assert len(first.candidates) == 1
    assert first.candidates[0].action_type is InteractionOperation.OBSERVE
    assert first.candidates[0].navigation_depth == 0
    assert first.candidates[0].metadata["startup_ad_countdown_reobserve_attempt"] == 1
    assert tenth.candidates[0].metadata["startup_ad_countdown_reobserve_attempt"] == 10
    assert first.candidates[0].candidate_id != tenth.candidates[0].candidate_id
    assert exhausted.candidates == ()
    assert exhausted.observation.metadata["objective_satisfied"] is False
    assert (
        exhausted.observation.metadata["startup_ad_countdown_reobserve_exhausted"]
        is True
    )


def test_stable_startup_ad_without_dismissal_still_reobserves() -> None:
    """开屏广告即使被模型标为 stable，也不能被当作根层业务完成态。"""
    detection = _detection(capture_id="capture-stable-ad", evidence_id="artifact-stable-ad")
    detection.update(
        {
            "page_type": "startup_ad",
            "page_state": "stable",
            "candidate_actions": [],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 10, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
    assert frontier.observation.metadata["objective_satisfied"] is False


def test_empty_flutter_bootstrap_injects_bounded_same_session_observe_candidate() -> None:
    detection = _detection(capture_id="capture-bootstrap", evidence_id="artifact-bootstrap")
    detection.update(
        {
            "detection_status": "insufficient",
            "page_type": None,
            "page_state": "needs_more_observation",
            "candidate_actions": [],
            "risk_flags": ["deterministic_flutter_bootstrap_pending"],
        }
    )

    first = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 10, tzinfo=UTC),
        navigation_depth=0,
    )
    fifth = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 11, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"flutter_bootstrap_reobserve_count": 4},
    )
    exhausted = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 12, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"flutter_bootstrap_reobserve_count": 5},
    )

    assert len(first.candidates) == 1
    assert first.candidates[0].action_type is InteractionOperation.OBSERVE
    assert first.candidates[0].navigation_depth == 0
    assert first.candidates[0].metadata["flutter_bootstrap_reobserve_attempt"] == 1
    assert fifth.candidates[0].metadata["flutter_bootstrap_reobserve_attempt"] == 5
    assert first.candidates[0].candidate_id != fifth.candidates[0].candidate_id
    assert exhausted.candidates == ()
    assert exhausted.observation.metadata["objective_satisfied"] is False
    assert exhausted.observation.metadata["flutter_bootstrap_reobserve_exhausted"] is True


def test_insufficient_unknown_page_injects_three_bounded_same_session_observes() -> None:
    """普通未稳定页不能因没有模型候选而在首次采集后直接 no_progress。"""

    detection = _detection(capture_id="capture-insufficient", evidence_id="artifact-insufficient")
    detection.update(
        {
            "detection_status": "insufficient",
            "page_type": None,
            "page_state": "needs_more_observation",
            "candidate_actions": [],
            "risk_flags": [],
        }
    )

    first = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 13, tzinfo=UTC),
        navigation_depth=0,
    )
    third = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 14, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"insufficient_reobserve_count": 2},
    )
    exhausted = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 15, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"insufficient_reobserve_count": 3},
    )

    assert first.candidates[0].action_type is InteractionOperation.OBSERVE
    assert first.candidates[0].semantic_target == "重新观察未稳定页面"
    assert first.candidates[0].metadata["insufficient_reobserve_attempt"] == 1
    assert third.candidates[0].metadata["insufficient_reobserve_attempt"] == 3
    assert exhausted.candidates == ()
    assert exhausted.observation.metadata["objective_satisfied"] is False
    assert exhausted.observation.metadata["insufficient_reobserve_exhausted"] is True


def test_ambiguous_launch_shell_injects_three_bounded_same_session_observes() -> None:
    """已稳定采到 Splash 但生命周期页型冲突时，只允许限次零触碰复观察。"""
    detection = _detection(
        capture_id="capture-ambiguous-splash",
        evidence_id="artifact-ambiguous-splash",
    )
    detection.update(
        {
            "detection_status": "ambiguous",
            "page_type": None,
            "page_state": "loading",
            "candidate_actions": [],
            "risk_flags": [],
            "metrics": {
                "page_settle_complete": True,
                "h5_business_structure_present": False,
            },
            "matched_catalog_signals": {
                "ocr_keywords": ["品牌启动图"],
                "dom_hints": ["splash", "startup"],
                "layout_hints": ["全屏启动宣传图"],
            },
            "page_type_candidates": [
                {"page_type": "line_loading", "confidence": 0.56},
                {"page_type": "startup_ad", "confidence": 0.48},
            ],
        }
    )

    first = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 16, tzinfo=UTC),
        navigation_depth=0,
    )
    third = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 17, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"ambiguous_launch_shell_reobserve_count": 2},
    )
    exhausted = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 18, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"ambiguous_launch_shell_reobserve_count": 3},
    )

    assert first.observation.metadata["ambiguous_launch_shell"] is True
    assert first.candidates[0].action_type is InteractionOperation.OBSERVE
    assert first.candidates[0].semantic_target == "重新观察模糊启动壳层"
    assert first.candidates[0].metadata["ambiguous_launch_shell_reobserve_attempt"] == 1
    assert third.candidates[0].metadata["ambiguous_launch_shell_reobserve_attempt"] == 3
    assert exhausted.candidates == ()
    assert exhausted.observation.metadata["objective_satisfied"] is False
    assert (
        exhausted.observation.metadata[
            "ambiguous_launch_shell_reobserve_exhausted"
        ]
        is True
    )


def test_insufficient_launch_shell_shares_ambiguous_reobserve_budget() -> None:
    """同一精确 Splash 启动壳即使暂被标为 insufficient 也继续零触碰复观察。"""
    detection = _detection(
        capture_id="capture-insufficient-splash",
        evidence_id="artifact-insufficient-splash",
    )
    detection.update(
        {
            "detection_status": "insufficient",
            "page_type": None,
            "page_state": "loading",
            "candidate_actions": [],
            "risk_flags": [],
            "metrics": {
                "page_settle_complete": True,
                "h5_business_structure_present": False,
            },
            "matched_catalog_signals": {
                "ocr_keywords": ["品牌启动图"],
                "dom_hints": ["splash", "startup"],
                "layout_hints": ["全屏启动宣传图"],
            },
            "page_type_candidates": [
                {"page_type": "line_loading", "confidence": 0.56},
                {"page_type": "startup_ad", "confidence": 0.48},
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 19, tzinfo=UTC),
        navigation_depth=0,
    )

    assert frontier.observation.metadata["ambiguous_launch_shell"] is True
    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
    assert frontier.candidates[0].semantic_target == "重新观察模糊启动壳层"
    assert frontier.candidates[0].metadata["ambiguous_launch_shell_reobserve_attempt"] == 1


def test_nprogress_busy_empty_launch_shell_uses_existing_zero_touch_reobserve() -> None:
    """NProgress 空启动壳复用 Splash 的同一 OBSERVE 协议与预算。"""
    detection = _detection(
        capture_id="capture-ambiguous-nprogress",
        evidence_id="artifact-ambiguous-nprogress",
    )
    detection.update(
        {
            "detection_status": "ambiguous",
            "page_type": None,
            "page_state": "loading",
            "candidate_actions": [],
            "risk_flags": ["deterministic_nprogress_busy_empty_launch_shell"],
            "metrics": {
                "page_settle_complete": True,
                "h5_business_structure_present": False,
                "nprogress_busy_empty_launch_shell_structure": True,
            },
            "matched_catalog_signals": {
                "ocr_keywords": [],
                "dom_hints": [],
                "layout_hints": [],
            },
            "page_type_candidates": [],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 20, tzinfo=UTC),
        navigation_depth=0,
        lineage_metadata={"ambiguous_launch_shell_reobserve_count": 2},
    )

    assert frontier.observation.metadata["ambiguous_launch_shell"] is True
    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].action_type is InteractionOperation.OBSERVE
    assert frontier.candidates[0].semantic_target == "重新观察模糊启动壳层"
    assert frontier.candidates[0].metadata["ambiguous_launch_shell_reobserve_attempt"] == 3
    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "ambiguous_launch_shell_reobserve",
        "ambiguous_launch_shell_reobserve_attempt": 3,
    }


@pytest.mark.parametrize(
    ("gate", "value"),
    [
        pytest.param("detection_status", "recognized", id="not-ambiguous"),
        pytest.param("page_type", "home", id="known-page-type"),
        pytest.param("page_state", "stable", id="not-loading"),
        pytest.param("page_settle_complete", False, id="not-settled"),
        pytest.param("h5_business_structure_present", True, id="business-structure"),
        pytest.param(
            "candidate_actions",
            [
                {
                    "action": "click",
                    "target": "首页",
                    "target_keywords": ["首页"],
                    "risk_level": "low",
                    "purpose": "已有业务动作",
                    "navigation_depth": 0,
                }
            ],
            id="action-candidate",
        ),
        pytest.param(
            "nprogress_busy_empty_launch_shell_structure",
            False,
            id="no-structure-proof",
        ),
    ],
)
def test_nprogress_launch_shell_requires_every_safety_gate(
    gate: str,
    value: object,
) -> None:
    """任一状态、稳定性、结构或空候选门禁缺失都必须失败关闭。"""
    module = import_module("services.api.app.application.h5_exploration_observation")
    detection: dict[str, object] = {
        "detection_status": "insufficient",
        "page_type": None,
        "page_state": "loading",
        "candidate_actions": [],
        "metrics": {
            "page_settle_complete": True,
            "h5_business_structure_present": False,
            "nprogress_busy_empty_launch_shell_structure": True,
        },
        "matched_catalog_signals": {
            "ocr_keywords": [],
            "dom_hints": [],
            "layout_hints": [],
        },
        "page_type_candidates": [],
    }
    metrics = detection["metrics"]
    if gate in {
        "page_settle_complete",
        "h5_business_structure_present",
        "nprogress_busy_empty_launch_shell_structure",
    }:
        assert isinstance(metrics, dict)
        metrics[gate] = value
    else:
        detection[gate] = value

    assert module._is_ambiguous_launch_shell_detection(detection) is False


def test_age_verification_gate_preserves_only_signed_adult_confirm_candidate() -> None:
    """年龄门禁候选必须经检测层结构能力验签后才能进入 Frontier。"""
    detection = _detection(
        capture_id="capture-age-verification",
        evidence_id="artifact-age-verification",
    )
    detection.update(
        {
            "detection_status": "ambiguous",
            "page_type": None,
            "page_state": "age_verification_blocking",
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "我未满18",
                    "target_keywords": ["age_verification", "exit"],
                    "risk_level": "low",
                    "purpose": "离开当前页面",
                    "navigation_depth": 0,
                },
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
                },
            ],
            "risk_flags": [
                "age_restricted_content",
                "age_verification_required",
                "underlying_page_obscured",
                "deterministic_h5_age_verification",
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 26, 1, 0, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    signed = frontier.candidates[0]
    assert signed.semantic_target == "年满进入"
    assert signed.metadata["action_parameters"] == {
        "target_mode": "age_verification_confirm",
        "age_verification_capability": "h5_age_verification_confirm.v1",
    }


def test_startup_ad_countdown_preserves_existing_trusted_dismissal() -> None:
    detection = _detection(
        capture_id="capture-countdown-close",
        evidence_id="artifact-countdown-close",
    )
    detection.update(
        {
            "page_type": "startup_ad",
            "page_state": "countdown",
            "candidate_actions": [
                {
                    "action": "click",
                    "target": "跳过",
                    "target_keywords": [
                        "startup_ad",
                        "close_control",
                        "startup_ad_dismissal",
                    ],
                    "risk_level": "low",
                    "purpose": "关闭开屏广告并在同一浏览器会话进入首页",
                    "expected_next_page_type": "home",
                    "navigation_depth": 0,
                    "obstruction_capability": "startup_ad_dismissal",
                    "obstruction_signature": "h5-obstruction:" + "4" * 64,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 8, 11, 9, 11, tzinfo=UTC),
        navigation_depth=0,
    )

    assert len(frontier.candidates) == 1
    assert frontier.candidates[0].action_type is InteractionOperation.CLICK
    assert frontier.candidates[0].semantic_target == "跳过"


def test_payment_collection_candidate_projects_bounded_horizontal_parameters() -> None:
    detection = _detection(capture_id="capture-payment", evidence_id="artifact-payment")
    detection.update(
        {
            "page_url": "https://m.example.test/vip",
            "page_type": "payment",
            "page_state": "stable",
            "candidate_actions": [
                {
                    "action": "scroll",
                    "target": "浏览全部会员卡",
                    "target_keywords": [
                        "payment_horizontal_collection",
                        "read_only_collection",
                        "page_type:payment",
                    ],
                    "risk_level": "low",
                    "purpose": "横向浏览当前支付页全部会员套餐",
                    "expected_next_page_type": None,
                    "navigation_depth": 0,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 14, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "h5_payment_horizontal_collection",
        "horizontal_scroll_max_segments": 6,
        "split_scroll_screenshots": True,
        "source_page_type": "payment",
    }


def test_entitlement_collection_projects_bounded_horizontal_parameters() -> None:
    detection = _detection(capture_id="capture-detail", evidence_id="artifact-detail")
    detection.update(
        {
            "page_url": "https://m.example.test/video/1",
            "page_type": "video_detail",
            "page_state": "entitlement_locked",
            "candidate_actions": [
                {
                    "action": "scroll",
                    "target": "浏览全部会员卡",
                    "target_keywords": [
                        "payment_horizontal_collection",
                        "read_only_collection",
                        "page_type:video_detail",
                    ],
                    "risk_level": "low",
                    "purpose": "横向浏览当前详情页权益层全部会员套餐",
                    "expected_next_page_type": None,
                    "navigation_depth": 0,
                }
            ],
        }
    )

    frontier = _build_frontier(
        detection_result=detection,
        observed_at=datetime(2026, 9, 14, tzinfo=UTC),
        navigation_depth=1,
    )

    assert frontier.candidates[0].metadata["action_parameters"] == {
        "target_mode": "h5_payment_horizontal_collection",
        "horizontal_scroll_max_segments": 6,
        "split_scroll_screenshots": True,
        "source_page_type": "video_detail",
    }
