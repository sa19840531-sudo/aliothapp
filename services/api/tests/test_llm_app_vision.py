"""LLM App Vision Provider 的合同映射与压缩测试。"""

from __future__ import annotations

import asyncio
import hashlib
import io

import pytest

from services.api.app.agents.runtime.usage_meter import bind_run_llm_usage_meter
from services.api.app.application.app_screenshot_understanding import (
    UnderstandAppScreenshotRequest,
)
from services.api.app.application.app_workflow_dispatch import AppWorkflowNotConfiguredError
from services.api.app.core.config import Settings
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.infrastructure.app_vision.factory import resolve_app_vision_provider
from services.api.app.infrastructure.app_vision.fake import FakeAppVisionProvider
from services.api.app.infrastructure.app_vision.image_codec import (
    compress_png_bytes_for_llm,
)
from services.api.app.infrastructure.app_vision.llm import (
    LlmAppVisionProvider,
    build_analysis_from_llm_payload,
)


def _png_bytes() -> bytes:
    try:
        from PIL import Image
    except ImportError:
        return b"\x89PNG\r\n\x1a\n"
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color=(20, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def _request() -> UnderstandAppScreenshotRequest:
    png = _png_bytes()
    return UnderstandAppScreenshotRequest(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="22222222-2222-2222-2222-222222222222",
        capture_id="33333333-3333-3333-3333-333333333333",
        screenshot_artifact_id="44444444-4444-4444-4444-444444444444",
        screenshot_png=png,
        image_signature=f"sha256:{hashlib.sha256(png).hexdigest()}",
        width=1080,
        height=1920,
        orientation="portrait",
        package_name="com.example.app",
        activity=".MainActivity",
        allowed_actions=(
            InteractionOperation.CLICK,
            InteractionOperation.SCROLL,
            InteractionOperation.BACK,
            InteractionOperation.OBSERVE,
        ),
        max_candidates=5,
    )


@pytest.mark.parametrize(
    "facts",
    [
        {"session_auth": "authenticated"},
        {"logged_in": False},
        {"authenticated": True},
        {"login_form_visible": True},
    ],
)
def test_build_analysis_preserves_explicit_session_auth_facts(facts) -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": "home",
            "visual_summary": "可见账号状态",
            "surface_facts": facts,
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert all(analysis.surface_facts[key] == value for key, value in facts.items())


def test_build_analysis_maps_page_type_and_click_candidate() -> None:
    request = _request()
    analysis = build_analysis_from_llm_payload(
        request=request,
        payload={
            "visual_page_type": "home",
            "page_name": "AI 世界首页",
            "page_name_source": "top_bar",
            "page_name_confidence": 0.94,
            "visual_summary": "首页信息流",
            "key_elements": ["搜索", "底部导航"],
            "candidates": [
                {
                    "semantic_name": "search_entry",
                    "action_type": "tap",
                    "normalized_bounds": {
                        "left": 0.1,
                        "top": 0.05,
                        "right": 0.9,
                        "bottom": 0.12,
                    },
                    "confidence": 0.91,
                    "risk_level": "low",
                    "purpose": "进入搜索",
                    "frontier_scope": "app",
                },
                {
                    "semantic_name": "invalid_click",
                    "action_type": "click",
                    "normalized_bounds": None,
                    "confidence": 0.5,
                    "risk_level": "low",
                    "purpose": "应被丢弃",
                },
            ],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert analysis.visual_page_type == "home"
    assert analysis.provider_id == "llm-app-vision"
    assert analysis.model_id == "test-model"
    assert analysis.surface_facts["visual_page_name"] == {
        "protocol_version": "app-visual-page-name.v1",
        "page_name": "AI 世界首页",
        "source": "top_bar",
        "confidence": 0.94,
    }
    assert len(analysis.candidates) == 1
    candidate = analysis.candidates[0]
    assert candidate.action_type is InteractionOperation.CLICK
    assert candidate.risk_level is InteractionRiskLevel.LOW
    assert candidate.normalized_bounds is not None
    assert candidate.confidence == 0.91
    assert candidate.frontier_scope == "app"


@pytest.mark.parametrize("semantic_name", ("关闭", "Fermer", "×", "Закрыть"))
def test_build_analysis_keeps_canonical_role_separate_from_localized_semantic_name(
    semantic_name: str,
) -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": "home",
            "visual_page_type_confidence": 0.88,
            "visual_summary": "首页有可关闭控件",
            "key_elements": [semantic_name],
            "candidates": [
                {
                    "semantic_name": semantic_name,
                    "action_type": "click",
                    "normalized_bounds": {
                        "left": 0.1,
                        "top": 0.1,
                        "right": 0.2,
                        "bottom": 0.2,
                    },
                    "confidence": 0.91,
                    "risk_level": "low",
                    "purpose": "关闭当前覆盖层",
                    "control_role": "dismiss",
                }
            ],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert analysis.surface_facts["visual_page_type_confidence"] == 0.88
    assert analysis.candidates[0].semantic_name == semantic_name
    assert analysis.candidates[0].control_role == "dismiss"


@pytest.mark.parametrize("control_role", ("Fermer", "关闭", "DISMISS", "x", "details"))
def test_build_analysis_rejects_noncanonical_control_role(control_role: str) -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": "home",
            "visual_summary": "首页",
            "key_elements": [],
            "candidates": [
                {
                    "semantic_name": "关闭",
                    "action_type": "click",
                    "normalized_bounds": {
                        "left": 0.1,
                        "top": 0.1,
                        "right": 0.2,
                        "bottom": 0.2,
                    },
                    "confidence": 0.91,
                    "risk_level": "low",
                    "purpose": "关闭当前覆盖层",
                    "control_role": control_role,
                }
            ],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert analysis.candidates == ()


def test_build_analysis_maps_surface_phase_facts_and_drops_unknown_fields() -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": "home",
            "page_state": "popup",
            "overlay_signature": "overlay:dialog-1",
            "surface_facts": {
                "popup_visible": True,
                "close_control_visible": True,
                "close_semantic_target": "关闭弹窗",
                "unknown_runtime_handle": "must-be-dropped",
            },
            "visual_summary": "首页被弹窗遮挡",
            "key_elements": ["关闭按钮"],
            "candidates": [],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert analysis.page_state == "popup"
    assert analysis.overlay_signature == "overlay:dialog-1"
    assert analysis.surface_facts == {
        "popup_visible": True,
        "close_control_visible": True,
        "close_semantic_target": "关闭弹窗",
        "visual_page_type_confidence": 0.0,
        "app_page_identity_assessment": {
            "status": "unknown",
            "reason": "business_surface_not_visible",
        },
    }


def test_build_analysis_fails_closed_unknown_page_state_to_unknown() -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": "home",
            "page_state": "future_surface_state",
            "overlay_signature": " ",
            "surface_facts": {"future_fact": True},
            "visual_summary": "未知阶段",
            "key_elements": [],
            "candidates": [],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert analysis.page_state == "unknown"
    assert analysis.overlay_signature is None
    assert analysis.surface_facts == {
        "visual_page_type_confidence": 0.0,
        "app_page_identity_assessment": {
            "status": "unknown",
            "reason": "business_surface_not_visible",
        },
    }


def test_build_analysis_resolves_unknown_page_type_to_none() -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": "not_a_real_page",
            "visual_summary": "未知页",
            "key_elements": [],
            "candidates": [],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert analysis.visual_page_type is None
    assert analysis.candidates == ()


@pytest.mark.parametrize(
    "page_type",
    get_default_page_type_registry().all_codes,
)
def test_build_analysis_accepts_every_shared_catalog_page_type(page_type: str) -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": page_type,
            "visual_summary": "共享页面类型",
            "key_elements": [],
            "candidates": [],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )

    assert analysis.visual_page_type == page_type


@pytest.mark.asyncio
async def test_llm_vision_prompt_uses_every_shared_catalog_page_type() -> None:
    captured: dict[str, object] = {}

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    captured.update(kwargs)
                    message = type(
                        "Message",
                        (),
                        {
                            "content": (
                                '{"visual_page_type":"line_loading",'
                                '"visual_summary":"线路加载中",'
                                '"key_elements":[],"candidates":[]}'
                            )
                        },
                    )()
                    choice = type("Choice", (), {"message": message})()
                    return type("Response", (), {"choices": [choice]})()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _Client(),
    )

    await provider.understand(_request())

    system_prompt = str(captured["messages"][0]["content"])
    for page_type in get_default_page_type_registry().all_codes:
        assert page_type in system_prompt
    assert "control_role" in system_prompt
    assert '"control_role":null' in system_prompt
    assert "visual_page_type_confidence" in system_prompt
    assert "广告素材内的装饰文字" in system_prompt
    assert "广告素材和倒计时不可标成业务详情" in system_prompt
    assert "App 探索默认操作身份为已满18岁的成年人" in system_prompt
    assert "无需再次询问用户年龄、请求用户操作或转人工" in system_prompt
    assert "我已满18岁" in system_prompt
    assert "auth_gate_visible=true、requires_auth=false" in system_prompt
    assert "risk_level=low、control_role=dismiss" in system_prompt
    assert "登录／注册表单中的年龄勾选" in system_prompt
    assert "严禁把「我未满18岁-离开」" in system_prompt
    assert "CP9" not in system_prompt
    assert "CP10" not in system_prompt


@pytest.mark.asyncio
async def test_llm_vision_retries_timeout_before_degrade(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    class _TimeoutClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    calls["n"] += 1
                    error = TimeoutError("vision upstream 408")
                    error.status_code = 408  # type: ignore[attr-defined]
                    raise error

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "services.api.app.infrastructure.app_vision.llm.asyncio.sleep",
        _fake_sleep,
    )
    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _TimeoutClient(),
    )
    analysis = await provider.understand(_request())
    assert calls["n"] == 3
    assert sleeps == [0.5, 1.0]
    assert analysis.surface_facts["visual_provider_quality"]["error_code"] == "provider_call_failed"
    assert analysis.surface_facts["visual_provider_quality"]["retryable"] is True


@pytest.mark.asyncio
async def test_llm_vision_falls_back_when_client_raises() -> None:
    class _BoomClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    raise RuntimeError("upstream down")

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _BoomClient(),
    )
    analysis = await provider.understand(_request())
    assert analysis.provider_id == "heuristic-app-vision"
    assert any(c.action_type is InteractionOperation.SCROLL for c in analysis.candidates)
    assert analysis.surface_facts["visual_provider_quality"] == {
        "protocol_version": "app-visual-provider-quality.v1",
        "status": "degraded",
        "provider_id": "llm-app-vision",
        "model_id": "m",
        "error_code": "provider_call_failed",
        "error_type": "RuntimeError",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_llm_vision_empty_candidates_preserve_provider_degradation_fact() -> None:
    class _EmptyClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    message = type(
                        "Message",
                        (),
                        {
                            "content": (
                                '{"visual_page_type":"home","visual_summary":"首页",'
                                '"key_elements":[],"candidates":[]}'
                            )
                        },
                    )()
                    choice = type("Choice", (), {"message": message})()
                    return type("Response", (), {"choices": [choice]})()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _EmptyClient(),
    )

    analysis = await provider.understand(_request())

    quality = analysis.surface_facts["visual_provider_quality"]
    assert quality["status"] == "degraded"
    assert quality["error_code"] == "provider_no_candidates"
    assert quality["provider_id"] == "llm-app-vision"


@pytest.mark.asyncio
async def test_llm_vision_treats_truncated_response_as_degraded_without_json_repair() -> None:
    captured: dict[str, object] = {}

    class _TruncatedClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    captured.update(kwargs)
                    message = type(
                        "Message",
                        (),
                        {
                            "content": (
                                '{"visual_page_type":"home","visual_summary":"首页",'
                                '"candidates":[{"semantic_name":"入口",'
                                '"normalized_bounds":{"left":0.1'
                            )
                        },
                    )()
                    choice = type(
                        "Choice",
                        (),
                        {"message": message, "finish_reason": "length"},
                    )()
                    return type("Response", (), {"choices": [choice]})()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "gpt-5.6-terra", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _TruncatedClient(),
    )

    analysis = await provider.understand(_request())

    quality = analysis.surface_facts["visual_provider_quality"]
    assert analysis.provider_id == "heuristic-app-vision"
    assert quality["error_code"] == "provider_response_truncated"
    assert quality["error_type"] == "ResponseTruncated"
    assert quality["finish_reason"] == "length"
    assert quality["retryable"] is True
    assert captured["max_completion_tokens"] == 2400
    # 半截 JSON 只能降级，不得从未闭合的 normalized_bounds 中伪造点击候选。
    assert all(
        candidate.action_type is not InteractionOperation.CLICK
        for candidate in analysis.candidates
    )


@pytest.mark.asyncio
async def test_llm_vision_uses_legacy_budget_field_for_non_modern_models() -> None:
    captured: dict[str, object] = {}

    class _LegacyClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    captured.update(kwargs)
                    message = type(
                        "Message",
                        (),
                        {
                            "content": (
                                '{"visual_page_type":"home","visual_summary":"首页",'
                                '"key_elements":[],"candidates":[]}'
                            )
                        },
                    )()
                    choice = type("Choice", (), {"message": message})()
                    return type("Response", (), {"choices": [choice]})()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "gpt-4o-mini", "provider": "custom", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _LegacyClient(),
    )

    await provider.understand(_request())

    assert captured["max_tokens"] == 2400
    assert "max_completion_tokens" not in captured


@pytest.mark.asyncio
async def test_llm_vision_removes_rejected_output_budget_and_retries() -> None:
    calls: list[dict[str, object]] = []

    class _RejectingClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    calls.append(kwargs)
                    if len(calls) == 1:
                        raise RuntimeError("Unsupported parameter: 'max_output_tokens'")
                    message = type(
                        "Message",
                        (),
                        {
                            "content": (
                                '{"visual_page_type":"home","visual_summary":"首页",'
                                '"key_elements":[],"candidates":[]}'
                            )
                        },
                    )()
                    choice = type("Choice", (), {"message": message})()
                    return type("Response", (), {"choices": [choice]})()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "gpt-5.6-terra", "provider": "custom", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _RejectingClient(),
    )

    with bind_run_llm_usage_meter() as meter:
        analysis = await provider.understand(_request())
        usage = meter.snapshot()

    assert analysis.provider_id == "heuristic-app-vision"
    assert analysis.surface_facts["visual_provider_quality"]["error_code"] == (
        "provider_no_candidates"
    )
    assert len(calls) == 2
    assert calls[0]["max_completion_tokens"] == 2400
    assert "max_tokens" not in calls[1]
    assert "max_completion_tokens" not in calls[1]
    assert usage is not None
    assert usage.ai_calls == 2
    assert usage.input_tokens is None
    assert usage.output_tokens is None


@pytest.mark.asyncio
async def test_llm_vision_records_openai_compatible_usage() -> None:
    class _UsageClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    message = type(
                        "Message",
                        (),
                        {
                            "content": (
                                '{"visual_page_type":"home","visual_summary":"首页",'
                                '"key_elements":["入口"],'
                                '"candidates":[{"semantic_name":"entry",'
                                '"action_type":"tap",'
                                '"normalized_bounds":{"left":0.1,"top":0.1,"right":0.3,"bottom":0.2},'
                                '"confidence":0.9,"risk_level":"low","purpose":"进入"}]}'
                            )
                        },
                    )()
                    choice = type("Choice", (), {"message": message})()
                    return type(
                        "Response",
                        (),
                        {
                            "choices": [choice],
                            "usage": {
                                "prompt_tokens": 100,
                                "completion_tokens": 20,
                                "total_tokens": 120,
                            },
                        },
                    )()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _UsageClient(),
    )

    with bind_run_llm_usage_meter() as meter:
        analysis = await provider.understand(_request())
        snapshot = meter.snapshot()

    assert analysis.provider_id == "llm-app-vision"
    assert snapshot is not None
    assert snapshot.ai_calls == 1
    assert snapshot.input_tokens == 100
    assert snapshot.output_tokens == 20
    assert snapshot.total_tokens == 120
    latency = analysis.surface_facts["visual_latency"]
    assert latency["protocol_version"] == "app-vision-provider-latency.v1"
    assert latency["capture_id"] == analysis.capture_id
    assert latency["retry_count"] == 0
    assert latency["attempt_count"] == 1
    assert latency["input_tokens"] == 100
    assert latency["output_tokens"] == 20
    assert latency["total_tokens"] == 120
    assert latency["error_code"] is None
    assert isinstance(latency["request_start_ms"], int)
    assert isinstance(latency["completed_ms"], int)
    assert latency["completed_ms"] >= 0


@pytest.mark.asyncio
async def test_llm_vision_streaming_records_first_token_ms() -> None:
    class _StreamChunk:
        def __init__(self, *, content: str | None = None, usage=None, finish_reason=None) -> None:
            delta = type("Delta", (), {"content": content})()
            choice = type("Choice", (), {"delta": delta, "finish_reason": finish_reason})()
            self.choices = [choice]
            self.usage = usage

    class _Stream:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield _StreamChunk(content='{"visual_page_type":"home",')
            yield _StreamChunk(
                content='"visual_summary":"首页","key_elements":[],"candidates":[]}',
                finish_reason="stop",
                usage={
                    "prompt_tokens": 11,
                    "completion_tokens": 7,
                    "total_tokens": 18,
                },
            )

    class _StreamClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    assert kwargs.get("stream") is True
                    return _Stream()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _StreamClient(),
    )

    analysis = await provider.understand(_request())
    latency = analysis.surface_facts["visual_latency"]
    assert latency["stream_mode"] is True
    assert isinstance(latency["first_token_ms"], int)
    assert latency["first_token_ms"] >= 0
    assert latency["completed_ms"] >= latency["first_token_ms"]
    assert latency["input_tokens"] == 11
    assert latency["output_tokens"] == 7


@pytest.mark.asyncio
async def test_llm_vision_retry_path_records_retry_count_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {"n": 0}

    class _TimeoutClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    calls["n"] += 1
                    error = TimeoutError("vision upstream 408")
                    error.status_code = 408  # type: ignore[attr-defined]
                    raise error

    async def _fake_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(
        "services.api.app.infrastructure.app_vision.llm.asyncio.sleep",
        _fake_sleep,
    )
    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _TimeoutClient(),
    )
    with bind_run_llm_usage_meter() as meter:
        analysis = await provider.understand(_request())
        usage = meter.snapshot()
    latency = analysis.surface_facts["visual_latency"]
    assert calls["n"] == 3
    assert latency["retry_count"] == 2
    assert latency["attempt_count"] == 3
    assert latency["error_code"] == "provider_call_failed"
    assert latency["error_type"] == "TimeoutError"
    assert len(latency["attempt_errors"]) == 3
    assert usage is not None
    assert usage.ai_calls == 3
    assert usage.input_tokens is None
    assert usage.output_tokens is None


@pytest.mark.asyncio
async def test_llm_vision_writes_latency_into_usage_event_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[dict] = []

    async def _fake_persist(payload):
        recorded.append(dict(payload))

    monkeypatch.setenv("ALIOTH_LLM_USAGE_AUDIT_ENABLED", "1")
    monkeypatch.setattr(
        "services.api.app.agents.runtime.usage_meter._persist_llm_usage_event",
        _fake_persist,
    )

    class _UsageClient:
        class chat:
            class completions:
                @staticmethod
                async def create(**_kwargs):
                    message = type(
                        "Message",
                        (),
                        {
                            "content": (
                                '{"visual_page_type":"home","visual_summary":"首页",'
                                '"key_elements":[],"candidates":[]}'
                            )
                        },
                    )()
                    choice = type("Choice", (), {"message": message})()
                    return type(
                        "Response",
                        (),
                        {
                            "choices": [choice],
                            "usage": {
                                "prompt_tokens": 5,
                                "completion_tokens": 3,
                                "total_tokens": 8,
                            },
                        },
                    )()

    provider = LlmAppVisionProvider(
        llm_settings=type(
            "S",
            (),
            {"model": "m", "provider": "openai", "api_style": "chat_completion"},
        )(),
        client_factory=lambda _settings: _UsageClient(),
    )
    await provider.understand(_request())
    await asyncio.sleep(0)

    assert recorded
    metadata = recorded[0]["metadata"]
    latency = metadata["app_vision_latency"]
    assert latency["protocol_version"] == "app-vision-provider-latency.v1"
    assert latency["input_tokens"] == 5
    assert latency["output_tokens"] == 3
    assert "request_started_at" in latency
    assert "completed_ms" in latency


def test_heuristic_escape_includes_scroll() -> None:
    from services.api.app.infrastructure.app_vision.heuristic import (
        build_first_screen_escape_analysis,
    )

    analysis = build_first_screen_escape_analysis(_request(), reason="test")
    assert analysis.candidates
    assert any(c.action_type is InteractionOperation.BACK for c in analysis.candidates)
    assert any(c.action_type is InteractionOperation.SCROLL for c in analysis.candidates)


def test_compress_png_bytes_returns_image() -> None:
    content, mime = compress_png_bytes_for_llm(_png_bytes(), max_side=64)
    assert content
    assert mime in {"image/jpeg", "image/png"}


def test_compress_png_keeps_side_limit_and_stronger_quality_shrinks() -> None:
    """分辨率策略不变（最长边≤max_side）；更低 JPEG 质量应更小。"""
    import io

    from PIL import Image

    src = Image.new("RGB", (1080, 1920), color=(32, 96, 160))
    # 加入纹理，避免纯色被压成几乎相同体积
    for y in range(0, 1920, 17):
        for x in range(0, 1080, 19):
            src.putpixel((x, y), ((x * 3) % 256, (y * 5) % 256, 120))
    buf = io.BytesIO()
    src.save(buf, format="PNG")
    png = buf.getvalue()

    loose, mime_a = compress_png_bytes_for_llm(png, max_side=720, quality=70)
    tight, mime_b = compress_png_bytes_for_llm(png, max_side=720, quality=50)
    assert mime_a == "image/jpeg"
    assert mime_b == "image/jpeg"
    assert len(tight) < len(loose)

    out = Image.open(io.BytesIO(tight))
    assert max(out.size) <= 720
    assert out.size == Image.open(io.BytesIO(loose)).size


def test_resolve_auto_fake_runtime_uses_fake() -> None:
    settings = Settings.model_validate(
        {
            "environment": "local",
            "workflow": {
                "app_runtime_provider": "fake",
                "app_vision_provider": "auto",
            },
        }
    )
    provider = resolve_app_vision_provider(settings)
    assert isinstance(provider, FakeAppVisionProvider)


def test_resolve_auto_official_uses_llm() -> None:
    settings = Settings.model_validate(
        {
            "environment": "local",
            "llm": {"api_key": "sk-test", "model": "gpt-test"},
            "workflow": {
                "app_runtime_provider": "official",
                "app_vision_provider": "auto",
            },
        }
    )
    provider = resolve_app_vision_provider(settings)
    assert isinstance(provider, LlmAppVisionProvider)


def test_resolve_rejects_fake_vision_for_production_official() -> None:
    settings = Settings.model_validate(
        {
            "environment": "production",
            "workflow": {
                "app_runtime_provider": "official",
                "app_vision_provider": "fake",
            },
        }
    )
    with pytest.raises(AppWorkflowNotConfiguredError, match="禁止使用 Fake"):
        resolve_app_vision_provider(settings)


@pytest.mark.parametrize("confidence", [None, True, -1, 2, "invalid"])
def test_new_llm_analysis_does_not_assume_missing_or_invalid_page_confidence(confidence) -> None:
    analysis = build_analysis_from_llm_payload(
        request=_request(),
        payload={
            "visual_page_type": "home",
            "visual_summary": "页面",
            "visual_page_type_confidence": confidence,
            "candidates": [],
        },
        page_types=get_default_page_type_registry(),
        model_id="test-model",
    )
    assert analysis.surface_facts["visual_page_type_confidence"] == 0.0
