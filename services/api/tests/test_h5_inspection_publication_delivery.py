from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from typing import Any

import pytest

from services.api.app.agents.runtime.h5_inspection_publication_wakeup import (
    H5InspectionPublicationWakeupHandler,
)
from services.api.app.application.h5_inspection_publication import (
    H5InspectionPublicationClaimDisposition,
    H5InspectionPublicationClaimResult,
    H5InspectionPublicationLease,
)
from services.api.app.domain.inspection import (
    H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
)
from services.api.app.infrastructure.db.h5_consumer_run_lifecycle import (
    _settle_h5_page_inspection_statuses,
    finish_h5_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.h5_inspection_publication_repository import (
    H5InspectionConsumerRunIdentityError,
    PostgresH5InspectionPublicationRepository,
    WebActiveSessionWorkPendingError,
)
from services.api.app.infrastructure.queue.redis_stream import (
    DEFAULT_WAKEUP_STREAM_NAMES,
    RedisStreamWakeup,
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _Connection:
    def __init__(self, row: dict[str, object]) -> None:
        self.row = row
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
        return self.row

    async def execute(self, query: str, *args: object) -> str:
        self.execute_calls.append((query, args))
        return "UPDATE 1"


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> _Connection:
        return self.connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self.connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self.connection)


class _PublicationCompletionConnection(_Connection):
    """为 Publication 跨流竞态提供最小真实 SQL 边界替身。"""

    def __init__(self, *, live_work_exists: bool) -> None:
        super().__init__({})
        self.live_work_exists = live_work_exists
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        del args
        if not self.live_work_exists:
            return []
        if "work_item.status IN ('succeeded', 'partial')" in query:
            return []
        return [
            {
                "work_item_id": "work-live-1",
                "batch_key": "live-batch",
                "rule_id": "rule-live",
                "rule_code": "LIVE_RULE",
                "contract_hash": "hash-live",
            }
        ]

    async def fetchval(self, query: str, *args: object) -> object:
        self.fetchval_calls.append((query, args))
        if "INSERT INTO qc_h5_inspection_publication_work_item" in query:
            return "work-live-1"
        if "SELECT COALESCE(max(ordinal) + 1, 0)" in query:
            return 0
        if "SELECT rule.id::text" in query and "FROM qc_rule AS rule" in query:
            return "rule-live"
        if "INSERT INTO qc_h5_inspection_publication_rule" in query:
            return "rule-live"
        if "UPDATE qc_h5_inspection_publication" in query:
            return "publication-1"
        if "UPDATE qc_h5_evidence_inventory_item" in query:
            return "inventory-1"
        raise AssertionError(f"未覆盖的 fetchval SQL: {query}")

    async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
        self.fetchrow_calls.append((query, args))
        if "AS candidate_rule_count" in query:
            return {
                "candidate_rule_count": 1,
                "work_item_count": 1 if self.live_work_exists else 0,
            }
        raise AssertionError(f"未覆盖的 fetchrow SQL: {query}")


def _active_session_rule_result() -> object:
    return type(
        "PublishResult",
        (),
        {
            "status": "active_session_rules_excluded",
            "published_count": 0,
            "prepared_commands": (),
            "excluded_active_session_rule_count": 1,
            "excluded_active_session_rules": (
                {
                    "rule_id": "rule-live",
                    "rule_code": "LIVE_RULE",
                    "contract_hash": "hash-live",
                    "strategy": "vision",
                    "category": "content",
                    "execution_plan": {},
                },
            ),
            "page_type": "home",
        },
    )()


async def _stub_publication_completion(
    monkeypatch: pytest.MonkeyPatch,
    repository: PostgresH5InspectionPublicationRepository,
    *,
    target_platform: str,
    session_status: str,
) -> None:
    async def validate_identity(
        connection: object,
        *,
        lease: H5InspectionPublicationLease,
    ) -> dict[str, object]:
        del connection, lease
        return {
            "ruleset_id": "ruleset-1",
            "ruleset_version": 1,
            "ruleset_snapshot_hash": "ruleset-hash-1",
            "page_type": "home",
            "page_state": "stable",
            "target_platform": target_platform,
            "task_config": {
                "workflow_version": "v2",
                "execution_mode": "full_inspection",
                "h5_inspection_notification_mode": "enforced",
                "web_inspection_notification_mode": "enforced",
            },
            "session_status": session_status,
        }

    async def annotate(*args: object, **kwargs: object) -> None:
        del args, kwargs

    async def finish(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    monkeypatch.setattr(repository, "_validate_completion_identity", validate_identity)
    monkeypatch.setattr(repository, "_annotate_capture_in_transaction", annotate)
    monkeypatch.setattr(repository, "_finish_consumer_run_if_idle", finish)


def _wakeup(*, version: int = 1) -> RedisStreamWakeup:
    return RedisStreamWakeup(
        stream_name="alioth:v2:wakeup:h5-inspection-publication",
        stream_entry_id="1-0",
        message_id=f"event:{version}",
        message_type=H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
        task_id="task-1",
        aggregate_type="h5_inspection_publication",
        aggregate_id="publication-1",
        aggregate_version=version,
        idempotency_key=f"publication-1:{version}",
    )


@dataclass(frozen=True)
class _PublishResult:
    status: str
    published_count: int
    excluded_active_session_rule_count: int = 0
    items: tuple[dict[str, Any], ...] = ()


class _Repository:
    def __init__(self, claim: H5InspectionPublicationClaimResult) -> None:
        self.claim_result = claim
        self.completed: list[tuple[H5InspectionPublicationLease, object]] = []
        self.retried: list[tuple[H5InspectionPublicationLease, Exception]] = []

    async def claim(self, **kwargs: object) -> H5InspectionPublicationClaimResult:
        self.claim_kwargs = kwargs
        return self.claim_result

    async def complete(self, *, lease: H5InspectionPublicationLease, result: object) -> None:
        self.completed.append((lease, result))

    async def retry(self, *, lease: H5InspectionPublicationLease, error: Exception) -> None:
        self.retried.append((lease, error))


class _Publisher:
    def __init__(self, result: object | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def publish_for_capture(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _lease() -> H5InspectionPublicationLease:
    return H5InspectionPublicationLease(
        publication_id="publication-1",
        task_id="task-1",
        source_agent_run_id="run-1",
        consumer_agent_run_id="judge-run-1",
        page_capture_id="capture-1",
        page_type_assessment_id="assessment-1",
        assessment_hash="assessment-hash-1",
        ruleset_id="ruleset-1",
        ruleset_version=1,
        ruleset_snapshot_hash="ruleset-hash-1",
        finalization_epoch=0,
        page_type="home",
        page_state="stable",
        lease_token="lease-1",
        row_version=1,
        attempt_count=1,
        max_attempts=3,
    )


def test_publication_message_has_dedicated_stream() -> None:
    assert (
        DEFAULT_WAKEUP_STREAM_NAMES[H5_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE]
        == "alioth:v2:wakeup:h5-inspection-publication"
    )


async def test_publication_terminal_barrier_waits_for_expected_rows() -> None:
    class SequenceConnection(_Connection):
        def __init__(self) -> None:
            super().__init__({})
            self.rows = [
                {
                    "total_count": 0,
                    "nonterminal_count": 0,
                    "published_work_item_count": 0,
                    "excluded_active_session_rule_count": 0,
                    "needs_review_count": 0,
                    "failed_count": 0,
                },
                {
                    "total_count": 2,
                    "nonterminal_count": 0,
                    "published_work_item_count": 2,
                    "excluded_active_session_rule_count": 0,
                    "needs_review_count": 0,
                    "failed_count": 0,
                },
            ]

        async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
            del query, args
            return self.rows.pop(0)

    connection = SequenceConnection()
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))

    result = await repository.await_task_terminal(
        task_id="task-1",
        minimum_total_count=2,
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result.total_count == 2
    assert connection.rows == []


async def test_publication_terminal_barrier_reconciles_late_terminal_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SequenceConnection(_Connection):
        def __init__(self) -> None:
            super().__init__({})
            self.rows = [
                {
                    "total_count": 1,
                    "nonterminal_count": 0,
                    "published_work_item_count": 1,
                    "excluded_active_session_rule_count": 1,
                    "needs_review_count": 1,
                    "failed_count": 0,
                },
                {
                    "total_count": 1,
                    "nonterminal_count": 0,
                    "published_work_item_count": 1,
                    "excluded_active_session_rule_count": 0,
                    "needs_review_count": 0,
                    "failed_count": 0,
                },
            ]

        async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
            del query, args
            return self.rows.pop(0)

    connection = SequenceConnection()
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))
    reconcile_calls = 0

    async def reconcile(*, limit: int) -> int:
        nonlocal reconcile_calls
        assert limit == 1
        reconcile_calls += 1
        return 1

    monkeypatch.setattr(repository, "reconcile_review_publications", reconcile)

    result = await repository.await_task_terminal(
        task_id="task-1",
        minimum_total_count=1,
        timeout_seconds=1,
        poll_interval_seconds=0.001,
    )

    assert result.needs_review_count == 0
    assert reconcile_calls == 1


def test_review_reconciler_requires_same_capture_run_and_rule_snapshot() -> None:
    source = inspect.getsource(
        PostgresH5InspectionPublicationRepository.reconcile_review_publications
    )

    assert "work_item.page_capture_id = publication.page_capture_id" in source
    assert "work_item.producer_agent_run_id" in source
    assert "work_item.ruleset_snapshot_hash IS NOT DISTINCT FROM" in source
    assert "member.contract_hash = frozen_rule.contract_hash" in source
    assert "AND EXISTS (" in source
    assert "resolved_after_publication" in source


def test_publication_creation_rechecks_authoritative_h5_evidence_gate() -> None:
    source = inspect.getsource(PostgresH5InspectionPublicationRepository.create_for_capture)

    assert "qc_h5_evidence_inventory_item" in source
    assert "inventory.status = 'pass'" in source
    assert "common_capture.capture_role = 'after_action'" in source
    assert "task.task_config->>'execution_mode' = 'full_inspection'" in source
    assert "task.task_config->>'h5_inspection_notification_mode' = 'enforced'" in source
    assert "action.finalization_epoch = surface_session.finalization_epoch" in source
    assert "surface_session.finalization_epoch = task.finalization_epoch" in source
    assert "task.finalization_agent_run_id = source_run.id" in source
    assert "supplemental_evidence_source" in source
    assert "targeted_recapture" in source
    assert "NOT IN ('superseded', 'abandoned')" in source


def test_reconciler_derives_missing_publications_from_database_inventory() -> None:
    source = inspect.getsource(
        PostgresH5InspectionPublicationRepository.reconcile_missing_publications
    )

    assert "qc_h5_evidence_inventory_item" in source
    assert "qc_h5_page_type_assessment" in source
    assert "action.finalization_epoch = surface_session.finalization_epoch" in source
    assert "surface_session.finalization_epoch = task.finalization_epoch" in source
    assert "task.finalization_agent_run_id = source_run.id" in source
    assert "supplemental_evidence_source" in source
    assert "targeted_recapture" in source
    assert "publication.id IS NULL" in source
    assert "create_for_capture" in source


def test_shadow_publication_freezes_plan_without_work_or_outbox() -> None:
    source = inspect.getsource(PostgresH5InspectionPublicationRepository.record_shadow_for_capture)

    assert "'shadow', 'not_applicable'" in source
    assert "qc_h5_inspection_publication_rule" not in source
    assert "_insert_shadow_rule" in source
    assert "publish_inspection_work_item_on_connection" not in source
    assert "insert_outbox_event" not in source
    helper_source = inspect.getsource(PostgresH5InspectionPublicationRepository._insert_shadow_rule)
    assert "qc_h5_inspection_publication_rule" in helper_source


def test_reconciler_rewakes_only_h5_linked_retry_and_expired_work() -> None:
    source = inspect.getsource(
        PostgresH5InspectionPublicationRepository.reconcile_linked_work_delivery
    )

    assert "qc_h5_inspection_publication_work_item" in source
    assert "work_item.status = 'retry_wait'" in source
    assert "work_item.status IN ('leased', 'running')" in source
    assert "row_version = row_version + 1" in source
    assert "h5-work-rewake:" in source
    assert "INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE" in source


def test_publication_reconciler_advances_version_before_rewaking() -> None:
    source = inspect.getsource(PostgresH5InspectionPublicationRepository.reconcile)

    assert "qc_outbox_event" in source
    assert "qc_event_inbox" in source
    assert "aggregate_version" in source
    assert "row_version = row_version + 1" in source
    assert source.index("row_version = row_version + 1") < source.index(
        "self._insert_available_event"
    )


def test_publication_claim_uses_deterministic_logical_judge_run() -> None:
    source = inspect.getsource(PostgresH5InspectionPublicationRepository._ensure_consumer_run)

    assert "h5-inspection-consumer:" in source
    assert "'rule_judge'" in source
    assert "ON CONFLICT (id) DO UPDATE" in source
    assert "logical_identity" in source
    assert "'logical_identity', $3::text" in source
    assert "'finalization_epoch', $4::integer" in source
    assert "existing.status = 'running'" in source
    assert "existing.stop_reason = 'h5_inspection_work_drained'" in source
    assert "owner_task.target_platform = 'h5'" in source
    assert "owner_task.finalization_state <> 'sealed'" in source


def test_publication_claim_revalidates_and_locks_frozen_identity() -> None:
    create_source = inspect.getsource(PostgresH5InspectionPublicationRepository.create_for_capture)
    claim_source = inspect.getsource(PostgresH5InspectionPublicationRepository.claim)
    completion_source = inspect.getsource(
        PostgresH5InspectionPublicationRepository._validate_completion_identity
    )

    assert "FOR UPDATE OF task, ruleset, capture, common_capture, assessment" in create_source
    assert "inventory, action, surface_session" in create_source
    assert "publication.assessment_hash" in claim_source
    assert "publication.ruleset_snapshot_hash" in claim_source
    assert "common_capture.capture_role" in claim_source
    assert "frozen_identity_drift" in claim_source
    assert "source_run.status AS source_run_status" in claim_source
    assert "source_run_terminal" in claim_source or "source_run_active" in claim_source
    assert "AND source_run.status IN (" in completion_source
    assert "'done', 'failed', 'aborted', 'canceled'" in completion_source
    assert "task.finalization_agent_run_id IS NULL" in completion_source
    assert "competing_run.input_context->>'source'" in completion_source


def test_publication_completion_links_work_before_emitting_work_wakeup() -> None:
    source = inspect.getsource(PostgresH5InspectionPublicationRepository.complete)

    link_position = source.index("qc_h5_inspection_publication_work_item")
    outbox_position = source.index("INSPECTION_WORK_AVAILABLE_MESSAGE_TYPE")
    terminal_position = source.index("SET status = $4")
    assert link_position < outbox_position < terminal_position
    assert "resolved_active_session_rule_count > 0" in source
    assert (
        "if terminal_status == \"published\" and excluded_active_session_rule_count > 0"
        not in source
    )
    assert "publish_inspection_work_item_on_connection" in source
    assert "prepared_commands" in source
    assert "excluded_active_session_rules" in source
    assert "work_item.ruleset_id = publication.ruleset_id" in source
    assert "published_inspection_count" in source
    assert "aggregate_version=0" in source
    assert 'aggregate_version=int(work_item["row_version"])' not in source
    assert "_link_active_session_work_items" in source
    assert "expected_work_item_count" in source


async def test_live_active_session_work_is_linked_to_publication_membership() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            calls.append((query, args))
            return [
                {
                    "work_item_id": "work-live-1",
                    "batch_key": "live-batch",
                    "rule_id": "rule-live",
                    "rule_code": "LIVE_RULE",
                    "contract_hash": "hash-live",
                }
            ]

        async def fetchval(self, query: str, *args: object) -> str:
            calls.append((query, args))
            return "work-live-1"

    linked = await PostgresH5InspectionPublicationRepository._link_active_session_work_items(
        Connection(),
        lease=_lease(),
        excluded_rules=(
            {
                "rule_id": "rule-live",
                "rule_code": "LIVE_RULE",
                "contract_hash": "hash-live",
            },
        ),
    )

    assert linked == ("work-live-1",)
    query, args = calls[0]
    assert "session_owner_agent_run_id = $3::uuid" in query
    assert "session_dependency = 'active_session'" in query
    assert args == (
        "task-1",
        "capture-1",
        "run-1",
        "ruleset-1",
        1,
        "ruleset-hash-1",
        0,
    )
    assert "qc_h5_inspection_publication_work_item" in calls[1][0]


async def test_live_active_session_work_rejects_rule_identity_drift() -> None:
    class Connection:
        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            del query, args
            return [
                {
                    "work_item_id": "work-live-1",
                    "batch_key": "live-batch",
                    "rule_id": "rule-drift",
                    "rule_code": "DRIFT_RULE",
                    "contract_hash": "hash-drift",
                }
            ]

    with pytest.raises(RuntimeError, match="成员与收口冻结候选不一致"):
        await PostgresH5InspectionPublicationRepository._link_active_session_work_items(
            Connection(),
            lease=_lease(),
            excluded_rules=(
                {
                    "rule_id": "rule-live",
                    "rule_code": "LIVE_RULE",
                    "contract_hash": "hash-live",
                },
            ),
        )


def test_consumer_run_closes_only_after_current_epoch_chain_is_idle() -> None:
    wrapper_source = inspect.getsource(
        PostgresH5InspectionPublicationRepository._finish_consumer_run_if_idle
    )
    source = inspect.getsource(finish_h5_consumer_run_if_idle_on_connection)

    assert "publication.consumer_agent_run_id = consumer.id" not in source
    assert "qc_inspection_attempt" in source
    assert "qc_outbox_event" in source
    assert "qc_event_inbox" in source
    assert "FROM work_scope WHERE status = 'failed'" in source
    assert "outbox.status = 'dead_letter'" in source
    assert "inbox.status = 'dead_letter'" in source
    assert "task.status = 'canceled'" in source
    assert "THEN 'canceled'" in source
    assert "h5_inspection_chain_failed" in source
    assert "task.finalization_agent_run_id IS NOT NULL" in source
    assert "OR task.status <> 'running'" in source
    assert "task.finalization_state <> 'sealed'" in source
    assert "_settle_h5_page_inspection_statuses" in source
    settlement_source = inspect.getsource(_settle_h5_page_inspection_statuses)
    assert "UPDATE qc_page_capture AS capture" in settlement_source
    assert "UPDATE qc_capture AS capture" in settlement_source
    assert "finish_h5_consumer_run_if_idle_on_connection" in wrapper_source
    lifecycle_source = inspect.getsource(
        PostgresH5InspectionPublicationRepository.reconcile_consumer_run_lifecycle
    )
    reconcile_source = inspect.getsource(PostgresH5InspectionPublicationRepository.reconcile)
    assert "_finish_consumer_run_if_idle" in lifecycle_source
    assert "INNER JOIN qc_task AS task" in lifecycle_source
    assert "task.finalization_state <> 'sealed'" in lifecycle_source
    assert "reconcile_consumer_run_lifecycle" in reconcile_source


async def test_consumer_run_reconciler_acquires_task_lock_before_run_rows() -> None:
    """三 Worker 对账不得先锁 Run 行，再等待任务收口锁。"""

    class Connection:
        def __init__(self) -> None:
            self.fetch_query = ""
            self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
            self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

        def transaction(self) -> _Transaction:
            return _Transaction()

        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            del args
            self.fetch_query = query
            return [
                {"consumer_agent_run_id": "run-busy", "task_id": "task-busy"},
                {"consumer_agent_run_id": "run-ready", "task_id": "task-ready"},
            ]

        async def fetchval(self, query: str, *args: object) -> object:
            self.fetchval_calls.append((query, args))
            if "pg_try_advisory_xact_lock" in query:
                return args == ("task-finalization:task-ready",)
            assert args == ("task-ready", "run-ready")
            return "run-ready"

        async def execute(self, query: str, *args: object) -> str:
            self.execute_calls.append((query, args))
            return "UPDATE 1"

    connection = Connection()
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))

    closed_count = await repository.reconcile_consumer_run_lifecycle(limit=10)

    assert closed_count == 1
    assert "FOR UPDATE" not in connection.fetch_query
    assert "ORDER BY consumer.created_at, consumer.id" in connection.fetch_query
    assert len(connection.fetchval_calls) == 3
    assert "pg_try_advisory_xact_lock" in connection.fetchval_calls[0][0]
    assert "pg_try_advisory_xact_lock" in connection.fetchval_calls[1][0]
    assert "UPDATE qc_agent_run" in connection.fetchval_calls[2][0]
    assert len(connection.execute_calls) == 3
    assert "UPDATE qc_page_capture AS capture" in connection.execute_calls[0][0]
    assert "UPDATE qc_capture AS capture" in connection.execute_calls[1][0]
    assert "SET finalization_retry_at = now()" in connection.execute_calls[2][0]
    assert connection.execute_calls[2][1] == ("task-ready", "run-ready")


def test_mixed_rule_batch_projects_one_review_status_to_both_capture_models() -> None:
    source = inspect.getsource(
        PostgresH5InspectionPublicationRepository._annotate_capture_in_transaction
    )

    assert '"active_session_rules_excluded" if excluded_count > 0' in source
    assert '"status": projected_business_status' in source
    assert '"inspection_status": projected_business_status' in source


async def test_completed_live_session_rule_is_not_left_as_publication_gap() -> None:
    """活会话规则已有同身份可信终态时，正式 Publication 不得再次转复核。"""
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            calls.append((query, args))
            return [
                {
                    "rule_id": "rule-live",
                    "rule_code": "LIVE_RULE",
                    "contract_hash": "hash-live",
                }
            ]

    excluded = (
        {
            "rule_id": "rule-live",
            "rule_code": "LIVE_RULE",
            "contract_hash": "hash-live",
        },
        {
            "rule_id": "rule-missing",
            "rule_code": "MISSING_RULE",
            "contract_hash": "hash-missing",
        },
    )

    unresolved = await PostgresH5InspectionPublicationRepository._unresolved_active_session_rules(
        Connection(),
        lease=_lease(),
        excluded_rules=excluded,
    )

    assert unresolved == (excluded[1],)
    query, args = calls[0]
    assert "session_dependency = 'active_session'" in query
    assert "'written', 'evidence_insufficient', 'error', 'skipped'" in query
    assert args == (
        "task-1",
        "capture-1",
        "run-1",
        "ruleset-1",
        1,
        "ruleset-hash-1",
        0,
    )


async def test_web_publication_waits_while_active_session_work_is_not_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """捕获 Publication 先于 capture.committed 消费时被永久转复核的回归。"""

    connection = _PublicationCompletionConnection(live_work_exists=False)
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))
    await _stub_publication_completion(
        monkeypatch,
        repository,
        target_platform="web",
        session_status="active",
    )

    with pytest.raises(RuntimeError, match="active-session Work 尚未创建") as caught:
        await repository.complete(lease=_lease(), result=_active_session_rule_result())

    assert getattr(caught.value, "preserve_attempt_budget", False) is True
    assert not any(
        "UPDATE qc_h5_inspection_publication" in query for query, _args in connection.fetchval_calls
    )


async def test_web_publication_links_live_work_after_capture_stream_catches_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """live Work 一旦存在，Publication 应绑定后发布，不等待其判定终态。"""

    connection = _PublicationCompletionConnection(live_work_exists=True)
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))
    await _stub_publication_completion(
        monkeypatch,
        repository,
        target_platform="web",
        session_status="active",
    )

    await repository.complete(lease=_lease(), result=_active_session_rule_result())

    publication_update = next(
        args
        for query, args in connection.fetchval_calls
        if "UPDATE qc_h5_inspection_publication" in query
    )
    assert publication_update[3] == "published"
    assert publication_update[6:8] == (1, 0)


async def test_web_publication_closure_adjudicates_missing_live_work_as_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有 Session 终态屏障后，缺失的 live Work 才成为正式覆盖缺口。"""

    connection = _PublicationCompletionConnection(live_work_exists=False)
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))
    await _stub_publication_completion(
        monkeypatch,
        repository,
        target_platform="web",
        session_status="closed",
    )

    await repository.complete(lease=_lease(), result=_active_session_rule_result())

    publication_update = next(
        args
        for query, args in connection.fetchval_calls
        if "UPDATE qc_h5_inspection_publication" in query
    )
    assert publication_update[3] == "needs_review"
    assert publication_update[6:8] == (0, 1)


async def test_h5_publication_keeps_existing_active_session_gap_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Web 的跨流等待不得扩大到 H5 任务。"""

    connection = _PublicationCompletionConnection(live_work_exists=False)
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))
    await _stub_publication_completion(
        monkeypatch,
        repository,
        target_platform="h5",
        session_status="active",
    )

    await repository.complete(lease=_lease(), result=_active_session_rule_result())

    publication_update = next(
        args
        for query, args in connection.fetchval_calls
        if "UPDATE qc_h5_inspection_publication" in query
    )
    assert publication_update[3] == "needs_review"
    assert publication_update[6:8] == (0, 1)


async def test_web_active_session_wait_does_not_exhaust_publication_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预期跨流等待可无限重放，不能耗尽普通故障的有限重试预算。"""

    class RetryConnection(_Connection):
        def __init__(self) -> None:
            super().__init__({})
            self.rows = [
                {
                    "task_id": "task-1",
                    "finalization_epoch": 0,
                    "attempt_count": 3,
                    "max_attempts": 3,
                },
                {"row_version": 2},
            ]
            self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []

        async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
            self.fetchrow_calls.append((query, args))
            return self.rows.pop(0)

    connection = RetryConnection()
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))

    async def insert_available(*args: object, **kwargs: object) -> str:
        del args, kwargs
        return "event-2"

    async def finish(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return True

    monkeypatch.setattr(repository, "_insert_available_event", insert_available)
    monkeypatch.setattr(repository, "_finish_consumer_run_if_idle", finish)
    pending = WebActiveSessionWorkPendingError("Web active-session Work 尚未创建")

    await repository.retry(lease=_lease(), error=pending)

    retry_query, retry_args = connection.fetchrow_calls[1]
    assert retry_args[3] is False
    assert "GREATEST(attempt_count - 1, 0)" in retry_query


async def test_off_coverage_reader_counts_only_unresolved_live_rules() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
            calls.append((query, args))
            return [
                {
                    "rule_id": "rule-live",
                    "rule_code": "LIVE_RULE",
                    "contract_hash": "hash-live",
                }
            ]

    class Acquire:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *args: object) -> None:
            return None

    class Pool:
        def acquire(self) -> Acquire:
            return Acquire()

    unresolved_count = await PostgresH5InspectionPublicationRepository(
        pool=Pool()
    ).count_unresolved_active_session_rules_for_capture(
        task_id="task-1",
        source_agent_run_id="run-1",
        capture_id="capture-1",
        excluded_rules=(
            {
                "rule_id": "rule-live",
                "rule_code": "LIVE_RULE",
                "contract_hash": "hash-live",
            },
            {
                "rule_id": "rule-missing",
                "rule_code": "MISSING_RULE",
                "contract_hash": "hash-missing",
            },
        ),
    )

    assert unresolved_count == 1
    query, args = calls[0]
    assert "COALESCE(" in query
    assert "task.task_config->>'execution_mode', 'full_inspection'" in query
    assert "h5_inspection_notification_mode', 'off'" in query
    assert "session_owner_agent_run_id = $2::uuid" in query
    assert "'written', 'evidence_insufficient', 'error', 'skipped'" in query
    assert args == ("task-1", "run-1", "capture-1")


async def test_handler_publishes_snapshot_work_and_completes_publication() -> None:
    lease = _lease()
    repository = _Repository(
        H5InspectionPublicationClaimResult(
            disposition=H5InspectionPublicationClaimDisposition.CLAIMED,
            lease=lease,
        )
    )
    publisher = _Publisher(
        _PublishResult(
            status="published",
            published_count=1,
            items=({"work_item_id": "work-1", "batch_key": "batch-1"},),
        )
    )
    handler = H5InspectionPublicationWakeupHandler(
        repository=repository,
        publisher=publisher,
        lease_owner="worker-1",
        lease_seconds=60,
    )

    assert await handler.handle(_wakeup()) is True
    assert publisher.calls == [
        {
            "task_id": "task-1",
            "capture_id": "capture-1",
            "visual_page_type": "home",
            "page_state": "stable",
            "agent_run_id": "run-1",
            "snapshot_only": True,
            "prepare_only": True,
        }
    ]
    assert repository.completed == [(lease, publisher.result)]
    assert repository.retried == []


async def test_handler_retries_publication_and_keeps_stream_entry_pending() -> None:
    lease = _lease()
    repository = _Repository(
        H5InspectionPublicationClaimResult(
            disposition=H5InspectionPublicationClaimDisposition.CLAIMED,
            lease=lease,
        )
    )
    publisher = _Publisher(error=RuntimeError("temporary"))
    handler = H5InspectionPublicationWakeupHandler(
        repository=repository,
        publisher=publisher,
        lease_owner="worker-1",
        lease_seconds=60,
    )

    with pytest.raises(RuntimeError, match="temporary"):
        await handler.handle(_wakeup())

    assert repository.completed == []
    assert len(repository.retried) == 1


async def test_repository_supersedes_nonterminal_publication_for_failed_task() -> None:
    connection = _Connection(
        {
            "publication_id": "publication-1",
            "task_id": "task-1",
            "consumer_agent_run_id": None,
            "status": "pending",
            "row_version": 1,
            "task_status": "failed",
            "publication_finalization_epoch": 0,
            "task_finalization_epoch": 0,
        }
    )
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))

    result = await repository.claim(
        publication_id="publication-1",
        task_id="task-1",
        message_version=1,
        lease_owner="worker-1",
        lease_seconds=60,
    )

    assert result.disposition is H5InspectionPublicationClaimDisposition.COMPLETED
    assert len(connection.execute_calls) == 2
    assert "pg_advisory_xact_lock" in connection.execute_calls[0][0]
    query, args = connection.execute_calls[1]
    assert "status = 'superseded'" in query
    assert "task_not_active" in query
    assert args == ("publication-1",)


async def test_repository_claim_accepts_asyncpg_json_text_task_config() -> None:
    """asyncpg 默认返回 JSON 文本时仍应按任务冻结配置领取 Publication。"""

    identity_row: dict[str, object] = {
        "publication_id": "publication-1",
        "task_id": "task-1",
        "source_agent_run_id": "run-1",
        "consumer_agent_run_id": None,
        "page_capture_id": "capture-1",
        "page_type_assessment_id": "assessment-1",
        "assessment_hash": "assessment-hash-1",
        "publication_ruleset_id": "ruleset-1",
        "publication_ruleset_version": 1,
        "publication_ruleset_snapshot_hash": "ruleset-hash-1",
        "status": "pending",
        "attempt_count": 0,
        "max_attempts": 3,
        "row_version": 1,
        "next_attempt_at": None,
        "lease_expires_at": None,
        "publication_finalization_epoch": 0,
        "task_status": "running",
        "task_finalization_state": "open",
        "task_finalization_epoch": 0,
        "task_finalization_agent_run_id": "run-1",
        "source_run_status": "running",
        "source_run_is_unique_active": True,
        "target_platform": "h5",
        "task_config": json.dumps(
            {
                "execution_mode": "full_inspection",
                "h5_inspection_notification_mode": "enforced",
            }
        ),
        "task_ruleset_id": "ruleset-1",
        "current_ruleset_version": 1,
        "current_ruleset_snapshot_hash": "ruleset-hash-1",
        "page_type": "home",
        "page_state": "stable",
        "inspection_lifecycle_status": "active",
        "capture_metadata": json.dumps({"pipeline_metrics": {"page_inspection_ready": True}}),
        "capture_role": "after_action",
        "capture_status": "captured",
        "capture_agent_run_id": "run-1",
        "assessment_record_id": "assessment-1",
        "current_assessment_hash": "assessment-hash-1",
        "detection_status": "recognized",
        "assessment_page_type": "home",
        "inventory_item_id": "inventory-1",
        "action_finalization_epoch": 0,
        "session_finalization_epoch": 0,
    }

    class SequenceConnection(_Connection):
        def __init__(self) -> None:
            super().__init__(identity_row)
            self.rows = [
                identity_row,
                {"attempt_count": 1, "max_attempts": 3, "row_version": 1},
            ]

        async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
            return self.rows.pop(0)

    class ClaimableRepository(PostgresH5InspectionPublicationRepository):
        async def _ensure_consumer_run(self, *args: object, **kwargs: object) -> str:
            return "judge-run-1"

    connection = SequenceConnection()
    repository = ClaimableRepository(pool=_Pool(connection))

    result = await repository.claim(
        publication_id="publication-1",
        task_id="task-1",
        message_version=1,
        lease_owner="worker-1",
        lease_seconds=60,
    )

    assert result.disposition is H5InspectionPublicationClaimDisposition.CLAIMED
    assert result.lease is not None
    assert result.lease.consumer_agent_run_id == "judge-run-1"
    assert all("frozen_identity_drift" not in query for query, _ in connection.execute_calls)


@pytest.mark.parametrize(
    "platform, consumer_error",
    [
        ("h5", None),
        ("h5", H5InspectionConsumerRunIdentityError),
        ("h5", ConnectionError),
        ("web", H5InspectionConsumerRunIdentityError),
    ],
)
@pytest.mark.parametrize("readiness", ["ready", "loading", "missing", "dynamic"])
@pytest.mark.parametrize("active_lease", [False, True])
async def test_repository_claim_accepts_unique_active_source_before_finalization_owner(
    platform: str, consumer_error: type[Exception] | None, readiness: str, active_lease: bool,
) -> None:
    """Publication 早于 draining 时允许唯一活动来源 Run，避免合法消息被误判漂移。"""

    identity_row = {
        "publication_id": "publication-1",
        "task_id": "task-1",
        "source_agent_run_id": "run-1",
        "consumer_agent_run_id": None,
        "page_capture_id": "capture-1",
        "page_type_assessment_id": "assessment-1",
        "assessment_hash": "assessment-hash-1",
        "publication_ruleset_id": "ruleset-1",
        "publication_ruleset_version": 1,
        "publication_ruleset_snapshot_hash": "ruleset-hash-1",
        "status": "pending",
        "attempt_count": 0,
        "max_attempts": 3,
        "row_version": 1,
        "next_attempt_at": None,
        "lease_expires_at": None,
        "publication_finalization_epoch": 0,
        "task_status": "running",
        "task_finalization_state": "open",
        "task_finalization_epoch": 0,
        "task_finalization_agent_run_id": None,
        "source_run_status": "running",
        "source_run_is_unique_active": True,
        "target_platform": platform,
        "task_config": {
            "execution_mode": "full_inspection",
            "h5_inspection_notification_mode": "enforced",
        },
        "task_ruleset_id": "ruleset-1",
        "current_ruleset_version": 1,
        "current_ruleset_snapshot_hash": "ruleset-hash-1",
        "page_type": "home",
        "page_state": "stable",
        "inspection_lifecycle_status": "active",
        "capture_metadata": {"pipeline_metrics": {
            "page_inspection_ready": readiness in {"ready", "dynamic"},
            "page_settle_complete": readiness == "ready",
        }} if readiness != "missing" else {},
        "capture_role": "after_action",
        "capture_status": "captured",
        "capture_agent_run_id": "run-1",
        "assessment_record_id": "assessment-1",
        "current_assessment_hash": "assessment-hash-1",
        "detection_status": "recognized",
        "assessment_page_type": "home",
        "inventory_item_id": "inventory-1",
        "action_finalization_epoch": 0,
        "session_finalization_epoch": 0,
    }

    class SequenceConnection(_Connection):
        def __init__(self) -> None:
            super().__init__(identity_row)
            self.rows = [
                identity_row,
                {"attempt_count": 1, "max_attempts": 3, "row_version": 1},
            ]

        async def fetchrow(self, query: str, *args: object) -> dict[str, object]:
            return self.rows.pop(0)

    class ClaimableRepository(PostgresH5InspectionPublicationRepository):
        async def _ensure_consumer_run(self, *args: object, **kwargs: object) -> str:
            if consumer_error is not None:
                raise consumer_error("逻辑消费 Run 不可继续")
            return "judge-run-1"

    connection = SequenceConnection()
    if active_lease:
        from datetime import UTC, datetime, timedelta

        identity_row["status"] = "leased"
        identity_row["lease_expires_at"] = datetime.now(UTC) + timedelta(seconds=30)
    repository = ClaimableRepository(pool=_Pool(connection))

    async def claim():
        return await repository.claim(
            publication_id="publication-1",
            task_id="task-1",
            message_version=1,
            lease_owner="worker-1",
            lease_seconds=60,
        )

    if active_lease:
        result = await claim()
        assert result.disposition is H5InspectionPublicationClaimDisposition.BUSY
        assert all("pg_advisory_xact_lock" in sql for sql, _ in connection.execute_calls)
        assert len(connection.rows) == 1
        return
    if platform == "h5" and readiness in {"loading", "missing"}:
        result = await claim()
        assert result.disposition is H5InspectionPublicationClaimDisposition.COMPLETED
        assert result.lease is None
        assert any("capture_not_inspection_ready" in args for sql, args in connection.execute_calls)
        assert len(connection.rows) == 1
        return
    if consumer_error is not None:
        if platform != "h5" or consumer_error is ConnectionError:
            with pytest.raises(consumer_error):
                await claim()
            assert not any("consumer_run_unavailable" in sql for sql, _ in connection.execute_calls)
            return
        result = await claim()
        assert result.disposition is H5InspectionPublicationClaimDisposition.COMPLETED
        assert result.lease is None
        assert any("consumer_run_unavailable" in sql and "needs_review" in sql
                   for sql, _ in connection.execute_calls)
        assert len(connection.rows) == 1
        return

    result = await claim()

    assert result.disposition is H5InspectionPublicationClaimDisposition.CLAIMED
    assert result.lease is not None


@pytest.mark.parametrize(
    "disposition, expected",
    [
        (H5InspectionPublicationClaimDisposition.COMPLETED, True),
        (H5InspectionPublicationClaimDisposition.STALE, True),
        (H5InspectionPublicationClaimDisposition.BUSY, False),
    ],
)
async def test_handler_ack_policy_follows_database_disposition(
    disposition: H5InspectionPublicationClaimDisposition,
    expected: bool,
) -> None:
    repository = _Repository(H5InspectionPublicationClaimResult(disposition=disposition))
    publisher = _Publisher()
    handler = H5InspectionPublicationWakeupHandler(
        repository=repository,
        publisher=publisher,
        lease_owner="worker-1",
        lease_seconds=60,
    )

    assert await handler.handle(_wakeup()) is expected
    assert publisher.calls == []


async def test_h5_reconciliation_settles_terminal_requests_without_another_judge(monkeypatch):
    from unittest.mock import AsyncMock

    from services.api.app.infrastructure.db import h5_inspection_publication_repository

    connection = _Connection({})
    connection.fetch = AsyncMock(return_value=[{"task_id": "h5-task"}])
    settlement = AsyncMock()
    settlement.reopen_waiting_evidence.return_value = 0
    monkeypatch.setattr(
        h5_inspection_publication_repository, "PostgresInspectionWorkItemRepository",
        lambda **kwargs: settlement,
    )
    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(connection))

    assert await repository.reconcile_waiting_evidence(limit=5) == 0
    settlement.reopen_waiting_evidence.assert_awaited_once_with(task_id="h5-task")
    sql, limit = connection.fetch.await_args.args
    assert limit == 5
    assert "task.target_platform = 'h5'" in sql
    assert "qc_h5_inspection_publication_work_item" in sql
    assert "work_item.finalization_epoch = task.finalization_epoch" in sql
    assert "NOT EXISTS" in sql
    assert "'pending', 'routed', 'running'" in sql


async def test_h5_reconciliation_recovers_waiting_rules_before_consumer_finalization(monkeypatch):
    from unittest.mock import AsyncMock

    repository = PostgresH5InspectionPublicationRepository(pool=_Pool(_Connection({})))
    order = []

    async def expired(**kwargs):
        order.append("expired")
        return 0

    async def waiting(**kwargs):
        order.append("waiting")
        return 0

    async def missing(**kwargs):
        order.append("missing")
        return kwargs["limit"]

    monkeypatch.setattr(repository, "reconcile_expired_evidence", AsyncMock(side_effect=expired))
    monkeypatch.setattr(repository, "reconcile_waiting_evidence", AsyncMock(side_effect=waiting))
    monkeypatch.setattr(
        repository, "reconcile_missing_publications", AsyncMock(side_effect=missing)
    )
    assert await repository.reconcile(limit=5) == 5
    assert order == ["expired", "waiting", "missing"]
