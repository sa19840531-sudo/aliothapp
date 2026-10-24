"""实际共享Writer到报告与任务详情的一致性；材料为隔离夹具，不代表产品验收。"""

from uuid import uuid4

import pytest

from services.api.app.application.report_insight import (
    ReportConfirmationError,
    ReportExportBlockedError,
    ReportInsightService,
)
from services.api.app.application.task_insight import TaskInsightService
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.task_rule_inventory_repository import (
    PostgresTaskRuleInventoryRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只升级显式隔离数据库
)
from services.api.tests.test_green_aggregate_execution_postgres import green_case
from services.api.tests.test_green_process_judge_postgres import _local_materials
from services.api.tests.test_green_rule_results_postgres import _write_actual


async def _facts(connection, task_id):
    """比较已有业务行完整内容，防止读报告时顺手更新复核或历史结果。"""
    result = {}
    for table in ("qc_rule_execution", "qc_review_item", "qc_capture", "qc_outbox_event"):
        result[table] = await connection.fetchval(
            "SELECT coalesce(jsonb_agg(payload ORDER BY payload::text),'[]'::jsonb)::text "
            f"FROM (SELECT to_jsonb(item) AS payload FROM {table} item "
            "WHERE task_id=$1::uuid) selected",
            task_id,
        )
    return result


@pytest.mark.parametrize(
    "fault", ["none", "approved", "approved_historical_pass", "action_changed"]
)
async def test_real_report_and_task_share_current_green_projection_without_writing(tmp_path, fault):
    async with green_case(
        tmp_path, "s35", local_display=_local_materials(transition=False), transactional=False
    ) as (connection, pool, ids, work, writer, *_):
        await _write_actual(writer, work, ids)
        execution_id = str(
            await connection.fetchval(
                "SELECT id FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"]
            )
        )
        if fault in {"approved", "approved_historical_pass"}:
            await connection.execute(
                "UPDATE qc_review_item SET status='approved',reviewed_at=now() "
                "WHERE task_id=$1::uuid",
                ids["task"],
            )
            if fault == "approved_historical_pass":
                await connection.execute(
                    "UPDATE qc_rule_execution SET human_verdict='pass',"
                    "human_verdict_note='历史人工结论，当前材料缺项仍保留',human_verdict_at=now() "
                    "WHERE id=$1::uuid",
                    execution_id,
                )
        elif fault == "action_changed":
            await connection.execute(
                "UPDATE qc_action_execution SET intent_payload=jsonb_set(intent_payload,"
                "'{purpose}','\"报告读取前动作发生变化\"'::jsonb) WHERE task_id=$1::uuid",
                ids["task"],
            )
        scope = AccessScope(
            actor_user_id=ids["user"], tenant_id=ids["tenant"], permissions=frozenset()
        )
        store = LocalEvidenceArtifactStore(root=tmp_path / "controlled-originals")
        repository = PostgresTaskInsightRepository(pool=pool, artifact_store=store)
        inventory = PostgresTaskRuleInventoryRepository(pool=pool)
        reports = ReportInsightService(
            repository=repository,
            rule_inventory_repository=inventory,
            evidence_store_factory=lambda: store,
        )
        tasks = TaskInsightService(repository=repository, rule_inventory_repository=inventory)
        before = await _facts(connection, ids["task"])
        task_result = await tasks.rule_results(ids["task"], access_scope=scope)
        report = await reports.report_insight(ids["task"], access_scope=scope)
        task_item = next(
            item for item in task_result["items"] if item.get("execution_id") == execution_id
        )
        report_item = next(
            item for item in report["rule_items"] if item.get("execution_id") == execution_id
        )
        assert report_item["green_result"] == task_item["green_result"]
        assert report_item["display_status"] == "other"
        assert report["green_results_incomplete"] is True
        assert report["export_ready"] is False and report["export_allowed"] is False
        assert report["confirm_progress"]["completed"] is False
        if fault == "action_changed":
            assert report_item["green_result"]["linkage_status"] == "evidence_changed"
            assert report_item["green_result"]["subitems"] == []
        else:
            assert report_item["green_result"]["review_status"] == (
                "approved" if fault in {"approved", "approved_historical_pass"} else "pending"
            )
            assert report_item["green_result"]["execution_status"] == "evidence_insufficient"
            assert report_item["green_result"]["subitems"][0]["status"] == "pass"
        with pytest.raises(ReportExportBlockedError):
            await reports.export_html(ids["task"], access_scope=scope)
        with pytest.raises(ReportConfirmationError):
            await reports.confirm_rule(
                ids["task"], execution_id, verdict="pass", access_scope=scope
            )
        assert await _facts(connection, ids["task"]) == before


async def test_review_only_internal_green_check_does_not_grant_public_task_or_image_read(tmp_path):
    async with green_case(
        tmp_path, "s35", local_display=_local_materials(transition=False), transactional=False
    ) as (connection, pool, ids, work, writer, *_):
        await _write_actual(writer, work, ids)
        execution_id = str(
            await connection.fetchval(
                "SELECT id FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"]
            )
        )
        repository = PostgresTaskInsightRepository(
            pool=pool,
            artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "controlled-originals"),
        )
        reviewer = AccessScope(
            actor_user_id=str(uuid4()),
            tenant_id=ids["tenant"],
            permissions=frozenset({"taskObjects:review:tenant"}),
        )
        before = await _facts(connection, ids["task"])
        assert await repository.fetch_task_context(ids["task"], access_scope=reviewer) is None
        assert (
            await repository.read_green_rule_results(
                task_id=ids["task"], execution_ids=[execution_id], access_scope=reviewer
            )
            == {}
        )
        row = await repository.fetch_rule_execution(
            execution_id, task_id=ids["task"], access_scope=reviewer
        )
        assert row["rule_id"] and row["green_confirmation_context"]["task_config"]
        checked = await repository.read_green_confirmation_results(
            task_id=ids["task"], execution_ids=[execution_id], access_scope=reviewer
        )
        assert checked[execution_id]["linkage_status"] == "linked"
        foreign = AccessScope(
            actor_user_id=str(uuid4()),
            tenant_id=str(uuid4()),
            permissions=frozenset({"taskObjects:review:tenant"}),
        )
        assert (
            await repository.fetch_rule_execution(
                execution_id, task_id=ids["task"], access_scope=foreign
            )
            is None
        )
        assert (
            await repository.fetch_rule_execution(
                execution_id, task_id=str(uuid4()), access_scope=reviewer
            )
            is None
        )
        assert (
            await repository.read_green_confirmation_results(
                task_id=ids["task"], execution_ids=[execution_id], access_scope=foreign
            )
            == {}
        )
        assert (
            await repository.read_green_confirmation_results(
                task_id=ids["task"], execution_ids=[execution_id], access_scope=None
            )
            == {}
        )
        assert await _facts(connection, ids["task"]) == before
