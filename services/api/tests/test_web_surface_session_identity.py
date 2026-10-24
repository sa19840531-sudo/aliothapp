"""Web/H5 Surface Session 身份契约：工作流侧与命令闸门侧必须给出同一个主 Surface。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.h5_exploration_workflow import H5_SURFACE_SESSION_OWNER
from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionSpec
from services.api.app.domain.interaction.surface_driver import web_primary_surface_ref
from services.api.app.infrastructure.browser.surface_driver import (
    PlaywrightSurfaceDriverAdapter,
)
from services.api.app.infrastructure.db.session_command_gate import (
    PostgresSessionCommandGate,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
    SurfaceSessionPersistenceError,
)

_TASK_ID = "11111111-1111-1111-1111-111111111111"
_SESSION_ID = "h5:80ac0324-e365-4fb5-94d3-be392fb8ca82"


class RecordingSessionRepository:
    def __init__(self) -> None:
        self.registrations: list[dict[str, object]] = []

    async def get(self, *, session_id: str):
        """闸门对 Web 先查已有会话；此处模拟首次进入（尚未注册）。"""
        del session_id
        return None

    async def register_active(self, **kwargs):
        self.registrations.append(kwargs)

    async def acquire_command(self, **kwargs) -> SessionCommandPermit:
        return SessionCommandPermit(
            task_id=kwargs["task_id"],
            session_id=kwargs["session_id"],
            surface_kind=kwargs["surface_kind"],
            lease_owner=kwargs["lease_owner"],
            lease_token=kwargs["lease_token"],
            command_sequence=1,
            finalization_epoch=0,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=120),
        )

    async def renew_command(self, *, permit, lease_seconds):
        del lease_seconds
        return permit

    async def release_command(self, *, permit, current_capture_id):
        del permit, current_capture_id
        return True


def _web_spec() -> SurfaceSessionSpec:
    return SurfaceSessionSpec(
        task_id=_TASK_ID,
        session_id=_SESSION_ID,
        surface_kind=SurfaceKind.WEB,
    )


async def test_web_command_gate_registers_same_primary_surface_as_driver() -> None:
    """两侧算法一旦分叉，后注册方会绕过主键幂等撞 UNIQUE(session_id, surface_key)。"""
    adapter = PlaywrightSurfaceDriverAdapter(
        browser=None,  # type: ignore[arg-type]
        target_resolver=None,  # type: ignore[arg-type]
        evidence_buffer=None,  # type: ignore[arg-type]
    )
    session = await adapter.start_session(_web_spec())

    repository = RecordingSessionRepository()
    gate = PostgresSessionCommandGate(
        repository=repository,  # type: ignore[arg-type]
        lease_owner="web-capture-runtime:test",
        session_owner=H5_SURFACE_SESSION_OWNER,
    )
    async with gate.hold(
        task_id=_TASK_ID,
        session_id=_SESSION_ID,
        surface_kind=SurfaceKind.WEB,
    ):
        pass

    registration = repository.registrations[0]
    assert registration["primary_surface_ref"] == session.primary_surface_ref
    assert registration["primary_surface_ref"] == web_primary_surface_ref(_SESSION_ID)
    assert registration["session_owner"] == H5_SURFACE_SESSION_OWNER


async def test_register_active_rejects_drifted_web_primary_surface_ref() -> None:
    """身份漂移要在进库前拦下，而不是让裸 duplicate key 冒到任务失败。"""
    repository = PostgresSurfaceSessionRepository()

    with pytest.raises(SurfaceSessionPersistenceError, match="主 Surface 身份不稳定"):
        await repository.register_active(
            spec=_web_spec(),
            session_owner=H5_SURFACE_SESSION_OWNER,
            primary_surface_ref="surface-reference:0f1e2d3c",
        )
