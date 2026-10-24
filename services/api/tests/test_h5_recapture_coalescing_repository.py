"""同缺口收敛要求旧 Probe 实际来源、合同和制品也未变化。"""

from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from services.api.app.infrastructure.db.h5_recapture_coalescing_repository import (
    PostgresH5RecaptureCoalescingRepository,
    _facts,
)
from services.api.app.infrastructure.db.h5_shared_probe_repository import (
    PostgresH5SharedProbeEvidenceReader,
)
from services.api.tests.test_h5_probe_completion_repository import _Connection, _Pool
from services.api.tests.test_h5_recapture_coalescing import probe, request


def facts():
    return dict(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        session_id="h5:session-1",
        logical_page_key="same-object",
        state_fingerprint="same-state",
        identity_status="confirmed",
        is_overlay=False,
        is_external=False,
        page_state="stable",
        contract_hash="rule-contract",
        ruleset_snapshot_hash="ruleset",
        manifest_payload={
            "artifacts": [
                {
                    "artifact_type": "ui_snapshot",
                    "role": "after_action",
                    "checksum": "same-bytes",
                    "size_bytes": 12,
                    "metadata": {},
                }
            ]
        },
    )


@pytest.mark.parametrize("change", [
    None, "bytes", "contract", "source", "no_proof", "loading", "single", "copied_frame",
])
async def test_gap_history_checks_failed_probe_real_source(change):
    first, current = request(required_artifact_types=("ui_snapshot",)), request(
        capture="new-frame", required_artifact_types=("ui_snapshot",))
    earlier = request(capture="older-frame", required_artifact_types=("ui_snapshot",))
    previous = {
        **facts(),
        "request_id": first.request_id,
        "request_payload": first.to_dict(),
        "failed_source_facts": {**facts(), "capture_id": "first-independent-observation"},
    }
    older = {**facts(), "request_id": earlier.request_id, "request_payload": earlier.to_dict(),
             "failed_source_facts": {**facts(), "capture_id": "second-independent-observation"}}
    newest = {
        **facts(),
        "request_id": current.request_id,
        "request_payload": current.to_dict(),
        "failed_source_facts": None,
    }
    if change == "bytes":
        newest["manifest_payload"] = deepcopy(newest["manifest_payload"])
        newest["manifest_payload"]["artifacts"][0]["checksum"] = "changed-text"
    if change == "contract":
        newest["contract_hash"] = "new-version"
    if change == "source":
        previous["failed_source_facts"]["logical_page_key"] = "wrong-restored-page"
    if change == "no_proof":
        previous["failed_source_facts"] = None
    if change == "loading":
        newest["page_state"] = "loading"
    if change == "copied_frame":
        older["failed_source_facts"]["capture_id"] = "first-independent-observation"
    connection = _Connection()

    async def fetch(query, *args):
        connection.query = query
        connection.args = args
        return [newest, previous] if change == "single" else [newest, previous, older]

    connection.fetch = fetch
    result = await PostgresH5RecaptureCoalescingRepository(
        pool=_Pool(connection)
    ).has_unchanged_gap(
        request=current,
        agent_run_id="run-h5",
        finalization_epoch=4,
    )
    assert result == (",".join(sorted([first.request_id, earlier.request_id]))
                      if change is None else None)
    assert "request.status = 'unfulfillable'" in connection.query
    assert "'h5_probe_terminal:blocked', 'h5_probe_terminal:no_progress'" in connection.query
    assert "probe.status IN ('blocked', 'no_progress')" in connection.query
    assert "source_state.surface_session_id = probe.session_id" in connection.query
    assert "source.surface_session_id = probe.session_id" in connection.query
    assert "probe.last_error_code IS NULL" in connection.query
    assert "probe.terminal_payload->>'status' = probe.status" in connection.query
    # “采集成功”不是“缺口消失”；已挂接的真实结果也可证明重复补采没有新增内容。
    assert "request.status = 'satisfied'" in connection.query
    assert "source.action_execution_id = action.action_execution_id" in connection.query
    assert "attached.capture_id = source.id" in connection.query
    assert "supplement.probe_work_id = probe.work_id" in connection.query


@pytest.mark.parametrize("change", [None, "object", "goal", "source", "missing"])
async def test_shared_pair_keeps_exact_goal_and_real_action_scope(change):
    follower = probe(request(rule="two"))
    leader = probe(request())
    if change == "object":
        leader = probe(request(capture="different-original"))
    if change == "goal":
        leader = probe(request(evidence_goal="等待广告加载失败的页面"))
    if change == "source":
        leader = probe(request(), source_capture_id="different-restored")
    row = {
        "origin_work_id": leader.work_id,
        "work_payload": leader.to_dict(),
        "result_capture_id": "actual-result",
        "before_capture_id": "actual-before",
        "agent_run_id": "run-h5",
        "session_id": follower.session_id,
    }
    connection = _Connection()

    async def fetch(query, *args):
        connection.query = query
        connection.args = args
        return [] if change == "missing" else [row]

    connection.fetch = fetch
    result = await PostgresH5SharedProbeEvidenceReader(pool=_Pool(connection)).find(
        work=follower,
        agent_run_id="run-h5",
    )
    assert bool(result) is (change is None)
    for guard in (
        "task.target_platform = 'h5'",
        "session.current_capture_id = result.id",
        "session.command_sequence = action.command_sequence",
        "follower.created_at <= action.created_at",
        "restoration.assessment_payload->>'passed' = 'true'",
        "action.intent_payload->>'action_type' = 'observe'",
        "result.capture_role = 'probe_result_replay'",
        "before_frame.capture_role = 'probe_before'",
        "state.is_overlay IS DISTINCT FROM false",
        "state.is_external IS DISTINCT FROM false",
    ):
        assert guard in connection.query


@pytest.mark.parametrize("available", [True, False])
async def test_stale_shared_work_requeue_is_fenced_and_does_not_reset_attempt_budget(available):
    connection = _Connection()
    connection.transaction = lambda: _Pool(connection).acquire()
    connection.execute = AsyncMock()
    connection.fetchval = AsyncMock(return_value=available)
    item = probe(request())
    reader = PostgresH5SharedProbeEvidenceReader(pool=_Pool(connection))
    result = await reader.release_stale_unexecuted(
        work=item, agent_run_id="run-h5", lease_token="held-token", row_version=3,
    )
    assert result is available
    query, *args = connection.fetchval.await_args.args
    assert args == [item.task_id, item.work_id, "run-h5", "held-token", 3]
    for guard in (
        "task.target_platform = 'h5'", "work.lease_token = $4::uuid AND work.row_version = $5",
        "work.lease_expires_at > now()", "request.status IN ('routed', 'running')",
        "session.current_capture_id <> source.id", "NOT EXISTS (SELECT 1 FROM qc_action_execution",
        "ELSE 'pending' END", "status = 'superseded'",
        "request.attempt_count >= request.max_attempts THEN 'unfulfillable'",
        "request.expires_at <= now() THEN 'expired'",
    ):
        assert guard in query
    assert "attempt_count =" not in query
    assert "max_attempts =" not in query


@pytest.mark.parametrize("change", ["provenance", "har", "content", "visibility", "missing"])
def test_scoped_gap_hash_keeps_required_content_not_capture_provenance(change):
    first = facts()
    first["manifest_payload"]["artifacts"][0]["metadata"] = {
        "presentation": {"document_epoch_ms": 100, "blocking_scope": "none"},
        "session_auth": "anonymous",
    }
    second = deepcopy(first)
    artifact = second["manifest_payload"]["artifacts"][0]
    if change == "provenance":
        artifact["role"] = "probe_result_replay"
        artifact["metadata"]["presentation"]["document_epoch_ms"] = 200
    elif change == "har":
        second["manifest_payload"]["artifacts"].append(
            {"artifact_type": "har", "checksum": "fresh-network-clock", "metadata": {}}
        )
    elif change == "content":
        artifact["checksum"] = "changed-condition"
    elif change == "visibility":
        artifact["metadata"]["presentation"]["blocking_scope"] = "page"
    else:
        second["manifest_payload"]["artifacts"] = []
    assert (_facts(first, required_artifacts=("ui_snapshot",))
            == _facts(second, required_artifacts=("ui_snapshot",))) is (
                change in {"provenance", "har"}
            )


@pytest.mark.parametrize("deferred", [(), ("snapshot-deferred",)])
@pytest.mark.parametrize("exact", [None, "exact-request"])
async def test_exact_claim_filter_is_h5_only_and_keeps_app_web_default_parameters(deferred, exact):
    from services.api.app.infrastructure.db.evidence_request_repository import (
        PostgresEvidenceRequestRepository,
    )
    from services.api.tests.test_restoration_persistence import FakeConnection, FakePool

    connection = FakeConnection()
    repository = PostgresEvidenceRequestRepository(pool=FakePool(connection))
    await repository.claim_next(
        task_id="task-h5", agent_run_id="run-h5", finalization_epoch=4,
        lease_owner="worker", lease_seconds=180, h5_request_id=exact,
        deferred_h5_snapshot_request_ids=deferred,
    )
    _, query, args = next(call for call in connection.calls if call[0] == "fetchrow")
    assert args[:6] == ("task-h5", "run-h5", 4, "worker", 180, ["active_session"])
    assert args[6:] == (*([list(deferred)] if deferred else []), *([exact] if exact else []))
    if exact:
        number = 8 if deferred else 7
        assert f"AND task.target_platform = 'h5' AND request.request_id = ${number}" in query
    else:
        assert "AND task.target_platform = 'h5' AND request.request_id =" not in query
    assert "request.attempt_count < request.max_attempts" in query
    assert "FOR UPDATE OF request SKIP LOCKED" in query
