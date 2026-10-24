"""真实 Chromium 关闭后重开、采集入库和清理；识别模型用确定性站点替身。"""

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import pytest

from services.api.app.application.h5_modal_close import H5_MODAL_CLOSE_CAPABILITY, h5_modal_close
from services.api.app.application.h5_new_session_restoration import H5NewSessionRestorationExecutor
from services.api.app.application.h5_targeted_recapture import H5TargetedRecapturePlanService
from services.api.app.application.restoration import RestorationCoordinator
from services.api.app.application.state_equivalence import StateEquivalenceGate
from services.api.app.application.supplemental_evidence_policy import SupplementalEvidencePolicy
from services.api.app.infrastructure.browser.capture_runtime import (
    build_default_web_capture_runtime,
)
from services.api.app.infrastructure.browser.h5_new_session_recovery_runtime import (
    H5NewSessionRecoveryRuntime,
)
from services.api.app.infrastructure.browser.page_capture import (
    PageCaptureMode,
    PageCaptureRequest,
    PlaywrightPageCaptureBrowser,
)
from services.api.app.infrastructure.db.h5_closed_session_context import (
    PostgresH5ClosedSessionContextReader,
)
from services.api.app.infrastructure.db.page_capture_repository import PostgresPageCaptureRepository
from services.api.app.infrastructure.db.page_graph_repository import PostgresPageGraphRepository
from services.api.app.infrastructure.db.restoration_attempt_repository import (
    PostgresRestorationAttemptRepository,
)
from services.api.app.infrastructure.storage.local_evidence_store import LocalEvidenceArtifactStore
from services.api.tests.test_h5_closed_session_recapture_postgres import _closed_request
from services.api.tests.test_h5_entry_profile_compatibility import (
    user_agent_sensitive_entry as user_agent_sensitive_entry,
)
from services.api.tests.test_targeted_recapture_chain_postgres import _snapshot

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="需原库事务测试连接"
)


@pytest.mark.parametrize("popup_chain", [False, True])
async def test_real_closed_browser_reopens_with_fresh_capture_and_releases_session(
    monkeypatch,
    tmp_path,
    user_agent_sensitive_entry,
    popup_chain,
):
    from services.api.app.infrastructure.browser import capture_runtime as composition
    from services.api.app.infrastructure.browser import page_capture as page_module
    from services.api.app.infrastructure.db import h5_session_journey_gate as journey_module
    from services.api.app.infrastructure.db import pool as pool_module

    root, _ = user_agent_sensitive_entry
    monkeypatch.setattr(page_module, "_wait_for_page_settle", AsyncMock(return_value=True))
    entry = root + ("/popup-chain" if popup_chain else "/valid")
    browser = PlaywrightPageCaptureBrowser()
    async with _closed_request(separate_connections=True) as (
        connection, pool, ids, request, _, _, _, lease,
    ):
        await connection.execute(
            "UPDATE qc_task SET target_url=$2 WHERE id=$1", ids["task"], entry
        )
        monkeypatch.setattr(
            pool_module._default_pool_manager, "get_pool", AsyncMock(return_value=pool)
        )
        # 控制连接仍用真实 PostgreSQL 锁，但必须与业务夹具属于同一隔离库。
        monkeypatch.setattr(
            journey_module, "PostgresH5SessionJourneyGate",
            partial(
                journey_module.PostgresH5SessionJourneyGate,
                database_url=urlunsplit(
                    urlsplit(os.environ["ALIOTH_TEST_DATABASE_URL"])._replace(
                        path="/" + await connection.fetchval("SELECT current_database()"),
                    ),
                ),
            ),
        )
        monkeypatch.setattr(
            composition,
            "build_evidence_artifact_store",
            lambda _: LocalEvidenceArtifactStore(root=tmp_path),
        )
        capture_runtime = build_default_web_capture_runtime(
            browser=browser, h5_visual_coordinate_mode="off"
        )
        captures = PostgresPageCaptureRepository(pool=pool)

        async def detect(*, capture, task_id, agent_run_id):
            proof = None
            if popup_chain:
                snapshot = await capture_runtime.trusted_ui_snapshot_reader.read_ui_snapshot(
                    task_id=task_id, page_capture_id=capture.capture_id,
                )
                proof = h5_modal_close(snapshot)
                if await browser._sessions[capture.session_id].page.locator(
                    '[role="dialog"]:visible'
                ).count():
                    assert proof is not None, {
                        "flags": {key: value for key, value in snapshot.items()
                                  if key != "elements"},
                        "elements": snapshot.get("elements"),
                    }
            assert await captures.update_detection_result(
                task_id=task_id,
                agent_run_id=agent_run_id,
                page_capture_id=capture.capture_id,
                page_type="home",
                page_state="popup" if proof else "stable",
                detection_status="recognized",
                confidence=0.9,
                catalog_version="test",
                catalog_sha256="a" * 64,
            )
            # 用确定性识别结果隔离模型；真实 Coordinator、Driver、台账、页面图读取照常运行。
            await connection.execute(
                "UPDATE qc_page_graph_state SET logical_page_key='detail:42',"
                "identity_status='confirmed',"
                "identity_source='logical_surface' WHERE state_id=$1",
                capture.capture_id,
            )
            return {
                "page_capture_id": capture.capture_id,
                "page_url": entry,
                "page_type": "home",
                "page_state": "popup" if proof else "stable",
                "confidence": 0.9,
                "candidate_actions": [{
                    "action": "click", "target": "关闭当前弹窗",
                    "risk_level": "low", "purpose": "关闭当前阻挡公告",
                    "navigation_depth": 0,
                    "obstruction_capability": H5_MODAL_CLOSE_CAPABILITY,
                    "obstruction_signature": proof.obstruction_signature,
                    "target_keywords": [
                        "popup_dismissal", "close_control", H5_MODAL_CLOSE_CAPABILITY,
                    ],
                }] if proof else [],
                "matched_catalog_signals": [],
            }

        contexts = PostgresH5ClosedSessionContextReader(pool=pool)
        runtime = H5NewSessionRecoveryRuntime(
            capture_runtime=capture_runtime,
            checkpoint=AsyncMock(),
            deadline=datetime.now(UTC) + timedelta(minutes=2),
            surface_sessions=capture_runtime.surface_sessions,
            capture_coordinator=capture_runtime.build_h5_probe_coordinator(
                supplemental_evidence_policy=SupplementalEvidencePolicy(
                    enabled=True, runtime_ready=True
                ),
                new_session_restoration=True,
            ),
            detector=SimpleNamespace(detect=detect),
            page_graph=PostgresPageGraphRepository(pool=pool),
        )
        resolution = await H5TargetedRecapturePlanService(
            contexts=contexts, new_session_runtime=runtime
        ).resolve(
            request=request,
            agent_run_id=ids["run"],
            finalization_epoch=0,
        )
        plan = resolution.plan
        bootstrap_errors = []
        prepare = runtime.prepare_new_session

        async def prepare_with_diagnostics(**kwargs):
            try:
                return await prepare(**kwargs)
            except Exception as error:
                bootstrap_errors.append(repr(error))
                raise

        monkeypatch.setattr(runtime, "prepare_new_session", prepare_with_diagnostics)
        if popup_chain:
            context = runtime._plans[plan.plan_id]
            runtime._plans[plan.plan_id] = replace(context, entry_normalization_required=True)

        async def read_snapshot(*, capture_id, session_id, **_):
            return replace(
                _snapshot(
                    task_id=ids["task"],
                    capture_id=capture_id,
                    captured_at=await connection.fetchval(
                        "SELECT captured_at FROM qc_capture WHERE id=$1", capture_id
                    ),
                ),
                session_id=session_id,
            )

        try:
            await browser.capture(
                PageCaptureRequest(
                    mode=PageCaptureMode.GOTO,
                    session_id=plan.source_session_id,
                    url=entry,
                    capture_screenshot=False,
                    capture_dom=False,
                    capture_visible_text=False,
                )
            )
            await browser.close_session(plan.source_session_id)
            assert plan.source_session_id not in browser._sessions
            attempt = await RestorationCoordinator(
                repository=PostgresRestorationAttemptRepository(pool=pool),
                executor=H5NewSessionRestorationExecutor(
                    delegate=object(),
                    runtime=runtime,
                    snapshots=SimpleNamespace(read=read_snapshot),
                    agent_run_id=ids["run"],
                    finalization_epoch=0,
                ),
                equivalence_gate=StateEquivalenceGate(),
            ).restore(
                request=request,
                plan=plan,
                attempt_no=lease.attempt_count,
                original_snapshot=await read_snapshot(
                    capture_id=request.target_capture_id, session_id=plan.source_session_id
                ),
            )
            assert attempt.assessment is not None, (attempt.error_code, bootstrap_errors)
            assert attempt.assessment.passed
            assert attempt.restored_capture_id != request.target_capture_id
            assert attempt.action_execution_ids and plan.target_session_id in browser._sessions
            if popup_chain:
                current = browser._sessions[plan.target_session_id].page
                assert await current.locator('[role="dialog"]:visible').count() == 0
                assert len(attempt.action_execution_ids) >= 2
                assert await connection.fetchval(
                    "SELECT count(*) FROM qc_action_execution WHERE session_id=$1"
                    " AND status <> 'completed'", plan.target_session_id,
                ) == 0
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM qc_evidence_artifact WHERE page_capture_id=$1"
                    " AND artifact_type='screenshot'",
                    attempt.restored_capture_id,
                )
                >= 1
            )
        finally:
            await runtime.close_owned_sessions()
            await browser.close_session(plan.source_session_id)
        assert plan.target_session_id not in browser._sessions
        assert (
            await connection.fetchval(
                "SELECT status FROM qc_surface_session WHERE session_id=$1",
                plan.target_session_id,
            )
            == "closed"
        )
