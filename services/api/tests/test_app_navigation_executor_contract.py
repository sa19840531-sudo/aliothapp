"""App 导航执行器 after 证据合同。"""

from __future__ import annotations

from services.api.app.application.app_navigation_executor_contract import (
    bind_navigation_candidate_epoch,
    is_native_navigation_operation,
    settle_navigation_after_evidence,
    settlement_blocks_business_arrival,
)


def test_native_navigation_ops() -> None:
    assert is_native_navigation_operation("tap")
    assert is_native_navigation_operation("click")
    assert not is_native_navigation_operation("deep_link")
    assert not is_native_navigation_operation("back")


def test_bind_epoch_requires_core_fields() -> None:
    incomplete = bind_navigation_candidate_epoch(
        capture_epoch=None,
        package_name="com.demo.app",
        activity=".Main",
        orientation="portrait",
        window=None,
    )
    assert incomplete["bind_complete"] is False
    assert "epoch_missing" in incomplete["bind_gaps"]
    assert "window_missing" in incomplete["bind_gaps"]

    complete = bind_navigation_candidate_epoch(
        capture_epoch="epoch-1",
        package_name="com.demo.app",
        activity=".Main",
        orientation="portrait",
        window="main",
        source_capture_id="cap-before",
    )
    assert complete["bind_complete"] is True
    assert complete["capture_epoch"] == "epoch-1"


def test_after_settlement_requires_quartet_and_epoch() -> None:
    bad = settle_navigation_after_evidence(
        screenshot_artifact_id="shot-1",
        package_name="com.demo.app",
        activity=".Main",
        orientation="portrait",
        page_state="ready",
        surface_facts={},
        bound_epoch="epoch-1",
        after_epoch="epoch-1",
        ui_tree_present=False,
    )
    assert bad["settled"] is False
    assert "ui_tree_missing" in bad["gaps"]
    assert "window_missing" in bad["gaps"]
    assert settlement_blocks_business_arrival(bad) is True

    good = settle_navigation_after_evidence(
        screenshot_artifact_id="shot-1",
        package_name="com.demo.app",
        activity=".Main",
        orientation="portrait",
        page_state="ready",
        surface_facts={"has_ui_tree": True, "current_window": "HomeActivity"},
        bound_epoch="epoch-1",
        after_epoch="epoch-1",
    )
    assert good["settled"] is True
    assert good["proves_native_navigation_click"] is True
    assert settlement_blocks_business_arrival(good) is False


def test_epoch_mismatch_blocks() -> None:
    payload = settle_navigation_after_evidence(
        screenshot_artifact_id="shot-1",
        package_name="com.demo.app",
        activity=".Main",
        orientation="portrait",
        surface_facts={"has_ui_tree": True, "current_window": "w1"},
        bound_epoch="epoch-1",
        after_epoch="epoch-2",
    )
    assert "epoch_mismatch" in payload["gaps"]
    assert payload["settled"] is False
