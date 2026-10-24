"""跨端流程基建 P2：触发／claim／挂起／恢复／预算（联调清单 P2）。

设计依据：2026-10-22 §5–§7。不连接数据库；应用层以本模块守卫做原子 claim／hold 语义。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from services.api.app.domain.exploration.flow_foundation import (
    FlowEvidenceState,
    FlowExecutionResult,
    FlowFoundationContractError,
    FlowResumeResult,
    FlowTriStateOutcome,
    TargetPlatform,
)
from services.api.app.domain.protocol import require_text, stable_protocol_hash

FLOW_CONTROL_CONTRACT_VERSION = "flow-foundation.p2.control.v1"

# 触发键禁止纳入的易变／非业务字段（设计 §5.2；T01／T02）。
TRIGGER_KEY_FORBIDDEN_FIELDS = frozenset(
    {
        "screenshot_hash",
        "image_hash",
        "capture_hash",
        "playback_seconds",
        "playback_position_ms",
        "media_position",
        "upload_time",
        "uploaded_at",
        "received_at",
        "worker_id",
        "session_id",
        "capture_id",
        "current_capture_id",
        "full_dom",
        "dom_hash",
        "ui_tree_hash",
    }
)


class TriggerDecisionKind(StrEnum):
    ELIGIBLE = "eligible"
    DEFERRED = "deferred"
    SKIPPED = "skipped"


class TriggerReasonCode(StrEnum):
    READY = "ready"
    OBSERVATION_STALE = "observation_stale"
    SUBJECT_UNCONFIRMED = "subject_unconfirmed"
    REQUIREMENT_MISSING = "requirement_missing"
    CAPABILITY_PARTIAL = "capability_partial"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    PERMISSION_DENIED = "permission_denied"
    BUDGET_INSUFFICIENT = "budget_insufficient"
    ALREADY_CLAIMED = "already_claimed"
    NESTED_FLOW_FORBIDDEN = "nested_flow_forbidden"
    CLEAR_DATA_NOT_RETURN = "clear_data_not_return"
    CANCELLED = "cancelled"
    HARD_DEADLINE = "hard_deadline"


class BudgetReservationStatus(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    OUTCOME_UNKNOWN = "outcome_unknown"


class ParentHoldStatus(StrEnum):
    ACTIVE = "active"
    RELEASED = "released"
    RECONCILED = "reconciled"


@dataclass(frozen=True, kw_only=True)
class TriggerDecision:
    """统一 evaluate_trigger 结论：无副作用；eligible／deferred／skipped＋原因码。"""

    kind: TriggerDecisionKind
    reason_code: TriggerReasonCode
    trigger_key: str | None = None
    missing_capabilities: tuple[str, ...] = ()
    next_eval_hint: str = ""
    contract_version: str = FLOW_CONTROL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.kind, TriggerDecisionKind):
            raise FlowFoundationContractError("trigger kind 无效")
        if not isinstance(self.reason_code, TriggerReasonCode):
            raise FlowFoundationContractError("trigger reason_code 无效")
        if self.kind is TriggerDecisionKind.ELIGIBLE:
            require_text(self.trigger_key or "", "trigger_key")


@dataclass(frozen=True, kw_only=True)
class TriggerKeyParts:
    """稳定触发键组成；禁止混入截图 hash／播放秒／上传时间等。"""

    task_id: str
    exploration_scope_id: str
    target_platform: TargetPlatform | str
    flow_code: str
    definition_hash: str
    target_scope: str
    semantic_page_identity: str
    subject_or_occurrence: str
    trigger_state_hash: str
    requirement_contract_hash: str

    def __post_init__(self) -> None:
        for name in (
            "task_id",
            "exploration_scope_id",
            "flow_code",
            "definition_hash",
            "target_scope",
            "semantic_page_identity",
            "subject_or_occurrence",
            "trigger_state_hash",
            "requirement_contract_hash",
        ):
            require_text(getattr(self, name), name)


def assert_trigger_state_payload_allowed(payload: Mapping[str, Any]) -> None:
    """trigger_state 只允许业务相关投影；禁止易变技术字段。"""

    for key in payload:
        normalized = str(key).strip().casefold()
        if normalized in TRIGGER_KEY_FORBIDDEN_FIELDS:
            raise FlowFoundationContractError(
                f"触发键／状态投影禁止字段: {key}"
            )


def build_trigger_key(parts: TriggerKeyParts) -> str:
    platform = (
        parts.target_platform.value
        if isinstance(parts.target_platform, TargetPlatform)
        else str(parts.target_platform).strip().casefold()
    )
    return "flow-trigger:" + stable_protocol_hash(
        {
            "contract_version": FLOW_CONTROL_CONTRACT_VERSION,
            "task_id": parts.task_id,
            "exploration_scope_id": parts.exploration_scope_id,
            "target_platform": platform,
            "flow_code": parts.flow_code,
            "definition_hash": parts.definition_hash,
            "target_scope": parts.target_scope,
            "semantic_page_identity": parts.semantic_page_identity,
            "subject_or_occurrence": parts.subject_or_occurrence,
            "trigger_state_hash": parts.trigger_state_hash,
            "requirement_contract_hash": parts.requirement_contract_hash,
        }
    )


def evaluate_trigger(
    *,
    observation_fresh: bool,
    subject_confirmed: bool,
    has_requirement_or_explore_goal: bool,
    capabilities_ready: bool,
    capabilities_partial_accepted: bool,
    permission_allowed: bool,
    budget_sufficient: bool,
    already_claimed: bool,
    cancelled: bool = False,
    hard_deadline_passed: bool = False,
    trigger_key: str | None = None,
    missing_capabilities: tuple[str, ...] = (),
) -> TriggerDecision:
    """观察后、领主探索动作前的统一准入（T01／T03）。"""

    if cancelled:
        return TriggerDecision(
            kind=TriggerDecisionKind.SKIPPED,
            reason_code=TriggerReasonCode.CANCELLED,
        )
    if hard_deadline_passed:
        return TriggerDecision(
            kind=TriggerDecisionKind.SKIPPED,
            reason_code=TriggerReasonCode.HARD_DEADLINE,
        )
    if already_claimed:
        return TriggerDecision(
            kind=TriggerDecisionKind.SKIPPED,
            reason_code=TriggerReasonCode.ALREADY_CLAIMED,
            trigger_key=trigger_key,
        )
    if not observation_fresh:
        return TriggerDecision(
            kind=TriggerDecisionKind.DEFERRED,
            reason_code=TriggerReasonCode.OBSERVATION_STALE,
            next_eval_hint="wait_fresh_observation",
        )
    if not subject_confirmed:
        return TriggerDecision(
            kind=TriggerDecisionKind.DEFERRED,
            reason_code=TriggerReasonCode.SUBJECT_UNCONFIRMED,
            next_eval_hint="confirm_subject",
        )
    if not has_requirement_or_explore_goal:
        return TriggerDecision(
            kind=TriggerDecisionKind.SKIPPED,
            reason_code=TriggerReasonCode.REQUIREMENT_MISSING,
        )
    if not capabilities_ready and not capabilities_partial_accepted:
        code = (
            TriggerReasonCode.CAPABILITY_UNSUPPORTED
            if missing_capabilities
            else TriggerReasonCode.CAPABILITY_PARTIAL
        )
        return TriggerDecision(
            kind=TriggerDecisionKind.SKIPPED,
            reason_code=code,
            missing_capabilities=missing_capabilities,
        )
    if not permission_allowed:
        return TriggerDecision(
            kind=TriggerDecisionKind.SKIPPED,
            reason_code=TriggerReasonCode.PERMISSION_DENIED,
        )
    if not budget_sufficient:
        return TriggerDecision(
            kind=TriggerDecisionKind.DEFERRED,
            reason_code=TriggerReasonCode.BUDGET_INSUFFICIENT,
            next_eval_hint="wait_budget_or_skip",
        )
    return TriggerDecision(
        kind=TriggerDecisionKind.ELIGIBLE,
        reason_code=TriggerReasonCode.READY,
        trigger_key=require_text(trigger_key or "", "trigger_key"),
        missing_capabilities=missing_capabilities,
    )


@dataclass(frozen=True, kw_only=True)
class ParentWorkHold:
    """持久挂起主待办（T05）；禁止只存调用栈。"""

    hold_id: str
    task_id: str
    phase_run_id: str
    parent_work_kind: str
    parent_work_id: str
    parent_version: int
    owner_execution_segment_id: str
    status: ParentHoldStatus = ParentHoldStatus.ACTIVE
    frontier_ref: str = ""
    resume_snapshot_ref: str = ""

    def __post_init__(self) -> None:
        for name in (
            "hold_id",
            "task_id",
            "phase_run_id",
            "parent_work_kind",
            "parent_work_id",
            "owner_execution_segment_id",
        ):
            require_text(getattr(self, name), name)
        if int(self.parent_version) < 0:
            raise FlowFoundationContractError("parent_version 无效")
        if not isinstance(self.status, ParentHoldStatus):
            raise FlowFoundationContractError("hold status 无效")


def assert_single_active_parent_hold(
    *,
    existing_active_hold_id: str | None,
    claimant_hold_id: str,
) -> None:
    """同一父待办最多被一个活动流程阻断（T04／T05）。"""

    require_text(claimant_hold_id, "claimant_hold_id")
    if existing_active_hold_id is None:
        return
    if existing_active_hold_id != claimant_hold_id:
        raise FlowFoundationContractError(
            "父待办已有活动 hold，禁止第二流程阻断"
        )


@dataclass(frozen=True, kw_only=True)
class ResumeEquivalenceFacts:
    """App 恢复核验维度（T10）；同 Activity 不足以证明可回源。"""

    package_name: str
    activity: str
    process_or_data_container: str
    account_context: str
    subject_ref: str

    def __post_init__(self) -> None:
        for name in (
            "package_name",
            "activity",
            "process_or_data_container",
            "account_context",
            "subject_ref",
        ):
            require_text(getattr(self, name), name)


def assess_app_resume_equivalence(
    *,
    expected: ResumeEquivalenceFacts,
    observed: ResumeEquivalenceFacts,
) -> str:
    """返回 restored｜reject_replan。账户／主体变化即使 Activity 相同也拒绝旧目标。"""

    if expected.package_name != observed.package_name:
        return "reject_replan"
    if expected.activity != observed.activity:
        return "reject_replan"
    if expected.process_or_data_container != observed.process_or_data_container:
        return "reject_replan"
    if expected.account_context != observed.account_context:
        return "reject_replan"
    if expected.subject_ref != observed.subject_ref:
        return "reject_replan"
    return "restored"


def assert_clear_data_not_ordinary_return(*, using_clear_data: bool) -> None:
    """清数据 ≠ 普通返回（P2.6）。"""

    if using_clear_data:
        raise FlowFoundationContractError(
            "清数据改变数据容器与账号，不能当作普通返回",
        )


def assert_short_flow_not_nested(*, active_short_flow_depth: int) -> None:
    """首期禁止短流程递归（P2.7）；嵌套深度须为 0。"""

    if int(active_short_flow_depth) > 0:
        raise FlowFoundationContractError(
            "短流程禁止递归触发短流程",
        )


@dataclass(frozen=True, kw_only=True)
class BudgetReservation:
    """流程预算预约；unknown 不得先退款再盲重试（T03／T09）。"""

    consumption_id: str
    task_id: str
    phase_run_id: str
    units: int
    status: BudgetReservationStatus

    def __post_init__(self) -> None:
        require_text(self.consumption_id, "consumption_id")
        require_text(self.task_id, "task_id")
        require_text(self.phase_run_id, "phase_run_id")
        if int(self.units) <= 0:
            raise FlowFoundationContractError("budget units 必须为正")
        if not isinstance(self.status, BudgetReservationStatus):
            raise FlowFoundationContractError("budget status 无效")


def decide_budget_after_unknown(
    *,
    outcome_unknown: bool,
    proved_not_consumed: bool,
) -> BudgetReservationStatus:
    """未知结果：保留预留／标 outcome_unknown；仅证明未消耗才可释放。"""

    if not outcome_unknown:
        return BudgetReservationStatus.COMMITTED
    if proved_not_consumed:
        return BudgetReservationStatus.RELEASED
    return BudgetReservationStatus.OUTCOME_UNKNOWN


def settle_cancel_or_deadline_outcome(
    *,
    cancelled: bool,
    hard_deadline_passed: bool,
    resume_attempted: bool,
    resume_succeeded: bool,
    evidence_complete: bool,
) -> FlowTriStateOutcome:
    """取消／硬截止／回源失败：三项结果分立，不虚报恢复（T09）。"""

    if cancelled or hard_deadline_passed:
        execution = FlowExecutionResult.CANCELLED
    else:
        execution = FlowExecutionResult.FAILED
    evidence = (
        FlowEvidenceState.COMPLETE
        if evidence_complete
        else FlowEvidenceState.INCOMPLETE
    )
    if not resume_attempted:
        resume = FlowResumeResult.NOT_REQUIRED
    elif resume_succeeded:
        # 取消／截止后即使表面回到原页，也不得虚报已恢复主探索。
        resume = (
            FlowResumeResult.CANCELLED
            if cancelled or hard_deadline_passed
            else FlowResumeResult.RESTORED
        )
    else:
        resume = (
            FlowResumeResult.CANCELLED
            if cancelled or hard_deadline_passed
            else FlowResumeResult.FAILED
        )
    return FlowTriStateOutcome(
        execution_result=execution,
        evidence_state=evidence,
        resume_result=resume,
    )


@dataclass(frozen=True, kw_only=True)
class FlowClaimRecord:
    """原子 claim 结果投影（T04）：唯一实例＋唯一预算预留＋唯一 hold。"""

    trigger_key: str
    phase_run_id: str
    consumption_id: str
    hold_id: str
    parent_work_id: str
    parent_version: int

    def __post_init__(self) -> None:
        for name in (
            "trigger_key",
            "phase_run_id",
            "consumption_id",
            "hold_id",
            "parent_work_id",
        ):
            require_text(getattr(self, name), name)
