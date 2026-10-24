"""真实 MAF 入口的详情提示合同与安全拒绝回归。"""

import json

import pytest

from services.api.app.application.web_page_classification import (
    WebPageClassifier,
    WebPageStructureFacts,
    WebReadyPageCapture,
)
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.app.domain.page.web_site_menu_registry import WebPageClassificationHint
from services.api.tests.support.fake_chat_client import ScriptedChatClient, make_text_response
from services.api.tests.test_web_exploration_assessment_agent import (
    assessment_agent,
    first_user_payload,
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subjects,model_review,expected_type,expected_review",
    [
        (1, None, "content_detail", None),
        (2, None, "unknown", "WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT"),
        (1, "当前主要内容仍需人工确认", "unknown", "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"),
        (1, "提示预期与当前主体不同", "unknown", "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"),
    ],
)
async def test_real_agent_explains_detail_hint_and_recommendation_scope_without_bypassing_review(
    subjects,
    model_review,
    expected_type,
    expected_review,
):
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "content_detail",
                        "confidence": 0.70,
                        "evidence": ["当前文章正文与主体外推荐列表分区显示"],
                        "needs_review_reason": model_review,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    capture = WebReadyPageCapture(
        capture_id="detail-capture",
        web_capture_stage="web_page_ready_baseline",
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        page_role="detail",
        classification_hint=WebPageClassificationHint(
            expected_page_type="video_detail",
            confidence=0.9,
            source="trusted_list_detail_transition",
        ),
        structure_facts=WebPageStructureFacts(
            navigation_element_count=1,
            repeated_link_count=20,
            video_element_count=0,
            single_subject_element_count=subjects,
            dom_tokens=("article", "article-body"),
            article_body_count=subjects,
            paragraph_text_length=800,
            subject_region_facts=WebSubjectRegionFacts(
                primary_subject_count=subjects,
                primary_subject_kind="article",
                primary_article_body_count=subjects,
                primary_article_text_length=800,
                secondary_recommendation_card_count=20,
            ),
        ),
    )

    result = await WebPageClassifier(
        assessor=assessment_agent(client),
        catalog=load_web_page_type_catalog(),
    ).classify(capture)

    assert result.page_type == expected_type
    assert result.needs_review_reason == expected_review
    assert result.confidence == 0.70
    assert result.expected_page_type == "video_detail"
    request = first_user_payload(client)
    assert request["structure_facts"]["subject_region_facts"]["primary_subject_count"] == subjects
    assert (
        request["structure_facts"]["subject_region_facts"]["secondary_recommendation_card_count"]
        == 20
    )
    contract = request["detail_classification_contract"]
    assert "video_list" in contract["upstream_list_type"]
    assert "不意味着" in contract["upstream_list_type"]
    assert "page_role=detail 不能证明" in contract["upstream_list_type"]
    assert "不得仅因" in contract["hint_conflict"]
    assert "needs_review_reason" in contract["hint_conflict"]
    assert "主体外" in contract["subject_boundary"]
    assert "多个" in contract["safety_review"]
    assert "复核" in contract["safety_review"]


@pytest.mark.asyncio
@pytest.mark.parametrize("page_role", ["home", "menu"])
async def test_detail_explanation_is_not_injected_into_other_page_roles(page_role):
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "content_list",
                        "confidence": 0.85,
                        "evidence": ["当前主体为重复内容卡片"],
                        "needs_review_reason": None,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    result = await assessment_agent(client).classify(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        allowed_page_types=("content_list",),
        page_type_catalog=({"code": "content_list"},),
        page_role=page_role,
        structure_facts={"repeated_primary_card_count": 8},
        classification_hint=None,
    )
    assert result.page_type == "content_list"
    assert "detail_classification_contract" not in first_user_payload(client)


@pytest.mark.asyncio
async def test_detail_explanation_does_not_change_isolated_ad_assessment():
    client = ScriptedChatClient(
        [
            make_text_response(
                json.dumps(
                    {
                        "schema_version": "web-page-assessment.v1",
                        "page_type": "ad_page",
                        "confidence": 0.9,
                        "evidence": ["隔离页面为广告落地内容"],
                        "needs_review_reason": None,
                    },
                    ensure_ascii=False,
                )
            )
        ]
    )
    result = await assessment_agent(client).assess_ad_page(
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        ui_snapshot_artifact_id="artifact-ui",
        har_artifact_id="artifact-har",
        action_trace_artifact_id="artifact-trace",
    )
    assert result.page_type == "ad_page"
    request = first_user_payload(client)
    assert request["assessment_kind"] == "web_ad_page_type"
    assert "detail_classification_contract" not in request
