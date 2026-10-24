"""扫描并回填存量证据附件的 checksum 与 size_bytes。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.api.app.application.evidence_artifact_backfill import (
    EvidenceArtifactBackfillReport,
    EvidenceArtifactBackfillService,
)
from services.api.app.core.config import get_settings
from services.api.app.infrastructure.db.evidence_artifact_backfill_repository import (
    PostgresEvidenceArtifactBackfillRepository,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("reports/evidence-artifact-identity-backfill.json")


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际回填；省略时仅执行 dry-run",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=500,
        help="每批扫描数量，默认 500",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"工作区内 JSON 报告路径，默认 {DEFAULT_REPORT_PATH.as_posix()}",
    )
    return parser


def _build_service() -> EvidenceArtifactBackfillService:
    settings = get_settings()
    inspector = build_evidence_artifact_store(settings.evidence_storage)
    return EvidenceArtifactBackfillService(
        repository=PostgresEvidenceArtifactBackfillRepository(),
        inspector=inspector,
    )


def _resolve_report_path(path: Path, *, workspace_root: Path) -> Path:
    root = workspace_root.resolve()
    resolved = (root / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("报告路径必须位于项目工作区内") from exc
    return resolved


def _serialize_report(
    report: EvidenceArtifactBackfillReport,
    *,
    apply: bool,
    batch_size: int,
    execution_id: str,
    started_at: datetime,
    completed_at: datetime,
    code_commit: str,
    backend_type: str,
) -> dict[str, Any]:
    return {
        "schema_version": "alioth.evidence-artifact-backfill-report/v1",
        "mode": "apply" if apply else "dry-run",
        "execution_id": execution_id,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "code_commit": code_commit,
        "backend_type": backend_type,
        "batch_size": batch_size,
        "scanned": len(report.items),
        "ready_for_constraint": report.ready_for_constraint,
        "counts": report.counts,
        "items": [
            {
                "artifact_id": item.artifact_id,
                "task_id": item.task_id,
                "capture_id": item.capture_id,
                "storage_path_digest": _storage_path_digest(item.storage_path),
                "status": item.status,
                "old_checksum": item.old_checksum,
                "old_size_bytes": item.old_size_bytes,
                "computed_checksum": item.computed_checksum,
                "computed_size_bytes": item.computed_size_bytes,
                "reason_code": item.reason_code,
            }
            for item in report.items
        ],
    }


def _storage_path_digest(storage_path: str | None) -> str | None:
    if storage_path is None:
        return None
    return "sha256:" + hashlib.sha256(storage_path.encode("utf-8")).hexdigest()


def _code_commit(workspace_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else "unknown"


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main(
    argv: Sequence[str] | None = None,
    *,
    service_factory: Callable[[], Any] = _build_service,
    workspace_root: Path = ROOT_DIR,
) -> int:
    args = _build_parser().parse_args(argv)
    report_path = _resolve_report_path(args.report, workspace_root=workspace_root)
    execution_id = f"evidence-artifact-backfill:{uuid4()}"
    started_at = datetime.now(UTC)
    try:
        service = service_factory()
        report = asyncio.run(service.run(apply=args.apply, batch_size=args.batch_size))
        payload = _serialize_report(
            report,
            apply=args.apply,
            batch_size=args.batch_size,
            execution_id=execution_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            code_commit=_code_commit(workspace_root),
            backend_type=str(getattr(service, "backend_type", type(service).__name__)),
        )
        _write_report(report_path, payload)
    except Exception as exc:  # noqa: BLE001 - 运维入口必须给出稳定失败码
        print(f"证据附件身份回填失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if report.ready_for_constraint else 2


if __name__ == "__main__":
    raise SystemExit(main())
