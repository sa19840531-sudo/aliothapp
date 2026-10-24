"""S35交互布局与重复稳定性的共享纯判定；事实归属由正式绑定读侧核验。"""

from __future__ import annotations

from collections.abc import Mapping

from services.api.app.application.green_interaction_fact_values import (
    _canonical,
    _ids,
    _time,
    _window,
)
from services.api.app.application.green_interaction_layout import _layout
from services.api.app.application.green_rule_evidence import GreenSubitem


def judge_interaction_subitem(key, views):
    """逐业务对象判定，明确反例优先且保留其他对象的缺项。"""
    if key not in {"correct_response", "usable_layout", "comparable_state_stability"}:
        raise ValueError("未声明的S35分项")
    outcomes, missing, captures, actions = [], [], [], []
    for view in views:
        captures.extend(view.capture_ids)
        actions.extend(view.action_ids)
        if view.issues:
            missing.extend(view.issues)
            continue
        try:
            handler = {
                "correct_response": _correct_response,
                "usable_layout": _layout,
                "comparable_state_stability": _stability,
            }[key]
            status, reason, gaps = handler(view)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            status, reason, gaps = (
                "evidence_insufficient",
                "原始交互事实结构无效",
                ["valid_raw_facts"],
            )
        outcomes.append((status, str(view.subject_id) + "：" + reason))
        missing.extend(str(view.subject_id) + ":" + item for item in gaps)
    if not outcomes:
        missing.append(
            {
                "correct_response": "declared_formal_expected_response",
                "usable_layout": "complete_interactive_layout",
                "comparable_state_stability": "independent_comparable_preconditions",
            }[key]
        )
    status = (
        "fail"
        if any(item[0] == "fail" for item in outcomes)
        else "evidence_insufficient"
        if missing or any(item[0] == "evidence_insufficient" for item in outcomes)
        else "pass"
        if any(item[0] == "pass" for item in outcomes)
        else "skipped"
    )
    return GreenSubitem(
        key,
        status,
        "；".join(dict.fromkeys(item[1] for item in outcomes)) or "尚缺正式交互事实",
        tuple(dict.fromkeys(missing)),
        tuple(dict.fromkeys(captures)),
        tuple(dict.fromkeys(actions)),
    )


def _correct_response(view):
    records = view.get("response_actions", view.get("repeat_actions"))
    if len(view.capture_ids) < 2 or not isinstance(records, (list, tuple)) or not records:
        return (
            "evidence_insufficient",
            "尚缺正式动作及声明的预期响应",
            ["declared_formal_expected_response"],
        )
    ids = [record.get("action_id") for record in records if isinstance(record, Mapping)]
    if len(ids) != len(records) or not _ids(ids) or not set(ids).issubset(view.action_ids):
        return (
            "evidence_insufficient",
            "动作身份未绑定正式台账或被重复使用",
            ["independent_formal_actions"],
        )
    statuses, gaps = [], []
    for record in records:
        action_id, window = record["action_id"], record.get("response_window")
        expected, actual = record.get("expected_response"), record.get("response")
        start, end = record.get("started_at_ms"), record.get("finished_at_ms")
        if (
            not _window(window)
            or not _time(start)
            or not _time(end)
            or not start < end
            or not window["start_ms"] <= start < end <= window["end_ms"]
            or window["clock_id"] != record.get("clock_id")
            or not record.get("session_id")
            or not record.get("target_id")
            or record.get("operation") not in {"click", "tap"}
            or not _native_input(record)
            or not _response_shape(expected, observed=False)
            or not _response_shape(actual, observed=True)
            or not start <= actual["observed_at_ms"] <= window["end_ms"]
        ):
            gaps.append(action_id + ":complete_expected_and_native_response_binding")
            continue
        if expected["kind"] != actual["kind"] or expected["subject_id"] != actual["subject_id"]:
            statuses.append("fail")
        elif not set(expected["state"]).issubset(actual["state"]):
            gaps.append(action_id + ":required_response_state_fields")
        else:
            statuses.append(
                "pass"
                if all(
                    _canonical(value) == _canonical(actual["state"][key])
                    for key, value in expected["state"].items()
                )
                else "fail"
            )
    status = (
        "fail"
        if "fail" in statuses
        else "evidence_insufficient"
        if gaps or not statuses
        else "pass"
    )
    return (
        status,
        (
            "完整响应窗口内的实际对象或状态与正式动作预期不一致"
            if status == "fail"
            else "正式动作的实际对象及全部声明状态均与冻结预期一致"
            if status == "pass"
            else "预期合同、原生响应或完整因果窗口仍不完整"
        ),
        gaps,
    )


def _response_shape(value, *, observed):
    return (
        isinstance(value, Mapping)
        and isinstance(value.get("kind"), str)
        and value["kind"]
        and isinstance(value.get("subject_id"), str)
        and value["subject_id"]
        and isinstance(value.get("state"), Mapping)
        and value["state"]
        and (not observed or _time(value.get("observed_at_ms")))
    )


def _native_input(record):
    event = record.get("input_event")
    return (
        isinstance(event, Mapping)
        and event.get("source") in {"browser_native_event", "device_native_input"}
        and (event["source"] != "browser_native_event" or event.get("is_trusted") is True)
        and event.get("action_id") == record["action_id"]
        and event.get("operation") == record.get("operation")
        and event.get("target_id") == record.get("target_id")
        and event.get("clock_id") == record.get("clock_id")
        and _time(event.get("at_ms"))
        and record["started_at_ms"] <= event["at_ms"] <= record["finished_at_ms"]
    )


def _stability(view):
    records, safety = view.get("repeat_actions"), view.get("repeat_safety")
    if safety not in {"read_only", "resettable"}:
        return "evidence_insufficient", "操作尚未证明可安全重复", ["safe_repeat_policy"]
    if len(view.capture_ids) < 2 or not isinstance(records, (list, tuple)) or len(records) < 2:
        return "evidence_insufficient", "尚缺至少两次独立正式操作", ["complete_repeated_actions"]
    if not all(isinstance(record, Mapping) for record in records):
        return "evidence_insufficient", "重复动作记录结构无效", ["formal_repeated_action_records"]
    ids = [record.get("action_id") for record in records]
    if not _ids(ids) or not set(ids).issubset(view.action_ids):
        return (
            "evidence_insufficient",
            "动作身份未绑定正式台账或被重复使用",
            ["independent_formal_actions"],
        )
    expected, observed, clock, session, target, operation = None, [], None, None, None, None
    gaps, previous_end, used_ids = [], None, set(ids)
    for record in records:
        action_id = record["action_id"]
        start, end = record.get("started_at_ms"), record.get("finished_at_ms")
        pre, response, window = (
            record.get("preconditions"),
            record.get("response"),
            record.get("response_window"),
        )
        if (
            not _time(start)
            or not _time(end)
            or start >= end
            or not _window(window)
            or window["start_ms"] > start
            or window["end_ms"] < end
            or record.get("clock_id") != window["clock_id"]
            or not isinstance(record.get("session_id"), str)
            or not record["session_id"]
            or not isinstance(record.get("target_id"), str)
            or not record["target_id"]
            or record.get("operation") not in {"click", "tap"}
            or not _native_input(record)
        ):
            gaps.append(action_id + ":complete_same_clock_action_window")
            continue
        signature = _preconditions(pre)
        if signature is None:
            gaps.append(action_id + ":complete_comparable_preconditions")
            continue
        if expected is None:
            expected, clock, session, target, operation = (
                signature,
                record["clock_id"],
                record["session_id"],
                record["target_id"],
                record["operation"],
            )
        elif (
            signature != expected
            or record["clock_id"] != clock
            or record["session_id"] != session
            or record["target_id"] != target
            or record["operation"] != operation
        ):
            gaps.append(action_id + ":incomparable_preconditions_or_operation")
            continue
        if previous_end is not None:
            reset = record.get("reset")
            if (
                not isinstance(reset, Mapping)
                or reset.get("action_id") not in view.action_ids
                or reset.get("clock_id") != clock
                or reset.get("session_id") != session
                or reset["action_id"] in used_ids
                or not _time(reset.get("started_at_ms"))
                or not _time(reset.get("finished_at_ms"))
                or not previous_end <= reset["started_at_ms"] < reset["finished_at_ms"] <= start
                or _canonical(reset.get("restored_values")) != _canonical(pre["values"])
            ):
                gaps.append(action_id + ":independent_reset_to_same_preconditions")
                continue
            used_ids.add(reset["action_id"])
        previous_end = window["end_ms"]
        if (
            not _response_shape(response, observed=True)
            or response.get("subject_id") != target
            or not start <= response["observed_at_ms"] <= window["end_ms"]
        ):
            gaps.append(action_id + ":causal_complete_response_state")
            continue
        observed.append(_canonical({"kind": response["kind"], "state": response["state"]}))
    status = (
        "fail"
        if len(set(observed)) > 1
        else "evidence_insufficient"
        if gaps or len(observed) < 2
        else "pass"
    )
    reason = (
        "相同完整前态和独立重置后的相同操作产生不同响应"
        if status == "fail"
        else "独立重置恢复完整相同前态后，各次相同操作的实际响应一致"
        if status == "pass"
        else "仍缺完整可比前态、独立重置或响应窗口"
    )
    return status, reason, gaps


def _preconditions(value):
    if (
        not isinstance(value, Mapping)
        or not isinstance(value.get("schema_id"), str)
        or not value["schema_id"]
        or value.get("complete") is not True
        or not _ids(value.get("required_keys"))
        or not isinstance(value.get("values"), Mapping)
        or set(value["values"]) != set(value["required_keys"])
    ):
        return None
    return _canonical({"schema_id": value["schema_id"], "values": value["values"]})
