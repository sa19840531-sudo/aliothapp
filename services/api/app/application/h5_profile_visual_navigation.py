"""H5“我的”页受控视觉入口合同。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class H5ProfileVisualEntryContract:
    """绑定视觉目标、入口类型与预期页面类型。"""

    target: str
    entry_kind: str
    expected_page_type: str
    target_keyword: str
    minimum_confidence: float
    maximum_area_ratio: float


_PROFILE_VISUAL_ENTRY_CONTRACTS = tuple(
    H5ProfileVisualEntryContract(
        target=target,
        entry_kind="profile_activity_ocr",
        expected_page_type="activity",
        target_keyword="profile_activity_entry",
        minimum_confidence=0.88,
        maximum_area_ratio=0.20,
    )
    for target in ("签到", "签到中心", "每日签到")
) + tuple(
    H5ProfileVisualEntryContract(
        target=target,
        entry_kind="profile_settings_image",
        expected_page_type="settings",
        target_keyword="profile_settings_entry",
        minimum_confidence=0.88,
        maximum_area_ratio=0.20,
    )
    for target in ("设置", "设置按钮")
) + tuple(
    H5ProfileVisualEntryContract(
        target=target,
        entry_kind="profile_vip_payment",
        expected_page_type="payment",
        target_keyword="profile_vip_payment_entry",
        minimum_confidence=0.80,
        maximum_area_ratio=0.35,
    )
    for target in ("立即开通", "会员中心")
)


def h5_profile_visual_entry_contract(
    target: object,
    *,
    entry_kind: object | None = None,
) -> H5ProfileVisualEntryContract | None:
    """仅返回精确命中白名单且入口类型一致的视觉合同。"""

    normalized_target = "".join(str(target or "").split())
    normalized_kind = str(entry_kind or "").strip().casefold()
    matches = [
        contract
        for contract in _PROFILE_VISUAL_ENTRY_CONTRACTS
        if "".join(contract.target.split()) == normalized_target
        and (not normalized_kind or contract.entry_kind == normalized_kind)
    ]
    return matches[0] if len(matches) == 1 else None


def h5_profile_visual_entry_kind_allowed(value: object) -> bool:
    """判断入口类型是否属于个人中心视觉白名单。"""

    normalized = str(value or "").strip().casefold()
    return any(contract.entry_kind == normalized for contract in _PROFILE_VISUAL_ENTRY_CONTRACTS)
