"""H5 任务 Frontier 的版本化领域合同测试。"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.exploration import (
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    SideEffectPolicy,
    build_exploration_work,
)
from services.api.app.domain.exploration.h5_frontier import (
    H5_POPUP_SIGNATURE_ATTEMPT_LIMIT,
    H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT,
    H5_TASK_FRONTIER_TARGET_BYTES,
    H5FrontierEntryStatus,
    H5FrontierValidationError,
    H5TaskFrontierSnapshot,
    _new_snapshot,
    canonical_h5_route_identity,
    compact_h5_task_frontier,
    merge_h5_candidates,
    retire_h5_skipped_entries,
    select_next_h5_entry,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.protocol import validate_protocol_payload


def _candidate(
    candidate_id: str,
    *,
    page_type: str | None = None,
    depth: int = 1,
    risk: InteractionRiskLevel = InteractionRiskLevel.LOW,
    target_keywords: tuple[str, ...] = (),
) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=candidate_id,
        action_type=InteractionOperation.CLICK,
        risk_level=risk,
        exploration_value=10,
        navigation_depth=depth,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "expected_next_page_type": page_type or candidate_id,
            "target_keywords": list(target_keywords),
        },
    )


@pytest.mark.parametrize("page_type", ["settings", "payment", "video_detail"])
@pytest.mark.parametrize(
    "proof", ["verified_business_link", "profile_local_action", "list_detail_navigation"]
)
def test_current_new_page_type_precedes_return_without_displacing_required_evidence(
    page_type, proof,
):
    snapshot = H5TaskFrontierSnapshot.create(max_pages=20).merge(
        source_capture_id="current", source_state_version="current",
        source_location="https://example.test/", navigation_depth=0,
        candidates=(
            replace(_candidate("return", page_type="home", target_keywords=("nested_return",)),
                    action_type=InteractionOperation.BACK, semantic_target="browser_history"),
            replace(_candidate("new", page_type=page_type, target_keywords=(proof,)),
                    metadata={"expected_next_page_type": page_type, "target_keywords": [proof],
                              "action_parameters": {"target_mode": "list_detail_navigation"}}),
        ),
    )
    assert select_next_h5_entry(snapshot).candidate.candidate_id == "new"


def test_unvisited_bottom_module_precedes_more_samples_of_covered_page_type():
    snapshot = H5TaskFrontierSnapshot.create(max_pages=20).merge(
        source_capture_id="first", source_state_version="first",
        source_location="https://example.test/", navigation_depth=0,
        candidates=(_candidate("first-list", page_type="content_list"),),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_executed(entry.entry_id)
    local_tab = _page_tab_candidate(
        "local-tab", index=3, label="分类", source_page_type="content_list",
        expected_page_type="content_list",
    )
    bottom = _bottom_navigation_candidate(
        "new-module", index=4, label="另一个栏目", source_page_type="content_list",
        expected_page_type="content_list",
    )
    snapshot = snapshot.merge(
        source_capture_id="second", source_state_version="second",
        source_location="https://example.test/", navigation_depth=0,
        candidates=(local_tab, bottom),
    )
    selected = select_next_h5_entry(snapshot)
    assert selected.candidate.candidate_id == "new-module"


def test_local_tab_scope_uses_parent_route_instead_of_dynamic_structure_cardinality():
    from services.api.app.application.h5_exploration_observation import _h5_source_branch_context

    first = {"identity_signals": {"route": "same-route", "active_navigation": []},
             "identity_fingerprint": "before-list-growth"}
    second = {**first, "identity_fingerprint": "after-list-growth"}
    assert _h5_source_branch_context(first) == _h5_source_branch_context(second)
    other = {**first, "identity_signals": {"route": "other-route", "active_navigation": []}}
    assert _h5_source_branch_context(first) != _h5_source_branch_context(other)


def test_stale_navigation_is_refreshed_without_reviving_executed_branch() -> None:
    candidate = _bottom_navigation_candidate(
        "ai-old", index=3, label="AI", source_page_type="home", expected_page_type="content_list"
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=20).merge(
        source_capture_id="capture-old", source_state_version="state-old",
        source_location="https://example.test/", navigation_depth=0, candidates=(candidate,),
    )
    entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(entry.entry_id).mark_skipped(
        entry.entry_id, reason="action_failure_reobserved:LOGICAL_SOURCE_STATE_STALE"
    )
    snapshot = retire_h5_skipped_entries(snapshot, max_retained_skipped=0)
    snapshot = snapshot.merge(
        source_capture_id="capture-new", source_state_version="state-new",
        source_location="https://example.test/", navigation_depth=0,
        candidates=(replace(candidate, candidate_id="ai-new"),),
    )
    assert len(snapshot.pending_entries) == 1
    fresh = snapshot.pending_entries[0]
    assert fresh.source_capture_id == "capture-new"
    assert fresh.candidate.metadata["source_refresh_attempt_count"] == 1
    snapshot = snapshot.select_entry(fresh.entry_id).mark_executed(fresh.entry_id)
    replay = snapshot.merge(
        source_capture_id="capture-later", source_state_version="state-later",
        source_location="https://example.test/", navigation_depth=0, candidates=(candidate,),
    )
    assert not replay.pending_entries


def test_stale_source_refresh_has_a_persisted_retry_limit() -> None:
    candidate = _candidate("module", page_type="content_list")
    snapshot = H5TaskFrontierSnapshot.create(max_pages=20)
    for index in range(H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT + 1):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{index}", source_state_version=f"state-{index}",
            source_location="https://example.test/", navigation_depth=0, candidates=(candidate,),
        )
        entry = snapshot.pending_entries[0]
        snapshot = snapshot.select_entry(entry.entry_id).mark_skipped(
            entry.entry_id, reason="action_failure_reobserved:LOGICAL_SOURCE_STATE_STALE"
        )
        snapshot = H5TaskFrontierSnapshot.from_dict(snapshot.to_dict())
    snapshot = snapshot.merge(
        source_capture_id="capture-exhausted", source_state_version="state-exhausted",
        source_location="https://example.test/", navigation_depth=0, candidates=(candidate,),
    )
    assert not snapshot.pending_entries


def test_same_named_tabs_are_scoped_to_parent_module_not_capture_or_selected_tab() -> None:
    tab = _page_tab_candidate(
        "recommend-a", index=2, label="推荐", source_page_type="content_list",
        expected_page_type="content_list",
    )
    first = replace(tab, metadata={**tab.metadata, "source_branch_context": "module-a"})
    second = replace(tab, candidate_id="recommend-b", metadata={
        **tab.metadata, "source_branch_context": "module-b",
    })
    snapshot = H5TaskFrontierSnapshot.create(max_pages=20).merge(
        source_capture_id="capture-a", source_state_version="state-a",
        source_location="https://example.test/", navigation_depth=0, candidates=(first,),
    ).merge(
        source_capture_id="capture-b", source_state_version="state-b",
        source_location="https://example.test/", navigation_depth=0, candidates=(second,),
    )
    assert len(snapshot.pending_entries) == 2
    refreshed = snapshot.merge(
        source_capture_id="capture-b2", source_state_version="state-b2",
        source_location="https://example.test/", navigation_depth=0,
        candidates=(replace(second, candidate_id="recommend-b2"),),
    )
    assert len(refreshed.pending_entries) == 2


@pytest.mark.parametrize("keyword,operation", [
    ("detail_journey", InteractionOperation.CLICK),
    ("video_timed_capture", InteractionOperation.TIMED_CAPTURE),
    ("player_seek_probe", InteractionOperation.SEEK_MEDIA),
])
def test_detail_evidence_is_selected_before_return(keyword, operation) -> None:
    back = replace(
        _candidate("browser_history"), action_type=InteractionOperation.BACK,
        metadata={"source_page_type": "video_detail", "target_keywords": ["nested_return"]},
    )
    evidence = replace(_candidate("detail-evidence"), action_type=operation,
                       metadata={"source_page_type": "video_detail", "target_keywords": [keyword]})
    snapshot = H5TaskFrontierSnapshot.create(max_pages=20).merge(
        source_capture_id="capture-detail", source_state_version="state-detail",
        source_location="https://example.test/video/42", navigation_depth=1,
        candidates=(back, evidence),
    )
    selected = select_next_h5_entry(snapshot, active_state_version="state-detail")
    assert selected is not None
    assert selected.candidate.candidate_id == "detail-evidence"


def test_detail_scroll_coverage_is_selected_before_nested_return() -> None:
    scroll = CandidateAction(
        candidate_id="detail-scroll",
        semantic_target="采集详情页第二屏内容",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_detail",
            "source_page_state": "stable",
            "target_keywords": ["detail_scroll_coverage", "two_screen_sequence"],
            "action_parameters": {
                "target_mode": "h5_detail_scroll_coverage",
                "scroll_max_segments": 2,
                "split_scroll_screenshots": True,
                "source_page_type": "video_detail",
            },
        },
    )
    back = CandidateAction(
        candidate_id="detail-back",
        semantic_target="browser_history",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_detail",
            "source_page_state": "stable",
            "target_keywords": ["nested_return"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=10).merge(
        source_capture_id="capture-video-detail",
        source_state_version="state-video-detail",
        source_location="https://m.example.test/video/42",
        navigation_depth=1,
        candidates=(back, scroll),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-video-detail")

    assert selected is not None
    assert selected.candidate.candidate_id == "detail-scroll"


def test_list_scroll_coverage_is_selected_before_same_page_tabs() -> None:
    tab = CandidateAction(
        candidate_id="home-tab",
        semantic_target="internal:page_tab:nth-of-type(2)",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={"source_page_type": "home", "target_keywords": ["page_tab_navigation"]},
    )
    scroll = CandidateAction(
        candidate_id="home-list-scroll",
        semantic_target="采集列表至底部",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=93,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "target_keywords": ["list_scroll_coverage", "bounded_to_bottom"],
            "action_parameters": {
                "target_mode": "h5_list_scroll_coverage",
                "scroll_max_segments": 8,
                "split_scroll_screenshots": True,
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(tab, scroll),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-home")

    assert selected is not None
    assert selected.candidate.candidate_id == "home-list-scroll"


def test_uncovered_navigation_branch_stays_ahead_of_list_scroll() -> None:
    branch = CandidateAction(
        candidate_id="short-video-branch",
        semantic_target="internal:bottom_navigation:nth-of-type(2)",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "expected_next_page_type": "video_list",
            "target_keywords": ["bottom_navigation"],
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_label": "短剧",
                "navigation_index": 2,
            },
        },
    )
    scroll = CandidateAction(
        candidate_id="home-list-scroll",
        semantic_target="采集列表至底部",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=93,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "target_keywords": ["list_scroll_coverage"],
            "action_parameters": {
                "target_mode": "h5_list_scroll_coverage",
                "scroll_max_segments": 8,
                "split_scroll_screenshots": True,
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://m.example.test/home",
        navigation_depth=0,
        candidates=(scroll, branch),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-home")

    assert selected is not None
    assert selected.candidate.candidate_id == "short-video-branch"


def test_detail_scroll_coverage_is_not_blocked_by_exhausted_page_budget() -> None:
    covered = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://m.example.test/",
        navigation_depth=0,
        candidates=(_candidate("video-detail", page_type="video_detail"),),
    )
    entry_id = covered.pending_entries[0].entry_id
    covered = covered.select_entry(entry_id).mark_executed(entry_id)
    scroll = CandidateAction(
        candidate_id="detail-scroll",
        semantic_target="采集详情页第二屏内容",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "video_detail",
            "source_page_state": "stable",
            "target_keywords": ["detail_scroll_coverage", "two_screen_sequence"],
            "action_parameters": {
                "target_mode": "h5_detail_scroll_coverage",
                "scroll_max_segments": 2,
                "split_scroll_screenshots": True,
                "source_page_type": "video_detail",
            },
        },
    )

    merged = covered.merge(
        source_capture_id="capture-video-detail",
        source_state_version="state-video-detail",
        source_location="https://m.example.test/video/42",
        navigation_depth=1,
        candidates=(scroll,),
    )

    assert merged.pending_count == 1
    assert merged.blocked_count == 0


def _close_candidate(
    candidate_id: str,
    *,
    signature: str | None = None,
    capability: str = "dialog_content_close",
) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target="关闭当前弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": [
                "popup_dismissal",
                "close_control",
                capability,
            ],
            "expected_next_page_type": None,
            "obstruction_signature": signature
            or "h5-obstruction:" + candidate_id.encode("utf-8").hex().ljust(64, "0")[:64],
            "action_parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": capability,
                "obstruction_signature": signature
                or "h5-obstruction:" + candidate_id.encode("utf-8").hex().ljust(64, "0")[:64],
            },
        },
    )


def _line_observe_candidate(candidate_id: str, *, attempt: int) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target="重新观察线路加载结果",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["line_loading", "same_session_reobserve"],
            "line_loading_reobserve_attempt": attempt,
        },
    )


def _nested_return_candidate(candidate_id: str, *, source_page_type: str) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target="browser_history",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": source_page_type,
            "target_keywords": ["nested_return"],
            "expected_next_page_type": None,
        },
    )


def _bottom_navigation_candidate(
    candidate_id: str,
    *,
    index: int,
    label: str,
    source_page_type: str,
    expected_page_type: str,
) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=f"internal:flutter_bottom_navigation:nth-of-type({index})",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": source_page_type,
            "source_page_state": "stable",
            "target_keywords": ["bottom_navigation", label, expected_page_type],
            "expected_next_page_type": expected_page_type,
            "action_parameters": {
                "target_mode": "bottom_navigation",
                "navigation_family": "flutter_semantics",
                "navigation_index": index,
                "navigation_label": label,
            },
        },
    )


def _page_tab_candidate(
    candidate_id: str,
    *,
    index: int,
    label: str,
    source_page_type: str,
    expected_page_type: str,
    pattern_signature: str | None = None,
) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=f"internal:page_tab:nth-of-type({index})",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=97,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": source_page_type,
            "source_page_state": "stable",
            "target_keywords": [
                "page_tab_navigation",
                label,
                expected_page_type,
                "same_surface_variant",
            ],
            "expected_next_page_type": expected_page_type,
            "action_parameters": {
                "target_mode": "page_tab_navigation",
                "navigation_index": index,
                "navigation_label": label,
                **(
                    {"interaction_pattern_signature": pattern_signature}
                    if pattern_signature is not None
                    else {}
                ),
            },
        },
    )


def _merge_args(state_version: str, candidate_id: str) -> dict[str, object]:
    return {
        "source_capture_id": f"capture-{state_version}",
        "source_state_version": state_version,
        "source_location": "https://example.test/home",
        "navigation_depth": 0,
        "candidates": (_candidate(candidate_id),),
    }


def test_h5_task_frontier_round_trip_keeps_siblings_and_hash() -> None:
    """若序列化遗漏 sibling 或未校验哈希，此测试会失败。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=3).merge(
        source_capture_id="capture-1",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_candidate("product"), _candidate("cart")),
    )

    restored = H5TaskFrontierSnapshot.from_dict(snapshot.to_dict())

    assert [item.candidate.candidate_id for item in restored.pending_entries] == [
        "product",
        "cart",
    ]
    assert restored.payload_hash == restored.recompute_payload_hash()


def test_h5_task_frontier_rejects_tampered_payload_and_unknown_schema() -> None:
    """若恢复时接受篡改载荷或未知 schema，此测试会失败。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=3).merge(**_merge_args("state-1", "product"))
    tampered = deepcopy(snapshot.to_dict())
    tampered["entries"][0]["candidate"]["semantic_target"] = "payment"
    unknown_schema = deepcopy(snapshot.to_dict())
    unknown_schema["schema_version"] = "h5-task-frontier.v2"

    with pytest.raises(H5FrontierValidationError, match="payload_hash"):
        H5TaskFrontierSnapshot.from_dict(tampered)
    with pytest.raises(H5FrontierValidationError, match="schema_version"):
        H5TaskFrontierSnapshot.from_dict(unknown_schema)


def test_h5_task_frontier_deduplicates_semantic_action_across_states() -> None:
    """若 fingerprint 将来源状态纳入去重身份，同语义 sibling 会重复进入 Frontier。"""
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(**_merge_args("state-1", "product"))
    second = first.merge(**_merge_args("state-2", "product"))

    assert len(second.entries) == 1
    assert second.entries[0].source_state_version == "state-1"


def test_h5_task_frontier_refreshes_pending_profile_local_source_evidence() -> None:
    """个人中心精确局部入口重现时应刷新来源，保证子页后可优先历史返回。"""

    def settings_candidate(candidate_id: str) -> CandidateAction:
        return CandidateAction(
            candidate_id=candidate_id,
            semantic_target="个人中心右上设置图标",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=98,
            navigation_depth=1,
            expected_surface_kind=SurfaceKind.WEB,
            metadata={
                "source_page_type": "profile",
                "source_page_state": "stable",
                "target_keywords": [
                    "settings_entry",
                    "profile_local_action",
                    "page_type:settings",
                    "evidence:structure",
                ],
                "expected_next_page_type": "settings",
                "action_parameters": {
                    "target_mode": "profile_settings_navigation",
                    "settings_entry_capability": "top_right_icon_setting",
                    "source_page_type": "profile",
                    "expected_page_type": "settings",
                },
            },
        )

    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile-old",
        source_state_version="state-profile-old",
        source_location="https://example.test/mine",
        navigation_depth=1,
        candidates=(settings_candidate("settings-old"),),
    )
    revisited = first.merge(
        source_capture_id="capture-profile-current",
        source_state_version="state-profile-current",
        source_location="https://example.test/mine",
        navigation_depth=1,
        parent_state_version="state-payment",
        candidates=(settings_candidate("settings-current"),),
    )

    assert len(revisited.entries) == 1
    entry = revisited.entries[0]
    assert entry.candidate.candidate_id == "settings-current"
    assert entry.source_capture_id == "capture-profile-current"
    assert entry.source_state_version == "state-profile-current"


def test_h5_task_frontier_refreshes_pending_page_tab_source_evidence() -> None:
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-old",
        source_state_version="state-home-old",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "tab-old",
                index=9,
                label="最新上架",
                source_page_type="home",
                expected_page_type="home",
            ),
        ),
    )
    revisited = first.merge(
        source_capture_id="capture-home-current",
        source_state_version="state-home-current",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "tab-current",
                index=9,
                label="最新上架",
                source_page_type="home",
                expected_page_type="home",
            ),
        ),
    )

    assert len(revisited.entries) == 1
    entry = revisited.entries[0]
    assert entry.candidate.candidate_id == "tab-current"
    assert entry.source_capture_id == "capture-home-current"
    assert entry.source_state_version == "state-home-current"


def test_h5_task_frontier_deduplicates_bottom_navigation_by_label_across_tab_states() -> None:
    """Flutter 激活项空文本导致索引漂移时，同一底栏标签只能进入一次 Frontier。"""
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(
            _bottom_navigation_candidate(
                "home-haijiao",
                index=3,
                label="海角",
                source_page_type="home",
                expected_page_type="post_list",
            ),
            _bottom_navigation_candidate(
                "home-chigua",
                index=5,
                label="吃瓜",
                source_page_type="home",
                expected_page_type="post_list",
            ),
        ),
    )

    revisited = first.merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.test/",
        navigation_depth=1,
        candidates=(
            _bottom_navigation_candidate(
                "profile-haijiao",
                index=4,
                label="海角",
                source_page_type="profile",
                expected_page_type="post_list",
            ),
            _bottom_navigation_candidate(
                "profile-chigua",
                index=6,
                label="吃瓜",
                source_page_type="profile",
                expected_page_type="post_list",
            ),
        ),
    )

    assert [entry.candidate.candidate_id for entry in revisited.entries] == [
        "profile-haijiao",
        "profile-chigua",
    ]
    assert {entry.source_state_version for entry in revisited.entries} == {"state-profile"}


def test_h5_task_frontier_deduplicates_bottom_navigation_across_routes() -> None:
    """底栏是全局入口，来源 URL 不得让同一标签在每个路由重复入队。"""
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(
            _bottom_navigation_candidate(
                "home-video",
                index=2,
                label="视频",
                source_page_type="home",
                expected_page_type="video_list",
            ),
        ),
    )

    revisited = first.merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.test/mine",
        navigation_depth=1,
        candidates=(
            _bottom_navigation_candidate(
                "profile-video",
                index=2,
                label="视频",
                source_page_type="profile",
                expected_page_type="video_list",
            ),
        ),
    )

    assert len(revisited.entries) == 1
    assert revisited.entries[0].candidate.candidate_id == "profile-video"
    assert revisited.entries[0].source_location == "https://example.test/mine"


def test_h5_task_frontier_keeps_same_action_on_distinct_routes() -> None:
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        **_merge_args("state-1", "product")
    )
    second = first.merge(
        source_capture_id="capture-state-2",
        source_state_version="state-2",
        source_location="https://example.test/profile",
        navigation_depth=0,
        candidates=(_candidate("product"),),
    )

    assert len(second.entries) == 2


def test_h5_task_frontier_normalizes_equivalent_route_identity() -> None:
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-1",
        source_state_version="state-1",
        source_location="HTTPS://EXAMPLE.TEST:443/home/?b=2&a=1#section",
        navigation_depth=0,
        candidates=(_candidate("product"),),
    )
    second = first.merge(
        source_capture_id="capture-2",
        source_state_version="state-2",
        source_location="https://example.test/home?a=1&b=2",
        navigation_depth=0,
        candidates=(_candidate("product"),),
    )

    assert len(second.entries) == 1
    assert canonical_h5_route_identity("https://example.test/#/home") != (
        canonical_h5_route_identity("https://example.test/#/profile")
    )


def test_h5_task_frontier_keeps_nested_return_for_each_child_page_type() -> None:
    """视频页的 back 不得吞掉帖子详情、活动、支付或设置页自己的返回动作。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-video-detail",
        source_state_version="state-video-detail",
        source_location="https://example.test/moviePlayer",
        navigation_depth=1,
        candidates=(_nested_return_candidate("back-video", source_page_type="video_detail"),),
    )
    snapshot = snapshot.merge(
        source_capture_id="capture-content-detail",
        source_state_version="state-content-detail",
        source_location="https://example.test/squareDetail",
        navigation_depth=1,
        candidates=(_nested_return_candidate("back-content", source_page_type="content_detail"),),
    )

    assert [entry.candidate.candidate_id for entry in snapshot.pending_entries] == [
        "back-video",
        "back-content",
    ]
    selected = select_next_h5_entry(snapshot, active_state_version="state-content-detail")
    assert selected is not None
    assert selected.candidate.candidate_id == "back-content"


def test_h5_task_frontier_keeps_close_control_for_distinct_overlay_states() -> None:
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup-1",
        source_state_version="popup-state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-popup-1"),),
    )
    second = first.merge(
        source_capture_id="capture-popup-2",
        source_state_version="popup-state-2",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-popup-2"),),
    )

    assert len(second.entries) == 2
    assert [entry.source_state_version for entry in second.entries] == [
        "popup-state-1",
        "popup-state-2",
    ]


def test_h5_task_frontier_deduplicates_same_obstruction_signature_across_captures() -> None:
    """同路由、同页型、同签名弹层不能因 Capture 身份变化重复入队。"""
    signature = "h5-obstruction:" + "a" * 64
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup-1",
        source_state_version="popup-state",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-popup-1", signature=signature),),
    )
    second = first.merge(
        source_capture_id="capture-popup-2",
        source_state_version="popup-state",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-popup-2", signature=signature),),
    )

    assert len(second.entries) == 1
    assert [entry.source_capture_id for entry in second.entries] == ["capture-popup-1"]


def test_h5_task_frontier_deduplicates_popup_when_page_state_label_drifts() -> None:
    """识别状态名称漂移不能让同一真实弹层重新获得语义身份。"""
    signature = "h5-obstruction:" + "a" * 64
    first_candidate = replace(
        _close_candidate("close-popup-1", signature=signature),
        metadata={
            **_close_candidate("close-popup-1", signature=signature).metadata,
            "source_page_type": "home",
            "source_page_state": "popup",
        },
    )
    second_candidate = replace(
        _close_candidate("close-popup-2", signature=signature),
        metadata={
            **_close_candidate("close-popup-2", signature=signature).metadata,
            "source_page_type": "home",
            "source_page_state": "locked",
        },
    )
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup-1",
        source_state_version="popup-state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(first_candidate,),
    )
    second = first.merge(
        source_capture_id="capture-popup-2",
        source_state_version="popup-state-2",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(second_candidate,),
    )

    assert len(second.entries) == 1


def test_reobserved_same_popup_candidate_and_state_never_collide_entry_ids() -> None:
    """YC003 重观察回到相同状态，终态关闭候选可有界重试但不能产生重复 ID。"""
    candidate = _close_candidate("same-candidate", signature="h5-obstruction:" + "a" * 64)
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for sequence in range(H5_POPUP_SIGNATURE_ATTEMPT_LIMIT):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{sequence}", source_state_version="same-state",
            source_location="https://fixture.test/home", navigation_depth=0,
            candidates=(candidate,),
        )
        assert len(snapshot.pending_entries) == 1
        entry = snapshot.pending_entries[0]
        snapshot = snapshot.select_entry(entry.entry_id).mark_blocked(
            entry.entry_id, reason="popup_close_blocked",
        )
        assert len({item.entry_id for item in snapshot.entries}) == len(snapshot.entries)
    bounded = snapshot.merge(
        source_capture_id="capture-exhausted", source_state_version="same-state",
        source_location="https://fixture.test/home", navigation_depth=0, candidates=(candidate,),
    )
    assert not bounded.pending_entries
    assert len(bounded.entries) == H5_POPUP_SIGNATURE_ATTEMPT_LIMIT


def test_h5_task_frontier_bounds_terminal_popup_regrowth_and_same_capture_replay() -> None:
    """同签名终态最多保留 3 次，重放同一 Capture 始终幂等。"""
    signature = "h5-obstruction:" + "a" * 64
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for sequence in range(1, H5_POPUP_SIGNATURE_ATTEMPT_LIMIT + 1):
        candidate = replace(
            _close_candidate(f"close-popup-{sequence}", signature=signature),
            metadata={
                **_close_candidate(f"close-popup-{sequence}", signature=signature).metadata,
                "source_page_type": "home",
            },
        )
        snapshot = snapshot.merge(
            source_capture_id=f"capture-popup-{sequence}",
            source_state_version=f"popup-state-{sequence}",
            source_location="https://example.test/home",
            navigation_depth=0,
            candidates=(candidate,),
        )
        entry = snapshot.entries[-1]
        snapshot = snapshot.select_entry(entry.entry_id).mark_blocked(
            entry.entry_id,
            reason="popup_close_blocked",
        )

    fourth = replace(
        _close_candidate("close-popup-4", signature=signature),
        metadata={
            **_close_candidate("close-popup-4", signature=signature).metadata,
            "source_page_type": "home",
        },
    )
    bounded = snapshot.merge(
        source_capture_id="capture-popup-4",
        source_state_version="popup-state-4",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(fourth,),
    )
    replay = bounded.merge(
        source_capture_id="capture-popup-3",
        source_state_version="popup-state-replay",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(fourth,),
    )

    assert len(bounded.entries) == H5_POPUP_SIGNATURE_ATTEMPT_LIMIT
    assert replay.entries == bounded.entries


def test_h5_task_frontier_keeps_same_signature_for_distinct_source_pages() -> None:
    """相同签名在不同来源路由或页型上仍代表不同真实弹层。"""
    signature = "h5-obstruction:" + "a" * 64
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup-home",
        source_state_version="popup-state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            replace(
                _close_candidate("close-popup-home", signature=signature),
                metadata={
                    **_close_candidate("close-popup-home", signature=signature).metadata,
                    "source_page_type": "home",
                },
            ),
        ),
    )
    second = first.merge(
        source_capture_id="capture-popup-profile",
        source_state_version="popup-state-profile",
        source_location="https://example.test/profile",
        navigation_depth=1,
        candidates=(
            replace(
                _close_candidate("close-popup-profile", signature=signature),
                metadata={
                    **_close_candidate("close-popup-profile", signature=signature).metadata,
                    "source_page_type": "profile",
                },
            ),
        ),
    )

    assert len(second.entries) == 2


def test_h5_task_frontier_keeps_distinct_popup_capabilities() -> None:
    """同签名下关闭能力不同的真实弹层仍不能互相吞并。"""
    signature = "h5-obstruction:" + "a" * 64
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup-close",
        source_state_version="popup-state-close",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _close_candidate(
                "close-popup-close",
                signature=signature,
                capability="dialog_content_close",
            ),
        ),
    )
    second = first.merge(
        source_capture_id="capture-popup-overlay",
        source_state_version="popup-state-overlay",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _close_candidate(
                "close-popup-overlay",
                signature=signature,
                capability="dialog_overlay",
            ),
        ),
    )

    assert len(second.entries) == 2


def test_h5_task_frontier_deduplicates_same_popup_occurrence_in_one_capture() -> None:
    signature = "h5-obstruction:" + "b" * 64
    candidate = _close_candidate("close-popup-1", signature=signature)
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup-1",
        source_state_version="popup-state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )
    repeated = first.merge(
        source_capture_id="capture-popup-1",
        source_state_version="popup-state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(candidate,),
    )

    assert len(repeated.entries) == 1


def test_popup_close_does_not_consume_page_sampling_budget() -> None:
    """弹窗清障不是业务页面覆盖，不能被 quick first_n 页面预算截断。"""
    first = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id="capture-popup-1",
        source_state_version="popup-state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-popup-1"),),
    )
    entry_id = first.pending_entries[0].entry_id
    selected = first.select_entry(entry_id)
    executed = selected.mark_executed(entry_id)

    assert executed.covered_page_count == 0

    second = executed.merge(
        source_capture_id="capture-popup-2",
        source_state_version="popup-state-2",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-popup-2"),),
    )

    assert second.pending_count == 1
    assert second.blocked_count == 0
    assert second.pending_entries[0].candidate.semantic_target == "关闭当前弹窗"


def test_page_sampling_budget_still_blocks_business_navigation_after_coverage() -> None:
    covered = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_candidate("content", page_type="content_list"),),
    )
    entry_id = covered.pending_entries[0].entry_id
    selected = covered.select_entry(entry_id)
    executed = selected.mark_executed(entry_id)

    assert executed.covered_page_count == 1
    assert executed.covered_page_types == frozenset({"content_list"})

    blocked_business = executed.merge(
        source_capture_id="capture-content",
        source_state_version="state-content",
        source_location="https://example.test/content",
        navigation_depth=1,
        candidates=(_candidate("settings", page_type="settings"),),
    )

    assert blocked_business.pending_count == 0
    assert blocked_business.blocked_count == 1
    assert blocked_business.entries[-1].last_failure_reason == "max_pages_reached"

    popup_allowed = executed.merge(
        source_capture_id="capture-popup",
        source_state_version="state-popup",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-popup"),),
    )

    assert popup_allowed.pending_count == 1
    assert popup_allowed.blocked_count == 0


def test_h5_task_frontier_keeps_bounded_line_observes_despite_include_policy() -> None:
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=None,
        sampling_policy_summary={
            "include_page_types": ["home"],
            "exclude_page_types": [],
            "journey_steps": [],
        },
    ).merge(
        source_capture_id="capture-line-1",
        source_state_version="state-line",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(_line_observe_candidate("line-observe-1", attempt=1),),
    )
    snapshot = snapshot.merge(
        source_capture_id="capture-line-2",
        source_state_version="state-line",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(_line_observe_candidate("line-observe-2", attempt=2),),
    )

    assert snapshot.pending_count == 2
    assert snapshot.blocked_count == 0
    assert [entry.candidate.candidate_id for entry in snapshot.entries] == [
        "line-observe-1",
        "line-observe-2",
    ]


def test_h5_task_frontier_keeps_same_target_with_different_expected_page_type() -> None:
    """若 fingerprint 未纳入预期页型，语义相同但目的不同的动作会被错误合并。"""
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(**_merge_args("state-1", "product"))
    second = merge_h5_candidates(
        first,
        source_capture_id="capture-state-2",
        source_state_version="state-2",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_candidate("product", page_type="product_reviews"),),
    )

    assert len(second.entries) == 2


def test_h5_task_frontier_deduplicates_page_tab_when_expected_page_type_changes() -> None:
    """确定性页签不能因模型改变结果页型猜测而重复探索。"""
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-anime-1",
        source_state_version="state-anime-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "anime-home",
                index=5,
                label="动漫",
                source_page_type="home",
                expected_page_type="home",
            ),
        ),
    )
    entry = first.entries[0]
    executed = first.select_entry(entry.entry_id).mark_executed(entry.entry_id)

    merged = executed.merge(
        source_capture_id="capture-anime-2",
        source_state_version="state-anime-2",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "anime-content-list",
                index=5,
                label="动漫",
                source_page_type="home",
                expected_page_type="content_list",
            ),
        ),
    )

    assert len(merged.entries) == 1
    assert merged.entries[0].status is H5FrontierEntryStatus.EXECUTED


def test_h5_task_frontier_keeps_same_page_tab_on_different_route() -> None:
    """不同路由里的同序号同名页签仍是两个独立业务入口。"""
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "anime-home",
                index=5,
                label="动漫",
                source_page_type="home",
                expected_page_type="home",
            ),
        ),
    )

    merged = first.merge(
        source_capture_id="capture-community",
        source_state_version="state-community",
        source_location="https://example.test/community",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "anime-community",
                index=5,
                label="动漫",
                source_page_type="home",
                expected_page_type="content_list",
            ),
        ),
    )

    assert len(merged.entries) == 2


def test_h5_task_frontier_keeps_overlapping_indices_from_distinct_tab_groups() -> None:
    """同页多组 Tab 的序号可重叠，组签名和标签必须共同区分动作身份。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "top-tab-domestic",
                index=3,
                label="国产",
                source_page_type="home",
                expected_page_type="home",
                pattern_signature="a" * 64,
            ),
            _page_tab_candidate(
                "content-tab-latest-review",
                index=3,
                label="最新热评",
                source_page_type="home",
                expected_page_type="home",
                pattern_signature="b" * 64,
            ),
        ),
    )

    assert len(snapshot.entries) == 2
    assert len({entry.entry_id for entry in snapshot.entries}) == 2
    assert len(snapshot.action_fingerprints) == 2


def test_h5_task_frontier_deduplicates_page_tab_across_visible_group_slices() -> None:
    """横向滚动改变模式签名和序号时，同名业务栏目不能重复进入 Frontier。"""
    first = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-1",
        source_state_version="state-home-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "anime-slice-1",
                index=6,
                label="动漫",
                source_page_type="home",
                expected_page_type="home",
                pattern_signature="a" * 64,
            ),
        ),
    )
    entry = first.entries[0]
    executed = first.select_entry(entry.entry_id).mark_executed(entry.entry_id)

    merged = executed.merge(
        source_capture_id="capture-home-2",
        source_state_version="state-home-2",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "anime-slice-2",
                index=2,
                label="动漫",
                source_page_type="home",
                expected_page_type="home",
                pattern_signature="b" * 64,
            ),
        ),
    )

    assert len(merged.entries) == 1
    assert merged.entries[0].status is H5FrontierEntryStatus.EXECUTED


def test_h5_task_frontier_separates_policy_exclusion_from_real_blockers() -> None:
    """策略排除只留审计，深度耗尽仍作为真实阻断进入完成门禁。"""
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=1,
        sampling_policy_summary={
            "include_page_types": ["product"],
            "exclude_page_types": ["payment"],
            "journey_steps": [],
        },
    ).merge(
        source_capture_id="capture-1",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _candidate("product", page_type="product"),
            _candidate("payment", page_type="payment"),
            _candidate("deep", page_type="product", depth=3),
        ),
        max_depth=2,
    )

    assert snapshot.pending_count == 1
    assert snapshot.blocked_count == 1
    assert snapshot.failed_count == 0
    assert [
        item.last_failure_reason for item in snapshot.entries if item.status.value == "blocked"
    ] == [
        "depth_limit_exceeded",
    ]
    skipped = [item for item in snapshot.entries if item.status.value == "skipped"]
    assert len(skipped) == 1
    assert skipped[0].last_failure_reason == "page_type_excluded"


def test_h5_task_frontier_passes_through_unselected_pages_until_selection_covered() -> None:
    """只选详情页时要能经过列表页去找；详情页走到后，剩下的经过类候选全部跳过。"""
    snapshot = H5TaskFrontierSnapshot.create(
        max_pages=None,
        sampling_policy_summary={
            "include_page_types": ["video_detail"],
            "exclude_page_types": [],
            "journey_steps": [],
        },
    ).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(
            _candidate("to-list", page_type="video_list"),
            _candidate("to-profile", page_type="profile"),
        ),
    )

    assert snapshot.pending_count == 2
    to_list = next(
        entry for entry in snapshot.pending_entries if entry.candidate.candidate_id == "to-list"
    )
    on_list = snapshot.select_entry(to_list.entry_id).mark_executed(to_list.entry_id)
    on_list = on_list.merge(
        source_capture_id="capture-list",
        source_state_version="state-list",
        source_location="https://example.test/list",
        navigation_depth=1,
        parent_state_version="state-home",
        candidates=(_candidate("to-detail", page_type="video_detail", depth=2),),
    )
    to_detail = next(
        entry for entry in on_list.pending_entries if entry.candidate.candidate_id == "to-detail"
    )

    done = on_list.select_entry(to_detail.entry_id).mark_executed(to_detail.entry_id)
    done = done.merge(
        source_capture_id="capture-detail",
        source_state_version="state-detail",
        source_location="https://example.test/detail/1",
        navigation_depth=2,
        parent_state_version="state-list",
        candidates=(_candidate("to-comments", page_type="comment_list", depth=3),),
    )

    assert done.pending_count == 0
    assert {
        entry.candidate.candidate_id: entry.last_failure_reason
        for entry in done.entries
        if entry.status is H5FrontierEntryStatus.SKIPPED
    } == {
        "to-profile": "page_type_not_included",
        "to-comments": "page_type_not_included",
    }


def test_h5_task_frontier_selects_active_lineage_before_recoverable_sibling() -> None:
    """若选择器改为全局插入顺序，会越过当前 lineage 而破坏有界深度优先推进。"""
    snapshot = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-1",
            source_state_version="state-1",
            source_location="https://example.test/home",
            navigation_depth=0,
            candidates=(_candidate("sibling"),),
        )
        .merge(
            source_capture_id="capture-2",
            source_state_version="state-2",
            source_location="https://example.test/product",
            navigation_depth=1,
            parent_state_version="state-1",
            candidates=(_candidate("child", depth=2),),
        )
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-2")

    assert selected is not None
    assert selected.candidate.candidate_id == "child"


def test_h5_task_frontier_prefers_nested_return_before_detail_local_navigation() -> None:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-video-detail",
        source_state_version="state-video-detail",
        source_location="https://example.test/movieDetails?id=1",
        navigation_depth=1,
        candidates=(
            _candidate("line-switch", page_type="video_list"),
            _nested_return_candidate("back-video", source_page_type="video_detail"),
        ),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-video-detail")

    assert selected is not None
    assert selected.candidate.candidate_id == "back-video"


def test_h5_task_frontier_prefers_uncovered_same_state_page_type() -> None:
    """当前页同时有已覆盖首页和未覆盖设置时，应先扩展新页型。"""
    initial = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(_candidate("home-first", page_type="home"),),
    )
    home_entry = initial.pending_entries[0]
    covered = initial.select_entry(home_entry.entry_id).mark_executed(
        home_entry.entry_id,
    )
    snapshot = covered.merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.test/",
        navigation_depth=1,
        parent_state_version="state-home",
        candidates=(
            _candidate("home-return", page_type="home"),
            _candidate("profile-settings", page_type="settings"),
        ),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-profile")

    assert snapshot.covered_page_types == frozenset({"home"})
    assert selected is not None
    assert selected.candidate.candidate_id == "profile-settings"


def test_h5_task_frontier_keeps_pending_branches_after_page_type_is_covered() -> None:
    """页型覆盖不是业务入口等价证明，未访问入口仍保留。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(
            _candidate("home-first", page_type="home"),
            _candidate("home-second", page_type="home"),
            _candidate("settings", page_type="settings"),
        ),
    )
    first = next(
        entry
        for entry in snapshot.pending_entries
        if entry.candidate.candidate_id == "home-first"
    )

    completed = snapshot.select_entry(first.entry_id).mark_executed(first.entry_id)

    second = next(
        entry
        for entry in completed.entries
        if entry.candidate.candidate_id == "home-second"
    )
    assert second.status is H5FrontierEntryStatus.PENDING
    assert second.last_failure_reason is None
    assert completed.blocked_count == 0
    selected = select_next_h5_entry(completed)
    assert selected is not None
    assert selected.candidate.candidate_id == "settings"


def test_h5_task_frontier_keeps_new_entry_for_already_covered_page_type() -> None:
    initial = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(_candidate("home-first", page_type="home"),),
    )
    entry = initial.pending_entries[0]
    covered = initial.select_entry(entry.entry_id).mark_executed(entry.entry_id)

    merged = covered.merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.test/Mine",
        navigation_depth=1,
        candidates=(
            _candidate("home-return", page_type="home"),
            _close_candidate("profile-popup-close"),
        ),
    )

    home_return = next(
        item
        for item in merged.entries
        if item.candidate.candidate_id == "home-return"
    )
    assert home_return.status is H5FrontierEntryStatus.PENDING
    assert home_return.last_failure_reason is None
    selected = select_next_h5_entry(merged, active_state_version="state-profile")
    assert selected is not None
    assert selected.candidate.semantic_target == "关闭当前弹窗"


def test_h5_task_frontier_keeps_distinct_page_tabs_after_page_type_is_covered() -> None:
    """同一页型的不同栏目是不同业务分支，不能被页型覆盖去重吞掉。"""

    snapshot = H5TaskFrontierSnapshot.create(max_pages=10).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "home-domestic",
                index=2,
                label="国产",
                source_page_type="home",
                expected_page_type="home",
            ),
            _page_tab_candidate(
                "home-animation",
                index=3,
                label="动漫",
                source_page_type="home",
                expected_page_type="home",
            ),
            _candidate("home-card", page_type="home"),
        ),
    )
    domestic = next(
        entry for entry in snapshot.entries if entry.candidate.candidate_id == "home-domestic"
    )

    completed = snapshot.select_entry(domestic.entry_id).mark_executed(domestic.entry_id)

    animation = next(
        entry for entry in completed.entries if entry.candidate.candidate_id == "home-animation"
    )
    ordinary_card = next(
        entry for entry in completed.entries if entry.candidate.candidate_id == "home-card"
    )
    assert animation.status is H5FrontierEntryStatus.PENDING
    assert ordinary_card.status is H5FrontierEntryStatus.PENDING
    assert ordinary_card.last_failure_reason is None


def test_h5_task_frontier_accepts_new_navigation_branch_for_covered_page_type() -> None:
    covered = H5TaskFrontierSnapshot.create(max_pages=10).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_candidate("home-first", page_type="home"),),
    )
    entry = covered.pending_entries[0]
    covered = covered.select_entry(entry.entry_id).mark_executed(entry.entry_id)

    merged = covered.merge(
        source_capture_id="capture-home-new",
        source_state_version="state-home-new",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "home-category",
                index=4,
                label="漫画",
                source_page_type="home",
                expected_page_type="home",
            ),
        ),
    )

    category = next(
        entry for entry in merged.entries if entry.candidate.candidate_id == "home-category"
    )
    assert category.status is H5FrontierEntryStatus.PENDING


def test_h5_task_frontier_keeps_distinct_bottom_tabs_with_same_page_type() -> None:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=10).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _bottom_navigation_candidate(
                "bottom-short",
                index=2,
                label="短剧",
                source_page_type="home",
                expected_page_type="content_list",
            ),
            _bottom_navigation_candidate(
                "bottom-ai",
                index=4,
                label="AI",
                source_page_type="home",
                expected_page_type="content_list",
            ),
        ),
    )
    short = next(
        entry for entry in snapshot.entries if entry.candidate.candidate_id == "bottom-short"
    )

    completed = snapshot.select_entry(short.entry_id).mark_executed(short.entry_id)

    ai = next(entry for entry in completed.entries if entry.candidate.candidate_id == "bottom-ai")
    assert ai.status is H5FrontierEntryStatus.PENDING


def test_h5_task_frontier_distinct_navigation_branches_still_obey_page_budget() -> None:
    """放开同页型业务分支不能绕过任务级页面预算。"""

    snapshot = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _page_tab_candidate(
                "home-domestic",
                index=2,
                label="国产",
                source_page_type="home",
                expected_page_type="home",
            ),
            _page_tab_candidate(
                "home-animation",
                index=3,
                label="动漫",
                source_page_type="home",
                expected_page_type="home",
            ),
        ),
    )
    domestic = next(
        entry for entry in snapshot.entries if entry.candidate.candidate_id == "home-domestic"
    )

    completed = snapshot.select_entry(domestic.entry_id).mark_executed(domestic.entry_id)

    animation = next(
        entry for entry in completed.entries if entry.candidate.candidate_id == "home-animation"
    )
    assert animation.status is H5FrontierEntryStatus.BLOCKED
    assert animation.last_failure_reason == "max_pages_reached"


def test_platform_control_observation_keeps_unvisited_business_branches() -> None:
    """清障后的真实页型参与覆盖统计，不代替业务分支去重。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup",
        source_state_version="state-popup",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(
            _close_candidate("popup-close"),
            _candidate("home-return", page_type="home"),
        ),
    )
    close_entry = next(
        item
        for item in snapshot.pending_entries
        if item.candidate.candidate_id == "popup-close"
    )

    executed = snapshot.select_entry(close_entry.entry_id).mark_executed(
        close_entry.entry_id,
    )
    observed = executed.record_observed_page_type(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/",
        navigation_depth=0,
        page_type="home",
    )

    # 观察到的真实业务页作为普通 executed 账本项计数，使旧 v1 Worker
    # 与当前版本从同一 entries 推导出完全一致的 count/type。
    assert observed.covered_page_count == 1
    assert observed.covered_page_types == frozenset({"home"})
    assert H5TaskFrontierSnapshot.from_dict(observed.to_dict()) == observed
    home_return = next(
        item
        for item in observed.entries
        if item.candidate.candidate_id == "home-return"
    )
    assert home_return.status is H5FrontierEntryStatus.PENDING
    assert home_return.last_failure_reason is None


def test_initial_popup_close_survives_observed_page_budget_exhaustion() -> None:
    """初始业务页即使耗尽页预算，也必须先允许平台清障关闭弹窗。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id="capture-popup",
        source_state_version="state-popup",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(
            _close_candidate("popup-close"),
            _candidate("home-return", page_type="home"),
        ),
    )

    observed = snapshot.record_observed_page_type(
        source_capture_id="capture-popup",
        source_state_version="state-popup",
        source_location="https://example.test/",
        navigation_depth=0,
        page_type="home",
    )

    close_entry = next(
        entry
        for entry in observed.entries
        if entry.candidate.candidate_id == "popup-close"
    )
    assert observed.covered_page_count == 1
    assert close_entry.status is H5FrontierEntryStatus.PENDING
    selected = select_next_h5_entry(observed, active_state_version="state-popup")
    assert selected is not None
    assert selected.candidate.candidate_id == "popup-close"


def test_h5_task_frontier_prefers_profile_local_action_before_leaving_state() -> None:
    """覆盖事实暂缺时，也应先执行当前个人页的结构能力再离开页面。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.test/",
        navigation_depth=1,
        candidates=(
            _candidate("home-return", page_type="home"),
            _candidate(
                "profile-settings",
                page_type="settings",
                target_keywords=("settings_entry", "profile_local_action"),
            ),
        ),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-profile")

    assert snapshot.covered_page_types == frozenset()
    assert selected is not None
    assert selected.candidate.candidate_id == "profile-settings"


def test_h5_task_frontier_revisit_ancestor_keeps_recent_acyclic_lineage() -> None:
    """子页回访旧状态时须保留实际父节点，并把旧状态移到谱系末尾。"""
    snapshot = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-root",
            source_state_version="state-root",
            source_location="https://example.test/home",
            navigation_depth=0,
            candidates=(_candidate("root-sibling"),),
        )
        .merge(
            source_capture_id="capture-profile",
            source_state_version="state-profile",
            source_location="https://example.test/mine",
            navigation_depth=1,
            parent_state_version="state-root",
            candidates=(_candidate("profile-settings"),),
        )
        .merge(
            source_capture_id="capture-activity",
            source_state_version="state-activity",
            source_location="https://example.test/task_sign_day",
            navigation_depth=2,
            parent_state_version="state-profile",
            candidates=(
                _nested_return_candidate("activity-return", source_page_type="activity"),
            ),
        )
    )

    returned = snapshot.merge(
        source_capture_id="capture-profile-returned",
        source_state_version="state-profile",
        source_location="https://example.test/mine",
        navigation_depth=1,
        parent_state_version="state-activity",
        candidates=(_close_candidate("profile-popup-after-return"),),
    )

    returned_entry = next(
        entry
        for entry in returned.entries
        if entry.candidate.candidate_id == "profile-popup-after-return"
    )
    assert returned_entry.parent_state_version == "state-activity"
    assert returned_entry.lineage == (
        "state-root",
        "state-activity",
        "state-profile",
    )
    assert len(returned_entry.lineage) == len(set(returned_entry.lineage))

    close_selected = select_next_h5_entry(returned, active_state_version="state-profile")
    assert close_selected is not None
    assert close_selected.candidate.candidate_id == "profile-popup-after-return"
    after_close = returned.select_entry(close_selected.entry_id).mark_executed(
        close_selected.entry_id,
    )
    next_selected = select_next_h5_entry(
        after_close,
        active_state_version="state-profile",
    )
    assert next_selected is not None
    assert next_selected.candidate.candidate_id == "profile-settings"


def test_h5_task_frontier_selects_obstruction_close_before_same_state_candidates() -> None:
    """遮挡状态下清障关闭候选即使排在候选末位也必须先被选中。

    检测端把「关闭当前弹窗」追加在模型候选之后；若选择器退回插入顺序，
    被蒙层拦截的卡片点击会先被逐个消耗（e4f35035 实况：/movie 七连白点）。
    """
    snapshot = merge_h5_candidates(
        H5TaskFrontierSnapshot.create(max_pages=None),
        source_capture_id="capture-popup",
        source_state_version="state-popup",
        source_location="https://example.test/movie",
        navigation_depth=0,
        candidates=(
            _candidate("card-one"),
            _candidate("card-two"),
            _close_candidate("close-popup"),
        ),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-popup")

    assert selected is not None
    assert selected.candidate.candidate_id == "close-popup"


def test_h5_task_frontier_prefers_stable_duplicate_over_popup_source() -> None:
    """局部切换产生未知状态时，不得先恢复弹窗下发现的旧业务候选。"""
    popup_profile = CandidateAction(
        candidate_id="profile-from-popup",
        semantic_target="我的",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "source_page_state": "popup",
            "expected_next_page_type": "profile",
            "target_keywords": ["我的"],
        },
    )
    stable_profile = CandidateAction(
        candidate_id="profile-from-stable",
        semantic_target="我的",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "source_page_state": "stable",
            "expected_next_page_type": "profile",
            "target_keywords": ["我的"],
        },
    )
    lobby = CandidateAction(
        candidate_id="lobby-from-stable",
        semantic_target="大厅",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=95,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "source_page_state": "stable",
            "expected_next_page_type": "content_list",
            "target_keywords": ["大厅"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup",
        source_state_version="state-popup",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(popup_profile,),
    )
    snapshot = snapshot.merge(
        source_capture_id="capture-stable",
        source_state_version="state-stable",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(lobby, stable_profile),
    )
    lobby_entry = next(
        entry
        for entry in snapshot.pending_entries
        if entry.candidate.candidate_id == lobby.candidate_id
    )
    snapshot = snapshot.select_entry(lobby_entry.entry_id).mark_executed(lobby_entry.entry_id)

    selected = select_next_h5_entry(
        snapshot,
        active_state_version="state-after-local-switch",
    )

    assert selected is not None
    assert selected.candidate.candidate_id == "profile-from-stable"


def test_h5_task_frontier_prefers_typed_sibling_over_untyped_same_source() -> None:
    """同来源局部候选打平时，已知覆盖目标不得被无类型动作饿死。"""
    untyped = _candidate("雅间", page_type=None, depth=0)
    untyped = CandidateAction(
        candidate_id=untyped.candidate_id,
        semantic_target=untyped.semantic_target,
        action_type=untyped.action_type,
        risk_level=untyped.risk_level,
        exploration_value=untyped.exploration_value,
        navigation_depth=untyped.navigation_depth,
        expected_surface_kind=untyped.expected_surface_kind,
        metadata={
            "source_page_type": "home",
            "source_page_state": "stable",
            "expected_next_page_type": None,
            "target_keywords": ["雅间"],
        },
    )
    profile = CandidateAction(
        candidate_id="profile-typed",
        semantic_target="我的",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "home",
            "source_page_state": "stable",
            "expected_next_page_type": "profile",
            "target_keywords": ["我的"],
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home-stable",
        source_state_version="state-home-stable",
        source_location="https://example.test/",
        navigation_depth=0,
        candidates=(untyped, profile),
    )

    selected = select_next_h5_entry(
        snapshot,
        active_state_version="state-after-local-switch",
    )

    assert selected is not None
    assert selected.candidate.candidate_id == "profile-typed"


def test_h5_task_frontier_prefers_medium_uncovered_type_over_low_covered_revisit() -> None:
    """受信 medium 新页型不得被无限生成的 low 已覆盖导航饿死。"""
    initial = _candidate("initial-home", page_type="home", depth=0)
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(initial,),
    )
    initial_entry = snapshot.pending_entries[0]
    snapshot = snapshot.select_entry(initial_entry.entry_id).mark_executed(
        initial_entry.entry_id
    )
    payment = _candidate(
        "profile-payment",
        page_type="payment",
        risk=InteractionRiskLevel.MEDIUM,
        target_keywords=("profile_local_action",),
    )
    home_revisit = _candidate("profile-home-revisit", page_type="home")
    snapshot = snapshot.merge(
        source_capture_id="capture-profile",
        source_state_version="state-profile",
        source_location="https://example.test/mine",
        navigation_depth=1,
        candidates=(home_revisit, payment),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-profile")

    assert selected is not None
    assert selected.candidate.candidate_id == "profile-payment"
    assert selected.candidate.risk_level is InteractionRiskLevel.MEDIUM


def test_h5_task_frontier_transitions_immutably_and_enforces_page_budget() -> None:
    """不同入口保留；总页预算用尽后阻断尚未访问的分支。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=2).merge(
        source_capture_id="capture-1",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            _candidate("product-one", page_type="product"),
            _candidate("product-two", page_type="product"),
            _candidate("cart", page_type="cart"),
        ),
    )
    started_at = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
    finished_at = datetime(2026, 8, 12, 12, 1, tzinfo=UTC)

    first_selected = snapshot.select_entry(
        snapshot.pending_entries[0].entry_id,
        selected_at=started_at,
    )
    first_executed = first_selected.mark_executed(
        snapshot.pending_entries[0].entry_id,
        finished_at=finished_at,
    )
    cart = next(
        item for item in first_executed.pending_entries if item.candidate.candidate_id == "cart"
    )
    second_selected = first_executed.select_entry(
        cart.entry_id,
        selected_at=started_at,
    )
    exhausted = second_selected.mark_executed(
        cart.entry_id,
        finished_at=finished_at,
    )

    assert snapshot.pending_count == 3
    assert exhausted.visited_state_ids == frozenset({"state-1"})
    assert exhausted.executed_candidate_ids == frozenset({"product-one", "cart"})
    assert exhausted.covered_page_count == 2
    assert exhausted.covered_page_types == frozenset({"product", "cart"})
    assert exhausted.pending_count == 0
    assert exhausted.blocked_count == 1
    product_two = next(
        item
        for item in exhausted.entries
        if item.candidate.candidate_id == "product-two"
    )
    assert product_two.last_failure_reason == "max_pages_reached"
    assert product_two.status is H5FrontierEntryStatus.BLOCKED


def _reobserve_candidate(candidate_id: str, *, capture_id: str) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target="当前列表首个内容卡片",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "expected_next_page_type": "video_detail",
            "action_reobservation_capture_id": capture_id,
        },
    )


def test_semantic_reobservation_retry_budget_stops_candidate_regrowth() -> None:
    """同页同语义候选被跳过 5 次后不得再重生，防止重观察土拨鼠循环撑爆载荷。"""
    location = "https://example.test/list"
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for round_index in range(5):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{round_index}",
            source_state_version=f"state-{round_index}",
            source_location=location,
            navigation_depth=0,
            candidates=(
                _reobserve_candidate(
                    f"card-{round_index}", capture_id=f"capture-{round_index}"
                ),
            ),
        )
        entry = snapshot.pending_entries[-1]
        snapshot = snapshot.select_entry(
            entry.entry_id, selected_at=datetime(2026, 8, 17, tzinfo=UTC)
        ).mark_skipped(
            entry.entry_id,
            reason="retryable_action_reobserved:LIST_DETAIL_TARGET_NOT_FOUND",
            finished_at=datetime(2026, 8, 17, 0, round_index + 1, tzinfo=UTC),
        )

    before_count = len(snapshot.entries)
    capped = snapshot.merge(
        source_capture_id="capture-final",
        source_state_version="state-final",
        source_location=location,
        navigation_depth=0,
        candidates=(_reobserve_candidate("card-final", capture_id="capture-final"),),
    )

    assert len(capped.entries) == before_count
    assert capped.pending_entries == ()

    # 不同页面的同语义候选不受本页预算影响。
    other_page = capped.merge(
        source_capture_id="capture-other",
        source_state_version="state-other",
        source_location="https://example.test/other-list",
        navigation_depth=0,
        candidates=(_reobserve_candidate("card-other", capture_id="capture-other"),),
    )
    assert len(other_page.pending_entries) == 1

    # 弹窗清障候选走业务页 20 轮策略，不受本预算约束。
    popup = other_page.merge(
        source_capture_id="capture-popup",
        source_state_version="state-popup",
        source_location=location,
        navigation_depth=0,
        candidates=(_close_candidate("close-final"),),
    )
    assert any(
        entry.candidate.semantic_target == "关闭当前弹窗"
        for entry in popup.pending_entries
    )


def test_h5_task_frontier_marks_skipped_without_coverage_or_failure_counts() -> None:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=2).merge(
        **_merge_args("state-uncatalogued", "removed_page_type")
    )
    entry_id = snapshot.pending_entries[0].entry_id
    selected = snapshot.select_entry(entry_id, selected_at=datetime(2026, 8, 16, tzinfo=UTC))

    skipped = selected.mark_skipped(
        entry_id,
        reason="uncatalogued_page_type",
        finished_at=datetime(2026, 8, 16, 0, 1, tzinfo=UTC),
    )
    restored = H5TaskFrontierSnapshot.from_dict(skipped.to_dict())

    assert restored.entries[0].status is H5FrontierEntryStatus.SKIPPED
    assert restored.entries[0].last_failure_reason == "uncatalogued_page_type"
    assert restored.entries[0].finished_at is not None
    assert restored.covered_page_count == 0
    assert restored.covered_page_types == frozenset()
    assert restored.pending_count == 0
    assert restored.blocked_count == 0
    assert restored.failed_count == 0


def test_h5_task_frontier_records_restoration_attempt_immutably_and_round_trips() -> None:
    """若恢复尝试未写回快照，重启后 sibling 会绕过两次上限。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(**_merge_args("state-1", "cart"))
    selected = snapshot.select_entry(snapshot.pending_entries[0].entry_id)

    attempted = selected.record_restoration_attempt(selected.entries[0].entry_id)
    restored = H5TaskFrontierSnapshot.from_dict(attempted.to_dict())

    assert selected.entries[0].restoration_attempt_count == 0
    assert restored.entries[0].restoration_attempt_count == 1
    assert restored.payload_hash == restored.recompute_payload_hash()
    with pytest.raises(H5FrontierValidationError, match="entry_id 不存在"):
        selected.record_restoration_attempt("missing-entry")
    executed = selected.mark_executed(selected.entries[0].entry_id)
    with pytest.raises(H5FrontierValidationError, match="selected"):
        executed.record_restoration_attempt(executed.entries[0].entry_id)


def test_h5_task_frontier_can_audit_skip_a_stale_pending_entry() -> None:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        **_merge_args("state-popup", "关闭当前弹窗")
    )
    entry_id = snapshot.pending_entries[0].entry_id

    skipped = snapshot.mark_pending_skipped(
        entry_id,
        reason="popup_obstruction_absent_on_stable_page",
        active_state_version="state-home-stable",
        finished_at=datetime(2026, 8, 16, 0, 1, tzinfo=UTC),
    )

    assert skipped.entries[0].status is H5FrontierEntryStatus.SKIPPED
    assert skipped.entries[0].selected_at is None
    assert skipped.entries[0].last_failure_reason == ("popup_obstruction_absent_on_stable_page")
    assert skipped.active_state_version == "state-home-stable"
    assert skipped.pending_count == 0


def test_h5_task_frontier_prefers_nearest_common_ancestor_sibling() -> None:
    """若 sibling 距离取自身 lineage 长度，根级 sibling 会错误早于近祖先 sibling。"""
    snapshot = (
        H5TaskFrontierSnapshot.create(max_pages=None)
        .merge(
            source_capture_id="capture-root",
            source_state_version="root",
            source_location="https://example.test/root",
            navigation_depth=0,
            candidates=(_candidate("root-sibling"),),
        )
        .merge(
            source_capture_id="capture-middle",
            source_state_version="middle",
            source_location="https://example.test/middle",
            navigation_depth=1,
            parent_state_version="root",
            candidates=(_candidate("near-sibling"),),
        )
        .merge(
            source_capture_id="capture-leaf",
            source_state_version="leaf",
            source_location="https://example.test/leaf",
            navigation_depth=2,
            parent_state_version="middle",
            candidates=(_candidate("active-branch"),),
        )
    )
    active = select_next_h5_entry(snapshot, active_state_version="leaf")
    assert active is not None
    exhausted = snapshot.select_entry(active.entry_id).mark_failed(
        active.entry_id,
        reason="interaction_failed",
    )

    selected = select_next_h5_entry(exhausted)

    assert exhausted.failed_count == 1
    assert selected is not None
    assert selected.candidate.candidate_id == "near-sibling"


def test_h5_task_frontier_keeps_nearest_sibling_ahead_of_far_journey_target() -> None:
    """若页型优先级排在恢复距离之前，远端旅程目标会抢占最近 sibling。"""
    snapshot = (
        H5TaskFrontierSnapshot.create(
            max_pages=None,
            sampling_policy_summary={"journey_steps": ["far-journey"]},
        )
        .merge(
            source_capture_id="capture-root",
            source_state_version="root",
            source_location="https://example.test/root",
            navigation_depth=0,
            candidates=(_candidate("far-journey"),),
        )
        .merge(
            source_capture_id="capture-middle",
            source_state_version="middle",
            source_location="https://example.test/middle",
            navigation_depth=1,
            parent_state_version="root",
            candidates=(_candidate("near-sibling"),),
        )
        .merge(
            source_capture_id="capture-leaf",
            source_state_version="leaf",
            source_location="https://example.test/leaf",
            navigation_depth=2,
            parent_state_version="middle",
            candidates=(_candidate("active-branch"),),
        )
    )
    active = select_next_h5_entry(snapshot, active_state_version="leaf")
    assert active is not None
    exhausted = snapshot.select_entry(active.entry_id).mark_failed(
        active.entry_id,
        reason="interaction_failed",
    )

    selected = select_next_h5_entry(exhausted)

    assert selected is not None
    assert selected.candidate.candidate_id == "near-sibling"


def test_h5_task_frontier_rejects_rule_target_policy_as_non_exploration_input() -> None:
    with pytest.raises(H5FrontierValidationError, match="未知字段"):
        H5TaskFrontierSnapshot.create(
            max_pages=None,
            sampling_policy_summary={
                "include_page_types": ["home", "product"],
                "exclude_page_types": [],
                "rule_target_page_types": ["product"],
                "journey_steps": [],
            },
        )


def _skip_all_pending(snapshot: H5TaskFrontierSnapshot) -> H5TaskFrontierSnapshot:
    for entry in snapshot.pending_entries:
        snapshot = snapshot.mark_pending_skipped(
            entry.entry_id,
            reason="stale_popup_superseded",
            active_state_version=entry.source_state_version,
        )
    return snapshot


def test_retire_skipped_entries_bounds_live_entries_and_keeps_dedup() -> None:
    """若退休丢掉语义去重键，弹窗墙场景旧候选会在重观察时无限重生。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for index in range(6):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{index}",
            source_state_version=f"state-{index}",
            source_location="https://example.test/home",
            navigation_depth=0,
            candidates=(_close_candidate(f"close-{index}"),),
        )
    snapshot = _skip_all_pending(snapshot)

    retired = retire_h5_skipped_entries(snapshot, max_retained_skipped=2)

    skipped_live = [
        entry for entry in retired.entries if entry.status is H5FrontierEntryStatus.SKIPPED
    ]
    assert len(skipped_live) == 2
    assert [entry.candidate.candidate_id for entry in skipped_live] == ["close-4", "close-5"]
    assert len(retired.retired_semantic_keys) == 4
    assert sum(retired.retired_skip_counts.values()) == 4
    # 已退休的同语义候选在同一状态与路由再次出现时不得重生
    revived = retired.merge(
        source_capture_id="capture-0",
        source_state_version="state-0",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_close_candidate("close-0"),),
    )
    assert len(revived.entries) == len(retired.entries)


def test_retire_skipped_entries_noop_within_limit() -> None:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        **_merge_args("state-1", "product")
    )
    snapshot = _skip_all_pending(snapshot)

    assert retire_h5_skipped_entries(snapshot, max_retained_skipped=24) is snapshot
    assert "retired_semantic_keys" not in snapshot.to_dict()
    assert "retired_skip_counts" not in snapshot.to_dict()


def test_retired_skip_counts_keep_reobservation_budget_exhausted() -> None:
    """若退休丢掉预算计数，重观察候选会绕过同页同语义重试上限复活。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for index in range(H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{index}",
            source_state_version=f"state-{index}",
            source_location="https://example.test/list",
            navigation_depth=1,
            candidates=(
                _reobserve_candidate("card", capture_id=f"reobserve-{index}"),
            ),
        )
    snapshot = _skip_all_pending(snapshot)
    retired = retire_h5_skipped_entries(snapshot, max_retained_skipped=0)
    assert not any(
        entry.status is H5FrontierEntryStatus.SKIPPED for entry in retired.entries
    )

    budget_probe = retired.merge(
        source_capture_id="capture-next",
        source_state_version="state-next",
        source_location="https://example.test/list",
        navigation_depth=1,
        candidates=(_reobserve_candidate("card", capture_id="reobserve-next"),),
    )

    assert len(budget_probe.entries) == len(retired.entries)


def test_retired_snapshot_round_trips_with_tombstones() -> None:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for index in range(3):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{index}",
            source_state_version=f"state-{index}",
            source_location="https://example.test/home",
            navigation_depth=0,
            candidates=(_candidate(f"nav-{index}"),),
        )
    snapshot = _skip_all_pending(snapshot)
    retired = retire_h5_skipped_entries(snapshot, max_retained_skipped=1)

    restored = H5TaskFrontierSnapshot.from_dict(retired.to_dict())

    assert restored.retired_semantic_keys == retired.retired_semantic_keys
    assert dict(restored.retired_skip_counts) == dict(retired.retired_skip_counts)
    assert restored.payload_hash == restored.recompute_payload_hash()
    followed = restored.merge(**_merge_args("state-9", "fresh"))
    assert followed.retired_semantic_keys == retired.retired_semantic_keys


def test_with_task_frontier_retires_overflow_before_embedding() -> None:
    """若嵌入点不做退休，弹窗墙任务的 ExplorationWork 会撑爆 256KB 协议上限。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.h5_frontier import (
        H5_RETAINED_SKIPPED_LIVE_LIMIT,
        H5_TASK_FRONTIER_METADATA_KEY,
    )
    from services.api.app.domain.exploration.snapshots import (
        FrontierSnapshot,
        SurfaceObservation,
    )

    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for index in range(H5_RETAINED_SKIPPED_LIVE_LIMIT + 10):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{index}",
            source_state_version=f"state-{index}",
            source_location="https://example.test/home",
            navigation_depth=0,
            candidates=(_close_candidate(f"close-{index}"),),
        )
    snapshot = _skip_all_pending(snapshot)

    observation = SurfaceObservation(
        capture_id="capture-embed",
        surface_kind=SurfaceKind.WEB,
        location="https://example.test/home",
        state_version="state-embed",
        observed_at=datetime.now(UTC),
    )
    outer = FrontierSnapshot(
        snapshot_id="outer-frontier",
        observation=observation,
        candidates=(),
    )

    embedded = _with_task_frontier(frontier=outer, snapshot=snapshot)

    payload = embedded.observation.metadata[H5_TASK_FRONTIER_METADATA_KEY]
    skipped_embedded = [
        entry for entry in payload["entries"] if entry["status"] == "skipped"
    ]
    assert len(skipped_embedded) == H5_RETAINED_SKIPPED_LIVE_LIMIT
    assert payload["retired_skip_counts"]


def test_compact_frontier_preserves_active_work_and_execution_facts() -> None:
    """完整探索会同时累积 executed 与 skipped，压缩不得减少后续探索或覆盖事实。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for index in range(60):
        candidate = CandidateAction(
            candidate_id=f"candidate-{index}",
            semantic_target=f"navigation-{index}",
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=90,
            navigation_depth=index % 4,
            expected_surface_kind=SurfaceKind.WEB,
            metadata={
                "expected_next_page_type": f"page-type-{index}",
                "target_keywords": [f"navigation-{index}", "business-entry"],
                "action_parameters": {
                    "target_mode": "dom_semantic_target",
                    "selector": f"section:nth-of-type({index + 1}) a[data-entry]",
                },
                "analysis_context": "页面候选语义与定位上下文" * 80,
            },
        )
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{index}",
            source_state_version=f"state-{index}",
            source_location=f"https://example.test/page/{index}",
            navigation_depth=index % 4,
            candidates=(candidate,),
        )
        entry = next(
            item
            for item in snapshot.entries
            if item.candidate.candidate_id == candidate.candidate_id
        )
        if index < 33:
            snapshot = snapshot.select_entry(entry.entry_id).mark_executed(entry.entry_id)
        elif index < 49:
            snapshot = snapshot.mark_pending_skipped(
                entry.entry_id,
                reason="same_business_surface",
                active_state_version=entry.source_state_version,
            )
        elif index == 49:
            snapshot = snapshot.select_entry(entry.entry_id)

    raw_size = len(
        json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert raw_size > H5_TASK_FRONTIER_TARGET_BYTES
    active_entry_ids = {
        entry.entry_id
        for entry in snapshot.entries
        if entry.status in {H5FrontierEntryStatus.PENDING, H5FrontierEntryStatus.SELECTED}
    }

    compacted = compact_h5_task_frontier(snapshot)

    compacted_size = len(
        json.dumps(
            compacted.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert compacted_size <= H5_TASK_FRONTIER_TARGET_BYTES
    assert {
        entry.entry_id
        for entry in compacted.entries
        if entry.status in {H5FrontierEntryStatus.PENDING, H5FrontierEntryStatus.SELECTED}
    } == active_entry_ids
    assert compacted.covered_page_count == snapshot.covered_page_count
    assert compacted.covered_page_types == snapshot.covered_page_types
    assert compacted.visited_state_ids == snapshot.visited_state_ids
    assert compacted.executed_candidate_ids == snapshot.executed_candidate_ids
    assert compacted.action_fingerprints == snapshot.action_fingerprints
    assert H5TaskFrontierSnapshot.from_dict(compacted.to_dict()) == compacted
    selected = next(
        entry for entry in compacted.entries if entry.status is H5FrontierEntryStatus.SELECTED
    )
    continued = compacted.mark_executed(selected.entry_id)
    assert continued.covered_page_count == snapshot.covered_page_count + 1
    assert selected.candidate.candidate_id in continued.executed_candidate_ids
    assert H5TaskFrontierSnapshot.from_dict(continued.to_dict()) == continued


def test_compacted_frontier_keeps_complete_exploration_work_within_protocol_limit() -> None:
    """写入前的 H5 Work 必须可被同一协议读取，不能留下延迟爆炸载荷。"""
    from services.api.app.application.h5_exploration_workflow import _with_task_frontier
    from services.api.app.domain.exploration.snapshots import FrontierSnapshot, SurfaceObservation

    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for index in range(70):
        snapshot = snapshot.merge(
            source_capture_id=f"capture-{index}",
            source_state_version=f"state-{index}",
            source_location=f"https://example.test/page/{index}",
            navigation_depth=index % 4,
            candidates=(
                CandidateAction(
                    candidate_id=f"candidate-{index}",
                    semantic_target=f"entry-{index}",
                    action_type=InteractionOperation.CLICK,
                    risk_level=InteractionRiskLevel.LOW,
                    exploration_value=90,
                    navigation_depth=index % 4,
                    expected_surface_kind=SurfaceKind.WEB,
                    metadata={
                        "expected_next_page_type": f"page-type-{index}",
                        "analysis_context": "动态页面候选上下文" * 90,
                    },
                ),
            ),
        )
        entry = next(
            item
            for item in snapshot.entries
            if item.candidate.candidate_id == f"candidate-{index}"
        )
        if index < 55:
            snapshot = snapshot.select_entry(entry.entry_id).mark_executed(entry.entry_id)

    now = datetime.now(UTC)
    frontier = FrontierSnapshot(
        snapshot_id="outer-frontier",
        observation=SurfaceObservation(
            capture_id="capture-current",
            surface_kind=SurfaceKind.WEB,
            location="https://example.test/home",
            state_version="state-current",
            observed_at=now,
            metadata={"runtime_context": "运行上下文" * 6_000},
        ),
        candidates=(),
    )
    frontier = _with_task_frontier(frontier=frontier, snapshot=snapshot)
    work = build_exploration_work(
        task_id="task-h5-capacity",
        session_id="session-h5-capacity",
        branch_id="branch-h5-capacity",
        objective="尽量完整探索 H5 业务页面",
        surface_kind=SurfaceKind.WEB,
        source_capture_id=frontier.observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("example.test",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=4,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=100,
            remaining_actions=15,
            max_depth=4,
            remaining_duration_ms=120_000,
        ),
        deadline=now + timedelta(minutes=5),
    )

    validate_protocol_payload(work.to_dict(), field_name="ExplorationWork")


def test_compact_frontier_recovers_legacy_oversized_active_lineages() -> None:
    """旧快照的活动候选谱系膨胀时，只压路径证明，不得丢候选。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-active",
        source_state_version="state-active",
        source_location="https://example.test/home",
        navigation_depth=1,
        candidates=tuple(
            CandidateAction(
                candidate_id=f"candidate-active-{index}",
                semantic_target=f"entry-active-{index}",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=90,
                navigation_depth=1,
                expected_surface_kind=SurfaceKind.WEB,
                metadata={"analysis_context": "候选上下文" * 80},
            )
            for index in range(30)
        ),
    )
    legacy_lineage = tuple(f"legacy-state-{index}" for index in range(260)) + (
        "state-active",
    )
    legacy_entries = tuple(
        replace(
            entry,
            lineage=legacy_lineage,
            # 根页直接子项的 root 与 parent 相同，是合法且常见的谱系。
            parent_state_version=legacy_lineage[0],
        )
        for entry in snapshot.entries
    )
    legacy = _new_snapshot(
        entries=legacy_entries,
        active_state_version=snapshot.active_state_version,
        visited_state_ids=snapshot.visited_state_ids,
        executed_candidate_ids=snapshot.executed_candidate_ids,
        covered_page_types=snapshot.covered_page_types,
        max_pages=snapshot.max_pages,
        sampling_policy_summary=snapshot.sampling_policy_summary,
        retired_semantic_keys=snapshot.retired_semantic_keys,
        retired_skip_counts=snapshot.retired_skip_counts,
        retired_visited_state_ids=snapshot.retired_visited_state_ids,
        retired_executed_candidate_ids=snapshot.retired_executed_candidate_ids,
        retired_action_fingerprints=snapshot.retired_action_fingerprints,
        retired_covered_page_count=snapshot.retired_covered_page_count,
        retired_covered_page_types=snapshot.retired_covered_page_types,
        retired_popup_attempt_counts=snapshot.retired_popup_attempt_counts,
    )
    assert len(
        json.dumps(
            legacy.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > H5_TASK_FRONTIER_TARGET_BYTES

    compacted = compact_h5_task_frontier(legacy)

    assert {entry.entry_id for entry in compacted.entries} == {
        entry.entry_id for entry in legacy.entries
    }
    assert all(entry.status is H5FrontierEntryStatus.PENDING for entry in compacted.entries)
    assert all(len(entry.lineage) <= 2 for entry in compacted.entries)
    assert len(
        json.dumps(
            compacted.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) <= H5_TASK_FRONTIER_TARGET_BYTES


def test_frontier_capacity_supports_full_h5_action_budget() -> None:
    """H5 的 80 步动作预算不能被过小的 Frontier 固定阈值提前截断。"""

    snapshot = H5TaskFrontierSnapshot.create(max_pages=100).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=1,
        candidates=tuple(
            CandidateAction(
                candidate_id=f"candidate-budget-{index}",
                semantic_target=f"internal:page_tab:nth-of-type({index + 1})",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=95,
                navigation_depth=1,
                expected_surface_kind=SurfaceKind.WEB,
                metadata={
                    "purpose": "探索当前页面现场识别出的页内栏目分支",
                    "target_keywords": [
                        "page_tab_navigation",
                        f"栏目-{index}",
                        "adaptive_page_tab",
                        "interaction_pattern:shadow",
                    ],
                    "source_page_type": "home",
                    "source_page_state": "stable",
                    "expected_next_page_type": "home",
                    "logical_target_required": True,
                    "source_state_fingerprint": "sha256:" + "a" * 64,
                    "source_identity_fingerprint": "sha256:" + "b" * 64,
                    "interaction_pattern_status": "shadow",
                    "navigation_knowledge_source": "contextual",
                    "navigation_knowledge_confidence": 1.0,
                    "action_parameters": {
                        "target_mode": "page_tab_navigation",
                        "navigation_index": index + 1,
                        "navigation_label": f"栏目-{index}",
                        "interaction_pattern_signature": "c" * 64,
                    },
                },
            )
            for index in range(80)
        ),
    )

    compacted = compact_h5_task_frontier(snapshot)

    assert len(compacted.entries) == 80
    assert compacted.pending_count == 80
    assert len(
        json.dumps(
            compacted.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ) <= H5_TASK_FRONTIER_TARGET_BYTES


def test_same_depth_navigation_replaces_sibling_lineage_instead_of_growing_chain() -> None:
    """同层栏目切换不能把每个新状态错误追加成下一代节点。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-root",
        source_state_version="state-root",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_candidate("root-entry"),),
    )
    parent_state = "state-root"
    for index in range(1, 31):
        state_version = f"state-tab-{index}"
        candidate_id = f"tab-entry-{index}"
        snapshot = snapshot.merge(
            source_capture_id=f"capture-tab-{index}",
            source_state_version=state_version,
            source_location="https://example.test/home",
            navigation_depth=1,
            parent_state_version=parent_state,
            candidates=(_candidate(candidate_id),),
        )
        current = next(
            entry
            for entry in snapshot.entries
            if entry.candidate.candidate_id == candidate_id
        )
        assert current.lineage == ("state-root", state_version)
        assert current.parent_state_version == "state-root"
        parent_state = state_version


def test_merge_tolerates_navigation_cycle_back_to_ancestor_state() -> None:
    """底栏往返会绕回谱系里的旧状态（A→B→A）；若直接追加谱系会撞唯一性校验，
    整个 Run 被打成 H5_EXPLORATION_TURN_FAILED（实测 51鉴片 R4/R5 两连败）。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(_candidate("to-product"),),
    )
    snapshot = snapshot.merge(
        source_capture_id="capture-product",
        source_state_version="state-product",
        source_location="https://example.test/product",
        navigation_depth=1,
        parent_state_version="state-home",
        candidates=(_candidate("to-detail"),),
    )

    cycled = snapshot.merge(
        source_capture_id="capture-home-again",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=2,
        parent_state_version="state-product",
        candidates=(_candidate("to-profile"),),
    )

    revisited = next(
        entry
        for entry in cycled.entries
        if entry.candidate.candidate_id == "to-profile"
    )
    assert revisited.lineage == ("state-product", "state-home")
    # entry 不变式：谱系必须同时含来源状态与父状态
    assert revisited.source_state_version in revisited.lineage
    assert revisited.parent_state_version in revisited.lineage


def test_merge_tolerates_self_parent_state() -> None:
    """同一状态内再签发候选（parent == source）不得因谱系重复炸掉整跑。"""
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-home",
        source_state_version="state-home",
        source_location="https://example.test/home",
        navigation_depth=0,
        parent_state_version="state-home",
        candidates=(_candidate("same-state"),),
    )

    assert snapshot.entries[0].lineage == ("state-home",)


def test_payment_horizontal_collection_is_selected_before_nested_return() -> None:
    horizontal = CandidateAction(
        candidate_id="payment-horizontal",
        semantic_target="浏览全部会员卡",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "source_page_type": "payment",
            "source_page_state": "stable",
            "target_keywords": ["payment_horizontal_collection", "read_only_collection"],
            "action_parameters": {
                "target_mode": "h5_payment_horizontal_collection",
                "horizontal_scroll_max_segments": 6,
                "split_scroll_screenshots": True,
                "source_page_type": "payment",
            },
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id="capture-payment",
        source_state_version="state-payment",
        source_location="https://m.example.test/vip",
        navigation_depth=1,
        candidates=(
            _nested_return_candidate("payment-back", source_page_type="payment"),
            horizontal,
        ),
    )

    selected = select_next_h5_entry(snapshot, active_state_version="state-payment")

    assert selected is not None
    assert selected.candidate.candidate_id == "payment-horizontal"
