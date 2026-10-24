"""App Explorer 单轮 Worker 的领取、执行与状态写回测试。"""

from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application import app_exploration_lease as lease_module
from services.api.app.application.app_action_observation import build_app_action_observation
from services.api.app.application.app_exploration_lease import (
    AppExplorationLeaseGuard,
    AppExplorationLeaseLostError,
    AppFrontierLeaseGuard,
    AppFrontierLeaseLostError,
)
from services.api.app.application.app_exploration_turn import (
    AppExplorationTurnRunner,
    _deadline_headroom_terminal,
    _eligible_candidate_count,
    _evaluate_exhausted_frontier_terminal,
    _frontier_exhausted_terminal,
    _frontier_restoration_target_status,
    _needs_deadline_headroom_stop,
    _selected_candidate_for_decision,
)
from services.api.app.application.app_frontier_queue import (
    RenewAppFrontierLeaseCommand,
)
from services.api.app.application.app_frontier_restoration import AppFrontierDeferredError
from services.api.app.application.app_global_frontier import AppGlobalFrontierService
from services.api.app.application.app_metrics import (
    APP_FRONTIER_CLAIM_AGE_SECONDS,
    app_metric_observation,
)
from services.api.app.application.app_page_type_assessment_adapter import (
    AppPageTypeAssessmentAdapterResult,
)
from services.api.app.application.app_screenshot_exploration import (
    AppBeforeActionVisualUnderstandingError,
    AppScreenshotExplorationResult,
)
from services.api.app.application.capture_coordination import CaptureCoordinationResult
from services.api.app.application.capture_profiles import CaptureProfilePolicy
from services.api.app.application.exploration_work_queue import (
    CommittedExplorerWorkSuccessor,
    ExplorerWorkLease,
    ExplorerWorkStatus,
    ExplorerWorkType,
    RenewExplorerWorkLeaseCommand,
)
from services.api.app.application.explorer_turn import ExplorerTurnResult
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidenceDisabledError,
)
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    AppExplorationOutcome,
    AppFrontierCounts,
    AppFrontierLease,
    AppFrontierStatus,
    AppVisualAnalysis,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    ExplorationWork,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureKind,
    CaptureRef,
    InteractionExecutionOutcome,
    InteractionOperation,
    InteractionResult,
    InteractionResultStatus,
    InteractionRiskLevel,
    SurfaceKind,
    TimedObservationIntent,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.app_vision.llm import build_analysis_from_llm_payload
from services.api.tests.test_app_visual_page_identity import _observation as _visual_observation
from services.api.tests.test_llm_app_vision import _request as _vision_request


def _exhausted_frontier_counts() -> AppFrontierCounts:
    return AppFrontierCounts(
        pending=0,
        leased=0,
        running=0,
        retry_wait=0,
        visited=3,
        blocked=1,
        failed=0,
        cancelled=0,
        superseded=0,
    )


def test_global_frontier_exhaustion_does_not_infer_completion_from_legacy_flags() -> None:
    proposed = ExplorationTerminal(
        work_id="work-1",
        task_id="task-1",
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前页没有局部候选",
        coverage_summary={
            "stable_page_reached": True,
            "baseline_evidence_complete": True,
            "pending_facts_available": True,
            "curriculum_complete": True,
            "coverage_complete": True,
            "visual_quality_satisfied": True,
        },
    )

    terminal = _frontier_exhausted_terminal(
        proposed,
        counts=_exhausted_frontier_counts(),
    )

    assert terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert terminal.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
    assert terminal.reason_code == "app_global_frontier_exhausted_without_completion"
    assert terminal.coverage_summary["app_global_frontier"]["exhausted"] is True


def test_global_frontier_exhaustion_keeps_incomplete_coverage_fail_closed() -> None:
    proposed = ExplorationTerminal(
        work_id="work-1",
        task_id="task-1",
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前页没有局部候选",
        coverage_summary={
            "stable_page_reached": True,
            "baseline_evidence_complete": True,
            "pending_facts_available": True,
            "curriculum_complete": True,
            "coverage_complete": False,
            "visual_quality_satisfied": True,
            "missing_page_types": ["profile"],
        },
    )

    terminal = _frontier_exhausted_terminal(
        proposed,
        counts=_exhausted_frontier_counts(),
    )

    assert terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert terminal.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
    assert terminal.reason_code == "app_global_frontier_exhausted_without_completion"
    assert terminal.coverage_summary["missing_page_types"] == ("profile",)


def test_global_frontier_exhaustion_keeps_superseded_reason_code_for_diagnosis() -> None:
    """通用码会把所有 App 失败抹成一个样；被改写掉的细分原因必须留在摘要里。"""
    proposed = ExplorationTerminal(
        work_id="work-1",
        task_id="task-1",
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前页没有局部候选",
        coverage_summary={"stable_page_reached": False},
    )

    terminal = _frontier_exhausted_terminal(
        proposed,
        counts=_exhausted_frontier_counts(),
        superseded_reason_code="stable_page_not_reached",
    )

    assert terminal.reason_code == "app_global_frontier_exhausted_without_completion"
    assert terminal.coverage_summary["insufficient_reason_code"] == "stable_page_not_reached"


def test_global_frontier_exhaustion_omits_superseded_reason_code_when_absent() -> None:
    proposed = ExplorationTerminal(
        work_id="work-1",
        task_id="task-1",
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前页没有局部候选",
        coverage_summary={"stable_page_reached": False},
    )

    terminal = _frontier_exhausted_terminal(
        proposed,
        counts=_exhausted_frontier_counts(),
    )

    assert "insufficient_reason_code" not in terminal.coverage_summary


def test_global_frontier_exhaustion_rebuilds_goal_coverage_from_current_work() -> None:
    base = _work()
    metadata = {
        "covered_page_types": ["home"],
        "blocked_page_types": [],
        "covered_milestones": [],
        "blocked_milestones": [],
        "covered_business_page_count": 1,
        "pending_facts_available": True,
        "pending_action_count": 0,
        "pending_evidence_count": 0,
        "screenshot_artifact_id": "artifact-screenshot",
        "degraded_provider_facts": [],
    }
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile", "settings"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=3,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata=metadata,
            ),
        ),
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="模型认为当前页没有候选",
    )

    terminal = _evaluate_exhausted_frontier_terminal(
        proposed,
        work=work,
        counts=_exhausted_frontier_counts(),
    )

    assert terminal.reason_code == "app_global_frontier_exhausted_without_completion"
    assert terminal.coverage_summary["required_page_types"] == (
        "home",
        "profile",
        "settings",
    )
    assert terminal.coverage_summary["covered_page_types"] == ("home",)
    assert terminal.coverage_summary["missing_page_types"] == ("profile", "settings")
    assert terminal.coverage_summary["coverage_complete"] is False
    assert terminal.coverage_summary["app_global_frontier"]["exhausted"] is True


def test_dynamic_frontier_builds_evidence_driven_page_type_denominator() -> None:
    base = _work()
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile", "payment"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=(),
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    "covered_page_types": ["home"],
                    "blocked_page_types": [],
                    "covered_milestones": ["surface:stable_business_page"],
                    "blocked_milestones": [],
                    "covered_business_page_count": 1,
                    "pending_facts_available": True,
                    "pending_action_count": 0,
                    "pending_evidence_count": 0,
                    "screenshot_artifact_id": "artifact-screenshot",
                    "degraded_provider_facts": [],
                    "discovered_page_type_candidates": [
                        {
                            "page_type": "profile",
                            "source_capture_id": "capture-home",
                            "screenshot_artifact_id": "artifact-home",
                            "candidate_id": "candidate-profile",
                        }
                    ],
                    "app_runtime_policy": {
                        "app_target_snapshot": {"completion_mode": "dynamic_discovery"}
                    },
                },
            ),
        ),
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="动态 Frontier 已收敛",
    )

    terminal = _evaluate_exhausted_frontier_terminal(
        proposed,
        work=work,
        counts=_exhausted_frontier_counts(),
    )

    applicability = terminal.coverage_summary["page_type_applicability"]
    assert applicability["protocol_version"] == "app-page-type-applicability.v2"
    assert applicability["assessment_scope_verified"] is True
    assert applicability["covered_page_types"] == ("home",)
    assert applicability["not_explored_page_types"] == ("profile",)
    # 未见 payment 不代表产品不存在；已见但未完成的 profile 仍是本轮真实缺口。
    assert applicability["not_applicable_page_types"] == ()
    assert applicability["not_observed_page_types"] == ("payment",)
    assert applicability["unresolved_applicability_page_types"] == ()
    assert applicability["applicable_page_types"] == ("home", "profile")
    assert applicability["coverage_rate"] == 0.5
    assert applicability["assessment_scope_complete"] is True
    assert terminal.coverage_summary["coverage_complete"] is False
    assert terminal.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value


def test_hard_page_type_not_applicable_requires_directed_no_entry() -> None:
    base = _work()
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile", "payment"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=(),
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    "covered_page_types": ["home", "profile"],
                    "blocked_page_types": [],
                    "covered_milestones": ["surface:stable_business_page"],
                    "blocked_milestones": [],
                    "covered_business_page_count": 2,
                    "pending_facts_available": True,
                    "pending_action_count": 0,
                    "pending_evidence_count": 0,
                    "screenshot_artifact_id": "artifact-screenshot",
                    "degraded_provider_facts": [],
                    "discovered_page_type_candidates": [],
                    "directed_discovery_attempts": [
                        {
                            "protocol_version": "app-directed-discovery.v1",
                            "page_type": "payment",
                            "source_page_type": "profile",
                            "source_capture_id": "capture-profile",
                            "outcome": "no_entry",
                        }
                    ],
                    "app_runtime_policy": {
                        "app_target_snapshot": {"completion_mode": "dynamic_discovery"}
                    },
                },
            ),
        ),
    )
    terminal = _evaluate_exhausted_frontier_terminal(
        ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.NO_PROGRESS,
            reason="动态 Frontier 已收敛",
        ),
        work=work,
        counts=_exhausted_frontier_counts(),
    )
    applicability = terminal.coverage_summary["page_type_applicability"]
    assert applicability["not_applicable_page_types"] == ("payment",)
    assert applicability["unresolved_applicability_page_types"] == ()
    assert applicability["assessment_scope_complete"] is True
    assert applicability["coverage_rate"] == 1.0
    assert terminal.outcome == AppExplorationOutcome.EXPLORATION_COMPLETED.value
    assert terminal.status is ExplorationTerminalStatus.COMPLETED
    assert terminal.coverage_summary["coverage_complete"] is True


def test_hard_page_type_capability_blocked_enters_known_applicable() -> None:
    base = _work()
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "payment"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=(),
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    "covered_page_types": ["home"],
                    "blocked_page_types": ["payment"],
                    "covered_milestones": ["surface:stable_business_page"],
                    "blocked_milestones": [],
                    "covered_business_page_count": 1,
                    "pending_facts_available": True,
                    "pending_action_count": 0,
                    "pending_evidence_count": 0,
                    "screenshot_artifact_id": "artifact-screenshot",
                    "degraded_provider_facts": [],
                    "directed_discovery_attempts": [
                        {
                            "page_type": "payment",
                            "source_page_type": "profile",
                            "outcome": "capability_blocked",
                            "blocker_code": "requires_auth",
                        }
                    ],
                    "app_runtime_policy": {
                        "app_target_snapshot": {"completion_mode": "dynamic_discovery"}
                    },
                },
            ),
        ),
    )
    terminal = _evaluate_exhausted_frontier_terminal(
        ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.NO_PROGRESS,
            reason="动态 Frontier 已收敛",
        ),
        work=work,
        counts=_exhausted_frontier_counts(),
    )
    applicability = terminal.coverage_summary["page_type_applicability"]
    assert "payment" in applicability["known_uncovered_page_types"]
    assert applicability["not_applicable_page_types"] == ()
    assert applicability["applicable_page_types"] == ("home", "payment")
    assert applicability["coverage_rate"] == 0.5
    assert terminal.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
    assert terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert terminal.reason_code == "app_global_frontier_exhausted_without_completion"
    assert terminal.coverage_summary["insufficient_reason_code"] == (
        "dynamic_page_coverage_incomplete"
    )
    assert terminal.coverage_summary["coverage_complete"] is False


def test_dynamic_entry_seen_page_type_cannot_complete_without_coverage() -> None:
    base = _work()
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=(),
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    "covered_page_types": ["home"],
                    "blocked_page_types": [],
                    "covered_milestones": ["surface:stable_business_page"],
                    "blocked_milestones": [],
                    "covered_business_page_count": 1,
                    "pending_facts_available": True,
                    "pending_action_count": 0,
                    "pending_evidence_count": 0,
                    "screenshot_artifact_id": "artifact-screenshot",
                    "degraded_provider_facts": [],
                    "directed_discovery_attempts": [
                        {
                            "page_type": "profile",
                            "source_page_type": "home",
                            "outcome": "entry_seen",
                        }
                    ],
                    "app_runtime_policy": {
                        "app_target_snapshot": {"completion_mode": "dynamic_discovery"}
                    },
                },
            ),
        ),
    )
    terminal = _evaluate_exhausted_frontier_terminal(
        ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.NO_PROGRESS,
            reason="动态 Frontier 已收敛",
        ),
        work=work,
        counts=_exhausted_frontier_counts(),
    )

    applicability = terminal.coverage_summary["page_type_applicability"]
    assert applicability["assessment_scope_complete"] is True
    assert applicability["known_uncovered_page_types"] == ("profile",)
    assert terminal.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
    assert terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert terminal.reason_code == "app_global_frontier_exhausted_without_completion"
    assert terminal.coverage_summary["insufficient_reason_code"] == (
        "dynamic_page_coverage_incomplete"
    )
    assert terminal.coverage_summary["coverage_complete"] is False


def test_video_detail_discovered_stays_not_explored() -> None:
    base = _work()
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "video_list", "video_detail"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=(),
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    "covered_page_types": ["home", "video_list"],
                    "blocked_page_types": [],
                    "covered_milestones": ["surface:stable_business_page"],
                    "blocked_milestones": [],
                    "covered_business_page_count": 2,
                    "pending_facts_available": True,
                    "pending_action_count": 0,
                    "pending_evidence_count": 0,
                    "screenshot_artifact_id": "artifact-screenshot",
                    "degraded_provider_facts": [],
                    "discovered_page_type_candidates": [
                        {
                            "page_type": "video_detail",
                            "source_capture_id": "capture-video-list",
                            "candidate_id": "candidate-card",
                        }
                    ],
                    "app_runtime_policy": {
                        "app_target_snapshot": {"completion_mode": "dynamic_discovery"}
                    },
                },
            ),
        ),
    )
    terminal = _evaluate_exhausted_frontier_terminal(
        ExplorationTerminal(
            work_id=work.work_id,
            task_id=work.task_id,
            status=ExplorationTerminalStatus.NO_PROGRESS,
            reason="动态 Frontier 已收敛",
        ),
        work=work,
        counts=_exhausted_frontier_counts(),
    )
    applicability = terminal.coverage_summary["page_type_applicability"]
    assert applicability["not_explored_page_types"] == ("video_detail",)
    assert applicability["not_applicable_page_types"] == ()
    assert applicability["unresolved_applicability_page_types"] == ()


def test_dynamic_page_type_applicability_stays_unresolved_after_budget_exhaustion() -> None:
    base = _work()
    work = replace(
        base,
        budget=replace(
            base.budget,
            remaining_actions=0,
            remaining_business_actions=0,
            remaining_obstruction_actions=0,
        ),
        app_goal=AppExplorationGoal(
            required_page_types=("home", "payment"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=1,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=(),
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    "covered_page_types": ["home"],
                    "blocked_page_types": [],
                    "covered_milestones": ["surface:stable_business_page"],
                    "blocked_milestones": [],
                    "covered_business_page_count": 1,
                    "pending_facts_available": True,
                    "pending_action_count": 0,
                    "pending_evidence_count": 0,
                    "screenshot_artifact_id": "artifact-screenshot",
                    "degraded_provider_facts": [],
                    "app_runtime_policy": {
                        "app_target_snapshot": {"completion_mode": "dynamic_discovery"}
                    },
                },
            ),
        ),
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.BUDGET_EXHAUSTED,
        reason="动作预算耗尽",
    )

    terminal = _evaluate_exhausted_frontier_terminal(
        proposed,
        work=work,
        counts=_exhausted_frontier_counts(),
    )

    applicability = terminal.coverage_summary["page_type_applicability"]
    assert applicability["assessment_scope_verified"] is False
    assert applicability["not_applicable_page_types"] == ()
    assert applicability["unresolved_applicability_page_types"] == ("payment",)
    assert applicability["coverage_rate"] is None


def test_global_frontier_exhaustion_overrides_stale_work_projection() -> None:
    base = _work()
    stale_work = replace(
        base,
        frontier=replace(
            base.frontier,
            observation=replace(
                base.frontier.observation,
                metadata={
                    **dict(base.frontier.observation.metadata),
                    "app_global_frontier": {
                        "protocol_version": "app-global-frontier-summary.v1",
                        "pending": 1,
                        "running": 0,
                        "retryable": 0,
                        "visited": 0,
                        "blocked": 0,
                        "failed": 0,
                        "nonterminal_count": 1,
                        "exhausted": False,
                    },
                },
            ),
        ),
    )
    proposed = ExplorationTerminal(
        work_id=stale_work.work_id,
        task_id=stale_work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="模型认为当前页没有候选",
    )

    terminal = _evaluate_exhausted_frontier_terminal(
        proposed,
        work=stale_work,
        counts=_exhausted_frontier_counts(),
    )

    assert terminal.reason_code == "app_global_frontier_exhausted_without_completion"
    assert terminal.coverage_summary["app_global_frontier"]["exhausted"] is True
    assert terminal.coverage_summary["app_global_frontier"]["nonterminal_count"] == 0


def test_strip_reset_synthetic_candidate_allowed_when_not_in_frontier() -> None:
    """滑回课纲的 forced-home-top-strip-reset 不在 Frontier 时不得整轮 ValueError。"""

    work = _work()
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-strip-reset",
        idempotency_key="intent-strip-reset",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.SCROLL,
        semantic_target="上方分类条滑回最新",
        parameters={
            "candidate_id": "forced-home-top-strip-reset:capture-source",
            "normalized_bounds": {
                "left": 0.12,
                "top": 0.11,
                "right": 0.88,
                "bottom": 0.17,
            },
            "action_parameters": {
                "from": {"x": 0.22, "y": 0.135},
                "to": {"x": 0.80, "y": 0.135},
                "duration_ms": 280,
            },
        },
        purpose="上方分类条已滑离最新／国产／动漫／漫画；先横向滑回课纲栏。",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            InteractionOperation.SCROLL,
        ),
        deadline=work.deadline,
    )

    selected = _selected_candidate_for_decision(work, decision)
    assert selected is not None
    assert selected.candidate_id == "forced-home-top-strip-reset:capture-source"
    assert selected.action_type is InteractionOperation.SCROLL
    assert selected.metadata.get("home_top_category_strip_reset") is True


def test_bounded_dismiss_observe_allowed_when_not_in_frontier() -> None:
    """限步清障 OBSERVE 不在 Frontier 时不得 TURN_FAILED 退桌面（QC 49f8f035）。"""

    work = _work()
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-bounded-observe",
        idempotency_key="intent-bounded-observe",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.OBSERVE,
        semantic_target="观察等待清障控件",
        parameters={
            "candidate_id": "bounded-dismiss-observe:splash:cap-1",
            "bounded_dismiss_tool": True,
            "bounded_dismiss_constrained": True,
            "bounded_dismiss_phase": "splash",
            "bounded_dismiss_step": 2,
            "bounded_dismiss_splash_steps": 2,
            "expected_state_hint": "popup_clearance_reobserve",
        },
        purpose="限步清障工具（splash 2/3）：约束输出 found=false，开屏再观察一帧找跳过。",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            InteractionOperation.OBSERVE,
        ),
        deadline=work.deadline,
    )

    selected = _selected_candidate_for_decision(work, decision)
    assert selected is not None
    assert selected.candidate_id.startswith("bounded-dismiss-observe:")
    assert selected.action_type is InteractionOperation.OBSERVE
    assert selected.metadata.get("bounded_dismiss_tool") is True
    assert selected.metadata.get("forced_popup_clearance_observe") is True


def test_bounded_dismiss_coord_allowed_when_not_in_frontier() -> None:
    """限步清障坐标点击不在 Frontier 时须重建执行。"""

    work = _work()
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-bounded-coord",
        idempotency_key="intent-bounded-coord",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="关闭弹层X",
        parameters={
            "candidate_id": "bounded-dismiss-coord:popup:cap-2",
            "bounded_dismiss_tool": True,
            "bounded_dismiss_constrained": True,
            "bounded_dismiss_phase": "popup",
            "bounded_dismiss_step": 1,
            "bounded_dismiss_popup_steps": 1,
            "normalized_bounds": {
                "left": 0.46,
                "top": 0.72,
                "right": 0.54,
                "bottom": 0.78,
            },
            "control_role": "dismiss",
            "expected_state_hint": "close_popup_x",
        },
        purpose="限步清障工具（popup 1/2）：约束输出 found=true 点关闭弹层X。",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            InteractionOperation.CLICK,
        ),
        deadline=work.deadline,
    )

    selected = _selected_candidate_for_decision(work, decision)
    assert selected is not None
    assert selected.candidate_id.startswith("bounded-dismiss-coord:")
    assert selected.metadata.get("normalized_bounds") == {
        "left": 0.46,
        "top": 0.72,
        "right": 0.54,
        "bottom": 0.78,
    }


def test_forced_pixel_home_content_entry_allowed_when_not_in_frontier() -> None:
    """合成首页影片第一卡不在 Frontier 时不得整轮 ValueError（QC 9ced42b8）。"""

    work = _work()
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-home-content-pixel",
        idempotency_key="intent-home-content-pixel",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="首页今日新剧第一片",
        parameters={
            "candidate_id": "forced-pixel-home-content-entry:cap-home",
            "home_content_entry_seek": True,
            "normalized_bounds": {
                "left": 0.03,
                "top": 0.56,
                "right": 0.32,
                "bottom": 0.78,
            },
            "require_screenshot_evidence": True,
            "expected_state_hint": "home_content_entry_first_card",
        },
        purpose="首页无可语义立即看时：相对点击今日新剧第一卡取证",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            InteractionOperation.CLICK,
        ),
        deadline=work.deadline,
    )

    selected = _selected_candidate_for_decision(work, decision)
    assert selected is not None
    assert selected.candidate_id.startswith("forced-pixel-home-content-entry:")
    assert selected.metadata.get("home_content_entry_seek") is True
    assert selected.metadata.get("forced_pixel_home_content_entry") is True
    assert selected.metadata.get("normalized_bounds") == {
        "left": 0.03,
        "top": 0.56,
        "right": 0.32,
        "bottom": 0.78,
    }


def test_tangguo_flow_synthetic_candidate_allowed_when_not_in_frontier() -> None:
    """弹窗清障后 tangguo-flow-tap 不在 Frontier 时不得整轮 ValueError 退桌面。"""

    work = _work()
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-tangguo-video",
        idempotency_key="intent-tangguo-video",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="立即看",
        parameters={
            "candidate_id": "tangguo-flow-tap:video_entry:cap-after-popup:立即看",
            "tangguo_flow_driver": True,
            "tangguo_flow_device_action": True,
            "home_content_entry_seek": True,
            "normalized_bounds": {
                "left": 0.10,
                "top": 0.42,
                "right": 0.28,
                "bottom": 0.48,
            },
            "require_screenshot_evidence": True,
        },
        purpose="糖果主流程：点击首页立即看进入影片",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            InteractionOperation.CLICK,
        ),
        deadline=work.deadline,
    )

    selected = _selected_candidate_for_decision(work, decision)
    assert selected is not None
    assert selected.candidate_id.startswith("tangguo-flow-tap:")
    assert selected.semantic_target == "立即看"
    assert selected.metadata.get("tangguo_flow_driver") is True
    assert selected.metadata.get("home_content_entry_seek") is True
    assert selected.metadata.get("normalized_bounds") == {
        "left": 0.10,
        "top": 0.42,
        "right": 0.28,
        "bottom": 0.48,
    }


def test_action_near_deadline_stops_as_budget_exhausted_before_device_io() -> None:
    base = _global_frontier_work()
    work = replace(base, deadline=datetime.now(UTC) + timedelta(seconds=90))
    decision = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-near-deadline",
        idempotency_key="intent-near-deadline",
        surface_kind=work.surface_kind,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="首页 Tab",
        parameters={"candidate_id": "candidate-a"},
        purpose="验证 deadline 余量门禁",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            work.surface_kind,
            InteractionOperation.CLICK,
        ),
        deadline=work.deadline,
    )

    assert _needs_deadline_headroom_stop(work=work, decision=decision) is True
    terminal = _deadline_headroom_terminal(work)
    assert terminal.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED
    assert terminal.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
    assert terminal.coverage_summary["budget_exhaustion_reason"] == "deadline_reached"

    enough_headroom = replace(base, deadline=datetime.now(UTC) + timedelta(seconds=181))
    assert (
        _needs_deadline_headroom_stop(work=enough_headroom, decision=decision) is False
    )

    slow_observation = replace(
        base.frontier.observation,
        metadata={
            **dict(base.frontier.observation.metadata),
            "surface_facts": {"visual_latency": {"elapsed_ms": 240_000}},
        },
    )
    slow_work = replace(
        base,
        deadline=datetime.now(UTC) + timedelta(seconds=650),
        frontier=replace(base.frontier, observation=slow_observation),
    )
    assert _needs_deadline_headroom_stop(work=slow_work, decision=decision) is True


def test_terminal_eligibility_count_excludes_blocked_and_visited_candidates() -> None:
    from services.api.app.agents.explorer.maf_agent import _eligible_candidates

    work = _global_frontier_work()
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            blocked_candidate_ids=("candidate-b",),
            visited_candidate_ids=("candidate-c",),
        ),
    )

    eligible, _ = _eligible_candidates(work)

    assert len(eligible) == 1
    assert _eligible_candidate_count(work) == len(eligible)
    assert eligible[0].candidate_id == "candidate-a"


def test_frontier_revalidation_retry_is_bounded_by_attempt_count() -> None:
    from services.api.app.application.app_frontier_restoration import (
        AppFrontierRestorationError,
    )

    transient = AppFrontierRestorationError(
        "候选暂未重现",
        error_code="APP_FRONTIER_CANDIDATE_REVALIDATION_REQUIRED",
        retryable=True,
    )
    stable_block = AppFrontierRestorationError(
        "无法安全恢复",
        error_code="APP_FRONTIER_SCOPE_MISMATCH",
    )

    assert (
        _frontier_restoration_target_status(transient, attempt_count=1)
        is AppFrontierStatus.RETRY_WAIT
    )
    assert (
        _frontier_restoration_target_status(transient, attempt_count=2)
        is AppFrontierStatus.RETRY_WAIT
    )
    assert (
        _frontier_restoration_target_status(transient, attempt_count=3)
        is AppFrontierStatus.BLOCKED
    )
    assert (
        _frontier_restoration_target_status(stable_block, attempt_count=1)
        is AppFrontierStatus.BLOCKED
    )


class MemoryWorkQueue:
    def __init__(self, work) -> None:
        self.work = work
        self.transitions = []
        self.claims = []
        self.renewals = []
        self.successor_commits = []
        self.successor_wakeups = []

    async def claim(self, *, command, lease_token):
        self.claims.append((command, lease_token))
        return ExplorerWorkLease(
            work=self.work,
            status=ExplorerWorkStatus.LEASED,
            lease_token=lease_token,
            attempt_count=1,
            row_version=4,
        )

    async def transition(self, *, command):
        self.transitions.append(command)
        return command.row_version + 1

    async def renew(self, *, command):
        self.renewals.append(command)

    async def commit_successor(self, *, command, emit_wakeup: bool = True):
        self.successor_commits.append(command)
        self.successor_wakeups.append(emit_wakeup)
        return CommittedExplorerWorkSuccessor(
            parent_row_version=command.row_version + 1,
            successor_work_id=command.successor.work_id,
            successor_created=True,
        )


class StaticTurnExecutor:
    def __init__(self, result) -> None:
        self.result = result
        self.calls = []

    async def execute(self, *, work, agent_run_id):
        self.calls.append((work, agent_run_id))
        return self.result


class MemoryAppFrontierQueue:
    def __init__(self) -> None:
        self.items = {}
        self.statuses = {}
        self.versions = {}
        self.claims = []
        self.transitions = []
        self.renewals = []

    async def publish_candidates(self, *, command):
        result = []
        for item in command.candidates:
            current = self.items.get(item.frontier_item_id)
            if current is None:
                self.items[item.frontier_item_id] = item
                self.statuses[item.frontier_item_id] = AppFrontierStatus.PENDING
                self.versions[item.frontier_item_id] = 0
                result.append(item)
            elif self.statuses[item.frontier_item_id] is AppFrontierStatus.PENDING:
                result.append(current)
        return tuple(result)

    async def claim(self, *, command, lease_token):
        self.claims.append((command, lease_token))
        candidates = [
            item
            for item_id, item in self.items.items()
            if self.statuses[item_id]
            in {
                AppFrontierStatus.PENDING,
                AppFrontierStatus.RETRY_WAIT,
            }
            and (command.frontier_item_id is None or item_id == command.frontier_item_id)
        ]
        if not candidates:
            return None
        item = candidates[0]
        self.statuses[item.frontier_item_id] = AppFrontierStatus.LEASED
        self.versions[item.frontier_item_id] += 1
        return AppFrontierLease(
            item=item,
            status=AppFrontierStatus.LEASED,
            lease_token=lease_token,
            attempt_count=1,
            row_version=self.versions[item.frontier_item_id],
        )

    async def transition(self, *, command):
        self.transitions.append(command)
        assert self.statuses[command.frontier_item_id] is command.expected_status
        assert self.versions[command.frontier_item_id] == command.row_version
        self.statuses[command.frontier_item_id] = command.target_status
        self.versions[command.frontier_item_id] += 1
        return self.versions[command.frontier_item_id]

    async def renew(self, *, command):
        self.renewals.append(command)

    async def counts(self, *, task_id, agent_run_id, session_id):
        del task_id, agent_run_id, session_id
        values = {status.value: 0 for status in AppFrontierStatus}
        for status in self.statuses.values():
            values[status.value] += 1
        return AppFrontierCounts(**values)

    async def next_pending(self, *, task_id, agent_run_id, session_id):
        del task_id, agent_run_id, session_id
        return next(
            (
                item
                for item_id, item in self.items.items()
                if self.statuses[item_id] is AppFrontierStatus.PENDING
            ),
            None,
        )

    async def list_pending(self, *, task_id, agent_run_id, session_id, limit=256):
        del task_id, agent_run_id, session_id
        return tuple(
            item
            for item_id, item in self.items.items()
            if self.statuses[item_id] is AppFrontierStatus.PENDING
        )[:limit]

    async def get_item(self, *, frontier_item_id):
        return self.items.get(frontier_item_id)

    async def cancel_nonterminal(self, *, task_id, agent_run_id, reason):
        del task_id, agent_run_id, reason
        count = 0
        for item_id, status in tuple(self.statuses.items()):
            if not status.is_terminal:
                self.statuses[item_id] = AppFrontierStatus.CANCELLED
                count += 1
        return count

    async def expire_unclaimable(self, *, task_id, agent_run_id=None):
        del task_id, agent_run_id
        return 0


class SplitTurnExecutor:
    def __init__(self, *, selected_candidate_id: str) -> None:
        self._selected_candidate_id = selected_candidate_id
        self.decisions = []
        self.executions = []

    async def decide(self, *, work):
        candidate = next(
            item
            for item in work.frontier.candidates
            if item.candidate_id == self._selected_candidate_id
        )
        decision = ActionIntent(
            task_id=work.task_id,
            session_id=work.session_id,
            intent_id="intent-global-a",
            idempotency_key="intent-global-a",
            surface_kind=work.surface_kind,
            source_capture_id=work.source_capture_id,
            action_type=candidate.action_type,
            semantic_target=candidate.semantic_target,
            parameters={"candidate_id": candidate.candidate_id},
            purpose="执行任务级 sibling A",
            risk_level=candidate.risk_level,
            capture_profile=CaptureProfilePolicy.for_surface(
                work.surface_kind,
                candidate.action_type,
            ),
            deadline=work.deadline,
        )
        self.decisions.append((work, decision))
        return decision

    async def execute_decision(self, *, work, agent_run_id, decision):
        self.executions.append((work, agent_run_id, decision))
        base = _turn_result(work)
        assert base.coordination is not None
        interaction = replace(
            base.coordination.interaction,
            intent_id=decision.intent_id,
        )
        return replace(
            base,
            decision=decision,
            coordination=replace(base.coordination, interaction=interaction),
        )


class SequenceCancellation:
    """测试用取消 Port：按调用次序返回可控的取消事实。"""

    def __init__(self, values: list[bool]) -> None:
        self._values = iter(values)
        self.calls = 0

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        del task_id, agent_run_id
        self.calls += 1
        try:
            return next(self._values)
        except StopIteration:
            return False


class ToggleCancellation:
    """测试 Driver 执行期间由外部切换的取消 Port。"""

    def __init__(self) -> None:
        self.requested = False
        self.calls = 0

    async def is_cancel_requested(self, *, task_id: str, agent_run_id: str) -> bool:
        del task_id, agent_run_id
        self.calls += 1
        return self.requested


def _runner_visual_analysis(
    *,
    work: ExplorationWork,
    capture: CaptureRef,
    page_type: str,
    image_signature: str,
    identity_observation: dict | None,
    page_state: str = "stable",
    surface_facts: dict | None = None,
    package_name: str | None = None,
    activity: str | None = None,
) -> AppVisualAnalysis:
    """为 Runner 测试构造带真实截图归属的 AppVisualAnalysis。"""

    request = _vision_request()
    request = replace(
        request,
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id=capture.capture_id,
        screenshot_artifact_id=capture.artifact_ids[0],
        image_signature=image_signature,
        package_name=package_name or work.boundaries.allowed_hosts[0],
        activity=activity or ".MainActivity",
    )
    facts = dict(surface_facts or {})
    confidence = facts.get("visual_page_type_confidence", 0.94)
    analysis = build_analysis_from_llm_payload(
        request=request,
        payload={
            "visual_page_type": page_type,
            "visual_page_type_confidence": confidence,
            "page_name": page_type,
            "page_name_source": "top_bar",
            "page_name_confidence": 0.95,
            "page_state": page_state,
            "visual_summary": f"Runner 测试观察：{page_type}",
            "page_identity_observation": identity_observation,
            "surface_facts": facts,
        },
        page_types=get_default_page_type_registry(),
        model_id="runner-test",
    )
    # LLM payload 会丢掉非视觉键；Runner 夹具在构建后再写入导航结算四件套。
    pkg = package_name or work.boundaries.allowed_hosts[0]
    epoch = (
        facts.get("capture_epoch")
        or facts.get("finalization_epoch")
        or "epoch:runner-test"
    )
    settlement = {
        "has_ui_tree": True,
        "ui_tree_artifact_id": f"ui-tree:{capture.capture_id}",
        "current_window": f"{pkg}/main",
        "capture_epoch": epoch,
        "finalization_epoch": epoch,
    }
    values = {
        item.name: getattr(analysis, item.name)
        for item in fields(analysis)
        if item.name not in {"result_hash", "contract_version", "surface_facts"}
    }
    return AppVisualAnalysis.build(
        **values,
        surface_facts={**dict(analysis.surface_facts), **settlement},
    )



class RecordingContinuation:
    def __init__(
        self,
        *,
        page_type: str | None = None,
        page_state: str | None = None,
        overlay_signature: str | None = None,
        surface_facts: dict | None = None,
        emit_action_observation: bool = False,
        before_package_name: str | None = None,
        before_activity: str | None = None,
    ) -> None:
        self.successor_work_id = None
        self.commands = []
        self.observation_commands = []
        self.page_type = page_type
        self.page_state = page_state
        self.overlay_signature = overlay_signature
        self.surface_facts = surface_facts
        self.emit_action_observation = emit_action_observation
        self.before_package_name = before_package_name
        self.before_activity = before_activity

    async def prepare_after_action(self, command):
        self.commands.append(command)
        prior = command.prior_work
        coordination = command.turn.coordination
        assert coordination is not None
        before_capture = coordination.before_capture
        after_capture = coordination.after_capture
        candidate_id = command.turn.decision.parameters.get("candidate_id")
        candidate = next(
            item for item in prior.frontier.candidates if item.candidate_id == candidate_id
        )
        after_page_type = self.page_type or "content_detail"
        after_facts = dict(self.surface_facts or {})
        after_identity = None
        if (
            after_page_type in get_default_page_type_registry().business_codes
            and after_page_type != "home"
            and not after_facts
        ):
            after_identity = _visual_observation(object_id=f"对象-{after_page_type}")
        before_analysis = None
        after_analysis = None
        action_observation = None
        if self.emit_action_observation:
            # 与导航执行器 after 证据合同对齐：Screenshot + UI Tree + Window + epoch。
            settlement_facts = {
                "has_ui_tree": True,
                "current_window": "ROOT",
                "capture_epoch": f"epoch:{before_capture.capture_id}",
            }
            before_facts = {**settlement_facts}
            after_facts = {**settlement_facts, **after_facts}
            after_facts.setdefault(
                "capture_epoch", f"epoch:{after_capture.capture_id}"
            )
            before_analysis = _runner_visual_analysis(
                work=prior,
                capture=before_capture,
                page_type=prior.frontier.observation.page_type or "home",
                image_signature="sha256:" + "a" * 64,
                identity_observation=_visual_observation(),
                package_name=self.before_package_name,
                activity=self.before_activity,
                surface_facts=before_facts,
            )
            after_analysis = _runner_visual_analysis(
                work=prior,
                capture=after_capture,
                page_type=after_page_type,
                image_signature="sha256:" + "b" * 64,
                identity_observation=after_identity,
                page_state=self.page_state or "stable",
                surface_facts=after_facts,
            )
            action_observation = build_app_action_observation(
                work=prior,
                turn=command.turn,
                candidate=candidate,
                before=before_analysis,
                after=after_analysis,
            ).to_dict()
        successor_metadata = {
            **dict(prior.frontier.observation.metadata),
            "parent_work_id": prior.work_id,
            "parent_branch_id": prior.branch_id,
            "parent_capture_id": prior.source_capture_id,
            **({"surface_facts": self.surface_facts} if self.surface_facts else {}),
        }
        if action_observation is not None:
            successor_metadata.update(
                {
                    "app_action_observation": action_observation,
                    "screenshot_artifact_id": after_analysis.screenshot_artifact_id,
                    "visual_result_hash": after_analysis.result_hash,
                }
            )
        frontier = FrontierSnapshot(
            snapshot_id="frontier-after",
            observation=SurfaceObservation(
                capture_id=after_capture.capture_id,
                surface_kind=prior.surface_kind,
                location="app://com.example.target/.HomeActivity",
                state_version=(
                    after_analysis.image_signature
                    if after_analysis is not None
                    else "sha256:" + "b" * 64
                ),
                observed_at=prior.frontier.observation.observed_at,
                page_type=self.page_type,
                page_state=self.page_state,
                overlay_signature=self.overlay_signature,
                metadata=successor_metadata,
            ),
            candidates=(),
        )
        successor = build_exploration_work(
            task_id=prior.task_id,
            session_id=prior.session_id,
            branch_id="app:capture-after",
            objective=prior.objective,
            surface_kind=prior.surface_kind,
            source_capture_id=after_capture.capture_id,
            frontier_snapshot_id=frontier.snapshot_id,
            frontier=frontier,
            boundaries=prior.boundaries,
            budget=ExplorationBudget(
                max_actions=prior.budget.max_actions,
                remaining_actions=prior.budget.remaining_actions - 1,
                max_depth=prior.budget.max_depth,
                remaining_duration_ms=prior.budget.remaining_duration_ms,
            ),
            deadline=prior.deadline,
            app_goal=prior.app_goal,
        )
        self.successor_work_id = successor.work_id
        return AppScreenshotExplorationResult(
            observation_id="visual-after",
            visual_created=True,
            page_type_assessment=AppPageTypeAssessmentAdapterResult(execution_status="skipped"),
            work=successor,
        )

    async def prepare_after_observation(self, command):
        self.observation_commands.append(command)
        prior = command.prior_work
        capture = command.turn.observation_capture
        assert capture is not None
        frontier = FrontierSnapshot(
            snapshot_id="frontier-observation",
            observation=SurfaceObservation(
                capture_id=capture.capture_id,
                surface_kind=prior.surface_kind,
                location="app://com.example.target/.MainActivity",
                state_version="sha256:" + "c" * 64,
                observed_at=prior.frontier.observation.observed_at,
                metadata={
                    **dict(prior.frontier.observation.metadata),
                    "parent_work_id": prior.work_id,
                    "parent_branch_id": prior.branch_id,
                    "parent_capture_id": prior.source_capture_id,
                },
            ),
            candidates=(),
        )
        successor = build_exploration_work(
            task_id=prior.task_id,
            session_id=prior.session_id,
            branch_id=f"app:{capture.capture_id}",
            objective=prior.objective,
            surface_kind=prior.surface_kind,
            source_capture_id=capture.capture_id,
            frontier_snapshot_id=frontier.snapshot_id,
            frontier=frontier,
            boundaries=prior.boundaries,
            budget=prior.budget,
            deadline=prior.deadline,
            app_goal=prior.app_goal,
        )
        return AppScreenshotExplorationResult(
            observation_id="visual-observation",
            visual_created=True,
            page_type_assessment=AppPageTypeAssessmentAdapterResult(execution_status="skipped"),
            work=successor,
        )


def _work():
    now = datetime.now(UTC)
    frontier = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=SurfaceObservation(
            capture_id="capture-source",
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.target/.MainActivity",
            state_version="sha256:" + "a" * 64,
            observed_at=now,
        ),
        candidates=(
            CandidateAction(
                candidate_id="candidate-1",
                semantic_target="登录按钮",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=90,
                navigation_depth=1,
                expected_surface_kind=SurfaceKind.ANDROID,
            ),
        ),
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="app:capture-source",
        objective="探索关键页面",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-source",
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_operations=(InteractionOperation.CLICK,),
            allowed_hosts=("com.example.target",),
            max_navigation_depth=2,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=2,
            remaining_actions=2,
            max_depth=2,
            remaining_duration_ms=30_000,
        ),
        deadline=now + timedelta(minutes=5),
    )


def _global_frontier_work():
    work = _work()
    candidates = tuple(
        CandidateAction(
            candidate_id=f"candidate-{name}",
            semantic_target=target,
            action_type=InteractionOperation.CLICK,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=value,
            navigation_depth=1,
            expected_surface_kind=SurfaceKind.ANDROID,
            metadata={
                "candidate_id": f"candidate-{name}",
                "purpose": f"进入{target}",
                "expected_state_hint": target,
                "frontier_scope": "app",
            },
        )
        for name, target, value in (
            ("a", "首页 Tab", 95),
            ("b", "消息 Tab", 90),
            ("c", "个人中心 Tab", 85),
        )
    )
    observation = replace(
        work.frontier.observation,
        metadata={
            "business_page_identity": "page:home",
            "app_navigation_path": ["page:app_ready", "page:home"],
        },
    )
    return replace(
        work,
        frontier=replace(work.frontier, observation=observation, candidates=candidates),
    )


def _turn_result(work) -> ExplorerTurnResult:
    intent = ActionIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id="intent-1",
        idempotency_key="intent-1",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=work.source_capture_id,
        action_type=InteractionOperation.CLICK,
        semantic_target="登录按钮",
        parameters={"candidate_id": "candidate-1"},
        purpose="进入登录页",
        risk_level=InteractionRiskLevel.LOW,
        capture_profile=CaptureProfilePolicy.for_surface(
            SurfaceKind.ANDROID,
            InteractionOperation.CLICK,
        ),
        deadline=work.deadline,
    )
    before = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-before",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="before_action",
        artifact_ids=("artifact-before",),
        parent_capture_id=work.source_capture_id,
        action_execution_id="action-1",
    )
    after = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-after",
        surface_kind=SurfaceKind.ANDROID,
        capture_kind=CaptureKind.APP,
        role="after_action",
        artifact_ids=("artifact-after",),
        parent_capture_id=before.capture_id,
        action_execution_id="action-1",
    )
    interaction = InteractionResult(
        task_id=work.task_id,
        session_id=work.session_id,
        intent_id=intent.intent_id,
        action_execution_id="action-1",
        source_capture_id=before.capture_id,
        result_capture_id=after.capture_id,
        status=InteractionResultStatus.SUCCEEDED,
    )
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=intent,
        coordination=CaptureCoordinationResult(
            before_capture=before,
            interaction=interaction,
            after_capture=after,
        ),
    )


def _package_boundary_turn_result(work) -> ExplorerTurnResult:
    turn = _turn_result(work)
    assert turn.coordination is not None
    interaction = replace(
        turn.coordination.interaction,
        status=InteractionResultStatus.FAILED,
        execution_outcome=InteractionExecutionOutcome.FAILED,
        error_code="APP_PACKAGE_BOUNDARY_VIOLATION",
        error_message="设备当前前台包已超出目标 App 包边界",
        retryable=True,
    )
    return replace(
        turn,
        coordination=replace(turn.coordination, interaction=interaction),
    )


def _observation_turn_result(work) -> ExplorerTurnResult:
    intent = TimedObservationIntent(
        task_id=work.task_id,
        session_id=work.session_id,
        phase_run_id="phase-1",
        decision_id="decision-1",
        observation_id="observation-1",
        source_capture_id=work.source_capture_id,
        surface_kind=work.surface_kind,
        delay_ms=0,
        directive=CaptureDirective(
            role="observation",
            artifact_types=("screenshot", "ui_tree"),
            inherit_parent_semantics=True,
        ),
        deadline=work.deadline,
    )
    capture = CaptureRef(
        task_id=work.task_id,
        session_id=work.session_id,
        capture_id="capture-observation",
        surface_kind=work.surface_kind,
        capture_kind=CaptureKind.APP,
        role="observation",
        artifact_ids=("artifact-observation",),
        parent_capture_id=work.source_capture_id,
        action_execution_id=None,
    )
    return ExplorerTurnResult(
        work_id=work.work_id,
        task_id=work.task_id,
        decision=intent,
        coordination=None,
        observation_capture=capture,
    )


async def test_runner_claims_executes_and_commits_success() -> None:
    work = _work()
    queue = MemoryWorkQueue(work)
    executor = StaticTurnExecutor(_turn_result(work))
    continuation = RecordingContinuation()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=continuation,
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.claimed is True
    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.result_capture_id == "capture-after"
    assert outcome.successor_work_id == continuation.successor_work_id
    assert queue.transitions[0].expected_status is ExplorerWorkStatus.LEASED
    assert queue.transitions[0].target_status is ExplorerWorkStatus.RUNNING
    assert len(queue.transitions) == 1
    assert len(queue.successor_commits) == 1
    assert queue.successor_commits[0].parent_work_id == work.work_id
    assert queue.successor_commits[0].row_version == 5
    assert queue.successor_commits[0].successor.work_id == continuation.successor_work_id
    assert queue.successor_wakeups == [True]
    assert queue.renewals[0].work_id == work.work_id
    assert queue.renewals[0].lease_owner == "app-explorer-1"
    assert executor.calls == [(work, "run-1")]
    assert continuation.commands[0].prior_work == work
    assert continuation.commands[0].turn == outcome.turn


async def test_runner_refreshes_rule_progress_when_reclaiming_same_work() -> None:
    work = _work()
    queue = MemoryWorkQueue(work)
    executor = StaticTurnExecutor(_turn_result(work))

    class FactsReader:
        def __init__(self):
            self.calls = []

        async def read_app_facts(self, **kwargs):
            self.calls.append(kwargs)
            return {"app_rule_inventory_coverage": {
                "protocol_version": "app-rule-coverage-facts.v1",
                "pending_inspection_work_count": max(0, 2 - len(self.calls)),
            }}

    facts = FactsReader()
    runner = AppExplorationTurnRunner(
        queue=queue, turn_executor=executor, continuation=RecordingContinuation(),
        lease_owner="app-explorer-1", scheduler_facts_reader=facts,
    )

    await runner.run_one(task_id=work.task_id, agent_run_id="run-1")
    await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert facts.calls == [{
        "task_id": work.task_id, "agent_run_id": "run-1", "session_id": work.session_id,
    }] * 2
    assert [item[0].frontier.observation.metadata["app_rule_inventory_coverage"][
        "pending_inspection_work_count"
    ] for item in executor.calls] == [1, 0]
    assert "app_rule_inventory_coverage" not in queue.work.frontier.observation.metadata


def test_exhausted_frontier_without_terminal_keeps_auditable_summary(monkeypatch) -> None:
    work = _work()
    proposed = ExplorationTerminal(
        work_id=work.work_id, task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS, reason="当前没有候选",
    )
    from types import SimpleNamespace

    monkeypatch.setattr(
        "services.api.app.application.app_exploration_turn.AppExplorationTerminalEvaluator.evaluate",
        lambda *args, **kwargs: SimpleNamespace(
            terminal=None, coverage=SimpleNamespace(to_dict=lambda: {"coverage_complete": False})
        ),
    )
    terminal = _evaluate_exhausted_frontier_terminal(
        proposed, work=work, counts=_exhausted_frontier_counts()
    )
    assert terminal.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
    assert terminal.coverage_summary["coverage_complete"] is False


def test_exhausted_frontier_waits_for_current_live_judge_within_deadline(monkeypatch) -> None:
    from types import SimpleNamespace

    base = _work()
    work = replace(base, frontier=replace(base.frontier, observation=replace(
        base.frontier.observation, metadata={"app_rule_inventory_coverage": {
            "protocol_version": "app-rule-coverage-facts.v1",
            "pending_inspection_work_count": 1,
        }},
    )))
    proposed = ExplorationTerminal(
        work_id=work.work_id, task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS, reason="当前没有候选",
    )
    monkeypatch.setattr(
        "services.api.app.application.app_exploration_turn.AppExplorationTerminalEvaluator.evaluate",
        lambda *args, **kwargs: SimpleNamespace(terminal=None),
    )

    with pytest.raises(AppFrontierDeferredError, match="规则正在判定"):
        _evaluate_exhausted_frontier_terminal(
            proposed, work=work, counts=_exhausted_frontier_counts()
        )


async def test_runner_closes_package_boundary_failure_without_successor() -> None:
    work = _work()
    queue = MemoryWorkQueue(work)
    continuation = RecordingContinuation()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaticTurnExecutor(_package_boundary_turn_result(work)),
        continuation=continuation,
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.claimed is True
    assert outcome.final_status is ExplorerWorkStatus.NO_PROGRESS
    assert outcome.error_code == "APP_PACKAGE_BOUNDARY_VIOLATION"
    assert outcome.result_capture_id == "capture-after"
    assert outcome.successor_work_id is None
    assert outcome.turn is not None
    assert outcome.turn.is_terminal is True
    assert isinstance(outcome.turn.decision, ExplorationTerminal)
    assert outcome.turn.decision.reason_code == "app_package_boundary_violation"
    assert outcome.turn.decision.outcome == "evidence_insufficient"
    assert outcome.turn.coordination is not None
    assert outcome.turn.coordination.before_capture.capture_id == "capture-before"
    assert outcome.turn.coordination.after_capture.capture_id == "capture-after"
    assert outcome.turn.coordination.interaction.error_code == ("APP_PACKAGE_BOUNDARY_VIOLATION")
    assert continuation.commands == []
    assert continuation.observation_commands == []
    assert queue.successor_commits == []
    terminal_transition = queue.transitions[-1]
    assert terminal_transition.target_status is ExplorerWorkStatus.NO_PROGRESS
    assert terminal_transition.terminal is not None
    assert terminal_transition.terminal.status.value == "no_progress"
    assert terminal_transition.terminal.outcome == "evidence_insufficient"
    assert terminal_transition.terminal.reason_code == "app_package_boundary_violation"
    assert terminal_transition.error_code == "APP_PACKAGE_BOUNDARY_VIOLATION"
    assert terminal_transition.error_message == "设备当前前台包已超出目标 App 包边界"


async def test_runner_settles_global_frontier_as_failed_on_package_boundary() -> None:
    class BoundarySplitTurnExecutor(SplitTurnExecutor):
        async def execute_decision(self, *, work, agent_run_id, decision):
            turn = await super().execute_decision(
                work=work,
                agent_run_id=agent_run_id,
                decision=decision,
            )
            assert turn.coordination is not None
            interaction = replace(
                turn.coordination.interaction,
                status=InteractionResultStatus.FAILED,
                execution_outcome=InteractionExecutionOutcome.FAILED,
                error_code="APP_PACKAGE_BOUNDARY_VIOLATION",
                error_message="设备当前前台包已超出目标 App 包边界",
                retryable=True,
            )
            return replace(
                turn,
                coordination=replace(turn.coordination, interaction=interaction),
            )

    work = _global_frontier_work()
    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=BoundarySplitTurnExecutor(selected_candidate_id="candidate-a"),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.NO_PROGRESS
    assert outcome.successor_work_id is None
    assert queue.successor_commits == []
    terminal_transition = queue.transitions[-1]
    settlement = terminal_transition.app_frontier_settlement
    assert settlement is not None
    assert settlement.target_status is AppFrontierStatus.FAILED
    assert settlement.result_capture_id == "capture-after"
    assert settlement.error_code == "APP_PACKAGE_BOUNDARY_VIOLATION"
    assert settlement.frontier_item_id is not None
    assert settlement.lease_token is not None
    assert settlement.row_version is not None


async def test_runner_recovers_persisted_decision_without_global_frontier() -> None:
    work = _work()
    recovered_turn = _turn_result(work)

    class RecoveryTurnExecutor:
        def __init__(self) -> None:
            self.recovery_calls = []
            self.execution_calls = []

        async def recover_decision(self, *, work):
            self.recovery_calls.append(work)
            return recovered_turn.decision

        async def execute_decision(self, *, work, agent_run_id, decision):
            self.execution_calls.append((work, agent_run_id, decision))
            return recovered_turn

        async def execute(self, *, work, agent_run_id):
            del work, agent_run_id
            raise AssertionError("已有持久化动作时不得重新询问 Explorer")

    queue = MemoryWorkQueue(work)
    executor = RecoveryTurnExecutor()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert executor.recovery_calls == [work]
    assert executor.execution_calls == [(work, "run-1", recovered_turn.decision)]


async def test_runner_discards_recovered_action_after_goal_becomes_complete() -> None:
    base = _work()
    metadata = {
        "covered_page_types": ["home", "content_detail"],
        "blocked_page_types": [],
        "covered_milestones": ["surface:stable_business_page"],
        "blocked_milestones": [],
        "covered_business_page_count": 2,
        "pending_facts_available": True,
        "pending_action_count": 0,
        "pending_evidence_count": 0,
        "screenshot_artifact_id": "artifact-screenshot",
        "degraded_provider_facts": [],
    }
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "content_detail"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata=metadata,
            ),
        ),
    )
    recovered_turn = _turn_result(work)

    class RecoveryTurnExecutor:
        def __init__(self) -> None:
            self.execution_calls = []

        async def recover_decision(self, *, work):
            del work
            return recovered_turn.decision

        async def execute_decision(self, *, work, agent_run_id, decision):
            self.execution_calls.append((work, agent_run_id, decision))
            return ExplorerTurnResult(
                work_id=work.work_id,
                task_id=work.task_id,
                decision=decision,
                coordination=None,
            )

        async def execute(self, *, work, agent_run_id):
            del work, agent_run_id
            raise AssertionError("恢复路径必须走 execute_decision")

    queue = MemoryWorkQueue(work)
    executor = RecoveryTurnExecutor()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.successor_work_id is None
    assert len(executor.execution_calls) == 1
    decision = executor.execution_calls[0][2]
    assert isinstance(decision, ExplorationTerminal)
    assert decision.reason_code == "exploration_completed"
    assert queue.successor_commits == []


async def test_global_frontier_cannot_override_goal_completion_terminal() -> None:
    work = _global_frontier_work()
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    await global_frontier.register_work_candidates(work=work, agent_run_id="run-1")
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="锁定覆盖目标已满足",
        outcome=AppExplorationOutcome.EXPLORATION_COMPLETED.value,
        reason_code="exploration_completed",
        coverage_summary={
            "goal_driven": True,
            "coverage_complete": True,
            "frontier_exhausted": False,
        },
    )
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )

    decision, assigned = await runner._restore_pending_frontier(
        work=work,
        agent_run_id="run-1",
        proposed_terminal=terminal,
    )

    assert decision is terminal
    assert assigned is None
    assert frontier_queue.transitions == []
    assert set(frontier_queue.statuses.values()) == {AppFrontierStatus.PENDING}


@pytest.mark.parametrize("has_siblings", [False, True])
async def test_runner_restores_trusted_sibling_after_local_repeated_page_plateau(
    has_siblings: bool,
) -> None:
    work = _global_frontier_work()
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            candidates=work.frontier.candidates if has_siblings else (),
            observation=replace(
                work.frontier.observation,
                metadata={
                    **dict(work.frontier.observation.metadata),
                    "dynamic_repeated_business_page_action_count": 3,
                    "app_rule_inventory_coverage": {"pending_rule_count": 1},
                    "app_runtime_policy": {
                        "app_target_snapshot": {"completion_mode": "dynamic_discovery"}
                    },
                },
            ),
        ),
    )
    terminal = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="连续动作落到已覆盖业务页",
        outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value,
        reason_code="app_repeated_business_page_plateau",
        coverage_summary={"repeated_business_page_plateau_reached": True},
    )

    class PlateauTurnExecutor:
        def __init__(self) -> None:
            self.executions = []

        async def recover_decision(self, *, work):
            del work
            return None

        async def decide(self, *, work):
            del work
            return terminal

        async def execute_decision(self, *, work, agent_run_id, decision):
            self.executions.append((work, agent_run_id, decision))
            if isinstance(decision, ExplorationTerminal):
                return ExplorerTurnResult(
                    work_id=work.work_id, task_id=work.task_id,
                    decision=decision, coordination=None,
                )
            turn = _turn_result(work)
            return replace(
                turn,
                decision=decision,
                coordination=replace(
                    turn.coordination,
                    interaction=replace(
                        turn.coordination.interaction, intent_id=decision.intent_id
                    ),
                ),
            )

    frontier_queue = MemoryAppFrontierQueue()
    executor = PlateauTurnExecutor()
    queue = MemoryWorkQueue(work)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.turn is not None
    assert len(executor.executions) == 1
    if has_siblings:
        assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
        assert isinstance(outcome.turn.decision, ActionIntent)
        assert outcome.turn.decision.parameters["candidate_id"] == "candidate-a"
        assert queue.successor_commits
        assert AppFrontierStatus.PENDING in frontier_queue.statuses.values()
        assert AppFrontierStatus.RUNNING in [
            item.target_status for item in frontier_queue.transitions
        ]
    else:
        assert outcome.final_status is ExplorerWorkStatus.NO_PROGRESS
        assert isinstance(outcome.turn.decision, ExplorationTerminal)
        assert outcome.turn.decision.outcome == AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value
        assert queue.successor_commits == []


async def test_restore_supersedes_executed_sibling_before_device_action() -> None:
    from services.api.app.application.app_semantic_progress import (
        build_app_semantic_action_key,
    )

    base = _global_frontier_work()
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    registered = await global_frontier.register_work_candidates(
        work=base,
        agent_run_id="run-1",
    )
    first = next(item for item in registered if item.candidate.candidate_id == "candidate-a")
    semantic_key = build_app_semantic_action_key(
        first.candidate,
        page_identity=first.source_page_identity,
    )
    work = replace(
        base,
        frontier=replace(
            base.frontier,
            observation=replace(
                base.frontier.observation,
                page_type="home",
                page_state="stable",
                metadata={
                    **dict(base.frontier.observation.metadata),
                    "covered_page_types": ("home",),
                    "app_semantic_action_counts": {semantic_key: 1},
                },
            ),
        ),
    )
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前页候选耗尽",
    )

    decision, assigned = await runner._restore_pending_frontier(
        work=work,
        agent_run_id="run-1",
        proposed_terminal=proposed,
    )

    assert isinstance(decision, ActionIntent)
    assert assigned is not None
    assert assigned.item.candidate.candidate_id == "candidate-b"
    assert frontier_queue.statuses[first.frontier_item_id] is AppFrontierStatus.SUPERSEDED
    superseded = next(
        command
        for command in frontier_queue.transitions
        if command.target_status is AppFrontierStatus.SUPERSEDED
    )
    assert superseded.error_code == "APP_FRONTIER_REDUNDANT_PAGE"
    assert superseded.result_capture_id is None


async def test_restore_supersedes_when_restore_only_attempts_exhausted() -> None:
    from services.api.app.application.app_frontier_restoration import (
        APP_FRONTIER_RESTORE_ATTEMPTS_KEY,
        APP_FRONTIER_RESTORE_MAX_ATTEMPTS,
    )

    source = _global_frontier_work()
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    registered = await global_frontier.register_work_candidates(
        work=source,
        agent_run_id="run-1",
    )
    first = next(item for item in registered if item.candidate.candidate_id == "candidate-a")
    system_back = CandidateAction(
        candidate_id="candidate-system-back",
        semantic_target="系统返回",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=0,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "candidate_id": "candidate-system-back",
            "purpose": "返回上一页",
            "expected_state_hint": "back",
            "frontier_scope": "local",
        },
    )
    # 当前页无 sibling 可见候选，仅有 BACK → restore_only；已耗尽次数则 SUPERSEDED
    work = replace(
        source,
        frontier=replace(
            source.frontier,
            observation=replace(
                source.frontier.observation,
                metadata={
                    **dict(source.frontier.observation.metadata),
                    "business_page_identity": "page:video-list",
                    APP_FRONTIER_RESTORE_ATTEMPTS_KEY: {
                        first.frontier_item_id: APP_FRONTIER_RESTORE_MAX_ATTEMPTS,
                    },
                },
            ),
            candidates=(system_back,),
        ),
    )
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="当前页候选耗尽",
    )

    decision, assigned = await runner._restore_pending_frontier(
        work=work,
        agent_run_id="run-1",
        proposed_terminal=proposed,
    )

    assert frontier_queue.statuses[first.frontier_item_id] is AppFrontierStatus.SUPERSEDED
    superseded = next(
        command
        for command in frontier_queue.transitions
        if command.target_status is AppFrontierStatus.SUPERSEDED
        and command.frontier_item_id == first.frontier_item_id
    )
    assert superseded.error_code == "APP_FRONTIER_RESTORE_EXHAUSTED"
    assert isinstance(decision, ActionIntent)
    assert decision.parameters.get("app_frontier_restore_only") is True
    assert assigned is None
    assert decision.parameters.get("app_frontier_item_id") != first.frontier_item_id


async def test_runner_preserves_abc_siblings_and_atomically_settles_selected_a() -> None:
    work = _global_frontier_work()
    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    executor = SplitTurnExecutor(selected_candidate_id="candidate-a")
    continuation = RecordingContinuation(emit_action_observation=True)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=continuation,
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    metric_before = app_metric_observation(APP_FRONTIER_CLAIM_AGE_SECONDS)
    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")
    metric_after = app_metric_observation(APP_FRONTIER_CLAIM_AGE_SECONDS)

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert len(frontier_queue.items) == 3
    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    assert settlement.target_status is AppFrontierStatus.VISITED
    assert settlement.result_capture_id == "capture-after"
    selected = next(
        item
        for item in frontier_queue.items.values()
        if item.candidate.candidate_id == "candidate-a"
    )
    assert settlement.frontier_item_id == selected.frontier_item_id
    assert frontier_queue.statuses[selected.frontier_item_id] is AppFrontierStatus.RUNNING
    remaining = {
        item.candidate.candidate_id
        for item_id, item in frontier_queue.items.items()
        if frontier_queue.statuses[item_id] is AppFrontierStatus.PENDING
    }
    assert remaining == {"candidate-b", "candidate-c"}
    assert len(frontier_queue.renewals) >= 1
    assert metric_after.count == metric_before.count + 1
    assert metric_after.latest >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actual_page_type", "expected_status", "expected_error_code", "surface_facts"),
    (
        ("content_detail", AppFrontierStatus.VISITED, None, {}),
        (
            "guide_page",
            AppFrontierStatus.SUPERSEDED,
            "APP_FRONTIER_INTERMEDIATE_PAGE_REACHED",
            {"visual_page_type_confidence": 0.95},
        ),
        (
            "home",
            AppFrontierStatus.FAILED,
            "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
            {},
        ),
        *(
            (
                "content_detail",
                AppFrontierStatus.FAILED,
                "APP_FRONTIER_TARGET_ARRIVAL_UNPROVEN",
                facts,
            )
            for facts in (
                {"popup_visible": True},
                {"topmost_ad_popup": {"is_ad": True}},
                {"visual_page_type_confidence": 0.69},
            )
        ),
    ),
)
async def test_runner_settles_expected_target_only_after_stable_arrival(
    actual_page_type: str,
    expected_status: AppFrontierStatus,
    expected_error_code: str | None,
    surface_facts: dict,
) -> None:
    base = _global_frontier_work()
    candidates = tuple(
        replace(
            candidate,
            metadata={
                **dict(candidate.metadata),
                "expected_next_page_type": "content_detail",
            },
        )
        if candidate.candidate_id == "candidate-a"
        else candidate
        for candidate in base.frontier.candidates
    )
    work = replace(base, frontier=replace(base.frontier, candidates=candidates))
    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=SplitTurnExecutor(selected_candidate_id="candidate-a"),
        continuation=RecordingContinuation(
            page_type=actual_page_type,
            page_state="stable",
            surface_facts=surface_facts,
            emit_action_observation=True,
        ),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    assert settlement.target_status is expected_status
    assert settlement.error_code == expected_error_code


@pytest.mark.parametrize(
    "action_type",
    (InteractionOperation.BACK, InteractionOperation.WARM_RESTORE),
)
async def test_runner_consumes_surface_restored_as_visited_without_business_arrival(
    action_type: InteractionOperation,
) -> None:
    """包边界恢复的同次证明可结算 Frontier，但不冒充业务到达。"""

    base = _global_frontier_work()
    candidate = replace(
        base.frontier.candidates[0],
        action_type=action_type,
        semantic_target=(
            "返回目标 App" if action_type is InteractionOperation.BACK else "重新打开目标应用"
        ),
        metadata={
            **dict(base.frontier.candidates[0].metadata),
            "package_boundary_recovery": True,
            "action_parameters": (
                {"package_name": "com.example.target"}
                if action_type is InteractionOperation.WARM_RESTORE
                else {}
            ),
        },
    )
    work = replace(
        base,
        boundaries=replace(
            base.boundaries,
            allowed_operations=(
                InteractionOperation.BACK,
                InteractionOperation.WARM_RESTORE,
            ),
        ),
        frontier=replace(base.frontier, candidates=(candidate,)),
    )

    class RecoveryTurnExecutor(SplitTurnExecutor):
        async def decide(self, *, work):
            decision = await super().decide(work=work)
            if action_type is InteractionOperation.WARM_RESTORE:
                decision = replace(
                    decision,
                    parameters={
                        **dict(decision.parameters),
                        "package_name": "com.example.target",
                    },
                )
            return decision

    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=RecoveryTurnExecutor(selected_candidate_id=candidate.candidate_id),
        continuation=RecordingContinuation(
            page_type="content_detail",
            page_state="stable",
            emit_action_observation=True,
            before_package_name="com.android.chrome",
            before_activity="com.android.chrome.ChromeActivity",
        ),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    assert settlement.target_status is AppFrontierStatus.VISITED
    assert settlement.result_capture_id == "capture-after"
    assert settlement.error_code is None
    successor = queue.successor_commits[0].successor
    proof = successor.frontier.observation.metadata["app_action_observation"]
    assert proof["outcome"] == "surface_restored"
    assert proof["business_visible"] is True


@pytest.mark.parametrize(
    "error_code",
    [
        "STALE_VISUAL_CANDIDATE",
        "APP_CANDIDATE_REVALIDATION_REQUIRED",
        "POST_SCROLL_REBIND_REQUIRED",
    ],
)
async def test_runner_retries_stale_visual_candidate_from_fresh_successor_capture(
    error_code,
) -> None:
    """动作前候选过期未触发 Driver 时，当前语义分支必须允许新截图重签。"""

    class StaleVisualSplitTurnExecutor(SplitTurnExecutor):
        async def execute_decision(self, *, work, agent_run_id, decision):
            turn = await super().execute_decision(
                work=work,
                agent_run_id=agent_run_id,
                decision=decision,
            )
            assert turn.coordination is not None
            interaction = replace(
                turn.coordination.interaction,
                status=InteractionResultStatus.FAILED,
                execution_outcome=InteractionExecutionOutcome.FAILED,
                error_code=error_code,
                error_message="动作前截图已偏离候选来源",
                retryable=True,
            )
            return replace(
                turn,
                coordination=replace(turn.coordination, interaction=interaction),
            )

    work = _global_frontier_work()
    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaleVisualSplitTurnExecutor(selected_candidate_id="candidate-a"),
        continuation=RecordingContinuation(page_type="startup_ad", page_state="countdown"),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert outcome.successor_work_id is not None
    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    assert settlement.target_status is AppFrontierStatus.RETRY_WAIT
    assert settlement.result_capture_id == "capture-after"
    assert settlement.error_code == error_code
    assert settlement.retry_delay_seconds == 1


@pytest.mark.asyncio
async def test_frontier_deferred_resets_work_attempt_count() -> None:
    """Frontier 暂不可领必须重置 Work attempt，避免 3 次空转误杀。"""

    terminal = ExplorationTerminal(
        work_id=_global_frontier_work().work_id,
        task_id=_global_frontier_work().task_id,
        status=ExplorationTerminalStatus.NO_PROGRESS,
        reason="本地候选已耗尽，等待全局 Frontier",
        outcome=AppExplorationOutcome.EVIDENCE_INSUFFICIENT.value,
        reason_code="app_local_frontier_exhausted",
    )
    work = replace(
        _global_frontier_work(),
        work_id=terminal.work_id,
        task_id=terminal.task_id,
    )

    class DeferredFrontier:
        async def register_work_candidates(self, *, work, agent_run_id):
            del work, agent_run_id
            return ()

        def retain_registered_candidates(self, *, work, registered):
            del registered
            return work

        async def project_counts(self, *, work, agent_run_id):
            del agent_run_id
            return work

        async def next_pending(self, *, work, agent_run_id):
            del work, agent_run_id
            return None

        async def next_pending_for_restore(self, *, work, agent_run_id):
            del work, agent_run_id
            return None

        async def counts(self, *, work, agent_run_id):
            del work, agent_run_id
            return AppFrontierCounts(
                pending=2,
                leased=0,
                running=1,
                retry_wait=0,
                visited=0,
                blocked=0,
                failed=0,
                cancelled=0,
                superseded=0,
            )

    class TerminalTurnExecutor:
        async def recover_decision(self, *, work):
            del work
            return None

        async def decide_deterministic(self, *, work):
            del work
            return None

        async def decide(self, *, work):
            del work
            return terminal

        async def execute_decision(self, *, work, agent_run_id, decision):
            raise AssertionError("Deferred 路径不得执行动作")

    queue = MemoryWorkQueue(work)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=TerminalTurnExecutor(),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=DeferredFrontier(),  # type: ignore[arg-type]
        frontier_queue=MemoryAppFrontierQueue(),
        retry_delay_seconds=1,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.RETRY_WAIT
    assert outcome.error_code == "APP_FRONTIER_NOT_CLAIMABLE"
    assert queue.transitions
    assert queue.transitions[-1].reset_attempt_count is True
    assert queue.transitions[-1].retry_delay_seconds == 1


async def test_runner_yields_stalled_stable_page_to_highest_ranked_global_sibling() -> None:
    base = _global_frontier_work()
    work = replace(
        base,
        frontier=replace(
            base.frontier,
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    **dict(base.frontier.observation.metadata),
                    "dynamic_no_new_business_page_action_count": 1,
                },
            ),
        ),
    )
    queue = MemoryWorkQueue(work)
    frontier_queue = MemoryAppFrontierQueue()
    executor = SplitTurnExecutor(selected_candidate_id="candidate-c")
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(emit_action_observation=True),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert executor.decisions[0][1].parameters["candidate_id"] == "candidate-c"
    assert executor.executions[0][2].parameters["candidate_id"] == "candidate-a"
    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    assert settlement.target_status is AppFrontierStatus.VISITED


async def test_runner_keeps_current_missing_goal_before_global_fairness() -> None:
    """当前帧缺页入口不能被全局 sibling 公平轮转抢占。"""

    base = _global_frontier_work()
    candidates = tuple(
        replace(
            candidate,
            metadata={
                **dict(candidate.metadata),
                **(
                    {"expected_next_page_type": "settings", "frontier_scope": "local"}
                    if candidate.candidate_id == "candidate-c"
                    else (
                        {"expected_next_page_type": "post_list"}
                        if candidate.candidate_id == "candidate-a"
                        else {}
                    )
                ),
            },
        )
        for candidate in base.frontier.candidates
    )
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "post_list", "settings"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=3,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=candidates,
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    **dict(base.frontier.observation.metadata),
                    "covered_page_types": ["home"],
                    "dynamic_no_new_business_page_action_count": 1,
                },
            ),
        ),
    )

    class DeterministicCurrentGapExecutor(SplitTurnExecutor):
        def __init__(self) -> None:
            super().__init__(selected_candidate_id="candidate-c")
            self.deterministic_calls = 0

        async def decide_deterministic(self, *, work):
            self.deterministic_calls += 1
            return await self.decide(work=work)

    executor = DeterministicCurrentGapExecutor()
    frontier_queue = MemoryAppFrontierQueue()
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=executor,
        continuation=RecordingContinuation(page_type="settings", page_state="stable"),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert executor.deterministic_calls == 1
    assert executor.executions[0][2].parameters["candidate_id"] == "candidate-c"
    assert any(
        item.candidate.candidate_id == "candidate-a"
        and frontier_queue.statuses[item.frontier_item_id] is AppFrontierStatus.PENDING
        for item in frontier_queue.items.values()
    )


async def test_runner_keeps_pending_bottom_nav_item_for_current_frame_rebind() -> None:
    """当前帧已给出底栏候选时，同轮模型动作仍须领取原 pending item。"""

    base = _global_frontier_work()
    stale_candidate = CandidateAction(
        candidate_id="stale-bottom-profile",
        semantic_target="底栏「个人中心」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={
            "candidate_id": "stale-bottom-profile",
            "purpose": "进入个人中心",
            "expected_state_hint": "profile",
            "expected_next_page_type": "profile",
            "frontier_scope": "app",
            "bottom_navigation_tab_seek": True,
            "normalized_bounds": {
                "left": 0.80,
                "top": 0.90,
                "right": 0.90,
                "bottom": 0.99,
            },
        },
    )
    current_candidate = replace(
        stale_candidate,
        candidate_id="current-bottom-profile",
        metadata={
            **dict(stale_candidate.metadata),
            "candidate_id": "current-bottom-profile",
            "normalized_bounds": {
                "left": 0.82,
                "top": 0.89,
                "right": 0.92,
                "bottom": 0.98,
            },
        },
    )
    stale_source = replace(
        base,
        frontier=replace(base.frontier, candidates=(stale_candidate,)),
    )
    current_work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=(current_candidate,),
            observation=replace(
                base.frontier.observation,
                page_type="home",
                page_state="stable",
                metadata={
                    **dict(base.frontier.observation.metadata),
                    "required_page_types": ["home", "profile"],
                    "covered_page_types": ["home"],
                    "business_page_identity": "page:home",
                },
            ),
        ),
    )
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    (stale_item,) = await global_frontier.register_work_candidates(
        work=stale_source,
        agent_run_id="run-1",
    )
    assert frontier_queue.statuses[stale_item.frontier_item_id] is AppFrontierStatus.PENDING

    class CurrentFrameExecutor(SplitTurnExecutor):
        def __init__(self) -> None:
            super().__init__(selected_candidate_id="current-bottom-profile")
            self.deterministic_calls = 0

        async def decide_deterministic(self, *, work):
            del work
            self.deterministic_calls += 1
            return None

    queue = MemoryWorkQueue(current_work)
    executor = CurrentFrameExecutor()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(
            page_type="profile",
            page_state="stable",
            emit_action_observation=True,
        ),
        lease_owner="app-explorer-1",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=current_work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert executor.deterministic_calls == 1
    assert executor.decisions[0][1].parameters["candidate_id"] == "current-bottom-profile"
    assert executor.executions[0][2].parameters["candidate_id"] == "current-bottom-profile"
    assert len(frontier_queue.claims) == 1
    assert frontier_queue.claims[0][0].frontier_item_id == stale_item.frontier_item_id
    assert frontier_queue.statuses[stale_item.frontier_item_id] is AppFrontierStatus.RUNNING
    assert all(
        command.target_status is not AppFrontierStatus.SUPERSEDED
        for command in frontier_queue.transitions
    )
    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    assert settlement.frontier_item_id == stale_item.frontier_item_id
    assert settlement.target_status is AppFrontierStatus.VISITED


async def test_runner_keeps_current_decision_before_global_fairness_threshold() -> None:
    base = _global_frontier_work()
    work = replace(
        base,
        frontier=replace(
            base.frontier,
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    **dict(base.frontier.observation.metadata),
                    "dynamic_no_new_business_page_action_count": 0,
                },
            ),
        ),
    )
    executor = SplitTurnExecutor(selected_candidate_id="candidate-c")
    frontier_queue = MemoryAppFrontierQueue()
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert executor.executions[0][2].parameters["candidate_id"] == "candidate-c"


async def test_runner_executes_missing_goal_frontier_without_model_decision() -> None:
    base = _global_frontier_work()
    candidates = tuple(
        replace(
            candidate,
            metadata={
                **dict(candidate.metadata),
                "frontier_scope": (
                    "page" if candidate.candidate_id == "candidate-a" else "app"
                ),
                **(
                    {"expected_next_page_type": "profile"}
                    if candidate.candidate_id == "candidate-a"
                    else {}
                ),
            },
        )
        for candidate in base.frontier.candidates
    )
    work = replace(
        base,
        app_goal=AppExplorationGoal(
            required_page_types=("home", "profile"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=("com.example.target",),
        ),
        frontier=replace(
            base.frontier,
            candidates=candidates,
            observation=replace(
                base.frontier.observation,
                page_state="stable",
                metadata={
                    **dict(base.frontier.observation.metadata),
                    "covered_page_types": ["home"],
                },
            ),
        ),
    )
    executor = SplitTurnExecutor(selected_candidate_id="candidate-c")
    frontier_queue = MemoryAppFrontierQueue()
    queue = MemoryWorkQueue(work)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(
            page_type="profile",
            page_state="stable",
            emit_action_observation=True,
        ),
        lease_owner="app-explorer-1",
        global_frontier=AppGlobalFrontierService(repository=frontier_queue),
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert executor.decisions == []
    assert executor.executions[0][2].parameters["candidate_id"] == "candidate-a"
    settlement = queue.successor_commits[0].app_frontier_settlement
    assert settlement is not None
    assert settlement.target_status is AppFrontierStatus.VISITED


async def test_runner_discards_recovered_action_when_frontier_item_is_retry_wait() -> None:
    work = _global_frontier_work()
    frontier_queue = MemoryAppFrontierQueue()
    global_frontier = AppGlobalFrontierService(repository=frontier_queue)
    registered = await global_frontier.register_work_candidates(
        work=work,
        agent_run_id="run-1",
    )
    item_a = next(item for item in registered if item.candidate.candidate_id == "candidate-a")
    frontier_queue.statuses[item_a.frontier_item_id] = AppFrontierStatus.RETRY_WAIT
    recovered = await SplitTurnExecutor(selected_candidate_id="candidate-a").decide(work=work)

    class RecoverStaleThenChooseB(SplitTurnExecutor):
        async def recover_decision(self, *, work):
            del work
            return recovered

    executor = RecoverStaleThenChooseB(selected_candidate_id="candidate-b")
    runner = AppExplorationTurnRunner(
        queue=MemoryWorkQueue(work),
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        global_frontier=global_frontier,
        frontier_queue=frontier_queue,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert len(executor.decisions) == 1
    assert executor.decisions[0][1].parameters["candidate_id"] == "candidate-b"
    assert executor.executions[0][2].parameters["candidate_id"] == "candidate-b"


async def test_runner_commits_observation_successor_without_action_coordination() -> None:
    work = _work()
    queue = MemoryWorkQueue(work)
    executor = StaticTurnExecutor(_observation_turn_result(work))
    continuation = RecordingContinuation()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=continuation,
        lease_owner="worker-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.result_capture_id == "capture-observation"
    assert outcome.successor_work_id is not None
    assert continuation.commands == []
    assert len(continuation.observation_commands) == 1
    assert queue.successor_commits[0].successor.budget.remaining_actions == 2


async def test_runner_returns_idle_when_no_app_work_is_available() -> None:
    class EmptyQueue(MemoryWorkQueue):
        async def claim(self, *, command, lease_token):
            self.claims.append((command, lease_token))
            return None

    runner = AppExplorationTurnRunner(
        queue=EmptyQueue(_work()),
        turn_executor=StaticTurnExecutor(None),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id="task-1", agent_run_id="run-1")

    assert outcome.claimed is False
    assert outcome.final_status is None
    assert outcome.result_capture_id is None


async def test_runner_checks_cancellation_before_claim_without_creating_work() -> None:
    work = _work()
    queue = MemoryWorkQueue(work)
    executor = StaticTurnExecutor(_turn_result(work))
    cancellation = SequenceCancellation([True])
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        cancellation=cancellation,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert cancellation.calls == 1
    assert outcome.claimed is False
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.error_code == "WORKFLOW_CANCELED"
    assert queue.claims == []
    assert executor.calls == []


async def test_runner_checks_cancellation_before_driver_and_never_starts_action() -> None:
    work = _work()
    queue = MemoryWorkQueue(work)
    executor = StaticTurnExecutor(_turn_result(work))
    continuation = RecordingContinuation()
    cancellation = SequenceCancellation([False, True])
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=continuation,
        lease_owner="app-explorer-1",
        cancellation=cancellation,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.claimed is True
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.error_code == "WORKFLOW_CANCELED"
    assert executor.calls == []
    assert continuation.commands == []
    assert continuation.observation_commands == []
    assert queue.successor_commits == []


async def test_runner_cancels_driver_when_port_changes_during_execution() -> None:
    class BlockingTurnExecutor(StaticTurnExecutor):
        def __init__(self) -> None:
            super().__init__(None)
            self.started = asyncio.Event()
            self.cancelled = False

        async def execute(self, *, work, agent_run_id):
            del work, agent_run_id
            self.started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("取消后不应完成 Driver 动作")

    work = _work()
    queue = MemoryWorkQueue(work)
    executor = BlockingTurnExecutor()
    cancellation = ToggleCancellation()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        cancellation=cancellation,
    )

    running = asyncio.create_task(runner.run_one(task_id=work.task_id, agent_run_id="run-1"))
    await asyncio.wait_for(executor.started.wait(), timeout=1)
    cancellation.requested = True
    outcome = await asyncio.wait_for(running, timeout=2)

    assert executor.cancelled is True
    assert outcome.claimed is True
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.error_code == "WORKFLOW_CANCELED"
    assert queue.successor_commits == []


async def test_runner_cancels_timed_observation_before_publishing_successor() -> None:
    class CancelsAfterObservation(StaticTurnExecutor):
        def __init__(self, cancellation) -> None:
            super().__init__(None)
            self._cancellation = cancellation

        async def execute(self, *, work, agent_run_id):
            result = _observation_turn_result(work)
            self._cancellation.requested = True
            return result

    work = _work()
    queue = MemoryWorkQueue(work)
    cancellation = ToggleCancellation()
    continuation = RecordingContinuation()
    executor = CancelsAfterObservation(cancellation)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=continuation,
        lease_owner="app-explorer-1",
        cancellation=cancellation,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.claimed is True
    assert outcome.final_status is ExplorerWorkStatus.CANCELLED
    assert outcome.error_code == "WORKFLOW_CANCELED"
    assert continuation.observation_commands == []
    assert continuation.commands == []
    assert queue.successor_commits == []


async def test_runner_blocks_probe_before_claim_when_policy_is_disabled() -> None:
    queue = MemoryWorkQueue(_work())
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaticTurnExecutor(None),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
    )

    with pytest.raises(SupplementalEvidenceDisabledError) as error:
        await runner.run_one(
            task_id="task-1",
            agent_run_id="run-1",
            work_type=ExplorerWorkType.PROBE,
        )

    assert error.value.operation.value == "claim_probe"
    assert queue.claims == []


async def test_runner_fails_closed_when_after_capture_cannot_be_understood() -> None:
    class UnderstandingFailure(RuntimeError):
        error_code = "VISUAL_UNDERSTANDING_FAILED"

    class FailedContinuation(RecordingContinuation):
        async def prepare_after_action(self, command):
            self.commands.append(command)
            raise UnderstandingFailure("视觉 Provider 不可用")

    work = _work()
    queue = MemoryWorkQueue(work)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=FailedContinuation(),
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == "VISUAL_UNDERSTANDING_FAILED"
    assert outcome.successor_work_id is None
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.FAILED


async def test_runner_does_not_retry_before_capture_visual_failure() -> None:
    class FailedContinuation(RecordingContinuation):
        async def prepare_after_action(self, command):
            self.commands.append(command)
            raise AppBeforeActionVisualUnderstandingError(capture_id="capture-before")

    work = _work()
    queue = MemoryWorkQueue(work)
    continuation = FailedContinuation()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=StaticTurnExecutor(_turn_result(work)),
        continuation=continuation,
        lease_owner="app-explorer-1",
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.FAILED
    assert outcome.error_code == "APP_ACTION_BEFORE_VISUAL_UNDERSTANDING_FAILED"
    assert outcome.successor_work_id is None
    assert queue.transitions[-1].target_status is ExplorerWorkStatus.FAILED
    assert queue.successor_commits == []
    assert len(continuation.commands) == 1


async def test_runner_renews_exploration_lease_while_driver_call_is_in_progress() -> None:
    class SlowTurnExecutor(StaticTurnExecutor):
        async def execute(self, *, work, agent_run_id):
            await asyncio.sleep(1.05)
            return await super().execute(work=work, agent_run_id=agent_run_id)

    work = _work()
    queue = MemoryWorkQueue(work)
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=SlowTurnExecutor(_turn_result(work)),
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        lease_seconds=1,
    )

    outcome = await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert outcome.final_status is ExplorerWorkStatus.SUCCEEDED
    assert len(queue.renewals) >= 2


async def test_runner_cancels_driver_flow_when_background_renewal_loses_lease() -> None:
    class LosingQueue(MemoryWorkQueue):
        async def renew(self, *, command):
            self.renewals.append(command)
            if len(self.renewals) >= 2:
                raise RuntimeError("测试模拟 Explorer Work 租约失效")

    class BlockingTurnExecutor(StaticTurnExecutor):
        def __init__(self) -> None:
            super().__init__(None)
            self.cancelled = False

        async def execute(self, *, work, agent_run_id):
            del work, agent_run_id
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            raise AssertionError("租约失效后不应继续完成 Driver 流程")

    work = _work()
    queue = LosingQueue(work)
    executor = BlockingTurnExecutor()
    runner = AppExplorationTurnRunner(
        queue=queue,
        turn_executor=executor,
        continuation=RecordingContinuation(),
        lease_owner="app-explorer-1",
        lease_seconds=1,
    )

    with pytest.raises(AppExplorationLeaseLostError):
        await runner.run_one(task_id=work.task_id, agent_run_id="run-1")

    assert executor.cancelled is True
    assert queue.successor_commits == []
    assert [item.target_status for item in queue.transitions] == [ExplorerWorkStatus.RUNNING]


@pytest.mark.parametrize(
    ("guard_type", "command_type"),
    [
        (AppExplorationLeaseGuard, RenewExplorerWorkLeaseCommand),
        (AppFrontierLeaseGuard, RenewAppFrontierLeaseCommand),
    ],
)
async def test_lease_guard_retries_transient_connection_reset_within_lease_window(
    monkeypatch: pytest.MonkeyPatch,
    guard_type,
    command_type,
) -> None:
    """单次连接重置只触发短退避，仍在有效窗口内成功续租。"""
    now = [0.0]

    async def fake_sleep(delay_seconds: float) -> None:
        now[0] += delay_seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(lease_module, "_monotonic", lambda: now[0])
    monkeypatch.setattr(lease_module, "_sleep", fake_sleep)

    class FlakyQueue(MemoryWorkQueue):
        def __init__(self) -> None:
            super().__init__(_work())
            self.recovered = asyncio.Event()

        async def renew(self, *, command):
            self.renewals.append(command)
            if len(self.renewals) == 2:
                raise ConnectionResetError("测试模拟 PostgreSQL 连接重置")
            if len(self.renewals) == 3:
                self.recovered.set()

    queue = FlakyQueue()
    if command_type is RenewExplorerWorkLeaseCommand:
        command = command_type(
            work_id="work-1",
            lease_owner="worker-1",
            lease_token="token-1",
            lease_seconds=10,
        )
    else:
        command = command_type(
            frontier_item_id="frontier-1",
            lease_owner="worker-1",
            lease_token="token-1",
            lease_seconds=10,
        )
    guard = guard_type(
        queue=queue,
        command=command,
        renew_interval_seconds=3,
    )

    await guard.start()
    await queue.recovered.wait()
    assert guard._renew_failure is None
    assert len(queue.renewals) == 3
    await guard.stop()


@pytest.mark.parametrize(
    ("guard_type", "command_type", "lost_error_type"),
    [
        (
            AppExplorationLeaseGuard,
            RenewExplorerWorkLeaseCommand,
            AppExplorationLeaseLostError,
        ),
        (
            AppFrontierLeaseGuard,
            RenewAppFrontierLeaseCommand,
            AppFrontierLeaseLostError,
        ),
    ],
)
async def test_lease_guard_fails_closed_only_after_transient_window_expires(
    monkeypatch: pytest.MonkeyPatch,
    guard_type,
    command_type,
    lost_error_type,
) -> None:
    """持续连接故障只有耗尽已知租约窗口后才触发 fail-closed。"""
    now = [0.0]

    async def fake_sleep(delay_seconds: float) -> None:
        now[0] += delay_seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(lease_module, "_monotonic", lambda: now[0])
    monkeypatch.setattr(lease_module, "_sleep", fake_sleep)

    class UnavailableQueue(MemoryWorkQueue):
        def __init__(self) -> None:
            super().__init__(_work())

        async def renew(self, *, command):
            self.renewals.append(command)
            if len(self.renewals) >= 2:
                raise TimeoutError("测试模拟 PostgreSQL pool reset")

    queue = UnavailableQueue()
    if command_type is RenewExplorerWorkLeaseCommand:
        command = command_type(
            work_id="work-1",
            lease_owner="worker-1",
            lease_token="token-1",
            lease_seconds=2,
        )
    else:
        command = command_type(
            frontier_item_id="frontier-1",
            lease_owner="worker-1",
            lease_token="token-1",
            lease_seconds=2,
        )
    guard = guard_type(
        queue=queue,
        command=command,
        renew_interval_seconds=1,
    )

    await guard.start()
    await guard._renew_failure_event.wait()

    with pytest.raises(lost_error_type) as error:
        await guard.assert_healthy()
    assert isinstance(error.value.__cause__, lease_module._RenewalWindowExpiredError)
    assert now[0] >= 2.0
    assert len(queue.renewals) >= 3
    with pytest.raises(lost_error_type):
        await guard.stop()
