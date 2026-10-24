"""Web 主线只登记引导候选；异步支线结果不参与主阶段成败。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from services.api.app.application.web_guide_candidate import select_web_guide_candidates
from services.api.app.application.web_guide_page import WebGuideOutcome

_LOGGER = logging.getLogger(__name__)


class WebGuideBranchDispatcher:
    def __init__(
        self, *, branch, ledger, task_id: str, agent_run_id: str,
        session_id: str, deadline: datetime, limit: int = 2,
        claim_timeout_seconds: float = 1.5,
    ):
        self._branch = branch
        self._ledger = ledger
        self._task_id = task_id
        self._agent_run_id = agent_run_id
        self._session_id = session_id
        self._deadline = deadline
        self._limit = limit
        self._claim_timeout_seconds = claim_timeout_seconds
        self._seen: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._task_starts: dict[asyncio.Task, asyncio.Event] = {}
        self._auditing: set[asyncio.Task] = set()
        self._registration_lock = asyncio.Lock()
        self._closed = False

    async def schedule(self, *, source_capture_id: str, source_url: str, snapshot) -> None:
        """登记最多两个入口；任何可选分支错误不得冒泡至主线。"""

        if self._closed or datetime.now(UTC) >= self._deadline or len(self._seen) >= self._limit:
            return
        try:
            candidates = select_web_guide_candidates(
                source_capture_id=source_capture_id, source_url=source_url,
                snapshot=snapshot, limit=self._limit,
            )
        except Exception:  # noqa: BLE001 - 入口解析故障不是主线故障
            _LOGGER.warning("web_guide_candidate_failed run_id=%s", self._agent_run_id)
            return
        for candidate in candidates:
            async with self._registration_lock:
                if self._closed or len(self._seen) >= self._limit:
                    break
                if candidate.destination_url in self._seen:
                    continue
                try:
                    phase = await asyncio.wait_for(
                        self._ledger.claim(
                            candidate=candidate, task_id=self._task_id,
                            session_id=self._session_id,
                        ),
                        timeout=self._claim_timeout_seconds,
                    )
                except Exception:  # noqa: BLE001 - 阶段登记失败仍不改变主阶段结果
                    _LOGGER.warning("web_guide_claim_failed run_id=%s", self._agent_run_id)
                    continue
                if phase is None:
                    continue
                self._seen.add(candidate.destination_url)
                started = asyncio.Event()
                task = asyncio.create_task(
                    self._attempt(candidate, phase, started=started),
                    name=f"web-guide:{candidate.candidate_id}",
                )
                self._tasks.add(task)
                self._task_starts[task] = started
                task.add_done_callback(self._forget_task)

    def _forget_task(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        self._task_starts.pop(task, None)
        self._auditing.discard(task)

    async def _attempt(self, candidate, phase, *, started: asyncio.Event) -> None:
        started.set()
        try:
            # 关闭期间完成登记的支线只留 skipped 审计，不再打开隔离浏览器。
            if self._closed:
                raise asyncio.CancelledError
            outcome = await self._branch.attempt(
                candidate, task_id=self._task_id, agent_run_id=self._agent_run_id,
            )
        except asyncio.CancelledError:
            outcome = WebGuideOutcome(
                "skipped", "WEB_GUIDE_MAINLINE_FINISHED",
                candidate.candidate_id, candidate.source_capture_id,
            )
        except Exception:  # noqa: BLE001 - 未预期支线错误也只记支线
            outcome = WebGuideOutcome(
                "incomplete", "WEB_GUIDE_BRANCH_FAILED",
                candidate.candidate_id, candidate.source_capture_id,
            )
        task = asyncio.current_task()
        self._auditing.add(task)
        try:
            await self._ledger.complete(phase=phase, outcome=outcome)
        except Exception:  # noqa: BLE001 - 独立审计写回失败不能使主线失败
            _LOGGER.warning("web_guide_result_failed run_id=%s", self._agent_run_id)

    async def finish(self) -> None:
        """主线收口后只给支线短暂清理窗口，未完成即跳过。"""

        if not self._tasks:
            return
        _done, pending = await asyncio.wait(self._tasks, timeout=2.0)
        for task in pending:
            if not task.cancelling() and task not in self._auditing:
                task.cancel()
        if pending:
            _, still_pending = await asyncio.wait(pending, timeout=5.0)
            if still_pending:
                _LOGGER.warning(
                    "web_guide_cleanup_pending run_id=%s count=%s",
                    self._agent_run_id, len(still_pending),
                )

    async def close(self) -> None:
        """退出当前 Run 前等待本调度器的隔离浏览器与终态审计真正结束。"""

        self._closed = True
        cleanup = asyncio.create_task(self._close_owned_tasks())
        interrupted = False
        while not cleanup.done():
            try:
                # 主流程重复收到取消时，仍须等拥有的支线停止后才能释放运行许可。
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        cleanup.result()
        if interrupted:
            raise asyncio.CancelledError

    async def _close_owned_tasks(self) -> None:
        async with self._registration_lock:
            tasks = tuple(self._tasks)
            starts = tuple(self._task_starts[task] for task in tasks)
        if not tasks:
            return
        # 未开始的 Task 直接取消不会进入异常处理，须先让它接管已登记阶段的审计。
        await asyncio.gather(*(started.wait() for started in starts))
        for task in tasks:
            # 已取消或已进入终态写回的支线不能再次取消打断浏览器清理及审计。
            if not task.done() and not task.cancelling() and task not in self._auditing:
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
