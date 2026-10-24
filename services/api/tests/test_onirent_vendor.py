"""ONIRENT 供應商與設備池綁定單測。"""

from __future__ import annotations

import pytest

from services.api.app.application.device_pool import parse_device_pool_slot_seeds
from services.api.app.infrastructure.device import morelogin_adb as ml
from services.api.app.infrastructure.device.cloud_phone_vendor import (
    cloud_phone_vendor_is_onirent,
    looks_like_adb_endpoint,
    resolve_cloud_phone_vendor,
)
from services.api.app.infrastructure.device.morelogin_device_api import (
    should_use_morelogin_device_api,
)


def test_looks_like_adb_endpoint() -> None:
    assert looks_like_adb_endpoint("51.89.167.239:20195")
    assert looks_like_adb_endpoint("127.0.0.1:5555")
    assert looks_like_adb_endpoint("emulator-5554")
    assert not looks_like_adb_endpoint("1716966721435348")
    assert not looks_like_adb_endpoint("http://127.0.0.1:40000")


def test_vendor_onirent_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB", raising=False)
    assert resolve_cloud_phone_vendor() == "onirent"
    assert cloud_phone_vendor_is_onirent()


def test_parse_pool_uses_onirent_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_SOURCE", "env")
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_SLOTS",
        "cloud_phone:qa-a,cloud_phone:qa-b",
    )
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_ONIRENT_ADB",
        "51.89.167.239:20195,51.195.137.27:20086",
    )
    seeds = parse_device_pool_slot_seeds()
    assert len(seeds) == 2
    assert seeds[0].morelogin_env_id == "51.89.167.239:20195"
    assert seeds[0].capabilities()["vendor"] == "onirent"
    assert seeds[1].morelogin_env_id == "51.195.137.27:20086"


def test_list_pool_from_cc_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_SOURCE", "ws")
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB", raising=False)

    def _fake_list() -> list[str]:
        return ["172.16.0.53:5555", "172.16.0.180:5555"]

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.onirent_ws.fetch_cc_device_list",
        _fake_list,
    )
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        list_pool_onirent_adb_endpoints,
    )

    assert list_pool_onirent_adb_endpoints() == [
        "172.16.0.53:5555",
        "172.16.0.180:5555",
    ]


def test_fetch_cc_device_list_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from services.api.app.infrastructure.device import onirent_ws as ws

    class _FakeSock:
        def __init__(self) -> None:
            body = json.dumps(
                {
                    "id": "abc",
                    "state": 0,
                    "message": "",
                    "data": ["172.16.0.53:5555", "172.16.0.180:5555"],
                },
                ensure_ascii=False,
            ).encode("utf-8")
            frame = bytes([0x81, len(body)]) + body
            handshake = (
                b"HTTP/1.1 101 Switching Protocols\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"\r\n"
            )
            self._buf = handshake + frame

        def settimeout(self, _t: float) -> None:
            return None

        def sendall(self, _data: bytes) -> None:
            return None

        def recv(self, n: int) -> bytes:
            if not self._buf:
                return b""
            take = self._buf[:n]
            self._buf = self._buf[n:]
            return take

        def close(self) -> None:
            return None

    monkeypatch.setattr(ws.socket, "create_connection", lambda *_a, **_k: _FakeSock())
    assert ws.fetch_cc_device_list(request_id="abc") == [
        "172.16.0.53:5555",
        "172.16.0.180:5555",
    ]


def test_refresh_static_onirent_adb(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    info, logs = ml.refresh_cloud_phone_adb_from_morelogin(
        "51.89.167.239:20195",
        allow_global_fallback=False,
        apply_to_process_env=False,
    )
    assert info is not None
    assert info.endpoint == "51.89.167.239:20195"
    assert info.auth == ""
    assert any("onirent static adb" in line for line in logs)


def test_should_not_use_morelogin_api_for_onirent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "open")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key")
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_MORELOGIN_API", "hybrid")
    assert not should_use_morelogin_device_api("51.89.167.239:20195")


def test_power_off_skipped_for_onirent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF", "1")
    logs = ml.maybe_power_off_morelogin_after_lease(
        backend="cloud_phone",
        morelogin_env_id="51.89.167.239:20195",
        reason="test",
    )
    assert any("onirent" in line and "skipped" in line for line in logs)


def test_tunnel_source_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_SOURCE", "tunnel")
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB", raising=False)
    monkeypatch.delenv("ALIOTH_ONIRENT_ADB_TUNNEL_LOCAL", raising=False)
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        list_pool_onirent_adb_endpoints,
        resolve_onirent_adb_source,
    )
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        DEFAULT_TUNNEL_JUMP_HOST,
        DEFAULT_TUNNEL_LOCAL_ENDPOINT,
        DEFAULT_TUNNEL_MAP,
        DEFAULT_TUNNEL_PUBLIC_ENDPOINT,
        DEFAULT_TUNNEL_SSH_FINGERPRINT,
        DEFAULT_TUNNEL_SSH_PUBLIC_KEY,
        DEFAULT_TUNNEL_SSH_USER,
        map_onirent_public_adb_to_tunnel,
        resolve_onirent_adb_tunnel_jump_host,
        resolve_onirent_adb_tunnel_ssh_user,
    )

    expected_locals = [local for _, local in DEFAULT_TUNNEL_MAP]
    assert resolve_onirent_adb_source() == "tunnel"
    assert list_pool_onirent_adb_endpoints() == expected_locals
    assert map_onirent_public_adb_to_tunnel(DEFAULT_TUNNEL_PUBLIC_ENDPOINT) == (
        DEFAULT_TUNNEL_LOCAL_ENDPOINT
    )
    assert map_onirent_public_adb_to_tunnel("51.89.164.208:20613") == (
        f"{DEFAULT_TUNNEL_JUMP_HOST}:30613"
    )
    assert map_onirent_public_adb_to_tunnel("51.195.137.27:20033") == (
        f"{DEFAULT_TUNNEL_JUMP_HOST}:30033"
    )
    assert map_onirent_public_adb_to_tunnel("198.244.190.180:20907") == (
        f"{DEFAULT_TUNNEL_JUMP_HOST}:30907"
    )
    assert map_onirent_public_adb_to_tunnel("51.195.137.27:20660") == (
        f"{DEFAULT_TUNNEL_JUMP_HOST}:30660"
    )
    assert resolve_onirent_adb_tunnel_jump_host() == DEFAULT_TUNNEL_JUMP_HOST
    assert resolve_onirent_adb_tunnel_ssh_user() == DEFAULT_TUNNEL_SSH_USER
    assert DEFAULT_TUNNEL_SSH_PUBLIC_KEY.startswith("ssh-ed25519 ")
    assert DEFAULT_TUNNEL_SSH_FINGERPRINT.startswith("SHA256:")
    assert "alioth-linode-tunnel" in DEFAULT_TUNNEL_SSH_PUBLIC_KEY
    assert DEFAULT_TUNNEL_LOCAL_ENDPOINT.startswith(f"{DEFAULT_TUNNEL_JUMP_HOST}:")
    assert all(
        local.startswith(f"{DEFAULT_TUNNEL_JUMP_HOST}:") for _, local in DEFAULT_TUNNEL_MAP
    )


def test_default_slot_codes_align_with_tunnel_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB", raising=False)
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        DEFAULT_DEVICE_SLOT_CODES,
        DEFAULT_TUNNEL_PORT_MAP,
        list_default_device_pool_slots,
        list_onirent_adb_tunnel_endpoints,
    )

    assert DEFAULT_DEVICE_SLOT_CODES == ("qa-a", "qa-b", "qa-c", "qa-d", "qa-e")
    assert len(DEFAULT_DEVICE_SLOT_CODES) == len(DEFAULT_TUNNEL_PORT_MAP)
    assert DEFAULT_TUNNEL_PORT_MAP[0][0].endswith(":20615")  # qa-a
    assert DEFAULT_TUNNEL_PORT_MAP[1][0].endswith(":20660")  # qa-b
    assert DEFAULT_TUNNEL_PORT_MAP[2][0].endswith(":20907")  # qa-c
    assert DEFAULT_TUNNEL_PORT_MAP[3][0].endswith(":20033")  # qa-d
    assert DEFAULT_TUNNEL_PORT_MAP[4][0].endswith(":20613")  # qa-e
    slots = list_default_device_pool_slots()
    assert slots == [
        "cloud_phone:qa-a",
        "cloud_phone:qa-b",
        "cloud_phone:qa-c",
        "cloud_phone:qa-d",
        "cloud_phone:qa-e",
    ]
    assert len(list_onirent_adb_tunnel_endpoints()) == 5


def test_tunnel_flag_with_auto_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_SOURCE", "auto")
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_TUNNEL", "1")
    monkeypatch.delenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB", raising=False)
    from services.api.app.infrastructure.device.cloud_phone_vendor import (
        list_pool_onirent_adb_endpoints,
        resolve_onirent_adb_source,
    )
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        DEFAULT_TUNNEL_MAP,
    )

    assert resolve_onirent_adb_source() == "tunnel"
    assert list_pool_onirent_adb_endpoints() == [local for _, local in DEFAULT_TUNNEL_MAP]


def test_parse_pool_tunnel_single_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "onirent")
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_SOURCE", "tunnel")
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_SLOTS", "cloud_phone:qa-a")
    # 显式单端点，避免默认四台与单槽不等长
    monkeypatch.setenv("ALIOTH_DEVICE_POOL_ONIRENT_ADB", "172.104.172.119:30615")
    seeds = parse_device_pool_slot_seeds()
    assert len(seeds) == 1
    assert seeds[0].morelogin_env_id == "172.104.172.119:30615"
    assert seeds[0].capabilities()["vendor"] == "onirent"


def test_tunnel_skips_docker_loopback_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_SOURCE", "tunnel")
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB_LOOPBACK_HOST", "host.docker.internal")
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        DEFAULT_TUNNEL_JUMP_HOST,
    )

    jump = f"{DEFAULT_TUNNEL_JUMP_HOST}:30615"
    assert ml.rewrite_loopback_adb_endpoint(jump) == jump
    assert ml.rewrite_loopback_adb_endpoint("51.89.164.208:20615") == jump
    assert ml.rewrite_loopback_adb_endpoint(
        "198.244.190.180:20907"
    ) == f"{DEFAULT_TUNNEL_JUMP_HOST}:30907"


def test_resolve_cloud_phone_endpoint_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.api.app.infrastructure.device.cloud_phone import (
        resolve_cloud_phone_endpoint,
    )
    from services.api.app.infrastructure.device.onirent_adb_tunnel import (
        DEFAULT_TUNNEL_JUMP_HOST,
    )

    monkeypatch.setenv("ALIOTH_ONIRENT_ADB_SOURCE", "tunnel")
    monkeypatch.delenv("ALIOTH_CLOUD_PHONE_ADB", raising=False)
    monkeypatch.delenv("ALIOTH_REDROID_ADB", raising=False)
    assert resolve_cloud_phone_endpoint() == f"{DEFAULT_TUNNEL_JUMP_HOST}:30615"
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB", "51.89.164.208:20615")
    assert resolve_cloud_phone_endpoint() == f"{DEFAULT_TUNNEL_JUMP_HOST}:30615"
