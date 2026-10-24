"""隔离真实 DOM 串联采集、判定包、规划、解析与单次执行；禁网且不提交支付。"""

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from services.api.app.agents.explorer.maf_agent import MafExplorerAgent
from services.api.app.application.h5_probe_goal_planning import H5ProbeGoalPlanner
from services.api.app.application.h5_probe_journey import verified_display_subjects
from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.app.application.interaction_runtime import SemanticTargetResolutionRequest
from services.api.app.application.observation_packet import _compact_ui_snapshot
from services.api.app.domain.inspection import EvidenceInteractionRequirement
from services.api.app.domain.interaction import ActionIntent, InteractionOperation
from services.api.app.infrastructure.browser import page_capture
from services.api.app.infrastructure.browser.capture_runtime import _build_browser_action_request
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.tests.test_h5_probe_goal_planning import (
    NOW,
    _frontier,
    _request_like,
    _runtime_work,
)
from services.api.tests.test_h5_typed_probe_contract import _with_request


@pytest.fixture
async def page():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 600})
            result = await context.new_page()
            from services.api.app.infrastructure.browser.h5_readonly_display import (
                install_h5_readonly_display_guard,
            )

            await install_h5_readonly_display_guard(result.context)
            await result.context.route("**/*", lambda route: route.abort())
            yield result
        finally:
            await browser.close()


async def execute(page, goal, page_type, *, exclude=(), before_execute=None):
    snapshot = await page_capture._collect_ui_snapshot(page, surface_session_id="h5:fixture")
    compact = _compact_ui_snapshot(snapshot)
    target = select_probe_target(compact, goal=goal, page_type=page_type, excluded_keys=exclude)
    assert target is not None
    work = _runtime_work()
    requirement = EvidenceInteractionRequirement(
        operation=InteractionOperation.CLICK,
        semantic_target="当前可信展示目标",
        purpose="阶段补证",
        parameters={"goal_capability": goal, "probe_subject_key": probe_subject_key(target)},
    )
    work = _with_request(
        work, _request_like(work.evidence_request, required_interaction=requirement)
    )
    frontier = _frontier(work)
    frontier = replace(
        frontier,
        observation=replace(
            frontier.observation,
            page_type=page_type,
            page_state="stable",
            overlay_signature=None,
        ),
    )
    reader = SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot))
    delegate = SimpleNamespace(decide_next=AsyncMock(side_effect=AssertionError("不能调用模型")))
    baseline = await MafExplorerAgent(agent=SimpleNamespace(), now=lambda: NOW).decide_next(work)
    intent = await H5ProbeGoalPlanner(
        delegate=delegate,
        frontiers=SimpleNamespace(read_probe_frontier=AsyncMock(return_value=frontier)),
        snapshot_reader=reader,
        now=lambda: NOW,
    ).plan(work=work, baseline=baseline)
    assert isinstance(intent, ActionIntent)
    assert intent.deadline == baseline.deadline
    delegate.decide_next.assert_not_awaited()
    resolved = await UiSnapshotSemanticTargetResolver(snapshot_reader=reader).resolve(
        SemanticTargetResolutionRequest(
            task_id=work.task_id,
            session_id="h5:fixture",
            source_capture_id=work.source_capture_id,
            action_type=intent.action_type,
            semantic_target=intent.semantic_target,
            parameters=intent.parameters,
        )
    )
    intent = replace(intent, session_id="h5:fixture")
    request = _build_browser_action_request(
        SimpleNamespace(intent=intent), target=resolved, timeout_seconds=5
    )
    trace = []
    before = await page.screenshot()
    if before_execute is not None:
        await before_execute()
    await page_capture._run_single_trigger(
        page=page, context=page.context, request=request, action_trace=trace, timeout_ms=5000
    )
    return trace, before, await page.screenshot()


async def test_saved_031_membership_shell_supports_custom_card_selection_without_order(page):
    source = json.loads(
        (Path(__file__).parent / "fixtures/h5_031_payment_20261012_103747.json").read_text(
            encoding="utf-8"
        )
    )
    # 会员外壳取自保存片段；卡片内层与点击处理为受控重建，不冒充原站操作结果。
    await page.set_content(
        "<style>.swiper-slide{width:150px;height:80px;cursor:pointer}"
        ".swiper-slide.selected{background:#abc}</style>" + source["after_fragment"]
    )
    await page.locator(".swiper-wrapper").evaluate("""node => {
      node.innerHTML='<div id="month" class="swiper-slide">月卡 ¥100</div>'
        + '<div id="year" class="swiper-slide">年卡 ¥200</div>';
      window.orders=0;document.querySelector('.nowPay').onclick=()=>window.orders++;
      for(const card of node.children) card.onclick=()=>{
        for(const other of node.children)other.classList.remove('selected');
        card.classList.add('selected');document.querySelector('.nowPay').textContent=card.textContent;
      };
    }""")
    original = await page_capture._collect_ui_snapshot(page, surface_session_id="h5:fixture")
    trace, before, after = await execute(page, "payment_plan_selection", "payment")
    assert await page.locator("#month").evaluate("node=>node.classList.contains('selected')")
    assert await page.evaluate("window.orders") == 0
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is True
    assert before != after
    compact = _compact_ui_snapshot(
        await page_capture._collect_ui_snapshot(page, surface_session_id="h5:fixture")
    )
    from services.api.app.application.checkpoint_judgement import judge_checkpoint
    from services.api.app.infrastructure.browser.capture_runtime import _public_state_diff
    from services.api.tests.test_h5_payment_checkpoint import payment_case

    checkpoint, _, _, _, controlled_manifests = payment_case()
    judgement = judge_checkpoint(
        checkpoint, before=original, after=compact, manifests=controlled_manifests,
        action_receipt={"state_diff": _public_state_diff(SimpleNamespace(action_trace=trace)),
                        "status": "succeeded", "execution_outcome": "succeeded"},
    )
    assert judgement["status"] == "pass"
    visited = verified_display_subjects(
        {"facts": {"action_log": trace}}, goal="payment_plan_selection", snapshot=compact
    )
    second, _, _ = await execute(page, "payment_plan_selection", "payment", exclude=visited)
    assert await page.locator("#year").evaluate("node=>node.classList.contains('selected')")
    compact = _compact_ui_snapshot(
        await page_capture._collect_ui_snapshot(page, surface_session_id="h5:fixture")
    )
    visited = verified_display_subjects(
        {"facts": {"action_log": trace + second}}, goal="payment_plan_selection", snapshot=compact
    )
    assert len(visited) == 2
    assert (
        select_probe_target(
            compact, goal="payment_plan_selection", page_type="payment", excluded_keys=visited
        )
        is None
    )
    assert await page.evaluate("window.orders") == 0


@pytest.mark.parametrize("nested", [False, True])
async def test_scroll_then_native_back_to_top_uses_actual_container_and_fresh_frames(page, nested):
    await page.set_content("""<style>body{margin:0}#list{height:600px;overflow-y:auto}
      #top{position:fixed;right:20px;bottom:20px;width:70px;height:40px}</style>
      <div id="list"><main style="height:1800px">列表正文</main></div>
      <button id="top" aria-label="返回顶部" class="back-to-top">返回顶部</button>""")
    await page.evaluate(
        """nested => {
      if(!nested)list.style='height:1800px;overflow:visible';
      document.getElementById('top').onclick=()=>nested?list.scrollTo(0,0):window.scrollTo(0,0);
    }""",
        nested,
    )
    for position in (480, 960, 1200):
        await page.evaluate(
            "([nested,y])=>nested?list.scrollTo(0,y):window.scrollTo(0,y)", [nested, position]
        )
        await page_capture._collect_ui_snapshot(page, surface_session_id="h5:fixture")
    trace, before, after = await execute(page, "scroll_back_to_top", "video_list")
    assert await page.evaluate("nested=>nested?list.scrollTop:window.scrollY", nested) == 0
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is True
    assert before != after


async def test_native_video_fullscreen_uses_loaded_instance_not_thumbnail_click(page):
    await page.set_content(
        '<div class="post-content"><video id="player" controls muted loop '
        'style="width:320px;height:180px"></video></div>'
    )
    await page.evaluate("""async()=>{
      const canvas=document.createElement('canvas');canvas.width=320;canvas.height=180;
      const ctx=canvas.getContext('2d'),stream=canvas.captureStream(20),chunks=[];
      const recorder=new MediaRecorder(stream,{mimeType:'video/webm'});
      recorder.ondataavailable=e=>chunks.push(e.data);
      const end=new Promise(resolve=>recorder.onstop=resolve);
      let i=0;const timer=setInterval(()=>{
        ctx.fillStyle=i++%2?'blue':'green';ctx.fillRect(0,0,320,180)},40);
      recorder.start();await new Promise(resolve=>setTimeout(resolve,400));
      recorder.stop();await end;
      clearInterval(timer);stream.getTracks().forEach(track=>track.stop());
      player.src=URL.createObjectURL(new Blob(chunks,{type:'video/webm'}));await player.play();
    }""")
    await page.wait_for_function("player.currentTime>.05 && player.readyState>=2")
    trace, before, after = await execute(page, "native_video_fullscreen", "post_list")
    assert await page.evaluate("document.fullscreenElement===player")
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is True
    assert before != after


@pytest.mark.parametrize("effect", ["panel", "aria_only", "none"])
async def test_scroll_button_needs_controlled_region_feedback_not_only_attribute(page, effect):
    await page.set_content("""<style>body{margin:0}.van-collapse-item{position:fixed;top:40px;
      left:20px;width:250px} #panel{display:none;height:100px;background:#abc}</style>
      <main style="height:1800px">列表</main><div class="van-collapse-item">
      <button id="button" role="button" class="van-collapse-item__title" aria-expanded="false"
      aria-controls="panel">展开目录</button><div id="panel">目录说明</div></div>""")
    await page.evaluate(
        """effect=>{
      document.getElementById('button').onclick=()=>{
        if(effect!=='none')document.getElementById('button').setAttribute('aria-expanded','true');
        if(effect==='panel')panel.style.display='block';
      };window.scrollTo(0,1200);
    }""",
        effect,
    )
    trace, _, _ = await execute(page, "scroll_button_feedback", "video_list")
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is (effect == "panel")


@pytest.mark.parametrize("change", ["text", "form", "covered"])
async def test_changed_payment_control_rejects_before_click_without_order(page, change):
    from services.api.app.infrastructure.browser.page_capture import BlockedPageCaptureActionError

    await page.set_content("""<div class="vip-plans"><div id="slot" role="radio"
      aria-checked="false" class="plan-option" style="width:150px;height:60px">月卡 ¥30</div></div>
      <script>window.clicks=0;slot.onclick=()=>window.clicks++;</script>""")

    async def mutate():
        await page.evaluate(
            """change=>{
          if(change==='text')slot.textContent='确认支付 ¥30';
              if(change==='form'){const node=document.getElementById('slot');
                const form=document.createElement('form');node.replaceWith(form);form.append(node)}
          if(change==='covered')document.body.insertAdjacentHTML('beforeend',
            '<div style="position:fixed;inset:0;z-index:9999">遮挡</div>');
        }""",
            change,
        )

    with pytest.raises(BlockedPageCaptureActionError):
        await execute(page, "payment_plan_selection", "payment", before_execute=mutate)
    assert await page.evaluate("window.clicks") == 0
