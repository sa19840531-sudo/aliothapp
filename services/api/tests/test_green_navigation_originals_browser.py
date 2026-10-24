"""禁网自有浏览器验证既有原件生产器到绿色消费侧；不是三端产品验收。"""

import json

import pytest

from services.api.app.application.green_navigation_judge import judge_green_navigation
from services.api.app.infrastructure.browser.page_capture import _collect_ui_snapshot
from services.api.app.infrastructure.rule_judge_evidence import (
    TrustedEvidenceArtifact,
    TrustedEvidenceBundle,
)
from services.api.tests.test_h5_probe_journey_browser import page  # noqa: F401 - 自有禁网浏览器


@pytest.mark.parametrize(
    "widths,expected",
    [((100, 190, 100), "fail"), ((130, 130, 130), "evidence_insufficient")],
)
async def test_current_producer_originals_prove_unequal_width_but_not_missing_container(
    page,  # noqa: F811 - 复用禁网 fixture
    widths,
    expected,
):
    links = "".join(
        f'<a href="/category/{index}" style="display:block;flex:none;width:{width}px">'
        f"Category {index}</a>"
        for index, width in enumerate(widths)
    )
    await page.set_content(
        '<!doctype html><html><head><meta charset="UTF-8"></head><body style="margin:0">'
        '<nav id="categories" style="display:flex;width:390px">' + links + "</nav></body></html>"
    )
    snapshot = await _collect_ui_snapshot(page, surface_session_id="web:green-consumption-fixture")
    original_dom = await page.content()
    png = await page.screenshot()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert [row["rect"]["width"] for row in snapshot["elements"]] == list(widths)
    artifacts = (
        TrustedEvidenceArtifact(
            "actual-ui",
            "ui_snapshot",
            "capture",
            "primary",
            "application/json",
            json.dumps(snapshot).encode(),
        ),
        TrustedEvidenceArtifact(
            "actual-dom", "dom", "capture", "primary", "text/html", original_dom.encode()
        ),
        TrustedEvidenceArtifact("actual-png", "screenshot", "capture", "primary", "image/png", png),
    )
    outcome = judge_green_navigation(
        TrustedEvidenceBundle(
            "packet",
            "capture",
            {"primary": "capture"},
            artifacts,
        )
    )
    assert outcome["execution_status"] == expected
    equal_width = outcome["subitems"][0]
    assert equal_width["status"] == expected
    assert outcome["missing_evidence"]
    assert outcome["subitems"][3]["status"] == "evidence_insufficient"
    if expected == "fail":
        assert set(equal_width["evidence_refs"]) == {"actual-ui", "actual-dom"}
        assert equal_width["evaluated"] is True
    else:
        assert any("navigation_usable_container" in item for item in equal_width["missing"])


async def test_current_producer_missing_hidden_sibling_is_not_a_complete_subset(
    page,  # noqa: F811 - 复用禁网 fixture
):
    await page.set_content(
        "<!doctype html><html><body><nav>"
        '<a href="/a">Category A</a><a href="/b">Category B</a>'
        '<a href="/c" hidden>Category C</a></nav></body></html>'
    )
    snapshot = await _collect_ui_snapshot(page, surface_session_id="web:green-consumption-fixture")
    artifacts = (
        TrustedEvidenceArtifact(
            "ui",
            "ui_snapshot",
            "capture",
            "primary",
            "application/json",
            json.dumps(snapshot).encode(),
        ),
        TrustedEvidenceArtifact(
            "dom", "dom", "capture", "primary", "text/html", (await page.content()).encode()
        ),
    )
    outcome = judge_green_navigation(
        TrustedEvidenceBundle(
            "packet",
            "capture",
            {"primary": "capture"},
            artifacts,
        )
    )
    assert outcome["execution_status"] == "evidence_insufficient"
    assert all(item["status"] == "evidence_insufficient" for item in outcome["subitems"])


async def test_pseudo_decoration_overflow_does_not_prove_navigation_text_is_clipped(
    page,  # noqa: F811 - 复用禁网 fixture
):
    await page.set_content(
        '<!doctype html><html><head><style>body{margin:0}'
        'nav{display:flex;width:390px;height:40px}a{flex:none;width:78px;white-space:nowrap;'
        'overflow:hidden;text-overflow:ellipsis}a::after{content:"";display:inline-block;'
        'width:150px;height:1px}</style></head><body><nav role="navigation" onclick="void(0)">'
        + ''.join(f'<a href="/category/{i}">A{i}</a>' for i in range(5))
        + '</nav></body></html>'
    )
    snapshot = await _collect_ui_snapshot(page, surface_session_id="web:green-consumption-fixture")
    # 此处仅建立反例的真实文字边界，绝不把测试测量伪装成正式生产器字段。
    text_fits = await page.evaluate("""() => [...document.querySelectorAll('a')].every(a => {
      const range = document.createRange(); range.selectNodeContents(a);
      return range.getBoundingClientRect().right <= a.getBoundingClientRect().right;
    })""")
    assert text_fits is True
    links = [row for row in snapshot['elements'] if row.get('tag') == 'a']
    assert len(links) == 5 and all(row['text_metrics']['horizontal_overflow'] for row in links)
    artifacts = (
        TrustedEvidenceArtifact('ui', 'ui_snapshot', 'capture', 'primary', 'application/json',
                                json.dumps(snapshot).encode()),
        TrustedEvidenceArtifact('dom', 'dom', 'capture', 'primary', 'text/html',
                                (await page.content()).encode()),
    )
    outcome = judge_green_navigation(TrustedEvidenceBundle(
        'packet', 'capture', {'primary': 'capture'}, artifacts,
    ))
    item = outcome['subitems'][1]
    assert outcome['execution_status'] == item['status'] == 'evidence_insufficient'
    assert any('text_bounds' in gap for gap in item['missing'])
    assert item['evaluated'] is False
    assert item['observations']
