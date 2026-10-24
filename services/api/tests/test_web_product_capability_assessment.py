"""Web 产品能力、入口状态与当前可见层的确定性判定。"""

import pytest

from services.api.app.application.web_product_capability_assessment import (
    WebEntryStatus,
    WebVisibleLayer,
    assess_web_product_capabilities,
)


def test_age_gate_is_current_layer_and_does_not_guess_hidden_home() -> None:
    result = assess_web_product_capabilities(
        readiness_status="stable",
        age_gate_present=True,
        business_visible=False,
        navigation_element_count=0,
        repeated_card_count=0,
        video_element_count=0,
        article_body_count=0,
        advertisement_element_count=0,
        transaction_element_count=0,
    )
    assert result.visible_layer is WebVisibleLayer.AGE_GATE
    assert result.entry_status is WebEntryStatus.BLOCKED
    assert result.capabilities == ()


def test_stable_mixed_site_reports_structural_capabilities() -> None:
    result = assess_web_product_capabilities(
        readiness_status="stable",
        age_gate_present=False,
        business_visible=True,
        navigation_element_count=4,
        repeated_card_count=8,
        video_element_count=2,
        article_body_count=1,
        advertisement_element_count=3,
        transaction_element_count=1,
    )
    assert result.visible_layer is WebVisibleLayer.BUSINESS
    assert result.entry_status is WebEntryStatus.KNOWN
    assert set(item.value for item in result.capabilities) == {
        "navigation",
        "content",
        "video",
        "advertisement",
        "transaction",
        "mixed",
    }


def test_zero_structure_is_unknown_not_absent() -> None:
    result = assess_web_product_capabilities(
        readiness_status="stable",
        business_visible=True,
        navigation_element_count=0,
        repeated_card_count=0,
        video_element_count=0,
        article_body_count=0,
        advertisement_element_count=0,
        transaction_element_count=0,
    )

    assert result.entry_status is WebEntryStatus.UNKNOWN
    assert result.capabilities == ()
    assert result.reason_codes == ("business_capability_not_observed",)
    assert "business_capability_absent" not in result.reason_codes


def test_unobservable_custom_drawn_surface_is_unknown_not_absent() -> None:
    """Canvas／自绘页面的零 DOM 计数不能充当产品能力负向证明。"""

    result = assess_web_product_capabilities(
        readiness_status="stable",
        business_visible=False,
        navigation_element_count=0,
        repeated_card_count=0,
        video_element_count=0,
        article_body_count=0,
        advertisement_element_count=0,
        transaction_element_count=0,
    )

    assert result.visible_layer is WebVisibleLayer.BUSINESS
    assert result.entry_status is WebEntryStatus.UNKNOWN
    assert result.reason_codes == ("business_capability_not_observed",)


def test_hidden_business_layer_ignores_positive_underlay_structure_counts() -> None:
    """未识别具体遮挡类型时，隐藏底页的正计数也不能签发已知能力。"""

    result = assess_web_product_capabilities(
        readiness_status="stable",
        business_visible=False,
        navigation_element_count=4,
        repeated_card_count=8,
        video_element_count=2,
        article_body_count=1,
        advertisement_element_count=0,
        transaction_element_count=1,
    )

    assert result.visible_layer is WebVisibleLayer.BUSINESS
    assert result.entry_status is WebEntryStatus.UNKNOWN
    assert result.capabilities == ()
    assert result.reason_codes == ("business_capability_not_observed",)


def test_human_verification_has_highest_blocking_priority() -> None:
    result = assess_web_product_capabilities(
        readiness_status="human_verification_blocked",
        age_gate_present=True,
        human_verification_present=True,
        business_visible=False,
        navigation_element_count=0,
        repeated_card_count=0,
        video_element_count=0,
        article_body_count=0,
        advertisement_element_count=0,
        transaction_element_count=0,
    )
    assert result.visible_layer is WebVisibleLayer.HUMAN_VERIFICATION
    assert result.entry_status is WebEntryStatus.BLOCKED


@pytest.mark.parametrize(
    ("kwargs", "expected_layer"),
    [
        ({"popup_present": True}, WebVisibleLayer.POPUP),
        ({"advertisement_element_count": 2}, WebVisibleLayer.ADVERTISEMENT),
    ],
)
def test_non_business_visible_layer_blocks_hidden_capability_guessing(
    kwargs: dict[str, object],
    expected_layer: WebVisibleLayer,
) -> None:
    arguments = {
        "readiness_status": "stable",
        "business_visible": False,
        "navigation_element_count": 8,
        "repeated_card_count": 8,
        "video_element_count": 2,
        "article_body_count": 1,
        "advertisement_element_count": 0,
        "transaction_element_count": 0,
        **kwargs,
    }

    result = assess_web_product_capabilities(**arguments)

    assert result.visible_layer is expected_layer
    assert result.entry_status is WebEntryStatus.BLOCKED
    assert result.capabilities == ()
    assert result.reason_codes == (f"visible_layer:{expected_layer.value}",)


@pytest.mark.parametrize(
    ("counts", "expected"),
    [
        ({"navigation_element_count": 3}, {"navigation"}),
        ({"repeated_card_count": 6}, {"content"}),
        ({"video_element_count": 1}, {"video"}),
        ({"transaction_element_count": 1}, {"transaction"}),
    ],
)
def test_stable_business_layer_reports_each_structural_capability(
    counts: dict[str, int],
    expected: set[str],
) -> None:
    arguments = {
        "readiness_status": "stable",
        "business_visible": True,
        "navigation_element_count": 0,
        "repeated_card_count": 0,
        "video_element_count": 0,
        "article_body_count": 0,
        "advertisement_element_count": 0,
        "transaction_element_count": 0,
        **counts,
    }

    result = assess_web_product_capabilities(**arguments)

    assert {item.value for item in result.capabilities} == expected
    assert result.entry_status is WebEntryStatus.KNOWN
