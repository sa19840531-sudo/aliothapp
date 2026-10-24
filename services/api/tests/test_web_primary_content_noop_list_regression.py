"""内容页已曝光时的无位移补证回归；只替换证据读取与动作传输。"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.web_interaction_facts import web_capture_content_facts
from services.api.app.application.web_interaction_precision import WebInteractionEvidenceMode
from services.api.app.application.web_primary_content_capture import (
    WebPrimaryContentCaptureService,
    build_web_primary_content_targets,
)
from services.api.app.application.web_primary_content_exposure import web_primary_content_exposed

URL = "https://example.test/archives.html"


class Evidence:
    def __init__(self, scenario="trusted", page_type="content_list"):
        self.scenario = scenario
        self.page_type = page_type

    async def read_snapshot(self, *, capture_id):
        rect = {"x": 780, "y": 342.69, "width": 64, "height": 38}
        if self.scenario == "offscreen" or (
            self.scenario == "forged_parent" and capture_id == "baseline"
        ):
            rect["y"] = 2000
        element = {
            "selector": "#subject", "tag": "a", "visible": True,
            "text": "业务条目", "href": "https://example.test/entry/1",
            "is_repeated_card": True, "dom_fingerprint": "subject",
            "within_repeated_structure": self.page_type == "content_list",
            "class_tokens": [], "rect": rect,
        }
        if self.page_type == "video_detail":
            element.update(tag="video", src="https://example.test/clip.mp4")
        elif self.page_type == "content_detail":
            element.update(tag="article", is_repeated_card=False)
        elements = [deepcopy(element)]
        if self.scenario in {"advertisement", "age_gate", "human_verification"}:
            elements.append({
                "selector": "#layer", "tag": "div", "visible": True,
                "rect": {"x": 0, "y": 0, "width": 1920, "height": 1080},
                "is_advertisement": self.scenario == "advertisement",
                "web_obstruction_layer_kind": "age_gate" if self.scenario == "age_gate" else None,
                "web_human_verification_present": self.scenario == "human_verification",
            })
        return {"viewport": {"width": 1920, "height": 1080, "scroll_y": 0},
                "elements": elements}

    async def read_url(self, *, capture_id):
        return URL

    async def read_ready(self, *, capture_id, page_role):
        if self.scenario == "unavailable":
            raise RuntimeError("当前证据不可用")
        facts = web_capture_content_facts(await self.read_snapshot(capture_id=capture_id), url=URL)
        return SimpleNamespace(
            capture_id="other-capture" if self.scenario == "wrong_id" else capture_id,
            url="https://example.test/other" if self.scenario == "wrong_url" else URL,
            page_role=page_role,
            interaction_facts=replace(
                facts, identity_verified=self.scenario != "old_capture",
                readiness_accepted=self.scenario != "loading",
                business_layer_visible=facts.business_layer_visible
                and self.scenario != "business_hidden",
                evidence_mode=WebInteractionEvidenceMode.DIAGNOSTIC
                if self.scenario == "diagnostic" else WebInteractionEvidenceMode.NATURAL,
            ),
        )


class Coordinator:
    def __init__(self, scenario="trusted"):
        self.scenario = scenario
        self.commands = []

    async def coordinate(self, command):
        self.commands.append(command)
        ordinal = len(self.commands)
        return SimpleNamespace(
            before_capture=SimpleNamespace(
                capture_id=f"before-{ordinal}", parent_capture_id=command.intent.source_capture_id,
                action_execution_id=f"action-{ordinal}",
            ),
            after_capture=SimpleNamespace(
                capture_id=f"after-{ordinal}", action_execution_id=f"action-{ordinal}",
                parent_capture_id="forged" if self.scenario == "forged_parent"
                else f"before-{ordinal}",
            ),
            interaction=SimpleNamespace(execution_outcome="succeeded", status="succeeded"),
        )


async def _capture(scenario="trusted", page_type="content_list"):
    evidence = Evidence(scenario, page_type)
    coordinator = Coordinator(scenario)

    class NoMetadataRewrite:
        async def mark_primary_content_capture(self, **kwargs):
            raise AssertionError("复用基线不得改写 capture_purpose 或伪造补证元数据")

    class NoAdditionalModelCall:
        async def assess_obstruction(self, **kwargs):
            raise AssertionError("内容基线复用不得增加模型调用")

    result = await WebPrimaryContentCaptureService(
        evidence_source=evidence, coordinator=coordinator,
        metadata_writer=NoMetadataRewrite(), obstruction_assessor=NoAdditionalModelCall(),
    ).capture(
        task_id="task", agent_run_id="run", session_id="web:run", capture_id="baseline",
        page_url=URL, page_type=page_type, deadline=datetime.now(UTC) + timedelta(minutes=2),
    )
    return result, coordinator, evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", ["content_list", "content_detail"])
async def test_current_trusted_content_page_reuses_exposed_baseline_without_scroll(page_type):
    result, coordinator, evidence = await _capture(page_type=page_type)
    snapshot = await evidence.read_snapshot(capture_id="baseline")
    target = build_web_primary_content_targets(
        capture_id="baseline", ui_snapshot=snapshot, expected_page_type=page_type,
    )[0]
    assert web_primary_content_exposed(snapshot, selector=target.selector, page_type=page_type)
    ready = await evidence.read_ready(capture_id="baseline", page_role="menu")
    assert ready.interaction_facts.business_layer_visible is True
    assert result.status == "captured"
    assert result.evidence_capture_id == "baseline"
    assert result.reused is True
    assert result.scroll_count == 0
    assert result.interaction_outcomes == ()
    assert coordinator.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", ["content_list", "content_detail"])
@pytest.mark.parametrize("scenario", [
    "offscreen", "old_capture", "loading", "diagnostic", "forged_parent",
    "wrong_id", "wrong_url", "business_hidden", "unavailable",
    "advertisement", "age_gate", "human_verification",
])
async def test_untrusted_or_unexposed_content_cannot_reuse_or_publish_after_capture(
    scenario, page_type,
):
    result, _, _ = await _capture(scenario, page_type)
    assert result.status == "blocked"
    assert result.evidence_capture_id is None
    assert result.reused is False
    assert all(not outcome.transition_arrived for outcome in result.interaction_outcomes)


@pytest.mark.asyncio
async def test_video_noop_still_records_no_effect_and_does_not_arrive():
    result, coordinator, _ = await _capture(page_type="video_detail")
    assert result.status == "blocked"
    assert result.reason_code == "WEB_PRIMARY_CONTENT_VIEWPORT_BLOCKED"
    assert len(coordinator.commands) == 2
    assert all(outcome.command_accepted for outcome in result.interaction_outcomes)
    assert all(outcome.postcondition_satisfied for outcome in result.interaction_outcomes)
    assert all(outcome.failure_reason == "effect_not_observed"
               for outcome in result.interaction_outcomes)
    assert all(not outcome.effect_observed and not outcome.transition_arrived
               for outcome in result.interaction_outcomes)
