<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { NSelect, useMessage } from 'naive-ui'
import PageScopeMultiSelect from '@/components/pickers/PageScopeMultiSelect.vue'
import RulesetMultiSelect from '@/components/pickers/RulesetMultiSelect.vue'
import H5ShortflowSelect from '@/modules/tasks/components/H5ShortflowSelect.vue'
import type { H5ShortflowSelection } from '@/services/h5-shortflows.api'
import {
  allSelection,
  resolvePageScope,
  resolveRulesets,
  rulesOfRulesets,
  rulesetSelectionError,
} from '@/components/pickers/selection'
import { uploadAppBuild, type AppBuildUploadResponse } from '@/services/app-builds.api'
import {
  getV2Capabilities,
  type V2CapabilitiesResponse,
} from '@/services/capabilities.api'
import {
  deviceOccupancyLabel,
  formatDeviceOptionLabel,
  listWorkstationDevices,
  type WorkstationDeviceItem,
} from '@/services/devices.api'
import { getRuleCatalog, type RuleCatalog } from '@/services/ruleCatalog.api'
import { submitExplorationTask, submitV2Task } from '@/services/tasks.api'
import { listWorkstationProducts, type WorkstationProductOption } from '@/services/products.api'
import { createTaskDraft, deleteTaskDraft, listTaskDrafts, type TaskDraft } from '@/services/task-drafts.api'
import { authStore } from '@/stores/auth'
import type {
  TaskChannel,
  V2TaskCreatePayload,
  V2TaskExecutionMode,
  V2TaskPlatform,
  V2TaskTarget,
} from '@/types/task'
import { categoryLabel } from '@/utils/rule-labels'

/**
 * 创建任务，对齐 2.1 原型 task-create.html：顶部平台胶囊（H5 / Web / Android / iOS 待接入），
 * 任务名称 + 产品，然后按平台展开配置。公司和部门由登录账号决定，页面上不再展示。
 * H5 / Web 填页面地址、规则集和页面范围，底部固定 保存草稿 / 取消 / 立即发起质检；
 * Android 走三步向导（基础信息 / 设备配置 / 巡检确认），在第三步「开始巡检」，不显示底部操作栏。
 */
const route = useRoute()
const nav = usePortalNav()
const message = useMessage()
const submitting = ref(false)
const h5Shortflows = ref<H5ShortflowSelection[]>([])

// 固定执行参数，不再暴露给用户配置
const FIXED_CHANNEL: TaskChannel = 'workbench'
const FIXED_PRIORITY = 100

/** 设备能力模板「使用默认模板」：不传给后端，产品登记了专属模板时由后端套用。 */
const DEFAULT_CAPABILITY_TEMPLATE = 'default'

const PAGE_SCOPE_HINT = '默认选择全部，可按需取消页面；只选部分页面时，这几类页面都必须巡检到'

/** 任务部门：优先产品资料上的部门，其次账号所属部门；部门账号在服务端会按登录身份改写。 */
function departmentFor(product: { department_name?: string | null }) {
  return product.department_name || authStore.user.value?.department_name || '-'
}

const initialPlatform: V2TaskPlatform =
  route.query.platform === 'app' || route.query.platform === 'web'
    ? route.query.platform
    : 'h5'
const formValue = reactive({
  task_name: '',
  execution_mode: 'full_inspection' as V2TaskExecutionMode,
  target_platform: initialPlatform as V2TaskPlatform,
  target_url: '',
  web_target_url: '',
  package_name: '',
  device_backend: 'cloud_phone',
  // 本机联调先锁定 qa-b（CP-2），避免抢观星占用的 qa-a。
  device_profile_id: 'qa-b',
  device_capability_template: DEFAULT_CAPABILITY_TEMPLATE as
    | typeof DEFAULT_CAPABILITY_TEMPLATE
    | 'android_visual_readonly_v1'
    | 'android_visual_evidence_v1',
  /** 规则集多选，默认「全部」；三个平台共用 */
  ruleset_selection: allSelection(),
  /** 页面范围多选，默认「全部」；各平台可选页面不同，分开记 */
  page_scope: {
    h5: allSelection(),
    web: allSelection(),
    app: allSelection(),
  } as Record<V2TaskPlatform, string[]>,
  remark: '',
})

const selectedFile = ref<File | null>(null)
const appBuild = ref<AppBuildUploadResponse | null>(null)
const uploading = ref(false)
const fileInputRef = ref<HTMLInputElement | null>(null)

const products = ref<WorkstationProductOption[]>([])
const productsLoading = ref(false)
const selectedProductCode = ref<string | null>(null)
const devices = ref<WorkstationDeviceItem[]>([])
const devicesLoading = ref(false)
const devicesError = ref('')
const capabilities = ref<V2CapabilitiesResponse | null>(null)
/** 部署能力还在查：Web 先说「能力加载中」，不说「能力未开启」 */
const capabilitiesLoading = ref(true)
let devicesPollTimer: ReturnType<typeof setInterval> | null = null

const productOptions = computed(() =>
  products.value.map((product) => ({
    label: `${product.project_code} · ${product.product_name}`,
    value: product.project_code,
  })),
)

const webCapability = computed(() => capabilities.value?.web ?? null)
const webEnabled = computed(
  () =>
    webCapability.value?.enabled === true &&
    webCapability.value.execution_modes.includes('exploration_only'),
)
const preferredWebExecutionMode = computed<V2TaskExecutionMode>(() =>
  webCapability.value?.execution_modes.includes('full_inspection')
    ? 'full_inspection'
    : 'exploration_only',
)
const webBrowserProfile = computed(() => {
  const capability = webCapability.value
  if (!capability) return null
  return (
    capability.browser_profiles.find(
      (profile) => profile.profile_id === capability.default_browser_profile_id,
    ) ?? null
  )
})

/** 平台胶囊：原型只显示短名，完整说明放 title。 */
const platformOptions = computed<
  Array<{ label: string; short: string; value: V2TaskPlatform; disabled?: boolean }>
>(() => [
  { label: 'H5 移动页面', short: 'H5', value: 'h5' },
  {
    label: webEnabled.value
      ? 'Web 桌面页面'
      : capabilitiesLoading.value
        ? 'Web 桌面页面（能力加载中…）'
        : 'Web 桌面页面（能力未开启）',
    short: 'Web',
    value: 'web',
    disabled: !webEnabled.value,
  },
  { label: 'Android App', short: 'Android', value: 'app' },
])

/** iOS 只登记产品资料，自动质检待接入：可以点开看说明，但不能提交。 */
const iosSelected = ref(false)

const deviceCapabilityTemplateOptions = [
  { label: '使用默认模板', value: DEFAULT_CAPABILITY_TEMPLATE },
  { label: '标准模板（截图、页面结构和基础操作）', value: 'android_visual_readonly_v1' },
  { label: '增强模板（额外留存页面文字、快照和设备日志）', value: 'android_visual_evidence_v1' },
]

// 与 App 质检 API 契约的 device.backend 枚举保持一致
const deviceBackendOptions = [
  { label: '云手机（MoreLogin）', value: 'cloud_phone' },
  { label: '雷电模拟器', value: 'ldplayer' },
  { label: '谷歌官方模拟器', value: 'google_emulator' },
  { label: '容器安卓（Redroid）', value: 'redroid' },
]

const devicesForBackend = computed(() =>
  devices.value.filter((item) => item.backend === formValue.device_backend),
)

const deviceProfileOptions = computed(() =>
  devicesForBackend.value.map((item) => ({
    label: formatDeviceOptionLabel(item),
    value: item.profile_id,
    disabled: item.occupancy_status === 'disabled' || item.occupancy_status === 'quarantined',
  })),
)

const selectedDevice = computed(
  () =>
    devicesForBackend.value.find((item) => item.profile_id === formValue.device_profile_id) ?? null,
)

const selectedProduct = computed(
  () => products.value.find((product) => product.project_code === selectedProductCode.value) ?? null,
)

const isExplorationOnly = computed(() => formValue.execution_mode === 'exploration_only')
watch(() => [formValue.target_platform, formValue.execution_mode], () => { h5Shortflows.value = [] })

// ---------- 规则集与页面范围（来自已发布规则目录） ----------

const catalog = ref<RuleCatalog | null>(null)
const catalogLoading = ref(true)
const catalogFailed = ref(false)

const rulesets = computed(() => catalog.value?.rulesets ?? [])
const pageTypes = computed(() => catalog.value?.page_types ?? [])
const selectedRulesets = computed(() =>
  resolveRulesets(formValue.ruleset_selection, rulesets.value),
)
/** 所选规则集里的启用规则（去重、按目录顺序）；本期全部执行，暂不支持逐条取消。 */
const selectedRules = computed(() =>
  rulesOfRulesets(selectedRulesets.value, catalog.value?.rules ?? []),
)

function pageScopeCodes(platform: V2TaskPlatform): string[] {
  return catalog.value?.page_scope_options[platform] ?? []
}

/** 当前平台要提交的页面范围：选全部或都不选为空数组，由系统自动识别页面。 */
const requiredPageTypes = computed(() =>
  resolvePageScope(
    formValue.page_scope[formValue.target_platform],
    pageScopeCodes(formValue.target_platform),
  ),
)

async function loadCatalog() {
  catalogLoading.value = true
  try {
    catalog.value = await getRuleCatalog()
    catalogFailed.value = false
  } catch (error) {
    catalogFailed.value = true
    message.error(error instanceof Error ? error.message : '规则集加载失败')
  } finally {
    catalogLoading.value = false
  }
}

/** 规则集可以多选、一起执行（「全部」即全部开启的规则集）；逐条去掉规则暂不支持。 */
function rulesetError(): string | null {
  if (catalogLoading.value) return '规则集还在加载，请稍后再试'
  if (catalogFailed.value) return '规则集加载失败，请刷新页面后重试'
  return rulesetSelectionError(selectedRulesets.value, rulesets.value)
}

function handleProductChange(code: string) {
  selectedProductCode.value = code
  if (formValue.target_platform === 'h5') {
    formValue.target_url =
      products.value.find((product) => product.project_code === code)?.h5_url ?? ''
  }
}

function handlePlatformChange(platform: V2TaskPlatform) {
  if (platform === 'web' && !webEnabled.value) {
    message.warning('Web 探索能力尚未开启')
    return
  }
  iosSelected.value = false
  formValue.target_platform = platform
  if (platform === 'web') {
    formValue.execution_mode = preferredWebExecutionMode.value
  }
  if (platform === 'h5') {
    formValue.target_url = selectedProduct.value?.h5_url ?? formValue.target_url
    stopDevicesPolling()
  } else if (platform === 'web') {
    stopDevicesPolling()
  } else {
    void loadDevices()
    startDevicesPolling()
  }
}

function selectIos() {
  iosSelected.value = true
  stopDevicesPolling()
}

function openFilePicker() {
  fileInputRef.value?.click()
}

function onFileChange(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0] ?? null
  selectedFile.value = file
  appBuild.value = null
  if (file) {
    void handleUpload(file)
  }
}

async function handleUpload(file: File) {
  if (!file.name.toLowerCase().endsWith('.apk')) {
    message.error('请上传 .apk 文件')
    selectedFile.value = null
    return
  }
  uploading.value = true
  try {
    const result = await uploadAppBuild(file)
    appBuild.value = result
    if (result.package_name && !formValue.package_name.trim()) {
      formValue.package_name = result.package_name
    }
    message.success('APK 上传并校验成功')
  } catch (error) {
    appBuild.value = null
    message.error(error instanceof Error ? error.message : 'APK 上传失败')
  } finally {
    uploading.value = false
  }
}

async function loadProducts() {
  productsLoading.value = true
  try {
    const result = await listWorkstationProducts()
    products.value = result.items
  } catch (error) {
    message.error(error instanceof Error ? error.message : '产品列表加载失败')
  } finally {
    productsLoading.value = false
  }
}

function syncSelectedDeviceProfile() {
  const matched = devicesForBackend.value
  if (!matched.length) {
    return
  }
  if (matched.some((item) => item.profile_id === formValue.device_profile_id)) {
    return
  }
  const preferred =
    matched.find((item) => item.occupancy_status === 'available') ?? matched[0]
  formValue.device_profile_id = preferred.profile_id
}

async function loadDevices(options: { silent?: boolean } = {}) {
  if (!options.silent) {
    devicesLoading.value = true
  }
  try {
    const result = await listWorkstationDevices()
    devices.value = result.items
    devicesError.value = ''
    syncSelectedDeviceProfile()
  } catch (error) {
    devicesError.value = error instanceof Error ? error.message : '设备列表加载失败'
    if (!options.silent) {
      message.error(devicesError.value)
    }
  } finally {
    devicesLoading.value = false
  }
}

function stopDevicesPolling() {
  if (devicesPollTimer != null) {
    clearInterval(devicesPollTimer)
    devicesPollTimer = null
  }
}

function startDevicesPolling() {
  stopDevicesPolling()
  devicesPollTimer = setInterval(() => {
    if (formValue.target_platform === 'app') {
      void loadDevices({ silent: true })
    }
  }, 8000)
}

async function loadCapabilities() {
  try {
    capabilities.value = await getV2Capabilities()
    if (formValue.target_platform === 'web') {
      if (webEnabled.value) {
        formValue.execution_mode = preferredWebExecutionMode.value
      } else {
        formValue.target_platform = 'h5'
      }
    }
  } catch {
    capabilities.value = null
    if (formValue.target_platform === 'web') {
      formValue.target_platform = 'h5'
    }
  } finally {
    capabilitiesLoading.value = false
  }
}

// ---------- Android 三步向导（基础信息 / 设备配置 / 巡检确认） ----------

const STEPS = [
  { key: 1, label: '基础信息' },
  { key: 2, label: '设备配置' },
  { key: 3, label: '巡检确认' },
] as const

const currentStep = ref(1)
/** 设备配置默认折叠成「已选择 N 个设备配置」摘要，要换设备再展开。 */
const devicePickerOpen = ref(false)

function validateStep(step: number): string | null {
  if (step === 1) {
    if (!appBuild.value?.platform_build_ref) return '请先上传并通过校验的 APK'
    if (!formValue.package_name.trim()) return '请填写 Android 包名'
  }
  if (step === 2) {
    if (!formValue.device_backend.trim()) return '请选择设备后端'
    if (!formValue.device_profile_id.trim()) return '请选择设备配置'
  }
  if (step === 3) {
    const error = rulesetError()
    if (error) return error
    // 仅页面探索不执行规则，不要求规则清单非空
    if (!isExplorationOnly.value && !selectedRules.value.length) {
      return '所选规则集里没有可用的规则，请换一个规则集'
    }
  }
  return null
}

function nextStep() {
  const error = validateStep(currentStep.value)
  if (error) {
    message.error(error)
    return
  }
  currentStep.value = Math.min(STEPS.length, currentStep.value + 1)
}

function prevStep() {
  currentStep.value = Math.max(1, currentStep.value - 1)
}

/** 往前跳直接放行；往后跳要逐步通过校验，停在第一个没填好的步骤。 */
function jumpToStep(step: number) {
  if (step <= currentStep.value) {
    currentStep.value = step
    return
  }
  for (let candidate = currentStep.value; candidate < step; candidate += 1) {
    const error = validateStep(candidate)
    if (error) {
      currentStep.value = candidate
      message.error(error)
      return
    }
  }
  currentStep.value = step
}

const deviceBackendLabel = computed(
  () =>
    deviceBackendOptions.find((option) => option.value === formValue.device_backend)?.label ??
    formValue.device_backend,
)

const deviceSummaryText = computed(() => {
  const device = selectedDevice.value
  return `${deviceBackendLabel.value} · ${formValue.device_profile_id || '未选择'}${
    device ? ` · ${deviceOccupancyLabel(device)}` : ''
  }`
})

/** 巡检确认的四个汇总块（原型：巡检页面 / 质检规则 / 设备 / 参数方案）。 */
const confirmTiles = computed(() => {
  const pageCount = requiredPageTypes.value.length
  let ruleText = catalogLoading.value ? '加载中…' : '待选择'
  if (isExplorationOnly.value) {
    ruleText = '不执行'
  } else if (selectedRulesets.value.length) {
    ruleText = `${selectedRules.value.length} 条`
  }
  return [
    { label: '巡检页面', value: pageCount ? `${pageCount} 类` : '自动识别' },
    { label: '质检规则', value: ruleText },
    { label: '设备', value: formValue.device_profile_id.trim() ? '1 个' : '未选择' },
    { label: '参数方案', value: '默认参数' },
  ]
})

// ---------- 保存草稿 / 草稿箱（服务端按账号保存） ----------

const drafts = ref<TaskDraft[]>([])
const draftsOpen = ref(false)
const savingDraft = ref(false)

async function loadDrafts() {
  try {
    drafts.value = (await listTaskDrafts()).items
  } catch {
    drafts.value = []
  }
}

async function saveDraft() {
  savingDraft.value = true
  try {
    const draft = await createTaskDraft({
      name: formValue.task_name.trim() || `未命名草稿 ${new Date().toLocaleString('zh-CN', { hour12: false })}`,
      target_platform: formValue.target_platform,
      payload: {
        form: { ...formValue },
        product_code: selectedProductCode.value,
        current_step: currentStep.value,
        h5_shortflows: h5Shortflows.value,
      },
    })
    message.success(`草稿已保存：${draft.name}`)
    await loadDrafts()
    draftsOpen.value = true
  } catch (error) {
    message.error(error instanceof Error ? error.message : '草稿保存失败')
  } finally {
    savingDraft.value = false
  }
}

/** 草稿里的多选值必须是字符串数组；旧草稿没有这两项时回到默认「全部」。 */
function savedSelection(value: unknown): string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
    ? [...value]
    : allSelection()
}

function restoreDraft(draft: TaskDraft) {
  h5Shortflows.value = []
  const saved = (draft.payload.form ?? {}) as Record<string, unknown>
  const platform = draft.target_platform
  if (platform === 'web' && capabilitiesLoading.value) {
    message.info('部署能力还在加载，请稍后再恢复这份 Web 草稿')
    return
  }
  if (platform === 'web' && !webEnabled.value) {
    message.warning('草稿是 Web 任务，但当前部署未开放 Web 能力')
  } else {
    handlePlatformChange(platform)
  }
  // 旧草稿里的用户旅程、要求页面类型、最低页数等字段已不再使用，按当前表单字段逐项恢复
  for (const key of Object.keys(formValue) as Array<keyof typeof formValue>) {
    if (key === 'ruleset_selection' || key === 'page_scope') continue
    const value = saved[key]
    if (value !== undefined && value !== null) {
      ;(formValue as Record<string, unknown>)[key] = value
    }
  }
  formValue.ruleset_selection = savedSelection(saved.ruleset_selection)
  const savedScope = (saved.page_scope ?? {}) as Record<string, unknown>
  for (const key of ['h5', 'web', 'app'] as const) {
    formValue.page_scope[key] = savedSelection(savedScope[key])
  }
  const productCode = draft.payload.product_code
  if (typeof productCode === 'string' && productCode) {
    selectedProductCode.value = productCode
  }
  const step = Number(draft.payload.current_step)
  currentStep.value = Number.isInteger(step) && step >= 1 && step <= STEPS.length ? step : 1
  draftsOpen.value = false
  // 草稿不是冻结版本来源，短流程必须重新从当前已发布目录选择。
  if (Array.isArray(draft.payload.h5_shortflows) && draft.payload.h5_shortflows.length) {
    message.warning('草稿已恢复，短流程请按当前已发布版本重新选择')
  }
  message.info(platform === 'app' ? '已恢复草稿，APK 需要重新上传' : '已恢复草稿')
}

async function removeDraft(draft: TaskDraft) {
  try {
    await deleteTaskDraft(draft.id)
    drafts.value = drafts.value.filter((item) => item.id !== draft.id)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '草稿删除失败')
  }
}

function formatDraftTime(value: string) {
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function cancelCreate() {
  void nav.push('/tasks')
}

/** 提交前逐项校验；Android 按步骤校验，并停在第一个没填好的步骤。 */
function validateBeforeSubmit(): string | null {
  if (!formValue.task_name.trim()) return '请填写任务名称'
  if (!selectedProduct.value) return '请选择产品'
  const platform = formValue.target_platform
  if (platform === 'app') {
    for (const step of STEPS) {
      const error = validateStep(step.key)
      if (error) {
        currentStep.value = step.key
        return error
      }
    }
    return null
  }
  if (
    platform === 'web' &&
    (!webEnabled.value || !webCapability.value?.execution_modes.includes(formValue.execution_mode))
  ) {
    return '当前部署未开放该 Web 执行模式'
  }
  const url = platform === 'web' ? formValue.web_target_url : formValue.target_url
  if (!url.trim()) return platform === 'web' ? '请填写 Web 页面地址' : '请填写 H5 页面地址'
  return rulesetError()
}

async function handleSubmit() {
  if (iosSelected.value) {
    message.warning('iOS 自动质检能力待接入，暂时不能发起')
    return
  }
  const error = validateBeforeSubmit()
  const product = selectedProduct.value
  const rulesetCodes = selectedRulesets.value.map((item) => item.ruleset_code)
  if (error || !product || !rulesetCodes.length) {
    message.error(error ?? '请选择规则集')
    return
  }

  // 页面范围：选全部时发空数组由系统自动识别，只选部分时这几类页面必须巡检到
  const explorationTarget = { required_page_types: requiredPageTypes.value }
  let target: V2TaskTarget
  if (formValue.target_platform === 'app') {
    const template = formValue.device_capability_template
    target = {
      platform: 'app',
      source: { type: 'build_ref', platform_build_ref: appBuild.value!.platform_build_ref },
      package_name: formValue.package_name.trim(),
      device: {
        backend: formValue.device_backend.trim(),
        profile_id: formValue.device_profile_id.trim(),
      },
      // 不再提交用户旅程、里程碑和最低页数；模板选默认时不传，由后端按产品画像决定
      exploration_target:
        template === DEFAULT_CAPABILITY_TEMPLATE
          ? explorationTarget
          : { ...explorationTarget, device_capability_template: template },
    }
  } else if (formValue.target_platform === 'web') {
    const url = formValue.web_target_url.trim()
    if (isExplorationOnly.value) {
      const browserProfileId = webCapability.value?.default_browser_profile_id
      if (!browserProfileId || !webBrowserProfile.value) {
        message.error('桌面浏览器环境还没准备好，请稍后再试')
        return
      }
      target = {
        platform: 'web',
        url,
        browser_profile_id: browserProfileId,
        exploration_target: explorationTarget,
      }
    } else {
      target = { platform: 'web', url, exploration_target: explorationTarget }
    }
  } else {
    target = {
      platform: 'h5',
      url: formValue.target_url.trim(),
      exploration_target: explorationTarget,
    }
  }

  const payload: V2TaskCreatePayload = {
    task_name: formValue.task_name.trim(),
    target,
    ruleset_codes: rulesetCodes,
    channel: FIXED_CHANNEL,
    priority: FIXED_PRIORITY,
    product: {
      department_name: departmentFor(product),
      product_code: product.project_code,
      product_name: product.product_name,
    },
    scan_mode: 'full',
    remark: formValue.remark.trim() || null,
    ...(formValue.target_platform === 'h5' && !isExplorationOnly.value && h5Shortflows.value.length
      ? { h5_shortflows: h5Shortflows.value } : {}),
  }

  submitting.value = true
  try {
    const result = isExplorationOnly.value
      ? await submitExplorationTask(payload)
      : await submitV2Task(payload)
    message.success(isExplorationOnly.value ? '页面探索任务已提交' : '质检任务已提交')
    void nav.push(`/tasks/${result.task_id}`)
  } catch (error) {
    const detail = error instanceof Error && error.message ? error.message : '请稍后重试'
    message.error(`任务提交失败：${detail}`)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  void loadCapabilities()
  void loadProducts()
  void loadCatalog()
  void loadDrafts()
  if (formValue.target_platform === 'app') {
    void loadDevices()
    startDevicesPolling()
  }
})

onBeforeUnmount(() => {
  stopDevicesPolling()
})

watch(
  () => formValue.device_backend,
  () => {
    syncSelectedDeviceProfile()
  },
)
</script>

<template>
  <div class="workstation-page create-page">
    <!-- 选择目标平台 -->
    <section class="workstation-glass-panel card">
      <h2 class="card-title">选择目标平台</h2>
      <div class="platform-pills">
        <button
          v-for="option in platformOptions"
          :key="option.value"
          type="button"
          class="pill"
          :class="{ active: !iosSelected && formValue.target_platform === option.value }"
          :disabled="option.disabled"
          :title="option.label"
          :data-testid="`platform-${option.value}`"
          @click="handlePlatformChange(option.value)"
        >
          {{ option.short }}
        </button>
        <button
          type="button"
          class="pill"
          :class="{ active: iosSelected }"
          title="iOS 自动质检能力待接入"
          data-testid="platform-ios"
          @click="selectIos"
        >
          iOS <em>待接入</em>
        </button>
      </div>
      <div v-if="!iosSelected" class="task-type-row">
        <span class="qc-label">任务类型</span>
        <label
          class="task-type-option"
          :class="{
            'is-disabled':
              formValue.target_platform === 'web' &&
              !webCapability?.execution_modes.includes('full_inspection'),
          }"
        >
          <input
            v-model="formValue.execution_mode"
            type="radio"
            name="task-type"
            value="full_inspection"
            :disabled="
              formValue.target_platform === 'web' &&
              !webCapability?.execution_modes.includes('full_inspection')
            "
          />
          完整质检
          <small>自动走一遍页面并截图，按所选规则逐条检查，完成后生成正式报告</small>
        </label>
        <label class="task-type-option">
          <input
            v-model="formValue.execution_mode"
            type="radio"
            name="task-type"
            value="exploration_only"
            :disabled="
              formValue.target_platform === 'web' &&
              !webCapability?.execution_modes.includes('exploration_only')
            "
          />
          仅页面探索
          <small>只自动走一遍页面、留存截图并识别页面类型，不做规则检查，也不生成报告</small>
        </label>
      </div>
    </section>

    <!-- 草稿箱 -->
    <section v-if="drafts.length" class="workstation-glass-panel draft-bar" data-testid="draft-bar">
      <button type="button" class="qc-link" @click="draftsOpen = !draftsOpen">
        草稿箱（{{ drafts.length }}）{{ draftsOpen ? '收起' : '展开' }}
      </button>
      <ul v-if="draftsOpen" class="draft-list">
        <li v-for="draft in drafts" :key="draft.id">
          <strong>{{ draft.name }}</strong>
          <span class="qc-muted">{{ draft.target_platform === 'app' ? 'Android' : draft.target_platform === 'web' ? 'Web' : 'H5' }} · {{ formatDraftTime(draft.updated_at) }}</span>
          <button type="button" class="qc-link" @click="restoreDraft(draft)">恢复</button>
          <button type="button" class="qc-link danger" @click="removeDraft(draft)">删除</button>
        </li>
      </ul>
    </section>

    <!-- 基础信息 -->
    <section class="workstation-glass-panel card form-card">
      <label class="field">
        <span class="qc-label">任务名称 <b>*</b></span>
        <input
          v-model="formValue.task_name"
          class="qc-input"
          type="text"
          placeholder="请输入任务名称"
          data-testid="task-name"
        />
      </label>
      <div class="two-col">
        <div class="field">
          <span class="qc-label">产品 <b>*</b></span>
          <n-select
            :value="selectedProductCode"
            :options="productOptions"
            :loading="productsLoading"
            filterable
            placeholder="请选择"
            @update:value="handleProductChange"
          >
            <template #empty>
              <span class="qc-muted">{{ productsLoading ? '产品加载中…' : '暂无产品' }}</span>
            </template>
          </n-select>
        </div>
      </div>
    </section>

    <!-- iOS：只登记产品资料，自动质检待接入 -->
    <section v-if="iosSelected" class="workstation-glass-panel card form-card" data-testid="ios-config">
      <h2 class="card-title">iOS 质检配置</h2>
      <div class="ios-notice">
        <strong>iOS 自动质检能力待接入</strong>
        <p>
          iOS 产品资料已经可以在产品管理和 Excel 导入里登记。上传 iOS 安装包、安装到手机和自动质检还在接入中，暂时不能发起 iOS 质检任务。
        </p>
      </div>
    </section>

    <!-- H5 / Web 质检配置 -->
    <section
      v-else-if="formValue.target_platform !== 'app'"
      class="workstation-glass-panel card form-card"
      :data-testid="`${formValue.target_platform}-config`"
    >
      <h2 class="card-title">{{ formValue.target_platform === 'web' ? 'Web 质检配置' : 'H5 质检配置' }}</h2>
      <label v-if="formValue.target_platform === 'h5'" class="field">
        <span class="qc-label">H5 页面地址 <b>*</b></span>
        <input
          v-model="formValue.target_url"
          class="qc-input"
          data-testid="h5-target-url"
          type="url"
          placeholder="https://example.com/h5"
        />
        <small class="field-hint">选择产品后默认带入 H5 地址，也可以按本次任务修改</small>
      </label>
      <label v-else class="field">
        <span class="qc-label">Web 页面地址 <b>*</b></span>
        <input
          v-model="formValue.web_target_url"
          class="qc-input"
          data-testid="web-target-url"
          type="url"
          placeholder="https://example.com/web"
        />
        <small class="field-hint">Web 地址单独填写，不读取产品资料里的 H5 地址</small>
      </label>
      <div class="two-col">
        <div class="field">
          <span class="qc-label">规则集 <b>*</b></span>
          <RulesetMultiSelect
            v-model="formValue.ruleset_selection"
            :rulesets="rulesets"
            :loading="catalogLoading"
            data-testid="ruleset-picker"
          />
        </div>
        <div class="field">
          <span class="qc-label">页面范围</span>
          <PageScopeMultiSelect
            v-model="formValue.page_scope[formValue.target_platform]"
            :codes="pageScopeCodes(formValue.target_platform)"
            :page-types="pageTypes"
            :loading="catalogLoading"
            data-testid="page-scope-picker"
          />
          <small class="field-hint">{{ PAGE_SCOPE_HINT }}</small>
        </div>
      </div>
      <H5ShortflowSelect v-if="formValue.target_platform === 'h5' && !isExplorationOnly && !catalogLoading"
        v-model="h5Shortflows" :ruleset-codes="selectedRulesets.map((item) => item.ruleset_code)" />
    </section>

    <!-- Android 质检配置（三步） -->
    <section v-else class="workstation-glass-panel card form-card" data-testid="android-config">
      <div class="wizard-head">
        <div>
          <h2 class="card-title">Android 质检配置</h2>
          <p class="qc-muted small">分步完成基础信息、设备和巡检确认</p>
        </div>
        <span class="step-indicator" data-testid="step-indicator">第 {{ currentStep }} 步 / {{ STEPS.length }}</span>
      </div>
      <ol class="step-bar" data-testid="create-steps">
        <li
          v-for="step in STEPS"
          :key="step.key"
          :class="{ active: currentStep === step.key, done: currentStep > step.key }"
        >
          <button type="button" @click="jumpToStep(step.key)">
            <i>{{ step.key }}</i>
            {{ step.label }}
          </button>
        </li>
      </ol>

      <div v-show="currentStep === 1" class="step-body" data-testid="step-basic">
        <div class="field">
          <span class="qc-label">APK 文件上传 <b>*</b></span>
          <input
            ref="fileInputRef"
            class="file-input-hidden"
            type="file"
            accept=".apk,application/vnd.android.package-archive"
            @change="onFileChange"
          />
          <button type="button" class="upload-box" :class="{ 'has-file': appBuild }" :disabled="uploading" @click="openFilePicker">
            <span class="upload-badge">APK</span>
            <strong>
              {{ uploading ? '上传校验中…' : appBuild ? appBuild.original_filename : '点击或拖拽 APK 文件到此处' }}
            </strong>
            <span>
              {{
                appBuild
                  ? `已通过校验${appBuild.package_name ? ` · ${appBuild.package_name}` : ''}`
                  : selectedFile
                    ? `已选择：${selectedFile.name}`
                    : '支持 .apk，最大 500MB'
              }}
            </span>
          </button>
        </div>
        <div class="two-col">
          <label class="field">
            <span class="qc-label">Android 包名 <b>*</b></span>
            <input
              v-model="formValue.package_name"
              class="qc-input"
              type="text"
              placeholder="com.example.app"
              data-testid="app-package-name"
            />
            <small class="field-hint">上传 APK 后自动带出，也可以手动填写</small>
          </label>
        </div>
      </div>

      <div v-show="currentStep === 2" class="step-body" data-testid="step-device">
        <div class="two-col">
          <div class="field">
            <span class="qc-label">设备后端 <b>*</b></span>
            <n-select
              v-model:value="formValue.device_backend"
              :options="deviceBackendOptions"
              filterable
              data-testid="app-device-backend"
            />
          </div>
          <div class="field span-2">
            <span class="qc-label">设备配置 <b>*</b></span>
            <div class="device-summary" data-testid="app-device-summary">
              <div>
                <strong>{{ formValue.device_profile_id ? '已选择 1 个设备配置' : '未选择设备配置' }}</strong>
                <span>{{ deviceSummaryText }}</span>
              </div>
              <button class="qc-link" type="button" @click="devicePickerOpen = !devicePickerOpen">
                {{ devicePickerOpen ? '收起' : '选择设备 >' }}
              </button>
            </div>
            <div v-show="devicePickerOpen" class="device-picker">
              <n-select
                v-model:value="formValue.device_profile_id"
                :options="deviceProfileOptions"
                :loading="devicesLoading"
                :fallback-option="false"
                filterable
                tag
                data-testid="app-device-profile"
                placeholder="选择设备，或手动填写设备编号"
              >
                <template #empty>
                  <span class="qc-muted">{{ devicesLoading ? '设备加载中…' : '暂无设备' }}</span>
                </template>
              </n-select>
              <small class="field-hint">
                来自已登记的设备；隔离或停用的设备不能选。设备正在用也可以提交，任务会排队等它空出来。
                <button class="qc-link" type="button" :disabled="devicesLoading" @click="loadDevices()">刷新状态</button>
              </small>
              <p v-if="devicesError" class="field-error">{{ devicesError }}</p>
              <ul v-if="devicesForBackend.length" class="device-status-list" data-testid="app-device-status-list">
                <li
                  v-for="item in devicesForBackend"
                  :key="item.device_ref"
                  :class="[
                    'device-status-item',
                    `is-${item.occupancy_status}`,
                    { 'is-selected': item.profile_id === formValue.device_profile_id },
                  ]"
                >
                  <strong>{{ item.profile_id }}</strong>
                  <span>{{ deviceOccupancyLabel(item) }}</span>
                  <span v-if="item.queued_preferred_count > 0">排队 {{ item.queued_preferred_count }}</span>
                  <span v-else-if="item.occupied && item.lease_task_id" class="qc-muted">
                    任务 {{ item.lease_task_id.slice(0, 8) }}
                  </span>
                </li>
              </ul>
              <p v-else-if="!devicesLoading && !devicesError" class="field-hint">
                这类设备还没有登记；可以手动填写设备编号，或联系运维添加设备。
              </p>
              <p v-if="selectedDevice?.occupancy_status === 'occupied'" class="field-hint warn">
                该设备正在质检中；提交后会排队，等设备空出来再开始。
              </p>
            </div>
          </div>
          <div class="field">
            <span class="qc-label">设备能力模板</span>
            <n-select
              v-model:value="formValue.device_capability_template"
              :options="deviceCapabilityTemplateOptions"
              filterable
              data-testid="app-device-capability-template"
            />
            <small class="field-hint">使用默认模板时，系统按产品登记的设置自动选择</small>
          </div>
          <label class="field">
            <span class="qc-label">质检参数方案</span>
            <SearchSelect disabled data-testid="app-qc-parameter-preset">
              <option>使用默认参数</option>
            </SearchSelect>
            <small class="field-hint">参数方案暂未开放，按默认参数执行</small>
          </label>
        </div>
      </div>

      <div v-show="currentStep === 3" class="step-body" data-testid="step-confirm">
        <div class="two-col">
          <div class="field">
            <span class="qc-label">规则集 <b>*</b></span>
            <RulesetMultiSelect
              v-model="formValue.ruleset_selection"
              :rulesets="rulesets"
              :loading="catalogLoading"
              data-testid="app-ruleset-picker"
            />
          </div>
          <div class="field">
            <span class="qc-label">页面范围</span>
            <PageScopeMultiSelect
              v-model="formValue.page_scope.app"
              :codes="pageScopeCodes('app')"
              :page-types="pageTypes"
              :loading="catalogLoading"
              data-testid="app-page-scope-picker"
            />
            <small class="field-hint">{{ PAGE_SCOPE_HINT }}</small>
          </div>
          <div class="field span-2">
            <span class="qc-label">规则清单 <b>*</b></span>
            <p v-if="catalogLoading" class="field-hint">规则加载中…</p>
            <p v-else-if="catalogFailed" class="field-hint warn">规则集加载失败，请刷新页面后重试</p>
            <p v-else-if="!selectedRulesets.length" class="field-hint">请先选择规则集</p>
            <p v-else-if="!selectedRules.length" class="field-hint warn">所选规则集里没有可用的规则</p>
            <template v-else>
              <div class="rule-list" data-testid="rule-checklist">
                <label v-for="rule in selectedRules" :key="rule.rule_code" class="rule-card">
                  <input type="checkbox" checked disabled />
                  <span>
                    <strong>{{ rule.name }}</strong>
                    <small>{{ categoryLabel(rule.category) }}</small>
                  </span>
                </label>
              </div>
              <small class="field-hint">共 {{ selectedRules.length }} 条规则，全部执行；逐条取消规则暂未开放</small>
            </template>
          </div>
        </div>
        <div class="confirm-tiles" data-testid="confirm-tiles">
          <div v-for="tile in confirmTiles" :key="tile.label" class="confirm-tile">
            <span>{{ tile.label }}</span>
            <strong>{{ tile.value }}</strong>
          </div>
        </div>
      </div>

      <div class="step-actions">
        <button v-show="currentStep > 1" class="qc-btn" type="button" data-testid="wizard-prev" @click="prevStep">上一步</button>
        <div class="step-actions-main">
          <button class="qc-btn" type="button" :disabled="savingDraft" data-testid="wizard-save-draft" @click="saveDraft">
            {{ savingDraft ? '保存中…' : '保存草稿' }}
          </button>
          <button
            v-show="currentStep < STEPS.length"
            class="qc-btn qc-btn-primary"
            type="button"
            data-testid="wizard-next"
            @click="nextStep"
          >
            下一步
          </button>
          <button
            v-show="currentStep === STEPS.length"
            class="qc-btn qc-btn-primary"
            type="button"
            :disabled="submitting"
            data-testid="wizard-submit"
            @click="handleSubmit"
          >
            {{ submitting ? '提交中…' : '开始巡检' }}
          </button>
        </div>
      </div>
    </section>

    <!-- 备注 -->
    <section class="workstation-glass-panel card form-card">
      <label class="field">
        <span class="qc-label">备注</span>
        <textarea v-model="formValue.remark" class="qc-textarea" maxlength="500" rows="3" placeholder="补充任务说明、特殊关注项或业务背景..." />
        <small class="field-hint">{{ formValue.remark.length }}/500</small>
      </label>
    </section>

    <!-- 底部操作（Android 在向导里提交，不显示这一栏） -->
    <div v-if="iosSelected || formValue.target_platform !== 'app'" class="form-footer" data-testid="form-footer">
      <button class="qc-btn" type="button" :disabled="savingDraft || iosSelected" data-testid="save-draft" @click="saveDraft">
        {{ savingDraft ? '保存中…' : '保存草稿' }}
      </button>
      <button class="qc-btn cancel-button" type="button" @click="cancelCreate">取消</button>
      <button
        class="qc-btn qc-btn-primary submit-button"
        type="button"
        :disabled="submitting || iosSelected"
        :title="iosSelected ? 'iOS 自动质检能力待接入' : undefined"
        @click="handleSubmit"
      >
        {{ submitting ? '创建中...' : '立即发起质检' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.create-page {
  gap: 16px;
  padding-bottom: 72px;
}

.card {
  padding: 20px;
}

.card-title {
  margin: 0 0 12px;
  color: var(--workstation-text);
  font-size: 14px;
  font-weight: 500;
}

.small {
  margin: 0;
  font-size: 12px;
}

.form-card {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.form-card .card-title {
  margin-bottom: 0;
}

/* 平台胶囊：px-4 py-2 border rounded-lg；选中 bg-blue-600 */
.platform-pills {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 18px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text);
  background: var(--workstation-panel);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}

.pill.active {
  border-color: var(--workstation-primary);
  color: #ffffff;
  background: var(--workstation-primary);
}

.pill:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

/* 原型 iOS「待接入」角标：text-amber-600 */
.pill em {
  color: var(--workstation-warning);
  font-size: 11px;
  font-style: normal;
}

.pill.active em {
  color: rgba(255, 255, 255, 0.85);
}

.task-type-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 16px;
  margin-top: 16px;
}

.task-type-row .qc-label {
  margin: 8px 0 0;
}

.task-type-option {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 4px 8px;
  max-width: 360px;
  padding: 10px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text-strong);
  font-size: 14px;
  cursor: pointer;
}

.task-type-option small {
  grid-column: 2;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.5;
}

.task-type-option.is-disabled {
  opacity: 0.55;
}

.field {
  display: block;
  min-width: 0;
}

.field .qc-label b {
  color: var(--workstation-danger);
}

.field-hint {
  display: block;
  margin-top: 6px;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.6;
}

.field-hint.warn {
  color: var(--workstation-warning);
}

.field-error {
  margin: 6px 0 0;
  color: var(--workstation-danger);
  font-size: 12px;
}

.two-col {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
}

.span-2 {
  grid-column: 1 / -1;
}

/* iOS 待接入说明：border-amber-200 bg-amber-50 */
.ios-notice {
  padding: 14px 16px;
  border: 1px solid var(--sev-medium-line);
  border-radius: var(--workstation-radius-sm);
  background: var(--tint-warning);
}

.ios-notice strong {
  display: block;
  color: var(--workstation-warning-strong);
  font-size: 14px;
  font-weight: 500;
}

.ios-notice p {
  margin: 6px 0 0;
  color: var(--workstation-warning);
  font-size: 12px;
  line-height: 1.7;
}

/* ---------- Android 三步向导 ---------- */

.wizard-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.step-indicator {
  color: var(--workstation-primary);
  font-size: 12px;
  font-weight: 600;
}

.step-bar {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.step-bar button {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text-muted);
  background: var(--workstation-panel-soft);
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  text-align: left;
  cursor: pointer;
}

.step-bar button i {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  border: 1px solid currentColor;
  border-radius: 50%;
  font-size: 11px;
  font-style: normal;
}

.step-bar li.active button {
  border-color: var(--workstation-primary);
  color: #ffffff;
  background: var(--workstation-primary);
}

.step-bar li.active button i {
  border-color: rgba(255, 255, 255, 0.6);
}

.step-bar li.done button {
  color: var(--workstation-success);
}

.step-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.file-input-hidden {
  display: none;
}

/* APK 上传区：原型 dashed 框 + 说明 */
.upload-box {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  width: 100%;
  min-height: 150px;
  padding: 24px;
  border: 2px dashed #cbd5e1;
  border-radius: var(--workstation-radius);
  color: var(--workstation-text-muted);
  background: var(--workstation-panel-soft);
  font: inherit;
  cursor: pointer;
}

.upload-box:hover:not(:disabled) {
  border-color: #60a5fa;
}

.upload-box.has-file {
  border-color: var(--workstation-success);
  border-style: solid;
}

.upload-badge {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  border-radius: var(--workstation-radius);
  color: var(--workstation-primary);
  background: var(--tint-primary);
  font-size: 15px;
  font-weight: 700;
}

.upload-box strong {
  color: var(--workstation-text);
  font-size: 14px;
  font-weight: 500;
}

.upload-box span:last-child {
  font-size: 12px;
}

.device-summary {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
}

.device-summary strong {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 14px;
  font-weight: 500;
}

.device-summary span {
  display: block;
  margin-top: 3px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.device-picker {
  display: grid;
  gap: 10px;
  margin-top: 12px;
}

.device-status-list {
  display: grid;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.device-status-item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  padding: 8px 10px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text);
  background: var(--workstation-panel);
  font-size: 13px;
}

.device-status-item strong {
  color: var(--workstation-text-strong);
}

.device-status-item.is-selected {
  border-color: var(--workstation-primary);
  background: var(--tint-primary);
}

.device-status-item.is-available {
  box-shadow: inset 3px 0 0 var(--workstation-success);
}

.device-status-item.is-occupied {
  box-shadow: inset 3px 0 0 var(--workstation-warning);
}

.device-status-item.is-recovering {
  box-shadow: inset 3px 0 0 var(--workstation-primary);
}

.device-status-item.is-quarantined,
.device-status-item.is-disabled {
  opacity: 0.72;
  box-shadow: inset 3px 0 0 var(--workstation-danger);
}

/* 规则清单：原型 grid-cols-1 md:2 xl:3 的勾选卡片；规则多时在框内滚动 */
.rule-list {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 8px;
  max-height: 320px;
  padding: 2px;
  overflow-y: auto;
}

.rule-card {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
}

.rule-card input {
  flex-shrink: 0;
  margin: 3px 0 0;
  accent-color: var(--workstation-primary);
}

.rule-card strong {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 13px;
  font-weight: 500;
}

.rule-card small {
  display: block;
  margin-top: 2px;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.confirm-tiles {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.confirm-tile {
  padding: 12px 14px;
  border-radius: var(--workstation-radius-sm);
  background: var(--tint-primary);
}

.confirm-tile span {
  display: block;
  color: var(--workstation-primary);
  font-size: 12px;
}

.confirm-tile strong {
  display: block;
  margin-top: 4px;
  color: var(--workstation-primary-strong);
  font-size: 16px;
  font-weight: 600;
}

.step-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.step-actions-main {
  display: flex;
  gap: 8px;
  margin-left: auto;
}

.draft-bar {
  padding: 12px 20px;
}

.draft-list {
  display: grid;
  gap: 8px;
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
}

.draft-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  padding: 8px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
  font-size: 13px;
}

.draft-list strong {
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.draft-list .qc-link.danger {
  color: var(--workstation-danger);
}

/* ---------- 底部固定操作栏 ---------- */

.form-footer {
  position: sticky;
  bottom: 0;
  z-index: 10;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 12px 20px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  background: var(--workstation-panel);
  box-shadow: var(--workstation-shadow-lg);
}

@media (max-width: 900px) {
  .two-col,
  .step-bar {
    grid-template-columns: 1fr;
  }

  .confirm-tiles {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
