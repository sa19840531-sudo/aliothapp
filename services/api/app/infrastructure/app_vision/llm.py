"""基于 LLM 多模态的 App Screenshot Understanding Provider。"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from openai import AsyncAzureOpenAI, AsyncOpenAI

from services.api.app.agents.runtime.usage_meter import (
    normalize_llm_usage_tokens,
    record_current_llm_attempt_failure,
    record_current_llm_usage,
    record_current_openai_compatible_response_usage,
)
from services.api.app.application.app_screenshot_understanding import (
    UnderstandAppScreenshotRequest,
)
from services.api.app.core.config import LLMSettings, get_settings
from services.api.app.domain.exploration import (
    AppVisualAnalysis,
    AppVisualCandidate,
    AppVisualValidationError,
    NormalizedBounds,
)
from services.api.app.domain.exploration.app_page_identity import (
    APP_VISUAL_PAGE_NAME_FACT_KEY,
    build_visual_page_name_fact,
)
from services.api.app.domain.exploration.app_visual import APP_VISUAL_CONTROL_ROLES
from services.api.app.domain.exploration.app_visual_page_identity import (
    build_app_visual_page_facts,
)
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.page.registry import (
    PageTypeRegistry,
    get_default_page_type_registry,
)
from services.api.app.infrastructure.app_vision.heuristic import (
    build_first_screen_escape_analysis,
    ensure_exploration_candidates,
)
from services.api.app.infrastructure.app_vision.image_codec import (
    compress_png_bytes_for_llm,
    resolve_app_vision_jpeg_quality,
)
from services.api.app.infrastructure.app_vision.page_structure_prompt import (
    APP_PAGE_STRUCTURE_PROMPT,
)
from services.api.app.infrastructure.llm_gateway.client_factory import (
    LLMConfigurationError,
    _build_raw_async_client,
)

logger = logging.getLogger(__name__)

PROVIDER_ID = "llm-app-vision"
APP_VISION_PROVIDER_LATENCY_VERSION = "app-vision-provider-latency.v1"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# 与 Main Agent 供应商重试对齐：408／429／5xx 与连接超时有界重试后再降级。
_VISION_PROVIDER_MAX_RETRIES = 2
_VISION_PROVIDER_RETRY_BACKOFF_SECONDS = 0.5
_VISION_PROVIDER_OVERLOAD_BACKOFF_SECONDS = 2.0
_RETRYABLE_VISION_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_JSON_OBJECT_PATTERN = re.compile(r"\{[\s\S]*\}")
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens"})
_MODERN_CHAT_TOKEN_MODELS = ("gpt-5", "o1", "o3", "o4")
_UNSUPPORTED_OUTPUT_BUDGET_MARKERS = (
    "unsupported",
    "not supported",
    "does not support",
    "unknown parameter",
    "unrecognized parameter",
    "invalid parameter",
)
_UNSUPPORTED_STREAM_MARKERS = (
    "stream",
    "streaming",
    "stream_options",
)
_ACTION_ALIASES = {
    "click": InteractionOperation.CLICK,
    "tap": InteractionOperation.CLICK,
    "scroll": InteractionOperation.SCROLL,
    "swipe": InteractionOperation.SCROLL,
    "back": InteractionOperation.BACK,
    "observe": InteractionOperation.OBSERVE,
}
_RISK_ALIASES = {
    "low": InteractionRiskLevel.LOW,
    "medium": InteractionRiskLevel.MEDIUM,
    "med": InteractionRiskLevel.MEDIUM,
    "high": InteractionRiskLevel.HIGH,
}


@dataclass
class AppVisionProviderCallTrace:
    """单次 app_vision Provider 调用的可聚合 latency 审计。"""

    capture_id: str
    request_started_at: str
    local_prep_ms: int = 0
    request_start_ms: int = 0
    first_token_ms: int | None = None
    completed_ms: int = 0
    parse_ms: int = 0
    retry_count: int = 0
    attempt_count: int = 0
    stream_mode: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    error_code: str | None = None
    error_type: str | None = None
    finish_reason: str | None = None
    attempt_errors: list[dict[str, object]] = field(default_factory=list)

    def to_latency_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "protocol_version": APP_VISION_PROVIDER_LATENCY_VERSION,
            "capture_id": self.capture_id,
            "request_started_at": self.request_started_at,
            "local_prep_ms": int(self.local_prep_ms),
            "request_start_ms": int(self.request_start_ms),
            "first_token_ms": self.first_token_ms,
            "completed_ms": int(self.completed_ms),
            "parse_ms": int(self.parse_ms),
            "provider_elapsed_ms": int(self.completed_ms),
            "retry_count": int(self.retry_count),
            "attempt_count": int(self.attempt_count),
            "stream_mode": bool(self.stream_mode),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "error_code": self.error_code,
            "error_type": self.error_type,
            "finish_reason": self.finish_reason,
            "attempt_errors": list(self.attempt_errors),
        }
        return payload


@dataclass(frozen=True, kw_only=True)
class _AssembledChatCompletion:
    """把流式／非流式响应统一成可解析对象。"""

    content: str
    finish_reason: str | None
    usage: Mapping[str, Any] | None
    raw_response: Any
    first_token_ms: int | None
    completed_ms: int
    stream_mode: bool


class LlmAppVisionProvider:
    """把 App 截图交给多模态 LLM，产出有界 page_type 与动作候选。"""

    def __init__(
        self,
        *,
        llm_settings: LLMSettings | None = None,
        page_types: PageTypeRegistry | None = None,
        client_factory: Callable[[LLMSettings], AsyncOpenAI | AsyncAzureOpenAI] | None = None,
        max_image_side: int = 720,
        jpeg_quality: int | None = None,
        temperature: float = 0.1,
        max_tokens: int = 2400,
    ) -> None:
        if (
            isinstance(max_image_side, bool)
            or not isinstance(max_image_side, int)
            or max_image_side <= 0
        ):
            raise ValueError("max_image_side 必须为正整数")
        if (
            isinstance(temperature, bool)
            or not isinstance(temperature, (int, float))
            or not 0 <= float(temperature) <= 2
        ):
            raise ValueError("temperature 必须在 0 到 2 之间")
        if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("max_tokens 必须为正整数")
        self._llm_settings = llm_settings
        self._page_types = page_types or get_default_page_type_registry()
        self._client_factory = client_factory or _build_raw_async_client
        self._max_image_side = max_image_side
        # 分辨率策略不变；仅压 JPEG 质量以降低视觉调用体积
        self._jpeg_quality = resolve_app_vision_jpeg_quality(jpeg_quality)
        self._temperature = float(temperature)
        self._max_tokens = max_tokens

    async def understand(
        self,
        request: UnderstandAppScreenshotRequest,
    ) -> AppVisualAnalysis:
        if not isinstance(request, UnderstandAppScreenshotRequest):
            raise TypeError("request 必须为 UnderstandAppScreenshotRequest")

        settings = self._llm_settings or get_settings().llm
        wall_started = time.perf_counter()
        request_started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        trace = AppVisionProviderCallTrace(
            capture_id=request.capture_id,
            request_started_at=request_started_at,
        )

        if not _is_valid_png_bytes(request.screenshot_png):
            logger.warning("Screenshot Understanding 收到无效 PNG，跳过 LLM 调用并降级探索兜底")
            trace.local_prep_ms = int((time.perf_counter() - wall_started) * 1000)
            trace.error_code = "invalid_screenshot_bytes"
            trace.error_type = "InvalidPngBytes"
            return _finalize_traced_analysis(
                _degraded_analysis(
                    request,
                    model_id=settings.model,
                    error_code="invalid_screenshot_bytes",
                    error_type="InvalidPngBytes",
                    retryable=True,
                    reason="截图字节不是有效 PNG，已降级继续探索",
                ),
                trace=trace,
                record_usage=False,
            )

        try:
            client = self._client_factory(settings)
        except LLMConfigurationError as exc:
            logger.warning("Screenshot Understanding LLM 未配置，记录降级事实：%s", exc)
            trace.local_prep_ms = int((time.perf_counter() - wall_started) * 1000)
            trace.error_code = "provider_configuration_failed"
            trace.error_type = type(exc).__name__
            return _finalize_traced_analysis(
                _degraded_analysis(
                    request,
                    model_id=settings.model,
                    error_code="provider_configuration_failed",
                    error_type=type(exc).__name__,
                    retryable=False,
                    reason="LLM 未配置，已降级继续探索",
                ),
                trace=trace,
                record_usage=False,
            )

        image_bytes, mime = compress_png_bytes_for_llm(
            request.screenshot_png,
            max_side=self._max_image_side,
            quality=self._jpeg_quality,
        )
        data_url = f"data:{mime};base64,{base64.b64encode(image_bytes).decode('ascii')}"
        allowed = tuple(item.value for item in request.allowed_actions)
        # APP 与 H5 共享唯一 PageType Catalog。生命周期页同样需要被识别，
        # 只是后续 Assessment 按 Catalog 属性决定是否进入业务覆盖和规则匹配。
        page_codes = list(self._page_types.all_codes)
        system_prompt = _system_prompt(
            page_codes=page_codes,
            allowed_actions=allowed,
            max_candidates=request.max_candidates,
            page_types=self._page_types,
        )
        user_prompt = _user_prompt(request)

        completion_kwargs = {
            "model": settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": data_url},
                        },
                    ],
                },
            ],
            "temperature": self._temperature,
        }
        completion_kwargs.update(
            _chat_completion_output_budget_kwargs(
                model=settings.model,
                max_tokens=self._max_tokens,
            )
        )
        trace.local_prep_ms = int((time.perf_counter() - wall_started) * 1000)
        provider_started = time.perf_counter()
        trace.request_start_ms = int((provider_started - wall_started) * 1000)
        try:
            assembled = await _create_chat_completion_with_retry(
                client=client,
                completion_kwargs=completion_kwargs,
                trace=trace,
            )
        except Exception as exc:
            logger.warning(
                "Screenshot Understanding LLM 调用失败，降级探索兜底：%s: %s",
                type(exc).__name__,
                exc,
            )
            trace.completed_ms = int((time.perf_counter() - provider_started) * 1000)
            trace.error_code = "provider_call_failed"
            trace.error_type = type(exc).__name__
            return _finalize_traced_analysis(
                _degraded_analysis(
                    request,
                    model_id=settings.model,
                    error_code="provider_call_failed",
                    error_type=type(exc).__name__,
                    retryable=True,
                    reason=f"LLM 调用失败（{type(exc).__name__}），已降级继续探索",
                ),
                trace=trace,
                record_usage=False,
            )

        _apply_usage_to_trace(trace, assembled.usage)
        record_current_openai_compatible_response_usage(
            assembled.raw_response,
            source="app_vision",
            operation="screenshot_understanding",
            metadata={"app_vision_latency": trace.to_latency_dict()},
        )

        finish_reason = assembled.finish_reason
        if finish_reason in _TRUNCATED_FINISH_REASONS:
            # 截断响应不能尝试补全或从半截 JSON 中提取候选，避免把不完整坐标当成事实。
            logger.warning(
                "Screenshot Understanding LLM 输出被截断，降级探索兜底：finish_reason=%s",
                finish_reason,
            )
            trace.error_code = "provider_response_truncated"
            trace.error_type = "ResponseTruncated"
            trace.finish_reason = finish_reason
            return _finalize_traced_analysis(
                _degraded_analysis(
                    request,
                    model_id=settings.model,
                    error_code="provider_response_truncated",
                    error_type="ResponseTruncated",
                    retryable=True,
                    reason="LLM 输出达到响应预算上限，已降级继续探索",
                    finish_reason=finish_reason,
                ),
                trace=trace,
                record_usage=False,
            )

        content = assembled.content.strip()
        if not content:
            trace.error_code = "provider_empty_response"
            trace.error_type = "EmptyResponse"
            return _finalize_traced_analysis(
                _degraded_analysis(
                    request,
                    model_id=settings.model,
                    error_code="provider_empty_response",
                    error_type="EmptyResponse",
                    retryable=True,
                    reason="LLM 返回为空，已降级继续探索",
                ),
                trace=trace,
                record_usage=False,
            )

        parse_started = time.perf_counter()
        try:
            payload = _parse_json_object(content)
            analysis = build_analysis_from_llm_payload(
                request=request,
                payload=payload,
                page_types=self._page_types,
                provider_id=PROVIDER_ID,
                model_id=settings.model,
            )
            analysis = ensure_exploration_candidates(analysis, request)
            trace.parse_ms = int((time.perf_counter() - parse_started) * 1000)
            return _finalize_traced_analysis(
                analysis,
                trace=trace,
                record_usage=False,
            )
        except (AppVisualValidationError, ValueError, TypeError, KeyError) as exc:
            trace.parse_ms = int((time.perf_counter() - parse_started) * 1000)
            logger.warning(
                "LLM App Vision 输出无法映射为合同，降级探索兜底: %s; raw=%s",
                exc,
                content[:500],
            )
            trace.error_code = "provider_parse_failed"
            trace.error_type = type(exc).__name__
            return _finalize_traced_analysis(
                _degraded_analysis(
                    request,
                    model_id=settings.model,
                    error_code="provider_parse_failed",
                    error_type=type(exc).__name__,
                    retryable=False,
                    reason="LLM 输出无效，已降级继续探索",
                ),
                trace=trace,
                record_usage=False,
            )


def _is_valid_png_bytes(content: bytes) -> bool:
    """校验 PNG magic，避免损坏截图触发视觉 LLM 400 invalid_image。"""
    return (
        isinstance(content, bytes)
        and len(content) >= len(_PNG_MAGIC)
        and content.startswith(_PNG_MAGIC)
    )


async def _create_chat_completion_with_retry(
    *,
    client: Any,
    completion_kwargs: Mapping[str, Any],
    trace: AppVisionProviderCallTrace,
) -> _AssembledChatCompletion:
    """对 408／429／5xx 与连接超时做有界退避；优先流式以采集 first-token。"""

    last_error: BaseException | None = None
    for attempt in range(_VISION_PROVIDER_MAX_RETRIES + 1):
        trace.attempt_count = attempt + 1
        try:
            try:
                return await _invoke_chat_completion(
                    client=client,
                    completion_kwargs=completion_kwargs,
                    prefer_stream=True,
                    trace=trace,
                )
            except Exception as exc:
                if not _is_unsupported_output_budget_error(exc):
                    raise
                record_current_llm_attempt_failure(
                    exc,
                    source="app_vision",
                    operation="screenshot_understanding",
                    attempt_no=attempt + 1,
                    metadata={
                        "provider_attempt_kind": "unsupported_output_budget",
                        "capture_id": trace.capture_id,
                    },
                )
                fallback_kwargs = dict(completion_kwargs)
                fallback_kwargs.pop("max_tokens", None)
                fallback_kwargs.pop("max_completion_tokens", None)
                logger.warning(
                    "Screenshot Understanding LLM 不支持输出预算参数，移除预算重试：%s: %s",
                    type(exc).__name__,
                    exc,
                )
                return await _invoke_chat_completion(
                    client=client,
                    completion_kwargs=fallback_kwargs,
                    prefer_stream=True,
                    trace=trace,
                )
        except Exception as exc:
            last_error = exc
            http_status = _vision_provider_http_status(exc)
            record_current_llm_attempt_failure(
                exc,
                source="app_vision",
                operation="screenshot_understanding",
                attempt_no=attempt + 1,
                metadata={
                    "provider_attempt_kind": "vision_request",
                    "capture_id": trace.capture_id,
                    "http_status": http_status,
                },
            )
            trace.attempt_errors.append(
                {
                    "attempt": attempt + 1,
                    "error_type": type(exc).__name__,
                    "http_status": http_status,
                    "message": str(exc)[:240],
                }
            )
            if attempt >= _VISION_PROVIDER_MAX_RETRIES or not _is_retryable_vision_provider_error(
                exc
            ):
                raise
            trace.retry_count = attempt + 1
            backoff_base = (
                _VISION_PROVIDER_OVERLOAD_BACKOFF_SECONDS
                if http_status in {500, 502, 503, 504}
                else _VISION_PROVIDER_RETRY_BACKOFF_SECONDS
            )
            delay = backoff_base * (2**attempt)
            reason = f"HTTP {http_status}" if http_status is not None else type(exc).__name__
            logger.warning(
                "Screenshot Understanding LLM %s，将在 %.1fs 后进行第 %s/%s 次重试",
                reason,
                delay,
                attempt + 1,
                _VISION_PROVIDER_MAX_RETRIES,
            )
            await asyncio.sleep(delay)
    assert last_error is not None
    raise last_error


async def _invoke_chat_completion(
    *,
    client: Any,
    completion_kwargs: Mapping[str, Any],
    prefer_stream: bool,
    trace: AppVisionProviderCallTrace,
) -> _AssembledChatCompletion:
    """优先流式采集 first-token；不支持时回退非流式。"""

    started = time.perf_counter()
    if prefer_stream:
        try:
            return await _invoke_streaming_chat_completion(
                client=client,
                completion_kwargs=completion_kwargs,
                started=started,
                trace=trace,
            )
        except Exception as exc:
            if not _is_unsupported_stream_error(exc):
                raise
            logger.warning(
                "Screenshot Understanding LLM 不支持流式，回退非流式：%s: %s",
                type(exc).__name__,
                exc,
            )
    return await _invoke_non_streaming_chat_completion(
        client=client,
        completion_kwargs=completion_kwargs,
        started=started,
        trace=trace,
    )


async def _invoke_streaming_chat_completion(
    *,
    client: Any,
    completion_kwargs: Mapping[str, Any],
    started: float,
    trace: AppVisionProviderCallTrace,
) -> _AssembledChatCompletion:
    stream_kwargs = dict(completion_kwargs)
    stream_kwargs["stream"] = True
    stream_kwargs["stream_options"] = {"include_usage": True}
    try:
        result = await client.chat.completions.create(**stream_kwargs)
    except TypeError:
        stream_kwargs.pop("stream_options", None)
        result = await client.chat.completions.create(**stream_kwargs)
    except Exception as exc:
        if _is_unsupported_stream_error(exc) and "stream_options" in stream_kwargs:
            stream_kwargs.pop("stream_options", None)
            result = await client.chat.completions.create(**stream_kwargs)
        else:
            raise

    if not hasattr(result, "__aiter__"):
        # 测试假体或网关直接返回完整响应
        assembled = _assemble_from_complete_response(result, started=started, stream_mode=False)
        trace.first_token_ms = assembled.first_token_ms
        trace.completed_ms = assembled.completed_ms
        trace.stream_mode = False
        return assembled

    content_parts: list[str] = []
    finish_reason: str | None = None
    usage: Mapping[str, Any] | None = None
    first_token_ms: int | None = None
    async for chunk in result:
        chunk_usage = _response_usage_mapping_local(chunk)
        if chunk_usage is not None:
            usage = chunk_usage
        choices = getattr(chunk, "choices", None) or ()
        if not choices:
            continue
        choice = choices[0]
        delta = getattr(choice, "delta", None)
        piece = getattr(delta, "content", None) if delta is not None else None
        if piece:
            if first_token_ms is None:
                first_token_ms = int((time.perf_counter() - started) * 1000)
            content_parts.append(str(piece))
        reason = getattr(choice, "finish_reason", None)
        if reason:
            finish_reason = str(reason).strip().casefold() or None

    completed_ms = int((time.perf_counter() - started) * 1000)
    content = "".join(content_parts)
    raw_response = type(
        "AssembledStreamResponse",
        (),
        {
            "choices": [
                type(
                    "Choice",
                    (),
                    {
                        "message": type("Message", (), {"content": content})(),
                        "finish_reason": finish_reason,
                    },
                )()
            ],
            "usage": usage,
        },
    )()
    assembled = _AssembledChatCompletion(
        content=content,
        finish_reason=finish_reason,
        usage=usage,
        raw_response=raw_response,
        first_token_ms=first_token_ms,
        completed_ms=completed_ms,
        stream_mode=True,
    )
    trace.first_token_ms = first_token_ms
    trace.completed_ms = completed_ms
    trace.stream_mode = True
    return assembled


async def _invoke_non_streaming_chat_completion(
    *,
    client: Any,
    completion_kwargs: Mapping[str, Any],
    started: float,
    trace: AppVisionProviderCallTrace,
) -> _AssembledChatCompletion:
    response = await client.chat.completions.create(**dict(completion_kwargs))
    assembled = _assemble_from_complete_response(response, started=started, stream_mode=False)
    trace.first_token_ms = assembled.first_token_ms
    trace.completed_ms = assembled.completed_ms
    trace.stream_mode = False
    return assembled


def _assemble_from_complete_response(
    response: Any,
    *,
    started: float,
    stream_mode: bool,
) -> _AssembledChatCompletion:
    completed_ms = int((time.perf_counter() - started) * 1000)
    content = ""
    try:
        content = str(response.choices[0].message.content or "")
    except (AttributeError, IndexError, TypeError):
        content = ""
    return _AssembledChatCompletion(
        content=content,
        finish_reason=_response_finish_reason(response),
        usage=_response_usage_mapping_local(response),
        raw_response=response,
        first_token_ms=None,
        completed_ms=completed_ms,
        stream_mode=stream_mode,
    )


def _response_usage_mapping_local(response: Any) -> Mapping[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, Mapping):
        return usage
    payload: dict[str, Any] = {}
    for key in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
    ):
        value = getattr(usage, key, None)
        if value is not None:
            payload[key] = value
    return payload or None


def _apply_usage_to_trace(
    trace: AppVisionProviderCallTrace,
    usage: Mapping[str, Any] | None,
) -> None:
    if not usage:
        return
    normalized = normalize_llm_usage_tokens(usage)
    trace.input_tokens = normalized["input_tokens"]
    trace.output_tokens = normalized["output_tokens"]
    trace.total_tokens = normalized["total_tokens"]


def _finalize_traced_analysis(
    analysis: AppVisualAnalysis,
    *,
    trace: AppVisionProviderCallTrace,
    record_usage: bool,
) -> AppVisualAnalysis:
    """把 provider latency 写入 surface_facts；失败路径补记 usage event。"""

    latency = trace.to_latency_dict()
    if record_usage:
        record_current_llm_usage(
            {
                "prompt_tokens": trace.input_tokens or 0,
                "completion_tokens": trace.output_tokens or 0,
                "total_tokens": trace.total_tokens or 0,
            },
            source="app_vision",
            operation="screenshot_understanding",
            metadata={"app_vision_latency": latency},
        )
    surface_facts = dict(analysis.surface_facts)
    existing_latency = (
        dict(surface_facts["visual_latency"])
        if isinstance(surface_facts.get("visual_latency"), Mapping)
        else {}
    )
    existing_latency.update(latency)
    existing_latency.setdefault("reused", False)
    surface_facts["visual_latency"] = existing_latency
    logger.info(
        "App vision provider latency capture_id=%s request_start_ms=%s first_token_ms=%s "
        "completed_ms=%s retry_count=%s input_tokens=%s output_tokens=%s error_code=%s "
        "stream_mode=%s",
        trace.capture_id,
        trace.request_start_ms,
        trace.first_token_ms,
        trace.completed_ms,
        trace.retry_count,
        trace.input_tokens,
        trace.output_tokens,
        trace.error_code,
        trace.stream_mode,
    )
    return AppVisualAnalysis.build(
        capture_id=analysis.capture_id,
        screenshot_artifact_id=analysis.screenshot_artifact_id,
        image_signature=analysis.image_signature,
        width=analysis.width,
        height=analysis.height,
        orientation=analysis.orientation,
        visual_page_type=analysis.visual_page_type,
        visual_summary=analysis.visual_summary,
        key_elements=analysis.key_elements,
        candidates=analysis.candidates,
        provider_id=analysis.provider_id,
        model_id=analysis.model_id,
        package_name=analysis.package_name,
        activity=analysis.activity,
        page_state=analysis.page_state,
        overlay_signature=analysis.overlay_signature,
        surface_facts=surface_facts,
    )


def _is_unsupported_stream_error(exc: BaseException) -> bool:
    message_parts = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        message_parts.append(str(body))
    message = " ".join(message_parts).casefold()
    if not any(marker in message for marker in _UNSUPPORTED_STREAM_MARKERS):
        return False
    return any(marker in message for marker in _UNSUPPORTED_OUTPUT_BUDGET_MARKERS)


def _is_retryable_vision_provider_error(error: BaseException) -> bool:
    """只重试超时、限流与网关类故障，不重试鉴权或内容策略错误。"""

    if _vision_provider_http_status(error) in _RETRYABLE_VISION_HTTP_STATUSES:
        return True
    pending: list[Any] = [error]
    visited: set[int] = set()
    retryable_names = {
        "apiconnectionerror",
        "apitimeouterror",
        "connecterror",
        "connectionerror",
        "connecttimeout",
        "pooltimeout",
        "ratelimiterror",
        "readtimeout",
        "timeout",
        "timeouterror",
        "writetimeout",
    }
    while pending:
        item = pending.pop()
        if id(item) in visited:
            continue
        visited.add(id(item))
        if isinstance(item, BaseException):
            if type(item).__name__.casefold() in retryable_names:
                return True
            pending.extend(item.args)
            pending.extend(
                cause for cause in (item.__cause__, item.__context__) if cause is not None
            )
    return False


def _vision_provider_http_status(error: BaseException) -> int | None:
    pending: list[Any] = [error]
    visited: set[int] = set()
    while pending:
        item = pending.pop()
        if id(item) in visited:
            continue
        visited.add(id(item))
        if isinstance(item, BaseException):
            status = getattr(item, "status_code", None)
            if isinstance(status, int):
                return status
            response = getattr(item, "response", None)
            response_status = getattr(response, "status_code", None)
            if isinstance(response_status, int):
                return response_status
            pending.extend(item.args)
            pending.extend(
                cause for cause in (item.__cause__, item.__context__) if cause is not None
            )
    return None


def _degraded_analysis(
    request: UnderstandAppScreenshotRequest,
    *,
    model_id: str,
    error_code: str,
    error_type: str,
    retryable: bool,
    reason: str,
    finish_reason: str | None = None,
) -> AppVisualAnalysis:
    """保留安全探索候选，同时把 Provider 降级写入不可变视觉事实。"""

    degradation_fact: dict[str, object] = {
        "protocol_version": "app-visual-provider-quality.v1",
        "status": "degraded",
        "provider_id": PROVIDER_ID,
        "model_id": model_id,
        "error_code": error_code,
        "error_type": error_type,
        "retryable": retryable,
    }
    if finish_reason is not None:
        degradation_fact["finish_reason"] = finish_reason

    return build_first_screen_escape_analysis(
        request,
        reason=reason,
        degradation_fact=degradation_fact,
    )


def _chat_completion_output_budget_kwargs(*, model: str, max_tokens: int) -> dict[str, int]:
    """按 Chat Completions 模型能力选择输出预算字段。"""

    normalized_model = str(model or "").strip().casefold()
    if normalized_model.startswith(_MODERN_CHAT_TOKEN_MODELS):
        # GPT-5 与 o 系列模型将 max_tokens 视为已弃用别名，优先发送新字段。
        return {"max_completion_tokens": max_tokens}
    return {"max_tokens": max_tokens}


def _is_unsupported_output_budget_error(exc: BaseException) -> bool:
    """只识别供应商明确拒绝输出预算字段的错误，避免扩大重试范围。"""

    message_parts = [str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        message_parts.append(str(body))
    message = " ".join(message_parts).casefold()
    output_budget_names = ("max_output_tokens", "max_completion_tokens", "max_tokens")
    if not any(name in message for name in output_budget_names):
        return False
    return any(marker in message for marker in _UNSUPPORTED_OUTPUT_BUDGET_MARKERS)


def _response_finish_reason(response: Any) -> str | None:
    """读取 OpenAI-compatible Chat Completions 的 finish_reason，缺失时保持兼容。"""
    try:
        raw_reason = response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None
    normalized = str(raw_reason or "").strip().casefold()
    return normalized or None


def build_analysis_from_llm_payload(
    *,
    request: UnderstandAppScreenshotRequest,
    payload: Mapping[str, Any],
    page_types: PageTypeRegistry,
    provider_id: str = PROVIDER_ID,
    model_id: str,
) -> AppVisualAnalysis:
    """把模型 JSON 规范化为 AppVisualAnalysis；越界候选静默丢弃。"""
    if not isinstance(payload, Mapping):
        raise ValueError("payload 必须为对象")

    visual_page_type = _resolve_page_type(
        payload.get("visual_page_type"),
        page_types=page_types,
    )
    visual_summary = str(payload.get("visual_summary") or "").strip() or "模型未提供摘要"
    page_state = _resolve_page_state(payload.get("page_state"))
    overlay_signature = _optional_string(payload.get("overlay_signature"))
    surface_facts = _surface_facts(payload.get("surface_facts"))
    page_name_fact = build_visual_page_name_fact(
        page_name=payload.get("page_name"),
        source=payload.get("page_name_source"),
        confidence=payload.get("page_name_confidence"),
    )
    if page_name_fact is not None:
        surface_facts[APP_VISUAL_PAGE_NAME_FACT_KEY] = page_name_fact
    page_type_confidence = _parse_confidence(
        payload.get(
            "visual_page_type_confidence",
            surface_facts.get(
                "visual_page_type_confidence", surface_facts.get("page_type_confidence")
            ),
        )
    )
    # 新的模型输出缺少可信度不能沿用历史证据的默认满分。
    surface_facts["visual_page_type_confidence"] = (
        page_type_confidence if page_type_confidence is not None else 0.0
    )
    surface_facts.update(
        build_app_visual_page_facts(
            identity_observation=payload.get("page_identity_observation"),
            local_state_observation=payload.get("local_state_observation"),
            capture_id=request.capture_id,
            screenshot_artifact_id=request.screenshot_artifact_id,
            image_signature=request.image_signature,
            package_name=request.package_name,
            activity=request.activity,
            page_state=page_state,
            overlay_signature=overlay_signature,
            surface_facts=surface_facts,
        )
    )
    key_elements = _unique_strings(payload.get("key_elements"), limit=20)
    raw_candidates = payload.get("candidates")
    if raw_candidates is None:
        raw_candidates = ()
    if not isinstance(raw_candidates, Sequence) or isinstance(raw_candidates, (str, bytes)):
        raise ValueError("candidates 必须为数组")

    candidates: list[AppVisualCandidate] = []
    for item in raw_candidates:
        if len(candidates) >= request.max_candidates:
            break
        mapped = _map_candidate(item, request=request)
        if mapped is None:
            continue
        if mapped.action_type not in request.allowed_actions:
            continue
        candidates.append(mapped)

    return AppVisualAnalysis.build(
        capture_id=request.capture_id,
        screenshot_artifact_id=request.screenshot_artifact_id,
        image_signature=request.image_signature,
        width=request.width,
        height=request.height,
        orientation=request.orientation,
        visual_page_type=visual_page_type,
        visual_summary=visual_summary,
        key_elements=key_elements,
        candidates=tuple(candidates),
        provider_id=provider_id,
        model_id=model_id,
        package_name=request.package_name,
        activity=request.activity,
        page_state=page_state,
        overlay_signature=overlay_signature,
        surface_facts=surface_facts,
    )


def _resolve_page_state(raw: Any) -> str:
    normalized = str(raw or "unknown").strip().casefold().replace("-", "_")
    allowed = {
        "unknown",
        "stable",
        "normal",
        "loaded",
        "loading",
        "line_loading",
        "content_available",
        "transitional",
        "blocked",
        "startup_ad",
        "countdown",
        "popup",
        "modal",
        "overlay",
        "dialog",
        "auth_gate",
        "auth_required",
        "login_required",
        "requires_auth",
        "system_dialog",
        "permission_dialog",
        "browser_permission",
    }
    return normalized if normalized in allowed else "unknown"


def _optional_string(raw: Any) -> str | None:
    value = str(raw).strip() if raw is not None else ""
    return value or None


def _surface_facts(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        return {}
    allowed = {
        "topmost_ad_popup",
        "countdown_remaining_ms",
        "close_control_visible",
        "close_control_enabled",
        "close_semantic_target",
        "popup_visible",
        "auth_gate_visible",
        "login_form_visible",
        "session_auth",
        "authenticated",
        "logged_in",
        "requires_auth",
        "dismiss_control_visible",
        "dismiss_control_enabled",
        "dismiss_semantic_target",
        "guest_entry_visible",
        "guest_entry_enabled",
        "guest_entry_semantic_target",
        "dialog_visible",
        "dialog_kind",
        "action_policy",
        "action_control_visible",
        "action_control_enabled",
        "action_semantic_target",
        "visual_page_type_confidence",
        "page_type_confidence",
        "confidence",
    }
    return {key: value for key, value in raw.items() if key in allowed}


def _resolve_page_type(
    raw: Any,
    *,
    page_types: PageTypeRegistry,
) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.casefold() in {"null", "none", "unknown", "n/a"}:
        return None
    try:
        page_types.require_code(text)
        return text
    except Exception:
        pass
    alias = page_types.resolve_alias(text)
    if alias is not None:
        return alias.canonical_code
    # 允许模型输出近似 code（大小写/连字符）
    normalized = text.casefold().replace("-", "_").replace(" ", "_")
    for code in page_types.all_codes:
        if code.casefold() == normalized:
            return code
    return None


def _map_candidate(
    item: Any,
    *,
    request: UnderstandAppScreenshotRequest,
) -> AppVisualCandidate | None:
    if not isinstance(item, Mapping):
        return None
    semantic_name = str(item.get("semantic_name") or "").strip()
    purpose = str(item.get("purpose") or "").strip()
    if not semantic_name or not purpose:
        return None
    action_type = _parse_action(item.get("action_type"))
    if action_type is None:
        return None
    risk_level = _parse_risk(item.get("risk_level"))
    confidence = _parse_confidence(item.get("confidence"))
    if confidence is None:
        return None
    bounds = _parse_bounds(item.get("normalized_bounds"), action_type=action_type)
    if action_type is InteractionOperation.CLICK and bounds is None:
        return None
    hint = item.get("expected_state_hint")
    expected = None if hint is None else str(hint).strip() or None
    frontier_scope = str(item.get("frontier_scope") or "local").strip().casefold()
    if frontier_scope not in {"local", "page", "app"}:
        frontier_scope = "local"
    control_role = _parse_control_role(item.get("control_role"))
    if "control_role" in item and item.get("control_role") is not None and control_role is None:
        return None
    try:
        return AppVisualCandidate.build(
            source_capture_id=request.capture_id,
            source_image_signature=request.image_signature,
            source_width=request.width,
            source_height=request.height,
            source_orientation=request.orientation,
            semantic_name=semantic_name,
            action_type=action_type,
            normalized_bounds=bounds,
            confidence=confidence,
            risk_level=risk_level,
            purpose=purpose,
            control_role=control_role,
            expected_state_hint=expected,
            frontier_scope=frontier_scope,
            discovery_source="vision",
        )
    except AppVisualValidationError:
        return None


def _parse_action(raw: Any) -> InteractionOperation | None:
    text = str(raw or "").strip().casefold()
    return _ACTION_ALIASES.get(text)


def _parse_control_role(raw: Any) -> str | None:
    """只接受 classifier 合同规定的规范 role。"""
    if raw is None:
        return None
    if isinstance(raw, str) and raw in APP_VISUAL_CONTROL_ROLES:
        return raw
    return None


def _parse_risk(raw: Any) -> InteractionRiskLevel:
    text = str(raw or "medium").strip().casefold()
    return _RISK_ALIASES.get(text, InteractionRiskLevel.MEDIUM)


def _parse_confidence(raw: Any) -> float | None:
    if isinstance(raw, bool):
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not 0 <= value <= 1:
        return None
    return value


def _parse_bounds(
    raw: Any,
    *,
    action_type: InteractionOperation,
) -> NormalizedBounds | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return None
    try:
        bounds = NormalizedBounds(
            left=float(raw["left"]),
            top=float(raw["top"]),
            right=float(raw["right"]),
            bottom=float(raw["bottom"]),
        )
    except (KeyError, TypeError, ValueError, AppVisualValidationError):
        return None
    if action_type is InteractionOperation.SCROLL:
        # scroll 允许有界区域提示，但合同不强制
        return bounds
    return bounds


def _unique_strings(raw: Any, *, limit: int) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return ()
    seen: set[str] = set()
    result: list[str] = []
    for item in raw:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
        if len(result) >= limit:
            break
    return tuple(result)


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    match = _JSON_OBJECT_PATTERN.search(stripped)
    if match is None:
        raise ValueError("模型未返回 JSON 对象")
    data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("模型 JSON 根必须为对象")
    return data


def _system_prompt(
    *,
    page_codes: Sequence[str],
    allowed_actions: Sequence[str],
    max_candidates: int,
    page_types: PageTypeRegistry | None = None,
) -> str:
    from services.api.app.domain.page.discrimination_guide import (
        build_app_page_discrimination_guide,
        format_app_page_discrimination_guide_for_prompt,
    )
    from services.api.app.domain.page.registry import PageTypeRegistry

    codes = ", ".join(page_codes)
    actions = ", ".join(allowed_actions)
    registry = page_types if isinstance(page_types, PageTypeRegistry) else None
    guide_text = format_app_page_discrimination_guide_for_prompt(
        build_app_page_discrimination_guide(registry)
    )
    return (
        "你是 Android App 质检的截图理解器。只根据截图输出一个 JSON 对象，不要 markdown。\n"
        f"{APP_PAGE_STRUCTURE_PROMPT}"
        "字段：\n"
        "- visual_page_type: 必须是目录代码之一，或 null；允许代码："
        f"{codes}\n"
        "- page_state: unknown|stable|loading|transitional|startup_ad|countdown|"
        "popup|modal|overlay|auth_gate|login_required|system_dialog|permission_dialog\n"
        "- overlay_signature: 当前覆盖层的稳定语义签名，没有则 null\n"
        "- surface_facts: 仅填写截图可见事实，如 countdown_remaining_ms、"
        "popup_visible、requires_auth、dialog_visible、dialog_kind 和可见控件状态。"
        "session_auth 仅允许 guest|authenticated|unknown：明确账号登录表单、游客标识"
        "可证明 guest；只有当前账户身份及退出登录等明确已登录控件可证明 authenticated，"
        "并在 key_elements 保留对应可见文字。没有登录弹窗、存在个人中心入口、"
        "18岁年龄门或能够浏览内容都不能证明登录态，无法确认必须填 unknown。"
        "若确认最上层是广告弹窗，填写 topmost_ad_popup={is_ad:true,confidence:0到1,"
        "normalized_bounds:{left,top,right,bottom}}，只框当前可交互最上层弹窗；"
        "不能确认则不填写，不得把背景横幅、角落浮动广告或底层内容算进此区域\n"
        "- page_name: 当前页面稳定短名称，优先顶部栏、选中 Tab 或固定页面地标；"
        "不要使用广告文案、内容卡片标题或动态计数，没有可靠名称则 null\n"
        "- page_name_source: top_bar|selected_tab|navigation_entry|page_landmark|"
        "visual_inference|null\n"
        "- page_name_confidence: 0.0 到 1.0，没有页面名称则 null\n"
        "- page_identity_observation: 无法确认稳定业务页或被弹窗遮挡时为 null；否则对象包含 "
        "page_role（home|navigation|collection|detail|search|profile|form|reader|player|settings）、"
        "confidence、location、function、return_semantics（root|back|close）、return_control、business_object。"
        "location 是当前业务容器的稳定名称锚点，不取局部 Tab、筛选值、动态计数或详情对象标题；"
        "function 是与名称不同的可见功能控件锚点，证明页面职责，不能重复名称充数。"
        "return_control 是实际返回/关闭控件，root 时给可见的根导航依据，不猜返回后的页面。"
        "business_object 是 0～3 个当前对象锚点，detail/reader/player 必须包含可见对象编号，"
        "或至少两条独立的对象文案（如标题和作者），不能用列表中的其他对象，也不能编造内部 ID。"
        "每个锚点格式为 {text:截图原文或图标描述,kind:label|icon|identifier,confidence:0到1,"
        "normalized_bounds:{left,top,right,bottom}}；只框实际依据，不用整屏代替。"
        "没有对象依据、独立功能依据或返回依据就返回 null，不用页型或标题强行补齐身份。\n"
        "- local_state_observation: 对象，仅按截图填写 selected_tab、selected_filter、sort_key、"
        "page_index、expanded_item_id；值为上述可见文字锚点或最多8个锚点数组。"
        "这里记录同一页面内的 Tab、筛选、排序、分页和展开状态，不把这些值写入 location。"
        "不可见或不确定的状态省略，不输出旧截图状态。\n"
        "- visual_page_type_confidence: 当前 visual_page_type 的可信度，必须明确输出 "
        "0.0 到 1.0 的数值；"
        "无法可靠识别业务页面时 visual_page_type 应为 null，并给出低可信度\n"
        "- visual_summary: 一句话中文摘要\n"
        "- key_elements: 字符串数组，页面关键控件/文案\n"
        "- candidates: 最多 "
        f"{max_candidates} 个可交互候选；action_type 仅允许 {actions}\n"
        "每个 candidate：\n"
        '{"semantic_name":"...","action_type":"click","normalized_bounds":'
        '{"left":0.1,"top":0.2,"right":0.4,"bottom":0.3},'
        '"confidence":0.0到1.0,"risk_level":"low|medium|high",'
        '"purpose":"...","control_role":null,'
        '"expected_state_hint":null,'
        '"frontier_scope":"local|page|app"}\n'
        "规则：\n"
        "1. click 必须给 normalized_bounds，坐标为相对整屏的 [0,1]，left<right 且 top<bottom。\n"
        "2. back/observe 可不给 bounds；不要臆造看不到的控件。\n"
        "3. 无法判断 page_type 时返回 null，不要编造目录外代码。\n"
        "4. visual_summary 不超过 32 个字，key_elements 最多 8 项且每项不超过 16 个字；"
        "优先列出探索价值高的导航/列表/关闭或跳过控件等候选。\n"
        "5. control_role 只允许 dismiss、skip 或 null；它只是当前截图中的通用语义标签，"
        "无论关闭/跳过控件的文字语言或是否只有图标，输出都必须使用上述规范值；"
        "用于识别真实可见的关闭/跳过控件。它不能降低 risk_level，不能绕过当前帧可见性、"
        "坐标、截图绑定或后续状态核验；无法确认真实控件时返回 null 并不要臆造候选。\n"
        "6. 必须区分广告素材内的装饰文字、logo、价格、倒计时数字或伪按钮与真实可交互控件；"
        "只有有截图证据的控件才能成为候选。广告素材和倒计时不可标成业务详情、业务入口或业务页名/页型。"
        "若当前可见弹窗广告，除关闭或跳过控件外，可识别最上层弹窗内有明确视觉证据的广告 CTA，"
        "bounds 必须只框 CTA 且完全位于 topmost_ad_popup 内；禁止选择背景浮动广告或装饰文字，"
        "下载、安装、充值或支付按钮不得作为该广告候选。"
        "该 CTA 的 semantic_name 使用“弹窗广告点击入口”，purpose 说明点击广告并观察浏览器落地页；"
        "另可框出同一弹窗的广告主体作为回退，semantic_name 使用“弹窗广告主体”。\n"
        "7. frontier_scope=app 仅用于底栏 Tab、一级业务入口等跨页面 sibling；"
        "page 用于当前业务页内可恢复分支；瞬时弹层、滚动和关闭/跳过操作必须为 local。\n"
        "8. 若候选能明确预测下一页类型，expected_state_hint 优先填写上述目录中的规范代码；"
        "无法确定页型时才填写简短状态提示或 null。\n"
        "9. 页型判别须遵守下列指南（影子输入；不得用页型改写业务身份）：\n"
        f"{guide_text}\n"
        "10. App 探索默认操作身份为已满18岁的成年人，入口年龄确认应自动确认，"
        "无需再次询问用户年龄、请求用户操作或转人工。"
        "若截图是年龄确认门禁（如「请确定你已年满18岁」「我已满18岁」「我年满18岁-进入」"
        "／「我未满18岁-离开」），page_state 必须为 auth_gate；"
        "surface_facts.dialog_kind=age_verification、dialog_visible=true、"
        "auth_gate_visible=true、requires_auth=false；单纯年龄门不属于账号登录、实名认证"
        "或人机验证。candidates 必须包含当前截图真实可见的成年确认进入按钮，"
        "semantic_name 优先原文「我已满18岁」「我年满18岁-进入」或 confirm_age_18_enter，"
        "purpose 说明确认已满18岁并进入应用，risk_level=low、control_role=dismiss、"
        "frontier_scope=local；只有单纯入口成年确认适用此规则，"
        "登录／注册表单中的年龄勾选、凭证输入、实名认证、人机验证或支付不得据此放行；"
        "严禁把「我未满18岁-离开」或离开／exit 按钮作为候选。"
        "11. 关闭／跳过控件必须用当前截图像素框出，禁止产品脚本坐标或固定绝对落点："
        "（a）先确认最上层遮罩/弹层边界（独立层、背景遮罩、挡住业务内容）；"
        "（b）在该层外围找关闭控件：圆形白底黑 X、圆框 ×、半透明「关闭/跳过」胶囊；"
        "bounds 只框该控件外接矩形，中心必须落在控件墨水内，不得扩大到广告素材或 CTA；"
        "（c）相对位置用「相对弹层面板」描述，禁止套死垂直 0.66–0.86："
        "面板下方圆 X 可在面板底边之外更远更低，只要仍属该层关闭钮；"
        "面板四角/右上角 X 只框角上小钮，勿框标题栏返回；"
        "（d）网格/人像弹层下方圆 X → semantic_name「关闭弹层下方X」，"
        "control_role=dismiss、expected_state_hint=close_popup_x、frontier_scope=local；"
        "（e）开屏／全屏「关闭」或「跳过」胶囊 → semantic_name「关闭开屏广告」或原文，"
        "control_role=skip、expected_state_hint=tap_skip，bounds 只框该胶囊；"
        "（f）禁区：广告图标本体、下载／安装／充值、底栏 Tab、页内左上返回、云手机系统 back；"
        "看不清关闭钮时不要臆造 dismiss，不要用猜坐标代替；弹层未关完前不要把底栏 Tab 列为优先候选。"
        "12. 稳定业务页可见底栏时，必须用视觉框出每个底栏 Tab（图标+下方文字）："
        "semantic_name 用「底栏「主页／短剧／暗网／AI／社区／我的」」等原文标签，"
        "frontier_scope=app、risk_level=low；bounds 只框该栏图标与文字（约 top 0.84–0.94，"
        "勿落入系统手势带 top≥0.92 的触控中心）；禁止等分臆造坐标、禁止框内容区宫格。"
        "弹层未关完时仍可识别底栏几何，但不要把底栏列为优先执行候选。"
    )


def _user_prompt(request: UnderstandAppScreenshotRequest) -> str:
    parts = [
        f"截图像素约 {request.width}x{request.height}，orientation={request.orientation}。",
    ]
    if request.package_name:
        parts.append(f"package_name={request.package_name}")
    if request.activity:
        parts.append(f"activity={request.activity}")
    parts.append("请输出视觉理解 JSON。")
    return "\n".join(parts)
