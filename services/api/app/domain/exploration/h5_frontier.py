"""H5 页面探索任务的版本化 Frontier 领域合同。"""

from __future__ import annotations

import json
import posixpath
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from services.api.app.domain.exploration.h5_structural_navigation import (
    h5_global_bottom_navigation_index,
)
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.interaction import (
    InteractionOperation,
    InteractionRiskLevel,
)
from services.api.app.domain.protocol import (
    MAX_PROTOCOL_PAYLOAD_BYTES,
    ProtocolContractError,
    freeze_json_mapping,
    parse_aware_datetime,
    require_aware_datetime,
    require_mapping,
    require_sequence,
    require_text,
    stable_protocol_hash,
    thaw_json_mapping,
)

H5_TASK_FRONTIER_METADATA_KEY = "h5_task_frontier"
H5_TASK_FRONTIER_SCHEMA_VERSION = "h5-task-frontier.v1"

# 重观察作用域候选的同页同语义重试预算：目标持续不可见时（如列表卡片被
# 广告墙遮挡）不再无限重生同一候选，探索转向其余候选；与清障同 URL 预算、
# 业务页弹窗 20 轮同属有界化护栏，并防止 Frontier entries 随重试无界膨胀。
H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT = 5

# Frontier 全量内嵌在 ExplorationWork 里受 256KB 协议上限约束；为外层观察、
# 调度账本和补采缺口摘要预留 64KB。旧终态退休成摘要，活动候选、覆盖、去重
# 与重试预算语义继续保留。
H5_RETAINED_SKIPPED_LIVE_LIMIT = 24
H5_RETAINED_EXECUTED_LIVE_LIMIT = 12
H5_EXPLORATION_WORK_RESERVED_BYTES = 64_000
H5_TASK_FRONTIER_TARGET_BYTES = (
    MAX_PROTOCOL_PAYLOAD_BYTES - H5_EXPLORATION_WORK_RESERVED_BYTES
)

# 同一来源页面、页型和遮挡签名允许保留少量历史终态，以便页面级 3 次签名
# 僵持策略仍能看到真实尝试次数；达到上限后，Capture/candidate_id 变化不得
# 再创建新的 Frontier entry。
H5_POPUP_SIGNATURE_ATTEMPT_LIMIT = 3

_POLICY_KEYS = frozenset(
    {
        "max_pages",
        "journey_steps",
        "include_page_types",
        "exclude_page_types",
    }
)
_BLOCK_REASON_DEPTH = "depth_limit_exceeded"
_BLOCK_REASON_EXCLUDED = "page_type_excluded"
_BLOCK_REASON_NOT_INCLUDED = "page_type_not_included"
_BLOCK_REASON_MAX_PAGES = "max_pages_reached"
_BLOCK_REASON_RISK = "risk_level_not_allowed"
_BLOCK_REASON_ALREADY_COVERED = "page_type_already_covered"
_NON_BLOCKING_REJECTION_REASONS = frozenset(
    {
        _BLOCK_REASON_EXCLUDED,
        _BLOCK_REASON_NOT_INCLUDED,
        _BLOCK_REASON_ALREADY_COVERED,
    }
)
_COMPLETION_ALLOWED_BLOCK_REASONS = frozenset(
    {
        _BLOCK_REASON_DEPTH,
        _BLOCK_REASON_MAX_PAGES,
    }
)
_OBSERVED_PAGE_TYPE_COVERAGE_TARGET_MODE = "observed_page_type_coverage"
_PROFILE_LOCAL_REFRESH_CONTRACTS = {
    "profile_activity_navigation": ("activity_entry_capability", "activity"),
    "profile_payment_navigation": ("payment_entry_capability", "payment"),
    "profile_settings_navigation": ("settings_entry_capability", "settings"),
}


class H5FrontierValidationError(ValueError):
    """H5 Frontier 载荷、状态或策略不满足持久化合同。"""


class H5FrontierEntryStatus(StrEnum):
    """候选在 Frontier 中的不可变处理状态。"""

    PENDING = "pending"
    SELECTED = "selected"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    BLOCKED = "blocked"
    FAILED = "failed"


_POPUP_ATTEMPT_TERMINAL_STATUSES = frozenset(
    {
        H5FrontierEntryStatus.FAILED,
        H5FrontierEntryStatus.BLOCKED,
        H5FrontierEntryStatus.SKIPPED,
    }
)


@dataclass(frozen=True, slots=True)
class H5TaskFrontierEntry:
    """一个可恢复 sibling 的来源状态、候选与处理结果。"""

    entry_id: str
    candidate: CandidateAction
    source_capture_id: str
    source_state_version: str
    source_location: str
    navigation_depth: int
    parent_state_version: str | None
    lineage: tuple[str, ...]
    created_at: datetime
    status: H5FrontierEntryStatus = H5FrontierEntryStatus.PENDING
    restoration_attempt_count: int = 0
    last_failure_reason: str | None = None
    selected_at: datetime | None = None
    finished_at: datetime | None = None

    def __post_init__(self) -> None:
        try:
            for name in (
                "entry_id",
                "source_capture_id",
                "source_state_version",
                "source_location",
            ):
                require_text(getattr(self, name), name)
            if self.parent_state_version is not None:
                require_text(self.parent_state_version, "parent_state_version")
            require_aware_datetime(self.created_at, "created_at")
            if self.selected_at is not None:
                require_aware_datetime(self.selected_at, "selected_at")
            if self.finished_at is not None:
                require_aware_datetime(self.finished_at, "finished_at")
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
        if not isinstance(self.candidate, CandidateAction):
            raise H5FrontierValidationError("candidate 必须为 CandidateAction")
        _non_negative_int(self.navigation_depth, "navigation_depth")
        _non_negative_int(self.restoration_attempt_count, "restoration_attempt_count")
        if not isinstance(self.status, H5FrontierEntryStatus):
            raise H5FrontierValidationError("status 不受支持")
        lineage = _unique_texts(self.lineage, "lineage")
        if self.source_state_version not in lineage:
            raise H5FrontierValidationError("lineage 必须包含 source_state_version")
        if self.parent_state_version is not None and self.parent_state_version not in lineage:
            raise H5FrontierValidationError("lineage 必须包含 parent_state_version")
        if self.status in {
            H5FrontierEntryStatus.BLOCKED,
            H5FrontierEntryStatus.SKIPPED,
        }:
            _optional_reason(self.last_failure_reason, required=True)
        else:
            _optional_reason(self.last_failure_reason, required=False)
        if self.status is H5FrontierEntryStatus.SELECTED and self.selected_at is None:
            raise H5FrontierValidationError("selected 状态必须包含 selected_at")
        if (
            self.status
            in {
                H5FrontierEntryStatus.EXECUTED,
                H5FrontierEntryStatus.SKIPPED,
                H5FrontierEntryStatus.FAILED,
            }
            and self.finished_at is None
        ):
            raise H5FrontierValidationError("终态必须包含 finished_at")
        object.__setattr__(self, "lineage", lineage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "candidate": self.candidate.to_dict(),
            "source_capture_id": self.source_capture_id,
            "source_state_version": self.source_state_version,
            "source_location": self.source_location,
            "navigation_depth": self.navigation_depth,
            "parent_state_version": self.parent_state_version,
            "lineage": list(self.lineage),
            "created_at": self.created_at.isoformat(),
            "status": self.status.value,
            "restoration_attempt_count": self.restoration_attempt_count,
            "last_failure_reason": self.last_failure_reason,
            "selected_at": self.selected_at.isoformat() if self.selected_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> H5TaskFrontierEntry:
        values = _read_fields(
            payload,
            known={
                "entry_id",
                "candidate",
                "source_capture_id",
                "source_state_version",
                "source_location",
                "navigation_depth",
                "parent_state_version",
                "lineage",
                "created_at",
                "status",
                "restoration_attempt_count",
                "last_failure_reason",
                "selected_at",
                "finished_at",
            },
            required={
                "entry_id",
                "candidate",
                "source_capture_id",
                "source_state_version",
                "source_location",
                "navigation_depth",
                "parent_state_version",
                "lineage",
                "created_at",
                "status",
                "restoration_attempt_count",
                "last_failure_reason",
                "selected_at",
                "finished_at",
            },
            field_name="H5TaskFrontierEntry",
        )
        try:
            return cls(
                entry_id=values["entry_id"],
                candidate=CandidateAction.from_dict(
                    require_mapping(values["candidate"], "candidate")
                ),
                source_capture_id=values["source_capture_id"],
                source_state_version=values["source_state_version"],
                source_location=values["source_location"],
                navigation_depth=values["navigation_depth"],
                parent_state_version=values["parent_state_version"],
                lineage=tuple(require_sequence(values["lineage"], "lineage")),
                created_at=parse_aware_datetime(values["created_at"], "created_at"),
                status=H5FrontierEntryStatus(values["status"]),
                restoration_attempt_count=values["restoration_attempt_count"],
                last_failure_reason=values["last_failure_reason"],
                selected_at=(
                    parse_aware_datetime(values["selected_at"], "selected_at")
                    if values["selected_at"] is not None
                    else None
                ),
                finished_at=(
                    parse_aware_datetime(values["finished_at"], "finished_at")
                    if values["finished_at"] is not None
                    else None
                ),
            )
        except (ProtocolContractError, ValueError) as exc:
            raise H5FrontierValidationError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class H5TaskFrontierSnapshot:
    """可序列化、可校验且保留 sibling 的 H5 任务 Frontier。"""

    schema_version: str
    snapshot_id: str
    entries: tuple[H5TaskFrontierEntry, ...]
    active_state_version: str | None
    visited_state_ids: frozenset[str]
    executed_candidate_ids: frozenset[str]
    action_fingerprints: frozenset[str]
    covered_page_count: int
    covered_page_types: frozenset[str]
    max_pages: int | None
    pending_count: int
    blocked_count: int
    failed_count: int
    sampling_policy_summary: Mapping[str, object]
    payload_hash: str
    # 已退休 skipped entry 的墓碑：语义键哈希保去重、预算键计数保重试预算
    retired_semantic_keys: frozenset[str] = frozenset()
    retired_skip_counts: Mapping[str, int] = field(default_factory=dict)
    # 已退休 executed entry 的派生事实，保证压缩不改变覆盖与执行状态。
    retired_visited_state_ids: frozenset[str] = frozenset()
    retired_executed_candidate_ids: frozenset[str] = frozenset()
    retired_action_fingerprints: frozenset[str] = frozenset()
    retired_covered_page_count: int = 0
    retired_covered_page_types: frozenset[str] = frozenset()
    retired_popup_attempt_counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != H5_TASK_FRONTIER_SCHEMA_VERSION:
            raise H5FrontierValidationError("schema_version 不受支持")
        try:
            require_text(self.snapshot_id, "snapshot_id")
            if self.active_state_version is not None:
                require_text(self.active_state_version, "active_state_version")
            require_text(self.payload_hash, "payload_hash")
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
        entries = tuple(self.entries)
        if any(not isinstance(entry, H5TaskFrontierEntry) for entry in entries):
            raise H5FrontierValidationError("entries 必须由 H5TaskFrontierEntry 组成")
        if len({entry.entry_id for entry in entries}) != len(entries):
            raise H5FrontierValidationError("entries 不能包含重复 entry_id")
        visited = _text_set(self.visited_state_ids, "visited_state_ids")
        executed = _text_set(self.executed_candidate_ids, "executed_candidate_ids")
        fingerprints = _text_set(self.action_fingerprints, "action_fingerprints")
        covered_types = _text_set(self.covered_page_types, "covered_page_types")
        _non_negative_int(self.covered_page_count, "covered_page_count")
        retired_covered_count = _non_negative_int(
            self.retired_covered_page_count, "retired_covered_page_count"
        )
        retired_covered_types = _text_set(
            self.retired_covered_page_types, "retired_covered_page_types"
        )
        live_covered_count, live_covered_types = _coverage(entries)
        expected_covered_count = live_covered_count + retired_covered_count
        expected_covered_types = live_covered_types | retired_covered_types
        if self.covered_page_count != expected_covered_count:
            raise H5FrontierValidationError("covered_page_count 必须由已执行 entries 重算")
        if covered_types != expected_covered_types:
            raise H5FrontierValidationError("covered_page_types 必须由已执行 entries 重算")
        if self.max_pages is not None:
            _positive_int(self.max_pages, "max_pages")
            if self.covered_page_count > self.max_pages:
                raise H5FrontierValidationError("covered_page_count 不能超过 max_pages")
        policy = _freeze_policy(self.sampling_policy_summary, self.max_pages)
        expected_counts = _status_counts(entries)
        if (self.pending_count, self.blocked_count, self.failed_count) != expected_counts:
            raise H5FrontierValidationError("Frontier 状态计数必须由 entries 重算")
        retired_visited = _text_set(
            self.retired_visited_state_ids, "retired_visited_state_ids"
        )
        retired_executed = _text_set(
            self.retired_executed_candidate_ids, "retired_executed_candidate_ids"
        )
        live_visited, live_executed = _execution_state(entries)
        expected_visited = live_visited | retired_visited
        expected_executed = live_executed | retired_executed
        if visited != expected_visited:
            raise H5FrontierValidationError("visited_state_ids 必须由已执行 entries 重算")
        if executed != expected_executed:
            raise H5FrontierValidationError("executed_candidate_ids 必须由已执行 entries 重算")
        retired_fingerprints = _text_set(
            self.retired_action_fingerprints, "retired_action_fingerprints"
        )
        expected_fingerprints = frozenset(
            _candidate_fingerprint(entry.source_state_version, entry.candidate) for entry in entries
        ) | retired_fingerprints
        if fingerprints != expected_fingerprints:
            raise H5FrontierValidationError("action_fingerprints 必须由 entries 重算")
        retired_keys = _text_set(self.retired_semantic_keys, "retired_semantic_keys")
        retired_counts = _validated_retired_skip_counts(self.retired_skip_counts)
        retired_popup_counts = _validated_positive_counts(
            self.retired_popup_attempt_counts, "retired_popup_attempt_counts"
        )
        object.__setattr__(self, "entries", entries)
        object.__setattr__(self, "visited_state_ids", visited)
        object.__setattr__(self, "executed_candidate_ids", executed)
        object.__setattr__(self, "action_fingerprints", fingerprints)
        object.__setattr__(self, "covered_page_types", covered_types)
        object.__setattr__(self, "sampling_policy_summary", policy)
        object.__setattr__(self, "retired_semantic_keys", retired_keys)
        object.__setattr__(self, "retired_skip_counts", retired_counts)
        object.__setattr__(self, "retired_visited_state_ids", retired_visited)
        object.__setattr__(self, "retired_executed_candidate_ids", retired_executed)
        object.__setattr__(self, "retired_action_fingerprints", retired_fingerprints)
        object.__setattr__(self, "retired_covered_page_types", retired_covered_types)
        object.__setattr__(self, "retired_popup_attempt_counts", retired_popup_counts)
        if self.payload_hash != self.recompute_payload_hash():
            raise H5FrontierValidationError("payload_hash 与 Frontier 内容不一致")

    @classmethod
    def create(
        cls,
        *,
        max_pages: int | None,
        sampling_policy_summary: Mapping[str, object] | None = None,
    ) -> H5TaskFrontierSnapshot:
        """创建尚未发现候选的空 Frontier。"""
        policy = _freeze_policy(sampling_policy_summary or {}, max_pages)
        values = _snapshot_values(
            entries=(),
            active_state_version=None,
            visited_state_ids=frozenset(),
            executed_candidate_ids=frozenset(),
            covered_page_types=frozenset(),
            max_pages=max_pages,
            sampling_policy_summary=policy,
        )
        return cls(**values)

    @property
    def pending_entries(self) -> tuple[H5TaskFrontierEntry, ...]:
        """按发现顺序返回仍可选择的 sibling。"""
        return tuple(
            entry for entry in self.entries if entry.status is H5FrontierEntryStatus.PENDING
        )

    def merge(
        self,
        *,
        source_capture_id: str,
        source_state_version: str,
        source_location: str,
        navigation_depth: int,
        candidates: Sequence[CandidateAction],
        parent_state_version: str | None = None,
        max_depth: int | None = None,
        now: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """合并一个已观测状态的新候选，保留所有未选择 sibling。"""
        return merge_h5_candidates(
            self,
            source_capture_id=source_capture_id,
            source_state_version=source_state_version,
            source_location=source_location,
            navigation_depth=navigation_depth,
            candidates=candidates,
            parent_state_version=parent_state_version,
            max_depth=max_depth,
            now=now,
        )

    def select_entry(
        self,
        entry_id: str,
        *,
        selected_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """不可变地将一个 pending entry 标记为 selected。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.PENDING:
            raise H5FrontierValidationError("只能选择 pending entry")
        selected = replace(
            entry,
            status=H5FrontierEntryStatus.SELECTED,
            selected_at=_now(selected_at),
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=selected,
            active_state_version=entry.source_state_version,
        )

    def defer_selected(self, entry_id: str) -> H5TaskFrontierSnapshot:
        """现场交接时归还原目标，不重置尝试次数或伪造已执行。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.SELECTED:
            raise H5FrontierValidationError("只能归还 selected entry")
        return _transition_snapshot(
            self, entry_id=entry_id,
            replacement=replace(entry, status=H5FrontierEntryStatus.PENDING, selected_at=None),
            active_state_version=self.active_state_version,
        )

    def mark_executed(
        self,
        entry_id: str,
        *,
        finished_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """不可变地收口 selected entry，并据此重算覆盖与预算阻断。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.SELECTED:
            raise H5FrontierValidationError("只能完成 selected entry")
        executed = replace(
            entry,
            status=H5FrontierEntryStatus.EXECUTED,
            finished_at=_now(finished_at),
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=executed,
            active_state_version=entry.source_state_version,
            visited_state_version=entry.source_state_version,
            executed_candidate_id=entry.candidate.candidate_id,
            apply_page_budget=True,
        )

    def record_observed_page_type(
        self,
        *,
        source_capture_id: str,
        source_state_version: str,
        source_location: str,
        navigation_depth: int,
        page_type: str | None,
        observed_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """用普通 executed 账本记录实际业务页型，保持旧 v1 派生兼容。"""

        if page_type is None:
            return self
        try:
            normalized = require_text(page_type, "page_type").casefold()
            require_text(source_capture_id, "source_capture_id")
            require_text(source_state_version, "source_state_version")
            require_text(source_location, "source_location")
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
        _non_negative_int(navigation_depth, "navigation_depth")
        if normalized in {"line_loading", "startup_ad", "guide_page"}:
            return self
        policy = self.sampling_policy_summary
        included = set(policy["include_page_types"])
        if normalized in set(policy["exclude_page_types"]):
            return self
        if included and normalized not in included:
            return self
        if normalized in self.covered_page_types:
            return self
        timestamp = _now(observed_at)
        candidate_id = "h5-candidate:" + stable_protocol_hash(
            {
                "target_mode": _OBSERVED_PAGE_TYPE_COVERAGE_TARGET_MODE,
                "source_capture_id": source_capture_id,
                "source_state_version": source_state_version,
                "page_type": normalized,
            }
        )
        candidate = CandidateAction(
            candidate_id=candidate_id,
            semantic_target=f"记录已观察页型 {normalized}",
            action_type=InteractionOperation.OBSERVE,
            risk_level=InteractionRiskLevel.LOW,
            exploration_value=0,
            navigation_depth=navigation_depth,
            metadata={
                "purpose": "记录平台清障动作后实际识别到的业务页型",
                "source_page_type": normalized,
                "expected_next_page_type": normalized,
                "target_keywords": [
                    _OBSERVED_PAGE_TYPE_COVERAGE_TARGET_MODE,
                    f"page_type:{normalized}",
                ],
                "action_parameters": {
                    "target_mode": _OBSERVED_PAGE_TYPE_COVERAGE_TARGET_MODE,
                    "observed_page_type": normalized,
                },
            },
        )
        fingerprint = _candidate_fingerprint(source_state_version, candidate)
        entry_id = "h5-frontier-entry:" + stable_protocol_hash(
            {"source_state_version": source_state_version, "fingerprint": fingerprint}
        )
        if any(entry.entry_id == entry_id for entry in self.entries):
            return self
        observed_entry = H5TaskFrontierEntry(
            entry_id=entry_id,
            candidate=candidate,
            source_capture_id=source_capture_id,
            source_state_version=source_state_version,
            source_location=source_location,
            navigation_depth=navigation_depth,
            parent_state_version=None,
            lineage=(source_state_version,),
            created_at=timestamp,
            status=H5FrontierEntryStatus.EXECUTED,
            selected_at=timestamp,
            finished_at=timestamp,
        )
        expanded = _new_snapshot(
            entries=(*self.entries, observed_entry),
            active_state_version=source_state_version,
            visited_state_ids=frozenset((*self.visited_state_ids, source_state_version)),
            executed_candidate_ids=frozenset((*self.executed_candidate_ids, candidate_id)),
            covered_page_types=self.covered_page_types,
            max_pages=self.max_pages,
            sampling_policy_summary=self.sampling_policy_summary,
            retired_semantic_keys=self.retired_semantic_keys,
            retired_skip_counts=self.retired_skip_counts,
            retired_visited_state_ids=self.retired_visited_state_ids,
            retired_executed_candidate_ids=self.retired_executed_candidate_ids,
            retired_action_fingerprints=self.retired_action_fingerprints,
            retired_covered_page_count=self.retired_covered_page_count,
            retired_covered_page_types=self.retired_covered_page_types,
            retired_popup_attempt_counts=self.retired_popup_attempt_counts,
        )
        return _transition_snapshot(
            expanded,
            entry_id=entry_id,
            replacement=observed_entry,
            active_state_version=source_state_version,
            visited_state_version=source_state_version,
            executed_candidate_id=candidate_id,
            apply_page_budget=True,
        )

    def mark_executed_with_observed_page_type(
        self,
        entry_id: str,
        *,
        observed_page_type: str,
        discrepancy_reason: str,
        finished_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """预期页型不准但已落地时，保留偏差并仅按实际页型计入覆盖。"""

        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.SELECTED:
            raise H5FrontierValidationError("只能完成 selected entry")
        try:
            normalized_page_type = require_text(observed_page_type, "observed_page_type").casefold()
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
        _optional_reason(discrepancy_reason, required=True)
        metadata = thaw_json_mapping(entry.candidate.metadata)
        declared = _expected_page_type(entry.candidate)
        if declared is not None:
            metadata["declared_expected_next_page_type"] = declared
        metadata["expected_next_page_type"] = normalized_page_type
        metadata["observed_next_page_type"] = normalized_page_type
        observed_candidate = replace(entry.candidate, metadata=metadata)
        executed = replace(
            entry,
            candidate=observed_candidate,
            status=H5FrontierEntryStatus.EXECUTED,
            last_failure_reason=discrepancy_reason,
            finished_at=_now(finished_at),
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=executed,
            active_state_version=entry.source_state_version,
            visited_state_version=entry.source_state_version,
            executed_candidate_id=entry.candidate.candidate_id,
            apply_page_budget=True,
        )

    def mark_blocked(
        self,
        entry_id: str,
        *,
        reason: str,
        finished_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """不可变地记录 selected entry 到达受阻页面。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.SELECTED:
            raise H5FrontierValidationError("只能阻断收口 selected entry")
        _optional_reason(reason, required=True)
        blocked = replace(
            entry,
            status=H5FrontierEntryStatus.BLOCKED,
            last_failure_reason=reason,
            finished_at=_now(finished_at),
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=blocked,
            active_state_version=entry.source_state_version,
        )

    def mark_skipped(
        self,
        entry_id: str,
        *,
        reason: str,
        finished_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """不可变地跳过 selected entry，不计覆盖、阻断或失败。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.SELECTED:
            raise H5FrontierValidationError("只能跳过收口 selected entry")
        _optional_reason(reason, required=True)
        skipped = replace(
            entry,
            status=H5FrontierEntryStatus.SKIPPED,
            last_failure_reason=reason,
            finished_at=_now(finished_at),
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=skipped,
            active_state_version=entry.source_state_version,
        )

    def mark_pending_skipped(
        self,
        entry_id: str,
        *,
        reason: str,
        active_state_version: str,
        finished_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """审计跳过已被当前权威页面状态证伪的 pending 候选。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.PENDING:
            raise H5FrontierValidationError("只能跳过 pending entry")
        _optional_reason(reason, required=True)
        try:
            require_text(active_state_version, "active_state_version")
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
        skipped = replace(
            entry,
            status=H5FrontierEntryStatus.SKIPPED,
            last_failure_reason=reason,
            finished_at=_now(finished_at),
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=skipped,
            active_state_version=active_state_version,
        )

    def mark_failed(
        self,
        entry_id: str,
        *,
        reason: str,
        finished_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """不可变地标记 selected entry 失败，不消耗页面覆盖预算。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.SELECTED:
            raise H5FrontierValidationError("只能失败收口 selected entry")
        _optional_reason(reason, required=True)
        failed = replace(
            entry,
            status=H5FrontierEntryStatus.FAILED,
            last_failure_reason=reason,
            finished_at=_now(finished_at),
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=failed,
            active_state_version=entry.source_state_version,
        )

    def settle_remaining(
        self,
        *,
        successful: bool,
        reason: str,
        finished_at: datetime | None = None,
    ) -> H5TaskFrontierSnapshot:
        """任务收场时结清未执行候选，避免终态 Work 继续暴露活动 Frontier。"""

        _optional_reason(reason, required=True)
        timestamp = _now(finished_at)
        settled = self
        unresolved = tuple(
            entry
            for entry in settled.entries
            if entry.status in {H5FrontierEntryStatus.SELECTED, H5FrontierEntryStatus.PENDING}
        )
        if not unresolved:
            return settled

        has_blocker = settled.blocked_count > 0 or settled.failed_count > 0
        blocker_id = None if successful or has_blocker else unresolved[0].entry_id
        for entry in unresolved:
            current = _entry_by_id(settled.entries, entry.entry_id)
            if current.status is H5FrontierEntryStatus.PENDING:
                settled = settled.select_entry(
                    current.entry_id,
                    selected_at=timestamp,
                )
            if current.entry_id == blocker_id:
                settled = settled.mark_blocked(
                    current.entry_id,
                    reason=reason,
                    finished_at=timestamp,
                )
            else:
                settled = settled.mark_skipped(
                    current.entry_id,
                    reason=reason,
                    finished_at=timestamp,
                )
        return settled

    def record_restoration_attempt(self, entry_id: str) -> H5TaskFrontierSnapshot:
        """不可变地记录 selected sibling 的一次来源恢复尝试。"""
        entry = _entry_by_id(self.entries, entry_id)
        if entry.status is not H5FrontierEntryStatus.SELECTED:
            raise H5FrontierValidationError("只能为 selected entry 记录恢复尝试")
        attempted = replace(
            entry,
            restoration_attempt_count=entry.restoration_attempt_count + 1,
        )
        return _transition_snapshot(
            self,
            entry_id=entry_id,
            replacement=attempted,
            active_state_version=entry.source_state_version,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "entries": [entry.to_dict() for entry in self.entries],
            "active_state_version": self.active_state_version,
            "visited_state_ids": sorted(self.visited_state_ids),
            "executed_candidate_ids": sorted(self.executed_candidate_ids),
            "action_fingerprints": sorted(self.action_fingerprints),
            "covered_page_count": self.covered_page_count,
            "covered_page_types": sorted(self.covered_page_types),
            "max_pages": self.max_pages,
            "pending_count": self.pending_count,
            "blocked_count": self.blocked_count,
            "failed_count": self.failed_count,
            "sampling_policy_summary": thaw_json_mapping(self.sampling_policy_summary),
            "payload_hash": self.payload_hash,
        }
        # 空墓碑不出现在序列化里，保证退休发生前的载荷与旧格式逐字节一致
        if self.retired_semantic_keys:
            payload["retired_semantic_keys"] = sorted(self.retired_semantic_keys)
        if self.retired_skip_counts:
            payload["retired_skip_counts"] = dict(sorted(self.retired_skip_counts.items()))
        if self.retired_visited_state_ids:
            payload["retired_visited_state_ids"] = sorted(self.retired_visited_state_ids)
        if self.retired_executed_candidate_ids:
            payload["retired_executed_candidate_ids"] = sorted(
                self.retired_executed_candidate_ids
            )
        if self.retired_action_fingerprints:
            payload["retired_action_fingerprints"] = sorted(self.retired_action_fingerprints)
        if self.retired_covered_page_count:
            payload["retired_covered_page_count"] = self.retired_covered_page_count
        if self.retired_covered_page_types:
            payload["retired_covered_page_types"] = sorted(self.retired_covered_page_types)
        if self.retired_popup_attempt_counts:
            payload["retired_popup_attempt_counts"] = dict(
                sorted(self.retired_popup_attempt_counts.items())
            )
        return payload

    def recompute_payload_hash(self) -> str:
        """用稳定 JSON 重算载荷哈希，哈希字段自身不参与输入。"""
        return stable_protocol_hash(self._payload_without_hash())

    def _payload_without_hash(self) -> dict[str, Any]:
        payload = self.to_dict()
        payload.pop("payload_hash")
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> H5TaskFrontierSnapshot:
        values = _read_fields(
            payload,
            known={
                "schema_version",
                "snapshot_id",
                "entries",
                "active_state_version",
                "visited_state_ids",
                "executed_candidate_ids",
                "action_fingerprints",
                "covered_page_count",
                "covered_page_types",
                "max_pages",
                "pending_count",
                "blocked_count",
                "failed_count",
                "sampling_policy_summary",
                "payload_hash",
                "retired_semantic_keys",
                "retired_skip_counts",
                "retired_visited_state_ids",
                "retired_executed_candidate_ids",
                "retired_action_fingerprints",
                "retired_covered_page_count",
                "retired_covered_page_types",
                "retired_popup_attempt_counts",
            },
            required={
                "schema_version",
                "snapshot_id",
                "entries",
                "active_state_version",
                "visited_state_ids",
                "executed_candidate_ids",
                "action_fingerprints",
                "covered_page_count",
                "covered_page_types",
                "max_pages",
                "pending_count",
                "blocked_count",
                "failed_count",
                "sampling_policy_summary",
                "payload_hash",
            },
            field_name="H5TaskFrontierSnapshot",
        )
        if values["schema_version"] != H5_TASK_FRONTIER_SCHEMA_VERSION:
            raise H5FrontierValidationError("schema_version 不受支持")
        serialized_payload = dict(values)
        supplied_hash = serialized_payload.pop("payload_hash")
        try:
            computed_hash = stable_protocol_hash(serialized_payload)
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
        if not isinstance(supplied_hash, str) or supplied_hash != computed_hash:
            raise H5FrontierValidationError("payload_hash 与 Frontier 内容不一致")
        try:
            return cls(
                schema_version=values["schema_version"],
                snapshot_id=values["snapshot_id"],
                entries=tuple(
                    H5TaskFrontierEntry.from_dict(require_mapping(item, "entries[]"))
                    for item in require_sequence(values["entries"], "entries")
                ),
                active_state_version=values["active_state_version"],
                visited_state_ids=frozenset(
                    require_sequence(values["visited_state_ids"], "visited_state_ids")
                ),
                executed_candidate_ids=frozenset(
                    require_sequence(values["executed_candidate_ids"], "executed_candidate_ids")
                ),
                action_fingerprints=frozenset(
                    require_sequence(values["action_fingerprints"], "action_fingerprints")
                ),
                covered_page_count=values["covered_page_count"],
                covered_page_types=frozenset(
                    require_sequence(values["covered_page_types"], "covered_page_types")
                ),
                max_pages=values["max_pages"],
                pending_count=values["pending_count"],
                blocked_count=values["blocked_count"],
                failed_count=values["failed_count"],
                sampling_policy_summary=require_mapping(
                    values["sampling_policy_summary"], "sampling_policy_summary"
                ),
                payload_hash=values["payload_hash"],
                retired_semantic_keys=frozenset(
                    require_sequence(
                        values.get("retired_semantic_keys", ()), "retired_semantic_keys"
                    )
                ),
                retired_skip_counts=require_mapping(
                    values.get("retired_skip_counts", {}), "retired_skip_counts"
                ),
                retired_visited_state_ids=frozenset(
                    require_sequence(
                        values.get("retired_visited_state_ids", ()),
                        "retired_visited_state_ids",
                    )
                ),
                retired_executed_candidate_ids=frozenset(
                    require_sequence(
                        values.get("retired_executed_candidate_ids", ()),
                        "retired_executed_candidate_ids",
                    )
                ),
                retired_action_fingerprints=frozenset(
                    require_sequence(
                        values.get("retired_action_fingerprints", ()),
                        "retired_action_fingerprints",
                    )
                ),
                retired_covered_page_count=values.get("retired_covered_page_count", 0),
                retired_covered_page_types=frozenset(
                    require_sequence(
                        values.get("retired_covered_page_types", ()),
                        "retired_covered_page_types",
                    )
                ),
                retired_popup_attempt_counts=require_mapping(
                    values.get("retired_popup_attempt_counts", {}),
                    "retired_popup_attempt_counts",
                ),
            )
        except (ProtocolContractError, ValueError) as exc:
            raise H5FrontierValidationError(str(exc)) from exc


def merge_h5_candidates(
    snapshot: H5TaskFrontierSnapshot,
    *,
    source_capture_id: str,
    source_state_version: str,
    source_location: str,
    navigation_depth: int,
    candidates: Sequence[CandidateAction],
    parent_state_version: str | None = None,
    max_depth: int | None = None,
    now: datetime | None = None,
) -> H5TaskFrontierSnapshot:
    """纯函数形式合并 H5 候选，跨状态按语义动作去重。"""
    if not isinstance(snapshot, H5TaskFrontierSnapshot):
        raise TypeError("snapshot 必须为 H5TaskFrontierSnapshot")
    _validate_merge_input(
        source_capture_id,
        source_state_version,
        source_location,
        navigation_depth,
        parent_state_version,
        max_depth,
    )
    candidate_values = tuple(candidates)
    if any(not isinstance(candidate, CandidateAction) for candidate in candidate_values):
        raise H5FrontierValidationError("candidates 必须由 CandidateAction 组成")
    observed_at = _now(now)
    existing_semantic_indices: dict[
        tuple[str, str, str | None, str, str | None, str | None, str | None], int
    ] = {}
    for index, entry in enumerate(snapshot.entries):
        semantic_key = _semantic_candidate_key(
            entry.candidate,
            source_state_version=entry.source_state_version,
            source_location=entry.source_location,
        )
        prior_index = existing_semantic_indices.get(semantic_key)
        if (
            prior_index is None
            or snapshot.entries[prior_index].status is not H5FrontierEntryStatus.PENDING
            and entry.status is H5FrontierEntryStatus.PENDING
        ):
            existing_semantic_indices[semantic_key] = index
    lineage = _lineage(
        snapshot,
        source_state_version,
        parent_state_version,
        navigation_depth=navigation_depth,
    )
    resolved_parent_state_version = parent_state_version
    if parent_state_version is not None and parent_state_version not in lineage:
        # 返回祖先状态时，动作前状态不是领域树父节点，必须恢复祖先原父链。
        resolved_parent_state_version = lineage[-2] if len(lineage) > 1 else None
    entries = list(snapshot.entries)
    for candidate in candidate_values:
        semantic_key = _semantic_candidate_key(
            candidate,
            source_state_version=source_state_version,
            source_location=source_location,
        )
        existing_index = existing_semantic_indices.get(semantic_key)
        if existing_index is not None and _independent_source_after_failed_restore(
            entries[existing_index], candidate=candidate,
        ):
            # 同名入口属于已独立证明的新对象，不复活旧来源，也不继承其失败缓存。
            existing_index = next((
                index for index, item in enumerate(entries)
                if item.candidate.metadata.get("source_identity_fingerprint")
                == candidate.metadata.get("source_identity_fingerprint")
                and _semantic_candidate_key(
                    item.candidate, source_state_version=item.source_state_version,
                    source_location=item.source_location,
                ) == semantic_key
            ), None)
        if existing_index is not None:
            existing_entry = entries[existing_index]
            same_semantic_entries = tuple(
                entry
                for entry in entries
                if _semantic_candidate_key(
                    entry.candidate,
                    source_state_version=entry.source_state_version,
                    source_location=entry.source_location,
                )
                == semantic_key
            )
            if any(entry.source_capture_id == source_capture_id for entry in same_semantic_entries):
                # 同一 Capture 的重复投影是幂等重放，不论前一条是否已进入终态，
                # 都不能因为模型 candidate_id 不同而再插入一条相同弹层动作。
                continue
            stale_source_retry = _can_refresh_stale_source(existing_entry, candidate=candidate)
            if stale_source_retry or existing_entry.status is H5FrontierEntryStatus.PENDING and (
                _is_distinct_navigation_branch_candidate(candidate)
                or _trusted_profile_local_refresh_candidate(candidate)
            ):
                # 结构导航和个人中心精确入口在当前页面重新出现时，用当前密封
                # 证据刷新尚未执行的语义候选，避免后续动作绑定已过期状态。
                reason = _rejection_reason(snapshot, candidate, max_depth)
                status = _rejection_status(reason)
                # 来源过期没有执行点击，只刷新当前重新证明的候选；次数跨快照
                # 保留，不能借新的 Capture 重置重试或恢复预算。
                refresh_count = _source_refresh_count(existing_entry) + int(stale_source_retry)
                candidate = replace(candidate, metadata={
                    **candidate.metadata,
                    "source_refresh_attempt_count": refresh_count,
                })
                entry_id = _frontier_entry_id(
                    source_state_version=source_state_version,
                    source_capture_id=source_capture_id,
                    candidate=candidate,
                    semantic_key=semantic_key,
                )
                entries[existing_index] = H5TaskFrontierEntry(
                    entry_id=entry_id,
                    candidate=candidate,
                    source_capture_id=source_capture_id,
                    source_state_version=source_state_version,
                    source_location=source_location,
                    navigation_depth=navigation_depth,
                    parent_state_version=resolved_parent_state_version,
                    lineage=lineage,
                    created_at=existing_entry.created_at,
                    status=status,
                    restoration_attempt_count=existing_entry.restoration_attempt_count,
                    last_failure_reason=reason,
                    finished_at=(observed_at if status is H5FrontierEntryStatus.SKIPPED else None),
                )
                continue
            if existing_entry.status in {
                H5FrontierEntryStatus.PENDING,
                H5FrontierEntryStatus.SELECTED,
            }:
                continue
            if not _is_obstruction_close_candidate(candidate):
                continue
            # 同一真实弹层允许少量历史终态，供页面级 3 次签名僵持判断；
            # 达到上限后，无论 Capture/candidate_id 如何变化都不再重生。
            if (
                sum(
                    entry.status in _POPUP_ATTEMPT_TERMINAL_STATUSES
                    and not str(entry.last_failure_reason or "").endswith(
                        ("LOGICAL_SOURCE_STATE_STALE", "POPUP_MODAL_SCOPE_MISMATCH")
                    )
                    for entry in same_semantic_entries
                )
                >= H5_POPUP_SIGNATURE_ATTEMPT_LIMIT
            ):
                continue
        if _semantic_key_hash(semantic_key) in snapshot.retired_semantic_keys:
            continue
        if _semantic_reobservation_budget_exhausted(
            snapshot,
            candidate=candidate,
            source_location=source_location,
        ):
            continue
        reason = _rejection_reason(snapshot, candidate, max_depth)
        status = _rejection_status(reason)
        entry_id = _frontier_entry_id(
            source_state_version=source_state_version,
            source_capture_id=source_capture_id,
            candidate=candidate,
            semantic_key=semantic_key,
        )
        entries.append(
            H5TaskFrontierEntry(
                entry_id=entry_id,
                candidate=candidate,
                source_capture_id=source_capture_id,
                source_state_version=source_state_version,
                source_location=source_location,
                navigation_depth=navigation_depth,
                parent_state_version=resolved_parent_state_version,
                lineage=lineage,
                created_at=observed_at,
                status=status,
                last_failure_reason=reason,
                finished_at=(observed_at if status is H5FrontierEntryStatus.SKIPPED else None),
            )
        )
        existing_semantic_indices[semantic_key] = len(entries) - 1
    return _new_snapshot(
        entries=tuple(entries),
        active_state_version=source_state_version,
        visited_state_ids=snapshot.visited_state_ids,
        executed_candidate_ids=snapshot.executed_candidate_ids,
        covered_page_types=snapshot.covered_page_types,
        max_pages=snapshot.max_pages,
        sampling_policy_summary=snapshot.sampling_policy_summary,
        retired_semantic_keys=snapshot.retired_semantic_keys,
        retired_skip_counts=snapshot.retired_skip_counts,
        retired_visited_state_ids=snapshot.retired_visited_state_ids,
        retired_executed_candidate_ids=snapshot.retired_executed_candidate_ids,
        retired_action_fingerprints=snapshot.retired_action_fingerprints,
        retired_covered_page_count=snapshot.retired_covered_page_count,
        retired_covered_page_types=snapshot.retired_covered_page_types,
        retired_popup_attempt_counts=snapshot.retired_popup_attempt_counts,
    )


def select_next_h5_entry(
    snapshot: H5TaskFrontierSnapshot,
    *,
    active_state_version: str | None = None,
    scheduler: Any | None = None,
    scheduler_state: Any | None = None,
    now: datetime | None = None,
) -> H5TaskFrontierEntry | None:
    """选择当前 lineage 的安全候选；耗尽后按最近 sibling 与插入顺序补全。"""
    if not isinstance(snapshot, H5TaskFrontierSnapshot):
        raise TypeError("snapshot 必须为 H5TaskFrontierSnapshot")
    active = (
        active_state_version if active_state_version is not None else snapshot.active_state_version
    )
    if active is not None:
        try:
            require_text(active, "active_state_version")
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
    candidates = [
        entry for entry in snapshot.entries if entry.status is H5FrontierEntryStatus.PENDING
    ]
    if not candidates:
        return None
    priorities = _priority_page_types(snapshot.sampling_policy_summary)
    missing_page_types = priorities - set(snapshot.covered_page_types)
    if isinstance(scheduler_state, Mapping):
        raw_scheduler_state = scheduler_state.get(
            "bounded_exploration_scheduler",
            scheduler_state,
        )
        required_page_types = tuple(
            raw_scheduler_state.get("required_page_types", ())
            if isinstance(raw_scheduler_state, Mapping)
            else ()
        )
        evidence_gap_page_types = tuple(
            raw_scheduler_state.get("evidence_gap_page_types", ())
            if isinstance(raw_scheduler_state, Mapping)
            else ()
        )
    else:
        required_page_types = tuple(
            getattr(scheduler_state, "required_page_types", ())
            if scheduler_state is not None
            else priorities
        )
        evidence_gap_page_types = tuple(
            getattr(scheduler_state, "evidence_gap_page_types", ())
            if scheduler_state is not None
            else ()
        )
    if scheduler is not None:
        filter_candidates = getattr(scheduler, "filter_candidates", None)
        if callable(filter_candidates):
            filtered = filter_candidates(
                tuple(entry.candidate for entry in candidates),
                required_page_types=required_page_types,
                evidence_gap_page_types=evidence_gap_page_types,
                missing_page_types=missing_page_types,
                state=scheduler_state,
                now=now,
            )
            allowed_ids = {candidate.candidate_id for candidate in filtered}
            candidates = [
                entry for entry in candidates if entry.candidate.candidate_id in allowed_ids
            ]
            if not candidates:
                return None
    active_lineage = _lineage_for_state(snapshot.entries, active)
    local_novel_entry_pending = any(
        entry.source_state_version == active
        and _expected_page_type(entry.candidate) not in snapshot.covered_page_types
        and _expected_page_type(entry.candidate) is not None
        and entry.candidate.risk_level is InteractionRiskLevel.LOW
        and (
            entry.candidate.metadata.get("verified_business_link") is True
            or any(_candidate_has_keyword(entry.candidate, keyword) for keyword in (
                "verified_business_link", "profile_local_action",
            ))
            or (
                _candidate_has_keyword(entry.candidate, "list_detail_navigation")
                and _is_uncovered_structural_branch_candidate(
                    entry.candidate, covered_page_types=snapshot.covered_page_types,
                )
            )
        )
        for entry in candidates
    )
    return min(
        enumerate(candidates),
        key=lambda pair: _selection_key(
            pair[1],
            pair[0],
            active,
            active_lineage,
            priorities,
            snapshot.covered_page_types,
            scheduler=scheduler,
            required_page_types=required_page_types,
            evidence_gap_page_types=evidence_gap_page_types,
            missing_page_types=missing_page_types,
            local_novel_entry_pending=local_novel_entry_pending,
        ),
    )[1]


def is_h5_obstruction_close_candidate(candidate: CandidateAction) -> bool:
    """判断候选是否为弹窗清障关闭动作，供选择端做清障优先排序。"""
    return _is_obstruction_close_candidate(candidate)


def _new_snapshot(**kwargs: Any) -> H5TaskFrontierSnapshot:
    return H5TaskFrontierSnapshot(**_snapshot_values(**kwargs))


def _transition_snapshot(
    snapshot: H5TaskFrontierSnapshot,
    *,
    entry_id: str,
    replacement: H5TaskFrontierEntry,
    active_state_version: str,
    visited_state_version: str | None = None,
    executed_candidate_id: str | None = None,
    apply_page_budget: bool = False,
) -> H5TaskFrontierSnapshot:
    entries = tuple(
        replacement if entry.entry_id == entry_id else entry for entry in snapshot.entries
    )
    if apply_page_budget:
        covered_page_types = _coverage(entries)[1] | snapshot.retired_covered_page_types
        included = frozenset(snapshot.sampling_policy_summary["include_page_types"])
        if included and included <= covered_page_types:
            # 所选页面类型都走到了：还没走的经过类候选不再需要
            entries = tuple(
                replace(
                    entry,
                    status=H5FrontierEntryStatus.SKIPPED,
                    last_failure_reason=_BLOCK_REASON_NOT_INCLUDED,
                    finished_at=replacement.finished_at,
                )
                if entry.status is H5FrontierEntryStatus.PENDING
                and not _is_platform_control_candidate(entry.candidate)
                and _expected_page_type(entry.candidate) not in included
                else entry
                for entry in entries
            )
    if (
        apply_page_budget
        and snapshot.max_pages is not None
        and _coverage(entries)[0] + snapshot.retired_covered_page_count >= snapshot.max_pages
    ):
        entries = tuple(
            replace(
                entry,
                status=H5FrontierEntryStatus.BLOCKED,
                last_failure_reason=_BLOCK_REASON_MAX_PAGES,
            )
            if entry.status is H5FrontierEntryStatus.PENDING
            and not _is_platform_control_candidate(entry.candidate)
            else entry
            for entry in entries
        )
    visited = set(snapshot.visited_state_ids)
    if visited_state_version is not None:
        visited.add(visited_state_version)
    executed = set(snapshot.executed_candidate_ids)
    if executed_candidate_id is not None:
        executed.add(executed_candidate_id)
    return _new_snapshot(
        entries=entries,
        active_state_version=active_state_version,
        visited_state_ids=frozenset(visited),
        executed_candidate_ids=frozenset(executed),
        covered_page_types=frozenset(),
        max_pages=snapshot.max_pages,
        sampling_policy_summary=snapshot.sampling_policy_summary,
        retired_semantic_keys=snapshot.retired_semantic_keys,
        retired_skip_counts=snapshot.retired_skip_counts,
        retired_visited_state_ids=snapshot.retired_visited_state_ids,
        retired_executed_candidate_ids=snapshot.retired_executed_candidate_ids,
        retired_action_fingerprints=snapshot.retired_action_fingerprints,
        retired_covered_page_count=snapshot.retired_covered_page_count,
        retired_covered_page_types=snapshot.retired_covered_page_types,
        retired_popup_attempt_counts=snapshot.retired_popup_attempt_counts,
    )


def _snapshot_values(
    *,
    entries: tuple[H5TaskFrontierEntry, ...],
    active_state_version: str | None,
    visited_state_ids: frozenset[str],
    executed_candidate_ids: frozenset[str],
    covered_page_types: frozenset[str],
    max_pages: int | None,
    sampling_policy_summary: Mapping[str, object],
    retired_semantic_keys: frozenset[str] = frozenset(),
    retired_skip_counts: Mapping[str, int] | None = None,
    retired_visited_state_ids: frozenset[str] = frozenset(),
    retired_executed_candidate_ids: frozenset[str] = frozenset(),
    retired_action_fingerprints: frozenset[str] = frozenset(),
    retired_covered_page_count: int = 0,
    retired_covered_page_types: frozenset[str] = frozenset(),
    retired_popup_attempt_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    policy = _freeze_policy(sampling_policy_summary, max_pages)
    retired_counts = dict(retired_skip_counts or {})
    retired_popup_counts = dict(retired_popup_attempt_counts or {})
    fingerprints = frozenset(
        _candidate_fingerprint(entry.source_state_version, entry.candidate) for entry in entries
    ) | retired_action_fingerprints
    pending_count, blocked_count, failed_count = _status_counts(entries)
    live_covered_page_count, live_covered_page_types = _coverage(entries)
    covered_page_count = live_covered_page_count + retired_covered_page_count
    derived_covered_page_types = live_covered_page_types | retired_covered_page_types
    identity = {
        "entries": [entry.to_dict() for entry in entries],
        "active_state_version": active_state_version,
        "visited_state_ids": sorted(visited_state_ids),
        "executed_candidate_ids": sorted(executed_candidate_ids),
        "covered_page_types": sorted(derived_covered_page_types),
        "max_pages": max_pages,
        "sampling_policy_summary": thaw_json_mapping(policy),
    }
    snapshot_id = "h5-task-frontier:" + stable_protocol_hash(identity)
    values = {
        "schema_version": H5_TASK_FRONTIER_SCHEMA_VERSION,
        "snapshot_id": snapshot_id,
        "entries": entries,
        "active_state_version": active_state_version,
        "visited_state_ids": visited_state_ids,
        "executed_candidate_ids": executed_candidate_ids,
        "action_fingerprints": fingerprints,
        "covered_page_count": covered_page_count,
        "covered_page_types": derived_covered_page_types,
        "max_pages": max_pages,
        "pending_count": pending_count,
        "blocked_count": blocked_count,
        "failed_count": failed_count,
        "sampling_policy_summary": policy,
    }
    hash_payload = {key: value for key, value in values.items() if key != "payload_hash"}
    hash_payload["entries"] = [entry.to_dict() for entry in entries]
    hash_payload["visited_state_ids"] = sorted(visited_state_ids)
    hash_payload["executed_candidate_ids"] = sorted(executed_candidate_ids)
    hash_payload["action_fingerprints"] = sorted(fingerprints)
    hash_payload["covered_page_types"] = sorted(derived_covered_page_types)
    hash_payload["sampling_policy_summary"] = thaw_json_mapping(policy)
    # 与 to_dict 的条件序列化一致：空墓碑不进哈希，退休前载荷与旧格式同哈希
    if retired_semantic_keys:
        hash_payload["retired_semantic_keys"] = sorted(retired_semantic_keys)
        values["retired_semantic_keys"] = retired_semantic_keys
    if retired_counts:
        hash_payload["retired_skip_counts"] = dict(sorted(retired_counts.items()))
        values["retired_skip_counts"] = retired_counts
    if retired_visited_state_ids:
        hash_payload["retired_visited_state_ids"] = sorted(retired_visited_state_ids)
        values["retired_visited_state_ids"] = retired_visited_state_ids
    if retired_executed_candidate_ids:
        hash_payload["retired_executed_candidate_ids"] = sorted(
            retired_executed_candidate_ids
        )
        values["retired_executed_candidate_ids"] = retired_executed_candidate_ids
    if retired_action_fingerprints:
        hash_payload["retired_action_fingerprints"] = sorted(retired_action_fingerprints)
        values["retired_action_fingerprints"] = retired_action_fingerprints
    if retired_covered_page_count:
        hash_payload["retired_covered_page_count"] = retired_covered_page_count
        values["retired_covered_page_count"] = retired_covered_page_count
    if retired_covered_page_types:
        hash_payload["retired_covered_page_types"] = sorted(retired_covered_page_types)
        values["retired_covered_page_types"] = retired_covered_page_types
    if retired_popup_counts:
        hash_payload["retired_popup_attempt_counts"] = dict(
            sorted(retired_popup_counts.items())
        )
        values["retired_popup_attempt_counts"] = retired_popup_counts
    values["payload_hash"] = stable_protocol_hash(hash_payload)
    return values


def _rejection_reason(
    snapshot: H5TaskFrontierSnapshot, candidate: CandidateAction, max_depth: int | None
) -> str | None:
    page_type = _expected_page_type(candidate)
    policy = snapshot.sampling_policy_summary
    excluded = set(policy["exclude_page_types"])
    included = set(policy["include_page_types"])
    if not _is_platform_control_candidate(candidate):
        if page_type in excluded:
            return _BLOCK_REASON_EXCLUDED
        # 选了质检范围时，所选页面类型还没走齐就允许经过没选中的页面去找；走齐后不再往外走
        if included and page_type not in included and included <= snapshot.covered_page_types:
            return _BLOCK_REASON_NOT_INCLUDED
    if max_depth is not None and candidate.navigation_depth > max_depth:
        return _BLOCK_REASON_DEPTH
    if candidate.risk_level is InteractionRiskLevel.HIGH:
        return _BLOCK_REASON_RISK
    if (
        not _is_platform_control_candidate(candidate)
        and snapshot.max_pages is not None
        and snapshot.covered_page_count >= snapshot.max_pages
    ):
        return _BLOCK_REASON_MAX_PAGES
    return None


def _rejection_status(reason: str | None) -> H5FrontierEntryStatus:
    if reason is None:
        return H5FrontierEntryStatus.PENDING
    if reason in _NON_BLOCKING_REJECTION_REASONS:
        return H5FrontierEntryStatus.SKIPPED
    return H5FrontierEntryStatus.BLOCKED


def _selection_key(
    entry: H5TaskFrontierEntry,
    index: int,
    active: str | None,
    active_lineage: tuple[str, ...],
    priorities: frozenset[str],
    covered_page_types: frozenset[str],
    *,
    scheduler: Any | None = None,
    required_page_types: Collection[str] = (),
    evidence_gap_page_types: Collection[str] = (),
    missing_page_types: Collection[str] = (),
    local_novel_entry_pending: bool = False,
) -> tuple[int, ...]:
    if active is None:
        lineage_rank, distance = 1, 0
    elif entry.source_state_version == active:
        lineage_rank, distance = 0, 0
    else:
        common_length = _common_prefix_length(active_lineage, entry.lineage)
        lineage_rank = 1 if common_length else 2
        distance = len(active_lineage) - common_length
    # 被遮挡状态下先清障：关闭候选不清，页面上其余候选点了也会被
    # van-overlay 蒙层吞掉，白白烧掉整页尝试预算。
    obstruction_rank = 0 if _is_obstruction_close_candidate(entry.candidate) else 1
    detail_coverage_rank = (
        0 if _is_detail_scroll_coverage_candidate(entry.candidate) else 1
    )
    uncovered_branch_rank = (
        0
        if _is_uncovered_structural_branch_candidate(
            entry.candidate, covered_page_types=covered_page_types
        )
        else 1
    )
    # 页型已经覆盖不代表所有全局栏目都已探索，未执行的底栏不能被局部抽样饿死。
    bottom_branch_rank = 0 if _bottom_navigation_semantic_identity(entry.candidate) else 1
    list_coverage_rank = (
        0 if is_h5_list_scroll_coverage_candidate(entry.candidate) else 1
    )
    horizontal_collection_rank = (
        0 if _is_payment_horizontal_collection_candidate(entry.candidate) else 1
    )
    # 当前详情先完成已发现的本地取证，再返回父级。任意详情链接不享受
    # 此优先级；播放、分集和分享仍经过实际命中、风险及总预算校验。
    detail_evidence_rank = 0 if (
        entry.candidate.metadata.get("source_page_type") in {"video_detail", "content_detail"}
        and any(_candidate_has_keyword(entry.candidate, keyword) for keyword in (
            "detail_journey", "video_timed_capture", "player_seek_probe",
        ))
    ) else 1
    nested_return_rank = 0 if _is_nested_return_candidate(entry.candidate) else 1
    scheduler_rank = 1
    if scheduler is not None:
        queue_rank = getattr(scheduler, "queue_rank", None)
        if callable(queue_rank):
            scheduler_rank = int(
                queue_rank(
                    entry.candidate,
                    required_page_types=required_page_types,
                    evidence_gap_page_types=evidence_gap_page_types,
                    missing_page_types=missing_page_types,
                )
            )
    source_page_state = entry.candidate.metadata.get("source_page_state")
    # SPA 局部切换后 active state 可能尚未进入任务树；同语义候选都退化为
    # sibling 时，稳定页证据必须先于弹窗下透出的旧业务候选。
    obstructed_source_rank = (
        1
        if isinstance(source_page_state, str)
        and source_page_state.casefold() in {"popup", "modal", "overlay", "dialog", "obstructed"}
        and not _is_obstruction_close_candidate(entry.candidate)
        else 0
    )
    priority_rank = (
        0
        if _expected_page_type(entry.candidate) in priorities
        or entry.candidate.semantic_target in priorities
        else 1
    )
    expected_page_type = _expected_page_type(entry.candidate)
    # 同局部性与风险打平时，先完成可计算覆盖的规范页型；未知目标仍保留，
    # 在已知 sibling 耗尽后继续开放式探索。
    typed_target_rank = 0 if expected_page_type is not None else 1
    uncovered_rank = (
        1 if expected_page_type is not None and expected_page_type in covered_page_types else 0
    )
    # 当前个人页的结构能力一旦离开就可能因 SPA 状态恢复失败而丢失；
    # 当覆盖新旧相同，先完成本地设置/活动入口，再执行通用首页回访。
    profile_local_rank = 0 if _candidate_has_keyword(entry.candidate, "profile_local_action") else 1
    # 只对“个人中心受信本地动作 + 未覆盖规范页型”防饥饿；其他候选继续
    # 保持 LOW 先于 MEDIUM，避免扩大覆盖优先规则改变通用安全顺序。
    risk_rank = (
        0
        if expected_page_type is not None and uncovered_rank == 0 and profile_local_rank == 0
        else 1
        if entry.candidate.risk_level is InteractionRiskLevel.LOW
        else 2
    )
    # 不可延期的当前页取证、清障与返回保留原顺序；其余同风险候选先补新页型。
    business_goal_rank = (
        0 if entry.source_state_version == active and any(rank == 0 for rank in (
            obstruction_rank, detail_coverage_rank, detail_evidence_rank,
            horizontal_collection_rank,
            nested_return_rank if not local_novel_entry_pending else 1,
        )) else 1 if nested_return_rank != 0
        and expected_page_type is not None and uncovered_rank == 0
        and risk_rank <= 1 else 2
    )
    # 未覆盖的受控全局入口先于局部内容抽样，避免有限预算耗尽前永远到不了个人中心。
    # 当前页清障、支付横向取证和真实详情后验仍由 business_goal_rank 优先保护。
    global_coverage_rank = 0 if (
        bottom_branch_rank == 0
        and not local_novel_entry_pending
        and uncovered_rank == 0
        and expected_page_type is not None
        and entry.candidate.risk_level is InteractionRiskLevel.LOW
        and entry.candidate.metadata.get("source_page_state") == "stable"
        and h5_global_bottom_navigation_index(entry.candidate) is not None
    ) else 1
    return (
        business_goal_rank,
        global_coverage_rank,
        lineage_rank,
        obstruction_rank,
        detail_coverage_rank,
        uncovered_branch_rank,
        bottom_branch_rank,
        list_coverage_rank,
        horizontal_collection_rank,
        detail_evidence_rank,
        nested_return_rank,
        scheduler_rank,
        risk_rank,
        distance,
        obstructed_source_rank,
        typed_target_rank,
        uncovered_rank,
        profile_local_rank,
        priority_rank,
        index,
    )


def _candidate_has_keyword(candidate: CandidateAction, keyword: str) -> bool:
    raw = candidate.metadata.get("target_keywords")
    if not isinstance(raw, Sequence) or isinstance(raw, str | bytes):
        return False
    normalized = keyword.casefold()
    return any(isinstance(item, str) and item.strip().casefold() == normalized for item in raw)


def _is_detail_scroll_coverage_candidate(candidate: CandidateAction) -> bool:
    parameters = candidate.metadata.get("action_parameters")
    return bool(
        candidate.action_type is InteractionOperation.SCROLL
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.navigation_depth == 0
        and _candidate_has_keyword(candidate, "detail_scroll_coverage")
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "h5_detail_scroll_coverage"
        and parameters.get("scroll_max_segments") == 2
        and parameters.get("split_scroll_screenshots") is True
    )


def is_h5_list_scroll_coverage_candidate(candidate: CandidateAction) -> bool:
    parameters = candidate.metadata.get("action_parameters")
    return bool(
        candidate.action_type is InteractionOperation.SCROLL
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.navigation_depth == 0
        and _candidate_has_keyword(candidate, "list_scroll_coverage")
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "h5_list_scroll_coverage"
        and parameters.get("split_scroll_screenshots") is True
        and isinstance(parameters.get("scroll_max_segments"), int)
        and 1 <= parameters["scroll_max_segments"] <= 8
    )


def _is_uncovered_structural_branch_candidate(
    candidate: CandidateAction, *, covered_page_types: frozenset[str]
) -> bool:
    expected = _expected_page_type(candidate)
    source = candidate.metadata.get("source_page_type")
    if (
        expected is None
        or expected in covered_page_types
        or expected == str(source or "").strip().casefold()
    ):
        return False
    if _is_distinct_navigation_branch_candidate(candidate):
        return True
    parameters = candidate.metadata.get("action_parameters")
    return bool(
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and _candidate_has_keyword(candidate, "list_detail_navigation")
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "list_detail_navigation"
    )


def _is_payment_horizontal_collection_candidate(candidate: CandidateAction) -> bool:
    parameters = candidate.metadata.get("action_parameters")
    return bool(
        candidate.action_type is InteractionOperation.SCROLL
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.navigation_depth == 0
        and _candidate_has_keyword(candidate, "payment_horizontal_collection")
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "h5_payment_horizontal_collection"
        and parameters.get("horizontal_scroll_max_segments") == 6
        and parameters.get("split_scroll_screenshots") is True
    )


def _lineage_for_state(
    entries: Sequence[H5TaskFrontierEntry], active: str | None
) -> tuple[str, ...]:
    """从快照历史解析 active state 的 lineage，未知状态退化为自身。"""
    if active is None:
        return ()
    for entry in reversed(entries):
        if entry.source_state_version == active:
            return entry.lineage
    return (active,)


def _common_prefix_length(left: Sequence[str], right: Sequence[str]) -> int:
    common = 0
    for left_item, right_item in zip(left, right, strict=False):
        if left_item != right_item:
            break
        common += 1
    return common


def _priority_page_types(policy: Mapping[str, object]) -> frozenset[str]:
    return frozenset(policy["journey_steps"])


def _lineage(
    snapshot: H5TaskFrontierSnapshot,
    state_version: str,
    parent: str | None,
    *,
    navigation_depth: int,
) -> tuple[str, ...]:
    if parent is None:
        return (state_version,)
    for entry in reversed(snapshot.entries):
        if entry.source_state_version == parent:
            if state_version != parent and entry.navigation_depth == navigation_depth:
                # 同层 Tab、底栏或 SPA 局部切换是 sibling，不应把每次切换
                # 继续追加成子孙链。按目标深度保留祖先前缀，再接当前状态。
                return _lineage_with_state(
                    entry.lineage[:navigation_depth],
                    state_version,
                )
            return _lineage_with_state(entry.lineage, state_version)
    return _lineage_with_state((parent,), state_version)


def _lineage_with_state(parent_lineage: Sequence[str], state_version: str) -> tuple[str, ...]:
    """把当前状态接到父谱系末尾；绕回旧状态时保留最近一次到达顺序。

    站内底栏往返会让探索绕回谱系里已出现的状态（A→B→A），直接追加会撞上
    lineage 唯一性校验并把整个 Run 打成 H5_EXPLORATION_TURN_FAILED。改为
    移除旧位置、以当前到达顺序收尾：谱系仍同时包含 parent 与当前状态，
    满足 entry 不变式，_common_prefix_length 也据最近路径排序候选。
    """
    ordered = tuple(item for item in parent_lineage if item != state_version)
    return _unique_texts((*ordered, state_version), "lineage")


def _candidate_fingerprint(state_version: str, candidate: CandidateAction) -> str:
    return "h5-candidate:" + stable_protocol_hash(
        {
            "state_version": state_version,
            "operation": candidate.action_type.value,
            "semantic_target": candidate.semantic_target,
            "expected_page_type": _expected_page_type(candidate),
            "bottom_navigation_identity": _bottom_navigation_semantic_identity(candidate),
            "page_tab_navigation_identity": _page_tab_navigation_semantic_identity(candidate),
            "popup_occurrence_candidate_id": (
                candidate.candidate_id if _is_obstruction_close_candidate(candidate) else None
            ),
            **(
                {"line_loading_occurrence_candidate_id": candidate.candidate_id}
                if _is_line_loading_observe_candidate(candidate)
                else {}
            ),
            **(
                {"startup_ad_countdown_occurrence_candidate_id": candidate.candidate_id}
                if _is_startup_ad_countdown_observe_candidate(candidate)
                else {}
            ),
            **(
                {"startup_ad_dismissal_occurrence_candidate_id": candidate.candidate_id}
                if _is_startup_ad_dismissal_candidate(candidate)
                else {}
            ),
            **(
                {"flutter_bootstrap_occurrence_candidate_id": candidate.candidate_id}
                if _is_flutter_bootstrap_observe_candidate(candidate)
                else {}
            ),
            **(
                {"insufficient_reobserve_occurrence_candidate_id": candidate.candidate_id}
                if _is_insufficient_observe_candidate(candidate)
                else {}
            ),
            **(
                {"ambiguous_launch_shell_occurrence_candidate_id": (candidate.candidate_id)}
                if _is_ambiguous_launch_shell_observe_candidate(candidate)
                else {}
            ),
            **(
                {"logical_reobserve_occurrence_candidate_id": candidate.candidate_id}
                if _is_logical_surface_observe_candidate(candidate)
                else {}
            ),
            **(
                {"action_reobservation_capture_id": (_action_reobservation_capture_id(candidate))}
                if _action_reobservation_capture_id(candidate) is not None
                else {}
            ),
        }
    )


def _frontier_entry_id(
    *,
    source_state_version: str,
    source_capture_id: str,
    candidate: CandidateAction,
    semantic_key: tuple[str, str, str | None, str, str | None, str | None, str | None],
) -> str:
    """按动作与语义分支共同签发唯一 Frontier 条目身份。"""

    return "h5-frontier-entry:" + stable_protocol_hash(
        {
            "source_state_version": source_state_version,
            "source_capture_id": source_capture_id,
            "fingerprint": _candidate_fingerprint(source_state_version, candidate),
            "semantic_key": semantic_key,
        }
    )


def _semantic_candidate_key(
    candidate: CandidateAction,
    *,
    source_state_version: str,
    source_location: str,
) -> tuple[str, str, str | None, str, str | None, str | None, str | None]:
    """普通导航跨状态去重；弹窗按来源路由、页型、签名和关闭能力去重。

    Capture 与 candidate_id 都是证据/执行身份，不是弹窗层的业务身份。旧实现
    把 candidate_id 拼进 popup state_scope，导致动作前后 Capture 每次产生一个
    新 Frontier entry；同一短命弹窗因而能够跨工作无限重生。真实不同弹层必须
    由检测器签发不同的 obstruction_signature 或关闭能力，才能保留为不同候选。
    """
    state_scope: str | None = None
    if _is_obstruction_close_candidate(candidate):
        # 同一 source route/page type/signature/capability 即同一可见弹层；
        # candidate_id 会随 Capture 变化，只能用于证据和执行幂等，不能参与
        # Frontier 语义去重。
        obstruction_signature = _obstruction_signature(candidate) or source_state_version
        obstruction_capability = _obstruction_capability(candidate)
        state_scope = (
            f"{obstruction_signature}:{obstruction_capability}"
            if obstruction_capability is not None
            else obstruction_signature
        )
    elif _is_line_loading_observe_candidate(candidate):
        state_scope = candidate.candidate_id
    elif _is_startup_ad_countdown_observe_candidate(candidate):
        state_scope = candidate.candidate_id
    elif _is_startup_ad_dismissal_candidate(candidate):
        state_scope = candidate.candidate_id
    elif _is_flutter_bootstrap_observe_candidate(candidate):
        state_scope = candidate.candidate_id
    elif _is_insufficient_observe_candidate(candidate):
        state_scope = candidate.candidate_id
    elif _is_ambiguous_launch_shell_observe_candidate(candidate):
        state_scope = candidate.candidate_id
    elif _is_logical_surface_observe_candidate(candidate):
        state_scope = candidate.candidate_id
    elif (reobservation_capture_id := _action_reobservation_capture_id(candidate)) is not None:
        state_scope = f"action-reobservation:{reobservation_capture_id}:{candidate.candidate_id}"
    elif _is_nested_return_candidate(candidate):
        source_page_type = candidate.metadata.get("source_page_type")
        state_scope = (
            f"nested-return:{source_page_type.casefold()}"
            if isinstance(source_page_type, str) and source_page_type.strip()
            else f"nested-return:{source_state_version}"
        )
    elif is_h5_list_scroll_coverage_candidate(candidate):
        # 同一逻辑页的不同栏目分别取证；回访和焦点变化不能重生滚动任务。
        identity = candidate.metadata.get("source_identity_fingerprint")
        context = candidate.metadata.get("bounded_capture_context")
        if isinstance(identity, str) and identity:
            state_scope = stable_protocol_hash({
                "identity": identity,
                "tab": dict(context) if isinstance(context, Mapping) else None,
            })
    source_page_type = candidate.metadata.get("source_page_type")
    source_page_state = candidate.metadata.get("source_page_state")
    if _is_obstruction_close_candidate(candidate):
        # page_state 是本次识别状态，可能在 popup/locked 等值之间漂移；稳定弹层
        # 身份已经由来源路由、业务页型、签名和关闭能力充分刻画。
        source_page_state = None
    semantic_target = candidate.semantic_target
    expected_page_type = _expected_page_type(candidate)
    source_route_identity = canonical_h5_route_identity(source_location)
    bottom_navigation_identity = _bottom_navigation_semantic_identity(candidate)
    if bottom_navigation_identity is not None:
        navigation_family, navigation_label = bottom_navigation_identity
        semantic_target = f"bottom_navigation:{navigation_family}:{navigation_label}"
        # Flutter 当前激活项可能没有文本，导致同一标签在不同 Tab 状态下的
        # nth 索引漂移；底栏是全局导航，不能再按来源页型重复探索。
        source_page_type = None
        source_page_state = None
        source_route_identity = "global-bottom-navigation"
    page_tab_identity = _page_tab_navigation_semantic_identity(candidate)
    if page_tab_identity is not None:
        semantic_target = f"page_tab:{page_tab_identity}"
        branch_context = candidate.metadata.get("source_branch_context")
        if isinstance(branch_context, str) and branch_context:
            state_scope = f"page-tab-parent:{branch_context}"
            source_page_type = None
            source_page_state = None
        # 页型是动作结果假设，不是平台确定性页签的业务身份。同一页签不能因
        # 模型先后猜成 home/content_list 而重复进入 Frontier。结构模式签名和
        # nth 序号只用于当前 Snapshot 重定位；横向滚动会改变可见切片和序号，
        # 不能把它们作为业务分支身份，否则同一栏目会跨状态反复重生。
        expected_page_type = None
    return (
        candidate.action_type.value,
        semantic_target,
        expected_page_type,
        source_route_identity,
        source_page_type.strip().casefold()
        if isinstance(source_page_type, str) and source_page_type.strip()
        else None,
        source_page_state.strip().casefold()
        if isinstance(source_page_state, str) and source_page_state.strip()
        else None,
        state_scope,
    )


def _bottom_navigation_semantic_identity(
    candidate: CandidateAction,
) -> tuple[str, str] | None:
    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(parameters, Mapping) or parameters.get("target_mode") != "bottom_navigation":
        return None
    label = parameters.get("navigation_label")
    if not isinstance(label, str) or not label.strip():
        return None
    family = parameters.get("navigation_family")
    normalized_family = (
        family.strip().casefold() if isinstance(family, str) and family.strip() else "unknown"
    )
    return normalized_family, label.strip().casefold()


def _page_tab_navigation_semantic_identity(
    candidate: CandidateAction,
) -> str | None:
    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(parameters, Mapping) or parameters.get("target_mode") != (
        "page_tab_navigation"
    ):
        return None
    index = parameters.get("navigation_index")
    label = parameters.get("navigation_label")
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or not 1 <= index <= 50
        or not isinstance(label, str)
        or not label.strip()
    ):
        return None
    return label.strip().casefold()


def _is_distinct_navigation_branch_candidate(candidate: CandidateAction) -> bool:
    """识别由平台签发且有稳定标签的底栏或页内栏目分支。"""

    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return False
    target_mode = parameters.get("target_mode")
    if target_mode not in {"bottom_navigation", "page_tab_navigation"}:
        return False
    label = parameters.get("navigation_label")
    return bool(
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and isinstance(label, str)
        and label.strip()
    )


def _trusted_profile_local_refresh_candidate(candidate: CandidateAction) -> bool:
    """只允许平台精确签发的“我的”局部入口刷新 pending 来源证据。"""
    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return False
    target_mode = parameters.get("target_mode")
    if not isinstance(target_mode, str):
        return False
    contract = _PROFILE_LOCAL_REFRESH_CONTRACTS.get(target_mode)
    if contract is None:
        return False
    capability_key, expected_page_type = contract
    capability = parameters.get(capability_key)
    keywords = candidate.metadata.get("target_keywords")
    normalized_keywords = (
        {str(item).strip().casefold() for item in keywords if str(item).strip()}
        if isinstance(keywords, (list, tuple, set, frozenset))
        else set()
    )
    return bool(
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.navigation_depth == 1
        and candidate.metadata.get("source_page_type") == "profile"
        and candidate.metadata.get("source_page_state") in {"stable", "normal", "loaded"}
        and candidate.metadata.get("expected_next_page_type") == expected_page_type
        and parameters.get("source_page_type") == "profile"
        and parameters.get("expected_page_type") == expected_page_type
        and isinstance(capability, str)
        and bool(capability.strip())
        and "profile_local_action" in normalized_keywords
        and f"page_type:{expected_page_type}" in normalized_keywords
    )


def canonical_h5_route_identity(location: str) -> str:
    """生成页面级 H5 路由身份；忽略普通锚点，但保留 SPA hash route。"""
    try:
        parsed = urlsplit(location.strip())
    except (TypeError, ValueError):
        return str(location).strip()
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return location.strip().split("#", 1)[0]
    scheme = parsed.scheme.casefold()
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError:
        return location.strip().split("#", 1)[0]
    netloc = host
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{netloc}:{port}"
    raw_path = parsed.path or "/"
    normalized_path = posixpath.normpath(raw_path)
    if not normalized_path.startswith("/"):
        normalized_path = "/" + normalized_path
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parsed.query, keep_blank_values=True)), doseq=True)
    fragment = parsed.fragment.strip()
    spa_route = fragment if fragment.startswith("/") or fragment.startswith("!/") else ""
    return urlunsplit((scheme, netloc, normalized_path, query, spa_route))


def _is_obstruction_close_candidate(candidate: CandidateAction) -> bool:
    raw_keywords = candidate.metadata.get("target_keywords")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return (
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.navigation_depth == 0
        and "popup_dismissal" in keywords
        and "close_control" in keywords
    )


def _is_line_loading_observe_candidate(candidate: CandidateAction) -> bool:
    raw_keywords = candidate.metadata.get("target_keywords")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return (
        candidate.action_type is InteractionOperation.OBSERVE
        and candidate.risk_level is InteractionRiskLevel.LOW
        and {"line_loading", "same_session_reobserve"}.issubset(keywords)
    )


def _is_startup_ad_countdown_observe_candidate(candidate: CandidateAction) -> bool:
    raw_keywords = candidate.metadata.get("target_keywords")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return (
        candidate.action_type is InteractionOperation.OBSERVE
        and candidate.risk_level is InteractionRiskLevel.LOW
        and {
            "startup_ad",
            "countdown",
            "same_session_reobserve",
        }.issubset(keywords)
    )


def _is_startup_ad_dismissal_candidate(candidate: CandidateAction) -> bool:
    parameters = candidate.metadata.get("action_parameters")
    return bool(
        candidate.action_type is InteractionOperation.CLICK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.navigation_depth == 0
        and isinstance(parameters, Mapping)
        and parameters.get("target_mode") == "startup_ad_dismissal"
        and parameters.get("startup_ad_capability") == "countdown_close"
    )


def _is_flutter_bootstrap_observe_candidate(candidate: CandidateAction) -> bool:
    raw_keywords = candidate.metadata.get("target_keywords")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return (
        candidate.action_type is InteractionOperation.OBSERVE
        and candidate.risk_level is InteractionRiskLevel.LOW
        and {"flutter_bootstrap", "same_session_reobserve"}.issubset(keywords)
    )


def _is_insufficient_observe_candidate(candidate: CandidateAction) -> bool:
    """识别普通未稳定页的有界同会话观察 occurrence。"""

    raw_keywords = candidate.metadata.get("target_keywords")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return bool(
        candidate.action_type is InteractionOperation.OBSERVE
        and candidate.risk_level is InteractionRiskLevel.LOW
        and {"insufficient", "same_session_reobserve"}.issubset(keywords)
        and isinstance(candidate.metadata.get("action_parameters"), Mapping)
        and candidate.metadata["action_parameters"].get("target_mode") == "insufficient_reobserve"
    )


def _is_ambiguous_launch_shell_observe_candidate(
    candidate: CandidateAction,
) -> bool:
    """识别生命周期页型冲突时的平台限次零触碰观察 occurrence。"""
    raw_keywords = candidate.metadata.get("target_keywords")
    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)) or not isinstance(
        parameters, Mapping
    ):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return bool(
        candidate.action_type is InteractionOperation.OBSERVE
        and candidate.risk_level is InteractionRiskLevel.LOW
        and {
            "ambiguous_launch_shell",
            "same_session_reobserve",
            "zero_touch",
        }.issubset(keywords)
        and parameters.get("target_mode") == "ambiguous_launch_shell_reobserve"
    )


def _is_logical_surface_observe_candidate(candidate: CandidateAction) -> bool:
    """识别确认 SPA/Flutter 页面由平台签发的逻辑转换复验 occurrence。"""
    raw_keywords = candidate.metadata.get("target_keywords")
    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)) or not isinstance(
        parameters, Mapping
    ):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return bool(
        candidate.action_type is InteractionOperation.OBSERVE
        and candidate.risk_level is InteractionRiskLevel.LOW
        and {"logical_surface", "same_session_reobserve"}.issubset(keywords)
        and parameters.get("target_mode") == "logical_surface_reobserve"
    )


def _is_popup_absence_observe_candidate(candidate: CandidateAction) -> bool:
    """识别弹窗已自动退场后由平台签发的一次性同会话观察。"""
    raw_keywords = candidate.metadata.get("target_keywords")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return (
        candidate.action_type is InteractionOperation.OBSERVE
        and candidate.risk_level is InteractionRiskLevel.LOW
        and {
            "popup_dismissal",
            "obstruction_absent",
            "same_session_reobserve",
        }.issubset(keywords)
        and _action_reobservation_capture_id(candidate) is not None
    )


def _is_nested_return_candidate(candidate: CandidateAction) -> bool:
    raw_keywords = candidate.metadata.get("target_keywords")
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return False
    keywords = {str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()}
    return (
        candidate.action_type is InteractionOperation.BACK
        and candidate.risk_level is InteractionRiskLevel.LOW
        and candidate.semantic_target == "browser_history"
        and "nested_return" in keywords
    )


def _is_platform_control_candidate(candidate: CandidateAction) -> bool:
    return (
        _is_obstruction_close_candidate(candidate)
        or _is_detail_scroll_coverage_candidate(candidate)
        or _is_payment_horizontal_collection_candidate(candidate)
        or _is_line_loading_observe_candidate(candidate)
        or _is_startup_ad_countdown_observe_candidate(candidate)
        or _is_startup_ad_dismissal_candidate(candidate)
        or _is_flutter_bootstrap_observe_candidate(candidate)
        or _is_ambiguous_launch_shell_observe_candidate(candidate)
        or _is_logical_surface_observe_candidate(candidate)
        or _is_popup_absence_observe_candidate(candidate)
    )


def _action_reobservation_capture_id(candidate: CandidateAction) -> str | None:
    value = candidate.metadata.get("action_reobservation_capture_id")
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        return None
    return value


def _semantic_reobservation_budget_exhausted(
    snapshot: H5TaskFrontierSnapshot,
    *,
    candidate: CandidateAction,
    source_location: str,
) -> bool:
    """重观察作用域候选在同页同语义 skipped 达到预算后不再重生。

    仅约束动作重观察重生的普通业务候选；弹窗清障走业务页 20 轮策略、
    线路加载走有界恢复，均不受本预算影响。
    """
    if _action_reobservation_capture_id(candidate) is None:
        return False
    if _is_platform_control_candidate(candidate):
        return False
    expected = _expected_page_type(candidate)
    attempts = sum(
        1
        for entry in snapshot.entries
        if entry.status is H5FrontierEntryStatus.SKIPPED
        and canonical_h5_route_identity(entry.source_location)
        == canonical_h5_route_identity(source_location)
        and entry.candidate.action_type is candidate.action_type
        and entry.candidate.semantic_target == candidate.semantic_target
        and _expected_page_type(entry.candidate) == expected
    )
    attempts += snapshot.retired_skip_counts.get(
        _skip_budget_key(candidate, source_location=source_location), 0
    )
    return attempts >= H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT


def _semantic_key_hash(semantic_key: tuple[str | None, ...]) -> str:
    """把语义去重键压成稳定哈希，供退休墓碑做集合成员判断。"""
    return "h5-semantic-key:" + stable_protocol_hash({"semantic_key": list(semantic_key)})


def _skip_budget_key(candidate: CandidateAction, *, source_location: str) -> str:
    """与重观察预算的同页同语义匹配口径一致的稳定计数键。"""
    return "h5-skip-budget:" + stable_protocol_hash(
        {
            "route": canonical_h5_route_identity(source_location),
            "action_type": candidate.action_type.value,
            "semantic_target": candidate.semantic_target,
            "expected_page_type": _expected_page_type(candidate),
        }
    )


def retired_h5_popup_attempt_count(
    snapshot: H5TaskFrontierSnapshot,
    *,
    page_key: str,
    obstruction_signature: str | None = None,
) -> int:
    """读取已退休弹窗动作的同页总数或同签名次数。"""
    try:
        normalized_page_key = require_text(page_key, "page_key")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc
    return snapshot.retired_popup_attempt_counts.get(
        _popup_attempt_counter_key(
            page_key=normalized_page_key,
            obstruction_signature=obstruction_signature,
        ),
        0,
    )


def _source_refresh_count(entry: H5TaskFrontierEntry) -> int:
    value = entry.candidate.metadata.get("source_refresh_attempt_count", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _can_refresh_stale_source(
    entry: H5TaskFrontierEntry, *, candidate: CandidateAction | None = None,
) -> bool:
    if (
        candidate is None and entry.status is H5FrontierEntryStatus.SKIPPED
        and entry.last_failure_reason == "h5_shared_source_restore_exhausted"
    ):
        # 压缩不能把精确来源的失败变成全局同名入口的永久拒绝缓存。
        return True
    if (
        entry.status is not H5FrontierEntryStatus.SKIPPED
        or _source_refresh_count(entry) >= H5_SEMANTIC_REOBSERVATION_RETRY_LIMIT
    ):
        return False
    if entry.last_failure_reason == "action_failure_reobserved:LOGICAL_SOURCE_STATE_STALE":
        return True
    if entry.last_failure_reason != "h5_shared_source_restore_exhausted":
        return False
    # 未执行的旧来源候选仍保留审计与有限刷新机会；新帧须独立证明原栏目身份。
    if candidate is None:
        return True
    identity = entry.candidate.metadata.get("source_identity_fingerprint")
    return bool(
        identity and candidate.metadata.get("source_identity_fingerprint") == identity
        and candidate.metadata.get("source_page_state") == "stable"
    )


def _independent_source_after_failed_restore(
    entry: H5TaskFrontierEntry, *, candidate: CandidateAction,
) -> bool:
    identity = candidate.metadata.get("source_identity_fingerprint")
    state = candidate.metadata.get("source_state_fingerprint")
    return bool(
        entry.status is H5FrontierEntryStatus.SKIPPED
        and entry.last_failure_reason == "h5_shared_source_restore_exhausted"
        and candidate.metadata.get("logical_target_required") is True
        and candidate.metadata.get("source_page_state") == "stable"
        and isinstance(identity, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", identity)
        and isinstance(state, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", state)
        and entry.candidate.metadata.get("source_identity_fingerprint")
        and entry.candidate.metadata.get("source_identity_fingerprint") != identity
        and _bottom_navigation_semantic_identity(candidate) is None
    )


def retire_h5_skipped_entries(
    snapshot: H5TaskFrontierSnapshot,
    *,
    max_retained_skipped: int = H5_RETAINED_SKIPPED_LIVE_LIMIT,
) -> H5TaskFrontierSnapshot:
    """把超出保留额的最旧 skipped entry 退休成墓碑。"""
    if not isinstance(snapshot, H5TaskFrontierSnapshot):
        raise TypeError("snapshot 必须为 H5TaskFrontierSnapshot")
    _non_negative_int(max_retained_skipped, "max_retained_skipped")
    skipped = [
        entry for entry in snapshot.entries
        if entry.status is H5FrontierEntryStatus.SKIPPED
        and not _can_refresh_stale_source(entry)
    ]
    if len(skipped) <= max_retained_skipped:
        return snapshot
    overflow = len(skipped) - max_retained_skipped
    return _retire_h5_terminal_entries(snapshot, skipped[:overflow])


def compact_h5_task_frontier(
    snapshot: H5TaskFrontierSnapshot,
    *,
    max_payload_bytes: int = H5_TASK_FRONTIER_TARGET_BYTES,
) -> H5TaskFrontierSnapshot:
    """在不减少活动候选的前提下，把 H5 Frontier 压到安全容量。

    先执行固定窗口退休，再按真实 JSON 字节数逐条退休最旧终态。pending、
    selected、blocked、failed 始终保留；若仅活动项已超限则明确失败，禁止
    保存一个随后无法读取的 Work。
    """
    if not isinstance(snapshot, H5TaskFrontierSnapshot):
        raise TypeError("snapshot 必须为 H5TaskFrontierSnapshot")
    _positive_int(max_payload_bytes, "max_payload_bytes")
    compacted = retire_h5_skipped_entries(snapshot)
    executed = [
        entry
        for entry in compacted.entries
        if entry.status is H5FrontierEntryStatus.EXECUTED
    ]
    if len(executed) > H5_RETAINED_EXECUTED_LIVE_LIMIT:
        compacted = _retire_h5_terminal_entries(
            compacted,
            executed[: len(executed) - H5_RETAINED_EXECUTED_LIVE_LIMIT],
        )
    lineage_compacted = False
    while _frontier_payload_bytes(compacted) > max_payload_bytes:
        retiring = next(
            (
                entry
                for entry in compacted.entries
                if entry.status
                in {H5FrontierEntryStatus.EXECUTED, H5FrontierEntryStatus.SKIPPED}
                and not _can_refresh_stale_source(entry)
            ),
            None,
        )
        if retiring is None:
            if not lineage_compacted:
                # 旧快照可能把同层 SPA/栏目切换串成很长的父子谱系，并把整条
                # 历史复制进每个活动候选。容量压力下仅保留根、直接父级和当前
                # 状态，候选、动作绑定和去重事实均不减少。
                normalized = _compact_h5_entry_lineages(compacted)
                lineage_compacted = True
                if _frontier_payload_bytes(normalized) < _frontier_payload_bytes(compacted):
                    compacted = normalized
                    continue
            raise H5FrontierValidationError(
                f"H5 Frontier 活动候选超出 {max_payload_bytes} 字节安全上限"
            )
        compacted = _retire_h5_terminal_entries(compacted, (retiring,))
    return compacted


def _compact_h5_entry_lineages(
    snapshot: H5TaskFrontierSnapshot,
) -> H5TaskFrontierSnapshot:
    """把候选谱系压成仍可校验的最小路径证明。"""

    entries = tuple(
        replace(entry, lineage=_compact_entry_lineage(entry))
        for entry in snapshot.entries
    )
    if entries == snapshot.entries:
        return snapshot
    return _new_snapshot(
        entries=entries,
        active_state_version=snapshot.active_state_version,
        visited_state_ids=snapshot.visited_state_ids,
        executed_candidate_ids=snapshot.executed_candidate_ids,
        covered_page_types=snapshot.covered_page_types,
        max_pages=snapshot.max_pages,
        sampling_policy_summary=snapshot.sampling_policy_summary,
        retired_semantic_keys=snapshot.retired_semantic_keys,
        retired_skip_counts=snapshot.retired_skip_counts,
        retired_visited_state_ids=snapshot.retired_visited_state_ids,
        retired_executed_candidate_ids=snapshot.retired_executed_candidate_ids,
        retired_action_fingerprints=snapshot.retired_action_fingerprints,
        retired_covered_page_count=snapshot.retired_covered_page_count,
        retired_covered_page_types=snapshot.retired_covered_page_types,
        retired_popup_attempt_counts=snapshot.retired_popup_attempt_counts,
    )


def _compact_entry_lineage(entry: H5TaskFrontierEntry) -> tuple[str, ...]:
    root_state = entry.lineage[0] if entry.lineage else None
    minimal_path = tuple(
        dict.fromkeys(
            state
            for state in (
                root_state,
                entry.parent_state_version,
                entry.source_state_version,
            )
            if state is not None
        )
    )
    return _unique_texts(
        minimal_path,
        "lineage",
    )


def _retire_h5_terminal_entries(
    snapshot: H5TaskFrontierSnapshot,
    entries: Sequence[H5TaskFrontierEntry],
) -> H5TaskFrontierSnapshot:
    retiring_entries = tuple(entries)
    if not retiring_entries:
        return snapshot
    if any(
        entry.status not in {H5FrontierEntryStatus.EXECUTED, H5FrontierEntryStatus.SKIPPED}
        for entry in retiring_entries
    ):
        raise H5FrontierValidationError("只能退休 executed 或 skipped entry")
    retiring = frozenset(entry.entry_id for entry in retiring_entries)
    retired_keys = set(snapshot.retired_semantic_keys)
    retired_counts = dict(snapshot.retired_skip_counts)
    retired_visited = set(snapshot.retired_visited_state_ids)
    retired_executed = set(snapshot.retired_executed_candidate_ids)
    retired_fingerprints = set(snapshot.retired_action_fingerprints)
    retired_covered_count = snapshot.retired_covered_page_count
    retired_covered_types = set(snapshot.retired_covered_page_types)
    retired_popup_counts = dict(snapshot.retired_popup_attempt_counts)
    for entry in retiring_entries:
        semantic_key = _semantic_candidate_key(
            entry.candidate,
            source_state_version=entry.source_state_version,
            source_location=entry.source_location,
        )
        if not (_is_obstruction_close_candidate(entry.candidate)
                and entry.status is H5FrontierEntryStatus.EXECUTED):
            retired_keys.add(_semantic_key_hash(semantic_key))
        retired_fingerprints.add(
            _candidate_fingerprint(entry.source_state_version, entry.candidate)
        )
        if entry.status is H5FrontierEntryStatus.SKIPPED:
            budget_key = _skip_budget_key(
                entry.candidate, source_location=entry.source_location
            )
            retired_counts[budget_key] = retired_counts.get(budget_key, 0) + 1
        else:
            retired_visited.add(entry.source_state_version)
            retired_executed.add(entry.candidate.candidate_id)
            if not _is_platform_control_candidate(entry.candidate):
                retired_covered_count += 1
                page_type = _expected_page_type(entry.candidate)
                if page_type is not None:
                    retired_covered_types.add(page_type)
        popup_facts = _retired_popup_attempt_facts(entry)
        if popup_facts is not None:
            page_key, signature = popup_facts
            for counter_key in (
                _popup_attempt_counter_key(
                    page_key=page_key,
                    obstruction_signature=None,
                ),
                _popup_attempt_counter_key(
                    page_key=page_key,
                    obstruction_signature=signature,
                ),
            ):
                retired_popup_counts[counter_key] = retired_popup_counts.get(counter_key, 0) + 1
    return _new_snapshot(
        entries=tuple(entry for entry in snapshot.entries if entry.entry_id not in retiring),
        active_state_version=snapshot.active_state_version,
        visited_state_ids=snapshot.visited_state_ids,
        executed_candidate_ids=snapshot.executed_candidate_ids,
        covered_page_types=frozenset(),
        max_pages=snapshot.max_pages,
        sampling_policy_summary=snapshot.sampling_policy_summary,
        retired_semantic_keys=frozenset(retired_keys),
        retired_skip_counts=retired_counts,
        retired_visited_state_ids=frozenset(retired_visited),
        retired_executed_candidate_ids=frozenset(retired_executed),
        retired_action_fingerprints=frozenset(retired_fingerprints),
        retired_covered_page_count=retired_covered_count,
        retired_covered_page_types=frozenset(retired_covered_types),
        retired_popup_attempt_counts=retired_popup_counts,
    )


def _frontier_payload_bytes(snapshot: H5TaskFrontierSnapshot) -> int:
    return len(
        json.dumps(
            snapshot.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    )


def _validated_retired_skip_counts(value: Mapping[str, int]) -> dict[str, int]:
    return _validated_positive_counts(value, "retired_skip_counts")


def _validated_positive_counts(value: Mapping[str, int], field_name: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise H5FrontierValidationError(f"{field_name} 必须为对象")
    result: dict[str, int] = {}
    for key, count in value.items():
        try:
            require_text(key, f"{field_name} key")
        except ProtocolContractError as exc:
            raise H5FrontierValidationError(str(exc)) from exc
        result[key] = _positive_int(count, f"{field_name} value")
    return result


def _popup_attempt_counter_key(
    *,
    page_key: str,
    obstruction_signature: str | None,
) -> str:
    return "h5-popup-attempt:" + stable_protocol_hash(
        {
            "page_key": page_key,
            "obstruction_signature": obstruction_signature,
        }
    )


def _retired_popup_attempt_facts(
    entry: H5TaskFrontierEntry,
) -> tuple[str, str] | None:
    if (
        entry.status not in _POPUP_ATTEMPT_TERMINAL_STATUSES
        or (entry.last_failure_reason or "").endswith(
            ("LOGICAL_SOURCE_STATE_STALE", "POPUP_MODAL_SCOPE_MISMATCH")
        )
        or
        entry.selected_at is None
        or entry.finished_at is None
        or entry.candidate.semantic_target != "关闭当前弹窗"
        or not _is_obstruction_close_candidate(entry.candidate)
    ):
        return None
    source_page_type = entry.candidate.metadata.get("source_page_type")
    if not isinstance(source_page_type, str):
        return None
    normalized_page_type = source_page_type.strip().casefold()
    if not normalized_page_type or normalized_page_type in {"line_loading", "startup_ad"}:
        return None
    try:
        parsed = urlsplit(entry.source_location.strip())
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    signature = _obstruction_signature(entry.candidate)
    if signature is None:
        return None
    parameters = entry.candidate.metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return None
    capability = parameters.get("popup_dismissal_capability")
    raw_keywords = entry.candidate.metadata.get("target_keywords")
    if not isinstance(capability, str) or not capability.strip():
        return None
    if not isinstance(raw_keywords, (list, tuple, set, frozenset)):
        return None
    keywords = {
        str(keyword).strip().casefold() for keyword in raw_keywords if str(keyword).strip()
    }
    if (
        parameters.get("target_mode") != "popup_dismissal"
        or parameters.get("obstruction_signature") != signature
        or not {
            "popup_dismissal",
            "close_control",
            capability.strip().casefold(),
        }.issubset(keywords)
    ):
        return None
    return (
        f"{canonical_h5_route_identity(entry.source_location)}|{normalized_page_type}",
        signature,
    )


def _obstruction_signature(candidate: CandidateAction) -> str | None:
    value = candidate.metadata.get("obstruction_signature")
    if not isinstance(value, str) or not value.startswith("h5-obstruction:"):
        return None
    digest = value.removeprefix("h5-obstruction:")
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    return value


def _obstruction_capability(candidate: CandidateAction) -> str | None:
    """读取精确弹窗关闭能力，避免同签名的不同真实控件互相吞并。"""
    parameters = candidate.metadata.get("action_parameters")
    if not isinstance(parameters, Mapping):
        return None
    value = parameters.get("popup_dismissal_capability")
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip().casefold()


def _expected_page_type(candidate: CandidateAction) -> str | None:
    value = candidate.metadata.get("expected_next_page_type")
    if value is None:
        return None
    try:
        return require_text(value, "expected_next_page_type")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc


def _freeze_policy(policy: Mapping[str, object], max_pages: int | None) -> Mapping[str, object]:
    try:
        source = require_mapping(policy, "sampling_policy_summary")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc
    unknown = set(source).difference(_POLICY_KEYS)
    if unknown:
        raise H5FrontierValidationError("sampling_policy_summary 包含未知字段")
    policy_max = source.get("max_pages", max_pages)
    if policy_max != max_pages:
        raise H5FrontierValidationError("sampling_policy_summary.max_pages 必须与 max_pages 一致")
    if max_pages is not None:
        _positive_int(max_pages, "max_pages")
    normalized = {"max_pages": max_pages}
    for key in _POLICY_KEYS - {"max_pages"}:
        normalized[key] = list(_unique_texts(source.get(key, ()), key))
    include = set(normalized["include_page_types"])
    exclude = set(normalized["exclude_page_types"])
    if include.intersection(exclude):
        raise H5FrontierValidationError("sampling_policy_summary 页型策略互相矛盾")
    try:
        return freeze_json_mapping(normalized, "sampling_policy_summary")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc


def _status_counts(entries: Sequence[H5TaskFrontierEntry]) -> tuple[int, int, int]:
    return (
        sum(entry.status is H5FrontierEntryStatus.PENDING for entry in entries),
        sum(entry.status is H5FrontierEntryStatus.BLOCKED for entry in entries),
        sum(entry.status is H5FrontierEntryStatus.FAILED for entry in entries),
    )


def h5_frontier_completion_blockers_are_explainable(
    frontier: H5TaskFrontierSnapshot,
) -> bool:
    """只允许探索预算/深度耗尽这类边界事实不阻断覆盖完成。"""

    for entry in frontier.entries:
        if entry.status is H5FrontierEntryStatus.FAILED:
            return False
        if (
            entry.status is H5FrontierEntryStatus.BLOCKED
            and entry.last_failure_reason not in _COMPLETION_ALLOWED_BLOCK_REASONS
        ):
            return False
    return True


def _coverage(entries: Sequence[H5TaskFrontierEntry]) -> tuple[int, frozenset[str]]:
    executed = [
        entry
        for entry in entries
        if entry.status is H5FrontierEntryStatus.EXECUTED
        and not _is_platform_control_candidate(entry.candidate)
    ]
    return len(executed), frozenset(
        page_type
        for entry in executed
        if (page_type := _expected_page_type(entry.candidate)) is not None
    )


def _execution_state(
    entries: Sequence[H5TaskFrontierEntry],
) -> tuple[frozenset[str], frozenset[str]]:
    executed = [entry for entry in entries if entry.status is H5FrontierEntryStatus.EXECUTED]
    return (
        frozenset(entry.source_state_version for entry in executed),
        frozenset(entry.candidate.candidate_id for entry in executed),
    )


def _entry_by_id(entries: Sequence[H5TaskFrontierEntry], entry_id: str) -> H5TaskFrontierEntry:
    try:
        require_text(entry_id, "entry_id")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc
    for entry in entries:
        if entry.entry_id == entry_id:
            return entry
    raise H5FrontierValidationError("entry_id 不存在于 Frontier")


def _read_fields(
    payload: Mapping[str, Any], *, known: set[str], required: set[str], field_name: str
) -> dict[str, Any]:
    try:
        source = require_mapping(payload, field_name)
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc
    unknown = set(source).difference(known)
    missing = required.difference(source)
    if unknown:
        raise H5FrontierValidationError(f"{field_name} 包含未知字段")
    if missing:
        raise H5FrontierValidationError(f"{field_name} 缺少必填字段")
    return dict(source)


def _validate_merge_input(
    capture_id: str,
    state: str,
    location: str,
    depth: int,
    parent: str | None,
    max_depth: int | None,
) -> None:
    try:
        require_text(capture_id, "source_capture_id")
        require_text(state, "source_state_version")
        require_text(location, "source_location")
        if parent is not None:
            require_text(parent, "parent_state_version")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc
    _non_negative_int(depth, "navigation_depth")
    if max_depth is not None:
        _non_negative_int(max_depth, "max_depth")


def _now(value: datetime | None) -> datetime:
    result = datetime.now(UTC) if value is None else value
    try:
        return require_aware_datetime(result, "now")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc


def _unique_texts(values: Sequence[Any], field_name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise H5FrontierValidationError(f"{field_name} 必须为数组")
    result = tuple(values)
    try:
        for value in result:
            require_text(value, field_name)
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc
    if len(set(result)) != len(result):
        raise H5FrontierValidationError(f"{field_name} 不能包含重复值")
    return result


def _text_set(values: Sequence[Any], field_name: str) -> frozenset[str]:
    return frozenset(_unique_texts(tuple(values), field_name))


def _non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise H5FrontierValidationError(f"{field_name} 必须为非负整数")
    return value


def _positive_int(value: Any, field_name: str) -> int:
    result = _non_negative_int(value, field_name)
    if result == 0:
        raise H5FrontierValidationError(f"{field_name} 必须为正整数")
    return result


def _optional_reason(value: Any, *, required: bool) -> None:
    if value is None and not required:
        return
    try:
        require_text(value, "last_failure_reason")
    except ProtocolContractError as exc:
        raise H5FrontierValidationError(str(exc)) from exc
