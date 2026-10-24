"""补采推进现场后，主探索不能继续消费旧 Capture 的能力。"""

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.h5_exploration_turn import H5ExplorationTurnRunner
from services.api.app.application.h5_exploration_workflow import _with_task_frontier
from services.api.app.application.h5_runtime_composition import H5CoordinatorFrontierRecovery
from services.api.app.application.page_capture_persistence import (
    PageCaptureRecoveryError,
    PageCaptureRecoveryService,
    PersistedEvidenceArtifact,
    PersistedPageRecoveryCapture,
)
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.interaction import SurfaceKind
from services.api.tests.test_h5_exploration_turn import (
    _capture_for_turn,
    _Continuation,
    _Executor,
    _Queue,
    _terminal_turn,
    _work,
)


async def test_runner_synchronizes_and_persists_source_before_deciding():
    work = _work()
    fresh = replace(
        work,
        source_capture_id="capture-after-recapture",
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                capture_id="capture-after-recapture",
            ),
        ),
    )
    queue = _Queue(work)
    calls = []

    async def synchronize(**kwargs):
        assert kwargs == {"work": work, "agent_run_id": "run-h5"}
        calls.append("synchronize")
        return fresh

    class Executor(_Executor):
        async def execute(self, *, work, agent_run_id):
            assert work == fresh
            assert queue.payload_updates[-1].work == fresh
            calls.append("execute")
            return await super().execute(work=work, agent_run_id=agent_run_id)

    await H5ExplorationTurnRunner(
        queue=queue,
        turn_executor=Executor(_terminal_turn(work)),
        continuation=_Continuation(_work(capture_id="next")),
        recovery=SimpleNamespace(synchronize=synchronize),
        lease_owner="h5-test",
    ).run_one(task_id="task-h5", agent_run_id="run-h5")
    assert calls == ["synchronize", "execute"]


@pytest.mark.parametrize("fault", ["task", "session", "budget", "deadline"])
async def test_runner_rejects_synchronizer_cross_scope_or_budget_changes(fault):
    work = _work()
    values = {
        "task": {"task_id": "other", "work_id": _work(capture_id="other").work_id},
        "session": {"session_id": "h5:other"},
        "budget": {
            "budget": replace(work.budget, remaining_actions=2, remaining_business_actions=2)
        },
        "deadline": {"deadline": work.frontier.observation.observed_at},
    }
    invalid = replace(work, **values[fault]) if fault != "task" else _work(capture_id="other")
    executor = SimpleNamespace(execute=AsyncMock())
    await H5ExplorationTurnRunner(
        queue=_Queue(work),
        turn_executor=executor,
        continuation=_Continuation(_work(capture_id="next")),
        recovery=SimpleNamespace(synchronize=AsyncMock(return_value=invalid)),
        lease_owner="h5-test",
    ).run_one(task_id="task-h5", agent_run_id="run-h5")
    executor.execute.assert_not_awaited()


@pytest.mark.parametrize("changed_page", [False, True])
@pytest.mark.parametrize("new_entry", [False, True])
@pytest.mark.parametrize("missing_selected", [False, True])
@pytest.mark.parametrize("logical_identity", ["off", "same", "changed"])
async def test_production_sync_rebinds_same_page_or_preserves_entry_for_restore(
    monkeypatch, changed_page, new_entry, missing_selected, logical_identity,
):
    from services.api.app.application import h5_runtime_composition as composition

    work = _work()
    work = replace(work, frontier=replace(work.frontier, observation=replace(
        work.frontier.observation, page_type="home",
    )))
    identity = "sha256:" + "1" * 64
    fresh_identity = "sha256:" + ("2" if logical_identity == "changed" else "1") * 64
    if logical_identity != "off":
        work = replace(work, frontier=replace(work.frontier, candidates=(replace(
            work.frontier.candidates[0], metadata={
                "logical_target_required": True,
                "source_identity_fingerprint": identity,
                "source_state_fingerprint": "sha256:" + "3" * 64,
                "source_page_type": "home",
            },
        ),)))
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=work.frontier.observation.state_version,
        source_location=work.frontier.observation.location,
        navigation_depth=0,
        candidates=(
            work.frontier.candidates[0],
            replace(
                work.frontier.candidates[0],
                candidate_id="sibling",
                semantic_target="其他入口",
            ),
        ),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    original = _with_task_frontier(frontier=work.frontier, snapshot=snapshot)
    work = replace(work, frontier=original, frontier_snapshot_id=original.snapshot_id)
    capture = _capture_for_turn(work, "fresh")
    fresh = replace(
        work.frontier,
        observation=replace(
            work.frontier.observation,
            capture_id="fresh",
            state_version="state:fresh",
            location="https://m.example.test/other"
            if changed_page
            else work.frontier.observation.location,
        ),
        candidates=(
            replace(
                work.frontier.candidates[0],
                candidate_id="fresh-candidate",
                semantic_target="另一页按钮" if changed_page or missing_selected else "我的",
            ),
        ),
    )
    if new_entry:
        fresh = replace(fresh, candidates=(*fresh.candidates, replace(
            fresh.candidates[0], candidate_id="new-category", semantic_target="新发现的分类入口",
        )))
    if logical_identity != "off":
        fresh = replace(fresh, observation=replace(fresh.observation, metadata={
            **fresh.observation.metadata, "logical_targets_effective": True,
            "logical_surface_identity_fingerprint": fresh_identity,
            "logical_surface_state_fingerprint": "sha256:" + "4" * 64,
        }), candidates=tuple(replace(candidate, metadata={
            **candidate.metadata, "source_identity_fingerprint": fresh_identity,
            "source_state_fingerprint": "sha256:" + "4" * 64,
        }) for candidate in fresh.candidates))
    monkeypatch.setattr(composition, "build_h5_frontier", lambda **_: fresh)
    session = SimpleNamespace(
        task_id=work.task_id,
        surface_kind=SurfaceKind.WEB,
        status=SimpleNamespace(value="active"),
        session_owner="h5-workflow",
        current_capture_id="fresh",
        finalization_epoch=0,
        command_sequence=8,
        row_version=1,
    )
    renewed = SimpleNamespace(**{**vars(session), "row_version": 2})
    captures = SimpleNamespace(
        get_verified_observation_capture=AsyncMock(return_value=SimpleNamespace(capture=capture))
    )
    result = await H5CoordinatorFrontierRecovery(
        capture_coordinator=object(),
        detector=SimpleNamespace(detect=AsyncMock(return_value={})),
        assessment_repository=object(),
        surface_sessions=SimpleNamespace(
            get=AsyncMock(side_effect=[session, renewed]),
        ),
        captures=captures,
    ).synchronize(work=work, agent_run_id="run-h5")
    assert result.source_capture_id == "fresh"
    assert result.budget == work.budget and result.deadline == work.deadline
    updated = H5TaskFrontierSnapshot.from_dict(
        result.frontier.observation.metadata["h5_task_frontier"]
    )
    same_source = not changed_page and logical_identity != "changed"
    assert updated.pending_count == 1 + (int(new_entry) + int(missing_selected)) * same_source
    selected = next(entry for entry in updated.entries if entry.status.value == "selected")
    assert selected.entry_id == snapshot.entries[0].entry_id
    if changed_page or missing_selected or logical_identity == "changed":
        assert result.frontier.observation.metadata["h5_frontier_source_recovery_pending"]
        assert selected.source_capture_id == work.source_capture_id
    else:
        assert selected.source_capture_id == "fresh"
        assert selected.candidate.candidate_id == "fresh-candidate"
        if new_entry:
            discovered = next(entry for entry in updated.pending_entries
                              if entry.candidate.candidate_id == "new-category")
            assert discovered.source_capture_id == "fresh"
    if new_entry and same_source:
        assert any(entry.candidate.candidate_id == "new-category"
                   and entry.source_capture_id == "fresh" for entry in updated.pending_entries)


@pytest.mark.parametrize("fault", [
    None, "blocked", "incomplete", "other_identity", "other_page", "business_target",
    "untrusted_transition", "truncated_clear", "census_incomplete", "census_nonempty",
])
async def test_sync_retires_only_proven_absent_same_page_popup(monkeypatch, fault):
    from services.api.app.application import h5_runtime_composition as composition

    work = _work()
    identity = "sha256:" + "1" * 64
    popup = replace(work.frontier.candidates[0], semantic_target="关闭当前弹窗", metadata={
        "logical_target_required": True, "source_identity_fingerprint": identity,
        "source_page_type": "home", "source_page_state": "popup",
        "action_parameters": {"target_mode": "popup_dismissal"},
        "target_keywords": ["popup_dismissal", "close_control"],
    })
    if fault == "business_target":
        popup = replace(popup, metadata={**popup.metadata, "action_parameters": {
            "target_mode": "bottom_navigation",
        }})
    original = replace(work.frontier, observation=replace(
        work.frontier.observation, page_type="home", page_state="popup",
    ), candidates=(popup,))
    snapshot = H5TaskFrontierSnapshot.create(max_pages=None).merge(
        source_capture_id=work.source_capture_id,
        source_state_version=original.observation.state_version,
        source_location=original.observation.location, navigation_depth=0,
        candidates=(popup,),
    )
    snapshot = snapshot.select_entry(snapshot.pending_entries[0].entry_id)
    original = _with_task_frontier(frontier=original, snapshot=snapshot)
    work = replace(work, frontier=original, frontier_snapshot_id=original.snapshot_id)
    fresh = replace(original, observation=replace(
        original.observation, capture_id="fresh", state_version="state:fresh",
        page_type="profile" if fault == "other_page" else "home", page_state="stable",
        metadata={
            "logical_surface_identity_fingerprint": (
                "other" if fault == "other_identity" else identity
            ),
            "logical_transition_proof": (
                "insufficient" if fault == "untrusted_transition" else "trusted"
            ),
            "obstruction_snapshot_evidence_status": (
                "incomplete" if fault in {
                    "incomplete", "truncated_clear", "census_incomplete", "census_nonempty",
                } else "ready"
            ),
            "obstruction_modal_census_complete": fault in {"truncated_clear", "census_nonempty"},
            "obstruction_modal_census_count": 1 if fault == "census_nonempty" else 0,
            "obstruction_blocking": fault == "blocked",
        },
    ), candidates=(replace(work.frontier.candidates[0], candidate_id="new-category",
                           semantic_target="新发现的分类入口", metadata={}),))
    monkeypatch.setattr(composition, "build_h5_frontier", lambda **_: fresh)
    session = SimpleNamespace(
        task_id=work.task_id, surface_kind=SurfaceKind.WEB, status=SimpleNamespace(value="active"),
        session_owner="h5-workflow", current_capture_id="fresh", finalization_epoch=0,
        command_sequence=8,
    )
    result = await H5CoordinatorFrontierRecovery(
        capture_coordinator=object(), detector=SimpleNamespace(detect=AsyncMock(return_value={})),
        assessment_repository=object(),
        surface_sessions=SimpleNamespace(get=AsyncMock(return_value=session)),
        captures=SimpleNamespace(get_verified_observation_capture=AsyncMock(
            return_value=SimpleNamespace(capture=_capture_for_turn(work, "fresh")),
        )),
    ).synchronize(work=work, agent_run_id="run-h5")
    updated = H5TaskFrontierSnapshot.from_dict(
        result.frontier.observation.metadata["h5_task_frontier"]
    )
    old = next(entry for entry in updated.entries if entry.entry_id == snapshot.entries[0].entry_id)
    assert result.budget == work.budget and result.deadline == work.deadline
    if fault in {None, "truncated_clear"}:
        assert old.status.value == "skipped"
        assert old.last_failure_reason == "popup_obstruction_absent_on_stable_page"
        selected = next(entry for entry in updated.entries if entry.status.value == "selected")
        assert selected.candidate.candidate_id == "new-category"
        assert selected.source_capture_id == "fresh"
        assert not result.frontier.observation.metadata.get("h5_frontier_source_recovery_pending")
    else:
        assert old.status.value == "selected" and old.source_capture_id == work.source_capture_id
        assert result.frontier.observation.metadata["h5_frontier_source_recovery_pending"]


@pytest.mark.parametrize("fault", [
    "owner", "capture_scope", "closed", "changed_during_detection",
    "changed_epoch", "changed_owner", "changed_status",
])
async def test_production_sync_rejects_untrusted_or_concurrently_changed_surface(fault):
    work = _work()
    session = SimpleNamespace(
        task_id=work.task_id,
        surface_kind=SurfaceKind.WEB,
        status=SimpleNamespace(value="closed" if fault == "closed" else "active"),
        session_owner="other" if fault == "owner" else "h5-workflow",
        current_capture_id="fresh",
        finalization_epoch=0,
        command_sequence=8,
    )
    capture = _capture_for_turn(work, "fresh")
    if fault == "capture_scope":
        capture = replace(capture, task_id="other")
    latest = SimpleNamespace(**{**vars(session), "command_sequence": 9})
    if fault == "changed_epoch":
        latest.finalization_epoch = 1
    if fault == "changed_owner":
        latest.session_owner = "other"
    if fault == "changed_status":
        latest.status = SimpleNamespace(value="closed")
    recovery = H5CoordinatorFrontierRecovery(
        capture_coordinator=object(),
        detector=SimpleNamespace(detect=AsyncMock(return_value={})),
        assessment_repository=object(),
        surface_sessions=SimpleNamespace(
            get=AsyncMock(side_effect=[session, latest]),
        ),
        captures=SimpleNamespace(
            get_verified_observation_capture=AsyncMock(return_value=SimpleNamespace(capture=capture))
        ),
    )
    with pytest.raises(ValueError) as raised:
        await recovery.synchronize(work=work, agent_run_id="run-h5")
    if fault == "changed_during_detection":
        assert raised.value.retryable is True
        assert raised.value.error_code == "H5_SOURCE_OBSERVATION_MOVED"
        executor = SimpleNamespace(execute=AsyncMock())
        result = await H5ExplorationTurnRunner(
            queue=_Queue(work), turn_executor=executor,
            continuation=_Continuation(_work(capture_id="next")),
            recovery=SimpleNamespace(synchronize=AsyncMock(side_effect=raised.value)),
            lease_owner="h5-test",
        ).run_one(task_id="task-h5", agent_run_id="run-h5")
        assert result.final_status.value == "retry_wait"
        executor.execute.assert_not_awaited()
    else:
        assert getattr(raised.value, "retryable", False) is False


@pytest.mark.parametrize("quality", ["pending", "pass"])
async def test_observation_reader_verifies_committed_capture_without_formal_evidence(quality):
    raw = b"captured-page"
    artifact = PersistedEvidenceArtifact(
        artifact_id="artifact-page", artifact_type="screenshot", storage_path="page.png",
        content_type="image/png", checksum=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        size_bytes=len(raw), metadata={},
    )
    capture = replace(_capture_for_turn(_work(), "fresh"), artifact_ids=(artifact.artifact_id,))
    persisted = PersistedPageRecoveryCapture(
        capture=capture, artifacts=(artifact,), manifest_quality_status=quality,
    )
    service = PageCaptureRecoveryService(
        repository=SimpleNamespace(get_recovery_capture=AsyncMock(return_value=persisted)),
        artifact_store=SimpleNamespace(read=AsyncMock(return_value=raw)),
    )
    assert await service.get_verified_observation_capture(capture_id="fresh") == persisted
    if quality == "pending":
        with pytest.raises(PageCaptureRecoveryError):
            await service.get_verified_capture(capture_id="fresh")
    else:
        assert await service.get_verified_capture(capture_id="fresh") == persisted


@pytest.mark.parametrize("fault", [
    "fail", "missing_quality", "checksum", "scope", "web_scope", "attachments",
    "capture_identity", "empty_artifacts", "size", "binary", "unreadable",
])
async def test_observation_reader_does_not_accept_failed_or_corrupt_capture(fault):
    raw = b"captured-page"
    artifact = PersistedEvidenceArtifact(
        artifact_id="artifact-page", artifact_type="screenshot", storage_path="page.png",
        content_type="image/png", checksum=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        size_bytes=len(raw), metadata={},
    )
    capture = replace(_capture_for_turn(_work(), "fresh"), artifact_ids=(artifact.artifact_id,))
    if fault == "scope":
        capture = replace(capture, session_id="app:other")
    elif fault == "web_scope":
        capture = replace(capture, session_id="web-v2:other")
    elif fault == "attachments":
        capture = replace(capture, artifact_ids=("another",))
    elif fault == "capture_identity":
        capture = replace(capture, capture_id="another")
    elif fault == "size":
        artifact = replace(artifact, size_bytes=len(raw) + 1)
    persisted = PersistedPageRecoveryCapture(
        capture=capture, artifacts=() if fault == "empty_artifacts" else (artifact,),
        manifest_quality_status=(
            "fail" if fault == "fail"
            else None if fault == "missing_quality" else "pending"
        ),
    )
    service = PageCaptureRecoveryService(
        repository=SimpleNamespace(get_recovery_capture=AsyncMock(return_value=persisted)),
        artifact_store=SimpleNamespace(read=AsyncMock(
            return_value=(
                b"changed" if fault == "checksum" else "not-bytes" if fault == "binary" else raw
            ),
            side_effect=OSError("missing file") if fault == "unreadable" else None,
        )),
    )
    with pytest.raises(PageCaptureRecoveryError):
        await service.get_verified_observation_capture(capture_id="fresh")


async def test_observation_reader_preserves_missing_capture():
    store = SimpleNamespace(read=AsyncMock())
    service = PageCaptureRecoveryService(
        repository=SimpleNamespace(get_recovery_capture=AsyncMock(return_value=None)),
        artifact_store=store,
    )
    assert await service.get_verified_observation_capture(capture_id="missing") is None
    store.read.assert_not_awaited()
