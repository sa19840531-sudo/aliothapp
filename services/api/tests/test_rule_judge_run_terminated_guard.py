"""Run 终态后不再发起判定的守卫回归。

实测一次任务里 82% 的判定 token 花在 Agent Run 已失败之后，而 Run 终态后
工作项没有宿主可领，这些判定结果在收尾时 100% 被作废——纯烧钱且不产出。

守卫必须 fail-open：查询异常一律放行，宁可多判一次也不能误杀正在进行的
判定，那会真的掉质检率。
"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.agents.runtime.run_context import (
    AgentRunContext,
    bind_agent_run_context,
)
from services.api.app.agents.tools import rule_judge_tools

RULES = [
    {"rule_code": "alioth_1", "judge_input": {}},
    {"rule_code": "alioth_2", "judge_input": {}},
]


class _Repo:
    def __init__(self, *, terminal: bool | Exception) -> None:
        self._terminal = terminal
        self.calls = 0

    async def agent_run_is_terminal(self, *, agent_run_id: str) -> bool:
        self.calls += 1
        if isinstance(self._terminal, Exception):
            raise self._terminal
        return self._terminal


class _JudgeService:
    """任何一次调用都说明守卫没拦住。"""

    def __init__(self) -> None:
        self.calls = 0

    async def judge(self, request: Any) -> Any:
        self.calls += 1
        raise AssertionError("Run 终态后不应再发起判定")


@pytest.fixture(autouse=True)
def _clear_guard_cache():
    rule_judge_tools._TERMINATED_RUNS.clear()
    yield
    rule_judge_tools._TERMINATED_RUNS.clear()


def _patch_repo(monkeypatch: pytest.MonkeyPatch, repo: _Repo) -> None:
    monkeypatch.setattr(rule_judge_tools, "_get_rule_judge_repository", lambda: repo)


async def test_terminated_run_short_circuits_without_calling_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _Repo(terminal=True)
    _patch_repo(monkeypatch, repo)
    judge = _JudgeService()

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await rule_judge_tools.judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=[],
            rules=list(RULES),
            service=judge,
        )

    assert judge.calls == 0
    assert result["execution_status"] == "blocked"
    assert result["succeeded_count"] == 0
    assert result["failed_count"] == len(RULES)
    assert {item["error"]["code"] for item in result["items"]} == {"RULE_JUDGE_RUN_TERMINATED"}


async def test_short_circuit_covers_every_rule_in_the_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """整批都要有结论，不能只拦一部分导致工作项永远等不到结果。"""
    _patch_repo(monkeypatch, _Repo(terminal=True))

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await rule_judge_tools.judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=[],
            rules=list(RULES),
            service=_JudgeService(),
        )

    assert [item["rule_code"] for item in result["items"]] == ["alioth_1", "alioth_2"]
    assert all(item["retryable"] is False for item in result["items"])


async def test_terminal_state_is_remembered_to_avoid_repeated_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """终态不会回退成 running，记住即可零成本短路后续判定。"""
    repo = _Repo(terminal=True)
    _patch_repo(monkeypatch, repo)

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        for _ in range(3):
            await rule_judge_tools.judge_rules_for_current_run(
                page_capture_id="capture-1",
                evidence_artifact_ids=[],
                rules=list(RULES),
                service=_JudgeService(),
            )

    assert repo.calls == 1


async def test_active_run_is_not_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run 仍存活时必须放行，否则直接掉质检率。"""
    repo = _Repo(terminal=False)
    _patch_repo(monkeypatch, repo)
    reached = {"judged": False}

    class _Reaching(_JudgeService):
        async def judge(self, request: Any) -> Any:
            reached["judged"] = True
            raise RuntimeError("stop-here")

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        result = await rule_judge_tools.judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=[],
            rules=list(RULES),
            service=_Reaching(),
        )

    assert reached["judged"] is True
    assert "RULE_JUDGE_RUN_TERMINATED" not in str(result)


async def test_lookup_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    """守卫查询异常必须放行——误杀正常判定比多判一次严重得多。"""
    repo = _Repo(terminal=RuntimeError("数据库抖动"))
    _patch_repo(monkeypatch, repo)
    reached = {"judged": False}

    class _Reaching(_JudgeService):
        async def judge(self, request: Any) -> Any:
            reached["judged"] = True
            raise RuntimeError("stop-here")

    with bind_agent_run_context(AgentRunContext(task_id="task-1", agent_run_id="run-1")):
        await rule_judge_tools.judge_rules_for_current_run(
            page_capture_id="capture-1",
            evidence_artifact_ids=[],
            rules=list(RULES),
            service=_Reaching(),
        )

    assert reached["judged"] is True
    assert rule_judge_tools._TERMINATED_RUNS == set()


async def test_missing_run_context_does_not_block(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _Repo(terminal=True)
    _patch_repo(monkeypatch, repo)

    assert await rule_judge_tools._agent_run_is_terminal(None) is False
    assert repo.calls == 0
