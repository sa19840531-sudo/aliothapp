"""Evidence Manifest 按 App Session 读取最新版本的 SQL 合同。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.infrastructure.db import evidence_manifest_repository as repository_module
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


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


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class ListingConnection:
    def __init__(self) -> None:
        self.query = ""
        self.args: tuple[Any, ...] = ()

    async def fetch(self, query: str, *args: Any):
        self.query = " ".join(query.split())
        self.args = args
        return []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.query = " ".join(query.split())
        self.args = args
        return "OK"


async def test_list_latest_for_session_uses_capture_scoped_latest_version() -> None:
    connection = ListingConnection()
    repository = PostgresEvidenceManifestRepository(pool=FakePool(connection))

    result = await repository.list_latest_for_session(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="session-1",
        agent_run_id="22222222-2222-2222-2222-222222222222",
    )

    assert result == ()
    assert "DISTINCT ON (manifest.capture_id)" in connection.query
    assert "manifest.manifest_version DESC" in connection.query
    assert "FROM qc_capture AS capture LEFT JOIN latest_manifest" in connection.query
    assert "capture.surface_session_id = $2" in connection.query
    assert "capture.agent_run_id = $3::uuid" in connection.query


async def test_list_stale_pending_only_selects_latest_app_capture_manifest() -> None:
    connection = ListingConnection()
    repository = PostgresEvidenceManifestRepository(pool=FakePool(connection))

    result = await repository.list_stale_pending(
        created_before=NOW,
        limit=25,
    )

    assert result == ()
    assert "DISTINCT ON (manifest.capture_id)" in connection.query
    assert "manifest.manifest_version DESC" in connection.query
    assert "task.target_platform = 'app'" in connection.query
    assert "latest_manifest.quality_status = 'pending'" in connection.query
    assert "latest_manifest.created_at <= $1" in connection.query
    assert connection.args == (NOW, 25)


@pytest.mark.asyncio
async def test_fail_latest_pending_for_agent_run_appends_fail_terminal_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = build_evidence_manifest_reference(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="agent-run:22222222-2222-2222-2222-222222222222",
        capture_id="33333333-3333-3333-3333-333333333333",
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id="44444444-4444-4444-4444-444444444444",
                artifact_type="screenshot",
                role="after_action",
                storage_path="evidence/screenshot.png",
                checksum="sha256:" + "0" * 64,
                size_bytes=1,
                content_type="image/png",
            ),
        ),
        quality_status=EvidenceQualityStatus.PENDING,
        quality_reason="page_capture_committed",
        created_at=NOW,
    )
    connection = ListingConnection()

    async def fetch(query: str, *args: Any):
        connection.query = " ".join(query.split())
        connection.args = args
        return [{"manifest_payload": pending.to_dict()}]

    inserted = []

    async def insert_manifest_on_connection(connection_arg, *, manifest, permitted_task_statuses):
        assert connection_arg is connection
        assert permitted_task_statuses == ("running",)
        inserted.append(manifest)
        return type("Persisted", (), {"created": True})()

    connection.fetch = fetch  # type: ignore[method-assign]
    monkeypatch.setattr(
        repository_module,
        "insert_manifest_on_connection",
        insert_manifest_on_connection,
    )
    repository = PostgresEvidenceManifestRepository(pool=FakePool(connection))

    count = await repository.fail_latest_pending_for_agent_run(
        task_id=pending.task_id,
        agent_run_id="22222222-2222-2222-2222-222222222222",
        quality_reason="h5_run_terminated_before_evidence_adjudication",
        created_at=NOW,
    )

    assert count == 1
    assert "DISTINCT ON (manifest.capture_id)" in connection.query
    assert "WHERE quality_status = 'pending'" in connection.query
    assert inserted[0].manifest_version == 2
    assert inserted[0].quality_status is EvidenceQualityStatus.FAIL
    assert inserted[0].quality_reason == "h5_run_terminated_before_evidence_adjudication"
