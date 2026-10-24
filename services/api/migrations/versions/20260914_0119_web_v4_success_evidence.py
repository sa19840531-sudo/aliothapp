"""允许 Web v4 首页写入菜单成功证据账本。

Revision ID: 20260914_0119
Revises: 20260914_0118
"""

from __future__ import annotations

from alembic import op

revision = "20260914_0119"
down_revision = "20260914_0118"
branch_labels = None
depends_on = None


def _replace_success_guard(*, allow_v4: bool) -> None:
    formal_versions = (
        "'web-page-type-assessment.v3', 'web-page-type-assessment.v4'"
        if allow_v4
        else "'web-page-type-assessment.v3'"
    )
    op.execute(
        f"""
        ALTER TABLE qc_web_menu_ledger_entry
        DROP CONSTRAINT ck_qc_web_menu_success_evidence,
        ADD CONSTRAINT ck_qc_web_menu_success_evidence CHECK ((
            status NOT IN ('home_baseline','menu_page_captured','detail_captured')
            OR (
                status = 'home_baseline'
                AND decision = 'home_baseline'
                AND execution_slot IS NULL
                AND menu_action_count = 0
                AND menu_intent_id IS NULL
                AND menu_source_capture_id IS NULL
                AND menu_action_execution_id IS NULL
                AND menu_probe_capture_id IS NOT NULL
                AND menu_ready_capture_id IS NOT NULL
                AND (
                    (
                        normalization_version IN ({formal_versions})
                        AND menu_page_type = 'home'
                    )
                    OR (
                        normalization_version IN (
                            'web-page-type-normalization.v1',
                            'web-page-type-assessment.v1',
                            'web-page-type-assessment.v2'
                        )
                        AND menu_page_type IN (
                            'content_list','video_list','content_detail','video_detail'
                        )
                    )
                )
                AND detail_action_count = 0
                AND detail_intent_id IS NULL
                AND detail_source_capture_id IS NULL
                AND detail_candidate_id IS NULL
                AND detail_destination_sha256 IS NULL
                AND detail_action_execution_id IS NULL
                AND detail_probe_capture_id IS NULL
                AND detail_ready_capture_id IS NULL
                AND detail_page_type IS NULL
                AND detail_selection = '{{}}'::jsonb
            )
            OR (
                status = 'menu_page_captured'
                AND menu_action_count = 1
                AND detail_action_count = 0
                AND menu_action_execution_id IS NOT NULL
                AND menu_probe_capture_id IS NOT NULL
                AND menu_ready_capture_id IS NOT NULL
                AND menu_page_type IN (
                    'content_list','video_list','web_content_list','web_video_list'
                )
                AND diagnostic->>'type' IS DISTINCT FROM 'WEB_SUBMENU_EXPANDED'
                AND detail_action_execution_id IS NULL
                AND detail_probe_capture_id IS NULL
                AND detail_ready_capture_id IS NULL
                AND detail_page_type IS NULL
                AND detail_selection = '{{}}'::jsonb
            )
            OR (
                status = 'menu_page_captured'
                AND menu_action_count = 1
                AND detail_action_count = 0
                AND menu_action_execution_id IS NOT NULL
                AND menu_probe_capture_id IS NOT NULL
                AND menu_ready_capture_id IS NOT NULL
                AND menu_page_type IS NULL
                AND detail_intent_id IS NULL
                AND detail_source_capture_id IS NULL
                AND detail_candidate_id IS NULL
                AND detail_destination_sha256 IS NULL
                AND detail_action_execution_id IS NULL
                AND detail_probe_capture_id IS NULL
                AND detail_ready_capture_id IS NULL
                AND detail_page_type IS NULL
                AND detail_selection = '{{}}'::jsonb
                AND diagnostic->>'type' = 'WEB_SUBMENU_EXPANDED'
                AND jsonb_typeof(diagnostic->'menu_interaction_outcome') = 'object'
                AND diagnostic->'menu_interaction_outcome'->>'command_accepted' = 'true'
                AND diagnostic->'menu_interaction_outcome'->>'effect_observed' = 'true'
                AND diagnostic->'menu_interaction_outcome'->>'postcondition_satisfied' = 'true'
                AND diagnostic->'menu_interaction_outcome'->>'transition_arrived' = 'true'
                AND diagnostic->'menu_interaction_outcome'->>'after_capture_id'
                    = menu_ready_capture_id::text
                AND diagnostic->'menu_interaction_outcome'->>'evidence_mode' = 'natural'
                AND diagnostic->'menu_interaction_outcome'->'failure_reason' = 'null'::jsonb
            )
            OR (
                status = 'detail_captured'
                AND menu_action_count = 1
                AND menu_action_execution_id IS NOT NULL
                AND menu_probe_capture_id IS NOT NULL
                AND menu_ready_capture_id IS NOT NULL
                AND (
                    (
                        detail_action_count = 0
                        AND menu_page_type IN (
                            'content_detail','video_detail',
                            'web_content_detail','web_video_detail'
                        )
                        AND detail_action_execution_id IS NULL
                        AND detail_probe_capture_id IS NULL
                        AND detail_ready_capture_id IS NULL
                        AND detail_page_type IS NULL
                    )
                    OR (
                        detail_action_count = 1
                        AND menu_page_type IN (
                            'content_list','video_list',
                            'web_content_list','web_video_list'
                        )
                        AND detail_action_execution_id IS NOT NULL
                        AND detail_probe_capture_id IS NOT NULL
                        AND detail_ready_capture_id IS NOT NULL
                        AND detail_page_type IN (
                            'content_detail','video_detail',
                            'web_content_detail','web_video_detail'
                        )
                    )
                )
            )
        ) IS TRUE)
        """
    )


def upgrade() -> None:
    """新增 Web v4 正式首页成功证据形状。"""

    _replace_success_guard(allow_v4=True)


def downgrade() -> None:
    """存在 v4 首页成功证据时拒绝恢复 v3-only 约束。"""

    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM qc_web_menu_ledger_entry
            WHERE status='home_baseline'
              AND normalization_version='web-page-type-assessment.v4'
          ) THEN
            RAISE EXCEPTION 'Cannot downgrade immutable Web v4 success evidence';
          END IF;
        END $$
        """
    )
    _replace_success_guard(allow_v4=False)
