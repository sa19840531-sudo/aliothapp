"""结果发布条件校验：报告始终允许，真正发布动作才做门禁。"""

import logging
from collections.abc import Awaitable, Callable

from agent_framework import FunctionInvocationContext, FunctionMiddleware

from services.api.app.agents.runtime.working_memory import WorkingMemory

logger = logging.getLogger(__name__)


class PublishConditionMiddleware(FunctionMiddleware):
    """发布前检查是否至少产生过一条规则执行写入。

    契约说明：
    1. ``report_generation_tool`` 属于报告产出动作，不是最终发布动作，必须始终允许。
       这样在成功、失败、取消、异常、无规则结果写入等场景下都能拿到结构化报告。
    2. 真正的“对外发布/落库定版”动作（如未来的 ``report_publish_tool``）才做前置门禁。
    """

    def __init__(
        self,
        working_memory: WorkingMemory,
        *,
        publish_tool: str = "report_publish_tool",
        required_tool: str = "record_rule_execution_tool",
        batch_required_tool: str = "record_rule_executions_batch_tool",
        report_tool: str = "report_generation_tool",
    ) -> None:
        self._working_memory = working_memory
        self._publish_tool = publish_tool
        self._required_tools = {required_tool, batch_required_tool}
        self._report_tool = report_tool
        self._has_required_tool_success = False

    async def process(
        self, context: FunctionInvocationContext, call_next: Callable[[], Awaitable[None]]
    ) -> None:
        if context.function.name in self._required_tools:
            await call_next()
            if _write_succeeded(context.result):
                self._has_required_tool_success = True
            return

        # 页面工作仍在运行时禁止提前生成报告；页面终态收口后仍始终允许报告产出。
        if context.function.name == self._report_tool:
            if self._working_memory.has_runnable_work():
                context.result = {
                    "execution_status": "blocked",
                    "error_code": "PAGE_EXPLORATION_PENDING",
                    "retryable": True,
                    "message": "页面识别、规则执行或必执行探索动作尚未收敛，暂不能生成报告。",
                }
                return
            if not self._has_required_tool_success:
                logger.info(
                    "发布条件提示：当前无规则写入成功记录，仍放行报告生成。"
                )
            await call_next()
            return

        if context.function.name != self._publish_tool:
            await call_next()
            return

        if not self._has_required_tool_success:
            context.result = {
                "execution_status": "blocked",
                "error_code": "REPORT_PUBLISH_CONDITION_UNMET",
                "retryable": False,
                "missing_fields": ["rule_execution"],
                "message": "当前运行尚未记录任何规则执行结果，暂不允许发布报告。",
            }
            return

        await call_next()


def _write_succeeded(result: object) -> bool:
    if result is None:
        return True
    if not isinstance(result, dict):
        return True
    if result.get("execution_status") == "blocked":
        return False
    if "succeeded_count" in result:
        return int(result.get("succeeded_count") or 0) > 0
    return True
