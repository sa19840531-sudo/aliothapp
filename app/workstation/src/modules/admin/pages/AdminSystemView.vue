<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { authStore } from '@/stores/auth'

/**
 * 系统管理（对齐 2.1 原型 system.html）：平台级入口卡片。
 * 账号与权限可点进；操作日志和系统参数按原型标「即将上线」，不做假数据。
 */
const router = useRouter()
const permissions = computed(() => new Set(authStore.user.value?.permissions ?? []))

const entries = computed(() => [
  {
    key: 'accounts',
    title: '账号与权限',
    description: '管理账号、角色与数据权限范围',
    path: '/admin/users',
    available: permissions.value.has('users'),
    badge: '账',
  },
  {
    key: 'audit',
    title: '操作日志',
    description: '记录关键操作与变更（即将上线）',
    path: '',
    available: false,
    badge: '志',
  },
  {
    key: 'settings',
    title: '系统参数',
    description: '全局参数与开关配置（即将上线）',
    path: '',
    available: false,
    badge: '参',
  },
])

function open(path: string, available: boolean) {
  if (available && path) {
    void router.push(path)
  }
}
</script>

<template>
  <div class="ap-page" data-testid="admin-system">
    <div class="system-grid">
      <component
        :is="entry.available ? 'button' : 'div'"
        v-for="entry in entries"
        :key="entry.key"
        :type="entry.available ? 'button' : undefined"
        class="workstation-glass-panel system-card"
        :class="{ 'is-available': entry.available, 'is-pending': !entry.available }"
        :data-testid="`system-entry-${entry.key}`"
        @click="open(entry.path, entry.available)"
      >
        <span class="system-icon">{{ entry.badge }}</span>
        <span class="system-text">
          <strong>{{ entry.title }}</strong>
          <small>{{ entry.description }}</small>
        </span>
      </component>
    </div>
  </div>
</template>

<style scoped>
.system-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 20px;
}

.system-card {
  display: flex;
  align-items: center;
  gap: 16px;
  padding: 20px;
  font: inherit;
  text-align: left;
}

.system-card.is-available {
  cursor: pointer;
  transition: border-color 0.18s ease, box-shadow 0.18s ease;
}

.system-card.is-available:hover {
  border-color: var(--workstation-primary);
  box-shadow: var(--workstation-shadow-lg);
}

.system-card.is-available:hover strong {
  color: var(--workstation-primary);
}

.system-card.is-pending {
  opacity: 0.6;
}

.system-icon {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 48px;
  height: 48px;
  border-radius: 12px;
  color: var(--workstation-primary);
  background: var(--tint-primary);
  font-size: 18px;
  font-weight: 700;
}

.is-pending .system-icon {
  color: var(--workstation-text-muted);
  background: var(--tint-neutral);
}

.system-text {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.system-text strong {
  color: var(--workstation-text-strong);
  font-size: 16px;
  font-weight: 600;
}

.system-text small {
  color: var(--workstation-text-muted);
  font-size: 14px;
}
</style>
