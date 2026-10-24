"""Web 失败序列与逐规则封存引用，保留旧发布及聚合合同。"""

from alembic import op

revision = "20260910_0103"
down_revision = "20260910_0102"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    DO $migration$ BEGIN
    CREATE TABLE qc_web_evidence_sequence (
        sequence_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        source_agent_run_id uuid NOT NULL,
        session_id varchar(255) NOT NULL,
        finalization_epoch integer NOT NULL CHECK (finalization_epoch >= 0),
        anchor_capture_id uuid NOT NULL,
        target_id varchar(64) NOT NULL CHECK (target_id ~ '^[0-9a-f]{64}$'),
        occurrence_id varchar(64) NOT NULL CHECK (occurrence_id ~ '^[0-9a-f]{64}$'),
        purpose varchar(32) NOT NULL CHECK (purpose IN ('failure_sequence','interaction_sequence')),
        readiness_status varchar(64) NOT NULL,
        sequence_hash varchar(64) NOT NULL CHECK (sequence_hash ~ '^[0-9a-f]{64}$'),
        sequence_payload jsonb NOT NULL CHECK (jsonb_typeof(sequence_payload)='object'),
        created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (task_id,source_agent_run_id) REFERENCES qc_agent_run(task_id,id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (task_id,anchor_capture_id) REFERENCES qc_capture(task_id,id)
            DEFERRABLE INITIALLY DEFERRED,
        UNIQUE (task_id,source_agent_run_id,anchor_capture_id,finalization_epoch,sequence_id),
        UNIQUE (task_id,source_agent_run_id,finalization_epoch,sequence_id),
        UNIQUE (task_id,source_agent_run_id,finalization_epoch,anchor_capture_id,purpose)
    );
    CREATE TABLE qc_web_evidence_sequence_member (
        sequence_id uuid NOT NULL REFERENCES qc_web_evidence_sequence(sequence_id) ON DELETE CASCADE,
        ordinal integer NOT NULL CHECK (ordinal > 0),
        task_id uuid NOT NULL,
        capture_id uuid NOT NULL,
        captured_at timestamptz NOT NULL,
        frame_payload jsonb NOT NULL CHECK (jsonb_typeof(frame_payload)='object'),
        PRIMARY KEY (sequence_id,ordinal),
        UNIQUE (sequence_id,capture_id),
        FOREIGN KEY (task_id,capture_id) REFERENCES qc_capture(task_id,id)
            DEFERRABLE INITIALLY DEFERRED
    );
    CREATE FUNCTION qc_guard_web_evidence_sequence() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='UPDATE' THEN
            RAISE EXCEPTION 'WEB_SEQUENCE_IMMUTABLE' USING ERRCODE='23514';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM qc_task task
            JOIN qc_agent_run run ON run.task_id=task.id AND run.id=NEW.source_agent_run_id
            JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id=NEW.session_id
            JOIN qc_capture capture ON capture.task_id=task.id AND capture.id=NEW.anchor_capture_id
            JOIN qc_web_run_policy_snapshot policy ON policy.task_id=task.id AND policy.agent_run_id=run.id
            WHERE task.id=NEW.task_id AND task.target_platform='web'
              AND task.task_config->>'workflow_version'='v2'
              AND task.task_config->>'execution_mode'='full_inspection'
              AND task.task_config->>'web_inspection_notification_mode'='enforced'
              AND task.status='running' AND task.finalization_state IN ('open','exploration_draining','session_draining')
              AND task.finalization_epoch=NEW.finalization_epoch
              AND run.agent_type='main' AND run.status IN ('running','done','failed','aborted')
              AND COALESCE(run.output_context->>'cancel_requested','false')='false'
              AND NOT EXISTS (SELECT 1 FROM qc_agent_run newer WHERE newer.task_id=task.id
                  AND newer.agent_type='main' AND (newer.created_at,newer.id)>(run.created_at,run.id))
              AND session.session_id='web:'||run.id::text
              AND session.session_owner='web-v2-workflow' AND session.surface_kind='web'
              AND session.finalization_epoch=NEW.finalization_epoch AND session.status IN ('active','draining')
              AND capture.agent_run_id=run.id AND capture.surface_session_id=session.session_id
              AND capture.capture_status='captured' AND capture.capture_role='after_action'
              AND capture.metadata->>'finalization_epoch'=NEW.finalization_epoch::text
        ) THEN RAISE EXCEPTION 'WEB_SEQUENCE_SCOPE' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER tr_web_evidence_sequence BEFORE INSERT OR UPDATE ON qc_web_evidence_sequence
        FOR EACH ROW EXECUTE FUNCTION qc_guard_web_evidence_sequence();
    CREATE FUNCTION qc_guard_web_sequence_member() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='UPDATE' THEN
            RAISE EXCEPTION 'WEB_SEQUENCE_IMMUTABLE' USING ERRCODE='23514';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM qc_web_evidence_sequence sequence
            JOIN qc_capture capture ON capture.task_id=sequence.task_id AND capture.id=NEW.capture_id
            WHERE sequence.sequence_id=NEW.sequence_id AND sequence.task_id=NEW.task_id
              AND (sequence.sequence_payload->'frames'->(NEW.ordinal-1)=NEW.frame_payload) IS TRUE
              AND capture.agent_run_id=sequence.source_agent_run_id
              AND capture.surface_session_id=sequence.session_id
              AND capture.capture_status='captured' AND capture.captured_at=NEW.captured_at
              AND capture.metadata->>'finalization_epoch'=sequence.finalization_epoch::text
              AND capture.capture_role=NEW.frame_payload->>'capture_role'
              AND capture.id::text=NEW.frame_payload->>'capture_id'
        ) THEN RAISE EXCEPTION 'WEB_SEQUENCE_MEMBER_SCOPE' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER tr_web_sequence_member BEFORE INSERT OR UPDATE ON qc_web_evidence_sequence_member
        FOR EACH ROW EXECUTE FUNCTION qc_guard_web_sequence_member();
    ALTER TABLE qc_h5_inspection_publication
        ALTER COLUMN page_type_assessment_id DROP NOT NULL,
        ALTER COLUMN assessment_hash DROP NOT NULL,
        ADD COLUMN web_evidence_sequence_id uuid,
        ADD CONSTRAINT ck_publication_web_sequence CHECK ((
            (web_evidence_sequence_id IS NULL AND page_type_assessment_id IS NOT NULL AND assessment_hash IS NOT NULL)
            OR (web_evidence_sequence_id IS NOT NULL AND page_type_assessment_id IS NULL AND assessment_hash IS NULL)
        ) IS TRUE),
        ADD CONSTRAINT fk_publication_web_sequence FOREIGN KEY
            (task_id,source_agent_run_id,page_capture_id,finalization_epoch,web_evidence_sequence_id)
            REFERENCES qc_web_evidence_sequence
            (task_id,source_agent_run_id,anchor_capture_id,finalization_epoch,sequence_id);
    ALTER TABLE qc_h5_inspection_publication DROP CONSTRAINT uq_qc_h5_publication_capture_epoch;
    ALTER TABLE qc_h5_inspection_publication ADD CONSTRAINT uq_qc_h5_publication_capture_epoch
        UNIQUE NULLS NOT DISTINCT(task_id,page_capture_id,finalization_epoch,web_evidence_sequence_id);
    CREATE FUNCTION qc_guard_web_sequence_publication() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='UPDATE' AND OLD.web_evidence_sequence_id IS DISTINCT FROM NEW.web_evidence_sequence_id THEN
            RAISE EXCEPTION 'WEB_SEQUENCE_PUBLICATION_IMMUTABLE' USING ERRCODE='23514';
        END IF;
        IF NEW.web_evidence_sequence_id IS NULL THEN RETURN NEW; END IF;
        IF NOT EXISTS (
            SELECT 1 FROM qc_web_evidence_sequence sequence
            JOIN qc_task task ON task.id=sequence.task_id AND task.target_platform='web'
            JOIN qc_agent_run run ON run.id=sequence.source_agent_run_id AND run.task_id=task.id
            JOIN qc_surface_session session ON session.session_id=sequence.session_id AND session.task_id=task.id
            WHERE sequence.sequence_id=NEW.web_evidence_sequence_id
              AND task.task_config->>'workflow_version'='v2'
              AND task.finalization_epoch=sequence.finalization_epoch
              AND run.agent_type='main' AND session.session_owner='web-v2-workflow'
              AND session.session_id='web:'||run.id::text
              AND session.finalization_epoch=sequence.finalization_epoch
              AND (SELECT count(*) FROM qc_web_evidence_sequence_member member
                   WHERE member.sequence_id=sequence.sequence_id)
                  =jsonb_array_length(sequence.sequence_payload->'frames')
              AND (SELECT count(*) FROM qc_web_evidence_sequence_member member
                   WHERE member.sequence_id=sequence.sequence_id
                     AND member.frame_payload->>'capture_role'='after_action'
                     AND member.frame_payload->>'target_id'=sequence.target_id
                     AND member.frame_payload->>'occurrence_id'=sequence.occurrence_id) >= 2
              AND NOT EXISTS (
                  SELECT 1 FROM qc_web_evidence_sequence_member member
                  JOIN qc_capture capture ON capture.id=member.capture_id
                  WHERE member.sequence_id=sequence.sequence_id AND (
                      member.frame_payload->>'evidence_mode' IS DISTINCT FROM 'natural'
                      OR NOT EXISTS(SELECT 1 FROM qc_evidence_manifest manifest
                          WHERE manifest.task_id=task.id AND manifest.capture_id=capture.id
                            AND manifest.quality_status='pass')
                      OR (capture.capture_role='after_action' AND NOT EXISTS (
                          SELECT 1 FROM qc_action_execution action
                          JOIN qc_capture before_capture ON before_capture.id=action.source_capture_id
                          JOIN qc_web_evidence_sequence_member before_member ON before_member.capture_id=before_capture.id
                              AND before_member.sequence_id=sequence.sequence_id
                          WHERE action.action_execution_id=capture.action_execution_id
                              AND action.task_id=task.id AND action.session_id=session.session_id
                              AND action.finalization_epoch=sequence.finalization_epoch
                              AND action.result_capture_id=capture.id
                              AND before_capture.capture_role='before_action'
                              AND before_capture.id=capture.parent_capture_id
                              AND before_capture.captured_at<capture.captured_at
                              AND before_capture.action_execution_id=action.action_execution_id
                      ))
                  )
              )
        ) THEN RAISE EXCEPTION 'WEB_SEQUENCE_PUBLICATION_UNPROVEN' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER tr_web_sequence_publication BEFORE INSERT OR UPDATE ON qc_h5_inspection_publication
        FOR EACH ROW EXECUTE FUNCTION qc_guard_web_sequence_publication();
    ALTER TABLE qc_evidence_input_set DROP CONSTRAINT ck_qc_evidence_input_set_scope;
    ALTER TABLE qc_evidence_input_set
        ADD COLUMN web_evidence_sequence_id uuid,
        ADD CONSTRAINT ck_qc_evidence_input_set_scope CHECK
            (scope IN ('page','journey','task','environment_matrix','web_sequence')),
        ADD CONSTRAINT ck_input_set_web_sequence CHECK
            (((scope='web_sequence')=(web_evidence_sequence_id IS NOT NULL)) IS TRUE),
        ADD CONSTRAINT fk_input_set_web_sequence FOREIGN KEY
            (task_id,source_agent_run_id,finalization_epoch,web_evidence_sequence_id)
            REFERENCES qc_web_evidence_sequence (task_id,source_agent_run_id,finalization_epoch,sequence_id),
        ADD CONSTRAINT uq_input_set_web_rule UNIQUE (input_set_id,rule_id,rule_contract_hash);
    ALTER TABLE qc_inspection_work_item_rule
        ADD COLUMN evidence_input_set_id varchar(96),
        ADD CONSTRAINT fk_work_rule_web_input FOREIGN KEY (evidence_input_set_id,rule_id,contract_hash)
            REFERENCES qc_evidence_input_set (input_set_id,rule_id,rule_contract_hash);
    CREATE FUNCTION qc_guard_web_work_rule_input() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='UPDATE' AND OLD.evidence_input_set_id IS DISTINCT FROM NEW.evidence_input_set_id THEN
            RAISE EXCEPTION 'WEB_SEQUENCE_BINDING_IMMUTABLE' USING ERRCODE='23514';
        END IF;
        IF NEW.evidence_input_set_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM qc_inspection_work_item work
            JOIN qc_evidence_input_set inputs ON inputs.task_id=work.task_id
                AND inputs.source_agent_run_id=work.producer_agent_run_id
                AND inputs.finalization_epoch=work.finalization_epoch
            JOIN qc_web_evidence_sequence sequence ON sequence.sequence_id=inputs.web_evidence_sequence_id
                AND sequence.anchor_capture_id=work.page_capture_id
            WHERE work.id=NEW.work_item_id AND inputs.input_set_id=NEW.evidence_input_set_id
                AND inputs.scope='web_sequence' AND inputs.rule_id=NEW.rule_id
                AND inputs.rule_contract_hash=NEW.contract_hash
        ) THEN RAISE EXCEPTION 'WEB_SEQUENCE_RULE_BINDING' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER tr_web_work_rule_input BEFORE INSERT OR UPDATE ON qc_inspection_work_item_rule
        FOR EACH ROW EXECUTE FUNCTION qc_guard_web_work_rule_input();
    CREATE TABLE qc_web_capability_revision (
        revision_id uuid PRIMARY KEY,
        task_id uuid NOT NULL REFERENCES qc_task(id) ON DELETE CASCADE,
        source_agent_run_id uuid NOT NULL,
        finalization_epoch integer NOT NULL CHECK (finalization_epoch >= 0),
        capture_id uuid NOT NULL,
        target_id varchar(64) NOT NULL,
        state_key varchar(64) NOT NULL,
        revision integer NOT NULL CHECK (revision > 0),
        assessment_payload jsonb NOT NULL CHECK (jsonb_typeof(assessment_payload)='object'),
        created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (task_id,source_agent_run_id) REFERENCES qc_agent_run(task_id,id)
            DEFERRABLE INITIALLY DEFERRED,
        FOREIGN KEY (task_id,capture_id) REFERENCES qc_capture(task_id,id)
            DEFERRABLE INITIALLY DEFERRED,
        UNIQUE (task_id,source_agent_run_id,finalization_epoch,target_id,state_key,revision)
    );
    CREATE UNIQUE INDEX uq_web_capability_capture ON qc_web_capability_revision
        (task_id,source_agent_run_id,finalization_epoch,capture_id);
    CREATE FUNCTION qc_guard_web_capability_revision() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF TG_OP='DELETE' THEN
            IF EXISTS(SELECT 1 FROM qc_task WHERE id=OLD.task_id) THEN
                RAISE EXCEPTION 'WEB_CAPABILITY_IMMUTABLE' USING ERRCODE='23514';
            END IF;
            RETURN OLD;
        END IF;
        IF TG_OP='UPDATE' THEN
            RAISE EXCEPTION 'WEB_CAPABILITY_IMMUTABLE' USING ERRCODE='23514';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtext('task-finalization:'||NEW.task_id::text));
        IF NOT EXISTS (
            SELECT 1 FROM qc_task task
            JOIN qc_agent_run run ON run.task_id=task.id AND run.id=NEW.source_agent_run_id
            JOIN qc_surface_session session ON session.task_id=task.id AND session.session_id='web:'||run.id::text
            JOIN qc_capture capture ON capture.task_id=task.id AND capture.id=NEW.capture_id
                AND capture.agent_run_id=run.id AND capture.surface_session_id=session.session_id
            JOIN qc_web_run_policy_snapshot policy ON policy.task_id=task.id AND policy.agent_run_id=run.id
            JOIN qc_evidence_manifest manifest ON manifest.task_id=task.id AND manifest.capture_id=capture.id
                AND manifest.manifest_id=NEW.assessment_payload->>'manifest_id'
                AND manifest.manifest_hash=NEW.assessment_payload->>'manifest_hash' AND manifest.quality_status='pass'
            WHERE task.id=NEW.task_id AND task.target_platform='web' AND task.status='running'
                AND task.task_config->>'workflow_version'='v2'
                AND task.task_config->>'execution_mode'='full_inspection'
                AND task.task_config->>'web_inspection_notification_mode'='enforced'
                AND task.finalization_epoch=NEW.finalization_epoch
                AND task.finalization_state IN ('open','exploration_draining','session_draining')
                AND run.agent_type='main' AND run.status IN ('running','done','failed','aborted')
                AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                AND run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
                    AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
                AND session.session_owner='web-v2-workflow' AND session.surface_kind='web'
                AND session.finalization_epoch=NEW.finalization_epoch AND session.status IN ('active','draining')
                AND capture.capture_status='captured' AND capture.capture_role='after_action'
                AND capture.metadata->>'finalization_epoch'=NEW.finalization_epoch::text
                AND NEW.assessment_payload->>'task_id'=NEW.task_id::text
                AND NEW.assessment_payload->>'source_agent_run_id'=NEW.source_agent_run_id::text
                AND NEW.assessment_payload->>'capture_id'=NEW.capture_id::text
                AND NEW.assessment_payload->>'finalization_epoch'=NEW.finalization_epoch::text
                AND NEW.assessment_payload->>'revision_id'=NEW.revision_id::text
                AND NEW.assessment_payload->>'revision'=NEW.revision::text
                AND NEW.assessment_payload->>'target_id'=NEW.target_id
                AND NEW.assessment_payload->>'state_key'=NEW.state_key
                AND NEW.assessment_payload->>'scope'='current_document_light_dom'
                AND NEW.assessment_payload->>'predicate'='native_video_element_present'
                AND NEW.assessment_payload->>'status' IN ('present','local_absent','unknown')
                AND NEW.assessment_payload->>'product_capability_status'='unknown'
        ) THEN RAISE EXCEPTION 'WEB_CAPABILITY_SCOPE' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER tr_web_capability_revision BEFORE INSERT OR UPDATE OR DELETE ON qc_web_capability_revision
        FOR EACH ROW EXECUTE FUNCTION qc_guard_web_capability_revision();
    END $migration$;
    """)


def downgrade() -> None:
    op.execute("""
    DO $migration$ BEGIN
    DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM qc_web_evidence_sequence)
           OR EXISTS(SELECT 1 FROM qc_web_capability_revision)
           OR EXISTS(SELECT 1 FROM qc_inspection_work_item_rule WHERE evidence_input_set_id IS NOT NULL)
        THEN RAISE EXCEPTION 'Web 序列或能力审计仍存在，拒绝有损降级'; END IF;
    END $$;
    DROP TABLE qc_web_capability_revision;
    DROP FUNCTION IF EXISTS qc_guard_web_capability_revision();
    DROP TRIGGER tr_web_work_rule_input ON qc_inspection_work_item_rule;
    DROP FUNCTION qc_guard_web_work_rule_input();
    ALTER TABLE qc_inspection_work_item_rule DROP COLUMN evidence_input_set_id;
    ALTER TABLE qc_evidence_input_set DROP CONSTRAINT uq_input_set_web_rule,
        DROP CONSTRAINT ck_input_set_web_sequence,
        DROP COLUMN web_evidence_sequence_id,
        DROP CONSTRAINT ck_qc_evidence_input_set_scope;
    ALTER TABLE qc_evidence_input_set ADD CONSTRAINT ck_qc_evidence_input_set_scope
        CHECK (scope IN ('page','journey','task','environment_matrix'));
    DROP TRIGGER tr_web_sequence_publication ON qc_h5_inspection_publication;
    DROP FUNCTION qc_guard_web_sequence_publication();
    ALTER TABLE qc_h5_inspection_publication DROP CONSTRAINT uq_qc_h5_publication_capture_epoch;
    ALTER TABLE qc_h5_inspection_publication DROP CONSTRAINT ck_publication_web_sequence,
        DROP COLUMN web_evidence_sequence_id,
        ALTER COLUMN page_type_assessment_id SET NOT NULL,
        ALTER COLUMN assessment_hash SET NOT NULL;
    ALTER TABLE qc_h5_inspection_publication ADD CONSTRAINT uq_qc_h5_publication_capture_epoch
        UNIQUE(task_id,page_capture_id,finalization_epoch);
    DROP TABLE qc_web_evidence_sequence_member;
    DROP FUNCTION qc_guard_web_sequence_member();
    DROP TABLE qc_web_evidence_sequence;
    DROP FUNCTION qc_guard_web_evidence_sequence();
    END $migration$;
    """)
