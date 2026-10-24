import { createRouter, createWebHistory } from 'vue-router'
import { routes } from './routes'
import { authStore } from '@/stores/auth'
import { canEnterAdminPortal } from './portal'

const router = createRouter({
  history: createWebHistory(),
  routes,
})

const LOGIN_ROUTES = new Set(['login', 'adminLogin'])

/**
 * 两套门户严格隔离（2.1 原型）：管理类账号只用管理后台，部门账号只用部门质检平台，
 * 页面里不再互跳，只有登录页之间能切换入口。
 */
function isAdminPortal(path: string) {
  return path === '/admin' || path.startsWith('/admin/')
}

/** 管理类账号打开部门平台地址时，换到管理后台的同名页面；没有同名页面就回管理后台首页。 */
function adminEquivalent(fullPath: string) {
  const candidate = fullPath === '/' ? '/admin' : `/admin${fullPath}`
  const resolved = router.resolve(candidate)
  return resolved.matched.length > 0 ? candidate : '/admin'
}

function loginRouteFor(path: string) {
  return isAdminPortal(path) ? 'adminLogin' : 'login'
}

router.beforeEach(async (to) => {
  await authStore.bootstrap()

  // 每次导航都做本地过期检查：workstation 业务接口大多未挂鉴权，
  // 不能指望 401 兜底，过期 token 在路由层直接拦回登录页。
  if (authStore.isAuthenticated.value && authStore.isSessionExpired()) {
    authStore.signOut()
    return { name: loginRouteFor(to.path), query: { redirect: to.fullPath, expired: '1' } }
  }

  const isLoginRoute = LOGIN_ROUTES.has(String(to.name))
  if (!isLoginRoute && !authStore.isAuthenticated.value) {
    return { name: loginRouteFor(to.path), query: { redirect: to.fullPath } }
  }

  if (isLoginRoute && authStore.isAuthenticated.value) {
    return { path: canEnterAdminPortal(authStore.user.value?.permissions) ? '/admin' : '/' }
  }

  // 管理类账号不进部门质检平台：拼部门平台地址时换到管理后台的同名页面
  if (
    !isLoginRoute &&
    !isAdminPortal(to.path) &&
    canEnterAdminPortal(authStore.user.value?.permissions)
  ) {
    return adminEquivalent(to.fullPath)
  }

  // 管理后台只对管理类账号开放：部门普通账号即使拼地址也退回部门平台（登录页本身不在此列）。
  if (!isLoginRoute && isAdminPortal(to.path)) {
    const permissions = authStore.user.value?.permissions ?? []
    if (!canEnterAdminPortal(permissions)) {
      return { path: '/' }
    }
    // 管理后台页面按 /auth/me 的权限键放行；没权限退回管理后台首页。
    const permission = to.meta.permission as string | undefined
    if (permission && !permissions.includes(permission)) {
      return to.path !== '/admin' ? { path: '/admin' } : { path: '/' }
    }
  }

  return true
})

export default router
