"""H5 弹窗普查必须在两次字节压缩之后仍与实际留存元素一致。"""

from copy import deepcopy

import pytest

from services.api.app.infrastructure.browser import page_capture


def _snapshot():
    return {
        "url": "https://fixture.test/", "viewport": {"width": 390, "height": 844},
        "h5_modal_census_selectors": ["#shell", "#exit"],
        "h5_modal_census_truncated": False,
        "elements": [
            {"selector": "#shell", "tag": "div", "rect": {"width": 390, "height": 844}},
            {"selector": "#exit", "tag": "button", "type": "button",
             "dismiss_semantics": "modal", "interactive": True},
            *[{"selector": f"#item-{i}", "tag": "a", "role": "button",
               "text": "栏目" * 300, "class_tokens": ["van-dialog"],
               "rect": {"y": 0, "height": 30}} for i in range(12)],
        ],
    }


@pytest.mark.parametrize("stage", ["serialization", "logical_surface"])
def test_h5_census_members_survive_body_size_compression(monkeypatch, stage):
    monkeypatch.setattr(page_capture, "MAX_UI_SNAPSHOT_BYTES", 4000)
    snapshot = _snapshot()
    if stage == "serialization":
        result = page_capture._sanitize_and_bound_ui_snapshot(snapshot)
    else:
        result = deepcopy(snapshot)
        page_capture._bound_snapshot_after_logical_surface(result)
    assert result["truncated"] is True
    assert result["h5_modal_census_truncated"] is False
    assert set(result["h5_modal_census_selectors"]) <= {
        item["selector"] for item in result["elements"]
    }
    assert page_capture._json_size_bytes(result) <= 4000


@pytest.mark.parametrize("stage", ["serialization", "logical_surface"])
def test_missing_or_oversized_census_never_keeps_complete_flag(monkeypatch, stage):
    monkeypatch.setattr(page_capture, "MAX_UI_SNAPSHOT_BYTES", 700)
    snapshot = _snapshot()
    snapshot["elements"][1]["text"] = "关闭" * 10000
    if stage == "serialization":
        result = page_capture._sanitize_and_bound_ui_snapshot(snapshot)
    else:
        result = deepcopy(snapshot)
        page_capture._bound_snapshot_after_logical_surface(result)
    assert result["h5_modal_census_truncated"] is True


def test_missing_member_is_detected_even_without_byte_trimming():
    snapshot = _snapshot()
    snapshot["elements"] = snapshot["elements"][:1]
    result = page_capture._sanitize_and_bound_ui_snapshot(snapshot)
    assert result["h5_modal_census_truncated"] is True


def test_non_h5_snapshot_does_not_acquire_census_fields():
    result = page_capture._sanitize_and_bound_ui_snapshot({"elements": []})
    assert "h5_modal_census_truncated" not in result
