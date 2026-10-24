"""H5 V2 Explorer 独立生产装配测试。"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.agents.explorer import bootstrap, maf_agent
from services.api.app.agents.explorer.maf_agent import _candidate_rejection_reason
from services.api.app.agents.skills import loader
from services.api.app.domain.exploration import (
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


def test_default_h5_explorer_excludes_app_workflow_skills(monkeypatch) -> None:
    """H5 构建入口不能把 App 脚本课纲带入 Web 探索决策。"""
    captured: dict[str, object] = {}

    class SpyExplorer:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(bootstrap, "MafExplorerAgent", SpyExplorer)
    monkeypatch.setattr(bootstrap, "build_chat_client", lambda _settings, **_kwargs: object())
    bootstrap.build_default_h5_explorer_agent.cache_clear()

    bootstrap.build_default_h5_explorer_agent()

    assert captured["required_workflow_skills"] == ("h5-explorer-workflow",)
    assert "system_instructions_builder" in captured
    assert (
        captured["system_instructions_builder"].__module__
        == "services.api.app.agents.explorer.h5_instructions"
    )
    providers = captured["context_providers"]
    assert isinstance(providers, list)
    assert len(providers) == 1
    assert getattr(providers[0], "source_id", None) == "h5-explorer-workflow"


@pytest.mark.asyncio
async def test_h5_provider_loads_only_h5_exploration_curriculum() -> None:
    provider = loader.build_h5_explorer_context_provider()

    workflow = await provider.load_skill("h5-explorer-workflow")

    assert "ActionIntent" in workflow
    assert "ExplorationTerminal" in workflow
    assert "动态 Frontier" in workflow
    for forbidden_skill in (
        "page-exploration",
        "navigation-boundary-skill",
        "app-workflow",
    ):
        with pytest.raises(ValueError, match="不存在"):
            await provider.load_skill(forbidden_skill)


def test_h5_workflow_skill_validation_rejects_missing_agent_boundary() -> None:
    invalid = f"""---
name: h5-explorer-workflow
metadata:
  version: '{loader.H5_EXPLORER_WORKFLOW_SKILL_VERSION}'
---
# H5 Explorer
"""

    error_type = loader.H5ExplorerWorkflowSkillConfigurationError
    with pytest.raises(error_type, match="缺少强制边界"):
        loader.validate_h5_explorer_workflow_skill_content(invalid)


def test_h5_workflow_skill_validation_rejects_inspection_semantics() -> None:
    skill_path = loader._SKILLS_ROOT / "h5-explorer-workflow" / "SKILL.md"
    invalid = skill_path.read_text(encoding="utf-8") + "\nProbeWork\n"

    error_type = loader.H5ExplorerWorkflowSkillConfigurationError
    with pytest.raises(error_type, match="包含非探索语义"):
        loader.validate_h5_explorer_workflow_skill_content(invalid)


def test_h5_workflow_skill_covers_runtime_decision_discipline() -> None:
    skill_path = loader._SKILLS_ROOT / "h5-explorer-workflow" / "SKILL.md"
    content = loader.validate_h5_explorer_workflow_skill_content(
        skill_path.read_text(encoding="utf-8")
    )

    for marker in (
        "source_capture_id",
        "frontier_snapshot_id",
        "state_version",
        "平台签发",
        "长页面",
        "最上层弹窗",
        "外部页面",
    ):
        assert marker in content


def test_h5_explorer_rejects_blank_navigation_destination() -> None:
    now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    candidate = CandidateAction(
        candidate_id="candidate-navigation",
        semantic_target="商品页",
        action_type=InteractionOperation.NAVIGATE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        metadata={"destination_url": "   "},
    )
    frontier = FrontierSnapshot(
        snapshot_id="frontier-navigation",
        observation=SurfaceObservation(
            capture_id="capture-navigation",
            surface_kind=SurfaceKind.WEB,
            location="https://example.test/shop",
            state_version="state-navigation",
            observed_at=now,
        ),
        candidates=(candidate,),
    )
    work = build_exploration_work(
        task_id="task-navigation",
        session_id="session-navigation",
        branch_id="branch-navigation",
        objective="探索商品页",
        surface_kind=SurfaceKind.WEB,
        source_capture_id="capture-navigation",
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("example.test",),
            allowed_operations=(InteractionOperation.NAVIGATE,),
            max_navigation_depth=2,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
            allowed_route_prefixes=("/shop",),
        ),
        budget=ExplorationBudget(
            max_actions=2,
            remaining_actions=2,
            max_depth=2,
            remaining_duration_ms=60_000,
        ),
        deadline=now + timedelta(minutes=5),
    )

    reason = _candidate_rejection_reason(
        work,
        candidate,
        blocked_ids=set(),
        visited_candidate_ids=set(),
        visited_action_fingerprints=set(),
    )

    assert reason == "h5_navigation_missing_url: navigate 候选缺少平台锁定的 destination_url"


def test_app_navigation_keeps_non_h5_destination_boundary(monkeypatch) -> None:
    now = datetime(2026, 8, 12, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(
        maf_agent,
        "_APP_EXPLORATION_OPERATIONS",
        frozenset({InteractionOperation.NAVIGATE}),
    )
    candidate = CandidateAction(
        candidate_id="candidate-app-navigation",
        semantic_target="商品详情",
        action_type=InteractionOperation.NAVIGATE,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=80,
        navigation_depth=1,
        metadata={"destination_url": "app://com.example.target/.ProductActivity"},
    )
    frontier = FrontierSnapshot(
        snapshot_id="frontier-app-navigation",
        observation=SurfaceObservation(
            capture_id="capture-app-navigation",
            surface_kind=SurfaceKind.ANDROID,
            location="app://com.example.target/.MainActivity",
            state_version="state-app-navigation",
            observed_at=now,
        ),
        candidates=(candidate,),
    )
    work = build_exploration_work(
        task_id="task-app-navigation",
        session_id="session-app-navigation",
        branch_id="branch-app-navigation",
        objective="探索商品详情",
        surface_kind=SurfaceKind.ANDROID,
        source_capture_id="capture-app-navigation",
        frontier_snapshot_id=frontier.snapshot_id,
        frontier=frontier,
        boundaries=ExplorationBoundaries(
            allowed_hosts=("com.example.target",),
            allowed_operations=(InteractionOperation.NAVIGATE,),
            max_navigation_depth=2,
            side_effect_policy=SideEffectPolicy.READ_ONLY,
        ),
        budget=ExplorationBudget(
            max_actions=2,
            remaining_actions=2,
            max_depth=2,
            remaining_duration_ms=60_000,
        ),
        deadline=now + timedelta(minutes=5),
    )

    reason = _candidate_rejection_reason(
        work,
        candidate,
        blocked_ids=set(),
        visited_candidate_ids=set(),
        visited_action_fingerprints=set(),
    )

    assert reason is None
