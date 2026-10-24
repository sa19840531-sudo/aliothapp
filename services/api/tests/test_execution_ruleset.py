"""规则集按期拆分后：任务同时跑多个规则集时落成一个组合规则集。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.api.v2.schemas import V2TaskSubmissionRequest
from services.api.app.application.execution_ruleset import (
    RulesetMember,
    TaskRulesetSelectionError,
    composite_ruleset_spec,
    composite_snapshot_hash,
    normalize_ruleset_codes,
)
from services.api.app.application.task_submission import (
    TaskRulesetResolverMissingError,
    TaskSubmissionService,
)
from services.api.app.infrastructure.db.execution_ruleset_repository import (
    resolve_execution_ruleset_on_connection,
)


def _member(ruleset_id: str, code: str, name: str, *, snapshot: str = "h", count: int = 1):
    return RulesetMember(
        ruleset_id=ruleset_id,
        ruleset_code=code,
        version=1,
        name=name,
        snapshot_hash=snapshot,
        rule_count=count,
    )


def test_normalize_ruleset_codes_trims_dedupes_and_treats_empty_as_all() -> None:
    assert normalize_ruleset_codes(None) is None
    assert normalize_ruleset_codes([]) is None
    assert normalize_ruleset_codes([" ", ""]) is None
    assert normalize_ruleset_codes([" phase1 ", "phase2", "phase1"]) == ("phase1", "phase2")


def test_composite_spec_is_stable_for_the_same_members_and_follows_member_snapshots() -> None:
    phase1 = _member(
        "00000000-0000-0000-0000-000000000001", "alioth_first_ruleset", "1期", count=40
    )
    phase2 = _member(
        "00000000-0000-0000-0000-000000000002", "alioth_phase2_ruleset", "2期", count=21
    )
    ruisheng = _member(
        "00000000-0000-0000-0000-000000000003", "alioth_ruisheng_ruleset", "3期瑞升", count=12
    )

    spec = composite_ruleset_spec([phase1, phase2, ruisheng])
    reordered = composite_ruleset_spec([ruisheng, phase1, phase2])

    # 同样的成员复用同一个组合规则集（编码、快照与顺序无关）；名称按传入的展示顺序拼
    assert spec.ruleset_code == reordered.ruleset_code
    assert spec.ruleset_code.startswith("alioth_combo_")
    assert spec.snapshot_hash == reordered.snapshot_hash
    assert spec.name == "1期 + 2期 + 3期瑞升"
    assert spec.rule_count == 73
    assert spec.member_ruleset_ids == tuple(
        sorted([phase1.ruleset_id, phase2.ruleset_id, ruisheng.ruleset_id])
    )

    # 成员重播种子（快照变了），组合的快照跟着变，漂移校验才能生效
    changed = composite_ruleset_spec(
        [phase1, _member(phase2.ruleset_id, phase2.ruleset_code, "2期", snapshot="new"), ruisheng]
    )
    assert changed.ruleset_code == spec.ruleset_code
    assert changed.snapshot_hash != spec.snapshot_hash
    assert composite_snapshot_hash([(phase1.ruleset_id, "h")]) != composite_snapshot_hash(
        [(phase1.ruleset_id, "x")]
    )


def test_composite_spec_needs_at_least_two_members() -> None:
    with pytest.raises(TaskRulesetSelectionError):
        composite_ruleset_spec([_member("id-1", "only", "唯一")])


class FakeConnection:
    """按 SQL 片段返回预置结果，记录执行过的写语句。"""

    def __init__(self, rulesets: list[dict[str, Any]], duplicated: list[str] | None = None):
        self._rulesets = rulesets
        self._duplicated = duplicated or []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM qc_ruleset AS ruleset" in sql:
            requested = args[0]
            return [
                row
                for row in self._rulesets
                if requested is None or row["ruleset_code"] in requested
            ]
        if "HAVING count(DISTINCT rule.ruleset_id) > 1" in sql:
            return [{"rule_code": code} for code in self._duplicated]
        raise AssertionError(sql)

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "INSERT 0 1"


def _ruleset_row(ruleset_id: str, code: str, name: str, created_day: int) -> dict[str, Any]:
    return {
        "ruleset_id": ruleset_id,
        "ruleset_code": code,
        "version": 1,
        "name": name,
        "snapshot_hash": f"hash-{code}",
        "rule_count": 10,
        "created_at": datetime(2026, 9, created_day, tzinfo=UTC),
    }


_ROWS = [
    _ruleset_row("00000000-0000-0000-0000-000000000003", "alioth_ruisheng_ruleset", "3期瑞升", 11),
    _ruleset_row("00000000-0000-0000-0000-000000000001", "alioth_first_ruleset", "1期", 1),
    _ruleset_row("00000000-0000-0000-0000-000000000002", "alioth_phase2_ruleset", "2期", 11),
]


@pytest.mark.asyncio
async def test_single_ruleset_is_used_as_is_without_composite() -> None:
    connection = FakeConnection(_ROWS)

    assert await resolve_execution_ruleset_on_connection(
        connection, ["alioth_first_ruleset"]
    ) == ("alioth_first_ruleset", 1)
    assert connection.executed == []


@pytest.mark.asyncio
async def test_all_rulesets_become_one_composite_named_in_creation_order() -> None:
    connection = FakeConnection(_ROWS)

    code, version = await resolve_execution_ruleset_on_connection(connection, None)

    assert code.startswith("alioth_combo_") and version == 1
    insert_sql, insert_args = connection.executed[0]
    assert "member_ruleset_ids" in insert_sql
    assert "ON CONFLICT (ruleset_code, version)" in insert_sql
    assert insert_args[1] == "1期 + 2期 + 3期瑞升"
    assert insert_args[3] == "composite"
    # 抽样与防死循环策略沿用排在最前的成员（1 期）
    assert insert_args[7] == "00000000-0000-0000-0000-000000000001"


@pytest.mark.asyncio
async def test_unknown_ruleset_and_shared_rules_are_rejected_in_plain_language() -> None:
    with pytest.raises(TaskRulesetSelectionError, match="规则集不存在或没有开启：no_such"):
        await resolve_execution_ruleset_on_connection(
            FakeConnection(_ROWS), ["alioth_first_ruleset", "no_such"]
        )
    with pytest.raises(TaskRulesetSelectionError, match="同一条规则（alioth_6）"):
        await resolve_execution_ruleset_on_connection(
            FakeConnection(_ROWS, duplicated=["alioth_6"]), None
        )
    with pytest.raises(TaskRulesetSelectionError, match="没有开启的规则集"):
        await resolve_execution_ruleset_on_connection(FakeConnection([]), None)


class RecordingResolverRepository:
    def __init__(self) -> None:
        self.resolved: list[list[str] | None] = []

    async def resolve_execution_ruleset(self, ruleset_codes):
        self.resolved.append(ruleset_codes)
        return ("alioth_combo_x", 1)


def _service(repository: Any) -> TaskSubmissionService:
    return TaskSubmissionService(
        task_repository=repository,
        agent_run_queue=object(),  # type: ignore[arg-type]
        state_store=object(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_submission_pins_explicit_single_ruleset_and_resolves_multi_or_default() -> None:
    repository = RecordingResolverRepository()
    service = _service(repository)

    # 老调用方显式指定单个规则集：照旧锁它，不走组合
    assert await service._resolve_ruleset(
        ruleset_code="alioth_first_ruleset", ruleset_version=None, ruleset_codes=None
    ) == ("alioth_first_ruleset", 1)
    assert repository.resolved == []

    # 选了多个、或什么都没指定（= 全部开启的规则集）：交给仓储落成执行规则集
    assert await service._resolve_ruleset(
        ruleset_code=None, ruleset_version=None, ruleset_codes=["a", "b"]
    ) == ("alioth_combo_x", 1)
    assert await service._resolve_ruleset(
        ruleset_code=None, ruleset_version=None, ruleset_codes=None
    ) == ("alioth_combo_x", 1)
    assert repository.resolved == [["a", "b"], None]


@pytest.mark.asyncio
async def test_submission_without_resolver_rejects_multi_ruleset_requests() -> None:
    with pytest.raises(TaskRulesetResolverMissingError):
        await _service(object())._resolve_ruleset(
            ruleset_code=None, ruleset_version=None, ruleset_codes=["a", "b"]
        )


def test_v2_request_defaults_to_all_rulesets_and_normalizes_ruleset_codes() -> None:
    base = {
        "task_name": "拆分后的任务",
        "target": {"platform": "h5", "url": "https://example.com/h5"},
    }

    default_command = V2TaskSubmissionRequest.model_validate(base).to_command()
    assert default_command.ruleset_code is None
    assert default_command.ruleset_version is None
    assert default_command.ruleset_codes == ()

    multi = V2TaskSubmissionRequest.model_validate(
        {
            **base,
            "ruleset_codes": [
                " alioth_first_ruleset ",
                "alioth_ruisheng_ruleset",
                "alioth_first_ruleset",
            ],
        }
    ).to_command()
    assert multi.ruleset_codes == ("alioth_first_ruleset", "alioth_ruisheng_ruleset")

    pinned = V2TaskSubmissionRequest.model_validate(
        {**base, "ruleset_code": " alioth_phase2_ruleset ", "ruleset_version": 1}
    ).to_command()
    assert (pinned.ruleset_code, pinned.ruleset_version) == ("alioth_phase2_ruleset", 1)
