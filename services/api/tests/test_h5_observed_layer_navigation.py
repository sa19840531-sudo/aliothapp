"""20261011-000641 脱敏结构回放：封面层不等于阻断层，底栏参数不能丢失。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.api.app.application.capture_coordinator import CaptureCoordinationError
from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.application.h5_obstruction_facts import assess_h5_ui_obstruction
from services.api.app.application.h5_runtime_composition import (
    _h5_candidate_parameters,
    _trusted_structural_navigation_candidate_parameters,
)
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.exploration.page_graph import build_page_state_snapshot
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)


def cover_snapshot():
    # 来源：YC003 873dc280 帧；只保留通用封面、命中和几何事实，不携带站点或图片。
    return {
        "schema_version": "ui-snapshot.v2",
        "ready_state": "complete",
        "viewport": {"width": 390, "height": 844},
        "document": {"time_origin_ms": 1000},
        "h5_modal_census_selectors": [],
        "h5_modal_census_truncated": False,
        "elements": [
            {"selector": "main", "tag": "main", "text": "视频列表"},
            {
                "selector": ".card > .cover",
                "tag": "div",
                "text": "",
                "class_tokens": ["aspect-ratio-overlay", "decode-image-overlay"],
                "ancestor_class_tokens": ["decode-image", "card"],
                "h5_modal_ancestor_selectors": [],
                "visible": True,
                "fixed_or_sticky_ancestor": False,
                "effective_z_index": 1,
                "style": {"position": "absolute", "pointer_events": "auto"},
                "rect": {"x": 12, "y": 200, "width": 180, "height": 110},
                "hit_test": {
                    "center_hits_target": True,
                    "pointer_interception": True,
                    "topmost": True,
                    "visual_occlusion_detected": False,
                },
            },
        ],
    }


def surface(snapshot):
    return build_h5_logical_surface(
        ui_snapshot=snapshot,
        page_url="https://fixture.test/list",
        surface_session_id="h5:fixture",
        document_token="document",
    )


@pytest.mark.parametrize("kind", ["cover", "spinner"])
def test_observed_nonblocking_nodes_do_not_turn_base_page_into_overlay(kind):
    snapshot = cover_snapshot()
    if kind == "spinner":
        # 来源：031 46db7f41 帧的局部加载节点，不改变整页阻断语义。
        snapshot["elements"][-1].update(
            {
                "selector": ".loading-icon",
                "class_tokens": ["van-loading"],
                "rect": {"x": 180, "y": 320, "width": 31, "height": 31},
            }
        )
    assert assess_h5_ui_obstruction(snapshot).blocking is False
    logical = surface(snapshot)
    assert logical["state_signals"]["obstruction_count"] == "0"
    graph = build_page_state_snapshot(
        task_id="task",
        agent_run_id="run",
        session_id="h5:fixture",
        capture_id="capture",
        finalization_epoch=0,
        command_sequence=2,
        capture_role="after_action",
        surface_kind="web",
        page_type="video_list",
        page_state="stable",
        metadata={
            "logical_surface": logical,
            "business_page_identity": {"key": "fixture-list", "status": "confirmed"},
        },
    )
    assert graph.is_overlay is False
    assert graph.counts_as_business_page is True


@pytest.mark.parametrize("layers", [1, 3])
def test_real_modal_layers_still_block_when_base_content_is_visible(layers):
    snapshot = cover_snapshot()
    for index in range(layers):
        selector = f"#dialog-{index}"
        snapshot["h5_modal_census_selectors"].append(selector)
        snapshot["elements"].append(
            {
                "selector": selector,
                "tag": "div",
                "role": "dialog",
                "aria_modal": True,
                "class_tokens": ["overlay"],
                "visible": True,
                "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                "style": {"position": "fixed", "pointer_events": "auto"},
                "hit_test": {"center_hits_target": True},
            }
        )
    assert assess_h5_ui_obstruction(snapshot).blocking is True
    assert surface(snapshot)["state_signals"]["obstruction_count"] != "0"
    after = deepcopy(snapshot)
    after["elements"].pop()
    after["h5_modal_census_selectors"].pop()
    assert (surface(after)["state_signals"]["obstruction_count"] != "0") is (layers > 1)


@pytest.mark.parametrize(
    "fault", ["truncated", "missing_ancestors", "modal_ancestor", "occluded", "high_layer"]
)
def test_inline_node_clearance_requires_complete_nonblocking_facts(fault):
    snapshot = cover_snapshot()
    element = snapshot["elements"][-1]
    if fault == "truncated":
        snapshot["h5_modal_census_truncated"] = True
    elif fault == "missing_ancestors":
        element.pop("h5_modal_ancestor_selectors")
    elif fault == "modal_ancestor":
        snapshot["h5_modal_census_selectors"] = ["#dialog"]
        element["h5_modal_ancestor_selectors"] = ["#dialog"]
    elif fault == "occluded":
        element["hit_test"]["visual_occlusion_detected"] = True
    elif fault == "high_layer":
        element["effective_z_index"] = 9999
    assert surface(snapshot)["state_signals"]["obstruction_count"] != "0"


@pytest.mark.parametrize(
    "independent,fault",
    [
        (True, None),
        (False, None),
        (True, "stale"),
        (True, "missing"),
        (True, "captcha"),
        (True, "duplicate"),
    ],
)
def test_observed_independent_bottom_navigation_keeps_structured_parameters(independent, fault):
    parameters = {
        "target_mode": "bottom_navigation",
        "navigation_index": 5,
        "navigation_label": "朋友圈",
        "navigation_family": "adaptive_snapshot",
        "interaction_pattern_signature": "a" * 64,
    }
    candidate = CandidateAction(
        candidate_id="candidate",
        semantic_target="internal:adaptive_bottom_navigation:nth-of-type(5)",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=92,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "action_parameters": parameters,
            "independent_bottom_navigation": independent,
            "target_keywords": ["bottom_navigation", "朋友圈"],
            "navigation_knowledge_source": "static",
            "navigation_knowledge_confidence": 0.9,
        },
    )
    work = SimpleNamespace(
        frontier=SimpleNamespace(
            observation=SimpleNamespace(
                page_type="video_list",
                page_state="popup",
                metadata={
                    "global_bottom_navigation_targets": [
                        {
                            "semantic_target": candidate.semantic_target,
                            "action_parameters": parameters,
                        }
                    ]
                },
            )
        )
    )
    facts = work.frontier.observation.metadata
    if fault == "stale":
        facts["global_bottom_navigation_targets"][0]["action_parameters"] = {}
    elif fault == "missing":
        facts["global_bottom_navigation_targets"] = None
    elif fault == "captcha":
        facts["captcha_verification_required"] = True
    elif fault == "duplicate":
        facts["global_bottom_navigation_targets"] *= 2
    result = _trusted_structural_navigation_candidate_parameters(work=work, candidate=candidate)
    assert result == (parameters if independent and fault is None else {})
    work.work_id = "work"
    work.source_capture_id = "capture"
    work.task_id = "task"
    work.session_id = "h5:fixture"
    work.surface_kind = SurfaceKind.WEB
    work.frontier_snapshot_id = "frontier"
    if independent and fault is None:
        assert (
            _h5_candidate_parameters(work=work, candidate=candidate)["target_mode"]
            == "bottom_navigation"
        )
    else:
        with pytest.raises(CaptureCoordinationError) as error:
            _h5_candidate_parameters(work=work, candidate=candidate)
        assert error.value.interaction.status.value == "blocked"
        assert error.value.interaction.retryable is True
        assert error.value.after_capture is None


@pytest.mark.parametrize(
    "fault", [None, "web", "incomplete", "modal", "logical_blocker", "bool_count"]
)
def test_graph_and_frontier_share_clearance_facts_without_erasing_detected_state(fault):
    logical = surface(cover_snapshot())
    facts = {
        "schema_version": "h5-obstruction-fact.v1",
        "kind": "none",
        "blocking": False,
        "snapshot_evidence_status": "ready",
        "modal_census_complete": True,
        "modal_census_count": 0,
    }
    if fault == "incomplete":
        facts["modal_census_complete"] = False
    elif fault == "modal":
        facts["modal_census_count"] = 1
    elif fault == "bool_count":
        facts["modal_census_count"] = False
    elif fault == "logical_blocker":
        logical["state_signals"]["obstruction_count"] = "1"
    detection = {"page_state": "popup", "logical_surface": logical, "obstruction_facts": facts}
    graph = build_page_state_snapshot(
        task_id="task",
        agent_run_id="run",
        session_id="web:fixture" if fault == "web" else "h5:fixture",
        capture_id="capture",
        finalization_epoch=0,
        command_sequence=2,
        capture_role="after_action",
        surface_kind="web",
        page_type="video_list",
        page_state="popup",
        metadata={
            "page_detection": detection,
            "business_page_identity": {"key": "fixture-list", "status": "confirmed"},
        },
    )
    assert graph.is_overlay is (fault is not None)
    assert detection["page_state"] == "popup"
