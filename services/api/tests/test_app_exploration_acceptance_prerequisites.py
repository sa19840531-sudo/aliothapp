"""App 页面探索部署级验收前置门禁测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = ROOT / "scripts/check_app_exploration_acceptance_prerequisites.py"


@pytest.fixture(scope="module")
def prerequisites():
    spec = importlib.util.spec_from_file_location(
        "check_app_exploration_acceptance_prerequisites",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ready_environment() -> dict[str, str]:
    return {
        "ALIOTH_WORKFLOW_VERSION": "v2",
        "ALIOTH_APP_QC_ENABLED": "true",
        "ALIOTH_APP_RUNTIME_PROVIDER": "official",
        "ALIOTH_AGENT_RUN_STATE_STORE_BACKEND": "postgres",
        "ALIOTH_APP_DEVICE_LEASE_BACKEND": "postgres",
        "ALIOTH_DEVICE_POOL_SLOTS": "cloud_phone:acceptance-01,cloud_phone:acceptance-02",
        "ALIOTH_WORKER_ROLES": "explorer,judge",
        "ALIOTH_TEST_DATABASE_URL": "postgresql://test-role:test-secret@postgres/alioth_test",
        "ALIOTH_DATABASE_URL": "postgresql://app-role:app-secret@postgres/alioth",
        "ALIOTH_REDIS_URL": "redis://:redis-secret@redis:6379/0",
        "ALIOTH_EVIDENCE_STORAGE_BACKEND": "minio",
        "ALIOTH_MINIO_ENDPOINT": "minio:9000",
        "ALIOTH_MINIO_ACCESS_KEY": "test-access-key",
        "ALIOTH_MINIO_SECRET_KEY": "minio-secret",
        "ALIOTH_MINIO_BUCKET": "alioth-test",
        "ALIOTH_APP_VISION_PROVIDER": "llm",
        "ALIOTH_LLM_PROVIDER": "openai",
        "ALIOTH_LLM_API_KEY": "llm-secret",
        "ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED": "true",
        "ALIOTH_OFFICIAL_APP_API_BASE_URL": "https://official.example.test",
        "ALIOTH_OFFICIAL_APP_AUTH_TOKEN": "official-secret",
        "ALIOTH_OFFICIAL_APP_CONTRACT_PLATFORM_BUILD_REF": "app-build:test",
        "ALIOTH_OFFICIAL_APP_CONTRACT_PACKAGE_NAME": "com.example.test",
        "ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256": "a" * 64,
    }


def test_missing_environment_is_blocked_without_external_calls(prerequisites) -> None:
    report = prerequisites.build_acceptance_prerequisite_report({})

    assert report.ready_to_run is False
    assert {item.code for item in report.checks if not item.ready} == {
        "app_runtime",
        "isolated_postgres",
        "redis_worker_recovery",
        "minio_evidence_store",
        "visual_provider",
        "official_app_device",
    }


def test_complete_environment_is_only_ready_to_run_not_accepted(prerequisites) -> None:
    report = prerequisites.build_acceptance_prerequisite_report(_ready_environment())
    payload = report.to_dict()

    assert report.ready_to_run is True
    assert payload["status"] == "ready_to_run"
    assert all(item["status"] == "ready" for item in payload["checks"])
    assert any("不执行" in item for item in payload["limitations"])
    assert any("不能把部署验收状态升级为通过" in item for item in payload["limitations"])
    assert any("verify_app_morelogin_acceptance.py" in item for item in payload["limitations"])


def test_report_never_exposes_connection_or_secret_values(prerequisites) -> None:
    environment = _ready_environment()

    serialized = json.dumps(
        prerequisites.build_acceptance_prerequisite_report(environment).to_dict(),
        ensure_ascii=False,
    )

    for name, value in environment.items():
        if name not in {
            "ALIOTH_APP_QC_ENABLED",
            "ALIOTH_WORKFLOW_VERSION",
            "ALIOTH_APP_RUNTIME_PROVIDER",
            "ALIOTH_AGENT_RUN_STATE_STORE_BACKEND",
            "ALIOTH_APP_DEVICE_LEASE_BACKEND",
            "ALIOTH_WORKER_ROLES",
            "ALIOTH_EVIDENCE_STORAGE_BACKEND",
            "ALIOTH_APP_VISION_PROVIDER",
            "ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED",
        }:
            assert value not in serialized


def test_postgres_gate_rejects_primary_database_identity(prerequisites) -> None:
    environment = _ready_environment()
    environment["ALIOTH_TEST_DATABASE_URL"] = (
        "postgresql://other-role:other-secret@postgres:5432/alioth"
    )

    report = prerequisites.build_acceptance_prerequisite_report(environment)
    check = next(item for item in report.checks if item.code == "isolated_postgres")

    assert check.ready is False
    assert check.missing_or_invalid_variables == ("ALIOTH_TEST_DATABASE_URL",)
    assert "other-secret" not in check.message


def test_postgres_gate_requires_primary_database_for_isolation_proof(prerequisites) -> None:
    environment = _ready_environment()
    environment.pop("ALIOTH_DATABASE_URL")

    report = prerequisites.build_acceptance_prerequisite_report(environment)
    check = next(item for item in report.checks if item.code == "isolated_postgres")

    assert check.ready is False
    assert check.missing_or_invalid_variables == ("ALIOTH_DATABASE_URL",)


@pytest.mark.parametrize(
    ("name", "value", "code"),
    [
        ("ALIOTH_EVIDENCE_STORAGE_BACKEND", "local", "minio_evidence_store"),
        ("ALIOTH_APP_VISION_PROVIDER", "fake", "visual_provider"),
        ("ALIOTH_APP_RUNTIME_PROVIDER", "fake", "app_runtime"),
        ("ALIOTH_APP_DEVICE_LEASE_BACKEND", "in_process", "app_runtime"),
        ("ALIOTH_OFFICIAL_APP_CONTRACT_TEST_ENABLED", "false", "official_app_device"),
        ("ALIOTH_XINGYU_OFFICIAL_DRY_RUN", "true", "official_app_device"),
    ],
)
def test_fake_or_disabled_backends_are_blocked(
    prerequisites,
    name: str,
    value: str,
    code: str,
) -> None:
    environment = _ready_environment()
    environment[name] = value

    report = prerequisites.build_acceptance_prerequisite_report(environment)
    check = next(item for item in report.checks if item.code == code)

    assert check.ready is False
    assert name in check.missing_or_invalid_variables


def test_placeholder_values_do_not_satisfy_gate(prerequisites) -> None:
    environment = _ready_environment()
    environment["ALIOTH_MINIO_SECRET_KEY"] = "replace-with-secret-key"
    environment["ALIOTH_OFFICIAL_APP_AUTH_TOKEN"] = "<STAR_PROVIDER_TOKEN>"

    report = prerequisites.build_acceptance_prerequisite_report(environment)

    minio = next(item for item in report.checks if item.code == "minio_evidence_store")
    official = next(item for item in report.checks if item.code == "official_app_device")
    assert "ALIOTH_MINIO_SECRET_KEY" in minio.missing_or_invalid_variables
    assert "ALIOTH_OFFICIAL_APP_AUTH_TOKEN" in official.missing_or_invalid_variables


@pytest.mark.parametrize(
    "slots",
    ["", "cloud_phone", "cloud_phone:", ":profile", "cloud_phone:p1,cloud_phone:p1"],
)
def test_device_pool_requires_unique_backend_profile_resources(
    prerequisites,
    slots: str,
) -> None:
    environment = _ready_environment()
    environment["ALIOTH_DEVICE_POOL_SLOTS"] = slots

    report = prerequisites.build_acceptance_prerequisite_report(environment)
    runtime = next(item for item in report.checks if item.code == "app_runtime")

    assert runtime.ready is False
    assert "ALIOTH_DEVICE_POOL_SLOTS" in runtime.missing_or_invalid_variables


def test_official_contract_rejects_malformed_apk_sha256(prerequisites) -> None:
    environment = _ready_environment()
    environment["ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256"] = "sha256:not-a-digest"

    report = prerequisites.build_acceptance_prerequisite_report(environment)
    official = next(item for item in report.checks if item.code == "official_app_device")

    assert official.ready is False
    assert "ALIOTH_OFFICIAL_APP_CONTRACT_APK_SHA256" in (
        official.missing_or_invalid_variables
    )


@pytest.mark.parametrize(
    ("provider", "missing_names"),
    [
        ("custom", ("ALIOTH_LLM_BASE_URL",)),
        ("xai", ("ALIOTH_LLM_BASE_URL",)),
        (
            "azure_openai",
            ("ALIOTH_LLM_AZURE_ENDPOINT", "ALIOTH_LLM_AZURE_API_VERSION"),
        ),
    ],
)
def test_visual_gate_requires_provider_specific_configuration(
    prerequisites,
    provider: str,
    missing_names: tuple[str, ...],
) -> None:
    environment = _ready_environment()
    environment["ALIOTH_LLM_PROVIDER"] = provider

    report = prerequisites.build_acceptance_prerequisite_report(environment)
    visual = next(item for item in report.checks if item.code == "visual_provider")

    assert visual.ready is False
    assert set(missing_names).issubset(visual.missing_or_invalid_variables)


def test_cli_exit_code_and_json_status_follow_gate(
    prerequisites,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert prerequisites.main(["--json"], environment={}) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked"

    assert prerequisites.main(["--json"], environment=_ready_environment()) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["status"] == "ready_to_run"
