"""目录外候选持久化、统一槽位与冷恢复的真实 PostgreSQL 回归。"""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest

from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
from services.api.app.application.web_exploration_budget_ledger import (
    WebExplorationBudgetLedgerConflictError,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_menu_exploration import WebMenuExplorer
from services.api.app.application.web_run_policy import (
    build_new_web_navigation_context,
    load_published_web_policy,
    materialize_web_policy,
)
from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
from services.api.app.domain.exploration import WebMenuTerminalStatus
from services.api.app.domain.exploration.web_discovery_policy import WebDiscoveryPolicy
from services.api.app.domain.exploration.web_run_policy_snapshot import WebRunPolicySnapshot
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

pytestmark = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需要显式双 DSN 和隔离数据库守卫",
)


@pytest.mark.asyncio
async def test_disabled_discovery_keeps_every_observed_candidate_after_persistent_read():
    async with _database() as (_control, pool, _other, ids):
        site = _site()
        command = _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await repository.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        candidate = WebNavigationCandidate(
            entry_id="unlisted-one",
            label="新增资讯",
            url="https://example.test/news",
            menu_path=("新增资讯",),
            dom_fingerprint="news-link",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
            source_capture_id=ids["capture"],
            discovery_capture_id=ids["capture"],
        )
        explorer = WebMenuExplorer(
            navigation_context=SimpleNamespace(
                resolve_site_context=lambda url: SimpleNamespace(site=replace(site, catalog_version=_CATALOG_VERSION), content_sha256=_CATALOG_SHA256),
                version=_CATALOG_VERSION,
                catalog_content_sha256=_CATALOG_SHA256,
            ),
            candidate_source=SimpleNamespace(navigation_candidates=lambda session: (candidate,)),
            page_visitor=SimpleNamespace(),
            budget_ledger=repository,
            discovery_policy=WebDiscoveryPolicy(enabled=False),
        )
        result = await explorer.explore(command, initial_capture_id=ids["capture"])
        assert result.runtime_discovered_count == 1
        restored = await PostgresWebExplorationBudgetLedgerRepository(pool=pool).read_summary(
            agent_run_id=ids["run"],
            site_id=site.site_id,
        )
        assert restored.runtime_discovered_count == 1
        record = restored.runtime_discovered_ledger[0].to_dict()
        assert record["source_kind"] == "runtime_discovered"
        assert record["source_capture_id"] == ids["capture"]
        assert record["error_code"] == "WEB_DISCOVERY_DISABLED"
        assert restored.executed_same_origin_count == 0


@pytest.mark.asyncio
async def test_runtime_discovery_roundtrips_twenty_five_lineage_levels_and_rejects_bad_shape():
    from services.api.app.application.web_runtime_discovery import discovery_entry_id

    async with _database() as (control, pool, _other, ids):
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await repository.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        parent = None
        expected = []
        for depth in range(1, 26):
            label = f"层级{depth}"
            path = tuple(f"层级{index}" for index in range(1, depth + 1))
            candidate = WebNavigationCandidate(
                entry_id=f"candidate-{depth}",
                label=label,
                url=f"https://example.test/level/{depth}",
                menu_path=(label,),
                dom_fingerprint=f"dom-{depth}",
                source_signal="semantic_nav",
                page_type_key="nav-class:runtime",
                menu_position=depth,
                source_capture_id=ids["capture"],
                parent_menu_entry_id=(discovery_entry_id(parent) if parent else None),
                parent_logical_target_identity=(
                    parent.logical_target_identity if parent else None
                ),
                menu_depth=depth,
                exploration_path=path,
            )
            await repository.record_discovery(
                command=command,
                site=site,
                candidate=candidate,
                source_capture_id=ids["capture"],
                error_code="WEB_DISCOVERY_DISABLED",
            )
            expected.append(candidate)
            parent = candidate

        restored = await repository.read_summary(
            agent_run_id=ids["run"],
            site_id=site.site_id,
        )
        records = restored.runtime_discovered_ledger
        assert [item.menu_depth for item in records] == list(range(1, 26))
        assert records[-1].exploration_path == tuple(
            f"层级{index}" for index in range(1, 26)
        )
        assert records[-1].parent_menu_entry_id == discovery_entry_id(expected[-2])
        assert (
            records[-1].parent_logical_target_identity
            == expected[-2].logical_target_identity
        )
        assert records[-1].logical_target_identity == expected[-1].logical_target_identity

        row = await control.fetchrow(
            """
            SELECT * FROM qc_web_menu_ledger_entry
            WHERE agent_run_id=$1::uuid AND site_id=$2 AND menu_depth=25
            """,
            ids["run"],
            site.site_id,
        )

        async def insert_invalid_lineage(*, identity: str, depth: int, path: str) -> None:
            entry_id = "discovery:" + identity
            manifest = {
                "version": "web-discovery-manifest.v1",
                "entry_id": entry_id,
                "candidate": {
                    "entry_id": "invalid-candidate",
                    "label": "无效",
                    "parent_logical_target_identity": None,
                },
            }
            await control.execute(
                """
                INSERT INTO qc_web_menu_ledger_entry(
                  agent_run_id,site_id,menu_entry_id,catalog_position,decision,menu_path,
                  canonical_url_sha256,status,diagnostic,source_kind,
                  discovery_source_capture_id,discovery_manifest,parent_menu_entry_id,
                  menu_depth,exploration_path,logical_target_identity
                ) VALUES(
                  $1::uuid,$2,$3,200,'runtime_record_only','["无效"]'::jsonb,$4,
                  'runtime_discovered_unreviewed','{"error_code":"WEB_DISCOVERY_DISABLED"}'::jsonb,
                  'runtime_discovered',$5::uuid,$6::jsonb,NULL,$7,$8::jsonb,$9
                )
                """,
                ids["run"],
                site.site_id,
                entry_id,
                "f" * 64,
                ids["capture"],
                json.dumps(manifest, ensure_ascii=False),
                depth,
                path,
                identity,
            )

        with pytest.raises(asyncpg.CheckViolationError):
            async with control.transaction():
                await insert_invalid_lineage(
                    identity="c" * 64,
                    depth=0,
                    path='["无效"]',
                )
        with pytest.raises(asyncpg.CheckViolationError):
            async with control.transaction():
                await insert_invalid_lineage(identity="d" * 64, depth=1, path="[]")
        with pytest.raises(asyncpg.CheckViolationError):
            async with control.transaction():
                await control.execute(
                    """
                    UPDATE qc_web_menu_ledger_entry SET menu_depth=0
                    WHERE agent_run_id=$1::uuid AND site_id=$2 AND menu_entry_id=$3
                    """,
                    ids["run"],
                    site.site_id,
                    row["menu_entry_id"],
                )
        with pytest.raises(asyncpg.CheckViolationError):
            async with control.transaction():
                await control.execute(
                    """
                    UPDATE qc_web_menu_ledger_entry SET exploration_path='[]'::jsonb
                    WHERE agent_run_id=$1::uuid AND site_id=$2 AND menu_entry_id=$3
                    """,
                    ids["run"],
                    site.site_id,
                    row["menu_entry_id"],
                )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_relation",
    [
        "failed_action",
        "wrong_source",
        "wrong_result",
        "wrong_parent",
        "wrong_action_execution",
        "probe_ready_different",
        "before_wrong_role",
        "before_wrong_action_execution",
        "before_wrong_parent",
        "before_wrong_sequence",
        "after_wrong_role",
        "after_wrong_sequence",
        "submenu_page_type_present",
    ],
)
async def test_submenu_terminal_rejects_invalid_action_capture_relations(invalid_relation):
    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(
            pool=pool,
            frozen_policy=policy,
        )
        await repository.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        candidate = WebNavigationCandidate(
            entry_id="submenu-parent",
            label="父菜单",
            # 终态血缘用例必须先取得合法导航授权；无 URL 拒绝另用反例验证。
            url="https://example.test/submenu",
            menu_path=("父菜单",),
            dom_fingerprint="submenu-parent",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
            source_capture_id=ids["capture"],
            source_page_url="https://example.test/",
            source_kind="button",
            target_hint="父菜单",
            actionability="requires_runtime_validation",
        )
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_PENDING",
        )
        entry = WebRuntimeMenuEntry.from_candidate(candidate)
        reservation = await repository.claim_menu(
            command=command,
            entry=entry,
            source_capture_id=ids["capture"],
        )
        assert reservation is not None
        before_capture_id, after_capture_id = await _seed_completed_action_evidence(
            control,
            ids,
            action_execution_id="pg-submenu-action",
            intent_id=reservation.intent_id,
            command_sequence=1,
            requested_source_capture_id=ids["capture"],
            parent_capture_id=ids["capture"],
        )
        probe_capture_id = after_capture_id
        ready_capture_id = after_capture_id
        if invalid_relation == "failed_action":
            await control.execute(
                "UPDATE qc_action_execution SET status='failed', "
                "error_code='WEB_TEST_ACTION_FAILED' "
                "WHERE action_execution_id='pg-submenu-action'"
            )
        elif invalid_relation == "wrong_source":
            await control.execute(
                "UPDATE qc_action_execution SET requested_source_capture_id=$1::uuid "
                "WHERE action_execution_id='pg-submenu-action'",
                before_capture_id,
            )
        elif invalid_relation == "wrong_result":
            probe_capture_id = ids["capture"]
            ready_capture_id = ids["capture"]
        elif invalid_relation == "wrong_parent":
            await control.execute(
                "UPDATE qc_capture SET parent_capture_id=$1::uuid WHERE id=$2::uuid",
                ids["capture"],
                after_capture_id,
            )
        elif invalid_relation == "wrong_action_execution":
            await control.execute(
                "UPDATE qc_capture SET action_execution_id=NULL WHERE id=$1::uuid",
                after_capture_id,
            )
        elif invalid_relation == "probe_ready_different":
            probe_capture_id = before_capture_id
        elif invalid_relation == "before_wrong_role":
            await control.execute(
                "UPDATE qc_capture SET capture_role='observation' WHERE id=$1::uuid",
                before_capture_id,
            )
        elif invalid_relation == "before_wrong_action_execution":
            await control.execute(
                "UPDATE qc_capture SET action_execution_id=NULL WHERE id=$1::uuid",
                before_capture_id,
            )
        elif invalid_relation == "before_wrong_parent":
            await control.execute(
                "UPDATE qc_capture SET parent_capture_id=NULL WHERE id=$1::uuid",
                before_capture_id,
            )
        elif invalid_relation == "before_wrong_sequence":
            await control.execute(
                "UPDATE qc_capture SET session_command_sequence=99 WHERE id=$1::uuid",
                before_capture_id,
            )
        elif invalid_relation == "after_wrong_role":
            await control.execute(
                "UPDATE qc_capture SET capture_role='observation' WHERE id=$1::uuid",
                after_capture_id,
            )
        elif invalid_relation == "after_wrong_sequence":
            await control.execute(
                "UPDATE qc_capture SET session_command_sequence=99 WHERE id=$1::uuid",
                after_capture_id,
            )
        outcome = {
            "command_accepted": True,
            "effect_observed": True,
            "postcondition_satisfied": True,
            "transition_arrived": True,
            "after_capture_id": ready_capture_id,
            "failure_reason": None,
            "evidence_mode": "natural",
        }
        with pytest.raises(WebExplorationBudgetLedgerConflictError):
            await repository.finalize(
                WebMenuLedgerTerminalRecord(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    site_id=site.site_id,
                    menu_entry_id=entry.entry_id,
                    status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
                    menu_action_execution_id="pg-submenu-action",
                    menu_probe_capture_id=probe_capture_id,
                    menu_ready_capture_id=ready_capture_id,
                    menu_page_type=(
                        "content_list"
                        if invalid_relation == "submenu_page_type_present"
                        else None
                    ),
                    diagnostic={
                        "type": "WEB_SUBMENU_EXPANDED",
                        "menu_interaction_outcome": outcome,
                    },
                )
            )


@pytest.mark.asyncio
async def test_submenu_terminal_accepts_complete_action_capture_relations():
    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(
            pool=pool,
            frozen_policy=policy,
        )
        await repository.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        candidate = WebNavigationCandidate(
            entry_id="submenu-parent-valid",
            label="父菜单",
            url="https://example.test/submenu",
            menu_path=("父菜单",),
            dom_fingerprint="submenu-parent-valid",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
            source_capture_id=ids["capture"],
            source_page_url="https://example.test/",
            source_kind="button",
            target_hint="父菜单",
            actionability="requires_runtime_validation",
        )
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_PENDING",
        )
        entry = WebRuntimeMenuEntry.from_candidate(candidate)
        reservation = await repository.claim_menu(
            command=command,
            entry=entry,
            source_capture_id=ids["capture"],
        )
        assert reservation is not None
        _before_capture_id, after_capture_id = await _seed_completed_action_evidence(
            control,
            ids,
            action_execution_id="pg-submenu-action-valid",
            intent_id=reservation.intent_id,
            command_sequence=1,
            requested_source_capture_id=ids["capture"],
            parent_capture_id=ids["capture"],
        )
        outcome = {
            "command_accepted": True,
            "effect_observed": True,
            "postcondition_satisfied": True,
            "transition_arrived": True,
            "after_capture_id": after_capture_id,
            "failure_reason": None,
            "evidence_mode": "natural",
        }

        await repository.finalize(
            WebMenuLedgerTerminalRecord(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                site_id=site.site_id,
                menu_entry_id=entry.entry_id,
                status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
                menu_action_execution_id="pg-submenu-action-valid",
                menu_probe_capture_id=after_capture_id,
                menu_ready_capture_id=after_capture_id,
                diagnostic={
                    "type": "WEB_SUBMENU_EXPANDED",
                    "menu_interaction_outcome": outcome,
                },
            )
        )
        restored = await repository.read_summary(
            agent_run_id=ids["run"],
            site_id=site.site_id,
        )

        execution = restored.runtime_discovered_ledger[0].execution
        assert execution is not None
        assert execution.status is WebMenuTerminalStatus.MENU_PAGE_CAPTURED, execution.to_dict()
        assert execution.submenu_relations_verified


async def test_submenu_without_url_remains_unclaimed_and_costs_no_action():
    async with _database() as (control, pool, _, ids):
        policy = await _enabled_case(control, pool, ids)
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        await repository.bootstrap(command, site, catalog_version=_CATALOG_VERSION,
                                   catalog_sha256=_CATALOG_SHA256)
        candidate = WebNavigationCandidate(entry_id="no-url", label="父菜单", url=None,
            menu_path=("父菜单",), dom_fingerprint="no-url", source_signal="semantic_nav",
            page_type_key="unknown", source_capture_id=ids["capture"],
            source_page_url="https://example.test/", source_kind="button", target_hint="父菜单",
            actionability="requires_runtime_validation")
        await repository.record_discovery(command=command, site=site, candidate=candidate,
            source_capture_id=ids["capture"], error_code="WEB_DISCOVERY_PENDING")
        assert await repository.claim_menu(command=command,
            entry=WebRuntimeMenuEntry.from_candidate(candidate), source_capture_id=ids["capture"]) is None
        summary = await repository.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        assert summary.runtime_discovered_ledger[0].error_code == "WEB_DISCOVERY_DIRECT_URL_REQUIRED"
        assert summary.executed_same_origin_count == 0
        assert await control.fetchval("SELECT count(*) FROM qc_action_execution WHERE task_id=$1::uuid",
                                      ids["task"]) == 0


async def _enabled_case(control, pool, ids, *, runtime_limit=2, menu_limit=10):
    # 重建本用例的来源采集前，先解除会话当前页面的引用。
    await control.execute(
        "UPDATE qc_surface_session SET current_capture_id=NULL WHERE session_id=$1",
        ids["session"],
    )
    await control.execute("DELETE FROM qc_capture WHERE id=$1::uuid", ids["capture"])
    session = "web:" + ids["run"]
    await control.execute(
        "UPDATE qc_surface_session SET session_id=$1 WHERE session_id=$2", session, ids["session"]
    )
    ids["session"] = session
    await control.execute(
        'UPDATE qc_task SET task_config=\'{"workflow_version":"v2"}\'::jsonb WHERE id=$1::uuid',
        ids["task"],
    )
    body = load_published_web_policy(
        now=datetime.now(UTC),
        max_duration_seconds=600,
        exploration_target={
            "required_page_types": ["content_list", "content_detail"],
            "discovery_policy": {
                "enabled": True,
                "max_unlisted_menus": runtime_limit,
                "same_origin_only": True,
            },
        },
    )
    body["budget"]["menu_limit"] = menu_limit
    body["discovery_policy"]["max_unlisted_menus"] = runtime_limit
    frozen = await PostgresWebRunPolicySnapshotRepository(pool=pool).freeze_or_read(
        WebRunPolicySnapshot.create(task_id=ids["task"], agent_run_id=ids["run"], payload=body),
    )
    await control.execute(
        """
        INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
          capture_role,capture_status,captured_at,metadata)
        VALUES ($1::uuid,$2::uuid,$3::uuid,'web',$4,'probe_before','captured',now(),
          '{"finalization_epoch":0}'::jsonb)
    """,
        ids["capture"],
        ids["task"],
        ids["run"],
        ids["session"],
    )
    await control.execute(
        "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
        ids["capture"],
        ids["session"],
    )
    return materialize_web_policy(frozen)


@pytest.mark.asyncio
async def test_new_runtime_policy_enforces_frozen_depth_for_dynamic_menus():
    """真实 PG 只领取冻结深度内的候选，越界候选保持预算终态。"""
    from services.api.app.application.web_runtime_discovery import discovery_entry_id

    async with _database() as (control, pool, other, ids):
        policy = await _enabled_case(control, pool, ids, runtime_limit=None)
        command = _command(ids)
        context = build_new_web_navigation_context(
            target_url=command.target_url, policy=policy,
        ).resolve_site_context(command.target_url)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        await repository.bootstrap(command, context, catalog_version=context.site.catalog_version,
                                   catalog_sha256=context.content_sha256)
        parent = None
        reservations = []
        for depth in range(1, policy.frontier_policy.max_depth + 2):
            candidate = WebNavigationCandidate(
                entry_id=f"observed-{depth}", label=f"分类{depth}",
                url=f"https://example.test/observed/{depth}", menu_path=(f"分类{depth}",),
                dom_fingerprint=f"runtime-{depth}", source_signal="semantic_nav", page_type_key="unknown",
                source_capture_id=ids["capture"], menu_position=depth,
                parent_menu_entry_id=discovery_entry_id(parent) if parent else None,
                parent_logical_target_identity=parent.logical_target_identity if parent else None,
                menu_depth=depth, exploration_path=tuple(f"分类{i}" for i in range(1, depth + 1)),
            )
            await repository.record_discovery(command=command, site=context.site, candidate=candidate,
                                             source_capture_id=ids["capture"], error_code="WEB_DISCOVERY_PENDING")
            entry = WebRuntimeMenuEntry.from_candidate(candidate)
            reservation = await repository.claim_menu(command=command, entry=entry, source_capture_id=ids["capture"])
            if depth <= policy.frontier_policy.max_depth:
                assert reservation is not None, await control.fetch(
                    "SELECT status,diagnostic FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid",
                    ids["run"],
                )
                reservations.append(reservation)
                # 深度预算与同源并发是独立围栏；前一槽未终态不得并行领取下一层。
                await repository.finalize(WebMenuLedgerTerminalRecord(
                    task_id=ids["task"], agent_run_id=ids["run"], session_id=ids["session"],
                    site_id=context.site.site_id, menu_entry_id=entry.entry_id,
                    status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                    diagnostic={"error_code": "WEB_NAVIGATION_TARGET_UNVERIFIED"},
                ))
            else:
                assert reservation is None
            parent = candidate
        assert [r.execution_slot for r in reservations] == list(
            range(1, policy.frontier_policy.max_depth + 1)
        )
        restored = PostgresWebExplorationBudgetLedgerRepository(pool=other, frozen_policy=policy)
        repeated = await restored.claim_menu(command=command, entry=entry, source_capture_id=ids["capture"])
        assert repeated is None
        rows = await control.fetch(
            "SELECT decision,menu_depth,menu_action_count,diagnostic "
            "FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        assert sum(row["menu_action_count"] for row in rows) == policy.frontier_policy.max_depth
        assert max(row["menu_depth"] or 0 for row in rows) == policy.frontier_policy.max_depth + 1
        rejected = next(
            row for row in rows if row["menu_depth"] == policy.frontier_policy.max_depth + 1
        )
        assert json.loads(rejected["diagnostic"])["error_code"] == "WEB_FRONTIER_DEPTH_EXHAUSTED"


@pytest.mark.asyncio
async def test_discovery_record_rejects_non_current_source_capture() -> None:
    async with _database() as (control, pool, _other, ids):
        await _enabled_case(control, pool, ids, runtime_limit=None)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        site, command = _site(), _command(ids)
        await repository.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        replacement_capture_id = str(uuid4())
        await control.execute(
            """
            INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
              capture_role,capture_status,captured_at,metadata)
            VALUES ($1::uuid,$2::uuid,$3::uuid,'web',$4,'probe_before','captured',now(),
              '{"finalization_epoch":0}'::jsonb)
            """,
            replacement_capture_id,
            ids["task"],
            ids["run"],
            ids["session"],
        )
        await control.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            replacement_capture_id,
            ids["session"],
        )
        candidate = WebNavigationCandidate(
            entry_id="stale-source",
            label="过期来源",
            url="https://example.test/stale",
            menu_path=("过期来源",),
            dom_fingerprint="stale-source",
            source_signal="semantic_nav",
            page_type_key="unknown",
            source_page_url="https://example.test/",
        )

        with pytest.raises(
            WebExplorationBudgetLedgerConflictError,
            match="WEB_DISCOVERY_SCOPE_INVALID",
        ):
            await repository.record_discovery(
                command=command,
                site=site,
                candidate=candidate,
                source_capture_id=ids["capture"],
                error_code="WEB_DISCOVERY_PENDING",
            )


@pytest.mark.asyncio
async def test_enabled_discovery_uses_one_shared_slot_and_holds_origin_concurrency():
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

    async with _database() as (control, pool, other, ids):
        policy = await _enabled_case(control, pool, ids, menu_limit=13)
        site, command = _site(), _command(ids)
        first = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        second = PostgresWebExplorationBudgetLedgerRepository(pool=other, frozen_policy=policy)
        await first.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        selected = await control.fetch(
            "SELECT menu_entry_id FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid AND execution_slot IS NOT NULL",
            ids["run"],
        )
        assert len(selected) == 11
        for row in selected:
            entry = next(item for item in site.entries if item.entry_id == row["menu_entry_id"])
            assert await first.claim_menu(
                command=command, entry=entry, source_capture_id=ids["capture"]
            )
        discoveries = []
        for index in range(3):
            candidate = WebNavigationCandidate(
                entry_id=f"candidate-{index}",
                label=f"资讯{index}",
                url=f"https://example.test/news/{index}",
                menu_path=(f"资讯{index}",),
                dom_fingerprint=f"dom-{index}",
                source_signal="semantic_nav",
                page_type_key="nav-class:runtime",
                source_capture_id=ids["capture"],
            )
            await first.record_discovery(
                command=command,
                site=site,
                candidate=candidate,
                source_capture_id=ids["capture"],
                error_code="WEB_DISCOVERY_PENDING",
            )
            discoveries.append(WebRuntimeMenuEntry.from_candidate(candidate))
        pair = await asyncio.gather(
            *(
                repo.claim_menu(
                    command=command, entry=discoveries[0], source_capture_id=ids["capture"]
                )
                for repo in (first, second)
            )
        )
        assert sum(item is not None and not item.already_reserved for item in pair) == 1
        assert len({item.execution_slot for item in pair if item is not None}) == 1
        assert {item.execution_slot for item in pair if item is not None} == {12}
        competing = await asyncio.gather(
            first.claim_menu(command=command, entry=discoveries[1], source_capture_id=ids["capture"]),
            second.claim_menu(command=command, entry=discoveries[2], source_capture_id=ids["capture"]),
        )
        assert competing == [None, None]
        await second.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        assert (
            await second.claim_menu(
                command=command, entry=discoveries[0], source_capture_id=ids["capture"]
            )
        ).already_reserved
        rows = await control.fetch(
            "SELECT execution_slot, menu_action_count, source_kind "
            "FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid",
            ids["run"],
        )
        assert sum(row["menu_action_count"] for row in rows) == 12
        assert (
            {row["execution_slot"] for row in rows if row["execution_slot"] is not None}
            == set(range(1, 13))
        )
        assert (
            sum(
                row["source_kind"] == "runtime_discovered" and row["menu_action_count"] == 1
                for row in rows
            )
            == 1
        )


@pytest.mark.asyncio
async def test_enabled_runtime_explores_unlisted_after_required_with_catalog_over_ten():
    from services.api.app.application.web_menu_exploration import WebMenuVisitResult
    from services.api.app.domain.exploration import WebMenuTerminalStatus

    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids, menu_limit=13)
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        candidates = tuple(
            WebNavigationCandidate(
                entry_id="observed-" + entry.entry_id,
                label=entry.menu_path[-1],
                url=entry.canonical_url,
                menu_path=entry.menu_path,
                dom_fingerprint="dom-" + entry.entry_id,
                source_signal="semantic_nav",
                page_type_key="nav-class:runtime",
                source_capture_id=ids["capture"],
            )
            for entry in site.entries
        )
        discoveries = tuple(
            WebNavigationCandidate(
                entry_id=f"new-{index}",
                label=label,
                url=url,
                menu_path=(label,),
                dom_fingerprint=f"new-dom-{index}",
                source_signal="semantic_nav",
                page_type_key="nav-class:runtime",
                source_capture_id=ids["capture"],
            )
            for index, (label, url) in enumerate(
                (
                    ("新文章", "https://example.test/news/1"),
                    ("新视频", "https://example.test/news/2"),
                    ("更多资讯", "https://example.test/news/3"),
                    ("账号登录", "https://example.test/login"),
                    ("确认支付", "https://example.test/pay"),
                    ("客户端下载", "https://example.test/download"),
                    ("外部新闻", "https://outside.test/news"),
                    ("删除数据", "https://example.test/delete"),
                )
            )
        )
        visited = []

        class Visitor:
            async def visit(self, **kwargs):
                visited.append(kwargs["entry"])
                return WebMenuVisitResult(
                    final_capture_id=None,
                    page_type=None,
                    status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                    error_code="WEB_TEST_PROVIDER_NOT_SUBMITTED",
                    menu_action_count=1,
                    menu_intent_id=kwargs["menu_intent_id"],
                )

        explorer = WebMenuExplorer(
            navigation_context=SimpleNamespace(
                resolve_site_context=lambda url: SimpleNamespace(site=replace(site, catalog_version=_CATALOG_VERSION), content_sha256=_CATALOG_SHA256),
                version=_CATALOG_VERSION,
                catalog_content_sha256=_CATALOG_SHA256,
            ),
            candidate_source=SimpleNamespace(
                navigation_candidates=lambda session: candidates + discoveries
            ),
            page_visitor=Visitor(),
            budget_ledger=repository,
            discovery_policy=policy.discovery_policy,
            required_page_types=policy.required_page_types,
        )
        result = await explorer.explore(command, initial_capture_id=ids["capture"])
        assert len(visited) == 13
        assert (
            sum(getattr(entry, "source_kind", "") == "runtime_discovered" for entry in visited) == 2
        )
        assert result.executed_same_origin_count == 13
        assert len(result.runtime_discovered_ledger) == 8
        reasons = {item.menu_path[-1]: item.error_code for item in result.runtime_discovered_ledger}
        assert reasons["更多资讯"] == "WEB_DISCOVERY_BUDGET_EXHAUSTED"
        assert reasons["账号登录"] == "WEB_DISCOVERY_LOGIN_BLOCKED"
        assert reasons["确认支付"] == "WEB_DISCOVERY_PAYMENT_BLOCKED"
        assert reasons["客户端下载"] == "WEB_DISCOVERY_DOWNLOAD_BLOCKED"
        assert reasons["外部新闻"] == "WEB_DISCOVERY_CROSS_ORIGIN_BLOCKED"
        assert reasons["删除数据"] == "WEB_DISCOVERY_UNAUTHORIZED_ACTION_BLOCKED"
        await explorer.explore(command, initial_capture_id=ids["capture"])
        assert len(visited) == 13


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["closeout", "unknown_no_href"])
async def test_discovery_refresh_preserves_rejection_and_never_repeats_unknown_no_href(boundary):
    from datetime import UTC, datetime, timedelta

    from services.api.app.application.web_menu_exploration import WebMenuVisitResult
    from services.api.app.domain.exploration import WebMenuTerminalStatus

    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        site, command = _site(), _command(ids)

        class BoundaryRepository(PostgresWebExplorationBudgetLedgerRepository):
            async def claim_menu(self, **kwargs):
                if boundary == "closeout":
                    kwargs["command"] = replace(
                        kwargs["command"], deadline=datetime.now(UTC) + timedelta(seconds=10)
                    )
                return await super().claim_menu(**kwargs)

        repository = BoundaryRepository(pool=pool, frozen_policy=policy)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        candidate = WebNavigationCandidate(
            entry_id="first-sealed",
            label="资讯",
            url="https://example.test/news" if boundary == "closeout" else None,
            menu_path=("资讯",),
            dom_fingerprint="first-dom",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
            source_capture_id=ids["capture"],
            actionability="requires_runtime_validation",
        )
        observed, visited = [candidate], []

        class Visitor:
            async def visit(self, **kwargs):
                visited.append(kwargs["entry"])
                observed[:] = [
                    replace(candidate, entry_id="fresh-sealed", dom_fingerprint="fresh-dom")
                ]
                return WebMenuVisitResult(
                    final_capture_id=ids["capture"],
                    page_type=None,
                    status=WebMenuTerminalStatus.NAVIGATION_FAILED,
                    error_code="WEB_DISCOVERY_SAME_URL_OBJECT_UNPROVEN",
                    menu_action_count=1,
                    menu_intent_id=kwargs["menu_intent_id"],
                )

        explorer = WebMenuExplorer(
            navigation_context=SimpleNamespace(
                resolve_site_context=lambda url: SimpleNamespace(site=replace(site, catalog_version=_CATALOG_VERSION), content_sha256=_CATALOG_SHA256),
                version=_CATALOG_VERSION,
                catalog_content_sha256=_CATALOG_SHA256,
            ),
            candidate_source=SimpleNamespace(navigation_candidates=lambda session: tuple(observed)),
            page_visitor=Visitor(),
            budget_ledger=repository,
            discovery_policy=policy.discovery_policy,
            required_page_types=policy.required_page_types,
        )
        summary = await explorer.explore(command, initial_capture_id=ids["capture"])
        if boundary == "closeout":
            assert not visited
            assert (
                summary.runtime_discovered_ledger[0].error_code == "WEB_DISCOVERY_CLOSEOUT_RESERVED"
            )
        else:
            assert not visited
            assert {item.error_code for item in summary.runtime_discovered_ledger} == {
                "WEB_DISCOVERY_DIRECT_URL_REQUIRED"
            }
            assert summary.executed_same_origin_count == 0
            assert await control.fetchval(
                "SELECT count(*) FROM qc_action_execution WHERE task_id=$1::uuid", ids["task"]
            ) == 0
            restored = await explorer.explore(command, initial_capture_id=ids["capture"])
            assert restored.runtime_discovered_ledger == summary.runtime_discovered_ledger
            assert not visited


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "missing_version",
        "null_version",
        "missing_candidate",
        "null_candidate",
        "wrong_candidate_type",
        "empty_candidate_id",
        "missing_candidate_id",
        "mismatched_entry_id",
    ],
)
async def test_discovery_manifest_constraint_rejects_null_missing_and_mismatched_fields(mutation):
    async with _database() as (control, pool, _other, ids):
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        candidate = WebNavigationCandidate(
            entry_id="candidate-one",
            label="资讯",
            url="https://example.test/news",
            menu_path=("资讯",),
            dom_fingerprint="dom-news",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
            source_capture_id=ids["capture"],
        )
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_DISABLED",
        )
        row = await control.fetchrow(
            "SELECT menu_entry_id,discovery_manifest FROM qc_web_menu_ledger_entry "
            "WHERE agent_run_id=$1::uuid AND source_kind='runtime_discovered'",
            ids["run"],
        )
        manifest = json.loads(row["discovery_manifest"])
        manifest["entry_id"] = "rejected-discovery"
        if mutation == "missing_version":
            manifest.pop("version")
        elif mutation == "null_version":
            manifest["version"] = None
        elif mutation == "missing_candidate":
            manifest.pop("candidate")
        elif mutation == "null_candidate":
            manifest["candidate"] = None
        elif mutation == "wrong_candidate_type":
            manifest["candidate"] = []
        elif mutation == "empty_candidate_id":
            manifest["candidate"]["entry_id"] = ""
        elif mutation == "missing_candidate_id":
            manifest["candidate"].pop("entry_id")
        else:
            manifest["entry_id"] = "another-discovery"
        with pytest.raises(asyncpg.CheckViolationError):
            async with control.transaction():
                await control.execute(
                    """
                    INSERT INTO qc_web_menu_ledger_entry(
                        agent_run_id,site_id,menu_entry_id,catalog_position,
                        decision,menu_path,canonical_url_sha256,source_kind,
                        discovery_source_capture_id,discovery_manifest,status)
                    VALUES($1::uuid,$2,'rejected-discovery',99,'runtime_record_only','["资讯"]'::jsonb,$3,
                        'runtime_discovered',$4::uuid,$5::jsonb,'runtime_discovered_unreviewed')
                """,
                    ids["run"],
                    site.site_id,
                    "a" * 64,
                    ids["capture"],
                    json.dumps(manifest),
                )


@pytest.mark.asyncio
async def test_discovery_source_scope_is_immutable_but_historical_after_current_advances():
    from uuid import uuid4

    async with _database() as (control, pool, _other, ids):
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        candidate = WebNavigationCandidate(
            entry_id="source-sealed",
            label="资讯",
            url="https://example.test/news",
            menu_path=("资讯",),
            dom_fingerprint="news",
            source_signal="semantic_nav",
            page_type_key="unknown",
            source_capture_id=ids["capture"],
        )
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_DISABLED",
        )
        second = str(uuid4())
        await control.execute(
            """
            INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
                capture_role,capture_status,captured_at)
            VALUES ($1::uuid,$2::uuid,$3::uuid,'web',$4,'observation','captured',now())
        """,
            second,
            ids["task"],
            ids["run"],
            ids["session"],
        )
        try:
            await control.execute(
                "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
                second,
                ids["session"],
            )
            before = await repository.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
            with pytest.raises(WebExplorationBudgetLedgerConflictError, match="WEB_DISCOVERY_SCOPE_INVALID"):
                await repository.record_discovery(
                    command=command, site=site, candidate=candidate,
                    source_capture_id=ids["capture"], error_code="WEB_DISCOVERY_DISABLED",
                )
            restored = await PostgresWebExplorationBudgetLedgerRepository(pool=pool).read_summary(
                agent_run_id=ids["run"], site_id=site.site_id,
            )
            assert restored.runtime_discovered_ledger == before.runtime_discovered_ledger
            async with control.transaction():
                with pytest.raises(asyncpg.CheckViolationError):
                    await control.execute(
                        """
                        UPDATE qc_web_menu_ledger_entry SET discovery_source_capture_id=$2::uuid
                        WHERE agent_run_id=$1::uuid AND source_kind='runtime_discovered'
                    """,
                        ids["run"],
                        second,
                    )
            async with _database() as (_other_control, _p2, _p3, foreign):
                async with control.transaction():
                    with pytest.raises(asyncpg.CheckViolationError):
                        await control.execute(
                            """
                            UPDATE qc_web_menu_ledger_entry SET discovery_source_capture_id=$2::uuid
                            WHERE agent_run_id=$1::uuid AND source_kind='runtime_discovered'
                        """,
                            ids["run"],
                            foreign["capture"],
                        )
                with pytest.raises(asyncpg.CheckViolationError, match="SOURCE_SCOPE_INVALID"):
                    async with control.transaction():
                        await control.execute(
                            """
                            INSERT INTO qc_web_menu_ledger_entry(
                                agent_run_id,site_id,menu_entry_id,catalog_position,
                                decision,menu_path,canonical_url_sha256,source_kind,
                                discovery_source_capture_id,discovery_manifest,status)
                            VALUES($1::uuid,$2,'foreign-source',99,'runtime_record_only','["资讯"]'::jsonb,$3,
                                'runtime_discovered',$4::uuid,$5::jsonb,'runtime_discovered_unreviewed')
                        """,
                            ids["run"],
                            site.site_id,
                            "a" * 64,
                            foreign["capture"],
                            json.dumps(
                                {
                                    "version": "web-discovery-manifest.v1",
                                    "entry_id": "foreign-source",
                                    "candidate": candidate.to_dict(),
                                }
                            ),
                        )
        finally:
            await control.execute(
                "UPDATE qc_surface_session SET current_capture_id=NULL WHERE session_id=$1",
                ids["session"],
            )
            await control.execute("DELETE FROM qc_capture WHERE id=$1::uuid", second)


@pytest.mark.asyncio
async def test_frozen_discovery_source_remains_authorized_after_current_capture_advances():
    """原始发现 Capture 同属当前 Web Run 时，后续 current Capture 可作为 GOTO 动作源。"""

    from uuid import uuid4

    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids, runtime_limit=None)
        site, command = _site(), _command(ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        await repository.bootstrap(
            command,
            site,
            catalog_version=_CATALOG_VERSION,
            catalog_sha256=_CATALOG_SHA256,
        )
        candidate = WebNavigationCandidate(
            entry_id="frozen-home-href",
            label="资讯",
            url="https://example.test/news",
            menu_path=("资讯",),
            dom_fingerprint="frozen-home-href",
            source_signal="semantic_nav",
            page_type_key="nav-class:runtime",
            source_capture_id=ids["capture"],
            discovery_capture_id=ids["capture"],
        )
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_PENDING",
        )
        current_capture_id = str(uuid4())
        await control.execute(
            """
            INSERT INTO qc_capture(id,task_id,agent_run_id,surface_kind,surface_session_id,
                capture_role,capture_status,captured_at,metadata)
            VALUES ($1::uuid,$2::uuid,$3::uuid,'web',$4,'observation','captured',now(),
                '{"finalization_epoch":0}'::jsonb)
            """,
            current_capture_id,
            ids["task"],
            ids["run"],
            ids["session"],
        )
        await control.execute(
            "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
            current_capture_id,
            ids["session"],
        )

        reservation = await repository.claim_menu(
            command=command,
            entry=WebRuntimeMenuEntry.from_candidate(candidate),
            source_capture_id=current_capture_id,
        )

        assert reservation is not None
        assert reservation.already_reserved is False
        row = await control.fetchrow(
            """
            SELECT discovery_source_capture_id::text,menu_source_capture_id::text
            FROM qc_web_menu_ledger_entry
            WHERE agent_run_id=$1::uuid AND menu_entry_id=$2
            """,
            ids["run"],
            WebRuntimeMenuEntry.from_candidate(candidate).entry_id,
        )
        assert row["discovery_source_capture_id"] == ids["capture"]
        assert row["menu_source_capture_id"] == current_capture_id


@pytest.mark.asyncio
async def test_discovery_detail_uses_same_reserved_menu_and_distinct_stable_intent():
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

    async with _database() as (control, pool, other, ids):
        policy = await _enabled_case(control, pool, ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        site, command = _site(), _command(ids)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        candidate = WebNavigationCandidate(
            entry_id="sealed-detail",
            label="资讯",
            url="https://example.test/news",
            menu_path=("资讯",),
            dom_fingerprint="news",
            source_signal="semantic_nav",
            page_type_key="unknown",
            source_capture_id=ids["capture"],
        )
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=ids["capture"],
            error_code="WEB_DISCOVERY_PENDING",
        )
        entry = WebRuntimeMenuEntry.from_candidate(candidate)
        menu = await repository.claim_menu(
            command=command, entry=entry, source_capture_id=ids["capture"]
        )
        detail = await repository.reserve_detail(
            command=command,
            entry=entry,
            source_capture_id=ids["capture"],
            candidate_id="detail-one",
            destination_url="https://example.test/article/1",
        )
        cold = PostgresWebExplorationBudgetLedgerRepository(pool=other, frozen_policy=policy)
        restored = await cold.reserve_detail(
            command=command,
            entry=entry,
            source_capture_id=ids["capture"],
            candidate_id="detail-one",
            destination_url="https://example.test/article/1",
        )
        assert restored.already_reserved
        assert restored.intent_id == detail.intent_id != menu.intent_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/news/",
        "https://example.test/news?utm_source=test",
        "https://example.test/news#section",
    ],
)
async def test_discovery_href_alias_reuses_materialized_t5_target_without_second_action(url):
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry
    from services.api.app.infrastructure.db.web_logical_target_repository import (
        PostgresWebLogicalTargetRepository,
    )
    from services.api.tests.test_web_logical_target_postgres import seed_material

    async with _database() as (control, pool, _other, ids):
        policy = await _enabled_case(control, pool, ids)
        repository = PostgresWebExplorationBudgetLedgerRepository(pool=pool, frozen_policy=policy)
        site, command = _site(), _command(ids)
        await repository.bootstrap(
            command, site, catalog_version=_CATALOG_VERSION, catalog_sha256=_CATALOG_SHA256
        )
        capture = await seed_material(
            control,
            pool,
            ids,
            url="https://example.test/news",
            requested_url=url,
            redirect_chain=[url, "https://example.test/news"] if url.endswith("/") else None,
        )
        candidate = WebNavigationCandidate(
            entry_id="alias-source",
            label="发现别名",
            url=url,
            menu_path=("发现别名",),
            dom_fingerprint="alias",
            source_signal="semantic_nav",
            page_type_key="unknown",
            source_capture_id=capture,
        )
        await repository.record_discovery(
            command=command,
            site=site,
            candidate=candidate,
            source_capture_id=capture,
            error_code="WEB_DISCOVERY_PENDING",
        )
        entry = WebRuntimeMenuEntry.from_candidate(candidate)
        targets = PostgresWebLogicalTargetRepository(pool=pool)
        if url.endswith("/"):
            assert (
                await targets.find(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    source_capture_id=capture,
                    url=url,
                )
                is None
            )
            await targets.record_capture_redirect_aliases(
                task_id=ids["task"],
                agent_run_id=ids["run"],
                session_id=ids["session"],
                source_capture_id=capture,
                url="https://example.test/news",
            )
        target = await targets.find(
            task_id=ids["task"],
            agent_run_id=ids["run"],
            session_id=ids["session"],
            source_capture_id=capture,
            url=entry.canonical_url,
        )
        await targets.record_menu_reuse(
            command=command,
            site_id=site.site_id,
            entry=entry,
            source_capture_id=capture,
            target=target,
        )
        explorer = WebMenuExplorer(
            navigation_context=SimpleNamespace(
                resolve_site_context=lambda url: SimpleNamespace(site=replace(site, catalog_version=_CATALOG_VERSION), content_sha256=_CATALOG_SHA256),
                version=_CATALOG_VERSION,
                catalog_content_sha256=_CATALOG_SHA256,
            ),
            candidate_source=SimpleNamespace(navigation_candidates=lambda session: (candidate,)),
            page_visitor=SimpleNamespace(),
            budget_ledger=repository,
            logical_target_repository=targets,
            discovery_policy=policy.discovery_policy,
            required_page_types=policy.required_page_types,
        )
        await explorer.explore(command, initial_capture_id=capture)
        restored = await repository.read_summary(agent_run_id=ids["run"], site_id=site.site_id)
        row = restored.runtime_discovered_ledger[0].to_dict()
        assert row["status"] == "logical_target_reused"
        assert row["reused_capture_id"] == capture
        assert row["menu_action_count"] == row["detail_action_count"] == 0
        diagnostic = json.loads(
            await control.fetchval(
                """
            SELECT diagnostic FROM qc_web_menu_ledger_entry
            WHERE agent_run_id=$1::uuid AND menu_entry_id=$2
        """,
                ids["run"],
                entry.entry_id,
            )
        )
        assert diagnostic == {
            "reused_target_stage": "menu",
            "reused_capture_id": capture,
            "reused_logical_page_id": target["logical_page_id"],
        }
