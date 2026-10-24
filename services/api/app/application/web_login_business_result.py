"""把认证接口的响应压缩为不含账号和响应正文的稳定失败原因。"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def classify_login_failure(url: str, http_status: int, payload: object) -> str | None:
    if not re.search(r"login|log-in|sign-?in|auth|session", urlsplit(url).path, re.I):
        return None
    if not isinstance(payload, dict):
        return None
    raw_code = payload.get("code", payload.get("errcode"))
    code = None
    if type(raw_code) is int:
        code = raw_code
    elif isinstance(raw_code, str) and re.fullmatch(r"-?\d{1,8}", raw_code):
        code = int(raw_code)
    rejected = http_status >= 400 or (code is not None and code < 0)
    rejected = rejected or payload.get("success") is False
    if not rejected:
        return None
    message = " ".join(
        value[:512]
        for key in ("msg", "message", "error", "error_message")
        if isinstance(value := payload.get(key), str)
    )
    if re.search(r"账号|账户|用户名|密码|account|username|password", message, re.I) and re.search(
        r"错误|不正确|失败|无效|不存在|不匹配|incorrect|invalid|wrong|fail|error",
        message,
        re.I,
    ):
        return "WEB_LOGIN_CREDENTIALS_REJECTED"
    return None
