import pytest

from services.api.app.application.app_surface_evidence import project_ui_snapshot_from_hierarchy
from services.api.app.application.observation_packet import (
    _compact_ui_snapshot,
    _structure_activity_mismatches_capture,
)
from services.api.app.application.rule_capabilities import (
    CAPABILITY_HANDLER_NAMES,
    evaluate_rule_engine_handler,
    run_hybrid_capability_tools,
)
from services.api.app.application.rule_capability_facts import compact_action
from services.api.app.application.rule_judge import RULE_ENGINE_HANDLER_REGISTRY


def test_all_implemented_capability_handlers_are_registered_for_worker_startup() -> None:
    assert CAPABILITY_HANDLER_NAMES <= RULE_ENGINE_HANDLER_REGISTRY.keys()


def _native_skip_packet(
    *,
    surface_kind: str = "android",
    source: str | None = None,
    snapshot_surface_kind: str | None = None,
    element_updates: dict | None = None,
    omitted_fields: tuple[str, ...] = (),
) -> dict:
    # 按正式 Accessibility 制品结构脱敏，先经过判定侧真实压缩入口。
    element = {
        "element_index": 8,
        "class_name": "android.view.View",
        "class_tokens": ["android.view.View", "android", "view", "View"],
        "platform_role": "View",
        "content_desc": "跳过",
        "aria_label": "跳过",
        "name": "跳过",
        "clickable": True,
        "enabled": True,
        "rect": {"x": 1153, "y": 182, "width": 229, "height": 135},
        **(element_updates or {}),
    }
    for field in omitted_fields:
        element.pop(field, None)
    snapshot = _compact_ui_snapshot(
        {
            "source": source if source is not None else f"{surface_kind}_ui_hierarchy",
            "surface_kind": snapshot_surface_kind or surface_kind,
            "package_name": "com.example.app",
            "activity": "com.example.MainActivity",
            "element_count": 1,
            "elements": [element],
        }
    )
    return {
        "page": {"surface_kind": surface_kind, "page_type": "startup_ad"},
        "facts": {"ui_snapshot": snapshot},
    }


@pytest.mark.parametrize("surface_kind", ["android", "ios"])
async def test_clickable_area_accepts_compacted_native_skip_control(surface_kind: str) -> None:
    packet = _native_skip_packet(surface_kind=surface_kind)
    element = packet["facts"]["ui_snapshot"]["elements"][0]

    assert element["clickable"] is True
    assert element["enabled"] is True
    assert "tag" not in element and "role" not in element and "hit_test" not in element
    result = await evaluate_rule_engine_handler(
        handler="clickable_area_handler",
        packet_payload=packet,
        engine_config={"target_keywords": ["跳过", "skip"], "min_width": 32, "min_height": 32},
    )

    assert result.conclusion == "pass"
    assert result.facts["elements"][0]["rect"]["width"] == 229


@pytest.mark.parametrize(
    ("element_updates", "omitted_fields", "expected"),
    [
        ({"rect": {"width": 20, "height": 135}}, (), "fail"),
        ({"rect": {}}, (), "fail"),
        ({"hit_test": {"center_hits_target": False}}, (), "fail"),
        ({"enabled": False}, (), "evidence_insufficient"),
        ({"enabled": False, "tag": "button"}, (), "evidence_insufficient"),
        ({"enabled": "false"}, (), "evidence_insufficient"),
        ({"enabled": "true"}, (), "evidence_insufficient"),
        ({"enabled": None}, (), "evidence_insufficient"),
        ({}, ("enabled",), "evidence_insufficient"),
        ({"clickable": False}, (), "evidence_insufficient"),
        ({"clickable": False, "role": "button"}, (), "evidence_insufficient"),
        ({"clickable": "true"}, (), "evidence_insufficient"),
        ({}, ("clickable",), "evidence_insufficient"),
        ({"aria_label": "普通文字", "name": "普通文字"}, (), "evidence_insufficient"),
    ],
)
async def test_clickable_area_keeps_native_defects_and_missing_facts(
    element_updates: dict,
    omitted_fields: tuple[str, ...],
    expected: str,
) -> None:
    result = await evaluate_rule_engine_handler(
        handler="clickable_area_handler",
        packet_payload=_native_skip_packet(
            element_updates=element_updates,
            omitted_fields=omitted_fields,
        ),
        engine_config={"target_keywords": ["跳过", "skip"], "min_width": 32, "min_height": 32},
    )

    assert result.conclusion == expected


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": ""},
        {"source": "visual_model"},
        {"source": "visual_model", "element_updates": {"role": "button"}},
        {"snapshot_surface_kind": "ios"},
        {"snapshot_surface_kind": "ios", "element_updates": {"tag": "button"}},
        {
            "surface_kind": "web",
            "source": "android_ui_hierarchy",
            "snapshot_surface_kind": "android",
        },
    ],
)
async def test_clickable_area_does_not_infer_native_control_without_source_identity(
    overrides: dict,
) -> None:
    result = await evaluate_rule_engine_handler(
        handler="clickable_area_handler",
        packet_payload=_native_skip_packet(**overrides),
        engine_config={"target_keywords": ["跳过"]},
    )

    assert result.conclusion == "evidence_insufficient"


async def test_clickable_area_preserves_web_button_matching() -> None:
    packet = _native_skip_packet(
        surface_kind="web",
        source="browser",
        element_updates={"tag": "button"},
        omitted_fields=("clickable", "enabled"),
    )
    result = await evaluate_rule_engine_handler(
        handler="clickable_area_handler",
        packet_payload=packet,
        engine_config={"target_keywords": ["跳过"]},
    )

    assert result.conclusion == "pass"


@pytest.mark.parametrize("surface_kind", ["web", "h5"])
def test_compaction_preserves_visibility_without_inventing_native_source(
    surface_kind: str,
) -> None:
    native = _native_skip_packet(element_updates={"visible": False})
    browser = _native_skip_packet(
        surface_kind=surface_kind,
        source="browser",
        element_updates={"visible": False},
    )
    native_element = native["facts"]["ui_snapshot"]["elements"][0]
    browser_element = browser["facts"]["ui_snapshot"]["elements"][0]

    assert {key: native_element[key] for key in ("clickable", "enabled", "visible")} == {
        "clickable": True,
        "enabled": True,
        "visible": False,
    }
    assert browser["facts"]["ui_snapshot"]["source"] == "browser"
    assert browser_element["visible"] is False
    assert all(key not in browser_element for key in ("clickable", "enabled"))
    assert {key: value for key, value in native_element.items()
            if key not in {"clickable", "enabled"}} == browser_element


async def test_clickable_area_rejects_invisible_native_control_after_projection() -> None:
    snapshot = project_ui_snapshot_from_hierarchy(
        '<hierarchy><node class="android.view.View" content-desc="跳过" '
        'clickable="true" enabled="true" visible-to-user="false" '
        'bounds="[1153,182][1382,317]" package="com.example.app" /></hierarchy>',
        package_name="com.example.app",
        activity="com.example.MainActivity",
    )
    compacted = _compact_ui_snapshot(snapshot)
    assert compacted["elements"][0]["visible"] is False
    result = await evaluate_rule_engine_handler(
        handler="clickable_area_handler",
        packet_payload={
            "page": {"surface_kind": "android"},
            "facts": {"ui_snapshot": compacted},
        },
        engine_config={"target_keywords": ["跳过"]},
    )

    assert result.conclusion == "evidence_insufficient"


def _packet(
    *,
    text: str = "",
    elements: list[dict] | None = None,
    action_log: list[dict] | None = None,
    page_type: str | None = None,
    journey: list[dict] | None = None,
    ui_snapshot: dict | None = None,
    media_state: dict | None = None,
    content_attribution: dict | None = None,
) -> dict:
    return {
        "page": {"page_type": page_type},
        "facts": {
            "visible_text": text,
            "ui_snapshot": ui_snapshot
            or {
                "schema_version": "ui-snapshot.v1",
                "elements": elements or [],
            },
            "action_log": action_log or [],
            "journey": journey or [],
            "media_state": media_state or {},
            "content_attribution": content_attribution or {},
        },
    }


def test_app_media_and_attributed_ugc_are_available_to_hybrid_tools() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(
            text="作者甲发布的帖子正文",
            elements=[
                {
                    "role": "image",
                    "selector": "resource-id:cover",
                    "image": {
                        "kind": "image",
                        "source": "android_accessibility_class",
                    },
                }
            ],
            content_attribution={
                "scope": "ugc",
                "basis": ["page_type:post_list"],
                "bylines": [{"author_text": "作者甲"}],
            },
        ),
        capability_config={
            "deterministic_tools": [
                "media_element_extractor_tool",
                "ugc_content_extractor_tool",
            ]
        },
    )

    media, ugc = results
    assert media["status"] == "ok"
    assert media["facts"]["media_elements"][0]["element"]["role"] == "image"
    assert ugc["status"] == "ok"
    assert ugc["facts"]["ugc_candidates"][0]["source"] == ("attributed_visible_text")
    assert ugc["facts"]["content_attribution"]["scope"] == "ugc"


def test_media_seek_control_compares_display_time_with_media_truth() -> None:
    packet = _packet(
        action_log=[
            {
                "action": "seek_media",
                "result": "media_seek_confirmed",
                "target_fraction": 0.5,
                "media_before": {"current_time": 10, "duration": 120},
                "media_after": {"current_time": 60.4, "duration": 120},
            }
        ],
        media_state={
            "schema_version": "media-state.v1",
            "items": [
                {
                    "media_index": 0,
                    "current_time": 60.4,
                    "duration": 120,
                    "seeking": False,
                }
            ],
            "display_candidates": [{"selector": "#clock", "text": "01:00 / 02:00"}],
        },
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "deterministic_tools": [
                "media_element_extractor_tool",
                "control_behavior_assertion_tool",
            ]
        },
        thresholds={"max_time_display_deviation_seconds": 1},
    )

    media, control = result
    assert media["status"] == "ok"
    assert media["facts"]["media_state_items"][0]["current_time"] == 60.4
    assert control["status"] == "ok"
    assert control["facts"]["current_time_deviation_seconds"] == 0.4
    assert control["facts"]["time_display_within_tolerance"] is True


def test_media_seek_control_marks_large_time_deviation_for_judge() -> None:
    packet = _packet(
        action_log=[{"action": "seek_media", "result": "media_seek_confirmed"}],
        media_state={
            "items": [{"current_time": 64, "duration": 120}],
            "display_candidates": [{"text": "01:00 / 02:00"}],
        },
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={"deterministic_tools": ["control_behavior_assertion_tool"]},
        thresholds={"max_time_display_deviation_seconds": 1},
    )[0]

    assert result["status"] == "ok"
    assert result["facts"]["time_display_within_tolerance"] is False


def test_media_seek_control_rejects_ambiguous_time_displays() -> None:
    packet = _packet(
        action_log=[{"action": "seek_media", "result": "media_seek_confirmed"}],
        media_state={
            "items": [{"current_time": 60, "duration": 120}],
            "display_candidates": [
                {"text": "01:00 / 02:00"},
                {"text": "00:05 / 00:30"},
            ],
        },
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={"deterministic_tools": ["control_behavior_assertion_tool"]},
        thresholds={"max_time_display_deviation_seconds": 1},
    )[0]

    assert result["status"] == "insufficient"
    assert result["facts"]["display_candidate_count"] == 2


def test_unlinked_payment_entry_is_not_judged() -> None:
    packet = _packet(page_type="payment", action_log=[{"action": "observe"}])
    packet["page"]["surface_kind"] = "android"

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "payment-entry",
            "deterministic_tools": ["interaction_state_diff_tool"],
        },
    )[0]

    assert result["status"] == "insufficient"
    assert result["facts"]["gap_codes"] == ["payment_entry_chain_missing"]
    assert result["reason"] == "充值入口、充值页和选档结果未按实际操作串联，缺对应画面。"


def test_linked_payment_entry_and_tier_can_still_be_judged() -> None:
    packet = _packet(
        page_type="payment",
        action_log=[
            {
                "action": "click",
                "semantic_target": "充值",
                "source_capture_id": "profile",
                "result_capture_id": "payment",
                "state_diff": {"changed_fields": ["page"]},
            },
            {
                "action": "click",
                "semantic_target": "月卡",
                "source_capture_id": "payment",
                "result_capture_id": "tier",
                "state_diff": {"changed_fields": ["price"]},
            },
        ],
    )
    packet["page"]["surface_kind"] = "android"

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "payment-entry",
            "deterministic_tools": ["interaction_state_diff_tool"],
        },
    )[0]

    assert result["status"] == "ok"
    assert "gap_codes" not in result["facts"]


def test_native_player_without_timing_or_switch_is_not_judged() -> None:
    packet = _packet(
        page_type="video_detail",
        action_log=[{"action": "observe"}],
        media_state={
            "source": "android_accessibility_class",
            "items": [{"kind": "video", "source": "android_accessibility_class"}],
        },
    )
    packet["page"]["surface_kind"] = "android"

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "player-playback",
            "deterministic_tools": [
                "media_element_extractor_tool",
                "multi_frame_capture_tool",
            ],
        },
    )[0]

    assert result["status"] == "insufficient"
    assert result["facts"]["gap_codes"] == ["native_playback_switch_missing"]
    assert result["reason"] == "原生媒体未提供播放时间与进度，缺实际起播及上下条切换的证据。"


def test_native_player_with_timing_can_still_be_judged() -> None:
    packet = _packet(
        page_type="video_detail",
        media_state={"items": [{"current_time": 3, "duration": 20, "kind": "video"}]},
    )
    packet["page"]["surface_kind"] = "android"

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "player-playback",
            "deterministic_tools": ["media_element_extractor_tool"],
        },
    )[0]

    assert result["status"] == "ok"
    assert result["facts"]["media_state_items"][0]["current_time"] == 3


def test_recharge_spacing_stops_when_screenshot_and_structure_differ() -> None:
    packet = _packet(
        page_type="payment",
        elements=[
            {
                "tag": "button",
                "text": "微信支付",
                "rect": {"x": 10, "y": 10, "width": 40, "height": 20},
            }
        ],
    )
    packet["facts"]["structure_screen_mismatch"] = True

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "visual-layout",
            "deterministic_tools": ["layout_geometry_assertion_handler"],
            "thresholds": {"max_overlap_pairs": 0},
        },
    )[0]

    assert result["status"] == "insufficient"
    assert result["facts"]["gap_codes"] == ["recharge_layout_screen_mismatch"]
    assert result["reason"] == "充值页截图与结构信息不属于同一界面，无法检查控件间距。"


def test_structure_activity_mismatch_compares_capture_and_tree() -> None:
    assert _structure_activity_mismatches_capture(
        "com.app.MainActivity",
        "com.app.PaymentActivity",
    )
    assert not _structure_activity_mismatches_capture(
        "com.app.PaymentActivity",
        "com.app.PaymentActivity",
    )
    assert not _structure_activity_mismatches_capture("", "com.app.PaymentActivity")


def test_visible_service_entry_without_arrival_is_not_judged() -> None:
    packet = _packet(
        text="在线客服",
        elements=[{"text": "在线客服", "clickable": True}],
        action_log=[{"action": "observe"}],
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "service-entry",
            "deterministic_tools": ["control_behavior_assertion_tool"],
        },
    )[0]

    assert result["status"] == "insufficient"
    assert result["facts"]["gap_codes"] == ["service_entry_arrival_missing"]
    assert result["reason"] == "客服入口已看见，尚未点击并采集实际到达的客服页面。"


def test_service_entry_click_with_result_capture_can_be_judged() -> None:
    packet = _packet(
        text="在线客服",
        action_log=[
            {
                "action": "click",
                "semantic_target": "在线客服",
                "source_capture_id": "profile",
                "result_capture_id": "service-page",
            }
        ],
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "service-entry",
            "deterministic_tools": ["control_behavior_assertion_tool"],
        },
    )[0]

    assert result["status"] == "ok"
    assert result["facts"]["service_page_reached"] is True


def test_static_playback_page_cannot_judge_progress_time() -> None:
    packet = _packet(
        action_log=[{"action": "observe"}],
        media_state={"items": [{"kind": "video"}]},
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "batch_group": "player-controls",
            "deterministic_tools": ["control_behavior_assertion_tool"],
        },
    )[0]

    assert result["status"] == "insufficient"
    assert result["facts"]["gap_codes"] == ["progress_bar_seek_and_playback_time_missing"]
    assert result["reason"] == "缺实际拖动进度条的过程及播放时间，静态播放页无法判断。"


async def test_technical_error_handler_reads_packet_visible_text() -> None:
    result = await evaluate_rule_engine_handler(
        handler="technical_error_text",
        packet_payload=_packet(text="Internal Server Error"),
        engine_config={"pattern": "Internal Server Error"},
    )

    assert result.conclusion == "fail"
    assert result.facts["match"] == "Internal Server Error"


async def test_visible_ad_presence_handler_passes_on_ad_snapshot_element() -> None:
    result = await evaluate_rule_engine_handler(
        handler="visible_ad_presence_handler",
        packet_payload=_packet(
            page_type="video_detail",
            elements=[
                {
                    "selector": "#pre-roll-ad",
                    "tag": "img",
                    "src": "https://cdn.example/ad-banner.png",
                }
            ],
        ),
        engine_config={},
    )

    assert result.conclusion == "pass"
    assert result.facts["ad_elements"][0]["selector"] == "#pre-roll-ad"


async def test_visible_ad_presence_handler_keeps_absence_as_insufficient() -> None:
    result = await evaluate_rule_engine_handler(
        handler="visible_ad_presence_handler",
        packet_payload=_packet(
            page_type="video_detail",
            elements=[{"selector": "#player", "tag": "video"}],
        ),
        engine_config={},
    )

    assert result.conclusion == "evidence_insufficient"


async def test_visible_ad_presence_handler_accepts_closeable_image_modal() -> None:
    result = await evaluate_rule_engine_handler(
        handler="visible_ad_presence_handler",
        packet_payload=_packet(
            page_type="video_detail",
            elements=[
                {
                    "selector": "[role=dialog]",
                    "role": "dialog",
                    "text": "关闭",
                    "rect": {"x": 0, "y": 180, "width": 390, "height": 390},
                },
                {
                    "selector": "[role=dialog] img",
                    "tag": "img",
                    "src": "https://cdn.example/creative.gif",
                    "rect": {"x": 58, "y": 180, "width": 273, "height": 340},
                },
                {
                    "selector": "[role=dialog] .close",
                    "tag": "button",
                    "text": "关闭",
                    "rect": {"x": 280, "y": 520, "width": 40, "height": 40},
                },
            ],
        ),
        engine_config={},
    )

    assert result.conclusion == "pass"
    assert result.facts["closeable_media_modal"]["media"]["tag"] == "img"


async def test_page_url_status_handler_passes_on_http_200() -> None:
    calls: list[dict[str, object]] = []

    async def validator(*, url: str, timeout_seconds: float, max_redirects: int) -> dict:
        calls.append(
            {
                "url": url,
                "timeout_seconds": timeout_seconds,
                "max_redirects": max_redirects,
            }
        )
        return {
            "url_checks": [{"status_code": 200, "input_url": url}],
            "issues": [],
            "is_valid": True,
        }

    packet = _packet()
    packet["page"]["url"] = "https://example.com/activity"
    result = await evaluate_rule_engine_handler(
        handler="page_url_status_handler",
        packet_payload=packet,
        engine_config={
            "allowed_status_codes": [200],
            "timeout_seconds": 3.5,
            "max_redirects": 2,
        },
        qr_validator=validator,
    )

    assert result.conclusion == "pass"
    assert result.facts["status_code"] == 200
    assert calls == [
        {
            "url": "https://example.com/activity",
            "timeout_seconds": 3.5,
            "max_redirects": 2,
        }
    ]


async def test_page_url_status_handler_fails_on_non_200_status() -> None:
    async def validator(*, url: str, timeout_seconds: float, max_redirects: int) -> dict:
        del timeout_seconds, max_redirects
        return {
            "url_checks": [{"status_code": 503, "input_url": url}],
            "issues": ["http_status:503"],
            "is_valid": False,
        }

    packet = _packet()
    packet["page"]["url"] = "https://example.com/activity"
    result = await evaluate_rule_engine_handler(
        handler="page_url_status_handler",
        packet_payload=packet,
        engine_config={"allowed_status_codes": [200]},
        qr_validator=validator,
    )

    assert result.conclusion == "fail"
    assert result.facts["status_code"] == 503


async def test_native_app_location_does_not_require_http_status() -> None:
    async def validator(*, url: str, timeout_seconds: float, max_redirects: int) -> dict:
        del url, timeout_seconds, max_redirects
        raise AssertionError("app:// 不是 HTTP 地址，不能探测状态码")

    packet = _packet()
    packet["page"]["url"] = "app://com.example.app/com.example.Activity"
    result = await evaluate_rule_engine_handler(
        handler="page_url_status_handler",
        packet_payload=packet,
        engine_config={"allowed_status_codes": [200]},
        qr_validator=validator,
    )

    assert result.conclusion == "evidence_insufficient"
    assert result.facts["http_status_required"] is False
    assert result.facts["gap_codes"] == ["native_activity_sync_facts_missing"]
    assert "不要求 HTTP 状态" in (result.problem_desc or "")


async def test_native_expired_activity_still_displayed_fails() -> None:
    packet = _packet()
    packet["page"]["url"] = "app://com.example.app/com.example.Activity"
    packet["facts"]["activity"] = {
        "activity_id": "summer-sale",
        "expired": True,
        "displayed": True,
    }
    result = await evaluate_rule_engine_handler(
        handler="page_url_status_handler",
        packet_payload=packet,
        engine_config={"allowed_status_codes": [200]},
    )

    assert result.conclusion == "fail"
    assert result.facts["http_status_required"] is False
    assert "仍在展示" in result.summary


async def test_native_expired_activity_hidden_passes_without_http_status() -> None:
    packet = _packet()
    packet["page"]["url"] = "app://com.example.app/com.example.Activity"
    packet["facts"]["activity"] = {
        "name": "暑期活动",
        "status": "已结束",
        "still_shown": False,
    }
    result = await evaluate_rule_engine_handler(
        handler="page_url_status_handler",
        packet_payload=packet,
        engine_config={"allowed_status_codes": [200]},
    )

    assert result.conclusion == "pass"
    assert result.facts["expired"] is True
    assert result.facts["displayed"] is False


async def test_startup_ad_home_transition_handler_passes_on_journey() -> None:
    result = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler",
        packet_payload=_packet(
            page_type="home",
            journey=[
                {"page_type": "startup_ad"},
                {"page_type": "home"},
            ],
        ),
        engine_config={},
    )

    assert result.conclusion == "pass"
    assert result.facts["page_types"] == ["startup_ad", "home"]


async def test_startup_ad_home_transition_handler_requires_later_page() -> None:
    result = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler",
        packet_payload=_packet(
            page_type="startup_ad",
            journey=[{"page_type": "startup_ad"}],
        ),
        engine_config={},
    )

    assert result.conclusion == "evidence_insufficient"
    assert result.facts["missing_capture_roles"] == ["after"]
    assert "applicability" not in result.facts
    assert "等待后续首页采集" in (result.problem_desc or "")


async def test_startup_ad_transition_uses_trusted_before_when_journey_is_compacted() -> None:
    packet = _packet(page_type="home", journey=[{"page_type": "home"}])
    packet["page"]["page_capture_id"] = "home-capture"
    packet["facts"]["capture_roles"] = {
        "before": {
            "page_capture_id": "startup-capture",
            "page_type": "startup_ad",
            "quality_status": "pass",
        },
    }
    result = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler", packet_payload=packet, engine_config={},
    )
    assert result.conclusion == "pass"
    assert result.facts["page_types"] == ["startup_ad", "home"]


async def test_startup_ad_home_transition_handler_skips_direct_home() -> None:
    result = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler",
        packet_payload=_packet(
            page_type="home",
            journey=[{"page_type": "home"}],
        ),
        engine_config={},
    )

    assert result.conclusion == "skipped"
    assert "未出现启动广告页" in (result.problem_desc or "")


async def test_startup_ad_home_transition_handler_fails_on_other_later_page() -> None:
    result = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler",
        packet_payload=_packet(
            page_type="content_list",
            journey=[
                {"page_type": "startup_ad"},
                {"page_type": "content_list"},
            ],
        ),
        engine_config={},
    )

    assert result.conclusion == "fail"


async def test_later_popup_is_not_the_startup_ad_skip_button() -> None:
    packet = _packet(
        page_type="home",
        journey=[{"page_type": "startup_ad"}, {"page_type": "home"}],
    )
    packet["page"]["page_state"] = "popup"
    result = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler",
        packet_payload=packet,
        engine_config={},
    )
    assert result.conclusion == "evidence_insufficient"
    assert result.facts["gap_codes"] == [
        "startup_ad_skip_button_missing",
        "startup_ad_skip_landing_missing",
    ]


async def test_startup_skip_button_and_landing_can_complete_the_set() -> None:
    packet = _packet(
        page_type="home",
        journey=[{"page_type": "startup_ad"}, {"page_type": "home"}],
        action_log=[
            {
                "action": "click",
                "semantic_target": "跳过",
                "source_page_type": "startup_ad",
                "source_capture_id": "startup-skip",
                "result_capture_id": "home-after-skip",
                "result_page_type": "home",
            }
        ],
    )
    packet["page"]["page_state"] = "popup"
    result = await evaluate_rule_engine_handler(
        handler="startup_ad_home_transition_handler",
        packet_payload=packet,
        engine_config={},
    )
    assert result.conclusion == "pass"


async def test_popup_close_handler_uses_action_and_current_modal_state() -> None:
    result = await evaluate_rule_engine_handler(
        handler="popup_close_behavior_handler",
        packet_payload=_packet(
            elements=[{"selector": "#home", "tag": "main", "text": "首页"}],
            action_log=[
                {
                    "action": "click",
                    "selector": "[role=dialog] .close",
                    "target_before": {
                        "selector": "[role=dialog] .close",
                        "tag": "button",
                        "text": "关闭广告",
                    },
                    "state_diff": {"changed_fields": ["text"]},
                }
            ],
        ),
        engine_config={},
    )

    assert result.conclusion == "pass"


async def test_profile_without_logout_does_not_finish_the_search() -> None:
    result = await evaluate_rule_engine_handler(
        handler="logout_entry_presence_handler",
        packet_payload=_packet(
            text="个人中心 我的订单 设置",
            page_type="profile",
            elements=[{"text": "设置"}],
        ),
        engine_config={"target_keywords": ["退出登录"]},
    )

    assert result.conclusion == "evidence_insufficient"
    assert result.problem_desc == "只查个人中心就判无退出入口，尚未进入设置或账号页完成搜索。"
    assert result.facts["gap_codes"] == ["logout_entry_search_incomplete"]


async def test_logout_presence_handler_fails_without_visible_control() -> None:
    result = await evaluate_rule_engine_handler(
        handler="logout_entry_presence_handler",
        packet_payload=_packet(
            text="个人资料",
            page_type="settings",
            elements=[{"selector": "#profile", "tag": "main", "text": "个人资料"}],
        ),
        engine_config={"target_keywords": ["退出登录"]},
    )

    assert result.conclusion == "fail"
    assert result.confidence == 1.0


async def test_logout_presence_handler_accepts_visible_text_naming_the_exit() -> None:
    """结构里没定位到控件、但页面文字里写着「退出登录」：入口存在（截图读字、画布页面都只有文字）。"""
    result = await evaluate_rule_engine_handler(
        handler="logout_entry_presence_handler",
        packet_payload=_packet(
            text="账号安全 登录账号 lichuan 其他 用户使用手册 退出登录",
            page_type="settings",
            elements=[],
        ),
        engine_config={"target_keywords": ["退出登录"]},
    )

    assert result.conclusion == "pass"
    assert result.facts["source"] == "visible_text"
    assert result.facts["matched_text"] == ["退出登录"]


async def test_external_navigation_requires_clicked_target_proven_external() -> None:
    result = await evaluate_rule_engine_handler(
        handler="external_navigation_handler",
        packet_payload=_packet(
            action_log=[
                {
                    "action": "click",
                    "selector": "#external",
                    "target_before": {
                        "selector": "#external",
                        "tag": "a",
                        "external": True,
                        "href": "https://external.example/path",
                    },
                    "before_url": "https://example.com/home",
                    "after_url": "https://example.com/home",
                    "new_page_urls": ["https://external.example/path"],
                }
            ]
        ),
        engine_config={},
    )

    assert result.conclusion == "pass"


async def test_external_navigation_does_not_pass_unproven_click() -> None:
    result = await evaluate_rule_engine_handler(
        handler="external_navigation_handler",
        packet_payload=_packet(
            action_log=[
                {
                    "action": "click",
                    "selector": "#unknown",
                    "before_url": "https://example.com/home",
                    "after_url": "https://example.com/home",
                    "new_page_urls": ["https://external.example/path"],
                }
            ]
        ),
        engine_config={},
    )

    assert result.conclusion == "evidence_insufficient"


def _safe_click_action() -> dict:
    return {
        "action": "click",
        "selector": "#external",
        "action_execution_id": "action-1",
        "source_capture_id": "capture-before",
        "result_capture_id": "capture-after",
        "target_before": {
            "selector": "#external",
            "tag": "a",
            "external": True,
        },
        "target_after": {
            "selector": "#external",
            "tag": "a",
            "external": True,
        },
        "before_url": "https://example.com/home",
        "after_url": "https://example.com/home",
        "new_page_urls": ["https://external.example/path"],
        "execution_outcome": "succeeded",
        "verification_outcome": "unverified",
    }


def test_compact_action_keeps_click_identity_and_fault_result_fields() -> None:
    compacted = compact_action(
        {
            **_safe_click_action(),
            "result": "network_fault_injected",
            "matched_request_count": 2,
            "route_restored": True,
            "untrusted_raw_page_dump": "secret",
        }
    )

    assert compacted["action_execution_id"] == "action-1"
    assert compacted["source_capture_id"] == "capture-before"
    assert compacted["result_capture_id"] == "capture-after"
    assert compacted["target_before"]["selector"] == "#external"
    assert compacted["target_after"]["selector"] == "#external"
    assert compacted["result"] == "network_fault_injected"
    assert compacted["matched_request_count"] == 2
    assert compacted["route_restored"] is True
    assert "untrusted_raw_page_dump" not in compacted


def test_safe_click_tool_requires_same_action_before_after_identity() -> None:
    action = _safe_click_action()
    result = run_hybrid_capability_tools(
        packet_payload=_packet(action_log=[action]),
        capability_config={"deterministic_tools": ["safe_click_observer_tool"]},
    )[0]

    assert result["status"] == "ok"
    assert result["facts"]["action"]["action_execution_id"] == "action-1"
    assert result["facts"]["action"]["source_capture_id"] == "capture-before"
    assert result["facts"]["action"]["result_capture_id"] == "capture-after"

    for field in ("action_execution_id", "source_capture_id", "result_capture_id"):
        incomplete = _safe_click_action()
        incomplete.pop(field)
        result = run_hybrid_capability_tools(
            packet_payload=_packet(action_log=[incomplete]),
            capability_config={"deterministic_tools": ["safe_click_observer_tool"]},
        )[0]
        assert result["status"] == "insufficient"
        assert field in result["reason"]


def test_safe_click_tool_requires_after_result_and_rejects_failed_action() -> None:
    incomplete = _safe_click_action()
    incomplete.pop("after_url")
    incomplete.pop("new_page_urls")
    incomplete.pop("target_after")
    incomplete.pop("state_diff", None)
    result = run_hybrid_capability_tools(
        packet_payload=_packet(action_log=[incomplete]),
        capability_config={"deterministic_tools": ["safe_click_observer_tool"]},
    )[0]
    assert result["status"] == "insufficient"
    assert "after URL" in result["reason"]

    failed = _safe_click_action()
    failed["result"] = "failed"
    result = run_hybrid_capability_tools(
        packet_payload=_packet(action_log=[failed]),
        capability_config={"deterministic_tools": ["safe_click_observer_tool"]},
    )[0]
    assert result["status"] == "insufficient"
    assert "执行或验证失败" in result["reason"]

    for field, value in (
        ("execution_outcome", "rejected"),
        ("verification_outcome", "pending"),
        ("status", "timed_out"),
    ):
        incomplete = _safe_click_action()
        incomplete[field] = value
        result = run_hybrid_capability_tools(
            packet_payload=_packet(action_log=[incomplete]),
            capability_config={"deterministic_tools": ["safe_click_observer_tool"]},
        )[0]
        assert result["status"] == "insufficient"
        assert "执行或验证失败" in result["reason"]


def _network_fault_action() -> dict:
    return {
        "action": "network_fault",
        "result": "network_fault_injected",
        "matched_request_count": 1,
        "matched_request_urls": ["https://ads.example.test/pre-roll.mp4"],
        "route_restored": True,
    }


def test_network_fault_tools_require_injection_result_hit_and_cleanup() -> None:
    for tool_name in ("network_fault_injection_tool", "fallback_state_assertion_tool"):
        result = run_hybrid_capability_tools(
            packet_payload=_packet(
                text="加载失败，请重试",
                action_log=[_network_fault_action()],
            ),
            capability_config={"deterministic_tools": [tool_name]},
        )[0]
        assert result["status"] == "ok"
        assert result["facts"]["action"]["result"] == "network_fault_injected"
        assert result["facts"]["action"]["matched_request_count"] == 1
        assert result["facts"]["action"]["route_restored"] is True

        for field, value in (
            ("result", "network_fault"),
            ("matched_request_count", 0),
            ("route_restored", False),
        ):
            incomplete = _network_fault_action()
            incomplete[field] = value
            result = run_hybrid_capability_tools(
                packet_payload=_packet(text="加载失败", action_log=[incomplete]),
                capability_config={"deterministic_tools": [tool_name]},
            )[0]
            assert result["status"] == "insufficient"


def test_fallback_assertion_requires_post_fault_visible_result() -> None:
    result = run_hybrid_capability_tools(
        packet_payload=_packet(action_log=[_network_fault_action()]),
        capability_config={"deterministic_tools": ["fallback_state_assertion_tool"]},
    )[0]

    assert result["status"] == "insufficient"
    assert "未观察到降级提示" in result["reason"]


def test_hybrid_typography_tool_returns_deterministic_facts() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(
            text="测试！",
            elements=[
                {
                    "selector": "#copy",
                    "tag": "p",
                    "text": "测试！",
                    "style": {"font_family": "SimSun, serif"},
                }
            ],
        ),
        capability_config={"deterministic_tools": ["typography_assertion_handler"]},
    )

    assert results[0]["status"] == "ok"
    assert results[0]["facts"]["serif_elements"][0]["selector"] == "#copy"


def test_hybrid_state_tools_use_real_action_diff() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(
            action_log=[
                {
                    "action": "click",
                    "selector": "#favorite",
                    "before_url": "https://example.com/detail",
                    "after_url": "https://example.com/detail",
                    "state_diff": {
                        "changed_fields": ["text", "checked"],
                        "change_count": 1,
                    },
                }
            ]
        ),
        capability_config={
            "deterministic_tools": [
                "state_snapshot_tool",
                "interaction_state_diff_tool",
            ]
        },
    )

    assert [item["status"] for item in results] == ["ok", "ok"]
    assert results[1]["facts"]["state_diff"]["change_count"] == 1


def test_transient_message_tool_prefers_action_bound_style_facts() -> None:
    result = run_hybrid_capability_tools(
        packet_payload=_packet(
            action_log=[
                {
                    "action": "click",
                    "transient_message_observation": {
                        "status": "observed",
                        "message_count": 1,
                    },
                    "transient_messages": [
                        {
                            "event_id": "transient-1",
                            "kind": "toast",
                            "text": "保存成功",
                            "position_zone": "top_center",
                            "style": {
                                "font_size": "16px",
                                "text_color": "rgb(255, 255, 255)",
                                "background_color": "rgba(0, 0, 0, 0.72)",
                                "background_opacity": 0.72,
                            },
                        }
                    ],
                }
            ]
        ),
        capability_config={"deterministic_tools": ["transient_message_capture_tool"]},
    )[0]

    assert result["status"] == "ok"
    assert "legacy_snapshot_fallback" not in result["facts"]
    assert result["facts"]["messages"][0]["style"]["font_size"] == "16px"
    assert result["facts"]["absence_confirmed"] is False


def test_transient_message_tool_distinguishes_observed_absence_from_missing_monitor() -> None:
    observed = run_hybrid_capability_tools(
        packet_payload=_packet(
            action_log=[
                {
                    "action": "click",
                    "transient_message_observation": {
                        "status": "observed",
                        "message_count": 0,
                    },
                }
            ]
        ),
        capability_config={"deterministic_tools": ["transient_message_capture_tool"]},
    )[0]
    missing = run_hybrid_capability_tools(
        packet_payload=_packet(),
        capability_config={"deterministic_tools": ["transient_message_capture_tool"]},
    )[0]

    assert observed["status"] == "ok"
    assert observed["facts"]["absence_confirmed"] is True
    assert missing["status"] == "insufficient"


def test_hybrid_url_reputation_does_not_claim_external_safety() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(
            elements=[
                {
                    "selector": "#ugc-link",
                    "tag": "a",
                    "href": "https://example.com/path",
                    "text": "用户链接",
                    "class_tokens": ["comment-link"],
                }
            ]
        ),
        capability_config={
            "deterministic_tools": [
                "ugc_link_extractor_tool",
                "safe_url_reputation_tool",
            ]
        },
    )

    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "insufficient"
    assert "信誉数据源" in results[1]["reason"]


def test_hybrid_cross_page_fact_store_uses_trusted_journey() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(
            journey=[
                {
                    "page_capture_id": "list-1",
                    "page_type": "content_list",
                    "url": "https://example.com/list",
                    "visible_text_excerpt": "优惠价 8.8 原价 10",
                    "ui_snapshot": {"elements": []},
                },
                {
                    "page_capture_id": "detail-1",
                    "page_type": "content_detail",
                    "url": "https://example.com/detail",
                    "visible_text_excerpt": "现价 8.8 划线价 10",
                    "ui_snapshot": {"elements": []},
                },
            ]
        ),
        capability_config={"deterministic_tools": ["cross_page_fact_store"]},
    )

    assert results[0]["status"] == "ok"
    assert len(results[0]["facts"]["price_pages"]) == 2


def test_ad_and_geometry_tools_scan_all_timed_capture_frames() -> None:
    packet = _packet(
        text="8s后关闭",
        page_type="startup_ad",
        ui_snapshot={
            "schema_version": "ui-snapshot.v2",
            "sequence": True,
            "frames": [
                {
                    "index": 0,
                    "element_count": 1,
                    "elements": [
                        {
                            "selector": "#splash-creative",
                            "tag": "img",
                            "src": "https://cdn.example/creative.png",
                            "rect": {"width": 300, "height": 300},
                            "image": {"natural_width": 1000, "natural_height": 500},
                        }
                    ],
                },
                {
                    "index": 1,
                    "element_count": 1,
                    "elements": [{"selector": "main", "tag": "main"}],
                },
            ],
        },
    )
    packet["page"]["page_state"] = "countdown"

    results = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={
            "deterministic_tools": [
                "ad_region_detector_tool",
                "media_element_extractor_tool",
                "image_geometry_tool",
            ]
        },
    )

    detector, media, geometry = results
    assert detector["status"] == "ok"
    assert detector["facts"]["ad_regions"][0]["frame_index"] == 0
    assert detector["facts"]["signals"]["ad_page_context"] is True
    assert media["facts"]["media_elements"][0]["element"]["frame_index"] == 0
    assert geometry["facts"]["image_count"] == 1
    assert geometry["facts"]["geometry_anomalies"][0]["element"]["frame_index"] == 0


def test_image_geometry_preserves_same_image_across_frames() -> None:
    packet = _packet(
        page_type="startup_ad",
        ui_snapshot={
            "schema_version": "ui-snapshot.v2",
            "sequence": True,
            "frames": [
                {
                    "index": 0,
                    "elements": [
                        {
                            "selector": "#hero",
                            "tag": "img",
                            "src": "https://cdn.example/hero.png",
                            "rect": {"width": 200, "height": 100},
                            "image": {"natural_width": 1000, "natural_height": 500},
                        }
                    ],
                },
                {
                    "index": 1,
                    "elements": [
                        {
                            "selector": "#hero",
                            "tag": "img",
                            "src": "https://cdn.example/hero.png",
                            "rect": {"width": 200, "height": 200},
                            "image": {"natural_width": 1000, "natural_height": 500},
                        }
                    ],
                },
            ],
        },
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={"deterministic_tools": ["image_geometry_tool"]},
    )[0]

    assert result["facts"]["image_count"] == 2
    assert len(result["facts"]["geometry_anomalies"]) == 1
    assert result["facts"]["geometry_anomalies"][0]["element"]["frame_index"] == 1


def test_multi_frame_capture_requires_media_dimensions_in_two_frames() -> None:
    packet = _packet(
        ui_snapshot={
            "schema_version": "ui-snapshot.v2",
            "sequence": True,
            "interval_ms": 500,
            "frames": [
                {
                    "index": 0,
                    "elements": [
                        {
                            "selector": "#hero",
                            "tag": "img",
                            "rect": {"width": 320, "height": 180},
                            "image": {"natural_width": 1280, "natural_height": 720},
                        }
                    ],
                },
                {
                    "index": 1,
                    "elements": [
                        {
                            "selector": "#hero",
                            "tag": "img",
                            "rect": {"width": 300, "height": 180},
                            "image": {"natural_width": 1280, "natural_height": 720},
                        }
                    ],
                },
            ],
        }
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={"deterministic_tools": ["multi_frame_capture_tool"]},
    )[0]

    assert result["status"] == "ok"
    assert result["facts"]["measurable_frame_count"] == 2
    assert result["facts"]["frame_media_measurements"][1]["media"][0]["rendered_width"] == 300


def test_multi_frame_capture_rejects_frames_without_media_dimensions() -> None:
    packet = _packet(
        ui_snapshot={
            "schema_version": "ui-snapshot.v2",
            "sequence": True,
            "frames": [
                {
                    "index": 0,
                    "elements": [
                        {
                            "selector": "#hero",
                            "tag": "img",
                            "rect": {"width": 320, "height": 180},
                        }
                    ],
                },
                {
                    "index": 1,
                    "elements": [{"selector": "#hero", "tag": "img"}],
                },
            ],
        }
    )

    result = run_hybrid_capability_tools(
        packet_payload=packet,
        capability_config={"deterministic_tools": ["multi_frame_capture_tool"]},
    )[0]

    assert result["status"] == "insufficient"
    assert result["facts"]["measurable_frame_count"] == 1
    assert "媒体渲染尺寸" in result["reason"]


def _geometry_packet(
    *,
    elements: list[dict],
    viewport: dict | None = None,
    document: dict | None = None,
    thresholds: dict | None = None,
) -> dict:
    """构造带视口几何与规则阈值的观察包，供几何断言用例复用。"""
    snapshot: dict = {"schema_version": "ui-snapshot.v1", "elements": elements}
    if viewport is not None:
        snapshot["viewport"] = viewport
    if document is not None:
        snapshot["document"] = document
    packet = _packet(ui_snapshot=snapshot)
    packet["rule"] = {"criteria": {"thresholds": thresholds or {}}}
    return packet


def _run_geometry(packet: dict, capability_config: dict | None = None) -> dict:
    config = {"deterministic_tools": ["layout_geometry_assertion_handler"]}
    config.update(capability_config or {})
    return run_hybrid_capability_tools(packet_payload=packet, capability_config=config)[0]


def test_layout_geometry_window_padding_reads_thresholds_from_packet_rule() -> None:
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 400, "height": 800},
            document={"scroll_width": 400, "scroll_height": 800},
            thresholds={"min_window_padding_px": 20},
            elements=[
                {
                    "selector": "#brand",
                    "tag": "img",
                    "rect": {"x": 5, "y": 100, "width": 60, "height": 60},
                },
                {
                    "selector": "#background",
                    "tag": "div",
                    "rect": {"x": 0, "y": 0, "width": 400, "height": 800},
                },
                {
                    "selector": "#safe",
                    "tag": "p",
                    "text": "正文",
                    "rect": {"x": 40, "y": 300, "width": 200, "height": 40},
                },
            ],
        )
    )

    assert result["status"] == "ok"
    assert result["facts"]["thresholds_used"]["min_window_padding_px"] == 20
    padding = result["facts"]["assertions"]["window_padding"]
    assert padding["edges_checked"] == ["left", "top", "right", "bottom"]
    assert padding["spanning_excluded_count"] == 1
    assert padding["measured_element_count"] == 2
    assert padding["violation_count"] == 1
    assert padding["violations"][0]["element"]["selector"] == "#brand"
    assert padding["violations"][0]["min_edge_px"] == 5
    assert padding["overflow_count"] == 0


def test_layout_geometry_fullscreen_image_crop_flags_safe_margin_breach() -> None:
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 390, "height": 844},
            thresholds={"safe_margin_min_ratio": 0.1},
            elements=[
                {
                    "selector": "#hero",
                    "tag": "img",
                    "rect": {"x": -50, "y": 0, "width": 490, "height": 844},
                },
                {
                    "selector": "#promo",
                    "tag": "img",
                    "class_tokens": ["ad-banner"],
                    "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
                },
                {
                    "selector": "#thumb",
                    "tag": "img",
                    "rect": {"x": 10, "y": 10, "width": 100, "height": 100},
                },
            ],
        )
    )

    assert result["status"] == "ok"
    margin = result["facts"]["assertions"]["fullscreen_image_margin"]
    assert margin["fullscreen_image_count"] == 1
    assert margin["violation_count"] == 1
    violation = margin["violations"][0]
    assert violation["element"]["selector"] == "#hero"
    assert violation["crop_px"]["left"] == 50
    assert violation["crop_exceeds_safe_margin_edges"] == ["left", "right"]


def test_layout_geometry_bottom_blank_measures_gap_and_excludes_pinned() -> None:
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 390, "height": 800, "scroll_y": 1200},
            document={"scroll_height": 2000},
            thresholds={"max_bottom_blank_viewport_ratio": 0.15},
            elements=[
                {
                    "selector": "#last",
                    "tag": "p",
                    "text": "最后一条",
                    "rect": {"x": 20, "y": 460, "width": 200, "height": 40},
                },
                {
                    "selector": "#tabbar",
                    "tag": "div",
                    "style": {"position": "fixed"},
                    "rect": {"x": 0, "y": 750, "width": 390, "height": 50},
                },
            ],
        )
    )

    assert result["status"] == "ok"
    blank = result["facts"]["assertions"]["bottom_blank"]
    assert blank["at_bottom"] is True
    assert blank["blank_px"] == 300
    assert blank["blank_ratio"] == 0.375
    assert blank["exceeds_threshold"] is True
    assert blank["pinned_excluded_count"] == 1
    assert blank["bottom_most_element"]["selector"] == "#last"


def test_layout_geometry_bottom_blank_requires_scrolled_to_bottom_evidence() -> None:
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 390, "height": 800, "scroll_y": 0},
            document={"scroll_height": 2000},
            thresholds={"max_bottom_blank_viewport_ratio": 0.15},
            elements=[
                {
                    "selector": "#first",
                    "tag": "p",
                    "text": "内容",
                    "rect": {"x": 20, "y": 60, "width": 200, "height": 40},
                }
            ],
        )
    )

    assert result["status"] == "insufficient"
    assert "未滚动到页面底部" in result["reason"]


def test_layout_geometry_alignment_reports_column_deviation() -> None:
    cards = [
        {"selector": f"#card-{index}", "tag": "li", "class_tokens": ["card"], "rect": rect}
        for index, rect in enumerate(
            [
                {"x": 10, "y": 0, "width": 180, "height": 200},
                {"x": 10, "y": 220, "width": 180, "height": 200},
                {"x": 10, "y": 440, "width": 180, "height": 200},
                {"x": 210, "y": 0, "width": 180, "height": 200},
                {"x": 210, "y": 220, "width": 180, "height": 200},
                {"x": 213, "y": 440, "width": 180, "height": 200},
            ]
        )
    ]
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 400, "height": 800},
            thresholds={"max_column_align_deviation_px": 2},
            elements=cards,
        )
    )

    assert result["status"] == "ok"
    alignment = result["facts"]["assertions"]["alignment"]
    assert alignment["group_count"] == 1
    summary = alignment["groups"][0]
    assert summary["group"] == "li.card"
    assert summary["column_count"] == 2
    assert summary["row_count"] == 3
    assert summary["max_column_deviation_px"] == 3
    assert alignment["violation_count"] == 1
    violation = alignment["violations"][0]
    assert violation["kind"] == "column_left_deviation"
    assert violation["deviation_px"] == 3
    assert violation["threshold_px"] == 2


def test_layout_geometry_aspect_ratio_groups_expose_outlier_cover() -> None:
    covers = [
        {"selector": f"#cover-{index}", "tag": "img", "class_tokens": ["cover"], "rect": rect}
        for index, rect in enumerate(
            [
                {"x": 0, "y": 0, "width": 160, "height": 90},
                {"x": 170, "y": 0, "width": 160, "height": 90},
                {"x": 340, "y": 0, "width": 285, "height": 90},
            ]
        )
    ]
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 700, "height": 800},
            thresholds={"max_column_align_deviation_px": 2},
            elements=covers,
        )
    )

    assert result["status"] == "ok"
    aspect = result["facts"]["assertions"]["aspect_ratio"]
    assert aspect["group_count"] == 1
    summary = aspect["groups"][0]
    assert summary["group"] == "img.cover"
    assert summary["distinct_ratio_count"] == 2
    assert summary["ratio_groups"] == [
        {"ratio": 1.78, "count": 2},
        {"ratio": 3.17, "count": 1},
    ]
    assert "violation_count" not in aspect


def test_layout_geometry_overlap_separates_containment_and_reads_config_override() -> None:
    packet = _geometry_packet(
        viewport={"width": 500, "height": 400},
        thresholds={"max_overlap_pairs": 5},
        elements=[
            {
                "selector": "#panel",
                "tag": "div",
                "rect": {"x": 10, "y": 10, "width": 200, "height": 100},
            },
            {
                "selector": "#inside",
                "tag": "button",
                "text": "赠品",
                "rect": {"x": 20, "y": 20, "width": 80, "height": 40},
            },
            {
                "selector": "#pay",
                "tag": "button",
                "text": "立即充值",
                "rect": {"x": 300, "y": 30, "width": 100, "height": 40},
            },
            {
                "selector": "#cancel",
                "tag": "button",
                "text": "取消",
                "rect": {"x": 370, "y": 50, "width": 60, "height": 40},
            },
            {
                "selector": "#ghost",
                "tag": "div",
                "style": {"display": "none"},
                "rect": {"x": 300, "y": 30, "width": 100, "height": 40},
            },
        ],
    )

    result = _run_geometry(packet, {"thresholds": {"max_overlap_pairs": 0}})

    assert result["status"] == "ok"
    overlap = result["facts"]["assertions"]["overlap"]
    assert overlap["threshold_max_pairs"] == 0
    assert overlap["containment_pair_count"] == 1
    assert overlap["partial_overlap_pair_count"] == 1
    pair = overlap["partial_overlap_pairs"][0]
    assert {pair["first"]["selector"], pair["second"]["selector"]} == {"#pay", "#cancel"}
    assert pair["intersection_px"]["area"] == 600
    assert overlap["exceeds_threshold"] is True
    spacing = result["facts"]["assertions"]["spacing"]
    assert spacing["control_count"] == 3
    assert spacing["min_gap_px"] == 200
    assert "violations" not in spacing


def test_layout_geometry_spacing_flags_gap_below_configured_minimum() -> None:
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 400, "height": 200},
            thresholds={"min_control_spacing_px": 8},
            elements=[
                {
                    "selector": "#a",
                    "tag": "button",
                    "text": "确认",
                    "rect": {"x": 10, "y": 10, "width": 80, "height": 30},
                },
                {
                    "selector": "#b",
                    "tag": "button",
                    "text": "取消",
                    "rect": {"x": 94, "y": 10, "width": 80, "height": 30},
                },
            ],
        )
    )

    assert result["status"] == "ok"
    spacing = result["facts"]["assertions"]["spacing"]
    assert spacing["threshold_px"] == 8
    assert spacing["violation_count"] == 1
    assert spacing["violations"][0]["gap_px"] == 4
    assert spacing["violations"][0]["axis"] == "horizontal"
    assert "overlap" not in result["facts"]["assertions"]
    assert any(item["assertion"] == "overlap" for item in result["facts"]["skipped_assertions"])


def test_layout_geometry_without_configured_thresholds_is_insufficient() -> None:
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 400, "height": 800},
            thresholds={"min_confidence": 0.75},
            elements=[
                {
                    "selector": "#p",
                    "tag": "p",
                    "rect": {"x": 10, "y": 10, "width": 50, "height": 20},
                }
            ],
        )
    )

    assert result["status"] == "insufficient"
    assert "未配置几何断言阈值" in result["reason"]


def test_layout_geometry_without_visible_elements_is_insufficient() -> None:
    result = _run_geometry(
        _geometry_packet(
            viewport={"width": 400, "height": 800},
            thresholds={"min_window_padding_px": 20},
            elements=[],
        )
    )

    assert result["status"] == "insufficient"
    assert "没有可见元素" in result["reason"]


def _truncation_element(
    *,
    selector: str = "#title",
    text: str = "很长的标题文本内容",
    metrics: dict | None = None,
    style: dict | None = None,
) -> dict:
    element: dict = {"selector": selector, "tag": "div", "text": text}
    if metrics is not None:
        element["text_metrics"] = metrics
    if style is not None:
        element["style"] = style
    return element


def _run_truncation(elements: list[dict]) -> dict:
    return run_hybrid_capability_tools(
        packet_payload=_packet(elements=elements),
        capability_config={"deterministic_tools": ["text_truncation_detector_tool"]},
    )[0]


def test_text_truncation_reports_ellipsis_truncated_title() -> None:
    result = _run_truncation(
        [
            _truncation_element(
                metrics={
                    "scroll_width": 400,
                    "client_width": 200,
                    "horizontal_overflow": True,
                    "vertical_overflow": False,
                    "text_overflow_ellipsis": True,
                },
                style={
                    "overflow": "hidden",
                    "text_overflow": "ellipsis",
                    "white_space": "nowrap",
                },
            )
        ]
    )

    assert result["status"] == "ok"
    assert result["facts"]["truncated_count"] == 1
    assert result["facts"]["hard_clipped_count"] == 0
    entry = result["facts"]["truncated_text_elements"][0]
    assert entry["element"]["selector"] == "#title"
    assert entry["ellipsis_terminated"] is True
    assert entry["horizontal_overflow"] is True
    assert entry["visible_width_ratio"] == 0.5
    assert entry["line_clamp"] is None


def test_text_truncation_flags_hard_clip_without_ellipsis() -> None:
    result = _run_truncation(
        [
            _truncation_element(
                metrics={
                    "scroll_width": 300,
                    "client_width": 150,
                    "horizontal_overflow": True,
                },
                style={"overflow": "hidden"},
            )
        ]
    )

    assert result["facts"]["truncated_count"] == 1
    assert result["facts"]["hard_clipped_count"] == 1
    assert result["facts"]["truncated_text_elements"][0]["ellipsis_terminated"] is False


def test_text_truncation_reports_line_clamp_rows_and_height_ratio() -> None:
    result = _run_truncation(
        [
            _truncation_element(
                metrics={
                    "scroll_height": 96,
                    "client_height": 48,
                    "vertical_overflow": True,
                    "line_clamp": "2",
                },
            )
        ]
    )

    entry = result["facts"]["truncated_text_elements"][0]
    assert entry["line_clamp"] == "2"
    assert entry["vertical_overflow"] is True
    assert entry["visible_height_ratio"] == 0.5


def test_text_truncation_skips_scrollable_containers() -> None:
    result = _run_truncation(
        [
            _truncation_element(
                selector="#feed",
                metrics={
                    "scroll_height": 3000,
                    "client_height": 800,
                    "vertical_overflow": True,
                },
                style={"overflow_y": "auto"},
            )
        ]
    )

    assert result["status"] == "ok"
    assert result["facts"]["measured_element_count"] == 1
    assert result["facts"]["truncated_count"] == 0


def test_text_truncation_skips_fitting_and_textless_elements() -> None:
    result = _run_truncation(
        [
            _truncation_element(
                selector="#fits",
                metrics={
                    "scroll_width": 100,
                    "client_width": 200,
                    "horizontal_overflow": False,
                    "text_overflow_ellipsis": True,
                },
            ),
            _truncation_element(
                selector="#no-text",
                text="  ",
                metrics={
                    "scroll_width": 400,
                    "client_width": 200,
                    "horizontal_overflow": True,
                },
                style={"overflow": "hidden"},
            ),
        ]
    )

    assert result["facts"]["measured_element_count"] == 2
    assert result["facts"]["truncated_count"] == 0


def test_text_truncation_skips_visible_growth_without_clip_style() -> None:
    result = _run_truncation(
        [
            _truncation_element(
                metrics={
                    "scroll_height": 500,
                    "client_height": 300,
                    "vertical_overflow": True,
                },
                style={"overflow": "visible"},
            )
        ]
    )

    assert result["facts"]["truncated_count"] == 0


def test_text_truncation_insufficient_without_metrics() -> None:
    result = _run_truncation([{"selector": "#plain", "tag": "span", "text": "无截断字段"}])

    assert result["status"] == "insufficient"
    assert "text_metrics" in result["reason"]


def test_text_truncation_caps_reported_elements_but_counts_all() -> None:
    elements = [
        _truncation_element(
            selector=f"#title-{index}",
            metrics={
                "scroll_width": 400,
                "client_width": 200,
                "horizontal_overflow": True,
                "text_overflow_ellipsis": True,
            },
        )
        for index in range(120)
    ]

    result = _run_truncation(elements)

    assert result["facts"]["truncated_count"] == 120
    assert len(result["facts"]["truncated_text_elements"]) == 100


def test_text_truncation_ratio_guards_zero_and_overflowing_values() -> None:
    result = _run_truncation(
        [
            _truncation_element(
                metrics={
                    "scroll_width": 0,
                    "client_width": 200,
                    "scroll_height": 200,
                    "client_height": 300,
                    "horizontal_overflow": True,
                    "vertical_overflow": True,
                },
                style={"overflow": "hidden"},
            )
        ]
    )

    entry = result["facts"]["truncated_text_elements"][0]
    assert entry["visible_width_ratio"] is None
    assert entry["visible_height_ratio"] == 1.0


def test_visual_text_tools_of_truncation_rules_are_registered() -> None:
    for tool_name in ("text_style_extractor_tool", "text_truncation_detector_tool"):
        result = run_hybrid_capability_tools(
            packet_payload=_packet(),
            capability_config={"deterministic_tools": [tool_name]},
        )[0]
        assert result["tool_name"] == tool_name
        assert "尚未注册" not in str(result.get("reason") or "")


def test_sensitive_word_tool_scans_catalog_by_category_scope() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(text="评论区聊到脱欧和赌博。"),
        capability_config={
            "deterministic_tools": ["sensitive_word_detection_tool"],
            "sensitive_word_categories": ["uk_politics"],
        },
    )

    assert results[0]["status"] == "ok"
    facts = results[0]["facts"]
    assert facts["scanned_categories"] == ["uk_politics"]
    assert facts["catalog_version"]
    assert {hit["category"] for hit in facts["hits"]} == {"uk_politics"}
    assert facts["hit_count"] >= 1


def test_sensitive_word_tool_reports_unknown_category_as_insufficient() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(text="任意文本"),
        capability_config={
            "deterministic_tools": ["sensitive_word_detection_tool"],
            "sensitive_word_categories": ["uk_weather"],
        },
    )

    assert results[0]["status"] == "insufficient"
    assert "uk_weather" in results[0]["reason"]
    assert results[0]["facts"]["requested_categories"] == ["uk_weather"]


def test_sensitive_word_tool_keeps_legacy_inline_dictionary_behavior() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(text="页面出现测试词样本。"),
        capability_config={
            "deterministic_tools": ["sensitive_word_detection_tool"],
            "sensitive_words": ["测试词", "未出现词"],
        },
    )

    assert results[0]["status"] == "ok"
    assert results[0]["facts"] == {"hits": ["测试词"], "dictionary_size": 2}


def test_sensitive_word_tool_without_any_word_source_stays_insufficient() -> None:
    results = run_hybrid_capability_tools(
        packet_payload=_packet(text="任意文本"),
        capability_config={"deterministic_tools": ["sensitive_word_detection_tool"]},
    )

    assert results[0]["status"] == "insufficient"
    assert results[0]["reason"] == "数据库未配置敏感词库版本或词表"


def test_hybrid_typography_tool_ignores_chinese_punctuation_but_flags_fullwidth_alnum() -> None:
    """中文标点本来就是全角，不能当违规；全角字母、数字、空格才是「文字应使用半角」要抓的。"""
    chinese_only = run_hybrid_capability_tools(
        packet_payload=_packet(text="线路读取完成，请稍后！还有问题？没有：好的；（备注）"),
        capability_config={"deterministic_tools": ["typography_assertion_handler"]},
    )
    assert chinese_only[0]["status"] == "ok"
    assert chinese_only[0]["facts"]["fullwidth_ascii"] == []

    fullwidth_alnum = run_hybrid_capability_tools(
        packet_payload=_packet(text="会员ＶＩＰ　１２３元"),
        capability_config={"deterministic_tools": ["typography_assertion_handler"]},
    )
    assert fullwidth_alnum[0]["facts"]["fullwidth_ascii"] == (
        ["Ｖ", "Ｉ", "Ｐ", "\u3000", "１", "２", "３"]
    )


async def test_popup_sequence_trusts_hit_test_over_popup_like_class_names() -> None:
    """短视频页的播放器遮罩只是类名像弹窗：结构命中测试确认最上层是页面时，22 记不适用。"""
    mask = {
        "selector": "div.video-mask",
        "class_tokens": ["video-mask"],
        "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
    }
    packet = _packet(page_type="video_list", elements=[mask])

    # 没有展示层结论时保持原口径：疑似有弹窗但只有一帧 → 证据不足
    unknown = await evaluate_rule_engine_handler(
        handler="popup_sequence_handler", packet_payload=packet, engine_config={}
    )
    assert unknown.conclusion == "evidence_insufficient"

    packet["facts"]["presentation"] = {
        "topmost_layer_kind": "page",
        "hit_test_status": "available",
    }
    confirmed = await evaluate_rule_engine_handler(
        handler="popup_sequence_handler", packet_payload=packet, engine_config={}
    )
    assert confirmed.conclusion == "skipped"

    # 真有弹窗盖着（最上层是浮层）照旧要多帧
    packet["facts"]["presentation"] = {
        "topmost_layer_kind": "overlay",
        "hit_test_status": "available",
    }
    occluded = await evaluate_rule_engine_handler(
        handler="popup_sequence_handler", packet_payload=packet, engine_config={}
    )
    assert occluded.conclusion == "evidence_insufficient"
    assert occluded.facts["gap_codes"] == [
        "popup_sequence_next_frame_missing",
        "popup_sequence_cleared_observation_missing",
    ]


def _popup_sequence_snapshot(*present: bool) -> dict:
    popup = {
        "selector": "div.popup",
        "class_tokens": ["popup"],
        "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
    }
    return {
        "frames": [
            {"elements": [popup] if flag else []}
            for flag in present
        ]
    }


async def test_popup_sequence_requires_next_popup_and_cleared_watch() -> None:
    next_only = await evaluate_rule_engine_handler(
        handler="popup_sequence_handler",
        packet_payload=_packet(ui_snapshot=_popup_sequence_snapshot(True, True)),
        engine_config={},
    )
    assert next_only.conclusion == "evidence_insufficient"
    assert next_only.facts["gap_codes"] == ["popup_sequence_cleared_observation_missing"]

    clear_without_watch = await evaluate_rule_engine_handler(
        handler="popup_sequence_handler",
        packet_payload=_packet(ui_snapshot=_popup_sequence_snapshot(True, False)),
        engine_config={},
    )
    assert clear_without_watch.facts["gap_codes"] == [
        "popup_sequence_cleared_observation_missing"
    ]

    watched = await evaluate_rule_engine_handler(
        handler="popup_sequence_handler",
        packet_payload=_packet(ui_snapshot=_popup_sequence_snapshot(True, True, False, False)),
        engine_config={},
    )
    assert watched.conclusion == "pass"

    late = await evaluate_rule_engine_handler(
        handler="popup_sequence_handler",
        packet_payload=_packet(ui_snapshot=_popup_sequence_snapshot(True, False, True)),
        engine_config={},
    )
    assert late.conclusion == "fail"
