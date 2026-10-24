"""实况入口凭据保护、持久声明与逐站失败审计回归。"""

import json
from urllib.error import HTTPError, URLError

import pytest

from scripts import web_v2_six_site_shadow as runner


def test_live_hydration_keeps_runtime_site_identity_for_previously_catalogued_host():
    from services.api.app.domain.page.web_site_menu_registry import RuntimeWebSiteMenuRegistry

    target = "https://heiliao.com/"
    context = RuntimeWebSiteMenuRegistry().resolve_site_context(target)
    result = runner._hydrate_preview_exploration_summary(
        target_url=target,
        report_exploration={},
        evidence_bundle={"site_id": context.site.site_id, "entries": [], "summary": {}},
    )
    assert result["site_id"] == context.site.site_id


def test_report_renders_ad_evidence_for_executed_runtime_menu():
    """未知站点菜单的广告证据也必须进入人可读报告。"""
    site = runner.LiveSiteResult(
        target_url="https://both.pnsmxatxp.cc/",
        task_id="task",
        run_id="run",
        task_status="review",
        report_status="ready",
        failure_reason=None,
        report_stage="preview",
        is_final=False,
        exploration_summary={
            "menu_ledger": [],
            "runtime_discovered_ledger": [
                {
                    "execution": {
                        "menu_path": ["运行时分类"],
                        "ad_landing_evidence": {
                            "status": "no_candidate",
                            "source_url": "https://both.pnsmxatxp.cc/runtime",
                            "reason_code": "WEB_AD_CANDIDATE_NOT_FOUND",
                        },
                    },
                }
            ],
        },
    )
    report = runner._aggregate_live_results((site,), expected_targets=(site.target_url,))
    for rendered in (runner.render_live_html(report), runner.render_live_markdown(report)):
        assert "运行时分类" in rendered
        assert "https://both.pnsmxatxp.cc/runtime" in rendered


def _arguments(tmp_path):
    return [
        "--live",
        "--username",
        "admin",
        "--password-env",
        "LIVE_TEST_PASSWORD",
        "--adult-statement-env",
        "LIVE_TEST_ADULT",
        "--output-dir",
        str(tmp_path),
        "--target-url",
        "https://heiliao.com/",
        "--target-url",
        "https://hl365.com/",
        "--target-url",
        "https://both.pnsmxatxp.cc/",
    ]


@pytest.mark.parametrize("http_status", [None, 500, 403])
def test_cli_login_uses_environment_and_audits_all_failed_sites(
    monkeypatch, tmp_path, capsys, http_status
):
    """缺少失败隔离会丢后续目标，记录原始异常则会泄漏凭据。"""
    monkeypatch.delenv("ALIOTH_WEB_V2_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LIVE_TEST_PASSWORD", "private-password-value")
    monkeypatch.setenv("LIVE_TEST_ADULT", "adult-v1")
    requests = []

    def requester(method, endpoint, payload, headers):
        if endpoint.endswith("/auth/login"):
            assert method == "POST"
            assert payload == {"username": "admin", "password": "private-password-value"}
            assert headers == {}
            return {"token": "private-token-value"}
        assert headers["Authorization"] == "Bearer private-token-value"
        requests.append(payload["target"]["url"])
        if http_status:
            raise HTTPError(
                endpoint, http_status, "private-password-value private-token-value", {}, None
            )
        raise URLError("private-password-value private-token-value")

    monkeypatch.setattr(runner, "_http_json_request", requester)
    assert runner.main(_arguments(tmp_path)) == 1
    assert requests == ["https://heiliao.com/", "https://hl365.com/", "https://both.pnsmxatxp.cc/"]
    report = json.loads((tmp_path / "final-exploration-report.json").read_text(encoding="utf-8"))
    assert len(report["sites"]) == 3
    expected_code = (
        "WEB_LIVE_SERVER_ERROR"
        if http_status == 500
        else "WEB_LIVE_HTTP_ERROR"
        if http_status
        else "WEB_LIVE_NETWORK_ERROR"
    )
    assert all(site["failure_reason"] == expected_code for site in report["sites"])
    assert all(site["task_status"] == "submission_failed" for site in report["sites"])
    text = capsys.readouterr().out + "".join(
        path.read_text(encoding="utf-8") for path in tmp_path.rglob("*") if path.is_file()
    )
    assert "private-password-value" not in text
    assert "private-token-value" not in text
    assert (tmp_path / "final-exploration-report.html").exists()
    assert (tmp_path / "final-exploration-report.md").exists()


@pytest.mark.parametrize("case", ["both_modes", "missing_password", "missing_adult"])
def test_cli_rejects_incomplete_or_ambiguous_credentials_before_requests(
    case,
    monkeypatch,
    tmp_path,
    capsys,
):
    monkeypatch.delenv("ALIOTH_WEB_V2_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LIVE_TEST_PASSWORD", "private-password-value")
    monkeypatch.setenv("LIVE_TEST_ADULT", "adult-v1")
    if case == "both_modes":
        monkeypatch.setenv("ALIOTH_WEB_V2_ACCESS_TOKEN", "private-token-value")
    elif case == "missing_password":
        monkeypatch.delenv("LIVE_TEST_PASSWORD")
    else:
        monkeypatch.delenv("LIVE_TEST_ADULT")
    requests = []
    monkeypatch.setattr(runner, "_http_json_request", lambda *args: requests.append(args))
    with pytest.raises(SystemExit) as failure:
        runner.main(_arguments(tmp_path))
    assert failure.value.code == 2
    assert not requests
    assert "private-password-value" not in capsys.readouterr().err


def test_cli_uses_persisted_account_independent_adult_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("ALIOTH_WEB_V2_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("LIVE_TEST_PASSWORD", "private-password-value")
    monkeypatch.delenv("ALIOTH_WEB_ADULT_STATEMENT", raising=False)
    from services.api.app.core import config

    configured = config.WebAdultSelfAttestationSettings(
        enabled=True,
        statement_version="adult-v1",
        declared_by="persistent-config",
        declared_at="2026-09-11T00:00:00+08:00",
    )
    monkeypatch.setattr(config, "_build_web_adult_self_attestation_settings", lambda: configured)
    args = _arguments(tmp_path)
    args[args.index("LIVE_TEST_ADULT")] = "ALIOTH_WEB_ADULT_STATEMENT"

    def requester(method, endpoint, payload, headers):
        if endpoint.endswith("/auth/login"):
            return {"token": "private-token-value"}
        raise URLError("site unavailable")

    monkeypatch.setattr(runner, "_http_json_request", requester)
    assert runner.main(args) == 1
    audit = json.loads((tmp_path / "execution-audit.json").read_text(encoding="utf-8"))
    assert audit["authentication"]["fresh_login"] is True
    assert audit["adult_statement"]["source"] == "persistent_web_configuration"
    assert audit["adult_statement"]["scope"] == "account_session_site_independent"
