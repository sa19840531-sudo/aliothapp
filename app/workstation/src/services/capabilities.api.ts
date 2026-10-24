import { requestJsonV2 } from './http'
import type { V2TaskExecutionMode } from '@/types/task'

export interface WebBrowserProfileCapability {
  profile_id: string
  viewport_width: number
  viewport_height: number
  screen_width: number
  screen_height: number
  device_scale_factor: number
  is_mobile: boolean
  has_touch: boolean
  color_scheme: string
}

export interface WebV2Capability {
  enabled: boolean
  execution_modes: V2TaskExecutionMode[]
  default_browser_profile_id: string
  browser_profiles: WebBrowserProfileCapability[]
}

export interface PlatformCapability {
  platform: 'h5' | 'web' | 'app'
  enabled: boolean
  configured_mode: string
  execution_modes: V2TaskExecutionMode[]
  unavailable_reasons: string[]
  browser_profiles: Array<Pick<WebBrowserProfileCapability,
    'profile_id' | 'viewport_width' | 'viewport_height' | 'device_scale_factor' | 'is_mobile' | 'has_touch'>>
}

export interface V2CapabilitiesResponse {
  schema_version?: 'v2-capabilities.1'
  platforms?: PlatformCapability[]
  web: WebV2Capability
}

/** 读取服务端真实部署门禁，前端不得根据环境名或平台选项自行推断。 */
export function getV2Capabilities(): Promise<V2CapabilitiesResponse> {
  return requestJsonV2<V2CapabilitiesResponse>('/capabilities')
}
