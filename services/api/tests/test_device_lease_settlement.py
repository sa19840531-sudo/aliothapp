"""设备租约暂态结算合同。"""

from __future__ import annotations

import pytest

from services.api.app.application import device_lease_settlement as settlement


@pytest.mark.parametrize(
    "reason",
    [
        "OFFICIAL_API_TIMEOUT",
        "OFFICIAL_HTTP_502",
        "CAPTURE_FAILED",
        "APP_ACTION_AFTER_CAPTURE_MISSING",
        "app_session_lost:OFFICIAL_HTTP_502",
    ],
)
def test_is_transient_device_loss_reason(reason: str) -> None:
    assert settlement.is_transient_device_loss_reason(reason) is True


@pytest.mark.parametrize(
    "reason",
    [
        "APP_WORKFLOW_CANCELED",
        "WORKFLOW_CANCELED",
        "workflow_canceled",
        "WORKFLOW_CANCELED_CLOSURE_FAILED:RuntimeError",
    ],
)
def test_workflow_cancel_preserves_cloud_phone_lease(reason: str) -> None:
    assert settlement.preserves_cloud_phone_lease(reason) is True


@pytest.mark.asyncio
async def test_settle_workflow_cancel_does_not_release_or_quarantine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    async def release(*, session_id: str) -> bool:
        events.append(f"release:{session_id}")
        return True

    async def quarantine(*, session_id: str, reason: str) -> bool:
        events.append(f"quarantine:{reason}")
        return True

    monkeypatch.setattr(settlement, "release_device_lease_for_session", release)
    monkeypatch.setattr(settlement, "quarantine_device_lease_for_session", quarantine)

    await settlement.settle_device_lease_after_session_loss(
        session_id="s-1",
        reason="APP_WORKFLOW_CANCELED",
    )
    assert events == []


def test_hard_device_loss_still_quarantines() -> None:
    assert settlement.is_transient_device_loss_reason("DEVICE_OFFLINE") is False
    assert settlement.is_transient_device_loss_reason("lease_fencing_lost") is False


@pytest.mark.asyncio
async def test_settle_transient_prefers_release(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def release(*, session_id: str) -> bool:
        events.append(f"release:{session_id}")
        return True

    async def quarantine(*, session_id: str, reason: str) -> bool:
        events.append(f"quarantine:{reason}")
        return True

    monkeypatch.setattr(settlement, "release_device_lease_for_session", release)
    monkeypatch.setattr(settlement, "quarantine_device_lease_for_session", quarantine)

    await settlement.settle_device_lease_after_session_loss(
        session_id="s-1",
        reason="OFFICIAL_HTTP_502",
    )
    assert events == ["release:s-1"]


@pytest.mark.asyncio
async def test_settle_hard_loss_quarantines(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []

    async def release(*, session_id: str) -> bool:
        events.append("release")
        return True

    async def quarantine(*, session_id: str, reason: str) -> bool:
        events.append(reason)
        return True

    monkeypatch.setattr(settlement, "release_device_lease_for_session", release)
    monkeypatch.setattr(settlement, "quarantine_device_lease_for_session", quarantine)

    await settlement.settle_device_lease_after_session_loss(
        session_id="s-1",
        reason="DEVICE_OFFLINE",
    )
    assert events == ["app_session_lost:DEVICE_OFFLINE"]
