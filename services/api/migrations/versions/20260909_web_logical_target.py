"""Web Run 业务目标唯一占有与可信落地别名。"""

from alembic import op

revision = "20260909_0101"
down_revision = "20260909_0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    _menu_status_constraint(include_reuse=True)
    op.execute("""
        CREATE TABLE qc_web_logical_target (
            agent_run_id uuid NOT NULL,
            logical_page_id text NOT NULL CHECK (logical_page_id ~ '^web-logical:[0-9a-f]{64}$'),
            task_id uuid NOT NULL,
            session_id text NOT NULL,
            finalization_epoch integer NOT NULL CHECK (finalization_epoch >= 0),
            identity_policy_version text NOT NULL CHECK (identity_policy_version = 'web-url-identity.v2'),
            source_capture_id uuid NOT NULL,
            state text NOT NULL DEFAULT 'claimed' CHECK (state IN ('claimed', 'materialized')),
            assessment_capture_id uuid,
            assessment jsonb,
            primary_state text NOT NULL DEFAULT 'pending' CHECK (primary_state IN ('pending', 'claimed', 'complete')),
            primary_capture_id uuid,
            refinement_capture_id uuid,
            primary_result jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (agent_run_id, logical_page_id),
            FOREIGN KEY (task_id, agent_run_id) REFERENCES qc_agent_run(task_id,id) ON DELETE CASCADE,
            FOREIGN KEY (task_id, session_id) REFERENCES qc_surface_session(task_id,session_id),
            FOREIGN KEY (task_id, source_capture_id) REFERENCES qc_capture(task_id,id),
            FOREIGN KEY (task_id, assessment_capture_id) REFERENCES qc_capture(task_id,id),
            FOREIGN KEY (task_id, primary_capture_id) REFERENCES qc_capture(task_id,id),
            FOREIGN KEY (task_id, refinement_capture_id) REFERENCES qc_capture(task_id,id),
            CHECK ((state = 'materialized') = (assessment IS NOT NULL AND assessment_capture_id IS NOT NULL)),
            CHECK (assessment IS NULL OR jsonb_typeof(assessment) = 'object'),
            CHECK (primary_result IS NULL OR jsonb_typeof(primary_result) = 'object')
        )
    """)
    op.execute("""
        CREATE TABLE qc_web_logical_target_alias (
            agent_run_id uuid NOT NULL,
            alias_logical_page_id text NOT NULL CHECK (alias_logical_page_id ~ '^web-logical:[0-9a-f]{64}$'),
            logical_page_id text NOT NULL,
            task_id uuid NOT NULL,
            evidence_capture_id uuid NOT NULL,
            evidence_kind text NOT NULL CHECK (evidence_kind IN ('natural_redirect', 'frozen_declaration')),
            PRIMARY KEY (agent_run_id, alias_logical_page_id),
            FOREIGN KEY (agent_run_id, logical_page_id) REFERENCES qc_web_logical_target(agent_run_id,logical_page_id) ON DELETE CASCADE,
            FOREIGN KEY (task_id, agent_run_id) REFERENCES qc_agent_run(task_id,id) ON DELETE CASCADE,
            FOREIGN KEY (task_id, evidence_capture_id) REFERENCES qc_capture(task_id,id)
        )
    """)


def downgrade() -> None:
    _menu_status_constraint(include_reuse=False)
    op.execute("DROP TABLE qc_web_logical_target_alias")
    op.execute("DROP TABLE qc_web_logical_target")


def _menu_status_constraint(*, include_reuse: bool) -> None:
    statuses = (
        "pending",
        "home_pending",
        "menu_reserved",
        "detail_reserved",
        "home_baseline",
        "detail_captured",
        "menu_page_captured",
        "no_detail_candidate",
        "popup_blocked",
        "human_verification_blocked",
        "unsafe_navigation",
        "page_type_unclassified",
        "detail_selection_untrusted",
        "detail_page_type_mismatch",
        "navigation_failed",
        "cross_origin_blocked",
        "menu_limit_skipped",
        "allowlist_drifted",
        "runtime_discovered_unreviewed",
    ) + (("logical_target_reused",) if include_reuse else ())
    # 降级时若仍有新终态，数据库拒绝回滚，禁止静默改写历史账本。
    values = ", ".join(f"'{status}'" for status in statuses)
    op.execute("ALTER TABLE qc_web_menu_ledger_entry DROP CONSTRAINT ck_qc_web_menu_status")
    op.execute(
        f"ALTER TABLE qc_web_menu_ledger_entry ADD CONSTRAINT ck_qc_web_menu_status CHECK (status IN ({values}))"
    )
