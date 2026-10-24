"""页面树和连续恢复路线只消费同运行可信事实，不能反向制造覆盖。"""

from services.api.app.application.page_graph_projection import (
    build_exploration_tree,
    build_recovery_routes,
)

SCOPE = {"task_id": "task", "agent_run_id": "run", "finalization_epoch": 1}


def state(capture, page, revision="stable", **overrides):
    return {
        **SCOPE,
        "state_id": capture,
        "logical_page_key": page,
        "state_fingerprint": revision,
        "surface_session_id": "session",
        "identity_status": "confirmed",
        "counts_as_business_page": True,
        "is_overlay": False,
        "is_external": False,
        "captured_at": capture,
        **overrides,
    }


def edge(identifier, source, target, **overrides):
    return {
        **SCOPE,
        "transition_id": identifier,
        "source_state_id": source,
        "target_state_id": target,
        "status": "completed",
        "proof": "trusted",
        "counts_as_business_arrival": True,
        "replay_eligible": True,
        "risk_level": "low",
        **overrides,
    }


def graph(states, transitions=()):
    return {**SCOPE, "states": states, "transitions": list(transitions), "status": "available"}


def tree(value, facts=(), candidates=()):
    return build_exploration_tree(value, progress_facts=list(facts), candidates=list(candidates))


def test_tree_groups_repeat_captures_but_keeps_distinct_business_states():
    result = tree(graph([state("1", "home"), state("2", "home"), state("3", "home", "tab-b")]))
    assert result["roots"] == ["home"]
    assert len(result["nodes"]) == 1
    assert [item["capture_ids"] for item in result["nodes"][0]["states"]] == [["1", "2"], ["3"]]
    assert result["nodes"][0]["status"] == "visited"
    assert result["coverage_authority_changed"] is False


def test_tree_keeps_cycles_and_multiple_entries_outside_parent_links():
    result = tree(
        graph(
            [state("1", "home"), state("2", "list"), state("3", "detail")],
            [
                edge("a", "1", "2"),
                edge("b", "2", "3"),
                edge("back", "3", "1"),
                edge("alt", "1", "3"),
            ],
        )
    )
    assert result["roots"] == ["home"]
    assert [node["parent_page_key"] for node in result["nodes"]] == [None, "home", "list"]
    assert result["cross_transition_ids"] == ["back", "alt"]


def test_startup_overlay_does_not_create_home_and_nested_overlays_attach_by_path():
    rows = [
        state("0", "unknown", is_overlay=True),
        state("1", "home"),
        state("2", "overlay-a", is_overlay=True),
        state("3", "overlay-b", is_overlay=True),
    ]
    links = [
        edge("a", "1", "2", counts_as_business_arrival=False),
        edge("b", "2", "3", counts_as_business_arrival=False),
    ]
    result = tree(graph(rows, links))
    assert result["roots"] == ["home"]
    assert [item["capture_id"] for item in result["unattached_overlays"]] == ["0"]
    assert [item["capture_id"] for item in result["nodes"][0]["overlays"]] == ["2", "3"]


def test_pending_candidates_remain_unvisited_targets_not_business_nodes():
    result = tree(
        graph([state("1", "home")]),
        candidates=[
            {
                "candidate_id": "menu",
                "source_capture_id": "1",
                "status": "pending",
                "semantic_target": "详情",
            }
        ],
    )
    assert len(result["nodes"]) == 1
    assert result["nodes"][0]["targets"][0]["progress_status"] == "unvisited"
    assert result["nodes"][0]["status"] == "visited"


def test_overlay_observation_attaches_by_capture_parent_without_inventing_transition():
    result = tree(
        graph(
            [
                state("1", "home"),
                state("2", "popup", is_overlay=True, parent_capture_id="1"),
            ]
        )
    )
    assert [overlay["capture_id"] for overlay in result["nodes"][0]["overlays"]] == ["2"]
    assert result["nodes"][0]["children"] == []
    assert result["nodes"][0]["transition_ids"] == []


def test_only_blocked_outgoing_targets_do_not_mark_page_completed():
    result = tree(
        graph([state("1", "home")]),
        facts=[
            {
                "capture_id": "1",
                "evidence_status": "complete",
                "inspection_status": "completed",
                "exploration_finished": True,
            }
        ],
        candidates=[{"candidate_id": "blocked", "source_capture_id": "1", "status": "blocked"}],
    )
    assert result["nodes"][0]["status"] == "blocked"


def test_progress_requires_evidence_inspection_and_exploration_completion():
    value = graph(
        [state("1", "home"), state("2", "list"), state("3", "detail"), state("4", "profile")]
    )
    result = tree(
        value,
        facts=[
            {
                "capture_id": "1",
                "evidence_status": "complete",
                "inspection_status": "completed",
                "exploration_finished": True,
            },
            {
                "capture_id": "2",
                "evidence_status": "insufficient",
                "reason_codes": ["screenshot_missing"],
            },
            {"capture_id": "3", "blocked": True, "reason_codes": ["login_required"]},
            {"capture_id": "4", "evidence_status": "complete", "inspection_status": "completed"},
        ],
    )
    assert [node["status"] for node in result["nodes"]] == [
        "completed",
        "needs_evidence",
        "blocked",
        "visited",
    ]
    assert result["nodes"][1]["states"][0]["reason_codes"] == ["screenshot_missing"]


def test_later_capture_does_not_inherit_older_completed_evidence_implicitly():
    result = tree(
        graph([state("1", "home"), state("2", "home")]),
        facts=[
            {
                "capture_id": "1",
                "evidence_status": "complete",
                "inspection_status": "completed",
                "exploration_finished": True,
            }
        ],
    )
    assert result["nodes"][0]["status"] == "visited"
    assert result["nodes"][0]["states"][0]["evidence_status"] == "unknown"


def test_recovery_returns_contiguous_multistep_path_and_ignores_historical_failed_prefix():
    value = graph(
        [state("1", "home"), state("2", "list"), state("3", "list"), state("4", "detail")],
        [edge("a", "1", "2"), edge("b", "3", "4")],
    )
    result = build_recovery_routes(value, source_state_id="1", target_identity="detail")
    assert result["status"] == "available"
    assert [item["transition_id"] for item in result["routes"][0]["steps"]] == ["a", "b"]
    value["transitions"][0]["status"] = "failed"
    assert (
        build_recovery_routes(value, source_state_id="1", target_identity="detail")["routes"] == []
    )


def test_recovery_rejects_unknown_epoch_and_missing_session():
    value = graph([state("1", "home"), state("2", "detail")], [edge("a", "1", "2")])
    value["finalization_epoch"] = None
    assert build_recovery_routes(value, source_state_id=None, target_identity=None) == {
        "status": "unavailable",
        "reason_code": "run_epoch_unknown",
        "routes": [],
    }
    value["finalization_epoch"] = 1
    for row in value["states"]:
        row["surface_session_id"] = None
    assert (
        build_recovery_routes(value, source_state_id="1", target_identity="detail")["routes"] == []
    )


def test_recovery_cannot_jump_between_different_states_or_sessions():
    for changed in ({"state_fingerprint": "tab-b"}, {"surface_session_id": "other"}):
        value = graph(
            [
                state("1", "home"),
                state("2", "list"),
                state("3", "list", **changed),
                state("4", "detail"),
            ],
            [edge("a", "1", "2"), edge("b", "3", "4")],
        )
        assert (
            build_recovery_routes(value, source_state_id="1", target_identity="detail")["routes"]
            == []
        )


def test_h5_recovery_rebinds_first_step_from_same_confirmed_logical_page() -> None:
    value = graph(
        [
            state(
                "current",
                "home",
                "timer-new",
                state_payload={"url": "https://example.test/home"},
            ),
            state(
                "historical",
                "home",
                "timer-old",
                state_payload={"url": "https://example.test/home"},
            ),
            state(
                "detail",
                "detail",
                state_payload={"url": "https://example.test/detail"},
            ),
        ],
        [edge("open-detail", "historical", "detail")],
    )
    value["target_platform"] = "h5"

    result = build_recovery_routes(
        value,
        source_state_id="current",
        target_identity="detail",
    )

    assert result["status"] == "available"
    route = result["routes"][0]
    assert route["source_state_id"] == "current"
    assert route["steps"][0]["source_state_id"] == "historical"
    assert route["source_rebind"] == {
        "protocol_version": "h5-page-graph-source-rebind.v1",
        "requested_source_state_id": "current",
        "historical_source_state_id": "historical",
        "logical_page_key": "home",
        "verified_by": "confirmed_logical_identity_and_canonical_url",
    }


def test_non_h5_recovery_never_rebinds_different_state_fingerprint() -> None:
    value = graph(
        [
            state(
                "current",
                "home",
                "timer-new",
                state_payload={"url": "https://example.test/home"},
            ),
            state(
                "historical",
                "home",
                "timer-old",
                state_payload={"url": "https://example.test/home"},
            ),
            state("detail", "detail"),
        ],
        [edge("open-detail", "historical", "detail")],
    )

    result = build_recovery_routes(
        value,
        source_state_id="current",
        target_identity="detail",
    )

    assert result["status"] == "unreachable"
    assert result["routes"] == []


def test_foreign_epoch_and_untrusted_edges_never_create_tree_parent_or_route():
    value = graph(
        [state("1", "home"), state("2", "detail"), state("3", "foreign", finalization_epoch=2)],
        [
            edge("foreign", "1", "2", finalization_epoch=2),
            edge("untrusted", "1", "2", proof="insufficient"),
        ],
    )
    assert tree(value)["roots"] == ["home", "detail"]
    assert (
        build_recovery_routes(value, source_state_id="1", target_identity="detail")["routes"] == []
    )


def test_web_capture_rejected_by_evidence_admission_never_projects_business_node():
    rejected = state(
        "rejected",
        "detail",
        surface_kind="web",
        state_payload={
            "web_evidence_admission": {
                "kind": "diagnostic_observation",
                "can_project_business_node": False,
                "can_enter_formal_bundle": False,
            }
        },
    )

    result = tree(graph([state("1", "home"), rejected]))

    assert [node["page_key"] for node in result["nodes"]] == ["home"]
    assert result["unknown_observation_ids"] == ["rejected"]


def test_json_string_state_payload_uses_same_business_admission_contract():
    rejected = state(
        "rejected",
        "detail",
        surface_kind="h5",
        state_payload=(
            '{"web_evidence_admission":{"kind":"diagnostic_observation",'
            '"can_project_business_node":false,"can_enter_formal_bundle":false}}'
        ),
    )

    result = tree(graph([state("1", "home"), rejected]))

    assert [node["page_key"] for node in result["nodes"]] == ["home"]
    assert result["unknown_observation_ids"] == ["rejected"]


def test_high_risk_and_external_observations_are_not_recoverable():
    value = graph(
        [state("1", "home"), state("2", "payment"), state("3", "ad", is_external=True)],
        [edge("pay", "1", "2", risk_level="high"), edge("ad", "1", "3")],
    )
    result = build_recovery_routes(value, source_state_id="1", target_identity=None)
    assert result["routes"] == []
    assert tree(value)["unknown_observation_ids"] == ["3"]
