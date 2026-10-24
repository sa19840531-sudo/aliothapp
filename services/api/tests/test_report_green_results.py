"""未封存报告服务契约与权限查询回归；受控投影不冒充正式判读或设备验收。"""

import copy
import json
from contextlib import AbstractAsyncContextManager
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.green_rule_catalog import build_green_rule_payload
from services.api.app.application.green_rule_result_projection import green_result_identity
from services.api.app.application.report_insight import (
    ReportConfirmationError,
    ReportExportBlockedError,
    ReportInsightService,
    _bound_final_snapshot_insight,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.tests.test_report_insight import (
    FakeEvidenceStore,
    FakeFinalReportSnapshotRepository,
    FakeReportRepository,
    _row,
)


@pytest.fixture(autouse=True)
def no_external_inventory(monkeypatch):
    monkeypatch.setattr(
        "services.api.app.infrastructure.db.task_rule_inventory_repository."
        "PostgresTaskRuleInventoryRepository.load_snapshot",
        AsyncMock(return_value=None),
    )


def _scope(*, review_only=False):
    return AccessScope(
        actor_user_id="reviewer-1",
        tenant_id="tenant-a",
        permissions=frozenset(
            {"taskObjects:review:tenant"}
            if review_only else {"taskObjects:read:tenant", "taskObjects:review:tenant"}
        ),
    )


class GreenReportRepository(FakeReportRepository):
    """只替代服务端读仓储；正式原件链在独立PG专项测试覆盖。"""

    def __init__(self, *, key="s35", state="pass", review="pending", missing=False):
        super().__init__()
        self.rule = {"rule_id": "green-rule-" + key, **build_green_rule_payload(key)}
        self.context.update(
            target_platform="app",
            app_report_availability="preview",
            task_config={
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
                "rule_inventory": {"rules": [self.rule]},
            },
        )
        self.rows = [
            _row(
                self.rule["rule_code"], state,
                rule_id=self.rule["rule_id"], execution_id="green-execution",
            )
        ]
        contract = self.rule["judge_config"]["green_rule_contract"]
        self.green = {
            **green_result_identity(self.rule),
            "linkage_status": "linked",
            "linkage_notice": None,
            "execution_status": state,
            "review_status": review,
            "review_reason": "当前复核记录",
            "missing_evidence": ["controlled_missing_fact"] if missing else [],
            "subitems": [
                {
                    "key": key,
                    "status": "evidence_insufficient" if missing else state,
                    "evaluated": not missing,
                    "confidence": None if missing or state == "manual_review" else 1.0,
                    "reason": "受控服务契约投影",
                    "missing_evidence": ["controlled_missing_fact"] if missing else [],
                    "evidence_artifact_ids": [],
                    "capture_ids": [],
                    "screenshot_artifact_ids": [],
                }
                for key in contract["subchecks"]
            ],
        }
        self.read_green_rule_results = AsyncMock(
            side_effect=lambda **_: {"green-execution": copy.deepcopy(self.green)}
        )
        self.read_green_confirmation_results = AsyncMock(
            side_effect=lambda **_: {"green-execution": copy.deepcopy(self.green)}
        )

    async def fetch_rule_execution(self, execution_id, **kwargs):
        result = await super().fetch_rule_execution(execution_id, **kwargs)
        if result is not None:
            source = next(row for row in self.rows if row["execution_id"] == execution_id)
            result.update(
                rule_id=source["rule_id"],
                green_confirmation_context=copy.deepcopy(self.context),
            )
        return result


def _service(repository, **kwargs):
    return ReportInsightService(
        repository=repository, evidence_store_factory=lambda: FakeEvidenceStore({}), **kwargs
    )


@pytest.mark.parametrize("key", ["102", "s72", "41", "62", "s35", "s49", "s73", "32"])
async def test_open_report_attaches_same_whitelist_and_never_counts_pending_as_pass(key):
    repository = GreenReportRepository(key=key)
    result = await _service(repository).report_insight("task-1", access_scope=_scope())
    item = result["rule_items"][0]
    assert item["green_result"] == repository.green
    assert item["display_status"] == "other" and item["execution_status"] == "pass"
    assert item["machine_display_status"] == "passed"
    assert result["pass_rate"] == result["qc_success_rate"] == 0
    assert result["summary"]["completed_candidate_rules"] == 0
    assert result["confirm_progress"] == {
        "total": 0, "confirmed": 0, "pending": 0, "completed": False,
    }
    assert result["green_results_incomplete"] is True and result["green_results_notice"]
    assert result["export_ready"] is False and result["export_allowed"] is False
    repository.read_green_rule_results.assert_awaited_once_with(
        task_id="task-1", execution_ids=["green-execution"], access_scope=_scope()
    )
    with pytest.raises(ReportExportBlockedError, match="绿色规则"):
        await _service(repository).export_html("task-1", access_scope=_scope())


@pytest.mark.parametrize("linkage", ["input_mismatch", "evidence_changed", "missing_projection"])
async def test_unlinked_or_missing_projection_never_exposes_an_old_pass_or_confirmation(linkage):
    repository = GreenReportRepository(state="manual_review")
    repository.rows[0]["human_verdict"] = "pass"
    repository.green.update(linkage_status=linkage, subitems=[], execution_status=None)
    if linkage == "missing_projection":
        repository.read_green_rule_results.side_effect = None
        repository.read_green_rule_results.return_value = {}
    result = await _service(repository).report_insight("task-1")
    item = result["rule_items"][0]
    assert item["display_status"] == "other" and item["display_status_label"] == "关联待核对"
    assert item["execution_status"] == "manual_review" and item["human_verdict"] == "pass"
    assert item["confirmable"] is False and item["confirm_status"] is None
    assert result["green_results_incomplete"] is True
    assert result["confirm_progress"]["completed"] is False
    assert result["pass_rate"] == result["qc_success_rate"] == 0


@pytest.mark.parametrize("state", ["fail", "evidence_insufficient"])
async def test_missing_materials_stay_incomplete_after_current_approval(state):
    repository = GreenReportRepository(state=state, review="approved", missing=True)
    repository.rows[0]["human_verdict"] = "pass"
    result = await _service(repository).report_insight("task-1")
    item = result["rule_items"][0]
    assert item["execution_status"] == state and item["human_verdict"] == "pass"
    assert item["green_result"]["review_status"] == "approved"
    assert item["green_result"]["subitems"][0]["evaluated"] is False
    assert item["display_status"] == ("failed" if state == "fail" else "other")
    assert result["green_results_incomplete"] is True
    assert result["qc_success_rate"] == result["summary"]["completed_candidate_rules"] == 0


async def test_explicit_fail_with_other_gaps_keeps_h5_preview_but_not_completed():
    repository = GreenReportRepository(key="32", state="fail", missing=True)
    repository.context.update(target_platform="h5")
    result = await _service(repository).report_insight("task-1")
    assert result["rule_items"][0]["display_status"] == "failed"
    assert result["report_availability"] == "preview" and result["is_final"] is False
    assert result["summary"]["completed_candidate_rules"] == 0
    assert result["qc_success_rate"] == 0 and result["export_allowed"] is False
    assert result["green_results_incomplete"] is True


@pytest.mark.parametrize("gap", ["current_pending", "unevaluated_child"])
async def test_known_fail_remains_failed_but_cannot_count_as_completed_when_unresolved(gap):
    repository = GreenReportRepository(state="fail", review="approved")
    if gap == "current_pending":
        repository.green["review_status"] = "pending"
    else:
        repository.green["subitems"][1].update(
            status="skipped", evaluated=False, confidence=None
        )
    result = await _service(repository).report_insight("task-1")
    assert result["rule_items"][0]["display_status"] == "failed"
    assert result["green_results_incomplete"] is True
    assert result["qc_success_rate"] == result["summary"]["completed_candidate_rules"] == 0
    assert result["confirm_progress"]["completed"] is False


@pytest.mark.parametrize("review", ["pending", "needs_recheck"])
async def test_current_pending_replaces_stale_human_confirmation_without_erasing_audit(review):
    repository = GreenReportRepository(state="manual_review", review=review)
    repository.rows[0]["human_verdict"] = "pass"
    result = await _service(repository).report_insight("task-1")
    item = result["rule_items"][0]
    assert item["display_status"] == "other" and item["human_verdict"] == "pass"
    assert item["review_status"] == review and item["confirm_status"] == "pending"
    assert result["confirm_progress"] == {
        "total": 1, "confirmed": 0, "pending": 1, "completed": False,
    }
    assert result["qc_success_rate"] == 0


async def test_real_approved_business_review_without_gaps_keeps_existing_human_verdict():
    repository = GreenReportRepository(state="manual_review", review="approved")
    repository.rows[0]["human_verdict"] = "pass"
    result = await _service(repository).report_insight("task-1")
    assert result["rule_items"][0]["display_status"] == "passed"
    assert result["green_results_incomplete"] is False and result["green_results_notice"] is None
    assert result["confirm_progress"]["completed"] is True
    assert result["qc_success_rate"] == result["pass_rate"] == 100


@pytest.mark.parametrize("kind", ["app_final", "legacy_app", "legacy_web", "legacy_h5"])
async def test_sealed_report_keeps_original_outputs_statistics_confirmation_and_export(kind):
    repository = GreenReportRepository(state="manual_review", missing=True)
    repository.rows[0]["human_verdict"] = "fail"
    repository.context.update(finalization_state="sealed", sealed_report_bound=True)
    if kind == "app_final":
        repository.context.update(app_report_availability="final", completion_gate_passed=True)
    else:
        repository.context["target_platform"] = kind.removeprefix("legacy_")
        repository.context["task_config"].pop("workflow_version")
        repository.context["completion_gate_passed"] = True
    baseline = copy.deepcopy(repository)
    baseline.context["task_config"].pop("rule_inventory")
    current_service, old_service = _service(repository), _service(baseline)
    assert await current_service.report_insight("task-1") == (
        await old_service.report_insight("task-1")
    )
    assert await current_service.export_html("task-1") == await old_service.export_html("task-1")
    current = await current_service.confirm_rule(
        "task-1", "green-execution", verdict="pass", access_scope=_scope(review_only=True)
    )
    old = await old_service.confirm_rule(
        "task-1", "green-execution", verdict="pass", access_scope=_scope(review_only=True)
    )
    assert current == old
    repository.read_green_rule_results.assert_not_awaited()
    repository.read_green_confirmation_results.assert_not_awaited()


async def test_app_preview_old_sealed_flags_do_not_bypass_actual_current_green_results():
    repository = GreenReportRepository()
    repository.context.update(finalization_state="sealed", sealed_report_bound=True)
    result = await _service(repository).report_insight("task-1")
    assert result["report_availability"] == "preview"
    assert result["green_results_incomplete"] is True
    repository.read_green_rule_results.assert_awaited_once()


@pytest.mark.parametrize("legacy", ["no_green", "v1", "unexecuted_candidate"])
async def test_ordinary_legacy_and_unexecuted_candidates_add_no_green_queries_or_flags(legacy):
    repository = GreenReportRepository()
    if legacy == "no_green":
        repository.context["task_config"].pop("rule_inventory")
    elif legacy == "v1":
        repository.rule["judge_config"]["green_rule_contract"]["schema_version"] = (
            "green-rule-contract.v1"
        )
    else:
        repository.rows = []
    result = await _service(repository).report_insight("task-1")
    assert "green_results_incomplete" not in result
    assert all("green_result" not in item for item in result["rule_items"])
    repository.read_green_rule_results.assert_not_awaited()


@pytest.mark.parametrize("gap", ["missing", "unlinked", "no_scope"])
async def test_confirmation_cannot_close_green_material_or_linkage_gaps(gap):
    repository = GreenReportRepository(state="manual_review", missing=gap == "missing")
    if gap == "unlinked":
        repository.green.update(linkage_status="input_mismatch", subitems=[], execution_status=None)
    service = _service(repository)
    with pytest.raises(ReportConfirmationError, match="材料缺口|关联待核对"):
        await service.confirm_rule(
            "task-1", "green-execution", verdict="pass",
            access_scope=None if gap == "no_scope" else _scope(review_only=True),
        )
    assert repository.updates == []
    repository.read_green_rule_results.assert_not_awaited()
    if gap == "no_scope":
        repository.read_green_confirmation_results.assert_not_awaited()
    else:
        repository.read_green_confirmation_results.assert_awaited_once_with(
            task_id="task-1", execution_ids=["green-execution"],
            access_scope=_scope(review_only=True),
        )


async def test_true_business_review_keeps_existing_explicit_review_only_confirmation_entry():
    repository = GreenReportRepository(state="manual_review")
    result = await _service(repository).confirm_rule(
        "task-1", "green-execution", verdict="pass", note="明确人工终审",
        access_scope=_scope(review_only=True),
    )
    assert result["human_verdict"] == "pass"
    assert "green_result" not in result and "green_confirmation_context" not in result
    assert repository.updates[0]["operator"] == "reviewer-1"
    repository.read_green_rule_results.assert_not_awaited()


class _AsyncContext(AbstractAsyncContextManager):
    def __init__(self, value=None):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_):
        return None


class _Connection:
    def __init__(self, row=None):
        self.row = row
        self.calls = []
        self.transactions = []

    async def fetchrow(self, sql, *args):
        self.calls.append((sql, args))
        return self.row

    def transaction(self, **kwargs):
        self.transactions.append(kwargs)
        return _AsyncContext()


class _Pool:
    def __init__(self, row=None):
        self.connection = _Connection(row)
        self.acquisitions = 0

    def acquire(self):
        self.acquisitions += 1
        return _AsyncContext(self.connection)


async def test_review_only_confirmation_context_keeps_exact_original_authorized_object_query():
    context = {"task_config": {}, "target_platform": "app", "app_report_availability": "preview"}
    pool = _Pool({
        "execution_id": "execution-1", "rule_id": "rule-1",
        "green_confirmation_context": json.dumps(context),
    })
    result = await PostgresTaskInsightRepository(pool=pool).fetch_rule_execution(
        "execution-1", task_id="task-1", access_scope=_scope(review_only=True)
    )
    assert result["green_confirmation_context"] == context
    sql, args = pool.connection.calls[0]
    assert "execution.task_id = $2::uuid" in sql and "task.tenant_id" in sql
    assert "execution.rule_id::text AS rule_id" in sql
    assert "'task_config',task.task_config" in sql
    assert "task.finalization_agent_run_id" in sql and "qc_report_gate_binding" in sql
    assert "SELECT id,output_context FROM qc_agent_run" in sql
    assert "run.output_context" in sql
    assert args == ("execution-1", "task-1", False, "tenant-a", True, "reviewer-1", None)


async def test_private_confirmation_reader_keeps_review_scope_separate_from_public_reader(
    monkeypatch,
):
    from services.api.app.infrastructure.db import green_rule_results

    load = AsyncMock(return_value={})
    monkeypatch.setattr(green_rule_results, "read_green_rule_results", load)
    pool = _Pool()
    repository = PostgresTaskInsightRepository(pool=pool, artifact_store=object())
    scope = _scope(review_only=True)
    await repository.read_green_confirmation_results(
        task_id="task-1", execution_ids=["execution-1"], access_scope=scope
    )
    internal = load.await_args.kwargs
    assert internal["task_id"] == "task-1" and internal["execution_ids"] == ["execution-1"]
    assert internal["scope_args"] == (False, "tenant-a", True, "reviewer-1", None)
    assert pool.connection.transactions == [{"isolation": "repeatable_read", "readonly": True}]
    await repository.read_green_rule_results(
        task_id="task-1", execution_ids=["execution-1"], access_scope=scope
    )
    assert load.await_args.kwargs["scope_args"] == (
        False, "tenant-a", False, "reviewer-1", None,
    )
    assert await repository.read_green_confirmation_results(
        task_id="task-1", execution_ids=["execution-1"], access_scope=None
    ) == {}
    assert pool.acquisitions == 2


@pytest.mark.parametrize("platform", ["web", "h5"])
async def test_bound_final_snapshot_never_queries_or_applies_current_green_projection(platform):
    repository = GreenReportRepository(missing=True)
    snapshot = {
        "report_id": "task:task-1:run:run-1", "report_binding_version": 2,
        "report_stage": "final", "is_final": True, "agent_run_id": "run-1",
        "task": {
            "task_id": "task-1", "target_platform": platform, "status": "done",
            "task_config": {"workflow_version": "v2", "execution_mode": "full_inspection"},
        },
        "summary": {"total_rules": 1, "pass": 1, "qc_success_rate": 100, "pass_rate": 100},
        "completion_gate": {
            "gate_passed": True, "gate_snapshot_id": "gate-1", "finalization_epoch": 0,
        },
        "page_exploration": {},
        "rule_results": [{
            "execution_id": "frozen-execution", "rule_id": repository.rule["rule_id"],
            "rule_code": repository.rule["rule_code"], "execution_status": "pass",
        }],
        "evidence_entries": [],
    }
    if platform == "web":
        snapshot["report_contract"] = {
            "target_platform": "web", "workflow_version": "v2",
            "execution_mode": "full_inspection", "notification_mode": "enforced",
        }
        snapshot["page_exploration"]["web_coverage"] = {
            "coverage_snapshot_id": "coverage-1", "snapshot_hash": "hash-1",
            "finalization_epoch": 0,
        }
    baseline = copy.deepcopy(snapshot)
    result = await _service(
        repository, final_report_snapshot_repository=FakeFinalReportSnapshotRepository(snapshot)
    ).report_insight("task-1")
    expected = _bound_final_snapshot_insight(snapshot, target_platform=platform)
    assert result["rule_items"] == [
        {**expected["rule_items"][0], "ruleset_code": None, "ruleset_name": None}
    ]
    assert result["summary"]["report_stage"] == "final"
    assert "green_results_incomplete" not in result
    assert snapshot == baseline
    repository.read_green_rule_results.assert_not_awaited()
