"""App 探索技术失败事实与 Runtime 审计出口测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from services.api.app.application import app_metrics
from services.api.app.application.app_exploration_failures import (
    PersistedAppExplorationFailure,
)
from services.api.app.application.app_exploration_reconciliation import (
    AppExplorationReconciler,
    AppExplorationResultIntent,
    PersistedAppExplorationResultIntent,
)
from services.api.app.application.app_exploration_results import (
    PersistedAppExplorationResult,
)
from services.api.app.application.app_runtime_composition import (
    _record_technical_failure,
)
from services.api.app.domain.evidence import (
    AppExplorationFailureRecoveryState,
    AppExplorationResultOutcome,
    app_exploration_result_context_sha256,
    build_app_evidence_index,
    build_app_exploration_failure_fact,
    seal_app_evidence_index,
)
from services.api.app.infrastructure.db.app_exploration_failure_repository import _json_dumps
from services.api.app.infrastructure.db.app_exploration_reconciliation_repository import (
    PostgresAppExplorationResultIntentRepository,
)
from services.api.app.infrastructure.db.app_exploration_reconciliation_repository import (
    _json_dumps as _result_intent_json_dumps,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunRecord


def _build_fact(*, observed_at: datetime):
    return build_app_exploration_failure_fact(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        surface_session_id="app:11111111-1111-1111-1111-111111111111",
        phase="exploration",
        reason_code="VISION_PROVIDER_UNAVAILABLE",
        error_type="TimeoutError",
        retryable=True,
        observed_at=observed_at,
        metadata={"source": "test"},
    )


def test_failure_hash_is_stable_across_idempotent_wall_clock_retry() -> None:
    first = _build_fact(observed_at=datetime(2026, 8, 14, 8, tzinfo=UTC))
    replay = _build_fact(observed_at=first.observed_at + timedelta(seconds=30))

    assert replay.failure_id == first.failure_id
    assert replay.failure_sha256 == first.failure_sha256
    assert replay.observed_at != first.observed_at


def test_failure_fact_rejects_tampered_identity_hash() -> None:
    fact = _build_fact(observed_at=datetime(2026, 8, 14, 8, tzinfo=UTC))

    with pytest.raises(ValueError, match="failure_sha256"):
        replace(fact, reason_code="DIFFERENT_REASON")


def test_failure_metadata_serializes_deeply_frozen_mappingproxy() -> None:
    fact = build_app_exploration_failure_fact(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        surface_session_id="app:11111111-1111-1111-1111-111111111111",
        phase="exploration",
        reason_code="APP_EXPLORATION_TURN_FAILED",
        error_type="TypeError",
        retryable=False,
        observed_at=datetime(2026, 8, 14, 8, tzinfo=UTC),
        metadata={
            "source": "test",
            "nested": {"items": [{"enabled": True}, {"count": 2}]},
        },
    )

    serialized = _json_dumps(fact.metadata)

    assert serialized == '{"nested":{"items":[{"enabled":true},{"count":2}]},"source":"test"}'


def test_result_intent_coverage_serializes_deeply_frozen_mappingproxy() -> None:
    intent = AppExplorationResultIntent(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        surface_session_id="app:11111111-1111-1111-1111-111111111111",
        result_context_sha256=app_exploration_result_context_sha256(
            coverage_summary={"nested": {"items": [{"enabled": True}]}},
            started_at=datetime(2026, 8, 14, 8, tzinfo=UTC),
        ),
        coverage_summary={"nested": {"items": [{"enabled": True}]}},
        started_at=datetime(2026, 8, 14, 8, tzinfo=UTC),
        created_at=datetime(2026, 8, 14, 8, 1, tzinfo=UTC),
    )

    serialized = _result_intent_json_dumps(intent.coverage_summary)

    assert serialized == '{"nested":{"items":[{"enabled":true}]}}'


class _IntentTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _IntentAcquire:
    def __init__(self, connection: _IntentConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _IntentConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _IntentConnection:
    def __init__(self, intent: AppExplorationResultIntent) -> None:
        self._intent = intent
        self.coverage_payload: dict[str, Any] | None = None

    def transaction(self) -> _IntentTransaction:
        return _IntentTransaction()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any]:
        assert "INSERT INTO qc_app_exploration_result_intent" in query
        self.coverage_payload = json.loads(args[4])
        return {
            "task_id": self._intent.task_id,
            "agent_run_id": self._intent.agent_run_id,
            "surface_session_id": self._intent.surface_session_id,
            "result_context_sha256": self._intent.result_context_sha256,
            "coverage_summary": self.coverage_payload,
            "started_at": self._intent.started_at,
            "created_at": self._intent.created_at,
            "created": True,
        }


class _IntentPool:
    def __init__(self, connection: _IntentConnection) -> None:
        self._connection = connection

    def acquire(self) -> _IntentAcquire:
        return _IntentAcquire(self._connection)


@pytest.mark.asyncio
async def test_result_intent_repository_accepts_frozen_coverage_summary() -> None:
    now = datetime(2026, 8, 14, 8, tzinfo=UTC)
    intent = AppExplorationResultIntent(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        surface_session_id="app:11111111-1111-1111-1111-111111111111",
        result_context_sha256=app_exploration_result_context_sha256(
            coverage_summary={"nested": {"items": [{"enabled": True}]}},
            started_at=now,
        ),
        coverage_summary={"nested": {"items": [{"enabled": True}]}},
        started_at=now,
        created_at=now + timedelta(minutes=1),
    )
    connection = _IntentConnection(intent)
    repository = PostgresAppExplorationResultIntentRepository(
        pool=_IntentPool(connection),
    )

    persisted = await repository.record(intent=intent)

    assert persisted.created is True
    assert connection.coverage_payload == {"nested": {"items": [{"enabled": True}]}}


@pytest.mark.asyncio
async def test_runtime_failure_exit_records_durable_fact_and_metric() -> None:
    captured = []

    class Runtime:
        async def record_exploration_failure(self, *, failure):
            captured.append(failure)

    record = AgentRunRecord(
        run_id="22222222-2222-2222-2222-222222222222",
        task_id="11111111-1111-1111-1111-111111111111",
        message="test",
        submitted_at=datetime(2026, 8, 14, 8, tzinfo=UTC),
    )
    with app_metrics._LOCK:
        app_metrics._COUNTERS.clear()

    await _record_technical_failure(
        Runtime(),  # type: ignore[arg-type]
        record=record,
        session_id="app:11111111-1111-1111-1111-111111111111",
        phase="runtime",
        reason_code="TimeoutError",
        error_type="TimeoutError",
        retryable=True,
        observed_at=datetime(2026, 8, 14, 8, tzinfo=UTC),
    )

    assert len(captured) == 1
    assert captured[0].agent_run_id == record.run_id
    assert app_metrics.app_metric_value(app_metrics.APP_TECHNICAL_FAILURE_TOTAL) == 1


@pytest.mark.asyncio
async def test_runtime_failure_exit_does_not_hide_original_when_audit_store_fails() -> None:
    class Runtime:
        async def record_exploration_failure(self, *, failure):
            del failure
            raise ConnectionError("postgres unavailable")

    record = AgentRunRecord(
        run_id="22222222-2222-2222-2222-222222222222",
        task_id="11111111-1111-1111-1111-111111111111",
        message="test",
        submitted_at=datetime(2026, 8, 14, 8, tzinfo=UTC),
    )

    await _record_technical_failure(
        Runtime(),  # type: ignore[arg-type]
        record=record,
        session_id="app:11111111-1111-1111-1111-111111111111",
        phase="runtime",
        reason_code="ConnectionError",
        error_type="ConnectionError",
        retryable=True,
        observed_at=datetime(2026, 8, 14, 8, tzinfo=UTC),
    )


class MemoryResultIntents:
    def __init__(self, intents=()):
        self.values = {item.agent_run_id: item for item in intents}

    async def record(self, *, intent):
        existing = self.values.setdefault(intent.agent_run_id, intent)
        return PersistedAppExplorationResultIntent(
            intent=existing,
            created=existing is intent,
        )

    async def get_by_run_id(self, *, run_id):
        return self.values.get(run_id)

    async def list_orphaned(self, *, limit=100):
        return tuple(self.values.values())[:limit]


class MemoryIndexes:
    def __init__(self, index=None):
        self.index = index

    async def get_by_run_id(self, *, run_id):
        if self.index is not None and self.index.run_id == run_id:
            return self.index
        return None


class MemoryResults:
    def __init__(self):
        self.result = None

    async def get_by_run_id(self, *, run_id):
        if self.result is not None and self.result.run_id == run_id:
            return self.result
        return None

    async def create(self, *, result):
        self.result = self.result or result
        return PersistedAppExplorationResult(result=self.result, created=self.result is result)


class MemoryFailures:
    def __init__(self, failures=()):
        self.values = {item.failure_id: item for item in failures}

    async def record(self, *, failure):
        existing = self.values.setdefault(failure.failure_id, failure)
        return PersistedAppExplorationFailure(failure=existing, created=existing is failure)

    async def get_by_run_id(self, *, run_id):
        return next(
            (item for item in self.values.values() if item.agent_run_id == run_id),
            None,
        )

    async def list_pending(self, *, limit=100):
        return tuple(
            item
            for item in self.values.values()
            if item.recovery_state is AppExplorationFailureRecoveryState.PENDING
        )[:limit]

    async def mark_reconciled(
        self,
        *,
        failure_id,
        failure_sha256,
        recovery_state,
        reconciled_at,
        sealed_index_id=None,
        result_id=None,
        session_status=None,
    ):
        current = self.values[failure_id]
        assert current.failure_sha256 == failure_sha256
        updated = replace(
            current,
            recovery_state=recovery_state,
            reconciled_at=reconciled_at,
            sealed_index_id=sealed_index_id,
            result_id=result_id,
            session_status=session_status,
        )
        self.values[failure_id] = updated
        return updated


class EmptySessions:
    async def get(self, *, session_id):
        del session_id
        return None


def _intent(now: datetime) -> AppExplorationResultIntent:
    coverage = {"visited_pages": 3, "required_pages": 2}
    started_at = now - timedelta(minutes=10)
    return AppExplorationResultIntent(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        surface_session_id="app:11111111-1111-1111-1111-111111111111",
        result_context_sha256=app_exploration_result_context_sha256(
            coverage_summary=coverage,
            started_at=started_at,
        ),
        coverage_summary=coverage,
        started_at=started_at,
        created_at=now - timedelta(minutes=1),
    )


def _sealed_index(intent: AppExplorationResultIntent, now: datetime):
    building = build_app_evidence_index(
        task_id=intent.task_id,
        run_id=intent.agent_run_id,
        build_id="app-build-1",
        surface_session_id=intent.surface_session_id,
        entries=(),
        created_at=now - timedelta(minutes=1),
        result_context_sha256=intent.result_context_sha256,
    )
    return seal_app_evidence_index(
        building,
        outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
        terminal_reason_code="minimum_coverage_not_met",
        sealed_at=now,
    )


def test_result_intent_rejects_tampered_context() -> None:
    intent = _intent(datetime(2026, 8, 14, 9, tzinfo=UTC))

    with pytest.raises(ValueError, match="result_context_sha256"):
        replace(intent, coverage_summary={"visited_pages": 99})


@pytest.mark.asyncio
async def test_reconciler_repairs_only_sealed_index_with_matching_result_intent() -> None:
    now = datetime(2026, 8, 14, 9, tzinfo=UTC)
    intent = _intent(now)
    results = MemoryResults()
    reconciler = AppExplorationReconciler(
        failures=MemoryFailures(),
        result_intents=MemoryResultIntents((intent,)),
        evidence_indexes=MemoryIndexes(_sealed_index(intent, now)),
        exploration_results=results,
        sessions=EmptySessions(),
        now=lambda: now,
    )

    summary = await reconciler.reconcile_once()

    assert summary.repaired_results == 1
    assert summary.error_count == 0
    assert results.result is not None
    assert results.result.coverage_summary == intent.coverage_summary
    assert results.result.started_at == intent.started_at


@pytest.mark.asyncio
async def test_reconciler_marks_old_terminal_failure_without_index_as_no_evidence() -> None:
    now = datetime(2026, 8, 14, 9, tzinfo=UTC)
    failure = _build_fact(observed_at=now - timedelta(minutes=10))
    failures = MemoryFailures((failure,))
    reconciler = AppExplorationReconciler(
        failures=failures,
        result_intents=MemoryResultIntents(),
        evidence_indexes=MemoryIndexes(),
        exploration_results=MemoryResults(),
        sessions=EmptySessions(),
        now=lambda: now,
    )

    summary = await reconciler.reconcile_once()

    reconciled = failures.values[failure.failure_id]
    assert summary.reconciled_failures == 1
    assert reconciled.recovery_state is AppExplorationFailureRecoveryState.NO_EVIDENCE
    assert reconciled.sealed_index_id is None
    assert reconciled.result_id is None


def test_failure_migration_keeps_result_intent_before_seal_and_safe_downgrade() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations/versions/20260814_0059_app_exploration_failure_fact.py"
    )
    source = migration.read_text(encoding="utf-8")

    assert 'down_revision = "20260814_0058"' in source
    assert "CREATE TABLE qc_app_exploration_result_intent" in source
    assert "guard_qc_app_result_intent_immutable" in source
    assert "CREATE TABLE qc_app_exploration_failure" in source
    assert "OR EXISTS (SELECT 1 FROM qc_app_exploration_result_intent)" in source
