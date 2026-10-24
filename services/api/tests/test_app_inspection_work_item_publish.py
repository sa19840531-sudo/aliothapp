"""App Capture 正式质检库存发布测试。"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from services.api.app.agents.runtime.business_middlewares.base import (
    TaskRuntimeSnapshot,
)
from services.api.app.application.app_inspection_publisher import (
    AppCaptureInspectionPublisher,
)
from services.api.app.application.inspection_work_items import (
    InspectionWorkItemStatus,
    PublishedInspectionWorkItem,
)
from services.api.app.domain.evidence.presentation import (
    BasePageVisibility,
    BlockingScope,
    PresentationAssessment,
    TopmostLayerKind,
)


@dataclass
class _FakeRule:
    rule_id: str
    rule_code: str
    name: str = "demo"
    description: str = ""
    category: str = "content"
    severity: str = "medium"
    executor_type: str = "llm"
    scope: str = "page"
    page_type: str = "home"
    applicable_page_types: list[str] | None = None
    url_pattern: str | None = None
    steps: list[Any] | None = None
    input_contract: dict[str, Any] | None = None
    check_point: str = "check"
    judge_config: dict[str, Any] | None = None
    manual_review_policy: dict[str, Any] | None = None
    sort_order: int = 1
    match_reason: str = "applicable_page_types"

    def __post_init__(self) -> None:
        if self.applicable_page_types is None:
            self.applicable_page_types = ["home"]
        if self.steps is None:
            self.steps = []
        if self.input_contract is None:
            self.input_contract = {"required_artifacts": ["screenshot"]}
        if self.judge_config is None:
            self.judge_config = {}
        if self.manual_review_policy is None:
            self.manual_review_policy = {}


class _FakeTaskQuery:
    def __init__(self, rules: list[_FakeRule] | None = None) -> None:
        self.rules = rules or []
        self.page_capture_url_calls: list[tuple[str, str]] = []
        self.page_urls: list[str | None] = []

    async def get_page_capture_url(
        self,
        *,
        task_id: str,
        page_capture_id: str,
    ) -> str:
        self.page_capture_url_calls.append((task_id, page_capture_id))
        return "app://com.example.target/.MainActivity"

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        page_url: str | None = None,
        auth_gate: str | None = "auto",
    ):
        del task_id
        del auth_gate
        self.page_urls.append(page_url)
        return [rule for rule in self.rules if page_type in (rule.applicable_page_types or [])]


class _FakeSnapshotProvider:
    def __init__(
        self, *, target_platform: str | None = None, inspection_scope: tuple[str, ...] = (),
    ) -> None:
        self.target_platform = target_platform
        self.inspection_scope = inspection_scope

    async def get_task_snapshot(self, task_id: str) -> TaskRuntimeSnapshot:
        return TaskRuntimeSnapshot(
            task_id=task_id,
            status="running",
            ruleset_version=1,
            ruleset_id="ruleset-1",
            ruleset_snapshot_hash="hash-1",
            priority=10,
            target_platform=self.target_platform,
            inspection_scope=self.inspection_scope,
        )


class _FakeWorkItemService:
    def __init__(self) -> None:
        self.commands: list[Any] = []

    async def publish(self, command):
        self.commands.append(command)
        return PublishedInspectionWorkItem(
            work_item_id=f"work-{len(self.commands)}",
            status=InspectionWorkItemStatus.PENDING,
            input_hash=f"hash-{len(self.commands)}",
            created=True,
        )


class _FakeAnnotator:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def annotate_inspection(self, **kwargs):
        self.calls.append(kwargs)


class _NoActionSceneReader:
    async def get_before_scene(self, **kwargs):
        return None


@pytest.fixture(autouse=True)
def _stub_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "services.api.app.application.inspection_candidate_publish.get_settings",
        lambda: SimpleNamespace(inspection_work_items=SimpleNamespace(max_attempts=3)),
    )


class _FakeAgentRunReader:
    async def get_agent_run_id(self, *, capture_id: str) -> str | None:
        del capture_id
        return "run-1"


class _FakeAdLandingRuleScopeReader:
    def __init__(self, value: bool | None) -> None:
        self.value = value

    async def has_complete_pair(self, *, task_id: str, capture_id: str) -> bool | None:
        assert task_id == "task-1"
        assert capture_id == "cap-1"
        return self.value


class _FakePresentationReader:
    def __init__(self, presentation: PresentationAssessment) -> None:
        self.presentation = presentation

    async def get_presentation(self, *, task_id: str, capture_id: str):
        assert task_id == "task-1"
        assert capture_id == "cap-1"
        return self.presentation


async def test_app_publisher_marks_no_rules_applicable_without_matching_rules() -> None:
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=_FakeWorkItemService(),  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
    )

    assert result.status == "no_rules_applicable"
    assert result.published_count == 0
    assert annotator.calls[0]["inspection_status"] == "no_rules_applicable"
    assert annotator.calls[0]["page_type"] == "home"


@pytest.mark.parametrize("snapshot_only", [False, True])
async def test_app_publisher_keeps_unselected_post_list_separate_from_no_rules(
    snapshot_only: bool,
) -> None:
    """原任务的五类明确选择不含帖子列表，不能标成规则已检查完成。"""
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(
            target_platform="app",
            inspection_scope=(
                "content_detail", "content_list", "home", "video_detail", "video_list",
            ),
        ),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=_NoActionSceneReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1", capture_id="cap-1", visual_page_type="post_list",
        page_state="stable", agent_run_id="run-1", snapshot_only=snapshot_only,
    )

    assert result.status == "out_of_scope"
    assert result.published_count == 0
    assert service.commands == []
    assert annotator.calls[0]["inspection_status"] == "out_of_scope"
    assert "范围" in result.message


@pytest.mark.parametrize("scope", [(), ("post_list",)])
async def test_app_publisher_selected_or_unrestricted_post_list_publishes(scope) -> None:
    service = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([
            _FakeRule(
                rule_id="post-rule", rule_code="POST-1", page_type="post_list",
                applicable_page_types=["post_list"],
            ),
        ]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(target_platform="app", inspection_scope=scope),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=_NoActionSceneReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1", capture_id="cap-1", visual_page_type="post_list",
        page_state="stable", agent_run_id="run-1",
    )

    assert result.status == "published"
    assert len(service.commands) == 1
    assert service.commands[0].rules[0].rule_code == "POST-1"


async def test_app_publisher_publishes_enforced_work_items_for_matching_rules() -> None:
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_FakeRule(rule_id="r1", rule_code="HOME-1")]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
    )

    assert result.status == "published"
    assert result.published_count == 1
    assert service.commands[0].page_capture_id == "cap-1"
    assert service.commands[0].control_mode == "enforced"
    assert annotator.calls[0]["inspection_status"] == "published"


@pytest.mark.parametrize("target_platform", ["web"])
async def test_non_h5_publisher_does_not_apply_h5_presentation_filter(
    target_platform: str,
) -> None:
    service = _FakeWorkItemService()
    occluded = PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.OVERLAY,
        base_page_visibility=BasePageVisibility.FULLY_OCCLUDED,
        blocking_scope=BlockingScope.PAGE,
    )
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_FakeRule(rule_id="r1", rule_code="BASE-1")]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(target_platform=target_platform),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        presentation_reader=_FakePresentationReader(occluded),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
    )

    assert result.published_count == 1
    assert [rule.rule_code for rule in service.commands[0].rules] == ["BASE-1"]


async def test_h5_publisher_keeps_overlay_rule_and_excludes_occluded_base_rule() -> None:
    service = _FakeWorkItemService()
    occluded = PresentationAssessment(
        topmost_layer_kind=TopmostLayerKind.OVERLAY,
        base_page_visibility=BasePageVisibility.FULLY_OCCLUDED,
        blocking_scope=BlockingScope.PAGE,
    )
    rules = [
        _FakeRule(rule_id="r1", rule_code="BASE-1"),
        _FakeRule(
            rule_id="r2",
            rule_code="OVERLAY-1",
            judge_config={"evidence_layer_target": "overlay"},
        ),
    ]
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(rules),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(target_platform="h5"),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        presentation_reader=_FakePresentationReader(occluded),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
    )

    assert result.published_count == 1
    assert [rule.rule_code for rule in service.commands[0].rules] == ["OVERLAY-1"]


async def test_app_browser_landing_capture_publishes_only_alioth_17() -> None:
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    rules = [
        _FakeRule(
            rule_id="r17",
            rule_code="alioth_17",
            executor_type="hybrid",
            page_type="startup_ad",
            applicable_page_types=["startup_ad"],
        ),
        _FakeRule(
            rule_id="r18",
            rule_code="alioth_18",
            page_type="startup_ad",
            applicable_page_types=["startup_ad"],
        ),
    ]
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(rules),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
        ad_landing_rule_scope_reader=_FakeAdLandingRuleScopeReader(True),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="browser_page",
        agent_run_id="run-1",
    )

    assert result.status == "published"
    assert result.page_type == "startup_ad"
    assert [rule.rule_code for command in service.commands for rule in command.rules] == [
        "alioth_17"
    ]
    # 浏览器落地帧只借用 startup_ad 做规则作用域，库存仍保留视觉真实页型。
    assert annotator.calls[0]["page_type"] == "browser_page"


async def test_snapshot_only_browser_landing_capture_judges_frozen_alioth_17() -> None:
    service = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(
            [
                _FakeRule(
                    rule_id="r17",
                    rule_code="alioth_17",
                    executor_type="hybrid",
                    page_type="startup_ad",
                    applicable_page_types=["startup_ad"],
                    input_contract={"required_artifacts": ["screenshot", "action_trace"]},
                ),
                _FakeRule(
                    rule_id="r18",
                    rule_code="alioth_18",
                    page_type="startup_ad",
                    applicable_page_types=["startup_ad"],
                ),
            ]
        ),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        ad_landing_rule_scope_reader=_FakeAdLandingRuleScopeReader(True),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="browser_page",
        agent_run_id="run-1",
        snapshot_only=True,
    )

    assert result.status == "published"
    assert result.published_count == 1
    assert result.excluded_active_session_rule_count == 0
    assert len(service.commands) == 1
    assert service.commands[0].batch_key == "hybrid:content:snapshot"
    assert service.commands[0].session_dependency == "snapshot"
    assert service.commands[0].session_owner_agent_run_id is None
    assert [rule.rule_code for rule in service.commands[0].rules] == ["alioth_17"]


async def test_app_ad_source_capture_defers_alioth_17_until_pair_complete() -> None:
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(
            [
                _FakeRule(
                    rule_id="r17",
                    rule_code="alioth_17",
                    executor_type="hybrid",
                    page_type="startup_ad",
                    applicable_page_types=["startup_ad"],
                )
            ]
        ),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
        ad_landing_rule_scope_reader=_FakeAdLandingRuleScopeReader(False),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="startup_ad",
        agent_run_id="run-1",
    )

    assert result.status == "no_rules_applicable"
    assert result.published_count == 0
    assert service.commands == []
    assert annotator.calls[0]["page_type"] == "startup_ad"


async def test_app_publisher_marks_unmatched_page_type() -> None:
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=_FakeWorkItemService(),  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type=None,
        agent_run_id="run-1",
    )

    assert result.status == "unmatched_page_type"
    assert annotator.calls[0]["inspection_status"] == "unmatched_page_type"


@pytest.mark.parametrize("visual_page_type", [None, "app", "unknown"])
async def test_app_publisher_unusable_page_type_still_publishes_popup_ad_rules(
    visual_page_type: str | None,
) -> None:
    """遮挡／伪页型 app 不得空跑：按宿主回退并只发弹窗广告契约。"""

    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(  # type: ignore[arg-type]
            [
                _FakeRule(
                    rule_id="r-ad",
                    rule_code="alioth_20",
                    name="弹窗广告关闭行为检查",
                    category="ad",
                    applicable_page_types=["home", "video_list"],
                ),
                _FakeRule(
                    rule_id="r-content",
                    rule_code="HOME-CONTENT",
                    name="首页内容质量",
                    category="content",
                    applicable_page_types=["home"],
                ),
            ]
        ),
        snapshot_provider=_FakeSnapshotProvider(
            target_platform="app",
            inspection_scope=("home", "video_list"),
        ),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type=visual_page_type,
        page_state="popup",
        agent_run_id="run-1",
    )

    assert result.status == "published"
    assert result.published_count == 1
    assert result.page_type == "home"
    assert annotator.calls[0]["page_type"] == "home"
    assert annotator.calls[0]["page_state"] == "popup"
    assert [rule.rule_code for command in service.commands for rule in command.rules] == [
        "alioth_20"
    ]


async def test_app_publisher_popup_state_keeps_only_popup_relevant_rules() -> None:
    service = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(  # type: ignore[arg-type]
            [
                _FakeRule(
                    rule_id="r-ad",
                    rule_code="alioth_22",
                    name="首页弹窗连续展示流畅性检查",
                    category="ad",
                    applicable_page_types=["home"],
                ),
                _FakeRule(
                    rule_id="r-content",
                    rule_code="HOME-CONTENT",
                    name="首页内容质量",
                    category="content",
                    applicable_page_types=["home"],
                ),
            ]
        ),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        page_state="popup",
        agent_run_id="run-1",
    )

    assert result.status == "published"
    assert result.published_count == 1


async def test_app_publisher_resolves_page_type_alias() -> None:
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_FakeRule(rule_id="r1", rule_code="HOME-1")]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="首页",
        agent_run_id="run-1",
    )

    assert result.status == "published"
    assert result.page_type == "home"
    assert annotator.calls[0]["page_type"] == "home"


async def test_snapshot_only_publisher_excludes_active_session_rules() -> None:
    service = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(
            [
                _FakeRule(rule_id="r1", rule_code="STATIC-1"),
                _FakeRule(
                    rule_id="r2",
                    rule_code="INTERACTION-1",
                    input_contract={"required_artifacts": ["screenshot", "action_trace"]},
                ),
            ]
        ),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
        snapshot_only=True,
    )

    assert result.published_count == 1
    assert result.excluded_active_session_rule_count == 1
    assert result.excluded_active_session_rules[0]["rule_id"] == "r2"
    assert result.excluded_active_session_rules[0]["rule_code"] == "INTERACTION-1"
    assert result.excluded_active_session_rules[0]["contract_hash"]
    assert result.excluded_active_session_rules[0]["session_dependency"] == "active_session"
    assert result.excluded_active_session_rules[0]["execution_plan"]["disposition"] == (
        "needs_review"
    )
    assert len(service.commands) == 1
    assert service.commands[0].session_dependency == "snapshot"
    assert [item.rule_code for item in service.commands[0].rules] == ["STATIC-1"]


async def test_active_session_only_publisher_excludes_snapshot_rules() -> None:
    service = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery(
            [
                _FakeRule(rule_id="r1", rule_code="STATIC-1"),
                _FakeRule(
                    rule_id="r2",
                    rule_code="INTERACTION-1",
                    input_contract={"required_artifacts": ["screenshot", "action_trace"]},
                ),
            ]
        ),
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
        active_session_only=True,
    )

    assert result.published_count == 1
    assert result.excluded_active_session_rule_count == 0
    assert len(service.commands) == 1
    assert service.commands[0].session_dependency == "active_session"
    assert [item.rule_code for item in service.commands[0].rules] == ["INTERACTION-1"]


async def test_active_session_only_marks_snapshot_rules_as_deferred() -> None:
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_FakeRule(rule_id="r1", rule_code="STATIC-1")]),
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
        active_session_only=True,
    )

    assert result.status == "snapshot_rules_deferred"
    assert result.published_count == 0
    assert service.commands == []
    assert annotator.calls[0]["inspection_status"] == "snapshot_rules_deferred"


async def test_publisher_rejects_conflicting_session_dependency_filters() -> None:
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([]),  # type: ignore[arg-type]
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=_FakeWorkItemService(),  # type: ignore[arg-type]
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
    )

    with pytest.raises(ValueError, match="不能同时启用"):
        await publisher.publish_for_capture(
            task_id="task-1",
            capture_id="cap-1",
            visual_page_type="home",
            agent_run_id="run-1",
            snapshot_only=True,
            active_session_only=True,
        )


async def test_h5_prepare_only_returns_commands_without_work_or_annotation_side_effects() -> None:
    service = _FakeWorkItemService()
    annotator = _FakeAnnotator()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_FakeRule(rule_id="r1", rule_code="STATIC-1")]),
        snapshot_provider=_FakeSnapshotProvider(),
        work_item_service=service,  # type: ignore[arg-type]
        annotator=annotator,
        agent_run_reader=_FakeAgentRunReader(),
    )

    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        visual_page_type="home",
        agent_run_id="run-1",
        snapshot_only=True,
        prepare_only=True,
    )

    assert result.status == "published"
    assert result.published_count == 1
    assert len(result.prepared_commands) == 1
    assert result.prepared_commands[0].defer_wakeup is True
    assert service.commands == []
    assert annotator.calls == []
