"""从页面判定和受信 UI Snapshot 提取 H5 登录语义。"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any

from services.api.app.application.h5_profile_login_navigation import (
    H5_FLUTTER_PROFILE_LOGIN_CAPABILITY,
    H5_FLUTTER_PROFILE_LOGIN_TARGET,
    H5_FLUTTER_PROFILE_LOGIN_TARGET_MODE,
    h5_unique_flutter_profile_login_entry,
)
from services.api.app.application.h5_structural_navigation import (
    h5_bottom_navigation_action_parameters,
    h5_match_bottom_navigation_target,
)
from services.api.app.domain.exploration.h5_auth_bootstrap import (
    H5AuthPageKind,
    H5AuthPageObservation,
)
from services.api.app.domain.exploration.h5_page_state import (
    h5_page_state_supports_popup_dismissal,
)
from services.api.app.domain.interaction.validation import (
    InteractionValidationError,
    validate_semantic_target,
)

_PROFILE_MARKERS = ("我的", "个人中心", "个人主页", "profile", "my account", "account")
_LOGIN_MARKERS = ("登录", "登陆", "log in", "login", "sign in")
_LOGOUT_MARKERS = ("退出登录", "退出登陆", "logout", "log out", "sign out")
_AUTHENTICATED_STATE_MARKERS = ("authenticated", "logged in", "已登录")
_GUEST_STATE_MARKERS = (
    "unauthenticated",
    "not authenticated",
    "not logged in",
    "logged out",
    "未登录",
    "guest",
)
_PASSWORD_MODE_MARKERS = (
    "密码登录",
    "密码登陆",
    "账号密码",
    "password login",
    "use password",
    "log in with password",
)
_CHALLENGE_MARKERS = (
    "验证码",
    "短信",
    "动态码",
    "扫码",
    "二维码",
    "otp",
    "one-time password",
    "verification code",
    "sms code",
    "mfa",
    "2fa",
    "qr code",
)
_AUTH_FAILURE_MARKERS = (
    "账号或密码错误",
    "用户名或密码错误",
    "密码错误",
    "登录失败",
    "登陆失败",
    "invalid credentials",
    "invalid password",
    "incorrect password",
    "login failed",
    "sign in failed",
)
_REGISTRATION_MARKERS = ("注册", "register", "sign up", "signup")
_REGISTRATION_SUCCESS_MARKERS = (
    "注册成功",
    "registration successful",
    "registration success",
    "signed up successfully",
)
_SUCCESS_DISMISS_MARKERS = ("关闭当前弹窗", "关闭", "确定", "我知道了", "close", "ok")
_PHONE_MARKERS = ("手机号", "手机号码", "mobile", "phone", "telephone")
_PASSWORD_MARKERS = ("密码", "password", "passwd", "pwd")
_POPUP_DISMISSAL_CAPABILITIES = frozenset(
    {
        "dialog_content_close",
        "dialog_active_ad_close",
        "dialog_text_ad_close",
        "dialog_accessible_close",
        "dialog_acknowledgement",
        "dialog_generic_close",
        "dialog_overlay",
        "home_suspend_close",
        "homepage_payment_close",
        "flutter_app_open_popup_close",
        "flutter_centered_ad_popup_close",
        "flutter_centered_ad_popup_close_above",
        "flutter_centered_scroll_popup_close_below",
        "flutter_image_popup_top_right_close",
        "flutter_account_notice_top_right_close",
        "flutter_marketing_announcement_confirm",
        "modal_local_safe_acknowledgement",
        "rendered_modal_close",
        "overlay_ad_close",
        "text_acknowledge",
        "text_later",
        "text_close",
        "text_skip",
    }
)
_OBSTRUCTION_SIGNATURE_PATTERN = re.compile(r"h5-obstruction:[0-9a-f]{64}")


class H5AuthPageAnalyzer:
    """只基于当前页持久化结果做确定性、失败关闭的认证分析。"""

    def analyze(
        self,
        *,
        detection_result: Mapping[str, Any],
        ui_snapshot: Mapping[str, Any],
    ) -> H5AuthPageObservation:
        if not isinstance(detection_result, Mapping):
            raise TypeError("detection_result 必须为映射")
        if not isinstance(ui_snapshot, Mapping):
            raise TypeError("ui_snapshot 必须为映射")

        actions = _actions(detection_result)
        elements = _elements(ui_snapshot)
        page_type = _normalize(detection_result.get("page_type"))
        page_state = _normalize(detection_result.get("page_state"))
        searchable = _searchable_text(
            actions=actions,
            elements=elements,
            page_state=page_state,
            detection_reason=_normalize(detection_result.get("reason")),
        )
        failure_marker_visible = _contains_any(searchable, _AUTH_FAILURE_MARKERS)
        phone_matches = [item for item in elements if _is_phone_field(item)]
        password_matches = [item for item in elements if _is_password_field(item)]
        success_dismiss_target = _unique_action_target(actions, _is_success_dismiss_action)
        if (
            _contains_any(searchable, _REGISTRATION_SUCCESS_MARKERS)
            and success_dismiss_target is not None
        ):
            transition_parameters = _trusted_popup_transition_parameters(
                actions=actions,
                page_state=page_state,
                target=success_dismiss_target,
            )
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.SUCCESS_NOTICE,
                reason_code="registration_success_notice_visible",
                success_dismiss_target=success_dismiss_target,
                transition_parameters=transition_parameters,
            )

        popup_transition = _trusted_popup_transition(actions=actions, page_state=page_state)
        if popup_transition is not None:
            target, transition_parameters = popup_transition
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                reason_code="pre_auth_popup_visible",
                transient_target=target,
                transition_parameters=transition_parameters,
            )

        snapshot_popup_transition = _trusted_flutter_app_open_snapshot_transition(
            elements=elements,
            page_state=page_state,
        )
        if snapshot_popup_transition is not None:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                reason_code="pre_auth_flutter_popup_snapshot_visible",
                transient_target="关闭当前弹窗",
                transition_parameters=snapshot_popup_transition,
            )

        if page_type == "line_loading":
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                reason_code="line_loading_wait",
                transition_parameters=(
                    ("target_mode", "line_loading_reobserve"),
                ),
            )

        if page_type == "startup_ad":
            skip_visible = any(
                _normalize(element.get("text")) == "跳过" and _is_clickable(element)
                for element in elements
            )
            if skip_visible:
                return H5AuthPageObservation(
                    page_kind=H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                    reason_code="startup_ad_skip_visible",
                    transient_target="跳过",
                    transition_parameters=(
                        ("target_mode", "startup_ad_dismissal"),
                        ("startup_ad_capability", "countdown_close"),
                    ),
                )
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.TRANSIENT_OBSTRUCTION,
                reason_code="startup_ad_countdown_wait",
                transition_parameters=(
                    ("target_mode", "startup_ad_countdown_reobserve"),
                ),
            )

        if page_type == "profile" and not (phone_matches and password_matches):
            if failure_marker_visible:
                return H5AuthPageObservation(
                    page_kind=H5AuthPageKind.UNKNOWN,
                    reason_code="login_failure_marker_visible",
                )
            structural_login_entry = h5_unique_flutter_profile_login_entry(elements)
            if structural_login_entry is not None:
                login_target = H5_FLUTTER_PROFILE_LOGIN_TARGET
                login_transition_parameters = (
                    ("target_mode", H5_FLUTTER_PROFILE_LOGIN_TARGET_MODE),
                    (
                        "flutter_profile_login_capability",
                        H5_FLUTTER_PROFILE_LOGIN_CAPABILITY,
                    ),
                )
            else:
                login_target = _unique_action_target(actions, _is_login_entry)
                if login_target is None:
                    login_target = _unique_element_target(elements, _is_login_entry_element)
                login_transition_parameters = ()
            authenticated_marker_visible = _contains_any(
                searchable,
                _LOGOUT_MARKERS,
            ) or _is_authenticated_state(page_state)
            guest_marker_visible = login_target is not None or _contains_any(
                page_state,
                _GUEST_STATE_MARKERS,
            )
            if authenticated_marker_visible and guest_marker_visible:
                return H5AuthPageObservation(
                    page_kind=H5AuthPageKind.UNKNOWN,
                    reason_code="authentication_markers_conflicted",
                )
            if authenticated_marker_visible:
                return H5AuthPageObservation(
                    page_kind=H5AuthPageKind.PROFILE_AUTHENTICATED,
                    reason_code="authenticated_marker_visible",
                )
            if not guest_marker_visible and re.search(
                r"\bid\s*:\s*[a-z0-9]",
                searchable,
                flags=re.IGNORECASE,
            ):
                return H5AuthPageObservation(
                    page_kind=H5AuthPageKind.PROFILE_AUTHENTICATED,
                    reason_code="profile_identity_visible",
                )
            if guest_marker_visible:
                return H5AuthPageObservation(
                    page_kind=H5AuthPageKind.PROFILE_GUEST,
                    reason_code="profile_login_entry_visible",
                    login_target=login_target,
                    transition_parameters=login_transition_parameters,
                )

        registration_entry_target = _unique_element_target(
            elements,
            _is_registration_entry_element,
        )
        submit_matches = [
            item for item in elements
            if _is_login_submit(item) and not _is_registration_entry_element(item)
        ]
        login_submit_matches = [item for item in submit_matches if _is_login_only_submit(item)]
        registration_submit_matches = [
            item for item in submit_matches if _is_registration_only_submit(item)
        ]

        if len(phone_matches) > 1:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.UNKNOWN,
                reason_code="phone_field_ambiguous",
            )
        if len(password_matches) > 1:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.UNKNOWN,
                reason_code="password_field_ambiguous",
            )
        if len(login_submit_matches) > 1 or len(registration_submit_matches) > 1:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.UNKNOWN,
                reason_code="login_submit_ambiguous",
            )
        login_submit_target = (
            _element_semantic_target(login_submit_matches[0])
            if login_submit_matches
            else None
        )
        registration_submit_target = (
            _element_semantic_target(registration_submit_matches[0])
            if registration_submit_matches
            else None
        )
        submit_target = login_submit_target or registration_submit_target
        password_form_complete = (
            len(phone_matches) == len(password_matches) == 1
            and submit_target is not None
        )
        password_mode_target = _unique_action_target(actions, _is_password_mode_action)
        if password_mode_target is None:
            password_mode_target = _unique_element_target(elements, _is_password_mode_element)
        profile_target = _unique_action_target(actions, _is_profile_action)
        login_surface = (
            page_type in {"login", "signin", "sign_in"}
            or bool(phone_matches or password_matches or password_mode_target)
            or (
                profile_target is None
                and _contains_any(page_state, ("登录", "登陆", "login", "sign in"))
            )
        )
        if login_surface and failure_marker_visible:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.UNKNOWN,
                reason_code="login_failure_marker_visible",
            )
        if password_mode_target is not None and not password_form_complete:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.LOGIN_SWITCHABLE,
                reason_code="password_mode_available",
                password_mode_target=password_mode_target,
            )

        if login_surface and _has_required_challenge(
            elements=elements,
            page_state=page_state,
        ):
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.LOGIN_CHALLENGE,
                reason_code="unsupported_login_challenge",
            )

        if password_form_complete:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.LOGIN_PASSWORD,
                reason_code="password_form_ready",
                phone_target=_element_semantic_target(phone_matches[0]),
                password_target=_element_semantic_target(password_matches[0]),
                submit_target=submit_target,
                registration_submit_target=registration_submit_target,
                registration_entry_target=registration_entry_target,
            )

        if password_mode_target is not None:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.LOGIN_SWITCHABLE,
                reason_code="password_mode_available",
                password_mode_target=password_mode_target,
            )

        if login_surface and _contains_any(searchable, _CHALLENGE_MARKERS):
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.LOGIN_CHALLENGE,
                reason_code="unsupported_login_challenge",
            )

        if profile_target is not None:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.UNKNOWN,
                reason_code="profile_entry_available",
                profile_target=profile_target,
                transition_parameters=_trusted_profile_navigation_parameters(
                    actions=actions,
                    target=profile_target,
                ),
            )

        if login_surface and registration_entry_target is not None:
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.UNKNOWN,
                reason_code="registration_entry_available",
                registration_entry_target=registration_entry_target,
            )

        if page_type in {"login", "signin", "sign_in"} and (
            phone_matches or password_matches or submit_matches
        ):
            return H5AuthPageObservation(
                page_kind=H5AuthPageKind.UNKNOWN,
                reason_code="password_form_incomplete",
            )
        return H5AuthPageObservation(
            page_kind=H5AuthPageKind.UNKNOWN,
            reason_code="auth_state_unknown",
        )


def _actions(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = value.get("candidate_actions")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _elements(value: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = value.get("elements")
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, Mapping))


def _action_text(action: Mapping[str, Any]) -> str:
    keywords = action.get("target_keywords")
    values: list[object] = [action.get("target"), action.get("purpose")]
    if isinstance(keywords, list):
        values.extend(keywords)
    return _normalize(" ".join(str(value or "") for value in values))


def _element_text(element: Mapping[str, Any]) -> str:
    return _normalize(
        " ".join(
            str(element.get(name) or "")
            for name in ("text", "aria_label", "placeholder", "name", "type", "role", "tag")
        )
    )


def _searchable_text(
    *,
    actions: tuple[Mapping[str, Any], ...],
    elements: tuple[Mapping[str, Any], ...],
    page_state: str,
    detection_reason: str,
) -> str:
    return " ".join(
        (
            page_state,
            detection_reason,
            *(_action_text(item) for item in actions),
            *(_element_text(item) for item in elements),
        )
    )


def _is_semantic_target(value: str) -> bool:
    """平台定位器不是语义目标，交给 Navigator 会在建 Intent 时直接抛错。

    分析器契约是失败关闭：认不出可用目标就返回 None，由上层走既有兜底，
    不能把这类值透传出去掀翻整个任务。复用领域校验避免两边判据漂移。
    """
    try:
        validate_semantic_target(value, "target")
    except InteractionValidationError:
        return False
    return True


def _unique_action_target(
    actions: tuple[Mapping[str, Any], ...],
    predicate: Callable[[Mapping[str, Any]], bool],
) -> str | None:
    targets = {
        str(action.get("target") or "").strip()
        for action in actions
        if predicate(action) and _is_semantic_target(str(action.get("target") or "").strip())
    }
    return next(iter(targets)) if len(targets) == 1 else None


def _unique_element_target(
    elements: tuple[Mapping[str, Any], ...],
    predicate: Callable[[Mapping[str, Any]], bool],
) -> str | None:
    targets = {
        _element_semantic_target(element)
        for element in elements
        if predicate(element) and _is_semantic_target(_element_semantic_target(element))
    }
    return next(iter(targets)) if len(targets) == 1 else None


def _is_profile_action(action: Mapping[str, Any]) -> bool:
    expected = _normalize(action.get("expected_next_page_type"))
    return expected == "profile" or _contains_any(_action_text(action), _PROFILE_MARKERS)


def _is_login_entry(action: Mapping[str, Any]) -> bool:
    text = _action_text(action)
    return _contains_any(text, _LOGIN_MARKERS) and not _contains_any(
        text,
        (*_LOGOUT_MARKERS, *_PASSWORD_MODE_MARKERS, *_CHALLENGE_MARKERS),
    )


def _is_login_entry_element(element: Mapping[str, Any]) -> bool:
    text = _element_text(element)
    return (
        _is_clickable(element)
        and _contains_any(text, _LOGIN_MARKERS)
        and not _contains_any(
            text,
            (*_LOGOUT_MARKERS, *_PASSWORD_MODE_MARKERS, *_CHALLENGE_MARKERS),
        )
    )


def _is_password_mode_action(action: Mapping[str, Any]) -> bool:
    return _contains_any(_action_text(action), _PASSWORD_MODE_MARKERS)


def _is_password_mode_element(element: Mapping[str, Any]) -> bool:
    return _is_clickable(element) and _contains_any(_element_text(element), _PASSWORD_MODE_MARKERS)


def _is_phone_field(element: Mapping[str, Any]) -> bool:
    if not _is_input(element):
        return False
    if _normalize(element.get("type")) == "tel":
        return True
    return _contains_any(_element_text(element), _PHONE_MARKERS)


def _is_password_field(element: Mapping[str, Any]) -> bool:
    if not _is_input(element):
        return False
    if _normalize(element.get("type")) == "password":
        return True
    return _contains_any(_element_text(element), _PASSWORD_MARKERS)


def _is_login_submit(element: Mapping[str, Any]) -> bool:
    text = _element_text(element)
    return (
        _is_clickable(element)
        and not _is_input(element)
        and (
            _is_login_only_submit(element)
            or _is_registration_only_submit(element)
        )
        and not _contains_any(
            text,
            (*_LOGOUT_MARKERS, *_PASSWORD_MODE_MARKERS, *_CHALLENGE_MARKERS),
        )
    )


def _is_success_dismiss_action(action: Mapping[str, Any]) -> bool:
    text = _action_text(action)
    return _contains_any(text, _SUCCESS_DISMISS_MARKERS) and not _contains_any(
        text,
        (*_LOGIN_MARKERS, *_CHALLENGE_MARKERS),
    )


def _is_login_only_submit(element: Mapping[str, Any]) -> bool:
    text = _element_text(element)
    return _contains_any(text, _LOGIN_MARKERS) and not _contains_any(
        text,
        _REGISTRATION_MARKERS,
    )


def _is_registration_only_submit(element: Mapping[str, Any]) -> bool:
    text = _element_text(element)
    return _contains_any(text, _REGISTRATION_MARKERS) and not _contains_any(
        text,
        _LOGIN_MARKERS,
    )


def _is_registration_entry_element(element: Mapping[str, Any]) -> bool:
    return bool(
        _is_clickable(element)
        and element.get("external") is not True
        and _is_registration_only_submit(element)
        and (
            _normalize(element.get("tag")) == "a"
            or _normalize(element.get("role")) == "tab"
        )
    )


def _trusted_popup_transition(
    *,
    actions: tuple[Mapping[str, Any], ...],
    page_state: str,
) -> tuple[str, tuple[tuple[str, str], ...]] | None:
    if not h5_page_state_supports_popup_dismissal(page_state):
        return None
    matches: list[tuple[str, tuple[tuple[str, str], ...]]] = []
    for action in actions:
        target = str(action.get("target") or "").strip()
        parameters = _trusted_popup_transition_parameters(
            actions=(action,),
            page_state=page_state,
            target=target,
        )
        if parameters:
            matches.append((target, parameters))
    return matches[0] if len(matches) == 1 else None


def _trusted_flutter_app_open_snapshot_transition(
    *,
    elements: tuple[Mapping[str, Any], ...],
    page_state: str,
) -> tuple[tuple[str, str], ...] | None:
    """DOM 语义树瞬时为空时，用密封 Snapshot 补签唯一 Flutter 广告关闭结构。"""
    if not h5_page_state_supports_popup_dismissal(page_state):
        return None
    semantic_elements = tuple(
        item
        for item in elements
        if str(item.get("tag") or "").casefold() == "flt-semantics"
    )
    labels = tuple(
        item
        for item in semantic_elements
        if item.get("interactive") is True
        and str(item.get("role") or "").casefold() == "text"
        and _normalize(item.get("text")).casefold().endswith("app内打开".casefold())
        and _snapshot_center_hits_target(item)
    )
    if len(labels) != 1:
        return None
    empty_controls = tuple(
        item
        for item in semantic_elements
        if item.get("interactive") is True
        and not _normalize(item.get("text"))
        and not str(item.get("role") or "").strip()
        and _snapshot_center_hits_target(item)
    )
    bodies = tuple(
        item
        for item in empty_controls
        if (rect := _snapshot_rect(item)) is not None
        and 240 <= rect[2] <= 360
        and 240 <= rect[3] <= 500
    )
    closes = tuple(
        item
        for item in empty_controls
        if (rect := _snapshot_rect(item)) is not None
        and 20 <= rect[2] <= 64
        and 20 <= rect[3] <= 64
        and abs(rect[2] - rect[3]) <= 8
    )
    matches: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    for body in bodies:
        body_rect = _snapshot_rect(body)
        body_parent = str(body.get("parent_semantics_selector") or "").strip()
        if body_rect is None or not body_parent:
            continue
        for close in closes:
            close_rect = _snapshot_rect(close)
            if (
                close_rect is None
                or str(close.get("parent_semantics_selector") or "").strip()
                != body_parent
            ):
                continue
            centered = abs(
                (body_rect[0] + body_rect[2] / 2)
                - (close_rect[0] + close_rect[2] / 2)
            ) <= 4
            gap = close_rect[1] - (body_rect[1] + body_rect[3])
            if (
                centered
                and 0 <= gap <= 80
                and _snapshot_z_index(close) > _snapshot_z_index(body)
            ):
                matches.append((body, close))
    if len(matches) != 1:
        return None
    body, close = matches[0]
    signature_payload = {
        "capability": "flutter_app_open_popup_close",
        "label": _normalize(labels[0].get("text")),
        "body_parent": str(body.get("parent_semantics_selector") or ""),
        "body_rect": _snapshot_rect(body),
        "close_parent": str(close.get("parent_semantics_selector") or ""),
        "close_rect": _snapshot_rect(close),
    }
    digest = hashlib.sha256(
        json.dumps(
            signature_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return (
        ("target_mode", "popup_dismissal"),
        ("popup_dismissal_capability", "flutter_app_open_popup_close"),
        ("obstruction_signature", f"h5-obstruction:{digest}"),
    )


def _snapshot_center_hits_target(item: Mapping[str, Any]) -> bool:
    hit_test = item.get("hit_test")
    return bool(
        isinstance(hit_test, Mapping)
        and hit_test.get("center_hits_target") is True
    )


def _snapshot_rect(
    item: Mapping[str, Any],
) -> tuple[float, float, float, float] | None:
    raw = item.get("rect")
    if not isinstance(raw, Mapping):
        return None
    values = tuple(raw.get(key) for key in ("x", "y", "width", "height"))
    if any(
        isinstance(value, bool) or not isinstance(value, int | float)
        for value in values
    ):
        return None
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def _snapshot_z_index(item: Mapping[str, Any]) -> int:
    style = item.get("style")
    if not isinstance(style, Mapping):
        return 0
    try:
        return int(str(style.get("z_index") or "0"))
    except ValueError:
        return 0


def _trusted_popup_transition_parameters(
    *,
    actions: tuple[Mapping[str, Any], ...],
    page_state: str,
    target: str,
) -> tuple[tuple[str, str], ...]:
    if not h5_page_state_supports_popup_dismissal(page_state) or target != "关闭当前弹窗":
        return ()
    matches: list[tuple[tuple[str, str], ...]] = []
    for action in actions:
        capability = str(action.get("obstruction_capability") or "").strip()
        signature = str(action.get("obstruction_signature") or "").strip()
        keywords = {
            _normalize(item)
            for item in action.get("target_keywords", [])
            if str(item).strip()
        }
        if (
            _normalize(action.get("action")) != "click"
            or str(action.get("target") or "").strip() != target
            or _normalize(action.get("risk_level")) != "low"
            or action.get("navigation_depth") != 0
            or capability not in _POPUP_DISMISSAL_CAPABILITIES
            or _OBSTRUCTION_SIGNATURE_PATTERN.fullmatch(signature) is None
            or not {"popup_dismissal", "close_control", capability}.issubset(keywords)
        ):
            continue
        matches.append(
            (
                ("target_mode", "popup_dismissal"),
                ("popup_dismissal_capability", capability),
                ("obstruction_signature", signature),
            )
        )
    return matches[0] if len(matches) == 1 else ()


def _trusted_profile_navigation_parameters(
    *,
    actions: tuple[Mapping[str, Any], ...],
    target: str,
) -> tuple[tuple[str, str | int], ...]:
    """只投影检测器已验签的结构化个人中心底栏动作。"""
    matched_target = h5_match_bottom_navigation_target(target)
    if matched_target is None:
        return ()
    family, navigation_index = matched_target
    matches: list[tuple[tuple[str, str | int], ...]] = []
    for action in actions:
        raw_keywords = [
            str(item).strip()
            for item in action.get("target_keywords", [])
            if str(item).strip()
        ]
        normalized_keywords = {_normalize(item) for item in raw_keywords}
        navigation_label = next(
            (
                item
                for item in raw_keywords
                if _normalize(item) not in {"bottom_navigation", "profile"}
            ),
            None,
        )
        confidence = action.get("navigation_knowledge_confidence")
        if (
            str(action.get("target") or "").strip() != target
            or _normalize(action.get("action")) != "click"
            or _normalize(action.get("risk_level")) != "low"
            or action.get("navigation_depth") != 1
            or _normalize(action.get("expected_next_page_type")) != "profile"
            or action.get("navigation_knowledge_source") not in {"static", "exact", "vector"}
            or isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= float(confidence) <= 1
            or "bottom_navigation" not in normalized_keywords
            or "profile" not in normalized_keywords
            or navigation_label is None
        ):
            continue
        try:
            parameters = h5_bottom_navigation_action_parameters(
                family=family,
                index=navigation_index,
                label=navigation_label,
                interaction_pattern_signature=action.get("interaction_pattern_signature"),
            )
        except ValueError:
            continue
        matches.append(tuple(parameters.items()))
    return matches[0] if len(matches) == 1 else ()


def _has_required_challenge(
    *,
    elements: tuple[Mapping[str, Any], ...],
    page_state: str,
) -> bool:
    if _contains_any(page_state, _CHALLENGE_MARKERS) and _contains_any(
        page_state,
        ("required", "必须", "需要", "校验", "挑战"),
    ):
        return True
    for element in elements:
        text = _element_text(element)
        if not _contains_any(text, _CHALLENGE_MARKERS):
            continue
        tag = _normalize(element.get("tag"))
        if _is_input(element) or tag in {"img", "canvas", "svg"}:
            return True
        if _is_clickable(element) and _contains_any(
            text,
            (
                "发送验证码",
                "获取验证码",
                "发送短信",
                "扫码",
                "滑块",
                "send code",
                "get code",
                "qr code",
                "captcha",
                "mfa",
                "2fa",
            ),
        ):
            return True
    return False


def _is_input(element: Mapping[str, Any]) -> bool:
    return (
        _normalize(element.get("tag")) in {"input", "textarea"}
        and element.get("disabled") is not True
        and element.get("read_only") is not True
    )


def _is_clickable(element: Mapping[str, Any]) -> bool:
    if element.get("disabled") is True:
        return False
    return element.get("interactive") is True or _normalize(element.get("tag")) in {
        "a",
        "button",
        "input",
    }


def _element_semantic_target(element: Mapping[str, Any]) -> str:
    for name in ("aria_label", "placeholder", "text", "name", "type"):
        value = str(element.get(name) or "").strip()
        if value:
            return value
    return ""


def _contains_any(value: str, markers: tuple[str, ...]) -> bool:
    normalized_markers = (_normalize(marker) for marker in markers)
    return any(marker and marker in value for marker in normalized_markers)


def _is_authenticated_state(page_state: str) -> bool:
    if _contains_any(page_state, _GUEST_STATE_MARKERS):
        return False
    return _contains_any(page_state, _AUTHENTICATED_STATE_MARKERS)


def _normalize(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()


__all__ = ["H5AuthPageAnalyzer"]
