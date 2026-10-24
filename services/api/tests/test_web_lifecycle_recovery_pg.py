"""Web 正式收口异常与取消恢复的真实 PostgreSQL 回归。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import fakeredis

from services.api.app import worker as worker_module
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemService,
)
from services.api.app.application.rule_execution import RuleExecutionService
from services.api.app.application.task_finalization import TaskFinalizationService
from services.api.app.application.web_formal_session_closure import (
    DurableWebFormalSessionCloser,
)
from services.api.app.application.web_inspection_reconciliation import (
    ReconcileWebInspectionCommand,
    WebInspectionReconciliation,
)
from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_inspection_notification import (
    WebPageInspectionNotification,
    WebPageInspectionNotifyingClassifier,
)
from services.api.app.application.web_page_type_assessment import (
    WebPageTypeAssessmentRecorder,
    WebPageTypeAssessmentRecordingClassifier,
)
from services.api.app.infrastructure.browser.capture_runtime import WebCaptureRuntime
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.task_finalization_repository import (
    PostgresTaskFinalizationRepository,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository
from services.api.app.infrastructure.db.web_capture_evidence_repository import (
    PostgresWebCaptureEvidenceRepository,
)
from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
    PostgresWebCoverageSnapshotRepository,
)
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import PostgresAgentRunStateStore
from services.api.tests.test_web_v2_formal_report_e2e import (
    _post_task,
    _PostgresCase,
)
from services.api.tests.test_web_v2_formal_report_e2e import (
    postgres_case as postgres_case,
)


class _Browser:
    def __init__(self) -> None:
        self.closed_sessions: list[str] = []

    async def close_session(self, session_id: str) -> None:
        self.closed_sessions.append(session_id)


class _CommandGate:
    def __init__(self, case: _PostgresCase) -> None:
        from services.api.app.infrastructure.db.session_command_gate import (
            PostgresSessionCommandGate,
        )

        self._delegate = PostgresSessionCommandGate(repository=PostgresSurfaceSessionRepository(pool=case.pool),
            lease_owner="t10-web-lifecycle", session_owner="web-v2-workflow")
        self.released_sessions: list[tuple[str, str]] = []

    async def release_session(self, *, task_id: str, session_id: str) -> None:
        self.released_sessions.append((task_id, session_id))
        await self._delegate.release_session(task_id=task_id, session_id=session_id)

    async def cancel_session(self, **kwargs):
        return await self._delegate.cancel_session(**kwargs)


class _NoopDrainer:
    async def drain_before_close(self, **_kwargs: object) -> None:
        return None


class _ForbiddenFormalWriter:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"待复核 Capture 不得调用正式写入：{name}")


class _PassPendingManifestFinalizer:
    """只替代对象完整性计算，状态转换仍提交到真实 PG Manifest。"""

    def __init__(self, case: _PostgresCase) -> None:
        self._case = case

    async def finalize_session(self, *, task_id, session_id, agent_run_id, **_kwargs):
        # 仍仅替代对象完整性；限定真实 Task/Run/Session 枚举，不伪造页面或正式判定。
        captures = await self._case.connection.fetch("SELECT id::text FROM qc_capture WHERE task_id=$1::uuid "
            "AND agent_run_id=$2::uuid AND surface_session_id=$3", task_id, agent_run_id, session_id)
        for capture in captures:
            await self.finalize_capture(task_id=task_id, capture_id=capture["id"])

    async def finalize_capture(self, *, task_id: str, capture_id: str) -> None:
        await self._case.connection.execute(
            """
            INSERT INTO qc_evidence_manifest (
                manifest_id, manifest_version, task_id, session_id,
                capture_id, protocol_version, manifest_hash, payload_hash,
                quality_status, quality_reason, manifest_payload, created_at
            )
            SELECT
                $3, latest.manifest_version + 1, latest.task_id, latest.session_id,
                latest.capture_id, latest.protocol_version, $4,
                latest.payload_hash, 'pass', 'artifact_integrity_verified',
                latest.manifest_payload || jsonb_build_object(
                    'quality_status', 'pass',
                    'quality_reason', 'artifact_integrity_verified'
                ),
                now()
            FROM qc_evidence_manifest AS latest
            WHERE latest.task_id = $1::uuid
              AND latest.capture_id = $2::uuid
              AND latest.quality_status = 'pending'
              AND NOT EXISTS (
                  SELECT 1
                  FROM qc_evidence_manifest AS terminal
                  WHERE terminal.task_id = latest.task_id
                    AND terminal.capture_id = latest.capture_id
                    AND terminal.manifest_version > latest.manifest_version
              )
            ON CONFLICT (capture_id, manifest_version) DO NOTHING
            """,
            task_id,
            capture_id,
            f"web-review-pass:{capture_id}",
            f"sha256:web-review-pass:{capture_id}",
        )


def _runtime_with_postgres_session(
    case: _PostgresCase,
) -> tuple[WebCaptureRuntime, _Browser, _CommandGate]:
    browser = _Browser()
    gate = _CommandGate(case)
    runtime = WebCaptureRuntime(
        coordinator=SimpleNamespace(),  # type: ignore[arg-type]
        session_command_gate=gate,  # type: ignore[arg-type]
        _browser=browser,  # type: ignore[arg-type]
        _executor=SimpleNamespace(),  # type: ignore[arg-type]
        _evidence_buffer=SimpleNamespace(),  # type: ignore[arg-type]
        surface_sessions=PostgresSurfaceSessionRepository(pool=case.pool),
    )
    return runtime, browser, gate


async def _start_formal_task(
    case: _PostgresCase,
    *,
    run_status: str = "running",
    run_error: str | None = None,
    with_session: bool,
) -> tuple[str, str, str]:
    response = await _post_task(case)
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    run_id = response.json()["run_id"]
    session_id = f"web:{run_id}"
    await case.connection.execute(
        """
        UPDATE qc_task
        SET status = 'running', started_at = now(),
            finalization_state = 'open', finalization_agent_run_id = $2::uuid
        WHERE id = $1::uuid
        """,
        task_id,
        run_id,
    )
    await case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = $2::varchar, started_at = now(), finished_at = CASE
                WHEN $2::varchar IN ('done', 'failed', 'aborted') THEN now()
                ELSE NULL
            END,
            error_message = $3::text
        WHERE id = $1::uuid
        """,
        run_id,
        run_status,
        run_error,
    )
    if with_session:
        await case.connection.execute(
            """
            INSERT INTO qc_surface_session (
                session_id, task_id, surface_kind, session_owner, status,
                finalization_epoch, activated_at
            ) VALUES ($1, $2::uuid, 'web', 'web-v2-workflow', 'active', 0, now())
            """,
            session_id,
            task_id,
        )
    return task_id, run_id, session_id


async def _seed_review_capture(
    case: _PostgresCase,
    *,
    task_id: str,
    run_id: str,
    session_id: str,
) -> WebReadyPageCapture:
    before_capture_id = str(uuid4())
    capture_id = str(uuid4())
    action_id = f"web-review:{task_id}"
    async with case.connection.transaction():
        await case.connection.execute("SET CONSTRAINTS ALL DEFERRED")
        await case.connection.execute(
            """
            INSERT INTO qc_action_execution (
                action_execution_id, task_id, session_id, intent_id,
                idempotency_key, intent_hash, intent_protocol_version,
                intent_payload, command_sequence, finalization_epoch,
                requested_source_capture_id, source_capture_id, result_capture_id,
                status, result_payload, started_at, action_finished_at,
                evidence_finished_at
            ) VALUES (
                $1, $2::uuid, $3, $4, $4, $5, 'web-v2-workflow/v1',
                '{}'::jsonb, 1, 0, $6::uuid, $6::uuid, $7::uuid,
                'completed', '{}'::jsonb, now(), now(), now()
            )
            """,
            action_id,
            task_id,
            session_id,
            f"web-review-intent:{task_id}",
            "d" * 64,
            before_capture_id,
            capture_id,
        )
        for page_seq, current_id, parent_id, role, artifact_types in (
            (1, before_capture_id, None, "before_action", ("screenshot", "dom")),
            (
                2,
                capture_id,
                before_capture_id,
                "after_action",
                ("screenshot", "dom", "visible_text", "action_trace"),
            ),
        ):
            await case.connection.execute(
                """
                INSERT INTO qc_page_capture (
                    id, task_id, agent_run_id, parent_page_capture_id,
                    page_seq, page_slug, url, page_type, page_state,
                    capture_status, captured_at, inspection_status,
                    surface_session_id, capture_role, action_execution_id,
                    session_command_sequence, metadata
                ) VALUES (
                    $1::uuid, $2::uuid, $3::uuid, $4::uuid,
                    $5, $6, $7, 'content_list', 'loaded',
                    'captured', now(), 'evaluating', $8, $9, $10, 1,
                    '{"finalization_epoch":0}'::jsonb
                )
                """,
                current_id,
                task_id,
                run_id,
                parent_id,
                page_seq,
                f"review-{page_seq}",
                f"https://example.test/review/{page_seq}",
                session_id,
                role,
                action_id,
            )
            for artifact_type in artifact_types:
                await case.connection.execute(
                    """
                    INSERT INTO qc_evidence_artifact (
                        task_id, page_capture_id, artifact_type, source,
                        storage_path, content_type, checksum, size_bytes
                    ) VALUES (
                        $1::uuid, $2::uuid, $3, 'baseline', $4, $5, $6, 1
                    )
                    """,
                    task_id,
                    current_id,
                    artifact_type,
                    f"tests/web-review/{current_id}/{artifact_type}",
                    "image/png" if artifact_type == "screenshot" else "text/plain",
                    f"sha256:{page_seq:x}".ljust(71, "e"),
                )
            await case.connection.execute(
                """
                INSERT INTO qc_evidence_manifest (
                    manifest_id, manifest_version, task_id, session_id,
                    capture_id, protocol_version, manifest_hash, payload_hash,
                    quality_status, manifest_payload, created_at
                ) VALUES (
                    $1, 1, $2::uuid, $3, $4::uuid,
                    'evidence-manifest.v1', $5, $6, 'pending', '{}'::jsonb, now()
                )
                """,
                f"web-review-manifest:{current_id}",
                task_id,
                session_id,
                current_id,
                f"sha256:{'3' * 64}",
                f"sha256:{'4' * 64}",
            )
    artifacts = await case.connection.fetch(
        """
        SELECT id::text AS artifact_id, artifact_type
        FROM qc_evidence_artifact
        WHERE task_id = $1::uuid AND page_capture_id = $2::uuid
        """,
        task_id,
        capture_id,
    )
    artifact_ids = {str(row["artifact_type"]): str(row["artifact_id"]) for row in artifacts}
    captured_at = await case.connection.fetchval(
        "SELECT captured_at FROM qc_page_capture WHERE id = $1::uuid",
        capture_id,
    )
    assert isinstance(captured_at, datetime)
    return WebReadyPageCapture(
        capture_id=capture_id,
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id=artifact_ids["screenshot"],
        dom_artifact_id=artifact_ids["dom"],
        visible_text_artifact_id=artifact_ids["visible_text"],
        page_role="menu",
        structure_facts=WebPageStructureFacts(
            navigation_element_count=1,
            repeated_link_count=1,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("content-list",),
        ),
        captured_at=captured_at.astimezone(UTC),
    )


async def test_review_capture_finalizes_manifest_and_reconciliation_seals_review(
    postgres_case: _PostgresCase,
) -> None:
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        run_status="running",
        with_session=True,
    )
    capture = await _seed_review_capture(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    assessment = WebPageAssessment(
        page_type=None,
        raw_page_type="content_list",
        confidence=0.3,
        evidence=("结构证据不足",),
        needs_review_reason="WEB_PAGE_TYPE_LOW_CONFIDENCE",
    )

    class Classifier:
        async def classify(self, _capture: object) -> WebPageAssessment:
            return assessment

    notification = WebPageInspectionNotification(
        reader=PostgresWebCaptureEvidenceRepository(pool=postgres_case.pool),
        inventory=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
        publication_writer=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
        manifest_finalizer=_PassPendingManifestFinalizer(postgres_case),
    )
    classifier = WebPageInspectionNotifyingClassifier(
        classifier=WebPageTypeAssessmentRecordingClassifier(
            classifier=Classifier(),
            recorder=WebPageTypeAssessmentRecorder(
                repository=PostgresWebCaptureEvidenceRepository(
                    pool=postgres_case.pool,
                    assessment_schema_version="web-page-type-assessment.v2",
                ),
                # 本用例冻结四类页型的历史 v2 合同，不使用 v4 默认 Catalog。
                assessment_schema_version="web-page-type-assessment.v2",
            ),
            task_id=task_id,
            agent_run_id=run_id,
            session_id=session_id,
        ),
        notification=notification,
        task_id=task_id,
        agent_run_id=run_id,
        session_id=session_id,
    )

    classified = await classifier.classify(capture)
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = 'done', finished_at = now()
        WHERE id = $1::uuid
        """,
        run_id,
    )

    assert classified.page_type is None
    manifest_statuses = await postgres_case.connection.fetchval(
        """
        SELECT array_agg(DISTINCT quality_status ORDER BY quality_status)
        FROM (
            SELECT DISTINCT ON (capture_id) capture_id, quality_status
            FROM qc_evidence_manifest
            WHERE task_id = $1::uuid
            ORDER BY capture_id, manifest_version DESC
        ) AS latest
        """,
        task_id,
    )
    assert manifest_statuses == ["pass"]
    assert await postgres_case.connection.fetchval(
        "SELECT count(*) FROM qc_h5_inspection_publication WHERE task_id = $1::uuid",
        task_id,
    ) == 0
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_web_exploration_budget (
            agent_run_id, site_id, task_id, session_id,
            target_origin_sha256, catalog_version, catalog_sha256,
            state, sealed_at
        ) VALUES (
            $1::uuid, 'web-review-site', $2::uuid, $3,
            $4, 'web-review-v1', $5, 'sealed', now()
        )
        """,
        run_id,
        task_id,
        session_id,
        "a" * 64,
        "b" * 64,
    )
    # 待复核页型只能留下未分类终态，不能伪造已成功分类的菜单证据。
    # 正式分类库存只认本轮菜单账本关联，不能仅有游离 Capture。
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_web_menu_ledger_entry (
            agent_run_id, site_id, menu_entry_id, catalog_position, decision,
            menu_path, canonical_url_sha256, execution_slot, status,
            menu_action_count, menu_intent_id, menu_source_capture_id,
            menu_action_execution_id, menu_probe_capture_id, menu_ready_capture_id
        )
        SELECT $1::uuid, 'web-review-site', 't10-review-menu', 0,
               'allowlisted_observe_only', '[]'::jsonb, $4, 1,
               'page_type_unclassified', 1, action.intent_id, action.source_capture_id,
               action.action_execution_id, action.result_capture_id, action.result_capture_id
        FROM qc_action_execution AS action
        WHERE action.task_id=$2::uuid AND action.result_capture_id=$3::uuid
        """, run_id, task_id, capture.capture_id, "c" * 64
    )
    runtime, browser, gate = _runtime_with_postgres_session(postgres_case)
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)
    snapshot_repository = PostgresWebCoverageSnapshotRepository(pool=postgres_case.pool)
    reconciliation = WebInspectionReconciliation(
        state_reader=snapshot_repository,
        manifest_finalizer=None,
        publication_publisher=None,
        active_session_drainer=_NoopDrainer(),
        session_closer=closer,
        finalization=TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        snapshot_repository=snapshot_repository,
        gap_rule_reader=snapshot_repository,
        rule_execution_service=RuleExecutionService(
            repository=PostgresTaskRepository(pool=postgres_case.pool)
        ),
        final_handoff=notification,
    )
    command = ReconcileWebInspectionCommand(
        task_id=task_id,
        agent_run_id=run_id,
        finalization_epoch=0,
    )

    first = await reconciliation.reconcile(command)
    second = await reconciliation.reconcile(command)

    assert first.status == "waiting_for_run_terminal"
    assert second.status == "needs_review"
    # 覆盖缺口是任务级证据不足，不是待复核页面的正式 Judge 判定。
    gap_rows = await postgres_case.connection.fetch(
        "SELECT execution_status, page_capture_id, source_idempotency_key "
        "FROM qc_rule_execution WHERE task_id=$1::uuid", task_id
    )
    assert gap_rows
    assert all(row["execution_status"] == "evidence_insufficient"
               and row["page_capture_id"] is None
               and row["source_idempotency_key"].startswith("web-coverage-gap:")
               for row in gap_rows)
    frozen_required = await postgres_case.connection.fetchval(
        "SELECT required_page_types FROM qc_web_coverage_snapshot "
        "WHERE task_id=$1::uuid AND agent_run_id=$2::uuid", task_id, run_id
    )
    assert set(json.loads(frozen_required)) == {
        "content_list", "content_detail", "video_list", "video_detail"
    }
    task = await postgres_case.connection.fetchrow(
        """
        SELECT status, finalization_state, execution_status
        FROM qc_task WHERE id = $1::uuid
        """,
        task_id,
    )
    assert dict(task) == {
        "status": "review",
        "finalization_state": "sealed",
        "execution_status": "needs_review",
    }
    assert browser.closed_sessions == [session_id]
    assert gate.released_sessions == [(task_id, session_id)]
    assert await postgres_case.connection.fetchval(
        "SELECT count(*) FROM qc_h5_inspection_publication WHERE task_id = $1::uuid",
        task_id,
    ) == 0


async def test_cancel_recovery_closes_postgres_session_before_atomic_canceled(
    postgres_case: _PostgresCase,
    monkeypatch,
    caplog,
) -> None:
    usage_repository = worker_module.PostgresLLMUsageEventRepository
    monkeypatch.setattr(worker_module, "PostgresLLMUsageEventRepository",
                        lambda: usage_repository(pool=postgres_case.pool))
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        with_session=True,
    )
    state_store = PostgresAgentRunStateStore(pool=postgres_case.pool)
    assert await state_store.record_cancel_requested(run_id)
    record = await state_store.get_record_snapshot(run_id=run_id)
    assert record is not None
    queue = RedisAgentRunQueue(
        fakeredis.aioredis.FakeRedis(decode_responses=True),
        ttl_seconds=3600,
        queue_key=f"test:web-cancel-pg:{run_id}",
    )
    await queue.save(record)
    monkeypatch.setattr(
        worker_module,
        "_cancel_h5_nonterminal_work",
        lambda **_kwargs: _async_zero(),
    )
    runtime, browser, gate = _runtime_with_postgres_session(postgres_case)
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)

    from services.api.app.infrastructure.db.web_formal_terminal_ledger_repository import (
        PostgresWebFormalTerminalLedgerRepository,
    )

    await worker_module._reconcile_canceled_run(
        queue,
        state_store,
        record,
        web_formal_session_closer=closer,
        web_formal_terminal_ledger_finalizer=PostgresWebFormalTerminalLedgerRepository(pool=postgres_case.pool),
    )

    facts = await postgres_case.connection.fetchrow(
        """
        SELECT task.status AS task_status, run.status AS run_status,
               session.status AS session_status,
               (session.lease_owner IS NULL AND session.lease_token IS NULL
                AND session.lease_expires_at IS NULL) AS lease_revoked,
               (SELECT count(*) FROM qc_completion_gate_snapshot AS gate_snapshot
                WHERE gate_snapshot.task_id = task.id)::integer AS gate_count,
               (SELECT count(*) FROM qc_report_gate_binding AS report
                WHERE report.task_id = task.id)::integer AS report_count
        FROM qc_task AS task
        INNER JOIN qc_agent_run AS run ON run.task_id = task.id AND run.id = $2::uuid
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
    }
    assert browser.closed_sessions == [session_id]
    assert gate.released_sessions == [(task_id, session_id)]
    assert "T10_PG_OUTBOUND_BLOCKED_BEFORE_AUTH" not in caplog.text
    assert "LLM usage" not in caplog.text


async def test_foundational_failure_without_registered_session_seals_failed(
    postgres_case: _PostgresCase,
) -> None:
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        run_status="failed",
        run_error="browser bootstrap unavailable",
        with_session=False,
    )
    runtime, browser, gate = _runtime_with_postgres_session(postgres_case)
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)
    snapshot_repository = PostgresWebCoverageSnapshotRepository(pool=postgres_case.pool)
    reconciliation = WebInspectionReconciliation(
        state_reader=snapshot_repository,
        manifest_finalizer=None,
        publication_publisher=None,
        active_session_drainer=_NoopDrainer(),
        session_closer=closer,
        finalization=TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        snapshot_repository=snapshot_repository,
        gap_rule_reader=snapshot_repository,
        rule_execution_service=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
        final_handoff=WebPageInspectionNotification(
            reader=PostgresWebCaptureEvidenceRepository(pool=postgres_case.pool),
            inventory=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
            publication_writer=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
        ),
    )
    command = ReconcileWebInspectionCommand(
        task_id=task_id,
        agent_run_id=run_id,
        finalization_epoch=0,
    )

    first = await reconciliation.reconcile(command)
    second = await reconciliation.reconcile(command)

    assert first.status == "waiting_for_run_terminal"
    assert second.status == "failed"
    facts = await postgres_case.connection.fetchrow(
        """
        SELECT status, finalization_state, execution_status,
               completion_gate_snapshot_id,
               (SELECT count(*) FROM qc_report_gate_binding AS report
                WHERE report.task_id = qc_task.id)::integer AS report_count
        FROM qc_task WHERE id = $1::uuid
        """,
        task_id,
    )
    assert facts["status"] == "failed"
    assert facts["finalization_state"] == "sealed"
    assert facts["execution_status"] == "failed"
    assert facts["completion_gate_snapshot_id"] is not None
    assert facts["report_count"] == 0
    assert browser.closed_sessions == [session_id]
    assert gate.released_sessions == [(task_id, session_id)]


async def test_foundational_failure_terminalizes_pending_snapshot_work_before_task_failure(
    postgres_case: _PostgresCase,
) -> None:
    task_id, run_id, session_id = await _start_formal_task(
        postgres_case,
        run_status="running",
        with_session=True,
    )
    capture = await _seed_review_capture(
        postgres_case,
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
    )
    parent_capture_id = await postgres_case.connection.fetchval(
        "SELECT parent_page_capture_id::text FROM qc_page_capture WHERE id = $1::uuid",
        capture.capture_id,
    )
    assert isinstance(parent_capture_id, str)
    finalizer = _PassPendingManifestFinalizer(postgres_case)
    await finalizer.finalize_capture(task_id=task_id, capture_id=parent_capture_id)
    await finalizer.finalize_capture(task_id=task_id, capture_id=capture.capture_id)
    await postgres_case.connection.execute(
        """
        UPDATE qc_agent_run
        SET status = 'failed', error_message = NULL, finished_at = now()
        WHERE id = $1::uuid
        """,
        run_id,
    )
    work_id = await postgres_case.connection.fetchval(
        """
        INSERT INTO qc_inspection_work_item (
            task_id, page_capture_id, producer_agent_run_id,
            ruleset_id, ruleset_version, ruleset_snapshot_hash,
            protocol_version, batch_key, strategy, session_dependency,
            input_hash, priority, max_attempts, control_mode, finalization_epoch
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid,
            $4::uuid, 1, 'web-v2-e2e-snapshot',
            'inspection-work-item.v1', 'web-foundation-pending', 'hybrid',
            'snapshot', $5, 100, 3, 'enforced', 0
        )
        RETURNING id::text
        """,
        task_id,
        capture.capture_id,
        run_id,
        postgres_case.ruleset_id,
        f"sha256:web-foundation:{run_id}",
    )
    assert isinstance(work_id, str)
    await postgres_case.connection.execute(
        """
        INSERT INTO qc_inspection_work_item_rule (
            work_item_id, rule_id, rule_code, ordinal, contract_hash, status
        ) VALUES (
            $1::uuid, $2::uuid, 'web_e2e_observed', 0, $3, 'pending'
        )
        """,
        work_id,
        postgres_case.observed_rule_id,
        f"sha256:web-foundation-rule:{run_id}",
    )
    runtime, _browser, _gate = _runtime_with_postgres_session(postgres_case)
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)
    snapshot_repository = PostgresWebCoverageSnapshotRepository(pool=postgres_case.pool)
    reconciliation = WebInspectionReconciliation(
        state_reader=snapshot_repository,
        manifest_finalizer=None,
        publication_publisher=None,
        active_session_drainer=_NoopDrainer(),
        session_closer=closer,
        finalization=TaskFinalizationService(
            PostgresTaskFinalizationRepository(pool=postgres_case.pool)
        ),
        snapshot_repository=snapshot_repository,
        gap_rule_reader=snapshot_repository,
        rule_execution_service=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
        final_handoff=WebPageInspectionNotification(
            reader=PostgresWebCaptureEvidenceRepository(pool=postgres_case.pool),
            inventory=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
            publication_writer=_ForbiddenFormalWriter(),  # type: ignore[arg-type]
        ),
        failed_work_finalizer=InspectionWorkItemService(
            repository=PostgresInspectionWorkItemRepository(pool=postgres_case.pool)
        ),
    )
    command = ReconcileWebInspectionCommand(
        task_id=task_id,
        agent_run_id=run_id,
        finalization_epoch=0,
    )

    draining = await reconciliation.reconcile(command)
    failed = await reconciliation.reconcile(command)

    assert draining.status == "waiting_for_run_terminal"
    assert failed.status == "failed"
    facts = await postgres_case.connection.fetchrow(
        """
        SELECT task.status AS task_status,
               task.execution_status AS task_execution_status,
               work.status AS work_status,
               work.last_error_code AS work_error_code
        FROM qc_task AS task
        INNER JOIN qc_inspection_work_item AS work ON work.task_id = task.id
        WHERE task.id = $1::uuid
        """,
        task_id,
    )
    assert dict(facts) == {
        "task_status": "failed",
        "task_execution_status": "failed",
        "work_status": "failed",
        "work_error_code": "WEB_FOUNDATIONAL_FAILURE",
    }


async def _async_zero() -> int:
    return 0
