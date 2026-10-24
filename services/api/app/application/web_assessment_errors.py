"""Web 只读模型判定的应用层可恢复错误。"""

from __future__ import annotations


class WebAssessmentTransientServiceError(RuntimeError):
    """明确模型服务端 5xx 或模型调用超时耗尽重试，不包含证据完整性故障。"""

    def __init__(self, *, status_code: int) -> None:
        if status_code not in {500, 502, 503, 504}:
            raise ValueError("status_code 必须为可恢复的模型服务端状态码")
        super().__init__(f"WEB_ASSESSMENT_TRANSIENT_SERVICE_{status_code}")
        self.status_code = status_code


class WebAssessmentPermissionDeniedError(RuntimeError):
    """模型供应商明确拒绝本次只读判定，不能重试或绕过。"""

    def __init__(self, *, assessment_kind: str = "web_page_type") -> None:
        reason_codes = {
            "web_page_type": "WEB_PAGE_TYPE_MODEL_PERMISSION_DENIED",
            "web_detail_selection": "WEB_DETAIL_SELECTION_MODEL_PERMISSION_DENIED",
        }
        if assessment_kind not in reason_codes:
            raise ValueError("assessment_kind 必须为受支持的 Web 判定类型")
        self.assessment_kind = assessment_kind
        super().__init__(reason_codes[assessment_kind])


__all__ = ["WebAssessmentPermissionDeniedError", "WebAssessmentTransientServiceError"]
