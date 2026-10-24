"""App Evidence Index PostgreSQL 仓储 SQL 与幂等门禁测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from services.api.app.domain.evidence import (
    AppEvidenceIndexEntry,
    AppEvidenceSemanticRole,
    AppExplorationResultOutcome,
    app_exploration_result_context_sha256,
    build_app_evidence_index,
    seal_app_evidence_index,
)
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection) -> None:
        self.connection = connection

    async def __aenter__(self):
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class RecordingConnection:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any):
        self.queries.append(" ".join(query.split()))
        return "SELECT 1"

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.queries.append(normalized)
        if "INSERT INTO qc_app_evidence_index AS existing" in normalized:
            return {"index_id": args[0], "created_at": NOW, "created": True}
        if "INSERT INTO qc_app_evidence_index_entry AS existing" in normalized:
            return {"sequence_no": args[2]}
        if "UPDATE qc_app_evidence_index AS target" in normalized:
            return {
                "index_id": args[0],
                "content_sha256": args[3],
                "sealed_at": args[5],
                "created_at": NOW,
            }
        raise AssertionError(f"未处理 SQL：{normalized}")


class ReadingConnection:
    def __init__(self, *, header: dict[str, Any], entries: list[dict[str, Any]]) -> None:
        self.header = header
        self.entries = entries
        self.queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any):
        self.queries.append(" ".join(query.split()))
        return self.header

    async def fetch(self, query: str, *args: Any):
        self.queries.append(" ".join(query.split()))
        return self.entries


def _building_index():
    return build_app_evidence_index(
        task_id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        build_id="build-1",
        surface_session_id="session-1",
        entries=(
            AppEvidenceIndexEntry(
                sequence_no=1,
                capture_id="33333333-3333-3333-3333-333333333333",
                evidence_manifest_id="evidence-manifest:abc",
                evidence_manifest_version=2,
                semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
                captured_at=NOW,
            ),
        ),
        created_at=NOW,
        result_context_sha256=app_exploration_result_context_sha256(
            coverage_summary={"stable_page_count": 2},
            started_at=NOW,
        ),
    )


async def test_create_is_unique_by_run_and_inserts_ordered_entries() -> None:
    connection = RecordingConnection()
    repository = PostgresAppEvidenceIndexRepository(pool=FakePool(connection))

    persisted = await repository.create(index=_building_index())

    assert persisted.created is True
    sql = " ".join(connection.queries)
    assert "pg_advisory_xact_lock" in sql
    assert "ON CONFLICT (agent_run_id)" in sql
    assert "INSERT INTO qc_app_evidence_index_entry AS existing" in sql
    assert "ON CONFLICT (index_id, sequence_no)" in sql
    assert "existing.created_at = EXCLUDED.created_at" not in sql
    assert "existing.result_context_sha256" in sql
    assert "SET result_context_sha256 = $2" in sql
    assert "DELETE FROM qc_app_evidence_index_entry" in sql
    assert "existing.surface_phase IS NOT DISTINCT FROM EXCLUDED.surface_phase" in sql
    assert "existing.captured_at = EXCLUDED.captured_at" in sql


async def test_create_reuses_persisted_created_at_during_building_recovery() -> None:
    connection = RecordingConnection()
    repository = PostgresAppEvidenceIndexRepository(pool=FakePool(connection))
    replay = replace(_building_index(), created_at=NOW + timedelta(minutes=1))

    persisted = await repository.create(index=replay)

    assert persisted.index.created_at == NOW
    assert persisted.index.result_context_sha256 == replay.result_context_sha256


async def test_closure_lock_uses_session_advisory_lock_and_releases_it() -> None:
    connection = RecordingConnection()
    repository = PostgresAppEvidenceIndexRepository(pool=FakePool(connection))

    async with repository.closure_lock(run_id="22222222-2222-2222-2222-222222222222"):
        assert "pg_advisory_lock" in connection.queries[-1]

    assert "pg_advisory_unlock" in connection.queries[-1]


async def test_seal_uses_row_lock_and_allows_only_same_atomic_content() -> None:
    connection = RecordingConnection()
    repository = PostgresAppEvidenceIndexRepository(pool=FakePool(connection))
    sealed = seal_app_evidence_index(
        _building_index(),
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        sealed_at=NOW + timedelta(seconds=1),
    )

    persisted = await repository.seal(index=sealed)

    assert persisted.index is sealed
    sql = " ".join(connection.queries)
    assert "FOR UPDATE" in sql
    assert "target.status = 'building'" in sql
    assert "target.content_sha256 = $4" in sql
    assert "target.result_context_sha256 IS NOT DISTINCT FROM $7" in sql
    assert "WHEN target.status = 'sealed' THEN target.sealed_at" in sql


async def test_get_by_run_id_adapts_postgres_datetimes_to_index_protocol() -> None:
    index = seal_app_evidence_index(
        _building_index(),
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed",
        sealed_at=NOW + timedelta(seconds=1),
    )
    payload = index.to_dict()
    connection = ReadingConnection(
        header={
            "index_id": index.index_id,
            "task_id": index.task_id,
            "run_id": index.run_id,
            "build_id": index.build_id,
            "surface_session_id": index.surface_session_id,
            "protocol_version": index.protocol_version,
            "status": payload["status"],
            "result_context_sha256": index.result_context_sha256,
            "outcome": payload["outcome"],
            "terminal_reason_code": index.terminal_reason_code,
            "content_sha256": index.content_sha256,
            "evidence_gaps": payload["evidence_gaps"],
            "sealed_at": index.sealed_at,
            "created_at": index.created_at,
        },
        entries=[
            {
                "sequence_no": 1,
                "capture_id": index.entries[0].capture_id,
                "evidence_manifest_id": index.entries[0].evidence_manifest_id,
                "evidence_manifest_version": index.entries[0].evidence_manifest_version,
                "semantic_role": index.entries[0].semantic_role.value,
                "surface_phase": index.entries[0].surface_phase,
                "exploration_work_id": index.entries[0].exploration_work_id,
                "action_execution_id": index.entries[0].action_execution_id,
                "candidate_id": index.entries[0].candidate_id,
                "page_identity": index.entries[0].page_identity,
                "captured_at": index.entries[0].captured_at,
                "metadata": {},
            }
        ],
    )
    repository = PostgresAppEvidenceIndexRepository(pool=FakePool(connection))

    restored = await repository.get_by_run_id(run_id=index.run_id)

    assert restored == index
    assert restored is not None
    assert restored.created_at == NOW
    assert restored.sealed_at == NOW + timedelta(seconds=1)
    assert restored.entries[0].captured_at == NOW
