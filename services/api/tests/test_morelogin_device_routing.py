"""MoreLogin 按租约选机：库存映射、单机回退与多机失败关闭。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.application.device_pool import (
    DevicePoolError,
    allow_global_morelogin_fallback,
    parse_device_pool_slot_seeds,
    resolve_morelogin_env_id_for_lease,
)
from services.api.app.application.xingyu_runtime.device_bridge import LiveCloudPhoneDeviceBridge
from services.api.app.application.xingyu_runtime.service import _resolve_morelogin_env_id


def test_slot_seeds_map_env_ids_in_cloud_phone_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_SLOTS",
        "cloud_phone:qa-a,ldplayer:0,cloud_phone:qa-b",
    )
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "id-a,id-b")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-should-not-win")
    seeds = parse_device_pool_slot_seeds()
    by_ref = {seed.device_ref: seed for seed in seeds}
    assert by_ref["cloud_phone:qa-a"].morelogin_env_id == "id-a"
    assert by_ref["cloud_phone:qa-b"].morelogin_env_id == "id-b"
    assert by_ref["ldplayer:0"].morelogin_env_id == ""
    assert by_ref["cloud_phone:qa-a"].capabilities()["env_id"] == "id-a"


def test_slot_seeds_reject_mismatched_env_id_count(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", "only-one")
    with pytest.raises(DevicePoolError) as failure:
        parse_device_pool_slot_seeds()
    assert failure.value.error_code == "DEVICE_POOL_ENV_ID_MAPPING_INVALID"
    assert failure.value.retryable is False


def test_single_slot_falls_back_to_global_cloudphone_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:default")
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", raising=False)
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "1716966721435348")
    seeds = parse_device_pool_slot_seeds()
    assert seeds[0].morelogin_env_id == "1716966721435348"
    assert allow_global_morelogin_fallback() is True


def test_multi_slot_does_not_allow_global_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS", raising=False)
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-id")
    seeds = parse_device_pool_slot_seeds()
    assert all(not seed.morelogin_env_id for seed in seeds)
    assert allow_global_morelogin_fallback() is False


def test_lease_env_id_wins_over_global(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.device_pool import DeviceLease

    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-id")
    now = datetime.now(UTC)
    lease = DeviceLease(
        lease_id="11111111-1111-1111-1111-111111111111",
        device_ref="cloud_phone:qa-a",
        task_id="22222222-2222-2222-2222-222222222222",
        session_id="session-1",
        claimed_at=now,
        expires_at=now + timedelta(minutes=5),
        backend="cloud_phone",
        profile_id="qa-a",
        morelogin_env_id="lease-env",
    )
    assert resolve_morelogin_env_id_for_lease(lease) == "lease-env"


def test_xingyu_payload_reads_morelogin_env_id_from_options() -> None:
    assert (
        _resolve_morelogin_env_id(
            {"options": {"backend": "cloud_phone", "morelogin_env_id": "env-9"}}
        )
        == "env-9"
    )
    assert _resolve_morelogin_env_id({"options": {"backend": "cloud_phone"}}) is None


def test_live_bridge_rejects_missing_env_id_when_multi_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a,cloud_phone:qa-b")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-id")

    class _BoomController:
        def __init__(self, **kwargs: Any) -> None:
            raise AssertionError(f"不得构造控制器：{kwargs}")

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.cloud_phone.CloudPhoneController",
        _BoomController,
    )
    with pytest.raises(RuntimeError, match="缺少租约 morelogin_env_id"):
        LiveCloudPhoneDeviceBridge().start(backend="cloud_phone")
