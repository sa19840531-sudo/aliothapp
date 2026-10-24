"""H5“我的”页会员支付入口的确定性结构能力合同。"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

H5_PROFILE_PAYMENT_TARGET = "个人中心会员支付入口"
H5_PROFILE_PAYMENT_TARGET_MODE = "profile_payment_navigation"
H5_PROFILE_PAYMENT_SOURCE_PAGE_TYPE = "profile"
H5_PROFILE_PAYMENT_EXPECTED_PAGE_TYPE = "payment"
H5_PROFILE_PAYMENT_CAPABILITY_KEYWORD_PREFIX = "capability:"
H5_PROFILE_PAYMENT_BANNER_IMAGE_CAPABILITY = "me_tab_payment_banner_image"
H5_PROFILE_PAYMENT_VIP_RIGHT_IMAGE_CAPABILITY = "vip_right_button_image"
H5_PROFILE_PAYMENT_NESTED_IMAGE_CAPABILITY = "payment_me_tab_nested_image"
H5_PROFILE_PAYMENT_SAME_NODE_IMAGE_CAPABILITY = "payment_me_tab_same_node_image"
H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY = "vip_section_immediate_open_text"
H5_PROFILE_VIP_CENTER_IMAGE_CAPABILITY = "profile_vip_center_image"
H5_PROFILE_VIP_PROMO_OPEN_IMAGE_CAPABILITY = "vip_promo_immediate_open_image"
H5_PROFILE_QUICK_VIP_CENTER_IMAGE_CAPABILITY = "profile_quick_vip_center_image"
H5_PROFILE_MINE_VIP_BANNER_TEXT_CAPABILITY = "mine_vip_banner_immediate_open_text"
H5_PROFILE_MINE_VIP_CENTER_IMAGE_CAPABILITY = "mine_vip_center_image"
H5_PROFILE_PAYMENT_IMAGE_CAPABILITIES = frozenset(
    {
        H5_PROFILE_PAYMENT_BANNER_IMAGE_CAPABILITY,
        H5_PROFILE_PAYMENT_VIP_RIGHT_IMAGE_CAPABILITY,
        H5_PROFILE_PAYMENT_NESTED_IMAGE_CAPABILITY,
        H5_PROFILE_PAYMENT_SAME_NODE_IMAGE_CAPABILITY,
        H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY,
        H5_PROFILE_VIP_CENTER_IMAGE_CAPABILITY,
        H5_PROFILE_VIP_PROMO_OPEN_IMAGE_CAPABILITY,
        H5_PROFILE_QUICK_VIP_CENTER_IMAGE_CAPABILITY,
        H5_PROFILE_MINE_VIP_BANNER_TEXT_CAPABILITY,
        H5_PROFILE_MINE_VIP_CENTER_IMAGE_CAPABILITY,
    }
)


def h5_profile_payment_capability_from_keywords(
    keywords: set[str],
) -> str | None:
    """从检测器签发关键词中提取唯一受信结构能力。"""
    capabilities = {
        keyword.removeprefix(H5_PROFILE_PAYMENT_CAPABILITY_KEYWORD_PREFIX)
        for keyword in keywords
        if keyword.startswith(H5_PROFILE_PAYMENT_CAPABILITY_KEYWORD_PREFIX)
        and keyword.removeprefix(H5_PROFILE_PAYMENT_CAPABILITY_KEYWORD_PREFIX)
        in H5_PROFILE_PAYMENT_IMAGE_CAPABILITIES
    }
    return next(iter(capabilities)) if len(capabilities) == 1 else None


def h5_profile_payment_snapshot_element_matches(
    element: Mapping[str, object],
    *,
    capability: str,
) -> bool:
    """按已签发能力复核 Snapshot 中唯一会员入口。"""
    hit_test = element.get("hit_test")
    if not isinstance(hit_test, Mapping):
        return False
    if not h5_profile_payment_snapshot_structure_matches(element, capability=capability):
        return False
    return bool(
        hit_test.get("center_hits_target") is True
        or (capability == H5_PROFILE_MINE_VIP_BANNER_TEXT_CAPABILITY
            and _mine_vip_banner_parent_is_center_target(element, hit_test))
    )


def h5_profile_payment_snapshot_structure_matches(
    element: Mapping[str, object], *, capability: str,
) -> bool:
    """保留结构候选供后续验签，不授予点击权限，也不伪造命中事实。"""
    class_tokens = _normalized_tokens(element.get("class_tokens"))
    parent_tokens = _normalized_tokens(element.get("parent_class_tokens"))
    ancestor_tokens = _normalized_tokens(element.get("ancestor_class_tokens"))
    if capability == H5_PROFILE_PAYMENT_VIP_TEXT_CAPABILITY:
        compact_text = "".join(str(element.get("text") or "").split())
        return bool(
            str(element.get("tag") or "").casefold() == "div"
            and "button" in class_tokens
            and "vip-section" in parent_tokens
            and "header" in ancestor_tokens
            and compact_text == "立即开通"
        )
    if capability == H5_PROFILE_MINE_VIP_BANNER_TEXT_CAPABILITY:
        compact_text = "".join(str(element.get("text") or "").split())
        structure_matches = bool(
            str(element.get("tag") or "").casefold() == "div"
            and "mine-vip-banner__btn" in class_tokens
            and {"vip-center", "mine-vip-banner"}.issubset(parent_tokens)
            and compact_text == "立即开通"
        )
        return structure_matches
    if str(element.get("tag") or "").casefold() != "img":
        return False
    if capability == H5_PROFILE_PAYMENT_BANNER_IMAGE_CAPABILITY:
        return "bannerimg" in class_tokens and "metabpaymentbanner" in ancestor_tokens
    if capability == H5_PROFILE_PAYMENT_VIP_RIGHT_IMAGE_CAPABILITY:
        return "vip-right-btn" in class_tokens and "vip-img" in ancestor_tokens
    if capability == H5_PROFILE_VIP_CENTER_IMAGE_CAPABILITY:
        return bool(
            "img" in class_tokens
            and {"left", "img-menus-wrap"}.issubset(ancestor_tokens)
            and h5_profile_vip_center_src_matches(element.get("src"))
        )
    if capability == H5_PROFILE_VIP_PROMO_OPEN_IMAGE_CAPABILITY:
        return bool(
            "vip-promo__button" in class_tokens
            and "vip-promo" in ancestor_tokens
            and "".join(str(element.get("alt") or "").split()) == "立即开通"
        )
    if capability == H5_PROFILE_QUICK_VIP_CENTER_IMAGE_CAPABILITY:
        return bool(
            "quick-card" in class_tokens
            and "quick-card-row" in ancestor_tokens
            and "".join(str(element.get("alt") or "").split()) == "会员中心"
        )
    if capability == H5_PROFILE_MINE_VIP_CENTER_IMAGE_CAPABILITY:
        return bool(
            "my-balance-wrap" in ancestor_tokens
            and h5_profile_mine_vip_center_src_matches(element.get("src"))
        )
    if capability in {
        H5_PROFILE_PAYMENT_NESTED_IMAGE_CAPABILITY,
        H5_PROFILE_PAYMENT_SAME_NODE_IMAGE_CAPABILITY,
    }:
        return {"vipboxsbgimg", "vip_box", "payment_me_tab"}.issubset(
            ancestor_tokens
        )
    return False


def h5_profile_payment_snapshot_selector(
    element: Mapping[str, object],
    *,
    capability: str,
) -> str | None:
    """返回经结构与 hit-test 双重验签后的唯一点击 selector。"""
    if not h5_profile_payment_snapshot_element_matches(
        element,
        capability=capability,
    ):
        return None
    if capability == H5_PROFILE_MINE_VIP_BANNER_TEXT_CAPABILITY:
        hit_test = element.get("hit_test")
        if (
            isinstance(hit_test, Mapping)
            and hit_test.get("center_hits_target") is not True
        ):
            return str(hit_test.get("center_target") or "").strip() or None
    return str(element.get("selector") or "").strip() or None


def h5_profile_vip_center_src_matches(value: object) -> bool:
    """只接受真实会员中心静态资源文件，拒绝相似广告文件名。"""
    basename = urlsplit(str(value or "").strip()).path.rsplit("/", 1)[-1].casefold()
    return basename.startswith("my-vip-center.")


def h5_profile_mine_vip_center_src_matches(value: object) -> bool:
    """只接受站3会员中心图标资源，拒绝同容器的钱包图标。"""
    basename = urlsplit(str(value or "").strip()).path.rsplit("/", 1)[-1].casefold()
    return basename.startswith("mine-vip-icon-")


def h5_profile_payment_entry_label(capability: str) -> str:
    """返回结构能力对应的审计入口名称。"""
    if capability in {
        H5_PROFILE_QUICK_VIP_CENTER_IMAGE_CAPABILITY,
        H5_PROFILE_MINE_VIP_CENTER_IMAGE_CAPABILITY,
    }:
        return "会员中心"
    return "立即开通"


def _mine_vip_banner_parent_is_center_target(
    element: Mapping[str, object],
    hit_test: Mapping[str, object],
) -> bool:
    """允许站3按钮叶节点将点击提升到其已命中的直属会员横幅。"""
    center_target = str(hit_test.get("center_target") or "").strip()
    center_target_tokens = _normalized_tokens(
        hit_test.get("center_target_class_tokens")
    )
    return bool(
        center_target
        and center_target == str(element.get("parent_selector") or "").strip()
        and {"vip-center", "mine-vip-banner"}.issubset(center_target_tokens)
        and hit_test.get("center_target_inactive_launch_shell") is False
        and hit_test.get("visual_occlusion_detected") is False
        and hit_test.get("pointer_interception") is False
    )


def _normalized_tokens(value: object) -> set[str]:
    return (
        {str(item).strip().casefold() for item in value if str(item).strip()}
        if isinstance(value, list)
        else set()
    )
