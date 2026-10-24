"""App 页面探索技术失败的不可变审计事实。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    require_aware_datetime,
    stable_protocol_hash,
    thaw_json_mapping,
)

APP_EXPLORATION_FAILURE_PROTOCOL_VERSION = "app-exploration-failure.v1"


class AppExplorationFailureRecoveryState(StrEnum):
    """技术失败证据的后续对账状态。"""

    PENDING = "pending"
    EVIDENCE_SEALED = "evidence_sealed"
    NO_EVIDENCE = "no_evidence"
    RECONCILED = "reconciled"


@dataclass(frozen=True, kw_only=True)
class AppExplorationFailureFact:
    """业务 Result 之外的技术失败事实，不把异常伪装成探索结论。"""

    failure_id: str
    task_id: str
    agent_run_id: str
    surface_session_id: str
    phase: str
    reason_code: str
    error_type: str
    retryable: bool
    observed_at: datetime
    failure_sha256: str
    metadata: Mapping[str, Any]
    recovery_state: AppExplorationFailureRecoveryState = AppExplorationFailureRecoveryState.PENDING
    sealed_index_id: str | None = None
    result_id: str | None = None
    session_status: str | None = None
    reconciled_at: datetime | None = None
    protocol_version: str = APP_EXPLORATION_FAILURE_PROTOCOL_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "failure_id",
            "task_id",
            "agent_run_id",
            "surface_session_id",
            "phase",
            "reason_code",
            "error_type",
            "failure_sha256",
        ):
            _require_text(getattr(self, field_name), field_name)
        for field_name in ("sealed_index_id", "result_id", "session_status"):
            value = getattr(self, field_name)
            if value is not None:
                _require_text(value, field_name)
        if not isinstance(self.retryable, bool):
            raise ValueError("retryable 必须为布尔值")
        if not isinstance(self.recovery_state, AppExplorationFailureRecoveryState):
            raise ValueError("recovery_state 不受支持")
        try:
            require_aware_datetime(self.observed_at, "observed_at")
            if self.reconciled_at is not None:
                require_aware_datetime(self.reconciled_at, "reconciled_at")
            metadata = freeze_json_mapping(self.metadata, "metadata")
        except ProtocolContractError as exc:
            raise ValueError(str(exc)) from exc
        if self.protocol_version != APP_EXPLORATION_FAILURE_PROTOCOL_VERSION:
            raise ValueError("protocol_version 不受支持")
        if (
            self.recovery_state is AppExplorationFailureRecoveryState.PENDING
            and self.reconciled_at is not None
        ) or (
            self.recovery_state is not AppExplorationFailureRecoveryState.PENDING
            and self.reconciled_at is None
        ):
            raise ValueError("recovery_state 与 reconciled_at 不一致")
        if (
            self.recovery_state is AppExplorationFailureRecoveryState.EVIDENCE_SEALED
            and self.sealed_index_id is None
        ):
            raise ValueError("evidence_sealed 必须引用 sealed_index_id")
        if self.recovery_state is AppExplorationFailureRecoveryState.NO_EVIDENCE and (
            self.sealed_index_id is not None or self.result_id is not None
        ):
            raise ValueError("no_evidence 不能引用 Index 或 Result")
        expected = _failure_sha256(
            task_id=self.task_id,
            agent_run_id=self.agent_run_id,
            surface_session_id=self.surface_session_id,
            phase=self.phase,
            reason_code=self.reason_code,
            error_type=self.error_type,
            retryable=self.retryable,
            observed_at=self.observed_at,
            metadata=metadata,
        )
        if self.failure_sha256 != expected:
            raise ValueError("failure_sha256 与技术失败事实不一致")
        object.__setattr__(self, "metadata", metadata)

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "failure_id": self.failure_id,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "surface_session_id": self.surface_session_id,
            "phase": self.phase,
            "reason_code": self.reason_code,
            "error_type": self.error_type,
            "retryable": self.retryable,
            "observed_at": self.observed_at.isoformat(),
            "failure_sha256": self.failure_sha256,
            "metadata": thaw_json_mapping(self.metadata),
            "recovery_state": self.recovery_state.value,
            "sealed_index_id": self.sealed_index_id,
            "result_id": self.result_id,
            "session_status": self.session_status,
            "reconciled_at": (
                self.reconciled_at.isoformat() if self.reconciled_at is not None else None
            ),
        }


def build_app_exploration_failure_fact(
    *,
    task_id: str,
    agent_run_id: str,
    surface_session_id: str,
    phase: str,
    reason_code: str,
    error_type: str,
    retryable: bool,
    observed_at: datetime,
    metadata: Mapping[str, Any] | None = None,
) -> AppExplorationFailureFact:
    """按 Run 生成稳定身份；同一 Run 的技术终态只能首次落一份。"""

    values = metadata or {}
    failure_id = str(uuid5(NAMESPACE_URL, f"alioth:app-exploration-failure:{agent_run_id}"))
    digest = _failure_sha256(
        task_id=task_id,
        agent_run_id=agent_run_id,
        surface_session_id=surface_session_id,
        phase=phase,
        reason_code=reason_code,
        error_type=error_type,
        retryable=retryable,
        observed_at=observed_at,
        metadata=values,
    )
    return AppExplorationFailureFact(
        failure_id=failure_id,
        task_id=task_id,
        agent_run_id=agent_run_id,
        surface_session_id=surface_session_id,
        phase=phase,
        reason_code=reason_code,
        error_type=error_type,
        retryable=retryable,
        observed_at=observed_at,
        failure_sha256=digest,
        metadata=values,
    )


def _failure_sha256(
    *,
    task_id: str,
    agent_run_id: str,
    surface_session_id: str,
    phase: str,
    reason_code: str,
    error_type: str,
    retryable: bool,
    observed_at: datetime,
    metadata: Mapping[str, Any],
) -> str:
    # observed_at 由首次成功提交的数据库事实决定，重试不能因墙钟变化制造冲突。
    return "sha256:" + stable_protocol_hash(
        {
            "protocol_version": APP_EXPLORATION_FAILURE_PROTOCOL_VERSION,
            "task_id": task_id,
            "agent_run_id": agent_run_id,
            "surface_session_id": surface_session_id,
            "phase": phase,
            "reason_code": reason_code,
            "error_type": error_type,
            "retryable": retryable,
            "metadata": thaw_json_mapping(freeze_json_mapping(metadata, "metadata")),
        }
    )


def _require_text(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
