"""E12/E13 需求集接线与写库契约测试（EvidenceExitRequirementService）。

验收口径（任务表 E12/E13）：真实候选规则查询结果可生成对应需求集并可从库中核对；
需求集按 task_id、session_id、capture_id、revision 写库，重复写入按幂等键处理。
查询端口以 CandidateRuleSource 收敛，TaskQueryService 与 Postgres 仓储结构性满足。
"""

from datetime import UTC, datetime, timedelta

import pytest

from services.api.app.application.evidence_exit_gate import (
    EvidenceExitRequirementRevisionConflict,
    PersistedEvidenceExitRequirementSet,
)
from services.api.app.application.evidence_exit_requirements import (
    CandidateRuleSource,
    EvidenceExitRequirementResolutionError,
    EvidenceExitRequirementService,
)
from services.api.app.application.task_query import CandidateRule, TaskQueryService
from services.api.app.domain.exploration.evidence_exit_requirements import (
    EvidenceExitRequirementSet,
    EvidenceExitRequirementStatus,
    build_active_session_evidence_requirement,
    build_evidence_exit_requirement_set,
)
from services.api.app.infrastructure.db.task_repository import PostgresTaskRepository

NOW = datetime(2026, 8, 11, 13, 0, tzinfo=UTC)


def _candidate(*, rule_id: str, rule_code: str, input_contract: dict) -> CandidateRule:
    return CandidateRule(
        rule_id=rule_id,
        rule_code=rule_code,
        name="测试规则",
        category="text",
        severity="low",
        executor_type="hybrid",
        page_type=None,
        applicable_page_types=["content_list"],
        check_point="",
        judge_config={},
        sort_order=1,
        match_reason="applicable_page_types",
        input_contract=input_contract,
    )


class _RecordingSource:
    def __init__(self, candidates: list[CandidateRule]) -> None:
        self.candidates = candidates
        self.calls: list[dict] = []

    async def list_candidate_rules(self, *, task_id: str, page_type: str) -> list[CandidateRule]:
        self.calls.append({"task_id": task_id, "page_type": page_type})
        return self.candidates


async def test_service_resolves_requirement_set_from_candidate_query() -> None:
    source = _RecordingSource(
        [
            _candidate(
                rule_id="rule-1",
                rule_code="alioth_1",
                input_contract={"required_artifacts": ["screenshot"]},
            ),
            _candidate(
                rule_id="rule-2",
                rule_code="alioth_2",
                input_contract={
                    "required_artifacts": ["dom_snapshot"],
                    "required_capture_roles": ["before", "primary"],
                    "quality_gate": {"min_screenshot_count": 1, "allow_empty_visible_text": False},
                },
            ),
        ]
    )
    service = EvidenceExitRequirementService(candidate_rules=source, clock=lambda: NOW)
    result = await service.resolve_for_capture(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        page_type="content_list",
    )
    assert source.calls == [{"task_id": "task-1", "page_type": "content_list"}]
    assert result.task_id == "task-1"
    assert result.session_id == "session-1"
    assert result.capture_id == "capture-1"
    assert result.revision == 1
    assert result.created_at == NOW
    assert [item.rule_code for item in result.requirements] == ["alioth_1", "alioth_2"]
    second = result.requirements[1]
    assert second.required_artifact_types == ("dom",)
    assert second.required_capture_roles == ("before_action", "primary")
    assert second.min_screenshot_count == 1
    assert second.allow_empty_visible_text is False
    assert all(item.status is EvidenceExitRequirementStatus.PENDING for item in result.requirements)


async def test_service_passes_revision_through() -> None:
    source = _RecordingSource([])
    service = EvidenceExitRequirementService(candidate_rules=source, clock=lambda: NOW)
    result = await service.resolve_for_capture(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        page_type="content_list",
        revision=3,
    )
    assert result.revision == 3
    assert result.requirements == ()


async def test_service_propagates_resolution_errors() -> None:
    duplicated = _candidate(
        rule_id="rule-1",
        rule_code="alioth_1",
        input_contract={"required_artifacts": ["screenshot"]},
    )
    source = _RecordingSource([duplicated, duplicated])
    service = EvidenceExitRequirementService(candidate_rules=source, clock=lambda: NOW)
    with pytest.raises(EvidenceExitRequirementResolutionError):
        await service.resolve_for_capture(
            task_id="task-1",
            session_id="session-1",
            capture_id="capture-1",
            page_type="content_list",
        )


def test_task_query_layers_satisfy_candidate_rule_source() -> None:
    assert issubclass(TaskQueryService, CandidateRuleSource)
    assert issubclass(PostgresTaskRepository, CandidateRuleSource)


class _FakeRequirementRepository:
    def __init__(self) -> None:
        self.latest: EvidenceExitRequirementSet | None = None
        self.added: list[EvidenceExitRequirementSet] = []

    async def add(
        self, *, requirement_set: EvidenceExitRequirementSet
    ) -> PersistedEvidenceExitRequirementSet:
        self.added.append(requirement_set)
        self.latest = requirement_set
        return PersistedEvidenceExitRequirementSet(
            requirement_set=requirement_set, created=True
        )

    async def get_latest(
        self, *, task_id: str, session_id: str, capture_id: str
    ) -> EvidenceExitRequirementSet | None:
        return self.latest


def _publishing_service(
    source: _RecordingSource, repository: _FakeRequirementRepository
) -> EvidenceExitRequirementService:
    return EvidenceExitRequirementService(
        candidate_rules=source, repository=repository, clock=lambda: NOW
    )


async def _publish(service: EvidenceExitRequirementService) -> PersistedEvidenceExitRequirementSet:
    return await service.publish_for_capture(
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        page_type="content_list",
    )


def _rule_one(**contract_overrides) -> CandidateRule:
    contract = {"required_artifacts": ["screenshot"]}
    contract.update(contract_overrides)
    return _candidate(rule_id="rule-1", rule_code="alioth_1", input_contract=contract)


def _rule_two() -> CandidateRule:
    return _candidate(
        rule_id="rule-2",
        rule_code="alioth_2",
        input_contract={"required_artifacts": ["dom"]},
    )


async def test_publish_first_write_persists_revision_one() -> None:
    repository = _FakeRequirementRepository()
    service = _publishing_service(_RecordingSource([_rule_one()]), repository)

    persisted = await _publish(service)

    assert persisted.created is True
    assert len(repository.added) == 1
    assert repository.added[0].revision == 1
    assert [item.rule_code for item in repository.added[0].requirements] == ["alioth_1"]


async def test_publish_is_idempotent_when_nothing_changed() -> None:
    repository = _FakeRequirementRepository()
    service = _publishing_service(_RecordingSource([_rule_one()]), repository)

    first = await _publish(service)
    second = await _publish(service)

    assert len(repository.added) == 1
    assert second.created is False
    assert second.requirement_set == first.requirement_set


async def test_publish_bumps_revision_and_appends_new_rule_as_pending() -> None:
    repository = _FakeRequirementRepository()
    source = _RecordingSource([_rule_one()])
    service = _publishing_service(source, repository)

    first = await _publish(service)
    source.candidates = [_rule_one(), _rule_two()]
    second = await _publish(service)

    assert second.created is True
    assert len(repository.added) == 2
    merged = repository.added[1]
    assert merged.revision == 2
    assert [item.rule_code for item in merged.requirements] == ["alioth_1", "alioth_2"]
    assert (
        merged.requirements[0].requirement_id
        == first.requirement_set.requirements[0].requirement_id
    )
    assert merged.requirements[1].status is EvidenceExitRequirementStatus.PENDING


async def test_publish_preserves_terminal_statuses_of_existing_members() -> None:
    repository = _FakeRequirementRepository()
    satisfied = build_active_session_evidence_requirement(
        task_id="task-1",
        capture_id="capture-1",
        rule_id="rule-1",
        rule_code="alioth_1",
        required_artifact_types=("screenshot",),
        required_capture_roles=("primary",),
        status=EvidenceExitRequirementStatus.SATISFIED,
        resolution_manifest_id="manifest-1",
    )
    repository.latest = build_evidence_exit_requirement_set(
        revision=1,
        task_id="task-1",
        session_id="session-1",
        capture_id="capture-1",
        matcher_snapshot_hash="matcher-before",
        requirements=(satisfied,),
        created_at=NOW,
    )
    service = _publishing_service(
        _RecordingSource([_rule_one(), _rule_two()]), repository
    )

    persisted = await _publish(service)

    assert persisted.created is True
    merged = repository.added[0]
    assert merged.revision == 2
    kept = merged.requirements[0]
    assert kept.requirement_id == satisfied.requirement_id
    assert kept.status is EvidenceExitRequirementStatus.SATISFIED
    assert kept.resolution_manifest_id == "manifest-1"
    assert merged.requirements[1].status is EvidenceExitRequirementStatus.PENDING


async def test_publish_keeps_members_of_vanished_rules() -> None:
    repository = _FakeRequirementRepository()
    source = _RecordingSource([_rule_one(), _rule_two()])
    service = _publishing_service(source, repository)

    await _publish(service)
    source.candidates = [_rule_one()]
    second = await _publish(service)

    assert second.created is True
    merged = repository.added[1]
    assert merged.revision == 2
    assert [item.rule_code for item in merged.requirements] == ["alioth_1", "alioth_2"]


async def test_publish_freezes_member_content_when_only_gate_changes() -> None:
    repository = _FakeRequirementRepository()
    source = _RecordingSource([_rule_one()])
    service = _publishing_service(source, repository)

    await _publish(service)
    source.candidates = [
        _rule_one(quality_gate={"min_screenshot_count": 1, "allow_empty_visible_text": False})
    ]
    second = await _publish(service)

    assert second.created is True
    merged = repository.added[1]
    assert merged.revision == 2
    assert len(merged.requirements) == 1
    assert merged.requirements[0].allow_empty_visible_text is True
    assert merged.requirements[0].min_screenshot_count == 0
    assert merged.matcher_snapshot_hash != repository.added[0].matcher_snapshot_hash


async def test_publish_without_repository_raises() -> None:
    service = EvidenceExitRequirementService(
        candidate_rules=_RecordingSource([]), clock=lambda: NOW
    )
    with pytest.raises(RuntimeError):
        await _publish(service)


class _ConcurrentRevisionRepository(_FakeRequirementRepository):
    def __init__(self, latest: EvidenceExitRequirementSet) -> None:
        super().__init__()
        self.latest = latest
        self.attempted_revisions: list[int] = []

    async def add(
        self, *, requirement_set: EvidenceExitRequirementSet
    ) -> PersistedEvidenceExitRequirementSet:
        self.attempted_revisions.append(requirement_set.revision)
        if len(self.attempted_revisions) == 1:
            assert self.latest is not None
            self.latest = build_evidence_exit_requirement_set(
                revision=requirement_set.revision,
                task_id=requirement_set.task_id,
                session_id=requirement_set.session_id,
                capture_id=requirement_set.capture_id,
                matcher_snapshot_hash='concurrent-winner',
                requirements=self.latest.requirements,
                created_at=NOW + timedelta(milliseconds=1),
            )
            raise EvidenceExitRequirementRevisionConflict('revision advanced')
        return await super().add(requirement_set=requirement_set)


async def test_publish_reloads_and_recomputes_after_revision_conflict() -> None:
    initial = build_evidence_exit_requirement_set(
        revision=1,
        task_id='task-1',
        session_id='session-1',
        capture_id='capture-1',
        matcher_snapshot_hash='matcher-before',
        requirements=(
            build_active_session_evidence_requirement(
                task_id='task-1',
                capture_id='capture-1',
                rule_id='rule-1',
                rule_code='alioth_1',
                required_artifact_types=('screenshot',),
                required_capture_roles=('primary',),
                status=EvidenceExitRequirementStatus.PENDING,
            ),
        ),
        created_at=NOW,
    )
    repository = _ConcurrentRevisionRepository(initial)
    service = _publishing_service(
        _RecordingSource([_rule_one(), _rule_two()]), repository
    )

    persisted = await _publish(service)

    assert repository.attempted_revisions == [2, 3]
    assert persisted.requirement_set.revision == 3
    assert [item.rule_code for item in persisted.requirement_set.requirements] == [
        'alioth_1',
        'alioth_2',
    ]


async def test_publish_stops_after_three_revision_conflicts() -> None:
    class AlwaysConflictingRepository(_FakeRequirementRepository):
        def __init__(self) -> None:
            super().__init__()
            self.get_calls = 0
            self.add_calls = 0

        async def get_latest(
            self, *, task_id: str, session_id: str, capture_id: str
        ) -> EvidenceExitRequirementSet | None:
            self.get_calls += 1
            return await super().get_latest(
                task_id=task_id,
                session_id=session_id,
                capture_id=capture_id,
            )

        async def add(
            self, *, requirement_set: EvidenceExitRequirementSet
        ) -> PersistedEvidenceExitRequirementSet:
            del requirement_set
            self.add_calls += 1
            raise EvidenceExitRequirementRevisionConflict('revision advanced')

    source = _RecordingSource([_rule_one()])
    repository = AlwaysConflictingRepository()
    service = _publishing_service(source, repository)

    with pytest.raises(
        EvidenceExitRequirementRevisionConflict, match='revision advanced'
    ):
        await _publish(service)

    assert repository.get_calls == 3
    assert repository.add_calls == 3
    assert len(source.calls) == 3


async def test_publish_does_not_retry_untyped_repository_failure() -> None:
    class FailingRepository(_FakeRequirementRepository):
        def __init__(self) -> None:
            super().__init__()
            self.add_calls = 0

        async def add(
            self, *, requirement_set: EvidenceExitRequirementSet
        ) -> PersistedEvidenceExitRequirementSet:
            del requirement_set
            self.add_calls += 1
            raise RuntimeError('database unavailable')

    repository = FailingRepository()
    service = _publishing_service(_RecordingSource([_rule_one()]), repository)

    with pytest.raises(RuntimeError, match='database unavailable'):
        await _publish(service)

    assert repository.add_calls == 1
