<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'
import { NDropdown, NIcon, useMessage } from 'naive-ui'
import { ChevronForwardOutline, MenuOutline, MoonOutline, SunnyOutline } from '@vicons/ionicons5'
import { adminMenuTree, type AdminMenuChild, type AdminMenuNode } from '@/router/routes'
import { authStore } from '@/stores/auth'
import { THEME_LABELS, themeStore, type WorkstationTheme } from '@/stores/theme'

/**
 * 体验中心管理后台外壳（对齐 2.1 原型 index.html + admin-navigation.js）：
 * 左侧深色树形菜单（一级可展开、二级是页面，侧栏可收成图标栏），顶部只有面包屑、皮肤切换和账号区。
 * 与部门平台同一个应用、同一份登录态；任务/报告/反馈等业务页直接复用，路径前缀 /admin。
 */
const route = useRoute()
const router = useRouter()
const message = useMessage()

const PORTAL_NAME = '自动化质检管理后台'
const SIDEBAR_STORAGE_KEY = 'alioth-admin-sidebar-collapsed'

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
const accountName = computed(() => currentUser.value?.username || displayName.value)
const avatarText = computed(() => displayName.value.slice(0, 1).toUpperCase())
const roleLabel = computed(() => currentUser.value?.role || '管理员')
const workspaceName = computed(() => currentUser.value?.tenant_name || '')
const permissions = computed(() => new Set(currentUser.value?.permissions ?? []))

function allowed(permission?: string) {
  return !permission || permissions.value.has(permission)
}

interface VisibleNode extends AdminMenuNode {
  children?: AdminMenuChild[]
}

/** 按权限过滤：一级没权限整组隐藏；有子项的组过滤后没子项也隐藏。 */
const visibleMenu = computed<VisibleNode[]>(() =>
  adminMenuTree
    .filter((node) => allowed(node.permission))
    .map((node) => ({
      ...node,
      children: node.children?.filter((child) => allowed(child.permission)),
    }))
    .filter((node) => !node.children || node.children.length > 0),
)

function splitPath(path: string): { pathname: string; tab: string | null } {
  const [pathname, search = ''] = path.split('?')
  const tab = new URLSearchParams(search).get('tab')
  return { pathname: pathname || '/', tab }
}

/** 二级项是否对应当前路由：路径相同（或是它的下级页面），带 tab 的还要 tab 一致。 */
function childMatches(child: AdminMenuChild): boolean {
  const { pathname, tab } = splitPath(child.path)
  const samePath = route.path === pathname
  const nested = pathname !== '/admin' && route.path.startsWith(`${pathname}/`)
  if (!samePath && !nested) return false
  const currentTab = route.query.tab?.toString() ?? null
  if (tab) return currentTab === tab
  return true
}

const activeChildKey = computed(() => {
  for (const node of visibleMenu.value) {
    const matches = (node.children ?? []).filter(childMatches)
    if (!matches.length) continue
    // 路径完全相同的入口优先于它的上级列表（「Excel 导入」「创建任务」不该高亮成列表页）
    const samePath = matches.filter((child) => splitPath(child.path).pathname === route.path)
    const candidates = samePath.length ? samePath : matches
    // 同路径多个入口时优先 tab 精确匹配，否则取不带 tab 的那个
    const currentTab = route.query.tab?.toString() ?? null
    const exact = candidates.find((child) => splitPath(child.path).tab === currentTab)
    return (exact ?? candidates[0]).key
  }
  return ''
})

const activeNodeKey = computed(() => {
  for (const node of visibleMenu.value) {
    if (node.children?.some((child) => child.key === activeChildKey.value)) return node.key
    if (node.path && (route.path === node.path || (node.path !== '/admin' && route.path.startsWith(`${node.path}/`)))) {
      return node.key
    }
  }
  return route.path === '/admin' ? 'dashboard' : ''
})

const expanded = ref<Record<string, boolean>>({})

watch(
  activeNodeKey,
  (key) => {
    if (key) expanded.value = { ...expanded.value, [key]: true }
  },
  { immediate: true },
)

function toggleNode(node: VisibleNode) {
  if (collapsed.value) {
    collapsed.value = false
    expanded.value = { ...expanded.value, [node.key]: true }
    return
  }
  expanded.value = { ...expanded.value, [node.key]: !expanded.value[node.key] }
}

function go(path: string) {
  if (route.fullPath !== path) {
    void router.push(path)
  }
}

const currentTitle = computed(() => {
  const title = route.meta?.title
  return typeof title === 'string' && title ? title : '工作台'
})

const crumbGroup = computed(() => {
  const node = visibleMenu.value.find((item) => item.key === activeNodeKey.value)
  if (!node || !node.children) return '运营中心'
  return node.label
})

/** 面包屑第二段对应的菜单页：二级菜单项；没有分组的一级页面（工作台、系统管理）就是它自己。 */
const crumbPage = computed<{ label: string; path: string } | null>(() => {
  for (const node of visibleMenu.value) {
    const child = node.children?.find((item) => item.key === activeChildKey.value)
    if (child) return child
  }
  const node = visibleMenu.value.find((item) => item.key === activeNodeKey.value)
  return node && !node.children && node.path ? { label: node.label, path: node.path } : null
})

/** 在二级页面的下级（任务详情、报告详情等）时补第三段当前页标题，第二段就能点回列表。 */
const crumbDetail = computed(() => {
  const page = crumbPage.value
  if (!page || splitPath(page.path).pathname === route.path) return null
  return currentTitle.value
})

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

const userMenuOptions = computed(() => [
  { label: `${displayName.value} · ${roleLabel.value}`, key: 'profile', disabled: true },
  { type: 'divider', key: 'divider' },
  // 两套门户严格隔离：不再提供进入部门质检平台的入口，只能从登录页切换
  { label: '退出登录', key: 'logout' },
])

async function logout() {
  authStore.signOut()
  message.success('已退出登录')
  await router.replace({ name: 'adminLogin' })
}

async function handleUserMenuSelect(key: string) {
  if (key === 'logout') {
    await logout()
  }
}
</script>

<template>
  <div class="admin-shell" :class="{ 'sidebar-collapsed': collapsed }">
    <aside class="admin-sidebar" aria-label="管理后台导航">
      <div class="sidebar-brand">
        <span class="brand-mark">Q</span>
        <template v-if="!collapsed">
          <strong class="brand-name">自动化质检平台</strong>
          <span class="brand-version">2.1</span>
        </template>
      </div>

      <div v-if="!collapsed" class="portal-block">
        <span>当前门户</span>
        <strong>{{ PORTAL_NAME }}</strong>
      </div>

      <nav class="sidebar-nav" data-testid="admin-nav">
        <div v-for="node in visibleMenu" :key="node.key" class="nav-node">
          <button
            v-if="node.children"
            type="button"
            class="side-item"
            :class="{ 'side-item-active': activeNodeKey === node.key }"
            :title="node.label"
            :data-testid="`admin-nav-${node.key}`"
            @click="toggleNode(node)"
          >
            <span class="side-badge">{{ node.badge }}</span>
            <span v-if="!collapsed" class="side-label">{{ node.label }}</span>
            <n-icon v-if="!collapsed" size="14" class="side-chevron" :class="{ open: expanded[node.key] }">
              <ChevronForwardOutline />
            </n-icon>
          </button>
          <button
            v-else
            type="button"
            class="side-item"
            :class="{ 'side-item-active': activeNodeKey === node.key }"
            :title="node.label"
            :data-testid="`admin-nav-${node.key}`"
            @click="go(node.path || '/admin')"
          >
            <span class="side-badge">{{ node.badge }}</span>
            <span v-if="!collapsed" class="side-label">{{ node.label }}</span>
          </button>
          <div v-if="node.children && !collapsed && expanded[node.key]" class="side-children">
            <button
              v-for="child in node.children"
              :key="child.key"
              type="button"
              class="side-child"
              :class="{ 'side-child-active': activeChildKey === child.key }"
              :data-testid="`admin-nav-${child.key}`"
              @click="go(child.path)"
            >
              {{ child.label }}
            </button>
          </div>
        </div>
      </nav>

      <div class="sidebar-foot">
        <div v-if="!collapsed" class="workspace-note">
          <strong>{{ workspaceName || roleLabel }}</strong>
          <span>跨公司数据按账号权限开放</span>
        </div>
        <button
          type="button"
          class="collapse-btn"
          :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
          data-testid="admin-sidebar-toggle"
          @click="toggleSidebar"
        >
          <n-icon size="16"><MenuOutline /></n-icon>
        </button>
      </div>
    </aside>

    <div class="admin-body">
      <header class="admin-topbar">
        <div class="topbar-crumbs" data-testid="admin-crumbs">
          <span class="crumb-portal">{{ crumbGroup }}</span>
          <span class="crumb-sep">/</span>
          <template v-if="crumbPage">
            <button
              type="button"
              class="crumb-link"
              :class="{ 'is-parent': crumbDetail }"
              data-testid="admin-crumb-page"
              @click="go(crumbPage.path)"
            >{{ crumbPage.label }}</button>
            <template v-if="crumbDetail">
              <span class="crumb-sep">/</span>
              <span class="crumb-current">{{ crumbDetail }}</span>
            </template>
          </template>
          <span v-else class="crumb-current">{{ currentTitle }}</span>
        </div>
        <div class="topbar-actions">
          <n-dropdown trigger="click" :options="skinOptions" @select="handleSkinSelect">
            <button type="button" class="skin-switch" title="切换皮肤" aria-label="切换皮肤" data-testid="theme-toggle">
              <n-icon size="17">
                <MoonOutline v-if="themeStore.isDark.value" />
                <SunnyOutline v-else />
              </n-icon>
            </button>
          </n-dropdown>
          <n-dropdown trigger="click" :options="userMenuOptions" @select="handleUserMenuSelect">
            <button class="user-block" type="button" aria-label="当前用户菜单">
              <span class="user-avatar">{{ avatarText }}</span>
              <span class="user-account">{{ accountName }}</span>
            </button>
          </n-dropdown>
          <button type="button" class="logout-btn" data-testid="admin-logout" @click="logout">退出登录</button>
        </div>
      </header>

      <main class="admin-main">
        <div class="content-inner">
          <router-view />
        </div>
      </main>
    </div>
  </div>
</template>

<style scoped>
.admin-shell {
  display: flex;
  min-height: 100vh;
  background: var(--workstation-bg);
}

.admin-sidebar {
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

.sidebar-collapsed .admin-sidebar {
  width: 64px;
}

.sidebar-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  height: var(--workstation-header-height);
  padding: 0 16px;
  border-bottom: 1px solid var(--workstation-sidebar-border);
}

.sidebar-collapsed .sidebar-brand {
  justify-content: center;
  padding: 0;
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

.portal-block {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--workstation-sidebar-border);
}

.portal-block span {
  color: var(--workstation-sidebar-muted);
  font-size: 11px;
}

.portal-block strong {
  color: #93c5fd;
  font-size: 14px;
  font-weight: 500;
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

.side-chevron {
  transition: transform 0.15s ease;
}

.side-chevron.open {
  transform: rotate(90deg);
}

.side-children {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 2px;
  padding-left: 44px;
}

.side-child {
  padding: 8px 12px;
  border: 0;
  border-radius: 8px;
  color: var(--workstation-sidebar-muted);
  background: transparent;
  font: inherit;
  font-size: 14px;
  text-align: left;
  cursor: pointer;
}

.side-child:hover {
  color: #ffffff;
  background: var(--workstation-sidebar-hover);
}

.side-child-active,
.side-child-active:hover {
  color: #ffffff;
  background: var(--workstation-sidebar-hover);
}

.sidebar-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 16px;
  border-top: 1px solid var(--workstation-sidebar-border);
}

.sidebar-collapsed .sidebar-foot {
  justify-content: center;
  padding: 12px 0;
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

.admin-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
}

.admin-topbar {
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

/* 二级页签：可点击回到该页；在它的下级页面时显示成普通链接色 */
.crumb-link {
  padding: 0;
  border: 0;
  color: var(--workstation-text-strong);
  background: transparent;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
}

.crumb-link.is-parent {
  color: var(--workstation-text-muted);
  font-weight: 400;
}

.crumb-link:hover {
  color: var(--workstation-primary);
  text-decoration: underline;
  text-underline-offset: 3px;
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
  gap: 10px;
  padding: 4px 8px 4px 4px;
  border: 0;
  border-radius: 999px;
  background: transparent;
  cursor: pointer;
}

.user-block:hover {
  background: var(--workstation-panel-soft);
}

.user-account {
  color: var(--workstation-text);
  font-size: 14px;
  white-space: nowrap;
}

.user-avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  border-radius: 50%;
  color: #ffffff;
  background: linear-gradient(135deg, #3b82f6, #4f46e5);
  font-size: 12px;
  font-weight: 600;
}

.logout-btn {
  padding: 6px 10px;
  border: 0;
  border-radius: 8px;
  color: var(--workstation-text-muted);
  background: transparent;
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}

.logout-btn:hover {
  color: var(--workstation-text-strong);
  background: var(--workstation-panel-soft);
}

.admin-main {
  flex: 1;
  padding: 24px;
}

.content-inner {
  max-width: var(--workstation-content-width);
  margin: 0 auto;
}

@media (max-width: 960px) {
  .admin-sidebar {
    position: fixed;
    z-index: 30;
  }

  .admin-body {
    padding-left: 64px;
  }
}
</style>
