"""App41共享内核的正式外链、浏览器组件、内置WebView与歧义边界。"""

import copy

import pytest

from services.api.app.application.green_external_link_rule import judge_external_link
from services.api.app.application.green_fact_bindings import GreenFactView


def facts(*, webview=False, platform="app"):
    source = "org.product"
    action = {
        "action_id": "outbound",
        "operation": "tap",
        "source_app_id": source,
        "before_foreground": {"app_id": source, "component_id": source + ".Main"},
        "target_url": "https://example.invalid/path?q=1#section",
        "clock_id": "device-clock",
        "started_at_ms": 2,
        "finished_at_ms": 8,
        "before_capture_id": "before",
        "after_capture_id": "after",
    }
    row = {
        "at_ms": 10,
        "clock_id": "device-clock",
        "foreground": {"app_id": "org.browser", "component_id": "org.browser.Main"},
        "destination": {"url": action["target_url"], "source": "browser_address_bar"},
        "browser_identity": {
            "app_id": "org.browser",
            "component_id": "org.browser.Main",
            "source": "platform_browser_registry",
        },
        "webview_identity": None,
    }
    if webview:
        row.update(
            foreground={"app_id": source, "component_id": source + ".Main"},
            browser_identity=None,
            webview_identity={
                "owner_app_id": source,
                "host_component_id": source + ".Main",
                "class_name": "android.webkit.WebView",
                "source": "native_view_hierarchy",
            },
        )
        row["destination"]["source"] = "webview_navigation_callback"
    return {
        "target_platform": platform,
        "external_link_action": action,
        "handoff_window": {
            "clock_id": "device-clock",
            "start_ms": 0,
            "end_ms": 20,
            "complete": True,
        },
        "handoff_observations": [row],
    }


def view(
    values,
    *,
    actions=("outbound",),
    captures=("before", "after"),
    issues=(),
    subject="outbound-link",
):
    return GreenFactView(subject, issues, values, captures, actions, ("raw-native",))


def test_registered_browser_component_with_exact_target_passes():
    item = judge_external_link([view(facts())])
    assert item.status == "pass" and not item.missing
    assert item.capture_ids == ("before", "after") and item.action_ids == ("outbound",)


@pytest.mark.parametrize("class_name", ["android.webkit.WebView", "WKWebView"])
def test_source_app_native_webview_with_exact_target_is_failure(class_name):
    values = facts(webview=True)
    values["handoff_observations"][0]["webview_identity"]["class_name"] = class_name
    assert judge_external_link([view(values)]).status == "fail"


@pytest.mark.parametrize("platform", ["web", "h5", "WEB", "H5"])
def test_authoritative_browser_task_is_not_applicable(platform):
    assert judge_external_link([view(facts(platform=platform))]).status == "skipped"


def test_same_or_different_package_without_native_receiver_identity_remains_unknown():
    for app_id in ("org.product", "org.another"):
        values = facts()
        row = values["handoff_observations"][0]
        row.update(
            foreground={"app_id": app_id, "component_id": app_id + ".Main"},
            browser_identity=None,
            webview_identity=None,
        )
        assert judge_external_link([view(values)]).status == "evidence_insufficient"


@pytest.mark.parametrize(
    "fault",
    [
        "unknown_action",
        "unknown_capture",
        "same_capture",
        "clock",
        "partial_window",
        "before_action",
        "weak_url",
        "wrong_component",
        "query_changed",
        "fragment_changed",
        "unrecognized_native_class",
        "weak_component",
        "unknown_platform",
        "credential_url",
        "before_foreground",
    ],
)
def test_weak_identity_url_or_unbound_formal_action_never_pass_or_fail(fault):
    values = facts(webview=fault == "unrecognized_native_class")
    action, row = values["external_link_action"], values["handoff_observations"][0]
    if fault == "unknown_action":
        action["action_id"] = "page-claimed-action"
    elif fault == "unknown_capture":
        action["after_capture_id"] = "unrelated-capture"
    elif fault == "same_capture":
        action["after_capture_id"] = action["before_capture_id"]
    elif fault == "clock":
        row["clock_id"] = "other-clock"
    elif fault == "partial_window":
        values["handoff_window"]["complete"] = False
    elif fault == "before_action":
        row["at_ms"] = 1
    elif fault == "weak_url":
        row["destination"]["source"] = "clipboard"
    elif fault == "wrong_component":
        row["browser_identity"]["component_id"] = "org.browser.Other"
    elif fault == "query_changed":
        row["destination"]["url"] = action["target_url"].replace("q=1", "q=2")
    elif fault == "fragment_changed":
        row["destination"]["url"] = action["target_url"].split("#")[0]
    elif fault == "unrecognized_native_class":
        row["webview_identity"]["class_name"] = "AppWebViewLikeName"
    elif fault == "weak_component":
        row["browser_identity"]["source"] = "screenshot_model"
    elif fault == "unknown_platform":
        values["target_platform"] = None
    elif fault == "before_foreground":
        action["before_foreground"]["app_id"] = "another-app"
    else:
        action["target_url"] = "https://user:password@example.invalid/path"
    assert judge_external_link([view(values)]).status == "evidence_insufficient"


@pytest.mark.parametrize("complete,expected", [(True, "pass"), (False, "evidence_insufficient")])
def test_redirects_need_exact_complete_original_chain(complete, expected):
    values = facts()
    row = values["handoff_observations"][0]
    row["destination"]["url"] = "https://target.invalid/final"
    row.update(
        redirect_chain=[values["external_link_action"]["target_url"], row["destination"]["url"]],
        redirect_chain_complete=complete,
    )
    assert judge_external_link([view(values)]).status == expected


def test_conflicting_browser_and_webview_identity_or_simultaneous_receivers_are_unknown():
    values = facts(webview=True)
    row = values["handoff_observations"][0]
    row["browser_identity"] = {**row["foreground"], "source": "platform_browser_registry"}
    assert judge_external_link([view(values)]).status == "evidence_insufficient"
    values = facts()
    values["handoff_observations"].append(facts(webview=True)["handoff_observations"][0])
    assert judge_external_link([view(values)]).status == "evidence_insufficient"


def test_webview_counterexample_survives_later_browser_and_other_subject_missing():
    values = facts(webview=True)
    later = copy.deepcopy(facts()["handoff_observations"][0])
    later["at_ms"] = 15
    values["handoff_observations"].append(later)
    item = judge_external_link([view(values), view({"target_platform": "app"}, subject="missing")])
    assert item.status == "fail" and item.missing


def test_url_equivalence_does_not_drop_path_query_fragment_or_change_port():
    values = facts()
    values["external_link_action"]["target_url"] = "HTTPS://EXAMPLE.invalid:443/path?q=1#section"
    assert judge_external_link([view(values)]).status == "pass"
    values["external_link_action"]["target_url"] = "https://example.invalid:8443/path?q=1#section"
    assert judge_external_link([view(values)]).status == "evidence_insufficient"


def test_missing_or_hash_drift_and_mixed_platforms_are_not_valid_handoff_evidence():
    assert judge_external_link([]).status == "evidence_insufficient"
    assert (
        judge_external_link([view(facts(), issues=("original_hash_drift",))]).status
        == "evidence_insufficient"
    )
    assert (
        judge_external_link([view(facts()), view(facts(platform="h5"))]).status
        == "evidence_insufficient"
    )
