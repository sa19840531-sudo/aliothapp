"""历史栏目漂移时保留真实新入口，不把同 URL 当作内容等价。"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application import h5_runtime_composition as composition
from services.api.app.application.h5_exploration_recovery import H5RestorationResult
from services.api.app.application.h5_exploration_workflow import _with_task_frontier
from services.api.app.domain.exploration.h5_frontier import (
    H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT,
    H5TaskFrontierSnapshot,
    retire_h5_skipped_entries,
)
from services.api.tests.test_h5_exploration_turn import _capture_for_turn, _work


@pytest.mark.parametrize(
    "fault",
    [
        None,
        "other_origin",
        "rejected",
        "first_attempt",
        "other_source",
        "assessment_capture",
        "assessment_type",
        "loading",
        "overlay",
        "auth",
    ],
)
async def test_failed_column_restore_preserves_new_frontier_and_coalesces_exact_source(
    monkeypatch, fault
):
    base = _work()
    candidate = replace(
        base.frontier.candidates[0],
        metadata={
            "source_page_type": "home",
            "source_identity_fingerprint": "home-column",
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=base.source_capture_id,
        source_state_version=base.frontier.observation.state_version,
        source_location=base.frontier.observation.location,
        navigation_depth=0,
        candidates=(
            candidate,
            replace(candidate, candidate_id="other-tab", semantic_target="其他栏目"),
        ),
    )
    if fault == "other_source":
        snapshot = snapshot.merge(
            source_capture_id="other-capture",
            source_state_version="other-state",
            source_location=base.frontier.observation.location,
            navigation_depth=0,
            candidates=(
                replace(candidate, candidate_id="independent", semantic_target="独立来源"),
            ),
        )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    entry = next(item for item in snapshot.entries if item.status.value == "selected")
    if fault != "first_attempt":
        snapshot = snapshot.record_restoration_attempt(entry.entry_id)
        entry = next(item for item in snapshot.entries if item.entry_id == entry.entry_id)
    frontier = _with_task_frontier(frontier=base.frontier, snapshot=snapshot)
    work = replace(base, frontier=frontier, frontier_snapshot_id=frontier.snapshot_id)
    capture = _capture_for_turn(work, "restored-frame")
    fresh = replace(
        base.frontier,
        observation=replace(
            base.frontier.observation,
            capture_id=capture.capture_id,
            state_version="fresh-state",
            location="https://outside.test/" if fault == "other_origin" else entry.source_location,
            page_type="home",
            page_state="loading" if fault == "loading" else "stable",
            overlay_signature="active" if fault == "overlay" else None,
            metadata={
                "detection_status": "recognized",
                "logical_surface_identity_fingerprint": "home-default",
                "requires_auth": fault == "auth",
            },
        ),
        candidates=(
            replace(
                candidate,
                candidate_id="fresh-settings",
                semantic_target="设置入口",
                metadata={"expected_next_page_type": "settings"},
            ),
        ),
    )
    result = H5RestorationResult(
        entry_id=entry.entry_id,
        restored=False,
        capture=capture,
        detection={"page_capture_id": capture.capture_id},
        route="navigate",
        reason_code="h5_frontier_restore_state_mismatch"
        if fault != "rejected"
        else "h5_frontier_restore_assessment_rejected",
        restoration_attempt_count=1 if fault == "first_attempt" else 2,
        assessment=SimpleNamespace(
            execution_status="accepted",
            page_type_assessment={
                "status": "covered",
                "page_type": "content_detail" if fault == "assessment_type" else "home",
                "page_capture_ids": [
                    "other-capture" if fault == "assessment_capture" else capture.capture_id
                ],
            },
        ),
    )
    monkeypatch.setattr(composition, "restore_h5_frontier_source", AsyncMock(return_value=result))
    monkeypatch.setattr(composition, "build_h5_frontier", lambda **_: fresh)
    recovery = composition.H5CoordinatorFrontierRecovery(
        capture_coordinator=object(),
        detector=object(),
        assessment_repository=object(),
    )
    outcome = await recovery.restore(work=work, agent_run_id="run-h5", entry=entry)
    updated = H5TaskFrontierSnapshot.from_dict(
        outcome.work.frontier.observation.metadata["h5_task_frontier"]
    )
    old = next(item for item in updated.entries if item.candidate.candidate_id == "other-tab")
    eligible = fault not in {
        "other_origin",
        "rejected",
        "assessment_capture",
        "assessment_type",
        "loading",
        "overlay",
        "auth",
    }
    assert any(item.source_capture_id == capture.capture_id for item in updated.entries) is eligible
    if eligible and fault != "first_attempt":
        assert old.status.value == "skipped"
        assert old.restoration_attempt_count == 0
        assert old.last_failure_reason == "h5_shared_source_restore_exhausted"
        chosen = next(item for item in updated.entries if item.status.value == "selected")
        assert chosen.source_capture_id == capture.capture_id
        assert (
            outcome.work.frontier.observation.metadata.get("h5_frontier_source_recovery_pending")
            is not True
        )
    else:
        assert old.status.value != "skipped"
    if fault == "other_source":
        independent = next(
            item for item in updated.entries if item.candidate.candidate_id == "independent"
        )
        assert independent.status.value in {"pending", "selected"}
    assert outcome.work.budget == work.budget
    assert outcome.work.deadline == work.deadline
    assert outcome.restored is False


@pytest.mark.parametrize("h5", [True, False])
def test_full_panel_text_cannot_be_h5_content_target(h5):
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.semantic_target_resolver import (
        SemanticTargetResolutionError,
        _select_unique_target,
    )

    snapshot = {
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#content-panel",
                "tag": "div",
                "role": "tabpanel",
                "visible": True,
                "interactive": True,
                "text": "内容标题",
                "rect": {"x": 0, "y": 355, "width": 390, "height": 1081},
                "hit_test": {"center_hits_target": False},
            }
        ],
    }
    kwargs = dict(
        snapshot=snapshot,
        semantic_target="内容标题",
        operation=InteractionOperation.CLICK,
        h5_navigation_target="内容标题" if h5 else None,
    )
    if h5:
        with pytest.raises(SemanticTargetResolutionError):
            _select_unique_target(**kwargs)
    else:
        assert _select_unique_target(**kwargs) == "#content-panel"


@pytest.mark.parametrize("identity", ["home-column", "other-column"])
def test_shared_unreachable_source_is_not_a_permanent_business_branch_ban(identity):
    base = _work()
    candidate = replace(
        base.frontier.candidates[0],
        metadata={
            "source_identity_fingerprint": "home-column",
            "source_page_type": "home",
            "source_page_state": "stable",
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="old",
        source_state_version="old-state",
        source_location=base.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    snapshot = snapshot.mark_pending_skipped(
        snapshot.pending_entries[0].entry_id,
        reason="h5_shared_source_restore_exhausted",
        active_state_version="fresh-state",
    )
    refreshed = snapshot.merge(
        source_capture_id="new",
        source_state_version="new-state",
        source_location=base.frontier.observation.location,
        navigation_depth=0,
        candidates=(
            replace(
                candidate,
                candidate_id="new",
                metadata={
                    **candidate.metadata,
                    "source_identity_fingerprint": identity,
                },
            ),
        ),
    )
    assert bool(refreshed.pending_entries) is (identity == "home-column")


def test_shared_source_refresh_keeps_existing_retry_limit_across_captures():
    base = _work()
    candidate = replace(
        base.frontier.candidates[0],
        metadata={
            "source_identity_fingerprint": "home-column",
            "source_page_type": "home",
            "source_page_state": "stable",
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="old",
        source_state_version="old-state",
        source_location=base.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    for index in range(H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT + 1):
        pending = snapshot.pending_entries[0]
        snapshot = snapshot.mark_pending_skipped(
            pending.entry_id,
            reason="h5_shared_source_restore_exhausted",
            active_state_version=f"state-{index}",
        ).merge(
            source_capture_id=f"capture-{index}",
            source_state_version=f"state-{index}",
            source_location=base.frontier.observation.location,
            navigation_depth=0,
            candidates=(replace(candidate, candidate_id=f"candidate-{index}"),),
        )
        assert bool(snapshot.pending_entries) is (index < H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT)
    assert (
        snapshot.entries[-1].candidate.metadata["source_refresh_attempt_count"]
        == H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT
    )


@pytest.mark.parametrize("proved", [True, False])
def test_same_named_entry_on_different_proved_object_is_not_banned_with_old_source(proved):
    base = _work()
    candidate = replace(
        base.frontier.candidates[0],
        metadata={
            "logical_target_required": True,
            "source_identity_fingerprint": "sha256:" + "a" * 64,
            "source_state_fingerprint": "sha256:" + "c" * 64,
            "source_page_type": "home",
            "source_page_state": "stable",
        },
    )
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id="old",
        source_state_version="old-state",
        source_location=base.frontier.observation.location,
        navigation_depth=0,
        candidates=(candidate,),
    )
    old_entry_id = snapshot.pending_entries[0].entry_id
    snapshot = snapshot.mark_pending_skipped(
        old_entry_id,
        reason="h5_shared_source_restore_exhausted",
        active_state_version="fresh-state",
    ).merge(
        source_capture_id="fresh",
        source_state_version="fresh-state",
        source_location=base.frontier.observation.location,
        navigation_depth=0,
        candidates=(
            replace(
                candidate,
                candidate_id="fresh",
                metadata={
                    **candidate.metadata,
                    "logical_target_required": proved,
                    "source_identity_fingerprint": "sha256:" + "b" * 64,
                    "source_state_fingerprint": "sha256:" + "d" * 64,
                },
            ),
        ),
    )
    assert bool(snapshot.pending_entries) is proved
    old = next(item for item in snapshot.entries if item.entry_id == old_entry_id)
    assert old.status.value == "skipped" and old.source_capture_id == "old"
    if proved:
        assert snapshot.pending_entries[0].source_capture_id == "fresh"
        assert snapshot.pending_entries[0].restoration_attempt_count == 0
        new_entry_id = snapshot.pending_entries[0].entry_id
        fresh = snapshot.pending_entries[0].candidate
        snapshot = snapshot.select_entry(new_entry_id).mark_executed(new_entry_id)
        snapshot = retire_h5_skipped_entries(snapshot, max_retained_skipped=0)
        assert any(item.entry_id == old_entry_id for item in snapshot.entries)
        snapshot = snapshot.merge(
            source_capture_id="fresh-again",
            source_state_version="fresh-again-state",
            source_location=base.frontier.observation.location,
            navigation_depth=0,
            candidates=(replace(fresh, candidate_id="fresh-again"),),
        )
        assert not snapshot.pending_entries
