"""Orchestrator 到 Explorer Agent 的版本化工作合同。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.exploration.app_goal import AppExplorationGoal
from services.api.app.domain.exploration.snapshots import (
    ExplorationBoundaries,
    FrontierSnapshot,
)
from services.api.app.domain.inspection import (
    EvidenceRequest,
    EvidenceSourceGrade,
    RestorationPlan,
    grade_meets_minimum,
    grade_supports,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    require_text,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    parse_aware_datetime,
    read_known_fields,
    require_mapping,
    thaw_json_mapping,
)

EXPLORATION_WORK_PROTOCOL_VERSION = "exploration-work.v1"
PROBE_WORK_PROTOCOL_VERSION = "probe-work.v2"
EXPLORATION_TERMINAL_PROTOCOL_VERSION = "exploration-terminal.v1"


class ExplorationValidationError(ValueError):
    """探索工作缺少稳定身份、预算或可序列化状态。"""


@dataclass(frozen=True, kw_only=True)
class ExplorationBudget:
    """Orchestrator 锁定、Explorer 只能消费的探索预算。

    广告清障与业务探索分账：remaining_actions = remaining_business + remaining_obstruction。
    旧四栏 payload 缺省时 obstruction=0、全部归 business（行为与历史等价）。
    """

    max_actions: int
    remaining_actions: int
    max_depth: int
    remaining_duration_ms: int
    max_business_actions: int | None = None
    remaining_business_actions: int | None = None
    max_obstruction_actions: int | None = None
    remaining_obstruction_actions: int | None = None
    business_duration_floor_ms: int = 0
    business_duration_floor_applied: int = 0

    def __post_init__(self) -> None:
        for field_name in (
            "max_actions",
            "remaining_actions",
            "max_depth",
            "remaining_duration_ms",
            "business_duration_floor_ms",
            "business_duration_floor_applied",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ExplorationValidationError(f"{field_name} 必须为非负整数")
        if self.max_actions == 0:
            raise ExplorationValidationError("max_actions 必须大于 0")
        if self.remaining_actions > self.max_actions:
            raise ExplorationValidationError("remaining_actions 不能超过 max_actions")
        if self.business_duration_floor_applied not in {0, 1}:
            raise ExplorationValidationError(
                "business_duration_floor_applied 必须为 0 或 1"
            )

        max_business = self.max_business_actions
        rem_business = self.remaining_business_actions
        max_obstruction = self.max_obstruction_actions
        rem_obstruction = self.remaining_obstruction_actions
        if (
            max_business is None
            and rem_business is None
            and max_obstruction is None
            and rem_obstruction is None
        ):
            # 旧合同：无分账字段时全部视为业务池
            max_business = self.max_actions
            rem_business = self.remaining_actions
            max_obstruction = 0
            rem_obstruction = 0
            object.__setattr__(self, "max_business_actions", max_business)
            object.__setattr__(self, "remaining_business_actions", rem_business)
            object.__setattr__(self, "max_obstruction_actions", max_obstruction)
            object.__setattr__(self, "remaining_obstruction_actions", rem_obstruction)
        else:
            for field_name, value in (
                ("max_business_actions", max_business),
                ("remaining_business_actions", rem_business),
                ("max_obstruction_actions", max_obstruction),
                ("remaining_obstruction_actions", rem_obstruction),
            ):
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ExplorationValidationError(f"{field_name} 必须为非负整数")
            assert max_business is not None
            assert rem_business is not None
            assert max_obstruction is not None
            assert rem_obstruction is not None
            if max_business + max_obstruction != self.max_actions:
                raise ExplorationValidationError(
                    "max_business_actions + max_obstruction_actions 必须等于 max_actions"
                )
            if rem_business + rem_obstruction != self.remaining_actions:
                raise ExplorationValidationError(
                    "remaining_business_actions + remaining_obstruction_actions "
                    "必须等于 remaining_actions"
                )
            if rem_business > max_business:
                raise ExplorationValidationError(
                    "remaining_business_actions 不能超过 max_business_actions"
                )
            if rem_obstruction > max_obstruction:
                raise ExplorationValidationError(
                    "remaining_obstruction_actions 不能超过 max_obstruction_actions"
                )

    @property
    def resolved_max_business_actions(self) -> int:
        value = self.max_business_actions
        return self.max_actions if value is None else value

    @property
    def resolved_remaining_business_actions(self) -> int:
        value = self.remaining_business_actions
        return self.remaining_actions if value is None else value

    @property
    def resolved_max_obstruction_actions(self) -> int:
        value = self.max_obstruction_actions
        return 0 if value is None else value

    @property
    def resolved_remaining_obstruction_actions(self) -> int:
        value = self.remaining_obstruction_actions
        return 0 if value is None else value

    def to_dict(self) -> dict[str, int]:
        return {
            "max_actions": self.max_actions,
            "remaining_actions": self.remaining_actions,
            "max_depth": self.max_depth,
            "remaining_duration_ms": self.remaining_duration_ms,
            "max_business_actions": self.resolved_max_business_actions,
            "remaining_business_actions": self.resolved_remaining_business_actions,
            "max_obstruction_actions": self.resolved_max_obstruction_actions,
            "remaining_obstruction_actions": self.resolved_remaining_obstruction_actions,
            "business_duration_floor_ms": self.business_duration_floor_ms,
            "business_duration_floor_applied": self.business_duration_floor_applied,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExplorationBudget:
        values = _read_known(
            payload,
            known={
                "max_actions",
                "remaining_actions",
                "max_depth",
                "remaining_duration_ms",
                "max_business_actions",
                "remaining_business_actions",
                "max_obstruction_actions",
                "remaining_obstruction_actions",
                "business_duration_floor_ms",
                "business_duration_floor_applied",
            },
            required={
                "max_actions",
                "remaining_actions",
                "max_depth",
                "remaining_duration_ms",
            },
            field_name="ExplorationBudget",
        )
        return cls(**values)


def split_app_action_budget_pools(
    *,
    max_actions: int,
    required_action_count: int,
    duration_ms: int,
) -> tuple[int, int, int]:
    """按课纲 required 与清障预留拆业务／清障池，并计算业务时长地板。"""

    if (
        isinstance(max_actions, bool)
        or not isinstance(max_actions, int)
        or max_actions <= 0
    ):
        raise ExplorationValidationError("max_actions 必须为正整数")
    if (
        isinstance(required_action_count, bool)
        or not isinstance(required_action_count, int)
        or required_action_count < 0
    ):
        raise ExplorationValidationError("required_action_count 必须为非负整数")
    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
    ):
        raise ExplorationValidationError("duration_ms 必须为正整数")
    if required_action_count > max_actions:
        raise ExplorationValidationError("required_action_count 不能超过 max_actions")
    max_obstruction = min(
        max(20, max_actions // 4),
        max(0, max_actions - required_action_count),
    )
    max_business = max_actions - max_obstruction
    if required_action_count > max_business:
        raise ExplorationValidationError(
            "课纲必达动作超出业务动作预算（清障预留后）"
        )
    business_duration_floor_ms = max(
        duration_ms * max_business // max_actions,
        duration_ms // 2,
    )
    return max_business, max_obstruction, business_duration_floor_ms


def build_split_exploration_budget(
    *,
    max_actions: int,
    max_depth: int,
    duration_ms: int,
    required_action_count: int = 0,
) -> ExplorationBudget:
    """锁定首条 Work 时生成分账预算。"""

    max_business, max_obstruction, floor_ms = split_app_action_budget_pools(
        max_actions=max_actions,
        required_action_count=required_action_count,
        duration_ms=duration_ms,
    )
    return ExplorationBudget(
        max_actions=max_actions,
        remaining_actions=max_actions,
        max_depth=max_depth,
        remaining_duration_ms=duration_ms,
        max_business_actions=max_business,
        remaining_business_actions=max_business,
        max_obstruction_actions=max_obstruction,
        remaining_obstruction_actions=max_obstruction,
        business_duration_floor_ms=floor_ms,
        business_duration_floor_applied=0,
    )


def consume_exploration_action_budget(
    budget: ExplorationBudget,
    *,
    counts_toward_business: bool,
    elapsed_duration_ms: int,
    apply_business_duration_floor: bool = False,
) -> ExplorationBudget:
    """按分类扣减业务／清障动作桶，并可选一次补足业务时长地板。

    旧四栏／max_obstruction=0 时清障动作仍扣业务桶，行为与历史单桶等价。
    """

    rem_business = budget.resolved_remaining_business_actions
    rem_obstruction = budget.resolved_remaining_obstruction_actions
    debit_obstruction = (
        not counts_toward_business
        and budget.resolved_max_obstruction_actions > 0
    )
    if debit_obstruction:
        rem_obstruction = max(rem_obstruction - 1, 0)
    else:
        rem_business = max(rem_business - 1, 0)
    remaining_duration = max(budget.remaining_duration_ms - elapsed_duration_ms, 0)
    floor_applied = budget.business_duration_floor_applied
    if (
        apply_business_duration_floor
        and floor_applied == 0
        and budget.business_duration_floor_ms > 0
    ):
        remaining_duration = max(remaining_duration, budget.business_duration_floor_ms)
        floor_applied = 1
    return ExplorationBudget(
        max_actions=budget.max_actions,
        remaining_actions=rem_business + rem_obstruction,
        max_depth=budget.max_depth,
        remaining_duration_ms=remaining_duration,
        max_business_actions=budget.resolved_max_business_actions,
        remaining_business_actions=rem_business,
        max_obstruction_actions=budget.resolved_max_obstruction_actions,
        remaining_obstruction_actions=rem_obstruction,
        business_duration_floor_ms=budget.business_duration_floor_ms,
        business_duration_floor_applied=floor_applied,
    )


def copy_exploration_budget_after_observation(
    budget: ExplorationBudget,
    *,
    elapsed_duration_ms: int,
) -> ExplorationBudget:
    """Observation 轮不扣动作数，只扣时长。"""

    return ExplorationBudget(
        max_actions=budget.max_actions,
        remaining_actions=budget.remaining_actions,
        max_depth=budget.max_depth,
        remaining_duration_ms=max(
            budget.remaining_duration_ms - elapsed_duration_ms,
            0,
        ),
        max_business_actions=budget.resolved_max_business_actions,
        remaining_business_actions=budget.resolved_remaining_business_actions,
        max_obstruction_actions=budget.resolved_max_obstruction_actions,
        remaining_obstruction_actions=budget.resolved_remaining_obstruction_actions,
        business_duration_floor_ms=budget.business_duration_floor_ms,
        business_duration_floor_applied=budget.business_duration_floor_applied,
    )


@dataclass(frozen=True, kw_only=True)
class ExplorationWork:
    """Explorer Agent 决定下一条探索路径时消费的只读快照。"""

    work_id: str
    task_id: str
    session_id: str
    branch_id: str
    objective: str
    objective_hash: str
    surface_kind: SurfaceKind
    source_capture_id: str
    frontier_snapshot_id: str
    frontier: FrontierSnapshot
    boundaries: ExplorationBoundaries
    budget: ExplorationBudget
    deadline: datetime
    app_goal: AppExplorationGoal | None = None
    protocol_version: str = EXPLORATION_WORK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "work_id",
            "task_id",
            "session_id",
            "branch_id",
            "objective",
            "objective_hash",
            "source_capture_id",
            "frontier_snapshot_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.protocol_version != EXPLORATION_WORK_PROTOCOL_VERSION:
            raise ExplorationValidationError("ExplorationWork protocol_version 不受支持")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise ExplorationValidationError("surface_kind 不受支持")
        if not isinstance(self.budget, ExplorationBudget):
            raise ExplorationValidationError("budget 必须为 ExplorationBudget")
        _require_aware_deadline(self.deadline)
        expected_objective_hash = _sha256_text(self.objective)
        if self.objective_hash != expected_objective_hash:
            raise ExplorationValidationError("objective_hash 与 objective 不一致")
        expected_work_id = _exploration_work_id(
            task_id=self.task_id,
            branch_id=self.branch_id,
            objective_hash=self.objective_hash,
        )
        if self.work_id != expected_work_id:
            raise ExplorationValidationError("work_id 与任务、分支、目标哈希不一致")
        if not isinstance(self.frontier, FrontierSnapshot):
            raise ExplorationValidationError("frontier 必须为 FrontierSnapshot")
        if self.frontier.snapshot_id != self.frontier_snapshot_id:
            raise ExplorationValidationError("frontier_snapshot_id 与 FrontierSnapshot 不一致")
        if self.frontier.observation.capture_id != self.source_capture_id:
            raise ExplorationValidationError("Frontier 当前 Capture 与 source_capture_id 不一致")
        if self.frontier.observation.surface_kind is not self.surface_kind:
            raise ExplorationValidationError("Frontier Surface 与 Exploration Work 不一致")
        if not isinstance(self.boundaries, ExplorationBoundaries):
            raise ExplorationValidationError("boundaries 必须为 ExplorationBoundaries")
        if self.budget.max_depth > self.boundaries.max_navigation_depth:
            raise ExplorationValidationError("budget.max_depth 不能超过探索边界")
        if self.app_goal is not None:
            if self.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
                raise ExplorationValidationError("只有 App Exploration Work 可以携带 app_goal")
            if not isinstance(self.app_goal, AppExplorationGoal):
                raise ExplorationValidationError("app_goal 必须为 AppExplorationGoal")

    def to_dict(self) -> dict[str, Any]:
        result = {
            "protocol_version": self.protocol_version,
            "work_id": self.work_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "branch_id": self.branch_id,
            "objective": self.objective,
            "objective_hash": self.objective_hash,
            "surface_kind": self.surface_kind.value,
            "source_capture_id": self.source_capture_id,
            "frontier_snapshot_id": self.frontier_snapshot_id,
            "frontier": self.frontier.to_dict(),
            "boundaries": self.boundaries.to_dict(),
            "budget": self.budget.to_dict(),
            "deadline": self.deadline.isoformat(),
        }
        if self.app_goal is not None:
            result["app_goal"] = self.app_goal.to_dict()
        return result

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ExplorationWork:
        values = _read_known(
            payload,
            known={
                "protocol_version",
                "work_id",
                "task_id",
                "session_id",
                "branch_id",
                "objective",
                "objective_hash",
                "surface_kind",
                "source_capture_id",
                "frontier_snapshot_id",
                "frontier",
                "boundaries",
                "budget",
                "deadline",
                "app_goal",
            },
            required={
                "protocol_version",
                "work_id",
                "task_id",
                "session_id",
                "branch_id",
                "objective",
                "objective_hash",
                "surface_kind",
                "source_capture_id",
                "frontier_snapshot_id",
                "frontier",
                "boundaries",
                "budget",
                "deadline",
            },
            field_name="ExplorationWork",
        )
        try:
            surface_kind = SurfaceKind(values["surface_kind"])
            deadline = parse_aware_datetime(values["deadline"], "deadline")
            frontier_payload = require_mapping(values["frontier"], "frontier")
            boundary_payload = require_mapping(values["boundaries"], "boundaries")
            budget_payload = require_mapping(values["budget"], "budget")
        except (ProtocolContractError, ValueError) as exc:
            raise ExplorationValidationError(str(exc)) from exc
        return cls(
            work_id=values["work_id"],
            task_id=values["task_id"],
            session_id=values["session_id"],
            branch_id=values["branch_id"],
            objective=values["objective"],
            objective_hash=values["objective_hash"],
            surface_kind=surface_kind,
            source_capture_id=values["source_capture_id"],
            frontier_snapshot_id=values["frontier_snapshot_id"],
            frontier=FrontierSnapshot.from_dict(frontier_payload),
            boundaries=ExplorationBoundaries.from_dict(boundary_payload),
            budget=ExplorationBudget.from_dict(budget_payload),
            deadline=deadline,
            app_goal=(
                AppExplorationGoal.from_dict(
                    require_mapping(values["app_goal"], "app_goal")
                )
                if values.get("app_goal") is not None
                else None
            ),
            protocol_version=values["protocol_version"],
        )


def build_exploration_work(
    *,
    task_id: str,
    session_id: str,
    branch_id: str,
    objective: str,
    surface_kind: SurfaceKind,
    source_capture_id: str,
    frontier_snapshot_id: str,
    frontier: FrontierSnapshot,
    boundaries: ExplorationBoundaries,
    budget: ExplorationBudget,
    deadline: datetime,
    app_goal: AppExplorationGoal | None = None,
) -> ExplorationWork:
    """使用 task + branch + objective hash 生成稳定 Exploration Work。"""
    normalized_objective = _require_text(objective, "objective")
    objective_hash = _sha256_text(normalized_objective)
    return ExplorationWork(
        work_id=_exploration_work_id(
            task_id=_require_text(task_id, "task_id"),
            branch_id=_require_text(branch_id, "branch_id"),
            objective_hash=objective_hash,
        ),
        task_id=task_id,
        session_id=session_id,
        branch_id=branch_id,
        objective=normalized_objective,
        objective_hash=objective_hash,
        surface_kind=surface_kind,
        source_capture_id=source_capture_id,
        frontier_snapshot_id=frontier_snapshot_id,
        frontier=frontier,
        boundaries=boundaries,
        budget=budget,
        deadline=deadline,
        app_goal=app_goal,
    )


@dataclass(frozen=True, kw_only=True)
class ProbeWork:
    """Evidence Request 被确定性路由后交给 Explorer Agent 的补采工作。"""

    work_id: str
    task_id: str
    session_id: str
    branch_id: str
    surface_kind: SurfaceKind
    source_capture_id: str
    restoration_plan_hash: str
    restoration_attempt_id: str
    equivalence_assessment_id: str
    source_grade: EvidenceSourceGrade
    evidence_request: EvidenceRequest
    budget: ExplorationBudget
    deadline: datetime
    restoration_plan: RestorationPlan | None = None
    protocol_version: str = PROBE_WORK_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "work_id",
            "task_id",
            "session_id",
            "branch_id",
            "source_capture_id",
            "restoration_plan_hash",
            "restoration_attempt_id",
            "equivalence_assessment_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        if self.protocol_version != PROBE_WORK_PROTOCOL_VERSION:
            raise ExplorationValidationError("ProbeWork protocol_version 不受支持")
        if not isinstance(self.evidence_request, EvidenceRequest):
            raise ExplorationValidationError("evidence_request 类型不正确")
        if not isinstance(self.surface_kind, SurfaceKind):
            raise ExplorationValidationError("surface_kind 不受支持")
        if not isinstance(self.source_grade, EvidenceSourceGrade):
            raise ExplorationValidationError("source_grade 不受支持")
        if self.evidence_request.task_id != self.task_id:
            raise ExplorationValidationError("Evidence Request 与 ProbeWork 任务不一致")
        if self.restoration_plan is not None:
            if not isinstance(self.restoration_plan, RestorationPlan):
                raise ExplorationValidationError("restoration_plan 类型不正确")
            if self.restoration_plan.plan_hash != self.restoration_plan_hash:
                raise ExplorationValidationError("ProbeWork 恢复计划哈希不一致")
            if (
                self.restoration_plan.request_id != self.evidence_request.request_id
                or self.restoration_plan.task_id != self.task_id
                or self.restoration_plan.original_capture_id
                != self.evidence_request.target_capture_id
                or self.restoration_plan.target_session_id != self.session_id
                or self.restoration_plan.surface_kind is not self.surface_kind
                or self.restoration_plan.deadline != self.deadline
            ):
                raise ExplorationValidationError("ProbeWork 恢复计划与工作上下文不一致")
        if not grade_meets_minimum(
            self.source_grade,
            self.evidence_request.minimum_source_grade,
        ) or not grade_supports(
            self.source_grade,
            self.evidence_request.required_equivalence,
        ):
            raise ExplorationValidationError("ProbeWork 来源等级不满足 Evidence Request")
        if not isinstance(self.budget, ExplorationBudget):
            raise ExplorationValidationError("budget 必须为 ExplorationBudget")
        _require_aware_deadline(self.deadline)
        expected = _probe_work_id(
            request_id=self.evidence_request.request_id,
            restoration_plan_hash=self.restoration_plan_hash,
            restoration_attempt_id=self.restoration_attempt_id,
            equivalence_assessment_id=self.equivalence_assessment_id,
            source_capture_id=self.source_capture_id,
        )
        if self.work_id != expected:
            raise ExplorationValidationError("ProbeWork work_id 与输入身份不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "work_id": self.work_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "branch_id": self.branch_id,
            "surface_kind": self.surface_kind.value,
            "source_capture_id": self.source_capture_id,
            "restoration_plan_hash": self.restoration_plan_hash,
            "restoration_attempt_id": self.restoration_attempt_id,
            "equivalence_assessment_id": self.equivalence_assessment_id,
            "source_grade": self.source_grade.value,
            "evidence_request": self.evidence_request.to_dict(),
            "budget": self.budget.to_dict(),
            "deadline": self.deadline.isoformat(),
            "restoration_plan": (
                self.restoration_plan.to_dict() if self.restoration_plan is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProbeWork:
        values = _read_known(
            payload,
            known={
                "protocol_version",
                "work_id",
                "task_id",
                "session_id",
                "branch_id",
                "surface_kind",
                "source_capture_id",
                "restoration_plan_hash",
                "restoration_attempt_id",
                "equivalence_assessment_id",
                "source_grade",
                "evidence_request",
                "budget",
                "deadline",
                "restoration_plan",
            },
            required={
                "protocol_version",
                "work_id",
                "task_id",
                "session_id",
                "branch_id",
                "surface_kind",
                "source_capture_id",
                "restoration_plan_hash",
                "restoration_attempt_id",
                "equivalence_assessment_id",
                "source_grade",
                "evidence_request",
                "budget",
                "deadline",
            },
            field_name="ProbeWork",
        )
        try:
            request_payload = require_mapping(values["evidence_request"], "evidence_request")
            budget_payload = require_mapping(values["budget"], "budget")
            plan_payload = values.get("restoration_plan")
            deadline = parse_aware_datetime(values["deadline"], "deadline")
            surface_kind = SurfaceKind(values["surface_kind"])
            source_grade = EvidenceSourceGrade(values["source_grade"])
        except (ProtocolContractError, ValueError) as exc:
            raise ExplorationValidationError(str(exc)) from exc
        return cls(
            work_id=values["work_id"],
            task_id=values["task_id"],
            session_id=values["session_id"],
            branch_id=values["branch_id"],
            surface_kind=surface_kind,
            source_capture_id=values["source_capture_id"],
            restoration_plan_hash=values["restoration_plan_hash"],
            restoration_attempt_id=values["restoration_attempt_id"],
            equivalence_assessment_id=values["equivalence_assessment_id"],
            source_grade=source_grade,
            evidence_request=EvidenceRequest.from_dict(request_payload),
            budget=ExplorationBudget.from_dict(budget_payload),
            deadline=deadline,
            restoration_plan=(
                RestorationPlan.from_dict(
                    require_mapping(plan_payload, "restoration_plan")
                )
                if plan_payload is not None
                else None
            ),
            protocol_version=values["protocol_version"],
        )


def build_probe_work(
    *,
    session_id: str,
    branch_id: str,
    surface_kind: SurfaceKind,
    source_capture_id: str,
    restoration_plan_hash: str,
    restoration_attempt_id: str,
    equivalence_assessment_id: str,
    source_grade: EvidenceSourceGrade,
    evidence_request: EvidenceRequest,
    budget: ExplorationBudget,
    deadline: datetime,
    restoration_plan: RestorationPlan | None = None,
) -> ProbeWork:
    """把已持久化 Evidence Request 转换为可幂等发布的 Probe Work。"""
    if not isinstance(evidence_request, EvidenceRequest):
        raise ExplorationValidationError("evidence_request 类型不正确")
    return ProbeWork(
        work_id=_probe_work_id(
            request_id=evidence_request.request_id,
            restoration_plan_hash=_require_text(
                restoration_plan_hash,
                "restoration_plan_hash",
            ),
            restoration_attempt_id=_require_text(
                restoration_attempt_id,
                "restoration_attempt_id",
            ),
            equivalence_assessment_id=_require_text(
                equivalence_assessment_id,
                "equivalence_assessment_id",
            ),
            source_capture_id=_require_text(source_capture_id, "source_capture_id"),
        ),
        task_id=evidence_request.task_id,
        session_id=session_id,
        branch_id=branch_id,
        surface_kind=surface_kind,
        source_capture_id=source_capture_id,
        restoration_plan_hash=restoration_plan_hash,
        restoration_attempt_id=restoration_attempt_id,
        equivalence_assessment_id=equivalence_assessment_id,
        source_grade=source_grade,
        evidence_request=evidence_request,
        budget=budget,
        deadline=deadline,
        restoration_plan=restoration_plan,
    )


class ExplorationTerminalStatus(StrEnum):
    """Explorer Agent 不再输出动作时的显式终态。"""

    COMPLETED = "completed"
    BOUNDED = "bounded"
    BUDGET_EXHAUSTED = "budget_exhausted"
    BLOCKED = "blocked"
    NO_PROGRESS = "no_progress"


@dataclass(frozen=True, kw_only=True)
class ExplorationTerminal:
    """Explorer Agent 的无动作终态，不等同于任务整体完成。"""

    work_id: str
    task_id: str
    status: ExplorationTerminalStatus
    reason: str
    outcome: str | None = None
    reason_code: str | None = None
    coverage_summary: Mapping[str, Any] = field(default_factory=dict)
    protocol_version: str = EXPLORATION_TERMINAL_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_text(self.work_id, "work_id")
        _require_text(self.task_id, "task_id")
        _require_text(self.reason, "reason")
        if self.outcome is not None:
            _require_text(self.outcome, "outcome")
        if self.reason_code is not None:
            _require_text(self.reason_code, "reason_code")
        if (self.outcome is None) != (self.reason_code is None):
            raise ExplorationValidationError("outcome 与 reason_code 必须同时提供")
        try:
            coverage_summary = freeze_json_mapping(
                self.coverage_summary,
                "coverage_summary",
            )
        except ProtocolContractError as exc:
            raise ExplorationValidationError(str(exc)) from exc
        if not isinstance(self.status, ExplorationTerminalStatus):
            raise ExplorationValidationError("status 不受支持")
        if self.protocol_version != EXPLORATION_TERMINAL_PROTOCOL_VERSION:
            raise ExplorationValidationError("ExplorationTerminal protocol_version 不受支持")
        object.__setattr__(self, "coverage_summary", coverage_summary)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "work_id": self.work_id,
            "task_id": self.task_id,
            "status": self.status.value,
            "reason": self.reason,
            "outcome": self.outcome,
            "reason_code": self.reason_code,
            "coverage_summary": thaw_json_mapping(self.coverage_summary),
        }


type ExplorerWork = ExplorationWork | ProbeWork


def _exploration_work_id(*, task_id: str, branch_id: str, objective_hash: str) -> str:
    return f"exploration-work:{_stable_hash([task_id, branch_id, objective_hash])}"


def _probe_work_id(
    *,
    request_id: str,
    restoration_plan_hash: str,
    restoration_attempt_id: str,
    equivalence_assessment_id: str,
    source_capture_id: str,
) -> str:
    return "probe-work:" + _stable_hash(
        [
            request_id,
            restoration_plan_hash,
            restoration_attempt_id,
            equivalence_assessment_id,
            source_capture_id,
        ]
    )


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except InteractionValidationError as exc:
        raise ExplorationValidationError(str(exc)) from exc


def _require_aware_deadline(value: Any) -> None:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ExplorationValidationError("deadline 必须是带时区的 datetime")


def _read_known(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str],
    field_name: str,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(required),
            field_name=field_name,
        )
    except ProtocolContractError as exc:
        raise ExplorationValidationError(str(exc)) from exc
