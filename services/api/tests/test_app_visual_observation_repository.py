from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    AppVisualObservationPersistenceError,
    PostgresAppVisualObservationRepository,
)

OBSERVED_AT = datetime(2026, 8, 6, 9, 30, tzinfo=UTC)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: VisualConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> VisualConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: VisualConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class VisualConnection:
    def __init__(self, *, insert_row: dict[str, Any] | None) -> None:
        self.insert_row = insert_row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append((normalized, args))
        if self.insert_row is not None and "INSERT INTO qc_app_visual_observation" in query:
            return {**self.insert_row, "surface_facts": args[13]}
        if "SELECT manifest.manifest_payload" in query:
            return None
        return self.insert_row

    async def execute(self, query: str, *args: Any):
        self.calls.append((" ".join(query.split()), args))
        return "UPDATE 1"


def _analysis(
    *,
    summary: str = "登录页",
    page_state: str = "unknown",
    overlay_signature: str | None = None,
    surface_facts: dict[str, Any] | None = None,
) -> AppVisualAnalysis:
    return AppVisualAnalysis.build(
        capture_id="33333333-3333-3333-3333-333333333333",
        screenshot_artifact_id="44444444-4444-4444-4444-444444444444",
        image_signature="sha256:" + "a" * 64,
        width=1080,
        height=1920,
        orientation="portrait",
        visual_page_type="login",
        visual_summary=summary,
        key_elements=("登录按钮",),
        candidates=(),
        provider_id="fake-app-vision",
        model_id="deterministic-v1",
        page_state=page_state,
        overlay_signature=overlay_signature,
        surface_facts=surface_facts,
    )


@pytest.mark.asyncio
async def test_save_inserts_visual_fact_through_app_capture_identity() -> None:
    connection = VisualConnection(
        insert_row={
            "observation_id": "55555555-5555-5555-5555-555555555555",
            "observed_at": OBSERVED_AT,
            "created": True,
        }
    )
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    analysis = _analysis(
        page_state="stable",
        overlay_signature="overlay:dialog-1",
        surface_facts={"dialog": {"buttons": ["close"]}},
    )

    saved = await repository.save(analysis)

    query, args = connection.calls[0]
    assert "INSERT INTO qc_app_visual_observation" in query
    assert "INNER JOIN qc_app_capture AS app_capture" in query
    assert "INNER JOIN qc_evidence_artifact AS artifact" in query
    assert "ON CONFLICT (capture_id) DO UPDATE" in query
    assert "existing.result_hash = EXCLUDED.result_hash" in query
    assert "page_state, overlay_signature, surface_facts" in query
    assert args[1] == "33333333-3333-3333-3333-333333333333"
    assert args[11] == "stable"
    assert args[12] == "overlay:dialog-1"
    assert args[13] == '{"dialog":{"buttons":["close"]}}'
    assert saved.created is True
    assert saved.observed_at == OBSERVED_AT
    assert saved.analysis.visual_summary == "登录页"


@pytest.mark.asyncio
async def test_save_replays_same_capture_and_hash() -> None:
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(
            VisualConnection(
                insert_row={
                    "observation_id": "55555555-5555-5555-5555-555555555555",
                    "observed_at": OBSERVED_AT,
                    "created": False,
                }
            )
        ),
        page_graph_repository=None,
    )

    saved = await repository.save(_analysis())

    assert saved.created is False
    assert saved.observation_id == "55555555-5555-5555-5555-555555555555"


@pytest.mark.asyncio
@pytest.mark.parametrize("session_auth", ["guest", "authenticated"])
async def test_save_projects_auth_only_after_visual_fact_identity_is_verified(session_auth) -> None:
    connection = VisualConnection(
        insert_row={
            "observation_id": "55555555-5555-5555-5555-555555555555",
            "observed_at": OBSERVED_AT,
            "created": True,
        }
    )
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(connection), page_graph_repository=None
    )
    analysis = _analysis(surface_facts={"session_auth": session_auth})

    await repository.save(analysis)

    assert len(connection.calls) == 5
    assert "pg_advisory_xact_lock" in connection.calls[1][0]
    query, args = connection.calls[2]
    assert "UPDATE qc_capture AS capture" in query
    assert "visual.capture_id = capture.id" in query
    assert "visual.task_id = capture.task_id" in query
    assert "visual.session_id = capture.surface_session_id" in query
    assert "IN ('', 'unknown')" in query
    assert "evidence_index.status = 'sealed'" in query
    assert args == (analysis.capture_id, "55555555-5555-5555-5555-555555555555", session_auth)


@pytest.mark.asyncio
async def test_visual_conflict_does_not_publish_auth_metadata() -> None:
    connection = VisualConnection(
        insert_row={
            "observation_id": "55555555-5555-5555-5555-555555555555",
            "observed_at": OBSERVED_AT,
            "created": True,
        }
    )
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    await repository.save(_analysis(surface_facts={"logged_in": True, "login_form_visible": True}))

    assert len(connection.calls) == 3


@pytest.mark.asyncio
async def test_save_rejects_same_capture_with_different_result_hash() -> None:
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(VisualConnection(insert_row=None)),
        page_graph_repository=None,
    )

    with pytest.raises(AppVisualObservationPersistenceError, match="hash"):
        await repository.save(_analysis(summary="冲突结果"))


@pytest.mark.asyncio
async def test_get_by_capture_id_reconstructs_visual_contract() -> None:
    analysis = _analysis(
        page_state="stable",
        overlay_signature="overlay:dialog-1",
        surface_facts={"dialog": {"buttons": ["close"]}},
    )
    connection = VisualConnection(
        insert_row={
            "observation_id": "55555555-5555-5555-5555-555555555555",
            "observed_at": OBSERVED_AT,
            "contract_version": analysis.contract_version,
            "capture_id": analysis.capture_id,
            "screenshot_artifact_id": analysis.screenshot_artifact_id,
            "image_signature": analysis.image_signature,
            "width": analysis.width,
            "height": analysis.height,
            "orientation": analysis.orientation,
            "package_name": analysis.package_name,
            "activity": analysis.activity,
            "visual_page_type": analysis.visual_page_type,
            "visual_summary": analysis.visual_summary,
            "key_elements": list(analysis.key_elements),
            "candidate_targets": [item.to_dict() for item in analysis.candidates],
            "page_state": analysis.page_state,
            "overlay_signature": analysis.overlay_signature,
            "surface_facts": {"dialog": {"buttons": ["close"]}},
            "provider_id": analysis.provider_id,
            "model_id": analysis.model_id,
            "result_hash": analysis.result_hash,
        }
    )
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    loaded = await repository.get_by_capture_id(capture_id=analysis.capture_id)

    assert loaded is not None
    assert loaded.created is False
    assert loaded.observed_at == OBSERVED_AT
    assert loaded.analysis == analysis


@pytest.mark.asyncio
async def test_get_by_capture_id_preserves_legacy_v1_defaults() -> None:
    payload = _analysis().to_dict()
    payload.pop("page_state", None)
    payload.pop("overlay_signature", None)
    payload.pop("surface_facts", None)
    payload["contract_version"] = "app-visual-analysis.v1"
    payload["result_hash"] = stable_protocol_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"contract_version", "result_hash"}
        }
    )
    analysis = AppVisualAnalysis.from_dict(payload)
    connection = VisualConnection(
        insert_row={
            "observation_id": "55555555-5555-5555-5555-555555555555",
            "observed_at": OBSERVED_AT,
            "contract_version": analysis.contract_version,
            "capture_id": analysis.capture_id,
            "screenshot_artifact_id": analysis.screenshot_artifact_id,
            "image_signature": analysis.image_signature,
            "width": analysis.width,
            "height": analysis.height,
            "orientation": analysis.orientation,
            "package_name": analysis.package_name,
            "activity": analysis.activity,
            "visual_page_type": analysis.visual_page_type,
            "visual_summary": analysis.visual_summary,
            "key_elements": list(analysis.key_elements),
            "candidate_targets": [],
            "provider_id": analysis.provider_id,
            "model_id": analysis.model_id,
            "result_hash": analysis.result_hash,
        }
    )
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    loaded = await repository.get_by_capture_id(capture_id=analysis.capture_id)

    assert loaded is not None
    assert loaded.analysis == analysis
    assert loaded.analysis.page_state == "unknown"
    assert loaded.analysis.overlay_signature is None
    assert loaded.analysis.surface_facts == {}


@pytest.mark.asyncio
async def test_get_by_capture_id_rejects_nonobject_surface_facts() -> None:
    analysis = _analysis()
    connection = VisualConnection(
        insert_row={
            "observation_id": "55555555-5555-5555-5555-555555555555",
            "observed_at": OBSERVED_AT,
            "contract_version": analysis.contract_version,
            "capture_id": analysis.capture_id,
            "screenshot_artifact_id": analysis.screenshot_artifact_id,
            "image_signature": analysis.image_signature,
            "width": analysis.width,
            "height": analysis.height,
            "orientation": analysis.orientation,
            "package_name": analysis.package_name,
            "activity": analysis.activity,
            "visual_page_type": analysis.visual_page_type,
            "visual_summary": analysis.visual_summary,
            "key_elements": list(analysis.key_elements),
            "candidate_targets": [],
            "page_state": "unknown",
            "overlay_signature": None,
            "surface_facts": [],
            "provider_id": analysis.provider_id,
            "model_id": analysis.model_id,
            "result_hash": analysis.result_hash,
        }
    )
    repository = PostgresAppVisualObservationRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    with pytest.raises(AppVisualObservationPersistenceError, match="JSON 对象"):
        await repository.get_by_capture_id(capture_id=analysis.capture_id)
