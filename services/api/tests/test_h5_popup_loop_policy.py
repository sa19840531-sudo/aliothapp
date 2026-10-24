"""H5 业务页面弹窗处理轮次策略测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.h5_popup_loop_policy import (
    H5_BUSINESS_PAGE_POPUP_LIMIT,
    H5_POPUP_DISMISSAL_REOBSERVE_LIMIT,
    H5_POPUP_DISMISSAL_STREAK_LIMIT,
    H5_SAME_POPUP_SIGNATURE_LIMIT,
    build_h5_business_page_key,
    evaluate_h5_business_page_popup_loop,
    next_popup_dismissal_reobserve_state,
    popup_dismissal_reobserve_exhausted,
)
from services.api.app.domain.exploration import CandidateAction
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)

_HOME_URL = "https://example.test/home?tab=feed#top"
_BASE_TIME = datetime(2026, 8, 15, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("error_code", [
    "LOGICAL_SOURCE_STATE_STALE", "POPUP_MODAL_SCOPE_MISMATCH",
])
@pytest.mark.parametrize("prior", [None, {"signature": "old", "count": 2, "streak": 2}])
def test_unclicked_popup_contract_refresh_does_not_increment_failure(error_code, prior):
    assert next_popup_dismissal_reobserve_state(
        candidate=_popup_candidate(1), prior_state=prior, error_code=error_code,
    ) == prior


def _popup_candidate(
    sequence: int,
    *,
    source_page_type: str = "home",
    signature: str | None = None,
) -> CandidateAction:
    obstruction_signature = signature or f"h5-obstruction:{sequence:064x}"
    capability = "dialog_content_close"
    return CandidateAction(
        candidate_id=f"popup-close-{sequence}",
        semantic_target="关闭当前弹窗",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=100,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.WEB,
        metadata={
            "target_keywords": ["popup_dismissal", "close_control", capability],
            "expected_next_page_type": source_page_type,
            "source_page_type": source_page_type,
            "obstruction_signature": obstruction_signature,
            "action_parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": capability,
                "obstruction_signature": obstruction_signature,
            },
        },
    )


def _add_attempt(
    snapshot: H5TaskFrontierSnapshot,
    sequence: int,
    *,
    source_location: str = _HOME_URL,
    source_page_type: str = "home",
    terminal_status: str = "executed",
    signature: str | None = None,
) -> H5TaskFrontierSnapshot:
    candidate = _popup_candidate(sequence, source_page_type=source_page_type, signature=signature)
    observed_at = _BASE_TIME + timedelta(seconds=sequence * 2)
    merged = snapshot.merge(
        source_capture_id=f"capture-popup-{sequence}",
        source_state_version=f"state-popup-{sequence}",
        source_location=source_location,
        navigation_depth=0,
        candidates=(candidate,),
        now=observed_at,
    )
    entry = next(
        item for item in merged.entries if item.candidate.candidate_id == candidate.candidate_id
    )
    selected = merged.select_entry(entry.entry_id, selected_at=observed_at)
    finished_at = observed_at + timedelta(seconds=1)
    if terminal_status == "executed":
        return selected.mark_executed(entry.entry_id, finished_at=finished_at)
    if terminal_status == "failed":
        return selected.mark_failed(
            entry.entry_id,
            reason="popup_close_failed",
            finished_at=finished_at,
        )
    if terminal_status == "blocked":
        return selected.mark_blocked(
            entry.entry_id,
            reason="popup_close_blocked",
            finished_at=finished_at,
        )
    if terminal_status == "skipped":
        return selected.mark_skipped(
            entry.entry_id,
            reason="retryable_action_reobserved:POPUP_DISMISSAL_NOT_OBSERVED",
            finished_at=finished_at,
        )
    if terminal_status == "selected":
        return selected
    raise AssertionError(f"不支持的测试终态：{terminal_status}")


def _snapshot_with_attempts(count: int) -> H5TaskFrontierSnapshot:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for sequence in range(1, count + 1):
        snapshot = _add_attempt(snapshot, sequence)
    return snapshot


@pytest.mark.parametrize("count", [10, 25, 50])
def test_successful_layers_never_consume_failure_allowance(count):
    state = evaluate_h5_business_page_popup_loop(
        snapshot=_snapshot_with_attempts(count), location=_HOME_URL, page_type="home",
        current_candidates=(_popup_candidate(count + 1),),
    )
    assert state is not None and not state.limit_reached
    assert state.completed_count == 0


def test_successful_same_signature_layers_remain_executable():
    signature = "h5-obstruction:" + "f" * 64
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for sequence in range(1, 6):
        snapshot = _add_attempt(snapshot, sequence, signature=signature)
    assert len(snapshot.entries) == 5
    state = evaluate_h5_business_page_popup_loop(
        snapshot=snapshot, location=_HOME_URL, page_type="home",
        current_candidates=(_popup_candidate(6, signature=signature),),
    )
    assert state is not None and not state.limit_reached


@pytest.mark.parametrize(
    ("location", "page_type", "expected"),
    [
        (
            "HTTPS://Example.COM:443/path?tab=1#vip",
            " HOME ",
            "https://example.com/path?tab=1|home",
        ),
        ("http://Example.COM:80", "home", "http://example.com/|home"),
        ("https://example.com", "home", "https://example.com/|home"),
        ("https://example.com/", "line_loading", None),
        ("https://example.com/", "startup_ad", None),
        ("https://example.com/", None, None),
        ("about:blank", "home", None),
        ("not-a-url", "home", None),
        ("https://example.com:invalid/", "home", None),
    ],
)
def test_business_page_key_normalizes_only_stable_http_pages(
    location: str,
    page_type: str | None,
    expected: str | None,
) -> None:
    assert build_h5_business_page_key(location, page_type) == expected


def test_popup_loop_allows_final_attempt_below_page_limit() -> None:
    current = _popup_candidate(H5_BUSINESS_PAGE_POPUP_LIMIT)

    state = evaluate_h5_business_page_popup_loop(
        snapshot=_snapshot_with_attempts(H5_BUSINESS_PAGE_POPUP_LIMIT - 1),
        location=_HOME_URL,
        page_type="home",
        current_candidates=(current,),
    )

    assert state is not None
    assert state.page_key == "https://example.test/home?tab=feed|home"
    assert state.completed_count == 0
    assert state.limit == H5_BUSINESS_PAGE_POPUP_LIMIT
    assert state.limit_reached is False
    assert state.reason_code == "popup_layer_limit_exceeded"


def test_popup_loop_allows_successful_layers_beyond_previous_page_limit() -> None:
    state = evaluate_h5_business_page_popup_loop(
        snapshot=_snapshot_with_attempts(H5_BUSINESS_PAGE_POPUP_LIMIT),
        location=_HOME_URL,
        page_type="home",
        current_candidates=(_popup_candidate(H5_BUSINESS_PAGE_POPUP_LIMIT + 1),),
    )

    assert state is not None
    assert state.completed_count == 0
    assert state.limit == 20
    assert state.limit_reached is False
    assert state.reason_code == "popup_layer_limit_exceeded"


def test_popup_loop_stalls_same_signature_across_works() -> None:
    """同签名弹窗（关闭被阻断/关闭无效）跨工作累计 3 次即僵持止损。

    线上实锤（91PORN /mine 身份凭证卡）：阻断轮次的阶段机计数随工作重发归零，
    同一个关不掉的弹窗每 20 秒重试一轮，必须靠 Frontier 快照跨工作止损。
    """
    stuck_signature = "h5-obstruction:" + "a" * 64
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for sequence in range(1, H5_SAME_POPUP_SIGNATURE_LIMIT + 1):
        snapshot = _add_attempt(
            snapshot,
            sequence,
            terminal_status="blocked",
            signature=stuck_signature,
        )

    state = evaluate_h5_business_page_popup_loop(
        snapshot=snapshot,
        location=_HOME_URL,
        page_type="home",
        current_candidates=(_popup_candidate(9, signature=stuck_signature),),
    )

    assert state is not None
    assert state.limit_reached is True
    assert state.reason_code == "popup_dismissal_stalled"
    assert state.completed_count == H5_SAME_POPUP_SIGNATURE_LIMIT
    assert state.limit == H5_SAME_POPUP_SIGNATURE_LIMIT


def test_popup_loop_does_not_stall_below_signature_limit_or_new_signature() -> None:
    stuck_signature = "h5-obstruction:" + "b" * 64
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    for sequence in range(1, H5_SAME_POPUP_SIGNATURE_LIMIT):
        snapshot = _add_attempt(
            snapshot,
            sequence,
            terminal_status="blocked",
            signature=stuck_signature,
        )

    same_signature_state = evaluate_h5_business_page_popup_loop(
        snapshot=snapshot,
        location=_HOME_URL,
        page_type="home",
        current_candidates=(_popup_candidate(9, signature=stuck_signature),),
    )
    assert same_signature_state is not None
    assert same_signature_state.limit_reached is False
    assert same_signature_state.reason_code == "popup_layer_limit_exceeded"

    # 换了新签名（新弹窗层）不受旧签名僵持影响。
    new_signature_state = evaluate_h5_business_page_popup_loop(
        snapshot=snapshot,
        location=_HOME_URL,
        page_type="home",
        current_candidates=(_popup_candidate(10),),
    )
    assert new_signature_state is not None
    assert new_signature_state.limit_reached is False


def test_popup_loop_counts_only_finished_attempts_for_same_page_key() -> None:
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None)
    snapshot = _add_attempt(snapshot, 1, terminal_status="executed")
    snapshot = _add_attempt(snapshot, 2, terminal_status="failed")
    snapshot = _add_attempt(snapshot, 3, terminal_status="blocked")
    snapshot = _add_attempt(snapshot, 4, terminal_status="selected")
    snapshot = _add_attempt(snapshot, 8, terminal_status="skipped")
    snapshot = _add_attempt(
        snapshot,
        5,
        source_location="https://example.test/other",
        terminal_status="executed",
    )
    snapshot = _add_attempt(
        snapshot,
        6,
        source_page_type="profile",
        terminal_status="executed",
    )

    state = evaluate_h5_business_page_popup_loop(
        snapshot=snapshot,
        location=_HOME_URL,
        page_type="home",
        current_candidates=(_popup_candidate(7),),
    )

    assert state is not None
    assert state.completed_count == 3


def test_popup_loop_does_not_count_policy_blocked_unselected_candidate() -> None:
    first = _popup_candidate(1)
    second = _popup_candidate(2)
    snapshot = H5TaskFrontierSnapshot.create(max_pages=1).merge(
        source_capture_id="capture-policy-1",
        source_state_version="state-policy-1",
        source_location=_HOME_URL,
        navigation_depth=0,
        candidates=(first,),
        now=_BASE_TIME,
    )
    snapshot = snapshot.merge(
        source_capture_id="capture-policy-2",
        source_state_version="state-policy-2",
        source_location=_HOME_URL,
        navigation_depth=0,
        candidates=(second,),
        parent_state_version="state-policy-1",
        now=_BASE_TIME + timedelta(seconds=1),
    )
    first_entry = next(
        item for item in snapshot.entries if item.candidate.candidate_id == first.candidate_id
    )
    snapshot = snapshot.select_entry(first_entry.entry_id, selected_at=_BASE_TIME)
    snapshot = snapshot.mark_executed(
        first_entry.entry_id,
        finished_at=_BASE_TIME + timedelta(seconds=1),
    )

    state = evaluate_h5_business_page_popup_loop(
        snapshot=snapshot,
        location=_HOME_URL,
        page_type="home",
        current_candidates=(_popup_candidate(3),),
    )

    assert state is not None
    assert state.completed_count == 0


def test_popup_loop_fails_closed_when_historical_source_page_type_is_missing() -> None:
    candidate = _popup_candidate(1)
    candidate = replace(
        candidate,
        metadata={
            key: value for key, value in candidate.metadata.items() if key != "source_page_type"
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="capture-popup-1",
        source_state_version="state-popup-1",
        source_location=_HOME_URL,
        navigation_depth=0,
        candidates=(candidate,),
        now=_BASE_TIME,
    )
    entry = snapshot.entries[0]
    snapshot = snapshot.select_entry(entry.entry_id, selected_at=_BASE_TIME)
    snapshot = snapshot.mark_executed(
        entry.entry_id,
        finished_at=_BASE_TIME + timedelta(seconds=1),
    )

    state = evaluate_h5_business_page_popup_loop(
        snapshot=snapshot,
        location=_HOME_URL,
        page_type="home",
        current_candidates=(_popup_candidate(2),),
    )

    assert state is not None
    assert state.completed_count == 0


@pytest.mark.parametrize(
    "page_type",
    ["line_loading", "startup_ad"],
)
def test_popup_loop_excludes_transient_page_types(page_type: str) -> None:
    assert (
        evaluate_h5_business_page_popup_loop(
            snapshot=_snapshot_with_attempts(20),
            location=_HOME_URL,
            page_type=page_type,
            current_candidates=(_popup_candidate(21),),
        )
        is None
    )


def test_popup_loop_does_not_activate_without_current_trusted_popup() -> None:
    navigation = replace(
        _popup_candidate(21),
        semantic_target="内容详情",
        metadata={"expected_next_page_type": "content_detail"},
    )

    assert (
        evaluate_h5_business_page_popup_loop(
            snapshot=_snapshot_with_attempts(20),
            location=_HOME_URL,
            page_type="home",
            current_candidates=(navigation,),
        )
        is None
    )


@pytest.mark.parametrize(
    "candidate",
    [
        replace(_popup_candidate(30), action_type=InteractionOperation.NAVIGATE),
        replace(_popup_candidate(31), risk_level=InteractionRiskLevel.HIGH),
        replace(_popup_candidate(32), navigation_depth=1),
        replace(_popup_candidate(33), semantic_target="关闭广告"),
        replace(
            _popup_candidate(34),
            metadata={
                **dict(_popup_candidate(34).metadata),
                "target_keywords": ["popup_dismissal", "dialog_content_close"],
            },
        ),
        replace(
            _popup_candidate(35),
            metadata={
                **dict(_popup_candidate(35).metadata),
                "action_parameters": {
                    **dict(_popup_candidate(35).metadata["action_parameters"]),
                    "obstruction_signature": "h5-obstruction:" + "f" * 64,
                },
            },
        ),
    ],
)
def test_popup_loop_rejects_untrusted_current_candidate(candidate: CandidateAction) -> None:
    assert (
        evaluate_h5_business_page_popup_loop(
            snapshot=H5TaskFrontierSnapshot.create(max_pages=None),
            location=_HOME_URL,
            page_type="home",
            current_candidates=(candidate,),
        )
        is None
    )


def test_popup_dismissal_reobserve_state_accumulates_same_signature() -> None:
    """无关闭控件的弹窗关闭失败重观察：同签名跨后继累加。"""
    candidate = _popup_candidate(1, signature="h5-obstruction:" + "a" * 64)

    first = next_popup_dismissal_reobserve_state(prior_state=None, candidate=candidate)
    assert first == {"signature": "h5-obstruction:" + "a" * 64, "count": 1, "streak": 1}

    second = next_popup_dismissal_reobserve_state(prior_state=first, candidate=candidate)
    assert second["count"] == 2
    assert second["streak"] == 2

    third = next_popup_dismissal_reobserve_state(prior_state=second, candidate=candidate)
    assert third["count"] == H5_POPUP_DISMISSAL_REOBSERVE_LIMIT


def test_popup_dismissal_reobserve_streak_survives_signature_drift() -> None:
    """倒计时/轮播弹窗每轮签名漂移（91PORN 新人特惠实锤）：
    同签名计数逐轮重置，但签名无关的 streak 持续累加并最终止损。"""
    state = None
    for sequence in range(1, H5_POPUP_DISMISSAL_STREAK_LIMIT + 1):
        drifting = _popup_candidate(sequence, signature=f"h5-obstruction:{sequence:064x}")
        state = next_popup_dismissal_reobserve_state(prior_state=state, candidate=drifting)
        assert state["count"] == 1  # 签名每轮都变，同签名计数始终 1

    assert state["streak"] == H5_POPUP_DISMISSAL_STREAK_LIMIT
    assert popup_dismissal_reobserve_exhausted(state) == (
        H5_POPUP_DISMISSAL_STREAK_LIMIT,
        H5_POPUP_DISMISSAL_STREAK_LIMIT,
    )


def test_popup_dismissal_reobserve_state_resets_count_on_different_signature() -> None:
    prior = {"signature": "h5-obstruction:" + "a" * 64, "count": 2, "streak": 2}
    other = _popup_candidate(2, signature="h5-obstruction:" + "b" * 64)

    state = next_popup_dismissal_reobserve_state(prior_state=prior, candidate=other)

    assert state == {"signature": "h5-obstruction:" + "b" * 64, "count": 1, "streak": 3}


def test_popup_dismissal_reobserve_state_ignores_non_popup_candidate() -> None:
    navigation = replace(
        _popup_candidate(3),
        semantic_target="内容详情",
    )
    assert next_popup_dismissal_reobserve_state(prior_state=None, candidate=navigation) is None


def test_popup_dismissal_reobserve_exhausted_threshold() -> None:
    assert popup_dismissal_reobserve_exhausted(None) is None
    assert popup_dismissal_reobserve_exhausted({"count": 1, "streak": 1}) is None
    assert popup_dismissal_reobserve_exhausted(
        {"count": H5_POPUP_DISMISSAL_REOBSERVE_LIMIT, "streak": 1}
    ) == (H5_POPUP_DISMISSAL_REOBSERVE_LIMIT, H5_POPUP_DISMISSAL_REOBSERVE_LIMIT)
    assert popup_dismissal_reobserve_exhausted(
        {"count": 1, "streak": H5_POPUP_DISMISSAL_STREAK_LIMIT}
    ) == (H5_POPUP_DISMISSAL_STREAK_LIMIT, H5_POPUP_DISMISSAL_STREAK_LIMIT)
    assert popup_dismissal_reobserve_exhausted({"count": True, "streak": True}) is None
