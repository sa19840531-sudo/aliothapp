"""强制证据附件身份并持久化证据门禁业务结果。

Revision ID: 20260813_0049
Revises: 20260812_0048
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op

revision = "20260813_0049"
down_revision = "20260812_0048"
branch_labels = None
depends_on = None

_ARTIFACT_TYPES = (
    "'screenshot', 'dom', 'visible_text', 'ocr_text', 'har', "
    "'console_log', 'media_state', 'ai_io', 'report', "
    "'action_trace', 'ui_snapshot', 'temporal_trace', 'page_coverage', 'ui_tree'"
)
_LEGACY_ARTIFACT_TYPES = (
    "'screenshot', 'dom', 'visible_text', 'ocr_text', 'har', "
    "'console_log', 'media_state', 'ai_io', 'report', "
    "'action_trace', 'ui_snapshot', 'temporal_trace', 'page_coverage'"
)


def upgrade() -> None:
    _upgrade_artifact_identity()
    _upgrade_exit_gate_outcome()


def downgrade() -> None:
    _downgrade_artifact_identity()
    _downgrade_exit_gate_outcome()


def _upgrade_artifact_identity() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_evidence_artifact
                WHERE storage_path IS NULL
                   OR btrim(storage_path) = ''
                   OR checksum IS NULL
                   OR checksum !~ '^sha256:[0-9a-f]{64}$'
                   OR size_bytes IS NULL OR size_bytes < 0
            ) THEN
                RAISE EXCEPTION
                    'qc_evidence_artifact contains unresolved trusted identity rows; '
                    'run backfill_evidence_artifact_identity.py first';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT IF EXISTS ck_qc_evidence_artifact_size"
    )
    for name, expression in (
        ("ck_qc_evidence_artifact_path_nonblank", "btrim(storage_path) <> ''"),
        (
            "ck_qc_evidence_artifact_checksum_format",
            "checksum ~ '^sha256:[0-9a-f]{64}$'",
        ),
        ("ck_qc_evidence_artifact_size", "size_bytes >= 0"),
        ("ck_qc_evidence_artifact_checksum_nn", "checksum IS NOT NULL"),
        ("ck_qc_evidence_artifact_size_nn", "size_bytes IS NOT NULL"),
    ):
        op.execute(
            f"ALTER TABLE qc_evidence_artifact ADD CONSTRAINT {name} "
            f"CHECK ({expression}) NOT VALID"
        )
    for name in (
        "ck_qc_evidence_artifact_path_nonblank",
        "ck_qc_evidence_artifact_checksum_format",
        "ck_qc_evidence_artifact_size",
        "ck_qc_evidence_artifact_checksum_nn",
        "ck_qc_evidence_artifact_size_nn",
    ):
        op.execute(
            f"ALTER TABLE qc_evidence_artifact VALIDATE CONSTRAINT {name}"
        )
    op.execute(
        "ALTER TABLE qc_evidence_artifact ALTER COLUMN checksum SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact ALTER COLUMN size_bytes SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_checksum_nn"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_size_nn"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_type"
    )
    op.execute(
        f"""
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type
        CHECK (artifact_type IN ({_ARTIFACT_TYPES}))
        """
    )


def _downgrade_artifact_identity() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM qc_evidence_artifact WHERE artifact_type = 'ui_tree'
            ) THEN
                RAISE EXCEPTION
                    'cannot downgrade while ui_tree evidence artifacts exist';
            END IF;
        END
        $$
        """
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_type"
    )
    op.execute(
        f"""
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_type
        CHECK (artifact_type IN ({_LEGACY_ARTIFACT_TYPES}))
        """
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact ALTER COLUMN checksum DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact ALTER COLUMN size_bytes DROP NOT NULL"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_path_nonblank"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_checksum_format"
    )
    op.execute(
        "ALTER TABLE qc_evidence_artifact "
        "DROP CONSTRAINT ck_qc_evidence_artifact_size"
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_artifact
        ADD CONSTRAINT ck_qc_evidence_artifact_size
        CHECK (size_bytes IS NULL OR size_bytes >= 0)
        """
    )


def _upgrade_exit_gate_outcome() -> None:
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        ADD COLUMN manifest_id varchar(96)
            REFERENCES qc_evidence_manifest(manifest_id) ON DELETE RESTRICT,
        ADD COLUMN manifest_version integer,
        ADD COLUMN manifest_hash varchar(128),
        ADD COLUMN manifest_quality_status varchar(16),
        ADD COLUMN business_outcome varchar(64),
        ADD COLUMN outcome_payload jsonb
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        ADD CONSTRAINT ck_qc_evidence_exit_gate_manifest_lineage CHECK (
            (
                manifest_id IS NULL
                AND manifest_version IS NULL
                AND manifest_hash IS NULL
                AND manifest_quality_status IS NULL
            )
            OR (
                manifest_id IS NOT NULL
                AND manifest_version > 0
                AND manifest_hash IS NOT NULL
                AND manifest_quality_status IN ('pending', 'pass', 'fail')
            )
        ) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        ADD CONSTRAINT ck_qc_evidence_exit_gate_business_outcome CHECK (
            business_outcome IS NULL
            OR business_outcome IN (
                'evidence_insufficient',
                'evidence_pending',
                'control_plane_incomplete',
                'evidence_integrity_failed'
            )
        ) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        ADD CONSTRAINT ck_qc_evidence_exit_gate_outcome_pair CHECK (
            (
                allow_leave
                AND business_outcome IS NULL
                AND outcome_payload IS NULL
            )
            OR (
                NOT allow_leave
                AND business_outcome IS NOT NULL
                AND jsonb_typeof(outcome_payload) = 'object'
            )
        ) NOT VALID
        """
    )
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        ADD CONSTRAINT ck_qc_evidence_exit_gate_insufficient_lineage CHECK (
            business_outcome IS NULL
            OR NOT (business_outcome = 'evidence_insufficient')
            OR (
                manifest_id IS NOT NULL
                AND manifest_version IS NOT NULL
                AND manifest_hash IS NOT NULL
                AND manifest_quality_status = 'fail'
                AND requirement_set_id IS NOT NULL
            )
        ) NOT VALID
        """
    )


def _downgrade_exit_gate_outcome() -> None:
    for name in (
        "ck_qc_evidence_exit_gate_insufficient_lineage",
        "ck_qc_evidence_exit_gate_outcome_pair",
        "ck_qc_evidence_exit_gate_business_outcome",
        "ck_qc_evidence_exit_gate_manifest_lineage",
    ):
        op.execute(
            "ALTER TABLE qc_evidence_exit_gate_evaluation "
            f"DROP CONSTRAINT IF EXISTS {name}"
        )
    op.execute(
        """
        ALTER TABLE qc_evidence_exit_gate_evaluation
        DROP COLUMN IF EXISTS outcome_payload,
        DROP COLUMN IF EXISTS business_outcome,
        DROP COLUMN IF EXISTS manifest_quality_status,
        DROP COLUMN IF EXISTS manifest_hash,
        DROP COLUMN IF EXISTS manifest_version,
        DROP COLUMN IF EXISTS manifest_id
        """
    )
