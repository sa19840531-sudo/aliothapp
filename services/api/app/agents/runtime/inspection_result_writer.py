"""迁移期 Rule Decision 正式写回适配。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Protocol

from services.api.app.agents.tools.rule_tools import (
    RuleExecutionBatchItem,
    record_rule_execution_for_current_run,
    record_rule_executions_for_current_run,
)
from services.api.app.application.evidence_request_admission import (
    EvidenceRequestAdmissionResult,
)
from services.api.app.application.evidence_request_routing import (
    route_outcome_evidence_requests,
)
from services.api.app.application.supplemental_evidence_outcome import (
    apply_supplemental_evidence_policy,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.domain.inspection import (
    EvidenceRequest,
    InspectionHandoff,
    RuleJudgeOutcome,
)

logger = logging.getLogger(__name__)


class EvidenceRequestAdmissionPort(Protocol):
    """Judge 只受理 Evidence Request 时使用的最小端口。"""

    async def admit(
        self,
        *,
        requests: tuple[EvidenceRequest, ...],
    ) -> EvidenceRequestAdmissionResult: ...


class CurrentRuleDecisionWriter:
    """把已持久化的 Judge 输出写入正式规则结果；不调用任何判定入口。"""

    def __init__(
        self,
        *,
        orchestrator: Any | None = None,
        request_admission: EvidenceRequestAdmissionPort | None = None,
        route_after_admission: bool = False,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
    ) -> None:
        if not isinstance(route_after_admission, bool):
            raise TypeError("route_after_admission 必须为布尔值")
        # 路由层不再发布 ProbeWork，写回不消费 orchestrator；参数保留，组合根不必改接线。
        self._orchestrator = orchestrator
        self._request_admission = request_admission
        self._route_after_admission = route_after_admission
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )

    async def write(
        self,
        *,
        handoff: InspectionHandoff,
        outcome: RuleJudgeOutcome,
    ) -> Mapping[str, Any]:
        if outcome.work_item_id != handoff.work_item_id:
            raise ValueError("Rule Judge Outcome 与 Handoff 工作项不一致")
        if outcome.attempt_id != handoff.attempt_id:
            raise ValueError("Rule Judge Outcome 与 Handoff Attempt 不一致")
        if outcome.task_id != handoff.task_id:
            raise ValueError("Rule Judge Outcome 与 Handoff 任务不一致")
        policy_application = apply_supplemental_evidence_policy(
            outcome=outcome,
            policy=self._supplemental_evidence_policy,
        )
        outcome = policy_application.outcome
        if policy_application.suppressed:
            logger.info(
                "task_id=%s work_item_id=%s 写回前抑制补采请求，request_ids=%s "
                "reason_code=%s policy_version=%s",
                outcome.task_id,
                outcome.work_item_id,
                ",".join(policy_application.suppressed_request_ids),
                policy_application.gate_decision.reason_code,
                policy_application.gate_decision.policy_version,
            )
        payloads = [
            RuleExecutionBatchItem.model_validate(item.to_writer_payload()).model_dump(
                exclude_defaults=True,
                exclude_none=True,
            )
            for item in outcome.decisions
        ]
        admitted_request_ids: tuple[str, ...] = ()
        planned_request_ids: tuple[str, ...] = ()
        if self._request_admission is not None:
            admission = await self._request_admission.admit(
                requests=outcome.evidence_requests,
            )
            admitted_request_ids = admission.admitted_request_ids
        if self._request_admission is None or self._route_after_admission:
            # 路由只受理与选路，不发布 ProbeWork：Probe 要等本端恢复执行器完成恢复并
            # 通过等价门禁后才存在，所以这里拿到的是「已规划、保持 pending」的请求。
            routing = await route_outcome_evidence_requests(
                handoff=handoff,
                outcome=outcome,
                supplemental_evidence_policy=self._supplemental_evidence_policy,
            )
            planned_request_ids = routing.planned_request_ids
        if not payloads:
            return {
                "execution_status": "evidence_requested",
                "retryable": outcome.retryable,
                "evidence_requests": [item.to_dict() for item in outcome.evidence_requests],
                "admitted_evidence_request_ids": list(admitted_request_ids),
                "planned_evidence_request_ids": list(planned_request_ids),
            }
        if len(payloads) == 1:
            recorded = await record_rule_execution_for_current_run(
                page_capture_id=handoff.evidence.page_capture_id,
                **payloads[0],
            )
        else:
            recorded = await record_rule_executions_for_current_run(
                page_capture_id=handoff.evidence.page_capture_id,
                executions=payloads,
            )
        return {
            **dict(recorded),
            "evidence_requests": [item.to_dict() for item in outcome.evidence_requests],
            "retryable": outcome.retryable,
            "admitted_evidence_request_ids": list(admitted_request_ids),
            "planned_evidence_request_ids": list(planned_request_ids),
            "suppressed_evidence_request_ids": list(
                policy_application.suppressed_request_ids
            ),
        }
