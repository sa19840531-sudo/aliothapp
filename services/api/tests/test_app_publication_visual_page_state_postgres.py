"""真实隔离 PG 验证封存发布的视觉状态及同帧证据围栏。"""

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application.app_evidence_manifest import AppEvidenceManifestFinalizer
from services.api.app.application.app_exploration_evidence import AppExplorationEvidenceAssembler
from services.api.app.application.app_inspection_publication import (
    AppInspectionPublicationEntryInput,
)
from services.api.app.application.app_sealed_inspection_publisher import (
    AppSealedEvidenceInspectionPublisher,
)
from services.api.app.domain.evidence import (
    AppEvidenceGap,
    AppEvidenceSemanticRole,
    AppExplorationResult,
    AppExplorationResultOutcome,
    app_exploration_result_context_sha256,
    seal_app_evidence_index,
)
from services.api.app.domain.exploration import AppVisualAnalysis
from services.api.app.infrastructure.db.app_evidence_index_repository import (
    PostgresAppEvidenceIndexRepository,
)
from services.api.app.infrastructure.db.app_exploration_result_repository import (
    PostgresAppExplorationResultRepository,
)
from services.api.app.infrastructure.db.app_inspection_publication_repository import (
    PostgresAppInspectionPublicationRepository,
)
from services.api.app.infrastructure.db.app_visual_observation_repository import (
    PostgresAppVisualObservationRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_capture_session_auth_postgres import _database
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 只迁移显式隔离测试库
)


async def _source(connection, pool, ids, saved, tmp_path, *, state, metadata_state, corrupt=None):
    manifests = PostgresEvidenceManifestRepository(pool=pool)
    pending = await manifests.get_latest_for_capture(
        task_id=ids["task"], capture_id=saved.capture_id,
    )
    await AppEvidenceManifestFinalizer(
        repository=manifests,
        artifact_store=LocalEvidenceArtifactStore(root=tmp_path / "evidence"),
    ).finalize(pending)
    if metadata_state is not None:
        await connection.execute(
            "UPDATE qc_capture SET metadata=metadata || "
            "jsonb_build_object('page_state',$2::text) WHERE id=$1::uuid",
            saved.capture_id, metadata_state,
        )
    visuals = PostgresAppVisualObservationRepository(pool=pool, page_graph_repository=None)
    if state is not None:
        await visuals.save(AppVisualAnalysis.build(
            capture_id=saved.capture_id,
            screenshot_artifact_id=saved.screenshot.artifact_id,
            image_signature=saved.image_signature, width=saved.width, height=saved.height,
            orientation=saved.orientation, package_name=saved.package_name,
            activity=saved.activity, visual_page_type="home",
            visual_summary="封存发布状态回归", key_elements=(), candidates=(),
            provider_id="controlled-state-test", model_id="controlled-payload",
            page_state=state, surface_facts={},
        ))
    if corrupt is not None:
        await corrupt(connection, ids, saved)
    await connection.execute(
        "UPDATE qc_task SET finalization_agent_run_id=$2::uuid WHERE id=$1::uuid",
        ids["task"], ids["run"],
    )
    coverage = {"diagnostic_capture_count": 1, "full_exploration_verified": False}
    now = datetime.now(UTC)
    indexes = PostgresAppEvidenceIndexRepository(pool=pool)
    building = AppExplorationEvidenceAssembler().assemble(
        task_id=ids["task"], run_id=ids["run"], build_id=ids["build_ref"],
        surface_session_id=ids["session"], created_at=now,
        captures=await manifests.list_latest_for_session(
            task_id=ids["task"], session_id=ids["session"], agent_run_id=ids["run"],
        ),
        result_context_sha256=app_exploration_result_context_sha256(
            coverage_summary=coverage, started_at=now,
        ),
    ).index
    assert len(building.entries) == 1
    await indexes.create(index=building)
    sealed = seal_app_evidence_index(
        building, outcome=AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT,
        terminal_reason_code="test_partial_capture", sealed_at=datetime.now(UTC),
        evidence_gaps=(AppEvidenceGap(
            semantic_role=AppEvidenceSemanticRole.FINAL_OBSERVATION,
            reason_code="test_subset_only", description="单帧状态测试，不声明完整探索",
            metadata={"scope": "run"},
        ),),
    )
    await indexes.seal(index=sealed)
    await PostgresAppExplorationResultRepository(pool=pool).create(result=AppExplorationResult(
        task_id=ids["task"], run_id=ids["run"], surface_session_id=ids["session"],
        outcome=sealed.outcome, terminal_reason_code=sealed.terminal_reason_code,
        evidence_index_id=sealed.index_id, evidence_index_hash=sealed.content_sha256,
        coverage_summary=coverage, evidence_gaps=sealed.evidence_gaps,
        started_at=now, completed_at=sealed.sealed_at,
    ))
    await connection.execute(
        "UPDATE qc_surface_session SET status='lost', lost_at=now(), "
        "failure_reason='单帧测试终止' WHERE session_id=$1", ids["session"],
    )
    await connection.execute(
        "UPDATE qc_agent_run SET status='done' WHERE id=$1::uuid", ids["run"],
    )
    publications = PostgresAppInspectionPublicationRepository(pool=pool)
    publisher = AppSealedEvidenceInspectionPublisher(
        evidence_indexes=indexes, manifests=manifests,
        sessions=PostgresSurfaceSessionRepository(pool=pool),
        visual_observations=visuals, publication_writer=publications,
    )
    entry = sealed.entries[0]
    return SimpleNamespace(
        index=sealed, publisher=publisher, publications=publications,
        request=dict(
            task_id=ids["task"], source_agent_run_id=ids["run"],
            evidence_index_id=sealed.index_id, evidence_index_hash=sealed.content_sha256,
            expected_finalization_epoch=0,
            entries=(AppInspectionPublicationEntryInput(
                sequence_no=entry.sequence_no, capture_id=entry.capture_id,
                evidence_manifest_id=entry.evidence_manifest_id,
                evidence_manifest_version=entry.evidence_manifest_version,
                semantic_role=entry.semantic_role.value,
                page_type="home" if state is not None else None,
                page_state=state if state is not None else "unknown",
            ),),
        ),
    )


async def _counts(connection, task_id):
    queries = (
        "SELECT count(*) FROM qc_app_inspection_publication WHERE task_id=$1::uuid",
        "SELECT count(*) FROM qc_app_inspection_publication_entry WHERE publication_id IN "
        "(SELECT id FROM qc_app_inspection_publication WHERE task_id=$1::uuid)",
        "SELECT count(*) FROM qc_outbox_event WHERE task_id=$1::uuid "
        "AND aggregate_type='app_inspection_publication'",
    )
    return tuple([await connection.fetchval(query, task_id) for query in queries])


async def _evidence(connection, source, saved):
    return (
        await connection.fetchval(
            "SELECT metadata FROM qc_capture WHERE id=$1::uuid", saved.capture_id,
        ),
        await connection.fetchrow(
            "SELECT metadata FROM qc_app_evidence_index_entry "
            "WHERE index_id=$1", source.index.index_id,
        ),
        await connection.fetchrow(
            "SELECT * FROM qc_app_evidence_index WHERE index_id=$1",
            source.index.index_id,
        ),
        await connection.fetchrow(
            "SELECT * FROM qc_evidence_manifest WHERE manifest_id=$1 AND manifest_version=$2",
            source.index.entries[0].evidence_manifest_id,
            source.index.entries[0].evidence_manifest_version,
        ),
    )


@pytest.mark.parametrize(("state", "metadata_state"), [
    ("popup", None), ("unknown", "stable"), ("stable", "popup"),
])
async def test_sealed_publication_freezes_trusted_visual_state_without_changing_index(
    tmp_path, state, metadata_state,
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        source = await _source(
            connection, pool, ids, saved, tmp_path, state=state, metadata_state=metadata_state,
        )
        before = await _evidence(connection, source, saved)
        result = await source.publisher.publish(task_id=ids["task"], agent_run_id=ids["run"])
        row = await connection.fetchrow(
            "SELECT page_type,page_state FROM qc_app_inspection_publication_entry "
            "WHERE publication_id=$1::uuid", result.publication_id,
        )
        assert dict(row) == {"page_type": "home", "page_state": state}
        assert await _evidence(connection, source, saved) == before
        replay = await source.publisher.publish(task_id=ids["task"], agent_run_id=ids["run"])
        assert replay.publication_created is False
        assert await _counts(connection, ids["task"]) == (1, 1, 1)


@pytest.mark.parametrize("metadata_state", [None, "stable"])
async def test_missing_visual_is_unknown_even_if_old_metadata_claims_stable(
    tmp_path, metadata_state,
):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        source = await _source(
            connection, pool, ids, saved, tmp_path, state=None, metadata_state=metadata_state,
        )
        before = await _evidence(connection, source, saved)
        result = await source.publisher.publish(task_id=ids["task"], agent_run_id=ids["run"])
        row = await connection.fetchrow(
            "SELECT page_type,page_state FROM qc_app_inspection_publication_entry "
            "WHERE publication_id=$1::uuid", result.publication_id,
        )
        assert dict(row) == {"page_type": None, "page_state": "unknown"}
        assert result.capture_results[0].status == "visual_fact_missing"
        assert result.outcome is AppExplorationResultOutcome.EVIDENCE_INSUFFICIENT
        assert await _evidence(connection, source, saved) == before


async def test_repository_rejects_state_drift_without_partial_publication(tmp_path):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        source = await _source(
            connection, pool, ids, saved, tmp_path, state="popup", metadata_state="stable",
        )
        changed = replace(source.request["entries"][0], page_state="stable")
        with pytest.raises(RuntimeError, match="漂移"):
            await source.publications.create_for_index(**{**source.request, "entries": (changed,)})
        assert await _counts(connection, ids["task"]) == (0, 0, 0)
        first = await source.publications.create_for_index(**source.request)
        replay = await source.publications.create_for_index(**source.request)
        assert first.created and not replay.created
        assert await _counts(connection, ids["task"]) == (1, 1, 1)


@pytest.mark.parametrize("mismatch", ["session", "task", "artifact"])
async def test_repository_rejects_visual_outside_capture_manifest_identity(tmp_path, mismatch):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        async def corrupt(connection, ids, saved):
            if mismatch == "artifact":
                artifact_id = str(uuid4())
                await connection.execute(
                    "INSERT INTO qc_evidence_artifact "
                    "(id,task_id,page_capture_id,artifact_type,source,storage_path,"
                    "content_type,checksum,size_bytes,metadata) "
                    "SELECT $1::uuid,task_id,page_capture_id,artifact_type,source,storage_path,"
                    "content_type,checksum,size_bytes,metadata FROM qc_evidence_artifact "
                    "WHERE id=$2::uuid", artifact_id, saved.screenshot.artifact_id,
                )
                await connection.execute(
                    "UPDATE qc_app_visual_observation SET screenshot_artifact_id=$2::uuid "
                    "WHERE capture_id=$1::uuid", saved.capture_id, artifact_id,
                )
                return
            task_id = ids["task"]
            if mismatch == "task":
                task_id = str(uuid4())
                await connection.execute(
                    "INSERT INTO qc_task "
                    "(id,ruleset_id,status,target_platform,submitter_id,tenant_id,task_config) "
                    "SELECT $1::uuid,ruleset_id,status,target_platform,submitter_id,tenant_id,"
                    "task_config FROM qc_task WHERE id=$2::uuid", task_id, ids["task"],
                )
            session_id = f"state-fence-{uuid4()}"
            await connection.execute(
                "INSERT INTO qc_surface_session "
                "(session_id,task_id,surface_kind,session_owner,platform_build_ref) "
                "VALUES($1,$2::uuid,'android','state-fence',$3)",
                session_id, task_id, ids["build_ref"],
            )
            await connection.execute(
                "UPDATE qc_app_visual_observation SET task_id=$2::uuid,session_id=$3 "
                "WHERE capture_id=$1::uuid", saved.capture_id, task_id, session_id,
            )
        source = await _source(
            connection, pool, ids, saved, tmp_path, state="popup", metadata_state=None,
            corrupt=corrupt,
        )
        with pytest.raises(RuntimeError, match="视觉事实.*Manifest"):
            await source.publications.create_for_index(**source.request)
        assert await _counts(connection, ids["task"]) == (0, 0, 0)


async def test_outbox_failure_rolls_back_visual_state_publication(tmp_path, monkeypatch):
    async with _database(tmp_path) as (connection, pool, ids, saved):
        source = await _source(
            connection, pool, ids, saved, tmp_path, state="popup", metadata_state=None,
        )
        before = await _evidence(connection, source, saved)
        async def fail_outbox(*args, **kwargs):
            raise RuntimeError("测试下游事件写入失败")
        monkeypatch.setattr(source.publications, "_insert_available_event", fail_outbox)
        with pytest.raises(RuntimeError, match="下游事件"):
            await source.publications.create_for_index(**source.request)
        assert await _counts(connection, ids["task"]) == (0, 0, 0)
        assert await _evidence(connection, source, saved) == before
