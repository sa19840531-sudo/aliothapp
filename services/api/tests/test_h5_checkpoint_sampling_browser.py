"""自有禁网 Chromium 的真实原生播放器采样；不是原站视频详情验收。"""

import json

import pytest

from services.api.app.application.checkpoint_judgement import measure_checkpoint_series
from services.api.app.application.h5_capture_readiness import h5_capture_is_inspection_ready
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    _collect_artifacts,
    _collect_ui_snapshot,
)
from services.api.tests.test_h5_probe_journey_browser import page  # noqa: F401 - 自有禁网浏览器


def publication(artifacts):
    observation = next(
        item.content for item in artifacts if item.artifact_type == "checkpoint_observation"
    )
    return {**observation["publication"], "samples": observation["samples"]}


@pytest.mark.parametrize("loaded", [True, False])
async def test_ordinary_video_primary_is_the_last_observed_frame_not_the_old_image(page, loaded):  # noqa: F811 - 复用 fixture
    await page.set_content('<canvas width="640" height="360" hidden></canvas>'
                           '<video style="width:360px;height:200px" muted controls></video>')
    if loaded:
        await page.evaluate("""() => {
            const c=document.querySelector('canvas'),v=document.querySelector('video');
            setInterval(()=>{const x=c.getContext('2d');x.fillStyle='green';
              x.fillRect(0,0,640,360)},50);
            v.srcObject=c.captureStream(20);v.play();
        }""")
        await page.wait_for_function("document.querySelector('video').readyState>=2")
        await _collect_ui_snapshot(page, surface_session_id="h5:ordinary-isolated")
        await page.evaluate(
            "new Promise(resolve=>document.querySelector('video')"
            ".requestVideoFrameCallback(resolve))"
        )
        await page.evaluate("document.querySelector('video').pause()")
    artifacts = await _collect_artifacts(page, request=PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE, session_id="h5:ordinary-isolated",
    ))
    contract = publication(artifacts)
    primary = next(x.content for x in artifacts if x.artifact_type == "screenshot")
    assert primary["frame_origin"]["screenshot_sha256"] == contract["primary_screenshot_sha256"]
    assert contract["primary_screenshot_sha256"] == contract["samples"][-1]["screenshot_sha256"]
    checksums = ["sha256:" + x.content["frame_origin"]["screenshot_sha256"]
                 for x in artifacts if x.artifact_type == "screenshot"]
    assert h5_capture_is_inspection_ready(
        {"pipeline_metrics": {"page_inspection_ready": True, "h5_video_publication": contract}},
        page_state="stable", page_type="video_detail", screenshot_checksums=checksums,
    ) is loaded


async def test_real_post_pause_capture_retains_samples_and_actual_pngs(page):  # noqa: F811 - 复用 fixture
    await page.set_content("""<!doctype html><canvas width="640" height="360" hidden></canvas>
        <video style="width:360px;height:200px" muted controls></video>
        <script>
        const c=document.querySelector('canvas'), v=document.querySelector('video');
        const ctx=c.getContext('2d');
        setInterval(()=>{ctx.fillStyle='rgb(30,150,100)';ctx.fillRect(0,0,640,360)},50);
        v.srcObject=c.captureStream(20);v.play();
        </script>""")
    await page.wait_for_function("document.querySelector('video').readyState>=2")
    await _collect_ui_snapshot(page, surface_session_id="h5:checkpoint-isolated")
    await page.evaluate(
        "new Promise(resolve=>document.querySelector('video').requestVideoFrameCallback(resolve))"
    )
    await page.evaluate("document.querySelector('video').pause()")
    artifacts = await _collect_artifacts(
        page,
        request=PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="h5:checkpoint-isolated",
            capture_checkpoint_observation=True,
            capture_media_state=True,
        ),
    )
    package = next(
        item.content for item in artifacts if item.artifact_type == "checkpoint_observation"
    )
    measured = measure_checkpoint_series(package["samples"])
    assert measured, package
    assert measured["stable_frame"] is True
    assert measured["media_time_stopped"] is True
    frames = [
        item
        for item in artifacts
        if item.artifact_type == "screenshot"
        and item.content.get("purpose") == "checkpoint_post_action_sample"
    ]
    assert all(
        frame.content["frame_origin"]["screenshot_sha256"] == sample["screenshot_sha256"]
        for frame, sample in zip(frames, package["samples"], strict=True)
    )
    assert (
        len(
            [
                item
                for item in artifacts
                if item.artifact_type == "screenshot"
                and item.content.get("purpose") == "checkpoint_post_action_sample"
            ]
        )
        == 2
    )


async def test_empty_loading_video_never_claims_stable_or_time_stopped(page):  # noqa: F811 - 复用 fixture
    await page.set_content('<video style="width:360px;height:200px" controls></video>')
    artifacts = await _collect_artifacts(
        page,
        request=PageCaptureRequest(
            mode=PageCaptureMode.OBSERVE,
            session_id="h5:checkpoint-isolated",
            capture_checkpoint_observation=True,
        ),
    )
    package = next(
        item.content for item in artifacts if item.artifact_type == "checkpoint_observation"
    )
    assert measure_checkpoint_series(package["samples"]) == {}


@pytest.mark.parametrize("placement", ["footer", "offscreen", "hidden"])
async def test_non_business_video_does_not_take_over_payment_display_capture(page, placement):  # noqa: F811 - 复用 fixture
    html = '<video style="width:360px;height:200px"></video>'
    if placement == "footer":
        html = "<footer>" + html + "</footer>"
    elif placement == "offscreen":
        html = '<div style="position:absolute;top:2000px">' + html + "</div>"
    else:
        html = '<div hidden>' + html + "</div>"
    await page.set_content('<main><h1>充值展示</h1><button>查看金额</button></main>' + html)
    artifacts = await _collect_artifacts(page, request=PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE, session_id="h5:ordinary-payment",
    ))
    assert not any(item.artifact_type == "checkpoint_observation" for item in artifacts)


async def test_first_ordinary_playing_capture_needs_no_probe_or_autoplay_from_sampler(page):  # noqa: F811 - 复用 fixture
    await page.set_content('''<canvas width="640" height="360" hidden></canvas>
      <video style="width:360px;height:200px" muted></video><script>
      const c=document.querySelector('canvas'),v=document.querySelector('video');
      setInterval(()=>{const x=c.getContext('2d');x.fillStyle='green';
        x.fillRect(0,0,640,360)},50);v.srcObject=c.captureStream(20);v.play();</script>''')
    await page.wait_for_function("document.querySelector('video').readyState>=2")
    artifacts = await _collect_artifacts(page, request=PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE, session_id="h5:first-playing",
    ), logical_surface_enabled=True)
    snapshot = next(x.content for x in artifacts if x.artifact_type == "ui_snapshot")
    assert "logical_surface" in snapshot
    assert "h5_video_publication" not in snapshot
    measured = measure_checkpoint_series(publication(artifacts)["samples"])
    assert measured.get("loaded_frame") is True, publication(artifacts)


@pytest.mark.parametrize("running", [True, False])
async def test_first_native_observation_waits_only_for_actual_playing_frame(page, running):  # noqa: F811 - 复用 fixture
    await page.set_content('''<canvas width="640" height="360" hidden></canvas>
      <video style="width:360px;height:200px" muted></video>''')
    if running:
        await page.evaluate('''() => {
          const c=document.querySelector('canvas'),v=document.querySelector('video');
          setInterval(()=>{const x=c.getContext('2d');x.fillStyle='green';
            x.fillRect(0,0,640,360)},50);
          v.srcObject=c.captureStream(20);return v.play();}''')
        await page.wait_for_function("document.querySelector('video').readyState>=2")
    await page.evaluate('''() => {
      const v=document.querySelector('video');window.samplerPlayCalls=0;
      v.play=()=>{window.samplerPlayCalls++;throw Error('不得主动播放')};}''')
    snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:first-native")
    video = next(x for x in snapshot["elements"] if x["tag"] == "video")
    assert bool(video.get("h5_video_frame")) is running, video
    assert await page.evaluate("window.samplerPlayCalls") == 0


async def test_video_samples_do_not_expand_near_limit_ui_snapshot(page, monkeypatch):  # noqa: F811 - 复用 fixture
    from services.api.app.infrastructure.browser import page_capture

    await page.set_content('<video style="width:360px;height:200px" controls></video>')
    original = page_capture._collect_ui_snapshot

    async def read(surface, **kwargs):
        snapshot = await original(surface, **kwargs)
        used = len(json.dumps(snapshot, ensure_ascii=False).encode())
        snapshot["controlled_padding"] = "x" * (page_capture.MAX_UI_SNAPSHOT_BYTES - used - 100)
        return snapshot

    monkeypatch.setattr(page_capture, "_collect_ui_snapshot", read)
    artifacts = await _collect_artifacts(page, request=PageCaptureRequest(
        mode=PageCaptureMode.OBSERVE, session_id="h5:near-limit-video",
    ))
    snapshot = next(item.content for item in artifacts if item.artifact_type == "ui_snapshot")
    assert (len(json.dumps(snapshot, ensure_ascii=False).encode())
            <= page_capture.MAX_UI_SNAPSHOT_BYTES)
    assert "h5_video_publication" not in snapshot
    contract = publication(artifacts)
    assert len(contract["samples"]) == 2
    assert not any("controlled_padding" in item["snapshot"] for item in contract["samples"])


@pytest.mark.parametrize("fill,bound", [(True, True), (False, True), (True, False)])
async def test_custom_controls_and_wrapper_fullscreen_bind_actual_native_video(page, fill, bound):  # noqa: F811 - 复用 fixture
    await page.set_content("""<section id="player"><canvas width="640" height="360" hidden></canvas>
      <video id="movie" style="width:360px;height:200px" muted></video>
      <button aria-controls="movie" onclick="document.querySelector('video').pause()">暂停</button>
      </section><script>
      const c=document.querySelector('canvas'),v=document.querySelector('video');
      setInterval(()=>{const ctx=c.getContext('2d');ctx.fillStyle='green';
        ctx.fillRect(0,0,640,360)},50);
      v.srcObject=c.captureStream(20);v.play();</script>""")
    await page.wait_for_function("document.querySelector('video').readyState>=2")
    if not bound:
        await page.locator("button").evaluate(
            "node=>node.setAttribute('aria-controls','other-video')"
        )
    before = await _collect_ui_snapshot(page, surface_session_id="h5:custom-frame")
    video = next(x for x in before["elements"] if x["tag"] == "video")
    assert video["h5_player_presentation"]["custom_control_count"] == int(bound)
    await page.evaluate(
        "new Promise(resolve=>document.querySelector('video').requestVideoFrameCallback(resolve))"
    )
    await page.locator("button").click()
    if fill:
        await page.add_style_tag(
            content="#player:fullscreen video {width:100vw!important;height:90vh!important}"
        )
    await page.evaluate("document.querySelector('#player').requestFullscreen()")
    snapshot = await _collect_ui_snapshot(page, surface_session_id="h5:custom-frame")
    from services.api.app.application.h5_checkpoint_observation import checkpoint_facts

    assert checkpoint_facts(snapshot).get("fullscreen_active") is (True if fill and bound else None)
    current = next(x for x in snapshot["elements"] if x["tag"] == "video")
    assert current["h5_media_instance_id"] == video["h5_media_instance_id"]
    assert current["h5_video_frame"]["presented_frames"] > 0


async def test_real_native_fullscreen_and_exit_keep_exact_video_identity(page, tmp_path):  # noqa: F811 - 复用 fixture
    from services.api.app.application.h5_checkpoint_observation import checkpoint_facts

    await page.set_content('''<main><canvas width="640" height="360" hidden></canvas>
      <video id="movie" style="width:360px;height:200px" muted controls></video></main>
      <script>const c=document.querySelector('canvas'),v=document.querySelector('video');
      setInterval(()=>{const x=c.getContext('2d');x.fillStyle='green';
        x.fillRect(0,0,640,360)},50);v.srcObject=c.captureStream(20);v.play();</script>''')
    await page.wait_for_function("document.querySelector('video').readyState>=2")
    before = await _collect_ui_snapshot(page, surface_session_id="h5:native-fullscreen")
    await page.evaluate("document.querySelector('video').requestFullscreen()")
    fullscreen = await _collect_ui_snapshot(page, surface_session_id="h5:native-fullscreen")
    state = fullscreen["document_state"]
    assert state["fullscreen_surface_kind"] == "native_video"
    assert state["fullscreen_media_instance_id"] == checkpoint_facts(before)["media_instance_id"]
    assert checkpoint_facts(fullscreen)["fullscreen_active"] is True
    (tmp_path / "native-fullscreen.json").write_text(
        json.dumps(fullscreen, ensure_ascii=False), encoding="utf-8",
    )
    await page.screenshot(path=str(tmp_path / "native-fullscreen.png"))
    await page.evaluate("document.exitFullscreen()")
    restored = await _collect_ui_snapshot(page, surface_session_id="h5:native-fullscreen")
    assert checkpoint_facts(restored)["fullscreen_active"] is False
    assert (
        checkpoint_facts(restored)["media_instance_id"]
        == checkpoint_facts(before)["media_instance_id"]
    )
