"""为历史无清单采集回填 pending Evidence Manifest（E23）。

只回填 `--before` 时点之前的采集：该时点即清单读取的分流切换点
（`ALIOTH_EVIDENCE_MANIFEST_CUTOVER_AT`），之后的采集由生产链路自己写清单。

回填只补 **pending** 清单，不代替齐全性裁决——脚本没有需求集上下文，
替清单侧下 pass/fail 就是伪造唯一权威（E02 条款 1.1）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from services.api.app.application.evidence_manifest_backfill import (
    EvidenceManifestBackfillService,
    ManifestBackfillReport,
)
from services.api.app.infrastructure.db.evidence_manifest_backfill_repository import (
    PostgresEvidenceManifestBackfillRepository,
)

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REPORT_PATH = Path("reports/evidence-manifest-backfill.json")


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return value


def _aware_datetime(raw: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须为 ISO 8601 时间") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际回填；省略时仅执行 dry-run",
    )
    parser.add_argument(
        "--before",
        type=_aware_datetime,
        default=None,
        help="只回填该采集时间之前的历史数据（清单分流切换点）；无时区按 UTC 解释",
    )
    parser.add_argument(
        "--batch-size",
        type=_positive_int,
        default=200,
        help="每批扫描数量，默认 200",
    )
    parser.add_argument(
        "--max-captures",
        type=_positive_int,
        default=None,
        help="本次最多处理的采集数；省略则处理全部候选",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"工作区内 JSON 报告路径，默认 {DEFAULT_REPORT_PATH.as_posix()}",
    )
    return parser


def _build_service(*, batch_size: int) -> EvidenceManifestBackfillService:
    return EvidenceManifestBackfillService(
        repository=PostgresEvidenceManifestBackfillRepository(),
        batch_size=batch_size,
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
    report: ManifestBackfillReport,
    *,
    execution_id: str,
    started_at: datetime,
    completed_at: datetime,
    code_commit: str,
    before: datetime | None,
    batch_size: int,
) -> dict[str, Any]:
    payload = report.to_dict()
    return {
        "schema_version": "alioth.evidence-manifest-backfill-report/v1",
        "mode": "apply" if report.apply else "dry-run",
        "execution_id": execution_id,
        "started_at": started_at.isoformat(),
        "completed_at": completed_at.isoformat(),
        "code_commit": code_commit,
        "before_captured_at": before.isoformat() if before is not None else None,
        "batch_size": batch_size,
        "scanned": len(report.items),
        **payload,
    }


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
    service_factory: Callable[..., Any] = _build_service,
    workspace_root: Path = ROOT_DIR,
) -> int:
    args = _build_parser().parse_args(argv)
    execution_id = f"evidence-manifest-backfill:{uuid4()}"
    started_at = datetime.now(UTC)
    try:
        # 报告路径校验放进 try：运维入口一律给稳定失败码，不抛栈
        report_path = _resolve_report_path(args.report, workspace_root=workspace_root)
        service = service_factory(batch_size=args.batch_size)
        report = asyncio.run(
            service.run(
                apply=args.apply,
                before_captured_at=args.before,
                max_captures=args.max_captures,
            )
        )
        payload = _serialize_report(
            report,
            execution_id=execution_id,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            code_commit=_code_commit(workspace_root),
            before=args.before,
            batch_size=args.batch_size,
        )
        _write_report(report_path, payload)
    except Exception as exc:  # noqa: BLE001 - 运维入口必须给出稳定失败码
        print(f"证据清单回填失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if report.ready_for_cutover else 2


if __name__ == "__main__":
    raise SystemExit(main())
