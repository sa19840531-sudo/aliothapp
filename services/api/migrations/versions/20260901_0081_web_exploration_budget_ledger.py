"""增加 Web V2 菜单与单详情持久化硬预算。

Revision ID: 20260901_0081
Revises: 20260826_0080
Create Date: 2026-09-01
"""

from __future__ import annotations

from alembic import op

revision = "20260901_0081"
down_revision = "20260826_0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE qc_web_exploration_budget (
            agent_run_id uuid NOT NULL REFERENCES qc_agent_run(id) ON DELETE CASCADE,
            site_id varchar(64) NOT NULL,
            task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
            session_id varchar(128) NOT NULL,
            surface_kind varchar(16) NOT NULL DEFAULT 'web',
            session_owner varchar(255) NOT NULL DEFAULT 'web-v2-workflow',
            target_origin_sha256 varchar(64) NOT NULL,
            catalog_version varchar(128) NOT NULL,
            catalog_sha256 varchar(64) NOT NULL,
            menu_limit smallint NOT NULL DEFAULT 10,
            state varchar(32) NOT NULL DEFAULT 'open',
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            sealed_at timestamptz,
            PRIMARY KEY (agent_run_id, site_id),
            CONSTRAINT fk_qc_web_budget_session FOREIGN KEY (task_id, session_id)
                REFERENCES qc_surface_session (task_id, session_id) ON DELETE CASCADE,
            CONSTRAINT ck_qc_web_budget_surface CHECK (surface_kind = 'web'),
            CONSTRAINT ck_qc_web_budget_owner CHECK (
                session_owner = 'web-v2-workflow'
            ),
            CONSTRAINT ck_qc_web_budget_menu_limit CHECK (menu_limit = 10),
            CONSTRAINT ck_qc_web_budget_state CHECK (
                state IN ('open', 'needs_review', 'sealed')
            ),
            CONSTRAINT ck_qc_web_budget_hashes CHECK (
                target_origin_sha256 ~ '^[0-9a-f]{64}$'
                AND catalog_sha256 ~ '^[0-9a-f]{64}$'
            ),
            CONSTRAINT ck_qc_web_budget_version CHECK (row_version >= 0),
            CONSTRAINT ck_qc_web_budget_sealed CHECK (
                (state = 'sealed' AND sealed_at IS NOT NULL)
                OR (state <> 'sealed' AND sealed_at IS NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE qc_web_menu_ledger_entry (
            agent_run_id uuid NOT NULL,
            site_id varchar(64) NOT NULL,
            menu_entry_id varchar(160) NOT NULL,
            catalog_position integer NOT NULL,
            decision varchar(64) NOT NULL,
            menu_path jsonb NOT NULL,
            canonical_url_sha256 varchar(64) NOT NULL,
            execution_slot smallint,
            status varchar(64) NOT NULL,
            menu_action_count smallint NOT NULL DEFAULT 0,
            menu_intent_id varchar(128),
            menu_source_capture_id uuid REFERENCES qc_capture(id) ON DELETE RESTRICT,
            menu_action_execution_id varchar(96)
                REFERENCES qc_action_execution(action_execution_id) ON DELETE RESTRICT,
            menu_probe_capture_id uuid REFERENCES qc_capture(id) ON DELETE RESTRICT,
            menu_ready_capture_id uuid REFERENCES qc_capture(id) ON DELETE RESTRICT,
            menu_page_type varchar(64),
            detail_action_count smallint NOT NULL DEFAULT 0,
            detail_intent_id varchar(128),
            detail_source_capture_id uuid REFERENCES qc_capture(id) ON DELETE RESTRICT,
            detail_candidate_id varchar(255),
            detail_destination_sha256 varchar(64),
            detail_action_execution_id varchar(96)
                REFERENCES qc_action_execution(action_execution_id) ON DELETE RESTRICT,
            detail_probe_capture_id uuid REFERENCES qc_capture(id) ON DELETE RESTRICT,
            detail_ready_capture_id uuid REFERENCES qc_capture(id) ON DELETE RESTRICT,
            detail_page_type varchar(64),
            detail_selection jsonb NOT NULL DEFAULT '{}'::jsonb,
            diagnostic jsonb NOT NULL DEFAULT '{}'::jsonb,
            row_version bigint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (agent_run_id, site_id, menu_entry_id),
            CONSTRAINT fk_qc_web_menu_budget FOREIGN KEY (agent_run_id, site_id)
                REFERENCES qc_web_exploration_budget (agent_run_id, site_id)
                ON DELETE CASCADE,
            CONSTRAINT ck_qc_web_menu_position CHECK (catalog_position >= 0),
            CONSTRAINT ck_qc_web_menu_slot CHECK (
                execution_slot BETWEEN 1 AND 10 OR execution_slot IS NULL
            ),
            CONSTRAINT ck_qc_web_menu_action_count CHECK (
                menu_action_count IN (0, 1)
            ),
            CONSTRAINT ck_qc_web_detail_action_count CHECK (
                detail_action_count IN (0, 1)
            ),
            CONSTRAINT ck_qc_web_menu_reservation CHECK (
                (
                    menu_action_count = 0
                    AND menu_intent_id IS NULL
                    AND menu_source_capture_id IS NULL
                    AND menu_action_execution_id IS NULL
                )
                OR (
                    menu_action_count = 1
                    AND execution_slot IS NOT NULL
                    AND menu_intent_id IS NOT NULL
                    AND menu_source_capture_id IS NOT NULL
                )
            ),
            CONSTRAINT ck_qc_web_detail_reservation CHECK (
                (
                    detail_action_count = 0
                    AND detail_intent_id IS NULL
                    AND detail_source_capture_id IS NULL
                    AND detail_candidate_id IS NULL
                    AND detail_destination_sha256 IS NULL
                    AND detail_action_execution_id IS NULL
                )
                OR (
                    detail_action_count = 1
                    AND menu_action_count = 1
                    AND detail_intent_id IS NOT NULL
                    AND detail_source_capture_id IS NOT NULL
                    AND detail_candidate_id IS NOT NULL
                    AND detail_destination_sha256 IS NOT NULL
                )
            ),
            CONSTRAINT ck_qc_web_menu_action_link CHECK (
                menu_action_execution_id IS NULL OR menu_action_count = 1
            ),
            CONSTRAINT ck_qc_web_detail_action_link CHECK (
                detail_action_execution_id IS NULL OR detail_action_count = 1
            ),
            CONSTRAINT ck_qc_web_menu_status CHECK (
                status IN (
                    'pending',
                    'home_pending',
                    'menu_reserved',
                    'detail_reserved',
                    'home_baseline',
                    'detail_captured',
                    'menu_page_captured',
                    'no_detail_candidate',
                    'popup_blocked',
                    'human_verification_blocked',
                    'unsafe_navigation',
                    'page_type_unclassified',
                    'detail_selection_untrusted',
                    'detail_page_type_mismatch',
                    'navigation_failed',
                    'cross_origin_blocked',
                    'menu_limit_skipped',
                    'allowlist_drifted',
                    'runtime_discovered_unreviewed'
                )
            ),
            CONSTRAINT ck_qc_web_menu_success_evidence CHECK (
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
                    AND menu_page_type = 'home'
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
                    AND menu_page_type IN ('web_content_list', 'web_video_list')
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
                                'web_content_list',
                                'web_video_list'
                            )
                            AND detail_action_execution_id IS NOT NULL
                            AND detail_probe_capture_id IS NOT NULL
                            AND detail_ready_capture_id IS NOT NULL
                            AND detail_page_type IN (
                                'web_content_detail',
                                'web_video_detail'
                            )
                        )
                    )
                )
            ),
            CONSTRAINT ck_qc_web_menu_payloads CHECK (
                jsonb_typeof(menu_path) = 'array'
                AND jsonb_typeof(detail_selection) = 'object'
                AND jsonb_typeof(diagnostic) = 'object'
            ),
            CONSTRAINT ck_qc_web_menu_hashes CHECK (
                canonical_url_sha256 ~ '^[0-9a-f]{64}$'
                AND (
                    detail_destination_sha256 IS NULL
                    OR detail_destination_sha256 ~ '^[0-9a-f]{64}$'
                )
            ),
            CONSTRAINT ck_qc_web_menu_version CHECK (row_version >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_web_menu_execution_slot
        ON qc_web_menu_ledger_entry (agent_run_id, site_id, execution_slot)
        WHERE execution_slot IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_web_menu_action_execution
        ON qc_web_menu_ledger_entry (menu_action_execution_id)
        WHERE menu_action_execution_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_qc_web_detail_action_execution
        ON qc_web_menu_ledger_entry (detail_action_execution_id)
        WHERE detail_action_execution_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS qc_web_menu_ledger_entry")
    op.execute("DROP TABLE IF EXISTS qc_web_exploration_budget")
