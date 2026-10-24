"""按真实 Capture 形状与首次分类顺序验证机器规则。"""

from dataclasses import replace

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.tests.test_web_page_recognition_rules import classify


@pytest.mark.asyncio
async def test_repeated_paragraphs_inside_primary_body_keep_text_and_detail_type():
    snapshot = {
        "elements": [{"tag": "main", "selector": "main", "visible": True}]
        + [
            {
                "tag": "p",
                "selector": f"main > p:nth-child({i})",
                "visible": True,
                "within_repeated_structure": True,
                "repeat_group_size": 3,
                "text": "文" * 200,
            }
            for i in range(1, 4)
        ]
    }
    facts = web_page_structure_facts(snapshot)
    assert facts.paragraph_text_length == 600
    result, _ = await classify("content_detail", facts)
    assert result.page_type == "content_detail"
    home_result, _ = await classify(
        "home", replace(facts, repeated_primary_card_count=2), url="https://example.org/"
    )
    assert home_result.page_type == "content_detail"
    assert home_result.decision_source == "deterministic"
    assert home_result.model_result["page_type"] == "home"
    assert "long_body_conflicts_with_home" in (
        home_result.model_result["rule_evaluation"]["strong_conflicts"]
    )


@pytest.mark.asyncio
async def test_article_ancestor_summary_recovers_generic_cms_content_detail() -> None:
    """元素上限遗漏正文容器时，段落父链仍应证明单一文章主体。"""

    snapshot = {
        "elements": [
            {
                "tag": "p",
                "selector": "#content article.post > div.post-content > p:nth-child(1)",
                "parent_selector": "#content article.post > div.post-content",
                "visible": True,
                "text": "通用文章正文" * 80,
                "ancestor_class_tokens": ["post-content", "post", "site-content"],
                "web_ancestor_structure": [
                    {
                        "selector": "#content article.post > div.post-content",
                        "tag": "div",
                        "repeat_group_size": 1,
                    },
                    {
                        "selector": "#content article.post",
                        "tag": "article",
                        "repeat_group_size": 1,
                    },
                    {
                        "selector": "#content",
                        "tag": "main",
                        "repeat_group_size": 1,
                    },
                ],
            }
        ]
    }

    facts = web_page_structure_facts(snapshot)
    result, _ = await classify("content_detail", facts)

    assert facts.single_subject_element_count == 1
    assert facts.article_body_count == 1
    assert facts.paragraph_text_length >= 300
    assert "post-content" in facts.dom_tokens
    assert result.page_type == "content_detail"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_type", "tag", "token"),
    [("content_detail", "div", "post-content"), ("video_detail", "video", "video-player")],
)
async def test_repeated_previews_cannot_sign_detail_even_inside_main(page_type, tag, token):
    snapshot = {
        "elements": [{"tag": "main", "selector": "main", "visible": True}]
        + [
            {
                "tag": tag,
                "selector": f"main > .preview:nth-child({i})",
                "visible": True,
                "class_tokens": [token],
                "src": "/preview.mp4" if tag == "video" else None,
                "within_repeated_structure": True,
                "repeat_group_size": 3,
            }
            for i in range(1, 4)
        ]
    }
    facts = web_page_structure_facts(snapshot)
    result, _ = await classify(page_type, facts)
    assert result.page_type == "unknown"
    assert "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT" in result.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["non-video-card", "not-media-card", "uncover", "videography"])
async def test_media_substrings_cannot_promote_text_cards_to_video_list(token):
    snapshot = {
        "elements": [
            {
                "tag": "a",
                "role": "link",
                "selector": f".{token}:nth-child({i})",
                "class_tokens": [token],
                "ancestor_class_tokens": [token],
                "visible": True,
                "interactive": True,
                "href": f"/entry/{i}",
                "text": f"文章{i}",
                "within_repeated_structure": True,
                "repeat_group_size": 2,
                "rect": {"x": 10, "y": i * 100, "width": 200, "height": 80},
            }
            for i in range(1, 3)
        ]
    }
    facts = web_page_structure_facts(
        snapshot, origin="https://example.org", current_url="https://example.org/category"
    )
    assert facts.primary_detail_link_count == 2
    assert facts.media_card_count == 0
    result, _ = await classify("video_list", facts)
    assert result.page_type == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("page_type", "tag", "token"),
    [("content_detail", "article", "article-body"), ("video_detail", "video", "video-player")],
)
async def test_genuine_primary_detail_structure_still_signs(page_type, tag, token):
    facts = web_page_structure_facts(
        {
            "elements": [
                {"tag": tag, "selector": "#primary", "visible": True, "class_tokens": [token],
                 "src": "/main.mp4" if tag == "video" else None,
                 "text": "正文" * 160 if tag == "article" else ""}
            ]
        }
    )
    result, _ = await classify(page_type, facts)
    assert result.page_type == page_type
    assert result.conflicts == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("trusted", [True, False])
async def test_first_read_then_classify_resolves_root_alias_before_rule_evaluation(trusted):
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.web_logical_target import WebLogicalTargetClassifier
    from services.api.app.application.web_page_classification import WebPageClassifier
    from services.api.app.application.web_runtime_evidence import WebRuntimeEvidenceSource
    from services.api.app.domain.page import load_web_page_type_catalog
    from services.api.app.domain.page.web_url_identity import WebUrlIdentityPolicy
    from services.api.tests.test_web_capture_readiness_metrics import _stable_capture_metrics
    from services.api.tests.test_web_page_recognition_rules import Suggestion
    from services.api.tests.test_web_runtime_evidence import IndexReader

    class Repository:
        alias_ready = False
        saved = None

        async def find(self, **kwargs):
            assert kwargs["source_capture_id"] == "capture-1"
            assert kwargs["url"] == "https://example.com/"
            return {"logical_page_id": target_id} if self.alias_ready else None

        async def claim(self, **kwargs):
            return {"created": True, "logical_page_id": target_id}

        async def record_capture_redirect_aliases(self, **kwargs):
            self.alias_ready = trusted

        async def materialize(self, **kwargs):
            self.saved = kwargs["assessment"]

    class Snapshot:
        async def read_ui_snapshot(self, **kwargs):
            return {
                "web_capture_stage": "web_page_ready_baseline",
                "elements": [
                    {"tag": "section", "selector": "#news", "visible": True},
                    {"tag": "section", "selector": "#hot", "visible": True},
                ],
            }

    repository = Repository()
    index = IndexReader()
    index.url = "https://example.com/index.html"
    target_id = WebUrlIdentityPolicy().logical_id(index.url)
    index.pipeline_metrics = {"web_readiness": _stable_capture_metrics(page_settle_complete=True)}
    source = WebRuntimeEvidenceSource(
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
        deadline=datetime.now(UTC) + timedelta(minutes=2),
        index_reader=index,
        snapshot_reader=Snapshot(),
        logical_target_repository=repository,
    )
    capture = await source.read_ready(capture_id="capture-1", page_role="home")
    assert capture.structure_facts.is_canonical_root_url is False
    classifier = WebLogicalTargetClassifier(
        classifier=WebPageClassifier(
            assessor=Suggestion("home"), catalog=load_web_page_type_catalog()
        ),
        repository=repository,
        task_id="task-1",
        agent_run_id="run-1",
        session_id="web:run-1",
    )
    result = await classifier.classify(capture)
    assert result.page_type == ("home" if trusted else "unknown")
    assert repository.saved["page_type"] == result.page_type
