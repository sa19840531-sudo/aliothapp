"""复验真实失败的导航序号、早期Manifest与判定包膨胀，不访问原站。"""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application import observation_packet
from services.api.app.application.h5_targeted_recapture import H5TargetedRecaptureOutcome
from services.api.app.application.page_capture_persistence import PageCaptureRecoveryError
from services.api.app.domain.interaction import (
    CaptureKind,
    CaptureRef,
    InteractionOperation,
    SurfaceKind,
)
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    SemanticTargetResolutionError,
    _select_structural_navigation_item,
)
from services.api.tests.test_h5_recapture_execution_limits import _consumer
from services.api.tests.test_h5_session_journey import _gate
from services.api.tests.test_h5_targeted_recapture import NOW

FIXTURES = Path(__file__).parent / "fixtures/h5_cfd7e6"


def _navigation():
    return json.loads((FIXTURES / "navigation.json").read_text())


def _select(snapshot, *, index=10, label="动漫"):
    return _select_structural_navigation_item(
        snapshot,
        target_mode="page_tab_navigation",
        navigation_index=index,
        navigation_label=label,
        operation=InteractionOperation.CLICK,
    )


def test_real_tab_index_is_dom_sibling_position_not_snapshot_priority_order():
    snapshot = _navigation()
    expected = next(item["selector"] for item in snapshot["elements"] if item["text"] == "动漫")
    assert _select(snapshot) == expected
    snapshot["elements"].reverse()
    assert _select(snapshot) == expected


@pytest.mark.parametrize(
    "mutation",
    [
        "disabled",
        "aria_disabled",
        "hidden",
        "wrong_label",
        "wrong_index",
        "obstructed",
    ],
)
def test_real_offscreen_tab_does_not_waive_action_contract(mutation):
    snapshot = _navigation()
    target = next(item for item in snapshot["elements"] if item["text"] == "动漫")
    if mutation == "disabled":
        target["disabled"] = True
    elif mutation == "aria_disabled":
        target["aria_disabled"] = "true"
    elif mutation == "hidden":
        target["visible"] = False
    elif mutation == "obstructed":
        target["rect"]["x"] = 100
        target["hit_test"] = {"center_hits_target": False}
    with pytest.raises(SemanticTargetResolutionError):
        _select(
            snapshot,
            index=9 if mutation == "wrong_index" else 10,
            label="不存在的栏目" if mutation == "wrong_label" else "动漫",
        )


def test_same_label_and_position_in_two_groups_is_still_ambiguous():
    snapshot = _navigation()
    target = deepcopy(next(item for item in snapshot["elements"] if item["text"] == "动漫"))
    target["selector"] = "#other > div:nth-of-type(10)"
    target["parent_selector"] = "#other"
    snapshot["elements"].append(target)
    with pytest.raises(SemanticTargetResolutionError) as caught:
        _select(snapshot)
    assert caught.value.error_code == "SEMANTIC_TARGET_AMBIGUOUS"


def test_unique_id_tab_uses_dom_position_not_rtl_geometry_or_snapshot_priority():
    snapshot = _navigation()
    siblings = [item for item in snapshot["elements"] if item["text"] in ("动漫", "18岁")]
    assert siblings
    target = deepcopy(next(item for item in siblings if item["text"] == "动漫"))
    target.update(selector="#anime", parent_selector="#tabs", dom_sibling_index=10)
    other = deepcopy(target)
    other.update(selector="#other", text="其他", dom_sibling_index=9)
    other["rect"]["x"] = target["rect"]["x"] + 50
    snapshot["elements"] = [target, other]
    assert _select(snapshot) == "#anime"
    snapshot["elements"].reverse()
    assert _select(snapshot) == "#anime"


def test_unique_id_without_proven_dom_position_cannot_guess_from_geometry():
    snapshot = _navigation()
    target = deepcopy(next(item for item in snapshot["elements"] if item["text"] == "动漫"))
    target.update(selector="#anime", parent_selector="#tabs")
    snapshot["elements"] = [target]
    with pytest.raises(SemanticTargetResolutionError):
        _select(snapshot, index=1)


@pytest.mark.parametrize("position", [True, "10", -1, 9])
def test_dom_position_invalid_or_conflicting_with_selector_is_rejected(position):
    snapshot = _navigation()
    target = next(item for item in snapshot["elements"] if item["text"] == "动漫")
    target["dom_sibling_index"] = position
    with pytest.raises(SemanticTargetResolutionError):
        _select(snapshot)


@pytest.mark.parametrize("foreign,corrupt", [(False, False), (True, False), (False, True)])
async def test_manifest_is_finalized_before_consumer_early_scope_read(foreign, corrupt):
    events = []
    ready = False
    capture = CaptureRef(
        task_id="other-task" if foreign else "task-h5",
        session_id="h5:run-h5",
        capture_id="capture-target",
        surface_kind=SurfaceKind.WEB,
        capture_kind=CaptureKind.PAGE,
        role="after_action",
    )

    async def finalize_capture(**kwargs):
        nonlocal ready
        assert kwargs == {"task_id": "task-h5", "capture_id": "capture-target"}
        events.append("finalize")
        ready = True

    async def verified(**kwargs):
        events.append("verify")
        if not ready or corrupt:
            raise PageCaptureRecoveryError(
                "未通过可信证据校验", error_code="H5_ACTION_RECOVERY_CAPTURE_INVALID"
            )
        return SimpleNamespace(capture=capture)

    expected = H5TargetedRecaptureOutcome(disposition="probe_published")
    runtime = SimpleNamespace(
        manifest_finalizer=SimpleNamespace(
            finalize_capture=AsyncMock(side_effect=finalize_capture)
        ),
        captures=SimpleNamespace(get_verified_capture=AsyncMock(side_effect=verified)),
    )
    consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
    consumer._session_command_gate = _gate()
    consumer._consume_in_journey = AsyncMock(return_value=expected)
    result = await consumer.consume_next(**kwargs)
    assert events[:2] == ["finalize", "verify"]
    if foreign or corrupt:
        consumer._consume_in_journey.assert_not_awaited()
        assert result is not expected
    else:
        assert result is expected
        consumer._consume_in_journey.assert_awaited_once()
        requests.settle_lease.assert_not_awaited()
        requests.release_lease.assert_not_awaited()


def _packet():
    return json.loads((FIXTURES / "packet.json").read_text())


def _bound(packet):
    return observation_packet._enforce_packet_budget(
        packet_payload=packet,
        gate_status="pass",
        gate_reason=None,
        missing_sources=[],
        quality_is_authoritative=True,
    )


def test_real_repeated_resource_registries_fit_without_erasing_criteria_or_raw_evidence():
    packet = _packet()
    original = deepcopy(packet)
    criteria = deepcopy(packet["rule"]["criteria"])
    assert criteria.get("budget_truncated") is not True
    artifact_ids = deepcopy(packet["evidence"])
    payload, status, _, missing = _bound(packet)
    assert (
        len(json.dumps(payload, ensure_ascii=False, default=str).encode())
        <= observation_packet.MAX_OBSERVATION_PACKET_BYTES
    )
    assert payload["quality_gate"].get("payload_budget_exceeded") is not True
    assert status == "pass" and not missing
    assert payload["rule"]["criteria"] == criteria
    assert payload["evidence"] == artifact_ids
    assert original["facts"]["ui_snapshot"]["frames"][0]["resource_summaries"]
    assert payload["facts"]["ui_snapshot"]["frames"][0]["elements"]


def test_budget_projection_preserves_payment_subject_and_video_clock():
    packet = _packet()
    for frame in packet["facts"]["ui_snapshot"]["frames"]:
        frame["elements"] = [
            {
                "selector": "#price",
                "tag": "button",
                "text": "会员价格 ¥30",
                "interactive": True,
                "class_tokens": ["payment-plan"],
                "h5_probe_local_display": {"kind": "price"},
                "rect": {"x": 10, "y": 120, "width": 120, "height": 40},
            },
            {
                "selector": "#player",
                "tag": "video",
                "text": "",
                "class_tokens": [],
                "rect": {"x": 0, "y": 200, "width": 390, "height": 200},
            },
        ]
        frame["h5_modal_census_selectors"] = []
        frame["media"] = [
            {
                "selector": "#player",
                "tag": "video",
                "media_instance_id": "media-one",
                "state": {"current_time": 12.5, "ready_state": 4, "paused": False},
                "viewport_coverage": 0.4,
            }
        ]
    payload, status, _, _ = _bound(packet)
    assert status == "pass"
    assert payload["quality_gate"].get("payload_budget_exceeded") is not True
    for frame in payload["facts"]["ui_snapshot"]["frames"]:
        assert {item["selector"] for item in frame["elements"]} == {"#price", "#player"}
        assert frame["media"][0]["state"]["current_time"] == 12.5
        assert frame["media"][0]["media_instance_id"] == "media-one"


def test_unbounded_essential_rule_contract_remains_blocked():
    packet = _packet()
    packet["rule"]["criteria"]["llm_checks"] = ["不可丢弃的规则事实" * 40_000]
    payload, status, _, _ = _bound(packet)
    assert status == "pass"
    assert payload["quality_gate"]["payload_budget_exceeded"] is True
    assert payload["rule"]["criteria"]["budget_truncated"] is True
