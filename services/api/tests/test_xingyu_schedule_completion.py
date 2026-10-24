"""星语排期收口：证据字段／角色补证／Journey／登录闸／Toast／页型硬闸。"""

from __future__ import annotations

from services.api.app.application.app_capture_evidence_fields import (
    enrich_capture_evidence_fields,
    normalize_session_auth,
    resolve_session_auth_from_facts,
)
from services.api.app.application.app_capture_role_supplement import (
    extract_requirement_roles_from_capture_rows,
    merge_evidence_capture_roles,
    normalize_requirement_role,
)
from services.api.app.application.app_exploration_flow_contract import (
    update_app_flow_contract,
)
from services.api.app.application.app_journey_execution_bridge import (
    attach_journey_plan_to_coverage,
    flow_ready_for_app_journey,
)
from services.api.app.application.app_p2_timing_capabilities import (
    APP_NETWORK_FAULT_UNSUPPORTED,
    app_network_fault_capability,
    build_toast_observation,
)
from services.api.app.application.rule_auth_gate import (
    AUTH_REQUIREMENT_AUTHENTICATED,
    AUTH_REQUIREMENT_GUEST,
    resolve_blocked_auth_requirement_from_session_auth,
)


def test_session_auth_and_obstruction_enrich_metadata() -> None:
    meta = enrich_capture_evidence_fields(
        {"package_name": "com.example"},
        surface_facts={"popup_visible": True},
        task_config={"no_test_account": True},
        requirement_role="refreshed",
    )
    assert meta["session_auth"] == "unknown"
    assert meta["is_obstructed"] is True
    assert meta["requirement_role"] == "refreshed"
    assert normalize_session_auth("logged_in") == "authenticated"
    assert (
        resolve_session_auth_from_facts(surface_facts={"logged_in": True})
        == "authenticated"
    )


def test_merge_evidence_roles_supports_location_and_attempts() -> None:
    roles = merge_evidence_capture_roles(
        primary_capture_id="cap-primary",
        before_capture_id="cap-before",
        supplemental={"location_1": "cap-loc", "attempt_2": "cap-a2"},
        requirement_role="refreshed",
        requirement_capture_id="cap-refresh",
    )
    assert roles["primary"] == "cap-primary"
    assert roles["before"] == "cap-before"
    assert roles["location_1"] == "cap-loc"
    assert roles["attempt_2"] == "cap-a2"
    assert roles["refreshed"] == "cap-refresh"
    assert normalize_requirement_role("after_action") == "primary"
    assert normalize_requirement_role("surface_reobserve") == "refreshed"
    extracted = extract_requirement_roles_from_capture_rows(
        [
            {
                "capture_id": "cap-loc",
                "metadata": {"requirement_role": "location_1"},
            },
            {
                "capture_id": "cap-before",
                "capture_role": "before_action",
                "metadata": {},
            },
        ],
        primary_capture_id="cap-primary",
    )
    assert extracted["location_1"] == "cap-loc"
    assert extracted["before"] == "cap-before"


def test_flow_contract_page_types_hard_gate() -> None:
    contract = update_app_flow_contract(
        None,
        page_state="stable",
        page_type="home",
        package_within_boundary=True,
        stable_business_page=True,
        required_page_types=("home", "content_detail", "settings"),
        covered_page_types=("home",),
        surface_facts={
            "trusted_accessibility_controls": {
                "bottom_navigation": [
                    {"label": "首页", "selected": True, "action_allowed": True}
                ]
            }
        },
    )
    assert contract["page_types_satisfied"] is False
    assert "page_types_incomplete" in contract["missing_reasons"]
    assert "content_detail" in contract["missing_page_types"]

    contract = update_app_flow_contract(
        contract,
        page_state="stable",
        page_type="content_detail",
        package_within_boundary=True,
        stable_business_page=True,
        action_observation_outcome="business_arrived",
        required_page_types=("home", "content_detail", "settings"),
    )
    contract = update_app_flow_contract(
        contract,
        page_state="stable",
        page_type="settings",
        package_within_boundary=True,
        stable_business_page=True,
        action_observation_outcome="business_arrived",
        required_page_types=("home", "content_detail", "settings"),
    )
    assert contract["page_types_satisfied"] is True
    assert "page_types_incomplete" not in contract["missing_reasons"]


def test_journey_attach_requires_home_and_popup_cleared() -> None:
    assert flow_ready_for_app_journey({"home_arrived": True, "popup_satisfied": True})
    assert not flow_ready_for_app_journey({"home_arrived": True, "popup_satisfied": False})
    meta = attach_journey_plan_to_coverage(
        {},
        plans=[
            {
                "definition_id": "journey-player",
                "execution_plan_id": "plan-1",
                "version": "1",
            }
        ],
    )
    assert meta["app_journey_plans_ready"] is True
    assert meta["journey_execution_plan_id"] == "plan-1"


def test_app_auth_gate_blocks_authenticated_for_guest() -> None:
    assert (
        resolve_blocked_auth_requirement_from_session_auth("guest")
        == AUTH_REQUIREMENT_AUTHENTICATED
    )
    assert (
        resolve_blocked_auth_requirement_from_session_auth(
            "unknown", no_test_account=True
        )
        == AUTH_REQUIREMENT_AUTHENTICATED
    )
    assert (
        resolve_blocked_auth_requirement_from_session_auth("authenticated")
        == AUTH_REQUIREMENT_GUEST
    )
    assert resolve_blocked_auth_requirement_from_session_auth("unknown") is None


def test_toast_and_network_fault_capabilities() -> None:
    toast = build_toast_observation(
        frames=[{"visible_text": "复制成功", "capture_id": "c1"}],
        markers=("成功",),
    )
    assert toast is not None
    assert toast["hit_count"] == 1
    fault = app_network_fault_capability()
    assert fault["supported"] is False
    assert fault["result"] == APP_NETWORK_FAULT_UNSUPPORTED
