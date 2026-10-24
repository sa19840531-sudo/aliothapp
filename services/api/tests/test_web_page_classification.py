"""Web 四类业务页面只能基于无遮挡基线签发。"""

from dataclasses import replace

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    WebPageStructureFacts,
    WebReadyPageCapture,
    web_page_structure_facts,
)
from services.api.app.application.web_readiness_contracts import (
    WebEvidenceRole,
    WebReadinessStatus,
    WebSettleAssessment,
)
from services.api.app.application.web_subject_region_model import WebSubjectRegionFacts
from services.api.app.domain.page import (
    load_web_page_type_catalog,
)
from services.api.app.domain.page.web_site_menu_registry import WebPageClassificationHint


def _capture(
    *,
    stage: str = "web_page_ready_baseline",
    capture_purpose: str = "web_page_ready_baseline",
    page_role: str = "menu",
    url: str | None = None,
    facts: WebPageStructureFacts | None = None,
    hint: WebPageClassificationHint | None = None,
) -> WebReadyPageCapture:
    if facts is not None and facts.subject_region_facts is None:
        # 手工门禁夹具显式提供独立主体；真实 DOM 用例始终使用实际提取结果。
        kind = (
            "article"
            if facts.article_body_count > 0 and facts.paragraph_text_length >= 120
            else "video"
            if facts.confirmed_primary_video_resource_count > 0
            else "unknown"
        )
        facts = replace(
            facts,
            subject_region_facts=WebSubjectRegionFacts(
                primary_subject_count=int(kind != "unknown"),
                primary_subject_kind=kind,
                primary_article_body_count=facts.article_body_count if kind == "article" else 0,
                primary_article_text_length=facts.paragraph_text_length if kind == "article" else 0,
                primary_player_count=facts.confirmed_primary_video_resource_count
                if kind == "video"
                else 0,
                repeated_primary_card_count=0
                if kind != "unknown"
                else facts.repeated_primary_card_count,
            ),
        )
    return WebReadyPageCapture(
        capture_id="capture-ready",
        web_capture_stage=stage,
        screenshot_artifact_id="artifact-screenshot",
        dom_artifact_id="artifact-dom",
        visible_text_artifact_id="artifact-visible-text",
        page_role=page_role,
        url=url,
        capture_purpose=capture_purpose,
        structure_facts=facts
        or WebPageStructureFacts(
            navigation_element_count=1,
            repeated_link_count=3,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("video-list", "video-grid", "poster"),
            primary_detail_link_count=3,
            repeated_primary_card_count=3,
            media_card_count=3,
            subject_region_facts=WebSubjectRegionFacts(repeated_primary_card_count=3),
        ),
        classification_hint=hint,
    )


class Assessor:
    def __init__(self, result: WebPageAssessment) -> None:
        self.result = result
        self.calls = []

    async def classify(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


@pytest.mark.asyncio
@pytest.mark.parametrize("page_role", ["menu", "home"])
async def test_conflicting_hint_cannot_clear_unresolved_model_review(page_role):
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_list",
                confidence=0.99,
                evidence=("重复卡片但视觉待复核",),
                needs_review=True,
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            page_role=page_role,
            facts=WebPageStructureFacts(
                navigation_element_count=2,
                repeated_link_count=6,
                primary_detail_link_count=6,
                repeated_primary_card_count=6,
                media_card_count=6,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=("video-grid",),
            ),
            hint=WebPageClassificationHint(
                expected_page_type="content_list",
                confidence=0.95,
                source="runtime_dom_observation",
            ),
        )
    )
    assert result.page_type == "unknown"
    assert result.needs_review is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model_type,want", [("content_detail", "content_detail"), ("video_detail", "video_detail")]
)
async def test_model_detail_axis_wins_while_embedded_media_facts_remain(model_type, want):
    structure = web_page_structure_facts(
        {
            "elements": [
                {"tag": "main", "selector": "#body", "visible": True},
                {
                    "tag": "p",
                    "selector": "#p",
                    "parent_selector": "#body",
                    "visible": True,
                    "text": "正文" * 150,
                },
                {
                    "tag": "video",
                    "selector": "#video",
                    "parent_selector": "#body",
                    "visible": True,
                    "src": "/embedded.mp4",
                },
            ]
        }
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(page_type=model_type, confidence=0.99, evidence=("正文及内嵌媒体",))
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=structure))
    assert result.page_type == want
    assert result.decision_source == "model"
    assert result.deterministic_result["page_type"] == "content_detail"
    assert structure.confirmed_primary_video_resource_count == 0


def test_frozen_v7_confidence_floor_keeps_legacy_unclassified_result():
    from services.api.app.application.web_page_classification import (
        enforce_web_formal_confidence_floor,
    )

    result = enforce_web_formal_confidence_floor(
        WebPageAssessment(
            page_type="video_list",
            confidence=0.69,
            evidence=("视频卡片",),
            guide_version="2026-09-13.web.v7",
        )
    )
    assert result.page_type is None
    assert result.proposed_page_type == "video_list"


def _v8_element(tag="div", selector="#subject", **values):
    return {"tag": tag, "selector": selector, "visible": True, **values}


V8_SUBJECT_CASES = [
    (
        [
            _v8_element(class_tokens=["image-gallery"]),
            _v8_element(
                "img", "#subject img", src="/photo.jpg", ancestor_class_tokens=["image-gallery"]
            ),
            _v8_element(
                "button",
                "#subject button",
                class_tokens=["zoom-in"],
                ancestor_class_tokens=["image-gallery"],
            ),
        ],
        "content_detail",
        "content_detail",
    ),
    (
        [
            _v8_element("article"),
            _v8_element("p", "#subject p", text="正文" * 160),
            _v8_element(
                "video",
                "#subject video",
                src="/embedded.mp4",
                ancestor_class_tokens=["article-body"],
            ),
        ],
        "content_detail",
        "content_detail",
    ),
    ([_v8_element(class_tokens=["video-player"])], "video_detail", "unknown"),
    ([_v8_element("video", src="/primary.mp4")], "video_detail", "video_detail"),
    (
        [
            _v8_element("main", class_tokens=["campaign"]),
            _v8_element(
                "button",
                "#subject button",
                class_tokens=["claim"],
                ancestor_class_tokens=["campaign"],
            ),
        ],
        "activity",
        "activity",
    ),
    (
        [
            _v8_element("form", role="search"),
            _v8_element("input", "#subject input", type="search"),
            _v8_element("button", "#subject button", type="submit"),
        ],
        "search_page",
        "search_page",
    ),
    ([_v8_element("button", class_tokens=["search-icon"])], "search_page", "unknown"),
    ([_v8_element("canvas", class_tokens=["player-root"])], "video_detail", "unknown"),
    (
        [_v8_element("video", src="/preview.mp4", within_repeated_structure=True)],
        "video_detail",
        "unknown",
    ),
    (
        [
            _v8_element(class_tokens=["search-results"]),
            _v8_element(
                "article",
                "#subject .result",
                within_repeated_structure=True,
                ancestor_class_tokens=["search-results"],
            ),
        ],
        "search_page",
        "search_page",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("elements,model_type,expected", V8_SUBJECT_CASES)
@pytest.mark.parametrize("confidence", [0.69, 0.70, 0.9])
async def test_v8_primary_subject_rules(elements, model_type, expected, confidence):
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type=model_type, confidence=confidence, evidence=("截图与主体一致",)
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=web_page_structure_facts({"elements": elements})))
    # 用户裁决：低可信详情模型允许独立主体规则兜底，非详情仍保留原阈值。
    fallback_detail = expected in {"content_detail", "video_detail"} and confidence < .7
    assert result.page_type == ("unknown" if confidence < .7 and not fallback_detail else expected)
    if fallback_detail:
        assert result.decision_source == "deterministic"
        assert result.model_result["confidence"] == confidence
    if result.page_type == "unknown":
        assert result.proposed_page_type == "unknown"
        assert result.needs_review is True


@pytest.mark.asyncio
async def test_business_classifier_never_issues_age_gate():
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(page_type="age_gate", confidence=0.99, evidence=("年龄门",))
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture())
    assert result.page_type == "unknown"


@pytest.mark.asyncio
async def test_classifier_infrastructure_failure_is_not_unknown():
    class UnavailableAssessor:
        async def classify(self, **kwargs):
            raise RuntimeError("模型服务不可用")

    with pytest.raises(RuntimeError, match="模型服务不可用"):
        await WebPageClassifier(
            assessor=UnavailableAssessor(), catalog=load_web_page_type_catalog()
        ).classify(_capture())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review",
    [
        {"needs_review": True},
        {"conflicts": ("主体截图与DOM无法消解",)},
    ],
)
async def test_unresolved_model_review_or_conflict_is_formal_unknown(review):
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_list",
                confidence=0.99,
                evidence=("存在重复卡片但视觉仍不明确",),
                **review,
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture())
    assert result.page_type == result.proposed_page_type == "unknown"
    assert result.raw_page_type == result.observed_page_type == "video_list"


async def _classify_ready_content_list(*, model_confidence: float) -> WebPageAssessment:
    return await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_list",
                confidence=model_confidence,
                evidence=("可信 DOM 呈现重复内容卡片",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            facts=WebPageStructureFacts(
                navigation_element_count=1,
                repeated_link_count=3,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=("content-grid", "article-card"),
                primary_detail_link_count=3,
                repeated_primary_card_count=3,
            )
        )
    )


@pytest.mark.asyncio
async def test_classifier_uses_only_ready_primary_evidence_for_page_type() -> None:
    """长图留给质检，页型识别只读取 ready 主证据。"""

    assessor = Assessor(
        WebPageAssessment(
            page_type="video_list",
            confidence=0.91,
            evidence=("主图显示重复视频卡片",),
        )
    )
    capture = _capture()
    capture = WebReadyPageCapture(
        **{
            **capture.__dict__,
            "supplemental_screenshot_artifact_ids": ("artifact-full-page",),
            "supplemental_evidence_status": "captured",
        }
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(capture)

    assert result.page_type == "video_list"
    assert assessor.calls[0]["supplemental_screenshot_artifact_ids"] == ()


@pytest.mark.asyncio
async def test_formal_page_type_default_confidence_boundary_is_seventy_percent() -> None:
    accepted = await _classify_ready_content_list(model_confidence=0.70)
    rejected = await _classify_ready_content_list(model_confidence=0.69)

    assert accepted.page_type == "content_list"
    assert rejected.needs_review_reason == "WEB_PAGE_TYPE_LOW_CONFIDENCE"


@pytest.mark.asyncio
@pytest.mark.parametrize("page_type", [
    "home", "content_list", "content_detail", "video_list", "video_detail",
])
@pytest.mark.parametrize(("confidence", "minimum", "formal"), [
    (0.699, 0.0, False), (0.70, 0.0, True),
    (0.699, 0.70, False), (0.70, 0.70, True), (0.70, 0.80, False),
])
async def test_formal_floor_applies_to_selected_source_not_overwritten_model_score(
    page_type, confidence, minimum, formal
):
    is_home = page_type == "home"
    is_detail = page_type.endswith("detail")
    is_video = page_type.startswith("video")
    facts = WebPageStructureFacts(
        navigation_element_count=3, repeated_link_count=0 if is_detail else 3,
        video_element_count=1 if is_video and is_detail else 0,
        single_subject_element_count=1 if is_detail else 0,
        dom_tokens=("video-player",) if is_video and is_detail else (
            ("article", "post-content") if is_detail else ("video-grid", "content-grid")
        ),
        top_navigation_container_count=1 if is_home else 0,
        visible_category_entry_count=3 if is_home else 0,
        primary_detail_link_count=0 if is_detail else 3,
        repeated_primary_card_count=0 if is_detail else 3,
        media_card_count=3 if is_video and not is_detail else 0,
        article_body_count=1 if is_detail and not is_video else 0,
        paragraph_text_length=400 if is_detail and not is_video else 0,
        player_root_count=1 if is_video and is_detail else 0,
        confirmed_primary_video_resource_count=1 if is_video and is_detail else 0,
    )
    result = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type=page_type, confidence=confidence, evidence=("受控机器必需结构完整",),
        )),
        catalog=load_web_page_type_catalog(), minimum_confidence=minimum,
    ).classify(_capture(
        page_role="home" if is_home else ("detail" if is_detail else "menu"),
        url="https://example.test/" if is_home else "https://example.test/page",
        facts=facts, hint=WebPageClassificationHint(
            expected_page_type=page_type, confidence=0.95,
            source="trusted_list_detail_transition" if is_detail else "runtime_dom_observation",
        ),
    ))
    fallback = is_detail and not formal
    issued = formal or fallback
    assert result.page_type == (page_type if issued else "unknown")
    assert result.confidence == (.9 if fallback else confidence)
    assert result.model_result["confidence"] == confidence
    assert result.decision_source == ("deterministic" if fallback else "model")
    assert result.proposed_page_type == (page_type if issued else "unknown")
    assert result.needs_review is (not issued)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("confidence", "obstructions", "nav_containers", "expected_type"),
    [(0.70, 0, 1, "home"), (0.71, 0, 1, "home"), (0.69, 0, 1, "unknown"),
     (0.70, 1, 1, "unknown"), (0.70, 0, 0, "unknown")],
)
async def test_machine_supported_home_at_formal_threshold_keeps_soft_review_as_audit(
    confidence: float, obstructions: int, nav_containers: int, expected_type: str | None,
) -> None:
    result = await WebPageClassifier(
        assessor=Assessor(WebPageAssessment(
            page_type="home", confidence=confidence,
            evidence=("根页顶部导航与聚合内容同时存在",),
            needs_review_reason="首页同时包含重复视频卡片", needs_review=True,
        )),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(
        page_role="home", url="https://example.test/",
        facts=WebPageStructureFacts(
            navigation_element_count=7 if nav_containers else 0, repeated_link_count=46,
            video_element_count=0, single_subject_element_count=0,
            dom_tokens=("navigation", "content-card"),
            top_navigation_container_count=nav_containers,
            visible_category_entry_count=7 if nav_containers else 0,
            repeated_primary_card_count=46 if nav_containers else 0,
            media_card_count=15, top_level_obstruction_count=obstructions,
        ),
    ))

    assert result.page_type == expected_type
    if expected_type != "unknown":
        assert result.needs_review is False
        assert result.needs_review_reason is None
        assert result.diagnostic_message == "首页同时包含重复视频卡片"
        assert "WEB_PAGE_TYPE_MODEL_REVIEW_CORROBORATED" in result.reason_codes


@pytest.mark.asyncio
async def test_classification_only_accepts_web_catalog_code() -> None:
    assessor = Assessor(
        WebPageAssessment(
            page_type="video_list",
            confidence=0.96,
            evidence=("DOM 有多个重复视频卡片", "卡片包含播放器详情链接"),
        )
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(_capture())

    assert result.page_type == "video_list"
    assert result.guide_version == load_web_page_type_catalog().version
    assert result.needs_review is False
    assert result.reason_codes == ()
    assert assessor.calls[0]["allowed_page_types"] == (
        "home",
        "content_list",
        "content_detail",
        "video_list",
        "video_detail",
        "activity", "search_page", "collection_page", "recharge_page",
        "topup_page", "unknown",
    )
    assert [item["code"] for item in assessor.calls[0]["page_type_catalog"]] == [
        "home",
        "content_list",
        "content_detail",
        "video_list",
        "video_detail",
        "activity", "search_page", "collection_page", "recharge_page",
        "topup_page", "unknown",
    ]
    public_video_list = assessor.calls[0]["page_type_catalog"][3]
    assert public_video_list["purpose"]
    assert public_video_list["required_positive_signals"]
    navigation_signals = assessor.calls[0]["structure_facts"]["navigation_signals"]
    breadcrumb_signal = navigation_signals["distinguish_from_breadcrumb"]["breadcrumb"][0]
    assert breadcrumb_signal.startswith("呈线性路径")


@pytest.mark.parametrize(
    ("page_role", "page_type", "facts", "expected_allowed"),
    (
        (
            "home",
            "content_list",
            WebPageStructureFacts(
                navigation_element_count=1,
                repeated_link_count=3,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=("nav", "content-grid", "article-card"),
                primary_detail_link_count=3,
                repeated_primary_card_count=3,
            ),
            (
                "home",
                "content_list",
                "content_detail",
                "video_list",
                "video_detail",
                "activity",
                "search_page",
                "collection_page",
                "recharge_page",
                "topup_page",
                "unknown",
            ),
        ),
        (
            "detail",
            "content_detail",
            WebPageStructureFacts(
                navigation_element_count=20,
                repeated_link_count=0,
                video_element_count=0,
                single_subject_element_count=2,
                dom_tokens=("article", "post-content"),
                article_body_count=1,
                paragraph_text_length=400,
            ),
            (
                "home",
                "content_list",
                "content_detail",
                "video_list",
                "video_detail",
                "activity",
                "search_page",
                "collection_page",
                "recharge_page",
                "topup_page",
                "unknown",
            ),
        ),
    ),
)
@pytest.mark.asyncio
async def test_page_role_limits_model_page_type_candidates(
    page_role: str,
    page_type: str,
    facts: WebPageStructureFacts,
    expected_allowed: tuple[str, ...],
) -> None:
    assessor = Assessor(
        WebPageAssessment(
            page_type=page_type,
            confidence=0.98,
            evidence=("角色闭集内的可信结构",),
        )
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role=page_role, facts=facts))

    assert result.page_type == page_type
    assert assessor.calls[0]["allowed_page_types"] == expected_allowed
    assert (
        tuple(item["code"] for item in assessor.calls[0]["page_type_catalog"]) == expected_allowed
    )


@pytest.mark.asyncio
async def test_detail_probe_can_recognize_list_mismatch_without_manual_review() -> None:
    """详情动作落回列表时应保留真实页型，由导航器记录 mismatch。"""

    assessor = Assessor(
        WebPageAssessment(
            page_type="video_list",
            confidence=0.98,
            evidence=("页面主体是重复媒体卡片而非单主体详情",),
        )
    )

    facts = WebPageStructureFacts(
        navigation_element_count=4,
        repeated_link_count=12,
        video_element_count=0,
        single_subject_element_count=0,
        dom_tokens=("video-img-box", "media", "card"),
        primary_detail_link_count=12,
        repeated_primary_card_count=12,
        media_card_count=12,
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="detail", facts=facts))

    assert result.page_type == "video_list"
    assert result.needs_review_reason is None
    assert assessor.calls[0]["allowed_page_types"] == (
        "home",
        "content_list",
        "content_detail",
        "video_list",
        "video_detail",
        "activity", "search_page", "collection_page", "recharge_page",
        "topup_page", "unknown",
    )


def test_bare_origin_capture_is_home_even_when_caller_declares_menu() -> None:
    bare_origin = _capture(page_role="menu", url="https://example.com")
    nested_page = _capture(page_role="menu", url="https://example.com/channel")

    assert bare_origin.page_role == "home"
    assert nested_page.page_role == "menu"


@pytest.mark.parametrize("page_type", ["web-home", "login"])
@pytest.mark.asyncio
async def test_out_of_catalog_result_becomes_needs_review(page_type: str) -> None:
    assessor = Assessor(
        WebPageAssessment(
            page_type=page_type,
            confidence=0.99,
            evidence=("模型返回目录外类型",),
        )
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(_capture())

    assert result.page_type == "unknown"
    assert result.raw_page_type == page_type
    assert result.needs_review_reason == "WEB_PAGE_TYPE_UNSUPPORTED"


@pytest.mark.asyncio
async def test_non_ready_capture_is_rejected_before_model_call() -> None:
    assessor = Assessor(
        WebPageAssessment(
            page_type="content_list",
            confidence=0.99,
            evidence=("重复内容卡片",),
        )
    )

    with pytest.raises(ValueError, match="无遮挡基线"):
        await WebPageClassifier(
            assessor=assessor,
            catalog=load_web_page_type_catalog(),
        ).classify(_capture(stage="web_page_probe"))

    assert assessor.calls == []


@pytest.mark.asyncio
async def test_primary_content_supplement_is_accepted_as_classification_evidence() -> None:
    assessor = Assessor(
        WebPageAssessment(
            page_type="content_list",
            confidence=0.99,
            evidence=("主体区域存在重复文章卡片",),
        )
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            stage="web_primary_content_evidence",
            capture_purpose="web_primary_content_evidence",
            facts=WebPageStructureFacts(
                navigation_element_count=1,
                repeated_link_count=3,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=("content-grid", "article-card"),
                primary_detail_link_count=3,
                repeated_primary_card_count=3,
            ),
        )
    )

    assert result.page_type == "content_list"
    assert len(assessor.calls) == 1


@pytest.mark.asyncio
async def test_low_confidence_or_missing_video_structure_needs_review() -> None:
    low_confidence = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_detail",
                confidence=0.42,
                evidence=("单一正文主体",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture())
    weak_video = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_detail",
                confidence=0.99,
                evidence=("URL 包含 video",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            facts=WebPageStructureFacts(
                navigation_element_count=0,
                repeated_link_count=0,
                video_element_count=0,
                single_subject_element_count=1,
                dom_tokens=("article-body",),
            )
        )
    )

    assert low_confidence.needs_review_reason == "WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT"
    assert low_confidence.raw_page_type == "content_detail"
    assert weak_video.needs_review_reason == "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"
    assert weak_video.raw_page_type == "video_detail"


@pytest.mark.asyncio
async def test_verified_hint_cannot_supply_missing_video_structure() -> None:
    """白名单提示只补足视频/内容轴，不能替代重复列表 DOM 骨架。"""

    hint = WebPageClassificationHint(
        expected_page_type="video_list",
        confidence=0.86,
        source="operator_verified_menu_classification",
    )
    repeated_list = WebPageStructureFacts(
        navigation_element_count=4,
        repeated_link_count=6,
        video_element_count=0,
        single_subject_element_count=1,
        dom_tokens=("topic-card", "topic-grid"),
        primary_detail_link_count=6,
        repeated_primary_card_count=6,
    )
    accepted = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_list",
                confidence=0.65,
                evidence=("页面主体为重复专题卡片",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=repeated_list, hint=hint))
    rejected = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_list",
                confidence=0.75,
                evidence=("页面只有菜单文案",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            facts=WebPageStructureFacts(
                navigation_element_count=6,
                repeated_link_count=0,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=("nav", "topic"),
            ),
            hint=hint,
        )
    )

    assert accepted.page_type == "unknown"
    assert accepted.confidence == 0.65
    assert rejected.page_type == "unknown"
    assert rejected.needs_review_reason == "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"


@pytest.mark.asyncio
async def test_detail_transition_hint_corroborates_landing_but_never_overrides_dom() -> None:
    """可信列表到详情的转换提示可佐证落地页，但不能覆盖实际返回的列表。"""

    hint = WebPageClassificationHint(
        expected_page_type="video_detail",
        confidence=0.8,
        source="trusted_list_detail_transition",
    )
    detail = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_detail",
                confidence=0.75,
                evidence=("存在播放入口、分集和单一剧集主体",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            page_role="detail",
            hint=hint,
            facts=WebPageStructureFacts(
                navigation_element_count=4,
                repeated_link_count=8,
                video_element_count=0,
                single_subject_element_count=1,
                dom_tokens=("hg-web-detail", "hg-web-detail__play", "episode-grid"),
                player_root_count=1,
                confirmed_primary_video_resource_count=1,
            ),
        )
    )
    returned_list = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_list",
                confidence=0.92,
                evidence=("落地后仍为重复视频卡片列表",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            page_role="detail",
            hint=hint,
            facts=WebPageStructureFacts(
                navigation_element_count=4,
                repeated_link_count=6,
                video_element_count=0,
                single_subject_element_count=0,
                dom_tokens=("video-grid", "drama-card"),
                primary_detail_link_count=6,
                repeated_primary_card_count=6,
                media_card_count=6,
            ),
        )
    )

    assert detail.page_type == "video_detail"
    assert returned_list.page_type == "video_list"
    assert returned_list.needs_review_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("confidence", [0.60, 0.65, 0.699, 0.70])
async def test_detail_transition_cannot_clear_explicit_model_review(confidence) -> None:
    """用户裁决：即使详情来源可信，也不能以兜底清除模型显式复核。"""

    hint = WebPageClassificationHint(
        expected_page_type="video_detail",
        confidence=0.8,
        source="trusted_list_detail_transition",
    )
    facts = WebPageStructureFacts(
        navigation_element_count=2,
        repeated_link_count=7,
        video_element_count=0,
        single_subject_element_count=1,
        dom_tokens=("hg-web-detail", "hg-web-detail__play", "episode-grid"),
        primary_detail_link_count=7,
        repeated_primary_card_count=7,
        media_card_count=7,
        player_root_count=2,
        confirmed_primary_video_resource_count=1,
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_detail",
                confidence=confidence,
                evidence=("播放器根节点和剧集选择均存在",),
                needs_review_reason="video_element_count=0 but player_root_count=2",
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="detail", facts=facts, hint=hint))

    assert result.confidence == confidence
    assert result.proposed_page_type == "unknown"
    assert result.page_type == "unknown"
    assert result.needs_review is True
    assert result.needs_review_reason == "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"


@pytest.mark.asyncio
async def test_model_review_reason_stays_without_matching_detail_transition() -> None:
    facts = WebPageStructureFacts(
        navigation_element_count=2,
        repeated_link_count=7,
        video_element_count=0,
        single_subject_element_count=1,
        dom_tokens=("hg-web-detail", "hg-web-detail__play", "episode-grid"),
        primary_detail_link_count=7,
        repeated_primary_card_count=7,
        media_card_count=7,
        player_root_count=2,
        confirmed_primary_video_resource_count=1,
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_detail",
                confidence=0.65,
                evidence=("播放器根节点和剧集选择均存在",),
                needs_review_reason="模型仍不确定",
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="detail", facts=facts))

    assert result.page_type == "unknown"
    assert result.needs_review_reason == "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"
    assert result.diagnostic_message == "模型仍不确定"


@pytest.mark.asyncio
async def test_strong_home_list_dom_resolves_mixed_content_without_url_hint() -> None:
    """首页可由强主体列表 DOM 消歧，不要求把综合首页写进 URL 白名单。"""

    facts = WebPageStructureFacts(
        navigation_element_count=8,
        repeated_link_count=76,
        video_element_count=0,
        single_subject_element_count=1,
        dom_tokens=("hg-drama-card", "hg-card-grid", "hg-hero"),
        primary_detail_link_count=19,
        repeated_primary_card_count=19,
        media_card_count=12,
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_list",
                confidence=0.78,
                evidence=("重复短剧卡片构成页面主体",),
                needs_review_reason="mixed_content",
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="home", facts=facts))

    assert result.page_type == "video_list"
    assert result.needs_review_reason is None


@pytest.mark.asyncio
async def test_home_page_type_uses_root_url_and_strong_aggregate_dom_to_corroborate_visual() -> (
    None
):
    """正式 home 需截图、根 URL 与聚合 DOM，不依赖站点详情 URL 模式。"""

    facts = WebPageStructureFacts(
        navigation_element_count=8,
        repeated_link_count=2,
        video_element_count=0,
        single_subject_element_count=0,
        dom_tokens=("main-navigation", "section", "content-card"),
        primary_detail_link_count=0,
        repeated_primary_card_count=8,
        top_navigation_container_count=1,
        visible_category_entry_count=6,
        homepage_section_count=3,
        is_canonical_root_url=True,
    )
    assessor = Assessor(
        WebPageAssessment(
            page_type="home",
            confidence=0.75,
            evidence=("截图呈现首页聚合布局", "DOM 存在主导航、多个板块与并列卡片"),
        )
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="home", url="https://example.com/", facts=facts))

    assert result.page_type == "home"
    assert result.needs_review is False
    assert assessor.calls[0]["screenshot_artifact_id"] == "artifact-screenshot"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facts",
    (
        WebPageStructureFacts(
            navigation_element_count=0,
            repeated_link_count=2,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("root",),
            repeated_primary_card_count=2,
            is_canonical_root_url=True,
        ),
        WebPageStructureFacts(
            navigation_element_count=8,
            repeated_link_count=24,
            video_element_count=0,
            single_subject_element_count=0,
            dom_tokens=("main-navigation", "section", "content-card"),
            primary_detail_link_count=8,
            repeated_primary_card_count=8,
            top_navigation_container_count=1,
            visible_category_entry_count=6,
            homepage_section_count=3,
            is_canonical_root_url=False,
        ),
    ),
)
async def test_home_page_type_does_not_promote_root_or_dom_evidence_alone(
    facts: WebPageStructureFacts,
) -> None:
    """裸域名或聚合 DOM 单独成立都不是正式 home 的充分条件。"""

    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="home",
                confidence=0.69,
                evidence=("截图候选为首页",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="home", facts=facts))

    assert result.page_type == "unknown"
    assert result.needs_review is True


@pytest.mark.asyncio
async def test_home_dom_corroboration_rejects_weak_or_non_home_evidence() -> None:
    """DOM 独立消歧只服务首页强列表骨架，弱证据和普通菜单页仍需复核。"""

    strong_list = WebPageStructureFacts(
        navigation_element_count=5,
        repeated_link_count=8,
        video_element_count=0,
        single_subject_element_count=0,
        dom_tokens=("drama-card", "drama-grid"),
        primary_detail_link_count=8,
        repeated_primary_card_count=8,
        media_card_count=6,
    )
    weak_list = WebPageStructureFacts(
        navigation_element_count=5,
        repeated_link_count=2,
        video_element_count=0,
        single_subject_element_count=0,
        dom_tokens=("drama-card",),
        primary_detail_link_count=2,
        repeated_primary_card_count=2,
        media_card_count=1,
    )

    async def classify(*, role: str, facts: WebPageStructureFacts) -> WebPageAssessment:
        return await WebPageClassifier(
            assessor=Assessor(
                WebPageAssessment(
                    page_type="video_list",
                    confidence=0.78,
                    evidence=("页面可能混合",),
                    needs_review_reason="mixed_content",
                )
            ),
            catalog=load_web_page_type_catalog(),
        ).classify(_capture(page_role=role, facts=facts))

    weak_home = await classify(role="home", facts=weak_list)
    strong_menu = await classify(role="menu", facts=strong_list)

    assert weak_home.page_type == "unknown"
    assert weak_home.needs_review_reason == "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"
    assert weak_home.diagnostic_message == "mixed_content"
    assert strong_menu.page_type == "unknown"
    assert strong_menu.needs_review_reason == "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"
    assert strong_menu.diagnostic_message == "mixed_content"


def _home_video_feed_facts() -> WebPageStructureFacts:
    return WebPageStructureFacts(
        navigation_element_count=7,
        repeated_link_count=18,
        video_element_count=2,
        single_subject_element_count=2,
        dom_tokens=("video-feed",),
        primary_detail_link_count=10,
        repeated_primary_card_count=10,
        player_root_count=2,
        continuous_video_feed_count=2,
        is_canonical_root_url=True,
        active_home_navigation=True,
        top_navigation_container_count=1,
        visible_category_entry_count=7,
        subject_region_facts=WebSubjectRegionFacts(
            primary_subject_count=1,
            primary_subject_kind="video",
            primary_player_count=1,
            repeated_primary_card_count=12,
        ),
    )


def _saved_short_video_home_facts(*, historical_cards: bool = False) -> WebPageStructureFacts:
    """两份根页 ready 快照的脱敏结构事实，仅历史主卡计数不同。"""

    card_count = 13 if historical_cards else 0
    return WebPageStructureFacts(
        navigation_element_count=17,
        repeated_link_count=19,
        video_element_count=2,
        single_subject_element_count=2,
        dom_tokens=("video-feed",),
        primary_detail_link_count=13 if historical_cards else 10,
        repeated_primary_card_count=13 if historical_cards else 10,
        player_root_count=2,
        continuous_video_feed_count=2,
        is_canonical_root_url=True,
        active_home_navigation=True,
        top_navigation_container_count=1,
        visible_category_entry_count=7,
        subject_region_facts=WebSubjectRegionFacts(
            primary_subject_count=1,
            primary_subject_kind="video",
            primary_player_count=1,
            repeated_primary_card_count=card_count,
        ),
    )


def _stable_home_capture(
    facts: WebPageStructureFacts, *, url: str = "https://example.test/"
) -> WebReadyPageCapture:
    return replace(
        _capture(page_role="home", url=url, facts=facts),
        inspection_readiness=WebSettleAssessment(
            role=WebEvidenceRole.CLASSIFICATION,
            status=WebReadinessStatus.STABLE,
            blocking_reasons=(),
        ),
    )


@pytest.mark.asyncio
async def test_saved_short_video_root_without_primary_cards_is_home() -> None:
    facts = _saved_short_video_home_facts()
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="home", confidence=0.65, evidence=("首页导航与视频流",),
                needs_review=True,
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_stable_home_capture(facts))

    assert result.page_type == "home"
    assert result.needs_review is False
    assert result.decision_source == "deterministic"
    assert result.model_result["page_type"] == "home"
    assert result.model_result["needs_review"] is True
    assert result.deterministic_result["page_type"] == "home"


@pytest.mark.asyncio
async def test_saved_historical_short_video_root_stays_home() -> None:
    facts = _saved_short_video_home_facts(historical_cards=True)
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="home", confidence=0.8, evidence=("首页导航与视频流",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_stable_home_capture(facts))

    assert result.page_type == "home"


@pytest.mark.asyncio
async def test_root_video_detail_with_recommendation_links_is_not_home() -> None:
    """根路径 SPA 详情的推荐链接不能冒充连续视频信息流。"""

    facts = replace(
        _saved_short_video_home_facts(),
        video_element_count=1,
        player_root_count=1,
        continuous_video_feed_count=0,
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="home", confidence=0.65, evidence=("根页与推荐入口",),
                needs_review=True,
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_stable_home_capture(facts))

    assert result.page_type != "home"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "inactive_home", "too_few_categories", "too_few_links", "off_root",
        "obstructed", "unsettled", "multiple_primary_players", "confident_detail",
        "conflicting_hint",
    ),
)
async def test_short_video_home_rejects_missing_independent_proof(case: str) -> None:
    facts = _saved_short_video_home_facts()
    capture = _stable_home_capture(facts)
    model_type = "home"
    confidence = 0.65
    needs_review = True
    if case == "inactive_home":
        facts = replace(facts, active_home_navigation=False)
    elif case == "too_few_categories":
        facts = replace(facts, visible_category_entry_count=3)
    elif case == "too_few_links":
        facts = replace(facts, primary_detail_link_count=3, repeated_link_count=3)
    elif case == "off_root":
        capture = _stable_home_capture(facts, url="https://example.test/video/1")
        facts = replace(facts, is_canonical_root_url=False)
    elif case == "obstructed":
        facts = replace(facts, top_level_obstruction_count=1)
    elif case == "unsettled":
        capture = replace(
            capture,
            inspection_readiness=WebSettleAssessment(
                role=WebEvidenceRole.CLASSIFICATION,
                status=WebReadinessStatus.LOADING,
                blocking_reasons=("dom_quiet",),
            ),
        )
    elif case == "multiple_primary_players":
        facts = replace(
            facts,
            subject_region_facts=replace(facts.subject_region_facts, primary_player_count=2),
        )
    elif case == "confident_detail":
        model_type, confidence, needs_review = "video_detail", 0.95, False
    elif case == "conflicting_hint":
        capture = replace(
            capture,
            classification_hint=WebPageClassificationHint(
                expected_page_type="video_detail", confidence=0.9,
                source="runtime_dom_observation",
            ),
        )
    capture = replace(capture, structure_facts=facts)
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type=model_type, confidence=confidence,
                evidence=("首页导航与视频流",), needs_review=needs_review,
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(capture)

    assert result.page_type != "home"


@pytest.mark.asyncio
async def test_weak_video_detail_conflicting_with_active_root_feed_is_home() -> None:
    """低分单播放器判断遇到可信首页导航和重复候选时保留模型审计。"""

    facts = _home_video_feed_facts()
    assessor = Assessor(
        WebPageAssessment(
            page_type="video_detail",
            confidence=0.55,
            evidence=("首屏有一个正在播放的视频",),
            needs_review=True,
        )
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="home", url="https://example.test/", facts=facts))

    assert result.page_type == "home"
    assert result.confidence >= 0.7
    assert result.needs_review is False
    assert result.decision_source == "deterministic"
    assert result.model_result["page_type"] == "video_detail"
    assert result.model_result["confidence"] == 0.55
    assert result.deterministic_result["page_type"] == "home"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    (
        "inactive_home",
        "too_few_cards",
        "off_root",
        "obstructed",
        "no_detail_conflict",
        "confident_model_detail",
        "conflicting_hint",
    ),
)
async def test_home_video_feed_fallback_rejects_missing_independent_evidence(
    case: str,
) -> None:
    facts = _home_video_feed_facts()
    url = "https://example.test/"
    confidence = 0.55
    hint = None
    if case == "inactive_home":
        facts = replace(facts, active_home_navigation=False)
    elif case == "too_few_cards":
        facts = replace(facts, repeated_primary_card_count=5, primary_detail_link_count=5)
    elif case == "off_root":
        url = "https://example.test/video/1"
        facts = replace(facts, is_canonical_root_url=False)
    elif case == "obstructed":
        facts = replace(facts, top_level_obstruction_count=1)
    elif case == "no_detail_conflict":
        facts = replace(
            facts,
            subject_region_facts=replace(facts.subject_region_facts, repeated_primary_card_count=0),
        )
    elif case == "confident_model_detail":
        confidence = 0.95
    elif case == "conflicting_hint":
        hint = WebPageClassificationHint(
            expected_page_type="video_detail",
            confidence=0.9,
            source="runtime_dom_observation",
        )

    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_detail",
                confidence=confidence,
                evidence=("首屏有播放器",),
                needs_review=True,
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="home", url=url, facts=facts, hint=hint))

    assert result.page_type != "home"


@pytest.mark.asyncio
async def test_active_home_navigation_does_not_sign_single_video_without_feed() -> None:
    """选中的首页导航不能把只有单个播放器的详情冒充首页。"""

    facts = WebPageStructureFacts(
        navigation_element_count=3,
        repeated_link_count=1,
        video_element_count=1,
        single_subject_element_count=1,
        dom_tokens=("video-player",),
        player_root_count=1,
        is_canonical_root_url=True,
        active_home_navigation=True,
        top_navigation_container_count=1,
        visible_category_entry_count=3,
        subject_region_facts=WebSubjectRegionFacts(
            primary_subject_count=1,
            primary_subject_kind="unknown",
            primary_player_count=0,
        ),
    )

    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="home",
                confidence=0.95,
                evidence=("选中的首页导航与单个播放器",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="home", url="https://example.test/", facts=facts))

    assert result.page_type == "unknown"
    assert result.needs_review_reason == "WEB_PAGE_TYPE_STRONG_NEGATIVE_CONFLICT"


@pytest.mark.asyncio
async def test_model_written_structure_cannot_replace_trusted_dom_facts() -> None:
    assessor = Assessor(
        WebPageAssessment(
            page_type="video_list",
            confidence=0.99,
            evidence=("模型声称页面有多个视频卡片和播放器",),
        )
    )
    facts = WebPageStructureFacts(
        navigation_element_count=0,
        repeated_link_count=0,
        video_element_count=0,
        single_subject_element_count=0,
        dom_tokens=("plain",),
    )

    result = await WebPageClassifier(
        assessor=assessor,
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=facts))

    assert result.page_type == "unknown"
    assert result.needs_review_reason == "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"


def test_audio_source_does_not_count_as_video_structure() -> None:
    facts = web_page_structure_facts(
        {
            "elements": [
                {"tag": "audio", "visible": True},
                {
                    "tag": "source",
                    "media_parent_tag": "audio",
                    "visible": True,
                },
                {"tag": "video", "visible": True},
                {
                    "tag": "source",
                    "media_parent_tag": "video",
                    "visible": True,
                },
            ]
        }
    )

    assert facts.video_element_count == 2
    assert facts.audio_element_count == 2


def _captured_content_links(*, web_navigation, legacy_navigation):
    """保留1a545dc7的30个正文节点标记组合，链接与文案改为中性测试值。"""
    elements = []
    for index in range(30):
        item = {
            "tag": "a",
            "role": "link",
            "visible": True,
            "interactive": True,
            "selector": f".post-card:nth-child({index + 1}) a",
            "href": f"/archives/{1000 + index}/",
            "text": f"文章 {index + 1}",
            "ancestor_class_tokens": ["post-card", "post-list"],
            "within_repeated_structure": True,
            "repeat_group_size": 30,
            "within_navigation": legacy_navigation,
            "within_pagination": False,
            "is_advertisement": False,
            "rect": {"x": 100, "y": 50 + index * 24, "width": 300, "height": 20},
        }
        if web_navigation is not None:
            item["within_web_navigation"] = web_navigation
        elements.append(item)
    return {"elements": elements}


@pytest.mark.parametrize(
    "web_navigation,legacy_navigation,expected",
    [
        (False, True, 30),
        (True, False, 0),
        (None, True, 0),
        (None, False, 30),
        ("false", True, 0),
    ],
)
def test_body_link_count_uses_same_navigation_precedence_as_detail_candidates(
    web_navigation,
    legacy_navigation,
    expected,
):
    facts = web_page_structure_facts(
        _captured_content_links(web_navigation=web_navigation, legacy_navigation=legacy_navigation),
        origin="https://example.test",
        current_url="https://example.test/list",
    )
    assert facts.primary_detail_link_count == expected
    assert facts.repeated_link_count == expected
    assert facts.navigation_element_count == (30 - expected)


@pytest.mark.asyncio
async def test_real_capture_navigation_flag_combination_allows_formal_content_list():
    facts = web_page_structure_facts(
        _captured_content_links(web_navigation=False, legacy_navigation=True),
        origin="https://example.test",
        current_url="https://example.test/list",
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_list",
                confidence=0.75,
                evidence=("重复正文卡片与详情入口",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=facts))
    assert result.page_type == "content_list"


def test_playback_landing_controls_count_as_video_structure() -> None:
    facts = web_page_structure_facts(
        {
            "elements": [
                {
                    "selector": ".player-root",
                    "tag": "button",
                    "visible": True,
                    "class_tokens": ["player-root"],
                },
                {
                    "selector": ".episode-grid",
                    "tag": "div",
                    "visible": True,
                    "class_tokens": ["episode-grid"],
                },
            ]
        }
    )

    assert facts.player_root_count == 2


@pytest.mark.asyncio
async def test_drama_cards_without_video_are_signed_as_video_list() -> None:
    elements = []
    for index in range(3):
        elements.append(
            {
                "selector": f"#drama-{index} a",
                "tag": "a",
                "role": "link",
                "text": f"剧集 {index}",
                "href": f"/detail/{3690 + index}/",
                "visible": True,
                "interactive": True,
                "within_repeated_structure": True,
                "repeat_group_size": 3,
                "within_web_navigation": False,
                "ancestor_class_tokens": ["hg-drama-card", "drama-grid"],
                "rect": {
                    "x": 100 + index * 320,
                    "y": 220,
                    "width": 300,
                    "height": 180,
                },
            }
        )
    facts = web_page_structure_facts(
        {"elements": elements},
        origin="https://example.test",
        current_url="https://example.test/drama",
    )

    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="video_list",
                confidence=0.98,
                evidence=("多个重复剧集封面卡片",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=facts))

    assert facts.video_element_count == 0
    assert facts.primary_detail_link_count == 3
    assert facts.repeated_primary_card_count == 3
    assert facts.media_card_count == 3
    assert result.page_type == "video_list"


def test_cross_origin_noise_and_current_page_do_not_count_as_primary_details() -> None:
    elements = [
        {
            "selector": f"#card-{index} a",
            "tag": "a",
            "role": "link",
            "text": f"外站内容 {index}",
            "href": f"https://evil.test/detail/{index}",
            "visible": True,
            "interactive": True,
            "within_repeated_structure": True,
            "repeat_group_size": 4,
            "ancestor_class_tokens": ["content-card", "content-grid"],
        }
        for index in range(2)
    ]
    elements.extend(
        [
            {
                **elements[0],
                "selector": "#current-link",
                "text": "当前列表",
                "href": "/list",
            },
            {
                **elements[0],
                "selector": "#download-link",
                "text": "下载",
                "href": "/download/client",
                "ancestor_class_tokens": ["download", "content-card"],
            },
        ]
    )

    facts = web_page_structure_facts(
        {"elements": elements},
        origin="https://example.test",
        current_url="https://example.test/list",
    )

    assert facts.primary_detail_link_count == 0
    assert facts.repeated_primary_card_count == 0
    assert facts.media_card_count == 0


@pytest.mark.asyncio
async def test_direct_content_detail_menu_uses_single_subject_structure() -> None:
    facts = web_page_structure_facts(
        {
            "elements": [
                {
                    "selector": "main",
                    "tag": "main",
                    "visible": True,
                    "class_tokens": ["detail-content"],
                },
                {
                    "selector": "main article",
                    "tag": "article",
                    "visible": True,
                    "class_tokens": ["article-body"],
                    "text": "正文" * 160,
                },
            ]
        }
    )

    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_detail",
                confidence=0.98,
                evidence=("单一正文主体",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="menu", facts=facts))

    assert facts.single_subject_element_count == 2
    assert facts.primary_detail_link_count == 0
    assert result.page_type == "content_detail"


@pytest.mark.asyncio
async def test_content_detail_id_is_trusted_single_subject_structure() -> None:
    facts = web_page_structure_facts(
        {
            "elements": [
                {
                    "selector": "#detail-content",
                    "tag": "div",
                    "visible": True,
                    "class_tokens": ["markdown-body", "editormd-html-preview"],
                    "ancestor_class_tokens": ["text-content", "site-content"],
                    "text": "正文" * 160,
                },
                {"selector": "#detail-content > p", "tag": "p", "visible": True,
                 "parent_selector": "#detail-content", "text": "正文" * 160},
            ]
        }
    )

    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_detail",
                confidence=0.95,
                evidence=("正文根节点为 #detail-content",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(page_role="detail", facts=facts))

    assert facts.single_subject_element_count == 1
    assert "#detail-content" in facts.dom_tokens
    assert result.page_type == "content_detail"


@pytest.mark.asyncio
async def test_navigation_tags_and_popup_addresses_cannot_form_content_list() -> None:
    facts = web_page_structure_facts(
        {
            "elements": [
                {
                    "selector": "nav a.tag",
                    "tag": "a",
                    "role": "link",
                    "text": "标签",
                    "href": "/tag/hot",
                    "visible": True,
                    "interactive": True,
                    "within_repeated_structure": True,
                    "within_web_navigation": True,
                    "ancestor_class_tokens": ["navbar-nav", "tag-list"],
                },
                {
                    "selector": "#tip_modal a.address",
                    "tag": "a",
                    "role": "link",
                    "text": "备用网址",
                    "href": "/address/1",
                    "visible": True,
                    "interactive": True,
                    "within_repeated_structure": True,
                    "web_obstruction_layer": "#tip_modal",
                    "ancestor_class_tokens": ["modal", "address-list", "card"],
                },
            ]
        }
    )

    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_list",
                confidence=0.99,
                evidence=("模型误认为重复链接是正文列表",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(_capture(facts=facts))

    assert facts.primary_detail_link_count == 0
    assert facts.repeated_primary_card_count == 0
    assert result.page_type == "unknown"
    assert result.needs_review_reason == "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"


@pytest.mark.asyncio
async def test_high_confidence_dom_conflict_keeps_observed_type_for_inspection() -> None:
    facts = WebPageStructureFacts(
        navigation_element_count=1,
        repeated_link_count=0,
        video_element_count=0,
        single_subject_element_count=1,
        dom_tokens=("article", "article-body"),
        article_body_count=1,
        paragraph_text_length=420,
    )
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_detail",
                confidence=0.96,
                evidence=("DOM 是单一长正文主体",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            page_role="menu",
            facts=facts,
            hint=WebPageClassificationHint(
                expected_page_type="video_list",
                confidence=0.9,
                source="operator_verified_menu_classification",
            ),
        )
    )

    assert result.page_type == "content_detail"
    assert result.raw_page_type == "content_detail"
    assert result.proposed_page_type == "content_detail"
    assert result.observed_page_type == "content_detail"
    assert result.expected_page_type == "video_list"
    assert result.conflict_reason_code == "URL_HINT_DOM_CONFLICT"
    assert result.needs_review_reason is None


@pytest.mark.asyncio
async def test_model_detail_axis_wins_but_opposing_structure_is_retained_for_audit() -> None:
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_detail",
                confidence=0.97,
                evidence=("页面同时存在文章容器和主播放器",),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            page_role="detail",
            facts=WebPageStructureFacts(
                navigation_element_count=1,
                repeated_link_count=0,
                video_element_count=1,
                single_subject_element_count=1,
                dom_tokens=("article", "article-body", "video-player"),
                article_body_count=1,
                paragraph_text_length=140,
                subject_region_facts=WebSubjectRegionFacts(
                    primary_subject_count=1, primary_subject_kind="video", primary_player_count=1,
                    primary_article_body_count=1, primary_article_text_length=140,
                ),
                player_root_count=1,
                confirmed_primary_video_resource_count=1,
            ),
        )
    )

    assert result.page_type == "content_detail"
    assert result.decision_source == "model"
    assert result.needs_review is False
    assert result.deterministic_result["page_type"] == "video_detail"
    assert "primary_video_conflicts_with_content_detail" in (
        result.deterministic_result["rules"]["content_detail"]["strong_conflicts"]
    )


@pytest.mark.asyncio
async def test_low_confidence_model_reason_is_diagnostic_not_error_code() -> None:
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(
                page_type="content_detail",
                confidence=0.42,
                evidence=("正文主体可能存在",),
                needs_review_reason=("model uncertain token=secret-value " + "x" * 600),
            )
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(
        _capture(
            facts=WebPageStructureFacts(
                navigation_element_count=1,
                repeated_link_count=0,
                video_element_count=0,
                single_subject_element_count=1,
                dom_tokens=("article", "article-body"),
                article_body_count=1,
                paragraph_text_length=420,
            )
        )
    )

    assert result.page_type == "unknown"
    assert result.proposed_page_type == "unknown"
    assert result.needs_review_reason == "WEB_PAGE_TYPE_MODEL_NEEDS_REVIEW"
    assert "secret-value" not in (result.diagnostic_message or "")
    assert len(result.diagnostic_message or "") <= 300
