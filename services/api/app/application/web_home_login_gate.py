"""从同一 ready Capture 的可信 UI 快照诊断首页登录门。"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import SplitResult, urlsplit


def _web_url(value: object) -> SplitResult | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.port == 0:
            return None
        return parsed
    except ValueError:
        return None


def _same_origin(left: SplitResult, right: SplitResult) -> bool:
    return (
        left.scheme == right.scheme
        and left.hostname == right.hostname
        and (left.port or (443 if left.scheme == "https" else 80))
        == (right.port or (443 if right.scheme == "https" else 80))
    )


def _visible_control(node: object) -> bool:
    if not isinstance(node, Mapping) or node.get("visible") is not True:
        return False
    if node.get("interactive") is not True or node.get("disabled") is True:
        return False
    hit = node.get("hit_test")
    return (
        isinstance(hit, Mapping)
        and hit.get("center_hits_target") is True
        and hit.get("visual_occlusion_detected") is not True
    )


def _within_form(node: Mapping[str, object], form_selector: str) -> bool:
    ancestors = node.get("web_ancestor_structure")
    return isinstance(ancestors, list) and any(
        isinstance(item, Mapping)
        and item.get("tag") == "form"
        and item.get("selector") == form_selector
        for item in ancestors
    )


def is_web_home_login_gate(
    *, target_url: str, ready_url: str | None, snapshot: Mapping[str, object]
) -> bool:
    """只在根请求落入同源登录路径、且可见表单完整时给出诊断。"""

    target = _web_url(target_url)
    ready = _web_url(ready_url)
    observed = _web_url(snapshot.get("url"))
    if (
        target is None
        or ready is None
        or observed is None
        or target.path not in {"", "/"}
        or ready.path.rstrip("/").casefold() != "/login"
        or observed.path.rstrip("/").casefold() != "/login"
        or not _same_origin(target, ready)
        or not _same_origin(ready, observed)
    ):
        return False
    elements = snapshot.get("elements")
    if not isinstance(elements, list):
        return False
    for form in elements:
        if not isinstance(form, Mapping) or form.get("tag") != "form":
            continue
        selector = form.get("selector")
        if form.get("visible") is not True or not isinstance(selector, str) or not selector:
            continue
        password = any(
            isinstance(node, Mapping)
            and node.get("tag") == "input"
            and str(node.get("type") or "").casefold() == "password"
            and _visible_control(node)
            and _within_form(node, selector)
            for node in elements
        )
        submit = any(
            isinstance(node, Mapping)
            and node.get("tag") in {"button", "input"}
            and str(node.get("type") or "").casefold() == "submit"
            and _visible_control(node)
            and _within_form(node, selector)
            for node in elements
        )
        if password and submit:
            return True
    return False
