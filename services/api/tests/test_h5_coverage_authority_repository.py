"""H5 页面覆盖权威 PostgreSQL 持久化。"""

from __future__ import annotations

import json

import pytest

from services.api.app.application.h5_coverage_authority import (
    H5CoverageAuthorityCandidate,
)
from services.api.app.domain.exploration.h5_coverage import H5CoverageSnapshot
from services.api.app.domain.exploration.h5_frontier import H5TaskFrontierSnapshot
from services.api.app.domain.exploration.snapshots import CandidateAction
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.app.infrastructure.db.h5_coverage_authority_repository import (
    PostgresH5CoverageAuthorityRepository,
)

pytestmark = pytest.mark.asyncio

_TASK_ID = "22222222-2222-2222-2222-222222222222"
_RUN_ID = "33333333-3333-3333-3333-333333333333"


class Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        del args


class Connection:
    def __init__(self, *, rows: list[object], values: list[object]) -> None:
        self.rows = rows
        self.values = values
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> Transaction:
        return Transaction()

    async def fetch(self, query: str, *args: object) -> list[object]:
        self.calls.append((" ".join(query.split()), args))
        return self.rows

    async def fetchrow(self, query: str, *args: object) -> object:
        self.calls.append((" ".join(query.split()), args))
        return self.rows.pop(0)

    async def fetchval(self, query: str, *args: object) -> object:
        self.calls.append((" ".join(query.split()), args))
        return self.values.pop(0)


class Acquire:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    async def __aenter__(self) -> Connection:
        return self.connection

    async def __aexit__(self, *args: object) -> None:
        del args


class Pool:
    def __init__(self, connection: Connection) -> None:
        self.connection = connection

    def acquire(self) -> Acquire:
        return Acquire(self.connection)


def _assessment(*, status: str) -> dict[str, object]:
    return {
        "execution_status": "accepted",
        "page_type_assessment": {
            "page_type": "video_detail",
            "status": status,
        },
    }


def _snapshot_row(snapshot: H5CoverageSnapshot) -> dict[str, object]:
    payload = snapshot.to_dict()
    return {
        "coverage_snapshot_id": "coverage-1",
        "revision": payload["revision"],
        "authority_source": payload["authority_source"],
        "required_page_types": json.dumps(payload["required_page_types"]),
        "covered_page_types": json.dumps(payload["covered_page_types"]),
        "missing_page_types": json.dumps(payload["missing_page_types"]),
        "scopes": json.dumps(payload["scopes"]),
        "assessment_scope_ready": payload["assessment_scope_ready"],
        "frontier_terminal": payload["frontier_terminal"],
        "completion_ready": payload["completion_ready"],
        "schema_version": payload["schema_version"],
        "payload_hash": payload["payload_hash"],
    }


async def test_list_candidates_qualifies_latest_coverage_order_columns() -> None:
    connection = Connection(rows=[], values=[])
    repository = PostgresH5CoverageAuthorityRepository(pool=Pool(connection))

    candidates = await repository.list_candidates(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        limit=10,
    )

    assert candidates == []
    query, _ = connection.calls[0]
    assert "ORDER BY coverage.revision DESC, coverage.created_at DESC" in query
    assert "ORDER BY revision DESC, created_at DESC" not in query


async def test_promote_reads_json_snapshot_and_appends_next_revision() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    )
    snapshot = H5CoverageSnapshot.advance(
        previous=None,
        frontier=frontier,
        location="https://example.test/movie?vid=1",
        capture_id="55555555-5555-5555-5555-555555555555",
        page_type="video_detail",
        page_state="entitlement_locked",
        assessment=_assessment(status="evidence_insufficient"),
        requires_auth=False,
        overlay_present=False,
    )
    connection = Connection(
        rows=[{"finalization_epoch": 0}, _snapshot_row(snapshot)],
        values=[json.dumps(frontier.to_dict()), "coverage-2"],
    )
    repository = PostgresH5CoverageAuthorityRepository(pool=Pool(connection))
    candidate = H5CoverageAuthorityCandidate(
        task_id=_TASK_ID,
        agent_run_id=_RUN_ID,
        session_id="h5-session-1",
        page_capture_id="44444444-4444-4444-4444-444444444444",
        location="https://example.test/movie?vid=1",
        page_type="video_detail",
        page_state="entitlement_locked",
        detection_confidence=0.92,
        detection_result={},
    )

    promoted = await repository.promote(
        candidate=candidate,
        assessment=_assessment(status="covered"),
    )

    assert promoted is True
    insert_query, insert_args = connection.calls[-1]
    assert "INSERT INTO qc_h5_coverage_snapshot" in insert_query
    assert insert_args[4] == snapshot.revision + 1
    assert json.loads(str(insert_args[7])) == ["video_detail"]
    assert json.loads(str(insert_args[8])) == []
    assert insert_args[12] is True


async def test_promote_refuses_to_run_while_h5_exploration_work_is_nonterminal() -> None:
    connection = Connection(rows=[None], values=[])
    repository = PostgresH5CoverageAuthorityRepository(pool=Pool(connection))

    promoted = await repository.promote(
        candidate=H5CoverageAuthorityCandidate(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5-session-1",
            page_capture_id="44444444-4444-4444-4444-444444444444",
            location="https://example.test/movie?vid=1",
            page_type="video_detail",
            page_state="stable",
            detection_confidence=0.92,
            detection_result={},
        ),
        assessment=_assessment(status="covered"),
    )

    assert promoted is False
    query, _ = connection.calls[0]
    assert "NOT EXISTS" in query
    assert "active_work.work_type = 'exploration'" in query
    assert "'pending', 'retry_wait', 'leased', 'running'" in query


async def test_promote_does_not_complete_with_pending_frontier() -> None:
    frontier = H5TaskFrontierSnapshot.create(
        max_pages=3,
        sampling_policy_summary={"include_page_types": ["video_detail"]},
    ).merge(
        source_capture_id="capture-root",
        source_state_version="state-1",
        source_location="https://example.test/home",
        navigation_depth=0,
        candidates=(
            CandidateAction(
                candidate_id="candidate-1",
                semantic_target="我的",
                action_type=InteractionOperation.CLICK,
                risk_level=InteractionRiskLevel.LOW,
                exploration_value=80,
                navigation_depth=1,
                metadata={"expected_next_page_type": "video_detail"},
            ),
        ),
    )
    snapshot = H5CoverageSnapshot.advance(
        previous=None,
        frontier=H5TaskFrontierSnapshot.create(
            max_pages=3,
            sampling_policy_summary={"include_page_types": ["video_detail"]},
        ),
        location="https://example.test/movie?vid=1",
        capture_id="55555555-5555-5555-5555-555555555555",
        page_type="video_detail",
        page_state="entitlement_locked",
        assessment=_assessment(status="evidence_insufficient"),
        requires_auth=False,
        overlay_present=False,
    )
    connection = Connection(
        rows=[{"finalization_epoch": 0}, _snapshot_row(snapshot)],
        values=[json.dumps(frontier.to_dict()), "coverage-2"],
    )
    repository = PostgresH5CoverageAuthorityRepository(pool=Pool(connection))

    await repository.promote(
        candidate=H5CoverageAuthorityCandidate(
            task_id=_TASK_ID,
            agent_run_id=_RUN_ID,
            session_id="h5-session-1",
            page_capture_id="44444444-4444-4444-4444-444444444444",
            location="https://example.test/movie?vid=1",
            page_type="video_detail",
            page_state="entitlement_locked",
            detection_confidence=0.92,
            detection_result={},
        ),
        assessment=_assessment(status="covered"),
    )

    assert connection.calls[-1][1][12] is False
