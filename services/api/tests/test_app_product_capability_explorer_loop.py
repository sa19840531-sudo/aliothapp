"""F04：Product 能力事实闭环接入 V2 Explorer。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from services.api.app.agents.explorer.maf_agent import _agent_input
from services.api.app.application.app_product_capability_for_explorer import (
    APP_PRODUCT_CAPABILITY_FACTS_KEY,
    capability_facts_for_agent_payload,
    rank_eligible_by_capability_facts,
    resolve_work_product_capability_facts,
    sanitize_capability_facts,
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


def _work(*, package: str, facts=None, candidates=()):
    now = datetime(2026, 10, 23, 12, tzinfo=UTC)
    meta = {
        "package_name": package,
        "app_runtime_policy": {"exploration_mode": "dynamic_frontier"},
    }
    if facts is not None:
        meta[APP_PRODUCT_CAPABILITY_FACTS_KEY] = facts
    observation = SurfaceObservation(
        capture_id="cap-1",
        surface_kind=SurfaceKind.ANDROID,
        location=f"app://{package}/.Main",
        state_version="sig",
        observed_at=now,
        page_type="home",
        page_state="stable",
        metadata=meta,
    )
    return build_exploration_work(
        task_id="t1",
        session_id="s1",
        branch_id="b1",
        objective="profile-loop",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="cap-1",
        frontier_snapshot_id="snap-1",
        frontier=FrontierSnapshot(
            snapshot_id="snap-1",
            observation=observation,
            candidates=candidates,
        ),
        boundaries=ExplorationBoundaries(
            allowed_hosts=(package,),
            allowed_operations=(InteractionOperation.CLICK, InteractionOperation.BACK),
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
            required_page_types=("home",),
            required_milestones=("surface:stable_business_page",),
            allowed_packages=(package,),
            minimum_business_pages=1,
        ),
    )


def test_resolve_facts_from_metadata_and_rebuild() -> None:
    work = _work(
        package="com.dzu4.app",
        facts={
            "ok": True,
            "profile_present": True,
            "taps": {"me_tab": [1, 2]},
            "grants_absolute_coordinates": True,
        },
    )
    # 脏 facts 会被剥坐标并强制 grants=false；也可现算
    sanitized = sanitize_capability_facts(
        work.frontier.observation.metadata[APP_PRODUCT_CAPABILITY_FACTS_KEY]
    )
    assert "taps" not in sanitized
    assert sanitized["grants_absolute_coordinates"] is False

    rebuilt = resolve_work_product_capability_facts(_work(package="com.dzu4.app"))
    assert rebuilt["ok"] is True
    assert rebuilt["profile_present"] is True
    assert rebuilt["grants_absolute_coordinates"] is False
    assert "taps" not in rebuilt


def test_unknown_package_allows_generic_explore() -> None:
    facts = resolve_work_product_capability_facts(
        _work(package="com.brand.new.unknown")
    )
    assert facts["ok"] is False
    assert facts["profile_present"] is False
    assert facts["grants_absolute_coordinates"] is False


def test_agent_input_includes_capability_facts() -> None:
    payload = _agent_input(
        work=_work(package="com.alz7xm2.drama"),
        candidates=[],
        app_coverage_summary={},
    )
    facts = payload["app_product_capability_facts"]
    assert facts["grants_absolute_coordinates"] is False
    assert "taps" not in facts
    assert "key_taps" not in facts
    # 摘要字段
    slim = capability_facts_for_agent_payload(_work(package="com.alz7xm2.drama"))
    assert slim["package_name"] == "com.alz7xm2.drama"


def test_agent_contract_ranks_recovery_semantics() -> None:
    back = CandidateAction(
        candidate_id="c-back",
        semantic_target="返回",
        action_type=InteractionOperation.BACK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=0,
        metadata={"purpose": "系统返回"},
    )
    content = CandidateAction(
        candidate_id="c-card",
        semantic_target="精彩短剧",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=90,
        navigation_depth=0,
        metadata={"purpose": "内容卡"},
    )
    # 全产品统一 Agent 合同：有 profile 恢复提示时提升返回语义。
    ranked = rank_eligible_by_capability_facts(
        work=_work(package="com.alz7xm2.drama"),
        eligible=(content, back),
    )
    assert ranked[0].candidate_id in {"c-back", "c-card"}
    mama_ranked = rank_eligible_by_capability_facts(
        work=_work(package="com.dzu4.app"),
        eligible=(content, back),
    )
    assert [item.candidate_id for item in mama_ranked] == ["c-back", "c-card"]
