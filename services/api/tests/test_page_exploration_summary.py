"""动态页面探索汇总的统一口径测试。"""

import json

import pytest

from services.api.app.application import page_exploration_summary
from services.api.app.application.page_exploration_summary import (
    build_page_exploration_summary,
    summarize_working_memory,
)


def _logical_surface(
    *,
    identity_digit: str,
    state_digit: str,
    render_profile: str,
    page_type: str,
    evidence_sources: list[str] | None = None,
    degraded_reasons: list[str] | None = None,
) -> dict[str, object]:
    return {
        "protocol_version": "logical_surface.v1",
        "render_profile": {
            "kind": render_profile,
            "status": "confirmed",
            "confidence": 0.96,
        },
        "document_identity": "sha256:" + "9" * 64,
        "identity_fingerprint": "sha256:" + identity_digit * 64,
        "state_fingerprint": "sha256:" + state_digit * 64,
        "evidence_sources": evidence_sources or ["dom", "document"],
        "evidence_quality": "high",
        "degraded_reasons": degraded_reasons or [],
        "shadow": True,
        "page_type_binding": {
            "page_type": page_type,
            "page_type_confidence": 0.91,
            "confidence_source": "deterministic_structure",
            "coverage_allowed": True,
            "rule_matching_allowed": True,
            "reason_code": None,
        },
    }


def test_page_exploration_reports_logical_surface_without_counting_modal_as_page() -> None:
    home = _logical_surface(
        identity_digit="1",
        state_digit="2",
        render_profile="dom_spa",
        page_type="home",
    )
    home_modal = _logical_surface(
        identity_digit="1",
        state_digit="3",
        render_profile="dom_spa",
        page_type="home",
    )
    profile = _logical_surface(
        identity_digit="4",
        state_digit="5",
        render_profile="flutter_web",
        page_type="profile",
        evidence_sources=["flutter_semantics", "document"],
    )
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "transition_kind": "surface_state_changed",
        "logical_transition_proof": "trusted",
        "before_identity_fingerprint": "sha256:" + "1" * 64,
        "after_identity_fingerprint": "sha256:" + "1" * 64,
        "before_state_fingerprint": "sha256:" + "2" * 64,
        "after_state_fingerprint": "sha256:" + "3" * 64,
        "corroborating_signals": ["surface_state"],
    }

    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "capture-home",
                "page_type": "home",
                "logical_surface": home,
            },
            {
                "page_capture_id": "capture-home-modal",
                "page_type": "home",
                "logical_surface": home_modal,
            },
            {
                "page_capture_id": "capture-profile",
                "page_type": "profile",
                "logical_surface": profile,
            },
        ],
        actions=[
            {
                "exploration_action_id": "action-modal",
                "status": "explored",
                "logical_surface_transition": transition,
                "state_diff": {
                    "before_location": "https://example.test/app",
                    "after_location": "https://example.test/app",
                    "location_changed": False,
                },
            }
        ],
    )

    logical = summary["logical_surface"]
    assert logical["unique_logical_page_count"] == 2
    assert logical["observed_surface_state_count"] == 3
    assert logical["render_profile_counts"] == {"dom_spa": 2, "flutter_web": 1}
    assert logical["transition_counts"] == {"surface_state_changed": 1}
    assert logical["semantics_activation"] == {
        "flutter_or_hybrid_capture_count": 1,
        "activated_capture_count": 1,
        "unavailable_capture_count": 0,
        "activation_rate": 1.0,
    }
    assert logical["shadow_capture_count"] == 3
    assert summary["page_results"][0]["logical_surface"]["identity_fingerprint"] == (
        "sha256:" + "1" * 64
    )
    assert "identity_signals" not in summary["page_results"][0]["logical_surface"]


def test_page_exploration_omits_logical_surface_for_historical_rows() -> None:
    summary = build_page_exploration_summary(
        pages=[{"page_capture_id": "capture-legacy"}],
        actions=[],
    )

    assert "logical_surface" not in summary
    assert "visual_navigation" not in summary


def test_visual_navigation_summary_is_aggregate_only_and_redacted() -> None:
    """删除任一聚合白名单或恢复透传视觉对象时，本测试必须失败。"""
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "capture-1",
                "visual_navigation_audit": {
                    "protocol_version": "visual_navigation_audit.v1",
                    "mode": "shadow",
                    "detector_status": "recognized",
                    "prediction_count": 2,
                    "target_kind_counts": {
                        "primary_navigation": 1,
                        "profile_payment": 1,
                        "private_kind": 99,
                    },
                    "evaluated_prediction_count": 2,
                    "allowed_prediction_count": 1,
                    "signed_capability_count": 1,
                    "verified_capability_count": 1,
                    "enforced_candidate_count": 0,
                    "policy_rejection_counts": {
                        "visual_target_confidence_low": 1,
                        "private_reason_立即支付": 99,
                    },
                    "conflict_codes": ["visual_model_conflict", "private-conflict"],
                    "navigation_knowledge": {
                        "match_type_counts": {"static": 1, "exact": 0, "vector": 1},
                        "repository_degraded_count": 1,
                        "private_repository_query": "立即支付",
                    },
                    "model_usage": {
                        "request_count": 1,
                        "elapsed_ms": 125,
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_hit_tokens": 10,
                        "total_tokens": 120,
                        "vendor_payload": "private-model-response",
                    },
                    "target_signature": "sha256:signature",
                    "local_visual_digest": "sha256:local",
                    "normalized_rect": {"x": 0.1, "y": 0.2},
                    "recognized_label": "立即支付",
                    "visual_navigation_capture": {"screenshot_sha256": "sha256:image"},
                    "unknown_audit_object": {"private": "must-not-pass-through"},
                },
            },
            {
                "page_capture_id": "capture-2",
                "visual_navigation_audit": {
                    "protocol_version": "visual_navigation_audit.v1",
                    "mode": "shadow",
                    "detector_status": "insufficient",
                    "prediction_count": 0,
                    "target_kind_counts": {},
                    "signed_capability_count": 0,
                    "policy_rejection_counts": {},
                    "navigation_knowledge": {
                        "match_type_counts": {"static": 0, "exact": 1, "vector": 0},
                        "repository_degraded_count": 0,
                    },
                    "model_usage": {
                        "request_count": 1,
                        "elapsed_ms": 75,
                        "input_tokens": 50,
                        "output_tokens": 10,
                        "total_tokens": 60,
                    },
                },
            },
        ],
        actions=[],
    )

    visual = summary["visual_navigation"]
    assert set(visual) == {
        "mode",
        "eligible_capture_count",
        "insufficient_capture_count",
        "prediction_count",
        "allowed_prediction_count",
        "signed_capability_count",
        "verified_capability_count",
        "enforced_candidate_count",
        "executed_action_count",
        "trusted_transition_count",
        "target_kind_counts",
        "policy_rejection_counts",
        "error_reason_counts",
        "navigation_knowledge",
        "model_usage",
        "page_types_added",
    }
    assert visual["mode"] == "shadow"
    assert visual["eligible_capture_count"] == 2
    assert visual["insufficient_capture_count"] == 1
    assert visual["prediction_count"] == 2
    assert visual["target_kind_counts"] == {
        "primary_navigation": 1,
        "profile_payment": 1,
    }
    assert visual["policy_rejection_counts"] == {
        "visual_target_confidence_low": 1
    }
    assert visual["error_reason_counts"] == {"visual_model_conflict": 1}
    assert visual["navigation_knowledge"] == {
        "static_hit_count": 1,
        "exact_hit_count": 1,
        "vector_hit_count": 1,
        "repository_degraded_count": 1,
    }
    assert visual["model_usage"] == {
        "request_count": 2,
        "elapsed_ms": 200,
        "input_tokens": 150,
        "output_tokens": 30,
        "cache_hit_tokens": 10,
        "total_tokens": 180,
    }
    assert visual["page_types_added"] == []
    serialized = json.dumps(visual, ensure_ascii=False)
    for forbidden in (
        "target_signature",
        "local_visual_digest",
        "立即支付",
        "normalized_rect",
        "visual_navigation_capture",
        "private-model-response",
        "must-not-pass-through",
        "sha256:image",
    ):
        assert forbidden not in serialized
    assert "x" not in visual
    assert "y" not in visual


def test_visual_navigation_eligible_capture_count_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可用 Capture 数必须和其他报告计数一样在公共上限处饱和。"""
    monkeypatch.setattr(page_exploration_summary, "_MAX_VISUAL_REPORT_COUNT", 2)
    pages = [
        {
            "page_capture_id": f"capture-{index}",
            "visual_navigation_audit": {
                "protocol_version": "visual_navigation_audit.v1",
                "mode": "shadow",
                "detector_status": "recognized",
            },
        }
        for index in range(3)
    ]

    summary = build_page_exploration_summary(pages=pages, actions=[])

    assert summary["visual_navigation"]["eligible_capture_count"] == 2


def test_obstructed_visual_capture_is_eligible_but_insufficient_without_model_usage() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "capture-obstructed",
                "visual_navigation_audit": {
                    "protocol_version": "visual_navigation_audit.v1",
                    "detector_status": "insufficient",
                    "prediction_count": 0,
                    "target_kind_counts": {},
                    "conflict_codes": ["visual_source_obstructed"],
                },
            }
        ],
        actions=[],
        visual_navigation_mode="shadow",
    )

    visual = summary["visual_navigation"]
    assert visual["eligible_capture_count"] == 1
    assert visual["insufficient_capture_count"] == 1
    assert visual["error_reason_counts"] == {"visual_source_obstructed": 1}
    assert visual["model_usage"]["request_count"] == 0


def test_visual_navigation_page_types_added_require_enforced_four_domain_gate() -> None:
    """只按实际 after 页及四域门禁归因，expected intent 和视觉置信度不得造页型。"""
    trusted_identity = "sha256:" + "a" * 64
    pages = [
        {
            "page_capture_id": "capture-payment",
            "page_type": "payment",
            "detection_status": "recognized",
            "detection_confidence": 0.92,
            "visual_navigation_audit": {
                "protocol_version": "visual_navigation_audit.v1",
                "mode": "enforced",
                "detector_status": "recognized",
                "prediction_count": 1,
                "target_kind_counts": {"profile_payment": 1},
                "signed_capability_count": 1,
            },
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "identity_fingerprint": trusted_identity,
                "page_type_binding": {
                    "identity_fingerprint": trusted_identity,
                    "page_type": "payment",
                    "coverage_allowed": True,
                },
            },
        },
        {
            "page_capture_id": "capture-low-confidence",
            "page_type": "settings",
            "detection_status": "recognized",
            "detection_confidence": 0.69,
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "identity_fingerprint": "sha256:" + "b" * 64,
                "page_type_binding": {
                    "identity_fingerprint": "sha256:" + "b" * 64,
                    "page_type": "settings",
                    "coverage_allowed": True,
                },
            },
        },
        {
            "page_capture_id": "capture-startup-ad",
            "page_type": "startup_ad",
            "detection_status": "recognized",
            "detection_confidence": 0.95,
            "logical_surface": {
                "protocol_version": "logical_surface.v1",
                "identity_fingerprint": "sha256:" + "c" * 64,
                "page_type_binding": {
                    "identity_fingerprint": "sha256:" + "c" * 64,
                    "page_type": "startup_ad",
                    "coverage_allowed": True,
                },
            },
        },
    ]
    actions = [
        {
            "exploration_action_id": "visual-payment",
            "source_page_type": "profile",
            "expected_next_page_type": "settings",
            "status": "explored",
            "execution_status": "completed",
            "result_page_capture_id": "capture-payment",
            "parameters": {
                "target_mode": "visual_navigation_coordinate",
                "visual_target_confidence": 0.99,
            },
            "visual_navigation_transition": {
                "protocol_version": "visual_navigation_transition_proof.v1",
                "status": "trusted",
                "logical_transition_proof": "trusted",
                "after_identity_bound": True,
                "reason_codes": [],
            },
        },
        {
            "exploration_action_id": "visual-low-confidence",
            "source_page_type": "profile",
            "expected_next_page_type": "activity",
            "status": "explored",
            "execution_status": "completed",
            "result_page_capture_id": "capture-low-confidence",
            "parameters": {"target_mode": "visual_navigation_coordinate"},
            "visual_navigation_transition": {
                "protocol_version": "visual_navigation_transition_proof.v1",
                "status": "trusted",
                "logical_transition_proof": "trusted",
                "after_identity_bound": True,
                "reason_codes": [],
            },
        },
        {
            "exploration_action_id": "visual-lifecycle",
            "source_page_type": "home",
            "expected_next_page_type": "startup_ad",
            "status": "explored",
            "execution_status": "completed",
            "result_page_capture_id": "capture-startup-ad",
            "parameters": {"target_mode": "visual_navigation_coordinate"},
            "visual_navigation_transition": {
                "protocol_version": "visual_navigation_transition_proof.v1",
                "status": "trusted",
                "logical_transition_proof": "trusted",
                "after_identity_bound": True,
                "reason_codes": [],
            },
        },
    ]

    enforced = build_page_exploration_summary(pages=pages, actions=actions)
    shadow = build_page_exploration_summary(
        pages=pages,
        actions=actions,
        visual_navigation_mode="shadow",
    )

    assert enforced["visual_navigation"]["executed_action_count"] == 3
    assert enforced["visual_navigation"]["trusted_transition_count"] == 3
    assert enforced["visual_navigation"]["page_types_added"] == ["payment"]
    assert shadow["visual_navigation"]["page_types_added"] == []


def test_visual_navigation_executed_count_uses_persisted_click_commit_fact() -> None:
    """失败动作只有显式持久化 click committed 事实才能计入已执行。"""
    summary = build_page_exploration_summary(
        pages=[],
        actions=[
            {
                "exploration_action_id": "cleanup-interrupted",
                "action": "click",
                "status": "failed",
                "execution_status": "failed",
                "failure_code": "VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED",
                "parameters": {"target_mode": "visual_navigation_coordinate"},
                "result_payload": {
                    "state_diff": {"visual_action_committed": True},
                },
            },
            {
                "exploration_action_id": "postcondition-failed",
                "action": "click",
                "status": "failed",
                "execution_status": "failed",
                "failure_code": "VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED",
                "parameters": {"target_mode": "visual_navigation_coordinate"},
                "result_payload": {
                    "state_diff": {"visual_action_committed": True},
                },
            },
            {
                "exploration_action_id": "target-invalid",
                "action": "click",
                "status": "failed",
                "execution_status": "failed",
                "failure_code": "VISUAL_TARGET_INVALID",
                "parameters": {"visual_target_kind": "profile_payment"},
                "result_payload": {
                    "state_diff": {"visual_action_committed": False},
                },
            },
        ],
    )

    visual = summary["visual_navigation"]
    assert visual["executed_action_count"] == 2
    assert visual["trusted_transition_count"] == 0
    assert visual["page_types_added"] == []
    assert visual["error_reason_counts"] == {
        "VISUAL_ACTION_COMMITTED_CLEANUP_INTERRUPTED": 1,
        "VISUAL_ACTION_COMMITTED_POSTCONDITION_FAILED": 1,
        "VISUAL_TARGET_INVALID": 1,
    }


def test_visual_navigation_action_target_is_redacted_without_changing_legacy_actions() -> None:
    """视觉目标标签不得进入报告，普通历史动作仍保留原有 target。"""
    summary = build_page_exploration_summary(
        pages=[],
        actions=[
            {
                "exploration_action_id": "visual-action",
                "action": "click",
                "target": "立即支付",
                "status": "explored",
                "execution_status": "completed",
                "parameters": {"target_mode": "visual_navigation_coordinate"},
            },
            {
                "exploration_action_id": "legacy-action",
                "action": "click",
                "target": "账户设置",
                "status": "explored",
                "execution_status": "completed",
                "parameters": {},
            },
        ],
    )

    visual_action, legacy_action = summary["action_results"]
    assert visual_action["target"] is None
    assert legacy_action["target"] == "账户设置"
    assert "立即支付" not in json.dumps(summary, ensure_ascii=False)


def test_visual_navigation_action_target_is_redacted_by_any_runtime_provenance() -> None:
    """删除或改写 target_mode 不能让仍带视觉来源的动作降级并泄露标签。"""
    summary = build_page_exploration_summary(
        pages=[],
        actions=[
            {
                "exploration_action_id": "visual-capability",
                "action": "click",
                "target": "能力对象内的敏感标签",
                "status": "failed",
                "execution_status": "failed",
                "parameters": {
                    "visual_navigation_capability": {"private": "不得投影"},
                },
            },
            {
                "exploration_action_id": "visual-provenance",
                "action": "click",
                "target": "来源标记对应的敏感标签",
                "status": "failed",
                "execution_status": "failed",
                "parameters": {
                    "target_mode": "bottom_navigation",
                    "visual_navigation_provenance": "visual_detector",
                },
            },
            {
                "exploration_action_id": "visual-kind",
                "action": "click",
                "target": "视觉目标类型对应的敏感标签",
                "status": "failed",
                "execution_status": "failed",
                "parameters": {"visual_target_kind": "profile_payment"},
            },
            {
                "exploration_action_id": "sql-provenance",
                "action": "click",
                "target": "查询层标记对应的敏感标签",
                "status": "failed",
                "execution_status": "failed",
                "is_visual_navigation_provenance": True,
                "parameters": {},
            },
            {
                "exploration_action_id": "legacy-action",
                "action": "click",
                "target": "普通历史标签",
                "status": "explored",
                "execution_status": "completed",
                "parameters": {"target_mode": "bottom_navigation"},
            },
        ],
    )

    assert [item["target"] for item in summary["action_results"]] == [
        None,
        None,
        None,
        None,
        "普通历史标签",
    ]
    encoded = json.dumps(summary, ensure_ascii=False)
    for secret in (
        "能力对象内的敏感标签",
        "来源标记对应的敏感标签",
        "视觉目标类型对应的敏感标签",
        "查询层标记对应的敏感标签",
        "不得投影",
    ):
        assert secret not in encoded


def test_summary_counts_dynamic_pages_without_expected_total() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-1",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            },
            {
                "page_capture_id": "cap-2",
                "inspection_lifecycle_status": "active",
                "inspection_status": "degraded",
            },
            {
                "page_capture_id": "cap-old",
                "inspection_lifecycle_status": "superseded",
                "inspection_status": "detecting",
            },
        ],
        actions=[
            {"exploration_action_id": "exploration-1", "status": "explored"},
            {"exploration_action_id": "exploration-2", "status": "failed"},
        ],
    )

    assert summary["page_total"] == 3
    assert summary["active_page_count"] == 2
    assert summary["terminal_page_count"] == 2
    assert summary["pending_page_count"] == 0
    assert summary["superseded_page_count"] == 1
    assert summary["action_total"] == 2
    assert summary["pending_action_count"] == 0
    assert summary["complete"] is True
    assert summary["requires_review"] is True
    assert summary["quality_status"] == "needs_review"
    assert "expected_page_count" not in summary
    assert summary["page_results"] == [
        {
            "page_capture_id": "cap-1",
            "parent_page_capture_id": None,
            "supersedes_page_capture_id": None,
            "page_seq": None,
            "page_type": None,
            "page_url": None,
            "page_title": None,
            "captured_at": None,
            "detection_status": None,
            "lifecycle_status": "active",
            "inspection_status": "completed",
            "failure_stage": None,
            "failure_code": None,
            "failure_message": None,
            "failure_retryable": None,
            "abandon_reason": None,
        },
        {
            "page_capture_id": "cap-2",
            "parent_page_capture_id": None,
            "supersedes_page_capture_id": None,
            "page_seq": None,
            "page_type": None,
            "page_url": None,
            "page_title": None,
            "captured_at": None,
            "detection_status": None,
            "lifecycle_status": "active",
            "inspection_status": "degraded",
            "failure_stage": None,
            "failure_code": None,
            "failure_message": None,
            "failure_retryable": None,
            "abandon_reason": None,
        },
        {
            "page_capture_id": "cap-old",
            "parent_page_capture_id": None,
            "supersedes_page_capture_id": None,
            "page_seq": None,
            "page_type": None,
            "page_url": None,
            "page_title": None,
            "captured_at": None,
            "detection_status": None,
            "lifecycle_status": "superseded",
            "inspection_status": "detecting",
            "failure_stage": None,
            "failure_code": None,
            "failure_message": None,
            "failure_retryable": None,
            "abandon_reason": None,
        },
    ]


def test_pending_frontier_is_the_completion_gate() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-1",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            }
        ],
        actions=[{"exploration_action_id": "exploration-2", "status": "candidate"}],
    )

    assert summary["pending_page_count"] == 0
    assert summary["pending_action_count"] == 1
    assert summary["pending_work_count"] == 1
    assert summary["complete"] is False
    assert summary["quality_status"] == "in_progress"


def test_page_result_projects_safety_interstitial_diagnostics() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-safety",
                "inspection_lifecycle_status": "active",
                "inspection_status": "detecting",
                "page_detection": {
                    "detection_status": "ambiguous",
                    "page_state": "safety_interstitial",
                    "risk_flags": [
                        "safety_interstitial",
                        "automation_blocked",
                    ],
                    "conflicts": ["target_blocked_by_safety_interstitial"],
                },
            }
        ],
        actions=[],
    )

    result = summary["page_results"][0]
    assert result["page_state"] == "safety_interstitial"
    assert result["risk_flags"] == ["safety_interstitial", "automation_blocked"]
    assert result["conflicts"] == ["target_blocked_by_safety_interstitial"]
    assert result["target_access_status"] == "blocked_by_safety_interstitial"


def test_only_abandoned_pages_cannot_report_complete_quality() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-1",
                "inspection_lifecycle_status": "abandoned",
                "inspection_status": "detecting",
                "inspection_abandon_reason": "page_type_not_rule_matchable",
            }
        ],
        actions=[{"exploration_action_id": "exploration-1", "status": "explored"}],
    )

    assert summary["complete"] is True
    assert summary["terminal_page_count"] == 0
    assert summary["requires_review"] is True
    assert summary["quality_status"] == "needs_review"


def test_working_memory_deduplicates_pages_across_snapshot_buckets() -> None:
    page = {
        "page_capture_id": "cap-1",
        "lifecycle_status": "active",
        "inspection_status": "failed",
    }
    summary = summarize_working_memory(
        {
            "schema_version": 2,
            "current_page": None,
            "inspected_pages": [page],
            "terminal_pages": [page],
            "exploration_frontier": {
                "pending_actions": [],
                "explored_actions": [],
                "inactive_actions": [],
            },
        }
    )

    assert summary["tracking_available"] is True
    assert summary["page_total"] == 1
    assert summary["failed_page_count"] == 1
    assert summary["complete"] is True


def test_working_memory_uses_persisted_dynamic_assessment_scope() -> None:
    summary = summarize_working_memory(
        {
            "schema_version": 2,
            "page_type_assessments": [
                {
                    "page_type": "home",
                    "status": "covered",
                    "reason_code": "recognized_with_evidence",
                    "page_capture_ids": ["cap-home"],
                    "evidence_artifact_ids": ["artifact-home"],
                    "exploration_action_ids": [],
                }
            ],
            "page_type_assessment_scope_complete": True,
            "page_type_assessment_scope_page_types": ["home", "content_list"],
            "current_page": None,
            "inspected_pages": [],
            "terminal_pages": [],
            "pending_pages": [],
            "inactive_pages": [],
            "exploration_frontier": {
                "pending_actions": [],
                "explored_actions": [],
                "inactive_actions": [],
            },
        }
    )

    product = summary["coverage"]["product_business"]
    assert product["missing_assessment_page_types"] == ["content_list"]
    # 声明为目标却整轮没走到的页型改按「已知未覆盖」计入分母，因此作用域
    # 本身是验过的；此前这里恒假会让比率永远留空。
    assert product["assessment_scope_verified"] is True
    assert product["known_uncovered_page_types"] == ["content_list"]
    assert product["applicable_count"] == 2
    # 本例 home 没有可配对的采集证据，被降级为证据不足，比率仍不发布。
    assert product["unresolved_page_types"] == ["home"]
    assert product["coverage_rate"] is None


def test_page_result_normalizes_database_and_working_memory_failure_shapes() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-db",
                "parent_page_capture_id": "cap-parent",
                "page_seq": 2,
                "page_type": "payment",
                "url": "https://example.com/payment",
                "inspection_lifecycle_status": "active",
                "inspection_status": "failed",
                "inspection_failure_stage": "rule_persistence",
                "inspection_failure_code": "RULE_WRITE_FAILED",
                "inspection_failure_message": "写入失败",
                "inspection_failure_retryable": True,
            },
            {
                "page_capture_id": "cap-memory",
                "page_url": "https://example.com/result",
                "page_detection": {"page_type": "result"},
                "lifecycle_status": "active",
                "inspection_status": "degraded",
                "failure": {
                    "stage": "rule_execution",
                    "code": "RULE_TOOL_FAILED",
                    "message": "规则工具失败",
                    "retryable": False,
                },
            },
        ],
        actions=[],
    )

    database_result, memory_result = summary["page_results"]
    assert database_result["parent_page_capture_id"] == "cap-parent"
    assert database_result["page_url"] == "https://example.com/payment"
    assert database_result["failure_code"] == "RULE_WRITE_FAILED"
    assert database_result["failure_retryable"] is True
    assert memory_result["page_type"] == "result"
    assert memory_result["failure_code"] == "RULE_TOOL_FAILED"
    assert memory_result["failure_retryable"] is False


def test_page_result_preserves_app_capture_metadata() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "app-cap",
                "page_type": "app",
                "package_name": "com.example.demo",
                "activity": ".MainActivity",
                "width": 1080,
                "height": 1920,
            }
        ],
        actions=[],
    )

    assert summary["page_results"][0]["package_name"] == "com.example.demo"
    assert summary["page_results"][0]["activity"] == ".MainActivity"
    assert summary["page_results"][0]["width"] == 1080
    assert summary["page_results"][0]["height"] == 1920


def test_action_result_keeps_popup_closure_identity_and_bounded_parameters() -> None:
    summary = build_page_exploration_summary(
        pages=[],
        actions=[
            {
                "action_execution_id": "action-1",
                "action": "click",
                "target": "关闭当前弹窗",
                "purpose": "关闭遮挡弹窗",
                "risk_level": "low",
                "status": "explored",
                "execution_status": "completed",
                "command_sequence": 7,
                "requested_source_capture_id": "capture-popup",
                "source_page_capture_id": "capture-before",
                "result_page_capture_id": "capture-after",
                "expected_next_page_type": "HOME",
                "parameters": {
                    "target_mode": "popup_dismissal",
                    "popup_dismissal_capability": "dialog_close",
                    "candidate_id": "private-candidate",
                },
            }
        ],
    )

    assert summary["action_results"] == [
        {
            "exploration_action_id": "action-1",
            "action": "click",
            "target": "关闭当前弹窗",
            "purpose": "关闭遮挡弹窗",
            "risk_level": "low",
            "status": "explored",
            "execution_status": "completed",
            "command_sequence": 7,
            "source_page_capture_id": "capture-before",
            "requested_source_capture_id": "capture-popup",
            "result_page_capture_id": "capture-after",
            "expected_next_page_type": "home",
            "failure_code": None,
            "failure_message": None,
            "parameters": {
                "target_mode": "popup_dismissal",
                "popup_dismissal_capability": "dialog_close",
            },
        }
    ]


def test_historical_rows_without_tracking_columns_are_unknown() -> None:
    summary = build_page_exploration_summary(
        pages=[{"page_capture_id": "legacy-cap"}],
        actions=[],
    )

    assert summary["tracking_available"] is False
    assert summary["complete"] is None
    assert summary["quality_status"] == "unknown"


def test_summary_requires_evidence_and_uses_explicit_product_assessments() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-line",
                "artifact_ids": ["artifact-line"],
                "page_type": "line_loading",
                "page_state": "line_loading",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "abandoned",
                "inspection_status": "completed",
            },
            {
                "page_capture_id": "cap-video",
                "artifact_ids": ["artifact-video"],
                "page_type": "video_list",
                "page_state": "loading",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            },
            {
                "page_capture_id": "cap-home",
                "artifact_ids": ["artifact-home"],
                "page_type": "home",
                "page_state": "stable",
                "detection_status": "recognized",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            },
        ],
        actions=[
            {
                "exploration_action_id": "payment-blocked",
                "origin": "discovered",
                "source_page_type": "home",
                "expected_next_page_type": "payment",
                "action": "click",
                "status": "blocked",
                "risk_level": "high",
            }
        ],
        page_type_assessments=[
            {
                "page_type": "home",
                "status": "covered",
                "reason_code": "recognized_with_evidence",
                "page_capture_ids": ["cap-home"],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
            },
            {
                "page_type": "video_list",
                "status": "covered",
                "reason_code": "recognized_with_evidence",
                "page_capture_ids": ["cap-video"],
                "evidence_artifact_ids": ["artifact-video"],
                "exploration_action_ids": [],
            },
            {
                "page_type": "payment",
                "status": "blocked_by_safety",
                "reason_code": "only_high_risk_entry",
                "page_capture_ids": [],
                "evidence_artifact_ids": [],
                "exploration_action_ids": ["payment-blocked"],
            },
        ],
        assessment_scope_complete=True,
    )

    coverage = summary["coverage"]
    assert coverage["catalog_observation"]["observed_page_types"] == [
        "line_loading",
        "home",
        "video_list",
    ]
    assert coverage["lifecycle_shells"]["strict_line_loading_observed"] is True
    assert coverage["lifecycle_shells"]["loading_state_observed"] is True
    assert coverage["product_business"]["applicable_page_types"] == [
        "home",
        "payment",
        "video_list",
    ]
    assert coverage["product_business"]["covered_page_types"] == [
        "home",
        "video_list",
    ]
    assert coverage["product_business"]["covered_count"] == 2
    assert coverage["product_business"]["applicable_count"] == 3
    assert coverage["product_business"]["assessment_scope_verified"] is True
    assert coverage["product_business"]["coverage_rate"] == 0.6667
    assert coverage["acceptance_complete"] is None
    assert summary["page_results"][1]["page_state"] == "loading"
    assert summary["page_results"][1]["artifact_ids"] == ["artifact-video"]


def test_summary_rejects_unproven_pages_and_non_business_frontier_actions() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-home",
                "page_type": "home",
                "page_state": "stable",
                "inspection_lifecycle_status": "active",
                "inspection_status": "completed",
            }
        ],
        actions=[
            {
                "exploration_action_id": "safe-done",
                "origin": "discovered",
                "source_page_type": "profile",
                "expected_next_page_type": "settings",
                "action": "click",
                "status": "explored",
                "risk_level": "low",
            },
            {
                "exploration_action_id": "safe-pending",
                "origin": "discovered",
                "source_page_type": "home",
                "expected_next_page_type": "post_publish",
                "action": "click",
                "status": "candidate",
                "risk_level": "low",
            },
            {
                "exploration_action_id": "unknown-risk",
                "origin": "discovered",
                "source_page_type": "home",
                "expected_next_page_type": "activity",
                "action": "click",
                "status": "candidate",
                "risk_level": None,
            },
            {
                "exploration_action_id": "restore",
                "origin": "recovery",
                "source_page_type": "profile",
                "expected_next_page_type": "profile",
                "action": "goto",
                "status": "explored",
                "risk_level": "low",
            },
        ],
    )

    coverage = summary["coverage"]
    assert coverage["catalog_observation"]["observed_page_types"] == []
    assert coverage["product_business"]["assessment_available"] is False
    assert coverage["product_business"]["coverage_rate"] is None
    assert coverage["exploration"]["action_total"] == 2
    assert coverage["exploration"]["terminal_count"] == 1
    assert coverage["exploration"]["terminal_rate"] == 0.5


def test_product_coverage_is_unknown_when_assessment_scope_is_incomplete() -> None:
    summary = build_page_exploration_summary(
        pages=[],
        actions=[],
        page_type_assessments=[
            {
                "page_type": "settings",
                "status": "evidence_insufficient",
                "reason_code": "entry_not_confirmed",
                "page_capture_ids": [],
                "evidence_artifact_ids": [],
                "exploration_action_ids": [],
            }
        ],
        assessment_scope_complete=True,
    )

    product = summary["coverage"]["product_business"]
    assert product["assessment_available"] is True
    assert product["assessment_complete"] is False
    assert product["coverage_rate"] is None


def test_acceptance_is_incomplete_while_page_work_is_pending() -> None:
    summary = build_page_exploration_summary(
        pages=[
            {
                "page_capture_id": "cap-home",
                "page_type": "home",
                "page_state": "stable",
                "page_detection": {
                    "detection_status": "recognized",
                    "page_type": "home",
                    "page_state": "stable",
                },
                "artifact_ids": ["artifact-home"],
                "inspection_lifecycle_status": "active",
                "inspection_status": "evaluating",
            }
        ],
        actions=[
            {
                "exploration_action_id": "home-entry",
                "origin": "discovered",
                "source_page_type": "content_list",
                "expected_next_page_type": "home",
                "action": "click",
                "status": "explored",
                "risk_level": "low",
            }
        ],
        page_type_assessments=[
            {
                "page_type": "home",
                "status": "covered",
                "reason_code": "recognized_with_evidence",
                "page_capture_ids": ["cap-home"],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
            }
        ],
        assessment_scope_complete=True,
        required_page_types=["home"],
    )

    assert summary["complete"] is False
    assert summary["coverage"]["product_business"]["assessment_complete"] is True
    assert summary["coverage"]["exploration"]["terminal_rate"] == 1.0
    assert summary["coverage"]["acceptance_complete"] is False


def test_business_frontier_target_cannot_be_removed_from_dynamic_scope() -> None:
    summary = build_page_exploration_summary(
        pages=[],
        actions=[
            {
                "exploration_action_id": "detail-entry",
                "origin": "discovered",
                "source_page_type": "home",
                "expected_next_page_type": "content_detail",
                "action": "click",
                "status": "explored",
                "risk_level": "low",
            }
        ],
        page_type_assessments=[
            {
                "page_type": "home",
                "status": "not_applicable",
                "reason_code": "entry_absent",
                "page_capture_ids": [],
                "evidence_artifact_ids": ["artifact-home"],
                "exploration_action_ids": [],
            }
        ],
        assessment_scope_complete=True,
        required_page_types=["home"],
    )

    product = summary["coverage"]["product_business"]
    assert product["assessment_complete"] is False
    assert product["missing_assessment_page_types"] == ["content_detail"]
    assert summary["coverage"]["acceptance_complete"] is False


def test_app_observed_scope_summary_preserves_unseen_types_separately() -> None:
    """本轮未观察到与已有不适用证明分别投影，不能把目录当作必达分母。"""
    from services.api.app.application.page_exploration_summary import (
        apply_app_page_type_applicability,
    )

    summary = {"coverage": {}}
    apply_app_page_type_applicability(
        summary,
        app_coverage={
            "page_type_applicability": {
                "assessment_scope_kind": "observed_frontier",
                "assessment_scope_complete": True,
                "assessment_scope_verified": True,
                "covered_page_types": ["home"],
                "known_uncovered_page_types": [],
                "not_applicable_page_types": ["video_detail"],
                "not_observed_page_types": ["payment", "post_publish"],
                "unresolved_applicability_page_types": [],
                "applicable_page_types": ["home"],
                "covered_count": 1,
                "applicable_count": 1,
                "coverage_rate": 1.0,
            }
        },
    )

    business = summary["coverage"]["product_business"]
    assert business["assessment_scope_kind"] == "observed_frontier"
    assert business["not_observed_page_types"] == ["payment", "post_publish"]
    assert business["not_applicable_page_types"] == ["video_detail"]
    assert business["applicable_count"] == business["covered_count"] == 1
    assert business["assessment_complete"] is True
