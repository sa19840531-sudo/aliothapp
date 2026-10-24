"""H5“我的”页签到活动入口的确定性结构能力合同。"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

H5_PROFILE_ACTIVITY_TARGET = "个人中心签到入口"
H5_PROFILE_CHECKIN_TARGET = "个人中心签到按钮"
H5_PROFILE_DAILY_SIGNIN_TARGET = "个人中心每日签到图片"
H5_PROFILE_SIGNIN_IMAGE_TARGET = "个人中心签到图片"
H5_PROFILE_ACTIVITY_TARGET_MODE = "profile_activity_navigation"
H5_PROFILE_ACTIVITY_CAPABILITY = "task_sign_image"
H5_PROFILE_CHECKIN_CAPABILITY = "checkin_entry"
H5_PROFILE_DAILY_SIGNIN_CAPABILITY = "daily_signin_image"
H5_PROFILE_SIGNIN_IMAGE_CAPABILITY = "profile_signin_image"
H5_PROFILE_ACTIVITY_SOURCE_PAGE_TYPE = "profile"
H5_PROFILE_ACTIVITY_EXPECTED_PAGE_TYPE = "activity"


_CAPABILITY_BY_TARGET = {
    H5_PROFILE_ACTIVITY_TARGET: H5_PROFILE_ACTIVITY_CAPABILITY,
    H5_PROFILE_CHECKIN_TARGET: H5_PROFILE_CHECKIN_CAPABILITY,
    H5_PROFILE_DAILY_SIGNIN_TARGET: H5_PROFILE_DAILY_SIGNIN_CAPABILITY,
    H5_PROFILE_SIGNIN_IMAGE_TARGET: H5_PROFILE_SIGNIN_IMAGE_CAPABILITY,
}


def h5_profile_activity_capability_for_target(target: str) -> str | None:
    """返回平台维护的签到入口能力，拒绝模型自造定位器。"""
    return _CAPABILITY_BY_TARGET.get(target)


def h5_profile_activity_snapshot_element_matches(
    element: Mapping[str, object],
    *,
    capability: str,
) -> bool:
    """只接受已签发结构且当前可命中的签到入口。"""
    raw_tokens = element.get("class_tokens")
    tokens = (
        {str(item).strip().casefold() for item in raw_tokens if str(item).strip()}
        if isinstance(raw_tokens, list)
        else set()
    )
    raw_ancestor_tokens = element.get("ancestor_class_tokens")
    ancestor_tokens = (
        {
            str(item).strip().casefold()
            for item in raw_ancestor_tokens
            if str(item).strip()
        }
        if isinstance(raw_ancestor_tokens, list)
        else set()
    )
    raw_parent_tokens = element.get("parent_class_tokens")
    parent_tokens = (
        {
            str(item).strip().casefold()
            for item in raw_parent_tokens
            if str(item).strip()
        }
        if isinstance(raw_parent_tokens, list)
        else set()
    )
    hit_test = element.get("hit_test")
    tag = str(element.get("tag") or "").casefold()
    if capability == H5_PROFILE_ACTIVITY_CAPABILITY:
        structure_matches = tag == "img" and "task_sign" in tokens
    elif capability == H5_PROFILE_CHECKIN_CAPABILITY:
        structure_matches = (tag == "div" and "checkin-entry" in tokens) or (
            tag == "img"
            and "checkin-entry-icon" in tokens
            and "checkin-entry" in ancestor_tokens
        )
    elif capability == H5_PROFILE_DAILY_SIGNIN_CAPABILITY:
        structure_matches = bool(
            tag == "img"
            and "img" in tokens
            and {"sigin-box", "right", "img-menus-wrap"}.issubset(ancestor_tokens)
            and h5_profile_daily_signin_src_matches(element.get("src"))
        )
    elif capability == H5_PROFILE_SIGNIN_IMAGE_CAPABILITY:
        structure_matches = bool(
            tag == "img"
            and (
                (
                    "sign-in-entry" in tokens
                    and "".join(str(element.get("alt") or "").split()) == "签到"
                )
                or (
                    "d-img" in tokens
                    and "sign-in" in parent_tokens
                    and h5_profile_signin_src_matches(element.get("src"))
                )
            )
        )
    else:
        structure_matches = False
    return bool(
        structure_matches
        and isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
    )


def h5_profile_daily_signin_src_matches(value: object) -> bool:
    """兼容实测站点的 sigin 拼写，但严格约束每日签到静态资源。"""
    basename = urlsplit(str(value or "").strip()).path.rsplit("/", 1)[-1].casefold()
    return basename.startswith("m-signin-bg.")


def h5_profile_signin_src_matches(value: object) -> bool:
    """只接受站3个人中心签到图资源，拒绝相似广告图片。"""
    basename = urlsplit(str(value or "").strip()).path.rsplit("/", 1)[-1].casefold()
    return basename.startswith("singi-")
