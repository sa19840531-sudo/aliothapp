"""MoreLogin ADB 自動刷新單元測試。"""

from __future__ import annotations

import pytest

from services.api.app.infrastructure.device import morelogin_adb as ml


@pytest.fixture(autouse=True)
def _isolate_morelogin_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """隔离宿主供应商与凭证，避免本地单测切换静态设备或远端路径。"""
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "morelogin")
    for key in (
        "ALIOTH_MORELOGIN_API_MODE",
        "ALIOTH_MORELOGIN_API_ID",
        "ALIOTH_MORELOGIN_API_KEY",
        "ML_API_ID",
        "ML_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)


def test_refresh_skipped_without_env_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", raising=False)
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "1")
    info, logs = ml.refresh_cloud_phone_adb_from_morelogin()
    assert info is None
    assert any("CLOUDPHONE_ID unset" in x for x in logs)


def test_refresh_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "0")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "123")
    info, logs = ml.refresh_cloud_phone_adb_from_morelogin()
    assert info is None
    assert any("disabled" in x for x in logs)


def test_fetch_from_batch_adb_info(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        assert "batchAdbInfo" in url
        assert payload["envIds"] == [1716966721435348]
        return {
            "code": 0,
            "data": [
                {
                    "id": "1716966721435348",
                    "envName": "CP-1",
                    "enableAdb": True,
                    "envStatus": 4,
                    "adbIp": "1.2.3.4",
                    "adbPort": "20268",
                    "adbPassword": "secret-code",
                }
            ],
        }

    monkeypatch.setattr(ml, "_post_json", _fake_post)
    info = ml.fetch_morelogin_adb_info("1716966721435348")
    assert info.endpoint == "1.2.3.4:20268"
    assert info.auth == "secret-code"
    assert info.env_name == "CP-1"


def test_refresh_applies_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "99")
    monkeypatch.setenv("ALIOTH_MORELOGIN_LOCAL_API", "http://127.0.0.1:40000")
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON", "1")

    def _fake_power(env_id: str, *, api_base: str | None = None, wait_seconds: float | None = None):
        assert env_id == "99"
        return ["morelogin phone already powered on"]

    def _fake_fetch(env_id: str, *, api_base: str | None = None, timeout_seconds: float = 20.0):
        assert env_id == "99"
        return ml.MoreLoginAdbInfo(
            env_id="99",
            endpoint="9.9.9.9:1111",
            auth="abc",
            enable_adb=True,
            env_status=4,
            env_name="CP",
        )

    monkeypatch.setattr(ml, "ensure_morelogin_cloudphone_powered_on", _fake_power)
    monkeypatch.setattr(ml, "fetch_morelogin_adb_info", _fake_fetch)
    info, logs = ml.refresh_cloud_phone_adb_from_morelogin()
    assert info is not None
    assert info.endpoint == "9.9.9.9:1111"
    assert ml.os.environ["ALIOTH_CLOUD_PHONE_ADB"] == "9.9.9.9:1111"
    assert ml.os.environ["ALIOTH_CLOUD_PHONE_ADB_AUTH"] == "abc"
    assert any("endpoint=9.9.9.9:1111" in x for x in logs)
    assert any("already powered on" in x for x in logs)
    assert all("abc" not in x or "auth=set" in x for x in logs)


def test_ensure_power_on_calls_api_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON", "1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_POWER_ON_WAIT_SECONDS", "30")
    calls: list[str] = []
    statuses = iter(
        [
            ml.MoreLoginPhoneStatus("99", 0, False, "CP-1", None),
            ml.MoreLoginPhoneStatus("99", 2, True, "CP-1", None),
            ml.MoreLoginPhoneStatus("99", 4, True, "CP-1", None),
        ]
    )

    def _status(env_id: str, *, api_base: str | None = None, timeout_seconds: float = 20.0):
        return next(statuses)

    def _power(env_id: str, *, api_base: str | None = None, headless: bool = True, timeout_seconds: float = 60.0):
        calls.append(env_id)

    monkeypatch.setattr(ml, "fetch_morelogin_phone_status", _status)
    monkeypatch.setattr(ml, "power_on_morelogin_cloudphone", _power)
    monkeypatch.setattr(ml.time, "sleep", lambda *_a, **_k: None)
    logs = ml.ensure_morelogin_cloudphone_powered_on("99")
    assert calls == ["99"]
    assert any("powerOn requested" in x or "powerOn envId" in x for x in logs)
    assert any("phone ready" in x for x in logs)


def test_parse_page_adb_info_nested() -> None:
    row = {
        "id": "1",
        "envName": "CP-1",
        "enableAdb": True,
        "envStatus": 4,
        "adbInfo": {
            "adbIp": "98.98.125.30",
            "adbPort": "20268",
            "adbPassword": "x",
        },
    }
    info = ml._parse_adb_row(row, fallback_id="1")
    assert info is not None
    assert info.endpoint == "98.98.125.30:20268"


def test_rewrite_loopback_adb_endpoint_for_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB_LOOPBACK_HOST", "host.docker.internal")
    assert ml.rewrite_loopback_adb_endpoint("localhost:60354") == "host.docker.internal:60354"
    assert ml.rewrite_loopback_adb_endpoint("127.0.0.1:60354") == "host.docker.internal:60354"
    assert ml.rewrite_loopback_adb_endpoint("98.98.125.30:20268") == "98.98.125.30:20268"


def test_parse_loopback_adb_rewrites_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB_LOOPBACK_HOST", "host.docker.internal")
    row = {
        "id": "2",
        "envName": "CP-2",
        "enableAdb": True,
        "envStatus": 4,
        "adbInfo": {
            "adbIp": "localhost",
            "adbPort": "60354",
            "adbPassword": "secret",
        },
    }
    info = ml._parse_adb_row(row, fallback_id="2")
    assert info is not None
    assert info.endpoint == "host.docker.internal:60354"


def test_as_bool_rejects_false_string() -> None:
    assert ml._as_bool("false") is False
    assert ml._as_bool("0") is False
    assert ml._as_bool("true") is True
    assert ml._as_bool(True) is True


def test_apply_env_clears_stale_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB_AUTH", "old-secret")
    info = ml.MoreLoginAdbInfo(
        env_id="1",
        endpoint="1.1.1.1:1",
        auth="",
        enable_adb=True,
        env_status=4,
    )
    ml.apply_morelogin_adb_to_env(info)
    assert "ALIOTH_CLOUD_PHONE_ADB_AUTH" not in ml.os.environ


def test_fetch_phone_status_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        1: {
            "code": 0,
            "data": {
                "total": 51,
                "dataList": [{"id": str(i), "envStatus": 4, "enableAdb": "false"} for i in range(1, 51)],
            },
        },
        2: {
            "code": 0,
            "data": {
                "total": 51,
                "dataList": [
                    {
                        "id": "99",
                        "envName": "CP-X",
                        "envStatus": 4,
                        "enableAdb": "true",
                        "adbInfo": {"adbIp": "2.2.2.2", "adbPort": "9", "adbPassword": "p"},
                    }
                ],
            },
        },
    }

    def _post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        assert "page" in url
        return pages[int(payload["page"])]

    monkeypatch.setattr(ml, "_post_json", _post)
    status = ml.fetch_morelogin_phone_status("99")
    assert status.env_name == "CP-X"
    assert status.enable_adb is True
    assert status.adb is not None
    assert status.adb.endpoint == "2.2.2.2:9"


def test_power_on_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON", raising=False)
    assert ml.morelogin_adb_auto_power_on_enabled() is False


def test_cloud_phone_ensure_ready_uses_refreshed_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from services.api.app.infrastructure.device.cloud_phone import CloudPhoneController

    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB", "0.0.0.0:1")
    sdk = tmp_path / "sdk"
    (sdk / "platform-tools").mkdir(parents=True)
    adb = sdk / "platform-tools" / "adb.exe"
    adb.write_text("", encoding="utf-8")
    monkeypatch.setenv("ANDROID_HOME", str(sdk))

    ctl = CloudPhoneController()

    def _refresh(logs: list[str]) -> None:
        logs.append("morelogin adb refreshed endpoint=8.8.8.8:5555 auth=set")
        ctl._endpoint = "8.8.8.8:5555"

    monkeypatch.setattr(ctl, "_refresh_endpoint_from_morelogin", _refresh)
    monkeypatch.setattr(ctl, "ensure_sdk_adb_server", lambda: ["sdk ok"])

    calls: list[list[str]] = []

    class _Done(Exception):
        pass

    def _adb(args: list[str], *, timeout_seconds: float = 60.0):
        calls.append(list(args))
        if args[:1] == ["connect"]:
            assert args[1] == "8.8.8.8:5555"
            raise _Done("connected-with-refreshed-endpoint")
        raise AssertionError(args)

    monkeypatch.setattr(ctl, "_adb", _adb)
    with pytest.raises(_Done, match="refreshed"):
        ctl.ensure_ready(timeout_seconds=30)
    assert any("connect" in c for c in calls)


def test_refresh_uses_explicit_env_id_not_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-id")
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON", "0")
    seen: list[str] = []

    def _fake_power(env_id: str, *, api_base: str | None = None, wait_seconds: float | None = None):
        seen.append(env_id)
        return ["morelogin phone already powered on"]

    def _fake_fetch(env_id: str, *, api_base: str | None = None, timeout_seconds: float = 20.0):
        seen.append(f"fetch:{env_id}")
        return ml.MoreLoginAdbInfo(
            env_id=env_id,
            endpoint=f"{env_id}.adb:5555",
            auth="secret",
            enable_adb=True,
            env_status=4,
            env_name="CP",
        )

    monkeypatch.setattr(ml, "ensure_morelogin_cloudphone_powered_on", _fake_power)
    monkeypatch.setattr(ml, "fetch_morelogin_adb_info", _fake_fetch)
    info, logs = ml.refresh_cloud_phone_adb_from_morelogin(
        "99001",
        apply_to_process_env=False,
    )
    assert info is not None
    assert info.env_id == "99001"
    assert info.endpoint == "99001.adb:5555"
    assert seen[0] == "99001"
    assert "fetch:99001" in seen
    assert all("global-id" not in item for item in seen)
    assert any("envId=99001" in item for item in logs)


def test_refresh_rejects_global_fallback_on_multi_device_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "global-id")
    with pytest.raises(ml.MoreLoginAdbError, match="禁止回退全域"):
        ml.refresh_cloud_phone_adb_from_morelogin(allow_global_fallback=False)


def test_refresh_without_process_env_does_not_clobber_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "1")
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB", "old:1")
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB_AUTH", "old-auth")

    def _fake_power(env_id: str, *, api_base: str | None = None, wait_seconds: float | None = None):
        return ["morelogin phone already powered on"]

    def _fake_fetch(env_id: str, *, api_base: str | None = None, timeout_seconds: float = 20.0):
        return ml.MoreLoginAdbInfo(
            env_id=env_id,
            endpoint=f"{env_id}:5555",
            auth=f"auth-{env_id}",
            enable_adb=True,
            env_status=4,
        )

    monkeypatch.setattr(ml, "ensure_morelogin_cloudphone_powered_on", _fake_power)
    monkeypatch.setattr(ml, "fetch_morelogin_adb_info", _fake_fetch)
    first, _ = ml.refresh_cloud_phone_adb_from_morelogin("A", apply_to_process_env=False)
    second, _ = ml.refresh_cloud_phone_adb_from_morelogin("B", apply_to_process_env=False)
    assert first is not None and second is not None
    assert first.endpoint == "A:5555"
    assert second.endpoint == "B:5555"
    assert ml.os.environ["ALIOTH_CLOUD_PHONE_ADB"] == "old:1"
    assert ml.os.environ["ALIOTH_CLOUD_PHONE_ADB_AUTH"] == "old-auth"


def test_auto_mode_uses_open_api_when_credentials_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "auto")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id-1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key-1")
    from services.api.app.infrastructure.device import morelogin_http as http

    http.reset_morelogin_open_token_cache()
    assert http.resolve_morelogin_api_mode() == "open"
    assert http.resolve_morelogin_api_base() == "https://api.morelogin.com"
    url = http.morelogin_cloudphone_url("batchAdbInfo")
    assert url == "https://api.morelogin.com/cloudphone/batchAdbInfo"
    assert "/api/cloudphone" not in url


def test_open_api_page_uses_page_no_and_open_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "open")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id-1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key-1")
    monkeypatch.delenv("ALIOTH_MORELOGIN_HTTP_USER_AGENT", raising=False)
    seen: list[tuple[str, dict]] = []

    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        seen.append((url, payload))
        return {"code": 0, "data": {"dataList": [], "total": 0}}

    monkeypatch.setattr(ml, "_post_json", _fake_post)
    with pytest.raises(ml.MoreLoginAdbError, match="找不到雲手機"):
        ml.fetch_morelogin_phone_status("1716966721435348")
    assert seen
    assert seen[0][0] == "https://api.morelogin.com/cloudphone/page"
    assert seen[0][1] == {"pageNo": 1, "pageSize": 50}


def test_open_api_power_on_payload_omits_headless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "open")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id-1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key-1")
    seen: list[tuple[str, dict]] = []

    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        seen.append((url, payload))
        return {"code": 0}

    monkeypatch.setattr(ml, "_post_json", _fake_post)
    ml.power_on_morelogin_cloudphone("99")
    assert seen[0][0] == "https://api.morelogin.com/cloudphone/powerOn"
    assert seen[0][1]["id"] == 99
    assert "headless" not in seen[0][1]
    assert seen[0][1]["disableMoneySavingMode"] is True


def test_power_off_morelogin_cloudphone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "open")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id-1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key-1")
    seen: list[tuple[str, dict]] = []

    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        seen.append((url, payload))
        return {"code": 0}

    monkeypatch.setattr(ml, "_post_json", _fake_post)
    ml.power_off_morelogin_cloudphone("1721996810228932")
    assert seen[0][0] == "https://api.morelogin.com/cloudphone/powerOff"
    assert seen[0][1]["id"] == 1721996810228932


def test_power_off_soft_when_already_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "open")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id-1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key-1")

    def _fake_post(url: str, payload: dict, *, timeout_seconds: float = 20.0) -> dict:
        return {"code": 33325, "msg": "The current profile is stopped."}

    monkeypatch.setattr(ml, "_post_json", _fake_post)
    ml.power_off_morelogin_cloudphone("99")  # 不应抛


def test_maybe_power_off_after_lease_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF", raising=False)
    called: list[str] = []

    def _fake_off(env_id: str, *, api_base: str | None = None, timeout_seconds: float = 60.0) -> None:
        called.append(env_id)

    monkeypatch.setattr(ml, "power_off_morelogin_cloudphone", _fake_off)
    logs = ml.maybe_power_off_morelogin_after_lease(
        backend="cloud_phone",
        morelogin_env_id="1721996810228932",
        reason="lease_released",
    )
    assert called == ["1721996810228932"]
    assert any("powerOff envId=1721996810228932" in x for x in logs)


def test_maybe_power_off_can_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF", "0")
    called: list[str] = []
    monkeypatch.setattr(
        ml,
        "power_off_morelogin_cloudphone",
        lambda *a, **k: called.append("x"),
    )
    logs = ml.maybe_power_off_morelogin_after_lease(
        backend="cloud_phone",
        morelogin_env_id="99",
        reason="lease_released",
    )
    assert called == []
    assert any("disabled" in x for x in logs)


def test_maybe_power_off_skips_non_cloud_phone(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        ml,
        "power_off_morelogin_cloudphone",
        lambda *a, **k: called.append("x"),
    )
    logs = ml.maybe_power_off_morelogin_after_lease(
        backend="ldplayer",
        morelogin_env_id="99",
    )
    assert called == []
    assert logs == []


def test_list_pool_morelogin_env_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS",
        "111,222,222,333",
    )
    monkeypatch.setenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", "111")
    assert ml.list_pool_morelogin_env_ids() == ["111", "222", "333"]


def test_fleet_idle_powers_off_all_except_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_OFF", raising=False)
    monkeypatch.setenv(
        "ALIOTH_DEVICE_POOL_MORELOGIN_ENV_IDS",
        "env-a,env-b,env-c",
    )
    monkeypatch.delenv("ALIOTH_MORELOGIN_CLOUDPHONE_ID", raising=False)
    called: list[str] = []

    def _fake_off(env_id: str, *, api_base: str | None = None, timeout_seconds: float = 60.0) -> None:
        called.append(env_id)

    monkeypatch.setattr(ml, "power_off_morelogin_cloudphone", _fake_off)
    logs = ml.maybe_power_off_morelogin_fleet_idle(
        skip_env_ids={"env-b"},
        reason="fleet_after:lease_released",
    )
    assert called == ["env-a", "env-c"]
    assert any("count=2" in x for x in logs)


def test_open_api_attaches_bearer_and_refreshes_on_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from services.api.app.infrastructure.device import morelogin_http as http

    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "open")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_ID", "id-1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_KEY", "key-1")
    monkeypatch.delenv("ALIOTH_MORELOGIN_HTTP_USER_AGENT", raising=False)
    http.reset_morelogin_open_token_cache()
    calls: list[str] = []
    seen_user_agents: list[tuple[str, str | None]] = []

    def _fake_http(
        url: str,
        payload: dict,
        *,
        headers: dict[str, str],
        timeout_seconds: float,
    ) -> tuple[int, dict]:
        calls.append(url)
        seen_user_agents.append((url, headers.get("User-Agent")))
        if url.endswith("/oauth2/token"):
            assert payload["client_id"] == "id-1"
            assert headers["User-Agent"] == http._DEFAULT_OPEN_API_USER_AGENT
            return 200, {
                "code": 0,
                "data": {"access_token": f"tok-{len(calls)}", "expires_in": 3600},
            }
        assert headers["User-Agent"] == http._DEFAULT_OPEN_API_USER_AGENT
        auth = headers.get("Authorization", "")
        if auth == "Bearer tok-1":
            return 401, {"code": 401, "msg": "expired"}
        assert auth.startswith("Bearer tok-")
        return 200, {"code": 0, "data": []}

    monkeypatch.setattr(http, "_http_json", _fake_http)
    data = http.post_morelogin_json(
        "https://api.morelogin.com/cloudphone/batchAdbInfo",
        {"envIds": [1]},
    )
    assert data["code"] == 0
    assert any(item.endswith("/oauth2/token") for item in calls)
    assert sum(1 for item in calls if item.endswith("/oauth2/token")) == 2
    assert all(
        user_agent == http._DEFAULT_OPEN_API_USER_AGENT
        for _, user_agent in seen_user_agents
    )


def test_open_api_user_agent_can_be_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.api.app.infrastructure.device import morelogin_http as http

    monkeypatch.setenv("ALIOTH_MORELOGIN_HTTP_USER_AGENT", "Alioth-Cloud-QA/2.0")

    assert http.resolve_morelogin_http_user_agent() == "Alioth-Cloud-QA/2.0"
