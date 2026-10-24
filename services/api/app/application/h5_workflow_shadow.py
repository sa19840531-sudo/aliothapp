"""H5 V1 审计快照到 V2 只读决策投影的 Shadow 记录。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.page_exploration_summary import (
    summarize_working_memory,
)
from services.api.app.application.pipeline_shadow import (
    PipelineShadowService,
    ShadowComparison,
)
from services.api.app.infrastructure.db.pipeline_shadow_repository import (
    PostgresPipelineShadowRepository,
)


@dataclass(frozen=True, kw_only=True)
class H5ShadowDecision:
    """不具备动作执行能力的 H5 V2 决策投影。"""

    conclusion: str
    executes_action: bool
    pending_action_count: int
    page_total: int
    blocked_action_count: int
    quality_status: str


def project_h5_v2_decision(working_memory: Mapping[str, Any]) -> H5ShadowDecision:
    """只读取不可变审计快照，输出候选流程下一步类别。"""
    if not isinstance(working_memory, Mapping):
        raise TypeError("working_memory 必须为不可变审计快照映射")
    assessments = working_memory.get("page_type_assessments")
    if isinstance(assessments, list) and any(
        isinstance(item, Mapping) and item.get("status") == "requires_auth"
        for item in assessments
    ):
        return H5ShadowDecision(
            conclusion="requires_auth",
            executes_action=False,
            pending_action_count=0,
            page_total=0,
            blocked_action_count=0,
            quality_status="needs_review",
        )
    summary = summarize_working_memory(working_memory)
    pending = int(summary.get("pending_action_count") or 0)
    if pending > 0:
        conclusion = "continue"
    elif summary.get("complete") is True and not summary.get("requires_review"):
        conclusion = "completed"
    elif summary.get("requires_review"):
        conclusion = "needs_review"
    else:
        conclusion = "evidence_insufficient"
    return H5ShadowDecision(
        conclusion=conclusion,
        executes_action=False,
        pending_action_count=pending,
        page_total=int(summary.get("page_total") or 0),
        blocked_action_count=int(summary.get("blocked_action_count") or 0),
        quality_status=str(summary.get("quality_status") or "unknown"),
    )


class H5WorkflowShadowRecorder:
    """保存最小 V1/V2 决策差异；不发布 Work，也不持有 Coordinator。"""

    def __init__(
        self,
        *,
        service: PipelineShadowService,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._service = service
        self._now = now or (lambda: datetime.now(UTC))

    async def record(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        working_memory: Mapping[str, Any],
        legacy_conclusion: str | None,
        legacy_duration_ms: int | None = None,
        legacy_input_tokens: int | None = None,
    ) -> str:
        decision = project_h5_v2_decision(working_memory)
        current_page = working_memory.get("current_page")
        capture_id = (
            str(current_page.get("page_capture_id"))
            if isinstance(current_page, Mapping)
            and current_page.get("page_capture_id") is not None
            else None
        )
        return await self._service.record(
            ShadowComparison(
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=capture_id,
                comparison_mode="h5_v1_v2_decision_projection",
                legacy_conclusion=legacy_conclusion,
                candidate_conclusion=decision.conclusion,
                legacy_duration_ms=legacy_duration_ms,
                legacy_input_tokens=legacy_input_tokens,
                summary={
                    "projection_version": "h5-v2-shadow.v1",
                    "projected_at": self._now().isoformat(),
                    "executes_action": decision.executes_action,
                    "pending_action_count": decision.pending_action_count,
                    "page_total": decision.page_total,
                    "blocked_action_count": decision.blocked_action_count,
                    "quality_status": decision.quality_status,
                },
            )
        )


def build_h5_workflow_shadow_recorder() -> H5WorkflowShadowRecorder:
    """构建使用 PostgreSQL Pipeline Shadow 仓储的只读记录器。"""
    return H5WorkflowShadowRecorder(
        service=PipelineShadowService(repository=PostgresPipelineShadowRepository())
    )
