"""Web 正式质检不可变覆盖快照。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from services.api.app.domain.page.web_page_catalog import (
    WEB_BUSINESS_PAGE_TYPE_CODES,
    WEB_LEGACY_PAGE_TYPE_CODES,
    WEB_LIFECYCLE_PAGE_TYPE_CODES,
    WEB_V3_BUSINESS_PAGE_TYPE_CODES,
    web_page_type_codes_for_catalog_version,
)
from services.api.app.domain.protocol import freeze_json_mapping, stable_protocol_hash

WEB_PAGE_TYPE_NORMALIZATION_VERSION = "web-page-type-assessment.v4"
WEB_LEGACY_NORMALIZATION_VERSIONS = frozenset(
    {
        "web-page-type-normalization.v1",
        "web-page-type-assessment.v1",
        "web-page-type-assessment.v2",
        "web-page-type-assessment.v3",
    }
)
WEB_LEGACY_CANONICAL_PAGE_TYPES = frozenset(WEB_LEGACY_PAGE_TYPE_CODES)
WEB_V3_CANONICAL_PAGE_TYPES = frozenset(WEB_V3_BUSINESS_PAGE_TYPE_CODES)
WEB_CANONICAL_PAGE_TYPES = frozenset(WEB_BUSINESS_PAGE_TYPE_CODES)
WEB_COVERAGE_STATUSES = frozenset({"complete", "completed_with_gaps", "failed"})
WEB_COVERAGE_SNAPSHOT_V1 = "web-coverage-snapshot.v1"
WEB_COVERAGE_SNAPSHOT_V2 = "web-coverage-snapshot.v2"
WEB_COVERAGE_SNAPSHOT_V3 = "web-coverage-snapshot.v3"
WEB_COVERAGE_SNAPSHOT_V4 = "web-coverage-snapshot.v4"


@dataclass(frozen=True, slots=True)
class WebCoverageSnapshot:
    """探索终态后冻结的 Web 覆盖事实。"""

    snapshot_id: str
    task_id: str
    agent_run_id: str
    finalization_epoch: int
    required_page_types: tuple[str, ...]
    observed_page_types: tuple[str, ...]
    missing_page_types: tuple[str, ...]
    unrecognized_page_types: tuple[str, ...]
    normalization_version: str
    coverage_status: str
    snapshot_hash: str
    created_at: datetime
    optional_page_types: tuple[str, ...] = ()
    not_applicable_page_types: tuple[str, ...] = ()
    applicability_mode: str = "legacy_required_snapshot"
    catalog_version: str = "legacy-frozen"
    catalog_hash: str = "legacy-frozen"
    snapshot_protocol_version: str = WEB_COVERAGE_SNAPSHOT_V1
    observed_lifecycle_page_types: tuple[str, ...] = ()
    unknown_page_capture_ids: tuple[str, ...] = ()
    supplemental_evidence_summary: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "task_id", "agent_run_id"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} 不能为空")
        if isinstance(self.finalization_epoch, bool) or self.finalization_epoch < 0:
            raise ValueError("finalization_epoch 必须为非负整数")
        canonical = _canonical_types(self.normalization_version, self.catalog_version)
        allowed_protocols = _allowed_snapshot_protocols(self.normalization_version)
        if self.snapshot_protocol_version not in allowed_protocols:
            raise ValueError("snapshot_protocol_version 与 normalization_version 不一致")
        if self.coverage_status not in WEB_COVERAGE_STATUSES:
            raise ValueError("coverage_status 不受支持")
        for field_name in (
            "required_page_types",
            "optional_page_types",
            "not_applicable_page_types",
            "observed_page_types",
            "missing_page_types",
        ):
            values = getattr(self, field_name)
            if values != _normalize_canonical_page_types(
                values,
                field_name=field_name,
                canonical=canonical,
            ):
                raise ValueError(f"{field_name} 必须为规范排序后的页面类型集合")
        partitions = (
            set(self.required_page_types),
            set(self.optional_page_types),
            set(self.not_applicable_page_types),
        )
        overlaps = any(
            left & right
            for index, left in enumerate(partitions)
            for right in partitions[index + 1 :]
        )
        if overlaps:
            raise ValueError("Web 页面类型适用性分区不能交叉")
        if set().union(*partitions) != canonical:
            raise ValueError("Web 页面类型适用性分区必须完整覆盖版本内规范页型")
        for field_name in ("applicability_mode", "catalog_version", "catalog_hash"):
            if not str(getattr(self, field_name)).strip():
                raise ValueError(f"{field_name} 不能为空")
        if self.snapshot_protocol_version not in {
            WEB_COVERAGE_SNAPSHOT_V1,
            WEB_COVERAGE_SNAPSHOT_V2,
            WEB_COVERAGE_SNAPSHOT_V3,
            WEB_COVERAGE_SNAPSHOT_V4,
        }:
            raise ValueError("snapshot_protocol_version 不受支持")
        if self.unrecognized_page_types != _normalize_unrecognized_page_types(
            self.unrecognized_page_types
        ):
            raise ValueError("unrecognized_page_types 必须为规范排序后的未知页面类型集合")
        lifecycle = _normalize_lifecycle_page_types(self.observed_lifecycle_page_types)
        unknown_capture_ids = _normalize_identifiers(
            self.unknown_page_capture_ids,
            field_name="unknown_page_capture_ids",
        )
        supplemental = _normalize_supplemental_evidence_summary(
            self.supplemental_evidence_summary
        )
        if self.snapshot_protocol_version != WEB_COVERAGE_SNAPSHOT_V4 and (
            lifecycle or unknown_capture_ids or supplemental
        ):
            raise ValueError("历史 Coverage Snapshot 不能携带 v4 字段")
        if self.observed_lifecycle_page_types != lifecycle:
            raise ValueError("observed_lifecycle_page_types 必须为规范生命周期页型集合")
        if self.unknown_page_capture_ids != unknown_capture_ids:
            raise ValueError("unknown_page_capture_ids 必须为规范排序后的 Capture 集合")
        object.__setattr__(self, "supplemental_evidence_summary", freeze_json_mapping(
            supplemental, "supplemental_evidence_summary"
        ))
        expected_missing = tuple(
            sorted(set(self.required_page_types) - set(self.observed_page_types))
        )
        if self.missing_page_types != expected_missing:
            raise ValueError("missing_page_types 必须由 required-observed 推导")
        expected_status = "completed_with_gaps" if self.missing_page_types else "complete"
        if self.coverage_status != "failed" and self.coverage_status != expected_status:
            raise ValueError("coverage_status 与覆盖事实不一致")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at 必须包含时区")
        expected_hash = stable_protocol_hash(self.hash_material())
        if self.snapshot_hash != expected_hash:
            raise ValueError("snapshot_hash 与覆盖事实不一致")

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        agent_run_id: str,
        finalization_epoch: int,
        required_page_types: Iterable[object],
        observed_page_types: Iterable[object],
        unrecognized_page_types: Iterable[object],
        coverage_status: str | None = None,
        normalization_version: str = WEB_PAGE_TYPE_NORMALIZATION_VERSION,
        snapshot_id: str | None = None,
        created_at: datetime | None = None,
        optional_page_types: Iterable[object] | None = None,
        not_applicable_page_types: Iterable[object] | None = None,
        applicability_mode: str | None = None,
        catalog_version: str | None = None,
        catalog_hash: str | None = None,
        snapshot_protocol_version: str | None = None,
        observed_lifecycle_page_types: Iterable[object] = (),
        unknown_page_capture_ids: Iterable[object] = (),
        supplemental_evidence_summary: Mapping[str, object] | None = None,
    ) -> WebCoverageSnapshot:
        """规范集合并创建快照；显式 ``failed`` 表示已确认的基础故障终态。"""

        canonical = _canonical_types(normalization_version, catalog_version)
        required = _normalize_canonical_page_types(
            required_page_types,
            field_name="required_page_types",
            canonical=canonical,
        )
        observed = _normalize_canonical_page_types(
            observed_page_types,
            field_name="observed_page_types",
            canonical=canonical,
        )
        optional = _normalize_canonical_page_types(
            optional_page_types or (),
            field_name="optional_page_types",
            canonical=canonical,
        )
        resolved_not_applicable = _normalize_canonical_page_types(
            (
                not_applicable_page_types
                if not_applicable_page_types is not None
                else canonical - set(required) - set(optional)
            ),
            field_name="not_applicable_page_types",
            canonical=canonical,
        )
        uses_v2_applicability = any(
            value is not None
            for value in (
                optional_page_types,
                not_applicable_page_types,
                applicability_mode,
                catalog_version,
                catalog_hash,
                snapshot_protocol_version,
            )
        )
        protocol_version = snapshot_protocol_version or (
            WEB_COVERAGE_SNAPSHOT_V4
            if normalization_version == WEB_PAGE_TYPE_NORMALIZATION_VERSION
            else WEB_COVERAGE_SNAPSHOT_V3
            if normalization_version == "web-page-type-assessment.v3"
            else WEB_COVERAGE_SNAPSHOT_V2
            if uses_v2_applicability
            else WEB_COVERAGE_SNAPSHOT_V1
        )
        resolved_mode = applicability_mode or "legacy_required_snapshot"
        resolved_catalog_version = catalog_version or "legacy-frozen"
        resolved_catalog_hash = catalog_hash or "legacy-frozen"
        missing = tuple(sorted(set(required) - set(observed)))
        unrecognized = _normalize_unrecognized_page_types(unrecognized_page_types)
        lifecycle = _normalize_lifecycle_page_types(observed_lifecycle_page_types)
        unknown_capture_ids = _normalize_identifiers(
            unknown_page_capture_ids,
            field_name="unknown_page_capture_ids",
        )
        supplemental = _normalize_supplemental_evidence_summary(
            supplemental_evidence_summary or {}
        )
        derived_status = "completed_with_gaps" if missing else "complete"
        resolved_status = derived_status if coverage_status is None else coverage_status
        if resolved_status not in WEB_COVERAGE_STATUSES:
            raise ValueError("coverage_status 不受支持")
        if resolved_status != "failed" and resolved_status != derived_status:
            raise ValueError("coverage_status 与覆盖事实不一致")
        material = {
            "task_id": str(task_id),
            "agent_run_id": str(agent_run_id),
            "finalization_epoch": finalization_epoch,
            "required_page_types": list(required),
            "observed_page_types": list(observed),
            "missing_page_types": list(missing),
            "unrecognized_page_types": list(unrecognized),
            "normalization_version": normalization_version,
            "coverage_status": resolved_status,
        }
        if protocol_version != WEB_COVERAGE_SNAPSHOT_V1:
            material.update(
                snapshot_protocol_version=protocol_version,
                optional_page_types=list(optional),
                not_applicable_page_types=list(resolved_not_applicable),
                applicability_mode=resolved_mode,
                catalog_version=resolved_catalog_version,
                catalog_hash=resolved_catalog_hash,
            )
        if protocol_version == WEB_COVERAGE_SNAPSHOT_V4:
            material.update(
                observed_lifecycle_page_types=list(lifecycle),
                unknown_page_capture_ids=list(unknown_capture_ids),
                supplemental_evidence_summary=dict(supplemental),
            )
        return cls(
            snapshot_id=snapshot_id or str(uuid4()),
            task_id=str(task_id),
            agent_run_id=str(agent_run_id),
            finalization_epoch=finalization_epoch,
            required_page_types=required,
            observed_page_types=observed,
            missing_page_types=missing,
            unrecognized_page_types=unrecognized,
            normalization_version=normalization_version,
            coverage_status=resolved_status,
            snapshot_hash=stable_protocol_hash(material),
            created_at=created_at or datetime.now(UTC),
            optional_page_types=optional,
            not_applicable_page_types=resolved_not_applicable,
            applicability_mode=resolved_mode,
            catalog_version=resolved_catalog_version,
            catalog_hash=resolved_catalog_hash,
            snapshot_protocol_version=protocol_version,
            observed_lifecycle_page_types=lifecycle,
            unknown_page_capture_ids=unknown_capture_ids,
            supplemental_evidence_summary=supplemental,
        )

    def hash_material(self) -> dict[str, object]:
        """返回稳定哈希覆盖的完整权威事实。"""

        material: dict[str, object] = {
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "finalization_epoch": self.finalization_epoch,
            "required_page_types": list(self.required_page_types),
            "observed_page_types": list(self.observed_page_types),
            "missing_page_types": list(self.missing_page_types),
            "unrecognized_page_types": list(self.unrecognized_page_types),
            "normalization_version": self.normalization_version,
            "coverage_status": self.coverage_status,
        }
        if self.snapshot_protocol_version != WEB_COVERAGE_SNAPSHOT_V1:
            material.update(
                snapshot_protocol_version=self.snapshot_protocol_version,
                optional_page_types=list(self.optional_page_types),
                not_applicable_page_types=list(self.not_applicable_page_types),
                applicability_mode=self.applicability_mode,
                catalog_version=self.catalog_version,
                catalog_hash=self.catalog_hash,
            )
        if self.snapshot_protocol_version == WEB_COVERAGE_SNAPSHOT_V4:
            material.update(
                observed_lifecycle_page_types=list(self.observed_lifecycle_page_types),
                unknown_page_capture_ids=list(self.unknown_page_capture_ids),
                supplemental_evidence_summary=dict(self.supplemental_evidence_summary),
            )
        return material


def _normalize_string_page_types(
    values: Iterable[object],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if isinstance(values, str | bytes):
        raise ValueError(f"{field_name} 必须是字符串集合，不能是单个字符串")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} 只能包含字符串")
        text = value.strip().casefold()
        if text:
            normalized.add(text)
    return tuple(sorted(normalized))


def _canonical_types(
    normalization_version: str, catalog_version: str | None = None,
) -> frozenset[str]:
    """按冻结协议选择页型分母，历史快照不得被新目录重新解释。"""
    if normalization_version == WEB_PAGE_TYPE_NORMALIZATION_VERSION:
        if catalog_version and ".web.v" in catalog_version and not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}\.web\.v[1-7]", catalog_version,
        ):
            # 新目录按已冻结版本解析；生命周期与补充证据仍不进入业务分母。
            return WEB_CANONICAL_PAGE_TYPES & frozenset(
                web_page_type_codes_for_catalog_version(catalog_version)
            )
        return WEB_CANONICAL_PAGE_TYPES - {"collection_page"}
    if normalization_version == "web-page-type-assessment.v3":
        return WEB_V3_CANONICAL_PAGE_TYPES
    if normalization_version in WEB_LEGACY_NORMALIZATION_VERSIONS:
        return WEB_LEGACY_CANONICAL_PAGE_TYPES
    raise ValueError("normalization_version 不受支持")


def _normalize_identifiers(
    values: Iterable[object],
    *,
    field_name: str,
) -> tuple[str, ...]:
    if isinstance(values, str | bytes):
        raise ValueError(f"{field_name} 必须是字符串集合，不能是单个字符串")
    normalized: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} 只能包含字符串")
        text = value.strip()
        if text:
            normalized.add(text)
    return tuple(sorted(normalized))


def _normalize_canonical_page_types(
    values: Iterable[object],
    *,
    field_name: str,
    canonical: frozenset[str] = WEB_CANONICAL_PAGE_TYPES,
) -> tuple[str, ...]:
    normalized = _normalize_string_page_types(values, field_name=field_name)
    unsupported = set(normalized) - canonical
    if unsupported:
        raise ValueError(f"{field_name} 包含非 Web 标准页型: {', '.join(sorted(unsupported))}")
    return normalized


def _normalize_unrecognized_page_types(
    values: Iterable[object],
) -> tuple[str, ...]:
    return _normalize_string_page_types(
        values,
        field_name="unrecognized_page_types",
    )


def _allowed_snapshot_protocols(normalization_version: str) -> frozenset[str]:
    if normalization_version == WEB_PAGE_TYPE_NORMALIZATION_VERSION:
        return frozenset({WEB_COVERAGE_SNAPSHOT_V4})
    if normalization_version == "web-page-type-assessment.v3":
        return frozenset({WEB_COVERAGE_SNAPSHOT_V3})
    return frozenset({WEB_COVERAGE_SNAPSHOT_V1, WEB_COVERAGE_SNAPSHOT_V2})


def _normalize_lifecycle_page_types(values: Iterable[object]) -> tuple[str, ...]:
    return _normalize_canonical_page_types(
        values,
        field_name="observed_lifecycle_page_types",
        canonical=frozenset(WEB_LIFECYCLE_PAGE_TYPE_CODES),
    )


def _normalize_supplemental_evidence_summary(
    value: Mapping[str, object],
) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise ValueError("supplemental_evidence_summary 必须为对象")
    normalized: dict[str, int] = {}
    for key, count in value.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError("supplemental_evidence_summary 键必须为规范字符串")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("supplemental_evidence_summary 计数必须为非负整数")
        normalized[key] = count
    return dict(sorted(normalized.items()))
