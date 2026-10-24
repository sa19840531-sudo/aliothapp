"""通过状态等价证明挂接重放补采证据的稳定协议。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from services.api.app.domain.inspection.evidence_request import EvidenceSourceGrade
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_text,
    stable_protocol_hash,
)

EVIDENCE_SUPPLEMENT_PROTOCOL_VERSION = "evidence-supplement.v1"
EVIDENCE_SUPPLEMENT_ORIGIN = "replay_probe"


class EvidenceSupplementValidationError(ValueError):
    """补采证据缺少 Probe、恢复尝试或等价证明身份。"""


@dataclass(frozen=True, kw_only=True)
class EvidenceSupplement:
    """只描述已证明等价的补采 Manifest 如何关联原请求。"""

    supplement_id: str
    task_id: str
    request_id: str
    manifest_id: str
    probe_work_id: str
    restoration_attempt_id: str
    equivalence_assessment_id: str
    source_grade: EvidenceSourceGrade
    created_at: datetime
    origin: str = EVIDENCE_SUPPLEMENT_ORIGIN
    protocol_version: str = EVIDENCE_SUPPLEMENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "supplement_id",
            "task_id",
            "request_id",
            "manifest_id",
            "probe_work_id",
            "restoration_attempt_id",
            "equivalence_assessment_id",
        ):
            _require_text(getattr(self, field_name), field_name)
        _require_aware_datetime(self.created_at, "created_at")
        if not isinstance(self.source_grade, EvidenceSourceGrade):
            raise EvidenceSupplementValidationError("source_grade 不受支持")
        if self.source_grade in {EvidenceSourceGrade.A0, EvidenceSourceGrade.D}:
            raise EvidenceSupplementValidationError("重放补采来源等级只能是 A1、B 或 C")
        if self.origin != EVIDENCE_SUPPLEMENT_ORIGIN:
            raise EvidenceSupplementValidationError("origin 只能标记为 replay_probe")
        if self.protocol_version != EVIDENCE_SUPPLEMENT_PROTOCOL_VERSION:
            raise EvidenceSupplementValidationError("EvidenceSupplement 版本不受支持")
        expected_id = build_evidence_supplement_id(
            request_id=self.request_id,
            task_id=self.task_id,
            manifest_id=self.manifest_id,
            probe_work_id=self.probe_work_id,
            restoration_attempt_id=self.restoration_attempt_id,
            equivalence_assessment_id=self.equivalence_assessment_id,
            source_grade=self.source_grade,
        )
        if self.supplement_id != expected_id:
            raise EvidenceSupplementValidationError("supplement_id 与补采证明身份不一致")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "supplement_id": self.supplement_id,
            "task_id": self.task_id,
            "request_id": self.request_id,
            "manifest_id": self.manifest_id,
            "probe_work_id": self.probe_work_id,
            "restoration_attempt_id": self.restoration_attempt_id,
            "equivalence_assessment_id": self.equivalence_assessment_id,
            "source_grade": self.source_grade.value,
            "origin": self.origin,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> EvidenceSupplement:
        known = {
            "protocol_version",
            "supplement_id",
            "task_id",
            "request_id",
            "manifest_id",
            "probe_work_id",
            "restoration_attempt_id",
            "equivalence_assessment_id",
            "source_grade",
            "origin",
            "created_at",
        }
        try:
            values = read_known_fields(
                payload,
                known_fields=frozenset(known),
                required_fields=frozenset(known),
                field_name="EvidenceSupplement",
            )
            return cls(
                supplement_id=values["supplement_id"],
                task_id=values["task_id"],
                request_id=values["request_id"],
                manifest_id=values["manifest_id"],
                probe_work_id=values["probe_work_id"],
                restoration_attempt_id=values["restoration_attempt_id"],
                equivalence_assessment_id=values["equivalence_assessment_id"],
                source_grade=EvidenceSourceGrade(values["source_grade"]),
                origin=values["origin"],
                created_at=parse_aware_datetime(values["created_at"], "created_at"),
                protocol_version=values["protocol_version"],
            )
        except (ProtocolContractError, ValueError) as exc:
            raise EvidenceSupplementValidationError(str(exc)) from exc


def build_evidence_supplement(
    *,
    task_id: str,
    request_id: str,
    manifest_id: str,
    probe_work_id: str,
    restoration_attempt_id: str,
    equivalence_assessment_id: str,
    source_grade: EvidenceSourceGrade,
    created_at: datetime,
) -> EvidenceSupplement:
    """以完整证明链生成幂等补采身份。"""
    return EvidenceSupplement(
        supplement_id=build_evidence_supplement_id(
            request_id=request_id,
            task_id=task_id,
            manifest_id=manifest_id,
            probe_work_id=probe_work_id,
            restoration_attempt_id=restoration_attempt_id,
            equivalence_assessment_id=equivalence_assessment_id,
            source_grade=source_grade,
        ),
        request_id=request_id,
        task_id=task_id,
        manifest_id=manifest_id,
        probe_work_id=probe_work_id,
        restoration_attempt_id=restoration_attempt_id,
        equivalence_assessment_id=equivalence_assessment_id,
        source_grade=source_grade,
        created_at=created_at,
    )


def build_evidence_supplement_id(
    *,
    task_id: str,
    request_id: str,
    manifest_id: str,
    probe_work_id: str,
    restoration_attempt_id: str,
    equivalence_assessment_id: str,
    source_grade: EvidenceSourceGrade,
) -> str:
    if not isinstance(source_grade, EvidenceSourceGrade):
        raise EvidenceSupplementValidationError("source_grade 不受支持")
    return "evidence-supplement:" + stable_protocol_hash(
        {
            "task_id": _require_text(task_id, "task_id"),
            "request_id": _require_text(request_id, "request_id"),
            "manifest_id": _require_text(manifest_id, "manifest_id"),
            "probe_work_id": _require_text(probe_work_id, "probe_work_id"),
            "restoration_attempt_id": _require_text(
                restoration_attempt_id,
                "restoration_attempt_id",
            ),
            "equivalence_assessment_id": _require_text(
                equivalence_assessment_id,
                "equivalence_assessment_id",
            ),
            "source_grade": source_grade.value,
            "origin": EVIDENCE_SUPPLEMENT_ORIGIN,
        }
    )


def _require_text(value: Any, field_name: str) -> str:
    try:
        return require_text(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceSupplementValidationError(str(exc)) from exc


def _require_aware_datetime(value: Any, field_name: str) -> None:
    try:
        require_aware_datetime(value, field_name)
    except ProtocolContractError as exc:
        raise EvidenceSupplementValidationError(str(exc)) from exc
