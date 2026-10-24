"""播放器短流程的全屏身份复现，不以原生标签替代媒体实例证明。"""

from copy import deepcopy

import pytest

from services.api.app.application.checkpoint_judgement import judge_checkpoint
from services.api.app.application.h5_checkpoint_observation import checkpoint_facts
from services.api.app.domain.journey import parse_journey_definition
from services.api.tests.test_h5_checkpoint_consumption import sample
from services.api.tests.test_h5_shortflow_activation import definition_payload


@pytest.mark.parametrize(
    "instance,kind",
    [("other", "native_video"), (None, "native_video"), ("media-1", None), ("media-1", "image")],
)
def test_native_video_tag_cannot_override_missing_or_foreign_fullscreen_identity(instance, kind):
    snapshot = deepcopy(sample()["snapshot"])
    snapshot["document_state"] = {
        "fullscreen_active": True,
        "fullscreen_tag": "video",
        "fullscreen_media_instance_id": instance,
        "fullscreen_surface_kind": kind,
    }
    assert "fullscreen_active" not in checkpoint_facts(snapshot)


@pytest.mark.parametrize("kind,tag", [("native_video", "video"), ("custom_wrapper", "section")])
def test_explicit_fullscreen_binding_keeps_native_and_custom_player_supported(kind, tag):
    snapshot = deepcopy(sample()["snapshot"])
    snapshot["document_state"] = {
        "fullscreen_active": True,
        "fullscreen_tag": tag,
        "fullscreen_media_instance_id": "media-1",
        "fullscreen_surface_kind": kind,
    }
    assert checkpoint_facts(snapshot)["fullscreen_active"] is True


def test_wrong_native_fullscreen_identity_cannot_become_journey_rule_pass():
    payload = definition_payload()
    payload["checkpoints"][0].update(
        entry_condition={"player_visible": True},
        expected={"fullscreen_active": True},
        evidence_requirements=[{"role": "after_fullscreen", "artifact_types": ["screenshot"]}],
    )
    checkpoint = parse_journey_definition(payload).checkpoints[0]
    before = deepcopy(sample()["snapshot"])
    before["document_state"] = {"fullscreen_active": False}
    after = deepcopy(before)
    after["document_state"] = {
        "fullscreen_active": True,
        "fullscreen_tag": "video",
        "fullscreen_media_instance_id": "other",
        "fullscreen_surface_kind": "native_video",
    }
    verdict = judge_checkpoint(
        checkpoint,
        before=before,
        after=after,
        manifests=[{"quality_status": "pass", "artifacts": [{"artifact_type": "screenshot"}]}] * 2,
    )
    assert verdict["status"] == "evidence_insufficient"
    assert "measurement:fullscreen_active" in verdict["missing"]
