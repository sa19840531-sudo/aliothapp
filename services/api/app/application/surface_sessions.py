"""Surface Session 租约、Surface Registry 与有序关闭的应用端口。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Protocol

from services.api.app.domain.interaction import (
    AppBootstrapStage,
    AppReadyHandoff,
    SurfaceKind,
    SurfaceReference,
    SurfaceReferenceStatus,
    SurfaceSessionSnapshot,
    SurfaceSessionSpec,
    SurfaceSessionStatus,
)


@dataclass(kw_only=True)
class SessionCommandPermit:
    """Session Actor 内部命令许可；租约 token 禁止进入跨 Worker 协议。"""

    task_id: str
    session_id: str
    surface_kind: SurfaceKind
    lease_owner: str
    lease_token: str
    command_sequence: int
    finalization_epoch: int
    lease_expires_at: datetime
    current_capture_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in ("task_id", "session_id", "lease_owner", "lease_token"):
            _require_text(getattr(self, field_name), field_name)
        if not isinstance(self.surface_kind, SurfaceKind):
            raise ValueError("surface_kind 不受支持")
        for field_name in ("command_sequence", "finalization_epoch"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")
        if self.command_sequence <= 0:
            raise ValueError("command_sequence 必须为正整数")
        if self.lease_expires_at.utcoffset() is None:
            raise ValueError("lease_expires_at 必须带时区")
        if self.current_capture_id is not None:
            _require_text(self.current_capture_id, "current_capture_id")

    def record_current_capture(self, capture_id: str) -> None:
        """记录本命令最后一份已提交 Capture，供释放租约时原子更新游标。"""
        self.current_capture_id = _require_text(capture_id, "capture_id")


@dataclass(frozen=True, kw_only=True)
class SessionCloseReadiness:
    """会话关闭前的确定性排空快照。"""

    session_id: str
    ready: bool
    pending_actions: int
    pending_capture_commits: int
    active_evidence_requests: int
    running_restorations: int
    nonterminal_active_work_items: int
    pinned_surfaces: int
    nonterminal_app_frontier_items: int = 0
    pending_manifests: int = 0
    building_evidence_indexes: int = 0
    session_status: SurfaceSessionStatus = SurfaceSessionStatus.DRAINING
    has_command_lease: bool = False
    has_journey_lease: bool = False

    def __post_init__(self) -> None:
        _require_text(self.session_id, "session_id")
        if not isinstance(self.ready, bool):
            raise ValueError("ready 必须为布尔值")
        if not isinstance(self.session_status, SurfaceSessionStatus):
            raise ValueError("session_status 不受支持")
        if not isinstance(self.has_command_lease, bool):
            raise ValueError("has_command_lease 必须为布尔值")
        if not isinstance(self.has_journey_lease, bool):
            raise ValueError("has_journey_lease 必须为布尔值")
        counts = (
            self.pending_actions,
            self.pending_capture_commits,
            self.active_evidence_requests,
            self.running_restorations,
            self.nonterminal_active_work_items,
            self.nonterminal_app_frontier_items,
            self.pinned_surfaces,
            self.pending_manifests,
            self.building_evidence_indexes,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts
        ):
            raise ValueError("会话关闭计数必须为非负整数")
        expected_ready = (
            self.session_status is SurfaceSessionStatus.DRAINING
            and not self.has_command_lease
            and not self.has_journey_lease
            and all(value == 0 for value in counts)
        )
        if self.ready != expected_ready:
            raise ValueError("ready 与会话状态、租约或关闭计数不一致")


class SessionStateConflictError(RuntimeError):
    """会话状态 CAS 未命中；调用方须重读身份与终态后才能有限重试。"""


class SessionDrainPendingError(RuntimeError):
    """排空尚未完成；仅包含安全快照，不代表 Driver 已尝试关闭。"""

    def __init__(self, *, task_id: str, readiness: SessionCloseReadiness) -> None:
        self.task_id = task_id
        self.readiness = readiness
        self.diagnostics = {"task_id": task_id, **asdict(readiness)}
        super().__init__(
            "Surface Session 尚未排空，禁止关闭 Driver: "
            + json.dumps(self.diagnostics, ensure_ascii=False, sort_keys=True)
        )


@dataclass(frozen=True, kw_only=True)
class AppExplorationPendingFacts:
    """App 探索进行期从 PostgreSQL 读取的待处理事实。"""

    task_id: str
    session_id: str
    pending_action_count: int
    pending_evidence_count: int

    def __post_init__(self) -> None:
        _require_text(self.task_id, "task_id")
        _require_text(self.session_id, "session_id")
        for field_name in ("pending_action_count", "pending_evidence_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} 必须为非负整数")


class SurfaceSessionRepository(Protocol):
    """PostgreSQL Surface Session 事实源所需能力。"""

    async def register_active(
        self,
        *,
        spec: SurfaceSessionSpec,
        session_owner: str,
        primary_surface_ref: str,
    ) -> SurfaceSessionSnapshot: ...

    async def register_starting(
        self,
        *,
        spec: SurfaceSessionSpec,
        session_owner: str,
        primary_surface_ref: str,
    ) -> SurfaceSessionSnapshot: ...

    async def activate_from_bootstrap(
        self,
        *,
        handoff: AppReadyHandoff,
        expected_bootstrap_stage: AppBootstrapStage,
        expected_bootstrap_row_version: int,
        expected_session_row_version: int,
        workflow_lease_owner: str,
        workflow_lease_token: str,
    ) -> SurfaceSessionSnapshot: ...

    async def get(self, *, session_id: str) -> SurfaceSessionSnapshot | None: ...

    async def acquire_command(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_kind: SurfaceKind,
        lease_owner: str,
        lease_token: str,
        lease_seconds: int,
        allow_draining: bool,
    ) -> SessionCommandPermit: ...

    async def renew_command(
        self,
        *,
        permit: SessionCommandPermit,
        lease_seconds: int,
    ) -> SessionCommandPermit: ...

    async def release_command(
        self,
        *,
        permit: SessionCommandPermit,
        current_capture_id: str | None,
    ) -> bool: ...

    async def begin_draining(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot: ...

    async def close_readiness(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> SessionCloseReadiness: ...

    async def exploration_pending_facts(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> AppExplorationPendingFacts: ...

    async def seal_for_close(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot: ...

    async def seal_exploration_for_close(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot: ...

    async def mark_closed(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
    ) -> SurfaceSessionSnapshot: ...

    async def mark_lost(
        self,
        *,
        task_id: str,
        session_id: str,
        expected_row_version: int,
        reason: str,
    ) -> SurfaceSessionSnapshot: ...


class SurfaceRegistryRepository(Protocol):
    """逻辑 Surface 注册、Pin 与释放的独立仓储端口。"""

    async def register_surface(
        self,
        *,
        task_id: str,
        session_id: str,
        surface_ref_id: str,
        surface_key: str,
        surface_kind: SurfaceKind,
    ) -> SurfaceReference: ...

    async def transition_surface(
        self,
        *,
        task_id: str,
        surface_ref_id: str,
        expected_status: SurfaceReferenceStatus,
        target_status: SurfaceReferenceStatus,
        row_version: int,
        pin_reason: str | None = None,
        current_capture_id: str | None = None,
    ) -> SurfaceReference: ...


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value
