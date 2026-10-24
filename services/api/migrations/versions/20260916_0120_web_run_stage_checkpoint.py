"""新增 Web 阶段持久化游标和待领取索引。

Revision ID: 20260916_0120
Revises: 20260914_0119
"""

from alembic import op

revision = "20260916_0120"
down_revision = "20260914_0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION qc_web_checkpoint_gap_audit_valid(gaps jsonb, codes jsonb)
        RETURNS boolean LANGUAGE sql IMMUTABLE AS $$
            SELECT CASE
                WHEN jsonb_typeof(gaps) IS DISTINCT FROM 'array'
                  OR jsonb_typeof(codes) IS DISTINCT FROM 'array' THEN false
                ELSE NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(gaps) AS entry(value)
                    WHERE NOT COALESCE(
                        jsonb_typeof(value)='object'
                        AND value->>'stage' IN (
                            'bootstrap_navigation','obstruction_clear','readiness','primary_capture',
                            'full_page_capture','classification','href_discovery','candidate_navigation',
                            'frontier_drain','evidence_reconciliation','coverage_snapshot',
                            'report_finalization','session_close','ad_isolated_navigation',
                            'ad_capture','ad_closeout')
                        AND jsonb_typeof(value->'stage_generation')='number'
                        AND value->>'stage_generation' ~ '^(0|[1-9][0-9]*)$'
                        AND jsonb_typeof(value->'attempt_count')='number'
                        AND value->>'attempt_count' ~ '^[1-9][0-9]*$'
                        AND jsonb_typeof(value->'code')='string'
                        AND value->>'code' ~ '^[A-Z][A-Z0-9_]{0,127}$'
                        AND jsonb_typeof(value->'error_code')='string'
                        AND value->>'error_code' ~ '^[A-Z][A-Z0-9_]{0,127}$'
                        AND codes @> jsonb_build_array(value->'code'), false)
                ) AND NOT EXISTS (
                    SELECT 1 FROM jsonb_array_elements(codes) AS entry(value)
                    WHERE jsonb_typeof(value) <> 'string'
                       OR NOT EXISTS (SELECT 1 FROM jsonb_array_elements(gaps) AS gap(value)
                                      WHERE gap.value->'code'=entry.value)
                )
            END
        $$;
    """)
    op.execute("""
        CREATE TABLE qc_web_run_stage_checkpoint (
            run_id uuid NOT NULL REFERENCES qc_agent_run(id) ON DELETE CASCADE,
            logical_page_key varchar(512) NOT NULL,
            current_stage varchar(32) NOT NULL DEFAULT 'bootstrap_navigation',
            stage_status varchar(16) NOT NULL DEFAULT 'pending',
            stage_generation bigint NOT NULL DEFAULT 0,
            cursor_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            attempt_count integer NOT NULL DEFAULT 0,
            next_attempt_at timestamptz,
            last_error_code varchar(128),
            last_error_detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            gap_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            gaps jsonb NOT NULL DEFAULT '[]'::jsonb,
            obstruction_attempts jsonb NOT NULL DEFAULT '{}'::jsonb,
            artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            row_version bigint NOT NULL DEFAULT 0,
            claimed_worker_id varchar(512),
            claim_token uuid,
            claimed_at timestamptz,
            lease_expires_at timestamptz,
            initial_identity varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, logical_page_key),
            CONSTRAINT ck_qc_web_checkpoint_stage CHECK (current_stage IN (
                'bootstrap_navigation','obstruction_clear','readiness','primary_capture',
                'full_page_capture','classification','href_discovery','candidate_navigation',
                'frontier_drain','evidence_reconciliation','coverage_snapshot',
                'report_finalization','session_close','ad_isolated_navigation',
                'ad_capture','ad_closeout')),
            CONSTRAINT ck_qc_web_checkpoint_status CHECK (stage_status IN (
                'pending','running','retry_wait','succeeded','gapped','cancelled')),
            CONSTRAINT ck_qc_web_checkpoint_numbers CHECK (
                stage_generation >= 0 AND attempt_count >= 0 AND row_version >= 0),
            CONSTRAINT ck_qc_web_checkpoint_retry CHECK (
                (stage_status='retry_wait') = (next_attempt_at IS NOT NULL)),
            CONSTRAINT ck_qc_web_checkpoint_claim CHECK (
                stage_status <> 'running' OR (
                    claim_token IS NOT NULL AND claimed_worker_id IS NOT NULL
                    AND claimed_at IS NOT NULL AND lease_expires_at IS NOT NULL
                    AND lease_expires_at > claimed_at
                    AND attempt_count > 0)),
            CONSTRAINT ck_qc_web_checkpoint_identity CHECK (
                logical_page_key <> '' AND initial_identity ~ '^[a-f0-9]{64}$'),
            CONSTRAINT ck_qc_web_checkpoint_json CHECK (
                jsonb_typeof(cursor_payload)='object'
                AND jsonb_typeof(last_error_detail)='object'
                AND jsonb_typeof(obstruction_attempts)='object'
                AND jsonb_typeof(gap_codes)='array' AND jsonb_typeof(gaps)='array'
                AND jsonb_typeof(artifact_ids)='array'),
            CONSTRAINT ck_qc_web_checkpoint_gap_audit CHECK (
                qc_web_checkpoint_gap_audit_valid(gaps, gap_codes)),
            CONSTRAINT ck_qc_web_checkpoint_gapped CHECK (
                stage_status <> 'gapped' OR (
                    attempt_count > 0 AND last_error_code IS NOT NULL
                    AND gaps @> jsonb_build_array(jsonb_build_object(
                        'stage', current_stage, 'stage_generation', stage_generation,
                        'attempt_count', attempt_count, 'error_code', last_error_code))
                )),
            CONSTRAINT ck_qc_web_checkpoint_reference_only CHECK (
                cursor_payload - ARRAY['parent_capture_id','capture_id','menu_ledger_id',
                    'session_id','idempotency_key','obstruction_fingerprint',
                    'surface_phase_run_id','candidate_href_index','finalization_epoch',
                    'current_url','requested_url','final_url']::text[] = '{}'::jsonb
                AND last_error_detail - ARRAY['parent_capture_id','capture_id','menu_ledger_id',
                    'session_id','idempotency_key','obstruction_fingerprint',
                    'surface_phase_run_id','candidate_href_index','finalization_epoch',
                    'current_url','requested_url','final_url']::text[] = '{}'::jsonb)
        )
    """)
    op.execute("""CREATE INDEX ix_qc_web_checkpoint_retry_due
        ON qc_web_run_stage_checkpoint (next_attempt_at, run_id)
        WHERE stage_status='retry_wait'""")
    op.execute("""CREATE INDEX ix_qc_web_checkpoint_lease_due
        ON qc_web_run_stage_checkpoint (lease_expires_at, run_id)
        WHERE stage_status='running'""")
    op.execute("""CREATE INDEX ix_qc_web_checkpoint_pending
        ON qc_web_run_stage_checkpoint (updated_at, run_id)
        WHERE stage_status IN ('pending','succeeded','gapped')
          AND current_stage NOT IN ('session_close','ad_closeout')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX ix_qc_web_checkpoint_pending")
    op.execute("DROP INDEX ix_qc_web_checkpoint_lease_due")
    op.execute("DROP INDEX ix_qc_web_checkpoint_retry_due")
    op.execute("DROP TABLE qc_web_run_stage_checkpoint")
    op.execute("DROP FUNCTION qc_web_checkpoint_gap_audit_valid(jsonb, jsonb)")
