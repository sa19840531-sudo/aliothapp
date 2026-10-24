"""EvidenceInputSet PostgreSQL 仓储的可信来源与幂等合同。"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest

from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
    seal_evidence_input_set,
)
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    EvidenceInputSetPersistenceError,
    PostgresEvidenceInputSetRepository,
)

NOW = datetime(2026, 8, 14, 18, 0, tzinfo=UTC)
TASK_ID = "11111111-1111-1111-1111-111111111111"
RUN_ID = "22222222-2222-2222-2222-222222222222"
RULE_ID = "33333333-3333-3333-3333-333333333333"
CAPTURE_IDS = (
    "44444444-4444-4444-4444-444444444444",
    "55555555-5555-5555-5555-555555555555",
)


def _manifest(capture_id: str):
    return build_evidence_manifest_reference(
        task_id=TASK_ID,
        session_id="session-1",
        capture_id=capture_id,
        manifest_version=2,
        artifacts=(
            ArtifactReference(
                artifact_id=(
                    "66666666-6666-6666-6666-666666666666"
                    if capture_id == CAPTURE_IDS[0]
                    else "77777777-7777-7777-7777-777777777777"
                ),
                artifact_type="ui_snapshot",
                role="primary",
                storage_path=f"evidence/{TASK_ID}/{capture_id}/ui.json",
                checksum="sha256:" + "a" * 64,
                size_bytes=128,
                content_type="application/json",
            ),
        ),
        observation_packet_ids=(),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="artifact_integrity_verified",
        created_at=NOW,
    )


def _input_set():
    manifests = tuple(_manifest(item) for item in CAPTURE_IDS)
    return seal_evidence_input_set(
        task_id=TASK_ID,
        source_agent_run_id=RUN_ID,
        rule_id=RULE_ID,
        rule_code="alioth_spec_48",
        rule_contract_hash="rule-contract-hash",
        ruleset_snapshot_hash="ruleset-snapshot-hash",
        scope=EvidenceInputScope.TASK,
        scope_key="toast_style",
        finalization_epoch=3,
        eligibility_snapshot_id="frontier-snapshot-9",
        eligibility_snapshot_hash="frontier-snapshot-hash",
        selection_policy_hash="selection-policy-hash",
        eligible_capture_ids=CAPTURE_IDS,
        candidates=tuple(
            EvidenceInputCandidate(
                manifest=manifest,
                capture_role="primary",
                page_sequence=index,
            )
            for index, manifest in enumerate(manifests, start=1)
        ),
        sealed_at=NOW,
    )


class _Context:
    def __init__(self, value: Any) -> None:
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        return None


class FakePool:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection

    def acquire(self) -> _Context:
        return _Context(self.connection)


class FakeConnection:
    def __init__(self, *, context_valid: bool = True, forged_manifest: bool = False) -> None:
        self.context_valid = context_valid
        self.forged_manifest = forged_manifest
        self.manifests = {item.capture_id: item for item in (_manifest(cid) for cid in CAPTURE_IDS)}
        self.main_payload: dict[str, Any] | None = None
        self.main_id: str | None = None
        self.main_hash: str | None = None
        self.member_rows: list[dict[str, Any]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.context_args: tuple[Any, ...] = ()

    def transaction(self) -> _Context:
        return _Context(self)

    async def execute(self, query: str, *args: Any) -> str:
        compact = " ".join(query.split())
        self.executed.append((compact, args))
        if "INSERT INTO qc_evidence_input_set_member" in compact:
            self.member_rows.append(
                {
                    "ordinal": args[2],
                    "capture_id": args[3],
                    "capture_role": args[4],
                    "page_sequence": args[5],
                    "manifest_id": args[6],
                    "manifest_version": args[7],
                    "manifest_hash": args[8],
                    "artifact_ids": json.loads(args[9]),
                    "observation_packet_ids": json.loads(args[10]),
                    "journey_execution_id": args[11],
                    "environment_profile_id": args[12],
                }
            )
        return "OK"

    async def fetchval(self, query: str, *args: Any):
        compact = " ".join(query.split())
        if "SELECT EXISTS" in compact:
            self.context_args = args
            return self.context_valid
        raise AssertionError(compact)

    async def fetchrow(self, query: str, *args: Any):
        compact = " ".join(query.split())
        if "SELECT task.target_platform" in compact:
            return {"target_platform": "h5", "task_config": {}}
        if "FROM qc_capture AS capture" in compact:
            manifest = self.manifests[str(args[1])]
            payload = manifest.to_dict()
            if self.forged_manifest:
                payload["artifacts"][0]["artifact_id"] = (
                    "88888888-8888-8888-8888-888888888888"
                )
                self.forged_manifest = False
            return {"capture_role": "primary", "manifest_payload": payload}
        if "FROM qc_evidence_input_set" in compact:
            if self.main_payload is None:
                return None
            return {
                "input_set_id": self.main_id,
                "input_set_hash": self.main_hash,
                "input_set_payload": self.main_payload,
            }
        if "INSERT INTO qc_evidence_input_set" in compact:
            self.main_id = str(args[0])
            self.main_hash = str(args[1])
            self.main_payload = json.loads(args[16])
            return {"input_set_id": self.main_id}
        raise AssertionError(compact)

    async def fetch(self, query: str, *args: Any):
        compact = " ".join(query.split())
        if "FROM qc_evidence_input_set_member" not in compact:
            raise AssertionError(compact)
        return tuple(sorted(self.member_rows, key=lambda row: int(row["ordinal"])))


async def test_repository_seals_current_epoch_and_exact_pass_manifest_members() -> None:
    connection = FakeConnection()
    input_set = _input_set()
    repository = PostgresEvidenceInputSetRepository(pool=FakePool(connection))

    persisted = await repository.add(input_set=input_set)

    assert persisted.created is True
    assert persisted.input_set == input_set
    assert connection.context_args[4] == ["session_draining"]
    assert connection.context_args[5] == input_set.finalization_epoch
    assert len(connection.member_rows) == 2
    assert connection.main_payload == input_set.to_dict()
    assert any(
        "pg_advisory_xact_lock" in query and args == (f"task-finalization:{TASK_ID}",)
        for query, args in connection.executed
    )


async def test_repository_retry_is_idempotent_and_does_not_duplicate_members() -> None:
    connection = FakeConnection()
    input_set = _input_set()
    repository = PostgresEvidenceInputSetRepository(pool=FakePool(connection))

    first = await repository.add(input_set=input_set)
    replay = replace(input_set, sealed_at=datetime(2026, 8, 14, 18, 5, tzinfo=UTC))
    second = await repository.add(input_set=replay)

    assert first.created is True
    assert second.created is False
    assert second.input_set == input_set
    assert second.input_set.sealed_at != replay.sealed_at
    assert len(connection.member_rows) == 2


async def test_repository_rejects_stale_or_wrong_task_context() -> None:
    connection = FakeConnection(context_valid=False)
    repository = PostgresEvidenceInputSetRepository(pool=FakePool(connection))

    with pytest.raises(EvidenceInputSetPersistenceError, match="Finalization Epoch"):
        await repository.add(input_set=_input_set())

    assert connection.main_payload is None


async def test_repository_rejects_manifest_payload_that_forges_artifact_closure() -> None:
    connection = FakeConnection(forged_manifest=True)
    repository = PostgresEvidenceInputSetRepository(pool=FakePool(connection))

    with pytest.raises(EvidenceInputSetPersistenceError, match="证据闭包"):
        await repository.add(input_set=_input_set())

    assert connection.main_payload is None


async def test_repository_rejects_corrupt_existing_input_set_payload() -> None:
    connection = FakeConnection()
    input_set = _input_set()
    connection.main_id = input_set.input_set_id
    connection.main_hash = input_set.input_set_hash
    connection.main_payload = {"protocol_version": "evidence-input-set.v1"}
    repository = PostgresEvidenceInputSetRepository(pool=FakePool(connection))

    with pytest.raises(EvidenceInputSetPersistenceError, match="载荷无法通过协议校验"):
        await repository.add(input_set=input_set)
