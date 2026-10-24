"""App 探索确定性终止评估合同。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.domain.exploration import (
    AppExplorationGoal,
    AppExplorationOutcome,
    AppExplorationTerminalEvaluator,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
    ExplorationTerminal,
    ExplorationTerminalStatus,
    FrontierSnapshot,
    SideEffectPolicy,
    SurfaceObservation,
    build_exploration_work,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.app.domain.page.registry import get_default_page_type_registry

NOW = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


def _candidate() -> CandidateAction:
    return CandidateAction(
        candidate_id="candidate-1",
        semantic_target="进入详情",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
    )


def _work(
    *,
    candidates: tuple[CandidateAction, ...] = (),
    visited_state_ids: tuple[str, ...] = ("state-0",),
    repeated_state_count: int = 0,
    remaining_actions: int = 3,
    page_state: str = "stable",
    page_type: str | None = None,
    metadata: dict[str, object] | None = None,
    app_goal: AppExplorationGoal | None = None,
    deadline: datetime = NOW + timedelta(minutes=1),
):
    observation = SurfaceObservation(
        capture_id="capture-1",
        surface_kind=SurfaceKind.ANDROID,
        location="app://com.example/.MainActivity",
        state_version="state-1",
        observed_at=NOW,
        page_type=page_type,
        page_state=page_state,
        metadata={"screenshot_artifact_id": "artifact-1", **(metadata or {})},
    )
    frontier = FrontierSnapshot(
        snapshot_id="frontier-1",
        observation=observation,
        candidates=candidates,
        visited_state_ids=visited_state_ids,
        repeated_state_count=repeated_state_count,
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-1",
        objective="覆盖关键页面",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id=observation.capture_id,
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example",),
            allowed_operations=(InteractionOperation.CLICK,),
            max_navigation_depth=3,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=3,
            remaining_actions=remaining_actions,
            max_depth=3,
            remaining_duration_ms=30_000,
        ),
        deadline=deadline,
        app_goal=app_goal,
    )


def _goal(
    *,
    required_page_types: tuple[str, ...] = ("home", "content_detail"),
    required_milestones: tuple[str, ...] = ("journey:login", "surface:stable"),
    minimum_business_pages: int = 2,
) -> AppExplorationGoal:
    return AppExplorationGoal(
        required_page_types=required_page_types,
        required_milestones=required_milestones,
        minimum_business_pages=minimum_business_pages,
        allowed_packages=("com.example",),
    )


def _covered_metadata(
    *,
    page_types: tuple[str, ...] = ("home", "content_detail"),
    milestones: tuple[str, ...] = ("journey:login", "surface:stable"),
    covered_business_page_count: int = 2,
) -> dict[str, object]:
    return {
        "covered_page_types": page_types,
        "covered_milestones": milestones,
        "covered_business_page_count": covered_business_page_count,
        "pending_action_count": 0,
        "pending_evidence_count": 0,
        "pending_facts_available": True,
    }


def _dynamic_covered_metadata(
    *,
    page_types: tuple[str, ...] = ("home", "content_detail"),
    covered_business_page_count: int = 2,
    global_frontier_exhausted: bool = True,
) -> dict[str, object]:
    metadata = _covered_metadata(
        page_types=page_types,
        milestones=(),
        covered_business_page_count=covered_business_page_count,
    )
    metadata["app_runtime_policy"] = {
        "app_target_snapshot": {
            "completion_mode": "dynamic_discovery",
            "requires_real_visual": True,
        }
    }
    metadata["app_global_frontier"] = {
        "exhausted": global_frontier_exhausted,
        "nonterminal_count": 0 if global_frontier_exhausted else 1,
    }
    return metadata


def test_frontier_exhaustion_with_minimum_coverage_completes() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.terminal.reason_code == "exploration_completed"
    assert evaluation.coverage.unique_state_count == 2


def test_dynamic_unobserved_hard_page_type_does_not_block_after_scope_verification() -> None:
    """动态模式只闭合本轮观察范围，未见硬页型必须单独记为 not_observed。"""

    goal = AppExplorationGoal(
        required_page_types=("home", "profile", "payment"),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
        allowed_packages=("com.example",),
    )
    metadata = {
        "covered_page_types": ["home", "profile"],
        "blocked_page_types": [],
        "covered_milestones": ["surface:stable"],
        "blocked_milestones": [],
        "covered_business_page_count": 2,
        "pending_facts_available": True,
        "pending_action_count": 0,
        "pending_evidence_count": 0,
        "screenshot_artifact_id": "artifact-1",
        "degraded_provider_facts": [],
        "discovered_page_type_candidates": [
            {
                "page_type": "profile",
                "source_capture_id": "capture-1",
                "screenshot_artifact_id": "artifact-1",
                "candidate_id": "c-profile",
            }
        ],
        "app_runtime_policy": {
            "app_target_snapshot": {"completion_mode": "dynamic_discovery"},
            "app_global_frontier": {"exhausted": True, "nonterminal_count": 0},
        },
    }
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=goal, metadata=metadata),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.terminal.reason_code == "exploration_completed"
    assert evaluation.coverage.assessment_scope_complete is True
    assert evaluation.coverage.coverage_complete is True
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["assessment_scope_kind"] == "observed_frontier"
    assert applicability["not_applicable_page_types"] == []
    assert applicability["not_observed_page_types"] == ["payment"]
    assert applicability["unresolved_applicability_page_types"] == []
    assert applicability["applicable_page_types"] == ["home", "profile"]
    assert applicability["coverage_rate"] == 1.0


@pytest.mark.parametrize("pending_work_count", [0, 1])
def test_frozen_unapplied_rules_block_completion_and_wait_for_current_inspection(
    pending_work_count: int,
) -> None:
    metadata = _dynamic_covered_metadata(page_types=("home", "content_detail"))
    metadata["covered_milestones"] = ["surface:stable"]
    metadata["app_rule_inventory_coverage"] = {
        "protocol_version": "app-rule-coverage-facts.v1",
        "pending_rule_count": 1,
        "pending_inspection_work_count": pending_work_count,
        "pending_rules": [{"rule_code": "payment.rule", "target_page_types": ["payment"]}],
    }
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(required_milestones=("surface:stable",)), metadata=metadata),
        eligible_candidate_count=0,
    )
    assert evaluation.outcome is not AppExplorationOutcome.EXPLORATION_COMPLETED
    if pending_work_count:
        assert evaluation.terminal is None
    else:
        assert evaluation.reason_code == "rule_coverage_incomplete"
    assert evaluation.coverage.to_dict()["app_rule_inventory_coverage"]["pending_rule_count"] == 1


def test_rule_gap_with_safe_candidate_continues_instead_of_terminating() -> None:
    metadata = _dynamic_covered_metadata(global_frontier_exhausted=False)
    metadata["covered_milestones"] = ["surface:stable"]
    metadata["app_rule_inventory_coverage"] = {"pending_rule_count": 1}
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=_goal(required_milestones=("surface:stable",)),
            metadata=metadata,
            candidates=(_candidate(),),
        ),
        eligible_candidate_count=1,
    )
    assert evaluation.terminal is None


@pytest.mark.parametrize(
    ("page_type", "attempt", "blocked_page_types"),
    [
        (
            "payment",
            {
                "page_type": "payment",
                "source_page_type": "profile",
                "outcome": "capability_blocked",
                "blocker_code": "requires_auth",
            },
            ("payment",),
        ),
        (
            "payment",
            {
                "page_type": "payment",
                "source_page_type": "profile",
                "outcome": "entry_seen",
            },
            (),
        ),
        (
            "profile",
            {
                "page_type": "profile",
                "source_page_type": "home",
                "outcome": "entry_seen",
            },
            (),
        ),
    ],
)
def test_dynamic_known_uncovered_page_type_blocks_completion(
    page_type: str,
    attempt: dict[str, str],
    blocked_page_types: tuple[str, ...],
) -> None:
    """适用性已确定也不等于页型已覆盖，known_uncovered 必须阻止终态。"""

    goal = AppExplorationGoal(
        required_page_types=("home", page_type),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
        allowed_packages=("com.example",),
    )
    metadata = {
        "covered_page_types": ["home"],
        "blocked_page_types": list(blocked_page_types),
        "covered_milestones": ["surface:stable"],
        "blocked_milestones": [],
        "covered_business_page_count": 1,
        "pending_facts_available": True,
        "pending_action_count": 0,
        "pending_evidence_count": 0,
        "screenshot_artifact_id": "artifact-1",
        "degraded_provider_facts": [],
        "directed_discovery_attempts": [attempt],
        "app_runtime_policy": {
            "app_target_snapshot": {"completion_mode": "dynamic_discovery"},
            "app_global_frontier": {"exhausted": True, "nonterminal_count": 0},
        },
    }
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=goal, metadata=metadata),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "dynamic_page_coverage_incomplete"
    assert evaluation.coverage.assessment_scope_complete is True
    assert evaluation.coverage.dynamic_page_coverage_complete is False
    assert evaluation.coverage.to_dict()["coverage_complete"] is False
    assert evaluation.coverage.to_dict()["page_type_applicability"][
        "known_uncovered_page_types"
    ] == [page_type]


def test_goal_coverage_and_frontier_exhaustion_are_required_for_completed() -> None:
    goal = _goal()
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_covered_metadata(),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.coverage.frontier_exhausted is True
    assert evaluation.coverage.required_page_types == goal.required_page_types
    assert evaluation.coverage.required_milestones == goal.required_milestones
    assert evaluation.coverage.minimum_business_pages == goal.minimum_business_pages
    assert evaluation.coverage.covered_business_page_count >= goal.minimum_business_pages
    assert evaluation.coverage.missing_page_types == ()
    assert evaluation.coverage.missing_milestones == ()


def test_dynamic_discovery_ignores_unobserved_catalog_pages_after_global_exhaustion() -> None:
    goal = _goal(
        required_page_types=("home", "content_detail", "profile"),
        required_milestones=("surface:stable_business_page",),
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_dynamic_covered_metadata(),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.coverage.completion_mode == "dynamic_discovery"
    assert evaluation.coverage.missing_page_types == ("profile",)
    assert evaluation.coverage.minimum_coverage_reached is True
    assert evaluation.coverage.frontier_exhausted is True
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["assessment_scope_complete"] is True
    assert applicability["not_applicable_page_types"] == []
    assert applicability["not_observed_page_types"] == ["profile"]
    assert applicability["unresolved_applicability_page_types"] == []
    assert applicability["applicable_page_types"] == ["home", "content_detail"]


def test_dynamic_discovery_completes_after_auth_branch_when_business_page_was_reached() -> None:
    goal = _goal(
        required_page_types=("home", "content_list", "profile"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
    )
    metadata = _dynamic_covered_metadata(
        page_types=("home", "content_list", "profile"),
        covered_business_page_count=3,
    )
    metadata["covered_milestones"] = ["surface:stable_business_page"]

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=metadata,
            page_state="login_required",
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.coverage.stable_page_reached is True
    assert evaluation.coverage.covered_business_page_count == 3


def test_explicit_targets_mark_stable_when_business_pages_already_covered() -> None:
    goal = _goal(
        required_page_types=("home", "video_list", "video_detail"),
        minimum_business_pages=2,
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata={
                "covered_page_types": ["home", "video_list"],
                "covered_business_page_count": 2,
                "screenshot_artifact_id": "artifact-1",
            },
            page_state="startup_ad",
            page_type="startup_ad",
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.coverage.stable_page_reached is True


def test_dynamic_discovery_requires_global_frontier_exhaustion() -> None:
    goal = _goal(
        required_page_types=("home", "content_detail", "profile"),
        required_milestones=("surface:stable_business_page",),
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_dynamic_covered_metadata(global_frontier_exhausted=False),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is None
    assert evaluation.terminal is None
    assert evaluation.coverage.minimum_coverage_reached is True
    assert evaluation.coverage.frontier_exhausted is False
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["assessment_scope_verified"] is False
    assert applicability["not_observed_page_types"] == []
    assert applicability["unresolved_applicability_page_types"] == ["profile"]


def test_dynamic_discovery_plateau_does_not_discard_pending_frontier() -> None:
    goal = _goal(
        required_page_types=("home", "content_detail", "profile"),
        required_milestones=("surface:stable_business_page",),
    )
    metadata = _dynamic_covered_metadata(global_frontier_exhausted=False)
    metadata["dynamic_no_new_business_page_action_count"] = 4

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            candidates=(_candidate(),),
            app_goal=goal,
            metadata=metadata,
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is None
    assert evaluation.coverage.frontier_exhausted is False
    assert evaluation.coverage.dynamic_frontier_plateau_reached is True
    assert evaluation.terminal is None


def test_dynamic_discovery_can_complete_single_page_tool_app() -> None:
    goal = _goal(
        required_page_types=("home", "content_detail", "profile"),
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_dynamic_covered_metadata(
                page_types=("home",),
                covered_business_page_count=1,
            ),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.coverage.covered_business_page_count == 1
    assert evaluation.coverage.missing_page_types == ("content_detail", "profile")
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["not_observed_page_types"] == ["content_detail", "profile"]
    assert applicability["unresolved_applicability_page_types"] == []


def test_dynamic_discovery_completes_with_only_home_from_full_catalog() -> None:
    """完整 Catalog 不是动态模式的固定产品分母。"""
    catalog = get_default_page_type_registry().coverage_required_codes
    goal = _goal(
        required_page_types=catalog,
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_dynamic_covered_metadata(
                page_types=("home",),
                covered_business_page_count=1,
            ),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["catalog_page_types"] == list(catalog)
    assert applicability["covered_page_types"] == ["home"]
    assert applicability["not_applicable_page_types"] == []
    assert applicability["not_observed_page_types"] == [
        page_type for page_type in catalog if page_type != "home"
    ]
    assert applicability["unresolved_applicability_page_types"] == []
    assert applicability["applicable_page_types"] == ["home"]
    assert applicability["coverage_rate"] == 1.0
    assert applicability["raw_catalog_coverage_rate"] == round(1 / len(catalog), 4)


def test_dynamic_no_entry_closes_one_unobserved_page_without_claiming_other_pages() -> None:
    catalog = get_default_page_type_registry().coverage_required_codes
    goal = _goal(
        required_page_types=catalog,
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
    )
    metadata = _dynamic_covered_metadata(
        page_types=("home",),
        covered_business_page_count=1,
    )
    metadata["directed_discovery_attempts"] = [
        {
            "page_type": "payment",
            "source_page_type": "profile",
            "outcome": "no_entry",
            "source_capture_id": "capture-profile",
        }
    ]
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=goal, metadata=metadata),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["not_applicable_page_types"] == ["payment"]
    assert applicability["not_observed_page_types"] == [
        page_type for page_type in catalog if page_type not in {"home", "payment"}
    ]
    assert applicability["unresolved_applicability_page_types"] == []
    assert applicability["applicable_page_types"] == ["home"]


def test_dynamic_budget_exhaustion_does_not_close_unobserved_page_types() -> None:
    catalog = get_default_page_type_registry().coverage_required_codes
    goal = _goal(
        required_page_types=catalog,
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_dynamic_covered_metadata(
                page_types=("home",),
                covered_business_page_count=1,
            ),
            remaining_actions=0,
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "assessment_scope_incomplete"
    assert evaluation.coverage.budget_exhaustion_reason == "action_budget_exhausted"
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["assessment_scope_verified"] is False
    assert applicability["not_observed_page_types"] == []
    assert applicability["unresolved_applicability_page_types"] == [
        page_type for page_type in catalog if page_type != "home"
    ]


def test_dynamic_local_frontier_not_exhausted_does_not_close_unobserved_page_types() -> None:
    catalog = get_default_page_type_registry().coverage_required_codes
    goal = _goal(
        required_page_types=catalog,
        required_milestones=("surface:stable_business_page",),
        minimum_business_pages=1,
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            candidates=(_candidate(),),
            app_goal=goal,
            metadata=_dynamic_covered_metadata(
                page_types=("home",),
                covered_business_page_count=1,
            ),
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is None
    assert evaluation.terminal is None
    assert evaluation.coverage.frontier_exhausted is False
    applicability = evaluation.coverage.to_dict()["page_type_applicability"]
    assert applicability["assessment_scope_verified"] is False
    assert applicability["not_observed_page_types"] == []
    assert applicability["unresolved_applicability_page_types"] == [
        page_type for page_type in catalog if page_type != "home"
    ]


def test_explicit_target_payment_remains_insufficient_without_coverage() -> None:
    goal = _goal(
        required_page_types=("home", "payment"),
        required_milestones=("surface:stable",),
        minimum_business_pages=1,
    )
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_covered_metadata(
                page_types=("home",),
                milestones=("surface:stable",),
                covered_business_page_count=1,
            ),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "minimum_coverage_not_met"
    assert evaluation.coverage.completion_mode == "explicit_targets"
    assert evaluation.coverage.missing_page_types == ("payment",)
    assert evaluation.coverage.coverage_complete is False


def test_real_visual_target_with_degraded_provider_cannot_complete() -> None:
    metadata = _covered_metadata()
    metadata.update(
        {
            "app_runtime_policy": {
                "app_target_snapshot": {
                    "requires_real_visual": True,
                    "target_hash": "sha256:target",
                }
            },
            "degraded_provider_facts": [
                {
                    "status": "degraded",
                    "provider_id": "llm-app-vision",
                    "error_code": "provider_call_failed",
                }
            ],
        }
    )

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.reason_code == "visual_provider_degraded"
    summary = evaluation.terminal.coverage_summary
    assert summary["coverage_complete"] is False
    assert summary["visual_provider_status"] == "degraded"
    assert summary["visual_quality_satisfied"] is False


def test_goal_cannot_complete_before_persisted_curriculum_is_complete() -> None:
    metadata = _covered_metadata()
    metadata.update(
        {
            "app_runtime_policy": {
                "protocol_version": "app-exploration-runtime-policy.v1",
                "scan_mode": "full",
                "budget_source": "agent_run.loop_guard",
            },
            "app_curriculum_progress": {
                "protocol_version": "app-curriculum-progress.v1",
                "phase_ids": ["phase-a", "phase-b"],
                "phase_cursor": 1,
                "completed_phase_ids": ["phase-a"],
                "current_phase_action_count": 0,
                "current_phase_id": "phase-b",
                "complete": False,
            },
        }
    )

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.reason_code == "curriculum_incomplete"
    assert evaluation.terminal.coverage_summary["curriculum_complete"] is False
    assert (
        evaluation.terminal.coverage_summary["app_runtime_policy"]["budget_source"]
        == "agent_run.loop_guard"
    )


def test_goal_accepts_only_self_consistent_completed_curriculum() -> None:
    metadata = _covered_metadata()
    metadata["app_curriculum_progress"] = {
        "protocol_version": "app-curriculum-progress.v1",
        "playbook_id": "default",
        "phase_ids": ["phase-a"],
        "phase_action_requirements": {"phase-a": 1},
        "phase_cursor": 1,
        "completed_phase_ids": ["phase-a"],
        "current_phase_action_count": 0,
        "current_phase_id": None,
        "complete": True,
        "required_action_count": 1,
    }

    completed = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata),
        eligible_candidate_count=0,
    )
    assert completed.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED

    metadata["app_curriculum_progress"]["phase_cursor"] = 0
    corrupted = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata),
        eligible_candidate_count=0,
    )
    assert corrupted.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert corrupted.terminal is not None
    assert corrupted.terminal.reason_code == "curriculum_incomplete"


def test_goal_cannot_complete_when_pending_facts_are_unavailable() -> None:
    metadata = _covered_metadata()
    metadata.update(
        {
            "pending_action_count": None,
            "pending_evidence_count": None,
            "pending_facts_available": False,
        }
    )

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.reason_code == "pending_facts_unavailable"
    assert evaluation.coverage.pending_facts_available is False
    assert evaluation.terminal.coverage_summary["pending_facts_available"] is False


def test_two_states_do_not_complete_when_required_business_pages_are_missing() -> None:
    goal = _goal(required_page_types=("home", "content_detail", "profile"))
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_covered_metadata(page_types=("home", "content_detail")),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.reason_code == "minimum_coverage_not_met"
    assert evaluation.coverage.missing_page_types == ("profile",)


def test_startup_ad_and_home_do_not_count_as_required_business_page_coverage() -> None:
    goal = _goal(required_page_types=("home", "content_detail"))
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_covered_metadata(
                page_types=("startup_ad", "home"),
                covered_business_page_count=1,
            ),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.coverage.missing_page_types == ("content_detail",)
    assert evaluation.coverage.covered_business_page_count == 1


def test_missing_required_milestone_is_auditable_evidence_insufficient() -> None:
    goal = _goal()
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=goal,
            metadata=_covered_metadata(milestones=("journey:login",)),
        ),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.coverage.required_milestones == goal.required_milestones
    assert evaluation.coverage.missing_milestones == ("surface:stable",)


def test_satisfied_goal_with_open_frontier_completes() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=_covered_metadata()),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.coverage.frontier_exhausted is False


def test_satisfied_goal_with_open_global_frontier_completes() -> None:
    metadata = _covered_metadata()
    metadata["app_global_frontier"] = {
        "protocol_version": "app-global-frontier-summary.v1",
        "pending": 2,
        "leased": 0,
        "running": 0,
        "retry_wait": 0,
        "visited": 1,
        "blocked": 0,
        "failed": 0,
        "cancelled": 0,
        "superseded": 0,
        "nonterminal_count": 2,
        "exhausted": False,
    }

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(app_goal=_goal(), metadata=metadata),
        eligible_candidate_count=0,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.coverage.frontier_exhausted is False
    assert evaluation.coverage.runtime_policy_summary["app_global_frontier"]["pending"] == 2


def test_budget_exhaustion_with_satisfied_goal_still_completes() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=_goal(),
            metadata=_covered_metadata(),
            remaining_actions=0,
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.coverage.frontier_exhausted is False
    assert evaluation.coverage.required_page_types == _goal().required_page_types
    assert evaluation.coverage.missing_page_types == ()


def test_deadline_exhaustion_with_satisfied_goal_still_completes() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW + timedelta(minutes=2)).evaluate(
        _work(
            app_goal=_goal(),
            metadata=_covered_metadata(),
            deadline=NOW + timedelta(minutes=1),
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.coverage.required_milestones == _goal().required_milestones
    assert evaluation.coverage.missing_milestones == ()
    assert evaluation.terminal.coverage_summary["budget_exhaustion_reason"] == ("deadline_reached")


def test_loop_with_goal_gap_preserves_auditable_coverage_summary() -> None:
    goal = _goal(required_page_types=("home", "content_detail", "profile"))
    evaluation = AppExplorationTerminalEvaluator(
        now=lambda: NOW,
        repeated_state_threshold=3,
    ).evaluate(
        _work(
            app_goal=goal,
            metadata=_covered_metadata(page_types=("home", "content_detail")),
            candidates=(_candidate(),),
            repeated_state_count=3,
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "loop_detected"
    assert evaluation.coverage.missing_page_types == ("profile",)


def test_budget_exhaustion_below_minimum_coverage_is_evidence_insufficient() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(visited_state_ids=(), remaining_actions=0),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.BUDGET_EXHAUSTED
    assert evaluation.terminal.reason_code == "minimum_coverage_not_met"
    assert evaluation.terminal.coverage_summary["budget_exhaustion_reason"] == (
        "action_budget_exhausted"
    )


def test_repeated_state_threshold_stops_as_loop_detected() -> None:
    evaluation = AppExplorationTerminalEvaluator(
        now=lambda: NOW,
        repeated_state_threshold=3,
    ).evaluate(
        _work(candidates=(_candidate(),), repeated_state_count=3),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "loop_detected"


def test_dynamic_repeated_business_page_plateau_stops_before_deadline() -> None:
    metadata = _dynamic_covered_metadata(global_frontier_exhausted=False)
    metadata["dynamic_repeated_business_page_action_count"] = 3
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            app_goal=_goal(),
            candidates=(_candidate(),),
            metadata=metadata,
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "app_repeated_business_page_plateau"
    assert evaluation.coverage.frontier_exhausted is False
    assert evaluation.terminal.coverage_summary["repeated_business_page_plateau_reached"] is True


def test_business_progress_stall_without_trusted_controls_fail_closed() -> None:
    evaluation = AppExplorationTerminalEvaluator(
        now=lambda: NOW,
        business_stall_unique_states=4,
    ).evaluate(
        _work(
            app_goal=_goal(),
            candidates=(_candidate(),),
            visited_state_ids=("s0", "s1", "s2"),
            metadata={
                "covered_page_types": (),
                "covered_business_page_count": 0,
                "pending_action_count": 0,
                "pending_evidence_count": 0,
                "pending_facts_available": True,
                "surface_facts": {},
            },
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "app_business_progress_stalled"


def test_business_progress_stall_skips_when_trusted_navigation_present() -> None:
    evaluation = AppExplorationTerminalEvaluator(
        now=lambda: NOW,
        business_stall_unique_states=4,
    ).evaluate(
        _work(
            app_goal=_goal(),
            candidates=(_candidate(),),
            visited_state_ids=("s0", "s1", "s2"),
            metadata={
                "covered_page_types": (),
                "covered_business_page_count": 0,
                "pending_action_count": 0,
                "pending_evidence_count": 0,
                "pending_facts_available": True,
                "surface_facts": {
                    "trusted_accessibility_controls": {
                        "bottom_navigation_count": 6,
                        "content_card_count": 0,
                        "overlay_close": False,
                    }
                },
            },
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.terminal is None


def test_business_progress_stall_skips_while_home_popup_or_coverage_seek() -> None:
    seek = CandidateAction(
        candidate_id="candidate-seek",
        semantic_target="覆盖寻页：底栏「片库」",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=98,
        navigation_depth=0,
        metadata={"coverage_page_seek": True, "expected_next_page_type": "video_list"},
    )
    evaluation = AppExplorationTerminalEvaluator(
        now=lambda: NOW,
        business_stall_unique_states=4,
    ).evaluate(
        _work(
            app_goal=_goal(),
            candidates=(seek,),
            visited_state_ids=("s0", "s1", "s2"),
            page_type="home",
            page_state="popup",
            metadata={
                "covered_page_types": (),
                "covered_business_page_count": 0,
                "pending_action_count": 0,
                "pending_evidence_count": 0,
                "pending_facts_available": True,
                "dynamic_home_popup_frame_count": 2,
                "surface_facts": {"popup_visible": True},
            },
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.terminal is None


def test_llm_terminal_is_only_input_while_candidates_remain() -> None:
    work = _work(candidates=(_candidate(),))
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="模型认为完成",
    )

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        work,
        eligible_candidate_count=1,
        proposed_terminal=proposed,
    )

    assert evaluation.terminal is None
    assert evaluation.outcome is None


def test_satisfied_goal_completes_independently_of_llm_terminal_and_open_frontier() -> None:
    work = _work(
        app_goal=_goal(),
        metadata=_covered_metadata(),
        candidates=(_candidate(),),
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="模型认为完成",
    )

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        work,
        eligible_candidate_count=1,
        proposed_terminal=proposed,
    )

    assert evaluation.outcome is AppExplorationOutcome.EXPLORATION_COMPLETED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.COMPLETED
    assert evaluation.coverage.frontier_exhausted is False


def test_llm_terminal_cannot_bypass_missing_goal_when_frontier_is_empty() -> None:
    work = _work(
        app_goal=_goal(required_page_types=("home", "content_detail", "profile")),
        metadata=_covered_metadata(page_types=("home", "content_detail")),
    )
    proposed = ExplorationTerminal(
        work_id=work.work_id,
        task_id=work.task_id,
        status=ExplorationTerminalStatus.COMPLETED,
        reason="模型认为完成",
    )

    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        work,
        eligible_candidate_count=0,
        proposed_terminal=proposed,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is not ExplorationTerminalStatus.COMPLETED
    assert evaluation.coverage.missing_page_types == ("profile",)


def test_technical_error_is_not_mixed_with_business_outcome() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(),
        eligible_candidate_count=0,
        technical_error_code="SESSION_LEASE_LOST",
    )

    assert evaluation.outcome is AppExplorationOutcome.TECHNICAL_FAILED
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.BLOCKED
    assert evaluation.terminal.reason_code == "SESSION_LEASE_LOST"


def test_external_return_failure_threshold_stops_with_explicit_reason() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            candidates=(_candidate(),),
            metadata={
                "external_return_failure_count": 2,
                "external_activity_kind": "browser",
            },
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.status is ExplorationTerminalStatus.NO_PROGRESS
    assert evaluation.terminal.reason_code == "app_external_return_failed"
    assert evaluation.coverage.external_return_failure_count == 2


def test_external_restore_loop_threshold_stops_with_explicit_reason() -> None:
    evaluation = AppExplorationTerminalEvaluator(now=lambda: NOW).evaluate(
        _work(
            candidates=(_candidate(),),
            metadata={
                "external_activity_bounce_count": 3,
                "external_activity_kind": "store",
            },
        ),
        eligible_candidate_count=1,
    )

    assert evaluation.outcome is AppExplorationOutcome.EVIDENCE_INSUFFICIENT
    assert evaluation.terminal is not None
    assert evaluation.terminal.reason_code == "app_external_restore_loop"
    assert evaluation.coverage.external_activity_bounce_count == 3
