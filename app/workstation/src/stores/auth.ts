import { computed, ref } from 'vue'
import { fetchCurrentUser, login, type AuthSession, type AuthUser, type LoginPayload } from '@/services/auth.api'

const STORAGE_KEY = 'alioth-workstation-session'

function loadSession(): AuthSession | null {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) {
    return null
  }
  try {
    return JSON.parse(raw) as AuthSession
  } catch {
    localStorage.removeItem(STORAGE_KEY)
    return null
  }
}

function persistSession(session: AuthSession | null) {
  if (session) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(session))
    return
  }
  localStorage.removeItem(STORAGE_KEY)
}

const session = ref<AuthSession | null>(loadSession())
const bootstrapped = ref(false)

// 到期定时器 + 回前台检查：token 一到期立即触发登出跳转，而不是等下一次 401。
// 回调由 main.ts 注入，避免 store 反向依赖 router 造成循环引用。
let expiryTimer: number | undefined
let expiredHandler: (() => void | Promise<void>) | null = null
let visibilityListenerInstalled = false

function sessionExpiresAtMs(): number | null {
  const raw = session.value?.expires_at
  if (!raw) {
    return null
  }
  const timestamp = new Date(raw).getTime()
  return Number.isNaN(timestamp) ? null : timestamp
}

function isSessionExpired(): boolean {
  const expiresAt = sessionExpiresAtMs()
  return expiresAt !== null && expiresAt <= Date.now()
}

function fireExpired() {
  if (!session.value) {
    return
  }
  void expiredHandler?.()
}

function scheduleExpiryWatch() {
  window.clearTimeout(expiryTimer)
  const expiresAt = sessionExpiresAtMs()
  if (expiresAt === null || !session.value) {
    return
  }
  const delay = expiresAt - Date.now()
  if (delay <= 0) {
    fireExpired()
    return
  }
  expiryTimer = window.setTimeout(() => {
    if (isSessionExpired()) {
      fireExpired()
    }
  }, delay)
  if (!visibilityListenerInstalled) {
    // 休眠/后台时 setTimeout 可能被推迟，回到前台立即补一次检查。
    visibilityListenerInstalled = true
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible' && isSessionExpired()) {
        fireExpired()
      }
    })
  }
}

export const authStore = {
  session,
  isAuthenticated: computed(() => Boolean(session.value?.token)),
  user: computed<AuthUser | null>(() => session.value?.user ?? null),
  isSessionExpired,
  onSessionExpired(handler: () => void | Promise<void>) {
    expiredHandler = handler
    scheduleExpiryWatch()
  },
  async bootstrap() {
    if (bootstrapped.value) {
      return
    }
    bootstrapped.value = true

    if (!session.value?.token) {
      return
    }
    if (isSessionExpired()) {
      session.value = null
      persistSession(null)
      return
    }

    try {
      const currentUser = await fetchCurrentUser()
      session.value = {
        token: session.value.token,
        user: currentUser,
        expires_at: session.value.expires_at ?? null,
      }
      persistSession(session.value)
      scheduleExpiryWatch()
    } catch {
      session.value = null
      persistSession(null)
    }
  },
  async signIn(payload: LoginPayload) {
    const result = await login(payload)
    session.value = result
    persistSession(result)
    scheduleExpiryWatch()
    return result
  },
  signOut() {
    window.clearTimeout(expiryTimer)
    session.value = null
    persistSession(null)
  },
}
