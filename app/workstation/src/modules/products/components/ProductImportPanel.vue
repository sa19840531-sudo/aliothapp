<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useMessage } from 'naive-ui'
import {
  downloadProductImportResult,
  downloadProductImportTemplate,
  importProducts,
  listProductImportRecords,
  type ProductImportRecord,
  type ProductImportResult,
} from '@/services/products.api'
import { usePortalNav } from '@/composables/usePortalNav'
import { authStore } from '@/stores/auth'

/**
 * Excel 产品导入 + 导入记录（对齐 2.1 原型 product-import.html）：拖拽 / 点击上传 xlsx（也收 CSV），
 * 字段说明，导入进度与结果，导入记录表（批次 / 文件 / 导入人 / 时间 / 成功失败 / 下载结果）。
 * 部门平台的产品页和管理后台的「Excel 导入」页共用。
 */
const props = defineProps<{ tab: 'import' | 'record' }>()
const emit = defineEmits<{ imported: [result: ProductImportResult]; 'update:tab': [tab: 'import' | 'record'] }>()

const message = useMessage()

const canReadAny = computed(() => (authStore.user.value?.permissions ?? []).includes('taskObjects:read:any'))
/** 部门平台不让填公司 / 部门：导入的产品自动归本部门（后端同样固定）。 */
const nav = usePortalNav()

const file = ref<File | null>(null)
const dragging = ref(false)
const importing = ref(false)
const result = ref<ProductImportResult | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)

const ACCEPTED = ['.xlsx', '.csv']

function acceptFile(candidate: File | null | undefined) {
  if (!candidate) return
  const lowered = candidate.name.toLowerCase()
  if (!ACCEPTED.some((ext) => lowered.endsWith(ext))) {
    message.warning('只支持 .xlsx 或 .csv 文件；旧版 .xls 请另存为 .xlsx')
    return
  }
  if (candidate.size > 5 * 1024 * 1024) {
    message.warning('单文件不能超过 5MB')
    return
  }
  file.value = candidate
  result.value = null
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  acceptFile(input.files?.[0])
  input.value = ''
}

function onDrop(event: DragEvent) {
  dragging.value = false
  acceptFile(event.dataTransfer?.files?.[0])
}

async function runImport() {
  if (!file.value) {
    message.warning('请先选择文件')
    return
  }
  importing.value = true
  try {
    result.value = await importProducts(file.value)
    message.success(`导入完成：新增 ${result.value.created_count}，更新 ${result.value.updated_count}，失败 ${result.value.failed_count}`)
    emit('imported', result.value)
    await loadRecords()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '导入失败')
  } finally {
    importing.value = false
  }
}

async function downloadTemplate() {
  try {
    await downloadProductImportTemplate()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '模板下载失败')
  }
}

const records = ref<ProductImportRecord[]>([])
const recordsLoading = ref(false)
const recordsError = ref('')

async function loadRecords() {
  recordsLoading.value = true
  try {
    records.value = (await listProductImportRecords()).items
    recordsError.value = ''
  } catch (error) {
    recordsError.value = error instanceof Error ? error.message : '导入记录加载失败'
  } finally {
    recordsLoading.value = false
  }
}

async function downloadResult(record: ProductImportRecord) {
  try {
    await downloadProductImportResult(record.id, record.batch_no)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '结果下载失败')
  }
}

function formatDate(value: string | null | undefined) {
  if (!value) return '—'
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

// 切到「导入记录」就加载：产品页顶部页签、管理后台侧栏都只改 tab，不会重新挂载面板
watch(
  () => props.tab,
  (tab) => {
    if (tab === 'record') void loadRecords()
  },
  { immediate: true },
)

defineExpose({ loadRecords })
</script>

<template>
  <div class="import-panel">
    <div class="tab-bar" data-testid="import-tabs">
      <button type="button" class="tab" :class="{ active: tab === 'import' }" data-testid="import-tab-import" @click="emit('update:tab', 'import')">Excel 导入</button>
      <button type="button" class="tab" :class="{ active: tab === 'record' }" data-testid="import-tab-record" @click="tab === 'record' ? loadRecords() : emit('update:tab', 'record')">导入记录</button>
    </div>

    <div v-if="tab === 'import'" class="panel-body" data-testid="import-panel">
      <div
        class="drop-zone"
        :class="{ dragging }"
        data-testid="import-dropzone"
        @click="fileInput?.click()"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="dragging = false"
        @drop.prevent="onDrop"
      >
        <div class="drop-icon">↑</div>
        <h3>点击或拖拽上传 Excel 文件</h3>
        <p class="qc-muted">支持 .xlsx（也可用 .csv），单文件不超过 5MB</p>
        <input ref="fileInput" type="file" class="hidden-input" accept=".xlsx,.csv" data-testid="import-file" @change="onFileChange" />
        <button type="button" class="qc-btn qc-btn-primary" @click.stop="fileInput?.click()">选择文件</button>
      </div>

      <div class="field-notes">
        <div class="notes-title">导入字段说明</div>
        <div class="notes-grid">
          <div>• 产品编码（必填，不能和已有产品重复）</div>
          <div>• 产品名称（必填）</div>
          <template v-if="nav.isAdmin.value">
            <div v-if="canReadAny">• 所属公司编码（运营中心导入时填写；留空为共享产品）</div>
            <div v-else>• 所属公司编码（可不填，自动归本公司）</div>
            <div>• 所属部门（建议填写，Token 费用按部门核算）</div>
          </template>
          <div v-else>• 所属公司、所属部门不用填，导入后自动归本部门</div>
          <div>• 平台类型（Android / iOS / H5 / Web，多个用逗号分隔）</div>
          <div class="is-warning">• iOS 当前用于产品资料录入，自动质检能力待接入</div>
          <div>• 负责人、H5 地址、状态（启用 / 停用）为可选列</div>
        </div>
        <button type="button" class="qc-link template-link" data-testid="import-template" @click="downloadTemplate">下载导入模板</button>
      </div>

      <div v-if="file" class="upload-progress" data-testid="import-progress">
        <div class="progress-meta">
          <span class="file-name">{{ file.name }}</span>
          <span class="qc-muted">{{ importing ? '导入中…' : result ? '已完成' : '待导入' }}</span>
        </div>
        <div class="ap-progress"><i :style="{ width: result ? '100%' : importing ? '60%' : '0%' }"></i></div>
        <div class="progress-actions">
          <button type="button" class="qc-btn qc-btn-primary" :disabled="importing" data-testid="import-run" @click="runImport">
            {{ importing ? '导入中…' : '开始导入' }}
          </button>
          <button type="button" class="qc-btn" :disabled="importing" @click="file = null; result = null">移除文件</button>
        </div>
      </div>

      <div v-if="result" class="import-result" data-testid="import-result">
        <div class="result-summary">
          <span>批次 <strong>{{ result.batch_no || result.id || '—' }}</strong></span>
          <span>共 {{ result.total_rows }} 行</span>
          <span class="is-success">新增 {{ result.created_count }}</span>
          <span>更新 {{ result.updated_count }}</span>
          <span class="is-danger">失败 {{ result.failed_count }}</span>
        </div>
        <table v-if="result.errors.length" class="qc-table error-table">
          <thead>
            <tr><th>行号</th><th>错误原因</th></tr>
          </thead>
          <tbody>
            <tr v-for="item in result.errors" :key="item.row">
              <td>第 {{ item.row }} 行</td>
              <td>{{ item.message }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="qc-muted">全部行导入成功，成功行不会重复创建。</p>
      </div>
    </div>

    <div v-else class="panel-body" data-testid="import-records">
      <p v-if="recordsError" class="qc-error">{{ recordsError }}</p>
      <div class="ap-table-wrap">
        <table class="qc-table">
          <thead>
            <tr>
              <th>导入批次</th>
              <th>文件名</th>
              <th v-if="canReadAny">公司</th>
              <th>导入人</th>
              <th>导入时间</th>
              <th>成功 / 失败</th>
              <th>状态</th>
              <th class="is-right">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-if="!records.length">
              <td :colspan="canReadAny ? 8 : 7" class="qc-empty">{{ recordsLoading ? '加载中…' : recordsError ? '导入记录加载失败，请稍后重试' : '还没有导入记录' }}</td>
            </tr>
            <tr v-for="record in records" :key="record.id">
              <td><strong>{{ record.batch_no || record.id }}</strong></td>
              <td>{{ record.filename }}</td>
              <td v-if="canReadAny">{{ record.tenant_name || '—' }}</td>
              <td>{{ record.operator_name }}</td>
              <td class="qc-muted">{{ formatDate(record.created_at) }}</td>
              <td>
                <span class="is-success">{{ record.created_count + record.updated_count }}</span> /
                <span class="is-danger">{{ record.failed_count }}</span>
              </td>
              <td><span class="qc-chip tone-success">已完成</span></td>
              <td class="is-right">
                <button type="button" class="qc-link" :data-testid="`import-record-download-${record.id}`" @click="downloadResult(record)">下载结果</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.tab-bar {
  display: flex;
  border-bottom: 1px solid var(--workstation-border);
}

.tab {
  padding: 12px 20px;
  border: 0;
  border-bottom: 2px solid transparent;
  color: var(--workstation-text-muted);
  background: transparent;
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}

.tab.active {
  border-bottom-color: var(--workstation-primary);
  color: var(--workstation-primary);
}

.panel-body {
  padding: 24px;
}

.drop-zone {
  padding: 40px 20px;
  border: 2px dashed var(--workstation-border-strong);
  border-radius: 12px;
  text-align: center;
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.drop-zone:hover,
.drop-zone.dragging {
  border-color: var(--workstation-primary);
  background: var(--workstation-panel-soft);
}

.drop-icon {
  display: grid;
  place-items: center;
  width: 64px;
  height: 64px;
  margin: 0 auto 16px;
  border-radius: 50%;
  color: var(--workstation-primary);
  background: var(--tint-primary);
  font-size: 24px;
  font-weight: 700;
}

.drop-zone h3 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 16px;
  font-weight: 600;
}

.drop-zone p {
  margin: 4px 0 16px;
  font-size: 14px;
}

.hidden-input {
  display: none;
}

.field-notes {
  margin-top: 24px;
  padding: 16px;
  border-radius: 8px;
  background: var(--workstation-panel-soft);
  font-size: 14px;
}

.notes-title {
  margin-bottom: 8px;
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.notes-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 8px;
  color: var(--workstation-text-muted);
}

.notes-grid .is-warning {
  color: var(--workstation-warning);
}

.template-link {
  margin-top: 12px;
  font-weight: 500;
}

.upload-progress {
  margin-top: 24px;
}

.progress-meta {
  display: flex;
  justify-content: space-between;
  margin-bottom: 8px;
  font-size: 14px;
}

.file-name {
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.progress-actions {
  display: flex;
  gap: 8px;
  margin-top: 12px;
}

.import-result {
  margin-top: 24px;
}

.result-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 12px;
  font-size: 14px;
}

.is-success {
  color: var(--workstation-success-strong);
}

.is-danger {
  color: var(--workstation-danger);
}

.error-table th,
.error-table td {
  padding: 8px 12px;
}
</style>
