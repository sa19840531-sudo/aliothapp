"""只保留理由拒绝类别，不能把敏感模型原文写进诊断。"""

import json

import pytest

from services.api.app.application.web_assessment_evidence import sanitize_web_model_reason
from services.api.app.application.web_detail_selection_diagnostics import (
    WebDetailSelectionDiagnosticError,
    detail_selection_diagnostic_scope,
    detail_selection_failure_diagnostic,
)


@pytest.mark.parametrize(
    "reason,category",
    [
        (" ", "empty_reason"),
        ("password=untrusted-private-value", "credential_content"),
        ("0123456789abcdef01234567", "high_entropy_content"),
        ("%2525252541", "excessive_encoding"),
    ],
)
def test_rejection_category_survives_bounded_diagnostic_without_reason(reason, category):
    with pytest.raises(WebDetailSelectionDiagnosticError) as caught:
        with detail_selection_diagnostic_scope() as context:
            context.phase = "reason_sanitize"
            sanitize_web_model_reason(reason)
    diagnostic = detail_selection_failure_diagnostic(
        caught.value,
        source_capture_id="capture",
        screenshot_artifact_id="image",
        dom_artifact_id="dom",
        candidate_ids=("candidate",),
    )
    assert diagnostic["reason_rejection_category"] == category
    assert diagnostic["reason_code"] == "WEB_DETAIL_SELECTION_REASON_REJECTED"
    assert reason.strip() == "" or reason not in json.dumps(diagnostic)
