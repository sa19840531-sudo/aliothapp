"""测试本地环境文件加载顺序和补采开关的 fail-closed 合同。"""

import logging
from datetime import UTC, datetime

import pytest

from services.api.app.application.supplemental_evidence_policy import (
    SUPPLEMENTAL_EVIDENCE_DISABLED,
    SUPPLEMENTAL_EVIDENCE_NOT_READY,
    SupplementalEvidenceDisabledError,
    SupplementalEvidenceNotReadyError,
    SupplementalEvidenceOperation,
    SupplementalEvidencePolicy,
    build_current_supplemental_evidence_policy,
    build_h5_supplemental_evidence_policy,
    has_supplemental_evidence_lineage,
)
from services.api.app.core import config as config_module


def test_web_menu_collection_limit_defaults_to_ten(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", raising=False)

    assert config_module.load_web_menu_collection_limit() == 10


@pytest.mark.parametrize("menu_limit", [5, 25])
def test_web_menu_collection_limit_is_configurable(monkeypatch, menu_limit) -> None:
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", str(menu_limit))

    assert config_module.load_web_menu_collection_limit() == menu_limit


@pytest.mark.parametrize("value", ["0", "-1", "1001", "invalid"])
def test_web_menu_collection_limit_rejects_invalid_values(monkeypatch, value: str) -> None:
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", value)

    with pytest.raises(ValueError, match="ALIOTH_WEB_MENU_COLLECTION_LIMIT"):
        config_module.load_web_menu_collection_limit()


def test_web_adult_self_attestation_is_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_VERSION", "adult-v1")
    monkeypatch.setenv(
        "ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_BY",
        "environment-config",
    )
    monkeypatch.setenv(
        "ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_AT",
        "2026-09-11T00:00:00+00:00",
    )

    configured = config_module._build_web_adult_self_attestation_settings()

    assert configured.enabled is True
    assert configured.statement_version == "adult-v1"
    assert configured.declared_by == "environment-config"
    assert configured.declared_at == datetime(2026, 9, 11, tzinfo=UTC)


def test_web_adult_self_attestation_disabled_does_not_require_identity(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_ENABLED", "false")
    monkeypatch.delenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_BY", raising=False)
    monkeypatch.delenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_AT", raising=False)

    configured = config_module._build_web_adult_self_attestation_settings()

    assert configured.enabled is False
    assert configured.declared_by is None
    assert configured.declared_at is None


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("ALIOTH_WEB_ADULT_SELF_ATTESTATION_VERSION", "adult-v2"),
        ("ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_BY", ""),
        ("ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_AT", "not-a-date"),
    ),
)
def test_enabled_web_adult_self_attestation_rejects_invalid_environment(
    monkeypatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_WEB_ADULT_SELF_ATTESTATION_VERSION", "adult-v1")
    monkeypatch.setenv(
        "ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_BY",
        "environment-config",
    )
    monkeypatch.setenv(
        "ALIOTH_WEB_ADULT_SELF_ATTESTATION_DECLARED_AT",
        "2026-09-11T00:00:00+00:00",
    )
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match="ALIOTH_WEB_ADULT_SELF_ATTESTATION"):
        config_module._build_web_adult_self_attestation_settings()


def test_env_local_overrides_env_file_for_llm_settings(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "ALIOTH_LLM_PROVIDER=openai",
                "ALIOTH_LLM_API_STYLE=responses",
                "ALIOTH_LLM_MODEL=gpt-4o-mini",
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env.local").write_text(
        "\n".join(
            [
                "ALIOTH_LLM_PROVIDER=custom",
                "ALIOTH_LLM_API_STYLE=chat_completion",
                "ALIOTH_LLM_MODEL=deepseek-v4-flash",
                "ALIOTH_LLM_BASE_URL=https://api.deepseek.com",
                "ALIOTH_LLM_REASONING_EFFORT=low",
            ]
        ),
        encoding="utf-8",
    )
    for key in (
        "ALIOTH_LLM_PROVIDER",
        "ALIOTH_LLM_API_STYLE",
        "ALIOTH_LLM_MODEL",
        "ALIOTH_LLM_BASE_URL",
        "ALIOTH_LLM_REASONING_EFFORT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)

    config_module._load_dotenv_files()
    settings = config_module._build_llm_settings()

    assert settings.provider == "custom"
    assert settings.api_style == "chat_completion"
    assert settings.model == "deepseek-v4-flash"
    assert settings.base_url == "https://api.deepseek.com"
    assert settings.reasoning_effort == "low"


def test_process_environment_overrides_env_local(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env.local").write_text(
        "ALIOTH_LLM_MODEL=deepseek-v4-flash\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALIOTH_LLM_MODEL", "process-model")
    monkeypatch.chdir(tmp_path)

    config_module._load_dotenv_files()
    settings = config_module._build_llm_settings()

    assert settings.model == "process-model"


def test_llm_cost_settings_are_read_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_LLM_COST_CURRENCY", "CNY")
    monkeypatch.setenv("ALIOTH_LLM_INPUT_PRICE_PER_MILLION_TOKENS", "1.25")
    monkeypatch.setenv("ALIOTH_LLM_OUTPUT_PRICE_PER_MILLION_TOKENS", "5.5")
    monkeypatch.setenv("ALIOTH_LLM_CACHE_HIT_PRICE_PER_MILLION_TOKENS", "0.25")
    monkeypatch.setenv("ALIOTH_LLM_LONG_CONTEXT_THRESHOLD_TOKENS", "200000")
    monkeypatch.setenv("ALIOTH_LLM_LONG_CONTEXT_INPUT_PRICE_PER_MILLION_TOKENS", "2")
    monkeypatch.setenv("ALIOTH_LLM_LONG_CONTEXT_OUTPUT_PRICE_PER_MILLION_TOKENS", "6")
    monkeypatch.setenv("ALIOTH_LLM_LONG_CONTEXT_CACHE_HIT_PRICE_PER_MILLION_TOKENS", "0.5")

    settings = config_module._build_llm_settings()

    assert settings.cost_currency == "CNY"
    assert settings.input_price_per_million_tokens == 1.25
    assert settings.output_price_per_million_tokens == 5.5
    assert settings.cache_hit_price_per_million_tokens == 0.25
    assert settings.long_context_threshold_tokens == 200000
    assert settings.long_context_input_price_per_million_tokens == 2
    assert settings.long_context_output_price_per_million_tokens == 6
    assert settings.long_context_cache_hit_price_per_million_tokens == 0.5


def test_agent_runtime_defaults_match_production_budget(monkeypatch) -> None:
    for key in (
        "ALIOTH_AGENT_MAX_STEPS",
        "ALIOTH_AGENT_MAX_ITERATIONS",
        "ALIOTH_AGENT_MAX_DURATION_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = config_module._build_agent_runtime_settings()

    assert settings.max_steps == 200
    assert settings.max_iterations == 200
    assert settings.max_duration_seconds == 3600


def test_worker_defaults_to_three_for_browser_heavy_batch(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_WORKER_CONCURRENCY", raising=False)

    settings = config_module._build_worker_settings()

    assert settings.concurrency == 3


def test_quality_verdict_mode_supports_balanced_and_rejects_unknown(
    monkeypatch,
) -> None:
    monkeypatch.setenv("ALIOTH_QC_VERDICT_MODE", "balanced")

    settings = config_module._build_quality_optimization_settings()

    assert settings.verdict_mode == "balanced"

    monkeypatch.setenv("ALIOTH_QC_VERDICT_MODE", "relaxed")

    settings = config_module._build_quality_optimization_settings()

    assert settings.verdict_mode == "strict"


def test_quality_optimization_reads_page_capture_timeout(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_QC_PAGE_CAPTURE_TIMEOUT_SECONDS", "180")

    settings = config_module._build_quality_optimization_settings()

    assert settings.page_capture_timeout_seconds == 180


def test_quality_optimization_defaults_page_capture_timeout_to_300(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_QC_PAGE_CAPTURE_TIMEOUT_SECONDS", raising=False)

    settings = config_module._build_quality_optimization_settings()

    assert settings.page_capture_timeout_seconds == 300


def test_quality_optimization_reads_web_interaction_timeout(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_QC_WEB_INTERACTION_TIMEOUT_SECONDS", "45")

    settings = config_module._build_quality_optimization_settings()

    assert settings.web_interaction_timeout_seconds == 45


def test_quality_optimization_defaults_web_interaction_timeout_to_180(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_QC_WEB_INTERACTION_TIMEOUT_SECONDS", raising=False)

    settings = config_module._build_quality_optimization_settings()

    assert settings.web_interaction_timeout_seconds == 180


def test_quality_optimization_reads_page_capture_concurrency(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_QC_PAGE_CAPTURE_CONCURRENCY", "2")

    settings = config_module._build_quality_optimization_settings()

    assert settings.page_capture_concurrency == 2


def test_quality_optimization_defaults_page_capture_concurrency_to_one(
    monkeypatch,
) -> None:
    monkeypatch.delenv("ALIOTH_QC_PAGE_CAPTURE_CONCURRENCY", raising=False)

    settings = config_module._build_quality_optimization_settings()

    assert settings.page_capture_concurrency == 1


def test_semantic_judge_uses_independent_latency_settings(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_QC_SEMANTIC_REQUEST_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("ALIOTH_QC_SEMANTIC_MAX_RETRIES", "1")
    monkeypatch.setenv("ALIOTH_QC_SEMANTIC_CIRCUIT_SECONDS", "180")

    settings = config_module._build_quality_optimization_settings()

    assert settings.semantic_request_timeout_seconds == 45
    assert settings.semantic_max_retries == 1
    assert settings.semantic_circuit_seconds == 180


def test_inspection_work_items_have_no_mode_switches() -> None:
    settings = config_module._build_inspection_work_item_settings()

    assert not hasattr(settings, "mode")
    assert not hasattr(settings, "wakeup_mode")


def test_inspection_work_items_read_lease_and_retry_settings(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_LEASE_SECONDS", "240")
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_RETRY_BASE_SECONDS", "3.5")

    settings = config_module._build_inspection_work_item_settings()

    assert settings.lease_seconds == 240
    assert settings.max_attempts == 5
    assert settings.retry_base_seconds == 3.5


def test_inspection_judge_dispatch_has_conservative_defaults(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_QC_INSPECTION_JUDGE_CONCURRENCY", raising=False)
    monkeypatch.delenv("ALIOTH_QC_INSPECTION_DISPATCH_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("ALIOTH_QC_INSPECTION_PUBLICATION_BATCH_LIMIT", raising=False)
    monkeypatch.delenv("ALIOTH_QC_INSPECTION_PUBLICATION_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("ALIOTH_QC_INSPECTION_DISPATCH_QUEUE_CAPACITY", raising=False)

    settings = config_module._build_inspection_work_item_settings()

    assert settings.judge_concurrency == 2
    assert settings.dispatch_batch_limit == 1
    assert settings.publication_batch_limit == 1
    assert settings.publication_timeout_seconds == 30.0
    assert settings.dispatch_queue_capacity == 128


def test_inspection_judge_dispatch_reads_env_settings(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_JUDGE_CONCURRENCY", "3")
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_DISPATCH_BATCH_LIMIT", "5")
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_PUBLICATION_BATCH_LIMIT", "2")
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_PUBLICATION_TIMEOUT_SECONDS", "4.5")
    monkeypatch.setenv("ALIOTH_QC_INSPECTION_DISPATCH_QUEUE_CAPACITY", "64")

    settings = config_module._build_inspection_work_item_settings()

    assert settings.judge_concurrency == 3
    assert settings.dispatch_batch_limit == 5
    assert settings.publication_batch_limit == 2
    assert settings.publication_timeout_seconds == 4.5
    assert settings.dispatch_queue_capacity == 64


@pytest.mark.parametrize("raw", ["off", "shadow", "enforced"])
def test_workflow_reads_h5_inspection_notification_mode(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("ALIOTH_H5_INSPECTION_NOTIFICATION_MODE", raw)

    settings = config_module._build_workflow_settings()

    assert settings.h5_inspection_notification_mode == raw


def test_workflow_rejects_unknown_h5_inspection_notification_mode(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_INSPECTION_NOTIFICATION_MODE", "on")

    settings = config_module._build_workflow_settings()

    assert settings.h5_inspection_notification_mode == "off"


def test_h5_visual_coordinate_mode_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_H5_VISUAL_COORDINATE_MODE", raising=False)
    settings = config_module._build_workflow_settings()

    assert settings.h5_visual_coordinate_mode == "off"
    assert settings.h5_visual_coordinate_runtime_mode == "off"


def test_h5_visual_coordinate_mode_rejects_unknown_value(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_VISUAL_COORDINATE_MODE", "enabled")

    with pytest.raises(ValueError, match="ALIOTH_H5_VISUAL_COORDINATE_MODE"):
        config_module._build_workflow_settings()


def test_h5_visual_coordinate_runtime_mode_obeys_all_prerequisites(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_VISUAL_COORDINATE_MODE", "ENFORCED")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", "false")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_TARGETS_ENABLED", "true")

    assert (
        config_module._build_workflow_settings().h5_visual_coordinate_runtime_mode
        == "enforced"
    )

    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", "true")
    assert (
        config_module._build_workflow_settings().h5_visual_coordinate_runtime_mode
        == "shadow"
    )


def test_h5_logical_surface_flags_default_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_H5_LOGICAL_SURFACE_ENABLED", raising=False)
    monkeypatch.delenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", raising=False)
    monkeypatch.delenv("ALIOTH_H5_LOGICAL_TARGETS_ENABLED", raising=False)

    settings = config_module._build_workflow_settings()

    assert settings.h5_logical_surface_enabled is False
    assert settings.h5_logical_surface_shadow is False
    assert settings.h5_logical_targets_enabled is False
    assert settings.h5_logical_surface_effective is False
    assert settings.h5_logical_targets_effective is False


def test_h5_browser_proxy_defaults_to_disabled(monkeypatch) -> None:
    """未配置代理时必须保持普通 H5 的 Chromium 直连行为。"""
    monkeypatch.delenv("ALIOTH_H5_BROWSER_PROXY_SERVER", raising=False)
    monkeypatch.delenv("ALIOTH_H5_BROWSER_PROXY_BYPASS", raising=False)

    settings = config_module._build_workflow_settings()

    assert settings.h5_browser_proxy_server is None
    assert settings.h5_browser_proxy_bypass is None


def test_h5_browser_proxy_reads_server_and_normalizes_bypass(monkeypatch) -> None:
    monkeypatch.setenv(
        "ALIOTH_H5_BROWSER_PROXY_SERVER",
        " http://host.docker.internal:7897 ",
    )
    monkeypatch.setenv(
        "ALIOTH_H5_BROWSER_PROXY_BYPASS",
        " localhost, 127.0.0.1 , alioth-api ",
    )

    settings = config_module._build_workflow_settings()

    assert settings.h5_browser_proxy_server == "http://host.docker.internal:7897"
    assert settings.h5_browser_proxy_bypass == "localhost,127.0.0.1,alioth-api"


@pytest.mark.parametrize(
    "server",
    [
        "ftp://proxy.example:21",
        "http://user:password@proxy.example:8080",
        "http://proxy.example:8080/path",
    ],
)
def test_h5_browser_proxy_rejects_unsafe_server(monkeypatch, server: str) -> None:
    monkeypatch.setenv("ALIOTH_H5_BROWSER_PROXY_SERVER", server)

    with pytest.raises(ValueError, match="ALIOTH_H5_BROWSER_PROXY_SERVER"):
        config_module._build_workflow_settings()


def test_web_browser_proxy_defaults_to_disabled_without_changing_h5(
    monkeypatch,
) -> None:
    """Web 代理必须独立默认关闭，不能继承 H5 代理。"""
    monkeypatch.setenv(
        "ALIOTH_H5_BROWSER_PROXY_SERVER",
        "http://h5-proxy.internal:8080",
    )
    monkeypatch.delenv("ALIOTH_WEB_BROWSER_PROXY_SERVER", raising=False)
    monkeypatch.delenv("ALIOTH_WEB_BROWSER_PROXY_BYPASS", raising=False)

    settings = config_module._build_workflow_settings()

    assert settings.h5_browser_proxy_server == "http://h5-proxy.internal:8080"
    assert settings.web_browser_proxy_server is None
    assert settings.web_browser_proxy_bypass is None


def test_web_browser_proxy_reads_server_and_normalizes_bypass(monkeypatch) -> None:
    monkeypatch.setenv(
        "ALIOTH_WEB_BROWSER_PROXY_SERVER",
        " http://host.docker.internal:7897 ",
    )
    monkeypatch.setenv(
        "ALIOTH_WEB_BROWSER_PROXY_BYPASS",
        " localhost, 127.0.0.1 , alioth-api, localhost ",
    )

    settings = config_module._build_workflow_settings()

    assert settings.web_browser_proxy_server == "http://host.docker.internal:7897"
    assert settings.web_browser_proxy_bypass == "localhost,127.0.0.1,alioth-api"


@pytest.mark.parametrize(
    "server",
    [
        "ftp://proxy.example:21",
        "http://user:password@proxy.example:8080",
        "http://proxy.example:8080/path",
        "http://proxy\\path:8080",
        "http://proxy example:8080",
        "http://proxy:",
        "http://proxy\nexample:8080",
        "http://999.999.999.999:8080",
        "http://127.0.0.999:8080",
        "http://1.2.3:8080",
        "http://2130706433:8080",
        "http://0127.0.0.1:8080",
        "http://0x7f000001:8080",
        "http://0x7f.1:8080",
        "http://0x7f.0x0.0x0.0x1:8080",
        "http://0177.0x0.0x0.0x1:8080",
        "http://127.0.0.0x1:8080",
        "http://0x7f.0x1:8080",
        "http://[example.com]:8080",
        "http://[127.0.0.1]:8080",
        "http://[fe80::1%25eth0]:8080",
        "http://[::1]evil:8080",
        "http://[::1].example:8080",
        "http://[::1]]:8080",
    ],
)
def test_web_browser_proxy_rejects_unsafe_server(monkeypatch, server: str) -> None:
    monkeypatch.setenv("ALIOTH_WEB_BROWSER_PROXY_SERVER", server)

    with pytest.raises(ValueError, match="ALIOTH_WEB_BROWSER_PROXY_SERVER"):
        config_module._build_workflow_settings()


def test_web_browser_proxy_accepts_bracketed_ipv6(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_WEB_BROWSER_PROXY_SERVER", "http://[::1]:8080")

    settings = config_module._build_workflow_settings()

    assert settings.web_browser_proxy_server == "http://[::1]:8080"


def test_h5_logical_surface_shadow_disables_decision_and_targets(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", "true")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_TARGETS_ENABLED", "true")

    settings = config_module._build_workflow_settings()

    assert settings.h5_logical_surface_enabled is True
    assert settings.h5_logical_surface_shadow is True
    assert settings.h5_logical_targets_enabled is True
    assert settings.h5_logical_surface_effective is False
    assert settings.h5_logical_targets_effective is False


def test_h5_logical_targets_require_surface_decision_mode(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_ENABLED", "true")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", "false")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_TARGETS_ENABLED", "true")

    settings = config_module._build_workflow_settings()

    assert settings.h5_logical_surface_effective is True
    assert settings.h5_logical_targets_effective is True


def test_invalid_h5_logical_surface_flags_fail_closed(monkeypatch) -> None:
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_ENABLED", "maybe")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_SURFACE_SHADOW", "sometimes")
    monkeypatch.setenv("ALIOTH_H5_LOGICAL_TARGETS_ENABLED", "perhaps")

    settings = config_module._build_workflow_settings()

    assert settings.h5_logical_surface_enabled is False
    assert settings.h5_logical_surface_shadow is False
    assert settings.h5_logical_targets_enabled is False


def test_supplemental_evidence_defaults_to_disabled(monkeypatch) -> None:
    monkeypatch.delenv("ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED", raising=False)

    settings = config_module._build_supplemental_evidence_settings()

    assert isinstance(settings, config_module.SupplementalEvidenceSettings)
    assert settings.enabled is False
    assert config_module.Settings().supplemental_evidence.enabled is False


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_supplemental_evidence_can_be_enabled_explicitly(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED", raw)

    settings = config_module._build_supplemental_evidence_settings()

    assert settings.enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "FALSE", " no ", "off", ""])
def test_supplemental_evidence_false_value_remains_disabled(monkeypatch, raw: str) -> None:
    monkeypatch.setenv("ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED", raw)

    settings = config_module._build_supplemental_evidence_settings()

    assert settings.enabled is False


def test_supplemental_evidence_invalid_value_warns_and_fails_closed(
    monkeypatch,
    caplog,
) -> None:
    monkeypatch.setenv("ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED", "maybe")

    with caplog.at_level(logging.WARNING, logger=config_module.__name__):
        settings = config_module._build_supplemental_evidence_settings()

    assert settings.enabled is False
    assert any(
        "ALIOTH_SUPPLEMENTAL_EVIDENCE_ENABLED" in record.message and "按关闭处理" in record.message
        for record in caplog.records
    )


def test_supplemental_evidence_policy_is_immutable_and_blocks_when_disabled() -> None:
    policy = SupplementalEvidencePolicy()

    decision = policy.decide(SupplementalEvidenceOperation.PUBLISH_PROBE)

    assert decision.allowed is False
    assert decision.enabled is False
    assert decision.reason_code == SUPPLEMENTAL_EVIDENCE_DISABLED
    assert decision.to_dict() == {
        "operation": "publish_probe",
        "allowed": False,
        "enabled": False,
        "reason_code": SUPPLEMENTAL_EVIDENCE_DISABLED,
        "message": decision.message,
        "retryable": False,
        "policy_version": "supplemental-evidence-policy.v1",
    }
    with pytest.raises(SupplementalEvidenceDisabledError) as error:
        policy.require_enabled(SupplementalEvidenceOperation.PUBLISH_PROBE)
    assert error.value.reason_code == SUPPLEMENTAL_EVIDENCE_DISABLED
    with pytest.raises(AttributeError):
        policy.enabled = True  # type: ignore[misc]


@pytest.mark.parametrize("operation", list(SupplementalEvidenceOperation))
def test_all_supplemental_evidence_operations_fail_closed_by_default(
    operation: SupplementalEvidenceOperation,
) -> None:
    decision = SupplementalEvidencePolicy().decide(operation)

    assert decision.allowed is False
    assert decision.reason_code == SUPPLEMENTAL_EVIDENCE_DISABLED
    assert decision.retryable is False


def test_supplemental_evidence_policy_rejects_enabled_but_unready_runtime() -> None:
    policy = SupplementalEvidencePolicy(enabled=True)

    decision = policy.decide(SupplementalEvidenceOperation.EXECUTE_PROBE)

    assert decision.allowed is False
    assert decision.enabled is True
    assert decision.reason_code == SUPPLEMENTAL_EVIDENCE_NOT_READY
    with pytest.raises(SupplementalEvidenceNotReadyError):
        policy.validate_runtime_ready()


def test_supplemental_evidence_policy_allows_when_enabled_and_ready() -> None:
    decision = SupplementalEvidencePolicy(
        enabled=True,
        runtime_ready=True,
    ).require_enabled(SupplementalEvidenceOperation.CAPTURE_SUPPLEMENT)

    assert decision.allowed is True
    assert decision.enabled is True
    assert decision.reason_code is None


def test_current_runtime_never_becomes_ready_from_config_alone() -> None:
    policy = build_current_supplemental_evidence_policy(enabled=True)

    assert policy.enabled is True
    assert policy.runtime_ready is False
    with pytest.raises(SupplementalEvidenceNotReadyError):
        policy.validate_runtime_ready()


@pytest.mark.parametrize(
    ("enabled", "h5_v2_mode", "expected_enabled", "expected_ready"),
    (
        (True, "on", True, True),
        (False, "on", False, True),
        (True, "shadow", False, False),
        (True, "off", False, False),
    ),
)
def test_h5_runtime_policy_only_opens_formal_h5_v2(
    enabled: bool,
    h5_v2_mode: str,
    expected_enabled: bool,
    expected_ready: bool,
) -> None:
    policy = build_h5_supplemental_evidence_policy(
        enabled=enabled,
        h5_v2_mode=h5_v2_mode,
    )

    assert policy.enabled is expected_enabled
    assert policy.runtime_ready is expected_ready
    policy.validate_runtime_ready()


def test_supplemental_lineage_uses_reserved_server_fields() -> None:
    for key in (
        "evidence_request_id",
        "restoration_attempt_id",
        "equivalence_assessment_id",
        "supplemental_evidence_source",
    ):
        assert has_supplemental_evidence_lineage({key: "lineage-1"}) is True
    assert has_supplemental_evidence_lineage({"candidate_id": "candidate-1"}) is False
    assert has_supplemental_evidence_lineage({"evidence_request_id": None}) is False
    assert has_supplemental_evidence_lineage({"evidence_request_id": "  "}) is False
    assert has_supplemental_evidence_lineage({"delay_ms": 1000}) is False
