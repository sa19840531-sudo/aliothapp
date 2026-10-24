"""历史 Huang 文章标签与前后篇链接不得伪装主列表卡。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.app.application.web_subject_region_extraction import extract_regions
from services.api.tests.test_web_detail_metamorphic import classify_snapshot
from services.api.tests.web_subject_fixture_support import load_fixture

_FIXTURE = Path(__file__).parent / "fixtures" / "web_detail" / "huang_archives_604_redacted.json"


@pytest.mark.asyncio
async def test_historical_single_article_keeps_related_cards_but_not_tags_or_prev_next():
    snapshot = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert snapshot["source_capture_id"] == "f460d5d5-6e08-432b-a9a8-23a6efdc85f4"
    regions = extract_regions(snapshot)
    assert regions.primary_subject_kind == "article"
    assert regions.primary_article_body_count == 1
    assert regions.primary_article_text_length == 318
    assert regions.repeated_primary_card_count == 0
    assert regions.secondary_recommendation_card_count == 3
    result, _ = await classify_snapshot(
        snapshot, "content_detail", url=snapshot["url"]
    )
    assert result.page_type == "content_detail"
    assert not result.needs_review


def test_search_shaped_urls_stay_real_list_cards_without_article_auxiliary_ancestry():
    snapshot = deepcopy(load_fixture("list_with_repeated_primary_cards.html"))
    for index, node in enumerate(snapshot["elements"]):
        if node.get("tag") == "a" and node.get("href"):
            node["href"] = f"https://example.test/search/post/label-{index}/"
    facts = web_page_structure_facts(snapshot)
    assert facts.subject_region_facts.primary_article_body_count == 0
    assert facts.subject_region_facts.repeated_primary_card_count == 8
