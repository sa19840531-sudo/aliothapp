<script setup lang="ts">
import { useRouter } from 'vue-router'

/** 配置中心顶部页签（2.1 原型 config.html）：只有规则配置与页面识别配置两项。 */
const props = defineProps<{ active: 'rule' | 'page' }>()
const router = useRouter()

const TABS = [
  { key: 'rule', label: '规则配置', path: '/admin/rules' },
  { key: 'page', label: '页面识别配置', path: '/admin/config' },
] as const

function open(tab: (typeof TABS)[number]) {
  if (tab.key !== props.active) void router.push(tab.path)
}
</script>

<template>
  <div class="workstation-glass-panel ap-tabs config-center-tabs" data-testid="config-center-tabs">
    <button
      v-for="tab in TABS"
      :key="tab.key"
      type="button"
      class="ap-tab"
      :class="{ 'is-active': tab.key === active }"
      :data-testid="`config-center-tab-${tab.key}`"
      @click="open(tab)"
    >
      {{ tab.label }}
    </button>
  </div>
</template>

<style scoped>
.config-center-tabs {
  align-self: flex-start;
}
</style>
