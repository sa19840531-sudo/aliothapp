"""广告 DOM 完整性与完整来源身份的失败关闭回归。"""

from types import SimpleNamespace

import pytest

from services.api.app.infrastructure.browser import capture_runtime, page_capture


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, "", "   "])
async def test_empty_or_unreadable_dom_cannot_be_observed(monkeypatch, content):
    class Page:
        url = "https://landing.example/"

        async def title(self):
            return "landing"

        async def content(self):
            if content is None:
                raise RuntimeError("private diagnostic")
            return content

        async def screenshot(self, **kwargs):
            return b"png"

    async def snapshot(*args, **kwargs):
        return {"elements": []}

    monkeypatch.setattr(page_capture, "_collect_ui_snapshot", snapshot)
    landing = await page_capture._capture_ad_landing_observation(page=Page(), index=1)
    trace = [{"navigation": {"new_page_count": 1}}]
    page_capture._attach_ad_navigation_evidence(
        action_trace=trace,
        before_url="https://source.example/",
        after_url="https://source.example/",
        source_target=None,
        landing=landing,
        frame_count=2,
    )
    assert trace[0]["ad_navigation_evidence"]["status"] == "evidence_insufficient"
    assert landing["dom_status"] in {"empty", "unreadable"}
    assert "private diagnostic" not in str(landing)


@pytest.mark.parametrize(
    "final_url,changed",
    [
        ("https://source.example/path?q=changed#route", False),
        ("https://source.example/path?q=original#other", False),
        ("https://source.example/path?q=original#route", True),
        ("https://source.example/other?q=original#route", False),
    ],
)
def test_source_identity_drift_blocks_reuse(final_url, changed):
    result = SimpleNamespace(
        final_url=final_url,
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "new_page_count": 1,
                    "closed_after_observation_count": 1,
                    "current_url_changed": changed,
                },
                "ad_navigation_evidence": {
                    "status": "observed",
                    "landing": {"url": "https://landing.example/"},
                },
            }
        ],
    )
    assert (
        capture_runtime._direct_ad_capture_outcome(
            result=result,
            source_url="https://source.example/path?q=original#route",
        )[1]
        is False
    )


def test_opened_ad_page_does_not_require_business_dom_sufficiency():
    result = SimpleNamespace(
        final_url="https://source.example/",
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "new_page_count": 1,
                    "closed_after_observation_count": 1,
                    "current_url_changed": False,
                },
                "ad_navigation_evidence": {
                    "status": "observed",
                    "landing": {
                        "url": "https://landing.example/",
                        "dom_status": "empty",
                    },
                },
            }
        ],
    )
    proven, restored, _, reachable, reason = capture_runtime._direct_ad_capture_outcome(
        result=result,
        source_url="https://source.example/",
    )
    assert proven is True
    assert restored is True
    assert reachable is True
    assert reason is None


def test_legacy_ad_evidence_keeps_insufficient_result_without_ad_page_upgrade():
    result = SimpleNamespace(
        final_url="https://source.example/",
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "new_page_count": 1,
                    "closed_after_observation_count": 1,
                    "current_url_changed": False,
                },
                "ad_navigation_evidence": {
                    "status": "evidence_insufficient",
                    "landing": {"url": "https://landing.example/", "dom_status": "empty"},
                },
            }
        ],
    )

    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    body = policy_payload()
    body["direct_ad_page_enabled"] = False
    body["historical_ad_capture"] = result.__dict__
    audit = LegacyWebRunAuditAdapter.load(body)
    assert audit.execution_allowed is False
    assert audit.direct_ad_page_enabled is False
    assert audit.frozen_fields["historical_ad_capture"]["final_url"] == result.final_url
    from services.api.app.domain.exploration.web_run_policy_snapshot import mutable_json
    assert mutable_json(audit.frozen_fields["historical_ad_capture"]) == result.__dict__



def test_missing_isolated_page_open_proof_cannot_sign_ad_page():
    result = SimpleNamespace(
        final_url="https://source.example/",
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "new_page_count": 0,
                    "closed_after_observation_count": 0,
                    "current_url_changed": False,
                },
                "ad_navigation_evidence": {
                    "status": "observed",
                    "landing": {"url": "https://landing.example/"},
                },
            }
        ],
    )

    proven, restored, landing_url, _, _ = capture_runtime._direct_ad_capture_outcome(
        result=result,
        source_url="https://source.example/",
    )

    assert proven is False
    assert restored is True
    assert landing_url == "https://landing.example/"


def test_source_page_drift_cannot_sign_ad_page_even_when_new_page_was_closed():
    result = SimpleNamespace(
        final_url="https://source.example/redirected",
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "new_page_count": 1,
                    "closed_after_observation_count": 1,
                    "current_url_changed": True,
                },
                "ad_navigation_evidence": {
                    "status": "observed",
                    "landing": {"url": "https://landing.example/"},
                },
            }
        ],
    )

    proven, restored, landing_url, _, _ = capture_runtime._direct_ad_capture_outcome(
        result=result,
        source_url="https://source.example/",
    )

    assert proven is False
    assert restored is False
    assert landing_url == "https://landing.example/"


def test_legacy_ad_audit_preserves_incomplete_roundtrip_without_replay():
    result = SimpleNamespace(
        final_url="https://source.example/",
        action_trace=[
            {
                "action": "direct_ad_outbound",
                "navigation": {
                    "new_page_count": 0,
                    "closed_after_observation_count": 0,
                    "current_url_changed": False,
                },
                "ad_navigation_evidence": {
                    "status": "observed",
                    "landing": {"url": "https://landing.example/"},
                },
            }
        ],
    )

    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    body = policy_payload()
    body["direct_ad_page_enabled"] = False
    body["historical_ad_capture"] = result.__dict__
    audit = LegacyWebRunAuditAdapter.load(body)
    assert audit.execution_allowed is False
    assert audit.direct_ad_page_enabled is False
    assert audit.frozen_fields["historical_ad_capture"]["final_url"] == result.final_url
    from services.api.app.domain.exploration.web_run_policy_snapshot import mutable_json
    assert mutable_json(audit.frozen_fields["historical_ad_capture"]) == result.__dict__



@pytest.mark.parametrize(
    "action_trace",
    (
        [],
        [{"action": "direct_ad_outbound", "navigation": None}],
        [{"action": "direct_ad_outbound", "ad_navigation_evidence": None}],
    ),
)
def test_legacy_ad_audit_preserves_missing_or_corrupt_action_proof_without_replay(action_trace):
    result = SimpleNamespace(
        final_url="https://source.example/",
        action_trace=action_trace,
    )

    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    body = policy_payload()
    body["direct_ad_page_enabled"] = False
    body["historical_ad_capture"] = result.__dict__
    audit = LegacyWebRunAuditAdapter.load(body)
    assert audit.execution_allowed is False
    assert audit.direct_ad_page_enabled is False
    assert audit.frozen_fields["historical_ad_capture"]["final_url"] == result.final_url
    from services.api.app.domain.exploration.web_run_policy_snapshot import mutable_json
    assert mutable_json(audit.frozen_fields["historical_ad_capture"]) == result.__dict__



@pytest.mark.asyncio
async def test_static_script_shell_is_saved_without_running_javascript(monkeypatch):
    class Page:
        url = "https://landing.example/"

        async def content(self):
            return "<html><body><script>render()</script></body></html>"

        async def evaluate(self, script):
            raise AssertionError("广告证据不能运行脚本")

    async def snapshot(*args, **kwargs):
        raise AssertionError("广告不进入普通 DOM 分类")

    monkeypatch.setattr(page_capture, "_collect_ui_snapshot", snapshot)
    landing = await page_capture._capture_ad_landing_observation(
        page=Page(), index=1, static_render=True
    )
    assert landing["dom_status"] == "readable"
