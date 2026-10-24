"""远程 ADB 长会话健康分状态机测试。"""

from __future__ import annotations

import pytest

from services.api.app.infrastructure.device.adb_session_health import (
    APP_ADB_SESSION_HEALTH_VERSION,
    AdbSessionHealthState,
    recover_cloud_phone_adb_binding,
)


def test_baseline_from_first_four_pulls_and_healthy_score() -> None:
    state = AdbSessionHealthState(session_started_at=0.0, last_refresh_at=0.0)
    for pull in (4000, 5000, 4500, 4800):
        state.record_pull(pull)

    assert state.baseline_pull_ms == 4650  # median of 4000,4500,4800,5000
    assert state.score == 100
    assert state.pending_refresh_reason is None
    assert state.slow_pull_threshold_ms() == 12_000  # abs floor > 2*4650


def test_slow_pull_marks_pending_and_lowers_score() -> None:
    state = AdbSessionHealthState(session_started_at=0.0, last_refresh_at=0.0)
    for pull in (4000, 4000, 4000, 4000):
        state.record_pull(pull)
    state.record_pull(13_000)

    assert state.pending_refresh_reason == "slow_pull"
    assert state.score == 85
    should, reason = state.should_proactive_refresh(now=70.0)
    assert should is True
    assert reason == "slow_pull"


def test_age_triggers_refresh_after_480s() -> None:
    state = AdbSessionHealthState(session_started_at=0.0, last_refresh_at=0.0)
    should, reason = state.should_proactive_refresh(now=479.0)
    assert should is False
    should, reason = state.should_proactive_refresh(now=480.0)
    assert should is True
    assert reason == "age"


def test_cooldown_blocks_refresh() -> None:
    state = AdbSessionHealthState(session_started_at=0.0, last_refresh_at=100.0)
    state.pending_refresh_reason = "slow_pull"
    should, reason = state.should_proactive_refresh(now=130.0)
    assert should is False
    assert state.last_decision_reason == "cooldown"
    should, reason = state.should_proactive_refresh(now=160.0)
    assert should is True
    assert reason == "slow_pull"


def test_low_score_triggers_refresh() -> None:
    state = AdbSessionHealthState(
        session_started_at=0.0,
        last_refresh_at=0.0,
        score=40,
    )
    should, reason = state.should_proactive_refresh(now=70.0)
    assert should is True
    assert reason == "low_score"


def test_mark_refreshed_resets_baseline_and_pending() -> None:
    state = AdbSessionHealthState(session_started_at=0.0, last_refresh_at=0.0)
    for pull in (4000, 4000, 4000, 4000):
        state.record_pull(pull)
    state.record_pull(20_000)
    state.mark_refreshed(reason="slow_pull", now=200.0)

    assert state.refresh_count == 1
    assert state.pending_refresh_reason is None
    assert state.baseline_pull_ms is None
    assert state.recent_pull_ms == []
    assert state.last_refreshed is True
    assert state.last_refresh_at == 200.0
    assert state.score >= 70
    # 本帧随后 record_pull 不得抹掉 refreshed 审计位
    state.record_pull(3000)
    assert state.last_refreshed is True


def test_max_refreshes_stops_proactive() -> None:
    state = AdbSessionHealthState(
        session_started_at=0.0,
        last_refresh_at=0.0,
        refresh_count=12,
        pending_refresh_reason="slow_pull",
    )
    should, reason = state.should_proactive_refresh(now=1000.0)
    assert should is False
    assert reason is None
    assert state.last_decision_reason == "max_refreshes"


def test_to_dict_includes_protocol() -> None:
    state = AdbSessionHealthState(session_started_at=0.0, last_refresh_at=0.0)
    state.record_pull(5000)
    payload = state.to_dict()
    assert payload["protocol_version"] == APP_ADB_SESSION_HEALTH_VERSION
    assert payload["last_recorded_pull_ms"] == 5000
    assert "slow_pull_threshold_ms" in payload


def test_recover_binding_rebuilds_controller_once_after_existing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = object()
    refreshed_ops = object()
    calls: list[object] = []

    class _ReplacementController:
        def ensure_ready(self, *, timeout_seconds: float) -> tuple[str, list[str]]:
            assert timeout_seconds == 25.0
            calls.append(self)
            return "cloud:fresh", ["fresh ready"]

    fresh = _ReplacementController()

    class _StaleController:
        def ensure_ready(self, *, timeout_seconds: float) -> tuple[str, list[str]]:
            assert timeout_seconds == 25.0
            calls.append(self)
            raise RuntimeError("旧控制器绑定失效")

        def fresh_for_adb_refresh(self) -> object:
            calls.append(replacement)
            return fresh

    monkeypatch.setattr(
        "services.api.app.infrastructure.device.adb_session_health.open_adb_device_ops",
        lambda *args, **kwargs: refreshed_ops,
    )

    controller, serial, ops, logs = recover_cloud_phone_adb_binding(
        controller=_StaleController()
    )

    assert controller is fresh
    assert serial == "cloud:fresh"
    assert ops is refreshed_ops
    assert "fresh controller recovery succeeded" in logs[0]
    assert len(calls) == 3


def test_recover_binding_stops_after_fresh_controller_also_fails() -> None:
    class _ReplacementController:
        def ensure_ready(self, *, timeout_seconds: float) -> tuple[str, list[str]]:
            del timeout_seconds
            raise RuntimeError("新控制器仍失败")

    class _StaleController:
        def ensure_ready(self, *, timeout_seconds: float) -> tuple[str, list[str]]:
            del timeout_seconds
            raise RuntimeError("旧控制器绑定失效")

        def fresh_for_adb_refresh(self) -> object:
            return _ReplacementController()

    with pytest.raises(RuntimeError, match="新控制器仍失败") as exc_info:
        recover_cloud_phone_adb_binding(controller=_StaleController())

    assert exc_info.value.__cause__ is not None
    assert "旧控制器绑定失效" in str(exc_info.value.__cause__)


def test_is_transient_adb_transport_error_markers() -> None:
    from services.api.app.infrastructure.device.adb_session_health import (
        is_transient_adb_transport_error,
    )

    assert is_transient_adb_transport_error(TimeoutError("timed out"))
    assert is_transient_adb_transport_error(
        RuntimeError("adb.exe: failed to connect device offline")
    )
    assert is_transient_adb_transport_error(
        RuntimeError("error: closed")
    )
    assert not is_transient_adb_transport_error(
        ValueError("semantic target not found")
    )


def test_run_with_cloud_phone_adb_reconnect_retries_then_succeeds() -> None:
    from services.api.app.infrastructure.device.adb_session_health import (
        run_with_cloud_phone_adb_reconnect,
    )

    calls = {"run": 0, "reconnect": 0, "sleeps": []}

    def run() -> str:
        calls["run"] += 1
        if calls["run"] < 3:
            raise RuntimeError("device offline")
        return "ok"

    def reconnect() -> None:
        calls["reconnect"] += 1

    result = run_with_cloud_phone_adb_reconnect(
        run=run,
        reconnect=reconnect,
        sleep=lambda seconds: calls["sleeps"].append(seconds),
    )

    assert result == "ok"
    assert calls["run"] == 3
    assert calls["reconnect"] == 2
    assert calls["sleeps"] == [0.5, 1.0]


def test_run_with_cloud_phone_adb_reconnect_skips_non_transient() -> None:
    from services.api.app.infrastructure.device.adb_session_health import (
        run_with_cloud_phone_adb_reconnect,
    )

    reconnects = {"n": 0}

    def run() -> None:
        raise ValueError("坐标非法")

    with pytest.raises(ValueError, match="坐标非法"):
        run_with_cloud_phone_adb_reconnect(
            run=run,
            reconnect=lambda: reconnects.__setitem__("n", reconnects["n"] + 1),
            sleep=lambda _s: None,
        )

    assert reconnects["n"] == 0
