"""假 ChatClient 与假响应构造工具，供 agents/runtime 相关测试复用。

不发起任何真实网络调用；通过继承 ``FunctionInvocationLayer, ChatMiddlewareLayer,
BaseChatClient``（与真实 ``OpenAIChatClient`` 相同的 MRO，去掉遥测层）来获得完整的
工具调用循环能力（``function_invocation_configuration``、自动多轮工具调用），
详见仓库记忆中的实测结论。
"""

from collections.abc import Sequence
from typing import Any

from agent_framework import (
    BaseChatClient,
    ChatMiddlewareLayer,
    ChatResponse,
    Content,
    FunctionInvocationLayer,
    Message,
)


def make_function_call_response(call_id: str, name: str, arguments: str) -> ChatResponse:
    """构造一条"模型请求调用某个工具"的假响应。"""
    return ChatResponse(
        messages=Message(
            role="assistant",
            contents=[Content.from_function_call(call_id=call_id, name=name, arguments=arguments)],
        )
    )


def make_text_response(text: str) -> ChatResponse:
    """构造一条"模型直接给出最终文本"的假响应。"""
    return ChatResponse(messages=Message(role="assistant", contents=[text]))


class ScriptedChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """按预设脚本依次返回响应的假 ChatClient。

    正确遵守 ``tool_choice="none"`` 约束（模拟真实 OpenAI/Azure OpenAI API 行为）：
    一旦框架因命中软上限而要求 ``tool_choice="none"``，本客户端会自动改为返回一条
    收尾文本，而不是继续请求工具调用。
    """

    def __init__(self, responses: Sequence[ChatResponse]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._index = 0
        self.received_messages: list[list[Message]] = []
        self.received_options: list[dict[str, Any]] = []

    async def _inner_get_response(
        self, *, messages: Any, stream: Any, options: Any, **kwargs: Any
    ) -> ChatResponse:
        self.received_messages.append(list(messages))
        self.received_options.append(dict(options))
        if options.get("tool_choice") == "none":
            return make_text_response("已达上限，自动收尾。")
        response = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return response


class NonCompliantChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """不遵守 ``tool_choice="none"`` 约束的假 ChatClient，模拟部分第三方供应商。

    用于验证自定义硬护栏（``LoopGuardMiddleware``）在框架自带软上限失效时仍能兜底。
    """

    def __init__(self, responses: Sequence[ChatResponse]) -> None:
        super().__init__()
        self._responses = list(responses)
        self._index = 0

    async def _inner_get_response(
        self, *, messages: Any, stream: Any, options: Any, **kwargs: Any
    ) -> ChatResponse:
        response = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return response


class HangingChatClient(FunctionInvocationLayer, ChatMiddlewareLayer, BaseChatClient):
    """永远不返回的假 ChatClient，用于测试墙钟超时。"""

    async def _inner_get_response(
        self, *, messages: Any, stream: Any, options: Any, **kwargs: Any
    ) -> ChatResponse:
        import asyncio

        await asyncio.sleep(3600)
        return make_text_response("never")
