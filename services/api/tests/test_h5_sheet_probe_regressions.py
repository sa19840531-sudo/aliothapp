"""问题表真实失败路径的合同复现；内存 DOM 是最小反例，不冒充历史截图。

当前卡点 gid=391083483：134 行首页误判官方群；91 行公告父子及屏外加载；
100/105 行只观察不滚动；17/58/123 行封面、全屏、充值缺动作配对。
来源任务 8a9a36db、713437c2，保留原始证据，不重跑业务任务。
"""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.application.aggregate_inspection_execution import evaluate_aggregate_context
from services.api.app.application.h5_exploration_observation import (
    _list_scroll_coverage_action_parameters,
)
from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
from services.api.app.application.rule_judge import RuleJudgeDefinition, _h5_gap_interaction
from services.api.app.domain.inspection import EvidenceInteractionRequirement
from services.api.app.domain.interaction import ActionIntent, InteractionOperation
from services.api.app.infrastructure.browser.page_capture import (
    _install_transient_message_monitor,
    _stop_transient_message_monitor,
)
from services.api.tests.test_aggregate_inspection_runtime import _context as _toast_context
from services.api.tests.test_aggregate_official_group_entry import _context, _packet
from services.api.tests.test_h5_probe_goal_planning import (
    NOW,
    _frontier,
    _request_like,
    _runtime_work,
)
from services.api.tests.test_h5_typed_probe_contract import _with_request


@pytest.mark.parametrize("page_type", ["home", "profile"])
def test_single_entry_page_cannot_prove_absence_of_official_group(page_type):
    result = evaluate_aggregate_context(
        _context(
            {
                page_type: _packet(
                    page_type=page_type,
                    visible_text="首页 视频 我的",
                )
            }
        )
    )
    assert result.execution_status == "evidence_insufficient"


@pytest.mark.parametrize(
    "case",
    [
        "notice",
        "offscreen",
        "missing_geometry",
        "duplicate_event",
        "duplicate_window",
        "unobserved_action",
    ],
)
def test_toast_aggregate_rejects_legacy_false_samples_and_repeated_action(case):
    context = _toast_context(style_two={"font_size": "12px"})
    message = {
        "event_id": "transient-1",
        "kind": "toast",
        "text": "保存成功",
        "platform_ui_confidence": 0.95,
        "position_zone": "bottom_center",
        "rect": {"x": 20, "y": 700, "width": 100, "height": 30},
        "viewport": {"width": 390, "height": 844},
        "style": {
            "font_size": "16px",
            "text_color": "white",
            "background_color": "black",
            "background_opacity": 0.7,
        },
    }
    if case == "notice":
        message["kind"] = "notice"
    elif case == "offscreen":
        message["rect"]["y"] = 2000
    elif case == "missing_geometry":
        message.pop("viewport")
    for packet in context.observation_packets.values():
        action = packet["facts"]["action_log"][0]
        action["action_execution_id"] = "same-real-action"
        action["transient_messages"] = [message]
        if case == "duplicate_window":
            action.pop("action_execution_id")
            message["observation_id"] = "same-observation-window"
        if case == "unobserved_action":
            action["transient_message_observation"]["status"] = "not_observed"
            action["transient_messages"].append(
                {
                    **message,
                    "event_id": "transient-2",
                    "text": "另一个反馈",
                }
            )
    if case == "unobserved_action":
        list(context.observation_packets.values())[0]["facts"]["action_log"] = [
            {"transient_message_observation": {"status": "observed"}, "transient_messages": []}
        ]
    result = evaluate_aggregate_context(context)
    assert result.execution_status == "evidence_insufficient"


@pytest.mark.parametrize(
    "case,expected_count",
    [
        ("announcement", 0),
        ("offscreen", 0),
        ("hidden_parent", 0),
        ("loading", 0),
        ("nested_toast", 1),
        ("independent_toasts", 2),
        ("error_status", 1),
        ("live_feedback", 1),
    ],
)
async def test_transient_monitor_counts_visible_message_roots_not_dom_matches(case, expected_count):
    html = {
        "announcement": '<div class="notice">公告<div class="notice-wrap">公告'
        '<span class="notice-text" style="font-size:12px">公告</span></div></div>',
        "offscreen": '<div role="status" style="top:2000px;font-size:0">加载中</div>',
        "hidden_parent": '<section style="opacity:0"><div class="toast">成功</div></section>',
        "loading": '<div role="status" class="loading">加载中...</div>',
        "nested_toast": '<div class="toast"><span class="toast-text" '
        'style="font-size:12px">保存成功</span></div>',
        "independent_toasts": '<div class="toast">保存成功</div>'
        '<div class="toast" style="top:100px">操作成功</div>',
        "error_status": '<div role="status">加载失败，请重试</div>',
        "live_feedback": '<div aria-live="polite" style="position:fixed;top:20px;'
        'background:black;padding:8px">保存成功</div>',
    }[case]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content(
                "<style>.toast,.notice,[role=status]{position:fixed;"
                "top:20px;left:20px;background:black;color:white;"
                "padding:8px;font-size:16px}</style>"
            )
            assert await _install_transient_message_monitor(page)
            await page.evaluate("html => document.body.insertAdjacentHTML('beforeend',html)", html)
            await page.wait_for_timeout(50)
            messages = await _stop_transient_message_monitor(page)
            assert len(messages) == expected_count
            if case == "nested_toast":
                assert messages[0]["style"]["font_size"] == "12px"
        finally:
            await browser.close()


async def test_production_bounded_scroll_candidate_satisfies_probe_goal_without_model():
    work = _runtime_work()
    work = _with_request(
        work,
        _request_like(
            work.evidence_request,
            required_interaction=EvidenceInteractionRequirement(
                operation=InteractionOperation.SCROLL,
                semantic_target="当前页面纵向滚动区域",
                purpose="滚动前后补证",
                parameters={"goal_capability": "vertical_scroll"},
            ),
        ),
    )
    parameters = _list_scroll_coverage_action_parameters(
        raw={"target_keywords": ["list_scroll_coverage", "bounded_to_bottom"]},
        operation=InteractionOperation.SCROLL,
        target="采集列表至底部",
        risk_text="low",
        source_page_type="video_list",
        source_page_state="stable",
    )
    assert parameters is not None
    frontier = _frontier(work)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            page_state="stable",
            page_type="video_list",
            overlay_signature=None,
        ),
        candidates=(
            replace(
                frontier.candidates[0],
                semantic_target="采集列表至底部",
                metadata={
                    "action_parameters": parameters,
                    "target_keywords": ["list_scroll_coverage", "bounded_to_bottom"],
                },
            ),
        ),
    )
    delegate = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不得调用模型")))
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    result = await H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        now=lambda: NOW,
    ).plan(work=work, baseline=baseline)
    assert isinstance(result, ActionIntent)
    assert result.parameters["scroll_max_segments"] == 8
    assert result.deadline == baseline.deadline
    assert result.capture_profile.before.role == "probe_before"
    delegate.decide_next.assert_not_awaited()


@pytest.mark.parametrize(
    "group,code,goal",
    [
        ("cover-content", "alioth_15", "cover_content_pair"),
        ("interaction-state", "alioth_46", "media_fullscreen_pair"),
        ("payment-entry", "alioth_ops_4", "payment_entry_pair"),
    ],
)
def test_interaction_gap_has_explicit_typed_action_instead_of_observe(group, code, goal):
    definition = RuleJudgeDefinition(
        rule_id=code,
        rule_code=code,
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": group}},
    )
    packet = SimpleNamespace(
        packet_payload={
            "identity": {"target_platform": "h5"},
            "page": {"surface_kind": "web", "page_type": "profile"},
        }
    )
    requirement = _h5_gap_interaction(definition, packet)
    assert requirement is not None
    assert requirement.operation is InteractionOperation.CLICK
    assert requirement.parameters["goal_capability"] == goal
