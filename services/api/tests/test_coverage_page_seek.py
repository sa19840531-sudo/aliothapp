"""????????????????????????????"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace

import pytest

from services.api.app.application.app_capture_playbook import (
    resolve_app_capture_playbook,
)
from services.api.app.application.app_coverage_page_seek import (
    APP_COVERAGE_PAGE_SEEK_VERSION,
    _append_coverage_page_seek_candidate,
    _coverage_seek_required_page_types,
    _resolve_coverage_page_seek,
    align_visual_page_type_after_coverage_seek,
    missing_required_page_tab_boost,
    missing_required_page_tab_rank,
    uncovered_required_page_rank,
)
from services.api.app.application.app_screenshot_exploration import (
    _append_popup_close_corner_candidate,
    _append_startup_ad_skip_candidate,
    _append_unstable_page_reobserve_candidate,
    _filter_curriculum_frontier_candidates,
    _filter_unstable_page_navigation_candidates,
    _is_coverage_page_seek_override,
    _is_popup_blocking_analysis,
    _is_risky_promo_body_close_candidate,
    _retain_only_obstruction_recovery_candidates,
)
from services.api.app.application.app_script_curriculum import (
    build_app_curriculum_progress,
)
from services.api.app.application.surface_phase_explorer import (
    _candidate_matches_close_phase,
    _first_close_candidate,
    _is_startup_enter_or_skip_candidate,
)
from services.api.app.domain.exploration import (
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

_SIGNATURE = "sha256:" + ("ab" * 32)


def _visual_candidate(
    *,
    expected_page_type: str = "content_list",
    action_type: InteractionOperation = InteractionOperation.CLICK,
    risk_level: InteractionRiskLevel = InteractionRiskLevel.LOW,
    control_role: str | None = None,
    bounds: NormalizedBounds | None = None,
    capture_id: str = "capture-1",
    image_signature: str = _SIGNATURE,
    frontier_scope: str = "app",
    semantic_name: str = "??????????",
) -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=capture_id,
        source_image_signature=image_signature,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name=semantic_name,
        action_type=action_type,
        normalized_bounds=(
            bounds
            if bounds is not None
            else (
                NormalizedBounds(0.20, 0.20, 0.80, 0.34)
                if action_type in {InteractionOperation.CLICK, InteractionOperation.TAP}
                else None
            )
        ),
        confidence=0.91,
        risk_level=risk_level,
        purpose="????????????",
        control_role=control_role,
        expected_state_hint=expected_page_type,
        frontier_scope=frontier_scope,
    )


def _action(
    visual: AppVisualCandidate,
    *,
    expected_page_type: str | None = None,
    metadata: dict[str, object] | None = None,
    exploration_value: int = 82,
) -> CandidateAction:
    expected = expected_page_type or visual.expected_state_hint
    action_metadata: dict[str, object] = {
        "candidate_id": visual.candidate_id,
        "purpose": visual.purpose,
        "frontier_scope": visual.frontier_scope,
        "source_capture_id": visual.source_capture_id,
    }
    if isinstance(expected, str) and expected.strip():
        action_metadata.update(
            {
                "expected_state_hint": expected,
                "expected_next_page_type": expected,
            }
        )
    if visual.control_role is not None:
        action_metadata["control_role"] = visual.control_role
    action_metadata.update(metadata or {})
    return CandidateAction(
        candidate_id=visual.candidate_id,
        semantic_target=visual.semantic_name,
        action_type=visual.action_type,
        risk_level=visual.risk_level,
        exploration_value=exploration_value,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata=action_metadata,
    )


def _analysis(
    *,
    visual_page_type: str = "home",
    page_state: str = "stable",
    package_name: str = "com.example.unknown",
    candidates: tuple[AppVisualCandidate, ...] = (),
    surface_facts: dict[str, object] | None = None,
    capture_id: str = "capture-1",
    image_signature: str = _SIGNATURE,
) -> SimpleNamespace:
    return SimpleNamespace(
        capture_id=capture_id,
        screenshot_artifact_id="artifact-1",
        image_signature=image_signature,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name=package_name,
        activity=".MainActivity",
        page_state=page_state,
        visual_page_type=visual_page_type,
        surface_facts=surface_facts or {},
        overlay_signature=None,
        candidates=candidates,
        visual_summary="???",
        key_elements=(),
        provider_id="test-provider",
        model_id="test-model",
    )


def _coverage(*required: str, covered: tuple[str, ...] = ("home",)) -> dict[str, object]:
    return {
        "required_page_types": list(required),
        "covered_page_types": list(covered),
    }


def _append(
    *,
    candidates: tuple[CandidateAction, ...],
    analysis: SimpleNamespace,
    coverage_metadata: dict[str, object] | None,
) -> tuple[CandidateAction, ...]:
    return _append_coverage_page_seek_candidate(
        candidates=candidates,
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=(analysis.package_name,),
        coverage_metadata=coverage_metadata,
    )


def _navigation(count: int) -> tuple[dict[str, object], ...]:
    return tuple(
        {
            "label": f"tab-{index}",
            "page_type": None,
            "bounds": {
                "left": index * 1080 / count,
                "top": 1740,
                "right": (index + 1) * 1080 / count,
                "bottom": 1920,
            },
        }
        for index in range(count)
    )


def _post_publish_overlay_candidate(
    *,
    candidate_id: str,
    semantic_target: str,
    purpose: str,
    risk_level: InteractionRiskLevel = InteractionRiskLevel.MEDIUM,
) -> CandidateAction:
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=semantic_target,
        action_type=InteractionOperation.CLICK,
        risk_level=risk_level,
        exploration_value=82,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": purpose,
            "frontier_scope": "local",
            "expected_next_page_type": "post_publish",
        },
    )


def _post_list_type_chooser_analysis() -> SimpleNamespace:
    return SimpleNamespace(
        page_state="stable",
        visual_page_type="post_list",
        surface_facts={"popup_visible": True},
        overlay_signature="post-type-chooser",
    )


def _back_and_close_candidates() -> tuple[CandidateAction, CandidateAction]:
    back = CandidateAction(
        candidate_id="back",
        semantic_target="返回上一级",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=70,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "返回上一级"},
    )
    close = CandidateAction(
        candidate_id="close",
        semantic_target="关闭弹层",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=75,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "关闭弹层"},
    )
    return back, close


def test_missing_post_publish_overlay_entries_are_kept_before_back_and_close() -> None:
    """缺少 post_publish 时，类型选择层的安全业务入口应先于清障动作。"""

    from services.api.app.application.app_semantic_progress import (
        APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS,
        record_app_coverage_seek_target_attempt,
        should_suppress_redundant_app_candidate,
    )

    text_entry = _post_publish_overlay_candidate(
        candidate_id="post-publish-text",
        semantic_target="图文发帖",
        purpose="进入图文发帖",
    )
    video_entry = _post_publish_overlay_candidate(
        candidate_id="post-publish-video",
        semantic_target="视频发帖",
        purpose="进入视频发帖",
    )
    back, close = _back_and_close_candidates()

    result = _retain_only_obstruction_recovery_candidates(
        candidates=(back, close, text_entry, video_entry),
        analysis=_post_list_type_chooser_analysis(),
        coverage_metadata={
            "required_page_types": ["post_list", "post_publish"],
            "covered_page_types": ["post_list"],
        },
    )

    assert [item.candidate_id for item in result] == [
        "post-publish-text",
        "post-publish-video",
        "back",
        "close",
    ]
    assert [item.risk_level for item in result[:2]] == [
        InteractionRiskLevel.MEDIUM,
        InteractionRiskLevel.MEDIUM,
    ]
    assert all(item.metadata["coverage_page_seek"] is True for item in result[:2])
    assert all(item.exploration_value == 98 for item in result[:2])
    page_identity = "com.example.target|.mainactivity|post_list|name:community"
    attempts = record_app_coverage_seek_target_attempt(
        {},
        candidate=result[0],
        page_identity=page_identity,
        arrival_proven=False,
    )
    progress = {
        "business_page_identity": page_identity,
        APP_COVERAGE_SEEK_TARGET_ATTEMPT_COUNTS: attempts,
    }
    assert should_suppress_redundant_app_candidate(
        candidate=result[0],
        coverage_metadata=progress,
    )
    assert not should_suppress_redundant_app_candidate(
        candidate=result[1],
        coverage_metadata=progress,
    )


def test_covered_post_publish_overlay_entries_are_not_retained() -> None:
    """post_publish 已覆盖时，类型选择层不再重复进入业务分支。"""

    text_entry = _post_publish_overlay_candidate(
        candidate_id="post-publish-text",
        semantic_target="图文发帖",
        purpose="进入图文发帖",
    )
    video_entry = _post_publish_overlay_candidate(
        candidate_id="post-publish-video",
        semantic_target="视频发帖",
        purpose="进入视频发帖",
    )
    back, close = _back_and_close_candidates()

    result = _retain_only_obstruction_recovery_candidates(
        candidates=(text_entry, video_entry, back, close),
        analysis=_post_list_type_chooser_analysis(),
        coverage_metadata={
            "required_page_types": ["post_list", "post_publish"],
            "covered_page_types": ["post_list", "post_publish"],
        },
    )

    assert [item.candidate_id for item in result] == ["back", "close"]


def test_post_publish_submission_semantics_are_not_retained_from_overlay() -> None:
    """提交发布和确认发布属于副作用语义，不得由浮层入口放行。"""

    submit_entry = _post_publish_overlay_candidate(
        candidate_id="submit-publish",
        semantic_target="提交发布",
        purpose="提交发布",
    )
    confirm_entry = _post_publish_overlay_candidate(
        candidate_id="confirm-publish",
        semantic_target="确认发布",
        purpose="确认发布",
    )
    back, close = _back_and_close_candidates()

    result = _retain_only_obstruction_recovery_candidates(
        candidates=(submit_entry, confirm_entry, back, close),
        analysis=_post_list_type_chooser_analysis(),
        coverage_metadata={
            "required_page_types": ["post_list", "post_publish"],
            "covered_page_types": ["post_list"],
        },
    )

    assert [item.candidate_id for item in result] == ["back", "close"]


def test_coverage_scope_uses_only_current_task_metadata() -> None:
    assert _coverage_seek_required_page_types(
        {"required_page_types": ["Home", "content_list"]}
    ) == {"home", "content_list"}
    assert _coverage_seek_required_page_types(
        {"app_runtime_policy": {"required_page_types": ["profile"]}}
    ) == {"profile"}
    assert _coverage_seek_required_page_types({}) == set()
    assert (
        _coverage_seek_required_page_types(
            {
                "required_page_types": [],
                "app_runtime_policy": {"required_page_types": ["profile"]},
            }
        )
        == set()
    )


def test_page_rank_does_not_create_unknown_navigation_priority() -> None:
    assert missing_required_page_tab_rank("content_list") == 0
    assert missing_required_page_tab_boost("unknown_page") == 0
    assert (
        uncovered_required_page_rank(
            "unknown_page",
            required={"unknown_page"},
            covered=set(),
        )
        == 1
    )
    assert (
        uncovered_required_page_rank(
                "content_list",
            required={"content_list"},
            covered={"content_list"},
        )
        == 0
    )


@pytest.mark.parametrize("tab_count", (3, 5, 6, 7))
def test_no_visual_candidate_never_synthesizes_for_any_tab_count(tab_count: int) -> None:
    analysis = _analysis(
        package_name="com.example.unseen",
        surface_facts={
            "trusted_accessibility_controls": {
                "bottom_navigation": _navigation(tab_count),
            }
        },
    )
    result = _append(
        candidates=(),
        analysis=analysis,
        coverage_metadata=_coverage("home", "content_list", "video_list"),
    )
    assert result == ()


@pytest.mark.parametrize("tab_count", (3, 6, 7))
def test_real_candidate_promotion_is_independent_of_tab_count(tab_count: int) -> None:
    visual = _visual_candidate(expected_page_type="video_list")
    candidate = _action(visual)
    analysis = _analysis(
        package_name="com.example.unseen",
        candidates=(visual,),
        surface_facts={
            "trusted_accessibility_controls": {
                "bottom_navigation": _navigation(tab_count),
            }
        },
    )

    result = _append(
        candidates=(candidate,),
        analysis=analysis,
        coverage_metadata=_coverage("home", "video_list"),
    )

    assert [item.candidate_id for item in result] == [candidate.candidate_id]
    promoted = result[0]
    assert promoted.metadata["coverage_page_seek"] is True
    assert promoted.metadata["expected_next_page_type"] == "video_list"
    assert "coverage_page_seek_contract" not in promoted.metadata
    assert "normalized_bounds" not in promoted.metadata
    assert visual.normalized_bounds == NormalizedBounds(0.20, 0.20, 0.80, 0.34)
    assert promoted.exploration_value == 96


@pytest.mark.parametrize("page_state", ("unknown", "loading"))
def test_unstable_page_state_does_not_promote_business_candidate(page_state: str) -> None:
    visual = _visual_candidate(expected_page_type="content_list")
    candidate = _action(visual)
    result = _append(
        candidates=(candidate,),
        analysis=_analysis(
            page_state=page_state,
            visual_page_type="content_list",
            candidates=(visual,),
        ),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert all(item.metadata.get("coverage_page_seek") is not True for item in result)


@pytest.mark.parametrize(
    "facts",
    (
        {"startup_ad_visible": True},
        {"countdown_visible": True},
        {"topmost_ad_popup": {"is_ad": True}},
        {"dialog_visible": True},
        {"auth_gate_visible": True},
    ),
)
def test_obstruction_facts_block_promotion_despite_stable_label(facts: dict) -> None:
    visual = _visual_candidate()
    candidate = _action(visual)
    result = _append(
        candidates=(candidate,),
        analysis=_analysis(candidates=(visual,), surface_facts=facts),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert all(item.metadata.get("coverage_page_seek") is not True for item in result)


def test_failed_real_coverage_candidate_is_not_reentered() -> None:
    from services.api.app.application.app_semantic_progress import (
        APP_COVERAGE_SEEK_FAILURE_SCORES,
        record_app_coverage_seek_outcome,
    )

    visual = _visual_candidate(expected_page_type="content_list")
    candidate = _action(visual)
    analysis = _analysis(candidates=(visual,))
    first = _append(
        candidates=(candidate,),
        analysis=analysis,
        coverage_metadata=_coverage("home", "content_list"),
    )
    promoted = first[0]
    failure_scores = record_app_coverage_seek_outcome(
        {},
        candidate=promoted,
        page_identity="com.example.unknown|.MainActivity|home",
        action_succeeded=False,
        arrival_proven=False,
    )
    second = _append(
        candidates=(candidate,),
        analysis=analysis,
        coverage_metadata={
            **_coverage("home", "content_list"),
            "business_page_identity": "com.example.unknown|.MainActivity|home",
            APP_COVERAGE_SEEK_FAILURE_SCORES: failure_scores,
        },
    )
    assert second == ()


def test_promotion_requires_current_source_candidate() -> None:
    visual = _visual_candidate(expected_page_type="content_list")
    unrelated = CandidateAction(
        candidate_id="candidate-from-another-capture",
        semantic_target="??????",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=82,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "expected_next_page_type": "content_list",
            "source_capture_id": "old-capture",
        },
    )
    result = _append(
        candidates=(unrelated,),
        analysis=_analysis(candidates=(visual,)),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert result == (unrelated,)
    assert result[0].metadata.get("coverage_page_seek") is not True


def test_promotion_rejects_stale_candidate_metadata() -> None:
    visual = _visual_candidate(expected_page_type="content_list")
    candidate = _action(visual, metadata={"source_capture_id": "old-capture"})
    result = _append(
        candidates=(candidate,),
        analysis=_analysis(candidates=(visual,)),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert result == (candidate,)
    assert result[0].metadata.get("coverage_page_seek") is not True


def test_medium_risk_visual_candidate_is_not_promoted() -> None:
    visual = _visual_candidate(
        expected_page_type="profile",
        risk_level=InteractionRiskLevel.MEDIUM,
    )
    candidate = _action(visual)
    result = _append(
        candidates=(candidate,),
        analysis=_analysis(candidates=(visual,)),
        coverage_metadata=_coverage("home", "profile"),
    )
    assert result == (candidate,)
    assert result[0].metadata.get("coverage_page_seek") is not True


@pytest.mark.parametrize("control_role", ("dismiss", "skip"))
def test_control_role_is_never_promoted_to_coverage_seek(control_role: str) -> None:
    visual = _visual_candidate(
        expected_page_type="content_list",
        control_role=control_role,
    )
    candidate = _action(visual)
    result = _append(
        candidates=(candidate,),
        analysis=_analysis(candidates=(visual,)),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert result == (candidate,)
    assert result[0].metadata.get("coverage_page_seek") is not True


def test_existing_control_role_coverage_marker_is_removed() -> None:
    visual = _visual_candidate(expected_page_type="content_list")
    candidate = _action(
        visual,
        metadata={"control_role": "dismiss", "coverage_page_seek": True},
    )
    result = _append(
        candidates=(candidate,),
        analysis=_analysis(candidates=(visual,)),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert result[0].metadata.get("coverage_page_seek") is not True
    assert result[0].metadata.get("control_role") == "dismiss"


def test_old_synthetic_candidate_is_dropped_before_any_execution_path() -> None:
    old = CandidateAction(
        candidate_id="old-synthetic",
        semantic_target="??????",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "coverage_page_seek_contract": {
                "protocol_version": APP_COVERAGE_PAGE_SEEK_VERSION,
                "normalized_bounds": {
                    "left": 0.25,
                    "top": 0.91,
                    "right": 0.40,
                    "bottom": 0.99,
                },
            },
        },
    )
    result = _append(
        candidates=(old,),
        analysis=_analysis(candidates=()),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert result == ()


def test_old_resolve_contract_fails_closed_for_package_and_navigation_facts() -> None:
    result = _resolve_coverage_page_seek(
        analysis=_analysis(
            package_name="com.example.known-or-unknown",
            surface_facts={
                "trusted_accessibility_controls": {
                    "bottom_navigation": _navigation(3),
                }
            },
        ),
        missing=["content_list", "video_list"],
        covered=["home"],
    )
    assert result is None


def test_preexisting_noncurrent_coverage_marker_is_cleared() -> None:
    stale = CandidateAction(
        candidate_id="stale-coverage",
        semantic_target="????",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=82,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "content_list",
        },
    )
    result = _append(
        candidates=(stale,),
        analysis=_analysis(candidates=()),
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert result[0].metadata.get("coverage_page_seek") is not True


def test_nested_surface_promotes_only_real_current_back_candidate() -> None:
    visual = _visual_candidate(
        action_type=InteractionOperation.BACK,
        expected_page_type="back",
        bounds=None,
        frontier_scope="local",
        semantic_name="?????",
    )
    back = _action(visual)
    analysis = _analysis(
        visual_page_type="content_list",
        candidates=(visual,),
        surface_facts={"visual_page_name": {"page_name": "????", "source": "top_bar"}},
    )
    result = _append(
        candidates=(back,),
        analysis=analysis,
        coverage_metadata=_coverage("home", "content_list", "video_list"),
    )
    assert result[0].candidate_id == back.candidate_id
    assert result[0].metadata["coverage_page_seek"] is True
    assert result[0].metadata["coverage_parent_restore"] is True
    assert "coverage_page_seek_contract" not in result[0].metadata
    assert "normalized_bounds" not in result[0].metadata


def test_loading_list_does_not_promote_unstable_detail_candidate() -> None:
    visual = _visual_candidate(expected_page_type="content_detail")
    detail = _action(visual)
    observe = CandidateAction(
        candidate_id="observe-loading",
        semantic_target="??????",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "????"},
    )
    result = _append(
        candidates=(detail, observe),
        analysis=_analysis(
            visual_page_type="content_list",
            page_state="loading",
            candidates=(visual,),
        ),
        coverage_metadata=_coverage("home", "content_list", "content_detail"),
    )
    assert [item.candidate_id for item in result] == [observe.candidate_id]


def test_overlay_blocks_promotion_even_when_real_candidate_exists() -> None:
    visual = _visual_candidate(expected_page_type="content_list")
    candidate = _action(visual)
    analysis = _analysis(
        candidates=(visual,),
        surface_facts={"popup_visible": True},
    )
    result = _append(
        candidates=(candidate,),
        analysis=analysis,
        coverage_metadata=_coverage("home", "content_list"),
    )
    assert result == (candidate,)
    assert result[0].metadata.get("coverage_page_seek") is not True


def test_coverage_seek_survives_curriculum_filter() -> None:
    playbook = resolve_app_capture_playbook(
        apk_filename="unknown.apk",
        package_name="com.example.target",
    )
    progress = build_app_curriculum_progress(playbook)
    visual = _visual_candidate(
        expected_page_type="profile",
        semantic_name="????????????",
    )
    seek = _action(
        visual,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "profile",
        },
    )
    analysis = _analysis(candidates=(visual,))

    assert _is_coverage_page_seek_override(candidate=seek, analysis=analysis) is True
    filtered = _filter_curriculum_frontier_candidates(
        candidates=(seek,),
        analysis=analysis,
        coverage_metadata={
            "app_curriculum_progress": progress.to_dict(),
            "covered_page_types": ["home", "startup_ad"],
        },
    )
    assert any(item.metadata.get("coverage_page_seek") is True for item in filtered)


def test_coverage_seek_prepends_ahead_of_curriculum_matching() -> None:
    playbook = resolve_app_capture_playbook(
        apk_filename="hlapp.apk",
        package_name="com.edxuf.euloop",
    )
    progress = build_app_curriculum_progress(playbook)
    visual = _visual_candidate(
        expected_page_type="content_list",
        semantic_name="????????????",
    )
    seek = _action(
        visual,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "content_list",
        },
        exploration_value=98,
    )
    matching = CandidateAction(
        candidate_id="scroll-1",
        semantic_target="????",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "???????", "expected_state_hint": "scroll"},
    )
    filtered = _filter_curriculum_frontier_candidates(
        candidates=(matching, seek),
        analysis=_analysis(candidates=(visual,)),
        coverage_metadata={
            "app_curriculum_progress": progress.to_dict(),
            "covered_page_types": ["home"],
            "required_page_types": ["home", "content_list"],
        },
    )
    assert filtered
    assert filtered[0].metadata.get("coverage_page_seek") is True


def test_startup_ad_skip_fallback_uses_relative_region_when_empty() -> None:
    """开屏无障碍树时仍用右上相对区签发进入/跳过，避免干等观察。"""

    analysis = SimpleNamespace(
        capture_id="cap-startup",
        image_signature="sha256:" + ("ab" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
        page_state="startup_ad",
        visual_page_type="startup_ad",
        package_name="com.example.target",
        surface_facts={},
    )
    out = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert len(out) == 1
    assert out[0].metadata.get("startup_ad_skip_fallback") is True
    assert out[0].metadata.get("expected_state_hint") == "tap_skip"
    assert "进入" in out[0].semantic_target or "跳过" in out[0].semantic_target
    contract = out[0].metadata.get("startup_ad_skip_contract") or {}
    assert contract.get("used_default_fixed_bounds") is True


def test_startup_skip_synthesized_on_depth0_unknown_fullscreen_promo() -> None:
    """Vision 把全屏开屏标 unknown 时，根帧仍须合成右上「进入」。"""

    analysis = SimpleNamespace(
        capture_id="cap-unknown-promo",
        image_signature="sha256:" + ("ef" * 32),
        width=1440,
        height=3040,
        orientation="portrait",
        page_state="unknown",
        visual_page_type=None,
        visual_summary="全屏赌博推广广告，无业务底栏与关闭按钮",
        key_elements=("PG娱进入", "注册送18", "V1.0.2"),
        package_name="dark.NVYCy.AmBPu",
        overlay_signature=None,
        surface_facts={},
    )
    out = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert len(out) == 1
    assert out[0].metadata.get("control_role") == "skip"
    assert "开屏进入" in out[0].semantic_target


def test_startup_skip_synthesized_on_overlay_fullscreen_with_enter() -> None:
    """Vision 把开屏标 overlay 时仍须合成进入，禁止误走 cloud_back 退桌面。"""

    analysis = SimpleNamespace(
        capture_id="cap-overlay-promo",
        image_signature="sha256:" + ("ab" * 32),
        width=1440,
        height=3040,
        orientation="portrait",
        page_state="overlay",
        visual_page_type=None,
        visual_summary="全屏PG娱乐博彩广告遮挡，未见关闭",
        key_elements=("PG娱乐", "进入", "玩PG赢大奖", "注册送1888"),
        package_name="dark.NVYCy.AmBPu",
        overlay_signature="fullscreen_promo",
        surface_facts={"popup_visible": True},
    )
    out = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert len(out) == 1
    assert out[0].metadata.get("control_role") == "skip"
    assert "开屏进入" in out[0].semantic_target


def test_startup_ad_skip_fallback_on_provider_degraded_empty_frame() -> None:
    """视觉降级空帧（常见首屏）也要签发右上进入/跳过。"""

    analysis = SimpleNamespace(
        capture_id="cap-degraded",
        image_signature="sha256:" + ("cd" * 32),
        width=1440,
        height=3040,
        orientation="portrait",
        page_state="unknown",
        visual_page_type=None,
        package_name="dark.NVYCy.AmBPu",
        surface_facts={
            "visual_provider_quality": {
                "protocol_version": "app-visual-provider-quality.v1",
                "status": "degraded",
                "error_code": "provider_parse_failed",
            }
        },
    )
    out = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert len(out) == 1
    assert out[0].action_type.value == "click"
    assert out[0].metadata.get("control_role") == "skip"


def _degraded_empty_analysis(*, package_name: str = "dark.NVYCy.AmBPu") -> SimpleNamespace:
    return SimpleNamespace(
        capture_id="cap-degraded",
        image_signature="sha256:" + ("cd" * 32),
        width=1440,
        height=3040,
        orientation="portrait",
        page_state="unknown",
        visual_page_type=None,
        visual_summary="",
        key_elements=(),
        package_name=package_name,
        overlay_signature=None,
        candidates=(),
        surface_facts={
            "visual_provider_quality": {
                "protocol_version": "app-visual-provider-quality.v1",
                "status": "degraded",
                "error_code": "provider_parse_failed",
            }
        },
    )


def test_startup_ad_skip_not_synthesized_on_degraded_empty_deeper_than_root() -> None:
    """中途降级空帧不得误点右上，仅 depth0 合成开屏进入。"""

    out = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=_degraded_empty_analysis(),
        navigation_depth=1,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert out == ()


def test_startup_ad_is_not_popup_blocking_for_corner_x() -> None:
    """开屏不得被当成弹窗挡路，避免合成关闭弹层下方X。"""

    analysis = _analysis(
        page_state="startup_ad",
        visual_page_type="startup_ad",
        package_name="dark.NVYCy.AmBPu",
    )
    assert _is_popup_blocking_analysis(analysis) is False
    out = _append_popup_close_corner_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert out == ()
    assert not any(
        "关闭弹层" in item.semantic_target for item in out
    )


def test_popup_watcher_cascade_skips_corner_x_without_geometry() -> None:
    """watcher_cascade：无像素／受信关闭框时不盲合成角落 X。"""

    analysis = _analysis(
        page_state="popup",
        visual_page_type="home",
        package_name="dark.NVYCy.AmBPu",
        surface_facts={"popup_visible": True, "dialog_kind": "ad_popup"},
    )
    assert _is_popup_blocking_analysis(analysis) is True
    out = _append_popup_close_corner_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=1,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert out == ()


def test_popup_watcher_cascade_keeps_vision_bottom_x_candidate() -> None:
    """watcher_cascade 保留 Vision 几何关层 ×（B）；不再按纯 cloud_back 整池剥掉。"""

    vision_x = CandidateAction(
        candidate_id="vision-bottom-x",
        semantic_target="关闭弹层下方X",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "expected_state_hint": "close_popup_x",
            "purpose": "点弹层底部圆形关闭X",
            "source_capture_id": "capture-1",
        },
    )
    analysis = _analysis(
        page_state="popup",
        visual_page_type="home",
        package_name="dark.NVYCy.AmBPu",
        surface_facts={"popup_visible": True, "dialog_kind": "ad_popup"},
    )
    out = _append_popup_close_corner_candidate(
        candidates=(vision_x,),
        analysis=analysis,
        navigation_depth=1,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert any(item.candidate_id == "vision-bottom-x" for item in out)


def test_startup_phase_prefers_enter_skip_over_popup_bottom_x() -> None:
    """startup_ad_countdown 不得选 ui_tree 下方X；应选开屏进入。"""

    skip = CandidateAction(
        candidate_id="startup-enter",
        semantic_target="开屏进入：进入/跳过",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "skip",
            "expected_state_hint": "tap_skip",
            "startup_ad_skip_fallback": True,
            "purpose": "按右上相对区点击开屏进入/跳过",
            "source_capture_id": "capture-1",
        },
    )
    bottom_x = CandidateAction(
        candidate_id="popup-bottom-x",
        semantic_target="关闭弹层下方X",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "control_role": "dismiss",
            "expected_state_hint": "close_popup_x",
            "popup_close_corner_fallback": True,
            "popup_close_bottom_x_fallback": True,
            "purpose": "点弹层下方关闭控件（ui_tree:bottom_banner_x）",
            "source_capture_id": "capture-1",
        },
    )
    assert _is_startup_enter_or_skip_candidate(skip) is True
    assert (
        _candidate_matches_close_phase(
            bottom_x,
            phase_key="startup_ad_countdown",
            page_type="startup_ad",
        )
        is False
    )
    assert (
        _candidate_matches_close_phase(
            skip,
            phase_key="startup_ad_countdown",
            page_type="startup_ad",
        )
        is True
    )

    class _Obs:
        page_type = "startup_ad"
        page_state = "startup_ad"
        metadata: dict = {}
        overlay_signature = None

    class _Frontier:
        candidates = (bottom_x, skip)
        observation = _Obs()

    class _Work:
        frontier = _Frontier()
        source_capture_id = "capture-1"
        surface_kind = SurfaceKind.ANDROID

    chosen = _first_close_candidate(_Work(), phase_key="startup_ad_countdown")
    assert chosen is not None
    assert chosen.candidate_id == "startup-enter"


def test_unstable_empty_page_keeps_startup_semantic_skip_click() -> None:
    """降级空页不得把进入/跳过滤成只剩 OBSERVE（21:16 首屏卡住根因）。"""

    analysis = _degraded_empty_analysis()
    skip = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )[0]
    observe = CandidateAction(
        candidate_id="observe-unstable",
        semantic_target="不稳定页零触控观察：第1帧",
        action_type=InteractionOperation.OBSERVE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "等待视觉页型稳定，不执行任何触控"},
    )
    filtered = _filter_unstable_page_navigation_candidates(
        candidates=(skip, observe),
        analysis=analysis,
    )
    assert any(
        item.metadata.get("startup_ad_skip_fallback") is True for item in filtered
    )
    assert any(item.action_type is InteractionOperation.CLICK for item in filtered)


def test_degraded_empty_skips_unstable_reobserve_at_depth0() -> None:
    """depth0 降级空帧不补 OBSERVE，避免抢占进入按钮。"""

    out = _append_unstable_page_reobserve_candidate(
        candidates=(),
        analysis=_degraded_empty_analysis(),
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        coverage_metadata={"dynamic_unstable_page_reobserve_count": 0},
    )
    assert out == ()


def test_curriculum_keeps_startup_skip_on_provider_degraded_empty() -> None:
    """课纲过滤在视觉降级空帧时仍放行合成开屏进入。"""

    analysis = _degraded_empty_analysis()
    skip = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )[0]
    playbook = resolve_app_capture_playbook(
        apk_filename="unknown.apk",
        package_name="dark.NVYCy.AmBPu",
    )
    progress = build_app_curriculum_progress(playbook)
    filtered = _filter_curriculum_frontier_candidates(
        candidates=(skip,),
        analysis=analysis,
        coverage_metadata={
            "app_curriculum_progress": progress.to_dict(),
            "covered_page_types": [],
            "required_page_types": ["home", "content_list"],
        },
    )
    assert filtered
    assert filtered[0].metadata.get("startup_ad_skip_fallback") is True


def test_startup_ad_skip_keeps_current_visual_skip_candidate() -> None:
    visual = _visual_candidate(
        expected_page_type="tap_skip",
        control_role="skip",
        semantic_name="????????????",
    )
    candidate = _action(visual)
    out = _append_startup_ad_skip_candidate(
        candidates=(candidate,),
        analysis=_analysis(
            page_state="startup_ad",
            visual_page_type="startup_ad",
            candidates=(visual,),
        ),
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert out == (candidate,)


def test_startup_ad_skip_fallback_uses_trusted_accessibility_fact() -> None:
    analysis = SimpleNamespace(
        capture_id="cap-startup",
        image_signature="sha256:" + ("ab" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
        page_state="startup_ad",
        visual_page_type="startup_ad",
        package_name="com.example.target",
        candidates=(),
        surface_facts={
            "trusted_accessibility_skip": {
                "artifact_id": "ui-tree-1",
                "package_name": "com.example.target",
                "label": "进入",
                "normalized_bounds": {
                    "left": 0.80,
                    "top": 0.05,
                    "right": 0.95,
                    "bottom": 0.10,
                },
            }
        },
    )
    out = _append_startup_ad_skip_candidate(
        candidates=(),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert len(out) == 1
    assert out[0].metadata.get("startup_ad_skip_fallback") is True
    assert out[0].metadata.get("expected_state_hint") == "tap_skip"
    assert "跳过开屏" in out[0].semantic_target


@pytest.mark.parametrize(
    ("close_bounds",),
    (
        (None,),
        (
            {"left": 486, "top": 1382.4, "right": 594, "bottom": 1497.6},
        ),
    ),
)
def test_popup_watcher_cascade_drops_risky_promo_and_may_synthesize_corner_x(
    close_bounds: dict[str, float] | None,
) -> None:
    """watcher_cascade：去掉危险广告本体；有受信关闭框时可合成角落 X（B）。"""

    risky = CandidateAction(
        candidate_id="vision-promo",
        semantic_target="close_promo_popup",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "purpose": "关闭居中成人推广弹层以恢复首页",
            "frontier_scope": "local",
        },
    )
    assert _is_risky_promo_body_close_candidate(risky) is True
    analysis = SimpleNamespace(
        capture_id="cap-popup",
        image_signature="sha256:" + ("cd" * 32),
        width=1080,
        height=1920,
        orientation="portrait",
        page_state="popup",
        visual_page_type="home",
        package_name="dark.NVYCy.AmBPu",
        overlay_signature="overlay:1",
        candidates=(),
        surface_facts={
            "popup_visible": True,
            **(
                {"trusted_accessibility_controls": {"overlay_close": {"bounds": close_bounds}}}
                if close_bounds is not None
                else {}
            ),
        },
    )
    out = _append_popup_close_corner_candidate(
        candidates=(risky,),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
    )
    assert all(item.candidate_id != "vision-promo" for item in out)
    assert all(not _is_risky_promo_body_close_candidate(item) for item in out)
    if close_bounds is not None:
        assert any(item.metadata.get("popup_close_corner_fallback") is True for item in out)


_ALIGN_SIGNATURE = "sha256:" + ("ef" * 32)


def _visual_analysis(
    *,
    visual_page_type: str,
    page_state: str = "stable",
    surface_facts: dict[str, object] | None = None,
):
    from services.api.app.domain.exploration import AppVisualAnalysis

    return AppVisualAnalysis.build(
        capture_id="capture-align",
        screenshot_artifact_id="artifact-align",
        image_signature=_ALIGN_SIGNATURE,
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="uk.frtju.nnxsaf",
        activity=".MainActivity",
        visual_page_type=visual_page_type,
        visual_summary="测试页",
        key_elements=(),
        candidates=(),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state=page_state,
        surface_facts=surface_facts or {},
    )


def _coverage_seek_action(*, semantic: str, expected: str) -> CandidateAction:
    return CandidateAction(
        candidate_id="seek-align",
        semantic_target=semantic,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": expected,
            "purpose": f"覆盖 {expected}",
        },
    )


def test_tab_seek_name_does_not_override_current_home_evidence() -> None:
    analysis = _visual_analysis(visual_page_type="home")
    action = _coverage_seek_action(semantic="覆盖寻页：底栏精选", expected="content_list")
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=True,
    )
    assert aligned.visual_page_type == "home"


def test_align_does_not_force_detail_when_vision_stays_list() -> None:
    analysis = _visual_analysis(visual_page_type="video_list")
    action = _coverage_seek_action(semantic="覆盖寻页：视频卡片", expected="video_detail")
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=True,
    )
    assert aligned.visual_page_type == "video_list"


def test_nested_title_does_not_prove_detail_type_after_card_seek() -> None:
    analysis = _visual_analysis(
        visual_page_type="content_list",
        surface_facts={"visual_page_name": {"page_name": "文章详情", "source": "top_bar"}},
    )
    action = _coverage_seek_action(
        semantic="覆盖寻页：内容卡片",
        expected="content_detail",
    )
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=True,
    )
    assert aligned.visual_page_type == "content_list"


def test_align_remaps_content_detail_from_video_list_to_video_detail() -> None:
    analysis = _visual_analysis(visual_page_type="content_detail")
    action = CandidateAction(
        candidate_id="card-1",
        semantic_target="视频卡片",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "打开视频"},
    )
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=True,
        source_page_type="video_list",
    )
    assert aligned.visual_page_type == "video_detail"


def test_align_skips_when_state_unchanged() -> None:
    analysis = _visual_analysis(visual_page_type="home")
    action = _coverage_seek_action(semantic="覆盖寻页：底栏精选", expected="content_list")
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=False,
    )
    assert aligned.visual_page_type == "home"


def test_align_does_not_force_bottom_tab_on_nested_top_bar_page() -> None:
    """嵌套页没有根底栏时，截图变化不能强行对齐成根 Tab。"""

    analysis = _visual_analysis(
        visual_page_type="content_list",
        surface_facts={
            "visual_page_name": {
                "protocol_version": "app-visual-page-name.v1",
                "page_name": "我的书架",
                "source": "top_bar",
                "confidence": 0.96,
            }
        },
    )
    action = _coverage_seek_action(semantic="覆盖寻页：底栏朋友圈", expected="post_list")
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=True,
    )
    assert aligned.visual_page_type == "content_list"


def test_alignment_uses_trusted_current_page_fact_without_geometry() -> None:
    analysis = _analysis(
        surface_facts={
            "trusted_accessibility_controls": {
                "page_type": "content_list",
            }
        }
    )
    action = CandidateAction(
        candidate_id="real-seek",
        semantic_target="底栏真实入口",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=96,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "content_list",
        },
    )
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=True,
    )
    assert aligned.visual_page_type == "content_list"


def test_alignment_rejects_old_synthetic_contract() -> None:
    analysis = _analysis()
    action = CandidateAction(
        candidate_id="old-seek",
        semantic_target="旧版合成寻页",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "expected_next_page_type": "content_list",
            "coverage_page_seek_contract": {
                "protocol_version": APP_COVERAGE_PAGE_SEEK_VERSION,
                "normalized_bounds": {"left": 0.1, "top": 0.9, "right": 0.2, "bottom": 1.0},
            },
        },
    )
    aligned = align_visual_page_type_after_coverage_seek(
        analysis,
        completed_action=action,
        state_changed=True,
    )
    assert aligned is analysis


def test_home_category_grid_seek_promotes_mid_page_icons_before_feed_and_bottom_nav() -> None:
    """稳定首页必须把中部宫格／分栏目提到推荐卡与底栏之前。"""

    from services.api.app.application.app_coverage_page_seek import (
        _append_home_category_grid_seek_candidate,
    )

    grid = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="宫格_视频",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.08, 0.28, 0.24, 0.40),
        confidence=0.93,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="打开内容入口宫格",
        frontier_scope="page",
    )
    feed = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="推荐视频卡",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.05, 0.45, 0.95, 0.68),
        confidence=0.92,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开推荐视频",
        expected_state_hint="content_detail",
        frontier_scope="page",
    )
    bottom = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏「交友」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.35, 0.90, 0.50, 0.98),
        confidence=0.94,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开底部导航交友",
        expected_state_hint="profile",
        frontier_scope="app",
    )
    analysis = _analysis(
        visual_page_type="home",
        package_name="com.example.target",
        candidates=(grid, feed, bottom),
    )
    actions = (
        _action(feed, exploration_value=90),
        _action(bottom, expected_page_type="profile", exploration_value=88),
        CandidateAction(
            candidate_id=grid.candidate_id,
            semantic_target=grid.semantic_name,
            action_type=grid.action_type,
            risk_level=grid.risk_level,
            exploration_value=80,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={
                "candidate_id": grid.candidate_id,
                "purpose": grid.purpose,
                "frontier_scope": "page",
                "source_capture_id": grid.source_capture_id,
            },
        ),
        CandidateAction(
            candidate_id="scroll",
            semantic_target="向下滑动",
            action_type=InteractionOperation.SCROLL,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=70,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={"purpose": "浏览更多推荐", "frontier_scope": "local"},
        ),
    )
    result = _append_home_category_grid_seek_candidate(
        candidates=actions,
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
        coverage_metadata=_coverage("home", "profile", "content_detail"),
    )
    assert result[0].candidate_id == grid.candidate_id
    assert result[0].metadata.get("home_category_grid_seek") is True
    assert result[0].metadata.get("coverage_page_seek") is True
    assert result[0].metadata.get("require_screenshot_evidence") is True
    assert result[0].metadata.get("home_category_grid_evidence") == "click_and_screenshot"
    assert "截图留证" in str(result[0].metadata.get("purpose") or "")
    assert result[0].exploration_value >= 99
    assert isinstance(result[0].metadata.get("normalized_bounds"), Mapping)
    assert all(item.metadata.get("home_category_grid_seek") is not True for item in result[1:])


def test_home_category_grid_seek_orders_left_to_right_for_click_sweep() -> None:
    """同一帧多个分栏目按上→下、左→右轮询，保证都会被点到并截图。"""

    from services.api.app.application.app_coverage_page_seek import (
        _append_home_category_grid_seek_candidate,
    )

    right = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="宫格_交友",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.55, 0.28, 0.72, 0.40),
        confidence=0.93,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="打开网格入口",
        frontier_scope="page",
    )
    left = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="宫格_视频",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.08, 0.28, 0.24, 0.40),
        confidence=0.93,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="打开网格入口",
        frontier_scope="page",
    )
    analysis = _analysis(
        visual_page_type="home",
        package_name="com.example.target",
        candidates=(right, left),
    )

    def _grid_action(visual: AppVisualCandidate) -> CandidateAction:
        return CandidateAction(
            candidate_id=visual.candidate_id,
            semantic_target=visual.semantic_name,
            action_type=visual.action_type,
            risk_level=visual.risk_level,
            exploration_value=80,
            navigation_depth=0,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={
                "candidate_id": visual.candidate_id,
                "purpose": visual.purpose,
                "frontier_scope": "page",
                "source_capture_id": visual.source_capture_id,
            },
        )

    result = _append_home_category_grid_seek_candidate(
        candidates=(_grid_action(right), _grid_action(left)),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
        coverage_metadata=_coverage("home"),
    )
    assert [item.candidate_id for item in result[:2]] == [
        left.candidate_id,
        right.candidate_id,
    ]
    assert all(
        item.metadata.get("home_category_grid_evidence") == "click_and_screenshot"
        for item in result[:2]
    )


def test_home_category_grid_seek_survives_curriculum_same_page_scroll() -> None:
    """课纲同页滑动不得挤掉首页中部分栏目寻页。"""

    from services.api.app.application.app_coverage_page_seek import (
        _append_home_category_grid_seek_candidate,
    )

    grid = AppVisualCandidate.build(
        source_capture_id="capture-1",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="入口_全国上门",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.55, 0.30, 0.72, 0.42),
        confidence=0.91,
        risk_level=InteractionRiskLevel.MEDIUM,
        purpose="打开内容入口",
        frontier_scope="page",
    )
    analysis = _analysis(
        visual_page_type="video_list",
        package_name="com.example.target",
        candidates=(grid,),
    )
    scroll = CandidateAction(
        candidate_id="scroll",
        semantic_target="向下滑动浏览",
        action_type=InteractionOperation.SCROLL,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=85,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"purpose": "同页滑动", "frontier_scope": "local"},
    )
    grid_action = CandidateAction(
        candidate_id=grid.candidate_id,
        semantic_target=grid.semantic_name,
        action_type=grid.action_type,
        risk_level=grid.risk_level,
        exploration_value=75,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "candidate_id": grid.candidate_id,
            "purpose": grid.purpose,
            "frontier_scope": "page",
            "source_capture_id": grid.source_capture_id,
        },
    )
    promoted = _append_home_category_grid_seek_candidate(
        candidates=(scroll, grid_action),
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
        coverage_metadata=_coverage("home", "video_list"),
    )
    assert promoted[0].metadata.get("home_category_grid_seek") is True
    playbook = resolve_app_capture_playbook(
        apk_filename="unknown.apk",
        package_name="com.example.target",
    )
    progress = build_app_curriculum_progress(playbook)
    filtered = _filter_curriculum_frontier_candidates(
        candidates=promoted,
        analysis=analysis,
        coverage_metadata={
            **_coverage("home", "video_list"),
            "app_curriculum_progress": progress.to_dict(),
        },
    )
    assert filtered[0].metadata.get("home_category_grid_seek") is True

def test_video_list_promotes_all_current_bottom_tabs_without_page_type_guess() -> None:
    """没有当前帧 selected 事实时，不按页型猜测并跳过任何真实底栏入口。"""

    from services.api.app.application.app_coverage_page_seek import (
        _append_bottom_navigation_tab_seek_candidate,
    )

    feed = AppVisualCandidate.build(
        source_capture_id="capture-vl",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="?????",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.05, 0.40, 0.95, 0.70),
        confidence=0.93,
        risk_level=InteractionRiskLevel.LOW,
        purpose="??????",
        expected_state_hint="video_detail",
        frontier_scope="page",
    )
    selected = AppVisualCandidate.build(
        source_capture_id="capture-vl",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="??????",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.02, 0.90, 0.18, 0.98),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="????????",
        expected_state_hint="video_list",
        frontier_scope="app",
    )
    dongtai = AppVisualCandidate.build(
        source_capture_id="capture-vl",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="??????",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.22, 0.90, 0.38, 0.98),
        confidence=0.94,
        risk_level=InteractionRiskLevel.LOW,
        purpose="????????",
        expected_state_hint="post_list",
        frontier_scope="app",
    )
    wode = AppVisualCandidate.build(
        source_capture_id="capture-vl",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="??????",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.82, 0.90, 0.98, 0.98),
        confidence=0.94,
        risk_level=InteractionRiskLevel.LOW,
        purpose="????????",
        expected_state_hint="profile",
        frontier_scope="app",
    )
    analysis = _analysis(
        visual_page_type="video_list",
        package_name="uerma.zqeluk.cwsxx",
        candidates=(feed, selected, dongtai, wode),
        capture_id="capture-vl",
    )
    actions = (
        _action(feed, exploration_value=95),
        _action(selected, expected_page_type="video_list", exploration_value=80),
        _action(wode, expected_page_type="profile", exploration_value=80),
        _action(dongtai, expected_page_type="post_list", exploration_value=80),
    )
    result = _append_bottom_navigation_tab_seek_candidate(
        candidates=actions,
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("uerma.zqeluk.cwsxx",),
        coverage_metadata=_coverage(
                "home",
                "video_list",
                "post_list",
            "profile",
            covered=("video_list",),
        ),
    )
    promoted = [
        item for item in result if item.metadata.get("bottom_navigation_tab_seek") is True
    ]
    assert [item.candidate_id for item in promoted] == [
        selected.candidate_id,
        dongtai.candidate_id,
        wode.candidate_id,
    ]
    assert result[0].candidate_id == selected.candidate_id
    assert result[0].metadata.get("coverage_page_seek") is True
    assert "playbook_tab_index" not in result[0].metadata
    assert all(item.candidate_id != feed.candidate_id for item in promoted)


def test_top_bar_app_scope_candidate_stays_as_regular_current_candidate() -> None:
    """当前帧顶栏入口即使属于 app 全局范围，也不得误标为底栏。"""

    from services.api.app.application.app_coverage_page_seek import (
        _append_bottom_navigation_tab_seek_candidate,
    )

    top_bar = AppVisualCandidate.build(
        source_capture_id="capture-top-bar",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="顶栏小说",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.36, 0.04, 0.64, 0.12),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="打开小说频道",
        expected_state_hint="content_list",
        frontier_scope="app",
        discovery_source="vision",
    )
    action = _action(top_bar, expected_page_type="content_list")
    result = _append_bottom_navigation_tab_seek_candidate(
        candidates=(action,),
        analysis=_analysis(
            visual_page_type="home",
            package_name="com.example.target",
            candidates=(top_bar,),
            capture_id="capture-top-bar",
        ),
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("com.example.target",),
        coverage_metadata=_coverage("home", "content_list"),
    )

    assert result == (action,)
    assert result[0].metadata.get("bottom_navigation_tab_seek") is not True


def test_bottom_nav_tab_seek_not_suppressed_until_visited_label() -> None:
    """Bottom-nav seek: 一次 UNPROVEN 不 suppress；已访或空点达上限才 suppress。"""

    from services.api.app.application.app_semantic_progress import (
        APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS,
        APP_BOTTOM_NAV_SEEK_ATTEMPT_THRESHOLD,
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        record_app_coverage_seek_target_attempt,
        record_bottom_nav_label_seek_attempt,
        should_suppress_redundant_app_candidate,
    )

    label = "\u5e95\u680f\u300c\u7247\u5e93\u300d"
    seek = CandidateAction(
        candidate_id="tab-pianku",
        semantic_target=label,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "coverage_page_seek": True,
            "bottom_navigation_tab_seek": True,
            "require_screenshot_evidence": True,
            "frontier_scope": "app",
        },
    )
    attempts = record_app_coverage_seek_target_attempt(
        {},
        candidate=seek,
        page_identity="pkg|main|home",
        arrival_proven=False,
    )
    assert attempts == {}
    assert not should_suppress_redundant_app_candidate(
        candidate=seek,
        coverage_metadata={
            "business_page_identity": "pkg|main|home",
            "app_coverage_seek_target_attempt_counts": {"any": 3},
        },
    )
    once = record_bottom_nav_label_seek_attempt({}, label=label, evidenced=False)
    assert not should_suppress_redundant_app_candidate(
        candidate=seek,
        coverage_metadata={APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS: once},
    )
    exhausted = once
    for _ in range(APP_BOTTOM_NAV_SEEK_ATTEMPT_THRESHOLD - 1):
        exhausted = record_bottom_nav_label_seek_attempt(
            exhausted, label=label, evidenced=False
        )
    assert should_suppress_redundant_app_candidate(
        candidate=seek,
        coverage_metadata={APP_BOTTOM_NAV_LABEL_ATTEMPT_COUNTS: exhausted},
    )
    assert should_suppress_redundant_app_candidate(
        candidate=seek,
        coverage_metadata={VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["\u7247\u5e93"]},
    )


def test_bottom_nav_uses_selected_and_visited_facts_without_playbook_filter() -> None:
    """当前帧 selected 与精确 visited 生效，产品 playbook 不限制真实入口。"""

    from services.api.app.application.app_coverage_page_seek import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        _append_bottom_navigation_tab_seek_candidate,
    )

    def _tab(name: str, left: float, right: float, hint: str) -> AppVisualCandidate:
        return AppVisualCandidate.build(
            source_capture_id="capture-hl",
            source_image_signature=_SIGNATURE,
            source_width=1080,
            source_height=1920,
            source_orientation="portrait",
            semantic_name=name,
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(left, 0.90, right, 0.98),
            confidence=0.95,
            risk_level=InteractionRiskLevel.LOW,
            purpose=hint,
            expected_state_hint=hint,
            frontier_scope="app",
            discovery_source="vision",
        )

    selected = _tab("\u5e95\u680f\u300c\u9996\u9875\u300d", 0.02, 0.14, "home")
    jingxuan = _tab("\u5e95\u680f\u300c\u7cbe\u9009\u300d", 0.16, 0.28, "content_list")
    wode = _tab("\u5e95\u680f\u300c\u6211\u7684\u300d", 0.86, 0.98, "profile")
    analysis = _analysis(
        visual_page_type="home",
        package_name="me.mfanv.npymci",
        candidates=(selected, jingxuan, wode),
        capture_id="capture-hl",
        surface_facts={"pixel_selected_labels": ["首页"]},
    )
    actions = (
        _action(selected, expected_page_type="home", exploration_value=80),
        _action(jingxuan, expected_page_type="content_list", exploration_value=80),
        _action(wode, expected_page_type="profile", exploration_value=80),
    )
    result = _append_bottom_navigation_tab_seek_candidate(
        candidates=actions,
        analysis=analysis,
        navigation_depth=0,
        surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("me.mfanv.npymci",),
        coverage_metadata={
            "required_page_types": ["home", "content_list", "video_list"],
            "covered_page_types": ["home"],
            VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["\u7cbe\u9009"],
            "app_curriculum_progress": {"playbook_id": "hlapp"},
        },
    )
    promoted = [
        item for item in result if item.metadata.get("bottom_navigation_tab_seek") is True
    ]
    # 首页由当前帧 selected 事实跳过；精选由精确 visited 跳过；「我的」即使
    # curriculum 是 hlapp 也必须保留，因为它是当前帧真实可见入口。
    assert [item.candidate_id for item in promoted] == [wode.candidate_id]
    assert "playbook_tab_index" not in promoted[0].metadata


def test_bottom_nav_visited_label_respects_stable_aliases_and_ignores_playbook() -> None:
    """已访按稳定别名互认（主页↔首页）；playbook 不改变当前帧入口集合。"""

    from services.api.app.application.app_coverage_page_seek import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
        _append_bottom_navigation_tab_seek_candidate,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
        should_suppress_redundant_app_candidate,
    )

    assert bottom_tab_label_already_visited("首页", {"主页"})
    assert bottom_tab_label_already_visited("短剧", {"视频"})
    assert bottom_tab_label_already_visited("首页", {"首页"})

    home = AppVisualCandidate.build(
        source_capture_id="capture-alias",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏「首页」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.02, 0.90, 0.14, 0.98),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="home",
        expected_state_hint="home",
        frontier_scope="app",
        discovery_source="vision",
    )
    short = AppVisualCandidate.build(
        source_capture_id="capture-alias",
        source_image_signature=_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="底栏「短剧」",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.16, 0.90, 0.28, 0.98),
        confidence=0.95,
        risk_level=InteractionRiskLevel.LOW,
        purpose="video_list",
        expected_state_hint="video_list",
        frontier_scope="app",
        discovery_source="vision",
    )
    analysis = _analysis(
        visual_page_type="content_list",
        package_name="dark.wwcskrpojs.ebjhyebjbl",
        candidates=(home, short),
        capture_id="capture-alias",
        surface_facts={"pixel_selected_labels": ["社区"]},
    )
    actions = (
        _action(home, expected_page_type="home", exploration_value=80),
        _action(short, expected_page_type="video_list", exploration_value=80),
    )
    result = _append_bottom_navigation_tab_seek_candidate(
        candidates=actions,
                analysis=analysis,
                navigation_depth=0,
                surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("dark.wwcskrpojs.ebjhyebjbl",),
                coverage_metadata={
            "required_page_types": ["home", "video_list", "profile"],
            "covered_page_types": ["home"],
            VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["主页"],
            "app_curriculum_progress": {"playbook_id": "hjll"},
            "package_name": "dark.wwcskrpojs.ebjhyebjbl",
            # hjll 上方分类未完会拦底栏；本用例只验别名已访，视为上方课纲已齐。
            "visited_home_top_category_tabs": [
                "最新",
                "推荐",
                "国产",
                "动漫",
                "漫画",
            ],
            "app_flow_contract": {
                "bottom_tabs_satisfied": False,
                "missing_bottom_tabs": ["tab-b"],
            },
        },
    )
    promoted = [
        item for item in result if item.metadata.get("bottom_navigation_tab_seek") is True
    ]
    # 「主页」已访则「首页」别名互认跳过；短剧仍来自当前帧，可提升。
    assert [item.candidate_id for item in promoted] == [short.candidate_id]
    seek = next(item for item in promoted if item.candidate_id == short.candidate_id)
    assert not should_suppress_redundant_app_candidate(
        candidate=seek,
        coverage_metadata={VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["主页"]},
    )
    assert should_suppress_redundant_app_candidate(
        candidate=seek,
        coverage_metadata={VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["视频"]},
    )
    assert should_suppress_redundant_app_candidate(
        candidate=seek,
        coverage_metadata={VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["短剧"]},
    )


def test_bottom_nav_does_not_interpolate_playbook_gaps() -> None:
    """playbook 缺口不得生成当前 Vision/UIA 未提供的标签或触控矩形。"""

    from services.api.app.application.app_coverage_page_seek import (
        _append_bottom_navigation_tab_seek_candidate,
    )

    def _tab(name: str, left: float, right: float, hint: str) -> AppVisualCandidate:
        return AppVisualCandidate.build(
            source_capture_id="capture-gap",
            source_image_signature=_SIGNATURE,
            source_width=1080,
            source_height=1920,
            source_orientation="portrait",
            semantic_name=f"底栏「{name}」",
            action_type=InteractionOperation.CLICK,
            normalized_bounds=NormalizedBounds(left, 0.90, right, 0.98),
            confidence=0.95,
            risk_level=InteractionRiskLevel.LOW,
            purpose=hint,
            expected_state_hint=hint,
            frontier_scope="app",
            discovery_source="vision",
        )

    left = _tab("精选", 0.02, 0.14, "content_list")
    right = _tab("圈子", 0.86, 0.98, "post_list")
    analysis = _analysis(
        visual_page_type="content_list",
        package_name="me.mfanv.npymci",
        candidates=(left, right),
        capture_id="capture-gap",
    )
    result = _append_bottom_navigation_tab_seek_candidate(
        candidates=(_action(left), _action(right)),
            analysis=analysis,
            navigation_depth=0,
            surface_kind=SurfaceKind.ANDROID,
        allowed_hosts=("me.mfanv.npymci",),
            coverage_metadata={
            "required_page_types": ["home", "content_list", "post_list"],
            "covered_page_types": ["home"],
            "app_curriculum_progress": {"playbook_id": "hlapp"},
        },
    )
    promoted = [
        item for item in result if item.metadata.get("bottom_navigation_tab_seek") is True
    ]
    # 非主页选中态：当前帧可见底栏全部保留提升，不因 playbook 缺口合成中间栏。
    assert [item.candidate_id for item in promoted] == [left.candidate_id, right.candidate_id]
    assert all("bottom_nav_gap_fill" not in item.metadata for item in promoted)


def test_flow_contract_bottom_tabs_hard_incomplete_without_playbook_or_package_rules() -> None:
    from services.api.app.application.app_coverage_page_seek import (
        playbook_bottom_tabs_hard_incomplete,
    )

    # hjll 课纲底栏未巡完（仅主页已访）→ hard incomplete。
    incomplete = {
        "app_curriculum_progress": {"playbook_id": "hjll"},
        "package_name": "dark.wwcskrpojs.ebjhyebjbl",
        "visited_bottom_navigation_labels": ["主页"],
    }
    assert playbook_bottom_tabs_hard_incomplete(coverage_metadata=incomplete) is True

    complete = {
        **incomplete,
        "app_flow_contract": {
            "bottom_tabs_satisfied": False,
            "missing_bottom_tabs": ["tab-b"],
        },
    }
    assert playbook_bottom_tabs_hard_incomplete(coverage_metadata=complete) is True

    satisfied = {
        **complete,
        "app_flow_contract": {"bottom_tabs_satisfied": True, "missing_bottom_tabs": []},
    }
    # flow 已满足时仍可能因课纲标签未访而 incomplete；补齐已访后再断言 False。
    from services.api.app.application.app_ui_tree_features import (
        ordered_bottom_tab_labels,
    )

    all_visited = {
        **satisfied,
        "visited_bottom_navigation_labels": list(
            ordered_bottom_tab_labels(playbook_id="hjll")
        ),
    }
    assert playbook_bottom_tabs_hard_incomplete(coverage_metadata=all_visited) is False


def test_coverage_seek_does_not_promote_detail_while_bottom_tabs_incomplete() -> None:
    """底栏未巡完时不得把内容卡提升为 video_detail／content_detail 寻页。"""

    from services.api.app.application.app_coverage_page_seek import (
        VISITED_BOTTOM_NAVIGATION_LABELS_KEY,
    )

    visual = _visual_candidate(expected_page_type="video_detail", semantic_name="内容卡")
    detail = _action(visual, expected_page_type="video_detail", exploration_value=95)
    result = _append(
        candidates=(detail,),
        analysis=_analysis(
            visual_page_type="home",
            page_state="stable",
            candidates=(visual,),
            package_name="dark.wwcskrpojs.ebjhyebjbl",
        ),
        coverage_metadata={
            "required_page_types": ["home", "video_list", "video_detail", "profile"],
            "covered_page_types": ["home"],
            VISITED_BOTTOM_NAVIGATION_LABELS_KEY: ["主页"],
            "app_curriculum_progress": {"playbook_id": "hjll"},
            "package_name": "dark.wwcskrpojs.ebjhyebjbl",
            "app_flow_contract": {
                "bottom_tabs_satisfied": False,
                "missing_bottom_tabs": ["tab-b"],
            },
        },
    )
    assert result[0].candidate_id == detail.candidate_id
    assert result[0].metadata.get("coverage_page_seek") is not True


def test_hjll_playbook_label_allows_rejects_fuli_skin_alias() -> None:
    """hjll 提升放行不得把「福利」当成「暗网」入口。"""

    from services.api.app.application.app_coverage_page_seek import (
        _normalize_bottom_tab_label,
        _playbook_label_allows,
    )
    from services.api.app.application.app_semantic_progress import (
        bottom_tab_label_already_visited,
    )

    playbook = {"主页", "短剧", "暗网", "AI", "社区", "我的"}
    assert _playbook_label_allows(_normalize_bottom_tab_label("短剧"), playbook) is True
    assert _playbook_label_allows(_normalize_bottom_tab_label("视频"), playbook) is True
    assert _playbook_label_allows(_normalize_bottom_tab_label("暗网"), playbook) is True
    assert _playbook_label_allows(_normalize_bottom_tab_label("福利"), playbook) is False
    # 已访互认仍可：点过暗网后福利视为同槽已访（换皮 App），但不等于可提升点福利。
    assert bottom_tab_label_already_visited("福利", {"暗网"}) is True


def test_playbook_progress_does_not_skip_anwang_for_manhua_alias() -> None:
    """上方分类「漫画」或同槽袋不得让课纲跳过暗网直接到 AI。"""

    from services.api.app.application.app_coverage_page_seek import (
        _next_unvisited_playbook_bottom_tab_label,
        _prefer_anwang_after_short_drama,
    )
    from services.api.app.application.app_semantic_progress import (
        SHORT_DRAMA_END_SEEN_KEY,
        bottom_tab_playbook_progress_already_visited,
    )

    ordered = ("主页", "短剧", "暗网", "AI", "社区", "我的")
    # 漫画在已访袋里：旧逻辑会把暗网当已访；课纲推进必须仍返回暗网。
    assert bottom_tab_playbook_progress_already_visited("暗网", {"主页", "短剧", "漫画"}) is False
    assert (
        _next_unvisited_playbook_bottom_tab_label(
            visited_labels={"主页", "短剧", "漫画"},
            ordered_labels=ordered,
            page_type="home",
        )
        == "暗网"
    )
    assert (
        _prefer_anwang_after_short_drama(
            next_label="ai",
            visited_labels={"主页", "短剧"},
            ordered_labels=ordered,
            coverage_metadata={SHORT_DRAMA_END_SEEN_KEY: True},
        )
        == "暗网"
    )
    assert (
        _prefer_anwang_after_short_drama(
            next_label="ai",
            visited_labels={"主页", "短剧", "暗网"},
            ordered_labels=ordered,
            coverage_metadata={SHORT_DRAMA_END_SEEN_KEY: True},
        )
        == "ai"
    )

def test_overlay_budget_force_yields_on_clearance_without_business_progress() -> None:
    """连续清障无业务进展达阈值时，即使未满 6 帧也让真实寻页穿透。"""

    from services.api.app.application.app_coverage_page_seek import (
        overlay_budget_allows_real_coverage_seek,
    )

    base = {
        "required_page_types": ["home", "video_list", "profile"],
        "covered_page_types": ["home"],
        "dynamic_home_popup_frame_count": 1,
    }
    assert (
        overlay_budget_allows_real_coverage_seek(
            page_type="home",
            coverage_metadata=base,
        )
        is False
    )
    assert (
        overlay_budget_allows_real_coverage_seek(
            page_type="home",
        coverage_metadata={
                **base,
                "clearance_without_business_progress_count": 4,
            },
        )
        is True
    )
    assert (
        overlay_budget_allows_real_coverage_seek(
            page_type="home",
                coverage_metadata={
                **base,
                "dismiss_noop_streak": 2,
            },
        )
        is True
    )
    assert (
        overlay_budget_allows_real_coverage_seek(
            page_type="home",
            coverage_metadata={
                **base,
                "dynamic_home_popup_frame_count": 3,
                "clearance_without_business_progress_count": 3,
            },
        )
        is True
    )
    # 覆盖已完：清障压力不得单独让路
    assert (
        overlay_budget_allows_real_coverage_seek(
            page_type="home",
            coverage_metadata={
                "required_page_types": ["home"],
                "covered_page_types": ["home"],
                "clearance_without_business_progress_count": 9,
                "dynamic_home_popup_frame_count": 1,
            },
        )
        is False
    )


def test_hjll_profile_credential_matches_save_not_already_saved() -> None:
    """海角个人中心账号凭证只认「保存凭证」，排除「我已保存」。"""

    from services.api.app.application.app_ui_tree_features import (
        candidate_matches_profile_credential_save,
        candidate_matches_profile_credential_skip,
        load_app_ui_tree_features,
        observation_matches_profile_credential,
        profile_credential_save_bounds,
        profile_credential_skip_bounds,
    )

    load_app_ui_tree_features.cache_clear()
    pkg = "dark.wwcskrpojs.ebjhyebjbl"
    assert observation_matches_profile_credential(
        page_type="profile",
        page_state="popup",
        observation_text="账号凭证 66507294 保存凭证",
        package_name=pkg,
        profile_tab_visited=True,
    )
    # 主页／未访「我的」：强文案也不得冒充凭证（误点右下会跳进个人中心）。
    assert not observation_matches_profile_credential(
        page_type="home",
        page_state="popup",
        observation_text="账号凭证 66507294 保存凭证 我已保存 请勿删除此凭证",
        package_name=pkg,
        profile_tab_visited=False,
    )
    assert not observation_matches_profile_credential(
        page_type="profile",
        page_state="popup",
        observation_text="账号凭证 66507294 保存凭证",
        package_name=pkg,
        profile_tab_visited=False,
    )
    assert observation_matches_profile_credential(
        page_type="home",
        page_state="popup",
        observation_text="账号凭证 66507294 保存凭证 我已保存 请勿删除此凭证",
        package_name=pkg,
        profile_tab_visited=True,
    )
    assert observation_matches_profile_credential(
        page_type="profile",
        page_state="popup",
        observation_text="个人中心 保存凭证 我已保存",
        package_name=pkg,
        profile_tab_visited=True,
    )
    # 主页永久地址／haijiao 横幅不得冒充凭证（即使 popup + 已访我的）。
    assert not observation_matches_profile_credential(
        page_type="home",
        page_state="popup",
        observation_text="普通广告弹窗 关闭 永久官网地址 haijiao.so",
        package_name=pkg,
        profile_tab_visited=True,
    )
    assert not observation_matches_profile_credential(
        page_type="home",
        page_state="popup",
        observation_text="personal center modal with qrcode haijiao",
        package_name=pkg,
        profile_tab_visited=True,
    )
    save = CandidateAction(
        candidate_id="save-credential",
        semantic_target="保存凭证",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        metadata={"purpose": "保存账号凭证"},
    )
    skip = CandidateAction(
        candidate_id="already-saved",
        semantic_target="我已保存",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        metadata={"purpose": "我已保存凭证"},
    )
    assert candidate_matches_profile_credential_save(save, package_name=pkg) is True
    assert candidate_matches_profile_credential_skip(save, package_name=pkg) is False
    assert candidate_matches_profile_credential_save(skip, package_name=pkg) is False
    assert candidate_matches_profile_credential_skip(skip, package_name=pkg) is True
    zone = profile_credential_save_bounds(package_name=pkg)
    assert zone is not None
    assert zone[1]["left"] >= 0.4
    assert zone[1]["top"] >= 0.84
    assert zone[1]["bottom"] <= 0.94
    skip_zone = profile_credential_skip_bounds(package_name=pkg)
    assert skip_zone is not None
    assert skip_zone[1]["top"] >= 0.84
    assert skip_zone[1]["right"] <= 0.52
    # 按钮中心须落在 UIA 实测钮带（约 y=0.855～0.911），避免旧框 0.76～0.825 点空。
    save_cy = (float(zone[1]["top"]) + float(zone[1]["bottom"])) / 2.0
    skip_cy = (float(skip_zone[1]["top"]) + float(skip_zone[1]["bottom"])) / 2.0
    assert 0.85 <= save_cy <= 0.91
    assert 0.85 <= skip_cy <= 0.91


def test_profile_credential_save_phase_ready_rejects_false_done_when_save_visible() -> None:
    """未点过保存且同帧仍见「保存凭证」时，不得因误标 done 跳进「我已保存」。"""

    from services.api.app.application.app_ui_tree_features import (
        load_app_ui_tree_features,
        profile_credential_save_phase_ready,
    )

    load_app_ui_tree_features.cache_clear()
    pkg = "dark.wwcskrpojs.ebjhyebjbl"
    blob = "账号凭证 保存凭证 我已保存 请勿删除此凭证"
    assert (
        profile_credential_save_phase_ready(
            save_done_flag=True,
            save_attempt_count=0,
            observation_text=blob,
            package_name=pkg,
        )
        is False
    )
    assert (
        profile_credential_save_phase_ready(
            save_done_flag=False,
            save_attempt_count=1,
            observation_text=blob,
            package_name=pkg,
        )
        is True
    )
    assert (
        profile_credential_save_phase_ready(
            save_done_flag=False,
            save_attempt_count=0,
            observation_text=blob + " 保存成功",
            package_name=pkg,
        )
        is True
    )
