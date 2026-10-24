"""允许 Web 页面类型评估 v2 与规范四类直接落库。

Revision ID: 20260904_0089
Revises: 20260903_0088
Create Date: 2026-09-04
"""

from __future__ import annotations

from alembic import op

revision = "20260904_0089"
down_revision = "20260903_0088"
branch_labels = None
depends_on = None


def _replace_coverage_normalization_constraint() -> None:
    """保留历史 v1 快照，同时允许新评估协议写入。"""

    op.execute(
        """
        ALTER TABLE qc_web_coverage_snapshot
            DROP CONSTRAINT ck_qc_web_coverage_snapshot_normalization,
            ADD CONSTRAINT ck_qc_web_coverage_snapshot_normalization CHECK (
                normalization_version IN (
                    'web-page-type-normalization.v1',
                    'web-page-type-assessment.v2'
                )
            )
        """
    )


def _replace_menu_success_evidence_constraint() -> None:
    """兼容历史 web_* 记录，并允许规范四类成为新写入口径。"""

    op.execute(
        """
        ALTER TABLE qc_web_menu_ledger_entry
            DROP CONSTRAINT ck_qc_web_menu_success_evidence,
            ADD CONSTRAINT ck_qc_web_menu_success_evidence CHECK (
                status NOT IN (
                    'home_baseline',
                    'menu_page_captured',
                    'detail_captured'
                )
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
                    AND menu_page_type IN (
                        'home',
                        'content_list',
                        'video_list',
                        'content_detail',
                        'video_detail'
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
                    AND detail_selection = '{}'::jsonb
                )
                OR (
                    status = 'menu_page_captured'
                    AND menu_action_count = 1
                    AND detail_action_count = 0
                    AND menu_action_execution_id IS NOT NULL
                    AND menu_probe_capture_id IS NOT NULL
                    AND menu_ready_capture_id IS NOT NULL
                    AND menu_page_type IN (
                        'content_list',
                        'video_list',
                        'web_content_list',
                        'web_video_list'
                    )
                    AND detail_action_execution_id IS NULL
                    AND detail_probe_capture_id IS NULL
                    AND detail_ready_capture_id IS NULL
                    AND detail_page_type IS NULL
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
                                'content_detail',
                                'video_detail',
                                'web_content_detail',
                                'web_video_detail'
                            )
                            AND detail_action_execution_id IS NULL
                            AND detail_probe_capture_id IS NULL
                            AND detail_ready_capture_id IS NULL
                            AND detail_page_type IS NULL
                        )
                        OR (
                            detail_action_count = 1
                            AND menu_page_type IN (
                                'content_list',
                                'video_list',
                                'web_content_list',
                                'web_video_list'
                            )
                            AND detail_action_execution_id IS NOT NULL
                            AND detail_probe_capture_id IS NOT NULL
                            AND detail_ready_capture_id IS NOT NULL
                            AND detail_page_type IN (
                                'content_detail',
                                'video_detail',
                                'web_content_detail',
                                'web_video_detail'
                            )
                        )
                    )
                )
            )
        """
    )


def upgrade() -> None:
    """升级为 v1/v2 与历史/规范页型并存的兼容约束。"""

    _replace_coverage_normalization_constraint()
    _replace_menu_success_evidence_constraint()


def downgrade() -> None:
    """不可破坏不可变 v2 快照；回退时继续保留双协议兼容约束。"""

    _replace_coverage_normalization_constraint()
    _replace_menu_success_evidence_constraint()
