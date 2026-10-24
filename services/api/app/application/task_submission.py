"""质检任务提交用例：创建业务任务并派发到 Agent Run 队列。"""

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from uuid import uuid4

from services.api.app.application.agent_run_queue_routing import AgentRunQueueRouter
from services.api.app.domain.iam.access_scope import AccessScope
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.domain.task.targets import TaskTarget, TaskTargetPlatform
from services.api.app.infrastructure.queue.redis_queue import (
    AgentRunQueueStatus,
    RedisAgentRunQueue,
)
from services.api.app.infrastructure.queue.state_store import AgentRunStateStore


@dataclass(frozen=True)
class CreatedTask:
    """已创建的业务任务及其持久化后有效执行策略快照。"""

    task_id: str
    ruleset_id: str
    target: TaskTarget
    status: str
    task_config: dict = field(default_factory=dict)
    page_sampling_policy: dict = field(default_factory=dict)
    loop_guard: dict = field(default_factory=dict)

    @property
    def target_url(self) -> str | None:
        """兼容现有 H5 消费方；App 任务返回 None。"""
        return self.target.target_url


@dataclass(frozen=True)
class TaskSubmissionResult:
    """任务提交后的业务任务与 Agent Run 绑定结果。"""

    task_id: str
    run_id: str
    ruleset_id: str
    status: str
    idempotency_key: str | None = None
    replayed: bool = False


@dataclass(frozen=True, kw_only=True)
class CreateTaskSubmissionCommand:
    """任务、初始 Run、提交事实与 Outbox 的原子写入命令。"""

    access_scope: AccessScope
    target: TaskTarget
    ruleset_code: str
    ruleset_version: int
    channel: str
    priority: int
    task_config: dict
    page_sampling_policy: dict
    loop_guard: dict
    submission_kind: str
    idempotency_key: str
    request_hash: str
    request_payload: dict


@dataclass(frozen=True, kw_only=True)
class PersistedTaskSubmission:
    """PostgreSQL 已原子提交的任务与初始 Run 身份。"""

    task: CreatedTask
    run_id: str
    replayed: bool


class TaskRulesetNotFoundError(Exception):
    """提交任务时未找到可用规则集。"""


class TaskRulesetResolverMissingError(TaskRulesetNotFoundError):
    """仓储不支持把多个规则集落成执行规则集（只在测试桩里出现）。"""


class AccessScopeRequiredError(ValueError):
    """创建任务时未提供明确的访问主体作用域。"""


class TaskSubmissionIdempotencyConflictError(RuntimeError):
    """同一主体、入口与幂等键已绑定不同请求。"""


class TaskSubmissionIdempotencyKeyError(ValueError):
    """Idempotency-Key 不满足稳定格式。"""


class AppQcDisabledError(Exception):
    """App 质检 Feature Flag 未启用。"""


class AppWorkflowVersionError(Exception):
    """App 任务没有锁定 V2 Workflow。"""


class RunCancellationOutcome(StrEnum):
    """Run 取消结果：区分已终结和等待 Worker 确认。"""

    CANCELED = "canceled"
    CANCELING = "canceling"


class TaskRepository(Protocol):
    """质检任务仓储契约。"""

    async def create_task(
        self,
        *,
        access_scope: AccessScope,
        target: TaskTarget,
        ruleset_code: str,
        ruleset_version: int,
        channel: str,
        priority: int,
        task_config: dict,
        page_sampling_policy: dict,
        loop_guard: dict,
    ) -> CreatedTask:
        """创建一条 queued 状态的业务任务。"""


class ReliableTaskSubmissionRepository(Protocol):
    """生产任务提交仓储；必须在一个 PostgreSQL 事务内提交全部初始事实。"""

    async def create_submission(
        self,
        command: CreateTaskSubmissionCommand,
    ) -> PersistedTaskSubmission: ...


class TaskSubmissionService:
    """提交质检任务并派发后台 Agent Run。"""

    def __init__(
        self,
        *,
        task_repository: TaskRepository,
        agent_run_queue: RedisAgentRunQueue,
        web_agent_run_queue: RedisAgentRunQueue | None = None,
        state_store: AgentRunStateStore,
        app_qc_enabled: bool = False,
        default_workflow_version: str = "v1",
    ) -> None:
        self._task_repository = task_repository
        self._agent_run_queue = agent_run_queue
        self._queue_router = AgentRunQueueRouter(
            default_queue=agent_run_queue,
            web_queue=web_agent_run_queue,
        )
        self._state_store = state_store
        self._app_qc_enabled = app_qc_enabled
        self._default_workflow_version = default_workflow_version

    async def submit_task(
        self,
        *,
        access_scope: AccessScope,
        target: TaskTarget | None = None,
        target_url: str | None = None,
        ruleset_code: str | None,
        ruleset_version: int | None,
        channel: str,
        priority: int,
        task_config: dict,
        page_sampling_policy: dict,
        loop_guard: dict,
        idempotency_key: str | None = None,
        submission_kind: str = "v1_task",
        ruleset_codes: Sequence[str] | None = None,
    ) -> TaskSubmissionResult:
        if not isinstance(access_scope, AccessScope):
            raise AccessScopeRequiredError("创建任务必须提供明确的 AccessScope")
        ruleset_code, ruleset_version = await self._resolve_ruleset(
            ruleset_code=ruleset_code,
            ruleset_version=ruleset_version,
            ruleset_codes=ruleset_codes,
        )
        effective_target = target or TaskTarget(
            platform=TaskTargetPlatform.H5,
            target_url=target_url,
        )
        locked_task_config = dict(task_config)
        # 任务主体和租户只能来自已认证请求的 AccessScope；即使调用方把同名
        # 字段塞进 payload/task_config，也不能让它覆盖数据库归属字段。
        locked_task_config.pop("submitter_id", None)
        locked_task_config.pop("tenant_id", None)
        # 逐规则应用清单必须由提交事务读取启用规则生成，不能信任请求伪造的快照。
        locked_task_config.pop("rule_inventory", None)
        # 部门账号的任务部门以登录身份为准；其它账号沿用产品快照里的部门。
        department_scope = access_scope.department_scope
        if department_scope:
            product_snapshot = dict(locked_task_config.get("product") or {})
            product_snapshot["department_name"] = department_scope
            locked_task_config["product"] = product_snapshot
        # 部门账号的任务部门以登录身份为准；其它账号沿用产品快照里的部门。
        department_scope = access_scope.department_scope
        if department_scope:
            product_snapshot = dict(locked_task_config.get("product") or {})
            product_snapshot["department_name"] = department_scope
            locked_task_config["product"] = product_snapshot
        workflow_version = str(
            locked_task_config.get("workflow_version") or self._default_workflow_version
        )
        if effective_target.platform is TaskTargetPlatform.APP:
            if not self._app_qc_enabled:
                raise AppQcDisabledError("App 质检未启用")
            if workflow_version != "v2":
                raise AppWorkflowVersionError("App 任务必须锁定 workflow_version=v2")
            locked_task_config["app"] = effective_target.to_dict()["app"]
        elif (
            effective_target.platform is TaskTargetPlatform.WEB
            and workflow_version == "v2"
        ):
            # Web 探索、通知与收口都从同一 Task 快照校验权威目标；
            # 仅写入 Run 会让仅探索任务丢失成年声明等冻结事实。
            locked_task_config["_task_target"] = effective_target.to_dict()
            from services.api.app.domain.exploration.web_runtime_contract import (
                CURRENT_WEB_RUNTIME_VERSION,
            )

            locked_task_config["web_runtime_version"] = CURRENT_WEB_RUNTIME_VERSION
        locked_task_config["workflow_version"] = workflow_version
        resolved_idempotency_key = normalize_task_submission_idempotency_key(
            idempotency_key or f"server:{uuid4()}"
        )
        resolved_submission_kind = normalize_task_submission_kind(submission_kind)
        request_payload = {
            "protocol_version": "task-submission-request.v1",
            "target": effective_target.to_dict(),
            "ruleset_code": ruleset_code,
            "ruleset_version": ruleset_version,
            "channel": channel,
            "priority": priority,
            "task_config": locked_task_config,
            "page_sampling_policy": page_sampling_policy,
            "loop_guard": loop_guard,
            "submission_kind": resolved_submission_kind,
        }
        create_submission = getattr(self._task_repository, "create_submission", None)
        if callable(create_submission):
            persisted = await create_submission(
                CreateTaskSubmissionCommand(
                    access_scope=access_scope,
                    target=effective_target,
                    ruleset_code=ruleset_code,
                    ruleset_version=ruleset_version,
                    channel=channel,
                    priority=priority,
                    task_config=locked_task_config,
                    page_sampling_policy=page_sampling_policy,
                    loop_guard=loop_guard,
                    submission_kind=resolved_submission_kind,
                    idempotency_key=resolved_idempotency_key,
                    request_hash=stable_protocol_hash(request_payload),
                    request_payload=request_payload,
                )
            )
            return TaskSubmissionResult(
                task_id=persisted.task.task_id,
                run_id=persisted.run_id,
                ruleset_id=persisted.task.ruleset_id,
                status=persisted.task.status,
                idempotency_key=resolved_idempotency_key,
                replayed=persisted.replayed,
            )

        # 仅为历史测试桩和非生产自定义仓储保留兼容路径。默认 PostgreSQL
        # 仓储必须实现 create_submission，生产 HTTP 组合根不得走此分支。
        task = await self._task_repository.create_task(
            access_scope=access_scope,
            target=effective_target,
            ruleset_code=ruleset_code,
            ruleset_version=ruleset_version,
            channel=channel,
            priority=priority,
            task_config=locked_task_config,
            page_sampling_policy=page_sampling_policy,
            loop_guard=loop_guard,
        )
        run_task_config = dict(task.task_config)
        run_task_config["_task_target"] = task.target.to_dict()
        if task.target_url is not None:
            run_task_config["_trusted_target_url"] = task.target_url
        record = await self._queue_router.for_target(task.target).enqueue(
            _task_message(task, ruleset_code, ruleset_version),
            task_id=task.task_id,
            submitted_by_user_id=access_scope.actor_user_id,
            tenant_id=access_scope.tenant_id,
            task_config=run_task_config,
            page_sampling_policy=task.page_sampling_policy,
            loop_guard=task.loop_guard,
        )
        await self._state_store.record_submitted(record)
        return TaskSubmissionResult(
            task_id=task.task_id,
            run_id=record.run_id,
            ruleset_id=task.ruleset_id,
            status=task.status,
            idempotency_key=resolved_idempotency_key,
        )

    async def _resolve_ruleset(
        self,
        *,
        ruleset_code: str | None,
        ruleset_version: int | None,
        ruleset_codes: Sequence[str] | None,
    ) -> tuple[str, int]:
        """任务锁哪个规则集：显式指定单个规则集（老调用方）照旧；选了多个或没指定时跑全部选中的规则集。

        多个规则集由仓储落成一个组合规则集，任务、工作项、发布记录仍只锁这一个。
        """

        if ruleset_code and not ruleset_codes:
            return ruleset_code, int(ruleset_version or 1)
        resolver = getattr(self._task_repository, "resolve_execution_ruleset", None)
        if not callable(resolver):
            raise TaskRulesetResolverMissingError("当前任务仓储不支持按多个规则集提交任务")
        return await resolver(list(ruleset_codes) if ruleset_codes else None)

    async def ensure_run_enqueued(self, run_id: str) -> bool:
        """幂等确认已提交 Run 仍在等待队列，避免由页面层伪造 running 状态。"""
        existing = await self._agent_run_queue.get(run_id)
        if existing is None:
            return False
        return await self._queue_router.for_record(existing).ensure_enqueued(run_id)

    async def cancel_run(self, run_id: str) -> RunCancellationOutcome | None:
        """取消 queued/running Run，并同步长期运行状态。"""
        existing = await self._agent_run_queue.get(run_id)
        selected_queue = (
            self._queue_router.for_record(existing)
            if existing is not None
            else self._agent_run_queue
        )
        if existing is not None and existing.status is AgentRunQueueStatus.CANCELED:
            if not existing.cancellation_finalized:
                await self._state_store.record_finished(existing)
                existing.cancellation_finalized = True
                await selected_queue.save(existing)
            return RunCancellationOutcome.CANCELED
        durable_cancellation_enabled = getattr(
            self._state_store,
            "durable_cancellation_enabled",
            True,
        )
        durable_cancellation_accepted = False
        if durable_cancellation_enabled:
            durable_cancellation_accepted = await self._state_store.record_cancel_requested(run_id)
            if not durable_cancellation_accepted:
                return None
        elif existing is None:
            # 未启用长期 Store 时保留旧队列语义，但不得把不存在的 Run 报告为已接受。
            return None

        canceled = await selected_queue.cancel_queued(run_id)
        if canceled is not None:
            await self._state_store.record_finished(canceled)
            canceled.cancellation_finalized = True
            await selected_queue.save(canceled)
            return RunCancellationOutcome.CANCELED

        canceled = await selected_queue.cancel_orphaned_running(run_id)
        if canceled is not None:
            await self._state_store.record_finished(canceled)
            canceled.cancellation_finalized = True
            await selected_queue.save(canceled)
            return RunCancellationOutcome.CANCELED

        accepted = await selected_queue.request_cancel(run_id)
        if not accepted:
            # PostgreSQL 已先接受取消；Redis 仅是运行中协作信号，不能反转该事实。
            return RunCancellationOutcome.CANCELING if durable_cancellation_accepted else None
        return RunCancellationOutcome.CANCELING

def build_task_message(task: CreatedTask, ruleset_code: str, ruleset_version: int) -> str:
    execution_policy = json.dumps(
        {
            "task_config": task.task_config,
            "page_sampling_policy": task.page_sampling_policy,
            "loop_guard": task.loop_guard,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    target_line = (
        f"目标URL：{task.target_url}"
        if task.target_url is not None
        else (f"目标平台：app\n构建制品引用：{task.target.app.platform_build_ref}")
    )
    return (
        "请执行一次自动化质检任务。\n"
        f"任务ID：{task.task_id}\n"
        f"{target_line}\n"
        f"规则集：{ruleset_code}@{ruleset_version}\n"
        f"任务执行策略（JSON）：{execution_policy}\n"
        "页面导航调用必须携带从入口页 0 开始的 navigation_depth，并严格遵守页面抽样与深度边界。\n"
        "要求：按规则集完成页面探索、证据采集和规则判定，并写回结构化结果。"
    )


# 保留历史私有名称，避免已有内部测试一次性迁移。
_task_message = build_task_message


_IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SUBMISSION_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")


def normalize_task_submission_idempotency_key(value: str) -> str:
    """限制为可安全写入索引、日志和协议身份的短文本。"""

    if not isinstance(value, str) or not _IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise TaskSubmissionIdempotencyKeyError(
            "Idempotency-Key 必须为 1-128 位字母、数字、点、下划线、冒号或连字符"
        )
    return value


def normalize_task_submission_kind(value: str) -> str:
    if not isinstance(value, str) or not _SUBMISSION_KIND_PATTERN.fullmatch(value):
        raise TaskSubmissionIdempotencyKeyError("submission_kind 格式无效")
    return value
