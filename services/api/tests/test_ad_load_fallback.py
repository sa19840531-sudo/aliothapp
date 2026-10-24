"""设备广告加载故障未注入时，不能把空白蒙层判成兜底失败。"""

from services.api.app.application.rule_capability_hybrid import run_hybrid_capability_tools

CONFIG = {
    "batch_group": "ad-fallback",
    "deterministic_tools": [
        "network_fault_injection_tool",
        "fallback_state_assertion_tool",
    ],
}


def test_proxy_offline_is_not_an_ad_load_fault() -> None:
    results = run_hybrid_capability_tools(
        packet_payload={
            "facts": {
                "action_log": [
                    {
                        "action": "network_fault",
                        "result": "network_fault_injected",
                        "matched_request_count": 1,
                        "route_restored": True,
                        "fault_scope": "http_proxy_traffic_only",
                    }
                ]
            }
        },
        capability_config=CONFIG,
    )
    assert [item["status"] for item in results] == ["insufficient", "insufficient"]
    assert results[0]["facts"]["gap_codes"] == [
        "ad_load_fault_injection_unsupported",
        "ad_load_fault_effect_missing",
        "ad_load_fallback_close_or_skip_missing",
    ]


def test_effective_ad_load_fault_still_needs_close_or_skip() -> None:
    results = run_hybrid_capability_tools(
        packet_payload={
            "facts": {
                "action_log": [
                    {
                        "action": "network_fault",
                        "result": "network_fault_injected",
                        "matched_request_count": 1,
                        "fault_scope": "ad_load",
                        "ad_load_fault_supported": True,
                        "fault_effective": True,
                    }
                ]
            }
        },
        capability_config=CONFIG,
    )
    assert results[0]["facts"]["gap_codes"] == ["ad_load_fallback_close_or_skip_missing"]
    assert results[0]["facts"]["fault_effective"] is True


def test_closed_ad_after_effective_fault_is_complete() -> None:
    results = run_hybrid_capability_tools(
        packet_payload={
            "facts": {
                "action_log": [
                    {
                        "action": "network_fault",
                        "result": "network_fault_injected",
                        "matched_request_count": 2,
                        "fault_scope": "ad_load",
                        "ad_load_fault_supported": True,
                        "fault_effective": True,
                        "fallback_outcome": "skipped",
                    }
                ]
            }
        },
        capability_config=CONFIG,
    )
    assert results[0]["status"] == "ok"
    assert results[1]["status"] == "ok"
    assert results[0]["facts"]["gap_codes"] == []
