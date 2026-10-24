"""存量证据附件可信身份回填应用服务。"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.domain.evidence.artifact_identity import (
    EvidenceArtifactIdentityError,
    validate_evidence_artifact_identity,
)


@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactBackfillRow:
    """回填扫描所需的最小数据库快照。"""

    artifact_id: str
    created_at: datetime
    storage_path: str | None
    checksum: str | None
    size_bytes: int | None
    task_id: str | None = None
    capture_id: str | None = None


@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactComputedIdentity:
    """从不可变对象内容计算出的可信身份。"""

    checksum: str
    size_bytes: int


@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactBackfillItemResult:
    """单条附件回填结果。"""

    artifact_id: str
    storage_path: str | None
    status: str
    old_checksum: str | None
    old_size_bytes: int | None
    task_id: str | None = None
    capture_id: str | None = None
    computed_checksum: str | None = None
    computed_size_bytes: int | None = None
    reason_code: str | None = None


@dataclass(frozen=True, kw_only=True)
class EvidenceArtifactBackfillReport:
    """可供迁移前置检查与运维留档的回填报告。"""

    apply: bool
    items: tuple[EvidenceArtifactBackfillItemResult, ...]

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(item.status for item in self.items).items()))

    @property
    def ready_for_constraint(self) -> bool:
        return all(item.status in {"updated", "skipped"} for item in self.items)


class EvidenceArtifactIdentityInspector(Protocol):
    async def inspect_identity(
        self,
        storage_path: str,
    ) -> EvidenceArtifactComputedIdentity: ...


class EvidenceArtifactBackfillRepository(Protocol):
    async def scan_candidates(
        self,
        *,
        after_created_at: datetime | None,
        after_id: str | None,
        limit: int,
    ) -> list[EvidenceArtifactBackfillRow]: ...

    async def update_identity(
        self,
        *,
        row: EvidenceArtifactBackfillRow,
        computed: EvidenceArtifactComputedIdentity,
    ) -> bool: ...


class EvidenceArtifactBackfillService:
    """以稳定游标扫描并失败关闭地回填证据附件身份。"""

    def __init__(
        self,
        *,
        repository: EvidenceArtifactBackfillRepository,
        inspector: EvidenceArtifactIdentityInspector,
        inspection_attempts: int = 3,
        retry_delay_seconds: float = 0.05,
    ) -> None:
        if (
            isinstance(inspection_attempts, bool)
            or not isinstance(inspection_attempts, int)
            or inspection_attempts <= 0
        ):
            raise ValueError("inspection_attempts 必须为正整数")
        if retry_delay_seconds < 0:
            raise ValueError("retry_delay_seconds 不能小于 0")
        self._repository = repository
        self._inspector = inspector
        self._inspection_attempts = inspection_attempts
        self._retry_delay_seconds = retry_delay_seconds

    @property
    def backend_type(self) -> str:
        return type(self._inspector).__name__

    async def run(self, *, apply: bool, batch_size: int) -> EvidenceArtifactBackfillReport:
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size 必须为正整数")
        after_created_at: datetime | None = None
        after_id: str | None = None
        results: list[EvidenceArtifactBackfillItemResult] = []
        while True:
            rows = await self._repository.scan_candidates(
                after_created_at=after_created_at,
                after_id=after_id,
                limit=batch_size,
            )
            if not rows:
                break
            for row in rows:
                results.append(await self._process(row=row, apply=apply))
            last = rows[-1]
            after_created_at = last.created_at
            after_id = last.artifact_id
        return EvidenceArtifactBackfillReport(apply=apply, items=tuple(results))

    async def _process(
        self,
        *,
        row: EvidenceArtifactBackfillRow,
        apply: bool,
    ) -> EvidenceArtifactBackfillItemResult:
        base = {
            "artifact_id": row.artifact_id,
            "storage_path": row.storage_path,
            "old_checksum": row.checksum,
            "old_size_bytes": row.size_bytes,
            "task_id": row.task_id,
            "capture_id": row.capture_id,
        }
        if not isinstance(row.storage_path, str) or not row.storage_path.strip():
            return EvidenceArtifactBackfillItemResult(
                **base,
                status="invalid",
                reason_code="storage_path_blank",
            )
        try:
            computed = await self._inspect_with_retry(row.storage_path)
        except FileNotFoundError:
            return EvidenceArtifactBackfillItemResult(
                **base,
                status="missing",
                reason_code="storage_object_missing",
            )
        except Exception:  # noqa: BLE001 - 单条对象故障必须留档并继续游标扫描
            return EvidenceArtifactBackfillItemResult(
                **base,
                status="error",
                reason_code="identity_inspection_failed",
            )

        try:
            validate_evidence_artifact_identity(
                storage_path=row.storage_path,
                checksum=computed.checksum,
                size_bytes=computed.size_bytes,
            )
        except (AttributeError, EvidenceArtifactIdentityError):
            return EvidenceArtifactBackfillItemResult(
                **base,
                status="invalid",
                reason_code="computed_identity_invalid",
            )

        computed_values = {
            "computed_checksum": computed.checksum,
            "computed_size_bytes": computed.size_bytes,
        }
        existing_checksum = row.checksum.strip() if isinstance(row.checksum, str) else None
        if existing_checksum and existing_checksum != computed.checksum:
            return EvidenceArtifactBackfillItemResult(
                **base,
                **computed_values,
                status="conflict",
                reason_code="checksum_conflict",
            )
        if row.size_bytes is not None and row.size_bytes != computed.size_bytes:
            return EvidenceArtifactBackfillItemResult(
                **base,
                **computed_values,
                status="conflict",
                reason_code="size_bytes_conflict",
            )
        if existing_checksum == computed.checksum and row.size_bytes == computed.size_bytes:
            return EvidenceArtifactBackfillItemResult(
                **base,
                **computed_values,
                status="skipped",
                reason_code="identity_already_complete",
            )
        if not apply:
            return EvidenceArtifactBackfillItemResult(
                **base,
                **computed_values,
                status="would_update",
                reason_code="dry_run",
            )
        updated = await self._repository.update_identity(row=row, computed=computed)
        return EvidenceArtifactBackfillItemResult(
            **base,
            **computed_values,
            status="updated" if updated else "conflict",
            reason_code="identity_backfilled" if updated else "concurrent_change",
        )

    async def _inspect_with_retry(
        self,
        storage_path: str,
    ) -> EvidenceArtifactComputedIdentity:
        for attempt in range(1, self._inspection_attempts + 1):
            try:
                return await self._inspector.inspect_identity(storage_path)
            except FileNotFoundError:
                raise
            except Exception:  # noqa: BLE001 - 瞬态存储错误按有界预算重试
                if attempt >= self._inspection_attempts:
                    raise
                if self._retry_delay_seconds:
                    await asyncio.sleep(self._retry_delay_seconds * (2 ** (attempt - 1)))
        raise AssertionError("对象身份读取重试状态不可达")
