"""验证两个 V2 App 探索入口把同一请求锁定为同一运行契约。"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import fakeredis
import pytest
from fastapi.testclient import TestClient

from services.api.app.api.v2 import tasks as v2_tasks_module
from services.api.app.application.app_exploration_policy import (
    build_dynamic_app_exploration_runtime_policy,
)
from services.api.app.application.app_exploration_target import (
    AppExplorationTargetSnapshot,
)
from services.api.app.application.dynamic_app_exploration import (
    build_dynamic_app_exploration_objective,
)
from services.api.app.application.task_submission import (
    CreatedTask,
    CreateTaskSubmissionCommand,
    PersistedTaskSubmission,
    TaskSubmissionService,
)
from services.api.app.application.v2_task_entries import (
    ExplorationTaskV2EntryService,
    TaskSubmissionV2EntryService,
)
from services.api.app.domain.exploration import build_app_exploration_goal
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.app_runtime.fake import FakeAppRuntime
from services.api.app.infrastructure.queue.redis_queue import RedisAgentRunQueue
from services.api.app.infrastructure.queue.state_store import InMemoryAgentRunStateStore
from services.api.app.main import create_app
from services.api.tests.test_v2_task_entrypoints import _app_task_payload

_APP_PAGE_TYPES = get_default_page_type_registry().coverage_required_codes


@dataclass
class RecordingReliableSubmissionRepository:
    """只记录真实 TaskSubmissionService 交给可靠仓储的提交命令。"""

    commands: list[CreateTaskSubmissionCommand] = field(default_factory=list)

    async def create_submission(
        self,
        command: CreateTaskSubmissionCommand,
    ) -> PersistedTaskSubmission:
        self.commands.append(command)
        sequence = len(self.commands)
        return PersistedTaskSubmission(
            task=CreatedTask(
                task_id=f"task-parity-{sequence}",
                ruleset_id=f"ruleset-parity-{sequence}",
                target=command.target,
                status="queued",
                task_config=command.task_config,
                page_sampling_policy=command.page_sampling_policy,
                loop_guard=command.loop_guard,
            ),
            run_id=f"run-parity-{sequence}",
            replayed=False,
        )


def _submission_service(
    repository: RecordingReliableSubmissionRepository,
) -> TaskSubmissionService:
    return TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=RedisAgentRunQueue(
            fakeredis.aioredis.FakeRedis(decode_responses=True),
            ttl_seconds=3600,
        ),
        state_store=InMemoryAgentRunStateStore(),
        app_qc_enabled=True,
        default_workflow_version="v2",
    )


def _app_parity_payload(
    *,
    scan_mode: str,
    timeout_minutes: int | None,
    max_actions: int | None,
    device_capability_template: str,
    required_page_types: tuple[str, ...],
    milestones: tuple[str, ...],
    minimum_business_pages: int | None,
) -> dict[str, Any]:
    payload = deepcopy(_app_task_payload())
    payload["scan_mode"] = scan_mode
    payload["timeout_minutes"] = timeout_minutes
    payload["target"]["exploration_target"] = {
        "required_page_types": list(required_page_types),
        "milestones": list(milestones),
        "minimum_business_pages": minimum_business_pages,
        "max_actions": max_actions,
        "device_capability_template": device_capability_template,
    }
    return payload


def _submit_both_endpoints(
    payload: dict[str, Any],
) -> tuple[CreateTaskSubmissionCommand, CreateTaskSubmissionCommand]:
    full_repository = RecordingReliableSubmissionRepository()
    exploration_repository = RecordingReliableSubmissionRepository()
    full_service = _submission_service(full_repository)
    exploration_service = _submission_service(exploration_repository)

    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_full_task_entry] = lambda: (
        TaskSubmissionV2EntryService(full_service)
    )
    app.dependency_overrides[v2_tasks_module._get_exploration_task_entry] = lambda: (
        ExplorationTaskV2EntryService(exploration_service)
    )

    idempotency_key = f"app-exploration-mode-parity:{payload['scan_mode']}"
    with TestClient(app) as client:
        full_response = client.post(
            "/api/v2/tasks",
            headers={"Idempotency-Key": idempotency_key},
            json=deepcopy(payload),
        )
        exploration_response = client.post(
            "/api/v2/exploration-tasks",
            headers={"Idempotency-Key": idempotency_key},
            json=deepcopy(payload),
        )

    assert full_response.status_code == 202, full_response.text
    assert exploration_response.status_code == 202, exploration_response.text
    assert len(full_repository.commands) == 1
    assert len(exploration_repository.commands) == 1
    return full_repository.commands[0], exploration_repository.commands[0]


def _normalized_submission(
    command: CreateTaskSubmissionCommand,
) -> tuple[dict[str, Any], dict[str, str | None]]:
    """移除两个入口被允许不同的字段，保留其余完整提交快照。"""

    task_config = deepcopy(command.task_config)
    execution_mode = task_config.pop("execution_mode", None)

    request_payload = deepcopy(command.request_payload)
    request_task_config = request_payload["task_config"]
    request_execution_mode = request_task_config.pop("execution_mode", None)
    request_payload["task_config"] = request_task_config
    request_submission_kind = request_payload.pop("submission_kind", None)

    assert request_task_config == task_config
    assert request_execution_mode == execution_mode
    assert request_submission_kind == command.submission_kind
    assert command.request_hash == stable_protocol_hash(command.request_payload)

    return (
        {
            "access_scope": command.access_scope,
            "target": command.target.to_dict(),
            "ruleset_code": command.ruleset_code,
            "ruleset_version": command.ruleset_version,
            "channel": command.channel,
            "priority": command.priority,
            "task_config": task_config,
            "page_sampling_policy": deepcopy(command.page_sampling_policy),
            "loop_guard": deepcopy(command.loop_guard),
            "request_payload": request_payload,
            "idempotency_key": command.idempotency_key,
        },
        {
            "execution_mode": execution_mode,
            "request_execution_mode": request_execution_mode,
            "submission_kind": command.submission_kind,
            "request_submission_kind": request_submission_kind,
        },
    )


def _runtime_contract(
    command: CreateTaskSubmissionCommand,
) -> tuple[Any, Any, str, AppExplorationTargetSnapshot]:
    """按生产执行器使用的真实动态构建器生成 Runtime 合同。"""

    app_target = command.target.app
    assert app_target is not None
    journey_steps = command.task_config["journey_steps"]
    assert isinstance(journey_steps, list)
    policy = build_dynamic_app_exploration_runtime_policy(
        task_config=command.task_config,
        loop_guard=command.loop_guard,
        page_sampling_policy=command.page_sampling_policy,
        journey_steps=journey_steps,
    )
    target_snapshot = policy.app_target_snapshot
    assert target_snapshot is not None
    goal = build_app_exploration_goal(
        package_name=app_target.package_name or "com.example.alioth",
        journey_steps=journey_steps,
        fallback_milestones=(),
        minimum_business_pages=policy.minimum_business_pages,
        required_page_types=target_snapshot.required_page_types,
        required_milestones=target_snapshot.required_milestones,
    )
    objective = build_dynamic_app_exploration_objective(
        goal,
        completion_mode=target_snapshot.completion_mode,
    )
    return policy, goal, objective, target_snapshot


@pytest.mark.parametrize(
    ("scan_mode", "timeout_minutes", "max_actions"),
    [
        ("full", None, None),
        ("quick", None, None),
        ("full", 60, 120),
        ("custom", 45, 200),
        ("custom", 180, 100),
    ],
)
@pytest.mark.parametrize(
    "device_capability_template",
    ["android_visual_readonly_v1", "android_visual_evidence_v1"],
)
@pytest.mark.parametrize(
    ("required_page_types", "milestones", "minimum_business_pages"),
    [
        pytest.param(
            ("home", "content_detail"),
            ("journey:登录", "surface:content_detail"),
            2,
            id="explicit-two-page-target",
        ),
        pytest.param((), (), None, id="default-dynamic-target"),
        pytest.param(
            _APP_PAGE_TYPES,
            ("startup_ad", "guide_page"),
            11,
            id="registered-eleven-page-target",
        ),
    ],
)
def test_same_app_request_has_only_mode_and_submission_kind_differences(
    scan_mode: str,
    timeout_minutes: int | None,
    max_actions: int | None,
    device_capability_template: str,
    required_page_types: tuple[str, ...],
    milestones: tuple[str, ...],
    minimum_business_pages: int | None,
) -> None:
    """两个 API 入口必须持久化同一 App target、预算、约束和能力模板。"""

    full, exploration = _submit_both_endpoints(
        _app_parity_payload(
            scan_mode=scan_mode,
            timeout_minutes=timeout_minutes,
            max_actions=max_actions,
            device_capability_template=device_capability_template,
            required_page_types=required_page_types,
            milestones=milestones,
            minimum_business_pages=minimum_business_pages,
        )
    )

    full_contract, full_modes = _normalized_submission(full)
    exploration_contract, exploration_modes = _normalized_submission(exploration)
    assert full_contract == exploration_contract
    assert full_modes == {
        "execution_mode": "full_inspection",
        "request_execution_mode": "full_inspection",
        "submission_kind": "v2_full_task",
        "request_submission_kind": "v2_full_task",
    }
    assert exploration_modes == {
        "execution_mode": "exploration_only",
        "request_execution_mode": "exploration_only",
        "submission_kind": "v2_exploration_task",
        "request_submission_kind": "v2_exploration_task",
    }

    assert full.target == exploration.target
    assert full.target.app is not None
    assert full.target.app.to_dict() == {
        "platform_build_ref": "app-build:00000000-0000-0000-0000-000000000001",
        "package_name": "com.example.alioth",
        "device": {"backend": "cloud_phone", "profile_id": "pixel-7"},
    }
    assert full.task_config["app"] == full.target.to_dict()["app"]
    assert exploration.task_config["app"] == exploration.target.to_dict()["app"]
    assert full.page_sampling_policy == exploration.page_sampling_policy
    assert full.loop_guard == exploration.loop_guard
    from services.api.app.application.v2_task_entries import UNLIMITED_DURATION_SECONDS

    assert full.loop_guard["max_duration_seconds"] == UNLIMITED_DURATION_SECONDS
    assert full.loop_guard["unlimited_duration"] == 1
    assert full.loop_guard.get("max_actions") == max_actions
    if max_actions is not None:
        assert full.loop_guard["max_steps"] is None
    assert full.task_config["max_depth"] == full.loop_guard["max_depth"]

    # 动态 Frontier 数量不限量：page_limit 仅作提示，不得因最低业务页数阻断。
    full_policy, full_goal, full_objective, full_snapshot = _runtime_contract(full)
    (
        exploration_policy,
        exploration_goal,
        exploration_objective,
        exploration_snapshot,
    ) = _runtime_contract(exploration)
    assert full_policy.to_dict() == exploration_policy.to_dict()
    assert full_goal.to_dict() == exploration_goal.to_dict()
    assert full_objective == exploration_objective
    assert full_snapshot.to_dict() == exploration_snapshot.to_dict()
    assert full_snapshot.device_capability_template == device_capability_template
    assert exploration_snapshot.device_capability_template == device_capability_template
    capability_manifest = FakeAppRuntime().describe_capabilities()
    full_snapshot.require_device_capabilities(capability_manifest)
    exploration_snapshot.require_device_capabilities(capability_manifest)
    if required_page_types:
        assert full_snapshot.required_page_types == required_page_types
        assert full_snapshot.completion_mode == "explicit_targets"
    else:
        assert full_snapshot.required_page_types == _APP_PAGE_TYPES
        assert full_snapshot.required_milestones == ("surface:stable_business_page",)
        assert full_snapshot.minimum_business_pages == 1
        assert full_snapshot.completion_mode == "dynamic_discovery"
    assert full_policy.exploration_mode == exploration_policy.exploration_mode == "dynamic_frontier"
    assert full_policy.playbook_id is None
    assert exploration_policy.playbook_id is None
    assert full_policy.curriculum_progress is None
    assert exploration_policy.curriculum_progress is None
    assert full_policy.active_tabs == exploration_policy.active_tabs == ()
    assert full_policy.max_actions == exploration_policy.max_actions
    assert full_policy.duration_ms == exploration_policy.duration_ms
    assert full_policy.max_navigation_depth == exploration_policy.max_navigation_depth
