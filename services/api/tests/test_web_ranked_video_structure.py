"""用黄果排行页同帧 DOM 摘录回归 Web 视频榜单的主体识别。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from services.api.app.application.web_page_classification import (
    WebPageAssessment,
    WebPageClassifier,
    WebReadyPageCapture,
    web_page_structure_facts,
)
from services.api.app.domain.page import load_web_page_type_catalog

_FIXTURE = Path(__file__).with_name("fixtures") / "web_huang_rank_excerpt_20260930.json"


def _snapshot() -> dict[str, object]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _facts(snapshot: dict[str, object]):
    return web_page_structure_facts(
        snapshot,
        origin="https://huangguoai.com",
        current_url=str(snapshot["url"]),
    )


class _Assessor:
    def __init__(self) -> None:
        self.calls = []

    async def classify(self, **_kwargs):
        self.calls.append(_kwargs)
        return WebPageAssessment(
            page_type="video_list",
            confidence=0.96,
            evidence=("主体为重复的视频排行条目",),
        )


@pytest.mark.asyncio
async def test_huang_rank_saved_dom_excerpt_signs_video_list_without_lazy_covers() -> None:
    facts = _facts(_snapshot())
    assessor = _Assessor()
    result = await WebPageClassifier(
        assessor=assessor, catalog=load_web_page_type_catalog()
    ).classify(
        WebReadyPageCapture(
            capture_id="huang-rank-ready",
            web_capture_stage="web_page_ready_baseline",
            screenshot_artifact_id="rank-screenshot",
            dom_artifact_id="rank-dom",
            visible_text_artifact_id="rank-text",
            page_role="menu",
            url="https://huangguoai.com/ranks/hot/",
            capture_purpose="web_page_ready_baseline",
            structure_facts=facts,
        )
    )

    assert facts.ranked_video_row_count >= 5
    assert facts.media_card_count == 0
    assert facts.player_root_count == 0
    assert result.page_type == "video_list"
    assert result.needs_review is False
    model_video_list = next(
        item for item in assessor.calls[0]["page_type_catalog"]
        if item["code"] == "video_list"
    )
    assert any("排行标题行" in signal for signal in model_video_list["required_positive_signals"])
    assert assessor.calls[0]["structure_facts"]["ranked_video_row_count"] >= 5


@pytest.mark.parametrize("mutation", [
    "only_url_title", "aside", "navigation", "footer", "article_links",
    "video_detail_recommendations", "missing_titles", "no_rank_rows", "cross_origin",
    "invalid_port", "invalid_ipv6", "native_player", "visible_player",
    "duplicate_detail", "malformed_tokens", "missing_census", "incomplete_census",
    "main_iframe",
])
def test_rank_video_rows_ignore_non_primary_or_unproven_links(mutation: str) -> None:
    snapshot = deepcopy(_snapshot())
    rows = [
        element for element in snapshot["elements"]
        if "/video/" in str(element.get("href"))
    ]
    if mutation == "only_url_title":
        snapshot["elements"] = []
    elif mutation == "aside":
        for row in rows:
            row["selector"] = row["selector"].replace(" > main > ", " > aside > ")
            for ancestor in row["web_ancestor_structure"]:
                if ancestor["tag"] == "main":
                    ancestor["tag"] = "aside"
    elif mutation == "navigation":
        for row in rows:
            row["within_web_navigation"] = True
    elif mutation == "footer":
        for row in rows:
            row["within_footer"] = True
    elif mutation == "article_links":
        for row in rows:
            row["href"] = row["href"].replace("/video/", "/article/")
    elif mutation == "video_detail_recommendations":
        for row in rows:
            row["web_ancestor_structure"].insert(0, {
                "selector": "main > article",
                "tag": "article",
                "repeat_group_size": 0,
            })
    elif mutation == "missing_titles":
        for row in rows:
            row["text"] = ""
    elif mutation == "no_rank_rows":
        for row in rows:
            row["ancestor_class_tokens"] = ["content-list", "content-item"]
    elif mutation == "cross_origin":
        for row in rows:
            row["href"] = row["href"].replace("huangguoai.com", "elsewhere.test")
    elif mutation == "invalid_port":
        for row in rows:
            row["href"] = "https://huangguoai.com:abc/video/1"
    elif mutation == "invalid_ipv6":
        for row in rows:
            row["href"] = "https://[broken/video/1"
    elif mutation == "native_player":
        snapshot["web_native_structure"] = {"complete": True, "native_video_count": 1}
    elif mutation == "visible_player":
        snapshot["elements"].append({
            "selector": "main > video",
            "tag": "video",
            "visible": True,
            "within_repeated_structure": False,
            "web_ancestor_structure": [{"tag": "main", "selector": "main"}],
        })
    elif mutation == "duplicate_detail":
        for row in rows:
            row["href"] = "https://huangguoai.com/video/1/"
    elif mutation == "malformed_tokens":
        for row in rows:
            row["ancestor_class_tokens"] = None
    elif mutation == "missing_census":
        snapshot.pop("web_native_structure")
    elif mutation == "incomplete_census":
        snapshot["web_native_structure"]["complete"] = False
    elif mutation == "main_iframe":
        snapshot["elements"].append({
            "selector": "main > iframe",
            "tag": "iframe",
            "visible": True,
            "within_repeated_structure": False,
            "web_ancestor_structure": [{"tag": "main", "selector": "main"}],
        })

    assert _facts(snapshot).ranked_video_row_count == 0
