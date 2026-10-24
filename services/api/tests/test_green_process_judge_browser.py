"""禁网 Chromium 消费真实采集函数的受控 DOM；不代表线上业务采集验收。"""

import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from services.api.app.application.h5_probe_targets import probe_subject_key, select_probe_target
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.tests.test_green_process_judge import judge, original, process_case
from services.api.tests.test_h5_probe_journey_browser import execute
from services.api.tests.test_h5_probe_journey_browser import page as page


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("correct", "pass"),
        ("invisible_expanded_region", "evidence_insufficient"),
        ("no_response", "evidence_insufficient"),
    ],
)
async def test_existing_native_probe_originals_support_only_the_actual_local_effect(
    page, mode, expected
):
    await page.set_content("""<style>body{margin:0}.van-collapse-item{position:fixed;top:40px;
      left:20px;width:250px} #panel{height:0;opacity:0;background:#abc}</style>
      <main style="height:1800px">列表</main><div class="van-collapse-item">
      <button id="button" role="button" class="van-collapse-item__title" aria-expanded="false"
      aria-controls="panel">展开目录</button><div id="panel">目录说明</div></div>""")
    await page.evaluate(
        """mode=>{
      document.getElementById('button').onclick=()=>{
        if(mode==='no_response')return;
        document.getElementById('button').setAttribute('aria-expanded','true');
        panel.style.height='100px';panel.style.opacity=mode==='correct'?'1':'0';
      };window.scrollTo(0,1200);
    }""",
        mode,
    )
    before = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    subject = select_probe_target(before, goal="scroll_button_feedback", page_type="video_list")
    assert subject is not None
    started_at = datetime.now(UTC)
    trace, _, _ = await execute(page, "scroll_button_feedback", "video_list")
    finished_at = datetime.now(UTC)
    after = await _collect_ui_snapshot(page, surface_session_id="h5:fixture")
    fixture, evidence = process_case()
    action = evidence.actions[0]
    intent = json.loads(action.intent_payload)
    intent["session_id"] = "h5:fixture"
    intent["parameters"]["probe_subject_key"] = probe_subject_key(subject)
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
    evidence = replace(evidence, captures=captures, actions=(action,))
    item = judge(fixture, evidence)[0]
    assert item.status == expected, item
