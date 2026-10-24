"""Web 报告公开白名单、逐规则状态与历史冻结兼容回归。"""

import pytest


def test_malformed_subject_summaries_cannot_crash_or_expose_private_evidence():
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(
        task_id="t",
        agent_run_id="r",
        epoch=0,
        rows={
            "web_logical_pages": [
                {
                    "subject_region_facts": {
                        "primary_subject_kind": {"private": True},
                        "primary_player_count": True,
                        "selector_summaries": [
                            None,
                            [],
                            ["primary_subject", ["private-secret", "article:sha256:" + "a" * 24]],
                        ],
                        "token_summaries": [["primary_subject", ["private-secret", "video"]]],
                    }
                }
            ],
        },
    )
    assert "private-secret" not in repr(result)
    assert "primary_player_count" not in result["page_classifications"][0]["subject_region_facts"]


@pytest.mark.asyncio
async def test_subject_facts_reach_report_snapshot_and_public_explanation(monkeypatch):
    from dataclasses import asdict

    from services.api.app.application.web_primary_subject_facts import extract_subject_region_facts
    from services.api.app.application.web_report_explanation import build_web_report_explanation
    from services.api.app.infrastructure.db import web_report_page_snapshot as snapshots
    from services.api.tests.web_subject_fixture_support import load_fixture

    facts = asdict(
        extract_subject_region_facts(load_fixture("video_primary_with_recommendations.html"))
    )
    fingerprint = "sha256:" + "a" * 64

    async def identities(*args, **kwargs):
        return {
            "capture": {
                "logical_page_identity_status": "verified",
                "logical_page_id": "logical",
                "canonical_url": "https://example.test/subject",
            }
        }

    class Connection:
        async def fetch(self, *args):
            return [
                {
                    "page_capture_id": "capture",
                    "url": "https://example.test/subject",
                    "page_type": "video_detail",
                    "page_seq": 1,
                    "capture_role": "after_action",
                    "screenshot_artifact_id": "shot",
                    "artifact_ids": ["shot"],
                    "menu_entry_ids": [],
                    "assessment": {
                        "final_page_type": "video_detail",
                        "classification_status": "classified",
                        "coverage_group": "business",
                        "subject_region_facts": facts,
                        "structure_fingerprint": fingerprint,
                        "guide_version": "frozen-guide",
                        "decision_source": "model",
                        "model_result": {"page_type": "content_detail", "confidence": .99},
                        "deterministic_result": {"page_type": "video_detail", "confidence": .9},
                    },
                }
            ]

    monkeypatch.setattr(snapshots, "read_web_capture_identities", identities)
    pages, _, _ = await snapshots.read_web_report_page_snapshot(
        Connection(), task_id="task", agent_run_id="run", epoch=0
    )
    assert pages[0]["subject_region_facts"]["primary_player_count"] == 1
    assert pages[0]["structure_fingerprint"] == fingerprint
    assert pages[0]["decision_source"] == "model"
    assert pages[0]["model_result"]["page_type"] == "content_detail"
    assert pages[0]["deterministic_result"]["page_type"] == "video_detail"
    public = build_web_report_explanation(
        task_id="task", agent_run_id="run", epoch=0, rows={"web_logical_pages": pages}
    )
    audit = public["page_classifications"][0]
    assert audit["guide_version"] == "frozen-guide"
    assert audit["decision_source"] == "model"
    assert audit["model_result"]["page_type"] == "content_detail"
    assert audit["deterministic_result"]["page_type"] == "video_detail"
    assert audit["structure_fingerprint"] == fingerprint
    assert audit["subject_region_facts"]["secondary_recommendation_card_count"] == 12
    assert audit["subject_region_facts"]["selector_summaries"]
    assert "https://" not in repr(audit)


@pytest.mark.parametrize("exit_reason", ["timeout", "loading"])
def test_sequence_readiness_public_fields_follow_actual_storage_and_closed_enums(exit_reason):
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(task_id="task", agent_run_id="run", epoch=0, rows={
        "sequences": [{"sequence_id": "seq", "purpose": "failure_sequence",
                       "readiness_status": "loading", "readiness_exit_reason": exit_reason,
                       "frames": [{"capture_id": "frame", "page_state": "loading"}]},
                      {"readiness_status": "PRIVATE", "readiness_exit_reason": "PRIVATE",
                       "frames": [{"page_state": "PRIVATE"}]}],
    })
    sequence = result["sequences"][0]
    assert sequence["readiness_status"] == "loading"
    assert sequence["readiness_exit_reason"] == exit_reason
    assert sequence["frames"][0]["page_state"] == "loading"
    assert "PRIVATE" not in str(result)
    assert "execution_status" not in sequence and "reason_code" not in sequence


@pytest.mark.asyncio
@pytest.mark.parametrize("oversized", ["artifact_ids", "menu_entry_ids"])
async def test_new_formal_page_snapshot_rejects_unbounded_nested_references(monkeypatch, oversized):
    from services.api.app.infrastructure.db import web_report_page_snapshot as snapshots

    async def identities(*args, **kwargs):
        return {}

    class Connection:
        async def fetch(self, *args):
            return [{"page_capture_id": "capture", "assessment": {}, oversized: ["ref"] * 101}]

    monkeypatch.setattr(snapshots, "read_web_capture_identities", identities)
    with pytest.raises(ValueError, match="WEB_REPORT_PAGE_REFERENCE_LIMIT_EXCEEDED"):
        await snapshots.read_web_report_page_snapshot(
            Connection(), task_id="task", agent_run_id="run", epoch=0
        )


@pytest.mark.asyncio
async def test_formal_page_snapshot_binds_only_parented_full_page_supplemental(monkeypatch):
    import json

    from services.api.app.infrastructure.db import web_report_page_snapshot as snapshots

    async def identities(*args, **kwargs):
        return {
            "capture-home": {
                "logical_page_identity_status": "verified",
                "logical_page_id": "logical-home",
                "canonical_url": "https://example.test/",
                "home_role_verified": True,
            }
        }

    expected = {
        "status": "captured",
        "capture_id": "capture-full",
        "screenshot_artifact_id": "shot-full",
        "reason_code": "WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
    }

    class Connection:
        async def fetch(self, query, *args):
            assert "parent_page_capture_id=p.id" in " ".join(query.split())
            assert "capture_role='supplemental'" in " ".join(query.split())
            assert "web_full_page_supplemental" in query
            assert "web-full-page-supplemental.v1" in query
            assert "supplemental_classification" not in query
            return [
                {
                    "page_capture_id": "capture-home",
                    "url": "https://example.test/",
                    "page_type": "home",
                    "page_seq": 1,
                    "inspection_status": "completed",
                    "captured_at": None,
                    "capture_purpose": "web_page_ready_baseline",
                    "capture_role": "after_action",
                    "parent_page_capture_id": None,
                    "menu_entry_ids": [],
                    "assessment": {
                        "final_page_type": "home",
                        "classification_status": "classified",
                        "coverage_group": "business",
                    },
                    "screenshot_artifact_id": "shot-home",
                    "artifact_ids": ["shot-home", "dom-home"],
                    "supplemental_full_page": json.dumps(expected),
                }
            ]

    monkeypatch.setattr(snapshots, "read_web_capture_identities", identities)
    pages, ad_pages, diagnostics = await snapshots.read_web_report_page_snapshot(
        Connection(), task_id="task", agent_run_id="run", epoch=0
    )

    assert diagnostics == []
    assert ad_pages == []
    assert pages[0]["primary_screenshot_artifact_id"] == "shot-home"
    assert pages[0]["supplemental_full_page"] == expected


@pytest.mark.asyncio
async def test_formal_page_snapshot_projects_ad_capture_as_evidence_only(monkeypatch):
    from services.api.app.infrastructure.db import web_report_page_snapshot as snapshots

    async def identities(*args, **kwargs):
        return {}

    class Connection:
        async def fetch(self, query, *args):
            compact = " ".join(query.split())
            assert "capture_purpose'='web_ad_page'" in compact
            assert "p.page_type='ad_page'" in compact
            assert (
                "COALESCE(p.metadata->>'capture_purpose','')<>'web_ad_page' "
                "OR a.metadata->>'purpose'='ad_landing'"
            ) in compact
            return [
                {
                    "page_capture_id": "capture-ad",
                    "url": "https://ads.example/landing",
                    "requested_url": "https://ads.example/campaign",
                    "page_type": "ad_page",
                    "page_seq": 2,
                    "inspection_status": None,
                    "captured_at": None,
                    "capture_purpose": "web_ad_page",
                    "capture_role": "supplemental",
                    "parent_page_capture_id": "capture-home",
                    "menu_entry_ids": [],
                    "assessment": {},
                    "screenshot_artifact_id": "shot-ad",
                    "artifact_ids": ["shot-ad", "dom-ad"],
                    "supplemental_full_page": None,
                }
            ]

    monkeypatch.setattr(snapshots, "read_web_capture_identities", identities)
    pages, ad_pages, diagnostics = await snapshots.read_web_report_page_snapshot(
        Connection(), task_id="task", agent_run_id="run", epoch=0
    )

    assert pages == []
    assert diagnostics == []
    assert ad_pages == [
        {
            "capture_id": "capture-ad",
            "parent_capture_id": "capture-home",
            "capture_purpose": "web_ad_page",
            "page_type": "ad_page",
            "coverage_group": "evidence_only",
            "requested_url": "https://ads.example/campaign",
            "final_url": "https://ads.example/landing",
            "captured_at": None,
            "screenshot_artifact_id": "shot-ad",
            "artifact_ids": ["shot-ad", "dom-ad"],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_reason", ["timeout", "loading"])
async def test_worker_web_formal_generation_reads_explanation_and_frozen_reader_keeps_history(
    exit_reason,
):
    from types import SimpleNamespace

    from services.api.app import worker
    from services.api.app.application.report_insight import _web_final_snapshot_insight
    from services.api.tests.test_web_final_report_binding import _coverage, _payload

    explanation = {
        "schema_version": "web-report-explanation.v1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "finalization_epoch": 4,
        "sequences": [
            {
                "sequence_id": "seq",
                "purpose": "failure_sequence",
                "readiness_status": "loading",
                "readiness_exit_reason": exit_reason,
                "frames": [{"capture_id": "frame", "page_state": "loading"}],
            }
        ],
        "web_logical_pages": [{"logical_page_id": "logical", "primary_capture_id": "frame"}],
    }

    class Reader:
        async def fetch_web_coverage_snapshot(self, **kwargs):
            return _payload(_coverage())["page_exploration"]["web_coverage"]

        async def fetch_web_report_explanation(self, **kwargs):
            assert kwargs["task_id"] == "task-1" and kwargs["agent_run_id"] == "run-1"
            return explanation

    record = SimpleNamespace(
        task_id="task-1",
        run_id="run-1",
        task_config={
            "workflow_version": "v2",
            "execution_mode": "full_inspection",
            "_task_target": {"platform": "web"},
        },
    )
    pages = await worker._page_exploration_for_report(record, reader=Reader())
    assert pages["web_report_explanation"] == explanation
    assert pages["web_logical_pages"][0]["primary_capture_id"] == "frame"
    frozen = _payload(_coverage())
    frozen["page_exploration"].update(pages)
    assert _web_final_snapshot_insight(frozen)["web_report_explanation"] == explanation
    assert _web_final_snapshot_insight(_payload(_coverage()))["web_report_explanation"] is None


def test_public_explanation_keeps_written_member_of_failed_attempt_and_hides_private_text():
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(
        task_id="task",
        agent_run_id="run",
        epoch=3,
        rows={
            "rule_materials": [
                {
                    "rule_id": "rule",
                    "work_item_id": "work",
                    "status": "written",
                    "work_status": "failed",
                    "input_scope": "page",
                    "attempts": [
                        {
                            "attempt_id": "attempt",
                            "status": "terminal_error",
                            "execution_status": "pass",
                            "reason": "PRIVATE MODEL",
                        }
                    ],
                    "canonical": {
                        "schema_version": "web-canonical-provenance.v1",
                        "selection": "conservative",
                        "sources": [
                            {
                                "work_item_id": "work",
                                "execution_status": "pass",
                                "raw_url": "SECRET",
                            }
                        ],
                    },
                }
            ],
            "sequences": [
                {
                    "sequence_id": "seq",
                    "purpose": "readiness_failure",
                    "reason_code": "WEB_TIMEOUT",
                    "raw_url": "SECRET",
                    "frames": [
                        {
                            "capture_id": "frame",
                            "capture_role": "after_action",
                            "selector": "PRIVATE",
                        }
                    ],
                }
            ],
        },
    )
    member = result["rule_materials"][0]
    assert member["status"] == "written"
    assert member["material_role"] == "stable"
    assert member["attempts"][0]["execution_status"] == "pass"
    assert "SECRET" not in str(result) and "PRIVATE" not in str(result)
    assert result["sequences"][0]["frames"] == [
        {"capture_id": "frame", "capture_role": "after_action"}
    ]


def test_public_explanation_sequence_role_requires_per_rule_input_binding():
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(
        task_id="task",
        agent_run_id="run",
        epoch=3,
        rows={
            "rule_materials": [
                {
                    "input_scope": "web_sequence",
                    "sequence_id": "sequence",
                    "evidence_input_set_id": "input",
                }
            ],
            "publication_rules": [
                {
                    "rule_id": "rule",
                    "selection": {
                        "status": "evidence_insufficient",
                        "reason_code": "WEB_RULE_ACCOUNT_STATE_UNKNOWN",
                    },
                }
            ],
        },
    )
    assert result["rule_materials"][0]["material_role"] == "sequence"
    assert result["publication_rules"][0]["selection"]["status"] == "evidence_insufficient"


def test_primary_failure_and_untrusted_identifier_values_are_bounded_public_facts():
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(
        task_id="task",
        agent_run_id="run",
        epoch=0,
        rows={
            "primary_content": [
                {
                    "menu_entry_id": "entry",
                    "status": "failed",
                    "parent_capture_id": "baseline",
                    "reason_code": "WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
                    "attempts": [
                        {
                            "stage": "observe",
                            "source_capture_id": "before",
                            "after_capture_id": "after",
                            "action_execution_id": "action",
                        }
                    ],
                }
            ],
            "notifications": [
                {
                    "capture_id": "https://secret.test/?token=PRIVATE",
                    "notification": {"reason_code": "PRIVATE model text", "status": "blocked"},
                }
            ],
        },
    )
    assert result["primary_content"][0]["attempts"][0]["source_capture_id"] == "before"
    assert result["primary_content"][0]["attempts"][0]["stage"] == "observe"
    assert "PRIVATE" not in str(result)


def test_frozen_subject_summary_rejects_nested_vendor_values_and_custom_roles():
    from copy import deepcopy

    from services.api.app.application.web_report_explanation import build_web_report_explanation

    frozen = {
        "player_vendor_tokens": ["dplayer", "private-secret", {"status": "private-secret"}, True],
        "selector_summaries": [
            ["private_secret", ["node:sha256:" + "a" * 24]],
            ["primary_subject", [
                "node:sha256:" + "b" * 24, {"status": "private-secret"},
                "private-secret:sha256:" + "a" * 24,
            ]],
        ],
        "token_summaries": [
            ["private_secret", ["video"]],
            ["primary_media_player", ["video", "private-secret", ["video"]]],
        ],
    }
    before = deepcopy(frozen)
    result = build_web_report_explanation(
        task_id="task", agent_run_id="run", epoch=0,
        rows={"web_logical_pages": [{"subject_region_facts": frozen}]},
    )["page_classifications"][0]["subject_region_facts"]
    assert result["player_vendor_tokens"] == ["dplayer"]
    assert result["selector_summaries"] == [["primary_subject", ["node:sha256:" + "b" * 24]]]
    assert result["token_summaries"] == [["primary_media_player", ["video"]]]
    assert "private" not in repr(result)
    assert frozen == before


def test_frozen_decision_audit_only_exposes_typed_closed_values():
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(
        task_id="task", agent_run_id="run", epoch=0,
        rows={"web_logical_pages": [{
            "subject_region_facts": {}, "decision_source": {"private": "secret"},
            "model_result": {
                "page_type": "private-secret", "confidence": True,
                "status": "private-secret", "needs_review": "private-secret",
            },
            "deterministic_result": {"page_type": "content_detail", "confidence": .9},
        }]},
    )["page_classifications"][0]
    assert result["model_result"] == {}
    assert result["decision_source"] is None
    assert result["deterministic_result"] == {"page_type": "content_detail", "confidence": .9}
    assert "private" not in repr(result)


@pytest.mark.parametrize("field", ["model_result", "deterministic_result"])
@pytest.mark.parametrize("confidence", [
    True, 10 ** 1000, float("nan"), float("inf"), -float("inf"), "0.9", {}, -1, 2,
], ids=["bool", "huge-int", "nan", "inf", "negative-inf", "string", "object", "negative", "over"])
def test_frozen_decision_confidence_rejects_invalid_values_without_conversion_crash(
    field, confidence
):
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    result = build_web_report_explanation(
        task_id="task", agent_run_id="run", epoch=0,
        rows={"web_logical_pages": [{
            "subject_region_facts": {},
            field: {"page_type": "content_detail", "confidence": confidence},
        }]},
    )["page_classifications"][0]
    assert result[field] == {"page_type": "content_detail"}


def test_public_nested_fact_limit_is_explicit_not_silent_truncation():
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    with pytest.raises(ValueError, match="LIMIT_EXCEEDED"):
        build_web_report_explanation(
            task_id="task",
            agent_run_id="run",
            epoch=0,
            rows={"rule_materials": [{"attempts": [{"attempt_id": str(i)} for i in range(101)]}]},
        )


def test_web_report_explanation_summarizes_business_lifecycle_and_unknown_separately():
    from services.api.app.application.web_report_explanation import build_web_report_explanation

    pages = [
        {"primary_capture_id": "gate", "page_type": "age_gate", "coverage_group": "lifecycle"},
        {"primary_capture_id": "home", "page_type": "home", "coverage_group": "business"},
        {"primary_capture_id": "search", "page_type": "search_page", "coverage_group": "business"},
        {
            "primary_capture_id": "opaque",
            "page_type": "unknown",
            "classification_status": "needs_review",
            "coverage_group": "neither",
        },
    ]

    result = build_web_report_explanation(
        task_id="task",
        agent_run_id="run",
        epoch=1,
        rows={
            "web_logical_pages": pages,
            "web_ad_pages": [{"capture_id": "ad", "page_type": "ad_page"}],
        },
    )

    assert result["page_type_summary"] == {
        "business_page_types": ["home", "search_page"],
        "lifecycle_page_types": ["age_gate"],
        "unknown_pages": ["opaque"],
        "ad_page_count": 1,
        "guide_page_count": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("platform,has_explanation", [("web", True), ("web", False), ("h5", True)])
async def test_real_html_export_keeps_frozen_web_explanation_without_changing_legacy_issue_data(
    platform, has_explanation
):
    from datetime import datetime

    from services.api.app.application.experience_report_export import render_experience_report
    from services.api.app.application.report_insight import ReportInsightService

    class ReadFixture(ReportInsightService):
        async def report_insight(self, task_id, **kwargs):
            return {
                "summary": {
                    "target_platform": platform,
                    "report_stage": "final",
                    "task_name": "冻结报告",
                    "generated_at": datetime(2026, 9, 11),
                },
                "confirm_progress": {"pending": 0},
                "rule_items": [],
                "web_report_explanation": {
                    "schema_version": "web-report-explanation.v1",
                    "task_id": "<script>UNSAFE</script>",
                    "agent_run_id": "run-1",
                    "finalization_epoch": 0,
                    "raw_model": "PRIVATE",
                    "sequences": [
                        {
                            "sequence_id": "sequence-1",
                            "readiness_status": "loading",
                            "readiness_exit_reason": "timeout",
                            "frames": [{"capture_id": "frame", "page_state": "loading"}],
                            "capability_revision": {
                                "scope": "current_document_light_dom",
                                "predicate": "native_video_element_present",
                                "account_state": "unknown",
                                "status": "local_absent",
                            },
                        }
                    ],
                }
                if has_explanation
                else None,
            }

    _, rendered = await ReadFixture(repository=object()).export_html("task-1")
    original = render_experience_report(
        title="冻结报告 质检报告", author="玉衡自动化质检", date="2026-09-11", issues=[]
    )
    if platform == "web" and has_explanation:
        assert "sequence-1" in rendered and "current_document_light_dom" in rendered
        assert "native_video_element_present" in rendered
        assert "readiness_exit_reason&quot;: &quot;timeout" in rendered
        assert "page_state&quot;: &quot;loading" in rendered
        assert "&lt;script&gt;UNSAFE&lt;/script&gt;" in rendered
        assert "<script>UNSAFE</script>" not in rendered and "PRIVATE" not in rendered
        assert (
            rendered.split("data-web-report-explanation")[0]
            == original.split("</body>")[0] + "<section "
        )
    else:
        assert rendered == original
