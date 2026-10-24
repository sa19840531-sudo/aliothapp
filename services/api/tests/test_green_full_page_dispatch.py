"""正式RuleJudgeService分派绑定原件；使用受控PNG与原始JSON，不替代设备验收。"""

import json
from copy import deepcopy
from dataclasses import replace
from io import BytesIO
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)
from services.api.tests.test_green_external_link_rule import facts as link_facts
from services.api.tests.test_green_navigation_rules import navigation
from services.api.tests.test_green_shared_rule_judge import definition, service


def png():
    stream = BytesIO()
    Image.new("RGB", (20, 20), "white").save(stream, format="PNG")
    return stream.getvalue()


def artifact(identity, kind, value, *, capture="before", role="primary"):
    raw = value if isinstance(value, bytes) else json.dumps(value).encode()
    return TrustedEvidenceArtifact(identity, kind, capture, role, "application/json", raw)


def bindings(fields, *, role="primary", kind="ui_snapshot", schema="native-facts.v1"):
    return {
        "schema_version": "green-shared-bindings.v1",
        "subjects": [
            {
                "subject_id": "current-object",
                "fields": {
                    key: {
                        "artifact_type": kind,
                        "capture_role": role,
                        "schema_version": schema,
                        "path": [key],
                    }
                    for key in fields
                },
            }
        ],
    }


def page_service(key, values, *, platform="app", formal_actions=None, missing_context=False):
    values = deepcopy(values)
    if key == "41":
        values["external_link_action"].update(
            session_id="native-session",
            command_sequence=1,
            formal_started_at="2026-10-21T00:00:00+00:00",
            formal_finished_at="2026-10-21T00:00:01+00:00",
        )
    rule = definition(key)
    rule = replace(
        rule,
        judge_config={
            **rule.judge_config,
            "green_shared_bindings": bindings(
                values, kind="action_trace" if key == "41" else "ui_snapshot"
            ),
        },
    )
    judge, request, reader = service(rule)
    native = artifact(
        "raw-native",
        "action_trace" if key == "41" else "ui_snapshot",
        {"schema_version": "native-facts.v1", **values},
    )
    originals = (
        artifact("before-png", "screenshot", png()),
        native,
        artifact("after-png", "screenshot", png(), capture="after", role="after_action"),
    )
    reader.prepare_observation_packet = AsyncMock(
        return_value=TrustedEvidenceBundle(
            "packet-1",
            "before",
            {"primary": "before", "after_action": "after"},
            originals,
        )
    )
    if not missing_context:
        reader.read_green_fact_context = AsyncMock(
            return_value=(platform, formal_actions or {}, ())
        )
    return (
        judge,
        replace(request, page_capture_id="before", evidence_artifact_ids=("before-png",)),
        reader,
    )


def states(result):
    return {item["name"]: item["status"] for item in result.judge_output["subresults"]}


def action_metadata(target=None):
    return {
        "parameters": {"target_url": target} if target else {},
        "session_id": "native-session",
        "action_type": "tap" if target else "scroll",
        "_session_id": "native-session",
        "_source_capture_id": "before",
        "_result_capture_id": "after",
        "_action_type": "tap" if target else "scroll",
        "_command_sequence": 1,
        "_started_at": "2026-10-21T00:00:00+00:00",
        "_finished_at": "2026-10-21T00:00:01+00:00",
    }


@pytest.mark.parametrize("case", ["fitting_five", "centered_three", "overflowing_five"])
async def test_frozen_page_bindings_dispatch_all_four_navigation_subchecks(case):
    data = navigation(overflow=case == "overflowing_five")
    actions = {}
    if case == "overflowing_five":
        actions = {"action-1": action_metadata()}
    else:
        data.pop("scroll_probe")
    if case == "centered_three":
        data["siblings"] = data["siblings"][:3]
        for key in ("rect", "clip_rect"):
            data["container"][key]["width"] = 120
        data["viewport"]["width"] = 120
        for index, row in enumerate(data["siblings"]):
            row["slot_rect"]["x"] = index * 40 + 10
            row["text_rects"][0]["x"] = index * 40 + 15
    judge, request, reader = page_service(
        "32", {"navigation": data}, platform="web", formal_actions=actions
    )
    result = await judge.judge(request)
    assert result.execution_status == "pass" and not result.judge_output["missing_evidence"]
    assert len(states(result)) == 4
    expected = {
        "fitting_five": ("skipped", "pass", "skipped", "skipped"),
        "centered_three": ("pass", "skipped", "skipped", "skipped"),
        "overflowing_five": ("skipped", "skipped", "pass", "pass"),
    }[case]
    assert tuple(states(result).values()) == expected
    assert result.problem_desc is None
    assert all(item["evidence_refs"] for item in result.judge_output["subresults"])
    reader.prepare_observation_packet.assert_awaited_once()


async def test_page_navigation_real_counterexample_survives_request_claims():
    data = navigation()
    data.pop("scroll_probe")
    data["siblings"][2]["text_rects"][0]["x"] += 1
    judge, request, _reader = page_service("32", {"navigation": data}, platform="web")
    request = replace(
        request,
        judge_input={
            "navigation": navigation(),
            "green_pass": True,
            "green_shared_bindings": {"subjects": []},
            "target_platform": "h5",
        },
    )
    result = await judge.judge(request)
    assert result.execution_status == "fail"
    assert states(result)["fit_navigation_spacing"] == "fail"
    assert "green_pass" not in result.judge_input


async def test_unbound_navigation_gesture_cannot_pass_from_request_action_claim():
    judge, request, _reader = page_service(
        "32", {"navigation": navigation(overflow=True)}, platform="web", missing_context=True
    )
    request = replace(request, judge_input={"formal_actions": {"action-1": action_metadata()}})
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert any(
        "action_outside_formal_ledger" in gap for gap in result.judge_output["missing_evidence"]
    )


@pytest.mark.parametrize("webview,status", [(False, "pass"), (True, "fail")])
async def test_page_app41_uses_exact_formal_outbound_action_and_native_receiver(webview, status):
    values = link_facts(webview=webview)
    values.pop("target_platform")
    action = values["external_link_action"]
    action.update(
        formal_started_at="2026-10-21T00:00:00+00:00",
        formal_finished_at="2026-10-21T00:00:01+00:00",
    )
    judge, request, reader = page_service(
        "41",
        values,
        formal_actions={
            "outbound": action_metadata(action["target_url"]),
        },
    )
    result = await judge.judge(request)
    assert result.execution_status == status
    assert states(result)["external_browser_handoff"] == status
    reader.read_green_fact_context.assert_awaited_once()


@pytest.mark.parametrize("platform", ["web", "h5"])
async def test_app41_platform_comes_from_repository_not_page_or_request(platform):
    values = link_facts()
    values.pop("target_platform")
    judge, request, _reader = page_service(
        "41",
        values,
        platform=platform,
        formal_actions={
            "outbound": action_metadata(values["external_link_action"]["target_url"]),
        },
    )
    request = replace(request, judge_input={"target_platform": "app", **deepcopy(values)})
    result = await judge.judge(request)
    assert result.execution_status == "skipped"
    assert result.problem_desc is None
    assert result.judge_output["subresults"][0]["applicability"] == "not_applicable"


async def test_app41_page_declared_target_cannot_override_actual_formal_target():
    values = link_facts()
    values.pop("target_platform")
    judge, request, _reader = page_service(
        "41",
        values,
        formal_actions={
            "outbound": action_metadata("https://different.invalid/"),
        },
    )
    result = await judge.judge(request)
    assert result.execution_status == "evidence_insufficient"
    assert "green_external_target_not_in_formal_intent" in result.judge_output["missing_evidence"]
