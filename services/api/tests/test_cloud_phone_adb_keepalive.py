"""跳板云手机掉出 adb 列表后只接回仍在转发的端口。"""

from __future__ import annotations

from services.api.app.infrastructure.device.cloud_phone_adb_keepalive import (
    endpoints_missing_from_adb_list,
    jump_adb_endpoints_to_keep,
)


def test_online_jump_endpoint_is_not_reconnected() -> None:
    text = "\n".join(
        [
            "List of devices attached",
            "172.104.172.119:30615	device",
            "172.104.172.119:30660	offline",
        ]
    )
    assert endpoints_missing_from_adb_list(
        text,
        ["172.104.172.119:30615", "172.104.172.119:30660", "172.104.172.119:30907"],
    ) == ["172.104.172.119:30660", "172.104.172.119:30907"]


def test_keepalive_skips_expired_jump_ports(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_TUNNEL", "1")
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_TUNNEL_JUMP_HOST", "172.104.172.119")
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_ONIRENT_ADB",
        ",".join(
            [
                "172.104.172.119:30615",
                "172.104.172.119:30660",
                "172.104.172.119:30907",
                "172.104.172.119:30033",
                "172.104.172.119:30613",
                "51.195.137.27:20233",
            ]
        ),
    )

    assert jump_adb_endpoints_to_keep() == [
        "172.104.172.119:30615",
        "172.104.172.119:30660",
        "172.104.172.119:30907",
    ]
