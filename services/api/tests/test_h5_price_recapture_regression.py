"""031 真实价格缺口：时序帧不是价格位置，无业务目标不能重复观察。"""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from services.api.app.application.rule_judge import RuleJudgeDefinition, RuleJudgeService
from services.api.app.domain.inspection.evidence_gap import materialize_evidence_request
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    _h5_probe_capture_roles,
    _parse_rule_evidence_binding,
)
from services.api.tests.test_h5_rule_preconditions import _price_service
from services.api.tests.test_rule_judge import (
    FakeObservationPacketService,
    FakeRuleJudgeEvidenceReader,
    FakeRuleJudgeRepository,
    _request,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "h5_031_price_20261012_170654.json").read_text()
)


def _row(roles):
    return {
        "target_platform": "h5",
        "capture_role": "probe_result_replay",
        "parent_capture_role": "probe_before",
        "parent_capture_id": FIXTURE["before_capture_id"],
        "page_capture_id": FIXTURE["after_capture_id"],
        "rule_input_contract": {"required_capture_roles": roles},
        "request_payload": {"required_interaction": None},
    }


def test_real_case_hashes_and_order_do_not_blame_a_later_back_action():
    facts = FIXTURE["verified_artifacts"]
    assert facts["same_hash_for_all_three"]
    assert facts["dom_price_fragments"] == facts["snapshot_price_elements"] == 0
    assert not facts["snapshot_element_limit_reached"]
    times = [datetime.fromisoformat(value) for value in FIXTURE["timeline"].values()]
    assert times == sorted(times)
    assert FIXTURE["failed_request_own_action_count"] == 0


@pytest.mark.parametrize(
    "roles",
    [
        ["primary", "location_1"],
        ["primary", "location_2"],
        ["primary", "location_1", "location_2"],
    ],
)
def test_probe_pair_cannot_invent_business_price_locations_or_pin_old_before(roles):
    assert _h5_probe_capture_roles(_row(roles)) == (("primary", FIXTURE["after_capture_id"]),)


def test_real_rejudge_primary_manifest_is_after_not_stale_before():
    row = {
        **_row(["primary", "location_1"]),
        "rule_id": "rule-price",
        "rule_code": "alioth_13",
        "producer_agent_run_id": FIXTURE["run_id"],
        "manifest_id": "after-manifest",
        "manifest_version": 2,
        "manifest_payload": {"artifacts": [{"artifact_id": "after-text"}]},
        "parent_manifest_id": "before-manifest",
        "parent_manifest_version": 1,
        "parent_manifest_payload": {"artifacts": [{"artifact_id": "before-text"}]},
    }
    binding = _parse_rule_evidence_binding(row)
    assert binding.page_capture_id == FIXTURE["after_capture_id"]
    assert binding.manifest_id == "after-manifest"
    assert binding.artifact_ids == ("after-text",)
    assert binding.evidence_manifest_refs == (("primary", "after-manifest", "2"),)


@pytest.mark.parametrize(
    "roles,expected",
    [
        (["primary"], (("primary", "after"),)),
        (["before", "primary"], (("before", "before"), ("primary", "after"))),
        (["primary", "refreshed"], (("primary", "before"), ("refreshed", "after"))),
        (["source", "primary"], (("source", "before"), ("primary", "after"))),
    ],
)
def test_legitimate_temporal_roles_are_not_removed(roles, expected):
    row = {**_row(roles), "parent_capture_id": "before", "page_capture_id": "after"}
    assert _h5_probe_capture_roles(row) == expected


class _Packets(FakeObservationPacketService):
    async def build(self, request):
        packet = await super().build(request)
        packet.packet_payload["identity"] = {"target_platform": "h5"}
        packet.packet_payload["page"].update(
            surface_kind="web",
            page_type=FIXTURE["page_type"],
            page_state="stable",
            url=FIXTURE["url"],
        )
        return packet


def _measured_service():
    definition = RuleJudgeDefinition(
        rule_id="rule-price",
        rule_code=FIXTURE["rule_code"],
        executor_type="rule_engine",
        input_contract=FIXTURE["input_contract"],
        judge_config={
            "engine_config": {
                "handler": "price_relation",
                "price_pattern": r"(?P<product>\S+)\s*(?:¥|￥)(?P<price>\d+(?:\.\d+)?)",
            }
        },
    )
    text = FIXTURE["visible_text_fragment"]
    return RuleJudgeService(
        repository=FakeRuleJudgeRepository(definition),
        observation_packet_service=_Packets(),
        evidence_reader=FakeRuleJudgeEvidenceReader(
            text,
            role_artifacts={
                "primary": {"visible_text": text},
                "location_1": {"visible_text": text},
            },
        ),
    )


@pytest.mark.parametrize(
    "roles", [None, {"primary": "capture-1"}, {"primary": "capture-1", "location_1": "capture-2"}]
)
async def test_real_price_gap_retains_exact_contract_without_repeating_observe(roles):
    service = _measured_service()
    for _ in range(3):
        request = replace(
            _request(rule_code=FIXTURE["rule_code"], judge_input={}),
            evidence_capture_roles=roles,
        )
        result = await service.judge(request)
        assert result.execution_status == "evidence_insufficient"
        assert result.confidence == 0
        gap = result.judge_output["evidence_gap"]
        assert gap["recapture_policy"] == "exact"
        assert gap["disposition"] == "manual_review_required"
        assert gap["requested_request"] is None
        assert (
            gap["diagnostics"]["auto_recapture_block_reason"]
            == "H5_PRICE_LOCATION_TARGET_UNAVAILABLE"
        )
        assert (
            materialize_evidence_request(
                gap=gap,
                task_id=request.task_id,
                rule_id=result.rule_id,
                rule_code=result.rule_code,
                page_capture_id=result.page_capture_id,
                work_item_id="work-price",
                attempt_id="attempt-price",
            )
            is None
        )
    assert service._repository.definition.input_contract == FIXTURE["input_contract"]


@pytest.mark.parametrize(
    "other,verdict",
    [
        ("黄金月卡 ¥19.90", "pass"),
        ("黄金月卡 ¥29.90", "fail"),
        ("白银月卡 ¥19.90", "evidence_insufficient"),
    ],
)
async def test_real_business_positions_keep_same_changed_and_different_product_verdicts(
    other, verdict
):
    service = _price_service("h5", "web", "黄金月卡 ¥19.90", other)
    result = await service.judge(
        replace(
            _request(rule_code="price", judge_input={}),
            evidence_capture_roles={
                "primary": "product-location-a",
                "location_1": "product-location-b",
            },
        )
    )
    assert result.execution_status == verdict


async def test_missing_artifact_is_not_confused_with_missing_business_price_target():
    from services.api.app.application.rule_judge import RuleJudgeEvidenceInsufficientError

    class MissingPriceText:
        async def prepare_observation_packet(self, **kwargs):
            raise RuleJudgeEvidenceInsufficientError("规则判定缺少必需证据类型：visible_text")

    service = _measured_service()
    service._evidence_reader = MissingPriceText()
    result = await service.judge(
        replace(
            _request(rule_code=FIXTURE["rule_code"], judge_input={}),
            evidence_capture_roles={"primary": "capture-1", "location_1": "capture-2"},
        )
    )
    gap = result.judge_output["evidence_gap"]
    assert gap["requested_request"] is not None
    assert "auto_recapture_block_reason" not in gap["diagnostics"]


@pytest.mark.parametrize("later_price,verdict", [("19.90", "pass"), ("29.90", "fail")])
async def test_missing_business_target_does_not_poison_later_payment_facts(later_price, verdict):
    service = _measured_service()
    request = replace(
        _request(rule_code=FIXTURE["rule_code"], judge_input={}),
        evidence_capture_roles={"primary": "capture-1", "location_1": "capture-2"},
    )
    missing = await service.judge(request)
    assert missing.execution_status == "evidence_insufficient"
    service._evidence_reader = FakeRuleJudgeEvidenceReader(
        "黄金月卡 ¥19.90",
        role_artifacts={
            "primary": {"visible_text": "黄金月卡 ¥19.90"},
            "location_1": {"visible_text": f"黄金月卡 ¥{later_price}"},
        },
    )
    later = await service.judge(request)
    assert later.execution_status == verdict
    assert "evidence_gap" not in later.judge_output


def test_real_business_location_manifest_survives_primary_only_supplement():
    from services.api.app.domain.inspection import EvidenceBundleReference
    from services.api.app.domain.inspection.handoff import _merge_evidence_references

    baseline = EvidenceBundleReference(
        page_capture_id="original",
        producer_agent_run_id="run-price",
        artifact_ids=("original-text", "location-text"),
        evidence_capture_roles={"primary": "original", "location_1": "real-location"},
        evidence_manifest_refs={
            "primary": {"manifest_id": "original-manifest", "manifest_version": "1"},
            "location_1": {"manifest_id": "location-manifest", "manifest_version": "1"},
        },
    )
    supplement = EvidenceBundleReference(
        page_capture_id="latest",
        producer_agent_run_id="run-price",
        artifact_ids=("latest-text",),
        manifest_id="latest-manifest",
        manifest_version="1",
        evidence_capture_roles={"primary": "latest"},
    )
    merged = _merge_evidence_references(baseline, supplement)
    assert merged.evidence_capture_roles == {"primary": "latest", "location_1": "real-location"}
    assert (
        merged.evidence_manifest_refs["location_1"] == baseline.evidence_manifest_refs["location_1"]
    )
    assert merged.retained_baseline_artifact_ids["location_1"] == baseline.artifact_ids
    assert merged.artifact_ids == ("latest-text",)


async def test_old_before_is_still_rejected_not_promoted_by_same_logical_identity():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from services.api.app.application.h5_targeted_recapture import (
        H5TargetedRecaptureContext,
        H5TargetedRecapturePlanService,
    )
    from services.api.tests.test_h5_targeted_recapture import NOW
    from services.api.tests.test_h5_targeted_recapture import _request as recapture_request

    context = H5TargetedRecaptureContext(
        task_id="task-h5",
        agent_run_id="run-h5",
        finalization_epoch=4,
        session_id="h5:run-h5",
        current_capture_id="new-frame",
        target_identity="logical:comic",
        current_identity="logical:comic",
    )
    result = await H5TargetedRecapturePlanService(
        contexts=SimpleNamespace(read=AsyncMock(return_value=context)),
        clock=lambda: NOW,
    ).resolve(request=recapture_request(), agent_run_id="run-h5", finalization_epoch=4)
    assert not result.executable
    assert result.reason_code == "H5_ACTIVE_SESSION_TARGET_DRIFTED"
