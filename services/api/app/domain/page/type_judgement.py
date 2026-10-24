"""页面类型判断记录与裁定（04 专项 §5.6、§17.3）。

同一个页面可以有多份页型判断（结构规则、模型、网页评估……），每份各留自己的依据；
判断打架时裁定为「未知」并把冲突留着，不拿置信度最高的那份硬定论。后来的改判只追加
新记录并指回被改的那条，不覆盖历史，不重建页面身份，也不动已冻结的必检范围；正式收口
之后才到的判断只留档，不参与裁定。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

PAGE_TYPE_JUDGEMENT_PROTOCOL_VERSION = "page-type-judgement.v1"
PAGE_TYPE_RESOLUTION_PROTOCOL_VERSION = "page-type-resolution.v1"

# 任务进入这些收口状态后到达的判断只留档，不能改写冻结结果（§5.6）。
SEALED_FINALIZATION_STATES: frozenset[str] = frozenset({"finalizing", "sealed"})


class PageTypeJudgementValidationError(ValueError):
    """判断记录缺少稳定身份或改判链不完整。"""


class JudgementKind(StrEnum):
    JUDGEMENT = "judgement"
    REVISION = "revision"


class ResolutionStatus(StrEnum):
    CONFIRMED = "confirmed"
    REVISED = "revised"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"
    NO_JUDGEMENT = "no_judgement"


SOURCE_LABELS: Mapping[str, str] = {
    "structure_rules": "结构规则",
    "model": "模型判断",
    "detection": "识别结果",
    "web_assessment_model": "网页模型判断",
    "web_assessment_observed": "网页观察",
    "app_visual": "App 视觉",
    "manual": "人工",
}

STATUS_LABELS: Mapping[ResolutionStatus, str] = {
    ResolutionStatus.CONFIRMED: "判断一致",
    ResolutionStatus.REVISED: "已改判",
    ResolutionStatus.CONFLICTING: "判断冲突",
    ResolutionStatus.UNKNOWN: "未知",
    ResolutionStatus.NO_JUDGEMENT: "没有判断记录",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source)


def status_label(status: ResolutionStatus | str) -> str:
    try:
        resolved = ResolutionStatus(str(status))
    except ValueError:
        return str(status)
    return STATUS_LABELS[resolved]


def _require_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PageTypeJudgementValidationError(f"{field_name} 不能为空")
    return text


def canonical_basis_hash(
    *,
    page_type: str | None,
    page_state: str,
    catalog_version: str,
    basis: Mapping[str, Any],
) -> str:
    """判断内容的稳定哈希：同一页面、同一来源、同样内容再记一次是幂等的。"""

    payload = {
        "page_type": page_type or "",
        "page_state": page_state or "",
        "catalog_version": catalog_version or "",
        "basis": dict(basis),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_judgement_id(*, task_id: str, page_capture_id: str, source: str, basis_hash: str) -> str:
    digest = hashlib.sha256(
        "|".join((task_id, page_capture_id, source, basis_hash)).encode("utf-8")
    ).hexdigest()
    return f"page-type-judgement:{digest[:32]}"


@dataclass(frozen=True, kw_only=True)
class PageTypeJudgement:
    """一份页型判断：谁判的、判成什么、凭什么；改判时指回被改的那条。"""

    judgement_id: str
    task_id: str
    page_capture_id: str
    sequence_no: int
    source: str
    page_type: str | None
    kind: JudgementKind = JudgementKind.JUDGEMENT
    revises_judgement_id: str | None = None
    page_state: str = ""
    confidence: float | None = None
    catalog_version: str = ""
    agent_run_id: str | None = None
    basis: Mapping[str, Any] = field(default_factory=dict)
    task_finalization_state: str = ""
    created_at: datetime | None = None
    protocol_version: str = PAGE_TYPE_JUDGEMENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        _require_text(self.judgement_id, "judgement_id")
        _require_text(self.task_id, "task_id")
        _require_text(self.page_capture_id, "page_capture_id")
        _require_text(self.source, "source")
        if isinstance(self.sequence_no, bool) or int(self.sequence_no) <= 0:
            raise PageTypeJudgementValidationError("sequence_no 必须是正整数")
        if not isinstance(self.kind, JudgementKind):
            raise PageTypeJudgementValidationError("kind 不受支持")
        if (self.kind is JudgementKind.REVISION) != bool(self.revises_judgement_id):
            raise PageTypeJudgementValidationError("改判必须指回被改的判断，普通判断不能带 revises")
        if self.confidence is not None and not 0 <= float(self.confidence) <= 1:
            raise PageTypeJudgementValidationError("confidence 必须在 0 到 1 之间")
        if self.protocol_version != PAGE_TYPE_JUDGEMENT_PROTOCOL_VERSION:
            raise PageTypeJudgementValidationError("判断记录协议版本不受支持")
        page_type = str(self.page_type).strip() if self.page_type is not None else None
        object.__setattr__(self, "page_type", page_type or None)
        object.__setattr__(self, "basis", dict(self.basis))

    @property
    def arrived_after_seal(self) -> bool:
        return self.task_finalization_state in SEALED_FINALIZATION_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "judgement_id": self.judgement_id,
            "task_id": self.task_id,
            "page_capture_id": self.page_capture_id,
            "agent_run_id": self.agent_run_id,
            "sequence_no": self.sequence_no,
            "source": self.source,
            "source_label": source_label(self.source),
            "page_type": self.page_type,
            "page_state": self.page_state,
            "confidence": self.confidence,
            "catalog_version": self.catalog_version,
            "kind": self.kind.value,
            "revises_judgement_id": self.revises_judgement_id,
            "basis": dict(self.basis),
            "task_finalization_state": self.task_finalization_state,
            "arrived_after_seal": self.arrived_after_seal,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass(frozen=True, kw_only=True)
class PageTypeRevision:
    """同一来源前后判断不同：从什么改成什么。"""

    source: str
    judgement_id: str
    from_page_type: str | None
    to_page_type: str | None
    sequence_no: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_label": source_label(self.source),
            "judgement_id": self.judgement_id,
            "from_page_type": self.from_page_type,
            "to_page_type": self.to_page_type,
            "sequence_no": self.sequence_no,
        }


@dataclass(frozen=True, kw_only=True)
class PageTypeResolution:
    """按判断记录算出来的裁定；只是解释，不是页面身份，也不改正式识别结果。"""

    page_capture_id: str
    status: ResolutionStatus
    page_type: str | None
    reason: str
    current_by_source: Mapping[str, str | None] = field(default_factory=dict)
    conflicting_page_types: tuple[str, ...] = ()
    revisions: tuple[PageTypeRevision, ...] = ()
    adopted_judgement_id: str | None = None
    judgement_count: int = 0
    after_seal_count: int = 0
    protocol_version: str = PAGE_TYPE_RESOLUTION_PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "page_capture_id": self.page_capture_id,
            "status": self.status.value,
            "status_label": status_label(self.status),
            "page_type": self.page_type,
            "reason": self.reason,
            "current_by_source": {
                source: page_type for source, page_type in self.current_by_source.items()
            },
            "conflicting_page_types": list(self.conflicting_page_types),
            "revisions": [item.to_dict() for item in self.revisions],
            "adopted_judgement_id": self.adopted_judgement_id,
            "judgement_count": self.judgement_count,
            "after_seal_count": self.after_seal_count,
        }


def resolve_page_type(
    page_capture_id: str,
    judgements: Sequence[PageTypeJudgement],
) -> PageTypeResolution:
    """按各来源最新判断裁定：全部一致才确认，不一致标未知并保留冲突，置信度不参与定论。"""

    records = sorted(
        (item for item in judgements if item.page_capture_id == page_capture_id),
        key=lambda item: item.sequence_no,
    )
    if not records:
        return PageTypeResolution(
            page_capture_id=page_capture_id,
            status=ResolutionStatus.NO_JUDGEMENT,
            page_type=None,
            reason="还没有页型判断记录",
        )
    effective = [item for item in records if not item.arrived_after_seal]
    after_seal_count = len(records) - len(effective)
    if not effective:
        return PageTypeResolution(
            page_capture_id=page_capture_id,
            status=ResolutionStatus.UNKNOWN,
            page_type=None,
            reason="只有收口后到达的判断，不改写已冻结的结果",
            judgement_count=len(records),
            after_seal_count=after_seal_count,
        )

    by_source: dict[str, list[PageTypeJudgement]] = {}
    for item in effective:
        by_source.setdefault(item.source, []).append(item)
    current_by_source: dict[str, str | None] = {}
    revisions: list[PageTypeRevision] = []
    currents: list[PageTypeJudgement] = []
    for source, items in by_source.items():
        previous: PageTypeJudgement | None = None
        for item in items:
            if previous is not None and previous.page_type != item.page_type:
                revisions.append(
                    PageTypeRevision(
                        source=source,
                        judgement_id=item.judgement_id,
                        from_page_type=previous.page_type,
                        to_page_type=item.page_type,
                        sequence_no=item.sequence_no,
                    )
                )
            previous = item
        assert previous is not None
        current_by_source[source] = previous.page_type
        currents.append(previous)
    revisions.sort(key=lambda item: item.sequence_no)

    page_types = sorted({item.page_type for item in currents if item.page_type})
    if not page_types:
        return PageTypeResolution(
            page_capture_id=page_capture_id,
            status=ResolutionStatus.UNKNOWN,
            page_type=None,
            reason="各来源都没有给出页型",
            current_by_source=current_by_source,
            revisions=tuple(revisions),
            judgement_count=len(records),
            after_seal_count=after_seal_count,
        )
    if len(page_types) > 1:
        detail = "，".join(
            f"{source_label(source)}判为 {page_type or '未知'}"
            for source, page_type in current_by_source.items()
        )
        return PageTypeResolution(
            page_capture_id=page_capture_id,
            status=ResolutionStatus.CONFLICTING,
            page_type=None,
            reason=f"各来源判断不一致（{detail}），按未知处理，不凭置信度硬定论",
            current_by_source=current_by_source,
            conflicting_page_types=tuple(page_types),
            revisions=tuple(revisions),
            judgement_count=len(records),
            after_seal_count=after_seal_count,
        )
    adopted = max(
        (item for item in currents if item.page_type == page_types[0]),
        key=lambda item: item.sequence_no,
    )
    status = ResolutionStatus.REVISED if revisions else ResolutionStatus.CONFIRMED
    reason = (
        f"经过 {len(revisions)} 次改判后各来源一致为 {page_types[0]}"
        if revisions
        else f"各来源一致为 {page_types[0]}"
    )
    return PageTypeResolution(
        page_capture_id=page_capture_id,
        status=status,
        page_type=page_types[0],
        reason=reason,
        current_by_source=current_by_source,
        revisions=tuple(revisions),
        adopted_judgement_id=adopted.judgement_id,
        judgement_count=len(records),
        after_seal_count=after_seal_count,
    )


__all__ = [
    "PAGE_TYPE_JUDGEMENT_PROTOCOL_VERSION",
    "PAGE_TYPE_RESOLUTION_PROTOCOL_VERSION",
    "SEALED_FINALIZATION_STATES",
    "SOURCE_LABELS",
    "STATUS_LABELS",
    "JudgementKind",
    "PageTypeJudgement",
    "PageTypeJudgementValidationError",
    "PageTypeResolution",
    "PageTypeRevision",
    "ResolutionStatus",
    "build_judgement_id",
    "canonical_basis_hash",
    "resolve_page_type",
    "source_label",
    "status_label",
]
