"""任意 CSS 的 H5 顶层关闭能力只接受现场渲染和命中证明。"""

from copy import deepcopy

import pytest

from services.api.app.application.h5_modal_close import h5_modal_close


def _snapshot():
    return {
        "h5_modal_census_truncated": False,
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": "#arbitrary-layer",
                "tag": "div",
                "role": "dialog",
                "rect": {"x": 40, "y": 150, "width": 310, "height": 400},
                "hit_test": {"center_target": "#copy", "center_hits_target": False},
            },
            {
                "selector": "#copy",
                "tag": "p",
                "text": "动态公告 1",
                "rect": {"x": 60, "y": 260, "width": 270, "height": 180},
                "hit_test": {"center_target": "#copy", "center_hits_target": True},
            },
            {
                "selector": "#arbitrary-exit",
                "tag": "button",
                "type": "button",
                "parent_selector": "#arbitrary-layer",
                "aria_label": "关闭",
                "interactive": True,
                "rect": {"x": 300, "y": 170, "width": 30, "height": 30},
                "hit_test": {"center_target": "#arbitrary-exit", "center_hits_target": True},
            },
        ],
    }


def test_arbitrary_css_and_icon_label_are_supported_without_product_mapping():
    proof = h5_modal_close(_snapshot())
    assert proof is not None
    assert proof.selector == "#arbitrary-exit"
    changed = _snapshot()
    changed["elements"][1]["text"] = "动态公告 2"
    assert h5_modal_close(changed).obstruction_signature == proof.obstruction_signature


@pytest.mark.parametrize(
    "changes",
    [
        {"type": "submit"},
        {"type": None},
        {"href": "https://outside.test/"},
        {"aria_label": "确认支付"},
        {"hit_test": {"center_hits_target": False}},
        {"style": {"pointer_events": "none"}},
        {"disabled": True},
        {"aria_disabled": "true"},
        {"navigation_payload_in_subtree": True},
    ],
)
def test_submit_navigation_or_non_hittable_controls_do_not_get_close_authority(changes):
    snapshot = _snapshot()
    snapshot["elements"][-1].update(changes)
    assert h5_modal_close(snapshot) is None


def test_two_visible_closers_and_truncated_snapshot_are_rejected():
    snapshot = _snapshot()
    second = deepcopy(snapshot["elements"][-1])
    second["selector"] = "#second-exit"
    snapshot["elements"].append(second)
    assert h5_modal_close(snapshot) is None
    snapshot = _snapshot()
    snapshot["truncated"] = True
    assert h5_modal_close(snapshot) is None


def test_inline_controls_cannot_claim_modal_authority():
    snapshot = _snapshot()
    snapshot["elements"][0]["role"] = ""
    assert h5_modal_close(snapshot) is None


def test_non_h5_snapshot_does_not_receive_new_modal_close_authority():
    snapshot = _snapshot()
    snapshot.pop("h5_modal_census_truncated")
    assert h5_modal_close(snapshot) is None


def test_unrelated_control_inside_modal_rectangle_cannot_claim_authority():
    snapshot = _snapshot()
    snapshot["elements"][-1]["parent_selector"] = "body"
    assert h5_modal_close(snapshot) is None


def test_custom_full_screen_backdrop_uses_geometry_not_class_names():
    snapshot = _snapshot()
    snapshot["elements"][0].update(
        {
            "role": "",
            "rect": {"x": 0, "y": 0, "width": 390, "height": 844},
            "style": {"position": "fixed", "z_index": "901"},
        }
    )
    assert h5_modal_close(snapshot) is not None


def test_existing_image_close_pattern_uses_the_same_signed_modal_contract():
    snapshot = _snapshot()
    snapshot["elements"][0]["class_tokens"] = ["van-dialog"]
    control = snapshot["elements"][-1]
    control.update({
        "tag": "div", "type": None, "aria_label": None, "text": "",
        "class_tokens": ["btn"], "ancestor_class_tokens": ["grid-main", "van-dialog"],
    })
    proof = h5_modal_close(snapshot)
    assert proof is not None
    assert proof.selector == control["selector"]
    control["navigation_payload_in_subtree"] = True
    assert h5_modal_close(snapshot) is None


def test_complete_census_retains_pointer_transparent_modal_wrapper():
    snapshot = _snapshot()
    wrapper = deepcopy(snapshot["elements"][0])
    wrapper.update({"selector": "#transparent-wrapper", "style": {"pointer_events": "none"}})
    snapshot["elements"].append(wrapper)
    snapshot["truncated"] = True
    snapshot["h5_modal_census_selectors"] = [
        "#arbitrary-layer", "#transparent-wrapper", "#arbitrary-exit",
    ]
    assert h5_modal_close(snapshot) is not None
    snapshot["elements"].pop()
    assert h5_modal_close(snapshot) is None
