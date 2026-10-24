"""Hybrid 规则的可信确定性预处理能力。"""

from __future__ import annotations

import re
from typing import Any

from services.api.app.application.rule_capability_facts import (
    candidate_urls as _candidate_urls,
)
from services.api.app.application.rule_capability_facts import (
    compact_action as _compact_action,
)
from services.api.app.application.rule_capability_facts import (
    element_area as _element_area,
)
from services.api.app.application.rule_capability_facts import (
    element_ref as _element_ref,
)
from services.api.app.application.rule_capability_facts import (
    elements as _elements,
)
from services.api.app.application.rule_capability_facts import (
    extract_price_pairs as _extract_price_pairs,
)
from services.api.app.application.rule_capability_facts import (
    extract_rank_values as _extract_rank_values,
)
from services.api.app.application.rule_capability_facts import (
    facts as _facts,
)
from services.api.app.application.rule_capability_facts import (
    integer as _integer,
)
from services.api.app.application.rule_capability_facts import (
    is_media_element as _is_media_element,
)
from services.api.app.application.rule_capability_facts import (
    is_out_of_viewport as _is_out_of_viewport,
)
from services.api.app.application.rule_capability_facts import (
    last_action as _last_action,
)
from services.api.app.application.rule_capability_facts import (
    latest_ui_snapshot as _latest_ui_snapshot,
)
from services.api.app.application.rule_capability_facts import (
    looks_like_ad_control as _looks_like_ad_control,
)
from services.api.app.application.rule_capability_facts import (
    looks_like_ad_element as _looks_like_ad_element,
)
from services.api.app.application.rule_capability_facts import (
    looks_like_promotional_text as _looks_like_promotional_text,
)
from services.api.app.application.rule_capability_facts import (
    looks_like_ugc_element as _looks_like_ugc_element,
)
from services.api.app.application.rule_capability_facts import (
    mapping as _mapping,
)
from services.api.app.application.rule_capability_facts import (
    modal_elements as _modal_elements,
)
from services.api.app.application.rule_capability_facts import (
    number as _number,
)
from services.api.app.application.rule_capability_facts import (
    snapshot_elements as _snapshot_elements,
)
from services.api.app.application.rule_capability_facts import (
    snapshot_sequence as _snapshot_sequence,
)
from services.api.app.application.rule_capability_facts import (
    string_list as _string_list,
)
from services.api.app.application.rule_capability_facts import (
    ui_snapshot as _ui_snapshot,
)
from services.api.app.application.rule_capability_facts import (
    url_syntax_fact as _url_syntax_fact,
)
from services.api.app.application.rule_capability_facts import (
    visible_text as _visible_text,
)
from services.api.app.application.rule_capability_geometry import (
    run_layout_geometry_assertions as _run_layout_geometry_assertions,
)
from services.api.app.domain.rule.forbidden_content import scan_sensitive_words


def run_hybrid_capability_tools(
    *,
    packet_payload: dict[str, Any],
    capability_config: dict[str, Any],
    thresholds: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """执行数据库声明的 Hybrid 预处理 Tool。"""
    configured = capability_config.get("deterministic_tools")
    tool_names = (
        [str(item).strip() for item in configured if str(item).strip()]
        if isinstance(configured, list)
        else []
    )
    return [
        _run_hybrid_tool(
            tool_name=tool_name,
            packet_payload=packet_payload,
            capability_config=capability_config,
            thresholds=thresholds or {},
        )
        for tool_name in tool_names
    ]


def _run_hybrid_tool(
    *,
    tool_name: str,
    packet_payload: dict[str, Any],
    capability_config: dict[str, Any],
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    facts = _facts(packet_payload)
    elements = _elements(packet_payload)
    temporal_elements = _snapshot_elements(packet_payload)
    visible_text = _visible_text(packet_payload)
    if tool_name == "text_style_extractor_tool":
        styled_text = [
            {
                "element": _element_ref(item),
                "style": {
                    key: style.get(key)
                    for key in (
                        "font_family",
                        "font_size",
                        "font_weight",
                        "font_style",
                        "text_transform",
                    )
                    if style.get(key) not in (None, "")
                },
            }
            for item in elements
            if str(item.get("text") or "").strip()
            and (style := _mapping(item.get("style")))
        ]
        return _tool_result(
            tool_name,
            status="ok" if styled_text else "insufficient",
            facts={"styled_text_elements": styled_text[:100]},
            reason=None if styled_text else "未提取到带计算样式的可见文本元素",
        )
    if tool_name == "text_truncation_detector_tool":
        # 工具层只出结构化截断事实，「核心信息是否受损」由语义 Judge 下结论。
        truncated: list[dict[str, Any]] = []
        measured = 0
        for item in elements:
            metrics = _mapping(item.get("text_metrics"))
            if not metrics:
                continue
            measured += 1
            if not str(item.get("text") or "").strip():
                continue
            horizontal = bool(metrics.get("horizontal_overflow"))
            vertical = bool(metrics.get("vertical_overflow"))
            if not horizontal and not vertical:
                continue
            style = _mapping(item.get("style"))
            overflow_values = " ".join(
                str(style.get(key) or "")
                for key in ("overflow", "overflow_x", "overflow_y")
            )
            if "auto" in overflow_values or "scroll" in overflow_values:
                # 可滚动容器的溢出是交互设计，不算文本截断
                continue
            ellipsis_terminated = bool(metrics.get("text_overflow_ellipsis")) or (
                str(style.get("text_overflow") or "") == "ellipsis"
            )
            line_clamp = (
                metrics.get("line_clamp")
                or style.get("webkit_line_clamp")
                or style.get("line_clamp")
            )
            has_line_clamp = line_clamp not in (None, "", "none")
            clip_capable = (
                ellipsis_terminated
                or has_line_clamp
                or "hidden" in overflow_values
                or "clip" in overflow_values
            )
            if not clip_capable:
                continue
            truncated.append(
                {
                    "element": _element_ref(item),
                    "horizontal_overflow": horizontal,
                    "vertical_overflow": vertical,
                    "ellipsis_terminated": ellipsis_terminated,
                    "line_clamp": str(line_clamp)[:32] if has_line_clamp else None,
                    "visible_width_ratio": _visible_ratio(
                        metrics.get("client_width"), metrics.get("scroll_width")
                    ),
                    "visible_height_ratio": _visible_ratio(
                        metrics.get("client_height"), metrics.get("scroll_height")
                    ),
                }
            )
        return _tool_result(
            tool_name,
            status="ok" if measured else "insufficient",
            facts={
                "truncated_text_elements": truncated[:100],
                "truncated_count": len(truncated),
                "hard_clipped_count": sum(
                    1 for item in truncated if not item["ellipsis_terminated"]
                ),
                "measured_element_count": measured,
            },
            reason=None if measured else "采集未包含文本截断字段（text_metrics）",
        )
    if tool_name == "typography_assertion_handler":
        serif = [
            _element_ref(item)
            for item in elements
            if re.search(
                r"\b(serif|times new roman|songti|simsun|宋体)\b",
                str(_mapping(item.get("style")).get("font_family") or ""),
                re.IGNORECASE,
            )
        ]
        # 规则要的是字母、数字用半角：ＡＢＣ、１２３、全角空格才是问题；中文标点（，。！？：；）
        # 本来就是全角，不能当违规——原来 FF01~FF5E 整段都算，等于每个中文逗号都报一次。
        fullwidth = [
            character
            for character in visible_text
            if character == "\u3000"
            or "\uff10" <= character <= "\uff19"
            or "\uff21" <= character <= "\uff3a"
            or "\uff41" <= character <= "\uff5a"
        ][:50]
        return _tool_result(
            tool_name,
            status="ok",
            facts={"serif_elements": serif[:20], "fullwidth_ascii": fullwidth},
        )
    if tool_name == "media_element_extractor_tool":
        media_state = _mapping(facts.get("media_state"))
        media_state_items = [
            dict(item)
            for item in list(media_state.get("items") or [])[:20]
            if isinstance(item, dict)
        ]
        if _native_playback_lacks_timing_and_switch(
            packet_payload=packet_payload,
            capability_config=capability_config,
            media_state_items=media_state_items,
        ):
            return _tool_result(
                tool_name,
                status="insufficient",
                facts={
                    "media_state_items": media_state_items,
                    "gap_codes": [NATIVE_PLAYBACK_SWITCH_GAP],
                },
                reason=NATIVE_PLAYBACK_SWITCH_REASON,
            )
        media_elements = [
            {
                "element": _element_ref(item),
                "src": item.get("src"),
                "alt": item.get("alt"),
                "image": _mapping(item.get("image")),
                "media": _mapping(item.get("media")),
                "style": {
                    "object_fit": _mapping(item.get("style")).get("object_fit"),
                },
            }
            for item in _distinct_temporal_elements(temporal_elements)
            if _is_media_element(item)
        ]
        return _tool_result(
            tool_name,
            status="ok" if media_elements or media_state_items else "insufficient",
            facts={
                "media_elements": media_elements[:100],
                "media_state_items": media_state_items,
                "display_candidates": list(media_state.get("display_candidates") or [])[:30],
            },
            reason=(
                None
                if media_elements or media_state_items
                else "当前页面未提取到可见图片或媒体状态"
            ),
        )
    if tool_name == "multi_frame_capture_tool":
        sequence = _snapshot_sequence(packet_payload)
        is_sequence = _ui_snapshot(packet_payload).get("sequence") is True
        frame_measurements: list[dict[str, Any]] = []
        for position, frame in enumerate(sequence[:20]):
            frame_media: list[dict[str, Any]] = []
            frame_elements = frame.get("elements")
            if not isinstance(frame_elements, list):
                continue
            for item in frame_elements:
                if not isinstance(item, dict) or not _is_media_element(item):
                    continue
                rect = _mapping(item.get("rect"))
                rendered_width = _number(rect.get("width"))
                rendered_height = _number(rect.get("height"))
                if not (
                    rendered_width
                    and rendered_width > 0
                    and rendered_height
                    and rendered_height > 0
                ):
                    continue
                image = _mapping(item.get("image"))
                frame_media.append(
                    {
                        "element": _element_ref(item),
                        "rendered_width": rendered_width,
                        "rendered_height": rendered_height,
                        "natural_width": _number(image.get("natural_width")),
                        "natural_height": _number(image.get("natural_height")),
                    }
                )
                if len(frame_media) >= 20:
                    break
            if frame_media:
                frame_measurements.append(
                    {
                        "frame_index": frame.get("index", position),
                        "media": frame_media,
                    }
                )
        measurable_frame_count = len(frame_measurements)
        has_required_measurements = (
            is_sequence and len(sequence) >= 2 and measurable_frame_count >= 2
        )
        return _tool_result(
            tool_name,
            status="ok" if has_required_measurements else "insufficient",
            facts={
                "is_sequence": is_sequence,
                "frame_count": len(sequence),
                "interval_ms": _ui_snapshot(packet_payload).get("interval_ms"),
                "measurable_frame_count": measurable_frame_count,
                "frame_media_measurements": frame_measurements,
            },
            reason=(
                None
                if has_required_measurements
                else (
                    "当前证据不是至少两帧的定时采集"
                    if not is_sequence or len(sequence) < 2
                    else "至少两帧缺少可判定的媒体渲染尺寸"
                )
            ),
        )
    if tool_name in {"image_quality_analyzer_tool", "image_geometry_tool"}:
        anomalies: list[dict[str, Any]] = []
        distinct_media = [
            item
            for item in _distinct_temporal_elements(
                temporal_elements,
                preserve_geometry_variants=True,
            )
            if _is_media_element(item)
        ]
        for item in distinct_media:
            image = _mapping(item.get("image"))
            rect = _mapping(item.get("rect"))
            natural_width = _number(image.get("natural_width"))
            natural_height = _number(image.get("natural_height"))
            rendered_width = _number(rect.get("width"))
            rendered_height = _number(rect.get("height"))
            if not all(
                value and value > 0
                for value in (
                    natural_width,
                    natural_height,
                    rendered_width,
                    rendered_height,
                )
            ):
                continue
            natural_ratio = natural_width / natural_height
            rendered_ratio = rendered_width / rendered_height
            delta = abs(rendered_ratio - natural_ratio) / natural_ratio
            object_fit = str(_mapping(item.get("style")).get("object_fit") or "")
            if delta > 0.08 and object_fit not in {"cover", "contain"}:
                anomalies.append(
                    {
                        "element": _element_ref(item),
                        "ratio_delta": round(delta, 4),
                        "object_fit": object_fit,
                    }
                )
        return _tool_result(
            tool_name,
            status="ok",
            facts={
                "geometry_anomalies": anomalies,
                "image_count": len(distinct_media),
                "measurable_image_count": sum(
                    bool(_mapping(item.get("image"))) for item in distinct_media
                ),
            },
        )
    if tool_name == "visible_text_extractor_tool":
        return _tool_result(
            tool_name,
            status="ok" if visible_text.strip() else "insufficient",
            facts={
                "character_count": len(visible_text),
                "line_count": len(
                    [line for line in visible_text.splitlines() if line.strip()]
                ),
                "text_excerpt": visible_text[:2_000],
            },
            reason=None if visible_text.strip() else "可信 visible_text 为空",
        )
    if tool_name == "layout_region_extractor_tool":
        viewport = _mapping(_latest_ui_snapshot(packet_payload).get("viewport"))
        out_of_viewport = [
            _element_ref(item)
            for item in elements
            if _is_out_of_viewport(item, viewport)
        ]
        return _tool_result(
            tool_name,
            status="ok" if viewport else "insufficient",
            facts={
                "viewport": viewport,
                "visible_element_count": len(elements),
                "out_of_viewport_elements": out_of_viewport[:50],
            },
            reason=None if viewport else "缺少浏览器 viewport 几何事实",
        )
    if tool_name == "layout_geometry_assertion_handler":
        outcome = _run_layout_geometry_assertions(
            packet_payload=packet_payload,
            capability_config=capability_config,
        )
        return _tool_result(
            tool_name,
            status=str(outcome.get("status") or "insufficient"),
            facts=_mapping(outcome.get("facts")),
            reason=outcome.get("reason"),
        )
    if tool_name == "network_console_summary_tool":
        network = _mapping(facts.get("network_summary"))
        console = _mapping(facts.get("console_summary"))
        return _tool_result(
            tool_name,
            status="ok" if network or console else "insufficient",
            facts={
                "network_entry_count": network.get("entry_count", 0),
                "network_failed_responses": network.get("failed_responses", [])[:50],
                "console_entry_count": console.get("entry_count", 0),
                "console_errors": console.get("errors", [])[:50],
            },
            reason=None if network or console else "未采集到网络或控制台事件",
        )
    if tool_name == "blank_module_detector_tool":
        network = _mapping(facts.get("network_summary"))
        console = _mapping(facts.get("console_summary"))
        tag_counts = _mapping(_mapping(facts.get("dom_summary")).get("tag_counts"))
        blank_signal = (
            not visible_text.strip()
            and len(elements) == 0
            and sum(_integer(value) for value in tag_counts.values()) > 0
        )
        return _tool_result(
            tool_name,
            status="ok" if blank_signal else "insufficient",
            facts={
                "blank_page_signal": blank_signal,
                "visible_text_character_count": len(visible_text),
                "visible_element_count": len(elements),
                "network_error_count": len(network.get("failed_responses") or []),
                "console_error_count": len(console.get("errors") or []),
            },
            reason=(
                None
                if blank_signal
                else "结构化快照无法独立证明局部模块是否异常留白，需视觉 Judge 复核"
            ),
        )
    if tool_name == "state_snapshot_tool":
        action = _last_action(packet_payload)
        state_diff = _mapping(action.get("state_diff")) if action else {}
        return _tool_result(
            tool_name,
            status="ok" if state_diff else "insufficient",
            facts={
                "action": _compact_action(action),
                "state_diff": state_diff,
                "current_element_count": len(elements),
            },
            reason=None if state_diff else "缺少同一动作前后的状态快照",
        )
    if tool_name == "network_event_summary_tool":
        network = _mapping(facts.get("network_summary"))
        return _tool_result(
            tool_name,
            status="ok" if network.get("entry_count") else "insufficient",
            facts={
                "entry_count": network.get("entry_count", 0),
                "failed_responses": network.get("failed_responses", [])[:50],
                "entries": network.get("entries", [])[:50],
            },
            reason=(
                None
                if network.get("entry_count")
                else "本次页面采集没有可用网络事件"
            ),
        )
    if tool_name == "interaction_state_diff_tool":
        if _payment_entry_chain_missing(
            packet_payload=packet_payload,
            capability_config=capability_config,
        ):
            return _tool_result(
                tool_name,
                status="insufficient",
                facts={
                    "action": _compact_action(_last_action(packet_payload)),
                    "gap_codes": [PAYMENT_ENTRY_CHAIN_GAP],
                },
                reason=PAYMENT_ENTRY_CHAIN_REASON,
            )
        action = _last_action(packet_payload)
        state_diff = _mapping(action.get("state_diff")) if action else {}
        return _tool_result(
            tool_name,
            status="ok" if state_diff else "insufficient",
            facts={
                "action": _compact_action(action),
                "state_diff": state_diff,
            },
            reason=None if state_diff else "尚未执行可逆交互或未生成状态差异",
        )
    if tool_name == "transient_message_capture_tool":
        action = _last_action(packet_payload)
        observation = _mapping(action.get("transient_message_observation"))
        structured_messages = [
            dict(item)
            for item in list(action.get("transient_messages") or [])[:50]
            if isinstance(item, dict)
        ]
        if observation.get("status") == "observed":
            return _tool_result(
                tool_name,
                status="ok",
                facts={
                    "schema_version": "transient-message-facts.v1",
                    "messages": structured_messages,
                    "message_count": len(structured_messages),
                    "absence_confirmed": not structured_messages,
                    "observation": observation,
                },
            )
        snapshots = _snapshot_sequence(packet_payload)
        messages = [
            {
                "frame_index": index,
                "elements": [
                    _element_ref(item)
                    for item in snapshot.get("elements", [])
                    if isinstance(item, dict)
                    and re.search(
                        r"toast|message|notice|alert|提示",
                        " ".join(
                            (
                                str(item.get("role") or ""),
                                str(item.get("text") or ""),
                                " ".join(_string_list(item.get("class_tokens"))),
                            )
                        ),
                        re.IGNORECASE,
                    )
                ],
            }
            for index, snapshot in enumerate(snapshots)
        ]
        messages = [item for item in messages if item["elements"]]
        return _tool_result(
            tool_name,
            status="ok" if messages else "insufficient",
            facts={
                "schema_version": "transient-message-facts.v1",
                "messages": messages[:20],
                "frame_count": len(snapshots),
                "legacy_snapshot_fallback": True,
            },
            reason=(
                None
                if messages
                else "动作窗口未启用瞬态观察，且多帧证据未捕获到提示元素"
            ),
        )
    if tool_name == "punctuation_assertion_handler":
        snippets = [
            line.strip()
            for line in visible_text.splitlines()
            if line.strip().endswith(("!", "！"))
        ][:50]
        return _tool_result(
            tool_name,
            status="ok",
            facts={"exclamation_snippets": snippets},
        )
    if tool_name in {"product_price_extractor_tool", "price_normalization_tool"}:
        pairs = _extract_price_pairs(packet_payload, capability_config)
        return _tool_result(
            tool_name,
            status="ok" if pairs else "insufficient",
            facts={"price_pairs": pairs},
            reason=None if pairs else "未提取到同一商品的现价与原价",
        )
    if tool_name == "cross_page_fact_store":
        journey = _journey(packet_payload)
        price_pages = []
        for step in journey:
            step_packet = {
                "facts": {
                    "visible_text": step.get("visible_text_excerpt"),
                    "ui_snapshot": step.get("ui_snapshot"),
                }
            }
            pairs = _extract_price_pairs(step_packet, capability_config)
            if pairs:
                price_pages.append(
                    {
                        "page_capture_id": step.get("page_capture_id"),
                        "page_type": step.get("page_type"),
                        "url": step.get("url"),
                        "price_pairs": pairs,
                    }
                )
        return _tool_result(
            tool_name,
            status="ok" if len(price_pages) >= 2 else "insufficient",
            facts={"price_pages": price_pages},
            reason=(
                None
                if len(price_pages) >= 2
                else "可信旅程中未形成至少两个页面的价格事实"
            ),
        )
    if tool_name == "ad_region_detector_tool":
        regions, signals = _ad_region_candidates(
            packet_payload=packet_payload,
            temporal_elements=temporal_elements,
            visible_text=visible_text,
        )
        app_ad_region = _app_ad_landing_source_region(packet_payload)
        if not regions and app_ad_region:
            regions = [app_ad_region]
            signals = {**signals, "app_controlled_ad_click": True}
        return _tool_result(
            tool_name,
            status="ok" if regions else "insufficient",
            facts={
                "ad_regions": regions[:100],
                "signals": signals,
                "absence_confirmed": _ad_absence_confirmed(
                    regions=regions,
                    signals=signals,
                ),
            },
            reason=None if regions else "未从结构化快照定位到广告区域",
        )
    if tool_name in {"safe_click_observer_tool", "landing_page_probe_tool"}:
        action = _last_action(packet_payload)
        if capability_config.get("batch_group") != "ad-landing":
            observed, reason = _safe_click_pair_status(action)
            return _tool_result(
                tool_name,
                status="ok" if observed else "insufficient",
                facts={"action": _compact_action(action)},
                reason=None if observed else reason,
            )
        ad_evidence = _mapping(action.get("ad_navigation_evidence"))
        screenshot_pair = _ad_landing_screenshot_pair(packet_payload)
        logical_pair = _ad_landing_logical_pair(ad_evidence, screenshot_pair)
        landing_valid = _ad_landing_destination_valid(ad_evidence)
        observed = bool(
            action
            and action.get("action") in {"click", "tap"}
            and ad_evidence.get("status") == "observed"
            and ad_evidence.get("pair_complete") is True
            and ad_evidence.get("navigation_mode")
            in {"same_page", "new_page", "external_browser"}
            and landing_valid
            and logical_pair
            and screenshot_pair
        )
        return _tool_result(
            tool_name,
            status="ok" if observed else "insufficient",
            facts={
                "action": _compact_action(action),
                "screenshot_pair": screenshot_pair,
            },
            reason=(
                None
                if observed
                else "缺少同一广告点击窗口的源素材、落地页和成对截图证据"
            ),
        )
    if tool_name == "visual_semantic_compare_tool":
        if capability_config.get("batch_group") == "cover-content":
            return _cover_content_compare(packet_payload)
        action = _last_action(packet_payload)
        if capability_config.get("batch_group") != "ad-landing":
            has_pair = bool(
                action.get("source_screenshot_id")
                and action.get("target_screenshot_id")
            )
            return _tool_result(
                tool_name,
                status="ok" if has_pair else "insufficient",
                facts={
                    "source_screenshot_id": action.get("source_screenshot_id"),
                    "target_screenshot_id": action.get("target_screenshot_id"),
                },
                reason=None if has_pair else "缺少广告素材与落地页的成对截图证据",
            )
        ad_evidence = _mapping(action.get("ad_navigation_evidence"))
        screenshot_pair = _ad_landing_screenshot_pair(packet_payload)
        has_pair = bool(
            ad_evidence.get("status") == "observed"
            and ad_evidence.get("pair_complete") is True
            and ad_evidence.get("navigation_mode")
            in {"same_page", "new_page", "external_browser"}
            and _ad_landing_destination_valid(ad_evidence)
            and _ad_landing_logical_pair(ad_evidence, screenshot_pair)
            and screenshot_pair
        )
        return _tool_result(
            tool_name,
            status="ok" if has_pair else "insufficient",
            facts={
                "ad_navigation_evidence": ad_evidence,
                "screenshot_pair": screenshot_pair,
            },
            reason=None if has_pair else "缺少广告素材与落地页的成对截图证据",
        )
    if tool_name == "network_fault_injection_tool":
        action = _last_action(packet_payload)
        if capability_config.get("batch_group") == "ad-fallback":
            return _ad_load_fallback_result(tool_name, action)
        injected, reason = _network_fault_status(action)
        return _tool_result(
            tool_name,
            status="ok" if injected else "insufficient",
            facts={"action": _compact_action(action)},
            reason=None if injected else reason,
        )
    if tool_name == "ad_modal_detector_tool":
        modals = _modal_elements(packet_payload)
        return _tool_result(
            tool_name,
            status="ok" if modals else "insufficient",
            facts={"modals": modals[:50]},
            reason=None if modals else "当前快照未定位到可见广告弹窗或蒙层",
        )
    if tool_name == "fallback_state_assertion_tool":
        action = _last_action(packet_payload)
        if capability_config.get("batch_group") == "ad-fallback":
            return _ad_load_fallback_result(tool_name, action)
        media = [item for item in elements if item.get("media") or item.get("image")]
        fallback_keywords = _string_list(
            capability_config.get("fallback_keywords")
        ) or ["重试", "暂时无法", "加载失败"]
        text_hit = [item for item in fallback_keywords if item in visible_text]
        has_fault, reason = _network_fault_status(action)
        fallback_observed = bool(text_hit or media)
        if has_fault and not fallback_observed:
            reason = "网络故障注入后未观察到降级提示或仍可见的媒体占位"
        return _tool_result(
            tool_name,
            status="ok" if has_fault and fallback_observed else "insufficient",
            facts={
                "action": _compact_action(action),
                "fault_injected": has_fault,
                "fallback_text_hits": text_hit,
                "visible_media_count": len(media),
            },
            reason=None if has_fault and fallback_observed else reason,
        )
    if tool_name == "ad_control_inventory_tool":
        controls = [
            _element_ref(item)
            for item in elements
            if _looks_like_ad_control(item)
        ]
        return _tool_result(
            tool_name,
            status="ok" if controls else "insufficient",
            facts={"controls": controls[:100]},
            reason=None if controls else "未定位到广告关闭、跳过或进入控件",
        )
    if tool_name == "safe_hit_test_tool":
        controls = [
            {
                "element": _element_ref(item),
                "hit_test": _mapping(item.get("hit_test")),
            }
            for item in elements
            if _looks_like_ad_control(item)
        ]
        tested = [
            item
            for item in controls
            if item["hit_test"].get("center_hits_target") is not None
        ]
        return _tool_result(
            tool_name,
            status="ok" if tested else "insufficient",
            facts={"controls": tested[:100]},
            reason=None if tested else "目标控件缺少浏览器中心点命中测试事实",
        )
    if tool_name == "control_behavior_assertion_tool":
        action = _last_action(packet_payload)
        if capability_config.get("batch_group") == "service-entry":
            return _service_entry_control_result(
                tool_name=tool_name,
                action=action,
                elements=elements,
                visible_text=visible_text,
            )
        if (
            capability_config.get("batch_group") == "player-controls"
            or action.get("action") == "seek_media"
        ):
            return _media_seek_control_result(
                tool_name=tool_name,
                action=action,
                facts=facts,
                thresholds=thresholds,
            )
        target = _mapping(action.get("target_before"))
        observed = (
            action.get("action") == "click"
            and _looks_like_ad_control(target)
            and bool(action.get("state_diff"))
        )
        return _tool_result(
            tool_name,
            status="ok" if observed else "insufficient",
            facts={"action": _compact_action(action)},
            reason=None if observed else "缺少广告控件点击后的状态变化证据",
        )
    if tool_name == "ugc_link_extractor_tool":
        content_attribution = _mapping(facts.get("content_attribution"))
        links = [
            {
                "url": item.get("href"),
                "element": _element_ref(item),
                "ugc_context": _looks_like_ugc_element(item),
            }
            for item in elements
            if item.get("href")
        ]
        return _tool_result(
            tool_name,
            status="ok" if links else "insufficient",
            facts={
                "links": links[:100],
                "content_attribution": content_attribution,
            },
            reason=None if links else "未从当前页面提取到链接",
        )
    if tool_name == "safe_url_reputation_tool":
        urls = _candidate_urls(packet_payload)
        syntax = [_url_syntax_fact(url) for url in urls[:100]]
        return _tool_result(
            tool_name,
            status="insufficient",
            facts={"syntax_checks": syntax},
            reason=(
                "已完成 URL 语法与协议检查，但缺少可信信誉数据源，"
                "不得据此断言链接安全或恶意"
            ),
        )
    if tool_name == "ugc_content_extractor_tool":
        content_attribution = _mapping(facts.get("content_attribution"))
        candidates = [
            _element_ref(item)
            for item in elements
            if _looks_like_ugc_element(item)
            and str(item.get("text") or "").strip()
        ]
        if (
            not candidates
            and content_attribution.get("scope") == "ugc"
            and visible_text.strip()
        ):
            candidates = [
                {
                    "text_excerpt": visible_text[:2_000],
                    "attribution": content_attribution,
                    "source": "attributed_visible_text",
                }
            ]
        return _tool_result(
            tool_name,
            status="ok" if candidates else "insufficient",
            facts={
                "ugc_candidates": candidates[:100],
                "content_attribution": content_attribution,
            },
            reason=None if candidates else "无法可靠区分平台文案与用户生成内容",
        )
    if tool_name == "sensitive_word_detection_tool":
        categories = _string_list(capability_config.get("sensitive_word_categories"))
        if categories:
            try:
                scan = scan_sensitive_words(visible_text, categories=categories)
            except ValueError as exc:
                return _tool_result(
                    tool_name,
                    status="insufficient",
                    facts={"requested_categories": categories},
                    reason=str(exc),
                )
            return _tool_result(
                tool_name,
                status="ok",
                facts={
                    "catalog_version": scan["catalog_version"],
                    "scanned_categories": scan["scanned_categories"],
                    "hit_count": scan["hit_count"],
                    "effective_hit_count": scan["effective_hit_count"],
                    "hits": scan["hits"][:100],
                },
            )
        dictionary = _string_list(capability_config.get("sensitive_words"))
        hits = [
            word for word in dictionary if word.casefold() in visible_text.casefold()
        ]
        return _tool_result(
            tool_name,
            status="ok" if dictionary else "insufficient",
            facts={"hits": hits, "dictionary_size": len(dictionary)},
            reason=None if dictionary else "数据库未配置敏感词库版本或词表",
        )
    if tool_name in {"ranking_fact_extractor_tool", "numeric_order_assertion_tool"}:
        ranks = _extract_rank_values(packet_payload)
        ordered = all(
            left <= right
            for left, right in zip(ranks, ranks[1:], strict=False)
        )
        return _tool_result(
            tool_name,
            status="ok" if len(ranks) >= 2 else "insufficient",
            facts={"rank_values": ranks, "ascending": ordered},
            reason=None if len(ranks) >= 2 else "未提取到至少两个可比较的榜单序号",
        )
    if tool_name == "activity_state_extractor_tool":
        state_keywords = (
            "进行中",
            "已结束",
            "已下线",
            "回顾",
            "活动结束",
            "报名中",
        )
        pages = [
            {
                "page_capture_id": step.get("page_capture_id"),
                "page_type": step.get("page_type"),
                "url": step.get("url"),
                "state_hits": [
                    keyword
                    for keyword in state_keywords
                    if keyword in str(step.get("visible_text_excerpt") or "")
                ],
            }
            for step in _journey(packet_payload)
        ]
        pages = [item for item in pages if item["state_hits"]]
        state_hits = [keyword for keyword in state_keywords if keyword in visible_text]
        return _tool_result(
            tool_name,
            status="ok" if state_hits or pages else "insufficient",
            facts={"state_hits": state_hits, "journey_state_pages": pages},
            reason=(
                None
                if state_hits or pages
                else "未提取到明确的活动生命周期状态"
            ),
        )
    if tool_name == "cross_page_state_assertion_tool":
        state_keywords = ("进行中", "已结束", "已下线", "回顾", "活动结束", "报名中")
        pages = [
            {
                "page_capture_id": step.get("page_capture_id"),
                "page_type": step.get("page_type"),
                "url": step.get("url"),
                "state_hits": [
                    keyword
                    for keyword in state_keywords
                    if keyword in str(step.get("visible_text_excerpt") or "")
                ],
            }
            for step in _journey(packet_payload)
        ]
        pages = [item for item in pages if item["state_hits"]]
        return _tool_result(
            tool_name,
            status="ok" if len(pages) >= 2 else "insufficient",
            facts={"state_pages": pages},
            reason=None if len(pages) >= 2 else "缺少列表页与详情页的成对活动状态事实",
        )
    return _tool_result(
        tool_name,
        status="insufficient",
        facts={},
        reason="数据库引用的能力 Tool 尚未注册",
    )


def _safe_click_pair_status(action: dict[str, Any]) -> tuple[bool, str]:
    """验证通用安全点击是否绑定到同一动作的前后观察结果。"""

    if action.get("action") != "click":
        return False, "尚未执行安全单击动作"

    missing_identity = [
        key
        for key in ("action_execution_id", "source_capture_id", "result_capture_id")
        if not isinstance(action.get(key), str) or not action[key].strip()
    ]
    if missing_identity:
        return False, "安全单击缺少动作或前后 Capture 身份：" + "、".join(missing_identity)

    source_capture_id = str(action["source_capture_id"]).strip()
    result_capture_id = str(action["result_capture_id"]).strip()
    if source_capture_id == result_capture_id:
        return False, "安全单击的 source_capture_id 与 result_capture_id 不能相同"

    target_before = _mapping(action.get("target_before"))
    if not target_before:
        return False, "安全单击缺少 target_before，无法证明实际点击目标"
    target_after = _mapping(action.get("target_after"))

    navigation = _mapping(action.get("navigation"))
    before_url = str(action.get("before_url") or navigation.get("before_url") or "").strip()
    after_url = str(action.get("after_url") or navigation.get("after_url") or "").strip()
    new_page_urls = _string_list(action.get("new_page_urls")) or _string_list(
        navigation.get("new_page_urls")
    )
    state_diff = _mapping(action.get("state_diff"))
    has_navigation_result = bool(before_url and (after_url or new_page_urls))
    has_state_result = bool(target_after or state_diff)
    if not has_navigation_result and not has_state_result:
        return False, "安全单击缺少同一动作的 after URL、打开页或状态差异"

    failure_values = {
        "failed",
        "failure",
        "error",
        "timeout",
        "timed_out",
        "blocked",
        "rejected",
        "cancelled",
        "canceled",
        "not_observed",
        "evidence_failed",
        "evidence_pending",
        "pending",
        "no_effect",
        "effect_unexpected",
    }
    for key in (
        "result",
        "status",
        "execution_outcome",
        "verification_outcome",
    ):
        value = str(action.get(key) or "").strip().casefold()
        if value in failure_values:
            return False, f"安全单击字段 {key} 表示执行或验证失败"
    # Interaction Runtime 的成功默认值仍是 unverified；这里已经用前后 Capture
    # 身份及导航/状态差异完成规则侧验证，因此不再把该默认值单独当作失败。
    return True, ""


def _ad_load_fallback_result(tool_name: str, action: dict[str, Any]) -> dict[str, Any]:
    """广告兜底只认设备上的广告加载故障，以及故障生效后的关闭或跳过。"""

    from services.api.app.application.ad_load_fallback import assess_ad_load_fallback

    assessment = assess_ad_load_fallback(action)
    reason = {
        "ad_load_fault_injection_unsupported": "未支持设备上的广告加载故障注入",
        "ad_load_fault_effect_missing": "缺少广告加载故障已经生效的结果",
        "ad_load_fallback_close_or_skip_missing": "缺少故障生效后的关闭或跳过结果",
    }
    gap_codes = list(assessment.get("gap_codes") or [])
    summary = "；".join(reason[code] for code in gap_codes if code in reason)
    return _tool_result(
        tool_name,
        status="ok" if assessment.get("complete") is True else "insufficient",
        facts={**assessment, "action": _compact_action(action)},
        reason=(
            None
            if assessment.get("complete") is True
            else summary or "广告加载失败兜底证据不足"
        ),
    )


def _network_fault_status(action: dict[str, Any]) -> tuple[bool, str]:
    """要求网络故障动作留下真实注入、命中和清理结果。"""

    if action.get("action") != "network_fault":
        return False, "尚未在隔离环境执行网络故障动作"
    if action.get("result") != "network_fault_injected":
        return False, "网络故障动作缺少 result=network_fault_injected"
    matched_request_count = action.get("matched_request_count")
    if (
        isinstance(matched_request_count, bool)
        or not isinstance(matched_request_count, int)
        or matched_request_count < 1
    ):
        return False, "网络故障动作缺少至少一个真实匹配请求"
    if action.get("route_restored") is not True:
        return False, "网络故障动作未证明路由已恢复"
    return True, ""


PAYMENT_ENTRY_CHAIN_GAP = "payment_entry_chain_missing"
PAYMENT_ENTRY_CHAIN_REASON = "充值入口、充值页和选档结果未按实际操作串联，缺对应画面。"
_PAYMENT_ENTRY_MARKERS = ("充值", "开通会员", "金币不足")
_PAYMENT_TIER_MARKERS = ("月卡", "季卡", "年卡", "终身", "档位", "套餐", "会员卡")


def _payment_entry_chain_missing(
    *,
    packet_payload: dict[str, Any],
    capability_config: dict[str, Any],
) -> bool:
    """充值入口、充值页和选档没有各自的操作与画面时，不能判充值页失败。"""
    if capability_config.get("batch_group") != "payment-entry":
        return False
    page = _mapping(packet_payload.get("page"))
    if str(page.get("surface_kind") or "").casefold() not in {"android", "ios"}:
        return False
    actions = [
        item
        for item in list(_facts(packet_payload).get("action_log") or [])
        if isinstance(item, dict)
    ]
    return not (
        _linked_payment_click(actions, _PAYMENT_ENTRY_MARKERS)
        and _linked_payment_click(actions, _PAYMENT_TIER_MARKERS)
    )


def _linked_payment_click(actions: list[dict[str, Any]], markers: tuple[str, ...]) -> bool:
    for action in actions:
        if action.get("action") != "click":
            continue
        text = " ".join(
            str(action.get(key) or "")
            for key in ("semantic_target", "purpose", "selector")
        )
        target = action.get("target_before")
        if isinstance(target, dict):
            text = f"{text} {target.get('text') or ''} {target.get('aria_label') or ''}"
        if not any(marker in text for marker in markers):
            continue
        source = action.get("source_capture_id")
        result = action.get("result_capture_id")
        if (
            isinstance(source, str)
            and isinstance(result, str)
            and source
            and result
            and source != result
        ):
            return True
    return False


NATIVE_PLAYBACK_SWITCH_GAP = "native_playback_switch_missing"
NATIVE_PLAYBACK_SWITCH_REASON = (
    "原生媒体未提供播放时间与进度，缺实际起播及上下条切换的证据。"
)
_PLAYBACK_SWITCH_MARKERS = ("下一条", "上一条", "下条", "上条")


def _native_playback_lacks_timing_and_switch(
    *,
    packet_payload: dict[str, Any],
    capability_config: dict[str, Any],
    media_state_items: list[dict[str, Any]],
) -> bool:
    """长视频和短视频没有原生进度，也没有起播或上下条切换时，不能判播放失败。"""
    if capability_config.get("batch_group") != "player-playback":
        return False
    tools = capability_config.get("deterministic_tools")
    if isinstance(tools, list) and "interaction_state_diff_tool" in tools:
        return False
    page = _mapping(packet_payload.get("page"))
    if str(page.get("surface_kind") or "").casefold() not in {"android", "ios"}:
        return False
    if not media_state_items or any(_media_item_has_timing(item) for item in media_state_items):
        return False
    return not _playback_start_or_switch_observed(packet_payload)


def _media_item_has_timing(item: dict[str, Any]) -> bool:
    current = _number(item.get("current_time"))
    duration = _number(item.get("duration"))
    if current is not None and duration is not None and duration > 0 and current >= 0:
        return True
    progress = _number(item.get("progress_ratio"))
    return progress is not None and 0 <= progress <= 1


def _playback_start_or_switch_observed(packet_payload: dict[str, Any]) -> bool:
    started = {"playback_started", "media_playing", "media_playback_started"}
    for action in _facts(packet_payload).get("action_log") or []:
        if not isinstance(action, dict):
            continue
        if action.get("action") == "play" or action.get("result") in started:
            return True
        if action.get("action") not in {"swipe", "fling", "scroll"}:
            continue
        text = " ".join(
            str(action.get(key) or "") for key in ("semantic_target", "purpose", "selector")
        )
        if any(marker in text for marker in _PLAYBACK_SWITCH_MARKERS):
            return True
    return False


SERVICE_ENTRY_ARRIVAL_GAP = "service_entry_arrival_missing"
SERVICE_ENTRY_ARRIVAL_REASON = "客服入口已看见，尚未点击并采集实际到达的客服页面。"
_SERVICE_ENTRY_PATTERN = re.compile(r"客服")


def _service_entry_control_result(
    *,
    tool_name: str,
    action: dict[str, Any],
    elements: list[dict[str, Any]],
    visible_text: str,
) -> dict[str, Any]:
    """看见客服入口还没点进客服页时，不能把当前页判成入口可用或不可用。"""
    visible = _service_entry_visible(visible_text, elements)
    arrived = visible and _service_entry_arrived(action)
    if visible and not arrived:
        return _tool_result(
            tool_name,
            status="insufficient",
            facts={
                "action": _compact_action(action),
                "service_entry_visible": True,
                "service_page_reached": False,
                "gap_codes": [SERVICE_ENTRY_ARRIVAL_GAP],
            },
            reason=SERVICE_ENTRY_ARRIVAL_REASON,
        )
    return _tool_result(
        tool_name,
        status="ok",
        facts={
            "action": _compact_action(action),
            "service_entry_visible": visible,
            "service_page_reached": arrived,
        },
    )


def _service_entry_visible(visible_text: str, elements: list[dict[str, Any]]) -> bool:
    if _SERVICE_ENTRY_PATTERN.search(visible_text):
        return True
    for item in elements:
        for key in ("text", "content_desc", "aria_label", "name"):
            value = item.get(key)
            if isinstance(value, str) and _SERVICE_ENTRY_PATTERN.search(value):
                return True
    return False


def _service_entry_arrived(action: dict[str, Any]) -> bool:
    if action.get("action") != "click" or not _action_mentions_service(action):
        return False
    if action.get("result") in {"service_page_reached", "customer_service_reached"}:
        return True
    source = action.get("source_capture_id")
    result = action.get("result_capture_id")
    return (
        isinstance(source, str)
        and isinstance(result, str)
        and bool(source)
        and bool(result)
        and source != result
    )


def _action_mentions_service(action: dict[str, Any]) -> bool:
    parts = [
        action.get("semantic_target"),
        action.get("purpose"),
        action.get("selector"),
    ]
    target = action.get("target_before")
    if isinstance(target, dict):
        parts.extend(target.get(key) for key in ("text", "content_desc", "aria_label", "name"))
    return any(isinstance(part, str) and _SERVICE_ENTRY_PATTERN.search(part) for part in parts)


PROGRESS_BAR_STATIC_PAGE_GAP = "progress_bar_seek_and_playback_time_missing"
PROGRESS_BAR_STATIC_PAGE_REASON = (
    "缺实际拖动进度条的过程及播放时间，静态播放页无法判断。"
)


def _media_seek_control_result(
    *,
    tool_name: str,
    action: dict[str, Any],
    facts: dict[str, Any],
    thresholds: dict[str, Any],
) -> dict[str, Any]:
    """确定性比较拖动后页面时间与媒体真值，语义 Judge 只消费结果。"""
    media_state = _mapping(facts.get("media_state"))
    items = [
        dict(item)
        for item in list(media_state.get("items") or [])[:20]
        if isinstance(item, dict)
    ]
    display_candidates = [
        dict(item)
        for item in list(media_state.get("display_candidates") or [])[:30]
        if isinstance(item, dict)
    ]
    media = items[0] if len(items) == 1 else {}
    display = _first_media_time_display(display_candidates)
    current_time = _number(media.get("current_time"))
    duration = _number(media.get("duration"))
    maximum_deviation = _number(
        thresholds.get("max_time_display_deviation_seconds")
    )
    maximum_deviation = maximum_deviation if maximum_deviation is not None else 1.0
    complete = (
        action.get("result") == "media_seek_confirmed"
        and current_time is not None
        and duration is not None
        and display is not None
    )
    if not complete:
        static_page = action.get("result") != "media_seek_confirmed"
        seek_facts: dict[str, Any] = {
            "action": _compact_action(action),
            "media_count": len(items),
            "display_candidate_count": len(display_candidates),
        }
        if static_page:
            seek_facts["gap_codes"] = [PROGRESS_BAR_STATIC_PAGE_GAP]
        return _tool_result(
            tool_name,
            status="insufficient",
            facts=seek_facts,
            reason=(
                PROGRESS_BAR_STATIC_PAGE_REASON
                if static_page
                else "缺少已确认拖动、唯一媒体状态或页面时间展示证据"
            ),
        )
    displayed_current, displayed_duration = display
    current_deviation = abs(displayed_current - current_time)
    duration_deviation = abs(displayed_duration - duration)
    return _tool_result(
        tool_name,
        status="ok",
        facts={
            "action": _compact_action(action),
            "actual_current_time": current_time,
            "actual_duration": duration,
            "displayed_current_time": displayed_current,
            "displayed_duration": displayed_duration,
            "current_time_deviation_seconds": round(current_deviation, 3),
            "duration_deviation_seconds": round(duration_deviation, 3),
            "maximum_deviation_seconds": maximum_deviation,
            "time_display_within_tolerance": (
                current_deviation <= maximum_deviation
                and duration_deviation <= maximum_deviation
            ),
        },
    )


def _first_media_time_display(
    candidates: list[dict[str, Any]],
) -> tuple[float, float] | None:
    matched: list[tuple[float, float]] = []
    for candidate in candidates:
        parsed = [
            _time_text_seconds(item)
            for item in _TIME_TEXT_PATTERN.findall(str(candidate.get("text") or ""))
        ]
        values = [item for item in parsed if item is not None]
        if len(values) >= 2:
            matched.append((values[0], values[1]))
    return matched[0] if len(matched) == 1 else None


def _time_text_seconds(value: tuple[str, str, str]) -> float | None:
    hours, minutes, seconds = value
    try:
        return float((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds))
    except ValueError:
        return None


_TIME_TEXT_PATTERN = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})")


def _distinct_temporal_elements(
    values: list[dict[str, Any]],
    *,
    preserve_geometry_variants: bool = False,
) -> list[dict[str, Any]]:
    """跨帧去重，并按需保留同一素材的几何变化。"""
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in values:
        image = _mapping(item.get("image"))
        rect = _mapping(item.get("rect"))
        style = _mapping(item.get("style"))
        key = (
            str(item.get("selector") or ""),
            str(item.get("tag") or ""),
            str(item.get("src") or ""),
            str(item.get("text") or "")[:120],
            str(image.get("natural_width") or ""),
            str(image.get("natural_height") or ""),
        )
        if preserve_geometry_variants:
            key += (
                str(rect.get("width") or ""),
                str(rect.get("height") or ""),
                str(style.get("object_fit") or ""),
            )
        if key in seen:
            continue
        seen.add(key)
        results.append(item)
    return results


def _ad_region_candidates(
    *,
    packet_payload: dict[str, Any],
    temporal_elements: list[dict[str, Any]],
    visible_text: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """汇总显式广告、广告上下文和大幅横幅素材，供后续视觉语义判定。"""
    page = _mapping(packet_payload.get("page"))
    page_type = str(page.get("page_type") or "").casefold()
    page_state = str(page.get("page_state") or "").casefold()
    context_signal = page_type == "startup_ad" or page_state in {
        "popup",
        "modal",
        "overlay",
        "dialog",
        "countdown",
    }
    promotion_signal = _looks_like_promotional_text(visible_text)
    distinct_elements = _distinct_temporal_elements(temporal_elements)
    media = [item for item in distinct_elements if _is_media_element(item)]
    explicit = [item for item in distinct_elements if _looks_like_ad_element(item)]

    banners: list[dict[str, Any]] = []
    for item in media:
        rect = _mapping(item.get("rect"))
        width = _number(rect.get("width")) or 0.0
        height = _number(rect.get("height")) or 0.0
        area = _element_area(item)
        ratio = width / height if height > 0 else 0.0
        if area >= 60_000 or (area >= 4_000 and ratio >= 1.8):
            banners.append(item)

    selected: list[tuple[dict[str, Any], str]] = [
        (item, "explicit_ad_identity") for item in explicit
    ]
    if context_signal:
        selected.extend(
            (item, "ad_page_context")
            for item in sorted(media, key=_element_area, reverse=True)[:20]
        )
    elif promotion_signal:
        selected.extend(
            (item, "promotional_text_context")
            for item in sorted(media, key=_element_area, reverse=True)[:20]
        )
    else:
        selected.extend(
            (item, "large_banner_media")
            for item in sorted(banners, key=_element_area, reverse=True)[:20]
        )

    regions: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str, str]] = set()
    for item, signal in selected:
        key = (
            str(item.get("selector") or ""),
            str(item.get("src") or ""),
            str(item.get("frame_index") or ""),
        )
        if key in selected_keys:
            continue
        selected_keys.add(key)
        regions.append({**_element_ref(item), "signal": signal})
    if not regions and (context_signal or promotion_signal):
        regions.append(
            {
                "region_type": "page_context",
                "page_type": page_type or None,
                "page_state": page_state or None,
                "signal": (
                    "ad_page_context" if context_signal else "promotional_text_context"
                ),
            }
        )
    signals = {
        "page_type": page_type or None,
        "page_state": page_state or None,
        "ad_page_context": context_signal,
        "promotional_text": promotion_signal,
        "explicit_element_count": len(explicit),
        "media_element_count": len(media),
        "large_banner_count": len(banners),
        "snapshot_complete": _snapshot_is_complete(packet_payload),
    }
    return regions, signals


def _snapshot_is_complete(packet_payload: dict[str, Any]) -> bool:
    snapshots = _snapshot_sequence(packet_payload)
    if not snapshots:
        return False
    for snapshot in snapshots:
        values = snapshot.get("elements")
        if not isinstance(values, list):
            return False
        if snapshot.get("budget_truncated") or snapshot.get("element_limit_reached"):
            return False
        declared = _integer(snapshot.get("element_count"))
        if declared > len(values):
            return False
    return True


def _ad_absence_confirmed(
    *,
    regions: list[dict[str, Any]],
    signals: dict[str, Any],
) -> bool:
    return bool(
        not regions
        and signals.get("snapshot_complete") is True
        and signals.get("ad_page_context") is False
        and signals.get("promotional_text") is False
        and signals.get("media_element_count") == 0
    )


def _cover_content_compare(packet_payload: dict[str, Any]) -> dict[str, Any]:
    """封面与正文或播放帧必须是同一条内容；广告图和只看列表的补采都不成套。"""

    from services.api.app.application.cover_content_pair import assess_cover_content_pair

    cover, content = _cover_content_sides(packet_payload)
    pair = assess_cover_content_pair(cover=cover, content=content)
    gap_codes = list(pair.get("gap_codes") or [])
    reason = {
        "cover_content_ad_material_mixed": "封面或正文材料混入了广告图，不能和内容配成一套",
        "cover_content_list_observation_only": "补采只观察了列表，没有点开同一封面",
        "cover_content_unpaired": "封面与正文或播放画面没有按同一内容配对",
    }.get(gap_codes[0] if gap_codes else "", "封面与正文或播放画面没有按同一内容配对")
    return _tool_result(
        "visual_semantic_compare_tool",
        status="ok" if pair.get("complete") is True else "insufficient",
        facts=pair,
        reason=None if pair.get("complete") is True else reason,
    )


def _cover_content_sides(
    packet_payload: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    facts = _facts(packet_payload)
    materials = facts.get("cover_content_materials")
    if isinstance(materials, dict):
        cover = materials.get("cover")
        content = materials.get("content")
        return (
            dict(cover) if isinstance(cover, dict) else None,
            dict(content) if isinstance(content, dict) else None,
        )
    actions = facts.get("action_log")
    if not isinstance(actions, list):
        return None, None
    for item in reversed(actions):
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or "").strip().casefold()
        if action in {"observe", "observation"}:
            page_type = str(item.get("page_type") or item.get("result_page_type") or "")
            return None, {
                "capture_id": item.get("result_capture_id") or item.get("source_capture_id"),
                "page_type": page_type,
                "action": action,
                "material_kind": (
                    "list"
                    if page_type.casefold() in {"home", "content_list", "video_list", "post_list"}
                    else ""
                ),
                "content_key": item.get("content_key") or item.get("semantic_target"),
                "is_ad": item.get("is_ad"),
                "semantic_target": item.get("semantic_target"),
            }
        if action in {"click", "tap"}:
            return (
                {
                    "capture_id": item.get("source_capture_id"),
                    "page_type": item.get("source_page_type") or "content_list",
                    "content_key": item.get("content_key") or item.get("semantic_target"),
                    "material_kind": item.get("source_material_kind") or "cover",
                    "is_ad": item.get("source_is_ad"),
                    "semantic_target": item.get("semantic_target"),
                },
                {
                    "capture_id": item.get("result_capture_id"),
                    "page_type": item.get("result_page_type") or "",
                    "content_key": item.get("result_content_key")
                    or item.get("content_key")
                    or item.get("semantic_target"),
                    "material_kind": item.get("result_material_kind") or "",
                    "action": action,
                    "is_ad": item.get("result_is_ad"),
                    "semantic_target": item.get("semantic_target"),
                },
            )
    return None, None


def _ad_landing_screenshot_pair(packet_payload: dict[str, Any]) -> dict[str, Any]:
    """从 Packet 的真实截图 Artifact 摘要中解析唯一 source/landing 帧对。"""
    values = _facts(packet_payload).get("screenshot_evidence")
    if not isinstance(values, list):
        return {}
    matches: list[dict[str, Any]] = []
    for item in values:
        if not isinstance(item, dict) or item.get("purpose") != "ad_landing_consistency":
            continue
        frames = item.get("frames")
        if not isinstance(frames, list) or item.get("frame_count") != 2:
            continue
        by_role = {
            str(frame.get("role") or ""): {
                "artifact_ref": str(frame.get("artifact_ref") or ""),
                "logical_ref": str(frame.get("logical_ref") or ""),
                "payload_valid": frame.get("payload_valid") is True,
            }
            for frame in frames
            if isinstance(frame, dict)
        }
        source = by_role.get("source") or {}
        landing = by_role.get("landing") or {}
        if (
            not source.get("artifact_ref")
            or not landing.get("artifact_ref")
            or source.get("payload_valid") is not True
            or landing.get("payload_valid") is not True
        ):
            continue
        matches.append(
            {
                "artifact_id": str(item.get("artifact_id") or ""),
                "source": source["artifact_ref"],
                "landing": landing["artifact_ref"],
                "source_logical_ref": source.get("logical_ref"),
                "landing_logical_ref": landing.get("logical_ref"),
            }
        )
    return matches[0] if len(matches) == 1 else {}


def _ad_landing_logical_pair(
    ad_evidence: dict[str, Any],
    screenshot_pair: dict[str, Any],
) -> bool:
    source = _mapping(ad_evidence.get("source"))
    landing = _mapping(ad_evidence.get("landing"))
    return bool(
        source.get("screenshot_ref") == "ad_landing_consistency#frame-0"
        and landing.get("screenshot_ref") == "ad_landing_consistency#frame-1"
        and source.get("screenshot_ref") == screenshot_pair.get("source_logical_ref")
        and landing.get("screenshot_ref") == screenshot_pair.get("landing_logical_ref")
    )


def _ad_landing_destination_valid(ad_evidence: dict[str, Any]) -> bool:
    landing = _mapping(ad_evidence.get("landing"))
    navigation_mode = str(ad_evidence.get("navigation_mode") or "")
    if navigation_mode in {"same_page", "new_page"}:
        return _url_syntax_fact(str(landing.get("url") or "")).get("syntax_valid") is True
    if navigation_mode != "external_browser":
        return False
    package_name = str(landing.get("package_name") or "").strip().casefold()
    return package_name in {
        "com.android.chrome",
        "com.chrome.beta",
        "com.chrome.canary",
        "com.microsoft.emmx",
        "org.mozilla.firefox",
    } or bool(
        package_name
        and package_name != "com.android.browser.provider"
        and any(marker in package_name for marker in ("browser", "chrome", "firefox"))
    )


def _app_ad_landing_source_region(packet_payload: dict[str, Any]) -> dict[str, Any]:
    action = _last_action(packet_payload)
    evidence = _mapping(action.get("ad_navigation_evidence"))
    source = _mapping(evidence.get("source"))
    if not (
        action.get("action") in {"click", "tap"}
        and evidence.get("status") == "observed"
        and evidence.get("pair_complete") is True
        and evidence.get("navigation_mode") == "external_browser"
        and source.get("capture_id")
        and source.get("artifact_id")
    ):
        return {}
    return {
        "source": "app_controlled_ad_click",
        "capture_id": source.get("capture_id"),
        "artifact_id": source.get("artifact_id"),
        "semantic_target": action.get("semantic_target") or action.get("selector"),
    }


def _tool_result(
    tool_name: str,
    *,
    status: str,
    facts: dict[str, Any],
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "tool_name": tool_name,
        "status": status,
        "facts": facts,
        **({"reason": reason} if reason else {}),
    }


def _visible_ratio(visible: Any, full: Any) -> float | None:
    visible_value = _number(visible)
    full_value = _number(full)
    if visible_value is None or full_value is None or full_value <= 0:
        return None
    return round(min(visible_value / full_value, 1.0), 3)


def _journey(packet_payload: dict[str, Any]) -> list[dict[str, Any]]:
    values = _facts(packet_payload).get("journey")
    if not isinstance(values, list):
        return []
    return [dict(item) for item in values if isinstance(item, dict)]


__all__ = ["run_hybrid_capability_tools"]
