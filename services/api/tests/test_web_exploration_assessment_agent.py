"""无工具 MAF Web 判定 Agent 的结构化输出合同。"""

import base64
import json

import pytest
from agent_framework import ChatResponse, Message
from agent_framework.exceptions import ChatClientException

from services.api.app.agents.explorer.web_assessment_agent import (
    WebExplorationAssessmentAgent,
)
from services.api.app.application.web_age_gate import WebAgeGateReviewRequiredError
from services.api.app.application.web_assessment_errors import (
    WebAssessmentTransientServiceError,
)
from services.api.app.application.web_assessment_evidence import (
    WebAssessmentEvidence,
)
from services.api.app.application.web_page_classification import (
    WebPageClassifier,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_page_readiness import (
    WebObstructionReviewRequiredError,
)
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.app.domain.page.web_site_menu_registry import WebPageClassificationHint
from services.api.tests.support.fake_chat_client import (
    ScriptedChatClient,
    make_text_response,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\nweb-assessment-test"
FULL_PAGE_PNG_BYTES = b"\x89PNG\r\n\x1a\nweb-full-page-supplemental-test"


class StaticEvidenceLoader:
    async def validate_same_capture_artifacts(self, *, artifact_types):
        assert tuple(artifact_types.values()) == (
            "screenshot",
            "dom",
            "ui_snapshot",
            "har",
            "action_trace",
        )
        return "task-1", "capture-ad"

    async def load(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        visible_text_artifact_id: str | None = None,
        supplemental_screenshot_artifact_ids: tuple[str, ...] = (),
    ) -> WebAssessmentEvidence:
        return WebAssessmentEvidence(
            screenshot_artifact_id=screenshot_artifact_id,
            screenshot_bytes=PNG_BYTES,
            screenshot_media_type="image/png",
            dom_artifact_id=dom_artifact_id,
            dom_text="<main><article>页面真实正文</article></main>",
            visible_text_artifact_id=visible_text_artifact_id,
            visible_text="页面真实可见文字" if visible_text_artifact_id else None,
            supplemental_screenshot_artifact_ids=supplemental_screenshot_artifact_ids,
            supplemental_screenshot_bytes=(FULL_PAGE_PNG_BYTES,)
            if supplemental_screenshot_artifact_ids
            else (),
            supplemental_screenshot_media_types=("image/png",)
            if supplemental_screenshot_artifact_ids
            else (),
        )


class SupplementalEvidenceLoader:
    """以独立字节模拟经谱系校验后的主图与长图证据。"""

    def __init__(self) -> None:
        self.requested_supplemental_ids: tuple[str, ...] | None = None

    async def load(
        self,
        *,
        screenshot_artifact_id: str,
        dom_artifact_id: str,
        visible_text_artifact_id: str | None = None,
        supplemental_screenshot_artifact_ids: tuple[str, ...] = (),
    ) -> WebAssessmentEvidence:
        self.requested_supplemental_ids = supplemental_screenshot_artifact_ids
        return WebAssessmentEvidence(
            screenshot_artifact_id=screenshot_artifact_id,
            screenshot_bytes=PNG_BYTES,
            screenshot_media_type="image/png",
            dom_artifact_id=dom_artifact_id,
            dom_text="<main><article>页面真实正文</article></main>",
            visible_text_artifact_id=visible_text_artifact_id,
            visible_text="页面真实可见文字" if visible_text_artifact_id else None,
            supplemental_screenshot_artifact_ids=supplemental_screenshot_artifact_ids,
            supplemental_screenshot_bytes=(FULL_PAGE_PNG_BYTES,)
            if supplemental_screenshot_artifact_ids
            else (),
            supplemental_screenshot_media_types=("image/png",)
            if supplemental_screenshot_artifact_ids
            else (),
        )


class ProviderParsingScriptedChatClient(ScriptedChatClient):
    """模拟真实 Provider 在 Agent.run 返回前绑定并解析 response_format。"""

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        response = await super()._inner_get_response(
            messages=messages,
            stream=stream,
            options=options,
            **kwargs,
        )
        return ChatResponse(
            messages=response.messages,
            response_format=options.get("response_format"),
        )


class FailingProviderChatClient(ScriptedChatClient):
    """模拟不可通过切换结构化解析模式恢复的 Provider 故障。"""

    def __init__(self) -> None:
        super().__init__([make_text_response("unused")])
        self.request_count = 0

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.request_count += 1
        raise ChatClientException("Error code: 401 - invalid api key")


def test_production_agent_preloads_workflow_without_exposing_skill_tools() -> None:
    wrapper = assessment_agent(ScriptedChatClient([make_text_response("{}")]))
    agent = wrapper.framework_agent

    assert agent.mcp_tools == []
    assert agent.context_providers == []


class UnavailableProviderChatClient(FailingProviderChatClient):
    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.request_count += 1
        raise ChatClientException("Error code: 503 - Service Unavailable")


class FirstUnavailableThenValidChatClient(ScriptedChatClient):
    """首次 503，第二次返回合法结构化响应。"""

    def __init__(self, response: ChatResponse) -> None:
        super().__init__([response])
        self.request_count = 0

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        self.request_count += 1
        if self.request_count == 1:
            self.received_messages.append(list(messages))
            self.received_options.append(dict(options))
            raise ChatClientException("Error code: 503 - Service Unavailable")
        return await super()._inner_get_response(
            messages=messages,
            stream=stream,
            options=options,
            **kwargs,
        )


def assessment_agent(client: ScriptedChatClient) -> WebExplorationAssessmentAgent:
    return WebExplorationAssessmentAgent(
        client=client,
        evidence_loader=StaticEvidenceLoader(),
    )


def first_user_payload(client: ScriptedChatClient) -> dict[str, object]:
    for message in client.received_messages[0]:
        if str(message.role) == "user":
            return json.loads(message.contents[0].text or "")
    raise AssertionError("未找到 user 消息")


def has_png_content(contents: list[object]) -> bool:
    encoded = base64.b64encode(PNG_BYTES).decode("ascii")
    return any(
        getattr(content, "uri", None) == f"data:image/png;base64,{encoded}" for content in contents
    )


@pytest.mark.asyncio
async def test_maf_agent_returns_schema_validated_candidate_id_only() -> None:
    candidate = {
        "candidate_id": "web-candidate:confirm-18",
        "capability": "web_age_gate_confirm",
        "text": "我已年满18岁并进入",
        "tag_name": "button",
        "role": "button",
        "rect": {"x": 760, "y": 600, "width": 400, "height": 60},
        "effective_z_index": 900,
        "visible": True,
        "hit_test_passed": True,
    }
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-age-gate-assessment.v1",
                        "gate_present": True,
                        "confirmation_candidate_id": "web-candidate:confirm-18",
                        "confidence": 0.98,
                        "reason": "页面要求确认已满18岁",
                        "human_verification_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(candidate,),
    )

    assert result.confirmation_candidate_id == "web-candidate:confirm-18"
    assert not hasattr(result, "selector")
    request_payload = first_user_payload(client)
    assert request_payload["candidates"][0]["candidate_id"] == ("web-candidate:confirm-18")
    assert "selector" not in request_payload["candidates"][0]
    assert request_payload["constraints"]["operator_is_adult"] is True
    assert request_payload["constraints"]["prefer_explicit_age_confirmation_candidate"] is True
    user_contents = [
        content
        for message in client.received_messages[0]
        if str(message.role) == "user"
        for content in message.contents
    ]
    assert has_png_content(user_contents)
    assert any("页面真实正文" in (content.text or "") for content in user_contents)
    instruction_text = "\n".join(
        content.text or ""
        for message in client.received_messages[0]
        if str(message.role) != "user"
        for content in message.contents
    )
    instruction_text += str(client.received_options[0].get("instructions") or "")
    assert "候选不是到达证明" in instruction_text
    offered_tool_names = {
        getattr(tool, "name", "") for tool in (client.received_options[0].get("tools") or ())
    }
    assert "load_skill" not in offered_tool_names


@pytest.mark.asyncio
async def test_maf_agent_accepts_one_json_code_fence_without_relaxing_schema() -> None:
    payload = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": False,
            "confirmation_candidate_id": None,
            "confidence": 0.96,
            "reason": "页面没有整站成年入口门禁",
            "human_verification_present": False,
        },
        ensure_ascii=False,
        indent=2,
    )
    client = ScriptedChatClient([make_text_response(f"```json\n{payload}\n```")])

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(),
    )

    assert result.gate_present is False
    assert result.confirmation_candidate_id is None


@pytest.mark.asyncio
async def test_maf_agent_accepts_provider_dangling_closing_json_fence() -> None:
    payload = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": False,
            "confirmation_candidate_id": None,
            "confidence": 0.96,
            "reason": "页面没有整站成年入口门禁",
            "human_verification_present": False,
        },
        ensure_ascii=False,
        indent=2,
    )
    client = ScriptedChatClient([make_text_response(f"{payload}\n```")])

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(),
    )

    assert result.gate_present is False
    assert result.confirmation_candidate_id is None


@pytest.mark.asyncio
async def test_maf_agent_accepts_identical_concatenated_provider_json() -> None:
    payload = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": False,
            "confirmation_candidate_id": None,
            "confidence": 0.96,
            "reason": "页面没有整站成年入口门禁",
            "human_verification_present": False,
        },
        ensure_ascii=False,
        indent=2,
    )
    client = ScriptedChatClient([make_text_response(f"{payload}\n{payload}")])

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(),
    )

    assert result.gate_present is False


@pytest.mark.asyncio
async def test_maf_agent_parses_only_final_assistant_message_after_tool_rounds() -> None:
    """AgentResponse.text 会拼接多轮消息，结构化判定只能读取最后回复。"""

    obsolete = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": True,
            "confirmation_candidate_id": "obsolete-candidate",
            "confidence": 0.7,
            "reason": "工具调用前的中间判断",
            "human_verification_present": False,
        },
        ensure_ascii=False,
    )
    final = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": False,
            "confirmation_candidate_id": None,
            "confidence": 0.96,
            "reason": "读取流程技能后的最终判断",
            "human_verification_present": False,
        },
        ensure_ascii=False,
    )
    client = ScriptedChatClient(
        [
            ChatResponse(
                messages=[
                    Message("assistant", [obsolete]),
                    Message("assistant", [final]),
                ]
            )
        ]
    )

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(),
    )

    assert result.gate_present is False
    assert result.reason == "读取流程技能后的最终判断"


@pytest.mark.asyncio
async def test_maf_agent_accepts_nine_identical_provider_json_echoes() -> None:
    """真实供应商多次回显时仍只消费同一份严格对象。"""

    payload = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": False,
            "confirmation_candidate_id": None,
            "confidence": 0.96,
            "reason": "页面没有整站成年入口门禁",
            "human_verification_present": False,
        },
        ensure_ascii=False,
        indent=2,
    )
    client = ScriptedChatClient([make_text_response("\n".join([payload] * 9))])

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(),
    )

    assert result.gate_present is False


@pytest.mark.asyncio
async def test_maf_agent_rejects_conflicting_concatenated_provider_json() -> None:
    first = {
        "schema_version": "web-age-gate-assessment.v1",
        "gate_present": False,
        "confirmation_candidate_id": None,
        "confidence": 0.96,
        "reason": "页面没有整站成年入口门禁",
        "human_verification_present": False,
    }
    second = {**first, "gate_present": True, "confirmation_candidate_id": "candidate"}
    payload = f"{json.dumps(first, ensure_ascii=False)}\n{json.dumps(second, ensure_ascii=False)}"
    client = ScriptedChatClient([make_text_response(payload), make_text_response(payload)])

    with pytest.raises(ValueError):
        await assessment_agent(client).assess(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
        )


@pytest.mark.asyncio
async def test_maf_agent_retries_provider_preparse_without_sdk_response_format() -> None:
    payload = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": False,
            "confirmation_candidate_id": None,
            "confidence": 0.97,
            "reason": "页面没有整站成年入口门禁",
            "human_verification_present": False,
        },
        ensure_ascii=False,
    )
    client = ProviderParsingScriptedChatClient(
        [
            make_text_response(f"```json\n{payload}\n```"),
            make_text_response(f"```json\n{payload}\n```"),
        ]
    )

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(),
    )

    assert result.gate_present is False
    assert "response_format" in client.received_options[0]
    assert "response_format" not in client.received_options[1]
    retry_payload = next(
        json.loads(message.contents[0].text or "")
        for message in client.received_messages[1]
        if str(message.role) == "user"
    )
    assert retry_payload["response_schema"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_maf_agent_does_not_retry_provider_failure_as_json_fallback() -> None:
    client = FailingProviderChatClient()

    with pytest.raises(ChatClientException, match="401"):
        await assessment_agent(client).assess(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
        )

    assert client.request_count == 1


@pytest.mark.asyncio
async def test_maf_agent_marks_exhausted_503_as_transient_service_error() -> None:
    client = UnavailableProviderChatClient()

    with pytest.raises(WebAssessmentTransientServiceError, match="503"):
        await assessment_agent(client).assess(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
        )

    assert client.request_count == 2


@pytest.mark.asyncio
async def test_maf_agent_retries_503_without_dropping_response_format() -> None:
    payload = json.dumps(
        {
            "schema_version": "web-age-gate-assessment.v1",
            "gate_present": False,
            "confirmation_candidate_id": None,
            "confidence": 0.96,
            "reason": "页面没有整站成年入口门禁",
            "human_verification_present": False,
        },
        ensure_ascii=False,
    )
    client = FirstUnavailableThenValidChatClient(make_text_response(payload))

    result = await assessment_agent(client).assess(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(),
    )

    assert result.gate_present is False
    assert client.request_count == 2
    assert "response_format" in client.received_options[0]
    assert "response_format" in client.received_options[1]


@pytest.mark.asyncio
async def test_maf_agent_rejects_prose_around_json_code_fence() -> None:
    fenced = (
        "结果如下：\n```json\n"
        '{"schema_version":"web-age-gate-assessment.v1",'
        '"gate_present":false,"confirmation_candidate_id":null,'
        '"confidence":0.96,"reason":"没有门禁",'
        '"human_verification_present":false}\n```'
    )
    client = ScriptedChatClient([make_text_response(fenced), make_text_response(fenced)])

    with pytest.raises(ValueError):
        await assessment_agent(client).assess(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
        )


@pytest.mark.asyncio
async def test_maf_agent_rejects_candidate_outside_supplied_closed_set() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-age-gate-assessment.v1",
                        "gate_present": True,
                        "confirmation_candidate_id": "web-candidate:invented",
                        "confidence": 0.99,
                        "reason": "模型构造了不存在的候选",
                        "human_verification_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    with pytest.raises(ValueError, match="不属于当前候选闭集"):
        await assessment_agent(client).assess(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
        )


@pytest.mark.asyncio
async def test_maf_agent_age_gate_without_safe_confirmation_requires_review() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-age-gate-assessment.v1",
                        "gate_present": True,
                        "confirmation_candidate_id": None,
                        "confidence": 0.91,
                        "reason": "正文包含成人内容，误判为入口门禁",
                        "human_verification_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    assessor = assessment_agent(client)

    with pytest.raises(WebAgeGateReviewRequiredError):
        await assessor.assess(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
        )


@pytest.mark.asyncio
async def test_maf_agent_returns_only_current_obstruction_candidate_id() -> None:
    candidate = {
        "candidate_id": "web-candidate:close-modal",
        "capability": "web_obstruction_dismiss",
        "text": "关闭广告",
        "tag_name": "button",
        "role": "button",
        "rect": {"x": 1800, "y": 30, "width": 50, "height": 50},
        "effective_z_index": 900,
        "visible": True,
        "hit_test_passed": True,
    }
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": True,
                        "dismiss_candidate_id": "web-candidate:close-modal",
                        "confidence": 0.97,
                        "reason": "高层弹窗遮挡页面主体",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    result = await assessment_agent(client).assess_obstruction(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(candidate,),
        page_role="menu",
    )

    assert result.dismiss_candidate_id == "web-candidate:close-modal"
    assert "selector" not in first_user_payload(client)["candidates"][0]


@pytest.mark.asyncio
async def test_maf_agent_uses_unique_safe_dismiss_candidate_when_model_omits_id() -> None:
    candidate = {
        "candidate_id": "web-candidate:close-modal",
        "capability": "web_obstruction_dismiss",
        "text": "关闭",
        "tag_name": "img",
        "role": None,
        "rect": {"x": 942.41, "y": 750.75, "width": 35.19, "height": 35.19},
        "effective_z_index": 100,
        "visible": True,
        "hit_test_passed": True,
    }
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": True,
                        "dismiss_candidate_id": None,
                        "confidence": 0.98,
                        "reason": "截图存在居中广告遮挡，但未回填关闭候选 ID",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    result = await assessment_agent(client).assess_obstruction(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(candidate,),
        page_role="home",
    )

    assert result.dismiss_candidate_id == "web-candidate:close-modal"


@pytest.mark.asyncio
async def test_maf_agent_retries_one_invalid_obstruction_envelope() -> None:
    candidate = {
        "candidate_id": "web-candidate:close-modal",
        "capability": "web_obstruction_dismiss",
        "text": "关闭广告",
        "tag_name": "button",
        "role": "button",
        "rect": {"x": 1800, "y": 30, "width": 50, "height": 50},
        "effective_z_index": 900,
        "visible": True,
        "hit_test_passed": True,
    }
    client = ScriptedChatClient(
        [
            make_text_response("{invalid-json"),
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": False,
                        "dismiss_candidate_id": None,
                        "confidence": 0.97,
                        "reason": "页面无遮挡",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )

    result = await assessment_agent(client).assess_obstruction(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(candidate,),
        page_role="detail",
    )

    assert result.obstruction_present is False
    assert len(client.received_messages) == 2


@pytest.mark.asyncio
async def test_maf_agent_retries_one_contradictory_obstruction_envelope() -> None:
    candidate = {
        "candidate_id": "web-candidate:close-modal",
        "capability": "web_obstruction_dismiss",
        "text": "关闭广告",
        "tag_name": "button",
        "role": "button",
        "rect": {"x": 1800, "y": 30, "width": 50, "height": 50},
        "effective_z_index": 900,
        "visible": True,
        "hit_test_passed": True,
    }
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": False,
                        "dismiss_candidate_id": "web-candidate:close-modal",
                        "confidence": 0.97,
                        "reason": "页面无遮挡但错误返回了关闭候选",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            ),
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": False,
                        "dismiss_candidate_id": None,
                        "confidence": 0.98,
                        "reason": "页面无遮挡",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            ),
        ]
    )

    result = await assessment_agent(client).assess_obstruction(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(candidate,),
        page_role="detail",
    )

    assert result.obstruction_present is False
    assert len(client.received_messages) == 2


@pytest.mark.asyncio
async def test_maf_agent_ordinary_obstruction_without_safe_candidate_requires_review() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": True,
                        "dismiss_candidate_id": None,
                        "confidence": 0.91,
                        "reason": "截图看起来像存在覆盖层",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    with pytest.raises(WebObstructionReviewRequiredError):
        await assessment_agent(client).assess_obstruction(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
            page_role="home",
        )


@pytest.mark.asyncio
async def test_maf_agent_ordinary_obstruction_outside_closed_set_requires_review() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": True,
                        "dismiss_candidate_id": "web-candidate:invented",
                        "confidence": 0.91,
                        "reason": "截图存在阻断，但模型返回了候选闭集外 ID",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    with pytest.raises(WebObstructionReviewRequiredError):
        await assessment_agent(client).assess_obstruction(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=(),
            page_role="home",
        )


@pytest.mark.asyncio
async def test_maf_agent_ordinary_obstruction_with_ambiguous_candidates_requires_review() -> None:
    candidates = tuple(
        {
            "candidate_id": f"web-candidate:close-{index}",
            "capability": "web_obstruction_dismiss",
            "text": "关闭",
            "tag_name": "button",
            "role": "button",
            "rect": {"x": index * 50, "y": 20, "width": 40, "height": 40},
            "effective_z_index": 900,
            "visible": True,
            "hit_test_passed": True,
        }
        for index in (1, 2)
    )
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-obstruction-assessment.v1",
                        "obstruction_present": True,
                        "dismiss_candidate_id": None,
                        "confidence": 0.91,
                        "reason": "存在阻断，但两个关闭候选无法消歧",
                        "human_verification_present": False,
                        "age_gate_present": False,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    with pytest.raises(WebObstructionReviewRequiredError) as exc_info:
        await assessment_agent(client).assess_obstruction(
            screenshot_artifact_id="artifact-screenshot",
            dom_artifact_id="artifact-dom",
            candidates=candidates,
            page_role="home",
        )

    assert exc_info.value.reason_code == "WEB_OBSTRUCTION_CANDIDATES_AMBIGUOUS"


@pytest.mark.asyncio
async def test_maf_agent_returns_schema_validated_web_page_assessment() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "content_detail",
                        "confidence": 0.95,
                        "evidence": ["DOM 存在单一正文容器", "截图主体为一篇文章"],
                        "needs_review_reason": None,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    result = await assessment_agent(client).classify(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        allowed_page_types=(
            "video_list",
            "video_detail",
            "content_list",
            "content_detail",
        ),
        page_type_catalog=(
            {"code": "video_list"},
            {"code": "video_detail"},
            {"code": "content_list"},
            {"code": "content_detail"},
        ),
        page_role="detail",
        structure_facts={
            "repeated_primary_card_count": 0,
            "article_body_count": 1,
            "video_element_count": 0,
        },
        classification_hint={
            "expected_page_type": "content_detail",
            "confidence": 0.86,
            "source": "operator_verified_menu_classification",
            "mixed_content": False,
            "authoritative": False,
        },
    )

    assert result.page_type == "content_detail"
    assert result.evidence == ("DOM 存在单一正文容器", "截图主体为一篇文章")
    user_contents = [
        content
        for message in client.received_messages[0]
        if str(message.role) == "user"
        for content in message.contents
    ]
    assert has_png_content(user_contents)
    assert any("页面真实可见文字" in (content.text or "") for content in user_contents)
    request = first_user_payload(client)
    assert request["structure_facts"]["article_body_count"] == 1
    assert request["classification_hint"]["authoritative"] is False
    assert "conflict_requires_review" not in request["constraints"]
    assert request["constraints"]["classification_hint_is_advisory_only"] is True
    assert request["constraints"]["current_visible_layer_must_be_business"] is True
    assert request["constraints"]["strong_negative_signals_override_advisory_hint"] is True


@pytest.mark.asyncio
async def test_maf_agent_classifies_persisted_isolated_ad_page_from_artifact_ids() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "ad_page",
                        "confidence": 0.96,
                        "evidence": ["截图与 DOM 显示独立广告落地内容"],
                        "needs_review_reason": None,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    result = await assessment_agent(client).assess_ad_page(
        screenshot_artifact_id="artifact-landing-screenshot",
        dom_artifact_id="artifact-landing-dom",
        ui_snapshot_artifact_id="artifact-landing-ui",
        har_artifact_id="artifact-landing-har",
        action_trace_artifact_id="artifact-landing-trace",
    )

    assert result.page_type == "ad_page"
    assert result.confidence == 0.96
    request = first_user_payload(client)
    assert request["assessment_kind"] == "web_ad_page_type"
    assert request["allowed_page_types"] == ["ad_page"]
    assert request["artifact_refs"] == {
        "ui_snapshot": "artifact-landing-ui",
        "har": "artifact-landing-har",
        "action_trace": "artifact-landing-trace",
    }


@pytest.mark.asyncio
async def test_maf_agent_assesses_guide_from_same_capture_evidence_only() -> None:
    client = ScriptedChatClient([
        make_text_response(json.dumps({
            "schema_version": "web-page-assessment.v1",
            "page_type": "guide_page",
            "confidence": 0.92,
            "evidence": ["落地页可见应用下载入口"],
            "needs_review_reason": None,
        }, ensure_ascii=False)),
    ])

    class GuideEvidenceLoader(StaticEvidenceLoader):
        async def validate_same_capture_artifacts(self, *, artifact_types):
            assert tuple(artifact_types.values()) == (
                "screenshot", "dom", "ui_snapshot", "action_trace",
            )
            return "task-1", "capture-guide"

    agent = WebExplorationAssessmentAgent(
        client=client, evidence_loader=GuideEvidenceLoader(),
    )
    result = await agent.assess_guide_page(
        screenshot_artifact_id="guide-shot", dom_artifact_id="guide-dom",
        ui_snapshot_artifact_id="guide-ui", action_trace_artifact_id="guide-trace",
        final_url="https://guide.example/app?private=secret",
    )

    assert result.page_type == "guide_page"
    request = first_user_payload(client)
    assert request["assessment_kind"] == "web_guide_page_type"
    assert request["allowed_page_types"] == ["guide_page"]
    assert request["screenshot_artifact_id"] == "guide-shot"
    assert request["dom_artifact_id"] == "guide-dom"
    assert request["landing_host"] == "guide.example"
    assert request["landing_path"] == "/app"
    assert "secret" not in repr(request)


@pytest.mark.asyncio
async def test_maf_agent_keeps_full_page_png_out_of_page_type_request() -> None:
    """即使调用方传入长图引用，页型模型也只能读取 ready 主证据。"""

    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "content_detail",
                        "confidence": 0.95,
                        "evidence": ["主图显示单一正文"],
                        "needs_review_reason": None,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    loader = SupplementalEvidenceLoader()
    agent = WebExplorationAssessmentAgent(
        client=client,
        evidence_loader=loader,
    )

    result = await agent.classify(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        supplemental_screenshot_artifact_ids=("artifact-full-page",),
        allowed_page_types=("content_detail",),
        page_type_catalog=({"code": "content_detail"},),
        page_role="detail",
        structure_facts={"article_body_count": 1},
        classification_hint=None,
    )

    assert result.page_type == "content_detail"
    assert loader.requested_supplemental_ids == ()
    user_contents = [
        content
        for message in client.received_messages[0]
        if str(message.role) == "user"
        for content in message.contents
    ]
    image_uris = [
        uri for content in user_contents if (uri := getattr(content, "uri", None)) is not None
    ]
    assert image_uris == [
        f"data:image/png;base64,{base64.b64encode(PNG_BYTES).decode('ascii')}",
    ]
    assert first_user_payload(client)["supplemental_screenshot_artifact_ids"] == []


@pytest.mark.parametrize(
    "review_reason",
    ["目录提示与 DOM 主体冲突", "目录提示与 DOM 主体冲突" * 30],
    ids=["short", "long"],
)
@pytest.mark.asyncio
async def test_production_assessment_agent_unresolved_conflict_keeps_candidate_as_unknown(
    review_reason,
) -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "content_detail",
                        "confidence": 0.96,
                        "evidence": ["DOM 存在单一长正文主体"],
                        "needs_review_reason": review_reason,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    capture = WebReadyPageCapture(
        capture_id="capture-conflict",
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        page_role="menu",
        structure_facts=WebPageStructureFacts(
            navigation_element_count=1,
            repeated_link_count=0,
            video_element_count=0,
            single_subject_element_count=1,
            dom_tokens=("article", "article-body"),
            article_body_count=1,
            paragraph_text_length=420,
        ),
        classification_hint=WebPageClassificationHint(
            expected_page_type="video_list",
            confidence=0.9,
            source="operator_verified_menu_classification",
        ),
    )

    result = await WebPageClassifier(
        assessor=assessment_agent(client),
        catalog=load_web_page_type_catalog(),
    ).classify(capture)

    assert result.page_type == result.proposed_page_type == "unknown"
    assert result.needs_review is True
    assert result.observed_page_type == "content_detail"
    assert result.expected_page_type == "video_list"
    assert result.conflict_reason_code == "URL_HINT_DOM_CONFLICT"
    assert result.diagnostic_message == review_reason[:200]
    request = first_user_payload(client)
    assert request["navigation_signals"]["common_positive_signals"]
    assert request["common_site_observations"]["navigation"]
    assert "navigation_structure_facts" in request
    assert request["constraints"]["navigation_cannot_override_deterministic_rules"] is True


@pytest.mark.asyncio
async def test_production_assessment_agent_conflict_cannot_bypass_structure_gate() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "content_detail",
                        "confidence": 0.99,
                        "evidence": ["模型声称正文完整"],
                        "needs_review_reason": "目录提示与模型输出冲突",
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    capture = WebReadyPageCapture(
        capture_id="capture-unsupported",
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        page_role="menu",
        structure_facts=WebPageStructureFacts(
            navigation_element_count=3,
            repeated_link_count=0,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("navigation",),
        ),
        classification_hint=WebPageClassificationHint(
            expected_page_type="video_list",
            confidence=0.9,
            source="operator_verified_menu_classification",
        ),
    )

    result = await WebPageClassifier(
        assessor=assessment_agent(client),
        catalog=load_web_page_type_catalog(),
    ).classify(capture)

    assert result.page_type == "unknown"
    assert result.proposed_page_type == "unknown"
    assert result.needs_review_reason == "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"


@pytest.mark.asyncio
async def test_production_agent_preserves_safe_candidate_for_out_of_catalog_output() -> None:
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "custom token=secret-value detail",
                        "confidence": 0.73,
                        "evidence": ["模型提出目录外候选"],
                        "needs_review_reason": "需要人工确认 password=hunter2",
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    result = await assessment_agent(client).classify(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        allowed_page_types=("video_list", "content_detail"),
        page_type_catalog=(
            {"code": "video_list"},
            {"code": "content_detail"},
        ),
        page_role="menu",
        structure_facts={"article_body_count": 0},
        classification_hint=None,
    )

    assert result.page_type is None
    assert result.raw_page_type == "custom token=[redacted] detail"
    assert result.proposed_page_type == "custom token=[redacted] detail"
    assert result.needs_review_reason == "WEB_PAGE_TYPE_UNSUPPORTED"
    assert result.diagnostic_message == "需要人工确认 password=[redacted]"


@pytest.mark.asyncio
async def test_maf_agent_selects_detail_candidate_without_url_or_selector() -> None:
    candidate = {
        "candidate_id": "web-candidate:detail-1",
        "expected_page_type": "video_detail",
        "evidence": ["同源可见重复卡片"],
        "text": "视频标题",
        "rect": {"x": 100, "y": 200, "width": 300, "height": 180},
    }
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-detail-selection.v1",
                        "selected_candidate_id": "web-candidate:detail-1",
                        "confidence": 0.96,
                        "reason": "首个可见重复视频卡片",
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )

    result = await assessment_agent(client).select_detail_candidate(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        candidates=(candidate,),
        list_page_type="video_list",
    )

    assert result.selected_candidate_id == "web-candidate:detail-1"
    request_candidate = first_user_payload(client)["candidates"][0]
    assert "selector" not in request_candidate
    assert "canonical_url" not in request_candidate
    user_contents = [
        content
        for message in client.received_messages[0]
        if str(message.role) == "user"
        for content in message.contents
    ]
    assert has_png_content(user_contents)
