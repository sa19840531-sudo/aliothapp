"""真实文件与隔离PG验证提前校验、并发收口及损坏拒绝，不访问原站。"""

import asyncio
import os
from dataclasses import fields, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.app_evidence_manifest import AppEvidenceManifestFinalizer
from services.api.app.application.h5_evidence_manifest import H5EvidenceManifestFinalizer
from services.api.app.application.h5_targeted_recapture import H5TargetedRecaptureOutcome
from services.api.app.application.page_capture_persistence import PageCaptureRecoveryService
from services.api.app.domain.evidence import (
    ArtifactReference,
    EvidenceQualityStatus,
    build_evidence_manifest_reference,
)
from services.api.app.domain.inspection import build_evidence_request
from services.api.app.domain.interaction import SurfaceKind, SurfaceSessionSpec
from services.api.app.infrastructure.db.evidence_manifest_repository import (
    PostgresEvidenceManifestRepository,
)
from services.api.app.infrastructure.db.page_capture_repository import PostgresPageCaptureRepository
from services.api.app.infrastructure.db.surface_session_repository import (
    PostgresSurfaceSessionRepository,
)
from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_app_postgres_integration import (
    _upgrade_isolated_postgres,  # noqa: F401 - 显式隔离库迁移
    _valid_png,
)
from services.api.tests.test_h5_recapture_execution_limits import _consumer
from services.api.tests.test_h5_session_journey import _gate
from services.api.tests.test_h5_targeted_recapture import NOW

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"),
    reason="必须显式提供隔离数据库",
)


async def _seed_h5_task(connection, ids):
    """并发读需要提交种子，名称不能沿用回滚夹具的固定值。"""
    async with connection.transaction():
        await connection.execute(
            "INSERT INTO qc_tenant(id,tenant_key,display_name) VALUES($1::uuid,$2,$2)",
            ids["tenant"],
            f"manifest-{ids['tenant']}",
        )
        await connection.execute(
            """INSERT INTO qc_user(id,username,full_name,password_hash,role,tenant_id)
               VALUES($1::uuid,$2,'隔离校验','not-a-secret','平台管理员',$3::uuid)""",
            ids["user"],
            f"manifest-{ids['user']}",
            ids["tenant"],
        )
        await connection.execute(
            """INSERT INTO qc_ruleset(id,ruleset_code,name,version,status,snapshot_hash,
                   rule_count,source_type)
               VALUES($1::uuid,$2,'隔离校验',1,'enabled','manifest-test',0,'manual')""",
            ids["ruleset"],
            f"manifest-{ids['ruleset']}",
        )
        await connection.execute(
            """INSERT INTO qc_task(id,target_url,ruleset_id,status,submitter_id,tenant_id,
                   target_platform)
               VALUES($1::uuid,'https://offline.invalid',$2::uuid,'running',$3::uuid,$4::uuid,
                      'h5')""",
            ids["task"],
            ids["ruleset"],
            ids["user"],
            ids["tenant"],
        )
        await connection.execute(
            """INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status)
               VALUES($1::uuid,$2::uuid,'main','隔离校验','running')""",
            ids["run"],
            ids["task"],
        )


@pytest.mark.parametrize("damage", [None, "checksum", "size", "missing"])
async def test_early_consumer_and_closeout_share_real_integrity_adjudication(tmp_path, damage):
    url = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.getenv("ALIOTH_DATABASE_URL"),
    )
    pool = await asyncpg.create_pool(url, min_size=1, max_size=3)
    try:
        ids = {key: str(uuid4()) for key in ("tenant", "user", "ruleset", "rule", "task", "run")}
        capture_id = str(uuid4())
        session_id = f"h5:{ids['run']}"
        store = LocalEvidenceArtifactStore(root=tmp_path / "evidence")
        content = _valid_png()
        artifact = await store.store(
            task_id=ids["task"],
            page_capture_id=capture_id,
            artifact_id=str(uuid4()),
            artifact_type="screenshot",
            content=content,
        )
        async with pool.acquire() as connection:
            await _seed_h5_task(connection, ids)
            await PostgresSurfaceSessionRepository(pool=pool).register_active(
                spec=SurfaceSessionSpec(
                    task_id=ids["task"],
                    session_id=session_id,
                    surface_kind=SurfaceKind.WEB,
                ),
                session_owner="h5-workflow",
                primary_surface_ref=f"web-primary:{session_id}",
            )
            await connection.execute(
                """INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,
                       page_type,capture_status,surface_session_id,capture_role,
                       session_command_sequence,captured_at,metadata)
                   VALUES($1::uuid,$2::uuid,$3::uuid,1,'source','https://offline.invalid',
                          'home','captured',$4,'after_action',1,now(),'{"finalization_epoch":0}')""",
                capture_id,
                ids["task"],
                ids["run"],
                session_id,
            )
            await connection.execute(
                """INSERT INTO qc_evidence_artifact(id,task_id,page_capture_id,artifact_type,
                       source,storage_path,content_type,checksum,size_bytes,metadata)
                   VALUES($1::uuid,$2::uuid,$3::uuid,'screenshot','baseline',
                          $4,$5,$6,$7,'{}')""",
                artifact.artifact_id,
                ids["task"],
                capture_id,
                artifact.storage_path,
                artifact.content_type,
                artifact.checksum,
                artifact.size_bytes,
            )
        manifests = PostgresEvidenceManifestRepository(pool=pool)
        pending = build_evidence_manifest_reference(
            task_id=ids["task"],
            session_id=session_id,
            capture_id=capture_id,
            manifest_version=1,
            created_at=datetime.now(UTC),
            quality_status=EvidenceQualityStatus.PENDING,
            quality_reason="page_capture_committed",
            artifacts=(
                ArtifactReference(
                    artifact_id=artifact.artifact_id,
                    artifact_type="screenshot",
                    role="after_action",
                    storage_path=artifact.storage_path,
                    checksum=artifact.checksum,
                    size_bytes=artifact.size_bytes,
                    content_type=artifact.content_type,
                ),
            ),
        )
        await manifests.add(manifest=pending)
        barrier = asyncio.Event()
        reads = 0

        class ObservedStore:
            async def read(self, path):
                nonlocal reads
                reads += 1
                if reads >= 2:
                    barrier.set()
                await asyncio.wait_for(barrier.wait(), timeout=5)
                if damage == "missing":
                    raise FileNotFoundError("隔离注入的缺失制品")
                raw = await store.read(path)
                if damage == "checksum":
                    return b"x" * len(raw)
                return raw[:-1] if damage == "size" else raw

        observed = ObservedStore()
        finalizer = H5EvidenceManifestFinalizer(
            repository=manifests,
            finalizer=AppEvidenceManifestFinalizer(repository=manifests, artifact_store=observed),
        )
        runtime = SimpleNamespace(
            manifest_finalizer=finalizer,
            captures=PageCaptureRecoveryService(
                repository=PostgresPageCaptureRepository(pool=pool),
                artifact_store=observed,
            ),
        )
        consumer, requests, kwargs = _consumer(runtime, clock=lambda: NOW)
        lease = requests.claim_next.return_value.lease
        request_values = {
            field.name: getattr(lease.request, field.name)
            for field in fields(lease.request)
            if field.name not in {"request_id", "requirement_hash", "protocol_version"}
        }
        request_values.update(task_id=ids["task"], target_capture_id=capture_id)
        requests.claim_next.return_value = replace(
            requests.claim_next.return_value,
            lease=replace(
                lease,
                request=build_evidence_request(**request_values),
            ),
        )
        kwargs.update(task_id=ids["task"], agent_run_id=ids["run"])
        expected = H5TargetedRecaptureOutcome(disposition="probe_published")
        consumer._consume_in_journey = AsyncMock(return_value=expected)
        consumer._session_command_gate = _gate()
        _, result = await asyncio.gather(
            finalizer.finalize_capture(task_id=ids["task"], capture_id=capture_id),
            consumer.consume_next(**kwargs),
        )
        latest = await manifests.get_latest_for_capture(task_id=ids["task"], capture_id=capture_id)
        assert latest.manifest_version == 2
        assert latest.artifacts == pending.artifacts
        assert latest.quality_status is (
            EvidenceQualityStatus.PASS if damage is None else EvidenceQualityStatus.FAIL
        )
        if damage is None:
            assert result is expected
            consumer._consume_in_journey.assert_awaited_once()
        else:
            assert result is not expected
            consumer._consume_in_journey.assert_not_awaited()
        async with pool.acquire() as connection:
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_evidence_manifest WHERE capture_id=$1::uuid",
                    capture_id,
                )
                == 2
            )
        assert await store.read(artifact.storage_path) == content
    finally:
        await pool.close()
