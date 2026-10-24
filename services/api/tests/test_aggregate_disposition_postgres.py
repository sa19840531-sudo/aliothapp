"""真实 PostgreSQL 上验证聚合候选「证据不足」终态：留下规则执行与复核项，收口不再卡死。

09-11 审查 P0-03：证据全部被遮挡原来借用「不适用」解除阻塞，其余没有可用证据的处置记 needs_review，
收口门禁把它算未结算、又没有可处理的复核项，任务停在收口出不来。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import json
import os
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.aggregate_inspection_execution import (
    AggregateWakeupClaimDisposition,
)
from services.api.app.application.aggregate_inspection_publication import (
    AggregatePublicationSnapshot,
    build_aggregate_rule_source,
)
from services.api.app.domain.task import CompletionBlockReason, TaskExecutionStatus
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    AggregateInspectionPublicationPersistenceError,
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    _GATE_FACTS_SQL,
    _parse_gate_facts,
    _should_mark_task_review,
)

_REJECTED = "AGGREGATE_PRESENTATION_EVIDENCE_REJECTED"
_UNPROVEN = "AGGREGATE_APPLICABILITY_UNPROVEN"


async def test_evidence_insufficient_candidates_close_through_review() -> None:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule_toast", "rule_group", "task", "run")
    }
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        toast = _rule_source(ids["rule_toast"], "alioth_agg_toast", "toast_style")
        group = _rule_source(ids["rule_group"], "alioth_agg_group", "official_group_entry")
        # 旧版本留下的两种处置：被遮挡借用的「不适用」、没采到适用页面的 needs_review
        await _seed_legacy_candidate(connection, ids, toast, "not_applicable", _REJECTED)
        await _seed_legacy_candidate(connection, ids, group, "needs_review", _UNPROVEN)

        stuck = _parse_gate_facts(await _gate_facts(connection, ids["task"]))
        assert CompletionBlockReason.NONTERMINAL_ENFORCED_WORK_ITEM in stuck.block_reasons
        assert stuck.open_review_count == 0
        # 旧口径下只有 needs_review 候选时既过不了门禁，也进不了人工复核
        assert not _should_mark_task_review(
            execution_status=TaskExecutionStatus.NEEDS_REVIEW, facts=stuck
        )

        repository = PostgresAggregateInspectionPublicationRepository(pool=pool)
        snapshot = AggregatePublicationSnapshot(
            task_id=ids["task"],
            source_agent_run_id=ids["run"],
            finalization_epoch=0,
            ruleset_snapshot_hash="snapshot-1",
            rules=(toast, group),
            evidence=(),
        )
        for rule, code in ((toast, _REJECTED), (group, _UNPROVEN)):
            await repository.persist_disposition(
                snapshot=snapshot,
                rule=rule,
                status="evidence_insufficient",
                expected_member_count=0,
                code=code,
                message=f"{code} 已转人工确认",
                scope_plan=None,
            )

        candidates = await connection.fetch(
            """
            SELECT rule_code, status, disposition_code, finished_at
            FROM qc_aggregate_inspection_candidate
            WHERE task_id = $1::uuid
            ORDER BY rule_code
            """,
            ids["task"],
        )
        assert [(row["rule_code"], row["status"]) for row in candidates] == [
            ("alioth_agg_group", "evidence_insufficient"),
            ("alioth_agg_toast", "evidence_insufficient"),
        ]
        assert all(row["finished_at"] is not None for row in candidates)

        executions = await connection.fetch(
            """
            SELECT
                execution.id::text AS execution_id,
                rule.rule_code,
                execution.execution_status,
                execution.quality_outcome,
                execution.agent_run_id::text AS agent_run_id,
                execution.page_capture_id,
                execution.error_code,
                execution.judge_input::text AS judge_input
            FROM qc_rule_execution AS execution
            INNER JOIN qc_rule AS rule ON rule.id = execution.rule_id
            WHERE execution.task_id = $1::uuid
            ORDER BY rule.rule_code
            """,
            ids["task"],
        )
        assert len(executions) == 2
        assert {row["execution_status"] for row in executions} == {"evidence_insufficient"}
        assert {row["quality_outcome"] for row in executions} == {"evidence_gap"}
        assert {row["agent_run_id"] for row in executions} == {ids["run"]}
        assert all(row["page_capture_id"] is None for row in executions)
        assert [row["error_code"] for row in executions] == [_UNPROVEN, _REJECTED]
        assert json.loads(executions[1]["judge_input"])["source"] == (
            "aggregate_candidate_disposition"
        )
        reviews = await connection.fetch(
            "SELECT status, trigger_reason FROM qc_review_item WHERE task_id = $1::uuid",
            ids["task"],
        )
        assert [(row["status"], row["trigger_reason"]) for row in reviews] == [
            ("pending", "evidence_insufficient"),
            ("pending", "evidence_insufficient"),
        ]

        # 候选按终态计，不再算未结算；人工部分交给复核策略，任务可以进复核
        facts = _parse_gate_facts(await _gate_facts(connection, ids["task"]))
        assert facts.nonterminal_enforced_work_item_count == 0
        assert facts.open_review_count == 2
        assert CompletionBlockReason.NONTERMINAL_ENFORCED_WORK_ITEM not in facts.block_reasons
        assert CompletionBlockReason.REVIEW_POLICY_UNSATISFIED in facts.block_reasons
        assert _should_mark_task_review(
            execution_status=TaskExecutionStatus.NEEDS_REVIEW, facts=facts
        )

        # 人工处理完一条后，收口对账重放不会把它重新打开，也不会多写
        toast_execution = executions[1]["execution_id"]
        await connection.execute(
            """
            UPDATE qc_review_item
            SET status = 'approved', reviewed_at = now()
            WHERE rule_execution_id = $1::uuid
            """,
            toast_execution,
        )
        for rule, code in ((toast, _REJECTED), (group, _UNPROVEN)):
            await repository.persist_disposition(
                snapshot=snapshot,
                rule=rule,
                status="evidence_insufficient",
                expected_member_count=0,
                code=code,
                message=f"{code} 已转人工确认",
                scope_plan=None,
            )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM qc_rule_execution WHERE task_id = $1::uuid", ids["task"]
            )
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_review_item WHERE rule_execution_id = $1::uuid",
                toast_execution,
            )
            == "approved"
        )

        # 同一轮换了处置码仍是身份漂移，照旧拒写
        with pytest.raises(AggregateInspectionPublicationPersistenceError, match="漂移"):
            await repository.persist_disposition(
                snapshot=snapshot,
                rule=group,
                status="evidence_insufficient",
                expected_member_count=0,
                code="AGGREGATE_RULE_SCOPE_CONFLICT",
                message="换了处置码",
                scope_plan=None,
            )
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute(
            "DELETE FROM qc_rule WHERE id = ANY($1::uuid[])",
            [ids["rule_toast"], ids["rule_group"]],
        )
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


async def test_exhausted_aggregate_judge_closes_through_review() -> None:
    """聚合 Judge 重试耗尽只记 failed 时同样卡收口：改为留下检测异常执行与复核项。"""
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")

    ids = {
        name: str(uuid4())
        for name in ("tenant", "user", "ruleset", "rule_toast", "rule_group", "task", "run")
    }
    work_id = str(uuid4())
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_task(connection, ids)
        # 只保留一条聚合规则，门禁的期望候选数与已发布候选一一对应
        await connection.execute(
            "UPDATE qc_rule SET status = 'disabled' WHERE id = $1::uuid", ids["rule_group"]
        )
        input_hash = "a" * 64
        input_set_id = f"evidence-input-set:{'b' * 64}"
        await connection.execute(
            """
            INSERT INTO qc_evidence_input_set (
                input_set_id, input_set_hash, task_id, source_agent_run_id,
                rule_id, rule_code, rule_contract_hash, ruleset_snapshot_hash,
                scope, scope_key, finalization_epoch, eligibility_snapshot_id,
                eligibility_snapshot_hash, selection_policy_hash,
                eligible_capture_ids, protocol_version, input_set_payload, sealed_at
            ) VALUES (
                $1, $2, $3::uuid, $4::uuid,
                $5::uuid, 'alioth_agg_toast', 'contract-1', 'snapshot-1',
                'task', 'toast_style', 0, 'eligibility-1',
                'eligibility-hash-1', 'policy-hash-1',
                '["capture-1"]'::jsonb, 'evidence-input-set.v1', '{}'::jsonb, now()
            )
            """,
            input_set_id,
            "b" * 64,
            ids["task"],
            ids["run"],
            ids["rule_toast"],
        )
        await connection.execute(
            """
            INSERT INTO qc_aggregate_inspection_work_item (
                id, task_id, evidence_input_set_id, producer_agent_run_id,
                rule_id, rule_code, rule_contract_hash, scope, scope_key,
                protocol_version, input_hash, status, attempt_count, max_attempts,
                next_attempt_at, finalization_epoch
            ) VALUES (
                $1::uuid, $2::uuid, $3, $4::uuid,
                $5::uuid, 'alioth_agg_toast', 'contract-1', 'task', 'toast_style',
                'aggregate-inspection-work-item.v1', $6, 'retry_wait', 3, 3,
                now() - interval '1 minute', 0
            )
            """,
            work_id,
            ids["task"],
            input_set_id,
            ids["run"],
            ids["rule_toast"],
            input_hash,
        )
        await connection.execute(
            """
            INSERT INTO qc_aggregate_inspection_candidate (
                task_id, source_agent_run_id, rule_id, rule_code, rule_contract_hash,
                scope, scope_key, finalization_epoch, status, expected_member_count,
                evidence_input_set_id, work_item_id
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, 'alioth_agg_toast', 'contract-1',
                'task', 'toast_style', 0, 'published', 1, $4, $5::uuid
            )
            """,
            ids["task"],
            ids["run"],
            ids["rule_toast"],
            input_set_id,
            work_id,
        )

        claim = await PostgresAggregateInspectionExecutionRepository(pool=pool).claim_for_wakeup(
            work_item_id=work_id,
            task_id=ids["task"],
            message_version=0,
            lease_owner="test-owner",
            lease_seconds=30,
        )

        assert claim.disposition is AggregateWakeupClaimDisposition.COMPLETED
        candidate = await connection.fetchrow(
            """
            SELECT status, disposition_code
            FROM qc_aggregate_inspection_candidate
            WHERE work_item_id = $1::uuid
            """,
            work_id,
        )
        assert (candidate["status"], candidate["disposition_code"]) == (
            "completed",
            "AGGREGATE_ATTEMPTS_EXHAUSTED",
        )
        execution = await connection.fetchrow(
            """
            SELECT id::text AS execution_id, execution_status, quality_outcome,
                   agent_run_id::text AS agent_run_id, evidence_input_set_id, problem_desc
            FROM qc_rule_execution
            WHERE aggregate_work_item_id = $1::uuid
            """,
            work_id,
        )
        assert execution["execution_status"] == "error"
        assert execution["quality_outcome"] == "inconclusive"
        assert execution["agent_run_id"] == ids["run"]
        assert execution["evidence_input_set_id"] == input_set_id
        assert "已转人工确认" in execution["problem_desc"]
        review = await connection.fetchrow(
            "SELECT status, trigger_reason FROM qc_review_item WHERE rule_execution_id = $1::uuid",
            execution["execution_id"],
        )
        assert (review["status"], review["trigger_reason"]) == ("pending", "manual_flag")

        facts = _parse_gate_facts(await _gate_facts(connection, ids["task"]))
        assert facts.nonterminal_enforced_work_item_count == 0
        assert facts.open_review_count == 1
        assert _should_mark_task_review(
            execution_status=TaskExecutionStatus.NEEDS_REVIEW, facts=facts
        )
    finally:
        await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
        await connection.execute(
            "DELETE FROM qc_rule WHERE id = ANY($1::uuid[])",
            [ids["rule_toast"], ids["rule_group"]],
        )
        await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
        await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
        await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])
        await pool.close()
        await connection.close()


def _judge_config(scope_key: str) -> dict:
    return {
        "capability_config": {
            "evidence_input_scope": "task",
            "evidence_scope_key": scope_key,
        }
    }


def _rule_source(rule_id: str, rule_code: str, scope_key: str):
    return build_aggregate_rule_source(
        {
            "rule_id": rule_id,
            "rule_code": rule_code,
            "category": "interaction",
            "executor_type": "hybrid",
            "severity": "high",
            "check_point": "聚合检查",
            "input_contract": {},
            "judge_config": _judge_config(scope_key),
            "manual_review_policy": {},
            "steps": [],
            "applicable_page_types": ["profile"],
        }
    )


async def _gate_facts(connection: asyncpg.Connection, task_id: str):
    row = await connection.fetchrow(_GATE_FACTS_SQL, task_id, "needs_review")
    assert row is not None
    return dict(row)


async def _seed_legacy_candidate(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    rule,
    status: str,
    code: str,
) -> None:
    from services.api.app.application.inspection_work_items import (
        build_inspection_rule_contract_hash,
    )

    await connection.execute(
        """
        INSERT INTO qc_aggregate_inspection_candidate (
            task_id, source_agent_run_id, rule_id, rule_code, rule_contract_hash,
            scope, scope_key, finalization_epoch, status, expected_member_count,
            disposition_code, disposition_message, finished_at
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4, $5,
            'task', $6, 0, $7, 0, $8, '旧版本写下的处置', now()
        )
        """,
        ids["task"],
        ids["run"],
        rule.rule_id,
        rule.rule_code,
        build_inspection_rule_contract_hash(rule.rule_payload),
        rule.evidence.scope_key,
        status,
        code,
    )


async def _seed_task(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute(
        """
        INSERT INTO qc_tenant (id, tenant_key, display_name)
        VALUES ($1::uuid, $2, 'Aggregate Disposition')
        """,
        ids["tenant"],
        f"aggregate-disposition-{ids['tenant']}",
    )
    await connection.execute(
        """
        INSERT INTO qc_user (id, username, full_name, password_hash, role, tenant_id)
        VALUES ($1::uuid, $2, 'Aggregate Disposition', 'not-a-login-secret', '平台管理员', $3::uuid)
        """,
        ids["user"],
        f"aggregate-disposition-{ids['user']}",
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_ruleset (
            id, ruleset_code, name, version, status, snapshot_hash, rule_count, source_type
        )
        VALUES ($1::uuid, $2, 'Aggregate Disposition', 1, 'enabled', 'snapshot-1', 2, 'manual')
        """,
        ids["ruleset"],
        f"aggregate-disposition-{ids['ruleset']}",
    )
    for key, code, scope_key in (
        ("rule_toast", "alioth_agg_toast", "toast_style"),
        ("rule_group", "alioth_agg_group", "official_group_entry"),
    ):
        await connection.execute(
            """
            INSERT INTO qc_rule (
                id, ruleset_id, rule_code, name, category, executor_type,
                severity, status, check_point, judge_config, applicable_page_types
            )
            VALUES (
                $1::uuid, $2::uuid, $3, 'Aggregate Rule',
                'interaction', 'hybrid', 'high', 'enabled', '聚合检查', $4::jsonb,
                '["profile"]'::jsonb
            )
            """,
            ids[key],
            ids["ruleset"],
            code,
            json.dumps(_judge_config(scope_key)),
        )
    # 收口中：生命周期约束要求 exploration_draining_at 与 session_draining_at 都已落时间
    await connection.execute(
        """
        INSERT INTO qc_task (
            id, target_url, ruleset_id, status, submitter_id, tenant_id,
            finalization_state, exploration_draining_at, session_draining_at
        )
        VALUES (
            $1::uuid, 'https://example.com', $2::uuid, 'running', $3::uuid, $4::uuid,
            'session_draining', now(), now()
        )
        """,
        ids["task"],
        ids["ruleset"],
        ids["user"],
        ids["tenant"],
    )
    await connection.execute(
        """
        INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
        VALUES ($1::uuid, $2::uuid, 'main', 'Aggregate Disposition', 'done')
        """,
        ids["run"],
        ids["task"],
    )
    await connection.execute(
        "UPDATE qc_task SET finalization_agent_run_id = $2::uuid WHERE id = $1::uuid",
        ids["task"],
        ids["run"],
    )
