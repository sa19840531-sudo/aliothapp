"""受控原件经真实只读加载、冻结绑定与聚合入口验证分派，不代表产品或设备验收。"""

import copy
import hashlib
import json
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from services.api.app.application.green_fact_bindings import read_green_fact_views
from services.api.app.application.green_rule_evidence import evaluate_green_aggregate_context
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceManifestReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
    seal_evidence_input_set,
)
from services.api.app.domain.interaction import ActionIntent, build_action_intent_hash
from services.api.app.infrastructure.db.green_process_evidence import load_green_process_evidence
from services.api.tests.test_green_fact_bindings import record as response_record
from services.api.tests.test_green_process_evidence import NOW, FixtureConnection, context
from services.api.tests.test_green_temporal_rules import case


def add_original(fixture, kind, value, *, capture_id=None):
    """先进入真实清单和原件台账，再由正式读取器核验字节与摘要。"""
    capture_id = capture_id or fixture.ids["after"]
    identity = str(uuid4())
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    content_type = "image/png" if isinstance(value, bytes) else "application/json"
    extension = "png" if content_type == "image/png" else "json"
    path = f"{fixture.ids['task']}/{capture_id}/{identity}.{extension}"
    checksum = "sha256:" + hashlib.sha256(raw).hexdigest()
    reference = ArtifactReference(
        artifact_id=identity,
        artifact_type=kind,
        role="after_action",
        storage_path=path,
        checksum=checksum,
        size_bytes=len(raw),
        content_type=content_type,
    )
    old = EvidenceManifestReference.from_dict(fixture.manifests[capture_id])
    manifest = build_evidence_manifest_reference(
        task_id=fixture.ids["task"],
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=old.manifest_version + 1,
        quality_status=EvidenceQualityStatus.PASS,
        created_at=NOW,
        artifacts=(*old.artifacts, reference),
    )
    fixture.manifests[capture_id] = manifest.to_dict()
    fixture.captures[capture_id]["manifest_payload"] = manifest.to_dict()
    fixture.artifacts[identity] = {
        "artifact_type": kind,
        "source": "baseline",
        "lifecycle_status": "active",
        "storage_path": path,
        "checksum": checksum,
        "size_bytes": len(raw),
        "content_type": content_type,
    }
    fixture.payloads[path] = raw
    return identity


def reseal(fixture):
    """冻结新增原件后的最终成员，不能在验证身份对象上直接追加未核验原件。"""
    fixture.input_set = seal_evidence_input_set(
        task_id=fixture.ids["task"],
        source_agent_run_id=fixture.ids["run"],
        rule_id=fixture.ids["rule"],
        rule_code=fixture.rule["rule_code"],
        rule_contract_hash="frozen-contract",
        ruleset_snapshot_hash="frozen-ruleset",
        scope=EvidenceInputScope.TASK,
        scope_key=fixture.rule["judge_config"]["green_rule_contract"]["input_kind"],
        finalization_epoch=0,
        eligibility_snapshot_id="eligibility-1",
        eligibility_snapshot_hash="eligibility-hash",
        selection_policy_hash="selection-hash",
        eligible_capture_ids=tuple(fixture.captures),
        candidates=tuple(
            EvidenceInputCandidate(
                manifest=EvidenceManifestReference.from_dict(fixture.manifests[capture]),
                capture_role=fixture.captures[capture]["capture_role"],
                page_sequence=index + 1,
            )
            for index, capture in enumerate(fixture.captures)
        ),
        sealed_at=NOW,
    )


def fixture_case(rule, *, mutation=None, native_source="browser_video_frame_callback"):
    fixture = FixtureConnection(rule)
    all_facts = copy.deepcopy(case().get("temporal"))
    names = {
        "s72": ("window", "state"),
        "s49": ("window", "state", "fault", "recovery", "support", "payment"),
        "s73": ("window", "state", "loading", "success"),
        "62": ("window", "handoff"),
    }[rule]
    temporal = {name: all_facts[name] for name in names}
    if rule == "s49":
        temporal["recovery"]["trigger_action_id"] = "action-1"
    if rule == "62":
        image = BytesIO()
        Image.new("RGB", (2, 2), "white").save(image, format="PNG")
        png_id = add_original(fixture, "screenshot", image.getvalue())
        native_id = add_original(
            fixture,
            "temporal_trace",
            {
                "schema_version": "native-presented-frame.v1",
                "source": native_source,
                "capture_id": fixture.ids["after"],
                "artifact_id": png_id,
                "content_id": "content",
                "at_ms": 60,
                "clock_id": "clock",
            },
        )
        temporal["handoff"]["first_frame"].update(
            capture_id=fixture.ids["after"],
            artifact_id=png_id,
            event_artifact_id=native_id,
        )
    if mutation is not None:
        mutation(temporal)
    add_original(
        fixture,
        "temporal_trace",
        {
            "schema_version": "controlled-green-process.v1",
            "facts": temporal,
        },
    )
    fixture.rule["judge_config"]["green_shared_bindings"] = {
        "schema_version": "green-shared-bindings.v1",
        "subjects": [
            {
                "subject_id": "module/request",
                "fields": {
                    "temporal": {
                        "artifact_type": "temporal_trace",
                        "capture_role": "after_action",
                        "schema_version": "controlled-green-process.v1",
                        "path": ["facts"],
                    }
                },
            }
        ],
    }
    reseal(fixture)
    return fixture


async def evaluate(fixture):
    evidence = await load_green_process_evidence(
        fixture,
        input_set=fixture.input_set,
        artifact_store=fixture,
    )
    assert evidence.verified_read and evidence.fingerprint
    return evidence, evaluate_green_aggregate_context(context(fixture, evidence))


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["s72", "s49", "s73", "62"])
async def test_frozen_binding_reaches_complete_shared_rule_from_verified_originals(rule):
    fixture = fixture_case(rule)
    evidence, result = await evaluate(fixture)
    assert not evidence.issues
    assert result.execution_status == "pass"
    assert result.review_status is None and result.error_code is None
    items = result.wrapper_payload["green"]["subitems"]
    assert all(item["status"] in {"pass", "skipped"} and not item["missing"] for item in items)
    assert any(item["status"] == "pass" for item in items)
    assert result.wrapper_payload["green"]["evidence_fingerprint"] == evidence.fingerprint


def negative(rule, temporal):
    if rule == "s72":
        temporal["state"]["messages"] = []
    elif rule == "s49":
        temporal["state"]["targets"] = []
    elif rule == "s73":
        temporal["loading"]["feedback"] = []
    else:
        temporal["handoff"]["segments"][0]["state"] = "blank"


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["s72", "s49", "s73", "62"])
async def test_business_counterexample_remains_failed_through_aggregate_entry(rule):
    fixture = fixture_case(rule, mutation=lambda temporal: negative(rule, temporal))
    evidence, result = await evaluate(fixture)
    assert not evidence.issues and result.execution_status == "fail"
    assert any(item["status"] == "fail" for item in result.wrapper_payload["green"]["subitems"])


def missing(rule, temporal):
    if rule == "s72":
        temporal["state"]["request_id"] = None
    elif rule == "s49":
        temporal["fault"]["request_id"] = None
    elif rule == "s73":
        temporal["loading"]["clock_id"] = None
    else:
        temporal["handoff"]["first_frame"]["event_artifact_id"] = "foreign-event"


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["s72", "s49", "s73", "62"])
async def test_missing_business_identity_or_event_cannot_be_promoted_by_shared_dispatch(rule):
    fixture = fixture_case(rule, mutation=lambda temporal: missing(rule, temporal))
    _evidence, result = await evaluate(fixture)
    assert result.execution_status == "evidence_insufficient"
    assert result.review_status == "pending"
    assert result.wrapper_payload["green"]["missing_evidence"]


@pytest.mark.asyncio
@pytest.mark.parametrize("rule", ["s72", "s49", "s73", "62"])
async def test_missing_frozen_binding_keeps_ordinary_old_task_behavior(rule):
    fixture = fixture_case(rule)
    fixture.rule["judge_config"].pop("green_shared_bindings")
    _evidence, result = await evaluate(fixture)
    assert result.execution_status == "evidence_insufficient"


@pytest.mark.asyncio
async def test_server_binder_derives_first_frame_metadata_from_real_png_and_native_event():
    fixture = fixture_case("62")
    evidence, result = await evaluate(fixture)
    views = read_green_fact_views(evidence, fixture.rule["judge_config"]["green_shared_bindings"])
    assert len(views) == 1 and not views[0].issues
    frame = views[0].get("temporal")["handoff"]["first_frame"]
    assert views[0].get("png_capture_ids")[frame["artifact_id"]] == fixture.ids["after"]
    native = views[0].get("presented_frame_events")
    assert len(native) == 1
    assert native[0]["event_artifact_id"] == frame["event_artifact_id"]
    assert result.execution_status == "pass"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["page_dom", "model_summary"])
async def test_png_and_json_claim_without_native_source_do_not_prove_first_frame(source):
    fixture = fixture_case("62", native_source=source)
    _evidence, result = await evaluate(fixture)
    assert result.execution_status == "evidence_insufficient"
    assert (
        "matching_first_frame_native_event" in result.wrapper_payload["green"]["missing_evidence"]
    )


def s35_response_fixture(mode):
    """原件先入清单再冻结；正式动作意图的预期与原始响应分别保存。"""
    fixture = FixtureConnection("s35")
    fixture.identity["target_platform"] = "h5"
    row = fixture.actions[0]
    action = response_record()
    action.update(
        action_id=row["action_execution_id"],
        session_id=row["session_id"],
        command_sequence=row["command_sequence"],
        before_capture_id=fixture.ids["before"],
        after_capture_id=fixture.ids["after"],
        formal_started_at=row["started_at"].isoformat(),
        formal_finished_at=row["action_finished_at"].isoformat(),
    )
    action["input_event"]["action_id"] = row["action_execution_id"]
    row["intent_payload"]["parameters"]["expected_response"] = copy.deepcopy(
        action["expected_response"]
    )
    row["intent_hash"] = build_action_intent_hash(ActionIntent.from_dict(row["intent_payload"]))
    if mode == "contradiction":
        action["response"]["state"]["expanded"] = False
    elif mode == "untrusted_event":
        action["input_event"]["is_trusted"] = False
    elif mode == "session_spoof":
        action["session_id"] = "another-session"
    elif mode == "expected_spoof":
        action["expected_response"]["state"]["expanded"] = False
        action["response"]["state"]["expanded"] = False
    elif mode == "timestamp_spoof":
        action["formal_started_at"] = "2026-10-21T00:00:00+00:00"
    add_original(
        fixture,
        "action_trace",
        {"schema_version": "controlled-s35-response.v1", "response_actions": [action]},
    )
    fixture.rule["judge_config"]["green_shared_bindings"] = {
        "schema_version": "green-shared-bindings.v1",
        "subjects": [
            {
                "subject_id": "panel",
                "fields": {
                    "response_actions": {
                        "artifact_type": "action_trace",
                        "capture_role": "after_action",
                        "schema_version": "controlled-s35-response.v1",
                        "path": ["response_actions"],
                    }
                },
            }
        ],
    }
    reseal(fixture)
    return fixture


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,expected",
    [
        ("correct", "pass"),
        ("contradiction", "fail"),
        ("untrusted_event", "evidence_insufficient"),
        ("session_spoof", "evidence_insufficient"),
        ("expected_spoof", "evidence_insufficient"),
        ("timestamp_spoof", "evidence_insufficient"),
    ],
)
async def test_verified_s35_originals_formal_intent_and_frozen_binding_reach_shared_response(
    mode, expected
):
    fixture = s35_response_fixture(mode)
    evidence, result = await evaluate(fixture)
    assert not evidence.issues and evidence.verified_read and evidence.fingerprint
    views = read_green_fact_views(evidence, fixture.rule["judge_config"]["green_shared_bindings"])
    assert len(views) == 1
    if mode in {"session_spoof", "expected_spoof", "timestamp_spoof"}:
        assert views[0].issues
    else:
        assert not views[0].issues
    items = result.wrapper_payload["green"]["subitems"]
    assert items[0]["key"] == "correct_response" and items[0]["status"] == expected
    assert items[0]["action_ids"] == ["action-1"]
    assert all(item["status"] == "evidence_insufficient" for item in items[1:])
    assert result.execution_status == ("fail" if expected == "fail" else "evidence_insufficient")
    assert result.review_status == "pending"
    assert result.wrapper_payload["green"]["evidence_fingerprint"] == evidence.fingerprint
