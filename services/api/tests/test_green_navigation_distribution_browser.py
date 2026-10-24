"""禁网浏览器经正式采集函数验证留白导航；不声称实际产品验收。"""

import json

import pytest

from services.api.app.application.green_navigation_judge import judge_green_navigation
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)
from services.api.tests.test_green_navigation_judge import subitem
from services.api.tests.test_h5_probe_journey_browser import page  # noqa: F401 - 复用禁网浏览器


@pytest.mark.parametrize("platform", ["h5", "web"])
@pytest.mark.parametrize("count,justify,expected", [
    (2, "space-around", "pass"),
    (3, "space-around", "pass"),
    (4, "space-around", "pass"),
    (3, "flex-start", "evidence_insufficient"),
    (5, "space-around", "skipped"),
])
async def test_existing_producer_equal_region_distribution(
    page, count, justify, expected, platform,  # noqa: F811 - 复用禁网浏览器
):
    links = "".join(
        f'<a href="/category/{index}" style="flex:none;width:60px;height:40px">'
        f"A{index}</a>" for index in range(count)
    )
    await page.set_content(
        '<!doctype html><html><body style="margin:0">'
        '<nav role="navigation" onclick="void(0)" '
        f'style="display:flex;width:390px;height:40px;justify-content:{justify}">'
        + links + '</nav></body></html>'
    )
    snapshot = await _collect_ui_snapshot(page, surface_session_id=f"{platform}:green-distribution")
    # 原件直接来自现有生产器，不补写容器、序号、文字边界或滚动状态。
    nav = [row for row in snapshot["elements"] if row["tag"] == "nav"]
    assert len(nav) == 1 and nav[0]["rect"]["width"] == 390
    controls = [row for row in snapshot["elements"] if row["tag"] == "a"]
    assert len(controls) == count and all(row["rect"]["width"] == 60 for row in controls)
    artifacts = (
        TrustedEvidenceArtifact("actual-ui", "ui_snapshot", "capture", "primary",
                                "application/json", json.dumps(snapshot).encode()),
        TrustedEvidenceArtifact("actual-dom", "dom", "capture", "primary",
                                "text/html", (await page.content()).encode()),
    )
    outcome = judge_green_navigation(TrustedEvidenceBundle(
        "packet", "capture", {"primary": "capture"}, artifacts,
    ))
    item = subitem(outcome, "equal_width_navigation")
    assert item["status"] == expected
    assert outcome["execution_status"] == "evidence_insufficient"
    assert subitem(outcome, "initial_next_label")["missing"]
    if count == 5:
        assert subitem(outcome, "fit_navigation_spacing")["status"] == "evidence_insufficient"
    elif expected == "pass":
        assert item["evidence_refs"] == ["actual-ui", "actual-dom"]
