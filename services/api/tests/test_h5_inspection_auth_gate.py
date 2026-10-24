"""H5 规则发布必须使用本次 Surface Session 的登录事实。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from services.api.app.application import app_inspection_publisher as module
from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
)


class _Acquire:
    def __init__(self, connection) -> None:
        self._connection = connection

    async def __aenter__(self):
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class _Pool:
    def __init__(self, row) -> None:
        self.connection = _Connection(row)

    def acquire(self):
        return _Acquire(self.connection)


class _Connection:
    def __init__(self, row) -> None:
        self.row = row
        self.query = ""

    async def fetchrow(self, query, capture_id, task_id):
        self.query = query
        assert capture_id == "capture-1"
        assert task_id == "task-1"
        return self.row


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("authenticated", "guest"),
        ("already_authenticated", "guest"),
        ("guest_only", "authenticated"),
        ("disabled", "authenticated"),
        ("failed", "authenticated"),
        ("checking", "authenticated"),
    ],
)
async def test_h5_auth_phase_result_drives_rule_gate(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected: str | None,
) -> None:
    pool = _Pool(
        {
            "session_auth": status,
            "no_test_account": False,
            "has_test_account": True,
        }
    )

    async def _pool():
        return pool

    monkeypatch.setattr(module, "get_postgres_pool", _pool)
    publisher = object.__new__(AppCaptureInspectionPublisher)

    resolved = await publisher._resolve_auth_gate(
        snapshot=SimpleNamespace(target_platform="h5"),
        task_id="task-1",
        capture_id="capture-1",
    )

    assert resolved == expected
    assert "phase_run.phase_key = 'h5_auth_bootstrap'" in pool.connection.query
    assert "phase_run.session_id = page_capture.surface_session_id" in pool.connection.query


@pytest.mark.asyncio
async def test_h5_task_without_test_account_closes_authenticated_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _Pool(
        {
            "session_auth": None,
            "no_test_account": True,
            "has_test_account": False,
        }
    )

    async def _pool():
        return pool

    monkeypatch.setattr(module, "get_postgres_pool", _pool)
    publisher = object.__new__(AppCaptureInspectionPublisher)

    resolved = await publisher._resolve_auth_gate(
        snapshot=SimpleNamespace(target_platform="h5"),
        task_id="task-1",
        capture_id="capture-1",
    )

    assert resolved == "authenticated"
