"""显式前后截图规则等待完整真实动作，不在单帧上制造缺证执行。"""

from types import SimpleNamespace

import pytest

from services.api.app.application import app_inspection_publisher, inspection_candidate_publish
from services.api.app.application.app_inspection_publisher import AppCaptureInspectionPublisher
from services.api.app.application.app_runtime_composition import PostgresAppRuntimeProvider
from services.api.app.application.rule_page_state import AppActionBeforeScene
from services.api.app.core.config import Settings
from services.api.app.infrastructure.db.app_action_scene_repository import (
    PostgresAppActionBeforeSceneReader,
)
from services.api.tests.test_app_action_scene_repository_postgres import _database, _pair
from services.api.tests.test_app_inspection_work_item_publish import (
    _FakeAgentRunReader,
    _FakeAnnotator,
    _FakeRule,
    _FakeSnapshotProvider,
    _FakeTaskQuery,
    _FakeWorkItemService,
)
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)
from services.api.tests.test_app_runtime_composition import _app_record, _app_target

_CONTRACT = {"required_artifacts": ["screenshot"], "required_capture_roles": ["before", "primary"]}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(
        inspection_candidate_publish,
        "get_settings",
        lambda: SimpleNamespace(inspection_work_items=SimpleNamespace(max_attempts=3)),
    )


def _rule(*, executor="rule_engine", contract=None):
    return _FakeRule(
        rule_id="pair-rule",
        rule_code="custom-pair-rule",
        executor_type=executor,
        input_contract=_CONTRACT if contract is None else contract,
        judge_config={
            "engine_config": {
                "handler": "screenshot_change_assertion",
                "before_role": "before",
                "after_role": "primary",
            }
        }
        if executor == "rule_engine"
        else {},
    )


@pytest.mark.parametrize("executor", ["rule_engine", "llm"])
@pytest.mark.parametrize("complete_pair", [False, True])
async def test_before_primary_rules_without_state_contract_wait_for_complete_pair(
    executor,
    complete_pair,
):
    class SceneReader:
        calls = []

        async def get_before_scene(self, **kwargs):
            self.calls.append(kwargs)
            return (
                AppActionBeforeScene(capture_id="before-1", page_type=None, page_state="popup")
                if complete_pair
                else None
            )

    reader = SceneReader()
    work = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_rule(executor=executor)]),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=work,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=reader,
    )
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        agent_run_id="run-1",
        visual_page_type="home",
        page_state="popup",
        active_session_only=True,
    )
    assert result.published_count == int(complete_pair)
    assert len(work.commands) == int(complete_pair)
    assert reader.calls == [{"task_id": "task-1", "capture_id": "cap-1", "agent_run_id": "run-1"}]
    if not complete_pair:
        assert result.status == "evidence_rules_deferred"


async def test_h5_paired_rule_keeps_existing_publication_path():
    class SceneReader:
        async def get_before_scene(self, **_kwargs):
            raise AssertionError("H5 不使用 APP 动作配对读取器")

    work = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([_rule()]),
        snapshot_provider=_FakeSnapshotProvider(target_platform="h5"),
        work_item_service=work,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=SceneReader(),
    )
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        agent_run_id="run-1",
        visual_page_type="home",
        active_session_only=True,
    )
    assert result.published_count == 1


async def test_single_capture_rule_is_not_delayed_by_absent_action_pair():
    class SceneReader:
        async def get_before_scene(self, **_kwargs):
            return None

    rule = _rule(contract={"required_artifacts": ["screenshot"]})
    rule.applicable_page_types = ["video_list"]
    work = _FakeWorkItemService()
    publisher = AppCaptureInspectionPublisher(
        task_query=_FakeTaskQuery([rule]),
        snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
        work_item_service=work,
        annotator=_FakeAnnotator(),
        agent_run_reader=_FakeAgentRunReader(),
        action_scene_reader=SceneReader(),
    )
    result = await publisher.publish_for_capture(
        task_id="task-1",
        capture_id="cap-1",
        agent_run_id="run-1",
        visual_page_type="video_list",
    )
    assert result.published_count == 1


async def test_real_bootstrap_before_and_unfinished_after_only_publish_on_completed_pair(
    tmp_path,
    monkeypatch,
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        before, after, action = await _pair(connection, pool, ids, saved, tmp_path)

        async def get_pool(*_args, **_kwargs):
            return pool

        monkeypatch.setattr(app_inspection_publisher, "get_postgres_pool", get_pool)
        work = _FakeWorkItemService()
        publisher = AppCaptureInspectionPublisher(
            task_query=_FakeTaskQuery([_rule()]),
            snapshot_provider=_FakeSnapshotProvider(target_platform="app"),
            work_item_service=work,
            annotator=_FakeAnnotator(),
            agent_run_reader=_FakeAgentRunReader(),
            action_scene_reader=PostgresAppActionBeforeSceneReader(pool=pool),
        )

        async def publish(capture):
            return await publisher.publish_for_capture(
                task_id=ids["task"],
                capture_id=capture,
                agent_run_id=ids["run"],
                visual_page_type="home",
                page_state="popup",
                active_session_only=True,
            )

        for capture in (saved.capture_id, before):
            result = await publish(capture)
            assert result.published_count == 0 and result.status == "evidence_rules_deferred"
        await connection.execute(
            "UPDATE qc_action_execution SET status='evidence_pending',result_capture_id=NULL,"
            "evidence_finished_at=NULL WHERE action_execution_id=$1",
            action,
        )
        result = await publish(after)
        assert result.published_count == 0 and result.status == "evidence_rules_deferred"
        assert work.commands == []
        await connection.execute(
            "UPDATE qc_action_execution SET status='completed',result_capture_id=$2::uuid,"
            "evidence_finished_at=now() WHERE action_execution_id=$1",
            action,
            after,
        )
        result = await publish(after)
        assert result.published_count == 1
        assert len(work.commands) == 1 and work.commands[0].page_capture_id == after


async def test_production_live_publisher_keeps_popup_business_rules_deferred_with_real_pg(
    tmp_path,
    monkeypatch,
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        _before, after, _action = await _pair(connection, pool, ids, saved, tmp_path)
        await connection.execute(
            "UPDATE qc_app_visual_observation SET page_state='popup' WHERE capture_id=$1::uuid",
            after,
        )

        async def get_pool(*_args, **_kwargs):
            return pool

        monkeypatch.setattr(app_inspection_publisher, "get_postgres_pool", get_pool)
        provider = PostgresAppRuntimeProvider(
            worker_instance_id="controlled-publisher-test",
            understanding_provider=object(),
        )
        dependencies = provider._build_dependencies(
            Settings.model_validate(
                {
                    "environment": "test",
                    "workflow": {"app_runtime_provider": "fake", "app_vision_provider": "fake"},
                    "evidence_storage": {"backend": "local"},
                }
            ),
            target=_app_target(_app_record()),
        )
        publisher = dependencies.active_session_inspection._publisher._publisher
        assert isinstance(
            publisher._presentation_reader,
            app_inspection_publisher.PostgresCapturePresentationReader,
        )
        work = _FakeWorkItemService()
        publisher._work_item_service = work
        publisher._task_query = _FakeTaskQuery(
            [
                _FakeRule(
                    rule_id="base-rule",
                    rule_code="controlled-content-rule",
                    category="content",
                    executor_type="llm",
                    input_contract={"required_artifacts": ["screenshot", "action_trace"]},
                )
            ]
        )
        publisher._snapshot_provider = _FakeSnapshotProvider(target_platform="app")
        publisher._annotator = _FakeAnnotator()
        publisher._action_scene_reader = PostgresAppActionBeforeSceneReader(pool=pool)
        result = await publisher.publish_for_capture(
            task_id=ids["task"],
            capture_id=after,
            agent_run_id=ids["run"],
            visual_page_type="home",
            page_state="popup",
            active_session_only=True,
        )
        assert result.published_count == 0 and result.status == "evidence_rules_deferred"
        assert work.commands == []
