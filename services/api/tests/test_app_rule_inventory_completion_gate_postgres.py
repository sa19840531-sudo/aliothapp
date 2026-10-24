"""真实 PostgreSQL 验证整份规则清单不能被已有少量判定替代。"""

import json
import os
from uuid import uuid4

import pytest

from services.api.app.application.task_finalization import (
    EvaluateCompletionGateCommand,
    TerminateTaskFinalizationCommand,
)
from services.api.app.application.task_rule_inventory import build_task_rule_inventory
from services.api.app.domain.task.completion import (
    CompletionGateFacts,
    TaskExecutionStatus,
    TaskQualityOutcome,
)
from services.api.app.infrastructure.db.app_rule_inventory_completion_gate import (
    apply_app_rule_inventory_gate,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
    _read_completion_gate_facts,
)
from services.api.tests.test_app_evidence_index_postgres_integration import (
    _database,
    _insert_capture,
    _SingleConnectionPool,
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库完整迁移
)

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="必须显式提供隔离测试数据库",
)


def _facts(*, candidates=1, terminal=1):
    return CompletionGateFacts(
        exploration_terminal=True, pending_action_count=0,
        pending_capture_commit_count=0, nonterminal_enforced_work_item_count=0,
        active_session_request_count=0, candidate_rule_count=candidates,
        terminal_candidate_rule_count=terminal, background_execution_count=0,
        review_policy_satisfied=True, require_nonempty_candidate_inventory=True,
    )


async def _inventory(connection, ids, *, all_excluded=False):
    rules = []
    for index, (clients, pages) in enumerate([
        (["WEB"] if all_excluded else ["APP"], ["home"]),
        (["WEB"], ["home"]),
        (["APP"], ["profile"]),
    ]):
        rule_id = str(uuid4())
        rule = {
            "rule_id": rule_id, "rule_code": f"inventory_{index}",
            "name": f"清单规则{index}", "category": "text", "executor_type": "llm",
            "severity": "medium", "check_point": "核对页面文字",
            "applicable_page_types": pages, "input_contract": {"target_clients": clients},
            "judge_config": {}, "manual_review_policy": {}, "steps": [],
            "sort_order": index, "channel": None,
        }
        await connection.execute(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type, check_point,
                applicable_page_types, input_contract
            ) VALUES ($1::uuid, $2::uuid, $3, $4, 'text', 'llm', '核对页面文字',
                      $5::jsonb, $6::jsonb)
            """,
            rule_id, ids["ruleset"], rule["rule_code"], rule["name"],
            json.dumps(pages), json.dumps(rule["input_contract"]),
        )
        rules.append(rule)
    config = {
        "workflow_version": "v2", "execution_mode": "full_inspection",
        "inspection_scope": {"page_types": ["home"]}, "ruleset_rule_total": len(rules),
    }
    config["rule_inventory"] = build_task_rule_inventory(
        ruleset_id=ids["ruleset"], ruleset_version=1,
        ruleset_snapshot_hash="app-evidence-index-test", target_platform="app",
        channel="workbench", task_config=config, rules=rules,
    )
    await connection.execute(
        """UPDATE qc_task SET task_config = task_config || $2::jsonb,
             finalization_agent_run_id = $3::uuid WHERE id = $1::uuid""",
        ids["task"], json.dumps(config), ids["run"],
    )
    row = {
        "inventory_task_config": config, "inventory_target_platform": "app",
        "inventory_ruleset_id": ids["ruleset"], "inventory_channel": "workbench",
    }
    return row, rules[0]["rule_id"]


async def _execution(connection, ids, rule_id, *, mode):
    capture = await _insert_capture(
        connection, ids, capture_role="observation", semantic_role="final_observation",
    )
    await connection.execute(
        "UPDATE qc_capture SET metadata = metadata || '{\"finalization_epoch\": 0}'::jsonb "
        "WHERE id = $1::uuid", capture,
    )
    writer = ids["run"]
    if mode in {"judge", "old_run"}:
        writer = str(uuid4())
        await connection.execute(
            """INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
               VALUES ($1::uuid, $2::uuid, 'rule_judge', '独立判定', 'running')""",
            writer, ids["task"],
        )
    if mode == "old_run":
        await connection.execute(
            "UPDATE qc_capture SET agent_run_id = $2::uuid WHERE id = $1::uuid",
            capture, writer,
        )
    if mode == "old_epoch":
        await connection.execute(
            "UPDATE qc_task SET finalization_epoch = 1 WHERE id = $1::uuid", ids["task"],
        )
    status = {
        "insufficient": "evidence_insufficient", "legacy_skipped": "skipped",
        "error": "error",
        "not_applicable": "skipped", "bootstrap": "skipped",
    }.get(mode, "pass")
    await connection.execute(
        """
        INSERT INTO qc_rule_execution (
            task_id, rule_id, page_capture_id, agent_run_id, execution_status,
            judge_strategy, judge_input, error_code
        ) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, $5, 'llm', $6::jsonb, $7)
        """,
        ids["task"], rule_id, capture, writer, status,
        json.dumps({"bootstrap": True} if mode == "bootstrap" else {}),
        "SUPPLEMENTAL_EVIDENCE_DISABLED" if mode == "legacy_skipped" else None,
    )
    return capture


@pytest.mark.parametrize(("mode", "missing"), [
    ("none", 1), ("pass", 0), ("judge", 0), ("old_run", 1),
    ("old_epoch", 1), ("bootstrap", 1), ("insufficient", 1),
    ("legacy_skipped", 1), ("not_applicable", 0), ("multiple", 0),
])
async def test_full_inventory_needs_current_real_conclusion(mode, missing):
    async with _database() as (connection, ids):
        row, rule_id = await _inventory(connection, ids)
        if mode != "none":
            await _execution(connection, ids, rule_id, mode=mode)
        if mode == "multiple":
            await _execution(connection, ids, rule_id, mode="pass")
        before = await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        )
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert result.candidate_rule_count == 1 + missing
        assert result.terminal_candidate_rule_count == 1
        assert result.gate_passed is (missing == 0)
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_rule_execution WHERE task_id=$1::uuid", ids["task"],
        ) == before


async def test_valid_all_excluded_inventory_is_explicit_no_rules_applicable():
    async with _database() as (connection, ids):
        row, _ = await _inventory(connection, ids, all_excluded=True)
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(candidates=0, terminal=0),
        )
        assert result.no_rules_applicable is True
        assert result.gate_passed is True


@pytest.mark.parametrize("incomplete", ["insufficient", "legacy_skipped", "error"])
async def test_one_page_pass_does_not_hide_another_page_gap(incomplete):
    """同一规则的多页执行中，一次通过不能覆盖另一页的真实检测缺口。"""
    async with _database() as (connection, ids):
        row, rule_id = await _inventory(connection, ids)
        await _execution(connection, ids, rule_id, mode="pass")
        await _execution(connection, ids, rule_id, mode=incomplete)
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert result.candidate_rule_count == 2
        assert result.gate_passed is False


async def test_current_conclusion_can_replace_bootstrap_placeholder():
    """同当前轮次真实结论仍可替代没有业务判定的启动占位。"""
    async with _database() as (connection, ids):
        row, rule_id = await _inventory(connection, ids)
        await _execution(connection, ids, rule_id, mode="bootstrap")
        await _execution(connection, ids, rule_id, mode="pass")
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert result.candidate_rule_count == 1
        assert result.gate_passed is True


@pytest.mark.parametrize(("occluded_page", "complete"), [
    ("home", True), ("content_detail", False), (None, False),
])
async def test_occluded_gap_only_replaced_by_current_same_page_type(occluded_page, complete):
    """遮挡缺口只能由当前范围同页型真实结论替代，未知页型不能猜测一致。"""
    async with _database() as (connection, ids):
        row, rule_id = await _inventory(connection, ids)
        concluded = await _execution(connection, ids, rule_id, mode="pass")
        occluded = await _execution(connection, ids, rule_id, mode="insufficient")
        for capture, page_type in ((concluded, "home"), (occluded, occluded_page)):
            await connection.execute(
                "UPDATE qc_capture SET metadata=metadata || $2::jsonb WHERE id=$1::uuid",
                capture, json.dumps({"page_type": page_type}),
            )
        await connection.execute(
            """UPDATE qc_rule_execution SET judge_output=$2::jsonb
               WHERE page_capture_id=$1::uuid""",
            occluded, json.dumps({"presentation_rejection": {"reason_code": "BASE_PAGE_OCCLUDED"}}),
        )
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert result.gate_passed is complete


async def test_execution_explicit_epoch_conflict_cannot_complete_inventory():
    """执行显式标记旧轮次时，不借当前 Capture 的身份放行。"""
    async with _database() as (connection, ids):
        row, rule_id = await _inventory(connection, ids)
        capture = await _execution(connection, ids, rule_id, mode="pass")
        await connection.execute(
            """UPDATE qc_rule_execution SET judge_input='{"finalization_epoch":1}'::jsonb
               WHERE page_capture_id=$1::uuid""", capture,
        )
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert result.gate_passed is False


async def test_corrupt_inventory_cannot_be_treated_as_empty():
    async with _database() as (connection, ids):
        row, _ = await _inventory(connection, ids)
        row["inventory_task_config"]["rule_inventory"]["rules"] = []
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert result.gate_passed is False


@pytest.mark.parametrize(("task_epoch", "missing"), [(0, 0), (1, 1)])
async def test_aggregate_inventory_requires_linked_current_producer_epoch(task_epoch, missing):
    """无页面锚点的聚合结论仅通过精确工作项关联与当前生产者阶段补齐清单。"""
    async with _database() as (connection, ids):
        row, rule_id = await _inventory(connection, ids)
        work_id, judge_id = str(uuid4()), str(uuid4())
        input_set_hash = uuid4().hex + uuid4().hex
        input_set_id = f"evidence-input-set:{input_set_hash}"
        capture_id = await _insert_capture(
            connection, ids, capture_role="observation", semantic_role="final_observation",
        )
        await connection.execute(
            """INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
               VALUES ($1::uuid, $2::uuid, 'rule_judge', '聚合判定', 'running')""",
            judge_id, ids["task"],
        )
        await connection.execute(
            """
            INSERT INTO qc_evidence_input_set (
                input_set_id, input_set_hash, task_id, source_agent_run_id,
                rule_id, rule_code, rule_contract_hash, ruleset_snapshot_hash,
                scope, scope_key, finalization_epoch, eligibility_snapshot_id,
                eligibility_snapshot_hash, selection_policy_hash,
                eligible_capture_ids, protocol_version, input_set_payload, sealed_at
            ) VALUES (
                $1, $2, $3::uuid, $4::uuid, $5::uuid, 'inventory_0', 'contract-1',
                'app-evidence-index-test', 'task', 'inventory_aggregate', $6,
                'eligibility-1', 'eligibility-hash-1', 'policy-hash-1',
                $7::jsonb, 'evidence-input-set.v1', '{}'::jsonb, now()
            )
            """,
            input_set_id, input_set_hash, ids["task"], ids["run"], rule_id, 0,
            json.dumps([capture_id]),
        )
        await connection.execute(
            """
            INSERT INTO qc_aggregate_inspection_work_item (
                id, task_id, evidence_input_set_id, producer_agent_run_id,
                rule_id, rule_code, rule_contract_hash, scope, scope_key,
                protocol_version, input_hash, status, finalization_epoch,
                result_payload, result_hash, finished_at
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4::uuid, $5::uuid, 'inventory_0', 'contract-1',
                'task', 'inventory_aggregate', 'aggregate-inspection-work-item.v1',
                $6, 'succeeded', $7, '{"execution_status":"pass"}'::jsonb, $6, now()
            )
            """,
            work_id, ids["task"], input_set_id, ids["run"], rule_id, "b" * 64, 0,
        )
        await connection.execute(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, agent_run_id, execution_status, judge_strategy,
                judge_input, evidence_input_set_id, aggregate_work_item_id
            ) VALUES ($1::uuid, $2::uuid, $3::uuid, 'pass', 'llm', '{}'::jsonb, $4, $5::uuid)
            """,
            ids["task"], rule_id, judge_id, input_set_id, work_id,
        )
        if task_epoch:
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=$2 WHERE id=$1::uuid",
                ids["task"], task_epoch,
            )
        result = await apply_app_rule_inventory_gate(
            connection, task_id=ids["task"], row=row, facts=_facts(),
        )
        assert result.candidate_rule_count == 1 + missing
        assert result.terminal_candidate_rule_count == 1
        assert result.gate_passed is (missing == 0)


async def test_authoritative_precheck_reads_full_inventory_gap():
    async with _database() as (connection, ids):
        await _inventory(connection, ids)
        _, facts = await _read_completion_gate_facts(
            connection,
            EvaluateCompletionGateCommand(
                task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0,
                expected_row_version=0, execution_status=TaskExecutionStatus.COMPLETED,
                quality_outcome=TaskQualityOutcome.PASS,
            ),
        )
        assert facts.candidate_rule_count == 1
        assert facts.terminal_candidate_rule_count == 0
        assert facts.gate_passed is False


async def test_terminal_partial_task_preserves_inventory_gap_without_claiming_success():
    """主运行确实终止后仍可封存部分成果，但必须保留未通过门禁和未知质量。"""
    async with _database() as (connection, ids):
        await _inventory(connection, ids)
        await connection.execute(
            """UPDATE qc_task SET status='running',finalization_state='session_draining',
                   exploration_draining_at=now(),session_draining_at=now()
               WHERE id=$1::uuid""", ids["task"],
        )
        await connection.execute(
            "UPDATE qc_agent_run SET status='done',finished_at=now() WHERE id=$1::uuid",
            ids["run"],
        )
        finalization = await PostgresTaskFinalizationRepository(
            pool=_SingleConnectionPool(connection),
        ).terminate(command=TerminateTaskFinalizationCommand(
            task_id=ids["task"], agent_run_id=ids["run"],
            expected_finalization_epoch=0, expected_row_version=0,
            execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            reason_code="APP_INSPECTION_INCOMPLETE",
        ))
        assert finalization.execution_status is TaskExecutionStatus.NEEDS_REVIEW
        assert finalization.quality_outcome is TaskQualityOutcome.UNKNOWN
        gate = await connection.fetchrow(
            """SELECT gate_passed,candidate_rule_count,terminal_candidate_rule_count,block_reasons
               FROM qc_completion_gate_snapshot WHERE gate_snapshot_id=$1""",
            finalization.gate_snapshot_id,
        )
        assert gate["gate_passed"] is False
        assert gate["candidate_rule_count"] == 1
        assert gate["terminal_candidate_rule_count"] == 0
        assert "candidate_rule_coverage_nonterminal" in json.loads(gate["block_reasons"])
