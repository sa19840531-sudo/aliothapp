"""App 探索 Persistence Gate：停新动作、排空在途、有限重试原写入后再封存。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.api.app.application.evidence_indexes import (
    EvidenceIndexRepository,
    EvidenceInventory,
)
from services.api.app.application.surface_sessions import SessionCloseReadiness
from services.api.app.domain.evidence.index import (
    NOT_YET_CUTOFF_STATUS,
    EvidenceCaptureLineage,
    EvidenceGapCode,
    EvidenceGapItem,
    EvidenceIndexReference,
    ExplorationEvidenceStatus,
    build_evidence_index_reference,
)
from services.api.app.domain.evidence.vocabulary import TERMINAL_CAPTURE_ROLES
from services.api.app.domain.interaction import SurfaceSessionStatus

logger = logging.getLogger(__name__)

_DEFAULT_MAX_WRITE_RETRIES = 2
_DEFAULT_RETRY_SLEEP_SECONDS = 0.25


@dataclass(frozen=True, kw_only=True)
class ExplorationCutoffDecision:
    """#gate 截止结论：齐全／不足才封存 Index；暂不截止不写 Index。"""

    status: str
    index: EvidenceIndexReference | None = None
    gaps: tuple[EvidenceGapItem, ...] = ()
    page_timeline: tuple[str, ...] = ()

    @property
    def deferred(self) -> bool:
        return self.status == NOT_YET_CUTOFF_STATUS


class AppExplorationPersistenceGate:
    """#gate 留存截止：先停新动作并排空；未完成走暂不截止，不派生 Probe。"""

    def __init__(
        self,
        *,
        repository: EvidenceIndexRepository,
        now: Callable[[], datetime] | None = None,
        max_write_retries: int = _DEFAULT_MAX_WRITE_RETRIES,
        retry_sleep_seconds: float = _DEFAULT_RETRY_SLEEP_SECONDS,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        if repository is None:
            raise ValueError("repository 不能为空")
        if max_write_retries < 0:
            raise ValueError("max_write_retries 不能为负数")
        if retry_sleep_seconds < 0:
            raise ValueError("retry_sleep_seconds 不能为负数")
        self._repository = repository
        self._now = now or (lambda: datetime.now(UTC))
        self._max_write_retries = max_write_retries
        self._retry_sleep_seconds = retry_sleep_seconds
        self._sleep = sleep or asyncio.sleep

    async def cutoff(
        self,
        *,
        task_id: str,
        session_id: str,
        runtime: Any | None = None,
        agent_run_id: str | None = None,
    ) -> ExplorationCutoffDecision:
        """停新动作 → 有限排空／重试 → 齐全或不足封存；禁止永久 not_yet_cutoff。"""
        await _stop_new_actions(
            runtime=runtime,
            repository=self._repository,
            task_id=task_id,
            session_id=session_id,
        )
        inventory = await self._inventory_with_drain(
            runtime=runtime,
            task_id=task_id,
            session_id=session_id,
        )
        for attempt in range(self._max_write_retries):
            if inventory.complete or not inventory.has_only_retryable_gaps:
                break
            await _retry_original_writes(
                repository=self._repository,
                task_id=task_id,
                session_id=session_id,
            )
            if self._retry_sleep_seconds > 0:
                await self._sleep(self._retry_sleep_seconds)
            inventory = await self._inventory_with_drain(
                runtime=runtime,
                task_id=task_id,
                session_id=session_id,
            )
            del attempt
        if inventory.complete:
            inventory = await self._record_and_refresh(
                runtime=runtime,
                task_id=task_id,
                session_id=session_id,
                role="exploration_terminal",
                inventory=inventory,
            )
            if inventory.complete:
                return await self._seal_decision(
                    task_id=task_id,
                    session_id=session_id,
                    inventory=inventory,
                    status=ExplorationEvidenceStatus.EXPLORATION_COMPLETED,
                    agent_run_id=agent_run_id,
                )
        timed_out = inventory.has_timed_out_writes
        if timed_out:
            logger.info(
                "task_id=%s session_id=%s 原写入／在途命令重试耗尽，固化 evidence_insufficient",
                task_id,
                session_id,
            )
            await _fail_pending_manifests(
                repository=self._repository,
                task_id=task_id,
                session_id=session_id,
            )
            inventory = await self._inventory_with_drain(
                runtime=runtime,
                task_id=task_id,
                session_id=session_id,
            )
        inventory = await self._record_and_refresh(
            runtime=runtime,
            task_id=task_id,
            session_id=session_id,
            role="evidence_insufficient",
            inventory=inventory,
        )
        if timed_out:
            inventory = _with_write_timeout(inventory, session_id=session_id)
        return await self._seal_decision(
            task_id=task_id,
            session_id=session_id,
            inventory=inventory,
            status=ExplorationEvidenceStatus.EVIDENCE_INSUFFICIENT,
            agent_run_id=agent_run_id,
        )

    async def seal_or_gap(
        self,
        *,
        task_id: str,
        session_id: str,
        runtime: Any | None = None,
        agent_run_id: str | None = None,
    ) -> EvidenceIndexReference | None:
        """兼容旧调用：暂不截止时返回 None，不伪装不足。"""
        decision = await self.cutoff(
            task_id=task_id,
            session_id=session_id,
            runtime=runtime,
            agent_run_id=agent_run_id,
        )
        return decision.index

    async def _inventory_with_drain(
        self,
        *,
        runtime: Any | None,
        task_id: str,
        session_id: str,
    ) -> EvidenceInventory:
        inventory = await self._inventory_or_failed(
            task_id=task_id,
            session_id=session_id,
        )
        readiness = await _poll_close_readiness(
            runtime=runtime,
            task_id=task_id,
            session_id=session_id,
        )
        return merge_close_readiness_gaps(
            inventory,
            readiness=readiness,
            session_id=session_id,
        )

    async def _record_and_refresh(
        self,
        *,
        runtime: Any | None,
        task_id: str,
        session_id: str,
        role: str,
        inventory: EvidenceInventory,
    ) -> EvidenceInventory:
        recorded = await _record_last_observation(
            runtime=runtime,
            repository=self._repository,
            task_id=task_id,
            session_id=session_id,
            role=role,
        )
        if recorded:
            refreshed = await self._inventory_with_drain(
                runtime=runtime,
                task_id=task_id,
                session_id=session_id,
            )
            return attach_terminal_observation(
                refreshed,
                capture_id=recorded,
                role=role,
            )
        if any(item.role == role for item in inventory.capture_lineage):
            return inventory
        return _with_missing_terminal(inventory, session_id=session_id)

    async def _inventory_or_failed(
        self,
        *,
        task_id: str,
        session_id: str,
    ) -> EvidenceInventory:
        try:
            return await self._repository.inventory(
                task_id=task_id,
                session_id=session_id,
            )
        except Exception as exc:  # noqa: BLE001 - 盘点失败必须固化缺口，禁止派生流程
            return EvidenceInventory(
                complete=False,
                gaps=(
                    EvidenceGapItem(
                        code=EvidenceGapCode.INVENTORY_FAILED,
                        subject_id=session_id,
                        detail=f"Persistence Gate 盘点失败：{type(exc).__name__}",
                    ),
                ),
            )

    async def _seal_decision(
        self,
        *,
        task_id: str,
        session_id: str,
        inventory: EvidenceInventory,
        status: ExplorationEvidenceStatus,
        agent_run_id: str | None = None,
    ) -> ExplorationCutoffDecision:
        index = build_evidence_index_reference(
            task_id=task_id,
            session_id=session_id,
            status=status,
            sealed_at=self._now(),
            manifest_ids=inventory.manifest_ids,
            capture_ids=inventory.capture_ids,
            evidence_gap=inventory.gaps,
            page_timeline=inventory.page_timeline,
            capture_lineage=inventory.capture_lineage,
            artifacts=inventory.artifacts,
            action_logs=inventory.action_logs,
        )
        sealed = await self._repository.seal(
            index=index,
            agent_run_id=agent_run_id,
        )
        return ExplorationCutoffDecision(
            status=status.value,
            index=sealed,
            gaps=inventory.gaps,
            page_timeline=inventory.page_timeline,
        )


class MemoryEvidenceIndexRepository:
    """测试用内存盘点/封存；默认齐全以免误打 Postgres。"""

    def __init__(
        self,
        *,
        inventory: EvidenceInventory | None = None,
        retry_inventory: EvidenceInventory | None = None,
        page_timeline: tuple[str, ...] = (),
    ) -> None:
        snapshot = inventory or EvidenceInventory(
            complete=True,
            page_timeline=page_timeline,
        )
        if page_timeline and not snapshot.page_timeline:
            snapshot = EvidenceInventory(
                complete=snapshot.complete,
                capture_ids=snapshot.capture_ids,
                manifest_ids=snapshot.manifest_ids,
                gaps=snapshot.gaps,
                page_timeline=page_timeline,
            )
        self.inventory_snapshot = snapshot
        self.retry_inventory = retry_inventory
        self.sealed: list[EvidenceIndexReference] = []
        self.seal_calls = 0
        self.retry_calls = 0
        self.supersede_calls = 0
        self.fail_pending_calls = 0
        self.terminal_observation_calls: list[str] = []
        self.sealed_agent_run_ids: list[str | None] = []

    async def inventory(self, *, task_id: str, session_id: str) -> EvidenceInventory:
        del task_id, session_id
        return self.inventory_snapshot

    async def retry_original_writes(self, *, task_id: str, session_id: str) -> None:
        del task_id, session_id
        self.retry_calls += 1
        if self.retry_inventory is not None:
            self.inventory_snapshot = self.retry_inventory
            self.retry_inventory = None

    async def record_terminal_observation(
        self, *, task_id: str, session_id: str, role: str
    ) -> str | None:
        del task_id
        if role not in TERMINAL_CAPTURE_ROLES:
            raise ValueError("收口观察 role 不受支持")
        self.terminal_observation_calls.append(role)
        capture_id = f"terminal-{role}"
        self.inventory_snapshot = attach_terminal_observation(
            self.inventory_snapshot,
            capture_id=capture_id,
            role=role,
        )
        return capture_id

    async def fail_pending_manifests(self, *, task_id: str, session_id: str) -> None:
        del task_id, session_id
        self.fail_pending_calls += 1

    async def supersede_unstarted_exploration_works(
        self, *, task_id: str, session_id: str
    ) -> int:
        del task_id, session_id
        self.supersede_calls += 1
        return 0

    async def seal(
        self,
        *,
        index: EvidenceIndexReference,
        agent_run_id: str | None = None,
    ) -> EvidenceIndexReference:
        self.seal_calls += 1
        self.sealed.append(index)
        self.sealed_agent_run_ids.append(agent_run_id)
        return index

    async def get(
        self, *, task_id: str, session_id: str
    ) -> EvidenceIndexReference | None:
        for item in reversed(self.sealed):
            if item.task_id == task_id and item.session_id == session_id:
                return item
        return None


def attach_terminal_observation(
    inventory: EvidenceInventory,
    *,
    capture_id: str,
    role: str,
) -> EvidenceInventory:
    """把收口观察并入盘点谱系；不得把已有缺口抹掉。"""
    if any(
        item.role == role and item.capture_id == capture_id
        for item in inventory.capture_lineage
    ):
        return inventory
    capture_ids = inventory.capture_ids
    if capture_id not in capture_ids:
        capture_ids = (*capture_ids, capture_id)
    return EvidenceInventory(
        complete=inventory.complete,
        capture_ids=capture_ids,
        manifest_ids=inventory.manifest_ids,
        gaps=inventory.gaps,
        page_timeline=inventory.page_timeline,
        capture_lineage=(
            *inventory.capture_lineage,
            EvidenceCaptureLineage(capture_id=capture_id, role=role),
        ),
        artifacts=inventory.artifacts,
        action_logs=inventory.action_logs,
    )


def _with_missing_terminal(inventory: EvidenceInventory, *, session_id: str) -> EvidenceInventory:
    extra = EvidenceGapItem(
        code=EvidenceGapCode.MISSING_TERMINAL_OBSERVATION,
        subject_id=session_id,
        detail="收口时未能记录最后一次页面观察",
    )
    if any(item.code is EvidenceGapCode.MISSING_TERMINAL_OBSERVATION for item in inventory.gaps):
        return inventory
    return EvidenceInventory(
        complete=False,
        capture_ids=inventory.capture_ids,
        manifest_ids=inventory.manifest_ids,
        gaps=(*inventory.gaps, extra),
        page_timeline=inventory.page_timeline,
        capture_lineage=inventory.capture_lineage,
        artifacts=inventory.artifacts,
        action_logs=inventory.action_logs,
    )


def _with_write_timeout(inventory: EvidenceInventory, *, session_id: str) -> EvidenceInventory:
    extra = EvidenceGapItem(
        code=EvidenceGapCode.WRITE_TIMEOUT,
        subject_id=session_id,
        detail="有限重试原写入／在途命令后仍未排空，已固化 evidence_insufficient",
    )
    if any(item.code is EvidenceGapCode.WRITE_TIMEOUT for item in inventory.gaps):
        return inventory
    return EvidenceInventory(
        complete=False,
        capture_ids=inventory.capture_ids,
        manifest_ids=inventory.manifest_ids,
        gaps=(*inventory.gaps, extra),
        page_timeline=inventory.page_timeline,
        capture_lineage=inventory.capture_lineage,
        artifacts=inventory.artifacts,
        action_logs=inventory.action_logs,
    )


def build_memory_persistence_gate(
    *,
    inventory: EvidenceInventory | None = None,
    retry_inventory: EvidenceInventory | None = None,
    now: Callable[[], datetime] | None = None,
    max_write_retries: int = _DEFAULT_MAX_WRITE_RETRIES,
    retry_sleep_seconds: float = 0,
) -> AppExplorationPersistenceGate:
    """组合根测试注入：齐全盘点，不触库、不发 Inspection/Probe。"""
    return AppExplorationPersistenceGate(
        repository=MemoryEvidenceIndexRepository(
            inventory=inventory,
            retry_inventory=retry_inventory,
        ),
        now=now,
        max_write_retries=max_write_retries,
        retry_sleep_seconds=retry_sleep_seconds,
    )


async def _stop_new_actions(
    *,
    runtime: Any | None,
    repository: EvidenceIndexRepository,
    task_id: str,
    session_id: str,
) -> None:
    sessions = getattr(runtime, "surface_sessions", None) if runtime is not None else None
    if sessions is not None:
        get = getattr(sessions, "get", None)
        begin = getattr(sessions, "begin_draining", None)
        if callable(get) and callable(begin):
            try:
                snapshot = await get(session_id=session_id)
                if (
                    snapshot is not None
                    and getattr(snapshot, "task_id", None) == task_id
                    and getattr(snapshot, "status", None) is SurfaceSessionStatus.ACTIVE
                ):
                    await begin(
                        task_id=task_id,
                        session_id=session_id,
                        expected_row_version=snapshot.row_version,
                    )
            except Exception:  # noqa: BLE001 - 停新动作失败仍继续盘点，禁止改走补采
                logger.warning(
                    "task_id=%s session_id=%s begin_draining 失败，继续原写入重试",
                    task_id,
                    session_id,
                    exc_info=True,
                )
    supersede = getattr(repository, "supersede_unstarted_exploration_works", None)
    if callable(supersede):
        try:
            await supersede(task_id=task_id, session_id=session_id)
        except Exception:  # noqa: BLE001
            logger.warning(
                "task_id=%s session_id=%s 停止未领取 Exploration Work 失败",
                task_id,
                session_id,
                exc_info=True,
            )


async def _retry_original_writes(
    *,
    repository: EvidenceIndexRepository,
    task_id: str,
    session_id: str,
) -> None:
    retry = getattr(repository, "retry_original_writes", None)
    if not callable(retry):
        return
    try:
        await retry(task_id=task_id, session_id=session_id)
    except Exception:  # noqa: BLE001 - 重试失败保留在途，由上层决定暂不截止
        logger.warning(
            "task_id=%s session_id=%s 重试原写入失败",
            task_id,
            session_id,
            exc_info=True,
        )


async def _record_last_observation(
    *,
    runtime: Any | None,
    repository: EvidenceIndexRepository,
    task_id: str,
    session_id: str,
    role: str,
) -> str | None:
    runtime_record = (
        getattr(runtime, "record_terminal_observation", None) if runtime is not None else None
    )
    if callable(runtime_record):
        try:
            recorded = await runtime_record(
                task_id=task_id,
                session_id=session_id,
                role=role,
            )
            if recorded:
                return str(recorded)
        except Exception:  # noqa: BLE001 - 设备观察失败仍记录缺口，禁止改走补采
            logger.warning(
                "task_id=%s session_id=%s runtime 收口观察失败",
                task_id,
                session_id,
                exc_info=True,
            )
    record = getattr(repository, "record_terminal_observation", None)
    if not callable(record):
        return None
    try:
        recorded = await record(task_id=task_id, session_id=session_id, role=role)
    except Exception:  # noqa: BLE001
        logger.warning(
            "task_id=%s session_id=%s 收口观察落库失败",
            task_id,
            session_id,
            exc_info=True,
        )
        return None
    return str(recorded) if recorded else None


async def _fail_pending_manifests(
    *,
    repository: EvidenceIndexRepository,
    task_id: str,
    session_id: str,
) -> None:
    fail = getattr(repository, "fail_pending_manifests", None)
    if not callable(fail):
        return
    try:
        await fail(task_id=task_id, session_id=session_id)
    except Exception:  # noqa: BLE001 - 固化失败仍由 Index 记录 write_timeout
        logger.warning(
            "task_id=%s session_id=%s 固化 pending Manifest fail 失败",
            task_id,
            session_id,
            exc_info=True,
        )


async def _poll_close_readiness(
    *,
    runtime: Any | None,
    task_id: str,
    session_id: str,
) -> SessionCloseReadiness | None:
    sessions = getattr(runtime, "surface_sessions", None) if runtime is not None else None
    if sessions is None:
        return None
    close_readiness = getattr(sessions, "close_readiness", None)
    if not callable(close_readiness):
        return None
    try:
        readiness = await close_readiness(task_id=task_id, session_id=session_id)
    except Exception:  # noqa: BLE001 - 排空快照失败仍用盘点缺口，禁止改走补采
        logger.warning(
            "task_id=%s session_id=%s close_readiness 失败，继续用盘点缺口",
            task_id,
            session_id,
            exc_info=True,
        )
        return None
    if not isinstance(readiness, SessionCloseReadiness):
        return None
    return readiness


def merge_close_readiness_gaps(
    inventory: EvidenceInventory,
    *,
    readiness: SessionCloseReadiness | None,
    session_id: str,
) -> EvidenceInventory:
    """把 Session 关闭排空快照并入盘点；在途命令不得伪装齐全。"""
    if readiness is None or readiness.ready:
        return inventory
    extras: list[EvidenceGapItem] = []
    if (
        readiness.pending_actions > 0
        or readiness.running_restorations > 0
        or readiness.nonterminal_active_work_items > 0
        or readiness.nonterminal_app_frontier_items > 0
        or readiness.pinned_surfaces > 0
    ):
        extras.append(
            EvidenceGapItem(
                code=EvidenceGapCode.IN_FLIGHT_COMMAND,
                subject_id=session_id,
                detail=(
                    "close_readiness 仍有在途设备命令／工作／恢复／钉住 Surface"
                    f"（actions={readiness.pending_actions},"
                    f" restorations={readiness.running_restorations},"
                    f" work_items={readiness.nonterminal_active_work_items},"
                    f" app_frontier={readiness.nonterminal_app_frontier_items},"
                    f" pinned={readiness.pinned_surfaces}）"
                ),
            )
        )
    if (
        readiness.pending_capture_commits > 0
        or readiness.active_evidence_requests > 0
        or readiness.pending_manifests > 0
        or readiness.building_evidence_indexes > 0
    ):
        extras.append(
            EvidenceGapItem(
                code=EvidenceGapCode.PENDING_OBJECT_WRITE,
                subject_id=session_id,
                detail=(
                    "close_readiness 仍有对象存储／绑定事务"
                    f"（capture_commits={readiness.pending_capture_commits},"
                    f" evidence_requests={readiness.active_evidence_requests},"
                    f" manifests={readiness.pending_manifests},"
                    f" evidence_indexes={readiness.building_evidence_indexes}）"
                ),
            )
        )
    if not extras:
        return inventory
    seen = {(item.code, item.subject_id) for item in inventory.gaps}
    merged = list(inventory.gaps)
    for item in extras:
        key = (item.code, item.subject_id)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return EvidenceInventory(
        complete=False,
        capture_ids=inventory.capture_ids,
        manifest_ids=inventory.manifest_ids,
        gaps=tuple(merged),
        page_timeline=inventory.page_timeline,
        capture_lineage=inventory.capture_lineage,
        artifacts=inventory.artifacts,
        action_logs=inventory.action_logs,
    )
