"""价格一致性、防刷与内容生命周期规则的确定性求值器。"""

from __future__ import annotations

import json
import re
from math import isfinite
from typing import Any

from services.api.app.application.rule_handler_evaluators import (
    HandlerEvaluation,
    HandlerInputError,
)

MAX_POLICY_TEXT_LENGTH = 100_000
MAX_PRICE_MATCHES = 200
MAX_RATE_ATTEMPTS = 20
MAX_ACTIONS_PER_ATTEMPT = 100
MAX_NETWORK_EVENTS_PER_ATTEMPT = 2_000
MAX_CONSUMER_ROLES = 50
DEFAULT_RATE_LIMIT_TEXT_PATTERN = r"操作(?:过于)?频繁|请求过多|稍后再试|验证码|captcha"
_CAPTURE_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# 价格前面是这类标签而不是商品名时，这个价格归不到具体商品，不参与跨位置比对
_PRICE_LABEL_PATTERN = re.compile(
    r"(?:原价|现价|优惠价|折扣价|折后价|特价|售价|价格|会员价|活动价|到手价|券后价?|划线价|市场价"
    r"|合计|总计|小计|实付|应付|金额|总价)\s*[:：]?"
)


class HandlerPreconditionMissing(HandlerInputError):
    """材料格式合法，但尚未提供判定所需的业务前置事实。"""


def validate_price_relation_config(config: dict[str, Any]) -> str | None:
    """校验跨页面同商品价格关系配置。"""
    pattern = config.get("price_pattern")
    if not isinstance(pattern, str) or not pattern:
        return "price_relation.price_pattern 必须为非空正则"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"price_relation.price_pattern 不是有效正则：{exc}"
    if not {"product", "price"} <= set(compiled.groupindex):
        return "price_relation.price_pattern 必须包含 product 与 price 命名捕获组"
    role_error = _validate_role_pattern(
        config,
        field="location_role_pattern",
        default=r"^(primary|location_\d+)$",
        require_group=False,
    )
    if role_error is not None:
        return role_error
    if re.fullmatch(
        str(config.get("location_role_pattern") or r"^(primary|location_\d+)$"),
        "primary",
    ) is None:
        return "price_relation.location_role_pattern 必须匹配 primary"
    for field, default, minimum, maximum in (
        ("min_locations_per_product", 2, 2, 20),
        ("min_shared_products", 1, 1, 50),
    ):
        value = config.get(field, default)
        if isinstance(value, bool) or not isinstance(value, int):
            return f"price_relation.{field} 必须为整数"
        if not minimum <= value <= maximum:
            return f"price_relation.{field} 必须在 {minimum} 到 {maximum} 之间"
    tolerance = config.get("tolerance", 0.01)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        return "price_relation.tolerance 必须为数值"
    if not isfinite(float(tolerance)) or not 0 <= float(tolerance) <= 1000:
        return "price_relation.tolerance 必须在 0 到 1000 之间"
    return None


def evaluate_price_relation(
    *,
    evidence_by_role: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> HandlerEvaluation:
    """从可信多位置文本中抽取同一商品价格并核对一致性。"""
    role_pattern = re.compile(
        str(config.get("location_role_pattern") or r"^(primary|location_\d+)$")
    )
    selected_roles = sorted(
        role for role in evidence_by_role if role_pattern.fullmatch(role)
    )
    if len(selected_roles) < 2:
        raise HandlerPreconditionMissing(
            f"price_relation 需要 2 到 {MAX_CONSUMER_ROLES} 个价格位置采集角色"
        )
    if len(selected_roles) > MAX_CONSUMER_ROLES:
        raise HandlerInputError(
            f"price_relation 需要 2 到 {MAX_CONSUMER_ROLES} 个价格位置采集角色"
        )
    price_pattern = re.compile(str(config["price_pattern"]))
    grouped: dict[str, list[dict[str, Any]]] = {}
    match_count = 0
    for role in selected_roles:
        text = _role_text(evidence_by_role, role)
        for match in price_pattern.finditer(text):
            match_count += 1
            if match_count > MAX_PRICE_MATCHES:
                raise HandlerInputError(
                    f"price_relation 最多允许 {MAX_PRICE_MATCHES} 个价格观测"
                )
            product = " ".join(match.group("product").split())
            if not product or _PRICE_LABEL_PATTERN.fullmatch(product):
                # 「原价：¥599」这类行没有商品名；各档位的原价本来就不同，混在一起比会误报不一致
                continue
            if len(product) > 200:
                raise HandlerInputError("price_relation.product 必须为至多 200 字符")
            price = _finite_number(
                match.group("price").replace(",", ""),
                field="price_relation.price",
            )
            if price < 0 or price > 1_000_000_000:
                raise HandlerInputError("price_relation.price 超出允许范围")
            grouped.setdefault(product.casefold(), []).append(
                {"product": product, "role": role, "price": price}
            )
    minimum_locations = int(config.get("min_locations_per_product", 2))
    comparable = {
        key: observations
        for key, observations in grouped.items()
        if len({item["role"] for item in observations}) >= minimum_locations
    }
    minimum_products = int(config.get("min_shared_products", 1))
    if len(comparable) < minimum_products:
        raise HandlerPreconditionMissing(
            f"price_relation 至少需要 {minimum_products} 个跨位置同商品价格组"
        )
    tolerance = float(config.get("tolerance", 0.01))
    products: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for observations in comparable.values():
        prices = [float(item["price"]) for item in observations]
        spread = max(prices) - min(prices)
        result = {
            "product": observations[0]["product"],
            "observations": observations,
            "spread": spread,
            "within_tolerance": spread <= tolerance,
        }
        products.append(result)
        if spread > tolerance:
            violations.append(result)
    passed = not violations
    return HandlerEvaluation(
        passed=passed,
        pass_summary="同一商品在各展示位置的价格一致",
        fail_summary="同一商品在不同展示位置出现价格不一致",
        problem_desc=None if passed else "存在跨页面商品价格差异",
        judge_output={
            "location_roles": selected_roles,
            "tolerance": tolerance,
            "products": products,
            "violations": violations,
        },
    )


def validate_rate_limit_config(config: dict[str, Any]) -> str | None:
    """校验防刷探测配置。"""
    role_error = _validate_role_pattern(
        config,
        field="attempt_role_pattern",
        default=r"^attempt_(?P<index>\d+)$",
        require_group=True,
    )
    if role_error is not None:
        return role_error
    for field in ("probe_content_pattern", "action_pattern"):
        if config.get(field) is None:
            return f"rate_limit_assertion.{field} 必须配置"
        error = _validate_optional_pattern(config, field)
        if error is not None:
            return error
    for field in ("rate_limit_text_pattern",):
        error = _validate_optional_pattern(config, field)
        if error is not None:
            return error
    minimum_attempts = config.get("min_attempts", 4)
    if isinstance(minimum_attempts, bool) or not isinstance(minimum_attempts, int):
        return "rate_limit_assertion.min_attempts 必须为整数"
    if not 4 <= minimum_attempts <= MAX_RATE_ATTEMPTS:
        return f"rate_limit_assertion.min_attempts 必须在 4 到 {MAX_RATE_ATTEMPTS} 之间"
    primary_index = config.get("primary_attempt_index", minimum_attempts)
    if isinstance(primary_index, bool) or not isinstance(primary_index, int):
        return "rate_limit_assertion.primary_attempt_index 必须为整数"
    if not 1 <= primary_index <= MAX_RATE_ATTEMPTS:
        return f"rate_limit_assertion.primary_attempt_index 必须在 1 到 {MAX_RATE_ATTEMPTS} 之间"
    if primary_index < minimum_attempts:
        return "rate_limit_assertion.primary_attempt_index 不能小于 min_attempts"
    max_visible = config.get("max_visible_repeats", 3)
    if isinstance(max_visible, bool) or not isinstance(max_visible, int):
        return "rate_limit_assertion.max_visible_repeats 必须为整数"
    if not 0 <= max_visible <= 20:
        return "rate_limit_assertion.max_visible_repeats 必须在 0 到 20 之间"
    statuses = config.get("rate_limit_statuses", [429])
    if not isinstance(statuses, list) or not statuses or not all(
        isinstance(item, int)
        and not isinstance(item, bool)
        and 100 <= item <= 599
        for item in statuses
    ):
        return "rate_limit_assertion.rate_limit_statuses 必须为有效 HTTP 状态码数组"
    if len(set(statuses)) != len(statuses):
        return "rate_limit_assertion.rate_limit_statuses 不得重复"
    if not isinstance(config.get("require_explicit_challenge", False), bool):
        return "rate_limit_assertion.require_explicit_challenge 必须为布尔值"
    return None


def evaluate_rate_limit(
    *,
    evidence_by_role: dict[str, dict[str, Any]],
    config: dict[str, Any],
    network_optional: bool = False,
) -> HandlerEvaluation:
    """核对连续用户输入是否触发限频、验证码或重复内容抑制。

    network_optional 只给采不到网络记录的 App 用：某次尝试没有 har 时不看状态码，
    只看页面上的限频 / 验证码提示；给了 har 仍按原合同校验。
    """
    role_pattern = re.compile(
        str(config.get("attempt_role_pattern") or r"^attempt_(?P<index>\d+)$")
    )
    attempts: list[tuple[int, str]] = []
    for role in evidence_by_role:
        match = role_pattern.fullmatch(role)
        if match is None:
            continue
        raw_index = match.groupdict().get("index") or match.group(1)
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise HandlerInputError("rate_limit_assertion 尝试序号必须为整数") from exc
        if not 1 <= index <= MAX_RATE_ATTEMPTS:
            raise HandlerInputError(
                f"rate_limit_assertion 尝试序号必须在 1 到 {MAX_RATE_ATTEMPTS} 之间"
            )
        attempts.append((index, role))
    minimum_attempts = int(config.get("min_attempts", 4))
    if "primary" not in evidence_by_role:
        raise HandlerInputError("rate_limit_assertion 缺少 primary 最终尝试采集")
    attempts.append(
        (int(config.get("primary_attempt_index", minimum_attempts)), "primary")
    )
    attempts.sort()
    indexes = [item[0] for item in attempts]
    if len(indexes) != len(set(indexes)):
        raise HandlerInputError("rate_limit_assertion 尝试序号不得重复")
    if indexes != list(range(1, len(indexes) + 1)) or attempts[-1][1] != "primary":
        raise HandlerInputError("rate_limit_assertion 尝试必须连续且 primary 为最终采集")
    if not minimum_attempts <= len(attempts) <= MAX_RATE_ATTEMPTS:
        raise HandlerInputError(
            f"rate_limit_assertion 需要 {minimum_attempts} 到 {MAX_RATE_ATTEMPTS} 次尝试"
        )
    rate_text_pattern = str(
        config.get("rate_limit_text_pattern") or DEFAULT_RATE_LIMIT_TEXT_PATTERN
    )
    action_pattern = config.get("action_pattern")
    status_set = set(config.get("rate_limit_statuses", [429]))
    attempt_outputs: list[dict[str, Any]] = []
    explicit_signal = False
    final_text = ""
    for index, role in attempts:
        evidence = evidence_by_role[role]
        text = _role_text(evidence_by_role, role)
        final_text = text
        action_trace = evidence.get("action_trace")
        if not isinstance(action_trace, list) or len(action_trace) > MAX_ACTIONS_PER_ATTEMPT:
            raise HandlerInputError(
                f"{role}.action_trace 必须为至多 {MAX_ACTIONS_PER_ATTEMPT} 项的数组"
            )
        if isinstance(action_pattern, str) and re.search(
            action_pattern,
            json.dumps(action_trace, ensure_ascii=False),
        ) is None:
            raise HandlerInputError(f"{role}.action_trace 未匹配防刷探测动作")
        network_recorded = not (network_optional and evidence.get("har") is None)
        statuses = _response_statuses(evidence.get("har"), role=role) if network_recorded else []
        text_limited = re.search(rate_text_pattern, text, flags=re.IGNORECASE) is not None
        status_limited = any(item in status_set for item in statuses)
        explicit_signal = explicit_signal or text_limited or status_limited
        attempt_outputs.append(
            {
                "index": index,
                "role": role,
                "response_statuses": statuses,
                "text_limit_signal": text_limited,
                "status_limit_signal": status_limited,
                **({} if network_recorded else {"network_recorded": False}),
            }
        )
    probe_pattern = config.get("probe_content_pattern")
    visible_repeats: int | None = None
    repeat_cap_ok = True
    suppression_observed = False
    if isinstance(probe_pattern, str):
        visible_repeats = len(re.findall(probe_pattern, final_text))
        max_visible = int(config.get("max_visible_repeats", 3))
        repeat_cap_ok = visible_repeats <= max_visible
        suppression_observed = visible_repeats < len(attempts)
    protection_observed = explicit_signal or (
        suppression_observed and not bool(config.get("require_explicit_challenge", False))
    )
    passed = protection_observed and repeat_cap_ok
    problems: list[str] = []
    if not protection_observed:
        problems.append("连续输入未触发限频、验证码或重复抑制")
    if not repeat_cap_ok:
        problems.append("探测内容在界面中重复展示超过允许次数")
    return HandlerEvaluation(
        passed=passed,
        pass_summary="连续用户输入已受到防刷保护且未形成刷屏",
        fail_summary="连续用户输入缺少防刷保护或已形成刷屏",
        problem_desc=None if passed else "；".join(problems),
        judge_output={
            "attempts": attempt_outputs,
            "explicit_limit_signal": explicit_signal,
            "suppression_observed": suppression_observed,
            "visible_probe_repeats": visible_repeats,
            "repeat_cap_ok": repeat_cap_ok,
        },
    )


def validate_content_lifecycle_config(config: dict[str, Any]) -> str | None:
    """校验下架或失效内容的入口展示策略。"""
    source_role = config.get("source_role", "source")
    if not isinstance(source_role, str) or _CAPTURE_ROLE_PATTERN.fullmatch(source_role) is None:
        return "content_lifecycle_state.source_role 必须为有效采集角色名"
    if source_role == "primary":
        return "content_lifecycle_state.source_role 不能使用 primary"
    role_error = _validate_role_pattern(
        config,
        field="consumer_role_pattern",
        default=r"^(primary|entry_\d+)$",
        require_group=False,
    )
    if role_error is not None:
        return role_error
    if re.fullmatch(
        str(config.get("consumer_role_pattern") or r"^(primary|entry_\d+)$"),
        "primary",
    ) is None:
        return "content_lifecycle_state.consumer_role_pattern 必须匹配 primary"
    for field in (
        "source_inactive_pattern",
        "consumer_entity_pattern",
        "consumer_marked_pattern",
    ):
        error = _validate_required_pattern(config, field)
        if error is not None:
            return error
    mode = config.get("expected_consumer_state", "hidden_or_marked")
    if mode not in {"hidden", "marked", "hidden_or_marked"}:
        return (
            "content_lifecycle_state.expected_consumer_state "
            "必须为 hidden/marked/hidden_or_marked"
        )
    minimum = config.get("min_consumer_captures", 1)
    if isinstance(minimum, bool) or not isinstance(minimum, int):
        return "content_lifecycle_state.min_consumer_captures 必须为整数"
    if not 1 <= minimum <= MAX_CONSUMER_ROLES:
        return (
            "content_lifecycle_state.min_consumer_captures "
            f"必须在 1 到 {MAX_CONSUMER_ROLES} 之间"
        )
    return None


def evaluate_content_lifecycle(
    *,
    evidence_by_role: dict[str, dict[str, Any]],
    config: dict[str, Any],
) -> HandlerEvaluation:
    """核对来源已失效内容在全部消费入口是否隐藏或前置标记。"""
    source_role = str(config.get("source_role") or "source")
    source_text = _role_text(evidence_by_role, source_role)
    if re.search(str(config["source_inactive_pattern"]), source_text) is None:
        raise HandlerPreconditionMissing("来源采集未证明目标内容处于失效状态")
    consumer_pattern = re.compile(
        str(config.get("consumer_role_pattern") or r"^(primary|entry_\d+)$")
    )
    consumer_roles = sorted(
        role
        for role in evidence_by_role
        if role != source_role and consumer_pattern.fullmatch(role)
    )
    minimum = int(config.get("min_consumer_captures", 1))
    if len(consumer_roles) < minimum:
        raise HandlerPreconditionMissing(
            f"content_lifecycle_state 缺少消费入口采集，需要至少 {minimum} 个"
        )
    if not minimum <= len(consumer_roles) <= MAX_CONSUMER_ROLES:
        raise HandlerInputError(
            f"content_lifecycle_state 需要 {minimum} 到 {MAX_CONSUMER_ROLES} 个消费入口采集"
        )
    entity_pattern = str(config["consumer_entity_pattern"])
    marked_pattern = str(config["consumer_marked_pattern"])
    expected = str(config.get("expected_consumer_state") or "hidden_or_marked")
    consumers: list[dict[str, Any]] = []
    for role in consumer_roles:
        text = _role_text(evidence_by_role, role)
        entity_visible = re.search(entity_pattern, text) is not None
        marker_visible = re.search(marked_pattern, text) is not None
        if expected == "hidden":
            role_passed = not entity_visible
        elif expected == "marked":
            role_passed = entity_visible and marker_visible
        else:
            role_passed = not entity_visible or marker_visible
        consumers.append(
            {
                "role": role,
                "entity_visible": entity_visible,
                "inactive_marker_visible": marker_visible,
                "passed": role_passed,
            }
        )
    passed = all(bool(item["passed"]) for item in consumers)
    failed_roles = [str(item["role"]) for item in consumers if not item["passed"]]
    return HandlerEvaluation(
        passed=passed,
        pass_summary="失效内容已在全部消费入口隐藏或同步前置状态",
        fail_summary="失效内容仍在消费入口无标识展示",
        problem_desc=None if passed else "未同步失效状态的入口：" + ", ".join(failed_roles),
        judge_output={
            "source_role": source_role,
            "expected_consumer_state": expected,
            "consumers": consumers,
            "failed_roles": failed_roles,
        },
    )


def _validate_role_pattern(
    config: dict[str, Any],
    *,
    field: str,
    default: str,
    require_group: bool,
) -> str | None:
    pattern = config.get(field, default)
    if not isinstance(pattern, str) or not pattern:
        return f"{field} 必须为非空正则"
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        return f"{field} 不是有效正则：{exc}"
    if require_group and compiled.groups < 1:
        return f"{field} 必须包含尝试序号捕获组"
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


def _validate_required_pattern(config: dict[str, Any], field: str) -> str | None:
    if config.get(field) is None:
        return f"content_lifecycle_state.{field} 必须配置"
    return _validate_optional_pattern(config, field)


def _role_text(evidence_by_role: dict[str, dict[str, Any]], role: str) -> str:
    evidence = evidence_by_role.get(role)
    if not isinstance(evidence, dict):
        raise HandlerInputError(f"缺少采集角色证据：{role}")
    text = evidence.get("visible_text")
    if not isinstance(text, str) or not text.strip():
        raise HandlerInputError(f"{role}.visible_text 必须为非空字符串")
    if len(text) > MAX_POLICY_TEXT_LENGTH:
        raise HandlerInputError(
            f"{role}.visible_text 最多允许 {MAX_POLICY_TEXT_LENGTH} 字符"
        )
    return text


def _response_statuses(value: Any, *, role: str) -> list[int]:
    if not isinstance(value, dict) or value.get("format") != "network_events":
        raise HandlerInputError(f"{role}.har 必须为 network_events 对象")
    entries = value.get("entries")
    if not isinstance(entries, list) or len(entries) > MAX_NETWORK_EVENTS_PER_ATTEMPT:
        raise HandlerInputError(
            f"{role}.har.entries 必须为至多 {MAX_NETWORK_EVENTS_PER_ATTEMPT} 项的数组"
        )
    statuses: list[int] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise HandlerInputError(f"{role}.har.entries 单项必须为对象")
        if entry.get("event") != "response":
            continue
        status = entry.get("status")
        if isinstance(status, bool) or not isinstance(status, int):
            raise HandlerInputError(f"{role}.har response status 必须为整数")
        if not 100 <= status <= 599:
            raise HandlerInputError(f"{role}.har response status 超出 HTTP 范围")
        statuses.append(status)
    return statuses


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
