"""H5 页面类型判别指南的内容与安全边界测试。"""

from __future__ import annotations

from services.api.app.agents.tools.page_detection_tools import _catalog_payload
from services.api.app.domain.page.catalog import load_default_page_type_catalog
from services.api.app.domain.page.registry import recognition_signal_references

EXPECTED_H5_PAGE_TYPES = {
    "home",
    "content_list",
    "content_detail",
    "video_list",
    "video_detail",
    "profile",
    "settings",
    "payment",
    "post_list",
    "post_publish",
    "activity",
    "line_loading",
    "startup_ad",
    "guide_page",
}


def test_h5_catalog_contains_complete_recognition_guides() -> None:
    catalog = load_default_page_type_catalog()

    assert set(catalog.codes) == EXPECTED_H5_PAGE_TYPES
    for definition in catalog.page_types:
        assert definition.purpose
        assert definition.required_positive_signals
        assert definition.supporting_signals
        assert definition.strong_negative_signals
        assert definition.confusable_with
        assert definition.missing_evidence_actions


def test_h5_home_does_not_require_a_bottom_tab_bar() -> None:
    home = load_default_page_type_catalog().require_code("home")

    assert all("底部" not in signal for signal in home.required_positive_signals)
    assert any("底部导航" in signal for signal in home.supporting_signals)


def test_h5_recognition_guides_are_sent_to_page_detection() -> None:
    catalog = load_default_page_type_catalog()

    payload = _catalog_payload(catalog)
    home = next(item for item in payload["page_types"] if item["code"] == "home")

    assert home["purpose"] == catalog.require_code("home").purpose
    assert home["required_positive_signals"]
    assert home["strong_negative_signals"]
    assert home["missing_evidence_actions"]
    assert home["recognition_signal_references"] == recognition_signal_references(
        catalog.require_code("home")
    )


def test_h5_recognition_signal_ids_are_stable_across_catalog_order() -> None:
    home = load_default_page_type_catalog().require_code("home")

    original = recognition_signal_references(home)
    reordered = recognition_signal_references(
        type(home)(
            **{
                **home.__dict__,
                "required_positive_signals": tuple(reversed(home.required_positive_signals)),
            }
        )
    )

    assert {item["signal_id"] for item in original["required_positive_signals"]} == {
        item["signal_id"] for item in reordered["required_positive_signals"]
    }


def test_h5_high_risk_pages_keep_recapture_read_only() -> None:
    catalog = load_default_page_type_catalog()
    payment_actions = "\n".join(catalog.require_code("payment").missing_evidence_actions)
    publish_actions = "\n".join(catalog.require_code("post_publish").missing_evidence_actions)
    guide_actions = "\n".join(catalog.require_code("guide_page").missing_evidence_actions)

    assert "仅观察不提交" in payment_actions
    assert "立即停止推进" in payment_actions
    assert "不输入或发布内容" in publish_actions
    assert "只记录，不点击" in guide_actions
