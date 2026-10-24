<script setup lang="ts">
import { NModal } from 'naive-ui'
import { finalResultOf, severityChipClass, severityText, type ResultGroupMeta } from './reportDisplay'
import { pageNamesText, type RuleRow } from './ruleGroups'

/**
 * 规则结果汇总里点六类结果瓦片的「!」：列出归到这一类的规则（一条规则只列一次），
 * 每条可以再点「查看详情」打开规则详情弹窗（二级），看这条规则这次用了几次、每一次的情况。
 * 开关由父组件持有，关闭时只收开关、内容留着让关闭动画走完（同规则详情弹窗）。
 */
defineProps<{
  show: boolean
  group: ResultGroupMeta | null
  /** 已按规则列表的顺序排好 */
  rules: RuleRow[]
}>()

const emit = defineEmits<{
  'update:show': [value: boolean]
  'after-leave': []
  'open-rule': [row: RuleRow]
}>()
</script>

<template>
  <n-modal :show="show" @update:show="emit('update:show', $event)" @after-leave="emit('after-leave')">
    <div v-if="group" class="category-dialog" data-testid="category-dialog">
      <header class="category-head">
        <h3>{{ group.label }} · {{ rules.length }} 条规则</h3>
        <button type="button" class="dialog-close" aria-label="关闭" @click="emit('update:show', false)">×</button>
      </header>
      <p v-if="!rules.length" class="category-empty" data-testid="category-empty">这一类没有规则</p>
      <div v-else class="category-body">
        <table class="qc-table category-table">
          <thead>
            <tr>
              <th>规则名称</th>
              <th>所属期</th>
              <th>所属页面</th>
              <th>用了几次</th>
              <th>最终结果</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rules" :key="row.key" :data-testid="`category-rule-${row.rule_code}`">
              <td class="name-cell">
                <span class="severity-chip" :class="severityChipClass(row.lead)">{{ severityText(row.lead) }}</span>
                <strong>{{ row.rule_name }}</strong>
                <span class="qc-mono code-line">{{ row.rule_code }}</span>
              </td>
              <td class="muted-cell">{{ row.lead.ruleset_name || '-' }}</td>
              <td class="muted-cell" :title="row.pageNames.join('、') || undefined">{{ pageNamesText(row.pageNames) }}</td>
              <td class="nowrap-cell">{{ row.executionCount }} 次</td>
              <td>
                <span class="qc-chip" :class="finalResultOf(row.lead).tone">{{ finalResultOf(row.lead).label }}</span>
              </td>
              <td class="is-right">
                <button
                  type="button"
                  class="qc-link"
                  :data-testid="`category-open-${row.rule_code}`"
                  @click="emit('open-rule', row)"
                >
                  查看详情
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </n-modal>
</template>

<style scoped>
.category-dialog {
  width: 94vw;
  max-width: 880px;
  padding: 20px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  color: var(--workstation-text);
  background: var(--workstation-panel-strong);
  box-shadow: var(--workstation-shadow-lg);
}

.category-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.category-head h3 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 15px;
  font-weight: 600;
}

.dialog-close {
  padding: 0 4px;
  border: 0;
  color: var(--workstation-text-faint);
  background: transparent;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}

.dialog-close:hover {
  color: var(--workstation-text-strong);
}

.category-empty {
  margin: 8px 0 4px;
  color: var(--workstation-text-muted);
  font-size: 14px;
}

.category-body {
  max-height: 64vh;
  overflow: auto;
}

.name-cell {
  min-width: 200px;
}

.name-cell strong {
  display: block;
  margin-top: 4px;
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.code-line {
  display: block;
  margin-top: 2px;
  color: var(--workstation-text-muted);
  font-size: 11px;
}

.muted-cell {
  color: var(--workstation-text-muted);
}

.nowrap-cell {
  white-space: nowrap;
}
</style>
