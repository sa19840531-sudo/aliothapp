"""禁网Chromium原件贯通S35新分派；受控静态场景不代表真实三端产品验收。"""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.test_green_process_judge import judge, original, process_case
from services.api.tests.test_h5_probe_journey_browser import execute
from services.api.tests.test_h5_probe_journey_browser import page as page

_IMAGE = (
    "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='120' "
    "height='90'%3E%3Crect width='120' height='90' fill='red'/%3E%3C/svg%3E"
)


async def _page_content(page, goal, outcome):
    if goal == "payment_plan_selection":
        await page.set_content(
            '<div class="vip-plans"><div id="source" role="radio" aria-checked="false" '
            'class="plan-option" style="cursor:pointer;width:160px;height:70px">月卡 ¥30</div>'
            "</div>"
        )
        await page.evaluate(
            """outcome=>{
          window.clicks=0;document.getElementById('source').onclick=()=>{
            window.clicks++;
            if(outcome==='correct')document.getElementById('source').setAttribute(
              'aria-checked','true');
            if(outcome==='class_only')document.getElementById('source').classList.add('selected');
          };
        }""",
            outcome,
        )
        return "payment"
    await page.set_content(
        '<style>body{margin:0}</style><div class="post-content"><img id="source" '
        'style="width:120px;height:90px" src="' + _IMAGE + '"></div>'
    )
    await page.wait_for_function("document.getElementById('source').complete")
    await page.evaluate(
        """outcome=>{
      window.clicks=0;document.getElementById('source').onclick=()=>{
        window.clicks++;if(outcome==='no_response')return;
        const image=document.getElementById('source').cloneNode();image.id='preview';
        image.style='position:fixed;inset:0;width:390px;height:600px;z-index:9999';
        if(outcome==='wrong_resource')image.src=image.src.replace('red','blue');
        const root=document.createElement('div');root.className='van-image-preview';
        root.setAttribute('role','dialog');root.append(image);document.body.append(root);
      };
    }""",
        outcome,
    )
    return "post_list"


@pytest.mark.parametrize(
    "goal,outcome,expected",
    [
        ("payment_plan_selection", "correct", "pass"),
        ("payment_plan_selection", "class_only", "evidence_insufficient"),
        ("payment_plan_selection", "no_response", "evidence_insufficient"),
        ("media_fullscreen_pair", "correct", "pass"),
        ("media_fullscreen_pair", "wrong_resource", "evidence_insufficient"),
        ("media_fullscreen_pair", "no_response", "evidence_insufficient"),
    ],
)
async def test_existing_capture_and_display_producers_flow_into_new_shared_s35_dispatch(
    page,
    goal,
    outcome,
    expected,
):
    page_type = await _page_content(page, goal, outcome)
    before = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    subject = select_probe_target(before, goal=goal, page_type=page_type)
    assert subject is not None
    started_at = datetime.now(UTC)
    trace, before_png, after_png = await execute(page, goal, page_type)
    finished_at = datetime.now(UTC)
    after = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    items = _judge_originals(
        before,
        after,
        trace,
        subject,
        goal,
        page_type,
        started_at,
        finished_at,
    )
    assert items[0].status == expected, items[0]
    assert all(item.status == "evidence_insufficient" for item in items[1:])
    assert before_png.startswith(b"\x89PNG") and after_png.startswith(b"\x89PNG")
    assert await page.evaluate("window.clicks") == 1


def _judge_originals(before, after, trace, subject, goal, page_type, started_at, finished_at):
    # 直接使用正式采集函数的原始字典和执行函数的原始trace，不补写判法所需字段。
    fixture, evidence = process_case()
    action = evidence.actions[0]
    intent = json.loads(action.intent_payload)
    intent["session_id"] = "h5:fixture"
    intent["parameters"].update(
        goal_capability=goal,
        source_page_type=page_type,
        probe_subject_key=probe_subject_key(subject),
    )
    action = replace(
        action,
        session_id="h5:fixture",
        started_at=started_at,
        finished_at=finished_at,
        intent_payload=json.dumps(intent).encode(),
    )
    captures = tuple(
        replace(
            capture,
            session_id="h5:fixture",
            originals=tuple(
                original(capture.capture_id, kind, payload)
                for kind, payload in (
                    [("ui_snapshot", before)]
                    if index == 0
                    else [("ui_snapshot", after), ("action_trace", trace)]
                )
            ),
        )
        for index, capture in enumerate(evidence.captures)
    )
    return judge(fixture, replace(evidence, captures=captures, actions=(action,)))


async def test_driver_fullscreen_on_video_without_product_click_logic_cannot_prove_s35(page):
    await page.set_content(
        '<div class="post-content"><video id="player" controls muted loop '
        'style="width:320px;height:180px"></video></div>'
    )
    await page.evaluate("""async()=>{
      const canvas=document.createElement('canvas');canvas.width=320;canvas.height=180;
      const context=canvas.getContext('2d'),stream=canvas.captureStream(20),chunks=[];
      const recorder=new MediaRecorder(stream,{mimeType:'video/webm'});
      recorder.ondataavailable=event=>chunks.push(event.data);
      const stopped=new Promise(resolve=>recorder.onstop=resolve);
      let frame=0;const timer=setInterval(()=>{
        context.fillStyle=frame++%2?'blue':'green';context.fillRect(0,0,320,180)},40);
      recorder.start();await new Promise(resolve=>setTimeout(resolve,400));
      recorder.stop();await stopped;
      clearInterval(timer);stream.getTracks().forEach(track=>track.stop());
      player.src=URL.createObjectURL(new Blob(chunks,{type:'video/webm'}));await player.play();
      window.clicks=0;document.addEventListener('click',()=>window.clicks++);
    }""")
    await page.wait_for_function("player.currentTime>.05 && player.readyState>=2")
    goal, page_type = "native_video_fullscreen", "post_list"
    before = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    subject = select_probe_target(before, goal=goal, page_type=page_type)
    assert subject is not None
    started_at = datetime.now(UTC)
    trace, _, _ = await execute(page, goal, page_type)
    finished_at = datetime.now(UTC)
    after = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    assert await page.evaluate("document.fullscreenElement===player") is True
    assert await page.evaluate("window.clicks") == 0
    assert trace[-1]["state_diff"]["h5_probe_effect"]["verified"] is True
    items = _judge_originals(
        before,
        after,
        trace,
        subject,
        goal,
        page_type,
        started_at,
        finished_at,
    )
    assert items[0].status == "evidence_insufficient" and items[0].missing
