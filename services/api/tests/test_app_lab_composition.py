"""App Lab 组合根开关回归。"""

from __future__ import annotations

import pytest

from services.api.app.application.app_bounded_exploration_milestones import (
    milestone_authority_enabled,
)
from services.api.app.application.app_command_broker import (
    AppCommandBrokerMode,
    resolve_app_command_broker_mode,
)
from services.api.app.application.app_exploration_grayscale import (
    milestone_policy_from_coverage_metadata,
    resolve_app_m5_schedules_from_graph,
)
from services.api.app.application.app_graph_recapture import resolve_app_m6_runtime_ready
from services.api.app.application.app_lab_composition import (
    APP_DEPLOYMENT_ENVIRONMENT_ENV,
    APP_LAB_EXECUTES_PROBE_ENV,
    APP_LAB_MILESTONE_ENV,
    AppLabCompositionError,
    build_app_lab_composition_audit,
    parse_app_lab_executes_probe,
    parse_app_lab_milestone,
    resolve_app_lab_composition,
    resolve_effective_app_milestone_policy,
)
from services.api.app.application.supplemental_evidence_policy import (
    resolve_supplemental_evidence_policy,
)


@pytest.fixture(autouse=True)
def _clear_production_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lab 单测默认非 production，避免本机 ALIOTH_ENVIRONMENT 误伤。"""

    monkeypatch.delenv(APP_DEPLOYMENT_ENVIRONMENT_ENV, raising=False)


def test_lab_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(APP_LAB_MILESTONE_ENV, raising=False)
    monkeypatch.delenv(APP_LAB_EXECUTES_PROBE_ENV, raising=False)
    for key in (
        "ALIOTH_APP_MILESTONE_AUTHORITY_M4",
        "ALIOTH_APP_MILESTONE_AUTHORITY_M5",
        "ALIOTH_APP_MILESTONE_AUTHORITY_M6",
        "ALIOTH_APP_MILESTONE_AUTHORITY_M7",
    ):
        monkeypatch.delenv(key, raising=False)
    composition = resolve_app_lab_composition()
    assert composition["enabled"] is False
    assert composition["lab_plan_ready"] is False
    assert composition["lab_executes_probe"] is False
    assert composition["lab_supplemental_ready"] is False
    policy = composition["milestone_policy"]
    assert milestone_authority_enabled(policy, "M4") is False
    assert resolve_app_command_broker_mode(policy) is AppCommandBrokerMode.OBSERVE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("M4", "M4"),
        ("m5", "M5"),
        ("M6", "M6"),
        ("m7", "M7"),
        ("off", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_lab_milestone(raw: str | None, expected: str | None) -> None:
    assert parse_app_lab_milestone(raw) == expected


def test_parse_lab_milestone_rejects_unknown() -> None:
    with pytest.raises(AppLabCompositionError, match="invalid_"):
        parse_app_lab_milestone("M3")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("1", True),
        ("false", False),
        ("off", False),
        ("", False),
        (None, False),
    ],
)
def test_parse_lab_executes_probe(raw: str | None, expected: bool) -> None:
    assert parse_app_lab_executes_probe(raw) is expected


def test_lab_m4_enables_actions_not_plan_or_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M4")
    monkeypatch.delenv(APP_LAB_EXECUTES_PROBE_ENV, raising=False)
    composition = resolve_app_lab_composition()
    assert composition["enabled"] is True
    assert composition["milestone"] == "M4"
    assert composition["lab_plan_ready"] is False
    assert composition["lab_executes_probe"] is False
    assert composition["lab_supplemental_ready"] is False
    policy = composition["milestone_policy"]
    assert milestone_authority_enabled(policy, "M4") is True
    assert milestone_authority_enabled(policy, "M5") is False
    assert resolve_app_command_broker_mode(policy) is AppCommandBrokerMode.ACTIONS


def test_lab_m5_enables_graph_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M5")
    policy = resolve_effective_app_milestone_policy()
    assert resolve_app_m5_schedules_from_graph(policy) is True
    assert resolve_app_m6_runtime_ready(policy) is False


def test_lab_m6_knife_a_plan_ready_without_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M6")
    monkeypatch.delenv(APP_LAB_EXECUTES_PROBE_ENV, raising=False)
    composition = resolve_app_lab_composition()
    assert composition["lab_plan_ready"] is True
    assert composition["lab_executes_probe"] is False
    assert composition["lab_supplemental_ready"] is False
    assert resolve_app_m6_runtime_ready(composition["milestone_policy"]) is True
    # 刀 A 不得抬升关闭前 Probe 策略。
    policy = resolve_supplemental_evidence_policy(
        enabled=True,
        lab_runtime_ready=bool(composition["lab_executes_probe"]),
    )
    assert policy.runtime_ready is False


def test_lab_m6_knife_b_executes_probe_requires_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M6")
    monkeypatch.setenv(APP_LAB_EXECUTES_PROBE_ENV, "true")
    composition = resolve_app_lab_composition()
    assert composition["lab_plan_ready"] is True
    assert composition["lab_executes_probe"] is True
    assert composition["lab_supplemental_ready"] is True
    policy = resolve_supplemental_evidence_policy(
        enabled=True,
        lab_runtime_ready=True,
    )
    assert policy.runtime_ready is True


def test_lab_executes_probe_rejected_below_m6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M5")
    monkeypatch.setenv(APP_LAB_EXECUTES_PROBE_ENV, "true")
    with pytest.raises(AppLabCompositionError, match="EXECUTES_PROBE"):
        resolve_app_lab_composition()


def test_lab_m7_keeps_plan_ready_gate_authority_probe_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M7")
    monkeypatch.delenv(APP_LAB_EXECUTES_PROBE_ENV, raising=False)
    policy = resolve_effective_app_milestone_policy()
    assert milestone_authority_enabled(policy, "M7") is True
    assert resolve_app_m6_runtime_ready(policy) is True
    composition = resolve_app_lab_composition()
    assert composition["lab_plan_ready"] is True
    assert composition["lab_executes_probe"] is False
    assert composition["lab_supplemental_ready"] is False


def test_lab_conflicts_with_production_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALIOTH_ENVIRONMENT", raising=False)
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M4")
    monkeypatch.setenv("ALIOTH_APP_MILESTONE_AUTHORITY_M4", "1")
    with pytest.raises(AppLabCompositionError, match="conflicts_with_production"):
        resolve_app_lab_composition()


def test_lab_forbidden_in_production_deployment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(APP_DEPLOYMENT_ENVIRONMENT_ENV, "production")
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M6")
    monkeypatch.delenv(APP_LAB_EXECUTES_PROBE_ENV, raising=False)
    for key in (
        "ALIOTH_APP_MILESTONE_AUTHORITY_M4",
        "ALIOTH_APP_MILESTONE_AUTHORITY_M5",
        "ALIOTH_APP_MILESTONE_AUTHORITY_M6",
        "ALIOTH_APP_MILESTONE_AUTHORITY_M7",
    ):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(AppLabCompositionError, match="lab_forbidden_in_production"):
        resolve_app_lab_composition()
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "off")
    composition = resolve_app_lab_composition()
    assert composition["enabled"] is False


def test_coverage_metadata_prefers_stored_lab_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALIOTH_ENVIRONMENT", raising=False)
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M4")
    stored = resolve_effective_app_milestone_policy()
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "off")
    recovered = milestone_policy_from_coverage_metadata(
        {"app_bounded_exploration_milestone_policy": stored}
    )
    assert milestone_authority_enabled(recovered, "M4") is True


def test_lab_audit_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALIOTH_ENVIRONMENT", raising=False)
    monkeypatch.setenv(APP_LAB_MILESTONE_ENV, "M5")
    audit = build_app_lab_composition_audit()
    assert audit["enabled"] is True
    assert audit["milestone"] == "M5"
    assert audit["lab_plan_ready"] is False
    assert audit["lab_executes_probe"] is False
    assert audit["authority_snapshot"]["M4"] is True
    assert audit["authority_snapshot"]["M5"] is True
    assert audit["authority_snapshot"]["M6"] is False
