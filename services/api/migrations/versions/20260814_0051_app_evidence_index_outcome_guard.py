"""对齐 App Evidence Index 的结果门禁与最新 Manifest 合同。

Revision ID: 20260814_0051
Revises: 20260813_0050
Create Date: 2026-08-13
"""

from __future__ import annotations

from alembic import op

revision = "20260814_0051"
down_revision = "20260813_0050"
branch_labels = None
depends_on = None


_LATEST_ENTRY_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_app_evidence_index_entry_latest()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    index_record qc_app_evidence_index%ROWTYPE;
    capture_record qc_capture%ROWTYPE;
    manifest_record qc_evidence_manifest%ROWTYPE;
    latest_manifest_id varchar(96);
    latest_manifest_version integer;
    expected_semantic_role varchar(32);
BEGIN
    SELECT * INTO index_record
    FROM qc_app_evidence_index
    WHERE index_id = NEW.index_id;

    SELECT * INTO capture_record
    FROM qc_capture
    WHERE id = NEW.capture_id;

    SELECT * INTO manifest_record
    FROM qc_evidence_manifest
    WHERE manifest_id = NEW.evidence_manifest_id;

    SELECT manifest.manifest_id, manifest.manifest_version
    INTO latest_manifest_id, latest_manifest_version
    FROM qc_evidence_manifest AS manifest
    WHERE manifest.capture_id = NEW.capture_id
    ORDER BY manifest.manifest_version DESC
    LIMIT 1;

    IF index_record.index_id IS NULL
       OR capture_record.id IS NULL
       OR manifest_record.manifest_id IS NULL THEN
        RAISE EXCEPTION 'Evidence Index Entry 缺少 Index、Capture 或 Manifest 事实';
    END IF;
    IF index_record.status IS DISTINCT FROM 'building'
       OR NEW.task_id IS DISTINCT FROM index_record.task_id THEN
        RAISE EXCEPTION 'Evidence Index Entry 只能写入同一任务的 building Index';
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.index_id IS DISTINCT FROM OLD.index_id
        OR NEW.sequence_no IS DISTINCT FROM OLD.sequence_no
        OR NEW.capture_id IS DISTINCT FROM OLD.capture_id
        OR NEW.evidence_manifest_id IS DISTINCT FROM OLD.evidence_manifest_id
        OR NEW.evidence_manifest_version IS DISTINCT FROM OLD.evidence_manifest_version
    ) THEN
        RAISE EXCEPTION 'Evidence Index Entry 身份字段禁止修改';
    END IF;
    IF capture_record.task_id IS DISTINCT FROM index_record.task_id
       OR capture_record.agent_run_id IS DISTINCT FROM index_record.agent_run_id
       OR capture_record.surface_session_id IS DISTINCT FROM index_record.surface_session_id
       OR capture_record.capture_status IS DISTINCT FROM 'captured'
       OR capture_record.surface_kind NOT IN ('android', 'ios') THEN
        RAISE EXCEPTION 'Evidence Index Entry 的 App Capture 归属或状态不一致';
    END IF;
    IF manifest_record.task_id IS DISTINCT FROM index_record.task_id
       OR manifest_record.session_id IS DISTINCT FROM index_record.surface_session_id
       OR manifest_record.capture_id IS DISTINCT FROM NEW.capture_id
       OR manifest_record.manifest_version IS DISTINCT FROM NEW.evidence_manifest_version
       OR manifest_record.quality_status IS DISTINCT FROM 'pass' THEN
        RAISE EXCEPTION 'Evidence Index Entry 只能引用同一 App Run 的 PASS Manifest';
    END IF;
    IF latest_manifest_id IS DISTINCT FROM NEW.evidence_manifest_id
       OR latest_manifest_version IS DISTINCT FROM NEW.evidence_manifest_version THEN
        RAISE EXCEPTION 'Evidence Index Entry 只能引用 Capture 的最新 Manifest';
    END IF;
    expected_semantic_role := CASE capture_record.capture_role
        WHEN 'bootstrap' THEN 'bootstrap'
        WHEN 'before_action' THEN
            CASE
                WHEN NULLIF(capture_record.metadata->>'surface_phase_run_id', '')
                    IS NOT NULL THEN 'surface_before'
                ELSE 'action_before'
            END
        WHEN 'after_action' THEN
            CASE
                WHEN NULLIF(capture_record.metadata->>'surface_phase_run_id', '')
                    IS NOT NULL THEN 'surface_after'
                ELSE 'action_after'
            END
        WHEN 'observation' THEN
            CASE capture_record.metadata->>'semantic_role'
                WHEN 'final_observation' THEN 'final_observation'
                WHEN 'surface_reobserve' THEN 'surface_reobserve'
                ELSE NULL
            END
        ELSE NULL
    END;
    IF expected_semantic_role IS NULL
       OR NEW.semantic_role IS DISTINCT FROM expected_semantic_role THEN
        RAISE EXCEPTION 'Evidence Index Entry 的 semantic_role 与 Capture 角色不一致';
    END IF;
    RETURN NEW;
END;
$$
"""


_OUTCOME_HEADER_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_app_evidence_index_header()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    sealed_replay boolean := false;
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM qc_agent_run AS run
            INNER JOIN qc_surface_session AS session
                ON session.task_id = run.task_id
               AND session.session_id = NEW.surface_session_id
            WHERE run.id = NEW.agent_run_id
              AND run.task_id = NEW.task_id
              AND session.surface_kind IN ('android', 'ios')
              AND session.platform_build_ref IS NOT DISTINCT FROM NEW.build_id
        ) THEN
            RAISE EXCEPTION 'Evidence Index 的 Agent Run、任务或 App Session 归属不一致';
        END IF;
        IF NEW.status = 'sealed' THEN
            RAISE EXCEPTION 'Evidence Index 必须先以 building 创建，禁止直接 INSERT sealed';
        END IF;
    END IF;
    IF TG_OP = 'DELETE' AND OLD.status = 'sealed' THEN
        RAISE EXCEPTION 'Evidence Index 已封存，禁止删除';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND (
        NEW.index_id IS DISTINCT FROM OLD.index_id
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.agent_run_id IS DISTINCT FROM OLD.agent_run_id
        OR NEW.build_id IS DISTINCT FROM OLD.build_id
        OR NEW.surface_session_id IS DISTINCT FROM OLD.surface_session_id
        OR (
            NEW.result_context_sha256 IS DISTINCT FROM OLD.result_context_sha256
            AND NOT (
                OLD.status = 'building'
                AND NEW.status = 'building'
                AND OLD.result_context_sha256 IS NULL
                AND NEW.result_context_sha256 ~ '^sha256:[0-9a-f]{64}$'
            )
        )
        OR NEW.protocol_version IS DISTINCT FROM OLD.protocol_version
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'Evidence Index 头身份字段禁止修改';
    END IF;
    IF TG_OP = 'UPDATE' AND OLD.status = 'sealed' THEN
        IF NEW.index_id = OLD.index_id
           AND NEW.task_id = OLD.task_id
           AND NEW.agent_run_id = OLD.agent_run_id
           AND NEW.build_id = OLD.build_id
           AND NEW.surface_session_id = OLD.surface_session_id
           AND NEW.result_context_sha256 IS NOT DISTINCT FROM OLD.result_context_sha256
           AND NEW.protocol_version = OLD.protocol_version
           AND NEW.status = OLD.status
           AND NEW.outcome = OLD.outcome
           AND NEW.terminal_reason_code = OLD.terminal_reason_code
           AND NEW.content_sha256 = OLD.content_sha256
           AND NEW.evidence_gaps = OLD.evidence_gaps
           AND NEW.sealed_at = OLD.sealed_at
           AND NEW.created_at = OLD.created_at THEN
            sealed_replay := true;
        ELSE
            RAISE EXCEPTION 'Evidence Index 已封存，禁止修改';
        END IF;
    END IF;
    IF NEW.status = 'sealed' THEN
        PERFORM pg_advisory_xact_lock(
            hashtext('app-evidence-index:' || NEW.agent_run_id::text)
        );
        IF NOT EXISTS (
            SELECT 1
            FROM qc_agent_run AS run
            INNER JOIN qc_surface_session AS session
                ON session.task_id = run.task_id
               AND session.session_id = NEW.surface_session_id
            WHERE run.id = NEW.agent_run_id
              AND run.task_id = NEW.task_id
              AND session.surface_kind IN ('android', 'ios')
              AND session.platform_build_ref IS NOT DISTINCT FROM NEW.build_id
        ) THEN
            RAISE EXCEPTION 'Evidence Index 的 Agent Run、任务或 App Session 归属不一致';
        END IF;
        IF NEW.content_sha256 IS NULL
           OR NEW.content_sha256 !~ '^sha256:[0-9a-f]{64}$'
           OR NEW.sealed_at IS NULL THEN
            RAISE EXCEPTION '封存必须提供 content_sha256 和 sealed_at';
        END IF;
        IF NOT sealed_replay AND (
            NEW.result_context_sha256 IS NULL
            OR NEW.result_context_sha256 !~ '^sha256:[0-9a-f]{64}$'
        ) THEN
            RAISE EXCEPTION '新封存 Evidence Index 必须绑定探索结果上下文';
        END IF;

        IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.evidence_gaps) AS gap(value)
            WHERE jsonb_typeof(gap.value) IS DISTINCT FROM 'object'
               OR gap.value->>'protocol_version' IS DISTINCT FROM 'app-evidence-gap.v1'
               OR gap.value->>'semantic_role' IS NULL
               OR gap.value->>'semantic_role' NOT IN (
                    'bootstrap', 'surface_before', 'surface_after',
                    'surface_reobserve', 'action_before', 'action_after',
                    'final_observation'
               )
               OR NULLIF(btrim(gap.value->>'reason_code'), '') IS NULL
               OR NULLIF(btrim(gap.value->>'description'), '') IS NULL
               OR NOT (gap.value ? 'capture_id')
               OR jsonb_typeof(gap.value->'capture_id') NOT IN ('null', 'string')
               OR jsonb_typeof(gap.value->'metadata') IS DISTINCT FROM 'object'
        ) THEN
            RAISE EXCEPTION 'Evidence Index evidence_gaps 合同无效';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.evidence_gaps) AS gap(value)
            WHERE jsonb_typeof(gap.value->'capture_id') = 'string'
              AND gap.value->>'capture_id' !~* (
                  '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                  '[0-9a-f]{4}-[0-9a-f]{12}$'
              )
        ) THEN
            RAISE EXCEPTION 'Evidence Index gap.capture_id 不是合法 UUID';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.evidence_gaps) AS gap(value)
            WHERE jsonb_typeof(gap.value->'capture_id') = 'null'
              AND gap.value->'metadata'->>'scope' IS DISTINCT FROM 'run'
        ) THEN
            RAISE EXCEPTION '无 capture_id 的 Evidence Gap 必须声明 run scope';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.evidence_gaps) AS gap(value)
            GROUP BY
                gap.value->>'capture_id',
                gap.value->>'semantic_role',
                gap.value->>'reason_code'
            HAVING count(*) > 1
        ) THEN
            RAISE EXCEPTION 'Evidence Index 禁止重复 Evidence Gap';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM jsonb_array_elements(NEW.evidence_gaps) AS gap(value)
            LEFT JOIN qc_capture AS capture
                ON capture.id::text = gap.value->>'capture_id'
               AND capture.task_id = NEW.task_id
               AND capture.agent_run_id = NEW.agent_run_id
               AND capture.surface_session_id = NEW.surface_session_id
               AND capture.capture_status = 'captured'
               AND capture.surface_kind IN ('android', 'ios')
            WHERE jsonb_typeof(gap.value->'capture_id') = 'string'
              AND (
                  capture.id IS NULL
                  OR gap.value->>'semantic_role' IS DISTINCT FROM CASE capture.capture_role
                      WHEN 'bootstrap' THEN 'bootstrap'
                      WHEN 'before_action' THEN
                          CASE
                              WHEN NULLIF(capture.metadata->>'surface_phase_run_id', '')
                                  IS NOT NULL THEN 'surface_before'
                              ELSE 'action_before'
                          END
                      WHEN 'after_action' THEN
                          CASE
                              WHEN NULLIF(capture.metadata->>'surface_phase_run_id', '')
                                  IS NOT NULL THEN 'surface_after'
                              ELSE 'action_after'
                          END
                      WHEN 'observation' THEN
                          CASE capture.metadata->>'semantic_role'
                              WHEN 'final_observation' THEN 'final_observation'
                              WHEN 'surface_reobserve' THEN 'surface_reobserve'
                              ELSE NULL
                          END
                      ELSE NULL
                  END
              )
        ) THEN
            RAISE EXCEPTION 'Evidence Gap 的 Capture 归属或 semantic_role 不一致';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM qc_capture AS capture
            WHERE capture.task_id = NEW.task_id
              AND capture.agent_run_id = NEW.agent_run_id
              AND capture.surface_session_id = NEW.surface_session_id
              AND capture.capture_status = 'captured'
              AND capture.surface_kind NOT IN ('android', 'ios')
        ) THEN
            RAISE EXCEPTION 'App Evidence Index 不能包含非 App Capture';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM qc_app_evidence_index_entry AS entry
            INNER JOIN qc_capture AS capture ON capture.id = entry.capture_id
            LEFT JOIN LATERAL (
                SELECT manifest.manifest_id, manifest.manifest_version,
                       manifest.task_id, manifest.session_id, manifest.capture_id,
                       manifest.quality_status
                FROM qc_evidence_manifest AS manifest
                WHERE manifest.capture_id = capture.id
                ORDER BY manifest.manifest_version DESC
                LIMIT 1
            ) AS latest ON TRUE
            WHERE entry.index_id = NEW.index_id
              AND (
                  capture.task_id IS DISTINCT FROM NEW.task_id
                  OR capture.agent_run_id IS DISTINCT FROM NEW.agent_run_id
                  OR capture.surface_session_id IS DISTINCT FROM NEW.surface_session_id
                  OR capture.capture_status IS DISTINCT FROM 'captured'
                  OR capture.surface_kind NOT IN ('android', 'ios')
                  OR latest.manifest_id IS DISTINCT FROM entry.evidence_manifest_id
                  OR latest.manifest_version IS DISTINCT FROM entry.evidence_manifest_version
                  OR latest.task_id IS DISTINCT FROM NEW.task_id
                  OR latest.session_id IS DISTINCT FROM NEW.surface_session_id
                  OR latest.capture_id IS DISTINCT FROM capture.id
                  OR latest.quality_status IS DISTINCT FROM 'pass'
                  OR entry.semantic_role IS DISTINCT FROM CASE capture.capture_role
                      WHEN 'bootstrap' THEN 'bootstrap'
                      WHEN 'before_action' THEN
                          CASE
                              WHEN NULLIF(capture.metadata->>'surface_phase_run_id', '')
                                  IS NOT NULL THEN 'surface_before'
                              ELSE 'action_before'
                          END
                      WHEN 'after_action' THEN
                          CASE
                              WHEN NULLIF(capture.metadata->>'surface_phase_run_id', '')
                                  IS NOT NULL THEN 'surface_after'
                              ELSE 'action_after'
                          END
                      WHEN 'observation' THEN
                          CASE capture.metadata->>'semantic_role'
                              WHEN 'final_observation' THEN 'final_observation'
                              WHEN 'surface_reobserve' THEN 'surface_reobserve'
                              ELSE NULL
                          END
                      ELSE NULL
                  END
              )
        ) THEN
            RAISE EXCEPTION 'Evidence Index Entry 未引用当前最新 PASS Manifest';
        END IF;

        IF NEW.outcome = 'exploration_completed' THEN
            IF jsonb_array_length(NEW.evidence_gaps) <> 0 THEN
                RAISE EXCEPTION 'exploration_completed 禁止携带 Evidence Gap';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM qc_capture AS capture
                LEFT JOIN LATERAL (
                    SELECT manifest.manifest_id, manifest.manifest_version,
                           manifest.quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.capture_id = capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS latest ON TRUE
                WHERE capture.task_id = NEW.task_id
                  AND capture.agent_run_id = NEW.agent_run_id
                  AND capture.surface_session_id = NEW.surface_session_id
                  AND capture.capture_status = 'captured'
                  AND latest.quality_status IS DISTINCT FROM 'pass'
            ) THEN
                RAISE EXCEPTION 'exploration_completed 要求所有最新 Manifest 均为 PASS';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM qc_capture AS capture
                LEFT JOIN LATERAL (
                    SELECT manifest.manifest_id, manifest.manifest_version
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.capture_id = capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS latest ON TRUE
                WHERE capture.task_id = NEW.task_id
                  AND capture.agent_run_id = NEW.agent_run_id
                  AND capture.surface_session_id = NEW.surface_session_id
                  AND capture.capture_status = 'captured'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_app_evidence_index_entry AS entry
                      WHERE entry.index_id = NEW.index_id
                        AND entry.capture_id = capture.id
                        AND entry.evidence_manifest_id = latest.manifest_id
                        AND entry.evidence_manifest_version = latest.manifest_version
                  )
            ) THEN
                RAISE EXCEPTION 'exploration_completed 要求全部 Capture 进入 Evidence Index';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                FROM qc_app_evidence_index_entry AS entry
                WHERE entry.index_id = NEW.index_id
                  AND entry.semantic_role = 'final_observation'
            ) THEN
                RAISE EXCEPTION 'exploration_completed 缺少 Final Observation Entry';
            END IF;
        ELSIF NEW.outcome = 'evidence_insufficient' THEN
            IF EXISTS (
                SELECT 1
                FROM qc_capture AS capture
                LEFT JOIN LATERAL (
                    SELECT manifest.manifest_id, manifest.manifest_version
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.capture_id = capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS latest ON TRUE
                WHERE capture.task_id = NEW.task_id
                  AND capture.agent_run_id = NEW.agent_run_id
                  AND capture.surface_session_id = NEW.surface_session_id
                  AND capture.capture_status = 'captured'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM qc_app_evidence_index_entry AS entry
                      WHERE entry.index_id = NEW.index_id
                        AND entry.capture_id = capture.id
                        AND entry.evidence_manifest_id = latest.manifest_id
                        AND entry.evidence_manifest_version = latest.manifest_version
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements(NEW.evidence_gaps) AS gap(value)
                      WHERE gap.value->>'capture_id' = capture.id::text
                  )
            ) THEN
                RAISE EXCEPTION 'evidence_insufficient 的未索引 Capture 缺少对应 Evidence Gap';
            END IF;
        END IF;
    END IF;
    IF sealed_replay THEN
        RETURN OLD;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$
"""


_SEALED_MANIFEST_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_sealed_app_evidence_manifest()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_agent_run_id uuid;
    new_agent_run_id uuid;
    old_index_sealed boolean := false;
    new_index_sealed boolean := false;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        SELECT capture.agent_run_id
        INTO old_agent_run_id
        FROM qc_capture AS capture
        WHERE capture.id = OLD.capture_id;
    END IF;
    IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
        SELECT 1
        FROM qc_app_evidence_index_entry AS entry
        INNER JOIN qc_app_evidence_index AS evidence_index
            ON evidence_index.index_id = entry.index_id
        WHERE entry.evidence_manifest_id = OLD.manifest_id
          AND evidence_index.status IN ('building', 'sealed')
    ) THEN
        IF TG_OP = 'UPDATE'
           AND NEW.manifest_id = OLD.manifest_id
           AND NEW.manifest_version = OLD.manifest_version
           AND NEW.task_id = OLD.task_id
           AND NEW.session_id = OLD.session_id
           AND NEW.capture_id = OLD.capture_id
           AND NEW.protocol_version = OLD.protocol_version
           AND NEW.manifest_hash = OLD.manifest_hash
           AND NEW.payload_hash = OLD.payload_hash
           AND NEW.quality_status = OLD.quality_status
           AND NEW.quality_reason IS NOT DISTINCT FROM OLD.quality_reason
           AND NEW.manifest_payload = OLD.manifest_payload
           AND NEW.created_at = OLD.created_at
           AND NEW.persisted_at = OLD.persisted_at THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'Manifest 已被 App Evidence Index 引用，禁止修改或删除';
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        SELECT capture.agent_run_id
        INTO new_agent_run_id
        FROM qc_capture AS capture
        WHERE capture.id = NEW.capture_id;
    END IF;

    IF old_agent_run_id IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            hashtext('app-evidence-index:' || old_agent_run_id::text)
        );
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = OLD.task_id
              AND evidence_index.agent_run_id = old_agent_run_id
              AND evidence_index.surface_session_id = OLD.session_id
        ) INTO old_index_sealed;
    END IF;
    IF old_index_sealed THEN
        IF TG_OP = 'UPDATE'
           AND NEW.manifest_id = OLD.manifest_id
           AND NEW.manifest_version = OLD.manifest_version
           AND NEW.task_id = OLD.task_id
           AND NEW.session_id = OLD.session_id
           AND NEW.capture_id = OLD.capture_id
           AND NEW.protocol_version = OLD.protocol_version
           AND NEW.manifest_hash = OLD.manifest_hash
           AND NEW.payload_hash = OLD.payload_hash
           AND NEW.quality_status = OLD.quality_status
           AND NEW.quality_reason IS NOT DISTINCT FROM OLD.quality_reason
           AND NEW.manifest_payload = OLD.manifest_payload
           AND NEW.created_at = OLD.created_at
           AND NEW.persisted_at = OLD.persisted_at THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Manifest';
    END IF;

    IF new_agent_run_id IS NOT NULL
       AND new_agent_run_id IS DISTINCT FROM old_agent_run_id THEN
        PERFORM pg_advisory_xact_lock(
            hashtext('app-evidence-index:' || new_agent_run_id::text)
        );
    END IF;
    IF new_agent_run_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = NEW.task_id
              AND evidence_index.agent_run_id = new_agent_run_id
              AND evidence_index.surface_session_id = NEW.session_id
        ) INTO new_index_sealed;
    END IF;
    IF new_index_sealed THEN
        IF TG_OP = 'INSERT' AND EXISTS (
            SELECT 1
            FROM qc_evidence_manifest AS existing
            WHERE existing.manifest_id = NEW.manifest_id
              AND existing.manifest_version = NEW.manifest_version
              AND existing.task_id = NEW.task_id
              AND existing.session_id = NEW.session_id
              AND existing.capture_id = NEW.capture_id
              AND existing.protocol_version = NEW.protocol_version
              AND existing.manifest_hash = NEW.manifest_hash
              AND existing.payload_hash = NEW.payload_hash
              AND existing.quality_status = NEW.quality_status
              AND existing.quality_reason IS NOT DISTINCT FROM NEW.quality_reason
              AND existing.manifest_payload = NEW.manifest_payload
              AND existing.created_at = NEW.created_at
        ) THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Manifest';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""


_SEALED_SESSION_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_sealed_app_evidence_session()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM qc_app_evidence_index AS evidence_index
        WHERE evidence_index.status = 'sealed'
          AND evidence_index.task_id = OLD.task_id
          AND evidence_index.surface_session_id = OLD.session_id
    ) AND (
        NEW.session_id IS DISTINCT FROM OLD.session_id
        OR NEW.task_id IS DISTINCT FROM OLD.task_id
        OR NEW.surface_kind IS DISTINCT FROM OLD.surface_kind
        OR NEW.platform_build_ref IS DISTINCT FROM OLD.platform_build_ref
    ) THEN
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止修改 Session 证据身份';
    END IF;
    RETURN NEW;
END;
$$
"""


_SEALED_CAPTURE_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_sealed_app_evidence_capture()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    old_agent_run_id uuid;
    new_agent_run_id uuid;
    old_index_sealed boolean := false;
    new_index_sealed boolean := false;
BEGIN
    IF TG_OP IN ('UPDATE', 'DELETE') THEN
        old_agent_run_id := OLD.agent_run_id;
    END IF;
    IF TG_OP IN ('INSERT', 'UPDATE') THEN
        new_agent_run_id := NEW.agent_run_id;
    END IF;

    IF old_agent_run_id IS NOT NULL
       AND new_agent_run_id IS NOT NULL
       AND old_agent_run_id IS DISTINCT FROM new_agent_run_id THEN
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || LEAST(old_agent_run_id::text, new_agent_run_id::text)
            )
        );
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || GREATEST(old_agent_run_id::text, new_agent_run_id::text)
            )
        );
    ELSIF COALESCE(old_agent_run_id, new_agent_run_id) IS NOT NULL THEN
        PERFORM pg_advisory_xact_lock(
            hashtext(
                'app-evidence-index:'
                || COALESCE(old_agent_run_id, new_agent_run_id)::text
            )
        );
    END IF;

    IF old_agent_run_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = OLD.task_id
              AND evidence_index.agent_run_id = old_agent_run_id
              AND evidence_index.surface_session_id = OLD.surface_session_id
        ) INTO old_index_sealed;
    END IF;
    IF old_index_sealed THEN
        IF TG_OP = 'UPDATE'
           AND NEW.id = OLD.id
           AND NEW.task_id = OLD.task_id
           AND NEW.agent_run_id IS NOT DISTINCT FROM OLD.agent_run_id
           AND NEW.surface_kind = OLD.surface_kind
           AND NEW.surface_session_id IS NOT DISTINCT FROM OLD.surface_session_id
           AND NEW.parent_capture_id IS NOT DISTINCT FROM OLD.parent_capture_id
           AND NEW.action_execution_id IS NOT DISTINCT FROM OLD.action_execution_id
           AND NEW.capture_role = OLD.capture_role
           AND NEW.session_command_sequence
                IS NOT DISTINCT FROM OLD.session_command_sequence
           AND NEW.capture_status = OLD.capture_status
           AND NEW.captured_at IS NOT DISTINCT FROM OLD.captured_at
           AND NEW.metadata = OLD.metadata
           AND NEW.created_at = OLD.created_at
           AND NEW.updated_at = OLD.updated_at THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Capture';
    END IF;

    IF new_agent_run_id IS NOT NULL THEN
        SELECT EXISTS (
            SELECT 1
            FROM qc_app_evidence_index AS evidence_index
            WHERE evidence_index.status = 'sealed'
              AND evidence_index.task_id = NEW.task_id
              AND evidence_index.agent_run_id = new_agent_run_id
              AND evidence_index.surface_session_id = NEW.surface_session_id
        ) INTO new_index_sealed;
    END IF;
    IF new_index_sealed THEN
        RAISE EXCEPTION 'App Evidence Index 已封存，禁止追加、修改或删除 Capture';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$
"""


_LEGACY_HEADER_GUARD_SQL = """
CREATE OR REPLACE FUNCTION qc_guard_app_evidence_index_header()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NOT EXISTS (
            SELECT 1
            FROM qc_agent_run AS run
            WHERE run.id = NEW.agent_run_id
              AND run.task_id = NEW.task_id
        ) THEN
            RAISE EXCEPTION 'Evidence Index 的 Agent Run 与任务归属不一致';
        END IF;
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND OLD.status = 'sealed' THEN
        RAISE EXCEPTION 'Evidence Index 已封存，禁止删除';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    IF OLD.status = 'sealed' THEN
        IF NEW.index_id = OLD.index_id
           AND NEW.task_id = OLD.task_id
           AND NEW.agent_run_id = OLD.agent_run_id
           AND NEW.build_id = OLD.build_id
           AND NEW.surface_session_id = OLD.surface_session_id
           AND NEW.protocol_version = OLD.protocol_version
           AND NEW.status = OLD.status
           AND NEW.outcome = OLD.outcome
           AND NEW.terminal_reason_code = OLD.terminal_reason_code
           AND NEW.content_sha256 = OLD.content_sha256
           AND NEW.evidence_gaps = OLD.evidence_gaps
           AND NEW.sealed_at = OLD.sealed_at
           AND NEW.created_at = OLD.created_at THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'Evidence Index 已封存，禁止修改';
    END IF;
    IF NEW.status = 'sealed' THEN
        IF NEW.content_sha256 IS NULL OR NEW.sealed_at IS NULL THEN
            RAISE EXCEPTION '封存必须提供 content_sha256 和 sealed_at';
        END IF;
        IF EXISTS (
            SELECT 1
            FROM qc_capture AS capture
            LEFT JOIN LATERAL (
                SELECT manifest.quality_status
                FROM qc_evidence_manifest AS manifest
                WHERE manifest.capture_id = capture.id
                ORDER BY manifest.manifest_version DESC
                LIMIT 1
            ) AS latest ON TRUE
            WHERE capture.task_id = NEW.task_id
              AND capture.surface_session_id = NEW.surface_session_id
              AND capture.agent_run_id = NEW.agent_run_id
              AND capture.capture_status = 'captured'
              AND latest.quality_status IS DISTINCT FROM 'pass'
        ) THEN
            RAISE EXCEPTION '存在尚未 PASS 的最新 Manifest，禁止封存';
        END IF;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END;
$$
"""


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE qc_app_evidence_index
        ADD COLUMN result_context_sha256 varchar(71)
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_app_evidence_index AS evidence_index
                WHERE evidence_index.status = 'sealed'
                  AND (
                      EXISTS (
                          SELECT 1
                          FROM qc_app_evidence_index_entry AS entry
                          INNER JOIN qc_capture AS capture ON capture.id = entry.capture_id
                          LEFT JOIN LATERAL (
                              SELECT manifest.manifest_id, manifest.manifest_version,
                                     manifest.quality_status
                              FROM qc_evidence_manifest AS manifest
                              WHERE manifest.capture_id = capture.id
                              ORDER BY manifest.manifest_version DESC
                              LIMIT 1
                          ) AS latest ON TRUE
                          WHERE entry.index_id = evidence_index.index_id
                            AND (
                                capture.task_id IS DISTINCT FROM evidence_index.task_id
                                OR capture.agent_run_id IS DISTINCT FROM evidence_index.agent_run_id
                                OR capture.surface_session_id
                                    IS DISTINCT FROM evidence_index.surface_session_id
                                OR latest.manifest_id
                                    IS DISTINCT FROM entry.evidence_manifest_id
                                OR latest.manifest_version
                                    IS DISTINCT FROM entry.evidence_manifest_version
                                OR latest.quality_status IS DISTINCT FROM 'pass'
                            )
                      )
                      OR (
                          evidence_index.outcome = 'exploration_completed'
                          AND NOT EXISTS (
                              SELECT 1
                              FROM qc_app_evidence_index_entry AS entry
                              WHERE entry.index_id = evidence_index.index_id
                                AND entry.semantic_role = 'final_observation'
                          )
                      )
                  )
            ) THEN
                RAISE EXCEPTION
                    '存在不符合 0050 合同的已封存 App Evidence Index，禁止升级';
            END IF;
        END
        $$
        """
    )
    op.execute(_LATEST_ENTRY_GUARD_SQL)
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_evidence_index_entry_latest_guard
        BEFORE INSERT OR UPDATE ON qc_app_evidence_index_entry
        FOR EACH ROW EXECUTE FUNCTION qc_guard_app_evidence_index_entry_latest()
        """
    )
    op.execute(_OUTCOME_HEADER_GUARD_SQL)
    op.execute(_SEALED_MANIFEST_GUARD_SQL)
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_evidence_manifest_sealed_guard
        BEFORE INSERT OR UPDATE OR DELETE ON qc_evidence_manifest
        FOR EACH ROW EXECUTE FUNCTION qc_guard_sealed_app_evidence_manifest()
        """
    )
    op.execute(_SEALED_CAPTURE_GUARD_SQL)
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_evidence_capture_sealed_guard
        BEFORE INSERT OR UPDATE OR DELETE ON qc_capture
        FOR EACH ROW EXECUTE FUNCTION qc_guard_sealed_app_evidence_capture()
        """
    )
    op.execute(_SEALED_SESSION_GUARD_SQL)
    op.execute(
        """
        CREATE TRIGGER trg_qc_app_evidence_session_sealed_guard
        BEFORE UPDATE ON qc_surface_session
        FOR EACH ROW EXECUTE FUNCTION qc_guard_sealed_app_evidence_session()
        """
    )
    # 让新 Header Guard 对历史 sealed 事实执行一次完整校验；任一异常会回滚整次迁移。
    op.execute(
        """
        UPDATE qc_app_evidence_index
        SET updated_at = updated_at
        WHERE status = 'sealed'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM qc_app_evidence_index AS evidence_index
                WHERE evidence_index.result_context_sha256 IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    '存在已绑定结果上下文且旧哈希合同无法表达的 Index，禁止降级到 0049';
            END IF;
            IF EXISTS (
                SELECT 1
                FROM qc_app_evidence_index AS evidence_index
                INNER JOIN qc_capture AS capture
                    ON capture.task_id = evidence_index.task_id
                   AND capture.agent_run_id = evidence_index.agent_run_id
                   AND capture.surface_session_id = evidence_index.surface_session_id
                   AND capture.capture_status = 'captured'
                LEFT JOIN LATERAL (
                    SELECT manifest.quality_status
                    FROM qc_evidence_manifest AS manifest
                    WHERE manifest.capture_id = capture.id
                    ORDER BY manifest.manifest_version DESC
                    LIMIT 1
                ) AS latest ON TRUE
                WHERE evidence_index.status = 'sealed'
                  AND latest.quality_status IS DISTINCT FROM 'pass'
            ) THEN
                RAISE EXCEPTION
                    '存在已封存但最新 Manifest 非 PASS 的 App Capture，禁止降级到 0049';
            END IF;
        END
        $$
        """
    )
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_evidence_session_sealed_guard
        ON qc_surface_session
        """
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_sealed_app_evidence_session()")
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_evidence_capture_sealed_guard
        ON qc_capture
        """
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_sealed_app_evidence_capture()")
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_evidence_manifest_sealed_guard
        ON qc_evidence_manifest
        """
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_sealed_app_evidence_manifest()")
    op.execute(_LEGACY_HEADER_GUARD_SQL)
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_qc_app_evidence_index_entry_latest_guard
        ON qc_app_evidence_index_entry
        """
    )
    op.execute("DROP FUNCTION IF EXISTS qc_guard_app_evidence_index_entry_latest()")
    op.execute("ALTER TABLE qc_app_evidence_index DROP COLUMN result_context_sha256")
