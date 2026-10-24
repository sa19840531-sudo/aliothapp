import type { AppBuildUploadResponse } from '@/services/app-builds.api'
import type { WorkstationProductOption } from '@/services/products.api'
import type { TaskChannel, V2TaskCreatePayload, V2TaskTarget } from '@/types/task'

/**
 * 快速质检的纯逻辑：产品自动匹配与 V2 任务载荷组装。
 * 与「完整配置任务」页共用同一套合同：规则集与页面范围由用户选，渠道、优先级固定，其余默认值由后端补齐。
 */
export const FIXED_CHANNEL: TaskChannel = 'workbench'
export const FIXED_PRIORITY = 100
/** 没有任何部门信息时的占位；部门账号在服务端会按登录身份改写。 */
export const FALLBACK_DEPARTMENT = '-'
export const DEFAULT_DEVICE_PROFILE = 'qa-b'
export const QUICK_TASK_REMARK = '快速质检入口创建'

export function hostOf(value: string | null | undefined): string {
  if (!value) return ''
  try {
    return new URL(value.trim()).host.toLowerCase()
  } catch {
    return ''
  }
}

/** URL 主域与产品登记的 H5 地址一致即自动匹配；匹配不到时留给用户选。 */
export function matchProductByUrl(
  url: string,
  candidates: WorkstationProductOption[],
): WorkstationProductOption | null {
  const host = hostOf(url)
  if (!host) return null
  return candidates.find((item) => hostOf(item.h5_url) === host) ?? null
}

/** 包名里的业务段（≥3 字符）命中产品名或产品编码即视为匹配。 */
export function matchProductByPackage(
  packageName: string | null | undefined,
  candidates: WorkstationProductOption[],
): WorkstationProductOption | null {
  if (!packageName) return null
  const segments = packageName
    .toLowerCase()
    .split('.')
    .filter((item) => item.length >= 3 && !['com', 'net', 'org', 'app', 'android'].includes(item))
  if (!segments.length) return null
  return (
    candidates.find((item) => {
      const name = item.product_name.toLowerCase()
      const code = item.project_code.toLowerCase()
      return segments.some((segment) => name.includes(segment) || code.includes(segment))
    }) ?? null
  )
}

export interface QuickPayloadInput {
  mode: 'app' | 'url'
  taskName: string
  product: WorkstationProductOption
  departmentName: string
  /** 用户选定的规则集编码（可以多选，一起执行） */
  rulesetCodes: string[]
  /** 页面范围：空数组 = 全部，由系统自动识别；非空 = 本次必须巡检到的页面类型 */
  requiredPageTypes?: string[]
  targetUrl?: string
  urlPlatform?: 'h5' | 'web'
  build?: AppBuildUploadResponse | null
  deviceProfileId?: string | null
}

export function buildQuickPayload(input: QuickPayloadInput): V2TaskCreatePayload {
  const explorationTarget = { required_page_types: [...(input.requiredPageTypes ?? [])] }
  let target: V2TaskTarget
  if (input.mode === 'app') {
    if (!input.build?.platform_build_ref) {
      throw new Error('缺少已解析的安装包')
    }
    // 设备能力模板、里程碑、最低页数都不传：后端按默认值补齐，产品登记了专属画像时套用画像。
    target = {
      platform: 'app',
      source: { type: 'build_ref', platform_build_ref: input.build.platform_build_ref },
      package_name: input.build.package_name ?? null,
      device: {
        backend: 'cloud_phone',
        profile_id: input.deviceProfileId || DEFAULT_DEVICE_PROFILE,
      },
      exploration_target: explorationTarget,
    }
  } else {
    const url = (input.targetUrl ?? '').trim()
    if (!hostOf(url)) {
      throw new Error('缺少完整的页面地址')
    }
    target =
      input.urlPlatform === 'web'
        ? { platform: 'web', url, exploration_target: explorationTarget }
        : { platform: 'h5', url, exploration_target: explorationTarget }
  }
  return {
    task_name: input.taskName.trim(),
    target,
    ruleset_codes: [...input.rulesetCodes],
    channel: FIXED_CHANNEL,
    priority: FIXED_PRIORITY,
    product: {
      department_name: input.product.department_name || input.departmentName || FALLBACK_DEPARTMENT,
      product_code: input.product.project_code,
      product_name: input.product.product_name,
    },
    scan_mode: 'full',
    remark: QUICK_TASK_REMARK,
  }
}

export function defaultQuickTaskName(input: {
  mode: 'app' | 'url' | 'batch'
  productName?: string | null
  build?: AppBuildUploadResponse | null
  targetUrl?: string
  urlPlatform?: 'h5' | 'web'
}): string {
  if (input.mode === 'app') {
    const version = input.build?.version_name ? ` ${input.build.version_name}` : ''
    const base = input.productName || input.build?.package_name || ''
    return base ? `${base}${version} App 质检` : ''
  }
  if (input.mode === 'url') {
    const base = input.productName || hostOf(input.targetUrl ?? '')
    return base ? `${base} ${input.urlPlatform === 'web' ? 'Web' : 'H5'} 质检` : ''
  }
  return ''
}
