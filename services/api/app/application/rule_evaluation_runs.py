"""独立规则质检运行：状态词汇、运行账本合同、仓储端口与执行编排。

对应 V2 入口 POST /api/v2/tasks/{task_id}/rule-evaluation-runs：
基于任务已留存证据重新跑规则判定，不重启页面探索、不做设备操作。

隔离红线：运行结果只写 qc_rule_evaluation_run，与正式规则结果
（qc_rule_execution）物理隔离；本模块不引用任何正式结果写入器，
报告中心、Completion Gate 与审核链路不读取本账本。
判定计算完全复用现役 RuleJudgeService（判定过程写入的 qc_observation_packet
审计行 agent_run_id 为空，不参与任何正式运行的幂等键）。

运行状态语义：无可判定页面 → evidence_insufficient；页面存在但候选规则为空 →
succeeded 且 rule_count=0（配置事实，非证据问题）；判定异常 → failed；
其余 → succeeded，逐规则结果进 result_payload（统一结果格式词汇）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from services.api.app.application.rule_judge import RuleJudgeRequest, RuleJudgeResult
from services.api.app.application.task_query import CandidateRule, TaskDetail
from services.api.app.application.v2_task_entries import RuleEvaluationSubmissionResult
from services.api.app.domain.iam.access_scope import AccessScope

logger = logging.getLogger(__name__)


class RuleEvaluationRunStatus(StrEnum):
    """独立规则质检运行状态（四终态 + 两在途态）。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self not in (
            RuleEvaluationRunStatus.PENDING,
            RuleEvaluationRunStatus.RUNNING,
        )


@dataclass(frozen=True, kw_only=True)
class RuleEvaluationRun:
    """一次独立规则质检运行的账本行。"""

    run_id: str
    task_id: str
    status: RuleEvaluationRunStatus
    page_count: int = 0
    rule_count: int = 0
    requested_rule_codes: tuple[str, ...] = ()
    result_payload: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    error_message: str | None = None
    requested_by: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class RuleEvaluationRunRepository(Protocol):
    """独立规则质检运行账本仓储端口。"""

    async def insert_pending(
        self, run: RuleEvaluationRun, *, access_scope: AccessScope
    ) -> None:
        """在任务对象作用域内落一条 pending 运行。"""

    async def mark_running(self, *, run_id: str) -> None:
        """把 pending 运行推进为 running 并记录开始时间。"""

    async def finish(
        self,
        *,
        run_id: str,
        status: RuleEvaluationRunStatus,
        result_payload: Sequence[dict[str, Any]] = (),
        page_count: int = 0,
        rule_count: int = 0,
        error_message: str | None = None,
    ) -> None:
        """把运行推进到终态并写入结果。"""

    async def get(
        self,
        *,
        run_id: str,
        task_id: str,
        access_scope: AccessScope,
    ) -> RuleEvaluationRun | None:
        """按 task_id + run_id + 访问作用域读运行账本。"""

    async def list_for_task(
        self, *, task_id: str, access_scope: AccessScope
    ) -> list[RuleEvaluationRun]:
        """在任务访问作用域内列运行摘要（新→旧，不含 result_payload 正文）。"""


@dataclass(frozen=True, kw_only=True)
class JudgeablePage:
    """任务已留存、可参与判定的一页。"""

    page_capture_id: str
    page_type: str | None
    surface_kind: str | None = None
    evidence_tags: frozenset[str] = field(default_factory=frozenset)


class RuleEvaluationTaskLookup(Protocol):
    """任务存在性校验端口（PostgresTaskRepository 结构性满足）。"""

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope
    ) -> TaskDetail | None:
        """按任务 ID 和访问作用域查询任务详情。"""


class RuleEvaluationPageSource(Protocol):
    """任务已留存页面枚举端口。"""

    async def list_judgeable_pages(
        self, *, task_id: str, access_scope: AccessScope
    ) -> list[JudgeablePage]:
        """在任务访问作用域内列全部可判定页面。"""


class RuleEvaluationCandidateSource(Protocol):
    """页面候选规则查询端口（PostgresTaskRepository 结构性满足）。"""

    async def list_candidate_rules(
        self, *, task_id: str, page_type: str, access_scope: AccessScope
    ) -> list[CandidateRule]:
        """在任务访问作用域内查询某页型的候选规则。"""


class RuleEvaluationJudge(Protocol):
    """规则判定端口（RuleJudgeService 结构性满足；纯计算，不写正式结果表）。"""

    async def judge_many(self, requests: list[RuleJudgeRequest]) -> list[RuleJudgeResult]:
        """批量判定并返回结果。"""


class RuleEvaluationTaskNotFoundError(RuntimeError):
    """task_id 对应任务不存在。"""


class RuleEvaluationRunService:
    """独立规则质检运行的受理与后台执行编排。

    submit 只做任务校验与 pending 落账，判定在后台任务里执行；
    进程重启导致的孤儿 running 行按账本时间戳由运维口径判定，不在此收口。
    """

    def __init__(
        self,
        *,
        task_lookup: RuleEvaluationTaskLookup,
        page_source: RuleEvaluationPageSource,
        candidate_source: RuleEvaluationCandidateSource,
        judge: RuleEvaluationJudge,
        repository: RuleEvaluationRunRepository,
        run_id_factory: Callable[[], str] | None = None,
        spawn: Callable[[Coroutine[Any, Any, None]], Any] | None = None,
    ) -> None:
        self._task_lookup = task_lookup
        self._page_source = page_source
        self._candidate_source = candidate_source
        self._judge = judge
        self._repository = repository
        self._run_id_factory = run_id_factory or _default_run_id
        self._spawn = spawn or self._spawn_background
        self._background_tasks: set[Any] = set()

    async def submit(
        self,
        task_id: str,
        *,
        rule_codes: Sequence[str] | None = None,
        access_scope: AccessScope,
    ) -> RuleEvaluationSubmissionResult:
        task = await self._task_lookup.get_task_detail(
            task_id, access_scope=access_scope
        )
        if task is None:
            raise RuleEvaluationTaskNotFoundError(f"任务 {task_id} 不存在")
        requested = _normalize_rule_codes(rule_codes)
        run_id = self._run_id_factory()
        await self._repository.insert_pending(
            RuleEvaluationRun(
                run_id=run_id,
                task_id=task_id,
                status=RuleEvaluationRunStatus.PENDING,
                requested_rule_codes=requested,
                requested_by=access_scope.actor_user_id,
            ),
            access_scope=access_scope,
        )
        self._spawn(
            self._execute(
                run_id=run_id,
                task_id=task_id,
                rule_codes=requested,
                access_scope=access_scope,
            )
        )
        return RuleEvaluationSubmissionResult(
            task_id=task_id,
            rule_evaluation_run_id=run_id,
            status=RuleEvaluationRunStatus.PENDING.value,
        )

    async def _execute(
        self,
        *,
        run_id: str,
        task_id: str,
        rule_codes: tuple[str, ...] = (),
        access_scope: AccessScope,
    ) -> None:
        try:
            await self._repository.mark_running(run_id=run_id)
            pages = await self._page_source.list_judgeable_pages(
                task_id=task_id,
                access_scope=access_scope,
            )
            if not pages:
                await self._repository.finish(
                    run_id=run_id,
                    status=RuleEvaluationRunStatus.EVIDENCE_INSUFFICIENT,
                    error_message="任务没有可判定的已留存页面",
                )
                return
            candidates_by_page_type: dict[str, list[CandidateRule]] = {}
            results: list[dict[str, Any]] = []
            rule_count = 0
            app_ad_pair_consumed = False
            for page in pages:
                page_type = (page.page_type or "").strip()
                has_ad_pair = "ad_navigation_pair_complete" in page.evidence_tags
                if not page_type and not has_ad_pair:
                    continue
                lookup_page_types = [page_type] if page_type else []
                if has_ad_pair and page_type != "startup_ad":
                    # 弹窗可覆盖任意业务页；只补入广告成对证据规则，不改写页面类型。
                    lookup_page_types.append("startup_ad")
                candidates_by_code: dict[str, CandidateRule] = {}
                for lookup_page_type in lookup_page_types:
                    if lookup_page_type not in candidates_by_page_type:
                        candidates_by_page_type[
                            lookup_page_type
                        ] = await self._candidate_source.list_candidate_rules(
                            task_id=task_id,
                            page_type=lookup_page_type,
                            access_scope=access_scope,
                        )
                    for rule in candidates_by_page_type[lookup_page_type]:
                        if lookup_page_type == page_type or rule.rule_code == "alioth_17":
                            candidates_by_code[rule.rule_code] = rule
                candidates = list(candidates_by_code.values())
                if rule_codes:
                    wanted = set(rule_codes)
                    candidates = [
                        rule for rule in candidates if rule.rule_code in wanted
                    ]
                candidates = [
                    rule
                    for rule in candidates
                    if rule.rule_code != "alioth_17"
                    or page.surface_kind not in {"android", "ios"}
                    or (has_ad_pair and not app_ad_pair_consumed)
                ]
                if not candidates:
                    continue
                if page.surface_kind in {"android", "ios"} and any(
                    rule.rule_code == "alioth_17" for rule in candidates
                ):
                    app_ad_pair_consumed = True
                requests = [
                    RuleJudgeRequest(
                        task_id=task_id,
                        rule_code=rule.rule_code,
                        page_capture_id=page.page_capture_id,
                        evidence_artifact_ids=(),
                        judge_input={},
                    )
                    for rule in candidates
                ]
                rule_count += len(requests)
                for outcome in await self._judge.judge_many(requests):
                    results.append(asdict(outcome))
            await self._repository.finish(
                run_id=run_id,
                status=RuleEvaluationRunStatus.SUCCEEDED,
                result_payload=results,
                page_count=len(pages),
                rule_count=rule_count,
            )
        except Exception as exc:  # noqa: BLE001 - 后台终态兜底，必须把失败写回账本
            logger.exception("独立规则质检运行 %s 执行失败", run_id)
            try:
                await self._repository.finish(
                    run_id=run_id,
                    status=RuleEvaluationRunStatus.FAILED,
                    error_message=str(exc)[:500] or exc.__class__.__name__,
                )
            except Exception:  # noqa: BLE001 - 终态写回失败只能留日志
                logger.exception("独立规则质检运行 %s 终态写回失败", run_id)

    def _spawn_background(self, coroutine: Coroutine[Any, Any, None]) -> Any:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task


def _default_run_id() -> str:
    return f"rule-eval:{uuid4().hex}"


def _normalize_rule_codes(rule_codes: Sequence[str] | None) -> tuple[str, ...]:
    if not rule_codes:
        return ()
    normalized = sorted({str(item).strip() for item in rule_codes if str(item).strip()})
    return tuple(normalized)
