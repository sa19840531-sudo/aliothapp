"""独立规则质检运行编排契约测试（RuleEvaluationRunService）。

对应 V2 入口 POST /api/v2/tasks/{task_id}/rule-evaluation-runs 的接入要求：
只用既有证据判定、结果独立保存（qc_rule_evaluation_run）、
不触碰正式规则结果，运行状态覆盖 成功/失败/证据不足 三终态。
"""

from collections.abc import Sequence
from typing import Any

import pytest

from services.api.app.application.rule_evaluation_runs import (
    JudgeablePage,
    RuleEvaluationRun,
    RuleEvaluationRunService,
    RuleEvaluationRunStatus,
    RuleEvaluationTaskNotFoundError,
)
from services.api.app.application.rule_judge import RuleJudgeRequest, RuleJudgeResult
from services.api.app.application.task_query import CandidateRule
from services.api.app.domain.iam.access_scope import AccessScope

TEST_ACCESS_SCOPE = AccessScope(
    actor_user_id="test-user",
    tenant_id="tenant-1",
    permissions=frozenset({"taskObjects:read:any", "taskObjects:write:any"}),
)


def _candidate(*, rule_code: str, page_type: str) -> CandidateRule:
    return CandidateRule(
        rule_id=f"rule-{rule_code}",
        rule_code=rule_code,
        name="测试规则",
        category="text",
        severity="low",
        executor_type="hybrid",
        page_type=page_type,
        applicable_page_types=[page_type],
        check_point="",
        judge_config={},
        sort_order=1,
        match_reason="applicable_page_types",
        input_contract={},
    )


def _judge_result(request: RuleJudgeRequest) -> RuleJudgeResult:
    return RuleJudgeResult(
        rule_id=f"rule-{request.rule_code}",
        rule_code=request.rule_code,
        executor_type="hybrid",
        judge_strategy="deterministic",
        execution_status="completed",
        confidence=0.9,
        result_summary="测试结论",
        problem_desc=None,
        page_capture_id=request.page_capture_id,
        evidence_artifact_ids=(),
        judge_input={},
        judge_output={},
    )


class _FakeTaskLookup:
    def __init__(self, *, exists: bool = True) -> None:
        self._exists = exists

    async def get_task_detail(
        self, task_id: str, *, access_scope: AccessScope
    ) -> object | None:
        assert access_scope is not None
        return object() if self._exists else None


class _FakePageSource:
    def __init__(self, pages: list[JudgeablePage]) -> None:
        self._pages = pages

    async def list_judgeable_pages(
        self, *, task_id: str, access_scope: AccessScope
    ) -> list[JudgeablePage]:
        assert access_scope is not None
        return self._pages


class _FakeCandidateSource:
    def __init__(self, by_page_type: dict[str, list[CandidateRule]]) -> None:
        self._by_page_type = by_page_type
        self.calls: list[str] = []

    async def list_candidate_rules(
        self,
        *,
        task_id: str,
        page_type: str,
        access_scope: AccessScope,
    ) -> list[CandidateRule]:
        assert access_scope is not None
        self.calls.append(page_type)
        return self._by_page_type.get(page_type, [])


class _FakeJudge:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
        self.requests: list[RuleJudgeRequest] = []

    async def judge_many(self, requests: list[RuleJudgeRequest]) -> list[RuleJudgeResult]:
        if self._error is not None:
            raise self._error
        self.requests.extend(requests)
        return [_judge_result(request) for request in requests]


class _FakeRunRepository:
    def __init__(self, *, finish_error: Exception | None = None) -> None:
        self.inserted: list[RuleEvaluationRun] = []
        self.running: list[str] = []
        self.finished: list[dict[str, Any]] = []
        self._finish_error = finish_error

    async def insert_pending(
        self, run: RuleEvaluationRun, *, access_scope: AccessScope
    ) -> None:
        assert access_scope is not None
        self.inserted.append(run)

    async def mark_running(self, *, run_id: str) -> None:
        self.running.append(run_id)

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
        if self._finish_error is not None:
            raise self._finish_error
        self.finished.append(
            {
                "run_id": run_id,
                "status": status,
                "result_payload": list(result_payload),
                "page_count": page_count,
                "rule_count": rule_count,
                "error_message": error_message,
            }
        )

    async def get(
        self,
        *,
        run_id: str,
        task_id: str,
        access_scope: AccessScope,
    ) -> RuleEvaluationRun | None:
        del run_id, task_id, access_scope
        return None


def _service(
    *,
    exists: bool = True,
    pages: list[JudgeablePage] | None = None,
    candidates: dict[str, list[CandidateRule]] | None = None,
    judge: _FakeJudge | None = None,
    repository: _FakeRunRepository | None = None,
):
    scheduled: list[Any] = []
    repo = repository if repository is not None else _FakeRunRepository()
    judge_port = judge if judge is not None else _FakeJudge()
    candidate_source = _FakeCandidateSource(candidates or {})
    service = RuleEvaluationRunService(
        task_lookup=_FakeTaskLookup(exists=exists),
        page_source=_FakePageSource(pages or []),
        candidate_source=candidate_source,
        judge=judge_port,
        repository=repo,
        run_id_factory=lambda: "rule-eval:test",
        spawn=scheduled.append,
    )
    return service, repo, judge_port, candidate_source, scheduled


async def _submit_and_run(service, scheduled) -> None:
    await service.submit("task-1", access_scope=TEST_ACCESS_SCOPE)
    assert len(scheduled) == 1
    await scheduled[0]


async def test_submit_unknown_task_raises_not_found() -> None:
    service, repo, _judge, _candidates, scheduled = _service(exists=False)

    with pytest.raises(RuleEvaluationTaskNotFoundError):
        await service.submit("task-missing", access_scope=TEST_ACCESS_SCOPE)

    assert repo.inserted == []
    assert scheduled == []


async def test_submit_returns_pending_and_defers_execution() -> None:
    service, repo, _judge, _candidates, scheduled = _service(
        pages=[JudgeablePage(page_capture_id="capture-1", page_type="home")]
    )

    result = await service.submit("task-1", access_scope=TEST_ACCESS_SCOPE)

    assert result.task_id == "task-1"
    assert result.rule_evaluation_run_id == "rule-eval:test"
    assert result.status == "pending"
    assert [run.status for run in repo.inserted] == [RuleEvaluationRunStatus.PENDING]
    assert repo.inserted[0].requested_by == TEST_ACCESS_SCOPE.actor_user_id
    assert repo.finished == []
    assert len(scheduled) == 1
    scheduled[0].close()


async def test_execute_judges_all_pages_and_caches_candidates_per_page_type() -> None:
    pages = [
        JudgeablePage(page_capture_id="capture-1", page_type="home"),
        JudgeablePage(page_capture_id="capture-2", page_type="home"),
        JudgeablePage(page_capture_id="capture-3", page_type="video_detail"),
    ]
    candidates = {
        "home": [
            _candidate(rule_code="alioth_1", page_type="home"),
            _candidate(rule_code="alioth_2", page_type="home"),
        ],
        "video_detail": [_candidate(rule_code="alioth_3", page_type="video_detail")],
    }
    service, repo, judge, candidate_source, scheduled = _service(pages=pages, candidates=candidates)

    await _submit_and_run(service, scheduled)

    assert candidate_source.calls == ["home", "video_detail"]
    assert len(judge.requests) == 5
    (finish,) = repo.finished
    assert finish["status"] is RuleEvaluationRunStatus.SUCCEEDED
    assert finish["page_count"] == 3
    assert finish["rule_count"] == 5
    assert len(finish["result_payload"]) == 5
    sample = finish["result_payload"][0]
    assert sample["rule_code"] == "alioth_1"
    assert sample["page_capture_id"] == "capture-1"
    assert sample["execution_status"] == "completed"
    assert repo.running == ["rule-eval:test"]


async def test_execute_without_pages_finishes_evidence_insufficient() -> None:
    service, repo, judge, _candidates, scheduled = _service(pages=[])

    await _submit_and_run(service, scheduled)

    (finish,) = repo.finished
    assert finish["status"] is RuleEvaluationRunStatus.EVIDENCE_INSUFFICIENT
    assert "没有可判定" in finish["error_message"]
    assert judge.requests == []


async def test_execute_with_untyped_pages_or_no_candidates_succeeds_empty() -> None:
    service, repo, judge, _candidates, scheduled = _service(
        pages=[
            JudgeablePage(page_capture_id="capture-1", page_type=None),
            JudgeablePage(page_capture_id="capture-2", page_type="home"),
        ],
        candidates={"home": []},
    )

    await _submit_and_run(service, scheduled)

    (finish,) = repo.finished
    assert finish["status"] is RuleEvaluationRunStatus.SUCCEEDED
    assert finish["page_count"] == 2
    assert finish["rule_count"] == 0
    assert finish["result_payload"] == []
    assert judge.requests == []


async def test_execute_judge_failure_finishes_failed_with_reason() -> None:
    service, repo, _judge, _candidates, scheduled = _service(
        pages=[JudgeablePage(page_capture_id="capture-1", page_type="home")],
        candidates={"home": [_candidate(rule_code="alioth_1", page_type="home")]},
        judge=_FakeJudge(error=RuntimeError("LLM 网关不可用")),
    )

    await _submit_and_run(service, scheduled)

    (finish,) = repo.finished
    assert finish["status"] is RuleEvaluationRunStatus.FAILED
    assert "LLM 网关不可用" in finish["error_message"]


async def test_execute_terminal_write_failure_never_escapes() -> None:
    repository = _FakeRunRepository(finish_error=RuntimeError("账本不可写"))
    service, _repo, _judge, _candidates, scheduled = _service(
        pages=[],
        repository=repository,
    )

    await _submit_and_run(service, scheduled)


async def test_default_run_id_carries_stable_prefix() -> None:
    repo = _FakeRunRepository()
    scheduled: list[Any] = []
    service = RuleEvaluationRunService(
        task_lookup=_FakeTaskLookup(),
        page_source=_FakePageSource([]),
        candidate_source=_FakeCandidateSource({}),
        judge=_FakeJudge(),
        repository=repo,
        spawn=scheduled.append,
    )

    result = await service.submit("task-1", access_scope=TEST_ACCESS_SCOPE)

    assert result.rule_evaluation_run_id.startswith("rule-eval:")
    scheduled[0].close()


async def test_submit_records_normalized_rule_codes_and_filters_candidates() -> None:
    service, repo, judge, _candidates, scheduled = _service(
        pages=[JudgeablePage(page_capture_id="capture-1", page_type="home")],
        candidates={
            "home": [
                _candidate(rule_code="alioth_1", page_type="home"),
                _candidate(rule_code="alioth_2", page_type="home"),
            ]
        },
    )

    await service.submit(
        "task-1",
        rule_codes=[" alioth_2 ", "alioth_2", ""],
        access_scope=TEST_ACCESS_SCOPE,
    )
    await scheduled[0]

    assert repo.inserted[0].requested_rule_codes == ("alioth_2",)
    assert [request.rule_code for request in judge.requests] == ["alioth_2"]
    (finish,) = repo.finished
    assert finish["status"] is RuleEvaluationRunStatus.SUCCEEDED
    assert finish["rule_count"] == 1


async def test_alioth_17_only_judges_after_action_with_complete_ad_pair() -> None:
    pages = [
        JudgeablePage(
            page_capture_id="startup-only",
            page_type="startup_ad",
            surface_kind="android",
        ),
        JudgeablePage(
            page_capture_id="popup-ad-after",
            page_type="home",
            surface_kind="android",
            evidence_tags=frozenset({"ad_navigation_pair_complete"}),
        ),
        JudgeablePage(
            page_capture_id="later-ad-after",
            page_type="home",
            surface_kind="android",
            evidence_tags=frozenset({"ad_navigation_pair_complete"}),
        ),
    ]
    service, repo, judge, _candidates, scheduled = _service(
        pages=pages,
        candidates={
            "home": [_candidate(rule_code="alioth_1", page_type="home")],
            "startup_ad": [
                _candidate(rule_code="alioth_17", page_type="startup_ad"),
                _candidate(rule_code="alioth_18", page_type="startup_ad"),
            ]
        },
    )

    await service.submit(
        "task-1",
        rule_codes=["alioth_17"],
        access_scope=TEST_ACCESS_SCOPE,
    )
    await scheduled[0]

    assert [request.page_capture_id for request in judge.requests] == [
        "popup-ad-after"
    ]
    (finish,) = repo.finished
    assert finish["rule_count"] == 1


async def test_web_alioth_17_keeps_existing_page_candidate_behavior() -> None:
    service, repo, judge, _candidates, scheduled = _service(
        pages=[
            JudgeablePage(
                page_capture_id="web-ad",
                page_type="startup_ad",
                surface_kind="web",
            )
        ],
        candidates={
            "startup_ad": [_candidate(rule_code="alioth_17", page_type="startup_ad")]
        },
    )

    await _submit_and_run(service, scheduled)

    assert [request.page_capture_id for request in judge.requests] == ["web-ad"]
    assert repo.finished[0]["rule_count"] == 1


async def test_submit_rule_codes_matching_nothing_succeeds_empty() -> None:
    service, repo, judge, _candidates, scheduled = _service(
        pages=[JudgeablePage(page_capture_id="capture-1", page_type="home")],
        candidates={"home": [_candidate(rule_code="alioth_1", page_type="home")]},
    )

    await service.submit(
        "task-1",
        rule_codes=["not_a_rule"],
        access_scope=TEST_ACCESS_SCOPE,
    )
    await scheduled[0]

    (finish,) = repo.finished
    assert finish["status"] is RuleEvaluationRunStatus.SUCCEEDED
    assert finish["rule_count"] == 0
    assert judge.requests == []
