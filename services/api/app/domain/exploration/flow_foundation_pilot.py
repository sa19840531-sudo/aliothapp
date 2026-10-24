"""跨端流程基建 P4：App 试点范围与验收门禁（联调清单 P4）。

设计依据：2026-10-22 §13／§14；纯观察先于低风险交互回源。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from services.api.app.domain.exploration.flow_foundation import (
    FORBIDDEN_AUTO_OPEN_CAPABILITY_IDS,
    FlowFoundationContractError,
    FlowTriStateOutcome,
    assert_pilot_capabilities_allowed,
)
from services.api.app.domain.protocol import require_text

FLOW_PILOT_CONTRACT_VERSION = "flow-foundation.p4.pilot.v1"


class PilotScenario(StrEnum):
    PURE_OBSERVATION = "pure_observation"
    LOW_RISK_INTERACT_RETURN = "low_risk_interact_return"


class PilotAcceptanceStatus(StrEnum):
    PENDING = "pending"
    PASSED = "passed"
    BLOCKED = "blocked"
    FAILED = "failed"


@dataclass(frozen=True, kw_only=True)
class PilotPlan:
    """试点声明：业务语义、能力清单、是否宣称播放验证。"""

    scenario: PilotScenario
    flow_code: str
    capability_ids: tuple[str, ...]
    claims_playback_proof: bool = False
    uses_clear_data: bool = False
    uses_payment: bool = False
    uses_unauthorized_deep_link: bool = False
    detail_business_state_reached: bool = False
    interact_authorized: bool = False
    resume_verification_required: bool = False
    contract_version: str = FLOW_PILOT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, PilotScenario):
            raise FlowFoundationContractError("pilot scenario 无效")
        require_text(self.flow_code, "flow_code")


@dataclass(frozen=True, kw_only=True)
class PilotAcceptanceRecord:
    scenario: PilotScenario
    status: PilotAcceptanceStatus
    reason_code: str
    tri_state: FlowTriStateOutcome | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.scenario, PilotScenario):
            raise FlowFoundationContractError("pilot scenario 无效")
        if not isinstance(self.status, PilotAcceptanceStatus):
            raise FlowFoundationContractError("pilot status 无效")
        require_text(self.reason_code, "reason_code")


def assert_pilot_plan_allowed(plan: PilotPlan) -> None:
    """P4 启动前硬门禁：禁止支付／清数据／未授权 deep link；不冒充播放验证。"""

    if plan.uses_clear_data or plan.uses_payment or plan.uses_unauthorized_deep_link:
        raise FlowFoundationContractError(
            "试点禁止用清数据／支付／未授权 deep link 凑验收"
        )
    forbidden = set(plan.capability_ids) & FORBIDDEN_AUTO_OPEN_CAPABILITY_IDS
    if forbidden:
        raise FlowFoundationContractError(
            f"试点能力清单含禁止项: {sorted(forbidden)}"
        )
    assert_pilot_capabilities_allowed(plan.capability_ids)
    if plan.claims_playback_proof:
        raise FlowFoundationContractError(
            "App 试点不得宣称正式播放验证通过"
        )
    if plan.scenario is PilotScenario.PURE_OBSERVATION:
        if not plan.detail_business_state_reached:
            raise FlowFoundationContractError(
                "纯观察试点须已到达详情类业务态"
            )
        if plan.resume_verification_required:
            raise FlowFoundationContractError(
                "纯观察试点不应要求交互回源核验"
            )
    if plan.scenario is PilotScenario.LOW_RISK_INTERACT_RETURN:
        if not plan.interact_authorized:
            raise FlowFoundationContractError(
                "低风险交互试点须已授权展开／关闭"
            )
        if not plan.resume_verification_required:
            raise FlowFoundationContractError(
                "低风险交互试点必须含受控回源核验"
            )


def evaluate_pure_observation_pilot(
    *,
    plan: PilotPlan,
    triggered: bool,
    samples_recorded: bool,
    seal_or_gap_explained: bool,
    claimed_playback_pass: bool,
) -> PilotAcceptanceRecord:
    """P4.1：触发→采样→封存／缺口可解释；不冒充播放验证。"""

    assert_pilot_plan_allowed(plan)
    if plan.scenario is not PilotScenario.PURE_OBSERVATION:
        raise FlowFoundationContractError("场景必须为 pure_observation")
    if claimed_playback_pass:
        return PilotAcceptanceRecord(
            scenario=plan.scenario,
            status=PilotAcceptanceStatus.FAILED,
            reason_code="playback_proof_claimed",
            notes="禁止冒充播放验证",
        )
    if not triggered:
        return PilotAcceptanceRecord(
            scenario=plan.scenario,
            status=PilotAcceptanceStatus.BLOCKED,
            reason_code="not_triggered",
        )
    if not samples_recorded:
        return PilotAcceptanceRecord(
            scenario=plan.scenario,
            status=PilotAcceptanceStatus.FAILED,
            reason_code="samples_missing",
        )
    if not seal_or_gap_explained:
        return PilotAcceptanceRecord(
            scenario=plan.scenario,
            status=PilotAcceptanceStatus.FAILED,
            reason_code="seal_or_gap_unexplained",
        )
    return PilotAcceptanceRecord(
        scenario=plan.scenario,
        status=PilotAcceptanceStatus.PASSED,
        reason_code="pure_observation_closed",
    )


def evaluate_low_risk_interact_return_pilot(
    *,
    plan: PilotPlan,
    action_ledger_present: bool,
    resume_verified: bool,
    tri_state: FlowTriStateOutcome,
    claimed_false_resume: bool,
) -> PilotAcceptanceRecord:
    """P4.2：动作账本＋恢复核验＋三结果；依赖 P1＋P2.5。"""

    assert_pilot_plan_allowed(plan)
    if plan.scenario is not PilotScenario.LOW_RISK_INTERACT_RETURN:
        raise FlowFoundationContractError(
            "场景必须为 low_risk_interact_return"
        )
    if claimed_false_resume:
        return PilotAcceptanceRecord(
            scenario=plan.scenario,
            status=PilotAcceptanceStatus.FAILED,
            reason_code="false_resume_claimed",
            tri_state=tri_state,
        )
    if not action_ledger_present:
        return PilotAcceptanceRecord(
            scenario=plan.scenario,
            status=PilotAcceptanceStatus.FAILED,
            reason_code="action_ledger_missing",
            tri_state=tri_state,
        )
    if not resume_verified:
        return PilotAcceptanceRecord(
            scenario=plan.scenario,
            status=PilotAcceptanceStatus.FAILED,
            reason_code="resume_not_verified",
            tri_state=tri_state,
        )
    return PilotAcceptanceRecord(
        scenario=plan.scenario,
        status=PilotAcceptanceStatus.PASSED,
        reason_code="low_risk_interact_return_closed",
        tri_state=tri_state,
    )
