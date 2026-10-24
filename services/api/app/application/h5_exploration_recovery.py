"""H5 Frontier sibling 的确定性来源恢复与 rebaseline 编排。"""

from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlsplit

from services.api.app.application.h5_exploration_cancellation import H5ExplorationCanceled
from services.api.app.application.h5_exploration_observation import build_h5_frontier
from services.api.app.domain.exploration import ExplorationWork, FrontierSnapshot
from services.api.app.domain.exploration.h5_frontier import (
    H5FrontierEntryStatus,
    H5TaskFrontierEntry,
    H5TaskFrontierSnapshot,
    canonical_h5_route_identity,
)
from services.api.app.domain.exploration.h5_structural_navigation import (
    h5_global_bottom_navigation_index as h5_global_bottom_navigation_index,
)

H5_FRONTIER_RESTORE_MAX_ATTEMPTS = 2
H5_FRONTIER_RESTORE_EXHAUSTED = "h5_frontier_restore_exhausted"
H5_FRONTIER_RESTORE_CROSS_ORIGIN = "h5_frontier_restore_cross_origin"
H5_FRONTIER_RESTORE_LOCATION_MISMATCH = "h5_frontier_restore_location_mismatch"
H5_FRONTIER_RESTORE_LOCATION_UNAVAILABLE = "h5_frontier_restore_location_unavailable"
H5_FRONTIER_RESTORE_REBASELINE_FAILED = "h5_frontier_restore_rebaseline_failed"
H5_FRONTIER_RESTORE_STATE_MISMATCH = "h5_frontier_restore_state_mismatch"
H5_FRONTIER_RESTORE_ASSESSMENT_REJECTED = "h5_frontier_restore_assessment_rejected"
H5_FRONTIER_RESTORE_REBASELINE_BLOCKED = "h5_frontier_restore_rebaseline_blocked"
H5_FRONTIER_RESTORE_STARTUP_AD_BLOCKED = "h5_frontier_restore_startup_ad_blocked"
H5_FRONTIER_RESTORE_POPUP_BLOCKED = "h5_frontier_restore_popup_blocked"
H5_FRONTIER_RESTORE_POPUP_DEADLINE = "h5_frontier_restore_popup_deadline_exceeded"

H5_BROWSER_RECOVERY_ATTEMPTS_EXHAUSTED = "h5_browser_recovery_attempts_exhausted"
H5_BROWSER_RECOVERY_CROSS_ORIGIN = "h5_browser_recovery_cross_origin"
H5_BROWSER_RECOVERY_ASSESSMENT_BLOCKED = "h5_browser_recovery_assessment_blocked"
H5_BROWSER_RECOVERY_STATE_MISMATCH = "h5_browser_recovery_state_mismatch"
H5_BROWSER_RECOVERY_FACTS_SCHEMA_VERSION = "h5-browser-recovery/v1"

class H5FailureStage(StrEnum):
    """浏览器丢失发生在 Capture Coordinator 的哪个不可逆边界。"""

    BEFORE_CAPTURE = "before_capture"
    ACTION = "action"
    AFTER_CAPTURE = "after_capture"


def build_h5_recovery_session_id(
    *,
    agent_run_id: str,
    work_id: str,
    stage: H5FailureStage,
    recovery_attempt_count: int,
) -> str:
    """从 canonical Run 身份和恢复事实派生非嵌套、可重放的 Session 身份。"""
    _require_text(agent_run_id, "agent_run_id")
    _require_text(work_id, "work_id")
    run_id = agent_run_id
    source_work_id = work_id
    if not isinstance(stage, H5FailureStage):
        raise TypeError("stage 必须为 H5FailureStage")
    if (
        isinstance(recovery_attempt_count, bool)
        or not isinstance(recovery_attempt_count, int)
        or recovery_attempt_count <= 0
    ):
        raise ValueError("recovery_attempt_count 必须为正整数")
    digest = hashlib.sha256(
        _canonical_json(
            {
                "agent_run_id": run_id,
                "work_id": source_work_id,
                "failure_stage": stage.value,
                "recovery_attempt_count": recovery_attempt_count,
            }
        ).encode("utf-8")
    ).hexdigest()[:20]
    return f"h5:{run_id}:recovery:{digest}"


@dataclass(frozen=True, slots=True)
class H5BrowserRecoveryFacts:
    """Work JSONB 中版本化、带内容哈希的恢复与状态等价权威事实。"""

    schema_version: str
    restoration_attempt_id: str
    state_equivalence_id: str
    task_id: str
    agent_run_id: str
    failed_work_id: str
    source_session_id: str
    target_session_id: str
    failure_stage: H5FailureStage
    recovery_attempt_count: int
    original_source_capture_id: str
    rebaseline_before_capture_id: str
    rebaseline_after_capture_id: str
    failed_action_execution_id: str | None
    restoration_action_execution_id: str
    inventory_status: str | None
    source_state_version: str
    target_state_version: str
    equivalent: bool
    reason_code: str | None
    original_action_replay_authorized: bool
    unknown_action_replayed: bool
    payload_hash: str

    def __post_init__(self) -> None:
        if self.schema_version != H5_BROWSER_RECOVERY_FACTS_SCHEMA_VERSION:
            raise ValueError("H5 恢复事实 schema_version 不受支持")
        for field_name in (
            "restoration_attempt_id",
            "state_equivalence_id",
            "task_id",
            "agent_run_id",
            "failed_work_id",
            "source_session_id",
            "target_session_id",
            "original_source_capture_id",
            "rebaseline_before_capture_id",
            "rebaseline_after_capture_id",
            "restoration_action_execution_id",
            "source_state_version",
            "target_state_version",
            "payload_hash",
        ):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.failure_stage, H5FailureStage):
            raise TypeError("failure_stage 必须为 H5FailureStage")
        if (
            isinstance(self.recovery_attempt_count, bool)
            or not isinstance(self.recovery_attempt_count, int)
            or self.recovery_attempt_count <= 0
        ):
            raise ValueError("recovery_attempt_count 必须为正整数")
        if self.failed_action_execution_id is not None:
            _require_text(self.failed_action_execution_id, "failed_action_execution_id")
        if self.inventory_status not in {None, "insufficient"}:
            raise ValueError("inventory_status 只允许 None 或 insufficient")
        if self.reason_code is not None:
            _require_text(self.reason_code, "reason_code")
        if self.equivalent is (self.reason_code is not None):
            raise ValueError("State Equivalence 成功当且仅当 reason_code 为空")
        if self.failure_stage is H5FailureStage.BEFORE_CAPTURE:
            if (
                self.recovery_attempt_count != 1
                or self.failed_action_execution_id is not None
                or self.inventory_status is not None
            ):
                raise ValueError(
                    "BEFORE_CAPTURE recovery facts 仅允许首次且不得包含未知 Action 库存"
                )
        else:
            if (
                self.recovery_attempt_count > 2
                or
                self.failed_action_execution_id is None
                or self.inventory_status != "insufficient"
            ):
                raise ValueError(
                    "未知 Action 恢复事实必须含 action_execution_id 和 insufficient 库存"
                )
        expected_authorized = self.failure_stage is H5FailureStage.BEFORE_CAPTURE
        if self.original_action_replay_authorized is not expected_authorized:
            raise ValueError("原 Action 重试授权与失败 stage 不一致")
        if self.unknown_action_replayed:
            raise ValueError("未知 Action 恢复事实不得声明已重放")
        canonical = f"h5:{self.agent_run_id}"
        recovery_prefix = f"{canonical}:recovery:"
        suffix = self.target_session_id.removeprefix(recovery_prefix)
        if (
            self.source_session_id != canonical
            and not self.source_session_id.startswith(recovery_prefix)
        ):
            raise ValueError("source_session_id 不属于 agent_run Session family")
        if self.source_session_id != canonical:
            source_suffix = self.source_session_id.removeprefix(recovery_prefix)
            if (
                len(source_suffix) != 20
                or any(character not in "0123456789abcdef" for character in source_suffix)
                or self.source_session_id.count(":recovery:") != 1
            ):
                raise ValueError("source_session_id 不是严格 H5 recovery Session 身份")
        if (
            not self.target_session_id.startswith(recovery_prefix)
            or len(suffix) != 20
            or any(character not in "0123456789abcdef" for character in suffix)
            or self.target_session_id.count(":recovery:") != 1
        ):
            raise ValueError("target_session_id 不是严格 H5 recovery Session 身份")
        if self.target_session_id != build_h5_recovery_session_id(
            agent_run_id=self.agent_run_id,
            work_id=self.failed_work_id,
            stage=self.failure_stage,
            recovery_attempt_count=self.recovery_attempt_count,
        ):
            raise ValueError("target_session_id 与失败 Work/attempt 内容不一致")
        expected_restoration, expected_equivalence = _recovery_fact_ids(
            self._identity_values()
        )
        if (
            self.restoration_attempt_id != expected_restoration
            or self.state_equivalence_id != expected_equivalence
        ):
            raise ValueError("H5 恢复事实 identity 与内容不一致")
        if self.payload_hash != _payload_hash(self._payload_values()):
            raise ValueError("H5 恢复事实 payload_hash 与内容不一致")

    @classmethod
    def create(
        cls,
        *,
        task_id: str,
        agent_run_id: str,
        failed_work_id: str,
        source_session_id: str,
        target_session_id: str,
        failure_stage: H5FailureStage,
        recovery_attempt_count: int,
        original_source_capture_id: str,
        rebaseline_before_capture_id: str,
        rebaseline_after_capture_id: str,
        failed_action_execution_id: str | None,
        restoration_action_execution_id: str,
        inventory_status: str | None,
        source_state_version: str,
        target_state_version: str,
        equivalent: bool,
        reason_code: str | None,
    ) -> H5BrowserRecoveryFacts:
        identity = {
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "failed_work_id": failed_work_id,
            "source_session_id": source_session_id,
            "target_session_id": target_session_id,
            "failure_stage": failure_stage.value,
            "recovery_attempt_count": recovery_attempt_count,
            "original_source_capture_id": original_source_capture_id,
            "rebaseline_before_capture_id": rebaseline_before_capture_id,
            "rebaseline_after_capture_id": rebaseline_after_capture_id,
            "failed_action_execution_id": failed_action_execution_id,
            "restoration_action_execution_id": restoration_action_execution_id,
        }
        restoration_id, equivalence_id = _recovery_fact_ids(identity)
        values = {
            "schema_version": H5_BROWSER_RECOVERY_FACTS_SCHEMA_VERSION,
            "restoration_attempt_id": restoration_id,
            "state_equivalence_id": equivalence_id,
            **identity,
            "inventory_status": inventory_status,
            "source_state_version": source_state_version,
            "target_state_version": target_state_version,
            "equivalent": equivalent,
            "reason_code": reason_code,
            "original_action_replay_authorized": (
                failure_stage is H5FailureStage.BEFORE_CAPTURE
            ),
            "unknown_action_replayed": False,
        }
        return cls(
            **{**values, "failure_stage": failure_stage},
            payload_hash=_payload_hash(values),
        )

    def _identity_values(self) -> dict[str, object]:
        return {
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "failed_work_id": self.failed_work_id,
            "source_session_id": self.source_session_id,
            "target_session_id": self.target_session_id,
            "failure_stage": self.failure_stage.value,
            "recovery_attempt_count": self.recovery_attempt_count,
            "original_source_capture_id": self.original_source_capture_id,
            "rebaseline_before_capture_id": self.rebaseline_before_capture_id,
            "rebaseline_after_capture_id": self.rebaseline_after_capture_id,
            "failed_action_execution_id": self.failed_action_execution_id,
            "restoration_action_execution_id": self.restoration_action_execution_id,
        }

    def _payload_values(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "restoration_attempt_id": self.restoration_attempt_id,
            "state_equivalence_id": self.state_equivalence_id,
            **self._identity_values(),
            "inventory_status": self.inventory_status,
            "source_state_version": self.source_state_version,
            "target_state_version": self.target_state_version,
            "equivalent": self.equivalent,
            "reason_code": self.reason_code,
            "original_action_replay_authorized": self.original_action_replay_authorized,
            "unknown_action_replayed": self.unknown_action_replayed,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._payload_values(), "payload_hash": self.payload_hash}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> H5BrowserRecoveryFacts:
        if not isinstance(payload, Mapping):
            raise TypeError("H5 恢复事实必须为 Mapping")
        expected = {
            "schema_version",
            "restoration_attempt_id",
            "state_equivalence_id",
            "task_id",
            "agent_run_id",
            "failed_work_id",
            "source_session_id",
            "target_session_id",
            "failure_stage",
            "recovery_attempt_count",
            "original_source_capture_id",
            "rebaseline_before_capture_id",
            "rebaseline_after_capture_id",
            "failed_action_execution_id",
            "restoration_action_execution_id",
            "inventory_status",
            "source_state_version",
            "target_state_version",
            "equivalent",
            "reason_code",
            "original_action_replay_authorized",
            "unknown_action_replayed",
            "payload_hash",
        }
        if set(payload) != expected:
            raise ValueError("H5 恢复事实字段集合不完整或包含未知字段")
        try:
            return cls(
                schema_version=str(payload["schema_version"]),
                restoration_attempt_id=str(payload["restoration_attempt_id"]),
                state_equivalence_id=str(payload["state_equivalence_id"]),
                task_id=str(payload["task_id"]),
                agent_run_id=str(payload["agent_run_id"]),
                failed_work_id=str(payload["failed_work_id"]),
                source_session_id=str(payload["source_session_id"]),
                target_session_id=str(payload["target_session_id"]),
                failure_stage=H5FailureStage(str(payload["failure_stage"])),
                recovery_attempt_count=_strict_int(
                    payload["recovery_attempt_count"], "recovery_attempt_count"
                ),
                original_source_capture_id=str(payload["original_source_capture_id"]),
                rebaseline_before_capture_id=str(payload["rebaseline_before_capture_id"]),
                rebaseline_after_capture_id=str(payload["rebaseline_after_capture_id"]),
                failed_action_execution_id=_optional_text_value(
                    payload["failed_action_execution_id"]
                ),
                restoration_action_execution_id=str(
                    payload["restoration_action_execution_id"]
                ),
                inventory_status=_optional_text_value(payload["inventory_status"]),
                source_state_version=str(payload["source_state_version"]),
                target_state_version=str(payload["target_state_version"]),
                equivalent=_strict_bool(payload["equivalent"], "equivalent"),
                reason_code=_optional_text_value(payload["reason_code"]),
                original_action_replay_authorized=_strict_bool(
                    payload["original_action_replay_authorized"],
                    "original_action_replay_authorized",
                ),
                unknown_action_replayed=_strict_bool(
                    payload["unknown_action_replayed"],
                    "unknown_action_replayed",
                ),
                payload_hash=str(payload["payload_hash"]),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"H5 恢复事实无效: {exc}") from exc


def _recovery_fact_ids(identity: Mapping[str, object]) -> tuple[str, str]:
    digest = hashlib.sha256(_canonical_json(identity).encode("utf-8")).hexdigest()
    return f"h5-restoration:{digest}", f"h5-state-equivalence:{digest}"


def _payload_hash(values: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(values).encode("utf-8")).hexdigest()


def _canonical_json(values: Mapping[str, object]) -> str:
    return json.dumps(
        dict(values),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _optional_text_value(value: object) -> str | None:
    if value is None:
        return None
    _require_text(value, "optional_text")
    return str(value)


def _strict_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} 必须为 bool")
    return value


def _strict_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} 必须为 int")
    return value


@dataclass(frozen=True, slots=True)
class H5RecoveryContext:
    """跨 Worker 重放所需的不可变 H5 浏览器恢复事实。"""

    task_id: str
    agent_run_id: str
    session_id: str
    stage: H5FailureStage
    last_safe_location: str
    action_execution_id: str | None
    frontier_snapshot: H5TaskFrontierSnapshot
    recovery_attempt_count: int
    max_recovery_attempts: int
    entry_origin: str | None = None
    failed_work: ExplorationWork | None = None
    failure_error_code: str | None = None
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        for field_name in ("task_id", "agent_run_id", "session_id"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.stage, H5FailureStage):
            raise TypeError("stage 必须为 H5FailureStage")
        _require_location(self.last_safe_location, "last_safe_location")
        if self.entry_origin is not None:
            _require_location(self.entry_origin, "entry_origin")
        if self.action_execution_id is not None:
            _require_text(self.action_execution_id, "action_execution_id")
        if not isinstance(self.frontier_snapshot, H5TaskFrontierSnapshot):
            raise TypeError("frontier_snapshot 必须为 H5TaskFrontierSnapshot")
        for field_name in (
            "recovery_attempt_count",
            "max_recovery_attempts",
            "elapsed_ms",
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.max_recovery_attempts == 0:
            raise ValueError("max_recovery_attempts 必须大于 0")
        if self.failed_work is not None and not isinstance(self.failed_work, ExplorationWork):
            raise TypeError("failed_work 必须为 ExplorationWork")


@dataclass(frozen=True, slots=True)
class H5RecoveryOutcome:
    """阶段恢复的审计结果；只有 BEFORE_CAPTURE 可能安全重试。"""

    retry_original_action: bool
    inventory_status: str | None
    supersede_failed_work: bool
    rebaseline_steps: tuple[str, ...]
    recovery_work: ExplorationWork | None
    recovery_attempt_count: int
    reason_code: str | None = None
    restoration_attempt_id: str | None = None
    equivalence_assessment_id: str | None = None


@dataclass(frozen=True, slots=True)
class H5RecoveryAttemptReservation:
    """恢复副作用前持久化的 attempt 与预算预留结果。"""

    work: ExplorationWork
    reserved: bool


@dataclass(frozen=True, slots=True)
class H5BrowserRecoveryAttemptFacts:
    """恢复副作用前写入 Work JSONB 的版本化、内容寻址 attempt 事实。"""

    task_id: str
    agent_run_id: str
    source_session_id: str
    failed_work_id: str
    source_state_version: str
    frontier_snapshot_id: str
    failure_stage: H5FailureStage
    recovery_attempt_count: int
    status: str
    error_code: str
    action_execution_id: str | None
    before_capture_id: str | None
    after_capture_id: str | None
    interaction_status: str | None
    interaction_result_capture_id: str | None
    started_at: str
    deadline: str
    remaining_duration_ms: int
    action_cost: int
    remaining_actions_after_reservation: int
    last_safe_location: str
    reason_code: str | None = None
    schema_version: str = "h5-browser-recovery-attempt/v1"
    payload_hash: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != "h5-browser-recovery-attempt/v1":
            raise ValueError("H5 browser recovery attempt schema_version 无效")
        if self.status not in {"started", "completed"}:
            raise ValueError("H5 browser recovery attempt status 无效")
        for name in (
            "task_id", "agent_run_id", "source_session_id", "failed_work_id",
            "source_state_version", "frontier_snapshot_id", "error_code",
            "started_at", "deadline",
        ):
            _require_text(getattr(self, name), name)
        _require_location(self.last_safe_location, "last_safe_location")
        for name in (
            "recovery_attempt_count", "remaining_duration_ms", "action_cost",
            "remaining_actions_after_reservation",
        ):
            _strict_int(getattr(self, name), name)
        if (
            self.recovery_attempt_count <= 0
            or self.remaining_duration_ms < 0
            or self.action_cost < 0
            or self.remaining_actions_after_reservation < 0
        ):
            raise ValueError("H5 browser recovery attempt 计数或预算无效")
        try:
            started = datetime.fromisoformat(self.started_at)
            deadline = datetime.fromisoformat(self.deadline)
        except ValueError as exc:
            raise ValueError("H5 browser recovery attempt 时间格式无效") from exc
        if (
            started.tzinfo is None or started.utcoffset() is None
            or deadline.tzinfo is None or deadline.utcoffset() is None
            or started > deadline
        ):
            raise ValueError("H5 browser recovery attempt 时间边界无效")
        if self.failure_stage is H5FailureStage.BEFORE_CAPTURE:
            if any(
                value is not None
                for value in (
                    self.action_execution_id, self.before_capture_id,
                    self.after_capture_id, self.interaction_status,
                    self.interaction_result_capture_id,
                )
            ):
                raise ValueError("BEFORE_CAPTURE attempt 不得包含未知 Action 事实")
        elif self.action_execution_id is None or self.before_capture_id is None:
            raise ValueError("unknown Action attempt 缺少 action/before capture 身份")
        expected_cost = 1 if self.failure_stage is H5FailureStage.BEFORE_CAPTURE else 2
        if self.action_cost not in {0, expected_cost} or (
            self.action_cost == 0
            and self.reason_code != "h5_browser_recovery_budget_exhausted"
        ):
            raise ValueError("H5 browser recovery attempt action_cost 与 stage 不一致")
        canonical = f"h5:{self.agent_run_id}"
        recovery = rf"h5:{re.escape(self.agent_run_id)}:recovery:[0-9a-f]{{20}}"
        if self.source_session_id != canonical and re.fullmatch(
            recovery, self.source_session_id
        ) is None:
            raise ValueError("attempt source_session_id 不属于 agent run Session family")
        for name in (
            "action_execution_id", "before_capture_id", "after_capture_id",
            "interaction_status", "interaction_result_capture_id", "reason_code",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_text(value, name)
        if self.payload_hash != _payload_hash(self._values()):
            raise ValueError("H5 browser recovery attempt payload_hash 与内容不一致")

    def _values(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "source_session_id": self.source_session_id,
            "failed_work_id": self.failed_work_id,
            "source_state_version": self.source_state_version,
            "frontier_snapshot_id": self.frontier_snapshot_id,
            "failure_stage": self.failure_stage.value,
            "recovery_attempt_count": self.recovery_attempt_count,
            "status": self.status,
            "error_code": self.error_code,
            "action_execution_id": self.action_execution_id,
            "before_capture_id": self.before_capture_id,
            "after_capture_id": self.after_capture_id,
            "interaction_status": self.interaction_status,
            "interaction_result_capture_id": self.interaction_result_capture_id,
            "started_at": self.started_at,
            "deadline": self.deadline,
            "remaining_duration_ms": self.remaining_duration_ms,
            "action_cost": self.action_cost,
            "remaining_actions_after_reservation": self.remaining_actions_after_reservation,
            "last_safe_location": self.last_safe_location,
            "reason_code": self.reason_code,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._values(), "payload_hash": self.payload_hash}

    def complete(self, *, reason_code: str | None) -> H5BrowserRecoveryAttemptFacts:
        return self.create(**{
            **self._values(),
            "schema_version": self.schema_version,
            "failure_stage": self.failure_stage,
            "status": "completed",
            "reason_code": reason_code,
        })

    @classmethod
    def create(cls, **kwargs: object) -> H5BrowserRecoveryAttemptFacts:
        stage = kwargs.get("failure_stage")
        if not isinstance(stage, H5FailureStage):
            raise TypeError("failure_stage 必须为 H5FailureStage")
        values = {
            "schema_version": "h5-browser-recovery-attempt/v1",
            **kwargs,
            "failure_stage": stage.value,
        }
        return cls(
            **{**values, "failure_stage": stage},
            payload_hash=_payload_hash(values),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> H5BrowserRecoveryAttemptFacts:
        expected = {
            "schema_version", "task_id", "agent_run_id", "source_session_id",
            "failed_work_id", "source_state_version", "frontier_snapshot_id",
            "failure_stage", "recovery_attempt_count", "status", "error_code",
            "action_execution_id", "before_capture_id", "after_capture_id",
            "interaction_status", "interaction_result_capture_id", "started_at",
            "deadline", "remaining_duration_ms", "action_cost",
            "remaining_actions_after_reservation", "last_safe_location",
            "reason_code", "payload_hash",
        }
        if not isinstance(payload, Mapping) or set(payload) != expected:
            raise ValueError("H5 browser recovery attempt 字段集合无效")
        return cls(
            schema_version=str(payload["schema_version"]),
            task_id=str(payload["task_id"]),
            agent_run_id=str(payload["agent_run_id"]),
            source_session_id=str(payload["source_session_id"]),
            failed_work_id=str(payload["failed_work_id"]),
            source_state_version=str(payload["source_state_version"]),
            frontier_snapshot_id=str(payload["frontier_snapshot_id"]),
            failure_stage=H5FailureStage(str(payload["failure_stage"])),
            recovery_attempt_count=_strict_int(
                payload["recovery_attempt_count"], "recovery_attempt_count"
            ),
            status=str(payload["status"]),
            error_code=str(payload["error_code"]),
            action_execution_id=_optional_text_value(payload["action_execution_id"]),
            before_capture_id=_optional_text_value(payload["before_capture_id"]),
            after_capture_id=_optional_text_value(payload["after_capture_id"]),
            interaction_status=_optional_text_value(payload["interaction_status"]),
            interaction_result_capture_id=_optional_text_value(
                payload["interaction_result_capture_id"]
            ),
            started_at=str(payload["started_at"]),
            deadline=str(payload["deadline"]),
            remaining_duration_ms=_strict_int(
                payload["remaining_duration_ms"], "remaining_duration_ms"
            ),
            action_cost=_strict_int(payload["action_cost"], "action_cost"),
            remaining_actions_after_reservation=_strict_int(
                payload["remaining_actions_after_reservation"],
                "remaining_actions_after_reservation",
            ),
            last_safe_location=str(payload["last_safe_location"]),
            reason_code=_optional_text_value(payload["reason_code"]),
            payload_hash=str(payload["payload_hash"]),
        )


class H5BrowserLossRecoveryRuntime(Protocol):
    """组合根提供的浏览器重开、重基线和事实持久化端口。"""

    async def checkpoint(self, *, context: H5RecoveryContext, stage: str) -> None: ...

    async def reopen(self, *, context: H5RecoveryContext) -> None: ...

    async def navigate(
        self,
        *,
        context: H5RecoveryContext,
        destination_url: str,
    ) -> None: ...

    async def capture(self, *, context: H5RecoveryContext) -> Any: ...

    async def detect(self, *, context: H5RecoveryContext, capture: Any) -> Any: ...

    async def assess(
        self,
        *,
        context: H5RecoveryContext,
        capture: Any,
        detection: Any,
    ) -> Any: ...

    async def state_equivalent(
        self,
        *,
        context: H5RecoveryContext,
        capture: Any,
        detection: Any,
        assessment: Any,
    ) -> bool: ...

    async def persist_recovery(
        self,
        *,
        context: H5RecoveryContext,
        capture: Any,
        detection: Any,
        assessment: Any,
        equivalent: bool,
        reason_code: str | None,
        recovery_attempt_count: int,
    ) -> ExplorationWork | None: ...


class H5ExplorationRecovery:
    """按不可逆阶段执行有界浏览器恢复，未知动作永不重放。"""

    def __init__(self, *, runtime: H5BrowserLossRecoveryRuntime) -> None:
        if runtime is None:
            raise TypeError("runtime 必须显式提供 H5 浏览器恢复端口")
        self._runtime = runtime

    async def recover(self, *, context: H5RecoveryContext) -> H5RecoveryOutcome:
        if not isinstance(context, H5RecoveryContext):
            raise TypeError("context 必须为 H5RecoveryContext")
        inventory_status = (
            None
            if context.stage is H5FailureStage.BEFORE_CAPTURE
            else "insufficient"
        )
        await self._checkpoint(context, "before_recovery_decision")
        if context.recovery_attempt_count >= context.max_recovery_attempts:
            return self._failed_outcome(
                context=context,
                inventory_status=inventory_status,
                reason_code=H5_BROWSER_RECOVERY_ATTEMPTS_EXHAUSTED,
            )
        if context.entry_origin is not None and not _same_origin(
            context.entry_origin,
            context.last_safe_location,
        ):
            return self._failed_outcome(
                context=context,
                inventory_status=inventory_status,
                reason_code=H5_BROWSER_RECOVERY_CROSS_ORIGIN,
            )

        await self._checkpoint(context, "before_reopen")
        await self._runtime.reopen(context=context)
        await self._checkpoint(context, "before_navigate")
        await self._runtime.navigate(
            context=context,
            destination_url=context.last_safe_location,
        )
        await self._checkpoint(context, "before_capture")
        capture = await self._runtime.capture(context=context)
        await self._checkpoint(context, "before_detect")
        detection = await self._runtime.detect(context=context, capture=capture)
        await self._checkpoint(context, "before_assess")
        assessment = await self._runtime.assess(
            context=context,
            capture=capture,
            detection=detection,
        )
        equivalent = False
        reason_code: str | None = None
        if not _browser_recovery_assessment_accepts(assessment):
            reason_code = H5_BROWSER_RECOVERY_ASSESSMENT_BLOCKED
        else:
            equivalent = await self._runtime.state_equivalent(
                context=context,
                capture=capture,
                detection=detection,
                assessment=assessment,
            )
            if not equivalent:
                reason_code = H5_BROWSER_RECOVERY_STATE_MISMATCH
        attempt_count = context.recovery_attempt_count + 1
        recovery_work = await self._runtime.persist_recovery(
            context=context,
            capture=capture,
            detection=detection,
            assessment=assessment,
            equivalent=equivalent,
            reason_code=reason_code,
            recovery_attempt_count=attempt_count,
        )
        if reason_code is not None:
            return H5RecoveryOutcome(
                retry_original_action=False,
                inventory_status=inventory_status,
                supersede_failed_work=False,
                rebaseline_steps=("capture", "detect", "assess"),
                recovery_work=recovery_work,
                recovery_attempt_count=attempt_count,
                reason_code=reason_code,
            )
        if context.stage is H5FailureStage.BEFORE_CAPTURE:
            await self._checkpoint(context, "before_retry_action")
            retry_original_action = True
            supersede_failed_work = True
        else:
            await self._checkpoint(context, "before_successor")
            retry_original_action = False
            supersede_failed_work = True
        return H5RecoveryOutcome(
            retry_original_action=retry_original_action,
            inventory_status=inventory_status,
            supersede_failed_work=supersede_failed_work,
            rebaseline_steps=("capture", "detect", "assess"),
            recovery_work=recovery_work,
            recovery_attempt_count=attempt_count,
        )

    async def _checkpoint(self, context: H5RecoveryContext, stage: str) -> None:
        await self._runtime.checkpoint(context=context, stage=stage)

    @staticmethod
    def _failed_outcome(
        *,
        context: H5RecoveryContext,
        inventory_status: str | None,
        reason_code: str,
    ) -> H5RecoveryOutcome:
        return H5RecoveryOutcome(
            retry_original_action=False,
            inventory_status=inventory_status,
            supersede_failed_work=False,
            rebaseline_steps=(),
            recovery_work=None,
            recovery_attempt_count=context.recovery_attempt_count,
            reason_code=reason_code,
        )


class H5FrontierRecoveryBrowser(Protocol):
    """恢复入口所需的最小浏览器能力，具体会话适配由组合根提供。"""

    async def back(self) -> str | None: ...

    async def navigate(self, destination_url: str) -> str | None: ...

    async def capture(self) -> Any: ...

    async def detect(self, *, capture: Any) -> Any: ...

    async def assess(self, *, capture: Any, detection: Any) -> Any: ...

    async def dismiss_startup_ad(self, *, capture: Any, detection: Any) -> Any | None: ...

    async def dismiss_popup(self, *, capture: Any, detection: Any) -> Any | None: ...


@dataclass(frozen=True, slots=True)
class H5RestorationResult:
    """一次 sibling 来源恢复的可审计结果。"""

    entry_id: str
    restored: bool
    restoration_attempt_count: int
    route: str | None = None
    reason_code: str | None = None
    capture: Any | None = None
    detection: Any | None = None
    assessment: Any | None = None
    failure_cause_code: str | None = None


async def restore_h5_frontier_source(
    *,
    entry: H5TaskFrontierEntry,
    current_location: str,
    browser: H5FrontierRecoveryBrowser,
    attempt_count: int | None = None,
    deadline: datetime | None = None,
    now: Any = None,
) -> H5RestorationResult:
    """恢复 selected sibling 的来源页面，并在返回前完成 capture、detect、assess。"""
    if not isinstance(entry, H5TaskFrontierEntry):
        raise TypeError("entry 必须为 H5TaskFrontierEntry")
    clock = now or (lambda: datetime.now(UTC))
    clearance_deadline = deadline or clock() + timedelta(minutes=10)
    attempts = entry.restoration_attempt_count if attempt_count is None else attempt_count
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 0:
        raise ValueError("attempt_count 必须为非负整数")
    if attempts >= H5_FRONTIER_RESTORE_MAX_ATTEMPTS:
        return H5RestorationResult(
            entry_id=entry.entry_id,
            restored=False,
            restoration_attempt_count=attempts,
            reason_code=H5_FRONTIER_RESTORE_EXHAUSTED,
        )
    # 空白页不是业务页面，但可用既有受控导航恢复来源；其他非法落点必须有界失败。
    blank_landing = current_location == "about:blank"
    if not blank_landing:
        try:
            _require_location(current_location, "current_location")
        except ValueError:
            return H5RestorationResult(
                entry_id=entry.entry_id,
                restored=False,
                restoration_attempt_count=attempts + 1,
                reason_code=H5_FRONTIER_RESTORE_LOCATION_UNAVAILABLE,
            )
    if not blank_landing and not _same_origin(entry.source_location, current_location):
        return H5RestorationResult(
            entry_id=entry.entry_id,
            restored=False,
            restoration_attempt_count=attempts + 1,
            reason_code=H5_FRONTIER_RESTORE_CROSS_ORIGIN,
        )

    route = (
        "back"
        if attempts == 0
        and not blank_landing
        and await _is_history_ancestor(
            browser=browser,
            source_location=entry.source_location,
            current_location=current_location,
        )
        else "navigate"
    )
    capture = detection = assessment = None
    failure_cause_code = None

    def failed(reason_code: str) -> H5RestorationResult:
        # 恢复失败不撤销已经发生的跳转；保留现场，下一次不能再绑定旧截图。
        return H5RestorationResult(
            entry_id=entry.entry_id,
            restored=False,
            restoration_attempt_count=attempts + 1,
            route=route,
            reason_code=reason_code,
            capture=capture,
            detection=detection,
            assessment=assessment,
            failure_cause_code=failure_cause_code,
        )

    try:
        location = (
            await browser.back()
            if route == "back"
            else await browser.navigate(entry.source_location)
        )
    except H5ExplorationCanceled:
        raise
    except Exception as error:  # noqa: BLE001 - 浏览器错误必须转换为可审计恢复结果
        failure_cause_code = getattr(error, "error_code", None) or type(error).__name__
        return failed(H5_FRONTIER_RESTORE_REBASELINE_FAILED)
    intermediate_global_navigation_back = bool(
        route == "back"
        and h5_global_bottom_navigation_index(entry.candidate) is not None
        and isinstance(location, str)
        and _same_origin(location, entry.source_location)
    )
    if (
        location is not None
        and not _same_location(location, entry.source_location)
        and not intermediate_global_navigation_back
    ):
        return failed(H5_FRONTIER_RESTORE_LOCATION_MISMATCH)
    try:
        capture = await browser.capture()
        detection = await browser.detect(capture=capture)
        assessment = await browser.assess(capture=capture, detection=detection)
    except H5ExplorationCanceled:
        raise
    except Exception as error:  # noqa: BLE001 - rebaseline 任一阶段失败均不得执行 sibling
        failure_cause_code = getattr(error, "error_code", None) or type(error).__name__
        return failed(H5_FRONTIER_RESTORE_REBASELINE_FAILED)
    if not _source_restoration_assessment_accepts(assessment):
        return failed(H5_FRONTIER_RESTORE_ASSESSMENT_REJECTED)
    startup_count = 0
    while (
        _is_retriggered_startup_ad(entry=entry, detection=detection)
        or _is_retriggered_source_popup(entry=entry, detection=detection)
    ):
        # 成功关闭不占层数额度；动作失败立即返回，持续成功仍受冻结期限约束。
        if clock() >= clearance_deadline:
            return failed(H5_FRONTIER_RESTORE_POPUP_DEADLINE)
        startup_ad = _is_retriggered_startup_ad(entry=entry, detection=detection)
        blocked_reason = (
            H5_FRONTIER_RESTORE_STARTUP_AD_BLOCKED
            if startup_ad else H5_FRONTIER_RESTORE_POPUP_BLOCKED
        )
        if startup_ad:
            if startup_count >= 1:
                return failed(blocked_reason)
            startup_count += 1
        dismiss = getattr(browser, "dismiss_startup_ad" if startup_ad else "dismiss_popup", None)
        if not callable(dismiss):
            return failed(blocked_reason)
        try:
            fresh_capture = await dismiss(capture=capture, detection=detection)
            if fresh_capture is None:
                return failed(blocked_reason)
            capture = fresh_capture
            detection = assessment = None
            detection = await browser.detect(capture=capture)
            assessment = await browser.assess(capture=capture, detection=detection)
        except H5ExplorationCanceled:
            raise
        except Exception as error:  # noqa: BLE001 - 弹窗清障失败仍按来源恢复失败闭合
            failure_cause_code = getattr(error, "error_code", None) or type(error).__name__
            return failed(H5_FRONTIER_RESTORE_REBASELINE_FAILED)
        if not _source_restoration_assessment_accepts(assessment):
            return failed(H5_FRONTIER_RESTORE_ASSESSMENT_REJECTED)
    rebaseline_frontier = _rebaseline_frontier(
        detection=detection,
        navigation_depth=entry.navigation_depth,
    )
    if rebaseline_frontier is None:
        return failed(H5_FRONTIER_RESTORE_STATE_MISMATCH)
    def source_matches(frontier: FrontierSnapshot) -> bool:
        return bool(
            frontier.observation.state_version == entry.source_state_version
            or _stable_dynamic_same_page_matches(entry=entry, route=route, frontier=frontier)
            or _stable_global_navigation_history_parent_matches(
                entry=entry, route=route, frontier=frontier,
            )
        )

    if not source_matches(rebaseline_frontier) and not _rebaseline_is_blocked(rebaseline_frontier):
        restore_path = getattr(browser, "restore_source_path", None)
        if callable(restore_path):
            try:
                replayed = await restore_path(entry=entry, capture=capture, detection=detection)
                if replayed is not None:
                    capture, detection = replayed.capture, replayed.detection
                    if not replayed.arrived:
                        failure_cause_code = replayed.failure_cause_code
                        return failed(H5_FRONTIER_RESTORE_STATE_MISMATCH)
                    assessment = await browser.assess(capture=capture, detection=detection)
                    if not _source_restoration_assessment_accepts(assessment):
                        return failed(H5_FRONTIER_RESTORE_ASSESSMENT_REJECTED)
                    rebaseline_frontier = _rebaseline_frontier(
                        detection=detection, navigation_depth=entry.navigation_depth,
                    )
            except H5ExplorationCanceled:
                raise
            except Exception as error:  # noqa: BLE001 - 路线错误不能跳过来源验证
                failure_cause_code = getattr(error, "error_code", None) or type(error).__name__
                return failed(H5_FRONTIER_RESTORE_REBASELINE_FAILED)
    if rebaseline_frontier is None or not source_matches(rebaseline_frontier):
        return failed(H5_FRONTIER_RESTORE_STATE_MISMATCH)
    if _rebaseline_is_blocked(rebaseline_frontier):
        return failed(H5_FRONTIER_RESTORE_REBASELINE_BLOCKED)
    return H5RestorationResult(
        entry_id=entry.entry_id,
        restored=True,
        restoration_attempt_count=attempts + 1,
        route=route,
        capture=capture,
        detection=detection,
        assessment=assessment,
    )


def apply_h5_frontier_restoration_result(
    *,
    snapshot: H5TaskFrontierSnapshot,
    result: H5RestorationResult,
) -> H5TaskFrontierSnapshot:
    """把一次恢复结果不可变写回快照；耗尽时以稳定原因终结 sibling。"""
    if not isinstance(snapshot, H5TaskFrontierSnapshot):
        raise TypeError("snapshot 必须为 H5TaskFrontierSnapshot")
    if not isinstance(result, H5RestorationResult):
        raise TypeError("result 必须为 H5RestorationResult")
    entry = next((item for item in snapshot.entries if item.entry_id == result.entry_id), None)
    if entry is None:
        raise ValueError("恢复结果引用的 entry 不存在于任务 Frontier")
    if entry.status is not H5FrontierEntryStatus.SELECTED:
        raise ValueError("只能写回 selected sibling 的恢复结果")
    if result.restoration_attempt_count not in {
        entry.restoration_attempt_count,
        entry.restoration_attempt_count + 1,
    }:
        raise ValueError("恢复结果的尝试次数与任务 Frontier 不一致")
    updated = snapshot
    if result.restoration_attempt_count == entry.restoration_attempt_count + 1:
        updated = snapshot.record_restoration_attempt(entry.entry_id)
    if not result.restored and result.restoration_attempt_count >= H5_FRONTIER_RESTORE_MAX_ATTEMPTS:
        return updated.mark_failed(
            entry.entry_id,
            reason=H5_FRONTIER_RESTORE_EXHAUSTED,
        )
    return updated


async def _is_history_ancestor(
    *,
    browser: H5FrontierRecoveryBrowser,
    source_location: str,
    current_location: str,
) -> bool:
    """优先使用会话适配器的真实历史判断，缺失时安全退化为受控导航。"""
    checker = getattr(browser, "is_history_ancestor", None)
    if not callable(checker):
        return False
    value = checker(source_location=source_location, current_location=current_location)
    if inspect.isawaitable(value):
        value = await value
    return value is True


def _same_origin(left: str, right: str) -> bool:
    left_origin = _normalized_origin(left)
    right_origin = _normalized_origin(right)
    return left_origin is not None and left_origin == right_origin


def _normalized_origin(location: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(location)
        scheme = parsed.scheme.casefold()
        if (
            scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        port = parsed.port
    except ValueError:
        return None
    return scheme, parsed.hostname.casefold(), port or (443 if scheme == "https" else 80)


def _same_location(left: str, right: str) -> bool:
    return canonical_h5_route_identity(left) == canonical_h5_route_identity(right)


def _assessment_accepts(assessment: object) -> bool:
    status = getattr(assessment, "execution_status", None)
    payload = getattr(assessment, "page_type_assessment", None)
    if isinstance(assessment, Mapping):
        status = assessment.get("execution_status")
        payload = assessment.get("page_type_assessment")
    if status == "skipped":
        return True
    if status == "accepted":
        return _covered_assessment_payload(payload)
    return False


def _source_restoration_assessment_accepts(assessment: object) -> bool:
    """来源恢复只豁免普通业务转场证明，不改变页面覆盖结论。"""
    if _assessment_accepts(assessment):
        return True
    status = getattr(assessment, "execution_status", None)
    payload = getattr(assessment, "page_type_assessment", None)
    if isinstance(assessment, Mapping):
        status = assessment.get("execution_status")
        payload = assessment.get("page_type_assessment")
    if (
        status != "accepted"
        or not isinstance(payload, Mapping)
        or payload.get("status") != "evidence_insufficient"
        or payload.get("reason_code") != "h5_logical_transition_untrusted"
        or not isinstance(payload.get("page_type"), str)
        or not str(payload["page_type"]).strip()
        or not all(
            _nonempty_text_sequence(payload.get(field_name))
            for field_name in (
                "page_capture_ids",
                "evidence_artifact_ids",
                "page_states",
            )
        )
    ):
        return False
    reason_codes = payload.get("reason_codes")
    return reason_codes is None or bool(
        _nonempty_text_sequence(reason_codes)
        and set(reason_codes) == {"h5_logical_transition_untrusted"}
    )


def _browser_recovery_assessment_accepts(assessment: object) -> bool:
    if isinstance(assessment, Mapping):
        direct_status = assessment.get("status")
        if direct_status in {"covered", "skipped"}:
            return True
        if direct_status in {
            "requires_auth",
            "blocked",
            "rejected",
            "evidence_insufficient",
        }:
            return False
    return _assessment_accepts(assessment)


def _covered_assessment_payload(payload: object) -> bool:
    if not isinstance(payload, Mapping) or payload.get("status") != "covered":
        return False
    if not all(
        isinstance(payload.get(field_name), str) and payload[field_name].strip()
        for field_name in ("page_type", "reason_code")
    ):
        return False
    return all(
        _nonempty_text_sequence(payload.get(field_name))
        for field_name in ("page_capture_ids", "evidence_artifact_ids", "page_states")
    )


def _nonempty_text_sequence(value: object) -> bool:
    return isinstance(value, (list, tuple)) and bool(value) and all(
        isinstance(item, str) and item.strip() for item in value
    )


def _rebaseline_frontier(*, detection: object, navigation_depth: int):
    if not isinstance(detection, Mapping):
        return None
    try:
        return build_h5_frontier(
            detection_result=detection,
            observed_at=datetime.now(UTC),
            navigation_depth=navigation_depth,
            logical_surface_effective=True,
        )
    except (TypeError, ValueError):
        return None


def _rebaseline_is_blocked(frontier: object) -> bool:
    observation = getattr(frontier, "observation", None)
    if observation is None:
        return True
    if getattr(observation, "overlay_signature", None) is not None:
        return True
    metadata = getattr(observation, "metadata", {})
    return isinstance(metadata, Mapping) and metadata.get("requires_auth") is True


def _is_retriggered_startup_ad(
    *,
    entry: H5TaskFrontierEntry,
    detection: object,
) -> bool:
    """只在恢复非开屏来源时把已识别开屏页视为一次性阻断层。"""
    if not isinstance(detection, Mapping):
        return False
    source_page_type = entry.candidate.metadata.get("source_page_type")
    return bool(
        detection.get("detection_status") == "recognized"
        and str(detection.get("page_type") or "").casefold() == "startup_ad"
        and _same_location(str(detection.get("page_url") or ""), entry.source_location)
        and isinstance(source_page_type, str)
        and source_page_type.casefold() != "startup_ad"
    )


def _is_retriggered_source_popup(
    *,
    entry: H5TaskFrontierEntry,
    detection: object,
) -> bool:
    """同一来源落点先清理已验签弹窗；清障后再核验业务页型与状态。"""
    if not isinstance(detection, Mapping):
        return False
    source_page_type = entry.candidate.metadata.get("source_page_type")
    detected_page_type = detection.get("page_type")
    risk_flags = detection.get("risk_flags") or ()
    return bool(
        detection.get("detection_status") == "recognized"
        and isinstance(source_page_type, str)
        and isinstance(detected_page_type, str)
        and source_page_type.casefold() not in {"startup_ad", "login", "register"}
        and detected_page_type.casefold() not in {"", "startup_ad", "login", "register"}
        and _same_location(str(detection.get("page_url") or ""), entry.source_location)
        and detection.get("requires_auth") is not True
        and isinstance(risk_flags, (list, tuple))
        and "authentication_required" not in risk_flags
        and str(detection.get("page_state") or "").casefold()
        in {"popup", "modal", "overlay", "dialog", "obstructed"}
    )


def _stable_dynamic_same_page_matches(
    *,
    entry: H5TaskFrontierEntry,
    route: str,
    frontier: object,
) -> bool:
    """内容漂移可重新观察，但不能把同 URL、同页型的不同 SPA 栏目当成来源页。

    任一侧启用了逻辑身份时，两侧必须同时提供一致的身份。没有逻辑身份的
    原 DOM 路由保留 URL 与页型检查，实际目标仍须由执行端重新验签。
    """
    del route
    observation = getattr(frontier, "observation", None)
    if observation is None or _rebaseline_is_blocked(frontier):
        return False
    metadata = getattr(observation, "metadata", {})
    candidate_metadata = entry.candidate.metadata
    expected_page_type = candidate_metadata.get("source_page_type")
    actual_page_type = getattr(observation, "page_type", None)
    location = getattr(observation, "location", None)
    expected_identity = candidate_metadata.get("source_identity_fingerprint")
    actual_identity = (
        metadata.get("logical_surface_identity_fingerprint")
        if isinstance(metadata, Mapping) else None
    )
    # 同 URL 的 SPA 栏目不能仅靠页型互换；内容漂移只允许同一逻辑身份。
    if expected_identity is not None or actual_identity is not None:
        if not expected_identity or expected_identity != actual_identity:
            return False
    return bool(
        isinstance(metadata, Mapping)
        and metadata.get("detection_status") == "recognized"
        and getattr(observation, "page_state", None) == "stable"
        and isinstance(expected_page_type, str)
        and expected_page_type
        and isinstance(actual_page_type, str)
        and (actual_page_type.casefold() == expected_page_type.casefold()
             or h5_frontier_type_drift_is_proven(entry=entry, frontier=frontier))
        and isinstance(location, str)
        and _same_location(location, entry.source_location)
    )


def h5_frontier_type_drift_is_proven(*, entry, frontier) -> bool:
    """只接受低风险正文标签漂移，重新选择新帧入口仍须独立动作验签。"""
    observation = frontier.observation
    source = entry.candidate.metadata
    current = observation.metadata
    types = {source.get("source_page_type"), observation.page_type}
    identity = source.get("source_identity_fingerprint")
    if (len(types) != 2 or not types <= {"home", "video_list", "content_list", "content_detail"}
            or not identity or identity != current.get("logical_surface_identity_fingerprint")
            or current.get("detection_status") != "recognized"
            or observation.page_state != "stable"
            or not _same_location(observation.location, entry.source_location)):
        return False
    if source.get("source_page_state") == "stable":
        return bool(source.get("source_document_identity")
            and source.get("source_document_identity")
            == current.get("logical_surface_document_identity")
            and source.get("source_state_fingerprint")
            and source.get("source_state_fingerprint")
            == current.get("logical_surface_state_fingerprint"))
    return bool(source.get("source_page_state") == "popup"
        and current.get("logical_clearance_proven") is True
        and current.get("logical_transition_before_identity") == identity
        and current.get("logical_transition_after_identity") == identity)




def _stable_global_navigation_history_parent_matches(
    *,
    entry: H5TaskFrontierEntry,
    route: str,
    frontier: object,
) -> bool:
    """未知子页只可回到重新证明同一全局底栏入口的中间历史父页。"""
    if route != "back":
        return False
    navigation_index = h5_global_bottom_navigation_index(entry.candidate)
    if navigation_index is None or _rebaseline_is_blocked(frontier):
        return False
    observation = getattr(frontier, "observation", None)
    if observation is None:
        return False
    metadata = getattr(observation, "metadata", {})
    location = getattr(observation, "location", None)
    page_type = (getattr(observation, "page_type", None) or "").casefold()
    page_state = (getattr(observation, "page_state", None) or "").casefold()
    current_navigation_indices = (
        metadata.get("global_bottom_navigation_indices")
        if isinstance(metadata, Mapping)
        else None
    )
    if not (
        isinstance(metadata, Mapping)
        and metadata.get("detection_status") == "recognized"
        and isinstance(location, str)
        and _same_origin(location, entry.source_location)
        and page_type not in {"", "line_loading", "startup_ad"}
        and (
            page_state == "stable"
            or (page_state == "empty" and metadata.get("detection_status") == "recognized")
        )
        and isinstance(current_navigation_indices, (list, tuple))
        and navigation_index in current_navigation_indices
    ):
        return False
    expected_parameters = dict(entry.candidate.metadata["action_parameters"])
    expected_page_type = entry.candidate.metadata.get("expected_next_page_type")
    return any(
        h5_global_bottom_navigation_index(candidate) == navigation_index
        and dict(candidate.metadata.get("action_parameters", {})) == expected_parameters
        and candidate.metadata.get("expected_next_page_type") == expected_page_type
        for candidate in getattr(frontier, "candidates", ())
    )


def _require_location(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空 URL")
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"{field_name} 必须为 HTTP(S) URL")


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
