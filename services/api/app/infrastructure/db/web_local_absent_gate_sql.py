"""最终 Web 序列门禁认证发布时冻结的局部不适用证明，不重算当前规则。"""

WEB_LOCAL_ABSENT_SELECTION_VALID_SQL = """
EXISTS (
    SELECT 1 FROM qc_web_capability_revision revision
    JOIN qc_evidence_manifest local_manifest
      ON local_manifest.manifest_id=revision.assessment_payload->>'manifest_id'
     AND local_manifest.manifest_hash=revision.assessment_payload->>'manifest_hash'
     AND local_manifest.task_id=revision.task_id
     AND local_manifest.capture_id=revision.capture_id
     AND local_manifest.session_id=sequence.session_id
     AND local_manifest.quality_status='pass'
    JOIN qc_evidence_artifact local_artifact
      ON local_artifact.id::text=revision.assessment_payload->>'ui_snapshot_artifact_id'
     AND local_artifact.task_id=revision.task_id
     AND local_artifact.page_capture_id=revision.capture_id
     AND local_artifact.artifact_type='ui_snapshot'
    JOIN qc_ruleset local_ruleset ON local_ruleset.id=publication.ruleset_id
      AND local_ruleset.id=task.ruleset_id AND local_ruleset.version=publication.ruleset_version
      AND local_ruleset.snapshot_hash IS NOT DISTINCT FROM publication.ruleset_snapshot_hash
    CROSS JOIN LATERAL (
        SELECT selected.execution_plan->'selection' AS plan, revision.assessment_payload AS proof
    ) local
    CROSS JOIN LATERAL (
        SELECT CASE
          WHEN jsonb_typeof(local.plan->'identity_json')='string'
           AND octet_length(local.plan->>'identity_json') BETWEEN 2 AND 16384
           AND pg_input_is_valid(local.plan->>'identity_json','jsonb')
          THEN (local.plan->>'identity_json')::jsonb ELSE 'null'::jsonb END AS identity
    ) frozen
    WHERE revision.revision_id::text=local.plan->>'revision_id'
      AND revision.task_id=task.id AND revision.source_agent_run_id=source_run.id
      AND revision.finalization_epoch=task.finalization_epoch
      AND revision.capture_id=sequence.anchor_capture_id
      AND local.proof=sequence.sequence_payload->'capability_revision'
      AND local.proof @> jsonb_build_object(
          'schema_version','web-local-structure.v1','status','local_absent',
          'scope','current_document_light_dom','predicate','native_video_element_present',
          'native_video_count',0,'product_capability_status','unknown',
          'reason_code','WEB_LOCAL_NATIVE_STRUCTURE_LOCAL_ABSENT',
          'revision_id',revision.revision_id::text,'revision',revision.revision,
          'task_id',task.id::text,'source_agent_run_id',source_run.id::text,
          'capture_id',anchor.id::text,'finalization_epoch',task.finalization_epoch)
      AND local.proof->'visible_layer'=jsonb_build_object(
          'topmost_layer_kind',anchor.metadata->'presentation'->'topmost_layer_kind',
          'base_page_visibility',anchor.metadata->'presentation'->'base_page_visibility',
          'blocking_scope',anchor.metadata->'presentation'->'blocking_scope')
      AND local.plan->>'reason_code'='WEB_LOCAL_NATIVE_STRUCTURE_LOCAL_ABSENT'
      AND local.plan->>'scope'='current_document_light_dom'
      AND local.plan->>'predicate'='native_video_element_present'
      AND selected.strategy='llm' AND selected.session_dependency='snapshot'
      AND local.plan->>'rule_contract_hash'=selected.contract_hash
      AND jsonb_typeof(local.plan->'rule_version')='string'
      AND length(btrim(local.plan->>'rule_version'))>0
      AND CASE WHEN jsonb_typeof(local.plan->'declaration')='object' THEN
          local.plan->'declaration' @> '{"schema_version":"web-local-structure-rule.v1",
          "scope":"current_document_light_dom","predicate":"native_video_element_present"}'
      AND local.plan->'declaration'->>'reuse_policy' IN ('per_occurrence','same_material_context')
      AND (local.plan->'declaration')-ARRAY['schema_version','scope','predicate','reuse_policy']
          ='{}'::jsonb ELSE FALSE END
      AND frozen.identity=jsonb_build_object(
          'declaration',local.plan->'declaration','rule_version',local.plan->'rule_version',
          'rule_contract_hash',selected.contract_hash,
          'revision_id',local.proof->'revision_id','revision',local.proof->'revision',
          'material_hash',local.proof->'material_hash','position',local.proof->'position',
          'account_state',local.proof->'account_state','visible_layer',local.proof->'visible_layer',
          'occurrence_id',local.proof->'occurrence_id','capture_id',local.proof->'capture_id',
          'scope',local.proof->'scope','predicate',local.proof->'predicate','status',local.proof->'status')
      AND encode(digest(local.plan->>'identity_json','sha256'),'hex')=local.plan->>'reuse_key'
      AND EXISTS (
          SELECT 1 FROM qc_web_evidence_sequence_member frame
          WHERE frame.sequence_id=sequence.sequence_id AND frame.capture_id=anchor.id
            AND frame.frame_payload->'artifact_ids' ? local_artifact.id::text)
      AND EXISTS (
          SELECT 1 FROM jsonb_array_elements(local_manifest.manifest_payload->'artifacts') artifact
          WHERE artifact->>'artifact_id'=local_artifact.id::text
            AND artifact->>'checksum'=local_artifact.checksum
            AND artifact->>'size_bytes'=local_artifact.size_bytes::text
            AND artifact->>'artifact_type'='ui_snapshot')
)
"""
