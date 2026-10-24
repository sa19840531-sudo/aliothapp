"""保留实际偶发失败帧；相同哈希和满分匹配不能授权空白区域点击。"""

import base64
import hashlib
import json
from pathlib import Path
from unittest.mock import Mock

import cv2
import numpy as np
import pytest

from services.api.app.infrastructure.browser import h5_visual_navigation_runtime as runtime
from services.api.app.infrastructure.browser.h5_visual_navigation_runtime import (
    resolve_visual_navigation_coordinate,
)
from services.api.tests.test_h5_visual_navigation_runtime import _viewport
from services.api.tests.test_page_capture_tool import _visual_navigation_runtime_target_from_png


@pytest.mark.parametrize("case_name", ["failed_case", "counterexample"])
def test_saved_identical_frames_require_real_target_not_only_hash(case_name):
    fixture = json.loads((Path(__file__).parent / "fixtures" /
        "h5_visual_rejected_frame_20261013.json").read_text(encoding="utf-8"))
    case = fixture[case_name]
    png = base64.b64decode(case["png_base64"], validate=True)
    if case_name == "failed_case":
        assert hashlib.sha256(png).hexdigest() == case["sha256"]
    target = _visual_navigation_runtime_target_from_png(
        source_png=png, source_rect=fixture["target_rect"],
    )
    result = resolve_visual_navigation_coordinate(
        source_png=png, current_png=png, capability=target.capability,
        current_viewport=_viewport(width=240, height=320),
    )
    if case_name == "failed_case":
        assert result.audit_codes == tuple(case["audit_codes"])
        assert result.match_score == case["match_score"]
        assert result.css_x is None and result.css_y is None
    else:
        assert not result.audit_codes
        assert result.css_x is not None and result.css_y is not None


@pytest.mark.parametrize("level", [0, 128, 255])
def test_flat_source_target_is_rejected_before_numerical_template_matching(level, monkeypatch):
    image = np.full((320, 240, 3), level, dtype=np.uint8)
    encoded, png = cv2.imencode(".png", image)
    assert encoded
    png = png.tobytes()
    target = _visual_navigation_runtime_target_from_png(
        source_png=png, source_rect=[18, 72, 115, 118],
    )
    matcher = Mock(side_effect=AssertionError("纯色目标不能产生相关性候选"))
    monkeypatch.setattr(runtime, "_template_score_map", matcher)
    result = resolve_visual_navigation_coordinate(
        source_png=png, current_png=png, capability=target.capability,
        current_viewport=_viewport(width=240, height=320),
    )
    assert result.audit_codes == ("VISUAL_TARGET_OBSTRUCTED",)
    assert result.css_x is None and result.css_y is None
    matcher.assert_not_called()
