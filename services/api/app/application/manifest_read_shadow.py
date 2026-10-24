"""E23：清单化读取的双读影子对账与按采集时间分流。

冻结依据：
- E23 验收：新旧读取可影子对账；历史无清单数据可按时间分流
- E02 条款 1.3：判定侧只做投影，影子读取同样只读不改写

两个机制各管一段：

**时间分流**（`ManifestReadRoutingPolicy`）
清单写入是 E07/E08 上线后才有的能力，早于切换时点的历史采集天然无清单。
按 `captured_at` 与切换时点比较来选路：切换后走清单读取，切换前直接走旧读取，
避免把「历史数据没有清单」误报成控制面故障（E02 条款 2.1 同源理由）。
历史数据补上清单后（回填脚本），分流时点即可前移。

**双读影子**（`ShadowReadReconciler`）
影子期内**旧读取仍是权威**，清单读取只作对照：
两侧证据身份逐一比对，差异记成 `ShadowReadComparison` 供对账，
清单侧任何失败都不得影响判定（E24 确认零差异后才切主）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from services.api.app.application.manifest_backed_packet import ManifestBackedReadError
from services.api.app.application.observation_packet import (
    ObservationArtifact,
    ObservationSource,
)

logger = logging.getLogger(__name__)

SHADOW_COMPARISON_SCHEMA_VERSION = "manifest-shadow-comparison.v1"

# 对账报告中逐条列出的差异上限，超出只报数量
_MAX_REPORTED_DIFFERENCES = 50


class ManifestReadRoute(StrEnum):
    """一次读取的选路结果。"""

    MANIFEST = "manifest"
    LEGACY = "legacy"


class ShadowComparisonOutcome(StrEnum):
    """影子对账结论。"""

    MATCHED = "matched"
    MISMATCHED = "mismatched"
    MANIFEST_READ_FAILED = "manifest_read_failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, kw_only=True)
class ManifestReadRoutingPolicy:
    """按采集时间在清单读取与旧读取之间分流。

    `manifest_cutover_at` 为 None 表示尚未启用清单读取，一律走旧路径。
    """

    manifest_cutover_at: datetime | None = None

    def route_for(self, captured_at: datetime | None) -> ManifestReadRoute:
        if self.manifest_cutover_at is None:
            return ManifestReadRoute.LEGACY
        if captured_at is None:
            # 采集时间缺失无法判定归属，保守走旧读取
            return ManifestReadRoute.LEGACY
        if captured_at < self.manifest_cutover_at:
            return ManifestReadRoute.LEGACY
        return ManifestReadRoute.MANIFEST


@dataclass(frozen=True, kw_only=True)
class ArtifactIdentity:
    """参与对账的证据身份（不含原文）。"""

    artifact_id: str
    artifact_type: str
    size_bytes: int

    def to_audit_payload(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, kw_only=True)
class ShadowReadComparison:
    """一次双读对账结果；只进审计，不影响判定。"""

    task_id: str
    capture_id: str
    outcome: ShadowComparisonOutcome
    compared_at: datetime
    legacy_artifact_count: int = 0
    manifest_artifact_count: int = 0
    missing_in_manifest: tuple[ArtifactIdentity, ...] = ()
    unexpected_in_manifest: tuple[ArtifactIdentity, ...] = ()
    mismatched_size: tuple[ArtifactIdentity, ...] = ()
    reason: str | None = None

    @property
    def matched(self) -> bool:
        return self.outcome is ShadowComparisonOutcome.MATCHED

    @property
    def difference_count(self) -> int:
        return (
            len(self.missing_in_manifest)
            + len(self.unexpected_in_manifest)
            + len(self.mismatched_size)
        )

    def to_audit_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": SHADOW_COMPARISON_SCHEMA_VERSION,
            "task_id": self.task_id,
            "capture_id": self.capture_id,
            "outcome": self.outcome.value,
            "compared_at": self.compared_at.isoformat(),
            "legacy_artifact_count": self.legacy_artifact_count,
            "manifest_artifact_count": self.manifest_artifact_count,
            "difference_count": self.difference_count,
            "missing_in_manifest": [
                item.to_audit_payload()
                for item in self.missing_in_manifest[:_MAX_REPORTED_DIFFERENCES]
            ],
            "unexpected_in_manifest": [
                item.to_audit_payload()
                for item in self.unexpected_in_manifest[:_MAX_REPORTED_DIFFERENCES]
            ],
            "mismatched_size": [
                item.to_audit_payload()
                for item in self.mismatched_size[:_MAX_REPORTED_DIFFERENCES]
            ],
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class ShadowComparisonSink(Protocol):
    """对账结果写入目的地契约。"""

    def record(self, comparison: ShadowReadComparison) -> None: ...


class AsyncShadowComparisonSink(Protocol):
    """可异步落库的对账写入契约。"""

    async def record(self, comparison: ShadowReadComparison) -> None: ...


class LoggingShadowComparisonSink:
    """默认实现：零差异记 info，有差异记 warning，便于日志聚合对账。"""

    def record(self, comparison: ShadowReadComparison) -> None:
        if comparison.matched:
            logger.info(
                "manifest_shadow_matched task_id=%s capture_id=%s artifacts=%s",
                comparison.task_id,
                comparison.capture_id,
                comparison.manifest_artifact_count,
            )
            return
        logger.warning(
            "manifest_shadow_mismatch task_id=%s capture_id=%s outcome=%s differences=%s reason=%s",
            comparison.task_id,
            comparison.capture_id,
            comparison.outcome.value,
            comparison.difference_count,
            comparison.reason,
        )


class InMemoryShadowComparisonSink:
    """内存态对账，供测试与进程内汇总使用。"""

    def __init__(self) -> None:
        self.comparisons: list[ShadowReadComparison] = []

    def record(self, comparison: ShadowReadComparison) -> None:
        self.comparisons.append(comparison)

    @property
    def all_matched(self) -> bool:
        """E24 切主前置条件：影子期内全部零差异。"""
        return bool(self.comparisons) and all(
            item.matched for item in self.comparisons
        )

    @property
    def mismatched(self) -> tuple[ShadowReadComparison, ...]:
        return tuple(item for item in self.comparisons if not item.matched)


class PersistingShadowComparisonSink:
    """内存汇总 + 持久化：跨进程仍可审计的 authoritative 凭证。"""

    def __init__(
        self,
        *,
        memory: InMemoryShadowComparisonSink | None = None,
        repository: Any | None = None,
        logging_sink: LoggingShadowComparisonSink | None = None,
    ) -> None:
        self.memory = memory or InMemoryShadowComparisonSink()
        self._repository = repository
        self._logging = logging_sink or LoggingShadowComparisonSink()

    def record(self, comparison: ShadowReadComparison) -> None:
        self.memory.record(comparison)
        self._logging.record(comparison)

    async def persist(self, comparison: ShadowReadComparison) -> None:
        """将单次对账异步写入持久化仓储；无仓储时为 no-op。"""
        if self._repository is None:
            return
        await self._repository.save_comparison(comparison)

    @property
    def comparisons(self) -> list[ShadowReadComparison]:
        return self.memory.comparisons



class _SourceLoader(Protocol):
    async def load_source(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> ObservationSource | None: ...


class ShadowReadReconciler:
    """影子期：旧读取权威，清单读取只对照。"""

    def __init__(
        self,
        *,
        legacy: _SourceLoader,
        manifest_backed: _SourceLoader,
        sink: ShadowComparisonSink | None = None,
        routing: ManifestReadRoutingPolicy | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._legacy = legacy
        self._manifest_backed = manifest_backed
        self._sink = sink or LoggingShadowComparisonSink()
        self._routing = routing or ManifestReadRoutingPolicy()
        self._now = now or (lambda: datetime.now(UTC))

    async def save_packet(self, **kwargs: object) -> object:
        """影子只改变读取对照，不接管写入：Observation Packet 落库恒走旧权威。

        判定服务把本对账器当完整仓储使用（ObservationPacketService 建包时
        必须 save_packet）；缺这条委托会让 shadow 模式下所有规则判定在建包
        阶段崩溃，质检工作项整批失败。
        """
        writer = getattr(self._legacy, "save_packet", None)
        if not callable(writer):
            raise TypeError("影子对账器的旧读取来源不支持 save_packet 写入")
        return await writer(**kwargs)

    async def load_source(
        self,
        *,
        task_id: str,
        page_capture_id: str,
        captured_at: datetime | None = None,
    ) -> ObservationSource | None:
        """返回旧读取结果；清单读取的差异只记录，不改变返回值。"""
        legacy_source = await self._legacy.load_source(
            task_id=task_id,
            page_capture_id=page_capture_id,
        )
        if legacy_source is None:
            return None
        if self._routing.route_for(captured_at) is ManifestReadRoute.LEGACY:
            comparison = ShadowReadComparison(
                task_id=task_id,
                capture_id=page_capture_id,
                outcome=ShadowComparisonOutcome.SKIPPED,
                compared_at=self._now(),
                legacy_artifact_count=len(legacy_source.artifacts),
                reason="采集时间早于清单切换时点，按分流走旧读取",
            )
            await self._record(comparison)
            return legacy_source
        try:
            manifest_source = await self._manifest_backed.load_source(
                task_id=task_id,
                page_capture_id=page_capture_id,
            )
        except ManifestBackedReadError as exc:
            # 影子期清单读取失败绝不影响判定
            await self._record(
                ShadowReadComparison(
                    task_id=task_id,
                    capture_id=page_capture_id,
                    outcome=ShadowComparisonOutcome.MANIFEST_READ_FAILED,
                    compared_at=self._now(),
                    legacy_artifact_count=len(legacy_source.artifacts),
                    reason=str(exc),
                )
            )
            return legacy_source
        await self._record(
            compare_sources(
                task_id=task_id,
                capture_id=page_capture_id,
                legacy=legacy_source.artifacts,
                manifest=(
                    manifest_source.artifacts if manifest_source is not None else ()
                ),
                compared_at=self._now(),
                reason=(
                    None if manifest_source is not None else "清单读取未返回证据来源"
                ),
            )
        )
        return legacy_source

    async def _record(self, comparison: ShadowReadComparison) -> None:
        self._sink.record(comparison)
        persist = getattr(self._sink, "persist", None)
        if callable(persist):
            await persist(comparison)


def compare_sources(
    *,
    task_id: str,
    capture_id: str,
    legacy: Sequence[ObservationArtifact],
    manifest: Sequence[ObservationArtifact],
    compared_at: datetime,
    reason: str | None = None,
) -> ShadowReadComparison:
    """按证据身份与字节数对账两侧读取结果（不比对原文，避免放大载荷）。"""
    legacy_by_id = {item.artifact_id: item for item in legacy}
    manifest_by_id = {item.artifact_id: item for item in manifest}
    missing = tuple(
        _identity(legacy_by_id[artifact_id])
        for artifact_id in sorted(set(legacy_by_id) - set(manifest_by_id))
    )
    unexpected = tuple(
        _identity(manifest_by_id[artifact_id])
        for artifact_id in sorted(set(manifest_by_id) - set(legacy_by_id))
    )
    mismatched = tuple(
        _identity(manifest_by_id[artifact_id])
        for artifact_id in sorted(set(legacy_by_id) & set(manifest_by_id))
        if len(legacy_by_id[artifact_id].payload) != len(manifest_by_id[artifact_id].payload)
    )
    matched = not (missing or unexpected or mismatched)
    return ShadowReadComparison(
        task_id=task_id,
        capture_id=capture_id,
        outcome=(
            ShadowComparisonOutcome.MATCHED
            if matched
            else ShadowComparisonOutcome.MISMATCHED
        ),
        compared_at=compared_at,
        legacy_artifact_count=len(legacy),
        manifest_artifact_count=len(manifest),
        missing_in_manifest=missing,
        unexpected_in_manifest=unexpected,
        mismatched_size=mismatched,
        reason=reason,
    )


def _identity(artifact: ObservationArtifact) -> ArtifactIdentity:
    return ArtifactIdentity(
        artifact_id=artifact.artifact_id,
        artifact_type=artifact.artifact_type,
        size_bytes=len(artifact.payload),
    )
