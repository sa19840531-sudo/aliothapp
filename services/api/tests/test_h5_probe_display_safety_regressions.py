"""保存的031结构与审查最小DOM反例；禁网，不追认原站业务验收。"""

import asyncio

import pytest

from services.api.app.application.h5_probe_journey import verified_display_subjects
from services.api.app.application.h5_probe_targets import select_probe_target
from services.api.app.application.observation_packet import _compact_ui_snapshot
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.test_h5_probe_journey_browser import execute
from services.api.tests.test_h5_probe_journey_browser import page as page

PLAN = """<div class="member"><div class="card-box"><div class="swiper-wrapper">
<div id="month" class="swiper-slide swiper-slide-active"
style="cursor:pointer;width:150px;height:80px">月卡 ¥30</div>
<div id="year" class="swiper-slide swiper-slide-next"
style="cursor:pointer;width:150px;height:80px">年卡 ¥200</div>
</div></div></div>"""
IMAGE = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='100' "
    "height='100'%3E%3Crect width='100' height='100' fill='red'/%3E%3C/svg%3E"
)


async def test_swiper_state_changes_do_not_reopen_completed_plan(page):
    await page.set_content(
        PLAN
        + """<script>
      for(const c of [month,year]) c.onclick=()=>{
        for(const o of [month,year])o.classList.remove('selected');
        c.classList.add('selected');
        if(c===year){month.classList.remove('swiper-slide-active');
          month.classList.add('swiper-slide-prev');year.classList.remove('swiper-slide-next');
          year.classList.add('swiper-slide-active')}
      };</script>"""
    )
    first, _, _ = await execute(page, "payment_plan_selection", "payment")
    snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    visited = verified_display_subjects(
        {"facts": {"action_log": first}}, goal="payment_plan_selection", snapshot=snapshot
    )
    second, _, _ = await execute(page, "payment_plan_selection", "payment", exclude=visited)
    assert second[-1]["result"] == "probe_effect_observed"
    snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    visited = verified_display_subjects(
        {"facts": {"action_log": first + second}},
        goal="payment_plan_selection",
        snapshot=snapshot,
    )
    assert len(visited) == 2
    assert (
        select_probe_target(
            snapshot, goal="payment_plan_selection", page_type="payment", excluded_keys=visited
        )
        is None
    )


@pytest.mark.parametrize("changed", ["price", "label", "source"])
async def test_plan_business_change_is_not_reused_as_completed(page, changed):
    await page.set_content(
        PLAN
        + """<script>
      month.onclick=()=>month.classList.add('selected');</script>"""
    )
    first, _, _ = await execute(page, "payment_plan_selection", "payment")
    snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    visited = verified_display_subjects(
        {"facts": {"action_log": first}}, goal="payment_plan_selection", snapshot=snapshot
    )
    await page.evaluate(
        """changed=>{
      month.classList.remove('selected');year.remove();
      if(changed==='price')month.textContent='月卡 ¥31';
      if(changed==='label')month.textContent='周卡 ¥30';
      if(changed==='source')month.id='replacement';
    }""",
        changed,
    )
    snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    assert (
        select_probe_target(
            snapshot, goal="payment_plan_selection", page_type="payment", excluded_keys=visited
        )
        is not None
    )


@pytest.mark.parametrize("delay", [0, 350])
async def test_plan_waits_for_local_feedback_without_reclick(page, delay):
    await page.set_content(PLAN)
    await page.evaluate(
        """delay=>{window.clicks=0;month.onclick=()=>{
      window.clicks++;setTimeout(()=>month.classList.add('selected'),delay)};}""",
        delay,
    )
    trace, _, _ = await execute(page, "payment_plan_selection", "payment")
    assert trace[-1]["result"] == "probe_effect_observed"
    assert await page.evaluate("window.clicks") == 1


async def test_smooth_back_to_top_waits_for_real_container_position(page):
    await page.set_content("""<style>body{margin:0}</style><main style="height:1800px">正文</main>
      <button id="top" aria-label="返回顶部" style="position:fixed;right:20px;bottom:20px">
      返回顶部</button><script>window.clicks=0;
      document.getElementById('top').onclick=()=>{window.clicks++;
        window.scrollTo({top:0,behavior:'smooth'})}</script>""")
    await page.evaluate("window.scrollTo({top:1200,behavior:'instant'})")
    trace, _, _ = await execute(page, "scroll_back_to_top", "video_list")
    assert trace[-1]["result"] == "probe_effect_observed"
    assert await page.evaluate("window.scrollY") <= 2
    assert await page.evaluate("window.clicks") == 1


@pytest.mark.parametrize("bad", [None, "wrong_resource", "duplicate", "preexisting", "loading"])
async def test_lightbox_pairs_new_preview_with_source_not_original_selector(page, bad):
    await page.set_content(
        '<div class="post-content"><img id="thumb" style="width:150px;height:100px" '
        f'src="{IMAGE}"></div>'
    )
    await page.wait_for_function("thumb.complete && thumb.naturalWidth>0")
    await page.evaluate(
        """bad=>{
      const make=()=>{const n=thumb.cloneNode();n.id='preview';
        n.className='van-image-preview__image';
        n.style='position:fixed;inset:0;width:390px;height:600px;z-index:9999';
        if(bad==='wrong_resource')n.src=n.src.replace('red','blue');
        if(bad==='loading')n.src='https://fixture.invalid/not-loaded';
        const root=document.createElement('div');root.className='van-image-preview';
        root.setAttribute('role','dialog');root.append(n);document.body.append(root);
        if(bad==='preexisting'){root.style.display='none';window.oldRoot=root;}
      };
      if(bad==='preexisting')make();
      window.clicks=0;thumb.onclick=()=>{window.clicks++;
        if(bad==='preexisting')oldRoot.style.display='block';
        else {make();if(bad==='duplicate')make();}}
    }""",
        bad,
    )
    trace, _, _ = await execute(page, "media_fullscreen_pair", "post_list")
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is (bad is None)
    assert await page.evaluate("window.clicks") == 1


async def test_observer_keeps_registered_plan_and_its_safety_facts_under_original_limit(page):
    images = "".join(
        f'<img id="image{i}" src="{IMAGE}" style="width:10px;height:10px">' for i in range(90)
    )
    await page.set_content(
        "<header><button>返回</button><button>帮助</button></header>"
        + images
        + '<div class="vip-plans"><div id="slot" role="radio" aria-checked="false" '
        'class="plan-option" style="width:150px;height:60px">月卡 ¥30</div></div>'
    )
    await page.wait_for_function("image0.complete && image0.naturalWidth>0")
    raw = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    assert select_probe_target(raw, goal="payment_plan_selection", page_type="payment")
    compact = _compact_ui_snapshot(raw)
    assert len(compact["elements"]) <= 80
    assert select_probe_target(compact, goal="payment_plan_selection", page_type="payment")


@pytest.mark.parametrize(
    "method", ["POST", "GET", "beacon", "popup", "late", "xhr", "image", "cached_fetch", "worker"]
)
async def test_payment_display_cannot_send_order_even_after_probe_returns(page, method):
    from services.api.app.infrastructure.browser.h5_readonly_display import (
        install_h5_readonly_display_guard,
    )

    await install_h5_readonly_display_guard(page.context)
    orders = []

    async def backend(route):
        orders.append(route.request.url)
        await route.fulfill(status=200, body="{}")

    await page.context.route("https://fixture.invalid/order**", backend)
    await page.set_content(PLAN)
    await page.evaluate(
        """method=>{const cachedFetch=fetch;month.onclick=()=>{
      month.classList.add('selected');
      const send=()=>fetch('https://fixture.invalid/order',{method:method==='POST'?'POST':'GET'})
        .catch(()=>{});
      if(method==='beacon')navigator.sendBeacon('https://fixture.invalid/order','order');
      else if(method==='popup')window.open('https://fixture.invalid/order');
      else if(method==='xhr'){const x=new XMLHttpRequest();
        x.open('POST','https://fixture.invalid/order');x.send('order');}
      else if(method==='image'){const i=new Image();i.src='https://fixture.invalid/order';}
      else if(method==='cached_fetch')cachedFetch('https://fixture.invalid/order').catch(()=>{});
      else if(method==='worker')new Worker(URL.createObjectURL(new Blob([
        "fetch('https://fixture.invalid/order',{method:'POST'})"],{type:'text/javascript'})));
      else if(method==='late')setTimeout(send,1800);else send();
    }}""",
        method,
    )
    trace, _, _ = await execute(page, "payment_plan_selection", "payment")
    await asyncio.sleep(2 if method == "late" else 0.1)
    assert not orders
    assert trace[-1]["state_diff"]["h5_probe_effect"].get("readonly_isolation") is True
    if method != "late":
        assert trace[-1]["result"] == "probe_effect_unverified"


async def test_payment_without_guard_rejects_before_input(page):
    from services.api.app.infrastructure.browser.page_capture import BlockedPageCaptureActionError

    context = await page.context.browser.new_context(viewport={"width": 390, "height": 600})
    try:
        unguarded = await context.new_page()
        await context.route("**/*", lambda route: route.abort())
        await unguarded.set_content(
            PLAN + "<script>window.orders=0;month.onclick=()=>window.orders++</script>"
        )
        with pytest.raises(BlockedPageCaptureActionError, match="只读"):
            await execute(unguarded, "payment_plan_selection", "payment")
        assert await unguarded.evaluate("window.orders") == 0
    finally:
        await context.close()


@pytest.mark.parametrize("channel", ["frame", "second_page", "peer", "inflight"])
async def test_payment_active_channel_rejects_before_input(page, channel):
    from services.api.app.infrastructure.browser.page_capture import BlockedPageCaptureActionError

    await page.set_content(
        PLAN + "<script>window.clicks=0;month.onclick=()=>window.clicks++</script>"
    )
    guard = page.context._alioth_h5_readonly_display_guard
    if channel == "frame":
        await page.evaluate("document.body.append(document.createElement('iframe'))")
    elif channel == "second_page":
        await page.context.new_page()
    elif channel == "peer":
        await page.evaluate("window.peer=new RTCPeerConnection()")
    else:
        guard.inflight.add(object())
    with pytest.raises(BlockedPageCaptureActionError, match="只读"):
        await execute(page, "payment_plan_selection", "payment")
    assert await page.evaluate("window.clicks") == 0


async def test_payment_guard_releases_only_trusted_new_document_and_keeps_video_route(page):
    from services.api.app.infrastructure.browser.h5_readonly_display import (
        authorize_h5_readonly_goto,
    )

    seen = []

    async def backend(route):
        seen.append(route.request.url)
        body = '<script>fetch("/media-metadata").catch(()=>{})</script><video controls></video>'
        await route.fulfill(
            body=body if route.request.is_navigation_request() else "{}",
            content_type="text/html"
            if route.request.is_navigation_request()
            else "application/json",
        )

    await page.context.route("https://fixture.invalid/**", backend)
    await page.set_content(
        PLAN + "<script>month.onclick=()=>month.classList.add('selected')</script>"
    )
    await execute(page, "payment_plan_selection", "payment")
    guard = page.context._alioth_h5_readonly_display_guard
    authorize_h5_readonly_goto(page, "about:blank#same-document")
    await page.goto("about:blank#same-document")
    assert guard.page is page
    await page.evaluate("fetch('https://fixture.invalid/order').catch(()=>{})")
    assert seen == []
    authorize_h5_readonly_goto(page, "https://fixture.invalid/video")
    await page.goto("https://fixture.invalid/video")
    await page.wait_for_function("document.querySelector('video')!==null")
    for _ in range(20):
        if "https://fixture.invalid/media-metadata" in seen:
            break
        await asyncio.sleep(0.05)
    assert guard.page is None
    assert seen == ["https://fixture.invalid/video", "https://fixture.invalid/media-metadata"]


async def test_display_no_effect_is_not_reclicked_or_accepted(page):
    await page.set_content(
        PLAN + "<script>window.clicks=0;month.onclick=()=>window.clicks++</script>"
    )
    trace, _, _ = await execute(page, "payment_plan_selection", "payment")
    assert trace[-1]["result"] == "probe_effect_unverified"
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is False
    assert await page.evaluate("window.clicks") == 1


async def test_post_input_timeout_keeps_action_fact_without_replay(page, monkeypatch):
    from services.api.app.infrastructure.browser import h5_probe_display_execution, page_capture

    await page.set_content(
        PLAN + "<script>window.clicks=0;month.onclick=()=>window.clicks++</script>"
    )
    original = page_capture._run_single_trigger
    recorded = []

    async def short_trigger(**kwargs):
        recorded.append(kwargs["action_trace"])
        kwargs["timeout_ms"] = 500
        return await original(**kwargs)

    async def slow_effect(*args):
        await asyncio.sleep(2)
        raise AssertionError("到期后不能继续接受结果")

    monkeypatch.setattr(page_capture, "_run_single_trigger", short_trigger)
    monkeypatch.setattr(h5_probe_display_execution, "_effect", slow_effect)
    with pytest.raises(TimeoutError):
        await execute(page, "payment_plan_selection", "payment")
    assert await page.evaluate("window.clicks") == 1
    assert recorded[0][-1]["result"] == "probe_effect_unverified"
    assert recorded[0][-1]["state_diff"]["h5_probe_effect"]["verified"] is False
