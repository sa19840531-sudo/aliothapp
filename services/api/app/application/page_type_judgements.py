"""页面类型判断台账：从识别写库与网页评估收集每一份判断，读侧按页面裁定。

只追加、不改正式识别结果（04 专项 B 期影子口径）：正式页型仍由各端识别链路写
qc_page_capture.page_type，这里把结构规则、模型、网页评估各自的判断分开留档，冲突和改判
在工作台看得见。记录失败只告警，不能挡采集与识别。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from services.api.app.domain.page.type_judgement import (
    JudgementKind,
    PageTypeJudgement,
    PageTypeResolution,
    resolve_page_type,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PageTypeJudgementInput:
    """一份待记录的判断。"""

    task_id: str
    page_capture_id: str
    source: str
    page_type: str | None
    agent_run_id: str | None = None
    page_state: str = ""
    confidence: float | None = None
    catalog_version: str = ""
    basis: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("task_id", "page_capture_id", "source"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"判断记录缺少 {name}")
        page_type = str(self.page_type).strip() if self.page_type is not None else None
        object.__setattr__(self, "page_type", page_type or None)
        if self.confidence is not None:
            value = float(self.confidence)
            object.__setattr__(self, "confidence", min(max(value, 0.0), 1.0))
        object.__setattr__(self, "basis", dict(self.basis))


class PageTypeJudgementRepository(Protocol):
    async def record(
        self, *, inputs: Sequence[PageTypeJudgementInput]
    ) -> Sequence[PageTypeJudgement]: ...

    async def list_for_task(
        self, *, task_id: str, agent_run_id: str | None = None
    ) -> Sequence[PageTypeJudgement]: ...


def source_from_confidence_source(confidence_source: str | None) -> str:
    """识别写库带的 confidence_source → 判断来源：确定性结构规则 / 模型 / 其它识别结果。"""

    text = str(confidence_source or "").strip().lower()
    if text.startswith("deterministic"):
        return "structure_rules"
    if text == "model":
        return "model"
    return "detection"


def h5_detection_judgements(
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    page_type: str,
    page_state: str,
    detection_status: str,
    confidence: float,
    catalog_version: str,
    catalog_sha256: str = "",
    confidence_source: str | None = None,
    model_page_type: str | None = None,
    model_confidence: float | None = None,
) -> tuple[PageTypeJudgementInput, ...]:
    """一次识别写库拆成各自的判断：采纳的结果一条，模型另有看法时再一条。"""

    adopted_source = source_from_confidence_source(confidence_source)
    inputs = [
        PageTypeJudgementInput(
            task_id=task_id,
            agent_run_id=agent_run_id,
            page_capture_id=page_capture_id,
            source=adopted_source,
            page_type=page_type,
            page_state=page_state,
            confidence=confidence,
            catalog_version=catalog_version,
            basis={
                "detection_status": detection_status,
                "confidence_source": str(confidence_source or "unavailable"),
                "catalog_sha256": catalog_sha256,
                "adopted_by_detection": True,
            },
        )
    ]
    model_type = str(model_page_type or "").strip()
    if model_type and adopted_source != "model":
        inputs.append(
            PageTypeJudgementInput(
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=page_capture_id,
                source="model",
                page_type=model_type,
                page_state=page_state,
                confidence=model_confidence,
                catalog_version=catalog_version,
                basis={
                    "detection_status": detection_status,
                    "adopted_by_detection": model_type == page_type,
                },
            )
        )
    return tuple(inputs)


def web_assessment_judgements(
    *,
    task_id: str,
    agent_run_id: str,
    page_capture_id: str,
    proposed_page_type: str | None,
    observed_page_type: str | None,
    final_page_type: str | None,
    classification_status: str,
    confidence: float | None,
    reason_code: str | None = None,
    conflict_reason_code: str | None = None,
    expected_page_type: str | None = None,
    evidence_artifact_ids: Sequence[str | None] = (),
) -> tuple[PageTypeJudgementInput, ...]:
    """网页页型评估拆成模型判断与观察判断；期望页型只是弱先验，只留在依据里不单独成判断。"""

    shared_basis = {
        "classification_status": classification_status,
        "final_page_type": final_page_type,
        "reason_code": reason_code,
        "conflict_reason_code": conflict_reason_code,
        "expected_page_type": expected_page_type,
        "evidence_artifact_ids": [item for item in evidence_artifact_ids if item],
    }
    inputs = [
        PageTypeJudgementInput(
            task_id=task_id,
            agent_run_id=agent_run_id,
            page_capture_id=page_capture_id,
            source="web_assessment_model",
            page_type=proposed_page_type,
            confidence=confidence,
            basis={**shared_basis, "adopted_by_assessment": final_page_type is not None},
        )
    ]
    observed = str(observed_page_type or "").strip()
    if observed:
        inputs.append(
            PageTypeJudgementInput(
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=page_capture_id,
                source="web_assessment_observed",
                page_type=observed,
                basis={**shared_basis, "adopted_by_assessment": observed == final_page_type},
            )
        )
    return tuple(inputs)


@dataclass(frozen=True)
class PageTypeJudgementPage:
    """一个页面的全部判断与裁定。"""

    page_capture_id: str
    judgements: tuple[PageTypeJudgement, ...]
    resolution: PageTypeResolution


class PageTypeJudgementLedger:
    """判断台账：记录失败只告警，读侧按页面裁定。"""

    def __init__(self, *, repository: PageTypeJudgementRepository) -> None:
        self._repository = repository

    async def record(
        self, inputs: Sequence[PageTypeJudgementInput]
    ) -> tuple[PageTypeJudgement, ...]:
        if not inputs:
            return ()
        try:
            return tuple(await self._repository.record(inputs=tuple(inputs)))
        except Exception as exc:  # noqa: BLE001 - 台账只是记录，不能挡识别与采集
            logger.warning(
                "task_id=%s page_capture_id=%s 页型判断记录失败，正式识别不受影响：%s",
                inputs[0].task_id,
                inputs[0].page_capture_id,
                exc,
            )
            return ()

    async def record_web_assessment(self, **kwargs: Any) -> tuple[PageTypeJudgement, ...]:
        return await self.record(web_assessment_judgements(**kwargs))

    async def pages_for_task(
        self, *, task_id: str, agent_run_id: str | None = None
    ) -> tuple[PageTypeJudgementPage, ...]:
        judgements = tuple(
            await self._repository.list_for_task(task_id=task_id, agent_run_id=agent_run_id)
        )
        return group_judgements(judgements)


def judgement_from_mapping(row: Mapping[str, Any]) -> PageTypeJudgement:
    """把仓储查出来的一行转成判断记录；basis 可能是 jsonb 字符串。"""

    basis = row.get("basis")
    if isinstance(basis, str):
        try:
            basis = json.loads(basis)
        except json.JSONDecodeError:
            basis = {}
    confidence = row.get("confidence")
    return PageTypeJudgement(
        judgement_id=str(row["judgement_id"]),
        task_id=str(row["task_id"]),
        agent_run_id=str(row["agent_run_id"]) if row.get("agent_run_id") else None,
        page_capture_id=str(row["page_capture_id"]),
        sequence_no=int(row["sequence_no"]),
        source=str(row["source"]),
        page_type=row.get("page_type"),
        page_state=str(row.get("page_state") or ""),
        confidence=float(confidence) if confidence is not None else None,
        catalog_version=str(row.get("catalog_version") or ""),
        kind=JudgementKind(str(row.get("kind") or "judgement")),
        revises_judgement_id=row.get("revises_judgement_id"),
        basis=basis if isinstance(basis, dict) else {},
        task_finalization_state=str(row.get("task_finalization_state") or ""),
        created_at=row.get("created_at"),
    )


def group_judgements(
    judgements: Sequence[PageTypeJudgement],
) -> tuple[PageTypeJudgementPage, ...]:
    """按页面分组并裁定；页面顺序按首次判断的先后。"""

    ordered: dict[str, list[PageTypeJudgement]] = {}
    ordered_items = sorted(
        judgements,
        key=lambda item: (item.created_at is None, item.created_at or 0, item.sequence_no),
    )
    for item in ordered_items:
        ordered.setdefault(item.page_capture_id, []).append(item)
    pages = []
    for page_capture_id, items in ordered.items():
        records = tuple(sorted(items, key=lambda item: item.sequence_no))
        pages.append(
            PageTypeJudgementPage(
                page_capture_id=page_capture_id,
                judgements=records,
                resolution=resolve_page_type(page_capture_id, records),
            )
        )
    return tuple(pages)


__all__ = [
    "PageTypeJudgementInput",
    "PageTypeJudgementLedger",
    "PageTypeJudgementPage",
    "PageTypeJudgementRepository",
    "group_judgements",
    "h5_detection_judgements",
    "judgement_from_mapping",
    "source_from_confidence_source",
    "web_assessment_judgements",
]
