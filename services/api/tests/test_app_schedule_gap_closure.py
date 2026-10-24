"""星语表上 App 缺口的落地：页型、关层核对、旅程、登录、断网。"""

from __future__ import annotations

from services.api.app.application.app_capture_evidence_fields import (
    enrich_capture_evidence_fields,
)
from services.api.app.application.app_capture_role_supplement import (
    normalize_requirement_role,
)
from services.api.app.application.app_exploration_flow_contract import (
    update_app_flow_contract,
)
from services.api.app.application.app_login_detect_only_contract import (
    login_action_allowed,
)
from services.api.app.application.app_network_proxy import get_app_network_proxy_hub
from services.api.app.application.app_schedule_gap_closure import (
    advance_journey_curriculum,
    credentials_configured,
    ensure_journey_curriculum,
    merge_required_arrival_page_types,
    popup_close_is_confirmed,
)


def test_arrival_page_types_include_detail_player_payment_publish_settings() -> None:
    required = merge_required_arrival_page_types(("home",))
    assert required[:1] == ("home",)
    for code in ("content_detail", "video_detail", "payment", "post_publish", "settings"):
        assert code in required


def test_same_overlay_does_not_count_as_closed() -> None:
    assert (
        popup_close_is_confirmed(
            popup_was_visible=True,
            before_overlay="ad:1",
            after_overlay="ad:1",
            before_image=None,
            after_image=None,
            still_popup=True,
        )
        is False
    )
    opened = update_app_flow_contract(
        None,
        page_state="popup",
        overlay_signature="ad:1",
        surface_facts={"popup_visible": True},
    )
    stuck = update_app_flow_contract(
        opened,
        page_state="popup",
        overlay_signature="ad:1",
        surface_facts={"popup_visible": True},
        action_observation_outcome="obstruction_removed",
    )
    assert stuck["popup_close_evidenced"] is False
    assert stuck["popup_close_unconfirmed"] is True

    cleared = update_app_flow_contract(
        opened,
        page_state="stable",
        overlay_signature=None,
        stable_business_page=True,
        package_within_boundary=True,
        action_observation_outcome="obstruction_removed",
    )
    assert cleared["popup_close_evidenced"] is True


def test_journey_curriculum_advances_matching_step() -> None:
    coverage = ensure_journey_curriculum(
        {},
        curriculum=[
            {
                "definition_id": "payment-checkout-chain",
                "step_id": "open-pay",
                "semantic_target": "立即充值",
                "status": "pending",
                "capture_id": "",
            }
        ],
    )
    assert coverage["app_journey_next_semantic_target"] == "立即充值"
    moved = advance_journey_curriculum(
        coverage,
        semantic_target="点击立即充值",
        capture_id="cap-34",
    )
    assert moved["app_journey_curriculum"][0]["status"] == "done"
    assert moved["app_journey_curriculum"][0]["capture_id"] == "cap-34"
    assert moved["app_journey_curriculum_complete"] is True


def test_f06_partial_word_empty_capture_and_out_of_order_cannot_mark_done() -> None:
    coverage = ensure_journey_curriculum(
        {},
        curriculum=[
            {
                "definition_id": "membership",
                "step_id": "open-entry",
                "semantic_target": "开通会员入口",
                "status": "pending",
                "capture_id": "",
            },
            {
                "definition_id": "membership",
                "step_id": "pick-tier",
                "semantic_target": "会员档位",
                "status": "pending",
                "capture_id": "",
            },
        ],
    )
    # 短词不得双向子串命中长目标
    partial = advance_journey_curriculum(
        coverage,
        semantic_target="会员",
        capture_id="cap-1",
    )
    assert partial["app_journey_curriculum"][0]["status"] == "pending"
    # 空 Capture 不得 done
    empty = advance_journey_curriculum(
        coverage,
        semantic_target="开通会员入口",
        capture_id="",
    )
    assert empty["app_journey_curriculum"][0]["status"] == "pending"
    # 乱序：后项命中时前项仍 pending，后项不得抢先 done
    skip = advance_journey_curriculum(
        coverage,
        semantic_target="选择会员档位",
        capture_id="cap-2",
    )
    assert skip["app_journey_curriculum"][0]["status"] == "pending"
    assert skip["app_journey_curriculum"][1]["status"] == "pending"


def test_login_fill_requires_configured_account() -> None:
    assert login_action_allowed("type_password") is False
    assert credentials_configured({"test_account": {"username": "a"}}) is False
    config = {"test_account": {"username": "reader", "password": "secret"}}
    assert login_action_allowed("type_password", task_config=config) is True


def test_network_fault_stays_unsupported_until_proxy_is_listening() -> None:
    status = get_app_network_proxy_hub().apply_network_fault("offline")
    assert status["supported"] is False
    assert status["result"] == "network_fault_unsupported_on_app"


def test_capture_fields_include_role_and_page_state() -> None:
    meta = enrich_capture_evidence_fields(
        {"package_name": "tv.example"},
        surface_facts={"popup_visible": True, "page_url": "app://pay"},
        requirement_role="before",
    )
    assert meta["capture_role"] == "before"
    assert meta["evidence_role"] == "overlay_occurrence"
    assert meta["page_url"] == "app://pay"
    assert normalize_requirement_role("popup_before_close") == "before"
    assert normalize_requirement_role("ad_outbound_source") == "source"
