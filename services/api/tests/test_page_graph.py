"""页面状态图领域规范化测试。"""

from dataclasses import replace

import pytest

from services.api.app.domain.exploration.page_graph import (
    PAGE_IDENTITY_CONFIRMED,
    PAGE_IDENTITY_UNKNOWN,
    build_app_visual_after_proof,
    build_page_state_snapshot,
    build_transition_decision,
    normalize_local_state,
    normalize_page_identity,
)


def test_identity_does_not_fall_back_to_page_type() -> None:
    identity = normalize_page_identity(
        metadata={},
        surface_kind="android",
        capture_id="capture-1",
    )

    assert identity.status == PAGE_IDENTITY_UNKNOWN
    assert identity.key == "unknown:capture-1"


def test_business_identity_and_local_state_are_stable() -> None:
    identity = normalize_page_identity(
        metadata={"business_page_identity": "com.demo|activity.Home|home"},
        surface_kind="android",
        capture_id="capture-1",
    )
    state = normalize_local_state(
        page_state="  Detail State ",
        metadata={"popup_visible": False},
    )

    assert identity.status == PAGE_IDENTITY_CONFIRMED
    assert identity.key.startswith("identity:com.demo")
    assert state == "detail_state"


def test_url_and_runtime_location_never_become_business_identity() -> None:
    url_identity = normalize_page_identity(
        metadata={"url": "https://demo.test/detail?id=42"},
        surface_kind="web",
        capture_id="capture-url",
    )
    runtime_identity = normalize_page_identity(
        metadata={"package_name": "com.demo", "activity": "MainActivity"},
        surface_kind="android",
        capture_id="capture-runtime",
    )

    assert url_identity.status == PAGE_IDENTITY_UNKNOWN
    assert runtime_identity.status == PAGE_IDENTITY_UNKNOWN


def test_overlay_and_external_target_are_not_business_arrivals() -> None:
    source = build_page_state_snapshot(
        capture_id="capture-source",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=2,
        command_sequence=1,
        capture_role="before_action",
        metadata={"business_page_identity": "home"},
    )
    target = build_page_state_snapshot(
        capture_id="capture-target",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=2,
        command_sequence=2,
        capture_role="after_action",
        metadata={
            "business_page_identity": "home",
            "page_state": "popup",
            "popup_visible": True,
        },
    )

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"transition_proof": "trusted"},
    )

    assert target.is_overlay is True
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is False
    assert "target_overlay" in decision.reason_codes


def test_completed_capture_pair_without_trusted_postcondition_is_not_arrival() -> None:
    source = build_page_state_snapshot(
        capture_id="capture-source",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=2,
        command_sequence=1,
        capture_role="before_action",
        metadata={"business_page_identity": "home"},
    )
    target = build_page_state_snapshot(
        capture_id="capture-target",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=2,
        command_sequence=2,
        capture_role="after_action",
        metadata={"business_page_identity": "detail"},
    )

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"status": "succeeded", "result_capture_id": "capture-target"},
    )

    assert decision.proof == "insufficient"
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is False
    assert "transition_proof_missing" in decision.reason_codes


def test_trusted_logical_postcondition_requires_confirmed_matching_identities() -> None:
    source = build_page_state_snapshot(
        capture_id="capture-source",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=2,
        command_sequence=1,
        capture_role="before_action",
        metadata={
            "logical_surface": _logical_surface("a"),
        },
    )
    target = build_page_state_snapshot(
        capture_id="capture-target",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=2,
        command_sequence=2,
        capture_role="after_action",
        metadata={
            "logical_surface": _logical_surface("b"),
        },
    )
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "logical_transition_proof": "trusted",
        "transition_kind": "logical_page_changed",
        "before_identity_fingerprint": _logical_surface("a")["identity_fingerprint"],
        "after_identity_fingerprint": _logical_surface("b")["identity_fingerprint"],
    }

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"state_diff": {"logical_surface_transition": transition}},
    )

    assert source.identity.status == PAGE_IDENTITY_CONFIRMED
    assert target.identity.status == PAGE_IDENTITY_CONFIRMED
    assert decision.proof == "trusted"
    assert decision.counts_as_business_arrival is True
    assert decision.replay_eligible is True


def test_trusted_postcondition_with_inferred_or_cross_epoch_target_is_rejected() -> None:
    source = build_page_state_snapshot(
        capture_id="capture-source",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=2,
        command_sequence=1,
        capture_role="before_action",
        metadata={"logical_surface": _logical_surface("a")},
    )
    target = build_page_state_snapshot(
        capture_id="capture-target",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=3,
        command_sequence=2,
        capture_role="after_action",
        metadata={"url": "https://demo.test/detail"},
    )
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "logical_transition_proof": "trusted",
        "transition_kind": "logical_page_changed",
        "before_identity_fingerprint": _logical_surface("a")["identity_fingerprint"],
        "after_identity_fingerprint": _logical_surface("b")["identity_fingerprint"],
    }

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"state_diff": {"logical_surface_transition": transition}},
    )

    assert target.identity.status == PAGE_IDENTITY_UNKNOWN
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is False
    assert "cross_epoch_capture_pair" in decision.reason_codes
    assert "target_identity_unconfirmed" in decision.reason_codes


def test_state_fingerprint_does_not_change_when_only_page_type_changes() -> None:
    common = {
        "capture_id": "capture-1",
        "task_id": "task-1",
        "agent_run_id": "run-1",
        "surface_kind": "web",
        "session_id": "session-1",
        "finalization_epoch": 1,
        "command_sequence": 1,
        "capture_role": "observation",
        "metadata": {"business_page_identity": "home", "local_state": "ready"},
    }

    home = build_page_state_snapshot(page_type="home", **common)
    detail = build_page_state_snapshot(page_type="content_list", **common)

    assert home.state_fingerprint == detail.state_fingerprint


def _logical_surface(seed: str) -> dict[str, object]:
    return {
        "protocol_version": "logical_surface.v1",
        "identity_fingerprint": f"sha256:{seed * 64}"[:71],
        "state_fingerprint": f"sha256:{seed * 64}"[:71],
        "render_profile": {"kind": "dom_spa", "status": "confirmed"},
        "evidence_quality": "high",
    }


def test_failed_action_keeps_terminal_edge_without_target_capture() -> None:
    source = build_page_state_snapshot(
        capture_id="capture-source",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=1,
        command_sequence=1,
        capture_role="before_action",
        metadata={"business_page_identity": "home"},
    )

    decision = build_transition_decision(
        action_status="failed",
        source=source,
        target=None,
        error_code="driver_failed",
    )

    assert decision.terminal is True
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is False
    assert "target_state_missing" in decision.reason_codes


def test_app_visual_after_contract_proves_arrival_without_using_page_type_as_identity() -> None:
    source_metadata = _app_visual_metadata("首页", "capture-source")
    target_metadata = _app_visual_metadata("详情-对象A", "capture-target")
    target_metadata["app_visual_after"] = build_app_visual_after_proof(
        capture_id="capture-target",
        source_capture_id="capture-source",
        capture_role="after_action",
        page_type="content_detail",
        page_state="stable",
        metadata={**target_metadata, "surface_kind": "android"},
    )
    source = build_page_state_snapshot(
        capture_id="capture-source",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="android",
        session_id="session-1",
        finalization_epoch=1,
        command_sequence=1,
        capture_role="before_action",
        metadata=source_metadata,
        page_type="home",
        page_state="stable",
    )
    target = build_page_state_snapshot(
        capture_id="capture-target",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="android",
        session_id="session-1",
        finalization_epoch=1,
        command_sequence=2,
        capture_role="after_action",
        metadata=target_metadata,
        page_type="content_detail",
        page_state="stable",
    )

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={},
    )

    assert source.identity.status == PAGE_IDENTITY_CONFIRMED
    assert target.identity.status == PAGE_IDENTITY_CONFIRMED
    assert "content_detail" not in target.identity.key
    assert decision.proof == "trusted"
    assert decision.counts_as_business_arrival is True


def test_app_visual_title_without_business_identity_contract_stays_unknown() -> None:
    identity = normalize_page_identity(
        metadata=_app_visual_title_only_metadata("详情"),
        surface_kind="android",
        capture_id="capture-title-only",
    )

    assert identity.status == PAGE_IDENTITY_UNKNOWN


def test_app_same_business_object_with_no_state_change_is_not_trusted_arrival() -> None:
    source_metadata = _app_visual_metadata("内容列表", "capture-source")
    target_metadata = _app_visual_metadata("内容列表", "capture-target")
    target_metadata["app_visual_after"] = build_app_visual_after_proof(
        capture_id="capture-target",
        source_capture_id="capture-source",
        capture_role="after_action",
        page_type="content_list",
        page_state="stable",
        metadata={**target_metadata, "surface_kind": "android"},
    )
    source = _app_snapshot("capture-source", "before_action", source_metadata, 1)
    target = _app_snapshot("capture-target", "after_action", target_metadata, 2)

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={},
    )

    assert decision.proof == "insufficient"
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is False


def test_app_same_page_pagination_is_state_change_not_new_page_arrival() -> None:
    source_metadata = _app_visual_metadata("内容列表", "capture-source")
    source_metadata["local_state"] = "page=1"
    target_metadata = _app_visual_metadata("内容列表", "capture-target")
    target_metadata["local_state"] = "page=2"
    target_metadata["app_visual_after"] = build_app_visual_after_proof(
        capture_id="capture-target",
        source_capture_id="capture-source",
        capture_role="after_action",
        page_type="content_list",
        page_state="stable",
        metadata={**target_metadata, "surface_kind": "android"},
    )
    source = _app_snapshot("capture-source", "before_action", source_metadata, 1)
    target = _app_snapshot("capture-target", "after_action", target_metadata, 2)

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={},
    )

    assert source.identity.key == target.identity.key
    assert source.state_fingerprint != target.state_fingerprint
    assert decision.proof == "trusted"
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is True


@pytest.mark.parametrize("changed_fact", ["image", "page_type", "readiness"])
def test_app_nonbusiness_visual_changes_do_not_prove_arrival(changed_fact) -> None:
    before = _app_visual_metadata("内容列表", "before")
    after = _app_visual_metadata(
        "内容列表", "after", image_seed="b" if changed_fact == "image" else "a"
    )
    before["image_signature"] = "sha256:" + "a" * 64
    after["image_signature"] = "sha256:" + ("b" if changed_fact == "image" else "a") * 64
    after["app_visual_after"] = build_app_visual_after_proof(
        capture_id="after",
        source_capture_id="before",
        capture_role="after_action",
        page_type="content_list",
        page_state="stable",
        metadata=after,
    )
    source = _app_snapshot("before", "before_action", before, 1)
    target = _app_snapshot("after", "after_action", after, 2)
    if changed_fact == "page_type":
        target = replace(target, page_type="content_detail")
    if changed_fact == "readiness":
        source = replace(source, page_state="loading")
    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={},
    )
    assert decision.proof == "insufficient"
    assert not decision.counts_as_business_arrival
    assert not decision.replay_eligible


@pytest.mark.parametrize(
    "after_url",
    [
        "https://example.test/list?page=2&sort=price&tab=favorites",
        "https://example.test/list?page=1&sort=price&tab=favorites#bottom",
    ],
)
def test_url_pagination_filter_and_anchor_are_recoverable_local_states(after_url) -> None:
    source = _web_url_snapshot(
        "before",
        "https://example.test/list?page=1&sort=date&tab=all",
        "before_action",
        1,
    )
    target = _web_url_snapshot("after", after_url, "after_action", 2)
    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"state_diff": {"location_changed": True}},
    )
    assert source.identity.key == target.identity.key
    assert source.state_fingerprint != target.state_fingerprint
    assert decision.proof == "trusted"
    assert decision.replay_eligible
    assert not decision.counts_as_business_arrival


def test_hash_route_keeps_object_identity_but_splits_local_pagination() -> None:
    first = _web_url_snapshot("a", "https://example.test/#/detail?id=1&page=1", "observation", 1)
    next_page = _web_url_snapshot(
        "b", "https://example.test/#/detail?id=1&page=2", "observation", 2
    )
    other = _web_url_snapshot("c", "https://example.test/#/detail?id=2&page=1", "observation", 3)
    assert first.identity.key == next_page.identity.key
    assert first.state_fingerprint != next_page.state_fingerprint
    assert first.identity.key != other.identity.key


def test_logical_local_state_requires_bound_before_and_after_state_proof() -> None:
    before_metadata = {
        "logical_surface": {**_logical_surface("a"), "state_fingerprint": "sha256:" + "b" * 64}
    }
    after_metadata = {
        "logical_surface": {**_logical_surface("a"), "state_fingerprint": "sha256:" + "c" * 64}
    }
    common = dict(
        task_id="task",
        agent_run_id="run",
        surface_kind="web",
        session_id="session",
        finalization_epoch=0,
    )
    source = build_page_state_snapshot(
        capture_id="before",
        command_sequence=1,
        capture_role="before_action",
        metadata=before_metadata,
        **common,
    )
    target = build_page_state_snapshot(
        capture_id="after",
        command_sequence=2,
        capture_role="after_action",
        metadata=after_metadata,
        **common,
    )
    transition = {
        "protocol_version": "logical_surface_transition.v1",
        "logical_transition_proof": "trusted",
        "transition_kind": "surface_state_changed",
        "before_identity_fingerprint": "sha256:" + "a" * 64,
        "after_identity_fingerprint": "sha256:" + "a" * 64,
        "before_state_fingerprint": "sha256:" + "b" * 64,
        "after_state_fingerprint": "sha256:" + "c" * 64,
    }
    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"logical_surface_transition": transition},
    )
    assert decision.replay_eligible
    assert not decision.counts_as_business_arrival
    transition["after_state_fingerprint"] = "sha256:" + "d" * 64
    rejected = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"logical_surface_transition": transition},
    )
    assert not rejected.replay_eligible
    assert rejected.proof == "insufficient"


def test_ordinary_h5_url_after_capture_proves_full_route_change() -> None:
    source = _web_url_snapshot(
        "capture-source",
        "https://example.test/home?tab=all#top",
        "before_action",
        1,
    )
    target = _web_url_snapshot(
        "capture-target",
        "https://example.test/detail?id=42#comments",
        "after_action",
        2,
    )

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"state_diff": {"location_changed": True}},
    )

    assert decision.proof == "trusted"
    assert decision.counts_as_business_arrival is True
    assert decision.replay_eligible is True


def test_ordinary_h5_query_and_hash_routes_remain_distinct() -> None:
    first = _web_url_snapshot(
        "capture-1",
        "https://example.test/detail?id=1#part-a",
        "observation",
        1,
    )
    second = _web_url_snapshot(
        "capture-2",
        "https://example.test/detail?id=2#part-b",
        "observation",
        2,
    )

    assert first.identity.key != second.identity.key
    assert first.state_payload["url"].endswith("?id=1#part-a")
    assert second.state_payload["url"].endswith("?id=2#part-b")


def test_ordinary_h5_external_after_is_evidence_only() -> None:
    source = _web_url_snapshot(
        "capture-source",
        "https://example.test/home",
        "before_action",
        1,
    )
    target_metadata = _url_metadata(
        "capture-target",
        "https://ads.example.net/landing?campaign=1",
    )
    target_metadata.update({"is_external": True, "external_navigation": True})
    target = build_page_state_snapshot(
        capture_id="capture-target",
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=1,
        command_sequence=2,
        capture_role="after_action",
        metadata=target_metadata,
    )

    decision = build_transition_decision(
        action_status="completed",
        source=source,
        target=target,
        result_payload={"state_diff": {"location_changed": True}},
    )

    assert decision.proof == "trusted"
    assert decision.counts_as_business_arrival is False
    assert decision.replay_eligible is False
    assert "external_navigation" in decision.reason_codes


def test_app_state_fingerprint_ignores_visual_evidence_but_tracks_filter_state() -> None:
    first_metadata = _app_visual_metadata("内容列表", "capture-first")
    first_metadata.update(
        {
            "detection_confidence": 0.81,
            "image_signature": "sha256:" + "a" * 64,
            "selected_filter": "all",
        }
    )
    second_metadata = _app_visual_metadata("内容列表", "capture-second", image_seed="b")
    second_metadata.update(
        {
            "detection_confidence": 0.99,
            "image_signature": "sha256:" + "b" * 64,
            "selected_filter": "all",
        }
    )
    first = _app_snapshot("capture-first", "observation", first_metadata, 1)
    second = _app_snapshot("capture-second", "observation", second_metadata, 2)

    assert first.identity.key == second.identity.key
    assert first.state_fingerprint == second.state_fingerprint

    filtered_metadata = _app_visual_metadata("内容列表", "capture-filtered", image_seed="b")
    filtered_metadata["selected_filter"] = "favorites"
    filtered = _app_snapshot("capture-filtered", "observation", filtered_metadata, 3)
    assert filtered.identity.key == first.identity.key
    assert filtered.state_fingerprint != first.state_fingerprint


def test_same_visual_name_with_different_business_object_has_distinct_identity() -> None:
    first_metadata = _app_visual_metadata("详情", "capture-first", object_id="object-a")
    second_metadata = _app_visual_metadata("详情", "capture-second", object_id="object-b")

    first = _app_snapshot("capture-first", "observation", first_metadata, 1)
    second = _app_snapshot("capture-second", "observation", second_metadata, 2)

    assert first.identity.status == PAGE_IDENTITY_CONFIRMED
    assert second.identity.status == PAGE_IDENTITY_CONFIRMED
    assert first.identity.key != second.identity.key


def _app_visual_metadata(
    page_name: str, capture_id: str, *, object_id: str = "object-a", image_seed: str = "a"
) -> dict[str, object]:
    from services.api.app.domain.exploration.app_visual_page_identity import (
        build_app_visual_page_facts,
    )
    from services.api.tests.test_app_visual_page_identity import _anchor, _observation

    if page_name == "首页":
        page_role = "home"
    elif "列表" in page_name:
        page_role = "collection"
    else:
        page_role = "detail"
    observation = _observation(object_id=object_id if page_role == "detail" else None)
    observation["page_role"] = page_role
    observation["location"] = _anchor("内容详情" if page_role == "detail" else page_name)
    visual_facts = build_app_visual_page_facts(
        identity_observation=observation,
        local_state_observation=None,
        capture_id=capture_id,
        screenshot_artifact_id=f"artifact-{capture_id}",
        image_signature="sha256:" + image_seed * 64,
        package_name="com.demo",
        activity="MainActivity",
        page_state="stable",
        overlay_signature=None,
        surface_facts={},
    )
    return {
        "package_name": "com.demo",
        "activity": "MainActivity",
        "image_signature": "sha256:" + image_seed * 64,
        "screenshot_artifact_id": f"artifact-{capture_id}",
        "surface_facts": {
            **visual_facts,
            "visual_page_name": {
                "protocol_version": "app-visual-page-name.v1",
                "page_name": page_name,
                "source": "visual_inference",
                "confidence": 0.92,
            },
            "visual_page_type_confidence": 0.92,
        },
    }


def _app_visual_title_only_metadata(page_name: str) -> dict[str, object]:
    return {
        "package_name": "com.demo",
        "activity": "MainActivity",
        "surface_facts": {
            "visual_page_name": {
                "protocol_version": "app-visual-page-name.v1",
                "page_name": page_name,
                "source": "visual_inference",
                "confidence": 0.99,
            },
            "visual_page_type_confidence": 0.99,
        },
    }


def _app_snapshot(
    capture_id: str,
    capture_role: str,
    metadata: dict[str, object],
    command_sequence: int,
):
    return build_page_state_snapshot(
        capture_id=capture_id,
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="android",
        session_id="session-1",
        finalization_epoch=1,
        command_sequence=command_sequence,
        capture_role=capture_role,
        metadata=metadata,
        page_type="content_list",
        page_state="stable",
    )


def _url_metadata(capture_id: str, url: str) -> dict[str, object]:
    return {
        "url": url,
        "trusted_url_identity": {
            "protocol_version": "web_url_after.v1",
            "verified": True,
            "verified_by": "action_after_capture",
            "capture_id": capture_id,
        },
    }


def _web_url_snapshot(
    capture_id: str,
    url: str,
    capture_role: str,
    command_sequence: int,
):
    return build_page_state_snapshot(
        capture_id=capture_id,
        task_id="task-1",
        agent_run_id="run-1",
        surface_kind="web",
        session_id="session-1",
        finalization_epoch=1,
        command_sequence=command_sequence,
        capture_role=capture_role,
        metadata=_url_metadata(capture_id, url),
    )
