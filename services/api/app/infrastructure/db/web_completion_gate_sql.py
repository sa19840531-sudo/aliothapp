"""Web V2 Capture、分类与 Publication 的 Completion Gate SQL。"""

from services.api.app.infrastructure.db.web_classification_reuse import (
    web_classification_reuse_cte,
)

WEB_COMPLETION_GATE_INVENTORY_CTES_SQL = (
    web_classification_reuse_cte("task.finalization_agent_run_id") + ",\n" + """
web_raw_capture_inventory AS (
    SELECT DISTINCT
        capture.id AS after_capture_id,
        task.finalization_agent_run_id AS agent_run_id,
        session.session_id,
        page_capture.page_type,
        COALESCE(
            page_capture.metadata->>'logical_page_id',
            capture.metadata->>'logical_page_id',
            capture.id::text
        ) AS logical_page_id,
        (
            SELECT md5(string_agg(
                fingerprint_artifact.artifact_type || ':' || fingerprint_artifact.checksum,
                '|' ORDER BY fingerprint_artifact.artifact_type,
                             fingerprint_artifact.checksum
            ))
            FROM qc_evidence_artifact AS fingerprint_artifact
            WHERE fingerprint_artifact.task_id = task.id
              AND fingerprint_artifact.page_capture_id = capture.id
              AND fingerprint_artifact.artifact_type IN ('dom', 'visible_text')
              AND fingerprint_artifact.lifecycle_status IN ('active', 'archived')
        ) AS content_fingerprint
    FROM target_task AS task
    INNER JOIN qc_surface_session AS session
        ON session.task_id = task.id
       AND session.session_id = ('web:' || task.finalization_agent_run_id::text)
       AND session.surface_kind = 'web'
       AND session.session_owner = 'web-v2-workflow'
       AND session.finalization_epoch = task.finalization_epoch
    INNER JOIN qc_action_execution AS action
        ON action.task_id = task.id
       AND action.session_id = session.session_id
       AND action.status = 'completed'
       AND action.finalization_epoch = task.finalization_epoch
    INNER JOIN qc_capture AS capture
        ON capture.task_id = task.id
       AND capture.id = action.result_capture_id
       AND capture.agent_run_id = task.finalization_agent_run_id
       AND capture.surface_kind = 'web'
       AND capture.surface_session_id = session.session_id
       AND capture.action_execution_id = action.action_execution_id
       AND capture.capture_status = 'captured'
       AND capture.capture_role = 'after_action'
       AND capture.parent_capture_id = action.source_capture_id
    INNER JOIN qc_capture AS before_capture
        ON before_capture.task_id = task.id
       AND before_capture.id = action.source_capture_id
       AND before_capture.id = capture.parent_capture_id
       AND before_capture.agent_run_id = task.finalization_agent_run_id
       AND before_capture.surface_kind = 'web'
       AND before_capture.surface_session_id = session.session_id
       AND before_capture.action_execution_id = action.action_execution_id
       AND before_capture.capture_status = 'captured'
       AND before_capture.capture_role = 'before_action'
    INNER JOIN qc_page_capture AS page_capture
        ON page_capture.task_id = task.id
       AND page_capture.id = capture.id
    INNER JOIN qc_evidence_artifact AS after_screenshot
        ON after_screenshot.task_id = task.id
       AND after_screenshot.page_capture_id = capture.id
       AND after_screenshot.artifact_type = 'screenshot'
       AND after_screenshot.content_type = 'image/png'
       AND after_screenshot.source IN ('baseline', 'rule_supplement')
       AND after_screenshot.lifecycle_status IN ('active', 'archived')
    INNER JOIN qc_evidence_artifact AS after_dom
        ON after_dom.task_id = task.id
       AND after_dom.page_capture_id = capture.id
       AND after_dom.artifact_type = 'dom'
       AND after_dom.source IN ('baseline', 'rule_supplement')
       AND after_dom.lifecycle_status IN ('active', 'archived')
    INNER JOIN qc_evidence_artifact AS after_text
        ON after_text.task_id = task.id
       AND after_text.page_capture_id = capture.id
       AND after_text.artifact_type = 'visible_text'
       AND after_text.source IN ('baseline', 'rule_supplement')
       AND after_text.lifecycle_status IN ('active', 'archived')
    INNER JOIN qc_evidence_artifact AS before_screenshot
        ON before_screenshot.task_id = task.id
       AND before_screenshot.page_capture_id = before_capture.id
       AND before_screenshot.artifact_type = 'screenshot'
       AND before_screenshot.content_type = 'image/png'
       AND before_screenshot.source IN ('baseline', 'rule_supplement')
       AND before_screenshot.lifecycle_status IN ('active', 'archived')
    INNER JOIN qc_evidence_artifact AS before_dom
        ON before_dom.task_id = task.id
       AND before_dom.page_capture_id = before_capture.id
       AND before_dom.artifact_type = 'dom'
       AND before_dom.source IN ('baseline', 'rule_supplement')
       AND before_dom.lifecycle_status IN ('active', 'archived')
    INNER JOIN qc_evidence_manifest AS after_manifest
        ON after_manifest.task_id = task.id
       AND after_manifest.capture_id = capture.id
       AND after_manifest.quality_status = 'pass'
       AND NULLIF(btrim(after_manifest.manifest_id), '') IS NOT NULL
       AND NULLIF(btrim(after_manifest.manifest_hash), '') IS NOT NULL
    INNER JOIN qc_evidence_manifest AS before_manifest
        ON before_manifest.task_id = task.id
       AND before_manifest.capture_id = before_capture.id
       AND before_manifest.quality_status = 'pass'
       AND NULLIF(btrim(before_manifest.manifest_id), '') IS NOT NULL
       AND NULLIF(btrim(before_manifest.manifest_hash), '') IS NOT NULL
    WHERE task.target_platform = 'web'
      AND task.task_config->>'execution_mode' = 'full_inspection'
      AND task.task_config->>'web_inspection_notification_mode' = 'enforced'
      AND COALESCE(
            page_capture.inspection_lifecycle_status,
            capture.metadata->>'inspection_lifecycle_status',
            ''
          ) NOT IN ('superseded', 'abandoned')
      AND (EXISTS (
          SELECT 1
          FROM qc_web_menu_ledger_entry AS ready_entry
          WHERE ready_entry.agent_run_id = task.finalization_agent_run_id
            AND (
                ready_entry.menu_ready_capture_id = capture.id
                OR ready_entry.detail_ready_capture_id = capture.id
            )
      ) OR EXISTS (
          SELECT 1 FROM web_classification_reuse AS reuse
          WHERE reuse.task_id = task.id
            AND reuse.agent_run_id = task.finalization_agent_run_id
            AND reuse.assessment_capture_id = capture.id
      ))
      AND NOT EXISTS (
          SELECT 1
          FROM qc_evidence_manifest AS newer_after
          WHERE newer_after.capture_id = after_manifest.capture_id
            AND newer_after.manifest_version > after_manifest.manifest_version
      )
      AND NOT EXISTS (
          SELECT 1
          FROM qc_evidence_manifest AS newer_before
          WHERE newer_before.capture_id = before_manifest.capture_id
            AND newer_before.manifest_version > before_manifest.manifest_version
      )
), web_effective_capture_inventory AS (
    SELECT inventory.*
    FROM web_raw_capture_inventory AS inventory
    WHERE NOT EXISTS (
        SELECT 1
        FROM web_classification_reuse AS reuse
        INNER JOIN web_raw_capture_inventory AS original
            ON original.after_capture_id = reuse.assessment_capture_id
           AND original.agent_run_id = reuse.agent_run_id
        WHERE reuse.observed_capture_id = inventory.after_capture_id
          AND reuse.agent_run_id = inventory.agent_run_id
    )
), web_classification_gate AS (
    SELECT count(DISTINCT inventory.logical_page_id) FILTER (
        WHERE classification_assessment.assessment_record_id IS NULL
    )::integer AS missing_assessment_count,
    count(DISTINCT inventory.logical_page_id) FILTER (
        WHERE classification_assessment.detection_status = 'needs_review'
    )::integer AS review_assessment_count
    FROM target_task AS task
    LEFT JOIN web_effective_capture_inventory AS inventory ON TRUE
    LEFT JOIN qc_h5_page_type_assessment AS classification_assessment
        ON classification_assessment.task_id = task.id
       AND classification_assessment.agent_run_id = task.finalization_agent_run_id
       AND classification_assessment.session_id = inventory.session_id
       AND classification_assessment.page_capture_id = inventory.after_capture_id
       AND classification_assessment.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')
), web_publishable_inventory AS (
    SELECT DISTINCT
        inventory.after_capture_id,
        inventory.agent_run_id,
        inventory.page_type,
        inventory.logical_page_id,
        inventory.content_fingerprint,
        assessment.assessment_record_id,
        assessment.payload_hash AS assessment_hash
    FROM target_task AS task
    INNER JOIN web_effective_capture_inventory AS inventory ON TRUE
    INNER JOIN qc_h5_page_type_assessment AS assessment
        ON assessment.task_id = task.id
       AND assessment.agent_run_id = task.finalization_agent_run_id
       AND assessment.session_id = inventory.session_id
       AND assessment.page_capture_id = inventory.after_capture_id
       AND assessment.schema_version IN ('web-page-type-assessment.v1', 'web-page-type-assessment.v2', 'web-page-type-assessment.v3', 'web-page-type-assessment.v4')
       AND assessment.detection_status = 'recognized'
       AND assessment.page_type = inventory.page_type
), web_publication_gate AS (
    SELECT
        count(DISTINCT (
            inventory.logical_page_id,
            inventory.content_fingerprint
        )) FILTER (
            WHERE inventory.after_capture_id IS NOT NULL
              AND publication.id IS NULL
        )::integer AS missing_publication_count,
        count(DISTINCT expected_member.id) FILTER (
            WHERE member.rule_id IS NULL
        )::integer AS missing_member_count
    FROM target_task AS task
    LEFT JOIN web_publishable_inventory AS inventory ON TRUE
    LEFT JOIN qc_h5_inspection_publication AS publication
        ON publication.task_id = task.id
       AND publication.web_evidence_sequence_id IS NULL
       AND publication.source_agent_run_id = task.finalization_agent_run_id
       AND publication.source_agent_run_id = inventory.agent_run_id
       AND (
            (
                publication.page_capture_id = inventory.after_capture_id
                AND publication.page_type_assessment_id = inventory.assessment_record_id
                AND publication.assessment_hash = inventory.assessment_hash
            )
            OR EXISTS (
                SELECT 1
                FROM qc_capture AS published_capture
                INNER JOIN qc_page_capture AS published_page
                  ON published_page.task_id = publication.task_id
                 AND published_page.id = published_capture.id
                WHERE published_capture.id = publication.page_capture_id
                  AND COALESCE(
                        published_page.metadata->>'logical_page_id',
                        published_capture.metadata->>'logical_page_id',
                        published_capture.id::text
                      ) = inventory.logical_page_id
                  AND (
                        SELECT md5(string_agg(
                            published_artifact.artifact_type || ':' || published_artifact.checksum,
                            '|' ORDER BY published_artifact.artifact_type,
                                         published_artifact.checksum
                        ))
                        FROM qc_evidence_artifact AS published_artifact
                        WHERE published_artifact.task_id = publication.task_id
                          AND published_artifact.page_capture_id = publication.page_capture_id
                          AND published_artifact.artifact_type IN ('dom', 'visible_text')
                          AND published_artifact.lifecycle_status IN ('active', 'archived')
                      ) = inventory.content_fingerprint
            )
       )
       AND publication.ruleset_id = task.ruleset_id
       AND publication.finalization_epoch = task.finalization_epoch
       AND publication.mode = 'enforced'
    LEFT JOIN qc_h5_inspection_publication_work_item AS contract_link
        ON contract_link.publication_id = publication.id
    LEFT JOIN qc_inspection_work_item_rule AS expected_member
        ON expected_member.work_item_id = contract_link.work_item_id
    LEFT JOIN qc_h5_inspection_publication_rule AS member
        ON member.publication_id = publication.id
       AND member.rule_id = expected_member.rule_id
       AND member.rule_code = expected_member.rule_code
       AND member.contract_hash = expected_member.contract_hash
)
"""
)

__all__ = ["WEB_COMPLETION_GATE_INVENTORY_CTES_SQL"]
