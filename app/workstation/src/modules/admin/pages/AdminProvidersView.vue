<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useMessage } from 'naive-ui'
import {
  createProvider,
  getProvidersOverview,
  listProviderEvents,
  setPrimaryProvider,
  testProviderConnection,
  updateProvider,
  updateProviderSettings,
  type ProviderEvent,
  type ProviderItem,
  type ProviderPayload,
  type ProvidersOverview,
  type ProviderStatus,
} from '../api/providers'
import SearchSelect from '@/components/pickers/SearchSelect.vue'

/**
 * AI 服务与供应商（对齐 2.1 原型 provider-management.html）：监控状态 → 预警条 → 供应商卡片
 * （延迟 / 错误率 / 剩余额度，配置 / 连接测试 / 设为主供应商）→ 路由与故障转移 + 预警策略 → 最近服务事件。
 * 当前真正生效的供应商由环境变量决定；切换只改登记与审计记录，接口返回的说明会原样展示。
 */
const message = useMessage()

const overview = ref<ProvidersOverview | null>(null)
const loading = ref(true)
const loadError = ref('')
const showWarnings = ref(false)

async function load() {
  loading.value = true
  try {
    overview.value = await getProvidersOverview()
    loadError.value = ''
  } catch (error) {
    loadError.value = error instanceof Error ? error.message : 'AI 服务信息加载失败'
  } finally {
    loading.value = false
  }
}

const providers = computed(() => overview.value?.providers ?? [])
const routingRows = computed(() =>
  [...providers.value].sort((a, b) => Number(b.is_primary) - Number(a.is_primary) || a.priority - b.priority),
)

const STATUS_LABELS: Record<ProviderStatus, string> = {
  active: '使用中',
  standby: '待命',
  restricted: '受限',
  disabled: '已停用',
}
const STATUS_TONE: Record<ProviderStatus, string> = {
  active: 'is-success',
  standby: 'is-success',
  restricted: 'is-warning',
  disabled: 'is-muted',
}
const HEALTH_TONE: Record<ProviderItem['health'], string> = {
  healthy: 'tone-success',
  warning: 'tone-warning',
  down: 'tone-danger',
  unknown: 'tone-muted',
}

function latencyText(provider: ProviderItem) {
  return provider.last_check_latency_ms === null ? '未测' : `${provider.last_check_latency_ms}ms`
}

function errorRateText(provider: ProviderItem) {
  return provider.metrics.error_rate === null ? '—' : `${provider.metrics.error_rate}%`
}

function quotaText(provider: ProviderItem) {
  return provider.quota_remaining_percent === null ? '—' : `${Math.round(provider.quota_remaining_percent)}%`
}

function strategyText(provider: ProviderItem) {
  return `${provider.is_primary ? '主路由' : '备用'} · ${provider.weight}%`
}

function formatEventTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })
}

// ---------- 设置 ----------

const savingSettings = ref(false)

async function saveSettings() {
  const settings = overview.value?.settings
  if (!settings) return
  savingSettings.value = true
  try {
    const saved = await updateProviderSettings({ routing: settings.routing, alert_policy: settings.alert_policy })
    overview.value!.settings = saved
    message.success('设置已保存')
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存设置失败')
  } finally {
    savingSettings.value = false
  }
}

// ---------- 新增 / 配置 ----------

const editorOpen = ref(false)
const editingId = ref<string | null>(null)
const editingEnvManaged = ref(false)
const saving = ref(false)
const form = reactive({
  name: '',
  base_url: '',
  model: '',
  priority: 1,
  weight: 0,
  breaker_rule: '',
  status: 'standby' as ProviderStatus,
  quota_remaining_percent: '' as string | number,
})

function openCreate() {
  editingId.value = null
  editingEnvManaged.value = false
  form.name = ''
  form.base_url = ''
  form.model = ''
  form.priority = providers.value.length + 1
  form.weight = 0
  form.breaker_rule = ''
  form.status = 'standby'
  form.quota_remaining_percent = ''
  editorOpen.value = true
}

function openEdit(provider: ProviderItem) {
  editingId.value = provider.id
  editingEnvManaged.value = provider.managed_by_env
  form.name = provider.name
  form.base_url = provider.base_url ?? ''
  form.model = provider.model ?? ''
  form.priority = provider.priority
  form.weight = provider.weight
  form.breaker_rule = provider.breaker_rule ?? ''
  form.status = provider.status
  form.quota_remaining_percent = provider.quota_remaining_percent ?? ''
  editorOpen.value = true
}

function buildPayload(): ProviderPayload {
  const quota = String(form.quota_remaining_percent).trim()
  return {
    name: form.name.trim(),
    base_url: form.base_url.trim() || null,
    model: form.model.trim() || null,
    priority: Number(form.priority) || 1,
    weight: Number(form.weight) || 0,
    breaker_rule: form.breaker_rule.trim() || null,
    status: form.status,
    quota_remaining_percent: quota ? Number(quota) : null,
  }
}

async function submit() {
  if (!form.name.trim()) {
    message.warning('请输入供应商名称')
    return
  }
  saving.value = true
  try {
    if (editingId.value) {
      await updateProvider(editingId.value, buildPayload())
      message.success('供应商配置已更新')
    } else {
      await createProvider(buildPayload())
      message.success('供应商已登记（尚未接入调用）')
    }
    editorOpen.value = false
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '保存供应商失败')
  } finally {
    saving.value = false
  }
}

// ---------- 连接测试 / 切换主供应商 ----------

const testingId = ref<string | null>(null)

async function runConnectionTest(provider: ProviderItem) {
  testingId.value = provider.id
  try {
    const result = await testProviderConnection(provider.id)
    if (result.status === 'ok') message.success(`${provider.name}：${result.message}`)
    else if (result.status === 'auth_required') message.warning(`${provider.name}：${result.message}`)
    else message.error(`${provider.name}：${result.message}`)
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '连接测试失败')
  } finally {
    testingId.value = null
  }
}

const switchTarget = ref<ProviderItem | null>(null)
const switching = ref(false)
const currentPrimary = computed(() => providers.value.find((item) => item.is_primary) ?? null)

async function confirmSwitch() {
  const target = switchTarget.value
  if (!target) return
  switching.value = true
  try {
    await setPrimaryProvider(target.id)
    message.success(`已把 ${target.name} 记为主供应商；执行侧接入路由后生效`)
    switchTarget.value = null
    await load()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '切换失败')
  } finally {
    switching.value = false
  }
}

// ---------- 事件全列表 ----------

const eventsOpen = ref(false)
const allEvents = ref<ProviderEvent[]>([])
/** 「全部事件」弹窗先打开再请求：加载中说「加载中…」，不说「暂无事件」 */
const eventsLoading = ref(false)

async function openEvents() {
  eventsOpen.value = true
  eventsLoading.value = true
  try {
    allEvents.value = await listProviderEvents(50)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '事件加载失败')
  } finally {
    eventsLoading.value = false
  }
}

onMounted(() => void load())
</script>

<template>
  <div class="ap-page" data-testid="admin-providers">
    <p class="qc-muted stamp" data-testid="provider-monitor">
      <span class="dot" :class="overview?.monitor_status ?? 'normal'"></span>{{ overview?.monitor_label ?? '监控加载中' }}
    </p>

    <div class="ap-head">
      <div>
        <h1 class="title-sm">Token 服务与供应商</h1>
        <p>持续监控可用性、额度、延迟和错误率，并在服务异常时切换路由。</p>
      </div>
      <div class="ap-head-actions">
        <button type="button" class="qc-btn" @click="openEvents">查看操作审计</button>
        <button type="button" class="qc-btn qc-btn-primary" data-testid="provider-create" @click="openCreate">新增供应商</button>
      </div>
    </div>

    <p v-if="loadError" class="qc-error" data-testid="provider-error">{{ loadError }}</p>

    <section v-if="overview?.warnings.length" class="ap-note is-warning warning-bar" data-testid="provider-warning">
      <div class="warning-main">
        <span class="warning-icon">!</span>
        <div>
          <strong>{{ overview.warnings[0] }}</strong>
          <span v-if="overview.warnings.length > 1">还有 {{ overview.warnings.length - 1 }} 条预警</span>
          <span v-else>服务事件已记录，可在下方查看处理结果。</span>
          <ul v-if="showWarnings && overview.warnings.length > 1" class="warning-list">
            <li v-for="item in overview.warnings.slice(1)" :key="item">{{ item }}</li>
          </ul>
        </div>
      </div>
      <button type="button" class="qc-btn qc-btn-sm" @click="showWarnings = !showWarnings">
        {{ showWarnings ? '收起' : '查看预警详情' }}
      </button>
    </section>

    <section class="provider-grid" data-testid="provider-cards">
      <div v-if="!providers.length" class="workstation-glass-panel qc-empty">{{ loading ? '加载中…' : '还没有登记供应商' }}</div>
      <article
        v-for="provider in providers"
        :key="provider.id"
        class="workstation-glass-panel provider-card"
        :class="{ 'is-primary': provider.is_primary, 'is-warning': provider.health === 'warning', 'is-down': provider.health === 'down' }"
        :data-testid="`provider-card-${provider.code}`"
      >
        <div class="provider-head">
          <div>
            <div class="provider-kicker" :class="{ primary: provider.is_primary }">
              {{ provider.is_primary ? '当前主供应商' : `备用优先级 ${provider.priority}` }}
              <span v-if="provider.managed_by_env" class="env-tag">环境配置</span>
            </div>
            <h2>{{ provider.name }}</h2>
            <p class="qc-muted provider-model">{{ provider.model || '未填写模型' }}<template v-if="provider.base_url"> · {{ provider.base_url }}</template></p>
          </div>
          <span class="qc-chip" :class="HEALTH_TONE[provider.health]">{{ provider.health_label }}</span>
        </div>
        <div class="provider-metrics">
          <div>
            <strong>{{ latencyText(provider) }}</strong>
            <span>{{ provider.last_check_latency_ms === null ? '平均延迟（未测）' : '连接延迟' }}</span>
          </div>
          <div>
            <strong :class="{ 'is-warning': (provider.metrics.error_rate ?? 0) > 5 }">{{ errorRateText(provider) }}</strong>
            <span>错误率（24h {{ provider.metrics.call_count }} 次）</span>
          </div>
          <div>
            <strong :class="{ 'is-warning': provider.quota_remaining_percent !== null && provider.quota_remaining_percent < 20 }">{{ quotaText(provider) }}</strong>
            <span>剩余额度</span>
          </div>
        </div>
        <p v-if="provider.last_check_message" class="qc-muted provider-check">最近测试：{{ provider.last_check_message }}</p>
        <div class="provider-actions">
          <button type="button" class="qc-btn" @click="openEdit(provider)">配置</button>
          <button
            v-if="provider.is_primary"
            type="button"
            class="qc-btn qc-btn-dark"
            :disabled="testingId === provider.id"
            :data-testid="`provider-test-${provider.code}`"
            @click="runConnectionTest(provider)"
          >
            {{ testingId === provider.id ? '测试中…' : '连接测试' }}
          </button>
          <template v-else>
            <button type="button" class="qc-btn" :disabled="testingId === provider.id" @click="runConnectionTest(provider)">
              {{ testingId === provider.id ? '测试中…' : '连接测试' }}
            </button>
            <button
              type="button"
              class="qc-btn qc-btn-outline"
              :disabled="provider.status === 'disabled'"
              :data-testid="`provider-switch-${provider.code}`"
              @click="switchTarget = provider"
            >
              设为主供应商
            </button>
          </template>
        </div>
      </article>
    </section>

    <div class="routing-grid">
      <section class="workstation-glass-panel" data-testid="provider-routing">
        <div class="qc-card-head">
          <div>
            <h2 class="card-title">路由与故障转移</h2>
            <p class="card-sub">切换只影响后续新请求，执行中的任务保持原供应商上下文。</p>
          </div>
          <label v-if="overview" class="toggle">
            <input v-model="overview.settings.routing.auto_failover" type="checkbox" :disabled="savingSettings" data-testid="provider-auto-failover" @change="saveSettings" />
            启用自动故障转移
          </label>
        </div>
        <div class="ap-table-wrap">
          <table class="qc-table">
            <thead>
              <tr>
                <th>顺序</th>
                <th>供应商</th>
                <th>分配策略</th>
                <th>熔断条件</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!routingRows.length"><td colspan="5" class="qc-empty">{{ loading ? '加载中…' : '暂无供应商' }}</td></tr>
              <tr v-for="(provider, index) in routingRows" :key="provider.id">
                <td><strong>{{ index + 1 }}</strong></td>
                <td>{{ provider.name }}</td>
                <td>{{ strategyText(provider) }}</td>
                <td>{{ provider.breaker_rule || '—' }}</td>
                <td :class="STATUS_TONE[provider.status]">{{ STATUS_LABELS[provider.status] }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p v-if="overview" class="qc-muted routing-note" data-testid="provider-routing-note">
          当前生效：{{ overview.settings.effective_provider_code || '未配置' }}。{{ overview.settings.note }}
        </p>
      </section>

      <section class="workstation-glass-panel" data-testid="provider-alerts">
        <div class="qc-card-head"><h2 class="card-title">预警策略</h2></div>
        <div v-if="overview" class="alert-list">
          <label class="alert-row">
            <span><b>服务停止</b><small>连接失败或连续无响应</small></span>
            <input v-model="overview.settings.alert_policy.service_down" type="checkbox" :disabled="savingSettings" @change="saveSettings" />
          </label>
          <label class="alert-row">
            <span><b>额度不足</b><small>低于 20% 预警，低于 10% 停止分配</small></span>
            <input v-model="overview.settings.alert_policy.quota_low" type="checkbox" :disabled="savingSettings" @change="saveSettings" />
          </label>
          <label class="alert-row">
            <span><b>错误率异常</b><small>5 分钟错误率超过 5%</small></span>
            <input v-model="overview.settings.alert_policy.error_rate" type="checkbox" :disabled="savingSettings" @change="saveSettings" />
          </label>
          <label class="alert-row">
            <span><b>延迟异常</b><small>P95 延迟超过 8 秒</small></span>
            <input v-model="overview.settings.alert_policy.latency" type="checkbox" :disabled="savingSettings" @change="saveSettings" />
          </label>
          <div class="alert-note qc-muted">通知方式和负责人只展示配置项名称，页面不保存手机号、邮箱或认证信息。</div>
        </div>
        <p v-else-if="loading" class="qc-empty">加载中…</p>
      </section>
    </div>

    <section class="workstation-glass-panel" data-testid="provider-events">
      <div class="qc-card-head">
        <h2 class="card-title">最近服务事件</h2>
        <button type="button" class="qc-link" @click="openEvents">查看全部</button>
      </div>
      <div class="ap-table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th>时间</th>
              <th>供应商</th>
              <th>事件</th>
              <th>处理</th>
              <th>结果</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!overview?.events.length"><td colspan="5" class="qc-empty">{{ loading ? '加载中…' : '暂无事件' }}</td></tr>
            <tr v-for="event in overview?.events ?? []" :key="event.id">
              <td class="qc-muted">{{ formatEventTime(event.occurred_at) }}</td>
              <td>{{ event.provider_name }}</td>
              <td>{{ event.description }}</td>
              <td>{{ event.action || '—' }}</td>
              <td>{{ event.result || '—' }}<small v-if="event.operator_name" class="qc-muted"> · {{ event.operator_name }}</small></td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="switchTarget" class="ap-modal-backdrop" data-testid="provider-switch-modal" @click.self="switchTarget = null">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>确认切换主供应商</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="switchTarget = null">×</button>
        </div>
        <div class="ap-modal-body">
          <p class="confirm-text">
            将主供应商由「{{ currentPrimary?.name || '无' }}」切换为「{{ switchTarget.name }}」。切换只影响后续新请求，执行中的任务保持原调用上下文；操作会记入服务事件。
          </p>
          <p class="qc-muted confirm-note">{{ overview?.settings.note }}</p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="switchTarget = null">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="switching" data-testid="provider-switch-confirm" @click="confirmSwitch">确认切换</button>
        </div>
      </div>
    </div>

    <div v-if="editorOpen" class="ap-modal-backdrop" data-testid="provider-editor" @click.self="editorOpen = false">
      <div class="ap-modal">
        <div class="ap-modal-head">
          <h3>{{ editingId ? '供应商配置' : '新增供应商' }}</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="editorOpen = false">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="ap-field">
            <label class="required">供应商名称</label>
            <input v-model="form.name" class="qc-input" type="text" placeholder="例如：供应商 B" data-testid="provider-form-name" />
          </div>
          <div class="ap-field">
            <label>服务地址</label>
            <input v-model="form.base_url" class="qc-input" type="url" placeholder="https://…/v1" :disabled="editingEnvManaged" />
            <small v-if="editingEnvManaged" class="qc-muted">地址和模型由环境变量维护，页面只能改优先级、权重、熔断和状态。</small>
          </div>
          <div class="ap-field">
            <label>模型</label>
            <input v-model="form.model" class="qc-input" type="text" placeholder="例如：grok-4.5" :disabled="editingEnvManaged" />
          </div>
          <div class="form-row">
            <div class="ap-field">
              <label>备用优先级</label>
              <input v-model="form.priority" class="qc-input" type="number" min="1" max="99" />
            </div>
            <div class="ap-field">
              <label>路由权重（%）</label>
              <input v-model="form.weight" class="qc-input" type="number" min="0" max="100" />
            </div>
          </div>
          <div class="form-row">
            <div class="ap-field">
              <label>状态</label>
              <SearchSelect v-model="form.status">
                <option value="active">使用中</option>
                <option value="standby">待命</option>
                <option value="restricted">受限</option>
                <option value="disabled">已停用</option>
              </SearchSelect>
            </div>
            <div class="ap-field">
              <label>剩余额度（%）</label>
              <input v-model="form.quota_remaining_percent" class="qc-input" type="number" min="0" max="100" placeholder="选填" />
            </div>
          </div>
          <div class="ap-field">
            <label>熔断条件</label>
            <input v-model="form.breaker_rule" class="qc-input" type="text" placeholder="例如：连续 3 次失败或错误率 > 5%" />
          </div>
          <p class="qc-muted form-hint">页面不保存密钥；接入新供应商的密钥由运维在服务端配置。</p>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="editorOpen = false">取消</button>
          <button type="button" class="qc-btn qc-btn-primary" :disabled="saving" data-testid="provider-form-submit" @click="submit">保存</button>
        </div>
      </div>
    </div>

    <div v-if="eventsOpen" class="ap-modal-backdrop" data-testid="provider-events-modal" @click.self="eventsOpen = false">
      <div class="ap-modal is-wide">
        <div class="ap-modal-head">
          <h3>操作审计 · 服务事件</h3>
          <button type="button" class="ap-modal-close" aria-label="关闭" @click="eventsOpen = false">×</button>
        </div>
        <div class="ap-modal-body">
          <div class="ap-table-wrap">
            <table class="qc-table">
              <thead>
                <tr>
                  <th>时间</th>
                  <th>供应商</th>
                  <th>事件</th>
                  <th>处理</th>
                  <th>结果</th>
                  <th>操作人</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="!allEvents.length"><td colspan="6" class="qc-empty">{{ eventsLoading ? '加载中…' : '暂无事件' }}</td></tr>
                <tr v-for="event in allEvents" :key="event.id">
                  <td class="qc-muted">{{ formatEventTime(event.occurred_at) }}</td>
                  <td>{{ event.provider_name }}</td>
                  <td>{{ event.description }}</td>
                  <td>{{ event.action || '—' }}</td>
                  <td>{{ event.result || '—' }}</td>
                  <td>{{ event.operator_name || '系统' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>
        <div class="ap-modal-foot">
          <button type="button" class="qc-btn" @click="eventsOpen = false">关闭</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.stamp {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 -12px;
  font-size: 12px;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--workstation-success-strong);
}

.dot.warning {
  background: var(--workstation-warning);
}

.dot.down {
  background: var(--workstation-danger);
}

.title-sm {
  font-size: 20px;
}

.warning-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.warning-main {
  display: flex;
  align-items: flex-start;
  gap: 12px;
}

.warning-icon {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 36px;
  height: 36px;
  border-radius: 8px;
  color: #92400e;
  background: rgba(217, 119, 6, 0.18);
  font-weight: 700;
}

.warning-list {
  margin: 6px 0 0;
  padding-left: 18px;
}

.provider-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
}

.provider-card {
  padding: 20px;
}

.provider-card.is-primary {
  border: 2px solid var(--workstation-primary);
}

.provider-card.is-warning {
  border-color: rgba(217, 119, 6, 0.5);
}

.provider-card.is-down {
  border-color: rgba(220, 38, 38, 0.5);
}

.provider-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.provider-head h2 {
  margin: 4px 0 0;
  color: var(--workstation-text-strong);
  font-size: 16px;
  font-weight: 600;
}

.provider-kicker {
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.provider-kicker.primary {
  color: var(--workstation-primary);
  font-weight: 500;
}

.env-tag {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 4px;
  color: var(--workstation-text-muted);
  background: var(--tint-neutral);
  font-size: 11px;
}

.provider-model {
  margin: 4px 0 0;
  font-size: 12px;
  word-break: break-all;
}

.provider-metrics {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  margin-top: 20px;
  text-align: center;
}

.provider-metrics strong {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 18px;
  font-weight: 600;
}

.provider-metrics strong.is-warning {
  color: var(--workstation-warning);
}

.provider-metrics span {
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.provider-check {
  margin: 12px 0 0;
  font-size: 12px;
}

.provider-actions {
  display: flex;
  gap: 8px;
  margin-top: 20px;
}

.provider-actions .qc-btn {
  flex: 1;
  justify-content: center;
}

.qc-btn-dark {
  border-color: var(--workstation-sidebar-bg);
  color: #ffffff;
  background: var(--workstation-sidebar-bg);
}

.qc-btn-outline {
  border-color: var(--workstation-primary);
  color: var(--workstation-primary);
}

.routing-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.15fr) minmax(280px, 0.85fr);
  gap: 16px;
}

.card-title {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 15px;
  font-weight: 600;
}

.card-sub {
  margin: 4px 0 0;
  color: var(--workstation-text-faint);
  font-size: 12px;
}

.toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--workstation-text-muted);
  font-size: 14px;
}

.is-success {
  color: var(--workstation-success-strong);
}

.is-warning {
  color: var(--workstation-warning);
}

.is-muted {
  color: var(--workstation-text-faint);
}

.routing-note {
  margin: 0;
  padding: 12px 20px;
  border-top: 1px solid var(--workstation-border);
  font-size: 12px;
  line-height: 1.6;
}

.alert-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 20px;
}

.alert-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  font-size: 14px;
}

.alert-row b {
  display: block;
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.alert-row small {
  color: var(--workstation-text-muted);
}

.alert-note {
  padding: 12px;
  border-radius: 8px;
  background: var(--tint-neutral);
  font-size: 12px;
}

.form-row {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.form-hint {
  margin: 0;
  font-size: 12px;
}

.confirm-text {
  margin: 0;
  color: var(--workstation-text);
  font-size: 14px;
  line-height: 1.7;
}

.confirm-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.6;
}

@media (max-width: 1100px) {
  .routing-grid {
    grid-template-columns: 1fr;
  }
}
</style>
