"""Web 运行阶段调度；子页面完成后才能收口父页面。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime

from services.api.app.application.web_run_stage_orchestrator import WebRunStageOrchestrator


@dataclass(frozen=True, kw_only=True)
class WebRuntimeStageResult:
    exploration: object
    preparation: object | None = None
    gap_codes: tuple[str, ...] = ()


class WebRuntimeStageRunner:
    def __init__(self, *, checkpoints, effects=None, worker_id=None, effects_factory=None):
        self._checkpoints = checkpoints
        self._effects = effects
        self._effects_factory = effects_factory
        self._worker_id = worker_id or f"web-stage-{os.getpid()}"
        self._orchestrator = None

    def configure_worker(self, worker_id):
        self._worker_id = worker_id

    async def initialize(self, command, *, adult_self_attestation):
        if self._effects_factory is not None:
            self._effects = self._effects_factory(command, adult_self_attestation)
        await self._effects.initialize()
        self._orchestrator = WebRunStageOrchestrator(
            checkpoints=self._checkpoints, effects=self._effects, worker_id=self._worker_id
        )

    async def advance_once(self, run_id, logical_page_key):
        if self._orchestrator is None:
            raise RuntimeError("阶段调度器尚未初始化")
        return await self._orchestrator.advance_once(run_id, logical_page_key)

    async def final_result(self, run_id):
        cursors = await self._checkpoints.list_for_run(run_id=run_id)
        return WebRuntimeStageResult(
            exploration=await self._effects.final_result(run_id),
            gap_codes=tuple(dict.fromkeys(code for cursor in cursors for code in cursor.gap_codes)),
        )

    async def next_due(self, run_id):
        while True:
            cursors = await self._checkpoints.list_for_run(run_id=run_id)
            pending = tuple(cursor for cursor in cursors if not cursor.is_terminal)
            if not pending:
                return None
            # 广告必须在其业务页仍为 current 时排空；不依赖创建时间打破同刻排序。
            ads = tuple(cursor for cursor in pending if cursor.logical_page_key.startswith("ad:"))
            auxiliary = tuple(
                cursor for cursor in pending
                if cursor.logical_page_key in {"aux:search", "aux:activity"}
            )
            collection = tuple(cursor for cursor in pending
                               if cursor.logical_page_key.startswith(
                                   ("collection:", "collection-detail:")))
            selected = max(
                ads or collection or auxiliary or pending,
                key=lambda cursor: (cursor.created_at, cursor.logical_page_key),
            )
            now = datetime.now(UTC)
            deadline = selected.next_attempt_at or selected.lease_expires_at
            if deadline is not None and deadline > now and selected.next_stage is None:
                await asyncio.sleep(min(1.0, (deadline - now).total_seconds()))
                continue
            return selected


def build_web_runtime_stage_runner(*, runtime, record, navigator):
    """生产默认组合根；实例化不读取数据库，初始化由 executor 的 Worker 身份驱动。"""
    from services.api.app.application.web_runtime_stage_effects import WebRuntimeStageEffects
    from services.api.app.application.web_runtime_stage_frontier import WebRuntimeStageFrontier
    from services.api.app.infrastructure.db.web_run_stage_checkpoint_repository import (
        PostgresWebRunStageCheckpointRepository,
    )
    from services.api.app.infrastructure.db.web_runtime_stage_evidence_repository import (
        PostgresWebRuntimeStageEvidenceRepository,
    )

    checkpoints = PostgresWebRunStageCheckpointRepository()
    evidence = PostgresWebRuntimeStageEvidenceRepository(
        task_id=record.task_id, run_id=record.run_id, session_id=f"web:{record.run_id}"
    )

    def effects_factory(command, adult_self_attestation):
        site = runtime.navigation_context.resolve_site_context(command.target_url).site
        return WebRuntimeStageEffects(
            runtime=runtime,
            evidence=evidence,
            command=command,
            checkpoints=checkpoints,
            adult_self_attestation=adult_self_attestation,
            frontier=WebRuntimeStageFrontier(
                ledger=runtime.budget_ledger,
                candidate_source=runtime.navigation_candidate_source,
                navigator=navigator,
                site=site,
                evidence=evidence,
            ),
        )

    return WebRuntimeStageRunner(checkpoints=checkpoints, effects_factory=effects_factory)
