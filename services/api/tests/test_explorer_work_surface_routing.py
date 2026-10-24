"""Probe/Exploration 工作按界面类型派发给对应平台执行器。

补采 Probe 由 evidence_request_routing 按目标 Surface 路由产出，各平台执行器共用
同一张 qc_v2_work_item；执行器不声明自己能跑什么，就会领到跑不了的活（Web 执行器
领到 android 的恢复路线）。这里守住三件事：命令声明合法、声明会真的进 SQL 过滤、
不声明时行为与过滤前一致。
"""

from __future__ import annotations

import pytest

from services.api.app.application.exploration_work_queue import (
    ClaimExplorerWorkCommand,
    ExplorerWorkType,
)
from services.api.app.application.supplemental_evidence_policy import (
    SupplementalEvidencePolicy,
)
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.explorer_work_repository import (
    PostgresExplorerWorkRepository,
)


class _CapturingConnection:
    """记下 claim 真正发给数据库的参数；领取结果一律为空。"""

    def __init__(self) -> None:
        self.sql: str | None = None
        self.args: tuple[object, ...] = ()

    async def fetchrow(self, sql: str, *args: object):
        self.sql = sql
        self.args = args
        return None

    def transaction(self) -> _AsyncNoop:
        return _AsyncNoop()


class _AsyncNoop:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


class _PinnedPool:
    def __init__(self, connection: _CapturingConnection) -> None:
        self._connection = connection

    def acquire(self) -> _PinnedAcquire:
        return _PinnedAcquire(self._connection)


class _PinnedAcquire:
    def __init__(self, connection: _CapturingConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _CapturingConnection:
        return self._connection

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _repository(
    connection: _CapturingConnection,
    *,
    supplemental: bool = False,
) -> PostgresExplorerWorkRepository:
    return PostgresExplorerWorkRepository(
        pool=_PinnedPool(connection),
        supplemental_evidence_policy=(
            SupplementalEvidencePolicy(enabled=True, runtime_ready=True)
            if supplemental
            else None
        ),
    )


async def _claim(
    connection: _CapturingConnection,
    command: ClaimExplorerWorkCommand,
    *,
    supplemental: bool = False,
) -> None:
    await _repository(connection, supplemental=supplemental).claim(
        command=command,
        lease_token="lease-token-1",
    )


async def test_claim_without_surface_declaration_does_not_filter() -> None:
    """既有单端调用不声明界面类型时，过滤参数为空，行为与加过滤前一致。"""

    connection = _CapturingConnection()

    await _claim(
        connection,
        ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="worker-1",
            task_id=None,
        ),
    )

    assert connection.args[5] is None
    assert "work_payload->>'surface_kind' = ANY($6::varchar[])" in (connection.sql or "")


async def test_claim_passes_declared_surface_kinds_into_filter() -> None:
    connection = _CapturingConnection()

    await _claim(
        connection,
        ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="web-worker-1",
            task_id=None,
            surface_kinds=(SurfaceKind.WEB,),
        ),
    )

    assert connection.args[5] == ["web"]


async def test_h5_probe_claim_allows_snapshot_and_active_session_during_draining() -> None:
    connection = _CapturingConnection()

    await _claim(
        connection,
        ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.PROBE,
            lease_owner="h5-worker-1",
            task_id=None,
            surface_kinds=(SurfaceKind.WEB,),
        ),
        supplemental=True,
    )

    sql = connection.sql or ""
    assert "task.status = 'review'" in sql
    assert "lower(task.target_platform) = 'h5'" in sql
    assert "request.session_dependency = 'active_session'" in sql
    assert "request.session_dependency = 'snapshot'" in sql


async def test_app_executor_declares_both_mobile_surfaces() -> None:
    connection = _CapturingConnection()

    await _claim(
        connection,
        ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="app-worker-1",
            task_id=None,
            surface_kinds=(SurfaceKind.ANDROID, SurfaceKind.IOS),
        ),
    )

    assert connection.args[5] == ["android", "ios"]


def test_claim_command_rejects_non_surface_kind_declaration() -> None:
    with pytest.raises(TypeError, match="surface_kinds"):
        ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="worker-1",
            surface_kinds=("web",),  # type: ignore[arg-type]
        )


def test_claim_command_rejects_duplicate_surface_kinds() -> None:
    with pytest.raises(ValueError, match="重复"):
        ClaimExplorerWorkCommand(
            work_type=ExplorerWorkType.EXPLORATION,
            lease_owner="worker-1",
            surface_kinds=(SurfaceKind.WEB, SurfaceKind.WEB),
        )
