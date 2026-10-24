"""排期第 58 行：2 任务 × 2 云手机仓库闭环回归。

覆盖设计 §8.2：互斥、实连绑定、释放、故障隔离、池满第三人。
本文件不呼叫真实 MoreLogin／ADB；生产双机验收仍须另跑实连冒烟。
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from services.api.app.application.app_workflow import AppWorkflowFailure
from services.api.app.application.device_pool import reset_device_pool_for_tests
from services.api.app.application.device_wait import claim_device_lease_with_wait
from services.api.app.application.xingyu_runtime.device_bridge import LiveCloudPhoneDeviceBridge
from services.api.app.application.xingyu_runtime.service import XingyuOfficialService
from services.api.app.domain.interaction import AppBootstrapStage
from services.api.tests.test_app_qc_workflow import _command, _harness


def _bind_two_cloud_phones(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "env-a,env-b")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-must-not-win")
    monkeypatch.delenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", raising=False)
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "0")
    return reset_device_pool_for_tests(slots=["cloud_phone:qa-a", "cloud_phone:qa-b"])


def _force_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.api.app.application import app_workflow as app_workflow_module

    monkeypatch.setattr(app_workflow_module, "is_postgres_device_lease_backend", lambda: True)


def _line_command(*, index: int):
    return replace(
        _command(),
        task_id=f"11111111-1111-1111-1111-11111111111{index}",
        agent_run_id=f"22222222-2222-2222-2222-22222222222{index}",
        session_id=f"app-session-{index}",
        lease_owner=f"app-worker-{index}",
        lease_token=str(uuid4()),
    )


def _record_start_specs(runtime: Any) -> list[Any]:
    recorded: list[Any] = []
    original = runtime.start_session

    async def _start(spec: Any) -> Any:
        recorded.append(spec)
        return await original(spec)

    runtime.start_session = _start  # type: ignore[method-assign]
    return recorded


async def test_dual_claim_assigns_distinct_device_ref_and_env_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bind_two_cloud_phones(monkeypatch)
    first, second = await asyncio.gather(
        claim_device_lease_with_wait(
            task_id="task-1",
            session_id="session-1",
            preferred_device_ref=None,
            lease_seconds=120,
        ),
        claim_device_lease_with_wait(
            task_id="task-2",
            session_id="session-2",
            preferred_device_ref=None,
            lease_seconds=120,
        ),
    )
    assert {first.device_ref, second.device_ref} == {
        "cloud_phone:qa-a",
        "cloud_phone:qa-b",
    }
    assert {first.morelogin_env_id, second.morelogin_env_id} == {"env-a", "env-b"}
    assert first.morelogin_env_id != second.morelogin_env_id


async def test_dual_workflow_binds_distinct_env_and_installs_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _bind_two_cloud_phones(monkeypatch)
    _force_claim(monkeypatch)
    left_wf, left_rt, _, left_sessions, _, left_pub, _ = _harness()
    right_wf, right_rt, _, right_sessions, _, right_pub, _ = _harness()
    left_specs = _record_start_specs(left_rt)
    right_specs = _record_start_specs(right_rt)

    await asyncio.gather(left_wf.run(_line_command(index=1)), right_wf.run(_line_command(index=2)))

    left_env = left_sessions.last_spec.options["morelogin_env_id"]
    right_env = right_sessions.last_spec.options["morelogin_env_id"]
    assert {left_env, right_env} == {"env-a", "env-b"}
    assert left_sessions.last_spec.device_ref != right_sessions.last_spec.device_ref
    assert left_specs[0].options["morelogin_env_id"] == left_env
    assert right_specs[0].options["morelogin_env_id"] == right_env
    assert "install" in left_rt.events
    assert "install" in right_rt.events
    assert left_pub.handoffs and right_pub.handoffs
    leased = {item["device_ref"] for item in pool.status()["leased"]}
    assert leased == {"cloud_phone:qa-a", "cloud_phone:qa-b"}


async def test_dual_official_start_keeps_instance_env_and_timeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-must-not-win")
    created: list[str] = []

    class _FakeController:
        def __init__(self, **kwargs: Any) -> None:
            env_id = str(kwargs.get("morelogin_env_id") or "")
            created.append(env_id)
            self.env_id = env_id
            assert kwargs.get("apply_morelogin_to_process_env") is False

        def ensure_ready(self) -> tuple[str, list[str]]:
            return f"serial:{self.env_id}", [f"timeline env={self.env_id}"]

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.cloud_phone.CloudPhoneController",
        _FakeController,
    )
    monkeypatch.setattr(
        "services.api.app.application.xingyu_runtime.device_bridge.open_adb_device_ops",
        lambda *args, **kwargs: SimpleNamespace(width=1080, height=1920),
    )
    bridge = LiveCloudPhoneDeviceBridge()
    serial_a, device_a = bridge.start(backend="cloud_phone", morelogin_env_id="env-a")
    serial_b, device_b = bridge.start(backend="cloud_phone", morelogin_env_id="env-b")
    assert serial_a != serial_b
    assert device_a.controller.env_id == "env-a"
    assert device_b.controller.env_id == "env-b"
    assert created == ["env-a", "env-b"]

    class _RecordingBridge:
        def __init__(self) -> None:
            self.starts: list[str | None] = []

        def start(
            self,
            *,
            backend: str,
            morelogin_env_id: str | None = None,
            clear_leftover: bool = True,
        ) -> tuple[str, dict[str, str]]:
            del backend, clear_leftover
            self.starts.append(morelogin_env_id)
            return f"serial:{morelogin_env_id}", {"env": str(morelogin_env_id)}

    recording = _RecordingBridge()
    service = XingyuOfficialService(bridge=recording)  # type: ignore[arg-type]
    first = service.start_session(
        {
            "task_id": "task-a",
            "session_id": "session-a",
            "options": {"backend": "cloud_phone", "morelogin_env_id": "env-a"},
        }
    )
    second = service.start_session(
        {
            "task_id": "task-b",
            "session_id": "session-b",
            "options": {"backend": "cloud_phone", "morelogin_env_id": "env-b"},
        }
    )
    assert recording.starts == ["env-a", "env-b"]
    assert first["session_handle"] != second["session_handle"]


async def test_release_returns_both_slots_to_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _bind_two_cloud_phones(monkeypatch)
    _force_claim(monkeypatch)
    left_wf, _, _, _, _, _, _ = _harness()
    right_wf, _, _, _, _, _, _ = _harness()
    await asyncio.gather(left_wf.run(_line_command(index=1)), right_wf.run(_line_command(index=2)))
    assert pool.status()["available"] == 0
    for session_id in ("app-session-1", "app-session-2"):
        lease = pool.get(session_id=session_id)
        assert lease is not None
        assert pool.release(lease=lease) is True
    assert pool.status()["available"] == 2
    again_a, again_b = await asyncio.gather(
        claim_device_lease_with_wait(
            task_id="task-3",
            session_id="session-3",
            preferred_device_ref=None,
            lease_seconds=120,
        ),
        claim_device_lease_with_wait(
            task_id="task-4",
            session_id="session-4",
            preferred_device_ref=None,
            lease_seconds=120,
        ),
    )
    assert {again_a.morelogin_env_id, again_b.morelogin_env_id} == {"env-a", "env-b"}


async def test_quarantine_one_slot_does_not_kill_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _bind_two_cloud_phones(monkeypatch)
    _force_claim(monkeypatch)
    left_wf, left_rt, _, left_sessions, _, _, _ = _harness()
    right_wf, right_rt, _, right_sessions, _, _, _ = _harness()
    await asyncio.gather(left_wf.run(_line_command(index=1)), right_wf.run(_line_command(index=2)))
    left_lease = pool.get(session_id="app-session-1")
    right_lease = pool.get(session_id="app-session-2")
    assert left_lease is not None and right_lease is not None
    assert pool.quarantine(lease=left_lease, reason="provider_failed") is True
    assert pool.release(lease=right_lease) is True

    survivor_wf, survivor_rt, _, survivor_sessions, _, _, _ = _harness()
    await survivor_wf.run(_line_command(index=3))
    assert survivor_sessions.last_spec.device_ref == right_lease.device_ref
    assert survivor_sessions.last_spec.options["morelogin_env_id"] == right_lease.morelogin_env_id
    assert "install" in survivor_rt.events
    assert left_rt.events.count("install") == 1
    assert right_rt.events.count("install") == 1

    blocked_wf, blocked_rt, _, blocked_sessions, _, _, _ = _harness()
    with pytest.raises(AppWorkflowFailure) as failure:
        await blocked_wf.run(_line_command(index=4))
    assert failure.value.error_code == "DEVICE_POOL_EXHAUSTED"
    assert blocked_rt.events == []
    assert blocked_sessions.events == []
    quarantined = pool.get(session_id="app-session-1")
    assert quarantined is not None
    assert quarantined.state == "quarantined"


async def test_third_task_waits_timeout_without_sharing_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = _bind_two_cloud_phones(monkeypatch)
    _force_claim(monkeypatch)
    left_wf, left_rt, _, left_sessions, _, _, _ = _harness()
    right_wf, right_rt, _, right_sessions, _, _, _ = _harness()
    await asyncio.gather(left_wf.run(_line_command(index=1)), right_wf.run(_line_command(index=2)))
    occupied_envs = {
        left_sessions.last_spec.options["morelogin_env_id"],
        right_sessions.last_spec.options["morelogin_env_id"],
    }
    assert occupied_envs == {"env-a", "env-b"}
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "0.05")
    third_wf, third_rt, _, third_sessions, _, _, _ = _harness()
    with pytest.raises(AppWorkflowFailure) as failure:
        await third_wf.run(_line_command(index=3))
    assert failure.value.error_code == "DEVICE_WAIT_TIMEOUT"
    assert failure.value.retryable is False
    assert failure.value.stage is AppBootstrapStage.ACQUIRING_SESSION
    assert third_rt.events == []
    assert third_sessions.events == []
    still = {item["session_id"] for item in pool.status()["leased"]}
    assert still == {"app-session-1", "app-session-2"}
    assert "install" in left_rt.events
    assert "install" in right_rt.events
