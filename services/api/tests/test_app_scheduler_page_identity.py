"""业务身份、局部状态与 App 调度循环的接线回归。"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from services.api.app.application.app_screenshot_exploration import (
    _app_page_fingerprint,
    _app_return_fingerprint,
    _app_scheduler_state_fingerprint,
    _next_coverage_metadata,
)
from services.api.app.application.bounded_exploration_scheduler import (
    BOUNDED_EXPLORATION_STATE_KEY,
)
from services.api.app.domain.exploration import AppVisualAnalysis, CandidateAction
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.app_vision.llm import build_analysis_from_llm_payload
from services.api.tests.test_app_screenshot_exploration import NOW, _coverage_goal
from services.api.tests.test_app_visual_page_identity import _anchor, _observation
from services.api.tests.test_llm_app_vision import _request


def _frame(index=1, *, page_type="content_list", local_state=None, observation=None, facts=None):
    analysis = build_analysis_from_llm_payload(
        request=replace(
            _request(),
            capture_id=f"00000000-0000-0000-0000-{index:012d}",
            screenshot_artifact_id=f"artifact-{index}",
            image_signature="sha256:" + f"{index:064x}",
        ),
        payload={
            "visual_page_type": page_type,
            "visual_page_type_confidence": 0.95,
            "page_state": "stable",
            "visual_summary": "当前作品目录",
            "page_identity_observation": observation or _observation(),
            "local_state_observation": local_state,
            "surface_facts": facts or {},
        },
        page_types=get_default_page_type_registry(),
        model_id="scheduler-identity-test",
    )
    if facts:
        # 结构化滚动读数模拟运行时观察，不能让纯视觉模型自报设备像素偏移。
        payload = analysis.to_dict()
        payload.pop("contract_version")
        payload.pop("result_hash")
        payload["surface_facts"].update(facts)
        return AppVisualAnalysis.build(**payload)
    return analysis


def _action(operation=InteractionOperation.CLICK):
    return CandidateAction(
        candidate_id="next-action",
        semantic_target="下一步",
        action_type=operation,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
    )


def test_page_fingerprint_ignores_type_pixels_and_local_state():
    first = _frame(local_state={"page_index": _anchor("第1页")})
    second = _frame(2, page_type="home", local_state={"page_index": _anchor("第2页")})
    assert first.image_signature != second.image_signature
    assert _app_page_fingerprint(first, {}) == _app_page_fingerprint(
        second, {"business_page_scope": "stale-page", "scroll_offset": 900}
    )
    assert _app_scheduler_state_fingerprint(
        first, candidate=_action(), fallback=first.image_signature
    ) != _app_scheduler_state_fingerprint(
        second, candidate=_action(), fallback=second.image_signature
    )


def test_page_fingerprint_changes_with_business_object_and_return_semantics():
    first = _frame(observation=_observation(object_id="作品编号 123"))
    second = _frame(observation=_observation(object_id="作品编号 124"))
    observation = _observation(object_id="作品编号 124")
    observation["return_semantics"] = "close"
    third = _frame(observation=observation)
    assert len({_app_page_fingerprint(item, {}) for item in (first, second, third)}) == 3


@pytest.mark.parametrize("with_goal", [False, True])
def test_repeated_clicks_keep_loop_count_across_new_frames_and_type_reassessment(with_goal):
    metadata = {}
    goal = _coverage_goal(required_page_types=("content_list",)) if with_goal else None
    for index, page_type in enumerate(("content_list", "home", "content_list"), start=1):
        analysis = _frame(index, page_type=page_type)
        metadata = _next_coverage_metadata(
            goal=goal,
            analysis=analysis,
            prior_metadata=json.loads(json.dumps(metadata)),
            completed_action=_action(),
            action_succeeded=True,
            scheduler_max_actions=20,
            scheduler_state_fingerprint=analysis.image_signature,
            scheduler_now=NOW,
        )
    state = metadata[BOUNDED_EXPLORATION_STATE_KEY]
    assert state["page_repeat_count"] == 2
    assert sum(state["consumed_actions"].values()) == 3
    assert state["last_state_fingerprint"] != analysis.image_signature


@pytest.mark.parametrize(
    "key", ["selected_tab", "selected_filter", "sort_key", "expanded_item_id", "page_index"]
)
def test_local_state_change_resets_page_repeat_without_creating_new_page(key):
    metadata = {}
    frames = (
        _frame(1, local_state={key: _anchor("选项一")}),
        _frame(2, local_state={key: _anchor("选项一")}),
        _frame(3, local_state={key: _anchor("选项二")}),
    )
    counts, pages = [], []
    for analysis in frames:
        metadata = _next_coverage_metadata(
            goal=None,
            analysis=analysis,
            prior_metadata=metadata,
            completed_action=_action(),
            action_succeeded=True,
            scheduler_max_actions=20,
            scheduler_state_fingerprint=analysis.image_signature,
            scheduler_now=NOW,
        )
        state = metadata[BOUNDED_EXPLORATION_STATE_KEY]
        counts.append(state["page_repeat_count"])
        pages.append(state["last_page_fingerprint"])
    assert counts == [0, 1, 0]
    assert len(set(pages)) == 1
    assert sum(state["consumed_actions"].values()) == 3


@pytest.mark.parametrize("with_scroll_facts", [False, True])
def test_scroll_uses_current_range_or_visual_fallback_without_splitting_page(with_scroll_facts):
    facts = {"scroll_offset": 120} if with_scroll_facts else {}
    first, second = _frame(1, facts=facts), _frame(2, facts=facts)
    fingerprints = [
        _app_scheduler_state_fingerprint(
            item, candidate=_action(InteractionOperation.SCROLL), fallback=item.image_signature
        )
        for item in (first, second)
    ]
    assert (fingerprints[0] == fingerprints[1]) is with_scroll_facts
    assert _app_page_fingerprint(first, {}) == _app_page_fingerprint(second, {})
    if with_scroll_facts:
        moved = _frame(3, facts={"scroll_offset": 240})
        assert fingerprints[1] != _app_scheduler_state_fingerprint(
            moved, candidate=_action(InteractionOperation.SCROLL), fallback=moved.image_signature
        )


def test_return_loop_uses_persisted_source_identity_not_revised_legacy_scope():
    source = {BOUNDED_EXPLORATION_STATE_KEY: {"last_page_fingerprint": "verified-source"}}
    first = _app_return_fingerprint(
        prior_metadata={**source, "business_page_scope": "old-type"},
        candidate=_action(InteractionOperation.BACK),
        analysis=_frame(1),
    )
    second = _app_return_fingerprint(
        prior_metadata={**source, "business_page_scope": "new-type"},
        candidate=_action(InteractionOperation.BACK),
        analysis=_frame(2, page_type="home"),
    )
    different_source = _app_return_fingerprint(
        prior_metadata={BOUNDED_EXPLORATION_STATE_KEY: {"last_page_fingerprint": "other-page"}},
        candidate=_action(InteractionOperation.BACK),
        analysis=_frame(2),
    )
    assert first == second
    assert first != different_source
    assert (
        _app_return_fingerprint(prior_metadata=source, candidate=_action(), analysis=_frame())
        is None
    )
