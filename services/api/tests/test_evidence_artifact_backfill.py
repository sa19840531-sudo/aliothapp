"""存量证据附件身份回填服务测试。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from services.api.app.application.evidence_artifact_backfill import (
    EvidenceArtifactBackfillRow,
    EvidenceArtifactBackfillService,
    EvidenceArtifactComputedIdentity,
)
from services.api.app.infrastructure.storage.local_evidence_store import (
    LocalEvidenceArtifactStore,
)
from services.api.app.infrastructure.storage.minio_evidence_store import (
    MinioEvidenceArtifactStore,
)

SHA256_ABC = "sha256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


class MemoryBackfillRepository:
    def __init__(self, rows: list[EvidenceArtifactBackfillRow]) -> None:
        self.rows = rows
        self.update_calls: list[dict[str, Any]] = []

    async def scan_candidates(
        self,
        *,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[EvidenceArtifactBackfillRow]:
        rows = sorted(self.rows, key=lambda item: (item.created_at, item.artifact_id))
        if after_created_at is not None and after_id is not None:
            rows = [
                item
                for item in rows
                if (item.created_at, item.artifact_id) > (after_created_at, after_id)
            ]
        return rows[:limit]

    async def update_identity(self, *, row, computed) -> bool:
        self.update_calls.append({"row": row, "computed": computed})
        for index, current in enumerate(self.rows):
            if current == row:
                self.rows[index] = replace(
                    current,
                    checksum=computed.checksum,
                    size_bytes=computed.size_bytes,
                )
                return True
        return False


def _row(
    artifact_id: str,
    *,
    storage_path: str | None = "abc.bin",
    checksum: str | None = None,
    size_bytes: int | None = None,
    offset: int = 0,
) -> EvidenceArtifactBackfillRow:
    return EvidenceArtifactBackfillRow(
        artifact_id=artifact_id,
        created_at=datetime(2026, 8, 13, tzinfo=UTC) + timedelta(seconds=offset),
        storage_path=storage_path,
        checksum=checksum,
        size_bytes=size_bytes,
    )


async def test_apply_backfills_both_missing_and_matching_partial_identity(tmp_path: Path) -> None:
    (tmp_path / "abc.bin").write_bytes(b"abc")
    repository = MemoryBackfillRepository(
        [
            _row("a", offset=1),
            _row("b", checksum=SHA256_ABC, offset=2),
        ]
    )
    service = EvidenceArtifactBackfillService(
        repository=repository,
        inspector=LocalEvidenceArtifactStore(root=tmp_path),
    )

    report = await service.run(apply=True, batch_size=1)

    assert [item.status for item in report.items] == ["updated", "updated"]
    assert len(repository.update_calls) == 2
    assert report.ready_for_constraint is True


async def test_backfill_fail_closed_results_and_dry_run(tmp_path: Path) -> None:
    (tmp_path / "abc.bin").write_bytes(b"abc")
    repository = MemoryBackfillRepository(
        [
            _row("conflict", checksum="sha256:" + "a" * 64, offset=1),
            _row("missing", storage_path="missing.bin", offset=2),
            _row("invalid", storage_path=" ", offset=3),
            _row("dry-run", offset=4),
            _row("skipped", checksum=SHA256_ABC, size_bytes=3, offset=5),
        ]
    )
    service = EvidenceArtifactBackfillService(
        repository=repository,
        inspector=LocalEvidenceArtifactStore(root=tmp_path),
    )

    report = await service.run(apply=False, batch_size=2)

    assert [item.status for item in report.items] == [
        "conflict",
        "missing",
        "invalid",
        "would_update",
        "skipped",
    ]
    assert repository.update_calls == []
    assert report.ready_for_constraint is False
    assert report.counts == {
        "conflict": 1,
        "invalid": 1,
        "missing": 1,
        "skipped": 1,
        "would_update": 1,
    }


async def test_repeated_run_after_update_is_skipped(tmp_path: Path) -> None:
    (tmp_path / "abc.bin").write_bytes(b"abc")
    repository = MemoryBackfillRepository([_row("a")])
    service = EvidenceArtifactBackfillService(
        repository=repository,
        inspector=LocalEvidenceArtifactStore(root=tmp_path),
    )

    first = await service.run(apply=True, batch_size=10)
    second = await service.run(apply=True, batch_size=10)

    assert first.items[0].status == "updated"
    assert second.items[0].status == "skipped"
    assert len(repository.update_calls) == 1


class StreamingResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.stream_calls: list[int] = []
        self.closed = False
        self.released = False

    def stream(self, amount: int):
        self.stream_calls.append(amount)
        yield from self.chunks

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class StreamingMinioClient:
    def __init__(self, response: StreamingResponse) -> None:
        self.response = response

    def get_object(self, bucket: str, storage_path: str) -> StreamingResponse:
        assert bucket == "evidence"
        assert storage_path == "abc.bin"
        return self.response


async def test_minio_inspector_streams_and_releases_response() -> None:
    response = StreamingResponse([b"a", b"bc"])
    store = MinioEvidenceArtifactStore(
        endpoint="unused",
        access_key="unused",
        secret_key="unused",
        bucket="evidence",
        client=StreamingMinioClient(response),
    )

    identity = await store.inspect_identity("abc.bin")

    assert identity.checksum == SHA256_ABC
    assert identity.size_bytes == 3
    assert response.stream_calls == [1024 * 1024]
    assert response.closed is True
    assert response.released is True


async def test_backfill_reports_inspector_errors(tmp_path: Path) -> None:
    class BrokenInspector:
        async def inspect_identity(self, storage_path: str):
            del storage_path
            raise RuntimeError("storage unavailable")

    service = EvidenceArtifactBackfillService(
        repository=MemoryBackfillRepository([_row("error")]),
        inspector=BrokenInspector(),
    )

    report = await service.run(apply=True, batch_size=10)

    assert report.items[0].status == "error"
    assert report.items[0].reason_code == "identity_inspection_failed"
    assert report.ready_for_constraint is False


async def test_backfill_rejects_invalid_computed_identity_before_update() -> None:
    class InvalidIdentityInspector:
        async def inspect_identity(self, storage_path: str):
            del storage_path
            return EvidenceArtifactComputedIdentity(
                checksum="sha256:" + "A" * 64,
                size_bytes=3,
            )

    repository = MemoryBackfillRepository([_row("invalid-computed")])
    service = EvidenceArtifactBackfillService(
        repository=repository,
        inspector=InvalidIdentityInspector(),
    )

    report = await service.run(apply=True, batch_size=10)

    assert repository.update_calls == []
    assert report.items[0].status == "invalid"
    assert report.items[0].reason_code == "computed_identity_invalid"
    assert report.ready_for_constraint is False


async def test_backfill_retries_transient_inspector_failure_before_succeeding() -> None:
    class RecoveringInspector:
        def __init__(self) -> None:
            self.calls = 0

        async def inspect_identity(self, storage_path: str):
            del storage_path
            self.calls += 1
            if self.calls < 3:
                raise TimeoutError("temporary object-store timeout")
            from services.api.app.application.evidence_artifact_backfill import (
                EvidenceArtifactComputedIdentity,
            )

            return EvidenceArtifactComputedIdentity(checksum=SHA256_ABC, size_bytes=3)

    inspector = RecoveringInspector()
    service = EvidenceArtifactBackfillService(
        repository=MemoryBackfillRepository([_row("recover")]),
        inspector=inspector,
        inspection_attempts=3,
        retry_delay_seconds=0,
    )

    report = await service.run(apply=False, batch_size=10)

    assert inspector.calls == 3
    assert report.items[0].status == "would_update"
