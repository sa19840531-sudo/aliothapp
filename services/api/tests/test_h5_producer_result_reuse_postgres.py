"""主 Run 结论复用必须核验原封存输入，不能只凭页面与规则相同。"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
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
    PostgresEvidenceInputSetRepository,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.tests.test_exploration_scheduler_facts_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 - 显式升级隔离库
)
from services.api.tests.test_inspection_terminalization_postgres import _PinnedConnectionPool

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="需要显式隔离 PostgreSQL",
)


@asynccontextmanager
async def _rollback(connection):
    transaction = connection.transaction()
    await transaction.start()
    try:
        yield
    finally:
        await transaction.rollback()


async def _save_manifest(connection, manifest):
    await connection.execute(
        """
        INSERT INTO qc_evidence_manifest (
            manifest_id, manifest_version, task_id, session_id, capture_id,
            protocol_version, manifest_hash, payload_hash, quality_status, quality_reason,
            manifest_payload, created_at
        ) VALUES ($1,$2,$3::uuid,$4,$5::uuid,$6,$7,$7,$8,$11,$9::jsonb,$10)
        """,
        manifest.manifest_id,
        manifest.manifest_version,
        manifest.task_id,
        manifest.session_id,
        manifest.capture_id,
        manifest.protocol_version,
        manifest.manifest_hash,
        manifest.quality_status.value,
        json.dumps(manifest.to_dict()),
        manifest.created_at,
        manifest.quality_reason,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        None,
        "member_input_missing",
        "execution_input_missing",
        "contract",
        "ruleset",
        "source_run",
        "capture_status",
        "latest_fail",
        "latest_artifact",
        "latest_same",
        "database_binding",
        "database_binding_immutable",
        "database_binding_contract",
        "database_binding_web",
        "database_binding_downgrade",
        "race_manifest",
        "race_contract",
        "same_consumer_bound",
        "same_consumer_unbound",
        "artifact_row_missing",
        "artifact_row_checksum",
        "artifact_row_deleted",
        "verdict_fail",
        "verdict_review",
        "verdict_warning",
        "packet_bound",
        "packet_generic",
        "packet_missing",
        "packet_wrong_rule",
        "packet_quality",
        "artifact_immutable",
        "ordinary_publish",
        "ordinary_publish_wrong_contract",
        "old_gap_terminal",
        "old_gap_inflight",
        "page_read",
        "page_read_review",
        "page_read_cancelled",
        "page_read_restored",
        "page_read_restored_material",
        "page_read_restored_parent_fail",
        "page_read_expired",
        "page_read_attempt",
        "page_read_web",
        "page_read_epoch",
        "page_read_sync",
        "page_read_sync_foreign",
        "page_read_sync_stale",
        "page_read_sync_material",
        "page_read_sync_failed_quality",
        "page_read_sync_pass",
        "page_read_sync_fail",
        "original_candidate",
        "original_candidate_wrong_contract",
        "original_candidate_missing",
    ],
)
async def test_producer_reuse_requires_original_input_and_live_evidence(failure, monkeypatch):
    async with _database(surface_kind="web", target_platform="h5") as (connection, _, ids):
        # 全部新增材料置于回滚事务；既有任务与封存历史不修改、不删除。
        async with _rollback(connection):
            # 仓储反例需注入历史不一致成员；真实数据库绑定约束另行独立验证。
            if not str(failure).startswith(("database_binding", "ordinary_publish")):
                await connection.execute(
                    "CREATE TEMP TABLE qc_inspection_work_item_rule "
                    "(LIKE public.qc_inspection_work_item_rule INCLUDING DEFAULTS "
                    "INCLUDING GENERATED INCLUDING INDEXES) ON COMMIT DROP",
                )
            pool = _PinnedConnectionPool(connection)
            consumer = ids["run"] if str(failure).startswith("same_consumer") else str(uuid4())
            if consumer != ids["run"]:
                await connection.execute(
                    "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name) "
                    "VALUES($1::uuid,$2::uuid,'rule_judge','strict-reuse-consumer')",
                    consumer,
                    ids["task"],
                )
            snapshot = "strict-reuse-ruleset"
            await connection.execute(
                "UPDATE qc_ruleset SET snapshot_hash=$2 WHERE id=$1::uuid",
                ids["ruleset"],
                snapshot,
            )
            # 正例保持真实捕获角色，不能把 probe_before 伪装成动作后证据。
            packet_id = str(uuid4()) if str(failure).startswith("packet_") else None
            manifest = build_evidence_manifest_reference(
                task_id=ids["task"],
                session_id=ids["session"],
                capture_id=ids["capture"],
                manifest_version=1,
                artifacts=(
                    ArtifactReference(
                        artifact_id=str(uuid4()),
                        artifact_type="ui_snapshot",
                        role="bootstrap",
                        storage_path=f"evidence/{ids['task']}/{ids['capture']}/ui.json",
                        checksum="sha256:" + "a" * 64,
                        size_bytes=128,
                        content_type="application/json",
                    ),
                ),
                observation_packet_ids=(packet_id,) if packet_id else (),
                quality_status=EvidenceQualityStatus.PASS,
                quality_reason="artifact_integrity_verified",
                created_at=datetime.now(UTC),
            )
            if failure != "artifact_row_missing":
                artifact = manifest.artifacts[0]
                await connection.execute(
                    "INSERT INTO qc_evidence_artifact "
                    "(id,task_id,page_capture_id,artifact_type,source,storage_path,checksum,"
                    "size_bytes,content_type) "
                    "VALUES($1::uuid,$2::uuid,$3::uuid,$4,'baseline',$5,$6,$7,$8)",
                    artifact.artifact_id,
                    ids["task"],
                    ids["capture"],
                    artifact.artifact_type,
                    artifact.storage_path,
                    "sha256:" + "c" * 64
                    if failure == "artifact_row_checksum"
                    else artifact.checksum,
                    artifact.size_bytes,
                    artifact.content_type,
                )
            if packet_id and failure != "packet_missing":
                packet_rule = None if failure == "packet_generic" else ids["rule"]
                if failure == "packet_wrong_rule":
                    packet_rule = str(uuid4())
                    await connection.execute(
                        "INSERT INTO qc_rule "
                        "(id,ruleset_id,rule_code,name,category,executor_type,check_point) "
                        "VALUES($1::uuid,$2::uuid,'OTHER-RULE','其他规则','text','rule_engine','detail')",
                        packet_rule,
                        ids["ruleset"],
                    )
                await connection.execute(
                    "INSERT INTO qc_observation_packet "
                    "(id,task_id,page_capture_id,rule_id,created_by_agent_run_id,"
                    "quality_gate_status,evidence_artifact_ids) "
                    "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6,$7::jsonb)",
                    packet_id,
                    ids["task"],
                    ids["capture"],
                    packet_rule,
                    ids["run"],
                    "fail" if failure == "packet_quality" else "pass",
                    json.dumps([manifest.artifacts[0].artifact_id]),
                )
            await _save_manifest(connection, manifest)
            input_set = seal_evidence_input_set(
                task_id=ids["task"],
                source_agent_run_id=ids["run"],
                rule_id=ids["rule"],
                rule_code="FACT-GAP-1",
                rule_contract_hash="strict-reuse-contract",
                ruleset_snapshot_hash=snapshot,
                scope=EvidenceInputScope.PAGE,
                scope_key="price",
                finalization_epoch=0,
                eligibility_snapshot_id="strict-page",
                eligibility_snapshot_hash="strict-page-hash",
                selection_policy_hash="strict-policy",
                eligible_capture_ids=(ids["capture"],),
                candidates=(
                    EvidenceInputCandidate(
                        manifest=manifest,
                        capture_role="bootstrap",
                        page_sequence=1,
                    ),
                ),
                sealed_at=datetime.now(UTC),
            )
            await PostgresEvidenceInputSetRepository(
                pool=pool,
                permitted_finalization_states=("open",),
            ).add(input_set=input_set)
            if str(failure).startswith("original_candidate"):
                if not failure.endswith("missing"):
                    await connection.execute(
                        "INSERT INTO qc_rule_execution(task_id,rule_id,page_capture_id,"
                        "agent_run_id,execution_status,judge_strategy,evidence_input_set_id) "
                        "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,'manual_review','hybrid',$5)",
                        ids["task"],
                        ids["rule"],
                        ids["capture"],
                        ids["run"],
                        input_set.input_set_id,
                    )
                bindings = await PostgresInspectionWorkItemRepository(
                    pool=pool
                ).read_original_page_inputs(
                    task_id=ids["task"],
                    source_agent_run_id=ids["run"],
                    page_capture_id=ids["capture"],
                    rule_contracts={
                        ids["rule"]: "wrong"
                        if failure.endswith("wrong_contract")
                        else "strict-reuse-contract"
                    },
                )
                assert bindings == (
                    {ids["rule"]: input_set.input_set_id} if failure == "original_candidate" else {}
                )
                return
            work_id = ids["inspection_work_item"]
            if str(failure).startswith("ordinary_publish"):
                from services.api.app.application.inspection_work_items import (
                    InspectionWorkItemRule,
                    PublishInspectionWorkItemCommand,
                    build_inspection_work_item_input_hash,
                )

                command = PublishInspectionWorkItemCommand(
                    task_id=ids["task"],
                    page_capture_id=ids["capture"],
                    producer_agent_run_id=ids["run"],
                    ruleset_id=ids["ruleset"],
                    ruleset_version=1,
                    ruleset_snapshot_hash=snapshot,
                    batch_key="ordinary-bound-publication",
                    strategy="rule_engine",
                    session_dependency="snapshot",
                    rules=(
                        InspectionWorkItemRule(
                            ids["rule"],
                            "FACT-GAP-1",
                            0,
                            "wrong-contract"
                            if failure.endswith("wrong_contract")
                            else "strict-reuse-contract",
                            input_set.input_set_id,
                        ),
                    ),
                )
                repository = PostgresInspectionWorkItemRepository(pool=pool)
                if failure.endswith("wrong_contract"):
                    with pytest.raises(RuntimeError, match="冻结 InputSet"):
                        await repository.publish(
                            command=command,
                            input_hash=build_inspection_work_item_input_hash(command),
                        )
                    return
                published = await repository.publish(
                    command=command,
                    input_hash=build_inspection_work_item_input_hash(command),
                )
                assert (
                    await connection.fetchval(
                        "SELECT evidence_input_set_id FROM qc_inspection_work_item_rule "
                        "WHERE work_item_id=$1::uuid",
                        published.work_item_id,
                    )
                    == input_set.input_set_id
                )
                duplicate = await repository.publish(
                    command=command,
                    input_hash=build_inspection_work_item_input_hash(command),
                )
                assert duplicate.work_item_id == published.work_item_id
                assert duplicate.created is False
                return
            await connection.execute(
                "UPDATE qc_inspection_work_item SET status='waiting_evidence',"
                "control_mode='enforced',consumer_agent_run_id=$2::uuid,"
                "ruleset_snapshot_hash=$3 WHERE id=$1::uuid",
                work_id,
                consumer,
                snapshot,
            )
            await connection.execute(
                "UPDATE qc_inspection_attempt SET consumer_agent_run_id=$2::uuid "
                "WHERE work_item_id=$1::uuid",
                work_id,
                consumer,
            )
            await connection.execute(
                "UPDATE qc_evidence_request SET status='unfulfillable',completed_at=now() "
                "WHERE request_id=$1",
                ids["request"],
            )
            if failure in {"database_binding_contract", "database_binding_web"}:
                if failure == "database_binding_web":
                    await connection.execute(
                        "UPDATE qc_task SET target_platform='web' WHERE id=$1::uuid",
                        ids["task"],
                    )
                code = (
                    "H5_PAGE_RULE_BINDING"
                    if failure == "database_binding_contract"
                    else "WEB_SEQUENCE_RULE_BINDING"
                )
                with pytest.raises(asyncpg.CheckViolationError, match=code):
                    async with connection.transaction():
                        await connection.execute(
                            "INSERT INTO qc_inspection_work_item_rule "
                            "(work_item_id,rule_id,rule_code,ordinal,contract_hash,"
                            "evidence_input_set_id) "
                            "VALUES($1::uuid,$2::uuid,'FACT-GAP-1',0,'wrong-contract',$3)",
                            work_id,
                            ids["rule"],
                            input_set.input_set_id,
                        )
                return
            await connection.execute(
                "INSERT INTO qc_inspection_work_item_rule "
                "(work_item_id,rule_id,rule_code,ordinal,contract_hash,evidence_input_set_id) "
                "VALUES($1::uuid,$2::uuid,'FACT-GAP-1',0,'strict-reuse-contract',$3)",
                work_id,
                ids["rule"],
                input_set.input_set_id,
            )
            if str(failure).startswith("page_read"):
                restored = None
                if failure.startswith("page_read_restored"):
                    from services.api.tests.h5_restored_input_support import (
                        seed_restored_supplement,
                    )

                    restored = await seed_restored_supplement(connection, ids)
                from services.api.app.application.observation_packet import (
                    ObservationPacketValidationError,
                )
                from services.api.app.infrastructure.db.h5_page_binding_reader import (
                    PostgresH5PageBindingReader,
                )

                lease = str(uuid4())
                await connection.execute(
                    "UPDATE qc_inspection_work_item SET status='running',attempt_count=1,"
                    "lease_token=$2::uuid,lease_owner='isolated',"
                    "lease_expires_at=now()+interval '5 minutes' WHERE id=$1::uuid",
                    work_id,
                    lease,
                )
                attempt = await connection.fetchval(
                    "UPDATE qc_inspection_attempt SET status='running',lease_token=$2::uuid,"
                    "input_hash=(SELECT input_hash FROM qc_inspection_work_item WHERE id=$1::uuid) "
                    "WHERE work_item_id=$1::uuid RETURNING id::text",
                    work_id,
                    lease,
                )
                if restored:
                    from services.api.app.application.inspection_work_items import (
                        build_inspection_attempt_input_hash,
                    )
                    from services.api.app.infrastructure.db.inspection_work_item_repository import (
                        _load_rule_evidence_bindings,
                    )

                    bindings = await _load_rule_evidence_bindings(connection, work_item_id=work_id)
                    attempt_hash = build_inspection_attempt_input_hash(
                        work_item_input_hash=await connection.fetchval(
                            "SELECT input_hash FROM qc_inspection_work_item WHERE id=$1::uuid",
                            work_id,
                        ),
                        rule_evidence=bindings,
                    )
                    await connection.execute(
                        "UPDATE qc_inspection_attempt SET input_hash=$2 WHERE id=$1::uuid",
                        attempt,
                        attempt_hash,
                    )
                if failure == "page_read_expired":
                    await connection.execute(
                        "UPDATE qc_inspection_work_item "
                        "SET lease_expires_at=now()-interval '1 second' WHERE id=$1::uuid",
                        work_id,
                    )
                elif failure == "page_read_attempt":
                    await connection.execute(
                        "UPDATE qc_inspection_attempt SET input_hash='stale-attempt' "
                        "WHERE id=$1::uuid",
                        attempt,
                    )
                elif failure == "page_read_web":
                    await connection.execute(
                        "UPDATE qc_task SET target_platform='web' WHERE id=$1::uuid", ids["task"]
                    )
                elif failure == "page_read_epoch":
                    await connection.execute(
                        "UPDATE qc_task SET finalization_epoch=finalization_epoch+1 "
                        "WHERE id=$1::uuid",
                        ids["task"],
                    )
                elif failure in {"page_read_review", "page_read_cancelled"}:
                    await connection.execute(
                        "UPDATE qc_task SET status=$2 WHERE id=$1::uuid",
                        ids["task"],
                        "review" if failure == "page_read_review" else "canceled",
                    )
                reader = PostgresH5PageBindingReader(pool=pool)
                kwargs = dict(
                    task_id=ids["task"],
                    page_capture_id=ids["capture"],
                    source_agent_run_id=ids["run"],
                    input_set_id=input_set.input_set_id,
                    rule_id=ids["rule"],
                    rule_contract_hash="strict-reuse-contract",
                    work_item_id=work_id,
                    attempt_id=attempt,
                    consumer_agent_run_id=consumer,
                    lease_token=lease,
                )
                if restored:
                    from services.api.app.domain.evidence import EvidenceInputSet

                    producer, before, manifests, restored_input_id, payload = restored
                    kwargs.update(
                        source_agent_run_id=producer,
                        page_capture_id=before,
                        input_set_id=restored_input_id,
                    )
                    loaded = await reader.load_authorized(**kwargs)
                    assert loaded == EvidenceInputSet.from_dict(
                        json.loads(
                            await connection.fetchval(
                                "SELECT input_set_payload FROM qc_evidence_input_set "
                                "WHERE input_set_id=$1",
                                restored_input_id,
                            )
                        )
                    )
                    assert loaded.source_agent_run_id == producer != ids["run"]
                    if failure.endswith("parent_fail"):
                        failed = build_evidence_manifest_reference(
                            task_id=ids["task"],
                            session_id=manifests[0].session_id,
                            capture_id=before,
                            manifest_version=2,
                            artifacts=manifests[0].artifacts,
                            quality_status=EvidenceQualityStatus.FAIL,
                            quality_reason="damaged_frame",
                            created_at=datetime.now(UTC),
                        )
                        await _save_manifest(connection, failed)
                        with pytest.raises(ValueError, match="前帧"):
                            await reader.load_authorized(**kwargs)
                        return
                    from services.api.app.application.h5_page_input_reader import (
                        H5PageInputObservationReader,
                    )
                    from services.api.app.application.manifest_backed_packet import (
                        ManifestBackedPacketLoader,
                    )
                    from services.api.app.application.observation_packet import (
                        BuildObservationPacketRequest,
                        ObservationPacketService,
                    )
                    from services.api.app.infrastructure.db.evidence_manifest_repository import (
                        PostgresEvidenceManifestRepository,
                    )
                    from services.api.app.infrastructure.db.observation_packet_repository import (
                        PostgresObservationPacketRepository,
                    )

                    class Store:
                        async def read(self, path):
                            assert path in {
                                item.storage_path
                                for manifest in manifests
                                for item in manifest.artifacts
                            }
                            return payload

                    contexts = PostgresObservationPacketRepository(
                        pool=pool, artifact_store=Store()
                    )
                    source_reader = H5PageInputObservationReader(
                        inner=contexts,
                        contexts=contexts,
                        bindings=reader,
                        packet_loader=ManifestBackedPacketLoader(
                            manifests=PostgresEvidenceManifestRepository(pool=pool),
                            artifact_reader=Store(),
                        ),
                    )
                    binding = bindings[0]
                    observation = await ObservationPacketService(repository=source_reader).build(
                        BuildObservationPacketRequest(
                            task_id=ids["task"],
                            page_capture_id=ids["capture"],
                            rule_id=ids["rule"],
                            rule_code="FACT-GAP-1",
                            rule_name="套餐反馈",
                            category="text",
                            executor_type="rule_engine",
                            check_point="价格",
                            input_contract={},
                            judge_config={},
                            manual_review_policy={},
                            agent_input={},
                            agent_run_id=consumer,
                            source_agent_run_id=producer,
                            evidence_source_page_capture_id=before,
                            evidence_manifest_id=binding.manifest_id,
                            original_page_input_set_id=loaded.input_set_id,
                            expected_contract_hash="strict-reuse-contract",
                            work_item_id=work_id,
                            attempt_id=attempt,
                            lease_token=lease,
                            evidence_capture_roles=dict(binding.evidence_capture_roles),
                            evidence_manifest_refs={
                                role: {"manifest_id": ref, "manifest_version": version}
                                for role, ref, version in binding.evidence_manifest_refs
                            },
                            requested_evidence_artifact_ids=binding.artifact_ids,
                        ),
                    )
                    packet = observation.observation_packet_id
                    artifacts = [
                        item.artifact_id for manifest in manifests for item in manifest.artifacts
                    ]
                    assert set(observation.evidence_artifact_ids) == set(artifacts)
                    assert (
                        observation.packet_payload["identity"]["original_page_input"][
                            "input_set_id"
                        ]
                        == restored_input_id
                    )
                    if failure.endswith("material"):
                        artifacts = list(loaded.members[0].artifact_ids)
                        await connection.execute(
                            "UPDATE qc_observation_packet SET evidence_artifact_ids=$2::jsonb "
                            "WHERE id=$1::uuid",
                            packet,
                            json.dumps(artifacts),
                        )
                    execution = await connection.fetchval(
                        "INSERT INTO qc_rule_execution(task_id,page_capture_id,"
                        "rule_id,agent_run_id,"
                        "execution_status,judge_strategy,observation_packet_id) "
                        "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,"
                        "'manual_review','hybrid',$5::uuid) "
                        "RETURNING id",
                        ids["task"],
                        ids["capture"],
                        ids["rule"],
                        consumer,
                        packet,
                    )
                    synced = await PostgresInspectionWorkItemRepository(
                        pool=pool
                    ).sync_formal_results(
                        work_item_id=work_id,
                        lease_token=lease,
                    )
                    permitted = failure == "page_read_restored"
                    assert synced.terminal_rule_count == int(permitted)
                    assert await connection.fetchval(
                        "SELECT evidence_input_set_id FROM qc_rule_execution WHERE id=$1",
                        execution,
                    ) == (restored_input_id if permitted else None)
                    assert (
                        await connection.fetchval(
                            "SELECT evidence_input_set_id FROM qc_inspection_work_item_rule "
                            "WHERE work_item_id=$1::uuid",
                            work_id,
                        )
                        == input_set.input_set_id
                    )
                    return
                if failure.startswith("page_read_sync"):
                    verdict = (
                        "pass"
                        if failure.endswith(("failed_quality", "_pass"))
                        else "fail"
                        if failure.endswith("_fail")
                        else "manual_review"
                    )
                    marker = dict(
                        input_set_id=input_set.input_set_id,
                        input_set_hash=input_set.input_set_hash,
                        rule_contract_hash=input_set.rule_contract_hash,
                        work_item_id=work_id,
                        attempt_id=str(uuid4()) if failure.endswith("stale") else attempt,
                        consumer_agent_run_id=consumer,
                        manifest_id=manifest.manifest_id,
                    )
                    packet = str(uuid4())
                    await connection.execute(
                        "INSERT INTO qc_observation_packet(id,task_id,page_capture_id,rule_id,"
                        "created_by_agent_run_id,packet_payload,evidence_artifact_ids) "
                        "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$6::jsonb,$7::jsonb)",
                        packet,
                        ids["task"],
                        ids["capture"],
                        ids["rule"],
                        consumer,
                        json.dumps(
                            {"identity": {"original_page_input": marker, "target_platform": "h5"}}
                        ),
                        json.dumps(
                            []
                            if failure.endswith("material")
                            else list(input_set.members[0].artifact_ids)
                        ),
                    )
                    if verdict in {"pass", "fail"}:
                        await connection.execute(
                            "UPDATE qc_observation_packet SET quality_gate_status=$2 "
                            "WHERE id=$1::uuid",
                            packet,
                            "fail" if failure.endswith("failed_quality") else "pass",
                        )
                    result = str(uuid4())
                    await connection.execute(
                        "INSERT INTO qc_rule_execution(id,task_id,page_capture_id,rule_id,"
                        "agent_run_id,execution_status,"
                        "judge_strategy,observation_packet_id,evidence_input_set_id) "
                        "VALUES($1::uuid,$2::uuid,$3::uuid,$4::uuid,$5::uuid,"
                        "'manual_review','hybrid',$6::uuid,$7)",
                        result,
                        ids["task"],
                        ids["capture"],
                        ids["rule"],
                        consumer,
                        packet,
                        "foreign-original" if failure.endswith("foreign") else None,
                    )
                    if verdict != "manual_review":
                        await connection.execute(
                            "UPDATE qc_rule_execution SET execution_status=$2 WHERE id=$1::uuid",
                            result,
                            verdict,
                        )
                    synced = await PostgresInspectionWorkItemRepository(
                        pool=pool
                    ).sync_formal_results(
                        work_item_id=work_id,
                        lease_token=lease,
                    )
                    permitted = failure in {
                        "page_read_sync",
                        "page_read_sync_pass",
                        "page_read_sync_fail",
                    }
                    assert synced.terminal_rule_count == int(permitted)
                    assert await connection.fetchval(
                        "SELECT evidence_input_set_id FROM qc_rule_execution WHERE id=$1::uuid",
                        result,
                    ) == (
                        input_set.input_set_id
                        if permitted
                        else "foreign-original"
                        if failure.endswith("foreign")
                        else None
                    )
                    assert (
                        await connection.fetchval(
                            "SELECT execution_status FROM qc_rule_execution WHERE id=$1::uuid",
                            result,
                        )
                        == verdict
                    )
                    return
                if failure in {"page_read", "page_read_review"}:
                    assert await reader.load_authorized(**kwargs) == input_set
                    with pytest.raises(ObservationPacketValidationError):
                        await reader.load_authorized(**{**kwargs, "page_capture_id": str(uuid4())})
                else:
                    with pytest.raises(ObservationPacketValidationError):
                        await reader.load_authorized(**kwargs)
                return
            if failure == "database_binding_immutable":
                with pytest.raises(
                    asyncpg.CheckViolationError,
                    match="WEB_SEQUENCE_BINDING_IMMUTABLE",
                ):
                    async with connection.transaction():
                        await connection.execute(
                            "UPDATE qc_inspection_work_item_rule SET evidence_input_set_id=NULL "
                            "WHERE work_item_id=$1::uuid",
                            work_id,
                        )
            if failure == "database_binding_downgrade":
                import importlib
                from types import SimpleNamespace

                migration = importlib.import_module(
                    "services.api.migrations.versions.20261017_0141_h5_page_input_binding",
                )
                statements = []
                monkeypatch.setattr(migration, "op", SimpleNamespace(execute=statements.append))
                migration.downgrade()
                with pytest.raises(
                    asyncpg.CheckViolationError,
                    match="H5_PAGE_BINDING_DOWNGRADE_BLOCKED",
                ):
                    async with connection.transaction():
                        for statement in statements:
                            await connection.execute(statement)
            execution = uuid4()
            verdict = {
                "verdict_fail": "fail",
                "verdict_review": "manual_review",
                "verdict_warning": "warning",
            }.get(failure, "pass")
            await connection.execute(
                "INSERT INTO qc_rule_execution "
                "(id,task_id,rule_id,page_capture_id,agent_run_id,execution_status,"
                "judge_strategy,severity,result_summary,evidence_input_set_id) "
                "VALUES($1,$2::uuid,$3::uuid,$4::uuid,$5::uuid,$7,'hybrid','medium','原结论',$6)",
                execution,
                ids["task"],
                ids["rule"],
                ids["capture"],
                ids["run"],
                input_set.input_set_id,
                verdict,
            )
            if failure == "artifact_immutable":
                with pytest.raises(asyncpg.RestrictViolationError, match="immutable"):
                    async with connection.transaction():
                        await connection.execute(
                            "UPDATE qc_evidence_artifact SET checksum=$2 WHERE id=$1::uuid",
                            manifest.artifacts[0].artifact_id,
                            "sha256:" + "c" * 64,
                        )
            elif failure == "artifact_row_deleted":
                await connection.execute(
                    "UPDATE qc_evidence_artifact SET lifecycle_status='deleted',deleted_at=now() "
                    "WHERE id=$1::uuid",
                    manifest.artifacts[0].artifact_id,
                )
            if failure in {
                "member_input_missing",
                "same_consumer_unbound",
                "old_gap_terminal",
                "old_gap_inflight",
            }:
                await connection.execute(
                    "UPDATE qc_inspection_work_item_rule SET evidence_input_set_id=NULL "
                    "WHERE work_item_id=$1::uuid",
                    work_id,
                )
            elif failure == "execution_input_missing":
                await connection.execute(
                    "UPDATE qc_rule_execution SET evidence_input_set_id=NULL WHERE id=$1",
                    execution,
                )
            elif failure == "contract":
                await connection.execute(
                    "UPDATE qc_inspection_work_item_rule SET contract_hash='other-contract' "
                    "WHERE work_item_id=$1::uuid",
                    work_id,
                )
            elif failure == "ruleset":
                await connection.execute(
                    "UPDATE qc_inspection_work_item SET ruleset_snapshot_hash='other-snapshot' "
                    "WHERE id=$1::uuid",
                    work_id,
                )
            elif failure == "source_run":
                await connection.execute(
                    "UPDATE qc_capture SET agent_run_id=$2::uuid WHERE id=$1::uuid",
                    ids["capture"],
                    consumer,
                )
            elif failure == "capture_status":
                await connection.execute(
                    "UPDATE qc_capture SET capture_status='failed' WHERE id=$1::uuid",
                    ids["capture"],
                )
            elif failure in {"latest_fail", "latest_artifact", "latest_same"}:
                latest = build_evidence_manifest_reference(
                    task_id=manifest.task_id,
                    session_id=manifest.session_id,
                    capture_id=manifest.capture_id,
                    manifest_version=2,
                    artifacts=(replace(manifest.artifacts[0], checksum="sha256:" + "b" * 64),)
                    if failure == "latest_artifact"
                    else manifest.artifacts,
                    observation_packet_ids=(),
                    created_at=datetime.now(UTC),
                    quality_status=EvidenceQualityStatus.FAIL
                    if failure == "latest_fail"
                    else EvidenceQualityStatus.PASS,
                    quality_reason="quality_recheck",
                )
                await _save_manifest(connection, latest)
            if failure in {"race_manifest", "race_contract"}:
                from services.api.app.infrastructure.db import inspection_work_item_repository

                read_proofs = inspection_work_item_repository.verified_producer_proofs

                async def change_after_validation(conn, *, task_id):
                    proofs = await read_proofs(conn, task_id=task_id)
                    assert json.loads(proofs), "先证明预读合法，再注入回填前变化"
                    if failure == "race_contract":
                        await conn.execute(
                            "UPDATE qc_inspection_work_item_rule SET contract_hash='late-contract' "
                            "WHERE work_item_id=$1::uuid",
                            work_id,
                        )
                    else:
                        await _save_manifest(
                            conn,
                            build_evidence_manifest_reference(
                                task_id=manifest.task_id,
                                session_id=manifest.session_id,
                                capture_id=manifest.capture_id,
                                manifest_version=2,
                                artifacts=manifest.artifacts,
                                observation_packet_ids=(),
                                created_at=datetime.now(UTC),
                                quality_status=EvidenceQualityStatus.FAIL,
                                quality_reason="late_quality_failure",
                            ),
                        )
                    return proofs

                monkeypatch.setattr(
                    inspection_work_item_repository,
                    "verified_producer_proofs",
                    change_after_validation,
                )

            repository = PostgresInspectionWorkItemRepository(pool=pool)
            if str(failure).startswith("old_gap"):
                await connection.execute(
                    "UPDATE qc_task SET finalization_state='session_draining',"
                    "finalization_agent_run_id=$2::uuid,exploration_draining_at=now(),"
                    "session_draining_at=now() WHERE id=$1::uuid",
                    ids["task"],
                    ids["run"],
                )
                if failure == "old_gap_inflight":
                    await connection.execute(
                        "UPDATE qc_evidence_request SET status='running',completed_at=NULL "
                        "WHERE request_id=$1",
                        ids["request"],
                    )
                before = await connection.fetchval(
                    "SELECT to_jsonb(execution) FROM qc_rule_execution execution WHERE id=$1",
                    execution,
                )
                await repository.settle_stalled_work_for_closure(
                    task_id=ids["task"],
                    snapshot_generation_closed=True,
                )
                member = await connection.fetchrow(
                    "SELECT status,rule_execution_id,last_error_code "
                    "FROM qc_inspection_work_item_rule "
                    "WHERE work_item_id=$1::uuid",
                    work_id,
                )
                assert member["rule_execution_id"] is None
                assert member["status"] == (
                    "pending" if failure == "old_gap_inflight" else "evidence_insufficient"
                )
                assert (
                    await connection.fetchval(
                        "SELECT to_jsonb(execution) FROM qc_rule_execution execution WHERE id=$1",
                        execution,
                    )
                    == before
                )
                assert await connection.fetchval(
                    "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid",
                    work_id,
                ) == ("waiting_evidence" if failure == "old_gap_inflight" else "partial")
                from services.api.app.infrastructure.db.task_finalization_repository import (
                    _GATE_FACTS_SQL,
                )

                gate = await connection.fetchrow(
                    _GATE_FACTS_SQL, ids["task"], "completed_with_gaps"
                )
                assert gate["terminal_candidate_rule_count"] == 0
                assert gate["candidate_rule_count"] >= 1
                if failure == "old_gap_terminal":
                    assert member["last_error_code"] == "H5_RESULT_INPUT_CONTRACT_UNVERIFIED"
                return
            assert (
                await PostgresInspectionWorkItemRepository(pool=pool).reopen_waiting_evidence(
                    task_id=ids["task"],
                )
                == 0
            )
            member = await connection.fetchrow(
                "SELECT status,rule_execution_id,last_error_code FROM qc_inspection_work_item_rule "
                "WHERE work_item_id=$1::uuid",
                work_id,
            )
            permitted = failure in {
                None,
                "latest_same",
                "database_binding",
                "database_binding_immutable",
                "database_binding_downgrade",
                "same_consumer_bound",
                "verdict_fail",
                "verdict_review",
                "verdict_warning",
                "packet_bound",
                "packet_generic",
                "artifact_immutable",
            }
            assert member["rule_execution_id"] == (execution if permitted else None)
            assert member["status"] == ("written" if permitted else "pending")
            assert await connection.fetchval(
                "SELECT status FROM qc_inspection_work_item WHERE id=$1::uuid",
                work_id,
            ) == ("succeeded" if permitted else "waiting_evidence")
            # 不覆盖原执行、不补造 InputSet、不触发新调用，也不能把缺口算通过。
            assert (
                await connection.fetchval(
                    "SELECT execution_status FROM qc_rule_execution WHERE id=$1",
                    execution,
                )
                == verdict
            )
