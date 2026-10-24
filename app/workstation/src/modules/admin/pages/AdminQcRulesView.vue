<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { authStore } from '@/stores/auth'
import ConfigCenterTabs from '@/modules/config/components/ConfigCenterTabs.vue'
import RuleListPanel from '../components/rules/RuleListPanel.vue'
import RuleTestTab from '../components/rules/RuleTestTab.vue'
import RulesetPanel from '../components/rules/RulesetPanel.vue'

/**
 * 配置中心 · 规则配置（2.1 原型 config.html「规则配置」页签）：
 * 规则明细 / 规则集 / 规则测试（?tab=list|set|test）。平台规则的增删改、启停和测试
 * 只归体验中心管理员（qcRules:write），其他管理账号只读。页签首次打开后常驻，切回来状态还在。
 */
type RulesTab = 'list' | 'set' | 'test'

const route = useRoute()
const router = useRouter()

const canWrite = computed(() => authStore.user.value?.permissions.includes('qcRules:write') ?? false)
const tabs = computed<Array<{ key: RulesTab; label: string }>>(() => [
  { key: 'list', label: '规则明细' },
  { key: 'set', label: '规则集' },
  ...(canWrite.value ? [{ key: 'test' as const, label: '规则测试' }] : []),
])

const activeTab = computed<RulesTab>(() => {
  const tab = route.query.tab
  if (tab === 'set') return 'set'
  if (tab === 'test' && canWrite.value) return 'test'
  return 'list'
})
const visited = ref<Record<RulesTab, boolean>>({
  list: true,
  set: activeTab.value === 'set',
  test: activeTab.value === 'test',
})
watch(activeTab, (tab) => {
  visited.value = { ...visited.value, [tab]: true }
})

function selectRuleset(rulesetId: string) {
  const query = { ...route.query }
  delete query.tab
  if (rulesetId) query.ruleset_id = rulesetId
  else delete query.ruleset_id
  void router.replace({ path: route.path, query })
}

function switchTab(tab: RulesTab) {
  const query = { ...route.query }
  if (tab === 'list') delete query.tab
  else query.tab = tab
  void router.replace({ path: route.path, query })
}
</script>

<template>
  <div class="qc-rules-page">
    <ConfigCenterTabs active="rule" />

    <div class="workstation-glass-panel ap-tabs rules-subtabs" data-testid="qc-rules-tabs">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="ap-tab"
        :class="{ 'is-active': activeTab === tab.key }"
        :data-testid="`qc-rules-tab-${tab.key}`"
        @click="switchTab(tab.key)"
      >
        {{ tab.label }}
      </button>
    </div>

    <RuleListPanel
      v-show="activeTab === 'list'"
      :can-write="canWrite"
      :selected-ruleset-id="typeof route.query.ruleset_id === 'string' ? route.query.ruleset_id : ''"
      @select-ruleset="selectRuleset"
    />
    <RulesetPanel v-if="visited.set" v-show="activeTab === 'set'" :can-write="canWrite" @view-rules="selectRuleset" />
    <RuleTestTab v-if="visited.test && canWrite" v-show="activeTab === 'test'" :active="activeTab === 'test'" />
  </div>
</template>

<style scoped>
.qc-rules-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  width: 100%;
}

.rules-subtabs {
  align-self: flex-start;
}
</style>
