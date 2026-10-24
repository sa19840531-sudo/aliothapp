"""测试 LLM 网关客户端工厂：provider/api_style、物理请求计量与配置校验。

仅验证客户端"能否被正确构造"（不发起真实网络请求），api_key 使用假字符串即可
（openai SDK 在构造阶段只校验是否提供了非空字符串，不会在此阶段发起网络调用）。
"""

import asyncio
import json

import httpx
import pytest
from agent_framework import ChatResponse, Content, Message
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient

from services.api.app.agents.runtime.usage_meter import bind_run_llm_usage_meter
from services.api.app.agents.tools.page_capture_tools import page_capture_tool
from services.api.app.core.config import LLMSettings
from services.api.app.infrastructure.llm_gateway.client_factory import (
    LLMConfigurationError,
    _apply_default_reasoning_effort,
    _enforce_required_function_name,
    _normalize_wrapped_function_arguments,
    _PhysicalRequestMeteredAsyncHttpxClient,
    _suppress_function_calls_when_disabled,
    build_chat_client,
)


def _build_test_chat_client(settings: LLMSettings):
    return build_chat_client(settings, usage_source="test_client_factory")


def _settings(**overrides: object) -> LLMSettings:
    defaults: dict[str, object] = {"api_key": "sk-fake-key-for-tests"}
    defaults.update(overrides)
    return LLMSettings(**defaults)  # type: ignore[arg-type]


def test_missing_api_key_raises_configuration_error() -> None:
    settings = LLMSettings(api_key=None)

    with pytest.raises(LLMConfigurationError, match="ALIOTH_LLM_API_KEY"):
        _build_test_chat_client(settings)


def test_azure_provider_without_endpoint_raises_configuration_error() -> None:
    settings = _settings(provider="azure_openai", azure_api_version="2024-06-01")

    with pytest.raises(LLMConfigurationError, match="ALIOTH_LLM_AZURE_ENDPOINT"):
        _build_test_chat_client(settings)


def test_azure_provider_without_api_version_raises_configuration_error() -> None:
    settings = _settings(provider="azure_openai", azure_endpoint="https://example.openai.azure.com")

    with pytest.raises(LLMConfigurationError, match="ALIOTH_LLM_AZURE_API_VERSION"):
        _build_test_chat_client(settings)


def test_xai_provider_without_base_url_raises_configuration_error() -> None:
    settings = _settings(provider="xai")

    with pytest.raises(LLMConfigurationError, match="ALIOTH_LLM_BASE_URL"):
        _build_test_chat_client(settings)


def test_custom_provider_without_base_url_raises_configuration_error() -> None:
    settings = _settings(provider="custom")

    with pytest.raises(LLMConfigurationError, match="ALIOTH_LLM_BASE_URL"):
        _build_test_chat_client(settings)


def test_default_responses_api_style_builds_openai_chat_client() -> None:
    settings = _settings()

    client = _build_test_chat_client(settings)

    assert isinstance(client, OpenAIChatClient)


def test_chat_completion_api_style_builds_chat_completion_client() -> None:
    settings = _settings(api_style="chat_completion", reasoning_effort="low")

    client = _build_test_chat_client(settings)

    assert isinstance(client, OpenAIChatCompletionClient)
    assert client.SUPERVISED_SINGLE_TOOL_DISPATCH is True
    assert client._alioth_reasoning_effort == "low"


def test_reasoning_effort_uses_api_specific_shape_and_allows_override() -> None:
    assert _apply_default_reasoning_effort(
        {},
        api_style="chat_completion",
        reasoning_effort="low",
    ) == {"reasoning_effort": "low"}
    assert _apply_default_reasoning_effort(
        {"reasoning_effort": "high"},
        api_style="chat_completion",
        reasoning_effort="low",
    ) == {"reasoning_effort": "high"}
    assert _apply_default_reasoning_effort(
        {},
        api_style="responses",
        reasoning_effort="low",
    ) == {"reasoning": {"effort": "low"}}
    assert _apply_default_reasoning_effort(
        {"reasoning": {"effort": "medium"}, "reasoning_effort": "high"},
        api_style="responses",
        reasoning_effort="low",
    ) == {"reasoning": {"effort": "medium"}}


def test_azure_provider_with_endpoint_and_api_version_succeeds() -> None:
    settings = _settings(
        provider="azure_openai",
        azure_endpoint="https://example.openai.azure.com",
        azure_api_version="2024-06-01",
    )

    client = _build_test_chat_client(settings)

    assert isinstance(client, OpenAIChatClient)


def test_xai_provider_with_base_url_succeeds() -> None:
    settings = _settings(provider="xai", base_url="https://api.x.ai/v1")

    client = _build_test_chat_client(settings)

    assert isinstance(client, OpenAIChatClient)


@pytest.mark.asyncio
async def test_physical_request_meter_records_success_usage_once() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 30,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 20},
                }
            },
            request=request,
        )

    client = _PhysicalRequestMeteredAsyncHttpxClient(
        usage_source="page_detection",
        usage_operation="page_type_detection",
        usage_scope_type="task_run",
        transport=httpx.MockTransport(handler),
    )
    with bind_run_llm_usage_meter() as meter:
        response = await client.post("https://example.com/v1/chat/completions", json={})
    await client.aclose()

    assert response.status_code == 200
    snapshot = meter.snapshot()
    assert snapshot is not None
    assert snapshot.ai_calls == 1
    assert snapshot.input_tokens == 120
    assert snapshot.output_tokens == 30
    assert snapshot.cache_hit_tokens == 20
    assert snapshot.total_tokens == 150


@pytest.mark.asyncio
async def test_physical_request_meter_records_each_sdk_retry_attempt() -> None:
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                503,
                headers={"content-type": "application/json"},
                json={"error": {"message": "overloaded"}},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"usage": {"prompt_tokens": 20, "completion_tokens": 5}},
            request=request,
        )

    http_client = _PhysicalRequestMeteredAsyncHttpxClient(
        usage_source="page_detection",
        usage_operation="page_type_detection",
        usage_scope_type="task_run",
        transport=httpx.MockTransport(handler),
    )
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        api_key="sk-test",
        base_url="https://example.com/v1",
        max_retries=2,
        http_client=http_client,
    )
    with bind_run_llm_usage_meter() as meter:
        response = await client.chat.completions.create(
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
        )
    await client.close()

    assert response.usage is not None
    assert attempts == 3
    snapshot = meter.snapshot()
    assert snapshot is not None
    assert snapshot.ai_calls == 3
    assert snapshot.input_tokens == 20
    assert snapshot.output_tokens == 5
    assert snapshot.total_tokens == 25


@pytest.mark.asyncio
async def test_physical_request_meter_records_hard_timeout_cancellation() -> None:
    started = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("不会执行到这里")

    client = _PhysicalRequestMeteredAsyncHttpxClient(
        usage_source="page_detection",
        usage_operation="page_type_detection",
        usage_scope_type="task_run",
        transport=httpx.MockTransport(handler),
    )
    with bind_run_llm_usage_meter() as meter:
        task = asyncio.create_task(
            client.post("https://example.com/v1/chat/completions", json={})
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await client.aclose()

    snapshot = meter.snapshot()
    assert snapshot is not None
    assert snapshot.ai_calls == 1
    assert snapshot.input_tokens is None
    assert snapshot.output_tokens is None
    assert snapshot.total_tokens is None


def test_scalar_function_arguments_are_unwrapped_by_tool_schema() -> None:
    content = Content.from_function_call(
        call_id="call-1",
        name="page_capture_tool",
        arguments=json.dumps(
            {
                "mode": "goto",
                "url": {"value": "https://example.com/"},
                "navigation_depth": {"value": 0},
                "trigger_action": {"type": "string", "value": "click"},
                "exploration_action_id": {"id": "exploration-1"},
            }
        ),
    )
    response = ChatResponse(
        messages=Message(role="assistant", contents=[content])
    )

    _normalize_wrapped_function_arguments(
        response,
        options={"tools": [page_capture_tool]},
    )

    assert json.loads(content.arguments) == {
        "mode": "goto",
        "url": "https://example.com/",
        "navigation_depth": 0,
        "trigger_action": "click",
        "exploration_action_id": "exploration-1",
    }


def test_non_nullable_object_value_field_is_not_unwrapped() -> None:
    class ObjectTool:
        name = "object_tool"

        @staticmethod
        def parameters() -> dict:
            return {
                "type": "object",
                "properties": {
                    "judge_input": {
                        "type": "object",
                        "additionalProperties": True,
                    }
                },
            }

    content = Content.from_function_call(
        call_id="call-2",
        name="object_tool",
        arguments='{"judge_input":{"value":"业务原值"}}',
    )
    response = ChatResponse(
        messages=Message(role="assistant", contents=[content])
    )

    _normalize_wrapped_function_arguments(
        response,
        options={"tools": [ObjectTool()]},
    )

    assert json.loads(content.arguments) == {
        "judge_input": {"value": "业务原值"}
    }


def test_mismatched_required_function_is_replaced_with_controlled_text() -> None:
    message = Message(
        role="assistant",
        contents=[
            Content.from_function_call(
                call_id="call-wrong",
                name="load_skill",
                arguments='{"name":"qc-workflow"}',
            )
        ],
    )
    response = ChatResponse(messages=message)

    _enforce_required_function_name(
        response,
        options={
            "tool_choice": {
                "mode": "required",
                "required_function_name": "record_rule_execution_tool",
            }
        },
    )

    assert [content.type for content in message.contents] == ["text"]
    assert "外层监督器重新派发" in (message.contents[0].text or "")


def test_required_function_is_retained_while_parallel_mismatch_is_removed() -> None:
    expected = Content.from_function_call(
        call_id="call-expected",
        name="record_rule_execution_tool",
        arguments="{}",
    )
    message = Message(
        role="assistant",
        contents=[
            expected,
            Content.from_function_call(
                call_id="call-wrong",
                name="report_generation_tool",
                arguments="{}",
            ),
        ],
    )
    response = ChatResponse(messages=message)

    _enforce_required_function_name(
        response,
        options={
            "tool_choice": {
                "mode": "required",
                "required_function_name": "record_rule_execution_tool",
            }
        },
    )

    assert message.contents == [expected]


@pytest.mark.parametrize(
    "tool_choice",
    ["none", {"mode": "none"}],
)
def test_function_calls_are_removed_when_framework_disables_tools(
    tool_choice: object,
) -> None:
    message = Message(
        role="assistant",
        contents=[
            Content.from_function_call(
                call_id="call-ignored-none",
                name="page_capture_tool",
                arguments='{"mode":"goto"}',
            )
        ],
    )
    response = ChatResponse(messages=message)

    _suppress_function_calls_when_disabled(
        response,
        options={"tool_choice": tool_choice},
    )

    assert [content.type for content in message.contents] == ["text"]
    assert "额外函数调用已被网关移除" in (message.contents[0].text or "")
