"""必要媒体分母只含关键主体；非关键解码问题保留独立审计。"""

from copy import deepcopy

import pytest

from services.api.app.infrastructure.browser.web_media_readiness import (
    read_viewport_media_readiness,
)


def _captured_empty_decoration():
    """来自3106eae3的未知节点，仅保留真实几何/解码字段，不含站点正文。"""
    return {
        "index": 75,
        "tag": "img",
        "rect": {"x": 680, "y": 896, "width": 96, "height": 96},
        "viewport_coverage": 0.0044444444444444444,
        "critical": False,
        "semantic_required": False,
        "complete": True,
        "natural_width": 0,
        "natural_height": 0,
        "ready_state": None,
        "error_code": None,
        "current_src": "",
        "resource_state": "unknown",
        "resource_state_basis": "image_decode_state",
    }


class _CapturedMediaPage:
    def __init__(self, items):
        self.items = items

    async def evaluate(self, expression):
        return deepcopy(self.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["unknown", "failed", "pending"])
async def test_noncritical_capture_issue_is_warning_not_necessary_media_blocker(state):
    item = _captured_empty_decoration()
    item["resource_state"] = state
    if state != "unknown":
        item["current_src"] = "https://images.example.test/decoration.png"
        item["complete"] = state == "failed"
    media = await read_viewport_media_readiness(_CapturedMediaPage([item]))
    assert media["state"] == "ready"
    assert media["schema_version"] == "viewport-media.v2"
    assert media["necessary_count"] == 0
    assert media["necessary_ready_count"] == 0
    assert media["noncritical_resource_counts"][state] == 1
    assert media["warnings"] == ["noncritical_media_" + state]
    assert media["items"][0]["resource_state"] == state


@pytest.mark.asyncio
@pytest.mark.parametrize("necessity", ["critical", "semantic_required"])
@pytest.mark.parametrize(
    "state,expected",
    [("unknown", "unknown"), ("failed", "critical_failed"), ("pending", "pending")],
)
async def test_necessary_media_issue_never_becomes_noncritical_warning(necessity, state, expected):
    item = _captured_empty_decoration()
    item[necessity] = True
    item["resource_state"] = state
    item["complete"] = state == "failed"
    media = await read_viewport_media_readiness(_CapturedMediaPage([item]))
    assert media["state"] == expected
    assert media["necessary_count"] == 1
    assert media["necessary_ready_count"] == 0
    assert media["warnings"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "incomplete", ["truncated", "necessity_missing", "state_missing", "state_invalid_type"]
)
async def test_incomplete_sampling_cannot_prove_empty_necessary_set(incomplete):
    item = _captured_empty_decoration()
    items = [item] * 101
    if incomplete == "necessity_missing":
        del item["critical"]
        items = [item]
    elif incomplete == "state_missing":
        del item["resource_state"]
        items = [item]
    elif incomplete == "state_invalid_type":
        item["resource_state"] = []
        items = [item]
    media = await read_viewport_media_readiness(_CapturedMediaPage(items))
    assert media["state"] == "unknown"
