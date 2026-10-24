"""为 pass 清单关联的证据附件与清单本身增加不可原地变更约束。

Revision ID: 20260814_0056
Revises: 20260814_0055
Create Date: 2026-08-14

E25：pass 清单指向的附件不可原地变更；补证只生成更高版本清单并保留旧清单。

两道守卫（均只拦 **UPDATE**，即"原地变更"）：
1. `qc_evidence_artifact`：被 **pass** 清单引用的附件禁止改动身份列
   （task_id/page_capture_id/artifact_type/storage_path/checksum/size_bytes）。
   metadata 仍可维护——它不是证据本体，运维标注不应被误伤。
2. `qc_evidence_manifest`：已落库清单禁止改写内容列
   （manifest_version/manifest_hash/payload_hash/quality_status/
   quality_reason/manifest_payload）。补证与裁决推进必须**追加更高版本**，
   旧清单原样保留。幂等写入路径（ON CONFLICT DO UPDATE SET manifest_id =
   自身）不改动任何内容列，因此不受影响。

**为什么不拦 DELETE**：`qc_evidence_manifest.capture_id` 与
`qc_evidence_artifact.page_capture_id` 都挂 ON DELETE CASCADE，任务/采集的
生命周期清理依赖级联；在子表 BEFORE DELETE 里无条件报错会把 task 清理一起打死。
行删除由归属方的级联生命周期治理，生产链路本身没有任何删除证据行的路径
（对象存储补偿只删未落库的对象）。若后续要连删除一并锁死，需针对级联场景
单独设计并在真实 PostgreSQL 上验证。
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0056"
down_revision = "20260814_0055"
branch_labels = None
depends_on = None

_ARTIFACT_IDENTITY_COLUMNS = (
    "task_id",
    "page_capture_id",
    "artifact_type",
    "storage_path",
    "checksum",
    "size_bytes",
)

_MANIFEST_CONTENT_COLUMNS = (
    "manifest_version",
    "manifest_hash",
    "payload_hash",
    "quality_status",
    "quality_reason",
    "manifest_payload",
)


def _changed(columns: tuple[str, ...]) -> str:
    return " OR ".join(
        f"NEW.{column} IS DISTINCT FROM OLD.{column}" for column in columns
    )


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION qc_guard_pass_manifest_artifact()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        DECLARE
            referenced boolean;
        BEGIN
            IF NOT ({_changed(_ARTIFACT_IDENTITY_COLUMNS)}) THEN
                RETURN NEW;
            END IF;
            SELECT EXISTS (
                SELECT 1
                FROM qc_evidence_manifest AS manifest
                WHERE manifest.capture_id = OLD.page_capture_id
                  AND manifest.quality_status = 'pass'
                  AND manifest.manifest_payload -> 'artifacts' @> jsonb_build_array(
                      jsonb_build_object('artifact_id', OLD.id::text)
                  )
            ) INTO referenced;
            IF referenced THEN
                RAISE EXCEPTION
                    'evidence artifact % is referenced by a passed manifest and is immutable',
                    OLD.id
                USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_evidence_artifact_pass_manifest_guard
        BEFORE UPDATE ON qc_evidence_artifact
        FOR EACH ROW EXECUTE FUNCTION qc_guard_pass_manifest_artifact()
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION qc_guard_evidence_manifest_immutability()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF {_changed(_MANIFEST_CONTENT_COLUMNS)} THEN
                RAISE EXCEPTION
                    'evidence manifest % is immutable; publish a higher version instead',
                    OLD.manifest_id
                USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_evidence_manifest_immutability_guard
        BEFORE UPDATE ON qc_evidence_manifest
        FOR EACH ROW EXECUTE FUNCTION qc_guard_evidence_manifest_immutability()
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_evidence_manifest_immutability_guard "
        "ON qc_evidence_manifest"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_evidence_manifest_immutability()")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_evidence_artifact_pass_manifest_guard "
        "ON qc_evidence_artifact"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_pass_manifest_artifact()")
