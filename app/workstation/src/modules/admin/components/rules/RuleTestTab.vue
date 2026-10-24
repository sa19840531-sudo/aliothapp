<script setup lang="ts">
import { nextTick, ref } from 'vue'
import { useRoute } from 'vue-router'
import RuleTestView from '@/modules/rule-test/pages/RuleTestView.vue'
import type { RuleTestPrefill } from '../../api/ruleTests'
import RuleTestPanel from '../RuleTestPanel.vue'
import RuleTestRecords from '../RuleTestRecords.vue'

/**
 * 规则配置 · 规则测试（2.1 原型：规则测试并入规则配置）。
 * 两种证据来源：人工上传证据（沙箱判定）/ 用已有任务留存的页面证据重测；两边判定逻辑都与正式任务同源。
 * 人工上传证据的每次测试都存进「测试记录」，可以拿记录里的证据回来重测。
 */
const props = defineProps<{ active: boolean }>()
const route = useRoute()

type Mode = 'manual' | 'task' | 'records'
// 从任务详情带 ?task= 过来时默认用任务证据
const mode = ref<Mode>(typeof route.query.task === 'string' && route.query.task ? 'task' : 'manual')
const MODES: Array<{ key: Mode; label: string }> = [
  { key: 'manual', label: '人工上传证据' },
  { key: 'task', label: '用已有任务的证据' },
  { key: 'records', label: '测试记录' },
]
const panel = ref<InstanceType<typeof RuleTestPanel> | null>(null)

async function handleRetest(prefill: RuleTestPrefill) {
  mode.value = 'manual'
  await nextTick()
  await panel.value?.prefill(prefill)
}
</script>

<template>
  <section class="rule-test-tab" data-testid="rule-test-tab">
    <div class="rt-mode" role="radiogroup">
      <button
        v-for="item in MODES"
        :key="item.key"
        type="button"
        :class="{ 'is-active': mode === item.key }"
        :data-testid="`rule-test-mode-${item.key}`"
        @click="mode = item.key"
      >
        {{ item.label }}
      </button>
    </div>
    <RuleTestPanel
      v-show="mode === 'manual'"
      ref="panel"
      :active="props.active && mode === 'manual'"
    />
    <RuleTestView v-if="mode === 'task'" embedded />
    <RuleTestRecords
      v-if="mode === 'records'"
      :active="props.active"
      @retest="handleRetest"
    />
  </section>
</template>

<style scoped>
.rule-test-tab {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.rt-mode {
  display: inline-flex;
  align-self: flex-start;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  overflow: hidden;
}

.rt-mode button {
  padding: 8px 16px;
  border: 0;
  color: var(--workstation-text-muted);
  background: var(--workstation-panel);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}

.rt-mode button + button {
  border-left: 1px solid var(--workstation-border);
}

.rt-mode button.is-active {
  color: #ffffff;
  background: var(--workstation-primary);
}
</style>
