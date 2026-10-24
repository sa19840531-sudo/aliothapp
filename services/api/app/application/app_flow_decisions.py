"""主流程的无触控观察及终态决策。"""

import hashlib
import json

from services.api.app.domain.exploration import ExplorationTerminal, ExplorationTerminalStatus
from services.api.app.domain.interaction import CaptureDirective, TimedObservationIntent


def flow_passive_decision(work, state):
    """将进度放入可序列化决策，支持观察后继与终态审计。"""
    if state.phase == "done":
        state.compute_bottom_coverage()
        failed = (
            state.stop_reason
            or any(value in {"failed", "blocked"} for value in state.stage_results.values())
            or state.bottom_coverage_status != "complete"
        )
        state.mark_stage("done", "blocked" if failed else "passed")
        return ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.BLOCKED
            if failed
            else ExplorationTerminalStatus.COMPLETED,
            reason=state.stop_reason or "app_flow_finished",
            coverage_summary={
                "tangguo_flow_state": state.to_dict(),
                "tangguo_flow_report": state.as_report(),
            },
        )
    payload = state.to_dict()
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return TimedObservationIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        phase_run_id=f"app-flow:{work.task_id}",
        decision_id=f"app-flow-wait:{work.source_capture_id}:{digest}",
        observation_id=f"app-flow-observe:{work.source_capture_id}:{digest}",
        source_capture_id=work.source_capture_id,
        surface_kind=work.surface_kind,
        delay_ms=500,
        directive=CaptureDirective(role="observation", artifact_types=("screenshot",)),
        deadline=work.deadline,
        workflow_state=payload,
    )
