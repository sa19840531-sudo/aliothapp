"""Web 正式终态前持久账本收束的真实 PostgreSQL 回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from importlib import import_module
from uuid import uuid4

import fakeredis
import pytest

from services.api.app import worker as worker_module
from services.api.app.application.task_finalization import TaskFinalizationService
from services.api.app.application.web_formal_session_closure import (
    DurableWebFormalSessionCloser,
)
from services.api.app.application.web_inspection_reconciliation import (
    ReconcileWebInspectionCommand,
    WebInspectionReconciliation,
)
from services.api.app.application.web_page_classification import WebPageAssessment
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
    PostgresWebCoverageSnapshotRepository,
)
from services.api.app.infrastructure.db.web_report_generation_attempt_repository import (
    PostgresWebReportGenerationAttemptRepository,
)
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_web_lifecycle_recovery_pg import (
    _ForbiddenFormalWriter,
    _NoopDrainer,
    _PassPendingManifestFinalizer,
    _runtime_with_postgres_session,
    _seed_review_capture,
    _start_formal_task,
)
from services.api.tests.test_web_v2_formal_report_e2e import (
    _PostgresCase,
)
from services.api.tests.test_web_v2_formal_report_e2e import (
    postgres_case as postgres_case,
)


async def _seed_nonterminal_formal_ledgers(
    case: _PostgresCase,
    *,
    task_id: str,
    run_id: str,
    session_id: str,
    include_evidence_requests: bool = True,
) -> dict[str, object]:
    capture = await _seed_review_capture(
        case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    parent_capture_id = await case.connection.fetchval(
        "SELECT parent_page_capture_id::text FROM qc_page_capture WHERE id = $1::uuid",
        capture.capture_id,
    )
    assert isinstance(parent_capture_id, str)
    manifest_finalizer = _PassPendingManifestFinalizer(case)
    await manifest_finalizer.finalize_capture(
        task_id=task_id,
        capture_id=parent_capture_id,
    )
    await manifest_finalizer.finalize_capture(
        task_id=task_id,
        capture_id=capture.capture_id,
    )
    recorder = WebPageTypeAssessmentRecorder(
        repository=PostgresWebCaptureEvidenceRepository(pool=case.pool)
    )
    assessment_result = await recorder.persist(
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
        capture=capture,
        assessment=WebPageAssessment(
            page_type="content_list",
            raw_page_type="content_list",
            confidence=0.99,
            evidence=("真实 PG 终态账本夹具",),
        ),
    )
    assert assessment_result.assessment_record_id is not None
    assessment_hash = await case.connection.fetchval(
        """
        SELECT payload_hash FROM qc_h5_page_type_assessment
        WHERE assessment_record_id = $1
        """,
        assessment_result.assessment_record_id,
    )
    publication_id = str(uuid4())
    lease_token = str(uuid4())
    await case.connection.execute(
        """
        INSERT INTO qc_h5_inspection_publication (
            id, task_id, source_agent_run_id, page_capture_id,
            page_type_assessment_id, assessment_hash,
            ruleset_id, ruleset_version, ruleset_snapshot_hash,
            finalization_epoch, input_hash, mode, status,
            lease_owner, lease_token, lease_expires_at,
            attempt_count, max_attempts
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4::uuid,
            $5, $6, $7::uuid, 1, 'web-v2-e2e-snapshot',
            0, $8, 'enforced', 'leased',
            'web-terminal-ledger-test', $9::uuid, $10::timestamptz,
            1, 3
        )
        """,
        publication_id,
        task_id,
        run_id,
        capture.capture_id,
        assessment_result.assessment_record_id,
        assessment_hash,
        case.ruleset_id,
        f"web-terminal-publication:{task_id}",
        lease_token,
        datetime.now(UTC) + timedelta(minutes=5),
    )

    page_work_ids: list[str] = []
    page_attempt_id: str | None = None
    for ordinal, (capture_id, status) in enumerate(
        ((parent_capture_id, "pending"), (capture.capture_id, "leased")),
        start=1,
    ):
        work_lease_token = str(uuid4())
        work_id = await case.connection.fetchval(
            """
            INSERT INTO qc_inspection_work_item (
                task_id, page_capture_id, producer_agent_run_id,
                consumer_agent_run_id, session_owner_agent_run_id,
                ruleset_id, ruleset_version, ruleset_snapshot_hash,
                protocol_version, batch_key, strategy, session_dependency,
                input_hash, status, attempt_count, max_attempts,
                lease_owner, lease_token, lease_expires_at,
                control_mode, finalization_epoch
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid,
                CASE WHEN $4 = 'leased' THEN $3::uuid ELSE NULL END, NULL,
                $5::uuid, 1, 'web-v2-e2e-snapshot',
                'inspection-work-item.v1', $6, 'hybrid', 'snapshot',
                $7, $4, CASE WHEN $4 = 'leased' THEN 1 ELSE 0 END, 3,
                CASE WHEN $4 = 'leased' THEN 'web-terminal-ledger-test' ELSE NULL END,
                CASE WHEN $4 = 'leased' THEN $8::uuid ELSE NULL END,
                CASE WHEN $4 = 'leased' THEN $9::timestamptz ELSE NULL END,
                'enforced', 0
            )
            RETURNING id::text
            """,
            task_id,
            capture_id,
            run_id,
            status,
            case.ruleset_id,
            f"web-terminal-{status}-{ordinal}",
            f"web-terminal-input-{status}-{ordinal}:{task_id}",
            work_lease_token,
            datetime.now(UTC) + timedelta(minutes=5),
        )
        assert isinstance(work_id, str)
        page_work_ids.append(work_id)
        await case.connection.execute(
            """
            INSERT INTO qc_inspection_work_item_rule (
                work_item_id, rule_id, rule_code, ordinal, contract_hash, status
            ) VALUES ($1::uuid, $2::uuid, 'web_e2e_observed', 0, $3, 'pending')
            """,
            work_id,
            case.observed_rule_id,
            f"web-terminal-contract:{ordinal}:{task_id}",
        )
        if status == "leased":
            page_attempt_id = await case.connection.fetchval(
                """
                INSERT INTO qc_inspection_attempt (
                    work_item_id, consumer_agent_run_id, attempt_no,
                    lease_token, status, input_hash
                ) VALUES ($1::uuid, $2::uuid, 1, $3::uuid, 'running', $4)
                RETURNING id::text
                """,
                work_id,
                run_id,
                work_lease_token,
                f"web-terminal-input-{status}-{ordinal}:{task_id}",
            )

    input_hash = "a" * 64
    input_set_id = f"evidence-input-set:{input_hash}"
    await case.connection.execute(
        """
        INSERT INTO qc_evidence_input_set (
            input_set_id, input_set_hash, task_id, source_agent_run_id,
            rule_id, rule_code, rule_contract_hash, ruleset_snapshot_hash,
            scope, scope_key, finalization_epoch,
            eligibility_snapshot_id, eligibility_snapshot_hash,
            selection_policy_hash, eligible_capture_ids,
            protocol_version, input_set_payload, sealed_at
        ) VALUES (
            $1, $2, $3::uuid, $4::uuid,
            $5::uuid, 'web_e2e_observed', $6, 'web-v2-e2e-snapshot',
            'task', 'web_terminal_ledger', 0,
            $7, $8, $9, jsonb_build_array($10::text),
            'evidence-input-set.v1', '{}'::jsonb, now()
        )
        """,
        input_set_id,
        input_hash,
        task_id,
        run_id,
        case.observed_rule_id,
        f"web-terminal-aggregate-contract:{task_id}",
        f"web-terminal-eligibility:{task_id}",
        f"web-terminal-eligibility-hash:{task_id}",
        f"web-terminal-selection:{task_id}",
        capture.capture_id,
    )
    aggregate_work_id = str(uuid4())
    aggregate_lease_token = str(uuid4())
    await case.connection.execute(
        """
        INSERT INTO qc_aggregate_inspection_work_item (
            id, task_id, evidence_input_set_id, producer_agent_run_id,
            consumer_agent_run_id, rule_id, rule_code, rule_contract_hash,
            scope, scope_key, protocol_version, input_hash,
            control_mode, status, attempt_count, max_attempts,
            lease_owner, lease_token, lease_expires_at,
            finalization_epoch
        ) VALUES (
            $1::uuid, $2::uuid, $3, $4::uuid,
            $4::uuid, $5::uuid, 'web_e2e_observed', $6,
            'task', 'web_terminal_ledger',
            'aggregate-inspection-work-item.v1', $7,
            'enforced', 'leased', 1, 3,
            'web-terminal-ledger-test', $8::uuid, $9::timestamptz, 0
        )
        """,
        aggregate_work_id,
        task_id,
        input_set_id,
        run_id,
        case.observed_rule_id,
        f"web-terminal-aggregate-contract:{task_id}",
        input_hash,
        aggregate_lease_token,
        datetime.now(UTC) + timedelta(minutes=5),
    )
    aggregate_attempt_id = await case.connection.fetchval(
        """
        INSERT INTO qc_aggregate_inspection_attempt (
            task_id, work_item_id, consumer_agent_run_id,
            attempt_no, lease_token, input_hash, status
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid,
            1, $4::uuid, $5, 'running'
        )
        RETURNING id::text
        """,
        task_id,
        aggregate_work_id,
        run_id,
        aggregate_lease_token,
        input_hash,
    )
    aggregate_candidate_ids: list[str] = []
    for suffix, status, evidence_set, work_item in (
        ("expected", "expected", None, None),
        ("published", "published", input_set_id, aggregate_work_id),
    ):
        candidate_id = await case.connection.fetchval(
            """
            INSERT INTO qc_aggregate_inspection_candidate (
                task_id, source_agent_run_id, rule_id, rule_code,
                rule_contract_hash, scope, scope_key, finalization_epoch,
                status, expected_member_count, evidence_input_set_id,
                work_item_id
            ) VALUES (
                $1::uuid, $2::uuid, $3::uuid, 'web_e2e_observed',
                $4, 'task', $5, 0,
                $6, 1, $7, $8::uuid
            )
            RETURNING id::text
            """,
            task_id,
            run_id,
            case.observed_rule_id,
            f"web-terminal-aggregate-contract:{task_id}",
            f"web_terminal_{suffix}",
            status,
            evidence_set,
            work_item,
        )
        assert isinstance(candidate_id, str)
        aggregate_candidate_ids.append(candidate_id)
    assert page_attempt_id is not None
    evidence_request_ids = (
        await _seed_nonterminal_evidence_requests(
            case,
            task_id=task_id,
            session_id=session_id,
            work_item_id=page_work_ids[-1],
            attempt_id=page_attempt_id,
            capture_id=capture.capture_id,
        )
        if include_evidence_requests
        else []
    )
    report_attempt_id = str(uuid4())
    await case.connection.execute(
        """
        INSERT INTO qc_web_report_generation_attempt (
            id, task_id, agent_run_id, finalization_epoch,
            attempt_number, status
        ) VALUES ($1::uuid, $2::uuid, $3::uuid, 0, 1, 'running')
        """,
        report_attempt_id,
        task_id,
        run_id,
    )
    return {
        "publication_id": publication_id,
        "page_work_ids": page_work_ids,
        "page_attempt_id": page_attempt_id,
        "aggregate_work_id": aggregate_work_id,
        "aggregate_attempt_id": aggregate_attempt_id,
        "aggregate_candidate_ids": aggregate_candidate_ids,
        "evidence_request_ids": evidence_request_ids,
        "request_work_item_id": page_work_ids[-1],
        "request_attempt_id": page_attempt_id,
        "request_capture_id": capture.capture_id,
        "report_attempt_id": report_attempt_id,
    }


async def _seed_nonterminal_evidence_requests(
    case: _PostgresCase,
    *,
    task_id: str,
    session_id: str,
    work_item_id: str,
    attempt_id: str,
    capture_id: str,
) -> list[str]:
    request_ids: list[str] = []
    for ordinal, status in enumerate(("pending", "routed", "running"), start=1):
        request_id = f"web-terminal-request:{task_id}:{status}"
        await case.connection.execute(
            """
            INSERT INTO qc_evidence_request (
                request_id, requirement_hash, payload_hash, protocol_version,
                work_item_id, attempt_id, task_id, rule_id, rule_code,
                target_capture_id, session_dependency, replay_policy,
                required_equivalence, side_effect_policy, minimum_source_grade,
                request_payload, status, expires_at, finalization_epoch
            ) VALUES (
                $1, $2, $3, 'evidence-request.v1',
                $4::uuid, $5::uuid, $6::uuid, $7::uuid, 'web_e2e_observed',
                $8::uuid, 'active_session', 'same_surface_only',
                'same_surface_continuation', 'read_only', 'A0',
                '{}'::jsonb, $9, $10::timestamptz, 0
            )
            """,
            request_id,
            f"web-terminal-requirement:{ordinal}:{task_id}",
            f"web-terminal-request-payload:{ordinal}:{task_id}",
            work_item_id,
            attempt_id,
            task_id,
            case.observed_rule_id,
            capture_id,
            status,
            datetime.now(UTC) + timedelta(minutes=5),
        )
        request_ids.append(request_id)
    action_execution_id = f"web-terminal-restoration-action:{task_id}"
    await case.connection.execute(
        """
        INSERT INTO qc_action_execution (
            action_execution_id, task_id, session_id, intent_id,
            idempotency_key, intent_hash, intent_protocol_version,
            intent_payload, command_sequence, finalization_epoch,
            requested_source_capture_id, status, result_payload
        ) VALUES (
            $1, $2::uuid, $3, $4, $4, $5, 'web-v2-workflow/v1',
            '{}'::jsonb, 2, 0, $6::uuid, 'planned', '{}'::jsonb
        )
        """,
        action_execution_id,
        task_id,
        session_id,
        f"web-terminal-restoration-intent:{task_id}",
        "a" * 64,
        capture_id,
    )
    running_restoration_id = f"web-terminal-restoration:{task_id}"
    await case.connection.execute(
        """
        INSERT INTO qc_restoration_attempt (
            attempt_id, plan_id, plan_hash, plan_protocol_version, plan_payload,
            request_id, task_id, original_capture_id,
            source_session_id, target_session_id, surface_kind, strategy,
            attempt_no, status, source_grade, attempt_protocol_version,
            started_at, finalization_epoch, action_execution_ids
        ) VALUES (
            $1, $2, $3, 'restoration-plan.v1', '{}'::jsonb,
            $4, $5::uuid, $6::uuid,
            $7, $7, 'web', 'original_surface_pin',
            1, 'running', 'A1', 'restoration-attempt.v1', now(), 0,
            jsonb_build_array($8::text)
        )
        """,
        running_restoration_id,
        f"web-terminal-restoration-plan:{task_id}",
        f"web-terminal-restoration-hash:{task_id}",
        request_ids[0],
        task_id,
        capture_id,
        session_id,
        action_execution_id,
    )
    equivalent_restoration_id = f"web-terminal-equivalent-restoration:{task_id}"
    equivalence_assessment_id = f"web-terminal-equivalence:{task_id}"
    await case.connection.execute(
        """
        INSERT INTO qc_restoration_attempt (
            attempt_id, plan_id, plan_hash, plan_protocol_version, plan_payload,
            request_id, task_id, original_capture_id,
            source_session_id, target_session_id, surface_kind, strategy,
            attempt_no, status, source_grade, restored_capture_id,
            equivalence_assessment_id, assessment_hash, assessment_payload,
            attempt_protocol_version, started_at, finished_at, finalization_epoch
        ) VALUES (
            $1, $2, $3, 'restoration-plan.v1', '{}'::jsonb,
            $4, $5::uuid, $6::uuid,
            $7, $7, 'web', 'original_surface_pin',
            1, 'equivalent', 'A1', $6::uuid,
            $8, $9, '{"passed":true}'::jsonb,
            'restoration-attempt.v1', now(), now(), 0
        )
        """,
        equivalent_restoration_id,
        f"web-terminal-equivalent-plan:{task_id}",
        f"web-terminal-equivalent-hash:{task_id}",
        request_ids[1],
        task_id,
        capture_id,
        session_id,
        equivalence_assessment_id,
        f"web-terminal-equivalence-hash:{task_id}",
    )
    probe_work_id = f"web-terminal-probe:{task_id}"
    await case.connection.execute(
        """
        INSERT INTO qc_v2_work_item (
            work_id, work_type, task_id, session_id, branch_id,
            source_capture_id, evidence_request_id, protocol_version,
            payload_hash, work_payload, status, deadline,
            restoration_attempt_id, equivalence_assessment_id, source_grade
        ) VALUES (
            $1, 'probe', $2::uuid, $3, 'web-terminal-probe',
            $4::uuid, $5, 'v1', $6, '{}'::jsonb,
            'pending', now() + interval '5 minutes', $7, $8, 'A1'
        )
        """,
        probe_work_id,
        task_id,
        session_id,
        capture_id,
        request_ids[1],
        f"web-terminal-probe-payload:{task_id}",
        equivalent_restoration_id,
        equivalence_assessment_id,
    )
    return request_ids


def _new_repository(case: _PostgresCase):
    module = import_module(
        "services.api.app.infrastructure.db.web_formal_terminal_ledger_repository"
    )
    repository_class = module.PostgresWebFormalTerminalLedgerRepository
    return repository_class(pool=case.pool)


def _json_object(value: object) -> dict[str, object]:
    if isinstance(value, str):
        value = json.loads(value)
    assert isinstance(value, dict)
    return value


async def _prepare_reportable_task(
    case: _PostgresCase,
    *,
    task_id: str,
    run_id: str,
) -> None:
    await case.connection.execute(
        """
        UPDATE qc_task
        SET finalization_state = 'exploration_draining',
            exploration_draining_at = now(),
            finalization_row_version = finalization_row_version + 1
        WHERE id = $1::uuid
        """,
        task_id,
    )
    await case.connection.execute(
        """
        UPDATE qc_task
        SET finalization_state = 'session_draining',
            session_draining_at = now(),
            finalization_row_version = finalization_row_version + 1
        WHERE id = $1::uuid
          AND finalization_state = 'exploration_draining'
        """,
        task_id,
    )
    await case.connection.execute(
        """
        INSERT INTO qc_web_coverage_snapshot (
            task_id, agent_run_id, finalization_epoch,
            required_page_types, observed_page_types,
            missing_page_types, unrecognized_page_types,
            normalization_version, coverage_status, snapshot_hash
        ) VALUES (
            $1::uuid, $2::uuid, 0,
            '["content_list"]'::jsonb, '["content_list"]'::jsonb,
            '[]'::jsonb, '[]'::jsonb,
            'web-page-type-assessment.v2', 'complete', $3
        )
        """,
        task_id,
        run_id,
        uuid4().hex.rjust(64, "0"),
    )


@pytest.mark.parametrize("write_method", ["record_finished", "record_run_finished"])
async def test_stale_state_store_finish_preserves_formal_terminal_fence(
    postgres_case: _PostgresCase,
    write_method: str,
) -> None:
    """终态收束后的陈旧 Run 快照不得经任一结束写回擦除正式终态栅栏。"""

    task_id, run_id, _session_id = await _start_formal_task(
        postgres_case,
        run_status="running",
        with_session=False,
    )
    state_store = PostgresAgentRunStateStore(pool=postgres_case.pool)
    stale_record = await state_store.get_record_snapshot(run_id=run_id)
    assert stale_record is not None
    stale_record.status = AgentRunQueueStatus.FAILED
    stale_record.finished_at = datetime.now(UTC)
    stale_record.termination_reason = "worker_interrupted"
    stale_record.final_text = f"stale-{write_method}"
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET output_context = jsonb_set(
            COALESCE(output_context, '{}'::jsonb),
            '{obsolete_key}',
            '"must_be_replaced"'::jsonb,
            TRUE
        )
        WHERE id = $1::uuid
        """,
        run_id,
    )

    await _new_repository(postgres_case).terminalize(
        task_id=task_id,
        agent_run_id=run_id,
        outcome="failed",
        reason_code="WORKER_INTERRUPTED",
        reason_message="Worker 已中断",
    )

    await getattr(state_store, write_method)(stale_record)

    output_context = _json_object(
        await postgres_case.connection.fetchval(
            "SELECT output_context FROM qc_agent_run WHERE id = $1::uuid",
            run_id,
        )
    )
    assert output_context["web_formal_terminalized"] == {
        "outcome": "failed",
        "reason_code": "WORKER_INTERRUPTED",
    }
    assert output_context["final_text"] == f"stale-{write_method}"
    assert "obsolete_key" not in output_context


async def test_report_attempt_cannot_restart_after_formal_terminalization(
    postgres_case: _PostgresCase,
) -> None:
    task_id, run_id, _session_id = await _start_formal_task(
        postgres_case,
        run_status="done",
        with_session=False,
    )
    await _prepare_reportable_task(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET output_context = jsonb_set(
            COALESCE(output_context, '{}'::jsonb),
            '{existing_audit}',
            '"kept"'::jsonb,
            TRUE
        )
        WHERE id = $1::uuid
        """,
        run_id,
    )
    attempt_repository = PostgresWebReportGenerationAttemptRepository(
        pool=postgres_case.pool
    )
    running = await attempt_repository.start_attempt(
        attempt_id=str(uuid4()),
        task_id=task_id,
        agent_run_id=run_id,
    )
    stale_record = await PostgresAgentRunStateStore(
        pool=postgres_case.pool
    ).get_record_snapshot(run_id=run_id)
    assert stale_record is not None

    terminalized = await _new_repository(postgres_case).terminalize(
        task_id=task_id,
        agent_run_id=run_id,
        outcome="canceled",
        reason_code="WORKFLOW_CANCELED",
        reason_message="Web 正式任务已取消",
    )
    replay = await _new_repository(postgres_case).terminalize(
        task_id=task_id,
        agent_run_id=run_id,
        outcome="failed",
        reason_code="LATE_FAILURE",
        reason_message="迟到的失败不得覆盖首次终态",
    )

    assert terminalized.report_attempt_count == 1
    assert replay.report_attempt_count == 0
    output_context = _json_object(
        await postgres_case.connection.fetchval(
            "SELECT output_context FROM qc_agent_run WHERE id = $1::uuid",
            run_id,
        )
    )
    assert output_context["existing_audit"] == "kept"
    assert output_context["web_formal_terminalized"] == {
        "outcome": "canceled",
        "reason_code": "WORKFLOW_CANCELED",
    }
    with pytest.raises(RuntimeError, match="当前不可启动"):
        await attempt_repository.start_attempt(
            attempt_id=str(uuid4()),
            task_id=task_id,
            agent_run_id=run_id,
        )
    await PostgresAgentRunStateStore(pool=postgres_case.pool).record_run_finished(
        stale_record
    )
    output_context = _json_object(
        await postgres_case.connection.fetchval(
            "SELECT output_context FROM qc_agent_run WHERE id = $1::uuid",
            run_id,
        )
    )
    assert output_context["web_formal_terminalized"] == {
        "outcome": "canceled",
        "reason_code": "WORKFLOW_CANCELED",
    }
    assert "existing_audit" not in output_context
    with pytest.raises(RuntimeError, match="当前不可启动"):
        await attempt_repository.start_attempt(
            attempt_id=str(uuid4()),
            task_id=task_id,
            agent_run_id=run_id,
        )
    with pytest.raises(RuntimeError, match="终态冲突"):
        await attempt_repository.finish_attempt(
            attempt_id=running.attempt_id,
            attempt_number=running.attempt_number,
            status="succeeded",
            failure_code=None,
        )
    row = await postgres_case.connection.fetchrow(
        """
        SELECT status, failure_code, finished_at
        FROM qc_web_report_generation_attempt
        WHERE id = $1::uuid
        """,
        running.attempt_id,
    )
    assert dict(row) == {
        "status": "cancelled",
        "failure_code": "WORKFLOW_CANCELED",
        "finished_at": row["finished_at"],
    }
    assert row["finished_at"] is not None


async def test_report_attempt_allows_retryable_gate_error_on_failed_main_run(
    postgres_case: _PostgresCase,
) -> None:
    """失败 main Run 的孤儿恢复可在旧 Gate/错误仍保留时重试正式报告。"""

    task_id, run_id, _session_id = await _start_formal_task(
        postgres_case,
        run_status="failed",
        with_session=False,
    )
    await _prepare_reportable_task(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
    )
    gate_snapshot_id = f"retryable-gate-{uuid4()}"
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_completion_gate_snapshot (
            gate_snapshot_id, task_id, finalization_epoch,
            evaluation_sequence, protocol_version, facts_hash,
            exploration_terminal, no_pending_action,
            no_pending_capture_commit, no_nonterminal_enforced_work_item,
            no_active_session_request, candidate_rule_coverage_terminal,
            no_background_execution, review_policy_satisfied,
            pending_action_count, pending_capture_commit_count,
            nonterminal_enforced_work_item_count, active_session_request_count,
            candidate_rule_count, terminal_candidate_rule_count,
            background_execution_count, open_review_count,
            review_generation, block_reasons, gate_passed, evaluated_at
        ) VALUES (
            $1, $2::uuid, 0, 1, 'task-completion-gate.v1', $3,
            true, false, true, true, true, true, true, true,
            1, 0, 0, 0, 0, 0, 0, 0, 0,
            '["pending_action"]'::jsonb, false, now()
        )
        """,
        gate_snapshot_id,
        task_id,
        uuid4().hex.rjust(64, "0"),
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_task
        SET completion_gate_snapshot_id = $2,
            finalization_last_error = 'provider_timeout'
        WHERE id = $1::uuid
        """,
        task_id,
        gate_snapshot_id,
    )

    attempt = await PostgresWebReportGenerationAttemptRepository(
        pool=postgres_case.pool
    ).start_attempt(
        attempt_id=str(uuid4()),
        task_id=task_id,
        agent_run_id=run_id,
    )

    assert attempt.task_id == task_id
    assert attempt.agent_run_id == run_id
    assert attempt.finalization_epoch == 0
    assert attempt.attempt_number == 1


async def test_web_cancel_terminalizes_all_formal_ledgers_and_replays_idempotently(
    postgres_case: _PostgresCase,
) -> None:
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        run_status="running",
        with_session=True,
    )
    ids = await _seed_nonterminal_formal_ledgers(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    repository = _new_repository(postgres_case)

    first = await repository.terminalize(
        task_id=task_id,
        agent_run_id=run_id,
        outcome="canceled",
        reason_code="WORKFLOW_CANCELED",
        reason_message="Web 正式任务已取消",
    )
    replay = await repository.terminalize(
        task_id=task_id,
        agent_run_id=run_id,
        outcome="canceled",
        reason_code="WORKFLOW_CANCELED",
        reason_message="Web 正式任务已取消",
    )

    assert first.terminalized_count == 17
    assert replay.terminalized_count == 0
    publication = await postgres_case.connection.fetchrow(
        """
        SELECT status, failure_code, lease_owner, lease_token, finished_at
        FROM qc_h5_inspection_publication WHERE id = $1::uuid
        """,
        ids["publication_id"],
    )
    assert publication["status"] == "cancelled"
    assert publication["failure_code"] == "WORKFLOW_CANCELED"
    assert publication["lease_owner"] is None
    assert publication["lease_token"] is None
    assert publication["finished_at"] is not None
    page_work = await postgres_case.connection.fetch(
        """
        SELECT status, last_error_code, lease_owner, lease_token, finished_at
        FROM qc_inspection_work_item
        WHERE id = ANY($1::uuid[]) ORDER BY id
        """,
        ids["page_work_ids"],
    )
    assert len(page_work) == 2
    assert {row["status"] for row in page_work} == {"cancelled"}
    assert {row["last_error_code"] for row in page_work} == {"WORKFLOW_CANCELED"}
    assert all(row["lease_owner"] is None for row in page_work)
    assert all(row["lease_token"] is None for row in page_work)
    assert all(row["finished_at"] is not None for row in page_work)
    assert await postgres_case.connection.fetchval(
        "SELECT status FROM qc_inspection_attempt WHERE id = $1::uuid",
        ids["page_attempt_id"],
    ) == "abandoned"
    assert await postgres_case.connection.fetchval(
        "SELECT count(*) FROM qc_inspection_work_item_rule WHERE status = 'error' "
        "AND work_item_id = ANY($1::uuid[])",
        ids["page_work_ids"],
    ) == 2
    aggregate = await postgres_case.connection.fetchrow(
        """
        SELECT status, last_error_code, lease_owner, lease_token, finished_at
        FROM qc_aggregate_inspection_work_item WHERE id = $1::uuid
        """,
        ids["aggregate_work_id"],
    )
    assert aggregate["status"] == "cancelled"
    assert aggregate["last_error_code"] == "WORKFLOW_CANCELED"
    assert aggregate["lease_owner"] is None
    assert aggregate["lease_token"] is None
    assert aggregate["finished_at"] is not None
    assert await postgres_case.connection.fetchval(
        "SELECT status FROM qc_aggregate_inspection_attempt WHERE id = $1::uuid",
        ids["aggregate_attempt_id"],
    ) == "abandoned"
    aggregate_candidates = await postgres_case.connection.fetch(
        """
        SELECT status, disposition_code, disposition_message, finished_at
        FROM qc_aggregate_inspection_candidate
        WHERE id = ANY($1::uuid[]) ORDER BY id
        """,
        ids["aggregate_candidate_ids"],
    )
    assert len(aggregate_candidates) == 2
    assert {row["status"] for row in aggregate_candidates} == {"failed"}
    assert {row["disposition_code"] for row in aggregate_candidates} == {
        "WORKFLOW_CANCELED"
    }
    assert all(row["disposition_message"] for row in aggregate_candidates)
    assert all(row["finished_at"] is not None for row in aggregate_candidates)
    evidence_requests = await postgres_case.connection.fetch(
        """
        SELECT status, resolution_reason, completed_at, row_version
        FROM qc_evidence_request
        WHERE request_id = ANY($1::varchar[]) ORDER BY request_id
        """,
        ids["evidence_request_ids"],
    )
    assert len(evidence_requests) == 3
    assert {row["status"] for row in evidence_requests} == {"cancelled"}
    assert {row["resolution_reason"] for row in evidence_requests} == {
        "WORKFLOW_CANCELED"
    }
    assert all(row["completed_at"] is not None for row in evidence_requests)
    assert {row["row_version"] for row in evidence_requests} == {1}
    report_attempt = await postgres_case.connection.fetchrow(
        """
        SELECT status, failure_code, finished_at
        FROM qc_web_report_generation_attempt WHERE id = $1::uuid
        """,
        ids["report_attempt_id"],
    )
    assert dict(report_attempt) == {
        "status": "cancelled",
        "failure_code": "WORKFLOW_CANCELED",
        "finished_at": report_attempt["finished_at"],
    }
    assert report_attempt["finished_at"] is not None
    restoration = await postgres_case.connection.fetchrow(
        """
        SELECT status, error_code, error_message, finished_at, row_version
        FROM qc_restoration_attempt
        WHERE task_id = $1::uuid AND attempt_id = $2
        """,
        task_id,
        f"web-terminal-restoration:{task_id}",
    )
    assert restoration["status"] == "failed"
    assert restoration["error_code"] == "WORKFLOW_CANCELED"
    assert restoration["error_message"] == "Web 正式任务已取消"
    assert restoration["finished_at"] is not None
    assert restoration["row_version"] == 1
    probe = await postgres_case.connection.fetchrow(
        """
        SELECT status, last_error_code, last_error_message, finished_at, row_version
        FROM qc_v2_work_item
        WHERE work_id = $1
        """,
        f"web-terminal-probe:{task_id}",
    )
    assert probe["status"] == "cancelled"
    assert probe["last_error_code"] == "WORKFLOW_CANCELED"
    assert probe["last_error_message"] == "Web 正式任务已取消"
    assert probe["finished_at"] is not None
    assert probe["row_version"] == 1
    action = await postgres_case.connection.fetchrow(
        """
        SELECT status, error_code, error_message, action_finished_at, row_version
        FROM qc_action_execution
        WHERE action_execution_id = $1
        """,
        f"web-terminal-restoration-action:{task_id}",
    )
    assert action["status"] == "failed"
    assert action["error_code"] == "WORKFLOW_CANCELED"
    assert action["error_message"] == "Web 正式任务已取消"
    assert action["action_finished_at"] is not None
    assert action["row_version"] == 1


async def test_web_review_marks_aggregate_candidates_needs_review_idempotently(
    postgres_case: _PostgresCase,
) -> None:
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        run_status="running",
        with_session=True,
    )
    ids = await _seed_nonterminal_formal_ledgers(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    repository = _new_repository(postgres_case)

    first = await repository.terminalize(
        task_id=task_id,
        agent_run_id=run_id,
        outcome="needs_review",
        reason_code="WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW",
        reason_message="Web 页面类型需要人工复核",
    )
    replay = await repository.terminalize(
        task_id=task_id,
        agent_run_id=run_id,
        outcome="needs_review",
        reason_code="WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW",
        reason_message="Web 页面类型需要人工复核",
    )

    assert first.aggregate_candidate_count == 2
    assert first.evidence_request_count == 3
    assert first.report_attempt_count == 1
    assert first.restoration_attempt_count == 1
    assert first.terminalized_count == 17
    assert replay.aggregate_candidate_count == 0
    assert replay.evidence_request_count == 0
    assert replay.report_attempt_count == 0
    assert replay.restoration_attempt_count == 0
    assert replay.terminalized_count == 0
    candidate_rows = await postgres_case.connection.fetch(
        """
        SELECT status, disposition_code, disposition_message, finished_at
        FROM qc_aggregate_inspection_candidate
        WHERE id = ANY($1::uuid[])
        """,
        ids["aggregate_candidate_ids"],
    )
    assert len(candidate_rows) == 2
    assert {row["status"] for row in candidate_rows} == {"needs_review"}
    assert {row["disposition_code"] for row in candidate_rows} == {
        "WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW"
    }
    assert all(row["disposition_message"] for row in candidate_rows)
    assert all(row["finished_at"] is not None for row in candidate_rows)
    request_rows = await postgres_case.connection.fetch(
        """
        SELECT status, resolution_reason, completed_at, row_version
        FROM qc_evidence_request
        WHERE request_id = ANY($1::varchar[])
        """,
        ids["evidence_request_ids"],
    )
    assert len(request_rows) == 3
    assert {row["status"] for row in request_rows} == {"unfulfillable"}
    assert {row["resolution_reason"] for row in request_rows} == {
        "WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW"
    }
    assert all(row["completed_at"] is not None for row in request_rows)
    assert {row["row_version"] for row in request_rows} == {1}
    report_attempt = await postgres_case.connection.fetchrow(
        """
        SELECT status, failure_code, finished_at
        FROM qc_web_report_generation_attempt WHERE id = $1::uuid
        """,
        ids["report_attempt_id"],
    )
    assert dict(report_attempt) == {
        "status": "failed",
        "failure_code": "WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW",
        "finished_at": report_attempt["finished_at"],
    }
    assert report_attempt["finished_at"] is not None
    restoration = await postgres_case.connection.fetchrow(
        """
        SELECT status, error_code, error_message, finished_at, row_version
        FROM qc_restoration_attempt
        WHERE task_id = $1::uuid AND attempt_id = $2
        """,
        task_id,
        f"web-terminal-restoration:{task_id}",
    )
    assert restoration["status"] == "failed"
    assert restoration["error_code"] == "WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW"
    assert restoration["error_message"] == "Web 页面类型需要人工复核"
    assert restoration["finished_at"] is not None
    assert restoration["row_version"] == 1
    assert await postgres_case.connection.fetchval(
        "SELECT status FROM qc_v2_work_item WHERE work_id = $1",
        f"web-terminal-probe:{task_id}",
    ) == "failed"
    action = await postgres_case.connection.fetchrow(
        """
        SELECT status, error_code, action_finished_at, row_version
        FROM qc_action_execution
        WHERE action_execution_id = $1
        """,
        f"web-terminal-restoration-action:{task_id}",
    )
    assert action["status"] == "failed"
    assert action["error_code"] == "WEB_PAGE_TYPE_ASSESSMENT_NEEDS_REVIEW"
    assert action["action_finished_at"] is not None
    assert action["row_version"] == 1


async def test_web_cancel_recovery_seals_all_formal_ledgers_before_task_canceled(
    postgres_case: _PostgresCase,
    monkeypatch,
    caplog,
) -> None:
    usage_repository = worker_module.PostgresLLMUsageEventRepository
    monkeypatch.setattr(worker_module, "PostgresLLMUsageEventRepository",
                        lambda: usage_repository(pool=postgres_case.pool))
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        run_status="running",
        with_session=True,
    )
    await _seed_nonterminal_formal_ledgers(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    state_store = PostgresAgentRunStateStore(pool=postgres_case.pool)
    assert await state_store.record_cancel_requested(run_id)
    record = await state_store.get_record_snapshot(run_id=run_id)
    assert record is not None
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key=f"test:web-terminal-ledger-cancel:{run_id}",
    )
    await queue.save(record)

    async def _no_exploration_work(**_kwargs: object) -> int:
        return 0

    monkeypatch.setattr(
        worker_module,
        "_cancel_h5_nonterminal_work",
        _no_exploration_work,
    )
    runtime, browser, gate = _runtime_with_postgres_session(postgres_case)
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)
    repository = _new_repository(postgres_case)

    await worker_module._reconcile_canceled_run(
        queue,
        state_store,
        record,
        web_formal_session_closer=closer,
        web_formal_terminal_ledger_finalizer=repository,
    )
    record.cancellation_finalized = False
    await worker_module._reconcile_canceled_run(
        queue,
        state_store,
        record,
        web_formal_session_closer=closer,
        web_formal_terminal_ledger_finalizer=repository,
    )

    facts = await postgres_case.connection.fetchrow(
        """
        SELECT
            task.status AS task_status,
            run.status AS run_status,
            session.status AS session_status,
            (session.lease_owner IS NULL AND session.lease_token IS NULL
             AND session.lease_expires_at IS NULL) AS lease_revoked,
            (SELECT count(*) FROM qc_completion_gate_snapshot AS snapshot
             WHERE snapshot.task_id = task.id)::integer AS gate_count,
            (SELECT count(*) FROM qc_report_gate_binding AS report
             WHERE report.task_id = task.id)::integer AS report_count,
            (SELECT count(*) FROM qc_h5_inspection_publication AS item
             WHERE item.task_id = task.id
               AND item.status IN ('pending', 'leased', 'retry_wait'))::integer
                AS open_publication_count,
            (SELECT count(*) FROM qc_inspection_work_item AS item
             WHERE item.task_id = task.id
               AND item.status IN (
                   'pending', 'leased', 'running', 'retry_wait', 'waiting_evidence'
               ))::integer AS open_page_work_count,
            (SELECT count(*) FROM qc_aggregate_inspection_work_item AS item
             WHERE item.task_id = task.id
               AND item.status IN (
                   'pending', 'leased', 'running', 'retry_wait'
               ))::integer AS open_aggregate_work_count
        FROM qc_task AS task
        INNER JOIN qc_agent_run AS run
          ON run.task_id = task.id AND run.id = $2::uuid
        INNER JOIN qc_surface_session AS session
          ON session.task_id = task.id AND session.session_id = $3
        WHERE task.id = $1::uuid
        """,
        task_id,
        run_id,
        session_id,
    )
    assert dict(facts) == {
        "task_status": "canceled",
        "run_status": "canceled",
        "session_status": "lost",
        "lease_revoked": True,
        "gate_count": 0,
        "report_count": 0,
        "open_publication_count": 0,
        "open_page_work_count": 0,
        "open_aggregate_work_count": 0,
    }
    assert browser.closed_sessions == [session_id, session_id]
    assert gate.released_sessions == [(task_id, session_id), (task_id, session_id)]
    assert "T10_PG_OUTBOUND_BLOCKED_BEFORE_AUTH" not in caplog.text
    assert "LLM usage" not in caplog.text


async def test_failed_capture_terminalizes_other_capture_formal_ledgers(
    postgres_case: _PostgresCase,
) -> None:
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        run_status="running",
        with_session=True,
    )
    ids = await _seed_nonterminal_formal_ledgers(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        include_evidence_requests=False,
    )
    blocker_capture_id = await postgres_case.connection.fetchval(
        """
        SELECT parent_page_capture_id::text
        FROM qc_page_capture
        WHERE id = (
            SELECT page_capture_id
            FROM qc_h5_inspection_publication
            WHERE id = $1::uuid
        )
        """,
        ids["publication_id"],
    )
    assert isinstance(blocker_capture_id, str)
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_evidence_manifest (
            manifest_id, manifest_version, task_id, session_id,
            capture_id, protocol_version, manifest_hash, payload_hash,
            quality_status, quality_reason, manifest_payload, created_at
        )
        SELECT
            $3, latest.manifest_version + 1, latest.task_id, latest.session_id,
            latest.capture_id, latest.protocol_version, $4, latest.payload_hash,
            'fail', 'artifact_integrity_failed',
            latest.manifest_payload || '{"quality_status":"fail"}'::jsonb,
            now()
        FROM qc_evidence_manifest AS latest
        WHERE latest.task_id = $1::uuid
          AND latest.capture_id = $2::uuid
        ORDER BY latest.manifest_version DESC
        LIMIT 1
        """,
        task_id,
        blocker_capture_id,
        f"web-terminal-failed-manifest:{blocker_capture_id}",
        f"sha256:{'f' * 64}",
    )
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_web_exploration_budget (
            agent_run_id, site_id, task_id, session_id,
            target_origin_sha256, catalog_version, catalog_sha256,
            state, sealed_at
        ) VALUES (
            $1::uuid, 'web-terminal-ledger-site', $2::uuid, $3,
            $4, 'web-terminal-ledger-v1', $5, 'sealed', now()
        )
        """,
        run_id,
        task_id,
        session_id,
        "c" * 64,
        "d" * 64,
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = 'done', stop_reason = 'completed', finished_at = now()
        WHERE id = $1::uuid
        """,
        run_id,
    )
    session_repository = PostgresSurfaceSessionRepository(pool=postgres_case.pool)
    active_session = await session_repository.get(session_id=session_id)
    assert active_session is not None
    draining_session = await session_repository.begin_draining(
        task_id=task_id,
        session_id=session_id,
        expected_row_version=active_session.row_version,
    )
    closing_session = await session_repository.seal_for_close(
        task_id=task_id,
        session_id=session_id,
        expected_row_version=draining_session.row_version,
    )
    await session_repository.mark_closed(
        task_id=task_id,
        session_id=session_id,
        expected_row_version=closing_session.row_version,
    )
    ids["evidence_request_ids"] = await _seed_nonterminal_evidence_requests(
        postgres_case,
        task_id=task_id,
        session_id=session_id,
        work_item_id=str(ids["request_work_item_id"]),
        attempt_id=str(ids["request_attempt_id"]),
        capture_id=str(ids["request_capture_id"]),
    )
    await postgres_case.connection.execute(
        """
        UPDATE qc_task
        SET status = 'running',
            finalization_state = 'session_draining',
            exploration_draining_at = now(),
            session_draining_at = now(),
            finalization_agent_run_id = $2::uuid,
            finalization_row_version = 2
        WHERE id = $1::uuid
        """,
        task_id,
        run_id,
    )
    snapshot_repository = PostgresWebCoverageSnapshotRepository(
        pool=postgres_case.pool
    )
    reconciliation = WebInspectionReconciliation(
        state_reader=snapshot_repository,
        manifest_finalizer=None,
        publication_publisher=None,
        active_session_drainer=_NoopDrainer(),
        session_closer=None,
        finalization=TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        snapshot_repository=snapshot_repository,
        gap_rule_reader=snapshot_repository,
        rule_execution_service=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
        final_handoff=_ForbiddenFormalWriter(),
        terminal_ledger_finalizer=_new_repository(postgres_case),
    )

    result = await reconciliation.reconcile(
        ReconcileWebInspectionCommand(
            task_id=task_id,
            agent_run_id=run_id,
            finalization_epoch=0,
        )
    )

    assert result.status == "failed"
    facts = await postgres_case.connection.fetchrow(
        """
        SELECT
            task.status AS task_status,
            task.execution_status,
            publication.status AS publication_status,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_inspection_work_item
             WHERE task_id = task.id) AS page_work_statuses,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_aggregate_inspection_work_item
             WHERE task_id = task.id) AS aggregate_work_statuses,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_aggregate_inspection_candidate
             WHERE task_id = task.id) AS aggregate_candidate_statuses,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_evidence_request
             WHERE task_id = task.id) AS evidence_request_statuses,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_web_report_generation_attempt
             WHERE task_id = task.id) AS report_attempt_statuses,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_restoration_attempt
             WHERE task_id = task.id
               AND attempt_id LIKE 'web-terminal-restoration:%')
                AS restoration_attempt_statuses,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_v2_work_item
             WHERE task_id = task.id AND work_type = 'probe') AS probe_work_statuses,
            (SELECT array_agg(DISTINCT status ORDER BY status)
             FROM qc_action_execution
             WHERE task_id = task.id
               AND action_execution_id LIKE 'web-terminal-restoration-action:%')
                AS restoration_action_statuses,
            (SELECT count(*) FROM qc_report_gate_binding AS report
             WHERE report.task_id = task.id)::integer AS report_count
        FROM qc_task AS task
        INNER JOIN qc_h5_inspection_publication AS publication
          ON publication.task_id = task.id
        WHERE task.id = $1::uuid
        """,
        task_id,
    )
    assert dict(facts) == {
        "task_status": "failed",
        "execution_status": "failed",
        "publication_status": "failed",
        "page_work_statuses": ["failed"],
        "aggregate_work_statuses": ["failed"],
        "aggregate_candidate_statuses": ["failed"],
        "evidence_request_statuses": ["unfulfillable"],
        "report_attempt_statuses": ["failed"],
        "restoration_attempt_statuses": ["failed"],
        "probe_work_statuses": ["failed"],
        "restoration_action_statuses": ["failed"],
        "report_count": 0,
    }
