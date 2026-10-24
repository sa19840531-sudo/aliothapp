import type { H5ShortflowPlan, H5ShortflowSnapshot } from '@/services/h5-shortflows.api'

export function shortflowPlan(): H5ShortflowPlan {
  const bindings = ['before_action', 'after_action'].map((role, index) => ({
    role, capture_id: `capture-${index}`, action_execution_id: 'action-1',
    manifest_id: `manifest-${index}`, manifest_version: 1, latest_manifest_version: 1,
    quality_status: 'pass', integrity: 'current', captured_at: `2026-10-19T00:00:0${index}Z`,
    artifacts: [{ artifact_id: `frame-${index}`, artifact_type: 'screenshot',
      checksum: 'sha256:' + 'a'.repeat(64), content_type: 'image/png', size_bytes: 64 }],
  }))
  return {
    execution_plan_id: 'plan-1', agent_run_id: 'run-1', definition_id: 'player',
    version: 2, content_hash: 'b'.repeat(64), scenario_key: 'pause',
    checkpoints: [{ checkpoint_id: 'pause', attempts: [{ attempt_id: 'attempt-1',
      session_id: 'session-1', finalization_epoch: 0, status: 'bound', observation_status: 'observed',
      created_at: '2026-10-19T00:00:00Z', bindings }] }],
    rule_results_status: 'available', rule_results_notice: null,
    rule_results: [{ rule_id: 'rule-1', rule_code: 'pause-rule', rule_name: '暂停反馈',
      rule_contract_hash: 'c'.repeat(64), execution_id: 'execution-1', input_set_id: 'input-1',
      input_hash: 'd'.repeat(64), execution_status: 'fail', result_summary: '暂停后画面仍在变化',
      problem_desc: '连续观测发现未停止播放', review_status: null, review_reason: null,
      finalization_epoch: 0, linkage_status: 'linked', linkage_notice: null,
      checkpoints: [{ checkpoint_id: 'pause', status: 'fail', reason: '暂停后画面仍在变化',
        checks: [{ name: 'media_time_stopped', passed: false }], missing: [],
        evidence_refs: bindings.map((binding) => ({ checkpoint_id: 'pause', attempt_id: 'attempt-1',
          session_id: 'session-1', role: binding.role, capture_id: binding.capture_id,
          manifest_id: binding.manifest_id, manifest_version: 1,
          artifact_ids: binding.artifacts.map((item) => item.artifact_id) })) }] }],
  }
}

export function shortflowSnapshot(plan = shortflowPlan(), taskId = 't1'): H5ShortflowSnapshot {
  return { task_id: taskId, snapshot_hash: 'e'.repeat(64), finalization_epoch: 0,
    finalization_state: 'open', plans: [plan] }
}
