"""真实 PostgreSQL 验证 APP 冻结规则从聚合发布到结果写回的完整链路。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from services.api.app.application.aggregate_inspection_execution import AggregateJudgement
from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.application.aggregate_inspection_work_items import (
    PublishAggregateInspectionWorkItemCommand,
)
from services.api.app.application.task_rule_inventory import TaskRuleInventoryError
from services.api.app.domain.evidence import (
    AppEvidenceIndexEntry,
    AppEvidenceSemanticRole,
    AppExplorationResultOutcome,
    ArtifactReference,
    EvidenceInputScope,
    EvidenceQualityStatus,
    build_app_evidence_index,
    build_evidence_manifest_reference,
    seal_app_evidence_index,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
    PostgresAggregateInspectionExecutionRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.aggregate_inspection_work_item_repository import (
    PostgresAggregateInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.tests.test_task_rule_inventory_postgres import _database


async def _prepare(connection, pool, ids, rules, *, with_evidence=True):
    await connection.execute(
        """UPDATE qc_task SET status='running',finalization_state='session_draining',
               finalization_agent_run_id=$2::uuid,exploration_draining_at=now(),
               session_draining_at=now() WHERE id=$1::uuid""",
        ids["task"], ids["run"],
    )
    if with_evidence:
        await _seed_evidence(connection, pool, ids)
    # 发布、判定及复核优先级均应取提交时合同，不能受这次 live 编辑影响。
    await connection.execute(
        """UPDATE qc_rule SET rule_code='edited_' || rule_code,name='后改规则',
               status='disabled',executor_type='rule_engine',severity='low',
               judge_config='{}',updated_at=now() WHERE id=$1::uuid""",
        rules["aggregate"],
    )
    await connection.execute(
        "UPDATE qc_ruleset SET snapshot_hash=NULL,updated_at=now() WHERE id=$1::uuid",
        ids["ruleset"],
    )
    repository = PostgresAggregateInspectionPublicationRepository(pool=pool)
    summary = await AggregateInspectionPublicationService(
        repository=repository, max_attempts=1,
    ).prepare(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
    return repository, summary


async def _seed_evidence(connection, pool, ids, *, page_type="home"):
    now = datetime.now(UTC)
    session_id, capture_id, artifact_id = f"aggregate-{uuid4()}", str(uuid4()), str(uuid4())
    build_ref = f"app-build:{ids['build']}"
    await connection.execute(
        """INSERT INTO qc_surface_session(session_id,task_id,surface_kind,session_owner,
                   status,platform_build_ref)
           VALUES($1,$2::uuid,'android','aggregate-inventory-test','starting',$3)""",
        session_id, ids["task"], build_ref,
    )
    await connection.execute(
        """INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
                   capture_role,capture_status,captured_at,metadata)
           VALUES($1::uuid,$2::uuid,$3::uuid,'android',$4,'observation','captured',$5,
                  $6::jsonb)""",
        capture_id, ids["task"], ids["run"], session_id, now,
        json.dumps({"page_type": page_type, "page_state": "stable", "finalization_epoch": 0,
                    "semantic_role": "final_observation"}),
    )
    await connection.execute(
        """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,
                   source,storage_path,size_bytes,checksum)
           VALUES($1::uuid,$2::uuid,$3::uuid,'ui_snapshot','baseline',
                  'tests/frozen-aggregate.json',10,$4)""",
        artifact_id, ids["task"], capture_id, "sha256:" + "a" * 64,
    )
    manifest = build_evidence_manifest_reference(
        task_id=ids["task"], session_id=session_id, capture_id=capture_id,
        manifest_version=1,
        artifacts=(ArtifactReference(
            artifact_id=artifact_id, artifact_type="ui_snapshot", role="observation",
            storage_path="tests/frozen-aggregate.json", checksum="sha256:" + "a" * 64,
            size_bytes=10, content_type="application/json",
        ),),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified", created_at=now,
    )
    await connection.execute(
        """INSERT INTO qc_evidence_manifest(manifest_id,manifest_version,task_id,session_id,
                   capture_id,protocol_version,manifest_hash,payload_hash,quality_status,
                   quality_reason,manifest_payload,created_at)
           VALUES($1,1,$2::uuid,$3,$4::uuid,$5,$6,$7,'pass',$8,$9::jsonb,$10)""",
        manifest.manifest_id, ids["task"], session_id, capture_id, manifest.protocol_version,
        manifest.manifest_hash, stable_protocol_hash(manifest.to_dict()),
        manifest.quality_reason, json.dumps(manifest.to_dict()), now,
    )
    index = build_app_evidence_index(
        task_id=ids["task"], run_id=ids["run"], build_id=build_ref,
        surface_session_id=session_id, created_at=now,
        result_context_sha256="sha256:" + "b" * 64,
        entries=(AppEvidenceIndexEntry(
            sequence_no=1, capture_id=capture_id, evidence_manifest_id=manifest.manifest_id,
            evidence_manifest_version=1, semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
            captured_at=now, metadata={"page_type": page_type},
        ),),
    )
    repository = PostgresAppEvidenceIndexRepository(pool=pool)
    await repository.create(index=index)
    await repository.seal(index=seal_app_evidence_index(
        index, outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed", sealed_at=now,
    ))


@pytest.mark.parametrize("outcome", ["pass", "evidence_insufficient", "error"])
async def test_frozen_aggregate_prepare_and_results_ignore_live_edits(outcome) -> None:
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        await _prepare(connection, pool, ids, rules)
        work = await connection.fetchrow(
            "SELECT * FROM qc_aggregate_inspection_work_item WHERE task_id=$1::uuid",
            ids["task"],
        )
        assert work is not None and work["rule_code"] == "inventory_aggregate"
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_evidence_input_set_member WHERE input_set_id=$1",
            work["evidence_input_set_id"],
        ) == 1
        executor = PostgresAggregateInspectionExecutionRepository(pool=pool)
        claim = await executor.claim_for_wakeup(
            work_item_id=str(work["id"]), task_id=ids["task"],
            message_version=work["row_version"], lease_owner="inventory-test", lease_seconds=60,
        )
        assert claim.lease is not None
        if outcome == "error":
            await executor.retry_or_fail(
                lease=claim.lease, error=RuntimeError("测试判定异常"), retry_delay_seconds=0,
            )
        else:
            context = await executor.load_judge_context(lease=claim.lease)
            await executor.complete_judgement(
                context=context,
                judgement=AggregateJudgement(
                    execution_status=outcome, result_summary="冻结合同测试",
                    problem_desc="缺少判定事实" if outcome == "evidence_insufficient" else None,
                    confidence=None, review_status="pending" if outcome != "pass" else None,
                    review_reason="证据不足" if outcome != "pass" else None,
                    error_code=None, wrapper_payload={"conclusion": outcome},
                    evidence_artifact_ids=context.input_set.members[0].artifact_ids,
                ),
            )
        result = await connection.fetchrow(
            """SELECT execution.execution_status,execution.judge_strategy,execution.severity,
                      review.priority,candidate.status AS candidate_status
               FROM qc_rule_execution execution
               LEFT JOIN qc_review_item review ON review.rule_execution_id=execution.id
               JOIN qc_aggregate_inspection_candidate candidate
                 ON candidate.work_item_id=execution.aggregate_work_item_id
               WHERE execution.task_id=$1::uuid""",
            ids["task"],
        )
        assert result is not None
        assert result["execution_status"] == outcome
        assert result["judge_strategy"] == "hybrid" and result["severity"] == "high"
        assert result["priority"] == (None if outcome == "pass" else 20)
        assert result["candidate_status"] == "completed"


@pytest.mark.parametrize("outcome", ["pass", "evidence_insufficient", "error"])
async def test_app_aggregate_terminal_wakes_same_epoch_blocked_gate(outcome) -> None:
    """聚合真实终态必须唤醒已停止扫描的 APP 门禁。"""
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        await _prepare(connection, pool, ids, rules)
        from services.api.app.domain.task.completion import (
            CompletionGateFacts,
            build_completion_gate_snapshot,
        )
        from services.api.app.infrastructure.db.task_finalization_repository import (
            _insert_gate_snapshot,
        )

        gate = build_completion_gate_snapshot(
            task_id=ids["task"], finalization_epoch=0, evaluation_sequence=1,
            evaluated_at=datetime.now(UTC),
            facts=CompletionGateFacts(
                exploration_terminal=True, pending_action_count=0,
                pending_capture_commit_count=0, nonterminal_enforced_work_item_count=1,
                active_session_request_count=0, candidate_rule_count=1,
                terminal_candidate_rule_count=0, background_execution_count=0,
                review_policy_satisfied=True,
            ),
        )
        await _insert_gate_snapshot(connection, gate)
        await connection.execute(
            """UPDATE qc_task SET completion_gate_snapshot_id=$2,
                   finalization_retry_at='infinity' WHERE id=$1::uuid""",
            ids["task"], gate.gate_snapshot_id,
        )
        work = await connection.fetchrow(
            "SELECT * FROM qc_aggregate_inspection_work_item WHERE task_id=$1::uuid",
            ids["task"],
        )
        executor = PostgresAggregateInspectionExecutionRepository(pool=pool)
        claim = await executor.claim_for_wakeup(
            work_item_id=str(work["id"]), task_id=ids["task"],
            message_version=work["row_version"], lease_owner="wake-test", lease_seconds=60,
        )
        assert claim.lease is not None
        if outcome == "error":
            await executor.retry_or_fail(
                lease=claim.lease, error=RuntimeError("测试重试耗尽"), retry_delay_seconds=0,
            )
        else:
            context = await executor.load_judge_context(lease=claim.lease)
            await executor.complete_judgement(
                context=context,
                judgement=AggregateJudgement(
                    execution_status=outcome, result_summary="聚合唤醒测试",
                    problem_desc=None, confidence=None,
                    review_status="pending" if outcome != "pass" else None,
                    review_reason="证据不足" if outcome != "pass" else None,
                    error_code=None, wrapper_payload={"conclusion": outcome},
                    evidence_artifact_ids=context.input_set.members[0].artifact_ids,
                ),
            )
        assert await connection.fetchval(
            "SELECT finalization_retry_at <= now() FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        ) is True
        from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
            _wake_app_finalization,
        )

        shifted_gate = build_completion_gate_snapshot(
            task_id=ids["task"], finalization_epoch=1, evaluation_sequence=2,
            evaluated_at=datetime.now(UTC), facts=gate.facts,
        )
        await _insert_gate_snapshot(connection, shifted_gate)
        changes = (
            f"finalization_epoch=1,completion_gate_snapshot_id='{shifted_gate.gate_snapshot_id}'",
            "finalization_agent_run_id=NULL", "deleted_at=now()",
        )
        for change in changes:
            await connection.execute(
                f"UPDATE qc_task SET finalization_retry_at='infinity',{change} WHERE id=$1::uuid",
                ids["task"],
            )
            await _wake_app_finalization(connection, work_item_id=str(work["id"]))
            assert await connection.fetchval(
                "SELECT finalization_retry_at='infinity' FROM qc_task WHERE id=$1::uuid",
                ids["task"],
            ) is True
            await connection.execute(
                """UPDATE qc_task SET finalization_epoch=0,finalization_agent_run_id=$2::uuid,
                       completion_gate_snapshot_id=$3,deleted_at=NULL WHERE id=$1::uuid""",
                ids["task"], ids["run"], gate.gate_snapshot_id,
            )


async def test_frozen_aggregate_missing_evidence_records_original_contract() -> None:
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        await _prepare(connection, pool, ids, rules, with_evidence=False)
        result = await connection.fetchrow(
            """SELECT execution.execution_status,execution.judge_strategy,execution.severity,
                      review.priority
               FROM qc_rule_execution execution
               JOIN qc_review_item review ON review.rule_execution_id=execution.id
               WHERE execution.task_id=$1::uuid""",
            ids["task"],
        )
        assert tuple(result.values()) == ("evidence_insufficient", "hybrid", "high", 20)


@pytest.mark.parametrize("drift", ["contract", "epoch", "producer"])
async def test_frozen_aggregate_work_rejects_wrong_contract_or_owner(drift) -> None:
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        await _prepare(connection, pool, ids, rules)
        work = await connection.fetchrow(
            "SELECT * FROM qc_aggregate_inspection_work_item WHERE task_id=$1::uuid",
            ids["task"],
        )
        command = PublishAggregateInspectionWorkItemCommand(
            task_id=ids["task"], evidence_input_set_id=work["evidence_input_set_id"],
            producer_agent_run_id=ids["run"], rule_id=rules["aggregate"],
            rule_code=work["rule_code"], rule_contract_hash=work["rule_contract_hash"],
            scope=EvidenceInputScope.TASK, scope_key="toast_style", finalization_epoch=0,
        )
        other_run = str(uuid4())
        if drift == "producer":
            # 同 Task 的另一个真实 Run 也不能冒充当前 finalization owner。
            await connection.execute(
                """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
                   VALUES($1::uuid,$2::uuid,'rule_judge','other-producer','running')""",
                other_run, ids["task"],
            )
        command = replace(command, **{
            "contract": {"rule_contract_hash": "forged-contract"},
            "epoch": {"finalization_epoch": 1},
            "producer": {"producer_agent_run_id": other_run},
        }[drift])
        with pytest.raises(TaskRuleInventoryError):
            await PostgresAggregateInspectionWorkItemRepository(pool=pool).publish(command=command)
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_aggregate_inspection_work_item WHERE task_id=$1::uuid",
            ids["task"],
        ) == 1
