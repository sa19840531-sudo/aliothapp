"""证据附件身份回填 PostgreSQL 仓储 SQL 合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from services.api.app.application.evidence_artifact_backfill import (
    EvidenceArtifactBackfillRow,
    EvidenceArtifactComputedIdentity,
)
from services.api.app.infrastructure.db.evidence_artifact_backfill_repository import (
    PostgresEvidenceArtifactBackfillRepository,
)


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
        self.query = ""
        self.args: tuple[Any, ...] = ()
        self.rows: list[dict[str, Any]] = []
        self.updated_id: str | None = None

    async def fetch(self, query: str, *args: Any):
        self.query = " ".join(query.split())
        self.args = args
        return self.rows

    async def fetchval(self, query: str, *args: Any):
        self.query = " ".join(query.split())
        self.args = args
        return self.updated_id


async def test_scan_candidates_uses_stable_cursor_and_finds_all_invalid_identity() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    connection = RecordingConnection()
    connection.rows = [
        {
            "artifact_id": "11111111-1111-1111-1111-111111111111",
            "task_id": "22222222-2222-2222-2222-222222222222",
            "capture_id": "33333333-3333-3333-3333-333333333333",
            "created_at": now,
            "storage_path": "a.bin",
            "checksum": None,
            "size_bytes": None,
        }
    ]
    repository = PostgresEvidenceArtifactBackfillRepository(
        pool=FakePool(connection)
    )

    rows = await repository.scan_candidates(
        after_created_at=now,
        after_id="00000000-0000-0000-0000-000000000001",
        limit=50,
    )

    assert rows[0].artifact_id == "11111111-1111-1111-1111-111111111111"
    assert rows[0].task_id == "22222222-2222-2222-2222-222222222222"
    assert rows[0].capture_id == "33333333-3333-3333-3333-333333333333"
    assert "(artifact.created_at, artifact.id) > ($1, $2::uuid)" in connection.query
    assert "ORDER BY artifact.created_at, artifact.id" in connection.query
    assert "artifact.checksum IS NULL" in connection.query
    assert "btrim(artifact.checksum) = ''" in connection.query
    assert "artifact.checksum !~ '^sha256:[0-9a-f]{64}$'" in connection.query
    assert "artifact.size_bytes IS NULL" in connection.query
    assert "artifact.size_bytes < 0" in connection.query
    assert "btrim(artifact.storage_path) = ''" in connection.query
    assert connection.args == (now, "00000000-0000-0000-0000-000000000001", 50)


async def test_update_identity_uses_optimistic_original_value_match() -> None:
    now = datetime(2026, 8, 13, tzinfo=UTC)
    row = EvidenceArtifactBackfillRow(
        artifact_id="11111111-1111-1111-1111-111111111111",
        created_at=now,
        storage_path="a.bin",
        checksum=None,
        size_bytes=None,
    )
    computed = EvidenceArtifactComputedIdentity(
        checksum="sha256:" + "a" * 64,
        size_bytes=3,
    )
    connection = RecordingConnection()
    repository = PostgresEvidenceArtifactBackfillRepository(
        pool=FakePool(connection)
    )

    assert await repository.update_identity(row=row, computed=computed) is False
    assert "storage_path IS NOT DISTINCT FROM $4" in connection.query
    assert "checksum IS NOT DISTINCT FROM $5" in connection.query
    assert "size_bytes IS NOT DISTINCT FROM $6" in connection.query
    assert connection.args == (
        row.artifact_id,
        computed.checksum,
        computed.size_bytes,
        row.storage_path,
        row.checksum,
        row.size_bytes,
    )

    connection.updated_id = row.artifact_id
    assert await repository.update_identity(row=row, computed=computed) is True
