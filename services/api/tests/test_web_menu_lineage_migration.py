"""菜单谱系迁移的静态合同与真实 PostgreSQL 验收。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import asyncpg
import pytest
from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig

from services.api.app.infrastructure.db.test_database_guard import (
    require_isolated_test_database_url,
)

_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = (
    _ROOT
    / "services"
    / "api"
    / "migrations"
    / "versions"
    / "20260914_0114_web_menu_lineage.py"
)
_CATALOG_PARENT_MIGRATION = (
    _ROOT
    / "services"
    / "api"
    / "migrations"
    / "versions"
    / "20260914_0116_web_catalog_parent_lineage.py"
)


class _OperationRecorder:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def _load_migration(monkeypatch: pytest.MonkeyPatch):
    operation = _OperationRecorder()
    alembic = ModuleType("alembic")
    alembic.op = operation  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alembic", alembic)
    spec = importlib.util.spec_from_file_location("web_menu_lineage_migration", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, operation


def _load_catalog_parent_migration(monkeypatch: pytest.MonkeyPatch):
    operation = _OperationRecorder()
    alembic = ModuleType("alembic")
    alembic.op = operation  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "alembic", alembic)
    spec = importlib.util.spec_from_file_location(
        "web_catalog_parent_lineage_migration",
        _CATALOG_PARENT_MIGRATION,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, operation


def test_catalog_parent_migration_preserves_strict_runtime_and_catalog_parent_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, operation = _load_catalog_parent_migration(monkeypatch)

    assert module.revision == "20260914_0116"
    assert module.down_revision == "20260914_0115"
    module.upgrade()
    sql = "\n".join(operation.statements)

    assert "parent.source_kind IN ('catalog', 'runtime_discovered')" in sql
    assert "parent.source_kind='catalog'" in sql
    assert "parent.menu_action_count" in sql
    assert "parent_menu_action_count=1" in sql
    assert "parent.menu_ready_capture_id" in sql
    assert "parent_menu_ready_capture_id IS NOT NULL" in sql
    assert "parent.menu_path" in sql
    assert "WEB_MENU_LINEAGE_PARENT_SCOPE_INVALID" in sql
    assert "WEB_MENU_LINEAGE_PARENT_MISMATCH" in sql
    assert "parent.logical_target_identity" in sql
    assert "parent_source_kind='runtime_discovered'" in sql
    assert "NEW.menu_depth <> 2" in sql
    assert "NEW.discovery_manifest->'candidate'->>'parent_logical_target_identity'" in sql
    assert "~ '^[0-9a-f]{64}$'" in sql


def test_catalog_parent_migration_downgrade_restores_runtime_only_parent_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, operation = _load_catalog_parent_migration(monkeypatch)

    module.downgrade()
    sql = "\n".join(operation.statements)

    validator = sql[sql.index("CREATE OR REPLACE FUNCTION qc_validate_web_menu_lineage") :]
    assert "parent.source_kind='runtime_discovered'" in validator
    assert "parent.source_kind='catalog'" not in validator
    assert "WEB_MENU_CATALOG_PARENT_DOWNGRADE_BLOCKED" in sql


def test_migration_adds_strict_runtime_lineage_and_explicit_submenu_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, operation = _load_migration(monkeypatch)

    assert module.revision == "20260914_0114"
    assert module.down_revision == "20260914_0113"
    module.upgrade()
    sql = "\n".join(operation.statements)

    for column in (
        "parent_menu_entry_id",
        "menu_depth",
        "exploration_path",
        "logical_target_identity",
        "normalization_version",
    ):
        assert f"ADD COLUMN {column}" in sql
    assert "menu_depth >= 1" in sql
    assert "jsonb_array_length(exploration_path) > 0" in sql
    assert "WEB_MENU_LINEAGE_IMMUTABLE" in sql
    assert "WEB_MENU_LINEAGE_PARENT_SCOPE_INVALID" in sql
    assert "WEB_MENU_LINEAGE_SELF_PARENT" in sql
    assert "WEB_SUBMENU_EXPANDED" in sql
    assert "menu_page_type IS NULL" in sql
    assert "menu_interaction_outcome" in sql
    assert ") IS TRUE" in sql
    assert "budget.task_id" in sql
    assert "NEW.task_id" not in sql
    assert "CREATE FUNCTION qc_validate_web_submenu_evidence" in sql
    assert "action.status='completed'" in sql
    assert "action.finalization_epoch=session.finalization_epoch" in sql
    assert "action.requested_source_capture_id=NEW.menu_source_capture_id" in sql
    assert "after_capture.parent_capture_id=before_capture.id" in sql
    assert "after_capture.action_execution_id=action.action_execution_id" in sql
    assert "before_capture.capture_role='before_action'" in sql
    assert "before_capture.action_execution_id=action.action_execution_id" in sql
    assert "before_capture.parent_capture_id=action.requested_source_capture_id" in sql
    assert "before_capture.session_command_sequence=action.command_sequence" in sql
    assert "after_capture.capture_role='after_action'" in sql
    assert "after_capture.session_command_sequence=action.command_sequence" in sql
    assert "NEW.menu_probe_capture_id IS DISTINCT FROM NEW.menu_ready_capture_id" in sql
    assert "diagnostic->>'type' IS DISTINCT FROM 'WEB_SUBMENU_EXPANDED'" in sql
    assert "NEW.menu_page_type IS NOT NULL" in sql
    submenu_constraint = sql[sql.index("AND menu_page_type IS NULL") :]
    for field_name in (
        "detail_intent_id",
        "detail_source_capture_id",
        "detail_candidate_id",
        "detail_destination_sha256",
        "detail_action_execution_id",
        "detail_probe_capture_id",
        "detail_ready_capture_id",
        "detail_page_type",
    ):
        assert f"AND {field_name} IS NULL" in submenu_constraint
    assert "AND detail_selection = '{}'::jsonb" in submenu_constraint
    ordinary_constraint = sql[
        sql.index("AND menu_page_type IN (\n                    'content_list'") :
        sql.index("AND menu_page_type IS NULL")
    ]
    assert "AND detail_selection = '{}'::jsonb" in ordinary_constraint
    assert "normalization_version = 'web-page-type-assessment.v3'" in sql
    assert "menu_page_type = 'home'" in sql
    assert "normalization_version IN (" in sql
    assert "'web-page-type-normalization.v1'" in sql
    assert "'web-page-type-assessment.v1'" in sql
    assert "CREATE OR REPLACE FUNCTION qc_web_require_formal_home_type" in sql
    assert "assessment.schema_version=NEW.normalization_version" in sql


def test_migration_classifies_every_invalid_historical_runtime_parent_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, operation = _load_migration(monkeypatch)

    module.upgrade()
    sql = "\n".join(operation.statements)

    assert "child.parent_menu_entry_id=child.menu_entry_id" in sql
    assert "child.menu_depth > 1" in sql
    assert "parent.agent_run_id=child.agent_run_id" in sql
    assert "parent.site_id=child.site_id" in sql
    assert "parent.menu_depth + 1" in sql
    assert "jsonb_array_length(child.exploration_path)" in sql
    assert "> jsonb_array_length(parent.exploration_path)" in sql
    assert "IS DISTINCT FROM parent.exploration_path" in sql
    assert "parent.logical_target_identity" in sql
    assert "parent_logical_target_identity' IS NOT NULL" in sql
    assert "WEB_MENU_LINEAGE_BACKFILL_INVALID" in sql


def test_migration_downgrade_refuses_to_drop_runtime_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, operation = _load_migration(monkeypatch)
    module.downgrade()
    sql = "\n".join(operation.statements)

    assert "WEB_MENU_LINEAGE_DOWNGRADE_REQUIRES_NO_RUNTIME_RECORDS" in sql
    assert "DROP COLUMN logical_target_identity" in sql
    assert "diagnostic->>'type' IS DISTINCT FROM 'WEB_SUBMENU_EXPANDED'" not in sql


def test_persistent_summary_restores_runtime_lineage_and_parent_logical_identity() -> None:
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _build_summary,
    )

    path = [f"层级{index}" for index in range(1, 26)]
    summary = _build_summary(
        {
            "site_id": "example",
            "catalog_version": "test",
            "catalog_sha256": "a" * 64,
            "menu_total": 1,
            "menu_limit": 10,
            "normalization_version": "web-page-type-assessment.v3",
        },
        [
            {
                "menu_entry_id": "discovery:" + "b" * 64,
                "menu_path": ["层级25"],
                "canonical_url_sha256": "b" * 64,
                "status": "runtime_discovered_unreviewed",
                "source_kind": "runtime_discovered",
                "discovery_source_capture_id": "capture-25",
                "decision": "runtime_record_only",
                "execution_slot": None,
                "discovery_manifest": {
                    "candidate": {
                        "entry_id": "temporary-child",
                        "menu_position": 24,
                        "source_kind": "button",
                        "parent_logical_target_identity": "a" * 64,
                    }
                },
                "diagnostic": {"error_code": "WEB_DISCOVERY_DISABLED"},
                "parent_menu_entry_id": "discovery:" + "a" * 64,
                "menu_depth": 25,
                "exploration_path": path,
                "logical_target_identity": "b" * 64,
            }
        ],
    )

    record = summary.runtime_discovered_ledger[0]
    assert record.parent_menu_entry_id == "discovery:" + "a" * 64
    assert record.parent_logical_target_identity == "a" * 64
    assert record.menu_depth == 25
    assert record.exploration_path == tuple(path)
    assert record.logical_target_identity == "b" * 64
    assert record.execution is None


@pytest.mark.asyncio
async def test_record_discovery_inserts_lineage_atomically() -> None:
    from services.api.app.application.web_desktop_navigation import WebNavigationCandidate
    from services.api.app.infrastructure.db.web_runtime_discovery_repository import (
        record_discovery,
    )

    class Connection:
        statement = ""
        arguments: tuple[object, ...] = ()

        async def fetchrow(self, *_args):
            return {"state": "open"}

        async def execute(self, statement, *arguments):
            self.statement = statement
            self.arguments = arguments

    connection = Connection()
    candidate = WebNavigationCandidate(
        entry_id="temporary-child",
        label="子菜单",
        url="https://example.test/child",
        menu_path=("子菜单",),
        dom_fingerprint="dom-child",
        source_signal="semantic_nav",
        page_type_key="unknown",
        source_capture_id="00000000-0000-0000-0000-000000000004",
        parent_menu_entry_id="discovery:" + "a" * 64,
        parent_logical_target_identity="a" * 64,
        menu_depth=2,
        exploration_path=("父菜单", "子菜单"),
    )
    await record_discovery(
        connection,
        command=SimpleNamespace(
            task_id="00000000-0000-0000-0000-000000000001",
            agent_run_id="00000000-0000-0000-0000-000000000002",
            session_id="web:test",
        ),
        site=SimpleNamespace(site_id="example", entries=()),
        candidate=candidate,
        source_capture_id="00000000-0000-0000-0000-000000000004",
        error_code="WEB_DISCOVERY_PENDING",
    )

    assert "parent_menu_entry_id" in connection.statement
    assert "menu_depth" in connection.statement
    assert "exploration_path" in connection.statement
    assert "logical_target_identity" in connection.statement
    assert connection.arguments[-4:] == (
        "discovery:" + "a" * 64,
        2,
        '["父菜单", "子菜单"]',
        candidate.logical_target_identity,
    )


def test_budget_recovery_recognizes_only_verified_submenu_completion() -> None:
    from dataclasses import replace

    from services.api.app.application.web_menu_exploration import (
        WebMenuLedgerEntry,
        _runtime_discovery_execution_is_complete,
    )
    from services.api.app.domain.exploration import WebMenuTerminalStatus

    execution = WebMenuLedgerEntry(
        entry_id="discovery:" + "b" * 64,
        menu_path=("层级25",),
        canonical_url="sha256:" + "a" * 64,
        status=WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
        source_capture_id="capture-before",
        final_capture_id="capture-after",
        page_type=None,
        detail_capture_id=None,
        error_code=None,
        menu_action_count=1,
        menu_intent_id="intent-submenu",
        menu_action_execution_id="action-submenu",
        menu_probe_capture_id="capture-after",
        menu_ready_capture_id="capture-after",
        menu_page_type=None,
        detail_page_type=None,
        diagnostic_type="WEB_SUBMENU_EXPANDED",
        menu_interaction_outcome={
            "command_accepted": True,
            "effect_observed": True,
            "postcondition_satisfied": True,
            "transition_arrived": True,
            "after_capture_id": "capture-after",
            "failure_reason": None,
            "evidence_mode": "natural",
        },
        menu_depth=25,
        exploration_path=tuple(f"层级{index}" for index in range(1, 26)),
        logical_target_identity="b" * 64,
        submenu_relations_verified=False,
    )

    assert not _runtime_discovery_execution_is_complete(execution)
    assert _runtime_discovery_execution_is_complete(
        replace(execution, submenu_relations_verified=True)
    )
    assert not _runtime_discovery_execution_is_complete(
        replace(
            execution,
            menu_interaction_outcome={},
            submenu_relations_verified=True,
        )
    )


def test_summary_requires_repository_verified_submenu_relations() -> None:
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _build_summary,
    )

    base = {
        "menu_entry_id": "discovery:" + "c" * 64,
        "menu_path": ["父菜单"],
        "canonical_url_sha256": "d" * 64,
        "status": "menu_page_captured",
        "source_kind": "runtime_discovered",
        "discovery_source_capture_id": "capture-before",
        "decision": "runtime_observe_only",
        "execution_slot": 1,
        "discovery_manifest": {
            "candidate": {
                "entry_id": "temporary-parent",
                "label": "父菜单",
                "menu_position": 1,
                "source_kind": "button",
                "parent_logical_target_identity": None,
            }
        },
        "diagnostic": {
            "type": "WEB_SUBMENU_EXPANDED",
            "menu_interaction_outcome": {
                "command_accepted": True,
                "effect_observed": True,
                "postcondition_satisfied": True,
                "transition_arrived": True,
                "after_capture_id": "capture-after",
                "failure_reason": None,
                "evidence_mode": "natural",
            },
        },
        "menu_action_count": 1,
        "menu_intent_id": "intent-submenu",
        "menu_source_capture_id": "capture-before",
        "menu_action_execution_id": "action-submenu",
        "menu_probe_capture_id": "capture-after",
        "menu_ready_capture_id": "capture-after",
        "menu_page_type": None,
        "detail_action_count": 0,
        "detail_page_type": None,
        "parent_menu_entry_id": None,
        "menu_depth": 1,
        "exploration_path": ["父菜单"],
        "logical_target_identity": "c" * 64,
    }
    budget = {
        "site_id": "example",
        "catalog_version": "test",
        "catalog_sha256": "a" * 64,
        "menu_total": 1,
        "menu_limit": 10,
        "normalization_version": "web-page-type-assessment.v3",
    }

    rejected = _build_summary(budget, [{**base, "submenu_relations_verified": False}])
    accepted = _build_summary(budget, [{**base, "submenu_relations_verified": True}])

    assert rejected.runtime_discovered_ledger[0].execution is not None
    assert rejected.runtime_discovered_ledger[0].execution.status.value == "navigation_failed"
    assert accepted.runtime_discovered_ledger[0].execution is not None
    assert accepted.runtime_discovered_ledger[0].execution.status.value == "menu_page_captured"


def test_summary_keeps_ordinary_list_and_submenu_branches_mutually_exclusive() -> None:
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        _has_valid_success_evidence,
    )

    row = {
        "status": "menu_page_captured",
        "execution_slot": 1,
        "menu_action_count": 1,
        "menu_intent_id": "intent-menu",
        "menu_source_capture_id": "capture-source",
        "menu_action_execution_id": "action-menu",
        "menu_probe_capture_id": "capture-probe",
        "menu_ready_capture_id": "capture-ready",
        "menu_page_type": "content_list",
        "detail_action_count": 0,
        "detail_action_execution_id": None,
        "diagnostic": {},
        "submenu_relations_verified": False,
    }

    assert _has_valid_success_evidence(
        row,
        WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
    )
    row["diagnostic"] = {
        "type": "WEB_SUBMENU_EXPANDED",
        "menu_interaction_outcome": {
            "command_accepted": True,
            "effect_observed": True,
            "postcondition_satisfied": True,
            "transition_arrived": True,
            "after_capture_id": "capture-ready",
            "failure_reason": None,
            "evidence_mode": "natural",
        },
    }
    row["submenu_relations_verified"] = True

    assert not _has_valid_success_evidence(
        row,
        WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
    )


pytestmark_pg = pytest.mark.skipif(
    not os.getenv("ALIOTH_TEST_DATABASE_URL") or not os.getenv("ALIOTH_DATABASE_URL"),
    reason="需要显式双 DSN 和隔离数据库守卫",
)


def _set_isolated_database_revision(revision: str) -> None:
    """迁移用例显式建立自身基线，不依赖其他测试模块的 autouse fixture。"""

    test_dsn = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.environ["ALIOTH_DATABASE_URL"],
    )
    config = AlembicConfig(str(_ROOT / "services" / "api" / "migrations" / "alembic.ini"))
    previous = os.environ["ALIOTH_DATABASE_URL"]
    try:
        os.environ["ALIOTH_DATABASE_URL"] = test_dsn
        alembic_command.upgrade(config, "head")
        if revision != "head":
            alembic_command.downgrade(config, revision)
    finally:
        os.environ["ALIOTH_DATABASE_URL"] = previous


@pytestmark_pg
def test_real_postgres_has_lineage_columns_and_constraints() -> None:
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _database,
    )

    _set_isolated_database_revision("head")

    async def verify() -> None:
        async with _database() as (control, _pool, _other, _ids):
            columns = {
                row["column_name"]
                for row in await control.fetch(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema='public'
                      AND table_name='qc_web_menu_ledger_entry'
                    """
                )
            }
            assert columns >= {
                "parent_menu_entry_id",
                "menu_depth",
                "exploration_path",
                "logical_target_identity",
                "normalization_version",
            }

    asyncio.run(verify())


@pytestmark_pg
def test_real_postgres_accepts_verified_catalog_parent_and_rejects_invalid_shape() -> None:
    """Catalog 父行须已完成访问；子项仍必须满足深度、路径和父身份合同。"""
    from services.api.app.application.web_desktop_navigation import (
        WebNavigationCandidate,
    )
    from services.api.app.application.web_exploration_budget_ledger import (
        WebMenuLedgerTerminalRecord,
    )
    from services.api.app.domain.exploration import WebMenuTerminalStatus
    from services.api.app.infrastructure.db.web_exploration_budget_ledger_repository import (
        PostgresWebExplorationBudgetLedgerRepository,
    )
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _CATALOG_SHA256,
        _CATALOG_VERSION,
        _command,
        _database,
        _seed_completed_action_evidence,
        _site,
    )

    _set_isolated_database_revision("head")

    async def verify() -> None:
        async with _database() as (control, first_pool, _second_pool, ids):
            site = _site()
            command = _command(ids)
            repository = PostgresWebExplorationBudgetLedgerRepository(pool=first_pool)
            await repository.bootstrap(
                command,
                site,
                catalog_version=_CATALOG_VERSION,
                catalog_sha256=_CATALOG_SHA256,
            )
            pending_parent = site.entries[2]
            pending_child = WebNavigationCandidate(
                entry_id="temporary-pending-child",
                label="未访问子菜单",
                url="https://example.test/menu/2/child",
                menu_path=("未访问子菜单",),
                dom_fingerprint="dom-pending-child",
                source_signal="semantic_nav",
                page_type_key="nav-class:child",
                source_capture_id=ids["capture"],
                parent_menu_entry_id=pending_parent.entry_id,
                parent_logical_target_identity="c" * 64,
                menu_depth=2,
                exploration_path=(*pending_parent.menu_path, "未访问子菜单"),
            )
            with pytest.raises(asyncpg.CheckViolationError, match="PARENT_SCOPE_INVALID"):
                await repository.record_discovery(
                    command=command,
                    site=site,
                    candidate=pending_child,
                    source_capture_id=ids["capture"],
                    error_code="WEB_DISCOVERY_PENDING",
                )

            parent = site.entries[1]
            reservation = await repository.claim_menu(
                command=command,
                entry=parent,
                source_capture_id=ids["capture"],
            )
            assert reservation is not None
            probe_id, ready_id = await _seed_completed_action_evidence(
                control,
                ids,
                action_execution_id="pg-catalog-parent-menu",
                intent_id=reservation.intent_id,
                command_sequence=1,
                requested_source_capture_id=ids["capture"],
                parent_capture_id=ids["capture"],
            )
            await repository.finalize(
                WebMenuLedgerTerminalRecord(
                    task_id=ids["task"],
                    agent_run_id=ids["run"],
                    session_id=ids["session"],
                    site_id=site.site_id,
                    menu_entry_id=parent.entry_id,
                    status=WebMenuTerminalStatus.PAGE_TYPE_UNCLASSIFIED,
                    menu_action_execution_id="pg-catalog-parent-menu",
                    menu_probe_capture_id=probe_id,
                    menu_ready_capture_id=ready_id,
                    diagnostic={"error_code": "WEB_PAGE_TYPE_UNCLASSIFIED"},
                )
            )

            candidate = WebNavigationCandidate(
                entry_id="temporary-catalog-child",
                label="子菜单",
                url="https://example.test/menu/1/child",
                menu_path=("子菜单",),
                dom_fingerprint="dom-catalog-child",
                source_signal="semantic_nav",
                page_type_key="nav-class:child",
                source_capture_id=ready_id,
                parent_menu_entry_id=parent.entry_id,
                parent_logical_target_identity="a" * 64,
                menu_depth=2,
                exploration_path=(*parent.menu_path, "子菜单"),
            )
            # 动作落盘不等于会话已推进；先证明旧 current 不得授权新来源。
            from services.api.app.application.web_exploration_budget_ledger import (
                WebExplorationBudgetLedgerConflictError,
            )

            with pytest.raises(
                WebExplorationBudgetLedgerConflictError, match="WEB_DISCOVERY_SCOPE_INVALID",
            ):
                await repository.record_discovery(
                    command=command, site=site, candidate=candidate,
                    source_capture_id=ready_id, error_code="WEB_DISCOVERY_PENDING",
                )
            await control.execute(
                "UPDATE qc_surface_session SET current_capture_id=$1::uuid WHERE session_id=$2",
                ready_id, ids["session"],
            )
            await repository.record_discovery(
                command=command,
                site=site,
                candidate=candidate,
                source_capture_id=ready_id,
                error_code="WEB_DISCOVERY_PENDING",
            )
            persisted = await control.fetchrow(
                """
                SELECT parent_menu_entry_id, menu_depth, exploration_path,
                       discovery_manifest->'candidate'
                         ->>'parent_logical_target_identity' AS parent_identity
                FROM qc_web_menu_ledger_entry
                WHERE agent_run_id=$1::uuid AND site_id=$2
                  AND menu_entry_id=$3
                """,
                ids["run"],
                site.site_id,
                "discovery:" + candidate.logical_target_identity,
            )
            assert persisted is not None
            assert persisted["parent_menu_entry_id"] == parent.entry_id
            assert persisted["menu_depth"] == 2
            assert json.loads(persisted["exploration_path"]) == [
                *parent.menu_path,
                "子菜单",
            ]
            assert persisted["parent_identity"] == "a" * 64

            invalid = WebNavigationCandidate(
                entry_id="temporary-invalid-child",
                label="错误子菜单",
                url="https://example.test/menu/1/invalid",
                menu_path=("错误子菜单",),
                dom_fingerprint="dom-invalid-child",
                source_signal="semantic_nav",
                page_type_key="nav-class:child",
                source_capture_id=ready_id,
                parent_menu_entry_id=parent.entry_id,
                parent_logical_target_identity="b" * 64,
                menu_depth=2,
                exploration_path=("错误父项", "错误子菜单"),
            )
            with pytest.raises(asyncpg.CheckViolationError, match="PARENT_MISMATCH"):
                await repository.record_discovery(
                    command=command,
                    site=site,
                    candidate=invalid,
                    source_capture_id=ready_id,
                    error_code="WEB_DISCOVERY_PENDING",
                )

    asyncio.run(verify())


@pytestmark_pg
def test_real_postgres_upgrade_classifies_all_historical_parent_shapes() -> None:
    """0107 全坏形状均固定诊断，合法父链则原样保留。"""
    from services.api.tests.test_web_exploration_budget_ledger_postgres_integration import (
        _cleanup_web_budget_run,
        _seed_web_run,
    )

    test_dsn = require_isolated_test_database_url(
        os.environ["ALIOTH_TEST_DATABASE_URL"],
        os.environ["ALIOTH_DATABASE_URL"],
    ).replace("postgresql+asyncpg://", "postgresql://", 1)
    config = AlembicConfig(str(_ROOT / "services" / "api" / "migrations" / "alembic.ini"))
    previous = os.environ["ALIOTH_DATABASE_URL"]
    ids = {
        "tenant": "10000000-0000-0000-0000-000000000108",
        "user": "20000000-0000-0000-0000-000000000108",
        "ruleset": "30000000-0000-0000-0000-000000000108",
        "task": "40000000-0000-0000-0000-000000000108",
        "run": "50000000-0000-0000-0000-000000000108",
        "session": "web:lineage-upgrade-0108",
        "capture": "60000000-0000-0000-0000-000000000108",
    }
    _set_isolated_database_revision("head")

    async def cleanup_previous_attempt() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await connection.execute(
                "DELETE FROM qc_web_exploration_budget WHERE task_id=$1::uuid",
                ids["task"],
            )
            await _cleanup_web_budget_run(connection, ids)
        finally:
            await connection.close()

    asyncio.run(cleanup_previous_attempt())
    _set_isolated_database_revision("20260914_0113")
    other_run_id = "51000000-0000-0000-0000-000000000108"
    other_capture_id = "61000000-0000-0000-0000-000000000108"
    identities = {name: f"{index:x}" * 64 for index, name in enumerate((
        "valid_parent", "valid_child", "missing_parent", "root_parent_logical",
        "self_parent", "cross_run", "cross_run_parent", "cross_site",
        "cross_site_parent", "wrong_depth", "wrong_prefix", "wrong_parent_logical",
    ), start=1)}

    def entry(name: str) -> str:
        return "discovery:" + identities[name]

    rows = (
        ("valid_parent", "合法父", 1, ["合法父"], None, None),
        (
            "valid_child",
            "合法子",
            2,
            ["合法父", "合法子"],
            entry("valid_parent"),
            identities["valid_parent"],
        ),
        ("missing_parent", "缺父子项", 2, ["缺失父", "缺父子项"], None, None),
        ("root_parent_logical", "错误根", 1, ["错误根"], None, identities["valid_parent"]),
        (
            "self_parent",
            "自父",
            2,
            ["自父", "自父"],
            entry("self_parent"),
            identities["self_parent"],
        ),
        (
            "cross_run",
            "跨运行",
            2,
            ["跨运行父", "跨运行"],
            entry("cross_run_parent"),
            identities["cross_run_parent"],
        ),
        (
            "cross_site",
            "跨站点",
            2,
            ["跨站点父", "跨站点"],
            entry("cross_site_parent"),
            identities["cross_site_parent"],
        ),
        (
            "wrong_depth",
            "深度错",
            3,
            ["合法父", "深度错"],
            entry("valid_parent"),
            identities["valid_parent"],
        ),
        (
            "wrong_prefix",
            "前缀错",
            2,
            ["其他父", "前缀错"],
            entry("valid_parent"),
            identities["valid_parent"],
        ),
        (
            "wrong_parent_logical",
            "父身份错",
            2,
            ["合法父", "父身份错"],
            entry("valid_parent"),
            identities["missing_parent"],
        ),
    )

    async def seed_0107() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            await _seed_web_run(connection, ids)
            await connection.execute(
                """
                INSERT INTO qc_agent_run (id, task_id, agent_type, agent_name, status)
                VALUES ($1::uuid, $2::uuid, 'main', 'Lineage Other Run', 'running')
                """,
                other_run_id,
                ids["task"],
            )
            await connection.execute(
                """
                INSERT INTO qc_capture (
                    id, task_id, agent_run_id, surface_kind, surface_session_id,
                    capture_role, capture_status, captured_at
                ) VALUES ($1::uuid, $2::uuid, $3::uuid, 'web', $4,
                          'probe_before', 'captured', now())
                """,
                other_capture_id,
                ids["task"],
                other_run_id,
                ids["session"],
            )
            for run_id, site_id in (
                (ids["run"], "lineage-upgrade"),
                (other_run_id, "lineage-upgrade"),
                (ids["run"], "lineage-other-site"),
            ):
                await connection.execute(
                    """
                    INSERT INTO qc_web_exploration_budget (
                        agent_run_id, site_id, task_id, session_id,
                        target_origin_sha256, catalog_version, catalog_sha256
                    ) VALUES ($1::uuid, $2, $3::uuid, $4, $5, '0107-fixture', $6)
                    """,
                    run_id, site_id, ids["task"], ids["session"], "a" * 64, "b" * 64,
                )

            async def insert_runtime(
                *, name, label, depth, path, parent_id, parent_logical,
                run_id=ids["run"], site_id="lineage-upgrade", capture_id=ids["capture"],
            ) -> None:
                manifest = {
                    "version": "web-discovery-manifest.v1",
                    "entry_id": entry(name),
                    "candidate": {
                        "entry_id": f"historical-{name}",
                        "label": label,
                        "menu_depth": depth,
                        "exploration_path": path,
                        "parent_menu_entry_id": parent_id,
                        "parent_logical_target_identity": parent_logical,
                        "logical_target_identity": identities[name],
                    },
                }
                await connection.execute(
                    """
                    INSERT INTO qc_web_menu_ledger_entry (
                        agent_run_id, site_id, menu_entry_id, catalog_position,
                        decision, menu_path, canonical_url_sha256, status,
                        source_kind, discovery_source_capture_id, discovery_manifest,
                        diagnostic
                    ) VALUES ($1::uuid, $2, $3, 0, 'runtime_record_only', $4::jsonb,
                              $5, 'runtime_discovered_unreviewed', 'runtime_discovered',
                              $6::uuid, $7::jsonb, '{}'::jsonb)
                    """,
                    run_id, site_id, entry(name), json.dumps([label], ensure_ascii=False),
                    identities[name], capture_id, json.dumps(manifest, ensure_ascii=False),
                )

            await insert_runtime(
                name="cross_run_parent", label="跨运行父", depth=1,
                path=["跨运行父"], parent_id=None, parent_logical=None,
                run_id=other_run_id, capture_id=other_capture_id,
            )
            await insert_runtime(
                name="cross_site_parent", label="跨站点父", depth=1,
                path=["跨站点父"], parent_id=None, parent_logical=None,
                site_id="lineage-other-site",
            )
            for name, label, depth, path, parent_id, parent_logical in rows:
                await insert_runtime(
                    name=name, label=label, depth=depth, path=path,
                    parent_id=parent_id, parent_logical=parent_logical,
                )
        finally:
            await connection.close()

    async def verify_and_cleanup() -> None:
        connection = await asyncpg.connect(test_dsn)
        try:
            result = await connection.fetch(
                """
                SELECT menu_entry_id, parent_menu_entry_id, menu_depth, exploration_path,
                       logical_target_identity, diagnostic->>'error_code' AS error_code,
                       discovery_manifest->'candidate'
                         ->>'parent_logical_target_identity' AS source_parent_identity
                FROM qc_web_menu_ledger_entry
                WHERE agent_run_id=$1::uuid AND site_id='lineage-upgrade'
                ORDER BY menu_entry_id
                """,
                ids["run"],
            )
            actual = {row["menu_entry_id"]: dict(row) for row in result}
            assert actual[entry("valid_parent")]["parent_menu_entry_id"] is None
            assert actual[entry("valid_parent")]["error_code"] is None
            assert actual[entry("valid_child")]["parent_menu_entry_id"] == entry("valid_parent")
            assert actual[entry("valid_child")]["error_code"] is None
            for name, *_rest in rows:
                if name in {"valid_parent", "valid_child"}:
                    continue
                assert actual[entry(name)]["parent_menu_entry_id"] is None
                assert actual[entry(name)]["error_code"] == "WEB_MENU_LINEAGE_UNAVAILABLE"
            assert actual[entry("root_parent_logical")]["menu_depth"] == 1
            assert (
                actual[entry("root_parent_logical")]["source_parent_identity"]
                == identities["valid_parent"]
            )
            assert json.loads(actual[entry("missing_parent")]["exploration_path"]) == [
                "缺失父",
                "缺父子项",
            ]
            assert await connection.fetchval(
                """
                SELECT count(*) FROM qc_web_menu_ledger_entry AS child
                WHERE child.source_kind='runtime_discovered'
                  AND child.diagnostic->>'error_code'
                    IS DISTINCT FROM 'WEB_MENU_LINEAGE_UNAVAILABLE'
                  AND (
                  (child.parent_menu_entry_id IS NULL AND child.menu_depth > 1)
                  OR (child.parent_menu_entry_id IS NULL AND child.menu_depth=1
                    AND child.discovery_manifest->'candidate'
                      ->>'parent_logical_target_identity' IS NOT NULL)
                  OR child.parent_menu_entry_id=child.menu_entry_id
                  OR (child.parent_menu_entry_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM qc_web_menu_ledger_entry AS parent
                    WHERE parent.agent_run_id=child.agent_run_id
                      AND parent.site_id=child.site_id
                      AND parent.menu_entry_id=child.parent_menu_entry_id
                      AND parent.source_kind='runtime_discovered'
                      AND child.menu_depth=parent.menu_depth+1
                      AND jsonb_array_length(child.exploration_path)
                          > jsonb_array_length(parent.exploration_path)
                      AND NOT EXISTS (SELECT 1 FROM generate_series(
                          0, jsonb_array_length(parent.exploration_path)-1) AS i
                        WHERE child.exploration_path->>i
                          IS DISTINCT FROM parent.exploration_path->>i)
                      AND child.discovery_manifest->'candidate'
                          ->>'parent_logical_target_identity'=parent.logical_target_identity))
                )
                """
            ) == 0
            await connection.execute(
                "DELETE FROM qc_web_exploration_budget WHERE task_id=$1::uuid",
                ids["task"],
            )
            await _cleanup_web_budget_run(connection, ids)
        finally:
            await connection.close()

    try:
        os.environ["ALIOTH_DATABASE_URL"] = test_dsn
        asyncio.run(seed_0107())
        alembic_command.upgrade(config, "20260914_0114")
        asyncio.run(verify_and_cleanup())
    finally:
        alembic_command.upgrade(config, "head")
        os.environ["ALIOTH_DATABASE_URL"] = previous
