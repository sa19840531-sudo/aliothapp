/** 与后端 task_progress 同口径：判定前处置有审计记录，但不代表实际执行。 */
const PREFLIGHT_DISPOSITION_REASONS = new Set([
  'aggregate_candidate_disposition', 'aggregate_rule_scope_not_applicable',
  'aggregate_applicable_evidence_missing', 'aggregate_applicability_unproven',
  'aggregate_rule_scope_conflict', 'aggregate_presentation_evidence_rejected',
  'aggregate_journey_evidence_insufficient', 'aggregate_environment_matrix_insufficient',
])

/** 未执行占位不计实际使用，真实规则判定后的缺证或异常仍计次数。 */
export function countsAsQualityExecution(item: {
  item_type?: string; execution_id: string | null; execution_status?: string | null
  is_bootstrap?: boolean; execution_reason?: string | null; machine_display_status: string
}): boolean {
  return item.item_type !== 'candidate' && !!item.execution_id && !!item.execution_status
    && !item.is_bootstrap && !['not_executed', 'coverage_gap'].includes(item.machine_display_status)
    && !PREFLIGHT_DISPOSITION_REASONS.has((item.execution_reason || '').trim().toLowerCase())
}
