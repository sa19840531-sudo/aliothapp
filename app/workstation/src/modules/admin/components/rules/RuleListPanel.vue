<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { NDrawer, NDrawerContent, NSelect, NSwitch, useMessage } from 'naive-ui'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { getRuleCatalog, type CatalogPageType } from '@/services/ruleCatalog.api'
import { categoryLabel } from '@/utils/rule-labels'
import JourneyRuleEditor from './JourneyRuleEditor.vue'
import type { JourneyRuleContract } from '@/services/h5-shortflows.api'
import {
  createBusinessRule,
  listRules,
  listRulesets,
  patchRule,
  setRuleStatus,
  type RuleItem,
  type RulesetSummary,
} from '../../api/rules'

/**
 * 规则配置 · 规则明细（2.1 原型）：按名称 / 编码、规则集、状态、风险等级、来源和适用页面筛选；状态用开关；
 * 新增 / 编辑只填规则名称、规则详情、适用页面、优先级，其余系统字段由后端补齐或原样保留。
 */
const props = defineProps<{ canWrite: boolean; selectedRulesetId?: string }>()
const emit = defineEmits<{ selectRuleset: [rulesetId: string] }>()

const PRIORITY_OPTIONS = [
  { label: '低', value: 'low' },
  { label: '中', value: 'medium' },
  { label: '高', value: 'high' },
  { label: '严重', value: 'critical' },
] as const
const EXECUTOR_LABELS: Record<string, string> = {
  rule_engine: '规则引擎',
  llm: '大模型',
  hybrid: '混合',
}
const SOURCE_LABELS: Record<RulesetSummary['source_type'], string> = {
  manual: '手动创建', spreadsheet: '电子表格', import: '文件导入', api: '接口导入',
}
const PAGE_SIZE = 20

const message = useMessage()
const rules = ref<RuleItem[]>([])
const total = ref(0)
const loading = ref(true)
const rulesets = ref<RulesetSummary[]>([])
// 规则集、页型目录还在加载：抽屉里先说「加载中」，不说「—」或「还没有规则集」
const rulesetsLoading = ref(true)
const pageTypes = ref<CatalogPageType[]>([])
const pageTypesLoading = ref(true)
const switching = ref<Record<string, boolean>>({})
const filters = reactive({
  keyword: '', rulesetId: props.selectedRulesetId ?? '', status: '' as '' | 'enabled' | 'disabled',
  severity: '' as '' | RuleItem['severity'], sourceType: '' as '' | RulesetSummary['source_type'],
  pageType: '', page: 1,
})
let loadVersion = 0

const drawerOpen = ref(false)
const saving = ref(false)
const editing = ref<RuleItem | null>(null)
const journeyContract = ref<JourneyRuleContract | null>(null)
const journeyEditor = ref<InstanceType<typeof JourneyRuleEditor> | null>(null)
const hasExistingJourneyContract = computed(() => Boolean(
  (editing.value?.judge_config?.capability_config as Record<string, unknown> | undefined)?.journey_rule_contract,
))
const form = reactive({
  name: '',
  checkPoint: '',
  pages: [] as string[],
  severity: 'medium' as RuleItem['severity'],
  evidenceScope: 'keep' as 'keep' | 'page' | 'journey',
})
const existingEvidenceScope = computed(() => {
  const capability = editing.value?.judge_config?.capability_config as Record<string, unknown> | undefined
  return String(capability?.evidence_input_scope ?? 'page')
})
const evidenceScopeEditable = computed(() => ['page', 'journey'].includes(existingEvidenceScope.value))
const journeyScopeAllowed = computed(() => !editing.value || JSON.stringify(editing.value.input_contract?.target_clients) === '["H5"]')
const effectiveEvidenceScope = computed(() => form.evidenceScope === 'keep' ? existingEvidenceScope.value : form.evidenceScope)

const pageNames = computed(() => new Map(pageTypes.value.map((item) => [item.code, item.name])))
const pageOptions = computed(() =>
  pageTypes.value.map((item) => ({ label: item.name, value: item.code })),
)
const rulesetOptions = computed(() => [
  { label: '全部规则集', value: '' },
  ...rulesets.value.map((item) => ({ label: `${item.name} · v${item.version}`, value: item.id })),
])
/** 新增规则加到哪个规则集：筛选里选了就用它，否则用第一个启用中的规则集。 */
const targetRuleset = computed(
  () =>
    rulesets.value.find((item) => item.id === filters.rulesetId && item.status !== 'archived') ??
    rulesets.value.find((item) => item.status === 'enabled') ??
    rulesets.value.find((item) => item.status !== 'archived') ??
    null,
)
const pageCount = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)))

onMounted(() => {
  void loadRulesets()
  void loadPageTypes()
  void loadRules()
})

watch(() => props.selectedRulesetId, (rulesetId) => {
  filters.rulesetId = rulesetId ?? ''
})
watch(() => filters.rulesetId, (rulesetId) => emit('selectRuleset', rulesetId))
watch(
  () => [filters.rulesetId, filters.status, filters.severity, filters.sourceType, filters.pageType],
  () => {
    filters.page = 1
    void loadRules()
  },
)

async function loadRulesets() {
  try {
    const result = await listRulesets({ page: 1, page_size: 100 })
    rulesets.value = result.items
  } catch (error) {
    message.error(error instanceof Error ? error.message : '规则集加载失败')
  } finally {
    rulesetsLoading.value = false
  }
}

async function loadPageTypes() {
  try {
    pageTypes.value = (await getRuleCatalog()).page_types
  } catch {
    // 页面名称拿不到时退回显示编码，不影响规则列表
  } finally {
    pageTypesLoading.value = false
  }
}

async function loadRules() {
  const version = ++loadVersion
  loading.value = true
  try {
    const result = await listRules({
      ruleset_id: filters.rulesetId || undefined,
      keyword: filters.keyword.trim() || undefined,
      status: filters.status || 'all',
      severity: filters.severity || undefined,
      source_type: filters.sourceType || undefined,
      page_type: filters.pageType || undefined,
      page: filters.page,
      page_size: PAGE_SIZE,
    })
    if (version !== loadVersion) return
    rules.value = result.items
    total.value = result.total
  } catch (error) {
    if (version === loadVersion) message.error(error instanceof Error ? error.message : '规则加载失败')
  } finally {
    if (version === loadVersion) loading.value = false
  }
}

function search() {
  filters.page = 1
  void loadRules()
}

function goPage(page: number) {
  filters.page = Math.min(Math.max(1, page), pageCount.value)
  void loadRules()
}

function pagesTitle(rule: RuleItem) {
  return rule.applicable_page_types.map((code) => pageNames.value.get(code) ?? code).join('、')
}

function priorityLabel(value: string) {
  return PRIORITY_OPTIONS.find((item) => item.value === value)?.label ?? value
}

async function toggleStatus(rule: RuleItem, enabled: boolean) {
  switching.value = { ...switching.value, [rule.id]: true }
  try {
    const updated = await setRuleStatus(rule.id, enabled ? 'enabled' : 'disabled')
    rules.value = rules.value.map((item) => (item.id === rule.id ? { ...item, ...updated } : item))
    message.success(enabled ? '规则已开启' : '规则已关闭')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '切换失败')
  } finally {
    switching.value = { ...switching.value, [rule.id]: false }
  }
}

function openCreate() {
  editing.value = null
  form.name = ''
  form.checkPoint = ''
  form.pages = []
  form.severity = 'medium'
  form.evidenceScope = 'keep'
  journeyContract.value = null
  drawerOpen.value = true
}

function openEdit(rule: RuleItem) {
  editing.value = rule
  form.name = rule.name
  form.checkPoint = rule.check_point
  form.pages = [...rule.applicable_page_types]
  form.severity = rule.severity
  form.evidenceScope = 'keep'
  const capability = rule.judge_config?.capability_config as Record<string, unknown> | undefined
  journeyContract.value = capability?.journey_rule_contract
    ? JSON.parse(JSON.stringify(capability.journey_rule_contract)) : null
  drawerOpen.value = true
}

async function save() {
  if (effectiveEvidenceScope.value === 'journey' && journeyEditor.value && !journeyEditor.value.validate()) return
  if (!form.name.trim() || !form.checkPoint.trim() || form.pages.length === 0) {
    message.error('规则名称、规则详情和适用页面都要填')
    return
  }
  const payload = {
    name: form.name.trim(),
    check_point: form.checkPoint.trim(),
    applicable_page_types: form.pages,
    severity: form.severity,
    ...(evidenceScopeEditable.value && form.evidenceScope !== 'keep'
      ? { evidence_input_scope: form.evidenceScope } : {}),
    ...(effectiveEvidenceScope.value === 'journey' && journeyContract.value
      ? { journey_rule_contract: journeyContract.value } : {}),
  }
  saving.value = true
  try {
    if (editing.value) {
      await patchRule(editing.value.id, payload)
      message.success('规则已保存')
    } else {
      if (rulesetsLoading.value) {
        message.warning('规则集还在加载，请稍后再保存')
        return
      }
      if (!targetRuleset.value) {
        message.error('还没有规则集，先到「规则集」里新增一个')
        return
      }
      await createBusinessRule(targetRuleset.value.id, payload)
      message.success('规则已新增，默认关闭；测试没问题后再开启')
    }
    drawerOpen.value = false
    await loadRules()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存失败')
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="workstation-glass-panel rule-list-panel" data-testid="rule-list-panel">
    <div class="rule-filters">
      <input
        v-model="filters.keyword"
        class="qc-input"
        placeholder="搜索规则名称或编码"
        data-testid="rule-keyword"
        @keyup.enter="search"
      />
      <SearchSelect v-model="filters.rulesetId" data-testid="rule-ruleset-filter">
        <option v-for="option in rulesetOptions" :key="option.value" :value="option.value">
          {{ option.label }}
        </option>
      </SearchSelect>
      <SearchSelect v-model="filters.status" data-testid="rule-status-filter">
        <option value="">全部状态</option>
        <option value="enabled">已开启</option>
        <option value="disabled">已关闭</option>
      </SearchSelect>
      <SearchSelect v-model="filters.severity" aria-label="风险等级" data-testid="rule-severity-filter">
        <option value="">全部风险等级</option>
        <option v-for="option in PRIORITY_OPTIONS" :key="option.value" :value="option.value">{{ option.label }}</option>
      </SearchSelect>
      <SearchSelect v-model="filters.sourceType" aria-label="规则来源" data-testid="rule-source-filter">
        <option value="">全部来源</option>
        <option v-for="(label, value) in SOURCE_LABELS" :key="value" :value="value">{{ label }}</option>
      </SearchSelect>
      <SearchSelect v-model="filters.pageType" aria-label="适用页面" data-testid="rule-page-filter">
        <option value="">全部适用页面</option>
        <option v-for="option in pageOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
      </SearchSelect>
      <button type="button" class="qc-btn" @click="search">搜索</button>
      <button
        v-if="props.canWrite"
        type="button"
        class="qc-btn qc-btn-primary"
        data-testid="rule-create"
        @click="openCreate"
      >
        新增规则
      </button>
    </div>

    <div class="table-scroll">
      <table class="qc-table" data-testid="rule-table">
        <thead>
          <tr>
            <th>规则编码</th>
            <th>规则名称</th>
            <th>规则集</th>
            <th>分类</th>
            <th>执行器</th>
            <th>风险等级</th>
            <th>规则来源</th>
            <th>适用页面</th>
            <th>状态</th>
            <th v-if="props.canWrite" class="is-right">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="loading">
            <td :colspan="props.canWrite ? 10 : 9" class="table-empty">规则加载中…</td>
          </tr>
          <tr v-else-if="rules.length === 0">
            <td :colspan="props.canWrite ? 10 : 9" class="table-empty">没有符合条件的规则</td>
          </tr>
          <tr v-for="rule in rules" v-else :key="rule.id">
            <td class="mono">{{ rule.rule_code }}</td>
            <td class="rule-name-cell">{{ rule.name }}</td>
            <td>{{ rule.ruleset_name || '—' }}<span v-if="rule.ruleset_version"> · v{{ rule.ruleset_version }}</span></td>
            <td>{{ categoryLabel(rule.category) }}</td>
            <td>{{ EXECUTOR_LABELS[rule.executor_type] ?? rule.executor_type }}</td>
            <td>{{ priorityLabel(rule.severity) }}</td>
            <td>{{ rule.source_type ? SOURCE_LABELS[rule.source_type] ?? rule.source_type : '—' }}</td>
            <td :title="pagesTitle(rule)">{{ rule.applicable_page_types.length }} 个页型</td>
            <td>
              <n-switch
                v-if="props.canWrite"
                :value="rule.status === 'enabled'"
                :loading="switching[rule.id]"
                :data-testid="`rule-switch-${rule.rule_code}`"
                @update:value="(value: boolean) => toggleStatus(rule, value)"
              />
              <span v-else class="qc-chip" :class="rule.status === 'enabled' ? 'tone-success' : 'tone-muted'">
                {{ rule.status === 'enabled' ? '已开启' : '已关闭' }}
              </span>
            </td>
            <td v-if="props.canWrite" class="is-right">
              <button type="button" class="qc-link" @click="openEdit(rule)">编辑</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div class="qc-pager">
      <span>{{ loading && !rules.length ? '加载中…' : `共 ${total} 条规则` }}</span>
      <div class="pager-actions">
        <button type="button" class="qc-btn qc-btn-sm" :disabled="filters.page <= 1" @click="goPage(filters.page - 1)">
          上一页
        </button>
        <span>{{ filters.page }} / {{ pageCount }}</span>
        <button
          type="button"
          class="qc-btn qc-btn-sm"
          :disabled="filters.page >= pageCount"
          @click="goPage(filters.page + 1)"
        >
          下一页
        </button>
      </div>
    </div>

    <n-drawer v-model:show="drawerOpen" width="min(560px, 100vw)" placement="right">
      <n-drawer-content :title="editing ? '编辑规则' : '新增规则'" closable>
        <div class="rule-form" data-testid="rule-form">
          <label class="rule-field">
            <span class="qc-label">规则名称</span>
            <input v-model="form.name" class="qc-input" placeholder="例如：首页不应出现错别字" data-testid="rule-form-name" />
          </label>
          <label class="rule-field">
            <span class="qc-label">规则详情（写清楚要检查什么、怎样算通过）</span>
            <textarea v-model="form.checkPoint" class="qc-textarea" rows="5" data-testid="rule-form-detail" />
          </label>
          <div class="rule-field">
            <span class="qc-label">适用页面</span>
            <n-select
              v-model:value="form.pages"
              multiple
              filterable
              :options="pageOptions"
              :loading="pageTypesLoading"
              placeholder="选择这条规则要检查的页面"
              data-testid="rule-form-pages"
            />
          </div>
          <label class="rule-field">
            <span class="qc-label">优先级</span>
            <SearchSelect v-model="form.severity" data-testid="rule-form-severity">
              <option v-for="option in PRIORITY_OPTIONS" :key="option.value" :value="option.value">
                {{ option.label }}
              </option>
            </SearchSelect>
          </label>
          <label class="rule-field">
            <span class="qc-label">证据输入范围</span>
            <SearchSelect v-model="form.evidenceScope" :disabled="!evidenceScopeEditable" data-testid="rule-form-evidence-scope">
              <option value="keep">保持原合同（{{ existingEvidenceScope }}）</option>
              <option value="page">单页证据</option>
              <option value="journey" :disabled="!journeyScopeAllowed">H5 V2 短流程证据（仅 H5 规则）</option>
            </SearchSelect>
            <small>只切换明确的输入范围；不修改质量门禁、预算、判定器或人工复核阈值。新流程仍需发布合同并在任务中显式选择。</small>
          </label>
          <JourneyRuleEditor v-if="effectiveEvidenceScope === 'journey' && journeyScopeAllowed" ref="journeyEditor" v-model="journeyContract" :required="hasExistingJourneyContract" />
          <p v-if="editing" class="rule-form-note">
            规则编号 {{ editing.rule_code }} · 所属规则集 {{ editing.ruleset_name || '—' }} ·
            执行器 {{ EXECUTOR_LABELS[editing.executor_type] ?? editing.executor_type }}。
            判定方式、证据要求等系统配置保持不变。
          </p>
          <p v-else class="rule-form-note">
            将加入「{{ targetRuleset?.name ?? (rulesetsLoading ? '加载中…' : '—') }}」；规则编号、判定方式等由系统自动补齐。
            新增后默认关闭，建议先在「规则测试」里测一下再开启。
          </p>
          <div class="rule-form-actions">
            <button type="button" class="qc-btn" @click="drawerOpen = false">取消</button>
            <button
              type="button"
              class="qc-btn qc-btn-primary"
              :disabled="saving"
              data-testid="rule-form-save"
              @click="save"
            >
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </n-drawer-content>
    </n-drawer>
  </section>
</template>

<style scoped>
.rule-list-panel {
  padding: 0;
  overflow: hidden;
}

.rule-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  padding: 16px 20px;
}

.rule-filters .qc-input {
  flex: 1 1 200px;
  min-width: 180px;
}

.rule-filters :deep(.search-select) {
  width: 160px;
}

.table-scroll {
  overflow-x: auto;
}

.table-empty {
  padding: 32px 20px;
  color: var(--workstation-text-muted);
  text-align: center;
}

.mono {
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 12px;
}

.rule-name-cell {
  min-width: 200px;
}

.pager-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.rule-form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.rule-field {
  display: flex;
  flex-direction: column;
}

.rule-form-note {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.7;
}

.rule-form-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}

@media (max-width: 700px) {
  .rule-filters :deep(.search-select) {
    flex: 1 1 140px;
  }
}
</style>
