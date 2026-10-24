"""H5 页面覆盖范围的版本化、可持久化事实。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.api.app.domain.exploration.h5_frontier import (
    H5TaskFrontierSnapshot,
    canonical_h5_route_identity,
    h5_frontier_completion_blockers_are_explainable,
)
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import stable_protocol_hash

H5_COVERAGE_METADATA_KEY = "h5_coverage_snapshot"
H5_COVERAGE_SCHEMA_VERSION = "h5-coverage-snapshot.v1"


@dataclass(frozen=True, slots=True)
class H5CoverageSnapshot:
    """运行时、完成门禁和 Task Insight 共同读取的 H5 覆盖快照。"""

    revision: int
    required_page_types: tuple[str, ...]
    covered_page_types: tuple[str, ...]
    missing_page_types: tuple[str, ...]
    scopes: tuple[Mapping[str, object], ...]
    assessment_scope_ready: bool
    frontier_terminal: bool
    completion_ready: bool
    authority_source: str = "h5_page_type_assessment"
    schema_version: str = H5_COVERAGE_SCHEMA_VERSION
    payload_hash: str = ""

    def __post_init__(self) -> None:
        if self.revision < 1:
            raise ValueError("H5 coverage revision 必须大于 0")
        if self.schema_version != H5_COVERAGE_SCHEMA_VERSION:
            raise ValueError("H5 coverage schema_version 不受支持")
        if self.authority_source != "h5_page_type_assessment":
            raise ValueError("H5 coverage authority_source 不受支持")
        expected = stable_protocol_hash(self._hash_material())
        if self.payload_hash != expected:
            raise ValueError("H5 coverage payload_hash 与内容不一致")

    @property
    def target_coverage_ready(self) -> bool:
        """任务锁定的 H5 业务页面类型已全部完成适用性评估。"""
        return (
            bool(self.required_page_types)
            and self.assessment_scope_ready
            and not self.missing_page_types
        )

    @classmethod
    def advance(
        cls,
        *,
        previous: Mapping[str, object] | None,
        frontier: H5TaskFrontierSnapshot,
        location: str,
        capture_id: str,
        page_type: str | None,
        page_state: str,
        assessment: Mapping[str, object],
        requires_auth: bool,
        overlay_present: bool,
    ) -> H5CoverageSnapshot:
        prior = cls.from_dict(previous) if previous is not None else None
        scopes: dict[str, dict[str, object]] = {
            str(scope["scope_id"]): dict(scope) for scope in (prior.scopes if prior else ())
        }
        execution_status = str(assessment.get("execution_status") or "blocked")
        assessment_payload = assessment.get("page_type_assessment")
        assessment_body = (
            dict(assessment_payload) if isinstance(assessment_payload, Mapping) else {}
        )
        assessment_status = str(
            assessment_body.get("status")
            or ("not_applicable" if execution_status == "skipped" else "incomplete")
        )
        normalized_page_type = (page_type or "unknown").strip().casefold()
        route_identity = canonical_h5_route_identity(location)
        scope_id = "h5-scope:" + stable_protocol_hash(
            {
                "route_identity": route_identity,
                "page_type": normalized_page_type,
                "page_state": page_state.casefold(),
            }
        )
        candidate_scope = {
            "scope_id": scope_id,
            "route_identity": route_identity,
            "page_type": normalized_page_type,
            "page_state": page_state,
            "page_capture_id": capture_id,
            "assessment_status": assessment_status,
            "assessment_hash": stable_protocol_hash(dict(assessment)),
        }
        current_scope = scopes.get(scope_id)
        if current_scope is None or _stronger_scope(candidate_scope, current=current_scope):
            scopes[scope_id] = candidate_scope
        ordered_scopes = tuple(scopes[key] for key in sorted(scopes))
        configured_required = tuple(
            sorted(
                {
                    str(value).strip().casefold()
                    for value in frontier.sampling_policy_summary.get("include_page_types", ())
                    if (
                        isinstance(value, str)
                        and value.strip()
                        and value.strip().casefold() in _business_page_type_codes()
                    )
                }
            )
        )
        required = configured_required or tuple(
            sorted(
                {
                    str(scope["page_type"]).strip().casefold()
                    for scope in ordered_scopes
                    if str(scope["page_type"]).strip().casefold() in _business_page_type_codes()
                }
            )
        )
        counted_page_types = _counted_page_types(
            frontier.sampling_policy_summary.get("include_page_types", ())
        )
        covered = tuple(
            sorted(
                {
                    str(scope["page_type"]).strip().casefold()
                    for scope in ordered_scopes
                    if (
                        scope.get("assessment_status") == "covered"
                        and str(scope["page_type"]).strip().casefold() in counted_page_types
                    )
                }
            )
        )
        not_applicable = {
            str(scope["page_type"]).strip().casefold()
            for scope in ordered_scopes
            if (
                scope.get("assessment_status") == "not_applicable"
                and str(scope["page_type"]).strip().casefold() in counted_page_types
            )
        }
        missing = tuple(sorted(set(required) - set(covered) - not_applicable))
        assessment_ready = _assessment_scope_ready(ordered_scopes, counted_page_types)
        frontier_terminal = _frontier_is_terminal(
            frontier,
            requires_auth=requires_auth,
            overlay_present=overlay_present,
        )
        values = {
            "revision": 1,
            "required_page_types": required,
            "covered_page_types": covered,
            "missing_page_types": missing,
            "scopes": ordered_scopes,
            "assessment_scope_ready": assessment_ready,
            "frontier_terminal": frontier_terminal,
            "completion_ready": (
                bool(required)
                and frontier_terminal
                and assessment_ready
                and not missing
                and h5_frontier_completion_blockers_are_explainable(frontier)
            ),
            "authority_source": "h5_page_type_assessment",
            "schema_version": H5_COVERAGE_SCHEMA_VERSION,
        }
        content_hash = stable_protocol_hash(
            {key: value for key, value in values.items() if key != "revision"}
        )
        prior_content_hash = (
            stable_protocol_hash(
                {
                    key: value
                    for key, value in prior.to_dict().items()
                    if key not in {"revision", "payload_hash"}
                }
            )
            if prior is not None
            else None
        )
        values["revision"] = (
            prior.revision
            if prior is not None and content_hash == prior_content_hash
            else (prior.revision + 1 if prior else 1)
        )
        values["payload_hash"] = stable_protocol_hash(
            {key: value for key, value in values.items() if key != "payload_hash"}
        )
        return cls(**values)

    def _hash_material(self) -> dict[str, object]:
        return {key: value for key, value in self.to_dict().items() if key != "payload_hash"}

    def reconcile_frontier(
        self,
        *,
        frontier: H5TaskFrontierSnapshot,
        requires_auth: bool,
        overlay_present: bool,
        completion_allowed: bool = True,
    ) -> H5CoverageSnapshot:
        """恢复/失败路径只改变 Frontier 时，仍推进同一覆盖 revision。"""
        terminal = _frontier_is_terminal(
            frontier,
            requires_auth=requires_auth,
            overlay_present=overlay_present,
        )
        business_codes = _business_page_type_codes()
        required = tuple(
            sorted(
                {
                    value.casefold()
                    for value in self.required_page_types
                    if value.casefold() in business_codes
                }
            )
        )
        counted_page_types = _counted_page_types(
            frontier.sampling_policy_summary.get("include_page_types", ())
        )
        covered = tuple(
            sorted(
                {
                    str(scope["page_type"]).strip().casefold()
                    for scope in self.scopes
                    if (
                        scope.get("assessment_status") == "covered"
                        and str(scope["page_type"]).strip().casefold() in counted_page_types
                    )
                }
            )
        )
        not_applicable = {
            str(scope["page_type"]).strip().casefold()
            for scope in self.scopes
            if (
                scope.get("assessment_status") == "not_applicable"
                and str(scope["page_type"]).strip().casefold() in counted_page_types
            )
        }
        missing = tuple(sorted(set(required) - set(covered) - not_applicable))
        assessment_ready = _assessment_scope_ready(self.scopes, counted_page_types)
        completion = (
            completion_allowed
            and bool(required)
            and terminal
            and assessment_ready
            and not missing
            and h5_frontier_completion_blockers_are_explainable(frontier)
        )
        if (
            terminal == self.frontier_terminal
            and completion == self.completion_ready
            and assessment_ready == self.assessment_scope_ready
            and required == self.required_page_types
            and covered == self.covered_page_types
            and missing == self.missing_page_types
        ):
            return self
        values = self.to_dict()
        values.update(
            revision=self.revision + 1,
            required_page_types=list(required),
            covered_page_types=list(covered),
            missing_page_types=list(missing),
            assessment_scope_ready=assessment_ready,
            frontier_terminal=terminal,
            completion_ready=completion,
        )
        values["payload_hash"] = stable_protocol_hash(
            {key: value for key, value in values.items() if key != "payload_hash"}
        )
        return H5CoverageSnapshot.from_dict(values)

    def promote_assessment(
        self,
        *,
        location: str,
        capture_id: str,
        page_type: str,
        page_state: str,
        assessment: Mapping[str, object],
        detection_confidence: float,
        completion_allowed: bool,
        include_page_types: Sequence[str] = (),
    ) -> H5CoverageSnapshot:
        """用同一页面作用域中更强的评估替换当前权威证据。

        ``include_page_types`` 传当前 Frontier 的质检范围，经过的页面不算覆盖。
        """

        normalized_page_type = page_type.strip().casefold()
        scope_id = _scope_id(
            location=location,
            page_type=normalized_page_type,
            page_state=page_state,
        )
        scopes = {str(scope["scope_id"]): dict(scope) for scope in self.scopes}
        candidate = _coverage_scope(
            scope_id=scope_id,
            location=location,
            capture_id=capture_id,
            page_type=normalized_page_type,
            page_state=page_state,
            assessment=assessment,
            detection_confidence=detection_confidence,
        )
        current = scopes.get(scope_id)
        if current is not None and not _stronger_scope(candidate, current=current):
            return self
        scopes[scope_id] = candidate
        ordered_scopes = tuple(scopes[key] for key in sorted(scopes))
        counted_page_types = _counted_page_types(include_page_types)
        covered = tuple(
            sorted(
                {
                    str(scope["page_type"]).strip().casefold()
                    for scope in ordered_scopes
                    if (
                        scope.get("assessment_status") == "covered"
                        and str(scope["page_type"]).strip().casefold() in counted_page_types
                    )
                }
            )
        )
        not_applicable = {
            str(scope["page_type"]).strip().casefold()
            for scope in ordered_scopes
            if (
                scope.get("assessment_status") == "not_applicable"
                and str(scope["page_type"]).strip().casefold() in counted_page_types
            )
        }
        missing = tuple(
            sorted(set(self.required_page_types) - set(covered) - not_applicable)
        )
        assessment_ready = _assessment_scope_ready(ordered_scopes, counted_page_types)
        values = self.to_dict()
        values.update(
            revision=self.revision + 1,
            scopes=[dict(scope) for scope in ordered_scopes],
            covered_page_types=list(covered),
            missing_page_types=list(missing),
            assessment_scope_ready=assessment_ready,
            completion_ready=(
                completion_allowed
                and bool(self.required_page_types)
                and self.frontier_terminal
                and assessment_ready
                and not missing
            ),
        )
        values["payload_hash"] = stable_protocol_hash(
            {key: value for key, value in values.items() if key != "payload_hash"}
        )
        return H5CoverageSnapshot.from_dict(values)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "revision": self.revision,
            "authority_source": self.authority_source,
            "required_page_types": list(self.required_page_types),
            "covered_page_types": list(self.covered_page_types),
            "missing_page_types": list(self.missing_page_types),
            "scopes": [dict(scope) for scope in self.scopes],
            "assessment_scope_ready": self.assessment_scope_ready,
            "frontier_terminal": self.frontier_terminal,
            "completion_ready": self.completion_ready,
            "payload_hash": self.payload_hash,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object] | None) -> H5CoverageSnapshot:
        if not isinstance(payload, Mapping):
            raise ValueError("H5 coverage payload 必须为对象")
        raw_scopes = payload.get("scopes")
        if not isinstance(raw_scopes, (list, tuple)) or any(
            not isinstance(scope, Mapping) for scope in raw_scopes
        ):
            raise ValueError("H5 coverage scopes 无效")
        return cls(
            schema_version=str(payload.get("schema_version") or ""),
            revision=int(payload.get("revision") or 0),
            authority_source=str(payload.get("authority_source") or ""),
            required_page_types=_texts(payload.get("required_page_types")),
            covered_page_types=_texts(payload.get("covered_page_types")),
            missing_page_types=_texts(payload.get("missing_page_types")),
            scopes=tuple(dict(scope) for scope in raw_scopes),
            assessment_scope_ready=payload.get("assessment_scope_ready") is True,
            frontier_terminal=payload.get("frontier_terminal") is True,
            completion_ready=payload.get("completion_ready") is True,
            payload_hash=str(payload.get("payload_hash") or ""),
        )


def _texts(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("H5 coverage 页面类型集合无效")
    return tuple(str(item) for item in value)


def _frontier_is_terminal(
    frontier: H5TaskFrontierSnapshot,
    *,
    requires_auth: bool,
    overlay_present: bool,
) -> bool:
    """Frontier 无待执行项且页面可判定时即终态，保留失败和阻断事实。"""
    return (
        frontier.pending_count == 0
        and not any(entry.status.value == "selected" for entry in frontier.entries)
        and not requires_auth
        and not overlay_present
    )


def _assessment_scope_ready(
    scopes: tuple[Mapping[str, object], ...],
    counted_page_types: frozenset[str],
) -> bool:
    """生命周期页和经过的页面保留审计，但只让计入覆盖的业务页的未决评估阻塞业务覆盖。"""

    return bool(scopes) and all(
        str(scope.get("page_type") or "").strip().casefold() not in counted_page_types
        or scope.get("assessment_status") in {"covered", "not_applicable"}
        for scope in scopes
    )


def _counted_page_types(include_page_types: Iterable[object]) -> frozenset[str]:
    """计入覆盖的业务页面类型。

    选了质检范围时只数选中的页面类型：为找目标经过的页面只留认路记录，不算已覆盖，
    评估没结论也不挡完成；没选时数全部业务页面类型。
    """

    business_codes = _business_page_type_codes()
    selected = frozenset(
        value.strip().casefold()
        for value in include_page_types
        if isinstance(value, str) and value.strip().casefold() in business_codes
    )
    return selected or business_codes


def _scope_id(*, location: str, page_type: str, page_state: str) -> str:
    return "h5-scope:" + stable_protocol_hash(
        {
            "route_identity": canonical_h5_route_identity(location),
            "page_type": page_type,
            "page_state": page_state.casefold(),
        }
    )


def _coverage_scope(
    *,
    scope_id: str,
    location: str,
    capture_id: str,
    page_type: str,
    page_state: str,
    assessment: Mapping[str, object],
    detection_confidence: float,
) -> dict[str, object]:
    execution_status = str(assessment.get("execution_status") or "blocked")
    raw_body = assessment.get("page_type_assessment")
    body = dict(raw_body) if isinstance(raw_body, Mapping) else {}
    status = str(
        body.get("status")
        or ("not_applicable" if execution_status == "skipped" else "incomplete")
    )
    confidence = max(0.0, min(1.0, float(detection_confidence)))
    return {
        "scope_id": scope_id,
        "route_identity": canonical_h5_route_identity(location),
        "page_type": page_type,
        "page_state": page_state,
        "page_capture_id": capture_id,
        "assessment_status": status,
        "assessment_hash": stable_protocol_hash(dict(assessment)),
        "detection_confidence": confidence,
    }


def _stronger_scope(
    candidate: Mapping[str, object],
    *,
    current: Mapping[str, object],
) -> bool:
    candidate_status = str(candidate.get("assessment_status") or "")
    current_status = str(current.get("assessment_status") or "")
    terminal_statuses = {
        "covered",
        "requires_auth",
        "blocked_by_safety",
        "not_applicable",
    }
    if current_status != candidate_status:
        return current_status in {"incomplete", "evidence_insufficient"} or (
            current_status == "not_found" and candidate_status in terminal_statuses
        )
    if "detection_confidence" not in current:
        return False
    candidate_confidence = float(candidate.get("detection_confidence") or 0.0)
    current_confidence = float(current.get("detection_confidence") or 0.0)
    return candidate_confidence > current_confidence


def _business_page_type_codes() -> frozenset[str]:
    """返回当前 Catalog 中可进入 H5 业务覆盖分母的页面类型。"""
    return frozenset(get_default_page_type_registry().coverage_required_codes)
