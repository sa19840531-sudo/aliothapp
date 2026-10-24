from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools.registry import PermissionLevel, default_registry
from services.api.app.agents.tools.report_tools import generate_report_for_current_run
from services.api.app.application.report_generation import (
    ReportGenerationService,
    ReportGenerationTargetNotFoundError,
)
from services.api.app.application.task_query import (
    EvidenceArtifactSummary,
    RuleExecutionCompleteness,
    RuleExecutionSummary,
    TaskDetail,
    TaskTargetPageCoverage,
    UnresolvedCandidateRule,
)


class FakeTaskQueryService:
    def __init__(
        self,
        *,
        task: TaskDetail | None,
        executions: list[RuleExecutionSummary],
        completeness: RuleExecutionCompleteness | None = None,
        evidence_artifacts: list[EvidenceArtifactSummary] | None = None,
        target_page_coverage: list[TaskTargetPageCoverage] | None = None,
    ) -> None:
        self.task = task
        self.executions = executions
        unresolved_codes = tuple(
            item.rule_code
            for item in executions
            if item.source == "bootstrap" and item.execution_status == "evidence_insufficient"
        )
        completed_count = sum(item.source == "agent" for item in executions)
        self.completeness = completeness or RuleExecutionCompleteness(
            candidate_query_count=1,
            candidate_rule_count=completed_count + len(unresolved_codes),
            completed_candidate_count=completed_count,
            unresolved_rule_codes=unresolved_codes,
        )
        self.evidence_artifacts = evidence_artifacts or []
        self.target_page_coverage = target_page_coverage or []
        self.calls: list[tuple[str, str]] = []

    async def get_task_detail(self, task_id: str) -> TaskDetail | None:
        self.calls.append(("get_task_detail", task_id))
        return self.task

    async def list_rule_executions(
        self, task_id: str, *, agent_run_id: str | None = None
    ) -> list[RuleExecutionSummary]:
        self.calls.append(("list_rule_executions", f"{task_id}:{agent_run_id}"))
        return self.executions

    async def list_evidence_artifacts(
        self, task_id: str, *, agent_run_id: str | None = None
    ) -> list[EvidenceArtifactSummary]:
        self.calls.append(("list_evidence_artifacts", f"{task_id}:{agent_run_id}"))
        return self.evidence_artifacts

    async def get_rule_execution_completeness(
        self, *, task_id: str, agent_run_id: str
    ) -> RuleExecutionCompleteness:
        self.calls.append(("get_rule_execution_completeness", f"{task_id}:{agent_run_id}"))
        return self.completeness

    async def list_task_target_page_coverage(
        self, *, task_id: str, agent_run_id: str | None = None
    ) -> list[TaskTargetPageCoverage]:
        self.calls.append(("list_task_target_page_coverage", f"{task_id}:{agent_run_id}"))
        return self.target_page_coverage


class FakeReportGenerationService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def generate_task_report(self, task_id: str, *, agent_run_id: str | None = None) -> dict:
        self.calls.append((task_id, agent_run_id))
        return {"task_id": task_id, "summary": {"total_rules": 0}}


async def test_report_generation_service_builds_structured_task_report() -> None:
    task = _build_task_detail()
    query_service = FakeTaskQueryService(
        task=task,
        executions=[
            _build_rule_execution(
                rule_code="alioth_1",
                status="pass",
                severity="low",
                summary="首页标题正常",
            ),
            _build_rule_execution(
                rule_code="alioth_2",
                status="fail",
                severity="high",
                summary="弹窗关闭按钮不可见",
                problem_desc="用户无法关闭广告弹窗",
            ),
            _build_rule_execution(
                rule_code="alioth_3",
                status="manual_review",
                severity="medium",
                summary="支付按钮结果需人工确认",
            ),
            _build_rule_execution(
                rule_code="alioth_4",
                status="skipped",
                severity="low",
                summary="规则不适用当前页面",
            ),
        ],
    )
    service = ReportGenerationService(
        query_service=query_service,
        clock=lambda: datetime(2026, 7, 9, 9, 30, tzinfo=UTC),
    )

    report = await service.generate_task_report("task-1")

    assert query_service.calls == [
        ("get_task_detail", "task-1"),
        # 按质检规则统一口径：规则结果不分 Run
        ("list_rule_executions", "task-1:None"),
        ("list_evidence_artifacts", "task-1:run-1"),
        ("get_rule_execution_completeness", "task-1:run-1"),
        ("list_task_target_page_coverage", "task-1:run-1"),
    ]
    assert report["report_version"] == "v2"
    assert report["report_stage"] == "preview"
    assert report["is_final"] is False
    assert report["agent_run_id"] == "run-1"
    assert report["generated_at"] == "2026-07-09T09:30:00+00:00"
    assert report["task"]["task_id"] == "task-1"
    assert report["task"]["ruleset_code"] == "alioth_phase1"
    assert report["summary"] == {
        "total_rules": 4,
        "pass": 1,
        "fail": 1,
        "warning": 0,
        "manual_review": 1,
        "evidence_insufficient": 0,
        "skipped": 1,
        "error": 0,
        "attention_required": 2,
        "evaluated_rules": 4,
        "unresolved_candidate_rules": 0,
        "non_candidate_rules": 0,
        "confirmed_not_applicable_rules": 1,
        "coverage_gap_rules": 0,
            "full_rule_pass_rate": 25.0,
            "qc_success_rate": 75.0,
            "pass_rate": 50.0,
            "candidate_execution_coverage": 100.0,
        "technical_execution_complete": True,
        "technical_completion_status": "complete",
        "business_completion_status": "failed",
        "automatic_decision_rules": 2,
        "automatic_decision_rate": 50.0,
        "platform_degraded_rules": 0,
        "platform_degradation_rate": 0.0,
        "human_review_required_rules": 1,
        "evidence_gap_rules": 0,
        "classified_evidence_gap_rules": 0,
        "unclassified_evidence_gap_rules": 0,
        "not_applicable_rules": 1,
        "platform_issue_codes": {},
        "platform_capability_status": "healthy",
        "target_page_total": 0,
        "reached_target_pages": 0,
        "failed_target_pages": 0,
        "blocked_target_pages": 0,
        "not_found_target_pages": 0,
        "pending_target_pages": 0,
        "target_page_coverage_rate": 0.0,
        "platform_degraded_target_pages": 0,
        "platform_target_degradation_rate": 0.0,
        "platform_rule_issue_codes": {},
        "platform_page_issue_codes": {},
        "task_goal_completed": None,
        "task_goal_completion_status": "unconfigured",
        "overall_status": "fail",
        "candidate_query_count": 1,
        "candidate_rule_count": 4,
        "completed_candidate_rules": 4,
        "unresolved_candidate_rule_codes": [],
        "unresolved_candidate_items": [],
        "candidate_execution_complete": True,
    }
    assert report["groups"]["by_status"]["fail"] == ["alioth_2"]
    assert report["groups"]["by_severity"]["high"] == ["alioth_2"]
    assert [item["rule_code"] for item in report["attention_results"]] == [
        "alioth_2",
        "alioth_3",
    ]
    assert report["rule_results"][1]["problem_desc"] == "用户无法关闭广告弹窗"
    assert report["rule_results"][1]["page_capture_id"] == "capture-alioth_2"
    assert report["rule_results"][1]["source"] == "agent"


async def test_h5_report_freezes_rule_evidence_identity() -> None:
    execution = replace(
        _build_rule_execution(
            rule_code="alioth_evidence",
            status="evidence_insufficient",
            severity="medium",
            summary="缺少关闭弹窗后的页面截图",
            missing_evidence=("after_action_screenshot",),
        ),
        evidence_artifact_ids=("artifact-after-action",),
        evidence_input_set_id="evidence-input-set:h5",
    )
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[execution],
        )
    )

    report = await service.generate_task_report("task-1")

    assert report["rule_results"][0]["evidence_artifact_ids"] == [
        "artifact-after-action"
    ]
    assert report["rule_results"][0]["evidence_input_set_id"] == (
        "evidence-input-set:h5"
    )
    assert report["rule_results"][0]["missing_evidence"] == [
        "after_action_screenshot"
    ]


async def test_web_report_does_not_adopt_h5_evidence_identity_extension() -> None:
    execution = replace(
        _build_rule_execution(
            rule_code="alioth_web",
            status="pass",
            severity="low",
            summary="Web 规则通过",
        ),
        evidence_artifact_ids=("artifact-web",),
        evidence_input_set_id="evidence-input-set:web",
    )
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=replace(_build_task_detail(), target_platform="web"),
            executions=[execution],
        )
    )

    report = await service.generate_task_report("task-1")

    assert "evidence_artifact_ids" not in report["rule_results"][0]
    assert "evidence_input_set_id" not in report["rule_results"][0]


async def test_report_generation_exposes_target_page_coverage_ledger() -> None:
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=replace(_build_task_detail(), status="review"),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_1",
                    status="pass",
                    severity="low",
                    summary="已完成规则判定",
                )
            ],
            target_page_coverage=[
                TaskTargetPageCoverage(
                    page_type="home",
                    capture_count=2,
                    explored_action_count=1,
                    failed_action_count=0,
                    blocked_action_count=0,
                    skipped_action_count=0,
                    pending_action_count=0,
                ),
                TaskTargetPageCoverage(
                    page_type="settings",
                    capture_count=0,
                    explored_action_count=0,
                    failed_action_count=1,
                    blocked_action_count=0,
                    skipped_action_count=0,
                    pending_action_count=0,
                    failure_codes=("PAGE_CAPTURE_FAILED",),
                ),
                TaskTargetPageCoverage(
                    page_type="activity",
                    capture_count=0,
                    explored_action_count=0,
                    failed_action_count=0,
                    blocked_action_count=0,
                    skipped_action_count=0,
                    pending_action_count=0,
                ),
            ],
        )
    )

    report = await service.generate_task_report("task-1")

    assert report["summary"]["target_page_total"] == 3
    assert report["summary"]["reached_target_pages"] == 1
    assert report["summary"]["failed_target_pages"] == 1
    assert report["summary"]["not_found_target_pages"] == 1
    assert report["summary"]["target_page_coverage_rate"] == 33.33
    assert report["summary"]["platform_degraded_target_pages"] == 1
    assert report["summary"]["platform_target_degradation_rate"] == 33.33
    assert report["summary"]["platform_capability_status"] == "degraded"
    assert report["summary"]["platform_page_issue_codes"] == {"PAGE_CAPTURE_FAILED": 1}
    assert report["summary"]["task_goal_completed"] is False
    assert report["summary"]["task_goal_completion_status"] == "partial"
    assert report["target_page_coverage"][1] == {
        "page_type": "settings",
        "status": "failed",
        "outcome_class": "platform_degraded",
        "capability_issue_codes": ["PAGE_CAPTURE_FAILED"],
        "capture_count": 0,
        "explored_action_count": 0,
        "failed_action_count": 1,
        "blocked_action_count": 0,
        "skipped_action_count": 0,
        "pending_action_count": 0,
        "failure_codes": ["PAGE_CAPTURE_FAILED"],
    }


async def test_report_generation_separates_platform_degradation_from_human_review() -> None:
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_1",
                    status="manual_review",
                    severity="high",
                    summary="语义规则判定超过独立墙钟预算，未生成自动判定结论。",
                    diagnostic_codes=("semantic_provider_timeout",),
                ),
                _build_rule_execution(
                    rule_code="alioth_2",
                    status="manual_review",
                    severity="medium",
                    summary="页面视觉语义存在歧义，需人工确认。",
                ),
                _build_rule_execution(
                    rule_code="alioth_3",
                    status="evidence_insufficient",
                    severity="medium",
                    summary="缺少点击后的页面状态。",
                ),
            ],
        )
    )

    report = await service.generate_task_report("task-1")

    assert report["summary"]["technical_completion_status"] == "complete"
    assert report["summary"]["business_completion_status"] == "review_required"
    assert report["summary"]["platform_capability_status"] == "degraded"
    assert report["summary"]["platform_degraded_rules"] == 1
    assert report["summary"]["human_review_required_rules"] == 1
    assert report["summary"]["evidence_gap_rules"] == 1
    assert report["summary"]["classified_evidence_gap_rules"] == 0
    assert report["summary"]["unclassified_evidence_gap_rules"] == 1
    assert report["summary"]["platform_issue_codes"] == {"semantic_provider_timeout": 1}
    assert report["rule_results"][0]["outcome_class"] == "platform_degraded"
    assert report["rule_results"][0]["capability_issue_code"] == "semantic_provider_timeout"


async def test_report_generation_service_exposes_unresolved_candidate_coverage() -> None:
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_1",
                    status="pass",
                    severity="low",
                    summary="规则已完成判定",
                ),
                _build_rule_execution(
                    rule_code="alioth_2",
                    status="evidence_insufficient",
                    severity="high",
                    summary="候选规则未完成判定",
                    source="bootstrap",
                ),
                _build_rule_execution(
                    rule_code="alioth_3",
                    status="skipped",
                    severity="low",
                    summary="规则不属于已匹配页面",
                    source="bootstrap",
                ),
            ],
        ),
        clock=lambda: datetime(2026, 7, 9, 9, 30, tzinfo=UTC),
    )

    report = await service.generate_task_report("task-1")

    assert report["summary"]["evaluated_rules"] == 1
    assert report["summary"]["unresolved_candidate_rules"] == 1
    assert report["summary"]["non_candidate_rules"] == 1
    assert report["summary"]["confirmed_not_applicable_rules"] == 0
    assert report["summary"]["coverage_gap_rules"] == 1
    assert report["summary"]["candidate_execution_coverage"] == 50.0
    assert report["summary"]["overall_status"] == "incomplete"
    assert report["rule_results"][2]["outcome_class"] == "coverage_gap"


async def test_report_generation_service_checks_current_run_candidate_completeness() -> None:
    query_service = FakeTaskQueryService(
        task=_build_task_detail(),
        executions=[
            _build_rule_execution(
                rule_code="alioth_5",
                status="skipped",
                severity="medium",
                summary="规则不适用",
            ),
            _build_rule_execution(
                rule_code="alioth_7",
                status="skipped",
                severity="high",
                summary="规则不适用",
            ),
        ],
        completeness=RuleExecutionCompleteness(
            candidate_query_count=1,
            candidate_rule_count=3,
            completed_candidate_count=2,
            unresolved_rule_codes=("alioth_6",),
        ),
    )
    service = ReportGenerationService(query_service=query_service)

    report = await service.generate_task_report("task-1", agent_run_id="run-1")

    assert (
        "get_rule_execution_completeness",
        "task-1:run-1",
    ) in query_service.calls
    assert report["summary"]["candidate_query_count"] == 1
    assert report["summary"]["candidate_rule_count"] == 3
    assert report["summary"]["completed_candidate_rules"] == 2
    assert report["summary"]["unresolved_candidate_rules"] == 1
    assert report["summary"]["unresolved_candidate_rule_codes"] == ["alioth_6"]
    assert report["summary"]["candidate_execution_coverage"] == 66.67
    assert report["summary"]["candidate_execution_complete"] is False


async def test_report_generation_counts_same_rule_gap_on_each_page() -> None:
    query_service = FakeTaskQueryService(
        task=_build_task_detail(),
        executions=[],
        completeness=RuleExecutionCompleteness(
            candidate_query_count=2,
            candidate_rule_count=2,
            completed_candidate_count=0,
            unresolved_rule_codes=("alioth_6", "alioth_6"),
            unresolved_candidate_items=(
                UnresolvedCandidateRule(
                    page_capture_id="capture-1",
                    rule_code="alioth_6",
                ),
                UnresolvedCandidateRule(
                    page_capture_id="capture-2",
                    rule_code="alioth_6",
                ),
            ),
        ),
    )

    report = await ReportGenerationService(query_service=query_service).generate_task_report(
        "task-1", agent_run_id="run-1"
    )

    assert report["summary"]["unresolved_candidate_rules"] == 2
    assert report["summary"]["unresolved_candidate_rule_codes"] == ["alioth_6"]
    assert report["summary"]["unresolved_candidate_items"] == [
        {"page_capture_id": "capture-1", "rule_code": "alioth_6"},
        {"page_capture_id": "capture-2", "rule_code": "alioth_6"},
    ]
    assert report["summary"]["candidate_execution_coverage"] == 0.0
    assert report["summary"]["overall_status"] == "incomplete"


async def test_report_generation_service_uses_task_run_for_dynamic_preview() -> None:
    query_service = FakeTaskQueryService(
        task=_build_task_detail(),
        executions=[],
        completeness=RuleExecutionCompleteness(
            candidate_query_count=1,
            candidate_rule_count=2,
            completed_candidate_count=1,
            unresolved_rule_codes=("alioth_2",),
        ),
    )

    report = await ReportGenerationService(query_service=query_service).generate_task_report(
        "task-1"
    )

    assert ("get_rule_execution_completeness", "task-1:run-1") in query_service.calls
    assert report["summary"]["candidate_execution_coverage"] == 50.0


async def test_report_generation_service_includes_evidence_artifact_summaries() -> None:
    created_at = datetime(2026, 7, 9, 9, 15, tzinfo=UTC)
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[],
            completeness=RuleExecutionCompleteness(0, 0, 0, ()),
            evidence_artifacts=[
                EvidenceArtifactSummary(
                    artifact_id="artifact-1",
                    page_capture_id="capture-1",
                    rule_execution_id=None,
                    artifact_type="screenshot",
                    source="baseline",
                    storage_path="tasks/task-1/captures/capture-1/screenshot.png",
                    content_type="image/png",
                    checksum="sha256:abc",
                    size_bytes=1024,
                    metadata={"width": 390, "height": 844},
                    created_at=created_at,
                )
            ],
        )
    )

    report = await service.generate_task_report("task-1")

    assert report["evidence_entries"] == [
        {
            "artifact_id": "artifact-1",
            "page_capture_id": "capture-1",
            "rule_execution_id": None,
            "artifact_type": "screenshot",
            "source": "baseline",
            "storage_path": "tasks/task-1/captures/capture-1/screenshot.png",
            "content_type": "image/png",
            "checksum": "sha256:abc",
            "size_bytes": 1024,
            "metadata": {"width": 390, "height": 844},
            "created_at": created_at.isoformat(),
        }
    ]


async def test_report_generation_keeps_finalized_candidates_without_match_trace() -> None:
    query_service = FakeTaskQueryService(
        task=_build_task_detail(),
        executions=[
            _build_rule_execution(
                rule_code="alioth_6",
                status="evidence_insufficient",
                severity="high",
                summary="入口失败，候选规则未执行",
                source="bootstrap",
            ),
            _build_rule_execution(
                rule_code="alioth_7",
                status="skipped",
                severity="high",
                summary="本次运行未匹配",
                source="bootstrap",
            ),
        ],
        completeness=RuleExecutionCompleteness(
            candidate_query_count=0,
            candidate_rule_count=0,
            completed_candidate_count=0,
            unresolved_rule_codes=(),
        ),
    )
    service = ReportGenerationService(query_service=query_service)

    report = await service.generate_task_report("task-1", agent_run_id="run-1")

    assert report["summary"]["candidate_query_count"] == 0
    assert report["summary"]["candidate_rule_count"] == 1
    assert report["summary"]["unresolved_candidate_rules"] == 1
    assert report["summary"]["unresolved_candidate_rule_codes"] == ["alioth_6"]
    assert report["summary"]["candidate_execution_complete"] is False


async def test_report_generation_keeps_legacy_unbound_bootstrap_gap() -> None:
    execution = _build_rule_execution(
        rule_code="alioth_6",
        status="evidence_insufficient",
        severity="high",
        summary="历史候选规则未完成判定",
        source="bootstrap",
    )
    execution = replace(execution, page_capture_id=None)
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[execution],
            completeness=RuleExecutionCompleteness(0, 0, 0, ()),
        )
    )

    report = await service.generate_task_report("task-1", agent_run_id="run-1")

    assert report["summary"]["unresolved_candidate_rules"] == 1
    assert report["summary"]["unresolved_candidate_rule_codes"] == ["alioth_6"]
    assert report["summary"]["candidate_rule_count"] == 1
    assert report["summary"]["candidate_execution_complete"] is False
    assert report["summary"]["overall_status"] == "incomplete"


async def test_report_generation_service_marks_empty_report_as_incomplete() -> None:
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(task=_build_task_detail(), executions=[]),
        clock=lambda: datetime(2026, 7, 9, 9, 30, tzinfo=UTC),
    )

    report = await service.generate_task_report("task-1")

    assert report["summary"]["total_rules"] == 0
    assert report["summary"]["overall_status"] == "incomplete"


async def test_report_generation_does_not_count_bootstrap_skips_as_qc_success() -> None:
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_1",
                    status="skipped",
                    severity="low",
                    summary="本次运行未覆盖规则",
                    source="bootstrap",
                ),
                _build_rule_execution(
                    rule_code="alioth_2",
                    status="skipped",
                    severity="low",
                    summary="本次运行未覆盖规则",
                    source="bootstrap",
                ),
            ],
        )
    )

    report = await service.generate_task_report("task-1")

    assert report["summary"]["qc_success_rate"] == 0.0
    assert report["summary"]["coverage_gap_rules"] == 2
    assert report["summary"]["confirmed_not_applicable_rules"] == 0


async def test_report_generation_counts_warning_as_completed_but_not_passed() -> None:
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_1",
                    status="warning",
                    severity="medium",
                    summary="已完成判定但需要关注",
                    source="agent",
                ),
                _build_rule_execution(
                    rule_code="alioth_2",
                    status="evidence_insufficient",
                    severity="medium",
                    summary="证据不足",
                    source="agent",
                ),
            ],
        )
    )

    report = await service.generate_task_report("task-1")

    assert report["summary"]["qc_success_rate"] == 50.0
    assert report["summary"]["pass_rate"] == 0.0


@pytest.mark.parametrize(
    "code", ["SUPPLEMENTAL_EVIDENCE_DISABLED", "SUPPLEMENTAL_EVIDENCE_NOT_READY"]
)
async def test_report_generation_projects_legacy_supplemental_skip_as_evidence_gap(
    code: str,
) -> None:
    execution = _build_rule_execution(
        rule_code="gap", status="skipped", severity="high", summary="缺少动作后的截图",
        diagnostic_codes=(code,),
    )
    service = ReportGenerationService(query_service=FakeTaskQueryService(
        task=_build_task_detail(), executions=[execution],
    ))
    report = await service.generate_task_report("task-1")
    summary = report["summary"]
    assert summary["qc_success_rate"] == 0
    assert summary["confirmed_not_applicable_rules"] == 0
    assert summary["evidence_insufficient"] == summary["evidence_gap_rules"] == 1
    assert summary["attention_required"] == 1
    assert summary["overall_status"] != "pass"
    assert report["attention_results"][0]["execution_status"] == "skipped"
    assert report["rule_results"][0]["diagnostic_codes"] == [code]


async def test_finalized_report_uses_terminal_task_snapshot() -> None:
    finished_at = datetime(2026, 7, 9, 9, 20, tzinfo=UTC)
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_7",
                    status="manual_review",
                    severity="high",
                    summary="需要人工复核",
                    review_status="pending",
                )
            ],
        )
    )

    report = await service.generate_task_report("task-1", finalized_at=finished_at)

    assert report["task"]["status"] == "review"
    assert report["task"]["finished_at"] == finished_at.isoformat()
    assert report["report_stage"] == "final"
    assert report["is_final"] is True


async def test_failed_finalized_report_preserves_failure_terminal_status() -> None:
    finished_at = datetime(2026, 7, 9, 9, 20, tzinfo=UTC)
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[],
            completeness=RuleExecutionCompleteness(0, 0, 0, ()),
        )
    )

    report = await service.generate_task_report(
        "task-1",
        finalized_at=finished_at,
        terminal_task_status="failed",
    )

    assert report["task"]["status"] == "failed"
    assert report["task"]["finished_at"] == finished_at.isoformat()
    assert report["summary"]["overall_status"] == "error"
    assert report["is_final"] is True


async def test_review_finalized_report_preserves_incomplete_terminal_status() -> None:
    finished_at = datetime(2026, 7, 9, 9, 20, tzinfo=UTC)
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_8",
                    status="fail",
                    severity="high",
                    summary="已有部分结果",
                )
            ],
            completeness=RuleExecutionCompleteness(1, 1, 1, ()),
        )
    )

    report = await service.generate_task_report(
        "task-1",
        finalized_at=finished_at,
        terminal_task_status="review",
    )

    assert report["task"]["status"] == "review"
    assert report["summary"]["overall_status"] == "incomplete"
    assert report["is_final"] is True


async def test_review_finalized_report_distinguishes_converged_local_page_failure() -> None:
    """动态探索已收敛但存在局部页面失败时，应表达为待复核而非未完成。"""
    finished_at = datetime(2026, 7, 9, 9, 20, tzinfo=UTC)
    page_exploration = {
        "tracking_available": True,
        "complete": True,
        "quality_status": "needs_review",
        "requires_review": True,
        "page_total": 2,
        "terminal_page_count": 2,
        "completed_page_count": 1,
        "degraded_page_count": 0,
        "failed_page_count": 1,
        "pending_page_count": 0,
        "action_total": 1,
        "pending_action_count": 0,
        "failed_action_count": 0,
    }
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_1",
                    status="pass",
                    severity="low",
                    summary="可完成页面的规则已判定",
                )
            ],
            completeness=RuleExecutionCompleteness(1, 1, 1, ()),
        )
    )

    report = await service.generate_task_report(
        "task-1",
        finalized_at=finished_at,
        terminal_task_status="review",
        termination_reason="completed",
        page_exploration=page_exploration,
    )

    assert report["task"]["status"] == "review"
    assert report["summary"]["overall_status"] == "manual_review"
    assert report["page_exploration"] == page_exploration
    assert report["is_final"] is True


async def test_guard_terminated_report_stays_incomplete_after_forced_terminalization() -> None:
    """护栏终止后的失败终态是被迫收口，不能伪装成正常探索收敛。"""
    page_exploration = {
        "tracking_available": True,
        "complete": True,
        "requires_review": True,
        "page_total": 1,
        "terminal_page_count": 1,
        "failed_page_count": 1,
        "pending_work_count": 0,
    }
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[],
            completeness=RuleExecutionCompleteness(0, 0, 0, ()),
        )
    )

    report = await service.generate_task_report(
        "task-1",
        finalized_at=datetime(2026, 7, 9, 9, 20, tzinfo=UTC),
        terminal_task_status="review",
        termination_reason="max_steps_reached",
        page_exploration=page_exploration,
    )

    assert report["termination_reason"] == "max_steps_reached"
    assert report["summary"]["overall_status"] == "incomplete"


async def test_report_generation_marks_safety_interstitial_as_target_access_blocked() -> None:
    page_exploration = {
        "tracking_available": True,
        "complete": True,
        "requires_review": True,
        "page_results": [
            {
                "page_capture_id": "capture-safety",
                "page_state": "safety_interstitial",
                "risk_flags": [
                    "safety_interstitial",
                    "automation_blocked",
                    "deterministic_safety_interstitial",
                ],
                "conflicts": ["target_blocked_by_safety_interstitial"],
            }
        ],
    }
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[],
            completeness=RuleExecutionCompleteness(0, 0, 0, ()),
        )
    )

    report = await service.generate_task_report(
        "task-1",
        finalized_at=datetime(2026, 7, 9, 9, 20, tzinfo=UTC),
        terminal_task_status="review",
        termination_reason="completed",
        page_exploration=page_exploration,
    )

    assert report["summary"]["overall_status"] == "incomplete"
    assert report["summary"]["business_completion_status"] == "incomplete"
    assert report["summary"]["platform_capability_status"] == "degraded"
    assert report["summary"]["target_access_blocked"] is True
    assert report["summary"]["target_access_status"] == "blocked_by_safety_interstitial"
    assert report["summary"]["target_access_blocked_page_count"] == 1
    assert report["summary"]["target_access_block_reason_codes"] == [
        "target_blocked_by_safety_interstitial"
    ]
    assert report["summary"]["target_access_blocked_page_capture_ids"] == ["capture-safety"]
    assert report["summary"]["platform_issue_codes"] == {"target_blocked_by_safety_interstitial": 1}


async def test_converged_page_failure_does_not_hide_confirmed_rule_failure() -> None:
    """局部页面待复核不能遮蔽其他页面已经确认的规则失败。"""
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=_build_task_detail(),
            executions=[
                _build_rule_execution(
                    rule_code="alioth_2",
                    status="fail",
                    severity="high",
                    summary="另一页面已确认不通过",
                )
            ],
            completeness=RuleExecutionCompleteness(1, 1, 1, ()),
        )
    )

    report = await service.generate_task_report(
        "task-1",
        finalized_at=datetime(2026, 7, 9, 9, 20, tzinfo=UTC),
        terminal_task_status="review",
        termination_reason="completed",
        page_exploration={
            "complete": True,
            "requires_review": True,
            "failed_page_count": 1,
        },
    )

    assert report["summary"]["overall_status"] == "fail"


async def test_web_report_with_only_coverage_gap_is_unknown() -> None:
    gap = _build_web_gap_execution(
        rule_code="alioth_web_gap",
        missing_page_type="content_detail",
    )
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=replace(_build_task_detail(), target_platform="web"),
            executions=[gap],
        )
    )

    report = await service.generate_task_report(
        "task-1",
        page_exploration={
            "complete": True,
            "requires_review": False,
            "web_coverage": {
                "coverage_snapshot_id": "web-snapshot-1",
                "coverage_status": "completed_with_gaps",
                "missing_page_types": ["content_detail"],
                "gap_rule_codes": ["alioth_web_gap"],
            },
        },
    )

    assert report["summary"]["evidence_insufficient"] == 1
    assert report["summary"]["coverage_gap_rules"] == 1
    assert report["summary"]["overall_status"] == "unknown"


async def test_web_report_real_fail_takes_priority_over_coverage_gap() -> None:
    gap = _build_web_gap_execution(
        rule_code="alioth_web_gap",
        missing_page_type="content_detail",
    )
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=replace(_build_task_detail(), target_platform="web"),
            executions=[
                gap,
                _build_rule_execution(
                    rule_code="alioth_web_real_fail",
                    status="fail",
                    severity="high",
                    summary="首页存在真实违规",
                ),
            ],
        )
    )

    report = await service.generate_task_report(
        "task-1",
        page_exploration={
            "complete": True,
            "requires_review": False,
            "web_coverage": {
                "coverage_snapshot_id": "web-snapshot-1",
                "coverage_status": "completed_with_gaps",
                "missing_page_types": ["content_detail"],
                "gap_rule_codes": ["alioth_web_gap"],
            },
        },
    )

    assert report["summary"]["coverage_gap_rules"] == 1
    assert report["summary"]["fail"] == 1
    assert report["summary"]["overall_status"] == "fail"


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("source_idempotency_key", "web-coverage-gap:stale:content_detail:alioth_web_gap"),
        ("schema_version", "web-coverage-gap.v0"),
        ("source", "agent"),
        ("coverage_snapshot_id", "web-snapshot-stale"),
        ("missing_page_type", "video_detail"),
        ("reason", "generic_evidence_insufficient"),
        ("page_capture_id", "capture-unexpected"),
    ],
)
async def test_web_report_requires_complete_coverage_gap_identity(
    field: str,
    invalid_value: str,
) -> None:
    gap = _build_web_gap_execution(
        rule_code="alioth_web_gap",
        missing_page_type="content_detail",
    )
    if field in {"source_idempotency_key", "page_capture_id"}:
        setattr(gap, field, invalid_value)
    else:
        gap.judge_input = {**gap.judge_input, field: invalid_value}
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=replace(_build_task_detail(), target_platform="web"),
            executions=[gap],
        )
    )

    report = await service.generate_task_report(
        "task-1",
        page_exploration={
            "complete": True,
            "requires_review": False,
            "web_coverage": {
                "coverage_snapshot_id": "web-snapshot-1",
                "coverage_status": "completed_with_gaps",
                "missing_page_types": ["content_detail"],
                "gap_rule_codes": ["alioth_web_gap"],
            },
        },
    )

    assert report["summary"]["coverage_gap_rules"] == 0
    assert report["summary"]["overall_status"] == "manual_review"


async def test_web_captureless_evidence_insufficient_without_gap_identity_is_not_gap() -> None:
    payload = vars(
        replace(
            _build_rule_execution(
                rule_code="alioth_web_gap",
                status="evidence_insufficient",
                severity="medium",
                summary="普通证据不足",
                source="agent",
            ),
            page_capture_id=None,
        )
    ).copy()
    payload.update(
        source_idempotency_key=None,
        judge_input={"reason": "generic_evidence_insufficient"},
    )
    ordinary = SimpleNamespace(**payload)
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(
            task=replace(_build_task_detail(), target_platform="web"),
            executions=[ordinary],
        )
    )

    report = await service.generate_task_report(
        "task-1",
        page_exploration={
            "complete": True,
            "requires_review": False,
            "web_coverage": {
                "coverage_snapshot_id": "web-snapshot-1",
                "coverage_status": "completed_with_gaps",
                "missing_page_types": ["content_detail"],
                "gap_rule_codes": ["alioth_web_gap"],
            },
        },
    )

    assert report["summary"]["coverage_gap_rules"] == 0
    assert report["summary"]["overall_status"] == "manual_review"


async def test_report_generation_service_rejects_unknown_task() -> None:
    service = ReportGenerationService(
        query_service=FakeTaskQueryService(task=None, executions=[]),
        clock=lambda: datetime(2026, 7, 9, 9, 30, tzinfo=UTC),
    )

    with pytest.raises(ReportGenerationTargetNotFoundError, match="未找到质检任务"):
        await service.generate_task_report("missing-task")


async def test_report_generation_tool_uses_task_id_from_run_context() -> None:
    service = FakeReportGenerationService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        report = await generate_report_for_current_run(service=service)

    assert service.calls == [("task-1", "run-1")]
    assert report["task_id"] == "task-1"


def test_report_generation_tool_is_registered_as_safe() -> None:
    metadata = default_registry.get("report_generation_tool")

    assert metadata is not None
    assert metadata.permission_level == PermissionLevel.SAFE


def _build_task_detail() -> TaskDetail:
    return TaskDetail(
        task_id="task-1",
        target_url="https://example.com",
        ruleset_id="ruleset-1",
        ruleset_code="alioth_phase1",
        ruleset_version=1,
        channel="h5",
        status="running",
        priority=5,
        run_id="run-1",
        created_at=datetime(2026, 7, 9, 9, 0, tzinfo=UTC),
        started_at=datetime(2026, 7, 9, 9, 1, tzinfo=UTC),
        finished_at=None,
        last_error=None,
    )


def _build_rule_execution(
    *,
    rule_code: str,
    status: str,
    severity: str,
    summary: str,
    problem_desc: str | None = None,
    source: str = "agent",
    review_status: str | None = None,
    diagnostic_codes: tuple[str, ...] = (),
    missing_evidence: tuple[str, ...] = (),
) -> RuleExecutionSummary:
    return RuleExecutionSummary(
        execution_id=f"execution-{rule_code}",
        rule_id=f"rule-{rule_code}",
        rule_code=rule_code,
        rule_name=f"{rule_code} 规则",
        page_capture_id=f"capture-{rule_code}",
        execution_status=status,
        judge_strategy="rule_engine",
        confidence=0.9,
        severity=severity,
        result_summary=summary,
        problem_desc=problem_desc,
        review_status=review_status,
        started_at=datetime(2026, 7, 9, 9, 10, tzinfo=UTC),
        finished_at=datetime(2026, 7, 9, 9, 11, tzinfo=UTC),
        source=source,
        diagnostic_codes=diagnostic_codes,
        missing_evidence=missing_evidence,
    )


def _build_web_gap_execution(
    *,
    rule_code: str,
    missing_page_type: str,
) -> SimpleNamespace:
    execution = replace(
        _build_rule_execution(
            rule_code=rule_code,
            status="evidence_insufficient",
            severity="medium",
            summary=f"未发现 {missing_page_type} 页面",
            source="agent",
        ),
        page_capture_id=None,
    )
    snapshot_id = "web-snapshot-1"
    payload = vars(execution).copy()
    payload.update(
        source_idempotency_key=(
            f"web-coverage-gap:{snapshot_id}:{missing_page_type}:{rule_code}"
        ),
        judge_input={
            "schema_version": "web-coverage-gap.v1",
            "source": "web_coverage_gap",
            "coverage_snapshot_id": snapshot_id,
            "missing_page_type": missing_page_type,
            "reason": "required_page_type_not_discovered",
        },
    )
    return SimpleNamespace(**payload)
