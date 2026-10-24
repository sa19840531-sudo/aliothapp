<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { getH5ShortflowCatalog, type H5ShortflowCatalog, type H5ShortflowSelection } from '@/services/h5-shortflows.api'

const props = defineProps<{ modelValue: H5ShortflowSelection[]; rulesetCodes: string[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: H5ShortflowSelection[]] }>()
const catalog = ref<H5ShortflowCatalog | null>(null)
const error = ref('')
const definitionKey = ref('')
const scenario = ref('')
const ruleIds = ref<string[]>([])
let disposed = false
const rules = computed(() => catalog.value?.rules.filter((rule) => props.rulesetCodes.includes(rule.ruleset_code)) ?? [])
const definition = computed(() => catalog.value?.definitions.find((item) => `${item.definition_id}:${item.version}` === definitionKey.value))
const canAdd = computed(() => definition.value && scenario.value.trim() && scenario.value.trim().length <= 128
  && ruleIds.value.length > 0 && ruleIds.value.length <= 100 && props.modelValue.length < 10
  && !props.modelValue.some((item) => item.definition_id === definition.value?.definition_id && item.scenario_key === scenario.value.trim())
  && !props.modelValue.some((item) => item.rule_ids.some((id) => ruleIds.value.includes(id))))

onMounted(async () => {
  try {
    const result = await getH5ShortflowCatalog()
    if (!disposed) catalog.value = result
  } catch (cause) {
    if (!disposed) error.value = cause instanceof Error ? cause.message : '短流程目录加载失败'
  }
})
onBeforeUnmount(() => { disposed = true })
watch(() => [catalog.value, props.rulesetCodes], () => {
  if (!catalog.value) return
  const available = new Set(rules.value.map((item) => item.rule_id))
  ruleIds.value = ruleIds.value.filter((id) => available.has(id))
  const valid = props.modelValue.map((item) => ({ ...item, rule_ids: item.rule_ids.filter((id) => available.has(id)) }))
    .filter((item) => item.rule_ids.length && catalog.value?.definitions.some((candidate) =>
      candidate.definition_id === item.definition_id && candidate.version === item.version && candidate.content_hash === item.content_hash))
  if (JSON.stringify(valid) !== JSON.stringify(props.modelValue)) emit('update:modelValue', valid)
}, { deep: true })

function add() {
  if (!canAdd.value || !definition.value) return
  const item = definition.value
  emit('update:modelValue', [...props.modelValue, {
    definition_id: item.definition_id, version: item.version, content_hash: item.content_hash,
    scenario_key: scenario.value.trim(), rule_ids: [...ruleIds.value], workflow_skill: item.workflow_skill,
  }])
  ruleIds.value = []
  scenario.value = ''
}
</script>

<template>
  <section class="shortflow-select" data-testid="h5-shortflow-select">
    <h3>H5 短流程（可选）</h3>
    <p class="qc-muted">只选择已发布的只读合同和已启用流程规则，不改变原任务预算与 Gate。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-else-if="!catalog">目录加载中…</p>
    <p v-else-if="!catalog.definitions.length || !rules.length">当前没有已发布合同或所选规则集没有已启用的 H5 流程规则。不会自动启用示例模板。</p>
    <template v-else>
      <label class="field">已发布流程
        <SearchSelect v-model="definitionKey" data-testid="shortflow-definition">
          <option value="">请选择</option>
          <option v-for="item in catalog.definitions" :key="`${item.definition_id}:${item.version}`" :value="`${item.definition_id}:${item.version}`">{{ item.name }} · v{{ item.version }}</option>
        </SearchSelect>
      </label>
      <label class="field">场景标识<input v-model="scenario" class="qc-input" maxlength="128" data-testid="shortflow-scenario" /></label>
      <fieldset><legend>绑定流程规则</legend>
        <label v-for="rule in rules" :key="rule.rule_id">
          <input v-model="ruleIds" type="checkbox" :value="rule.rule_id" :data-testid="`shortflow-rule-${rule.rule_id}`" />{{ rule.rule_code }} · {{ rule.name }}
        </label>
      </fieldset>
      <button type="button" class="qc-btn qc-btn--secondary" :disabled="!canAdd" data-testid="shortflow-add" @click="add">添加流程</button>
    </template>
    <ul v-if="modelValue.length">
      <li v-for="(item, index) in modelValue" :key="`${item.definition_id}:${item.scenario_key}`">
        {{ item.definition_id }} · v{{ item.version }} · {{ item.scenario_key }} · {{ item.rule_ids.length }} 条规则
        <button type="button" class="qc-btn qc-btn--secondary" @click="emit('update:modelValue', modelValue.filter((_, position) => position !== index))">移除</button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.shortflow-select { margin-top: 16px; }
fieldset { border: 1px solid var(--qc-border, #d9e1e8); padding: 12px; }
fieldset label { display: block; margin: 6px 0; }
</style>
