<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  executeDeliveryIncidentCommand,
  listDeliveryIncidents,
  type DeliveryIncident,
  type DeliveryOperationAction,
} from '@/services/system.api'
import { authStore } from '@/stores/auth'

const props = defineProps<{
  taskId: string
  targetPlatform: 'h5' | 'app' | 'web'
}>()

const message = useMessage()
const incidents = ref<DeliveryIncident[]>([])
const loading = ref(false)
const errorMessage = ref('')
const pendingAction = ref<{
  incident: DeliveryIncident
  action: DeliveryOperationAction
} | null>(null)
const reason = ref('')
const submitting = ref(false)

const canOperate = computed(
  () =>
    (props.targetPlatform === 'h5' || props.targetPlatform === 'app') &&
    (authStore.user.value?.permissions.includes('systemOperations') ?? false),
)

const actionDefinitions: Array<{ action: DeliveryOperationAction; label: string }> = [
  { action: 'reconcile', label: '对账恢复' },
  { action: 'requeue', label: '重新投递' },
  { action: 'mark_needs_review', label: '标记需复核' },
]

let loadSequence = 0

async function loadIncidents() {
  if (!canOperate.value || !props.taskId.trim()) {
    incidents.value = []
    return
  }
  const sequence = ++loadSequence
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await listDeliveryIncidents(props.taskId)
    if (sequence === loadSequence) {
      incidents.value = result
    }
  } catch (error) {
    if (sequence !== loadSequence) return
    errorMessage.value = error instanceof Error ? error.message : '投递事故加载失败'
  } finally {
    if (sequence === loadSequence) {
      loading.value = false
    }
  }
}

watch([() => props.taskId, () => props.targetPlatform, canOperate], () => {
  if (!canOperate.value) {
    incidents.value = []
    pendingAction.value = null
    return
  }
  void loadIncidents()
}, { immediate: true })

function isAllowed(incident: DeliveryIncident, action: DeliveryOperationAction) {
  return incident.allowed_actions.includes(action)
}

function beginAction(incident: DeliveryIncident, action: DeliveryOperationAction) {
  pendingAction.value = { incident, action }
  reason.value = ''
  errorMessage.value = ''
}

function cancelAction() {
  if (submitting.value) return
  pendingAction.value = null
  reason.value = ''
}

async function confirmAction() {
  const current = pendingAction.value
  const normalizedReason = reason.value.trim()
  if (!current) return
  if (!normalizedReason) {
    errorMessage.value = '请填写处置理由后再确认'
    return
  }
  if (normalizedReason.length > 500) {
    errorMessage.value = '处置理由不能超过 500 字符'
    return
  }

  submitting.value = true
  errorMessage.value = ''
  try {
    const result = await executeDeliveryIncidentCommand(
      current.incident.platform,
      current.incident.publication_id,
      {
        action: current.action,
        reason: normalizedReason,
        expected_row_version: current.incident.row_version,
      },
    )
    pendingAction.value = null
    reason.value = ''
    message.success(result.result || '投递处置已受理')
    await loadIncidents()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '投递处置失败'
  } finally {
    submitting.value = false
  }
}

function formatDate(value: string | null | undefined) {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

function platformLabel(platform: DeliveryIncident['platform']) {
  return platform === 'h5' ? 'H5' : 'App'
}

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    sealed: '已封存',
    delivered: '已投递',
    claimed: '已领取',
    judged: '已判定',
    published: '已完成',
    failed: '失败',
    needs_review: '需要复核',
    pending: '处理中',
  }
  return labels[status] || status
}

function actionLabel(action: DeliveryOperationAction) {
  return actionDefinitions.find((item) => item.action === action)?.label || action
}

function sourceIdentityLabel(incident: DeliveryIncident) {
  const source = incident.source_identity
  const identity = [
    source.page_capture_id,
    source.capture_id,
    source.evidence_index_id,
    source.input_set_id,
  ].find((value) => typeof value === 'string' && value)
  return identity ? String(identity) : '主证据身份已锁定'
}

function hasDeadLetter(incident: DeliveryIncident) {
  return incident.outbox_dead_letter_count > 0 || incident.inbox_dead_letter_count > 0
}
</script>

<template>
  <section v-if="canOperate" class="delivery-incident-panel workstation-glass-panel" data-testid="delivery-incident-panel">
    <div class="delivery-panel-heading">
      <div>
        <h3>规则投递运营处置</h3>
        <p>仅展示 H5/App 主证据链的 Publication、Outbox、Inbox 与 Work 事故，不包含补采。</p>
      </div>
      <button class="delivery-refresh-button" type="button" :disabled="loading" @click="loadIncidents">
        {{ loading ? '加载中…' : '刷新事故' }}
      </button>
    </div>

    <div v-if="loading && !incidents.length" class="delivery-empty">正在读取投递事实…</div>
    <div v-else-if="errorMessage && !incidents.length" class="delivery-error" role="alert">
      {{ errorMessage }}
      <button type="button" @click="loadIncidents">重试</button>
    </div>
    <div v-else-if="!incidents.length" class="delivery-empty">当前任务没有可处置的投递事故。</div>

    <div v-else class="delivery-incident-list">
      <article
        v-for="incident in incidents"
        :key="`${incident.platform}:${incident.publication_id}`"
        class="delivery-incident-card"
        :class="{ 'has-dead-letter': hasDeadLetter(incident) }"
        :data-testid="`delivery-incident-${incident.publication_id}`"
      >
        <div class="delivery-card-head">
          <div>
            <strong>{{ platformLabel(incident.platform) }} · Publication</strong>
            <p class="mono">{{ incident.publication_id }}</p>
          </div>
          <span class="delivery-status">{{ statusLabel(incident.status) }}</span>
        </div>

        <div class="delivery-card-meta">
          <span>Outbox：{{ incident.outbox_pending_count }} 待处理 / {{ incident.outbox_dead_letter_count }} 死信</span>
          <span>Inbox：{{ incident.inbox_pending_count }} 待处理 / {{ incident.inbox_dead_letter_count }} 死信</span>
          <span>Work：{{ incident.linked_work_active_count }} 未终态 / {{ incident.linked_work_item_count }} 总数</span>
          <span>Work 失败：{{ incident.linked_work_failed_count }}</span>
          <span>epoch：{{ incident.finalization_epoch }} · row：{{ incident.row_version }}</span>
        </div>
        <p class="delivery-source mono">输入身份：{{ sourceIdentityLabel(incident) }}</p>
        <p v-if="incident.failure_code" class="delivery-failure">
          <b>{{ incident.failure_code }}</b> · {{ incident.failure_message || '无错误详情' }}
        </p>

        <div class="delivery-actions">
          <button
            v-for="definition in actionDefinitions"
            :key="definition.action"
            type="button"
            class="delivery-action-button"
            :disabled="!isAllowed(incident, definition.action) || submitting"
            :data-testid="`delivery-action-${definition.action}`"
            @click="beginAction(incident, definition.action)"
          >
            {{ definition.label }}
          </button>
        </div>

        <div v-if="incident.operations.length" class="delivery-operation-timeline">
          <strong>最近处置</strong>
          <ol>
            <li v-for="operation in incident.operations" :key="operation.operation_id">
              <span>{{ actionLabel(operation.action) }} · {{ operation.result }}</span>
              <small>{{ operation.operator_user_id }} · {{ formatDate(operation.created_at) }}</small>
              <p>{{ operation.reason }}</p>
            </li>
          </ol>
        </div>
      </article>
    </div>

    <div v-if="pendingAction" class="delivery-confirm" role="dialog" aria-modal="true">
      <strong>确认{{ actionLabel(pendingAction.action) }}</strong>
      <p>
        将对 {{ platformLabel(pendingAction.incident.platform) }} Publication
        <span class="mono">{{ pendingAction.incident.publication_id }}</span>
        执行处置；服务端会再次校验 epoch、规则快照和证据身份。
      </p>
      <label>
        处置理由（必填）
        <textarea
          v-model="reason"
          rows="3"
          maxlength="500"
          data-testid="delivery-reason-input"
          placeholder="说明为什么需要恢复、重投或转人工复核"
        />
      </label>
      <p v-if="errorMessage" class="delivery-error" role="alert">{{ errorMessage }}</p>
      <div class="delivery-confirm-actions">
        <button type="button" :disabled="submitting" @click="cancelAction">取消</button>
        <button type="button" data-testid="confirm-delivery-action" :disabled="submitting" @click="confirmAction">
          {{ submitting ? '提交中…' : '确认处置' }}
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.delivery-incident-panel {
  margin-top: 18px;
  padding: 22px;
}

.delivery-panel-heading,
.delivery-card-head,
.delivery-confirm-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.delivery-panel-heading h3,
.delivery-card-head strong {
  margin: 0;
  color: var(--workstation-text-strong);
}

.delivery-panel-heading p,
.delivery-card-head p,
.delivery-confirm p,
.delivery-operation-timeline p {
  margin: 6px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.delivery-refresh-button,
.delivery-action-button,
.delivery-confirm-actions button,
.delivery-error button {
  border: 1px solid var(--workstation-border-strong);
  border-radius: 7px;
  padding: 7px 12px;
  background: var(--workstation-panel-soft);
  color: var(--workstation-primary);
  font: inherit;
  cursor: pointer;
}

.delivery-refresh-button:disabled,
.delivery-action-button:disabled,
.delivery-confirm-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.delivery-incident-list {
  display: grid;
  gap: 12px;
  margin-top: 16px;
}

.delivery-incident-card {
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  padding: 15px;
  background: var(--workstation-panel-soft);
}

.delivery-incident-card.has-dead-letter {
  border-color: rgba(210, 92, 70, 0.38);
}

.delivery-status {
  border-radius: 999px;
  padding: 4px 9px;
  border: 1px solid var(--workstation-border-strong);
  background: var(--workstation-panel-soft);
  color: var(--workstation-primary);
  font-size: 12px;
}

.delivery-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 7px 14px;
  margin-top: 12px;
  color: var(--workstation-text);
  font-size: 12px;
  line-height: 1.5;
}

.delivery-source {
  margin: 10px 0 0;
  color: var(--workstation-text-muted);
  font-size: 11px;
}

.delivery-failure {
  margin: 10px 0 0;
  color: var(--workstation-danger);
  font-size: 12px;
}

.delivery-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 14px;
}

.delivery-action-button {
  background: var(--workstation-panel);
}

.delivery-operation-timeline {
  margin-top: 14px;
  border-top: 1px dashed var(--workstation-border-strong);
  padding-top: 12px;
  color: var(--workstation-text);
  font-size: 12px;
}

.delivery-operation-timeline ol {
  display: grid;
  gap: 8px;
  margin: 8px 0 0;
  padding-left: 22px;
}

.delivery-operation-timeline li {
  padding-left: 3px;
}

.delivery-operation-timeline small {
  display: block;
  margin-top: 2px;
  color: var(--workstation-text-muted);
}

.delivery-confirm {
  margin-top: 16px;
  border: 1px solid var(--workstation-border-strong);
  border-radius: 9px;
  padding: 14px;
  background: var(--workstation-panel-soft);
}

.delivery-confirm label {
  display: block;
  margin-top: 12px;
  color: var(--workstation-text);
  font-size: 12px;
}

.delivery-confirm textarea {
  display: block;
  box-sizing: border-box;
  width: 100%;
  margin-top: 6px;
  border: 1px solid var(--workstation-border-strong);
  border-radius: 7px;
  padding: 9px;
  resize: vertical;
  background: var(--workstation-input-bg);
  color: var(--workstation-text);
  font: inherit;
  font-size: 13px;
}

.delivery-confirm-actions {
  justify-content: flex-end;
  margin-top: 12px;
}

.delivery-confirm-actions button:last-child {
  background: var(--workstation-primary-strong);
  color: #ffffff;
}

.delivery-empty,
.delivery-error {
  margin-top: 14px;
  color: var(--workstation-text-muted);
  font-size: 13px;
}

.delivery-error {
  color: var(--workstation-danger);
}

.delivery-error button {
  margin-left: 8px;
  color: var(--workstation-danger);
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

@media (max-width: 640px) {
  .delivery-incident-panel {
    padding: 15px;
  }

  .delivery-panel-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
