"""E16/E17：Web 离场门禁的灰度装配、影子模式与预留过期清扫。

冻结依据：
- E16 验收：Web 生产路径使用持久化门禁；证据不全时离场动作被真实拒绝
- E17 验收：只拦离场类动作；观察类动作不受影响；预留可消费、取消或超时回收

三档模式见 `core/config.WebEvidenceExitGateSettings`：

- `off`：空门禁，与改造前一致；
- `shadow`：真实评估并持久化门禁事实与预留，但**阻断结论不生效**——
  被拒的动作照常执行，只在日志留痕，用于上线前核对拒绝率；
- `on`：阻断结论生效，证据不全的离场动作被真实拒绝。

影子模式不是跳过评估，而是照常评估、照常落库，只吞掉阻断异常——
这样拒绝面能真实对账，动作却不被拦下。

预留生命周期：reserve/finalize 由 CaptureCoordinator 在命令栈内完成；
`expire_stale` 此前全仓无调用方，超时预留只能被动堆积，
`EvidenceExitReservationSweeper` 补上周期回收。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from services.api.app.application.evidence_exit_gate import (
    EvidenceExitGate,
    EvidenceExitGateBlockedError,
)
from services.api.app.core.config import WebEvidenceExitGateSettings, get_settings
from services.api.app.domain.exploration import (
    EvidenceExitGateDecision,
    EvidenceExitReservationStatus,
)
from services.api.app.domain.exploration.web_coverage_snapshot import (
    WebCoverageSnapshot,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebCoverageExitGateDecision:
    """Web 最终 Coverage 对报告出口的纯事实判定。"""

    allow_report: bool
    execution_status: str | None
    reason_codes: tuple[str, ...]


def evaluate_web_coverage_exit_gate(
    *,
    exploration_terminal: bool,
    pending_capture_commit_count: int,
    snapshot: WebCoverageSnapshot | None,
    untrusted_formal_capture_count: int = 0,
) -> WebCoverageExitGateDecision:
    """区分探索中的暂缺、可报告 Gap 与基础证据失败。"""

    if not isinstance(exploration_terminal, bool):
        raise ValueError("exploration_terminal 必须为布尔值")
    if (
        isinstance(pending_capture_commit_count, bool)
        or not isinstance(pending_capture_commit_count, int)
        or pending_capture_commit_count < 0
    ):
        raise ValueError("pending_capture_commit_count 必须为非负整数")
    if (
        isinstance(untrusted_formal_capture_count, bool)
        or not isinstance(untrusted_formal_capture_count, int)
        or untrusted_formal_capture_count < 0
    ):
        raise ValueError("untrusted_formal_capture_count 必须为非负整数")

    reasons: list[str] = []
    if not exploration_terminal:
        reasons.append("WEB_EXPLORATION_NOT_TERMINAL")
    if pending_capture_commit_count:
        reasons.append("WEB_CAPTURE_COMMIT_PENDING")
    if untrusted_formal_capture_count:
        reasons.append("WEB_UNTRUSTED_FORMAL_EVIDENCE")
    if snapshot is None:
        reasons.append("WEB_COVERAGE_SNAPSHOT_MISSING")
        return WebCoverageExitGateDecision(
            allow_report=False,
            execution_status=None,
            reason_codes=tuple(reasons),
        )
    if snapshot.coverage_status == "failed":
        reasons.append("WEB_FOUNDATIONAL_EVIDENCE_FAILED")
        return WebCoverageExitGateDecision(
            allow_report=False,
            execution_status="failed",
            reason_codes=tuple(reasons),
        )
    if reasons:
        return WebCoverageExitGateDecision(
            allow_report=False,
            execution_status=None,
            reason_codes=tuple(reasons),
        )
    if snapshot.coverage_status == "completed_with_gaps":
        return WebCoverageExitGateDecision(
            allow_report=True,
            execution_status="completed_with_gaps",
            reason_codes=("WEB_COVERAGE_GAP",),
        )
    return WebCoverageExitGateDecision(
        allow_report=True,
        execution_status="completed",
        reason_codes=(),
    )


class NoopEvidenceExitGate:
    """`off` 模式：不评估、不预留，保持改造前行为。"""

    async def reserve(self, *, intent, permit):  # noqa: ANN001
        del intent, permit
        return None

    async def reserve_action_recovery(
        self,
        *,
        intent,  # noqa: ANN001
        permit,  # noqa: ANN001
        action_execution_id: str,
        before_capture_id: str | None,
    ):
        del intent, permit, action_execution_id, before_capture_id
        return None

    async def finalize_stale_action_reservations(
        self,
        *,
        intent,  # noqa: ANN001
        permit,  # noqa: ANN001
        action_execution_id: str,
        status: EvidenceExitReservationStatus,
    ) -> int:
        del intent, permit, action_execution_id, status
        return 0

    async def finalize(self, *, decision, status) -> None:  # noqa: ANN001
        del decision, status

    async def expire_stale(self, *, limit: int = 100) -> int:
        del limit
        return 0


class ShadowEvidenceExitGate:
    """`shadow` 模式：照常评估与预留，但吞掉阻断并留痕。

    阻断由 `PostgresEvidenceExitGate.reserve` 以
    `EvidenceExitGateBlockedError` 抛出，此时门禁评估、业务结果与预留
    **已经落库**；影子期只是不让这个异常传到编排层，
    因此拒绝面可以照常对账，动作却不被真的拦下。
    """

    def __init__(self, inner: EvidenceExitGate) -> None:
        self._inner = inner
        self.blocked_decisions: list[EvidenceExitGateDecision] = []

    async def reserve(self, *, intent, permit):  # noqa: ANN001
        try:
            return await self._inner.reserve(intent=intent, permit=permit)
        except EvidenceExitGateBlockedError as exc:
            self._observe(exc)
            return None

    async def reserve_action_recovery(
        self,
        *,
        intent,  # noqa: ANN001
        permit,  # noqa: ANN001
        action_execution_id: str,
        before_capture_id: str | None,
    ):
        try:
            return await self._inner.reserve_action_recovery(
                intent=intent,
                permit=permit,
                action_execution_id=action_execution_id,
                before_capture_id=before_capture_id,
            )
        except EvidenceExitGateBlockedError as exc:
            self._observe(exc)
            return None

    async def finalize_stale_action_reservations(
        self,
        *,
        intent,  # noqa: ANN001
        permit,  # noqa: ANN001
        action_execution_id: str,
        status: EvidenceExitReservationStatus,
    ) -> int:
        return await self._inner.finalize_stale_action_reservations(
            intent=intent,
            permit=permit,
            action_execution_id=action_execution_id,
            status=status,
        )

    async def finalize(self, *, decision, status) -> None:  # noqa: ANN001
        await self._inner.finalize(decision=decision, status=status)

    async def expire_stale(self, *, limit: int = 100) -> int:
        return await self._inner.expire_stale(limit=limit)

    def _observe(self, error: EvidenceExitGateBlockedError) -> None:
        decision = error.decision
        self.blocked_decisions.append(decision)
        logger.warning(
            "web_evidence_exit_gate_shadow_block task_id=%s session_id=%s "
            "capture_id=%s reasons=%s",
            decision.snapshot.task_id,
            decision.snapshot.session_id,
            decision.snapshot.source_capture_id,
            [item.value for item in decision.block_reasons],
        )


def build_web_evidence_exit_gate(
    *,
    settings: WebEvidenceExitGateSettings | None = None,
    postgres_gate_factory: Any | None = None,
) -> Any:
    """按灰度模式装配 Web 离场门禁。"""
    resolved = settings or get_settings().web_evidence_exit_gate
    if resolved.mode == "off":
        return NoopEvidenceExitGate()
    if postgres_gate_factory is None:
        from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
            PostgresEvidenceExitGate,
        )

        inner = PostgresEvidenceExitGate(
            reservation_seconds=resolved.reservation_seconds
        )
    else:
        inner = postgres_gate_factory(resolved)
    if resolved.mode == "shadow":
        return ShadowEvidenceExitGate(inner)
    return inner


class EvidenceExitReservationSweeper:
    """E17：周期回收超时未终结的离场预留。

    `expire_stale` 在此之前全仓无调用方，崩溃或超时遗留的预留只能被动发现，
    并持续占用「预留未终结」这项离场阻断条件。
    """

    def __init__(
        self,
        *,
        gate: EvidenceExitGate,
        interval_seconds: float = 60.0,
        limit: int = 100,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds 必须为正数")
        if limit <= 0:
            raise ValueError("limit 必须为正整数")
        self._gate = gate
        self._interval_seconds = interval_seconds
        self._limit = limit
        self._task: asyncio.Task[None] | None = None

    async def sweep_once(self) -> int:
        """执行一次回收；失败只告警，不打断周期任务。"""
        try:
            return await self._gate.expire_stale(limit=self._limit)
        except Exception as exc:  # noqa: BLE001 - 清扫失败不得终止 Worker
            logger.warning("离场预留过期回收失败：%s", exc, exc_info=True)
            return 0

    def start(self) -> None:
        """在当前事件循环上启动周期清扫。"""
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            expired = await self.sweep_once()
            if expired:
                logger.info("离场预留过期回收 %s 条", expired)
