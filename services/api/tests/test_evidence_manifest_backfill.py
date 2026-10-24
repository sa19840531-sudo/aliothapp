"""E23：历史无清单采集的清单回填测试。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.evidence_manifest_backfill import (
    BackfillArtifactRow,
    BackfillCaptureRow,
    EvidenceManifestBackfillService,
    summarize_backfill,
)
from services.api.app.domain.evidence import (
    EvidenceManifestReference,
    EvidenceQualityStatus,
)

CUTOVER = datetime(2026, 8, 13, 0, 0, tzinfo=UTC)
CAPTURED_AT = CUTOVER - timedelta(days=2)
TASK_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "33333333-3333-3333-3333-333333333333"


def _artifact(artifact_id: str = "artifact-1") -> BackfillArtifactRow:
    payload = f"payload-{artifact_id}".encode()
    return BackfillArtifactRow(
        artifact_id=artifact_id,
        artifact_type="screenshot",
        storage_path=f"{TASK_ID}/capture/{artifact_id}.data",
        checksum=f"sha256:{hashlib.sha256(payload).hexdigest()}",
        size_bytes=len(payload),
        content_type="image/png",
    )


def _capture(
    *,
    capture_id: str,
    artifacts: tuple[BackfillArtifactRow, ...] = (_artifact(),),
    capture_role: str = "primary",
    surface_session_id: str | None = "surface-session-1",
    captured_at: datetime = CAPTURED_AT,
) -> BackfillCaptureRow:
    return BackfillCaptureRow(
        task_id=TASK_ID,
        capture_id=capture_id,
        agent_run_id=RUN_ID,
        capture_role=capture_role,
        captured_at=captured_at,
        surface_session_id=surface_session_id,
        artifacts=artifacts,
    )


class FakeBackfillRepository:
    def __init__(
        self,
        batches: list[list[BackfillCaptureRow]],
        *,
        existing: set[str] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.batches = list(batches)
        self.existing = existing or set()
        self.error = error
        self.added: list[EvidenceManifestReference] = []
        self.scan_calls: list[dict[str, object]] = []

    async def scan_captures_without_manifest(
        self,
        *,
        before_captured_at,
        after_captured_at,
        after_capture_id,
        limit,
    ):
        self.scan_calls.append(
            {
                "before_captured_at": before_captured_at,
                "after_captured_at": after_captured_at,
                "after_capture_id": after_capture_id,
                "limit": limit,
            }
        )
        if not self.batches:
            return []
        batch = self.batches.pop(0)
        if len(batch) > limit:
            # 真实仓储按 limit 截断，剩余留给下一批
            self.batches.insert(0, batch[limit:])
            return batch[:limit]
        return batch

    async def add_manifest(self, *, manifest: EvidenceManifestReference) -> bool:
        if self.error is not None:
            raise self.error
        self.added.append(manifest)
        return manifest.capture_id not in self.existing


async def test_dry_run_reports_without_writing() -> None:
    repository = FakeBackfillRepository([[_capture(capture_id="capture-1")]])
    service = EvidenceManifestBackfillService(repository=repository)

    report = await service.run(apply=False, before_captured_at=CUTOVER)

    assert report.apply is False
    assert repository.added == []
    assert report.counts == {"created": 1}
    assert report.items[0].reason_code == "dry_run"
    assert report.ready_for_cutover is True


async def test_apply_writes_pending_manifest_only() -> None:
    """E02 条款 1.1：回填不得替清单侧下 pass/fail 裁决。"""
    repository = FakeBackfillRepository([[_capture(capture_id="capture-1")]])
    service = EvidenceManifestBackfillService(repository=repository)

    report = await service.run(apply=True, before_captured_at=CUTOVER)

    assert report.counts == {"created": 1}
    assert len(repository.added) == 1
    manifest = repository.added[0]
    assert manifest.quality_status is EvidenceQualityStatus.PENDING
    assert manifest.manifest_version == 1
    assert manifest.session_id == "surface-session-1"
    # 清单创建时间沿用采集时间，保持历史时间线可读
    assert manifest.created_at == CAPTURED_AT
    assert report.items[0].manifest_id == manifest.manifest_id


async def test_capture_without_surface_session_uses_run_sentinel() -> None:
    repository = FakeBackfillRepository(
        [[_capture(capture_id="capture-1", surface_session_id=None)]]
    )
    service = EvidenceManifestBackfillService(repository=repository)

    await service.run(apply=True, before_captured_at=CUTOVER)

    assert repository.added[0].session_id == f"agent-run:{RUN_ID}"


async def test_capture_without_artifacts_is_skipped() -> None:
    repository = FakeBackfillRepository(
        [[_capture(capture_id="capture-1", artifacts=())]]
    )
    service = EvidenceManifestBackfillService(repository=repository)

    report = await service.run(apply=True, before_captured_at=CUTOVER)

    assert report.counts == {"skipped": 1}
    assert report.items[0].reason_code == "no_artifacts"
    assert repository.added == []


async def test_existing_manifest_is_not_overwritten() -> None:
    repository = FakeBackfillRepository(
        [[_capture(capture_id="capture-1")]],
        existing={"capture-1"},
    )
    service = EvidenceManifestBackfillService(repository=repository)

    report = await service.run(apply=True, before_captured_at=CUTOVER)

    assert report.counts == {"skipped": 1}
    assert report.items[0].reason_code == "manifest_exists"


async def test_invalid_capture_role_fails_without_blocking_batch() -> None:
    """E01 条款 3.1：表外角色回填失败，但不阻断同批其他采集。"""
    repository = FakeBackfillRepository(
        [
            [
                _capture(capture_id="capture-bad", capture_role="not_a_frozen_role"),
                _capture(capture_id="capture-good"),
            ]
        ]
    )
    service = EvidenceManifestBackfillService(repository=repository)

    report = await service.run(apply=True, before_captured_at=CUTOVER)

    assert report.counts == {"created": 1, "failed": 1}
    assert report.ready_for_cutover is False
    failed = next(item for item in report.items if item.status == "failed")
    assert "EvidenceVocabularyError" in (failed.reason_code or "")
    assert len(repository.added) == 1


async def test_write_failure_is_recorded_and_batch_continues() -> None:
    repository = FakeBackfillRepository(
        [[_capture(capture_id="capture-1"), _capture(capture_id="capture-2")]],
        error=RuntimeError("database unavailable"),
    )
    service = EvidenceManifestBackfillService(repository=repository)

    report = await service.run(apply=True, before_captured_at=CUTOVER)

    assert report.counts == {"failed": 2}
    assert report.ready_for_cutover is False
    assert "database unavailable" in (report.items[0].reason_code or "")


async def test_scan_advances_cursor_across_batches() -> None:
    first = [_capture(capture_id=f"capture-{index}") for index in range(2)]
    second = [
        _capture(
            capture_id="capture-late",
            captured_at=CAPTURED_AT + timedelta(hours=1),
        )
    ]
    repository = FakeBackfillRepository([first, second, []])
    service = EvidenceManifestBackfillService(repository=repository, batch_size=2)

    report = await service.run(apply=True, before_captured_at=CUTOVER)

    assert report.counts == {"created": 3}
    assert repository.scan_calls[0]["after_capture_id"] is None
    assert repository.scan_calls[1]["after_capture_id"] == "capture-1"
    assert repository.scan_calls[1]["after_captured_at"] == CAPTURED_AT
    # 每批都带切换时点，只回填历史区间
    assert all(call["before_captured_at"] == CUTOVER for call in repository.scan_calls)


async def test_max_captures_bounds_the_run() -> None:
    repository = FakeBackfillRepository(
        [[_capture(capture_id=f"capture-{index}") for index in range(5)]]
    )
    service = EvidenceManifestBackfillService(repository=repository, batch_size=5)

    report = await service.run(apply=True, before_captured_at=CUTOVER, max_captures=3)

    assert len(report.items) == 3
    assert repository.scan_calls[0]["limit"] == 3


def test_service_rejects_invalid_batch_size() -> None:
    with pytest.raises(ValueError, match="batch_size"):
        EvidenceManifestBackfillService(repository=object(), batch_size=0)  # type: ignore[arg-type]


async def test_report_serializes_for_ops_archive() -> None:
    repository = FakeBackfillRepository([[_capture(capture_id="capture-1")]])
    service = EvidenceManifestBackfillService(repository=repository)

    report = await service.run(apply=True, before_captured_at=CUTOVER)
    payload = report.to_dict()

    assert payload["apply"] is True
    assert payload["counts"] == {"created": 1}
    assert payload["ready_for_cutover"] is True
    assert payload["items"][0]["capture_id"] == "capture-1"


async def test_summarize_merges_multiple_reports() -> None:
    service_one = EvidenceManifestBackfillService(
        repository=FakeBackfillRepository([[_capture(capture_id="capture-1")]])
    )
    service_two = EvidenceManifestBackfillService(
        repository=FakeBackfillRepository(
            [[_capture(capture_id="capture-2", artifacts=())]]
        )
    )

    first = await service_one.run(apply=True, before_captured_at=CUTOVER)
    second = await service_two.run(apply=True, before_captured_at=CUTOVER)

    assert summarize_backfill([first, second]) == {"created": 1, "skipped": 1}
