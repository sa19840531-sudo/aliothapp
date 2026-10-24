"""App 页面探索 Runtime 时间线与 PostgreSQL Work 恢复合同。"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from services.api.app.application.exploration_work_queue import ExplorerWorkStatus
from services.api.app.domain.evidence import AppExplorationResult
from services.api.app.domain.exploration import ExplorationTerminal, ExplorationWork
from services.api.app.domain.protocol import (
    ProtocolContractError,
    freeze_json_mapping,
    require_aware_datetime,
    stable_protocol_hash,
    thaw_json_mapping,
)

APP_EXPLORATION_RUNTIME_CHECKPOINT_VERSION = "app-exploration-runtime-checkpoint.v1"


class AppExplorationRuntimeRecoveryError(RuntimeError):
    """持久化时间线缺失、漂移或无法安全恢复。"""

    error_code = "APP_EXPLORATION_RUNTIME_RECOVERY_INVALID"


@dataclass(frozen=True, kw_only=True)
class AppExplorationRuntimeCheckpoint:
    """首次发布 Work 前冻结的 App Runtime 身份、预算和墙钟时间线。"""

    task_id: str
    agent_run_id: str
    surface_session_id: str
    platform_build_ref: str
    bootstrap_capture_id: str
    installed_package: str
    runtime_policy: Mapping[str, Any]
    runtime_policy_sha256: str
    objective_hash: str
    started_at: datetime
    deadline: datetime
    created_at: datetime
    protocol_version: str = APP_EXPLORATION_RUNTIME_CHECKPOINT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "agent_run_id",
            "surface_session_id",
            "platform_build_ref",
            "bootstrap_capture_id",
            "installed_package",
        ):
            _require_text(getattr(self, field_name), field_name)
        try:
            policy = freeze_json_mapping(self.runtime_policy, "runtime_policy")
            require_aware_datetime(self.started_at, "started_at")
            require_aware_datetime(self.deadline, "deadline")
            require_aware_datetime(self.created_at, "created_at")
        except ProtocolContractError as exc:
            raise AppExplorationRuntimeRecoveryError(str(exc)) from exc
        if self.deadline <= self.started_at:
            raise AppExplorationRuntimeRecoveryError("deadline 必须晚于 started_at")
        if self.protocol_version != APP_EXPLORATION_RUNTIME_CHECKPOINT_VERSION:
            raise AppExplorationRuntimeRecoveryError("Runtime Checkpoint 版本不受支持")
        if not _is_prefixed_sha256(self.runtime_policy_sha256):
            raise AppExplorationRuntimeRecoveryError("runtime_policy_sha256 格式无效")
        if not _is_plain_sha256(self.objective_hash):
            raise AppExplorationRuntimeRecoveryError("objective_hash 格式无效")
        expected_policy_hash = _runtime_policy_hash(policy)
        if self.runtime_policy_sha256 != expected_policy_hash:
            raise AppExplorationRuntimeRecoveryError("Runtime Policy hash 与内容不一致")
        embedded_hash = policy.get("policy_hash")
        if embedded_hash is not None and embedded_hash != expected_policy_hash:
            raise AppExplorationRuntimeRecoveryError("Runtime Policy 内嵌 hash 与内容不一致")
        object.__setattr__(self, "runtime_policy", policy)

    def validate_runtime_identity(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        surface_session_id: str,
        platform_build_ref: str,
        bootstrap_capture_id: str,
        installed_package: str,
        runtime_policy: Mapping[str, Any],
        objective: str,
    ) -> None:
        """恢复前拒绝 Task、包、Bootstrap、策略或目标发生漂移。"""

        if (
            self.task_id != task_id
            or self.agent_run_id != agent_run_id
            or self.surface_session_id != surface_session_id
            or self.platform_build_ref != platform_build_ref
            or self.bootstrap_capture_id != bootstrap_capture_id
            or self.installed_package != installed_package
            or self.runtime_policy_sha256 != _runtime_policy_hash(runtime_policy)
            or self.objective_hash != _objective_hash(objective)
        ):
            raise AppExplorationRuntimeRecoveryError("App Runtime Checkpoint 身份或策略已漂移")

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "task_id": self.task_id,
            "agent_run_id": self.agent_run_id,
            "surface_session_id": self.surface_session_id,
            "platform_build_ref": self.platform_build_ref,
            "bootstrap_capture_id": self.bootstrap_capture_id,
            "installed_package": self.installed_package,
            "runtime_policy": thaw_json_mapping(self.runtime_policy),
            "runtime_policy_sha256": self.runtime_policy_sha256,
            "objective_hash": self.objective_hash,
            "started_at": self.started_at.isoformat(),
            "deadline": self.deadline.isoformat(),
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, kw_only=True)
class AppExplorationWorkRecovery:
    """同一 App Run 的持久化 Work 时间线只读投影。"""

    initial_work: ExplorationWork | None
    latest_work: ExplorationWork | None
    terminal: ExplorationTerminal | None
    terminal_status: ExplorerWorkStatus | None
    terminal_error_code: str | None
    consumed_turn_count: int
    consumed_action_count: int
    has_nonterminal_work: bool
    next_claim_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.has_nonterminal_work, bool):
            raise AppExplorationRuntimeRecoveryError("has_nonterminal_work 必须为布尔值")
        for field_name in ("consumed_turn_count", "consumed_action_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise AppExplorationRuntimeRecoveryError(f"{field_name} 必须为非负整数")
        if (self.initial_work is None) != (self.latest_work is None):
            raise AppExplorationRuntimeRecoveryError("Work 时间线首尾事实不完整")
        if self.initial_work is None and (
            self.has_nonterminal_work
            or self.consumed_turn_count != 0
            or self.consumed_action_count != 0
            or self.terminal_error_code is not None
            or self.next_claim_at is not None
        ):
            raise AppExplorationRuntimeRecoveryError("无 Work 时不得携带恢复进度或重领时间")
        if (self.terminal is None) != (self.terminal_status is None):
            raise AppExplorationRuntimeRecoveryError("Terminal payload 与持久化状态必须同时存在")
        if self.terminal_status is not None and not self.terminal_status.is_terminal:
            raise AppExplorationRuntimeRecoveryError("terminal_status 必须为终态")
        if self.terminal_status is not None and self.has_nonterminal_work:
            raise AppExplorationRuntimeRecoveryError("终态 Work 与非终态 Work 不能并存")
        if (
            self.initial_work is not None
            and self.terminal is None
            and not self.has_nonterminal_work
        ):
            raise AppExplorationRuntimeRecoveryError("已有 Work 时必须处于非终态或携带 Terminal")
        if self.terminal is not None and self.next_claim_at is not None:
            raise AppExplorationRuntimeRecoveryError("终态 Work 不得携带重领时间")
        if self.terminal_error_code is not None:
            _require_text(self.terminal_error_code, "terminal_error_code")
        if self.next_claim_at is not None:
            try:
                require_aware_datetime(self.next_claim_at, "next_claim_at")
            except ProtocolContractError as exc:
                raise AppExplorationRuntimeRecoveryError(str(exc)) from exc

    @property
    def has_any_work(self) -> bool:
        return self.initial_work is not None


class AppExplorationRuntimeRecoveryRepository(Protocol):
    """Runtime 只通过该端口读取/冻结 App 专用恢复事实。"""

    async def get_checkpoint(
        self,
        *,
        agent_run_id: str,
    ) -> AppExplorationRuntimeCheckpoint | None: ...

    async def create_checkpoint(
        self,
        *,
        checkpoint: AppExplorationRuntimeCheckpoint,
    ) -> AppExplorationRuntimeCheckpoint: ...

    async def get_work_recovery(
        self,
        *,
        task_id: str,
        agent_run_id: str,
        surface_session_id: str,
    ) -> AppExplorationWorkRecovery: ...

    async def get_result(self, *, agent_run_id: str) -> AppExplorationResult | None: ...


def build_app_exploration_runtime_checkpoint(
    *,
    task_id: str,
    agent_run_id: str,
    surface_session_id: str,
    platform_build_ref: str,
    bootstrap_capture_id: str,
    installed_package: str,
    runtime_policy: Mapping[str, Any],
    objective: str,
    started_at: datetime,
    deadline: datetime,
    created_at: datetime,
) -> AppExplorationRuntimeCheckpoint:
    """从本次冻结输入构造可幂等保存的 Runtime Checkpoint。"""

    return AppExplorationRuntimeCheckpoint(
        task_id=task_id,
        agent_run_id=agent_run_id,
        surface_session_id=surface_session_id,
        platform_build_ref=platform_build_ref,
        bootstrap_capture_id=bootstrap_capture_id,
        installed_package=installed_package,
        runtime_policy=runtime_policy,
        runtime_policy_sha256=_runtime_policy_hash(runtime_policy),
        objective_hash=_objective_hash(objective),
        started_at=started_at,
        deadline=deadline,
        created_at=created_at,
    )


def _runtime_policy_hash(value: Mapping[str, Any]) -> str:
    policy = dict(value)
    policy.pop("policy_hash", None)
    return "sha256:" + stable_protocol_hash(policy)


def _objective_hash(value: str) -> str:
    _require_text(value, "objective")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise AppExplorationRuntimeRecoveryError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _is_prefixed_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("sha256:")
        and _is_plain_sha256(value.removeprefix("sha256:"))
    )


def _is_plain_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
