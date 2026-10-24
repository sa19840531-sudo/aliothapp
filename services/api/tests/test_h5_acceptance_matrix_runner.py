"""H5 验收批次执行器测试。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import scripts.run_h5_acceptance_matrix as acceptance_matrix
from scripts.run_h5_acceptance_matrix import (
    _bind_fixture_attempt_key,
    _check_browser_crash_recovery,
    _check_capture_retry,
    _check_evidence_failure,
    _check_expected_page_types,
    _check_high_risk_action_blocking,
    _check_targeted_recapture,
    _check_unstable_usage_trace,
    _fetch_popup_action_trace_artifacts,
    _scope_scenario_checks,
    _wait_and_release_targeted_recapture_driver,
    build_batch_abort_result,
    build_controlled_driver_block_result,
    build_task_payload,
    inspect_fixture_capabilities,
    resolve_target_url,
    run_scenario,
    select_scenarios,
    should_abort_batch,
    validate_base_url,
    validate_fixture_capabilities,
    write_scenario_snapshots,
)


def _scenario(**overrides: str) -> dict[str, str]:
    row = {
        "scenario_id": "W46-H5-001",
        "scenario_name": "普通首页直达",
        "priority": "P0",
        "target_category": "mainline",
        "product_code": "ALIOTH-H5-E2E-HOME",
        "target_url": "https://example.test/h5/home?seed=1",
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": "1",
        "expected_surface_phases": "home",
        "expected_page_types": "home",
        "expected_rule_scope": "home",
        "required_artifacts": "screenshot,visible_text,dom_summary",
        "pass_criteria": "任务完成且报告可查询",
        "risk_notes": "不得只凭任务 done 判定通过",
    }
    row.update(overrides)
    return row


def test_select_scenarios_filters_and_rejects_unknown_ids() -> None:
    rows = [
        _scenario(),
        _scenario(
            scenario_id="W46-H5-101",
            priority="P1",
            target_category="recovery",
        ),
    ]

    assert select_scenarios(rows, priorities={"P1"}) == [rows[1]]
    assert select_scenarios(rows, categories={"mainline"}) == [rows[0]]
    with pytest.raises(ValueError, match="未知场景编号"):
        select_scenarios(rows, scenario_ids={"W46-H5-999"})


def test_batch_aborts_after_runtime_error_or_systemic_gate_failure() -> None:
    assert should_abort_batch(
        {
            "scenario_id": "W46-H5-001",
            "passed": False,
            "error": "API unavailable",
        }
    )
    assert should_abort_batch(
        {
            "scenario_id": "W46-H5-017",
            "passed": False,
            "checks": [],
        }
    )
    assert not should_abort_batch(
        {
            "scenario_id": "W46-H5-001",
            "passed": False,
            "checks": [],
        }
    )


def test_batch_abort_result_records_unexecuted_scenario() -> None:
    result = build_batch_abort_result(
        _scenario(scenario_id="W46-H5-002"),
        target_url="http://127.0.0.1:9080/h5/page",
        blocker={
            "scenario_id": "W46-H5-017",
            "error": "Probe Work 发布合同不满足",
        },
    )

    assert result["acceptance_status"] == "not_executed"
    assert result["error_code"] == "BATCH_ABORTED_AFTER_SYSTEMIC_FAILURE"
    assert result["blocked_by_scenario_id"] == "W46-H5-017"
    assert "未创建验收任务" in result["message"]


def test_resolve_target_url_requires_explicit_placeholder_override() -> None:
    row = _scenario()

    with pytest.raises(ValueError, match="使用占位地址"):
        resolve_target_url(row, target_map={}, fixture_base_url=None)

    assert resolve_target_url(
        row,
        target_map={},
        fixture_base_url="http://127.0.0.1:9080/fixtures",
    ) == "http://127.0.0.1:9080/fixtures/h5/home?seed=1"
    assert resolve_target_url(
        row,
        target_map={row["scenario_id"]: "https://h5.example.com/home"},
        fixture_base_url=None,
    ) == "https://h5.example.com/home"


def test_capture_retry_fixture_url_isolated_by_batch() -> None:
    assert _bind_fixture_attempt_key(
        scenario_id="W46-H5-101",
        target_url="http://127.0.0.1:9080/h5/capture-retry?seed=1",
        batch_id="batch-1",
        fixture_managed=True,
    ) == "http://127.0.0.1:9080/h5/capture-retry?seed=1&fixture_run=batch-1"
    assert _bind_fixture_attempt_key(
        scenario_id="W46-H5-101",
        target_url="https://real.example.com",
        batch_id="batch-1",
        fixture_managed=False,
    ) == "https://real.example.com"


def test_targeted_recapture_fixture_url_isolated_by_batch() -> None:
    assert _bind_fixture_attempt_key(
        scenario_id="W46-H5-017",
        target_url="http://127.0.0.1:9080/h5/recapture",
        batch_id="batch-017",
        fixture_managed=True,
    ) == "http://127.0.0.1:9080/h5/recapture?fixture_run=batch-017"


def test_targeted_recapture_driver_waits_for_formal_interaction_request() -> None:
    calls: list[dict[str, Any]] = []
    responses = iter(
        [
            {"items": []},
            {"status": "running", "run_id": "run-017"},
            {"status": "running"},
            {
                "items": [
                    {
                        "rule_code": "alioth_12",
                        "gap": {
                            "overlay_phase": "after_close",
                            "requested_request": {
                                "session_dependency": "active_session",
                            }
                        },
                        "route_plan": {
                            "state": "已规划出路线",
                            "request_status": "running",
                        },
                    }
                ]
            },
            {
                "driver": "targeted_recapture_driver",
                "fixture_run": "batch-017",
                "released": True,
            },
        ]
    )

    def request(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return next(responses)

    result = _wait_and_release_targeted_recapture_driver(
        base_url="http://127.0.0.1:8000",
        task_id="task-017",
        run_id="run-017",
        target_url="http://127.0.0.1:9080/h5/recapture?fixture_run=batch-017",
        access_token="token",
        deadline=10.0,
        poll_interval_seconds=0,
        request=request,
        sleep=lambda _seconds: None,
        monotonic=iter([0.0, 1.0]).__next__,
    )

    assert result["released"] is True
    assert [call["method"] for call in calls] == ["GET", "GET", "GET", "GET", "POST"]
    assert calls[-1]["url"].endswith(
        "/_drivers/targeted-recapture/release?fixture_run=batch-017"
    )


def test_targeted_recapture_driver_stops_when_run_failed() -> None:
    responses = iter(
        [
            {"items": []},
            {"status": "running", "run_id": "run-017"},
            {
                "status": "failed",
                "error_message": "H5 Session 关闭前补证请求未排空",
            },
        ]
    )

    with pytest.raises(RuntimeError, match="运行已终止.*补证请求未排空"):
        _wait_and_release_targeted_recapture_driver(
            base_url="http://127.0.0.1:8000",
            task_id="task-017",
            run_id="run-017",
            target_url=(
                "http://127.0.0.1:9080/h5/recapture?fixture_run=batch-017"
            ),
            access_token="token",
            deadline=10.0,
            poll_interval_seconds=0,
            request=lambda **_kwargs: next(responses),
            sleep=lambda _seconds: None,
            monotonic=lambda: 0.0,
        )


def test_build_task_payload_preserves_scenario_contract() -> None:
    payload = build_task_payload(
        _scenario(expected_page_types="home,content_list"),
        target_url="http://127.0.0.1:9080/h5/home",
        batch_id="batch-1",
    )

    assert payload["target_platform"] == "h5"
    assert payload["execution_mode"] == "full_inspection"
    assert payload["required_page_types"] == ["home", "content_list"]
    assert payload["product"]["product_code"] == "ALIOTH-H5-E2E-HOME"
    assert payload["task_name"].startswith("[H5验收][batch-1] W46-H5-001")


def test_build_task_payload_separates_lifecycle_phases_and_deduplicates_journey() -> None:
    payload = build_task_payload(
        _scenario(
            expected_page_types="line_loading,startup_ad,home",
            expected_surface_phases="line_loading,startup_ad,home,popup_chain,home",
        ),
        target_url="http://127.0.0.1:9080/h5/line-startup-popup",
        batch_id="batch-1",
    )

    assert payload["required_page_types"] == ["home"]
    assert payload["journey_steps"] == [
        "line_loading",
        "startup_ad",
        "home",
        "popup_chain",
    ]


def test_expected_page_types_require_covered_business_pages() -> None:
    row = _scenario(expected_page_types="startup_ad,home,video_detail")
    timeline = {
        "nodes": [
            {"page_type": "startup_ad"},
            {"page_type": "home"},
            {"page_type": "video_detail"},
        ],
        "h5_assessments": [
            {"page_type": "home", "status": "covered"},
            {"page_type": "video_detail", "status": "evidence_insufficient"},
        ],
        "h5_coverage": {
            "covered_page_types": ["home"],
            "missing_page_types": ["video_detail"],
        },
    }

    check = _check_expected_page_types(row, timeline)

    assert check.passed is False
    assert "未形成可信覆盖=video_detail" in check.message


def test_expected_page_types_accept_covered_business_and_observed_lifecycle() -> None:
    check = _check_expected_page_types(
        _scenario(expected_page_types="startup_ad,home,video_detail"),
        {
            "nodes": [
                {"page_type": "startup_ad"},
                {"page_type": "home"},
                {"page_type": "video_detail"},
            ],
            "h5_assessments": [
                {"page_type": "home", "status": "covered"},
                {"page_type": "video_detail", "status": "covered"},
            ],
            "h5_coverage": {
                "covered_page_types": ["home", "video_detail"],
                "missing_page_types": [],
            },
        },
    )

    assert check.passed is True


def test_high_risk_action_check_requires_detection_without_execution() -> None:
    timeline = {
        "h5_assessments": [
            {
                "detection_result": {
                    "risk_flags": [
                        "high_risk_payment_entry",
                        "high_risk_publish_entry",
                        "high_risk_upload_entry",
                    ]
                }
            }
        ],
        "exploration_actions": [
            {
                "action": "navigate",
                "execution_status": "completed",
                "requested_source_capture_id": None,
            }
        ],
    }

    checks = _check_high_risk_action_blocking(timeline)

    assert [check.code for check in checks] == [
        "HIGH_RISK_ENTRIES_RECOGNIZED",
        "HIGH_RISK_ACTIONS_BLOCKED",
    ]
    assert all(check.passed for check in checks)


def test_targeted_recapture_check_requires_satisfied_request_and_probe_capture() -> None:
    checks = _check_targeted_recapture(
        {
            "evidence_gaps": {
                "total": 1,
                "items": [
                    {
                        "route_plan": {
                            "state": "已规划出路线",
                            "request_status": "satisfied",
                        }
                    }
                ],
            },
            "closure_outcome": {
                "supplement_request_counts": {"satisfied": 1},
            },
            "timeline": {
                "nodes": [
                    {
                        "capture_role": "probe_result_replay",
                        "page_capture_id": "capture-probe-result",
                    }
                ]
            },
        }
    )

    assert [check.code for check in checks] == [
        "TARGETED_RECAPTURE_REQUEST",
        "TARGETED_RECAPTURE_PROBE_CAPTURE",
        "TARGETED_RECAPTURE_SETTLED",
    ]
    assert all(check.passed for check in checks)


def test_targeted_recapture_check_rejects_open_request_without_probe_capture() -> None:
    checks = _check_targeted_recapture(
        {
            "evidence_gaps": {
                "total": 1,
                "items": [
                    {
                        "route_plan": {
                            "state": "已规划出路线",
                            "request_status": "running",
                        }
                    }
                ],
            },
            "closure_outcome": {
                "supplement_request_counts": {"running": 1},
            },
            "timeline": {"nodes": []},
        }
    )

    assert not any(check.passed for check in checks)


def test_targeted_recapture_non_strict_mode_scopes_out_formal_report_gate() -> None:
    checks = [
        acceptance_matrix.AcceptanceCheck("REPORT_READY", False, "报告仍为预览"),
        acceptance_matrix.AcceptanceCheck("RULE_EVIDENCE", True, "证据完整"),
    ]

    scoped = _scope_scenario_checks(
        scenario_id="W46-H5-017",
        checks=checks,
        require_business_completion=False,
    )

    assert [check.code for check in scoped] == ["RULE_EVIDENCE"]


def test_targeted_recapture_strict_mode_keeps_formal_report_gate() -> None:
    checks = [
        acceptance_matrix.AcceptanceCheck("REPORT_READY", False, "报告仍为预览")
    ]

    scoped = _scope_scenario_checks(
        scenario_id="W46-H5-017",
        checks=checks,
        require_business_completion=True,
    )

    assert scoped == checks


def test_popup_trace_fetch_uses_ticketed_evidence_endpoint(monkeypatch) -> None:
    requested_urls: list[str] = []

    class Headers:
        @staticmethod
        def get_content_type() -> str:
            return "application/json"

    class Response:
        headers = Headers()

        @staticmethod
        def read() -> bytes:
            return b'[{"action":"click","obstruction_chain_index":1}]'

        @staticmethod
        def close() -> None:
            return None

    def fake_urlopen(url: str, timeout: int) -> Response:
        requested_urls.append(url)
        assert timeout == 30
        return Response()

    monkeypatch.setattr(acceptance_matrix.urllib.request, "urlopen", fake_urlopen)

    traces = _fetch_popup_action_trace_artifacts(
        base_url="http://127.0.0.1:8000",
        report={
            "page_exploration": {
                "page_results": [
                    {
                        "page_capture_id": "capture-home",
                        "artifact_ids": ["artifact-trace"],
                    }
                ],
                "action_results": [
                    {
                        "result_page_capture_id": "capture-home",
                        "parameters": {"target_mode": "popup_dismissal"},
                    }
                ],
            }
        },
        access_token="access-token",
        request=lambda **_kwargs: {"url": "/evidence/artifact-trace/file?ticket=signed"},
    )

    assert traces == {
        "artifact-trace": [{"action": "click", "obstruction_chain_index": 1}]
    }
    assert requested_urls == [
        "http://127.0.0.1:8000/api/v1/evidence/artifact-trace/file?ticket=signed"
    ]


def test_validate_base_url_defaults_to_loopback_only() -> None:
    assert validate_base_url(
        "http://127.0.0.1:8000/", allow_non_loopback=False
    ) == "http://127.0.0.1:8000"
    with pytest.raises(ValueError, match="allow-non-loopback"):
        validate_base_url("http://192.0.2.10", allow_non_loopback=False)
    assert validate_base_url(
        "https://qa.example.com", allow_non_loopback=True
    ) == "https://qa.example.com"


def test_validate_fixture_capabilities_rejects_missing_controlled_driver() -> None:
    manifest = {
        "schema_version": "h5-acceptance-fixture-manifest.v1",
        "fixtures": {"W46-H5-001": {"path": "/h5/home"}},
        "requires_controlled_driver": {
            "W46-H5-103": "browser_crash_driver",
        },
    }

    assert validate_fixture_capabilities(
        fixture_base_url="http://127.0.0.1:9080",
        scenario_ids={"W46-H5-001"},
        request=lambda **_kwargs: manifest,
    ) == manifest
    with pytest.raises(ValueError, match="browser_crash_driver"):
        validate_fixture_capabilities(
            fixture_base_url="http://127.0.0.1:9080",
            scenario_ids={"W46-H5-001", "W46-H5-103"},
            request=lambda **_kwargs: manifest,
        )


def test_inspect_fixture_capabilities_separates_external_driver_without_fake_run() -> None:
    manifest = {
        "schema_version": "h5-acceptance-fixture-manifest.v1",
        "fixtures": {"W46-H5-001": {"path": "/h5/home"}},
        "requires_controlled_driver": {
            "W46-H5-017": "targeted_recapture_driver",
        },
    }

    loaded, blocked = inspect_fixture_capabilities(
        fixture_base_url="http://127.0.0.1:9080",
        scenario_ids={"W46-H5-001", "W46-H5-017"},
        request=lambda **_kwargs: manifest,
    )
    result = build_controlled_driver_block_result(
        _scenario(
            scenario_id="W46-H5-017",
            scenario_name="证据缺口定向补采",
        ),
        target_url="http://127.0.0.1:9080/h5/recapture",
        required_driver=blocked["W46-H5-017"],
    )

    assert loaded is manifest
    assert blocked == {"W46-H5-017": "targeted_recapture_driver"}
    assert result == {
        "scenario_id": "W46-H5-017",
        "scenario_name": "证据缺口定向补采",
        "target_url": "http://127.0.0.1:9080/h5/recapture",
        "acceptance_status": "blocked",
        "passed": False,
        "error_code": "CONTROLLED_DRIVER_REQUIRED",
        "required_driver": "targeted_recapture_driver",
        "message": "缺少外部受控驱动，本场景未创建验收任务",
    }


def test_inspect_fixture_capabilities_rejects_undeclared_missing_scenario() -> None:
    manifest = {
        "schema_version": "h5-acceptance-fixture-manifest.v1",
        "fixtures": {},
        "requires_controlled_driver": {},
    }

    with pytest.raises(ValueError, match="未声明以下场景能力"):
        inspect_fixture_capabilities(
            fixture_base_url="http://127.0.0.1:9080",
            scenario_ids={"W46-H5-001"},
            request=lambda **_kwargs: manifest,
        )


def test_inspect_fixture_capabilities_rejects_empty_controlled_driver() -> None:
    manifest = {
        "schema_version": "h5-acceptance-fixture-manifest.v1",
        "fixtures": {},
        "requires_controlled_driver": {"W46-H5-017": "  "},
    }

    with pytest.raises(ValueError, match="controlled driver 不能为空"):
        inspect_fixture_capabilities(
            fixture_base_url="http://127.0.0.1:9080",
            scenario_ids={"W46-H5-017"},
            request=lambda **_kwargs: manifest,
        )


def test_main_runs_available_fixture_and_records_controlled_driver_block(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rows = [
        _scenario(),
        _scenario(
            scenario_id="W46-H5-017",
            scenario_name="证据缺口定向补采",
            target_url="https://example.test/h5/recapture",
        ),
    ]
    executed: list[str] = []
    strict_modes: list[bool] = []
    manifest = {
        "schema_version": "h5-acceptance-fixture-manifest.v1",
        "fixtures": {"W46-H5-001": {"path": "/h5/home"}},
        "requires_controlled_driver": {
            "W46-H5-017": "targeted_recapture_driver",
        },
    }

    monkeypatch.setattr(acceptance_matrix, "load_scenarios", lambda _path: rows)
    monkeypatch.setattr(acceptance_matrix, "validate_scenarios", lambda _rows: [])
    monkeypatch.setattr(
        acceptance_matrix,
        "request_json",
        lambda **kwargs: manifest
        if kwargs["url"].endswith("/_manifest")
        else pytest.fail(f"不应调用业务 API：{kwargs}"),
    )

    def fake_run_scenario(
        row: dict[str, str],
        **kwargs: Any,
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        executed.append(row["scenario_id"])
        strict_modes.append(kwargs["require_business_completion"])
        return (
            {
                "scenario_id": row["scenario_id"],
                "scenario_name": row["scenario_name"],
                "passed": True,
            },
            {"task": {"task_id": "task-1"}},
        )

    monkeypatch.setattr(acceptance_matrix, "run_scenario", fake_run_scenario)

    exit_code = acceptance_matrix.main(
        [
            "--base-url",
            "http://127.0.0.1:8000",
            "--fixture-base-url",
            "http://127.0.0.1:9080",
            "--batch-id",
            "mixed-batch",
            "--output-dir",
            str(tmp_path),
            "--require-business-completion",
        ]
    )

    batch = json.loads((tmp_path / "mixed-batch" / "batch.json").read_text(encoding="utf-8"))
    assert exit_code == 1
    assert executed == ["W46-H5-001"]
    assert strict_modes == [True]
    assert batch["passed_count"] == 1
    assert batch["failed_count"] == 0
    assert batch["blocked_count"] == 1
    assert batch["executed_count"] == 1
    assert batch["results"][1]["error_code"] == "CONTROLLED_DRIVER_REQUIRED"


def test_run_scenario_creates_waits_fetches_and_verifies() -> None:
    calls: list[dict[str, Any]] = []
    report_attempts = 0
    responses = {
        "/api/v1/workstation/tasks/task-1": {
            "task_id": "task-1",
            "run_id": "run-1",
            "status": "done",
            "report_status": "ready",
        },
        "/api/v1/agents/runs/run-1": {
            "run_id": "run-1",
            "status": "completed",
            "usage": {"total_tokens": 120, "total_cost": 0.01},
        },
        "/api/v1/tasks/task-1/rule-executions?agent_run_id=run-1": {
            "items": [
                {
                    "rule_code": "alioth_1",
                    "execution_status": "pass",
                    "source": "judge",
                    "page_capture_id": "capture-1",
                    "evidence_artifact_ids": ["artifact-1"],
                }
            ]
        },
        "/api/v1/workstation/reports/task-1?agent_run_id=run-1": {
            "report_stage": "final",
            "summary": {"overall_status": "pass"},
            "page_exploration": {
                "tracking_available": True,
                "complete": True,
                "quality_status": "complete",
                "requires_review": False,
                "terminal_reason": None,
                "page_total": 1,
            },
        },
        "/api/v1/workstation/tasks/task-1/page-timeline": {
            "execution_mode": "full_inspection",
            "exploration_summary": {
                "complete": True,
                "quality_status": "complete",
                "requires_review": False,
                "terminal_reason": None,
            },
            "terminal_reason": None,
            "nodes": [{"page_capture_id": "capture-1", "page_type": "home"}],
            "h5_assessments": [{"page_capture_id": "capture-1", "page_type": "home"}],
            "h5_coverage": {
                "authority_source": "h5_page_type_assessment",
                "revision": 1,
                "payload_hash": "a" * 64,
                "completion_ready": True,
                "covered_page_types": ["home"],
                "missing_page_types": [],
            },
            "h5_evidence_inventory": {
                "items": [
                    {
                        "inventory_item_id": "inventory-1",
                        "status": "pass",
                        "authority_source": "pass_manifest",
                        "before_manifest_id": "manifest-before",
                        "after_manifest_id": "manifest-after",
                    }
                ]
            },
            "full_inspection_closure": {
                "nonterminal_inspection_count": 0,
                "gate_passed": True,
                "publication_projection": {},
            },
        },
        "/api/v1/workstation/tasks/task-1/inspection-work-items": {
            "summary": {"nonterminal_count": 0}
        },
        "/api/v1/workstation/tasks/task-1/evidence-gaps": {
            "total": 0,
            "items": [],
        },
        "/api/v1/workstation/tasks/task-1/closure-outcome": {
            "supplement_request_counts": {},
        },
    }

    def fake_request(**kwargs: Any) -> dict[str, Any]:
        nonlocal report_attempts
        calls.append(kwargs)
        path = kwargs["url"].removeprefix("http://127.0.0.1:8000")
        if kwargs["method"] == "POST":
            return {"task_id": "task-1", "run_id": "run-1", "status": "queued"}
        if path == "/api/v1/workstation/reports/task-1?agent_run_id=run-1":
            report_attempts += 1
            if report_attempts == 1:
                raise RuntimeError("报告投影暂不可用")
        return responses[path]

    result, snapshots = run_scenario(
        _scenario(),
        base_url="http://127.0.0.1:8000",
        access_token="token",
        target_url="http://127.0.0.1:9080/h5/home",
        batch_id="batch-1",
        timeout_seconds=1,
        projection_timeout_seconds=1,
        poll_interval_seconds=0,
        request=fake_request,
        sleep=lambda _seconds: None,
    )

    assert result["passed"] is True
    assert result["task_id"] == "task-1"
    assert set(snapshots) == {
        "task",
        "run",
        "rules",
        "report",
        "timeline",
        "inspection_work",
        "evidence_gaps",
        "closure_outcome",
    }
    assert calls[0]["method"] == "POST"
    assert calls[0]["idempotency_key"].startswith("h5-acceptance-")
    assert calls[0]["payload"]["target_url"] == "http://127.0.0.1:9080/h5/home"
    assert report_attempts == 2


def test_run_cancellation_scenario_waits_for_fact_and_verifies_quiescence() -> None:
    calls: list[dict[str, Any]] = []
    cancellation_requested = False

    def timeline(*, terminal: bool) -> dict[str, Any]:
        return {
            "nodes": [{"page_capture_id": "capture-1", "page_type": "home"}],
            "exploration_actions": [{"action_execution_id": "action-1"}],
            "surface_session_summary": {
                "total_count": 1,
                "terminal_count": 1 if terminal else 0,
                "nonterminal_count": 0 if terminal else 1,
                "reference_count": 1,
                "nonterminal_reference_count": 0 if terminal else 1,
                "status_counts": {"closed" if terminal else "active": 1},
            },
        }

    def fake_request(**kwargs: Any) -> dict[str, Any]:
        nonlocal cancellation_requested
        calls.append(kwargs)
        path = kwargs["url"].removeprefix("http://127.0.0.1:8000")
        if kwargs["method"] == "POST" and path.endswith("/cancel"):
            cancellation_requested = True
            return {
                "task_id": "task-1",
                "run_id": "run-1",
                "status": "canceling",
                "message": "取消请求已提交",
            }
        if kwargs["method"] == "POST":
            return {"task_id": "task-1", "run_id": "run-1", "status": "queued"}
        terminal = cancellation_requested
        if path == "/api/v1/workstation/tasks/task-1":
            return {
                "task_id": "task-1",
                "run_id": "run-1",
                "status": "canceled" if terminal else "running",
            }
        if path == "/api/v1/agents/runs/run-1":
            return {
                "run_id": "run-1",
                "status": "canceled" if terminal else "running",
                "termination_reason": "canceled" if terminal else None,
                "step_count": 1,
                "action_trace": [{"step": 1, "tool_name": "page_capture_tool"}],
            }
        if path == "/api/v1/workstation/tasks/task-1/page-timeline":
            return timeline(terminal=terminal)
        if path == "/api/v1/workstation/tasks/task-1/inspection-work-items":
            return {"summary": {"nonterminal_count": 0}}
        raise AssertionError(path)

    result, snapshots = run_scenario(
        _scenario(
            scenario_id="W46-H5-016",
            scenario_name="任务取消与资源释放",
            target_url="https://example.test/h5/cancel",
        ),
        base_url="http://127.0.0.1:8000",
        access_token="token",
        target_url="http://127.0.0.1:9080/h5/cancel",
        batch_id="batch-1",
        timeout_seconds=1,
        projection_timeout_seconds=1,
        poll_interval_seconds=0,
        quiescence_seconds=0,
        request=fake_request,
        sleep=lambda _seconds: None,
    )

    assert result["passed"] is True
    assert result["task_status"] == "canceled"
    assert {check["code"] for check in result["checks"]} == {
        "CANCEL_TERMINAL",
        "CANCEL_FACTS_PRESERVED",
        "CANCEL_NO_POST_TERMINAL_GROWTH",
        "CANCEL_RESOURCES_RELEASED",
        "CANCEL_NO_PENDING_INSPECTION",
    }
    assert "report" not in snapshots
    cancel_call = next(
        call for call in calls if call["method"] == "POST" and call["url"].endswith("/cancel")
    )
    assert cancel_call["idempotency_key"].startswith("h5-acceptance-")


def test_capture_retry_check_requires_bounded_persisted_retry_and_evidence() -> None:
    checks = _check_capture_retry(
        {
            "nodes": [
                {
                    "page_capture_id": "capture-1",
                    "screenshot_artifact_id": "artifact-1",
                    "navigation_retry": {
                        "schema_version": "navigation_retry.v1",
                        "max_attempts": 2,
                        "attempt_count": 2,
                        "retry_count": 1,
                        "retry_reasons": ["err_empty_response"],
                        "outcome": "succeeded_after_retry",
                    },
                }
            ]
        }
    )

    assert [check.code for check in checks] == [
        "CAPTURE_RETRY_AUDIT",
        "CAPTURE_RETRY_RECOVERED_CAPTURE",
    ]
    assert all(check.passed for check in checks)


def test_browser_crash_check_requires_attempt_lineage_and_recovered_capture() -> None:
    checks = _check_browser_crash_recovery(
        {
            "browser_recovery": {
                "attempts": [
                    {
                        "recovery_attempt_count": 1,
                        "status": "completed",
                        "error_code": "PAGE_CAPTURE_SESSION_CRASHED",
                        "reason_code": None,
                    }
                ],
                "recoveries": [
                    {
                        "source_session_id": "h5:run-1",
                        "target_session_id": "h5:run-1:recovery:0123456789abcdefabcd",
                        "equivalent": True,
                        "reason_code": None,
                        "failure_stage": "action",
                        "original_source_capture_id": "capture-before",
                        "failed_action_execution_id": "action-failed",
                        "restoration_action_execution_id": "action-recovery",
                        "rebaseline_after_capture_id": "capture-recovered",
                    }
                ],
            },
            "nodes": [
                {
                    "page_capture_id": "capture-before",
                    "screenshot_artifact_id": "artifact-before",
                },
                {
                    "page_capture_id": "capture-recovered",
                    "screenshot_artifact_id": "artifact-recovered",
                }
            ],
            "exploration_actions": [
                {"action_execution_id": "action-failed"},
                {"action_execution_id": "action-recovery"},
            ],
        }
    )

    assert [check.code for check in checks] == [
        "BROWSER_RECOVERY_AUDIT",
        "BROWSER_RECOVERY_SESSION_LINEAGE",
        "BROWSER_RECOVERY_CAPTURE",
        "BROWSER_RECOVERY_TRACE_PRESERVED",
    ]
    assert all(check.passed for check in checks)


def test_browser_crash_check_rejects_status_without_persisted_recovery_facts() -> None:
    checks = _check_browser_crash_recovery(
        {
            "browser_recovery": {
                "status": "recovered",
                "attempts": [],
                "recoveries": [],
            },
            "nodes": [],
        }
    )

    assert not any(check.passed for check in checks)


def test_evidence_failure_check_requires_preflight_gate_and_report_alignment() -> None:
    audit = {
        "schema_version": "h5-evidence-preflight.v1",
        "status": "evidence_insufficient",
        "blocked_item_count": 1,
        "reason_codes": ["artifact_object_missing"],
        "publication_allowed": False,
        "invalid_evidence_publication_blocked": True,
        "improperly_published_inventory_item_ids": [],
        "improper_rule_result_ids": [],
        "gate_passed": False,
        "gate_block_reasons": ["evidence_insufficient"],
    }
    checks = _check_evidence_failure(
        {
            "timeline": {"h5_evidence_preflight": audit},
            "report": {
                "page_exploration": {
                    "complete": False,
                    "quality_status": "needs_review",
                    "requires_review": True,
                    "terminal_reason": "evidence_insufficient",
                    "evidence_preflight": audit,
                }
            },
        }
    )

    assert [check.code for check in checks] == [
        "EVIDENCE_PREFLIGHT",
        "INVALID_EVIDENCE_NOT_PUBLISHED",
        "EVIDENCE_GATE_BLOCKED",
        "EVIDENCE_REPORT_VISIBLE",
    ]
    assert all(check.passed for check in checks)


def test_evidence_failure_check_rejects_invalid_rule_publication() -> None:
    checks = _check_evidence_failure(
        {
            "timeline": {
                "h5_evidence_preflight": {
                    "schema_version": "h5-evidence-preflight.v1",
                    "status": "evidence_insufficient",
                    "blocked_item_count": 1,
                    "reason_codes": ["artifact_object_missing"],
                    "publication_allowed": False,
                    "invalid_evidence_publication_blocked": False,
                    "improperly_published_inventory_item_ids": ["inventory-1"],
                    "improper_rule_result_ids": ["execution-1"],
                    "gate_passed": True,
                    "gate_block_reasons": [],
                }
            },
            "report": {"page_exploration": {}},
        }
    )

    assert checks[0].passed is True
    assert not all(check.passed for check in checks)


def test_model_contract_failure_check_requires_audited_fallback_capture() -> None:
    from scripts.run_h5_acceptance_matrix import _check_model_contract_failure

    checks = _check_model_contract_failure(
        {
            "h5_model_contract": {
                "schema_version": "h5-model-contract-audit.v1",
                "status": "degraded",
                "fallback_action_count": 1,
                "error_work_count": 0,
                "reason_codes": ["H5_EXPLORER_OUTPUT_INVALID"],
                "fallback_actions": [
                    {
                        "action_execution_id": "action-1",
                        "execution_status": "completed",
                        "result_page_capture_id": "capture-2",
                        "reason_code": "H5_EXPLORER_OUTPUT_INVALID",
                    }
                ],
            },
            "nodes": [
                {
                    "page_capture_id": "capture-2",
                    "screenshot_artifact_id": "artifact-2",
                }
            ],
        }
    )

    assert [check.code for check in checks] == [
        "MODEL_CONTRACT_AUDIT",
        "MODEL_CONTRACT_REASON_PRESERVED",
        "MODEL_CONTRACT_FALLBACK_EVIDENCE",
    ]
    assert all(check.passed for check in checks)


def test_unstable_page_check_requires_bounded_review_without_normal_rules() -> None:
    from scripts.run_h5_acceptance_matrix import _check_unstable_terminal

    checks = _check_unstable_terminal(
        {
            "task": {"status": "review"},
            "run": {"status": "failed", "termination_reason": "incomplete"},
            "rules": {
                "items": [
                    {
                        "rule_code": "rule-1",
                        "execution_status": "evidence_insufficient",
                    }
                ]
            },
            "report": {
                "page_exploration": {
                    "complete": False,
                    "quality_status": "needs_review",
                    "terminal_reason": "work_bounded",
                }
            },
            "timeline": {
                "terminal_reason": "work_bounded",
                "exploration_summary": {
                    "complete": False,
                    "quality_status": "needs_review",
                },
                "nodes": [
                    {
                        "page_capture_id": "capture-1",
                        "screenshot_artifact_id": "artifact-1",
                        "page_settle_diagnostics": {
                            "outcome": "blocked",
                            "blocking_reasons": ["blocking_loader_visible"],
                            "dynamic_reasons": ["dom_continues_changing"],
                            "sample_count": 12,
                            "stable_sample_count": 0,
                        },
                    }
                ],
            },
        }
    )

    assert [check.code for check in checks] == [
        "UNSTABLE_TERMINAL_ALIGNMENT",
        "UNSTABLE_BUDGET_AUDIT",
        "UNSTABLE_NO_FAKE_RULE_RESULT",
    ]
    assert all(check.passed for check in checks)


def test_unstable_page_check_rejects_done_task_and_normal_rule_result() -> None:
    from scripts.run_h5_acceptance_matrix import _check_unstable_terminal

    checks = _check_unstable_terminal(
        {
            "task": {"status": "done"},
            "run": {"status": "completed", "termination_reason": "completed"},
            "rules": {"items": [{"execution_status": "pass"}]},
            "report": {"page_exploration": {"complete": True}},
            "timeline": {
                "terminal_reason": None,
                "exploration_summary": {"complete": True},
                "nodes": [],
            },
        }
    )

    assert not any(check.passed for check in checks)


def test_unstable_page_allows_zero_cost_only_for_pre_llm_deterministic_block() -> None:
    check = _check_unstable_usage_trace(
        {
            "usage": {"h5_evidence_insufficient_count": 1},
            "action_trace": [
                {
                    "tool_name": "h5_exploration_work",
                    "status": "skipped",
                }
            ],
        }
    )
    invalid = _check_unstable_usage_trace(
        {
            "usage": {},
            "action_trace": [{"tool_name": "page_detection_tool", "status": "ok"}],
        }
    )

    assert check.passed is True
    assert invalid.passed is False


def test_run_cancellation_scenario_rejects_early_terminal_task() -> None:
    def fake_request(**kwargs: Any) -> dict[str, Any]:
        if kwargs["method"] == "POST":
            return {"task_id": "task-1", "run_id": "run-1", "status": "queued"}
        return {"task_id": "task-1", "run_id": "run-1", "status": "done"}

    with pytest.raises(RuntimeError, match="取消前提前进入终态"):
        run_scenario(
            _scenario(scenario_id="W46-H5-016"),
            base_url="http://127.0.0.1:8000",
            access_token="token",
            target_url="http://127.0.0.1:9080/h5/cancel",
            batch_id="batch-1",
            timeout_seconds=1,
            projection_timeout_seconds=1,
            poll_interval_seconds=0,
            request=fake_request,
            sleep=lambda _seconds: None,
        )


def test_write_scenario_snapshots_uses_auditable_relative_paths(tmp_path: Path) -> None:
    paths = write_scenario_snapshots(
        tmp_path,
        scenario_id="W46-H5-001",
        snapshots={"task": {"task_id": "task-1"}},
    )

    assert paths == {"task": "W46-H5-001/task.json"}
    payload = json.loads((tmp_path / paths["task"]).read_text(encoding="utf-8"))
    assert payload == {"task_id": "task-1"}
