"""依据版本化 Catalog 对 H5 页面识别结果进行服务端二次裁决。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

from services.api.app.domain.page.catalog import PageTypeCatalog
from services.api.app.domain.page.registry import recognition_signal_references

H5_RECOGNITION_GUIDE_AUDIT_VERSION = "h5-recognition-guide-audit.v1"
H5RecognitionGuideStatus = Literal["accepted", "downgraded", "not_applicable"]


@dataclass(frozen=True, slots=True)
class H5PageRecognitionDecision:
    """页面识别二次裁决及其可持久化审计材料。"""

    status: H5RecognitionGuideStatus
    accepted: bool
    authority_source: str
    candidate_page_type: str | None
    reason_code: str
    required_positive_signal_ids: tuple[str, ...] = ()
    matched_required_positive_signal_ids: tuple[str, ...] = ()
    missing_required_positive_signal_ids: tuple[str, ...] = ()
    matched_supporting_signal_ids: tuple[str, ...] = ()
    matched_strong_negative_signal_ids: tuple[str, ...] = ()
    excluded_confusable_page_types: tuple[str, ...] = ()
    unresolved_confusable_page_types: tuple[str, ...] = ()
    invalid_signal_ids: tuple[str, ...] = ()
    invalid_confusable_page_types: tuple[str, ...] = ()
    missing_evidence_actions: tuple[str, ...] = ()
    schema_version: str = H5_RECOGNITION_GUIDE_AUDIT_VERSION

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_h5_page_recognition(
    *,
    catalog: PageTypeCatalog,
    detection_status: str,
    page_type: str | None,
    matched_required_positive_signal_ids: tuple[str, ...] | list[str],
    matched_supporting_signal_ids: tuple[str, ...] | list[str],
    matched_strong_negative_signal_ids: tuple[str, ...] | list[str],
    excluded_confusable_page_types: tuple[str, ...] | list[str],
    deterministic_authority_source: str | None = None,
) -> H5PageRecognitionDecision:
    """只允许证据引用完整或服务端确定性证明成立的 recognized 结论。"""
    if detection_status != "recognized" or not page_type:
        return H5PageRecognitionDecision(
            status="not_applicable",
            accepted=False,
            authority_source="none",
            candidate_page_type=page_type,
            reason_code="recognition_not_claimed",
        )

    definition = catalog.require_code(page_type)
    references = recognition_signal_references(definition)
    required_ids = _reference_ids(references["required_positive_signals"])
    supporting_ids = _reference_ids(references["supporting_signals"])
    negative_ids = _reference_ids(references["strong_negative_signals"])
    matched_required = _normalized_values(matched_required_positive_signal_ids)
    matched_supporting = _normalized_values(matched_supporting_signal_ids)
    matched_negative = _normalized_values(matched_strong_negative_signal_ids)
    excluded_confusable = _normalized_values(excluded_confusable_page_types)
    expected_confusable = tuple(definition.confusable_with)
    invalid_signal_ids = tuple(
        sorted(
            (set(matched_required) - set(required_ids))
            | (set(matched_supporting) - set(supporting_ids))
            | (set(matched_negative) - set(negative_ids))
        )
    )
    invalid_confusable = tuple(sorted(set(excluded_confusable) - set(expected_confusable)))
    missing_required = tuple(item for item in required_ids if item not in matched_required)
    unresolved_confusable = tuple(
        item for item in expected_confusable if item not in excluded_confusable
    )

    common = {
        "candidate_page_type": page_type,
        "required_positive_signal_ids": required_ids,
        "matched_required_positive_signal_ids": matched_required,
        "missing_required_positive_signal_ids": missing_required,
        "matched_supporting_signal_ids": matched_supporting,
        "matched_strong_negative_signal_ids": matched_negative,
        "excluded_confusable_page_types": excluded_confusable,
        "unresolved_confusable_page_types": unresolved_confusable,
        "invalid_signal_ids": invalid_signal_ids,
        "invalid_confusable_page_types": invalid_confusable,
        "missing_evidence_actions": tuple(definition.missing_evidence_actions),
    }
    if deterministic_authority_source:
        return H5PageRecognitionDecision(
            status="accepted",
            accepted=True,
            authority_source=deterministic_authority_source,
            reason_code="deterministic_authority_accepted",
            **common,
        )
    if invalid_signal_ids or invalid_confusable:
        reason_code = "recognition_guide_reference_invalid"
    elif matched_negative:
        reason_code = "strong_negative_signal_matched"
    elif missing_required:
        reason_code = "required_positive_signal_missing"
    elif unresolved_confusable:
        reason_code = "confusable_page_type_unresolved"
    else:
        return H5PageRecognitionDecision(
            status="accepted",
            accepted=True,
            authority_source="model_guide",
            reason_code="recognition_guide_satisfied",
            **common,
        )
    return H5PageRecognitionDecision(
        status="downgraded",
        accepted=False,
        authority_source="model_guide",
        reason_code=reason_code,
        **common,
    )


def _reference_ids(references: list[dict[str, str]]) -> tuple[str, ...]:
    return tuple(item["signal_id"] for item in references)


def _normalized_values(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
