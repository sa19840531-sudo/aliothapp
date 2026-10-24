"""App 探索覆盖、循环与预算的确定性终止评估。"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from services.api.app.domain.exploration.app_goal import (
    AppExplorationCoverageSnapshot,
)
from services.api.app.domain.exploration.contracts import (
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
)
from services.api.app.domain.interaction import InteractionOperation, SurfaceKind

_DYNAMIC_DISCOVERY = "dynamic_discovery"
_EXPLICIT_TARGETS = "explicit_targets"
_SUPPORTED_COMPLETION_MODES = frozenset({_DYNAMIC_DISCOVERY, _EXPLICIT_TARGETS})
_DYNAMIC_FRONTIER_PLATEAU_ACTION_LIMIT = 4
_DYNAMIC_REPEATED_BUSINESS_PAGE_ACTION_LIMIT = 3
_EXTERNAL_RETURN_FAILURE_LIMIT = 2
_EXTERNAL_ACTIVITY_BOUNCE_LIMIT = 3
_EXTERNAL_ACTIVITY_BOUNCE_COUNT = "external_activity_bounce_count"
_EXTERNAL_RETURN_FAILURE_COUNT = "external_return_failure_count"


class AppExplorationOutcome(StrEnum):
    """App 探索子流程的业务结果与独立技术失败分类。"""

    EXPLORATION_COMPLETED = "exploration_completed"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    TECHNICAL_FAILED = "technical_failed"


@dataclass(frozen=True, kw_only=True)
class AppExplorationCoverage:
    """终止判断使用的覆盖投影，同时保留旧 Work 的状态计数。"""

    stable_page_reached: bool
    baseline_evidence_complete: bool
    unique_state_count: int
    minimum_unique_state_count: int
    eligible_candidate_count: int
    repeated_state_count: int
    snapshot: AppExplorationCoverageSnapshot | None = None
    completion_mode: str = _EXPLICIT_TARGETS
    pending_facts_available: bool = True
    runtime_policy_summary: Mapping[str, object] = field(default_factory=dict)
    curriculum_progress: Mapping[str, object] = field(default_factory=dict)
    flow_contract: Mapping[str, object] = field(default_factory=dict)
    budget_exhaustion_reason: str | None = None
    requires_real_visual: bool = False
    degraded_provider_facts: tuple[Mapping[str, object], ...] = ()
    dynamic_no_new_business_page_action_count: int = 0
    dynamic_repeated_business_page_action_count: int = 0
    external_activity_bounce_count: int = 0
    external_return_failure_count: int = 0
    discovered_page_type_candidates: tuple[Mapping[str, object], ...] = ()
    directed_discovery_attempts: tuple[Mapping[str, object], ...] = ()
    covered_business_pages: tuple[Mapping[str, object], ...] = ()
    rule_inventory_coverage: Mapping[str, object] = field(default_factory=dict)

    @property
    def rule_coverage_ready(self) -> bool:
        """新正式任务按冻结规则需求收口；旧探索 Work 不强加质检规则义务。"""
        if not self.rule_inventory_coverage:
            return True
        pending = self.rule_inventory_coverage.get("pending_rule_count")
        return _is_non_negative_int(pending) and pending == 0

    def __post_init__(self) -> None:
        if self.completion_mode not in _SUPPORTED_COMPLETION_MODES:
            raise ValueError("completion_mode 不受支持")
        if self.dynamic_no_new_business_page_action_count < 0:
            raise ValueError("dynamic_no_new_business_page_action_count 不能为负数")
        if self.dynamic_repeated_business_page_action_count < 0:
            raise ValueError("dynamic_repeated_business_page_action_count 不能为负数")
        if self.external_activity_bounce_count < 0:
            raise ValueError("external_activity_bounce_count 不能为负数")
        if self.external_return_failure_count < 0:
            raise ValueError("external_return_failure_count 不能为负数")

    @property
    def assessment_scope_complete(self) -> bool:
        """动态模式必须证明页型适用性闭合；硬页型 unresolved 时不得完成。"""

        if self.completion_mode != _DYNAMIC_DISCOVERY:
            return True
        if self.snapshot is None:
            return False
        applicability = _dynamic_page_type_applicability(self)
        return applicability.get("assessment_scope_complete") is True

    @property
    def dynamic_page_coverage_complete(self) -> bool:
        """动态模式下，已确认适用的每个页型都必须有覆盖证据。"""

        if self.completion_mode != _DYNAMIC_DISCOVERY:
            return True
        if self.snapshot is None:
            return False
        applicability = _dynamic_page_type_applicability(self)
        return bool(
            applicability.get("assessment_scope_complete") is True
            and not applicability.get("known_uncovered_page_types")
        )

    @property
    def coverage_complete(self) -> bool:
        """综合最低覆盖与动态页型覆盖，形成终态和审计共用的完成判定。"""

        return self.minimum_coverage_reached and self.dynamic_page_coverage_complete

    @property
    def minimum_coverage_reached(self) -> bool:
        if self.snapshot is not None:
            common_complete = (
                self.pending_facts_available
                and self.stable_page_reached
                and self.baseline_evidence_complete
                and self.covered_business_page_count >= self.minimum_business_pages
                and self.curriculum_complete
                and self.visual_quality_satisfied
                and self.flow_contract_complete
            )
            if self.completion_mode == _DYNAMIC_DISCOVERY:
                # 动态发现保留完整目录用于报告，但目录中尚未发现的页型不构成必达缺口。
                return (
                    common_complete and not self.missing_milestones and not self.blocked_milestones
                )
            return (
                common_complete
                and not self.missing_page_types
                and not self.blocked_page_types
                and not self.missing_milestones
                and not self.blocked_milestones
            )
        return self.unique_state_count >= self.minimum_unique_state_count

    @property
    def curriculum_complete(self) -> bool:
        """旧 Work 无课纲元数据时保持兼容；新 App Work 必须显式完成。"""

        if not self.curriculum_progress:
            return True
        return _curriculum_progress_complete(self.curriculum_progress)

    @property
    def flow_contract_complete(self) -> bool:
        """开屏／弹层／首页／底栏硬流程；无契约元数据时兼容旧任务。"""

        from services.api.app.application.app_exploration_flow_contract import (
            flow_contract_complete,
        )

        return flow_contract_complete(self.flow_contract)

    @property
    def visual_quality_satisfied(self) -> bool:
        return not self.requires_real_visual or not self.degraded_provider_facts

    @property
    def dynamic_frontier_plateau_reached(self) -> bool:
        """连续分支未产生新页型的诊断信号；不得据此丢弃尚未探索的分支。"""

        return (
            self.completion_mode == _DYNAMIC_DISCOVERY
            and self.minimum_coverage_reached
            and self.dynamic_no_new_business_page_action_count
            >= _DYNAMIC_FRONTIER_PLATEAU_ACTION_LIMIT
        )

    @property
    def repeated_business_page_plateau_reached(self) -> bool:
        """连续动作重复既有业务截图时让出当前分支，不代表全局分支耗尽。"""

        return (
            self.completion_mode == _DYNAMIC_DISCOVERY
            and self.dynamic_repeated_business_page_action_count
            >= _DYNAMIC_REPEATED_BUSINESS_PAGE_ACTION_LIMIT
        )

    @property
    def frontier_exhausted(self) -> bool:
        local_exhausted = (
            self.snapshot.frontier_exhausted
            if self.snapshot is not None
            else self.eligible_candidate_count == 0
        )
        global_frontier = self.runtime_policy_summary.get("app_global_frontier")
        if not isinstance(global_frontier, Mapping):
            return local_exhausted
        return local_exhausted and global_frontier.get("exhausted") is True

    @property
    def goal_driven(self) -> bool:
        return self.snapshot is not None

    @property
    def required_page_types(self) -> tuple[str, ...]:
        return self.snapshot.required_page_types if self.snapshot is not None else ()

    @property
    def covered_page_types(self) -> tuple[str, ...]:
        return self.snapshot.covered_page_types if self.snapshot is not None else ()

    @property
    def missing_page_types(self) -> tuple[str, ...]:
        return self.snapshot.missing_page_types if self.snapshot is not None else ()

    @property
    def blocked_page_types(self) -> tuple[str, ...]:
        return self.snapshot.blocked_page_types if self.snapshot is not None else ()

    @property
    def required_milestones(self) -> tuple[str, ...]:
        return self.snapshot.required_milestones if self.snapshot is not None else ()

    @property
    def covered_milestones(self) -> tuple[str, ...]:
        return self.snapshot.covered_milestones if self.snapshot is not None else ()

    @property
    def missing_milestones(self) -> tuple[str, ...]:
        return self.snapshot.missing_milestones if self.snapshot is not None else ()

    @property
    def blocked_milestones(self) -> tuple[str, ...]:
        return self.snapshot.blocked_milestones if self.snapshot is not None else ()

    @property
    def minimum_business_pages(self) -> int:
        return self.snapshot.minimum_business_pages if self.snapshot is not None else 0

    @property
    def covered_business_page_count(self) -> int:
        return self.snapshot.covered_business_page_count if self.snapshot is not None else 0

    @property
    def pending_action_count(self) -> int:
        return self.snapshot.pending_action_count if self.snapshot is not None else 0

    @property
    def pending_evidence_count(self) -> int:
        return self.snapshot.pending_evidence_count if self.snapshot is not None else 0

    def to_dict(self) -> dict[str, object]:
        """生成可直接写入 AppExplorationResult 的稳定审计摘要。"""
        result = (
            self.snapshot.to_dict()
            if self.snapshot is not None
            else {
                "stable_page_reached": self.stable_page_reached,
                "baseline_evidence_complete": self.baseline_evidence_complete,
                "eligible_candidate_count": self.eligible_candidate_count,
                "frontier_exhausted": self.frontier_exhausted,
            }
        )
        summary = {
            **result,
            "goal_driven": self.goal_driven,
            "completion_mode": self.completion_mode,
            "pending_facts_available": self.pending_facts_available,
            "unique_state_count": self.unique_state_count,
            "minimum_unique_state_count": self.minimum_unique_state_count,
            "repeated_state_count": self.repeated_state_count,
            "curriculum_complete": self.curriculum_complete,
            "flow_contract_complete": self.flow_contract_complete,
            "budget_exhaustion_reason": self.budget_exhaustion_reason,
            "coverage_complete": self.coverage_complete,
            "assessment_scope_complete": self.assessment_scope_complete,
            "requires_real_visual": self.requires_real_visual,
            "visual_provider_status": (
                "degraded"
                if self.degraded_provider_facts
                else ("healthy" if self.requires_real_visual else "not_required")
            ),
            "visual_quality_satisfied": self.visual_quality_satisfied,
            "degraded_provider_facts": [dict(item) for item in self.degraded_provider_facts],
            "dynamic_no_new_business_page_action_count": (
                self.dynamic_no_new_business_page_action_count
            ),
            "dynamic_frontier_plateau_reached": self.dynamic_frontier_plateau_reached,
            "dynamic_repeated_business_page_action_count": (
                self.dynamic_repeated_business_page_action_count
            ),
            "repeated_business_page_plateau_reached": (self.repeated_business_page_plateau_reached),
            "external_activity_bounce_count": self.external_activity_bounce_count,
            "external_return_failure_count": self.external_return_failure_count,
            "covered_page_type_count": len(self.covered_page_types),
            "covered_business_pages": [dict(item) for item in self.covered_business_pages],
        }
        if self.runtime_policy_summary:
            summary["app_runtime_policy"] = dict(self.runtime_policy_summary)
        if self.curriculum_progress:
            summary["app_curriculum_progress"] = dict(self.curriculum_progress)
        if self.flow_contract:
            summary["app_flow_contract"] = dict(self.flow_contract)
        if self.rule_inventory_coverage:
            summary["app_rule_inventory_coverage"] = dict(self.rule_inventory_coverage)
        if self.completion_mode == _DYNAMIC_DISCOVERY and self.snapshot is not None:
            summary["page_type_applicability"] = _dynamic_page_type_applicability(self)
        return summary


@dataclass(frozen=True, kw_only=True)
class AppExplorationTerminalEvaluation:
    """确定性评估结果；无终态时 outcome 与 terminal 均为空。"""

    coverage: AppExplorationCoverage
    outcome: AppExplorationOutcome | None = None
    reason_code: str | None = None
    terminal: ExplorationTerminal | None = None

    def __post_init__(self) -> None:
        has_terminal = self.terminal is not None
        if has_terminal != (self.outcome is not None) or has_terminal != (
            self.reason_code is not None
        ):
            raise ValueError("终止评估的 outcome、reason_code 与 terminal 必须同时存在")


class AppExplorationTerminalEvaluator:
    """不信任模型终态，只根据已持久化 Frontier 与预算形成结果。"""

    def __init__(
        self,
        *,
        minimum_unique_states: int = 2,
        repeated_state_threshold: int = 3,
        business_stall_unique_states: int = 8,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if minimum_unique_states <= 0:
            raise ValueError("minimum_unique_states 必须为正整数")
        if repeated_state_threshold <= 0:
            raise ValueError("repeated_state_threshold 必须为正整数")
        if business_stall_unique_states <= 0:
            raise ValueError("business_stall_unique_states 必须为正整数")
        self._minimum_unique_states = minimum_unique_states
        self._repeated_state_threshold = repeated_state_threshold
        self._business_stall_unique_states = business_stall_unique_states
        self._now = now or (lambda: datetime.now(UTC))

    def evaluate(
        self,
        work: ExplorationWork,
        *,
        eligible_candidate_count: int,
        proposed_terminal: ExplorationTerminal | None = None,
        technical_error_code: str | None = None,
    ) -> AppExplorationTerminalEvaluation:
        if not isinstance(work, ExplorationWork):
            raise TypeError("work 必须为 ExplorationWork")
        if work.surface_kind not in {SurfaceKind.ANDROID, SurfaceKind.IOS}:
            raise ValueError("AppExplorationTerminalEvaluator 只接受 App Work")
        if (
            isinstance(eligible_candidate_count, bool)
            or not isinstance(eligible_candidate_count, int)
            or eligible_candidate_count < 0
        ):
            raise ValueError("eligible_candidate_count 必须为非负整数")
        if proposed_terminal is not None and not isinstance(proposed_terminal, ExplorationTerminal):
            raise TypeError("proposed_terminal 必须为 ExplorationTerminal")
        if technical_error_code is not None and (
            not isinstance(technical_error_code, str)
            or not technical_error_code.strip()
            or technical_error_code != technical_error_code.strip()
        ):
            raise ValueError("technical_error_code 必须为无首尾空白的非空字符串")

        now = self._now()
        coverage = self._coverage(
            work,
            eligible_candidate_count=eligible_candidate_count,
            now=now,
        )
        if technical_error_code is not None:
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.TECHNICAL_FAILED,
                status=ExplorationTerminalStatus.BLOCKED,
                reason_code=technical_error_code,
                reason="App 探索发生独立技术失败。",
            )
        completion_ready = (
            coverage.stable_page_reached
            and coverage.baseline_evidence_complete
            and coverage.minimum_coverage_reached
            and coverage.assessment_scope_complete
            and coverage.dynamic_page_coverage_complete
            and coverage.rule_coverage_ready
        )
        dynamic_frontier_ready = (
            coverage.frontier_exhausted and _global_frontier_exhausted(coverage)
            if coverage.completion_mode == _DYNAMIC_DISCOVERY
            else True
        )
        if coverage.goal_driven and completion_ready and dynamic_frontier_ready:
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EXPLORATION_COMPLETED,
                status=ExplorationTerminalStatus.COMPLETED,
                reason_code="exploration_completed",
                reason="锁定页面目标、里程碑、基线证据和最低覆盖均已满足。",
            )
        if (
            coverage.completion_mode == _DYNAMIC_DISCOVERY
            and coverage.goal_driven
            and coverage.stable_page_reached
            and coverage.baseline_evidence_complete
            and coverage.minimum_coverage_reached
            and dynamic_frontier_ready
            and not coverage.assessment_scope_complete
        ):
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                reason_code="assessment_scope_incomplete",
                reason="动态页型适用性未闭合（硬页型仍 unresolved），禁止 exploration_completed。",
            )
        if coverage.external_return_failure_count >= _EXTERNAL_RETURN_FAILURE_LIMIT:
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                reason_code="app_external_return_failed",
                reason="外部 Activity 回源连续失败，无法安全返回目标 App。",
            )
        if coverage.external_activity_bounce_count >= _EXTERNAL_ACTIVITY_BOUNCE_LIMIT:
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                reason_code="app_external_restore_loop",
                reason="检测到外部 Activity 循环跳出，停止继续恢复以免空转。",
            )
        budget_exhausted = coverage.budget_exhaustion_reason is not None
        if (
            not budget_exhausted
            and not coverage.rule_coverage_ready
            and _non_negative_int(
                coverage.rule_inventory_coverage.get("pending_inspection_work_count")
            ) > 0
        ):
            # 正式 Judge 仍在推进时先等真实结论；等待不能延长原任务预算。
            return AppExplorationTerminalEvaluation(coverage=coverage)
        if not budget_exhausted and coverage.repeated_state_count >= self._repeated_state_threshold:
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                reason_code="loop_detected",
                reason="连续页面状态未变化，达到循环检测阈值。",
            )
        if not budget_exhausted and coverage.repeated_business_page_plateau_reached:
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                reason_code="app_repeated_business_page_plateau",
                reason="当前分支连续重复既有业务截图，交由全局 Frontier 恢复其他安全分支。",
            )
        if not budget_exhausted and _business_progress_stalled(
            coverage,
            work=work,
            unique_state_threshold=self._business_stall_unique_states,
        ):
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT,
                status=ExplorationTerminalStatus.NO_PROGRESS,
                reason_code="app_business_progress_stalled",
                reason=(
                    "已累计足够独特页面状态，但未覆盖任何业务页型，"
                    "且当前帧缺少受信 Accessibility 业务控件，停止空转探索。"
                ),
            )

        if (
            completion_ready
            and coverage.frontier_exhausted
            and dynamic_frontier_ready
            and not budget_exhausted
        ):
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EXPLORATION_COMPLETED,
                status=ExplorationTerminalStatus.COMPLETED,
                reason_code="exploration_completed",
                reason="稳定页面、基线证据和最低覆盖均已满足。",
            )
        # 启动／引导尚未进入业务页时，空候选不得判 no_progress；交回 Explorer 继续清障
        if (
            coverage.frontier_exhausted
            and not budget_exhausted
            and not coverage.covered_page_types
            and not _global_frontier_exhausted(coverage)
            and _still_in_startup_or_guide_gate(work)
        ):
            return AppExplorationTerminalEvaluation(coverage=coverage)
        # 弹窗流程未满足：即使全局 Frontier pending=0 也不得排空收口。
        if (
            coverage.frontier_exhausted
            and not budget_exhausted
            and _popup_phase_blocks_frontier_exhaustion(work)
            and work.budget.remaining_actions > 0
        ):
            return AppExplorationTerminalEvaluation(coverage=coverage)
        if budget_exhausted or coverage.frontier_exhausted:
            reason_code = (
                "rule_coverage_incomplete"
                if not coverage.rule_coverage_ready and not budget_exhausted
                else _insufficient_reason_code(coverage)
            )
            proposed_status = (
                proposed_terminal.status
                if proposed_terminal is not None
                and proposed_terminal.status
                in {
                    ExplorationTerminalStatus.BOUNDED,
                    ExplorationTerminalStatus.NO_PROGRESS,
                }
                else ExplorationTerminalStatus.NO_PROGRESS
            )
            return _terminal_evaluation(
                work=work,
                coverage=coverage,
                outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT,
                status=(
                    ExplorationTerminalStatus.BUDGET_EXHAUSTED
                    if budget_exhausted
                    else proposed_status
                ),
                reason_code=reason_code,
                reason=(
                    proposed_terminal.reason
                    if proposed_terminal is not None and not budget_exhausted
                    else "App 探索已停止，但尚未满足确定性完成条件。"
                ),
            )

        # 模型 TerminalIntent 仅作为输入；仍有候选且预算未耗尽时不形成终态。
        del proposed_terminal
        return AppExplorationTerminalEvaluation(coverage=coverage)

    def _coverage(
        self,
        work: ExplorationWork,
        *,
        eligible_candidate_count: int,
        now: datetime,
    ) -> AppExplorationCoverage:
        observation = work.frontier.observation
        unique_states = set(work.frontier.visited_state_ids)
        unique_states.add(observation.state_version)
        screenshot_artifact_id = observation.metadata.get("screenshot_artifact_id")
        goal = work.app_goal
        metadata = observation.metadata
        runtime_policy = _mapping(metadata.get("app_runtime_policy"))
        global_frontier = _mapping(metadata.get("app_global_frontier"))
        if global_frontier:
            runtime_policy = {**runtime_policy, "app_global_frontier": global_frontier}
        curriculum_progress = _mapping(metadata.get("app_curriculum_progress"))
        flow_contract = _mapping(metadata.get("app_flow_contract"))
        target_snapshot = _mapping(runtime_policy.get("app_target_snapshot"))
        completion_mode = _completion_mode(target_snapshot)
        requires_real_visual = target_snapshot.get("requires_real_visual") is True
        degraded_provider_facts = _mapping_sequence(metadata.get("degraded_provider_facts"))
        pending_facts_available = (
            metadata.get("pending_facts_available") is True
            and _is_non_negative_int(metadata.get("pending_action_count"))
            and _is_non_negative_int(metadata.get("pending_evidence_count"))
        )
        required_page_types = goal.required_page_types if goal is not None else ()
        required_milestones = goal.required_milestones if goal is not None else ()
        covered_business_page_count = _non_negative_int(metadata.get("covered_business_page_count"))
        persisted_milestones = _text_sequence(metadata.get("covered_milestones"))
        covered_page_types_snapshot = _text_sequence(metadata.get("covered_page_types"))
        _startup_gate_page_types = frozenset(
            {
                "startup_ad",
                "splash",
                "countdown",
                "line_loading",
                "guide_page",
                "payment",
                "startup_connection",
            }
        )
        has_stable_business_pages = covered_business_page_count > 0 and any(
            page_type.casefold() not in _startup_gate_page_types
            for page_type in covered_page_types_snapshot
        )
        stable_page_reached = observation.page_state == "stable" or (
            completion_mode == _DYNAMIC_DISCOVERY
            and covered_business_page_count > 0
            and "surface:stable_business_page" in persisted_milestones
        ) or has_stable_business_pages
        baseline_evidence_complete = isinstance(screenshot_artifact_id, str) and bool(
            screenshot_artifact_id.strip()
        )
        covered_page_types = _required_partition(
            required_page_types,
            _text_sequence(metadata.get("covered_page_types")),
        )
        blocked_page_types = _required_partition(
            required_page_types,
            _text_sequence(metadata.get("blocked_page_types")),
            excluded=covered_page_types,
        )
        covered_milestones = _required_partition(
            required_milestones,
            persisted_milestones,
        )
        if (
            completion_mode == _DYNAMIC_DISCOVERY
            and stable_page_reached
            and covered_business_page_count > 0
            and "surface:stable_business_page" in required_milestones
            and "surface:stable_business_page" not in covered_milestones
        ):
            covered_milestones = tuple((*covered_milestones, "surface:stable_business_page"))
        blocked_milestones = _required_partition(
            required_milestones,
            _text_sequence(metadata.get("blocked_milestones")),
            excluded=covered_milestones,
        )
        missing_page_types = _missing_partition(
            required_page_types,
            covered=covered_page_types,
            blocked=blocked_page_types,
        )
        missing_milestones = _missing_partition(
            required_milestones,
            covered=covered_milestones,
            blocked=blocked_milestones,
        )
        snapshot = (
            AppExplorationCoverageSnapshot(
                required_page_types=required_page_types,
                covered_page_types=covered_page_types,
                missing_page_types=missing_page_types,
                blocked_page_types=blocked_page_types,
                required_milestones=required_milestones,
                covered_milestones=covered_milestones,
                missing_milestones=missing_milestones,
                blocked_milestones=blocked_milestones,
                minimum_business_pages=goal.minimum_business_pages,
                covered_business_page_count=covered_business_page_count,
                eligible_candidate_count=eligible_candidate_count,
                pending_action_count=_non_negative_int(metadata.get("pending_action_count")),
                pending_evidence_count=_non_negative_int(metadata.get("pending_evidence_count")),
                frontier_exhausted=(
                    eligible_candidate_count == 0
                    and (not global_frontier or global_frontier.get("exhausted") is True)
                ),
                stable_page_reached=stable_page_reached,
                baseline_evidence_complete=baseline_evidence_complete,
            )
            if goal is not None
            else None
        )
        return AppExplorationCoverage(
            stable_page_reached=stable_page_reached,
            baseline_evidence_complete=baseline_evidence_complete,
            unique_state_count=len(unique_states),
            minimum_unique_state_count=self._minimum_unique_states,
            eligible_candidate_count=eligible_candidate_count,
            repeated_state_count=work.frontier.repeated_state_count,
            snapshot=snapshot,
            completion_mode=completion_mode,
            pending_facts_available=(pending_facts_available if goal is not None else True),
            runtime_policy_summary=runtime_policy,
            curriculum_progress=curriculum_progress,
            flow_contract=flow_contract,
            budget_exhaustion_reason=_budget_exhaustion_reason(
                work,
                now=now,
                eligible_candidate_count=eligible_candidate_count,
            ),
            requires_real_visual=requires_real_visual,
            degraded_provider_facts=degraded_provider_facts,
            dynamic_no_new_business_page_action_count=_non_negative_int(
                metadata.get("dynamic_no_new_business_page_action_count")
            ),
            dynamic_repeated_business_page_action_count=_non_negative_int(
                metadata.get("dynamic_repeated_business_page_action_count")
            ),
            external_activity_bounce_count=_non_negative_int(
                metadata.get(_EXTERNAL_ACTIVITY_BOUNCE_COUNT)
            ),
            external_return_failure_count=_non_negative_int(
                metadata.get(_EXTERNAL_RETURN_FAILURE_COUNT)
            ),
            discovered_page_type_candidates=_mapping_sequence(
                metadata.get("discovered_page_type_candidates")
            ),
            directed_discovery_attempts=_mapping_sequence(
                metadata.get("directed_discovery_attempts")
            ),
            covered_business_pages=_mapping_sequence(metadata.get("covered_business_pages")),
            rule_inventory_coverage=_mapping(metadata.get("app_rule_inventory_coverage")),
        )


def _dynamic_page_type_applicability(
    coverage: AppExplorationCoverage,
) -> dict[str, object]:
    """用本 Run 的观察、入口、定向发现与 Frontier 终态生成动态适用性分母。"""

    from services.api.app.application.app_directed_discovery import (
        OUTCOME_CAPABILITY_BLOCKED,
        OUTCOME_NO_ENTRY,
        has_directed_outcome,
    )

    catalog = coverage.required_page_types
    covered = coverage.covered_page_types
    covered_set = set(covered)
    attempts = coverage.directed_discovery_attempts
    blocked_from_meta = set(coverage.blocked_page_types)
    for page_type in catalog:
        if page_type in covered_set:
            continue
        if has_directed_outcome(
            attempts,
            page_type=page_type,
            outcomes={OUTCOME_CAPABILITY_BLOCKED},
        ):
            blocked_from_meta.add(page_type)
    blocked = tuple(
        page_type
        for page_type in catalog
        if page_type in blocked_from_meta - covered_set
    )
    candidate_evidence: list[dict[str, object]] = []
    discovered: set[str] = set()
    for raw in coverage.discovered_page_type_candidates:
        page_type = raw.get("page_type")
        if (
            not isinstance(page_type, str)
            or page_type not in catalog
            or page_type in covered_set
            or page_type in discovered
        ):
            continue
        discovered.add(page_type)
        candidate_evidence.append(dict(raw))
    # entry_seen 定向审计也视为可见入口
    for page_type in catalog:
        if page_type in covered_set or page_type in discovered:
            continue
        if has_directed_outcome(
            attempts,
            page_type=page_type,
            outcomes={"entry_seen"},
        ):
            discovered.add(page_type)
    not_explored = tuple(
        page_type for page_type in catalog if page_type in discovered and page_type not in blocked
    )
    known_applicable = tuple(
        page_type
        for page_type in catalog
        if page_type in covered_set or page_type in set(blocked) | set(not_explored)
    )
    scope_verified = bool(
        coverage.frontier_exhausted
        and coverage.minimum_coverage_reached
        and coverage.pending_facts_available
        and coverage.budget_exhaustion_reason is None
        and coverage.curriculum_complete
        and coverage.visual_quality_satisfied
    )
    unresolved_list: list[str] = [
        page_type for page_type in catalog if page_type not in set(known_applicable)
    ]
    not_applicable_list: list[str] = []
    not_observed_list: list[str] = []
    if scope_verified:
        for page_type in unresolved_list:
            if has_directed_outcome(
                attempts,
                page_type=page_type,
                outcomes={OUTCOME_NO_ENTRY},
            ):
                not_applicable_list.append(page_type)
            else:
                # 动态探索只闭合本轮已观察范围，不要求排除目录中的固定业务类型。
                # 未观察到不代表 App 不含该能力，单独保留，禁止写成全产品不存在。
                not_observed_list.append(page_type)
        unresolved_list = []
    not_applicable = tuple(not_applicable_list)
    unresolved = tuple(unresolved_list)
    assessment_complete = bool(scope_verified and not unresolved)
    coverage_rate = (
        round(len(covered) / len(known_applicable), 4)
        if assessment_complete and known_applicable
        else None
    )
    directed_evidence = [
        dict(item)
        for item in attempts
        if str(item.get("page_type") or "").casefold() in catalog
    ]
    return {
        "protocol_version": "app-page-type-applicability.v2",
        "authority_source": "run_observation_frontier_exhaustion_and_directed_discovery",
        "assessment_scope_kind": "observed_frontier",
        "assessment_scope_complete": assessment_complete,
        "assessment_scope_verified": scope_verified,
        "catalog_page_types": list(catalog),
        "covered_page_types": list(covered),
        "not_explored_page_types": list(not_explored),
        "known_uncovered_page_types": list((*blocked, *not_explored)),
        "not_applicable_page_types": list(not_applicable),
        "not_observed_page_types": not_observed_list,
        "unresolved_applicability_page_types": list(unresolved),
        "applicable_page_types": list(known_applicable),
        "covered_count": len(covered),
        "applicable_count": len(known_applicable) if assessment_complete else None,
        "coverage_rate": coverage_rate,
        "raw_catalog_coverage_rate": (round(len(covered) / len(catalog), 4) if catalog else None),
        "candidate_evidence": candidate_evidence,
        "directed_discovery_evidence": directed_evidence,
        "completion_evidence": {
            "frontier_exhausted": coverage.frontier_exhausted,
            "pending_facts_available": coverage.pending_facts_available,
            "budget_exhaustion_reason": coverage.budget_exhaustion_reason,
            "visual_quality_satisfied": coverage.visual_quality_satisfied,
        },
    }


def _insufficient_reason_code(coverage: AppExplorationCoverage) -> str:
    if not coverage.stable_page_reached:
        return "stable_page_not_reached"
    if not coverage.baseline_evidence_complete:
        return "baseline_evidence_missing"
    if coverage.goal_driven and not coverage.pending_facts_available:
        return "pending_facts_unavailable"
    if not coverage.curriculum_complete:
        return "curriculum_incomplete"
    if not coverage.flow_contract_complete:
        from services.api.app.application.app_exploration_flow_contract import (
            flow_contract_insufficient_reason,
        )

        return flow_contract_insufficient_reason(coverage.flow_contract) or (
            "flow_contract_incomplete"
        )
    if not coverage.visual_quality_satisfied:
        return "visual_provider_degraded"
    if not coverage.minimum_coverage_reached:
        return "minimum_coverage_not_met"
    if (
        coverage.completion_mode == _DYNAMIC_DISCOVERY
        and not coverage.dynamic_page_coverage_complete
    ):
        return "dynamic_page_coverage_incomplete"
    return "final_state_unconfirmed"


def _completion_mode(target_snapshot: Mapping[str, object]) -> str:
    mode = target_snapshot.get("completion_mode")
    return mode if mode in _SUPPORTED_COMPLETION_MODES else _EXPLICIT_TARGETS


def _global_frontier_exhausted(coverage: AppExplorationCoverage) -> bool:
    global_frontier = coverage.runtime_policy_summary.get("app_global_frontier")
    return isinstance(global_frontier, Mapping) and global_frontier.get("exhausted") is True


def _still_in_startup_or_guide_gate(work: ExplorationWork) -> bool:
    """启动广告／引导／线路页尚未进入业务页。"""

    page_type = str(work.frontier.observation.page_type or "").casefold()
    page_state = str(work.frontier.observation.page_state or "").casefold()
    return page_type in {
        "startup_ad",
        "line_loading",
        "guide_page",
        "splash",
        "countdown",
        "",
    } or page_state in {
        "startup_ad",
        "line_loading",
        "countdown",
        "splash",
    }


def _popup_phase_blocks_frontier_exhaustion(work: ExplorationWork) -> bool:
    """流程契约弹窗未清时禁止 Frontier 排空收口。"""

    from services.api.app.application.app_exploration_flow_contract import (
        popup_phase_blocks_frontier_exhaustion,
    )

    return popup_phase_blocks_frontier_exhaustion(work.frontier.observation.metadata)


def _business_progress_stalled(
    coverage: AppExplorationCoverage,
    *,
    work: ExplorationWork,
    unique_state_threshold: int,
) -> bool:
    """课纲 OBSERVE/SCROLL 可改画面，但无业务页与受信控件时必须提早 fail-closed。"""

    if not coverage.goal_driven and not coverage.curriculum_progress:
        return False
    if coverage.unique_state_count < unique_state_threshold:
        return False
    if coverage.covered_page_types:
        return False
    metadata = work.frontier.observation.metadata
    if _non_negative_int(metadata.get("covered_business_page_count")) > 0:
        return False
    # 启动／弹层清障与覆盖寻页仍在推进时，不算业务空转
    page_type = str(work.frontier.observation.page_type or "").casefold()
    page_state = str(work.frontier.observation.page_state or "").casefold()
    if page_type in {
        "startup_ad",
        "line_loading",
        "guide_page",
        "splash",
        "countdown",
    } or page_state in {
        "popup",
        "modal",
        "overlay",
        "dialog",
        "startup_ad",
        "loading",
        "line_loading",
    }:
        return False
    home_popup_frames = metadata.get("dynamic_home_popup_frame_count")
    if (
        isinstance(home_popup_frames, int)
        and not isinstance(home_popup_frames, bool)
        and home_popup_frames > 0
        and page_type in {"home", ""}
    ):
        return False
    for candidate in work.frontier.candidates:
        if candidate.metadata.get("coverage_page_seek") is True:
            return False
        if candidate.action_type in {
            InteractionOperation.CLICK,
            InteractionOperation.TAP,
        } and any(
            marker
            in " ".join(
                (
                    candidate.semantic_target,
                    str(candidate.metadata.get("purpose") or ""),
                    str(candidate.metadata.get("expected_state_hint") or ""),
                )
            ).casefold()
            for marker in ("关闭", "跳过", "下次", "稍后", "close", "dismiss", "skip")
        ):
            return False
    surface_facts = _mapping(metadata.get("surface_facts"))
    trusted = _mapping(surface_facts.get("trusted_accessibility_controls"))
    nav_count = trusted.get("bottom_navigation_count")
    if isinstance(nav_count, int) and not isinstance(nav_count, bool) and nav_count > 0:
        return False
    if trusted.get("overlay_close") is True:
        return False
    if _non_negative_int(trusted.get("content_card_count")) > 0:
        return False
    return True


def _text_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item == item.strip() and item not in result:
            result.append(item)
    return tuple(result)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): item for key, item in value.items()}


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(_mapping(item) for item in value if isinstance(item, Mapping))


def _curriculum_progress_complete(progress: Mapping[str, object]) -> bool:
    """只信任自洽的持久化课纲完成事实，损坏元数据一律按未完成处理。"""

    if progress.get("protocol_version") != "app-curriculum-progress.v1":
        return False
    phase_ids = _text_sequence(progress.get("phase_ids"))
    if not phase_ids:
        return False
    phase_cursor = progress.get("phase_cursor")
    if (
        isinstance(phase_cursor, bool)
        or not isinstance(phase_cursor, int)
        or not 0 <= phase_cursor <= len(phase_ids)
    ):
        return False
    completed = _text_sequence(progress.get("completed_phase_ids"))
    if completed != phase_ids[:phase_cursor]:
        return False
    requirements = progress.get("phase_action_requirements")
    if not isinstance(requirements, Mapping) or set(requirements) != set(phase_ids):
        return False
    required_counts = tuple(requirements.get(phase_id) for phase_id in phase_ids)
    if any(
        isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in required_counts
    ):
        return False
    current_count = progress.get("current_phase_action_count")
    if isinstance(current_count, bool) or not isinstance(current_count, int) or current_count < 0:
        return False
    expected_complete = phase_cursor == len(phase_ids)
    expected_current = None if expected_complete else phase_ids[phase_cursor]
    if progress.get("complete") is not expected_complete:
        return False
    if progress.get("current_phase_id") != expected_current:
        return False
    if progress.get("required_action_count") != sum(required_counts):
        return False
    return expected_complete and current_count == 0


def _budget_exhaustion_reason(
    work: ExplorationWork,
    *,
    now: datetime,
    eligible_candidate_count: int = 0,
) -> str | None:
    if now >= work.deadline:
        return "deadline_reached"
    # remaining_actions = business + obstruction；为 0 时两桶皆尽
    if work.budget.remaining_actions == 0:
        return "action_budget_exhausted"
    # 业务桶已尽、清障桶仍有额度，但当前无任何可执行候选（含无可清障）
    if (
        work.budget.resolved_remaining_business_actions == 0
        and eligible_candidate_count == 0
    ):
        return "business_action_budget_exhausted"
    if work.budget.remaining_duration_ms == 0:
        return "duration_budget_exhausted"
    return None


def _required_partition(
    required: tuple[str, ...],
    values: tuple[str, ...],
    *,
    excluded: tuple[str, ...] = (),
) -> tuple[str, ...]:
    allowed = set(values) - set(excluded)
    return tuple(item for item in required if item in allowed)


def _missing_partition(
    required: tuple[str, ...],
    *,
    covered: tuple[str, ...],
    blocked: tuple[str, ...],
) -> tuple[str, ...]:
    accounted = set(covered) | set(blocked)
    return tuple(item for item in required if item not in accounted)


def _non_negative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _terminal_evaluation(
    *,
    work: ExplorationWork,
    coverage: AppExplorationCoverage,
    outcome: AppExplorationOutcome,
    status: ExplorationTerminalStatus,
    reason_code: str,
    reason: str,
) -> AppExplorationTerminalEvaluation:
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=status,
        reason=reason,
        outcome=outcome.value,
        reason_code=reason_code,
        coverage_summary=coverage.to_dict(),
    )
    return AppExplorationTerminalEvaluation(
        coverage=coverage,
        outcome=outcome,
        reason_code=reason_code,
        terminal=terminal,
    )
