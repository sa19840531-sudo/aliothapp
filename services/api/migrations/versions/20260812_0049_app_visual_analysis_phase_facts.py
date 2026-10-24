"""持久化 App 视觉分析阶段与覆盖事实。

Revision ID: 20260812_0049
Revises: 20260812_0048
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "20260812_0049"
down_revision = "20260812_0048"
branch_labels = None
depends_on = None

_PAGE_STATES = (
    "'unknown', 'stable', 'normal', 'loaded', 'loading', 'line_loading', "
    "'content_available', 'transitional', 'blocked', 'startup_ad', 'countdown', "
    "'popup', 'modal', 'overlay', 'dialog', 'auth_gate', 'auth_required', "
    "'login_required', 'requires_auth', 'system_dialog', 'permission_dialog', "
    "'browser_permission'"
)


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_app_visual_observation
        ADD COLUMN page_state varchar(64),
        ADD COLUMN overlay_signature text,
        ADD COLUMN surface_facts jsonb
        """
    )
    op.execute(
        """
        UPDATE qc_app_visual_observation
        SET page_state = 'unknown',
            surface_facts = '{}'::jsonb
        WHERE page_state IS NULL OR surface_facts IS NULL
        """
    )
    op.execute(
        """
        ALTER TABLE qc_app_visual_observation
        ALTER COLUMN page_state SET DEFAULT 'unknown',
        ALTER COLUMN page_state SET NOT NULL,
        ALTER COLUMN surface_facts SET DEFAULT '{}'::jsonb,
        ALTER COLUMN surface_facts SET NOT NULL
        """
    )
    op.execute(
        f"""
        ALTER TABLE qc_app_visual_observation
        ADD CONSTRAINT ck_qc_app_visual_observation_page_state
            CHECK (page_state IN ({_PAGE_STATES})),
        ADD CONSTRAINT ck_qc_app_visual_observation_overlay_signature
            CHECK (
                overlay_signature IS NULL
                OR (
                    length(btrim(overlay_signature)) > 0
                    AND overlay_signature = btrim(overlay_signature)
                )
            ),
        ADD CONSTRAINT ck_qc_app_visual_observation_surface_facts
            CHECK (jsonb_typeof(surface_facts) = 'object'),
        ADD CONSTRAINT ck_qc_app_visual_observation_v1_phase_facts
            CHECK (
                contract_version <> 'app-visual-analysis.v1'
                OR (
                    page_state = 'unknown'
                    AND overlay_signature IS NULL
                    AND surface_facts = '{{}}'::jsonb
                )
            )
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_app_visual_observation
                WHERE page_state <> 'unknown'
                   OR overlay_signature IS NOT NULL
                   OR surface_facts <> '{}'::jsonb
            ) THEN
                RAISE EXCEPTION
                    'qc_app_visual_observation contains phase facts that downgrade would lose';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        ALTER TABLE qc_app_visual_observation
        DROP CONSTRAINT ck_qc_app_visual_observation_v1_phase_facts,
        DROP CONSTRAINT ck_qc_app_visual_observation_surface_facts,
        DROP CONSTRAINT ck_qc_app_visual_observation_overlay_signature,
        DROP CONSTRAINT ck_qc_app_visual_observation_page_state,
        DROP COLUMN surface_facts,
        DROP COLUMN overlay_signature,
        DROP COLUMN page_state
        """
    )
