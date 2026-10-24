"""真实隔离 PostgreSQL 验证 APP 快照排空后聚合恢复先于异常封存。"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.aggregate_inspection_publication import (
    AggregateInspectionPublicationService,
)
from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationEntryInput,
)
from services.api.app.application.task_finalization import TerminateTaskFinalizationCommand
from services.api.app.domain.evidence import AppExplorationResult, AppExplorationResultOutcome
from services.api.app.domain.task import TaskExecutionStatus, TaskQualityOutcome
from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
    PostgresAggregateInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.app.infrastructure.db.app_exploration_result_repository import (
    PostgresAppExplorationResultRepository,
)
from services.api.app.infrastructure.db.app_inspection_publication_repository import (
    PostgresAppInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.tests.test_aggregate_rule_inventory_postgres import _seed_evidence
from services.api.tests.test_task_rule_inventory_postgres import _database
from services.api.tests.test_worker import queue as queue  # noqa: F401 - 真实 Redis 队列夹具


async def _source(connection, pool, ids, *, mode="ready", complete=False, not_applicable=False):
    await connection.execute(
        """UPDATE qc_task SET status='running',finalization_state='session_draining',
               finalization_agent_run_id=$2::uuid,exploration_draining_at=now(),
               session_draining_at=now() WHERE id=$1::uuid""",
        ids["task"], ids["run"],
    )
    await _seed_evidence(connection, pool, ids, page_type="login" if not_applicable else "home")
    index = await PostgresAppEvidenceIndexRepository(pool=pool).get_by_run_id(run_id=ids["run"])
    now = datetime.now(UTC)
    coverage = {} if mode == "missing_authority" else {"page_type_applicability": {
        "protocol_version": "app-page-type-applicability.v2",
        "assessment_scope_verified": True, "assessment_scope_complete": True,
        "assessment_scope_kind": "fixed_scope",
        "applicable_page_types": [] if not_applicable else ["home"],
        "not_applicable_page_types": ["home"] if not_applicable else [],
        "not_observed_page_types": [],
        "unresolved_applicability_page_types": [],
    }}
    if complete:
        coverage.update({"coverage_complete": True, "visual_quality_satisfied": True,
                         "app_runtime_policy": {"app_target_snapshot": {"target_hash": "test"}}})
        await connection.execute(
            """UPDATE qc_task SET task_config=task_config ||
                   '{"app_exploration_target":{"target_hash":"test"}}'::jsonb
               WHERE id=$1::uuid""", ids["task"],
        )
    result = AppExplorationResult(
        task_id=ids["task"], run_id=ids["run"], surface_session_id=index.surface_session_id,
        outcome=AppExplorationResultOutcome.EXPLORATION_COMPLETED,
        terminal_reason_code="exploration_completed", evidence_index_id=index.index_id,
        evidence_index_hash=index.content_sha256,
        coverage_summary=coverage,
        evidence_gaps=(), started_at=now-timedelta(seconds=5), completed_at=now,
    )
    if mode == "corrupt_result":
        await connection.execute(
            """INSERT INTO qc_app_exploration_result(
                agent_run_id,task_id,surface_session_id,evidence_index_id,protocol_version,
                outcome,terminal_reason_code,evidence_index_hash,coverage_summary,evidence_gaps,
                started_at,completed_at,result_sha256)
               VALUES($1::uuid,$2::uuid,$3,$4,$5,$6,$7,$8,$9::jsonb,'[]',$10,$11,$12)""",
            result.run_id, result.task_id, result.surface_session_id, result.evidence_index_id,
            result.protocol_version, result.outcome.value, result.terminal_reason_code,
            result.evidence_index_hash, json.dumps(result.to_dict()["coverage_summary"]),
            result.started_at, result.completed_at, "sha256:"+"c"*64,
        )
    else:
        await PostgresAppExplorationResultRepository(pool=pool).create(result=result)
    await connection.execute(
        """UPDATE qc_surface_session SET status='lost',activated_at=now(),lost_at=now(),
               failure_reason='测试会话已终结',finalization_epoch=0
           WHERE session_id=$1""", index.surface_session_id,
    )
    await connection.execute(
        """UPDATE qc_agent_run SET status='done',finished_at=now()-interval '2 hours'
           WHERE id=$1::uuid""", ids["run"],
    )
    entry, = index.entries
    publication = await PostgresAppInspectionPublicationRepository(pool=pool).create_for_index(
        task_id=ids["task"], source_agent_run_id=ids["run"],
        evidence_index_id=index.index_id, evidence_index_hash=index.content_sha256,
        expected_finalization_epoch=0,
        entries=(AppInspectionPublicationEntryInput(
            sequence_no=1, capture_id=entry.capture_id,
            evidence_manifest_id=entry.evidence_manifest_id,
            evidence_manifest_version=entry.evidence_manifest_version,
            semantic_role=entry.semantic_role.value, page_type=None, page_state="unknown",
        ),),
    )
    await connection.execute(
        """UPDATE qc_app_inspection_publication SET status='published',finished_at=now(),
               terminal_entry_count=expected_entry_count,expected_rule_count=0
           WHERE id=$1::uuid""", publication.publication_id,
    )
    return index


@pytest.mark.parametrize("mode", [
    "ready", "missing_authority", "corrupt_index", "corrupt_result", "session_epoch",
    "publication_pending", "work_pending", "cancel", "old_owner", "old_epoch",
])
async def test_only_coherent_drained_app_source_recovers_before_timeout(mode, monkeypatch):
    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        index = await _source(connection, pool, ids, mode=mode)
        if mode == "corrupt_index":
            # 正式封存后禁止改写；模拟读取层真实执行哈希复核时拒绝损坏材料。
            async def corrupted_index(_self, **_kwargs):
                raise ValueError("Evidence Index 哈希不匹配")

            monkeypatch.setattr(
                PostgresAppEvidenceIndexRepository, "get_by_run_id", corrupted_index,
            )
        elif mode == "session_epoch":
            await connection.execute(
                "UPDATE qc_surface_session SET finalization_epoch=1 WHERE session_id=$1",
                index.surface_session_id,
            )
        elif mode == "publication_pending":
            await connection.execute(
                """UPDATE qc_app_inspection_publication SET status='pending',finished_at=NULL,
                       terminal_entry_count=0 WHERE task_id=$1::uuid""", ids["task"],
            )
        elif mode == "work_pending":
            from services.api.tests.test_inspection_closure_settlement_postgres import (
                _seed_work_item,
            )

            await _seed_work_item(
                connection, {**ids, "rule": rules["eligible"]}, name="recovery-pending",
                page_seq=2, status="pending", attempt_count=0, request_status=None,
            )
        elif mode == "cancel":
            await connection.execute(
                """UPDATE qc_agent_run SET output_context='{"cancel_requested":true}'
                   WHERE id=$1::uuid""", ids["run"],
            )
        elif mode == "old_owner":
            await connection.execute(
                "UPDATE qc_task SET finalization_agent_run_id=NULL WHERE id=$1::uuid", ids["task"],
            )
        elif mode == "old_epoch":
            await connection.execute(
                "UPDATE qc_task SET finalization_epoch=1 WHERE id=$1::uuid", ids["task"],
            )
        repository = PostgresAggregateInspectionPublicationRepository(pool=pool)
        prepared = await AggregateInspectionPublicationService(
            repository=repository,
        ).prepare_app_recovery(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
        assert prepared is (mode == "ready")
        count = await connection.fetchval(
            "SELECT count(*) FROM qc_aggregate_inspection_work_item WHERE task_id=$1::uuid",
            ids["task"],
        )
        assert count == (1 if mode == "ready" else 0)
        if mode == "ready":
            assert await PostgresTaskFinalizationRepository(pool=pool).terminate_stalled(
                command=TerminateTaskFinalizationCommand(
                    task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0,
                    expected_row_version=0, execution_status=TaskExecutionStatus.NEEDS_REVIEW,
                    reason_code="FINALIZATION_STALLED_TIMEOUT",
                ), minimum_terminal_age_seconds=600,
            ) is None
            assert await repository.is_app_recovery_ready(
                task_id=ids["task"], source_agent_run_id=ids["run"], finalization_epoch=0,
            ) is False


async def test_completion_recovery_before_publication_scanner_does_not_seal_old_app(queue):
    """主 Run 已超时且扫描器未跑时，Completion 自身也必须先补正式交接。"""
    from services.api.app import worker
    from services.api.app.application.app_sealed_inspection_publisher import (
        AppSealedEvidenceInspectionPublisher,
    )
    from services.api.app.infrastructure.db.app_visual_observation_repository import (
        PostgresAppVisualObservationRepository,
    )
    from services.api.app.infrastructure.db.evidence_manifest_repository import (
        PostgresEvidenceManifestRepository,
    )
    from services.api.app.infrastructure.db.surface_session_repository import (
        PostgresSurfaceSessionRepository,
    )
    from services.api.app.infrastructure.db.task_finalization_repository import (
        build_default_task_finalization_service,
    )
    from services.api.app.infrastructure.queue.redis_queue import (
        AgentRunQueueStatus,
        AgentRunRecord,
    )
    from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore

    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, *_rest,
    ):
        await _source(connection, pool, ids)
        await connection.execute(
            "DELETE FROM qc_app_inspection_publication WHERE task_id=$1::uuid", ids["task"],
        )
        now = datetime.now(UTC)
        await queue.save(AgentRunRecord(
            run_id=ids["run"], task_id=ids["task"], message="超龄 APP 缺正式交接",
            task_config={"workflow_version": "v2", "execution_mode": "full_inspection",
                         "_task_target": {"platform": "app"}},
            status=AgentRunQueueStatus.COMPLETED,
            submitted_at=now-timedelta(hours=3), finished_at=now-timedelta(hours=2),
        ))
        publisher = AppSealedEvidenceInspectionPublisher(
            evidence_indexes=PostgresAppEvidenceIndexRepository(pool=pool),
            manifests=PostgresEvidenceManifestRepository(pool=pool),
            sessions=PostgresSurfaceSessionRepository(pool=pool),
            visual_observations=PostgresAppVisualObservationRepository(pool=pool),
            publication_writer=PostgresAppInspectionPublicationRepository(pool=pool),
        )
        service = build_default_task_finalization_service(pool=pool)

        async def deferred(_command):
            return True

        service.defer_unchanged_completion_gate = deferred
        await worker._retry_pending_task_finalizations(
            queue, InMemoryAgentRunStateStore(), service, sealed_inspection_publisher=publisher,
        )
        assert await connection.fetchval(
            "SELECT finalization_state FROM qc_task WHERE id=$1::uuid", ids["task"],
        ) == "session_draining"
        assert await connection.fetchval(
            "SELECT status FROM qc_app_inspection_publication WHERE task_id=$1::uuid", ids["task"],
        ) == "pending"


async def test_completed_recovered_aggregate_uses_real_gate_before_old_main_timeout(
    queue, monkeypatch,
):
    """超龄 Main 的聚合完成后，真实门禁通过必须正常绑定报告，不能再技术封存。"""
    from services.api.app import worker
    from services.api.app.application.aggregate_inspection_execution import AggregateJudgement
    from services.api.app.application.task_finalization import EvaluateCompletionGateCommand
    from services.api.app.infrastructure.db.aggregate_inspection_execution_repository import (
        PostgresAggregateInspectionExecutionRepository,
    )
    from services.api.app.infrastructure.db.task_finalization_repository import (
        _read_completion_gate_facts,
        build_default_task_finalization_service,
    )
    from services.api.app.infrastructure.queue.redis_queue import (
        AgentRunQueueStatus,
        AgentRunRecord,
    )
    from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore

    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, rules, *_rest,
    ):
        index = await _source(connection, pool, ids, complete=True)
        capture_id = index.entries[0].capture_id
        # 本夹具已有当前 Capture 的两个页面级真实结论；本例只恢复尚未交接的聚合。
        for rule_name in ("eligible", "auth"):
            await connection.execute(
                """INSERT INTO qc_rule_execution(task_id,rule_id,page_capture_id,agent_run_id,
                       execution_status,judge_strategy,judge_input)
                   VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'pass','hybrid',
                          '{"finalization_epoch":0}')""",
                ids["task"], rules[rule_name], capture_id, ids["run"],
            )
        await connection.execute(
            """UPDATE qc_app_inspection_publication_entry
               SET status='not_applicable',finished_at=now()
               WHERE publication_id IN (
                 SELECT id FROM qc_app_inspection_publication WHERE task_id=$1::uuid)""",
            ids["task"],
        )
        await connection.execute(
            """INSERT INTO qc_v2_work_item(work_id,work_type,task_id,session_id,branch_id,
                   source_capture_id,protocol_version,payload_hash,work_payload,status,
                   deadline,finished_at)
               VALUES($1,'exploration',$2::uuid,$3,'gate-test',$4::uuid,
                   'exploration-work.v1','test','{}','succeeded',now(),now())""",
            f"gate-{ids['task']}", ids["task"], index.surface_session_id, capture_id,
        )
        repository = PostgresAggregateInspectionPublicationRepository(pool=pool)
        assert await AggregateInspectionPublicationService(
            repository=repository,
        ).prepare_app_recovery(task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0)
        work = await connection.fetchrow(
            "SELECT * FROM qc_aggregate_inspection_work_item WHERE task_id=$1::uuid", ids["task"],
        )
        executor = PostgresAggregateInspectionExecutionRepository(pool=pool)
        claim = await executor.claim_for_wakeup(
            work_item_id=str(work["id"]), task_id=ids["task"], message_version=work["row_version"],
            lease_owner="gate-complete-test", lease_seconds=60,
        )
        context = await executor.load_judge_context(lease=claim.lease)
        await executor.complete_judgement(context=context, judgement=AggregateJudgement(
            execution_status="pass", result_summary="真实聚合结论", problem_desc=None,
            confidence=None, review_status=None, review_reason=None, error_code=None,
            wrapper_payload={"conclusion": "pass"},
            evidence_artifact_ids=context.input_set.members[0].artifact_ids,
        ))
        await connection.execute(
            """UPDATE qc_outbox_event SET status='published',published_at=now()
               WHERE task_id=$1::uuid""", ids["task"],
        )
        command = EvaluateCompletionGateCommand(
            task_id=ids["task"], agent_run_id=ids["run"], expected_finalization_epoch=0,
            expected_row_version=0, execution_status=TaskExecutionStatus.COMPLETED,
            quality_outcome=TaskQualityOutcome.PASS,
        )
        _, facts = await _read_completion_gate_facts(connection, command)
        assert facts.gate_passed, facts.block_reasons
        service = build_default_task_finalization_service(pool=pool)
        assert await service.app_completion_ready_before_timeout(command)

        async def report(record, *_args, **_kwargs):
            record.final_report = {"report_version": "v1", "report_stage": "final",
                                   "is_final": True, "task": {"status": "done"},
                                   "summary": {"overall_status": "pass", "pass": 3}}

        monkeypatch.setattr(worker, "_generate_final_report", report)
        now = datetime.now(UTC)
        await queue.save(AgentRunRecord(
            run_id=ids["run"], task_id=ids["task"], message="聚合已完成的超龄 APP",
            task_config={"workflow_version": "v2", "execution_mode": "full_inspection",
                         "_task_target": {"platform": "app"}},
            status=AgentRunQueueStatus.COMPLETED,
            submitted_at=now-timedelta(hours=3), finished_at=now-timedelta(hours=2),
        ))
        assert await worker._retry_pending_task_finalizations(
            queue, InMemoryAgentRunStateStore(), service,
        ) == 1
        task = await connection.fetchrow(
            "SELECT finalization_state,execution_status FROM qc_task WHERE id=$1::uuid",
            ids["task"],
        )
        assert dict(task) == {"finalization_state": "sealed", "execution_status": "completed"}
        assert await connection.fetchval(
            "SELECT count(*) FROM qc_report_gate_binding WHERE task_id=$1::uuid", ids["task"],
        ) == 1


@pytest.mark.parametrize("race", ["cancel", "deleted"])
@pytest.mark.parametrize("route", ["work", "disposition"])
async def test_app_recovery_write_rechecks_cancel_and_delete_after_ready(race, route, monkeypatch):
    from services.api.app.application import aggregate_inspection_publication as publication
    from services.api.app.infrastructure.db.aggregate_inspection_publication_repository import (
        AggregateInspectionPublicationPersistenceError,
    )

    async with _database(include_aggregate=True, transactional=True) as (
        connection, pool, ids, *_rest,
    ):
        await _source(connection, pool, ids)
        repository = PostgresAggregateInspectionPublicationRepository(pool=pool)
        method = "persist_published" if route == "work" else "persist_disposition"
        original = getattr(repository, method)

        async def write_after_race(**kwargs):
            if race == "cancel":
                await connection.execute(
                    """UPDATE qc_agent_run SET output_context='{"cancel_requested":true}'
                       WHERE id=$1::uuid""", ids["run"],
                )
            else:
                await connection.execute(
                    "UPDATE qc_task SET deleted_at=now() WHERE id=$1::uuid", ids["task"],
                )
            return await original(**kwargs)

        if route == "disposition":
            monkeypatch.setattr(
                publication, "_select_rule_evidence",
                lambda *_args, **_kwargs: ((), (
                    "evidence_insufficient", "AGGREGATE_APPLICABLE_EVIDENCE_MISSING", "测试缺证",
                )),
            )
        monkeypatch.setattr(repository, method, write_after_race)
        with pytest.raises(AggregateInspectionPublicationPersistenceError):
            await AggregateInspectionPublicationService(repository=repository).prepare_app_recovery(
                task_id=ids["task"], agent_run_id=ids["run"], finalization_epoch=0,
            )
        for table in ("qc_aggregate_inspection_work_item", "qc_aggregate_inspection_candidate",
                      "qc_evidence_input_set", "qc_rule_execution"):
            assert await connection.fetchval(
                f"SELECT count(*) FROM {table} WHERE task_id=$1::uuid", ids["task"],
            ) == 0
