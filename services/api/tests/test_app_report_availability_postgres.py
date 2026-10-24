"""Android 报告预览、正式绑定及列表筛选的真实 PostgreSQL 回归。"""

import json
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.api.app.application.report_payloads import stable_report_payload_hash
from services.api.app.application.workstation_tasks import WorkstationTaskListFilter
from services.api.app.infrastructure.db.task_insight_repository import PostgresTaskInsightRepository
from services.api.app.infrastructure.db.workstation_query_repository import (
    PostgresWorkstationQueryRepository,
)
from services.api.tests.test_workstation_task_list_postgres import (
    _ConnectionPool,
    _scope,
    _seed_business_case,
)
from services.api.tests.test_workstation_task_list_postgres import (
    pg_connection as pg_connection,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL",
)


@pytest.fixture(autouse=True)
async def _rollback_test_data(pg_connection):
    """全部业务种子位于外层写事务，成功或失败均整体回滚。"""
    transaction = pg_connection.transaction()
    await transaction.start()
    try:
        yield
    finally:
        await transaction.rollback()


async def _app_case(connection, *, status="review", owner_bound=True):
    """复用隔离库任务图，清除预置结果后切换为 App V2 合同。"""
    case = await _seed_business_case(connection, legacy_status=status)
    await connection.execute(
        "DELETE FROM qc_rule_execution WHERE task_id=$1::uuid", case.ids["legacy"],
    )
    await connection.execute(
        """UPDATE qc_task SET task_config=task_config || '{"workflow_version":"v2"}'::jsonb,
               finalization_agent_run_id=$2::uuid WHERE id=$1::uuid""",
        case.ids["legacy"], case.ids["run"] if owner_bound else None,
    )
    return case


async def _run(connection, case, *, old=False, judge=False):
    run_id = str(uuid4())
    await connection.execute(
        """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status,created_at)
           VALUES($1::uuid,$2::uuid,$3,'报告可用性回归','done',$4)""",
        run_id, case.ids["legacy"], "rule_judge" if judge else "main",
        datetime.now(UTC) + timedelta(days=-1 if old else 1),
    )
    return run_id


async def _capture(connection, case, *, run_id=None, status="captured", task_id=None, epoch=0):
    capture_id = str(uuid4())
    metadata = {"page_type": "home"}
    if epoch is not None:
        metadata["finalization_epoch"] = epoch
    await connection.execute(
        """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,capture_role,
                                  capture_status,metadata)
           VALUES($1::uuid,$2::uuid,$3::uuid,'android','observation',$4,$5::jsonb)""",
        capture_id, task_id or case.ids["legacy"], run_id or case.ids["run"], status,
        json.dumps(metadata),
    )
    return capture_id


async def _result(
    connection, case, *, run_id=None, capture_id=None, status="pass", reason=None,
    bootstrap=False, epoch=None,
):
    judge_input = {"bootstrap": True} if bootstrap else {}
    if epoch is not None:
        judge_input["finalization_epoch"] = epoch
    await connection.execute(
        """INSERT INTO qc_rule_execution(task_id,rule_id,agent_run_id,page_capture_id,
                                          execution_status,judge_strategy,
                                          judge_input,judge_output,started_at,finished_at)
           VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5,'hybrid',
                  $6::jsonb,$7::jsonb,now(),now())""",
        case.ids["legacy"], case.ids["rule_a"], run_id or case.ids["run"], capture_id,
        status, json.dumps(judge_input),
        json.dumps({"reason": reason} if reason else {}),
    )


async def _assert_projection(
    connection, case, *, availability, report_status="ready", expected_run_id=None,
):
    """从公开仓储验证列表、详情、报告入口与分页前筛选使用同一资格。"""
    repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(connection))
    scope = _scope(actor_user_id=case.ids["colleague"], tenant_id=case.ids["tenant"])
    name = f"{case.prefix}-legacy"
    listed = await repository.list_tasks(
        WorkstationTaskListFilter(task_name=name), access_scope=scope,
    )
    detail = await repository.get_task_detail(case.ids["legacy"], access_scope=scope)
    insight_repository = PostgresTaskInsightRepository(pool=_ConnectionPool(connection))
    context = await insight_repository.fetch_task_context(
        case.ids["legacy"], access_scope=scope,
    )
    assert (listed.total, len(listed.items)) == (1, 1)
    assert detail is not None
    assert context is not None
    assert context["app_report_availability"] == availability
    assert context["run_id"] == (expected_run_id or case.ids["run"])
    for item in (listed.items[0], detail):
        assert item.report_availability == availability
        assert item.report_status == report_status
        assert ("report" in item.available_actions) is (availability in {"preview", "final"})
    for selected in ("not_started", "generating", "ready", "failed", "not_applicable"):
        filtered = await repository.list_tasks(
            WorkstationTaskListFilter(task_name=name, report_status=selected, page_size=1),
            access_scope=scope,
        )
        assert filtered.total == int(selected == report_status)
        assert len(filtered.items) == int(selected == report_status)


@pytest.mark.parametrize("owner_bound", [False, True])
@pytest.mark.parametrize(
    "facts,expected",
    [
        ("none", "none"),
        ("empty_report", "none"),
        ("persisted_report", "preview"),
        ("persisted_wrong_task", "none"),
        ("persisted_wrong_run", "none"),
        ("stale_epoch_report", "preview"),
        ("current_capture", "preview"),
        ("pending_capture", "none"),
        ("failed_capture", "none"),
        ("old_capture", "none"),
        ("stale_epoch_capture", "none"),
        ("missing_epoch_capture", "none"),
        ("current_result", "preview"),
        ("evidence_gap", "preview"),
        ("bootstrap_result", "none"),
        ("not_executed_result", "none"),
        ("old_result", "none"),
        ("stale_epoch_result", "none"),
        ("independent_current_capture", "preview"),
        ("independent_old_capture", "none"),
    ],
)
async def test_app_preview_requires_current_run_facts(pg_connection, owner_bound, facts, expected):
    """缺证可供预览；旧轮次、空正文、未执行占位和失败采集均不能假装有报告。"""
    async with pg_connection.transaction():
        case = await _app_case(pg_connection, owner_bound=owner_bound)
        if facts.startswith("stale_epoch"):
            await pg_connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid",
                case.ids["legacy"],
            )
        old_run = await _run(pg_connection, case, old=True)
        judge_run = await _run(pg_connection, case, judge=True)
        if facts.startswith("persisted") or facts == "stale_epoch_report":
            report = {
                "task": {
                    "task_id": case.ids["formal"] if facts == "persisted_wrong_task"
                    else case.ids["legacy"],
                },
                "agent_run_id": old_run if facts == "persisted_wrong_run" else case.ids["run"],
                "report_stage": "final",
                "is_final": True,
                "completion_gate": {"finalization_epoch": 0},
                "summary": {"overall_status": "incomplete"},
                "rules": [{"rule_id": case.ids["rule_a"], "status": "evidence_insufficient"}],
            }
            await pg_connection.execute(
                "UPDATE qc_agent_run SET output_context=$2::jsonb WHERE id=$1::uuid",
                case.ids["run"], json.dumps({"final_report": report}),
            )
        elif facts == "empty_report":
            await pg_connection.execute(
                """UPDATE qc_agent_run SET output_context='{"final_report":{}}'::jsonb
                   WHERE id=$1::uuid""", case.ids["run"],
            )
        elif facts.endswith("capture") and not facts.startswith("independent"):
            await _capture(
                pg_connection, case,
                run_id=old_run if facts == "old_capture" else case.ids["run"],
                status={"pending_capture": "pending", "failed_capture": "failed"}.get(
                    facts, "captured",
                ),
                epoch=None if facts == "missing_epoch_capture" else 0,
            )
        elif facts.startswith("independent"):
            capture_id = await _capture(
                pg_connection, case,
                run_id=old_run if facts == "independent_old_capture" else case.ids["run"],
                # 采集提交状态未同步时，真实判定仍能证明当前轮有可读的业务结果。
                status="pending",
            )
            await _result(pg_connection, case, run_id=judge_run, capture_id=capture_id)
        elif facts != "none":
            await _result(
                pg_connection, case,
                run_id=old_run if facts == "old_result" else case.ids["run"],
                status="evidence_insufficient" if facts == "evidence_gap" else "pass",
                reason="candidate_not_executed" if facts == "not_executed_result" else None,
                bootstrap=facts == "bootstrap_result",
                epoch=0 if facts == "stale_epoch_result" else None,
            )
        await _assert_projection(
            pg_connection, case, availability=expected,
            report_status="ready" if expected == "preview" else "failed",
        )


@pytest.mark.parametrize("facts_on_owner", [False, True])
async def test_app_bound_owner_is_not_replaced_by_newer_main_or_consumer(
    pg_connection, facts_on_owner,
):
    """封存负责人存在时，新主 Run 和消费者均不能替换报告所属轮次。"""
    case = await _app_case(pg_connection)
    newer_main = await _run(pg_connection, case)
    await _run(pg_connection, case, judge=True)
    await _capture(
        pg_connection, case,
        run_id=case.ids["run"] if facts_on_owner else newer_main,
    )

    await _assert_projection(
        pg_connection, case, availability="preview" if facts_on_owner else "none",
        report_status="ready" if facts_on_owner else "failed",
    )


async def test_app_without_bound_owner_uses_latest_main_before_consumer(pg_connection):
    """未冻结负责人时选最新主 Run，不能借旧主 Run 的成果或更新消费者充数。"""
    case = await _app_case(pg_connection, owner_bound=False)
    newer_main = await _run(pg_connection, case)
    await _run(pg_connection, case, judge=True)
    await _capture(pg_connection, case)

    await _assert_projection(
        pg_connection, case, availability="none", report_status="failed",
        expected_run_id=newer_main,
    )
    await _capture(pg_connection, case, run_id=newer_main)
    await _assert_projection(
        pg_connection, case, availability="preview", expected_run_id=newer_main,
    )


async def _aggregate_result(connection, case, *, source_run_id, epoch):
    """以独立证据输入集保留聚合来源，执行 Run 不能覆盖原始生产者或阶段。"""
    work_id = str(uuid4())
    input_hash = uuid4().hex + uuid4().hex
    input_id = f"evidence-input-set:{input_hash}"
    capture_id = await _capture(
        connection, case, run_id=source_run_id, status="pending", epoch=epoch,
    )
    await connection.execute(
        """INSERT INTO qc_evidence_input_set(
               input_set_id,input_set_hash,task_id,source_agent_run_id,rule_id,
               rule_code,rule_contract_hash,ruleset_snapshot_hash,scope,scope_key,
               finalization_epoch,eligibility_snapshot_id,eligibility_snapshot_hash,
               selection_policy_hash,eligible_capture_ids,protocol_version,
               input_set_payload,sealed_at)
           VALUES($1,$2,$3::uuid,$4::uuid,$5::uuid,'app_report_rule','contract-1',
                  'snapshot-1','task','app_report',$6,'eligibility-1','hash-1',
                  'policy-1',$7::jsonb,'evidence-input-set.v1','{}'::jsonb,now())""",
        input_id, input_hash, case.ids["legacy"], source_run_id, case.ids["rule_a"],
        epoch, json.dumps([capture_id]),
    )
    await connection.execute(
        """INSERT INTO qc_aggregate_inspection_work_item(
               id,task_id,evidence_input_set_id,producer_agent_run_id,rule_id,rule_code,
               rule_contract_hash,scope,scope_key,protocol_version,input_hash,
               status,finalization_epoch)
           VALUES($1::uuid,$2::uuid,$3,$4::uuid,$5::uuid,'app_report_rule','contract-1',
                  'task','app_report','aggregate-inspection-work-item.v1',$6,'pending',$7)""",
        work_id, case.ids["legacy"], input_id, source_run_id, case.ids["rule_a"],
        input_hash, epoch,
    )
    await connection.execute(
        """INSERT INTO qc_rule_execution(
               task_id,rule_id,agent_run_id,execution_status,judge_strategy,
               evidence_input_set_id,aggregate_work_item_id)
           VALUES($1::uuid,$2::uuid,$3::uuid,'pass','hybrid',$4,$5::uuid)""",
        case.ids["legacy"], case.ids["rule_a"], case.ids["run"], input_id, work_id,
    )


@pytest.mark.parametrize("source", ["capture", "aggregate"])
@pytest.mark.parametrize("stale", [None, "run", "epoch"])
async def test_app_main_execution_cannot_override_linked_source_identity(
    pg_connection, source, stale,
):
    """执行写在当前主 Run 时，已链接采集或聚合来源仍必须属于当前 Run 和阶段。"""
    async with pg_connection.transaction():
        case = await _app_case(pg_connection)
        old_run = await _run(pg_connection, case, old=True)
        source_run = old_run if stale == "run" else case.ids["run"]
        if source == "capture":
            capture_id = await _capture(
                pg_connection, case, run_id=source_run, status="pending", epoch=0,
            )
            await _result(pg_connection, case, capture_id=capture_id)
        else:
            await _aggregate_result(pg_connection, case, source_run_id=source_run, epoch=0)
        if stale == "epoch":
            await pg_connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid",
                case.ids["legacy"],
            )
        await _assert_projection(
            pg_connection, case, availability="none" if stale else "preview",
            report_status="failed" if stale else "ready",
        )


async def _gate(connection, case, *, epoch=1, passed=True, sequence=1):
    gate_id = f"app-report-gate-{uuid4()}"
    await connection.execute(
        """INSERT INTO qc_completion_gate_snapshot(
               gate_snapshot_id,task_id,finalization_epoch,evaluation_sequence,
               protocol_version,facts_hash,exploration_terminal,no_pending_action,
               no_pending_capture_commit,no_nonterminal_enforced_work_item,
               no_active_session_request,candidate_rule_coverage_terminal,
               no_background_execution,review_policy_satisfied,pending_action_count,
               pending_capture_commit_count,nonterminal_enforced_work_item_count,
               active_session_request_count,candidate_rule_count,terminal_candidate_rule_count,
               background_execution_count,open_review_count,review_generation,
               block_reasons,gate_passed,evaluated_at)
           VALUES($1,$2::uuid,$3,$4,'task-completion-gate.v1',$5,$6,
                  true,true,true,true,true,true,true,0,0,0,0,1,1,0,0,0,$7::jsonb,$6,now())""",
        gate_id, case.ids["legacy"], epoch, sequence, "a" * 64, passed,
        json.dumps([] if passed else ["exploration_not_terminal"]),
    )
    return gate_id


async def _bound_report(connection, case, *, mismatch=None):
    """构造不可变 Gate/Binding/Payload，失配用独立快照表示，不改写审计记录。"""
    gate_id = await _gate(connection, case, passed=mismatch != "failed_gate")
    canonical_id = f"task:{case.ids['legacy']}:run:{case.ids['run']}"
    binding_id = f"unexpected:{uuid4()}" if mismatch == "binding_report_id" else canonical_id
    report = {
        "report_id": canonical_id,
        "report_binding_version": 1,
        "task": {"task_id": case.ids["legacy"]},
        "agent_run_id": case.ids["run"],
        "report_stage": "final",
        "is_final": True,
        "completion_gate": {
            "gate_snapshot_id": gate_id, "finalization_epoch": 1, "gate_passed": True,
        },
    }
    if mismatch == "task_id":
        report["task"]["task_id"] = case.ids["formal"]
    elif mismatch == "run_id":
        report["agent_run_id"] = str(uuid4())
    elif mismatch == "payload_report_id":
        report["report_id"] = f"unexpected:{uuid4()}"
    elif mismatch == "report_version":
        report["report_binding_version"] = 2
    elif mismatch == "preview_stage":
        report["report_stage"] = "preview"
    elif mismatch == "not_final":
        report["is_final"] = False
    elif mismatch == "payload_gate_id":
        report["completion_gate"]["gate_snapshot_id"] = f"other:{uuid4()}"
    elif mismatch == "payload_epoch":
        report["completion_gate"]["finalization_epoch"] = 0
    elif mismatch == "payload_gate_failed":
        report["completion_gate"]["gate_passed"] = False
    payload_hash = stable_report_payload_hash(report)
    await connection.execute(
        """INSERT INTO qc_report_gate_binding(report_id,report_version,report_payload_hash,
               protocol_version,task_id,gate_snapshot_id,finalization_epoch,
               execution_status,quality_outcome)
           VALUES($1,1,$2,'report-gate-binding.v1',$3::uuid,$4,1,'completed','unknown')""",
        binding_id, payload_hash, case.ids["legacy"], gate_id,
    )
    if mismatch != "missing_payload":
        await connection.execute(
            """INSERT INTO qc_report_payload_snapshot(report_id,report_version,
                   report_payload_hash,report_payload) VALUES($1,1,$2,$3::jsonb)""",
            binding_id, payload_hash, json.dumps(report),
        )
    epoch = 1
    if mismatch in {"stale_epoch", "task_gate_id"}:
        epoch = 2 if mismatch == "stale_epoch" else 1
        gate_id = await _gate(connection, case, epoch=epoch, sequence=2)
    await connection.execute(
        """UPDATE qc_task SET finalization_state=$2::text,finalization_epoch=$3,
               completion_gate_snapshot_id=$4,
               exploration_draining_at=CASE WHEN $2='sealed' THEN now() ELSE NULL END,
               session_draining_at=CASE WHEN $2='sealed' THEN now() ELSE NULL END,
               sealed_at=CASE WHEN $2='sealed' THEN now() ELSE NULL END,
               execution_status=CASE WHEN $2='sealed' THEN 'completed' ELSE NULL END,
               quality_outcome=CASE WHEN $2='sealed' THEN 'unknown' ELSE NULL END
           WHERE id=$1::uuid""",
        case.ids["legacy"], "open" if mismatch == "unsealed" else "sealed", epoch, gate_id,
    )


@pytest.mark.parametrize(
    "mismatch",
    [
        None, "failed_gate", "missing_payload", "task_id", "run_id",
        "binding_report_id", "payload_report_id", "report_version", "preview_stage",
        "not_final", "payload_gate_id", "payload_epoch", "payload_gate_failed",
        "stale_epoch", "task_gate_id", "unsealed",
    ],
)
async def test_app_final_report_requires_coherent_current_binding(pg_connection, mismatch):
    """报告正文、Gate、任务、Run 与封存轮次必须一致；不一致时仍保留真实预览。"""
    async with pg_connection.transaction():
        case = await _app_case(pg_connection, status="done")
        await _result(pg_connection, case)
        await _bound_report(pg_connection, case, mismatch=mismatch)
        await _assert_projection(
            pg_connection, case, availability="final" if mismatch is None else "preview",
        )


async def test_app_bound_final_survives_capture_epoch_before_sealing(pg_connection):
    """正式封存阶段提升后，上一阶段的原始采集不应推翻当前有效正式绑定。"""
    async with pg_connection.transaction():
        case = await _app_case(pg_connection, status="done")
        capture_id = await _capture(pg_connection, case, epoch=0)
        await _result(pg_connection, case, capture_id=capture_id, epoch=0)
        await _assert_projection(pg_connection, case, availability="preview")
        await _bound_report(pg_connection, case)
        assert await pg_connection.fetchval(
            "SELECT finalization_epoch FROM qc_task WHERE id=$1::uuid", case.ids["legacy"],
        ) == 1
        assert await pg_connection.fetchval(
            "SELECT metadata->>'finalization_epoch' FROM qc_capture WHERE id=$1::uuid", capture_id,
        ) == "0"
        await _assert_projection(pg_connection, case, availability="final")


@pytest.mark.parametrize("status", ["canceled", "failed"])
async def test_app_terminal_task_preserves_actual_preview_and_status_filter(pg_connection, status):
    """取消或失败不应隐藏已经存在的预览，也不能凭终态制造一份报告。"""
    async with pg_connection.transaction():
        case = await _app_case(pg_connection, status=status)
        await _assert_projection(
            pg_connection, case, availability="none",
            report_status="not_started" if status == "canceled" else "failed",
        )
        await _result(pg_connection, case)
        await _assert_projection(pg_connection, case, availability="preview")


async def test_app_report_filter_respects_permissions_and_page_total(pg_connection):
    """可预览资格应在分页前过滤，并继续遵守租户与本人权限。"""
    async with pg_connection.transaction():
        case = await _app_case(pg_connection)
        await _result(pg_connection, case)
        other_run = str(uuid4())
        await pg_connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','报告分页回归','done')""",
            other_run, case.ids["other_department"],
        )
        await pg_connection.execute(
            """UPDATE qc_task SET status='running',finalization_agent_run_id=$2::uuid,
                   task_config=task_config || '{"workflow_version":"v2"}'::jsonb
               WHERE id=$1::uuid""", case.ids["other_department"], other_run,
        )
        await _capture(
            pg_connection, case, task_id=case.ids["other_department"], run_id=other_run,
        )
        repository = PostgresWorkstationQueryRepository(pool=_ConnectionPool(pg_connection))
        tenant_scope = _scope(actor_user_id=case.ids["owner"], tenant_id=case.ids["tenant"])
        first = await repository.list_tasks(
            WorkstationTaskListFilter(task_name=case.prefix, report_status="ready", page_size=1),
            access_scope=tenant_scope,
        )
        assert (first.total, len(first.items), first.page, first.page_size) == (2, 1, 1, 1)
        assert first.items[0].task_id == case.ids["other_department"]
        second = await repository.list_tasks(
            WorkstationTaskListFilter(
                task_name=case.prefix, report_status="ready", page=2, page_size=1,
            ), access_scope=tenant_scope,
        )
        assert (second.total, len(second.items)) == (2, 1)
        assert second.items[0].task_id == case.ids["legacy"]
        for restricted in (
            _scope(actor_user_id=case.ids["owner"], tenant_id=case.ids["tenant"],
                   permissions=frozenset({"taskObjects:read:own"})),
            _scope(actor_user_id=case.ids["owner"], tenant_id=case.ids["foreign_tenant"]),
        ):
            hidden = await repository.list_tasks(
                WorkstationTaskListFilter(task_name=case.prefix, report_status="ready"),
                access_scope=restricted,
            )
            assert (hidden.total, hidden.items) == (0, [])
            assert await repository.get_task_detail(
                case.ids["legacy"], access_scope=restricted,
            ) is None
            assert await PostgresTaskInsightRepository(
                pool=_ConnectionPool(pg_connection),
            ).fetch_task_context(case.ids["legacy"], access_scope=restricted) is None
