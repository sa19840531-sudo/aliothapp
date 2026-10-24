"""流程基建 P2／P3／P4 应用接线：触发 claim、挂起、预算、事件序、试点门禁。

领域合同见 ``flow_foundation_control``／``_evidence``／``_pilot``。
本模块提供进程内可注入实现；PostgreSQL 可替换 Store，不改守卫语义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock
from typing import Protocol

from services.api.app.domain.exploration.flow_foundation import (
    FlowFoundationContractError,
    FlowTriStateOutcome,
)
from services.api.app.domain.exploration.flow_foundation_control import (
    BudgetReservation,
    BudgetReservationStatus,
    FlowClaimRecord,
    ParentHoldStatus,
    ParentWorkHold,
    ResumeEquivalenceFacts,
    TriggerDecision,
    TriggerKeyParts,
    assert_clear_data_not_ordinary_return,
    assert_short_flow_not_nested,
    assert_single_active_parent_hold,
    assert_trigger_state_payload_allowed,
    assess_app_resume_equivalence,
    build_trigger_key,
    decide_budget_after_unknown,
    evaluate_trigger,
    settle_cancel_or_deadline_outcome,
)
from services.api.app.domain.exploration.flow_foundation_evidence import (
    ObservationClockDomain,
    ResourceEventSeqAllocation,
    assert_observed_at_not_persisted_legacy,
)
from services.api.app.domain.exploration.flow_foundation_pilot import (
    PilotAcceptanceRecord,
    PilotPlan,
    PilotScenario,
    assert_pilot_plan_allowed,
    evaluate_low_risk_interact_return_pilot,
    evaluate_pure_observation_pilot,
)


class FlowFoundationControlError(RuntimeError):
    """P2／P3／P4 接线层可路由错误。"""

    def __init__(
        self,
        message: str,
        *,
        error_code: str = "FLOW_FOUNDATION_CONTROL_REJECTED",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retryable = retryable


def _budget_transition_allowed(
    current: BudgetReservationStatus,
    nxt: BudgetReservationStatus,
) -> bool:
    if current is nxt:
        return True
    allowed = {
        BudgetReservationStatus.RESERVED: {
            BudgetReservationStatus.COMMITTED,
            BudgetReservationStatus.RELEASED,
            BudgetReservationStatus.OUTCOME_UNKNOWN,
        },
        BudgetReservationStatus.OUTCOME_UNKNOWN: {
            BudgetReservationStatus.COMMITTED,
            BudgetReservationStatus.OUTCOME_UNKNOWN,
        },
    }
    return nxt in allowed.get(current, set())


class FlowControlStore(Protocol):
    def get_claim(self, trigger_key: str) -> FlowClaimRecord | None: ...

    def put_claim(self, claim: FlowClaimRecord) -> FlowClaimRecord: ...

    def get_active_hold_for_parent(
        self, *, parent_work_id: str
    ) -> ParentWorkHold | None: ...

    def list_active_holds(self) -> list[ParentWorkHold]: ...

    def put_hold(self, hold: ParentWorkHold) -> None: ...

    def get_budget(self, consumption_id: str) -> BudgetReservation | None: ...

    def put_budget(self, reservation: BudgetReservation) -> None: ...

    def next_resource_event_seq(
        self, *, resource_id: str, producer_epoch: str
    ) -> int: ...


@dataclass
class InMemoryFlowControlStore:
    """进程内 claim／hold／预算／事件序；联调与单测默认实现。"""

    _claims: dict[str, FlowClaimRecord] = field(default_factory=dict)
    _holds: dict[str, ParentWorkHold] = field(default_factory=dict)
    _active_parent: dict[str, str] = field(default_factory=dict)
    _budgets: dict[str, BudgetReservation] = field(default_factory=dict)
    _event_seq: dict[tuple[str, str], int] = field(default_factory=dict)
    _lock: RLock = field(default_factory=RLock)

    def get_claim(self, trigger_key: str) -> FlowClaimRecord | None:
        with self._lock:
            return self._claims.get(trigger_key)

    def put_claim(self, claim: FlowClaimRecord) -> FlowClaimRecord:
        with self._lock:
            existing = self._claims.get(claim.trigger_key)
            if existing is not None:
                return existing
            self._claims[claim.trigger_key] = claim
            return claim

    def get_active_hold_for_parent(
        self, *, parent_work_id: str
    ) -> ParentWorkHold | None:
        with self._lock:
            hold_id = self._active_parent.get(parent_work_id)
            if hold_id is None:
                return None
            return self._holds.get(hold_id)

    def list_active_holds(self) -> list[ParentWorkHold]:
        with self._lock:
            holds: list[ParentWorkHold] = []
            for hold_id in self._active_parent.values():
                hold = self._holds.get(hold_id)
                if hold is not None and hold.status is ParentHoldStatus.ACTIVE:
                    holds.append(hold)
            return holds

    def put_hold(self, hold: ParentWorkHold) -> None:
        with self._lock:
            existing_id = self._active_parent.get(hold.parent_work_id)
            if hold.status is ParentHoldStatus.ACTIVE:
                try:
                    assert_single_active_parent_hold(
                        existing_active_hold_id=existing_id,
                        claimant_hold_id=hold.hold_id,
                    )
                except FlowFoundationContractError as exc:
                    raise FlowFoundationControlError(
                        str(exc),
                        error_code="PARENT_HOLD_CONFLICT",
                    ) from exc
                self._active_parent[hold.parent_work_id] = hold.hold_id
            elif existing_id == hold.hold_id:
                self._active_parent.pop(hold.parent_work_id, None)
            self._holds[hold.hold_id] = hold

    def get_budget(self, consumption_id: str) -> BudgetReservation | None:
        with self._lock:
            return self._budgets.get(consumption_id)

    def put_budget(self, reservation: BudgetReservation) -> None:
        with self._lock:
            existing = self._budgets.get(reservation.consumption_id)
            if existing is not None and existing != reservation:
                if existing.status is BudgetReservationStatus.OUTCOME_UNKNOWN:
                    # unknown 预留下禁止先释放再盲重试；可幂等保持 unknown／committed。
                    if reservation.status is BudgetReservationStatus.RELEASED:
                        raise FlowFoundationControlError(
                            "outcome_unknown 预算不得先退款再盲重试",
                            error_code="BUDGET_UNKNOWN_REFUND_FORBIDDEN",
                        )
                elif not _budget_transition_allowed(
                    existing.status, reservation.status
                ):
                    raise FlowFoundationControlError(
                        "预算预约状态冲突",
                        error_code="BUDGET_RESERVATION_CONFLICT",
                    )
            self._budgets[reservation.consumption_id] = reservation

    def next_resource_event_seq(
        self, *, resource_id: str, producer_epoch: str
    ) -> int:
        key = (resource_id, producer_epoch)
        with self._lock:
            current = self._event_seq.get(key, 0) + 1
            self._event_seq[key] = current
            return current


class FlowFoundationControlService:
    """App 组合根可注入的 P2／P3／P4 控制与证据服务。"""

    def __init__(self, *, store: FlowControlStore | None = None) -> None:
        self._store: FlowControlStore = store or InMemoryFlowControlStore()
        self._active_short_flow_depth = 0

    @property
    def store(self) -> FlowControlStore:
        """暴露控制仓供 ParentHoldGate 等应用门禁只读查询。"""

        return self._store

    def get_active_hold_for_parent(
        self, *, parent_work_id: str
    ) -> ParentWorkHold | None:
        return self._store.get_active_hold_for_parent(parent_work_id=parent_work_id)

    def evaluate_trigger_candidate(
        self,
        *,
        parts: TriggerKeyParts,
        trigger_state_payload: dict[str, object],
        observation_fresh: bool,
        subject_confirmed: bool,
        has_requirement_or_explore_goal: bool,
        capabilities_ready: bool,
        capabilities_partial_accepted: bool,
        permission_allowed: bool,
        budget_sufficient: bool,
        cancelled: bool = False,
        hard_deadline_passed: bool = False,
        missing_capabilities: tuple[str, ...] = (),
    ) -> TriggerDecision:
        try:
            assert_trigger_state_payload_allowed(trigger_state_payload)
            trigger_key = build_trigger_key(parts)
        except FlowFoundationContractError as exc:
            raise FlowFoundationControlError(
                str(exc),
                error_code="TRIGGER_KEY_REJECTED",
            ) from exc
        already = self._store.get_claim(trigger_key) is not None
        return evaluate_trigger(
            observation_fresh=observation_fresh,
            subject_confirmed=subject_confirmed,
            has_requirement_or_explore_goal=has_requirement_or_explore_goal,
            capabilities_ready=capabilities_ready,
            capabilities_partial_accepted=capabilities_partial_accepted,
            permission_allowed=permission_allowed,
            budget_sufficient=budget_sufficient,
            already_claimed=already,
            cancelled=cancelled,
            hard_deadline_passed=hard_deadline_passed,
            trigger_key=trigger_key,
            missing_capabilities=missing_capabilities,
        )

    def claim_flow(
        self,
        *,
        decision: TriggerDecision,
        phase_run_id: str,
        parent_work_kind: str,
        parent_work_id: str,
        parent_version: int,
        owner_execution_segment_id: str,
        budget_units: int,
        task_id: str,
        expected_parent_version: int,
        clear_data_return: bool = False,
    ) -> FlowClaimRecord:
        """原子语义：校验父版本→唯一键→预算→hold（T04）。竞争返回已有实例。"""

        try:
            assert_clear_data_not_ordinary_return(using_clear_data=clear_data_return)
        except FlowFoundationContractError as exc:
            raise FlowFoundationControlError(
                str(exc),
                error_code="FLOW_CLAIM_PRECONDITION",
            ) from exc
        if decision.kind.value != "eligible" or not decision.trigger_key:
            raise FlowFoundationControlError(
                "仅 eligible 决策可 claim",
                error_code="FLOW_CLAIM_NOT_ELIGIBLE",
            )
        if int(expected_parent_version) != int(parent_version):
            raise FlowFoundationControlError(
                "父待办版本不匹配，拒绝 claim",
                error_code="PARENT_VERSION_MISMATCH",
            )
        existing = self._store.get_claim(decision.trigger_key)
        if existing is not None:
            return existing
        # P2.7：禁止「同一父待办」上再嵌套短流程。
        # 跨后继 Work 串行时旧 hold 常未 release，不得用全局 depth 误杀主探索
        # （QC e7242d9e：home 误 claim 后后继 Work 报「短流程禁止递归」）。
        active_on_parent = self._store.get_active_hold_for_parent(
            parent_work_id=parent_work_id
        )
        if active_on_parent is not None:
            try:
                assert_short_flow_not_nested(active_short_flow_depth=1)
            except FlowFoundationContractError as exc:
                raise FlowFoundationControlError(
                    str(exc),
                    error_code="FLOW_CLAIM_PRECONDITION",
                ) from exc
        else:
            self._release_foreign_active_holds(except_parent_work_id=parent_work_id)
            self._active_short_flow_depth = 0

        consumption_id = f"budget:{decision.trigger_key}"
        hold_id = f"hold:{decision.trigger_key}"
        reservation = BudgetReservation(
            consumption_id=consumption_id,
            task_id=task_id,
            phase_run_id=phase_run_id,
            units=budget_units,
            status=BudgetReservationStatus.RESERVED,
        )
        self._store.put_budget(reservation)
        hold = ParentWorkHold(
            hold_id=hold_id,
            task_id=task_id,
            phase_run_id=phase_run_id,
            parent_work_kind=parent_work_kind,
            parent_work_id=parent_work_id,
            parent_version=parent_version,
            owner_execution_segment_id=owner_execution_segment_id,
            status=ParentHoldStatus.ACTIVE,
            frontier_ref=f"app-frontier:{parent_work_id}",
        )
        try:
            self._store.put_hold(hold)
        except FlowFoundationControlError:
            # hold 失败不留下「空预算」：标为 released 仅当尚未执行。
            self._store.put_budget(
                BudgetReservation(
                    consumption_id=consumption_id,
                    task_id=task_id,
                    phase_run_id=phase_run_id,
                    units=budget_units,
                    status=BudgetReservationStatus.RELEASED,
                )
            )
            raise
        claim = FlowClaimRecord(
            trigger_key=decision.trigger_key,
            phase_run_id=phase_run_id,
            consumption_id=consumption_id,
            hold_id=hold_id,
            parent_work_id=parent_work_id,
            parent_version=parent_version,
        )
        stored = self._store.put_claim(claim)
        if stored is claim:
            self._active_short_flow_depth = 1
        return stored

    def release_parent_hold(self, hold: ParentWorkHold) -> ParentWorkHold:
        released = ParentWorkHold(
            hold_id=hold.hold_id,
            task_id=hold.task_id,
            phase_run_id=hold.phase_run_id,
            parent_work_kind=hold.parent_work_kind,
            parent_work_id=hold.parent_work_id,
            parent_version=hold.parent_version,
            owner_execution_segment_id=hold.owner_execution_segment_id,
            status=ParentHoldStatus.RELEASED,
            frontier_ref=hold.frontier_ref,
            resume_snapshot_ref=hold.resume_snapshot_ref,
        )
        self._store.put_hold(released)
        self._active_short_flow_depth = 0
        return released

    def _release_foreign_active_holds(self, *, except_parent_work_id: str) -> None:
        """释放其它父待办上遗留的活动 hold，允许探索后继 Work 串行短流程。"""

        list_fn = getattr(self._store, "list_active_holds", None)
        if not callable(list_fn):
            self._active_short_flow_depth = 0
            return
        for hold in list(list_fn()):
            if str(hold.parent_work_id) == str(except_parent_work_id):
                continue
            if hold.status is ParentHoldStatus.ACTIVE:
                self.release_parent_hold(hold)

    def assess_resume(
        self,
        *,
        expected: ResumeEquivalenceFacts,
        observed: ResumeEquivalenceFacts,
    ) -> str:
        return assess_app_resume_equivalence(expected=expected, observed=observed)

    def settle_budget_unknown(
        self,
        reservation: BudgetReservation,
        *,
        outcome_unknown: bool,
        proved_not_consumed: bool,
    ) -> BudgetReservation:
        status = decide_budget_after_unknown(
            outcome_unknown=outcome_unknown,
            proved_not_consumed=proved_not_consumed,
        )
        updated = BudgetReservation(
            consumption_id=reservation.consumption_id,
            task_id=reservation.task_id,
            phase_run_id=reservation.phase_run_id,
            units=reservation.units,
            status=status,
        )
        self._store.put_budget(updated)
        return updated

    def settle_cancel_deadline(
        self,
        *,
        cancelled: bool,
        hard_deadline_passed: bool,
        resume_attempted: bool,
        resume_succeeded: bool,
        evidence_complete: bool,
    ) -> FlowTriStateOutcome:
        return settle_cancel_or_deadline_outcome(
            cancelled=cancelled,
            hard_deadline_passed=hard_deadline_passed,
            resume_attempted=resume_attempted,
            resume_succeeded=resume_succeeded,
            evidence_complete=evidence_complete,
        )

    def allocate_resource_event_seq(
        self,
        *,
        resource_id: str,
        producer_epoch: str,
        sample_index: int,
        clock_domain: ObservationClockDomain = ObservationClockDomain.PRODUCER_MONOTONIC,
    ) -> ResourceEventSeqAllocation:
        try:
            assert_observed_at_not_persisted_legacy(clock_domain=clock_domain)
        except FlowFoundationContractError as exc:
            raise FlowFoundationControlError(
                str(exc),
                error_code="CLOCK_DOMAIN_REJECTED",
            ) from exc
        seq = self._store.next_resource_event_seq(
            resource_id=resource_id,
            producer_epoch=producer_epoch,
        )
        return ResourceEventSeqAllocation(
            resource_id=resource_id,
            producer_epoch=producer_epoch,
            resource_event_seq=seq,
            sample_index=sample_index,
            clock_domain=clock_domain,
        )

    def gate_pilot(self, plan: PilotPlan) -> None:
        try:
            assert_pilot_plan_allowed(plan)
        except FlowFoundationContractError as exc:
            raise FlowFoundationControlError(
                str(exc),
                error_code="PILOT_PLAN_REJECTED",
            ) from exc

    def accept_pure_observation_pilot(
        self,
        plan: PilotPlan,
        *,
        triggered: bool,
        samples_recorded: bool,
        seal_or_gap_explained: bool,
        claimed_playback_pass: bool = False,
    ) -> PilotAcceptanceRecord:
        self.gate_pilot(plan)
        return evaluate_pure_observation_pilot(
            plan=plan,
            triggered=triggered,
            samples_recorded=samples_recorded,
            seal_or_gap_explained=seal_or_gap_explained,
            claimed_playback_pass=claimed_playback_pass,
        )

    def accept_low_risk_interact_return_pilot(
        self,
        plan: PilotPlan,
        *,
        action_ledger_present: bool,
        resume_verified: bool,
        tri_state: FlowTriStateOutcome,
        claimed_false_resume: bool = False,
    ) -> PilotAcceptanceRecord:
        if plan.scenario is not PilotScenario.LOW_RISK_INTERACT_RETURN:
            raise FlowFoundationControlError(
                "场景不匹配",
                error_code="PILOT_SCENARIO_MISMATCH",
            )
        self.gate_pilot(plan)
        return evaluate_low_risk_interact_return_pilot(
            plan=plan,
            action_ledger_present=action_ledger_present,
            resume_verified=resume_verified,
            tri_state=tri_state,
            claimed_false_resume=claimed_false_resume,
        )
