"""列表滚动后点内容入口，真实结果仍由详情帧承担。"""

from copy import deepcopy

import pytest

from services.api.tests.test_app_rule_action_readiness import RESPONSE, assess, pair


def transition(mode="detail"):
    scroll = pair(1, "scroll")
    click = pair(2, "click", requested_capture_id=scroll[1]["capture_id"])
    if mode != "no_effect":
        click[1]["page_type"] = "content_detail"
    else:
        click[2]["verification_outcome"] = "no_effect"
    if mode == "cross_package":
        click[1]["package_name"] = "other.app"
    elif mode == "old_epoch":
        scroll[1]["finalization_epoch"] = 1
    elif mode == "occluded":
        click[0]["page_state"] = "popup"
    elif mode == "not_adjacent":
        for item in click:
            if "session_command_sequence" in item:
                item["session_command_sequence"] = 4
            else:
                item["command_sequence"] = 4
    elif mode == "wrong_requested":
        click[2]["requested_capture_id"] = "other-list"
    elif mode == "bad_manifest":
        click[0]["manifest_screenshot_artifact_ids"] = []
    elif mode == "after_popup":
        click[1]["page_state"] = "popup"
    elif mode == "after_unknown":
        click[1]["page_type"] = None
    return scroll, click


@pytest.mark.parametrize(
    "mode",
    [
        "detail",
        "no_effect",
        "cross_package",
        "old_epoch",
        "occluded",
        "not_adjacent",
        "wrong_requested",
        "bad_manifest",
        "after_popup",
        "after_unknown",
    ],
)
def test_scroll_click_transition_preserves_real_scope_and_feedback(mode):
    scroll, click = transition(mode)
    assert assess(RESPONSE, scroll, click).ready is (
        mode in {"detail", "no_effect", "after_popup", "after_unknown"}
    )
    assert click[1]["page_type"] == (
        "home" if mode == "no_effect" else None if mode == "after_unknown" else "content_detail"
    )


def test_no_scroll_does_not_fill_list_interaction():
    _, click = transition()
    assert not assess(RESPONSE, click).ready


@pytest.mark.asyncio
async def test_publisher_uses_exact_list_source_but_keeps_detail_primary():
    from services.api.app.application.app_inspection_publisher import AppCaptureInspectionPublisher
    from services.api.app.application.rule_page_state import AppActionBeforeScene
    from services.api.tests.test_app_inspection_work_item_publish import (
        _FakeAnnotator,
        _FakeRule,
        _FakeSnapshotProvider,
        _FakeTaskQuery,
        _FakeWorkItemService,
    )
    from services.api.tests.test_app_rule_action_readiness import SCOPE

    scroll, click = transition()
    action_facts = {
        "scope": SCOPE,
        "primary_capture_id": click[1]["capture_id"],
        "captures": [scroll[0], scroll[1], click[0], click[1]],
        "actions": [scroll[2], click[2]],
    }

    class Reader:
        calls = 0

        async def get_facts(self, **kwargs):
            self.calls += 1
            return deepcopy(action_facts)

    class SceneReader:
        async def get_before_scene(self, **kwargs):
            return AppActionBeforeScene(
                capture_id=click[0]["capture_id"], page_type="home", page_state="stable"
            )

    reader, service, annotator = Reader(), _FakeWorkItemService(), _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(
            [
                _FakeRule(
                    rule_id="response", rule_code="renamed", judge_config=RESPONSE["judge_config"]
                )
            ]
        ),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=service,
        annotator=annotator,
        action_scene_reader=SceneReader(),
        action_facts_reader=reader,
    )
    result = await publisher.publish_for_capture(
        task_id=SCOPE["task_id"],
        agent_run_id=SCOPE["agent_run_id"],
        capture_id=click[1]["capture_id"],
        visual_page_type="content_detail",
    )
    assert result.status == "published"
    assert reader.calls == 1
    assert service.commands[0].page_capture_id == click[1]["capture_id"]
    assert annotator.calls[-1]["page_type"] == "content_detail"
