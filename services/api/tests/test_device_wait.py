"""设备池满有界等待（排期第 57 行）单元测试。"""

from __future__ import annotations

import pytest

from services.api.app.application.device_pool import DevicePoolError, reset_device_pool_for_tests
from services.api.app.application.device_wait import (
    claim_device_lease_with_wait,
    describe_worker_device_concurrency,
    device_wait_backoff_seconds,
    resolve_device_wait_max_seconds,
)


def test_wait_max_seconds_defaults_to_600(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", raising=False)
    assert resolve_device_wait_max_seconds() == 600.0
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "0")
    assert resolve_device_wait_max_seconds() == 0.0


def test_backoff_grows_and_caps() -> None:
    assert device_wait_backoff_seconds(0, error_code="DEVICE_BUSY") == 1.0
    assert device_wait_backoff_seconds(1, error_code="DEVICE_POOL_EXHAUSTED") == 2.0
    assert device_wait_backoff_seconds(10, error_code="DEVICE_BUSY") == 30.0
    assert device_wait_backoff_seconds(0, error_code="DEVICE_LEASE_CLAIM_RACE") == 0.05
    assert device_wait_backoff_seconds(8, error_code="DEVICE_LEASE_CLAIM_RACE") == 0.2


def test_concurrency_warning_only_when_exceeding_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", raising=False)
    assert describe_worker_device_concurrency(2) is None
    warning = describe_worker_device_concurrency(4)
    assert warning is not None
    assert "concurrency=4" in warning
    assert "槽位=2" in warning


async def test_wait_disabled_reraises_original_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "0")
    pool = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    pool.claim(task_id="other", session_id="other-session", lease_seconds=120)
    with pytest.raises(DevicePoolError) as failure:
        await claim_device_lease_with_wait(
            task_id="task-1",
            session_id="session-1",
            preferred_device_ref=None,
            lease_seconds=120,
        )
    assert failure.value.error_code == "DEVICE_POOL_EXHAUSTED"
    assert failure.value.retryable is True


async def test_wait_timeout_advances_clock_in_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "0.05")
    clock = {"now": 0.0}
    waits: list[str] = []

    async def _sleep(delay: float) -> None:
        clock["now"] += delay

    def _monotonic() -> float:
        return clock["now"]

    pool = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    pool.claim(task_id="other", session_id="other-session", lease_seconds=120)
    with pytest.raises(DevicePoolError) as failure:
        await claim_device_lease_with_wait(
            task_id="task-1",
            session_id="session-1",
            preferred_device_ref=None,
            lease_seconds=120,
            sleep=_sleep,
            monotonic=_monotonic,
            on_waiting=lambda attempt: waits.append(attempt.error_code),
        )
    assert failure.value.error_code == "DEVICE_WAIT_TIMEOUT"
    assert failure.value.retryable is False
    assert "DEVICE_POOL_EXHAUSTED" in waits


async def test_wait_then_claim_succeeds_after_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "2")
    pool = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    occupied = pool.claim(task_id="other", session_id="other-session", lease_seconds=120)
    released = {"done": False}

    async def _sleep(delay: float) -> None:
        del delay
        if not released["done"]:
            pool.release(lease=occupied)
            released["done"] = True

    lease = await claim_device_lease_with_wait(
        task_id="task-1",
        session_id="session-1",
        preferred_device_ref=None,
        lease_seconds=120,
        sleep=_sleep,
    )
    assert lease.session_id == "session-1"
    assert lease.device_ref == "cloud_phone:a"


async def test_wait_canceled_does_not_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_WAIT_MAX_SECONDS", "10")
    pool = reset_device_pool_for_tests(slots=["cloud_phone:a"])
    pool.claim(task_id="other", session_id="other-session", lease_seconds=120)

    async def _canceled() -> bool:
        return True

    with pytest.raises(DevicePoolError) as failure:
        await claim_device_lease_with_wait(
            task_id="task-1",
            session_id="session-1",
            preferred_device_ref=None,
            lease_seconds=120,
            is_canceled=_canceled,
        )
    assert failure.value.error_code == "WORKFLOW_CANCELED"
    assert failure.value.retryable is False
