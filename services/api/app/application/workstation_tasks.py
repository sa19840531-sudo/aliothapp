"""工作台任务页面用例。"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from services.api.app.application.h5_task_runtime_settings import (
    h5_scan_mode_runtime_limits,
)
from services.api.app.application.task_submission import (
    RunCancellationOutcome,
    TaskSubmissionResult,
    TaskSubmissionService,
)
from services.api.app.application.web_browser_profile import WebBrowserProfileRegistry
from services.api.app.application.web_page_applicability import resolve_web_page_applicability
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.page.registry import get_default_page_type_registry
from services.api.app.domain.page.web_page_catalog import (
    WEB_BUSINESS_PAGE_TYPE_CODES,
)
from services.api.app.domain.task import (
    DEFAULT_WEB_BROWSER_PROFILE_ID,
    AdultSelfAttestation,
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

TASK_QUEUE_OBSERVATION_TIMEOUT_SECONDS = 1.0

TASK_ACTIONS_BY_STATUS: dict[str, list[str]] = {
    "queued": ["start", "edit", "cancel"],
    "running": ["detail", "cancel"],
    "canceling": ["detail"],
    "review": ["detail", "report"],
    "done": ["detail", "report", "rerun", "copy"],
    "failed": ["failure", "rerun", "copy"],
    "canceled": ["copy"],
}

QUICK_SCAN_TIMEOUT_MINUTES = 15
FULL_SCAN_TIMEOUT_MINUTES = 150
H5_FULL_SCAN_TIMEOUT_MINUTES = 90
QUICK_SCAN_MAX_PAGES = 3


@dataclass(frozen=True)
class ProductSnapshot:
    """工作台页面展示的产品快照。"""

    department_name: str
    product_code: str
    product_name: str


@dataclass(frozen=True)
class WorkstationTaskListFilter:
    """任务中心列表筛选条件。"""

    task_name: str | None = None
    product_name: str | None = None
    product_code: str | None = None
    target_url: str | None = None
    status: str | None = None
    report_status: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    department_name: str | None = None
    target_platform: str | None = None
    submitter_name: str | None = None
    page: int = 1
    page_size: int = 20


@dataclass(frozen=True)
class WorkstationTaskListItem:
    """任务中心列表项。"""

    task_id: str
    task_name: str
    product: ProductSnapshot
    target_url: str | None
    target_platform: TaskTargetPlatform
    execution_mode: TaskExecutionMode
    journey_steps: list[str]
    status: str
    progress: int
    report_status: str
    report_availability: str
    failure_reason: str | None
    available_actions: list[str]
    created_at: datetime
    # 2.0 原型任务列表的「所属公司 / 发起人 / 执行时长」；kw_only 以免打乱子类的位置字段。
    tenant_name: str | None = field(default=None, kw_only=True)
    submitter_name: str | None = field(default=None, kw_only=True)
    execution_started_at: datetime | None = field(default=None, kw_only=True)
    execution_finished_at: datetime | None = field(default=None, kw_only=True)


@dataclass(frozen=True)
class WorkstationTaskListResult:
    """任务中心分页结果。"""

    items: list[WorkstationTaskListItem]
    total: int
    page: int
    page_size: int


@dataclass(frozen=True)
class WebStageDurations:
    """Web V2 正式链路的阶段耗时；阶段未完成时对应值为空。"""

    exploration_ms: int | None
    inspection_ms: int | None
    report_generation_ms: int | None
    total_ms: int | None

    def to_dict(self) -> dict[str, int | None]:
        return {
            "exploration_ms": self.exploration_ms,
            "inspection_ms": self.inspection_ms,
            "report_generation_ms": self.report_generation_ms,
            "total_ms": self.total_ms,
        }


@dataclass(frozen=True)
class WorkstationTaskDetail(WorkstationTaskListItem):
    """工作台任务详情摘要。"""

    started_at: datetime | None
    finished_at: datetime | None
    ruleset_code: str
    ruleset_version: int
    channel: str
    priority: int
    run_id: str | None
    workflow_version: str = "v1"
    scan_mode: str = "full"
    remark: str | None = None
    required_page_types: tuple[str, ...] = ()
    # 建任务时选的质检范围；老任务没有这个配置，按全部处理
    inspection_scope_page_types: tuple[str, ...] = ()
    optional_page_types: tuple[str, ...] = ()
    not_applicable_page_types: tuple[str, ...] = ()
    applicability_mode: str | None = None
    applicability_catalog_version: str | None = None
    applicability_catalog_hash: str | None = None
    browser_profile_id: str | None = None
    adult_self_attestation: AdultSelfAttestation | None = None
    viewport_width: int | None = None
    viewport_height: int | None = None
    execution_status: str | None = None
    finalization_state: str | None = None
    completion_gate_passed: bool = False
    report_available: bool = False
    coverage_status: str | None = None
    coverage_observed_page_types: tuple[str, ...] = ()
    coverage_missing_page_types: tuple[str, ...] = ()
    coverage_unrecognized_page_types: tuple[str, ...] = ()
    coverage_normalization_protocol_version: str | None = None
    stage_durations: WebStageDurations | None = None
    run_status: str | None = None
    execution_phase: str | None = None
    queue_position: int | None = None
    tasks_ahead: int | None = None
    available_worker_count: int | None = None
    start_condition: str | None = None


def web_execution_phase(
    *,
    task_status: str,
    run_status: str | None,
    failure_reason: str | None,
    coverage_unrecognized_page_types: tuple[str, ...] = (),
) -> str:
    """把正常排队、运行和不同失败面翻译为互斥的 Web 用户状态。"""

    status = str(task_status).strip().casefold()
    run = str(run_status or "").strip().casefold()
    reason = str(failure_reason or "").strip().upper()
    if status == "queued":
        return "pending" if run in {"pending", "queued"} else "queued"
    if status in {"running", "canceling"}:
        return "running"
    if status == "review" or coverage_unrecognized_page_types:
        return "classification_review"
    if status == "failed":
        if any(marker in reason for marker in ("CONFIG", "ATTESTATION", "POLICY")):
            return "configuration_error"
        if any(marker in reason for marker in ("NAVIGATION", "UNREACHABLE", "HOME_")):
            return "navigation_failed"
        return "evidence_failed"
    return status or "queued"


@dataclass(frozen=True)
class WorkstationTaskCreateCommand:
    """工作台创建任务命令。"""

    task_name: str
    target_url: str
    # 单个规则集（老调用方）；为空时按 ruleset_codes 或全部开启的规则集执行
    ruleset_code: str | None
    ruleset_version: int | None
    channel: str
    priority: int
    product: ProductSnapshot
    scan_mode: str
    journey_steps: list[str]
    remark: str | None
    target_platform: TaskTargetPlatform = TaskTargetPlatform.H5
    required_page_types: tuple[str, ...] = ()
    execution_mode: TaskExecutionMode = TaskExecutionMode.FULL_INSPECTION
    ruleset_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkstationTaskOperationResult:
    """任务操作结果。"""

    task_id: str
    status: str
    run_id: str | None
    message: str


class WorkstationTaskRepository(Protocol):
    """工作台任务查询与操作仓储契约。"""

    async def list_tasks(
        self,
        filters: WorkstationTaskListFilter,
        *,
        access_scope: AccessScope,
    ) -> WorkstationTaskListResult:
        """按页面筛选条件查询任务中心列表。"""

    async def get_task_detail(
        self,
        task_id: str,
        *,
        access_scope: AccessScope,
        for_write: bool = False,
    ) -> WorkstationTaskDetail | None:
        """查询任务详情摘要。"""

    async def get_web_stage_durations(
        self,
        task_id: str,
        *,
        agent_run_id: str | None,
        access_scope: AccessScope,
    ) -> WebStageDurations | None:
        """查询指定 Web V2 正式 Run 的阶段耗时。"""

    async def update_task_status(
        self,
        *,
        task_id: str,
        status: str,
        allowed_statuses: list[str],
        access_scope: AccessScope,
    ) -> WorkstationTaskOperationResult | None:
        """在允许状态内更新任务状态。"""

    async def retry_quarantined_finalization(
        self,
        *,
        task_id: str,
        access_scope: AccessScope,
    ) -> WorkstationTaskOperationResult | None:
        """管理员显式恢复已隔离的正式报告收口。"""


class TaskQueueStatusReader(Protocol):
    """工作台只读所需的任务队列状态。"""

    async def queue_position(self, run_id: str) -> int | None: ...

    async def read_worker_heartbeats(self) -> list[dict]: ...


class TerminalRunTaskCanceler(Protocol):
    """平台取消已结束 Main Run 对应、尚未封存的根任务。"""

    async def cancel_finished_run_task(
        self, *, task_id: str, run_id: str, access_scope: AccessScope
    ) -> WorkstationTaskOperationResult | None: ...


class WorkstationTaskService:
    """工作台任务页面服务。"""

    def __init__(
        self,
        *,
        repository: WorkstationTaskRepository,
        submission_service: TaskSubmissionService | None = None,
        queue_status_reader: TaskQueueStatusReader | None = None,
        web_queue_status_reader: TaskQueueStatusReader | None = None,
        terminal_run_task_canceler: TerminalRunTaskCanceler | None = None,
        h5_inspection_notification_mode: H5InspectionNotificationMode = (
            H5InspectionNotificationMode.OFF
        ),
    ) -> None:
        if not isinstance(h5_inspection_notification_mode, H5InspectionNotificationMode):
            raise TypeError("h5_inspection_notification_mode 类型无效")
        self._repository = repository
        self._submission_service = submission_service
        self._queue_status_reader = queue_status_reader
        self._web_queue_status_reader = web_queue_status_reader
        self._terminal_run_task_canceler = terminal_run_task_canceler
        self._h5_inspection_notification_mode = h5_inspection_notification_mode

    async def list_tasks(
        self,
        filters: WorkstationTaskListFilter,
        *,
        access_scope: AccessScope,
    ) -> WorkstationTaskListResult:
        return await self._repository.list_tasks(filters, access_scope=access_scope)

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope
    ) -> WorkstationTaskDetail | None:
        detail = await self._repository.get_task_detail(task_id, access_scope=access_scope)
        if (
            detail is None
            or detail.execution_phase not in {"queued", "pending"}
        ):
            return detail
        is_web = detail.target_platform is TaskTargetPlatform.WEB
        queue_reader = self._web_queue_status_reader if is_web else self._queue_status_reader
        worker_name = "Web Worker" if is_web else "探索 Worker"
        # 任务优先级或创建时间不等于平台队列的 FIFO 位置；无法观测时保持未知。
        detail = replace(
            detail,
            queue_position=None,
            tasks_ahead=None,
            available_worker_count=None,
            start_condition=f"等待{worker_name}领取当前 Run；排队状态暂未确认",
        )
        if queue_reader is None or detail.run_id is None:
            return detail
        try:
            # Redis 只读观测必须有界，不能拖住权威任务状态查询。
            async with asyncio.timeout(TASK_QUEUE_OBSERVATION_TIMEOUT_SECONDS):
                async with asyncio.TaskGroup() as group:
                    position_request = group.create_task(queue_reader.queue_position(detail.run_id))
                    heartbeats_request = group.create_task(queue_reader.read_worker_heartbeats())
                position = position_request.result()
                heartbeats = heartbeats_request.result()
        except Exception:  # noqa: BLE001 - 队列观测故障不得破坏任务详情
            return detail
        worker_role = "web_explorer" if is_web else "explorer"
        available_workers = sum(
            1
            for heartbeat in heartbeats
            if not heartbeat.get("roles") or worker_role in heartbeat.get("roles", [])
        )
        return replace(
            detail,
            queue_position=position,
            tasks_ahead=(position - 1 if position is not None else None),
            available_worker_count=available_workers,
            start_condition=(
                "尚未确认排队位置，等待调度状态同步"
                if position is None
                else (
                    f"已入队，等待{worker_name}释放执行槽并领取当前 Run"
                    if available_workers > 0
                    else f"已入队，等待{worker_name}上线并领取当前 Run"
                )
            ),
        )

    async def get_web_stage_durations(
        self,
        task_id: str,
        *,
        agent_run_id: str | None,
        access_scope: AccessScope,
    ) -> WebStageDurations | None:
        return await self._repository.get_web_stage_durations(
            task_id,
            agent_run_id=agent_run_id,
            access_scope=access_scope,
        )

    async def create_task(
        self,
        command: WorkstationTaskCreateCommand,
        *,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> WorkstationTaskOperationResult:
        if self._submission_service is None:
            raise RuntimeError("工作台任务提交服务未配置")
        if not _is_h5_platform(command.target_platform):
            raise InvalidTaskOperationError("工作台创建入口仅支持 H5 质检任务")
        result: TaskSubmissionResult = await self._submission_service.submit_task(
            access_scope=access_scope,
            target_url=command.target_url,
            ruleset_code=command.ruleset_code,
            ruleset_version=command.ruleset_version,
            ruleset_codes=command.ruleset_codes,
            channel=command.channel,
            priority=command.priority,
            task_config=_task_config_from_command(
                command,
                h5_inspection_notification_mode=self._h5_inspection_notification_mode,
            ),
            page_sampling_policy=scan_mode_page_sampling_policy(
                command.scan_mode,
                platform=command.target_platform,
                required_page_types=command.required_page_types,
            ),
            loop_guard=_scan_mode_loop_guard(command.scan_mode),
            idempotency_key=idempotency_key,
            submission_kind="workstation_create",
        )
        return WorkstationTaskOperationResult(
            task_id=result.task_id,
            status=result.status,
            run_id=result.run_id,
            message="任务已提交",
        )

    async def perform_operation(
        self,
        *,
        task_id: str,
        operation: str,
        access_scope: AccessScope,
        idempotency_key: str | None = None,
    ) -> WorkstationTaskOperationResult:
        if operation == "retry-finalization":
            if not access_scope.can_write_any:
                raise InvalidTaskOperationError("只有管理员可以恢复已隔离的正式报告收口")
            result = await self._repository.retry_quarantined_finalization(
                task_id=task_id,
                access_scope=access_scope,
            )
            if result is None:
                raise InvalidTaskOperationError("任务未处于可恢复的收口隔离状态")
            return result
        if operation == "start":
            detail = await self._required_detail(
                task_id,
                access_scope=access_scope,
                for_write=True,
            )
            if detail.status != "queued":
                raise InvalidTaskOperationError("只有待执行任务可以开始执行")
            run_id = _required_run_id(detail)
            submission_service = self._required_submission_service()
            if not await submission_service.ensure_run_enqueued(run_id):
                raise InvalidTaskOperationError("任务运行记录不存在或已不在待执行状态")
            return WorkstationTaskOperationResult(
                task_id=task_id,
                status="queued",
                run_id=run_id,
                message="任务已在执行队列中等待 Worker 消费",
            )
        if operation == "cancel":
            detail = await self._required_detail(
                task_id,
                access_scope=access_scope,
                for_write=True,
            )
            if detail.status in {"canceling", "canceled"}:
                return WorkstationTaskOperationResult(
                    task_id=task_id,
                    status=detail.status,
                    run_id=detail.run_id,
                    message=("取消请求处理中" if detail.status == "canceling" else "任务已取消"),
                )
            if detail.status not in {"queued", "running"}:
                raise InvalidTaskOperationError("只有待执行或执行中任务可以取消")
            run_id = _required_run_id(detail)
            if detail.run_status in {"done", "failed", "aborted", "canceled"}:
                result = await self._cancel_finished_run_task(
                    task_id=task_id, run_id=run_id, access_scope=access_scope
                )
                if result is not None:
                    return result
            submission_service = self._required_submission_service()
            cancellation_outcome = await submission_service.cancel_run(run_id)
            if cancellation_outcome is None:
                # 点击与运行结束可能并发；重新在数据库锁内校验当前 Run 和根任务。
                result = await self._cancel_finished_run_task(
                    task_id=task_id, run_id=run_id, access_scope=access_scope
                )
                if result is not None:
                    return result
                raise InvalidTaskOperationError("任务运行记录不存在或已进入终态")
            if cancellation_outcome == RunCancellationOutcome.CANCELING:
                return WorkstationTaskOperationResult(
                    task_id=task_id,
                    status="canceling",
                    run_id=run_id,
                    message="取消请求已提交，等待 Worker 安全终止当前步骤",
                )
            result = await self._repository.update_task_status(
                task_id=task_id,
                status="canceled",
                allowed_statuses=["queued", "running", "canceled"],
                access_scope=access_scope,
            )
            if result is None:
                raise InvalidTaskOperationError("只有待执行或执行中任务可以取消")
            return result
        if operation == "rerun":
            detail = await self._required_detail(
                task_id,
                access_scope=access_scope,
                for_write=True,
            )
            exploration_terminal = (
                detail.execution_mode is TaskExecutionMode.EXPLORATION_ONLY
                and detail.status in {"completed", "review"}
            )
            if detail.status not in {"done", "failed"} and not exploration_terminal:
                raise InvalidTaskOperationError("只有已完成或失败任务可以重新执行")
            return await self._submit_from_detail(
                detail,
                source_operation="rerun",
                access_scope=access_scope,
                idempotency_key=idempotency_key,
            )
        if operation == "exploration":
            detail = await self._required_detail(
                task_id,
                access_scope=access_scope,
                for_write=True,
            )
            if (
                detail.target_platform is not TaskTargetPlatform.WEB
                or detail.execution_mode is not TaskExecutionMode.EXPLORATION_ONLY
                or detail.status not in {"completed", "done", "review"}
            ):
                raise InvalidTaskOperationError("只有已完成的 Web 探索任务可以重新探索")
            return await self._submit_from_detail(
                detail,
                source_operation="exploration",
                access_scope=access_scope,
                idempotency_key=idempotency_key,
            )
        if operation == "copy":
            detail = await self._required_detail(
                task_id,
                access_scope=access_scope,
                for_write=True,
            )
            exploration_terminal = (
                detail.execution_mode is TaskExecutionMode.EXPLORATION_ONLY
                and detail.status in {"completed", "review"}
            )
            if (
                detail.status not in {"done", "failed", "canceled"}
                and not exploration_terminal
            ):
                raise InvalidTaskOperationError("只有终态任务可以复制")
            return await self._submit_from_detail(
                detail,
                source_operation="copy",
                access_scope=access_scope,
                idempotency_key=idempotency_key,
            )
        raise InvalidTaskOperationError(f"不支持的任务操作：{operation}")

    async def _required_detail(
        self,
        task_id: str,
        *,
        access_scope: AccessScope,
        for_write: bool = False,
    ) -> WorkstationTaskDetail:
        detail = await self._repository.get_task_detail(
            task_id,
            access_scope=access_scope,
            for_write=for_write,
        )
        if detail is None:
            raise TaskNotFoundError(f"未找到任务：{task_id}")
        return detail

    def _required_submission_service(self) -> TaskSubmissionService:
        if self._submission_service is None:
            raise RuntimeError("工作台任务提交服务未配置")
        return self._submission_service

    async def _cancel_finished_run_task(
        self, *, task_id: str, run_id: str, access_scope: AccessScope
    ) -> WorkstationTaskOperationResult | None:
        if self._terminal_run_task_canceler is None:
            return None
        return await self._terminal_run_task_canceler.cancel_finished_run_task(
            task_id=task_id, run_id=run_id, access_scope=access_scope
        )

    async def _submit_from_detail(
        self,
        detail: WorkstationTaskDetail,
        *,
        source_operation: str,
        access_scope: AccessScope,
        idempotency_key: str | None,
    ) -> WorkstationTaskOperationResult:
        if not detail.target_url:
            raise InvalidTaskOperationError(
                "App 质检任务暂不支持工作台重跑/复制，请从 App 质检页重新创建"
            )
        task_config = {
            "task_name": detail.task_name,
            "product": {
                "department_name": detail.product.department_name,
                "product_code": detail.product.product_code,
                "product_name": detail.product.product_name,
            },
            "scan_mode": detail.scan_mode,
            "execution_mode": detail.execution_mode.value,
            "workflow_version": detail.workflow_version,
            "journey_steps": detail.journey_steps,
            "remark": detail.remark,
            "max_depth": _scan_mode_depth(detail.scan_mode),
            "source_task_id": detail.task_id,
            "source_operation": source_operation,
            INSPECTION_SCOPE_CONFIG_KEY: build_inspection_scope_config(
                detail.inspection_scope_page_types
            ),
        }
        if _is_h5_platform(detail.target_platform):
            task_config["viewport"] = "390x844"
            task_config["h5_exploration_target"] = {
                "required_page_types": list(
                    resolve_h5_required_page_types(
                        detail.scan_mode,
                        detail.required_page_types,
                    )
                )
            }
        web_config = None
        if detail.target_platform is TaskTargetPlatform.WEB:
            if detail.adult_self_attestation is None:
                raise InvalidTaskOperationError(
                    "源 Web 任务缺少操作者成年声明快照，不能重跑或复制"
                )
            web_config = WebTaskConfig(
                browser_profile_id=(detail.browser_profile_id or DEFAULT_WEB_BROWSER_PROFILE_ID),
                adult_self_attestation=detail.adult_self_attestation,
            )
            applicability = resolve_web_page_applicability(
                target_url=detail.target_url,
                requested_mode=None,
                requested_required_page_types=tuple(
                    code
                    for code in detail.required_page_types
                    if code in WEB_BUSINESS_PAGE_TYPE_CODES
                ),
            ).to_dict()
            task_config["web_exploration_target"] = {
                **web_config.to_dict(),
                **applicability,
            }
            # 和新建任务一样冻结浏览器配置快照；Web 运行时开跑前会拿它和目标配置比对，缺了直接拒跑。
            task_config["browser_profile_snapshot"] = (
                WebBrowserProfileRegistry().resolve(web_config.browser_profile_id).to_dict()
            )
            # Web V2 正式链路的消费者通知是服务端锁定合同，复制不能降级；
            # 仅探索任务不送判，和新建时一样锁 off，否则 Web 运行时同样拒跑。
            notification_mode = (
                H5InspectionNotificationMode.OFF
                if detail.execution_mode is TaskExecutionMode.EXPLORATION_ONLY
                else H5InspectionNotificationMode.ENFORCED
            ).value
            task_config["web_inspection_notification_mode"] = notification_mode
            task_config["h5_inspection_notification_mode"] = notification_mode
        result = await self._required_submission_service().submit_task(
            access_scope=access_scope,
            target=TaskTarget(
                platform=detail.target_platform,
                target_url=detail.target_url,
                web=web_config,
            ),
            ruleset_code=detail.ruleset_code,
            ruleset_version=detail.ruleset_version,
            channel=detail.channel,
            priority=detail.priority,
            task_config=task_config,
            page_sampling_policy=scan_mode_page_sampling_policy(
                detail.scan_mode,
                platform=detail.target_platform,
                required_page_types=detail.required_page_types,
            ),
            loop_guard=_scan_mode_loop_guard(detail.scan_mode),
            idempotency_key=idempotency_key,
            submission_kind=f"workstation_{source_operation}",
        )
        message = {
            "rerun": "任务重新执行已受理",
            "exploration": "任务重新探索已受理",
            "copy": "任务复制已受理",
        }[source_operation]
        return WorkstationTaskOperationResult(
            task_id=result.task_id,
            status=result.status,
            run_id=result.run_id,
            message=message,
        )


class TaskNotFoundError(ValueError):
    """工作台任务不存在。"""


class InvalidTaskOperationError(ValueError):
    """任务操作不符合当前状态。"""


def available_actions_for_status(
    status: str,
    *,
    execution_mode: TaskExecutionMode = TaskExecutionMode.FULL_INSPECTION,
) -> list[str]:
    if execution_mode is TaskExecutionMode.EXPLORATION_ONLY and status in {
        "completed",
        "done",
        "review",
    }:
        return ["detail", "exploration", "evidence", "rerun", "copy"]
    # 每次返回独立列表，权限与任务级动作不能污染状态模板或其他请求。
    return list(TASK_ACTIONS_BY_STATUS.get(status, ["detail"]))


def _scan_mode_timeout_minutes(scan_mode: str) -> int:
    """返回工作台扫描模式的墙钟预算。

    工作台创建/重跑入口只受理 H5 任务（App 任务提交前已被拦截），full 档
    取 H5 的 90 分钟合同，与 V2 入口的平台分流保持一致。
    """
    return QUICK_SCAN_TIMEOUT_MINUTES if scan_mode == "quick" else H5_FULL_SCAN_TIMEOUT_MINUTES


def _scan_mode_loop_guard(scan_mode: str) -> dict[str, int | None]:
    """返回可覆盖规则集同名字段的工作台扫描护栏。"""
    timeout_minutes = _scan_mode_timeout_minutes(scan_mode)
    guard = {
        "max_depth": _scan_mode_depth(scan_mode),
        # 直接覆盖规则集的 max_duration_seconds，避免 timeout_minutes=60
        # 与旧的 1800 秒字段并存时仍被解析成 30 分钟。
        "max_duration_seconds": timeout_minutes * 60,
    }
    max_actions, max_ai_calls = h5_scan_mode_runtime_limits(scan_mode)
    guard.update(
        max_steps=max_actions,
        max_ai_calls=max_ai_calls,
    )
    return guard


def _required_run_id(detail: WorkstationTaskDetail) -> str:
    if detail.run_id is None:
        raise InvalidTaskOperationError("任务尚未绑定可操作的 Agent Run")
    return detail.run_id


def report_status_from_task_status(
    status: str,
    total_rules: int,
    *,
    execution_mode: TaskExecutionMode = TaskExecutionMode.FULL_INSPECTION,
) -> str:
    if execution_mode is TaskExecutionMode.EXPLORATION_ONLY and status in {
        "completed",
        "done",
        "review",
    }:
        return "not_applicable"
    if status in {"queued", "canceled"}:
        return "not_started"
    if status == "running":
        return "generating"
    if status == "failed":
        return "failed"
    if status in {"done", "review"} and total_rules > 0:
        return "ready"
    return "not_started"


def _task_config_from_command(
    command: WorkstationTaskCreateCommand,
    *,
    h5_inspection_notification_mode: H5InspectionNotificationMode,
) -> dict:
    config = {
        "task_name": command.task_name,
        "product": {
            "department_name": command.product.department_name,
            "product_code": command.product.product_code,
            "product_name": command.product.product_name,
        },
        "scan_mode": command.scan_mode,
        "journey_steps": command.journey_steps,
        "remark": command.remark,
        "viewport": "390x844",
        "max_depth": _scan_mode_depth(command.scan_mode),
        INSPECTION_SCOPE_CONFIG_KEY: build_inspection_scope_config(command.required_page_types),
    }
    if _is_h5_platform(command.target_platform):
        config["h5_exploration_target"] = {
            "required_page_types": list(
                resolve_h5_required_page_types(
                    command.scan_mode,
                    command.required_page_types,
                )
            )
        }
    return lock_h5_inspection_notification_mode(
        lock_task_execution_mode(config, command.execution_mode),
        h5_inspection_notification_mode,
    )


def resolve_h5_required_page_types(
    scan_mode: str,
    required_page_types: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """解析 H5 任务的显式业务覆盖目标。

    未指定时保持动态探索，不把 Catalog 全量页型伪造成单个产品的必须目标。
    """
    del scan_mode
    requested = _normalize_h5_required_page_types(required_page_types)
    return requested


def scan_mode_page_sampling_policy(
    scan_mode: str,
    *,
    platform: TaskTargetPlatform | str | None = None,
    required_page_types: Sequence[str] | None = None,
) -> dict:
    """返回扫描模式的页面抽样策略，并为 H5 锁定业务覆盖目标。"""
    if scan_mode != "quick":
        if _is_h5_platform(platform):
            required = resolve_h5_required_page_types(scan_mode, required_page_types)
            if required:
                return {
                    "mode": "by_page_type",
                    "include_page_types": list(required),
                }
        return {}
    policy: dict[str, object] = {
        "mode": "first_n",
        "max_pages": QUICK_SCAN_MAX_PAGES,
    }
    if _is_h5_platform(platform):
        required = _normalize_h5_required_page_types(required_page_types)
        if required:
            policy["include_page_types"] = list(required)
    return policy


def _is_h5_platform(platform: TaskTargetPlatform | str | None) -> bool:
    return platform is TaskTargetPlatform.H5 or platform == TaskTargetPlatform.H5.value


def _normalize_h5_required_page_types(
    values: Sequence[str] | None,
) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str | bytes):
        raise ValueError("H5 required_page_types 必须为字符串列表")
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError("H5 required_page_types 必须为字符串列表")
        value = item.strip().casefold()
        if not value:
            raise ValueError("H5 required_page_types 不能包含空字符串")
        if value not in normalized:
            normalized.append(value)
    allowed = set(get_default_page_type_registry().coverage_required_codes)
    invalid = sorted(set(normalized) - allowed)
    if invalid:
        raise ValueError("H5 required_page_types 不属于业务页面类型目录：" + ", ".join(invalid))
    return tuple(normalized)


def _scan_mode_depth(scan_mode: str) -> int:
    if scan_mode == "quick":
        return 2
    if scan_mode == "custom":
        return 3
    return 4
