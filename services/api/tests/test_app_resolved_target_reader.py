"""App 视觉候选解析读取测试。"""

from __future__ import annotations

from typing import Any

import pytest

from services.api.app.domain.exploration import (
    AppVisualCandidate,
    NormalizedBounds,
)
from services.api.app.domain.interaction import InteractionOperation, InteractionRiskLevel
from services.api.app.infrastructure.db.app_resolved_target_reader import (
    PostgresAppResolvedTargetReader,
)

TASK_ID = "00000000-0000-0000-0000-000000000001"
CAPTURE_ID = "00000000-0000-0000-0000-000000000002"
OTHER_TASK_ID = "00000000-0000-0000-0000-000000000011"
OTHER_CAPTURE_ID = "00000000-0000-0000-0000-000000000012"
SESSION_ID = "app:session-1"
PACKAGE_NAME = "com.example.app"
IMAGE_SIGNATURE = "sha256:" + "a" * 64
ARTIFACT_ID = "00000000-0000-0000-0000-000000000003"


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.acquire_count = 0

    def acquire(self) -> FakeAcquire:
        self.acquire_count += 1
        return FakeAcquire(self.connection)


class FakeConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((" ".join(query.split()), args))
        return self.row


def _candidate() -> AppVisualCandidate:
    return AppVisualCandidate.build(
        source_capture_id=CAPTURE_ID,
        source_image_signature=IMAGE_SIGNATURE,
        source_width=1080,
        source_height=1920,
        source_orientation="portrait",
        semantic_name="我已满18岁",
        action_type=InteractionOperation.CLICK,
        normalized_bounds=NormalizedBounds(0.2, 0.75, 0.8, 0.85),
        confidence=0.99,
        risk_level=InteractionRiskLevel.LOW,
        purpose="确认年龄后继续探索",
    )


def _row(*, candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "capture_task_id": TASK_ID,
        "capture_session_id": SESSION_ID,
        "capture_surface_kind": "android",
        "capture_status": "captured",
        "capture_id": CAPTURE_ID,
        "visual_task_id": TASK_ID,
        "visual_session_id": SESSION_ID,
        "visual_capture_id": CAPTURE_ID,
        "visual_screenshot_artifact_id": ARTIFACT_ID,
        "app_capture_id": CAPTURE_ID,
        "app_screenshot_artifact_id": ARTIFACT_ID,
        "image_signature": IMAGE_SIGNATURE,
        "width": 1080,
        "height": 1920,
        "orientation": "portrait",
        "package_name": PACKAGE_NAME,
        "candidate_targets": candidates if candidates is not None else [_candidate().to_dict()],
    }


async def _read(
    row: dict[str, Any] | None,
    *,
    task_id: str = TASK_ID,
    session_id: str = SESSION_ID,
    capture_id: str = CAPTURE_ID,
    candidate_id: str | None = None,
    package_name: str = PACKAGE_NAME,
) -> tuple[AppVisualCandidate | None, FakeConnection, FakePool]:
    connection = FakeConnection(row)
    pool = FakePool(connection)
    result = await PostgresAppResolvedTargetReader(pool=pool).read_resolved_candidate(
        task_id=task_id,
        session_id=session_id,
        capture_id=capture_id,
        candidate_id=candidate_id or _candidate().candidate_id,
        package_name=package_name,
    )
    return result, connection, pool


@pytest.mark.asyncio
async def test_reader_returns_only_candidate_bound_to_same_capture_identity() -> None:
    candidate = _candidate()

    result, connection, _ = await _read(_row())

    assert result == candidate
    query, args = connection.calls[0]
    assert "FROM qc_capture AS capture" in query
    assert "INNER JOIN qc_app_capture AS app_capture" in query
    assert "INNER JOIN qc_app_visual_observation AS visual" in query
    assert "capture.surface_kind = 'android'" in query
    assert "visual.screenshot_artifact_id = app_capture.screenshot_artifact_id" in query
    assert "$1::uuid" in query
    assert "$3::uuid" in query
    assert args == (TASK_ID, SESSION_ID, CAPTURE_ID, candidate.candidate_id, PACKAGE_NAME)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_id", OTHER_TASK_ID),
        ("session_id", "app:session-2"),
        ("capture_id", OTHER_CAPTURE_ID),
        ("candidate_id", "candidate:missing"),
        ("package_name", "com.other.app"),
    ],
)
async def test_reader_rejects_mismatched_lookup_identity(field: str, value: str) -> None:
    result, _, _ = await _read(_row(), **{field: value})

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update(source_capture_id=OTHER_CAPTURE_ID),
        lambda payload: payload.update(source_width=720),
        lambda payload: payload.update(source_height=1280),
        lambda payload: payload.update(source_orientation="landscape"),
    ],
)
async def test_reader_rejects_candidate_with_mismatched_source_facts(mutation) -> None:
    payload = _candidate().to_dict()
    mutation(payload)

    result, _, _ = await _read(_row(candidates=[payload]))

    assert result is None


@pytest.mark.asyncio
async def test_reader_rejects_duplicate_candidate_id() -> None:
    payload = _candidate().to_dict()

    result, _, _ = await _read(_row(candidates=[payload, payload.copy()]))

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_targets", ["not-json", {"candidate_id": "wrong"}, None])
async def test_reader_fails_closed_for_invalid_candidate_json(candidate_targets: Any) -> None:
    row = _row()
    row["candidate_targets"] = candidate_targets

    result, _, _ = await _read(row)

    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [("task_id", "not-a-uuid"), ("capture_id", "not-a-uuid")],
)
async def test_reader_does_not_query_for_invalid_uuid(field: str, value: str) -> None:
    connection = FakeConnection(_row())
    pool = FakePool(connection)
    arguments = {
        "task_id": TASK_ID,
        "session_id": SESSION_ID,
        "capture_id": CAPTURE_ID,
        "candidate_id": _candidate().candidate_id,
        "package_name": PACKAGE_NAME,
    }
    arguments[field] = value

    result = await PostgresAppResolvedTargetReader(pool=pool).read_resolved_candidate(
        **arguments
    )

    assert result is None
    assert pool.acquire_count == 0
    assert connection.calls == []


@pytest.mark.asyncio
async def test_reader_rejects_visual_and_app_artifact_drift() -> None:
    row = _row()
    row["app_screenshot_artifact_id"] = "00000000-0000-0000-0000-000000000004"

    result, _, _ = await _read(row)

    assert result is None
