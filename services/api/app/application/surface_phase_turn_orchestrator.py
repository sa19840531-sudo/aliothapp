"""W17-W20 Surface 阶段单轮应用编排入口。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.api.app.application.surface_phase_action_handoff import (
    SurfacePhaseActionHandoff,
    build_surface_phase_action_handoff,
)
from services.api.app.application.surface_phase_observation_bridge import (
    SurfacePhaseObservationBridgeResult,
    build_surface_phase_observation,
)
from services.api.app.application.surface_phase_runtime import (
    SurfacePhaseRepository,
    SurfacePhaseRuntime,
    SurfacePhaseTurnResult,
)
from services.api.app.domain.exploration import SurfacePhaseRunSnapshot


@dataclass(frozen=True, kw_only=True)
class SurfacePhaseOrchestratedTurn:
    """阶段单轮完成后的运行时结果与下一步交接。"""

    observation_bridge: SurfacePhaseObservationBridgeResult
    turn_result: SurfacePhaseTurnResult
    action_handoff: SurfacePhaseActionHandoff | None = None

    @property
    def is_terminal(self) -> bool:
        return self.turn_result.finished_run is not None

    @property
    def needs_interaction(self) -> bool:
        return self.action_handoff is not None


class SurfacePhaseTurnOrchestrator:
    """组合阶段观测、领域判定、账本写入和动作交接。"""

    def __init__(self, *, repository: SurfacePhaseRepository) -> None:
        self._runtime = SurfacePhaseRuntime(repository=repository)

    async def run_turn(
        self,
        *,
        run: SurfacePhaseRunSnapshot,
        page_detection_result: Mapping[str, Any],
        observed_at: datetime | None = None,
        action_deadline: datetime,
    ) -> SurfacePhaseOrchestratedTurn:
        """执行一次阶段判断，并在需要动作时返回 Interaction Runtime 交接合同。"""

        bridge = build_surface_phase_observation(
            run=run,
            page_detection_result=page_detection_result,
            observed_at=observed_at,
        )
        turn_result = await self._runtime.evaluate_and_record(
            run=run,
            context=bridge.context,
            observation=bridge.observation,
        )
        next_action = turn_result.decision.next_action
        if next_action is None:
            return SurfacePhaseOrchestratedTurn(
                observation_bridge=bridge,
                turn_result=turn_result,
            )
        handoff = build_surface_phase_action_handoff(
            run=run,
            decision=turn_result.decision,
            deadline=action_deadline,
        )
        return SurfacePhaseOrchestratedTurn(
            observation_bridge=bridge,
            turn_result=turn_result,
            action_handoff=handoff,
        )
