"""Web V2 菜单与单详情硬预算的 PostgreSQL 仓储。"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from services.api.app.application.web_detail_selection_diagnostics import (
    bounded_detail_selection_diagnostic,
)
from services.api.app.application.web_exploration_budget_ledger import (
    WebDetailActionReservation,
    WebExplorationBudgetLedgerConflictError,
    WebMenuActionReservation,
    WebMenuClaimRejection,
    WebMenuLedgerTerminalRecord,
)
from services.api.app.application.web_exploration_workflow import RunWebWorkflowCommand
from services.api.app.application.web_menu_exploration import (
    WEB_MENU_COLLECTION_LIMIT,
    WebMenuExplorationSummary,
    WebMenuLedgerEntry,
    WebRuntimeDiscoveredLedgerEntry,
    plan_web_menu_collection,
)
from services.api.app.domain.exploration import (
    WEB_DETAIL_PAGE_TYPES,
    WEB_LIST_PAGE_TYPES,
    WebMenuTerminalStatus,
)
from services.api.app.domain.page import (
    WebCatalogStatus,
    WebMenuDecision,
    WebSiteMenuCatalog,
    WebSiteMenuContext,
    WebSiteMenuEntry,
)
from services.api.app.infrastructure.db.pool import get_postgres_pool

if TYPE_CHECKING:
    from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

_LOCK_BOOTSTRAP_SESSION_SQL = """
    SELECT session.task_id, session.session_id
    FROM qc_surface_session AS session
    INNER JOIN qc_agent_run AS run
        ON run.id = $2::uuid
       AND run.task_id = session.task_id
    WHERE session.task_id = $1::uuid
      AND session.session_id = $3
      AND session.surface_kind = 'web'
      AND session.session_owner = 'web-v2-workflow'
      AND session.status IN ('active', 'draining')
    FOR UPDATE OF session
"""


_READ_BOOTSTRAP_BUDGET_SQL = """
    SELECT budget.*
    FROM qc_web_exploration_budget AS budget
    WHERE budget.agent_run_id = $1::uuid
      AND budget.site_id = $2
    FOR UPDATE OF budget
"""


_INSERT_BOOTSTRAP_BUDGET_SQL = """
    INSERT INTO qc_web_exploration_budget (
        agent_run_id, site_id, task_id, session_id,
        surface_kind, session_owner, target_origin_sha256,
        catalog_version, catalog_sha256, menu_limit, state
    )
    VALUES (
        $2::uuid, $4, $1::uuid, $3,
        'web', 'web-v2-workflow', $5, $6, $7, $8, 'open'
    )
    ON CONFLICT (agent_run_id, site_id) DO NOTHING
    RETURNING *
"""


_BOOTSTRAP_ENTRIES_SQL = """
    WITH locked_budget AS (
        SELECT budget.agent_run_id, budget.site_id
        FROM qc_web_exploration_budget AS budget
        WHERE budget.agent_run_id = $1::uuid
          AND budget.site_id = $2
          AND budget.state = 'open'
          AND (budget.menu_limit IS NULL OR budget.menu_limit > 0)
        FOR UPDATE OF budget
    ), incoming AS (
        SELECT *
        FROM jsonb_to_recordset($3::jsonb) AS item(
            menu_entry_id varchar,
            catalog_position integer,
            decision varchar,
            menu_path jsonb,
            canonical_url_sha256 varchar,
            execution_slot integer,
            status varchar,
            diagnostic jsonb
        )
    )
    INSERT INTO qc_web_menu_ledger_entry (
        agent_run_id, site_id, menu_entry_id, catalog_position,
        decision, menu_path, canonical_url_sha256, execution_slot, status,
        diagnostic
    )
    SELECT
        budget.agent_run_id, budget.site_id, item.menu_entry_id,
        item.catalog_position, item.decision, item.menu_path,
        item.canonical_url_sha256, item.execution_slot, item.status,
        item.diagnostic
    FROM locked_budget AS budget
    CROSS JOIN incoming AS item
    RETURNING menu_entry_id, catalog_position, decision, menu_path,
        canonical_url_sha256, execution_slot, status, diagnostic
"""


_READ_MATERIALIZED_ENTRIES_SQL = """
    SELECT
        entry.menu_entry_id, entry.catalog_position, entry.decision,
        entry.menu_path, entry.canonical_url_sha256,
        entry.execution_slot, entry.status, entry.diagnostic,
        entry.menu_action_count, entry.detail_action_count
    FROM qc_web_menu_ledger_entry AS entry
    WHERE entry.agent_run_id = $1::uuid
      AND entry.site_id = $2
      AND entry.source_kind = 'catalog'
    ORDER BY entry.catalog_position, entry.menu_entry_id
"""


_SOURCE_CAPTURE_VALIDATION_SQL = """
    SELECT source.id, budget.menu_limit
    FROM qc_web_exploration_budget AS budget
    INNER JOIN qc_surface_session AS session
        ON session.task_id = budget.task_id
       AND session.session_id = budget.session_id
    INNER JOIN qc_agent_run AS run
        ON run.id = budget.agent_run_id
       AND run.task_id = budget.task_id
    INNER JOIN qc_capture AS source
        ON source.id = $4::uuid
       AND source.task_id = budget.task_id
       AND source.agent_run_id = budget.agent_run_id
       AND source.surface_session_id = budget.session_id
       AND source.surface_kind = 'web'
       AND source.capture_status = 'captured'
    WHERE budget.task_id = $1::uuid
      AND budget.agent_run_id = $2::uuid
      AND budget.session_id = $3
      AND budget.state = 'open'
      AND session.surface_kind = 'web'
      AND session.session_owner = 'web-v2-workflow'
      AND session.status IN ('active', 'draining')
    FOR UPDATE OF budget
    FOR KEY SHARE OF source
"""


_CLAIM_MENU_SQL = """
    WITH locked_entry AS (
        SELECT
            entry.agent_run_id, entry.site_id, entry.menu_entry_id,
            entry.execution_slot, entry.menu_action_count,
            entry.menu_intent_id, entry.menu_source_capture_id,
            entry.status,
            CASE WHEN entry.source_kind='runtime_discovered'
              THEN 'web-discovery-menu:' || entry.agent_run_id::text || ':'
                || md5(entry.site_id || ':' || entry.menu_entry_id)
              ELSE 'web-menu:' || entry.agent_run_id::text || ':'
                || entry.site_id || ':' || entry.menu_entry_id END AS expected_intent_id
        FROM qc_web_menu_ledger_entry AS entry
        INNER JOIN qc_web_exploration_budget AS budget
            ON budget.agent_run_id = entry.agent_run_id
           AND budget.site_id = entry.site_id
        INNER JOIN qc_surface_session AS session
            ON session.task_id = budget.task_id
           AND session.session_id = budget.session_id
        INNER JOIN qc_agent_run AS run
            ON run.id = budget.agent_run_id
           AND run.task_id = budget.task_id
        INNER JOIN qc_capture AS source
            ON source.id = $5::uuid
           AND source.task_id = budget.task_id
           AND source.agent_run_id = budget.agent_run_id
           AND source.surface_session_id = budget.session_id
           AND source.surface_kind = 'web'
           AND source.capture_status = 'captured'
        WHERE budget.task_id = $1::uuid
          AND budget.agent_run_id = $2::uuid
          AND budget.session_id = $3
          AND budget.state = 'open'
          AND (budget.menu_limit IS NULL OR budget.menu_limit > 0)
          AND session.surface_kind = 'web'
          AND session.session_owner = 'web-v2-workflow'
          AND session.status IN ('active', 'draining')
          AND entry.menu_entry_id = $4
          AND entry.execution_slot IS NOT NULL
          AND (budget.menu_limit IS NULL OR entry.execution_slot <= budget.menu_limit)
          AND (
              (entry.source_kind='catalog' AND entry.decision='allowlisted_observe_only')
              OR (entry.source_kind='runtime_discovered' AND entry.decision='runtime_observe_only'
                  AND entry.discovery_manifest->'authorization'->>'source_capture_id'=$5::text)
          )
          AND entry.status IN ('pending', 'menu_reserved', 'detail_reserved')
        FOR UPDATE OF entry
    ), reserved AS (
        UPDATE qc_web_menu_ledger_entry AS entry
        SET
            menu_action_count = 1,
            menu_intent_id = locked.expected_intent_id,
            menu_source_capture_id = $5::uuid,
            status = 'menu_reserved',
            row_version = entry.row_version + 1,
            updated_at = now()
        FROM locked_entry AS locked
        WHERE entry.agent_run_id = locked.agent_run_id
          AND entry.site_id = locked.site_id
          AND entry.menu_entry_id = locked.menu_entry_id
          AND locked.menu_action_count = 0
        RETURNING entry.site_id, entry.menu_entry_id, entry.execution_slot,
            entry.menu_intent_id, false AS already_reserved
    )
    SELECT * FROM reserved
    UNION ALL
    SELECT
        locked.site_id, locked.menu_entry_id, locked.execution_slot,
        locked.menu_intent_id, true AS already_reserved
    FROM locked_entry AS locked
    WHERE locked.menu_action_count = 1
      AND locked.menu_intent_id = locked.expected_intent_id
      AND locked.menu_source_capture_id = $5::uuid
      AND NOT EXISTS (SELECT 1 FROM reserved)
    LIMIT 1
"""


_RESERVE_DETAIL_SQL = """
    WITH locked_entry AS (
        SELECT
            entry.agent_run_id, entry.site_id, entry.menu_entry_id,
            entry.menu_action_count, entry.detail_action_count,
            entry.detail_intent_id, entry.detail_source_capture_id,
            entry.detail_candidate_id, entry.detail_destination_sha256,
            entry.status,
            CASE WHEN entry.source_kind='runtime_discovered'
              THEN 'web-discovery-detail:' || entry.agent_run_id::text || ':'
                || md5(entry.site_id || ':' || entry.menu_entry_id)
              ELSE 'web-detail:' || entry.agent_run_id::text || ':'
                || entry.site_id || ':' || entry.menu_entry_id END AS expected_intent_id
        FROM qc_web_menu_ledger_entry AS entry
        INNER JOIN qc_web_exploration_budget AS budget
            ON budget.agent_run_id = entry.agent_run_id
           AND budget.site_id = entry.site_id
        INNER JOIN qc_surface_session AS session
            ON session.task_id = budget.task_id
           AND session.session_id = budget.session_id
        INNER JOIN qc_agent_run AS run
            ON run.id = budget.agent_run_id
           AND run.task_id = budget.task_id
        INNER JOIN qc_capture AS source
            ON source.id = $5::uuid
           AND source.task_id = budget.task_id
           AND source.agent_run_id = budget.agent_run_id
           AND source.surface_session_id = budget.session_id
           AND source.surface_kind = 'web'
           AND source.capture_status = 'captured'
        WHERE budget.task_id = $1::uuid
          AND budget.agent_run_id = $2::uuid
          AND budget.session_id = $3
          AND budget.state = 'open'
          AND (budget.menu_limit IS NULL OR budget.menu_limit > 0)
          AND session.surface_kind = 'web'
          AND session.session_owner = 'web-v2-workflow'
          AND session.status IN ('active', 'draining')
          AND entry.menu_entry_id = $4
          AND entry.execution_slot IS NOT NULL
          AND (budget.menu_limit IS NULL OR entry.execution_slot <= budget.menu_limit)
          AND entry.menu_action_count = 1
        FOR UPDATE OF entry
    ), reserved AS (
        UPDATE qc_web_menu_ledger_entry AS entry
        SET
            detail_action_count = 1,
            detail_intent_id = locked.expected_intent_id,
            detail_source_capture_id = $5::uuid,
            detail_candidate_id = $6,
            detail_destination_sha256 = $7,
            status = 'detail_reserved',
            row_version = entry.row_version + 1,
            updated_at = now()
        FROM locked_entry AS locked
        WHERE entry.agent_run_id = locked.agent_run_id
          AND entry.site_id = locked.site_id
          AND entry.menu_entry_id = locked.menu_entry_id
          AND locked.detail_action_count = 0
          AND locked.status = 'menu_reserved'
        RETURNING entry.site_id, entry.menu_entry_id,
            entry.detail_intent_id, false AS already_reserved
    )
    SELECT * FROM reserved
    UNION ALL
    SELECT
        locked.site_id, locked.menu_entry_id,
        locked.detail_intent_id, true AS already_reserved
    FROM locked_entry AS locked
    WHERE locked.detail_action_count = 1
      AND locked.detail_intent_id = locked.expected_intent_id
      AND locked.detail_source_capture_id = $5::uuid
      AND locked.detail_candidate_id = $6
      AND locked.detail_destination_sha256 = $7
      AND NOT EXISTS (SELECT 1 FROM reserved)
    LIMIT 1
"""


_FINALIZE_SQL = """
    WITH locked_entry AS (
        SELECT entry.*, budget.task_id, budget.menu_limit,
               session.finalization_epoch AS session_epoch, session.current_capture_id
        FROM qc_web_menu_ledger_entry AS entry
        INNER JOIN qc_web_exploration_budget AS budget
            ON budget.agent_run_id = entry.agent_run_id
           AND budget.site_id = entry.site_id
        INNER JOIN qc_surface_session AS session
            ON session.task_id = budget.task_id
           AND session.session_id = budget.session_id
        WHERE budget.task_id = $1::uuid
          AND budget.agent_run_id = $2::uuid
          AND budget.session_id = $3
          AND budget.site_id = $4
          AND session.surface_kind = 'web'
          AND session.session_owner = 'web-v2-workflow'
          AND entry.menu_entry_id = $5
          AND entry.status IN ('home_pending', 'pending', 'menu_reserved', 'detail_reserved')
        FOR UPDATE OF entry
    )
    UPDATE qc_web_menu_ledger_entry AS entry
    SET
        status = $6,
        menu_action_execution_id = $7,
        menu_probe_capture_id = $8::uuid,
        menu_ready_capture_id = $9::uuid,
        menu_page_type = $10,
        detail_action_execution_id = $11,
        detail_probe_capture_id = $12::uuid,
        detail_ready_capture_id = $13::uuid,
        detail_page_type = $14,
        detail_selection = $15::jsonb,
        normalization_version = CASE
            WHEN $6 = 'home_baseline'
              OR ($6 = 'menu_page_captured' AND $10 = 'collection_page') THEN (
                SELECT MIN(home_assessment.schema_version)
                FROM qc_h5_page_type_assessment AS home_assessment
                WHERE home_assessment.task_id = $1::uuid
                  AND home_assessment.agent_run_id = $2::uuid
                  AND home_assessment.session_id = $3
                  AND home_assessment.page_capture_id = $9::uuid
                  AND home_assessment.detection_status = 'recognized'
                  AND home_assessment.page_type = $10
                HAVING COUNT(*) = 1
            )
            ELSE entry.normalization_version
        END,
        diagnostic = $16::jsonb,
        row_version = entry.row_version + 1,
        updated_at = now()
    FROM locked_entry AS locked
    WHERE entry.agent_run_id = locked.agent_run_id
      AND entry.site_id = locked.site_id
      AND entry.menu_entry_id = locked.menu_entry_id
      AND locked.status IN ('home_pending', 'pending', 'menu_reserved', 'detail_reserved')
      AND ($7::varchar IS NULL OR (
          locked.menu_action_count = 1
          AND EXISTS (
              SELECT 1 FROM qc_action_execution AS action
              WHERE action.action_execution_id = $7
                AND action.task_id = $1::uuid
                AND action.session_id = $3
                AND action.intent_id = locked.menu_intent_id
          )
      ))
      AND ($11::varchar IS NULL OR (
          locked.detail_action_count = 1
          AND EXISTS (
              SELECT 1 FROM qc_action_execution AS action
              WHERE action.action_execution_id = $11
                AND action.task_id = $1::uuid
                AND action.session_id = $3
                AND action.intent_id = locked.detail_intent_id
          )
      ))
      AND NOT EXISTS (
          SELECT 1
          FROM unnest(ARRAY[
              locked.menu_source_capture_id,
              locked.detail_source_capture_id,
              $8::uuid,
              $9::uuid,
              $12::uuid,
              $13::uuid
          ]) AS wanted(id)
          WHERE wanted.id IS NOT NULL
            AND NOT EXISTS (
                SELECT 1 FROM qc_capture AS capture
                WHERE capture.id = wanted.id
                  AND capture.task_id = $1::uuid
                  AND capture.agent_run_id = $2::uuid
                  AND capture.surface_session_id = $3
                  AND capture.surface_kind = 'web'
                  AND capture.capture_status = 'captured'
              )
      )
      AND ($10::varchar IS NULL OR $8::uuid IS NOT NULL OR $9::uuid IS NOT NULL)
      AND ($14::varchar IS NULL OR $12::uuid IS NOT NULL OR $13::uuid IS NOT NULL)
      AND (
          ($12::uuid IS NULL AND $13::uuid IS NULL AND $14::varchar IS NULL)
          OR locked.detail_action_count = 1
      )
      AND (
          (
              $6 = 'home_baseline'
              AND locked.status = 'home_pending'
              AND locked.decision = 'home_baseline'
              AND locked.execution_slot IS NULL
              AND locked.menu_action_count = 0
              AND locked.menu_intent_id IS NULL
              AND locked.menu_source_capture_id IS NULL
              AND locked.detail_action_count = 0
              AND locked.detail_intent_id IS NULL
              AND locked.detail_source_capture_id IS NULL
              AND locked.detail_candidate_id IS NULL
              AND locked.detail_destination_sha256 IS NULL
              AND $7::varchar IS NULL
              AND $8::uuid IS NOT NULL
              AND $9::uuid IS NOT NULL
              AND EXISTS (
                  SELECT 1
                  FROM qc_h5_page_type_assessment AS home_assessment
                  WHERE home_assessment.task_id = locked.task_id
                    AND home_assessment.agent_run_id = locked.agent_run_id
                    AND home_assessment.session_id = $3
                    AND home_assessment.page_capture_id = $9::uuid
                    AND home_assessment.detection_status = 'recognized'
                    AND home_assessment.page_type = $10
                    AND (
                      (
                        home_assessment.schema_version IN (
                          'web-page-type-assessment.v3',
                          'web-page-type-assessment.v4'
                        )
                        AND $10 = 'home'
                      )
                      OR (
                        home_assessment.schema_version IN (
                          'web-page-type-normalization.v1',
                          'web-page-type-assessment.v1',
                          'web-page-type-assessment.v2'
                        )
                        AND $10 IN (
                          'content_list', 'video_list', 'content_detail', 'video_detail'
                        )
                      )
                    )
              )
              AND $11::varchar IS NULL
              AND $12::uuid IS NULL
              AND $13::uuid IS NULL
              AND $14::varchar IS NULL
              AND $15::jsonb = '{}'::jsonb
          )
          OR (
              $6 = 'page_type_unclassified'
              AND locked.status = 'home_pending'
              AND locked.decision = 'home_baseline'
              AND locked.execution_slot IS NULL
              AND locked.menu_action_count = 0
              AND locked.menu_intent_id IS NULL
              AND locked.menu_source_capture_id IS NULL
              AND locked.detail_action_count = 0
              AND locked.detail_intent_id IS NULL
              AND locked.detail_source_capture_id IS NULL
              AND locked.detail_candidate_id IS NULL
              AND locked.detail_destination_sha256 IS NULL
              AND $7::varchar IS NULL
              AND $8::uuid IS NOT NULL
              AND $9::uuid IS NOT NULL
              AND $10::varchar IS NULL
              AND $11::varchar IS NULL
              AND $12::uuid IS NULL
              AND $13::uuid IS NULL
              AND $14::varchar IS NULL
              AND $15::jsonb = '{}'::jsonb
              AND $16::jsonb @> '{"error_code":"WEB_PAGE_TYPE_UNCLASSIFIED"}'::jsonb
          )
          OR (
              $6 = 'navigation_failed'
              AND locked.status = 'home_pending'
              AND locked.decision = 'home_baseline'
              AND locked.execution_slot IS NULL
              AND locked.menu_action_count = 0 AND locked.detail_action_count = 0
              AND locked.menu_intent_id IS NULL AND locked.detail_intent_id IS NULL
              AND locked.menu_source_capture_id IS NULL AND locked.detail_source_capture_id IS NULL
              AND locked.detail_candidate_id IS NULL AND locked.detail_destination_sha256 IS NULL
              AND $7::varchar IS NULL AND $8::uuid IS NULL AND $9::uuid IS NULL
              AND $10::varchar IS NULL AND $11::varchar IS NULL
              AND $12::uuid IS NULL AND $13::uuid IS NULL AND $14::varchar IS NULL
              AND $15::jsonb = '{}'::jsonb
              AND EXISTS (
                  SELECT 1 FROM qc_web_run_stage_checkpoint checkpoint,
                      jsonb_array_elements(checkpoint.gaps) AS gap
                  WHERE checkpoint.run_id = locked.agent_run_id
                    AND checkpoint.logical_page_key = 'home'
                    AND checkpoint.cursor_payload->>'session_id' = $3
                    AND checkpoint.cursor_payload->>'finalization_epoch'
                        = locked.session_epoch::text
                    AND gap->>'stage' IN (
                        'bootstrap_navigation', 'readiness',
                        'primary_capture', 'candidate_navigation'
                    )
                    AND gap->>'code' = $16::jsonb->>'error_code'
              )
          )
          OR (
              $6 = 'menu_page_captured'
              AND locked.status = 'menu_reserved'
              AND locked.menu_action_count = 1
              AND locked.detail_action_count = 0
              AND $7::varchar IS NOT NULL
              AND $8::uuid IS NOT NULL
              AND $9::uuid IS NOT NULL
              AND (
                  $10 IN ('content_list', 'video_list')
                  OR (
                      $10 = 'collection_page'
                      AND EXISTS (
                          SELECT 1 FROM qc_action_execution AS collection_action
                          JOIN qc_capture AS before_capture
                            ON before_capture.id = collection_action.source_capture_id
                          JOIN qc_capture AS probe_capture
                            ON probe_capture.id = collection_action.result_capture_id
                          WHERE collection_action.action_execution_id = $7
                            AND collection_action.task_id = locked.task_id
                            AND collection_action.session_id = $3
                            AND collection_action.intent_id = locked.menu_intent_id
                            AND collection_action.status = 'completed'
                            AND collection_action.finalization_epoch = locked.session_epoch
                            AND collection_action.requested_source_capture_id
                                = locked.menu_source_capture_id
                            AND collection_action.result_capture_id = $8::uuid
                            AND before_capture.task_id = locked.task_id
                            AND before_capture.agent_run_id = locked.agent_run_id
                            AND before_capture.surface_session_id = $3
                            AND before_capture.capture_status = 'captured'
                            AND before_capture.capture_role = 'before_action'
                            AND before_capture.parent_capture_id = locked.menu_source_capture_id
                            AND before_capture.action_execution_id = $7
                            AND before_capture.session_command_sequence
                                = collection_action.command_sequence
                            AND probe_capture.capture_role = 'after_action'
                            AND probe_capture.parent_capture_id = before_capture.id
                            AND probe_capture.action_execution_id = $7
                            AND probe_capture.session_command_sequence
                                = collection_action.command_sequence
                      )
                      AND EXISTS (
                          SELECT 1 FROM qc_h5_page_type_assessment AS collection_assessment
                          JOIN qc_page_capture AS collection_page
                            ON collection_page.id=collection_assessment.page_capture_id
                           AND collection_page.task_id=collection_assessment.task_id
                          WHERE collection_assessment.task_id = locked.task_id
                            AND collection_assessment.agent_run_id = locked.agent_run_id
                            AND collection_assessment.session_id = $3
                            AND collection_assessment.page_capture_id = $9::uuid
                            AND collection_assessment.detection_status = 'recognized'
                            AND collection_assessment.page_type = 'collection_page'
                            AND collection_assessment.schema_version = 'web-page-type-assessment.v4'
                            AND collection_assessment.assessment_payload->>'catalog_version'=$17
                            AND collection_assessment.assessment_payload->>'catalog_sha256'=$18
                            AND collection_page.metadata
                                #>>'{web_page_type_assessment,catalog_version}'=$17
                            AND collection_page.metadata
                                #>>'{web_page_type_assessment,catalog_sha256}'=$18
                      )
                  )
              )
              AND $16::jsonb->>'type' IS DISTINCT FROM 'WEB_SUBMENU_EXPANDED'
              AND $11::varchar IS NULL
              AND $12::uuid IS NULL
              AND $13::uuid IS NULL
              AND $14::varchar IS NULL
              AND $15::jsonb = '{}'::jsonb
          )
          OR (
              $6 = 'menu_page_captured'
              AND locked.status = 'menu_reserved'
              AND locked.source_kind = 'runtime_discovered'
              AND locked.menu_action_count = 1
              AND locked.detail_action_count = 0
              AND $7::varchar IS NOT NULL
              AND $8::uuid IS NOT NULL
              AND $9::uuid IS NOT NULL
              AND $8::uuid = $9::uuid
              AND $10::varchar IS NULL
              AND $11::varchar IS NULL
              AND $12::uuid IS NULL
              AND $13::uuid IS NULL
              AND $14::varchar IS NULL
              AND $16::jsonb->>'type' = 'WEB_SUBMENU_EXPANDED'
              AND locked.detail_intent_id IS NULL
              AND locked.detail_source_capture_id IS NULL
              AND locked.detail_candidate_id IS NULL
              AND locked.detail_destination_sha256 IS NULL
              AND locked.detail_action_execution_id IS NULL
              AND locked.detail_probe_capture_id IS NULL
              AND locked.detail_ready_capture_id IS NULL
              AND locked.detail_page_type IS NULL
              AND locked.detail_selection = '{}'::jsonb
              AND $16::jsonb->'menu_interaction_outcome' @> '{
                  "command_accepted": true,
                  "effect_observed": true,
                  "postcondition_satisfied": true,
                  "transition_arrived": true,
                  "failure_reason": null,
                  "evidence_mode": "natural"
              }'::jsonb
              AND $16::jsonb->'menu_interaction_outcome'->>'after_capture_id' = $9::text
              AND EXISTS (
                  SELECT 1
                  FROM qc_action_execution AS action
                  JOIN qc_capture AS before_capture
                    ON before_capture.id = action.source_capture_id
                  JOIN qc_capture AS after_capture
                    ON after_capture.id = action.result_capture_id
                  WHERE action.action_execution_id = $7
                    AND action.task_id = $1::uuid
                    AND action.session_id = $3
                    AND action.intent_id = locked.menu_intent_id
                    AND action.status = 'completed'
                    AND action.finalization_epoch = locked.session_epoch
                    AND action.requested_source_capture_id = locked.menu_source_capture_id
                    AND action.result_capture_id = $8::uuid
                    AND before_capture.task_id = action.task_id
                    AND before_capture.agent_run_id = locked.agent_run_id
                    AND before_capture.surface_session_id = action.session_id
                    AND before_capture.surface_kind = 'web'
                    AND before_capture.capture_status = 'captured'
                    AND before_capture.capture_role = 'before_action'
                    AND before_capture.action_execution_id = action.action_execution_id
                    AND before_capture.parent_capture_id = action.requested_source_capture_id
                    AND before_capture.session_command_sequence = action.command_sequence
                    AND after_capture.task_id = action.task_id
                    AND after_capture.agent_run_id = locked.agent_run_id
                    AND after_capture.surface_session_id = action.session_id
                    AND after_capture.surface_kind = 'web'
                    AND after_capture.capture_status = 'captured'
                    AND after_capture.capture_role = 'after_action'
                    AND after_capture.parent_capture_id = before_capture.id
                    AND after_capture.action_execution_id = action.action_execution_id
                    AND after_capture.session_command_sequence = action.command_sequence
              )
          )
          OR (
              $6 = 'logical_target_reused'
              AND locked.status IN ('menu_reserved', 'detail_reserved')
              AND EXISTS (
                  SELECT 1 FROM qc_web_logical_target target
                  WHERE target.task_id=$1::uuid AND target.agent_run_id=$2::uuid
                    AND target.session_id=$3 AND target.state='materialized'
                    AND target.finalization_epoch=locked.session_epoch
                    AND target.identity_policy_version='web-url-identity.v2'
                    AND target.logical_page_id=$16::jsonb->>'reused_logical_page_id'
                    AND target.assessment_capture_id::text=$16::jsonb->>'reused_capture_id'
              )
              AND $7::varchar IS NOT NULL AND $8::uuid IS NOT NULL AND $9::uuid IS NOT NULL
              AND locked.current_capture_id=COALESCE($13::uuid,$9::uuid)
              AND COALESCE($13::uuid,$9::uuid)::text<>$16::jsonb->>'reused_capture_id'
              AND EXISTS(SELECT 1 FROM qc_action_execution observed
                  JOIN qc_capture before_capture ON before_capture.id=observed.source_capture_id
                  JOIN qc_capture after_capture ON after_capture.id=observed.result_capture_id
                  WHERE observed.action_execution_id=$7 AND observed.task_id=$1::uuid
                    AND observed.session_id=$3
                    AND observed.status='completed'
                    AND observed.finalization_epoch=locked.session_epoch
                    AND observed.result_capture_id=$8::uuid
                    AND after_capture.parent_capture_id=before_capture.id
                    AND before_capture.capture_role='before_action'
                    AND after_capture.action_execution_id=observed.action_execution_id)
              AND (locked.detail_action_count=0 OR (
                  $11::varchar IS NOT NULL AND $12::uuid IS NOT NULL AND $13::uuid IS NOT NULL))
          )
          OR (
              $6 = 'detail_captured'
              AND locked.status = 'menu_reserved'
              AND locked.menu_action_count = 1
              AND locked.detail_action_count = 0
              AND $7::varchar IS NOT NULL
              AND $8::uuid IS NOT NULL
              AND $9::uuid IS NOT NULL
              AND $10 IN ('content_detail', 'video_detail')
              AND $11::varchar IS NULL
              AND $12::uuid IS NULL
              AND $13::uuid IS NULL
              AND $14::varchar IS NULL
          )
          OR (
              $6 = 'detail_captured'
              AND locked.status = 'detail_reserved'
              AND locked.menu_action_count = 1
              AND locked.detail_action_count = 1
              AND $7::varchar IS NOT NULL
              AND $8::uuid IS NOT NULL
              AND $9::uuid IS NOT NULL
              AND $10 IN ('content_list', 'video_list')
              AND $11::varchar IS NOT NULL
              AND $12::uuid IS NOT NULL
              AND $13::uuid IS NOT NULL
              AND $14 IN ('content_detail', 'video_detail')
          )
          OR (
              $6 IN ('allowlist_drifted', 'human_verification_blocked')
              AND locked.status = 'pending'
              AND locked.decision = 'allowlisted_observe_only'
              AND locked.execution_slot >= 1
              AND (locked.menu_limit IS NULL OR locked.execution_slot <= locked.menu_limit)
              AND locked.menu_action_count = 0
              AND locked.menu_intent_id IS NULL
              AND locked.menu_source_capture_id IS NULL
              AND locked.detail_action_count = 0
              AND locked.detail_intent_id IS NULL
              AND locked.detail_source_capture_id IS NULL
              AND locked.detail_candidate_id IS NULL
              AND locked.detail_destination_sha256 IS NULL
              AND $7::varchar IS NULL
              AND $8::uuid IS NOT NULL
              AND $9::uuid IS NULL
              AND $10::varchar IS NULL
              AND $11::varchar IS NULL
              AND $12::uuid IS NULL
              AND $13::uuid IS NULL
              AND $14::varchar IS NULL
              AND $15::jsonb = '{}'::jsonb
          )
          OR (
              $6 IN (
                  'no_detail_candidate',
                  'popup_blocked',
                  'human_verification_blocked',
                  'unsafe_navigation',
                  'page_type_unclassified',
                  'detail_selection_untrusted',
                  'detail_page_type_mismatch',
                  'navigation_failed'
              )
              AND locked.status IN ('menu_reserved', 'detail_reserved')
              AND (
                  $6 <> 'human_verification_blocked'
                  OR $7::varchar IS NOT NULL
              )
          )
      )
    RETURNING entry.menu_entry_id
"""

_BOOTSTRAP_ONLY_TERMINAL_STATUSES = frozenset(
    {
        WebMenuTerminalStatus.CROSS_ORIGIN_BLOCKED,
        WebMenuTerminalStatus.MENU_LIMIT_SKIPPED,
        WebMenuTerminalStatus.RUNTIME_DISCOVERED_UNREVIEWED,
    }
)


_SEAL_BUDGET_SQL = """
    WITH locked_budget AS (
        SELECT
            budget.agent_run_id, budget.site_id, budget.state,
            budget.row_version, budget.sealed_at
        FROM qc_web_exploration_budget AS budget
        INNER JOIN qc_surface_session AS session
            ON session.task_id = budget.task_id
           AND session.session_id = budget.session_id
        INNER JOIN qc_agent_run AS run
            ON run.id = budget.agent_run_id
           AND run.task_id = budget.task_id
        INNER JOIN qc_task AS task
            ON task.id = budget.task_id
        WHERE budget.task_id = $1::uuid
          AND budget.agent_run_id = $2::uuid
          AND budget.session_id = $3
          AND budget.site_id = $4
          AND budget.surface_kind = 'web'
          AND budget.session_owner = 'web-v2-workflow'
          AND budget.state IN ('open', 'sealed')
          AND session.surface_kind = 'web'
          AND session.session_owner = 'web-v2-workflow'
          AND session.status IN ('active', 'draining')
          AND run.agent_type = 'main'
          AND run.status IN ('running', 'done')
          AND task.status = 'running'
          AND run.id = (
                SELECT latest.id
                FROM qc_agent_run AS latest
                WHERE latest.task_id = task.id
                  AND latest.agent_type = 'main'
                ORDER BY latest.created_at DESC, latest.id DESC
                LIMIT 1
              )
          AND (
                task.finalization_state = 'open'
                OR (
                    task.finalization_state = 'exploration_draining'
                    AND task.finalization_agent_run_id = budget.agent_run_id
                )
              )
        FOR UPDATE OF budget
    ), terminal_facts AS (
        SELECT
            count(entry.menu_entry_id) AS entry_count,
            count(*) FILTER (
                WHERE entry.status IN (
                    'pending', 'home_pending', 'menu_reserved', 'detail_reserved'
                )
            ) AS nonterminal_count
        FROM locked_budget AS budget
        LEFT JOIN qc_web_menu_ledger_entry AS entry
            ON entry.agent_run_id = budget.agent_run_id
           AND entry.site_id = budget.site_id
    ), sealed AS (
        UPDATE qc_web_exploration_budget AS budget
        SET state = 'sealed',
            sealed_at = COALESCE(budget.sealed_at, now()),
            row_version = budget.row_version + 1,
            updated_at = now()
        FROM locked_budget AS locked, terminal_facts AS facts
        WHERE budget.agent_run_id = locked.agent_run_id
          AND budget.site_id = locked.site_id
          AND locked.state = 'open'
          AND facts.entry_count > 0
          AND facts.nonterminal_count = 0
        RETURNING budget.state, budget.row_version, budget.sealed_at
    )
    SELECT state, row_version, sealed_at FROM sealed
    UNION ALL
    SELECT locked.state, locked.row_version, locked.sealed_at
    FROM locked_budget AS locked, terminal_facts AS facts
    WHERE locked.state = 'sealed'
      AND facts.entry_count > 0
      AND facts.nonterminal_count = 0
      AND NOT EXISTS (SELECT 1 FROM sealed)
    LIMIT 1
"""


class PostgresWebExplorationBudgetLedgerRepository:
    """在事务内先锁定并持久化预算，再允许调用方发浏览器动作。"""

    def __init__(
        self, *, database_url: str | None = None, pool: Any | None = None, frozen_policy=None
    ) -> None:
        self._database_url = database_url
        self._pool = pool
        self._frozen_policy = frozen_policy

    async def bootstrap(
        self,
        command: RunWebWorkflowCommand,
        site: WebSiteMenuCatalog | WebSiteMenuContext,
        *,
        catalog_version: str,
        catalog_sha256: str,
    ) -> None:
        _validate_command(command)
        if isinstance(site, WebSiteMenuContext):
            if (
                catalog_version != site.site.catalog_version
                or catalog_sha256.casefold() != site.content_sha256
            ):
                raise WebExplorationBudgetLedgerConflictError("Web 预算与站点上下文摘要不一致")
            site = site.site
        if not isinstance(site, WebSiteMenuCatalog):
            raise TypeError("site 必须为 WebSiteMenuCatalog")
        _require_text(catalog_version, "catalog_version")
        _require_sha256(catalog_sha256, "catalog_sha256")
        target_origin_sha256 = _sha256_text(_canonical_origin(site.origin))
        normalized_catalog_sha256 = catalog_sha256.casefold()
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            session = await connection.fetchrow(
                _LOCK_BOOTSTRAP_SESSION_SQL,
                command.task_id,
                command.agent_run_id,
                command.session_id,
            )
            if session is None:
                raise WebExplorationBudgetLedgerConflictError("Web 预算 Session 归属无效")
            budget = await connection.fetchrow(
                _READ_BOOTSTRAP_BUDGET_SQL,
                command.agent_run_id,
                site.site_id,
            )
            if budget is not None:
                require_budget_policy_match(budget, self._frozen_policy)
                if not _same_budget_identity(
                    budget,
                    command=command,
                    site_id=site.site_id,
                    target_origin_sha256=target_origin_sha256,
                    catalog_version=catalog_version,
                    catalog_sha256=normalized_catalog_sha256,
                ):
                    raise WebExplorationBudgetLedgerConflictError(
                        "Web 预算 Catalog/origin/session 发生漂移"
                    )
                payload = _catalog_payload(
                    site, menu_limit=budget["menu_limit"], frozen_policy=self._frozen_policy
                )
                persisted = await connection.fetch(
                    _READ_MATERIALIZED_ENTRIES_SQL,
                    command.agent_run_id,
                    site.site_id,
                )
                if not _same_materialized_catalog(
                    payload,
                    persisted,
                    allow_closeout=(
                        self._frozen_policy is not None
                        and self._frozen_policy.discovery_policy.enabled
                    ),
                ):
                    raise WebExplorationBudgetLedgerConflictError(
                        "Web 菜单账本与冻结 Catalog 发生漂移"
                    )
                return
            menu_limit = self._frozen_policy.menu_limit if self._frozen_policy is not None else None
            payload = _catalog_payload(
                site, menu_limit=menu_limit, frozen_policy=self._frozen_policy
            )
            created_budget = await connection.fetchrow(
                _INSERT_BOOTSTRAP_BUDGET_SQL,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                site.site_id,
                target_origin_sha256,
                catalog_version,
                normalized_catalog_sha256,
                menu_limit,
            )
            if created_budget is None:
                raise WebExplorationBudgetLedgerConflictError("Web 预算并发创建发生冲突")
            await connection.fetch(
                _BOOTSTRAP_ENTRIES_SQL,
                command.agent_run_id,
                site.site_id,
                _json_dumps(payload),
            )
            persisted = await connection.fetch(
                _READ_MATERIALIZED_ENTRIES_SQL,
                command.agent_run_id,
                site.site_id,
            )
            if not _same_materialized_catalog(payload, persisted):
                raise WebExplorationBudgetLedgerConflictError("Web 菜单账本与冻结 Catalog 发生漂移")

    async def claim_menu(
        self,
        *,
        command: RunWebWorkflowCommand,
        entry: WebSiteMenuEntry,
        source_capture_id: str,
    ) -> WebMenuActionReservation | WebMenuClaimRejection | None:
        _validate_command(command)
        from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

        runtime_discovery = isinstance(entry, WebRuntimeMenuEntry)
        if not runtime_discovery:
            _validate_entry(entry)
            if (
                self._frozen_policy is not None
                and self._frozen_policy.discovery_policy.enabled
                and (
                    min(command.deadline, self._frozen_policy.deadline) - datetime.now(UTC)
                ).total_seconds()
                <= 15
            ):
                return await self._closeout_catalog_claim(command, entry, source_capture_id)
        _require_text(source_capture_id, "source_capture_id")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            if runtime_discovery:
                from services.api.app.infrastructure.db.web_runtime_discovery_repository import (
                    authorize_discovery_slot,
                )

                if not await authorize_discovery_slot(
                    connection, command=command, entry=entry, source_capture_id=source_capture_id
                ):
                    return None
            source = await connection.fetchrow(
                _SOURCE_CAPTURE_VALIDATION_SQL,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                source_capture_id,
            )
            if source is None:
                raise WebExplorationBudgetLedgerConflictError(
                    "菜单 source Capture 不属于当前 captured Web Session"
                )
            require_budget_policy_match(source, self._frozen_policy)
            row = await connection.fetchrow(
                _CLAIM_MENU_SQL,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                entry.entry_id,
                source_capture_id,
            )
        if row is None:
            return None
        return WebMenuActionReservation(
            site_id=str(row["site_id"]),
            menu_entry_id=str(row["menu_entry_id"]),
            execution_slot=int(row["execution_slot"]),
            intent_id=str(row["menu_intent_id"]),
            already_reserved=bool(row["already_reserved"]),
        )

    async def _closeout_catalog_claim(self, command, entry, source_capture_id):
        """只结算仍有所有权的未领取目录，不把未知 reservation 当作预算耗尽。"""
        from services.api.app.application.web_run_policy import materialize_web_policy
        from services.api.app.infrastructure.db.web_logical_target_repository import _require_scope
        from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
            read_web_policy_on_connection,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await _require_scope(
                connection,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                source_capture_id,
            )
            policy = materialize_web_policy(
                await read_web_policy_on_connection(
                    connection, command.task_id, command.agent_run_id
                )
            )
            if (
                not policy.discovery_policy.enabled
                or (min(command.deadline, policy.deadline) - datetime.now(UTC)).total_seconds() > 15
            ):
                return None
            budget = await connection.fetchrow(
                "SELECT budget.* FROM qc_web_exploration_budget budget "
                "JOIN qc_task task ON task.id=budget.task_id "
                "WHERE budget.task_id=$1::uuid AND budget.agent_run_id=$2::uuid "
                "AND budget.session_id=$3 AND budget.state='open' "
                "AND task.status='running' AND task.finalization_state='open' "
                "FOR UPDATE OF budget,task",
                command.task_id,
                command.agent_run_id,
                command.session_id,
            )
            if budget is None:
                return None
            rows = await connection.fetch(
                "SELECT * FROM qc_web_menu_ledger_entry WHERE agent_run_id=$1::uuid "
                "AND site_id=$2 FOR UPDATE",
                command.agent_run_id,
                budget["site_id"],
            )
            if any(row["status"] in {"menu_reserved", "detail_reserved"} for row in rows):
                return None
            target = next((row for row in rows if row["menu_entry_id"] == entry.entry_id), None)
            if (
                target is None
                or target["source_kind"] != "catalog"
                or target["status"] != "pending"
                or target["menu_action_count"] != 0
                or target["decision"] != "allowlisted_observe_only"
                or target["canonical_url_sha256"] != _sha256_text(entry.canonical_url)
                or tuple(_json_value(target["menu_path"])) != entry.menu_path
            ):
                return None
            await connection.execute(
                "UPDATE qc_web_menu_ledger_entry SET status='navigation_failed', "
                'diagnostic=\'{"error_code":"WEB_MENU_CLOSEOUT_RESERVED"}\'::jsonb, '
                "row_version=row_version+1,updated_at=now() "
                "WHERE agent_run_id=$1::uuid AND site_id=$2 AND source_kind='catalog' "
                "AND decision='allowlisted_observe_only' AND status='pending' "
                "AND menu_action_count=0 AND detail_action_count=0",
                command.agent_run_id,
                budget["site_id"],
            )
            return WebMenuClaimRejection()

    async def reserve_detail(
        self,
        *,
        command: RunWebWorkflowCommand,
        entry: WebSiteMenuEntry | WebRuntimeMenuEntry,
        source_capture_id: str,
        candidate_id: str,
        destination_url: str,
    ) -> WebDetailActionReservation:
        _validate_command(command)
        from services.api.app.application.web_runtime_discovery import WebRuntimeMenuEntry

        runtime_discovery = isinstance(entry, WebRuntimeMenuEntry)
        if not runtime_discovery:
            _validate_entry(entry)
        _require_text(source_capture_id, "source_capture_id")
        _require_text(candidate_id, "candidate_id")
        _require_http_url(destination_url, "destination_url")
        if (
            self._frozen_policy is not None
            and self._frozen_policy.discovery_policy.enabled
            and (
                min(command.deadline, self._frozen_policy.deadline) - datetime.now(UTC)
            ).total_seconds()
            <= 15
        ):
            raise WebExplorationBudgetLedgerConflictError("WEB_CLOSEOUT_RESERVED")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            action_policy = self._frozen_policy
            if runtime_discovery:
                from services.api.app.application.web_run_policy import materialize_web_policy
                from services.api.app.infrastructure.db.web_logical_target_repository import (
                    _require_scope,
                )
                from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
                    read_web_policy_on_connection,
                )

                await _require_scope(
                    connection,
                    command.task_id,
                    command.agent_run_id,
                    command.session_id,
                    source_capture_id,
                )
                action_policy = materialize_web_policy(
                    await read_web_policy_on_connection(
                        connection, command.task_id, command.agent_run_id
                    )
                )
            source = await connection.fetchrow(
                _SOURCE_CAPTURE_VALIDATION_SQL,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                source_capture_id,
            )
            if source is None:
                raise WebExplorationBudgetLedgerConflictError(
                    "详情 source Capture 不属于当前 captured Web Session"
                )
            require_budget_policy_match(source, action_policy)
            row = await connection.fetchrow(
                _RESERVE_DETAIL_SQL,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                entry.entry_id,
                source_capture_id,
                candidate_id,
                _sha256_text(destination_url),
            )
        if row is None:
            raise WebExplorationBudgetLedgerConflictError(
                "详情预算不存在、已终态或 reservation 身份发生漂移"
            )
        return WebDetailActionReservation(
            site_id=str(row["site_id"]),
            menu_entry_id=str(row["menu_entry_id"]),
            intent_id=str(row["detail_intent_id"]),
            already_reserved=bool(row["already_reserved"]),
        )

    async def finalize(self, record: WebMenuLedgerTerminalRecord) -> None:
        if not isinstance(record, WebMenuLedgerTerminalRecord):
            raise TypeError("record 必须为 WebMenuLedgerTerminalRecord")
        if record.status in _BOOTSTRAP_ONLY_TERMINAL_STATUSES:
            raise WebExplorationBudgetLedgerConflictError(
                "Web bootstrap-only 终态不得通过 reservation finalize 写入"
            )
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            catalog_identity = None
            if record.menu_page_type == "collection_page":
                from services.api.app.application.web_page_type_assessment import (
                    WebPageTypeCatalogIdentity,
                )
                from services.api.app.infrastructure.db.web_run_policy_snapshot_repository import (
                    read_web_policy_on_connection,
                )

                frozen = await read_web_policy_on_connection(
                    connection, record.task_id, record.agent_run_id,
                )
                if frozen is None:
                    raise WebExplorationBudgetLedgerConflictError("合集终态缺少 Run 冻结目录")
                catalog_identity = WebPageTypeCatalogIdentity.from_frozen_catalog_payload(
                    frozen.to_payload()["page_catalog"],
                )
            row = await connection.fetchrow(
                _FINALIZE_SQL,
                record.task_id,
                record.agent_run_id,
                record.session_id,
                record.site_id,
                record.menu_entry_id,
                record.status.value,
                record.menu_action_execution_id,
                record.menu_probe_capture_id,
                record.menu_ready_capture_id,
                record.menu_page_type,
                record.detail_action_execution_id,
                record.detail_probe_capture_id,
                record.detail_ready_capture_id,
                record.detail_page_type,
                _json_dumps(record.detail_selection),
                _json_dumps(record.diagnostic),
                catalog_identity.catalog_version if catalog_identity else None,
                catalog_identity.catalog_sha256 if catalog_identity else None,
            )
        if row is None:
            raise WebExplorationBudgetLedgerConflictError(
                "Web 菜单终态引用了无效 Session、Capture、Action 或 reservation"
            )

    async def seal(
        self,
        *,
        command: RunWebWorkflowCommand,
        site_id: str,
    ) -> None:
        """仅在完整持久菜单集合均已终态时幂等封账。"""

        _validate_command(command)
        _require_text(site_id, "site_id")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _SEAL_BUDGET_SQL,
                command.task_id,
                command.agent_run_id,
                command.session_id,
                site_id,
            )
        if row is None:
            raise WebExplorationBudgetLedgerConflictError(
                "Web 菜单预算不存在、身份漂移或账本尚未全部终态"
            )

    async def has_budget(self, *, command, site_id):
        """区分尚未初始化与已有预算；写入仍由原有事务 fencing 验证。"""
        _validate_command(command)
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            return bool(await connection.fetchval(
                """SELECT EXISTS(SELECT 1 FROM qc_web_exploration_budget
                   WHERE task_id=$1::uuid AND agent_run_id=$2::uuid
                     AND session_id=$3 AND site_id=$4)""",
                command.task_id, command.agent_run_id, command.session_id, site_id,
            ))

    async def read_summary(
        self,
        *,
        agent_run_id: str,
        site_id: str,
    ) -> WebMenuExplorationSummary:
        _require_text(agent_run_id, "agent_run_id")
        _require_text(site_id, "site_id")
        pool = await self._get_pool()
        async with pool.acquire() as connection:
            budget = await connection.fetchrow(
                """
                SELECT
                    budget.site_id,
                    budget.catalog_version,
                    home_entry.normalization_version,
                    budget.catalog_sha256,
                    budget.menu_limit,
                    COALESCE((
                        SELECT MAX(entry.catalog_position) + 1
                        FROM qc_web_menu_ledger_entry AS entry
                        WHERE entry.agent_run_id = budget.agent_run_id
                          AND entry.site_id = budget.site_id
                          AND entry.source_kind = 'catalog'
                    ), 0) AS menu_total
                FROM qc_web_exploration_budget AS budget
                LEFT JOIN qc_web_menu_ledger_entry AS home_entry
                  ON home_entry.agent_run_id = budget.agent_run_id
                 AND home_entry.site_id = budget.site_id
                 AND home_entry.decision = 'home_baseline'
                WHERE budget.agent_run_id = $1::uuid AND budget.site_id = $2
                """,
                agent_run_id,
                site_id,
            )
            if budget is None:
                raise WebExplorationBudgetLedgerConflictError("Web 预算账本不存在")
            rows = await connection.fetch(
                """
                SELECT entry.*,
                       CASE
                         WHEN entry.status = 'menu_page_captured'
                          AND entry.diagnostic->>'type' = 'WEB_SUBMENU_EXPANDED'
                         THEN EXISTS (
                           SELECT 1
                           FROM qc_web_exploration_budget AS budget
                           JOIN qc_surface_session AS session
                             ON session.task_id = budget.task_id
                            AND session.session_id = budget.session_id
                           JOIN qc_action_execution AS action
                             ON action.action_execution_id = entry.menu_action_execution_id
                            AND action.task_id = budget.task_id
                            AND action.session_id = budget.session_id
                           JOIN qc_capture AS before_capture
                             ON before_capture.id = action.source_capture_id
                           JOIN qc_capture AS after_capture
                             ON after_capture.id = action.result_capture_id
                           WHERE budget.agent_run_id = entry.agent_run_id
                             AND budget.site_id = entry.site_id
                             AND action.intent_id = entry.menu_intent_id
                             AND action.status = 'completed'
                             AND action.finalization_epoch = session.finalization_epoch
                             AND action.requested_source_capture_id = entry.menu_source_capture_id
                             AND action.result_capture_id = entry.menu_ready_capture_id
                             AND entry.menu_probe_capture_id = entry.menu_ready_capture_id
                             AND before_capture.task_id = action.task_id
                             AND before_capture.agent_run_id = entry.agent_run_id
                             AND before_capture.surface_session_id = action.session_id
                             AND before_capture.surface_kind = 'web'
                             AND before_capture.capture_status = 'captured'
                             AND before_capture.capture_role = 'before_action'
                             AND before_capture.action_execution_id = action.action_execution_id
                             AND before_capture.parent_capture_id
                                 = action.requested_source_capture_id
                             AND before_capture.session_command_sequence = action.command_sequence
                             AND after_capture.task_id = action.task_id
                             AND after_capture.agent_run_id = entry.agent_run_id
                             AND after_capture.surface_session_id = action.session_id
                             AND after_capture.surface_kind = 'web'
                             AND after_capture.capture_status = 'captured'
                             AND after_capture.capture_role = 'after_action'
                             AND after_capture.parent_capture_id = before_capture.id
                             AND after_capture.action_execution_id = action.action_execution_id
                             AND after_capture.session_command_sequence = action.command_sequence
                         )
                         ELSE FALSE
                       END AS submenu_relations_verified
                FROM qc_web_menu_ledger_entry AS entry
                WHERE entry.agent_run_id = $1::uuid AND entry.site_id = $2
                ORDER BY entry.catalog_position,
                         (entry.discovery_manifest #>> '{candidate,url}') COLLATE "C" NULLS LAST,
                         entry.menu_entry_id COLLATE "C"
                """,
                agent_run_id,
                site_id,
            )
            if any(row["status"] == "logical_target_reused" for row in rows):
                from services.api.app.domain.exploration.web_run_policy_snapshot import (
                    WebRunPolicyError,
                )
                from services.api.app.infrastructure.db.web_capture_identity import (
                    read_web_capture_identities,
                )

                task_id = await connection.fetchval(
                    "SELECT task_id::text FROM qc_agent_run WHERE id=$1::uuid", agent_run_id
                )
                try:
                    identities = await read_web_capture_identities(
                        connection, task_id=task_id, agent_run_id=agent_run_id
                    )
                except WebRunPolicyError:
                    identities = {}
                rows = [dict(row) for row in rows]
                for row in rows:
                    if row["status"] != "logical_target_reused":
                        continue
                    diagnostic = _mapping_value(row.get("diagnostic"))
                    identity = identities.get(str(diagnostic.get("reused_capture_id")), {})
                    row["reused_material_valid"] = identity.get(
                        "logical_page_identity_status"
                    ) == "verified" and identity.get("logical_page_id") == diagnostic.get(
                        "reused_logical_page_id"
                    )
        return _build_summary(budget, rows)

    async def closeout_unexecuted(self, *, command, site_id):
        """额度真实用尽后，将零动作的暂缓发现持久结算为预算审计。"""
        _validate_command(command)
        _require_text(site_id, "site_id")
        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                """UPDATE qc_web_menu_ledger_entry AS entry
                   SET status='menu_limit_skipped',
                       diagnostic=jsonb_build_object(
                       'error_code','WEB_DISCOVERY_BUDGET_EXHAUSTED'
                   ), row_version=entry.row_version+1, updated_at=now()
                   FROM qc_web_exploration_budget AS budget
                   JOIN qc_task AS task ON task.id=budget.task_id
                   JOIN qc_agent_run AS run ON run.id=budget.agent_run_id
                     AND run.task_id=task.id
                   JOIN qc_surface_session AS session ON session.task_id=task.id
                     AND session.session_id=budget.session_id
                   WHERE budget.task_id=$1::uuid AND budget.agent_run_id=$2::uuid
                     AND budget.session_id=$3 AND budget.site_id=$4
                     AND budget.state='open'
                     AND task.status='running' AND task.finalization_state='open'
                     AND run.status='running'
                     AND COALESCE(run.output_context->>'cancel_requested','false')='false'
                     AND session.surface_kind='web'
                     AND session.session_owner='web-v2-workflow'
                     AND session.status IN ('active','draining')
                     AND session.finalization_epoch=task.finalization_epoch
                     AND budget.menu_limit IS NOT NULL
                     AND entry.agent_run_id=budget.agent_run_id
                     AND entry.site_id=budget.site_id
                     AND entry.source_kind='runtime_discovered'
                     AND entry.decision='runtime_record_only'
                     AND entry.status='runtime_discovered_unreviewed'
                     AND entry.execution_slot IS NULL AND entry.menu_action_count=0
                     AND entry.detail_action_count=0
                     AND COALESCE(entry.diagnostic->>'error_code','') IN (
                         'WEB_DISCOVERY_PENDING','WEB_FRONTIER_ORIGIN_CONCURRENCY_BUSY'
                     )
                     AND (
                         SELECT COUNT(*) FROM qc_web_menu_ledger_entry AS occupied
                         WHERE occupied.agent_run_id=budget.agent_run_id
                           AND occupied.site_id=budget.site_id
                           AND occupied.execution_slot IS NOT NULL
                           AND occupied.menu_action_count=1
                     ) >= budget.menu_limit
                     AND NOT EXISTS (
                         SELECT 1 FROM qc_web_menu_ledger_entry AS active
                         WHERE active.agent_run_id=budget.agent_run_id
                           AND active.site_id=budget.site_id
                           AND active.status IN ('menu_reserved','detail_reserved')
                     )""",
                command.task_id,
                command.agent_run_id,
                command.session_id,
                site_id,
            )

    async def record_discovery(
        self, *, command, site, candidate, source_capture_id, error_code, discovery_rank=None,
    ):
        from services.api.app.infrastructure.db.web_runtime_discovery_repository import (
            record_discovery,
        )

        pool = await self._get_pool()
        async with pool.acquire() as connection, connection.transaction():
            await record_discovery(
                connection,
                command=command,
                site=site,
                candidate=candidate,
                source_capture_id=source_capture_id,
                error_code=error_code,
                discovery_rank=discovery_rank,
            )

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        return await get_postgres_pool(self._database_url)


def _catalog_payload(
    site: WebSiteMenuCatalog,
    *,
    menu_limit: int | None = WEB_MENU_COLLECTION_LIMIT,
    frozen_policy=None,
) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    collection_plan = plan_web_menu_collection(
        site,
        menu_limit=menu_limit,
        discovery_policy=frozen_policy.discovery_policy if frozen_policy is not None else None,
        required_page_types=frozen_policy.required_page_types if frozen_policy is not None else (),
    )
    slot_by_entry_id = {
        entry.entry_id: position
        for position, entry in enumerate(collection_plan.selected_entries, start=1)
    }
    for position, entry in enumerate(site.entries):
        execution_slot = slot_by_entry_id.get(entry.entry_id)
        diagnostic: dict[str, object] = {}
        if execution_slot is not None:
            status = "pending"
        elif entry.decision is WebMenuDecision.HOME_BASELINE:
            status = "home_pending"
        elif entry.decision is WebMenuDecision.BLOCKED_CROSS_ORIGIN:
            status = WebMenuTerminalStatus.CROSS_ORIGIN_BLOCKED.value
            diagnostic = {"error_code": "WEB_MENU_CROSS_ORIGIN_BLOCKED"}
        else:
            status = WebMenuTerminalStatus.MENU_LIMIT_SKIPPED.value
            alias_of = collection_plan.alias_of.get(entry.entry_id)
            if alias_of is not None:
                diagnostic = {
                    "error_code": "WEB_MENU_LOGICAL_ALIAS",
                    "alias_of": alias_of,
                }
            elif entry.status is WebCatalogStatus.DEPRECATED:
                diagnostic = {
                    "error_code": "WEB_MENU_CATALOG_DEPRECATED",
                    "catalog_status": entry.status.value,
                }
            elif not entry.collection_eligibility.eligible:
                diagnostic = {"error_code": "WEB_MENU_COLLECTION_INELIGIBLE"}
            else:
                diagnostic = {"error_code": "WEB_MENU_LIMIT_SKIPPED"}
        payload.append(
            {
                "menu_entry_id": entry.entry_id,
                "catalog_position": position,
                "decision": entry.decision.value,
                "menu_path": list(entry.menu_path),
                "canonical_url_sha256": _sha256_text(entry.canonical_url),
                "execution_slot": execution_slot,
                "status": status,
                "diagnostic": diagnostic,
            }
        )
    return payload


def _same_materialized_catalog(
    expected: list[dict[str, object]],
    rows: list[Mapping[str, object]],
    *,
    allow_closeout: bool = False,
) -> bool:
    if len(expected) != len(rows):
        return False
    actual_by_id = {str(row["menu_entry_id"]): row for row in rows}
    if len(actual_by_id) != len(rows):
        return False
    fields = (
        "menu_entry_id",
        "catalog_position",
        "decision",
        "canonical_url_sha256",
        "execution_slot",
    )
    for wanted in expected:
        actual = actual_by_id.get(str(wanted["menu_entry_id"]))
        if actual is None:
            return False
        if any(actual[field] != wanted[field] for field in fields):
            return False
        if tuple(_json_value(actual["menu_path"])) != tuple(wanted["menu_path"]):
            return False
        closeout_terminal = (
            allow_closeout
            and wanted["status"] == "pending"
            and actual.get("status") == "navigation_failed"
            and actual.get("menu_action_count") == 0
            and actual.get("detail_action_count") == 0
            and _mapping_value(actual.get("diagnostic"))
            == {"error_code": "WEB_MENU_CLOSEOUT_RESERVED"}
        )
        if (
            _mapping_value(actual.get("diagnostic")).get("error_code")
            == "WEB_MENU_CLOSEOUT_RESERVED"
            and not closeout_terminal
        ):
            return False
        if (
            wanted["status"] == "pending"
            and actual.get("status") == "navigation_failed"
            and actual.get("menu_action_count") == 0
            and not closeout_terminal
        ):
            return False
        # 目录身份保持冻结；已执行菜单和首页未分类的诊断是运行事实，不是目录配置。
        runtime_diagnostic = (
            wanted["status"] == "pending"
            and actual.get("menu_action_count") == 1
            and actual.get("status") not in {
                "pending", "home_pending", "menu_reserved", "detail_reserved",
                "home_baseline", "menu_limit_skipped", "cross_origin_blocked",
                "runtime_discovered_unreviewed",
            }
        ) or (
            wanted["status"] == "home_pending"
            and actual.get("status") == "page_type_unclassified"
            and actual.get("menu_action_count") == 0
            and actual.get("detail_action_count") == 0
            and _mapping_value(actual.get("diagnostic")).get("error_code")
                == "WEB_PAGE_TYPE_UNCLASSIFIED"
        )
        if (
            not closeout_terminal
            and not runtime_diagnostic
            and actual.get("status") != "logical_target_reused"
            and _mapping_value(actual.get("diagnostic")) != wanted["diagnostic"]
        ):
            return False
    return True


def require_budget_policy_match(budget: Mapping[str, object], frozen_policy) -> None:
    """当前运行必须使用冻结额度；无冻结策略的历史账本保留原有读取语义。"""
    if frozen_policy is not None and (
        type(budget.get("menu_limit")) is not int
        or budget["menu_limit"] != frozen_policy.menu_limit
    ):
        raise WebExplorationBudgetLedgerConflictError("WEB_BUDGET_POLICY_MISMATCH")


def _same_budget_identity(
    budget: Mapping[str, object],
    *,
    command: RunWebWorkflowCommand,
    site_id: str,
    target_origin_sha256: str,
    catalog_version: str,
    catalog_sha256: str,
) -> bool:
    return (
        str(budget["task_id"]) == command.task_id
        and str(budget["agent_run_id"]) == command.agent_run_id
        and str(budget["session_id"]) == command.session_id
        and str(budget["site_id"]) == site_id
        and budget.get("surface_kind") == "web"
        and budget.get("session_owner") == "web-v2-workflow"
        and budget.get("target_origin_sha256") == target_origin_sha256
        and budget.get("catalog_version") == catalog_version
        and budget.get("catalog_sha256") == catalog_sha256
        and (
            budget.get("menu_limit") is None
            or (type(budget["menu_limit"]) is int and budget["menu_limit"] > 0)
        )
        and budget.get("state") == "open"
    )


def _build_summary(
    budget: Mapping[str, object],
    rows: list[Mapping[str, object]],
) -> WebMenuExplorationSummary:
    ledger: list[WebMenuLedgerEntry] = []
    discovered: list[WebRuntimeDiscoveredLedgerEntry] = []
    timeline: list[str] = []
    page_types: list[str] = []
    status_counts: Counter[str] = Counter()
    frontier_remaining = 0
    for source_row in rows:
        row = dict(source_row)
        row["diagnostic"] = _mapping_value(row.get("diagnostic"))
        row["detail_selection"] = _mapping_value(row.get("detail_selection"))
        persisted_status = str(row["status"])
        if persisted_status in {
            "pending",
            "home_pending",
            "menu_reserved",
            "detail_reserved",
        }:
            status = WebMenuTerminalStatus.NAVIGATION_FAILED
            frontier_remaining += persisted_status == "pending"
            default_error = "WEB_BUDGET_RESERVED_NEEDS_REVIEW"
        else:
            status = WebMenuTerminalStatus(persisted_status)
            default_error = None
        if status in {
            WebMenuTerminalStatus.HOME_BASELINE,
            WebMenuTerminalStatus.MENU_PAGE_CAPTURED,
            WebMenuTerminalStatus.DETAIL_CAPTURED,
        } and not _has_valid_success_evidence(
            row,
            status,
            normalization_version=str(budget.get("normalization_version") or ""),
        ):
            status = WebMenuTerminalStatus.NAVIGATION_FAILED
            default_error = "WEB_BUDGET_TERMINAL_EVIDENCE_INVALID"
        if status is WebMenuTerminalStatus.LOGICAL_TARGET_REUSED and not row.get(
            "reused_material_valid"
        ):
            status = WebMenuTerminalStatus.NAVIGATION_FAILED
            default_error = "WEB_LOGICAL_TARGET_MATERIAL_MISSING"
        status_counts[status.value] += 1
        detail_selection = _mapping_value(row.get("detail_selection"))
        diagnostic = _mapping_value(row.get("diagnostic"))
        menu_ready = _optional_text(row.get("menu_ready_capture_id"))
        detail_ready = _optional_text(row.get("detail_ready_capture_id"))
        menu_page_type = _optional_text(row.get("menu_page_type"))
        detail_page_type = _optional_text(row.get("detail_page_type"))
        if (
            status is WebMenuTerminalStatus.DETAIL_CAPTURED
            and int(row.get("detail_action_count") or 0) == 0
            and menu_page_type in WEB_DETAIL_PAGE_TYPES
        ):
            detail_ready = menu_ready
            detail_page_type = menu_page_type
        for capture_id in (menu_ready, detail_ready):
            if capture_id is not None and capture_id not in timeline:
                timeline.append(capture_id)
        for page_type in (
            menu_page_type,
            detail_page_type,
        ):
            if page_type is not None and page_type not in page_types:
                page_types.append(page_type)
        candidate_id = _optional_text(detail_selection.get("candidate_id"))
        manifest = _mapping_value(row.get("discovery_manifest"))
        manifest_candidate = _mapping_value(manifest.get("candidate"))
        ledger.append(
            WebMenuLedgerEntry(
                entry_id=str(row["menu_entry_id"]),
                menu_path=tuple(str(item) for item in _json_value(row["menu_path"])),
                canonical_url=f"sha256:{row['canonical_url_sha256']}",
                status=status,
                decision=_optional_text(row.get("decision")),
                execution_slot=row.get("execution_slot"),
                source_capture_id=str(
                    row.get("menu_source_capture_id") or row.get("menu_probe_capture_id") or ""
                ),
                final_capture_id=detail_ready or menu_ready,
                page_type=detail_page_type or menu_page_type,
                detail_capture_id=detail_ready,
                error_code=_optional_text(diagnostic.get("error_code")) or default_error,
                alias_of=_optional_text(diagnostic.get("alias_of")),
                reused_capture_id=_optional_text(diagnostic.get("reused_capture_id")),
                reused_logical_page_id=_optional_text(diagnostic.get("reused_logical_page_id")),
                menu_interaction_outcome=_mapping_value(diagnostic.get("menu_interaction_outcome")),
                menu_probe_capture_id=_optional_text(row.get("menu_probe_capture_id")),
                menu_ready_capture_id=menu_ready,
                menu_page_type=menu_page_type,
                detail_candidate_ids=((candidate_id,) if candidate_id else ()),
                selected_detail_candidate_id=candidate_id,
                detail_selection_diagnostic=bounded_detail_selection_diagnostic(
                    diagnostic.get("detail_selection_diagnostic")
                ),
                detail_probe_capture_id=_optional_text(row.get("detail_probe_capture_id")),
                detail_ready_capture_id=detail_ready,
                detail_page_type=detail_page_type,
                diagnostic_type=_optional_text(diagnostic.get("type")),
                diagnostic_message=_optional_text(diagnostic.get("message")),
                menu_action_count=int(row.get("menu_action_count") or 0),
                menu_intent_id=_optional_text(row.get("menu_intent_id")),
                menu_action_execution_id=_optional_text(row.get("menu_action_execution_id")),
                detail_action_count=int(row.get("detail_action_count") or 0),
                detail_intent_id=_optional_text(row.get("detail_intent_id")),
                detail_action_execution_id=_optional_text(row.get("detail_action_execution_id")),
                parent_menu_entry_id=_optional_text(row.get("parent_menu_entry_id")),
                parent_logical_target_identity=_optional_text(
                    manifest_candidate.get("parent_logical_target_identity")
                ),
                menu_depth=(int(row["menu_depth"]) if row.get("menu_depth") is not None else None),
                exploration_path=tuple(
                    str(item) for item in _json_value(row.get("exploration_path") or [])
                ),
                logical_target_identity=_optional_text(row.get("logical_target_identity")),
                submenu_relations_verified=(row.get("submenu_relations_verified") is True),
                ad_landing_evidence=_mapping_value(diagnostic.get("ad_landing_evidence")),
            )
        )
        if row.get("source_kind") == "runtime_discovered":
            execution = ledger.pop()
            candidate = manifest_candidate
            pending_discovery = (
                execution.status is WebMenuTerminalStatus.RUNTIME_DISCOVERED_UNREVIEWED
                and row.get("execution_slot") is None
            )
            discovered.append(
                WebRuntimeDiscoveredLedgerEntry(
                    entry_id=execution.entry_id,
                    menu_path=execution.menu_path,
                    canonical_url=execution.canonical_url,
                    menu_position=int(candidate.get("menu_position") or 0),
                    source_capture_id=str(row["discovery_source_capture_id"]),
                    source_kind="runtime_discovered",
                    candidate_id=_optional_text(candidate.get("entry_id")),
                    candidate_source_kind=_optional_text(candidate.get("source_kind")),
                    decision=str(row["decision"]),
                    execution_slot=row.get("execution_slot"),
                    persisted_status=persisted_status,
                    actionability=str(candidate.get("actionability") or "blocked"),
                    risk_level=str(candidate.get("risk_level") or "unknown"),
                    status=execution.status,
                    error_code=execution.error_code,
                    execution=None if pending_discovery else execution,
                    persisted_ledger_entry=execution,
                    parent_menu_entry_id=execution.parent_menu_entry_id,
                    parent_logical_target_identity=(execution.parent_logical_target_identity),
                    menu_depth=execution.menu_depth,
                    exploration_path=execution.exploration_path,
                    logical_target_identity=execution.logical_target_identity,
                    candidate_manifest=dict(candidate),
                )
            )
    return WebMenuExplorationSummary(
        site_id=str(budget["site_id"]),
        normalization_version=str(budget.get("normalization_version") or ""),
        catalog_version=str(budget["catalog_version"]),
        catalog_sha256=str(budget["catalog_sha256"]),
        menu_total=int(budget["menu_total"]),
        menu_limit=(int(budget["menu_limit"]) if budget["menu_limit"] is not None else None),
        executed_same_origin_count=sum(int(row.get("menu_action_count") or 0) for row in rows),
        menu_limit_skipped_count=sum(
            str(row["status"]) == WebMenuTerminalStatus.MENU_LIMIT_SKIPPED.value for row in rows
        ),
        frontier_remaining=frontier_remaining,
        menu_ledger=tuple(ledger),
        runtime_discovered_ledger=tuple(discovered),
        status_counts=dict(status_counts),
        page_timeline=tuple(timeline),
        page_type_keys=tuple(page_types),
    )


def _has_valid_success_evidence(
    row: Mapping[str, object],
    status: WebMenuTerminalStatus,
    *,
    normalization_version: str = "web-page-type-assessment.v2",
) -> bool:
    menu_action_count = int(row.get("menu_action_count") or 0)
    detail_action_count = int(row.get("detail_action_count") or 0)
    menu_page_type = _optional_text(row.get("menu_page_type"))
    if status is WebMenuTerminalStatus.HOME_BASELINE:
        from services.api.app.application.web_home_baseline_validation import (
            persisted_home_baseline_has_valid_shape,
        )

        return persisted_home_baseline_has_valid_shape(
            row,
            normalization_version=normalization_version,
        )
    has_menu_evidence = all(
        _optional_text(row.get(field_name)) is not None
        for field_name in (
            "menu_action_execution_id",
            "menu_probe_capture_id",
            "menu_ready_capture_id",
        )
    )
    if menu_action_count != 1 or row.get("execution_slot") is None or not has_menu_evidence:
        return False
    if status is WebMenuTerminalStatus.MENU_PAGE_CAPTURED:
        from services.api.app.application.web_submenu_expansion import (
            persisted_menu_page_is_submenu_expansion,
            persisted_ordinary_menu_page_has_valid_shape,
            persisted_submenu_expansion_has_valid_shape,
        )

        if persisted_menu_page_is_submenu_expansion(row):
            return row.get(
                "submenu_relations_verified"
            ) is True and persisted_submenu_expansion_has_valid_shape(row)
        return persisted_ordinary_menu_page_has_valid_shape(row)
    if detail_action_count == 0:
        return (
            menu_page_type in WEB_DETAIL_PAGE_TYPES
            and _optional_text(row.get("detail_action_execution_id")) is None
        )
    return (
        detail_action_count == 1
        and menu_page_type in WEB_LIST_PAGE_TYPES
        and all(
            _optional_text(row.get(field_name)) is not None
            for field_name in (
                "detail_action_execution_id",
                "detail_probe_capture_id",
                "detail_ready_capture_id",
            )
        )
        and _optional_text(row.get("detail_page_type")) in WEB_DETAIL_PAGE_TYPES
    )


def _validate_command(command: RunWebWorkflowCommand) -> None:
    if not isinstance(command, RunWebWorkflowCommand):
        raise TypeError("command 必须为 RunWebWorkflowCommand")


def _validate_entry(entry: WebSiteMenuEntry) -> None:
    if not isinstance(entry, WebSiteMenuEntry):
        raise TypeError("entry 必须为 WebSiteMenuEntry")


def _canonical_origin(value: str) -> str:
    parsed = _require_http_url(value, "site.origin")
    default_port = 443 if parsed.scheme.casefold() == "https" else 80
    netloc = parsed.hostname.casefold()
    if parsed.port not in {None, default_port}:
        netloc = f"{netloc}:{parsed.port}"
    return f"{parsed.scheme.casefold()}://{netloc}"


def _require_http_url(value: object, field_name: str):
    text = _require_text(value, field_name)
    try:
        parsed = urlsplit(text)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须为合法 HTTP(S) URL") from exc
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError(f"{field_name} 必须为无凭据的 HTTP(S) URL")
    return parsed


def _require_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{field_name} 必须为无首尾空白的非空字符串")
    return value


def _require_sha256(value: object, field_name: str) -> str:
    text = _require_text(value, field_name)
    if len(text) != 64 or any(character not in "0123456789abcdefABCDEF" for character in text):
        raise ValueError(f"{field_name} 必须为 64 位十六进制摘要")
    return text.casefold()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_value(value: object) -> object:
    return json.loads(value) if isinstance(value, str) else value


def _mapping_value(value: object) -> Mapping[str, object]:
    decoded = _json_value(value or {})
    return decoded if isinstance(decoded, Mapping) else {}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = ["PostgresWebExplorationBudgetLedgerRepository"]
