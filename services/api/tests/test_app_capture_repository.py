"""App Capture 公共身份、截图明细与 Bootstrap 绑定事务测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from services.api.app.application.page_capture_persistence import PersistedEvidenceArtifact
from services.api.app.domain.interaction import SurfaceKind
from services.api.app.infrastructure.db.app_action_capture_repository import save_action_capture
from services.api.app.infrastructure.db.app_capture_repository import (
    AppCapturePersistenceError,
    PostgresAppCaptureRepository,
)


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakeAcquire:
    def __init__(self, connection: CaptureConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> CaptureConnection:
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: CaptureConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


class CaptureConnection:
    def __init__(
        self,
        *,
        reject_bootstrap_update: bool = False,
        existing_row: dict[str, Any] | None = None,
        canonical_capture_id: str | None = None,
        artifact_rows: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, tuple[Any, ...]]] = []
        self.reject_bootstrap_update = reject_bootstrap_update
        self.existing_row = existing_row
        self.canonical_capture_id = canonical_capture_id
        self.artifact_rows = artifact_rows or []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def execute(self, query: str, *args: Any) -> str:
        self.calls.append(("execute", " ".join(query.split()), args))
        return "OK"

    async def fetchval(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchval", normalized, args))
        if "INSERT INTO qc_capture" in normalized and self.canonical_capture_id:
            return self.canonical_capture_id
        if "UPDATE qc_app_bootstrap AS bootstrap" in normalized:
            return None if self.reject_bootstrap_update else args[4] + 1
        return args[0]

    async def fetchrow(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetchrow", normalized, args))
        if "INSERT INTO qc_evidence_manifest" in normalized:
            return {"manifest_id": args[0], "created": True}
        return self.existing_row

    async def fetch(self, query: str, *args: Any):
        normalized = " ".join(query.split())
        self.calls.append(("fetch", normalized, args))
        if "artifact.artifact_type = $2" in normalized:
            return self.artifact_rows
        return []


def _screenshot() -> PersistedEvidenceArtifact:
    return PersistedEvidenceArtifact(
        artifact_id="44444444-4444-4444-4444-444444444444",
        artifact_type="screenshot",
        storage_path="task/capture/screenshot.png",
        content_type="image/png",
        checksum="sha256:image",
        size_bytes=33,
        metadata={"encoding": "binary"},
    )


def _action_trace() -> PersistedEvidenceArtifact:
    return PersistedEvidenceArtifact(
        artifact_id="66666666-6666-6666-6666-666666666666",
        artifact_type="action_trace",
        storage_path="task/capture/action_trace.json",
        content_type="application/json",
        checksum="sha256:trace",
        size_bytes=20,
        metadata={"encoding": "utf-8"},
    )


async def test_save_bootstrap_capture_writes_all_facts_then_binds_checkpoint() -> None:
    connection = CaptureConnection()
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    persisted = await repository.save_bootstrap_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        capture_id="33333333-3333-3333-3333-333333333333",
        screenshot=_screenshot(),
        width=1,
        height=1,
        orientation="portrait",
        image_signature="sha256:image",
        package_name="com.example.target",
        activity=".MainActivity",
        device_snapshot={},
        expected_bootstrap_row_version=7,
        workflow_lease_owner="app-worker-1",
        workflow_lease_token="55555555-5555-5555-5555-555555555555",
    )

    queries = [call[1] for call in connection.calls]
    capture_index = next(i for i, query in enumerate(queries) if "INSERT INTO qc_capture" in query)
    artifact_index = next(
        i for i, query in enumerate(queries) if "INSERT INTO qc_evidence_artifact" in query
    )
    app_index = next(i for i, query in enumerate(queries) if "INSERT INTO qc_app_capture" in query)
    bootstrap_index = next(
        i for i, query in enumerate(queries) if "UPDATE qc_app_bootstrap" in query
    )
    manifest_index = next(
        i for i, query in enumerate(queries) if "INSERT INTO qc_evidence_manifest" in query
    )
    outbox_index = next(
        i for i, query in enumerate(queries) if "INSERT INTO qc_outbox_event" in query
    )
    assert capture_index < artifact_index < app_index < bootstrap_index < manifest_index
    assert manifest_index < outbox_index
    assert persisted.capture.capture_id == "33333333-3333-3333-3333-333333333333"
    assert persisted.bootstrap_row_version == 8
    assert "capture_role" in queries[capture_index]
    assert "'bootstrap'" in queries[capture_index]
    assert "'android'" in queries[capture_index]
    assert "device_lease.fencing_token" in queries[capture_index]
    assert "device_lease.expires_at > now()" in queries[capture_index]
    assert "page_capture_id" in queries[artifact_index]
    assert "capture_id" in queries[app_index]
    bootstrap_query = queries[bootstrap_index]
    assert "bootstrap.stage = 'bootstrap_capturing'" in bootstrap_query
    assert "bootstrap.row_version = $5" in bootstrap_query
    assert "bootstrap.workflow_lease_owner = $6" in bootstrap_query
    assert "bootstrap.workflow_lease_token = $7::uuid" in bootstrap_query
    assert "bootstrap_capture_id = $3::uuid" in bootstrap_query


async def test_save_rejects_stale_bootstrap_and_rolls_back_transaction() -> None:
    repository = PostgresAppCaptureRepository(
        pool=FakePool(CaptureConnection(reject_bootstrap_update=True)),
        page_graph_repository=None,
    )

    with pytest.raises(AppCapturePersistenceError, match="阶段、版本或 Workflow 租约"):
        await repository.save_bootstrap_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            agent_run_id="22222222-2222-2222-2222-222222222222",
            session_id="app-session-1",
            capture_id="33333333-3333-3333-3333-333333333333",
            screenshot=_screenshot(),
            width=1,
            height=1,
            orientation="portrait",
            image_signature="sha256:image",
            package_name=None,
            activity=None,
            device_snapshot={},
            expected_bootstrap_row_version=7,
            workflow_lease_owner="stale-worker",
            workflow_lease_token="55555555-5555-5555-5555-555555555555",
        )


async def test_save_action_capture_validates_parent_action_command_and_epoch() -> None:
    connection = CaptureConnection()
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )
    device_log = replace(
        _action_trace(),
        artifact_id="77777777-7777-7777-7777-777777777777",
        artifact_type="device_log",
        storage_path="task/capture/device_log.json",
    )
    screenshot = replace(
        _screenshot(),
        metadata={
            **dict(_screenshot().metadata),
            "capture_artifact_contract": {
                "contract_version": "app-capture-artifact-contract.v3",
                "role": "after_action",
                "requested_artifact_types": ["screenshot", "action_trace"],
                "produced_artifact_types": ["screenshot", "action_trace"],
                "persisted_artifact_types": ["screenshot", "action_trace"],
                "required_artifact_types": ["screenshot", "action_trace"],
                "missing_soft_optional_artifact_types": [],
                "provider_only_artifact_types": [],
            },
        },
    )

    persisted = await repository.save_action_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_id="33333333-3333-3333-3333-333333333333",
        artifacts=(screenshot, _action_trace(), device_log),
        screenshot=screenshot,
        width=1080,
        height=1920,
        orientation="portrait",
        image_signature="sha256:image",
        package_name="com.example.target",
        activity=".MainActivity",
        device_snapshot={},
        role="after_action",
        parent_capture_id="77777777-7777-7777-7777-777777777777",
        action_execution_id="action-1",
        session_command_sequence=3,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    queries = [call[1] for call in connection.calls]
    capture_query = next(query for query in queries if "INSERT INTO qc_capture" in query)
    artifact_queries = [query for query in queries if "INSERT INTO qc_evidence_artifact" in query]
    manifest_index = next(
        i for i, query in enumerate(queries) if "INSERT INTO qc_evidence_manifest" in query
    )
    app_capture_index = next(
        i for i, query in enumerate(queries) if "INSERT INTO qc_app_capture" in query
    )
    assert "session.command_sequence = $" in capture_query
    assert "session.finalization_epoch = $" in capture_query
    assert "session.lease_owner = $" in capture_query
    assert "session.lease_token = $" in capture_query
    assert "parent.id = $" in capture_query
    assert "action.action_execution_id = $" in capture_query
    assert "action.status IN (" in capture_query
    for status in ("evidence_pending", "blocked", "failed", "timed_out"):
        assert f"'{status}'" in capture_query
    assert "qc_capture.capture_status = 'captured'" in capture_query
    assert "ON CONFLICT (action_execution_id, capture_role)" in capture_query
    assert len(artifact_queries) == 3
    assert {
        args[3]
        for _kind, query, args in connection.calls
        if "INSERT INTO qc_evidence_artifact" in query
    } == {"screenshot", "action_trace", "device_log"}
    assert app_capture_index < manifest_index
    capture_args = next(
        args for _kind, query, args in connection.calls if "INSERT INTO qc_capture" in query
    )
    assert json.loads(capture_args[10])["finalization_epoch"] == 2
    assert json.loads(capture_args[10])["capture_artifact_contract"][
        "contract_version"
    ] == "app-capture-artifact-contract.v3"
    manifest_args = next(
        args
        for _kind, query, args in connection.calls
        if "INSERT INTO qc_evidence_manifest" in query
    )
    assert "capture_artifact_contract" in str(manifest_args)
    assert persisted.capture.role == "after_action"
    assert persisted.capture.parent_capture_id == "77777777-7777-7777-7777-777777777777"
    assert persisted.capture.artifact_ids == (
        _screenshot().artifact_id,
        _action_trace().artifact_id,
        device_log.artifact_id,
    )


async def test_save_observation_capture_uses_parent_and_session_fence_without_action() -> None:
    connection = CaptureConnection()
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    persisted = await repository.save_observation_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_id="33333333-3333-3333-3333-333333333333",
        artifacts=(_screenshot(),),
        screenshot=_screenshot(),
        width=1080,
        height=1920,
        orientation="portrait",
        image_signature="sha256:image",
        package_name="com.example.target",
        activity=".MainActivity",
        device_snapshot={},
        role="observation",
        parent_capture_id="77777777-7777-7777-7777-777777777777",
        observation_id="observation-1",
        surface_phase_run_id="phase-1",
        surface_phase_decision_id="decision-1",
        semantic_role="surface_reobserve",
        session_command_sequence=4,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    capture_query = next(
        query for _kind, query, _args in connection.calls if "INSERT INTO qc_capture" in query
    )
    capture_args = next(
        args for _kind, query, args in connection.calls if "INSERT INTO qc_capture" in query
    )
    assert "capture_role" in capture_query
    assert "INNER JOIN qc_capture AS parent" in capture_query
    assert "LEFT JOIN qc_action_execution AS action" in capture_query
    assert "session.command_sequence" in capture_query
    assert "device_lease.fencing_token" in capture_query
    assert "device_lease.expires_at > now()" in capture_query
    assert "ON CONFLICT (id)" in capture_query
    assert capture_args[6] is None
    assert capture_args[7] == "observation"
    assert persisted.capture.role == "observation"
    assert persisted.capture.parent_capture_id == "77777777-7777-7777-7777-777777777777"
    assert persisted.capture.action_execution_id is None


async def test_save_action_capture_uses_existing_business_capture_id() -> None:
    old_capture_id = "99999999-9999-9999-9999-999999999999"
    connection = CaptureConnection(canonical_capture_id=old_capture_id)
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    persisted = await repository.save_action_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_id="33333333-3333-3333-3333-333333333333",
        artifacts=(_screenshot(), _action_trace()),
        screenshot=_screenshot(),
        width=1080,
        height=1920,
        orientation="portrait",
        image_signature="sha256:image",
        package_name="com.example.target",
        activity=".MainActivity",
        device_snapshot={},
        role="after_action",
        parent_capture_id="77777777-7777-7777-7777-777777777777",
        action_execution_id="action-1",
        session_command_sequence=3,
        finalization_epoch=2,
        lease_owner="worker-1:app-command",
        lease_token="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    )

    artifact_query = next(
        args
        for kind, query, args in connection.calls
        if kind == "fetchval" and "INSERT INTO qc_evidence_artifact" in query
    )
    app_query = next(
        args
        for kind, query, args in connection.calls
        if kind == "fetchval" and "INSERT INTO qc_app_capture" in query
    )
    manifest_args = next(
        args
        for kind, query, args in connection.calls
        if kind == "fetchrow" and "INSERT INTO qc_evidence_manifest" in query
    )
    assert persisted.capture.capture_id == old_capture_id
    assert artifact_query[2] == old_capture_id
    assert app_query[0] == old_capture_id
    assert manifest_args[4] == old_capture_id


async def test_save_action_capture_reuses_historical_artifact_ids() -> None:
    class HistoricalConnection(CaptureConnection):
        async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
            normalized = " ".join(query.split())
            self.calls.append(("fetch", normalized, args))
            if "FROM qc_evidence_artifact" in normalized:
                return [
                    {
                        "artifact_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "artifact_type": "screenshot",
                        "task_id": "11111111-1111-1111-1111-111111111111",
                        "page_capture_id": "99999999-9999-9999-9999-999999999999",
                        "source": "app_runtime",
                        "storage_path": _screenshot().storage_path,
                        "content_type": _screenshot().content_type,
                        "checksum": _screenshot().checksum,
                        "size_bytes": _screenshot().size_bytes,
                        "metadata": '{"encoding":"binary"}',
                    },
                    {
                        "artifact_id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                        "artifact_type": "action_trace",
                        "task_id": "11111111-1111-1111-1111-111111111111",
                        "page_capture_id": "99999999-9999-9999-9999-999999999999",
                        "source": "app_runtime",
                        "storage_path": _action_trace().storage_path,
                        "content_type": _action_trace().content_type,
                        "checksum": _action_trace().checksum,
                        "size_bytes": _action_trace().size_bytes,
                        "metadata": '{"encoding":"utf-8"}',
                    },
                ]
            return []

    old_capture_id = "99999999-9999-9999-9999-999999999999"
    connection = HistoricalConnection(canonical_capture_id=old_capture_id)
    retry_screenshot = replace(
        _screenshot(),
        storage_path="task/capture/uuid5-screenshot.png",
    )
    retry_action_trace = replace(
        _action_trace(),
        storage_path="task/capture/uuid5-action-trace.json",
    )
    result = await save_action_capture(
        pool=FakePool(connection),
        task_id="11111111-1111-1111-1111-111111111111",
        agent_run_id="22222222-2222-2222-2222-222222222222",
        session_id="app-session-1",
        surface_kind=SurfaceKind.ANDROID,
        capture_id="33333333-3333-3333-3333-333333333333",
        artifacts=(retry_screenshot, retry_action_trace),
        screenshot=retry_screenshot,
        width=1080,
        height=1920,
        orientation="portrait",
        image_signature="sha256:image",
        package_name="com.example.target",
        activity=".MainActivity",
        device_snapshot={},
        role="after_action",
        parent_capture_id="77777777-7777-7777-7777-777777777777",
        action_execution_id="action-1",
        session_command_sequence=3,
        finalization_epoch=2,
        lease_owner="test-worker",
        lease_token="test-lease-token",
    )

    assert result.capture.capture_id == old_capture_id
    assert result.capture.artifact_ids == (
        "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
    )
    assert result.screenshot.artifact_id == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert result.screenshot.storage_path == "task/capture/screenshot.png"
    assert result.screenshot.created is False
    assert not any(
        kind == "fetchval" and "INSERT INTO qc_evidence_artifact" in query
        for kind, query, _args in connection.calls
    )


async def test_get_bootstrap_capture_uses_common_capture_not_page_fact() -> None:
    connection = CaptureConnection()
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    assert (
        await repository.get_bootstrap_capture(
            task_id="11111111-1111-1111-1111-111111111111",
            session_id="app-session-1",
        )
        is None
    )
    query = connection.calls[0][1]
    assert "FROM qc_app_bootstrap AS bootstrap" in query
    assert "INNER JOIN qc_capture AS capture" in query
    assert "INNER JOIN qc_app_capture AS app_capture" in query
    assert "INNER JOIN qc_evidence_artifact AS artifact" in query
    assert "qc_page_capture" not in query


async def test_get_capture_reads_common_app_capture_identity() -> None:
    connection = CaptureConnection()
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    assert await repository.get_capture(capture_id="33333333-3333-3333-3333-333333333333") is None

    query = connection.calls[0][1]
    assert "FROM qc_capture AS capture" in query
    assert "INNER JOIN qc_app_capture AS app_capture" in query
    assert "capture.parent_capture_id" in query
    assert "capture.action_execution_id" in query


async def test_get_capture_artifact_requires_same_captured_app_runtime_fact() -> None:
    connection = CaptureConnection(
        artifact_rows=[
            {
                "artifact_id": "77777777-7777-7777-7777-777777777777",
                "artifact_type": "ui_tree",
                "storage_path": "task/capture/ui_tree.xml",
                "content_type": "application/xml; charset=utf-8",
                "checksum": "sha256:tree",
                "size_bytes": 128,
                "artifact_metadata": {"encoding": "utf-8"},
            }
        ]
    )
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    artifact = await repository.get_capture_artifact(
        capture_id="33333333-3333-3333-3333-333333333333",
        artifact_type="ui_tree",
    )

    assert artifact is not None
    assert artifact.artifact_id == "77777777-7777-7777-7777-777777777777"
    assert artifact.created is False
    query = connection.calls[0][1]
    assert "artifact.page_capture_id = capture.id" in query
    assert "artifact.source = 'app_runtime'" in query
    assert "capture.capture_status = 'captured'" in query
    assert "LIMIT 2" in query


async def test_get_action_capture_reads_by_action_identity_not_capture_uuid() -> None:
    connection = CaptureConnection()
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    assert (
        await repository.get_action_capture(
            action_execution_id="action-1",
            role="before_action",
        )
        is None
    )

    query, args = connection.calls[0][1], connection.calls[0][2]
    assert "capture.action_execution_id = $1" in query
    assert "capture.capture_role = $2" in query
    assert "capture.surface_kind IN ('android', 'ios')" in query
    assert "capture.id = $1::uuid" not in query
    assert args == ("action-1", "before_action")


async def test_get_action_capture_returns_historical_random_capture_uuid() -> None:
    connection = CaptureConnection(
        existing_row={
            "capture_id": "99999999-9999-9999-9999-999999999999",
            "task_id": "11111111-1111-1111-1111-111111111111",
            "agent_run_id": "22222222-2222-2222-2222-222222222222",
            "session_id": "app-session-1",
            "surface_kind": "android",
            "capture_role": "before_action",
            "parent_capture_id": "88888888-8888-8888-8888-888888888888",
            "action_execution_id": "action-1",
            "capture_metadata": {"artifact_ids": ["44444444-4444-4444-4444-444444444444"]},
            "artifact_id": "44444444-4444-4444-4444-444444444444",
            "artifact_type": "screenshot",
            "storage_path": "historical/screenshot.png",
            "content_type": "image/png",
            "checksum": "sha256:image",
            "size_bytes": 33,
            "artifact_metadata": {"encoding": "binary"},
            "width": 1,
            "height": 1,
            "orientation": "portrait",
            "image_signature": "sha256:image",
            "package_name": "com.example.target",
            "activity": ".MainActivity",
            "bootstrap_row_version": None,
        }
    )
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    persisted = await repository.get_action_capture(
        action_execution_id="action-1",
        role="before_action",
    )

    assert persisted is not None
    assert persisted.capture.capture_id == "99999999-9999-9999-9999-999999999999"
    assert persisted.capture.action_execution_id == "action-1"


async def test_get_bootstrap_capture_parses_jsonb_and_returns_reference() -> None:
    connection = CaptureConnection(
        existing_row={
            "capture_id": "33333333-3333-3333-3333-333333333333",
            "task_id": "11111111-1111-1111-1111-111111111111",
            "agent_run_id": "22222222-2222-2222-2222-222222222222",
            "session_id": "app-session-1",
            "capture_role": "bootstrap",
            "artifact_id": "44444444-4444-4444-4444-444444444444",
            "artifact_type": "screenshot",
            "storage_path": "task/capture/screenshot.png",
            "content_type": "image/png",
            "checksum": "sha256:image",
            "size_bytes": 33,
            "artifact_metadata": '{"encoding":"binary"}',
            "width": 1,
            "height": 1,
            "orientation": "portrait",
            "image_signature": "sha256:image",
            "package_name": "com.example.target",
            "activity": ".MainActivity",
            "bootstrap_row_version": 8,
        }
    )
    repository = PostgresAppCaptureRepository(
        pool=FakePool(connection), page_graph_repository=None
    )

    persisted = await repository.get_bootstrap_capture(
        task_id="11111111-1111-1111-1111-111111111111",
        session_id="app-session-1",
    )

    assert persisted is not None
    assert persisted.capture.capture_id == "33333333-3333-3333-3333-333333333333"
    assert persisted.screenshot.metadata == {"encoding": "binary"}
    assert persisted.bootstrap_row_version == 8
