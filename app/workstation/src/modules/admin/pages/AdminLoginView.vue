<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useMessage } from 'naive-ui'
import { canEnterAdminPortal } from '@/router/portal'
import { authStore } from '@/stores/auth'

/**
 * 管理后台登录页，一比一对齐 2.0 原型 admin-login.html：蓝色主色，左侧四个能力点，
 * 右侧登录卡片，卡片里常驻「进入自动化质检平台」切到部门平台（同一应用内跳转）。
 */
const router = useRouter()
const route = useRoute()
const message = useMessage()

onMounted(() => {
  if (route.query.expired === '1') {
    message.warning('登录已过期，请重新登录')
  }
})

const FEATURES = [
  { index: '01', title: '全局运营', text: '汇总任务、报告、队列容量和异常趋势' },
  { index: '02', title: '资源成本', text: '按部门核算 Token 消耗、单价和费用' },
  { index: '03', title: '服务保障', text: '监控供应商并配置预警与故障转移' },
  { index: '04', title: '规则治理', text: '统一维护规则与处理部门反馈' },
]

const loading = ref(false)
const showPassword = ref(false)
const formError = ref('')
const form = reactive({
  username: '',
  password: '',
})

const redirectTarget = computed(() => {
  const redirect = route.query.redirect
  return typeof redirect === 'string' && redirect ? redirect : '/admin'
})

async function handleSubmit() {
  if (!form.username.trim()) {
    formError.value = '请输入账号'
    return
  }
  if (!form.password) {
    formError.value = '请输入密码'
    return
  }
  formError.value = ''
  loading.value = true
  try {
    await authStore.signIn({
      username: form.username.trim(),
      password: form.password,
    })
    // 两套门户严格隔离：部门账号不能登录管理后台，提示去质检平台登录而不是悄悄跳走
    if (!canEnterAdminPortal(authStore.user.value?.permissions)) {
      authStore.signOut()
      formError.value = '该账号是部门账号，请从自动化质检平台登录'
      return
    }
    message.success('登录成功')
    await router.replace(redirectTarget.value)
  } catch (error) {
    formError.value = error instanceof Error ? error.message : '登录失败'
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="intro" aria-label="产品介绍">
      <div class="intro-inner">
        <div class="brand"><span class="brand-mark">Q</span><span>自动化质检管理后台</span></div>
        <div class="eyebrow">Automated Quality Inspection Admin</div>
        <h1>自动化质检<br />管理后台</h1>
        <p class="intro-copy">
          面向体验中心管理人员，集中管理组织、规则、任务运行、部门 Token 成本、供应商健康与故障转移。
        </p>
        <div class="feature-grid">
          <article v-for="feature in FEATURES" :key="feature.index" class="feature">
            <div class="feature-index">{{ feature.index }}</div>
            <div class="feature-title">{{ feature.title }}</div>
            <div class="feature-text">{{ feature.text }}</div>
          </article>
        </div>
      </div>
    </section>

    <section class="login-side" aria-label="登录区域">
      <form class="login-card" novalidate @submit.prevent="handleSubmit">
        <h2>欢迎登录</h2>
        <p class="login-subtitle">登录自动化质检管理后台</p>

        <div class="field">
          <label for="admin-login-username">账号</label>
          <div class="input-wrap">
            <svg class="input-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
              <path d="M20 21a8 8 0 0 0-16 0" stroke-linecap="round" />
              <circle cx="12" cy="8" r="4" />
            </svg>
            <input
              id="admin-login-username"
              v-model="form.username"
              name="username"
              type="text"
              autocomplete="username"
              placeholder="请输入账号"
            />
          </div>
        </div>

        <div class="field">
          <label for="admin-login-password">密码</label>
          <div class="input-wrap">
            <svg class="input-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
              <rect x="4" y="10" width="16" height="11" rx="2" />
              <path d="M8 10V7a4 4 0 0 1 8 0v3" stroke-linecap="round" />
            </svg>
            <input
              id="admin-login-password"
              v-model="form.password"
              name="password"
              :type="showPassword ? 'text' : 'password'"
              autocomplete="current-password"
              placeholder="请输入密码"
            />
            <button
              type="button"
              class="password-toggle"
              :aria-label="showPassword ? '隐藏密码' : '显示密码'"
              @click="showPassword = !showPassword"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
                <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z" />
                <circle cx="12" cy="12" r="3" />
                <path v-if="showPassword" d="M4 4l16 16" stroke-linecap="round" />
              </svg>
            </button>
          </div>
        </div>

        <div class="form-error" role="alert" aria-live="polite">{{ formError }}</div>
        <button class="login-button" type="submit" :disabled="loading">
          {{ loading ? '登录中…' : '登录' }}
        </button>
        <router-link class="portal-link" to="/login" data-testid="workstation-portal-link">进入自动化质检平台</router-link>
        <p class="reset-note">重置密码请联系运营中心-体验部</p>
        <p class="demo-note">平台账号由运营中心统一分配</p>
      </form>
    </section>
  </main>
</template>

<style scoped>
/* 原型 admin-login.html 的调色板：蓝色主色，浅蓝介绍面。 */
.login-page {
  --page: #f5f7fb;
  --ink: #182338;
  --muted: #6f7b90;
  --line: #dce3ee;
  --primary: #2563eb;
  --primary-dark: #1d4ed8;
  --primary-soft: #eff6ff;
  --primary-line: #bfdbfe;
  --panel: rgba(255, 255, 255, 0.92);
  display: grid;
  grid-template-columns: minmax(0, 1.08fr) minmax(420px, 0.92fr);
  min-height: 100vh;
  overflow: hidden;
  color: var(--ink);
  background: var(--page);
  font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
  letter-spacing: 0;
}

.intro {
  position: relative;
  display: flex;
  align-items: center;
  padding: clamp(42px, 7vw, 96px);
  overflow: hidden;
  border-right: 1px solid #dce5f3;
  background: #edf3fc;
}

.intro-inner {
  position: relative;
  z-index: 1;
  width: min(600px, 100%);
}

.brand {
  display: inline-flex;
  align-items: center;
  gap: 11px;
  color: var(--ink);
  font-size: 16px;
  font-weight: 700;
}

.brand-mark {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 11px;
  color: #ffffff;
  background: var(--primary);
  font-size: 19px;
  font-weight: 800;
  box-shadow: 0 9px 20px rgba(55, 104, 217, 0.24);
}

.eyebrow {
  margin-top: 58px;
  color: var(--primary);
  font-size: 12px;
  font-weight: 750;
  letter-spacing: 1.5px;
  text-transform: uppercase;
}

h1 {
  max-width: 610px;
  margin: 10px 0 14px;
  font-size: clamp(34px, 4.2vw, 58px);
  line-height: 1.16;
  letter-spacing: 0;
}

.intro-copy {
  max-width: 560px;
  margin: 0;
  color: var(--muted);
  font-size: 15px;
  line-height: 1.85;
}

.feature-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  max-width: 620px;
  margin-top: 36px;
}

.feature {
  min-height: 98px;
  padding: 17px 18px;
  border: 1px solid rgba(190, 203, 226, 0.78);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.68);
  box-shadow: 0 7px 20px rgba(39, 58, 91, 0.05);
}

.feature-index {
  color: var(--primary);
  font-size: 11px;
  font-weight: 800;
}

.feature-title {
  margin-top: 7px;
  font-size: 14px;
  font-weight: 700;
}

.feature-text {
  margin-top: 5px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}

.login-side {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 42px clamp(24px, 6vw, 92px);
  background: rgba(255, 255, 255, 0.7);
}

.login-card {
  width: min(420px, 100%);
  padding: 38px 36px 32px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
  box-shadow: 0 20px 55px rgba(44, 65, 101, 0.1);
}

.login-card h2 {
  margin: 0;
  font-size: 27px;
  line-height: 1.3;
}

.login-subtitle {
  margin: 8px 0 29px;
  color: var(--muted);
  font-size: 13px;
}

.field {
  margin-top: 17px;
}

.field:first-of-type {
  margin-top: 0;
}

.field label {
  display: block;
  margin-bottom: 8px;
  color: #3d4a60;
  font-size: 12px;
  font-weight: 650;
}

.input-wrap {
  position: relative;
}

.input-icon {
  position: absolute;
  top: 50%;
  left: 14px;
  width: 17px;
  height: 17px;
  color: #98a4b6;
  transform: translateY(-50%);
  pointer-events: none;
}

.field input {
  width: 100%;
  height: 46px;
  padding: 0 42px 0 42px;
  border: 1px solid var(--line);
  border-radius: 8px;
  outline: 0;
  color: var(--ink);
  background: #fbfcfe;
  font: inherit;
  font-size: 14px;
  transition: border-color 0.18s, box-shadow 0.18s, background 0.18s;
}

.field input::placeholder {
  color: #a4afbf;
}

.field input:focus {
  border-color: #8eaae8;
  background: #ffffff;
  box-shadow: 0 0 0 3px rgba(55, 104, 217, 0.12);
}

.password-toggle {
  position: absolute;
  top: 50%;
  right: 12px;
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border: 0;
  color: #98a4b6;
  background: transparent;
  transform: translateY(-50%);
  cursor: pointer;
}

.password-toggle:hover {
  color: var(--primary);
}

.password-toggle svg {
  width: 16px;
  height: 16px;
}

.form-error {
  min-height: 18px;
  margin-top: 12px;
  color: #c34848;
  font-size: 12px;
}

.login-button {
  width: 100%;
  height: 46px;
  margin-top: 26px;
  border: 0;
  border-radius: 8px;
  color: #ffffff;
  background: var(--primary);
  font: inherit;
  font-size: 14px;
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 8px 16px rgba(55, 104, 217, 0.2);
}

.login-button:hover:not(:disabled) {
  background: var(--primary-dark);
  box-shadow: 0 10px 20px rgba(55, 104, 217, 0.27);
}

.login-button:active {
  transform: translateY(1px);
}

.login-button:disabled {
  cursor: wait;
  opacity: 0.7;
}

.portal-link {
  display: block;
  margin-top: 18px;
  padding: 11px 14px;
  border: 1px solid var(--primary-line);
  border-radius: 8px;
  color: var(--primary-dark);
  background: var(--primary-soft);
  font-size: 13px;
  font-weight: 700;
  text-align: center;
  text-decoration: none;
}

.reset-note {
  margin: 25px 0 0;
  color: #8792a4;
  font-size: 12px;
  line-height: 1.6;
  text-align: center;
}

.demo-note {
  margin: 15px 0 0;
  color: #a2adbc;
  font-size: 11px;
  text-align: center;
}

@media (max-width: 900px) {
  .login-page {
    grid-template-columns: 1fr;
  }

  .intro {
    min-height: 430px;
    padding: 40px 28px;
  }

  .eyebrow {
    margin-top: 38px;
  }

  .login-side {
    padding: 34px 24px 54px;
  }
}

@media (max-width: 520px) {
  .intro {
    min-height: 390px;
  }

  h1 {
    font-size: 34px;
  }

  .intro-copy {
    font-size: 13px;
  }

  .feature-grid {
    grid-template-columns: 1fr;
    margin-top: 25px;
  }

  .feature {
    min-height: 0;
  }

  .login-card {
    padding: 30px 22px 26px;
  }
}
</style>
