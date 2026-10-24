"""旧 Web Run 的只终止对账，不装配任何浏览器或正式巡检。"""

from datetime import UTC, datetime

from services.api.app.domain.exploration.web_runtime_contract import (
    WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED,
    is_legacy_web_execution,
)
from services.api.app.infrastructure.queue.redis_queue import AgentRunQueueStatus


async def reconcile_legacy_web_run(queue, state_store, record) -> None:
    """长期非终态记录是重试来源；终态事实已提交时不重复写入。"""
    finished = await state_store.get_finished_record(run_id=record.run_id)
    if finished is not None:
        return
    if await state_store.is_cancel_requested(record.run_id):
        return
    current = await queue.get(record.run_id)
    if current is not None:
        if not is_legacy_web_execution(current.task_config):
            return
        if current.status in {AgentRunQueueStatus.QUEUED, AgentRunQueueStatus.RUNNING}:
            await queue.claim_running(current, worker_instance_id=None)
            current = await queue.get(record.run_id)
        if current is None or current.error_message != WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED:
            return
        record = current
    else:
        record = record.model_copy(deep=True)
        record.status = AgentRunQueueStatus.FAILED
        record.finished_at = datetime.now(UTC)
        record.termination_reason = "error"
        record.error_message = WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED
        record.error_category = "infrastructure"
        record.exploration_status = "failed"
        record.evidence_gap = [WEB_LEGACY_RUN_EXECUTION_NOT_SUPPORTED]
        record.worker_instance_id = None
    await state_store.record_finished(record)
