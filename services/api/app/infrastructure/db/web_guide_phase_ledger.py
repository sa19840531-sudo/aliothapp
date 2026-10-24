"""Web 引导页可选 Phase 审计；不产生主线 Stage gap。"""

from __future__ import annotations

import asyncio
import logging

from services.api.app.application.web_guide_candidate import WebGuideCandidate
from services.api.app.application.web_guide_page import WebGuideOutcome
from services.api.app.domain.exploration.surface_phase import (
    SurfacePhaseRunStatus,
    SurfacePhaseStepStatus,
    build_surface_phase_run_id,
)
from services.api.app.domain.interaction import SurfaceKind

_PHASE_KEY = "web_guide_page"
_PROTOCOL = "web-guide-page.v1"
_LOGGER = logging.getLogger(__name__)


class WebGuidePhaseLedger:
    def __init__(self, *, gate, phases):
        self._gate = gate
        self._phases = phases

    async def claim(self, *, candidate: WebGuideCandidate, task_id: str, session_id: str):
        registration = asyncio.create_task(
            self._claim_once(candidate=candidate, task_id=task_id, session_id=session_id)
        )
        try:
            return await asyncio.shield(registration)
        except asyncio.CancelledError:
            # 登记可能已提交，仍须取得确切返回值并完成本次阶段的终态交接。
            cleanup = asyncio.create_task(self._skip_abandoned_claim(registration, candidate))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    # 重复取消不能打断命令租约释放及已知阶段的 skipped 审计。
                    continue
            try:
                cleanup.result()
            except Exception:  # noqa: BLE001 - 所有权未知或审计失败保持活动围栏
                _LOGGER.warning("web_guide_claim_cleanup_failed")
            raise

    async def _skip_abandoned_claim(self, registration, candidate) -> None:
        phase = await registration
        if phase is not None:
            await self._complete_skipped(phase, candidate, "WEB_GUIDE_MAINLINE_FINISHED")

    async def _claim_once(self, *, candidate: WebGuideCandidate, task_id: str, session_id: str):
        phase_id = build_surface_phase_run_id(
            task_id=task_id, session_id=session_id,
            phase_key=_PHASE_KEY, idempotency_key=candidate.candidate_id,
        )
        phase = None
        try:
            async with self._gate.hold(
                task_id=task_id, session_id=session_id, surface_kind=SurfaceKind.WEB,
            ) as permit:
                # 在跨进程命令租约内复查，避免并发 Worker 重放同一入口。
                if await self._phases.get_run(phase_run_id=phase_id) is not None:
                    return None
                phase = await self._phases.start_run(
                    task_id=task_id,
                    session_id=session_id,
                    surface_kind=SurfaceKind.WEB,
                    phase_key=_PHASE_KEY,
                    phase_protocol_version=_PROTOCOL,
                    idempotency_key=candidate.candidate_id,
                    source_capture_id=candidate.source_capture_id,
                    context_payload={
                        "candidate_id": candidate.candidate_id,
                        "destination_url": candidate.destination_url,
                        "kind": candidate.kind,
                        "source_url": candidate.source_url,
                    },
                    permit=permit,
                )
        except Exception:
            # 只有本次 start_run 明确返回的阶段可补偿，不能查猜未知登记的所有权。
            if phase is not None:
                try:
                    await self._complete_skipped(phase, candidate, "WEB_GUIDE_CLAIM_FAILED")
                except Exception:  # noqa: BLE001 - 保留原门禁失败与未完成的真实活动
                    _LOGGER.warning("web_guide_claim_cleanup_failed")
            raise
        return phase

    async def _complete_skipped(self, phase, candidate, reason_code) -> None:
        await self.complete(
            phase=phase,
            outcome=WebGuideOutcome(
                "skipped", reason_code, candidate.candidate_id, candidate.source_capture_id,
            )
        )

    async def complete(self, *, phase, outcome: WebGuideOutcome) -> None:
        succeeded = outcome.status in {"confirmed", "uncertain", "skipped"}
        await self._phases.record_step(
            phase_run_id=phase.phase_run_id,
            step_key="terminal",
            status=(
                SurfacePhaseStepStatus.COMPLETED
                if succeeded else SurfacePhaseStepStatus.FAILED
            ),
            decision_status=outcome.status,
            reason_code=outcome.reason_code,
            source_capture_id=outcome.source_capture_id,
            result_capture_id=outcome.capture_id,
            decision_payload={
                "candidate_id": outcome.candidate_id,
                "capture_id": outcome.capture_id,
                "page_type": outcome.page_type,
            },
            evidence_artifact_ids=outcome.artifact_ids,
        )
        fresh = await self._phases.get_run(phase_run_id=phase.phase_run_id)
        await self._phases.finish_run(
            phase_run_id=phase.phase_run_id,
            status=(SurfacePhaseRunStatus.COMPLETED if succeeded else SurfacePhaseRunStatus.FAILED),
            terminal_reason_code=outcome.reason_code,
            expected_row_version=fresh.row_version,
        )
