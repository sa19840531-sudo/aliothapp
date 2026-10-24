"""探索调度事实读取仓储的归属、缺口和来源合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.application.exploration_scheduler_facts import (
    merge_exploration_scheduler_facts,
)
from services.api.app.infrastructure.db.exploration_scheduler_facts_repository import (
    ExplorationSchedulerFactsScopeError,
    PostgresExplorationSchedulerFactsReader,
)

TASK_ID = "00000000-0000-0000-0000-000000000001"
RUN_ID = "00000000-0000-0000-0000-000000000002"
SESSION_ID = "session-1"
CAPTURE_ID = "00000000-0000-0000-0000-000000000003"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


class _Acquire:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    async def __aenter__(self) -> Any:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Pool:
    def __init__(self, connection: Any) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _Transaction:
    def __init__(self, connection: _Connection, options: dict[str, Any]) -> None:
        self._connection = connection
        self._options = options

    async def __aenter__(self) -> _Transaction:
        self._connection.transactions.append(self._options)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Connection:
    def __init__(
        self,
        *,
        scope: dict[str, Any] | None = None,
        entry_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.scope = scope or {
            "task_id": TASK_ID,
            "agent_run_id": RUN_ID,
            "session_id": SESSION_ID,
            "task_finalization_epoch": 4,
            "session_finalization_epoch": 4,
            "surface_kind": "web",
            "session_status": "active",
            "platform_build_ref": None,
        }
        self.queries: list[tuple[str, tuple[Any, ...]]] = []
        self.transactions: list[dict[str, Any]] = []
        self.entry_overrides = dict(entry_overrides or {})

    def transaction(self, **options: Any) -> _Transaction:
        return _Transaction(self, options)

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        normalized = " ".join(query.split())
        self.queries.append((normalized, args))
        if "FROM qc_task AS task" in normalized:
            if len(args) >= 2 and args[1] != self.scope.get("agent_run_id", RUN_ID):
                return None
            if len(args) >= 4 and args[3] is not None and args[3] != self.scope[
                "task_finalization_epoch"
            ]:
                return None
            return {
                "task_id": self.scope["task_id"],
                "agent_run_id": self.scope.get("agent_run_id", RUN_ID),
                "session_id": self.scope["session_id"],
                "task_finalization_epoch": self.scope["task_finalization_epoch"],
                "session_finalization_epoch": self.scope["session_finalization_epoch"],
                "surface_kind": (
                    "android"
                    if len(args) >= 5 and args[4] == ["android", "ios"]
                    else self.scope.get("surface_kind", "web")
                ),
                "session_status": self.scope.get("session_status", "active"),
                "platform_build_ref": (
                    "build-1"
                    if len(args) >= 5 and args[4] == ["android", "ios"]
                    else self.scope.get("platform_build_ref")
                ),
            }
        if "FROM qc_h5_coverage_snapshot" in normalized:
            return {
                "coverage_snapshot_id": "h5-snapshot-1",
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "finalization_epoch": 4,
                "revision": 2,
                "authority_source": "h5_page_type_assessment",
                "required_page_types": ["home", "profile"],
                "covered_page_types": ["home"],
                "missing_page_types": ["profile"],
                "coverage_scopes": [{"scope": "business"}],
                "assessment_scope_ready": True,
                "frontier_terminal": False,
                "completion_ready": False,
                "schema_version": "h5_coverage.v1",
                "payload_hash": "sha256:h5",
                "created_at": NOW,
            }
        if "FROM qc_app_evidence_index" in normalized:
            return {
                "index_id": "app-index-1",
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "build_id": "build-1",
                "surface_session_id": SESSION_ID,
                "protocol_version": "app-evidence-index.v1",
                "status": "building",
                "result_context_sha256": "sha256:context",
                "outcome": None,
                "terminal_reason_code": None,
                "content_sha256": None,
                "evidence_gaps": [
                    {"page_type": "profile", "reason_code": "missing_manifest"}
                ],
                "sealed_at": None,
                "created_at": NOW,
                "updated_at": NOW,
            }
        raise AssertionError(f"未处理 fetchrow SQL：{normalized}")

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        normalized = " ".join(query.split())
        self.queries.append((normalized, args))
        if "FROM qc_evidence_request AS request" in normalized:
            return [
                {
                    "request_id": "evidence-request-1",
                    "task_id": TASK_ID,
                    "rule_id": "00000000-0000-0000-0000-000000000004",
                    "rule_code": "profile.avatar",
                    "target_capture_id": CAPTURE_ID,
                    "session_dependency": "active_session",
                    "status": "pending",
                    "request_payload": {
                        "reason_code": "evidence_insufficient",
                        "target_page_type": "profile",
                    },
                    "finalization_epoch": 4,
                    "created_at": NOW,
                    "updated_at": NOW,
                    "surface_session_id": SESSION_ID,
                    "work_consumer_run_id": RUN_ID,
                    "attempt_consumer_run_id": None,
                }
            ]
        if "FROM qc_app_evidence_index_entry" in normalized:
            entry = {
                    "sequence_no": 1,
                    "capture_id": CAPTURE_ID,
                    "evidence_manifest_id": "manifest-1",
                    "evidence_manifest_version": 1,
                    "semantic_role": "final_observation",
                    "surface_phase": "home",
                    "exploration_work_id": "work-1",
                    "action_execution_id": None,
                    "candidate_id": "candidate-1",
                    "page_identity": "profile-1",
                    "captured_at": NOW,
                    "metadata": {"source": "test"},
                    "verified_capture_surface_kind": "android",
                    "verified_capture_id": CAPTURE_ID,
                    "verified_capture_task_id": TASK_ID,
                    "verified_capture_agent_run_id": RUN_ID,
                    "verified_capture_session_id": SESSION_ID,
                    "verified_capture_parent_capture_id": None,
                    "verified_capture_action_execution_id": None,
                    "verified_capture_status": "captured",
                    "verified_capture_role": "bootstrap",
                    "verified_capture_metadata": {"finalization_epoch": 4},
                    "verified_capture_session_record_id": SESSION_ID,
                    "verified_capture_session_surface_kind": "android",
                    "verified_capture_session_finalization_epoch": 4,
                    "verified_action_execution_id": None,
                    "verified_action_task_id": None,
                    "verified_action_session_id": None,
                    "verified_action_finalization_epoch": None,
                    "verified_action_source_capture_id": None,
                    "verified_action_result_capture_id": None,
                    "verified_action_source_id": None,
                    "verified_action_source_task_id": None,
                    "verified_action_source_agent_run_id": None,
                    "verified_action_source_surface_kind": None,
                    "verified_action_source_session_id": None,
                    "verified_action_source_parent_capture_id": None,
                    "verified_action_source_action_execution_id": None,
                    "verified_action_source_status": None,
                    "verified_action_source_session_record_id": None,
                    "verified_action_source_session_surface_kind": None,
                    "verified_action_source_session_finalization_epoch": None,
                    "verified_action_target_id": None,
                    "verified_action_target_task_id": None,
                    "verified_action_target_agent_run_id": None,
                    "verified_action_target_surface_kind": None,
                    "verified_action_target_session_id": None,
                    "verified_action_target_parent_capture_id": None,
                    "verified_action_target_action_execution_id": None,
                    "verified_action_target_status": None,
                    "verified_action_target_session_record_id": None,
                    "verified_action_target_session_surface_kind": None,
                    "verified_action_target_session_finalization_epoch": None,
                    "verified_work_id": "work-1",
                    "verified_work_task_id": TASK_ID,
                    "verified_work_session_id": SESSION_ID,
                    "verified_work_source_capture_id": CAPTURE_ID,
                    "verified_work_finalization_epoch": 4,
                    "verified_work_source_id": CAPTURE_ID,
                    "verified_work_source_task_id": TASK_ID,
                    "verified_work_source_agent_run_id": RUN_ID,
                    "verified_work_source_surface_kind": "android",
                    "verified_work_source_session_id": SESSION_ID,
                    "verified_work_source_status": "captured",
                    "verified_work_source_metadata": {"finalization_epoch": 4},
                    "verified_work_source_session_record_id": SESSION_ID,
                    "verified_work_source_session_surface_kind": "android",
                    "verified_work_source_session_finalization_epoch": 4,
                }
            entry.update(self.entry_overrides)
            return [entry]
        raise AssertionError(f"未处理 fetch SQL：{normalized}")


@pytest.mark.asyncio
async def test_reader_preserves_scope_snapshot_index_and_targeted_gap_source() -> None:
    connection = _Connection()
    reader = PostgresExplorationSchedulerFactsReader(pool=_Pool(connection))

    h5 = await reader.read_h5_facts(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        session_id=SESSION_ID,
        finalization_epoch=4,
    )
    app = await reader.read_app_facts(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        session_id=SESSION_ID,
        finalization_epoch=4,
    )

    assert h5["exploration_scheduler_facts_scope"] == {
        "task_id": TASK_ID,
        "agent_run_id": RUN_ID,
        "session_id": SESSION_ID,
        "finalization_epoch": 4,
        "surface_kind": "web",
        "session_status": "active",
        "platform_build_ref": None,
        "source": "current_task_surface_session",
    }
    assert h5["h5_coverage_snapshot_fact"]["source"] == "qc_h5_coverage_snapshot"
    request = h5["rule_evidence_requests"][0]
    assert request["source"] == "qc_evidence_request"
    assert request["target_capture_id"] == CAPTURE_ID
    assert request["evidence_gap"]["page_types"] == ["profile"]
    assert "request_payload" not in request
    assert app["rule_evidence_requests"][0]["request_payload"]["target_page_type"] == (
        "profile"
    )
    assert app["app_evidence_index"]["source"] == "qc_app_evidence_index"
    assert app["app_evidence_index"]["entries"][0]["capture_id"] == CAPTURE_ID
    assert app["app_evidence_index"]["entries"][0]["source"] == (
        "qc_app_evidence_index_entry"
    )
    assert connection.transactions == [
        {"isolation": "repeatable_read", "readonly": True},
        {"isolation": "repeatable_read", "readonly": True},
    ]

    scope_query = next(query for query, _ in connection.queries if "FROM qc_task AS task" in query)
    assert "$4::integer IS NULL OR task.finalization_epoch = $4" in scope_query
    assert "run.task_id = task.id" in scope_query
    assert "session.surface_kind = ANY($5::text[])" in scope_query
    request_query = next(
        query for query, _ in connection.queries if "FROM qc_evidence_request AS request" in query
    )
    assert "request.finalization_epoch = $4" in request_query
    assert "work_item.finalization_epoch = $4" in request_query
    assert "capture.agent_run_id = $2::uuid" in request_query
    assert "request.status IN ('pending', 'routed', 'running')" in request_query
    app_entry_query = next(
        query
        for query, _ in connection.queries
        if "FROM qc_app_evidence_index_entry" in query
    )
    assert "verified_capture_finalization_epoch" in app_entry_query
    assert "verified_action_source_finalization_epoch" in app_entry_query
    assert "verified_work_source_finalization_epoch" in app_entry_query


@pytest.mark.asyncio
async def test_app_entry_without_work_still_requires_immutable_capture_epoch() -> None:
    connection = _Connection(
        entry_overrides={
            "exploration_work_id": None,
            "verified_work_id": None,
            "verified_work_task_id": None,
            "verified_work_session_id": None,
            "verified_work_finalization_epoch": None,
        }
    )
    reader = PostgresExplorationSchedulerFactsReader(pool=_Pool(connection))

    facts = await reader.read_app_facts(
        task_id=TASK_ID,
        agent_run_id=RUN_ID,
        session_id=SESSION_ID,
        finalization_epoch=4,
    )
    assert facts["app_evidence_index"]["entries"][0]["exploration_work_id"] is None

    missing_proof = _Connection(
        entry_overrides={
            "exploration_work_id": None,
            "verified_work_id": None,
            "verified_work_task_id": None,
            "verified_work_session_id": None,
            "verified_work_finalization_epoch": None,
            "verified_capture_metadata": {},
        }
    )
    missing_proof_reader = PostgresExplorationSchedulerFactsReader(
        pool=_Pool(missing_proof)
    )
    with pytest.raises(ExplorationSchedulerFactsScopeError, match="不可变"):
        await missing_proof_reader.read_app_facts(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            finalization_epoch=4,
        )


@pytest.mark.asyncio
async def test_app_entry_rejects_capture_epoch_that_disagrees_with_scope() -> None:
    connection = _Connection(
        entry_overrides={"verified_capture_metadata": {"finalization_epoch": 3}}
    )
    reader = PostgresExplorationSchedulerFactsReader(pool=_Pool(connection))

    with pytest.raises(ExplorationSchedulerFactsScopeError, match="metadata"):
        await reader.read_app_facts(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            finalization_epoch=4,
        )


@pytest.mark.asyncio
async def test_reader_fails_closed_when_task_and_session_epochs_are_not_aligned() -> None:
    connection = _Connection(
        scope={
            "task_id": TASK_ID,
            "session_id": SESSION_ID,
            "task_finalization_epoch": 4,
            "session_finalization_epoch": 3,
        }
    )
    reader = PostgresExplorationSchedulerFactsReader(pool=_Pool(connection))

    with pytest.raises(ExplorationSchedulerFactsScopeError):
        await reader.read_h5_facts(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            finalization_epoch=4,
        )


@pytest.mark.asyncio
async def test_reader_fails_closed_when_agent_run_does_not_belong_to_task() -> None:
    connection = _Connection()
    connection.scope["agent_run_id"] = "00000000-0000-0000-0000-000000000099"
    reader = PostgresExplorationSchedulerFactsReader(pool=_Pool(connection))

    with pytest.raises(ExplorationSchedulerFactsScopeError):
        await reader.read_h5_facts(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            finalization_epoch=4,
        )


@pytest.mark.asyncio
async def test_reader_rejects_stale_epoch_before_reading_snapshot() -> None:
    connection = _Connection()
    reader = PostgresExplorationSchedulerFactsReader(pool=_Pool(connection))

    with pytest.raises(ExplorationSchedulerFactsScopeError):
        await reader.read_app_facts(
            task_id=TASK_ID,
            agent_run_id=RUN_ID,
            session_id=SESSION_ID,
            finalization_epoch=5,
        )

    assert not any("FROM qc_app_evidence_index" in query for query, _ in connection.queries)


def test_merge_clears_stale_surface_fact_when_reader_confirms_no_current_row() -> None:
    metadata = {
        "h5_coverage_snapshot_fact": {"missing_page_types": ["profile"]},
        "app_evidence_index": {"evidence_gaps": [{"page_type": "profile"}]},
    }

    h5 = merge_exploration_scheduler_facts(
        metadata,
        {"rule_evidence_requests": []},
        surface="h5",
    )
    app = merge_exploration_scheduler_facts(
        metadata,
        {"rule_evidence_requests": []},
        surface="app",
    )

    assert "h5_coverage_snapshot_fact" not in h5
    assert "app_evidence_index" in h5
    assert "app_evidence_index" not in app
    assert "h5_coverage_snapshot_fact" in app
