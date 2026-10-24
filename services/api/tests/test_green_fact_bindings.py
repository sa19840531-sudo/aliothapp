"""服务器冻结事实绑定、正式动作真实性与首帧PNG原件归属验证。"""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO

import pytest
from PIL import Image

from services.api.app.application.green_fact_bindings import (
    bind_green_fact_views,
    read_green_fact_views,
    validate_green_shared_bindings,
)
from services.api.app.application.green_layout_stability_rules import judge_interaction_subitem
from services.api.app.application.green_temporal_rules import judge_temporal_rule
from services.api.tests.test_green_process_judge import process_case

START = "2026-10-21T01:00:00+00:00"
FINISH = "2026-10-21T01:00:01+00:00"


def config(*names):
    return {
        "schema_version": "green-shared-bindings.v1",
        "subjects": [
            {
                "subject_id": "panel",
                "fields": {
                    name: {
                        "artifact_type": "action_trace" if name != "temporal" else "temporal_trace",
                        "capture_role": "after_action",
                        "schema_version": "native-facts.v1",
                        "path": [name],
                    }
                    for name in names
                },
            }
        ],
    }


def record():
    expected = {"kind": "panel_state", "subject_id": "panel", "state": {"expanded": True}}
    return {
        "action_id": "action",
        "session_id": "h5:session",
        "operation": "click",
        "command_sequence": 2,
        "before_capture_id": "before",
        "after_capture_id": "after",
        "formal_started_at": START,
        "formal_finished_at": FINISH,
        "target_id": "button",
        "clock_id": "native-clock",
        "started_at_ms": 1,
        "finished_at_ms": 5,
        "expected_response": expected,
        "input_event": {
            "action_id": "action",
            "source": "browser_native_event",
            "is_trusted": True,
            "operation": "click",
            "target_id": "button",
            "clock_id": "native-clock",
            "at_ms": 2,
        },
        "response": {**copy.deepcopy(expected), "observed_at_ms": 6},
        "response_window": {
            "clock_id": "native-clock",
            "start_ms": 0,
            "end_ms": 10,
            "complete": True,
        },
    }


def formal_actions():
    return {
        "action": {
            "task_id": "task",
            "session_id": "h5:session",
            "action_type": "click",
            "parameters": {
                "expected_response": record()["expected_response"],
                "target_url": "https://example.invalid/outbound",
            },
            "_session_id": "h5:session",
            "_action_type": "click",
            "_command_sequence": 2,
            "_source_capture_id": "before",
            "_result_capture_id": "after",
            "_started_at": START,
            "_finished_at": FINISH,
        }
    }


def source(payload, *, kind="action_trace", capture="after", artifact="trace", role="after_action"):
    return capture, role, artifact, kind, json.dumps(payload).encode()


def bind(values=None, *, formal=None, sources=None, names=("response_actions",)):
    if values is None:
        values = {"response_actions": [record()]}
    if sources is None:
        sources = (
            source({"schema_version": "native-facts.v1", **values}),
            source(
                {"schema_version": "ui-snapshot.v2"},
                kind="ui_snapshot",
                capture="before",
                artifact="ui-before",
                role="before_action",
            ),
        )
    return bind_green_fact_views(
        sources,
        config(*names),
        action_ids=("action",),
        formal_actions=formal_actions() if formal is None else formal,
        target_platform="h5",
    )


def png():
    stream = BytesIO()
    Image.new("RGB", (2, 2), (0, 0, 0)).save(stream, "PNG")
    return stream.getvalue()


def media_sources(*, borrowed=False, malformed_event=None, invalid_png=False):
    artifact = "png-before" if borrowed else "png-after"
    event = {
        "schema_version": "native-presented-frame.v1",
        "source": "browser_video_frame_callback",
        "capture_id": "after",
        "artifact_id": artifact,
        "content_id": "content",
        "at_ms": 20,
        "clock_id": "media-clock",
    }
    if malformed_event:
        event.update(malformed_event)
    temporal = {
        "window": {"coverage": "complete", "start_ms": 0, "end_ms": 30, "clock_id": "media-clock"},
        "handoff": {
            "ad_presence": "present",
            "ad_id": "ad",
            "content_id": "content",
            "end": {"kind": "ended", "media_id": "ad", "at_ms": 10, "clock_id": "media-clock"},
            "first_frame": {
                "content_id": "content",
                "at_ms": 20,
                "clock_id": "media-clock",
                "capture_id": "after",
                "artifact_id": artifact,
                "event_artifact_id": "native-frame-event",
            },
        },
    }
    return (
        source({"schema_version": "native-facts.v1", "temporal": temporal}, kind="temporal_trace"),
        source(event, kind="temporal_trace", artifact="native-frame-event"),
        (
            "after",
            "after_action",
            "png-after",
            "screenshot",
            b"\x89PNG\r\n\x1a\ninvalid" if invalid_png else png(),
        ),
        ("before", "before_action", "png-before", "screenshot", png()),
    )


def test_formal_action_session_operation_captures_sequence_and_exact_timestamps_are_bound():
    views = bind()
    assert not views[0].issues
    assert set(views[0].capture_ids) == {"before", "after"}
    assert judge_interaction_subitem("correct_response", views).status == "pass"


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "another-session"),
        ("operation", "tap"),
        ("command_sequence", 3),
        ("command_sequence", True),
        ("before_capture_id", "after"),
        ("after_capture_id", "before"),
        ("formal_started_at", "2026-10-21T01:00:00.000001+00:00"),
        ("formal_finished_at", "2026-10-21T01:00:02+00:00"),
        ("formal_started_at", "2026-10-21T01:00:00"),
        ("formal_started_at", None),
        ("action_id", "not-in-ledger"),
    ],
)
def test_native_json_action_claim_cannot_replace_actual_formal_ledger(field, value):
    action = record()
    action[field] = value
    views = bind({"response_actions": [action]})
    assert views[0].issues
    assert judge_interaction_subitem("correct_response", views).status == "evidence_insufficient"


def test_timestamp_comparison_preserves_exact_instant_without_invented_tolerance():
    action = record()
    action["formal_started_at"] = "2026-10-21T09:00:00+08:00"
    action["formal_finished_at"] = "2026-10-21T09:00:01+08:00"
    assert not bind({"response_actions": [action]})[0].issues


def test_expected_response_and_external_url_must_match_real_intent():
    action = record()
    action["expected_response"]["state"]["expanded"] = False
    assert (
        "green_expected_response_not_in_formal_intent"
        in bind({"response_actions": [action]})[0].issues
    )
    action = record()
    action["target_url"] = "https://attacker.invalid/outbound"
    assert (
        "green_external_target_not_in_formal_intent"
        in bind(
            {"external_link_action": action},
            names=("external_link_action",),
        )[0].issues
    )


def test_both_response_and_repeat_sections_are_checked_and_reset_is_a_formal_action():
    repeated = record()
    repeated["session_id"] = "foreign-session"
    views = bind(
        {"response_actions": [record()], "repeat_actions": [repeated]},
        names=("response_actions", "repeat_actions"),
    )
    assert "green_action_identity_not_in_formal_ledger" in views[0].issues
    action = record()
    action["reset"] = {"action_id": "unregistered-reset"}
    views = bind({"repeat_actions": [action]}, names=("repeat_actions",))
    assert "green_action_missing_from_formal_ledger" in views[0].issues


@pytest.mark.parametrize("bad", [None, True, [], "bad", {"action_id": []}])
def test_malformed_action_values_create_issues_without_exceptions(bad):
    views = bind({"response_actions": bad})
    assert views[0].issues


@pytest.mark.parametrize("bad", [None, True, [], "bad", {"action": []}])
def test_malformed_formal_context_cannot_authorize_raw_action(bad):
    # None在辅助函数中表示默认上下文，因此用空对象覆盖该情况。
    views = bind(formal={} if bad is None else bad)
    assert views[0].issues


def test_hash_verified_evidence_reader_adds_real_action_metadata_and_rejects_bad_intent():
    _, evidence = process_case()
    action = evidence.actions[0]
    capture = evidence.captures[1]
    # 这里只验证读取器的正式意图容错；不改写原始采集内容来伪装一次业务成功。
    invalid = replace(action, intent_payload=b"[]")
    result = read_green_fact_views(
        replace(evidence, actions=(invalid,)), config("response_actions")
    )
    assert "green_formal_action_payload_invalid" in result[0].issues
    valid = replace(
        action,
        started_at=datetime(2026, 10, 21, tzinfo=UTC),
        finished_at=datetime(2026, 10, 21, 0, 0, 1, tzinfo=UTC),
    )
    result = read_green_fact_views(replace(evidence, actions=(valid,)), config("response_actions"))
    assert capture.capture_id in {row.capture_id for row in evidence.captures}
    assert "green_formal_action_payload_invalid" not in result[0].issues


def test_presented_frame_requires_its_own_capture_png_and_native_event():
    views = bind(sources=media_sources(), names=("temporal",), formal={})
    assert not views[0].issues
    assert views[0].get("png_artifact_ids") == ("png-after",)
    assert len(views[0].get("presented_frame_events")) == 1
    assert judge_temporal_rule("62", views, ("presented_first_frame",))[0].status == "pass"


@pytest.mark.parametrize(
    "mode",
    ["borrowed_png", "fake_png", "event_wrong_capture", "bad_event_artifact", "bad_event_source"],
)
def test_png_from_other_capture_or_header_only_and_malformed_native_event_cannot_pass(mode):
    mutations = {
        "event_wrong_capture": {"capture_id": "before"},
        "bad_event_artifact": {"artifact_id": {"forged": "png-after"}},
        "bad_event_source": {"source": ["browser_video_frame_callback"]},
    }
    views = bind(
        sources=media_sources(
            borrowed=mode == "borrowed_png",
            invalid_png=mode == "fake_png",
            malformed_event=mutations.get(mode),
        ),
        names=("temporal",),
        formal={},
    )
    assert not views[0].get("presented_frame_events")
    assert (
        judge_temporal_rule("62", views, ("presented_first_frame",))[0].status
        == "evidence_insufficient"
    )


def test_duplicate_artifact_identity_cannot_move_png_bytes_to_a_different_capture():
    sources = list(media_sources())
    sources.append(("before", "before_action", "png-after", "screenshot", png()))
    views = bind(sources=sources, names=("temporal",), formal={})
    assert "green_fact_duplicate_artifact_identity" in views[0].issues
    assert "png-after" not in views[0].get("png_artifact_ids")
    assert (
        judge_temporal_rule("62", views, ("presented_first_frame",))[0].status
        == "evidence_insufficient"
    )


@pytest.mark.parametrize(
    "bad_source", [None, b"bad", ("only", "two"), ({}, "role", "artifact", "ui_snapshot", b"{}")]
)
def test_bad_source_record_is_safe_and_never_silently_authorized(bad_source):
    sources = [
        source({"schema_version": "native-facts.v1", "response_actions": [record()]}),
        bad_source,
    ]
    views = bind(sources=sources)
    assert "green_fact_source_identity_invalid" in views[0].issues


def test_deep_fact_values_and_arbitrary_capture_reference_are_rejected():
    nested = current = {}
    for _ in range(48):
        current["child"] = {}
        current = current["child"]
    views = bind(
        {"temporal": nested},
        names=("temporal",),
        sources=(
            source(
                {"schema_version": "native-facts.v1", "temporal": nested}, kind="temporal_trace"
            ),
        ),
    )
    assert "green_fact_nesting_limit_exceeded" in views[0].issues
    action = record()
    action["before_capture_id"] = "some-other-task-capture"
    assert "green_fact_capture_outside_input" in bind({"response_actions": [action]})[0].issues


def test_frozen_binding_cannot_request_unknown_fields_or_executable_paths():
    value = config("response_actions")
    value["subjects"][0]["fields"]["response_actions"]["path"] = [0]
    with pytest.raises(ValueError, match="有界对象路径"):
        validate_green_shared_bindings(value)
    original = config("response_actions")
    copied = validate_green_shared_bindings(original)
    copied["subjects"][0]["fields"]["response_actions"]["path"].append("mutated")
    assert original == config("response_actions")


@pytest.mark.parametrize("kind", ["har", "action_trace"])
def test_business_temporal_facts_cannot_be_bound_from_arbitrary_response_or_action_body(kind):
    value = config("temporal")
    value["subjects"][0]["fields"]["temporal"]["artifact_type"] = kind
    with pytest.raises(ValueError, match="来源类型与分项不匹配"):
        validate_green_shared_bindings(value)


def test_malformed_formal_identity_type_cannot_authorize_equal_json_claims():
    action, formal = record(), formal_actions()
    action["session_id"] = {"claimed": "session"}
    formal["action"]["session_id"] = formal["action"]["_session_id"] = {"claimed": "session"}
    views = bind({"response_actions": [action]}, formal=formal)
    assert "green_formal_action_metadata_invalid" in views[0].issues
