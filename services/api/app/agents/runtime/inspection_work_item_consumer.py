"""V1 Worker 内持久化质检工作项消费者。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from time import perf_counter
from typing import Protocol

from services.api.app.agents.rule_judge import RuleJudgeAgent
from services.api.app.agents.rule_judge.bootstrap import build_default_rule_judge_agent
from services.api.app.agents.runtime.inspection_result_writer import (
    CurrentRuleDecisionWriter,
)
from services.api.app.agents.runtime.inspection_work_item_execution import (
    InspectionWorkItemAttempt,
    replay_recoverable_judgement,
)
from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
    get_agent_run_context,
)
from services.api.app.agents.runtime.usage_meter import (
    bind_run_llm_usage_meter,
    get_current_llm_usage_meter,
)
from services.api.app.application.h5_evidence_reachability_boundary import (
    apply_h5_evidence_reachability_boundary,
)
from services.api.app.application.inspection_handoff import (
    build_inspection_handoff,
    resolve_inspection_evidence_capture_roles,
)
from services.api.app.application.inspection_results import RuleDecisionWriter
from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    InspectionWorkItemConflictError,
    InspectionWorkItemLease,
    InspectionWorkItemService,
)
from services.api.app.application.live_inspection_publication import (
    LiveInspectionPublicationBatchResult,
)
from services.api.app.application.rule_execution import RuleExecutionLeaseConflictError
from services.api.app.application.rule_result_reuse import (
    RuleReusePlan,
    RuleReusePlanner,
    merge_reused_decisions,
    no_reuse_plan,
)
from services.api.app.application.run_usage import usage_snapshot_to_payload
from services.api.app.application.supplemental_evidence_outcome import (
    apply_supplemental_evidence_policy,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.application.web_active_interaction_boundary import (
    apply_web_active_interaction_boundary,
)
from services.api.app.application.web_sequence_errors import WebRuleContractIncompatibleError
from services.api.app.domain.inspection.handoff import InspectionHandoff

logger = logging.getLogger(__name__)

DEFAULT_INSPECTION_ATTEMPT_TIMEOUT_SECONDS = 120.0
_PUBLICATION_CANCEL_GRACE_SECONDS = 0.05


class H5ClosedSessionSettlement(Protocol):
    """按持久化关闭事实结算 H5 补采，不能把来源 Run 换成消费 Run。"""

    async def settle_requests_after_session_close(
        self, *, task_id: str, agent_run_id: str,
    ) -> int: ...


@dataclass(frozen=True)
class InspectionWorkItemDrainResult:
    """一次任务级 Snapshot 工作项排空摘要。"""

    claimed_count: int
    completed_count: int
    recovered_count: int
    deferred_count: int
    usage: dict[str, object] | None = None


@dataclass(frozen=True)
class InspectionJudgeDispatchRequest:
    """独立 Judge worker 的 task/run 调度身份；业务工作仍由 PostgreSQL claim。"""

    task_id: str
    consumer_agent_run_id: str


class LiveInspectionPublisherPort(Protocol):
    """按保守小批补发 Capture 对应质检工作项。"""

    async def publish_pending(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: None = None,
    ) -> int: ...

    async def publish_pending_batch(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        limit: int,
        cursor: str | None,
    ) -> LiveInspectionPublicationBatchResult: ...


class _InspectionPublicationStopping(RuntimeError):
    """停止信号先于 Publication 完成。"""


class _InspectionPublicationStuck(RuntimeError):
    """Publication 吞掉取消，仍在后台占用调度槽。"""


class InspectionJudgeDispatcher:
    """合并重复 wakeup，并以公平小批次驱动独立 Judge worker。"""

    def __init__(
        self,
        *,
        service: InspectionWorkItemService,
        consumer: InspectionWorkItemConsumer,
        live_publisher: LiveInspectionPublisherPort | None = None,
        batch_limit: int,
        publication_batch_limit: int = 1,
        publication_timeout_seconds: float = 30.0,
        queue_capacity: int,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        if (
            batch_limit <= 0
            or publication_batch_limit <= 0
            or publication_timeout_seconds <= 0
            or queue_capacity <= 0
            or retry_delay_seconds <= 0
        ):
            raise ValueError(
                "batch_limit、publication_batch_limit、publication_timeout_seconds、"
                "queue_capacity、retry_delay_seconds 必须大于 0"
            )
        self._service = service
        self._consumer = consumer
        self._live_publisher = live_publisher
        self._batch_limit = batch_limit
        self._publication_batch_limit = publication_batch_limit
        self._publication_timeout_seconds = publication_timeout_seconds
        self._retry_delay_seconds = retry_delay_seconds
        self._queue: asyncio.Queue[InspectionJudgeDispatchRequest] = asyncio.Queue(
            maxsize=queue_capacity
        )
        self._queued: set[InspectionJudgeDispatchRequest] = set()
        self._active: set[InspectionJudgeDispatchRequest] = set()
        self._dirty: set[InspectionJudgeDispatchRequest] = set()
        self._reschedule: set[InspectionJudgeDispatchRequest] = set()
        self._publication_requested: set[InspectionJudgeDispatchRequest] = set()
        self._publication_cursors: dict[InspectionJudgeDispatchRequest, str | None] = {}
        self._publication_slots: asyncio.Semaphore | None = None
        self._stuck_publications: dict[
            InspectionJudgeDispatchRequest,
            asyncio.Task[LiveInspectionPublicationBatchResult],
        ] = {}
        self._stuck_publication_total = 0
        self._coalesced_count = 0
        self._accepting = True
        self._closed = False
        self._stop_event: asyncio.Event | None = None

    @property
    def coalesced_count(self) -> int:
        return self._coalesced_count

    @property
    def stuck_publication_count(self) -> int:
        """仍未响应取消的 Publication 数量。"""

        return len(self._stuck_publications)

    @property
    def stuck_publication_total(self) -> int:
        """本 dispatcher 生命周期内进入 stuck 的 Publication 总数。"""

        return self._stuck_publication_total

    @property
    def accepting(self) -> bool:
        return (
            self._accepting
            and not self._closed
            and (self._stop_event is None or not self._stop_event.is_set())
        )

    @property
    def stopping(self) -> bool:
        return not self._closed and (
            not self._accepting
            or (self._stop_event is not None and self._stop_event.is_set())
        )

    @property
    def closed(self) -> bool:
        return self._closed

    async def submit(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        prepare_publication: bool = False,
    ) -> bool:
        """接收 task/run 唤醒；重复身份合并，容量不足时保留 Redis Pending 重试。"""
        if (
            not self._accepting
            or self._closed
            or (self._stop_event is not None and self._stop_event.is_set())
        ):
            self._accepting = False
            logger.info(
                "inspection_dispatch_rejected_stopping task_id=%s "
                "consumer_agent_run_id=%s",
                task_id,
                consumer_agent_run_id,
            )
            return False
        request = InspectionJudgeDispatchRequest(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
        )
        if (
            request in self._queued
            or request in self._active
            or request in self._reschedule
            or request in self._stuck_publications
        ):
            self._coalesced_count += 1
            if prepare_publication:
                self._publication_requested.add(request)
                self._publication_cursors.setdefault(request, None)
            if request in self._active:
                self._dirty.add(request)
            logger.info(
                "inspection_wakeup_coalesced task_id=%s consumer_agent_run_id=%s "
                "coalesced_count=%d",
                task_id,
                consumer_agent_run_id,
                self._coalesced_count,
            )
            return True
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            logger.warning(
                "inspection_dispatch_queue_full task_id=%s consumer_agent_run_id=%s "
                "queue_capacity=%d",
                task_id,
                consumer_agent_run_id,
                self._queue.maxsize,
            )
            return False
        self._queued.add(request)
        if prepare_publication:
            self._publication_requested.add(request)
            self._publication_cursors.setdefault(request, None)
        return True

    async def run(self, stop_event: asyncio.Event, *, concurrency: int) -> None:
        """运行固定数量 Judge worker；停止后不再领取新批次并等待在途批次收敛。"""
        if concurrency <= 0:
            raise ValueError("concurrency 必须大于 0")
        self._stop_event = stop_event
        if stop_event.is_set():
            self._accepting = False
            self._closed = True
            return
        self._publication_slots = asyncio.Semaphore(concurrency)
        workers = [
            asyncio.create_task(
                self._worker_loop(stop_event),
                name=f"alioth-inspection-judge-{worker_id}",
            )
            for worker_id in range(concurrency)
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            self._accepting = False
            await self._stop_stuck_publications()
            self._closed = True

    async def _worker_loop(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            self._flush_reschedule()
            try:
                request = await asyncio.wait_for(self._queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            self._queued.discard(request)
            if stop_event.is_set():
                self._accepting = False
                self._queue.task_done()
                return
            self._active.add(request)
            started_at = perf_counter()
            needs_more = False
            stage = "publication"
            publication_cursor: str | None = None
            try:
                prepare_publication = request in self._publication_requested
                self._publication_requested.discard(request)
                if prepare_publication and self._live_publisher is not None:
                    publication_started_at = perf_counter()
                    publication_cursor = self._publication_cursors.pop(request, None)
                    publication_result = await self._await_publication_batch(
                        request=request,
                        cursor=publication_cursor,
                        stop_event=stop_event,
                    )
                    publication_result = LiveInspectionPublicationBatchResult(
                        attempted_count=int(publication_result.attempted_count),
                        created_count=int(publication_result.created_count),
                        has_more=bool(publication_result.has_more),
                        next_cursor=publication_result.next_cursor,
                    )
                    if publication_result.has_more:
                        self._publication_cursors[request] = (
                            publication_result.next_cursor
                        )
                        self._publication_requested.add(request)
                    elif request not in self._publication_requested:
                        self._publication_cursors.pop(request, None)
                    needs_more = needs_more or publication_result.has_more
                    logger.info(
                        "inspection_publication_batch task_id=%s "
                        "consumer_agent_run_id=%s attempted=%d created=%d "
                        "has_more=%s batch_limit=%d duration_seconds=%.6f",
                        request.task_id,
                        request.consumer_agent_run_id,
                        publication_result.attempted_count,
                        publication_result.created_count,
                        publication_result.has_more,
                        self._publication_batch_limit,
                        perf_counter() - publication_started_at,
                    )
                if stop_event.is_set():
                    raise _InspectionPublicationStopping
                stage = "judge"
                result = await self._consumer.drain_available(
                    task_id=request.task_id,
                    consumer_agent_run_id=request.consumer_agent_run_id,
                    session_dependency=None,
                    max_items=self._batch_limit,
                )
                needs_more = result.claimed_count >= self._batch_limit
                if result.usage:
                    usage_recorded = await self._service.record_consumer_usage(
                        task_id=request.task_id,
                        consumer_agent_run_id=request.consumer_agent_run_id,
                        usage=result.usage,
                    )
                    if not usage_recorded:
                        logger.warning(
                            "task_id=%s consumer_run_id=%s Judge worker 用量写入失败。",
                            request.task_id,
                            request.consumer_agent_run_id,
                        )
                await self._service.finish_publication_consumer_run_if_idle(
                    task_id=request.task_id,
                    consumer_agent_run_id=request.consumer_agent_run_id,
                )
                logger.info(
                    "inspection_judge_batch task_id=%s consumer_agent_run_id=%s "
                    "claimed=%d completed=%d recovered=%d deferred=%d "
                    "duration_seconds=%.6f",
                    request.task_id,
                    request.consumer_agent_run_id,
                    result.claimed_count,
                    result.completed_count,
                    result.recovered_count,
                    result.deferred_count,
                    perf_counter() - started_at,
                )
            except _InspectionPublicationStopping:
                self._publication_cursors[request] = publication_cursor
                self._publication_requested.add(request)
                logger.info(
                    "inspection_publication_cancelled_stopping task_id=%s "
                    "consumer_agent_run_id=%s",
                    request.task_id,
                    request.consumer_agent_run_id,
                )
                return
            except _InspectionPublicationStuck:
                self._publication_cursors[request] = publication_cursor
                self._publication_requested.add(request)
            except asyncio.CancelledError:
                if stage == "publication":
                    self._publication_cursors[request] = publication_cursor
                    self._publication_requested.add(request)
                raise
            except Exception:  # noqa: BLE001 - PostgreSQL 短断后保留身份重试
                needs_more = True
                if stage == "publication":
                    self._publication_cursors[request] = publication_cursor
                    self._publication_requested.add(request)
                logger.exception(
                    "inspection_dispatch_batch_failed stage=%s task_id=%s "
                    "consumer_agent_run_id=%s duration_seconds=%.6f",
                    stage,
                    request.task_id,
                    request.consumer_agent_run_id,
                    perf_counter() - started_at,
                )
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._retry_delay_seconds,
                    )
                except TimeoutError:
                    pass
            finally:
                self._active.discard(request)
                needs_more = (
                    needs_more
                    or request in self._dirty
                    or request in self._publication_requested
                )
                self._dirty.discard(request)
                if (
                    needs_more
                    and not stop_event.is_set()
                    and request not in self._stuck_publications
                ):
                    self._reschedule.add(request)
                self._queue.task_done()

    async def _await_publication_batch(
        self,
        *,
        request: InspectionJudgeDispatchRequest,
        cursor: str | None,
        stop_event: asyncio.Event,
    ) -> LiveInspectionPublicationBatchResult:
        """为 Publication 单独限时；停止优先时取消挂起发布。"""

        if self._live_publisher is None:
            return LiveInspectionPublicationBatchResult(0, 0, False)
        await self._acquire_publication_slot(stop_event=stop_event)
        publisher_task = asyncio.create_task(
            self._live_publisher.publish_pending_batch(
                task_id=request.task_id,
                agent_run_id=request.consumer_agent_run_id,
                limit=self._publication_batch_limit,
                cursor=cursor,
            )
        )
        stop_task = asyncio.create_task(stop_event.wait())
        result_task: asyncio.Task[LiveInspectionPublicationBatchResult] | None = None
        terminal_error: BaseException | None = None
        try:
            done, _pending = await asyncio.wait(
                {publisher_task, stop_task},
                timeout=self._publication_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_event.is_set() or stop_task in done:
                terminal_error = _InspectionPublicationStopping()
            elif publisher_task in done:
                result_task = publisher_task
            else:
                terminal_error = TimeoutError(
                    f"Publication 超过 {self._publication_timeout_seconds:g} 秒未完成"
                )
        except BaseException as exc:
            terminal_error = exc

        try:
            for task in (publisher_task, stop_task):
                if not task.done():
                    task.cancel()
            done_after_cancel, pending_after_cancel = await asyncio.wait(
                {publisher_task, stop_task},
                timeout=_PUBLICATION_CANCEL_GRACE_SECONDS,
            )
            for task in done_after_cancel:
                if task is not result_task:
                    _consume_task_result(task)
            if publisher_task in pending_after_cancel:
                self._track_stuck_publication(
                    request=request,
                    publisher_task=publisher_task,
                )
                if isinstance(terminal_error, asyncio.CancelledError):
                    raise terminal_error
                if isinstance(terminal_error, _InspectionPublicationStopping):
                    raise terminal_error
                raise _InspectionPublicationStuck(
                    "Publication 未在取消宽限内退出"
                ) from terminal_error
        finally:
            if request not in self._stuck_publications:
                self._release_publication_slot()

        if terminal_error is not None:
            raise terminal_error
        if result_task is None:
            raise RuntimeError("Publication 等待结束但没有结果")
        return result_task.result()

    async def _acquire_publication_slot(self, *, stop_event: asyncio.Event) -> None:
        """stuck Publication 继续占槽，且等待槽时仍以停止信号优先。"""

        slots = self._publication_slots
        if slots is None:
            raise RuntimeError("Inspection dispatcher 尚未启动")
        acquire_task = asyncio.create_task(slots.acquire())
        stop_task = asyncio.create_task(stop_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {acquire_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stop_event.is_set() or stop_task in done:
                if acquire_task in done and acquire_task.result():
                    slots.release()
                raise _InspectionPublicationStopping
            await acquire_task
        finally:
            for task in (acquire_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(acquire_task, stop_task, return_exceptions=True)

    def _release_publication_slot(self) -> None:
        slots = self._publication_slots
        if slots is not None:
            slots.release()

    def _track_stuck_publication(
        self,
        *,
        request: InspectionJudgeDispatchRequest,
        publisher_task: asyncio.Task[LiveInspectionPublicationBatchResult],
    ) -> None:
        """显式持有吞取消的 task；完成前禁止同身份和超额 Publication。"""

        if request in self._stuck_publications:
            raise RuntimeError("同一调度身份已有 stuck Publication")
        self._stuck_publications[request] = publisher_task
        self._stuck_publication_total += 1
        publisher_task.add_done_callback(
            lambda task: self._recover_stuck_publication(
                request=request,
                publisher_task=task,
            )
        )
        logger.critical(
            "inspection_publication_stuck_fatal task_id=%s "
            "consumer_agent_run_id=%s grace_seconds=%.3f stuck_current=%d "
            "stuck_total=%d",
            request.task_id,
            request.consumer_agent_run_id,
            _PUBLICATION_CANCEL_GRACE_SECONDS,
            self.stuck_publication_count,
            self.stuck_publication_total,
        )

    def _recover_stuck_publication(
        self,
        *,
        request: InspectionJudgeDispatchRequest,
        publisher_task: asyncio.Task[LiveInspectionPublicationBatchResult],
    ) -> None:
        """回收 orphan，并从 PostgreSQL 库存重新扫描其不确定批次。"""

        if self._stuck_publications.get(request) is not publisher_task:
            _consume_task_result(publisher_task)
            return
        self._stuck_publications.pop(request, None)
        self._release_publication_slot()
        _consume_task_result(publisher_task)
        logger.warning(
            "inspection_publication_stuck_recovered task_id=%s "
            "consumer_agent_run_id=%s stuck_current=%d",
            request.task_id,
            request.consumer_agent_run_id,
            self.stuck_publication_count,
        )
        if self.accepting:
            self._reschedule.add(request)

    async def _stop_stuck_publications(self) -> None:
        """停止时再次请求取消，但只做有界等待；进程强杀归 supervisor。"""

        tracked = tuple(self._stuck_publications.values())
        if not tracked:
            return
        for task in tracked:
            if not task.done():
                task.cancel()
        _done, pending = await asyncio.wait(
            tracked,
            timeout=_PUBLICATION_CANCEL_GRACE_SECONDS,
        )
        if pending:
            logger.critical(
                "inspection_publication_stuck_shutdown_fatal stuck_current=%d "
                "grace_seconds=%.3f supervisor_kill_required=true",
                len(pending),
                _PUBLICATION_CANCEL_GRACE_SECONDS,
            )

    def _flush_reschedule(self) -> None:
        for request in tuple(self._reschedule):
            if self._queue.full():
                return
            self._queue.put_nowait(request)
            self._queued.add(request)
            self._reschedule.discard(request)


def _consume_task_result(task: asyncio.Task[object]) -> None:
    """回收已取消的后台 Publication Task 异常，避免事件循环告警。"""

    try:
        task.exception()
    except asyncio.CancelledError:
        pass


class InspectionWorkItemConsumer:
    """在现有 Worker 进程内按会话依赖消费质检工作项。"""

    def __init__(
        self,
        *,
        service: InspectionWorkItemService,
        lease_owner: str,
        lease_seconds: int,
        retry_backoff_seconds: float,
        max_drain_items: int = 1000,
        attempt_timeout_seconds: float = DEFAULT_INSPECTION_ATTEMPT_TIMEOUT_SECONDS,
        judge: RuleJudgeAgent | None = None,
        reuse_planner: RuleReusePlanner | None = None,
        result_writer: RuleDecisionWriter | None = None,
        h5_result_writer: RuleDecisionWriter | None = None,
        h5_closed_session_settlement: H5ClosedSessionSettlement | None = None,
        supplemental_evidence_policy: SupplementalEvidencePolicy | None = None,
        supplemental_evidence_target_platforms: frozenset[str] | None = None,
    ) -> None:
        self._service = service
        self._lease_owner = lease_owner
        self._lease_seconds = lease_seconds
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_drain_items = max(1, max_drain_items)
        if attempt_timeout_seconds <= 0:
            raise ValueError("attempt_timeout_seconds 必须大于 0")
        self._attempt_timeout_seconds = float(attempt_timeout_seconds)
        self._judge = judge if judge is not None else build_default_rule_judge_agent()
        # 规则结论复用规划器；None 表示不复用，每页各自判定（默认，测试与旧装配点不受影响）。
        self._reuse_planner = reuse_planner
        self._supplemental_evidence_policy = (
            supplemental_evidence_policy
            if supplemental_evidence_policy is not None
            else SupplementalEvidencePolicy()
        )
        if supplemental_evidence_target_platforms is None:
            self._supplemental_evidence_target_platforms = None
        else:
            normalized_platforms = frozenset(
                str(platform).strip().casefold()
                for platform in supplemental_evidence_target_platforms
                if str(platform).strip()
            )
            if not normalized_platforms:
                raise ValueError("supplemental_evidence_target_platforms 不能为空")
            self._supplemental_evidence_target_platforms = normalized_platforms
        self._result_writer = (
            result_writer
            if result_writer is not None
            else CurrentRuleDecisionWriter(
                supplemental_evidence_policy=self._supplemental_evidence_policy
            )
        )
        self._h5_result_writer = h5_result_writer
        self._h5_closed_session_settlement = h5_closed_session_settlement

    async def drain_snapshot(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> InspectionWorkItemDrainResult:
        """兼容现有调用方，只排空可脱离活跃会话执行的 Snapshot 工作项。"""
        return await self.drain_available(
            task_id=task_id,
            consumer_agent_run_id=consumer_agent_run_id,
            session_dependency="snapshot",
        )

    async def drain_available(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        session_dependency: str | None = None,
        allow_terminal_web_owner_recovery: bool = False,
        max_items: int | None = None,
    ) -> InspectionWorkItemDrainResult:
        """以显式任务／Run 身份绑定业务 Tool 上下文后排空工作项。"""

        if get_current_llm_usage_meter() is not None:
            return await self._drain_available_with_context(
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
                session_dependency=session_dependency,
                allow_terminal_web_owner_recovery=allow_terminal_web_owner_recovery,
                max_items=max_items,
            )
        with bind_run_llm_usage_meter() as usage_meter:
            result = await self._drain_available_with_context(
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
                session_dependency=session_dependency,
                allow_terminal_web_owner_recovery=allow_terminal_web_owner_recovery,
                max_items=max_items,
            )
        return replace(result, usage=usage_snapshot_to_payload(usage_meter.snapshot()))

    async def _drain_available_with_context(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        session_dependency: str | None,
        allow_terminal_web_owner_recovery: bool,
        max_items: int | None,
    ) -> InspectionWorkItemDrainResult:
        """复用当前 Run 上下文；缺失或不匹配时临时绑定消费者身份。"""

        current = get_agent_run_context()
        if (
            current is not None
            and current.task_id == task_id
            and current.agent_run_id == consumer_agent_run_id
        ):
            return await self._drain_available(
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
                session_dependency=session_dependency,
                allow_terminal_web_owner_recovery=allow_terminal_web_owner_recovery,
                max_items=max_items,
            )
        with bind_agent_run_context(
            AgentRunContext(task_id=task_id, agent_run_id=consumer_agent_run_id)
        ):
            return await self._drain_available(
                task_id=task_id,
                consumer_agent_run_id=consumer_agent_run_id,
                session_dependency=session_dependency,
                allow_terminal_web_owner_recovery=allow_terminal_web_owner_recovery,
                max_items=max_items,
            )

    async def _drain_available(
        self,
        *,
        task_id: str,
        consumer_agent_run_id: str,
        session_dependency: str | None = None,
        allow_terminal_web_owner_recovery: bool = False,
        max_items: int | None = None,
    ) -> InspectionWorkItemDrainResult:
        """排空当前 Run 有权领取的工作项；None 同时覆盖活跃会话与 Snapshot。"""
        if session_dependency not in {None, "snapshot", "active_session"}:
            raise ValueError("session_dependency 仅允许 snapshot、active_session 或 None")
        if max_items is not None and max_items <= 0:
            raise ValueError("max_items 必须大于 0")
        drain_limit = self._max_drain_items if max_items is None else min(
            self._max_drain_items,
            max_items,
        )
        claimed_count = 0
        completed_count = 0
        recovered_count = 0
        deferred_count = 0
        while claimed_count < drain_limit:
            lease = await self._service.claim(
                ClaimInspectionWorkItemCommand(
                    task_id=task_id,
                    consumer_agent_run_id=consumer_agent_run_id,
                    lease_owner=self._lease_owner,
                    lease_seconds=self._lease_seconds,
                    session_dependency=session_dependency,
                    allow_terminal_web_owner_recovery=(
                        allow_terminal_web_owner_recovery
                    ),
                )
            )
            if lease is None:
                break
            claimed_count += 1
            attempt = InspectionWorkItemAttempt(
                service=self._service,
                lease=lease,
                consumer_agent_run_id=consumer_agent_run_id,
                lease_seconds=self._lease_seconds,
                retry_backoff_seconds=self._retry_backoff_seconds,
                allow_terminal_web_owner_recovery=(
                    allow_terminal_web_owner_recovery
                ),
            )
            try:
                await attempt.start()
                async with asyncio.timeout(self._attempt_timeout_seconds):
                    finalized, recovered = await self._execute_started_attempt(
                        attempt=attempt,
                        lease=lease,
                        task_id=task_id,
                        consumer_agent_run_id=consumer_agent_run_id,
                    )
                if recovered:
                    recovered_count += 1
                if finalized is not None:
                    completed_count += 1
                else:
                    deferred_count += 1
            except TimeoutError:
                logger.warning(
                    "work_item_id=%s 单次质检超过 %.1f 秒，已释放租约等待重试。",
                    lease.work_item_id,
                    self._attempt_timeout_seconds,
                )
                try:
                    await attempt.fail_incomplete(
                        retryable=True,
                        error_code="INSPECTION_WORK_ITEM_TIMEOUT",
                        error_message=(
                            f"单次质检超过 {self._attempt_timeout_seconds:g} 秒，"
                            "已停止本次判定并等待重试"
                        ),
                    )
                except Exception:  # noqa: BLE001 - 超时与状态收口失败都必须保留日志
                    logger.exception(
                        "work_item_id=%s 超时后的状态收口失败。",
                        lease.work_item_id,
                    )
                deferred_count += 1
            except InspectionWorkItemConflictError:
                logger.info(
                    "work_item_id=%s 状态已被其他流程推进，当前消费者跳过收口。",
                    lease.work_item_id,
                )
                deferred_count += 1
            except WebRuleContractIncompatibleError as exc:
                try:
                    await attempt.terminate_web_contract_incompatibility(command=exc.command)
                except Exception:
                    logger.exception("Web 合同不兼容的当前租约技术收口失败。")
                deferred_count += 1
            except RuleExecutionLeaseConflictError as exc:
                logger.warning(
                    "work_item_id=%s 租约 fencing 拒绝写回：%s",
                    lease.work_item_id,
                    exc,
                )
                try:
                    await attempt.fail_incomplete(
                        retryable=True,
                        error_code="INSPECTION_LEASE_CONFLICT",
                        error_message=str(exc) or exc.__class__.__name__,
                    )
                except Exception:  # noqa: BLE001 - 原异常与租约失效都必须保留日志
                    logger.exception(
                        "work_item_id=%s 租约冲突后的状态收口也失败。",
                        lease.work_item_id,
                    )
                deferred_count += 1
            except Exception as exc:  # noqa: BLE001 - 单个工作项失败不得中断其余批次
                logger.exception(
                    "work_item_id=%s Snapshot 持久化消费失败。",
                    lease.work_item_id,
                )
                try:
                    from services.api.app.application.h5_llm_budget import h5_llm_budget_error_code

                    budget_error = (
                        h5_llm_budget_error_code(exc) if lease.target_platform == "h5" else None
                    )
                    from services.api.app.application.rule_judge import (
                        RuleJudgeEvidenceInsufficientError,
                        RuleJudgeValidationError,
                    )

                    contract_error = (
                        lease.target_platform == "h5"
                        and isinstance(exc, RuleJudgeValidationError)
                        and not isinstance(exc, RuleJudgeEvidenceInsufficientError)
                    )
                    await attempt.fail_incomplete(
                        retryable=budget_error is None and not contract_error,
                        error_code=budget_error or (
                            "H5_EVIDENCE_CONTRACT_INVALID" if contract_error
                            else "INSPECTION_WORK_ITEM_CONSUME_FAILED"
                        ),
                        error_message=str(exc) or exc.__class__.__name__,
                    )
                except Exception:  # noqa: BLE001 - 原异常与租约失效都必须保留日志
                    logger.exception(
                        "work_item_id=%s 消费失败后的状态收口也失败。",
                        lease.work_item_id,
                    )
                deferred_count += 1
        return InspectionWorkItemDrainResult(
            claimed_count=claimed_count,
            completed_count=completed_count,
            recovered_count=recovered_count,
            deferred_count=deferred_count,
        )

    async def _plan_rule_reuse(self, handoff: InspectionHandoff) -> RuleReusePlan:
        """判定前先看哪些规则可以沿用同一任务里已有的结论；没装规划器就全量判定。"""
        if self._reuse_planner is None:
            return no_reuse_plan(handoff)
        plan = await self._reuse_planner.plan(handoff=handoff)
        if plan.reused:
            logger.info(
                "task_id=%s work_item_id=%s 规则结论复用 %d 条（%s），仍需判定 %d 条",
                handoff.task_id,
                handoff.work_item_id,
                len(plan.reused_decisions),
                ",".join(
                    f"{decision.rule_code}<-{record.source_execution_id}"
                    for decision, record in zip(plan.reused_decisions, plan.records, strict=True)
                ),
                len(plan.judge_rules),
            )
        return plan

    async def _execute_started_attempt(
        self,
        *,
        attempt: InspectionWorkItemAttempt,
        lease: InspectionWorkItemLease,
        task_id: str,
        consumer_agent_run_id: str,
    ) -> tuple[dict[str, object] | None, bool]:
        """在单次硬时限内完成证据恢复、Judge、正式写回和工作项收口。"""
        existing = await attempt.recover_formal_results()
        if existing is not None:
            return existing, True
        async with attempt.lease_heartbeat():
            active_context = get_agent_run_context()
            write_context = AgentRunContext(
                task_id=task_id,
                agent_run_id=consumer_agent_run_id,
                task_config=(
                    dict(active_context.task_config)
                    if active_context is not None and active_context.task_config is not None
                    else None
                ),
                work_item_id=lease.work_item_id,
                attempt_id=attempt.attempt_id,
                lease_token=lease.lease_token,
                h5_llm_budget=lease.target_platform == "h5",
            )
            if lease.recoverable_result_payload:
                await attempt.persist_recovered_judgement(lease.recoverable_result_payload)
                with bind_agent_run_context(write_context):
                    result = await replay_recoverable_judgement(
                        lease=lease,
                        result_payload=lease.recoverable_result_payload,
                    )
                recovered = True
            else:
                roles = await resolve_inspection_evidence_capture_roles(lease)
                handoff = build_inspection_handoff(
                    lease,
                    attempt_id=attempt.attempt_id,
                    evidence_capture_roles=roles,
                )
                reuse_plan = await self._plan_rule_reuse(handoff)
                judge_handoff = reuse_plan.judge_handoff(handoff)
                if lease.target_platform == "h5":
                    with bind_agent_run_context(write_context):
                        judged_outcome = (
                            await self._judge.judge(judge_handoff)
                            if judge_handoff is not None else None
                        )
                else:
                    judged_outcome = (
                        await self._judge.judge(judge_handoff)
                        if judge_handoff is not None else None
                    )
                raw_outcome = merge_reused_decisions(
                    handoff=handoff,
                    outcome=judged_outcome,
                    decisions=reuse_plan.reused_decisions,
                )
                web_boundary = apply_web_active_interaction_boundary(
                    outcome=raw_outcome,
                    target_platform=lease.target_platform,
                    work_session_dependency=lease.session_dependency,
                )
                raw_outcome = web_boundary.outcome
                if web_boundary.suppressed:
                    logger.info(
                        "task_id=%s work_item_id=%s Web 主动交互请求已按安全边界抑制，"
                        "request_ids=%s reason_code=web_active_interaction_not_enabled",
                        raw_outcome.task_id,
                        raw_outcome.work_item_id,
                        ",".join(web_boundary.suppressed_request_ids),
                    )
                h5_boundary = apply_h5_evidence_reachability_boundary(
                    outcome=raw_outcome,
                    target_platform=lease.target_platform,
                )
                raw_outcome = h5_boundary.outcome
                if h5_boundary.suppressed:
                    logger.info(
                        "task_id=%s work_item_id=%s H5 不可达补采请求已确定性收口，"
                        "request_ids=%s reason_code=h5_evidence_target_unreachable",
                        raw_outcome.task_id,
                        raw_outcome.work_item_id,
                        ",".join(h5_boundary.suppressed_request_ids),
                    )
                policy_application = apply_supplemental_evidence_policy(
                    outcome=raw_outcome,
                    policy=self._supplemental_evidence_policy_for(
                        target_platform=lease.target_platform,
                    ),
                )
                outcome = policy_application.outcome
                if policy_application.suppressed:
                    logger.info(
                        "task_id=%s work_item_id=%s 补采请求已按服务端策略抑制，"
                        "request_ids=%s reason_code=%s policy_version=%s",
                        outcome.task_id,
                        outcome.work_item_id,
                        ",".join(policy_application.suppressed_request_ids),
                        policy_application.gate_decision.reason_code,
                        policy_application.gate_decision.policy_version,
                    )
                await attempt.persist_judgement(outcome.to_dict())
                writer = (
                    self._h5_result_writer
                    if lease.target_platform == "h5" and self._h5_result_writer is not None
                    else self._result_writer
                )
                with bind_agent_run_context(write_context):
                    result = await writer.write(
                        handoff=handoff,
                        outcome=outcome,
                    )
                recovered = False
            # 正式写回同步和 Work/Attempt 收口同样依赖当前 fencing 租约，
            # 必须纳入心跳保护，避免慢写回刚完成就因租约到期被误判失败。
            h5_settlement = (
                self._h5_closed_session_settlement if lease.target_platform == "h5" else None
            )
            if h5_settlement is not None:
                await h5_settlement.settle_requests_after_session_close(
                    task_id=lease.task_id, agent_run_id=lease.producer_agent_run_id,
                )
            finalized = await attempt.finalize_after_tool(result)
            if h5_settlement is not None:
                await self._service.reopen_waiting_evidence(task_id=lease.task_id)
        return finalized, recovered

    def _supplemental_evidence_policy_for(
        self,
        *,
        target_platform: str | None,
    ) -> SupplementalEvidencePolicy:
        """按组合根声明的平台边界选择补采策略，防止共用 Judge 串台。"""
        allowed = self._supplemental_evidence_target_platforms
        if allowed is None:
            return self._supplemental_evidence_policy
        normalized = str(target_platform or "").strip().casefold()
        if normalized in allowed:
            return self._supplemental_evidence_policy
        return SupplementalEvidencePolicy()
