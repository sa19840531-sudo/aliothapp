"""真实 RuntimeProvider 在阶段续跑前验证冻结额度，避免绕过 bootstrap。"""

from types import SimpleNamespace

import pytest

from services.api.app.application import web_runtime_composition as composition
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
)
from services.api.app.application.web_menu_exploration import WebMenuExplorationSummary
from services.api.app.core.config import Settings
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
from services.api.tests.test_web_run_policy_snapshot import policy_payload
from services.api.tests.test_web_runtime_composition import _web_record


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_limit", [None, 2, 17, 10, "missing"])
async def test_provider_checks_existing_budget_before_restoring_advanced_checkpoint(
    monkeypatch,
    execution_limit,
):
    record = _web_record()
    body = policy_payload()
    body["budget"]["menu_limit"] = 10
    snapshot = WebRunPolicySnapshot.create(
        task_id=record.task_id,
        agent_run_id=record.run_id,
        payload=body,
    )

    class PolicyRepository:
        async def read(self, *, task_id, agent_run_id):
            assert (task_id, agent_run_id) == (record.task_id, record.run_id)
            return snapshot

    async def read_summary(self, *, agent_run_id, site_id):
        assert agent_run_id == record.run_id
        if execution_limit == "missing":
            raise WebExplorationBudgetLedgerConflictError("Web 预算尚未建立")
        return WebMenuExplorationSummary(
            site_id=site_id,
            menu_limit=execution_limit,
            page_timeline=("capture-before-resume",),
            page_type_keys=("content_detail",),
            frontier_remaining=0,
        )

    async def forbidden_bootstrap(*args, **kwargs):
        raise AssertionError("续跑验证不得依赖再次执行 bootstrap")

    restored = []
    real_restore = composition.restore_persisted_navigation_state

    def restore(*args, **kwargs):
        restored.append(args[1])
        return real_restore(*args, **kwargs)

    monkeypatch.setenv("ALIOTH_DATABASE_URL", "postgresql://unused/unused")
    monkeypatch.setenv("ALIOTH_WEB_MENU_COLLECTION_LIMIT", "23")
    monkeypatch.setattr(
        composition.PostgresWebExplorationBudgetLedgerRepository, "read_summary", read_summary
    )
    monkeypatch.setattr(
        composition.PostgresWebExplorationBudgetLedgerRepository, "bootstrap", forbidden_bootstrap
    )
    monkeypatch.setattr(composition, "restore_persisted_navigation_state", restore)
    capture_runtime = SimpleNamespace(
        coordinator=SimpleNamespace(),
        surface_driver=SimpleNamespace(),
        surface_sessions=SimpleNamespace(),
        web_desktop_capture_policy=SimpleNamespace(),
        trusted_ui_snapshot_reader=SimpleNamespace(),
        web_ad_landing_capture_port=SimpleNamespace(),
        web_full_page_capture_browser=SimpleNamespace(),
        web_page_capture_persistence=SimpleNamespace(),
        session_command_gate=SimpleNamespace(),
    )
    provider = composition.PostgresWebRuntimeProvider(
        capture_runtime_builder=lambda **kwargs: capture_runtime,
        assessment_agent=SimpleNamespace(),
        policy_repository=PolicyRepository(),
    )
    if execution_limit not in (10, "missing"):
        with pytest.raises(
            WebExplorationBudgetLedgerConflictError, match="WEB_BUDGET_POLICY_MISMATCH"
        ):
            await provider.build(record=record, settings=Settings())
        assert restored == []
    else:
        runtime = await provider.build(record=record, settings=Settings())
        assert runtime.stage_orchestrator is not None
        assert len(restored) == (0 if execution_limit == "missing" else 1)
