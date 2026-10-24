"""102正式页面分派的三政策、专用内容原件与范围安全，语义调用使用结构化替身。"""

import hashlib
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.green_content_judge import validate_content_policies
from services.api.app.application.rule_judge import RuleJudgeService
from services.api.app.application.semantic_rule_judge import (
    SemanticJudgeFinding,
    SemanticJudgeModelOutput,
)
from services.api.app.infrastructure.rule_judge_evidence import TrustedEvidenceBundle
from services.api.tests.test_green_full_page_dispatch import artifact, bindings, png
from services.api.tests.test_green_shared_rule_judge import definition, service
from services.api.tests.test_rule_judge import FakeObservationPacketService, FakeRuleJudgeRepository

RISKS = ("minor_safety", "political_safety", "terrorism_safety")


def policies():
    result = {}
    for risk in RISKS:
        text = "受控产品政策正文：" + risk
        result[risk] = {
            "policy_id": "product-policy-" + risk,
            "version": "2026-10-v1",
            "text": text,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
        }
    return result


def scope():
    full = {"x": 0, "y": 0, "width": 100, "height": 100}
    return {
        "scope_contract_id": "homepage-scope-v1",
        "content_artifact_ids": ["content-image"],
        "advertisement_exclusion": {
            "contract_id": "ads-v1",
            "source": "frozen_ad_selector",
            "excluded_artifact_ids": [],
        },
        "regions": [{"id": "home", "required_rect": full, "covered_rects": [deepcopy(full)]}],
    }


def semantic_result(
    status="pass", *, confidence=0.95, refs=None, reason="已独立检查本风险", missing=()
):
    return SemanticJudgeModelOutput(
        conclusion=status,
        confidence=confidence,
        reasoning_summary=reason,
        evidence_refs=["content-image"] if refs is None else refs,
        missing_evidence=list(missing),
        review_reason="年龄或上下文不能确定，须人工复核" if status == "manual_review" else None,
    )


def content_service(*, data=None, policy_config=None, no_policies=False, results=None):
    rule = definition("102")
    config = {
        **rule.judge_config,
        "green_shared_bindings": bindings(
            ("content_scope",), role="content_only", schema="native-content-scope.v1"
        ),
        "green_content_scope_contract": {
            "id": "homepage-scope-v1",
            "regions": {"home": {"x": 0, "y": 0, "width": 100, "height": 100}},
            "ad_contract_id": "ads-v1",
            "content_capture_roles": ["content_only"],
        },
    }
    if not no_policies:
        config["green_content_policies"] = policies() if policy_config is None else policy_config
    rule = replace(rule, judge_config=config)
    _judge, request, reader = service(rule)
    originals = (
        artifact("primary-ad-image", "screenshot", png(), capture="ad-page"),
        artifact(
            "content-scope",
            "ui_snapshot",
            {
                "schema_version": "native-content-scope.v1",
                "content_scope": scope() if data is None else data,
            },
            capture="content-page",
            role="content_only",
        ),
        artifact("content-image", "screenshot", png(), capture="content-page", role="content_only"),
    )
    reader.prepare_observation_packet = AsyncMock(
        return_value=TrustedEvidenceBundle(
            "packet-1",
            "ad-page",
            {"primary": "ad-page", "content_only": "content-page"},
            originals,
        )
    )

    class ContentPacketService(FakeObservationPacketService):
        async def build(self, options):
            packet = await super().build(options)
            payload = deepcopy(packet.packet_payload)
            payload["facts"].update(
                visible_text="广告敏感词不得进入内容语义包", page_claimed_pass=True
            )
            return replace(
                packet,
                packet_payload=payload,
                evidence_artifact_ids=tuple(item.artifact_id for item in originals),
                image_payloads=(
                    ("primary-ad-image", png(), "image/png"),
                    ("content-image", png(), "image/png"),
                ),
            )

    semantic = SimpleNamespace(judge=AsyncMock(side_effect=results or [semantic_result()] * 3))
    judge = RuleJudgeService(
        repository=FakeRuleJudgeRepository(rule),
        evidence_reader=reader,
        observation_packet_service=ContentPacketService(),
        semantic_judge=semantic,
    )
    return judge, replace(request, page_capture_id="ad-page"), semantic, rule


def subitems(result):
    return {item["name"]: item for item in result.judge_output["subresults"]}


async def test_complete_frozen_scope_and_three_independent_passes_finish_formal102():
    judge, request, semantic, _rule = content_service()
    result = await judge.judge(request)
    assert result.execution_status == "pass" and result.problem_desc is None
    assert result.confidence == 0.95
    assert not result.judge_output["missing_evidence"]
    assert all(item["status"] == "pass" for item in subitems(result).values())
    assert all(item["evidence_refs"] == ["content-image"] for item in subitems(result).values())
    assert semantic.judge.await_count == 3


async def test_formal_risk_subresult_retains_policy_findings_location_and_review_details():
    finding = SemanticJudgeFinding(
        summary="内容区域中的人物年龄和上下文需要复核",
        evidence_refs=["content-image"],
        element_refs=["content-image:content-region"],
    )
    uncertain = semantic_result("manual_review").model_copy(
        update={
            "findings": [finding],
            "conflicts": ["年龄与内容上下文证据不一致"],
        }
    )
    judge, request, _semantic, _rule = content_service(
        results=[
            uncertain,
            semantic_result(),
            semantic_result(),
        ]
    )
    result = await judge.judge(request)
    item = subitems(result)[RISKS[0]]
    assert result.execution_status == "manual_review" and result.confidence == 0.95
    assert item["policy_reference"] == {
        key: policies()[RISKS[0]][key] for key in ("policy_id", "version", "sha256")
    }
    assert item["findings"] == [finding.model_dump(mode="json")]
    assert item["evidence_refs"] == ["content-image"]
    assert item["conflicts"] == uncertain.conflicts
    assert item["review_reason"] == uncertain.review_reason
    assert "年龄" in item["review_reason"]


async def test_formal102_runs_three_independent_risks_with_only_bound_content_images():
    judge, request, semantic, _rule = content_service(
        results=[
            semantic_result("fail"),
            semantic_result("pass"),
            semantic_result("manual_review"),
        ]
    )
    result = await judge.judge(request)
    assert result.execution_status == "fail"
    assert tuple(item["status"] for item in subitems(result).values()) == (
        "fail",
        "pass",
        "manual_review",
        "pass",
    )
    assert semantic.judge.await_count == 3
    for risk, call in zip(RISKS, semantic.judge.call_args_list, strict=True):
        selected = call.kwargs["packet"]
        assert selected.evidence_artifact_ids == ("content-image",)
        assert [image[0] for image in selected.image_payloads] == ["content-image"]
        assert call.kwargs["tool_results"][0]["risk"] == risk
        assert policies()[risk]["text"] in selected.packet_payload["rule"]["check_point"]
        assert set(selected.packet_payload["facts"]) == {"content_artifact_ids"}
        assert "page_claimed_pass" not in str(selected.packet_payload)
        assert "广告敏感词" not in str(selected.packet_payload)


async def test_no_product_policy_never_calls_general_semantic_fallback():
    judge, request, semantic, _rule = content_service(no_policies=True)
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert all(subitems(result)[risk]["status"] == "evidence_insufficient" for risk in RISKS)
    semantic.judge.assert_not_awaited()


async def test_only_configured_risk_policy_runs_and_other_risks_remain_missing():
    judge, request, semantic, _rule = content_service(
        policy_config={RISKS[1]: policies()[RISKS[1]]}
    )
    result = await judge.judge(request)
    assert semantic.judge.await_count == 1
    assert subitems(result)[RISKS[1]]["status"] == "pass"
    assert result.execution_status == "evidence_insufficient"


async def test_uncertain_age_and_low_confidence_risk_preserve_real_manual_review():
    judge, request, _semantic, _rule = content_service(
        results=[
            semantic_result("manual_review"),
            semantic_result("pass", confidence=0.4),
            semantic_result(),
        ]
    )
    result = await judge.judge(request)
    assert result.execution_status == "manual_review"
    assert subitems(result)[RISKS[0]]["status"] == "manual_review"
    assert subitems(result)[RISKS[1]]["status"] == "manual_review"
    assert "年龄" in subitems(result)[RISKS[0]]["reason"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("review_reason", "年龄不确定，必须由人工确认上下文"),
        ("conflicts", ["人物年龄存在相互矛盾的证据"]),
    ],
)
async def test_semantic_pass_cannot_override_its_own_uncertainty_or_conflicts(field, value):
    uncertain = semantic_result().model_copy(update={field: value})
    judge, request, _semantic, _rule = content_service(
        results=[
            uncertain,
            semantic_result(),
            semantic_result(),
        ]
    )
    result = await judge.judge(request)
    assert subitems(result)[RISKS[0]]["status"] == "manual_review"
    assert result.execution_status == "manual_review"


async def test_shrinking_the_required_region_cannot_make_overall102_pass():
    data = scope()
    tiny = {"x": 0, "y": 0, "width": 1, "height": 1}
    data["regions"][0].update(required_rect=tiny, covered_rects=[tiny])
    judge, request, _semantic, _rule = content_service(data=data)
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert subitems(result)["content_scope"]["status"] == "evidence_insufficient"
    assert "content_region_coverage:home" in result.judge_output["missing_evidence"]


async def test_primary_screenshot_cannot_claim_advertisement_free_content_role():
    data = scope()
    data["content_artifact_ids"] = ["primary-ad-image"]
    judge, request, semantic, _rule = content_service(data=data)
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    semantic.judge.assert_not_awaited()


async def test_wrong_risk_references_are_rejected_without_borrowing_other_risks_result():
    judge, request, semantic, _rule = content_service(
        results=[
            semantic_result(refs=["primary-ad-image"]),
            semantic_result(),
            semantic_result(),
        ]
    )
    result = await judge.judge(request)
    assert semantic.judge.await_count == 3
    assert result.execution_status == "evidence_insufficient"
    assert subitems(result)[RISKS[0]]["status"] == "evidence_insufficient"
    assert subitems(result)[RISKS[1]]["status"] == "pass"


@pytest.mark.parametrize("fault", ["bad_region_id", "bad_source", "huge_number", "missing_region"])
async def test_malformed_original_scope_is_missing_evidence_not_a_judge_crash(fault):
    data = scope()
    if fault == "bad_region_id":
        data["regions"][0]["id"] = []
    elif fault == "bad_source":
        data["advertisement_exclusion"]["source"] = []
    elif fault == "huge_number":
        data["regions"][0]["covered_rects"][0]["width"] = 10**400
    else:
        data["regions"] = []
    judge, request, _semantic, _rule = content_service(data=data)
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert result.judge_output["missing_evidence"]


@pytest.mark.parametrize("field", ["policy_id", "version", "text", "sha256"])
def test_product_policy_requires_identity_version_original_text_and_matching_hash(field):
    config = policies()
    config[RISKS[0]][field] = "" if field != "sha256" else "0" * 64
    with pytest.raises(ValueError, match="受控政策"):
        validate_content_policies(config)


async def test_request_policies_and_scope_cannot_override_frozen_missing_product_policy():
    judge, request, semantic, _rule = content_service(no_policies=True)
    request = replace(
        request,
        judge_input={
            "green_content_policies": policies(),
            "green_content_scope_contract": scope(),
            "green_pass": True,
        },
    )
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    semantic.judge.assert_not_awaited()
