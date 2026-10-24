"""Web 正式质检 Session 的同进程关闭与跨进程恢复合同。"""

from __future__ import annotations

from datetime import UTC, datetime

from services.api.app.application.web_formal_session_closure import (
    DurableWebFormalSessionCloser,
)


async def test_formal_session_closer_uses_live_runtime_then_durable_fallback() -> None:
    events: list[tuple[str, str, str]] = []

    class Runtime:
        def __init__(self, source: str) -> None:
            self.source = source

        async def close_session(self, *, task_id: str, session_id: str) -> None:
            events.append((self.source, task_id, session_id))

    fallback_builds = 0

    def build_fallback() -> Runtime:
        nonlocal fallback_builds
        fallback_builds += 1
        return Runtime("recovered")

    closer = DurableWebFormalSessionCloser(runtime_factory=build_fallback)
    closer.bind_live_runtime(
        session_id="web:run-1",
        runtime=Runtime("live"),
    )

    await closer.close(task_id="task-1", session_id="web:run-1")
    await closer.close(task_id="task-1", session_id="web:run-1")

    assert events == [
        ("live", "task-1", "web:run-1"),
        ("recovered", "task-1", "web:run-1"),
    ]
    assert fallback_builds == 1


async def test_formal_session_closer_keeps_live_runtime_after_transient_failure() -> None:
    calls = 0

    class Runtime:
        async def close_session(self, **_kwargs: object) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("browser close transient")

    def unexpected_fallback() -> Runtime:
        raise AssertionError("同进程重试必须继续使用原浏览器 Runtime")

    closer = DurableWebFormalSessionCloser(runtime_factory=unexpected_fallback)
    closer.bind_live_runtime(session_id="web:run-1", runtime=Runtime())

    try:
        await closer.close(task_id="task-1", session_id="web:run-1")
    except RuntimeError as exc:
        assert str(exc) == "browser close transient"
    else:
        raise AssertionError("首次关闭应暴露瞬时失败")

    await closer.close(task_id="task-1", session_id="web:run-1")

    assert calls == 2


async def test_formal_session_closer_treats_missing_persistent_session_as_closed() -> None:
    events: list[tuple[str, str, str]] = []

    class Sessions:
        async def get(self, *, session_id: str) -> None:
            assert session_id == "web:run-1"
            return None

    class Gate:
        async def release_session(self, *, task_id: str, session_id: str) -> None:
            events.append(("release", task_id, session_id))

    class Runtime:
        surface_sessions = Sessions()
        session_command_gate = Gate()

        async def close_session(self, **_kwargs: object) -> None:
            raise AssertionError("缺失持久化 Session 不得重复进入正式 close 状态机")

        async def close_browser_session(self, session_id: str) -> None:
            events.append(("browser", "task-1", session_id))

    runtime = Runtime()
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)

    await closer.close(task_id="task-1", session_id="web:run-1")
    await closer.close(task_id="task-1", session_id="web:run-1")

    assert events == [
        ("browser", "task-1", "web:run-1"),
        ("release", "task-1", "web:run-1"),
        ("browser", "task-1", "web:run-1"),
        ("release", "task-1", "web:run-1"),
    ]


async def test_formal_session_closer_recovers_after_browser_failure_marked_lost() -> None:
    events: list[str] = []
    session = type("Session", (), {"task_id": "task-1", "status": "active"})()

    class Sessions:
        async def get(self, *, session_id: str):
            assert session_id == "web:run-1"
            return session

    class Gate:
        async def release_session(self, **_kwargs: object) -> None:
            events.append("release")

    class Runtime:
        surface_sessions = Sessions()
        session_command_gate = Gate()

        async def close_session(self, **_kwargs: object) -> None:
            events.append("close")
            session.status = "lost"
            raise RuntimeError("browser close failed")

        async def close_browser_session(self, _session_id: str) -> None:
            events.append("browser-idempotent-close")

    runtime = Runtime()
    closer = DurableWebFormalSessionCloser(runtime_factory=lambda: runtime)
    closer.bind_live_runtime(session_id="web:run-1", runtime=runtime)

    try:
        await closer.close(task_id="task-1", session_id="web:run-1")
    except RuntimeError as exc:
        assert str(exc) == "browser close failed"
    else:
        raise AssertionError("首次 Driver 失败必须向上游暴露")

    await closer.close(task_id="task-1", session_id="web:run-1")

    assert events == [
        "close",
        "browser-idempotent-close",
        "release",
    ]


async def test_formal_session_closer_cancels_expired_orphan_command_lease() -> None:
    """失联 Worker 的过期命令租约不得永久阻止 Web 正式失败收口。"""

    events: list[str] = []
    session = type(
        "Session",
        (),
        {
            "task_id": "task-1",
            "status": "active",
        },
    )()

    class Sessions:
        async def get(self, *, session_id: str):
            assert session_id == "web:run-1"
            return session

    class Gate:
        async def cancel_expired_session(
            self,
            *,
            task_id: str,
            session_id: str,
            reason: str,
        ) -> bool:
            assert task_id == "task-1"
            assert session_id == "web:run-1"
            assert reason == "web_formal_orphaned_command_lease_expired"
            events.append("cancel")
            session.status = "lost"
            return True

        async def release_session(self, **_kwargs: object) -> None:
            events.append("release")

    class Runtime:
        surface_sessions = Sessions()
        session_command_gate = Gate()

        async def close_session(self, **_kwargs: object) -> None:
            raise AssertionError("过期孤儿租约应先转为 lost，不得进入普通关闭 CAS")

        async def close_browser_session(self, _session_id: str) -> None:
            events.append("browser")

    closer = DurableWebFormalSessionCloser(runtime_factory=Runtime)

    await closer.close(task_id="task-1", session_id="web:run-1")

    assert events == ["cancel", "browser", "release"]


async def test_formal_session_closer_revokes_active_command_lease_for_cancel() -> None:
    """显式取消必须立即撤销活跃命令租约，不能等待租约自然过期。"""

    events: list[str] = []
    session = type(
        "Session",
        (),
        {
            "task_id": "task-1",
            "status": "active",
        },
    )()

    class Sessions:
        async def get(self, *, session_id: str):
            assert session_id == "web:run-1"
            return session

    class Gate:
        async def cancel_session(
            self,
            *,
            task_id: str,
            session_id: str,
            reason: str,
        ) -> None:
            assert task_id == "task-1"
            assert session_id == "web:run-1"
            assert reason == "web_formal_run_canceled"
            events.append("cancel-active")
            session.status = "lost"

        async def cancel_expired_session(self, **_kwargs: object) -> bool:
            raise AssertionError("显式取消不得等待动作租约自然过期")

        async def release_session(self, **_kwargs: object) -> None:
            events.append("release")

    class Runtime:
        surface_sessions = Sessions()
        session_command_gate = Gate()

        async def close_session(self, **_kwargs: object) -> None:
            raise AssertionError("动作租约撤销后应按 lost 幂等释放，不再进入普通关闭 CAS")

        async def close_browser_session(self, _session_id: str) -> None:
            events.append("browser")

    closer = DurableWebFormalSessionCloser(runtime_factory=Runtime)

    await closer.close(
        task_id="task-1",
        session_id="web:run-1",
        cancel_active_commands=True,
    )

    assert events == ["cancel-active", "browser", "release"]


async def test_cancel_closure_fails_closed_without_command_revocation_capability() -> None:
    """显式取消不能在缺少命令闸门时假装已经安全关闭。"""

    calls = 0

    class Runtime:
        async def close_session(self, **_kwargs: object) -> None:
            nonlocal calls
            calls += 1

    closer = DurableWebFormalSessionCloser(runtime_factory=Runtime)

    try:
        await closer.close(
            task_id="task-1",
            session_id="web:run-1",
            cancel_active_commands=True,
        )
    except RuntimeError as exc:
        assert str(exc) == "Web 取消收口缺少 Session 命令撤销能力"
    else:
        raise AssertionError("缺少命令撤销能力时必须失败关闭")

    assert calls == 0


async def test_recovery_session_close_checks_fence_immediately_before_runtime_mutation(
    monkeypatch,
) -> None:
    """恢复 Session 的浏览器/持久化关闭副作用必须先校验当前 holder。"""

    events: list[object] = []
    expires_at = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)

    class Runtime:
        async def close_session(self, *, task_id: str, session_id: str) -> None:
            events.append(("close", task_id, session_id))

    async def assert_fence(_connection, **kwargs: object) -> None:
        events.append(("fence", kwargs))

    from services.api.app.application import web_formal_session_closure as module

    monkeypatch.setattr(module, "assert_finalization_recovery_fence", assert_fence)
    closer = DurableWebFormalSessionCloser(
        runtime_factory=Runtime,
        pool=_Pool(_Connection()),
    )

    await closer.close(
        task_id="task-1",
        session_id="web:run-1",
        recovery_lease_token="lease-1",
        recovery_generation=2,
        recovery_lease_expires_at=expires_at,
    )

    assert events == [
        (
            "fence",
            {
                "task_id": "task-1",
                "recovery_lease_token": "lease-1",
                "recovery_generation": 2,
                "recovery_lease_expires_at": expires_at,
            },
        ),
        ("close", "task-1", "web:run-1"),
    ]


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class _Connection:
    def transaction(self) -> _Transaction:
        return _Transaction()


class _Acquire:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    async def __aenter__(self) -> _Connection:
        return self._connection

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class _Pool:
    def __init__(self, connection: _Connection) -> None:
        self._connection = connection

    def acquire(self) -> _Acquire:
        return _Acquire(self._connection)
