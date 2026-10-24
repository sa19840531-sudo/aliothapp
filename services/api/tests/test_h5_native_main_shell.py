"""非语义 DOM 的原生主体壳层补足结构证据，不能靠 URL 或正文放行。"""

from copy import deepcopy

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.domain.exploration.h5_logical_surface import compare_h5_logical_surfaces
from services.api.app.infrastructure.browser.page_capture import (
    _collect_ui_snapshot,
    _install_logical_surface_history_observer,
)


def _snapshot(*, member=False):
    return {
        "document": {"time_origin_ms": 1000, "history_api_observed": True,
                     "spa_root_present": True},
        "elements": [] if member else [{
            "tag": "div", "role": "tab", "text": "我的", "aria_selected": "true",
        }],
        "h5_main_structure": {
            "schema_version": "h5-native-main-shell.v1", "complete": True,
            "document_time_origin_ms": 1000,
            "roots": [{"tag": "main", "role": "", "class_tokens": ["frame"],
                       "children": [{"tag": "div", "role": "", "class_tokens": [
                           "toolbar" if member else "body-frame",
                       ]}]}],
        },
    }


def _surface(snapshot, *, url="https://fixture.test/mine"):
    return build_h5_logical_surface(
        ui_snapshot=snapshot, page_url=url, surface_session_id="h5:shell-fixture",
        document_token="document",
    )


def test_native_main_shell_corroborates_navigation_change_without_semantic_controls():
    before = _surface(_snapshot())
    after = _surface(_snapshot(member=True), url="https://fixture.test/options")
    transition = compare_h5_logical_surfaces(before=before, after=after)
    assert after["identity_signals"]["main_structure"]
    assert transition["logical_transition_proof"] == "trusted"
    assert "main_structure" in transition["corroborating_signals"]


def test_shell_change_alone_or_url_change_alone_does_not_prove_business_navigation():
    before = _snapshot()
    changed = _snapshot(member=True)
    changed["elements"] = deepcopy(before["elements"])
    assert compare_h5_logical_surfaces(
        before=_surface(before), after=_surface(changed),
    )["logical_transition_proof"] != "trusted"
    assert compare_h5_logical_surfaces(
        before=_surface(before), after=_surface(before, url="https://fixture.test/options"),
    )["logical_transition_proof"] != "trusted"


@pytest.mark.parametrize("boundary", [
    "legacy", "incomplete", "epoch", "oversize", "invalid_child", "root", "profile",
])
def test_unproven_native_shell_is_not_identity_evidence(boundary):
    snapshot = _snapshot()
    shell = snapshot["h5_main_structure"]
    if boundary == "legacy":
        snapshot.pop("h5_main_structure")
    elif boundary == "incomplete":
        shell["complete"] = False
    elif boundary == "epoch":
        shell["document_time_origin_ms"] = 2000
    elif boundary == "oversize":
        shell["roots"] *= 5
    elif boundary == "invalid_child":
        shell["roots"][0]["children"] = [None]
    elif boundary == "root":
        shell["roots"][0]["tag"] = "div"
    else:
        snapshot["document"].update(history_api_observed=False, spa_root_present=False)
    assert _surface(snapshot)["identity_signals"]["main_structure"] == []


def test_existing_structural_identity_does_not_change_when_shell_is_added():
    snapshot = _snapshot()
    snapshot["elements"].append({"tag": "h1", "text": "个人主页"})
    legacy = deepcopy(snapshot)
    legacy.pop("h5_main_structure")
    assert _surface(snapshot)["identity_fingerprint"] == _surface(legacy)["identity_fingerprint"]


def test_missing_legacy_shell_cannot_be_mistaken_for_a_structure_change():
    before = _snapshot()
    before.pop("h5_main_structure")
    after = _snapshot(member=True)
    assert compare_h5_logical_surfaces(
        before=_surface(before), after=_surface(after, url="https://fixture.test/options"),
    )["logical_transition_proof"] != "trusted"


@pytest.mark.parametrize("changed_shell", [False, True])
def test_native_and_semantic_structures_require_comparable_shell_evidence(changed_shell):
    before = _snapshot()
    after = _snapshot(member=changed_shell)
    after["elements"] = [{"tag": "h1", "text": "新页面标题"}]
    transition = compare_h5_logical_surfaces(
        before=_surface(before), after=_surface(after, url="https://fixture.test/options"),
    )
    assert (transition["logical_transition_proof"] == "trusted") is changed_shell


def test_shell_ignores_child_order_duplicate_rows_dynamic_classes_and_notice():
    before = _snapshot()
    after = deepcopy(before)
    children = after["h5_main_structure"]["roots"][0]["children"]
    children += [deepcopy(children[0]), {"tag": "div", "role": "",
                                       "class_tokens": ["announcement-banner"]}]
    children[0]["class_tokens"].append("animation-123456789")
    children.reverse()
    assert _surface(before)["identity_fingerprint"] == _surface(after)["identity_fingerprint"]


@pytest.mark.parametrize("state_class", ["active", "toolbar--active", "toolbar-selected",
                                         "toolbar_current", "checked"])
def test_native_shell_state_classes_do_not_change_page_identity(state_class):
    before = _snapshot()
    after = deepcopy(before)
    after["h5_main_structure"]["roots"][0]["children"][0]["class_tokens"].append(state_class)
    assert _surface(before)["identity_fingerprint"] == _surface(after)["identity_fingerprint"]


def test_native_shell_class_order_and_state_noise_do_not_displace_structural_classes():
    before = _snapshot()
    before["h5_main_structure"]["roots"][0]["class_tokens"] = [f"layout-{c}" for c in "abcdefghij"]
    after = deepcopy(before)
    classes = after["h5_main_structure"]["roots"][0]["class_tokens"]
    classes.reverse()
    classes.append("active")
    assert _surface(before)["identity_fingerprint"] == _surface(after)["identity_fingerprint"]


@pytest.mark.parametrize("h5", [True, False])
async def test_browser_native_shell_is_h5_only_and_survives_content_updates(h5):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 390, "height": 844})
            await _install_logical_surface_history_observer(context)
            page = await context.new_page()
            await page.set_content("""
              <div id="app"><main class="frame"><div class="toolbar">任意页面标题</div>
                <div class="body-frame"><p>动态正文</p></div></main></div>
            """)
            sid = "h5:shell-fixture" if h5 else "web:shell-fixture"
            await _collect_ui_snapshot(page, surface_session_id=sid)
            await page.evaluate("history.pushState({}, '', '#entry')")
            before = await _collect_ui_snapshot(page, surface_session_id=sid)
            if not h5:
                assert "h5_main_structure" not in before
                return
            assert before["h5_main_structure"]["complete"] is True
            assert _surface(before)["render_profile"]["kind"] == "dom_spa"
            assert _surface(before)["identity_signals"]["main_structure"]
            await page.evaluate("""() => {
              const body = document.querySelector('.body-frame');
              body.innerHTML = '<p>另一个用户的正文 99</p>'.repeat(50);
              const banner = document.createElement('div');
              banner.className = 'notice-banner'; banner.textContent = '滚动公告';
              document.querySelector('main').prepend(banner);
            }""")
            after = await _collect_ui_snapshot(page, surface_session_id=sid)
            assert (
                _surface(before)["identity_fingerprint"] == _surface(after)["identity_fingerprint"]
            )
        finally:
            await browser.close()
