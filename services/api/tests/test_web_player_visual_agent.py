"""播放器截图模型只读输出的闭集与坐标边界。"""

import json
import struct

import pytest
from pydantic import ValidationError

from services.api.app.agents.explorer.web_assessment_agent import (
    WebExplorationAssessmentAgent,
    WebPlayerVisualEnvelopeModel,
)
from services.api.app.application.web_assessment_evidence import WebAssessmentEvidence
from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response


class EvidenceLoader:
    async def load(
        self,
        *,
        screenshot_artifact_id,
        dom_artifact_id,
        visible_text_artifact_id=None,
        supplemental_screenshot_artifact_ids=(),
    ):
        assert screenshot_artifact_id == "screenshot-player"
        assert dom_artifact_id == "dom-player"
        assert visible_text_artifact_id is None
        assert supplemental_screenshot_artifact_ids == ()
        return WebAssessmentEvidence(
            screenshot_artifact_id=screenshot_artifact_id,
            screenshot_bytes=b"\x89PNG\r\n\x1a\n"
            + b"\x00\x00\x00\rIHDR"
            + struct.pack(">II", 1920, 1080)
            + b"\x08\x06\x00\x00\x00",
            screenshot_media_type="image/png",
            dom_artifact_id=dom_artifact_id,
            dom_text="<main><video id='player'></video></main>",
        )


@pytest.mark.asyncio
async def test_player_visual_agent_returns_readonly_control_bounds():
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-player-visual.v1",
                        "candidate_id": "candidate-1",
                        "status": "visible",
                        "confidence": 0.94,
                        "reason": "播放器中央有播放键",
                        "controls": [
                            {"kind": "play", "x": 0.49, "y": 0.47, "width": 0.04, "height": 0.06}
                        ],
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    agent = WebExplorationAssessmentAgent(client=client, evidence_loader=EvidenceLoader())
    result = await agent.assess_player_visual(
        screenshot_artifact_id="screenshot-player",
        dom_artifact_id="dom-player",
        candidate_id="candidate-1",
        target_rect={"x": 300, "y": 240, "width": 1000, "height": 560},
        viewport={"width": 1920, "height": 1080},
    )
    assert result.status == "visible"
    assert result.screenshot_width == 1920
    assert result.screenshot_height == 1080
    assert result.controls[0].kind == "play"
    assert len(client.received_messages) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {
            "status": "uncertain",
            "controls": [{"kind": "play", "x": 0.5, "y": 0.5, "width": 0.1, "height": 0.1}],
        },
        {
            "status": "visible",
            "controls": [{"kind": "play", "x": 0.98, "y": 0.5, "width": 0.1, "height": 0.1}],
        },
        {
            "status": "not_visible",
            "controls": [{"kind": "pause", "x": 0.5, "y": 0.5, "width": 0.1, "height": 0.1}],
        },
        {
            "status": "visible",
            "controls": [{"kind": "play", "x": 0.5, "y": 0.5, "width": 0.1, "height": 0.1}] * 5,
        },
    ],
)
def test_player_visual_schema_rejects_inconsistent_controls(payload):
    with pytest.raises(ValidationError):
        WebPlayerVisualEnvelopeModel.model_validate(
            {
                "schema_version": "web-player-visual.v1",
                "candidate_id": "candidate-1",
                "confidence": 0.8,
                "reason": "状态有冲突",
                **payload,
            }
        )


@pytest.mark.asyncio
async def test_player_visual_agent_rejects_wrong_candidate_id():
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-player-visual.v1",
                        "candidate_id": "other",
                        "status": "visible",
                        "confidence": 0.9,
                        "reason": "播放键可见",
                        "controls": [],
                    },
                    ensure_ascii=False,
                )
            )
        ]
        * 2
    )
    agent = WebExplorationAssessmentAgent(client=client, evidence_loader=EvidenceLoader())
    with pytest.raises(ValueError, match="candidate_id"):
        await agent.assess_player_visual(
            screenshot_artifact_id="screenshot-player",
            dom_artifact_id="dom-player",
            candidate_id="candidate-1",
            target_rect={"x": 300, "y": 240, "width": 1000, "height": 560},
            viewport={"width": 1920, "height": 1080},
        )
