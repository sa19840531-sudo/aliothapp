"""广告阶段一次性领取在并发、失败和 Worker 重建后仍持久生效。"""

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
from services.api.app.application.web_ad_landing_evidence import WebAdLandingCaptureError
from services.api.app.domain.exploration.surface_phase import build_surface_phase_payload_hash
from services.api.app.domain.interaction import SurfaceKind
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _database,
    _upgrade_isolated_postgres,  # noqa: F401 -- 注册隔离库迁移 fixture。
)
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case

pytestmark = pytest.mark.skipif(not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="仅显式隔离 PG")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [None, "failed", "completed"])
async def test_one_outbound_claim_survives_concurrent_workers_failure_and_rebuild(terminal):
    module_name = "services.api.app.infrastructure.db.web_ad_outbound_claim_repository"
    assert importlib.util.find_spec(module_name) is not None, "缺少持久广告阶段领取仓储"
    repository_type = importlib.import_module(module_name).PostgresWebAdOutboundClaimRepository
    async with _database() as (control, pool, other, ids):
        await _enabled_case(control, pool, ids)
        # 广告领取同时核验当前主帧页面 URL，不能仅构造中性 Capture。
        await control.execute(
            "INSERT INTO qc_page_capture(id,task_id,agent_run_id,page_seq,page_slug,url,"
            "capture_status,surface_session_id,capture_role,session_command_sequence) "
            "VALUES($1::uuid,$2::uuid,$3::uuid,1,'home','https://example.test/',"
            "'captured',$4,'probe_before',6)",
            ids["capture"], ids["task"], ids["run"], ids["session"],
        )
        lease = str(uuid4())
        await control.execute(
            "UPDATE qc_surface_session SET command_sequence=7,lease_owner='ad-test',"
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
            lease_owner="ad-test",
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
            logical_page_id="web-logical:" + "a" * 64,
            source_capture_id=ids["capture"],
            source_url="https://example.test/",
            destination_url="https://ad.example.test/",
            page_role="home",
        )
        first, second = repository_type(pool=pool), repository_type(pool=other)
        for changes in (
            {"task_id": str(uuid4())},
            {"session_id": "web:" + str(uuid4())},
            {"lease_token": str(uuid4())},
            {"lease_owner": "stale-worker"},
            {"command_sequence": 6},
            {"finalization_epoch": 1},
        ):
            assert await first.claim(command=command, permit=replace(permit, **changes)) is None
        claimed = await asyncio.gather(
            first.claim(command=command, permit=permit),
            second.claim(command=command, permit=permit),
        )
        assert sum(value is not None for value in claimed) == 1
        phase_id = next(value for value in claimed if value is not None)
        context_hash = await control.fetchval(
            "SELECT context_payload_hash FROM qc_surface_phase_run WHERE phase_run_id=$1", phase_id,
        )
        diagnostic = {
            "cause_type": "BlockedPageCaptureActionError",
            "raw_error_code": "WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS",
            "surface_phase_run_id": phase_id,
        }
        if terminal is not None:
            await first.finish(
                phase_id=phase_id,
                status=terminal,
                reason_code="WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS"
                if terminal == "failed"
                else "WEB_AD_LANDING_OBSERVED",
                artifact_ids=(),
                detail=diagnostic if terminal == "failed" else None,
            )
        if terminal == "failed":
            # 第二次收口不可重写首次原因，也不得再插一条诊断步骤。
            await second.finish(
                phase_id=phase_id, status="failed", reason_code="WEB_AD_PAGE_OPEN_FAILED",
                artifact_ids=(), detail={"cause_type": "RuntimeError"},
            )
            with pytest.raises(WebAdLandingCaptureError) as caught:
                await repository_type(pool=other).read_terminal(command=command)
            assert caught.value.reason_code == "WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS"
            assert caught.value.detail == diagnostic
            step = await control.fetchrow(
                "SELECT decision_payload,decision_payload_hash,reason_code "
                "FROM qc_surface_phase_step WHERE phase_run_id=$1", phase_id,
            )
            assert json.loads(step["decision_payload"]) == diagnostic
            assert step["decision_payload_hash"] == build_surface_phase_payload_hash(diagnostic)
            assert step["reason_code"] == "WEB_AD_OUTBOUND_NONPUBLIC_ADDRESS"
            phase = await control.fetchrow(
                "SELECT step_count,context_payload_hash FROM qc_surface_phase_run "
                "WHERE phase_run_id=$1", phase_id,
            )
            assert phase["step_count"] == 1
            assert phase["context_payload_hash"] == context_hash
        await control.execute(
            "UPDATE qc_surface_session SET command_sequence=8 WHERE session_id=$1", ids["session"]
        )
        permit.command_sequence = 8
        assert await repository_type(pool=other).claim(command=command, permit=permit) is None
        rows = await control.fetch(
            "SELECT status,phase_key FROM qc_surface_phase_run WHERE task_id=$1::uuid", ids["task"]
        )
        assert [dict(row) for row in rows] == [
            {"status": terminal or "running", "phase_key": "web_ad_outbound"}
        ]
        await control.execute(
            "DELETE FROM qc_surface_phase_run WHERE task_id=$1::uuid", ids["task"]
        )
