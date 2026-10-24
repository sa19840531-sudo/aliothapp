"""App 活会话规则的增量发布与关闭前排空。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol

from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
    select_stable_publication_batch,
)

logger = logging.getLogger(__name__)

# 与 Worker 收口门禁保持一致：无进展宽限 + 绝对上限，避免 120s 绝对窗内批量规则跑不完。
DEFAULT_ACTIVE_SESSION_DRAIN_GRACE_SECONDS = 120.0
DEFAULT_ACTIVE_SESSION_DRAIN_MAX_WAIT_SECONDS = 900.0
DEFAULT_CURRENT_SURFACE_CHECKPOINT_SECONDS = 120.0


@dataclass(frozen=True, kw_only=True)
class AppLiveInspectionCapture:
    """仍需检查 active-session 候选规则的 App Capture。"""

    capture_id: str
    page_type: str | None
    page_state: str


@dataclass(frozen=True, kw_only=True)
class AppActiveSessionDrainState:
    """关闭前 App 活会话规则的数据库排空事实。"""

    pending_capture_count: int
    nonterminal_work_item_count: int
    next_attempt_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in ("pending_capture_count", "nonterminal_work_item_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.next_attempt_at is not None and self.next_attempt_at.utcoffset() is None:
            raise ValueError("next_attempt_at 必须带时区")

    @property
    def drained(self) -> bool:
        return self.pending_capture_count == 0 and self.nonterminal_work_item_count == 0


@dataclass(frozen=True, kw_only=True)
class AppActiveSessionInspectionSummary:
    """关闭前活会话规则发布与消费摘要。"""

    published_work_item_count: int
    drain_round_count: int
    # 排空窗口用尽后按证据不足结算的活会话工作项数；0 表示正常排空。
    settled_work_item_count: int = 0


@dataclass(frozen=True, kw_only=True)
class AppActiveSessionInspectionRoundResult:
    """一次发布、消费后的权威排空状态。"""

    published_work_item_count: int
    state: AppActiveSessionDrainState


class AppLiveInspectionCaptureReader(Protocol):
    """读取 App 活会话增量发布及排空事实。"""

    async def list_pending_captures(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> tuple[AppLiveInspectionCapture, ...]: ...

    async def load_drain_state(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> AppActiveSessionDrainState: ...


class AppCaptureInspectionPublisherPort(Protocol):
    """按 Capture 发布 active-session 规则工作项。"""

    async def publish_for_capture(self, **kwargs: Any) -> object: ...


class AppInspectionWorkConsumerPort(Protocol):
    """同步消费当前 App Run 持有的活会话规则工作项。"""

    async def drain_available(self, **kwargs: Any) -> object: ...


class AppLiveInspectionPublishService:
    """在 App Session 存活期间增量发布 active-session 规则。"""

    def __init__(
        self,
        *,
        capture_reader: AppLiveInspectionCaptureReader,
        publisher: AppCaptureInspectionPublisherPort,
    ) -> None:
        self._capture_reader = capture_reader
        self._publisher = publisher

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: None = None,
    ) -> int:
        if limit is not None:
            raise ValueError("有界发布必须调用 publish_pending_batch")
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        captures = await self._capture_reader.list_pending_captures(
            task_id=task,
            agent_run_id=run_id,
        )
        return await self._publish_captures(task=task, run_id=run_id, captures=captures)

    async def publish_pending_batch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
        cursor: str | None,
    ) -> LiveInspectionPublicationBatchResult:
        """按稳定 Capture cursor 尝试一批；失败与幂等命中都推进扫描。"""

        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        captures = await self._capture_reader.list_pending_captures(
            task_id=task,
            agent_run_id=run_id,
        )
        batch = select_stable_publication_batch(
            captures,
            identity=lambda capture: capture.capture_id,
            limit=limit,
            cursor=cursor,
        )
        created = await self._publish_captures(
            task=task,
            run_id=run_id,
            captures=batch.items,
        )
        return LiveInspectionPublicationBatchResult(
            attempted_count=len(batch.items),
            created_count=created,
            has_more=batch.has_more,
            next_cursor=batch.next_cursor,
        )

    async def _publish_captures(
        self,
        *,
        task: str,
        run_id: str,
        captures: tuple[AppLiveInspectionCapture, ...],
    ) -> int:
        published_count = 0
        for capture in captures:
            try:
                result = await self._publisher.publish_for_capture(
                    task_id=task,
                    capture_id=capture.capture_id,
                    visual_page_type=capture.page_type,
                    page_state=capture.page_state,
                    agent_run_id=run_id,
                    active_session_only=True,
                )
                items = tuple(getattr(result, "items", ()) or ())
                if items:
                    published_count += sum(
                        bool(item.get("created")) for item in items if isinstance(item, dict)
                    )
                else:
                    published_count += int(getattr(result, "published_count", 0) or 0)
            except Exception:  # noqa: BLE001 - 后续唤醒和关闭门禁会重试并最终失败关闭
                logger.exception(
                    "task_id=%s capture_id=%s App 活会话规则增量发布失败。",
                    task,
                    capture.capture_id,
                )
        return published_count


class AppActiveSessionInspectionDrainService:
    """在封存并关闭 Session 前发布、判定并排空 active-session 工作项。"""

    def __init__(
        self,
        *,
        publisher: AppLiveInspectionPublishService,
        consumer: AppInspectionWorkConsumerPort,
        state_reader: AppLiveInspectionCaptureReader,
        grace_seconds: float = DEFAULT_ACTIVE_SESSION_DRAIN_GRACE_SECONDS,
        max_wait_seconds: float = DEFAULT_ACTIVE_SESSION_DRAIN_MAX_WAIT_SECONDS,
        max_poll_seconds: float = 5.0,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        closure_settlement: Any | None = None,
    ) -> None:
        if grace_seconds <= 0 or max_wait_seconds <= 0 or max_poll_seconds <= 0:
            raise ValueError("App 活会话排空宽限、绝对上限与轮询间隔必须大于 0")
        self._closure_settlement = closure_settlement
        self._publisher = publisher
        self._consumer = consumer
        self._state_reader = state_reader
        self._grace_seconds = grace_seconds
        self._max_wait_seconds = max_wait_seconds
        self._max_poll_seconds = max_poll_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._sleep = sleep or asyncio.sleep

    async def run_current_surface_checkpoint(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> AppActiveSessionInspectionSummary:
        """下一次探索动作前有界推进判定，让规则缺证能及时反馈到探索。"""

        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        if deadline.utcoffset() is None:
            raise ValueError("deadline 必须带时区")
        remaining = (deadline - self._now()).total_seconds()
        if remaining <= 0:
            return AppActiveSessionInspectionSummary(
                published_work_item_count=0,
                drain_round_count=0,
            )
        published = 0
        try:
            async with asyncio.timeout(
                min(remaining, DEFAULT_CURRENT_SURFACE_CHECKPOINT_SECONDS)
            ):
                batch = await self._publisher.publish_pending_batch(
                    task_id=task,
                    agent_run_id=run_id,
                    limit=1,
                    cursor=None,
                )
                published = batch.created_count
                # 每轮只领取一个工作项，其余仍由独立 Judge Worker 公平消费。
                # 不能复用关闭前的全量排空，否则质检积压会吞掉全部探索预算。
                await self._consumer.drain_available(
                    task_id=task,
                    consumer_agent_run_id=run_id,
                    session_dependency="active_session",
                    max_items=1,
                )
        except TimeoutError:
            logger.warning(
                "task_id=%s run_id=%s App 当前页面质检检查点达到时间上限，"
                "未完成工作保留原始租约与重试事实。",
                task,
                run_id,
            )
        return AppActiveSessionInspectionSummary(
            published_work_item_count=published,
            drain_round_count=1,
        )

    async def drain_round(
        self,
        *,
        task_id: str,
        agent_run_id: str,
    ) -> AppActiveSessionInspectionRoundResult:
        """执行一轮发布和 Judge，供补采协调器与关闭流程复用。"""

        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        published = await self._publisher.publish_pending(
            task_id=task,
            agent_run_id=run_id,
        )
        await self._consumer.drain_available(
            task_id=task,
            consumer_agent_run_id=run_id,
            session_dependency="active_session",
        )
        state = await self._state_reader.load_drain_state(
            task_id=task,
            agent_run_id=run_id,
        )
        return AppActiveSessionInspectionRoundResult(
            published_work_item_count=published,
            state=state,
        )

    async def drain_before_close(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        deadline: datetime,
    ) -> AppActiveSessionInspectionSummary:
        task = _require_text(task_id, "task_id")
        run_id = _require_text(agent_run_id, "agent_run_id")
        if deadline.utcoffset() is None:
            raise ValueError("deadline 必须带时区")
        published_count = 0
        drain_round_count = 0
        waited = 0.0
        stalled = 0.0
        last_outstanding: tuple[int, int] | None = None
        while waited < self._max_wait_seconds:
            round_started = monotonic()
            round_result = await self.drain_round(
                task_id=task,
                agent_run_id=run_id,
            )
            # 发布、判定和读取状态的耗时同样消耗排空预算，不能只累计轮询休眠。
            waited += max(0.0, monotonic() - round_started)
            published_count += round_result.published_work_item_count
            drain_round_count += 1
            state = round_result.state
            if state.drained:
                logger.info(
                    "task_id=%s run_id=%s App 关闭前活会话规则已排空："
                    "published=%s rounds=%s waited=%.1fs。",
                    task,
                    run_id,
                    published_count,
                    drain_round_count,
                    waited,
                )
                return AppActiveSessionInspectionSummary(
                    published_work_item_count=published_count,
                    drain_round_count=drain_round_count,
                )
            outstanding = (
                state.pending_capture_count,
                state.nonterminal_work_item_count,
            )
            remaining = self._max_wait_seconds - waited
            if remaining <= 0:
                break
            current = self._now()
            delay = min(self._max_poll_seconds, remaining)
            if state.next_attempt_at is not None:
                delay = min(
                    delay,
                    max(0.05, (state.next_attempt_at - current).total_seconds()),
                )
            delay = max(0.05, delay)
            if last_outstanding is None or outstanding != last_outstanding:
                stalled = 0.0
            elif sum(outstanding) > 0:
                stalled += delay
                if stalled >= self._grace_seconds:
                    settled = await self._settle_or_raise(
                        task_id=task,
                        agent_run_id=run_id,
                        pending_capture_count=state.pending_capture_count,
                        nonterminal_work_item_count=state.nonterminal_work_item_count,
                        published_count=published_count,
                        drain_round_count=drain_round_count,
                    )
                    return AppActiveSessionInspectionSummary(
                        published_work_item_count=published_count,
                        drain_round_count=drain_round_count,
                        settled_work_item_count=settled,
                    )
            last_outstanding = outstanding
            await self._sleep(delay)
            waited += delay
        state = await self._state_reader.load_drain_state(
            task_id=task,
            agent_run_id=run_id,
        )
        settled = await self._settle_or_raise(
            task_id=task,
            agent_run_id=run_id,
            pending_capture_count=state.pending_capture_count,
            nonterminal_work_item_count=state.nonterminal_work_item_count,
            published_count=published_count,
            drain_round_count=drain_round_count,
        )
        return AppActiveSessionInspectionSummary(
            published_work_item_count=published_count,
            drain_round_count=drain_round_count,
            settled_work_item_count=settled,
        )

    async def _settle_or_raise(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        pending_capture_count: int,
        nonterminal_work_item_count: int,
        published_count: int,
        drain_round_count: int,
    ) -> int:
        """排空窗口用尽：能结算就结算并继续关闭会话，没有结算能力才抛错。

        抛错会让 CloseAppExploration 整条中断，设备租约结算（_settle_device_lease）
        根本执行不到，云机要等租约对账循环过期才回收；按证据不足结算并留放弃原因，
        比让收口无限重试、设备迟迟不释放更符合设计（01 专项 §6 步骤 9）。
        """
        detail = (
            f"pending_captures={pending_capture_count}, "
            f"nonterminal_work_items={nonterminal_work_item_count}"
        )
        settlement = self._closure_settlement
        if settlement is None:
            raise RuntimeError(f"App Session 关闭前 active-session 规则未排空：{detail}")
        result = await settlement.settle_stalled_work_for_closure(
            task_id=task_id,
            active_session_agent_run_id=agent_run_id,
        )
        logger.warning(
            "task_id=%s run_id=%s App 关闭前活会话规则未排空（%s），"
            "已按证据不足结算 %s 个工作项、%s 条规则后继续关闭会话：published=%s rounds=%s",
            task_id,
            agent_run_id,
            detail,
            result.settled_work_item_count,
            result.settled_rule_count,
            published_count,
            drain_round_count,
        )
        return result.settled_work_item_count


def build_default_app_live_inspection_publish_service() -> AppLiveInspectionPublishService:
    """Worker inspection 唤醒消费的 App 增量发布组合根。"""

    from services.api.app.application.app_inspection_publisher import (
        build_default_app_capture_inspection_publisher,
    )
    from services.api.app.infrastructure.db.app_live_inspection_repository import (
        PostgresAppLiveInspectionRepository,
    )

    return AppLiveInspectionPublishService(
        capture_reader=PostgresAppLiveInspectionRepository(),
        publisher=build_default_app_capture_inspection_publisher(),
    )


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
