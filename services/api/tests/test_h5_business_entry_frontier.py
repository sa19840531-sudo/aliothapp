"""业务入口与受遮挡分支的独立导航必须来自当前快照。"""

import json
from datetime import UTC, datetime

import pytest

from services.api.app.agents.tools import page_detection_tools as tools
from services.api.app.application.h5_adaptive_navigation import (
    discover_h5_adaptive_navigation_patterns,
)


def _snapshot():
    return {
        "url": "https://m.example.test/",
        "viewport": {"width": 390, "height": 844},
        "h5_modal_census_truncated": False,
        "h5_modal_census_selectors": ["#gate"],
        "elements": [
            {
                "selector": f"#nav-{index}",
                "parent_selector": "#nav",
                "tag": "a",
                "role": "link",
                "text": label,
                "href": f"/section/{index}",
                "visible": True,
                "interactive": True,
                "within_navigation": True,
                "rect": {"x": index * 65, "y": 755, "width": 65, "height": 69},
                "hit_test": {"center_hits_target": True},
                "active": index == 0,
            }
            for index, label in enumerate(("首页", "视频", "社区", "推荐", "活动", "我的"))
        ],
    }


@pytest.mark.parametrize("fault", [None, "intercepted", "incomplete", "close_available", "auth"])
def test_gate_keeps_only_current_hit_tested_global_navigation(fault):
    snapshot = _snapshot()
    actions = [
        tools.CandidateAction(action="click", target="内容标题", risk_level="low", purpose="详情")
    ]
    if fault == "intercepted":
        for element in snapshot["elements"]:
            element["hit_test"]["center_hits_target"] = False
    elif fault == "incomplete":
        snapshot["h5_modal_census_truncated"] = True
    elif fault == "close_available":
        actions.append(
            tools.CandidateAction(
                action="click",
                target="关闭弹窗",
                risk_level="low",
                purpose="关闭",
                obstruction_capability="h5_modal_close",
                obstruction_signature="signature",
            )
        )
    patterns = discover_h5_adaptive_navigation_patterns(snapshot)
    result = tools._with_independent_h5_gate_navigation(
        page_type="video_list",
        page_state="popup",
        candidate_actions=actions,
        patterns=patterns,
        pattern_statuses={},
        nav_alias_matches={},
        ui_snapshot=snapshot,
        authentication_required=fault == "auth",
    )
    if fault is None:
        assert result and all("bottom_navigation" in action.target_keywords for action in result)
        assert all(action.risk_level == "low" for action in result)
    else:
        assert result == actions


@pytest.mark.parametrize("page_type", ["home", "content_detail", "profile"])
@pytest.mark.parametrize(
    "label, expected", [("设置", "settings"), ("会员中心", "payment"), ("播放视频", "video_detail")]
)
def test_generic_business_link_is_discovered_without_profile_restriction(
    page_type, label, expected
):
    snapshot = _snapshot()
    snapshot["h5_modal_census_selectors"] = []
    snapshot["elements"] = [
        {
            "tag": "a",
            "selector": "#business-entry",
            "href": "/account/view",
            "text": label,
            "visible": True,
            "interactive": True,
            "hit_test": {"center_hits_target": True},
        }
    ]
    actions = tools._with_h5_verified_business_links(
        page_type=page_type,
        page_state="stable",
        candidate_actions=[],
        page_url=snapshot["url"],
        ui_snapshot=snapshot,
    )
    assert len(actions) == 1
    assert actions[0].expected_next_page_type == expected
    assert actions[0].destination_url == "https://m.example.test/account/view"
    assert actions[0].action == "navigate"


def test_purchase_candidate_cannot_hide_read_only_payment_entry():
    snapshot = _snapshot()
    snapshot["h5_modal_census_selectors"] = []
    snapshot["elements"] = [
        {
            "tag": "a",
            "href": "/membership/view",
            "text": "会员中心",
            "visible": True,
            "hit_test": {"center_hits_target": True},
        }
    ]
    purchase = tools.CandidateAction(
        action="click",
        target="立即购买",
        risk_level="high",
        purpose="购买",
        expected_next_page_type="payment",
    )
    actions = tools._with_h5_verified_business_links(
        page_type="home",
        page_state="stable",
        candidate_actions=[purchase],
        page_url=snapshot["url"],
        ui_snapshot=snapshot,
    )
    assert actions[0] == purchase
    assert actions[0].risk_level == "high"
    assert len(actions) == 2 and actions[1].action == "navigate"


@pytest.mark.parametrize(
    "fault",
    ["outside", "javascript", "write", "blocked", "modal", "incomplete", "ambiguous", "button"],
)
def test_business_link_does_not_authorize_submission_or_guess_hidden_entry(fault):
    snapshot = _snapshot()
    snapshot["h5_modal_census_selectors"] = []
    element = {
        "tag": "a",
        "href": "/view",
        "text": "会员中心",
        "visible": True,
        "interactive": True,
        "hit_test": {"center_hits_target": True},
    }
    if fault == "outside":
        element["href"] = "https://outside.test/vip"
    elif fault == "javascript":
        element["href"] = "javascript:purchase()"
    elif fault == "write":
        element["href"] = "/api/order/create?pay=1"
    elif fault == "blocked":
        element["hit_test"]["center_hits_target"] = False
    elif fault == "modal":
        snapshot["h5_modal_census_selectors"] = ["#gate"]
    elif fault == "incomplete":
        snapshot["capture_degraded"] = True
    elif fault == "button":
        element["tag"] = "button"
    snapshot["elements"] = [element]
    if fault == "ambiguous":
        snapshot["elements"].append({**element, "href": "/different"})
    assert (
        tools._with_h5_verified_business_links(
            page_type="home",
            page_state="stable",
            candidate_actions=[],
            page_url=snapshot["url"],
            ui_snapshot=snapshot,
        )
        == []
    )


@pytest.mark.parametrize(
    "fault", [None, "wrong_capture", "no_census", "forged_tab", "uncatalogued"]
)
async def test_gate_navigation_survives_detector_projection_with_bound_proof(monkeypatch, fault):
    from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
    from services.api.app.application.h5_exploration_observation import build_h5_frontier
    from services.api.app.domain.interaction import InteractionOperation
    from services.api.app.infrastructure.browser.semantic_target_resolver import (
        _select_structural_navigation_item,
    )
    from services.api.tests.test_page_detection_tools import (
        PNG_BYTES,
        FakeArtifactReader,
        FakeChatClient,
        FakeEvidenceLoader,
        _artifact,
        _bundle,
        _recognized_output,
        load_default_page_type_catalog,
    )

    monkeypatch.setattr(tools, "_PAGE_DETECTION_RESULT_CACHE", {})
    snapshot = _snapshot()
    dom = _artifact("dom", "dom", content_type="text/html")
    shot = _artifact("shot", "screenshot", content_type="image/png")
    ui = _artifact("ui", "ui_snapshot", content_type="application/json")
    output = _recognized_output("video_list")
    if fault == "uncatalogued":
        output.page_type = "uncatalogued-business-type"
    output.page_state = "popup"
    output.candidate_actions = [
        tools.CandidateAction(
            action="click",
            target="内容标题",
            risk_level="low",
            purpose="打开详情",
        )
    ]
    with bind_agent_run_context(
        AgentRunContext(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="run-1",
        )
    ):
        payload = await tools.detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(dom, shot, ui)),
            artifact_reader=FakeArtifactReader(
                {
                    "dom": b"<html><body><div>video list</div></body></html>",
                    "shot": PNG_BYTES,
                    "ui": json.dumps(snapshot).encode(),
                }
            ),
            chat_client=FakeChatClient(output),
            skill_text="# page-detection",
            catalog=load_default_page_type_catalog(),
            model_name="vision-test",
        )
    if fault == "uncatalogued":
        assert payload["candidate_actions"] == []
        assert not payload["obstruction_facts"].get("independent_bottom_navigation_targets")
        return
    assert payload["page_state"] == "popup"
    assert payload["rule_matching_allowed"] is True
    assert payload["candidate_actions"]
    facts = payload["obstruction_facts"]
    if fault == "wrong_capture":
        facts["independent_navigation_capture_id"] = "other-capture"
    elif fault == "no_census":
        facts["modal_census_complete"] = False
    elif fault == "forged_tab":
        payload["candidate_actions"][0]["target_keywords"][0] = "page_tab_navigation"
    frontier = build_h5_frontier(
        detection_result=payload, observed_at=datetime.now(UTC), navigation_depth=0
    )
    if fault in {"wrong_capture", "no_census"}:
        assert not any(
            candidate.metadata.get("independent_bottom_navigation")
            for candidate in frontier.candidates
        )
    else:
        independent = [
            candidate
            for candidate in frontier.candidates
            if candidate.metadata.get("independent_bottom_navigation")
        ]
        assert independent
        candidate = independent[-1]
        parameters = candidate.metadata["action_parameters"]
        assert parameters["target_mode"] == "bottom_navigation"
        selector = _select_structural_navigation_item(
            snapshot,
            operation=InteractionOperation.CLICK,
            **parameters,
        )
        assert selector.startswith("#nav-")
        assert frontier.observation.overlay_signature is not None


@pytest.mark.parametrize("blocked", [False, True])
def test_settings_and_payment_links_project_without_faking_arrival(blocked):
    from services.api.app.application.h5_exploration_observation import build_h5_frontier

    snapshot = _snapshot()
    snapshot["h5_modal_census_selectors"] = []
    snapshot["elements"] = [
        {
            "tag": "a",
            "href": "/account/center",
            "text": "设置",
            "visible": True,
            "hit_test": {"center_hits_target": not blocked},
        }
    ]
    actions = tools._with_h5_verified_business_links(
        page_type="home",
        page_state="stable",
        candidate_actions=[],
        page_url=snapshot["url"],
        ui_snapshot=snapshot,
    )
    payload = {
        "page_capture_id": "capture",
        "page_url": snapshot["url"],
        "page_type": "home",
        "page_state": "stable",
        "confidence": 0.95,
        "detection_status": "recognized",
        "candidate_actions": [
            {**action.model_dump(mode="json"), "destination_url": action.destination_url}
            for action in actions
        ],
        "h5_business_link_facts": {
            "source_capture_id": "capture",
            "source_url": snapshot["url"],
            "links": [
                {
                    "page_type": "settings",
                    "destination_url": "https://m.example.test/account/center",
                }
            ],
        },
    }
    frontier = build_h5_frontier(
        detection_result=payload, observed_at=datetime.now(UTC), navigation_depth=0
    )
    assert frontier.observation.page_type == "home"
    if not blocked:
        assert frontier.candidates[0].metadata["expected_next_page_type"] == "settings"
        assert (
            frontier.candidates[0].metadata["destination_url"]
            == "https://m.example.test/account/center"
        )
