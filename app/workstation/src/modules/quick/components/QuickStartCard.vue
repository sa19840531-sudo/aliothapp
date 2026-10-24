<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { NIcon, useMessage } from 'naive-ui'
import { LayersOutline } from '@vicons/ionicons5'
import PageScopeMultiSelect from '@/components/pickers/PageScopeMultiSelect.vue'
import RulesetMultiSelect from '@/components/pickers/RulesetMultiSelect.vue'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import {
  allSelection,
  resolvePageScope,
  resolveRulesets,
  rulesetOptionLabel,
  rulesetSelectionError,
} from '@/components/pickers/selection'
import { uploadAppBuild, type AppBuildUploadResponse } from '@/services/app-builds.api'
import { getV2Capabilities } from '@/services/capabilities.api'
import { listWorkstationDevices, type WorkstationDeviceItem } from '@/services/devices.api'
import { listWorkstationProducts, type WorkstationProductOption } from '@/services/products.api'
import { getRuleCatalog, type RuleCatalog } from '@/services/ruleCatalog.api'
import { submitV2Task } from '@/services/tasks.api'
import { authStore } from '@/stores/auth'
import {
  DEFAULT_DEVICE_PROFILE,
  FALLBACK_DEPARTMENT,
  buildQuickPayload,
  defaultQuickTaskName,
  hostOf,
  matchProductByPackage,
  matchProductByUrl,
} from '@/modules/quick/quickStart'

/**
 * 快速开始质检（对齐 2.1 原型部门工作台）：上传解析与信息录入同步进行，
 * 选好规则集和页面范围、信息齐全后一键进入执行队列。复杂配置走「完整配置任务」。
 */
const props = withDefaults(
  defineProps<{
    /** 首页嵌入时收窄内边距 */
    embedded?: boolean
    /** 预选产品编码（从产品管理「发起质检」进入） */
    presetProductCode?: string | null
  }>(),
  { embedded: false, presetProductCode: null },
)

const emit = defineEmits<{
  submitted: [payload: { task_id: string; task_name: string }]
}>()

const message = useMessage()

type Mode = 'app' | 'url' | 'batch'
const mode = ref<Mode>('app')

const products = ref<WorkstationProductOption[]>([])
const productsLoading = ref(false)
const webEnabled = ref(false)
/** 部署能力还在查：Web 选项先说「加载中…」，不说「当前部署未开放」 */
const capabilitiesLoading = ref(true)
const devices = ref<WorkstationDeviceItem[]>([])
const devicesLoaded = ref(false)

const taskName = ref('')
const taskNameTouched = ref(false)
const productCode = ref('')
const targetUrl = ref('')
const urlPlatform = ref<'h5' | 'web'>('h5')

const appBuild = ref<AppBuildUploadResponse | null>(null)
const uploading = ref(false)
const uploadError = ref('')
const dragOver = ref(false)
const submitting = ref(false)

const selectedProduct = computed(
  () => products.value.find((item) => item.project_code === productCode.value) ?? null,
)

// ---------- 规则集与页面范围（来自已发布规则目录） ----------

type ScopePlatform = 'h5' | 'web' | 'app'

const catalog = ref<RuleCatalog | null>(null)
const catalogLoading = ref(true)
const catalogFailed = ref(false)
/** 规则集多选，两种入口共用，默认「全部」。 */
const rulesetSelection = ref<string[]>(allSelection())
/** 页面范围多选：各平台可选页面不同，分开记，默认都是「全部」。 */
const pageScope = reactive<Record<ScopePlatform, string[]>>({
  h5: allSelection(),
  web: allSelection(),
  app: allSelection(),
})

const rulesets = computed(() => catalog.value?.rulesets ?? [])
const pageTypes = computed(() => catalog.value?.page_types ?? [])
const currentPlatform = computed<ScopePlatform>(() =>
  mode.value === 'app' ? 'app' : urlPlatform.value,
)
const selectedRulesets = computed(() => resolveRulesets(rulesetSelection.value, rulesets.value))

function scopeCodes(platform: ScopePlatform): string[] {
  return catalog.value?.page_scope_options[platform] ?? []
}

/** 后端每个任务只锁一个规则集；目录没加载好时也不能提交。 */
const rulesetError = computed(() => {
  if (catalogLoading.value) return '规则集加载中'
  if (catalogFailed.value) return '规则集加载失败，请刷新页面后重试'
  return rulesetSelectionError(selectedRulesets.value, rulesets.value)
})

/** 提交用的页面范围：全部或都不选时为空，由系统自动识别。 */
const requiredPageTypes = computed(() =>
  resolvePageScope(pageScope[currentPlatform.value], scopeCodes(currentPlatform.value)),
)

const pageScopeText = computed(() =>
  requiredPageTypes.value.length ? `${requiredPageTypes.value.length} 类` : '全部可匹配页型',
)

function syncTaskName() {
  if (taskNameTouched.value) return
  taskName.value = defaultQuickTaskName({
    mode: mode.value,
    productName: selectedProduct.value?.product_name,
    build: appBuild.value,
    targetUrl: targetUrl.value,
    urlPlatform: urlPlatform.value,
  })
}

watch([mode, productCode, urlPlatform, appBuild], syncTaskName)

/** 最近一次由选中产品带出的地址：地址为空或还是它时才跟着换产品，用户自己填的地址不动。 */
let autoFilledUrl = ''

/** H5 / Web 地址：选了产品就带出它登记的地址；换成没登记地址的产品时清掉上次带出的。 */
function fillUrlFromProduct() {
  if (mode.value !== 'url') return
  const current = targetUrl.value.trim()
  if (current && current !== autoFilledUrl) return
  autoFilledUrl = selectedProduct.value?.h5_url?.trim() ?? ''
  targetUrl.value = autoFilledUrl
}

watch([mode, productCode], fillUrlFromProduct)

watch(targetUrl, (value) => {
  // 选产品带出的地址不再反过来匹配产品，免得同域名的另一个产品把刚选的顶掉
  const matched = value === autoFilledUrl ? null : matchProductByUrl(value, products.value)
  if (matched && productCode.value !== matched.project_code) {
    productCode.value = matched.project_code
  }
  syncTaskName()
})

async function loadProducts() {
  productsLoading.value = true
  try {
    const result = await listWorkstationProducts()
    products.value = result.items
    if (props.presetProductCode) {
      const preset = result.items.find((item) => item.project_code === props.presetProductCode)
      if (preset) {
        productCode.value = preset.project_code
        if (preset.h5_url) {
          mode.value = 'url'
          fillUrlFromProduct()
        }
      }
    } else if (result.items.length === 1) {
      productCode.value = result.items[0]!.project_code
    }
    syncTaskName()
  } catch (error) {
    message.error(error instanceof Error ? error.message : '产品列表加载失败')
  } finally {
    productsLoading.value = false
  }
}

async function loadCatalog() {
  try {
    catalog.value = await getRuleCatalog()
    catalogFailed.value = false
  } catch {
    catalogFailed.value = true
  } finally {
    catalogLoading.value = false
  }
}

async function loadCapabilities() {
  try {
    const capabilities = await getV2Capabilities()
    webEnabled.value = Boolean(
      capabilities.web?.enabled && capabilities.web.execution_modes.includes('full_inspection'),
    )
  } catch {
    webEnabled.value = false
  } finally {
    capabilitiesLoading.value = false
  }
}

async function loadDevices() {
  if (devicesLoaded.value) return
  try {
    const result = await listWorkstationDevices()
    devices.value = result.items
  } catch {
    devices.value = []
  } finally {
    devicesLoaded.value = true
  }
}

const availableDevice = computed(
  () =>
    devices.value.find(
      (item) =>
        item.enabled && item.occupancy_status === 'available' && item.backend === 'cloud_phone',
    ) ??
    devices.value.find((item) => item.enabled && item.backend === 'cloud_phone') ??
    null,
)

const deviceHint = computed(() => {
  if (!devicesLoaded.value) return '设备信息加载中'
  if (!devices.value.length) return `未读到设备库存，将使用默认设备 ${DEFAULT_DEVICE_PROFILE}`
  const device = availableDevice.value
  if (!device) return `设备全部占用，将使用默认设备 ${DEFAULT_DEVICE_PROFILE}`
  if (device.occupancy_status === 'available') return `将使用空闲设备 ${device.profile_id}`
  return `设备 ${device.profile_id} 当前${device.occupancy_status === 'occupied' ? '质检中' : '不可用'}，任务会排队等待`
})

async function handleFile(file: File | null | undefined) {
  if (!file) return
  if (!file.name.toLowerCase().endsWith('.apk')) {
    uploadError.value = '只支持 .apk 安装包'
    return
  }
  if (file.size > 500 * 1024 * 1024) {
    uploadError.value = 'APK 超过 500MB'
    return
  }
  uploadError.value = ''
  uploading.value = true
  try {
    const build = await uploadAppBuild(file)
    appBuild.value = build
    if (!productCode.value) {
      const matched = matchProductByPackage(build.package_name, products.value)
      if (matched) productCode.value = matched.project_code
    }
    syncTaskName()
    message.success('安装包已解析')
  } catch (error) {
    uploadError.value = error instanceof Error ? error.message : '安装包上传失败'
    appBuild.value = null
  } finally {
    uploading.value = false
  }
}

function onFileInput(event: Event) {
  const input = event.target as HTMLInputElement
  void handleFile(input.files?.[0])
  input.value = ''
}

function onDrop(event: DragEvent) {
  dragOver.value = false
  void handleFile(event.dataTransfer?.files?.[0])
}

const canSubmit = computed(() => {
  if (submitting.value || rulesetError.value) return false
  if (!taskName.value.trim() || !selectedProduct.value) return false
  if (mode.value === 'app') return Boolean(appBuild.value?.platform_build_ref) && !uploading.value
  if (mode.value === 'url') return Boolean(hostOf(targetUrl.value))
  return false
})

/** 缺什么先提示什么；信息齐全时回显本次的规则集和页面范围。 */
const submitHint = computed(() => {
  // 规则集目录加载失败时下拉里什么都没有，先把原因说出来
  if (catalogFailed.value) return '规则集加载失败，请刷新页面后重试'
  if (!selectedProduct.value) return '请选择匹配产品'
  if (mode.value === 'app' && !appBuild.value) return '请先上传安装包'
  if (mode.value === 'url' && !hostOf(targetUrl.value)) return '请填写完整地址（含 http/https）'
  if (!taskName.value.trim()) return '请填写任务名称'
  if (rulesetError.value) return rulesetError.value
  const picked = selectedRulesets.value
  if (!picked.length) return ''
  const rulesetText =
    picked.length === 1
      ? rulesetOptionLabel(picked[0])
      : `${picked.length} 个规则集 · ${picked.reduce((sum, item) => sum + item.enabled_rule_count, 0)} 条规则`
  return `已选择 ${rulesetText}，页面范围 ${pageScopeText.value}；提交后进入执行队列`
})

async function submit() {
  const rulesetCodes = selectedRulesets.value.map((item) => item.ruleset_code)
  if (!canSubmit.value || !selectedProduct.value || !rulesetCodes.length || mode.value === 'batch') return
  submitting.value = true
  try {
    const payload = buildQuickPayload({
      mode: mode.value,
      taskName: taskName.value,
      product: selectedProduct.value,
      departmentName: authStore.user.value?.department_name || FALLBACK_DEPARTMENT,
      rulesetCodes,
      requiredPageTypes: requiredPageTypes.value,
      targetUrl: targetUrl.value,
      urlPlatform: urlPlatform.value,
      build: appBuild.value,
      deviceProfileId: availableDevice.value?.profile_id ?? null,
    })
    const result = await submitV2Task(payload)
    message.success('已进入执行队列')
    emit('submitted', { task_id: result.task_id, task_name: payload.task_name })
    appBuild.value = null
    targetUrl.value = ''
    autoFilledUrl = ''
    taskNameTouched.value = false
    syncTaskName()
  } catch (error) {
    const detail = error instanceof Error && error.message ? error.message : '请稍后重试'
    message.error(`创建失败：${detail}`)
  } finally {
    submitting.value = false
  }
}

watch(mode, (value) => {
  if (value === 'app') void loadDevices()
})

onMounted(() => {
  void loadProducts()
  void loadCatalog()
  void loadCapabilities()
  void loadDevices()
})
</script>

<template>
  <section class="quick-card" :class="{ embedded }" data-testid="quick-start-card">
    <header class="quick-head">
      <strong>快速开始质检</strong>
      <span>上传解析与信息录入同步进行，信息齐全后进入执行队列。</span>
    </header>

    <div class="mode-tabs" role="tablist">
      <button
        type="button"
        role="tab"
        :aria-selected="mode === 'app'"
        :class="{ active: mode === 'app' }"
        @click="mode = 'app'"
      >
        App 安装包
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="mode === 'url'"
        :class="{ active: mode === 'url' }"
        data-testid="mode-url"
        @click="mode = 'url'"
      >
        H5 / Web 地址
      </button>
      <button
        type="button"
        role="tab"
        :aria-selected="mode === 'batch'"
        :class="{ active: mode === 'batch' }"
        @click="mode = 'batch'"
      >
        批量导入
      </button>
    </div>

    <!-- App 安装包：左侧拖拽上传，右侧同步补录 -->
    <div v-if="mode === 'app'" class="quick-body">
      <label
        class="drop-zone"
        :class="{ 'drag-over': dragOver, 'has-file': appBuild }"
        data-testid="apk-drop-zone"
        @dragover.prevent="dragOver = true"
        @dragleave.prevent="dragOver = false"
        @drop.prevent="onDrop"
      >
        <input type="file" accept=".apk" hidden data-testid="apk-input" @change="onFileInput" />
        <span class="drop-badge">APP</span>
        <template v-if="uploading">
          <strong>正在上传并解析安装包…</strong>
        </template>
        <template v-else-if="appBuild">
          <strong>{{ appBuild.original_filename }}</strong>
          <span>
            {{ appBuild.package_name || '未解析出包名' }}
            <template v-if="appBuild.version_name"> · 版本 {{ appBuild.version_name }}</template>
            · {{ (appBuild.size_bytes / 1024 / 1024).toFixed(1) }} MB
          </span>
          <em>点击可重新选择</em>
        </template>
        <template v-else>
          <strong>拖拽 APK 到这里，或点击选择文件</strong>
          <span>APK 最大 500MB；iOS 产品资料可录入，IPA 自动质检能力待接入</span>
        </template>
        <p v-if="uploadError" class="error-text">{{ uploadError }}</p>
      </label>

      <div class="quick-right">
        <label class="field">
          <span>任务名称</span>
          <input
            v-model="taskName"
            type="text"
            maxlength="120"
            placeholder="上传后自动生成，可修改"
            data-testid="task-name"
            @input="taskNameTouched = true"
          />
        </label>
        <label class="field">
          <span>匹配产品</span>
          <SearchSelect v-model="productCode" data-testid="product-select" :disabled="productsLoading">
            <option value="">{{ productsLoading ? '加载中…' : '自动匹配产品' }}</option>
            <option v-for="item in products" :key="item.project_code" :value="item.project_code">
              {{ item.product_name }}（{{ item.project_code }}）
            </option>
          </SearchSelect>
        </label>
        <div class="field">
          <span>规则集</span>
          <RulesetMultiSelect
            v-model="rulesetSelection"
            :rulesets="rulesets"
            :loading="catalogLoading"
            data-testid="quick-ruleset"
          />
        </div>
        <div class="field">
          <span>页面范围</span>
          <PageScopeMultiSelect
            v-model="pageScope.app"
            :codes="scopeCodes('app')"
            :page-types="pageTypes"
            :loading="catalogLoading"
            data-testid="quick-page-scope"
          />
        </div>
        <p class="hint">{{ deviceHint }}</p>
        <button
          type="button"
          class="primary-btn"
          :disabled="!canSubmit"
          data-testid="quick-submit"
          @click="submit"
        >
          {{ submitting ? '提交中…' : '解析后进入队列' }}
        </button>
        <p v-if="submitHint" class="hint" data-testid="quick-hint">{{ submitHint }}</p>
      </div>
    </div>

    <!-- H5 / Web 地址：地址 + 规则集 + 页面范围 + 识别并创建任务 -->
    <div v-else-if="mode === 'url'" class="url-body">
      <div class="url-row">
        <label class="field">
          <span>页面地址</span>
          <input
            v-model="targetUrl"
            type="url"
            placeholder="输入 H5 或 Web 页面地址"
            data-testid="target-url"
          />
        </label>
        <div class="field">
          <span>规则集</span>
          <RulesetMultiSelect
            v-model="rulesetSelection"
            :rulesets="rulesets"
            :loading="catalogLoading"
            data-testid="quick-ruleset"
          />
        </div>
        <div class="field">
          <span>页面范围</span>
          <PageScopeMultiSelect
            v-model="pageScope[urlPlatform]"
            :codes="scopeCodes(urlPlatform)"
            :page-types="pageTypes"
            :loading="catalogLoading"
            data-testid="quick-page-scope"
          />
        </div>
        <button
          type="button"
          class="primary-btn"
          :disabled="!canSubmit"
          data-testid="quick-submit"
          @click="submit"
        >
          {{ submitting ? '提交中…' : '识别并创建任务' }}
        </button>
      </div>
      <div class="url-details">
        <div class="field">
          <span>平台</span>
          <div class="platform-row">
            <label><input v-model="urlPlatform" type="radio" value="h5" /> H5（手机视口）</label>
            <label :class="{ disabled: !webEnabled }">
              <input v-model="urlPlatform" type="radio" value="web" :disabled="!webEnabled" />
              Web（桌面视口）
              <em v-if="capabilitiesLoading">加载中…</em>
              <em v-else-if="!webEnabled">当前部署未开放</em>
            </label>
          </div>
        </div>
        <label class="field">
          <span>任务名称</span>
          <input
            v-model="taskName"
            type="text"
            maxlength="120"
            placeholder="填写地址后自动生成，可修改"
            data-testid="task-name"
            @input="taskNameTouched = true"
          />
        </label>
        <label class="field">
          <span>匹配产品</span>
          <SearchSelect v-model="productCode" data-testid="product-select" :disabled="productsLoading">
            <option value="">{{ productsLoading ? '加载中…' : '自动匹配产品' }}</option>
            <option v-for="item in products" :key="item.project_code" :value="item.project_code">
              {{ item.product_name }}（{{ item.project_code }}）
            </option>
          </SearchSelect>
        </label>
      </div>
      <p class="hint">地址主域与产品登记的 H5 地址一致时会自动匹配产品。</p>
      <p v-if="submitHint" class="hint" data-testid="quick-hint">{{ submitHint }}</p>
    </div>

    <!-- 批量导入 -->
    <div v-else class="batch-body">
      <div class="batch-zone">
        <n-icon size="30" class="drop-icon"><LayersOutline /></n-icon>
        <strong>批量质检即将支持</strong>
        <span>一次上传多个 APK 或使用 Excel 录入多个 H5 / Web 地址，系统将创建批次及独立子任务。</span>
      </div>
    </div>
  </section>
</template>

<style scoped>
/* 原型：bg-white rounded-xl border-slate-200 shadow-sm overflow-visible（下拉多选要能盖出卡片）；头部 px-5 py-4 border-b；主体 p-5 */
.quick-card {
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  background: var(--workstation-panel);
  box-shadow: var(--workstation-shadow);
}

.quick-head {
  padding: 16px 20px;
  border-bottom: 1px solid var(--workstation-border);
}

.quick-head strong {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 14px;
  font-weight: 600;
}

.quick-head span {
  display: block;
  margin-top: 4px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.mode-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 20px 20px 0;
}

.mode-tabs button {
  padding: 8px 16px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text);
  background: var(--workstation-panel);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}

.mode-tabs button.active {
  border-color: var(--workstation-primary);
  color: #ffffff;
  background: var(--workstation-primary);
}

.quick-body {
  display: grid;
  grid-template-columns: 1.15fr 0.85fr;
  gap: 20px;
  padding: 16px 20px 20px;
}

/* 拖拽区：min-h-[230px] rounded-xl border-2 dashed slate-300 bg-slate-50 */
.drop-zone {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  min-height: 230px;
  height: 100%;
  padding: 32px;
  border: 2px dashed #cbd5e1;
  border-radius: var(--workstation-radius);
  background: var(--workstation-panel-soft);
  text-align: center;
  cursor: pointer;
  transition: border-color 0.15s ease;
}

.drop-zone:hover,
.drop-zone.drag-over {
  border-color: #60a5fa;
}

.drop-zone.has-file {
  border-color: var(--workstation-success);
  border-style: solid;
}

.drop-badge {
  display: grid;
  place-items: center;
  width: 48px;
  height: 48px;
  border-radius: var(--workstation-radius);
  color: var(--workstation-primary);
  background: var(--tint-primary);
  font-size: 18px;
  font-weight: 700;
}

.drop-zone strong {
  margin-top: 16px;
  color: var(--workstation-text);
  font-size: 16px;
  font-weight: 500;
}

.drop-zone span {
  margin-top: 8px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.drop-zone em {
  margin-top: 8px;
  color: var(--workstation-primary);
  font-size: 12px;
  font-style: normal;
}

.error-text {
  margin: 8px 0 0;
  color: var(--workstation-danger);
  font-size: 12px;
}

/* H5 / Web：原型 grid-cols-[1fr_220px_260px_auto]，地址、规则集、页面范围、按钮一行 */
.url-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 20px 20px;
}

.url-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 220px 260px auto;
  align-items: end;
  gap: 12px;
}

/* 与同一行的输入框等高 */
.url-row .primary-btn {
  width: auto;
  padding: 8px 20px;
  white-space: nowrap;
}

.url-details {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) minmax(0, 1fr);
  align-items: end;
  gap: 12px 16px;
}

.batch-body {
  padding: 16px 20px 20px;
}

.batch-zone {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 12px;
  min-height: 200px;
  padding: 24px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  background: var(--workstation-panel-soft);
  text-align: center;
}

.drop-icon {
  color: var(--workstation-primary);
}

.batch-zone strong {
  color: var(--workstation-text);
  font-size: 14px;
  font-weight: 500;
}

.batch-zone span {
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.field {
  display: block;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.field > span {
  display: block;
}

/* 单选框不套输入框样式：宽 100% 会把单选框撑宽，旁边的文字被挤成逐字换行 */
.field input:not([type='radio']) {
  width: 100%;
  margin-top: 4px;
  padding: 8px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text);
  background: var(--workstation-input-bg);
  font: inherit;
  font-size: 14px;
}

.field input:not([type='radio']):focus {
  border-color: var(--workstation-primary);
  outline: 0;
  box-shadow: 0 0 0 2px rgba(37, 99, 235, 0.18);
}

.platform-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 14px;
  min-height: 38px;
  margin-top: 4px;
  font-size: 13px;
  color: var(--workstation-text);
}

.platform-row label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  white-space: nowrap;
}

.platform-row label.disabled {
  color: var(--workstation-text-faint);
}

.platform-row em {
  color: var(--workstation-text-faint);
  font-size: 12px;
  font-style: normal;
}

.hint {
  margin: 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.quick-right {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 规则集 / 页面范围 / 产品选择器与输入框同高、同间距 */
.field > .multi-select,
.field > .search-select {
  margin-top: 4px;
}

.primary-btn {
  width: 100%;
  padding: 10px 16px;
  border: 1px solid var(--workstation-primary);
  border-radius: var(--workstation-radius-sm);
  color: #ffffff;
  background: var(--workstation-primary);
  font: inherit;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
}

.primary-btn:hover:not(:disabled) {
  border-color: var(--workstation-primary-strong);
  background: var(--workstation-primary-strong);
}

.primary-btn:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

@media (max-width: 1024px) {
  .quick-body,
  .url-row,
  .url-details {
    grid-template-columns: 1fr;
  }

  .url-row .primary-btn {
    width: 100%;
  }
}
</style>
