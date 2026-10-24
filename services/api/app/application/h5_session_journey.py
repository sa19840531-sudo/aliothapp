"""H5 整段会话互斥；内部命令仍分别领取原有租约和序号。"""

import asyncio
from contextlib import asynccontextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4


class H5SessionJourneyLost(RuntimeError):
    error_code = "H5_SESSION_JOURNEY_LOST"
    retryable = False


class H5SessionJourneyDeadline(TimeoutError):
    error_code = "H5_SESSION_JOURNEY_DEADLINE_EXHAUSTED"
    retryable = False


_current_journeys = ContextVar("h5_physical_journeys", default=())


def quarantine_h5_journeys(reason):
    """清理未确认时留下隔离事实，不能把释放协程锁当作物理结束。"""
    for scope in _current_journeys.get():
        scope.quarantine_reason = reason


def quarantine_h5_browser_operations(reason):
    for scope in _current_journeys.get():
        if scope.resources and not scope.quarantine_reason:
            scope.quarantine_reason = reason


def assert_h5_journeys_active():
    """在物理返回与成功写回之间复核继承旅程，不接受迟到成功。"""
    for scope in _current_journeys.get():
        scope.check(scope.task_id)


def track_h5_physical_operation(task):
    for scope in _current_journeys.get():
        scope.operations.add(task)
        task.add_done_callback(scope.operations.discard)


def bind_h5_physical_resource(session_id, resource):
    """绑定实际持有的浏览器实例，不接受外部传入的停止布尔值。"""
    for scope in _current_journeys.get():
        scope.check(scope.task_id)
        if scope.session_id == session_id:
            scope.resources[resource.resource_id] = resource


@dataclass(eq=False)
class _Scope:
    task_id: str
    session_id: str
    owner: asyncio.Task
    active: bool = True
    lost: bool = False
    try_only: bool = False
    quarantine_reason: str | None = None
    token: str = field(default_factory=lambda: str(uuid4()))
    resources: dict = field(default_factory=dict)
    operations: set = field(default_factory=set)

    def invalidate(self, *, uncertain=True):
        self.lost = True
        self.active = False
        if uncertain:
            self.quarantine_reason = "H5_JOURNEY_CONTROL_CONNECTION_LOST"
        self.owner.cancel()

    def check(self, task_id):
        if task_id != self.task_id:
            raise ValueError("H5 会话旅程不属于当前任务")
        if self.quarantine_reason:
            raise H5SessionJourneyLost("H5 会话物理排空未确认，不能继续动作或写回成功")
        if not self.active:
            raise H5SessionJourneyLost("H5 会话旅程已失效，不能继续动作")


class H5SessionJourneyGate:
    def __init__(self, *, delegate, remote_hold=None):
        self._delegate = delegate
        self._remote_hold = remote_hold
        self._locks = {}
        self._lock_users = {}
        self._active = {}
        self._quarantined = {}
        self._scopes = ContextVar("h5_session_journeys", default=())

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    @asynccontextmanager
    async def hold_journey(self, *, task_id, session_id, join_existing=True):
        if session_id in self._quarantined:
            raise H5SessionJourneyLost("H5 会话物理操作未排空或未确认，不能继续旅程")
        scopes = self._scopes.get()
        prior = next((scope for scope in scopes if scope.session_id == session_id), None)
        if prior is not None:
            prior.check(task_id)
            if not join_existing:
                raise H5SessionJourneyLost("另一根工作不能继承当前旅程并插入动作")
            yield
            prior.check(task_id)
            return
        for scope in scopes:
            scope.check(task_id)
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        # 恢复会话的随机后缀不能决定是否允许恢复；嵌套只尝试，不交叉等待。
        if scopes and lock.locked():
            raise H5SessionJourneyLost("H5 恢复目标会话正在另一旅程中，拒绝交叉等待")
        async with self._hold_local_lock(session_id, lock):
            if session_id in self._quarantined:
                raise H5SessionJourneyLost("等待期间原旅程进入隔离，物理排空未确认")
            scope = _Scope(task_id, session_id, asyncio.current_task(), try_only=bool(scopes))
            token = self._scopes.set((*scopes, scope))
            physical_token = _current_journeys.set((*_current_journeys.get(), scope))
            self._active[session_id] = scope
            try:
                remote = self._remote_hold(scope) if self._remote_hold else nullcontext()
                async with remote:
                    scope.check(task_id)
                    try:
                        yield
                        scope.check(task_id)
                    except (asyncio.CancelledError, TimeoutError) as error:
                        # 必须先于远端finally写回；Python取消返回不代表CDP已停止。
                        if scope.resources and not scope.quarantine_reason:
                            scope.quarantine_reason = (
                                "H5_BROWSER_CANCEL_STOP_UNCONFIRMED"
                                if isinstance(error, asyncio.CancelledError)
                                else "H5_BROWSER_TIMEOUT_STOP_UNCONFIRMED"
                            )
                        raise
            except asyncio.CancelledError:
                if scope.lost:
                    raise H5SessionJourneyLost(
                        "H5 会话旅程已失效，不能证明物理操作已停止",
                    ) from None
                raise
            finally:
                if scope.quarantine_reason:
                    self._quarantined[session_id] = scope
                scope.active = False
                self._active.pop(session_id, None)
                self._scopes.reset(token)
                _current_journeys.reset(physical_token)

    @asynccontextmanager
    async def _hold_local_lock(self, session_id, lock):
        self._lock_users[session_id] = self._lock_users.get(session_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            users = self._lock_users[session_id] - 1
            if users:
                self._lock_users[session_id] = users
            else:
                self._lock_users.pop(session_id)
                self._locks.pop(session_id, None)

    async def resolve_quarantine(self, *, task_id, session_id):
        """仅核销原旅程的停止事实；不恢复旧作用域，也不宣告业务通过。"""
        scope = self._quarantined.get(session_id)
        if scope is None:
            return None
        if scope.task_id != task_id:
            raise ValueError("H5 隔离旅程不属于当前任务")
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with self._hold_local_lock(session_id, lock):
            if self._quarantined.get(session_id) is not scope or scope.active:
                raise H5SessionJourneyLost("H5 停止证明的原旅程围栏已改变")
            if not scope.resources:
                raise H5SessionJourneyLost("H5 原物理资源缺少停止证明，不能解封")
            proofs = [await resource.stop_and_prove() for resource in scope.resources.values()]
            pending = {task for task in scope.operations if not task.done()}
            if pending:
                _, pending = await asyncio.wait(pending, timeout=5)
            if pending:
                raise H5SessionJourneyLost("H5 原物理操作仍未排空，不能解封")
            await self._ack_physical_stop(scope, proofs)
            self._quarantined.pop(session_id)
            return proofs

    async def _ack_physical_stop(self, scope, proofs):
        pass

    @asynccontextmanager
    async def hold(self, *, task_id, session_id, **kwargs):
        journey = (self.hold_journey(task_id=task_id, session_id=session_id)
                   if session_id.startswith("h5:") else nullcontext())
        async with journey:
            async with self._delegate.hold(
                task_id=task_id, session_id=session_id, **kwargs,
            ) as permit:
                yield permit

    async def release_session(self, *, task_id, session_id):
        if session_id in self._quarantined:
            raise H5SessionJourneyLost("H5 会话物理操作未排空或未确认，不能释放")
        if session_id in self._active or self._locks.get(session_id, asyncio.Lock()).locked():
            raise RuntimeError("H5 会话仍有整段旅程，不能释放")
        await self._delegate.release_session(task_id=task_id, session_id=session_id)

    async def cancel_session(self, *, task_id, session_id, **kwargs):
        scope = self._active.get(session_id)
        if scope is not None and scope.task_id != task_id:
            raise ValueError("H5 会话不属于当前任务")
        await self._delegate.cancel_session(task_id=task_id, session_id=session_id, **kwargs)
        if scope is not None:
            scope.invalidate(uncertain=False)


@asynccontextmanager
async def h5_session_journey(*, gate, task_id, session_id, deadline, clock=None):
    """等待锁也消耗原冻结墙钟，不刷新动作或物理调用预算。"""
    now = clock or (lambda: datetime.now(UTC))
    hold = getattr(gate, "hold_journey", None)
    if not callable(hold):
        yield
        return
    seconds = (deadline - now()).total_seconds()
    if seconds <= 0:
        raise H5SessionJourneyDeadline("H5 会话旅程已超过冻结截止")
    timeout = asyncio.timeout(seconds)
    try:
        async with timeout:
            async with hold(task_id=task_id, session_id=session_id, join_existing=False):
                yield
                if now() >= deadline:
                    raise H5SessionJourneyDeadline("H5 会话旅程结果晚于冻结截止")
    except TimeoutError:
        if not timeout.expired():
            raise
        raise H5SessionJourneyDeadline(
            "等待或执行旅程耗尽冻结截止；物理停止状态以排空证明为准",
        ) from None
