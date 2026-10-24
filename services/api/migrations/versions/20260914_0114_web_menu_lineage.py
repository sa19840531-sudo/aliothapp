"""持久化 Web 运行时菜单谱系并显式约束子菜单展开证据。

Revision ID: 20260914_0114
Revises: 20260914_0113
"""

from alembic import op

revision = "20260914_0114"
down_revision = "20260914_0113"
branch_labels = None
depends_on = None


def _replace_success_constraint(*, allow_submenu: bool) -> None:
    ordinary_submenu_exclusion = (
        "AND diagnostic->>'type' IS DISTINCT FROM 'WEB_SUBMENU_EXPANDED'"
        if allow_submenu
        else ""
    )
    ordinary_detail_selection = (
        "AND detail_selection = '{}'::jsonb" if allow_submenu else ""
    )
    home_page_type = (
        """
                AND (
                    (
                        normalization_version = 'web-page-type-assessment.v3'
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
        """
        if allow_submenu
        else """
                AND menu_page_type IN (
                    'home','content_list','video_list','content_detail','video_detail'
                )
        """
    )
    submenu = """
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
                    AND detail_selection = '{}'::jsonb
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
    """ if allow_submenu else ""
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
                {home_page_type}
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
                {ordinary_submenu_exclusion}
                AND detail_action_execution_id IS NULL
                AND detail_probe_capture_id IS NULL
                AND detail_ready_capture_id IS NULL
                AND detail_page_type IS NULL
                {ordinary_detail_selection}
            )
            {submenu}
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
                            'content_list','video_list','web_content_list','web_video_list'
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
    op.execute(
        """
        ALTER TABLE qc_web_menu_ledger_entry
          ADD COLUMN parent_menu_entry_id varchar(128),
          ADD COLUMN menu_depth integer,
          ADD COLUMN exploration_path jsonb,
          ADD COLUMN logical_target_identity varchar(128),
          ADD COLUMN normalization_version varchar(64)
        """
    )
    op.execute(
        """
        UPDATE qc_web_menu_ledger_entry AS entry
        SET normalization_version=COALESCE(
          (
            SELECT coverage.normalization_version
            FROM qc_web_exploration_budget AS budget
            JOIN qc_web_coverage_snapshot AS coverage
              ON coverage.task_id=budget.task_id
             AND coverage.agent_run_id=budget.agent_run_id
            WHERE budget.agent_run_id=entry.agent_run_id
              AND budget.site_id=entry.site_id
            ORDER BY coverage.finalization_epoch DESC
            LIMIT 1
          ),
          (
            SELECT assessment.schema_version
            FROM qc_web_exploration_budget AS budget
            JOIN qc_h5_page_type_assessment AS assessment
              ON assessment.task_id=budget.task_id
             AND assessment.agent_run_id=budget.agent_run_id
             AND assessment.session_id=budget.session_id
             AND assessment.page_capture_id=entry.menu_ready_capture_id
             AND assessment.page_type=entry.menu_page_type
            WHERE budget.agent_run_id=entry.agent_run_id
              AND budget.site_id=entry.site_id
            ORDER BY assessment.created_at DESC
            LIMIT 1
          )
        )
        WHERE entry.status='home_baseline'
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM qc_web_menu_ledger_entry
            WHERE status='home_baseline'
              AND normalization_version IS NULL
          ) THEN
            RAISE EXCEPTION 'WEB_HOME_NORMALIZATION_VERSION_UNAVAILABLE';
          END IF;
        END $$
        """
    )
    op.execute(
        """
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
                     NEW.normalization_version='web-page-type-assessment.v3'
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
    op.execute(
        """
        UPDATE qc_web_menu_ledger_entry
        SET
          parent_menu_entry_id=NULLIF(
            discovery_manifest->'candidate'->>'parent_menu_entry_id', ''
          ),
          menu_depth=CASE
            WHEN jsonb_typeof(discovery_manifest->'candidate'->'menu_depth')='number'
              AND discovery_manifest->'candidate'->>'menu_depth' ~ '^[1-9][0-9]*$'
            THEN (discovery_manifest->'candidate'->>'menu_depth')::integer
            ELSE 1
          END,
          exploration_path=CASE
            WHEN jsonb_typeof(discovery_manifest->'candidate'->'exploration_path')='array'
              AND jsonb_array_length(
                discovery_manifest->'candidate'->'exploration_path'
              ) > 0
            THEN discovery_manifest->'candidate'->'exploration_path'
            WHEN jsonb_typeof(menu_path)='array' AND jsonb_array_length(menu_path) > 0
            THEN menu_path
            ELSE jsonb_build_array(discovery_manifest->'candidate'->>'label')
          END,
          logical_target_identity=COALESCE(
            NULLIF(discovery_manifest->'candidate'->>'logical_target_identity', ''),
            NULLIF(regexp_replace(menu_entry_id, '^discovery:', ''), '')
          )
        WHERE source_kind='runtime_discovered'
        """
    )
    op.execute(
        """
        UPDATE qc_web_menu_ledger_entry AS child
        SET parent_menu_entry_id=NULL,
            diagnostic=jsonb_set(
              child.diagnostic,
              '{error_code}',
              '"WEB_MENU_LINEAGE_UNAVAILABLE"'::jsonb,
              true
            )
        WHERE child.source_kind='runtime_discovered'
          AND (
            (
              child.parent_menu_entry_id IS NULL
              AND (
                child.menu_depth > 1
                OR child.discovery_manifest->'candidate'
                     ->>'parent_logical_target_identity' IS NOT NULL
              )
            )
            OR child.parent_menu_entry_id=child.menu_entry_id
            OR (
              child.parent_menu_entry_id IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM qc_web_menu_ledger_entry AS parent
                WHERE parent.agent_run_id=child.agent_run_id
                  AND parent.site_id=child.site_id
                  AND parent.menu_entry_id=child.parent_menu_entry_id
                  AND parent.source_kind='runtime_discovered'
                  AND child.menu_depth=parent.menu_depth + 1
                  AND jsonb_array_length(child.exploration_path)
                      > jsonb_array_length(parent.exploration_path)
                  AND NOT EXISTS (
                    SELECT 1
                    FROM generate_series(
                      0,
                      jsonb_array_length(parent.exploration_path) - 1
                    ) AS path_index
                    WHERE child.exploration_path->>path_index
                        IS DISTINCT FROM parent.exploration_path->>path_index
                  )
                  AND child.discovery_manifest->'candidate'
                        ->>'parent_logical_target_identity'
                      = parent.logical_target_identity
              )
            )
          )
        """
    )
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1
            FROM qc_web_menu_ledger_entry AS child
            WHERE child.source_kind='runtime_discovered'
              AND (
                (
                  child.parent_menu_entry_id IS NULL
                  AND (
                    (child.menu_depth > 1 AND child.diagnostic->>'error_code'
                      IS DISTINCT FROM 'WEB_MENU_LINEAGE_UNAVAILABLE')
                    OR (child.menu_depth=1 AND child.discovery_manifest->'candidate'
                      ->>'parent_logical_target_identity' IS NOT NULL
                      AND child.diagnostic->>'error_code'
                        IS DISTINCT FROM 'WEB_MENU_LINEAGE_UNAVAILABLE')
                  )
                )
                OR child.parent_menu_entry_id=child.menu_entry_id
                OR (
                  child.parent_menu_entry_id IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM qc_web_menu_ledger_entry AS parent
                    WHERE parent.agent_run_id=child.agent_run_id
                      AND parent.site_id=child.site_id
                      AND parent.menu_entry_id=child.parent_menu_entry_id
                      AND parent.source_kind='runtime_discovered'
                      AND child.menu_depth=parent.menu_depth + 1
                      AND jsonb_array_length(child.exploration_path)
                          > jsonb_array_length(parent.exploration_path)
                      AND NOT EXISTS (
                        SELECT 1 FROM generate_series(
                          0,
                          jsonb_array_length(parent.exploration_path) - 1
                        ) AS path_index
                        WHERE child.exploration_path->>path_index
                          IS DISTINCT FROM parent.exploration_path->>path_index
                      )
                      AND child.discovery_manifest->'candidate'
                        ->>'parent_logical_target_identity'
                          = parent.logical_target_identity
                  )
                )
              )
          ) THEN
            RAISE EXCEPTION 'WEB_MENU_LINEAGE_BACKFILL_INVALID';
          END IF;
        END $$
        """
    )
    op.execute(
        """
        ALTER TABLE qc_web_menu_ledger_entry
          ADD CONSTRAINT ck_qc_web_menu_lineage_shape CHECK ((
            (
              source_kind='catalog'
              AND parent_menu_entry_id IS NULL
              AND menu_depth IS NULL
              AND exploration_path IS NULL
              AND logical_target_identity IS NULL
            )
            OR (
              source_kind='runtime_discovered'
              AND menu_depth >= 1
              AND jsonb_typeof(exploration_path)='array'
              AND jsonb_array_length(exploration_path) > 0
              AND logical_target_identity ~ '^[0-9a-f]{64}$'
              AND menu_entry_id='discovery:' || logical_target_identity
              AND exploration_path->>-1
                  = discovery_manifest->'candidate'->>'label'
            )
          ) IS TRUE)
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_validate_web_menu_lineage() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          parent_depth integer;
          parent_path jsonb;
          parent_logical_identity varchar(128);
        BEGIN
          IF TG_OP='UPDATE' AND (
            NEW.parent_menu_entry_id IS DISTINCT FROM OLD.parent_menu_entry_id OR
            NEW.menu_depth IS DISTINCT FROM OLD.menu_depth OR
            NEW.exploration_path IS DISTINCT FROM OLD.exploration_path OR
            NEW.logical_target_identity IS DISTINCT FROM OLD.logical_target_identity
          ) THEN
            RAISE EXCEPTION 'WEB_MENU_LINEAGE_IMMUTABLE' USING ERRCODE='23514';
          END IF;
          IF NEW.source_kind <> 'runtime_discovered' THEN
            RETURN NEW;
          END IF;
          IF NEW.parent_menu_entry_id = NEW.menu_entry_id THEN
            RAISE EXCEPTION 'WEB_MENU_LINEAGE_SELF_PARENT' USING ERRCODE='23514';
          END IF;
          IF NEW.parent_menu_entry_id IS NULL THEN
            IF NEW.menu_depth > 1 AND NOT (
              NEW.diagnostic->>'error_code'='WEB_MENU_LINEAGE_UNAVAILABLE'
            ) THEN
              RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_MISSING' USING ERRCODE='23514';
            END IF;
            IF NEW.menu_depth = 1 AND
               NEW.discovery_manifest->'candidate'->>'parent_logical_target_identity'
                   IS NOT NULL THEN
              RAISE EXCEPTION 'WEB_MENU_LINEAGE_ROOT_PARENT_INVALID' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
          END IF;
          SELECT parent.menu_depth, parent.exploration_path,
                 parent.logical_target_identity
          INTO parent_depth, parent_path, parent_logical_identity
          FROM qc_web_menu_ledger_entry AS parent
          WHERE parent.agent_run_id=NEW.agent_run_id
            AND parent.site_id=NEW.site_id
            AND parent.menu_entry_id=NEW.parent_menu_entry_id
            AND parent.source_kind='runtime_discovered';
          IF NOT FOUND THEN
            RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_SCOPE_INVALID' USING ERRCODE='23514';
          END IF;
          IF NEW.menu_depth <> parent_depth + 1
             OR jsonb_array_length(NEW.exploration_path)
                <= jsonb_array_length(parent_path)
             OR EXISTS (
               SELECT 1
               FROM generate_series(0, jsonb_array_length(parent_path) - 1)
                    AS path_index
               WHERE NEW.exploration_path->>path_index
                   IS DISTINCT FROM parent_path->>path_index
             )
             OR NEW.discovery_manifest->'candidate'->>'parent_logical_target_identity'
                IS DISTINCT FROM parent_logical_identity THEN
            RAISE EXCEPTION 'WEB_MENU_LINEAGE_PARENT_MISMATCH' USING ERRCODE='23514';
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_web_menu_lineage
        BEFORE INSERT OR UPDATE ON qc_web_menu_ledger_entry
        FOR EACH ROW EXECUTE FUNCTION qc_validate_web_menu_lineage()
        """
    )
    op.execute(
        """
        CREATE FUNCTION qc_validate_web_submenu_evidence() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          IF NEW.status='menu_page_captured'
             AND NEW.diagnostic->>'type'='WEB_SUBMENU_EXPANDED' THEN
            IF NEW.menu_page_type IS NOT NULL
               OR NEW.menu_probe_capture_id IS DISTINCT FROM NEW.menu_ready_capture_id
               OR NOT EXISTS (
                 SELECT 1
                 FROM qc_web_exploration_budget AS budget
                 JOIN qc_surface_session AS session
                   ON session.task_id=budget.task_id
                  AND session.session_id=budget.session_id
                 JOIN qc_action_execution AS action
                   ON action.action_execution_id=NEW.menu_action_execution_id
                  AND action.task_id=budget.task_id
                  AND action.session_id=budget.session_id
                 JOIN qc_capture AS before_capture
                   ON before_capture.id=action.source_capture_id
                 JOIN qc_capture AS after_capture
                   ON after_capture.id=action.result_capture_id
                 WHERE budget.agent_run_id=NEW.agent_run_id
                   AND budget.site_id=NEW.site_id
                   AND action.intent_id=NEW.menu_intent_id
                   AND action.status='completed'
                   AND action.finalization_epoch=session.finalization_epoch
                   AND action.requested_source_capture_id=NEW.menu_source_capture_id
                   AND action.result_capture_id=NEW.menu_ready_capture_id
                   AND before_capture.task_id=action.task_id
                   AND before_capture.agent_run_id=NEW.agent_run_id
                   AND before_capture.surface_session_id=action.session_id
                   AND before_capture.surface_kind='web'
                   AND before_capture.capture_status='captured'
                   AND before_capture.capture_role='before_action'
                   AND before_capture.action_execution_id=action.action_execution_id
                   AND before_capture.parent_capture_id=action.requested_source_capture_id
                   AND before_capture.session_command_sequence=action.command_sequence
                   AND after_capture.task_id=action.task_id
                   AND after_capture.agent_run_id=NEW.agent_run_id
                   AND after_capture.surface_session_id=action.session_id
                   AND after_capture.surface_kind='web'
                   AND after_capture.capture_status='captured'
                   AND after_capture.capture_role='after_action'
                   AND after_capture.parent_capture_id=before_capture.id
                   AND after_capture.action_execution_id=action.action_execution_id
                   AND after_capture.session_command_sequence=action.command_sequence
               ) THEN
              RAISE EXCEPTION 'WEB_SUBMENU_EVIDENCE_INVALID' USING ERRCODE='23514';
            END IF;
          END IF;
          RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_qc_web_submenu_evidence
        BEFORE INSERT OR UPDATE ON qc_web_menu_ledger_entry
        FOR EACH ROW EXECUTE FUNCTION qc_validate_web_submenu_evidence()
        """
    )
    _replace_success_constraint(allow_submenu=True)


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM qc_web_menu_ledger_entry
            WHERE source_kind='runtime_discovered'
          ) THEN
            RAISE EXCEPTION 'WEB_MENU_LINEAGE_DOWNGRADE_REQUIRES_NO_RUNTIME_RECORDS';
          END IF;
        END $$
        """
    )
    _replace_success_constraint(allow_submenu=False)
    op.execute(
        """
        CREATE OR REPLACE FUNCTION qc_web_require_formal_home_type() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND OLD.status = 'home_baseline'
               AND NEW.status = OLD.status
               AND NEW.menu_page_type IS NOT DISTINCT FROM OLD.menu_page_type THEN
                RETURN NEW;
            END IF;
            IF NEW.status = 'home_baseline'
               AND NEW.menu_page_type IS DISTINCT FROM 'home'
               AND NOT EXISTS (
                   SELECT 1
                   FROM qc_web_exploration_budget AS budget
                   JOIN qc_web_run_policy_snapshot AS policy
                     ON policy.agent_run_id=budget.agent_run_id
                    AND policy.task_id=budget.task_id
                   WHERE budget.agent_run_id=NEW.agent_run_id
                     AND budget.site_id=NEW.site_id
                     AND NOT (policy.payload->'page_catalog'->'page_types'
                         @> '[{"code":"home"}]'::jsonb)
               ) THEN
                RAISE EXCEPTION 'Web home_baseline requires formal home page_type';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_qc_web_submenu_evidence "
        "ON qc_web_menu_ledger_entry"
    )
    op.execute("DROP FUNCTION IF EXISTS qc_validate_web_submenu_evidence()")
    op.execute("DROP TRIGGER IF EXISTS trg_qc_web_menu_lineage ON qc_web_menu_ledger_entry")
    op.execute("DROP FUNCTION IF EXISTS qc_validate_web_menu_lineage()")
    op.execute(
        """
        ALTER TABLE qc_web_menu_ledger_entry
          DROP CONSTRAINT ck_qc_web_menu_lineage_shape,
          DROP COLUMN logical_target_identity,
          DROP COLUMN exploration_path,
          DROP COLUMN menu_depth,
          DROP COLUMN parent_menu_entry_id,
          DROP COLUMN normalization_version
        """
    )
