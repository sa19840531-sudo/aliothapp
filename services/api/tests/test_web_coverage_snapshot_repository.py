from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

import pytest


def test_current_frozen_catalog_without_task_partition_can_seal_coverage():
    from services.api.app.application.web_page_type_assessment import WebPageTypeCatalogIdentity
    from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        _task_applicability,
    )
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    policy = policy_payload()
    applicability = _task_applicability({}, frozen_policy=policy)
    identity = WebPageTypeCatalogIdentity.from_frozen_catalog_payload(policy["page_catalog"])
    assert applicability["catalog_version"] == identity.catalog_version
    assert applicability["catalog_hash"] == identity.catalog_sha256
    snapshot = WebCoverageSnapshot.create(task_id="task", agent_run_id="run", finalization_epoch=0,
        observed_page_types=(), unrecognized_page_types=(), **applicability)
    assert "collection_page" in snapshot.optional_page_types


def test_legacy_without_frozen_catalog_does_not_borrow_current_collection_partition():
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        _task_applicability,
    )

    legacy = _task_applicability({})
    assert legacy["catalog_version"] == "legacy-task-config"
    assert "collection_page" not in legacy["optional_page_types"]


@pytest.mark.parametrize("configured", [None, ["content_detail", "content_list"], ["search_page"]])
def test_required_scope_uses_frozen_run_and_rejects_later_task_drift(configured):
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        WebCoverageSnapshotPersistenceError,
        _task_applicability,
    )
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    policy = policy_payload()
    frozen = ["content_detail", "content_list"]
    policy["discovery_policy"]["required_page_types"] = frozen
    task = (
        {} if configured is None
        else {"web_exploration_target": {"required_page_types": configured}}
    )
    if configured == ["search_page"]:
        with pytest.raises(WebCoverageSnapshotPersistenceError, match="冻结"):
            _task_applicability(task, frozen_policy=policy)
    else:
        actual = _task_applicability(task, frozen_policy=policy)
        assert actual["required_page_types"] == tuple(frozen)
    assert policy["discovery_policy"]["required_page_types"] == frozen


@pytest.mark.parametrize("required", [["search_page"], ["collection_page"], ["home", "activity"]])
def test_explicit_frozen_optional_types_can_be_required_without_partition_overlap(required):
    from services.api.app.domain.exploration.web_coverage_snapshot import WebCoverageSnapshot
    from services.api.app.infrastructure.db.web_coverage_snapshot_repository import (
        _task_applicability,
    )
    from services.api.tests.test_web_run_policy_snapshot import policy_payload

    policy = policy_payload()
    policy["discovery_policy"]["required_page_types"] = required
    actual = _task_applicability({}, frozen_policy=policy)
    snapshot = WebCoverageSnapshot.create(
        task_id="task", agent_run_id="run", finalization_epoch=0,
        observed_page_types=(), unrecognized_page_types=(), **actual,
    )
    assert snapshot.required_page_types == tuple(sorted(required))
    assert not set(snapshot.required_page_types) & set(snapshot.optional_page_types)


class FakeAcquire:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    async def __aenter__(self) -> "FakeConnection":
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, rows: list[object | None]) -> None:
        self.connection = FakeConnection(rows)

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class FakeTransaction:
    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    async def __aenter__(self) -> None:
        self._connection.transaction_entered = True
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self._connection.transaction_exited = True
        return None


class FakeConnection:
    def __init__(self, rows: list[object | None]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple]] = []
        self.transaction_entered = False
        self.transaction_exited = False

    async def fetchrow(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        if not self.rows:
            raise AssertionError("fetchrow 调用次数超出测试安排")
        return self.rows.pop(0)

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self)


def _modules():
    domain = import_module("services.api.app.domain.exploration.web_coverage_snapshot")
    repository = import_module(
        "services.api.app.infrastructure.db.web_coverage_snapshot_repository"
    )
    return domain, repository


def _snapshot(**overrides):
    domain, _ = _modules()
    values = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "agent_run_id": "00000000-0000-0000-0000-000000000002",
        "finalization_epoch": 4,
        "required_page_types": ["video_list", "content_list", "content_detail"],
        "observed_page_types": ["video_list", "content_list"],
        "unrecognized_page_types": ["web_unknown"],
        "snapshot_id": "00000000-0000-0000-0000-000000000003",
        "created_at": datetime(2026, 9, 2, 8, 30, tzinfo=UTC),
    }
    values.update(overrides)
    return domain.WebCoverageSnapshot.create(**values)


def _task_row(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "target_platform": "web",
        "task_status": "running",
        "finalization_state": "session_draining",
        "finalization_epoch": 4,
        "finalization_agent_run_id": "00000000-0000-0000-0000-000000000002",
        "agent_run_id": "00000000-0000-0000-0000-000000000002",
        "agent_run_task_id": "00000000-0000-0000-0000-000000000001",
        "agent_type": "main",
        "agent_run_status": "done",
        "agent_run_error_message": None,
        "is_latest_main_run": True,
    }
    values.update(overrides)
    return values


def _snapshot_row(snapshot, **overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "id": snapshot.snapshot_id,
        "task_id": snapshot.task_id,
        "agent_run_id": snapshot.agent_run_id,
        "finalization_epoch": snapshot.finalization_epoch,
        "required_page_types": list(snapshot.required_page_types),
        "observed_page_types": list(snapshot.observed_page_types),
        "missing_page_types": list(snapshot.missing_page_types),
        "unrecognized_page_types": list(snapshot.unrecognized_page_types),
        "normalization_version": snapshot.normalization_version,
        "coverage_status": snapshot.coverage_status,
        "snapshot_hash": snapshot.snapshot_hash,
        "created_at": snapshot.created_at,
        "applicability_snapshot": {
            "required_page_types": list(snapshot.required_page_types),
            "optional_page_types": list(snapshot.optional_page_types),
            "not_applicable_page_types": list(snapshot.not_applicable_page_types),
            "observed_page_types": list(snapshot.observed_page_types),
            "missing_page_types": list(snapshot.missing_page_types),
            "unrecognized_page_types": list(snapshot.unrecognized_page_types),
            "applicability_mode": snapshot.applicability_mode,
            "catalog_version": snapshot.catalog_version,
            "catalog_hash": snapshot.catalog_hash,
            "snapshot_protocol_version": snapshot.snapshot_protocol_version,
            "lifecycle_observations": list(snapshot.observed_lifecycle_page_types),
            "unknown_page_capture_ids": list(snapshot.unknown_page_capture_ids),
            "supplemental_evidence_summary": dict(snapshot.supplemental_evidence_summary),
        },
    }
    values.update(overrides)
    return values


def test_v4_repository_payload_round_trips_extended_facts_without_new_columns() -> None:
    domain, repository_module = _modules()
    snapshot = domain.WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=2,
        required_page_types=("home",),
        optional_page_types=("activity", "search_page"),
        not_applicable_page_types=("content_detail", "content_list", "video_detail", "video_list"),
        observed_page_types=("home",),
        unrecognized_page_types=(),
        observed_lifecycle_page_types=("age_gate",),
        unknown_page_capture_ids=("Capture-Unknown",),
        supplemental_evidence_summary={"captured": 3, "failed": 1},
    )

    payload = repository_module._applicability_payload(snapshot)
    assert payload["snapshot_protocol_version"] == "web-coverage-snapshot.v4"
    assert payload["lifecycle_observations"] == ["age_gate"]
    assert payload["unknown_page_capture_ids"] == ["Capture-Unknown"]
    assert payload["supplemental_evidence_summary"] == {"captured": 3, "failed": 1}
    assert payload["observed_page_types"] == ["home"]
    assert payload["missing_page_types"] == []
    assert payload["unrecognized_page_types"] == []
    assert repository_module.parse_web_coverage_snapshot_row(_snapshot_row(snapshot)) == snapshot


@pytest.mark.parametrize(
    ("field_name", "tampered_value"),
    [
        ("required_page_types", ["content_list"]),
        ("observed_page_types", []),
        ("missing_page_types", ["home"]),
        ("unrecognized_page_types", ["unknown"]),
    ],
)
def test_v4_repository_row_rejects_applicability_fact_mismatch(
    field_name: str,
    tampered_value: list[str],
) -> None:
    domain, repository_module = _modules()
    snapshot = domain.WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=2,
        required_page_types=("home",),
        observed_page_types=("home",),
        unrecognized_page_types=(),
    )
    row = _snapshot_row(snapshot)
    row["applicability_snapshot"] = {
        **row["applicability_snapshot"],
        field_name: tampered_value,
    }

    with pytest.raises(repository_module.WebCoverageSnapshotPersistenceError):
        repository_module.parse_web_coverage_snapshot_row(row)


@pytest.mark.parametrize(
    "field_name",
    [
        "optional_page_types",
        "not_applicable_page_types",
        "lifecycle_observations",
        "unknown_page_capture_ids",
        "supplemental_evidence_summary",
    ],
)
def test_v4_repository_row_requires_extended_applicability_facts(field_name: str) -> None:
    domain, repository_module = _modules()
    snapshot = domain.WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=2,
        required_page_types=("home",),
        observed_page_types=("home",),
        unrecognized_page_types=(),
    )
    row = _snapshot_row(snapshot)
    row["applicability_snapshot"] = dict(row["applicability_snapshot"])
    del row["applicability_snapshot"][field_name]

    with pytest.raises(repository_module.WebCoverageSnapshotPersistenceError):
        repository_module.parse_web_coverage_snapshot_row(row)


def test_v4_repository_row_requires_applicability_snapshot_object() -> None:
    domain, repository_module = _modules()
    snapshot = domain.WebCoverageSnapshot.create(
        task_id="task",
        agent_run_id="run",
        finalization_epoch=2,
        required_page_types=("home",),
        observed_page_types=("home",),
        unrecognized_page_types=(),
    )

    with pytest.raises(repository_module.WebCoverageSnapshotPersistenceError):
        repository_module.parse_web_coverage_snapshot_row(
            _snapshot_row(snapshot, applicability_snapshot=None)
        )


def test_v4_policy_without_task_partition_defaults_new_types_to_optional() -> None:
    domain, repository_module = _modules()
    del domain
    policy = {
        "page_catalog": {
            "page_types": [
                {"code": page_type}
                for page_type in (
                    "home",
                    "content_list",
                    "content_detail",
                    "video_list",
                    "video_detail",
                    "activity",
                    "search_page",
                    "age_gate",
                    "unknown",
                )
            ]
        }
    }

    applicability = repository_module._task_applicability({}, frozen_policy=policy)

    assert applicability["normalization_version"] == "web-page-type-assessment.v4"
    assert applicability["snapshot_protocol_version"] == "web-coverage-snapshot.v4"
    assert applicability["required_page_types"] == (
        "content_detail",
        "content_list",
        "home",
        "video_detail",
        "video_list",
    )
    assert applicability["optional_page_types"] == ("activity", "search_page")
    assert applicability["not_applicable_page_types"] == ()


def test_reconciliation_sql_limits_observed_denominator_to_business_types() -> None:
    _, repository_module = _modules()
    compact_sql = " ".join(repository_module._RECONCILIATION_STATE_SQL.split())

    assert (
        "assessment.page_type IN ( "
        "'home', 'content_list', 'content_detail', 'video_list', "
        "'video_detail', 'activity', 'search_page', 'collection_page' )"
    ) in compact_sql


def test_runtime_applicability_promotes_discovered_and_covered_business_types() -> None:
    _, repository_module = _modules()
    applicability = {
        "required_page_types": ("home",),
        "optional_page_types": (
            "activity",
            "content_detail",
            "content_list",
            "search_page",
            "video_detail",
            "video_list",
        ),
        "not_applicable_page_types": (),
        "applicability_mode": "runtime_dynamic",
        "catalog_version": "2026-09-14.web.v9",
        "catalog_hash": "a" * 64,
        "normalization_version": "web-page-type-assessment.v4",
        "snapshot_protocol_version": "web-coverage-snapshot.v4",
    }

    finalized = repository_module._finalize_runtime_applicability(
        applicability,
        applicable_page_types=("content_detail", "age_gate", "ad_page"),
        covered_page_types=("home", "content_list", "unknown"),
    )

    assert finalized["required_page_types"] == ("content_detail", "content_list", "home")
    assert finalized["page_type_statuses"]["content_detail"] == "applicable_uncovered"
    assert finalized["page_type_statuses"]["content_list"] == "covered"
    assert "age_gate" not in finalized["required_page_types"]
    assert "ad_page" not in finalized["required_page_types"]


async def test_recovery_seals_only_completed_current_web_budget() -> None:
    pool = FakePool([{"agent_run_id": "00000000-0000-0000-0000-000000000002"}])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    recovered = await repository.seal_completed_budget(
        task_id="00000000-0000-0000-0000-000000000001",
        agent_run_id="00000000-0000-0000-0000-000000000002",
        finalization_epoch=4,
    )

    assert recovered is True
    assert pool.connection.transaction_entered is True
    assert pool.connection.transaction_exited is True
    query, args = pool.connection.calls[0]
    assert "run.status = 'done'" in query
    assert "budget.state = 'open'" in query
    assert "session.status IN ('active', 'draining')" in query
    assert "NOT EXISTS" in query
    assert "'pending', 'home_pending'" in query
    assert "SET state = 'sealed'" in query
    assert args == (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        4,
    )


async def test_load_reference_reads_historical_snapshot_without_parsing_protocol() -> None:
    pool = FakePool(
        [
            {
                "snapshot_id": "00000000-0000-0000-0000-000000000003",
                "snapshot_hash": "a" * 64,
            }
        ]
    )
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    reference = await repository.load_reference(
        task_id="00000000-0000-0000-0000-000000000001",
        agent_run_id="00000000-0000-0000-0000-000000000002",
        finalization_epoch=4,
    )

    assert reference == (
        "00000000-0000-0000-0000-000000000003",
        "a" * 64,
    )
    query, args = pool.connection.calls[0]
    assert "SELECT id::text AS snapshot_id, snapshot_hash" in query
    assert "normalization_version" not in query
    assert args == (
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
        4,
    )


async def test_web_coverage_snapshot_repository_creates_after_atomic_identity_lock() -> None:
    snapshot = _snapshot()
    pool = FakePool([_task_row(), None, _snapshot_row(snapshot)])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    persisted = await repository.create(snapshot)

    assert persisted == snapshot
    assert pool.connection.transaction_entered is True
    assert pool.connection.transaction_exited is True
    identity_query = pool.connection.calls[0][0]
    insert_query = pool.connection.calls[2][0]
    assert "FROM qc_task AS task" in identity_query
    assert "JOIN qc_agent_run AS agent_run" in identity_query
    assert "latest_main.agent_type = 'main'" in identity_query
    assert "ORDER BY latest_main.created_at DESC, latest_main.id DESC" in identity_query
    assert "FOR UPDATE OF task, agent_run" in identity_query
    assert "INSERT INTO qc_web_coverage_snapshot" in insert_query
    assert "ON CONFLICT (task_id, agent_run_id, finalization_epoch) DO NOTHING" in insert_query
    assert all(not query.startswith("UPDATE") for query, _ in pool.connection.calls)


async def test_web_coverage_snapshot_repository_replays_same_hash() -> None:
    snapshot = _snapshot()
    stored = _snapshot_row(snapshot)
    pool = FakePool([_task_row(), stored])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    persisted = await repository.create(snapshot)

    assert persisted == snapshot
    assert len(pool.connection.calls) == 2
    assert "SELECT" in pool.connection.calls[1][0]
    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


@pytest.mark.parametrize(
    ("finalization_state", "task_status"),
    [("finalizing", "running"), ("sealed", "done")],
)
async def test_web_coverage_snapshot_repository_replays_same_hash_in_late_phase(
    finalization_state: str,
    task_status: str,
) -> None:
    snapshot = _snapshot()
    stored = _snapshot_row(snapshot)
    pool = FakePool(
        [
            _task_row(
                finalization_state=finalization_state,
                task_status=task_status,
            ),
            stored,
        ]
    )
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    persisted = await repository.create(snapshot)

    assert persisted == snapshot
    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


async def test_web_coverage_snapshot_repository_rejects_different_hash_replay() -> None:
    snapshot = _snapshot()
    stored = _snapshot_row(snapshot, snapshot_hash="different-hash")
    pool = FakePool([_task_row(), stored])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    with pytest.raises(
        repository_module.WebCoverageSnapshotConflictError,
        match="不同覆盖事实",
    ):
        await repository.create(snapshot)

    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


@pytest.mark.parametrize(
    "task_row",
    [
        _task_row(finalization_state="open"),
        _task_row(agent_run_status="running"),
        _task_row(agent_type="rule_judge"),
        _task_row(finalization_agent_run_id="00000000-0000-0000-0000-000000000099"),
        _task_row(finalization_epoch=5),
        _task_row(target_platform="h5"),
    ],
)
async def test_web_coverage_snapshot_repository_rejects_nonterminal_or_stale_identity(
    task_row: dict[str, object],
) -> None:
    snapshot = _snapshot()
    pool = FakePool([task_row])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    with pytest.raises(repository_module.WebCoverageSnapshotPersistenceError):
        await repository.create(snapshot)

    assert len(pool.connection.calls) == 1


@pytest.mark.parametrize(
    ("finalization_state", "task_status"),
    [("finalizing", "running"), ("sealed", "done")],
)
async def test_web_coverage_snapshot_repository_rejects_first_insert_in_late_phase(
    finalization_state: str,
    task_status: str,
) -> None:
    snapshot = _snapshot()
    pool = FakePool(
        [
            _task_row(
                finalization_state=finalization_state,
                task_status=task_status,
            ),
            None,
            _snapshot_row(snapshot),
        ]
    )
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    with pytest.raises(
        repository_module.WebCoverageSnapshotPersistenceError,
        match="首次创建",
    ):
        await repository.create(snapshot)

    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


@pytest.mark.parametrize(
    "task_row",
    [
        _task_row(task_status="canceled"),
        _task_row(agent_run_status="canceled"),
        _task_row(is_latest_main_run=False),
    ],
)
async def test_web_coverage_snapshot_repository_rejects_canceled_or_nonlatest_identity(
    task_row: dict[str, object],
) -> None:
    snapshot = _snapshot()
    pool = FakePool([task_row, None, _snapshot_row(snapshot)])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    with pytest.raises(repository_module.WebCoverageSnapshotPersistenceError):
        await repository.create(snapshot)

    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


@pytest.mark.parametrize("task_status", ["queued", "done", "failed"])
async def test_web_coverage_snapshot_repository_rejects_wrong_first_insert_task_status(
    task_status: str,
) -> None:
    snapshot = _snapshot()
    pool = FakePool([_task_row(task_status=task_status), None, _snapshot_row(snapshot)])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    with pytest.raises(
        repository_module.WebCoverageSnapshotPersistenceError,
        match="任务状态",
    ):
        await repository.create(snapshot)

    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


@pytest.mark.parametrize("agent_run_status", ["failed", "aborted"])
async def test_web_coverage_snapshot_repository_requires_done_run_for_normal_snapshot(
    agent_run_status: str,
) -> None:
    snapshot = _snapshot()
    pool = FakePool(
        [
            _task_row(
                agent_run_status=agent_run_status,
                agent_run_error_message="浏览器启动失败",
            ),
            None,
            _snapshot_row(snapshot),
        ]
    )
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    with pytest.raises(
        repository_module.WebCoverageSnapshotPersistenceError,
        match="成功结束",
    ):
        await repository.create(snapshot)

    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


@pytest.mark.parametrize(
    "task_row",
    [
        _task_row(agent_run_status="done"),
    ],
)
async def test_web_coverage_snapshot_repository_requires_persisted_failure_for_failed_snapshot(
    task_row: dict[str, object],
) -> None:
    snapshot = _snapshot(coverage_status="failed")
    pool = FakePool([task_row, None, _snapshot_row(snapshot)])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    with pytest.raises(
        repository_module.WebCoverageSnapshotPersistenceError,
        match="基础失败事实",
    ):
        await repository.create(snapshot)

    assert all("INSERT INTO" not in query for query, _ in pool.connection.calls)


@pytest.mark.parametrize(
    ("agent_run_status", "error_message"),
    [("failed", None), ("aborted", "  ")],
)
async def test_web_coverage_snapshot_repository_accepts_terminal_failure_without_message(
    agent_run_status: str,
    error_message: str | None,
) -> None:
    snapshot = _snapshot(coverage_status="failed")
    pool = FakePool(
        [
            _task_row(
                agent_run_status=agent_run_status,
                agent_run_error_message=error_message,
            ),
            None,
            _snapshot_row(snapshot),
        ]
    )
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    persisted = await repository.create(snapshot)

    assert persisted == snapshot


@pytest.mark.parametrize("agent_run_status", ["failed", "aborted"])
async def test_web_coverage_snapshot_repository_accepts_persisted_failure_fact(
    agent_run_status: str,
) -> None:
    snapshot = _snapshot(coverage_status="failed")
    pool = FakePool(
        [
            _task_row(
                agent_run_status=agent_run_status,
                agent_run_error_message="首页不可达",
            ),
            None,
            _snapshot_row(snapshot),
        ]
    )
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    persisted = await repository.create(snapshot)

    assert persisted == snapshot


async def test_repository_reads_persistent_reconciliation_state() -> None:
    row = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "agent_run_id": "00000000-0000-0000-0000-000000000002",
        "target_platform": "web",
        "task_status": "running",
        "finalization_state": "session_draining",
        "task_row_version": 8,
        "finalization_epoch": 4,
        "finalization_agent_run_id": "00000000-0000-0000-0000-000000000002",
        "run_status": "done",
        "current_main_run": True,
        "execution_mode": "full_inspection",
        "notification_mode": "enforced",
        "budget_state": "sealed",
        "frontier_work_count": 0,
        "pending_capture_commit_count": 0,
        "pending_manifest_count": 5,
        "missing_assessment_count": 3,
        "review_assessment_count": 4,
        "open_review_count": 6,
        "missing_publication_count": 2,
        "failed_manifest_count": 1,
        "nonterminal_publication_count": 0,
        "nonterminal_work_item_count": 0,
        "active_session_request_count": 0,
        "observed_page_types": ["content_list", "video_list"],
        "unrecognized_page_types": ["raw_unknown"],
        "observed_lifecycle_page_types": ["age_gate"],
        "unknown_page_capture_ids": ["capture-unknown"],
        "supplemental_evidence_summary": {"captured": 2},
        "foundational_failure_code": None,
    }
    pool = FakePool([row])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    state = await repository.load_reconciliation_state(
        task_id=row["task_id"],
        agent_run_id=row["agent_run_id"],
        finalization_epoch=4,
    )

    assert state is not None
    assert state.normalization_version == "web-page-type-assessment.v2"
    assert state.snapshot_protocol_version == "web-coverage-snapshot.v2"
    assert state.exploration_terminal is True
    assert state.observed_page_types == ("content_list", "video_list")
    assert state.observed_lifecycle_page_types == ("age_gate",)
    assert state.unknown_page_capture_ids == ("capture-unknown",)
    assert dict(state.supplemental_evidence_summary) == {"captured": 2}
    assert state.missing_assessment_count == 3
    assert state.pending_manifest_count == 5
    assert state.review_assessment_count == 4
    assert state.open_review_count == 6
    assert state.missing_publication_count == 2
    assert state.failed_manifest_count == 1
    query = pool.connection.calls[0][0]
    assert "qc_web_exploration_budget" in query
    assert "policy.payload AS frozen_run_policy" in query
    assert "to_jsonb(existing_coverage) AS frozen_coverage_snapshot" in query
    assert "qc_v2_work_item" in query
    assert "qc_evidence_manifest" in query
    assert "qc_h5_inspection_publication" in query
    assert "qc_inspection_work_item" in query
    assert "qc_h5_page_type_assessment" in query
    assert "recognized_publication_inventory" in query
    assert "classification_inventory" in query
    assert "classification_inventory.missing_assessment_count" in query
    assert "classification_inventory.review_assessment_count" in query
    assert "'web-page-type-assessment.v4'" in query
    assert "observed_lifecycle_page_types" in query
    assert "unknown_page_capture_ids" in query
    assert "supplemental_evidence_summary" in query
    assert "entry.decision = 'runtime_observe_only'" in query
    assert "entry.discovery_manifest ? 'authorization'" in query
    assert "qc_review_item" in query
    assert "status IN ('pending', 'needs_recheck')" in query
    assert "classification_capture.capture_status = 'captured'" in query
    assert "classification_capture.capture_role = 'after_action'" in query
    assert "classification_manifest.quality_status = 'pass'" in query
    assert "LEFT JOIN qc_h5_page_type_assessment AS classification_assessment" in query
    classification_inventory_sql = query.split(") AS classification_inventory ON TRUE", maxsplit=1)[
        0
    ].rsplit("LEFT JOIN LATERAL (", maxsplit=1)[-1]
    assert "classification_assessment.detection_status = 'needs_review'" in (
        classification_inventory_sql
    )
    assert "detection_status IN ('recognized', 'unrecognized')" not in (
        classification_inventory_sql
    )
    assert "classification_assessment.assessment_record_id IS NULL" in query
    assert "qc_web_menu_ledger_entry AS classification_entry" in query
    assert "classification_entry.menu_ready_capture_id" in query
    assert "classification_entry.detail_ready_capture_id" in query
    assert "classification_entry.menu_probe_capture_id" not in (classification_inventory_sql)
    assert "classification_entry.detail_probe_capture_id" not in (classification_inventory_sql)
    assert "observed_capture.capture_status = 'captured'" in query
    assert "observed_capture.capture_role = 'after_action'" in query
    assert "qc_web_menu_ledger_entry AS observed_entry" in query
    assert "observed_entry.menu_ready_capture_id" in query
    assert "observed_entry.detail_ready_capture_id" in query
    assert "qc_web_menu_ledger_entry AS unrecognized_entry" in query
    assert "unrecognized_entry.menu_ready_capture_id" in query
    assert "unrecognized_entry.detail_ready_capture_id" in query
    assert "observed_page.inspection_lifecycle_status" in query
    assert "observed_capture.metadata->>'inspection_lifecycle_status'" in query
    assert "NOT IN ('superseded', 'abandoned')" in query
    assert "latest_manifest.manifest_id IS NULL" in query
    assert "latest_manifest.quality_status = 'pending'" in query
    assert "latest_manifest.quality_status = 'fail'" in query
    assert (
        "count(*) FILTER ( WHERE capture.capture_status = 'pending' ) AS pending_capture_count"
    ) in query


@pytest.mark.parametrize(
    ("run_status", "failure_code", "expected_terminal"),
    (
        ("failed", "WEB_HOME_UNREACHABLE", True),
        ("aborted", "WEB_MANIFEST_PERSISTENCE_FAILED", True),
        ("cancelled", None, False),
    ),
)
async def test_foundational_terminal_run_without_budget_can_reach_failed_snapshot(
    run_status: str,
    failure_code: str | None,
    expected_terminal: bool,
) -> None:
    row = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "agent_run_id": "00000000-0000-0000-0000-000000000002",
        "target_platform": "web",
        "task_status": "running",
        "finalization_state": "session_draining",
        "task_row_version": 8,
        "finalization_epoch": 4,
        "finalization_agent_run_id": "00000000-0000-0000-0000-000000000002",
        "run_status": run_status,
        "current_main_run": True,
        "execution_mode": "full_inspection",
        "notification_mode": "enforced",
        "budget_state": "missing",
        "frontier_work_count": 0,
        "pending_capture_commit_count": 0,
        "nonterminal_publication_count": 0,
        "nonterminal_work_item_count": 0,
        "active_session_request_count": 0,
        "observed_page_types": [],
        "unrecognized_page_types": [],
        "foundational_failure_code": failure_code,
    }
    pool = FakePool([row])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    state = await repository.load_reconciliation_state(
        task_id=row["task_id"],
        agent_run_id=row["agent_run_id"],
        finalization_epoch=4,
    )

    assert state is not None
    assert state.exploration_terminal is expected_terminal


@pytest.mark.parametrize("run_status", ["failed", "aborted"])
async def test_failed_terminal_run_uses_auditable_fallback_without_error_message(
    run_status: str,
) -> None:
    row = {
        "task_id": "00000000-0000-0000-0000-000000000001",
        "agent_run_id": "00000000-0000-0000-0000-000000000002",
        "target_platform": "web",
        "task_status": "running",
        "finalization_state": "session_draining",
        "task_row_version": 8,
        "finalization_epoch": 4,
        "finalization_agent_run_id": "00000000-0000-0000-0000-000000000002",
        "run_status": run_status,
        "current_main_run": True,
        "execution_mode": "full_inspection",
        "notification_mode": "enforced",
        "budget_state": "missing",
        "frontier_work_count": 0,
        "pending_capture_commit_count": 0,
        "nonterminal_publication_count": 0,
        "nonterminal_work_item_count": 0,
        "active_session_request_count": 0,
        "observed_page_types": [],
        "unrecognized_page_types": [],
        "foundational_failure_code": "WEB_FOUNDATIONAL_FAILURE",
    }
    pool = FakePool([row])
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    state = await repository.load_reconciliation_state(
        task_id=row["task_id"],
        agent_run_id=row["agent_run_id"],
        finalization_epoch=4,
    )

    assert state is not None
    assert state.exploration_terminal is True
    assert state.foundational_failure_code == "WEB_FOUNDATIONAL_FAILURE"
    query = pool.connection.calls[0][0]
    assert "run.status IN ('failed', 'aborted')" in query
    assert "AND NULLIF(BTRIM(run.error_message), '') IS NOT NULL" not in query
    assert "run.output_context->>'error_code'" in query
    assert "'WEB_FOUNDATIONAL_FAILURE'" in query


class GapRuleConnection(FakeConnection):
    async def fetch(self, query: str, *args):
        self.calls.append((" ".join(query.split()), args))
        return [
            {
                "rule_id": "rule-web-1",
                "rule_code": "alioth_6",
                "missing_page_type": "content_detail",
            }
        ]


class GapRulePool:
    def __init__(self) -> None:
        self.connection = GapRuleConnection([])

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


async def test_gap_rule_query_is_web_only_page_level_and_exact() -> None:
    snapshot = _snapshot()
    pool = GapRulePool()
    _, repository_module = _modules()
    repository = repository_module.PostgresWebCoverageSnapshotRepository(pool=pool)

    rules = await repository.list_gap_rule_candidates(snapshot=snapshot)

    assert [(rule.rule_code, rule.missing_page_type) for rule in rules] == [
        ("alioth_6", "content_detail")
    ]
    query, args = pool.connection.calls[0]
    assert "rule.ruleset_id = ANY(qc_ruleset_scope(task.ruleset_id))" in query
    assert "rule.status = 'enabled'" in query
    assert "rule.channel IS NULL OR rule.channel = task.channel" in query
    assert "rule.input_contract->'target_clients' ? 'WEB'" in query
    assert "rule.applicable_page_types ? missing.page_type" in query
    assert "evidence_input_scope" in query
    assert "= 'page'" in query
    assert args[-1] == list(snapshot.missing_page_types)


def test_web_coverage_snapshot_migration_defines_immutable_table_and_source_key() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260902_0084_web_formal_inspection_closure.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260902_0084"' in migration
    assert 'down_revision = "20260901_0083"' in migration
    assert "CREATE TABLE qc_web_coverage_snapshot" in migration
    assert "UNIQUE (task_id, agent_run_id, finalization_epoch)" in migration
    assert "coverage_status IN (" in migration
    assert "'complete', 'completed_with_gaps', 'failed'" in migration
    assert "BEFORE UPDATE OR DELETE ON qc_web_coverage_snapshot" in migration
    assert "ADD COLUMN source_idempotency_key" in migration
    assert "CREATE UNIQUE INDEX uq_qc_rule_execution_source_idempotency" in migration
    assert "ON qc_rule_execution (task_id, rule_id, source_idempotency_key)" in migration
    assert "WHERE source_idempotency_key IS NOT NULL" in migration
    assert "CREATE FUNCTION qc_guard_rule_execution_source_identity()" in migration
    assert "NEW.source_idempotency_key IS DISTINCT FROM OLD.source_idempotency_key" in migration
    assert "CREATE TRIGGER trg_qc_rule_execution_source_identity_guard" in migration


def test_task4_0084_migration_does_not_rewrite_completion_gate_formula() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260902_0084_web_formal_inspection_closure.py"
    ).read_text(encoding="utf-8")

    assert "ck_qc_completion_gate_formula" not in migration
    assert "web_coverage_snapshot_incomplete" not in migration


def test_0085_migration_upgrades_already_applied_0084_gate_formula() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260902_0085_web_completion_gate_formula.py"
    )
    migration = migration_path.read_text(encoding="utf-8") if migration_path.exists() else ""

    assert 'revision = "20260902_0085"' in migration
    assert 'down_revision = "20260902_0084"' in migration
    upgrade = migration.split("def upgrade() -> None:", maxsplit=1)[1].split(
        "def downgrade() -> None:", maxsplit=1
    )[0]
    assert "DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula" in upgrade
    assert "ADD CONSTRAINT ck_qc_completion_gate_formula CHECK" in upgrade
    assert "AND NOT (block_reasons ? 'web_coverage_snapshot_incomplete')" in upgrade


def test_0092_migration_adds_nullable_immutable_applicability_payload() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260906_0092_web_report_applicability_recovery.py"
    )
    migration = migration_path.read_text(encoding="utf-8") if migration_path.exists() else ""

    assert 'revision = "20260906_0092"' in migration
    assert 'down_revision = "20260905_0091"' in migration
    assert "ADD COLUMN applicability_snapshot jsonb" in migration
    assert "applicability_snapshot IS NULL" in migration
    assert "jsonb_typeof(applicability_snapshot) = 'object'" in migration
    assert "UPDATE qc_web_coverage_snapshot" not in migration


def test_0085_populated_downgrade_preserves_historical_gate_snapshots() -> None:
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260902_0085_web_completion_gate_formula.py"
    )
    migration = migration_path.read_text(encoding="utf-8") if migration_path.exists() else ""
    assert 'revision = "20260902_0085"' in migration
    assert 'down_revision = "20260902_0084"' in migration
    downgrade = migration.split("def downgrade() -> None:", maxsplit=1)[1]

    assert "DROP CONSTRAINT IF EXISTS ck_qc_completion_gate_formula" in downgrade
    assert "ADD CONSTRAINT ck_qc_completion_gate_formula CHECK" in downgrade
    assert "NOT VALID" in downgrade
    assert "candidate_inventory_satisfied" in downgrade
    assert "app_coverage_satisfied" in downgrade
    assert "h5_coverage_satisfied" in downgrade
    assert "web_coverage_snapshot_incomplete" not in downgrade
    assert "DELETE FROM qc_completion_gate_snapshot" not in downgrade
    assert "UPDATE qc_completion_gate_snapshot" not in downgrade


def test_web_coverage_snapshot_migration_downgrade_only_removes_0084_objects() -> None:
    migration = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260902_0084_web_formal_inspection_closure.py"
    ).read_text(encoding="utf-8")
    downgrade = migration.split("def downgrade() -> None:", maxsplit=1)[1]

    assert "DROP TABLE qc_web_coverage_snapshot" in downgrade
    assert "DROP COLUMN IF EXISTS source_idempotency_key" in downgrade
    trigger_drop = downgrade.index(
        "DROP TRIGGER IF EXISTS trg_qc_rule_execution_source_identity_guard"
    )
    function_drop = downgrade.index(
        "DROP FUNCTION IF EXISTS qc_guard_rule_execution_source_identity()"
    )
    column_drop = downgrade.index("DROP COLUMN IF EXISTS source_idempotency_key")
    assert trigger_drop < function_drop < column_drop
    assert "qc_h5_coverage_snapshot" not in downgrade
    assert "qc_task" not in downgrade
