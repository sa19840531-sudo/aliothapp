"""外链窗口行为与多采集状态迁移的确定性求值器。"""

from __future__ import annotations

import json
import re
from math import isfinite
from typing import Any
from urllib.parse import urlsplit

from services.api.app.application.rule_handler_evaluators import (
    HandlerEvaluation,
    HandlerInputError,
)

MAX_STATE_TEXT_LENGTH = 100_000
MAX_STATE_ACTIONS = 100
MAX_CAPTURED_NEW_PAGE_URLS = 10
_CAPTURE_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def validate_external_link_open_mode_config(config: dict[str, Any]) -> str | None:
    """校验外链窗口打开方式配置。"""
    expected_mode = config.get("expected_mode", "new_window")
    if expected_mode not in {"new_window", "current_window"}:
        return "external_link_open_mode.expected_mode 必须为 new_window/current_window"
    role_error = _validate_capture_role(config, "action_role", default="primary")
    if role_error is not None:
        return role_error
    for field in ("action_selector_pattern", "target_url_pattern"):
        pattern_error = _validate_optional_pattern(config, field)
        if pattern_error is not None:
            return pattern_error
    for field, default in (
        ("require_cross_origin", True),
        ("require_current_url_unchanged", True),
    ):
        if not isinstance(config.get(field, default), bool):
            return f"external_link_open_mode.{field} 必须为布尔值"
    if expected_mode == "current_window" and config.get(
        "require_current_url_unchanged", True
    ):
        return "current_window 模式下 require_current_url_unchanged 必须为 false"
    return None


def evaluate_external_link_open_mode(
    *,
    evidence_by_role: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> HandlerEvaluation:
    """核对外链是否按配置打开新窗口或复用当前窗口。"""
    action_role = str(config.get("action_role") or "primary")
    action_trace = _role_evidence(evidence_by_role, action_role).get("action_trace")
    if not isinstance(action_trace, list) or len(action_trace) > MAX_STATE_ACTIONS:
        raise HandlerInputError(
            f"{action_role}.action_trace 必须为至多 {MAX_STATE_ACTIONS} 项的数组"
        )
    selector_pattern = config.get("action_selector_pattern")
    click_actions = [
        item
        for item in action_trace
        if isinstance(item, dict)
        and item.get("action") == "click"
        and (
            not isinstance(selector_pattern, str)
            or re.search(selector_pattern, str(item.get("selector") or ""))
        )
    ]
    if len(click_actions) != 1:
        raise HandlerInputError("外链判定必须匹配唯一 click Action Trace")
    navigation = click_actions[0].get("navigation")
    if not isinstance(navigation, dict):
        raise HandlerInputError("外链 click Action Trace 缺少 navigation 结果")
    before_url = _required_url(navigation.get("before_url"), field="before_url")
    after_url = _required_url(navigation.get("after_url"), field="after_url")
    new_page_count = navigation.get("new_page_count")
    if isinstance(new_page_count, bool) or not isinstance(new_page_count, int):
        raise HandlerInputError("navigation.new_page_count 必须为整数")
    new_page_urls = navigation.get("new_page_urls")
    if not isinstance(new_page_urls, list) or not all(
        isinstance(item, str) and item.strip() for item in new_page_urls
    ):
        raise HandlerInputError("navigation.new_page_urls 必须为 URL 字符串数组")
    urls_truncated = navigation.get("new_pages_truncated", False)
    if not isinstance(urls_truncated, bool):
        raise HandlerInputError("navigation.new_pages_truncated 必须为布尔值")
    full_detail_invalid = not urls_truncated and new_page_count != len(new_page_urls)
    truncated_detail_invalid = urls_truncated and (
        new_page_count <= len(new_page_urls)
        or len(new_page_urls) != MAX_CAPTURED_NEW_PAGE_URLS
    )
    if full_detail_invalid or truncated_detail_invalid:
        raise HandlerInputError("navigation.new_page_count 与 URL 明细不一致")
    parsed_targets = [
        _required_url(item, field="new_page_urls") for item in new_page_urls
    ]
    current_unchanged = before_url == after_url
    target_pattern = config.get("target_url_pattern")
    target_pattern_ok = not isinstance(target_pattern, str) or any(
        re.search(target_pattern, item) for item in parsed_targets
    )
    cross_origin_ok = not bool(config.get("require_cross_origin", True)) or any(
        _url_origin(item) != _url_origin(before_url) for item in parsed_targets
    )
    expected_mode = str(config.get("expected_mode") or "new_window")
    if expected_mode == "new_window":
        mode_ok = new_page_count > 0
        current_ok = current_unchanged or not bool(
            config.get("require_current_url_unchanged", True)
        )
    else:
        mode_ok = new_page_count == 0 and not current_unchanged
        current_ok = True
        cross_origin_ok = not bool(config.get("require_cross_origin", True)) or (
            _url_origin(after_url) != _url_origin(before_url)
        )
        target_pattern_ok = not isinstance(target_pattern, str) or bool(
            re.search(target_pattern, after_url)
        )
    passed = mode_ok and current_ok and cross_origin_ok and target_pattern_ok
    problems: list[str] = []
    if not mode_ok:
        problems.append("链接未按预期窗口方式打开")
    if not current_ok:
        problems.append("打开新窗口时当前页面发生了跳转")
    if not cross_origin_ok:
        problems.append("目标未形成跨域外链")
    if not target_pattern_ok:
        problems.append("目标 URL 不匹配配置")
    return HandlerEvaluation(
        passed=passed,
        pass_summary="外部链接窗口打开方式符合预期",
        fail_summary="外部链接窗口打开方式不符合预期",
        problem_desc=None if passed else "；".join(problems),
        judge_output={
            "expected_mode": expected_mode,
            "before_url": before_url,
            "after_url": after_url,
            "current_url_unchanged": current_unchanged,
            "new_page_count": new_page_count,
            "new_page_urls": parsed_targets,
            "cross_origin_ok": cross_origin_ok,
            "target_pattern_ok": target_pattern_ok,
        },
    )


def validate_state_transition_config(config: dict[str, Any]) -> str | None:
    """校验多采集状态迁移配置。"""
    for field, default in (
        ("before_role", "primary"),
        ("after_role", "after"),
        ("refreshed_role", "refreshed"),
        ("action_role", "after"),
    ):
        error = _validate_capture_role(config, field, default=default)
        if error is not None:
            return error
    before_role = str(config.get("before_role") or "primary")
    after_role = str(config.get("after_role") or "after")
    if before_role == after_role:
        return "state_transition_assertion.before_role 与 after_role 必须不同"
    for field in (
        "before_text_pattern",
        "after_text_pattern",
        "after_absent_pattern",
        "refreshed_text_pattern",
        "action_pattern",
    ):
        error = _validate_optional_pattern(config, field)
        if error is not None:
            return error
    if config.get("refreshed_text_pattern") is not None:
        refreshed_role = str(config.get("refreshed_role") or "refreshed")
        if refreshed_role in {before_role, after_role}:
            return "state_transition_assertion.refreshed_role 必须独立于前后采集"
    for field, default in (
        ("require_text_change", False),
        ("after_fullscreen_required", False),
    ):
        if not isinstance(config.get(field, default), bool):
            return f"state_transition_assertion.{field} 必须为布尔值"
    coverage = config.get("fullscreen_min_coverage", 0.85)
    if isinstance(coverage, bool) or not isinstance(coverage, (int, float)):
        return "state_transition_assertion.fullscreen_min_coverage 必须为数值"
    if not isfinite(float(coverage)) or not 0.5 <= float(coverage) <= 1:
        return "state_transition_assertion.fullscreen_min_coverage 必须在 0.5 到 1 之间"
    numeric = config.get("numeric_change")
    numeric_error = _validate_numeric_change(numeric)
    if numeric_error is not None:
        return numeric_error
    configured_assertions = any(
        config.get(field) is not None
        for field in (
            "after_text_pattern",
            "after_absent_pattern",
            "refreshed_text_pattern",
        )
    ) or any(
        (
            bool(config.get("require_text_change", False)),
            bool(config.get("after_fullscreen_required", False)),
            numeric is not None,
        )
    )
    if not configured_assertions:
        return "state_transition_assertion 至少配置一项状态断言"
    return None


def evaluate_state_transition(
    *,
    evidence_by_role: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> HandlerEvaluation:
    """核对操作前、操作后及刷新后的可信状态变化。"""
    before_role = str(config.get("before_role") or "primary")
    after_role = str(config.get("after_role") or "after")
    before = _role_evidence(evidence_by_role, before_role)
    after = _role_evidence(evidence_by_role, after_role)
    before_text = _role_text(before, before_role)
    after_text = _role_text(after, after_role)
    checks: list[dict[str, Any]] = []
    _append_text_checks(
        checks=checks,
        before_text=before_text,
        after_text=after_text,
        config=config,
    )
    refreshed_pattern = config.get("refreshed_text_pattern")
    if isinstance(refreshed_pattern, str):
        refreshed_role = str(config.get("refreshed_role") or "refreshed")
        refreshed_text = _role_text(
            _role_evidence(evidence_by_role, refreshed_role),
            refreshed_role,
        )
        _append_pattern_check(
            checks,
            name="refreshed_text_pattern",
            pattern=refreshed_pattern,
            text=refreshed_text,
            expected=True,
        )
    action_pattern = config.get("action_pattern")
    if isinstance(action_pattern, str):
        action_role = str(config.get("action_role") or "after")
        action_trace = _role_evidence(evidence_by_role, action_role).get("action_trace")
        if not isinstance(action_trace, list) or len(action_trace) > MAX_STATE_ACTIONS:
            raise HandlerInputError(
                f"{action_role}.action_trace 必须为至多 {MAX_STATE_ACTIONS} 项的数组"
            )
        checks.append(
            {
                "name": "action_pattern",
                "passed": re.search(
                    action_pattern,
                    json.dumps(action_trace, ensure_ascii=False),
                )
                is not None,
            }
        )
    numeric_output = _evaluate_numeric_change(
        checks=checks,
        before_text=before_text,
        after_text=after_text,
        before_role=before_role,
        after_role=after_role,
        config=config,
    )
    fullscreen_output = _evaluate_fullscreen_transition(
        checks=checks,
        before=before,
        after=after,
        config=config,
    )
    passed = bool(checks) and all(bool(item["passed"]) for item in checks)
    failed_names = [str(item["name"]) for item in checks if not item["passed"]]
    return HandlerEvaluation(
        passed=passed,
        pass_summary="操作后的页面状态按预期即时迁移并保持一致",
        fail_summary="操作后的页面状态未按预期迁移或发生回滚",
        problem_desc=None if passed else "未满足状态断言：" + ", ".join(failed_names),
        judge_output={
            "before_role": before_role,
            "after_role": after_role,
            "checks": checks,
            "numeric_change": numeric_output,
            "fullscreen": fullscreen_output,
        },
    )


def _validate_numeric_change(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        return "state_transition_assertion.numeric_change 必须为对象"
    pattern = value.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return "state_transition_assertion.numeric_change.pattern 必须为非空正则"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"state_transition_assertion.numeric_change.pattern 不是有效正则：{exc}"
    if compiled.groups < 1:
        return "state_transition_assertion.numeric_change.pattern 必须包含数值捕获组"
    for field, default in (("min_delta", 0.0), ("max_delta", None)):
        number = value.get(field, default)
        if number is None and field == "max_delta":
            continue
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            return f"state_transition_assertion.numeric_change.{field} 必须为数值"
        if not isfinite(float(number)):
            return f"state_transition_assertion.numeric_change.{field} 必须为有限数"
    maximum = value.get("max_delta")
    if maximum is not None and float(maximum) < float(value.get("min_delta", 0.0)):
        return "state_transition_assertion.numeric_change.max_delta 不能小于 min_delta"
    return None


def _append_text_checks(
    *,
    checks: list[dict[str, Any]],
    before_text: str,
    after_text: str,
    config: dict[str, Any],
) -> None:
    _append_pattern_check(
        checks,
        name="before_text_pattern",
        pattern=config.get("before_text_pattern"),
        text=before_text,
        expected=True,
    )
    _append_pattern_check(
        checks,
        name="after_text_pattern",
        pattern=config.get("after_text_pattern"),
        text=after_text,
        expected=True,
    )
    _append_pattern_check(
        checks,
        name="after_absent_pattern",
        pattern=config.get("after_absent_pattern"),
        text=after_text,
        expected=False,
    )
    if bool(config.get("require_text_change", False)):
        checks.append(
            {
                "name": "visible_text_changed",
                "passed": _normalize_state_text(before_text)
                != _normalize_state_text(after_text),
            }
        )


def _append_pattern_check(
    checks: list[dict[str, Any]],
    *,
    name: str,
    pattern: Any,
    text: str,
    expected: bool,
) -> None:
    if not isinstance(pattern, str):
        return
    matched = re.search(pattern, text) is not None
    checks.append({"name": name, "passed": matched is expected})


def _evaluate_numeric_change(
    *,
    checks: list[dict[str, Any]],
    before_text: str,
    after_text: str,
    before_role: str,
    after_role: str,
    config: dict[str, Any],
) -> dict[str, float] | None:
    numeric = config.get("numeric_change")
    if not isinstance(numeric, dict):
        return None
    before_value = _unique_pattern_number(
        before_text,
        pattern=str(numeric["pattern"]),
        field=f"{before_role}.visible_text",
    )
    after_value = _unique_pattern_number(
        after_text,
        pattern=str(numeric["pattern"]),
        field=f"{after_role}.visible_text",
    )
    delta = after_value - before_value
    minimum = float(numeric.get("min_delta", 0.0))
    maximum_raw = numeric.get("max_delta")
    maximum = float(maximum_raw) if maximum_raw is not None else None
    passed = delta >= minimum and (maximum is None or delta <= maximum)
    checks.append({"name": "numeric_change", "passed": passed})
    return {
        "before": before_value,
        "after": after_value,
        "delta": delta,
        "min_delta": minimum,
        **({"max_delta": maximum} if maximum is not None else {}),
    }


def _evaluate_fullscreen_transition(
    *,
    checks: list[dict[str, Any]],
    before: dict[str, Any],
    after: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any] | None:
    if not bool(config.get("after_fullscreen_required", False)):
        return None
    minimum_coverage = float(config.get("fullscreen_min_coverage", 0.85))
    before_fullscreen = _fullscreen_state(before, minimum_coverage=minimum_coverage)
    after_fullscreen = _fullscreen_state(after, minimum_coverage=minimum_coverage)
    passed = not before_fullscreen["active"] and after_fullscreen["active"]
    checks.append({"name": "fullscreen_transition", "passed": passed})
    return {
        "before": before_fullscreen,
        "after": after_fullscreen,
        "minimum_coverage": minimum_coverage,
    }


def _validate_capture_role(
    config: dict[str, Any],
    field: str,
    *,
    default: str,
) -> str | None:
    value = config.get(field, default)
    if not isinstance(value, str) or _CAPTURE_ROLE_PATTERN.fullmatch(value) is None:
        return f"{field} 必须为有效采集角色名"
    return None


def _validate_optional_pattern(config: dict[str, Any], field: str) -> str | None:
    pattern = config.get(field)
    if pattern is None:
        return None
    if not isinstance(pattern, str) or not pattern:
        return f"{field} 必须为非空正则"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"{field} 不是有效正则：{exc}"
    return None


def _role_evidence(
    evidence_by_role: dict[str, dict[str, Any]],
    role: str,
) -> dict[str, Any]:
    value = evidence_by_role.get(role)
    if not isinstance(value, dict):
        raise HandlerInputError(f"缺少采集角色证据：{role}")
    return value


def _role_text(evidence: dict[str, Any], role: str) -> str:
    value = evidence.get("visible_text")
    if not isinstance(value, str) or not value.strip():
        raise HandlerInputError(f"{role}.visible_text 必须为非空字符串")
    if len(value) > MAX_STATE_TEXT_LENGTH:
        raise HandlerInputError(
            f"{role}.visible_text 最多允许 {MAX_STATE_TEXT_LENGTH} 字符"
        )
    return value


def _normalize_state_text(value: str) -> str:
    return " ".join(value.split())


def _unique_pattern_number(text: str, *, pattern: str, field: str) -> float:
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise HandlerInputError(f"{field} 必须唯一匹配数值状态")
    match = matches[0]
    raw_value = (
        match.groupdict().get("value")
        if "value" in match.groupdict()
        else match.group(1)
    )
    return _finite_number(raw_value, field=f"{field} 数值状态")


def _fullscreen_state(
    evidence: dict[str, Any],
    *,
    minimum_coverage: float,
) -> dict[str, Any]:
    snapshot = evidence.get("ui_snapshot")
    if not isinstance(snapshot, dict):
        raise HandlerInputError("全屏状态判定缺少 ui_snapshot")
    if snapshot.get("collection_error"):
        raise HandlerInputError(f"ui_snapshot 采集失败：{snapshot['collection_error']}")
    document_state = snapshot.get("document_state")
    if not isinstance(document_state, dict) or not isinstance(
        document_state.get("fullscreen_active"), bool
    ):
        raise HandlerInputError("ui_snapshot.document_state.fullscreen_active 必须为布尔值")
    media = snapshot.get("media")
    if not isinstance(media, list) or len(media) > 100:
        raise HandlerInputError("ui_snapshot.media 必须为至多 100 项的数组")
    coverage_values: list[float] = []
    for item in media:
        if not isinstance(item, dict):
            raise HandlerInputError("ui_snapshot.media 单项必须为对象")
        coverage = _finite_number(
            item.get("viewport_coverage"),
            field="ui_snapshot.media.viewport_coverage",
        )
        if not 0 <= coverage <= 1:
            raise HandlerInputError("ui_snapshot.media.viewport_coverage 必须在 0 到 1 之间")
        coverage_values.append(coverage)
    maximum_coverage = max(coverage_values, default=0.0)
    native_fullscreen = bool(document_state["fullscreen_active"])
    return {
        "active": native_fullscreen or maximum_coverage >= minimum_coverage,
        "native_fullscreen": native_fullscreen,
        "maximum_viewport_coverage": maximum_coverage,
    }


def _required_url(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HandlerInputError(f"navigation.{field} 必须为非空 URL")
    normalized = value.strip()
    parsed = urlsplit(normalized)
    try:
        _ = parsed.port
    except ValueError as exc:
        raise HandlerInputError(f"navigation.{field} 包含非法端口") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HandlerInputError(f"navigation.{field} 必须为 HTTP(S) URL")
    return normalized


def _url_origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else None
    return scheme, (parsed.hostname or "").lower(), port


def _finite_number(value: Any, *, field: str) -> float:
    if isinstance(value, bool):
        raise HandlerInputError(f"{field} 必须为有限数值")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise HandlerInputError(f"{field} 必须为有限数值") from exc
    if not isfinite(number):
        raise HandlerInputError(f"{field} 必须为有限数值")
    return number
