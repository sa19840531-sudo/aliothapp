import { requestJsonV2 } from './http'

export interface RuleEvaluationRunSummary {
  run_id: string
  status: string
  page_count: number
  rule_count: number
  requested_rule_codes: string[]
  error_message: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

export interface RuleEvaluationRunListResponse {
  task_id: string
  runs: RuleEvaluationRunSummary[]
}

export interface RuleEvaluationRunDetail extends RuleEvaluationRunSummary {
  task_id: string
  results: Array<Record<string, unknown>>
}

export interface RuleEvaluationSubmitResponse {
  task_id: string
  rule_evaluation_run_id: string
  status: string
}

/** 提交独立规则测试；不传 ruleCodes 表示测全部命中规则。 */
export function submitRuleEvaluationRun(
  taskId: string,
  ruleCodes?: string[],
): Promise<RuleEvaluationSubmitResponse> {
  return requestJsonV2<RuleEvaluationSubmitResponse>(
    `/tasks/${encodeURIComponent(taskId)}/rule-evaluation-runs`,
    {
      method: 'POST',
      ...(ruleCodes && ruleCodes.length
        ? { body: JSON.stringify({ rule_codes: ruleCodes }) }
        : {}),
    },
  )
}

export function listRuleEvaluationRuns(taskId: string): Promise<RuleEvaluationRunListResponse> {
  return requestJsonV2<RuleEvaluationRunListResponse>(
    `/tasks/${encodeURIComponent(taskId)}/rule-evaluation-runs`,
  )
}

export function getRuleEvaluationRun(
  taskId: string,
  runId: string,
): Promise<RuleEvaluationRunDetail> {
  return requestJsonV2<RuleEvaluationRunDetail>(
    `/tasks/${encodeURIComponent(taskId)}/rule-evaluation-runs/${encodeURIComponent(runId)}`,
  )
}
