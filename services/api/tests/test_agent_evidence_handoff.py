"""原表 26/30/35/36 的真实采集角色交接与空白通过隔离回归。"""

import base64
import json
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from services.api.app.application.observation_packet import (
    ManifestQualityProjection,
    ObservationPacketService,
    ObservationPacketValidationError,
)
from services.api.app.application.observation_surface_guard import (
    target_document_unavailability_reason,
)
from services.api.app.application.rule_judge import (
    RuleJudgeDefinition,
    RuleJudgeRequest,
    RuleJudgeService,
)
from services.api.app.application.semantic_rule_judge import SemanticJudgeModelOutput
from services.api.tests.test_manifest_backed_observation import (
    CAPTURE_ID as MANIFEST_CAPTURE_ID,
)
from services.api.tests.test_manifest_backed_observation import (
    TASK_ID as MANIFEST_TASK_ID,
)
from services.api.tests.test_manifest_backed_observation import (
    _context_source,
    _manifest,
)
from services.api.tests.test_manifest_backed_observation import (
    _repository as _manifest_repository,
)
from services.api.tests.test_observation_packet import _artifact, _request, _source
from services.api.tests.test_rule_judge import FakeRuleJudgeEvidenceReader, FakeRuleJudgeRepository


class RoleRepository:
    def __init__(self, sources):
        self.sources = sources
        self.reads = []
        self.saved = []

    async def load_source(self, *, task_id, page_capture_id):
        self.reads.append((page_capture_id, None))
        source = self.sources.get(page_capture_id)
        return source if source is not None and source.task_id == task_id else None

    async def load_source_for_manifest(self, *, task_id, page_capture_id, manifest_id):
        self.reads.append((page_capture_id, manifest_id))
        return self.sources.get(page_capture_id)

    async def save_packet(self, **kwargs):
        self.saved.append(kwargs)
        return "packet-roles"


def _role_repository():
    primary = replace(
        _source(), manifest_quality=ManifestQualityProjection("manifest-primary", 2, "pass")
    )
    before = replace(
        primary,
        page_capture_id="capture-before",
        capture_role="before",
        manifest_quality=ManifestQualityProjection("manifest-before", 3, "pass"),
        artifacts=(
            _artifact("shot-before", "screenshot", b"\x89PNG\r\n\x1a\nbefore", "image/png"),
            _artifact("text-before", "visible_text", "操作前真实页面".encode(), "text/plain"),
        ),
    )
    return RoleRepository({"capture-1": primary, "capture-before": before})


def _role_request():
    return replace(
        _request(),
        evidence_capture_roles={"primary": "capture-1", "before": "capture-before"},
        evidence_manifest_refs={
            "primary": {"manifest_id": "manifest-primary", "manifest_version": "2"},
            "before": {"manifest_id": "manifest-before", "manifest_version": "3"},
        },
    )


async def test_declared_before_materials_reach_model_and_final_execution_ids():
    repository = _role_repository()
    service = ObservationPacketService(repository=repository)
    packet = await service.build(_role_request())
    assert "shot-before" in packet.evidence_artifact_ids
    assert "text-before" in packet.evidence_artifact_ids
    assert [item[0] for item in packet.image_payloads] == ["shot-1", "shot-before"]
    assert (
        packet.packet_payload["facts"]["capture_roles"]["before"]["page_capture_id"]
        == "capture-before"
    )
    assert repository.reads == [
        ("capture-1", "manifest-primary"),
        ("capture-before", "manifest-before"),
    ]
    assert repository.saved[0]["evidence_artifact_ids"] == packet.evidence_artifact_ids
    # 同主页面之后另一条规则增加角色，图片缓存不能重用之前的单图结果。
    plain = await service.build(replace(_request(), rule_code="another-rule"))
    assert [item[0] for item in plain.image_payloads] == ["shot-1"]
    paired = await service.build(_role_request())
    assert [item[0] for item in paired.image_payloads] == ["shot-1", "shot-before"]


async def test_role_selection_preserves_exact_retained_baseline():
    packet = await ObservationPacketService(repository=_role_repository()).build(
        replace(
            _role_request(),
            retained_baseline_artifact_ids={"before": ("shot-before",)},
        )
    )
    assert "shot-before" in packet.evidence_artifact_ids
    assert "text-before" not in packet.evidence_artifact_ids


async def test_explicit_role_images_do_not_reuse_truncated_primary_cache():
    source = replace(
        _source(),
        artifacts=tuple(
            _artifact(
                f"shot-{index}", "screenshot", b"\x89PNG\r\n\x1a\n" + bytes([index]), "image/png"
            )
            for index in range(8)
        ),
    )
    service = ObservationPacketService(repository=RoleRepository({"capture-1": source}))
    plain = await service.build(replace(_request(), input_contract={}))
    explicit = await service.build(
        replace(
            _request(),
            input_contract={},
            evidence_capture_roles={"primary": "capture-1"},
        )
    )
    assert len(plain.image_payloads) == 6
    assert len(explicit.image_payloads) == 8


@pytest.mark.parametrize(
    "change, message",
    [
        ({"agent_run_id": "other-run"}, "生产 Run"),
        ({"manifest_quality": ManifestQualityProjection("manifest-before", 4, "pass")}, "版本"),
    ],
)
async def test_role_evidence_rejects_cross_run_or_manifest_drift(change, message):
    repository = _role_repository()
    repository.sources["capture-before"] = replace(repository.sources["capture-before"], **change)
    with pytest.raises(ObservationPacketValidationError, match=message):
        await ObservationPacketService(repository=repository).build(_role_request())
    assert not repository.saved


async def test_pending_role_manifest_does_not_inherit_primary_pass():
    repository = _role_repository()
    repository.sources["capture-before"] = replace(
        repository.sources["capture-before"],
        manifest_quality=ManifestQualityProjection(
            "manifest-before", 3, "pending", "前置截图提交中"
        ),
    )
    packet = await ObservationPacketService(repository=repository).build(_role_request())
    assert packet.quality_gate_status == "pending"
    assert packet.quality_gate_reason == "前置截图提交中"


class PassJudge:
    def __init__(self, *, cited=False):
        self.calls = 0
        self.cited = cited

    async def judge(self, *, packet, tool_results):
        self.calls += 1
        return SemanticJudgeModelOutput(
            conclusion="pass",
            confidence=0.99,
            reasoning_summary="没有发现问题",
            evidence_refs=list(packet.evidence_artifact_ids) if self.cited else [],
        )


def _semantic_service(source, judge):
    definition = RuleJudgeDefinition(
        rule_id="rule-1",
        rule_code="alioth_test",
        executor_type="llm",
        judge_config={"tool_refs": ["semantic_rule_judge_tool"]},
        rule_name="业务内容检查",
        check_point="当前业务页面内容应符合规则",
        input_contract={"required_artifacts": ["screenshot"]},
        manual_review_policy={"min_confidence": 0.8},
    )
    return RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        semantic_judge=judge,
        observation_packet_service=ObservationPacketService(
            repository=RoleRepository({"capture-1": source})
        ),
    )


def _semantic_request():
    return RuleJudgeRequest(
        task_id="task-1",
        page_capture_id="capture-1",
        rule_code="alioth_test",
        evidence_artifact_ids=(),
        judge_input={},
        source_agent_run_id="producer-run-1",
    )


@pytest.mark.parametrize("batch", [False, True])
async def test_no_cited_pass_is_reviewed_with_real_materials(batch):
    service = _semantic_service(_source(), PassJudge())
    result = (
        (await service.judge_many([_semantic_request()]))[0]
        if batch
        else await service.judge(_semantic_request())
    )
    assert result.execution_status == "manual_review"
    assert result.judge_output["proposed_conclusion"] == "pass"
    assert "未引用" in result.problem_desc
    assert "shot-1" in result.evidence_artifact_ids


@pytest.mark.parametrize("batch", [False, True])
async def test_empty_primary_business_surface_cannot_pass(batch):
    png = BytesIO()
    Image.new("RGB", (100, 200), "white").save(png, format="PNG")
    source = replace(
        _source(visible_text=""),
        artifacts=(_artifact("blank-shot", "screenshot", png.getvalue(), "image/png"),),
    )
    judge = PassJudge(cited=True)
    service = _semantic_service(source, judge)
    result = (
        (await service.judge_many([_semantic_request()]))[0]
        if batch
        else await service.judge(_semantic_request())
    )
    assert result.execution_status == "evidence_insufficient"
    assert "空白图" in result.problem_desc
    assert result.evidence_artifact_ids == ("blank-shot",)
    assert judge.calls == 0


async def test_visible_business_text_is_not_rejected_for_white_background():
    png = BytesIO()
    Image.new("RGB", (100, 200), "white").save(png, format="PNG")
    source = replace(
        _source(),
        artifacts=(
            _artifact("real-shot", "screenshot", png.getvalue(), "image/png"),
            _artifact("real-text", "visible_text", "已采到的业务页面".encode(), "text/plain"),
        ),
    )
    judge = PassJudge(cited=True)
    result = await _semantic_service(source, judge).judge(_semantic_request())
    assert result.execution_status == "pass"
    assert judge.calls == 1


CF_TITLE = "hifans1.com | 520: Web server is returning an unknown error"
CF_TEXT = (
    "Web server is returning an unknown error Error code 520 "
    "You Browser Working Hong Kong Cloudflare Working hifans1.com Host Error"
)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("rule_code", ["alioth_27", "alioth_30", "alioth_34"])
async def test_cloudflare_error_document_with_refs_cannot_pass_or_be_skipped(batch, rule_code):
    source = replace(_source(visible_text=CF_TEXT), page_title=CF_TITLE, page_type="content_detail")
    judge = PassJudge(cited=True)
    service = _semantic_service(source, judge)
    service._repository.definition = replace(
        service._repository.definition,
        rule_code=rule_code,
        input_contract={
            "required_artifacts": ["screenshot"],
            "scenario_trigger_roles": ["source"],
        },
    )
    request = replace(
        _semantic_request(), rule_code=rule_code, evidence_capture_roles={"primary": "capture-1"}
    )
    result = (await service.judge_many([request]))[0] if batch else await service.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert "Cloudflare" in result.problem_desc
    assert "shot-1" in result.evidence_artifact_ids
    assert judge.calls == 0


async def test_explicit_technical_error_rule_can_still_check_error_document():
    source = replace(_source(visible_text=CF_TEXT), page_title=CF_TITLE)
    definition = RuleJudgeDefinition(
        rule_id="rule-1",
        rule_code="alioth_test",
        executor_type="rule_engine",
        judge_config={
            "engine_config": {"handler": "technical_error_text", "pattern": "Error code 520"}
        },
    )
    service = RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=ObservationPacketService(
            repository=RoleRepository({"capture-1": source})
        ),
        evidence_reader=FakeRuleJudgeEvidenceReader(CF_TEXT),
    )
    result = await service.judge(
        replace(_semantic_request(), evidence_artifact_ids=("text-1", "shot-1"))
    )
    assert result.execution_status == "fail"
    assert "Error code 520" in str(result.judge_output)


async def test_actual_blocked_web_readiness_is_not_replaced_by_detail_page_label():
    source = replace(
        _source(),
        page_type="content_detail",
        web_document_facts={
            "readiness": {"status": "human_verification_blocked", "role": "inspection"},
            "primary_subject": {"primary_subject_count": 0, "primary_subject_kind": "unknown"},
        },
    )
    judge = PassJudge(cited=True)
    result = await _semantic_service(source, judge).judge(_semantic_request())
    assert result.execution_status == "evidence_insufficient"
    assert "human_verification_blocked" in result.problem_desc
    assert judge.calls == 0


@pytest.mark.parametrize(
    "title, text",
    [
        (CF_TITLE, CF_TEXT),
        ("Just a moment...", "Verifying you are human Cloudflare security verification Ray ID"),
        ("502 Bad Gateway", "502 Bad Gateway nginx"),
    ],
)
def test_canonical_error_and_challenge_documents_are_unavailable(title, text):
    assert target_document_unavailability_reason(title=title, visible_text=text)


@pytest.mark.parametrize(
    "title, text",
    [
        ("帮助文章", "Cloudflare 520 错误解决文章 Error code 520 Cloudflare Ray ID"),
        ("我的产品", "发生错误，请稍后重试"),
        ("首页", "Verifying you are human 是本文介绍的 Cloudflare 安全提示"),
        ("订单支付结果", "502 Bad Gateway nginx 为示例排错文本"),
        (
            "Cloudflare 520: Web server is returning an unknown error 故障排查教程",
            "本文介绍 Cloudflare Error code 520 的排查步骤，先保存 Ray ID，再检查源站日志。",
        ),
        (CF_TITLE + " 故障排查教程", CF_TEXT),
    ],
)
def test_business_documents_containing_error_words_are_not_reclassified(title, text):
    assert target_document_unavailability_reason(title=title, visible_text=text) is None


class ReusedOutputJudge:
    """模拟传输缓存或另一条规则复用同一输出，最终门禁必须逐请求重验。"""

    def __init__(self, refs, *, finding_refs=()):
        self.output = SemanticJudgeModelOutput(
            conclusion="pass",
            confidence=0.99,
            reasoning_summary="已核对页面",
            evidence_refs=list(refs),
            findings=[{"summary": "可见材料", "evidence_refs": list(finding_refs)}]
            if finding_refs
            else [],
        )
        self.batch_calls = 0

    async def judge(self, *, packet, tool_results):
        return self.output

    async def judge_batch(self, *, items):
        self.batch_calls += 1
        return {packet.rule_code: self.output for packet, _ in items}


class MultiRuleRepository:
    def __init__(self, definition):
        self.definition = definition

    async def get_rule_judge_definition(self, *, task_id, rule_code):
        return replace(self.definition, rule_id=f"rule-{rule_code}", rule_code=rule_code)


async def _judge_mode(service, request, batch):
    if not batch:
        return await service.judge(request)
    service._repository = MultiRuleRepository(service._repository.definition)
    results = await service.judge_many(
        [
            request,
            replace(request, rule_code=request.rule_code + "_second"),
        ]
    )
    assert len(results) == 2
    assert results[0].execution_status == results[1].execution_status
    return results[0]


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    "refs, finding_refs",
    [
        (["artifact-from-other-task"], []),
        (["shot-1"], ["artifact-from-other-task"]),
        (["shot-1#frame-does-not-exist"], []),
    ],
)
async def test_foreign_or_invented_refs_are_reviewed_on_every_reuse(batch, refs, finding_refs):
    judge = ReusedOutputJudge(refs, finding_refs=finding_refs)
    service = _semantic_service(_source(), judge)
    for _ in range(2):
        result = await _judge_mode(service, _semantic_request(), batch)
        assert result.execution_status == "manual_review"
        assert result.judge_output["proposed_conclusion"] == "pass"
        assert result.judge_output["invalid_evidence_refs"]
        assert result.judge_output["model_output"]["evidence_refs"] == refs
        assert "不属于" in result.problem_desc
    assert judge.batch_calls == (2 if batch else 0)


@pytest.mark.parametrize("batch", [False, True])
async def test_cached_valid_ref_cannot_be_reused_for_a_different_packet(batch):
    judge = ReusedOutputJudge(["shot-1"])
    first = await _judge_mode(_semantic_service(_source(), judge), _semantic_request(), batch)
    assert first.execution_status == "pass"
    other_source = replace(
        _source(),
        artifacts=(
            _artifact("other-shot", "screenshot", b"\x89PNG\r\n\x1a\nother", "image/png"),
            _artifact("other-text", "visible_text", "另一业务采集".encode(), "text/plain"),
        ),
    )
    second = await _judge_mode(_semantic_service(other_source, judge), _semantic_request(), batch)
    assert second.execution_status == "manual_review"
    assert second.judge_output["invalid_evidence_refs"] == ["shot-1"]


@pytest.mark.parametrize("batch", [False, True])
async def test_actual_before_role_material_is_an_allowed_final_model_reference(batch):
    judge = ReusedOutputJudge(["shot-before"])
    service = _semantic_service(_source(), judge)
    service._observation_packet_service = ObservationPacketService(repository=_role_repository())
    request = replace(
        _semantic_request(),
        evidence_capture_roles=_role_request().evidence_capture_roles,
        evidence_manifest_refs=_role_request().evidence_manifest_refs,
    )
    result = await _judge_mode(service, request, batch)
    assert result.execution_status == "pass"
    assert "shot-before" in result.evidence_artifact_ids


@pytest.mark.parametrize("batch", [False, True])
async def test_actual_sequence_frame_is_an_allowed_model_reference(batch):
    output = BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
    sequence = json.dumps(
        {
            "sequence": True,
            "frames": [
                {
                    "index": 1,
                    "encoding": "base64",
                    "data": base64.b64encode(output.getvalue()).decode(),
                }
            ],
        }
    ).encode()
    source = replace(
        _source(),
        artifacts=(
            _artifact("shots", "screenshot", sequence, "application/json"),
            _artifact("real-text", "visible_text", "页面实际内容".encode(), "text/plain"),
        ),
    )
    result = await _judge_mode(
        _semantic_service(source, ReusedOutputJudge(["shots#frame-1"])),
        _semantic_request(),
        batch,
    )
    assert result.execution_status == "pass"
    assert result.evidence_artifact_ids == ("shots", "real-text")


@pytest.mark.parametrize("batch", [False, True])
async def test_cloudflare_troubleshooting_article_remains_judgeable(batch):
    source = replace(
        _source(
            visible_text=(
                "本文介绍 Cloudflare Error code 520 的排查步骤，先保存 Ray ID，再检查源站日志。"
            )
        ),
        page_title="Cloudflare 520: Web server is returning an unknown error 故障排查教程",
    )
    service = _semantic_service(source, ReusedOutputJudge(["text-1"]))
    result = await _judge_mode(service, _semantic_request(), batch)
    assert result.execution_status == "pass"


@pytest.mark.parametrize("precise_manifest", [False, True])
@pytest.mark.parametrize("batch", [False, True])
async def test_manifest_wrapper_preserves_blocked_document_facts(precise_manifest, batch):
    context = replace(
        _context_source(),
        page_title="正常业务页面标题",
        page_type="content_detail",
        web_document_facts={
            "readiness": {"role": "inspection", "status": "human_verification_blocked"},
        },
    )
    manifest = _manifest()
    repository, inner = _manifest_repository(context=context, manifest=manifest)
    judge = ReusedOutputJudge(["artifact-text"])
    service = _semantic_service(_source(), judge)
    service._observation_packet_service = ObservationPacketService(repository=repository)
    request = RuleJudgeRequest(
        task_id=MANIFEST_TASK_ID,
        page_capture_id=MANIFEST_CAPTURE_ID,
        rule_code="alioth_test",
        evidence_artifact_ids=(),
        judge_input={},
        evidence_source_page_capture_id=MANIFEST_CAPTURE_ID if precise_manifest else None,
        evidence_manifest_id=manifest.manifest_id if precise_manifest else None,
    )
    result = await _judge_mode(service, request, batch)
    assert result.execution_status == "evidence_insufficient"
    assert "human_verification_blocked" in result.problem_desc
    assert inner.saved[0]["packet_payload"]["facts"]["web_document"] == context.web_document_facts
    # 清单齐全性仍然 pass，业务文档资格没有冒充重新裁决清单质量。
    assert inner.saved[0]["quality_gate_status"] == "pass"
