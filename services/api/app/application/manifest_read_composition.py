"""E24：判定侧证据读取来源的生产组合根。

按 `ALIOTH_EVIDENCE_MANIFEST_READ_MODE` 决议有效模式，并装配对应读取来源：

- `off`（默认）：`PostgresObservationPacketRepository`，与改造前完全一致；
- `shadow`：`ShadowReadReconciler`，旧读取权威、清单读取只对账（E23）；
- `on`：`ManifestBackedObservationRepository`，清单读取为主路径（E22）。

请求 `on` 但缺少零差异凭证时降级为 `shadow`（E24 验收）。
切主凭证优先使用调用方传入或持久化的 shadow summary；影子对账同时写入
`qc_manifest_shadow_comparison`，重启后仍可审计，不依赖进程内存。

回滚：把模式改回 `shadow`／`off` 并重建（进程重启或清缓存），
读取来源立即退回旧路径；全程不改写原始证据，无需数据修复。
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from services.api.app.application.manifest_backed_observation import (
    ManifestBackedObservationRepository,
)
from services.api.app.application.manifest_backed_packet import (
    ManifestBackedPacketLoader,
)
from services.api.app.application.manifest_read_cutover import (
    ManifestReadMode,
    ManifestReadModeDecision,
    ManifestReadModeResolver,
    ShadowReconciliationSummary,
    build_observation_source_reader,
    summarize_comparisons,
)
from services.api.app.application.manifest_read_shadow import (
    InMemoryShadowComparisonSink,
    ManifestReadRoutingPolicy,
    PersistingShadowComparisonSink,
    ShadowReadReconciler,
)
from services.api.app.core.config import EvidenceManifestReadSettings, get_settings
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.manifest_shadow_comparison_repository import (
    PostgresManifestShadowComparisonRepository,
)
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
)
from services.api.app.infrastructure.storage.factory import build_evidence_artifact_store

logger = logging.getLogger(__name__)


class ManifestReadComposition:
    """一次装配的读取来源与其决议留痕。"""

    def __init__(
        self,
        *,
        reader: object,
        decision: ManifestReadModeDecision,
        shadow_sink: InMemoryShadowComparisonSink | PersistingShadowComparisonSink | None = None,
    ) -> None:
        self.reader = reader
        self.decision = decision
        self.shadow_sink = shadow_sink

    @property
    def effective_mode(self) -> ManifestReadMode:
        return self.decision.effective_mode

    def current_summary(self):
        """本进程影子期至今的对账汇总，供运维确认零差异。"""
        if self.shadow_sink is None:
            return summarize_comparisons([])
        return summarize_comparisons(self.shadow_sink.comparisons)


def build_manifest_read_composition(
    *,
    settings: EvidenceManifestReadSettings | None = None,
    summary: ShadowReconciliationSummary | None = None,
    shadow_repository: PostgresManifestShadowComparisonRepository | None = None,
) -> ManifestReadComposition:
    """按配置装配判定侧证据读取来源。

    `summary` 未传入且请求 `on` 时，尽力从持久化对账表加载切主凭证。
    """
    resolved = settings or get_settings().evidence_manifest_read
    requested = ManifestReadMode(resolved.mode)
    legacy = PostgresObservationPacketRepository()
    repository = shadow_repository or PostgresManifestShadowComparisonRepository()
    effective_summary = summary
    if effective_summary is None and requested is ManifestReadMode.ON:
        effective_summary = _load_persisted_summary_best_effort(repository)
    decision = ManifestReadModeResolver(
        minimum_compared_captures=resolved.minimum_compared_captures
    ).resolve(requested_mode=requested, summary=effective_summary)
    if decision.effective_mode is ManifestReadMode.OFF:
        return ManifestReadComposition(reader=legacy, decision=decision)

    manifest_backed = ManifestBackedObservationRepository(
        inner=legacy,
        packet_loader=ManifestBackedPacketLoader(
            manifests=PostgresEvidenceManifestRepository(),
            artifact_reader=build_evidence_artifact_store(
                get_settings().evidence_storage
            ),
        ),
    )
    if decision.effective_mode is ManifestReadMode.ON:
        return ManifestReadComposition(reader=manifest_backed, decision=decision)

    shadow_sink = PersistingShadowComparisonSink(repository=repository)
    routing = ManifestReadRoutingPolicy(manifest_cutover_at=resolved.cutover_at)
    reader = build_observation_source_reader(
        decision=decision,
        legacy=legacy,
        manifest_backed=manifest_backed,
        shadow_factory=lambda inner_legacy, inner_manifest: ShadowReadReconciler(
            legacy=inner_legacy,
            manifest_backed=inner_manifest,
            sink=shadow_sink,
            routing=routing,
        ),
    )
    return ManifestReadComposition(
        reader=reader,
        decision=decision,
        shadow_sink=shadow_sink,
    )


async def load_manifest_shadow_summary(
    *,
    repository: PostgresManifestShadowComparisonRepository | None = None,
) -> ShadowReconciliationSummary:
    """异步读取持久化影子对账汇总。"""
    repo = repository or PostgresManifestShadowComparisonRepository()
    return await repo.load_reconciliation_summary()


def _load_persisted_summary_best_effort(
    repository: PostgresManifestShadowComparisonRepository,
) -> ShadowReconciliationSummary | None:
    """同步装配路径尽力加载持久化凭证；失败则返回 None（保持降级 shadow）。"""

    async def _load() -> ShadowReconciliationSummary:
        return await repository.load_reconciliation_summary()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        try:
            return asyncio.run(_load())
        except Exception:  # noqa: BLE001 - 装配失败不得阻断判定，降级即可
            logger.warning("加载 Manifest 影子对账凭证失败，on 将降级为 shadow", exc_info=True)
            return None

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(_load())).result(timeout=10)
    except Exception:  # noqa: BLE001 - 装配失败不得阻断判定，降级即可
        logger.warning("加载 Manifest 影子对账凭证失败，on 将降级为 shadow", exc_info=True)
        return None
