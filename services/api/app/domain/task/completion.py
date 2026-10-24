"""HTML 架构定义的任务完成门禁、Finalization Epoch 与报告绑定合同。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from services.api.app.domain.protocol import (
    optional_text,
    require_aware_datetime,
    require_text,
    stable_protocol_hash,
)

COMPLETION_GATE_PROTOCOL_VERSION = "task-completion-gate.v1"
TASK_FINALIZATION_PROTOCOL_VERSION = "task-finalization.v1"
REPORT_GATE_BINDING_PROTOCOL_VERSION = "report-gate-binding.v1"


class TaskFinalizationState(StrEnum):
    """任务级收口状态；它不是 Agent，也不承载业务质检结论。"""

    OPEN = "open"
    EXPLORATION_DRAINING = "exploration_draining"
    SESSION_DRAINING = "session_draining"
    FINALIZING = "finalizing"
    SEALED = "sealed"


class TaskExecutionStatus(StrEnum):
    """任务是否完整执行，与规则是否通过严格分离。"""

    COMPLETED = "completed"
    COMPLETED_WITH_GAPS = "completed_with_gaps"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"
    CANCELED = "canceled"


class TaskQualityOutcome(StrEnum):
    """规则质检结果维度，不可替代任务执行状态。"""

    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class CompletionBlockReason(StrEnum):
    """与 HTML 中八项完成条件一一对应的阻断原因。"""

    EXPLORATION_NOT_TERMINAL = "exploration_not_terminal"
    PENDING_ACTION = "pending_action"
    PENDING_CAPTURE_COMMIT = "pending_capture_commit"
    NONTERMINAL_ENFORCED_WORK_ITEM = "nonterminal_enforced_work_item"
    ACTIVE_SESSION_REQUEST = "active_session_request"
    CANDIDATE_RULE_COVERAGE_NONTERMINAL = "candidate_rule_coverage_nonterminal"
    BACKGROUND_EXECUTION = "background_execution"
    REVIEW_POLICY_UNSATISFIED = "review_policy_unsatisfied"
    APP_CANDIDATE_INVENTORY_EMPTY = "app_candidate_inventory_empty"
    APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE = "app_coverage_or_visual_quality_incomplete"
    H5_COVERAGE_SCOPE_INCOMPLETE = "h5_coverage_scope_incomplete"
    JOURNEY_RESULTS_INCOMPLETE = "journey_results_incomplete"
    WEB_COVERAGE_SNAPSHOT_INCOMPLETE = "web_coverage_snapshot_incomplete"
    # 专项七 §23：自然结论消费了被遮挡底页或诊断材料，不能算正式通过。
    NATURAL_EVIDENCE_PRESENTATION_VIOLATION = "natural_evidence_presentation_violation"


@dataclass(frozen=True, kw_only=True)
class CompletionGateFacts:
    """一次事务内重算得到的完整门禁事实，不接受调用方拼布尔值。"""

    exploration_terminal: bool
    pending_action_count: int
    pending_capture_commit_count: int
    nonterminal_enforced_work_item_count: int
    active_session_request_count: int
    candidate_rule_count: int
    terminal_candidate_rule_count: int
    background_execution_count: int
    review_policy_satisfied: bool
    open_review_count: int = 0
    review_generation: int = 0
    review_policy_decision_id: str | None = None
    require_nonempty_candidate_inventory: bool = False
    no_rules_applicable: bool = False
    app_coverage_satisfied: bool = True
    h5_coverage_satisfied: bool = True
    h5_coverage_snapshot_id: str | None = None
    h5_coverage_snapshot_hash: str | None = None
    h5_coverage_snapshot_epoch: int | None = None
    web_coverage_required: bool = False
    web_coverage_satisfied: bool = True
    web_coverage_snapshot_id: str | None = None
    web_coverage_snapshot_hash: str | None = None
    web_coverage_snapshot_epoch: int | None = None
    web_coverage_status: str | None = None
    # pass/fail/warning 结论中消费了被遮挡底页或诊断材料的条数；0 时不进入哈希材料。
    natural_evidence_presentation_violation_count: int = 0
    journey_result_gap_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.exploration_terminal, bool):
            raise ValueError("exploration_terminal 必须为布尔值")
        if not isinstance(self.review_policy_satisfied, bool):
            raise ValueError("review_policy_satisfied 必须为布尔值")
        if not isinstance(self.require_nonempty_candidate_inventory, bool):
            raise ValueError("require_nonempty_candidate_inventory 必须为布尔值")
        if not isinstance(self.no_rules_applicable, bool):
            raise ValueError("no_rules_applicable 必须为布尔值")
        if not isinstance(self.app_coverage_satisfied, bool):
            raise ValueError("app_coverage_satisfied 必须为布尔值")
        if not isinstance(self.h5_coverage_satisfied, bool):
            raise ValueError("h5_coverage_satisfied 必须为布尔值")
        if not isinstance(self.web_coverage_required, bool):
            raise ValueError("web_coverage_required 必须为布尔值")
        if not isinstance(self.web_coverage_satisfied, bool):
            raise ValueError("web_coverage_satisfied 必须为布尔值")
        for field_name in (
            "pending_action_count",
            "pending_capture_commit_count",
            "nonterminal_enforced_work_item_count",
            "active_session_request_count",
            "candidate_rule_count",
            "terminal_candidate_rule_count",
            "background_execution_count",
            "open_review_count",
            "review_generation",
            "natural_evidence_presentation_violation_count",
            "journey_result_gap_count",
        ):
            _require_nonnegative_int(getattr(self, field_name), field_name)
        if self.terminal_candidate_rule_count > self.candidate_rule_count:
            raise ValueError("terminal_candidate_rule_count 不能超过 candidate_rule_count")
        h5_identity = (
            self.h5_coverage_snapshot_id,
            self.h5_coverage_snapshot_hash,
            self.h5_coverage_snapshot_epoch,
        )
        if any(value is not None for value in h5_identity):
            if any(value is None for value in h5_identity):
                raise ValueError("H5 Coverage 必须绑定完整快照身份")
            require_text(self.h5_coverage_snapshot_id, "h5_coverage_snapshot_id")
            require_text(self.h5_coverage_snapshot_hash, "h5_coverage_snapshot_hash")
            _require_nonnegative_int(self.h5_coverage_snapshot_epoch, "h5_coverage_snapshot_epoch")
        optional_text(self.review_policy_decision_id, "review_policy_decision_id")
        optional_text(self.web_coverage_snapshot_id, "web_coverage_snapshot_id")
        optional_text(self.web_coverage_snapshot_hash, "web_coverage_snapshot_hash")
        optional_text(self.web_coverage_status, "web_coverage_status")
        if self.web_coverage_snapshot_epoch is not None:
            _require_nonnegative_int(
                self.web_coverage_snapshot_epoch,
                "web_coverage_snapshot_epoch",
            )
        if self.web_coverage_satisfied and self.web_coverage_required:
            if (
                self.web_coverage_snapshot_id is None
                or self.web_coverage_snapshot_hash is None
                or self.web_coverage_snapshot_epoch is None
                or self.web_coverage_status not in {"complete", "completed_with_gaps"}
            ):
                raise ValueError("Web Coverage 放行事实必须绑定完整快照身份")

    @property
    def no_pending_action(self) -> bool:
        return self.pending_action_count == 0

    @property
    def no_pending_capture_commit(self) -> bool:
        return self.pending_capture_commit_count == 0

    @property
    def no_nonterminal_enforced_work_item(self) -> bool:
        return self.nonterminal_enforced_work_item_count == 0

    @property
    def no_active_session_request(self) -> bool:
        return self.active_session_request_count == 0

    @property
    def candidate_rule_coverage_terminal(self) -> bool:
        return self.terminal_candidate_rule_count == self.candidate_rule_count

    @property
    def candidate_inventory_satisfied(self) -> bool:
        """App 禁止零规则空集默过；H5 保持既有空 inventory 语义。"""
        if not self.require_nonempty_candidate_inventory:
            return True
        if self.candidate_rule_count > 0:
            return True
        return self.no_rules_applicable

    @property
    def no_background_execution(self) -> bool:
        return self.background_execution_count == 0

    @property
    def no_natural_evidence_presentation_violation(self) -> bool:
        return self.natural_evidence_presentation_violation_count == 0

    @property
    def gate_passed(self) -> bool:
        """严格实现 HTML 的八项合取公式，并叠加 App 库存门禁。"""
        return all(
            (
                self.exploration_terminal,
                self.no_pending_action,
                self.no_pending_capture_commit,
                self.no_nonterminal_enforced_work_item,
                self.no_active_session_request,
                self.candidate_rule_coverage_terminal,
                self.no_background_execution,
                self.review_policy_satisfied,
                self.candidate_inventory_satisfied,
                self.app_coverage_satisfied,
                self.h5_coverage_satisfied,
                (not self.web_coverage_required or self.web_coverage_satisfied),
                self.no_natural_evidence_presentation_violation,
                self.journey_result_gap_count == 0,
            )
        )

    @property
    def block_reasons(self) -> tuple[CompletionBlockReason, ...]:
        reasons: list[CompletionBlockReason] = []
        checks = (
            (self.journey_result_gap_count == 0, CompletionBlockReason.JOURNEY_RESULTS_INCOMPLETE),
            (self.exploration_terminal, CompletionBlockReason.EXPLORATION_NOT_TERMINAL),
            (self.no_pending_action, CompletionBlockReason.PENDING_ACTION),
            (
                self.no_pending_capture_commit,
                CompletionBlockReason.PENDING_CAPTURE_COMMIT,
            ),
            (
                self.no_nonterminal_enforced_work_item,
                CompletionBlockReason.NONTERMINAL_ENFORCED_WORK_ITEM,
            ),
            (
                self.no_active_session_request,
                CompletionBlockReason.ACTIVE_SESSION_REQUEST,
            ),
            (
                self.candidate_rule_coverage_terminal,
                CompletionBlockReason.CANDIDATE_RULE_COVERAGE_NONTERMINAL,
            ),
            (
                self.no_background_execution,
                CompletionBlockReason.BACKGROUND_EXECUTION,
            ),
            (
                self.review_policy_satisfied,
                CompletionBlockReason.REVIEW_POLICY_UNSATISFIED,
            ),
            (
                self.candidate_inventory_satisfied,
                CompletionBlockReason.APP_CANDIDATE_INVENTORY_EMPTY,
            ),
            (
                self.app_coverage_satisfied,
                CompletionBlockReason.APP_COVERAGE_OR_VISUAL_QUALITY_INCOMPLETE,
            ),
            (
                self.h5_coverage_satisfied,
                CompletionBlockReason.H5_COVERAGE_SCOPE_INCOMPLETE,
            ),
            (
                not self.web_coverage_required or self.web_coverage_satisfied,
                CompletionBlockReason.WEB_COVERAGE_SNAPSHOT_INCOMPLETE,
            ),
            (
                self.no_natural_evidence_presentation_violation,
                CompletionBlockReason.NATURAL_EVIDENCE_PRESENTATION_VIOLATION,
            ),
        )
        for satisfied, reason in checks:
            if not satisfied:
                reasons.append(reason)
        return tuple(reasons)

    def to_dict(self) -> dict[str, object]:
        facts: dict[str, object] = {
            "exploration_terminal": self.exploration_terminal,
            "no_pending_action": self.no_pending_action,
            "no_pending_capture_commit": self.no_pending_capture_commit,
            "no_nonterminal_enforced_work_item": self.no_nonterminal_enforced_work_item,
            "no_active_session_request": self.no_active_session_request,
            "candidate_rule_coverage_terminal": self.candidate_rule_coverage_terminal,
            "candidate_inventory_satisfied": self.candidate_inventory_satisfied,
            "no_background_execution": self.no_background_execution,
            "review_policy_satisfied": self.review_policy_satisfied,
            "pending_action_count": self.pending_action_count,
            "pending_capture_commit_count": self.pending_capture_commit_count,
            "nonterminal_enforced_work_item_count": (self.nonterminal_enforced_work_item_count),
            "active_session_request_count": self.active_session_request_count,
            "candidate_rule_count": self.candidate_rule_count,
            "terminal_candidate_rule_count": self.terminal_candidate_rule_count,
            "background_execution_count": self.background_execution_count,
            "open_review_count": self.open_review_count,
            "review_generation": self.review_generation,
            "review_policy_decision_id": self.review_policy_decision_id,
            "require_nonempty_candidate_inventory": (self.require_nonempty_candidate_inventory),
            "no_rules_applicable": self.no_rules_applicable,
            "app_coverage_satisfied": self.app_coverage_satisfied,
            "h5_coverage_satisfied": self.h5_coverage_satisfied,
        }
        # 新 H5 快照身份参与 Gate 哈希；历史和其他平台没有绑定时保持原哈希。
        if self.h5_coverage_snapshot_id is not None:
            facts.update(
                h5_coverage_snapshot_id=self.h5_coverage_snapshot_id,
                h5_coverage_snapshot_hash=self.h5_coverage_snapshot_hash,
                h5_coverage_snapshot_epoch=self.h5_coverage_snapshot_epoch,
            )
        # 未携带 Web 身份的调用方保持原有哈希材料。
        if (
            self.web_coverage_required
            or not self.web_coverage_satisfied
            or self.web_coverage_snapshot_id is not None
            or self.web_coverage_snapshot_hash is not None
            or self.web_coverage_snapshot_epoch is not None
            or self.web_coverage_status is not None
        ):
            facts.update(
                {
                    "web_coverage_required": self.web_coverage_required,
                    "web_coverage_satisfied": self.web_coverage_satisfied,
                    "web_coverage_snapshot_id": self.web_coverage_snapshot_id,
                    "web_coverage_snapshot_hash": self.web_coverage_snapshot_hash,
                    "web_coverage_snapshot_epoch": self.web_coverage_snapshot_epoch,
                    "web_coverage_status": self.web_coverage_status,
                }
            )
        # 只有真的出现违规时才进入哈希材料，历史快照身份保持不变。
        if self.natural_evidence_presentation_violation_count > 0:
            facts["natural_evidence_presentation_violation_count"] = (
                self.natural_evidence_presentation_violation_count
            )
        if self.journey_result_gap_count > 0:
            facts["journey_result_gap_count"] = self.journey_result_gap_count
        return facts


@dataclass(frozen=True, kw_only=True)
class CompletionGateSnapshot:
    """不可变门禁快照；报告只能绑定一份已放行快照。"""

    gate_snapshot_id: str
    task_id: str
    finalization_epoch: int
    evaluation_sequence: int
    facts: CompletionGateFacts
    facts_hash: str
    evaluated_at: datetime
    protocol_version: str = COMPLETION_GATE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("gate_snapshot_id", "task_id", "facts_hash"):
            require_text(getattr(self, field_name), field_name)
        _require_nonnegative_int(self.finalization_epoch, "finalization_epoch")
        _require_positive_int(self.evaluation_sequence, "evaluation_sequence")
        if not isinstance(self.facts, CompletionGateFacts):
            raise ValueError("facts 必须为 CompletionGateFacts")
        if self.facts_hash != stable_protocol_hash(self.facts.to_dict()):
            raise ValueError("facts_hash 与门禁事实不一致")
        require_aware_datetime(self.evaluated_at, "evaluated_at")
        if self.protocol_version != COMPLETION_GATE_PROTOCOL_VERSION:
            raise ValueError("Completion Gate protocol_version 不受支持")
        expected_id = _gate_snapshot_id(
            task_id=self.task_id,
            finalization_epoch=self.finalization_epoch,
            evaluation_sequence=self.evaluation_sequence,
            facts_hash=self.facts_hash,
        )
        if self.gate_snapshot_id != expected_id:
            raise ValueError("gate_snapshot_id 与任务、epoch、序号和事实不一致")

    @property
    def gate_passed(self) -> bool:
        return self.facts.gate_passed

    @property
    def block_reasons(self) -> tuple[CompletionBlockReason, ...]:
        return self.facts.block_reasons


@dataclass(frozen=True, kw_only=True)
class TaskFinalizationSnapshot:
    """任务收口状态与乐观锁快照。"""

    task_id: str
    state: TaskFinalizationState
    finalization_epoch: int
    row_version: int
    agent_run_id: str | None = None
    gate_snapshot_id: str | None = None
    execution_status: TaskExecutionStatus | None = None
    quality_outcome: TaskQualityOutcome | None = None
    protocol_version: str = TASK_FINALIZATION_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        require_text(self.task_id, "task_id")
        if not isinstance(self.state, TaskFinalizationState):
            raise ValueError("state 不受支持")
        _require_nonnegative_int(self.finalization_epoch, "finalization_epoch")
        _require_nonnegative_int(self.row_version, "row_version")
        optional_text(self.agent_run_id, "agent_run_id")
        optional_text(self.gate_snapshot_id, "gate_snapshot_id")
        if self.execution_status is not None and not isinstance(
            self.execution_status,
            TaskExecutionStatus,
        ):
            raise ValueError("execution_status 不受支持")
        if self.quality_outcome is not None and not isinstance(
            self.quality_outcome,
            TaskQualityOutcome,
        ):
            raise ValueError("quality_outcome 不受支持")
        sealed = self.state is TaskFinalizationState.SEALED
        if sealed != (
            self.gate_snapshot_id is not None
            and self.execution_status is not None
            and self.quality_outcome is not None
        ):
            raise ValueError("只有 sealed 状态必须同时携带门禁快照、执行状态和质量结论")
        if self.protocol_version != TASK_FINALIZATION_PROTOCOL_VERSION:
            raise ValueError("Task Finalization protocol_version 不受支持")
        if not sealed and (self.execution_status is not None or self.quality_outcome is not None):
            raise ValueError("非 sealed 状态不能提前携带执行状态或质量结论")


@dataclass(frozen=True, kw_only=True)
class CompletionGateDecision:
    """门禁评估结果及评估后的任务收口状态。"""

    snapshot: CompletionGateSnapshot
    finalization: TaskFinalizationSnapshot

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, CompletionGateSnapshot):
            raise ValueError("snapshot 类型不正确")
        if not isinstance(self.finalization, TaskFinalizationSnapshot):
            raise ValueError("finalization 类型不正确")
        if self.snapshot.task_id != self.finalization.task_id:
            raise ValueError("门禁快照与任务收口快照不属于同一任务")
        if self.snapshot.finalization_epoch != self.finalization.finalization_epoch:
            raise ValueError("门禁快照与任务收口快照 epoch 不一致")
        if self.snapshot.gate_passed != (self.finalization.state is TaskFinalizationState.SEALED):
            raise ValueError("门禁结论与任务 sealed 状态不一致")


@dataclass(frozen=True, kw_only=True)
class ReportGateBinding:
    """报告版本与放行门禁快照之间的不可变绑定。"""

    report_id: str
    report_version: int
    report_payload_hash: str
    task_id: str
    gate_snapshot_id: str
    finalization_epoch: int
    execution_status: TaskExecutionStatus
    quality_outcome: TaskQualityOutcome
    bound_at: datetime
    protocol_version: str = REPORT_GATE_BINDING_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "report_id",
            "report_payload_hash",
            "task_id",
            "gate_snapshot_id",
        ):
            require_text(getattr(self, field_name), field_name)
        _require_positive_int(self.report_version, "report_version")
        _require_nonnegative_int(self.finalization_epoch, "finalization_epoch")
        if not isinstance(self.execution_status, TaskExecutionStatus):
            raise ValueError("execution_status 不受支持")
        if not isinstance(self.quality_outcome, TaskQualityOutcome):
            raise ValueError("quality_outcome 不受支持")
        require_aware_datetime(self.bound_at, "bound_at")
        if self.protocol_version != REPORT_GATE_BINDING_PROTOCOL_VERSION:
            raise ValueError("Report Gate Binding protocol_version 不受支持")


def build_completion_gate_snapshot(
    *,
    task_id: str,
    finalization_epoch: int,
    evaluation_sequence: int,
    facts: CompletionGateFacts,
    evaluated_at: datetime,
) -> CompletionGateSnapshot:
    """从事务内事实构造可复算身份的不可变门禁快照。"""
    if not isinstance(facts, CompletionGateFacts):
        raise TypeError("facts 必须为 CompletionGateFacts")
    task = require_text(task_id, "task_id")
    _require_nonnegative_int(finalization_epoch, "finalization_epoch")
    _require_positive_int(evaluation_sequence, "evaluation_sequence")
    facts_hash = stable_protocol_hash(facts.to_dict())
    return CompletionGateSnapshot(
        gate_snapshot_id=_gate_snapshot_id(
            task_id=task,
            finalization_epoch=finalization_epoch,
            evaluation_sequence=evaluation_sequence,
            facts_hash=facts_hash,
        ),
        task_id=task,
        finalization_epoch=finalization_epoch,
        evaluation_sequence=evaluation_sequence,
        facts=facts,
        facts_hash=facts_hash,
        evaluated_at=evaluated_at,
    )


def _gate_snapshot_id(
    *,
    task_id: str,
    finalization_epoch: int,
    evaluation_sequence: int,
    facts_hash: str,
) -> str:
    digest = stable_protocol_hash(
        {
            "task_id": task_id,
            "finalization_epoch": finalization_epoch,
            "evaluation_sequence": evaluation_sequence,
            "facts_hash": facts_hash,
        }
    )
    return f"gate_{digest[:48]}"


def _require_nonnegative_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} 必须为非负整数")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    result = _require_nonnegative_int(value, field_name)
    if result == 0:
        raise ValueError(f"{field_name} 必须为正整数")
    return result
