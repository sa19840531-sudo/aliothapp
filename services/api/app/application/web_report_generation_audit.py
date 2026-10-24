"""Web V2 正式报告生成 attempt 的持久化审计边界。"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebReportGenerationAttempt:
    """一次已持久化的 Web 正式报告生成尝试。"""

    attempt_id: str
    task_id: str
    agent_run_id: str
    finalization_epoch: int
    attempt_number: int


class WebReportGenerationAttemptLedger(Protocol):
    async def start_attempt(
        self,
        *,
        attempt_id: str,
        task_id: str,
        agent_run_id: str,
    ) -> WebReportGenerationAttempt:
        """在真正调用报告生成器前提交 running 事实。"""

    async def finish_attempt(
        self,
        *,
        attempt_id: str,
        attempt_number: int,
        status: str,
        failure_code: str | None,
    ) -> None:
        """幂等提交 attempt 终态。"""


class FinalReportGenerator(Protocol):
    async def generate_task_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        finalized_at: datetime | None = None,
        terminal_task_status: str | None = None,
        termination_reason: str | None = None,
        page_exploration: dict[str, Any] | None = None,
    ) -> dict:
        """生成任务报告。"""


class AuditedWebFinalReportGenerator:
    """只为 Web 正式报告记录真实调用起止，预览报告保持原语义。"""

    def __init__(
        self,
        *,
        delegate: FinalReportGenerator,
        ledger: WebReportGenerationAttemptLedger,
    ) -> None:
        self._delegate = delegate
        self._ledger = ledger

    async def generate_task_report(
        self,
        task_id: str,
        *,
        agent_run_id: str | None = None,
        finalized_at: datetime | None = None,
        terminal_task_status: str | None = None,
        termination_reason: str | None = None,
        page_exploration: dict[str, Any] | None = None,
    ) -> dict:
        kwargs = {
            "agent_run_id": agent_run_id,
            "finalized_at": finalized_at,
            "terminal_task_status": terminal_task_status,
            "termination_reason": termination_reason,
            "page_exploration": page_exploration,
        }
        if finalized_at is None:
            return await self._delegate.generate_task_report(task_id, **kwargs)
        if agent_run_id is None:
            raise RuntimeError("Web 正式报告生成审计缺少 agent_run_id")

        attempt = await self._ledger.start_attempt(
            attempt_id=str(uuid.uuid4()),
            task_id=task_id,
            agent_run_id=agent_run_id,
        )
        try:
            report = await self._delegate.generate_task_report(task_id, **kwargs)
        except asyncio.CancelledError:
            await self._finish_without_masking_error(
                attempt,
                status="cancelled",
                failure_code="CancelledError",
            )
            raise
        except Exception as exc:
            await self._finish_without_masking_error(
                attempt,
                status="failed",
                failure_code=type(exc).__name__,
            )
            raise
        await self._finish(attempt, status="succeeded", failure_code=None)
        return report

    async def _finish(
        self,
        attempt: WebReportGenerationAttempt,
        *,
        status: str,
        failure_code: str | None,
    ) -> None:
        await self._ledger.finish_attempt(
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            status=status,
            failure_code=failure_code,
        )

    async def _finish_without_masking_error(
        self,
        attempt: WebReportGenerationAttempt,
        *,
        status: str,
        failure_code: str,
    ) -> None:
        try:
            await self._finish(
                attempt,
                status=status,
                failure_code=failure_code,
            )
        except Exception:  # noqa: BLE001 - 审计写失败不能覆盖报告生成原始异常
            logger.exception(
                "task_id=%s Web 正式报告生成 attempt 终态写入失败",
                attempt.task_id,
            )
