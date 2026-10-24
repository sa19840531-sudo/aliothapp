"""DevUI 独立调试入口：通过 Alioth API 驱动 Worker，不改变正式执行链路。"""

from agent_framework import Agent

from services.api.app.core.config import get_settings
from services.api.app.infrastructure.llm_gateway.client_factory import build_chat_client

from .tools import get_rule_executions, get_run_status, get_task_detail, submit_url_task

_INSTRUCTIONS = """
你是 Alioth DevUI 开发调试入口，只用于本地或内网开发调试。

你的职责：
1. 用户给出 URL 后，调用 submit_url_task 提交正式质检任务。
2. 拿到 task_id 与 run_id 后，调用 get_run_status 查询 Worker 执行状态和 action_trace。
3. 需要查看业务任务详情时，调用 get_task_detail。
4. 需要查看规则执行结果时，调用 get_rule_executions。

边界：
- 你不能绕过 Alioth API 直接执行质检逻辑。
- 你不能直接调用数据库、Redis 或 Worker 内部函数。
- 你不能把 DevUI 当作生产入口。
- 如果 action_trace 为空，说明 Worker 尚未写回或 Run 未结束，应提示用户稍后再查。
"""

_settings = get_settings()

agent = Agent(
    name="alioth_worker_debug",
    client=build_chat_client(
        _settings.llm,
        usage_source="devui_debug_agent",
        usage_operation="debug_agent_response",
        usage_scope_type="debug",
    ),
    instructions=_INSTRUCTIONS,
    tools=[submit_url_task, get_run_status, get_task_detail, get_rule_executions],
)
