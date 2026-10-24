"""定向发现审计合同单测。"""

from __future__ import annotations

from services.api.app.application.app_directed_discovery import (
    HARD_APPLICABILITY_PAGE_TYPES,
    OUTCOME_NO_ENTRY,
    append_directed_discovery_attempt,
    guide_page_markers_present,
    hard_targets_missing_entry_on_source,
    has_directed_outcome,
    merge_blocked_page_types,
    record_no_entry_attempts,
)


def test_hard_targets_missing_entry_on_source_mapping() -> None:
    assert hard_targets_missing_entry_on_source(
        current_page_type="profile",
        missing_page_types={"payment", "post_publish"},
        analysis_has_entry_for=set(),
    ) == ("payment",)
    assert hard_targets_missing_entry_on_source(
        current_page_type="video_list",
        missing_page_types={"video_detail"},
        analysis_has_entry_for={"video_detail"},
    ) == ()


def test_append_directed_discovery_attempt_dedupes_same_capture_outcome() -> None:
    first = append_directed_discovery_attempt(
        (),
        page_type="payment",
        source_page_type="profile",
        source_capture_id="cap-1",
        outcome=OUTCOME_NO_ENTRY,
    )
    second = append_directed_discovery_attempt(
        first,
        page_type="payment",
        source_page_type="profile",
        source_capture_id="cap-1",
        outcome=OUTCOME_NO_ENTRY,
    )
    assert len(second) == 1
    assert HARD_APPLICABILITY_PAGE_TYPES == {"payment", "post_publish", "video_detail"}


def test_guide_page_markers_present() -> None:
    assert guide_page_markers_present(
        page_type=None,
        surface_facts={"visible_texts": ["安卓下载", "苹果下载"]},
    )
    assert not guide_page_markers_present(
        page_type="home",
        surface_facts={"visible_texts": ["首页推荐"]},
    )


def test_merge_blocked_and_no_entry_helpers() -> None:
    blocked = merge_blocked_page_types(("payment",), extra=("payment", "post_publish"))
    assert blocked == ["payment", "post_publish"]
    attempts = record_no_entry_attempts(
        (),
        page_types=("post_publish",),
        source_page_type="post_list",
        source_capture_id="cap-2",
    )
    assert has_directed_outcome(
        attempts,
        page_type="post_publish",
        outcomes={OUTCOME_NO_ENTRY},
    )
