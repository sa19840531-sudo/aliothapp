"""H5 链接目标只能绑定同帧已有、唯一可命中的控件。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.domain.interaction import InteractionOperation
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)


def _snapshot(*, second_hittable: bool = False):
    return {
        "url": "https://fixture.test/",
        "viewport": {"width": 390, "height": 844},
        "elements": [
            {
                "selector": selector,
                "tag": "a",
                "text": "浏览栏目",
                "href": "https://fixture.test/Browse?Token=AbC",
                "interactive": True,
                "rect": {"x": x, "y": 100, "width": 100, "height": 40},
                "hit_test": {"center_hits_target": hittable},
            }
            for selector, x, hittable in (
                ("#visible", 10, True),
                ("#drawer", 150 if second_hittable else -500, second_hittable),
            )
        ]
        + [
            {
                "selector": "#other",
                "tag": "a",
                "text": "其他栏目",
                "href": "https://fixture.test/other",
                "interactive": True,
                "rect": {"x": 10, "y": 160, "width": 100, "height": 40},
                "hit_test": {"center_hits_target": True},
            }
        ],
    }


async def _resolve(snapshot, target, *, session_id="h5:fixture"):
    resolver = UiSnapshotSemanticTargetResolver(
        snapshot_reader=SimpleNamespace(read_ui_snapshot=AsyncMock(return_value=snapshot))
    )
    return await resolver.resolve(
        SemanticTargetResolutionRequest(
            task_id="task-navigation",
            session_id=session_id,
            source_capture_id="capture",
            action_type=InteractionOperation.CLICK,
            semantic_target=target,
            parameters={},
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["浏览栏目", 'a[href="/Browse?Token=AbC"]:visible'])
async def test_h5_unique_hit_proof_preserves_href_case(target):
    resolved = await _resolve(_snapshot(), target)
    assert resolved.selector == "#visible"


@pytest.mark.asyncio
async def test_h5_two_hittable_duplicates_still_reject_ambiguity():
    with pytest.raises(SemanticTargetResolutionError) as caught:
        await _resolve(_snapshot(second_hittable=True), "浏览栏目")
    assert caught.value.error_code == "SEMANTIC_TARGET_AMBIGUOUS"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,code",
    [
        ('a[href="/missing"]:visible', "SEMANTIC_TARGET_NOT_FOUND"),
        ('a[href="/Browse"]:visible:has(button)', "SEMANTIC_TARGET_INVALID"),
    ],
)
async def test_h5_unknown_or_executable_selector_is_not_guessed(target, code):
    with pytest.raises(SemanticTargetResolutionError) as caught:
        await _resolve(_snapshot(), target)
    assert caught.value.error_code == code


@pytest.mark.asyncio
async def test_web_duplicate_matching_retains_existing_behavior():
    resolved = await _resolve(_snapshot(), "浏览栏目", session_id="web:fixture")
    assert resolved.selector == "#visible"
    with pytest.raises(SemanticTargetResolutionError) as caught:
        await _resolve(_snapshot(), 'a[href="/Browse?Token=AbC"]:visible', session_id="web:fixture")
    assert caught.value.error_code == "SEMANTIC_TARGET_AMBIGUOUS"
