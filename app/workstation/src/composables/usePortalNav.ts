import { computed } from 'vue'
import { useRoute, useRouter, type LocationQueryRaw } from 'vue-router'
import { isAdminPath, withPortal } from '@/router/portal'

type NavTarget = string | { path: string; query?: LocationQueryRaw }

/**
 * 页面内跳转统一走这里：写部门平台路径，落在管理后台时自动带 /admin 前缀。
 * 单元测试常常只 mock 了 useRouter，拿不到 useRoute 时退化为原路径。
 */
export function usePortalNav() {
  const router = useRouter()
  let currentPath = () => ''
  try {
    const route = useRoute()
    currentPath = () => route?.path ?? ''
  } catch {
    currentPath = () => ''
  }

  const isAdmin = computed(() => isAdminPath(currentPath()))

  function to(target: string): string {
    return withPortal(currentPath(), target)
  }

  function push(target: NavTarget) {
    if (typeof target === 'string') {
      return router.push(to(target))
    }
    return router.push({ ...target, path: to(target.path) })
  }

  function replace(target: NavTarget) {
    if (typeof target === 'string') {
      return router.replace(to(target))
    }
    return router.replace({ ...target, path: to(target.path) })
  }

  return { isAdmin, to, push, replace }
}
