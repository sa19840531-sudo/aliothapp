"""H5/App 共用有界调度器的合同测试。"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from services.api.app.application.bounded_exploration_scheduler import (
    BoundedExplorationScheduler,
    BoundedExplorationState,
    ExplorationQueue,
    ExplorationStopReason,
    evidence_gap_page_types_from_facts,
)
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)


def _candidate(
    candidate_id: str,
    *,
    expected_page_type: str | None = None,
    action_type: InteractionOperation = InteractionOperation.CLICK,
    **metadata: object,
) -> CandidateAction:
    if expected_page_type is not None:
        metadata["expected_next_page_type"] = expected_page_type
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=f"目标-{candidate_id}",
        action_type=action_type,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=50,
        navigation_depth=0,
        metadata=metadata,
    )


def test_classification_uses_frozen_facts_not_agent_priority() -> None:
    scheduler = BoundedExplorationScheduler()
    required = _candidate(
        "required",
        expected_page_type="home",
        scheduler_queue="discovery",
    )
    evidence_gap = _candidate("gap", expected_page_type="settings")
    discovery = _candidate("discovery", expected_page_type="activity")
    popup = _candidate("popup", control_role="dismiss")

    assert scheduler.classify(required, required_page_types=("home",)) is ExplorationQueue.REQUIRED
    assert (
        scheduler.classify(
            evidence_gap,
            evidence_gap_page_types=("settings",),
        )
        is ExplorationQueue.EVIDENCE_GAP
    )
    assert scheduler.classify(discovery) is ExplorationQueue.DISCOVERY
    assert scheduler.classify(popup) is ExplorationQueue.REQUIRED
    assert scheduler.queue_rank(required, required_page_types=("home",)) < scheduler.queue_rank(
        evidence_gap,
        evidence_gap_page_types=("settings",),
    )


def test_state_keeps_per_queue_action_time_retry_and_loop_facts_after_roundtrip() -> None:
    scheduler = BoundedExplorationScheduler()
    state = BoundedExplorationState.create(
        max_actions=10,
        max_duration_ms=10_000,
        deadline=datetime.now(UTC) + timedelta(minutes=1),
        required_page_types=("home",),
        evidence_gap_page_types=("settings",),
    )
    state = state.advance(
        scheduler,
        _candidate("required", expected_page_type="home"),
        action_claimed=True,
        retryable_failure=True,
        progressed=True,
        state_fingerprint="state-1",
        page_fingerprint="page-1",
        popup_fingerprint="popup-1",
        return_fingerprint="return-1",
        elapsed_duration_ms=125,
    )
    restored = BoundedExplorationState.from_dict(
        state.to_dict(),
        default_max_actions=99,
        default_max_duration_ms=99_999,
    )

    assert restored.max_actions == 10
    assert restored.max_duration_ms == 10_000
    assert restored.consumed_actions == {"required": 1}
    assert restored.consumed_duration_ms == {"required": 125}
    assert restored.retry_attempts == {"required": 1}
    assert restored.retry_attempts_by_candidate == state.retry_attempts_by_candidate
    assert restored.required_page_types == ("home",)
    assert restored.evidence_gap_page_types == ("settings",)
    assert restored.last_page_fingerprint == "page-1"
    assert restored.last_popup_fingerprint == "popup-1"
    assert restored.last_return_fingerprint == "return-1"


def test_filter_stops_exhausted_queue_but_allows_explicit_borrowing_only_for_other_queues() -> None:
    scheduler = BoundedExplorationScheduler()
    state = BoundedExplorationState(
        max_actions=10,
        max_duration_ms=1_000,
        consumed_actions={"required": 7},
        consumed_duration_ms={"required": 700},
        retry_attempts={"required": 2},
        required_page_types=("home",),
    )
    required = _candidate("required", expected_page_type="home")
    discovery = _candidate("discovery", expected_page_type="activity")

    assert scheduler.filter_candidates(
        (required, discovery),
        state=state,
    ) == (discovery,)
    assert scheduler.filter_candidates((required,), state=state) == ()


def test_new_trusted_obstruction_candidate_is_not_starved_by_prior_queue_retries() -> None:
    scheduler = BoundedExplorationScheduler()
    failed_startup = _candidate(
        "startup",
        target_keywords=["startup_ad", "close_control"],
        action_parameters={
            "target_mode": "startup_ad_dismissal",
            "startup_ad_capability": "countdown_close",
            "obstruction_signature": "h5-obstruction:" + "a" * 64,
        },
    )
    popup = _candidate(
        "popup",
        target_keywords=["popup_dismissal", "close_control"],
        action_parameters={
            "target_mode": "popup_dismissal",
            "popup_dismissal_capability": "dialog_generic_close",
            "obstruction_signature": "h5-obstruction:" + "b" * 64,
        },
    )
    state = BoundedExplorationState.create(max_actions=10)
    for _ in range(2):
        state = state.advance(
            scheduler,
            failed_startup,
            action_claimed=True,
            retryable_failure=True,
        )

    assert state.retry_attempts == {"required": 2}
    assert scheduler.filter_candidates((failed_startup,), state=state) == ()
    assert scheduler.filter_candidates((popup,), state=state) == (popup,)


def test_page_popup_and_return_signatures_stop_separate_loops() -> None:
    scheduler = BoundedExplorationScheduler()
    policy = scheduler.policy.__class__(repeat_limit=1)
    scheduler = BoundedExplorationScheduler(policy=policy)
    candidate = _candidate("candidate")

    page_state = BoundedExplorationState.create(max_actions=10, policy=policy)
    page_state = page_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        page_fingerprint="same-page",
    )
    page_state = page_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        page_fingerprint="same-page",
    )
    assert page_state.stop_reason() is ExplorationStopReason.REPEATED_PAGE_LIMIT

    popup_state = BoundedExplorationState.create(max_actions=10, policy=policy)
    popup_state = popup_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        popup_fingerprint="same-popup",
    )
    popup_state = popup_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        popup_fingerprint="same-popup",
    )
    assert popup_state.stop_reason() is ExplorationStopReason.POPUP_LOOP_LIMIT

    return_state = BoundedExplorationState.create(max_actions=10, policy=policy)
    return_state = return_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        return_fingerprint="same-return",
    )
    return_state = return_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        return_fingerprint="same-return",
    )
    assert return_state.stop_reason() is ExplorationStopReason.RETURN_LOOP_LIMIT


def test_non_adjacent_return_cycle_is_bounded_but_same_page_scroll_is_not_duplicate() -> None:
    scheduler = BoundedExplorationScheduler(
        policy=BoundedExplorationScheduler().policy.__class__(repeat_limit=1)
    )
    candidate = _candidate("cycle", action_type=InteractionOperation.BACK)

    state = BoundedExplorationState.create(max_actions=20, policy=scheduler.policy)
    for signature in ("return-a", "return-b", "return-a", "return-b"):
        state = state.advance(
            scheduler,
            candidate,
            action_claimed=False,
            progressed=True,
            return_fingerprint=signature,
        )
    assert state.return_fingerprint_history == (
        "return-a",
        "return-b",
        "return-a",
        "return-b",
    )
    assert state.stop_reason() is ExplorationStopReason.RETURN_LOOP_LIMIT

    scroll_state = BoundedExplorationState.create(max_actions=20, policy=scheduler.policy)
    scroll_state = scroll_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        state_fingerprint="visual-frame-1",
        page_fingerprint="same-page",
    )
    scroll_state = scroll_state.advance(
        scheduler,
        candidate,
        action_claimed=False,
        progressed=True,
        state_fingerprint="visual-frame-2",
        page_fingerprint="same-page",
    )
    assert scroll_state.page_repeat_count == 0


def test_missing_return_signature_preserves_cycle_history_but_resets_adjacent_repeat() -> None:
    scheduler = BoundedExplorationScheduler(
        policy=BoundedExplorationScheduler().policy.__class__(repeat_limit=1)
    )
    back_candidate = _candidate("cycle", action_type=InteractionOperation.BACK)
    stale_candidate = _candidate("stale")

    state = BoundedExplorationState.create(max_actions=20, policy=scheduler.policy)
    for signature in ("return-a", "return-b"):
        state = state.advance(
            scheduler,
            back_candidate,
            action_claimed=False,
            progressed=True,
            return_fingerprint=signature,
        )

    state = state.advance(
        scheduler,
        stale_candidate,
        action_claimed=True,
        retryable_failure=True,
        progressed=False,
        return_fingerprint=None,
    )
    assert state.last_return_fingerprint is None
    assert state.return_repeat_count == 0
    assert state.return_fingerprint_history == ("return-a", "return-b")

    for signature in ("return-a", "return-b"):
        state = state.advance(
            scheduler,
            back_candidate,
            action_claimed=False,
            progressed=True,
            return_fingerprint=signature,
        )

    assert state.return_fingerprint_history == (
        "return-a",
        "return-b",
        "return-a",
        "return-b",
    )
    assert state.return_repeat_count == 1
    assert state.stop_reason() is ExplorationStopReason.RETURN_LOOP_LIMIT


def test_progressed_observation_clears_return_history_and_resets_no_progress() -> None:
    state = BoundedExplorationState(
        max_actions=20,
        no_progress_count=2,
        return_fingerprint_history=("return-a", "return-b"),
    )

    state = state.advance_observation(
        queue=ExplorationQueue.DISCOVERY,
        progressed=True,
        return_fingerprint=None,
    )

    assert state.no_progress_count == 0
    assert state.last_return_fingerprint is None
    assert state.return_fingerprint_history == ()


def test_filter_candidates_honors_global_duration_and_deadline_stop() -> None:
    scheduler = BoundedExplorationScheduler()
    candidate = _candidate("candidate", expected_page_type="activity")
    duration_state = BoundedExplorationState(
        max_actions=10,
        max_duration_ms=1_000,
        consumed_duration_ms={"discovery": 1_000},
    )
    assert scheduler.filter_candidates((candidate,), state=duration_state) == ()
    deadline = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)
    deadline_state = BoundedExplorationState(
        max_actions=10,
        deadline=deadline,
    )
    assert (
        scheduler.filter_candidates(
            (candidate,),
            state=deadline_state,
            now=deadline,
        )
        == ()
    )


def test_evidence_gap_projection_uses_writer_facts_not_scheduler_draft_keys() -> None:
    metadata = {
        # 这些键不是 Evidence/Rule writer 的事实，不能驱动缺口队列。
        "evidence_gap_page_types": ["draft_only"],
        "missing_evidence_page_types": ["draft_only_2"],
        "pending_evidence_page_types": ["draft_only_3"],
        "h5_coverage_snapshot": {
            "scopes": [
                {"page_type": "profile", "assessment_status": "incomplete"},
            ],
        },
        "app_evidence_index": {
            "evidence_gaps": [
                {"reason_code": "capture_missing", "metadata": {"page_type": "settings"}},
            ],
        },
        "rule_judge_output": {"missing_evidence": ["activity", "temporal_trace"]},
    }

    assert evidence_gap_page_types_from_facts(
        metadata,
        known_page_types=("profile", "settings", "activity"),
    ) == ("activity", "profile", "settings")
    assert "draft_only" not in evidence_gap_page_types_from_facts(metadata)


def test_required_page_type_already_covered_can_be_classified_as_evidence_gap() -> None:
    scheduler = BoundedExplorationScheduler()
    candidate = _candidate("profile-gap", expected_page_type="profile")

    assert scheduler.classify(
        candidate,
        required_page_types=("profile",),
        evidence_gap_page_types=("profile",),
    ) is ExplorationQueue.EVIDENCE_GAP
    assert scheduler.classify(
        candidate,
        required_page_types=("profile",),
        evidence_gap_page_types=("profile",),
        missing_page_types=("profile",),
    ) is ExplorationQueue.REQUIRED


def test_suppress_popup_loop_stop_keeps_run_open_when_bottom_coverage_incomplete() -> None:
    scheduler = BoundedExplorationScheduler(
        policy=BoundedExplorationScheduler().policy.__class__(repeat_limit=1)
    )
    candidate = _candidate("dismiss")

    state = BoundedExplorationState.create(
        max_actions=10,
        policy=scheduler.policy,
    )
    state = state.advance(
        scheduler,
        candidate,
        action_claimed=True,
        progressed=False,
        popup_fingerprint="same-popup",
        suppress_popup_loop_stop=True,
    )
    state = state.advance(
        scheduler,
        candidate,
        action_claimed=True,
        progressed=False,
        popup_fingerprint="same-popup",
        suppress_popup_loop_stop=True,
    )
    assert state.popup_repeat_count >= 1
    assert state.suppress_popup_loop_stop is True
    assert state.stop_reason() is ExplorationStopReason.NONE


def test_suppress_popup_loop_stop_does_not_bypass_no_progress_limit() -> None:
    policy = BoundedExplorationScheduler().policy.__class__(repeat_limit=1)
    state = replace(
        BoundedExplorationState.create(max_actions=10, policy=policy),
        popup_repeat_count=policy.repeat_limit,
        no_progress_count=policy.no_progress_limit,
        suppress_popup_loop_stop=True,
    )

    assert state.stop_reason() is ExplorationStopReason.NO_PROGRESS_LIMIT
    assert (
        replace(state, suppress_popup_loop_stop=False).stop_reason()
        is ExplorationStopReason.POPUP_LOOP_LIMIT
    )


def test_bottom_navigation_seek_resets_popup_repeat_count() -> None:
    scheduler = BoundedExplorationScheduler(
        policy=BoundedExplorationScheduler().policy.__class__(repeat_limit=1)
    )
    dismiss = _candidate("dismiss")
    tab = _candidate("tab-mine", bottom_navigation_tab_seek=True)

    state = BoundedExplorationState.create(max_actions=10, policy=scheduler.policy)
    state = state.advance(
        scheduler,
        dismiss,
        action_claimed=True,
        progressed=False,
        popup_fingerprint="same-popup",
    )
    state = state.advance(
        scheduler,
        dismiss,
        action_claimed=True,
        progressed=False,
        popup_fingerprint="same-popup",
    )
    assert state.stop_reason() is ExplorationStopReason.POPUP_LOOP_LIMIT

    state = state.advance(
        scheduler,
        tab,
        action_claimed=True,
        progressed=True,
        popup_fingerprint="same-popup",
        suppress_popup_loop_stop=True,
    )
    assert state.popup_repeat_count == 0
    assert state.last_popup_fingerprint is None
    assert state.stop_reason() is ExplorationStopReason.NONE
