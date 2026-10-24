"""V2 任务公开入口的 HTTP 合同测试。

这些测试验证 API 边界：H5/App 完整任务、H5 仅探索和独立规则评估进入各自
应用服务；未开启的 H5 正式模式及未接入平台返回结构化错误。
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from unittest.mock import ANY

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from services.api.app.api.v2 import tasks as v2_tasks_module
from services.api.app.api.v2.schemas import V2TaskSubmissionRequest
from services.api.app.application.app_exploration_target import (
    AppExplorationTargetSnapshot,
)
from services.api.app.application.task_submission import TaskSubmissionResult
from services.api.app.application.v2_task_entries import (
    UNLIMITED_DURATION_SECONDS,
    ExplorationTaskSubmissionResult,
    ExplorationTaskV2EntryService,
    H5V2ModeNotEnabledError,
    RuleEvaluationSubmissionResult,
    TaskSubmissionV2EntryService,
    WebV2ModeNotEnabledError,
    _scan_mode_loop_guard,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.task import AdultSelfAttestation, TaskTargetPlatform
from services.api.app.main import create_app
from services.api.tests.conftest import pop_test_auth

APP_BUILD_REF = "app-build:00000000-0000-0000-0000-000000000001"
TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any"}),
)


@dataclass
class RecordingTaskEntry:
    """替代完整任务入口，记录 HTTP 层交给应用层的命令。"""

    result: TaskSubmissionResult = field(
        default_factory=lambda: TaskSubmissionResult(
            task_id="task-v2",
            run_id="run-v2",
            ruleset_id="ruleset-v2",
            status="queued",
        )
    )
    commands: list[object] = field(default_factory=list)
    idempotency_keys: list[str | None] = field(default_factory=list)

    async def submit(
        self,
        command: object,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> TaskSubmissionResult:
        assert access_scope is not None
        self.commands.append(command)
        self.idempotency_keys.append(idempotency_key)
        return self.result


@dataclass
class RecordingExplorationTaskEntry:
    """替代探索入口，验证它接收与完整入口相同的 V2 命令。"""

    result: ExplorationTaskSubmissionResult = field(
        default_factory=lambda: ExplorationTaskSubmissionResult(
            task_id="exploration-task-v2",
            run_id="exploration-run-v2",
            status="queued",
        )
    )
    commands: list[object] = field(default_factory=list)
    idempotency_keys: list[str | None] = field(default_factory=list)

    async def submit(
        self,
        command: object,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> ExplorationTaskSubmissionResult:
        assert access_scope is not None
        self.commands.append(command)
        self.idempotency_keys.append(idempotency_key)
        return self.result


@dataclass
class RecordingRuleEvaluationEntry:
    """替代规则评估入口，记录路由传入的任务 ID。"""

    result: RuleEvaluationSubmissionResult = field(
        default_factory=lambda: RuleEvaluationSubmissionResult(
            task_id="task-v2",
            rule_evaluation_run_id="rule-run-v2",
            status="queued",
        )
    )
    task_ids: list[str] = field(default_factory=list)
    rule_codes: list[tuple[str, ...] | None] = field(default_factory=list)
    access_scopes: list[AccessScope] = field(default_factory=list)

    async def submit(
        self,
        task_id: str,
        *,
        rule_codes: tuple[str, ...] | None = None,
        access_scope: AccessScope,
    ) -> RuleEvaluationSubmissionResult:
        self.task_ids.append(task_id)
        self.rule_codes.append(rule_codes)
        self.access_scopes.append(access_scope)
        return self.result


@dataclass
class RecordingSubmissionService:
    """替代现有 TaskSubmissionService，记录完整适配器的调用参数。"""

    result: TaskSubmissionResult = field(
        default_factory=lambda: TaskSubmissionResult(
            task_id="task-adapter-v2",
            run_id="run-adapter-v2",
            ruleset_id="ruleset-adapter-v2",
            status="queued",
        )
    )
    calls: list[dict] = field(default_factory=list)

    async def submit_task(self, **kwargs) -> TaskSubmissionResult:
        self.calls.append(kwargs)
        return self.result


@pytest.fixture
def app_entry_client() -> tuple[TestClient, RecordingTaskEntry]:
    entry = RecordingTaskEntry()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_full_task_entry] = lambda: entry
    return TestClient(app), entry


def _app_task_payload() -> dict:
    return {
        "task_name": "App 首页回归",
        "target": {
            "platform": "app",
            "source": {
                "type": "build_ref",
                "platform_build_ref": APP_BUILD_REF,
            },
            "package_name": "com.example.alioth",
            "device": {"backend": "cloud_phone", "profile_id": "pixel-7"},
        },
        "ruleset_code": "alioth_first_ruleset",
        "ruleset_version": 1,
        "channel": "api",
        "priority": 80,
        "product": {
            "department_name": "质量保障部",
            "product_code": "APP-001",
            "product_name": "示例 App",
        },
        "scan_mode": "full",
        "journey_steps": ["首页", "详情"],
        "remark": "V2 入口合同测试",
    }


def _h5_task_payload() -> dict:
    payload = _app_task_payload()
    payload["target"] = {"platform": "h5", "url": "https://example.test/h5"}
    return payload


def test_shortflow_selection_reaches_authenticated_public_entry(app_entry_client):
    from services.api.tests.test_h5_shortflow_activation import selection

    client, entry = app_entry_client
    response = client.post(
        "/api/v2/tasks", json=_h5_task_payload() | {"h5_shortflows": [selection()]})
    assert response.status_code == 202
    assert entry.commands[-1].h5_shortflows[0] == selection()


def test_shortflow_configuration_failure_is_structured_422(app_entry_client):
    from services.api.app.application.h5_shortflows import H5ShortflowError
    from services.api.tests.test_h5_shortflow_activation import selection

    client, entry = app_entry_client

    async def reject(*args, **kwargs):
        raise H5ShortflowError("未发布或未实现的流程")

    entry.submit = reject
    response = client.post(
        "/api/v2/tasks", json=_h5_task_payload() | {"h5_shortflows": [selection()]})
    assert response.status_code == 422
    assert "未发布或未实现" in response.text


def _web_task_payload(*, url: str = "https://example.test") -> dict:
    payload = _h5_task_payload()
    payload["target"] = {
        "platform": "web",
        "url": url,
        "adult_self_attestation": {
            "declared": True,
            "statement_version": "adult-v1",
        },
    }
    return payload


@pytest.mark.parametrize(
    "legacy_override",
    [
        {"applicability_mode": "strict_catalog"},
        {"discovery_policy": {"enabled": False}},
        {"direct_ad_page_enabled": False},
        {"menu_registry_version": "web-menu-allowlist.v4"},
    ],
)
def test_new_web_run_rejects_legacy_execution_override(
    app_entry_client,
    legacy_override: dict[str, object],
) -> None:
    client, entry = app_entry_client
    payload = _web_task_payload()
    payload["target"]["exploration_target"] = legacy_override

    response = client.post("/api/v2/tasks", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN"
    assert entry.commands == []


def test_web_minimal_request_defaults_to_safe_adult_attestation_and_profile() -> None:
    """缺少成人声明的普通 Web 目标必须可提交，并安全地保持未声明状态。"""

    payload = _web_task_payload()
    payload["target"].pop("adult_self_attestation")

    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    assert command.platform.value == "web"
    assert command.browser_profile_id == "desktop_fhd_1920x1080_v1"
    assert command.adult_self_attestation_declared is None
    assert command.adult_self_attestation_statement_version is None


def test_web_request_rejects_client_supplied_attestation_identity_or_time() -> None:
    payload = _web_task_payload()
    payload["target"]["adult_self_attestation"]["declared_by"] = "spoofed-user"
    payload["target"]["adult_self_attestation"]["declared_at"] = "2026-08-30T12:00:00Z"

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        V2TaskSubmissionRequest.model_validate(payload)


def test_web_request_defaults_to_registered_desktop_profile() -> None:
    payload = _web_task_payload(url="https://example.test/dashboard")

    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    assert command.platform.value == "web"
    assert command.browser_profile_id == "desktop_fhd_1920x1080_v1"
    assert command.adult_self_attestation_declared is True
    assert command.adult_self_attestation_statement_version == "adult-v1"


def test_web_request_preserves_explicit_registered_profile_id() -> None:
    payload = _web_task_payload(url="https://example.test/dashboard")
    payload["target"]["browser_profile_id"] = "desktop_fhd_1920x1080_v1"

    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    assert command.browser_profile_id == "desktop_fhd_1920x1080_v1"


@pytest.mark.parametrize(
    "target_url",
    [
        "https://alice@example.test/h5",
        "https://alice:secret@example.test/h5",
        "https://example.test:invalid/h5",
        "https://alice:secret@example.com／evil/path",
    ],
)
@pytest.mark.parametrize("endpoint", ["/api/v2/tasks", "/api/v2/exploration-tasks"])
def test_h5_unsafe_url_is_rejected_before_application_entry(
    target_url: str,
    endpoint: str,
) -> None:
    entry = RecordingTaskEntry() if endpoint == "/api/v2/tasks" else RecordingExplorationTaskEntry()
    app = create_app()
    dependency = (
        v2_tasks_module._get_full_task_entry
        if endpoint == "/api/v2/tasks"
        else v2_tasks_module._get_exploration_task_entry
    )
    app.dependency_overrides[dependency] = lambda: entry
    client = TestClient(app)
    payload = _h5_task_payload()
    payload["target"]["url"] = target_url

    response = client.post(endpoint, json=payload)

    assert response.status_code == 422
    serialized_response = response.text
    assert "alice:secret@example.com" not in serialized_response
    assert "secret" not in serialized_response
    assert "alice" not in serialized_response
    body = response.json()
    assert set(body) == {"detail"}
    h5_url_error = next(
        error for error in body["detail"] if error["loc"] == ["body", "target", "h5", "url"]
    )
    assert h5_url_error["type"] == "value_error"
    assert h5_url_error["msg"] == "Value error, H5 URL 无效"
    assert h5_url_error["input"] == "[已脱敏]"
    assert "ctx" not in h5_url_error
    assert entry.commands == []


def test_unrelated_validation_error_keeps_default_detail_contract() -> None:
    entry = RecordingTaskEntry()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_full_task_entry] = lambda: entry
    client = TestClient(app)
    payload = _app_task_payload()
    payload["priority"] = -1

    response = client.post("/api/v2/tasks", json=payload)

    assert response.status_code == 422
    body = response.json()
    assert set(body) == {"detail"}
    assert any(
        error["loc"] == ["body", "priority"] and error["input"] == -1 for error in body["detail"]
    )
    assert entry.commands == []


def _download_url_task_payload() -> dict:
    payload = _app_task_payload()
    payload["target"] = {
        "platform": "app",
        "source": {
            "type": "download_url",
            "download_url": "https://example.test/builds/alioth.apk",
        },
        "package_name": "com.example.alioth",
    }
    return payload


def _assert_structured_error(response, *, code: str, status_code: int = 501) -> dict:
    assert response.status_code == status_code
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert body["error"]["message"]
    return body


def test_submit_v2_app_task_maps_nested_target_to_full_entry(
    app_entry_client: tuple[TestClient, RecordingTaskEntry],
) -> None:
    client, entry = app_entry_client

    response = client.post("/api/v2/tasks", json=_app_task_payload())

    assert response.status_code == 202
    assert response.json() == {
        "task_id": entry.result.task_id,
        "run_id": entry.result.run_id,
        "ruleset_id": entry.result.ruleset_id,
        "status": entry.result.status,
    }
    assert len(entry.commands) == 1
    command = entry.commands[0]
    assert command.task_name == "App 首页回归"
    assert getattr(command.platform, "value", command.platform) == "app"
    assert command.target_url is None
    assert command.app_source.kind == "build_ref"
    assert command.app_source.platform_build_ref == APP_BUILD_REF
    assert command.package_name == "com.example.alioth"
    assert command.device_backend == "cloud_phone"
    assert command.device_profile_id == "pixel-7"
    assert command.ruleset_code == "alioth_first_ruleset"
    assert command.ruleset_version == 1
    assert command.channel == "api"
    assert command.priority == 80


def test_submit_v2_task_forwards_idempotency_key(
    app_entry_client: tuple[TestClient, RecordingTaskEntry],
) -> None:
    client, entry = app_entry_client

    response = client.post(
        "/api/v2/tasks",
        headers={"Idempotency-Key": "v2-browser-request-1"},
        json=_app_task_payload(),
    )

    assert response.status_code == 202
    assert entry.idempotency_keys == ["v2-browser-request-1"]


@pytest.mark.asyncio
async def test_full_v2_entry_submits_h5_to_v2_runtime_when_mode_is_on() -> None:
    """H5 V2 正式开启后必须复用既有任务提交和队列链路。"""

    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(
        submission,
        h5_v2_mode="on",
        h5_inspection_notification_mode="enforced",
    )
    command = V2TaskSubmissionRequest.model_validate(_h5_task_payload()).to_command()

    result = await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert result == submission.result
    assert len(submission.calls) == 1
    call = submission.calls[0]
    assert call["target"].platform.value == "h5"
    assert call["target"].target_url == "https://example.test/h5"
    assert call["task_config"]["workflow_version"] == "v2"
    assert call["task_config"]["execution_mode"] == "full_inspection"
    assert call["task_config"]["h5_inspection_notification_mode"] == "enforced"
    assert call["task_config"]["h5_exploration_target"]["required_page_types"] == []
    assert call["task_config"]["inspection_scope"] == {"page_types": []}
    assert call["page_sampling_policy"] == {}
    assert call["loop_guard"] == {
        "max_depth": 4,
        "max_duration_seconds": 5400,
        "max_steps": 80,
        "max_ai_calls": 200,
    }


@pytest.mark.asyncio
async def test_full_v2_h5_entry_locks_explicit_business_page_subset() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, h5_v2_mode="on")
    payload = _h5_task_payload()
    payload["target"]["exploration_target"] = {
        "required_page_types": ["HOME", "profile", "home"],
    }

    command = V2TaskSubmissionRequest.model_validate(payload).to_command()
    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    call = submission.calls[0]
    assert command.required_page_types == ("home", "profile")
    assert call["task_config"]["h5_exploration_target"] == {
        "required_page_types": ["home", "profile"],
    }
    assert call["task_config"]["inspection_scope"] == {"page_types": ["home", "profile"]}
    assert call["page_sampling_policy"] == {
        "mode": "by_page_type",
        "include_page_types": ["home", "profile"],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("scan_mode", ["quick", "custom"])
async def test_v2_h5_explicit_subset_preserves_quick_and_custom_semantics(scan_mode: str) -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, h5_v2_mode="on")
    payload = _h5_task_payload()
    payload["scan_mode"] = scan_mode
    payload["target"]["exploration_target"] = {
        "required_page_types": ["home", "payment"],
    }

    command = V2TaskSubmissionRequest.model_validate(payload).to_command()
    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    call = submission.calls[0]
    assert call["task_config"]["h5_exploration_target"] == {
        "required_page_types": ["home", "payment"],
    }
    if scan_mode == "quick":
        assert call["page_sampling_policy"] == {
            "mode": "first_n",
            "max_pages": 3,
            "include_page_types": ["home", "payment"],
        }
    else:
        assert call["page_sampling_policy"] == {
            "mode": "by_page_type",
            "include_page_types": ["home", "payment"],
        }


@pytest.mark.asyncio
async def test_v2_h5_custom_without_subset_keeps_dynamic_discovery() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, h5_v2_mode="on")
    payload = _h5_task_payload()
    payload["scan_mode"] = "custom"

    command = V2TaskSubmissionRequest.model_validate(payload).to_command()
    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls[0]["task_config"]["h5_exploration_target"] == {
        "required_page_types": [],
    }
    assert submission.calls[0]["page_sampling_policy"] == {}


def test_v2_h5_target_rejects_lifecycle_page_types() -> None:
    payload = _h5_task_payload()
    payload["target"]["exploration_target"] = {
        "required_page_types": ["home", "startup_ad"],
    }

    with pytest.raises(ValueError, match="业务页面类型目录"):
        V2TaskSubmissionRequest.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["off", "shadow"])
async def test_full_v2_entry_rejects_h5_when_formal_mode_is_not_on(mode: str) -> None:
    """新 H5 V2 入口不能静默落入 V1 或 Shadow。"""

    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, h5_v2_mode=mode)
    command = V2TaskSubmissionRequest.model_validate(_h5_task_payload()).to_command()

    with pytest.raises(H5V2ModeNotEnabledError, match="ALIOTH_H5_V2_MODE=on"):
        await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls == []


def test_submit_v2_h5_maps_disabled_formal_mode_to_structured_conflict() -> None:
    """部署门禁必须在 HTTP 边界给出可识别冲突，不能泄露为 500。"""

    submission = RecordingSubmissionService()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_full_task_entry] = lambda: (
        TaskSubmissionV2EntryService(submission, h5_v2_mode="off")
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/v2/tasks", json=_h5_task_payload())

    _assert_structured_error(
        response,
        code="H5V2ModeNotEnabledError",
        status_code=409,
    )
    assert submission.calls == []


def test_exploration_task_override_returns_202_with_same_v2_command_as_full_entry() -> None:
    full_entry = RecordingTaskEntry()
    exploration_entry = RecordingExplorationTaskEntry()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_full_task_entry] = lambda: full_entry
    app.dependency_overrides[v2_tasks_module._get_exploration_task_entry] = lambda: (
        exploration_entry
    )
    client = TestClient(app)
    payload = _app_task_payload()

    full_response = client.post("/api/v2/tasks", json=payload)
    exploration_response = client.post("/api/v2/exploration-tasks", json=payload)

    assert full_response.status_code == 202
    assert exploration_response.status_code == 202
    assert exploration_response.json() == {
        "task_id": exploration_entry.result.task_id,
        "run_id": exploration_entry.result.run_id,
        "status": exploration_entry.result.status,
    }
    assert len(full_entry.commands) == 1
    assert len(exploration_entry.commands) == 1
    assert exploration_entry.commands[0] == full_entry.commands[0]
    assert exploration_entry.commands[0].platform.value == "app"


def test_rule_evaluation_override_returns_202_with_path_task_id() -> None:
    entry = RecordingRuleEvaluationEntry()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_rule_evaluation_entry] = lambda: entry
    client = TestClient(app)

    response = client.post(
        "/api/v2/tasks/task-from-path/rule-evaluation-runs",
        json={},
    )

    assert response.status_code == 202
    assert response.json() == {
        "task_id": entry.result.task_id,
        "rule_evaluation_run_id": entry.result.rule_evaluation_run_id,
        "status": entry.result.status,
    }
    assert entry.task_ids == ["task-from-path"]


def test_submit_v2_app_download_url_returns_structured_501_without_task_submission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    async def forbidden_submit(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("App 下载链接未接入时不应调用现有任务提交服务")

    monkeypatch.setattr(
        v2_tasks_module.TaskSubmissionService,
        "submit_task",
        forbidden_submit,
    )
    client = TestClient(create_app())

    response = client.post("/api/v2/tasks", json=_download_url_task_payload())

    _assert_structured_error(
        response,
        code="AppArtifactSourceNotConnectedError",
    )
    assert calls == []


async def test_full_v2_entry_adapts_app_command_to_existing_submission_service() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission)
    command = V2TaskSubmissionRequest.model_validate(_app_task_payload()).to_command()

    result = await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert result == submission.result
    assert len(submission.calls) == 1
    call = submission.calls[0]
    target = call["target"]
    assert target.platform.value == "app"
    assert target.target_url is None
    assert target.app.platform_build_ref == APP_BUILD_REF
    assert call["task_config"]["workflow_version"] == "v2"
    assert call["task_config"]["execution_mode"] == "full_inspection"
    assert call["task_config"]["task_name"] == "App 首页回归"
    assert call["task_config"]["product"] == {
        "department_name": "质量保障部",
        "product_code": "APP-001",
        "product_name": "示例 App",
    }
    assert call["task_config"]["scan_mode"] == "full"
    assert call["task_config"]["journey_steps"] == ["首页", "详情"]
    locked_target = AppExplorationTargetSnapshot.from_dict(
        call["task_config"]["app_exploration_target"]
    )
    assert locked_target.required_page_types == (
        get_default_page_type_registry().coverage_required_codes
    )
    assert locked_target.completion_mode == "dynamic_discovery"
    assert locked_target.required_milestones == ("surface:stable_business_page",)
    assert locked_target.minimum_business_pages == 1
    assert locked_target.requires_real_visual is True
    assert call["loop_guard"] == {
        "max_depth": 4,
        "max_duration_seconds": UNLIMITED_DURATION_SECONDS,
        "max_steps": None,
        "unlimited_duration": 1,
    }
    assert call["page_sampling_policy"] == {}


@pytest.mark.parametrize("package_name", ["dark.Tozhn.wzmtH", "dark.FiVQo.sMyUI"])
@pytest.mark.parametrize(
    ("endpoint", "entry_type", "dependency", "execution_mode"),
    [
        (
            "/api/v2/tasks",
            TaskSubmissionV2EntryService,
            v2_tasks_module._get_full_task_entry,
            "full_inspection",
        ),
        (
            "/api/v2/exploration-tasks",
            ExplorationTaskV2EntryService,
            v2_tasks_module._get_exploration_task_entry,
            "exploration_only",
        ),
    ],
)
def test_app_submission_accepts_mixed_case_package_with_consistent_target_hash(
    package_name: str,
    endpoint: str,
    entry_type,
    dependency,
    execution_mode: str,
) -> None:
    """真实入口须保持安装包名原样，仅规范化冻结目标的包名与哈希。"""

    submission = RecordingSubmissionService()
    entry = entry_type(submission)
    app = create_app()
    app.dependency_overrides[dependency] = lambda: entry
    client = TestClient(app, raise_server_exceptions=False)
    payload = _app_task_payload()
    payload["target"]["package_name"] = package_name
    payload["target"]["exploration_target"] = {"required_page_types": []}
    payload.pop("journey_steps")

    response = client.post(endpoint, json=payload)

    assert response.status_code == 202, response.text
    assert len(submission.calls) == 1
    call = submission.calls[0]
    assert call["target"].app.package_name == package_name
    assert call["task_config"]["execution_mode"] == execution_mode
    snapshot = AppExplorationTargetSnapshot.from_dict(call["task_config"]["app_exploration_target"])
    assert snapshot.allowed_packages == (package_name.casefold(),)
    assert snapshot.completion_mode == "dynamic_discovery"
    assert snapshot.target_hash == snapshot.recompute_target_hash()


async def test_full_v2_entry_locks_custom_app_target_with_content_hash() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission)
    payload = _app_task_payload()
    payload["target"]["exploration_target"] = {
        "required_page_types": ["HOME", "content_detail", "HOME"],
        "milestones": ["journey:登录", "surface:content_detail"],
        "minimum_business_pages": 4,
        "device_capability_template": "android_visual_evidence_v1",
    }
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    snapshot = AppExplorationTargetSnapshot.from_dict(
        submission.calls[0]["task_config"]["app_exploration_target"]
    )
    assert snapshot.required_page_types == ("home", "content_detail")
    assert snapshot.required_milestones == (
        "journey:登录",
        "surface:content_detail",
    )
    assert snapshot.minimum_business_pages == 4
    assert snapshot.device_capability_template == "android_visual_evidence_v1"
    assert snapshot.target_hash == snapshot.recompute_target_hash()


async def test_full_v2_entry_keeps_known_package_in_dynamic_discovery() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission)
    payload = _app_task_payload()
    payload["target"]["package_name"] = "app.paf4c0.pede1e12de8c2f952a9aa"
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    snapshot = AppExplorationTargetSnapshot.from_dict(
        submission.calls[0]["task_config"]["app_exploration_target"]
    )
    assert snapshot.profile_id is None
    assert snapshot.applicability_mode == "dynamic_discovery"
    assert snapshot.completion_mode == "dynamic_discovery"
    assert snapshot.minimum_business_pages == 1
    assert snapshot.optional_page_types == ()
    assert snapshot.lifecycle_page_types == ()
    assert snapshot.allowed_packages == ("app.paf4c0.pede1e12de8c2f952a9aa",)
    assert snapshot.target_hash == snapshot.recompute_target_hash()


async def test_full_v2_entry_passes_explicit_app_applicability_groups() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission)
    payload = _app_task_payload()
    payload["target"]["exploration_target"] = {
        "required_page_types": ["HOME"],
        "optional_page_types": ["PROFILE"],
        "lifecycle_page_types": ["LINE_LOADING"],
        "applicability_mode": "explicit_targets",
    }
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    assert command.optional_page_types == ("profile",)
    assert command.lifecycle_page_types == ("line_loading",)
    assert command.applicability_mode == "explicit_targets"
    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    snapshot = AppExplorationTargetSnapshot.from_dict(
        submission.calls[0]["task_config"]["app_exploration_target"]
    )
    assert snapshot.required_page_types == ("home",)
    assert snapshot.optional_page_types == ("profile",)
    assert snapshot.lifecycle_page_types == ("line_loading",)
    assert snapshot.applicability_mode == "explicit_targets"
    assert submission.calls[0]["task_config"]["inspection_scope"] == {"page_types": ["home"]}


def test_v2_app_target_rejects_unknown_or_lifecycle_page_types() -> None:
    payload = _app_task_payload()
    payload["target"]["exploration_target"] = {
        "required_page_types": ["line_loading", "custom_app_home"],
    }

    with pytest.raises(ValueError, match="业务页面类型目录"):
        V2TaskSubmissionRequest.model_validate(payload)


def test_v2_app_target_rejects_overlapping_required_and_optional_page_types() -> None:
    payload = _app_task_payload()
    payload["target"]["exploration_target"] = {
        "required_page_types": ["home"],
        "optional_page_types": ["HOME"],
    }

    with pytest.raises(ValueError, match="不能交叉"):
        V2TaskSubmissionRequest.model_validate(payload)


async def test_full_v2_entry_uses_quick_scan_runtime_budget() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission)
    payload = _app_task_payload()
    payload["scan_mode"] = "quick"
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls[0]["loop_guard"] == {
        "max_depth": 2,
        "max_duration_seconds": UNLIMITED_DURATION_SECONDS,
        "max_steps": 40,
        "unlimited_duration": 1,
    }
    assert submission.calls[0]["page_sampling_policy"] == {
        "mode": "first_n",
        "max_pages": 3,
    }


async def test_full_v2_h5_entry_keeps_quick_scan_runtime_budget() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, h5_v2_mode="on")
    payload = _h5_task_payload()
    payload["scan_mode"] = "quick"
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls[0]["loop_guard"] == {
        "max_depth": 2,
        "max_duration_seconds": 900,
        "max_steps": 40,
        "max_ai_calls": 64,
    }
    assert submission.calls[0]["page_sampling_policy"] == {
        "mode": "first_n",
        "max_pages": 3,
    }
    assert submission.calls[0]["task_config"]["h5_exploration_target"] == {
        "required_page_types": [],
    }


@pytest.mark.asyncio
async def test_exploration_v2_entry_submits_h5_without_formal_inspection() -> None:
    """仅探索入口必须创建真实 H5 Run，并锁定无正式质检副作用的模式。"""

    submission = RecordingSubmissionService()
    entry = ExplorationTaskV2EntryService(submission, h5_v2_mode="on")
    command = V2TaskSubmissionRequest.model_validate(_h5_task_payload()).to_command()

    result = await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert result == ExplorationTaskSubmissionResult(
        task_id=submission.result.task_id,
        run_id=submission.result.run_id,
        status=submission.result.status,
    )
    assert len(submission.calls) == 1
    call = submission.calls[0]
    assert call["target"].platform.value == "h5"
    assert call["target"].target_url == "https://example.test/h5"
    assert call["task_config"]["workflow_version"] == "v2"
    assert call["task_config"]["execution_mode"] == "exploration_only"
    assert call["loop_guard"] == {
        "max_depth": 4,
        "max_duration_seconds": 5400,
        "max_steps": 80,
        "max_ai_calls": 200,
    }


@pytest.mark.asyncio
async def test_exploration_v2_entry_submits_app_without_formal_inspection() -> None:
    """App 仅探索入口必须锁定 App 目标和 exploration_only 模式。"""

    submission = RecordingSubmissionService()
    entry = ExplorationTaskV2EntryService(submission, h5_v2_mode="on")
    command = V2TaskSubmissionRequest.model_validate(_app_task_payload()).to_command()

    result = await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert result == ExplorationTaskSubmissionResult(
        task_id=submission.result.task_id,
        run_id=submission.result.run_id,
        status=submission.result.status,
    )
    call = submission.calls[0]
    assert call["target"].platform.value == "app"
    assert call["target"].app.platform_build_ref == APP_BUILD_REF
    assert call["task_config"]["workflow_version"] == "v2"
    assert call["task_config"]["execution_mode"] == "exploration_only"
    snapshot = AppExplorationTargetSnapshot.from_dict(call["task_config"]["app_exploration_target"])
    assert snapshot.completion_mode == "dynamic_discovery"
    assert snapshot.required_milestones == ("surface:stable_business_page",)
    assert snapshot.minimum_business_pages == 1


@pytest.mark.asyncio
async def test_exploration_v2_entry_app_ignores_explicit_180_minute_budget() -> None:
    from services.api.app.application.v2_task_entries import UNLIMITED_DURATION_SECONDS
    submission = RecordingSubmissionService()
    entry = ExplorationTaskV2EntryService(submission, h5_v2_mode="on")
    payload = _app_task_payload()
    payload["timeout_minutes"] = 180
    payload["target"]["exploration_target"] = {"max_actions": 100}
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    assert command.timeout_minutes == 180
    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls[0]["loop_guard"] == {
        "max_depth": 4,
        "max_duration_seconds": UNLIMITED_DURATION_SECONDS,
        "max_steps": None,
        "unlimited_duration": 1,
        "max_actions": 100,
    }


@pytest.mark.asyncio
async def test_exploration_v2_entry_unlimited_duration_ignores_150_minute_cap() -> None:
    from services.api.app.application.v2_task_entries import UNLIMITED_DURATION_SECONDS

    submission = RecordingSubmissionService()
    entry = ExplorationTaskV2EntryService(submission, h5_v2_mode="on")
    payload = _app_task_payload()
    payload["unlimited_duration"] = True
    payload.pop("timeout_minutes", None)
    payload["target"]["exploration_target"] = {"max_actions": 600}
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    assert command.unlimited_duration is True
    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls[0]["loop_guard"]["max_duration_seconds"] == UNLIMITED_DURATION_SECONDS
    assert submission.calls[0]["loop_guard"]["unlimited_duration"] == 1
    assert submission.calls[0]["loop_guard"]["max_actions"] == 600


@pytest.mark.parametrize("timeout_minutes", [0, 181])
def test_v2_entry_rejects_timeout_outside_public_limit(timeout_minutes: int) -> None:
    payload = _app_task_payload()
    payload["timeout_minutes"] = timeout_minutes

    with pytest.raises(ValueError):
        V2TaskSubmissionRequest.model_validate(payload)


@pytest.mark.parametrize("value", [181, True, 1.5, "180"])
def test_v2_timeout_policy_rejects_invalid_value(value) -> None:
    with pytest.raises(ValueError, match="1 到 180"):
        _scan_mode_loop_guard(
            "full",
            platform=TaskTargetPlatform.APP,
            timeout_minutes=value,
        )


@pytest.mark.parametrize("max_actions", [0, 601])
def test_v2_entry_rejects_app_action_budget_outside_hard_limit(max_actions: int) -> None:
    payload = _app_task_payload()
    payload["target"]["exploration_target"] = {"max_actions": max_actions}

    with pytest.raises(ValueError):
        V2TaskSubmissionRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_app_v2_entries_lock_the_same_exploration_contract() -> None:
    """两个 App 入口只能在探索完成后的质检模式上不同。"""

    full_submission = RecordingSubmissionService()
    exploration_submission = RecordingSubmissionService()
    command = V2TaskSubmissionRequest.model_validate(_app_task_payload()).to_command()

    await TaskSubmissionV2EntryService(full_submission).submit(
        command,
        access_scope=TEST_ACCESS_SCOPE,
    )
    await ExplorationTaskV2EntryService(exploration_submission).submit(
        command,
        access_scope=TEST_ACCESS_SCOPE,
    )

    full = dict(full_submission.calls[0])
    exploration = dict(exploration_submission.calls[0])
    assert full.pop("submission_kind") == "v2_full_task"
    assert exploration.pop("submission_kind") == "v2_exploration_task"
    full_config = dict(full["task_config"])
    exploration_config = dict(exploration["task_config"])
    assert full_config.pop("execution_mode") == "full_inspection"
    assert exploration_config.pop("execution_mode") == "exploration_only"
    assert full_config == exploration_config
    full["task_config"] = full_config
    exploration["task_config"] = exploration_config
    assert full == exploration


@pytest.mark.asyncio
async def test_exploration_v2_entry_accepts_web_only_through_web_mode() -> None:
    """Web 仅探索入口只依赖独立 Web 门禁并冻结 Browser Profile。"""

    submission = RecordingSubmissionService()
    declared_at = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
    entry = ExplorationTaskV2EntryService(
        submission,
        h5_v2_mode="off",
        web_v2_mode="exploration_only",
        now=lambda: declared_at,
    )
    command = V2TaskSubmissionRequest.model_validate(_web_task_payload()).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    call = submission.calls[0]
    assert call["target"].platform.value == "web"
    assert call["target"].web.browser_profile_id == "desktop_fhd_1920x1080_v1"
    assert call["target"].web.adult_self_attestation.declared_by == "test-user"
    assert call["target"].web.adult_self_attestation.declared_at == declared_at
    assert call["task_config"]["execution_mode"] == "exploration_only"
    assert call["task_config"]["web_inspection_notification_mode"] == "off"
    assert call["task_config"]["browser_profile_snapshot"] == {
        "profile_id": "desktop_fhd_1920x1080_v1",
        "viewport_width": 1920,
        "viewport_height": 1080,
        "screen_width": 1920,
        "screen_height": 1080,
        "device_scale_factor": 1.0,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": "light",
    }
    assert call["loop_guard"] == {
        "max_depth": 4,
        "max_duration_seconds": 10_800,
        "max_steps": None,
    }


@pytest.mark.asyncio
async def test_environment_adult_attestation_applies_without_account_or_request() -> None:
    submission = RecordingSubmissionService()
    configured = AdultSelfAttestation(
        declared=True,
        statement_version="adult-v1",
        declared_by="environment-config",
        declared_at=datetime(2026, 9, 11, tzinfo=UTC),
    )
    entry = ExplorationTaskV2EntryService(
        submission,
        web_v2_mode="exploration_only",
        environment_adult_self_attestation=configured,
    )
    payload = _web_task_payload()
    payload["target"].pop("adult_self_attestation")
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    frozen = submission.calls[0]["target"].web.adult_self_attestation
    assert frozen == configured
    assert frozen.declared_by == "environment-config"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scan_mode", "expected_seconds", "expected_steps"),
    (
        ("custom", 10_800, None),
        ("quick", 900, 40),
    ),
)
async def test_web_v2_scan_budgets_remain_bounded_and_platform_local(
    scan_mode: str,
    expected_seconds: int,
    expected_steps: int | None,
) -> None:
    submission = RecordingSubmissionService()
    entry = ExplorationTaskV2EntryService(
        submission,
        h5_v2_mode="off",
        web_v2_mode="exploration_only",
    )
    payload = _web_task_payload()
    payload["scan_mode"] = scan_mode
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls[0]["loop_guard"] == {
        "max_depth": 3 if scan_mode == "custom" else 2,
        "max_duration_seconds": expected_seconds,
        "max_steps": expected_steps,
    }


@pytest.mark.asyncio
async def test_web_selected_page_types_become_inspection_scope_without_forced_home() -> None:
    submission = RecordingSubmissionService()
    entry = ExplorationTaskV2EntryService(
        submission,
        h5_v2_mode="off",
        web_v2_mode="exploration_only",
    )
    payload = _web_task_payload()
    payload["target"]["exploration_target"] = {"required_page_types": ["video_list"]}
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    task_config = submission.calls[0]["task_config"]
    assert task_config["inspection_scope"] == {"page_types": ["video_list"]}
    assert task_config["web_exploration_target"]["required_page_types"] == ["video_list"]


@pytest.mark.asyncio
async def test_web_exploration_rejects_disabled_web_mode() -> None:
    submission = RecordingSubmissionService()
    entry = ExplorationTaskV2EntryService(submission, web_v2_mode="off")
    command = V2TaskSubmissionRequest.model_validate(_web_task_payload()).to_command()

    with pytest.raises(WebV2ModeNotEnabledError, match="WEB_V2_DISABLED"):
        await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls == []


@pytest.mark.asyncio
async def test_web_full_inspection_rejects_exploration_only_mode() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(
        submission,
        web_v2_mode="exploration_only",
    )
    command = V2TaskSubmissionRequest.model_validate(_web_task_payload()).to_command()

    with pytest.raises(WebV2ModeNotEnabledError, match="WEB_FULL_INSPECTION_DISABLED"):
        await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls == []


@pytest.mark.asyncio
async def test_full_v2_entry_accepts_web_only_when_formal_runtime_is_on() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(
        submission,
        web_v2_mode="on",
    )
    payload = _web_task_payload()
    payload["target"].pop("adult_self_attestation")
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    call = submission.calls[0]
    assert call["target"].platform.value == "web"
    assert call["target"].web.browser_profile_id == "desktop_fhd_1920x1080_v1"
    assert call["target"].web.adult_self_attestation is None
    assert call["task_config"]["execution_mode"] == "full_inspection"
    assert call["task_config"]["web_inspection_notification_mode"] == "enforced"
    assert call["task_config"]["h5_inspection_notification_mode"] == "enforced"
    assert call["task_config"]["browser_profile_snapshot"] == {
        "profile_id": "desktop_fhd_1920x1080_v1",
        "viewport_width": 1920,
        "viewport_height": 1080,
        "screen_width": 1920,
        "screen_height": 1080,
        "device_scale_factor": 1.0,
        "is_mobile": False,
        "has_touch": False,
        "color_scheme": "light",
    }
    assert call["submission_kind"] == "v2_full_task"


@pytest.mark.asyncio
async def test_full_v2_entry_rejects_web_in_exploration_only_deployment() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, web_v2_mode="exploration_only")
    payload = _web_task_payload()
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    with pytest.raises(WebV2ModeNotEnabledError, match="WEB_FULL_INSPECTION_DISABLED"):
        await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls == []


@pytest.mark.asyncio
async def test_unknown_web_target_locks_dynamic_home_denominator() -> None:
    """未知站点只固定首页，其余页型等待运行时证据。"""

    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, h5_v2_mode="on", web_v2_mode="on")
    payload = _web_task_payload()
    command = V2TaskSubmissionRequest.model_validate(payload).to_command()

    assert command.required_page_types == ("home",)

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    assert submission.calls[0]["task_config"]["web_exploration_target"] == {
        "browser_profile_id": "desktop_fhd_1920x1080_v1",
        "required_page_types": ["home"],
        "optional_page_types": [
            "activity",
            "collection_page",
            "content_detail",
            "content_list",
            "search_page",
            "video_detail",
            "video_list",
        ],
        "not_applicable_page_types": [],
        "applicability_mode": "runtime_dynamic",
        "catalog_version": ANY,
        "catalog_hash": ANY,
        "resolution_reason": "runtime_evidence_initial",
        "product_capabilities": [],
        "page_type_statuses": {
            "home": "required",
            "content_list": "not_observed",
            "content_detail": "not_observed",
            "video_list": "not_observed",
            "video_detail": "not_observed",
            "activity": "not_observed",
            "search_page": "not_observed",
            "collection_page": "not_observed",
        },
    }


@pytest.mark.asyncio
async def test_known_web_target_also_uses_runtime_dynamic_denominator() -> None:
    submission = RecordingSubmissionService()
    entry = TaskSubmissionV2EntryService(submission, h5_v2_mode="on", web_v2_mode="on")
    command = V2TaskSubmissionRequest.model_validate(
        _web_task_payload(url="https://avhub360.com/")
    ).to_command()

    await entry.submit(command, access_scope=TEST_ACCESS_SCOPE)

    target = submission.calls[0]["task_config"]["web_exploration_target"]
    assert target["required_page_types"] == ["home"]
    assert "video_list" in target["optional_page_types"]
    assert target["not_applicable_page_types"] == []
    assert target["applicability_mode"] == "runtime_dynamic"
    assert target["resolution_reason"] == "runtime_evidence_initial"


def test_web_target_rejects_explicit_strict_four_mode() -> None:
    payload = _web_task_payload(url="https://avhub360.com/")
    payload["target"]["exploration_target"] = {
        "applicability_mode": "strict_four",
    }

    with pytest.raises(ValidationError, match="WEB_LEGACY_EXECUTION_OVERRIDE_FORBIDDEN"):
        V2TaskSubmissionRequest.model_validate(payload)


def test_web_target_rejects_prefixed_legacy_page_types() -> None:
    """新 Web 入口接受正式 home，但拒绝带 web_ 前缀的旧页型。"""

    payload = _web_task_payload()
    payload["target"]["exploration_target"] = {
        "required_page_types": [
            "home",
            "web_video_list",
            "video_list",
            "web_content_detail",
        ]
    }
    with pytest.raises(ValueError, match="业务页面类型目录"):
        V2TaskSubmissionRequest.model_validate(payload)


def test_web_target_rejects_unknown_page_types() -> None:
    payload = _web_task_payload()
    payload["target"]["exploration_target"] = {"required_page_types": ["web_post_list"]}

    with pytest.raises(ValueError, match="业务页面类型目录"):
        V2TaskSubmissionRequest.model_validate(payload)


def test_exploration_endpoint_accepts_app_target() -> None:
    """HTTP 探索入口必须允许 App，并交给统一应用服务。"""

    submission = RecordingSubmissionService()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_exploration_task_entry] = lambda: (
        ExplorationTaskV2EntryService(submission, h5_v2_mode="on")
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/v2/exploration-tasks", json=_app_task_payload())

    assert response.status_code == 202
    assert response.json() == {
        "task_id": submission.result.task_id,
        "run_id": submission.result.run_id,
        "status": submission.result.status,
    }
    assert submission.calls[0]["target"].platform.value == "app"


def test_exploration_endpoint_maps_disabled_h5_mode_to_structured_conflict() -> None:
    """仅探索 H5 也必须在 API 入库前执行正式模式门禁。"""

    submission = RecordingSubmissionService()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_exploration_task_entry] = lambda: (
        ExplorationTaskV2EntryService(submission, h5_v2_mode="shadow")
    )
    client = TestClient(app, raise_server_exceptions=False)

    response = client.post("/api/v2/exploration-tasks", json=_h5_task_payload())

    _assert_structured_error(
        response,
        code="H5V2ModeNotEnabledError",
        status_code=409,
    )
    assert submission.calls == []


def test_exploration_task_default_factory_builds_real_h5_service() -> None:
    """默认工厂必须装配真实仅探索入口，不能继续返回占位实现。"""

    v2_tasks_module._get_exploration_task_entry.cache_clear()
    try:
        entry = v2_tasks_module._get_exploration_task_entry()
        assert isinstance(entry, ExplorationTaskV2EntryService)
    finally:
        v2_tasks_module._get_exploration_task_entry.cache_clear()


def test_rule_evaluation_default_factory_builds_real_service() -> None:
    """默认工厂应组装独立规则质检编排服务，且不再返回占位实现。"""
    from services.api.app.application.rule_evaluation_runs import RuleEvaluationRunService

    v2_tasks_module._get_rule_evaluation_entry.cache_clear()
    try:
        entry = v2_tasks_module._get_rule_evaluation_entry()
        assert isinstance(entry, RuleEvaluationRunService)
    finally:
        v2_tasks_module._get_rule_evaluation_entry.cache_clear()


def test_rule_evaluation_unconfigured_entry_still_maps_to_structured_501() -> None:
    """501 错误映射契约保留：注入占位实现时入口仍结构化失败。"""
    from services.api.app.application.v2_task_entries import UnconfiguredRuleEvaluationEntry

    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_rule_evaluation_entry] = (
        UnconfiguredRuleEvaluationEntry
    )
    client = TestClient(app)

    response = client.post(
        "/api/v2/tasks/task-v2/rule-evaluation-runs",
        json={},
    )

    _assert_structured_error(
        response,
        code="RuleEvaluationPipelineNotConnectedError",
    )


def test_rule_evaluation_unknown_task_maps_to_404() -> None:
    """任务不存在时映射为 404，而不是启动运行。"""
    from services.api.app.application.rule_evaluation_runs import (
        RuleEvaluationTaskNotFoundError,
    )

    class _MissingTaskEntry:
        async def submit(self, task_id: str, *, rule_codes=None, access_scope):
            assert access_scope is not None
            raise RuleEvaluationTaskNotFoundError(f"任务 {task_id} 不存在")

    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_rule_evaluation_entry] = _MissingTaskEntry
    client = TestClient(app)

    response = client.post(
        "/api/v2/tasks/task-missing/rule-evaluation-runs",
        json={},
    )

    assert response.status_code == 404


def test_v2_task_entrypoints_require_authentication() -> None:
    app = create_app()
    pop_test_auth(app)
    client = TestClient(app)

    response = client.post("/api/v2/tasks", json=_app_task_payload())

    assert response.status_code == 401
    assert response.json()["error"]["message"] == "缺少登录令牌"


def test_rule_evaluation_accepts_rule_codes_body() -> None:
    entry = RecordingRuleEvaluationEntry()
    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_rule_evaluation_entry] = lambda: entry
    client = TestClient(app)

    response = client.post(
        "/api/v2/tasks/task-x/rule-evaluation-runs",
        json={"rule_codes": ["alioth_1", "alioth_2"]},
    )

    assert response.status_code == 202
    assert entry.task_ids == ["task-x"]
    assert entry.rule_codes == [("alioth_1", "alioth_2")]
    assert len(entry.access_scopes) == 1
    assert entry.access_scopes[0].actor_user_id == "test-user"
    assert entry.access_scopes[0].tenant_id == "tenant-1"


def test_rule_evaluation_run_query_endpoints() -> None:
    from datetime import UTC, datetime

    from services.api.app.application.rule_evaluation_runs import (
        RuleEvaluationRun,
        RuleEvaluationRunStatus,
    )

    run = RuleEvaluationRun(
        run_id="rule-eval:abc",
        task_id="task-x",
        status=RuleEvaluationRunStatus.SUCCEEDED,
        page_count=2,
        rule_count=3,
        requested_rule_codes=("alioth_1",),
        result_payload=({"rule_code": "alioth_1", "execution_status": "completed"},),
        created_at=datetime(2026, 8, 11, 15, 0, tzinfo=UTC),
        finished_at=datetime(2026, 8, 11, 15, 1, tzinfo=UTC),
    )

    class _Repo:
        async def get(self, *, run_id: str, task_id: str, access_scope):
            assert access_scope is not None
            if task_id != run.task_id:
                return None
            return run if run_id == run.run_id else None

        async def list_for_task(self, *, task_id: str, access_scope):
            assert access_scope is not None
            return [run] if task_id == run.task_id else []

        async def insert_pending(self, run, *, access_scope) -> None: ...

        async def mark_running(self, *, run_id: str) -> None: ...

        async def finish(self, **kwargs) -> None: ...

    app = create_app()
    app.dependency_overrides[v2_tasks_module._get_rule_evaluation_run_repository] = _Repo
    client = TestClient(app)

    listing = client.get("/api/v2/tasks/task-x/rule-evaluation-runs")
    assert listing.status_code == 200
    body = listing.json()
    assert body["task_id"] == "task-x"
    assert [item["run_id"] for item in body["runs"]] == ["rule-eval:abc"]
    assert body["runs"][0]["requested_rule_codes"] == ["alioth_1"]
    assert body["runs"][0]["status"] == "succeeded"

    detail = client.get("/api/v2/tasks/task-x/rule-evaluation-runs/rule-eval:abc")
    assert detail.status_code == 200
    assert detail.json()["results"][0]["rule_code"] == "alioth_1"

    mismatch = client.get("/api/v2/tasks/other-task/rule-evaluation-runs/rule-eval:abc")
    assert mismatch.status_code == 404
