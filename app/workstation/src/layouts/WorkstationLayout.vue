<script setup lang="ts">
import { computed, ref } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { NDropdown, NIcon, useMessage } from 'naive-ui'
import {
  ChevronBackOutline,
  ChevronForwardOutline,
  MoonOutline,
  PhonePortraitOutline,
  SunnyOutline,
} from '@vicons/ionicons5'
import { workstationMenuItems, type WorkstationMenuItem } from '@/router/routes'
import { authStore } from '@/stores/auth'
import { THEME_LABELS, themeStore, type WorkstationTheme } from '@/stores/theme'

/**
 * 部门质检平台外壳，对齐 2.1 原型 department-workbench.html：
 * 左侧 256px 深色侧栏（品牌 Q、单字标识菜单、工作空间脚注），
 * 顶栏白底（账号上下文 / 页面标题，切换皮肤、账号类型、头像菜单）。数据范围由账号自动确定。
 * 两套门户严格隔离：这里不提供进入管理后台的入口，只能从登录页切换。
 */
const route = useRoute()
const router = useRouter()
const message = useMessage()

const PORTAL_NAME = '部门质检平台'
const SIDEBAR_STORAGE_KEY = 'alioth-workstation-sidebar-collapsed'

function loadCollapsed(): boolean {
  try {
    return localStorage.getItem(SIDEBAR_STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

const collapsed = ref(loadCollapsed())

function toggleSidebar() {
  collapsed.value = !collapsed.value
  try {
    localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed.value ? '1' : '0')
  } catch {
    // 忽略存储失败
  }
}

const currentUser = computed(() => authStore.user.value)
const displayName = computed(
  () => currentUser.value?.full_name || currentUser.value?.username || '未登录',
)
const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase())
/** 工作空间 = 所属公司 · 所属部门（原型「北京分公司 · 一部」）；旧会话没有公司信息时退到角色。 */
const workspaceName = computed(() => {
  const tenant = currentUser.value?.tenant_name || ''
  const department = currentUser.value?.department_name || ''
  if (tenant && department) return `${tenant} · ${department}`
  return tenant || currentUser.value?.role || ''
})
const ACCOUNT_LABEL = '分公司部门账号'

const userMenuOptions = computed(() => [
  { label: `${displayName.value} · ${workspaceName.value || ACCOUNT_LABEL}`, key: 'profile', disabled: true },
  { type: 'divider', key: 'divider' },
  { label: '退出登录', key: 'logout' },
])

async function handleUserMenuSelect(key: string) {
  if (key === 'logout') {
    authStore.signOut()
    message.success('已退出登录')
    await router.replace({ name: 'login' })
  }
}

const mainItems = computed(() => workstationMenuItems.filter((item) => item.group === 'main'))

const activeMenuKey = computed(() => {
  const name = route.name?.toString()
  if (name === 'taskCreate' || name === 'taskDetail' || name === 'appQcCreate') {
    return 'taskList'
  }
  if (name === 'reportDetail') {
    return 'reportCenter'
  }
  return name || 'dashboard'
})

const isDashboard = computed(() => activeMenuKey.value === 'dashboard')

const currentTitle = computed(() => {
  if (isDashboard.value) return '部门工作台'
  const title = route.meta?.title
  return typeof title === 'string' && title ? title : '工作台'
})

function handleNavClick(item: WorkstationMenuItem) {
  if (item.path !== route.path) {
    void router.push(item.path)
  }
}

/** 换肤：原型是「切换皮肤」按钮下拉两项（默认皮肤 / 暗黑皮肤）。 */
const skinOptions = computed(() =>
  (['light', 'dark'] as WorkstationTheme[]).map((key) => ({
    key,
    label: `${THEME_LABELS[key]}${themeStore.theme.value === key ? ' ✓' : ''}`,
  })),
)

function handleSkinSelect(key: string) {
  if (key === 'light' || key === 'dark') {
    themeStore.setTheme(key)
  }
}

/** App 檢測 Demo（mobile-demo）；可用 VITE_MOBILE_DEMO_URL 覆寫 */
const mobileDemoUrl =
  (import.meta.env.VITE_MOBILE_DEMO_URL as string | undefined)?.trim() ||
  'http://127.0.0.1:8010/api/v1/mobile-demo/demo'
const mobileDemoEnabled =
  String(import.meta.env.VITE_MOBILE_DEMO_ENABLED ?? '').toLowerCase() === 'true'

function openAppDemo() {
  const url = new URL(mobileDemoUrl)
  url.searchParams.set('yuheng', window.location.origin)
  window.open(url.toString(), '_blank', 'noopener,noreferrer')
}
</script>

<template>
  <div class="workstation-shell" :class="{ 'sidebar-collapsed': collapsed }">
    <aside class="workstation-sidebar" aria-label="部门质检平台导航">
      <div class="sidebar-brand">
        <span class="brand-mark">Q</span>
        <template v-if="!collapsed">
          <strong class="brand-name">自动化质检平台</strong>
          <span class="brand-version">2.1</span>
        </template>
      </div>

      <nav class="sidebar-nav">
        <button
          v-for="item in mainItems"
          :key="item.key"
          type="button"
          class="side-item"
          :class="{ 'side-item-active': activeMenuKey === item.key }"
          :title="item.label"
          @click="handleNavClick(item)"
        >
          <span class="side-badge">{{ item.badge }}</span>
          <span v-if="!collapsed" class="side-label">{{ item.label }}</span>
        </button>

        <button
          v-if="mobileDemoEnabled"
          type="button"
          class="side-item side-item-secondary"
          title="App检测"
          @click="openAppDemo"
        >
          <span class="side-badge">检</span>
          <span v-if="!collapsed" class="side-label">App检测</span>
          <n-icon v-if="!collapsed" size="16" class="side-icon"><PhonePortraitOutline /></n-icon>
        </button>
      </nav>

      <div class="sidebar-foot">
        <div v-if="!collapsed && workspaceName" class="workspace-note">
          <strong>{{ workspaceName }}</strong>
          <span>数据范围由账号自动确定</span>
        </div>
        <button
          type="button"
          class="collapse-btn"
          :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
          @click="toggleSidebar"
        >
          <n-icon size="16">
            <ChevronForwardOutline v-if="collapsed" />
            <ChevronBackOutline v-else />
          </n-icon>
        </button>
      </div>
    </aside>

    <div class="workstation-body">
      <header class="workstation-topbar">
        <div class="topbar-crumbs">
          <span class="crumb-portal">{{ workspaceName || PORTAL_NAME }}</span>
          <span class="crumb-sep">/</span>
          <span class="crumb-current">{{ currentTitle }}</span>
        </div>
        <div class="topbar-actions">
          <n-dropdown trigger="click" :options="skinOptions" @select="handleSkinSelect">
            <button
              type="button"
              class="skin-switch"
              title="切换皮肤"
              aria-label="切换皮肤"
              data-testid="theme-toggle"
            >
              <n-icon size="17">
                <MoonOutline v-if="themeStore.isDark.value" />
                <SunnyOutline v-else />
              </n-icon>
            </button>
          </n-dropdown>
          <span class="account-tag">{{ ACCOUNT_LABEL }}</span>
          <n-dropdown trigger="click" :options="userMenuOptions" @select="handleUserMenuSelect">
            <button class="user-block" type="button" aria-label="当前用户菜单" :title="displayName">
              <span class="user-avatar">{{ avatarText }}</span>
            </button>
          </n-dropdown>
        </div>
      </header>

      <main class="workstation-main">
        <div class="content-inner">
          <router-view />
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.workstation-shell {
  display: flex;
  min-height: 100vh;
  background: var(--workstation-bg);
}

/* ---------- 侧栏（原型 aside#sidebar：w-64 bg-slate-900 text-slate-300） ---------- */

.workstation-sidebar {
  position: sticky;
  top: 0;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  width: var(--workstation-sidebar-width);
  height: 100vh;
  border-right: 1px solid var(--workstation-sidebar-border);
  color: var(--workstation-sidebar-text);
  background: var(--workstation-sidebar-bg);
  transition: width 0.18s ease;
}

.sidebar-collapsed .workstation-sidebar {
  width: 72px;
}

.sidebar-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--workstation-header-height);
  padding: 0 16px;
  border-bottom: 1px solid var(--workstation-sidebar-border);
}

.brand-mark {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  color: #ffffff;
  background: var(--workstation-primary);
  font-size: 16px;
  font-weight: 700;
}

.brand-name {
  color: #ffffff;
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
}

.brand-version {
  padding: 1px 7px;
  border-radius: 999px;
  color: #bfdbfe;
  background: rgba(37, 99, 235, 0.35);
  font-size: 11px;
  font-weight: 700;
}

.sidebar-nav {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 4px;
  padding: 12px;
  overflow-y: auto;
}

.side-item {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 10px 12px;
  border: 0;
  border-radius: 8px;
  color: var(--workstation-sidebar-text);
  background: transparent;
  font: inherit;
  font-size: 14px;
  text-align: left;
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}

.sidebar-collapsed .side-item {
  justify-content: center;
  padding: 10px 0;
}

.side-item:hover {
  color: #ffffff;
  background: var(--workstation-sidebar-hover);
}

.side-item-active,
.side-item-active:hover {
  color: var(--workstation-sidebar-active-text);
  background: var(--workstation-sidebar-active);
}

.side-badge {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 20px;
  height: 20px;
  border-radius: 4px;
  color: #ffffff;
  background: var(--workstation-sidebar-badge);
  font-size: 10px;
  font-weight: 500;
}

.side-item-active .side-badge {
  background: rgba(255, 255, 255, 0.2);
}

.side-label {
  flex: 1;
  white-space: nowrap;
}

.side-icon {
  color: var(--workstation-sidebar-muted);
}

.side-item-secondary {
  font-size: 13px;
}

.sidebar-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 16px;
  border-top: 1px solid var(--workstation-sidebar-border);
}

.workspace-note {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  font-size: 12px;
}

.workspace-note strong {
  overflow: hidden;
  color: var(--workstation-sidebar-muted);
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.workspace-note span {
  color: var(--workstation-sidebar-muted);
  opacity: 0.85;
}

.collapse-btn {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 8px;
  color: var(--workstation-sidebar-muted);
  background: transparent;
  cursor: pointer;
}

.collapse-btn:hover {
  color: #ffffff;
  background: var(--workstation-sidebar-hover);
}

/* ---------- 主体 ---------- */

.workstation-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.workstation-topbar {
  position: sticky;
  top: 0;
  z-index: 20;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  height: var(--workstation-header-height);
  padding: 0 24px;
  border-bottom: 1px solid var(--workstation-border);
  background: var(--workstation-topbar-bg);
}

.topbar-crumbs {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.crumb-portal {
  color: var(--workstation-text-muted);
}

.crumb-sep {
  color: var(--workstation-text-faint);
}

.crumb-current {
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.topbar-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}

.skin-switch {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  padding: 0;
  border: 1px solid var(--workstation-border);
  border-radius: 8px;
  color: var(--workstation-text-muted);
  background: var(--workstation-panel);
  cursor: pointer;
}

.skin-switch:hover {
  border-color: var(--workstation-border-strong);
  color: var(--workstation-primary);
  background: var(--workstation-panel-soft);
}

.user-block {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 4px 4px 8px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  cursor: pointer;
}

.user-block:hover {
  background: var(--workstation-panel-soft);
}

.account-tag {
  padding: 3px 10px;
  border-radius: 999px;
  color: var(--workstation-success-strong);
  background: var(--tint-success);
  font-size: 12px;
  white-space: nowrap;
}

.user-avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  color: #ffffff;
  background: #059669;
  font-size: 12px;
  font-weight: 600;
}

.workstation-main {
  flex: 1;
  padding: 20px 24px 32px;
}

.content-inner {
  max-width: var(--workstation-content-width);
  margin: 0 auto;
}

@media (max-width: 960px) {
  .workstation-sidebar {
    position: fixed;
    z-index: 30;
  }

  .workstation-body {
    padding-left: 72px;
  }
}
</style>
