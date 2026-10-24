<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NDrawer, NDrawerContent } from 'naive-ui'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import {
  getRuleCatalog,
  type CatalogPageType,
  type CatalogRule,
  type CatalogRuleset,
  type RuleCatalog,
} from '@/services/ruleCatalog.api'
import { categoryLabel } from '@/utils/rule-labels'

/**
 * 部门质检平台 · 规则中心（2.1 原型 department-rules.html）：规则明细 / 页面配置 / 规则集三个页签，
 * 展示平台已发布（启用规则集里的启用规则）的内容。部门自定义页面、规则、规则集需要后端归属模型，
 * 本期先只读；有疑问或需要新检查项时走反馈。
 */
type Tab = 'rules' | 'pages' | 'sets'

const nav = usePortalNav()
const TABS: Array<{ key: Tab; label: string }> = [
  { key: 'rules', label: '规则明细' },
  { key: 'pages', label: '页面配置' },
  { key: 'sets', label: '规则集' },
]
const PRIORITY_LABELS: Record<string, string> = { low: '低', medium: '中', high: '高', critical: '严重' }
const CATEGORY_NAMES: Record<string, string> = { business: '业务页面', lifecycle: '生命周期页面' }
const SOURCE_NAMES: Record<string, string> = {
  manual: '手动创建', spreadsheet: '电子表格', import: '文件导入', api: '接口导入',
}

const catalog = ref<RuleCatalog | null>(null)
const loading = ref(true)
const loadError = ref('')
const tab = ref<Tab>('rules')
const keyword = ref('')
const severity = ref('')
const sourceType = ref('')
const applicablePage = ref('')
const ruleDetail = ref<CatalogRule | null>(null)
const pageDetail = ref<CatalogPageType | null>(null)
const rulesetDetail = ref<CatalogRuleset | null>(null)

const pageNames = computed(
  () => new Map((catalog.value?.page_types ?? []).map((item) => [item.code, item.name])),
)
const rulesByCode = computed(
  () => new Map((catalog.value?.rules ?? []).map((item) => [item.rule_code, item])),
)
const filteredRules = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  return (catalog.value?.rules ?? []).filter((item) =>
    (!needle || item.name.toLowerCase().includes(needle) || item.rule_code.toLowerCase().includes(needle)) &&
    (!severity.value || item.severity === severity.value) &&
    (!sourceType.value || ruleSourceKey(item) === sourceType.value) &&
    (!applicablePage.value || item.applicable_page_types.includes(applicablePage.value)),
  )
})
const sourceOptions = computed(() =>
  Array.from(new Set((catalog.value?.rules ?? []).map(ruleSourceKey))).map((value) => ({
    value,
    label: value === '__unknown__' ? '来源未记录' : SOURCE_NAMES[value] ?? value,
  })),
)
const pageOptions = computed(() =>
  Array.from(new Set([
    ...(catalog.value?.page_types ?? []).map((item) => item.code),
    ...(catalog.value?.rules ?? []).flatMap((item) => item.applicable_page_types),
  ])).map((value) => ({ value, label: pageNames.value.get(value) ?? value })),
)
const rulesetRules = computed(() => {
  const selected = rulesetDetail.value
  return selected ? (catalog.value?.rules ?? []).filter((rule) => belongsToRuleset(rule, selected)) : []
})

function ruleSourceKey(rule: CatalogRule) {
  return rule.source_type?.trim() || '__unknown__'
}

function sourceName(rule: CatalogRule) {
  const source = ruleSourceKey(rule)
  return source === '__unknown__' ? '来源未记录' : SOURCE_NAMES[source] ?? source
}

function belongsToRuleset(rule: CatalogRule, ruleset: CatalogRuleset) {
  // 新目录按版本 ID 精确关联；兼容旧目录时也必须同时匹配编码和版本。
  return rule.ruleset_id
    ? rule.ruleset_id === ruleset.ruleset_id
    : rule.ruleset_code === ruleset.ruleset_code && rule.ruleset_version === ruleset.version
}

function resetFilters() {
  keyword.value = ''
  severity.value = ''
  sourceType.value = ''
  applicablePage.value = ''
}

onMounted(() => void load())

async function load() {
  loading.value = true
  try {
    catalog.value = await getRuleCatalog()
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '规则中心加载失败'
  } finally {
    loading.value = false
  }
}

function pagesText(codes: readonly string[], limit = 3) {
  if (!codes.length) return '—'
  const names = codes.map((code) => pageNames.value.get(code) ?? code)
  return names.length > limit ? `${names.slice(0, limit).join('、')} 等` : names.join('、')
}

function relatedRulesText(page: CatalogPageType) {
  if (!page.related_rule_codes.length) return '未关联'
  const names = page.related_rule_codes.map((code) => rulesByCode.value.get(code)?.name ?? code)
  return `${names.length} 条：${names.slice(0, 3).join('、')}${names.length > 3 ? '…' : ''}`
}

function rulesetName(rule: CatalogRule) {
  const ruleset = catalog.value?.rulesets.find(
    (item) => belongsToRuleset(rule, item),
  )
  return ruleset ? `${ruleset.name} · v${ruleset.version}` : `${rule.ruleset_code} · v${rule.ruleset_version}`
}

function goFeedback(rule?: CatalogRule) {
  void nav.push({
    path: '/feedback',
    query: rule
      ? { tab: 'my', source: 'department-rules', rule: rule.rule_code, name: rule.name }
      : { tab: 'my', source: 'department-rules' },
  })
}
</script>

<template>
  <div class="rule-center" data-testid="department-rule-center">
    <header class="rc-head">
      <div>
        <h1>规则中心</h1>
        <p>查看平台已发布的规则、页面和规则集。对规则有疑问、或需要新的检查项，点右侧按钮告诉我们。</p>
      </div>
      <button type="button" class="qc-btn qc-btn-primary" data-testid="rule-center-feedback" @click="goFeedback()">
        反馈规则问题
      </button>
    </header>
    <p class="rc-note">部门自定义页面、规则和规则集的功能还在建设中，暂时请通过反馈提出需求。</p>

    <div class="workstation-glass-panel ap-tabs rc-tabs">
      <button
        v-for="item in TABS"
        :key="item.key"
        type="button"
        class="ap-tab"
        :class="{ 'is-active': tab === item.key }"
        :data-testid="`rule-center-tab-${item.key}`"
        @click="tab = item.key"
      >
        {{ item.label }}
      </button>
    </div>

    <section v-if="loading" class="workstation-glass-panel rc-state">规则中心加载中…</section>
    <section v-else-if="loadError" class="workstation-glass-panel rc-state">
      <span>{{ loadError }}</span>
      <button type="button" class="qc-link" @click="load()">重试</button>
    </section>
    <template v-else-if="catalog">
      <section v-show="tab === 'rules'" class="workstation-glass-panel rc-panel">
        <div class="rc-toolbar">
          <input v-model="keyword" class="qc-input" placeholder="搜索规则名称或编码" data-testid="rule-center-keyword" />
          <SearchSelect v-model="severity" aria-label="风险等级" data-testid="rule-center-severity">
            <option value="">全部风险等级</option>
            <option v-for="(label, value) in PRIORITY_LABELS" :key="value" :value="value">{{ label }}</option>
          </SearchSelect>
          <SearchSelect v-model="sourceType" aria-label="规则来源" data-testid="rule-center-source">
            <option value="">全部规则来源</option>
            <option v-for="item in sourceOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
          </SearchSelect>
          <SearchSelect v-model="applicablePage" aria-label="适用页面" data-testid="rule-center-page">
            <option value="">全部适用页面</option>
            <option v-for="item in pageOptions" :key="item.value" :value="item.value">{{ item.label }}</option>
          </SearchSelect>
          <button type="button" class="qc-link" data-testid="rule-center-reset" @click="resetFilters">重置</button>
          <span class="rc-count">共 {{ filteredRules.length }} 条已开启规则</span>
        </div>
        <div class="table-scroll">
          <table class="qc-table" data-testid="rule-center-rules">
            <thead>
              <tr>
                <th>规则编码</th>
                <th>规则名称</th>
                <th>适用页面</th>
                <th>优先级</th>
                <th>状态</th>
                <th class="is-right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="filteredRules.length === 0">
                <td colspan="6" class="rc-empty">没有符合条件的规则</td>
              </tr>
              <tr v-for="rule in filteredRules" :key="rule.rule_id">
                <td class="mono">{{ rule.rule_code }}</td>
                <td>{{ rule.name }}</td>
                <td>{{ pagesText(rule.applicable_page_types) }}</td>
                <td>{{ PRIORITY_LABELS[rule.severity] ?? rule.severity }}</td>
                <td><span class="qc-chip tone-success">已开启</span></td>
                <td class="is-right">
                  <button type="button" class="qc-link" @click="ruleDetail = rule">查看详情</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-show="tab === 'pages'" class="workstation-glass-panel rc-panel">
        <div class="table-scroll">
          <table class="qc-table" data-testid="rule-center-pages">
            <thead>
              <tr>
                <th>页面名称</th>
                <th>分类</th>
                <th>关联规则</th>
                <th>状态</th>
                <th class="is-right">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="page in catalog.page_types" :key="page.code">
                <td>{{ page.name }}</td>
                <td>{{ CATEGORY_NAMES[page.category] ?? page.category }}</td>
                <td class="rc-related">{{ relatedRulesText(page) }}</td>
                <td><span class="qc-chip tone-success">已开启</span></td>
                <td class="is-right">
                  <button type="button" class="qc-link" @click="pageDetail = page">查看详情</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section v-show="tab === 'sets'" class="rc-sets" data-testid="rule-center-sets">
        <p v-if="catalog.rulesets.length === 0" class="rc-empty">还没有开启的规则集</p>
        <article
          v-for="item in catalog.rulesets"
          :key="item.ruleset_id"
          class="workstation-glass-panel rc-set"
          role="button"
          tabindex="0"
          :aria-label="`查看${item.name} v${item.version}规则明细`"
          :data-testid="`rule-center-ruleset-${item.ruleset_id}`"
          @click="rulesetDetail = item"
          @keydown.enter="rulesetDetail = item"
          @keydown.space.prevent="rulesetDetail = item"
        >
          <header>
            <div>
              <strong>{{ item.name }}</strong>
              <small>{{ item.ruleset_code }} · v{{ item.version }}</small>
            </div>
            <span class="qc-chip tone-success">已开启</span>
          </header>
          <p>{{ item.description || '暂无说明' }}</p>
          <footer>
            <span>包含 {{ item.enabled_rule_count }} 条已开启规则</span>
            <span class="qc-link">查看规则</span>
          </footer>
        </article>
      </section>
    </template>

    <n-drawer :show="ruleDetail !== null" width="520" @update:show="(open) => { if (!open) ruleDetail = null }">
      <n-drawer-content v-if="ruleDetail" :title="ruleDetail.name" closable>
        <dl class="rc-detail" data-testid="rule-center-rule-detail">
          <dt>规则编码</dt><dd>{{ ruleDetail.rule_code }}</dd>
          <dt>检查什么</dt><dd>{{ ruleDetail.check_point || '—' }}</dd>
          <dt>适用页面</dt><dd>{{ pagesText(ruleDetail.applicable_page_types, 99) }}</dd>
          <dt>优先级</dt><dd>{{ PRIORITY_LABELS[ruleDetail.severity] ?? ruleDetail.severity }}</dd>
          <dt>规则来源</dt><dd>{{ sourceName(ruleDetail) }}</dd>
          <dt>分类</dt><dd>{{ categoryLabel(ruleDetail.category) }}</dd>
          <dt>所属规则集</dt><dd>{{ rulesetName(ruleDetail) }}</dd>
        </dl>
        <button type="button" class="qc-btn rc-detail-action" @click="goFeedback(ruleDetail)">反馈这条规则</button>
      </n-drawer-content>
    </n-drawer>

    <n-drawer :show="rulesetDetail !== null" width="min(720px, 94vw)" @update:show="(open) => { if (!open) rulesetDetail = null }">
      <n-drawer-content v-if="rulesetDetail" :title="`${rulesetDetail.name} · v${rulesetDetail.version}`" closable>
        <section data-testid="rule-center-ruleset-detail">
          <p class="rc-ruleset-note">{{ rulesetDetail.ruleset_code }} · 该版本共 {{ rulesetRules.length }} 条已开启规则</p>
          <p>{{ rulesetDetail.description || '暂无说明' }}</p>
          <p v-if="!rulesetRules.length" class="rc-empty">该版本没有已开启规则</p>
          <ol v-else class="rc-ruleset-rules">
            <li v-for="rule in rulesetRules" :key="rule.rule_id" :data-testid="`ruleset-rule-${rule.rule_id}`">
              <h3>{{ rule.name }} <small class="mono">{{ rule.rule_code }}</small></h3>
              <dl class="rc-detail">
                <dt>检查内容</dt><dd>{{ rule.check_point || '未填写检查内容' }}</dd>
                <dt>风险等级</dt><dd>{{ PRIORITY_LABELS[rule.severity] ?? rule.severity }}</dd>
                <dt>规则来源</dt><dd>{{ sourceName(rule) }}</dd>
                <dt>适用页面</dt><dd>{{ pagesText(rule.applicable_page_types, 99) }}</dd>
              </dl>
            </li>
          </ol>
        </section>
      </n-drawer-content>
    </n-drawer>

    <n-drawer :show="pageDetail !== null" width="520" @update:show="(open) => { if (!open) pageDetail = null }">
      <n-drawer-content v-if="pageDetail" :title="pageDetail.name" closable>
        <dl class="rc-detail" data-testid="rule-center-page-detail">
          <dt>页面分类</dt><dd>{{ CATEGORY_NAMES[pageDetail.category] ?? pageDetail.category }}</dd>
          <dt>页面说明</dt><dd>{{ pageDetail.description || '—' }}</dd>
          <dt>关联规则</dt>
          <dd>
            <template v-if="pageDetail.related_rule_codes.length">
              <div v-for="code in pageDetail.related_rule_codes" :key="code">
                {{ rulesByCode.get(code)?.name ?? code }}（{{ code }}）
              </div>
            </template>
            <template v-else>未关联，只参与页面识别</template>
          </dd>
        </dl>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.rule-center {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.rc-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.rc-head h1 {
  margin: 0 0 6px;
  color: var(--workstation-text-strong);
  font-size: 20px;
}

.rc-head p {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.rc-note {
  margin: 0;
  padding: 10px 14px;
  border: 1px solid rgba(217, 119, 6, 0.35);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-warning-strong);
  background: var(--tint-warning);
  font-size: 13px;
}

.rc-tabs {
  align-self: flex-start;
}

.rc-state {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 32px 20px;
  color: var(--workstation-text-muted);
}

.rc-panel {
  padding: 0;
  overflow: hidden;
}

.rc-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 16px 20px;
}

.rc-toolbar .qc-input {
  max-width: 320px;
}

.rc-toolbar .search-select {
  width: 170px;
}

.rc-count {
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.table-scroll {
  overflow-x: auto;
}

.rc-empty {
  padding: 28px 20px;
  color: var(--workstation-text-muted);
  text-align: center;
}

.rc-related {
  max-width: 420px;
  color: var(--workstation-text-muted);
}

.mono {
  font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
  font-size: 12px;
}

.rc-sets {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.rc-set {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 18px;
  cursor: pointer;
}

.rc-set:hover,
.rc-set:focus-visible {
  outline: 2px solid var(--workstation-primary);
  outline-offset: 2px;
}

.rc-set footer {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.rc-set header {
  display: flex;
  justify-content: space-between;
  gap: 12px;
}

.rc-set strong {
  display: block;
  color: var(--workstation-text-strong);
}

.rc-set small,
.rc-set footer {
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.rc-set p {
  margin: 0;
  color: var(--workstation-text);
  font-size: 13px;
}

.rc-detail {
  display: grid;
  grid-template-columns: 90px minmax(0, 1fr);
  gap: 8px 12px;
  margin: 0 0 16px;
  font-size: 13px;
}

.rc-detail dt {
  color: var(--workstation-text-muted);
}

.rc-detail dd {
  margin: 0;
  color: var(--workstation-text);
  line-height: 1.7;
  overflow-wrap: anywhere;
}

.rc-ruleset-note {
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.rc-ruleset-rules {
  padding-left: 24px;
}

.rc-ruleset-rules li + li {
  border-top: 1px solid var(--workstation-border);
  padding-top: 12px;
}

.rc-ruleset-rules h3 {
  color: var(--workstation-text-strong);
  font-size: 14px;
}

@media (max-width: 900px) {
  .rc-sets {
    grid-template-columns: 1fr;
  }
}
</style>
