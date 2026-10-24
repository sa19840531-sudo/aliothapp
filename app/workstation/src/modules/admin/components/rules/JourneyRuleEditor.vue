<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { getH5ShortflowCatalog, type H5ShortflowCatalog, type JourneyRuleContract } from '@/services/h5-shortflows.api'

const model = defineModel<JourneyRuleContract | null>({ required: true })
const props = defineProps<{ required?: boolean }>()
const definitions = ref<H5ShortflowCatalog['definitions']>([])
const error = ref('')
const loading = ref(true)
let alive = true
const key = (item: { definition_id: string; version: number }) => `${item.definition_id}:${item.version}`
const selected = computed(() => definitions.value.find((item) => model.value && key(item) === key(model.value)))
onBeforeUnmount(() => { alive = false })
onMounted(async () => {
  try {
    const result = await getH5ShortflowCatalog()
    if (alive) definitions.value = result.definitions.filter((item) => item.evidence_conditions?.length)
  } catch (cause) {
    if (alive) error.value = cause instanceof Error ? cause.message : '发布流程目录读取失败'
  } finally { if (alive) loading.value = false }
})

function choose(value: string | number) {
  const item = definitions.value.find((definition) => key(definition) === value)
  if (!item) { model.value = null; return }
  model.value = { definition_id: item.definition_id, version: item.version, content_hash: item.content_hash,
    workflow_skill: item.workflow_skill, scenario_key: '',
    evidence_conditions: JSON.parse(JSON.stringify(item.evidence_conditions)) }
}
function toggleCheckpoint(checkpointId: string) {
  if (!model.value || !selected.value?.evidence_conditions) return
  const current = model.value.evidence_conditions
  const exists = current.some((item) => item.checkpoint_id === checkpointId)
  const published = selected.value.evidence_conditions.find((item) => item.checkpoint_id === checkpointId)
  if (!published) return
  const next = exists
    ? current.filter((item) => item.checkpoint_id !== checkpointId)
    : [...current, JSON.parse(JSON.stringify(published))]
  model.value = { ...model.value, evidence_conditions: next }
}
const conditionFor = (checkpointId: string) =>
  model.value?.evidence_conditions.find((item) => item.checkpoint_id === checkpointId)
function validate() {
  if (!model.value) {
    if (props.required) error.value = '原规则已绑定流程，请选择发布版本；清空选择不会删除原合同'
    return !props.required
  }
  if (loading.value || !selected.value || selected.value.content_hash !== model.value.content_hash) {
    error.value = '原发布版本不可核对，不自动选择其他流程'; return false
  }
  if (!model.value.scenario_key.trim() || model.value.scenario_key !== model.value.scenario_key.trim()
    || !model.value.evidence_conditions.length
    || model.value.evidence_conditions.some((condition) => !condition.requirements.length
      || condition.requirements.some((item) => !item.artifact_types.length))) {
    error.value = '请填写场景，至少选择一个检查点，并为每项条件选择实际支持的材料'; return false
  }
  error.value = ''; return true
}
defineExpose({ validate })
</script>

<template>
  <section class="journey-editor">
    <label class="rule-field"><span class="qc-label">流程类型与发布版本</span>
      <SearchSelect :model-value="model ? key(model) : ''" :disabled="loading" data-testid="journey-definition" @update:model-value="choose">
        <option value="">未设置版本（不自动生成或启用流程）</option>
        <option v-for="item in definitions" :key="key(item)" :value="key(item)">{{ item.workflow_skill === 'h5-payment-workflow' ? '支付展示' : '播放器' }} · {{ item.name }} · v{{ item.version }}</option>
      </SearchSelect>
    </label>
    <p v-if="loading">发布流程加载中…</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="model">
      <p>版本哈希：{{ model.content_hash }}</p>
      <label class="rule-field"><span class="qc-label">场景标识</span>
        <input v-model="model.scenario_key" class="qc-input" maxlength="128" data-testid="journey-scenario" placeholder="必须与任务选择的场景一致" />
      </label>
      <fieldset v-for="condition in selected?.evidence_conditions || model.evidence_conditions" :key="condition.checkpoint_id">
        <legend>
          <label>
            <input type="checkbox" :checked="Boolean(conditionFor(condition.checkpoint_id))"
              :data-testid="`journey-checkpoint-${condition.checkpoint_id}`"
              :disabled="!selected" @change="toggleCheckpoint(condition.checkpoint_id)" />
            将检查点 {{ condition.checkpoint_id }} 用于本规则
          </label>
        </legend>
        <div v-for="requirement in conditionFor(condition.checkpoint_id)?.requirements || []" :key="requirement.role">
          <p>{{ requirement.role }} · {{ requirement.capture_phase || 'after_action' }}</p>
          <label v-for="kind in selected?.evidence_conditions?.find((item) => item.checkpoint_id === condition.checkpoint_id)?.requirements.find((item) => item.role === requirement.role)?.artifact_types || []" :key="kind">
            <input v-model="requirement.artifact_types" type="checkbox" :value="kind" />{{ kind }}
          </label>
          <label>最少独立样本
            <input v-model.number="requirement.minimum_count" type="number" min="1" :max="selected?.evidence_conditions?.find((item) => item.checkpoint_id === condition.checkpoint_id)?.requirements.find((item) => item.role === requirement.role)?.minimum_count || 1" :placeholder="'1'" />
          </label>
        </div>
      </fieldset>
    </template>
    <small>本规则使用选中的检查点及其必要依赖；依赖由服务端自动追加。完整流程的质量门禁与执行限制仍独立校验，局部结论不能替代整段流程验收。</small>
  </section>
</template>

<style scoped>
.journey-editor { display: grid; gap: 12px; overflow-wrap: anywhere; }
fieldset { border: 1px solid var(--qc-border); padding: 12px; }
label { display: block; margin-top: 6px; }
</style>
