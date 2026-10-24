"""App 质检结束后 ADB 进程清理合同测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from services.api.app.application.device_pool import (
    DeviceLease,
    maybe_cleanup_adb_after_app_qc,
    release_device_lease,
)
from services.api.app.infrastructure.device import adb_process_cleanup as cleanup_mod


def _lease(*, device_ref: str = "cloud_phone:qa-b", env_id: str = "51.195.137.27:20660") -> DeviceLease:
    return DeviceLease(
        lease_id="lease-1",
        device_ref=device_ref,
        task_id="task-1",
        session_id="session-1",
        claimed_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(seconds=60),
        backend="cloud_phone",
        profile_id="qa-b",
        morelogin_env_id=env_id,
    )


def test_cleanup_enabled_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_ADB_CLEANUP_AFTER_QC", raising=False)
    assert cleanup_mod.adb_cleanup_after_qc_enabled() is True
    monkeypatch.setenv("ALIOTH_ADB_CLEANUP_AFTER_QC", "0")
    assert cleanup_mod.adb_cleanup_after_qc_enabled() is False


def test_cleanup_disconnect_only_when_parallel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    adb = tmp_path / "adb.exe"
    adb.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("ALIOTH_HOST_ADB", str(adb))
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(cleanup_mod.subprocess, "run", _run)
    logs = cleanup_mod.cleanup_adb_processes_after_app_qc(
        endpoint="1.2.3.4:20660",
        kill_processes=False,
        reason="parallel",
    )
    assert any("disconnect" in " ".join(c) for c in calls)
    assert all("kill-server" not in c for c in calls)
    assert any("deferred" in line for line in logs)


def test_cleanup_kills_when_idle(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    adb = tmp_path / "adb"
    adb.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("ALIOTH_HOST_ADB", str(adb))
    monkeypatch.setattr(cleanup_mod.sys, "platform", "linux")
    calls: list[list[str]] = []

    def _run(cmd, **_kwargs):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cleanup_mod.subprocess, "run", _run)
    logs = cleanup_mod.cleanup_adb_processes_after_app_qc(
        endpoint="1.2.3.4:20660",
        kill_processes=True,
        reason="idle",
    )
    flat = [" ".join(c) for c in calls]
    assert any("disconnect" in item for item in flat)
    assert any("kill-server" in item for item in flat)
    assert any("pkill" in item or "killall" in item for item in flat)
    assert any("kill_processes=True" in line for line in logs)


@pytest.mark.asyncio
async def test_release_device_lease_triggers_adb_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.application import device_pool as pool_mod

    lease = _lease()
    pool = SimpleNamespace(
        release=lambda **_kwargs: True,
        status=lambda: {"leased": []},
        _repository=None,
    )
    async def _fake_get_pool():
        return pool

    async def _fake_power_off(**_kwargs):
        return None

    monkeypatch.setattr(pool_mod, "get_runtime_device_pool", _fake_get_pool)
    monkeypatch.setattr(pool_mod, "maybe_power_off_cloud_phone_for_lease", _fake_power_off)
    called: list[dict] = []

    def _fake_cleanup(*, endpoint=None, kill_processes=True, reason=""):
        called.append(
            {
                "endpoint": endpoint,
                "kill_processes": kill_processes,
                "reason": reason,
            }
        )
        return ["ok"]

    monkeypatch.setenv("ALIOTH_ADB_CLEANUP_AFTER_QC", "1")
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.adb_process_cleanup.cleanup_adb_processes_after_app_qc",
        _fake_cleanup,
    )
    assert await release_device_lease(lease=lease) is True
    assert called
    assert called[0]["endpoint"] == "51.195.137.27:20660"
    assert called[0]["kill_processes"] is True


@pytest.mark.asyncio
async def test_cleanup_defers_kill_when_other_lease_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = _lease()
    called: list[dict] = []

    def _fake_cleanup(*, endpoint=None, kill_processes=True, reason=""):
        called.append({"kill_processes": kill_processes, "endpoint": endpoint})
        return ["deferred"]

    monkeypatch.setenv("ALIOTH_ADB_CLEANUP_AFTER_QC", "1")
    monkeypatch.setattr(
        "services.api.app.infrastructure.device.adb_process_cleanup.cleanup_adb_processes_after_app_qc",
        _fake_cleanup,
    )

    async def _has_other(**_kwargs) -> bool:
        return True

    monkeypatch.setattr(
        "services.api.app.application.device_pool._has_other_active_device_leases",
        _has_other,
    )
    await maybe_cleanup_adb_after_app_qc(lease=lease, reason="unit")
    assert called == [{"kill_processes": False, "endpoint": "51.195.137.27:20660"}]
