"""Web 播放器视觉补判的目标和证据边界。"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_player_visual_recheck import (
    WebPlayerVisualRecheckService,
    build_web_player_targets,
)


def _snapshot(*elements, url="https://example.test/watch/1"):
    return {
        "url": url,
        "viewport": {"width": 1920, "height": 1080},
        "elements": list(elements),
    }


def _video(selector="#player", **overrides):
    value = {
        "selector": selector,
        "tag": "video",
        "visible": True,
        "src": "https://example.test/stream.m3u8",
        "rect": {"x": 300, "y": 1350, "width": 1000, "height": 560},
    }
    value.update(overrides)
    return value


def _media(selector="#player", ready_state=4):
    return {
        "schema_version": "media-state.v1",
        "url": "https://example.test/watch/1",
        "items": [{"selector": selector, "tag": "video", "ready_state": ready_state}],
    }


def test_player_target_is_unique_primary_video_correlated_with_media_state():
    targets = build_web_player_targets(_snapshot(_video()), media_state=_media())
    assert len(targets) == 1
    assert targets[0].selector == "#player"
    assert len(targets[0].dom_fingerprint) == 64


def test_player_target_correlates_different_capture_selector_algorithms_by_bounds():
    target = build_web_player_targets(
        _snapshot(_video("html > body > main > article > section > video")),
        media_state={
            "items": [
                {
                    "selector": "body > main > article > section > video",
                    "tag": "video",
                    "ready_state": 4,
                    "bounds": {"x": 300, "y": 1350, "width": 1000, "height": 560},
                }
            ],
        },
    )
    assert len(target) == 1
    assert target[0].media_selector == "body > main > article > section > video"


def test_player_target_rejects_duplicate_selector_or_mismatched_bounds():
    assert build_web_player_targets(_snapshot(_video(), _video()), media_state=_media()) == ()
    assert (
        build_web_player_targets(
            _snapshot(_video()),
            media_state={
                "items": [
                    {
                        "selector": "#player",
                        "tag": "video",
                        "ready_state": 4,
                        "bounds": {"x": 800, "y": 1350, "width": 1000, "height": 560},
                    }
                ]
            },
        )
        == ()
    )


@pytest.mark.parametrize(
    "excluded",
    [
        {"is_advertisement": True},
        {"within_web_navigation": True},
        {"within_footer": True},
        {"within_repeated_structure": True},
        {"class_tokens": ["video-preview"]},
    ],
)
def test_player_target_excludes_secondary_or_preview_media(excluded):
    assert build_web_player_targets(_snapshot(_video(**excluded)), media_state=_media()) == ()


def test_player_target_rejects_multiple_candidates_and_media_mismatch():
    assert (
        build_web_player_targets(
            _snapshot(_video("#first"), _video("#second", dom_fingerprint="second")),
            media_state={
                "items": [
                    {"selector": "#first", "tag": "video", "ready_state": 4},
                    {"selector": "#second", "tag": "video", "ready_state": 4},
                ]
            },
        )
        == ()
    )
    assert build_web_player_targets(_snapshot(_video()), media_state=_media("#other")) == ()
    assert (
        build_web_player_targets(
            _snapshot(_video()),
            media_state={
                "items": _media()["items"]
                + [{"selector": "#other", "tag": "video", "ready_state": 4}]
            },
        )
        == ()
    )
    assert build_web_player_targets(_snapshot(_video()), media_state=None) == ()
    assert (
        build_web_player_targets(
            _snapshot(_video()), media_state={**_media(), "items_truncated": True}
        )
        == ()
    )


@pytest.mark.parametrize(
    "ready_state,control_x,expected_status,expected_reason,model_calls",
    [
        (4, 0.49, "visible", None, 1),
        (1, 0.49, "uncertain", "WEB_PLAYER_VISUAL_MEDIA_NOT_READY", 0),
        (4, 0.05, "uncertain", "WEB_PLAYER_VISUAL_CONTROL_OUTSIDE_TARGET", 1),
        (4, None, "uncertain", "WEB_PLAYER_VISUAL_ASSESSMENT_FAILED", 1),
        (-1, 0.49, "uncertain", "WEB_PLAYER_VISUAL_MEDIA_UNAVAILABLE", 0),
    ],
)
@pytest.mark.asyncio
async def test_visual_recheck_scrolls_to_video_and_records_observation_without_click(
    ready_state,
    control_x,
    expected_status,
    expected_reason,
    model_calls,
):
    before = _snapshot(_video())
    after = _snapshot(_video(rect={"x": 300, "y": 240, "width": 1000, "height": 560}))

    class Evidence:
        async def read_snapshot(self, *, capture_id):
            return {"ready": before, "scrolled": after}[capture_id]

        async def read_media_state(self, *, capture_id):
            if capture_id == "scrolled" and ready_state == -1:
                raise RuntimeError("媒体事实读取失败")
            return _media(ready_state=ready_state if capture_id == "scrolled" else 4)

        async def read_ready(self, *, capture_id, page_role):
            assert page_role == "detail"
            return SimpleNamespace(
                capture_id=capture_id,
                url="https://example.test/watch/1",
                screenshot_artifact_id="screenshot-" + capture_id,
                dom_artifact_id="dom-" + capture_id,
                capture_state_fingerprint="state-" + capture_id,
                interaction_facts=SimpleNamespace(identity_verified=True, readiness_accepted=True),
            )

        async def read_url(self, *, capture_id):
            return "https://example.test/watch/1"

    class Primary:
        async def capture(self, **kwargs):
            assert kwargs["page_type"] == "video_detail"
            assert kwargs["capture_id"] == "ready"
            return SimpleNamespace(status="captured", evidence_capture_id="scrolled")

    class Assessor:
        calls = 0

        async def assess_player_visual(self, **kwargs):
            self.calls += 1
            assert kwargs["screenshot_artifact_id"] == "screenshot-scrolled"
            if control_x is None:
                raise RuntimeError("模型暂不可用")
            return SimpleNamespace(
                status="visible",
                confidence=0.94,
                reason="播放器和播放键可见",
                screenshot_width=1920,
                screenshot_height=1080,
                controls=(
                    SimpleNamespace(
                        kind="play",
                        x=control_x,
                        y=0.47,
                        width=0.04,
                        height=0.06,
                    ),
                ),
            )

    class Writer:
        saved = None

        async def record_player_visual_recheck(self, **kwargs):
            self.saved = kwargs

    writer = Writer()
    assessor = Assessor()
    result = await WebPlayerVisualRecheckService(
        primary_capture=Primary(),
        evidence_source=Evidence(),
        assessor=assessor,
        writer=writer,
    ).assess(
        task_id="task",
        agent_run_id="run",
        session_id="web:run",
        capture_id="ready",
        page_url="https://example.test/watch/1",
        page_type="content_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert result.status == expected_status
    assert result.reason_code == expected_reason
    assert result.evidence_capture_id == "scrolled"
    assert writer.saved["capture_id"] == (
        "ready"
        if expected_reason
        in {"WEB_PLAYER_VISUAL_MEDIA_NOT_READY", "WEB_PLAYER_VISUAL_MEDIA_UNAVAILABLE"}
        else "scrolled"
    )
    assert writer.saved["payload"]["source_capture_id"] == "ready"
    assert assessor.calls == model_calls
    if expected_reason is None:
        assert writer.saved["payload"]["screenshot_artifact_id"] == "screenshot-scrolled"
        assert writer.saved["payload"]["controls"][0]["kind"] == "play"
    else:
        assert writer.saved["payload"]["controls"] == []
    if expected_reason == "WEB_PLAYER_VISUAL_ASSESSMENT_FAILED":
        assert writer.saved["payload"]["screenshot_artifact_id"] == "screenshot-scrolled"


@pytest.mark.asyncio
async def test_visual_recheck_rejects_drift_without_model_call():
    class Evidence:
        async def read_snapshot(self, *, capture_id):
            return (
                _snapshot(_video())
                if capture_id == "ready"
                else _snapshot(_video(), url="https://example.test/other")
            )

        async def read_media_state(self, *, capture_id):
            return _media()

        async def read_ready(self, *, capture_id, page_role):
            return SimpleNamespace(
                capture_id=capture_id,
                url="https://example.test/watch/1",
                screenshot_artifact_id="shot",
                dom_artifact_id="dom",
                capture_state_fingerprint="state",
                interaction_facts=SimpleNamespace(identity_verified=True, readiness_accepted=True),
            )

        async def read_url(self, *, capture_id):
            return (
                "https://example.test/watch/1"
                if capture_id == "ready"
                else "https://example.test/other"
            )

    class Primary:
        async def capture(self, **kwargs):
            return SimpleNamespace(status="captured", evidence_capture_id="scrolled")

    class Assessor:
        async def assess_player_visual(self, **kwargs):
            raise AssertionError("页面漂移后不得送模")

    class Writer:
        payload = None

        async def record_player_visual_recheck(self, **kwargs):
            self.payload = kwargs["payload"]

    writer = Writer()
    result = await WebPlayerVisualRecheckService(
        primary_capture=Primary(),
        evidence_source=Evidence(),
        assessor=Assessor(),
        writer=writer,
    ).assess(
        task_id="task",
        agent_run_id="run",
        session_id="web:run",
        capture_id="ready",
        page_url="https://example.test/watch/1",
        page_type="video_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    assert result.status == "uncertain"
    assert result.reason_code == "WEB_PLAYER_VISUAL_PAGE_CHANGED"
    assert writer.payload["status"] == "uncertain"


@pytest.mark.asyncio
async def test_visual_recheck_does_not_reuse_failed_primary_scroll_intent():
    class Evidence:
        async def read_snapshot(self, *, capture_id):
            return _snapshot(_video())

        async def read_media_state(self, *, capture_id):
            return _media()

        async def read_ready(self, *, capture_id, page_role):
            return SimpleNamespace(
                capture_id=capture_id,
                url="https://example.test/watch/1",
                capture_state_fingerprint="state-ready",
                interaction_facts=SimpleNamespace(
                    identity_verified=True,
                    readiness_accepted=True,
                ),
            )

    class NeverScroll:
        async def capture(self, **kwargs):
            raise AssertionError("先前主体滚动失败后不得重用同一动作意图")

    class NeverAssess:
        async def assess_player_visual(self, **kwargs):
            raise AssertionError("离屏播放器不得送模")

    class Writer:
        payload = None

        async def record_player_visual_recheck(self, **kwargs):
            self.payload = kwargs["payload"]

    writer = Writer()
    result = await WebPlayerVisualRecheckService(
        primary_capture=NeverScroll(),
        evidence_source=Evidence(),
        assessor=NeverAssess(),
        writer=writer,
    ).assess(
        task_id="task",
        agent_run_id="run",
        session_id="web:run",
        capture_id="ready",
        page_url="https://example.test/watch/1",
        page_type="content_detail",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        allow_scroll=False,
    )
    assert result.reason_code == "WEB_PLAYER_VISUAL_SCROLL_SKIPPED_AFTER_PRIMARY_FAILURE"
    assert writer.payload["status"] == "uncertain"
