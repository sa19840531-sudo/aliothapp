export interface ActionTraceEntry {
  step: number
  tool_name: string
  arguments: string
  status: string
  duration_seconds: number
  error_message: string | null
  retry_count: number
}

export interface AgentRunStatus {
  run_id: string
  status: string
  submitted_at: string
  started_at: string | null
  finished_at: string | null
  termination_reason: string | null
  final_text: string | null
  step_count: number | null
  error_message: string | null
  error_category: string | null
  usage: Record<string, number | null> | null
  action_trace: ActionTraceEntry[]
}
