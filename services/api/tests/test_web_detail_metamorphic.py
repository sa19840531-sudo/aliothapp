"""详情判定、审计与结构簇不依赖站点身份。"""

from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime
from random import Random

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    web_page_structure_facts,
)
from services.api.app.domain.page import load_web_page_type_catalog
from services.api.tests.test_web_page_classification import Assessor, _capture
from services.api.tests.test_web_page_type_catalog import (
    frozen_v5_to_v7_catalog_payload as frozen_v5_to_v7_catalog_payload,
)
from services.api.tests.web_subject_fixture_support import load_fixture, snapshot_from_html

CASES = [
    ("video_primary_with_recommendations.html", "video_detail"),
    ("article_with_inline_video.html", "content_detail"),
    ("gallery_primary.html", "content_detail"),
    ("list_with_repeated_primary_cards.html", "content_list"),
    ("vendor_neutral_iframe_player.html", "video_detail"),
]


async def classify_snapshot(snapshot, expected, url="https://sample.example/subject"):
    capture = _capture(facts=web_page_structure_facts(snapshot), url=url)
    result = await WebPageClassifier(
        assessor=Assessor(
            WebPageAssessment(page_type=expected, confidence=0.99, evidence=("主体证据",))
        ),
        catalog=load_web_page_type_catalog(),
    ).classify(capture)
    return result, capture


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,expected", CASES)
async def test_page_type_uses_primary_region_not_total_card_count(fixture, expected):
    result, _ = await classify_snapshot(load_fixture(fixture), expected)
    assert result.page_type == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot,reason",
    [
        (load_fixture("conflicting_primary_regions.html"), "WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT"),
        (snapshot_from_html("<main><h1>空页面</h1></main>"), "WEB_PAGE_TYPE_EVIDENCE_INSUFFICIENT"),
    ],
)
async def test_empty_or_conflicting_primary_region_remains_unknown(snapshot, reason):
    result, _ = await classify_snapshot(snapshot, "video_detail")
    assert result.page_type == "unknown"
    assert reason in result.reason_codes


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,expected", CASES)
async def test_detail_classification_is_invariant_under_site_identity_mutation(fixture, expected):
    original = load_fixture(fixture)
    first, _ = await classify_snapshot(original, expected)
    random = Random(83)
    for _ in range(4):
        mutated = deepcopy(original)
        nonce = str(random.randrange(10**8))
        selectors = {
            node["selector"]: "#node" + str(i) + nonce for i, node in enumerate(mutated["elements"])
        }
        for node in mutated["elements"]:
            node["selector"] = selectors[node["selector"]]
            node["parent_selector"] = selectors.get(node["parent_selector"], "")
            node["class_tokens"].append("layout-" + nonce)
            if node["tag"] == "h1":
                node["text"] = "新主题" + nonce
            if node.get("href"):
                node["href"] = "https://fresh" + nonce + ".example/random/" + node["selector"][1:]
        result, _ = await classify_snapshot(
            mutated, expected, "https://fresh" + nonce + ".example/random/subject"
        )
        assert result.page_type == first.page_type == expected
        assert result.structure_fingerprint == first.structure_fingerprint
        assert result.structure_fingerprint.startswith("sha256:")


@pytest.mark.asyncio
async def test_subject_audit_survives_recording_in_both_persistent_payloads():
    from services.api.tests.test_web_page_type_assessment import Repository, _v4_recorder

    result, capture = await classify_snapshot(load_fixture(CASES[0][0]), "video_detail")
    repository = Repository()
    await _v4_recorder(repository).persist(
        task_id="task",
        agent_run_id="run",
        session_id="session",
        capture=replace(capture, captured_at=datetime(2026, 9, 15, tzinfo=UTC)),
        assessment=result,
    )
    for payload in (
        repository.calls[0]["assessment_payload"],
        repository.calls[0]["audit_metadata"]["web_page_type_assessment"],
    ):
        audit = payload["subject_region_facts"]
        assert audit == asdict(capture.structure_facts.subject_region_facts)
        assert audit["primary_player_count"] == 1
        assert audit["secondary_recommendation_card_count"] == 12
        assert audit["selector_summaries"] and audit["token_summaries"]
        assert payload["structure_fingerprint"] == result.structure_fingerprint


@pytest.mark.asyncio
async def test_unknown_assessment_keeps_actual_conflict_values():
    result, _ = await classify_snapshot(
        load_fixture("conflicting_primary_regions.html"), "video_detail"
    )
    assert result.subject_region_facts.primary_subject_count == 2
    assert result.subject_region_facts.primary_player_count == 2
    assert result.structure_fingerprint.startswith("sha256:")


@pytest.mark.parametrize("version", ["2026-09-13.web.v8", "2026-09-14.web.v9"])
def test_frozen_catalog_is_readable_but_cannot_execute(version):
    from services.api.app.domain.page.web_page_catalog import parse_web_page_type_catalog
    from services.api.tests.test_web_page_type_assessment import _catalog_payload

    payload = _catalog_payload()
    payload["version"] = version
    excluded = {"collection_page", "recharge_page", "topup_page", "guide_page"}
    if version.endswith(".v8"):
        excluded.add("ad_page")
        payload["page_type_groups"].pop("evidence_only")
    else:
        payload["page_type_groups"]["evidence_only"] = ["ad_page"]
    payload["page_types"] = [
        item for item in payload["page_types"] if item["code"] not in excluded
    ]
    payload["page_type_groups"]["business"].remove("collection_page")
    catalog = parse_web_page_type_catalog(payload, legacy_frozen=True)
    assert catalog.version == version
    with pytest.raises(ValueError, match="WEB_LEGACY_CATALOG_EXECUTION_FORBIDDEN"):
        WebPageClassifier(assessor=Assessor(None), catalog=catalog)
    assert_historical_explanation_keeps_catalog(payload)


def assert_historical_explanation_keeps_catalog(payload):
    from services.api.app.application.web_legacy_run_audit import LegacyWebRunAuditAdapter
    from services.api.app.application.web_report_explanation import build_web_report_explanation
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    frozen = policy_payload()
    frozen["page_catalog"] = payload
    frozen["guide_version"] = payload["version"]
    before = deepcopy(frozen)
    view = LegacyWebRunAuditAdapter.load(frozen)
    assert view.execution_allowed is False
    assert view.catalog.version == payload["version"]
    assert view.catalog.require_code("content_detail").name == next(
        item["name"] for item in payload["page_types"] if item["code"] == "content_detail"
    )
    explanation = build_web_report_explanation(
        task_id="t",
        agent_run_id="r",
        epoch=0,
        rows={
            "web_logical_pages": [
                {
                    "primary_capture_id": "frozen-capture",
                    "page_type": "content_detail",
                    "coverage_group": "business",
                }
            ],
        },
    )
    assert explanation["page_type_summary"]["business_page_types"] == ["content_detail"]
    assert "subject_region_facts" not in repr(explanation)
    assert frozen == before


def test_v5_to_v7_history_remains_read_only_without_classifier(
    monkeypatch, frozen_v5_to_v7_catalog_payload
):
    def forbidden(*args, **kwargs):
        raise AssertionError("历史报告不得启动当前分类器")

    monkeypatch.setattr(WebPageClassifier, "__init__", forbidden)
    payload, _ = frozen_v5_to_v7_catalog_payload
    assert_historical_explanation_keeps_catalog(payload)


@pytest.mark.asyncio
async def test_multiple_native_sources_are_one_subject_not_repeated_preview():
    result, _ = await classify_snapshot(
        snapshot_from_html(
            '<main><h1>主题</h1><video controls><source src="/one.mp4">'
            '<source src="/one.webm"></video></main>'
        ),
        "video_detail",
    )
    assert result.page_type == "video_detail"
    assert result.subject_region_facts.primary_player_count == 1


@pytest.mark.asyncio
async def test_two_primary_players_under_one_article_remain_conflicting():
    result, _ = await classify_snapshot(
        snapshot_from_html(
            '<article><h1>主题</h1><video src="/one.mp4"></video>'
            '<video src="/two.mp4"></video></article>'
        ),
        "video_detail",
    )
    assert result.page_type == "unknown"
    assert "WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT" in result.reason_codes


@pytest.mark.asyncio
async def test_conflict_reason_is_frozen_without_becoming_model_review():
    from services.api.tests.test_web_page_type_assessment import Repository, _v4_recorder

    result, capture = await classify_snapshot(
        load_fixture("conflicting_primary_regions.html"), "video_detail"
    )
    repository = Repository()
    await _v4_recorder(repository).persist(
        task_id="task",
        agent_run_id="run",
        session_id="session",
        capture=replace(capture, captured_at=datetime(2026, 9, 15, tzinfo=UTC)),
        assessment=result,
    )
    for payload in (
        repository.calls[0]["assessment_payload"],
        repository.calls[0]["audit_metadata"]["web_page_type_assessment"],
    ):
        assert payload["reason_code"] == "WEB_PAGE_TYPE_PRIMARY_REGION_CONFLICT"
        assert payload["subject_region_facts"]["primary_subject_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture,expected", [CASES[1], CASES[2], CASES[0]])
@pytest.mark.parametrize("vendor", ["dplayer", "video-js", "plyr", "jwplayer"])
async def test_vendor_class_in_any_region_does_not_change_classification(fixture, expected, vendor):
    original = load_fixture(fixture)
    first, _ = await classify_snapshot(original, expected)
    mutated = deepcopy(original)
    for node in mutated["elements"]:
        if fixture != CASES[0][0] or node["tag"] == "li":
            node["class_tokens"].append(vendor)
    result, _ = await classify_snapshot(mutated, expected)
    assert result.page_type == first.page_type == expected
    assert result.structure_fingerprint == first.structure_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("elements", [None, 42, True])
async def test_malformed_element_collection_is_safe_unknown(elements):
    result, capture = await classify_snapshot({"elements": elements}, "video_detail")
    assert capture.structure_facts.subject_region_facts.primary_subject_count == 0
    assert result.page_type == "unknown"


@pytest.mark.parametrize("elements", [None, 42, True])
def test_subject_extraction_direct_entry_rejects_non_collection_safely(elements):
    from services.api.app.application.web_primary_subject_facts import extract_subject_region_facts

    assert extract_subject_region_facts(elements).primary_subject_count == 0
    assert extract_subject_region_facts({"elements": elements}).primary_player_count == 0
