"""测试 Observation Packet PostgreSQL 仓储的幂等写入语义。"""

import inspect
import json
from typing import Any

import pytest

from services.api.app.application.observation_packet import (
    ObservationPacketValidationError,
    _compact_ui_snapshot,
    build_observation_input_fingerprint,
)
from services.api.app.infrastructure.db.observation_packet_repository import (
    PostgresObservationPacketRepository,
    _app_ad_screenshot_artifact_ids,
)


class FakeConnection:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        self.calls.append((query, args))
        return self.row


class FakeAcquire:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self.connection

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.connection)


async def _save_packet(connection: FakeConnection) -> str:
    repository = PostgresObservationPacketRepository(
        pool=FakePool(connection),
        artifact_store=object(),  # type: ignore[arg-type]
    )
    return await repository.save_packet(
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
        rule_id="33333333-3333-3333-3333-333333333333",
        evidence_artifact_ids=("44444444-4444-4444-4444-444444444444",),
        packet_payload={"schema_version": "observation-packet.v1"},
        quality_gate_status="pass",
        quality_gate_reason=None,
        agent_run_id="55555555-5555-5555-5555-555555555555",
    )


async def test_save_packet_reuses_existing_run_page_rule_packet() -> None:
    connection = FakeConnection(_matching_packet_row("packet-existing"))

    packet_id = await _save_packet(connection)

    assert packet_id == "packet-existing"
    assert len(connection.calls) == 1
    assert "DO UPDATE SET updated_at = packet.updated_at" in connection.calls[0][0]


async def test_save_packet_inserts_with_partial_unique_index_conflict_guard() -> None:
    connection = FakeConnection(_matching_packet_row("packet-new"))

    packet_id = await _save_packet(connection)

    assert packet_id == "packet-new"
    assert len(connection.calls) == 1
    insert_sql = connection.calls[0][0]
    assert "ON CONFLICT" in insert_sql
    assert "created_by_agent_run_id IS NOT NULL" in insert_sql
    assert "input_fingerprint IS NOT NULL" in insert_sql
    assert "packet.packet_payload::text" in insert_sql
    assert connection.calls[0][1][8] == build_observation_input_fingerprint(
        evidence_artifact_ids=("44444444-4444-4444-4444-444444444444",),
    )


async def test_save_packet_rejects_frozen_packet_content_drift() -> None:
    row = _matching_packet_row("packet-existing")
    row["packet_payload"] = json.dumps({"schema_version": "drifted"})
    connection = FakeConnection(row)

    with pytest.raises(ObservationPacketValidationError, match="已冻结"):
        await _save_packet(connection)


async def test_save_packet_rejects_native_control_facts_added_to_frozen_packet() -> None:
    snapshot = _compact_ui_snapshot(
        {
            "source": "android_ui_hierarchy",
            "surface_kind": "android",
            "elements": [{"aria_label": "跳过", "clickable": True, "enabled": True}],
        }
    )
    current = {
        "schema_version": "observation-packet.v1",
        "page": {"surface_kind": "android"},
        "facts": {"ui_snapshot": snapshot},
    }
    frozen = json.loads(json.dumps(current))
    frozen_element = frozen["facts"]["ui_snapshot"]["elements"][0]
    frozen_element.pop("clickable")
    frozen_element.pop("enabled")
    row = _matching_packet_row("packet-frozen-native")
    row["packet_payload"] = json.dumps(frozen)
    connection = FakeConnection(row)
    repository = PostgresObservationPacketRepository(
        pool=FakePool(connection),
        artifact_store=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(ObservationPacketValidationError, match="已冻结.*观察包内容"):
        await repository.save_packet(
            task_id="11111111-1111-1111-1111-111111111111",
            page_capture_id="22222222-2222-2222-2222-222222222222",
            rule_id="33333333-3333-3333-3333-333333333333",
            evidence_artifact_ids=("44444444-4444-4444-4444-444444444444",),
            packet_payload=current,
            quality_gate_status="pass",
            quality_gate_reason=None,
            agent_run_id="55555555-5555-5555-5555-555555555555",
        )

    assert connection.calls[0][1][8] == build_observation_input_fingerprint(
        evidence_artifact_ids=("44444444-4444-4444-4444-444444444444",),
    )
    assert "DO UPDATE SET updated_at = packet.updated_at" in connection.calls[0][0]
    assert json.loads(row["packet_payload"]) == frozen
    assert "clickable" not in frozen_element and "enabled" not in frozen_element


async def test_save_packet_uses_h5_semantic_revision_in_frozen_identity() -> None:
    packet_payload = {
        "schema_version": "observation-packet.v1",
        "identity": {"target_platform": "h5"},
        "page": {"page_type": "home", "page_state": "overlay"},
        "facts": {
            "presentation": {
                "topmost_layer_kind": "overlay",
                "base_page_visibility": "fully_occluded",
            }
        },
    }
    connection = FakeConnection(
        {
            "observation_packet_id": "packet-h5",
            "evidence_artifact_ids": json.dumps(
                ["44444444-4444-4444-4444-444444444444"]
            ),
            "packet_payload": json.dumps(packet_payload),
            "quality_gate_status": "pass",
            "quality_gate_reason": None,
        }
    )
    repository = PostgresObservationPacketRepository(
        pool=FakePool(connection),
        artifact_store=object(),  # type: ignore[arg-type]
    )

    packet_id = await repository.save_packet(
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
        rule_id="33333333-3333-3333-3333-333333333333",
        evidence_artifact_ids=("44444444-4444-4444-4444-444444444444",),
        packet_payload=packet_payload,
        quality_gate_status="pass",
        quality_gate_reason=None,
        agent_run_id="55555555-5555-5555-5555-555555555555",
    )

    assert packet_id == "packet-h5"
    assert connection.calls[0][1][8] == build_observation_input_fingerprint(
        evidence_artifact_ids=("44444444-4444-4444-4444-444444444444",),
        semantic_revision={
            "page": {"page_type": "home", "page_state": "overlay"},
            "presentation": {
                "topmost_layer_kind": "overlay",
                "base_page_visibility": "fully_occluded",
            },
        },
    )


async def test_page_input_attempt_is_part_of_packet_fingerprint() -> None:
    fingerprints = []
    for attempt in ("attempt-1", "attempt-2", "attempt-1"):
        payload = {"schema_version": "observation-packet.v1", "identity": {
            "target_platform": "h5", "original_page_input": {
                "input_set_id": "input", "input_set_hash": "hash", "attempt_id": attempt,
            }}, "page": {}, "facts": {}}
        connection = FakeConnection({
            "observation_packet_id": "packet", "packet_payload": json.dumps(payload),
            "evidence_artifact_ids": '["artifact"]', "quality_gate_status": "pass",
            "quality_gate_reason": None,
        })
        await PostgresObservationPacketRepository(
            pool=FakePool(connection), artifact_store=object(),
        ).save_packet(
            task_id="task", page_capture_id="capture", rule_id="rule",
            evidence_artifact_ids=("artifact",), packet_payload=payload, quality_gate_status="pass",
            quality_gate_reason=None, agent_run_id="consumer",
        )
        fingerprints.append(connection.calls[0][1][8])
    assert fingerprints[0] != fingerprints[1]
    assert fingerprints[0] == fingerprints[2]


def _matching_packet_row(packet_id: str) -> dict[str, Any]:
    return {
        "observation_packet_id": packet_id,
        "evidence_artifact_ids": json.dumps(
            ["44444444-4444-4444-4444-444444444444"]
        ),
        "packet_payload": json.dumps({"schema_version": "observation-packet.v1"}),
        "quality_gate_status": "pass",
        "quality_gate_reason": None,
    }


def test_observation_input_fingerprint_is_stable_and_tracks_evidence_binding() -> None:
    primary_only = build_observation_input_fingerprint(
        evidence_artifact_ids=["artifact-2", "artifact-1"],
        capture_roles={"primary": "capture-1"},
    )
    same_binding = build_observation_input_fingerprint(
        evidence_artifact_ids=["artifact-1", "artifact-2"],
        capture_roles={"primary": "capture-1"},
    )
    paired_capture = build_observation_input_fingerprint(
        evidence_artifact_ids=["artifact-1", "artifact-2"],
        capture_roles={"before": "capture-1", "primary": "capture-2"},
    )

    assert primary_only == same_binding
    assert len(primary_only) == 64
    assert paired_capture != primary_only


def test_observation_input_fingerprint_tracks_h5_semantic_revision() -> None:
    stable = build_observation_input_fingerprint(
        evidence_artifact_ids=["artifact-1"],
        semantic_revision={
            "page": {"page_type": "home", "page_state": "stable"},
            "presentation": {"base_page_visibility": "visible"},
        },
    )
    occluded = build_observation_input_fingerprint(
        evidence_artifact_ids=["artifact-1"],
        semantic_revision={
            "page": {"page_type": "home", "page_state": "overlay"},
            "presentation": {"base_page_visibility": "fully_occluded"},
        },
    )

    assert stable != occluded


def test_load_source_query_includes_active_evidence_artifacts() -> None:
    source = inspect.getsource(PostgresObservationPacketRepository.load_source)

    assert "'action_trace'" in source
    assert "'temporal_trace'" in source
    assert "'page_coverage'" in source
    assert "session_command_sequence" in source
    assert "<= $3" in source
    assert "source IN ('baseline', 'app_runtime')" in source
    assert "qc_app_capture" in source
    assert "app_package_name" in source
    assert "app_device_snapshot" in source
    assert "id = ANY($3::uuid[])" in source


def test_app_ad_screenshot_artifacts_only_accept_complete_external_browser_pair() -> None:
    source_artifact_id = "11111111-1111-1111-1111-111111111111"
    landing_artifact_id = "22222222-2222-2222-2222-222222222222"
    action_log = [
        {
            "ad_navigation_evidence": {
                "status": "observed",
                "pair_complete": True,
                "navigation_mode": "external_browser",
                "source": {"artifact_id": source_artifact_id},
                "landing": {"artifact_id": landing_artifact_id},
            }
        },
        {
            "ad_navigation_evidence": {
                "status": "observed",
                "pair_complete": False,
                "navigation_mode": "external_browser",
                "source": {"artifact_id": "33333333-3333-3333-3333-333333333333"},
            }
        },
        {
            "ad_navigation_evidence": {
                "status": "observed",
                "pair_complete": True,
                "navigation_mode": "in_app",
                "source": {"artifact_id": "not-a-uuid"},
            }
        },
    ]

    artifact_ids = _app_ad_screenshot_artifact_ids(action_log)

    assert artifact_ids == (source_artifact_id, landing_artifact_id)


class CaptureSourceConnection:
    """返回一条 App Capture 上下文，验证子表事实会进入 ObservationSource。"""

    def __init__(self, *, url: str = "app://com.example/home") -> None:
        self.url = url

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        del args
        normalized = " ".join(query.split())
        if "FROM qc_app_visual_observation" in normalized:
            return None
        return {
            "page_capture_id": "22222222-2222-2222-2222-222222222222",
            "task_id": "11111111-1111-1111-1111-111111111111",
            "agent_run_id": "55555555-5555-5555-5555-555555555555",
            "surface_kind": "android",
            "capture_role": "observation",
            "parent_capture_id": "66666666-6666-6666-6666-666666666666",
            "captured_at": "2026-08-25 10:00:01+08",
            "page_seq": 1,
            "url": self.url,
            "page_type": "home",
            "page_state": "ready",
            "action_log": [],
            "metadata": {"surface_phase_run_id": "surface-phase-1"},
            "app_package_name": "com.example",
            "app_activity": ".MainActivity",
            "app_width": 1080,
            "app_height": 1920,
            "app_device_snapshot": {
                "safe_area": {
                    "top": 24,
                    "right": 0,
                    "bottom": 48,
                    "left": 0,
                    "width": 1080,
                    "height": 1920,
                    "source": "device_snapshot",
                }
            },
        }

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        del query, args
        return []


async def test_load_source_projects_app_capture_facts() -> None:
    connection = CaptureSourceConnection()
    repository = PostgresObservationPacketRepository(
        pool=FakePool(connection),  # type: ignore[arg-type]
        artifact_store=object(),  # type: ignore[arg-type]
    )

    source = await repository.load_capture_context(
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
    )

    assert source is not None
    assert source.app_capture_facts == {
        "package_name": "com.example",
        "activity": ".MainActivity",
        "width": 1080,
        "height": 1920,
        "device_snapshot": {
            "safe_area": {
                "top": 24,
                "right": 0,
                "bottom": 48,
                "left": 0,
                "width": 1080,
                "height": 1920,
                "source": "device_snapshot",
            }
        },
    }
    assert source.capture_role == "observation"
    assert source.parent_capture_id == "66666666-6666-6666-6666-666666666666"
    assert source.surface_phase_run_id == "surface-phase-1"
    assert source.captured_at == "2026-08-25 10:00:01+08"


async def test_load_source_falls_back_to_trusted_app_capture_location() -> None:
    connection = CaptureSourceConnection(url="")
    repository = PostgresObservationPacketRepository(
        pool=FakePool(connection),  # type: ignore[arg-type]
        artifact_store=object(),  # type: ignore[arg-type]
    )

    source = await repository.load_capture_context(
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
    )

    assert source is not None
    assert source.page_url == "app://com.example/.MainActivity"


class RecordingConnection:
    """记录所有 fetch/fetchrow 查询，用于验证是否触达附件表。"""

    def __init__(self) -> None:
        self.queries: list[str] = []

    async def fetchrow(self, query: str, *args: Any) -> None:
        del args
        self.queries.append(" ".join(query.split()))
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        del args
        self.queries.append(" ".join(query.split()))
        return []


async def test_load_capture_context_never_queries_evidence_artifact_table() -> None:
    """E22 验收：判定读取路径不得直查 qc_evidence_artifact。"""
    connection = RecordingConnection()
    repository = PostgresObservationPacketRepository(
        pool=FakePool(connection),  # type: ignore[arg-type]
        artifact_store=object(),  # type: ignore[arg-type]
    )

    source = await repository.load_capture_context(
        task_id="11111111-1111-1111-1111-111111111111",
        page_capture_id="22222222-2222-2222-2222-222222222222",
    )

    # capture 不存在时提前返回，且全程未触达附件表
    assert source is None
    assert connection.queries
    assert all("qc_evidence_artifact" not in query for query in connection.queries)


def test_load_capture_context_delegates_without_artifacts() -> None:
    source = inspect.getsource(
        PostgresObservationPacketRepository.load_capture_context
    )

    assert "include_artifacts=False" in source
