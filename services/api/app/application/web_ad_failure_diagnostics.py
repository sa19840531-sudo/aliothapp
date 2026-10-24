"""Web 广告失败仅持久化有界标识，不复制异常文本或请求内容。"""

import re
from collections.abc import Mapping


def safe_ad_failure_code(value):
    """拒绝把非稳定错误码当作业务原因传播。"""
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", value)
        else "WEB_AD_PAGE_OPEN_FAILED"
    )


def safe_ad_failure_detail(detail=None, *, error=None, phase_id=None):
    """三个固定字段独立校验；其余输入永不进入审计载荷。"""
    values = dict(detail) if isinstance(detail, Mapping) else {}
    if error is not None:
        values.setdefault("cause_type", type(error).__name__)
        values.setdefault("raw_error_code", getattr(error, "error_code", None))
    if phase_id is not None:
        values["surface_phase_run_id"] = phase_id
    patterns = {
        "cause_type": r"[A-Za-z][A-Za-z0-9_]{0,127}",
        "raw_error_code": r"[A-Z][A-Z0-9_]{0,127}",
        "surface_phase_run_id": r"surface_phase_run:[0-9a-f]{64}",
    }
    return {
        key: value for key, pattern in patterns.items()
        if isinstance(value := values.get(key), str) and re.fullmatch(pattern, value)
    }
