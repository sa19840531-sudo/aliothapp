"""绿色时序规则共享判定内核，只接受服务端绑定器产出的原件事实视图。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping

from services.api.app.application.green_rule_catalog import GREEN_RULES
from services.api.app.application.green_rule_evidence import _MISSING_FACTS, GreenSubitem

_EMPTY = re.compile(
    r"^(?:暂无(?:数据|内容|记录)|没有(?:数据|内容|记录)|"
    r"no (?:data|results)(?: available| found)?)[。.!！\s]*$",
    re.I,
)
_FAIL = re.compile(
    r"^(?:加载失败|请求失败|网络(?:连接)?异常|网络已断开|连接超时|请求超时|"
    r"network error|failed to load|request timed out)",
    re.I,
)
_CAUSE = re.compile(
    r"^(?:网络(?:连接)?异常|网络已断开|连接超时|请求超时|网络不可用|无网络连接|"
    r"network error|request timed out)",
    re.I,
)
_GUIDE = re.compile(r"(?:请|点击|点此)?(?:刷新|重试|重新加载)|\b(?:retry|refresh|reload)\b", re.I)


def judge_temporal_rule(rule_key, views, subchecks):
    """按实例独立判断再归并；反例优先，其他实例缺项始终保留。"""
    definition = next((row for row in GREEN_RULES if row.key == rule_key), None)
    keys = tuple(subchecks)
    if (
        rule_key not in {"s72", "s49", "s73", "62"}
        or definition is None
        or not keys
        or any(key not in definition.subchecks for key in keys)
        or len(set(keys)) != len(keys)
    ):
        raise ValueError("不支持的绿色时序规则或分项")
    snapshots = tuple(views)
    return tuple(
        _merge(key, tuple(_view(rule_key, key, view) for view in snapshots)) for key in keys
    )


def _view(rule_key, key, view):
    issues = tuple(getattr(view, "issues", ()))
    temporal = view.get("temporal")
    if issues or not isinstance(temporal, Mapping):
        result = _gap(*issues, *_MISSING_FACTS[key])
    else:
        try:
            result = {"s72": _s72, "s49": _s49, "s73": _s73, "62": _media}[rule_key](
                key, temporal, view
            )
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError):
            result = _gap("temporal_fact_shape_invalid")
    status, reason, missing = result
    return GreenSubitem(
        key,
        status,
        reason,
        tuple(dict.fromkeys(missing)),
        tuple(getattr(view, "capture_ids", ())),
        tuple(getattr(view, "action_ids", ())),
    )


def _merge(key, rows):
    if not rows:
        return GreenSubitem(
            key, "evidence_insufficient", "缺少已绑定的业务实例原件", _MISSING_FACTS[key]
        )
    order = ("fail", "manual_review", "evidence_insufficient", "pass", "skipped")
    status = next(value for value in order if any(row.status == value for row in rows))
    return GreenSubitem(
        key,
        status,
        "；".join(dict.fromkeys(row.reason for row in rows)),
        tuple(dict.fromkeys(gap for row in rows for gap in row.missing)),
        tuple(dict.fromkeys(value for row in rows for value in row.capture_ids)),
        tuple(dict.fromkeys(value for row in rows for value in row.action_ids)),
    )


def _out(status, reason):
    return status, reason, ()


def _gap(*codes):
    return "evidence_insufficient", "必要业务身份、原件或完整观察仍有缺项", tuple(codes)


def _object(value):
    return value if isinstance(value, Mapping) else None


def _rows(value):
    return (
        tuple(value)
        if isinstance(value, (list, tuple)) and all(isinstance(row, Mapping) for row in value)
        else None
    )


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) else None


def _identity(value):
    return isinstance(value, str) and bool(value.strip())


def _window(temporal, start=None, end=None, clock=None):
    window = _object(temporal.get("window"))
    if window is None or window.get("coverage") != "complete":
        return False
    a, b = _number(window.get("start_ms")), _number(window.get("end_ms"))
    if a is None or b is None or b < a or not _identity(window.get("clock_id")):
        return False
    if start is not None and end is not None and start > end:
        return False
    return (
        (start is None or a <= start)
        and (end is None or b >= end)
        and (clock is None or window["clock_id"] == clock)
    )


def _state(temporal):
    state = _object(temporal.get("state"))
    if state is None or not all(_identity(state.get(key)) for key in ("module_id", "request_id")):
        return None
    return state


def _messages(state):
    rows = _rows(state.get("messages"))
    if rows is None or any(
        type(row.get("visible")) is not bool or not isinstance(row.get("text"), str) for row in rows
    ):
        return None
    return tuple(row["text"].strip() for row in rows if row["visible"] and row["text"].strip())


def _entry(targets, kinds, temporal):
    rows = _rows(targets)
    if rows is None or any(not _identity(row.get("kind")) for row in rows):
        return _gap("module_recovery_targets")
    matching = tuple(row for row in rows if row["kind"] in kinds)
    identities = [row.get("target_id") for row in matching]
    if any(_identity(value) and identities.count(value) > 1 for value in identities):
        return _gap("recovery_target_identity_ambiguous")
    for row in matching:
        if (
            row.get("visible") is True
            and row.get("enabled") is True
            and row.get("hit") == "target"
            and _identity(row.get("target_id"))
        ):
            return _out("pass", "当前业务模块具有可见且可用的对应入口")
    if any(
        type(row.get("visible")) is not bool
        or type(row.get("enabled")) is not bool
        or row.get("hit") not in {"target", "blocked"}
        for row in matching
    ):
        return _gap("recovery_target_usability")
    return (
        _out("fail", "完整模块原件中未提供可用的对应入口")
        if _window(temporal)
        else _gap("complete_module_rendering")
    )


def _s72(key, temporal, view):
    state = _state(temporal)
    if state is None:
        return _gap("same_module_request_identity")
    outcome = state.get("outcome")
    expected = "empty" if key == "empty_state_meaning" else "failure"
    if outcome in {"empty", "failure", "nonempty"} and outcome != expected:
        return _out("skipped", "真实业务结果未触发当前空态或失败分项")
    if outcome != expected:
        return _gap("actual_module_business_outcome")
    messages = _messages(state)
    if messages is None:
        return _gap("module_rendering")
    empty, failure = (
        any(_EMPTY.match(text) for text in messages),
        any(_FAIL.match(text) for text in messages),
    )
    if expected == "empty" and empty:
        return _out("pass", "同模块真实空结果呈现明确空态说明")
    if expected == "failure":
        if empty and not failure:
            return (
                _out("fail", "同模块真实加载失败被呈现为空数据")
                if _window(temporal)
                else _gap("complete_module_rendering")
            )
        if empty and failure:
            return _out("manual_review", "同模块同时出现空态与失败说明，需要确认其呈现含义")
        entry = _entry(state.get("targets"), {"retry", "refresh"}, temporal)
        if failure and (any(_GUIDE.search(text) for text in messages) or entry[0] == "pass"):
            return _out("pass", "同模块失败呈现明确，并提供刷新或重试引导")
    if not _window(temporal):
        return _gap("complete_module_rendering")
    if not messages:
        return _out("fail", "完整模块呈现缺少当前业务状态的说明")
    return _out("manual_review", "完整呈现文案无法确定是否正确表达业务状态与处理引导")


def _fault(temporal):
    fault = _object(temporal.get("fault"))
    if fault is None or not _identity(fault.get("module_id")):
        return None
    if fault.get("kind") != "none" and not _identity(fault.get("request_id")):
        return None
    return fault


def _s49(key, temporal, view):
    fault = _fault(temporal)
    if fault is None or fault.get("kind") not in {"none", "offline", "timeout", "load_failure"}:
        return _gap("actual_module_fault")
    if fault["kind"] == "none" and key != "payment_not_interrupted":
        if not _window(temporal):
            return _gap("complete_fault_observation")
        return _out("skipped", "当前模块未发生已核验的业务异常")
    business = fault.get("business_type")
    if key == "payment_not_interrupted":
        return _payment(temporal, business)
    if key in {"video_line_recovery", "account_payment_support"}:
        applicable = {"video"} if key == "video_line_recovery" else {"account", "payment"}
        if business in {"video", "account", "payment", "general"} and business not in applicable:
            return _out("skipped", "已核验的业务类型不适用该异常分支")
        if business not in applicable:
            return _gap("trusted_business_type")
    if key == "account_payment_support":
        support = _object(temporal.get("support"))
        return _entry(support.get("targets") if support else None, {"customer_service"}, temporal)
    if key in {"business_recovery", "video_line_recovery"}:
        return _recovery(temporal, fault, view, line=key == "video_line_recovery")
    state = _state(temporal)
    if state is None or any(
        state.get(name) != fault.get(name) for name in ("module_id", "request_id")
    ):
        return _gap("same_fault_rendering_identity")
    if key == "retry_entry":
        return _entry(state.get("targets"), {"retry", "refresh"}, temporal)
    messages = _messages(state)
    if messages is None:
        return _gap("failure_reason_rendering")
    if any(_CAUSE.match(text) for text in messages):
        return _out("pass", "真实异常已在同模块说明网络或超时原因")
    if not _window(temporal):
        return _gap("complete_failure_reason_window")
    if not messages or all(_FAIL.match(text) or _GUIDE.search(text) for text in messages):
        return _out("fail", "完整异常呈现未说明异常原因")
    return _out("manual_review", "异常文案的原因说明语义需要复核")


def _recovery(temporal, fault, view, *, line):
    recovery = _object(temporal.get("recovery"))
    if recovery is None:
        return _gap("same_module_retry_request", "business_recovered_response")
    if (
        recovery.get("source_request_id") != fault["request_id"]
        or not _identity(recovery.get("request_id"))
        or recovery["request_id"] == fault["request_id"]
        or recovery.get("trigger_action_id") not in view.action_ids
    ):
        return _gap("same_module_retry_request")
    if line:
        if not all(_identity(recovery.get(name)) for name in ("line_before", "line_after")):
            return _gap("video_line_identity")
        if recovery["line_before"] == recovery["line_after"]:
            return _gap("actual_line_switch")
    if recovery.get("outcome") == "success":
        return _out("pass", "正式重试或切线后取得同模块真实业务恢复结果")
    if recovery.get("outcome") == "failure":
        return _out("fail", "正式重试或切线后的真实业务结果仍为失败")
    return _gap("business_recovered_response")


def _payment(temporal, business):
    if business in {"general", "video", "account"}:
        return _out("skipped", "已核验的当前业务不是支付")
    payment = _object(temporal.get("payment"))
    if business != "payment" or payment is None:
        return _gap("trusted_payment_stage")
    if payment.get("phase") == "noncritical":
        return _out("skipped", "当前已核验支付阶段不属于关键过程")
    if payment.get("phase") != "critical":
        return _gap("trusted_payment_stage")
    a, b, clock = (
        _number(payment.get("start_ms")),
        _number(payment.get("end_ms")),
        payment.get("clock_id"),
    )
    events = _rows(payment.get("events"))
    if (
        a is None
        or b is None
        or b < a
        or not _identity(clock)
        or events is None
        or not _window(temporal, a, b, clock)
    ):
        return _gap("complete_support_interruption_window")
    for event in events:
        at = _number(event.get("at_ms"))
        if (
            at is None
            or event.get("clock_id") != clock
            or event.get("kind")
            not in {
                "support_open",
                "support_close",
                "business_blocked",
                "payment_progress",
                "payment_complete",
            }
        ):
            return _gap("support_event_clock")
        if event["kind"] == "business_blocked" and (
            not _identity(event.get("caused_by")) or event.get("origin") not in {"system", "user"}
        ):
            return _gap("support_interruption_causality")
    for event in events:
        at = event["at_ms"]
        if (
            a <= at <= b
            and event["kind"] == "business_blocked"
            and event.get("caused_by") == "support"
            and event.get("origin") == "system"
        ):
            return _out("fail", "支付关键窗口内客服流程实际阻断了业务")
    return _out("pass", "完整支付关键窗口未出现客服造成的实际业务阻断")


def _s73(key, temporal, view):
    if key == "loading_feedback":
        return _loading(temporal)
    if key == "business_success_feedback":
        return _success(temporal)
    state = _state(temporal)
    if state is None:
        return _gap("actual_empty_response")
    if state.get("outcome") in {"nonempty", "failure"}:
        return _out("skipped", "当前真实结果不为空数据")
    if state.get("outcome") != "empty":
        return _gap("actual_empty_response")
    illustrations = _rows(state.get("illustrations"))
    if illustrations is not None and any(
        row.get("visible") is True
        and row.get("role") == "empty_state"
        and row.get("image_state") in {"decoded", "rendered"}
        for row in illustrations
    ):
        return _out("pass", "真实空态有实际呈现的对应插图")
    entry = _entry(state.get("targets"), {"guidance"}, temporal)
    if entry[0] == "pass":
        return _out("pass", "真实空态提供可用的操作引导")
    if illustrations is None or entry[0] == "evidence_insufficient":
        return _gap("same_module_illustration_or_guidance")
    if any(
        row.get("image_state") not in {"decoded", "rendered", "failed"} for row in illustrations
    ):
        return _gap("empty_illustration_rendering")
    return _out("fail", "完整真实空态既无有效插图，也无可用引导")


def _loading(temporal):
    loading = _object(temporal.get("loading"))
    if loading is None:
        return _gap("same_module_loading_window")
    a, b, clock = (
        _number(loading.get("start_ms")),
        _number(loading.get("end_ms")),
        loading.get("clock_id"),
    )
    rows = _rows(loading.get("feedback"))
    if (
        a is None
        or b is None
        or b < a
        or not _identity(clock)
        or rows is None
        or not _window(temporal, a, b, clock)
    ):
        return _gap("complete_progress_feedback_observation")
    if a == b:
        return _out("skipped", "原生事件证明本次没有持续加载区间")
    intervals = []
    for row in rows:
        start, end = _number(row.get("start_ms")), _number(row.get("end_ms"))
        if (
            start is None
            or end is None
            or end < start
            or type(row.get("visible")) is not bool
            or row.get("clock_id", clock) != clock
            or row.get("kind") not in {"progress", "spinner", "skeleton", "text"}
        ):
            return _gap("loading_feedback_intervals")
        if row["visible"] and row.get("kind") in {"progress", "spinner", "skeleton", "text"}:
            intervals.append((start, end))
    if _covers(intervals, a, b):
        return _out("pass", "完整加载区间持续有可见进度或加载反馈")
    return _out("fail", "完整加载区间存在没有进度或加载反馈的过程")


def _success(temporal):
    success = _object(temporal.get("success"))
    if success is None:
        return _gap("trusted_business_success")
    if success.get("outcome") == "failure" and _identity(success.get("event_id")):
        return _out("skipped", "真实业务结果为失败，不适用成功反馈分项")
    if success.get("outcome") != "success" or not _identity(success.get("event_id")):
        return _gap("trusted_business_success")
    at, clock = _number(success.get("at_ms")), success.get("clock_id")
    rows, baseline = _rows(success.get("feedback")), success.get("baseline_ids")
    if (
        at is None
        or not _identity(clock)
        or rows is None
        or not isinstance(baseline, (list, tuple))
        or not all(_identity(v) for v in baseline)
        or not _window(temporal, at, at, clock)
    ):
        return _gap("new_feedback_window")
    for row in rows:
        when = _number(row.get("at_ms"))
        if (
            when is None
            or row.get("clock_id") != clock
            or not _identity(row.get("event_id"))
            or type(row.get("visible")) is not bool
            or row.get("kind") not in {"toast", "animation", "message"}
        ):
            return _gap("native_click_request_clock")
        if row.get("kind") in {"toast", "message"} and not _identity(row.get("text")):
            return _gap("success_feedback_message_original")
    for row in rows:
        when = row["at_ms"]
        if (
            row["visible"]
            and row.get("kind") in {"toast", "animation", "message"}
            and row.get("business_event_id") == success["event_id"]
            and row["event_id"] not in baseline
            and when >= at
            and _window(temporal, when, when, clock)
        ):
            if row["kind"] != "animation" and re.search(
                r"失败|错误|\b(?:error|failed|failure)\b", row["text"], re.I
            ):
                return _out("fail", "真实业务成功后同次新反馈却明确显示失败或错误")
            return _out("pass", "真实业务成功后出现同次关联的新提示或动效")
    return _out("fail", "完整业务成功窗口未出现本次关联的新提示或动效")


def _covers(intervals, start, end):
    position = start
    for a, b in sorted(intervals):
        if b <= position:
            continue
        if a > position:
            return False
        position = b
        if position >= end:
            return True
    return position >= end


def _media(key, temporal, view):
    from services.api.app.application.green_temporal_media import judge_media_subitem

    return judge_media_subitem(key, temporal, view)
