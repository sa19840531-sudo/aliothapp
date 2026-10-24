"""把能力映射表评审结论同步到首期规则数据库种子。"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULE_CSV = ROOT / "docs/data/alioth_first_ruleset_qc_rule_seed.csv"


@dataclass(frozen=True)
class CapabilityPlan:
    """单条数据库规则的执行能力配置。"""

    executor_type: str
    batch_group: str
    internal_tools: tuple[str, ...]
    semantic_judge: str | None = None
    handler: str | None = None
    requires_journey: bool = False


def _hybrid(
    batch_group: str,
    *tools: str,
    semantic_judge: str = "semantic_rule_judge_tool",
    requires_journey: bool = False,
) -> CapabilityPlan:
    return CapabilityPlan(
        executor_type="hybrid",
        batch_group=batch_group,
        internal_tools=tools,
        semantic_judge=semantic_judge,
        requires_journey=requires_journey,
    )


def _llm(
    batch_group: str,
    *tools: str,
    semantic_judge: str = "semantic_rule_judge_tool",
    requires_journey: bool = False,
) -> CapabilityPlan:
    return CapabilityPlan(
        executor_type="llm",
        batch_group=batch_group,
        internal_tools=tools,
        semantic_judge=semantic_judge,
        requires_journey=requires_journey,
    )


def _engine(
    batch_group: str,
    handler: str,
    *tools: str,
    requires_journey: bool = False,
) -> CapabilityPlan:
    return CapabilityPlan(
        executor_type="rule_engine",
        batch_group=batch_group,
        internal_tools=tools,
        handler=handler,
        requires_journey=requires_journey,
    )


PLANS: dict[str, CapabilityPlan] = {
    "alioth_2": _hybrid(
        "text-static",
        "text_style_extractor_tool",
        "typography_assertion_handler",
    ),
    "alioth_3": _hybrid(
        "visual-media",
        "media_element_extractor_tool",
        "multi_frame_capture_tool",
        "image_quality_analyzer_tool",
        semantic_judge="visual_rule_judge_tool",
    ),
    "alioth_4": _hybrid(
        "text-static",
        "visible_text_extractor_tool",
    ),
    "alioth_5": _hybrid(
        "visual-layout",
        "layout_region_extractor_tool",
        "network_console_summary_tool",
        "blank_module_detector_tool",
        semantic_judge="visual_rule_judge_tool",
    ),
    "alioth_6": _engine(
        "deterministic-text",
        "technical_error_text",
        "trusted_visible_text_reader",
        "deterministic_assertion_tool",
    ),
    "alioth_7": _engine(
        "interaction-navigation",
        "external_navigation_handler",
        "link_inventory_tool",
        "safe_click_observer_tool",
        requires_journey=True,
    ),
    "alioth_8": _hybrid(
        "interaction-state",
        "state_snapshot_tool",
        "network_event_summary_tool",
        "interaction_state_diff_tool",
        requires_journey=True,
    ),
    "alioth_9": _engine(
        "interaction-state",
        "data_refresh_handler",
        "state_snapshot_tool",
        "network_event_summary_tool",
        requires_journey=True,
    ),
    "alioth_10": _hybrid(
        "text-semantic",
        "transient_message_capture_tool",
        requires_journey=True,
    ),
    "alioth_11": _hybrid(
        "text-semantic",
        "transient_message_capture_tool",
        requires_journey=True,
    ),
    "alioth_12": _hybrid(
        "text-semantic",
        "transient_message_capture_tool",
        "punctuation_assertion_handler",
        requires_journey=True,
    ),
    "alioth_13": _hybrid(
        "payment-price",
        "product_price_extractor_tool",
        "cross_page_fact_store",
        "price_normalization_tool",
        requires_journey=True,
    ),
    "alioth_14": _engine(
        "deterministic-price",
        "discount_price_compare",
        "product_price_extractor_tool",
        "deterministic_assertion_tool",
    ),
    "alioth_16": _hybrid(
        "ad-visual",
        "ad_region_detector_tool",
        "media_element_extractor_tool",
        "image_geometry_tool",
        semantic_judge="visual_rule_judge_tool",
    ),
    "alioth_17": _hybrid(
        "ad-landing",
        "ad_region_detector_tool",
        "safe_click_observer_tool",
        "landing_page_probe_tool",
        "visual_semantic_compare_tool",
        semantic_judge="visual_rule_judge_tool",
        requires_journey=True,
    ),
    "alioth_18": _engine(
        "ad-timing",
        "countdown_accuracy_handler",
        "multi_frame_capture_tool",
        "monotonic_timer_tool",
        requires_journey=True,
    ),
    "alioth_19": _engine(
        "ad-control",
        "clickable_area_handler",
        "element_geometry_tool",
        "safe_hit_test_tool",
    ),
    "alioth_20": _engine(
        "ad-popup",
        "popup_close_behavior_handler",
        "ad_modal_detector_tool",
        "safe_click_observer_tool",
        requires_journey=True,
    ),
    "alioth_21": _engine(
        "ad-popup",
        "overlay_dismissal_handler",
        "ad_modal_detector_tool",
        "safe_click_observer_tool",
        requires_journey=True,
    ),
    "alioth_22": _engine(
        "ad-popup",
        "popup_sequence_handler",
        "ad_modal_detector_tool",
        "monotonic_timer_tool",
        requires_journey=True,
    ),
    "alioth_23": _hybrid(
        "ad-fallback",
        "network_fault_injection_tool",
        "ad_modal_detector_tool",
        "fallback_state_assertion_tool",
        semantic_judge="visual_rule_judge_tool",
        requires_journey=True,
    ),
    "alioth_24": _hybrid(
        "ad-control",
        "ad_control_inventory_tool",
        "safe_hit_test_tool",
        "control_behavior_assertion_tool",
        semantic_judge="visual_rule_judge_tool",
        requires_journey=True,
    ),
    "alioth_25": _engine(
        "ad-timing",
        "ad_first_frame_latency_handler",
        "performance_timeline_tool",
        "network_event_summary_tool",
    ),
    "alioth_26": _engine(
        "ad-fallback",
        "ad_failure_media_fallback_handler",
        "network_fault_injection_tool",
        "player_state_probe_tool",
        requires_journey=True,
    ),
    "alioth_27": _hybrid(
        "input-link",
        "ugc_link_extractor_tool",
        "safe_url_reputation_tool",
    ),
    "alioth_28": _hybrid(
        "input-safety",
        "ugc_content_extractor_tool",
        "sensitive_word_detection_tool",
        semantic_judge="content_safety_judge_tool",
    ),
    "alioth_29": _engine(
        "input-frequency",
        "spam_frequency_handler",
        "ugc_sequence_extractor_tool",
        "rate_limit_observer_tool",
        requires_journey=True,
    ),
    "alioth_30": _hybrid(
        "input-safety",
        "ugc_content_extractor_tool",
        "sensitive_word_detection_tool",
        semantic_judge="content_safety_judge_tool",
    ),
    "alioth_31": _engine(
        "interaction-state",
        "comment_state_sync_handler",
        "state_snapshot_tool",
        "network_event_summary_tool",
        requires_journey=True,
    ),
    "alioth_32": _hybrid(
        "input-safety",
        "ugc_content_extractor_tool",
        "sensitive_word_detection_tool",
        semantic_judge="content_safety_judge_tool",
    ),
    "alioth_34": _hybrid(
        "input-safety",
        "ugc_content_extractor_tool",
        "sensitive_word_detection_tool",
        semantic_judge="content_safety_judge_tool",
    ),
    "alioth_38": _engine(
        "deterministic-text",
        "label_case_consistency_handler",
        "label_group_extractor_tool",
    ),
    "alioth_40": _engine(
        "content-state",
        "offline_content_visibility_handler",
        "content_item_state_extractor_tool",
        "safe_click_observer_tool",
        requires_journey=True,
    ),
    "alioth_41": _engine(
        "deterministic-link",
        "qr_url_validation",
        "qr_url_extractor_tool",
        "qr_link_validation_tool",
    ),
    "alioth_42": _hybrid(
        "content-state",
        "activity_state_extractor_tool",
        "safe_click_observer_tool",
        "cross_page_state_assertion_tool",
        requires_journey=True,
    ),
    "alioth_43": _engine(
        "player-history",
        "playback_resume_handler",
        "player_state_probe_tool",
        "session_journey_runner_tool",
        requires_journey=True,
    ),
    "alioth_45": _engine(
        "input-upload",
        "upload_rule_visibility_handler",
        "upload_contract_extractor_tool",
        "safe_file_dialog_probe_tool",
    ),
    "alioth_46": _engine(
        "interaction-media",
        "media_fullscreen_handler",
        "media_element_extractor_tool",
        "safe_click_observer_tool",
        requires_journey=True,
    ),
    "alioth_48": _engine(
        "interaction-account",
        "logout_entry_presence_handler",
        "account_control_extractor_tool",
    ),
}

_ENGINE_DEFAULTS: dict[str, dict[str, object]] = {
    "technical_error_text": {
        "input_field": "visible_text",
        "pattern": (
            r"(?i)(JSON Parse Error|HTTP\s*500|Null(?:PointerException)?|"
            r"Internal Server Error|TypeError|ReferenceError)"
        ),
    },
    "discount_price_compare": {"operator": "lt"},
    "qr_url_validation": {"allowed_schemes": ["http", "https"]},
    "clickable_area_handler": {
        "target_keywords": ["跳过", "关闭", "×"],
        "min_width": 32,
        "min_height": 32,
    },
    "popup_sequence_handler": {"max_simultaneous_popups": 1},
    "ad_first_frame_latency_handler": {"max_latency_ms": 3_000},
    "spam_frequency_handler": {"max_same_content_count": 3},
    "upload_rule_visibility_handler": {"rule_keywords": ["格式", "大小", "支持", "上传"]},
    "logout_entry_presence_handler": {"target_keywords": ["退出登录", "退出账号", "注销"]},
}

_CAPABILITY_CONFIG_OVERRIDES: dict[str, dict[str, object]] = {
    "alioth_28": {
        "sensitive_word_categories": ["political", "terrorism", "personal_attack"]
    },
    "alioth_30": {
        "sensitive_word_categories": ["adult", "gambling", "private_ad"]
    },
    "alioth_32": {"sensitive_word_categories": ["china_related"]},
    "alioth_34": {"sensitive_word_categories": ["terrorism"]},
}


def _required_artifacts(row: dict[str, str], plan: CapabilityPlan) -> list[str]:
    category = row["category"]
    if plan.handler == "qr_url_validation":
        return ["visible_text", "ui_snapshot", "screenshot"]
    required = ["screenshot", "ui_snapshot"]
    if category in {"text", "payment", "input_risk"} or plan.handler in {
        "technical_error_text",
        "discount_price_compare",
        "label_case_consistency_handler",
        "upload_rule_visibility_handler",
        "logout_entry_presence_handler",
    }:
        required.extend(["dom", "visible_text"])
    elif category in {"visual", "ad"}:
        required.append("dom")
    if "transient_message_capture_tool" in plan.internal_tools:
        required.append("action_trace")
    return list(dict.fromkeys(required))


def _apply_plan(row: dict[str, str], plan: CapabilityPlan) -> dict[str, str]:
    updated = dict(row)
    updated["executor_type"] = plan.executor_type

    input_contract = json.loads(updated.get("input_contract_json") or "{}")
    required_artifacts = _required_artifacts(updated, plan)
    input_contract.update(
        {
            "schema_version": "1.1",
            "required_artifacts": required_artifacts,
            "optional_artifacts": ["har", "console_log", "ocr_text"],
            "required_fields": [
                "page.url",
                "facts.ui_snapshot",
                *(["facts.visible_text"] if "visible_text" in required_artifacts else []),
            ],
            "quality_gate": {
                "min_screenshot_count": 1,
                "allow_empty_visible_text": "visible_text" not in required_artifacts,
            },
        }
    )
    updated["input_contract_json"] = json.dumps(
        input_contract,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    judge_config = json.loads(updated.get("judge_config_json") or "{}")
    tool_refs = [
        "page_capture_tool",
        *plan.internal_tools,
        *([plan.semantic_judge] if plan.semantic_judge else ["rule_judge_tool"]),
    ]
    capability_config = {
        "schema_version": "1.0",
        "batch_group": plan.batch_group,
        "required_artifact_types": required_artifacts,
        "deterministic_tools": list(plan.internal_tools)
        if plan.executor_type == "hybrid"
        else [],
        "semantic_judge": plan.semantic_judge,
        "requires_journey": plan.requires_journey,
        "evidence_source": "observation_packet",
    }
    capability_config.update(_CAPABILITY_CONFIG_OVERRIDES.get(row["rule_code"], {}))
    judge_config.update(
        {
            "schema_version": "1.1",
            "tool_refs": list(dict.fromkeys(tool_refs)),
            "capability_config": capability_config,
        }
    )
    if plan.handler:
        previous = judge_config.get("engine_config")
        engine_config = dict(previous) if isinstance(previous, dict) else {}
        engine_config.update(_ENGINE_DEFAULTS.get(plan.handler, {}))
        engine_config["handler"] = plan.handler
        judge_config["engine_config"] = engine_config
    else:
        judge_config.pop("engine_config", None)
    updated["judge_config_json"] = json.dumps(
        judge_config,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return updated


def main() -> None:
    with RULE_CSV.open(newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    codes = {row["rule_code"] for row in rows}
    if codes != set(PLANS):
        missing = sorted(codes - set(PLANS))
        extra = sorted(set(PLANS) - codes)
        raise RuntimeError(f"能力方案与规则种子不一致：missing={missing}, extra={extra}")

    updated_rows = [_apply_plan(row, PLANS[row["rule_code"]]) for row in rows]
    with RULE_CSV.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(updated_rows)

    counts: dict[str, int] = {}
    for plan in PLANS.values():
        counts[plan.executor_type] = counts.get(plan.executor_type, 0) + 1
    print(
        json.dumps(
            {
                "rule_count": len(updated_rows),
                "executor_counts": counts,
                "output": str(RULE_CSV),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
