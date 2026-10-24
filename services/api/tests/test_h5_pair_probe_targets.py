"""定向媒体及套餐补证只授权当前帧可信展示动作，不提交支付。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)


def _snapshot(goal):
    element = {
        "selector": "#target",
        "tag": "img" if goal == "media_fullscreen_pair" else "div",
        "role": None if goal == "media_fullscreen_pair" else "radio",
        "src": "https://fixture.invalid/post/cover.png"
        if goal == "media_fullscreen_pair"
        else None,
        "text": "" if goal == "media_fullscreen_pair" else "月卡 ¥30",
        "class_tokens": ["post-image"] if goal == "media_fullscreen_pair" else ["plan-option"],
        "ancestor_class_tokens": ["post-content"]
        if goal == "media_fullscreen_pair"
        else ["vip-plans"],
        "visible": True,
        "disabled": False,
        "form_associated": False,
        "h5_probe_local_display": True,
        "image": {"complete": True, "natural_width": 120, "natural_height": 90},
        "rect": {"x": 20, "y": 100, "width": 120, "height": 90},
        "hit_test": {"center_hits_target": True},
        "aria_checked": "false",
    }
    return {"viewport": {"width": 390, "height": 844}, "elements": [element]}


@pytest.mark.parametrize(
    "goal,page",
    [
        ("media_fullscreen_pair", "content_detail"),
        ("media_fullscreen_pair", "post_list"),
        ("payment_plan_selection", "payment"),
    ],
)
@pytest.mark.parametrize(
    "bad",
    [
        None,
        "covered",
        "offscreen",
        "advertisement",
        "form",
        "href",
        "disabled",
        "aria_disabled",
        "wrong_page",
        "duplicate",
        "zero_size",
        "nonlocal",
        "unknown_safety",
    ],
)
def test_pair_target_rejects_unsafe_or_ambiguous_evidence(goal, page, bad):
    from services.api.app.application.h5_probe_targets import select_probe_target

    snapshot = _snapshot(goal)
    element = snapshot["elements"][0]
    if bad == "covered":
        element["hit_test"]["center_hits_target"] = False
    elif bad == "offscreen":
        element["rect"]["y"] = 2000
    elif bad == "advertisement":
        element["is_advertisement"] = True
    elif bad == "form":
        element["form_associated"] = True
    elif bad == "href":
        element["href"] = "https://fixture.invalid/submit-payment"
    elif bad == "disabled":
        element["disabled"] = True
    elif bad == "aria_disabled":
        element["aria_disabled"] = "true"
    elif bad == "wrong_page":
        page = "home"
    elif bad == "duplicate":
        snapshot["elements"].append({**element, "selector": "#other"})
    elif bad == "zero_size":
        element["rect"]["height"] = 0
    elif bad == "nonlocal":
        element["h5_probe_local_display"] = False
    elif bad == "unknown_safety":
        element.pop("h5_probe_local_display")
    assert bool(select_probe_target(snapshot, goal=goal, page_type=page)) is (bad is None)


@pytest.mark.parametrize(
    "goal,page",
    [
        ("media_fullscreen_pair", "content_detail"),
        ("media_fullscreen_pair", "post_list"),
        ("payment_plan_selection", "payment"),
    ],
)
@pytest.mark.parametrize("drift", [False, True])
async def test_target_resolution_revalidates_subject_and_does_not_use_model_selector(
    goal, page, drift
):
    from services.api.app.application.h5_probe_targets import (
        PROBE_TARGET_LABELS,
        PROBE_TARGET_MODE,
        probe_subject_key,
        select_probe_target,
    )

    snapshot = _snapshot(goal)
    original = select_probe_target(snapshot, goal=goal, page_type=page)
    current = deepcopy(snapshot)
    if drift:
        current["elements"][0]["src" if goal == "media_fullscreen_pair" else "text"] = "另一个对象"
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=current))
    )
    request = SemanticTargetResolutionRequest(
        task_id="task",
        session_id="session",
        source_capture_id="capture",
        action_type=InteractionOperation.CLICK,
        semantic_target=PROBE_TARGET_LABELS[goal],
        parameters={
            "target_mode": PROBE_TARGET_MODE,
            "goal_capability": goal,
            "source_page_type": page,
            "probe_subject_key": probe_subject_key(original),
        },
    )
    if drift:
        with pytest.raises(SemanticTargetResolutionError):
            await resolver.resolve(request)
    else:
        target = await resolver.resolve(request)
        assert target.selector == "#target"
        assert target.expected_rect == original["rect"]


@pytest.mark.parametrize(
    "goal,page_type",
    [
        ("media_fullscreen_pair", "content_detail"),
        ("media_fullscreen_pair", "post_list"),
        ("payment_plan_selection", "payment"),
    ],
)
@pytest.mark.parametrize("logical_targets", [False, True])
async def test_actual_dom_planner_resolver_and_display_click_keep_fresh_pair_without_network(
    goal, page_type, logical_targets
):
    from dataclasses import replace

    from playwright.async_api import async_playwright

    from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
    from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
    from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
    from services.api.app.domain.inspection import EvidenceInteractionRequirement
    from services.api.app.domain.interaction import ActionIntent
    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
    from services.api.tests.test_h5_probe_goal_planning import (
        NOW,
        _frontier,
        _request_like,
        _runtime_work,
    )
    from services.api.tests.test_h5_typed_probe_contract import _with_request

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: route.abort())
            if goal == "media_fullscreen_pair":
                await page.set_content("""<div class="post-content"><img id="target"
                    style="width:120px;height:90px" src="data:image/svg+xml,%3Csvg
                    xmlns='http://www.w3.org/2000/svg' width='120' height='90'%3E
                    %3Crect width='120' height='90' fill='red'/%3E%3C/svg%3E"></div>
                    <script>target.onclick=()=>{target.style='position:fixed;inset:0;width:390px;height:844px';
                    target.dataset.fullscreen='true'}</script>""")
                await page.locator("#target").evaluate("node => node.decode()")
            else:
                await page.set_content("""<div class="vip-plans"><div id="target" role="radio"
                    aria-checked="false" class="plan-option" style="width:120px;height:90px">
                    月卡 ¥30</div>
                    <div id="annual" role="radio" aria-checked="false" class="plan-option"
                    style="width:120px;height:90px">年卡 ¥300</div></div>
                    <div id="price">未选档</div>
                    <button id="submit">立即支付</button><script>window.submitCount=0;
                    target.onclick=()=>{target.setAttribute('aria-checked','true');price.textContent='¥30'};
                    submit.onclick=()=>window.submitCount++;</script>""")
            snapshot = await _collect_ui_snapshot(
                page,
                surface_session_id="h5:fixture",
                logical_surface_enabled=logical_targets,
                logical_surface_shadow=False,
            )
            before_image = await page.screenshot()
            work = _runtime_work()
            work = _with_request(
                work,
                _request_like(
                    work.evidence_request,
                    required_interaction=EvidenceInteractionRequirement(
                        operation=InteractionOperation.CLICK,
                        semantic_target="定向展示目标",
                        purpose="补充前后证据",
                        parameters={
                            "goal_capability": goal,
                            "probe_subject_key": probe_subject_key(
                                select_probe_target(snapshot, goal=goal, page_type=page_type)
                            ),
                        },
                    ),
                ),
            )
            frontier = _frontier(work)
            frontier = replace(
                frontier,
                observation=replace(
                    frontier.observation,
                    page_type=page_type,
                    page_state="stable",
                    overlay_signature=None,
                    metadata={
                        **frontier.observation.metadata,
                        "logical_targets_effective": logical_targets,
                        **(
                            {
                                "logical_surface_identity_fingerprint": snapshot["logical_surface"][
                                    "identity_fingerprint"
                                ],
                                "logical_surface_state_fingerprint": snapshot["logical_surface"][
                                    "state_fingerprint"
                                ],
                            }
                            if logical_targets
                            else {}
                        ),
                    },
                ),
            )
            reader = SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot))
            delegate = SimpleNamespace(
                decide_next=AsyncMock(side_effect=AssertionError("不得调用模型"))
            )
            baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(
                work
            )
            intent = await H5ProbeGoalPlanner(
                delegate=delegate,
                frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
                snapshot_reader=reader,
                now=lambda: NOW,
            ).plan(work=work, baseline=baseline)
            assert isinstance(intent, ActionIntent)
            assert intent.deadline == baseline.deadline
            assert intent.capture_profile.before.role == "probe_before"
            assert intent.capture_profile.after.role == "probe_result_replay"
            delegate.decide_next.assert_not_awaited()
            target = await UiSnapshotSemanticTargetResolver(
                snapshot_reader=reader,
                logical_targets_enabled=logical_targets,
            ).resolve(
                SemanticTargetResolutionRequest(
                    task_id=work.task_id,
                    session_id=work.session_id,
                    source_capture_id=work.source_capture_id,
                    action_type=intent.action_type,
                    semantic_target=intent.semantic_target,
                    parameters=intent.parameters,
                )
            )
            if logical_targets:
                assert target.target_signature and target.target_identity_signature
                assert (
                    target.source_state_fingerprint
                    == snapshot["logical_surface"]["state_fingerprint"]
                )
                assert intent.parameters["logical_target"]["expects_logical_navigation"] is False
            await page.locator(target.selector).click()
            assert await page.screenshot() != before_image
            if goal == "media_fullscreen_pair":
                assert await page.locator("#target").get_attribute("data-fullscreen") == "true"
            else:
                assert await page.locator("#price").text_content() == "¥30"
                assert await page.evaluate("window.submitCount") == 0
            await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
        finally:
            await browser.close()


def test_payment_submit_is_never_a_plan_selection():
    from services.api.app.application.h5_probe_targets import select_probe_target

    for text in ("立即支付 ¥30", "确认充值 ¥30", "购买月卡 ¥30", "Submit order $30"):
        snapshot = _snapshot("payment_plan_selection")
        snapshot["elements"][0]["text"] = text
        assert (
            select_probe_target(snapshot, goal="payment_plan_selection", page_type="payment")
            is None
        )


def test_loading_thumbnail_cannot_start_media_pair():
    from services.api.app.application.h5_probe_targets import select_probe_target

    snapshot = _snapshot("media_fullscreen_pair")
    snapshot["elements"][0]["image"]["complete"] = False
    snapshot["elements"][0]["image"]["natural_width"] = 0
    assert (
        select_probe_target(snapshot, goal="media_fullscreen_pair", page_type="content_detail")
        is None
    )


def test_distinct_plan_options_choose_one_slot_without_submitting_or_claiming_all_slots():
    from services.api.app.application.h5_probe_targets import select_probe_target

    snapshot = _snapshot("payment_plan_selection")
    first = snapshot["elements"][0]
    snapshot["elements"].append(
        {**first, "selector": "#annual", "text": "年卡 ¥300", "rect": {**first["rect"], "y": 250}}
    )
    assert (
        select_probe_target(snapshot, goal="payment_plan_selection", page_type="payment") == first
    )
    first["aria_checked"] = "true"
    assert (
        select_probe_target(snapshot, goal="payment_plan_selection", page_type="payment")[
            "selector"
        ]
        == "#annual"
    )


async def test_crowded_snapshot_retains_selection_proof_with_original_byte_limit(monkeypatch):
    from playwright.async_api import async_playwright

    from services.api.app.application.h5_probe_targets import select_probe_target
    from services.api.app.infrastructure.browser import page_capture

    monkeypatch.setattr(page_capture, "MAX_UI_SNAPSHOT_BYTES", 16000)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""<div class="vip-plans"><div id="target" role="radio"
                aria-checked="false" class="plan-option"
                style="width:120px;height:90px">月卡 ¥30</div></div><main id="crowded"></main>""")
            await page.evaluate("""() => {
                for(let i=0;i<150;i++) {
                  const a=document.createElement('a');a.href='#';a.id='item'+i;
                  a.textContent='普通目录'.repeat(100);
                  a.style='position:absolute;top:300px;left:0;width:350px;height:24px';
                  crowded.append(a);
                }
            }""")
            snapshot = await page_capture._collect_ui_snapshot(
                page, surface_session_id="h5:fixture"
            )
            assert page_capture._json_size_bytes(snapshot) <= 16000
            target = select_probe_target(
                snapshot, goal="payment_plan_selection", page_type="payment"
            )
            assert target is not None and target["selector"] == "#target"
            assert target["aria_checked"] == "false"
            assert snapshot["truncated"] is True
        finally:
            await browser.close()


@pytest.mark.parametrize("disabled_scope", [None, "self", "ancestor", "inert"])
async def test_actual_dom_disabled_controls_cannot_become_display_probe(disabled_scope):
    from playwright.async_api import async_playwright

    from services.api.app.application.h5_probe_targets import select_probe_target
    from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""<div id="plans" class="vip-plans"><div id="target"
                role="radio" aria-checked="false" class="plan-option"
                style="width:120px;height:90px">月卡 ¥30</div></div>""")
            if disabled_scope == "self":
                await page.locator("#target").evaluate(
                    "node=>node.setAttribute('aria-disabled','true')"
                )
            elif disabled_scope == "ancestor":
                await page.locator("#plans").evaluate(
                    "node=>node.setAttribute('aria-disabled','true')"
                )
            elif disabled_scope == "inert":
                await page.locator("#plans").evaluate("node=>node.inert=true")
            snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
            subject = select_probe_target(
                snapshot, goal="payment_plan_selection", page_type="payment"
            )
            assert bool(subject) is (disabled_scope is None)
        finally:
            await browser.close()
