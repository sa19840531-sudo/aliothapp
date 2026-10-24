"""App Publication 稳定身份、整份 Index 处理与 ACK 语义测试。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from services.api.app.agents.runtime.app_inspection_publication_wakeup import (
    AppInspectionPublicationWakeupHandler,
)
from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationClaimDisposition,
    AppInspectionPublicationClaimResult,
    AppInspectionPublicationEntryLease,
    AppInspectionPublicationLease,
)
from services.api.app.domain.inspection.app_publication import (
    APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
    build_app_inspection_publication_available,
    build_app_inspection_publication_id,
)
from services.api.app.infrastructure.db.app_consumer_run_lifecycle import (
    finish_app_consumer_run_if_idle_on_connection,
)
from services.api.app.infrastructure.db.app_inspection_publication_repository import (
    PostgresAppInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.task_insight_repository import (
    _app_publication_projection,
)
from services.api.app.infrastructure.queue.redis_stream import RedisStreamWakeup


def _lease() -> AppInspectionPublicationLease:
    return AppInspectionPublicationLease(
        publication_id="9f8b416e-026c-49ab-963e-23f4ed4aa40e",
        task_id="1fe06d7a-2653-43e8-b20d-7a366192119f",
        source_agent_run_id="cce2845b-e699-4475-920d-e4220be72232",
        evidence_index_id="index-1",
        evidence_index_hash="sha256:" + "a" * 64,
        ruleset_id="93e03b52-405d-4b06-8680-363153880371",
        ruleset_version=2,
        ruleset_snapshot_hash="sha256:" + "b" * 64,
        finalization_epoch=3,
        lease_token="17852fe4-1280-45f4-a098-09d8a9c97024",
        row_version=4,
        attempt_count=1,
        max_attempts=3,
        entries=(
            AppInspectionPublicationEntryLease(
                entry_id=1,
                sequence_no=1,
                capture_id="7dbdc875-fce4-4a72-95e7-5dc1b967e8bd",
                page_type="home",
                page_state="stable",
            ),
            AppInspectionPublicationEntryLease(
                entry_id=2,
                sequence_no=2,
                capture_id="ea83b125-4c01-4a10-bf57-79c07ecc4d0f",
                page_type=None,
                page_state="stable",
            ),
        ),
    )


def _wakeup() -> RedisStreamWakeup:
    lease = _lease()
    return RedisStreamWakeup(
        stream_name="alioth:v2:wakeup:app-inspection-publication",
        stream_entry_id="1-0",
        message_id="message-1",
        message_type=APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE,
        task_id=lease.task_id,
        aggregate_type="app_inspection_publication",
        aggregate_id=lease.publication_id,
        aggregate_version=lease.row_version,
        idempotency_key="input:4",
    )


class RecordingRepository:
    def __init__(self, disposition=AppInspectionPublicationClaimDisposition.CLAIMED) -> None:
        self.disposition = disposition
        self.claim_calls: list[dict] = []
        self.complete_calls: list[dict] = []
        self.retry_calls: list[dict] = []

    async def claim(self, **kwargs):
        self.claim_calls.append(kwargs)
        return AppInspectionPublicationClaimResult(
            disposition=self.disposition,
            lease=_lease()
            if self.disposition is AppInspectionPublicationClaimDisposition.CLAIMED
            else None,
        )

    async def complete(self, **kwargs):
        self.complete_calls.append(kwargs)

    async def retry(self, **kwargs):
        self.retry_calls.append(kwargs)


class RecordingPlanner:
    def __init__(self, *, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls: list[dict] = []

    async def publish_for_capture(self, **kwargs):
        self.calls.append(kwargs)
        if kwargs["capture_id"] == self.fail_on:
            raise RuntimeError("planner failed")
        return SimpleNamespace(
            status=("published" if kwargs["visual_page_type"] else "unmatched_page_type"),
            page_capture_id=kwargs["capture_id"],
            published_count=0,
            prepared_commands=(),
            excluded_active_session_rule_count=0,
            excluded_active_session_rules=(),
        )


async def test_handler_plans_all_entries_before_one_atomic_complete() -> None:
    repository = RecordingRepository()
    planner = RecordingPlanner()
    handler = AppInspectionPublicationWakeupHandler(
        repository=repository,
        publisher=planner,
        lease_owner="worker:app-publication",
        lease_seconds=60,
    )

    assert await handler.handle(_wakeup()) is True

    assert [call["capture_id"] for call in planner.calls] == [
        item.capture_id for item in _lease().entries
    ]
    assert all(call["snapshot_only"] is True for call in planner.calls)
    assert all(call["prepare_only"] is True for call in planner.calls)
    assert len(repository.complete_calls) == 1
    assert len(repository.complete_calls[0]["results"]) == 2
    assert repository.retry_calls == []


async def test_busy_is_not_acked_and_terminal_replay_is_acked() -> None:
    planner = RecordingPlanner()
    busy = AppInspectionPublicationWakeupHandler(
        repository=RecordingRepository(AppInspectionPublicationClaimDisposition.BUSY),
        publisher=planner,
        lease_owner="worker",
        lease_seconds=60,
    )
    completed = AppInspectionPublicationWakeupHandler(
        repository=RecordingRepository(AppInspectionPublicationClaimDisposition.COMPLETED),
        publisher=planner,
        lease_owner="worker",
        lease_seconds=60,
    )

    assert await busy.handle(_wakeup()) is False
    assert await completed.handle(_wakeup()) is True
    assert planner.calls == []


async def test_planning_failure_is_persisted_before_message_retries() -> None:
    repository = RecordingRepository()
    planner = RecordingPlanner(fail_on=_lease().entries[-1].capture_id)
    handler = AppInspectionPublicationWakeupHandler(
        repository=repository,
        publisher=planner,
        lease_owner="worker",
        lease_seconds=60,
    )

    with pytest.raises(RuntimeError, match="planner failed"):
        await handler.handle(_wakeup())

    assert repository.complete_calls == []
    assert len(repository.retry_calls) == 1
    assert repository.retry_calls[0]["lease"] == _lease()


def test_publication_identity_and_event_are_deterministic() -> None:
    kwargs = {
        "task_id": _lease().task_id,
        "finalization_epoch": 3,
        "evidence_index_id": "index-1",
        "evidence_index_hash": "sha256:" + "a" * 64,
        "ruleset_snapshot_hash": "sha256:" + "b" * 64,
    }
    publication_id = build_app_inspection_publication_id(**kwargs)
    assert publication_id == build_app_inspection_publication_id(**kwargs)

    envelope = build_app_inspection_publication_available(
        publication_id=publication_id,
        task_id=_lease().task_id,
        evidence_index_id="index-1",
        evidence_index_hash="sha256:" + "a" * 64,
        finalization_epoch=3,
        row_version=4,
        input_hash="sha256:" + "c" * 64,
        occurred_at=datetime(2026, 8, 18, tzinfo=UTC),
    )
    assert envelope.aggregate_type == "app_inspection_publication"
    assert envelope.aggregate_version == 4
    assert envelope.message_type == APP_INSPECTION_PUBLICATION_AVAILABLE_MESSAGE_TYPE
    assert envelope.payload["evidence_index_id"] == "index-1"


def test_repository_freezes_index_ruleset_and_all_entry_results_transactionally() -> None:
    create_source = inspect.getsource(PostgresAppInspectionPublicationRepository.create_for_index)
    complete_source = inspect.getsource(PostgresAppInspectionPublicationRepository.complete)
    retry_source = inspect.getsource(PostgresAppInspectionPublicationRepository.retry)
    reconcile_source = inspect.getsource(PostgresAppInspectionPublicationRepository.reconcile)

    assert "task-finalization:" in create_source
    assert "index.status = 'sealed'" in create_source
    assert "surface_session.status IN ('closed', 'lost', 'failed')" in create_source
    assert "ruleset.snapshot_hash" in create_source
    assert "qc_app_inspection_publication_entry" in create_source
    assert "_insert_available_event" in create_source
    assert 'str(row["status"]) == "pending"' in create_source

    assert "zip(lease.entries, results, strict=True)" in complete_source
    assert "publish_inspection_work_item_on_connection" in complete_source
    assert "_load_resolved_active_session_rules" in complete_source
    assert "app_active_session_rule_completed" in complete_source
    assert "page_type_unavailable" in complete_source
    assert "active_session_rules_unresolved" in complete_source
    assert "active_session_or_page_type_unavailable" not in complete_source
    assert "expected_rule_count" in complete_source
    assert "lease_token" in complete_source and "row_version" in complete_source
    assert "lease_expires_at > now()" in complete_source
    assert "actual_work_count" in complete_source
    assert "actual_rule_count" in complete_source
    assert "_ensure_consumer_run" in complete_source
    assert "connection.transaction()" in complete_source
    assert "SET status = $3::varchar(32)" in complete_source
    assert "failure_code = $7" in complete_source
    assert "SET status = $5::varchar(32)" in complete_source
    assert "CASE WHEN $5::varchar(32) = 'needs_review'" in complete_source
    # 范围外注解可以同事务写回，证据身份仍由封存门禁保护。
    assert "UPDATE qc_evidence_manifest" not in complete_source
    assert "UPDATE qc_app_evidence_index " not in complete_source
    assert "SET capture_role" not in complete_source
    assert "SET captured_at" not in complete_source
    assert "SET surface_session_id" not in complete_source

    assert "retry_wait" in retry_source
    assert "attempts_exhausted" in retry_source
    assert "不能提前发布尚不可领取的消息" in retry_source
    assert "lease_expires_at <= now()" in reconcile_source
    assert "FOR UPDATE SKIP LOCKED" in reconcile_source
    assert "_insert_available_event" in reconcile_source
    assert "_recover_completed_wakeup_work" in reconcile_source
    assert "_reconcile_consumer_run_lifecycle" in reconcile_source


def test_app_publication_consumer_is_deterministic_and_recovery_is_one_shot() -> None:
    ensure_source = inspect.getsource(
        PostgresAppInspectionPublicationRepository._ensure_consumer_run
    )
    recovery_source = inspect.getsource(
        PostgresAppInspectionPublicationRepository._recover_completed_wakeup_work
    )
    lifecycle_source = inspect.getsource(finish_app_consumer_run_if_idle_on_connection)

    assert "app-inspection-consumer:v2:" in ensure_source
    assert "uuid5" in ensure_source
    assert "'source', 'app_inspection_publication'" in ensure_source
    assert "work_item.row_version = 0" in recovery_source
    assert "work_item.consumer_agent_run_id IS NULL" in recovery_source
    assert "inbox.status = 'completed'" in recovery_source
    assert "app-consumer-recovery:" in recovery_source
    assert "qc_app_inspection_publication_work_item" in lifecycle_source
    assert "app_inspection_work_drained" in lifecycle_source
    reconcile_lifecycle_source = inspect.getsource(
        PostgresAppInspectionPublicationRepository._reconcile_consumer_run_lifecycle
    )
    assert "finish_app_consumer_run_if_idle_on_connection" in reconcile_lifecycle_source
    assert "FOR UPDATE OF consumer SKIP LOCKED" in reconcile_lifecycle_source


async def test_resolved_active_session_rules_require_terminal_member_identity() -> None:
    calls: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        async def fetch(self, query: str, *args: object):
            calls.append((query, args))
            return [
                {
                    "work_item_id": "11111111-1111-1111-1111-111111111111",
                    "batch_key": "active:interaction",
                    "rule_id": "22222222-2222-2222-2222-222222222222",
                    "rule_code": "APP_LIVE_RULE",
                    "contract_hash": "hash-live",
                },
                {
                    "work_item_id": "33333333-3333-3333-3333-333333333333",
                    "batch_key": "stale:batch",
                    "rule_id": "44444444-4444-4444-4444-444444444444",
                    "rule_code": "STALE_RULE",
                    "contract_hash": "hash-stale",
                },
            ]

    lease = _lease()
    resolved = await PostgresAppInspectionPublicationRepository(
        pool=object()
    )._load_resolved_active_session_rules(
        Connection(),
        lease=lease,
        entry=lease.entries[0],
        excluded_rules=(
            {
                "rule_id": "22222222-2222-2222-2222-222222222222",
                "rule_code": "APP_LIVE_RULE",
                "contract_hash": "hash-live",
            },
        ),
    )

    assert resolved == {
        (
            "22222222-2222-2222-2222-222222222222",
            "APP_LIVE_RULE",
            "hash-live",
        ): {
            "work_item_id": "11111111-1111-1111-1111-111111111111",
            "batch_key": "active:interaction",
        }
    }
    query, args = calls[0]
    assert "work_item.session_dependency = 'active_session'" in query
    assert "work_item.status IN ('succeeded', 'partial')" in query
    assert "'written', 'evidence_insufficient', 'error', 'skipped'" in query
    assert args == (
        lease.task_id,
        lease.entries[0].capture_id,
        lease.source_agent_run_id,
        lease.ruleset_id,
        lease.ruleset_version,
        lease.ruleset_snapshot_hash,
        lease.finalization_epoch,
    )


def test_insight_projects_multi_entry_delivery_claim_and_judgement() -> None:
    publication = {
        "publication_id": "publication-1",
        "status": "needs_review",
        "expected_entry_count": 2,
        "terminal_entry_count": 2,
        "linked_work_item_count": 1,
        "terminal_work_count": 1,
        "claimed_work_count": 1,
        "outbox_published_count": 1,
        "inbox_count": 1,
        "outbox_pending_count": 0,
        "outbox_dead_letter_count": 0,
        "inbox_pending_count": 0,
        "inbox_dead_letter_count": 0,
    }
    entries = [
        {
            "publication_id": "publication-1",
            "entry_id": 1,
            "status": "linked",
            "linked_work_item_count": 1,
            "claimed_work_count": 1,
            "terminal_work_count": 1,
        },
        {
            "publication_id": "publication-1",
            "entry_id": 2,
            "status": "needs_review",
            "linked_work_item_count": 0,
            "claimed_work_count": 0,
            "terminal_work_count": 0,
        },
    ]

    projection = _app_publication_projection(
        publication_rows=[publication],
        entry_rows=entries,
    )

    assert projection["current_stage"] == "judged"
    assert projection["stage_counts"] == {
        "sealed": 0,
        "delivered": 0,
        "claimed": 0,
        "judged": 1,
    }
    assert [item["current_stage"] for item in projection["items"][0]["entries"]] == [
        "judged",
        "judged",
    ]
    assert projection["needs_review_count"] == 1
