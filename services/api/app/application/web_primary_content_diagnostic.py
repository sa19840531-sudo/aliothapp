"""主体补图审计的最小固定字段，不透传 URL、选择器、DOM 或异常正文。"""

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, is_dataclass


def primary_content_diagnostic(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if is_dataclass(value):
        value = asdict(value)
    elif not isinstance(value, Mapping):
        value = vars(value) if hasattr(value, "__dict__") else {}
    result = {
        key: _identifier(value.get(key))
        for key in (
            "status",
            "parent_capture_id",
            "evidence_capture_id",
            "logical_page_id",
            "reason_code",
            "diagnostic_type",
        )
    }
    result["reused"] = value.get("reused") is True
    for key in ("scroll_count", "original_scroll_count"):
        number = value.get(key)
        result[key] = number if type(number) is int and 0 <= number <= 2 else None
    for key in ("before_ad_coverage", "after_ad_coverage"):
        number = value.get(key)
        result[key] = (
            number
            if type(number) in (int, float) and math.isfinite(number) and 0 <= number <= 1
            else None
        )
    result["attempts"] = [
        {
            key: (_count(row.get(key)) if key == "attempt" else _identifier(row.get(key)))
            for key in (
                "attempt",
                "source_capture_id",
                "before_capture_id",
                "after_capture_id",
                "action_execution_id",
                "stage",
                "error_code",
            )
        }
        for row in _rows(value.get("attempts"))[:2]
    ]
    result["interaction_outcomes"] = [
        {
            **{
                key: row.get(key) is True
                for key in (
                    "command_accepted",
                    "effect_observed",
                    "postcondition_satisfied",
                    "transition_arrived",
                )
            },
            **{
                key: _identifier(row.get(key))
                for key in (
                    "after_capture_id",
                    "failure_reason",
                    "evidence_mode",
                )
            },
        }
        for row in _rows(value.get("interaction_outcomes"))[:2]
    ]
    return result


def primary_content_diagnostics(value: object) -> list[dict[str, object]]:
    return [primary_content_diagnostic(row) for row in _rows(value)[:2]]


def primary_content_failure(capture_id: str, exc: Exception):
    from services.api.app.application.web_primary_content_capture import (
        WebPrimaryContentCaptureResult,
    )

    return WebPrimaryContentCaptureResult(
        status="failed",
        parent_capture_id=capture_id,
        evidence_capture_id=None,
        logical_page_id="",
        target_candidate_id=None,
        scroll_count=0,
        before_ad_coverage=None,
        after_ad_coverage=None,
        reason_code="WEB_PRIMARY_CONTENT_CAPTURE_FAILED",
        diagnostic_type=type(exc).__name__,
    )


def _rows(value: object) -> list[Mapping]:
    return (
        [row for row in value if isinstance(row, Mapping)]
        if isinstance(value, (list, tuple))
        else []
    )


def _identifier(value: object) -> str | None:
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", value) else None
    )


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= 2 else None
