"""已渲染正文和导航展示文字不应把同一页面反复变成加载或新页面。"""

from copy import deepcopy

import pytest
from playwright.async_api import async_playwright

from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.domain.exploration.page_graph import build_page_state_snapshot
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot


def _snapshot():
    return {
        "schema_version": "ui-snapshot.v2", "ready_state": "complete",
        "viewport": {"width": 390, "height": 844},
        "document": {"time_origin_ms": 1000},
        "elements": [
            {"selector": "h1", "tag": "h1", "text": "栏目首页"},
            {"selector": "#footer > div:nth-of-type(1)", "parent_selector": "#footer",
             "h5_adaptive_navigation_group_selector": "#footer",
             "tag": "div", "role": "tab", "text": "主页",
             "aria_selected": "true", "interactive": True},
        ],
    }


def _surface(snapshot):
    return build_h5_logical_surface(
        ui_snapshot=snapshot, page_url="https://fixture.test/home",
        surface_session_id="h5:noise", document_token="document",
    )


def test_navigation_label_changes_do_not_change_active_slot_identity():
    before = _snapshot()
    renamed = deepcopy(before)
    renamed["elements"][1]["text"] = "返回顶部"
    assert _surface(before)["identity_fingerprint"] == _surface(renamed)["identity_fingerprint"]
    changed_slot = deepcopy(before)
    changed_slot["elements"][1]["selector"] = "#footer > div:nth-of-type(2)"
    assert (
        _surface(before)["identity_fingerprint"] != _surface(changed_slot)["identity_fingerprint"]
    )


@pytest.mark.parametrize("hidden", [False, True])
def test_completed_content_with_loading_class_is_not_page_loading(hidden):
    snapshot = _snapshot()
    snapshot["elements"].append({
        "selector": ".card > span.loading", "tag": "span", "class_tokens": ["loading"],
        "text": "更新八话 · 连载中", "visible": not hidden,
        "within_repeated_structure": True,
        "rect": {"x": 10, "y": 1200 if hidden else 100, "width": 120, "height": 14},
    })
    assert _surface(snapshot)["state_signals"]["loading"] is False


def test_visible_loading_indicator_still_blocks_and_hidden_indicator_does_not():
    snapshot = _snapshot()
    snapshot["elements"].append({
        "selector": "#spinner", "tag": "div", "class_tokens": ["loading"], "text": "",
        "rect": {"x": 150, "y": 300, "width": 40, "height": 40}, "visible": True,
    })
    assert _surface(snapshot)["state_signals"]["loading"] is True
    snapshot["elements"][-1]["visible"] = False
    assert _surface(snapshot)["state_signals"]["loading"] is False


def test_distinct_tab_positions_are_not_normalized_to_same_tab_key():
    snapshot = _snapshot()
    snapshot["elements"][1].update({
        "selector": "#tabs > div:nth-of-type(1)", "class_tokens": ["van-tab", "van-tab--active"],
        "h5_adaptive_navigation_group_selector": None,
    })
    first = _surface(snapshot)
    snapshot["elements"][1]["selector"] = "#tabs > div:nth-of-type(2)"
    second = _surface(snapshot)
    assert first["state_signals"]["tab_key"] != second["state_signals"]["tab_key"]


def _floating_timer_snapshot():
    snapshot = _snapshot()
    snapshot.update({"h5_modal_census_selectors": [], "h5_modal_census_truncated": False})
    snapshot["elements"].append({
        "selector": "#campaign-clock", "tag": "div", "role": "timer",
        "class_tokens": ["countdown"], "text": "23:51:47", "visible": True,
        "interactive": False, "aria_modal": False, "h5_modal_ancestor_selectors": [],
        "fixed_or_sticky_ancestor": True, "viewport_coverage": 0.0011,
        "rect": {"x": 260, "y": 597, "width": 36, "height": 10},
    })
    return snapshot


def test_nonblocking_floating_timer_reflow_does_not_stale_the_page():
    before = _floating_timer_snapshot()
    after = deepcopy(before)
    after["elements"][-1].update({
        "text": "23:51:31", "rect": {"x": 301, "y": 597, "width": 36, "height": 10},
    })
    assert _surface(before)["identity_fingerprint"] == _surface(after)["identity_fingerprint"]
    assert _surface(before)["state_fingerprint"] == _surface(after)["state_fingerprint"]


def _nested_floating_timer_snapshot():
    snapshot = _floating_timer_snapshot()
    timer = snapshot["elements"][-1]
    timer["text"] = "01 : 51 : 13"
    wrapper = deepcopy(timer)
    wrapper.update({"selector": "#promo-clock", "role": "", "class_tokens": ["overlay"]})
    number = deepcopy(timer)
    number.update({
        "selector": "#promo-clock > span", "role": "", "tag": "span", "text": "13",
        "class_tokens": [], "ancestor_class_tokens": ["overlay"],
        "rect": {"x": 286, "y": 597, "width": 10, "height": 10},
    })
    snapshot["elements"].extend([wrapper, number])
    return snapshot


def test_nonblocking_timer_wrapper_and_digits_are_not_obstruction_layers():
    before = _snapshot()
    after = _nested_floating_timer_snapshot()
    assert _surface(after)["state_signals"]["obstructions"] == []
    assert _surface(before)["state_fingerprint"] == _surface(after)["state_fingerprint"]


def test_nonmodal_inline_timer_and_digits_are_not_obstruction_layers():
    snapshot = _nested_floating_timer_snapshot()
    for item in snapshot["elements"][-3:]:
        item["fixed_or_sticky_ancestor"] = False
    assert _surface(snapshot)["state_signals"]["obstructions"] == []
    after = deepcopy(snapshot)
    after["elements"][-3]["text"] = "01 : 51 : 12"
    after["elements"][-2]["text"] = "01 : 51 : 12"
    after["elements"][-1]["text"] = "12"
    assert _surface(snapshot)["state_fingerprint"] == _surface(after)["state_fingerprint"]


@pytest.mark.parametrize("boundary", [
    "modal", "interactive", "census", "ancestry", "geometry", "large", "position",
])
def test_inline_timer_does_not_exclude_unproven_display_nodes(boundary):
    snapshot = _floating_timer_snapshot()
    timer = snapshot["elements"][-1]
    timer["fixed_or_sticky_ancestor"] = False
    if boundary == "modal":
        timer["aria_modal"] = True
    elif boundary == "interactive":
        timer["interactive"] = True
    elif boundary == "census":
        snapshot["h5_modal_census_truncated"] = True
    elif boundary == "ancestry":
        timer.pop("h5_modal_ancestor_selectors")
    elif boundary == "geometry":
        timer.pop("rect")
    elif boundary == "large":
        timer["rect"].update(width=390, height=844)
    else:
        timer.pop("fixed_or_sticky_ancestor")
    assert _surface(snapshot)["state_signals"]["obstructions"]


def test_timer_digit_at_exact_decimal_boundary_is_not_an_overlay():
    snapshot = _nested_floating_timer_snapshot()
    for item in snapshot["elements"][-3:-1]:
        item["rect"].update({"x": 324.03, "width": 42.5})
    snapshot["elements"][-1]["rect"].update({"x": 356.92, "width": 9.61})
    assert _surface(snapshot)["state_signals"]["obstructions"] == []


@pytest.mark.parametrize(
    "boundary", ["interactive", "modal", "outside", "unknown", "text", "geometry"],
)
def test_timer_region_does_not_hide_unproven_or_unrelated_nodes(boundary):
    snapshot = _nested_floating_timer_snapshot()
    number = snapshot["elements"][-1]
    if boundary == "interactive":
        number["interactive"] = True
    elif boundary == "modal":
        number["h5_modal_ancestor_selectors"] = ["#dialog"]
        snapshot["h5_modal_census_selectors"] = ["#dialog"]
    elif boundary == "outside":
        number["rect"]["x"] = 100
    elif boundary == "unknown":
        number.pop("h5_modal_ancestor_selectors")
    elif boundary == "geometry":
        number.pop("rect")
    else:
        number["text"] = "请先确认"
    assert any(
        item["tag"] == "span" for item in _surface(snapshot)["state_signals"]["obstructions"]
    )


def test_timer_without_geometry_cannot_exclude_its_wrapper():
    snapshot = _nested_floating_timer_snapshot()
    snapshot["elements"][-3].pop("rect")
    assert len(_surface(snapshot)["state_signals"]["obstructions"]) == 2


def _floating_image_snapshot():
    snapshot = _snapshot()
    snapshot.update({"h5_modal_census_selectors": [], "h5_modal_census_truncated": False})
    image = {
        "selector": "#promo > img", "tag": "img", "role": None, "text": "",
        "class_tokens": [], "ancestor_class_tokens": ["countdown-banner"],
        "visible": True, "interactive": False, "aria_modal": False,
        "h5_modal_ancestor_selectors": [], "fixed_or_sticky_ancestor": True,
        "within_navigation": False, "viewport_coverage": 0.012,
        "rect": {"x": 310, "y": 636, "width": 62, "height": 62},
        "image": {"complete": True, "natural_width": 120, "natural_height": 120},
    }
    wrapper = deepcopy(image)
    wrapper.update({"selector": "#promo", "tag": "div", "class_tokens": ["countdown-banner"]})
    wrapper.pop("image")
    snapshot["elements"].extend([wrapper, image])
    return snapshot


def test_small_rendered_floating_image_is_not_a_page_wide_obstruction():
    snapshot = _floating_image_snapshot()
    assert _surface(snapshot)["state_signals"]["obstructions"] == []
    assert (
        _surface(snapshot)["identity_fingerprint"] == _surface(_snapshot())["identity_fingerprint"]
    )


@pytest.mark.parametrize("modal", [False, True])
def test_floating_image_page_graph_keeps_actual_modal_exclusion(modal):
    snapshot = _floating_image_snapshot()
    if modal:
        snapshot["h5_modal_census_selectors"] = ["#promo"]
        snapshot["elements"][-1]["h5_modal_ancestor_selectors"] = ["#promo"]
    graph = build_page_state_snapshot(
        capture_id="capture", task_id="task", agent_run_id="run", surface_kind="web",
        session_id="h5:image-fixture", finalization_epoch=0, command_sequence=1,
        capture_role="after_action", page_type="home", page_state="stable",
        metadata={"logical_surface": _surface(snapshot)},
    )
    assert graph.is_overlay is modal
    assert graph.counts_as_business_page is not modal


@pytest.mark.parametrize(
    "boundary", ["modal", "truncated", "ancestry", "interactive", "role", "large", "geometry",
                 "unloaded", "text", "unrelated_wrapper", "nan", "infinite", "viewport"],
)
def test_floating_image_does_not_hide_modal_or_unproven_nodes(boundary):
    snapshot = _floating_image_snapshot()
    wrapper, image = snapshot["elements"][-2:]
    if boundary == "modal":
        snapshot["h5_modal_census_selectors"] = ["#promo"]
        image["h5_modal_ancestor_selectors"] = ["#promo"]
    elif boundary == "truncated":
        snapshot["h5_modal_census_truncated"] = True
    elif boundary == "ancestry":
        image.pop("h5_modal_ancestor_selectors")
    elif boundary == "interactive":
        image["interactive"] = True
    elif boundary == "role":
        image["role"] = "dialog"
    elif boundary == "large":
        image["viewport_coverage"] = 0.5
        image["rect"].update({"width": 390, "height": 422})
    elif boundary == "geometry":
        image.pop("rect")
    elif boundary == "unloaded":
        image["image"]["complete"] = False
    elif boundary == "text":
        wrapper["text"] = "请先确认"
    elif boundary == "nan":
        image["rect"]["width"] = float("nan")
    elif boundary == "infinite":
        image["image"]["natural_width"] = float("inf")
    elif boundary == "viewport":
        snapshot["viewport"].pop("width")
    else:
        wrapper["rect"]["x"] = 100
    assert _surface(snapshot)["state_signals"]["obstructions"]


async def test_real_floating_image_class_name_does_not_create_a_modal():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content('<h1>栏目首页</h1><button>查看栏目</button>')
            before = await _collect_ui_snapshot(page, surface_session_id="h5:image-fixture")
            await page.evaluate("""async () => {
                const wrapper = document.createElement('div');
                wrapper.className = 'countdown-banner';
                wrapper.style = 'position:fixed;top:636px;left:310px;width:62px;height:62px';
                const image = new Image();
                image.style = 'width:62px;height:62px';
                image.src = 'data:image/svg+xml,' + encodeURIComponent(
                    '<svg xmlns="http://www.w3.org/2000/svg" width="62" height="62">'
                    + '<rect width="62" height="62" fill="orange"/></svg>');
                await image.decode();
                wrapper.appendChild(image);
                document.body.appendChild(wrapper);
            }""")
            after = await _collect_ui_snapshot(page, surface_session_id="h5:image-fixture")
            assert after["h5_modal_census_selectors"] == []
            assert _surface(after)["state_signals"]["obstructions"] == []
            assert _surface(before)["state_fingerprint"] == _surface(after)["state_fingerprint"]
        finally:
            await browser.close()


async def test_real_timer_wrapper_and_digits_do_not_create_overlay_states():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.set_content('<h1>栏目首页</h1><button>查看栏目</button>')
            before = await _collect_ui_snapshot(page, surface_session_id="h5:timer-fixture")
            await page.evaluate("""() => {
                const wrapper = document.createElement('div');
                wrapper.className = 'overlay-countdown';
                wrapper.style = 'position:fixed;top:597px;left:260px;font-size:10px';
                wrapper.innerHTML = '<div role="timer"><span>01</span><span>:</span>'
                    + '<span>51</span><span>:</span><span>13</span></div>';
                document.body.appendChild(wrapper);
            }""")
            after = await _collect_ui_snapshot(page, surface_session_id="h5:timer-fixture")
            assert after["h5_modal_census_selectors"] == []
            assert _surface(before)["state_fingerprint"] == _surface(after)["state_fingerprint"]
        finally:
            await browser.close()


@pytest.mark.parametrize("modal", [False, True])
async def test_real_inline_timer_reflow_keeps_nonmodal_state_only(modal):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            modal_role = 'role="dialog" aria-modal="true"' if modal else ""
            await page.set_content(f"""
                <h1>栏目首页</h1><button>查看栏目</button>
                <div {modal_role} style="width:300px;height:100px">
                  <div role="timer" class="countdown" style="font-size:10px">
                    <span>01</span>:<span>12</span>:<span id="seconds">57</span>
                  </div>
                </div>
            """)
            before = await _collect_ui_snapshot(page, surface_session_id="h5:timer-fixture")
            await page.locator("#seconds").evaluate("e => {e.textContent = '56'}")
            after = await _collect_ui_snapshot(page, surface_session_id="h5:timer-fixture")
            timer = next(e for e in before["elements"] if e.get("role") == "timer")
            assert timer["fixed_or_sticky_ancestor"] is False
            assert bool(_surface(after)["state_signals"]["obstructions"]) is modal
            if not modal:
                assert _surface(before)["state_fingerprint"] == _surface(after)["state_fingerprint"]
        finally:
            await browser.close()


@pytest.mark.parametrize(
    "boundary",
    ["modal_parent", "modal_self", "truncated", "unknown", "large", "interactive", "aria_modal"],
)
def test_timer_reflow_remains_a_state_change_without_nonblocking_proof(boundary):
    before = _floating_timer_snapshot()
    timer = before["elements"][-1]
    if boundary == "modal_parent":
        before["h5_modal_census_selectors"] = ["#dialog"]
        timer["h5_modal_ancestor_selectors"] = ["#dialog"]
    elif boundary == "modal_self":
        before["h5_modal_census_selectors"] = [timer["selector"]]
    elif boundary == "truncated":
        before["h5_modal_census_truncated"] = True
    elif boundary == "unknown":
        before.pop("h5_modal_census_selectors")
    elif boundary == "large":
        timer["viewport_coverage"] = 0.4
    elif boundary == "interactive":
        timer["interactive"] = True
    else:
        timer["aria_modal"] = True
    after = deepcopy(before)
    after["elements"][-1]["rect"]["x"] = 301
    assert _surface(before)["state_fingerprint"] != _surface(after)["state_fingerprint"]


@pytest.mark.parametrize("modal", [False, True])
async def test_real_floating_timer_geometry_is_not_a_modal_state_unless_in_modal(modal):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            modal_role = 'role="dialog" aria-modal="true"' if modal else ""
            await page.set_content(f"""
                <h1>栏目首页</h1><button>查看栏目</button>
                <div {modal_role}
                    style="position:fixed;top:300px;left:50px;width:300px;height:350px">
                    <div id="campaign-clock" role="timer" class="countdown"
                        style="position:fixed;top:597px;left:260px;font-size:10px">23:51:47</div>
                </div>
            """)
            before = await _collect_ui_snapshot(page, surface_session_id="h5:timer-fixture")
            await page.locator("#campaign-clock").evaluate("e => {e.style.left = '301px'}")
            after = await _collect_ui_snapshot(page, surface_session_id="h5:timer-fixture")
            assert before["h5_modal_census_truncated"] is False
            before_timer = next(e for e in before["elements"] if e.get("role") == "timer")
            after_timer = next(e for e in after["elements"] if e.get("role") == "timer")
            assert before_timer["rect"] != after_timer["rect"]
            same_state = (
                _surface(before)["state_fingerprint"] == _surface(after)["state_fingerprint"]
            )
            assert same_state is not modal
        finally:
            await browser.close()
