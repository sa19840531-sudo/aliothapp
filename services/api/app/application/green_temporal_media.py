"""广告与正片事实判定，分别覆盖结束、关闭、预加载和实际首帧。"""

from services.api.app.application.green_temporal_rules import (
    _covers,
    _gap,
    _identity,
    _number,
    _object,
    _out,
    _rows,
    _window,
)


def judge_media_subitem(key, temporal, view):
    """仅使用已绑定原件的媒体身份和同源时间，不采用播放器摘要替代首帧。"""
    handoff = _object(temporal.get("handoff"))
    if handoff is None or handoff.get("ad_presence") not in {"present", "absent"}:
        return _gap("ad_content_media_identity")
    if handoff["ad_presence"] == "absent":
        return _out("skipped", "原件可靠排除广告，本次不适用广告衔接分项")
    if (
        not all(_identity(handoff.get(name)) for name in ("ad_id", "content_id"))
        or handoff["ad_id"] == handoff["content_id"]
    ):
        return _gap("ad_content_media_identity")
    end = _object(handoff.get("end"))
    if end is None or end.get("media_id") != handoff["ad_id"]:
        return _gap(
            "natural_ad_end_event" if key == "natural_end_handoff" else "formal_ad_close_action"
        )
    kind, at, clock = end.get("kind"), _number(end.get("at_ms")), end.get("clock_id")
    if at is None or not _identity(clock) or kind not in {"ended", "closed"}:
        return _gap("handoff_clock")
    if kind == "closed" and end.get("action_id") not in view.action_ids:
        return _gap("formal_ad_close_action")
    if key in {"natural_end_handoff", "manual_close_handoff"}:
        expected = "ended" if key == "natural_end_handoff" else "closed"
        if kind != expected:
            return _out("skipped", "本次实际广告结束方式未触发当前分支")
    if key == "content_preload":
        return _preload(handoff, temporal, at, clock)
    frame = _object(handoff.get("first_frame"))
    if frame is None:
        return _gap("content_presented_event", "matching_first_frame_png")
    presented = _number(frame.get("at_ms"))
    if (
        frame.get("state") == "render_failed"
        and _identity(frame.get("error_code"))
        and frame.get("content_id") == handoff["content_id"]
        and presented is not None
        and presented >= at
        and frame.get("clock_id") == clock
    ):
        return _out("fail", "同正片的实际呈现事件明确记录首帧渲染失败")
    artifact = frame.get("artifact_id")
    png_ids = view.get("png_artifact_ids", ())
    if (
        frame.get("content_id") != handoff["content_id"]
        or presented is None
        or frame.get("clock_id") != clock
        or presented < at
    ):
        return _gap("content_presented_event", "handoff_clock")
    if (
        not isinstance(png_ids, (list, tuple))
        or not _identity(artifact)
        or artifact not in view.artifact_ids
        or artifact not in png_ids
    ):
        return _gap("matching_first_frame_png")
    owners = _object(view.get("png_capture_ids"))
    capture = frame.get("capture_id")
    event_artifact = frame.get("event_artifact_id")
    events = _rows(view.get("presented_frame_events"))
    if (
        owners is None
        or not _identity(capture)
        or capture not in view.capture_ids
        or owners.get(artifact) != capture
        or not _identity(event_artifact)
        or event_artifact not in view.artifact_ids
        or events is None
    ):
        return _gap("matching_first_frame_native_event")
    matching = tuple(row for row in events if row.get("event_artifact_id") == event_artifact)
    expected = {
        "event_artifact_id": event_artifact,
        "capture_id": capture,
        "artifact_id": artifact,
        "content_id": handoff["content_id"],
        "at_ms": presented,
        "clock_id": clock,
    }
    if (
        len(matching) != 1
        or _number(matching[0].get("at_ms")) is None
        or any(matching[0].get(key) != value for key, value in expected.items())
    ):
        return _gap("matching_first_frame_native_event")
    if not _window(temporal, at, presented, clock):
        return _gap("full_handoff_window")
    if key == "presented_first_frame":
        return _out("pass", "同正片呈现事件已关联实际首帧PNG及同源时间")
    segments = _rows(handoff.get("segments"))
    if segments is None:
        return _gap("full_handoff_window")
    intervals = []
    interruptions = False
    for row in segments:
        a, b = _number(row.get("start_ms")), _number(row.get("end_ms"))
        state = row.get("state")
        if (
            a is None
            or b is None
            or b < a
            or state not in {"ad", "content", "blank", "error", "loading"}
        ):
            return _gap("handoff_segment_identity")
        if row.get("clock_id", clock) != clock:
            return _gap("handoff_clock")
        if (state == "ad" and row.get("media_id") != handoff["ad_id"]) or (
            state == "content" and row.get("media_id") != handoff["content_id"]
        ):
            return _gap("ad_content_media_identity")
        if b > at and a < presented:
            intervals.append((a, b))
            interruptions |= state in {"blank", "error", "loading"}
    if not _covers(intervals, at, presented):
        return _gap("full_handoff_window")
    if interruptions:
        return _out("fail", "完整广告至正片过程实际出现空白、错误或加载中断")
    return _out("pass", "实际结束或关闭后，同广告与正片在完整观察内连续衔接")


def _preload(handoff, temporal, ended_at, clock):
    preload = _object(handoff.get("preload"))
    if preload is None:
        return _gap("content_preload_original")
    if preload.get("content_id") != handoff["content_id"]:
        return _gap("ad_content_media_identity")
    if preload.get("state") == "not_started":
        start = _number(handoff.get("ad_started_at_ms"))
        if start is None or start > ended_at or not _window(temporal, start, ended_at, clock):
            return _gap("content_preload_original")
        return _out("fail", "完整广告过程证实正片未启动预加载")
    start, ready = _number(preload.get("started_at_ms")), _number(preload.get("ready_at_ms"))
    if start is None or preload.get("clock_id") != clock:
        return _gap("content_preload_original")
    if start > ended_at:
        return _out("fail", "同正片直到广告结束或关闭后才开始加载")
    if preload.get("state") == "ready" and ready is not None and ready >= start:
        if ready <= ended_at:
            return _out("pass", "同正片资源在广告结束或关闭前已完成预加载")
        return _out("fail", "同正片资源直到广告结束或关闭后才完成加载")
    if preload.get("state") == "requested" and _window(temporal, start, ended_at, clock):
        return _out("fail", "完整广告窗口内正片仅发起请求，结束前尚未完成预加载")
    return _gap("content_preload_original")
