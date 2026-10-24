"""登录 API 的业务拒绝只输出稳定原因码，不持久化响应正文。"""

from services.api.app.application.web_login_business_result import classify_login_failure


def test_same_origin_auth_api_credential_rejection_is_classified():
    assert classify_login_failure(
        "https://example.test/index/login", 200,
        {"code": -9999, "data": None, "msg": "账号或密码错误"},
    ) == "WEB_LOGIN_CREDENTIALS_REJECTED"


def test_success_or_unrelated_api_response_is_not_called_credential_failure():
    assert classify_login_failure(
        "https://example.test/index/login", 200,
        {"code": 0, "msg": "登录成功"},
    ) is None
    assert classify_login_failure(
        "https://example.test/analytics", 200,
        {"code": -9999, "msg": "密码错误"},
    ) is None
    assert classify_login_failure(
        "https://example.test/index/login", 200,
        {"code": -9999, "msg": "服务繁忙"},
    ) is None
