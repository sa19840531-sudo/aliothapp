"""E23：证据清单回填仓储与命令行入口测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from scripts.backfill_evidence_manifest import main
from services.api.app.application.evidence_manifest_backfill import (
    ManifestBackfillItemResult,
    ManifestBackfillReport,
)
from services.api.app.infrastructure.db.evidence_manifest_backfill_repository import (
    PostgresEvidenceManifestBackfillRepository,
)

CUTOVER = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
CAPTURE_ID = "44444444-4444-4444-4444-444444444444"
RUN_ID = "33333333-3333-3333-3333-333333333333"


class RecordingConnection:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.query = ""
        self.args: tuple[Any, ...] = ()

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        self.query = " ".join(query.split())
        self.args = args
        return self.rows


class FakeAcquire:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> RecordingConnection:
        return self.connection

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakePool:
    def __init__(self, connection: RecordingConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


# ---------- 仓储 ----------


async def test_scan_only_returns_captures_without_manifest() -> None:
    connection = RecordingConnection()
    repository = PostgresEvidenceManifestBackfillRepository(pool=FakePool(connection))

    rows = await repository.scan_captures_without_manifest(
        before_captured_at=CUTOVER,
        after_captured_at=None,
        after_capture_id=None,
        limit=50,
    )

    assert rows == []
    assert "NOT EXISTS ( SELECT 1 FROM qc_evidence_manifest AS manifest" in connection.query
    assert "capture.capture_status = 'captured'" in connection.query
    # 只回填切换点之前的历史区间
    assert "capture.captured_at < $1" in connection.query
    # 游标按 (captured_at, id) 稳定推进
    assert "(capture.captured_at, capture.id) > ($2, $3::uuid)" in connection.query
    assert "ORDER BY capture.captured_at, capture.id" in connection.query
    assert connection.args == (CUTOVER, None, None, 50)


async def test_scan_parses_aggregated_artifacts() -> None:
    connection = RecordingConnection(
        [
            {
                "capture_id": CAPTURE_ID,
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "capture_role": "primary",
                "captured_at": CUTOVER,
                "surface_session_id": "surface-session-1",
                "artifacts": json.dumps(
                    [
                        {
                            "artifact_id": "artifact-1",
                            "artifact_type": "screenshot",
                            "storage_path": f"{TASK_ID}/{CAPTURE_ID}/artifact-1.png",
                            "checksum": "sha256:" + "a" * 64,
                            "size_bytes": 128,
                            "content_type": "image/png",
                        }
                    ]
                ),
            }
        ]
    )
    repository = PostgresEvidenceManifestBackfillRepository(pool=FakePool(connection))

    rows = await repository.scan_captures_without_manifest(
        before_captured_at=None,
        after_captured_at=None,
        after_capture_id=None,
        limit=10,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.capture_id == CAPTURE_ID
    assert row.capture_role == "primary"
    assert row.surface_session_id == "surface-session-1"
    assert len(row.artifacts) == 1
    assert row.artifacts[0].artifact_type == "screenshot"
    assert row.artifacts[0].size_bytes == 128


async def test_scan_handles_capture_without_artifacts() -> None:
    connection = RecordingConnection(
        [
            {
                "capture_id": CAPTURE_ID,
                "task_id": TASK_ID,
                "agent_run_id": RUN_ID,
                "capture_role": "primary",
                "captured_at": CUTOVER,
                "surface_session_id": None,
                "artifacts": "[]",
            }
        ]
    )
    repository = PostgresEvidenceManifestBackfillRepository(pool=FakePool(connection))

    rows = await repository.scan_captures_without_manifest(
        before_captured_at=None,
        after_captured_at=None,
        after_capture_id=None,
        limit=10,
    )

    assert rows[0].artifacts == ()
    assert rows[0].surface_session_id is None


async def test_scan_requires_paired_cursor() -> None:
    repository = PostgresEvidenceManifestBackfillRepository(
        pool=FakePool(RecordingConnection())
    )

    with pytest.raises(ValueError, match="游标"):
        await repository.scan_captures_without_manifest(
            before_captured_at=None,
            after_captured_at=CUTOVER,
            after_capture_id=None,
            limit=10,
        )


async def test_add_manifest_treats_version_conflict_as_not_created() -> None:
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        EvidenceManifestPersistenceError,
    )

    class ConflictingManifests:
        async def add(self, *, manifest: Any) -> Any:
            raise EvidenceManifestPersistenceError("版本已被占用")

    repository = PostgresEvidenceManifestBackfillRepository(
        pool=FakePool(RecordingConnection()),
        manifests=ConflictingManifests(),  # type: ignore[arg-type]
    )

    assert await repository.add_manifest(manifest=object()) is False  # type: ignore[arg-type]


# ---------- 命令行 ----------


class FakeService:
    def __init__(self, report: ManifestBackfillReport) -> None:
        self.report = report
        self.calls: list[dict[str, Any]] = []

    async def run(self, **kwargs: Any) -> ManifestBackfillReport:
        self.calls.append(kwargs)
        return self.report


def _report(*, apply: bool, status: str = "created") -> ManifestBackfillReport:
    return ManifestBackfillReport(
        apply=apply,
        items=(
            ManifestBackfillItemResult(
                task_id=TASK_ID,
                capture_id=CAPTURE_ID,
                status=status,
                manifest_id="evidence-manifest:abc",
                artifact_count=1,
            ),
        ),
    )


def test_cli_dry_run_writes_report(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    service = FakeService(_report(apply=False))

    code = main(
        ["--report", "reports/manifest.json", "--before", "2026-08-13T00:00:00"],
        service_factory=lambda **_kwargs: service,
        workspace_root=tmp_path,
    )

    assert code == 0
    assert service.calls[0]["apply"] is False
    assert service.calls[0]["before_captured_at"] == CUTOVER
    payload = json.loads((tmp_path / "reports/manifest.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "dry-run"
    assert payload["schema_version"] == "alioth.evidence-manifest-backfill-report/v1"
    assert payload["before_captured_at"] == CUTOVER.isoformat()
    assert payload["counts"] == {"created": 1}
    assert payload["ready_for_cutover"] is True


def test_cli_apply_passes_through_limits(tmp_path: Path) -> None:
    service = FakeService(_report(apply=True))

    code = main(
        ["--apply", "--batch-size", "50", "--max-captures", "10", "--report", "r.json"],
        service_factory=lambda **_kwargs: service,
        workspace_root=tmp_path,
    )

    assert code == 0
    assert service.calls[0]["apply"] is True
    assert service.calls[0]["max_captures"] == 10
    payload = json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))
    assert payload["mode"] == "apply"
    assert payload["batch_size"] == 50


def test_cli_returns_two_when_not_ready_for_cutover(tmp_path: Path) -> None:
    service = FakeService(_report(apply=True, status="failed"))

    code = main(
        ["--apply", "--report", "r.json"],
        service_factory=lambda **_kwargs: service,
        workspace_root=tmp_path,
    )

    assert code == 2


def test_cli_rejects_report_path_outside_workspace(tmp_path: Path) -> None:
    service = FakeService(_report(apply=False))

    code = main(
        ["--report", "../escape.json"],
        service_factory=lambda **_kwargs: service,
        workspace_root=tmp_path,
    )

    assert code == 1


def test_cli_rejects_invalid_before(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            ["--before", "not-a-time"],
            service_factory=lambda **_kwargs: FakeService(_report(apply=False)),
            workspace_root=tmp_path,
        )
