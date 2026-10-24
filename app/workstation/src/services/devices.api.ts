import { requestJson } from './http'

export type DeviceOccupancyStatus =
  | 'available'
  | 'occupied'
  | 'recovering'
  | 'quarantined'
  | 'disabled'

export interface WorkstationDeviceItem {
  device_ref: string
  backend: string
  profile_id: string
  enabled: boolean
  health_state: string
  occupied: boolean
  occupancy_status: DeviceOccupancyStatus | string
  lease_task_id: string | null
  lease_session_id: string | null
  lease_state: string | null
  lease_expires_at: string | null
  morelogin_env_id: string
  queued_preferred_count: number
}

export interface WorkstationDeviceListResponse {
  items: WorkstationDeviceItem[]
}

export function listWorkstationDevices(): Promise<WorkstationDeviceListResponse> {
  return requestJson<WorkstationDeviceListResponse>('/workstation/devices')
}

export function deviceOccupancyLabel(item: WorkstationDeviceItem): string {
  switch (item.occupancy_status) {
    case 'available':
      return '空闲'
    case 'occupied':
      return '质检中'
    case 'recovering':
      return '恢复中'
    case 'quarantined':
      return '隔离'
    case 'disabled':
      return '已停用'
    default:
      return item.occupancy_status
  }
}

export function formatDeviceOptionLabel(item: WorkstationDeviceItem): string {
  const parts = [item.profile_id, deviceOccupancyLabel(item)]
  if (item.queued_preferred_count > 0) {
    parts.push(`排队 ${item.queued_preferred_count}`)
  }
  return parts.join(' · ')
}
