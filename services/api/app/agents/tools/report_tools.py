"""报告生成 Tool：汇总当前任务的规则执行结果，形成结构化质检报告。"""

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from services.api.app.agents.runtime.run_context import require_agent_run_context
from services.api.app.agents.tools.registry import PermissionLevel, tool
from services.api.app.application.report_generation import ReportGenerationService
from services.api.app.application.task_query import TaskQueryService
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository


@lru_cache
def _get_report_generation_service() -> ReportGenerationService:
    return ReportGenerationService(
        query_service=TaskQueryService(repository=PostgresTaskRepository())
    )


async def generate_report_for_current_run(*, service: Any | None = None) -> dict:
    """生成当前 Agent Run 绑定任务的结构化报告。"""
    context = require_agent_run_context()
    report_service = service if service is not None else _get_report_generation_service()
    return await report_service.generate_task_report(
        task_id=context.task_id,
        agent_run_id=context.agent_run_id,
    )


async def publish_report_for_current_run(
    *,
    publish_channel: str = "workstation",
    report_version: str | None = None,
    operator_note: str | None = None,
) -> dict[str, Any]:
    """占位版最终发布：返回结构化发布回执，不做真实落库与外部投递。"""
    context = require_agent_run_context()
    return {
        "published": False,
        "publish_status": "placeholder_only",
        "task_id": context.task_id,
        "agent_run_id": context.agent_run_id,
        "publish_channel": publish_channel,
        "report_version": report_version,
        "operator_note": operator_note,
        "published_at": datetime.now(UTC).isoformat(),
        "message": "占位发布成功：已通过发布门禁校验，但尚未接入真实发布落库链路。",
    }


@tool(
    name="report_generation_tool",
    description="汇总当前 Agent Run 绑定任务的规则执行结果，生成结构化质检报告。",
    category="报告",
    permission_level=PermissionLevel.SAFE,
)
async def report_generation_tool() -> dict:
    """Agent 可调用的任务级报告生成入口。"""
    return await generate_report_for_current_run()


@tool(
    name="report_publish_tool",
    description="执行报告最终发布动作（占位实现：仅返回发布回执，不做真实发布落库）。",
    category="报告",
    permission_level=PermissionLevel.SENSITIVE,
)
async def report_publish_tool(
    publish_channel: str = "workstation",
    report_version: str | None = None,
    operator_note: str | None = None,
) -> dict[str, Any]:
    """Agent 可调用的报告发布入口（占位实现）。"""
    return await publish_report_for_current_run(
        publish_channel=publish_channel,
        report_version=report_version,
        operator_note=operator_note,
    )
