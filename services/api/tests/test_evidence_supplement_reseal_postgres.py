"""真实 PostgreSQL 上验证补采重新封存输入集合（09-11 审查 P1-10）。

补采接上后，工作项被领取准备重判时要先封一个不可变输入集合，Attempt 身份只引用封存后的集合，
写回后的规则执行也指回它：Request → Supplement → InputSet → Attempt → RuleExecution 可追溯。
挂接补采后立即封存与领取时补封共用同一个集合，一次补采不会封出两份。

默认跳过；迁移后的隔离数据库可通过 ``ALIOTH_TEST_DATABASE_URL`` 显式启用。
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.inspection_work_items import (
    ClaimInspectionWorkItemCommand,
    InspectionWorkItemStatus,
    StartInspectionAttemptCommand,
    TransitionInspectionWorkItemCommand,
    build_inspection_attempt_input_hash,
)
from services.api.app.application.supplement_input_set_reseal import (
    SealedSupplementInputSet,
)
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.protocol import stable_protocol_hash
from services.api.app.infrastructure.db.evidence_supplement_reseal import (
    seal_supplement_input_set_on_connection,
)
from services.api.app.infrastructure.db.inspection_work_item_repository import (
    PostgresInspectionWorkItemRepository,
)
from services.api.tests.test_inspection_closure_settlement_postgres import _seed_task


async def test_supplement_is_resealed_before_rejudge_and_execution_points_back() -> None:
    database_url = _require_database_url()
    ids, seeded = _new_ids()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        original_manifest, result_manifest = await _seed_satisfied_supplement(
            connection, ids, seeded
        )
        repository = PostgresInspectionWorkItemRepository(pool=pool)
        context = await repository.get_rule_input_set_context(
            work_item_id=seeded["work"],
            rule_id=ids["rule"],
        )

        assert context is not None
        assert context.ruleset_snapshot_hash == "snapshot-1"

        lease_token = str(uuid4())

        lease = await repository.claim(command=_claim_command(ids), lease_token=lease_token)

        assert lease is not None
        assert len(lease.rule_evidence) == 1
        binding = lease.rule_evidence[0]
        assert binding.page_capture_id == seeded["result"]
        assert binding.evidence_input_set_id is not None
        input_set = await connection.fetchrow(
            """
            SELECT scope, eligibility_snapshot_id, source_agent_run_id::text AS source_run,
                   finalization_epoch, eligible_capture_ids::text AS eligible
            FROM qc_evidence_input_set
            WHERE input_set_id = $1
            """,
            binding.evidence_input_set_id,
        )
        assert input_set["scope"] == "page"
        assert input_set["eligibility_snapshot_id"] == (
            f"evidence-supplement:{seeded['supplement']}"
        )
        assert input_set["source_run"] == ids["run"]
        assert json.loads(input_set["eligible"]) == [seeded["result"]]
        member = await connection.fetchrow(
            """
            SELECT capture_id::text AS capture_id, capture_role, manifest_id
            FROM qc_evidence_input_set_member
            WHERE input_set_id = $1
            """,
            binding.evidence_input_set_id,
        )
        assert (member["capture_id"], member["capture_role"], member["manifest_id"]) == (
            seeded["result"],
            "probe_result_replay",
            result_manifest["manifest_id"],
        )
        assert (
            await connection.fetchval(
                "SELECT evidence_input_set_id FROM qc_evidence_supplement WHERE supplement_id = $1",
                seeded["supplement"],
            )
            == binding.evidence_input_set_id
        )
        # Attempt 身份只引用封存后的集合，与没有封存时的输入哈希不同
        assert lease.input_hash == build_inspection_attempt_input_hash(
            work_item_input_hash=lease.work_item_input_hash,
            rule_evidence=lease.rule_evidence,
        )
        assert lease.input_hash != build_inspection_attempt_input_hash(
            work_item_input_hash=lease.work_item_input_hash,
            rule_evidence=(
                replace(binding, evidence_input_set_id=None, evidence_input_set_hash=None),
            ),
        )
        assert original_manifest["manifest_id"] != result_manifest["manifest_id"]

        # 模拟 Judge：开始 Attempt、写回规则执行，再按租约同步
        await repository.start_attempt(
            command=StartInspectionAttemptCommand(
                work_item_id=lease.work_item_id,
                consumer_agent_run_id=ids["run"],
                attempt_no=lease.attempt_no,
                lease_token=lease.lease_token,
                input_hash=lease.input_hash,
                work_item_input_hash=lease.work_item_input_hash,
            )
        )
        await repository.transition(
            command=TransitionInspectionWorkItemCommand(
                work_item_id=lease.work_item_id,
                expected_status=InspectionWorkItemStatus.LEASED,
                target_status=InspectionWorkItemStatus.RUNNING,
                row_version=lease.row_version,
                lease_token=lease.lease_token,
            )
        )
        execution_id = await connection.fetchval(
            """
            INSERT INTO qc_rule_execution (
                task_id, rule_id, page_capture_id, agent_run_id,
                execution_status, judge_strategy
            ) VALUES ($1::uuid, $2::uuid, $3::uuid, $4::uuid, 'pass', 'hybrid')
            RETURNING id::text
            """,
            ids["task"],
            ids["rule"],
            seeded["original"],
            ids["run"],
        )

        synced = await repository.sync_formal_results(
            work_item_id=lease.work_item_id,
            lease_token=lease.lease_token,
        )

        assert synced.links[0].rule_execution_id == execution_id
        assert (
            await connection.fetchval(
                "SELECT evidence_input_set_id FROM qc_rule_execution WHERE id = $1::uuid",
                execution_id,
            )
            == binding.evidence_input_set_id
        )
        # 回写输入集合不改结论，不进结论历史
        assert (
            await connection.fetchval(
                """
                SELECT count(*) FROM qc_rule_execution_revision
                WHERE rule_execution_id = $1::uuid
                """,
                execution_id,
            )
            == 0
        )
    finally:
        await _cleanup(connection, ids)
        await pool.close()
        await connection.close()


async def test_attach_time_seal_is_reused_when_work_item_is_claimed() -> None:
    """挂接补采后立即封存，领取重判沿用同一个集合，不再另封一份。"""
    database_url = _require_database_url()
    ids, seeded = _new_ids()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_satisfied_supplement(connection, ids, seeded)
        repository = PostgresInspectionWorkItemRepository(pool=pool)

        sealed = await repository.seal_supplement_input_set(request_id=seeded["request"])
        sealed_again = await repository.seal_supplement_input_set(request_id=seeded["request"])
        lease = await repository.claim(command=_claim_command(ids), lease_token=str(uuid4()))

        assert sealed is not None
        assert sealed.created
        assert sealed_again == replace(sealed, created=False)
        assert lease is not None
        assert lease.rule_evidence[0].evidence_input_set_id == sealed.input_set_id
        assert await _input_set_ids(connection, ids["task"]) == [sealed.input_set_id]
    finally:
        await _cleanup(connection, ids)
        await pool.close()
        await connection.close()


async def test_claim_time_seal_is_reused_by_attach_time_seal() -> None:
    """领取时先封了，挂接后的封存拿到的还是那一个集合。"""
    database_url = _require_database_url()
    ids, seeded = _new_ids()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_satisfied_supplement(connection, ids, seeded)
        repository = PostgresInspectionWorkItemRepository(pool=pool)

        lease = await repository.claim(command=_claim_command(ids), lease_token=str(uuid4()))
        sealed = await repository.seal_supplement_input_set(request_id=seeded["request"])

        assert lease is not None
        binding = lease.rule_evidence[0]
        assert sealed == SealedSupplementInputSet(
            input_set_id=binding.evidence_input_set_id,
            input_set_hash=binding.evidence_input_set_hash,
            created=False,
        )
        assert await _input_set_ids(connection, ids["task"]) == [binding.evidence_input_set_id]
    finally:
        await _cleanup(connection, ids)
        await pool.close()
        await connection.close()


async def test_claim_waits_for_uncommitted_attach_time_seal_and_reuses_it() -> None:
    """挂接后的封存还没提交时，领取重判要等它提交，然后沿用同一个集合。"""
    database_url = _require_database_url()
    ids, seeded = _new_ids()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    claiming: asyncio.Task | None = None
    try:
        await _seed_satisfied_supplement(connection, ids, seeded)
        repository = PostgresInspectionWorkItemRepository(pool=pool)

        async with pool.acquire() as holder, holder.transaction():
            sealed = await seal_supplement_input_set_on_connection(
                holder, request_id=seeded["request"]
            )
            claiming = asyncio.create_task(
                repository.claim(command=_claim_command(ids), lease_token=str(uuid4()))
            )
            await asyncio.sleep(0.5)
            claim_waited = not claiming.done()
        lease = await claiming

        assert sealed is not None
        assert claim_waited
        assert lease is not None
        assert lease.rule_evidence[0].evidence_input_set_id == sealed[0]
        assert await _input_set_ids(connection, ids["task"]) == [sealed[0]]
    finally:
        if claiming is not None and not claiming.done():
            claiming.cancel()
        await _cleanup(connection, ids)
        await pool.close()
        await connection.close()


async def test_attach_time_seal_covers_rule_that_will_not_be_rejudged() -> None:
    """规则已经结算、不会再重判，挂接后照样封存，补采请求的标记与补采记录才能对上。"""
    database_url = _require_database_url()
    ids, seeded = _new_ids()
    connection = await asyncpg.connect(database_url)
    pool = await asyncpg.create_pool(database_url)
    try:
        await _seed_satisfied_supplement(connection, ids, seeded)
        await connection.execute(
            """
            UPDATE qc_inspection_work_item_rule
            SET status = 'evidence_insufficient'
            WHERE work_item_id = $1::uuid
            """,
            seeded["work"],
        )
        repository = PostgresInspectionWorkItemRepository(pool=pool)

        sealed = await repository.seal_supplement_input_set(request_id=seeded["request"])

        assert sealed is not None
        assert (
            await connection.fetchval(
                "SELECT evidence_input_set_id FROM qc_evidence_supplement WHERE supplement_id = $1",
                seeded["supplement"],
            )
            == sealed.input_set_id
        )
    finally:
        await _cleanup(connection, ids)
        await pool.close()
        await connection.close()


def _require_database_url() -> str:
    database_url = os.getenv("ALIOTH_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("需要显式设置隔离的 ALIOTH_TEST_DATABASE_URL")
    return database_url


def _new_ids() -> tuple[dict[str, str], dict[str, str]]:
    ids = {name: str(uuid4()) for name in ("tenant", "user", "ruleset", "rule", "task", "run")}
    seeded = {
        name: str(uuid4())
        for name in ("original", "result", "work", "first_attempt")
    }
    seeded["request"] = f"request-reseal-{uuid4().hex}"
    seeded["supplement"] = f"evidence-supplement:{uuid4().hex}"
    return ids, seeded


def _claim_command(ids: dict[str, str]) -> ClaimInspectionWorkItemCommand:
    return ClaimInspectionWorkItemCommand(
        task_id=ids["task"],
        consumer_agent_run_id=ids["run"],
        lease_owner="reseal-test",
        lease_seconds=180,
        session_dependency="snapshot",
    )


async def _input_set_ids(connection: asyncpg.Connection, task_id: str) -> list[str]:
    rows = await connection.fetch(
        "SELECT input_set_id FROM qc_evidence_input_set WHERE task_id = $1::uuid",
        task_id,
    )
    return [str(row["input_set_id"]) for row in rows]


async def _cleanup(connection: asyncpg.Connection, ids: dict[str, str]) -> None:
    await connection.execute("DELETE FROM qc_task WHERE id = $1::uuid", ids["task"])
    await connection.execute("DELETE FROM qc_rule WHERE id = $1::uuid", ids["rule"])
    await connection.execute("DELETE FROM qc_ruleset WHERE id = $1::uuid", ids["ruleset"])
    await connection.execute("DELETE FROM qc_user WHERE id = $1::uuid", ids["user"])
    await connection.execute("DELETE FROM qc_tenant WHERE id = $1::uuid", ids["tenant"])


async def _seed_satisfied_supplement(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    seeded: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """第一次判定证据不足提出补采、补采已挂上且请求已满足，工作项等下一次领取重判。"""
    await _seed_task(connection, ids)
    original_manifest = await _seed_capture_with_manifest(
        connection, ids, capture_id=seeded["original"], page_seq=1, role=None
    )
    result_manifest = await _seed_capture_with_manifest(
        connection,
        ids,
        capture_id=seeded["result"],
        page_seq=2,
        role="probe_result_replay",
    )
    await _seed_waiting_work(connection, ids, seeded, result_manifest["manifest_id"])
    # 补采行的等价证明、Probe 工作项外键链与本用例无关，只绕开外键写入补采本身
    async with connection.transaction():
        await connection.execute("SET LOCAL session_replication_role = replica")
        await connection.execute(
            """
            INSERT INTO qc_evidence_supplement (
                supplement_id, task_id, request_id, manifest_id, probe_work_id,
                restoration_attempt_id, equivalence_assessment_id, source_grade,
                origin, protocol_version, payload_hash
            ) VALUES (
                $1, $2::uuid, $3, $4, $5,
                'restoration:reseal-1', 'equivalence:reseal-1', 'A1',
                'replay_probe', 'evidence-supplement.v1', 'payload-hash-1'
            )
            """,
            seeded["supplement"],
            ids["task"],
            seeded["request"],
            result_manifest["manifest_id"],
            f"probe-work:{seeded['request']}",
        )
    return original_manifest, result_manifest


async def _seed_capture_with_manifest(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    *,
    capture_id: str,
    page_seq: int,
    role: str | None,
) -> dict[str, str]:
    if role is None:
        # 原始页面采集：页面行由触发器同步出通用采集行（角色占位 observation）
        await connection.execute(
            """
            INSERT INTO qc_page_capture (
                id, task_id, agent_run_id, page_seq, page_slug, url, page_type,
                capture_status
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5, 'https://example.com', 'home',
                'captured'
            )
            """,
            capture_id,
            ids["task"],
            ids["run"],
            page_seq,
            f"page-{page_seq}",
        )
    else:
        # 补采结果：只有通用采集行，页面序号回落到原采集
        await connection.execute(
            """
            INSERT INTO qc_capture (
                id, task_id, agent_run_id, surface_kind, capture_role,
                capture_status, captured_at, metadata
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, 'web', $4, 'captured', now(), '{}'::jsonb)
            """,
            capture_id,
            ids["task"],
            ids["run"],
            role,
        )
    manifest = build_evidence_manifest_reference(
        task_id=ids["task"],
        session_id=f"unbound:{ids['run']}",
        capture_id=capture_id,
        manifest_version=1,
        artifacts=(
            ArtifactReference(
                artifact_id=str(uuid4()),
                artifact_type="screenshot",
                role=role or "observation",
                storage_path=f"task/{capture_id}/shot.png",
                checksum="sha256:" + "c" * 64,
                size_bytes=3,
                content_type="image/png",
            ),
        ),
        quality_status=EvidenceQualityStatus.PASS,
        quality_reason="evidence_requirements_satisfied",
        created_at=datetime(2026, 9, 11, tzinfo=UTC),
    )
    payload = manifest.to_dict()
    await connection.execute(
        """
        INSERT INTO qc_evidence_manifest (
            manifest_id, manifest_version, task_id, session_id, capture_id,
            protocol_version, manifest_hash, payload_hash, quality_status,
            quality_reason, manifest_payload, created_at
        ) VALUES (
            $1, 1, $2::uuid, $3, $4::uuid,
            $5, $6, $7, 'pass',
            'evidence_requirements_satisfied', $8::jsonb, $9
        )
        """,
        manifest.manifest_id,
        ids["task"],
        manifest.session_id,
        capture_id,
        manifest.protocol_version,
        manifest.manifest_hash,
        stable_protocol_hash(payload),
        json.dumps(payload),
        manifest.created_at,
    )
    return {"manifest_id": manifest.manifest_id}


async def _seed_waiting_work(
    connection: asyncpg.Connection,
    ids: dict[str, str],
    seeded: dict[str, str],
    resolution_manifest_id: str,
) -> None:
    """第一次判定证据不足提出补采、补采已满足，工作项等下一次领取重判。"""
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item (
            id, task_id, page_capture_id, producer_agent_run_id,
            ruleset_id, ruleset_version, ruleset_snapshot_hash,
            batch_key, strategy, category, session_dependency, input_hash, status,
            attempt_count, max_attempts, control_mode, finalization_epoch
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid, $4::uuid,
            $5::uuid, 1, 'snapshot-1',
            'hybrid:visual:snapshot', 'hybrid', 'visual', 'snapshot', 'input-reseal', 'pending',
            1, 3, 'enforced', 0
        )
        """,
        seeded["work"],
        ids["task"],
        seeded["original"],
        ids["run"],
        ids["ruleset"],
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_work_item_rule (
            work_item_id, rule_id, rule_code, ordinal, contract_hash, status
        )
        VALUES ($1::uuid, $2::uuid, 'alioth_closure_1', 0, 'contract-1', 'pending')
        """,
        seeded["work"],
        ids["rule"],
    )
    await connection.execute(
        """
        INSERT INTO qc_inspection_attempt (
            id, work_item_id, consumer_agent_run_id, attempt_no,
            lease_token, status, input_hash
        )
        VALUES ($1::uuid, $2::uuid, $3::uuid, 1, gen_random_uuid(), 'judged', 'input-reseal')
        """,
        seeded["first_attempt"],
        seeded["work"],
        ids["run"],
    )
    await connection.execute(
        """
        INSERT INTO qc_evidence_request (
            request_id, requirement_hash, payload_hash, protocol_version,
            work_item_id, attempt_id, task_id, rule_id, rule_code,
            target_capture_id, session_dependency, replay_policy,
            required_equivalence, side_effect_policy, minimum_source_grade,
            request_payload, status, resolution_manifest_id, resolution_reason,
            expires_at, completed_at, finalization_epoch
        )
        VALUES (
            $7, 'requirement-reseal-1', 'payload-reseal-1', 'evidence-request.v1',
            $1::uuid, $2::uuid, $3::uuid, $4::uuid, 'alioth_closure_1',
            $5::uuid, 'snapshot', 'same_context_allowed',
            'logical_equivalent', 'read_only', 'B',
            '{}'::jsonb, 'satisfied', $6, '补采已满足',
            now() + interval '1 hour', now(), 0
        )
        """,
        seeded["work"],
        seeded["first_attempt"],
        ids["task"],
        ids["rule"],
        seeded["original"],
        resolution_manifest_id,
        seeded["request"],
    )
