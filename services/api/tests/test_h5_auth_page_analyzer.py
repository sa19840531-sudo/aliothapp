"""H5 登录页面分析器的确定性状态与无秘密输出回归。"""

from __future__ import annotations

import json

from services.api.app.application.h5_auth_page_analyzer import H5AuthPageAnalyzer
from services.api.app.domain.exploration import H5AuthPageKind


def _detection(
    *,
    page_type: str | None,
    page_state: str = "stable",
    actions: tuple[dict[str, object], ...] = (),
) -> dict[str, object]:
    return {
        "schema_version": "page-detection.v2",
        "page_capture_id": "capture-auth",
        "page_url": "https://h5.example.com/current",
        "detection_status": "detected",
        "page_type": page_type,
        "page_state": page_state,
        "confidence": 0.95,
        "reason": "测试页面",
        "candidate_actions": list(actions),
    }


def _action(target: str, *, purpose: str = "页面导航") -> dict[str, object]:
    return {
        "action": "click",
        "target": target,
        "target_keywords": [target],
        "risk_level": "low",
        "purpose": purpose,
        "expected_next_page_type": None,
        "navigation_depth": 1,
    }


def _element(
    selector: str,
    *,
    tag: str,
    text: str = "",
    element_type: str | None = None,
    name: str | None = None,
    placeholder: str | None = None,
    aria_label: str | None = None,
) -> dict[str, object]:
    return {
        "selector": selector,
        "tag": tag,
        "role": "button" if tag == "button" else None,
        "text": text,
        "aria_label": aria_label,
        "type": element_type,
        "name": name,
        "placeholder": placeholder,
        "interactive": True,
        "disabled": False,
        "read_only": False,
    }


def _trusted_popup_action() -> dict[str, object]:
    return {
        "action": "click",
        "target": "关闭当前弹窗",
        "target_keywords": [
            "popup_dismissal",
            "close_control",
            "flutter_centered_ad_popup_close",
        ],
        "risk_level": "low",
        "purpose": "关闭当前遮挡弹窗并重新采集",
        "expected_next_page_type": None,
        "navigation_depth": 0,
        "obstruction_capability": "flutter_centered_ad_popup_close",
        "obstruction_signature": "h5-obstruction:" + "a" * 64,
    }


def _snapshot(*elements: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": "ui-snapshot.v2",
        "elements": list(elements),
    }


def test_home_page_exposes_profile_entry_without_claiming_login_state() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="home",
            actions=(_action("我的", purpose="进入个人中心"),),
        ),
        ui_snapshot=_snapshot(_element("#profile", tag="button", text="我的")),
    )

    assert observation.page_kind is H5AuthPageKind.UNKNOWN
    assert observation.profile_target == "我的"
    assert observation.reason_code == "profile_entry_available"


def test_home_page_projects_signed_flutter_profile_navigation_parameters() -> None:
    action = _action(
        "internal:flutter_bottom_navigation:nth-of-type(6)",
        purpose="进入个人中心",
    )
    action.update(
        target_keywords=["bottom_navigation", "我的", "profile"],
        expected_next_page_type="profile",
        navigation_knowledge_source="static",
        navigation_knowledge_confidence=1.0,
    )

    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="home", actions=(action,)),
        ui_snapshot=_snapshot(),
    )

    assert observation.profile_target == (
        "internal:flutter_bottom_navigation:nth-of-type(6)"
    )
    assert dict(observation.transition_parameters) == {
        "target_mode": "bottom_navigation",
        "navigation_index": 6,
        "navigation_label": "我的",
        "navigation_family": "flutter_semantics",
    }


def test_home_popup_projects_trusted_dismissal_before_profile_navigation() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="home",
            page_state="popup",
            actions=(
                _action("我的", purpose="进入个人中心"),
                _trusted_popup_action(),
            ),
        ),
        ui_snapshot=_snapshot(_element("#profile", tag="button", text="我的")),
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert observation.transient_target == "关闭当前弹窗"
    assert dict(observation.transition_parameters) == {
        "target_mode": "popup_dismissal",
        "popup_dismissal_capability": "flutter_centered_ad_popup_close",
        "obstruction_signature": "h5-obstruction:" + "a" * 64,
    }


def test_compound_popup_state_projects_the_same_trusted_dismissal() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="home",
            page_state="popup_or_overlay",
            actions=(_trusted_popup_action(),),
        ),
        ui_snapshot=_snapshot(),
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert observation.transient_target == "关闭当前弹窗"
    assert dict(observation.transition_parameters)["popup_dismissal_capability"] == (
        "flutter_centered_ad_popup_close"
    )


def test_home_popup_accepts_signed_flutter_marketing_announcement() -> None:
    action = _trusted_popup_action()
    action.update(
        obstruction_capability="flutter_marketing_announcement_confirm",
        target_keywords=[
            "popup_dismissal",
            "close_control",
            "flutter_marketing_announcement_confirm",
        ],
    )
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="home",
            page_state="popup",
            actions=(action,),
        ),
        ui_snapshot=_snapshot(_element("#background", tag="div", text="首页")),
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert observation.transient_target == "关闭当前弹窗"
    assert dict(observation.transition_parameters)["popup_dismissal_capability"] == (
        "flutter_marketing_announcement_confirm"
    )


def test_payment_popup_accepts_signed_modal_local_safe_acknowledgement() -> None:
    action = _trusted_popup_action()
    action.update(
        obstruction_capability="modal_local_safe_acknowledgement",
        target_keywords=[
            "popup_dismissal",
            "close_control",
            "modal_local_safe_acknowledgement",
        ],
    )
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="payment",
            page_state="popup",
            actions=(action,),
        ),
        ui_snapshot=_snapshot(_element("#modal-ack", tag="button", text="确认")),
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert dict(observation.transition_parameters)["popup_dismissal_capability"] == (
        "modal_local_safe_acknowledgement"
    )


def test_pre_auth_flutter_popup_uses_sealed_snapshot_when_dom_candidate_is_missing() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="home",
            page_state="popup",
            actions=(),
        ),
        ui_snapshot={
            "elements": [
                {
                    "selector": "#app-open-label",
                    "tag": "flt-semantics",
                    "role": "text",
                    "text": "91PornAPP内打开",
                    "interactive": True,
                    "rect": {"x": 91, "y": 714, "width": 208, "height": 31},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#popup-body",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "parent_semantics_selector": "#popup-root",
                    "rect": {"x": 36, "y": 217, "width": 317, "height": 354},
                    "style": {"z_index": "2"},
                    "hit_test": {"center_hits_target": True},
                },
                {
                    "selector": "#popup-close",
                    "tag": "flt-semantics",
                    "role": None,
                    "text": "",
                    "interactive": True,
                    "parent_semantics_selector": "#popup-root",
                    "rect": {"x": 177, "y": 591, "width": 36, "height": 36},
                    "style": {"z_index": "3"},
                    "hit_test": {"center_hits_target": True},
                },
            ]
        },
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert observation.reason_code == "pre_auth_flutter_popup_snapshot_visible"
    assert observation.transient_target == "关闭当前弹窗"
    parameters = dict(observation.transition_parameters)
    assert parameters["target_mode"] == "popup_dismissal"
    assert parameters["popup_dismissal_capability"] == "flutter_app_open_popup_close"
    signature = parameters["obstruction_signature"]
    assert signature.startswith("h5-obstruction:")
    assert len(signature.removeprefix("h5-obstruction:")) == 64
    assert set(signature.removeprefix("h5-obstruction:")) <= set("0123456789abcdef")


def test_numeric_startup_ad_waits_before_auth_state_decision() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="startup_ad",
            page_state="countdown",
        ),
        ui_snapshot=_snapshot(_element("#countdown", tag="div", text="3")),
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert observation.transient_target is None
    assert dict(observation.transition_parameters) == {
        "target_mode": "startup_ad_countdown_reobserve",
    }


def test_line_loading_waits_before_auth_state_decision() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="line_loading",
            page_state="loading",
        ),
        ui_snapshot=_snapshot(_element("#loading", tag="div", text="正在获取线路")),
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert observation.transient_target is None
    assert observation.reason_code == "line_loading_wait"
    assert dict(observation.transition_parameters) == {
        "target_mode": "line_loading_reobserve",
    }


def test_startup_ad_skip_projects_controlled_dismissal() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="startup_ad",
            page_state="ad_visible",
        ),
        ui_snapshot=_snapshot(_element("#skip", tag="button", text="跳过")),
    )

    assert observation.page_kind is H5AuthPageKind.TRANSIENT_OBSTRUCTION
    assert observation.transient_target == "跳过"
    assert dict(observation.transition_parameters) == {
        "target_mode": "startup_ad_dismissal",
        "startup_ad_capability": "countdown_close",
    }


def test_platform_locator_target_is_not_accepted_as_semantic_target() -> None:
    """定位器透传出去会在 Navigator 建 Intent 时抛错；分析器必须当作认不出。"""
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="home",
            actions=(_action("#profile-entry", purpose="进入个人中心"),),
        ),
        ui_snapshot=_snapshot(_element("#profile", tag="button", text="")),
    )

    assert observation.profile_target is None


def test_home_page_challenge_copy_does_not_hide_profile_entry() -> None:
    """非登录页的验证码文案不得抢占 LLM 识别出的 Profile 导航。"""

    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="home",
            actions=(_action("我的", purpose="进入个人中心"),),
        ),
        ui_snapshot=_snapshot(
            _element("#profile", tag="button", text="我的"),
            _element("#campaign", tag="button", text="领取短信验证码活动"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.UNKNOWN
    assert observation.profile_target == "我的"
    assert observation.reason_code == "profile_entry_available"


def test_profile_page_with_logout_marker_is_authenticated() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="profile",
            page_state="authenticated",
            actions=(_action("退出登录", purpose="退出当前账号"),),
        ),
        ui_snapshot=_snapshot(
            _element("#avatar", tag="button", text="用户头像"),
            _element("#logout", tag="button", text="退出登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.PROFILE_AUTHENTICATED
    assert observation.reason_code == "authenticated_marker_visible"


def test_profile_page_with_login_button_is_guest() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="profile",
            page_state="未登录",
            actions=(_action("立即登录", purpose="登录后查看个人中心"),),
        ),
        ui_snapshot=_snapshot(_element("#login", tag="button", text="立即登录")),
    )

    assert observation.page_kind is H5AuthPageKind.PROFILE_GUEST
    assert observation.login_target == "立即登录"
    assert observation.reason_code == "profile_login_entry_visible"


def test_flutter_profile_group_projects_controlled_login_entry() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="profile",
            page_state="stable",
            actions=(
                _action(
                    "微笑自行车 ID: 13228263 立即登录",
                    purpose="登录后查看个人中心",
                ),
            ),
        ),
        ui_snapshot={
            "elements": [
                {
                    "selector": "#flutter-profile-login",
                    "tag": "flt-semantics",
                    "role": "group",
                    "text": "微笑自行车 ID: 13228263 立即登录",
                    "aria_label": "微笑自行车\nID: 13228263\n立即登录",
                    "parent_semantics_selector": "#profile-root",
                    "parent_semantics_tappable": False,
                    "interactive": True,
                    "rect": {"x": 16, "y": 31, "width": 359, "height": 62},
                    "hit_test": {"center_hits_target": False},
                }
            ]
        },
    )

    assert observation.page_kind is H5AuthPageKind.PROFILE_GUEST
    assert observation.login_target == "个人中心 Flutter 登录入口"
    assert dict(observation.transition_parameters) == {
        "target_mode": "flutter_profile_login_entry",
        "flutter_profile_login_capability": "profile_guest_login",
    }


def test_profile_unauthenticated_state_is_never_treated_as_authenticated() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="profile",
            page_state="unauthenticated",
            actions=(_action("Sign in", purpose="Open account login"),),
        ),
        ui_snapshot=_snapshot(_element("#login", tag="button", text="Sign in")),
    )

    assert observation.page_kind is H5AuthPageKind.PROFILE_GUEST
    assert observation.login_target == "Sign in"


def test_complete_password_form_returns_semantic_targets_without_selectors() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="login", actions=(_action("登录"),)),
        ui_snapshot=_snapshot(
            _element(
                "#phone-secret-selector",
                tag="input",
                element_type="tel",
                name="mobile",
                placeholder="请输入手机号",
            ),
            _element(
                "#password-secret-selector",
                tag="input",
                element_type="password",
                name="password",
                placeholder="请输入密码",
            ),
            _element("#submit-secret-selector", tag="button", text="登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.LOGIN_PASSWORD
    assert observation.phone_target == "请输入手机号"
    assert observation.password_target == "请输入密码"
    assert observation.submit_target == "登录"
    rendered = json.dumps(observation.to_safe_dict(), ensure_ascii=False)
    assert "selector" not in rendered
    assert "#phone-secret-selector" not in rendered


def test_registration_form_returns_secret_input_targets() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="login", actions=(_action("注册"),)),
        ui_snapshot=_snapshot(
            _element("#phone", tag="input", element_type="tel", placeholder="请输入手机号"),
            _element(
                "#password",
                tag="input",
                element_type="password",
                placeholder="请输入密码",
            ),
            _element("#register", tag="button", text="注册"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.LOGIN_PASSWORD
    assert observation.phone_target == "请输入手机号"
    assert observation.password_target == "请输入密码"
    assert observation.submit_target == "注册"


def test_dual_login_registration_form_keeps_both_safe_submit_targets() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="profile",
            page_state="login_required",
            actions=(_action("登录"),),
        ),
        ui_snapshot=_snapshot(
            _element(
                "#phone",
                tag="input",
                text="请输入账号(最少6位字母)或手机号",
            ),
            _element("#password", tag="input", text="请输入登录密码"),
            _element("#register", tag="flt-semantics", text="注册"),
            _element("#login", tag="flt-semantics", text="登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.LOGIN_PASSWORD
    assert observation.phone_target == "请输入账号(最少6位字母)或手机号"
    assert observation.password_target == "请输入登录密码"
    assert observation.submit_target == "登录"
    assert observation.registration_submit_target == "注册"


def test_registration_link_is_navigation_not_form_submission() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="login"),
        ui_snapshot=_snapshot(
            _element("#phone", tag="input", element_type="tel", placeholder="手机号"),
            _element("#password", tag="input", element_type="password", placeholder="密码"),
            _element("#login", tag="button", text="登录"),
            _element("#register-link", tag="a", text="注册"),
        ),
    )
    assert observation.page_kind is H5AuthPageKind.LOGIN_PASSWORD
    assert observation.submit_target == "登录"
    assert observation.registration_entry_target == "注册"
    assert observation.registration_submit_target is None


def test_registration_success_modal_returns_safe_close_target() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="login",
            page_state="popup",
            actions=(_action("关闭当前弹窗", purpose="关闭注册成功提示"),),
        ),
        ui_snapshot=_snapshot(
            _element("#success", tag="div", text="注册成功"),
            _element("#close", tag="button", text="关闭"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.SUCCESS_NOTICE
    assert observation.success_dismiss_target == "关闭当前弹窗"


def test_visual_registration_success_reason_can_close_unique_notice() -> None:
    detection = _detection(
        page_type="login",
        page_state="popup",
        actions=(_action("关闭当前弹窗", purpose="关闭结果提示"),),
    )
    detection["reason"] = "截图显示注册成功弹窗"

    observation = H5AuthPageAnalyzer().analyze(
        detection_result=detection,
        ui_snapshot=_snapshot(_element("#close", tag="button", text="关闭")),
    )

    assert observation.page_kind is H5AuthPageKind.SUCCESS_NOTICE
    assert observation.success_dismiss_target == "关闭当前弹窗"
    assert observation.reason_code == "registration_success_notice_visible"


def test_profile_identity_without_login_entry_is_authenticated() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="profile"),
        ui_snapshot=_snapshot(
            _element("#identity", tag="div", text="ID：12345678"),
            _element("#settings", tag="button", text="设置"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.PROFILE_AUTHENTICATED
    assert observation.reason_code == "profile_identity_visible"


def test_sms_login_can_switch_to_password_mode() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="login",
            page_state="验证码登录",
            actions=(_action("密码登录", purpose="切换登录方式"),),
        ),
        ui_snapshot=_snapshot(
            _element("#sms", tag="input", placeholder="短信验证码"),
            _element("#password-mode", tag="button", text="密码登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.LOGIN_SWITCHABLE
    assert observation.password_mode_target == "密码登录"
    assert observation.reason_code == "password_mode_available"


def test_challenge_only_login_is_not_treated_as_password_login() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="login", page_state="扫码或验证码登录"),
        ui_snapshot=_snapshot(
            _element("#sms", tag="input", placeholder="短信验证码"),
            _element("#send", tag="button", text="发送验证码"),
            _element("#qr", tag="button", text="扫码登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.LOGIN_CHALLENGE
    assert observation.reason_code == "unsupported_login_challenge"


def test_password_form_with_required_verification_code_fails_closed() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="login", actions=(_action("登录"),)),
        ui_snapshot=_snapshot(
            _element("#phone", tag="input", element_type="tel", placeholder="手机号"),
            _element(
                "#password",
                tag="input",
                element_type="password",
                placeholder="密码",
            ),
            _element("#captcha", tag="input", placeholder="图形验证码"),
            _element("#submit", tag="button", text="登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.LOGIN_CHALLENGE
    assert observation.reason_code == "unsupported_login_challenge"


def test_password_failure_marker_prevents_authenticated_or_ready_claim() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(
            page_type="login",
            page_state="账号或密码错误",
            actions=(_action("登录"),),
        ),
        ui_snapshot=_snapshot(
            _element("#phone", tag="input", element_type="tel", placeholder="手机号"),
            _element(
                "#password",
                tag="input",
                element_type="password",
                placeholder="密码",
            ),
            _element("#submit", tag="button", text="登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.UNKNOWN
    assert observation.reason_code == "login_failure_marker_visible"


def test_ambiguous_phone_fields_fail_closed() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="login", actions=(_action("登录"),)),
        ui_snapshot=_snapshot(
            _element("#phone-a", tag="input", element_type="tel", placeholder="手机号"),
            _element("#phone-b", tag="input", element_type="tel", placeholder="备用手机号"),
            _element("#password", tag="input", element_type="password", placeholder="密码"),
            _element("#submit", tag="button", text="登录"),
        ),
    )

    assert observation.page_kind is H5AuthPageKind.UNKNOWN
    assert observation.phone_target is None
    assert observation.reason_code == "phone_field_ambiguous"


def test_unrelated_page_remains_unknown() -> None:
    observation = H5AuthPageAnalyzer().analyze(
        detection_result=_detection(page_type="product_detail"),
        ui_snapshot=_snapshot(_element("#buy", tag="button", text="立即购买")),
    )

    assert observation.page_kind is H5AuthPageKind.UNKNOWN
    assert observation.reason_code == "auth_state_unknown"
