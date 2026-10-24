"""规则 Handler 的纯确定性求值器与配置校验器。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite
from typing import Any

MAX_COLLECTION_ITEMS = 200
MAX_COLLECTION_TEXT_LENGTH = 500
MAX_CAPABILITIES = 200
MAX_CAPABILITY_NAME_LENGTH = 128
MAX_VIOLATION_DETAILS = 20
MAX_TEMPORAL_SAMPLES = 100
MAX_UI_ELEMENTS = 500
MAX_VISIBLE_TEXT_LENGTH = 1_000_000
_CAPTURE_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


class HandlerInputError(ValueError):
    """Handler 结构化输入不满足确定性求值契约。"""


@dataclass(frozen=True)
class HandlerEvaluation:
    """Handler 统一求值结果。"""

    passed: bool
    pass_summary: str
    fail_summary: str
    problem_desc: str | None
    judge_output: dict[str, Any]


def validate_visible_text_presence_config(config: dict[str, Any]) -> str | None:
    """校验单次可见文本命中配置。"""
    pattern = config.get("pattern")
    if not isinstance(pattern, str) or not pattern:
        return "visible_text_presence.pattern 必须为非空正则"
    try:
        re.compile(pattern)
    except re.error as exc:
        return f"visible_text_presence.pattern 不是有效正则：{exc}"
    role = config.get("capture_role", "primary")
    if not isinstance(role, str) or _CAPTURE_ROLE_PATTERN.fullmatch(role) is None:
        return "visible_text_presence.capture_role 必须为有效采集角色名"
    absence_conclusion = config.get("absence_conclusion", "evidence_insufficient")
    if absence_conclusion not in {"fail", "evidence_insufficient"}:
        return "visible_text_presence.absence_conclusion 必须为 fail/evidence_insufficient"
    if not isinstance(config.get("include_ui_snapshot_text", False), bool):
        return "visible_text_presence.include_ui_snapshot_text 必须为布尔值"
    return None


def evaluate_visible_text_presence(
    *,
    visible_text: str,
    config: dict[str, Any],
) -> HandlerEvaluation:
    """只按可信可见文本是否命中配置特征形成结果。"""
    if not isinstance(visible_text, str) or not visible_text.strip():
        raise HandlerInputError("visible_text 必须为非空字符串")
    if len(visible_text) > MAX_VISIBLE_TEXT_LENGTH:
        raise HandlerInputError(f"visible_text 最多允许 {MAX_VISIBLE_TEXT_LENGTH} 字符")
    matched = re.search(str(config["pattern"]), visible_text)
    passed = matched is not None
    return HandlerEvaluation(
        passed=passed,
        pass_summary="页面已出现规则要求的可见特征",
        fail_summary="页面未出现规则要求的可见特征",
        problem_desc=None if passed else "可信可见文本未命中配置特征",
        judge_output={
            "pattern": config["pattern"],
            "matched": passed,
            "match": matched.group(0)[:200] if matched is not None else None,
        },
    )


def validate_screenshot_change_config(config: dict[str, Any]) -> str | None:
    """校验前后截图变化配置。"""
    roles: list[str] = []
    for field, default in (("before_role", "before"), ("after_role", "primary")):
        role = config.get(field, default)
        if not isinstance(role, str) or _CAPTURE_ROLE_PATTERN.fullmatch(role) is None:
            return f"screenshot_change_assertion.{field} 必须为有效采集角色名"
        roles.append(role)
    if roles[0] == roles[1]:
        return "screenshot_change_assertion.before_role 与 after_role 必须不同"
    return None


def evaluate_screenshot_change(
    *,
    screenshot_by_role: dict[str, bytes],
    config: dict[str, Any],
) -> HandlerEvaluation:
    """按前后可信截图二进制摘要是否变化形成结果。"""
    before_role = str(config.get("before_role") or "before")
    after_role = str(config.get("after_role") or "primary")
    payloads: dict[str, bytes] = {}
    for role in (before_role, after_role):
        payload = screenshot_by_role.get(role)
        if not isinstance(payload, bytes) or not payload:
            raise HandlerInputError(f"缺少采集角色截图证据：{role}")
        payloads[role] = payload
    before_hash = sha256(payloads[before_role]).hexdigest()
    after_hash = sha256(payloads[after_role]).hexdigest()
    passed = before_hash != after_hash
    return HandlerEvaluation(
        passed=passed,
        pass_summary="操作前后截图已发生变化",
        fail_summary="操作前后截图未发生变化",
        problem_desc=None if passed else "前后可信截图内容完全一致",
        judge_output={
            "before_role": before_role,
            "after_role": after_role,
            "before_sha256": before_hash,
            "after_sha256": after_hash,
            "changed": passed,
        },
    )


def validate_collection_constraint_config(config: dict[str, Any]) -> str | None:
    """校验集合约束配置。"""
    constraint = config.get("constraint")
    if constraint not in {"case_style_consistency", "monotonic"}:
        return "collection_constraint.constraint 必须为 case_style_consistency/monotonic"
    error = _validate_optional_field_name(config, "input_field")
    if error is not None:
        return error
    min_items = config.get("min_items", 2)
    if isinstance(min_items, bool) or not isinstance(min_items, int):
        return "collection_constraint.min_items 必须为整数"
    if not 2 <= min_items <= MAX_COLLECTION_ITEMS:
        return f"collection_constraint.min_items 必须在 2 到 {MAX_COLLECTION_ITEMS} 之间"
    if constraint == "case_style_consistency":
        expected_style = config.get("expected_style")
        if expected_style is not None and expected_style not in {
            "upper",
            "lower",
            "title",
            "mixed",
        }:
            return "collection_constraint.expected_style 必须为 upper/lower/title/mixed"
        return None
    direction = config.get("direction", "descending")
    if direction not in {"ascending", "descending"}:
        return "collection_constraint.direction 必须为 ascending/descending"
    if not isinstance(config.get("strict", False), bool):
        return "collection_constraint.strict 必须为布尔值"
    return _validate_optional_field_name(config, "value_field")


def evaluate_collection_constraint(
    *,
    judge_input: dict[str, Any],
    config: dict[str, Any],
) -> HandlerEvaluation:
    """执行大小写一致或数值单调排序约束。"""
    input_field = str(config.get("input_field") or "items")
    raw_items = judge_input.get(input_field)
    if not isinstance(raw_items, list):
        raise HandlerInputError(f"{input_field} 必须为数组")
    if len(raw_items) > MAX_COLLECTION_ITEMS:
        raise HandlerInputError(f"{input_field} 最多允许 {MAX_COLLECTION_ITEMS} 项")
    min_items = int(config.get("min_items", 2))
    if len(raw_items) < min_items:
        raise HandlerInputError(f"{input_field} 至少需要 {min_items} 项")
    if config["constraint"] == "case_style_consistency":
        return _evaluate_case_style(raw_items, config=config)
    return _evaluate_monotonic(raw_items, config=config)


def validate_required_ui_capability_config(config: dict[str, Any]) -> str | None:
    """校验必需 UI 能力配置。"""
    error = _validate_optional_field_name(config, "input_field")
    if error is not None:
        return error
    required_all = config.get("required_all_of", [])
    if not _is_unique_string_list(required_all, allow_empty=True):
        return "required_ui_capability.required_all_of 必须为不重复字符串数组"
    required_groups = config.get("required_any_groups", [])
    if not isinstance(required_groups, list) or not all(
        _is_unique_string_list(group, allow_empty=False) for group in required_groups
    ):
        return "required_ui_capability.required_any_groups 必须为非空字符串数组的数组"
    if not required_all and not required_groups:
        return "required_ui_capability 至少配置 required_all_of 或 required_any_groups"
    patterns = config.get("capability_patterns", {})
    if not isinstance(patterns, dict):
        return "required_ui_capability.capability_patterns 必须为对象"
    declared = set(required_all)
    for group in required_groups:
        declared.update(group)
    for capability, pattern in patterns.items():
        if capability not in declared:
            return f"capability_patterns 包含未声明能力：{capability}"
        if not isinstance(pattern, str) or not pattern:
            return f"capability_patterns.{capability} 必须为非空正则"
        try:
            re.compile(pattern)
        except re.error as exc:
            return f"capability_patterns.{capability} 不是有效正则：{exc}"
    forbidden_pattern = config.get("forbidden_action_pattern")
    if forbidden_pattern is not None:
        if not isinstance(forbidden_pattern, str) or not forbidden_pattern:
            return "required_ui_capability.forbidden_action_pattern 必须为非空正则"
        try:
            re.compile(forbidden_pattern)
        except re.error as exc:
            return f"forbidden_action_pattern 不是有效正则：{exc}"
    return None


def evaluate_required_ui_capability(
    *,
    judge_input: dict[str, Any],
    config: dict[str, Any],
    visible_text: str,
    action_trace_text: str,
) -> HandlerEvaluation:
    """核对 UI 能力存在性，并拒绝命中禁止动作。"""
    input_field = str(config.get("input_field") or "observed_capabilities")
    raw_capabilities = judge_input.get(input_field, [])
    if not isinstance(raw_capabilities, list) or not all(
        isinstance(item, str) and item.strip() for item in raw_capabilities
    ):
        raise HandlerInputError(f"{input_field} 必须为非空字符串数组")
    if len(raw_capabilities) > MAX_CAPABILITIES:
        raise HandlerInputError(f"{input_field} 最多允许 {MAX_CAPABILITIES} 项")
    if any(len(item) > MAX_CAPABILITY_NAME_LENGTH for item in raw_capabilities):
        raise HandlerInputError(f"{input_field} 单项最多 {MAX_CAPABILITY_NAME_LENGTH} 字符")
    capabilities = {item.strip() for item in raw_capabilities}
    matched_from_text: list[str] = []
    for capability, pattern in config.get("capability_patterns", {}).items():
        if re.search(pattern, visible_text):
            capabilities.add(capability)
            matched_from_text.append(capability)

    required_all = list(config.get("required_all_of", []))
    missing_all = [item for item in required_all if item not in capabilities]
    required_groups = list(config.get("required_any_groups", []))
    missing_groups = [
        list(group) for group in required_groups if not capabilities.intersection(group)
    ]
    forbidden_pattern = config.get("forbidden_action_pattern")
    forbidden_match = (
        re.search(forbidden_pattern, action_trace_text)
        if isinstance(forbidden_pattern, str)
        else None
    )
    passed = not missing_all and not missing_groups and forbidden_match is None
    problems: list[str] = []
    if missing_all:
        problems.append("缺少必需能力：" + ", ".join(missing_all))
    if missing_groups:
        problems.append(
            "未满足备选能力组：" + "；".join("/".join(group) for group in missing_groups)
        )
    if forbidden_match is not None:
        problems.append(f"Action Trace 命中禁止动作：{forbidden_match.group(0)[:100]}")
    return HandlerEvaluation(
        passed=passed,
        pass_summary="必需 UI 能力均已提供",
        fail_summary="必需 UI 能力不完整或发生禁止动作",
        problem_desc=None if passed else "；".join(problems),
        judge_output={
            "observed_capabilities": sorted(capabilities),
            "matched_from_visible_text": sorted(matched_from_text),
            "missing_required_all": missing_all,
            "missing_required_groups": missing_groups,
            "forbidden_action_matched": forbidden_match is not None,
        },
    )


def validate_temporal_sequence_config(config: dict[str, Any]) -> str | None:
    """校验时序数值变化配置。"""
    pattern = config.get("value_pattern")
    if not isinstance(pattern, str) or not pattern:
        return "temporal_sequence_assertion.value_pattern 必须为非空正则"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"temporal_sequence_assertion.value_pattern 不是有效正则：{exc}"
    if compiled.groups < 1:
        return "temporal_sequence_assertion.value_pattern 必须包含数值捕获组"
    min_samples = config.get("min_samples", 3)
    if isinstance(min_samples, bool) or not isinstance(min_samples, int):
        return "temporal_sequence_assertion.min_samples 必须为整数"
    if not 2 <= min_samples <= MAX_TEMPORAL_SAMPLES:
        return f"temporal_sequence_assertion.min_samples 必须在 2 到 {MAX_TEMPORAL_SAMPLES} 之间"
    for field, default, minimum, maximum in (
        ("expected_rate_per_second", -1.0, -1000.0, 1000.0),
        ("tolerance", 0.35, 0.0, 100.0),
    ):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"temporal_sequence_assertion.{field} 必须为数值"
        if not isfinite(float(value)) or not minimum <= float(value) <= maximum:
            return f"temporal_sequence_assertion.{field} 超出允许范围"
    return None


def evaluate_temporal_sequence(
    *,
    temporal_trace: Any,
    config: dict[str, Any],
) -> HandlerEvaluation:
    """按单调时钟偏移核对倒计时或递增计数速率。"""
    if not isinstance(temporal_trace, dict):
        raise HandlerInputError("temporal_trace 必须为对象")
    if temporal_trace.get("clock") != "monotonic":
        raise HandlerInputError("temporal_trace.clock 必须为 monotonic")
    samples = temporal_trace.get("samples")
    if not isinstance(samples, list):
        raise HandlerInputError("temporal_trace.samples 必须为数组")
    min_samples = int(config.get("min_samples", 3))
    if not min_samples <= len(samples) <= MAX_TEMPORAL_SAMPLES:
        raise HandlerInputError(
            f"temporal_trace.samples 必须在 {min_samples} 到 {MAX_TEMPORAL_SAMPLES} 项之间"
        )
    pattern = re.compile(str(config["value_pattern"]))
    parsed: list[dict[str, Any]] = []
    end_markers: list[dict[str, Any]] = []
    first_unmatched_error: HandlerInputError | None = None
    previous_offset = -1.0
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            raise HandlerInputError(f"temporal_trace 第 {index + 1} 项必须为对象")
        offset = _finite_number(sample.get("offset_ms"), field=f"第 {index + 1} 项 offset_ms")
        if offset < 0:
            raise HandlerInputError("temporal_trace.offset_ms 不能为负数")
        if offset <= previous_offset:
            raise HandlerInputError("temporal_trace.offset_ms 必须严格递增")
        previous_offset = offset
        text = sample.get("visible_text")
        if not isinstance(text, str):
            raise HandlerInputError(f"temporal_trace 第 {index + 1} 项缺少 visible_text")
        match = pattern.search(text)
        if match is None:
            if parsed and _is_temporal_end_text(text):
                end_markers.append({"index": index, "offset_ms": offset})
                continue
            error = HandlerInputError(f"temporal_trace 第 {index + 1} 项未匹配计数值")
            if parsed:
                raise error
            if first_unmatched_error is None:
                first_unmatched_error = error
            continue
        raw_value = (
            match.groupdict().get("value") if "value" in match.groupdict() else match.group(1)
        )
        value = _finite_number(raw_value, field=f"第 {index + 1} 项计数值")
        parsed.append({"index": index, "offset_ms": offset, "value": value})

    if parsed and first_unmatched_error is not None:
        raise first_unmatched_error
    if not parsed and first_unmatched_error is not None:
        event_samples = _parse_temporal_countdown_events(
            temporal_trace,
            pattern=pattern,
            min_samples=min_samples,
        )
        if not event_samples:
            raise first_unmatched_error
        parsed = event_samples

    expected_rate = float(config.get("expected_rate_per_second", -1.0))
    tolerance = float(config.get("tolerance", 0.35))
    if len(parsed) < min_samples:
        raise HandlerInputError(f"temporal_trace 可解析计数样本少于 {min_samples} 项")
    if end_markers and expected_rate < 0 and float(parsed[-1]["value"]) > 0:
        parsed.append(
            {
                **end_markers[0],
                "value": 0.0,
                "inferred_from_end": True,
            }
        )
    change_samples = [parsed[0]]
    for sample in parsed[1:]:
        if float(sample["value"]) != float(change_samples[-1]["value"]):
            change_samples.append(sample)
    intervals: list[dict[str, Any]] = []
    for left, right in zip(change_samples, change_samples[1:], strict=False):
        elapsed_seconds = (float(right["offset_ms"]) - float(left["offset_ms"])) / 1000
        observed_delta = float(right["value"]) - float(left["value"])
        expected_delta = expected_rate * elapsed_seconds
        error = abs(observed_delta - expected_delta)
        interval = {
            "from_index": left["index"],
            "to_index": right["index"],
            "elapsed_seconds": round(elapsed_seconds, 6),
            "observed_delta": observed_delta,
            "expected_delta": round(expected_delta, 6),
            "absolute_error": round(error, 6),
            "within_tolerance": error <= tolerance,
        }
        intervals.append(interval)
    cumulative_intervals: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    origin = change_samples[0]
    for sample in change_samples[1:]:
        elapsed_seconds = (float(sample["offset_ms"]) - float(origin["offset_ms"])) / 1000
        observed_delta = float(sample["value"]) - float(origin["value"])
        expected_delta = expected_rate * elapsed_seconds
        error = abs(observed_delta - expected_delta)
        cumulative = {
            "from_index": origin["index"],
            "to_index": sample["index"],
            "elapsed_seconds": round(elapsed_seconds, 6),
            "observed_delta": observed_delta,
            "expected_delta": round(expected_delta, 6),
            "absolute_error": round(error, 6),
            "within_tolerance": error <= tolerance,
        }
        cumulative_intervals.append(cumulative)
        if error > tolerance:
            violations.append(cumulative)
    if len(change_samples) < 2:
        violations.append(
            {
                "reason": "countdown_value_did_not_change",
                "sample_count": len(parsed),
                "within_tolerance": False,
            }
        )
    passed = not violations
    return HandlerEvaluation(
        passed=passed,
        pass_summary="时序计数与真实经过时间一致",
        fail_summary="时序计数与真实经过时间不一致",
        problem_desc=None if passed else "计数存在卡顿、跳秒或偷时",
        judge_output={
            "samples": parsed,
            "change_samples": change_samples,
            "expected_rate_per_second": expected_rate,
            "tolerance": tolerance,
            "intervals": intervals,
            "cumulative_intervals": cumulative_intervals,
            "violations": violations,
        },
    )


def _parse_temporal_countdown_events(
    temporal_trace: dict[str, Any],
    *,
    pattern: re.Pattern[str],
    min_samples: int,
) -> list[dict[str, Any]]:
    events = temporal_trace.get("countdown_events")
    if events is None:
        return []
    if not isinstance(events, list):
        raise HandlerInputError("temporal_trace.countdown_events 必须为数组")
    if len(events) > MAX_TEMPORAL_SAMPLES:
        raise HandlerInputError(
            f"temporal_trace.countdown_events 最多允许 {MAX_TEMPORAL_SAMPLES} 项"
        )
    parsed: list[dict[str, Any]] = []
    previous_offset = -1.0
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise HandlerInputError(f"countdown_events 第 {index + 1} 项必须为对象")
        offset = _finite_number(
            event.get("offset_ms"),
            field=f"countdown_events 第 {index + 1} 项 offset_ms",
        )
        if offset < 0:
            raise HandlerInputError("countdown_events.offset_ms 不能为负数")
        if offset <= previous_offset:
            raise HandlerInputError("countdown_events.offset_ms 必须严格递增")
        previous_offset = offset
        value: float | None = None
        if event.get("value") is not None:
            value = _finite_number(
                event.get("value"),
                field=f"countdown_events 第 {index + 1} 项计数值",
            )
        else:
            text = event.get("text")
            if isinstance(text, str):
                match = pattern.search(text)
                if match is not None:
                    raw_value = (
                        match.groupdict().get("value")
                        if "value" in match.groupdict()
                        else match.group(1)
                    )
                    value = _finite_number(
                        raw_value,
                        field=f"countdown_events 第 {index + 1} 项计数值",
                    )
        if value is None:
            continue
        parsed.append(
            {
                "index": index,
                "offset_ms": offset,
                "value": value,
                "source": "countdown_events",
                "event_kind": str(event.get("kind") or "")[:50],
            }
        )
    return parsed if len(parsed) >= min_samples else []


def _is_temporal_end_text(text: str) -> bool:
    """识别倒计时结束后出现的关闭/跳过控制，不把结束关键帧当坏证据。"""
    return (
        re.search(
            r"(?:^|\s)(?:关闭|跳过|close|skip)(?:\s|$)",
            text,
            flags=re.IGNORECASE,
        )
        is not None
    )


def validate_geometry_hit_area_config(config: dict[str, Any]) -> str | None:
    """校验点击命中区域配置。"""
    error = _validate_optional_field_name(config, "element_index_field")
    if error is not None:
        return error
    for field, default in (("min_width", 44.0), ("min_height", 44.0)):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"geometry_hit_area_assertion.{field} 必须为数值"
        if not isfinite(float(value)) or not 1 <= float(value) <= 500:
            return f"geometry_hit_area_assertion.{field} 必须在 1 到 500 之间"
    tolerance = config.get("containment_tolerance", 0.5)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        return "geometry_hit_area_assertion.containment_tolerance 必须为数值"
    if not isfinite(float(tolerance)) or not 0 <= float(tolerance) <= 20:
        return "geometry_hit_area_assertion.containment_tolerance 必须在 0 到 20 之间"
    if not isinstance(config.get("require_enabled", True), bool):
        return "geometry_hit_area_assertion.require_enabled 必须为布尔值"
    return None


def evaluate_geometry_hit_area(
    *,
    judge_input: dict[str, Any],
    ui_snapshot: Any,
    config: dict[str, Any],
) -> HandlerEvaluation:
    """核对可信 UI Snapshot 中目标元素的命中框覆盖关系。"""
    if not isinstance(ui_snapshot, dict):
        raise HandlerInputError("ui_snapshot 必须为对象")
    if ui_snapshot.get("collection_error"):
        raise HandlerInputError(f"ui_snapshot 采集失败：{ui_snapshot['collection_error']}")
    elements = ui_snapshot.get("elements")
    if not isinstance(elements, list) or len(elements) > MAX_UI_ELEMENTS:
        raise HandlerInputError(f"ui_snapshot.elements 必须为至多 {MAX_UI_ELEMENTS} 项的数组")
    index_field = str(config.get("element_index_field") or "element_index")
    target_index = judge_input.get(index_field)
    if isinstance(target_index, bool) or not isinstance(target_index, int) or target_index < 0:
        raise HandlerInputError(f"{index_field} 必须为非负整数")
    matches: list[dict[str, Any]] = []
    for item in elements:
        if not isinstance(item, dict):
            continue
        element_index = item.get("element_index")
        if isinstance(element_index, bool) or not isinstance(element_index, int):
            raise HandlerInputError("ui_snapshot.element_index 必须为整数")
        if element_index == target_index:
            matches.append(item)
    if len(matches) != 1:
        raise HandlerInputError(f"ui_snapshot 中未找到唯一 element_index={target_index}")
    target = matches[0]
    hit = _bounds(target.get("hit_bounds"), field="hit_bounds")
    visual = _bounds(target.get("visual_bounds"), field="visual_bounds")
    tolerance = float(config.get("containment_tolerance", 0.5))
    contains_visual = (
        hit["x"] <= visual["x"] + tolerance
        and hit["y"] <= visual["y"] + tolerance
        and hit["x"] + hit["width"] + tolerance >= visual["x"] + visual["width"]
        and hit["y"] + hit["height"] + tolerance >= visual["y"] + visual["height"]
    )
    min_width = float(config.get("min_width", 44.0))
    min_height = float(config.get("min_height", 44.0))
    size_ok = hit["width"] >= min_width and hit["height"] >= min_height
    disabled = target.get("disabled")
    if not isinstance(disabled, bool):
        raise HandlerInputError("ui_snapshot.disabled 必须为布尔值")
    enabled = not disabled
    enabled_ok = enabled or not bool(config.get("require_enabled", True))
    passed = contains_visual and size_ok and enabled_ok
    problems: list[str] = []
    if not contains_visual:
        problems.append("可点击命中框未完整覆盖可视区域")
    if not size_ok:
        problems.append(f"可点击区域小于 {min_width:g}×{min_height:g}")
    if not enabled_ok:
        problems.append("目标元素处于禁用状态")
    return HandlerEvaluation(
        passed=passed,
        pass_summary="目标可点击区域覆盖完整且尺寸达标",
        fail_summary="目标可点击区域不完整或尺寸不足",
        problem_desc=None if passed else "；".join(problems),
        judge_output={
            "element_index": target_index,
            "text": str(target.get("text") or "")[:200],
            "hit_bounds": hit,
            "visual_bounds": visual,
            "contains_visual": contains_visual,
            "minimum_size": {"width": min_width, "height": min_height},
            "size_ok": size_ok,
            "enabled": enabled,
        },
    )


def _evaluate_case_style(
    raw_items: list[Any],
    *,
    config: dict[str, Any],
) -> HandlerEvaluation:
    if not all(isinstance(item, str) and item.strip() for item in raw_items):
        raise HandlerInputError("大小写一致性集合必须全部为非空字符串")
    if any(len(item) > MAX_COLLECTION_TEXT_LENGTH for item in raw_items):
        raise HandlerInputError(f"大小写一致性集合单项最多 {MAX_COLLECTION_TEXT_LENGTH} 字符")
    classified = [{"value": item, "style": _classify_case_style(item)} for item in raw_items]
    meaningful = [item for item in classified if item["style"] != "no_letters"]
    min_items = int(config.get("min_items", 2))
    if len(meaningful) < min_items:
        raise HandlerInputError(f"含字母的标签至少需要 {min_items} 项")
    expected_style = config.get("expected_style")
    styles = {str(item["style"]) for item in meaningful}
    passed = styles == {expected_style} if expected_style is not None else len(styles) == 1
    return HandlerEvaluation(
        passed=passed,
        pass_summary="分类标签字母大小写风格一致",
        fail_summary="分类标签字母大小写风格不一致",
        problem_desc=None if passed else "同类标签使用了不同的字母大小写风格",
        judge_output={
            "constraint": "case_style_consistency",
            "styles": sorted(styles),
            "items": classified,
            "expected_style": expected_style,
        },
    )


def _evaluate_monotonic(
    raw_items: list[Any],
    *,
    config: dict[str, Any],
) -> HandlerEvaluation:
    value_field = config.get("value_field")
    values: list[float] = []
    for index, item in enumerate(raw_items):
        value = item.get(value_field) if isinstance(item, dict) and value_field else item
        if isinstance(value, bool):
            raise HandlerInputError(f"第 {index + 1} 项不是有效有限数值")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise HandlerInputError(f"第 {index + 1} 项不是有效有限数值") from exc
        if not isfinite(number):
            raise HandlerInputError(f"第 {index + 1} 项不是有效有限数值")
        values.append(number)
    direction = str(config.get("direction", "descending"))
    strict = bool(config.get("strict", False))
    violations: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(zip(values, values[1:], strict=False)):
        if direction == "ascending":
            ordered = left < right if strict else left <= right
        else:
            ordered = left > right if strict else left >= right
        if not ordered and len(violations) < MAX_VIOLATION_DETAILS:
            violations.append(
                {"left_index": index, "left": left, "right_index": index + 1, "right": right}
            )
    passed = not violations
    direction_text = "升序" if direction == "ascending" else "降序"
    return HandlerEvaluation(
        passed=passed,
        pass_summary=f"榜单数值按{direction_text}排列",
        fail_summary=f"榜单数值未按{direction_text}排列",
        problem_desc=None if passed else "榜单存在排序倒置",
        judge_output={
            "constraint": "monotonic",
            "direction": direction,
            "strict": strict,
            "values": values,
            "violations": violations,
        },
    )


def _classify_case_style(value: str) -> str:
    words = re.findall(r"[A-Za-z]+", value)
    if not words:
        return "no_letters"
    letters = "".join(words)
    if letters.isupper():
        return "upper"
    if letters.islower():
        return "lower"
    if all(word[0].isupper() and word[1:].islower() for word in words):
        return "title"
    return "mixed"


def _is_unique_string_list(value: Any, *, allow_empty: bool) -> bool:
    return (
        isinstance(value, list)
        and (allow_empty or bool(value))
        and all(isinstance(item, str) and item.strip() for item in value)
        and all(len(item) <= MAX_CAPABILITY_NAME_LENGTH for item in value)
        and len(set(value)) == len(value)
        and len(value) <= MAX_CAPABILITIES
    )


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


def _bounds(value: Any, *, field: str) -> dict[str, float]:
    if not isinstance(value, dict):
        raise HandlerInputError(f"{field} 必须为矩形对象")
    bounds = {
        key: _finite_number(value.get(key), field=f"{field}.{key}")
        for key in ("x", "y", "width", "height")
    }
    if bounds["width"] <= 0 or bounds["height"] <= 0:
        raise HandlerInputError(f"{field} 的宽高必须大于 0")
    return bounds


def _validate_optional_field_name(config: dict[str, Any], field: str) -> str | None:
    value = config.get(field)
    if value is not None and (not isinstance(value, str) or not value.strip()):
        return f"{field} 必须为非空字符串"
    return None
