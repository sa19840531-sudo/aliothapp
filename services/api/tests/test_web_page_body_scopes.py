"""正文与卡片摘要按 Snapshot 父链区分，不能依赖选择器形状。"""

import pytest

from services.api.app.application.web_page_classification import web_page_structure_facts
from services.api.tests.test_web_page_recognition_rules import classify


def _body(selector_kind, *, with_cards=False, with_body=True):
    elements = [{"tag": "main", "selector": "#body", "visible": True}]
    if with_body:
        elements.extend(
            [
                {"tag": "div", "selector": "#wrapper", "parent_selector": "#body", "visible": True},
                {
                    "tag": "div",
                    "selector": '[data-testid="prose"]',
                    "parent_selector": "#wrapper",
                    "visible": True,
                },
            ]
        )
        for i in range(3):
            selector = {
                "id": f"#paragraph-{i}",
                "testid": f'[data-testid="paragraph-{i}"]',
                "name": f'p[name="paragraph-{i}"]',
            }[selector_kind]
            elements.append(
                {
                    "tag": "p",
                    "selector": selector,
                    "parent_selector": '[data-testid="prose"]',
                    "visible": True,
                    "text": "文" * 200,
                    "within_repeated_structure": True,
                    "repeat_group_size": 3,
                }
            )
    if with_cards:
        for i in range(2):
            card = f"#body > div:nth-of-type({i + 2})"
            elements.extend(
                [
                    {
                        "tag": "div",
                        "selector": card,
                        "parent_selector": "#body",
                        "visible": True,
                        "within_repeated_structure": True,
                        "repeat_group_size": 2,
                    },
                    {
                        "tag": "a",
                        "role": "link",
                        "selector": f"#link-{i}",
                        "parent_selector": card,
                        "href": f"/entry/{i}",
                        "text": "推荐",
                        "visible": True,
                        "interactive": True,
                        "within_repeated_structure": True,
                        "repeat_group_size": 2,
                        "rect": {"x": 1, "y": i * 100, "width": 100, "height": 40},
                    },
                    {
                        "tag": "p",
                        "selector": card + " > p",
                        "parent_selector": card,
                        "text": "摘" * 200,
                        "visible": True,
                        "within_repeated_structure": True,
                        "repeat_group_size": 2,
                    },
                ]
            )
    return {"elements": elements}


@pytest.mark.asyncio
@pytest.mark.parametrize("selector_kind", ["id", "testid", "name"])
async def test_independent_paragraph_selectors_follow_multilevel_parent_scope(selector_kind):
    facts = web_page_structure_facts(_body(selector_kind))
    assert facts.paragraph_text_length == 600
    result, _ = await classify("content_detail", facts)
    assert result.page_type == "content_detail"


@pytest.mark.asyncio
@pytest.mark.parametrize("with_body", [False, True])
async def test_plain_div_card_summaries_do_not_become_single_article(with_body):
    facts = web_page_structure_facts(
        _body("id", with_cards=True, with_body=with_body),
        origin="https://example.org",
        current_url="https://example.org/category",
    )
    assert facts.primary_detail_link_count == 2
    assert facts.paragraph_text_length == (600 if with_body else 0)
    assert facts.single_subject_element_count == (1 if with_body else 0)
    result, _ = await classify("content_detail", facts)
    assert result.page_type == ("content_detail" if with_body else "unknown")
    assert result.needs_review is (not with_body)
    if not with_body:
        assert "repeated_cards_conflict_with_detail" in result.conflicts
