"""真实共享 resolver 消费冻结授权、已领取意图与实际动作前 Capture。"""

import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.interaction_runtime import (
    SemanticTargetResolutionError,
    SemanticTargetResolutionRequest,
)
from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
from services.api.app.domain.interaction import (
    ActionIntent,
    CaptureDirective,
    CaptureProfile,
    InteractionOperation,
    SurfaceKind,
)
from services.api.app.infrastructure.browser.semantic_target_resolver import (
    UiSnapshotSemanticTargetResolver,
)
from services.api.app.infrastructure.db.web_discovery_action_authorization import (
    PostgresWebDiscoveryActionAuthorization,
)
from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
    PostgresWebExplorationBudgetLedgerRepository,
)
from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
    PostgresWebRunPolicySnapshotRepository,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _CATALOG_SHA256,
    _CATALOG_VERSION,
    _command,
    _database,
    _seed_completed_action_evidence,
    _site,
)
from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
    _upgrade_isolated_postgres as _upgrade_isolated_postgres,
)
from services.api.tests.test_web_runtime_discovery_postgres import _enabled_case

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL"), reason="只允许显式隔离PG"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation", ["h5", "owner", "unclaimed", "current", "epoch", "sequence", "disabled", "latest", "candidate"]
)
async def test_real_shared_resolver_rejects_forged_discovery_scope_before_reading_dom(mutation):
    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        site, command = _site(), _command(ids)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        candidate = WebNavigationCandidate(
            entry_id="authorization-sealed",
            label="资讯",
            url="https://example.test/news",
            menu_path=("资讯",),
            dom_fingerprint="sealed",
            source_signal="semantic_nav",
            page_type_key="unknown",
            source_capture_id=ids["capture"],
            navigation_evidence=("visible", "clickable", "parallel_entries"),
            navigation_container_restore_seed={"observations": [{"labels": ["资讯", "其他"]}]},
        )
        entry = WebRuntimeMenuEntry.from_candidate(candidate)
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_PENDING",
        )
        reservation = await repository.claim_menu(
            command=command, entry=entry, source_capture_id=ids["capture"]
        )
        before, after = await _seed_completed_action_evidence(
            control,
            ids,
            action_execution_id="authorization-action",
            intent_id=reservation.intent_id,
            command_sequence=1,
            requested_source_capture_id=ids["capture"],
            parent_capture_id=ids["capture"],
        )
        await control.execute(
            "UPDATE qc_capture SET metadata='{\"finalization_epoch\":0}'::jsonb WHERE id=$1::uuid",
            before,
        )
        await control.execute(
            "UPDATE qc_action_execution SET status='executing',result_capture_id=NULL,"
            "action_finished_at=NULL,evidence_finished_at=NULL "
            "WHERE action_execution_id='authorization-action'"
        )
        await control.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            before,
            ids["session"],
        )
        snapshot = await PostgresWebRunPolicySnapshotRepository(pool=pool).read(
            task_id=ids["task"], agent_run_id=ids["run"]
        )
        authorizer = PostgresWebDiscoveryActionAuthorization(snapshot=snapshot, pool=pool)
        request = SemanticTargetResolutionRequest(
            task_id=ids["task"],
            session_id=ids["session"],
            source_capture_id=before,
            action_type=InteractionOperation.CLICK,
            semantic_target="资讯",
            parameters={
                "target_mode": "web_discovery_candidate",
                "web_menu_entry_id": entry.entry_id,
                "web_discovery_source_capture_id": ids["capture"],
                "web_discovery_candidate": candidate.to_dict(),
                "web_discovery_origin": "https://example.test/",
                "destination_url": candidate.url,
            },
        )
        # 与真实 Capture Runtime 一致：参数必须经过 ActionIntent 的深冻结边界。
        intent = ActionIntent(
            task_id=request.task_id,
            session_id=request.session_id,
            intent_id=reservation.intent_id,
            idempotency_key=reservation.intent_id,
            surface_kind=SurfaceKind.WEB,
            source_capture_id=ids["capture"],
            action_type=request.action_type,
            semantic_target=request.semantic_target,
            purpose="验证已预留导航候选",
            deadline=datetime.now(UTC),
            capture_profile=CaptureProfile(
                profile_id="authorization-test",
                before=CaptureDirective(role="before_action", artifact_types=("dom",)),
                after=CaptureDirective(role="after_action", artifact_types=("dom",)),
            ),
            parameters=request.parameters,
        )
        request = replace(request, parameters=intent.parameters)
        assert await authorizer.authorize(request)
        if mutation == "h5":
            await control.execute(
                "UPDATE qc_task SET target_platform='h5' WHERE id=$1::uuid", ids["task"]
            )
        elif mutation == "owner":
            await control.execute(
                "UPDATE qc_surface_session SET session_owner='h5-workflow' WHERE session_id=$1",
                ids["session"],
            )
        elif mutation == "unclaimed":
            request = replace(
                request, parameters={**request.parameters, "web_menu_entry_id": "unclaimed"}
            )
        elif mutation == "current":
            await control.execute(
                "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
                after,
                ids["session"],
            )
        elif mutation == "candidate":
            request = replace(
                request,
                parameters={
                    **request.parameters,
                    "web_discovery_candidate": {
                        **request.parameters["web_discovery_candidate"],
                        "navigation_container_restore_seed": {
                            "observations": [{"labels": ["伪造", "其他"]}]
                        },
                    },
                },
            )
        elif mutation == "epoch":
            with pytest.raises(asyncpg.RaiseError, match="finalization_epoch"):
                async with control.transaction():
                    await control.execute(
                        "UPDATE qc_action_execution SET finalization_epoch=1 "
                        "WHERE action_execution_id='authorization-action'"
                    )
            return
        elif mutation == "sequence":
            await control.execute(
                "UPDATE qc_action_execution SET command_sequence=2 "
                "WHERE action_execution_id='authorization-action'"
            )
        elif mutation == "latest":
            await control.execute(
                "INSERT INTO qc_agent_run(id,task_id,agent_type,agent_name,status) "
                "VALUES($1::uuid,$2::uuid,'main','隔离授权边界新Run','running')",
                str(uuid4()),
                ids["task"],
            )
        else:
            from services.api.app.application.web_run_policy import load_published_web_policy
            from services.api.app.domain.exploration.web_run_policy_snapshot import (
                WebRunPolicySnapshot,
            )

            disabled = WebRunPolicySnapshot.create(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                payload=load_published_web_policy(now=datetime.now(UTC), max_duration_seconds=600),
            )
            authorizer = PostgresWebDiscoveryActionAuthorization(snapshot=disabled, pool=pool)
        resolver = UiSnapshotSemanticTargetResolver(
            snapshot_reader=object(), web_discovery_authorizer=authorizer
        )
        with pytest.raises(SemanticTargetResolutionError, match="授权"):
            await resolver.resolve(request)
