"""MoreLogin 开机业务拒绝与精确等待语义回归。"""

from __future__ import annotations

import pytest

from services.api.app.infrastructure.device import morelogin_adb as ml


@pytest.fixture(autouse=True)
def _isolate_morelogin(monkeypatch: pytest.MonkeyPatch) -> None:
    """单测只走替身，不能继承宿主凭证访问实际云机。"""
    monkeypatch.setenv("ALIOTH_MORELOGIN_API_MODE", "local")
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_VENDOR", "morelogin")
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_POWER_ON", "1")
    monkeypatch.setenv("ALIOTH_MORELOGIN_LOCAL_API", "http://127.0.0.1:40000")


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (33323, "Profile is starting"),
        (33324, "Profile is already running"),
    ],
)
def test_exact_started_codes_allow_bounded_ready_polling(
    monkeypatch: pytest.MonkeyPatch, code: int, message: str
) -> None:
    states = iter([2, 3, 4])
    power_requests: list[str] = []
    sleeps: list[float] = []

    def _status(env_id: str, **_kwargs) -> ml.MoreLoginPhoneStatus:
        return ml.MoreLoginPhoneStatus(env_id, next(states), True)

    def _post(url: str, _payload: dict, **_kwargs) -> dict:
        power_requests.append(url)
        return {"code": code, "msg": message}

    monkeypatch.setattr(ml, "fetch_morelogin_phone_status", _status)
    monkeypatch.setattr(ml, "_post_json", _post)
    monkeypatch.setattr(ml.time, "sleep", sleeps.append)

    logs = ml.ensure_morelogin_cloudphone_powered_on("99", wait_seconds=10)

    assert len(power_requests) == 1
    assert sleeps == [3.0]
    assert any("phone ready envStatus=4" in line for line in logs)


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (33320, "The proxy bound to this Cloud Phone was deleted"),
        (33318, "Insufficient balance to power on"),
        (33367, "Failed to boot: model is under maintenance"),
        (33321, "Profile unavailable; cannot be running"),
        (33325, "云手机未激活，无法开机"),
        (99000, "Unknown failure while booting"),
        (99000, "Profile is already running"),
        (99000, "已经开机"),
        (99000, "已開機"),
    ],
)
def test_business_rejection_preserves_reason_without_polling(
    monkeypatch: pytest.MonkeyPatch, code: int, message: str
) -> None:
    status_requests: list[str] = []
    power_requests: list[str] = []

    def _status(env_id: str, **_kwargs) -> ml.MoreLoginPhoneStatus:
        status_requests.append(env_id)
        return ml.MoreLoginPhoneStatus(env_id, 2, True)

    def _post(url: str, _payload: dict, **_kwargs) -> dict:
        power_requests.append(url)
        return {"code": code, "msg": message, "requestId": "vendor-request-1"}

    def _unexpected_sleep(_seconds: float) -> None:
        pytest.fail("明确开机拒绝不应进入等待轮询")

    monkeypatch.setattr(ml, "fetch_morelogin_phone_status", _status)
    monkeypatch.setattr(ml, "_post_json", _post)
    monkeypatch.setattr(ml.time, "sleep", _unexpected_sleep)

    with pytest.raises(ml.MoreLoginAdbError) as error:
        ml.ensure_morelogin_cloudphone_powered_on("99", wait_seconds=10)

    assert f"code={code}" in str(error.value)
    assert f"msg={message}" in str(error.value)
    assert "requestId=vendor-request-1" in str(error.value)
    assert "逾時" not in str(error.value)
    assert status_requests == ["99"]
    assert len(power_requests) == 1


def test_unknown_transport_error_is_not_replaced_by_ready_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = ml.MoreLoginAdbError("MoreLogin Open API HTTP 503：启动结果未知")
    monkeypatch.setattr(
        ml,
        "fetch_morelogin_phone_status",
        lambda env_id, **_kwargs: ml.MoreLoginPhoneStatus(env_id, 2, True),
    )

    def _post(*_args, **_kwargs) -> dict:
        raise original

    monkeypatch.setattr(ml, "_post_json", _post)

    with pytest.raises(ml.MoreLoginAdbError) as error:
        ml.ensure_morelogin_cloudphone_powered_on("99", wait_seconds=0)

    assert error.value is original


@pytest.mark.parametrize("state", [3, 5, 6, 7])
def test_existing_power_operation_waits_for_exact_powered_on_state(
    monkeypatch: pytest.MonkeyPatch, state: int
) -> None:
    states = iter([state, state, 4])
    sleeps: list[float] = []

    def _status(env_id: str, **_kwargs) -> ml.MoreLoginPhoneStatus:
        return ml.MoreLoginPhoneStatus(env_id, next(states), True)

    def _unexpected_power(*_args, **_kwargs) -> None:
        pytest.fail("已有开机／重置／重启／换机操作时不能再次发起开机")

    monkeypatch.setattr(ml, "fetch_morelogin_phone_status", _status)
    monkeypatch.setattr(ml, "power_on_morelogin_cloudphone", _unexpected_power)
    monkeypatch.setattr(ml.time, "sleep", sleeps.append)

    logs = ml.ensure_morelogin_cloudphone_powered_on("99", wait_seconds=10)

    assert sleeps == [3.0]
    assert any(f"existing power operation envStatus={state}" in line for line in logs)
    assert any("phone ready envStatus=4" in line for line in logs)


@pytest.mark.parametrize("state", [5, 6, 7])
def test_nonready_power_operation_keeps_original_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch, state: int
) -> None:
    monkeypatch.setattr(
        ml,
        "fetch_morelogin_phone_status",
        lambda env_id, **_kwargs: ml.MoreLoginPhoneStatus(env_id, state, True),
    )

    def _unexpected_power(*_args, **_kwargs) -> None:
        pytest.fail("重置／重启／换机中不能误当成可开机资源")

    monkeypatch.setattr(ml, "power_on_morelogin_cloudphone", _unexpected_power)

    with pytest.raises(ml.MoreLoginAdbError, match=f"envStatus={state}，需 =4"):
        ml.ensure_morelogin_cloudphone_powered_on("99", wait_seconds=0)


@pytest.mark.parametrize("state", [None, 0, 1, 2, 3, 5, 6, 7])
def test_nonready_adb_result_never_applies_or_returns_endpoint(
    monkeypatch: pytest.MonkeyPatch, state: int | None
) -> None:
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "1")
    monkeypatch.setattr(
        ml, "ensure_morelogin_cloudphone_powered_on", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        ml,
        "fetch_morelogin_adb_info",
        lambda env_id, **_kwargs: ml.MoreLoginAdbInfo(
            env_id, "127.0.0.1:5555", "unused-test-code", True, state
        ),
    )

    def _unexpected_apply(_info: ml.MoreLoginAdbInfo) -> None:
        pytest.fail("非已开机状态不能发布 ADB 端点或改写进程设备绑定")

    monkeypatch.setattr(ml, "apply_morelogin_adb_to_env", _unexpected_apply)

    with pytest.raises(ml.MoreLoginAdbError, match=f"envStatus={state}（需 =4"):
        ml.refresh_cloud_phone_adb_from_morelogin("99")
