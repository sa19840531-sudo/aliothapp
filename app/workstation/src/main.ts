import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { configureHttpClient } from './services/http'
import { authStore } from './stores/auth'
import './styles/tokens.css'
import './styles/workstation.css'

async function redirectToLogin(options: { expired?: boolean } = {}) {
  authStore.signOut()
  const current = router.currentRoute.value
  if (current.name === 'login' || current.name === 'adminLogin') {
    return
  }
  const isAdminPortal = current.path === '/admin' || current.path.startsWith('/admin/')
  await router.replace({
    name: isAdminPortal ? 'adminLogin' : 'login',
    query: {
      redirect: current.fullPath,
      ...(options.expired ? { expired: '1' } : {}),
    },
  })
}

configureHttpClient({
  getToken: () => authStore.session.value?.token ?? null,
  onUnauthorized: async () => {
    if (!authStore.isAuthenticated.value) {
      return
    }
    await redirectToLogin({ expired: true })
  },
})

// token 到期瞬间（含休眠后回前台补检）强制回登录页，不等下一次 401。
authStore.onSessionExpired(() => redirectToLogin({ expired: true }))

createApp(App).use(router).mount('#app')
