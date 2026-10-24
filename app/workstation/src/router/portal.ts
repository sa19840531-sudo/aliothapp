/**
 * 双门户共用一份业务页面：部门质检平台挂在 `/`，体验中心管理后台挂在 `/admin`。
 * 页面里的跳转写部门平台的路径，进入管理后台时自动加 `/admin` 前缀。
 */
export const ADMIN_PREFIX = '/admin'

export function isAdminPath(path: string): boolean {
  return path === ADMIN_PREFIX || path.startsWith(`${ADMIN_PREFIX}/`)
}

export function portalBase(currentPath: string): '' | typeof ADMIN_PREFIX {
  return isAdminPath(currentPath) ? ADMIN_PREFIX : ''
}

/** 把部门平台路径映射到当前门户；已经带 /admin 的路径原样返回。 */
export function withPortal(currentPath: string, target: string): string {
  const base = portalBase(currentPath)
  if (!base || isAdminPath(target)) {
    return target
  }
  if (target === '/' || target === '') {
    return base
  }
  return target.startsWith('/') ? `${base}${target}` : `${base}/${target}`
}

/** 能进管理后台的权限键：有任一项就放行（部门普通账号一项都没有）。 */
const ADMIN_PORTAL_PERMISSIONS = [
  'users',
  'tenants',
  'departments',
  'analytics',
  'system',
  'costReports',
  'auditLogs',
  'systemOperations',
  'taskObjects:read:tenant',
  'taskObjects:read:any',
]

export function canEnterAdminPortal(permissions: readonly string[] | undefined | null): boolean {
  if (!permissions?.length) return false
  return ADMIN_PORTAL_PERMISSIONS.some((key) => permissions.includes(key))
}
