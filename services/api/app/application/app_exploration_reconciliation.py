"""App 探索封存结果与技术失败事实的独立对账。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from services.api.app.application.app_exploration_evidence import (
    AppEvidenceIndexRepository,
)
from services.api.app.application.app_exploration_failures import (
    AppExplorationFailureRepository,
)
from services.api.app.application.app_exploration_results import (
    AppExplorationResultRepository,
)
from services.api.app.application.app_metrics import (
    APP_TECHNICAL_FAILURE_TOTAL,
    record_app_metric_safely,
)
from services.api.app.application.surface_sessions import SurfaceSessionRepository
from services.api.app.domain.evidence import (
    AppEvidenceIndexStatus,
    AppExplorationFailureFact,
    AppExplorationFailureRecoveryState,
    AppExplorationResult,
    app_exploration_result_context_sha256,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    require_aware_datetime,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AppExplorationResultIntent:
    """Evidence Index 封存前保存的最小 Result 恢复上下文。"""

    task_id: str
    agent_run_id: str
    surface_session_id: str
    result_context_sha256: str
    coverage_summary: Mapping[str, Any]
    started_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "surface_session_id",
            "result_context_sha256",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
        try:
            require_aware_datetime(self.started_at, "started_at")
            require_aware_datetime(self.created_at, "created_at")
            coverage = freeze_json_mapping(self.coverage_summary, "coverage_summary")
        except ProtocolContractError as exc:
            raise ValueError(str(exc)) from exc
        expected = app_exploration_result_context_sha256(
            coverage_summary=coverage,
            started_at=self.started_at,
        )
        if self.result_context_sha256 != expected:
            raise ValueError("result_context_sha256 与 Result Intent 不一致")
        object.__setattr__(self, "coverage_summary", coverage)


@dataclass(frozen=True, kw_only=True)
class PersistedAppExplorationResultIntent:
    """Result Intent 幂等写入结果。"""

    intent: AppExplorationResultIntent
    created: bool


class AppExplorationResultIntentRepository(Protocol):
    """封存前 Result 恢复上下文的不可变持久化端口。"""

    async def record(
        self,
        *,
        intent: AppExplorationResultIntent,
    ) -> PersistedAppExplorationResultIntent: ...

    async def get_by_run_id(
        self,
        *,
        run_id: str,
    ) -> AppExplorationResultIntent | None: ...

    async def list_orphaned(
        self,
        *,
        limit: int = 100,
    ) -> tuple[AppExplorationResultIntent, ...]: ...


@dataclass(frozen=True, kw_only=True)
class AppExplorationReconciliationSummary:
    """单轮对账结果，便于 Worker 记录与测试。"""

    repaired_results: int = 0
    reconciled_failures: int = 0
    pending_facts: int = 0
    error_count: int = 0


class AppExplorationReconciler:
    """从 PostgreSQL 权威事实修复孤儿 Result，并收口失败审计状态。"""

    def __init__(
        self,
        *,
        failures: AppExplorationFailureRepository,
        result_intents: AppExplorationResultIntentRepository,
        evidence_indexes: AppEvidenceIndexRepository,
        exploration_results: AppExplorationResultRepository,
        sessions: SurfaceSessionRepository,
        reconciliation_grace_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            isinstance(reconciliation_grace_seconds, bool)
            or not isinstance(reconciliation_grace_seconds, int)
            or reconciliation_grace_seconds < 0
        ):
            raise ValueError("reconciliation_grace_seconds 必须为非负整数")
        self._failures = failures
        self._result_intents = result_intents
        self._evidence_indexes = evidence_indexes
        self._exploration_results = exploration_results
        self._sessions = sessions
        self._grace = timedelta(seconds=reconciliation_grace_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    async def reconcile_once(self, *, limit: int = 100) -> AppExplorationReconciliationSummary:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit 必须在 1 到 1000 之间")
        repaired_results = 0
        reconciled_failures = 0
        error_count = 0

        for intent in await self._result_intents.list_orphaned(limit=limit):
            try:
                if await self._repair_result(intent) is not None:
                    repaired_results += 1
            except Exception:  # noqa: BLE001 - 单个坏事实不能阻断整轮对账
                error_count += 1
                logger.exception(
                    "App 探索孤儿 Result 对账失败",
                    extra={"agent_run_id": intent.agent_run_id},
                )

        pending = await self._failures.list_pending(limit=limit)
        for failure in pending:
            try:
                if await self._reconcile_failure(failure):
                    reconciled_failures += 1
            except Exception:  # noqa: BLE001 - 并发恢复冲突留待下一轮
                error_count += 1
                logger.exception(
                    "App 探索技术失败事实对账失败",
                    extra={
                        "failure_id": failure.failure_id,
                        "agent_run_id": failure.agent_run_id,
                    },
                )
        return AppExplorationReconciliationSummary(
            repaired_results=repaired_results,
            reconciled_failures=reconciled_failures,
            pending_facts=max(0, len(pending) - reconciled_failures),
            error_count=error_count,
        )

    async def _repair_result(
        self,
        intent: AppExplorationResultIntent,
    ) -> AppExplorationResult | None:
        existing = await self._exploration_results.get_by_run_id(
            run_id=intent.agent_run_id
        )
        if existing is not None:
            return existing
        index = await self._evidence_indexes.get_by_run_id(run_id=intent.agent_run_id)
        if index is None or index.status is not AppEvidenceIndexStatus.SEALED:
            return None
        if (
            index.task_id != intent.task_id
            or index.run_id != intent.agent_run_id
            or index.surface_session_id != intent.surface_session_id
            or index.result_context_sha256 != intent.result_context_sha256
        ):
            raise ValueError("Result Intent 与封存 Evidence Index 身份或上下文不一致")
        if (
            index.outcome is None
            or index.terminal_reason_code is None
            or index.content_sha256 is None
            or index.sealed_at is None
        ):
            raise ValueError("sealed Evidence Index 缺少 Result 所需字段")
        result = AppExplorationResult(
            task_id=intent.task_id,
            run_id=intent.agent_run_id,
            surface_session_id=intent.surface_session_id,
            outcome=index.outcome,
            terminal_reason_code=index.terminal_reason_code,
            evidence_index_id=index.index_id,
            evidence_index_hash=index.content_sha256,
            coverage_summary=intent.coverage_summary,
            evidence_gaps=index.evidence_gaps,
            started_at=intent.started_at,
            completed_at=index.sealed_at,
        )
        return (await self._exploration_results.create(result=result)).result

    async def _reconcile_failure(self, failure: AppExplorationFailureFact) -> bool:
        index = await self._evidence_indexes.get_by_run_id(run_id=failure.agent_run_id)
        result = await self._exploration_results.get_by_run_id(run_id=failure.agent_run_id)
        if result is None and index is not None and index.status is AppEvidenceIndexStatus.SEALED:
            intent = await self._result_intents.get_by_run_id(run_id=failure.agent_run_id)
            if intent is not None:
                result = await self._repair_result(intent)
        session = await self._sessions.get(session_id=failure.surface_session_id)
        session_status = session.status.value if session is not None else None

        if result is not None:
            if (
                index is None
                or index.status is not AppEvidenceIndexStatus.SEALED
                or result.evidence_index_id != index.index_id
                or result.evidence_index_hash != index.content_sha256
            ):
                raise ValueError("App Exploration Result 与封存 Index 不一致")
            await self._mark_failure(
                failure,
                recovery_state=AppExplorationFailureRecoveryState.EVIDENCE_SEALED,
                sealed_index_id=index.index_id,
                result_id=result.run_id,
                session_status=session_status,
            )
            return True

        old_enough = failure.observed_at + self._grace <= self._now()
        if index is not None and index.status is AppEvidenceIndexStatus.SEALED and old_enough:
            await self._mark_failure(
                failure,
                recovery_state=AppExplorationFailureRecoveryState.EVIDENCE_SEALED,
                sealed_index_id=index.index_id,
                session_status=session_status,
            )
            return True
        if old_enough and (session is None or session.status.is_terminal):
            await self._mark_failure(
                failure,
                recovery_state=AppExplorationFailureRecoveryState.NO_EVIDENCE,
                session_status=session_status,
            )
            return True
        return False

    async def _mark_failure(
        self,
        failure: AppExplorationFailureFact,
        *,
        recovery_state: AppExplorationFailureRecoveryState,
        sealed_index_id: str | None = None,
        result_id: str | None = None,
        session_status: str | None = None,
    ) -> None:
        await self._failures.mark_reconciled(
            failure_id=failure.failure_id,
            failure_sha256=failure.failure_sha256,
            recovery_state=recovery_state,
            reconciled_at=self._now(),
            sealed_index_id=sealed_index_id,
            result_id=result_id,
            session_status=session_status,
        )
        record_app_metric_safely(
            APP_TECHNICAL_FAILURE_TOTAL,
            phase=failure.phase,
            reason_code=failure.reason_code,
            recovered="true",
        )
