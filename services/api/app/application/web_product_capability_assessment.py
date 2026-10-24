"""基于当前可见结构识别 Web 产品能力、入口状态和可见层。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WebProductCapability(StrEnum):
    NAVIGATION = "navigation"
    CONTENT = "content"
    VIDEO = "video"
    ADVERTISEMENT = "advertisement"
    TRANSACTION = "transaction"
    MIXED = "mixed"


class WebEntryStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    BLOCKED = "blocked"
    CONFIRMED_ABSENT = "confirmed_absent"


class WebVisibleLayer(StrEnum):
    BUSINESS = "business"
    AGE_GATE = "age_gate"
    POPUP = "popup"
    ADVERTISEMENT = "advertisement"
    HUMAN_VERIFICATION = "human_verification"


@dataclass(frozen=True, kw_only=True)
class WebProductCapabilityAssessment:
    capabilities: tuple[WebProductCapability, ...]
    entry_status: WebEntryStatus
    visible_layer: WebVisibleLayer
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "capabilities": [item.value for item in self.capabilities],
            "entry_status": self.entry_status.value,
            "visible_layer": self.visible_layer.value,
            "reason_codes": list(self.reason_codes),
        }


def assess_web_product_capabilities(
    *,
    readiness_status: str,
    business_visible: bool,
    navigation_element_count: int,
    repeated_card_count: int,
    video_element_count: int,
    article_body_count: int,
    advertisement_element_count: int,
    transaction_element_count: int,
    age_gate_present: bool = False,
    popup_present: bool = False,
    human_verification_present: bool = False,
) -> WebProductCapabilityAssessment:
    """先判当前层，再判入口和能力；底页不可见时不猜测产品形态。"""

    counts = (
        navigation_element_count,
        repeated_card_count,
        video_element_count,
        article_body_count,
        advertisement_element_count,
        transaction_element_count,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts):
        raise ValueError("产品结构计数必须为非负整数")
    for value in (
        business_visible,
        age_gate_present,
        popup_present,
        human_verification_present,
    ):
        if not isinstance(value, bool):
            raise ValueError("可见层事实必须为布尔值")

    if human_verification_present or readiness_status == "human_verification_blocked":
        layer = WebVisibleLayer.HUMAN_VERIFICATION
    elif age_gate_present:
        layer = WebVisibleLayer.AGE_GATE
    elif popup_present:
        layer = WebVisibleLayer.POPUP
    elif advertisement_element_count > 0 and not business_visible:
        layer = WebVisibleLayer.ADVERTISEMENT
    else:
        layer = WebVisibleLayer.BUSINESS
    if layer is not WebVisibleLayer.BUSINESS:
        return WebProductCapabilityAssessment(
            capabilities=(),
            entry_status=WebEntryStatus.BLOCKED,
            visible_layer=layer,
            reason_codes=(f"visible_layer:{layer.value}",),
        )
    if readiness_status != "stable":
        return WebProductCapabilityAssessment(
            capabilities=(),
            entry_status=WebEntryStatus.UNKNOWN,
            visible_layer=layer,
            reason_codes=("business_layer_not_stable",),
        )
    if not business_visible:
        return WebProductCapabilityAssessment(
            capabilities=(),
            entry_status=WebEntryStatus.UNKNOWN,
            visible_layer=layer,
            reason_codes=("business_capability_not_observed",),
        )

    capabilities: list[WebProductCapability] = []
    if navigation_element_count > 0:
        capabilities.append(WebProductCapability.NAVIGATION)
    if repeated_card_count > 0 or article_body_count > 0:
        capabilities.append(WebProductCapability.CONTENT)
    if video_element_count > 0:
        capabilities.append(WebProductCapability.VIDEO)
    if advertisement_element_count > 0:
        capabilities.append(WebProductCapability.ADVERTISEMENT)
    if transaction_element_count > 0:
        capabilities.append(WebProductCapability.TRANSACTION)
    if WebProductCapability.CONTENT in capabilities and WebProductCapability.VIDEO in capabilities:
        capabilities.append(WebProductCapability.MIXED)
    return WebProductCapabilityAssessment(
        capabilities=tuple(capabilities),
        entry_status=(
            WebEntryStatus.KNOWN if capabilities else WebEntryStatus.UNKNOWN
        ),
        visible_layer=layer,
        reason_codes=(
            () if capabilities else ("business_capability_not_observed",)
        ),
    )


__all__ = [
    "WebEntryStatus",
    "WebProductCapability",
    "WebProductCapabilityAssessment",
    "WebVisibleLayer",
    "assess_web_product_capabilities",
]
