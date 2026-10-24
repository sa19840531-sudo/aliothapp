"""禁网原生Range和真实手势交叉验证共享内核，不冒充现有采集端已经接入。"""

import pytest

from services.api.app.application.green_fact_bindings import GreenFactView
from services.api.app.application.green_navigation_rules import judge_navigation_subitem
from services.api.tests.test_h5_probe_journey_browser import page  # noqa: F401 - 复用禁网浏览器

_READ_GEOMETRY = """() => {
  const nav=document.querySelector('nav');
  const box=rect=>({x:rect.x,y:rect.y,width:rect.width,height:rect.height});
  const clip=box(nav.getBoundingClientRect());
  return {
    siblings_complete:true,text_ranges_complete:true,geometry_axis_aligned:true,
    geometry_precision:'raw',coordinate_space:'viewport_css_px',document_id:'document-1',
    viewport:{width:innerWidth,height:innerHeight,scale:visualViewport.scale},
    container:{id:'container-1',selector:'#categories',direction:'ltr',rect:clip,clip_rect:clip,
      scroll_left:nav.scrollLeft,client_width:nav.clientWidth,scroll_width:nav.scrollWidth},
    siblings:[...nav.children].map((a,i)=>{
      const span=a.querySelector('span'),range=document.createRange();
      range.selectNodeContents(span);
      return {id:a.id,ordinal:i+1,text:span.textContent,slot_rect:box(a.getBoundingClientRect()),
        text_rects:[...range.getClientRects()].map(box),
        clip_rect:getComputedStyle(span).overflowX==='hidden'?box(span.getBoundingClientRect()):clip};
    }),
    initial:{is_initial:true,prior_horizontal_actions:0,scroll_left:nav.scrollLeft}
  };
}"""


async def setup_navigation(browser_page, *, width=100, count=5, irregular=False, clipped=False):
    links = "".join(f'<a id="label-{i}"><span>A{i}</span></a>' for i in range(count))
    await browser_page.set_content(
        "<!doctype html><style>body{margin:0}nav{display:flex;overflow:auto;height:40px;"
        f"width:{width}px}}a{{flex:none;width:20px;height:40px;display:flex;align-items:center;"
        "justify-content:center}span{font:10px monospace}"
        + ("a:nth-child(3) span{position:relative;left:1px}" if irregular else "")
        + ("a:nth-child(3) span{display:block;width:2px;overflow:hidden}" if clipped else "")
        + '</style><nav id="categories">'
        + links
        + "</nav>"
    )
    return await browser_page.evaluate(_READ_GEOMETRY)


def judge(key, data):
    bound = GreenFactView(
        "navigation-1",
        (),
        {"navigation": data},
        ("before", "after"),
        ("gesture-1",),
        ("native-ranges", "native-gesture"),
    )
    return judge_navigation_subitem(key, (bound,))


@pytest.mark.parametrize(
    "irregular,clipped,expected",
    [
        (False, False, "pass"),
        (True, False, "fail"),
        (False, True, "fail"),
    ],
)
async def test_real_unrounded_ranges_prove_text_and_spacing(
    page,  # noqa: F811 - 复用禁网浏览器
    irregular,
    clipped,
    expected,  # noqa: F811 - 复用禁网浏览器
):
    data = await setup_navigation(page, irregular=irregular, clipped=clipped)
    assert judge("fit_navigation_spacing", data).status == expected


@pytest.mark.parametrize("width,expected", [(50, "pass"), (40, "fail"), (100, "skipped")])
async def test_initial_exposure_uses_real_range_intersection(
    page,  # noqa: F811 - 复用禁网浏览器
    width,
    expected,  # noqa: F811 - 复用禁网浏览器
):
    data = await setup_navigation(page, width=width)
    assert judge("initial_next_label", data).status == expected


async def test_real_horizontal_wheel_changes_the_same_native_container(
    page,  # noqa: F811 - 复用禁网浏览器
):
    data = await setup_navigation(page, width=50)
    await page.locator("nav").hover()
    await page.mouse.wheel(30, 0)
    await page.wait_for_function("document.querySelector('nav').scrollLeft>0")
    after = await page.locator("nav").evaluate("node=>node.scrollLeft")
    assert after != data["container"]["scroll_left"]
    data["scroll_probe"] = {
        "action_id": "gesture-1",
        "before_capture_id": "before",
        "after_capture_id": "after",
        "before_document_id": "document-1",
        "after_document_id": "document-1",
        "before_container_id": "container-1",
        "after_container_id": "container-1",
        "completed": True,
        "window_complete": True,
        "gesture_delivered": True,
        "interrupted": False,
        "position_kind": "scroll_left",
        "direction": "forward",
        "before_scroll_left": data["container"]["scroll_left"],
        "after_scroll_left": after,
    }
    assert judge("overflow_scroll", data).status == "pass"


@pytest.mark.parametrize(
    "count,width,expected",
    [
        (3, 60, "pass"),
        (3, 100, "evidence_insufficient"),
        (5, 100, "skipped"),
    ],
)
async def test_equal_width_branch_uses_real_slot_and_container_geometry(
    page,  # noqa: F811 - 复用禁网浏览器
    count,
    width,
    expected,
):
    data = await setup_navigation(page, count=count, width=width)
    assert judge("equal_width_navigation", data).status == expected


async def test_real_raw_slot_width_difference_is_an_equal_width_counterexample(
    page,  # noqa: F811 - 复用禁网浏览器
):
    await setup_navigation(page, count=3, width=60)
    await page.locator("a:nth-child(2)").evaluate("node=>node.style.width='21px'")
    data = await page.evaluate(_READ_GEOMETRY)
    assert judge("equal_width_navigation", data).status == "fail"
