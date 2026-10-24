"""验收证据关联：仅使用持久化结构事实，不读取原始 DOM 或异常正文。"""

import json
from dataclasses import asdict

from scripts.validation.web_acceptance_reporter import classify_validation_failure


def decoded(value):
    return json.loads(value) if isinstance(value, str) else value


def full_page_associations(captures, artifacts, phases):
    """补图须具备正式阶段、父子 Capture 与 PNG；滚动模式还须关联清单。"""
    captures_by_id = {row["capture_id"]: row for row in captures}
    artifacts_by_id = {row["artifact_id"]: row for row in artifacts}
    associations = {}
    for phase in phases:
        ids = decoded(phase.get("evidence_artifact_ids")) or []
        if (
            phase.get("phase_key") != "web_full_page_supplemental"
            or phase.get("phase_protocol_version") != "web-full-page-supplemental.v1"
            or phase.get("status") != "completed"
            or not isinstance(ids, list)
            or len(ids) not in {1, 2}
        ):
            continue
        artifact = artifacts_by_id.get(ids[0], {})
        child = captures_by_id.get(artifact.get("capture_id"), {})
        parent = phase.get("source_capture_id")
        # 新单图需有长图来源标记；无标记的历史单图继续可读。
        if len(ids) == 1 and artifact.get("screenshot_purpose") not in {
            None,
            "web_direct_full_page",
        }:
            continue
        if len(ids) == 2:
            manifest = artifacts_by_id.get(ids[1], {})
            metadata = decoded(manifest.get("coverage_metadata")) or {}
            if (
                not isinstance(metadata, dict)
                or manifest.get("artifact_type") != "page_coverage"
                or str(manifest.get("content_type", "")).split(";", 1)[0].lower()
                != "application/json"
                or manifest.get("capture_id") != artifact.get("capture_id")
                or metadata.get("protocol_version") != "web-scrolled-viewport-manifest.v1"
                or metadata.get("capture_mode") != "scroll_offset_viewport"
                or metadata.get("associated_page_capture_id") != artifact.get("capture_id")
                or metadata.get("associated_screenshot_artifact_ids") != [ids[0]]
            ):
                continue
        if (
            parent not in captures_by_id
            or not child
            or artifact.get("artifact_type") != "screenshot"
            or str(artifact.get("content_type", "")).split(";", 1)[0].lower() != "image/png"
            or child.get("parent_capture_id") != parent
            or child.get("page_parent_capture_id") != parent
            or child.get("role") != "supplemental"
            or child.get("page_role") != "supplemental"
            or child.get("capture_purpose") != "web_full_page_supplemental"
            or child.get("page_capture_purpose") != "web_full_page_supplemental"
            or phase.get("finalization_epoch") is None
            or str(child.get("finalization_epoch")) != str(phase["finalization_epoch"])
            or str(child.get("page_finalization_epoch")) != str(phase["finalization_epoch"])
            or type(phase.get("command_sequence")) is not int
            or phase["command_sequence"] < 1
            or child.get("command_sequence") != phase.get("command_sequence")
        ):
            continue
        associations[child["capture_id"]] = ids[0]
    return associations


def obstruction_witness(raw):
    """多层关闭必须已恢复稳定；单纯启用清障或未清除阻断不计成功见证。"""
    chain = decoded(raw) or {}
    if not isinstance(chain, dict):
        return {}
    result = {
        key: chain[key]
        for key in ("enabled", "detected", "remaining_obstruction")
        if type(chain.get(key)) is bool
    }
    if type(chain.get("dismissal_count")) is int and chain["dismissal_count"] >= 0:
        result["dismissal_count"] = chain["dismissal_count"]
    if chain.get("outcome") in {
        "stable",
        "duration_limit_reached",
        "dismissal_limit_reached",
        "llm_fallback_required",
    }:
        result["outcome"] = chain["outcome"]
    return result


def popup_completed(witness):
    return (
        witness.get("enabled") is True
        and witness.get("dismissal_count", 0) >= 2
        and witness.get("outcome") == "stable"
        and witness.get("remaining_obstruction") is False
    )


def response_evidence(row):
    """没有响应元数据时返回未知，不把别的页面成功或空值当作此请求证据。"""
    code = row.get("status_code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    if type(code) is int and 100 <= code <= 599:
        return True
    if "status_code" in row and code is None and row.get("reachable") is False:
        return False
    return None


def _failure_stage(checkpoint):
    """推进后仍保留旧错误；使用最后持久 gap，不把收口阶段当作出错阶段。"""
    if checkpoint.get("stage_status") == "retry_wait":
        return checkpoint.get("current_stage")
    gaps = decoded(checkpoint.get("gaps")) or []
    if gaps:
        last = gaps[-1]
        if last.get("error_code") == checkpoint.get("last_error_code") and last.get(
            "stage_generation"
        ) == checkpoint.get("stage_generation"):
            return last.get("stage")
    return None


def _navigation_action(checkpoint, actions, frontier, run_id):
    """只关联启动阶段的唯一稳定导航身份；后续详情、清障不能继承该身份。"""
    if _failure_stage(checkpoint) != "bootstrap_navigation":
        return None
    payload = decoded(checkpoint.get("cursor_payload")) or {}
    root = checkpoint.get("logical_page_key") == "home" and not payload.get("menu_ledger_id")
    if root:
        intent_id = f"web-bootstrap:{run_id}"
    else:
        entries = [
            row
            for row in frontier
            if row.get("menu_entry_id") == payload.get("menu_ledger_id")
            and row.get("menu_entry_id") == checkpoint.get("logical_page_key")
        ]
        if len(entries) != 1 or entries[0].get("menu_action_count") != 1:
            return None
        intent_id = entries[0].get("menu_intent_id")
    if not intent_id:
        return None
    candidates = [row for row in actions if row.get("intent_id") == intent_id]
    if len(candidates) != 1:
        return None
    action = candidates[0]
    if (
        action.get("idempotency_key") != intent_id
        or not action.get("action_execution_id")
        or action.get("action") != "navigate"
        or action.get("status") not in {"failed", "timed_out", "evidence_failed", "blocked"}
        or action.get("error_code") != checkpoint.get("last_error_code")
        or type(payload.get("finalization_epoch")) is not int
        or action.get("finalization_epoch") != payload["finalization_epoch"]
        or type(action.get("command_sequence")) is not int
    ):
        return None
    if root:
        if (
            action["command_sequence"] != 1
            or decoded(action.get("initial_surface_action")) is not True
            or action.get("intent_source_capture_id") != f"web-entry:{run_id}"
            or action.get("requested_source_capture_id") is not None
            or action.get("web_capture_stage") != "web_entry_probe"
        ):
            return None
    elif (
        action["command_sequence"] <= 1
        or action.get("web_menu_entry_id") != payload.get("menu_ledger_id")
        or action.get("web_capture_stage") != "web_page_probe"
        or decoded(action.get("initial_surface_action")) is True
    ):
        return None
    return action


def _action_failure(action, captures_by_id):
    """响应只来自此 Action 或其正式结果帧；动作前主页不参与失败归因。"""
    summary = decoded(action.get("state_summary")) or {}
    response = response_evidence(summary)
    capture = captures_by_id.get(action.get("result_capture_id"), {})
    if not (
        capture.get("action_execution_id") == action.get("action_execution_id")
        and capture.get("role") == "after_action"
        and type(action.get("command_sequence")) is int
        and capture.get("command_sequence") == action["command_sequence"]
    ):
        capture = {}
    capture_response = response_evidence(capture)
    # 同一请求若任一正式事实证明收到响应，不得宣称无响应。
    if capture_response is True or response is None:
        response = capture_response
    return {
        **asdict(classify_validation_failure(action["error_code"], response_received=response)),
        "action_execution_id": action["action_execution_id"],
        "response_received": response,
        **({"capture_id": capture["capture_id"]} if capture else {}),
    }


def failure_witnesses(checkpoints, captures, actions, *, frontier=(), run_id=None):
    captures_by_id = {row["capture_id"]: row for row in captures}
    failures = []
    consumed = set()
    for checkpoint in checkpoints:
        error = checkpoint.get("last_error_code")
        if not error:
            continue
        action = _navigation_action(checkpoint, actions, frontier, run_id)
        if action is not None and action["action_execution_id"] not in consumed:
            failures.append(
                {
                    **_action_failure(action, captures_by_id),
                    "logical_page_key": checkpoint["logical_page_key"],
                }
            )
            consumed.add(action["action_execution_id"])
            continue
        detail = decoded(checkpoint.get("last_error_detail")) or {}
        own_capture = captures_by_id.get(detail.get("capture_id"), {})
        response = response_evidence(own_capture)
        failures.append(
            {
                **asdict(classify_validation_failure(error, response_received=response)),
                "logical_page_key": checkpoint["logical_page_key"],
                "response_received": response,
                "capture_id": own_capture.get("capture_id"),
            }
        )
    for action in actions:
        if not action.get("error_code") or action["action_execution_id"] in consumed:
            continue
        failures.append(_action_failure(action, captures_by_id))
    return failures
