"""有专属编排优先；无编排才走 Agent 三合同（限步／无状态／约束输出）。"""

from __future__ import annotations

from services.api.app.application.app_bounded_dismiss_tool import (
    package_uses_bounded_dismiss,
)
from services.api.app.application.app_product_orchestration_policy import (
    product_fixed_script_enabled,
    profile_allows_product_orchestrator,
    uses_agent_generic_contract,
)
from services.api.app.application.app_ui_tree_features import (
    bottom_navigation_tab_tap_bounds,
)


def test_mama_uses_agent_contract(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_APP_EXECUTION_MODE", raising=False)
    assert product_fixed_script_enabled() is False
    assert profile_allows_product_orchestrator("com.dzu4.app") is False
    assert uses_agent_generic_contract("com.dzu4.app") is True
    assert package_uses_bounded_dismiss(package_name="com.dzu4.app") is True


def test_tangguo_uses_agent_contract(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_APP_EXECUTION_MODE", raising=False)
    assert profile_allows_product_orchestrator("com.alz7xm2.drama") is False
    assert uses_agent_generic_contract("com.alz7xm2.drama") is True
    assert package_uses_bounded_dismiss(package_name="com.alz7xm2.drama") is True


def test_unknown_package_uses_agent_contract(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_APP_EXECUTION_MODE", raising=False)
    assert profile_allows_product_orchestrator("com.brand.new.app") is False
    assert uses_agent_generic_contract("com.brand.new.app") is True
    assert package_uses_bounded_dismiss(package_name="com.brand.new.app") is True


def test_agent_bottom_nav_forces_relative_slots() -> None:
    """Agent 回落包：底栏语义相对槽；编排包也可有相对策略但不强制跳过 tap_bounds。"""

    mine = bottom_navigation_tab_tap_bounds("我的", package_name="com.alz7xm2.drama")
    assert mine is not None
    assert 0.84 <= mine["top"] < mine["bottom"] <= 0.96
    assert (mine["left"] + mine["right"]) / 2.0 >= 0.7
