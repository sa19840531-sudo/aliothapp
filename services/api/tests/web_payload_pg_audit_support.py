"""使用本轮真实 Chrome 实体验证载荷围栏；所有变更仅在随机测试 Task 内。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from services.api.app.application.evidence_exit_gate import EvidenceExitGateBlockedError
from services.api.app.application.web_exploration_workflow import web_capture_profile
from services.api.app.domain.interaction import (
    ActionIntent,
    InteractionOperation,
    SurfaceKind,
    build_action_intent_hash,
)
from services.api.app.infrastructure.db.evidence_exit_gate_repository import (
    PostgresEvidenceExitGate,
    PostgresEvidenceExitRequirementRepository,
)
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.session_command_gate import PostgresSessionCommandGate
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.web_payload_readiness_repository import (
    PostgresWebPayloadReadinessRepository,
    _ConnectionPool,
)


async def audit_real_payload_fences(connection, pool, ids, store, ready_capture):
    repository = PostgresWebPayloadReadinessRepository(pool=pool, artifact_store=store)
    borrowed = PostgresWebPayloadReadinessRepository(
        pool=_ConnectionPool(connection), artifact_store=store
    )
    task, run, session = (ids[key] for key in ("task", "run", "session"))
    scope = dict(task_id=task, agent_run_id=run, session_id=session, capture_id=ready_capture)
    with pytest.raises(ValueError, match="CONTEXT_REJECTED"):
        await repository.finalize_classified_capture(**(scope | {"agent_run_id": str(uuid4())}))
    for mutation, parameters in (
        (
            "UPDATE qc_task SET task_config=jsonb_set(task_config,'{workflow_version}','\"v1\"') WHERE id=$1::uuid",
            (task,),
        ),
        (
            "UPDATE qc_capture SET metadata=jsonb_set(metadata,'{finalization_epoch}','99') WHERE id=$1::uuid",
            (ready_capture,),
        ),
        (
            "UPDATE qc_agent_run SET output_context=jsonb_set(COALESCE(output_context,'{}'::jsonb),'{cancel_requested}','true') WHERE id=$1::uuid",
            (run,),
        ),
    ):
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(mutation, *parameters)
            with pytest.raises(ValueError, match="CONTEXT_REJECTED"):
                await borrowed.finalize_classified_capture(**scope)
        finally:
            await transaction.rollback()

    # 可信首跳必须同时满足完整原意图、首动作序号和真实双采身份。
    from services.api.app.infrastructure.db.web_evidence_sequence_repository import (
        _trusted_bootstrap_pair,
    )

    action = await connection.fetchrow(
        "SELECT * FROM qc_action_execution WHERE task_id=$1::uuid AND command_sequence=1", task
    )
    before = await connection.fetchrow(
        "SELECT * FROM qc_capture WHERE id=$1::uuid", action["source_capture_id"]
    )
    after = await connection.fetchrow(
        "SELECT * FROM qc_capture WHERE id=$1::uuid", action["result_capture_id"]
    )
    assert await _trusted_bootstrap_pair(connection, task, run, session, 0, before, after)
    assert not await _trusted_bootstrap_pair(
        connection, task, str(uuid4()), session, 0, before, after
    )
    original_intent = ActionIntent.from_dict(json.loads(action["intent_payload"]))
    for false_intent in (
        replace(original_intent, source_capture_id=f"web-entry:{uuid4()}"),
        replace(original_intent, action_type=InteractionOperation.CLICK),
    ):
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(
                "UPDATE qc_action_execution SET intent_payload=$2::jsonb,intent_hash=$3 WHERE action_execution_id=$1",
                action["action_execution_id"],
                json.dumps(false_intent.to_dict()),
                build_action_intent_hash(false_intent),
            )
            assert not await _trusted_bootstrap_pair(
                connection, task, run, session, 0, before, after
            )
        finally:
            await transaction.rollback()
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(
            "UPDATE qc_action_execution SET command_sequence=99 WHERE action_execution_id=$1",
            action["action_execution_id"],
        )
        assert not await _trusted_bootstrap_pair(connection, task, run, session, 0, before, after)
    finally:
        await transaction.rollback()

    gate = PostgresSessionCommandGate(
        repository=PostgresSurfaceSessionRepository(pool=pool),
        lease_owner="t10-payload-audit",
        session_owner="web-v2-workflow",
    )
    async with gate.hold(task_id=task, session_id=session, surface_kind=SurfaceKind.WEB) as permit:
        capture = await connection.fetchval(
            "SELECT current_capture_id::text FROM qc_surface_session WHERE task_id=$1::uuid AND session_id=$2",
            task,
            session,
        )
        # 此命令只核验旧游标，没有新 Capture；释放时不能冒称旧捕获属于新命令序号。
        intent = ActionIntent(
            task_id=task,
            session_id=session,
            intent_id=f"t10-audit:{uuid4()}",
            idempotency_key=f"t10-audit:{uuid4()}",
            surface_kind=SurfaceKind.WEB,
            source_capture_id=capture,
            action_type=InteractionOperation.CLICK,
            semantic_target="仅核验门禁，不送达浏览器",
            purpose="本轮隔离负向",
            capture_profile=web_capture_profile("t10-audit"),
            deadline=datetime.now(UTC) + timedelta(seconds=60),
        )
        for changed in (
            replace(permit, lease_token=str(uuid4())),
            replace(permit, finalization_epoch=1),
        ):
            with pytest.raises(ValueError, match="LEASE_REJECTED"):
                await repository.preserve_and_reserve(intent=intent, permit=changed)
        historical_capture = await connection.fetchval(
            "SELECT id::text FROM qc_capture WHERE task_id=$1::uuid AND id<>$2::uuid AND capture_role='after_action' ORDER BY captured_at LIMIT 1",
            task,
            capture,
        )
        assert historical_capture
        with pytest.raises(ValueError, match="LEASE_REJECTED"):
            await repository.preserve_and_reserve(
                intent=replace(intent, source_capture_id=historical_capture), permit=permit
            )
        decision = await repository.preserve_and_reserve(intent=intent, permit=permit)
        assert decision.allow_leave
        from services.api.app.domain.exploration import EvidenceExitReservationStatus

        await PostgresEvidenceExitGate(pool=pool).finalize(
            decision=decision, status=EvidenceExitReservationStatus.CANCELLED
        )

        # 并发提交缺少 DOM 的新版本后，旧 PASS 不得掩盖最新载荷缺口。
        from services.api.app.domain.evidence import (
            EvidenceQualityStatus,
            build_evidence_manifest_reference,
        )

        manifests = PostgresEvidenceManifestRepository(pool=pool)
        original_manifest = await manifests.get_latest_for_capture(task_id=task, capture_id=capture)
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(
                "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task}"
            )
            queued_manifest = asyncio.create_task(
                repository.preserve_and_reserve(
                    intent=replace(intent, intent_id=f"t10-audit:{uuid4()}"), permit=permit
                )
            )
            pending = build_evidence_manifest_reference(
                task_id=task,
                session_id=session,
                capture_id=capture,
                manifest_version=original_manifest.manifest_version + 1,
                artifacts=tuple(
                    item for item in original_manifest.artifacts if item.artifact_type != "dom"
                ),
                observation_packet_ids=original_manifest.observation_packet_ids,
                quality_status=EvidenceQualityStatus.PENDING,
                quality_reason="t10_concurrent_payload",
                created_at=datetime.now(UTC),
            )
            await PostgresEvidenceManifestRepository(pool=_ConnectionPool(connection)).add(
                manifest=pending
            )
            assert not queued_manifest.done()
            await transaction.commit()
        except BaseException:
            await transaction.rollback()
            raise
        with pytest.raises(EvidenceExitGateBlockedError):
            await asyncio.wait_for(queued_manifest, 30)
        latest = await manifests.get_latest_for_capture(task_id=task, capture_id=capture)
        assert latest.quality_status is EvidenceQualityStatus.FAIL
        assert latest.quality_reason == "WEB_PAYLOAD_REQUIRED_ARTIFACT_MISSING"
        assert (
            latest.manifest_version > pending.manifest_version > original_manifest.manifest_version
        )
        assert (
            await manifests.get(manifest_id=original_manifest.manifest_id)
        ).quality_status is EvidenceQualityStatus.PASS
        # 追加原真实对象集合的新 pending 版本用于后续独立负例；不覆盖任何历史版本。
        restored = build_evidence_manifest_reference(
            task_id=task,
            session_id=session,
            capture_id=capture,
            manifest_version=latest.manifest_version + 1,
            artifacts=original_manifest.artifacts,
            observation_packet_ids=original_manifest.observation_packet_ids,
            quality_status=EvidenceQualityStatus.PENDING,
            quality_reason="t10_restore_original_objects",
            created_at=datetime.now(UTC),
        )
        await manifests.add(manifest=restored)

        # 真实文件丢失/错字节；恢复原始字节，不制造新证据身份。
        artifact = await connection.fetchrow(
            "SELECT storage_path FROM qc_evidence_artifact WHERE page_capture_id=$1::uuid AND artifact_type='dom'",
            capture,
        )
        path = store._resolve_path(artifact["storage_path"])
        original = path.read_bytes()
        for damaged in (None, b"changed-current-object"):
            try:
                if damaged is None:
                    path.unlink()
                else:
                    path.write_bytes(damaged)
                with pytest.raises(EvidenceExitGateBlockedError):
                    await repository.preserve_and_reserve(
                        intent=replace(intent, intent_id=f"t10-audit:{uuid4()}"), permit=permit
                    )
                latest = await PostgresEvidenceManifestRepository(pool=pool).get_latest_for_capture(
                    task_id=task, capture_id=capture
                )
                assert latest.quality_status.value == "fail"
            finally:
                path.write_bytes(original)

        # 新 pending Manifest 与并发新增业务需求均不能借上一版 PASS 离场。
        from services.api.app.domain.exploration.evidence_exit_requirements import (
            EvidenceExitRequirementSet,
            EvidenceExitRequirementStatus,
            build_active_session_evidence_requirement,
        )
        from services.api.app.domain.exploration.web_payload_requirements import (
            rebuild_payload_requirement_set,
        )

        body = await connection.fetchval(
            "SELECT requirement_payload FROM qc_evidence_exit_requirement_set WHERE capture_id=$1::uuid ORDER BY revision DESC LIMIT 1",
            capture,
        )
        current = EvidenceExitRequirementSet.from_dict(json.loads(body))
        with pytest.raises(ValueError, match="SCOPED_REPOSITORY"):
            await PostgresEvidenceExitRequirementRepository(pool=pool).add(requirement_set=current)
        rule = str(uuid4())
        await connection.execute(
            "INSERT INTO qc_rule(id,ruleset_id,rule_code,name,category,executor_type,severity,status,check_point,input_contract,applicable_page_types) "
            "VALUES($1::uuid,$2::uuid,'t10-payload-stronger','真实更强规则','visual','llm','medium','enabled','要求更多当前截图',"
            '\'{"target_clients":["WEB"],"required_artifacts":["screenshot","dom"],"quality_gate":{"min_screenshot_count":2}}\', \'["content_detail"]\')',
            rule,
            ids["ruleset"],
        )
        stronger = build_active_session_evidence_requirement(
            task_id=task,
            capture_id=capture,
            rule_id=rule,
            rule_code="t10-payload-stronger",
            required_artifact_types=("screenshot", "dom"),
            required_capture_roles=("after_action",),
            min_screenshot_count=2,
            status=EvidenceExitRequirementStatus.PENDING,
        )
        try:
            classified_before = await PostgresEvidenceManifestRepository(
                pool=pool
            ).get_latest_for_capture(task_id=task, capture_id=ready_capture)
            classified_after = await repository.finalize_classified_capture(**scope)
            assert classified_after.quality_status.value == "fail"
            assert classified_after.manifest_version > classified_before.manifest_version
            assert classified_before.quality_status.value == "pass"
            transaction = connection.transaction()
            await transaction.start()
            try:
                await connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext($1))", f"task-finalization:{task}"
                )
                queued = asyncio.create_task(
                    repository.preserve_and_reserve(
                        intent=replace(intent, intent_id=f"t10-audit:{uuid4()}"), permit=permit
                    )
                )
                newer = rebuild_payload_requirement_set(
                    current,
                    requirements=(*current.requirements, stronger),
                    created_at=datetime.now(UTC),
                )
                await borrowed._append(connection, previous=current, current=newer)
                assert not queued.done()
                await transaction.commit()
            except BaseException:
                await transaction.rollback()
                raise
            with pytest.raises(EvidenceExitGateBlockedError):
                await asyncio.wait_for(queued, 30)
            payload = await connection.fetchval(
                "SELECT requirement_payload FROM qc_evidence_exit_requirement_set WHERE capture_id=$1::uuid ORDER BY revision DESC LIMIT 1",
                capture,
            )
            persisted = EvidenceExitRequirementSet.from_dict(json.loads(payload))
            assert (
                next(
                    item
                    for item in persisted.requirements
                    if item.requirement_id == stronger.requirement_id
                ).status
                is EvidenceExitRequirementStatus.PENDING
            )
        finally:
            await connection.execute("DELETE FROM qc_rule WHERE id=$1::uuid", rule)

    # 固定 InputSet 成员继续绑定原始 Manifest，不被较新技术裁决换版本。
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM qc_evidence_input_set_member member JOIN qc_evidence_input_set inputs USING(input_set_id) "
            "LEFT JOIN qc_evidence_manifest manifest ON manifest.manifest_id=member.manifest_id AND manifest.manifest_hash=member.manifest_hash "
            "AND manifest.manifest_version=member.manifest_version WHERE inputs.task_id=$1::uuid AND manifest.manifest_id IS NULL",
            task,
        )
        == 0
    )
    from services.api.app.application.task_finalization import TerminateTaskFinalizationCommand
    from services.api.app.domain.task import TaskExecutionStatus
    from services.api.app.infrastructure.db.task_finalization_repository import (
        PostgresTaskFinalizationRepository,
    )

    await connection.execute(
        "UPDATE qc_agent_run SET status='done',finished_at=now(),stop_reason='completed' WHERE task_id=$1::uuid",
        task,
    )
    await connection.execute(
        "UPDATE qc_task SET finalization_state='session_draining',exploration_draining_at=now(),session_draining_at=now(),"
        "finalization_agent_run_id=$2::uuid,finalization_row_version=2 WHERE id=$1::uuid",
        task,
        run,
    )
    await PostgresTaskFinalizationRepository(pool=pool).terminate(
        command=TerminateTaskFinalizationCommand(
            task_id=task,
            agent_run_id=run,
            expected_finalization_epoch=0,
            expected_row_version=2,
            execution_status=TaskExecutionStatus.NEEDS_REVIEW,
            reason_code="T10_PAYLOAD_AUDIT",
        )
    )
    before_count = await connection.fetchval(
        "SELECT count(*) FROM qc_evidence_manifest WHERE task_id=$1::uuid", task
    )
    with pytest.raises(ValueError, match="CONTEXT_REJECTED"):
        await repository.finalize_classified_capture(**scope)
    with pytest.raises(ValueError, match="CONTEXT_REJECTED"):
        await repository.preserve_and_reserve(intent=intent, permit=permit)
    assert (
        await connection.fetchval(
            "SELECT count(*) FROM qc_evidence_manifest WHERE task_id=$1::uuid", task
        )
        == before_count
    )
