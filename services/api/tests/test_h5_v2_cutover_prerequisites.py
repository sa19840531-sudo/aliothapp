"""H5 切 V2 前置检查脚本测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest

from scripts.check_h5_v2_cutover_prerequisites import build_report, collect_checks, main


def _by_name(checks: Any) -> dict[str, Any]:
    return {item.name: item for item in checks}


def test_shadow_requires_legacy_entry_to_lock_v2_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_settings(monkeypatch, workflow_version="v1", h5_mode="shadow")

    report = build_report(target="shadow")

    assert report["ready"] is False
    assert 'legacy_entry_default_locks_v2' in report['blocking_failures']


def test_on_accepts_v2_api_snapshot_when_legacy_default_remains_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_settings(monkeypatch, workflow_version='v1', h5_mode='on')

    report = build_report(target='on')
    checks = _by_name(collect_checks(target='on'))

    assert report['ready'] is True
    assert report['blocking_failures'] == []
    assert checks['legacy_entry_default_locks_v2'].passed is False
    assert checks['legacy_entry_default_locks_v2'].blocking is False
    assert 'V2 API' in checks['legacy_entry_default_locks_v2'].detail
    assert '任务快照' in checks['legacy_entry_default_locks_v2'].detail


def test_ready_when_workflow_version_is_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_settings(monkeypatch, workflow_version="v2", h5_mode="shadow")

    report = build_report(target="shadow")

    assert report["ready"] is True
    assert report["blocking_failures"] == []


def test_shadow_target_checks_recorder(monkeypatch: pytest.MonkeyPatch) -> None:
    _reset_settings(monkeypatch, workflow_version="v2", h5_mode="shadow")

    checks = _by_name(collect_checks(target="shadow"))

    assert checks["shadow_recorder_available"].passed is True
    assert "v2_handler_available" not in checks


def test_on_target_checks_handler_and_demands_shadow_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_settings(monkeypatch, workflow_version="v2", h5_mode="on")

    checks = _by_name(collect_checks(target="on"))

    assert checks["v2_handler_available"].passed is True
    # 影子期人工确认不能被脚本代替，但必须显式提示
    assert checks["shadow_observed_before_on"].passed is False
    assert checks["shadow_observed_before_on"].blocking is False


def test_evidence_switches_are_warnings_not_blockers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """证据链灰度与 H5 切换是两条独立灰度，不互相阻塞。"""
    _reset_settings(monkeypatch, workflow_version="v2", h5_mode="shadow")

    checks = _by_name(collect_checks(target="shadow"))

    assert checks["evidence_manifest_read_enabled"].blocking is False
    assert checks["web_evidence_exit_gate_enabled"].blocking is False


def test_report_note_refuses_to_claim_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_settings(monkeypatch, workflow_version="v2", h5_mode="shadow")

    report = build_report(target="shadow")

    assert "不代表切换已验证通过" in report["note"]


def test_cli_exit_code_reflects_target_specific_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_settings(monkeypatch, workflow_version='v1', h5_mode='shadow')
    assert main(["--target", "shadow"]) == 2

    _reset_settings(monkeypatch, workflow_version='v1', h5_mode='on')
    assert main(['--target', 'on']) == 0

    _reset_settings(monkeypatch, workflow_version="v2", h5_mode="shadow")
    assert main(["--target", "shadow"]) == 0


def test_cli_json_output_is_machine_readable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _reset_settings(monkeypatch, workflow_version="v2", h5_mode="shadow")

    assert main(["--target", "shadow", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["target"] == "shadow"


def _reset_settings(
    monkeypatch: pytest.MonkeyPatch,
    *,
    workflow_version: str,
    h5_mode: str,
) -> None:
    from services.api.app.core import config

    monkeypatch.setenv("ALIOTH_WORKFLOW_VERSION", workflow_version)
    monkeypatch.setenv("ALIOTH_H5_V2_MODE", h5_mode)
    config.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    from services.api.app.core import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()
