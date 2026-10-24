"""证据附件身份回填 CLI 测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.backfill_evidence_artifact_identity import main
from services.api.app.application.evidence_artifact_backfill import (
    EvidenceArtifactBackfillItemResult,
    EvidenceArtifactBackfillReport,
)


class FakeService:
    def __init__(self, report: EvidenceArtifactBackfillReport) -> None:
        self.report = report
        self.calls: list[dict[str, object]] = []

    async def run(self, *, apply: bool, batch_size: int):
        self.calls.append({"apply": apply, "batch_size": batch_size})
        return self.report


def _report(*, status: str = "skipped") -> EvidenceArtifactBackfillReport:
    return EvidenceArtifactBackfillReport(
        apply=False,
        items=(
            EvidenceArtifactBackfillItemResult(
                artifact_id="11111111-1111-1111-1111-111111111111",
                storage_path="safe/path.bin",
                status=status,
                old_checksum=None,
                old_size_bytes=None,
                computed_checksum="sha256:" + "a" * 64,
                computed_size_bytes=3,
                reason_code="test_result",
            ),
        ),
    )


def test_cli_defaults_to_dry_run_and_writes_utf8_json(tmp_path: Path) -> None:
    service = FakeService(_report())
    report_path = tmp_path / "report.json"

    exit_code = main(
        ["--batch-size", "25", "--report", str(report_path)],
        service_factory=lambda: service,
        workspace_root=tmp_path,
    )

    assert exit_code == 0
    assert service.calls == [{"apply": False, "batch_size": 25}]
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry-run"
    assert payload["ready_for_constraint"] is True
    assert payload["batch_size"] == 25
    assert payload["scanned"] == 1
    assert payload["execution_id"].startswith("evidence-artifact-backfill:")
    assert payload["started_at"] <= payload["completed_at"]
    assert "code_commit" in payload
    assert "backend_type" in payload
    assert payload["items"][0]["task_id"] is None
    assert payload["items"][0]["capture_id"] is None
    assert payload["items"][0]["storage_path_digest"].startswith("sha256:")
    report_text = report_path.read_text(encoding="utf-8")
    assert "safe/path.bin" not in report_text
    assert "raw_payload" not in report_text


def test_cli_apply_and_unresolved_exit_code(tmp_path: Path) -> None:
    service = FakeService(_report(status="conflict"))

    exit_code = main(
        ["--apply", "--report", str(tmp_path / "conflict.json")],
        service_factory=lambda: service,
        workspace_root=tmp_path,
    )

    assert exit_code == 2
    assert service.calls == [{"apply": True, "batch_size": 500}]


def test_cli_rejects_invalid_batch_size_and_report_path_escape(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(
            ["--batch-size", "0"],
            service_factory=lambda: FakeService(_report()),
            workspace_root=tmp_path,
        )

    with pytest.raises(ValueError, match="工作区"):
        main(
            ["--report", str(tmp_path.parent / "outside.json")],
            service_factory=lambda: FakeService(_report()),
            workspace_root=tmp_path,
        )
