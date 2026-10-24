"""动态规则取证不被旧课纲强制切栏或阻止详情采集。"""

from dataclasses import replace
from datetime import UTC, datetime

from services.api.app.agents.explorer import MafExplorerAgent, maf_agent
from services.api.app.application.app_coverage_page_seek import (
    playbook_bottom_tabs_hard_incomplete,
)
from services.api.app.domain.exploration import AppExplorationGoal, CandidateAction
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    InteractionRiskLevel,
    SurfaceKind,
)
from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
from services.api.tests.test_maf_explorer_agent import _decision, _work


def _metadata(mode="dynamic_frontier"):
    return {
        "app_runtime_policy": {"exploration_mode": mode},
        "covered_page_types": ["home"],
        "covered_milestones": ["surface:stable_business_page"],
        "covered_business_page_count": 1,
        "pending_facts_available": True,
        "pending_action_count": 0,
        "pending_evidence_count": 0,
        "app_flow_contract": {
            "bottom_tabs_satisfied": False,
            "missing_bottom_tabs": ["社区", "我的"],
            "observed_bottom_tabs": ["主页", "社区", "我的"],
        },
    }


def _candidates():
    detail = CandidateAction(
        candidate_id="detail-evidence",
        semantic_target="进入内容详情取证",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=99,
        navigation_depth=1,
        metadata={"expected_next_page_type": "content_detail", "frontier_scope": "page"},
    )
    tab = CandidateAction(
        candidate_id="community-tab",
        semantic_target="底栏社区",
        action_type=InteractionOperation.CLICK,
        risk_level=InteractionRiskLevel.LOW,
        exploration_value=10,
        navigation_depth=0,
        metadata={
            "bottom_navigation_tab_seek": True,
            "expected_state_hint": "tab:社区",
            "normalized_bounds": {"left": 0.55, "top": 0.88, "right": 0.7, "bottom": 0.96},
        },
    )
    return detail, tab


def _dynamic_work():
    return _work(
        candidates=_candidates(),
        surface_kind=SurfaceKind.ANDROID,
        page_type="home",
        page_state="stable",
        observation_metadata=_metadata(),
        app_goal=AppExplorationGoal(
            required_page_types=("home", "content_detail"),
            required_milestones=("surface:stable_business_page",),
            minimum_business_pages=2,
            allowed_packages=("com.example.target",),
        ),
    )


def test_dynamic_policy_excludes_playbook_hard_gate_but_explicit_curriculum_keeps_it():
    assert not playbook_bottom_tabs_hard_incomplete(coverage_metadata=_metadata())
    assert playbook_bottom_tabs_hard_incomplete(coverage_metadata=_metadata("playbook_curriculum"))


async def test_model_detail_selection_is_not_rewritten_by_old_bottom_contract(monkeypatch):
    # 单独覆盖模型返回后的执行门禁；确定性路径另有下一条测试。
    monkeypatch.setattr(maf_agent, "_deterministic_app_coverage_decision", lambda **kwargs: None)
    detail, _ = _candidates()
    agent = MafExplorerAgent(
        client=ScriptedChatClient(
            [make_text_response(_decision(candidate_id=detail.candidate_id))]
        ),
        now=lambda: datetime(2026, 8, 3, 10, tzinfo=UTC),
    )
    result = await agent.decide_next(_dynamic_work())
    assert isinstance(result, ActionIntent)
    assert result.parameters["candidate_id"] == detail.candidate_id


def test_dynamic_deterministic_path_does_not_pick_fixed_next_tab():
    _, tab = _candidates()
    assert (
        maf_agent._pick_next_playbook_bottom_tab(work=_dynamic_work(), visible_bottom=(tab,))
        is None
    )


def test_dynamic_fallback_keeps_high_value_detail_candidate():
    detail, tab = _candidates()
    result = maf_agent._decide_app_fallback(
        work=_dynamic_work(), eligible=(detail, tab), error=RuntimeError("模型暂不可用")
    )
    assert isinstance(result, ActionIntent)
    assert result.parameters["candidate_id"] == detail.candidate_id


def test_dynamic_known_package_does_not_open_home_top_curriculum():
    work = _dynamic_work()
    work = replace(
        work,
        frontier=replace(
            work.frontier,
            observation=replace(
                work.frontier.observation,
                metadata={**_metadata(), "package_name": "com.hj.ttthree"},
            ),
        ),
    )
    assert not maf_agent._home_top_curriculum_still_open(work)
