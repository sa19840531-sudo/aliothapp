"""F01 App 短流程：种子可解析、驱动选点、回执推进。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from services.api.app.application.app_journey_execution_bridge import (
    attach_journey_plan_to_coverage,
    load_app_shortflow_definition_ids,
)
from services.api.app.application.app_shortflow_driver import (
    APP_SHORTFLOW_BINDING_KEY,
    APP_SHORTFLOW_PROGRESS_KEY,
    merge_shortflow_progress_from_intent_parameters,
    record_app_shortflow_receipt,
    try_app_shortflow_driver,
)
from services.api.app.domain.exploration import (
    AppExplorationGoal,
    CandidateAction,
    ExplorationBoundaries,
    ExplorationBudget,
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
from services.api.app.domain.journey import parse_journey_definition

_SEED = (
    Path(__file__).resolve().parents[3]
    / "docs"
    / "data"
    / "journey_definitions"
    / "app_shortflows_v2.json"
)


def _work(
    *,
    page_type: str,
    candidates: tuple[CandidateAction, ...] = (),
    metadata: dict | None = None,
):
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    observation = SurfaceObservation(
        capture_id="cap-1",
        surface_kind=SurfaceKind.ANDROID,
        location="app://demo/.Player",
        state_version="state-1",
        observed_at=now,
        key_elements=(),
        page_type=page_type,
        page_state="stable",
        overlay_signature=None,
        metadata={
            "package_name": "com.demo.app",
            **(metadata or {}),
        },
    )
    return build_exploration_work(
        task_id="task-1",
        session_id="session-1",
        branch_id="branch-1",
        objective="F01 shortflow",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-1",
        frontier_snapshot_id="snap-1",
        frontier=FrontierSnapshot(
            snapshot_id="snap-1",
            observation=observation,
            candidates=candidates,
        ),
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.demo.app",),
            allowed_operations=(
                InteractionOperation.CLICK,
                InteractionOperation.TAP,
                InteractionOperation.BACK,
            ),
            max_navigation_depth=4,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
            allowed_route_prefixes=("/",),
        ),
        budget=ExplorationBudget(
            max_actions=20,
            remaining_actions=20,
            max_depth=4,
            remaining_duration_ms=60_000,
        ),
        deadline=now + timedelta(minutes=10),
        app_goal=AppExplorationGoal(
            required_page_types=("home", "video_detail", "payment"),
            required_milestones=("surface:stable_business_page",),
            allowed_packages=("com.demo.app",),
            minimum_business_pages=1,
        ),
    )


def _candidate(label: str, *, candidate_id: str = "c1") -> CandidateAction:
    from services.api.app.domain.exploration import NormalizedBounds

    bounds = NormalizedBounds(0.4, 0.4, 0.6, 0.6)
    return CandidateAction(
        candidate_id=candidate_id,
        semantic_target=label,
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=1,
        expected_surface_kind=SurfaceKind.ANDROID,
        metadata={"text": label, "normalized_bounds": bounds.to_dict()},
    )


def test_app_shortflow_seeds_parse() -> None:
    raw = json.loads(_SEED.read_text(encoding="utf-8"))
    ids = {item["definition_id"] for item in raw}
    assert ids == {"app-player-pause-resume", "app-payment-display-return"}
    assert set(load_app_shortflow_definition_ids()) == ids
    for item in raw:
        definition = parse_journey_definition(item)
        assert definition.checkpoints
        assert "android" in definition.surface_kinds
        assert all(cp.side_effect_policy == "read_only" for cp in definition.checkpoints)


def test_attach_merges_player_and_payment_plans() -> None:
    coverage = attach_journey_plan_to_coverage(
        {},
        plans=(
            {
                "definition_id": "app-player-pause-resume",
                "execution_plan_id": "plan-player",
                "plan_kind": "app_shortflow",
            },
            {
                "definition_id": "app-payment-display-return",
                "execution_plan_id": "plan-pay",
                "plan_kind": "app_shortflow",
            },
        ),
    )
    assert coverage["app_shortflow_plans_ready"] is True
    assert coverage["journey_execution_plan_id"] == "plan-player"
    assert len(coverage["journey_execution_plans"]) == 2


def test_player_pause_then_resume_receipts() -> None:
    plans = [
        {
            "definition_id": "app-player-pause-resume",
            "execution_plan_id": "plan-player",
            "plan_kind": "app_shortflow",
        }
    ]
    work = _work(
        page_type="video_detail",
        candidates=(_candidate("暂停", candidate_id="pause-btn"),),
        metadata={"journey_execution_plans": plans, "app_journey_plans_ready": True},
    )
    intent = try_app_shortflow_driver(work)
    assert intent is not None
    assert intent.parameters[APP_SHORTFLOW_BINDING_KEY]["checkpoint_id"] == "pause"
    assert "禁止" not in intent.purpose or "只读" in intent.purpose

    after_pause = record_app_shortflow_receipt(
        {"journey_execution_plans": plans},
        binding=intent.parameters[APP_SHORTFLOW_BINDING_KEY],
        result_capture_id="cap-2",
        ok=True,
    )
    assert after_pause[APP_SHORTFLOW_PROGRESS_KEY]["app-player-pause-resume"][
        "next_checkpoint_id"
    ] == "resume"

    work2 = _work(
        page_type="player",
        candidates=(_candidate("播放", candidate_id="play-btn"),),
        metadata={
            "journey_execution_plans": plans,
            APP_SHORTFLOW_PROGRESS_KEY: after_pause[APP_SHORTFLOW_PROGRESS_KEY],
        },
    )
    resume = try_app_shortflow_driver(work2)
    assert resume is not None
    assert resume.parameters[APP_SHORTFLOW_BINDING_KEY]["checkpoint_id"] == "resume"
    done = record_app_shortflow_receipt(
        after_pause,
        binding=resume.parameters[APP_SHORTFLOW_BINDING_KEY],
        result_capture_id="cap-3",
        ok=True,
    )
    assert done[APP_SHORTFLOW_PROGRESS_KEY]["app-player-pause-resume"]["status"] == "succeeded"
    assert "app-player-pause-resume" in done["app_shortflow_completed"]


def test_payment_select_then_system_back_return() -> None:
    plans = [
        {
            "definition_id": "app-payment-display-return",
            "execution_plan_id": "plan-pay",
            "plan_kind": "app_shortflow",
        }
    ]
    work = _work(
        page_type="payment",
        candidates=(_candidate("连续包月", candidate_id="tier"),),
        metadata={"journey_execution_plans": plans},
    )
    select = try_app_shortflow_driver(work)
    assert select is not None
    assert select.parameters.get("forbid_confirm_pay") is True
    mid = record_app_shortflow_receipt(
        {"journey_execution_plans": plans},
        binding=select.parameters[APP_SHORTFLOW_BINDING_KEY],
        result_capture_id="cap-pay-1",
        ok=True,
    )
    work2 = _work(
        page_type="payment",
        candidates=(),
        metadata={
            "journey_execution_plans": plans,
            APP_SHORTFLOW_PROGRESS_KEY: mid[APP_SHORTFLOW_PROGRESS_KEY],
        },
    )
    back = try_app_shortflow_driver(work2)
    assert back is not None
    assert back.action_type == InteractionOperation.BACK
    assert back.parameters[APP_SHORTFLOW_BINDING_KEY]["checkpoint_id"] == "return_source"


def test_lineage_merge_writes_progress() -> None:
    prior = {"journey_execution_plans": [{"definition_id": "app-player-pause-resume"}]}
    binding = {
        "definition_id": "app-player-pause-resume",
        "execution_plan_id": "plan-player",
        "checkpoint_id": "pause",
        "purpose": "pause",
    }
    merged = merge_shortflow_progress_from_intent_parameters(
        prior_metadata=prior,
        parameters={
            "app_shortflow_driver": True,
            APP_SHORTFLOW_BINDING_KEY: binding,
        },
        result_capture_id="cap-x",
        ok=True,
    )
    assert merged[APP_SHORTFLOW_PROGRESS_KEY]["app-player-pause-resume"][
        "next_checkpoint_id"
    ] == "resume"
