import { requestJson } from './http'
import type { AgentRunStatus } from '@/types/agent-run'

export function getAgentRun(runId: string): Promise<AgentRunStatus> {
  return requestJson<AgentRunStatus>(`/agents/runs/${runId}`)
}
