"""App41强组件身份与外链目标承接的共享纯判定。"""

from __future__ import annotations

import math
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from services.api.app.application.green_rule_evidence import GreenSubitem

_BROWSER_SOURCES = {
    "browser_address_bar",
    "browser_debug_protocol",
    "platform_browser_navigation_event",
}


def judge_external_link(views):
    """只认正式已知外链动作及平台强来源；包名变化或截图变化不能补齐身份。"""
    views = tuple(views)
    missing, outcomes, captures, actions = [], [], [], []
    platforms = {str(view.get("target_platform") or "").lower() for view in views}
    if len(platforms) > 1:
        return GreenSubitem(
            "external_browser_handoff",
            "evidence_insufficient",
            "同一判定混入不同端的外链事实",
            ("authoritative_task_platform",),
        )
    for view in views:
        captures.extend(view.capture_ids)
        actions.extend(view.action_ids)
        if view.issues:
            missing.extend(view.issues)
            continue
        platform = str(view.get("target_platform") or "").lower()
        if platform in {"web", "h5"}:
            outcomes.append(("skipped", "当前正式任务为浏览器端，App41不适用"))
            continue
        if platform != "app":
            missing.append("authoritative_app_task_identity")
            continue
        try:
            status, reason, gaps = _handoff(view)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            status, reason, gaps = (
                "evidence_insufficient",
                "外链原始事实结构无效",
                ["valid_native_handoff_facts"],
            )
        outcomes.append((status, str(view.subject_id) + "：" + reason))
        missing.extend(str(view.subject_id) + ":" + gap for gap in gaps)
    if not outcomes:
        missing.append("formal_external_link_action")
    status = (
        "fail"
        if any(item[0] == "fail" for item in outcomes)
        else "evidence_insufficient"
        if missing or any(item[0] == "evidence_insufficient" for item in outcomes)
        else "pass"
        if any(item[0] == "pass" for item in outcomes)
        else "skipped"
    )
    return GreenSubitem(
        "external_browser_handoff",
        status,
        "；".join(dict.fromkeys(item[1] for item in outcomes)) or "尚缺正式外链与系统组件原件",
        tuple(dict.fromkeys(missing)),
        tuple(dict.fromkeys(captures)),
        tuple(dict.fromkeys(actions)),
    )


def _handoff(view):
    action, window = view.get("external_link_action"), view.get("handoff_window")
    rows = view.get("handoff_observations")
    if not isinstance(action, Mapping) or not isinstance(window, Mapping):
        return (
            "evidence_insufficient",
            "尚缺正式外链动作及完整承接窗口",
            ["formal_external_link_action"],
        )
    source, target = action.get("source_app_id"), _url(action.get("target_url"))
    before_foreground = action.get("before_foreground")
    if (
        action.get("action_id") not in view.action_ids
        or action.get("operation") not in {"click", "tap"}
        or not isinstance(source, str)
        or not source
        or not isinstance(before_foreground, Mapping)
        or before_foreground.get("app_id") != source
        or not isinstance(before_foreground.get("component_id"), str)
        or not before_foreground["component_id"]
        or target is None
        or action.get("before_capture_id") not in view.capture_ids
        or action.get("after_capture_id") not in view.capture_ids
        or action["before_capture_id"] == action["after_capture_id"]
        or not isinstance(action.get("clock_id"), str)
        or not action["clock_id"]
        or action.get("clock_id") != window.get("clock_id")
        or not all(
            _time(value)
            for value in (
                action.get("started_at_ms"),
                action.get("finished_at_ms"),
                window.get("start_ms"),
                window.get("end_ms"),
            )
        )
        or not window["start_ms"]
        <= action["started_at_ms"]
        < action["finished_at_ms"]
        <= window["end_ms"]
        or window.get("complete") is not True
    ):
        return (
            "evidence_insufficient",
            "正式动作、前后原件、目标或同源完整窗口未闭合",
            ["strong_formal_handoff_binding"],
        )
    if not isinstance(rows, (list, tuple)) or not rows:
        return "evidence_insufficient", "没有实际承接组件观察", ["foreground_browser_component"]
    results, gaps, seen = [], [], {}
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or not _time(row.get("at_ms"))
            or row.get("clock_id") != action["clock_id"]
            or not action["started_at_ms"] <= row["at_ms"] <= window["end_ms"]
        ):
            gaps.append("native_handoff_clock")
            continue
        kind, issue = _receiver(row, source, target)
        if issue:
            gaps.append(issue)
            continue
        if row["at_ms"] in seen and seen[row["at_ms"]] != kind:
            gaps.append("conflicting_component_observations")
            results = [result for at, result in results if at != row["at_ms"]]
            continue
        seen[row["at_ms"]] = kind
        results.append((row["at_ms"], kind))
    if any(kind == "webview" for _at, kind in results):
        return "fail", "已知外链由源App内的原生WebView实际承接", gaps
    if any(kind == "browser" for _at, kind in results) and not gaps:
        return "pass", "已知外链由平台注册的系统或第三方浏览器组件实际承接", []
    return (
        "evidence_insufficient",
        "承接身份、实际地址或观察一致性仍缺强来源",
        gaps or ["strong_receiver_identity"],
    )


def _receiver(row, source_app_id, target):
    foreground, destination = row.get("foreground"), row.get("destination")
    if not isinstance(foreground, Mapping) or not isinstance(destination, Mapping):
        return None, "foreground_component_and_actual_url"
    if not all(
        isinstance(foreground.get(key), str) and foreground[key]
        for key in ("app_id", "component_id")
    ):
        return None, "native_component_identity"
    observed = _url(destination.get("url"))
    if observed is None:
        return None, "strong_current_destination_url"
    if observed != target:
        redirects = row.get("redirect_chain")
        if (
            row.get("redirect_chain_complete") is not True
            or not isinstance(redirects, (list, tuple))
            or len(redirects) < 2
            or any(_url(url) is None for url in redirects)
            or _url(redirects[0]) != target
            or _url(redirects[-1]) != observed
        ):
            return None, "exact_action_destination_or_complete_redirect_chain"
    browser, webview = row.get("browser_identity"), row.get("webview_identity")
    browser_matches = (
        isinstance(browser, Mapping)
        and browser.get("source") == "platform_browser_registry"
        and browser.get("app_id") == foreground["app_id"]
        and browser.get("component_id") == foreground["component_id"]
    )
    webview_matches = (
        isinstance(webview, Mapping)
        and webview.get("source") == "native_view_hierarchy"
        and webview.get("owner_app_id") == source_app_id == foreground["app_id"]
        and webview.get("host_component_id") == foreground["component_id"]
        and webview.get("class_name") in {"android.webkit.WebView", "WKWebView"}
    )
    if browser_matches and webview_matches:
        return None, "ambiguous_browser_and_webview_identity"
    if browser_matches and destination.get("source") in _BROWSER_SOURCES:
        return "browser", None
    if webview_matches and destination.get("source") == "webview_navigation_callback":
        return "webview", None
    return None, "strong_browser_or_builtin_webview_identity_and_url"


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _url(value):
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        return None
    try:
        parts = urlsplit(value)
        scheme, host = parts.scheme.lower(), parts.hostname
        if (
            scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
        ):
            return None
        port = parts.port
        host = host.lower()
        if ":" in host:
            host = "[" + host + "]"
        if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
            host += ":" + str(port)
        return urlunsplit((scheme, host, parts.path or "/", parts.query, parts.fragment))
    except (TypeError, ValueError, UnicodeError):
        return None
