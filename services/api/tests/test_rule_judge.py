import asyncio
import json
from contextlib import contextmanager
from dataclasses import asdict, fields

import pytest

from services.api.app.agents.rule_judge.normalization import normalize_rule_judge_outcome
from services.api.app.agents.runtime import inspection_result_writer
from services.api.app.application.observation_packet import (
    ObservationPacket,
    ObservationPacketValidationError,
)
from services.api.app.application.rule_judge import (
    RuleEngineConfigurationError,
    RuleEngineConfigurationGuard,
    RuleJudgeDefinition,
    RuleJudgeEvidenceInsufficientError,
    RuleJudgeNotConfiguredError,
    RuleJudgeRequest,
    RuleJudgeResult,
    RuleJudgeService,
    RuleJudgeValidationError,
    _hybrid_preflight_result,
    _packet_has_screenshot,
    resolve_rule_execution_route,
    rule_execution_route_payload,
    validate_enabled_rule_engine_configuration,
)
from services.api.app.application.semantic_rule_judge import (
    SemanticJudgeModelOutput,
)
from services.api.app.domain.inspection import (
    EvidenceBundleReference,
    InspectionHandoff,
    InspectionRuleTarget,
)
from services.api.app.domain.task import TaskExecutionMode
from services.api.app.infrastructure.db.observation_packet_repository import (
    _ensure_frozen_packet_matches,
)


class FakeRuleJudgeRepository:
    def __init__(self, definition: RuleJudgeDefinition | None) -> None:
        self.definition = definition
        self.calls: list[tuple[str, str]] = []

    async def get_rule_judge_definition(
        self, *, task_id: str, rule_code: str
    ) -> RuleJudgeDefinition | None:
        self.calls.append((task_id, rule_code))
        return self.definition


class FakeRuleEngineConfigurationRepository:
    def __init__(self, definitions: list[RuleJudgeDefinition]) -> None:
        self.definitions = definitions
        self.calls = 0

    async def list_enabled_rule_judge_definitions(self) -> list[RuleJudgeDefinition]:
        self.calls += 1
        await asyncio.sleep(0)
        return self.definitions


class FakeRuleJudgeEvidenceReader:
    def __init__(
        self,
        visible_text: str,
        action_trace: object | None = None,
        json_artifacts: dict[str, object] | None = None,
        role_artifacts: dict[str, dict[str, object]] | None = None,
    ) -> None:
        self.visible_text = visible_text
        self.action_trace = [{"action": "observe"}] if action_trace is None else action_trace
        self.json_artifacts = json_artifacts or {}
        self.role_artifacts = role_artifacts or {}
        self.calls: list[dict[str, object]] = []

    async def prepare_observation_packet(
        self,
        *,
        task_id: str,
        agent_run_id: str | None,
        source_agent_run_id: str | None,
        rule_id: str,
        page_capture_id: str,
        evidence_capture_roles: dict[str, str] | None,
        evidence_artifact_ids: tuple[str, ...],
        required_artifact_types: tuple[str, ...],
        frozen_observation_packet=None,
    ) -> object:
        self.calls.append(
            {
                "task_id": task_id,
                "agent_run_id": agent_run_id,
                "source_agent_run_id": source_agent_run_id,
                "rule_id": rule_id,
                "page_capture_id": page_capture_id,
                "evidence_capture_roles": evidence_capture_roles,
                "evidence_artifact_ids": evidence_artifact_ids,
                "required_artifact_types": required_artifact_types,
            }
        )
        return FakeEvidenceBundle(
            self.visible_text,
            self.action_trace,
            self.json_artifacts,
            self.role_artifacts,
        )


class MissingRequiredEvidenceReader:
    async def prepare_observation_packet(self, **kwargs):
        del kwargs
        raise RuleJudgeEvidenceInsufficientError("规则判定缺少必需证据类型：action_trace")


class FakeEvidenceArtifact:
    def __init__(self, visible_text: str, *, capture_role: str = "primary") -> None:
        self.visible_text = visible_text
        self.capture_role = capture_role

    def decode_text(self) -> str:
        if not self.visible_text.strip():
            raise RuleJudgeEvidenceInsufficientError("visible_text 证据内容为空")
        return self.visible_text


class FakeObservationPacketService:
    async def build(self, request) -> ObservationPacket:
        return ObservationPacket(
            observation_packet_id="packet-1",
            task_id=request.task_id,
            page_capture_id=request.page_capture_id,
            rule_id=request.rule_id,
            rule_code=request.rule_code,
            evidence_artifact_ids=("artifact-1",),
            packet_payload={
                "rule": {"rule_code": request.rule_code},
                "page": {
                    "page_capture_id": request.page_capture_id,
                    "url": "https://example.com",
                },
                "facts": {
                    "visible_text": "页面文案",
                    "ui_snapshot": {"element_count": 1, "elements": []},
                    "screenshot_count": 1,
                    "action_log": [],
                    "network_summary": {"failed_responses": []},
                    "console_summary": {"errors": []},
                },
                "quality_gate": {"status": "pass", "missing_sources": []},
            },
            quality_gate_status="pass",
            quality_gate_reason=None,
        )


class PageStateObservationPacketService(FakeObservationPacketService):
    def __init__(self, page_state: str) -> None:
        self.page_state = page_state

    async def build(self, request) -> ObservationPacket:
        packet = await super().build(request)
        packet.packet_payload["page"]["page_state"] = self.page_state
        return packet


class JourneyObservationPacketService(FakeObservationPacketService):
    async def build(self, request) -> ObservationPacket:
        packet = await super().build(request)
        packet.packet_payload["facts"]["action_log"] = [
            {
                "action": "click",
                "ad_navigation_evidence": {
                    "schema_version": "ad-navigation-evidence.v1",
                    "status": "observed",
                    "navigation_mode": "same_page",
                    "pair_complete": True,
                    "source": {
                        "url": "https://example.com/source",
                        "screenshot_ref": "ad_landing_consistency#frame-0",
                    },
                    "landing": {
                        "url": "https://example.com/landing",
                        "screenshot_ref": "ad_landing_consistency#frame-1",
                    },
                },
            }
        ]
        packet.packet_payload["facts"]["screenshot_evidence"] = [
            {
                "artifact_id": "artifact-ad-pair",
                "purpose": "ad_landing_consistency",
                "frame_count": 2,
                "frames": [
                    {
                        "index": 0,
                        "role": "source",
                        "artifact_ref": "artifact-ad-pair#frame-0",
                        "logical_ref": "ad_landing_consistency#frame-0",
                        "payload_valid": True,
                    },
                    {
                        "index": 1,
                        "role": "landing",
                        "artifact_ref": "artifact-ad-pair#frame-1",
                        "logical_ref": "ad_landing_consistency#frame-1",
                        "payload_valid": True,
                    },
                ],
            }
        ]
        return packet


class CompleteSurfaceObservationPacketService(FakeObservationPacketService):
    def __init__(
        self,
        visible_text: str,
        *,
        elements: list[dict[str, object]] | None = None,
        visible_text_truncated: bool = False,
    ) -> None:
        self.visible_text = visible_text
        self.elements = elements or []
        self.visible_text_truncated = visible_text_truncated

    async def build(self, request) -> ObservationPacket:
        packet = await super().build(request)
        facts = packet.packet_payload["facts"]
        facts["visible_text"] = self.visible_text
        facts["visible_text_character_count"] = len(self.visible_text)
        facts["visible_text_truncated"] = self.visible_text_truncated
        facts["ui_snapshot"] = {
            "element_count": len(self.elements),
            "elements": self.elements,
            "element_limit_reached": False,
        }
        facts["dom_summary"] = {
            "character_count": max(len(self.visible_text), 1),
            "tag_counts": {"main": 1},
            "elements": self.elements,
            "element_limit_reached": False,
        }
        packet.packet_payload["page"]["page_state"] = "ready"
        return packet


class FakeSemanticJudge:
    async def judge(self, *, packet, tool_results):
        del packet, tool_results
        return SemanticJudgeModelOutput(
            conclusion="pass",
            confidence=0.91,
            reasoning_summary="页面文案满足规则。",
            evidence_refs=["artifact-1"],
        )


class SemanticJudgeMustNotRun:
    def __init__(self) -> None:
        self.call_count = 0

    async def judge(self, *, packet, tool_results):
        del packet, tool_results
        self.call_count += 1
        raise AssertionError("确定性预检已形成终态时不应调用语义模型")


class CountingSemanticJudge(FakeSemanticJudge):
    def __init__(self) -> None:
        self.call_count = 0

    async def judge(self, *, packet, tool_results):
        self.call_count += 1
        return await super().judge(packet=packet, tool_results=tool_results)


class FakeJsonArtifact:
    def __init__(self, payload: object, *, capture_role: str = "primary") -> None:
        self.payload = payload
        self.capture_role = capture_role

    def decode_json(self) -> object:
        return self.payload


class FakeBinaryArtifact:
    def __init__(self, payload: bytes, *, capture_role: str = "primary") -> None:
        self.payload = payload
        self.capture_role = capture_role
        self.content_type = "image/png"


class FakeEvidenceBundle:
    observation_packet_id = "packet-1"

    def __init__(
        self,
        visible_text: str,
        action_trace: object,
        json_artifacts: dict[str, object],
        role_artifacts: dict[str, dict[str, object]],
    ) -> None:
        self.visible_text = visible_text
        self.action_trace = action_trace
        self.json_artifacts = json_artifacts
        self.role_artifacts = role_artifacts

    def require_unique(self, artifact_type: str) -> object:
        if artifact_type == "visible_text":
            return FakeEvidenceArtifact(self.visible_text)
        if artifact_type == "screenshot":
            return FakeBinaryArtifact(self.json_artifacts[artifact_type])
        return FakeJsonArtifact(self.json_artifacts[artifact_type])

    def artifacts_of_type(self, artifact_type: str) -> tuple[object, ...]:
        role_matches: list[object] = []
        for role, artifacts in self.role_artifacts.items():
            if artifact_type not in artifacts:
                continue
            payload = artifacts[artifact_type]
            role_matches.append(
                FakeEvidenceArtifact(str(payload), capture_role=role)
                if artifact_type == "visible_text"
                else FakeBinaryArtifact(payload, capture_role=role)
                if artifact_type == "screenshot"
                else FakeJsonArtifact(payload, capture_role=role)
            )
        if role_matches:
            return tuple(role_matches)
        if artifact_type == "visible_text":
            return (FakeEvidenceArtifact(self.visible_text),)
        if artifact_type == "action_trace":
            return (FakeJsonArtifact(self.action_trace),)
        if artifact_type in self.json_artifacts:
            if artifact_type == "screenshot":
                return (FakeBinaryArtifact(self.json_artifacts[artifact_type]),)
            return (FakeJsonArtifact(self.json_artifacts[artifact_type]),)
        return ()


def _definition(*, rule_code: str, handler: str) -> RuleJudgeDefinition:
    return RuleJudgeDefinition(
        rule_id=f"rule-{rule_code}",
        rule_code=rule_code,
        executor_type="rule_engine",
        judge_config={
            "schema_version": "1.0",
            "engine_config": {"handler": handler},
        },
    )


def _request(
    *,
    rule_code: str,
    judge_input: dict,
    evidence_capture_roles: dict[str, str] | None = None,
) -> RuleJudgeRequest:
    return RuleJudgeRequest(
        task_id="task-1",
        rule_code=rule_code,
        page_capture_id="capture-1",
        evidence_artifact_ids=("artifact-1",),
        judge_input=judge_input,
        evidence_capture_roles=evidence_capture_roles,
    )


async def test_rule_judge_detects_technical_error_text() -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_6",
            rule_code="alioth_6",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "technical_error_text",
                    "pattern": "JSON Parse Error|HTTP\\s*500|NullPointerException",
                }
            },
        )
    )
    evidence_reader = FakeRuleJudgeEvidenceReader("HTTP 500")
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=evidence_reader,
    )

    result = await service.judge(
        _request(rule_code="alioth_6", judge_input={"visible_text": "模型总结不可信"})
    )

    assert result.execution_status == "fail"
    assert result.judge_strategy == "rule_engine"
    assert result.confidence == 1.0
    assert result.page_capture_id == "capture-1"
    assert result.evidence_artifact_ids == ("artifact-1",)
    assert result.judge_output["assertion"]["passed"] is False
    assert result.judge_output["assertion"]["match_details"] == {
        "matches": [{"text": "HTTP 500", "start": 0, "end": 8}],
        "truncated": False,
    }
    assert result.judge_input["visible_text"] == "HTTP 500"
    assert evidence_reader.calls == [
        {
                "task_id": "task-1",
                "agent_run_id": None,
                "source_agent_run_id": None,
            "rule_id": "rule-alioth_6",
            "page_capture_id": "capture-1",
            "evidence_capture_roles": None,
            "evidence_artifact_ids": ("artifact-1",),
            "required_artifact_types": ("visible_text",),
        }
    ]
    assert result.observation_packet_id == "packet-1"


async def test_rule_judge_converts_executor_empty_evidence_to_insufficient() -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_6",
            rule_code="alioth_6",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "technical_error_text",
                    "pattern": "HTTP\\s*500",
                }
            },
        )
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader(""),
    )

    result = await service.judge(_request(rule_code="alioth_6", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_strategy == "rule_engine"
    assert result.observation_packet_id == "packet-1"
    assert result.evidence_artifact_ids == ("artifact-1",)
    assert "证据内容为空" in (result.problem_desc or "")


async def test_rule_judge_returns_truncated_text_summary() -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_6",
            rule_code="alioth_6",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "technical_error_text",
                    "pattern": "HTTP\\s*500",
                }
            },
        )
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader("正常页面" * 300),
    )

    result = await service.judge(
        _request(rule_code="alioth_6", judge_input={"visible_text": "模型摘要"})
    )

    assert len(result.judge_input["visible_text"]) == 500
    assert result.judge_input["visible_text_truncated"] is True
    assert result.judge_input["visible_text_length"] == 1200


async def test_rule_judge_ignores_negative_model_summary_for_technical_error() -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_6",
            rule_code="alioth_6",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "technical_error_text",
                    "pattern": "HTTP\\s*500|NullPointerException",
                }
            },
        )
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader("页面加载正常，欢迎访问"),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_6",
            judge_input={"visible_text": "未见 HTTP 500 或 NullPointerException 等技术错误"},
        )
    )

    assert result.execution_status == "pass"
    assert result.judge_input["visible_text"] == "页面加载正常，欢迎访问"
    assert result.judge_output["assertion"]["passed"] is True


async def test_rule_judge_compares_discount_price_with_original_price() -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_14",
            rule_code="alioth_14",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "discount_price_compare",
                    "discount_price_field": "discount_price",
                    "original_price_field": "original_price",
                    "operator": "lt",
                }
            },
        )
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader("价格页面"),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_14",
            judge_input={"discount_price": 8.8, "original_price": 10},
        )
    )

    assert result.execution_status == "pass"
    assert result.judge_output["assertion"]["details"] == {
        "left": 8.8,
        "operator": "lt",
        "right": 10.0,
    }


async def test_rule_judge_marks_missing_discount_prices_as_evidence_insufficient() -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_14",
            rule_code="alioth_14",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "discount_price_compare",
                    "operator": "lt",
                }
            },
        )
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader("price page without numeric prices"),
    )

    result = await service.judge(_request(rule_code="alioth_14", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert "discount_price" in (result.problem_desc or "")
    assert "original_price" in (result.problem_desc or "")


async def test_rule_judge_passes_payment_page_without_discount_marker() -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_14",
            rule_code="alioth_14",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "discount_price_compare",
                    "operator": "lt",
                    "pass_when_discount_absent": True,
                    "discount_marker_pattern": r"(?:优惠价|活动价)",
                }
            },
        )
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader("会员充值 请选择充值金额"),
    )

    result = await service.judge(_request(rule_code="alioth_14", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["applicability"] == "condition_not_triggered"


@pytest.mark.parametrize("invalid_value", [True, float("nan"), float("inf")])
async def test_rule_judge_rejects_non_finite_or_boolean_price(
    invalid_value: object,
) -> None:
    repository = FakeRuleJudgeRepository(
        RuleJudgeDefinition(
            rule_id="rule-alioth_14",
            rule_code="alioth_14",
            executor_type="rule_engine",
            judge_config={
                "engine_config": {
                    "handler": "discount_price_compare",
                    "operator": "lt",
                }
            },
        )
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader("价格页面"),
    )

    with pytest.raises(RuleJudgeValidationError, match="数值"):
        await service.judge(
            _request(
                rule_code="alioth_14",
                judge_input={"discount_price": invalid_value, "original_price": 10},
            )
        )


async def test_rule_judge_validates_qr_url_with_injected_validator() -> None:
    validator_calls: list[dict] = []

    async def validator(*, url: str, **kwargs) -> dict:
        validator_calls.append({"url": url, **kwargs})
        return {"is_valid": False, "issues": ["链接不可达"], "url_checks": []}

    repository = FakeRuleJudgeRepository(
        _definition(rule_code="alioth_41", handler="qr_url_validation")
    )
    service = RuleJudgeService(
        repository=repository,
        evidence_reader=FakeRuleJudgeEvidenceReader("二维码页面"),
        qr_validator=validator,
    )

    result = await service.judge(
        _request(rule_code="alioth_41", judge_input={"url": "https://example.com/qr"})
    )

    assert validator_calls == [
        {
            "url": "https://example.com/qr",
            "timeout_seconds": 8.0,
            "max_redirects": 5,
        }
    ]
    assert result.execution_status == "fail"
    assert result.problem_desc == "链接不可达"
    assert result.judge_output["validation"]["is_valid"] is False


async def test_rule_judge_extracts_qr_url_from_trusted_ui_snapshot() -> None:
    validator_calls: list[dict] = []

    async def validator(*, url: str, **kwargs) -> dict:
        validator_calls.append({"url": url, **kwargs})
        return {
            "is_valid": True,
            "issues": [],
            "url_checks": [{"is_valid": True}],
        }

    evidence_reader = FakeRuleJudgeEvidenceReader(
        "请扫描二维码",
        json_artifacts={
            "ui_snapshot": {
                "elements": [
                    {
                        "selector": "#credential-qr",
                        "alt": "身份凭证二维码",
                        "src": "https://example.com/credential",
                    }
                ]
            },
            "screenshot": b"\x89PNG\r\n\x1a\nfake",
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=evidence_reader,
        qr_validator=validator,
    )

    result = await service.judge(_request(rule_code="alioth_41", judge_input={}))

    assert validator_calls == [
        {
            "url": "https://example.com/credential",
            "timeout_seconds": 8.0,
            "max_redirects": 5,
        }
    ]
    assert result.execution_status == "pass"
    # 二维码规则不硬性要求任一材料：文字、快照、截图任一都能找目标，读取器按可选取
    assert evidence_reader.calls[0]["required_artifact_types"] == ()


async def test_rule_judge_decodes_qr_from_trusted_screenshot() -> None:
    validator_calls: list[dict] = []

    async def validator(*, image_base64: str, **kwargs) -> dict:
        validator_calls.append({"image_base64": image_base64, **kwargs})
        return {
            "is_valid": True,
            "decoded_texts": ["https://example.com/credential"],
            "issues": [],
            "url_checks": [{"is_valid": True}],
        }

    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "身份凭证",
            json_artifacts={
                "ui_snapshot": {"elements": []},
                "screenshot": b"\x89PNG\r\n\x1a\nfake",
            },
        ),
        qr_validator=validator,
    )

    result = await service.judge(_request(rule_code="alioth_41", judge_input={}))

    assert len(validator_calls) == 1
    assert validator_calls[0]["timeout_seconds"] == 8.0
    assert validator_calls[0]["max_redirects"] == 5
    assert result.execution_status == "pass"


async def test_rule_judge_skips_qr_whose_content_is_not_a_url() -> None:
    """身份凭证卡上的二维码存的是账号凭证，扫得出来但不是网址：不能按网址失效判未通过。"""

    async def validator(*, image_base64: str, **kwargs) -> dict:
        del image_base64, kwargs
        return {
            "input_type": "image",
            "is_valid": False,
            "decoded_texts": ["ACCOUNT-CREDENTIAL-7f3a9c"],
            "decoded_urls": [],
            "issues": ["未从二维码中识别出可访问的 http/https URL"],
            "url_checks": [],
        }

    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "身份凭证",
            json_artifacts={
                "ui_snapshot": {"elements": []},
                "screenshot": b"\x89PNG\r\n\x1a\nfake",
            },
        ),
        qr_validator=validator,
    )

    result = await service.judge(_request(rule_code="alioth_41", judge_input={}))

    assert result.execution_status == "skipped"
    assert "内容不是网址" in result.problem_desc


async def test_rule_judge_decodes_qr_when_visible_text_is_empty() -> None:
    """二维码弹窗页可信文本合法为空时，判定必须继续走快照与截图解码兜底。"""
    validator_calls: list[dict] = []

    async def validator(*, image_base64: str, **kwargs) -> dict:
        validator_calls.append({"image_base64": image_base64, **kwargs})
        return {
            "is_valid": True,
            "decoded_texts": ["https://example.com/credential"],
            "issues": [],
            "url_checks": [{"is_valid": True}],
        }

    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "",
            json_artifacts={
                "ui_snapshot": {
                    "elements": [{"class_tokens": ["qr-dialog"], "text": ""}]
                },
                "screenshot": b"\x89PNG\r\n\x1a\nfake",
            },
        ),
        qr_validator=validator,
    )

    result = await service.judge(_request(rule_code="alioth_41", judge_input={}))

    assert len(validator_calls) == 1
    assert result.execution_status == "pass"


async def test_rule_judge_default_qr_validator_accepts_screenshot_payload(monkeypatch) -> None:
    from services.api.app.agents.tools import qr_link_validation_tool as qr_tool_module

    validator_calls: list[dict] = []

    async def fake_validate_qr_link(**kwargs) -> dict:
        validator_calls.append(kwargs)
        return {
            "is_valid": True,
            "decoded_texts": ["https://example.com/credential"],
            "issues": [],
            "url_checks": [{"is_valid": True}],
        }

    monkeypatch.setattr(qr_tool_module, "validate_qr_link", fake_validate_qr_link)
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "scan QR code",
            json_artifacts={
                "ui_snapshot": {"elements": []},
                "screenshot": b"\x89PNG\r\n\x1a\nfake",
            },
        ),
    )

    result = await service.judge(_request(rule_code="alioth_41", judge_input={}))

    assert result.execution_status == "pass"
    assert validator_calls == [
        {
            "url": None,
            "image_base64": "iVBORw0KGgpmYWtl",
            "image_path": None,
            "timeout_seconds": 8.0,
            "max_redirects": 5,
        }
    ]


async def test_rule_judge_skips_qr_rule_when_page_has_no_qr_semantics() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader("个人中心 设置 身份凭证卡"),
    )

    result = await service.judge(_request(rule_code="alioth_41", judge_input={}))

    assert result.execution_status == "skipped"
    assert result.judge_output["applicability"] == "not_applicable"


async def test_rule_judge_marks_qr_without_target_url_as_evidence_insufficient() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader("请扫码查看二维码"),
    )

    result = await service.judge(_request(rule_code="alioth_41", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert "缺少可验证的目标网址" in (result.problem_desc or "")


async def test_rule_judge_rejects_malformed_qr_validator_result() -> None:
    async def validator(**kwargs) -> dict:
        del kwargs
        return {"is_valid": True, "issues": [], "url_checks": []}

    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_41", handler="qr_url_validation")
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader("二维码页面"),
        qr_validator=validator,
    )

    with pytest.raises(RuleJudgeNotConfiguredError, match="URL 校验明细不一致"):
        await service.judge(
            _request(
                rule_code="alioth_41",
                judge_input={"url": "https://example.com/qr"},
            )
        )


async def test_rule_judge_evaluates_collection_constraint() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_39",
        rule_code="alioth_39",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "collection_constraint",
                "constraint": "monotonic",
                "input_field": "ranking_values",
                "direction": "descending",
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader("榜单页面"),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_39",
            judge_input={"ranking_values": [100, 80, 90]},
        )
    )

    assert result.execution_status == "fail"
    assert result.judge_output["violations"] == [
        {"left_index": 1, "left": 80.0, "right_index": 2, "right": 90.0}
    ]


def _ranking_definition() -> RuleJudgeDefinition:
    """与 alioth_39 种子同形：agent_structured 的 ranking_items 没人给时靠页面文字。"""
    return RuleJudgeDefinition(
        rule_id="rule-alioth_39",
        rule_code="alioth_39",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "collection_constraint",
                "constraint": "monotonic",
                "input_field": "ranking_items",
                "value_field": "metric",
                "direction": "descending",
                "strict": False,
                "min_items": 2,
            }
        },
    )


_HOT_LIST_TEXT = (
    "最新\n热播\n福利姬\n颜值女神\n最新\n最热\n免费\n极品女友 哇噻\n104w\n00:12:11\n"
    "【顶级身材极品女友】\n免费\n顶流人造极品美女\n102w\n00:22:57\n免费\n绝美 女神\n100w\n"
    "00:49:29\n热门\n影片\n"
)


async def test_monotonic_collection_constraint_reads_ranking_from_visible_text() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(_HOT_LIST_TEXT),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["values"] == [1040000.0, 1020000.0, 1000000.0]
    assert result.judge_output["items_source"] == "visible_text"
    assert result.judge_output["items"][0]["name"] == "极品女友 哇噻"


async def test_monotonic_collection_constraint_ignores_bare_numbers_and_durations() -> None:
    text = "排行榜\n限时 4 7 4 天\n4\n7\n0\n00:12:11\n9689\n视频A\n1.2万次播放\n视频B\n8,000次播放\n"
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(text),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["values"] == [12000.0, 8000.0]


async def test_monotonic_collection_constraint_fails_disordered_ranking_page() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader("热榜\n视频A\n1.2万\n视频B\n3.4万\n"),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "fail"
    assert result.judge_output["violations"] == [
        {"left_index": 0, "left": 12000.0, "right_index": 1, "right": 34000.0}
    ]


async def test_monotonic_collection_constraint_reports_missing_ranking_data() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader("周榜\n第一名 视频A\n第二名 视频B\n"),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert "缺榜单数据" in str(result.problem_desc)


async def test_monotonic_collection_constraint_skips_page_without_ranking_semantics() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader("首页\n视频A\n1.2万\n视频B\n3.4万\n"),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "skipped"
    assert result.judge_output["applicability"] == "not_applicable"


async def test_monotonic_collection_constraint_skips_unsorted_list_with_only_hot_tabs() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader("最新\n最热\n视频A\n1.2w\n视频B\n3.4w\n"),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "skipped"
    assert "最热" in str(result.problem_desc)
    assert "12000、34000" in str(result.problem_desc)


async def test_monotonic_collection_constraint_prefers_structured_ranking_input() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader("热榜\n视频A\n1.2万\n视频B\n3.4万\n"),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_39",
            judge_input={
                "ranking_items": [
                    {"name": "视频A", "metric": 100},
                    {"name": "视频B", "metric": 90},
                ]
            },
        )
    )

    assert result.execution_status == "pass"
    assert result.judge_output["values"] == [100.0, 90.0]
    assert "items_source" not in result.judge_output


async def test_more_button_numbers_are_not_ranking_items() -> None:
    text = "热榜\n视频A\n3.4万\n视频B\n1.2万\n更多\n9.9万\n查看更多\n8万次播放\n"
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(text),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["values"] == [34000.0, 12000.0]
    assert [item["name"] for item in result.judge_output["items"]] == ["视频A", "视频B"]


async def test_ranking_page_without_content_metric_pairs_is_insufficient() -> None:
    text = "排行榜\n更多\n12.8万\n查看更多\n3.4万次播放\n"
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(text),
    )

    result = await service.judge(_request(rule_code="alioth_39", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output["gap_codes"] == ["ranking_content_metric_unbound"]
    assert "不能当作榜单项" in str(result.problem_desc)


async def test_structured_ranking_metrics_without_content_are_not_judged() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_ranking_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader("热榜\n视频A\n1.2万\n视频B\n3.4万\n"),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_39",
            judge_input={"ranking_items": [{"metric": 100}, {"name": "更多", "metric": 90}]},
        )
    )

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output["gap_codes"] == ["ranking_content_metric_unbound"]
    assert result.execution_status != "fail"

def _case_style_definition() -> RuleJudgeDefinition:
    """与 alioth_38 同形：分类标签大小写一致性。"""
    return RuleJudgeDefinition(
        rule_id="rule-alioth_38",
        rule_code="alioth_38",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "collection_constraint",
                "constraint": "case_style_consistency",
                "input_field": "labels",
                "min_items": 2,
            }
        },
    )


_NAV_SNAPSHOT = {
    "elements": [
        {"text": "最新", "within_navigation": True},
        {"text": "AV", "within_navigation": True},
        {"text": "VIP", "within_navigation": True},
        {"text": "HOT", "role": "tab"},
        {"text": "PG娱乐", "within_navigation": False},
        {"text": "iXX直播", "within_navigation": False},
    ]
}
# 页面文字把导航标签和广告瓷片混在一起：只看文字会把 iXX直播 当分类标签而误判不一致
_NAV_VISIBLE_TEXT = "最新\nAV\nVIP\nHOT\nPG娱乐\niXX直播\n"


async def test_case_style_compares_navigation_labels_only_when_snapshot_present() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_case_style_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            _NAV_VISIBLE_TEXT, json_artifacts={"ui_snapshot": _NAV_SNAPSHOT}
        ),
    )

    result = await service.judge(_request(rule_code="alioth_38", judge_input={}))

    assert result.execution_status == "pass"
    assert [item["value"] for item in result.judge_output["items"]] == ["AV", "VIP", "HOT"]


async def test_case_style_flags_inconsistent_navigation_labels() -> None:
    snapshot = {
        "elements": [
            {"text": "AV", "within_navigation": True},
            {"text": "Hot", "within_navigation": True},
            {"text": "PG娱乐", "within_navigation": False},
        ]
    }
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_case_style_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "AV\nHot\nPG娱乐\n", json_artifacts={"ui_snapshot": snapshot}
        ),
    )

    result = await service.judge(_request(rule_code="alioth_38", judge_input={}))

    assert result.execution_status == "fail"


async def test_case_style_not_applicable_when_snapshot_shows_no_category_bar() -> None:
    # 内容列表页：快照标明没有导航 / 分类栏，页面文字里只有内容标题和 Banner 广告文字，
    # 不能拿它们凑分类标签去判大小写（原来会退回整页文字误报「不一致」转人工）
    snapshot = {
        "elements": [
            {"text": "抖音百万 coser 喵喵球现身机车展 H罩杯的极致身材", "within_navigation": False},
            {"text": "Dernit Paradise Hotel", "within_navigation": False, "is_advertisement": True},
        ]
    }
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_case_style_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "抖音百万 coser 喵喵球现身机车展 H罩杯的极致身材\nDernit Paradise Hotel\nAI短剧\n",
            json_artifacts={"ui_snapshot": snapshot},
        ),
    )

    result = await service.judge(_request(rule_code="alioth_38", judge_input={}))

    assert result.execution_status == "skipped"


async def test_case_style_falls_back_to_visible_text_without_structure() -> None:
    # 只有截图、没有界面快照（如视觉路 App）时仍按页面文字提取
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_case_style_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader("AV\nHot\n"),
    )

    result = await service.judge(_request(rule_code="alioth_38", judge_input={}))

    assert result.execution_status == "fail"


async def test_case_style_ignores_advertisement_tabs_in_navigation() -> None:
    # 广告弹窗里的页签也会被认成导航，但不是产品的分类标签
    snapshot = {
        "elements": [
            {"text": "AV", "within_navigation": True},
            {"text": "Hot", "within_navigation": True, "is_advertisement": True},
        ]
    }
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(_case_style_definition()),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "AV\nHot\n", json_artifacts={"ui_snapshot": snapshot}
        ),
    )

    result = await service.judge(_request(rule_code="alioth_38", judge_input={}))

    assert result.execution_status == "skipped"

_AD_LANDING_TOOLS = [
    {"tool_name": "safe_click_observer_tool", "status": "insufficient", "facts": {}},
    {"tool_name": "landing_page_probe_tool", "status": "insufficient", "facts": {}},
    {"tool_name": "visual_semantic_compare_tool", "status": "insufficient", "facts": {}},
]


def _ad_landing_definition() -> RuleJudgeDefinition:
    return RuleJudgeDefinition(
        rule_id="rule-alioth_17",
        rule_code="alioth_17",
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": "ad-landing"}},
    )


def _ad_landing_packet(*, screenshot_count: int = 2) -> ObservationPacket:
    return ObservationPacket(
        observation_packet_id="packet-17",
        task_id="task-1",
        page_capture_id="capture-1",
        rule_id="rule-alioth_17",
        rule_code="alioth_17",
        evidence_artifact_ids=(),
        packet_payload={
            "page": {"surface_kind": "web"},
            "facts": {"screenshot_count": screenshot_count},
        },
        quality_gate_status="pass",
        quality_gate_reason=None,
    )


def test_ad_landing_preflight_short_circuits_without_visual_fallback() -> None:
    result = _hybrid_preflight_result(
        definition=_ad_landing_definition(),
        request=_request(rule_code="alioth_17", judge_input={}),
        packet=_ad_landing_packet(),
        tool_results=_AD_LANDING_TOOLS,
        allow_visual_fallback=False,
    )

    assert result is not None
    assert result.execution_status == "evidence_insufficient"


def test_ad_landing_preflight_defers_to_model_with_visual_fallback() -> None:
    result = _hybrid_preflight_result(
        definition=_ad_landing_definition(),
        request=_request(rule_code="alioth_17", judge_input={}),
        packet=_ad_landing_packet(),
        tool_results=_AD_LANDING_TOOLS,
        allow_visual_fallback=True,
    )

    assert result is None


def test_packet_has_screenshot_reads_count() -> None:
    assert _packet_has_screenshot(_ad_landing_packet(screenshot_count=2)) is True
    assert _packet_has_screenshot(_ad_landing_packet(screenshot_count=0)) is False

async def test_collection_constraint_without_applicable_labels_is_skipped() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_38",
        rule_code="alioth_38",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "collection_constraint",
                "constraint": "case_style_consistency",
                "input_field": "labels",
                "min_items": 2,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader("首页\n精选\n8s后关闭"),
    )

    result = await service.judge(_request(rule_code="alioth_38", judge_input={}))

    assert result.execution_status == "skipped"
    assert result.judge_output["applicability"] == "not_applicable"


async def test_balanced_collection_constraint_without_labels_vacuously_passes() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_38",
        rule_code="alioth_38",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "collection_constraint",
                "constraint": "case_style_consistency",
                "input_field": "labels",
                "min_items": 2,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader("首页\n精选\n8s后关闭"),
        verdict_mode="balanced",
    )

    result = await service.judge(_request(rule_code="alioth_38", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["applicability"] == "vacuous_pass"
    assert result.judge_output["policy_mode"] == "balanced"


async def test_rule_judge_derives_required_ui_capabilities_from_trusted_text() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_45",
        rule_code="alioth_45",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "required_ui_capability",
                "required_all_of": ["upload_limit", "format_disclosure"],
                "required_any_groups": [["multi_select", "single_select_disclosed"]],
                "capability_patterns": {
                    "upload_limit": "最多\\s*9\\s*张",
                    "format_disclosure": "JPG|PNG",
                    "multi_select": "多选",
                },
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader("图片最多 9 张，支持 JPG、PNG，可多选"),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_45",
            judge_input={"observed_capabilities": []},
        )
    )

    assert result.execution_status == "pass"
    assert result.judge_output["matched_from_visible_text"] == [
        "format_disclosure",
        "multi_select",
        "upload_limit",
    ]


async def test_rule_judge_passes_visible_text_presence_rule() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_9",
        rule_code="alioth_9",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "visible_text_presence",
                "pattern": r"(?:评论|点赞)\s*\d+",
                "absence_conclusion": "evidence_insufficient",
            }
        },
        input_contract={"required_capture_roles": ["primary"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "评论 12 点赞 8",
            role_artifacts={"primary": {"visible_text": "评论 12 点赞 8"}},
        ),
    )

    result = await service.judge(_request(rule_code="alioth_9", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["matched"] is True


async def test_rule_judge_can_use_trusted_ui_snapshot_text_for_numeric_indicator() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_9",
        rule_code="alioth_9",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "visible_text_presence",
                "pattern": r"(?m)^\s*\d+\s*$",
                "include_ui_snapshot_text": True,
                "absence_conclusion": "evidence_insufficient",
            }
        },
        input_contract={"required_capture_roles": ["primary"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "[]",
            role_artifacts={
                "primary": {
                    "visible_text": "[]",
                    "ui_snapshot": {
                        "elements": [
                            {"text": "最高点赞"},
                            {"text": "8"},
                            {"src": "https://example.com/2026/image.png"},
                        ]
                    },
                }
            },
        ),
    )

    result = await service.judge(_request(rule_code="alioth_9", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["match"] == "8"


async def test_rule_judge_marks_missing_visible_text_pattern_as_insufficient() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_18",
        rule_code="alioth_18",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "visible_text_presence",
                "pattern": r"\d+\s*(?:秒|s)",
                "absence_conclusion": "evidence_insufficient",
            }
        },
        input_contract={"required_capture_roles": ["primary"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "广告页面",
            role_artifacts={"primary": {"visible_text": "广告页面"}},
        ),
    )

    result = await service.judge(_request(rule_code="alioth_18", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert "未命中" in (result.problem_desc or "")

async def test_visible_text_presence_ignores_empty_unrequested_before_role() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_18",
        rule_code="alioth_18",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "visible_text_presence",
                "capture_role": "primary",
                "pattern": "关闭",
                "absence_conclusion": "evidence_insufficient",
            }
        },
        input_contract={"required_capture_roles": ["primary"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "关闭",
            role_artifacts={
                "before": {"visible_text": ""},
                "primary": {"visible_text": "关闭"},
            },
        ),
    )

    result = await service.judge(_request(rule_code="alioth_18", judge_input={}))

    assert result.execution_status == "pass"



async def test_rule_judge_passes_when_before_after_screenshots_differ() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_20",
        rule_code="alioth_20",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "screenshot_change_assertion",
                "before_role": "before",
                "after_role": "primary",
            }
        },
        input_contract={"required_capture_roles": ["before", "primary"]},
    )
    evidence_reader = FakeRuleJudgeEvidenceReader(
        "弹窗广告",
        role_artifacts={
            "before": {"screenshot": b"before-image"},
            "primary": {"screenshot": b"after-image"},
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=evidence_reader,
    )

    result = await service.judge(
        _request(
            rule_code="alioth_20",
            judge_input={},
            evidence_capture_roles={
                "before": "capture-before",
                "primary": "capture-after",
            },
        )
    )

    assert result.execution_status == "pass"
    assert result.judge_output["changed"] is True
    assert evidence_reader.calls[0]["evidence_capture_roles"] == {
        "before": "capture-before",
        "primary": "capture-after",
    }


async def test_rule_judge_ignores_screenshot_sequence_json_for_role_uniqueness() -> None:
    """时序截图沿用 screenshot 类型，但不能冒充角色的规范 PNG。"""

    class Artifact:
        def __init__(self, *, role: str, payload: bytes, content_type: str) -> None:
            self.capture_role = role
            self.payload = payload
            self.content_type = content_type

    class EvidenceBundle:
        observation_packet_id = "packet-1"
        artifacts: tuple[object, ...] = ()

        def artifacts_of_type(self, artifact_type: str) -> tuple[object, ...]:
            if artifact_type != "screenshot":
                return ()
            return (
                Artifact(role="before", payload=b"before-image", content_type="image/png"),
                Artifact(role="primary", payload=b"after-image", content_type="image/png"),
                Artifact(
                    role="primary",
                    payload=b'{"sequence":true,"frames":[]}',
                    content_type="application/json",
                ),
            )

    class EvidenceReader:
        async def prepare_observation_packet(self, **kwargs: object) -> object:
            del kwargs
            return EvidenceBundle()

    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_20",
        rule_code="alioth_20",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "screenshot_change_assertion",
                "before_role": "before",
                "after_role": "primary",
            }
        },
        input_contract={"required_capture_roles": ["before", "primary"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=EvidenceReader(),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_20",
            judge_input={},
            evidence_capture_roles={
                "before": "capture-before",
                "primary": "capture-after",
            },
        )
    )

    assert result.execution_status == "pass"
    assert result.judge_output["changed"] is True


async def test_rule_judge_drops_extra_before_role_for_primary_only_contract() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_19",
        rule_code="alioth_19",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "geometry_hit_area_assertion",
                "min_width": 1.0,
                "min_height": 1.0,
                "require_enabled": True,
            }
        },
        input_contract={"required_capture_roles": ["primary"]},
    )
    evidence_reader = FakeRuleJudgeEvidenceReader(
        "跳过",
        json_artifacts={
            "ui_snapshot": {
                "elements": [
                    {
                        "element_index": 0,
                        "text": "跳过",
                        "disabled": False,
                        "hit_bounds": {
                            "x": 0,
                            "y": 0,
                            "width": 20,
                            "height": 20,
                        },
                        "visual_bounds": {
                            "x": 0,
                            "y": 0,
                            "width": 20,
                            "height": 20,
                        },
                    }
                ]
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=evidence_reader,
    )

    result = await service.judge(
        _request(
            rule_code="alioth_19",
            judge_input={},
            evidence_capture_roles={
                "before": "capture-before",
                "primary": "capture-1",
            },
        )
    )

    assert result.execution_status == "pass"
    assert evidence_reader.calls[0]["evidence_capture_roles"] == {"primary": "capture-1"}


async def test_rule_judge_rejects_forbidden_ui_action_from_trusted_trace() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_48",
        rule_code="alioth_48",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "required_ui_capability",
                "required_all_of": ["logout_entry"],
                "capability_patterns": {"logout_entry": "退出登录"},
                "forbidden_action_pattern": "退出登录|logout|sign.?out",
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "设置 退出登录",
            action_trace=[{"action": "click", "selector": "text=退出登录"}],
        ),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_48",
            judge_input={"observed_capabilities": []},
        )
    )

    assert result.execution_status == "fail"
    assert result.judge_output["forbidden_action_matched"] is True


async def test_rule_judge_evaluates_trusted_temporal_trace() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_18",
        rule_code="alioth_18",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "temporal_sequence_assertion",
                "value_pattern": "倒计时\\s*(\\d+)",
                "expected_rate_per_second": -1,
                "tolerance": 0.1,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "倒计时",
            json_artifacts={
                "temporal_trace": {
                    "clock": "monotonic",
                    "samples": [
                        {"offset_ms": 0, "visible_text": "倒计时 3"},
                        {"offset_ms": 1000, "visible_text": "倒计时 2"},
                        {"offset_ms": 2000, "visible_text": "倒计时 1"},
                    ],
                }
            },
        ),
    )

    result = await service.judge(_request(rule_code="alioth_18", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["expected_rate_per_second"] == -1.0


async def test_rule_judge_marks_missing_count_values_as_evidence_insufficient() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_18",
        rule_code="alioth_18",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "temporal_sequence_assertion",
                "value_pattern": r"(?P<value>\d+)\s*s",
                "expected_rate_per_second": -1,
                "tolerance": 0.1,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "close",
            json_artifacts={
                "temporal_trace": {
                    "clock": "monotonic",
                    "samples": [
                        {"offset_ms": 1000, "visible_text": "close\nhome content"},
                        {"offset_ms": 2000, "visible_text": "close\nhome content"},
                        {"offset_ms": 3000, "visible_text": "close\nhome content"},
                    ],
                    "countdown_events": [
                        {"kind": "end", "offset_ms": 0.8, "text": "close", "value": None}
                    ],
                }
            },
        ),
    )

    result = await service.judge(_request(rule_code="alioth_18", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output["conclusion"] == "evidence_insufficient"
    assert "倒计时计数样本" in result.problem_desc


async def test_rule_judge_evaluates_trusted_geometry_snapshot() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_19",
        rule_code="alioth_19",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "geometry_hit_area_assertion",
                "min_width": 44,
                "min_height": 44,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "跳过",
            json_artifacts={
                "ui_snapshot": {
                    "elements": [
                        {
                            "element_index": 2,
                            "text": "跳过",
                            "disabled": False,
                            "hit_bounds": {
                                "x": 10,
                                "y": 10,
                                "width": 48,
                                "height": 48,
                            },
                            "visual_bounds": {
                                "x": 14,
                                "y": 14,
                                "width": 32,
                                "height": 32,
                            },
                        }
                    ]
                }
            },
        ),
    )

    result = await service.judge(_request(rule_code="alioth_19", judge_input={"element_index": 2}))

    assert result.execution_status == "pass"
    assert result.judge_output["element_index"] == 2


async def test_geometry_rule_auto_locates_control_in_timed_snapshot() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_19",
        rule_code="alioth_19",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "geometry_hit_area_assertion",
                "min_width": 1,
                "min_height": 1,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "8s后关闭",
            json_artifacts={
                "ui_snapshot": {
                    "sequence": True,
                    "frames": [
                        {
                            "elements": [
                                {
                                    "text": "8s后关闭",
                                    "class_tokens": ["down-time"],
                                    "rect": {
                                        "x": 10,
                                        "y": 10,
                                        "width": 82,
                                        "height": 28,
                                    },
                                }
                            ]
                        }
                    ],
                }
            },
        ),
    )

    result = await service.judge(_request(rule_code="alioth_19", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["element_index"] == 0


async def test_rule_judge_evaluates_external_link_navigation_trace() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_7",
        rule_code="alioth_7",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "external_link_open_mode",
                "expected_mode": "new_window",
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "广告",
            action_trace=[
                {
                    "action": "click",
                    "selector": "a.external-ad",
                    "navigation": {
                        "before_url": "https://product.example/home",
                        "after_url": "https://product.example/home",
                        "new_page_count": 1,
                        "new_page_urls": ["https://advertiser.example/landing"],
                    },
                }
            ],
        ),
    )

    result = await service.judge(_request(rule_code="alioth_7", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["expected_mode"] == "new_window"


async def test_rule_judge_maps_missing_required_artifact_to_evidence_insufficient() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_7",
        rule_code="alioth_7",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "external_link_open_mode",
                "expected_mode": "new_window",
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=MissingRequiredEvidenceReader(),
    )

    result = await service.judge(_request(rule_code="alioth_7", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_strategy == "rule_engine"
    assert result.evidence_artifact_ids == ("artifact-1",)
    assert "action_trace" in (result.problem_desc or "")
    assert result.judge_output["conclusion"] == "evidence_insufficient"


async def test_external_link_rule_skips_page_without_external_target() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_7",
        rule_code="alioth_7",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "external_link_open_mode",
                "expected_mode": "new_window",
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "普通业务页面",
            action_trace=[{"action": "observe"}],
            json_artifacts={"ui_snapshot": {"elements": []}},
        ),
    )

    result = await service.judge(_request(rule_code="alioth_7", judge_input={}))

    assert result.execution_status == "skipped"
    assert result.confidence == 1.0
    assert result.judge_output["applicability"] == "not_applicable"


async def test_balanced_external_link_rule_without_target_vacuously_passes() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_7",
        rule_code="alioth_7",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "external_link_open_mode",
                "expected_mode": "new_window",
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "普通业务页面",
            action_trace=[{"action": "observe"}],
            json_artifacts={"ui_snapshot": {"elements": []}},
        ),
        verdict_mode="balanced",
    )

    result = await service.judge(_request(rule_code="alioth_7", judge_input={}))

    assert result.execution_status == "pass"
    assert result.judge_output["applicability"] == "vacuous_pass"
    assert result.judge_output["policy_mode"] == "balanced"


async def test_multi_capture_reports_insufficient_without_required_role() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_40",
        rule_code="alioth_40",
        executor_type="rule_engine",
        input_contract={"required_capture_roles": ["source", "primary"]},
        judge_config={
            "engine_config": {
                "handler": "content_lifecycle_state",
                "source_inactive_pattern": "已下架",
                "consumer_entity_pattern": "目标内容",
                "consumer_marked_pattern": "目标内容.*已下架",
            }
        },
    )
    evidence_reader = FakeRuleJudgeEvidenceReader("普通内容列表")
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=evidence_reader,
        observation_packet_service=FakeObservationPacketService(),
    )

    result = await service.judge(_request(rule_code="alioth_40", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert result.observation_packet_id == "packet-1"
    assert result.confidence == 0.0
    assert "applicability" not in result.judge_output
    assert evidence_reader.calls == []


@pytest.mark.parametrize(
    ("rule_code", "input_contract", "engine_config", "visible_text"),
    [
        (
            "alioth_40",
            {"required_capture_roles": ["source", "primary"]},
            {
                "handler": "content_lifecycle_state",
                "source_inactive_pattern": "已下架",
                "consumer_entity_pattern": "目标内容",
                "consumer_marked_pattern": "目标内容.*已下架",
            },
            "普通内容列表",
        ),
        (
            "alioth_8",
            {"required_capture_roles": ["before", "primary", "refreshed"]},
            {
                "handler": "state_transition_assertion",
                "action_pattern": "关注|收藏|点赞",
                "after_text_pattern": "已关注|已收藏|已点赞",
            },
            "普通详情页",
        ),
        (
            "alioth_29",
            {
                "required_capture_roles": [
                    "attempt_1",
                    "attempt_2",
                    "attempt_3",
                    "primary",
                ]
            },
            {
                "handler": "rate_limit_assertion",
                "action_pattern": "提交|发送|发布",
                "probe_content_pattern": "ALIOTH_RATE_PROBE",
            },
            "只读内容页",
        ),
        (
            "alioth_46",
            {"required_capture_roles": ["before", "primary"]},
            {
                "handler": "state_transition_assertion",
                "action_pattern": '"action"\\s*:\\s*"click"',
                "after_fullscreen_required": True,
            },
            "纯文字帖子",
        ),
    ],
)
async def test_balanced_preflight_skips_complete_page_without_target(
    rule_code: str,
    input_contract: dict[str, object],
    engine_config: dict[str, object],
    visible_text: str,
) -> None:
    definition = RuleJudgeDefinition(
        rule_id=f"rule-{rule_code}",
        rule_code=rule_code,
        executor_type="rule_engine",
        input_contract=input_contract,
        judge_config={"engine_config": engine_config},
    )
    evidence_reader = FakeRuleJudgeEvidenceReader(visible_text)
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=evidence_reader,
        observation_packet_service=CompleteSurfaceObservationPacketService(visible_text),
        verdict_mode="balanced",
    )

    result = await service.judge(_request(rule_code=rule_code, judge_input={}))

    assert result.execution_status == "skipped"
    assert result.judge_output["applicability"] == "not_applicable"
    assert result.judge_output["tool_results"][0]["tool_name"] == (
        "balanced_applicability_preflight"
    )
    assert evidence_reader.calls == []


@pytest.mark.parametrize("visible_text_truncated", [False, True])
async def test_balanced_preflight_keeps_insufficient_when_target_or_surface_uncertain(
    visible_text_truncated: bool,
) -> None:
    visible_text = "目标内容" if not visible_text_truncated else "普通内容列表"
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_40",
        rule_code="alioth_40",
        executor_type="rule_engine",
        input_contract={"required_capture_roles": ["source", "primary"]},
        judge_config={
            "engine_config": {
                "handler": "content_lifecycle_state",
                "source_inactive_pattern": "已下架",
                "consumer_entity_pattern": "目标内容",
                "consumer_marked_pattern": "目标内容.*已下架",
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(visible_text),
        observation_packet_service=CompleteSurfaceObservationPacketService(
            visible_text,
            visible_text_truncated=visible_text_truncated,
        ),
        verdict_mode="balanced",
    )

    result = await service.judge(_request(rule_code="alioth_40", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert "applicability" not in result.judge_output


async def test_rule_judge_evaluates_multi_capture_state_transition() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_8",
        rule_code="alioth_8",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "state_transition_assertion",
                "after_text_pattern": "已关注",
                "refreshed_text_pattern": "已关注",
                "action_pattern": "follow",
            }
        },
    )
    ui_snapshot = {
        "document_state": {"fullscreen_active": False},
        "media": [],
    }
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "关注",
            role_artifacts={
                "primary": {
                    "visible_text": "关注",
                    "action_trace": [],
                    "ui_snapshot": ui_snapshot,
                },
                "after": {
                    "visible_text": "已关注",
                    "action_trace": [{"action": "click", "selector": "button.follow"}],
                    "ui_snapshot": ui_snapshot,
                },
                "refreshed": {
                    "visible_text": "已关注",
                    "action_trace": [],
                    "ui_snapshot": ui_snapshot,
                },
            },
        ),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_8",
            judge_input={},
            evidence_capture_roles={
                "primary": "capture-1",
                "after": "capture-2",
                "refreshed": "capture-3",
            },
        )
    )

    assert result.execution_status == "pass"
    assert [item["name"] for item in result.judge_output["checks"]] == [
        "after_text_pattern",
        "refreshed_text_pattern",
        "action_pattern",
    ]


_FULLSCREEN_UI = {
    "document_state": {"fullscreen_active": False},
    "media": [{"viewport_coverage": 0.2}],
}


async def test_bottom_tab_switch_does_not_judge_post_fullscreen() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_46",
        rule_code="alioth_46",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "state_transition_assertion",
                "before_role": "before",
                "after_role": "primary",
                "action_role": "primary",
                "action_pattern": '"action"\\s*:\\s*"click"',
                "after_fullscreen_required": True,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "帖子",
            role_artifacts={
                "before": {
                    "visible_text": "帖子列表",
                    "action_trace": [],
                    "ui_snapshot": _FULLSCREEN_UI,
                },
                "primary": {
                    "visible_text": "社区",
                    "action_trace": [
                        {"action": "click", "semantic_target": "底栏「社区」"}
                    ],
                    "ui_snapshot": _FULLSCREEN_UI,
                },
            },
        ),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_46",
            judge_input={},
            evidence_capture_roles={"before": "capture-1", "primary": "capture-2"},
        )
    )

    assert result.execution_status == "evidence_insufficient"
    assert result.problem_desc == "采到的是底栏切换，缺同一帖子内缩略图点击后全屏的配对画面。"
    assert result.judge_output["gap_codes"] == ["post_media_fullscreen_pair_missing"]


async def test_post_thumbnail_click_without_fullscreen_still_fails() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_46",
        rule_code="alioth_46",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "state_transition_assertion",
                "before_role": "before",
                "after_role": "primary",
                "action_role": "primary",
                "after_fullscreen_required": True,
            }
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "帖子图片",
            role_artifacts={
                "before": {
                    "visible_text": "帖子图片",
                    "action_trace": [],
                    "ui_snapshot": _FULLSCREEN_UI,
                },
                "primary": {
                    "visible_text": "帖子图片",
                    "action_trace": [
                        {"action": "click", "semantic_target": "帖子图片缩略图"}
                    ],
                    "ui_snapshot": _FULLSCREEN_UI,
                },
            },
        ),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_46",
            judge_input={},
            evidence_capture_roles={"before": "capture-1", "primary": "capture-2"},
        )
    )

    assert result.execution_status == "fail"


_AD_PLAYBACK_ENGINE = {
    "handler": "state_transition_assertion",
    "before_role": "before",
    "after_role": "primary",
    "action_role": "primary",
    "before_text_pattern": r"(?i)(?:广告|advertisement|\bad\b)",
    "after_text_pattern": r"(?i)(?:正片|正在播放|播放中|video\s*playing|content\s*playing)",
    "action_pattern": (
        r'(?s)"action"\s*:\s*"network_fault".*?'
        r'"matched_request_count"\s*:\s*[1-9]\d*'
    ),
    "require_text_change": True,
}


def _ad_fallback_reader(
    *, after_text: str, fault_effective: bool
) -> FakeRuleJudgeEvidenceReader:
    return FakeRuleJudgeEvidenceReader(
        after_text,
        role_artifacts={
            "before": {
                "visible_text": "广告加载中",
                "action_trace": [],
                "ui_snapshot": {},
            },
            "primary": {
                "visible_text": after_text,
                "action_trace": [
                    {
                        "action": "network_fault",
                        "result": "network_fault_injected",
                        "fault_scope": "ad_load",
                        "ad_load_fault_supported": True,
                        "matched_request_count": 1,
                        "fault_effective": fault_effective,
                        "fallback_outcome": "skipped",
                    }
                ],
                "ui_snapshot": {},
            },
        },
    )


async def test_old_ad_fallback_judges_frozen_fault_contract_without_first_frame_timestamps() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_26",
        rule_code="alioth_26",
        executor_type="rule_engine",
        judge_config={"engine_config": _AD_PLAYBACK_ENGINE},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=_ad_fallback_reader(after_text="正片正在播放", fault_effective=True),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_26",
            judge_input={},
            evidence_capture_roles={"before": "capture-1", "primary": "capture-2"},
        )
    )

    assert result.execution_status == "pass"
    assert result.judge_output["ad_fault_effective"] is True
    assert "handoff_ms" not in result.judge_output
    assert "limit_ms" not in result.judge_output


async def test_old_ad_fallback_requires_proven_fault_even_when_playback_text_is_present() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_26",
        rule_code="alioth_26",
        executor_type="rule_engine",
        judge_config={"engine_config": _AD_PLAYBACK_ENGINE},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=_ad_fallback_reader(after_text="正片正在播放", fault_effective=False),
    )

    result = await service.judge(
        _request(
            rule_code="alioth_26",
            judge_input={},
            evidence_capture_roles={"before": "capture-1", "primary": "capture-2"},
        )
    )

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output["gap_codes"] == ["ad_load_fault_effect_missing"]
    assert "广告故障生效" in (result.problem_desc or "")


async def test_old_ad_fallback_reports_playback_failure_from_effective_fault_not_elapsed_time() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-alioth_26",
        rule_code="alioth_26",
        executor_type="rule_engine",
        judge_config={"engine_config": _AD_PLAYBACK_ENGINE},
    )
    request = _request(
        rule_code="alioth_26",
        judge_input={},
        evidence_capture_roles={"before": "capture-1", "primary": "capture-2"},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=_ad_fallback_reader(after_text="广告已关闭，内容空白", fault_effective=True),
    )

    result = await service.judge(request)

    assert result.execution_status == "fail"
    assert result.judge_output["ad_fault_effective"] is True
    assert "正片没有开始播放" in (result.problem_desc or "")
    assert "handoff_ms" not in result.judge_output


@pytest.mark.parametrize(
    ("rule_code", "engine_config", "role_artifacts", "expected_output_field"),
    [
        (
            "alioth_13",
            {
                "handler": "price_relation",
                "price_pattern": (r"(?P<product>黄金月卡).*?¥(?P<price>\d+(?:\.\d+)?)"),
            },
            {
                "primary": {"visible_text": "黄金月卡 ¥19.90"},
                "location_1": {"visible_text": "黄金月卡 支付 ¥19.90"},
            },
            "products",
        ),
        (
            "alioth_29",
            {
                "handler": "rate_limit_assertion",
                "action_pattern": "submit",
                "probe_content_pattern": "ALIOTH_RATE_PROBE",
                "rate_limit_text_pattern": "操作频繁",
            },
            {
                **{
                    f"attempt_{index}": {
                        "visible_text": " ".join(["ALIOTH_RATE_PROBE"] * min(index, 3)),
                        "action_trace": [{"action": "click", "selector": "submit"}],
                        "har": {"format": "network_events", "entries": []},
                    }
                    for index in range(1, 4)
                },
                "primary": {
                    "visible_text": "ALIOTH_RATE_PROBE " * 3 + "操作频繁",
                    "action_trace": [{"action": "click", "selector": "submit"}],
                    "har": {
                        "format": "network_events",
                        "entries": [{"event": "response", "status": 429}],
                    },
                },
            },
            "attempts",
        ),
        (
            "alioth_40",
            {
                "handler": "content_lifecycle_state",
                "source_inactive_pattern": r"ALIOTH_OFFLINE.*已下架",
                "consumer_entity_pattern": "ALIOTH_OFFLINE",
                "consumer_marked_pattern": r"ALIOTH_OFFLINE.*已下架",
            },
            {
                "source": {"visible_text": "ALIOTH_OFFLINE 状态 已下架"},
                "primary": {"visible_text": "其他内容"},
            },
            "consumers",
        ),
    ],
)
async def test_rule_judge_evaluates_policy_handlers_from_trusted_roles(
    rule_code: str,
    engine_config: dict,
    role_artifacts: dict[str, dict[str, object]],
    expected_output_field: str,
) -> None:
    definition = RuleJudgeDefinition(
        rule_id=f"rule-{rule_code}",
        rule_code=rule_code,
        executor_type="rule_engine",
        judge_config={"engine_config": engine_config},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            "可信证据",
            role_artifacts=role_artifacts,
        ),
    )
    role_ids = {"primary": "capture-1"}
    role_ids.update(
        {
            role: f"capture-{index}"
            for index, role in enumerate(
                (item for item in role_artifacts if item != "primary"),
                start=2,
            )
        }
    )

    result = await service.judge(
        _request(
            rule_code=rule_code,
            judge_input={},
            evidence_capture_roles=role_ids,
        )
    )

    assert result.execution_status == "pass"
    assert expected_output_field in result.judge_output


async def test_rule_judge_requires_page_capture_and_evidence() -> None:
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_6", handler="technical_error_text")
        )
    )

    with pytest.raises(RuleJudgeValidationError, match="页面采集 ID"):
        await service.judge(
            RuleJudgeRequest(
                task_id="task-1",
                rule_code="alioth_6",
                page_capture_id="",
                evidence_artifact_ids=("artifact-1",),
                judge_input={"visible_text": "ok"},
            )
        )

    with pytest.raises(RuleJudgeValidationError, match="证据附件 ID"):
        await service.judge(
            RuleJudgeRequest(
                task_id="task-1",
                rule_code="alioth_6",
                page_capture_id="capture-1",
                evidence_artifact_ids=(),
                judge_input={"visible_text": "ok"},
            )
        )

    with pytest.raises(RuleJudgeValidationError, match="证据采集角色必须为对象"):
        await service.judge(
            RuleJudgeRequest(
                task_id="task-1",
                rule_code="alioth_6",
                page_capture_id="capture-1",
                evidence_artifact_ids=("artifact-1",),
                judge_input={"visible_text": "ok"},
                evidence_capture_roles=["capture-1"],  # type: ignore[arg-type]
            )
        )


async def test_rule_judge_rejects_misconfigured_semantic_or_unknown_handler() -> None:
    non_engine = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            RuleJudgeDefinition(
                rule_id="rule-alioth_2",
                rule_code="alioth_2",
                executor_type="llm",
                judge_config={"engine_config": {"handler": "technical_error_text"}},
            )
        )
    )
    with pytest.raises(RuleJudgeNotConfiguredError, match="独立语义 Judge"):
        await non_engine.judge(_request(rule_code="alioth_2", judge_input={"visible_text": "ok"}))

    unsupported = RuleJudgeService(
        repository=FakeRuleJudgeRepository(
            _definition(rule_code="alioth_20", handler="popup_close_behavior")
        )
    )
    with pytest.raises(RuleJudgeNotConfiguredError, match="alioth_20"):
        await unsupported.judge(_request(rule_code="alioth_20", judge_input={"visible_text": "ok"}))


def test_rule_execution_route_exposes_registered_handler_contract() -> None:
    route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "discount_price_compare",
                "discount_price_field": "sale_price",
                "original_price_field": "list_price",
            }
        },
    )

    assert route == {
        "strategy": "rule_engine",
        "ready": True,
        "rule_engine_ready": True,
        "tool_name": "rule_judge_tool",
        "handler": "discount_price_compare",
        "handler_tool_name": "deterministic_assertion_tool",
        "judge_input_source": "agent_structured",
        "required_input_fields": ["sale_price", "list_price"],
        "required_artifact_types": ["visible_text"],
        "session_dependency": "snapshot",
        "blocked_reason": None,
    }


def test_rule_execution_route_blocks_unregistered_handler() -> None:
    route = resolve_rule_execution_route(
        executor_type="rule_engine",
        judge_config={"engine_config": {"handler": "popup_close_behavior"}},
    )

    assert route.ready is False
    assert route.rule_engine_ready is False
    assert route.tool_name is None
    assert route.judge_input_source == "unavailable"
    assert route.blocked_reason == {
        "code": "HANDLER_NOT_REGISTERED",
        "message": "Handler 未注册：popup_close_behavior",
    }


def test_semantic_rule_execution_route_requires_independent_judge() -> None:
    route = rule_execution_route_payload(
        executor_type="hybrid",
        judge_config={
            "tool_refs": [
                "image_quality_analyzer_tool",
                "visual_rule_judge_tool",
            ],
            "capability_config": {
                "deterministic_tools": ["image_quality_analyzer_tool"],
                "required_artifact_types": ["screenshot", "ui_snapshot"],
            },
        },
    )

    assert route["ready"] is True
    assert route["tool_name"] == "rule_judge_tool"
    assert route["handler"] == "semantic_rule_judge"
    assert route["handler_tool_name"] == "visual_rule_judge_tool"
    assert route["judge_input_source"] == "trusted_observation_packet"
    assert route["required_artifact_types"] == ["screenshot", "ui_snapshot"]


@pytest.mark.parametrize(
    ("engine_config", "message"),
    [
        (
            {"handler": "technical_error_text", "pattern": "["},
            "不是有效正则",
        ),
        (
            {"handler": "discount_price_compare", "operator": "approximately"},
            "operator 必须为",
        ),
        (
            {"handler": "qr_url_validation", "input_field": 42},
            "input_field 必须为非空字符串",
        ),
        (
            {"handler": "qr_url_validation", "timeout_seconds": 0},
            "timeout_seconds 必须在",
        ),
        (
            {"handler": "qr_url_validation", "max_redirects": 21},
            "max_redirects 必须在",
        ),
    ],
)
def test_rule_execution_route_blocks_invalid_handler_config(
    engine_config: dict,
    message: str,
) -> None:
    route = resolve_rule_execution_route(
        executor_type="rule_engine",
        judge_config={"engine_config": engine_config},
    )

    assert route.ready is False
    assert route.blocked_reason is not None
    assert route.blocked_reason["code"] == "HANDLER_CONFIG_INVALID"
    assert message in route.blocked_reason["message"]


def test_technical_error_route_reads_trusted_visible_text() -> None:
    route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "technical_error_text",
                "pattern": "HTTP 500",
            }
        },
    )

    assert route["judge_input_source"] == "trusted_evidence"
    assert route["required_input_fields"] == []
    assert route["required_artifact_types"] == ["visible_text"]


def test_new_collection_and_ui_handler_routes_expose_input_contracts() -> None:
    collection_route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "collection_constraint",
                "constraint": "case_style_consistency",
                "input_field": "labels",
            }
        },
    )
    ui_route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "required_ui_capability",
                "required_all_of": ["logout_entry"],
                "input_field": "capabilities",
            }
        },
    )

    assert collection_route["rule_engine_ready"] is True
    assert collection_route["required_input_fields"] == ["labels"]
    assert collection_route["required_artifact_types"] == ["visible_text"]
    assert ui_route["rule_engine_ready"] is True
    assert ui_route["judge_input_source"] == "trusted_and_agent_structured"
    assert ui_route["required_input_fields"] == ["capabilities"]
    # 上传限制展示型判定只把页面文字当必需证据；Action Trace 仅供可选的禁止动作检查
    assert ui_route["required_artifact_types"] == ["visible_text"]


def test_temporal_and_geometry_routes_expose_trusted_evidence_contracts() -> None:
    temporal_route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "temporal_sequence_assertion",
                "value_pattern": "倒计时\\s*(\\d+)",
            }
        },
    )
    geometry_route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "geometry_hit_area_assertion",
            }
        },
    )

    assert temporal_route["rule_engine_ready"] is True
    assert temporal_route["judge_input_source"] == "trusted_evidence"
    assert temporal_route["required_input_fields"] == []
    assert temporal_route["required_artifact_types"] == ["temporal_trace"]
    assert geometry_route["rule_engine_ready"] is True
    assert geometry_route["judge_input_source"] == "trusted_and_agent_structured"
    assert geometry_route["required_input_fields"] == ["element_index"]
    assert geometry_route["required_artifact_types"] == ["ui_snapshot"]


def test_external_link_and_state_transition_routes_expose_contracts() -> None:
    external_route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "external_link_open_mode",
                "expected_mode": "new_window",
            }
        },
    )
    state_route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "state_transition_assertion",
                "after_text_pattern": "已关注",
            }
        },
    )

    assert external_route["judge_input_source"] == "trusted_evidence"
    assert external_route["required_input_fields"] == []
    assert external_route["required_artifact_types"] == [
        "action_trace",
        "ui_snapshot",
    ]
    assert state_route["judge_input_source"] == "trusted_evidence"
    assert state_route["required_input_fields"] == []
    assert state_route["required_artifact_types"] == [
        "visible_text",
        "action_trace",
        "ui_snapshot",
    ]


@pytest.mark.parametrize(
    ("engine_config", "artifact_types"),
    [
        (
            {
                "handler": "price_relation",
                "price_pattern": r"(?P<product>月卡).*?(?P<price>\d+)",
            },
            ["visible_text"],
        ),
        (
            {
                "handler": "rate_limit_assertion",
                "action_pattern": "submit",
                "probe_content_pattern": "ALIOTH_RATE_PROBE",
            },
            ["visible_text", "action_trace", "har"],
        ),
        (
            {
                "handler": "content_lifecycle_state",
                "source_inactive_pattern": "已下架",
                "consumer_entity_pattern": "目标作品",
                "consumer_marked_pattern": "目标作品.*已下架",
            },
            ["visible_text"],
        ),
    ],
)
def test_policy_handler_routes_use_only_trusted_evidence(
    engine_config: dict,
    artifact_types: list[str],
) -> None:
    route = rule_execution_route_payload(
        executor_type="rule_engine",
        judge_config={"engine_config": engine_config},
    )

    assert route["rule_engine_ready"] is True
    assert route["judge_input_source"] == "trusted_evidence"
    assert route["required_input_fields"] == []
    assert route["required_artifact_types"] == artifact_types


def test_rule_execution_route_blocks_non_object_judge_config() -> None:
    route = resolve_rule_execution_route(
        executor_type="rule_engine",
        judge_config=[],
    )

    assert route.ready is False
    assert route.blocked_reason == {
        "code": "JUDGE_CONFIG_INVALID",
        "message": "rule_engine 规则的 judge_config 必须是对象",
    }


async def test_rule_engine_startup_validation_reports_all_invalid_rules() -> None:
    repository = FakeRuleEngineConfigurationRepository(
        [
            RuleJudgeDefinition(
                rule_id="rule-6",
                rule_code="alioth_6",
                executor_type="rule_engine",
                judge_config={
                    "engine_config": {
                        "handler": "technical_error_text",
                        "pattern": "HTTP 500",
                    }
                },
            ),
            RuleJudgeDefinition(
                rule_id="rule-7",
                rule_code="alioth_7",
                executor_type="rule_engine",
                judge_config={},
            ),
            RuleJudgeDefinition(
                rule_id="rule-20",
                rule_code="alioth_20",
                executor_type="rule_engine",
                judge_config={"engine_config": {"handler": "popup_close_behavior"}},
            ),
        ]
    )

    with pytest.raises(RuleEngineConfigurationError) as exc_info:
        await validate_enabled_rule_engine_configuration(repository)

    message = str(exc_info.value)
    assert "alioth_7[ENGINE_CONFIG_MISSING]" in message
    assert "alioth_20[HANDLER_NOT_REGISTERED]" in message


async def test_rule_engine_guard_skips_exploration_only_without_repository_access() -> None:
    repository = FakeRuleEngineConfigurationRepository([])
    guard = RuleEngineConfigurationGuard(repository)

    await guard.ensure_ready_for(TaskExecutionMode.EXPLORATION_ONLY)

    assert repository.calls == 0


async def test_rule_engine_guard_caches_success_across_concurrent_full_tasks() -> None:
    repository = FakeRuleEngineConfigurationRepository([])
    guard = RuleEngineConfigurationGuard(repository)

    await asyncio.gather(
        guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION),
        guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION),
    )

    assert repository.calls == 1


async def test_rule_engine_guard_retries_after_configuration_failure() -> None:
    repository = FakeRuleEngineConfigurationRepository(
        [
            RuleJudgeDefinition(
                rule_id="rule-invalid",
                rule_code="alioth_invalid",
                executor_type="rule_engine",
                judge_config={},
            )
        ]
    )
    guard = RuleEngineConfigurationGuard(repository)

    with pytest.raises(RuleEngineConfigurationError):
        await guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION)
    repository.definitions = []
    await guard.ensure_ready_for(TaskExecutionMode.FULL_INSPECTION)

    assert repository.calls == 2


async def test_rule_judge_executes_llm_through_independent_semantic_judge() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-10",
        rule_code="alioth_10",
        executor_type="llm",
        judge_config={
            "tool_refs": ["semantic_rule_judge_tool"],
            "llm_checks": ["提示文案应清晰"],
            "capability_config": {"required_artifact_types": ["visible_text"]},
        },
        rule_name="提示文案检查",
        category="text",
        check_point="提示应易于理解",
        input_contract={"required_artifacts": ["visible_text"]},
        manual_review_policy={"min_confidence": 0.8},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=FakeSemanticJudge(),
    )

    result = await service.judge(_request(rule_code="alioth_10", judge_input={}))

    assert result.execution_status == "pass"
    assert result.observation_packet_id == "packet-1"
    assert result.judge_output["schema_version"] == "1.0"
    assert result.judge_output["conclusion"] == "pass"


async def test_rule_judge_splits_semantic_batches_at_gateway_limit() -> None:
    """同页规则超过四条时必须分批，不能把整个质检 Work 交给网关拒绝。"""
    definitions = {
        f"alioth_batch_{index}": RuleJudgeDefinition(
            rule_id=f"rule-batch-{index}",
            rule_code=f"alioth_batch_{index}",
            executor_type="llm",
            judge_config={"tool_refs": ["semantic_rule_judge_tool"]},
            rule_name=f"批量规则 {index}",
            category="text",
        )
        for index in range(5)
    }

    class Repository:
        async def get_rule_judge_definition(self, *, task_id, rule_code):
            del task_id
            return definitions.get(rule_code)

    class BatchSemanticJudge:
        def __init__(self) -> None:
            self.batch_sizes: list[int] = []

        async def judge_batch(self, *, items):
            self.batch_sizes.append(len(items))
            if len(items) > 4:
                raise ValueError("语义 Judge 批次大小必须在 1 到 4 之间")
            return {
                packet.rule_code: SemanticJudgeModelOutput(
                    conclusion="pass",
                    confidence=0.91,
                    reasoning_summary="页面文案满足规则。",
                    evidence_refs=["artifact-1"],
                )
                for packet, _ in items
            }

    semantic_judge = BatchSemanticJudge()
    service = RuleJudgeService(
        repository=Repository(),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=semantic_judge,
    )

    results = await service.judge_many(
        [
            _request(rule_code=rule_code, judge_input={})
            for rule_code in definitions
        ]
    )

    assert semantic_judge.batch_sizes == [4, 1]
    assert [result.rule_code for result in results] == list(definitions)
    assert all(result.execution_status == "pass" for result in results)


async def test_rule_judge_executes_observation_packet_capability_handler() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-21",
        rule_code="alioth_21",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {"handler": "overlay_dismissal_handler"},
        },
        rule_name="首页弹窗蒙层点击行为检查",
        category="ad",
        input_contract={
            "required_artifacts": ["screenshot", "ui_snapshot", "dom"],
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
    )

    result = await service.judge(_request(rule_code="alioth_21", judge_input={}))

    assert result.execution_status == "skipped"
    assert result.observation_packet_id == "packet-1"
    assert result.judge_output["handler"] == "overlay_dismissal_handler"


async def test_rule_judge_skips_popup_rule_when_page_state_has_no_overlay() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-21",
        rule_code="alioth_21",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {
                "handler": "overlay_dismissal_handler",
                "required_page_states": ["popup", "modal", "overlay", "dialog"],
            },
        },
        rule_name="首页弹窗蒙层点击行为检查",
        category="ad",
        input_contract={"required_artifacts": ["screenshot", "ui_snapshot", "dom"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=PageStateObservationPacketService("stable"),
    )

    result = await service.judge(_request(rule_code="alioth_21", judge_input={}))

    assert result.execution_status == "skipped"
    assert result.judge_output["applicability"] == "not_applicable"
    assert result.judge_output["tool_results"][0] == {
        "tool_name": "required_page_state_preflight",
        "status": "not_applicable",
        "observed_page_state": "stable",
        "required_page_states": ["dialog", "modal", "overlay", "popup"],
    }


async def test_hybrid_preflight_keeps_insufficient_tool_on_semantic_path() -> None:
    """确定性工具给不出结果时仍交给语义判定，不得就地压成人工复核。"""
    tool_name = "ad_region_detector_tool"
    expected_status = "pass"
    expected_marker = "页面文案满足规则"
    expected_semantic_calls = 1
    semantic_judge = CountingSemanticJudge()
    definition = RuleJudgeDefinition(
        rule_id="rule-hybrid",
        rule_code="alioth_hybrid",
        executor_type="hybrid",
        judge_config={
            "tool_refs": [tool_name, "visual_rule_judge_tool"],
            "capability_config": {
                "deterministic_tools": [tool_name],
                "required_artifact_types": ["ui_snapshot", "visible_text"],
            },
        },
        rule_name="混合规则预检",
        category="ad",
        input_contract={"required_artifacts": ["ui_snapshot", "visible_text"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=semantic_judge,
    )

    result = await service.judge(_request(rule_code="alioth_hybrid", judge_input={}))

    assert result.execution_status == expected_status
    assert expected_marker in (result.problem_desc or result.result_summary or "")
    assert semantic_judge.call_count == expected_semantic_calls


async def test_typo_rule_judges_text_without_any_dictionary() -> None:
    """错别字由语义判定按上下文直接判断：任何字都可能写错，穷举不了词表。"""
    semantic_judge = CountingSemanticJudge()
    definition = RuleJudgeDefinition(
        rule_id="rule-4",
        rule_code="alioth_4",
        executor_type="hybrid",
        judge_config={
            "tool_refs": [
                "page_capture_tool",
                "visible_text_extractor_tool",
                "semantic_rule_judge_tool",
            ],
            "capability_config": {
                "deterministic_tools": ["visible_text_extractor_tool"],
                "semantic_judge": "semantic_rule_judge_tool",
                "required_artifact_types": ["ui_snapshot", "visible_text"],
            },
        },
        rule_name="错别字检查",
        category="text",
        input_contract={"required_artifacts": ["ui_snapshot", "visible_text"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=semantic_judge,
    )

    result = await service.judge(_request(rule_code="alioth_4", judge_input={}))

    assert semantic_judge.call_count == 1
    assert result.execution_status != "manual_review"
    tool_names = [item["tool_name"] for item in result.judge_output["tool_results"]]
    assert "visible_text_extractor_tool" in tool_names
    assert all("spellcheck" not in name for name in tool_names)


async def test_ad_preflight_skips_only_when_structured_absence_is_confirmed() -> None:
    class CompleteNoAdObservationPacketService(FakeObservationPacketService):
        async def build(self, request) -> ObservationPacket:
            packet = await super().build(request)
            packet.packet_payload["facts"]["ui_snapshot"] = {
                "element_count": 0,
                "elements": [],
            }
            return packet

    definition = RuleJudgeDefinition(
        rule_id="rule-16",
        rule_code="alioth_16",
        executor_type="hybrid",
        judge_config={
            "tool_refs": ["ad_region_detector_tool", "visual_rule_judge_tool"],
            "capability_config": {
                "deterministic_tools": ["ad_region_detector_tool"],
                "required_artifact_types": ["ui_snapshot", "visible_text"],
            },
        },
        rule_name="广告素材比例检查",
        category="ad",
        input_contract={"required_artifacts": ["ui_snapshot", "visible_text"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=CompleteNoAdObservationPacketService(),
        semantic_judge=SemanticJudgeMustNotRun(),
    )

    result = await service.judge(_request(rule_code="alioth_16", judge_input={}))

    assert result.execution_status == "skipped"
    assert result.judge_output["applicability"] == "not_applicable"
    detector = result.judge_output["tool_results"][0]
    assert detector["facts"]["absence_confirmed"] is True


async def test_ad_preflight_keeps_real_click_journey_for_semantic_comparison() -> None:
    semantic_judge = CountingSemanticJudge()
    definition = RuleJudgeDefinition(
        rule_id="rule-17",
        rule_code="alioth_17",
        executor_type="hybrid",
        judge_config={
            "tool_refs": [
                "ad_region_detector_tool",
                "safe_click_observer_tool",
                "visual_rule_judge_tool",
            ],
            "capability_config": {
                "batch_group": "ad-landing",
                "requires_journey": True,
                "deterministic_tools": [
                    "ad_region_detector_tool",
                    "safe_click_observer_tool",
                    "landing_page_probe_tool",
                    "visual_semantic_compare_tool",
                ],
                "required_artifact_types": ["ui_snapshot", "action_trace"],
            },
        },
        rule_name="广告点击跳转一致性检查",
        category="ad",
        input_contract={"required_artifacts": ["ui_snapshot", "action_trace"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=JourneyObservationPacketService(),
        semantic_judge=semantic_judge,
    )

    result = await service.judge(_request(rule_code="alioth_17", judge_input={}))

    assert result.execution_status == "pass"
    assert semantic_judge.call_count == 1


async def test_ad_preflight_blocks_semantic_judge_without_action_bound_pair() -> None:
    semantic_judge = CountingSemanticJudge()
    definition = RuleJudgeDefinition(
        rule_id="rule-17",
        rule_code="alioth_17",
        executor_type="hybrid",
        judge_config={
            "tool_refs": [
                "safe_click_observer_tool",
                "landing_page_probe_tool",
                "visual_semantic_compare_tool",
                "visual_rule_judge_tool",
            ],
            "capability_config": {
                "batch_group": "ad-landing",
                "requires_journey": True,
                "deterministic_tools": [
                    "safe_click_observer_tool",
                    "landing_page_probe_tool",
                    "visual_semantic_compare_tool",
                ],
                "required_artifact_types": ["ui_snapshot", "action_trace"],
            },
        },
        rule_name="广告点击跳转一致性检查",
        category="ad",
        input_contract={"required_artifacts": ["ui_snapshot", "action_trace"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=semantic_judge,
    )

    result = await service.judge(_request(rule_code="alioth_17", judge_input={}))

    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output["model_output"]["conflicts"] == [
        "ad_landing_evidence_pair_incomplete"
    ]
    # 原因写明缺哪份证据，不再是写回后只剩「……成对证据：检测工具」
    expected = (
        "没拿到同一次点击广告的完整证据，没法比对广告和落地页是否一致："
        "缺少点击广告前后的页面截图、点开广告后到达的落地页、广告画面和落地页的比对"
    )
    assert result.problem_desc == expected
    assert result.result_summary == expected
    assert semantic_judge.call_count == 0


def test_unlinked_payment_entry_closes_as_evidence_gap() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-ops-4",
        rule_code="alioth_ops_4",
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": "payment-entry"}},
        rule_name="充值入口进入充值页检查",
    )
    packet = ObservationPacket(
        observation_packet_id="packet-ops-4",
        task_id="task-ops-4",
        page_capture_id="capture-ops-4",
        rule_id="rule-ops-4",
        rule_code="alioth_ops_4",
        evidence_artifact_ids=(),
        packet_payload={"page": {"surface_kind": "android", "page_type": "payment"}},
        quality_gate_status="pass",
        quality_gate_reason=None,
    )
    request = RuleJudgeRequest(
        task_id="task-ops-4",
        rule_code="alioth_ops_4",
        page_capture_id="capture-ops-4",
        evidence_artifact_ids=(),
        judge_input={},
    )
    reason = "充值入口、充值页和选档结果未按实际操作串联，缺对应画面。"

    result = _hybrid_preflight_result(
        definition=definition,
        request=request,
        packet=packet,
        tool_results=[
            {
                "tool_name": "interaction_state_diff_tool",
                "status": "insufficient",
                "facts": {"gap_codes": ["payment_entry_chain_missing"]},
                "reason": reason,
            }
        ],
    )

    assert result is not None
    assert result.execution_status == "evidence_insufficient"
    assert result.problem_desc == reason


def test_native_player_without_switch_closes_as_evidence_gap() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-ops-3",
        rule_code="alioth_ops_3",
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": "player-playback"}},
        rule_name="视频与短视频播放检查",
    )
    packet = ObservationPacket(
        observation_packet_id="packet-ops-3",
        task_id="task-ops-3",
        page_capture_id="capture-ops-3",
        rule_id="rule-ops-3",
        rule_code="alioth_ops_3",
        evidence_artifact_ids=(),
        packet_payload={"page": {"surface_kind": "android", "page_type": "video_detail"}},
        quality_gate_status="pass",
        quality_gate_reason=None,
    )
    request = RuleJudgeRequest(
        task_id="task-ops-3",
        rule_code="alioth_ops_3",
        page_capture_id="capture-ops-3",
        evidence_artifact_ids=(),
        judge_input={},
    )
    reason = "原生媒体未提供播放时间与进度，缺实际起播及上下条切换的证据。"

    result = _hybrid_preflight_result(
        definition=definition,
        request=request,
        packet=packet,
        tool_results=[
            {
                "tool_name": "media_element_extractor_tool",
                "status": "insufficient",
                "facts": {"gap_codes": ["native_playback_switch_missing"]},
                "reason": reason,
            }
        ],
    )

    assert result is not None
    assert result.execution_status == "evidence_insufficient"
    assert result.problem_desc == reason


def test_recharge_spacing_mismatch_closes_as_evidence_gap() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-spec-24",
        rule_code="alioth_spec_new_24",
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": "visual-layout"}},
        rule_name="充值页控件间距检查",
    )
    packet = ObservationPacket(
        observation_packet_id="packet-spec-24",
        task_id="task-spec-24",
        page_capture_id="capture-spec-24",
        rule_id="rule-spec-24",
        rule_code="alioth_spec_new_24",
        evidence_artifact_ids=(),
        packet_payload={"page": {"surface_kind": "android", "page_type": "payment"}},
        quality_gate_status="pass",
        quality_gate_reason=None,
    )
    request = RuleJudgeRequest(
        task_id="task-spec-24",
        rule_code="alioth_spec_new_24",
        page_capture_id="capture-spec-24",
        evidence_artifact_ids=(),
        judge_input={},
    )

    result = _hybrid_preflight_result(
        definition=definition,
        request=request,
        packet=packet,
        tool_results=[
            {
                "tool_name": "layout_geometry_assertion_handler",
                "status": "insufficient",
                "facts": {"gap_codes": ["recharge_layout_screen_mismatch"]},
                "reason": "充值页截图与结构信息不属于同一界面，无法检查控件间距。",
            }
        ],
    )

    assert result is not None
    assert result.execution_status == "evidence_insufficient"
    assert result.problem_desc == "充值页截图与结构信息不属于同一界面，无法检查控件间距。"


def test_visible_service_entry_without_arrival_closes_as_evidence_gap() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-47",
        rule_code="alioth_47",
        executor_type="hybrid",
        judge_config={"capability_config": {"batch_group": "service-entry"}},
        rule_name="客服入口可用性检查",
    )
    packet = ObservationPacket(
        observation_packet_id="packet-47",
        task_id="task-47",
        page_capture_id="capture-47",
        rule_id="rule-47",
        rule_code="alioth_47",
        evidence_artifact_ids=(),
        packet_payload={"page": {"surface_kind": "android"}},
        quality_gate_status="pass",
        quality_gate_reason=None,
    )
    request = RuleJudgeRequest(
        task_id="task-47",
        rule_code="alioth_47",
        page_capture_id="capture-47",
        evidence_artifact_ids=(),
        judge_input={},
    )

    result = _hybrid_preflight_result(
        definition=definition,
        request=request,
        packet=packet,
        tool_results=[
            {
                "tool_name": "control_behavior_assertion_tool",
                "status": "insufficient",
                "facts": {"gap_codes": ["service_entry_arrival_missing"]},
                "reason": "客服入口已看见，尚未点击并采集实际到达的客服页面。",
            }
        ],
    )

    assert result is not None
    assert result.execution_status == "evidence_insufficient"
    assert result.problem_desc == "客服入口已看见，尚未点击并采集实际到达的客服页面。"


def test_static_app_playback_page_closes_progress_rule_as_evidence_gap() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-44",
        rule_code="alioth_44",
        executor_type="hybrid",
        judge_config={
            "capability_config": {
                "batch_group": "player-controls",
                "requires_journey": True,
            }
        },
        rule_name="进度条时间显示检查",
    )
    packet = ObservationPacket(
        observation_packet_id="packet-44",
        task_id="task-44",
        page_capture_id="capture-44",
        rule_id="rule-44",
        rule_code="alioth_44",
        evidence_artifact_ids=(),
        packet_payload={"page": {"surface_kind": "android"}},
        quality_gate_status="pass",
        quality_gate_reason=None,
    )
    request = RuleJudgeRequest(
        task_id="task-44",
        rule_code="alioth_44",
        page_capture_id="capture-44",
        evidence_artifact_ids=(),
        judge_input={},
    )

    result = _hybrid_preflight_result(
        definition=definition,
        request=request,
        packet=packet,
        tool_results=[
            {
                "tool_name": "control_behavior_assertion_tool",
                "status": "insufficient",
                "facts": {"gap_codes": ["progress_bar_seek_and_playback_time_missing"]},
                "reason": "缺实际拖动进度条的过程及播放时间，静态播放页无法判断。",
            }
        ],
    )

    assert result is not None
    assert result.execution_status == "evidence_insufficient"
    assert result.problem_desc == "缺实际拖动进度条的过程及播放时间，静态播放页无法判断。"


async def test_observation_packet_route_does_not_require_agent_evidence_ids() -> None:
    definition = RuleJudgeDefinition(
        rule_id="rule-10",
        rule_code="alioth_10",
        executor_type="llm",
        judge_config={
            "tool_refs": ["semantic_rule_judge_tool"],
            "capability_config": {"required_artifact_types": ["visible_text"]},
        },
        rule_name="提示文案检查",
        category="text",
        input_contract={"required_artifacts": ["visible_text"]},
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=FakeObservationPacketService(),
        semantic_judge=FakeSemanticJudge(),
    )

    result = await service.judge(
        RuleJudgeRequest(
            task_id="task-1",
            rule_code="alioth_10",
            page_capture_id="capture-1",
            evidence_artifact_ids=(),
            judge_input={},
        )
    )

    assert result.execution_status == "pass"
    assert result.observation_packet_id == "packet-1"


def test_rule_judge_result_is_record_tool_compatible() -> None:
    result_fields = {
        "rule_id",
        "rule_code",
        "execution_status",
        "page_capture_id",
        "judge_strategy",
        "confidence",
        "result_summary",
        "problem_desc",
        "evidence_artifact_ids",
        "judge_input",
        "judge_output",
    }

    assert result_fields <= {item.name for item in fields(RuleJudgeResult)}


class _MappingRuleRepository:
    def __init__(self, definitions: dict[str, RuleJudgeDefinition]) -> None:
        self.definitions = definitions

    async def get_rule_judge_definition(
        self,
        *,
        task_id: str,
        rule_code: str,
    ) -> RuleJudgeDefinition | None:
        del task_id
        return self.definitions.get(rule_code)


class _BatchScopeEvidenceReader:
    """记录每个 rule_engine 批次的同步读取作用域。"""

    def __init__(self) -> None:
        self.scope_ids: list[int] = []
        self.events: list[tuple[str, int, str | None]] = []
        self.current_scope_id: int | None = None
        self._next_scope_id = 0

    @contextmanager
    def read_batch(self):
        self._next_scope_id += 1
        scope_id = self._next_scope_id
        self.scope_ids.append(scope_id)
        self.current_scope_id = scope_id
        self.events.append(("enter", scope_id, None))
        try:
            yield scope_id
        except BaseException as exc:
            self.events.append(("exit", scope_id, type(exc).__name__))
            raise
        else:
            self.events.append(("exit", scope_id, None))
        finally:
            self.current_scope_id = None


def _engine_definition(rule_id: str, rule_code: str) -> RuleJudgeDefinition:
    return RuleJudgeDefinition(
        rule_id=rule_id,
        rule_code=rule_code,
        executor_type="rule_engine",
        judge_config={"tool_refs": ["deterministic_assertion_tool"]},
        category="ad",
        severity="medium",
        check_point="占位",
        steps=[{"type": "observe"}],
    )


def _engine_request(rule_code: str) -> RuleJudgeRequest:
    return RuleJudgeRequest(
        task_id="task-1",
        rule_code=rule_code,
        page_capture_id="capture-1",
        evidence_artifact_ids=("artifact-1",),
        judge_input={},
    )


def _passing_engine_result(request: RuleJudgeRequest) -> RuleJudgeResult:
    return RuleJudgeResult(
        rule_id=f"rule-{request.rule_code}",
        rule_code=request.rule_code,
        executor_type="rule_engine",
        judge_strategy="rule_engine",
        execution_status="pass",
        confidence=1.0,
        result_summary="通过",
        problem_desc=None,
        page_capture_id=request.page_capture_id,
        evidence_artifact_ids=request.evidence_artifact_ids,
        judge_input=request.judge_input,
        judge_output={"conclusion": "pass"},
        observation_packet_id=f"packet-{request.rule_code}",
    )


async def test_judge_many_isolates_single_rule_failure_in_engine_batch() -> None:
    """一条规则的输入问题若不隔离，会吞掉同批已判结果并让整个工作项失败。"""
    service = RuleJudgeService(
        repository=_MappingRuleRepository(
            {
                "alioth_18": _engine_definition("rule-18", "alioth_18"),
                "alioth_24": _engine_definition("rule-24", "alioth_24"),
            }
        )
    )

    async def fake_judge(request: RuleJudgeRequest) -> RuleJudgeResult:
        if request.rule_code == "alioth_24":
            raise RuleJudgeValidationError("证据采集角色未引用有效证据：capture-1")
        return RuleJudgeResult(
            rule_id="rule-18",
            rule_code="alioth_18",
            executor_type="rule_engine",
            judge_strategy="rule_engine",
            execution_status="pass",
            confidence=1.0,
            result_summary="通过",
            problem_desc=None,
            page_capture_id="capture-1",
            evidence_artifact_ids=("artifact-1",),
            judge_input={},
            judge_output={"conclusion": "pass"},
        )

    service.judge = fake_judge

    results = await service.judge_many(
        [_engine_request("alioth_18"), _engine_request("alioth_24")]
    )

    assert [item.rule_code for item in results] == ["alioth_18", "alioth_24"]
    assert results[0].execution_status == "pass"
    assert results[1].execution_status == "evidence_insufficient"
    assert "未引用有效证据" in (results[1].problem_desc or "")
    assert results[1].judge_output["reason_code"] == "RULE_JUDGE_INPUT_UNAVAILABLE"


def _packet_isolation_definitions(executor_type: str) -> dict[str, RuleJudgeDefinition]:
    return {
        rule_code: RuleJudgeDefinition(
            rule_id=f"rule-{rule_code}",
            rule_code=rule_code,
            executor_type=executor_type,
            judge_config=(
                {
                    "engine_config": {
                        "handler": "technical_error_text",
                        "pattern": r"HTTP\s*500",
                        "required_page_states": ["content"],
                    }
                }
                if executor_type == "rule_engine"
                else {"tool_refs": ["semantic_rule_judge_tool"]}
            ),
        )
        for rule_code in ("alioth_before", "alioth_invalid", "alioth_after")
    }


@pytest.mark.parametrize("executor_type", ["rule_engine", "llm"])
async def test_judge_many_isolates_frozen_packet_drift_and_preserves_writer_error(
    executor_type: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真实冻包校验故障不丢掉前后规则结果，写回也不把完整性错误改成业务缺证。"""
    definitions = _packet_isolation_definitions(executor_type)
    frozen_payload = {"schema_version": "observation-packet.v1", "facts": {"text": "原始"}}
    frozen_json = json.dumps(frozen_payload, ensure_ascii=False)
    frozen_row = {
        "observation_packet_id": "packet-frozen",
        "evidence_artifact_ids": '["artifact-1"]',
        "packet_payload": frozen_json,
        "quality_gate_status": "pass",
        "quality_gate_reason": None,
    }

    class FrozenPacketService(PageStateObservationPacketService):
        def __init__(self) -> None:
            super().__init__("content")
            self.rule_codes: list[str] = []

        async def build(self, request):
            self.rule_codes.append(request.rule_code)
            if request.rule_code == "alioth_invalid":
                _ensure_frozen_packet_matches(
                    row=frozen_row,
                    evidence_artifact_ids=("artifact-1",),
                    packet_payload={**frozen_payload, "facts": {"text": "变化"}},
                    quality_gate_status="pass",
                    quality_gate_reason=None,
                )
            return await super().build(request)

    class BatchSemanticJudge(FakeSemanticJudge):
        def __init__(self) -> None:
            self.batches: list[list[str]] = []

        async def judge_batch(self, *, items):
            self.batches.append([packet.rule_code for packet, _ in items])
            return {
                packet.rule_code: await self.judge(packet=packet, tool_results=tool_results)
                for packet, tool_results in items
            }

    packet_service = FrozenPacketService()
    semantic_judge = BatchSemanticJudge()
    service = RuleJudgeService(
        repository=_MappingRuleRepository(definitions),
        evidence_reader=FakeRuleJudgeEvidenceReader("页面正常"),
        observation_packet_service=packet_service,
        semantic_judge=semantic_judge,
    )
    results = await service.judge_many([_engine_request(code) for code in definitions])

    assert packet_service.rule_codes == list(definitions)
    assert semantic_judge.batches == (
        [["alioth_before", "alioth_after"]] if executor_type == "llm" else []
    )
    assert [result.execution_status for result in results] == ["pass", "error", "pass"]
    invalid = results[1]
    assert invalid.observation_packet_id is None
    assert invalid.evidence_artifact_ids == ()
    assert invalid.judge_output["reason_code"] == "OBSERVATION_PACKET_VALIDATION_FAILED"
    assert invalid.judge_output["exception_type"] == ObservationPacketValidationError.__name__
    assert invalid.judge_output["evidence_trusted"] is False
    assert "已冻结" in (invalid.problem_desc or "")
    assert "evidence_gap" not in invalid.judge_output
    assert frozen_row["packet_payload"] == frozen_json

    handoff = InspectionHandoff(
        work_item_id="work-1", attempt_id="attempt-1", task_id="task-1",
        agent_run_id="consumer-1", input_hash="input-1", batch_key="batch-1",
        strategy=executor_type, session_dependency="snapshot",
        evidence=EvidenceBundleReference(
            page_capture_id="capture-1", producer_agent_run_id="producer-1",
            observation_packet_ids=("packet-untrusted",), artifact_ids=("artifact-untrusted",),
        ),
        rules=tuple(
            InspectionRuleTarget(
                rule_id=definition.rule_id, rule_code=code, ordinal=index, contract_hash="hash-1",
            )
            for index, (code, definition) in enumerate(definitions.items())
        ),
    )
    outcome = normalize_rule_judge_outcome(
        handoff=handoff, raw={"items": [asdict(result) for result in results]},
    )
    assert outcome.evidence_requests == ()
    assert outcome.deferred_decisions == ()
    recorded: list[dict] = []

    async def record_batch(*, page_capture_id, executions):
        assert page_capture_id == "capture-1"
        recorded.extend(executions)
        return {"items": []}

    monkeypatch.setattr(
        inspection_result_writer, "record_rule_executions_for_current_run", record_batch,
    )
    await inspection_result_writer.CurrentRuleDecisionWriter().write(handoff=handoff, outcome=outcome)

    assert [item["execution_status"] for item in recorded] == ["pass", "error", "pass"]
    assert recorded[1].get("observation_packet_id") is None
    assert recorded[1].get("evidence_artifact_ids", []) == []
    assert recorded[1]["judge_output"] == invalid.judge_output


@pytest.mark.parametrize("executor_type", ["rule_engine", "llm"])
@pytest.mark.parametrize(
    "error_type", [TimeoutError, ConnectionError, RuntimeError, ValueError, asyncio.CancelledError],
)
async def test_judge_many_packet_isolation_does_not_swallow_system_failures(
    executor_type: str, error_type: type[BaseException],
) -> None:
    """规则级包校验隔离不得吞掉基础设施错误、未知异常或任务取消。"""
    failure = error_type("判定中断")

    class FailedPacketService(FakeObservationPacketService):
        async def build(self, request):
            raise failure

    service = RuleJudgeService(
        repository=_MappingRuleRepository(_packet_isolation_definitions(executor_type)),
        observation_packet_service=FailedPacketService(),
        semantic_judge=FakeSemanticJudge(),
    )
    with pytest.raises(error_type) as caught:
        await service.judge_many([_engine_request("alioth_before")])
    assert caught.value is failure


async def test_judge_many_rule_engine_uses_one_evidence_scope_for_the_whole_batch() -> None:
    rule_codes = [f"alioth_batch_{index}" for index in range(6)]
    service = RuleJudgeService(
        repository=_MappingRuleRepository(
            {
                rule_code: _engine_definition(f"rule-{index}", rule_code)
                for index, rule_code in enumerate(rule_codes)
            }
        ),
        evidence_reader=_BatchScopeEvidenceReader(),
    )
    evidence_reader = service._evidence_reader
    assert isinstance(evidence_reader, _BatchScopeEvidenceReader)
    observed_scopes: list[tuple[str, int | None]] = []

    async def fake_judge(request: RuleJudgeRequest) -> RuleJudgeResult:
        observed_scopes.append((request.rule_code, evidence_reader.current_scope_id))
        return _passing_engine_result(request)

    service.judge = fake_judge
    requests = [_engine_request(rule_code) for rule_code in rule_codes]

    results = await service.judge_many(requests)

    assert evidence_reader.scope_ids == [1]
    assert evidence_reader.events == [("enter", 1, None), ("exit", 1, None)]
    assert observed_scopes == [(rule_code, 1) for rule_code in rule_codes]
    assert [result.rule_code for result in results] == rule_codes
    assert [result.observation_packet_id for result in results] == [
        f"packet-{rule_code}" for rule_code in rule_codes
    ]


async def test_judge_many_rule_engine_closes_evidence_scope_when_judge_times_out() -> None:
    rule_codes = ["alioth_batch_0", "alioth_batch_1"]
    evidence_reader = _BatchScopeEvidenceReader()
    service = RuleJudgeService(
        repository=_MappingRuleRepository(
            {
                rule_code: _engine_definition(f"rule-{index}", rule_code)
                for index, rule_code in enumerate(rule_codes)
            }
        ),
        evidence_reader=evidence_reader,
    )

    async def fake_judge(request: RuleJudgeRequest) -> RuleJudgeResult:
        if request.rule_code == "alioth_batch_1":
            raise TimeoutError("判定超时")
        return _passing_engine_result(request)

    service.judge = fake_judge

    with pytest.raises(TimeoutError, match="判定超时"):
        await service.judge_many([_engine_request(rule_code) for rule_code in rule_codes])

    assert evidence_reader.events == [("enter", 1, None), ("exit", 1, "TimeoutError")]
    assert evidence_reader.current_scope_id is None


async def test_judge_many_rule_engine_does_not_share_scope_between_batches() -> None:
    rule_codes = ["alioth_batch_0", "alioth_batch_1"]
    evidence_reader = _BatchScopeEvidenceReader()
    service = RuleJudgeService(
        repository=_MappingRuleRepository(
            {
                rule_code: _engine_definition(f"rule-{index}", rule_code)
                for index, rule_code in enumerate(rule_codes)
            }
        ),
        evidence_reader=evidence_reader,
    )
    observed_scopes: list[tuple[str, int | None]] = []

    async def fake_judge(request: RuleJudgeRequest) -> RuleJudgeResult:
        observed_scopes.append((request.rule_code, evidence_reader.current_scope_id))
        return _passing_engine_result(request)

    service.judge = fake_judge

    first_results = await service.judge_many([_engine_request("alioth_batch_0")])
    assert evidence_reader.current_scope_id is None
    second_results = await service.judge_many([_engine_request("alioth_batch_1")])

    assert evidence_reader.scope_ids == [1, 2]
    assert evidence_reader.events == [
        ("enter", 1, None),
        ("exit", 1, None),
        ("enter", 2, None),
        ("exit", 2, None),
    ]
    assert observed_scopes == [("alioth_batch_0", 1), ("alioth_batch_1", 2)]
    assert first_results[0].observation_packet_id == "packet-alioth_batch_0"
    assert second_results[0].observation_packet_id == "packet-alioth_batch_1"
