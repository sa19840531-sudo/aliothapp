from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v1.workstation import reports as workstation_reports_module
from services.api.app.application.report_generation import (
    ReportGenerationTargetNotFoundError,
)
from services.api.app.application.workstation_reports import WorkstationReportService
from services.api.app.domain.task import TaskExecutionMode, TaskTargetPlatform
from services.api.app.main import create_app


@dataclass
class FakeWorkstationReportService:
    async def list_reports(self, *, page: int, page_size: int, **_kwargs) -> dict:
        return {
            "items": [
                {
                    "task_id": "task-1",
                    "task_name": "首页改版回归测试",
                    "report_status": "ready",
                    "report_availability": "preview",
                    "overall_status": "fail",
                    "attention_required": 2,
                    "generated_at": "2026-07-10T09:30:00+00:00",
                }
            ],
            "total": 1,
            "page": page,
            "page_size": page_size,
        }

    async def get_report(
        self, task_id: str, *, agent_run_id: str | None = None, **_kwargs
    ) -> dict:
        return {
            "report_version": "v1",
            "agent_run_id": agent_run_id or "run-latest",
            "generated_at": "2026-07-10T09:30:00+00:00",
            "task": {"task_id": task_id, "task_name": "首页改版回归测试"},
            "summary": {"total_rules": 4, "overall_status": "fail"},
            "attention_results": [{"rule_code": "RULE-014"}],
            "rule_results": [{"rule_code": "RULE-014"}],
            "evidence_entries": [],
        }


class FakeTaskService:
    def __init__(self, detail=None, stage_durations=None, list_result=None) -> None:
        self.detail = detail
        self.stage_durations = stage_durations
        self.list_result = list_result

    async def list_tasks(self, *_args, **_kwargs):
        return self.list_result

    async def get_task_detail(self, _task_id: str, **_kwargs):
        return self.detail

    async def get_web_stage_durations(self, _task_id: str, **_kwargs):
        return self.stage_durations


class FakeReportGenerationService:
    def __init__(self, report: dict) -> None:
        self.report = report
        self.calls: list[tuple[str, str | None]] = []
        self.page_explorations: list[dict | None] = []

    async def generate_task_report(
        self, task_id: str, *, agent_run_id: str | None = None, **_kwargs
    ) -> dict:
        self.calls.append((task_id, agent_run_id))
        self.page_explorations.append(_kwargs.get("page_exploration"))
        return self.report


class FakeFinalReportSnapshotRepository:
    def __init__(self, report: dict | None) -> None:
        self.report = report
        self.calls: list[tuple[str, str | None]] = []
        self.existing_runs = {"run-1"}
        self.run_existence_calls: list[tuple[str, str]] = []

    async def get_final_report(
        self, task_id: str, *, agent_run_id: str | None = None, **_kwargs
    ) -> dict | None:
        self.calls.append((task_id, agent_run_id))
        return self.report

    async def agent_run_exists(self, *, task_id: str, agent_run_id: str, **_kwargs) -> bool:
        self.run_existence_calls.append((task_id, agent_run_id))
        return agent_run_id in self.existing_runs


class FakeTaskInsightRepositoryForList:
    """列表增强元信息（待确认数/生成时间）的假实现：返回空表示无增强数据。"""

    async def report_list_meta(self, task_ids: list[str], **_kwargs) -> dict:
        return {}


class FakePageExplorationReader:
    def __init__(self, summary: dict | None) -> None:
        self.summary = summary
        self.calls: list[str] = []

    async def page_timeline(self, task_id: str, **_kwargs) -> dict:
        self.calls.append(task_id)
        if self.summary is None:
            return {}
        return {"exploration_summary": self.summary}


class FakeReportInsightReader:
    def __init__(self, insight: dict) -> None:
        self.insight = insight
        self.calls: list[tuple[str, object]] = []

    async def report_insight(self, task_id: str, *, access_scope=None) -> dict:
        self.calls.append((task_id, access_scope))
        return self.insight


def _legacy_h5_preview() -> dict:
    return {
        "report_version": "v2", "agent_run_id": "run-h5",
        "report_stage": "final", "is_final": True,
        "task": {
            "task_id": "task-h5", "target_platform": "h5", "status": "review",
            "task_config": {"workflow_version": "v2", "execution_mode": "full_inspection"},
        },
        "summary": {
            "total_rules": 73, "evaluated_rules": 73, "qc_success_rate": 100,
            "not_applicable_rules": 73, "completed_candidate_rules": 73,
            "confirmed_not_applicable_rules": 73, "candidate_execution_coverage": 100,
            "platform_capability_status": "healthy", "task_goal_completion_status": "in_progress",
        },
        "page_exploration": {"complete": True},
        "export_ready": True, "export_allowed": True,
        "evidence_entries": [{"artifact_id": "wrong-host-diagnostic"}],
        "rule_results": [{"rule_code": "RULE-001", "source": "bootstrap"}],
    }


def _h5_no_business_insight() -> dict:
    return {
        "task_id": "task-h5",
        "summary": {
            "run_id": "run-h5", "report_stage": "final_review", "total_rules": 73,
            "evaluated_rules": 0, "completed_candidate_rules": 0, "candidate_rule_count": 0,
            "confirmed_not_applicable_rules": 0, "coverage_gap_rules": 73,
            "qc_success_rate": 0, "pass_rate": 0, "full_rule_pass_rate": 0,
            "candidate_execution_coverage": 0,
        },
        "distribution": [{"status": "coverage_gap", "count": 73}],
        "rule_items": [], "confirm_progress": {"pending": 0},
        "report_availability": "none", "is_final": False,
        "export_ready": False, "export_allowed": False,
        "report_unavailable_reason": "H5_REPORT_NO_BUSINESS_RESULTS",
        "incomplete_reasons": ["H5_REPORT_NO_BUSINESS_RESULTS"],
        "page_exploration": {
            "complete": False, "quality_status": "technical_failed",
            "terminal_reason": "COMMAND_HOST_BOUNDARY_VIOLATION",
        },
    }


async def test_unbound_h5_snapshot_uses_insight_facts_without_mutating_evidence() -> None:
    snapshot = _legacy_h5_preview()
    original = deepcopy(snapshot)
    reader = FakeReportInsightReader(_h5_no_business_insight())
    generator = FakeReportGenerationService({})
    scope = object()
    service = WorkstationReportService(
        task_service=FakeTaskService(), report_generation_service=generator,
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(snapshot),
        report_insight_reader=reader,
    )

    report = await service.get_report("task-h5", access_scope=scope)

    assert report["summary"]["evaluated_rules"] == 0
    assert report["summary"]["completed_candidate_rules"] == 0
    assert report["summary"]["not_applicable_rules"] == 0
    assert report["summary"]["qc_success_rate"] == 0
    assert report["summary"]["coverage_gap_rules"] == 73
    assert report["summary"]["platform_capability_status"] == "degraded"
    assert report["summary"]["task_goal_completion_status"] == "incomplete"
    assert report["report_availability"] == "none"
    assert report["report_stage"] == "final_review"
    assert report["is_final"] is False
    assert report["export_allowed"] is False
    assert report["export_ready"] is False
    assert report["termination_reason"] == "COMMAND_HOST_BOUNDARY_VIOLATION"
    assert report["incomplete_reasons"] == [
        "H5_REPORT_NO_BUSINESS_RESULTS", "COMMAND_HOST_BOUNDARY_VIOLATION",
    ]
    assert report["evidence_entries"] == original["evidence_entries"]
    assert report["rule_results"] == original["rule_results"]
    assert reader.calls == [("task-h5", scope)]
    assert generator.calls == []
    assert snapshot == original


@pytest.mark.parametrize("platform", ["h5", "web"])
async def test_bound_report_bypasses_h5_live_projection(platform: str) -> None:
    snapshot = _legacy_h5_preview()
    snapshot["task"]["target_platform"] = platform
    snapshot.update(
        report_id="task:task-h5:run:run-h5", report_binding_version=1,
        completion_gate={
            "gate_snapshot_id": "gate-h5", "finalization_epoch": 0, "gate_passed": True,
        },
    )
    original = deepcopy(snapshot)
    reader = FakeReportInsightReader(_h5_no_business_insight())
    service = WorkstationReportService(
        task_service=FakeTaskService(), report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(snapshot),
        report_insight_reader=reader,
    )

    report = await service.get_report("task-h5")

    assert report == original
    assert report is snapshot
    assert reader.calls == []


async def test_partial_h5_review_uses_existing_insight_export_eligibility() -> None:
    insight = _h5_no_business_insight()
    insight["summary"].update(
        total_rules=3, evaluated_rules=2, completed_candidate_rules=2, candidate_rule_count=2,
        coverage_gap_rules=1, qc_success_rate=66.7, pass_rate=50, candidate_execution_coverage=100,
    )
    insight.update(
        distribution=[{"status": "passed", "count": 1}, {"status": "failed", "count": 1},
                      {"status": "coverage_gap", "count": 1}],
        report_availability="preview", export_ready=True, export_allowed=True,
        report_unavailable_reason=None, incomplete_reasons=["H5_REPORT_NOT_FINALIZED"],
        page_exploration={"complete": False, "quality_status": "needs_review"},
    )
    service = WorkstationReportService(
        task_service=FakeTaskService(), report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(_legacy_h5_preview()),
        report_insight_reader=FakeReportInsightReader(insight),
    )

    report = await service.get_report("task-h5")

    assert report["report_availability"] == "preview"
    assert report["export_allowed"] is True
    assert report["export_ready"] is True
    assert report["summary"]["qc_success_rate"] == 66.7
    assert report["summary"]["evaluated_rules"] == 2
    assert report["summary"]["pass"] == 1
    assert report["summary"]["fail"] == 1
    assert report["summary"]["platform_capability_status"] == "unassessed"
    assert report["incomplete_reasons"] == ["H5_REPORT_NOT_FINALIZED"]


async def test_historical_h5_run_does_not_receive_current_run_projection() -> None:
    snapshot = _legacy_h5_preview()
    snapshot["agent_run_id"] = "run-historical"
    reader = FakeReportInsightReader(_h5_no_business_insight())
    service = WorkstationReportService(
        task_service=FakeTaskService(), report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(snapshot),
        report_insight_reader=reader,
    )

    report = await service.get_report("task-h5", agent_run_id="run-historical")

    assert report is snapshot
    assert report["agent_run_id"] == "run-historical"
    assert "report_unavailable_reason" not in report


@pytest.mark.parametrize("binding_readable", [True, False])
async def test_h5_sealing_during_preview_read_never_marks_old_body_final(
    binding_readable: bool,
) -> None:
    preview = _legacy_h5_preview()
    frozen = deepcopy(preview)
    frozen.update(
        report_id="task:task-h5:run:run-h5", report_binding_version=1,
        completion_gate={
            "gate_snapshot_id": "gate-h5", "finalization_epoch": 0, "gate_passed": True,
        },
    )
    frozen["evidence_entries"] = [{"artifact_id": "sealed-evidence"}]

    class SealingRepository(FakeFinalReportSnapshotRepository):
        async def get_final_report(self, task_id: str, *, agent_run_id=None, **_kwargs):
            self.calls.append((task_id, agent_run_id))
            return preview if len(self.calls) == 1 else (frozen if binding_readable else None)

    insight = _h5_no_business_insight()
    insight.update(
        report_availability="final", is_final=True, export_ready=True, export_allowed=True,
    )
    insight["summary"]["report_stage"] = "final"
    repository = SealingRepository(preview)
    service = WorkstationReportService(
        task_service=FakeTaskService(), report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=repository,
        report_insight_reader=FakeReportInsightReader(insight),
    )

    report = await service.get_report("task-h5")

    assert repository.calls == [("task-h5", None), ("task-h5", "run-h5")]
    if binding_readable:
        assert report is frozen
        assert report["evidence_entries"] == [{"artifact_id": "sealed-evidence"}]
    else:
        assert report["report_availability"] == "preview"
        assert report["report_stage"] == "final_review"
        assert report["is_final"] is False
        assert report["export_allowed"] is False
        assert report["evidence_entries"] == preview["evidence_entries"]


@pytest.fixture
def client() -> TestClient:
    app = create_app()
    app.dependency_overrides[workstation_reports_module._get_workstation_report_service] = (
        lambda: FakeWorkstationReportService()
    )
    app.dependency_overrides[workstation_reports_module._get_task_insight_repository] = (
        lambda: FakeTaskInsightRepositoryForList()
    )
    return TestClient(app)


def test_list_workstation_reports_returns_report_rows(client: TestClient) -> None:
    response = client.get("/api/v1/workstation/reports?page=2&page_size=10")

    assert response.status_code == 200
    body = response.json()
    assert body["items"][0]["task_id"] == "task-1"
    assert body["items"][0]["report_status"] == "ready"
    assert body["items"][0]["report_availability"] == "preview"
    assert body["page"] == 2
    assert body["page_size"] == 10


def test_get_workstation_report_returns_report_detail(client: TestClient) -> None:
    response = client.get("/api/v1/workstation/reports/task-1")

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["task_id"] == "task-1"
    assert body["summary"]["overall_status"] == "fail"
    assert body["attention_results"][0]["rule_code"] == "RULE-014"


def test_get_workstation_report_supports_explicit_agent_run(client: TestClient) -> None:
    response = client.get(
        "/api/v1/workstation/reports/task-1",
        params={"agent_run_id": "run-1"},
    )

    assert response.status_code == 200
    assert response.json()["agent_run_id"] == "run-1"


def test_h5_report_http_detail_does_not_repeat_legacy_business_success(client: TestClient) -> None:
    service = WorkstationReportService(
        task_service=FakeTaskService(), report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(_legacy_h5_preview()),
        report_insight_reader=FakeReportInsightReader(_h5_no_business_insight()),
    )
    client.app.dependency_overrides[workstation_reports_module._get_workstation_report_service] = (
        lambda: service
    )

    response = client.get("/api/v1/workstation/reports/task-h5")

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["qc_success_rate"] == 0
    assert body["summary"]["evaluated_rules"] == 0
    assert body["summary"]["confirmed_not_applicable_rules"] == 0
    assert body["report_availability"] == "none"
    assert body["export_allowed"] is False


async def test_h5_dynamic_preview_uses_same_report_insight_facts() -> None:
    reader = FakeReportInsightReader(_h5_no_business_insight())
    generator = FakeReportGenerationService(_legacy_h5_preview())
    service = WorkstationReportService(
        task_service=FakeTaskService(SimpleNamespace(
            target_platform=TaskTargetPlatform.H5, workflow_version="v2",
            execution_mode=TaskExecutionMode.FULL_INSPECTION, status="review",
        )),
        report_generation_service=generator,
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(None),
        report_insight_reader=reader,
    )

    report = await service.get_report("task-h5")

    assert generator.calls == [("task-h5", None)]
    assert reader.calls == [("task-h5", None)]
    assert report["report_availability"] == "none"
    assert report["summary"]["completed_candidate_rules"] == 0


async def test_workstation_report_prefers_worker_final_snapshot() -> None:
    snapshot = {
        "report_version": "v1",
        "report_stage": "final",
        "is_final": True,
        "task": {"task_id": "task-1", "status": "failed"},
        "summary": {"overall_status": "error"},
        "evidence_entries": [{"artifact_id": "artifact-1"}],
    }
    snapshot_repository = FakeFinalReportSnapshotRepository(snapshot)
    generator = FakeReportGenerationService({"report_stage": "preview"})
    service = WorkstationReportService(
        task_service=FakeTaskService(),
        report_generation_service=generator,
        final_report_snapshot_repository=snapshot_repository,
    )

    report = await service.get_report("task-1")

    assert report == snapshot
    assert snapshot_repository.calls == [("task-1", None)]
    assert generator.calls == []


async def test_workstation_report_never_enriches_final_snapshot_from_live_facts() -> None:
    snapshot = {
        "report_version": "v2",
        "report_stage": "final",
        "is_final": True,
        "task": {"task_id": "task-1", "status": "failed"},
        "summary": {"overall_status": "error"},
        "evidence_entries": [{"artifact_id": "artifact-1"}],
    }
    page_exploration = {
        "tracking_available": True,
        "complete": True,
        "quality_status": "complete",
        "page_total": 3,
        "page_results": [{"page_capture_id": "capture-1"}],
        "action_results": [{"exploration_action_id": "action-1"}],
    }
    snapshot_repository = FakeFinalReportSnapshotRepository(snapshot)
    generator = FakeReportGenerationService({"report_stage": "preview"})
    reader = FakePageExplorationReader(page_exploration)
    service = WorkstationReportService(
        task_service=FakeTaskService(),
        report_generation_service=generator,
        final_report_snapshot_repository=snapshot_repository,
        page_exploration_reader=reader,
    )

    report = await service.get_report("task-1")

    assert report == snapshot
    assert "page_exploration" not in report
    assert reader.calls == []
    assert generator.calls == []


async def test_workstation_report_falls_back_to_dynamic_preview() -> None:
    preview = {
        "report_version": "v1",
        "report_stage": "preview",
        "is_final": False,
        "task": {"task_id": "task-1", "status": "running"},
        "summary": {"overall_status": "incomplete"},
        "evidence_entries": [],
    }
    snapshot_repository = FakeFinalReportSnapshotRepository(None)
    generator = FakeReportGenerationService(preview)
    service = WorkstationReportService(
        task_service=FakeTaskService(),
        report_generation_service=generator,
        final_report_snapshot_repository=snapshot_repository,
    )

    report = await service.get_report("task-1")

    assert report == preview
    assert snapshot_repository.calls == [("task-1", None)]
    assert generator.calls == [("task-1", None)]
    assert generator.page_explorations == [None]


async def test_web_v2_full_report_route_returns_unbound_preview() -> None:
    snapshot_repository = FakeFinalReportSnapshotRepository(None)
    generator = FakeReportGenerationService(
        {
            "report_stage": "preview",
            "is_final": False,
            "task": {"task_id": "task-web-formal"},
        }
    )
    service = WorkstationReportService(
        task_service=FakeTaskService(
            SimpleNamespace(
                target_platform=TaskTargetPlatform.WEB,
                workflow_version="v2",
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
                report_availability="preview",
            )
        ),
        report_generation_service=generator,
        final_report_snapshot_repository=snapshot_repository,
    )

    report = await service.get_report("task-web-formal")

    assert report["report_stage"] == "preview"
    assert report["is_final"] is False
    assert report["report_availability"] == "preview"
    assert report["export_allowed"] is False
    assert snapshot_repository.calls == [("task-web-formal", None)]
    assert generator.calls == [("task-web-formal", None)]


async def test_h5_v2_terminal_report_fallback_is_final_review() -> None:
    snapshot_repository = FakeFinalReportSnapshotRepository(None)
    generator = FakeReportGenerationService(
        {
            "report_stage": "preview",
            "is_final": False,
            "task": {"task_id": "task-h5-review"},
        }
    )
    service = WorkstationReportService(
        task_service=FakeTaskService(
            SimpleNamespace(
                target_platform=TaskTargetPlatform.H5,
                workflow_version="v2",
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
                status="review",
                report_availability="preview",
            )
        ),
        report_generation_service=generator,
        final_report_snapshot_repository=snapshot_repository,
    )

    report = await service.get_report("task-h5-review")

    assert report["report_stage"] == "final_review"
    assert report["report_availability"] == "preview"
    assert report["is_final"] is False
    assert report["export_allowed"] is False
    assert report["export_ready"] is False
    assert report["incomplete_reasons"] == ["H5_REPORT_NOT_FINALIZED"]


async def test_h5_v2_running_report_fallback_keeps_preview() -> None:
    preview = {
        "report_stage": "preview",
        "is_final": False,
        "task": {"task_id": "task-h5-running"},
    }
    service = WorkstationReportService(
        task_service=FakeTaskService(
            SimpleNamespace(
                target_platform=TaskTargetPlatform.H5,
                workflow_version="v2",
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
                status="running",
                report_availability="preview",
            )
        ),
        report_generation_service=FakeReportGenerationService(preview),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(None),
    )

    report = await service.get_report("task-h5-running")

    assert report == preview


async def test_web_v2_report_without_persisted_facts_has_none_availability() -> None:
    generator = FakeReportGenerationService({"report_stage": "preview"})
    service = WorkstationReportService(
        task_service=FakeTaskService(
            SimpleNamespace(
                target_platform=TaskTargetPlatform.WEB,
                workflow_version="v2",
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
                report_availability="none",
            )
        ),
        report_generation_service=generator,
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(None),
    )

    with pytest.raises(ReportGenerationTargetNotFoundError, match="尚无可预览"):
        await service.get_report("task-web-empty")

    assert generator.calls == []


async def test_report_list_only_labels_web_none_state_as_missing_trusted_facts() -> None:
    created_at = datetime(2026, 9, 7, tzinfo=UTC)
    list_result = SimpleNamespace(
        items=[
            SimpleNamespace(
                task_id="task-web",
                task_name="Web 任务",
                target_platform=TaskTargetPlatform.WEB,
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
                report_status="not_started",
                report_availability="none",
                created_at=created_at,
                product=SimpleNamespace(
                    department_name="质检部",
                    product_code="WEB-1",
                    product_name="Web 站点",
                ),
            ),
            SimpleNamespace(
                task_id="task-h5",
                task_name="H5 任务",
                target_platform=TaskTargetPlatform.H5,
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
                report_status="not_started",
                report_availability="none",
                created_at=created_at,
                product=SimpleNamespace(
                    department_name="质检部",
                    product_code="H5-1",
                    product_name="H5 站点",
                ),
            ),
        ],
        total=2,
        page=1,
        page_size=20,
    )
    service = WorkstationReportService(
        task_service=FakeTaskService(list_result=list_result),
        report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(None),
    )

    payload = await service.list_reports(page=1, page_size=20)

    assert payload["items"][0]["report_unavailable_reason"] == (
        "WEB_REPORT_NO_PERSISTED_FACTS"
    )
    assert payload["items"][1]["report_unavailable_reason"] is None


class FailingWorkstationReportService(FakeWorkstationReportService):
    async def list_reports(self, **_kwargs) -> dict:
        raise RuntimeError("database connection reset")

    async def get_report(self, task_id: str, **_kwargs) -> dict:
        del task_id
        raise RuntimeError("database connection reset")


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/workstation/reports",
        "/api/v1/workstation/reports/task-1",
    ],
)
def test_report_backend_failure_returns_auditable_error_id(path: str) -> None:
    app = create_app()
    app.dependency_overrides[workstation_reports_module._get_workstation_report_service] = (
        lambda: FailingWorkstationReportService()
    )
    api = TestClient(app, raise_server_exceptions=False)

    response = api.get(path)

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "ReportBackendError"
    assert error["error_id"].startswith("report-")
    assert error["error_id"] in error["message"]
    assert "database connection reset" not in error["message"]


class FailingReportInsightService:
    async def report_insight(self, task_id: str, **_kwargs) -> dict:
        del task_id
        raise RuntimeError("object storage unavailable")


def test_report_insight_backend_failure_returns_auditable_error_id() -> None:
    app = create_app()
    app.dependency_overrides[workstation_reports_module._get_report_insight_service] = (
        lambda: FailingReportInsightService()
    )
    api = TestClient(app, raise_server_exceptions=False)

    response = api.get("/api/v1/workstation/reports/task-1/insight")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "ReportBackendError"
    assert error["error_id"].startswith("report-")
    assert error["error_id"] in error["message"]
    assert "object storage unavailable" not in error["message"]


async def test_web_v2_formal_report_replay_does_not_read_live_stage_durations() -> None:
    snapshot = {
        "report_version": "v2",
        "report_stage": "final",
        "is_final": True,
        "agent_run_id": "run-web",
        "task": {"task_id": "task-web", "status": "done"},
        "summary": {"overall_status": "pass"},
        "evidence_entries": [],
    }
    durations = {
        "exploration_ms": 120_000,
        "inspection_ms": 90_000,
        "report_generation_ms": 30_000,
        "total_ms": 300_000,
    }
    service = WorkstationReportService(
        task_service=FakeTaskService(
            SimpleNamespace(
                target_platform=TaskTargetPlatform.WEB,
                workflow_version="v2",
                execution_mode=TaskExecutionMode.FULL_INSPECTION,
            ),
            stage_durations=durations,
        ),
        report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(snapshot),
    )

    report = await service.get_report("task-web")
    replayed = await service.get_report("task-web")

    assert "stage_durations" not in report
    assert replayed == report
    assert report == snapshot


async def test_workstation_report_dynamic_preview_loads_page_exploration() -> None:
    preview = {
        "report_version": "v2",
        "report_stage": "preview",
        "is_final": False,
        "task": {"task_id": "task-1", "status": "failed"},
        "summary": {"overall_status": "error"},
        "evidence_entries": [],
    }
    page_exploration = {
        "tracking_available": True,
        "complete": True,
        "quality_status": "complete",
        "page_total": 3,
        "page_results": [{"page_capture_id": "capture-1"}],
        "action_results": [{"exploration_action_id": "action-1"}],
    }
    snapshot_repository = FakeFinalReportSnapshotRepository(None)
    generator = FakeReportGenerationService(preview)
    reader = FakePageExplorationReader(page_exploration)
    service = WorkstationReportService(
        task_service=FakeTaskService(),
        report_generation_service=generator,
        final_report_snapshot_repository=snapshot_repository,
        page_exploration_reader=reader,
    )

    report = await service.get_report("task-1")

    assert report == preview
    assert reader.calls == ["task-1"]
    assert generator.calls == [("task-1", None)]
    assert generator.page_explorations == [page_exploration]


async def test_workstation_report_reads_snapshot_for_explicit_run() -> None:
    snapshot = {"agent_run_id": "run-1", "report_stage": "final"}
    snapshot_repository = FakeFinalReportSnapshotRepository(snapshot)
    generator = FakeReportGenerationService({"report_stage": "preview"})
    service = WorkstationReportService(
        task_service=FakeTaskService(),
        report_generation_service=generator,
        final_report_snapshot_repository=snapshot_repository,
    )

    report = await service.get_report("task-1", agent_run_id="run-1")

    assert report == snapshot
    assert snapshot_repository.calls == [("task-1", "run-1")]
    assert snapshot_repository.run_existence_calls == []
    assert generator.calls == []


async def test_workstation_report_rejects_run_from_another_task() -> None:
    service = WorkstationReportService(
        task_service=FakeTaskService(),
        report_generation_service=FakeReportGenerationService({}),
        final_report_snapshot_repository=FakeFinalReportSnapshotRepository(None),
    )

    with pytest.raises(ReportGenerationTargetNotFoundError, match="不存在 Agent Run"):
        await service.get_report("task-1", agent_run_id="run-other")
