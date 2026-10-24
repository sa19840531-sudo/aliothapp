"""雲手機 APK 安裝：非串流失敗後應走 push + pm install。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from services.api.app.infrastructure.device.cloud_phone import (
    CloudPhoneController,
    CloudPhoneError,
    same_label_risk_acceptance_payload,
)


def _cp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[CloudPhoneController, Path]:
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB", "1.2.3.4:5555")
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_ADB_AUTH", "")
    monkeypatch.setenv("ALIOTH_MORELOGIN_ADB_AUTO_REFRESH", "0")
    apk = tmp_path / "demo.apk"
    apk.write_bytes(b"a" * 1024)
    phone = CloudPhoneController.__new__(CloudPhoneController)
    phone._endpoint = "1.2.3.4:5555"
    phone._serial = "1.2.3.4:5555"
    phone._emu = MagicMock()
    phone._emu._serial = "1.2.3.4:5555"
    phone._emu._run_adb = MagicMock()
    phone._adb = MagicMock(
        return_value=SimpleNamespace(stdout="connected", stderr="", returncode=0)
    )
    phone._shell_probe_ok = MagicMock(return_value=True)  # type: ignore[method-assign]
    phone._authenticate_adb_shell = MagicMock()  # type: ignore[method-assign]
    return phone, apk


def test_same_label_risk_acceptance_payload_from_remark() -> None:
    assert same_label_risk_acceptance_payload(None) is None
    assert same_label_risk_acceptance_payload("普通备注") is None
    payload = same_label_risk_acceptance_payload(
        "SAME_LABEL_SCAN_SKIP_AUTHORIZED: 仅限 lab 隔离机"
    )
    assert payload is not None
    assert payload["accepted"] is True
    assert payload["source"] == "task_remark"
    assert "lab" in str(payload["authorization_scope"])


def test_install_prefers_no_streaming_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    phone, apk = _cp(monkeypatch, tmp_path)

    def _run(args, *, serial, timeout_seconds):  # noqa: ANN001
        joined = " ".join(args)
        if "install" in args and "--no-streaming" in args:
            return SimpleNamespace(stdout="Success", stderr="", returncode=0)
        raise AssertionError(f"unexpected adb args: {joined}")

    phone._emu._run_adb.side_effect = _run
    out = phone.install_apk(apk, timeout_seconds=60.0)
    assert "Success" in out
    assert "--no-streaming" in out


def test_install_falls_back_to_push_pm_on_abb_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    phone, apk = _cp(monkeypatch, tmp_path)
    calls: list[list[str]] = []

    def _run(args, *, serial, timeout_seconds):  # noqa: ANN001
        calls.append(list(args))
        if args[:1] == ["install"] and "--no-streaming" in args:
            return SimpleNamespace(
                stdout="Performing Streamed Install",
                stderr="adb: failed to run abb_exec. Error: closed",
                returncode=1,
            )
        if args[:1] == ["push"]:
            return SimpleNamespace(stdout="1 file pushed", stderr="", returncode=0)
        if args[:2] == ["shell", "pm"]:
            return SimpleNamespace(stdout="Success", stderr="", returncode=0)
        if args[:2] == ["shell", "rm"]:
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        raise AssertionError(f"unexpected adb args: {args}")

    phone._emu._run_adb.side_effect = _run
    out = phone.install_apk(apk, timeout_seconds=60.0)
    assert "cloud_phone install fallback: push + pm install" in out
    assert "Success" in out
    assert any(c[:1] == ["push"] for c in calls)
    assert any(c[:2] == ["shell", "pm"] for c in calls)


def test_is_stream_install_fragile_error() -> None:
    assert CloudPhoneController._is_stream_install_fragile_error(
        "adb: failed to run abb_exec. Error: closed"
    )
    assert not CloudPhoneController._is_stream_install_fragile_error("Failure [INSTALL_FAILED]")


def test_install_push_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    phone, apk = _cp(monkeypatch, tmp_path)

    def _run(args, *, serial, timeout_seconds):  # noqa: ANN001
        if args[:1] == ["install"]:
            return SimpleNamespace(stdout="", stderr="abb_exec closed", returncode=1)
        if args[:1] == ["push"]:
            return SimpleNamespace(stdout="", stderr="error: closed", returncode=1)
        return SimpleNamespace(stdout="", stderr="", returncode=0)

    phone._emu._run_adb.side_effect = _run
    with pytest.raises(CloudPhoneError, match="adb push"):
        phone.install_apk(apk, timeout_seconds=60.0)


def test_explicit_risk_acceptance_skips_wan_same_label_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    phone, apk = _cp(monkeypatch, tmp_path)
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_SAME_LABEL_SCAN_ENABLED", "0")
    original_list = phone._emu.list_user_installed_packages
    auth = "SAME_LABEL_SCAN_SKIP_AUTHORIZED: lab isolation only"

    def _ensure(path: Path, *, timeout_seconds: float):
        assert path == apk
        assert timeout_seconds >= 300
        assert phone._emu.list_user_installed_packages(timeout_seconds=30) == []
        return "apk package=uk.pnixc.juywgn", "uk.pnixc.juywgn", True

    phone._emu.ensure_apk_installed.side_effect = _ensure

    logs, package_name, installed_now = phone.ensure_apk_installed(
        apk,
        same_label_skip_authorization=auth,
    )

    assert package_name == "uk.pnixc.juywgn"
    assert installed_now is True
    assert "same-label risk scan skipped" in logs
    assert "authorization_scope=" in logs
    assert phone._emu.list_user_installed_packages is original_list


def test_env_zero_without_task_remark_keeps_same_label_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    phone, apk = _cp(monkeypatch, tmp_path)
    monkeypatch.setenv("ALIOTH_CLOUD_PHONE_SAME_LABEL_SCAN_ENABLED", "0")
    original_list = phone._emu.list_user_installed_packages
    phone._emu.ensure_apk_installed.return_value = ("checked", "demo.pkg", False)

    logs, package_name, installed_now = phone.ensure_apk_installed(apk)

    assert package_name == "demo.pkg"
    assert installed_now is False
    assert "same-label risk scan kept" in logs
    assert phone._emu.list_user_installed_packages is original_list


def test_wan_same_label_scan_remains_enabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    phone, apk = _cp(monkeypatch, tmp_path)
    monkeypatch.delenv("ALIOTH_CLOUD_PHONE_SAME_LABEL_SCAN_ENABLED", raising=False)
    original_list = phone._emu.list_user_installed_packages
    phone._emu.ensure_apk_installed.return_value = ("checked", "demo.pkg", False)

    result = phone.ensure_apk_installed(apk)

    assert result == ("checked", "demo.pkg", False)
    assert phone._emu.list_user_installed_packages is original_list
