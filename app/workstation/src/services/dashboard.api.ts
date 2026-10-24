import { requestJson } from './http'
import type { WorkstationDashboardOverview } from '@/types/dashboard'

export function getDashboardOverview(): Promise<WorkstationDashboardOverview> {
  return requestJson<WorkstationDashboardOverview>('/workstation/dashboard/overview')
}
