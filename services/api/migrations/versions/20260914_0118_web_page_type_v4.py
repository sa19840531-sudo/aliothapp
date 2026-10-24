"""同步 Web v4 页型协议的数据库门禁。

Revision ID: 20260914_0118
Revises: 20260914_0117
"""

from __future__ import annotations

from alembic import op

revision = "20260914_0118"
down_revision = "20260914_0117"
branch_labels = None
depends_on = None


def _replace_coverage_guards(*, allow_v4: bool) -> None:
    normalization_versions = (
        "'web-page-type-normalization.v1', "
        "'web-page-type-assessment.v1', "
        "'web-page-type-assessment.v2', "
        "'web-page-type-assessment.v3'"
        + (", 'web-page-type-assessment.v4'" if allow_v4 else "")
    )
    snapshot_protocols = (
        "'web-coverage-snapshot.v2', 'web-coverage-snapshot.v3'"
        + (", 'web-coverage-snapshot.v4'" if allow_v4 else "")
    )
    op.execute(
        f"""
        ALTER TABLE qc_web_coverage_snapshot
        DROP CONSTRAINT ck_qc_web_coverage_snapshot_normalization,
        ADD CONSTRAINT ck_qc_web_coverage_snapshot_normalization CHECK (
            normalization_version IN ({normalization_versions})
        )
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_web_coverage_snapshot
        DROP CONSTRAINT IF EXISTS ck_qc_web_coverage_applicability_snapshot,
        ADD CONSTRAINT ck_qc_web_coverage_applicability_snapshot
        CHECK (
            applicability_snapshot IS NULL
            OR (
                jsonb_typeof(applicability_snapshot) = 'object'
                AND jsonb_typeof(
                    applicability_snapshot->'required_page_types'
                ) = 'array'
                AND jsonb_typeof(
                    applicability_snapshot->'optional_page_types'
                ) = 'array'
                AND jsonb_typeof(
                    applicability_snapshot->'not_applicable_page_types'
                ) = 'array'
                AND NULLIF(
                    BTRIM(applicability_snapshot->>'applicability_mode'),
                    ''
                ) IS NOT NULL
                AND NULLIF(
                    BTRIM(applicability_snapshot->>'catalog_version'),
                    ''
                ) IS NOT NULL
                AND NULLIF(
                    BTRIM(applicability_snapshot->>'catalog_hash'),
                    ''
                ) IS NOT NULL
                AND applicability_snapshot->>'snapshot_protocol_version'
                    IN ({snapshot_protocols})
            )
        )
        """
    )


def _replace_home_guard(*, allow_v4: bool) -> None:
    formal_versions = (
        "'web-page-type-assessment.v3', 'web-page-type-assessment.v4'"
        if allow_v4
        else "'web-page-type-assessment.v3'"
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION qc_web_require_formal_home_type() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.status = 'home_baseline'
               AND NEW.status = OLD.status
               AND NEW.menu_page_type IS NOT DISTINCT FROM OLD.menu_page_type
               AND NEW.normalization_version IS NOT DISTINCT FROM OLD.normalization_version THEN
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.normalization_version IS NOT NULL
               AND NEW.normalization_version IS DISTINCT FROM OLD.normalization_version THEN
                RAISE EXCEPTION 'WEB_HOME_NORMALIZATION_VERSION_IMMUTABLE'
                    USING ERRCODE='23514';
            END IF;
            IF NEW.status = 'home_baseline'
               AND (
                 NOT (
                   (
                     NEW.normalization_version IN ({formal_versions})
                     AND NEW.menu_page_type='home'
                   )
                   OR (
                     NEW.normalization_version IN (
                       'web-page-type-normalization.v1',
                       'web-page-type-assessment.v1',
                       'web-page-type-assessment.v2'
                     )
                     AND NEW.menu_page_type IN (
                       'content_list','video_list','content_detail','video_detail'
                     )
                   )
                 )
                 OR NOT EXISTS (
                   SELECT 1
                   FROM qc_web_exploration_budget AS budget
                   LEFT JOIN qc_h5_page_type_assessment AS assessment
                     ON assessment.task_id=budget.task_id
                    AND assessment.agent_run_id=budget.agent_run_id
                    AND assessment.session_id=budget.session_id
                    AND assessment.page_capture_id=NEW.menu_ready_capture_id
                    AND assessment.page_type=NEW.menu_page_type
                    AND assessment.schema_version=NEW.normalization_version
                   LEFT JOIN qc_web_coverage_snapshot AS coverage
                     ON coverage.task_id=budget.task_id
                    AND coverage.agent_run_id=budget.agent_run_id
                    AND coverage.normalization_version=NEW.normalization_version
                   WHERE budget.agent_run_id=NEW.agent_run_id
                     AND budget.site_id=NEW.site_id
                     AND (assessment.assessment_record_id IS NOT NULL OR coverage.id IS NOT NULL)
                 )
               ) THEN
                RAISE EXCEPTION 'WEB_HOME_NORMALIZATION_VERSION_INVALID'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )


def upgrade() -> None:
    """允许新 Web Run 按 v4 页型与 Coverage 协议落库。"""

    _replace_coverage_guards(allow_v4=True)
    _replace_home_guard(allow_v4=True)


def downgrade() -> None:
    """存在不可变 v4 证据时拒绝降级，否则恢复 v3 门禁。"""

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM qc_web_coverage_snapshot
            WHERE normalization_version='web-page-type-assessment.v4'
               OR applicability_snapshot->>'snapshot_protocol_version'
                  = 'web-coverage-snapshot.v4'
          ) OR EXISTS (
            SELECT 1 FROM qc_web_menu_ledger_entry
            WHERE normalization_version='web-page-type-assessment.v4'
          ) OR EXISTS (
            SELECT 1 FROM qc_h5_page_type_assessment
            WHERE schema_version='web-page-type-assessment.v4'
          ) THEN
            RAISE EXCEPTION 'Cannot downgrade immutable Web v4 evidence';
          END IF;
        END $$
        """
    )
    _replace_home_guard(allow_v4=False)
    _replace_coverage_guards(allow_v4=False)
