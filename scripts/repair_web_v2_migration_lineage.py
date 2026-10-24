"""维护窗口内兼容旧 Web 0115 迁移链；默认仅审计，绝不直接 stamp。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from scripts.web_v2_schema_manifest import EXPECTED_MANIFEST, schema_manifest

LEGACY = "20260915_0115"
TARGET = "20260916_0121"
VERSIONS = Path(__file__).resolve().parents[1] / "services/api/migrations/versions"
MISSING = (
    "20260911_0107_ruleset_phase_split.py",
    "20260911_0108_aggregate_candidate_evidence_insufficient.py",
    "20260911_0109_evidence_supplement_input_set.py",
    "20260912_0110_h5_gate_coverage_binding.py",
    "20260912_0111_rule_test_record.py",
    "20260912_0112_rule_test_record_second_location.py",
)
PROTECTED = (
    "qc_web_run_stage_checkpoint",
    "qc_web_menu_ledger_entry",
    "qc_web_exploration_budget",
    "qc_web_coverage_snapshot",
    "qc_page_capture",
)


def load_migration(filename):
    spec = importlib.util.spec_from_file_location("lineage_" + filename[:-3], VERSIONS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_audit(audit):
    versions = audit["versions"]
    if versions not in ([LEGACY], [TARGET]):
        raise RuntimeError("未知或多头迁移版本；拒绝自动修复，请使用对应正式迁移路径")
    if not audit["web_checks"] or not all(audit["web_checks"].values()):
        raise RuntimeError("Web 迁移结构不完整，拒绝自动修复")
    additions = audit["addition_checks"]
    if versions == [TARGET] and additions and all(additions.values()):
        return "already_current"
    if versions == [LEGACY] and additions and not any(additions.values()):
        return "repair_required"
    raise RuntimeError("新增迁移存在部分落库或版本漂移，拒绝自动修复")


def audit(connection):
    inspector_columns = connection.execute(
        text("""
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema=current_schema()
    """)
    ).all()
    columns = set(inspector_columns)
    constraints = dict(
        connection.execute(
            text("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE connamespace=current_schema()::regnamespace
    """)
        ).all()
    )
    functions = set(
        connection.execute(
            text("""
        SELECT proname FROM pg_proc WHERE pronamespace=current_schema()::regnamespace
    """)
        ).scalars()
    )
    web_checks = {name: any(table == name for table, _ in columns) for name in PROTECTED}
    for name in (
        "ck_qc_web_checkpoint_error_audit",
        "ck_qc_web_checkpoint_reference_only",
        "ck_qc_web_checkpoint_gap_audit",
        "ck_qc_web_menu_lineage_shape",
        "ck_qc_web_menu_success_evidence",
        "ck_qc_web_coverage_applicability_snapshot",
    ):
        web_checks[name] = name in constraints
    for name in (
        "parent_menu_entry_id",
        "menu_depth",
        "exploration_path",
        "logical_target_identity",
        "normalization_version",
    ):
        web_checks[name] = ("qc_web_menu_ledger_entry", name) in columns
    web_checks["v4"] = "web-page-type-assessment.v4" in constraints.get(
        "ck_qc_web_coverage_snapshot_normalization", ""
    )
    for name in (
        "qc_web_require_formal_home_type",
        "qc_validate_web_menu_lineage",
        "qc_validate_web_submenu_evidence",
        "qc_enforce_web_menu_execution_slot",
        "qc_web_checkpoint_gap_audit_valid",
    ):
        web_checks[name] = name in functions
    observed_manifest = schema_manifest(connection)
    for category, expected in EXPECTED_MANIFEST.items():
        web_checks["manifest_" + category] = observed_manifest.get(category) == expected
    additions = {
        "ruleset_members": ("qc_ruleset", "member_ruleset_ids") in columns,
        "ruleset_scope": "qc_ruleset_scope" in functions,
        "composite": "composite" in constraints.get("ck_qc_ruleset_source_type", ""),
        "aggregate_status": "evidence_insufficient"
        in constraints.get("ck_qc_aggregate_candidate_status", ""),
        "aggregate_links": "evidence_insufficient"
        in constraints.get("ck_qc_aggregate_candidate_links", ""),
        "supplement_input": ("qc_evidence_supplement", "evidence_input_set_id") in columns,
        "supplement_fk": "fk_qc_evidence_supplement_input_set" in constraints,
        "coverage_identity": "uq_qc_h5_coverage_task_identity" in constraints,
        "gate_fk": "fk_qc_gate_h5_coverage" in constraints,
        "gate_check": "ck_qc_gate_h5_coverage_identity" in constraints,
        "rule_test": ("qc_rule_test_record", "id") in columns,
        "second_location": ("qc_rule_test_record", "second_location_text") in columns,
    }
    for name in (
        "h5_coverage_snapshot_id",
        "h5_coverage_snapshot_hash",
        "h5_coverage_snapshot_epoch",
    ):
        additions[name] = ("qc_completion_gate_snapshot", name) in columns
    return {
        "versions": list(
            connection.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).scalars()
        ),
        "web_checks": web_checks,
        "addition_checks": additions,
    }


def protected_fingerprints(connection):
    # 表名固定白名单；逐行摘要后排序，避免把 DOM 或截图内容载入审计输出。
    return {
        table: tuple(
            connection.execute(
                text(f"SELECT md5(to_jsonb(t)::text) FROM {table} t ORDER BY 1")
            ).scalars()
        )
        for table in PROTECTED
    }


def repair(connection, *, apply=False, writers_stopped=False):
    if apply and not writers_stopped:
        raise RuntimeError("执行修复前必须停止 API/worker 等写入方并确认 --writers-stopped")
    if apply:
        connection.execute(text("SET LOCAL lock_timeout='10s'"))
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('alioth:web-v2-lineage-repair'))")
        )
        connection.execute(text("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE"))
    before = audit(connection)
    status = validate_audit(before)
    if not apply or status == "already_current":
        return {"status": status, "applied": False, "before": before}
    connection.execute(text("LOCK TABLE " + ",".join(PROTECTED) + " IN SHARE MODE"))
    fingerprints = protected_fingerprints(connection)
    digests = {}
    with Operations.context(MigrationContext.configure(connection)):
        for filename in MISSING:
            digests[filename] = hashlib.sha256((VERSIONS / filename).read_bytes()).hexdigest()
            load_migration(filename).upgrade()
    after = audit(connection)
    if not all(after["web_checks"].values()) or not all(after["addition_checks"].values()):
        raise RuntimeError("补齐迁移后的结构检查失败，事务必须回滚")
    if protected_fingerprints(connection) != fingerprints:
        raise RuntimeError("Web 证据或 checkpoint 数据发生变化，事务必须回滚")
    changed = connection.execute(
        text("UPDATE alembic_version SET version_num=:target WHERE version_num=:legacy"),
        {"target": TARGET, "legacy": LEGACY},
    )
    if changed.rowcount != 1:
        raise RuntimeError("版本 CAS 失败，事务必须回滚")
    return {
        "status": validate_audit(audit(connection)),
        "applied": True,
        "from_version": LEGACY,
        "to_version": TARGET,
        "migration_sha256": digests,
    }


async def run(*, apply=False, writers_stopped=False):
    url = os.environ.get("ALIOTH_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("缺少 ALIOTH_DATABASE_URL；不读取隐式默认库")
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://", 1))
    try:
        async with engine.begin() as connection:
            return await connection.run_sync(
                lambda sync: repair(sync, apply=apply, writers_stopped=writers_stopped)
            )
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(run(apply=args.apply, writers_stopped=args.writers_stopped))
    except Exception as error:
        # 数据库异常可能包含连接参数、业务值；终端只输出错误类型，不泄露凭据。
        parser.exit(1, f"迁移兼容失败，未提交事务：{type(error).__name__}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
