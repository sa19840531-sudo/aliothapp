"""补充全页截图阶段的 PostgreSQL claim 在终态后不可再次消费。"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from services.api.app.application.surface_sessions import SessionCommandPermit
from services.api.app.domain.interaction import SurfaceKind
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 -- 注册隔离库迁移 fixture。
)
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case

_requires_postgres = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需要显式隔离 ALIOTH_TEST_DATABASE_URL 和 ALIOTH_DATABASE_URL 双 DSN",
)


@pytest.mark.asyncio
@_requires_postgres
async def test_long_page_limit_audit_keeps_primary_and_claim_context_immutable():
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    async with _database() as (control, pool, _other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids)
        permit, command = await _permit_and_command(control, ids)
        repository = PostgresWebFullPageSupplementalClaimRepository(pool=pool)
        phase_id = await repository.claim(command=command, permit=permit)
        before = await control.fetchrow(
            "SELECT context_payload,context_payload_hash FROM qc_surface_phase_run "
            "WHERE phase_run_id=$1",
            phase_id,
        )
        await repository.finish(
            phase_id=phase_id,
            status="failed",
            reason_code="WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED",
            artifact_ids=(),
            stabilization_audit={"document_height": 40000, "scroll_steps": 1},
        )
        after = await control.fetchrow(
            "SELECT context_payload,context_payload_hash FROM qc_surface_phase_run "
            "WHERE phase_run_id=$1",
            phase_id,
        )
        assert before == after
        assert (
            str(
                await control.fetchval(
                    "SELECT current_capture_id FROM qc_surface_session WHERE session_id=$1",
                    ids["session"],
                )
            )
            == ids["capture"]
        )
        audit = await control.fetchval(
            "SELECT decision_payload FROM qc_surface_phase_step WHERE phase_run_id=$1",
            phase_id,
        )
        assert json.loads(audit)["document_height"] == 40000
        assert (await repository.read_terminal(command=command)).reason_code == (
            "WEB_FULL_PAGE_SCREENSHOT_LIMIT_EXCEEDED"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "failed", "completed"])
@_requires_postgres
async def test_one_full_page_claim_survives_concurrent_workers_and_terminal_states(terminal):
    module_name = "services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository"
    assert importlib.util.find_spec(module_name) is not None, "缺少持久全页补充 claim 仓储"
    repository_type = importlib.import_module(
        module_name
    ).PostgresWebFullPageSupplementalClaimRepository
    async with _database() as (control, pool, other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids)
        lease = str(uuid4())
        await control.execute(
            "UPDATE qc_surface_session SET command_sequence=7,lease_owner='full-page-test',"
            "lease_token=$1::uuid,lease_expires_at=now()+interval '10 minutes',"
            "current_capture_id=$2::uuid WHERE session_id=$3",
            lease,
            ids["capture"],
            ids["session"],
        )
        permit = SessionCommandPermit(
            task_id=ids["task"],
            session_id=ids["session"],
            surface_kind=SurfaceKind.WEB,
            lease_owner="full-page-test",
            lease_token=lease,
            command_sequence=7,
            finalization_epoch=0,
            lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
            current_capture_id=ids["capture"],
        )
        command = SimpleNamespace(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=ids["capture"],
            source_url="https://example.test/",
        )
        first, second = repository_type(pool=pool), repository_type(pool=other)
        for changes in (
            {"task_id": str(uuid4())},
            {"session_id": "web:" + str(uuid4())},
            {"lease_token": str(uuid4())},
            {"lease_owner": "stale-worker"},
            {"command_sequence": 6},
            {"finalization_epoch": 1},
            {"current_capture_id": str(uuid4())},
        ):
            assert await first.claim(command=command, permit=replace(permit, **changes)) is None
        claimed = await asyncio.gather(
            first.claim(command=command, permit=permit),
            second.claim(command=command, permit=permit),
        )
        assert sum(value is not None for value in claimed) == 1
        phase_id = next(value for value in claimed if value is not None)
        if terminal is not None:
            artifact_ids = ()
            if terminal == "completed":
                _, artifact_id = await _make_supplemental_child(
                    control,
                    ids,
                    child_epoch=0,
                    child_sequence=7,
                )
                artifact_ids = (artifact_id,)
            await first.finish(
                phase_id=phase_id,
                status=terminal,
                reason_code="WEB_FULL_PAGE_SCREENSHOT_PERSIST_FAILED"
                if terminal == "failed"
                else "WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                artifact_ids=artifact_ids,
            )
        permit.command_sequence = 8
        assert await repository_type(pool=other).claim(command=command, permit=permit) is None
        rows = await control.fetch(
            "SELECT status,phase_key,phase_protocol_version FROM qc_surface_phase_run "
            "WHERE task_id=$1::uuid",
            ids["task"],
        )
        assert [dict(row) for row in rows] == [
            {
                "status": terminal or "running",
                "phase_key": "web_full_page_supplemental",
                "phase_protocol_version": "web-full-page-supplemental.v1",
            }
        ]
        await control.execute(
            "DELETE FROM qc_surface_phase_run WHERE task_id=$1::uuid", ids["task"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["probe_before", "supplemental"])
@_requires_postgres
async def test_full_page_claim_rejects_source_that_is_not_ready_primary(source_kind: str) -> None:
    """移除 ready-primary 准入会让探针或补图继续触发一张新的全页图。"""
    module_name = "services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository"
    repository_type = importlib.import_module(
        module_name
    ).PostgresWebFullPageSupplementalClaimRepository
    async with _database() as (control, pool, _other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids)
        await control.execute(
            "UPDATE qc_capture SET capture_role=$1 WHERE id=$2::uuid", source_kind, ids["capture"]
        )
        permit, command = await _permit_and_command(control, ids)

        repository = repository_type(pool=pool)
        assert await repository.claim(command=command, permit=permit) is None
        terminal = await repository.read_terminal(command=command)
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.reason_code == "WEB_FULL_PAGE_SCREENSHOT_CLAIM_REJECTED"


@pytest.mark.asyncio
@_requires_postgres
async def test_full_page_claim_rejects_command_url_that_differs_from_ready_source() -> None:
    """移除来源 URL 绑定会把已切换页面的长图错误挂到旧 Capture 下。"""
    module_name = "services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository"
    repository_type = importlib.import_module(
        module_name
    ).PostgresWebFullPageSupplementalClaimRepository
    async with _database() as (control, pool, _other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids)
        permit, command = await _permit_and_command(
            control, ids, source_url="https://example.test/changed"
        )

        repository = repository_type(pool=pool)
        assert await repository.claim(command=command, permit=permit) is None
        terminal = await repository.read_terminal(command=command)
        assert terminal is not None
        assert terminal.status == "failed"
        assert terminal.reason_code == "WEB_FULL_PAGE_SCREENSHOT_CLAIM_REJECTED"


@pytest.mark.asyncio
@_requires_postgres
async def test_full_page_claim_accepts_ready_after_action_source() -> None:
    """删除 after_action 会让真实 readiness 主证据无法补采全页截图。"""
    module_name = "services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository"
    repository_type = importlib.import_module(
        module_name
    ).PostgresWebFullPageSupplementalClaimRepository
    async with _database() as (control, pool, _other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids, capture_role="after_action")
        permit, command = await _permit_and_command(control, ids)

        assert await repository_type(pool=pool).claim(command=command, permit=permit) is not None


@pytest.mark.asyncio
@_requires_postgres
async def test_segmented_finish_binds_ordered_pngs_and_manifest_in_real_postgres() -> None:
    """分段顺序或清单引用漂移时，真实 PG 必须保持 phase running。"""

    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    async with _database() as (control, pool, _other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids)
        permit, command = await _permit_and_command(control, ids)
        repository = PostgresWebFullPageSupplementalClaimRepository(pool=pool)
        phase_id = await repository.claim(command=command, permit=permit)
        assert phase_id is not None
        _, screenshot_ids, manifest_id = await _make_segmented_supplemental_child(
            control,
            ids,
            child_epoch=0,
            child_sequence=7,
        )

        with pytest.raises(
            RuntimeError,
            match="WEB_FULL_PAGE_SUPPLEMENTAL_FINISH_SCOPE_INVALID",
        ):
            await repository.finish(
                phase_id=phase_id,
                status="completed",
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                artifact_ids=(*reversed(screenshot_ids), manifest_id),
            )

        await repository.finish(
            phase_id=phase_id,
            status="completed",
            reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
            artifact_ids=(*screenshot_ids, manifest_id),
        )
        terminal = await repository.read_terminal(command=command)
        assert terminal is not None
        assert terminal.screenshot_artifact_ids == screenshot_ids
        assert terminal.manifest_artifact_id == manifest_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_epoch", "child_sequence", "purpose"),
    [
        pytest.param(1, 7, "web_direct_full_page", id="wrong-epoch"),
        pytest.param(0, 8, "web_direct_full_page", id="wrong-sequence"),
        pytest.param(0, 7, None, id="missing-purpose"),
        pytest.param(0, 7, "viewport", id="viewport-is-not-full-page"),
    ],
)
@_requires_postgres
async def test_finish_and_reconciliation_reject_child_from_wrong_phase_scope(
    child_epoch: int,
    child_sequence: int,
    purpose: str | None,
) -> None:
    """严格阶段范围不能把旧 epoch 或其它命令序列的直接子图绑定为成功。"""

    module_name = "services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository"
    repository_type = importlib.import_module(
        module_name
    ).PostgresWebFullPageSupplementalClaimRepository
    async with _database() as (control, pool, _other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids)
        permit, command = await _permit_and_command(control, ids)
        repository = repository_type(pool=pool)
        phase_id = await repository.claim(command=command, permit=permit)
        assert phase_id is not None

        _, artifact_id = await _make_supplemental_child(
            control,
            ids,
            child_epoch=child_epoch,
            child_sequence=child_sequence,
        )
        await control.execute(
            "UPDATE qc_evidence_artifact SET metadata=$1::jsonb WHERE id=$2::uuid",
            json.dumps({} if purpose is None else {"purpose": purpose}), artifact_id,
        )

        with pytest.raises(
            RuntimeError,
            match="WEB_FULL_PAGE_SUPPLEMENTAL_FINISH_SCOPE_INVALID",
        ):
            await repository.finish(
                phase_id=phase_id,
                status="completed",
                reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED",
                artifact_ids=(artifact_id,),
            )
        assert await repository.read_terminal(command=command) is None
        assert (
            await control.fetchval(
                "SELECT status FROM qc_surface_phase_run WHERE phase_run_id=$1",
                phase_id,
            )
            == "running"
        )


@pytest.mark.asyncio
@_requires_postgres
async def test_legacy_completed_full_page_is_read_only_not_reconciled_again() -> None:
    """历史已完成、用途缺失的单图可读取，但绝不再次领取或修改原终态。"""
    from services.api.app.infrastructure.db.web_full_page_supplemental_claim_repository import (
        PostgresWebFullPageSupplementalClaimRepository,
    )

    async with _database() as (control, pool, other, ids):
        await _enabled_case(control, pool, ids)
        await _make_ready_primary_capture(control, ids)
        permit, command = await _permit_and_command(control, ids)
        repository = PostgresWebFullPageSupplementalClaimRepository(pool=pool)
        phase_id = await repository.claim(command=command, permit=permit)
        child_id, artifact_id = await _make_supplemental_child(
            control, ids, child_epoch=0, child_sequence=7,
        )
        await repository.finish(phase_id=phase_id, status="completed",
            reason_code="WEB_FULL_PAGE_SCREENSHOT_CAPTURED", artifact_ids=(artifact_id,))
        # 只构造隔离旧协议样本，不修改任何历史业务材料。
        await control.execute(
            "UPDATE qc_evidence_artifact SET metadata='{}'::jsonb WHERE id=$1::uuid", artifact_id,
        )
        phase_query = "SELECT * FROM qc_surface_phase_run WHERE phase_run_id=$1"
        before = await control.fetchrow(phase_query, phase_id)
        cold = PostgresWebFullPageSupplementalClaimRepository(pool=other)
        result = await cold.read_terminal(command=command)
        assert result.supplemental_capture_id == child_id
        assert result.screenshot_artifact_ids == (artifact_id,)
        assert await cold.claim(command=command, permit=permit) is None
        assert await control.fetchrow(phase_query, phase_id) == before


async def _make_ready_primary_capture(control, ids, *, capture_role="primary") -> None:
    """把 PG fixture 的 probe Capture 升级成尚未分类的 stable ready primary。"""
    await control.execute(
        "UPDATE qc_capture SET capture_role=$1 WHERE id=$2::uuid", capture_role, ids["capture"]
    )
    await control.execute(
        """
        INSERT INTO qc_page_capture (
            id, task_id, agent_run_id, page_seq, page_slug, url, page_state,
            capture_status, metadata, captured_at, surface_session_id, capture_role,
            session_command_sequence
        ) VALUES (
            $1::uuid, $2::uuid, $3::uuid, 1, 'ready-primary', 'https://example.test/',
            'loaded', 'captured', $4::jsonb, now(), $5, $6, 7
        )
        """,
        ids["capture"],
        ids["task"],
        ids["run"],
        json.dumps(
            {
                "capture_purpose": "web_page_ready_baseline",
                "pipeline_metrics": {
                    "web_readiness": {
                        "settle_complete": True,
                        "capture_consistent": True,
                        "human_verification_present": False,
                        "visible_loader": False,
                        "transition_active": False,
                    }
                },
                "finalization_epoch": 0,
            }
        ),
        ids["session"],
        capture_role,
    )
    await control.execute(
        """
        UPDATE qc_capture
        SET capture_role=$1,session_command_sequence=7,
            metadata=metadata || '{"finalization_epoch":0}'::jsonb
        WHERE id=$2::uuid
        """,
        capture_role,
        ids["capture"],
    )
    for artifact_type, content_type, suffix in (
        ("screenshot", "image/png", "png"),
        ("dom", "text/html", "html"),
    ):
        await control.execute(
            """
            INSERT INTO qc_evidence_artifact (
                id,task_id,page_capture_id,artifact_type,source,storage_path,
                content_type,checksum,size_bytes
            ) VALUES ($1::uuid,$2::uuid,$3::uuid,$4,'baseline',$5,$6,$7,1)
            """,
            str(uuid4()),
            ids["task"],
            ids["capture"],
            artifact_type,
            f"memory://ready.{suffix}",
            content_type,
            "sha256:" + ("1" if artifact_type == "screenshot" else "2") * 64,
        )


async def _make_supplemental_child(
    control,
    ids,
    *,
    child_epoch: int,
    child_sequence: int,
) -> tuple[str, str]:
    child_id, artifact_id = str(uuid4()), str(uuid4())
    await control.execute(
        """
        INSERT INTO qc_page_capture (
            id,task_id,agent_run_id,parent_page_capture_id,page_seq,page_slug,url,
            page_state,capture_status,metadata,captured_at,surface_session_id,
            capture_role,session_command_sequence
        ) VALUES (
            $1::uuid,$2::uuid,$3::uuid,$4::uuid,2,'supplemental-child',
            'https://example.test/','loaded','captured',$5::jsonb,now(),$6,
            'supplemental',$7
        )
        """,
        child_id,
        ids["task"],
        ids["run"],
        ids["capture"],
        json.dumps(
            {
                "capture_purpose": "web_full_page_supplemental",
                "finalization_epoch": child_epoch,
            }
        ),
        ids["session"],
        child_sequence,
    )
    await control.execute(
        """
        INSERT INTO qc_evidence_artifact (
            id,task_id,page_capture_id,artifact_type,source,storage_path,
            content_type,checksum,size_bytes,metadata
        ) VALUES (
            $1::uuid,$2::uuid,$3::uuid,'screenshot','baseline',
            'memory://supplemental.png','image/png',$4,1,
            '{"purpose":"web_direct_full_page"}'::jsonb
        )
        """,
        artifact_id,
        ids["task"],
        child_id,
        "sha256:" + "0" * 64,
    )
    return child_id, artifact_id


async def _make_segmented_supplemental_child(
    control,
    ids,
    *,
    child_epoch: int,
    child_sequence: int,
) -> tuple[str, tuple[str, str], str]:
    child_id, first_id = await _make_supplemental_child(
        control,
        ids,
        child_epoch=child_epoch,
        child_sequence=child_sequence,
    )
    second_id, manifest_id = str(uuid4()), str(uuid4())
    await control.execute(
        """
        INSERT INTO qc_evidence_artifact (
            id,task_id,page_capture_id,artifact_type,source,storage_path,
            content_type,checksum,size_bytes
        ) VALUES (
            $1::uuid,$2::uuid,$3::uuid,'screenshot','baseline',
            'memory://supplemental-1.png','image/png',$4,1
        )
        """,
        second_id,
        ids["task"],
        child_id,
        "sha256:" + "1" * 64,
    )
    await control.execute(
        """
        INSERT INTO qc_evidence_artifact (
            id,task_id,page_capture_id,artifact_type,source,storage_path,
            content_type,checksum,size_bytes,metadata
        ) VALUES (
            $1::uuid,$2::uuid,$3::uuid,'page_coverage','baseline',
            'memory://segment-manifest.json','application/json',$4,1,$5::jsonb
        )
        """,
        manifest_id,
        ids["task"],
        child_id,
        "sha256:" + "2" * 64,
        json.dumps(
            {
                "protocol_version": "web-full-page-segment-manifest.v1",
                "segment_count": 2,
                "associated_page_capture_id": child_id,
                "associated_screenshot_artifact_ids": [first_id, second_id],
            }
        ),
    )
    return child_id, (first_id, second_id), manifest_id


async def _permit_and_command(control, ids, *, source_url="https://example.test/"):
    lease = str(uuid4())
    await control.execute(
        "UPDATE qc_surface_session SET command_sequence=7,lease_owner='full-page-test',"
        "lease_token=$1::uuid,lease_expires_at=now()+interval '10 minutes',"
        "current_capture_id=$2::uuid WHERE session_id=$3",
        lease,
        ids["capture"],
        ids["session"],
    )
    permit = SessionCommandPermit(
        task_id=ids["task"],
        session_id=ids["session"],
        surface_kind=SurfaceKind.WEB,
        lease_owner="full-page-test",
        lease_token=lease,
        command_sequence=7,
        finalization_epoch=0,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=10),
        current_capture_id=ids["capture"],
    )
    return permit, SimpleNamespace(
        task_id=ids["task"],
        agent_run_id=ids["run"],
        session_id=ids["session"],
        source_capture_id=ids["capture"],
        source_url=source_url,
    )
