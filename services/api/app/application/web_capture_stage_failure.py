"""把 Capture 协调原始错误转换为稳定阶段代码及安全审计字段。"""

import re
from hashlib import sha256

from services.api.app.application.web_run_stage_orchestrator import WebStageFailure


def bootstrap_coordination_failure(error, *, reference):
    # 首跳前的空白 Capture 只属于动作审计，不可作为业务页恢复引用。
    # 不用请求 URL 冒充实际已到达页面，原始错误仍由协调诊断保留。
    if reference.get("current_url") in (None, "", "about:blank"):
        reference = {}
    return coordination_stage_failure(error, reference=reference, stage="bootstrap")


def stage_failure_from_code(raw, *, stage, transient=False):
    """导航结果已吞并协调异常时，仍转换代码并保留安全原始诊断。"""
    code = (
        raw
        if isinstance(raw, str) and re.fullmatch(r"WEB_[A-Z0-9_]{1,123}", raw)
        else {
            "before_capture_failed": f"WEB_{stage.upper()}_BEFORE_CAPTURE_FAILED",
            "after_capture_failed": f"WEB_{stage.upper()}_AFTER_CAPTURE_FAILED",
        }.get(raw if isinstance(raw, str) else None, f"WEB_{stage.upper()}_COORDINATION_FAILED")
    )
    return WebStageFailure(
        code, transient=transient, detail={"raw_error_code": _safe_token(raw)},
    )


def coordination_stage_failure(error, *, reference, stage):
    failure = stage_failure_from_code(error.error_code, stage=stage, transient=error.retryable)
    detail = {
        **reference,
        **failure.detail,
        "coordination_stage": error.stage.value,
        "retryable": error.retryable,
    }
    if error.cause_type is not None:
        detail["cause_type"] = _safe_token(error.cause_type)
    return WebStageFailure(failure.code, transient=failure.transient, detail=detail)


def _safe_token(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value):
        return value
    # 不把异常正文、URL 或凭据写进恢复审计；不安全标签仅保留不可逆指纹。
    return "sha256_" + sha256(str(value).encode("utf-8")).hexdigest()
