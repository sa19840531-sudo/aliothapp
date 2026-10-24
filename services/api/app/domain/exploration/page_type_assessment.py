"""产品业务页面类型评估的稳定合同。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from services.api.app.domain.page.registry import (
    PageTypeRegistry,
    get_default_page_type_registry,
)
from services.api.app.domain.protocol import (
    ProtocolContractError,
    parse_aware_datetime,
    read_known_fields,
    require_aware_datetime,
    require_sequence,
    require_text,
    stable_protocol_hash,
)

PAGE_TYPE_ASSESSMENT_PROTOCOL_VERSION = "page-type-assessment.v1"
PAGE_TYPE_ASSESSMENT_SET_PROTOCOL_VERSION = "page-type-assessment-set.v1"


class PageTypeAssessmentValidationError(ValueError):
    """页面类型评估缺少稳定身份、Catalog 约束或可审计引用。"""


class PageTypeAssessmentStatus(StrEnum):
    """产品业务页面类型适用性评估状态。"""

    COVERED = "covered"
    REQUIRES_AUTH = "requires_auth"
    BLOCKED_BY_SAFETY = "blocked_by_safety"
    NOT_FOUND = "not_found"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    NOT_APPLICABLE = "not_applicable"


_REFERENCE_REQUIRED_STATUSES = frozenset(
    {
        PageTypeAssessmentStatus.COVERED,
        PageTypeAssessmentStatus.REQUIRES_AUTH,
        PageTypeAssessmentStatus.BLOCKED_BY_SAFETY,
        PageTypeAssessmentStatus.NOT_APPLICABLE,
    }
)


@dataclass(frozen=True, kw_only=True)
class PageTypeAssessment:
    """单个产品业务页面类型在一次 Agent Run 内的适用性评估事实。"""

    assessment_id: str
    task_id: str
    agent_run_id: str
    page_type: str
    status: PageTypeAssessmentStatus
    reason_code: str
    catalog_version: str
    catalog_sha256: str
    assessed_at: datetime
    page_capture_ids: tuple[str, ...] = field(default_factory=tuple)
    evidence_artifact_ids: tuple[str, ...] = field(default_factory=tuple)
    exploration_action_ids: tuple[str, ...] = field(default_factory=tuple)
    page_states: tuple[str, ...] = field(default_factory=tuple)
    protocol_version: str = PAGE_TYPE_ASSESSMENT_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "assessment_id",
            "task_id",
            "agent_run_id",
            "page_type",
            "reason_code",
            "catalog_version",
            "catalog_sha256",
        ):
            require_text(getattr(self, field_name), field_name)
        if self.protocol_version != PAGE_TYPE_ASSESSMENT_PROTOCOL_VERSION:
            raise PageTypeAssessmentValidationError(
                "PageTypeAssessment protocol_version 不受支持"
            )
        if not isinstance(self.status, PageTypeAssessmentStatus):
            raise PageTypeAssessmentValidationError("status 不受支持")
        require_aware_datetime(self.assessed_at, "assessed_at")
        for field_name in (
            "page_capture_ids",
            "evidence_artifact_ids",
            "exploration_action_ids",
            "page_states",
        ):
            object.__setattr__(
                self,
                field_name,
                _deduplicated_texts(getattr(self, field_name), field_name),
            )
        self._validate_auditable_reference()
        expected = build_page_type_assessment_id(
            task_id=self.task_id,
            agent_run_id=self.agent_run_id,
            page_type=self.page_type,
            catalog_sha256=self.catalog_sha256,
        )
        if self.assessment_id != expected:
            raise PageTypeAssessmentValidationError(
                "assessment_id 与 task_id、agent_run_id、page_type、catalog_sha256 不一致"
            )

    def to_dict(self) -> dict[str, Any]:
        """返回可写入 JSONB、WorkingMemory 或消息队列的纯数据。"""
        return {
            "protocol_version": self.protocol_version,
            "assessment_id": self.assessment_id,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "page_type": self.page_type,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "catalog_version": self.catalog_version,
            "catalog_sha256": self.catalog_sha256,
            "assessed_at": self.assessed_at.isoformat(),
            "page_capture_ids": list(self.page_capture_ids),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "exploration_action_ids": list(self.exploration_action_ids),
            "page_states": list(self.page_states),
        }

    def to_tool_payload(self) -> dict[str, Any]:
        """转换为当前 exploration_closure_tool 兼容的入参片段。"""
        return {
            "page_type": self.page_type,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "page_capture_ids": list(self.page_capture_ids),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "exploration_action_ids": list(self.exploration_action_ids),
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        registry: PageTypeRegistry | None = None,
    ) -> PageTypeAssessment:
        """兼容解析当前合同并忽略未来新增字段。"""
        values = _read_known(
            payload,
            known={
                "protocol_version",
                "assessment_id",
                "task_id",
                "agent_run_id",
                "page_type",
                "status",
                "reason_code",
                "catalog_version",
                "catalog_sha256",
                "assessed_at",
                "page_capture_ids",
                "evidence_artifact_ids",
                "exploration_action_ids",
                "page_states",
            },
            required={
                "protocol_version",
                "assessment_id",
                "task_id",
                "agent_run_id",
                "page_type",
                "status",
                "reason_code",
                "catalog_version",
                "catalog_sha256",
                "assessed_at",
            },
            field_name="PageTypeAssessment",
        )
        try:
            status = PageTypeAssessmentStatus(values["status"])
            assessed_at = parse_aware_datetime(values["assessed_at"], "assessed_at")
            page_capture_ids = _tuple_field(values, "page_capture_ids")
            evidence_artifact_ids = _tuple_field(values, "evidence_artifact_ids")
            exploration_action_ids = _tuple_field(values, "exploration_action_ids")
            page_states = _tuple_field(values, "page_states")
        except (ProtocolContractError, ValueError) as exc:
            raise PageTypeAssessmentValidationError(str(exc)) from exc
        assessment = cls(
            assessment_id=values["assessment_id"],
            task_id=values["task_id"],
            agent_run_id=values["agent_run_id"],
            page_type=values["page_type"],
            status=status,
            reason_code=values["reason_code"],
            catalog_version=values["catalog_version"],
            catalog_sha256=values["catalog_sha256"],
            assessed_at=assessed_at,
            page_capture_ids=page_capture_ids,
            evidence_artifact_ids=evidence_artifact_ids,
            exploration_action_ids=exploration_action_ids,
            page_states=page_states,
            protocol_version=values["protocol_version"],
        )
        _validate_catalog_page_type(
            assessment.page_type,
            registry or get_default_page_type_registry(),
        )
        return assessment

    def _validate_auditable_reference(self) -> None:
        if self.status is PageTypeAssessmentStatus.COVERED:
            if not self.page_capture_ids or not self.evidence_artifact_ids:
                raise PageTypeAssessmentValidationError(
                    "covered 评估必须引用 page_capture_ids 与 evidence_artifact_ids"
                )
            if not self.page_states:
                raise PageTypeAssessmentValidationError("covered 评估必须记录 page_states")
        elif self.status in _REFERENCE_REQUIRED_STATUSES and not (
            self.page_capture_ids
            or self.evidence_artifact_ids
            or self.exploration_action_ids
        ):
            raise PageTypeAssessmentValidationError(
                f"{self.status.value} 评估必须引用可审计页面、证据或探索动作"
            )


@dataclass(frozen=True, kw_only=True)
class PageTypeAssessmentSet:
    """一次 Agent Run 对产品业务页面类型范围的完整评估合同。"""

    task_id: str
    agent_run_id: str
    catalog_version: str
    catalog_sha256: str
    assessments: tuple[PageTypeAssessment, ...]
    assessment_scope_complete: bool
    required_page_types: tuple[str, ...] = ()
    protocol_version: str = PAGE_TYPE_ASSESSMENT_SET_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "catalog_version", "catalog_sha256"):
            require_text(getattr(self, field_name), field_name)
        if self.protocol_version != PAGE_TYPE_ASSESSMENT_SET_PROTOCOL_VERSION:
            raise PageTypeAssessmentValidationError(
                "PageTypeAssessmentSet protocol_version 不受支持"
            )
        if not isinstance(self.assessment_scope_complete, bool):
            raise PageTypeAssessmentValidationError("assessment_scope_complete 必须为布尔值")
        if not self.assessments:
            raise PageTypeAssessmentValidationError("assessments 不能为空")
        page_types: list[str] = []
        for assessment in self.assessments:
            if not isinstance(assessment, PageTypeAssessment):
                raise PageTypeAssessmentValidationError(
                    "assessments 必须全部为 PageTypeAssessment"
                )
            if assessment.task_id != self.task_id or assessment.agent_run_id != self.agent_run_id:
                raise PageTypeAssessmentValidationError("assessments 与评估范围任务不一致")
            if (
                assessment.catalog_version != self.catalog_version
                or assessment.catalog_sha256 != self.catalog_sha256
            ):
                raise PageTypeAssessmentValidationError("assessments 与 Catalog 快照不一致")
            page_types.append(assessment.page_type)
        duplicates = sorted(
            {page_type for page_type in page_types if page_types.count(page_type) > 1}
        )
        if duplicates:
            raise PageTypeAssessmentValidationError(
                f"页面类型评估重复：{', '.join(duplicates)}"
            )

        required = tuple(dict.fromkeys(self.required_page_types or tuple(page_types)))
        if not required:
            raise PageTypeAssessmentValidationError("required_page_types 不能为空")
        object.__setattr__(self, "required_page_types", required)

    def validate_scope(self, *, registry: PageTypeRegistry | None = None) -> None:
        """校验完整评估覆盖本产品锁定或动态发现的业务页面范围。"""
        resolved = registry or get_default_page_type_registry()
        allowed = set(resolved.coverage_required_codes)
        submitted = {assessment.page_type for assessment in self.assessments}
        required = set(self.required_page_types)
        invalid = sorted((submitted | required) - allowed)
        if invalid:
            raise PageTypeAssessmentValidationError(
                f"页面类型不属于业务覆盖 Catalog：{', '.join(invalid)}"
            )
        missing = [
            page_type
            for page_type in self.required_page_types
            if page_type not in submitted
        ]
        if self.assessment_scope_complete and missing:
            raise PageTypeAssessmentValidationError(
                f"页面类型评估范围不完整：{', '.join(missing)}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "catalog_version": self.catalog_version,
            "catalog_sha256": self.catalog_sha256,
            "assessment_scope_complete": self.assessment_scope_complete,
            "required_page_types": list(self.required_page_types),
            "assessments": [assessment.to_dict() for assessment in self.assessments],
        }

    def to_tool_payloads(self) -> list[dict[str, Any]]:
        """返回 exploration_closure_tool 当前消费的轻量列表。"""
        return [assessment.to_tool_payload() for assessment in self.assessments]

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, Any],
        *,
        registry: PageTypeRegistry | None = None,
    ) -> PageTypeAssessmentSet:
        values = _read_known(
            payload,
            known={
                "protocol_version",
                "task_id",
                "agent_run_id",
                "catalog_version",
                "catalog_sha256",
                "assessment_scope_complete",
                "required_page_types",
                "assessments",
            },
            required={
                "protocol_version",
                "task_id",
                "agent_run_id",
                "catalog_version",
                "catalog_sha256",
                "assessment_scope_complete",
                "assessments",
            },
            field_name="PageTypeAssessmentSet",
        )
        assessments = tuple(
            PageTypeAssessment.from_dict(item, registry=registry)
            for item in require_sequence(values["assessments"], "assessments")
        )
        assessment_set = cls(
            task_id=values["task_id"],
            agent_run_id=values["agent_run_id"],
            catalog_version=values["catalog_version"],
            catalog_sha256=values["catalog_sha256"],
            assessments=assessments,
            assessment_scope_complete=values["assessment_scope_complete"],
            required_page_types=tuple(
                require_sequence(
                    values.get("required_page_types")
                    or [assessment.page_type for assessment in assessments],
                    "required_page_types",
                )
            ),
            protocol_version=values["protocol_version"],
        )
        assessment_set.validate_scope(registry=registry)
        return assessment_set


def build_page_type_assessment(
    *,
    task_id: str,
    agent_run_id: str,
    page_type: str,
    status: PageTypeAssessmentStatus | str,
    reason_code: str,
    assessed_at: datetime,
    page_capture_ids: tuple[str, ...] | list[str] = (),
    evidence_artifact_ids: tuple[str, ...] | list[str] = (),
    exploration_action_ids: tuple[str, ...] | list[str] = (),
    page_states: tuple[str, ...] | list[str] = (),
    registry: PageTypeRegistry | None = None,
) -> PageTypeAssessment:
    """按 Catalog 快照生成幂等评估事实。"""
    resolved = registry or get_default_page_type_registry()
    catalog_sha256 = resolved.content_sha256
    normalized_page_type = require_text(page_type, "page_type")
    _validate_catalog_page_type(normalized_page_type, resolved)
    assessment_status = (
        status if isinstance(status, PageTypeAssessmentStatus) else PageTypeAssessmentStatus(status)
    )
    return PageTypeAssessment(
        assessment_id=build_page_type_assessment_id(
            task_id=task_id,
            agent_run_id=agent_run_id,
            page_type=normalized_page_type,
            catalog_sha256=catalog_sha256,
        ),
        task_id=task_id,
        agent_run_id=agent_run_id,
        page_type=normalized_page_type,
        status=assessment_status,
        reason_code=reason_code,
        catalog_version=resolved.catalog.version,
        catalog_sha256=catalog_sha256,
        assessed_at=assessed_at,
        page_capture_ids=tuple(page_capture_ids),
        evidence_artifact_ids=tuple(evidence_artifact_ids),
        exploration_action_ids=tuple(exploration_action_ids),
        page_states=tuple(page_states),
    )


def build_page_type_assessment_set(
    *,
    task_id: str,
    agent_run_id: str,
    assessments: tuple[PageTypeAssessment, ...] | list[PageTypeAssessment],
    assessment_scope_complete: bool,
    required_page_types: tuple[str, ...] | list[str] = (),
    registry: PageTypeRegistry | None = None,
) -> PageTypeAssessmentSet:
    """构建一次 Run 的页面类型评估范围合同。"""
    resolved = registry or get_default_page_type_registry()
    assessment_set = PageTypeAssessmentSet(
        task_id=task_id,
        agent_run_id=agent_run_id,
        catalog_version=resolved.catalog.version,
        catalog_sha256=resolved.content_sha256,
        assessments=tuple(assessments),
        assessment_scope_complete=assessment_scope_complete,
        required_page_types=tuple(required_page_types),
    )
    assessment_set.validate_scope(registry=resolved)
    return assessment_set


def build_page_type_assessment_id(
    *,
    task_id: str,
    agent_run_id: str,
    page_type: str,
    catalog_sha256: str,
) -> str:
    """生成同一 Run、同一 Catalog、同一页面类型的幂等评估 ID。"""
    identity = {
        "protocol_version": PAGE_TYPE_ASSESSMENT_PROTOCOL_VERSION,
        "task_id": require_text(task_id, "task_id"),
        "agent_run_id": require_text(agent_run_id, "agent_run_id"),
        "page_type": require_text(page_type, "page_type"),
        "catalog_sha256": require_text(catalog_sha256, "catalog_sha256"),
    }
    return f"page-type-assessment:{stable_protocol_hash(identity)}"


def _validate_catalog_page_type(page_type: str, registry: PageTypeRegistry | None) -> None:
    if registry is None:
        return
    if page_type not in registry.coverage_required_codes:
        raise PageTypeAssessmentValidationError(
            f"页面类型不属于业务覆盖 Catalog：{page_type}"
        )


def _read_known(
    payload: Mapping[str, Any],
    *,
    known: set[str],
    required: set[str],
    field_name: str,
) -> dict[str, Any]:
    try:
        return read_known_fields(
            payload,
            known_fields=frozenset(known),
            required_fields=frozenset(required),
            field_name=field_name,
        )
    except ProtocolContractError as exc:
        raise PageTypeAssessmentValidationError(str(exc)) from exc


def _tuple_field(values: Mapping[str, Any], field_name: str) -> tuple[str, ...]:
    raw_value = values.get(field_name, ())
    if raw_value is None:
        return ()
    return tuple(require_sequence(raw_value, field_name))


def _deduplicated_texts(value: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    items = require_sequence(value, field_name)
    normalized = tuple(require_text(item, field_name) for item in items)
    if len(set(normalized)) != len(normalized):
        raise PageTypeAssessmentValidationError(f"{field_name} 不能包含重复值")
    return normalized
