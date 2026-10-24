<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { NButton, NDrawer, NDrawerContent } from 'naive-ui'
import { getPageConfig, type PageConfigItem, type WorkstationPageConfig } from '@/services/config.api'
import { SEVERITY_META } from '@/utils/status'
import ConfigCenterTabs from '../components/ConfigCenterTabs.vue'

/**
 * 配置中心 · 页面识别配置（2.1 原型 config.html「页面识别配置」页签）。
 * 页面类型、识别特征、进入 / 离开提示等全部来自页面识别目录；关联规则来自已启用规则的适用页面。
 * 页面的新增与开关需要后端落库支持，本期只读展示，按钮上说明原因。
 */
const config = ref<WorkstationPageConfig | null>(null)
const loading = ref(true)
const loadError = ref('')
const detail = ref<PageConfigItem | null>(null)

const CATEGORY_LABELS: Record<string, string> = {
  business: '业务页面',
  lifecycle: '生命周期页面',
}

async function fetchConfig() {
  loading.value = true
  try {
    config.value = await getPageConfig()
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : '页面配置加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => void fetchConfig())

const pageNames = computed(
  () => new Map((config.value?.pages ?? []).map((page) => [page.code, page.name])),
)

const statCards = computed(() => {
  const stats = config.value?.stats
  if (!stats) return []
  return [
    { label: '页面类型总数', value: stats.page_type_total, hint: `识别目录 ${config.value?.catalog_version ?? ''}` },
    { label: '已开启', value: stats.supported_total, hint: '可被自动识别的页面类型' },
    { label: '识别特征', value: stats.feature_total, hint: '文字关键词、页面结构与版面特征合计' },
    { label: '规则绑定数', value: stats.rule_binding_total, hint: `${stats.enabled_rule_total} 条启用规则跨页面复用` },
  ]
})

function categoryLabel(code: string) {
  return CATEGORY_LABELS[code] ?? (code || '未分类')
}

function relatedRulesText(page: PageConfigItem) {
  if (!page.bound_rules.length) return '未关联'
  const names = page.bound_rules.map((rule) => rule.name)
  const head = names.slice(0, 3).join('、')
  return `${names.length} 条：${head}${names.length > 3 ? '…' : ''}`
}

function pageName(code: string | null | undefined) {
  if (!code) return '—'
  return pageNames.value.get(code) ?? code
}

function listText(values: readonly string[]) {
  return values.length ? values.join('、') : '—'
}

function severityLabel(value: string) {
  return SEVERITY_META[value]?.label ?? value
}
</script>

<template>
  <div class="page-config" data-testid="page-identification-config">
    <ConfigCenterTabs active="page" />

    <section v-if="loading" class="workstation-glass-panel page-config-state">页面配置加载中…</section>
    <section v-else-if="loadError" class="workstation-glass-panel page-config-state">
      <span>{{ loadError }}</span>
      <n-button quaternary type="primary" @click="fetchConfig()">重试</n-button>
    </section>
    <template v-else-if="config">
      <section class="page-config-stats">
        <article v-for="card in statCards" :key="card.label" class="workstation-glass-panel stat-card">
          <span>{{ card.label }}</span>
          <strong>{{ card.value }}</strong>
          <small>{{ card.hint }}</small>
        </article>
      </section>

      <section class="workstation-glass-panel page-config-table">
        <header class="table-head">
          <div>
            <h2>页面类型配置</h2>
            <p>页面类型与识别特征来自页面识别目录；线上已覆盖的页面默认开启。</p>
          </div>
          <button
            type="button"
            class="qc-btn"
            disabled
            title="新增页面要先把页面加入识别目录并完成识别覆盖，暂不支持在这里直接新增"
          >
            新增页面
          </button>
        </header>
        <div class="table-scroll">
          <table class="qc-table" data-testid="page-config-table">
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
              <tr v-for="page in config.pages" :key="page.code">
                <td>
                  <strong class="page-name">{{ page.name }}</strong>
                  <small class="page-code">{{ page.code }}</small>
                </td>
                <td>{{ categoryLabel(page.category) }}</td>
                <td class="related-cell">{{ relatedRulesText(page) }}</td>
                <td>
                  <span class="qc-chip" :class="page.status === 'supported' ? 'tone-success' : 'tone-muted'">
                    {{ page.status === 'supported' ? '已开启' : '已关闭' }}
                  </span>
                </td>
                <td class="is-right">
                  <button type="button" class="qc-link" @click="detail = page">查看详情</button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </template>

    <n-drawer :show="detail !== null" width="560" placement="right" @update:show="(open) => { if (!open) detail = null }">
      <n-drawer-content v-if="detail" :title="`${detail.name} · 页面详情`" closable>
        <div class="detail" data-testid="page-config-detail">
          <section>
            <h3>基本信息</h3>
            <dl class="detail-grid">
              <dt>页面名称</dt><dd>{{ detail.name }}</dd>
              <dt>页面编码</dt><dd>{{ detail.code }}</dd>
              <dt>页面分类</dt><dd>{{ categoryLabel(detail.category) }}</dd>
              <dt>状态</dt><dd>{{ detail.status === 'supported' ? '已开启' : '已关闭' }}</dd>
              <dt>参与规则匹配</dt><dd>{{ detail.rule_matchable ? '是' : '否' }}</dd>
              <dt>计入覆盖率</dt><dd>{{ detail.coverage_required ? '是' : '否' }}</dd>
              <dt>默认详情页型</dt><dd>{{ pageName(detail.default_detail_page_type) }}</dd>
              <dt>来源</dt><dd>{{ detail.source || '—' }}</dd>
              <dt>备注</dt><dd>{{ detail.remark || '—' }}</dd>
            </dl>
          </section>
          <section v-if="detail.purpose">
            <h3>页面用途</h3>
            <p>{{ detail.purpose }}</p>
          </section>
          <section>
            <h3>进入 / 离开特征</h3>
            <dl class="detail-grid">
              <dt>怎么进入</dt><dd>{{ listText(detail.entry_hints) }}</dd>
              <dt>怎么离开</dt><dd>{{ listText(detail.exit_hints) }}</dd>
              <dt>下一跳页面</dt><dd>{{ listText(detail.next_page_types.map((code) => pageName(code))) }}</dd>
              <dt>高危动作</dt><dd>{{ listText(detail.risk_actions) }}</dd>
            </dl>
          </section>
          <section>
            <h3>识别特征</h3>
            <dl class="detail-grid">
              <dt>文字关键词</dt><dd>{{ listText(detail.ocr_keywords) }}</dd>
              <dt>页面结构提示</dt><dd>{{ listText(detail.dom_hints) }}</dd>
              <dt>版面特征</dt><dd>{{ listText(detail.layout_hints) }}</dd>
              <dt>页面别名</dt><dd>{{ listText(detail.aliases) }}</dd>
              <dt>导航别名</dt><dd>{{ listText(detail.navigation_aliases) }}</dd>
              <dt>地址前缀</dt><dd>{{ listText(detail.route_prefixes) }}</dd>
            </dl>
          </section>
          <section>
            <h3>关联规则（{{ detail.bound_rules.length }} 条）</h3>
            <ul v-if="detail.bound_rules.length" class="rule-list">
              <li v-for="rule in detail.bound_rules" :key="rule.rule_code">
                <span class="severity-chip" :class="SEVERITY_META[rule.severity]?.cls ?? 'sev-unknown'">
                  {{ severityLabel(rule.severity) }}
                </span>
                <span>{{ rule.name }}</span>
                <small>{{ rule.rule_code }}</small>
              </li>
            </ul>
            <p v-else class="muted">这个页面还没有关联规则，只参与页面识别。</p>
          </section>
        </div>
      </n-drawer-content>
    </n-drawer>
  </div>
</template>

<style scoped>
.page-config {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.page-config-state {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 32px 20px;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.page-config-stats {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.stat-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 16px 18px;
}

.stat-card span {
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.stat-card strong {
  color: var(--workstation-text-strong);
  font-size: 24px;
}

.stat-card small {
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.page-config-table {
  padding: 0;
  overflow: hidden;
}

.table-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 20px;
}

.table-head h2 {
  margin: 0 0 4px;
  color: var(--workstation-text-strong);
  font-size: 16px;
}

.table-head p {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.table-scroll {
  overflow-x: auto;
}

.page-name {
  display: block;
  color: var(--workstation-text-strong);
}

.page-code {
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.related-cell {
  max-width: 420px;
  color: var(--workstation-text-muted);
}

.detail {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.detail h3 {
  margin: 0 0 8px;
  color: var(--workstation-text-strong);
  font-size: 14px;
}

.detail p {
  margin: 0;
  color: var(--workstation-text);
  font-size: 13px;
  line-height: 1.7;
}

.detail-grid {
  display: grid;
  grid-template-columns: 110px minmax(0, 1fr);
  gap: 6px 12px;
  margin: 0;
  font-size: 13px;
}

.detail-grid dt {
  color: var(--workstation-text-muted);
}

.detail-grid dd {
  margin: 0;
  color: var(--workstation-text);
  overflow-wrap: anywhere;
}

.rule-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
  font-size: 13px;
}

.rule-list li {
  display: flex;
  align-items: center;
  gap: 8px;
}

.rule-list small {
  color: var(--workstation-text-faint);
}

.muted {
  color: var(--workstation-text-muted);
}

@media (max-width: 1100px) {
  .page-config-stats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
