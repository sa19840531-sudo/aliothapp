"""H5 标签切换不能因轮播差异占满摘要而丢失业务面板证据。"""

from copy import deepcopy

import pytest

from services.api.app.application.h5_logical_surface_evidence import build_h5_logical_surface
from services.api.app.infrastructure.browser.page_capture import _complete_action_trace


@pytest.mark.parametrize("h5, activated, content_changed", [
    (True, True, True), (False, True, True),
    (True, False, True), (True, True, False),
])
def test_tab_content_survives_bounded_carousel_diff(h5, activated, content_changed):
    url = "https://fixture.test/"
    tab = {
        "selector": "#tab", "tag": "div", "role": "tab", "text": "栏目",
        "aria_selected": "false", "class_tokens": [],
        "rect": {"x": 0, "y": 0, "width": 100, "height": 40},
    }
    panel = {
        "selector": "#z-panel-old", "tag": "div", "text": "原栏目业务内容" * 12,
        "rect": {"x": 0, "y": 40, "width": 390, "height": 720},
    }
    before = {"elements": [tab, panel] + [
        {"selector": f"#a-carousel-{index:03}", "text": "轮播",
         "rect": {"x": 0, "y": 0, "width": 20, "height": 20}}
        for index in range(60)
    ]}
    before.update(
        schema_version="ui-snapshot.v2", url=url,
        document={"history_api_observed": True, "spa_root_present": True},
        viewport={"width": 390, "height": 844, "scroll_y": 0},
    )
    after = deepcopy(before)
    after["elements"][0]["aria_selected"] = "true" if activated else "false"
    if content_changed:
        after["elements"][1].update(selector="#z-panel-new", text="新栏目业务内容" * 12)
    for element in after["elements"][2:]:
        element["rect"]["x"] = 10
    surface = build_h5_logical_surface(
        ui_snapshot=before, page_url=url, surface_session_id="h5:fixture",
        document_token="fixture-document",
    )
    before["logical_surface"] = surface
    after["logical_surface"] = deepcopy(surface)
    if h5:
        before["h5_main_structure"] = {"schema_version": "h5-native-main-shell.v1"}
        after["h5_main_structure"] = {"schema_version": "h5-native-main-shell.v1"}
    trace = [{"action": "click", "selector": "#tab"}]
    _complete_action_trace(
        trace, before_url=url, after_url=url,
        before_page_urls=[url], after_page_urls=[url],
        before_snapshot=before, after_snapshot=after,
    )
    diff = trace[0]["state_diff"]
    assert len(diff["changes"]) == 50
    assert diff["changes_truncated"] is True
    transition = diff["logical_surface_transition"]
    if h5 and activated and content_changed:
        assert transition["reason_code"] == "trusted_page_tab_state_change"
        assert transition["page_tab_proof"]["content_change_count"] == 2
    else:
        assert transition.get("reason_code") != "trusted_page_tab_state_change"
    if not h5:
        assert all(change["selector"].startswith("#a-carousel-") for change in diff["changes"])
