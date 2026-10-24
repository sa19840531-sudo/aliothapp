"""真实独立 PG 的只读原件加载；台账、动作和图片均为明确展示夹具。"""

import json
from datetime import UTC, datetime

import asyncpg
import pytest

from services.api.app.domain.evidence import (
    EvidenceInputCandidate,
    EvidenceInputScope,
    EvidenceManifestReference,
    seal_evidence_input_set,
)
from services.api.app.domain.interaction import build_action_intent_hash
from services.api.app.infrastructure.db.evidence_input_set_repository import (
    PostgresEvidenceInputSetRepository,
)
from services.api.app.infrastructure.db.green_process_evidence import load_green_process_evidence
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_checkpoint_evidence_postgres import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离库
    append_quality,
    case,
    save_receipt,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    pytestmark as pytestmark,
)
from services.api.tests.test_journey_cross_session_e2e_postgres import attach_actual_material


async def prepare(connection, repo, ids, work, context, decision, store):
    await repo.reserve(work=work, context=context, decision=decision)
    turn = await attach_actual_material(
        connection,
        store,
        await save_receipt(connection, ids, work, decision),
        instance="green-fixture",
        document=1000,
    )
    await repo.bind(work=work, context=context, turn=turn)
    await connection.execute(
        "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
        ids["task"],
        ids["run"],
    )
    await connection.execute(
        "UPDATE qc_action_execution SET intent_hash=$2 WHERE task_id=$1::uuid",
        ids["task"],
        build_action_intent_hash(decision),
    )
    candidates = []
    for index, capture in enumerate(
        (turn.coordination.before_capture, turn.coordination.after_capture)
    ):
        payload = await connection.fetchval(
            "SELECT manifest_payload FROM qc_evidence_manifest WHERE capture_id=$1::uuid "
            "ORDER BY manifest_version DESC LIMIT 1",
            capture.capture_id,
        )
        manifest = EvidenceManifestReference.from_dict(json.loads(payload))
        candidates.append(
            EvidenceInputCandidate(
                manifest=manifest, capture_role=capture.role, page_sequence=index + 1
            )
        )
    inputs = seal_evidence_input_set(
        task_id=ids["task"],
        source_agent_run_id=ids["run"],
        rule_id=ids["rule"],
        rule_code="FACT-GAP-1",
        rule_contract_hash="controlled-green-contract",
        ruleset_snapshot_hash="controlled-ruleset",
        scope=EvidenceInputScope.TASK,
        scope_key="green_temporal_rules.v1",
        finalization_epoch=0,
        eligibility_snapshot_id="controlled-green-eligibility-" + ids["task"],
        eligibility_snapshot_hash="controlled-green-eligibility",
        selection_policy_hash="controlled-selection",
        eligible_capture_ids=tuple(item.manifest.capture_id for item in candidates),
        candidates=tuple(candidates),
        sealed_at=datetime.now(UTC),
    )
    # 仅夹具直接持久化域对象；正式发布仍由现有 sealer 执行全部准入和清单裁定。
    await PostgresEvidenceInputSetRepository(pool=repo._pool)._insert_or_verify(
        connection, input_set=inputs
    )
    return inputs, turn


async def counts(connection, task_id):
    return tuple(
        [
            await connection.fetchval(
                f"SELECT count(*) FROM {name} WHERE task_id=$1::uuid",
                task_id,
            )
            for name in (
                "qc_action_execution",
                "qc_rule_execution",
                "qc_evidence_input_set",
                "qc_evidence_artifact",
                "qc_v2_work_item",
            )
        ]
    )


async def test_real_pg_readonly_transaction_checks_original_bytes_and_all_actions(tmp_path):
    async with case() as (connection, repo, ids, work, context, decision):
        store = LocalEvidenceArtifactStore(root=tmp_path)
        inputs, _ = await prepare(connection, repo, ids, work, context, decision, store)
        before = await counts(connection, ids["task"])
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            evidence = await load_green_process_evidence(
                connection, input_set=inputs, artifact_store=store
            )
            again = await load_green_process_evidence(
                connection, input_set=inputs, artifact_store=store
            )
        assert evidence.verified_read
        assert len(evidence.captures) == 2 and len(evidence.actions) == 1
        assert evidence.fingerprint == again.fingerprint
        assert any("green_capture_not_in_frozen_input" in item for item in evidence.issues)
        assert await counts(connection, ids["task"]) == before


@pytest.mark.parametrize("fault", ["quality", "bytes", "late_action", "session", "frozen_members"])
async def test_real_pg_late_drift_prevents_reusing_old_evidence_fingerprint(tmp_path, fault):
    async with case() as (connection, repo, ids, work, context, decision):
        store = LocalEvidenceArtifactStore(root=tmp_path)
        inputs, turn = await prepare(connection, repo, ids, work, context, decision, store)
        first = await load_green_process_evidence(
            connection, input_set=inputs, artifact_store=store
        )
        if fault == "quality":
            await append_quality(connection, turn.coordination.after_capture.capture_id, "fail", 3)
        elif fault == "bytes":
            artifact = first.captures[0].originals[0]
            path = await connection.fetchval(
                "SELECT storage_path FROM qc_evidence_artifact WHERE id=$1::uuid",
                artifact.artifact_id,
            )
            (tmp_path / path).write_bytes(b"changed-original-fixture")
        elif fault == "late_action":
            from dataclasses import replace

            await save_receipt(
                connection,
                ids,
                work,
                replace(decision, intent_id="new-formal-intent", idempotency_key="new-formal-key"),
                command_sequence=3,
            )
        elif fault == "session":
            await connection.execute(
                "UPDATE qc_capture SET surface_session_id='foreign-session' WHERE id=$1::uuid",
                turn.coordination.after_capture.capture_id,
            )
        else:
            with pytest.raises(asyncpg.RestrictViolationError, match="immutable"):
                await connection.execute(
                    "UPDATE qc_evidence_input_set_member SET artifact_ids='[]'::jsonb "
                    "WHERE input_set_id=$1 AND ordinal=1",
                    inputs.input_set_id,
                )
            unchanged = await load_green_process_evidence(
                connection, input_set=inputs, artifact_store=store
            )
            assert unchanged.fingerprint == first.fingerprint
            return
        before = await counts(connection, ids["task"])
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            changed = await load_green_process_evidence(
                connection, input_set=inputs, artifact_store=store
            )
        assert changed.fingerprint != first.fingerprint
        assert changed.issues
        assert await counts(connection, ids["task"]) == before
