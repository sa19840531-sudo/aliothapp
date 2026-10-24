"""LLM 网关：根据配置构建 Agent Framework 聊天客户端，屏蔽供应商差异。

支持的 provider：
- openai：官方 OpenAI API。
- azure_openai：Azure OpenAI 服务。
- xai / custom：任何兼容 OpenAI 协议的第三方端点，必须显式提供 base_url。

支持的 api_style：
- responses：OpenAI / Azure OpenAI 推荐的新一代 Responses API（``OpenAIChatClient``）。
- chat_completion：传统 Chat Completions API（``OpenAIChatCompletionClient``），
  用于尚未支持 Responses API 的第三方供应商（如 x.ai）。

实现要点（均已通过实测 Agent Framework 1.10.0 确认，详见仓库记忆）：
``OpenAIChatClient``/``OpenAIChatCompletionClient`` 构造函数本身不接受 ``timeout``/
``max_retries`` 关键字参数，因此超时与重试统一由我们自行构造的标准 ``openai`` SDK
异步客户端（``AsyncOpenAI``/``AsyncAzureOpenAI``）承载，再通过 ``async_client=``
参数注入给 Agent Framework 的客户端；传入 ``AsyncAzureOpenAI`` 实例即代表走 Azure
路由，无需重复传递 ``azure_endpoint``/``api_version``。
"""

import json
from collections.abc import Mapping
from typing import Any

import httpx
from agent_framework import Content, SupportsChatGetResponse
from agent_framework.openai import OpenAIChatClient, OpenAIChatCompletionClient
from openai import AsyncAzureOpenAI, AsyncOpenAI, DefaultAsyncHttpxClient

from services.api.app.agents.runtime.usage_meter import (
    record_current_llm_attempt_failure,
    record_current_llm_usage,
)
from services.api.app.application.h5_llm_budget import reserve_current_h5_llm_call
from services.api.app.core.config import LLMSettings
from services.api.app.core.errors import AliothError
from services.api.app.infrastructure.llm_gateway import h5_provider_circuit

_AZURE_PROVIDER = "azure_openai"
_OPENAI_COMPATIBLE_PROVIDERS_REQUIRING_BASE_URL = {"xai", "custom"}


class LLMConfigurationError(AliothError):
    """LLM 网关配置缺失或不受支持时抛出，返回 500（属于服务端配置问题，非调用方输入错误）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message=message, status_code=500)


class _PhysicalRequestMeteredAsyncHttpxClient(DefaultAsyncHttpxClient):
    """在 SDK 重试边界内逐次记录 OpenAI-compatible 物理请求。"""

    def __init__(
        self,
        *,
        usage_source: str,
        usage_operation: str | None,
        usage_scope_type: str | None,
        **kwargs: Any,
    ) -> None:
        self._alioth_usage_source = usage_source
        self._alioth_usage_operation = usage_operation
        self._alioth_usage_scope_type = usage_scope_type
        super().__init__(**kwargs)

    async def send(
        self,
        request: httpx.Request,
        *args: Any,
        **kwargs: Any,
    ) -> httpx.Response:
        if not _is_llm_inference_request(request):
            return await super().send(request, *args, **kwargs)
        ticket = h5_provider_circuit.admit(request)
        try:
            await reserve_current_h5_llm_call()
        except BaseException:
            h5_provider_circuit.finish(ticket, None)
            raise
        metadata = {
            "physical_request": True,
            "request_method": request.method,
            "request_path": request.url.path,
        }
        try:
            response = await super().send(request, *args, **kwargs)
            h5_provider_circuit.finish(ticket, response.status_code)
            if _is_streaming_response(response):
                record_current_llm_usage(
                    {},
                    source=self._alioth_usage_source,
                    operation=self._alioth_usage_operation,
                    scope_type=self._alioth_usage_scope_type,
                    metadata={
                        **metadata,
                        "request_status": (
                            "succeeded" if response.is_success else "failed"
                        ),
                        "usage_status": "unavailable",
                        "http_status_code": response.status_code,
                        "streaming": True,
                    },
                )
                return response
            await response.aread()
        except BaseException as exc:
            h5_provider_circuit.finish(ticket, None)
            record_current_llm_attempt_failure(
                exc,
                source=self._alioth_usage_source,
                operation=self._alioth_usage_operation,
                scope_type=self._alioth_usage_scope_type,
                metadata=metadata,
            )
            raise

        usage = _response_usage(response)
        record_current_llm_usage(
            usage,
            source=self._alioth_usage_source,
            operation=self._alioth_usage_operation,
            scope_type=self._alioth_usage_scope_type,
            metadata={
                **metadata,
                "request_status": "succeeded" if response.is_success else "failed",
                "usage_status": "available" if usage else "unavailable",
                "http_status_code": response.status_code,
            },
        )
        return response


class RequestCountingChatClientMixin:
    """为每次非流式模型请求补充可累计的 request_count 用量。"""

    # 兼容端点在强制 Tool 后转入 auto 模式时容易连续产生界外
    # 调用。Main Agent 已由结构化派发器决定每一个最小工作单元，
    # 因此此类网关客户端每次受监督 Agent 往返只需执行一批 Tool。
    SUPERVISED_SINGLE_TOOL_DISPATCH = True

    def __init__(
        self,
        *args: Any,
        alioth_reasoning_effort: str | None = None,
        alioth_api_style: str = "chat_completion",
        alioth_usage_source: str | None = None,
        alioth_usage_operation: str | None = None,
        alioth_usage_scope_type: str | None = None,
        alioth_usage_metered_by_transport: bool = False,
        **kwargs: Any,
    ) -> None:
        self._alioth_reasoning_effort = alioth_reasoning_effort
        self._alioth_api_style = alioth_api_style
        self._alioth_usage_source = alioth_usage_source
        self._alioth_usage_operation = alioth_usage_operation
        self._alioth_usage_scope_type = alioth_usage_scope_type
        self._alioth_usage_metered_by_transport = alioth_usage_metered_by_transport
        super().__init__(*args, **kwargs)

    def _inner_get_response(
        self,
        *,
        messages: Any,
        options: Any,
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        effective_options = _apply_default_reasoning_effort(
            options,
            api_style=self._alioth_api_style,
            reasoning_effort=self._alioth_reasoning_effort,
        )
        response = super()._inner_get_response(  # type: ignore[misc]
            messages=messages,
            options=effective_options,
            stream=stream,
            **kwargs,
        )
        if stream:
            return response

        async def _record_request_count() -> Any:
            try:
                resolved = await response
            except Exception as exc:
                if not self._alioth_usage_metered_by_transport:
                    record_current_llm_attempt_failure(
                        exc,
                        source=self._alioth_usage_source,
                        operation=self._alioth_usage_operation,
                        scope_type=self._alioth_usage_scope_type,
                    )
                raise
            _suppress_function_calls_when_disabled(
                resolved,
                options=effective_options,
            )
            _enforce_required_function_name(resolved, options=effective_options)
            _normalize_wrapped_function_arguments(
                resolved,
                options=effective_options,
            )
            usage_details = dict(resolved.usage_details or {})
            if not self._alioth_usage_metered_by_transport:
                record_current_llm_usage(
                    usage_details,
                    source=self._alioth_usage_source,
                    operation=self._alioth_usage_operation,
                    scope_type=self._alioth_usage_scope_type,
                    metadata={
                        "request_status": "succeeded",
                        "usage_status": "available" if usage_details else "unavailable",
                    },
                )
            usage_details["request_count"] = int(usage_details.get("request_count") or 0) + 1
            resolved.usage_details = usage_details
            return resolved

        return _record_request_count()


def _apply_default_reasoning_effort(
    options: Mapping[str, Any],
    *,
    api_style: str,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    """按 API 风格注入统一推理强度，同时允许单次调用显式覆盖。"""
    effective_options = dict(options)
    if api_style == "chat_completion":
        if reasoning_effort and effective_options.get("reasoning_effort") is None:
            effective_options["reasoning_effort"] = reasoning_effort
        return effective_options

    explicit_effort = effective_options.pop("reasoning_effort", None)
    selected_effort = explicit_effort or reasoning_effort
    if not selected_effort:
        return effective_options
    reasoning = effective_options.get("reasoning")
    reasoning_config = dict(reasoning) if isinstance(reasoning, Mapping) else {}
    reasoning_config.setdefault("effort", selected_effort)
    effective_options["reasoning"] = reasoning_config
    return effective_options


def _suppress_function_calls_when_disabled(response: Any, *, options: Any) -> None:
    """兼容端点忽略 tool_choice=none 时，在网关边界禁用其函数调用。"""
    if not _function_calls_disabled(options):
        return
    raw_messages = getattr(response, "messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else [raw_messages]
    for message in messages:
        contents = list(getattr(message, "contents", []) or [])
        retained = [
            content
            for content in contents
            if getattr(content, "type", None) != "function_call"
        ]
        if len(retained) == len(contents):
            continue
        if not retained:
            retained.append(
                Content.from_text(
                    "当前派发轮次已禁用 Tool，供应商返回的额外函数"
                    "调用已被网关移除。"
                )
            )
        message.contents = retained


def _function_calls_disabled(options: Any) -> bool:
    """判断框架是否已要求本次模型请求禁用 Tool。"""
    if not isinstance(options, Mapping):
        return False
    tool_choice = options.get("tool_choice")
    if tool_choice == "none":
        return True
    return isinstance(tool_choice, Mapping) and tool_choice.get("mode") == "none"


def _enforce_required_function_name(response: Any, *, options: Any) -> None:
    """在函数调用层前移除供应商返回的非派发 Tool。"""
    required_name = _required_function_name(options)
    if required_name is None:
        return
    raw_messages = getattr(response, "messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else [raw_messages]
    for message in messages:
        contents = list(getattr(message, "contents", []) or [])
        mismatched = [
            content
            for content in contents
            if getattr(content, "type", None) == "function_call"
            and str(getattr(content, "name", "") or "") != required_name
        ]
        if not mismatched:
            continue
        retained = [content for content in contents if content not in mismatched]
        if not any(
            getattr(content, "type", None) == "function_call"
            for content in retained
        ):
            retained.append(
                Content.from_text(
                    "供应商返回了非当前派发 Tool，本轮已受控结束，"
                    "由外层监督器重新派发。"
                )
            )
        message.contents = retained


def _required_function_name(options: Any) -> str | None:
    """读取 Agent 本轮精确派发的函数名。"""
    if not isinstance(options, Mapping):
        return None
    tool_choice = options.get("tool_choice")
    if not isinstance(tool_choice, Mapping) or tool_choice.get("mode") != "required":
        return None
    required_name = str(tool_choice.get("required_function_name") or "").strip()
    return required_name or None


def _normalize_wrapped_function_arguments(response: Any, *, options: Any) -> None:
    """修正部分兼容端点在强制 Tool 时产生的单键标量包装。

    实测供应商会交替返回 ``{"value": ...}``、``{"action": ...}``
    或 ``{"id": ...}``。只对 Tool Schema 明确声明为非 object 的顶层字段
    解包且要求内部值符合 Schema；真正的对象参数保持原样，避免影响
    规则判定中的 ``judge_input`` / ``judge_output``。
    """
    tool_schemas = _tool_parameter_schemas(options)
    raw_messages = getattr(response, "messages", [])
    messages = raw_messages if isinstance(raw_messages, list) else [raw_messages]
    for message in messages:
        for content in getattr(message, "contents", []):
            if getattr(content, "type", None) != "function_call":
                continue
            tool_name = str(getattr(content, "name", "") or "")
            properties = tool_schemas.get(tool_name)
            if properties is None:
                continue
            raw_arguments = getattr(content, "arguments", None)
            try:
                arguments = (
                    json.loads(raw_arguments)
                    if isinstance(raw_arguments, str)
                    else dict(raw_arguments)
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(arguments, dict):
                continue
            changed = False
            for field_name, field_value in list(arguments.items()):
                wrapped_value = _wrapped_scalar_value(field_value)
                if not (
                    wrapped_value is not _NO_WRAPPED_VALUE
                    and _should_unwrap_value_wrapper(
                        properties.get(field_name),
                        wrapped_value,
                    )
                ):
                    continue
                arguments[field_name] = wrapped_value
                changed = True
            if not changed:
                continue
            normalized = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            content.arguments = normalized
            raw_representation = getattr(content, "raw_representation", None)
            if raw_representation is not None and hasattr(raw_representation, "arguments"):
                raw_representation.arguments = normalized


_NO_WRAPPED_VALUE = object()


def _wrapped_scalar_value(value: Any) -> Any:
    """提取兼容端点的标量包装值；非包装返回哨兵。"""
    if not isinstance(value, Mapping):
        return _NO_WRAPPED_VALUE
    if len(value) == 1:
        return next(iter(value.values()))
    # 部分端点会同时输出类型元数据，例如
    # ``{"type":"string","value":"click"}``。只忽略一个 type 元字段，
    # 仍要求剩余业务值唯一，避免对任意对象做模糊转换。
    payload = {key: item for key, item in value.items() if key != "type"}
    if len(payload) == 1 and "type" in value:
        return next(iter(payload.values()))
    return _NO_WRAPPED_VALUE


def _tool_parameter_schemas(options: Any) -> dict[str, Mapping[str, Any]]:
    """提取当前请求中各 Tool 的顶层参数 Schema。"""
    if not isinstance(options, Mapping):
        return {}
    schemas: dict[str, Mapping[str, Any]] = {}
    for item in options.get("tools") or []:
        name = str(getattr(item, "name", "") or "")
        parameters = getattr(item, "parameters", None)
        if not name or not callable(parameters):
            continue
        schema = parameters()
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if isinstance(properties, Mapping):
            schemas[name] = properties
    return schemas


def _should_unwrap_value_wrapper(schema: Any, wrapped_value: Any) -> bool:
    """仅在包装内值符合字段 Schema 时解包；未知 Schema 保持原样。"""
    if not isinstance(schema, Mapping):
        return False
    direct_type = schema.get("type")
    if isinstance(direct_type, str):
        # 非 nullable 的 object 可能合法地只包含业务字段 ``value``，不能解包。
        return direct_type != "object" and _value_matches_json_type(
            wrapped_value, direct_type
        )
    alternatives = schema.get("anyOf")
    return isinstance(alternatives, list) and any(
        isinstance(item, Mapping)
        and isinstance(item.get("type"), str)
        and _value_matches_json_type(wrapped_value, str(item["type"]))
        for item in alternatives
    )


def _value_matches_json_type(value: Any, json_type: str) -> bool:
    """判断 Python 值是否符合常用 JSON Schema 基础类型。"""
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, Mapping)
    if json_type == "null":
        return value is None
    return False


class _RequestCountingOpenAIChatClient(RequestCountingChatClientMixin, OpenAIChatClient):
    pass


class _RequestCountingOpenAIChatCompletionClient(
    RequestCountingChatClientMixin, OpenAIChatCompletionClient
):
    pass


def _build_raw_async_client(
    settings: LLMSettings,
    *,
    usage_source: str | None = None,
    usage_operation: str | None = None,
    usage_scope_type: str | None = None,
) -> AsyncOpenAI | AsyncAzureOpenAI:
    """根据 provider 构建底层 openai SDK 异步客户端，统一承载 timeout/max_retries/鉴权信息。"""
    if not settings.api_key:
        raise LLMConfigurationError(
            "必须配置 ALIOTH_LLM_API_KEY，否则无法构建 LLM 客户端（不依赖 openai 包自身的 "
            "OPENAI_API_KEY 环境变量兜底，避免鉴权来源不透明）。"
        )

    common_kwargs: dict = {
        "api_key": settings.api_key,
        "timeout": settings.timeout_seconds,
        "max_retries": settings.max_retries,
    }
    if usage_source:
        common_kwargs["http_client"] = _PhysicalRequestMeteredAsyncHttpxClient(
            usage_source=usage_source,
            usage_operation=usage_operation,
            usage_scope_type=usage_scope_type,
        )

    if settings.provider == _AZURE_PROVIDER:
        if not settings.azure_endpoint:
            raise LLMConfigurationError(
                "provider=azure_openai 时必须配置 ALIOTH_LLM_AZURE_ENDPOINT"
            )
        if not settings.azure_api_version:
            raise LLMConfigurationError(
                "provider=azure_openai 时必须配置 ALIOTH_LLM_AZURE_API_VERSION（不依赖 "
                "openai 包自身的 OPENAI_API_VERSION 环境变量兜底，避免版本来源不透明）。"
            )
        return AsyncAzureOpenAI(
            azure_endpoint=settings.azure_endpoint,
            api_version=settings.azure_api_version,
            **common_kwargs,
        )

    requires_base_url = settings.provider in _OPENAI_COMPATIBLE_PROVIDERS_REQUIRING_BASE_URL
    if requires_base_url and not settings.base_url:
        raise LLMConfigurationError(
            f"provider={settings.provider} 时必须配置 ALIOTH_LLM_BASE_URL"
        )

    if settings.base_url:
        common_kwargs["base_url"] = settings.base_url
    return AsyncOpenAI(**common_kwargs)


def build_chat_client(
    settings: LLMSettings,
    *,
    usage_source: str,
    usage_operation: str | None = None,
    usage_scope_type: str | None = None,
) -> SupportsChatGetResponse:
    """根据 LLMSettings 构建可直接传给 ``Agent(client=...)`` 的聊天客户端。"""
    raw_client = _build_raw_async_client(
        settings,
        usage_source=usage_source,
        usage_operation=usage_operation,
        usage_scope_type=usage_scope_type,
    )
    client_kwargs: dict = {
        "model": settings.model,
        "async_client": raw_client,
        "alioth_reasoning_effort": settings.reasoning_effort,
        "alioth_api_style": settings.api_style,
        "alioth_usage_source": usage_source,
        "alioth_usage_operation": usage_operation,
        "alioth_usage_scope_type": usage_scope_type,
        "alioth_usage_metered_by_transport": True,
    }
    if settings.api_style == "chat_completion":
        return _RequestCountingOpenAIChatCompletionClient(**client_kwargs)
    return _RequestCountingOpenAIChatClient(**client_kwargs)


def _is_llm_inference_request(request: httpx.Request) -> bool:
    path = request.url.path.rstrip("/").casefold()
    return request.method.upper() == "POST" and (
        path.endswith("/chat/completions") or path.endswith("/responses")
    )


def _is_streaming_response(response: httpx.Response) -> bool:
    return "text/event-stream" in response.headers.get("content-type", "").casefold()


def _response_usage(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(payload, Mapping):
        return {}
    usage = payload.get("usage")
    return dict(usage) if isinstance(usage, Mapping) else {}
