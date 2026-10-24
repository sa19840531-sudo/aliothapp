"""恢复与自定义播放器的受控反例，不把几何稳定当已加载帧。"""

from copy import deepcopy

import pytest

from services.api.app.application.checkpoint_judgement import measure_checkpoint_series
from services.api.app.application.h5_checkpoint_observation import checkpoint_facts
from services.api.tests.test_h5_checkpoint_consumption import sample


def rendered_sample(instant=1000):
    value = sample(instant=instant)
    value["video_frame"] = {
        "status": "presented",
        "media_instance_id": "media-1",
        "resource_generation": 1,
        "presented_frames": 3,
        "media_time": 5,
        "width": 640,
        "height": 360,
    }
    value["video_region_sha256"] = "b" * 64
    return value


def test_identical_geometry_without_presented_frame_is_unknown():
    values = [sample(), sample(instant=1600)]
    for value in values:
        value.pop("video_frame")
    assert measure_checkpoint_series(values) == {}


@pytest.mark.parametrize("fault", ["missing", "stale_time", "instance", "resource", "size"])
def test_loaded_frame_requires_actual_matching_presentation(fault):
    values = [rendered_sample(), rendered_sample(1600)]
    frame = values[-1]["video_frame"]
    if fault == "missing":
        frame["presented_frames"] = 0
    elif fault == "stale_time":
        frame["media_time"] = 0
    elif fault == "instance":
        frame["media_instance_id"] = "poster-video"
    elif fault == "resource":
        frame["resource_generation"] = 2
    else:
        frame["width"] = 0
    assert measure_checkpoint_series(values) == {}


def test_paused_frame_content_change_is_not_stable():
    values = [rendered_sample(), rendered_sample(1600)]
    values[-1]["video_region_sha256"] = "c" * 64
    measured = measure_checkpoint_series(values)
    assert measured["stable_frame"] is False
    assert measured["media_time_stopped"] is True


def test_loaded_paused_frame_is_stable_with_real_content_and_presentation():
    assert measure_checkpoint_series([rendered_sample(), rendered_sample(1600)])["stable_frame"]


async def test_player_enrichment_cannot_bypass_existing_snapshot_size_limit():
    import json
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.infrastructure.browser.h5_player_frames import attach_player_frames

    snapshot = deepcopy(sample()["snapshot"])
    snapshot["elements"][0]["selector"] = "#movie"
    value = dict(
        selector="#movie",
        media_instance_id="new-native-id",
        frame={"status": "presented"},
        url=snapshot["url"],
        time_origin_ms=10,
        visible=True,
        clear=True,
        rect={},
        custom_control_count=1,
    )
    session = SimpleNamespace(
        send=AsyncMock(
            side_effect=[
                {"frameTree": {"frame": {"id": "frame"}}},
                {"executionContextId": 1},
                {"result": {"value": [value]}},
            ]
        ),
        detach=AsyncMock(),
    )
    page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)))
    original = deepcopy(snapshot)
    result = await attach_player_frames(
        page,
        snapshot,
        max_snapshot_bytes=len(json.dumps(snapshot, ensure_ascii=False).encode()),
    )
    assert result == original


async def test_native_enrichment_replaces_stale_media_time_with_same_observation():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.infrastructure.browser.h5_player_frames import attach_player_frames

    snapshot = deepcopy(sample()["snapshot"])
    snapshot["elements"][0]["selector"] = "#movie"
    media = {**snapshot["elements"][0]["media"], "current_time": 9.8}
    session = SimpleNamespace(send=AsyncMock(side_effect=[
        {"frameTree": {"frame": {"id": "frame"}}}, {"executionContextId": 1},
        {"result": {"value": [dict(
            selector="#movie", media_instance_id="native-1", frame={},
            url=snapshot["url"], time_origin_ms=10, visible=True, clear=True,
            rect={}, custom_control_count=0, media=media,
        )]}},
    ]), detach=AsyncMock())
    page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)))
    result = await attach_player_frames(page, snapshot)
    assert result["elements"][0]["media"]["current_time"] == 9.8
    assert session.send.await_args_list[-1].args[1]["awaitPromise"] is True


@pytest.mark.parametrize("advancing", [True, False])
def test_playing_requires_presented_frame_progress_not_pixel_equality(advancing):
    values = [rendered_sample(), rendered_sample(1600)]
    for value in values:
        value["snapshot"]["elements"][0]["media"]["paused"] = False
    if advancing:
        values[-1]["video_frame"]["presented_frames"] = 5
        values[-1]["video_frame"]["media_time"] = 5.6
        values[-1]["snapshot"]["elements"][0]["media"]["current_time"] = 5.6
        values[-1]["video_region_sha256"] = "c" * 64
        assert measure_checkpoint_series(values)["stable_frame"] is True
    else:
        assert measure_checkpoint_series(values) == {}


@pytest.mark.parametrize("kind", ["native_video", "custom_wrapper", "unbound_wrapper"])
def test_fullscreen_requires_exact_native_media_binding(kind):
    snapshot = deepcopy(sample()["snapshot"])
    snapshot["document_state"] = {
        "fullscreen_active": True,
        "fullscreen_tag": "video" if kind == "native_video" else "div",
        "fullscreen_media_instance_id": "media-1" if kind != "unbound_wrapper" else "other",
        "fullscreen_surface_kind": kind,
    }
    facts = checkpoint_facts(snapshot)
    assert facts.get("fullscreen_active") is (True if kind != "unbound_wrapper" else None)
