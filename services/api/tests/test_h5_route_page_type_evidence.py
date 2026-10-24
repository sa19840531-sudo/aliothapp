"""阅读详情真实误分类的脱敏事实；路由不能覆盖合格视觉模型证据。"""

import pytest

from services.api.app.agents.runtime.run_context import AgentRunContext, bind_agent_run_context
from services.api.app.agents.tools import page_detection_tools as detection
from services.api.app.domain.page.catalog import load_default_page_type_catalog
from services.api.app.domain.page.registry import recognition_signal_references
from services.api.tests.test_page_detection_tools import (
    PNG_BYTES,
    FakeArtifactReader,
    FakeChatClient,
    FakeEvidenceLoader,
    _artifact,
    _bundle,
    _recognized_output,
)

READER_DOM = (
    '<html><body><main><h1>作品详情</h1><button>开始阅读</button>'
    '<button>章节列表</button><button>加入书架</button></main></body></html>'
)
PLAYER_DOM = (
    '<html><body><main><div class="video-player-box"><video controls src="/media.mp4">'
    '</video></div><div class="video-info"><h1>视频详情</h1></div></main></body></html>'
)


@pytest.mark.parametrize("page_type,dom,expected", [
    ("content_detail", READER_DOM, "content_detail"),
    ("video_detail", PLAYER_DOM, "video_detail"),
    ("content_detail", '<main><p>详情加载中</p></main>', "content_detail"),
])
async def test_h5_route_does_not_override_accepted_guide(page_type, dom, expected):
    catalog = load_default_page_type_catalog()
    page = catalog.require_code(page_type)
    refs = recognition_signal_references(page)
    output = _recognized_output(page_type).model_copy(update={
        "confidence": 0.93,
        "page_type_candidates": [],
        "matched_catalog_signals": detection.MatchedCatalogSignals(
            required_positive_signal_ids=[
                item["signal_id"] for item in refs["required_positive_signals"]
            ],
            excluded_confusable_page_types=list(page.confusable_with),
        ),
    })
    with bind_agent_run_context(AgentRunContext(
        task_id="11111111-1111-1111-1111-111111111111", agent_run_id="run-route",
    )):
        result = await detection.detect_page_for_agent(
            page_capture_id="22222222-2222-2222-2222-222222222222",
            evidence_loader=FakeEvidenceLoader(_bundle(
                _artifact("dom", "dom", content_type="text/html"),
                _artifact("shot", "screenshot", content_type="image/png"),
                page_url="https://fixture.test/twoDimensional/comics/detail?id=123",
            )), artifact_reader=FakeArtifactReader({"dom": dom.encode(), "shot": PNG_BYTES}),
            chat_client=FakeChatClient(output), skill_text="# detection", catalog=catalog,
            model_name="test", recognition_guide_enforced=True,
        )
    assert result["page_type"] == expected
    assert result["confidence"] == 0.93
    assert result["confidence_source"] == "model_self_report"


@pytest.mark.parametrize("dom,expected", [
    (READER_DOM, None), (PLAYER_DOM, "video_detail"),
    ('<div hidden>' + PLAYER_DOM + '</div>', None),
    ('<div style="display:none">' + PLAYER_DOM + '</div>', None),
    ('<div role="dialog" aria-modal="true">' + PLAYER_DOM + '</div>', None),
    ('<main><video controls src="/ad.mp4"></video><p>推荐视频</p></main>', None),
])
def test_h5_video_route_requires_player_corroboration(dom, expected):
    result = detection._deterministic_page_type_from_business_route(
        "https://fixture.test/twoDimensional/comics/detail?id=123", h5_dom_chunks=[dom],
    )
    assert (result.page_type if result else None) == expected


def test_non_h5_business_route_contract_is_unchanged():
    result = detection._deterministic_page_type_from_business_route(
        "https://fixture.test/twoDimensional/comics/detail?id=123",
    )
    assert result.page_type == "video_detail"
