"""V2 统一任务入口的应用层合同与当前可用适配。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Protocol

from services.api.app.application.app_exploration_target import (
    build_app_exploration_target_snapshot,
)
from services.api.app.application.h5_task_runtime_settings import (
    h5_scan_mode_runtime_limits,
)
from services.api.app.application.task_submission import (
    TaskSubmissionResult,
    TaskSubmissionService,
)
from services.api.app.application.web_browser_profile import WebBrowserProfileRegistry
from services.api.app.application.web_page_applicability import (
    WebPageApplicabilitySnapshot,
)
from services.api.app.application.workstation_tasks import (
    FULL_SCAN_TIMEOUT_MINUTES,
    H5_FULL_SCAN_TIMEOUT_MINUTES,
    QUICK_SCAN_TIMEOUT_MINUTES,
    resolve_h5_required_page_types,
    scan_mode_page_sampling_policy,
)
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.task import (
    AdultSelfAttestation,
    AppTaskConfig,
    H5InspectionNotificationMode,
    TaskExecutionMode,
    TaskTarget,
    TaskTargetPlatform,
    WebTaskConfig,
    lock_h5_inspection_notification_mode,
    lock_task_execution_mode,
)
from services.api.app.domain.task.inspection_scope import (
    INSPECTION_SCOPE_CONFIG_KEY,
    build_inspection_scope_config,
)

WEB_FULL_SCAN_TIMEOUT_MINUTES = 180
V2_TIMEOUT_MAX_MINUTES = 180
# 不按 150 分鐘收口。牆鐘欄位仍要是正整數，取一個質檢裡不會到期的值。
UNLIMITED_DURATION_SECONDS = 10 * 365 * 24 * 60 * 60


class AppArtifactSourceKind(StrEnum):
    """App 制品来源类型。"""

    BUILD_REF = "build_ref"
    DOWNLOAD_URL = "download_url"


@dataclass(frozen=True)
class AppArtifactSource:
    """App 制品来源；下载链接由后续制品接入方转换为稳定引用。"""

    kind: AppArtifactSourceKind
    platform_build_ref: str | None = None
    download_url: str | None = None


@dataclass(frozen=True)
class ProductSnapshot:
    """随任务锁定的产品展示快照。"""

    department_name: str
    product_code: str
    product_name: str


@dataclass(frozen=True)
class V2TaskSubmissionCommand:
    """三个 V2 任务入口共用的稳定应用命令。"""

    task_name: str
    platform: TaskTargetPlatform
    target_url: str | None
    app_source: AppArtifactSource | None
    package_name: str | None
    device_backend: str
    device_profile_id: str
    # 单个规则集（老调用方）；为空时按 ruleset_codes 或全部开启的规则集执行
    ruleset_code: str | None
    ruleset_version: int | None
    channel: str
    priority: int
    product: ProductSnapshot
    scan_mode: str
    timeout_minutes: int | None
    journey_steps: tuple[str, ...]
    required_page_types: tuple[str, ...]
    required_milestones: tuple[str, ...]
    minimum_business_pages: int | None
    device_capability_template: str
    remark: str | None
    app_max_actions: int | None = None
    browser_profile_id: str | None = None
    adult_self_attestation_declared: bool | None = None
    adult_self_attestation_statement_version: str | None = None
    optional_page_types: tuple[str, ...] = ()
    lifecycle_page_types: tuple[str, ...] = ()
    applicability_mode: str | None = None
    web_applicability_snapshot: WebPageApplicabilitySnapshot | None = None
    web_discovery_policy: dict[str, object] | None = None
    # 同时执行的规则集编码；为空且没指定 ruleset_code 时跑全部开启的规则集
    ruleset_codes: tuple[str, ...] = ()
    unlimited_duration: bool = False
    # 用户在「页面范围」里原样选的页面类型（质检范围）；为空表示全部
    inspection_scope_page_types: tuple[str, ...] = ()
    h5_shortflows: tuple[dict[str, object], ...] = ()


@dataclass(frozen=True)
class ExplorationTaskSubmissionResult:
    """页面探索测试任务提交结果。"""

    task_id: str
    run_id: str
    status: str


@dataclass(frozen=True)
class RuleEvaluationSubmissionResult:
    """独立规则质检运行提交结果。"""

    task_id: str
    rule_evaluation_run_id: str
    status: str


class V2TaskEntryPort(Protocol):
    """完整 V2 任务入口。"""

    async def submit(
        self,
        command: V2TaskSubmissionCommand,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> TaskSubmissionResult:
        """提交一次完整 V2 任务。"""


class ExplorationTaskEntryPort(Protocol):
    """只运行页面探索和证据留存的任务入口。"""

    async def submit(
        self,
        command: V2TaskSubmissionCommand,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> ExplorationTaskSubmissionResult:
        """提交一次页面探索测试任务。"""


class RuleEvaluationEntryPort(Protocol):
    """基于既有任务证据运行独立规则质检的入口。"""

    async def submit(
        self,
        task_id: str,
        *,
        rule_codes: tuple[str, ...] | None = None,
        access_scope: AccessScope,
    ) -> RuleEvaluationSubmissionResult:
        """提交一次独立规则质检运行；rule_codes 为空表示测全部命中规则。"""


class AppArtifactSourceResolverPort(Protocol):
    """把 App 外部来源转换为既有稳定制品引用。"""

    async def resolve(self, source: AppArtifactSource) -> str:
        """返回 app-build:<uuid> 格式的稳定引用。"""


class AppTaskTargetResolver:
    """把统一入口命令转换为稳定的 App TaskTarget。

    完整质检与仅页面探索必须共享这一层，避免两个入口对制品来源、设备
    配置和稳定 build ref 的校验逐渐漂移。
    """

    def __init__(
        self,
        artifact_source_resolver: AppArtifactSourceResolverPort | None = None,
    ) -> None:
        self._artifact_source_resolver = (
            artifact_source_resolver or UnconfiguredAppArtifactSourceResolver()
        )

    async def resolve(self, command: V2TaskSubmissionCommand) -> TaskTarget:
        """把 App 外部制品来源转换为已验证的稳定任务目标。"""

        source = command.app_source
        if source is None:
            raise AppArtifactSourceNotConfiguredError("App 任务缺少制品来源")
        platform_build_ref = source.platform_build_ref
        if source.kind is AppArtifactSourceKind.DOWNLOAD_URL:
            platform_build_ref = await self._artifact_source_resolver.resolve(source)
        if not platform_build_ref:
            raise AppArtifactSourceNotConfiguredError("App 任务缺少构建制品引用")

        return TaskTarget(
            platform=TaskTargetPlatform.APP,
            app=AppTaskConfig(
                platform_build_ref=platform_build_ref,
                package_name=command.package_name,
                device_backend=command.device_backend,
                device_profile_id=command.device_profile_id,
            ),
        )


class V2PlatformHandlerNotConfiguredError(RuntimeError):
    """目标平台尚未接入完整 V2 运行时。"""


class H5V2ModeNotEnabledError(RuntimeError):
    """H5 V2 正式运行模式未开启。"""


class WebV2ModeNotEnabledError(RuntimeError):
    """Web V2 请求不满足独立部署门禁。"""


class AppArtifactSourceNotConfiguredError(RuntimeError):
    """App 下载链接尚未接入制品导入逻辑。"""


class ExplorationPipelineNotConfiguredError(RuntimeError):
    """页面探索测试入口尚未接入下游流程。"""


class RuleEvaluationPipelineNotConfiguredError(RuntimeError):
    """独立规则质检入口尚未接入下游流程。"""


class UnconfiguredAppArtifactSourceResolver:
    """App 下载链接导入的安全占位适配器。"""

    async def resolve(self, source: AppArtifactSource) -> str:
        # 后续接入逻辑：
        # 1. 对 download_url 做 HTTP(S)、内网地址、重定向、超时和大小限制校验。
        # 2. 下载完成后复用现有 PlatformBuildService 校验并保存 APK 制品。
        # 3. 只返回已经验证的 app-build:<uuid> 引用，禁止把原始 URL 交给设备运行时。
        # 4. 接入方需要补齐下载审计、哈希校验、失败清理和重复请求幂等。
        del source
        raise AppArtifactSourceNotConfiguredError("App 下载链接导入逻辑尚未接入")


class TaskSubmissionV2EntryService:
    """把已接入的 H5/App V2 请求交给既有任务提交服务。"""

    def __init__(
        self,
        submission_service: TaskSubmissionService,
        artifact_source_resolver: AppArtifactSourceResolverPort | None = None,
        *,
        h5_v2_mode: Literal["off", "shadow", "on"] = "off",
        web_v2_mode: Literal["off", "exploration_only", "on"] = "off",
        h5_inspection_notification_mode: Literal["off", "shadow", "enforced"] = "off",
        environment_adult_self_attestation: AdultSelfAttestation | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._submission_service = submission_service
        self._app_target_resolver = AppTaskTargetResolver(artifact_source_resolver)
        if h5_v2_mode not in {"off", "shadow", "on"}:
            raise ValueError("h5_v2_mode 不受支持")
        if web_v2_mode not in {"off", "exploration_only", "on"}:
            raise ValueError("web_v2_mode 不受支持")
        self._h5_v2_mode = h5_v2_mode
        if web_v2_mode not in {"off", "exploration_only", "on"}:
            raise ValueError("web_v2_mode 不受支持")
        self._web_v2_mode = web_v2_mode
        self._h5_inspection_notification_mode = H5InspectionNotificationMode(
            h5_inspection_notification_mode
        )
        self._environment_adult_self_attestation = environment_adult_self_attestation
        self._now = now or (lambda: datetime.now(UTC))

    async def submit(
        self,
        command: V2TaskSubmissionCommand,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> TaskSubmissionResult:
        if command.platform is TaskTargetPlatform.H5:
            if self._h5_v2_mode != "on":
                raise H5V2ModeNotEnabledError(
                    "H5 V2 正式运行模式未开启，请配置 ALIOTH_H5_V2_MODE=on"
                )
            target = TaskTarget(
                platform=TaskTargetPlatform.H5,
                target_url=command.target_url,
            )
        elif command.platform is TaskTargetPlatform.APP:
            target = await self._build_app_target(command)
        elif command.platform is TaskTargetPlatform.WEB:
            if self._web_v2_mode == "off":
                raise WebV2ModeNotEnabledError(
                    "WEB_V2_DISABLED: 请配置 ALIOTH_WEB_V2_MODE"
                )
            if self._web_v2_mode != "on":
                raise WebV2ModeNotEnabledError(
                    "WEB_FULL_INSPECTION_DISABLED: 当前只允许 Web 页面探索"
                )
            target = _build_web_target(
                command,
                access_scope=access_scope,
                declared_at=self._now(),
                environment_adult_self_attestation=(
                    self._environment_adult_self_attestation
                ),
            )
        else:
            raise V2PlatformHandlerNotConfiguredError(
                f"{command.platform.value} 平台尚未接入完整 V2 运行时"
            )

        return await _submit_shared_exploration_task(
            self._submission_service, command,
            target=target, access_scope=access_scope, idempotency_key=idempotency_key,
            execution_mode=TaskExecutionMode.FULL_INSPECTION,
            h5_inspection_notification_mode=self._h5_inspection_notification_mode,
            web_inspection_notification_mode=(
                H5InspectionNotificationMode.ENFORCED
                if command.platform is TaskTargetPlatform.WEB
                else H5InspectionNotificationMode.OFF
            ),
        )

    async def _build_app_target(self, command: V2TaskSubmissionCommand) -> TaskTarget:
        """把 App 外部制品来源转换为已验证的稳定任务目标。"""
        return await self._app_target_resolver.resolve(command)


class ExplorationTaskV2EntryService:
    """提交只执行页面探索和证据留存的 V2 任务。"""

    def __init__(
        self,
        submission_service: TaskSubmissionService,
        artifact_source_resolver: AppArtifactSourceResolverPort | None = None,
        *,
        h5_v2_mode: Literal["off", "shadow", "on"] = "off",
        web_v2_mode: Literal["off", "exploration_only", "on"] = "off",
        h5_inspection_notification_mode: Literal["off", "shadow", "enforced"] = "off",
        environment_adult_self_attestation: AdultSelfAttestation | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._submission_service = submission_service
        self._app_target_resolver = AppTaskTargetResolver(artifact_source_resolver)
        if h5_v2_mode not in {"off", "shadow", "on"}:
            raise ValueError("h5_v2_mode 不受支持")
        if web_v2_mode not in {"off", "exploration_only", "on"}:
            raise ValueError("web_v2_mode 不受支持")
        self._h5_v2_mode = h5_v2_mode
        if web_v2_mode not in {"off", "exploration_only", "on"}:
            raise ValueError("web_v2_mode 不受支持")
        self._web_v2_mode = web_v2_mode
        self._h5_inspection_notification_mode = H5InspectionNotificationMode(
            h5_inspection_notification_mode
        )
        self._environment_adult_self_attestation = environment_adult_self_attestation
        self._now = now or (lambda: datetime.now(UTC))

    async def submit(
        self,
        command: V2TaskSubmissionCommand,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> ExplorationTaskSubmissionResult:
        if command.platform is TaskTargetPlatform.H5:
            if self._h5_v2_mode != "on":
                raise H5V2ModeNotEnabledError(
                    "H5 V2 正式运行模式未开启，请配置 ALIOTH_H5_V2_MODE=on"
                )
            target = TaskTarget(
                platform=TaskTargetPlatform.H5,
                target_url=command.target_url,
            )
        elif command.platform is TaskTargetPlatform.APP:
            target = await self._app_target_resolver.resolve(command)
        elif command.platform is TaskTargetPlatform.WEB:
            if self._web_v2_mode == "off":
                raise WebV2ModeNotEnabledError(
                    "WEB_V2_DISABLED: 请配置 ALIOTH_WEB_V2_MODE"
                )
            target = _build_web_target(
                command,
                access_scope=access_scope,
                declared_at=self._now(),
                environment_adult_self_attestation=(
                    self._environment_adult_self_attestation
                ),
            )
        else:
            raise V2PlatformHandlerNotConfiguredError(
                f"{command.platform.value} 平台尚未接入仅页面探索 V2 运行时"
            )
        submitted = await _submit_shared_exploration_task(
            self._submission_service, command,
            target=target, access_scope=access_scope, idempotency_key=idempotency_key,
            execution_mode=TaskExecutionMode.EXPLORATION_ONLY,
            h5_inspection_notification_mode=self._h5_inspection_notification_mode,
            web_inspection_notification_mode=H5InspectionNotificationMode.OFF,
        )
        return ExplorationTaskSubmissionResult(
            task_id=submitted.task_id,
            run_id=submitted.run_id,
            status=submitted.status,
        )


async def _submit_shared_exploration_task(
    service: TaskSubmissionService,
    command: V2TaskSubmissionCommand,
    *,
    target: TaskTarget,
    access_scope: AccessScope,
    idempotency_key: str | None,
    execution_mode: TaskExecutionMode,
    h5_inspection_notification_mode: H5InspectionNotificationMode,
    web_inspection_notification_mode: H5InspectionNotificationMode,
) -> TaskSubmissionResult:
    """两入口只切换规则消费模式，探索目标、预算和设备合同统一构建。"""

    return await service.submit_task(
        access_scope=access_scope,
        target=target,
        ruleset_code=command.ruleset_code,
        ruleset_version=command.ruleset_version,
        ruleset_codes=command.ruleset_codes,
        channel=command.channel,
        priority=command.priority,
        task_config=_task_config(
            command,
            execution_mode=execution_mode,
            h5_inspection_notification_mode=(
                h5_inspection_notification_mode
                if command.platform is TaskTargetPlatform.H5
                else H5InspectionNotificationMode.OFF
            ),
            web_inspection_notification_mode=web_inspection_notification_mode,
        ),
        page_sampling_policy=scan_mode_page_sampling_policy(
            command.scan_mode,
            platform=command.platform,
            required_page_types=command.required_page_types,
        ),
        loop_guard=_scan_mode_loop_guard(
            command.scan_mode,
            platform=command.platform,
            timeout_minutes=command.timeout_minutes,
            app_max_actions=command.app_max_actions,
            unlimited_duration=command.unlimited_duration,
        ),
        idempotency_key=idempotency_key,
        submission_kind=(
            "v2_full_task" if execution_mode is TaskExecutionMode.FULL_INSPECTION
            else "v2_exploration_task"
        ),
    )


class UnconfiguredExplorationTaskEntry:
    """页面探索测试的安全占位入口。"""

    async def submit(
        self,
        command: V2TaskSubmissionCommand,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> ExplorationTaskSubmissionResult:
        # 后续接入逻辑：
        # 1. 创建并记录探索测试任务，返回稳定的 task_id 和 run_id。
        # 2. 根据目标平台选择 Web、H5 或 App Explorer。
        # 3. 只运行页面探索、截图采集、页面理解和证据持久化。
        # 4. 证据写入完成后结束运行，并记录证据就绪状态。
        # 5. 禁止发布规则质检工单、执行 Rule Judge 或生成正式报告。
        # 6. 接入方需要补齐失败状态、取消和任务回读。
        del command, access_scope, idempotency_key
        raise ExplorationPipelineNotConfiguredError("页面探索测试流程尚未接入")


class UnconfiguredRuleEvaluationEntry:
    """独立规则质检的安全占位入口。"""

    async def submit(
        self,
        task_id: str,
        *,
        rule_codes: tuple[str, ...] | None = None,
        access_scope: AccessScope,
    ) -> RuleEvaluationSubmissionResult:
        # 后续接入逻辑：
        # 1. 校验 task_id 对应任务存在，并检查当前调用方的访问权限。
        # 2. 根据 task_id 查询已经持久化的截图、页面信息和证据制品。
        # 3. 创建独立规则质检运行，并生成 rule_evaluation_run_id。
        # 4. 仅使用既有证据组装 Rule Judge 输入，不重新启动页面探索或设备操作。
        # 5. 调用规则匹配与 Rule Judge，并独立保存本次测试结果。
        # 6. 禁止覆盖原任务正式规则结果，禁止影响 Completion Gate 和正式报告。
        # 7. 接入方需要记录成功、失败、证据不足和取消状态，供后续查询。
        del task_id, rule_codes, access_scope
        raise RuleEvaluationPipelineNotConfiguredError("独立规则质检流程尚未接入")


def _task_config(
    command: V2TaskSubmissionCommand,
    *,
    execution_mode: TaskExecutionMode,
    h5_inspection_notification_mode: H5InspectionNotificationMode = (
        H5InspectionNotificationMode.OFF
    ),
    web_inspection_notification_mode: H5InspectionNotificationMode = (
        H5InspectionNotificationMode.OFF
    ),
) -> dict[str, object]:
    """把公开入口字段锁定到既有任务配置快照。"""
    app_target = (
        build_app_exploration_target_snapshot(
            journey_steps=command.journey_steps,
            package_name=command.package_name,
            required_page_types=command.required_page_types,
            optional_page_types=command.optional_page_types,
            lifecycle_page_types=command.lifecycle_page_types,
            required_milestones=command.required_milestones,
            minimum_business_pages=command.minimum_business_pages,
            device_capability_template=command.device_capability_template,
            applicability_mode=command.applicability_mode,
        ).to_dict()
        if command.platform is TaskTargetPlatform.APP
        else None
    )
    config: dict[str, object] = {
        "task_name": command.task_name,
        "product": {
            "department_name": command.product.department_name,
            "product_code": command.product.product_code,
            "product_name": command.product.product_name,
        },
        "scan_mode": command.scan_mode,
        "journey_steps": list(command.journey_steps),
        "remark": command.remark,
        "workflow_version": "v2",
        "max_depth": _scan_mode_depth(command.scan_mode),
        INSPECTION_SCOPE_CONFIG_KEY: build_inspection_scope_config(
            command.inspection_scope_page_types
        ),
    }
    if command.h5_shortflows:
        from services.api.app.application.h5_shortflows import (
            H5ShortflowError,
            validate_h5_shortflow_selections,
        )

        if (command.platform is not TaskTargetPlatform.H5
                or execution_mode is not TaskExecutionMode.FULL_INSPECTION):
            raise H5ShortflowError("短流程目前只接入 H5 V2 正式质检")
        config["h5_shortflows"] = [item.model_dump() for item in
                                  validate_h5_shortflow_selections(command.h5_shortflows)]
    if app_target is not None:
        config["app_exploration_target"] = app_target
    elif command.platform is TaskTargetPlatform.H5:
        config["h5_exploration_target"] = {
            "required_page_types": list(
                resolve_h5_required_page_types(
                    command.scan_mode,
                    command.required_page_types,
                )
            )
        }
    elif command.platform is TaskTargetPlatform.WEB:
        config["browser_profile_snapshot"] = WebBrowserProfileRegistry().resolve(
            command.browser_profile_id or ""
        ).to_dict()
        if command.web_applicability_snapshot is None:
            raise ValueError("Web 任务缺少页面类型适用性快照")
        config["web_exploration_target"] = {
            "browser_profile_id": command.browser_profile_id,
            **command.web_applicability_snapshot.to_dict(),
        }
        if command.web_discovery_policy is not None:
            from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy

            config["web_exploration_target"]["discovery_policy"] = (
                WebDiscoveryPolicy.from_config(command.web_discovery_policy).to_config()
            )
        config["web_inspection_notification_mode"] = (
            web_inspection_notification_mode.value
        )
        # 共享 Publication 读取器当前仍读取该内部键；仅 Web 快照保留该投影。
        h5_inspection_notification_mode = web_inspection_notification_mode
    return lock_h5_inspection_notification_mode(
        lock_task_execution_mode(
            config,
            execution_mode,
        ),
        h5_inspection_notification_mode,
    )


def _build_web_target(
    command: V2TaskSubmissionCommand,
    *,
    access_scope: AccessScope,
    declared_at: datetime,
    environment_adult_self_attestation: AdultSelfAttestation | None = None,
) -> TaskTarget:
    """把 Web profile 解析为显式目标配置，禁止沿用 H5 默认值。"""

    profile = WebBrowserProfileRegistry().resolve(command.browser_profile_id or "")
    adult_self_attestation = environment_adult_self_attestation
    if adult_self_attestation is None and command.adult_self_attestation_declared is not None:
        adult_self_attestation = AdultSelfAttestation(
            declared=command.adult_self_attestation_declared,
            statement_version=(
                command.adult_self_attestation_statement_version or ""
            ),
            declared_by=access_scope.actor_user_id,
            declared_at=declared_at,
        )
    return TaskTarget(
        platform=TaskTargetPlatform.WEB,
        target_url=command.target_url,
        web=WebTaskConfig(
            browser_profile_id=profile.profile_id,
            adult_self_attestation=adult_self_attestation,
        ),
    )


def _scan_mode_loop_guard(
    scan_mode: str,
    *,
    platform: TaskTargetPlatform,
    timeout_minutes: int | None = None,
    app_max_actions: int | None = None,
    unlimited_duration: bool = False,
) -> dict[str, int | None]:
    """返回按平台隔离的扫描预算，避免动态页面挤占其他平台策略。

    App 脚本可能长于 150／180 分钟。timeout_minutes 对 App 不再生效，墙钟改为不限时。
    """
    if timeout_minutes is not None:
        if (
            isinstance(timeout_minutes, bool)
            or not isinstance(timeout_minutes, int)
            or not 1 <= timeout_minutes <= V2_TIMEOUT_MAX_MINUTES
        ):
            raise ValueError(f"timeout_minutes 必须为 1 到 {V2_TIMEOUT_MAX_MINUTES} 的整数")
    if platform is TaskTargetPlatform.APP:
        unlimited_duration = True
    if unlimited_duration:
        guard: dict[str, int | None] = {
            "max_depth": _scan_mode_depth(scan_mode),
            "max_duration_seconds": UNLIMITED_DURATION_SECONDS,
            "max_steps": 40 if scan_mode == "quick" else None,
            "unlimited_duration": 1,
        }
        if app_max_actions is not None:
            if platform is not TaskTargetPlatform.APP:
                raise ValueError("app_max_actions 只允许用于 App 任务")
            if isinstance(app_max_actions, bool) or not 1 <= app_max_actions <= 600:
                raise ValueError("app_max_actions 必须为 1 到 600 的整数")
            guard["max_actions"] = app_max_actions
        return guard
    if timeout_minutes is not None:
        resolved_timeout_minutes = timeout_minutes
    elif scan_mode == "quick":
        resolved_timeout_minutes = QUICK_SCAN_TIMEOUT_MINUTES
    elif platform is TaskTargetPlatform.H5:
        resolved_timeout_minutes = H5_FULL_SCAN_TIMEOUT_MINUTES
    elif platform is TaskTargetPlatform.WEB:
        resolved_timeout_minutes = WEB_FULL_SCAN_TIMEOUT_MINUTES
    else:
        resolved_timeout_minutes = FULL_SCAN_TIMEOUT_MINUTES
    guard: dict[str, int | None] = {
        "max_depth": _scan_mode_depth(scan_mode),
        "max_duration_seconds": resolved_timeout_minutes * 60,
        "max_steps": 40 if scan_mode == "quick" else None,
    }
    if app_max_actions is not None:
        if platform is not TaskTargetPlatform.APP:
            raise ValueError("app_max_actions 只允许用于 App 任务")
        if isinstance(app_max_actions, bool) or not 1 <= app_max_actions <= 600:
            raise ValueError("app_max_actions 必须为 1 到 600 的整数")
        # App 专用预算独立于通用 Agent max_steps；避免 quick 默认 40 与显式值冲突。
        guard["max_steps"] = None
        guard["max_actions"] = app_max_actions
    if platform is TaskTargetPlatform.H5:
        max_actions, max_ai_calls = h5_scan_mode_runtime_limits(scan_mode)
        guard.update(
            max_steps=max_actions,
            max_ai_calls=max_ai_calls,
        )
    return guard


def _scan_mode_depth(scan_mode: str) -> int:
    if scan_mode == "quick":
        return 2
    if scan_mode == "custom":
        return 3
    return 4
