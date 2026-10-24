"""Web 自然主体对象与动作后事实的回归。"""

from types import SimpleNamespace

import pytest

from services.api.app.application.web_interaction_facts import (
    web_business_identity_stable,
    web_capture_content_facts,
)


@pytest.mark.parametrize(
    "marker", ["post-content", "entry-content", "article-body", "video-player", "player-root"]
)
def test_existing_body_and_player_markers_are_business_not_object_identity(marker):
    facts = web_capture_content_facts(
        {
            "elements": [
                {
                    "tag": "div",
                    "selector": "#body",
                    "class_tokens": [marker],
                    "visible": True,
                    "text": "真实正文",
                }
            ]
        },
        url="https://example.test/article/1",
    )
    assert facts.business_layer_visible is True
    assert facts.content_identity is not None
    assert facts.subject_urls == ()


@pytest.mark.parametrize(
    "context", ["within_footer", "within_breadcrumb", "within_pagination", "within_web_navigation"]
)
def test_context_only_media_is_neither_business_nor_subject(context):
    facts = web_capture_content_facts(
        {
            "elements": [
                {
                    "tag": "video",
                    "selector": "#v1",
                    "visible": True,
                    "src": "/video/1",
                    context: True,
                }
            ]
        },
        url="https://example.test/",
    )
    assert facts.business_layer_visible is False
    assert facts.content_identity is None
    assert facts.subject_urls == ()


@pytest.mark.parametrize(
    "element,expected",
    [
        ({"tag": "video", "src": "/video/1", "visible": True}, True),
        (
            {"tag": "a", "href": "/video/1", "visible": True, "within_repeated_structure": True},
            False,
        ),
        (
            {
                "tag": "a",
                "href": "/video/1",
                "visible": True,
                "ancestor_class_tokens": ["recommendations"],
            },
            False,
        ),
        ({"tag": "main", "text": "视频标题一", "visible": True}, False),
        ({"tag": "video", "src": "/video/1", "visible": True, "is_advertisement": True}, False),
    ],
)
def test_only_business_object_itself_proves_same_url_target(element, expected):
    facts = web_capture_content_facts({"elements": [element]}, url="https://example.test/list")
    assert ("https://example.test/video/1" in facts.subject_urls) is expected


def test_visible_obstruction_prevents_business_layer():
    facts = web_capture_content_facts(
        {
            "elements": [
                {"tag": "video", "src": "/video/1", "visible": True},
                {"tag": "section", "visible": True, "web_obstruction_layer_structural": True},
            ]
        },
        url="https://example.test/list",
    )
    assert facts.business_layer_visible is False


def test_business_identity_accepts_same_url_monotonic_safe_fact_retention():
    """弹层移除释放快照容量时，只允许既有安全业务事实的单调扩展。"""

    existing = {"tag": "main", "selector": "#body", "visible": True, "text": "原业务正文"}
    before = web_capture_content_facts(
        {"elements": [existing]},
        url="https://example.test/detail/1?from=menu#top",
    )
    after = web_capture_content_facts(
        {
            "elements": [
                existing,
                {
                    "tag": "article",
                    "selector": "#late-retained",
                    "visible": True,
                    "text": "容量释放后保留的业务事实",
                },
            ]
        },
        url="https://example.test/detail/1",
    )

    assert before.content_identity != after.content_identity
    assert set(before.business_fact_identities) < set(after.business_fact_identities)
    assert web_business_identity_stable(before, after) is True


@pytest.mark.parametrize("scenario", ["missing", "changed", "url_drift"])
def test_business_identity_rejects_non_monotonic_or_obstructed_results(scenario):
    """旧事实丢失/变化或 URL 漂移不能冒充原业务页身份。"""

    existing = {"tag": "main", "selector": "#body", "visible": True, "text": "原业务正文"}
    retained = {
        "tag": "article",
        "selector": "#retained",
        "visible": True,
        "text": "另一个稳定事实",
    }
    before = web_capture_content_facts(
        {"elements": [existing, retained]},
        url="https://example.test/detail/1",
    )
    after_elements = [existing, retained]
    after_url = "https://example.test/detail/1"
    if scenario == "missing":
        after_elements = [existing]
    elif scenario == "changed":
        after_elements = [{**existing, "text": "被替换的业务正文"}, retained]
    elif scenario == "url_drift":
        after_url = "https://example.test/detail/2"
    after = web_capture_content_facts({"elements": after_elements}, url=after_url)

    assert web_business_identity_stable(before, after) is False


def test_business_fact_identity_excludes_ads_hidden_layers_and_dangerous_controls():
    """单调比较的事实集只密封安全可见业务事实。"""

    facts = web_capture_content_facts(
        {
            "elements": [
                {"tag": "main", "selector": "#safe", "visible": True, "text": "正文"},
                {
                    "tag": "article",
                    "selector": "#ad",
                    "visible": True,
                    "text": "广告",
                    "is_advertisement": True,
                },
                {"tag": "article", "selector": "#hidden", "visible": False, "text": "隐藏"},
                {
                    "tag": "article",
                    "selector": "#blocked",
                    "visible": True,
                    "text": "遮挡内容",
                    "web_obstruction_layer": "#modal",
                },
                {
                    "tag": "a",
                    "selector": "#download",
                    "class_tokens": ["content-detail"],
                    "visible": True,
                    "text": "下载",
                },
            ]
        },
        url="https://example.test/detail/1",
    )

    safe_only = web_capture_content_facts(
        {"elements": [{"tag": "main", "selector": "#safe", "visible": True, "text": "正文"}]},
        url="https://example.test/detail/1",
    )
    assert facts.business_fact_identities == safe_only.business_fact_identities


@pytest.mark.parametrize(
    "receipt,accepted,authorized",
    [
        (SimpleNamespace(), False, False),
        (
            SimpleNamespace(execution_outcome="succeeded", status="succeeded", error_code=None),
            True,
            True,
        ),
        (
            SimpleNamespace(
                execution_outcome="succeeded", status="evidence_failed", error_code="lease_lost"
            ),
            True,
            False,
        ),
        (
            SimpleNamespace(
                execution_outcome="failed", status="failed", error_code="action_failed"
            ),
            False,
            False,
        ),
    ],
)
def test_receipt_separates_executed_input_from_arrival_permission(receipt, accepted, authorized):
    from services.api.app.application.web_interaction_facts import (
        web_command_accepted,
        web_receipt_allows_arrival,
    )

    assert web_command_accepted(receipt) is accepted
    assert web_receipt_allows_arrival(receipt) is authorized


def test_blob_media_keeps_business_visibility_without_fabricating_target_identity():
    facts = web_capture_content_facts(
        {
            "elements": [
                {"tag": "video", "src": "blob:https://example.test/media", "visible": True},
            ]
        },
        url="https://example.test/list",
    )
    assert facts.subject_urls == ()
    assert facts.business_layer_visible is True


def test_one_resource_among_multiple_players_does_not_prove_primary_identity():
    facts = web_capture_content_facts(
        {
            "elements": [
                {"tag": "video", "visible": True},
                {"tag": "video", "src": "/video/1", "visible": True},
            ]
        },
        url="https://example.test/list",
    )
    assert facts.subject_urls == ()


@pytest.mark.parametrize(
    "scenario,postcondition,arrival",
    [
        ("restored", True, True),
        ("wrong_body", False, False),
        ("another_layer", True, False),
    ],
)
def test_close_requires_old_layer_absent_and_original_business_body(
    scenario, postcondition, arrival
):
    from dataclasses import replace

    from services.api.app.application.web_obstruction_outcome import (
        settle_web_obstruction_dismissal,
    )
    from services.api.tests.test_web_interaction_precision import _plan

    body = {"tag": "main", "selector": "#body", "visible": True, "text": "原业务对象"}
    layer = {
        "tag": "button",
        "selector": "#close",
        "visible": True,
        "web_obstruction_layer": "#dialog",
        "web_obstruction_layer_structural": True,
    }
    before = web_capture_content_facts({"elements": [body, layer]}, url="https://example.test/list")
    after_elements = [{**body, "text": "错误对象" if scenario == "wrong_body" else "原业务对象"}]
    if scenario == "another_layer":
        after_elements.append({**layer, "web_obstruction_layer": "#second-dialog"})
    after = replace(
        web_capture_content_facts({"elements": after_elements}, url="https://example.test/list"),
        readiness_accepted=True,
        identity_verified=True,
    )
    outcome = settle_web_obstruction_dismissal(
        intent=_plan().intent,
        selector="#close",
        before=before,
        after=after,
        after_capture_id="after-close",
        command_accepted=True,
    )
    assert outcome.effect_observed is True
    assert outcome.postcondition_satisfied is postcondition
    assert outcome.transition_arrived is arrival


def test_close_observes_rotated_hashed_creative_without_claiming_layer_dismissed():
    """同层创意资源变化是动作效果，但不等于阻断层已经消失。"""

    from dataclasses import replace

    from services.api.app.application.web_obstruction_outcome import (
        settle_web_obstruction_dismissal,
    )
    from services.api.tests.test_web_interaction_precision import _plan

    body = {"tag": "main", "selector": "#body", "visible": True, "text": "原业务对象"}
    layer = {
        "tag": "button",
        "selector": "#close",
        "visible": True,
        "web_obstruction_layer": "#dialog",
        "web_obstruction_layer_structural": True,
    }

    def facts(sha256: str):
        return web_capture_content_facts(
            {
                "elements": [body, layer],
                "resource_summaries": [
                    {
                        "selector": "#dialog > div > img",
                        "attribute": "src",
                        "sha256": sha256,
                    }
                ],
            },
            url="https://example.test/list",
        )

    before = facts("a" * 64)
    after = replace(facts("b" * 64), identity_verified=True)
    outcome = settle_web_obstruction_dismissal(
        intent=_plan().intent,
        selector="#close",
        before=before,
        after=after,
        after_capture_id="after-close",
        command_accepted=True,
    )

    assert outcome.effect_observed is True
    assert outcome.postcondition_satisfied is False
    assert outcome.transition_arrived is False
