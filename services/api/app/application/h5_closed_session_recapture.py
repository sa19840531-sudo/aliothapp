"""H5 浏览器关闭后的 Snapshot 补采组合入口。"""

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from services.api.app.agents.runtime.usage_meter import get_current_llm_usage_meter
from services.api.app.domain.exploration import ExplorationBudget, ExplorationWork

logger = logging.getLogger(__name__)


async def drain_h5_closed_session_snapshots(
    *, record: Any, settings: Any, worker_instance_id: str
) -> int:
    """每个任务跨进程串行处理；沿用原 Work 的执行域、预算与截止时间。"""
    target = (record.task_config or {}).get("_task_target", {})
    if (
        record.task_id is None
        or target.get("platform") != "h5"
        or record.task_config.get("workflow_version") != "v2"
        or not settings.supplemental_evidence.enabled
        or settings.workflow.h5_v2_mode != "on"
    ):
        return 0
    from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
    from services.api.app.application.h5_live_inspection_publish import (
        DEFAULT_ACTIVE_SESSION_DRAIN_GRACE_SECONDS,
        DEFAULT_ACTIVE_SESSION_DRAIN_MAX_WAIT_SECONDS,
        build_default_h5_active_session_inspection_drain_service,
    )
    from services.api.app.application.h5_runtime_composition import (
        PersistingH5PageDetector,
        _h5_usage_budget_error,
    )
    from services.api.app.application.h5_task_runtime_settings import build_h5_task_runtime_settings
    from services.api.app.application.supplemental_evidence_policy import SupplementalEvidencePolicy
    from services.api.app.infrastructure.browser.capture_runtime import (
        build_default_web_capture_runtime,
    )
    from services.api.app.infrastructure.db.h5_closed_session_context import (
        PostgresH5ClosedSessionContextReader,
    )
    from services.api.app.infrastructure.db.h5_interaction_pattern_repository import (
        PostgresH5InteractionPatternRepository,
    )
    from services.api.app.infrastructure.db.llm_usage_event_repository import (
        PostgresLLMUsageEventRepository,
    )
    from services.api.app.infrastructure.db.pool import get_postgres_pool

    contexts = PostgresH5ClosedSessionContextReader()
    frozen = await contexts.frozen_frontier(task_id=record.task_id, agent_run_id=record.run_id)
    if frozen is None:
        return 0
    payload = frozen["work_payload"]
    work = ExplorationWork.from_dict(json.loads(payload) if isinstance(payload, str) else payload)
    if work.task_id != record.task_id or work.surface_kind.value != "web":
        raise RuntimeError("H5 新会话补采原 Work 执行域不匹配")
    deadline = work.deadline
    if deadline <= datetime.now(UTC):
        return 0
    limits = build_h5_task_runtime_settings(
        task_config=record.task_config,
        loop_guard=record.loop_guard,
        page_sampling_policy=record.page_sampling_policy,
        defaults=settings.agent_runtime,
    )
    pool = await get_postgres_pool()
    async with pool.acquire() as lock_connection:
        key = f"h5-new-session-recapture:{record.task_id}"
        acquired = await lock_connection.fetchval("SELECT pg_try_advisory_lock(hashtext($1))", key)
        if not acquired:
            return 0
        runtime = None
        drain = None
        progressed = 0
        primary_error = None
        try:
            if not await contexts.has_closed_snapshot_requests(
                task_id=record.task_id,
                agent_run_id=record.run_id,
            ):
                return 0
            audit = PostgresLLMUsageEventRepository()

            async def checkpoint() -> None:
                await contexts.check_budget(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    finalization_epoch=int(frozen["finalization_epoch"]),
                    max_actions=min(limits.max_actions, work.budget.max_actions),
                    deadline=deadline,
                )
                meter = get_current_llm_usage_meter()
                if meter is not None:
                    await meter.flush_audit_events(timeout_seconds=5.0)
                    if any(
                        not task.done() or task.cancelled() or task.exception() is not None
                        for task in meter.audit_tasks
                    ):
                        raise RuntimeError("H5 新会话补采用量审计尚未完整落库")
                usage = await audit.usage_totals_for_task(task_id=record.task_id)
                reason = _h5_usage_budget_error(limits, audited_usage=usage)
                if reason:
                    from services.api.app.application.evidence_recovery_replay import (
                        EvidenceRecoveryReplayError,
                    )

                    error = EvidenceRecoveryReplayError(reason_code=reason, message=reason)
                    error.retryable = False
                    raise error

            runtime = build_default_web_capture_runtime(
                h5_visual_coordinate_mode=settings.workflow.h5_visual_coordinate_runtime_mode,
            )
            drain = build_default_h5_active_session_inspection_drain_service(
                worker_instance_id=worker_instance_id,
                settings=settings,
                supplemental_evidence_policy=SupplementalEvidencePolicy(
                    enabled=True, runtime_ready=True
                ),
                capture_runtime=runtime,
                detector=PersistingH5PageDetector(
                    h5_visual_coordinate_mode=settings.workflow.h5_visual_coordinate_runtime_mode,
                    interaction_pattern_knowledge=PostgresH5InteractionPatternRepository(),
                ),
                recapture_budget=ExplorationBudget(
                    max_actions=work.budget.max_actions,
                    remaining_actions=work.budget.remaining_actions,
                    max_depth=work.budget.max_depth,
                    remaining_duration_ms=min(
                        work.budget.remaining_duration_ms,
                        max(0, int((deadline - datetime.now(UTC)).total_seconds() * 1000)),
                    ),
                ),
                closed_session_checkpoint=checkpoint,
                closed_session_deadline=deadline,
            )
            loop = asyncio.get_running_loop()
            started = last_progress = loop.time()
            with bind_agent_run_context(
                AgentRunContext(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                    task_config=dict(record.task_config or {}),
                    h5_llm_budget=True,
                )
            ):
                while datetime.now(UTC) < deadline and await contexts.has_closed_snapshot_requests(
                    task_id=record.task_id,
                    agent_run_id=record.run_id,
                ):
                    remaining = min(
                        (deadline - datetime.now(UTC)).total_seconds(),
                        DEFAULT_ACTIVE_SESSION_DRAIN_MAX_WAIT_SECONDS - (loop.time() - started),
                        DEFAULT_ACTIVE_SESSION_DRAIN_GRACE_SECONDS - (loop.time() - last_progress),
                    )
                    if remaining <= 0:
                        break
                    async with asyncio.timeout(max(0.001, remaining)):
                        changed = await drain.drain_closed_session_once(
                            task_id=record.task_id,
                            agent_run_id=record.run_id,
                            deadline=deadline,
                        )
                    progressed += int(changed)
                    if changed:
                        last_progress = loop.time()
                    else:
                        await asyncio.sleep(
                            min(
                                max(0.05, settings.inspection_work_items.retry_base_seconds),
                                max(0, remaining),
                            )
                        )
            return progressed
        except TimeoutError:
            # 截止时间中断仍由 finally 回收；后续收口把缺证留下，不伪造补采完成。
            return progressed
        except (Exception, asyncio.CancelledError) as error:
            primary_error = error
            raise
        finally:
            try:
                if drain is not None:
                    try:
                        await drain.close_recapture_sessions()
                    except (Exception, asyncio.CancelledError) as cleanup_error:
                        if primary_error is None:
                            primary_error = cleanup_error
                            raise
                        primary_error.add_note(
                            f"H5 新会话回收失败: {type(cleanup_error).__name__}: {cleanup_error}"
                        )
                        logger.exception("H5 新会话回收失败 task_id=%s", record.task_id)
            finally:
                try:
                    await lock_connection.execute("SELECT pg_advisory_unlock(hashtext($1))", key)
                except (Exception, asyncio.CancelledError) as lock_error:
                    if primary_error is None:
                        raise
                    primary_error.add_note(f"H5 新会话锁释放失败: {type(lock_error).__name__}")
                    logger.exception("H5 新会话锁释放失败 task_id=%s", record.task_id)
