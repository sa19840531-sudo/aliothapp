"""Web 序列正式收口的持久闭包校验，不借用稳定页库存或改变其他端门禁。"""

from services.api.app.infrastructure.db.web_local_absent_gate_sql import (
    WEB_LOCAL_ABSENT_SELECTION_VALID_SQL,
)

# 同消费读取合同核对冻结的精确 Manifest，而不是后来追加的最新版本。
_INPUT_MEMBERS_VALID = """
    jsonb_array_length(inputs.input_set_payload->'members') =
    jsonb_array_length(sequence.sequence_payload->'frames')
    AND (SELECT count(*) FROM qc_evidence_input_set_member m WHERE m.input_set_id=
    inputs.input_set_id)
        = jsonb_array_length(sequence.sequence_payload->'frames')
    AND NOT EXISTS (
        SELECT 1 FROM qc_evidence_input_set_member m
        WHERE m.input_set_id=inputs.input_set_id AND NOT EXISTS (
            SELECT 1 FROM qc_web_evidence_sequence_member frame
            JOIN qc_capture capture ON capture.id=frame.capture_id AND capture.task_id=frame.task_id
            JOIN qc_evidence_manifest manifest ON manifest.task_id=capture.task_id AND
            manifest.capture_id=capture.id
            WHERE frame.sequence_id=sequence.sequence_id AND frame.ordinal=m.ordinal
              AND frame.capture_id=m.capture_id AND frame.task_id=inputs.task_id AND m.task_id=
              inputs.task_id
              AND capture.agent_run_id=inputs.source_agent_run_id AND capture.capture_status=
              'captured'
              AND capture.surface_session_id=sequence.session_id AND capture.capture_role=
              m.capture_role
              AND capture.metadata->>'finalization_epoch'=inputs.finalization_epoch::text
              AND frame.frame_payload=sequence.sequence_payload->'frames'->(m.ordinal-1)
              AND frame.captured_at=capture.captured_at
              AND manifest.manifest_id=m.manifest_id AND manifest.manifest_version=
              m.manifest_version
              AND manifest.manifest_hash=m.manifest_hash AND manifest.quality_status='pass'
              AND manifest.manifest_payload->>'manifest_id'=m.manifest_id
              AND manifest.manifest_payload->>'manifest_hash'=m.manifest_hash
              AND manifest.manifest_payload->>'capture_id'=capture.id::text
              AND manifest.manifest_payload->>'task_id'=inputs.task_id::text
              AND manifest.manifest_payload->>'manifest_version'=m.manifest_version::text
              AND m.artifact_ids=frame.frame_payload->'artifact_ids'
              AND m.artifact_ids=(SELECT jsonb_agg(a->>'artifact_id' ORDER BY ordinal)
                  FROM jsonb_array_elements(manifest.manifest_payload->'artifacts') WITH
                  ORDINALITY AS artifacts(a,ordinal))
              AND inputs.input_set_payload->'members'->(m.ordinal-1) @> jsonb_build_object(
                  'capture_id',m.capture_id::text,'ordinal',m.ordinal,'capture_role',m.capture_role,
                  'manifest_id',m.manifest_id,'manifest_version',m.manifest_version,
                  'manifest_hash',m.manifest_hash,
                  'artifact_ids',m.artifact_ids)
              AND NOT EXISTS (
                  SELECT 1 FROM jsonb_array_elements(manifest.manifest_payload->'artifacts') a
                  WHERE NOT EXISTS (SELECT 1 FROM qc_evidence_artifact artifact
                      WHERE artifact.id::text=a->>'artifact_id' AND artifact.task_id=inputs.task_id
                        AND artifact.page_capture_id=capture.id AND artifact.checksum=a->>'checksum'
                        AND artifact.size_bytes::text=a->>'size_bytes' AND
                        artifact.artifact_type=a->>'artifact_type')
              )
        )
    )
"""

_SELECTED_INPUT_VALID = f"""
    EXISTS (
        SELECT 1 FROM qc_h5_inspection_publication_work_item link
        JOIN qc_inspection_work_item work ON work.id=link.work_item_id
        JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id
        JOIN qc_evidence_input_set inputs ON inputs.input_set_id=member.evidence_input_set_id
        WHERE link.publication_id=publication.id AND member.rule_id=selected.rule_id
          AND member.contract_hash=selected.contract_hash AND member.rule_code=selected.rule_code
          AND inputs.input_set_id=selected.execution_plan->>'evidence_input_set_id'
          AND inputs.rule_id=selected.rule_id AND inputs.rule_contract_hash=selected.contract_hash
          AND inputs.rule_code=selected.rule_code AND inputs.scope='web_sequence'
          AND inputs.task_id=publication.task_id AND inputs.source_agent_run_id=
          publication.source_agent_run_id
          AND inputs.finalization_epoch=publication.finalization_epoch
          AND inputs.ruleset_snapshot_hash IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
          AND inputs.web_evidence_sequence_id=sequence.sequence_id
          AND inputs.eligibility_snapshot_id=sequence.sequence_id::text
          AND inputs.eligibility_snapshot_hash=sequence.sequence_hash
          AND inputs.input_set_payload @> jsonb_build_object(
              'input_set_id',inputs.input_set_id,'input_set_hash',inputs.input_set_hash,
              'task_id',inputs.task_id::text,'source_agent_run_id',inputs.source_agent_run_id::text,
              'rule_id',inputs.rule_id::text,'rule_contract_hash',inputs.rule_contract_hash,
              'finalization_epoch',inputs.finalization_epoch,'scope','web_sequence',
              'eligibility_snapshot_id',sequence.sequence_id::text,'eligibility_snapshot_hash',
              sequence.sequence_hash)
          AND {_INPUT_MEMBERS_VALID}
    )
"""

WEB_SEQUENCE_PUBLICATION_INVENTORY_MATCH_SQL = f"""
EXISTS (
    SELECT 1 FROM qc_web_evidence_sequence sequence
    JOIN qc_agent_run source_run ON source_run.id=sequence.source_agent_run_id AND
    source_run.task_id=sequence.task_id
    JOIN qc_capture anchor ON anchor.id=sequence.anchor_capture_id AND anchor.task_id=
    sequence.task_id
    JOIN qc_surface_session session ON session.session_id=sequence.session_id AND
    session.task_id=sequence.task_id
    JOIN qc_web_run_policy_snapshot policy ON policy.task_id=sequence.task_id AND
    policy.agent_run_id=source_run.id
    WHERE task.target_platform='web' AND task.task_config->>'workflow_version'='v2'
      AND task.task_config->>'execution_mode'='full_inspection'
      AND task.task_config->>'web_inspection_notification_mode'='enforced'
      AND sequence.sequence_id=publication.web_evidence_sequence_id AND sequence.task_id=task.id
      AND sequence.source_agent_run_id=publication.source_agent_run_id
      AND sequence.source_agent_run_id=task.finalization_agent_run_id
      AND sequence.finalization_epoch=task.finalization_epoch AND sequence.finalization_epoch=
      publication.finalization_epoch
      AND sequence.anchor_capture_id=publication.page_capture_id
      AND source_run.agent_type='main' AND source_run.status='done'
      AND COALESCE(source_run.output_context->>'cancel_requested','false')='false'
      AND source_run.id=(SELECT latest.id FROM qc_agent_run latest WHERE latest.task_id=task.id
          AND latest.agent_type='main' ORDER BY latest.created_at DESC,latest.id DESC LIMIT 1)
      AND anchor.agent_run_id=source_run.id AND anchor.surface_session_id=sequence.session_id
      AND anchor.capture_status='captured' AND anchor.capture_role='after_action'
      AND anchor.metadata->>'finalization_epoch'=sequence.finalization_epoch::text
      AND session.session_id='web:'||source_run.id::text AND session.session_owner='web-v2-workflow'
      AND session.surface_kind='web' AND session.finalization_epoch=sequence.finalization_epoch
      AND session.status IN ('active','draining','closing','closed')
      AND sequence.sequence_payload @> jsonb_build_object(
          'task_id',task.id::text,'agent_run_id',source_run.id::text,'session_id',
          session.session_id,
          'finalization_epoch',sequence.finalization_epoch,'target_platform','web',
          'workflow_version','v2',
          'session_owner','web-v2-workflow','purpose',sequence.purpose,'policy_hash',
          policy.payload_sha256)
      AND jsonb_array_length(sequence.sequence_payload->'frames') >= 2
      AND sequence.sequence_payload->'frames'->-1->>'capture_id'=anchor.id::text
      AND (SELECT count(*) FROM qc_web_evidence_sequence_member frame WHERE frame.sequence_id=
      sequence.sequence_id)
          = jsonb_array_length(sequence.sequence_payload->'frames')
      -- 零候选与不适用同样必须保留封存帧身份，不能依赖已选规则的InputSet才核验。
      AND NOT EXISTS (
          SELECT 1 FROM qc_web_evidence_sequence_member frame
          LEFT JOIN qc_capture capture ON capture.id=frame.capture_id
          WHERE frame.sequence_id=sequence.sequence_id AND NOT (
              frame.task_id=sequence.task_id AND capture.task_id=sequence.task_id
              AND capture.agent_run_id=source_run.id AND capture.capture_status='captured'
              AND capture.surface_session_id=sequence.session_id
              AND capture.metadata->>'finalization_epoch'=sequence.finalization_epoch::text
              AND frame.ordinal BETWEEN 1 AND
                  jsonb_array_length(sequence.sequence_payload->'frames')
              AND frame.frame_payload=sequence.sequence_payload->'frames'->(frame.ordinal-1)
              AND frame.captured_at=capture.captured_at
              AND frame.frame_payload->>'capture_id'=capture.id::text
              AND frame.frame_payload->>'task_id'=capture.task_id::text
              AND frame.frame_payload->>'agent_run_id'=capture.agent_run_id::text
              AND frame.frame_payload->>'session_id'=capture.surface_session_id
              AND frame.frame_payload->>'capture_role'=capture.capture_role
              AND frame.frame_payload->>'finalization_epoch'=sequence.finalization_epoch::text
              AND (frame.frame_payload->>'parent_capture_id') IS NOT DISTINCT FROM
                  capture.parent_capture_id::text
              AND (frame.frame_payload->>'action_execution_id') IS NOT DISTINCT FROM
                  capture.action_execution_id
          ) IS TRUE
      )
      AND NOT EXISTS (
          SELECT 1 FROM qc_h5_inspection_publication_rule selected
          WHERE selected.publication_id=publication.id AND NOT (
              selected.execution_plan->>'web_evidence_sequence_id'=sequence.sequence_id::text
              AND CASE selected.execution_plan->'selection'->>'status'
                  WHEN 'admissible' THEN {_SELECTED_INPUT_VALID}
                  WHEN 'not_applicable' THEN
                  (selected.execution_plan->'selection'->>'reason_code' IN (
                      'WEB_RULE_CHANNEL_MISMATCH','WEB_RULE_AGGREGATE_SCOPE_REJECTED',
                      'WEB_RULE_URL_MISMATCH',
                      'WEB_SEQUENCE_ROLE_REJECTED','WEB_RULE_UNCLASSIFIED_REJECTED',
                      'WEB_RULE_PAGE_TYPE_MISMATCH')
                   OR ({WEB_LOCAL_ABSENT_SELECTION_VALID_SQL}))
                      AND selected.execution_plan->>'evidence_input_set_id' IS NULL
                      AND NOT EXISTS (SELECT 1 FROM qc_h5_inspection_publication_work_item link
                          JOIN qc_inspection_work_item_rule member ON member.work_item_id=
                          link.work_item_id
                          WHERE link.publication_id=publication.id AND member.rule_id=
                          selected.rule_id)
                  ELSE FALSE END
          ) IS TRUE
      )
      AND NOT EXISTS (
          SELECT 1 FROM qc_h5_inspection_publication_work_item link
          JOIN qc_inspection_work_item work ON work.id=link.work_item_id
          WHERE link.publication_id=publication.id AND NOT (
              work.task_id=publication.task_id AND work.page_capture_id=sequence.anchor_capture_id
              AND work.producer_agent_run_id=source_run.id AND work.finalization_epoch=
              sequence.finalization_epoch
              AND work.ruleset_id=publication.ruleset_id AND work.ruleset_version=
              publication.ruleset_version
              AND work.ruleset_snapshot_hash IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
              AND work.control_mode='enforced' AND work.session_dependency='snapshot' AND
              work.strategy='llm'
              AND work.status IN ('succeeded','partial','failed','cancelled','superseded')
              AND link.batch_key=work.batch_key
              AND NOT EXISTS (SELECT 1 FROM qc_inspection_work_item_rule member WHERE
              member.work_item_id=work.id
                  AND NOT EXISTS (SELECT 1 FROM qc_h5_inspection_publication_rule selected
                      WHERE selected.publication_id=publication.id AND selected.rule_id=
                      member.rule_id
                        AND selected.contract_hash=member.contract_hash
                        AND selected.execution_plan->'selection'->>'status'='admissible'
                        AND selected.execution_plan->>'evidence_input_set_id'=
                        member.evidence_input_set_id))
          ) IS TRUE
      )
      AND NOT EXISTS (
          SELECT 1 FROM qc_inspection_work_item work
          JOIN qc_inspection_work_item_rule member ON member.work_item_id=work.id
          JOIN qc_evidence_input_set inputs ON inputs.input_set_id=member.evidence_input_set_id
          WHERE inputs.web_evidence_sequence_id=sequence.sequence_id
            AND NOT EXISTS (SELECT 1 FROM qc_h5_inspection_publication_work_item link
                WHERE link.publication_id=publication.id AND link.work_item_id=work.id)
      )
)
"""
