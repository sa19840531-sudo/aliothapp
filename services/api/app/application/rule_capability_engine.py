"""确定性规则 Handler，只依据 Observation Packet 中的可信事实判定。"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from services.api.app.application.rule_capability_facts import (
    candidate_qr_urls,
    element_area,
    element_ref,
    elements,
    extract_price_pairs,
    facts,
    first_timer_value,
    integer,
    is_media_element,
    last_action,
    latest_ui_snapshot,
    looks_like_ad_control,
    looks_like_ad_element,
    looks_like_promotional_text,
    mapping,
    modal_elements,
    modal_elements_from_snapshot,
    number,
    snapshot_elements,
    snapshot_sequence,
    string_list,
    visible_text,
)

CAPABILITY_HANDLER_NAMES = frozenset(
    {
        "technical_error_text",
        "discount_price_compare",
        "visible_ad_presence_handler",
        "page_url_status_handler",
        "startup_ad_home_transition_handler",
        "qr_url_validation",
        "external_navigation_handler",
        "data_refresh_handler",
        "countdown_accuracy_handler",
        "clickable_area_handler",
        "popup_close_behavior_handler",
        "overlay_dismissal_handler",
        "popup_sequence_handler",
        "ad_first_frame_latency_handler",
        "ad_failure_media_fallback_handler",
        "spam_frequency_handler",
        "comment_state_sync_handler",
        "label_case_consistency_handler",
        "offline_content_visibility_handler",
        "playback_resume_handler",
        "upload_rule_visibility_handler",
        "media_fullscreen_handler",
        "logout_entry_presence_handler",
    }
)


@dataclass(frozen=True)
class CapabilityResult:
    """确定性能力处理器的统一输出。"""

    tool_name: str
    status: str
    conclusion: str
    confidence: float
    summary: str
    problem_desc: str | None
    facts: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


async def evaluate_rule_engine_handler(
    *,
    handler: str,
    packet_payload: dict[str, Any],
    engine_config: dict[str, Any],
    qr_validator: Callable[..., Awaitable[dict[str, Any]]] | None = None,
) -> CapabilityResult:
    """执行数据库声明的 Handler；证据不足时绝不猜测通过。"""
    if handler not in CAPABILITY_HANDLER_NAMES:
        return _insufficient(handler, f"Handler 未注册：{handler}")
    if handler == "technical_error_text":
        return _technical_error(packet_payload, engine_config)
    if handler == "discount_price_compare":
        return _discount_price(packet_payload, engine_config)
    if handler == "visible_ad_presence_handler":
        return _visible_ad_presence(packet_payload)
    if handler == "page_url_status_handler":
        return await _page_url_status(
            packet_payload,
            engine_config,
            url_validator=qr_validator,
        )
    if handler == "startup_ad_home_transition_handler":
        return _startup_ad_home_transition(packet_payload)
    if handler == "qr_url_validation":
        return await _qr_url(
            packet_payload,
            engine_config,
            qr_validator=qr_validator,
        )
    if handler == "external_navigation_handler":
        return _external_navigation(packet_payload)
    if handler == "data_refresh_handler":
        return _data_refresh(packet_payload, engine_config)
    if handler in {
        "comment_state_sync_handler",
        "offline_content_visibility_handler",
        "playback_resume_handler",
    }:
        return _journey_state_assertion(handler, packet_payload)
    if handler == "countdown_accuracy_handler":
        return _countdown_accuracy(packet_payload)
    if handler == "clickable_area_handler":
        return _clickable_area(packet_payload, engine_config)
    if handler in {"popup_close_behavior_handler", "overlay_dismissal_handler"}:
        return _dismissal_behavior(handler, packet_payload)
    if handler == "popup_sequence_handler":
        return _popup_sequence(packet_payload, engine_config)
    if handler == "ad_first_frame_latency_handler":
        return _first_frame_latency(packet_payload, engine_config)
    if handler == "ad_failure_media_fallback_handler":
        return _ad_failure_fallback(packet_payload, engine_config)
    if handler == "spam_frequency_handler":
        return _spam_frequency(packet_payload, engine_config)
    if handler == "label_case_consistency_handler":
        return _label_case_consistency(packet_payload)
    if handler == "upload_rule_visibility_handler":
        return _upload_rule_visibility(packet_payload, engine_config)
    if handler == "media_fullscreen_handler":
        return _media_fullscreen(packet_payload)
    if handler == "logout_entry_presence_handler":
        return _logout_presence(packet_payload, engine_config)
    return _insufficient(handler, "Handler 尚未绑定确定性实现")


def _technical_error(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    text = visible_text(packet_payload)
    pattern = str(config.get("pattern") or "").strip()
    if not text:
        return _insufficient("technical_error_text", "可信 visible_text 为空")
    if not pattern:
        return _insufficient("technical_error_text", "数据库未配置错误特征正则")
    try:
        matched = re.search(pattern, text)
    except re.error as exc:
        return _insufficient("technical_error_text", f"规则正则无效：{exc}")
    if matched is None:
        return _pass(
            "technical_error_text",
            "未发现技术错误直出",
            facts={"pattern": pattern, "matched": False},
        )
    return _fail(
        "technical_error_text",
        "发现技术错误直出",
        problem_desc="页面可见文本命中技术错误特征",
        facts={
            "pattern": pattern,
            "matched": True,
            "match": matched.group(0)[:300],
            "span": list(matched.span()),
        },
    )


def _discount_price(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    pairs = extract_price_pairs(packet_payload, config)
    if not pairs:
        return _insufficient(
            "discount_price_compare",
            "未从可信页面元素提取到同一商品的优惠价与原价",
        )
    invalid = [item for item in pairs if item["discount_price"] >= item["original_price"]]
    if invalid:
        return _fail(
            "discount_price_compare",
            "优惠价未低于原价",
            problem_desc="页面展示的优惠价应低于原价",
            facts={"pairs": pairs, "invalid_pairs": invalid},
        )
    return _pass(
        "discount_price_compare",
        "优惠价低于原价",
        facts={"pairs": pairs},
    )


def _visible_ad_presence(packet_payload: dict[str, Any]) -> CapabilityResult:
    """截图对应的结构化快照出现广告特征即可通过。"""
    ad_elements = [
        element_ref(item)
        for item in snapshot_elements(packet_payload)
        if looks_like_ad_element(item)
    ]
    page = mapping(packet_payload.get("page"))
    page_state = str(page.get("page_state") or "").casefold()
    text_hit = looks_like_promotional_text(visible_text(packet_payload))
    ad_state = page_state in {
        "ad",
        "advertisement",
        "countdown",
        "startup_ad",
        "pre_roll_ad",
    }
    media_modal = _closeable_media_modal(latest_ui_snapshot(packet_payload))
    if ad_elements or text_hit or ad_state or media_modal is not None:
        return _pass(
            "visible_ad_presence_handler",
            "页面截图与结构化快照已出现广告",
            facts={
                "ad_elements": ad_elements[:50],
                "promotional_text_matched": text_hit,
                "page_state": page_state or None,
                "closeable_media_modal": media_modal,
            },
        )
    return _insufficient(
        "visible_ad_presence_handler",
        "截图对应的结构化快照未识别到广告特征",
        facts={
            "ad_elements": [],
            "promotional_text_matched": False,
            "page_state": page_state or None,
            "closeable_media_modal": None,
        },
    )


def _closeable_media_modal(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """识别详情页中以大图为主体且提供关闭控件的广告弹窗。"""
    snapshot_items = snapshot.get("elements")
    if not isinstance(snapshot_items, list):
        return None
    items = [dict(item) for item in snapshot_items if isinstance(item, dict)]
    for modal in modal_elements_from_snapshot(snapshot):
        modal_area = element_area(modal)
        if modal_area <= 0:
            continue
        media_items = [
            item
            for item in items
            if is_media_element(item)
            and element_area(item) >= modal_area * 0.25
            and _rect_is_within(item, modal)
        ]
        controls = [
            item
            for item in items
            if looks_like_ad_control(item) and _rect_is_within(item, modal)
        ]
        if media_items and controls:
            return {
                "modal": element_ref(modal),
                "media": element_ref(max(media_items, key=element_area)),
                "control": element_ref(controls[0]),
            }
    return None


def _rect_is_within(item: dict[str, Any], container: dict[str, Any]) -> bool:
    item_rect = mapping(item.get("rect"))
    container_rect = mapping(container.get("rect"))
    values = [
        number(item_rect.get(field))
        for field in ("x", "y", "width", "height")
    ] + [
        number(container_rect.get(field))
        for field in ("x", "y", "width", "height")
    ]
    if any(value is None for value in values):
        return False
    item_x, item_y, item_width, item_height, box_x, box_y, box_width, box_height = values
    assert all(value is not None for value in values)
    tolerance = 2.0
    return bool(
        item_x >= box_x - tolerance
        and item_y >= box_y - tolerance
        and item_x + item_width <= box_x + box_width + tolerance
        and item_y + item_height <= box_y + box_height + tolerance
    )


NATIVE_ACTIVITY_SYNC_FACTS_MISSING = "native_activity_sync_facts_missing"
_EXPIRED_ACTIVITY_STATUSES = frozenset({"expired", "ended", "inactive", "已结束", "已失效", "失效"})
_ACTIVE_ACTIVITY_STATUSES = frozenset({"active", "valid", "进行中"})


def _native_app_location(page_url: str) -> bool:
    """app://package/activity 是原生页面身份，不是可请求的 HTTP 地址。"""
    return urlparse(page_url).scheme.casefold() == "app"


def _optional_bool(record: dict[str, Any], *keys: str) -> bool | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, bool):
            return value
    return None


def _activity_expired(record: dict[str, Any]) -> bool | None:
    expired = _optional_bool(record, "expired")
    if expired is not None:
        return expired
    status = str(record.get("status") or "").strip().casefold()
    if status in _EXPIRED_ACTIVITY_STATUSES:
        return True
    if status in _ACTIVE_ACTIVITY_STATUSES:
        return False
    return None


def _native_activity_display_sync(
    packet_payload: dict[str, Any],
    page_url: str,
) -> CapabilityResult:
    """原生页不探测 HTTP。有活动对象、有效期和展示事实才判断失效活动是否仍在展示。"""
    record = mapping(facts(packet_payload).get("activity"))
    identity = ""
    for key in ("activity_id", "id", "name", "title"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            identity = value.strip()
            break
    expired = _activity_expired(record)
    displayed = _optional_bool(record, "displayed", "visible", "still_shown")
    if not identity or expired is None or displayed is None:
        return _insufficient(
            "page_url_status_handler",
            "原生页面使用 app:// 地址，不要求 HTTP 状态。"
            "缺少活动对象、有效期或展示事实，无法判断失效活动是否仍在展示",
            facts={
                "url": page_url,
                "http_status_required": False,
                "gap_codes": [NATIVE_ACTIVITY_SYNC_FACTS_MISSING],
            },
        )
    shared = {
        "url": page_url,
        "http_status_required": False,
        "activity_id": identity,
        "expired": expired,
        "displayed": displayed,
    }
    if expired and displayed:
        return _fail(
            "page_url_status_handler",
            "失效活动仍在展示",
            problem_desc="活动已失效，但原生页面仍在展示该活动",
            facts=shared,
        )
    if expired:
        return _pass(
            "page_url_status_handler",
            "失效活动已不再展示",
            facts=shared,
        )
    return _pass(
        "page_url_status_handler",
        "活动未失效，不按失效展示判断",
        facts=shared,
    )


async def _page_url_status(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
    *,
    url_validator: Callable[..., Awaitable[dict[str, Any]]] | None,
) -> CapabilityResult:
    """按当前页面 URL 的最终 HTTP 状态码形成结果。"""
    page_url = str(mapping(packet_payload.get("page")).get("url") or "").strip()
    if not page_url:
        return _insufficient("page_url_status_handler", "Observation Packet 缺少页面 URL")
    if _native_app_location(page_url):
        return _native_activity_display_sync(packet_payload, page_url)
    validator = url_validator or _default_qr_validator
    timeout_seconds = float(config.get("timeout_seconds", 8.0))
    max_redirects = int(config.get("max_redirects", 5))
    validation = await validator(
        url=page_url,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )
    checks = validation.get("url_checks")
    check = checks[0] if isinstance(checks, list) and checks else {}
    status_code = check.get("status_code") if isinstance(check, dict) else None
    if isinstance(status_code, bool) or not isinstance(status_code, int):
        return _insufficient(
            "page_url_status_handler",
            "页面 URL 请求未返回可确认的 HTTP 状态码",
            facts={"url": page_url, "validation": validation},
        )
    allowed_statuses = config.get("allowed_status_codes", [200])
    passed = status_code in allowed_statuses
    if passed:
        return _pass(
            "page_url_status_handler",
            f"页面 URL 返回 HTTP {status_code}",
            facts={"url": page_url, "status_code": status_code},
        )
    return _fail(
        "page_url_status_handler",
        f"页面 URL 返回 HTTP {status_code}",
        problem_desc="页面 URL 未返回规则允许的成功状态码",
        facts={
            "url": page_url,
            "status_code": status_code,
            "allowed_status_codes": list(allowed_statuses),
        },
    )


def _submitted_frame_is_later_popup(packet_payload: dict[str, Any]) -> bool:
    """后续首页弹层不能冒充原启动广告上的跳过按钮。"""

    page = mapping(packet_payload.get("page"))
    if page.get("page_type") == "startup_ad":
        return False
    state = str(page.get("page_state") or "").casefold()
    if any(token in state for token in ("popup", "overlay", "modal", "弹窗", "弹层")):
        return True
    presentation = mapping(facts(packet_payload).get("presentation"))
    if presentation.get("occluding_layer_present") is True:
        return True
    return presentation.get("topmost_layer_kind") in {"overlay", "popup", "modal"}


def _startup_ad_skip_pair(packet_payload: dict[str, Any]) -> tuple[bool, bool]:
    """原启动广告上的跳过按钮，以及点击后不同于该帧的落点。"""

    evidence = mapping(facts(packet_payload).get("startup_ad_skip"))
    button_id = str(evidence.get("button_capture_id") or "").strip()
    landing_id = str(evidence.get("landing_capture_id") or "").strip()
    button_ready = bool(
        button_id
        and evidence.get("button_page_type") == "startup_ad"
        and evidence.get("later_popup") is not True
    )
    landing_ready = bool(
        landing_id
        and landing_id != button_id
        and evidence.get("landing_page_type") not in {None, "", "startup_ad"}
    )
    if button_ready and landing_ready:
        return True, True
    actions = facts(packet_payload).get("action_log")
    if not isinstance(actions, list):
        return button_ready, landing_ready
    for item in reversed(actions):
        if not isinstance(item, dict):
            continue
        semantic = str(item.get("semantic_target") or item.get("selector") or "")
        if item.get("action") not in {"click", "tap"} or "跳过" not in semantic:
            continue
        if item.get("source_page_type") != "startup_ad" or item.get("later_popup") is True:
            continue
        source_id = str(item.get("source_capture_id") or "").strip()
        result_id = str(item.get("result_capture_id") or "").strip()
        result_page = str(item.get("result_page_type") or "")
        if source_id:
            button_ready = True
        if result_id and result_id != source_id and result_page not in {"", "startup_ad"}:
            landing_ready = True
    return button_ready, landing_ready


def _startup_ad_home_transition(
    packet_payload: dict[str, Any],
) -> CapabilityResult:
    """启动广告之后进入首页即可通过。"""
    journey = facts(packet_payload).get("journey")
    journey = journey if isinstance(journey, list) else []
    page_types = [
        str(item.get("page_type"))
        for item in journey
        if isinstance(item, dict) and item.get("page_type")
    ]
    current_page_type = mapping(packet_payload.get("page")).get("page_type")
    if isinstance(current_page_type, str) and (
        not page_types or page_types[-1] != current_page_type
    ):
        page_types.append(current_page_type)
    before = mapping(mapping(facts(packet_payload).get("capture_roles")).get("before"))
    if (
        before.get("page_type") == "startup_ad"
        and before.get("quality_status") == "pass"
        and before.get("page_capture_id")
        and before.get("page_capture_id") != mapping(packet_payload.get("page")).get(
            "page_capture_id"
        )
        and current_page_type != "startup_ad"
        and "startup_ad" not in page_types
    ):
        # 可信精确 before/primary 配对优先于被压缩的旅程摘要。
        page_types.insert(max(len(page_types) - 1, 0), "startup_ad")
    startup_indexes = [
        index for index, page_type in enumerate(page_types) if page_type == "startup_ad"
    ]
    if not startup_indexes:
        if current_page_type == "home":
            return _skipped(
                "startup_ad_home_transition_handler",
                "当前旅程直接进入首页，未出现启动广告页",
            )
        return _insufficient(
            "startup_ad_home_transition_handler",
            "旅程中未确认启动广告页",
            facts={"page_types": page_types},
        )
    subsequent_page_types = page_types[startup_indexes[-1] + 1 :]
    if _submitted_frame_is_later_popup(packet_payload):
        button_ready, landing_ready = _startup_ad_skip_pair(packet_payload)
        if not (button_ready and landing_ready):
            gap_codes: list[str] = []
            if not button_ready:
                gap_codes.append("startup_ad_skip_button_missing")
            if not landing_ready:
                gap_codes.append("startup_ad_skip_landing_missing")
            return _insufficient(
                "startup_ad_home_transition_handler",
                "提交的截图是后续弹层，缺少原启动广告的跳过按钮及点击后落点",
                facts={"page_types": page_types, "gap_codes": gap_codes},
            )
    if "home" in subsequent_page_types:
        return _pass(
            "startup_ad_home_transition_handler",
            "启动广告后已进入首页",
            facts={"page_types": page_types},
        )
    if subsequent_page_types:
        return _fail(
            "startup_ad_home_transition_handler",
            "启动广告后未进入首页",
            problem_desc="启动广告后的已知页面不是首页",
            facts={"page_types": page_types},
        )
    return _insufficient(
        "startup_ad_home_transition_handler",
        "启动广告仍处于当前旅程末端，等待后续首页采集后再判定",
        facts={"page_types": page_types, "missing_capture_roles": ["after"]},
    )


async def _qr_url(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
    *,
    qr_validator: Callable[..., Awaitable[dict[str, Any]]] | None,
) -> CapabilityResult:
    allowed_schemes = set(string_list(config.get("allowed_schemes")) or ["http", "https"])
    urls = [
        url
        for url in candidate_qr_urls(packet_payload)
        if urlparse(url).scheme.casefold() in allowed_schemes
    ]
    if not urls:
        return _insufficient(
            "qr_url_validation",
            "未从可信页面元素提取到二维码目标网址",
        )
    validator = qr_validator or _default_qr_validator
    timeout_seconds = float(config.get("timeout_seconds", 8.0))
    max_redirects = int(config.get("max_redirects", 5))
    validations = [
        await validator(
            url=url,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
        )
        for url in urls[:10]
    ]
    invalid = [item for item in validations if not bool(item.get("is_valid"))]
    if invalid:
        issues = [issue for item in invalid for issue in string_list(item.get("issues"))]
        return _fail(
            "qr_url_validation",
            "二维码或网址有效性校验未通过",
            problem_desc="；".join(issues) or "二维码目标不可用",
            facts={"validations": validations},
        )
    return _pass(
        "qr_url_validation",
        "二维码或网址可正常访问",
        facts={"validations": validations},
    )


def _external_navigation(packet_payload: dict[str, Any]) -> CapabilityResult:
    action = last_action(packet_payload)
    if not action or action.get("action") != "click":
        links = [
            element_ref(item) for item in elements(packet_payload) if item.get("external") is True
        ]
        return _insufficient(
            "external_navigation_handler",
            "尚未对外链执行安全单击",
            facts={"external_links": links[:30]},
        )
    target_before = mapping(action.get("target_before"))
    if target_before.get("external") is not True:
        return _insufficient(
            "external_navigation_handler",
            "无法证明最后一次点击的目标属于外部链接",
            facts={"action": action},
        )
    before_url = str(action.get("before_url") or "")
    after_url = str(action.get("after_url") or "")
    new_pages = string_list(action.get("new_page_urls"))
    if new_pages and before_url and after_url == before_url:
        return _pass(
            "external_navigation_handler",
            "外链在新窗口打开且当前页保持不变",
            facts={"action": action},
        )
    if before_url and after_url and before_url != after_url:
        return _fail(
            "external_navigation_handler",
            "外链覆盖了当前产品页",
            problem_desc="外部链接应在新窗口或新标签页打开",
            facts={"action": action},
        )
    return _insufficient(
        "external_navigation_handler",
        "点击后未观测到可确认的导航结果",
        facts={"action": action},
    )


def _data_refresh(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    action = last_action(packet_payload)
    if not action or action.get("action") != "click":
        return _insufficient("data_refresh_handler", "尚未执行数据变更动作")
    state_diff = mapping(action.get("state_diff"))
    if not state_diff:
        return _insufficient("data_refresh_handler", "缺少操作前后状态差异证据")
    changed_fields = string_list(state_diff.get("changed_fields"))
    expected_fields = string_list(config.get("expected_changed_fields"))
    if expected_fields and not set(expected_fields).issubset(changed_fields):
        return _fail(
            "data_refresh_handler",
            "目标状态未按规则更新",
            problem_desc="操作后的页面状态变化不完整",
            facts={"state_diff": state_diff, "expected_fields": expected_fields},
        )
    if integer(state_diff.get("change_count")) <= 0 or not changed_fields:
        return _fail(
            "data_refresh_handler",
            "操作后页面状态未变化",
            problem_desc="未观测到规则要求的数据或 UI 更新",
            facts={"state_diff": state_diff},
        )
    return _pass(
        "data_refresh_handler",
        "操作后页面状态按预期更新",
        facts={"state_diff": state_diff},
    )


def _journey_state_assertion(
    handler: str,
    packet_payload: dict[str, Any],
) -> CapabilityResult:
    """跨页规则只接收浏览器运行时生成的专项验证事实。"""
    verification = mapping(facts(packet_payload).get("journey_verification"))
    result = mapping(verification.get(handler))
    if not result:
        return _insufficient(
            handler,
            "缺少跨页返回、刷新或重进后的专项验证事实",
        )
    passed = result.get("passed")
    if not isinstance(passed, bool):
        return _insufficient(handler, "专项验证事实缺少布尔 passed 结果")
    if passed:
        return _pass(handler, "跨页状态验证通过", facts={"verification": result})
    return _fail(
        handler,
        "跨页状态验证未通过",
        problem_desc=str(result.get("reason") or "跨页状态与规则预期不一致"),
        facts={"verification": result},
    )


def _countdown_accuracy(packet_payload: dict[str, Any]) -> CapabilityResult:
    sequence = snapshot_sequence(packet_payload)
    if len(sequence) < 2:
        return _insufficient(
            "countdown_accuracy_handler",
            "倒计时判定至少需要两帧结构化快照",
        )
    timer_values = [first_timer_value(snapshot) for snapshot in sequence]
    if any(value is None for value in timer_values):
        return _insufficient(
            "countdown_accuracy_handler",
            "多帧快照中未稳定提取到倒计时数值",
            facts={"timer_values": timer_values},
        )
    numeric_values = [float(value) for value in timer_values if value is not None]
    if not all(
        left > right for left, right in zip(numeric_values, numeric_values[1:], strict=False)
    ):
        return _fail(
            "countdown_accuracy_handler",
            "倒计时未单调递减",
            problem_desc="倒计时数值应随时间递减",
            facts={"timer_values": numeric_values},
        )
    return _pass(
        "countdown_accuracy_handler",
        "倒计时按序递减",
        facts={"timer_values": numeric_values},
    )


def _clickable_area(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    keywords = string_list(config.get("target_keywords")) or ["跳过", "关闭", "×", "x"]
    minimum_width = number(config.get("min_width")) or 32
    minimum_height = number(config.get("min_height")) or 32
    surface_kind = mapping(packet_payload.get("page")).get("surface_kind")
    is_app = surface_kind in {"android", "ios"}
    snapshot = latest_ui_snapshot(packet_payload)
    native_source = (
        is_app
        and snapshot.get("surface_kind") == surface_kind
        and snapshot.get("source") == f"{surface_kind}_ui_hierarchy"
    )
    controls = [
        item
        for item in elements(packet_payload)
        if (
            # 原生 View 不一定有 Web 标签；只承接可信原生快照中的真实可点击事实。
            native_source
            and item.get("clickable") is True
            and item.get("enabled") is True
            and item.get("visible") is not False
            if is_app
            else (
                str(item.get("tag") or "") in {"a", "button", "input"}
                or str(item.get("role") or "") in {"button", "link"}
            )
        )
        and any(
            keyword.casefold()
            in " ".join(
                (
                    str(item.get("text") or ""),
                    str(item.get("aria_label") or ""),
                )
            ).casefold()
            for keyword in keywords
        )
    ]
    if not controls:
        return _insufficient("clickable_area_handler", "未定位到目标交互控件")
    invalid: list[dict[str, Any]] = []
    for item in controls:
        rect = mapping(item.get("rect"))
        hit_test = mapping(item.get("hit_test"))
        too_small = (number(rect.get("width")) or 0) < minimum_width or (
            number(rect.get("height")) or 0
        ) < minimum_height
        obstructed = hit_test.get("center_hits_target") is False
        if too_small or obstructed:
            invalid.append(
                {
                    "element": element_ref(item),
                    "too_small": too_small,
                    "center_obstructed": obstructed,
                    "hit_test": hit_test,
                }
            )
    if invalid:
        return _fail(
            "clickable_area_handler",
            "控件点击区域过小或中心点被遮挡",
            problem_desc="目标控件未达到数据库配置的可点击要求",
            facts={"invalid_elements": invalid},
        )
    return _pass(
        "clickable_area_handler",
        "控件点击区域满足要求",
        facts={"elements": [element_ref(item) for item in controls]},
    )


def _dismissal_behavior(
    handler: str,
    packet_payload: dict[str, Any],
) -> CapabilityResult:
    action = last_action(packet_payload)
    if not action or action.get("action") != "click":
        if not modal_elements(packet_payload):
            return _skipped(handler, "当前页面未出现可见弹窗或蒙层")
        return _insufficient(handler, "尚未执行弹窗或遮罩关闭动作")
    target_before = mapping(action.get("target_before"))
    identity = " ".join(
        (
            str(action.get("selector") or ""),
            str(target_before.get("role") or ""),
            str(target_before.get("text") or ""),
            str(target_before.get("aria_label") or ""),
            " ".join(string_list(target_before.get("class_tokens"))),
        )
    )
    if not re.search(
        r"close|dismiss|overlay|dialog|modal|popup|关闭|跳过|遮罩|×",
        identity,
        re.IGNORECASE,
    ):
        return _insufficient(handler, "最后一次点击不是可确认的弹窗关闭动作")
    remaining = modal_elements(packet_payload)
    if remaining:
        return _fail(
            handler,
            "关闭动作后弹窗或遮罩仍然可见",
            problem_desc="广告弹窗关闭行为未生效",
            facts={"action": action, "remaining": remaining[:20]},
        )
    return _pass(
        handler,
        "关闭动作后弹窗或遮罩已消失",
        facts={"action": action},
    )


def _page_is_topmost_layer(packet_payload: dict[str, Any]) -> bool:
    """展示层评估（页面结构命中测试）已确认最上层就是页面本身、没有弹窗盖着。

    弹窗识别只看类名（mask / overlay / popup …），短视频页的播放器遮罩、组件库里
    没打开的弹层都会被当成弹窗；有结构命中测试的结论时以它为准。
    """
    presentation = mapping(facts(packet_payload).get("presentation"))
    return (
        presentation.get("topmost_layer_kind") == "page"
        and presentation.get("hit_test_status") == "available"
    )


def _popup_sequence(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    sequence = snapshot_sequence(packet_payload)
    if len(sequence) < 2:
        if mapping(packet_payload.get("identity")).get("target_platform") == "h5":
            from services.api.app.application.h5_observation_sequence import h5_modal_count

            count = h5_modal_count(latest_ui_snapshot(packet_payload))
            if count == 0:
                return _skipped("popup_sequence_handler", "当前完整观察未出现弹窗")
            return _insufficient(
                "popup_sequence_handler", "H5 弹窗顺序判定缺少完整前后观察",
            )
        if not modal_elements(packet_payload) or _page_is_topmost_layer(packet_payload):
            return _skipped("popup_sequence_handler", "当前页面未出现连续弹窗")
        return _insufficient(
            "popup_sequence_handler",
            "首页弹窗只采到单帧，缺少关闭后的下一弹窗，以及清空后的连续观察",
            facts={
                "gap_codes": [
                    "popup_sequence_next_frame_missing",
                    "popup_sequence_cleared_observation_missing",
                ],
                "frame_count": len(sequence),
            },
        )
    if mapping(packet_payload.get("identity")).get("target_platform") == "h5":
        from services.api.app.application.h5_observation_sequence import h5_modal_count

        modal_counts = [h5_modal_count(snapshot) for snapshot in sequence]
        if any(count is None for count in modal_counts):
            return _insufficient(
                "popup_sequence_handler", "H5 弹窗顺序判定缺少完整现场弹窗普查",
            )
    else:
        modal_counts = [len(modal_elements_from_snapshot(snapshot)) for snapshot in sequence]
    maximum = integer(config.get("max_simultaneous_popups")) or 1
    if any(count > maximum for count in modal_counts):
        return _fail(
            "popup_sequence_handler",
            "同一时间展示了过多弹窗",
            problem_desc="弹窗展示顺序或互斥关系不符合规则",
            facts={"modal_counts": modal_counts, "maximum": maximum},
        )
    if not any(count > 0 for count in modal_counts):
        return _skipped("popup_sequence_handler", "当前页面未出现连续弹窗")
    clear_at = next((index for index, count in enumerate(modal_counts) if count == 0), None)
    facts_payload = {"modal_counts": modal_counts, "maximum": maximum, "frame_count": len(sequence)}
    if clear_at is None:
        return _insufficient(
            "popup_sequence_handler",
            "已看到关闭后的下一弹窗，但没有清空后的连续观察",
            facts={**facts_payload, "gap_codes": ["popup_sequence_cleared_observation_missing"]},
        )
    if clear_at == 0:
        return _insufficient(
            "popup_sequence_handler",
            "首页弹窗只采到单帧，缺少关闭后的下一弹窗，以及清空后的连续观察",
            facts={
                **facts_payload,
                "gap_codes": [
                    "popup_sequence_next_frame_missing",
                    "popup_sequence_cleared_observation_missing",
                ],
            },
        )
    watched = modal_counts[clear_at + 1 :]
    if not watched:
        return _insufficient(
            "popup_sequence_handler",
            "弹窗已清空，但缺少清空后的连续观察",
            facts={**facts_payload, "gap_codes": ["popup_sequence_cleared_observation_missing"]},
        )
    if any(count > 0 for count in watched):
        return _fail(
            "popup_sequence_handler",
            "首页弹窗清空后又出现迟到弹窗",
            problem_desc="弹窗链清空后的连续观察里再次出现弹窗，属于稍后载入",
            facts=facts_payload,
        )
    return _pass(
        "popup_sequence_handler",
        "关闭后能看到下一弹窗，清空后的连续观察没有迟到弹窗",
        facts=facts_payload,
    )


def _first_frame_latency(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    page = mapping(packet_payload.get("page"))
    if page.get("page_type") not in {None, "startup_ad"}:
        return _insufficient(
            "ad_first_frame_latency_handler",
            "当前页面不是可确认的开屏广告页面",
        )
    performance = mapping(latest_ui_snapshot(packet_payload).get("performance"))
    paints = performance.get("paints")
    paints = paints if isinstance(paints, list) else []
    first_paint = next(
        (
            number(item.get("start_time_ms"))
            for item in paints
            if isinstance(item, dict)
            and item.get("name") in {"first-paint", "first-contentful-paint"}
        ),
        None,
    )
    threshold = number(config.get("max_latency_ms")) or 3_000
    if first_paint is None:
        return _insufficient(
            "ad_first_frame_latency_handler",
            "浏览器未提供首帧性能时序",
        )
    if first_paint > threshold:
        return _fail(
            "ad_first_frame_latency_handler",
            "广告首帧展示超时",
            problem_desc="首帧耗时超过数据库阈值",
            facts={"first_paint_ms": first_paint, "threshold_ms": threshold},
        )
    return _pass(
        "ad_first_frame_latency_handler",
        "广告首帧在时限内展示",
        facts={"first_paint_ms": first_paint, "threshold_ms": threshold},
    )


def _ad_failure_fallback(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    action = last_action(packet_payload)
    if not action or action.get("action") != "network_fault":
        return _insufficient(
            "ad_failure_media_fallback_handler",
            "尚未在隔离环境执行网络故障动作",
        )
    media = [item for item in elements(packet_payload) if item.get("media") or item.get("image")]
    keywords = string_list(config.get("fallback_keywords")) or [
        "重试",
        "暂时无法",
        "加载失败",
    ]
    fallback_hits = [keyword for keyword in keywords if keyword in visible_text(packet_payload)]
    if media or fallback_hits:
        return _pass(
            "ad_failure_media_fallback_handler",
            "广告失败后已展示媒体或友好兜底",
            facts={
                "media_count": len(media),
                "fallback_text_hits": fallback_hits,
            },
        )
    return _fail(
        "ad_failure_media_fallback_handler",
        "广告失败后未展示可用兜底",
        problem_desc="网络异常后页面既无媒体内容也无友好提示",
        facts={"action": action},
    )


def _spam_frequency(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    sequence = mapping(facts(packet_payload).get("content_sequence"))
    entries = sequence.get("entries")
    if not isinstance(entries, list) or len(entries) < 2:
        return _insufficient(
            "spam_frequency_handler",
            "缺少带时间戳和主体标识的连续内容序列",
        )
    maximum = integer(config.get("max_same_content_count")) or 3
    counts: dict[tuple[str, str], int] = {}
    for item in entries:
        value = mapping(item)
        actor = str(value.get("actor_id") or "").strip()
        content = str(value.get("content") or "").strip()
        if not actor or not content or not value.get("captured_at"):
            return _insufficient(
                "spam_frequency_handler",
                "连续内容序列缺少 actor_id、content 或 captured_at",
            )
        key = (actor, content)
        counts[key] = counts.get(key, 0) + 1
    violating = {
        f"{actor}:{content[:100]}": count
        for (actor, content), count in counts.items()
        if count > maximum
    }
    if violating:
        return _fail(
            "spam_frequency_handler",
            "发现同一主体超频重复内容",
            problem_desc="相同内容出现次数超过数据库阈值",
            facts={"violating": violating, "maximum": maximum},
        )
    return _pass(
        "spam_frequency_handler",
        "未发现超频重复内容",
        facts={"sequence_count": len(entries), "maximum": maximum},
    )


def _label_case_consistency(packet_payload: dict[str, Any]) -> CapabilityResult:
    labels = [
        str(item.get("text") or "").strip()
        for item in elements(packet_payload)
        if str(item.get("text") or "").strip()
        and (
            str(item.get("tag") or "") in {"a", "button"}
            or str(item.get("role") or "") in {"button", "link", "tab"}
        )
    ]
    if not labels:
        return _insufficient(
            "label_case_consistency_handler",
            "未提取到可比较的交互标签",
        )
    grouped: dict[str, set[str]] = {}
    for label in labels:
        grouped.setdefault(label.casefold(), set()).add(label)
    inconsistent = {
        normalized: sorted(values) for normalized, values in grouped.items() if len(values) > 1
    }
    if inconsistent:
        return _fail(
            "label_case_consistency_handler",
            "同类标签大小写不一致",
            problem_desc="同一标签存在多种大小写形式",
            facts={"inconsistent_groups": inconsistent},
        )
    return _pass(
        "label_case_consistency_handler",
        "标签大小写保持一致",
        facts={"label_count": len(labels)},
    )


def _upload_rule_visibility(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    page_elements = elements(packet_payload)
    uploads = [
        item for item in page_elements if item.get("tag") == "input" and item.get("type") == "file"
    ]
    if not uploads:
        return _insufficient("upload_rule_visibility_handler", "未找到上传控件")
    keywords = string_list(config.get("rule_keywords")) or [
        "格式",
        "大小",
        "支持",
        "上传",
    ]
    maximum_distance = number(config.get("max_rule_distance_px")) or 240
    nearby: list[dict[str, Any]] = []
    for upload in uploads:
        upload_rect = mapping(upload.get("rect"))
        upload_y = number(upload_rect.get("y"))
        if upload_y is None:
            continue
        for item in page_elements:
            item_text = str(item.get("text") or "").strip()
            hits = [keyword for keyword in keywords if keyword in item_text]
            item_y = number(mapping(item.get("rect")).get("y"))
            if not hits or item_y is None or abs(item_y - upload_y) > maximum_distance:
                continue
            nearby.append(
                {
                    "upload": element_ref(upload),
                    "rule_element": element_ref(item),
                    "keyword_hits": hits,
                    "vertical_distance_px": round(abs(item_y - upload_y), 2),
                }
            )
    if not nearby:
        return _fail(
            "upload_rule_visibility_handler",
            "上传规则未在控件附近明确展示",
            problem_desc="上传控件附近未展示文件格式或大小限制",
            facts={"uploads": [element_ref(item) for item in uploads]},
        )
    return _pass(
        "upload_rule_visibility_handler",
        "上传规则与控件已明确展示",
        facts={"nearby_rule_evidence": nearby[:50]},
    )


def _media_fullscreen(packet_payload: dict[str, Any]) -> CapabilityResult:
    action = last_action(packet_payload)
    if not action or action.get("action") != "click":
        return _insufficient("media_fullscreen_handler", "尚未点击全屏控件")
    target = mapping(action.get("target_before"))
    identity = " ".join(
        (
            str(action.get("selector") or ""),
            str(target.get("text") or ""),
            str(target.get("aria_label") or ""),
            " ".join(string_list(target.get("class_tokens"))),
        )
    )
    if not re.search(r"fullscreen|全屏", identity, re.IGNORECASE):
        return _insufficient(
            "media_fullscreen_handler",
            "无法证明最后一次点击目标是媒体全屏控件",
            facts={"action": action},
        )
    fullscreen = bool(mapping(latest_ui_snapshot(packet_payload).get("document")).get("fullscreen"))
    if fullscreen:
        return _pass(
            "media_fullscreen_handler",
            "媒体已进入全屏",
            facts={"action": action},
        )
    return _fail(
        "media_fullscreen_handler",
        "全屏控件点击后未进入全屏",
        problem_desc="媒体全屏行为未生效",
        facts={"action": action},
    )


def _logout_presence(
    packet_payload: dict[str, Any],
    config: dict[str, Any],
) -> CapabilityResult:
    page = mapping(packet_payload.get("page"))
    if page.get("page_type") not in {"profile", "settings"}:
        return _insufficient(
            "logout_entry_presence_handler",
            "当前页面不是可确认的个人中心或设置页",
        )
    keywords = string_list(config.get("target_keywords")) or [
        "退出登录",
        "退出账号",
        "注销",
    ]
    matches = [
        element_ref(item)
        for item in elements(packet_payload)
        if any(
            keyword.casefold()
            in " ".join(
                (
                    str(item.get("text") or ""),
                    str(item.get("aria_label") or ""),
                )
            ).casefold()
            for keyword in keywords
        )
    ]
    if matches:
        return _pass(
            "logout_entry_presence_handler",
            "账号设置页存在退出入口",
            facts={"matches": matches},
        )
    text = visible_text(packet_payload)
    if not text.strip():
        return _insufficient(
            "logout_entry_presence_handler",
            "页面可见文本为空，无法证明退出入口缺失",
        )
    # 结构里没定位到控件、但页面文字里明明写着「退出登录」：入口存在（截图读字、画布页面都只有文字）
    text_hits = [keyword for keyword in keywords if keyword.casefold() in text.casefold()]
    if text_hits:
        return _pass(
            "logout_entry_presence_handler",
            "账号设置页存在退出入口",
            facts={"matches": [], "matched_text": text_hits, "source": "visible_text"},
        )
    if page.get("page_type") == "profile":
        return _insufficient(
            "logout_entry_presence_handler",
            "只查个人中心就判无退出入口，尚未进入设置或账号页完成搜索。",
            facts={"gap_codes": ["logout_entry_search_incomplete"], "keywords": keywords},
        )
    return _fail(
        "logout_entry_presence_handler",
        "账号设置页缺少退出入口",
        problem_desc="未找到可见的退出登录控件",
        facts={"keywords": keywords},
    )


async def _default_qr_validator(
    *,
    url: str,
    timeout_seconds: float = 8.0,
    max_redirects: int = 5,
) -> dict[str, Any]:
    from services.api.app.agents.tools.qr_link_validation_tool import (
        validate_qr_link,
    )

    return await validate_qr_link(
        url=url,
        timeout_seconds=timeout_seconds,
        max_redirects=max_redirects,
    )


def _pass(
    tool_name: str,
    summary: str,
    *,
    facts: dict[str, Any],
) -> CapabilityResult:
    return CapabilityResult(
        tool_name=tool_name,
        status="ok",
        conclusion="pass",
        confidence=1.0,
        summary=summary,
        problem_desc=None,
        facts=facts,
    )


def _fail(
    tool_name: str,
    summary: str,
    *,
    problem_desc: str,
    facts: dict[str, Any],
) -> CapabilityResult:
    return CapabilityResult(
        tool_name=tool_name,
        status="ok",
        conclusion="fail",
        confidence=1.0,
        summary=summary,
        problem_desc=problem_desc,
        facts=facts,
    )


def _skipped(tool_name: str, reason: str) -> CapabilityResult:
    return CapabilityResult(
        tool_name=tool_name,
        status="ok",
        conclusion="skipped",
        confidence=1.0,
        summary="当前页面未出现规则适用目标，已跳过",
        problem_desc=reason,
        facts={"applicability": "not_applicable"},
    )


def _insufficient(
    tool_name: str,
    reason: str,
    *,
    facts: dict[str, Any] | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        tool_name=tool_name,
        status="insufficient",
        conclusion="evidence_insufficient",
        confidence=0.0,
        summary="证据不足，无法执行确定性判定",
        problem_desc=reason,
        facts=facts or {},
    )
