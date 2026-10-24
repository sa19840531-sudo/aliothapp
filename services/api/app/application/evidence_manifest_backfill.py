"""E23：为历史无清单采集回填 Evidence Manifest。

冻结依据：
- E23 验收：历史无清单数据可按时间分流并回填
- E02 条款 1.1：清单 quality_status 是齐全性唯一权威

回填只补 **pending** 清单，绝不代替裁决：
历史采集的齐全性结论必须由清单写入侧按需求集裁决（E07/E14），
回填脚本没有需求集上下文，替它下 pass/fail 就是伪造权威。
补出 pending 清单后，读取侧即可分型为 `evidence_pending`（E02 条款 2.1），
与「无清单＝控制面未就绪」区分开。

已有清单的采集一律跳过（不改历史版本，E25 不可变约束的同源理由）。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from services.api.app.application.evidence_manifest_writer import (
    build_pending_page_capture_manifest,
    manifest_session_id,
)
from services.api.app.domain.evidence import EvidenceManifestReference
from services.api.app.domain.evidence.vocabulary import EvidenceVocabularyError


@dataclass(frozen=True, kw_only=True)
class BackfillArtifactRow:
    """回填所需的单份附件身份。"""

    artifact_id: str
    artifact_type: str
    storage_path: str
    checksum: str
    size_bytes: int
    content_type: str | None = None


@dataclass(frozen=True, kw_only=True)
class BackfillCaptureRow:
    """一个待回填 Capture 的最小数据库快照。"""

    task_id: str
    capture_id: str
    agent_run_id: str
    capture_role: str
    captured_at: datetime
    surface_session_id: str | None
    artifacts: tuple[BackfillArtifactRow, ...]


@dataclass(frozen=True, kw_only=True)
class ManifestBackfillItemResult:
    """单个 Capture 的回填结果。"""

    task_id: str
    capture_id: str
    status: str
    manifest_id: str | None = None
    artifact_count: int = 0
    reason_code: str | None = None


@dataclass(frozen=True, kw_only=True)
class ManifestBackfillReport:
    """可供运维留档与 E24 切主前置检查的回填报告。"""

    apply: bool
    items: tuple[ManifestBackfillItemResult, ...]

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.status for item in self.items).items()))

    @property
    def ready_for_cutover(self) -> bool:
        """无 failed 项时，历史区间可纳入清单读取。"""
        return all(item.status in {"created", "skipped"} for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "apply": self.apply,
            "counts": self.counts,
            "ready_for_cutover": self.ready_for_cutover,
            "items": [
                {
                    "task_id": item.task_id,
                    "capture_id": item.capture_id,
                    "status": item.status,
                    "manifest_id": item.manifest_id,
                    "artifact_count": item.artifact_count,
                    "reason_code": item.reason_code,
                }
                for item in self.items
            ],
        }


class ManifestBackfillRepository(Protocol):
    """历史采集扫描与清单写入端口。"""

    async def scan_captures_without_manifest(
        self,
        *,
        before_captured_at: datetime | None,
        after_captured_at: datetime | None,
        after_capture_id: str | None,
        limit: int,
    ) -> list[BackfillCaptureRow]: ...

    async def add_manifest(self, *, manifest: EvidenceManifestReference) -> bool: ...


class EvidenceManifestBackfillService:
    """以稳定游标扫描历史采集并补写 pending 清单。"""

    def __init__(
        self,
        *,
        repository: ManifestBackfillRepository,
        batch_size: int = 200,
    ) -> None:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        self._repository = repository
        self._batch_size = batch_size

    async def run(
        self,
        *,
        apply: bool,
        before_captured_at: datetime | None = None,
        max_captures: int | None = None,
    ) -> ManifestBackfillReport:
        """扫描 `before_captured_at` 之前的无清单采集并回填。

        `before_captured_at` 即分流切换时点：只回填切换点之前的历史数据，
        切换点之后的采集由 E07/E08 生产链路自己写清单。
        """
        items: list[ManifestBackfillItemResult] = []
        cursor_captured_at: datetime | None = None
        cursor_capture_id: str | None = None
        while max_captures is None or len(items) < max_captures:
            remaining = (
                self._batch_size
                if max_captures is None
                else min(self._batch_size, max_captures - len(items))
            )
            rows = await self._repository.scan_captures_without_manifest(
                before_captured_at=before_captured_at,
                after_captured_at=cursor_captured_at,
                after_capture_id=cursor_capture_id,
                limit=remaining,
            )
            if not rows:
                break
            for row in rows:
                items.append(await self._backfill_one(row, apply=apply))
            cursor_captured_at = rows[-1].captured_at
            cursor_capture_id = rows[-1].capture_id
            if len(rows) < remaining:
                break
        return ManifestBackfillReport(apply=apply, items=tuple(items))

    async def _backfill_one(
        self,
        row: BackfillCaptureRow,
        *,
        apply: bool,
    ) -> ManifestBackfillItemResult:
        if not row.artifacts:
            return ManifestBackfillItemResult(
                task_id=row.task_id,
                capture_id=row.capture_id,
                status="skipped",
                reason_code="no_artifacts",
            )
        try:
            manifest = build_pending_page_capture_manifest(
                task_id=row.task_id,
                session_id=manifest_session_id(
                    row.surface_session_id,
                    agent_run_id=row.agent_run_id,
                ),
                capture_id=row.capture_id,
                capture_role=row.capture_role,
                artifacts=row.artifacts,
                # 清单创建时间沿用采集时间，保持历史时间线可读
                created_at=row.captured_at,
            )
        except (EvidenceVocabularyError, ValueError) as exc:
            return ManifestBackfillItemResult(
                task_id=row.task_id,
                capture_id=row.capture_id,
                status="failed",
                artifact_count=len(row.artifacts),
                reason_code=_reason_code(exc),
            )
        if not apply:
            return ManifestBackfillItemResult(
                task_id=row.task_id,
                capture_id=row.capture_id,
                status="created",
                manifest_id=manifest.manifest_id,
                artifact_count=len(manifest.artifacts),
                reason_code="dry_run",
            )
        try:
            created = await self._repository.add_manifest(manifest=manifest)
        except Exception as exc:  # noqa: BLE001 - 单条失败不阻断整批回填
            return ManifestBackfillItemResult(
                task_id=row.task_id,
                capture_id=row.capture_id,
                status="failed",
                artifact_count=len(manifest.artifacts),
                reason_code=_reason_code(exc),
            )
        return ManifestBackfillItemResult(
            task_id=row.task_id,
            capture_id=row.capture_id,
            status="created" if created else "skipped",
            manifest_id=manifest.manifest_id,
            artifact_count=len(manifest.artifacts),
            reason_code=None if created else "manifest_exists",
        )


def _reason_code(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"[:200]


def summarize_backfill(reports: Sequence[ManifestBackfillReport]) -> dict[str, int]:
    """合并多批报告的状态计数，供运维汇总。"""
    counter: Counter[str] = Counter()
    for report in reports:
        counter.update(report.counts)
    return dict(sorted(counter.items()))
