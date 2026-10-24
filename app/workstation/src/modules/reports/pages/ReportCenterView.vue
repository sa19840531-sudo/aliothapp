<script setup lang="ts">
import WebReportExplanationPanel from '@/components/WebReportExplanationPanel.vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import SearchSelect from '@/components/pickers/SearchSelect.vue'
import { usePortalNav } from '@/composables/usePortalNav'
import { taskListReturnQuery } from '@/modules/tasks/taskListState'
import { NButton, NIcon, NModal, NSpin, useMessage } from 'naive-ui'
import { ChevronBackOutline, ChevronForwardOutline, DownloadOutline } from '@vicons/ionicons5'
import { downloadReportExport, getReportInsight } from '@/services/reports.api'
import { AliothHttpError } from '@/services/http'
import { getTaskScreenshots, issueEvidenceFileUrl } from '@/services/task-insight.api'
import { getTask } from '@/services/tasks.api'
import { useEvidenceFileUrls } from '@/composables/useEvidenceFileUrls'
import { authStore } from '@/stores/auth'
import type {
  ReportDistributionStatus,
  ReportInsight,
  ReportExecutionItem,
  ReportRuleItem,
  WebLogicalReportPage,
} from '@/types/report-insight'
import type { WorkstationTaskDetail } from '@/types/task'
import type { ScreenshotRelatedRule, TaskScreenshots } from '@/types/task-insight'
import {
  REPORT_STATE_LABELS,
  reportStateOf,
  severityClass,
  severityLabel,
  type ReportState,
} from '@/utils/status'
import { pageTypeNames } from '@/utils/exploration-labels'
import ReportConfirmSection from '@/modules/reports/components/ReportConfirmSection.vue'
import ReportEvidenceThumb from '@/modules/reports/components/ReportEvidenceThumb.vue'
import { indexReportScreenshots } from '@/modules/reports/components/reportScreenshots'
import RuleCategoryDialog from '@/modules/reports/components/RuleCategoryDialog.vue'
import RuleUsageDialog from '@/modules/reports/components/RuleUsageDialog.vue'
import { isLinkedGreenResult } from '@/modules/tasks/components/greenRuleResultDetails'
import {
  REPORT_STATE_TONES,
  RESULT_GROUPS,
  aiResultOf,
  confirmStatusText,
  finalResultOf,
  currentReportRuleItem,
  ruleExecutionStateText,
  ruleExecutionStateDetail,
  isUndecidableResult,
  isExecutionReportItem,
  countsAsQualityExecution,
  missingEvidenceText,
  resultGroupOf,
  severityChipClass,
  severityText,
  type ResultGroupMeta,
} from '@/modules/reports/components/reportDisplay'
import {
  QC_RATE_HELP,
  buildPhaseGroups,
  buildRuleRows,
  pageNamesText,
  rateText,
  summarizeRules,
  type PhaseGroup,
  type RuleRow,
} from '@/modules/reports/components/ruleGroups'

/**
 * 报告详情，对齐 2.1 原型 report-detail.html：
 * 标题行（报告状态三态 / 导出）→ 任务基本信息 + 规则结果汇总（规则总数、实际使用和六类结果，
 * 和规则列表同一口径按条数算；「!」说明状态并保留查看这一类规则的入口）→
 * 人工确认规则（仅已执行且业务结论待复核的规则）→ 规则执行结果列表
 * （按几期规则集分组、一次展开一期，一行一条规则带质检率；「查看详情」弹窗列出这条规则每一次的
 * 页面、判定与截图）→ Web 专属的证据说明与逻辑页面。
 * 报告状态只看报告生成情况（成功 / 生成中 / 生成失败），不拿单条规则的六类结果充当报告状态。
 */

const route = useRoute()
const router = useRouter()
const nav = usePortalNav()
const message = useMessage()
const evidenceFiles = useEvidenceFileUrls()

const taskId = computed(
  () => route.params.taskId?.toString() || route.query.task?.toString() || '',
)

const insight = ref<ReportInsight | null>(null)
const task = ref<WorkstationTaskDetail | null>(null)
const screenshots = ref<TaskScreenshots | null>(null)
const loading = ref(true)
/** 任务信息和截图各自加载，较慢的任务明细不会挡住已返回的证据。 */
const supportingLoading = ref(false)
const screenshotsLoading = ref(false)
const screenshotsError = ref('')
const loadError = ref('')
const confirmSection = ref<InstanceType<typeof ReportConfirmSection> | null>(null)

let pollTimer: number | undefined
const supportingRequests = new Map<string, Promise<void>>()
let active = true
let reportRequestSerial = 0
let viewGeneration = 0

async function loadAll(silent = false) {
  const requested = taskId.value
  const serial = ++reportRequestSerial
  const current = () => active && taskId.value === requested && serial === reportRequestSerial
  if (!requested) {
    loading.value = false
    return
  }
  if (!silent) {
    viewGeneration += 1
    loading.value = true
    insight.value = null
    screenshots.value = null
    task.value = null
    evidenceFiles.clear()
    openPhase.value = null
    fullPhases.value = new Set()
    ruleDialogVisible.value = false
    categoryDialogVisible.value = false
    closeViewer()
  }
  let focusExecutionId: string | null = null
  try {
    const insightResult = await getReportInsight(requested)
    if (!current()) {
      return
    }
    if (insight.value && insight.value.summary.run_id !== insightResult.summary.run_id
      && insight.value.rule_items.some((item) => item.green_result)) {
      // 当前报告进入新执行批次时，旧原图与慢任务/截图请求都不能延续。
      viewGeneration += 1
      closeViewer()
      ruleDialogVisible.value = false
      screenshots.value = null
      task.value = null
      evidenceFiles.clear()
    }
    insight.value = insightResult
    loadError.value = ''
    const requestedExecutionId = route.query.execution?.toString()
    if (requestedExecutionId) {
      // 从任务详情「去确认」过来：页面渲染后直接定位到这条规则的人工判定
      if (
        insightResult.rule_items.some(
          (item) => item.execution_id === requestedExecutionId && item.confirmable,
        )
      ) {
        focusExecutionId = requestedExecutionId
      }
      const nextQuery = { ...route.query }
      delete nextQuery.execution
      void router.replace({ query: nextQuery })
    }
    // 报告状态跟着任务走，静默刷新也要重取任务；截图只在首次或还没拿到时加载
    void loadSupportingData(requested, !silent || screenshots.value === null)
  } catch (error) {
    if (!current()) {
      return
    }
    loadError.value = error instanceof Error ? error.message : '报告加载失败'
  } finally {
    if (current()) {
      loading.value = false
      scheduleNextPoll()
    }
  }
  if (focusExecutionId && current()) {
    await nextTick()
    if (current()) focusConfirmItem(focusExecutionId)
  }
}

function loadSupportingData(requested: string, withScreenshots: boolean) {
  const generation = viewGeneration
  const current = () => active && taskId.value === requested && generation === viewGeneration
  supportingLoading.value = true
  const taskKey = `task:${requested}:${generation}`
  if (!supportingRequests.has(taskKey)) {
    const pending = getTask(requested)
      .then((value) => { if (current()) task.value = value })
      .catch(() => undefined)
      .finally(() => {
        supportingRequests.delete(taskKey)
        if (current()) supportingLoading.value = false
      })
    supportingRequests.set(taskKey, pending)
  }
  if (!withScreenshots) return
  screenshotsLoading.value = true
  screenshotsError.value = ''
  const shotsKey = `shots:${requested}:${generation}`
  if (supportingRequests.has(shotsKey)) return
  const pending = getTaskScreenshots(requested)
    .then((value) => {
      if (!current()) return
      screenshots.value = value
      void evidenceFiles.load([...indexReportScreenshots(value).keys()])
    })
    .catch(() => { if (current()) screenshotsError.value = '截图加载失败' })
    .finally(() => {
      supportingRequests.delete(shotsKey)
      if (current()) screenshotsLoading.value = false
    })
  supportingRequests.set(shotsKey, pending)
}

function scheduleNextPoll() {
  window.clearTimeout(pollTimer)
  const status = insight.value?.summary.task_status
  if (!loadError.value && (status === 'queued' || status === 'running')) {
    pollTimer = window.setTimeout(() => void loadAll(true), 5000)
  }
}

onMounted(() => void loadAll())
onBeforeUnmount(() => {
  active = false
  window.clearTimeout(pollTimer)
  window.removeEventListener('keydown', onViewerKeydown)
})
watch(taskId, () => void loadAll())

// ---------- 等级 ----------

const SEVERITY_RANK: Record<string, number> = {
  critical: 0,
  high: 1,
  medium: 2,
  low: 3,
  info: 4,
  unknown: 5,
}

/** 归一化等级键：兼容原始等级与 P0~P3 展示文案，取不到时为 unknown。 */
function severityKey(item: ReportRuleItem): string {
  return severityClass(item.severity || item.severity_display).replace('sev-', '')
}

const PLATFORM_LABELS: Record<string, string> = {
  h5: 'H5',
  web: 'Web',
  app: 'Android',
  android: 'Android',
  ios: 'iOS',
}

function platformLabel(code: string | null | undefined) {
  if (!code) return '-'
  return PLATFORM_LABELS[code.toLowerCase()] ?? code
}

// ---------- 标题行 ----------

const greenReadCurrent = computed(() => !task.value?.run_id || !insight.value?.summary.run_id
  || task.value.run_id === insight.value.summary.run_id)
const ruleItems = computed(() => (insight.value?.rule_items ?? [])
  .filter((item) => !item.green_result || greenReadCurrent.value).map(currentReportRuleItem))

/** 已执行且业务结论待复核的规则，含已确认的；检测缺口不进入人工业务确认。 */
const confirmItems = computed(
  () => (insight.value?.confirm_items ?? ruleItems.value)
    .filter((item) => !item.green_result || greenReadCurrent.value).map(currentReportRuleItem).filter(
    (item): item is ReportExecutionItem => isExecutionReportItem(item) && item.confirmable && !isUndecidableResult(item),
  ),
)

/** 报告状态三态：取任务的报告状态；任务数据还没回来时不显示。 */
const reportState = computed<ReportState | null>(() =>
  task.value ? reportStateOf(task.value.report_status, task.value.status) : null,
)
const isH5NoBusinessResults = computed(() =>
  insight.value?.incomplete_reasons?.includes('H5_REPORT_NO_BUSINESS_RESULTS') ?? false,
)
const reportPresentation = computed(() => {
  if (isH5NoBusinessResults.value) {
    return { label: '尚无有效质检结果', tone: 'tone-danger' }
  }
  const state = reportState.value
  if (state === 'success' && (greenIncomplete.value || executionGapCount.value > 0 || hasUnexecutedAudit.value || insight.value?.summary.rule_inventory_complete === false)) {
    return { label: '需复核', tone: 'tone-warning' }
  }
  return { label: state ? REPORT_STATE_LABELS[state] : '-', tone: state ? REPORT_STATE_TONES[state] : 'tone-muted' }
})

const reportStageNote = computed(() => {
  if (isH5NoBusinessResults.value) return '本次未完成规则质检，诊断证据保留在任务过程；不能作为业务质检报告或导出。'
  if (['invalid', 'unavailable'].includes(insight.value?.summary.rule_inventory_status ?? '')) return '完整规则清单暂不可核验：已有执行记录保留，不能据此认定全部所选规则已应用。'
  if (insight.value?.summary.rule_inventory_total === 0) return '本次规则清单没有可检规则，质检率没有执行分母。'
  if (insight.value?.summary.rule_inventory_complete === false) return `本次所选范围尚未完成：有 ${insight.value.summary.rule_inventory_pending_rules ?? 0} 条规则未完成适用性核对或判定。逐条原因见下方清单。`
  if (greenIncomplete.value) return '当前绿色规则仍有分项缺口、待复核或关联待核对；已有明确失败单独保留，待确认清零不代表规则完整。'
  if (executionGapCount.value > 0) return `检测未完整完成：有 ${executionGapCount.value} 次检测未形成结论，待确认清零不代表检测缺口已解决。`
  if (hasUnexecutedAudit.value) return '本次仍有规则未进行实际判定，未执行处置保留在原始记录中；待确认清零不代表规则已完成。'
  const stage = insight.value?.summary.report_stage
  if (stage === 'final') return '已通过完成校验，报告内容已固定。'
  if (stage === 'final_review') return '任务已结束，但还没通过完成校验：还有页面没覆盖到，或有待确认的规则。'
  return '任务尚未结束，页面、规则和证据数据仍可能继续变化。'
})

const exportReady = computed(() => insight.value?.export_ready ?? false)
const isReportPreview = computed(
  () =>
    insight.value?.report_availability !== undefined &&
    insight.value.report_availability !== 'final',
)
const isH5ReviewReport = computed(
  () =>
    insight.value?.summary.target_platform === 'h5' &&
    !isH5NoBusinessResults.value &&
    insight.value.report_availability === 'preview',
)
const isExportBlockedPreview = computed(
  () => isH5NoBusinessResults.value || (isReportPreview.value && !isH5ReviewReport.value),
)

const APPLICABILITY_MODE_LABELS: Record<string, string> = {
  catalog_declared: '按产品目录声明',
  observed: '按实际观察',
  default: '默认口径',
}

const webApplicabilityNotice = computed(() => {
  const applicability = insight.value?.page_exploration?.web_coverage?.applicability_snapshot
  if (!applicability) {
    return null
  }
  const required = pageTypeNames([...applicability.required_page_types].sort())
  const notApplicable = pageTypeNames([...applicability.not_applicable_page_types].sort())
  const prefix = insight.value?.report_availability === 'final' ? '适用范围已冻结' : '当前适用范围'
  const mode =
    APPLICABILITY_MODE_LABELS[applicability.applicability_mode] ?? applicability.applicability_mode
  return `${prefix}：必检 ${required}；不适用 ${notApplicable}（${mode}）`
})

const popupWallNote = computed(() => insight.value?.page_exploration?.popup_wall?.note ?? null)
const webLogicalPages = computed(() => insight.value?.web_logical_pages ?? [])

function webLogicalPageLabel(page: WebLogicalReportPage) {
  const pageName =
    page.classification_status === 'classified'
      ? page.page_type_name || page.page_type || '待识别'
      : `候选 ${page.proposed_page_type || '待识别'} · 待复核`
  return page.page_role === 'home' ? `首页 · ${pageName}` : pageName
}

// ---------- 导出 ----------

const EXPORT_NEEDS_CONFIRM = '请先完成人工确认规则，再生成导出报告'
const exporting = ref(false)

/** 导出被拦的原因；空串表示可以导出。 */
const exportBlockedReason = computed(() => {
  if (isH5NoBusinessResults.value) return '尚无有效质检结果，不能导出业务质检报告'
  if (greenIncomplete.value) return '当前绿色规则仍需复核、补齐分项或核对关联，暂不能导出'
  if (exportReady.value) {
    return ''
  }
  const pending =
    insight.value?.confirm_progress?.pending ??
    confirmItems.value.filter((item) => item.confirm_status === 'pending').length
  return pending > 0 ? EXPORT_NEEDS_CONFIRM : '报告暂不满足导出条件，请稍后再试'
})

async function exportReport() {
  if (!taskId.value || exporting.value) {
    return
  }
  if (exportBlockedReason.value) {
    message.warning(exportBlockedReason.value)
    return
  }
  exporting.value = true
  try {
    await downloadReportExport(taskId.value)
    message.success('HTML 报告已生成')
  } catch (error) {
    message.error(
      error instanceof AliothHttpError && error.message ? error.message : '报告生成失败，请稍后重试',
    )
  } finally {
    exporting.value = false
  }
}

function backToList() {
  const query = taskListReturnQuery(route.query)
  void nav.push(query ? { path: '/reports', query } : '/reports')
}

function goTaskDetail() {
  if (taskId.value) {
    const query = taskListReturnQuery(route.query)
    void nav.push(query ? { path: `/tasks/${taskId.value}`, query } : `/tasks/${taskId.value}`)
  }
}

// ---------- 任务基本信息 ----------

const workspaceName = computed(() => authStore.user.value?.tenant_name || '本公司')

function formatDate(value: string | null | undefined) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function formatTime(value: string | null | undefined) {
  if (!value) {
    return '--:--'
  }
  return new Date(value).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false })
}

function formatDuration(start: string | null | undefined, end: string | null | undefined) {
  if (!start || !end) return '-'
  const seconds = Math.max(0, Math.round((new Date(end).getTime() - new Date(start).getTime()) / 1000))
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  return minutes ? `${minutes}分${rest}秒` : `${rest}秒`
}

const rulesetText = computed(() => {
  // 同时跑多个规则集时编码是系统生成的，直接显示名称
  const name = insight.value?.summary.ruleset_name
  if (name) return name
  const code = insight.value?.summary.ruleset_code || task.value?.ruleset_code
  const version = insight.value?.summary.ruleset_version ?? task.value?.ruleset_version
  if (!code) return '-'
  return version != null ? `${code} · v${version}` : code
})

/** 页面类型：本次实际识别到的业务页面类型（不含开屏、加载等过渡页）；没有数据时不显示这一行。 */
const observedPageTypes = computed(() => {
  const codes =
    insight.value?.page_exploration?.coverage?.catalog_observation?.business_observed_page_types ?? []
  if (!codes.length) return null
  const names = pageTypeNames(codes.slice(0, 3))
  return codes.length > 3 ? `${names}等 ${codes.length} 类` : names
})

interface TaskInfoRow {
  label: string
  value: string
  strong?: boolean
  state?: ReportState | null
}

const taskInfo = computed<TaskInfoRow[]>(() => {
  const summary = insight.value?.summary
  if (!summary) return []
  const detail = task.value
  const started = detail?.started_at ?? summary.started_at
  const finished = detail?.finished_at ?? summary.finished_at
  const rows: TaskInfoRow[] = [{ label: '任务名称', value: summary.task_name, strong: true }]
  // 部门质检平台只看本部门的报告，所属公司和部门只在管理后台展示
  if (nav.isAdmin.value) {
    rows.push(
      { label: '所属公司', value: detail?.tenant_name || workspaceName.value },
      { label: '部门', value: detail?.product?.department_name || (supportingLoading.value ? '加载中…' : '-') },
    )
  }
  rows.push(
    { label: '产品', value: detail?.product?.product_name || (supportingLoading.value ? '加载中…' : '-') },
    { label: '目标平台', value: platformLabel(summary.target_platform) },
    { label: '规则集', value: rulesetText.value },
    { label: '页面识别数', value: `${summary.page_recognized_count ?? 0} 个页面` },
  )
  if (observedPageTypes.value) {
    rows.push({ label: '页面类型', value: observedPageTypes.value })
  }
  rows.push(
    {
      label: '规则执行数',
      value: `${ruleSummary.value.usedRuleCount} 条规则 · 用了 ${ruleSummary.value.executionCount} 次`,
    },
    {
      label: '执行时间',
      value: started ? `${formatDate(started)}${finished ? ` - ${formatTime(finished)}` : ' 起'}` : '-',
    },
    { label: '执行时长', value: formatDuration(started, finished) },
    {
      label: '报告状态',
      value: reportState.value
        ? reportPresentation.value.label
        : supportingLoading.value
          ? '加载中…'
          : '-',
      state: reportState.value,
    },
  )
  return rows
})

// ---------- 规则结果汇总 ----------

/** 「!」说明弹窗的内容：标题 + 几段话。 */
interface SummaryHelp {
  title: string
  paragraphs: string[]
  group?: ResultGroupMeta
}

interface SummaryTile {
  key: string
  label: string
  value: string
  tile: string
  /** 所有瓦片的「!」均弹出状态说明。 */
  help?: SummaryHelp
  /** 六类结果瓦片可筛选列表，并在说明弹窗内查看对应规则清单。 */
  group?: ResultGroupMeta
}

const TOTAL_HELP: SummaryHelp = {
  title: '规则总数 · 说明',
  paragraphs: [
    '新建任务时所选规则集里启用的规则条数；之后规则再启停，这个数也不变。',
    '每条所选规则都应进行适用性核对；平台不适用、任务范围未选择、缺证和未执行也必须逐条留在清单中，不能静默消失。',
    '这个功能上线前建的任务没记下当时的数，按现在启用的条数显示。',
  ],
}

const USED_HELP: SummaryHelp = {
  title: '实际使用 · 说明',
  paragraphs: [
    '这次任务实际进行过规则判定的规则条数，一条规则不管判了几次都只算一条；真实判定后证据不足仍计为使用。未执行占位、适用性核对和判定前处置不计入。',
    '规则总数减去实际使用，表示尚未实际判定的规则条数。平台或检查范围筛选、触发条件未出现、页面未覆盖等原因需查看逐条处置，原始审计记录仍保留。',
  ],
}

/** 六类结果瓦片的顺序（2026-09-24 立川定）：不适用、通过、未通过、计划外未覆盖、检测异常、其他。 */
const TILE_STATUS_ORDER: ReportDistributionStatus[] = [
  'not_applicable',
  'passed',
  'failed',
  'coverage_gap',
  'detect_error',
  'other',
]

/** 规则结果汇总和下面的规则执行结果列表同一口径：都按规则条数，不按判了几次累计。 */
const ruleSummary = computed(() => summarizeRules(ruleRows.value))
const executionGapCount = computed(() => ruleItems.value.filter((item) => countsAsQualityExecution(item) && isUndecidableResult(item)).length)
const greenIncomplete = computed(() => insight.value?.green_results_incomplete === true || ruleItems.value.some((item) => item.green_result
  && (isUndecidableResult(item)
    || ['pending', 'needs_recheck'].includes(item.green_result.review_status || '')
    || !['passed', 'failed', 'not_applicable'].includes(item.display_status))))
const hasUnexecutedAudit = computed(() => ruleItems.value.some((item) =>
  isExecutionReportItem(item) && !countsAsQualityExecution(item)
  && !(item.inventory_source && ['frozen', 'legacy_verified'].includes(item.inventory_source)
    && ['not_applicable', 'out_of_scope'].includes(item.applicability || '')),
))
/** 只有已知且范围可对齐的规则总数才计算差额，不把旧报告缺失的总数当成零。 */
const noExecutionRuleCount = computed(() => {
  const total = insight.value?.summary.ruleset_rule_total
  const used = ruleSummary.value.usedRuleCount
  return total != null && total >= used ? total - used : null
})

const summaryTiles = computed<SummaryTile[]>(() => {
  if (!insight.value) return []
  const summary = ruleSummary.value
  // 接口没给规则总数（旧后端）时，只能按实际使用的条数显示
  const total = insight.value.summary.ruleset_rule_total ?? summary.ruleCount
  const usedHelp = insight.value.summary.ruleset_rule_total != null && total > 0
    ? {
        ...USED_HELP,
        paragraphs: [
          ...USED_HELP.paragraphs,
          `本次实际使用的规则占规则总数的比例为 ${summary.usedRuleCount}/${total}（${rateText(summary.usedRuleCount, total)}）；该比例不是适用范围完成率，也不是质检率。`,
        ],
      }
    : USED_HELP
  const statusTiles = TILE_STATUS_ORDER.map((key) => RESULT_GROUPS.find((group) => group.key === key)!).map(
    (group) => ({
      key: group.key,
      label: group.label,
      value: String(summary.statusCounts[group.key]),
      tile: group.tile,
      group,
    }),
  )
  return [
    { key: 'total', label: '规则总数', value: String(total), tile: 'tile-neutral', help: TOTAL_HELP },
    { key: 'used', label: '实际使用', value: String(summary.usedRuleCount), tile: 'tile-primary', help: usedHelp },
    ...statusTiles,
  ]
})

/** 点汇总瓦片：表格只看该类结果（再点取消）。 */
function focusState(group: ResultGroupMeta) {
  resultFilter.value = resultFilter.value === group.key ? 'all' : group.key
  scrollToTable()
}

/**
 * 「!」说明弹窗（规则总数、实际使用）。开关和内容分开存：关闭时只收起弹窗、内容先留着。
 * 内容跟着一起清掉的话，弹窗的关闭动画播不完，页面的滚动锁不会解除（点过说明后页面滚不动）。
 */
const summaryHelp = ref<SummaryHelp | null>(null)
const summaryHelpVisible = ref(false)

function openSummaryHelp(help: SummaryHelp) {
  summaryHelp.value = help
  summaryHelpVisible.value = true
}

/** 状态说明中的查看规则入口：列出这一类规则，再查看各次执行的详情。 */
const categoryGroup = ref<ResultGroupMeta | null>(null)
const categoryDialogVisible = ref(false)
const categoryRules = computed(() => {
  const group = categoryGroup.value
  if (!group) return []
  return ruleRows.value.filter((row) => row.lead.display_status === group.key).sort(compareRules)
})

function openCategoryDialog(group: ResultGroupMeta) {
  categoryGroup.value = group
  categoryDialogVisible.value = true
}

function openTileInfo(tile: SummaryTile) {
  if (tile.group) {
    openSummaryHelp({ title: `${tile.label} · 说明`, paragraphs: [tile.group.help], group: tile.group })
  } else if (tile.help) {
    openSummaryHelp(tile.help)
  }
}

function openSummaryCategory() {
  const group = summaryHelp.value?.group
  if (!group) return
  summaryHelpVisible.value = false
  openCategoryDialog(group)
}

/** Web 任务才有的证据与判定说明（未发布原因、序列诊断）。 */
const showWebExplanation = computed(() => {
  const value = insight.value
  return (
    !!value &&
    value.summary.target_platform === 'web' &&
    (value.report_availability !== undefined || !!value.web_report_explanation)
  )
})

// ---------- 人工确认规则 ----------

/** 规则表「人工判定」：滚到人工确认区里这条规则的判定下拉。 */
function focusConfirmItem(executionId: string) {
  confirmSection.value?.focusItem(executionId)
}

// ---------- 规则执行结果列表：按几期规则集分组 ----------

const severityFilter = ref('all')
const resultFilter = ref<ReportDistributionStatus | 'all'>('all')
const tableRef = ref<HTMLElement | null>(null)
/** 每一期先列 10 条规则，其余点「展开全部」。 */
const PHASE_PREVIEW_SIZE = 10

const severityOptions = computed(() => {
  const keys = new Set(ruleItems.value.map((item) => severityKey(item)))
  return Array.from(keys)
    .sort((a, b) => (SEVERITY_RANK[a] ?? 9) - (SEVERITY_RANK[b] ?? 9))
    .map((key) => ({ value: key, label: key === 'unknown' ? '未定级' : (severityLabel(key) ?? key) }))
})

function compareRules(a: RuleRow, b: RuleRow) {
  const failedFirst =
    Number(b.lead.display_status === 'failed') - Number(a.lead.display_status === 'failed')
  if (failedFirst !== 0) return failedFirst
  const rank = (SEVERITY_RANK[severityKey(a.lead)] ?? 9) - (SEVERITY_RANK[severityKey(b.lead)] ?? 9)
  return rank !== 0 ? rank : a.rule_name.localeCompare(b.rule_name, 'zh-CN')
}

/** 接口给的是逐次结果（规则 × 页面），合成一行一条规则；每期的规则数、次数、质检率按整期算，不跟筛选走。 */
const ruleRows = computed(() => buildRuleRows(ruleItems.value))
const phaseGroups = computed(() => buildPhaseGroups(ruleRows.value))

const filtersActive = computed(
  () => severityFilter.value !== 'all' || resultFilter.value !== 'all',
)

function ruleMatches(row: RuleRow) {
  if (severityFilter.value !== 'all' && severityKey(row.lead) !== severityFilter.value) return false
  // 结果筛选和汇总瓦片同一口径：看这条规则行上的最终结果，筛出几条就是瓦片上的数
  if (resultFilter.value !== 'all' && row.lead.display_status !== resultFilter.value) {
    return false
  }
  return true
}

interface VisiblePhase {
  group: PhaseGroup
  rules: RuleRow[]
}

/** 筛选时只留有匹配规则的期。 */
const visiblePhases = computed<VisiblePhase[]>(() =>
  phaseGroups.value
    .map((group) => ({ group, rules: group.rules.filter(ruleMatches).sort(compareRules) }))
    .filter((entry) => !filtersActive.value || entry.rules.length > 0),
)

const matchedRuleCount = computed(() =>
  visiblePhases.value.reduce((sum, entry) => sum + entry.rules.length, 0),
)

/** 筛出来的规则一共用了几次，和每期标题「N 条规则 · 用了 M 次」同一口径。 */
const matchedUsageCount = computed(() =>
  visiblePhases.value.reduce(
    (sum, entry) => sum + entry.rules.reduce((count, row) => count + row.executionCount, 0),
    0,
  ),
)

/**
 * 手风琴：同一时间只展开一期。null = 还没选过（只有一期、或正在筛选时自动展开第一期），
 * '' = 用户把展开的那期收起了。
 */
const openPhase = ref<string | null>(null)

const effectiveOpenPhase = computed(() => {
  const keys = visiblePhases.value.map((entry) => entry.group.key)
  const chosen = openPhase.value
  if (chosen === '') return null
  if (chosen && keys.includes(chosen)) return chosen
  return keys.length === 1 || filtersActive.value ? (keys[0] ?? null) : null
})

function togglePhase(key: string) {
  openPhase.value = effectiveOpenPhase.value === key ? '' : key
}

const fullPhases = ref<Set<string>>(new Set())

function phaseRules(entry: VisiblePhase) {
  return fullPhases.value.has(entry.group.key)
    ? entry.rules
    : entry.rules.slice(0, PHASE_PREVIEW_SIZE)
}

function togglePhaseFull(key: string) {
  const next = new Set(fullPhases.value)
  if (next.has(key)) {
    next.delete(key)
  } else {
    next.add(key)
  }
  fullPhases.value = next
}

// 换了筛选条件：重新自动展开第一期有结果的
watch([severityFilter, resultFilter], () => {
  openPhase.value = null
})

function scrollToTable() {
  const element = tableRef.value
  if (element && typeof element.scrollIntoView === 'function') {
    element.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }
}

/** 行上「人工确认」：只用了一次照旧；多次时写还有几次待确认。 */
function ruleConfirmText(row: RuleRow) {
  if (row.items.length === 1) return confirmStatusText(row.lead)
  const unlinked = row.items.filter((item) => item.green_result && !isLinkedGreenResult(item.green_result)).length
  if (unlinked) return `关联待核对 ${unlinked} 次`
  const greenPending = row.items.filter((item) => item.green_result
    && ['pending', 'needs_recheck'].includes(item.green_result.review_status || '')).length
  if (greenPending) return `待人工复核 ${greenPending} 次`
  if (row.pendingCount) return `待确认 ${row.pendingCount} 次`
  return row.confirmedCount ? '已确认' : '-'
}

/** 规则表「人工判定」：优先定位还没确认的那一次。 */
function judgeTarget(row: RuleRow): string | null {
  const target =
    row.items.find((item) => isExecutionReportItem(item) && item.confirm_status === 'pending') ??
    row.items.find((item) => isExecutionReportItem(item) && item.confirmable)
  return target?.execution_id ?? null
}

function ruleMissingEvidenceText(row: RuleRow) {
  return [...new Set(row.items.flatMap((item) => missingEvidenceText(item).split('、')))]
    .filter(Boolean)
    .join('、')
}

// ---------- 规则详情弹窗 ----------

// 开关和内容分开：关弹窗只收开关，内容留着让关闭动画走完
const ruleDialogVisible = ref(false)
const ruleDialogKey = ref<string | null>(null)
const ruleDialogRow = computed(
  () => ruleRows.value.find((row) => row.key === ruleDialogKey.value) ?? null,
)
let judgeAfterDialog: string | null = null

function openRuleDialog(row: RuleRow) {
  ruleDialogKey.value = row.key
  ruleDialogVisible.value = true
}

/** 弹窗里点「去人工判定」：先关弹窗（从分类规则清单进来的连清单一起关），等它关完再定位，免得焦点被弹窗还回「查看详情」按钮。 */
function judgeFromDialog(executionId: string) {
  judgeAfterDialog = executionId
  ruleDialogVisible.value = false
  categoryDialogVisible.value = false
}

function onRuleDialogAfterLeave() {
  const executionId = judgeAfterDialog
  judgeAfterDialog = null
  if (executionId) {
    focusConfirmItem(executionId)
  }
}

// ---------- 截图与大图 ----------

interface ViewerEntry {
  artifact_id: string
  page_name: string
  captured_at: string | null
  related_rules: ScreenshotRelatedRule[]
}

const viewerVisible = ref(false)
const viewerItems = ref<ViewerEntry[]>([])
const viewerIndex = ref(0)
const viewerRuleName = ref('')
const viewerGreenBindings = ref<Array<{ executionId: string; ruleId: string; contractHash: string; artifactIds: string[] }>>([])
let viewerSelectionSerial = 0
/** null = 适应窗口；数字 = 相对原图的倍率，翻页时保持不变。 */
const viewerZoom = ref<number | null>(null)
const viewerNaturalWidth = ref(0)
const ZOOM_STEPS = [0.5, 0.75, 1, 1.5, 2, 3]
const viewerCurrent = computed(() => viewerItems.value[viewerIndex.value] || null)
const viewerAtFirst = computed(() => viewerIndex.value <= 0)
const viewerAtLast = computed(() => viewerIndex.value >= viewerItems.value.length - 1)

const viewerZoomLabel = computed(() =>
  viewerZoom.value === null ? '适应窗口' : `${Math.round(viewerZoom.value * 100)}%`,
)

const viewerImageStyle = computed(() => {
  if (viewerZoom.value === null || !viewerNaturalWidth.value) {
    return undefined
  }
  return { width: `${Math.round(viewerNaturalWidth.value * viewerZoom.value)}px` }
})

const screenshotIndex = computed(() => indexReportScreenshots(screenshots.value))

/** 每条规则能打开的截图（只保留本任务截图清单里有的）。 */
const ruleShotIds = computed(() => {
  const map = new Map<string, string[]>()
  for (const item of ruleItems.value) {
    if (!isExecutionReportItem(item)) continue
    if (item.green_result) {
      map.set(item.execution_id, isLinkedGreenResult(item.green_result)
        ? [...new Set(item.green_result.subitems.flatMap((part) => part.screenshot_artifact_ids))]
          .filter((id) => screenshotIndex.value.has(id)) : [])
      continue
    }
    map.set(
      item.execution_id,
      item.evidence_artifact_ids.filter((id) => screenshotIndex.value.has(id)),
    )
  }
  return map
})

function ruleScreenshotIds(item: ReportRuleItem) {
  return isExecutionReportItem(item) ? ruleShotIds.value.get(item.execution_id) ?? [] : []
}

/** 一条规则所有次数的截图（去重，按先后）。 */
function ruleRowShotIds(row: RuleRow) {
  return [...new Set(row.items.flatMap((item) => ruleScreenshotIds(item)))]
}

/** 打开大图查看器：从第 index 张开始，标题写规则名。 */
function openShots(artifactIds: string[], index: number, ruleName: string, sources?: ReportRuleItem[]) {
  const bindings: typeof viewerGreenBindings.value = []
  const allowed = new Set<string>()
  if (sources) {
    for (const source of sources) {
      if (!source.green_result) {
        ruleScreenshotIds(source).forEach((id) => allowed.add(id))
        continue
      }
      const current = ruleItems.value.find((item) => item.execution_id === source.execution_id && item.rule_id === source.rule_id)
      if (!current || !isLinkedGreenResult(current.green_result)
        || current.green_result.rule_contract_hash !== source.green_result.rule_contract_hash || !current.execution_id) continue
      const ids = ruleScreenshotIds(current).filter((id) => artifactIds.includes(id))
      ids.forEach((id) => allowed.add(id))
      bindings.push({ executionId: current.execution_id, ruleId: current.rule_id,
        contractHash: current.green_result.rule_contract_hash!, artifactIds: ids })
    }
  }
  const ids = [...new Set(artifactIds)].filter((id) => !sources || allowed.has(id))
  const entries = ids
    .map((id) => screenshotIndex.value.get(id))
    .filter((entry): entry is ViewerEntry => !!entry)
  if (!entries.length) {
    return
  }
  viewerItems.value = entries
  viewerSelectionSerial += 1
  viewerGreenBindings.value = bindings
  viewerRuleName.value = ruleName
  viewerIndex.value = Math.min(Math.max(index, 0), entries.length - 1)
  viewerZoom.value = null
  viewerVisible.value = true
}

function openViewerForRule(item: ReportRuleItem) {
  openShots(ruleScreenshotIds(item), 0, item.rule_name, [item])
}

function openGreenSubitemShot(item: ReportRuleItem, artifactId: string) {
  // 分项按钮只打开这张明确引用的原图，不借本规则其他分项或其他次执行补图。
  openShots([artifactId], 0, item.rule_name, [item])
}

function closeViewer() {
  viewerSelectionSerial += 1
  viewerVisible.value = false
  viewerItems.value = []
  viewerGreenBindings.value = []
}

function greenViewerCurrent() {
  return viewerGreenBindings.value.every((binding) => {
    const current = ruleItems.value.find((item) => item.execution_id === binding.executionId && item.rule_id === binding.ruleId)
    return current && isLinkedGreenResult(current.green_result)
      && current.green_result.rule_contract_hash === binding.contractHash
      && binding.artifactIds.every((id) => ruleScreenshotIds(current).includes(id))
  })
}

watch([ruleItems, screenshotIndex], () => {
  if (viewerGreenBindings.value.length && !greenViewerCurrent()) closeViewer()
})

/** 翻页到头就停，不循环。 */
function viewerPrev() {
  if (!viewerAtFirst.value) {
    viewerIndex.value -= 1
  }
}

function viewerNext() {
  if (!viewerAtLast.value) {
    viewerIndex.value += 1
  }
}

function zoomIn() {
  const current = viewerZoom.value ?? 1
  viewerZoom.value = ZOOM_STEPS.find((step) => step > current + 1e-6) ?? ZOOM_STEPS[ZOOM_STEPS.length - 1]
}

function zoomOut() {
  const current = viewerZoom.value ?? 1
  viewerZoom.value =
    [...ZOOM_STEPS].reverse().find((step) => step < current - 1e-6) ?? ZOOM_STEPS[0]
}

function toggleZoomMode() {
  viewerZoom.value = viewerZoom.value === null ? 1 : null
}

function onViewerImageLoad(event: Event) {
  const target = event.target as HTMLImageElement | null
  viewerNaturalWidth.value = target?.naturalWidth ?? 0
}

function onViewerKeydown(event: KeyboardEvent) {
  if (!viewerVisible.value) {
    return
  }
  switch (event.key) {
    case 'ArrowLeft':
      viewerPrev()
      break
    case 'ArrowRight':
      viewerNext()
      break
    case 'Escape':
      viewerVisible.value = false
      break
    case '+':
    case '=':
      zoomIn()
      break
    case '-':
      zoomOut()
      break
    case '0':
      viewerZoom.value = null
      break
    default:
      return
  }
  event.preventDefault()
}

watch(viewerVisible, (visible) => {
  if (visible) {
    window.addEventListener('keydown', onViewerKeydown)
  } else {
    window.removeEventListener('keydown', onViewerKeydown)
  }
})

async function downloadViewerCurrent() {
  const current = viewerCurrent.value
  if (current) {
    try {
      if (viewerGreenBindings.value.length) {
        const generation = viewGeneration
        const selection = viewerSelectionSerial
        const url = await issueEvidenceFileUrl(current.artifact_id, { download: true })
        if (!active || generation !== viewGeneration || selection !== viewerSelectionSerial || !viewerVisible.value || !greenViewerCurrent()
          || !viewerItems.value.some((entry) => entry.artifact_id === current.artifact_id)) return
        const anchor = document.createElement('a')
        anchor.href = url
        anchor.target = '_blank'
        anchor.rel = 'noopener'
        document.body.appendChild(anchor)
        anchor.click()
        anchor.remove()
        return
      }
      await evidenceFiles.open(current.artifact_id, { download: true })
    } catch (error) {
      message.error(error instanceof Error ? error.message : '证据下载失败')
    }
  }
}

function evidenceUrl(artifactId: string) {
  return evidenceFiles.get(artifactId)
}
</script>

<template>
  <div class="workstation-page report-detail-page">
    <div v-if="loading" class="workstation-glass-panel state-panel">
      <n-spin size="large" />
      <p>报告加载中…</p>
    </div>

    <div v-else-if="loadError" class="workstation-glass-panel state-panel">
      <h3>报告加载失败</h3>
      <p>{{ loadError }}</p>
      <div class="state-actions">
        <n-button type="primary" @click="loadAll()">重试</n-button>
        <n-button secondary @click="backToList">返回报告列表</n-button>
      </div>
    </div>

    <div v-else-if="!insight" class="workstation-glass-panel state-panel">
      <p>请从报告列表选择一份报告查看</p>
      <n-button type="primary" @click="backToList">前往报告列表</n-button>
    </div>

    <template v-else>
      <!-- 标题行 -->
      <header class="report-header" data-testid="report-header">
        <div>
          <h1>{{ insight.summary.task_name }}</h1>
          <p>
            任务编号：<span class="qc-mono">{{ insight.summary.task_id }}</span>
            · 生成时间：{{ formatDate(insight.summary.generated_at) }}
          </p>
        </div>
        <div class="header-actions">
          <span
            v-if="reportState"
            class="qc-chip"
            :class="reportPresentation.tone"
            data-testid="report-state"
          >
            <i></i>{{ reportPresentation.label }}
          </span>
          <span v-if="isExportBlockedPreview" class="qc-chip tone-danger">{{ isH5NoBusinessResults ? '诊断信息 · 不可导出' : '预览 · 不可导出' }}</span>
          <span v-if="isH5ReviewReport" class="qc-chip tone-warning">复核报告 · 非正式</span>
          <button
            v-if="!isExportBlockedPreview"
            type="button"
            class="qc-btn"
            :class="{ 'is-disabled': !!exportBlockedReason }"
            :disabled="exporting || greenIncomplete"
            :title="exportBlockedReason || undefined"
            data-testid="export-report"
            @click="exportReport"
          >
            <n-icon size="15"><DownloadOutline /></n-icon>
            {{ exporting ? '正在生成 HTML…' : executionGapCount || hasUnexecutedAudit || isH5ReviewReport ? '导出复核报告' : '导出报告' }}
          </button>
        </div>
      </header>

      <div class="notice-row">
        <span class="notice" :class="`notice-${isH5NoBusinessResults || executionGapCount ? 'final_review' : insight.summary.report_stage}`">{{ reportStageNote }}</span>
        <span v-if="webApplicabilityNotice" class="notice">{{ webApplicabilityNotice }}</span>
        <span v-if="popupWallNote" class="notice notice-final_review">弹窗问题：{{ popupWallNote }}</span>
      </div>

      <section
        v-if="insight.report_availability === 'none'"
        class="workstation-glass-panel card report-none-notice"
        data-testid="report-none-notice"
      >
        <div>
          <strong>{{ isH5NoBusinessResults ? '尚无有效质检结果' : '尚未产生可信采集' }}</strong>
          <p v-if="isH5NoBusinessResults">本次未完成规则质检，诊断证据保留在任务过程；不能作为业务质检报告或导出。</p>
          <p v-else>当前任务还没有可用于报告展示的持久化页面、动作、规则或覆盖事实。</p>
        </div>
        <button type="button" class="qc-btn" @click="goTaskDetail">查看任务</button>
      </section>

      <section
        v-if="insight.web_preview && insight.report_availability !== 'final'"
        class="workstation-glass-panel card web-preview-status"
        data-testid="web-preview-status"
      >
        <span>可信采集 <strong>{{ insight.web_preview.trusted_capture_count }}</strong></span>
        <span>
          质检进度
          <strong>{{ insight.web_preview.inspection_progress.completed }}/{{ insight.web_preview.inspection_progress.total }}</strong>
        </span>
        <span>页面类型待复核 <strong>{{ insight.web_preview.page_type_review.pending }}</strong></span>
        <span>阻断 <strong>{{ insight.web_preview.obstructions.length }}</strong></span>
        <span>完成校验缺口 <strong>{{ insight.web_preview.completion_gate.gaps.length }}</strong></span>
      </section>

      <!-- 任务基本信息 + 规则结果汇总 -->
      <div class="info-grid">
        <section class="workstation-glass-panel card" data-testid="task-info">
          <h2 class="card-title">任务基本信息</h2>
          <dl class="info-list">
            <template v-for="row in taskInfo" :key="row.label">
              <dt>{{ row.label }}</dt>
              <dd :class="{ strong: row.strong }">
                <span v-if="row.state" class="qc-chip" :class="reportPresentation.tone">
                  <i></i>{{ row.value }}
                </span>
                <template v-else>{{ row.value }}</template>
              </dd>
            </template>
          </dl>
        </section>

        <section class="workstation-glass-panel card" data-testid="result-summary">
          <h2 class="card-title">
            规则结果汇总
            <span class="qc-muted small">按规则条数算，一条规则只算一次；判了多次时只要有一次未通过就算未通过</span>
          </h2>
          <div class="tile-grid">
            <div
              v-for="tile in summaryTiles"
              :key="tile.key"
              class="tile"
              :class="[tile.tile, { clickable: !!tile.group, active: !!tile.group && resultFilter === tile.key }]"
              :role="tile.group ? 'button' : undefined"
              :tabindex="tile.group ? 0 : undefined"
              :data-testid="`tile-${tile.key}`"
              @click="tile.group && focusState(tile.group)"
              @keydown.enter.self="tile.group && focusState(tile.group)"
              @keydown.space.self.prevent="tile.group && focusState(tile.group)"
            >
              <div class="tile-head">
                <span :title="tile.group?.help">{{ tile.label }}</span>
                <button
                  type="button"
                  class="tile-info"
                  :aria-label="`查看${tile.label}说明`"
                  :data-testid="`status-help-${tile.key}`"
                  @click.stop="openTileInfo(tile)"
                >
                  !
                </button>
              </div>
              <strong>{{ tile.value }}</strong>
            </div>
          </div>
          <p class="summary-reconciliation" data-testid="summary-reconciliation">
            <span v-if="noExecutionRuleCount !== null" data-testid="summary-rule-accounting">
              规则总数 {{ insight.summary.ruleset_rule_total }} = 实际使用 {{ ruleSummary.usedRuleCount }} + 未实际使用 {{ noExecutionRuleCount }}。
            </span>
            <span>六类处置合计 {{ ruleSummary.ruleCount }} 条，与下方逐条清单一致；“实际使用”单独统计已实际判定的规则，未执行处置保留审计记录。</span>
            <span v-if="insight.summary.rule_inventory_total != null && noExecutionRuleCount != null && noExecutionRuleCount > 0">
              无执行记录的规则按逐条适用性核对和台账原因归类，核对处置不计为 AI 判定次数。
            </span>
            <span v-else-if="noExecutionRuleCount != null && noExecutionRuleCount > 0">
              无执行记录的规则没有结果分类，不能直接算作“本产品不适用”或“计划外未覆盖”。
            </span>
          </p>
          <p v-if="executionGapCount" class="qc-muted small" data-testid="execution-gap-note">
            <template v-if="greenIncomplete">有绿色规则尚未完整，详见分项及复核状态；已有明确失败保留，人工确认完成不代表材料与判读缺口已解决。</template>
            <template v-else>有 {{ executionGapCount }} 次检测未形成结论，详见列表执行状态；人工确认完成不代表这些检测缺口已解决。</template>
          </p>
        </section>
      </div>

      <p v-if="greenIncomplete" class="qc-muted small" data-testid="green-review-gap-note">
        当前绿色规则仍需复核、补齐分项或核对关联；无需业务确认不代表这些缺口已解决。
      </p>
      <!-- 人工确认规则 -->
      <ReportConfirmSection
        ref="confirmSection"
        :task-id="taskId"
        :items="confirmItems"
        @confirmed="loadAll(true)"
      >
        <template #thumb="{ item }">
          <ReportEvidenceThumb
            v-if="ruleScreenshotIds(item).length"
            :url="evidenceUrl(ruleScreenshotIds(item)[0])"
            :count="ruleScreenshotIds(item).length"
            @open="openViewerForRule(item)"
          />
          <span v-else class="thumb-empty">{{ screenshotsLoading ? '加载中' : screenshotsError || '无截图' }}</span>
        </template>
      </ReportConfirmSection>

      <!-- 规则执行结果列表：按几期规则集分组，一次展开一期 -->
      <section ref="tableRef" class="workstation-glass-panel table-card" data-testid="rule-table">
        <div class="qc-card-head">
          <h2>
            规则应用与执行清单
            <span class="qc-muted small">共 {{ matchedRuleCount }} 条规则 · 用了 {{ matchedUsageCount }} 次</span>
          </h2>
          <div class="table-filters">
            <SearchSelect v-model="severityFilter" data-testid="filter-severity">
              <option value="all">全部等级</option>
              <option v-for="option in severityOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
            </SearchSelect>
            <SearchSelect v-model="resultFilter" data-testid="filter-result">
              <option value="all">全部结果</option>
              <option v-for="group in RESULT_GROUPS" :key="group.key" :value="group.key">{{ group.label }}</option>
            </SearchSelect>
          </div>
        </div>
        <div v-if="visiblePhases.length" class="phase-list">
          <section
            v-for="entry in visiblePhases"
            :key="entry.group.key"
            class="phase-group"
            :class="{ 'is-open': effectiveOpenPhase === entry.group.key }"
            :data-testid="`phase-${entry.group.key}`"
          >
            <button
              type="button"
              class="phase-head"
              :aria-expanded="effectiveOpenPhase === entry.group.key"
              :data-testid="`phase-toggle-${entry.group.key}`"
              @click="togglePhase(entry.group.key)"
            >
              <span class="phase-caret" aria-hidden="true">›</span>
              <strong class="phase-name">{{ entry.group.name }}</strong>
              <span class="phase-meta">{{ entry.group.rules.length }} 条规则 · 用了 {{ entry.group.executionCount }} 次</span>
              <span class="phase-rate" :title="QC_RATE_HELP" data-testid="phase-rate">
                质检率 <b>{{ rateText(entry.group.conclusiveCount, entry.group.executionCount) }}</b>
              </span>
              <span v-if="entry.group.failedRuleCount" class="qc-chip tone-danger">未通过 {{ entry.group.failedRuleCount }} 条</span>
              <span v-if="entry.group.pendingRuleCount" class="qc-chip tone-muted">待人工确认 {{ entry.group.pendingRuleCount }} 条</span>
              <span v-if="filtersActive" class="phase-matched">筛出 {{ entry.rules.length }} 条</span>
            </button>
            <div v-if="effectiveOpenPhase === entry.group.key" class="phase-body">
              <div class="table-wrap">
                <table class="qc-table rule-table">
                  <thead>
                    <tr>
                      <th>规则名称</th>
                      <th>所属页面</th>
                      <th>所属规则包</th>
                      <th :title="QC_RATE_HELP">质检率</th>
                      <th>执行状态</th>
                      <th>AI 判定</th>
                      <th>最终结果</th>
                      <th>人工确认</th>
                      <th>截图证据</th>
                      <th class="is-right">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="row in phaseRules(entry)" :key="row.key" :data-testid="`rule-row-${row.rule_code}`">
                      <td class="name-cell">
                        <span class="severity-chip" :class="severityChipClass(row.lead)">{{ severityText(row.lead) }}</span>
                        <strong>{{ row.rule_name }}</strong>
                        <span class="qc-mono code-line">{{ row.rule_code }}</span>
                      </td>
                      <td class="muted-cell" :title="row.pageNames.join('、') || undefined">{{ pageNamesText(row.pageNames) }}</td>
                      <td class="muted-cell">{{ row.lead.rule_package || '-' }}</td>
                      <td class="rate-cell" data-testid="rule-rate">
                        <strong>{{ rateText(row.conclusiveCount, row.executionCount) }}</strong>
                        <span>{{ row.conclusiveCount }}/{{ row.executionCount }} 次</span>
                      </td>
                      <td class="muted-cell" data-testid="execution-state" :title="ruleExecutionStateDetail(row.items)">{{ ruleExecutionStateText(row.items) }}</td>
                      <td>
                        <span class="qc-chip" :class="aiResultOf(row.lead).tone" data-testid="ai-result">
                          {{ aiResultOf(row.lead).label }}
                        </span>
                      </td>
                      <td>
                        <span class="qc-chip" :class="finalResultOf(row.lead).tone" data-testid="final-result">
                          {{ finalResultOf(row.lead).label }}
                        </span>
                      </td>
                      <td class="muted-cell nowrap-cell" data-testid="confirm-status">{{ ruleConfirmText(row) }}</td>
                      <td>
                        <ReportEvidenceThumb
                          v-if="ruleRowShotIds(row).length"
                          :url="evidenceUrl(ruleRowShotIds(row)[0])"
                          :count="ruleRowShotIds(row).length"
                          @open="openShots(ruleRowShotIds(row), 0, row.rule_name, row.items)"
                        />
                        <span v-else-if="screenshotsLoading" class="qc-muted">加载中…</span>
                        <span v-else-if="screenshotsError" class="qc-muted">{{ screenshotsError }}</span>
                        <span
                          v-else-if="insight.summary.target_platform === 'h5' && ruleMissingEvidenceText(row)"
                          class="evidence-gap-label"
                          data-testid="evidence-gap"
                          :title="ruleMissingEvidenceText(row)"
                        >
                          缺少证据
                        </span>
                        <span v-else class="qc-muted">-</span>
                      </td>
                      <td class="is-right actions">
                        <button
                          type="button"
                          class="qc-link"
                          :data-testid="`rule-detail-${row.rule_code}`"
                          @click="openRuleDialog(row)"
                        >
                          查看详情
                        </button>
                        <button
                          v-if="judgeTarget(row)"
                          type="button"
                          class="qc-link qc-link-plain"
                          :data-testid="`row-judge-${row.rule_code}`"
                          @click="focusConfirmItem(judgeTarget(row)!)"
                        >
                          人工判定
                        </button>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <button
                v-if="entry.rules.length > PHASE_PREVIEW_SIZE"
                type="button"
                class="qc-link phase-more"
                :data-testid="`phase-more-${entry.group.key}`"
                @click="togglePhaseFull(entry.group.key)"
              >
                {{ fullPhases.has(entry.group.key) ? '收起' : `展开全部 ${entry.rules.length} 条规则` }}
              </button>
            </div>
          </section>
        </div>
        <p v-else class="qc-empty">没有匹配筛选条件的规则</p>
      </section>

      <!-- Web 证据说明（仅 Web 任务）：未发布原因、序列与逻辑页面属于排查用的技术明细，默认收起 -->
      <details
        v-if="showWebExplanation || webLogicalPages.length"
        class="workstation-glass-panel card web-evidence-details"
        data-testid="web-evidence-details"
      >
        <summary class="card-title">
          Web 证据说明 <span class="qc-muted small">未发布原因、采集序列与逻辑页面，排查用，默认收起</span>
        </summary>
      <WebReportExplanationPanel
        v-if="showWebExplanation"
        :explanation="insight.web_report_explanation"
        :identity-diagnostics="insight.web_identity_diagnostics"
      />

      <!-- Web 逻辑页面证据（仅 Web 任务） -->
      <section v-if="webLogicalPages.length" class="workstation-glass-panel card">
        <h2 class="card-title">Web 逻辑页面证据 <span class="qc-muted small">同一规范 URL 仅统计一次</span></h2>
        <div class="logical-list">
          <div v-for="pageItem in webLogicalPages" :key="pageItem.logical_page_id" class="logical-row">
            <span class="qc-chip" :class="pageItem.classification_status === 'classified' ? 'tone-success' : 'tone-warning'">
              {{ webLogicalPageLabel(pageItem) }}
            </span>
            <span class="qc-mono url">{{ pageItem.canonical_url }}</span>
            <span class="qc-muted small">
              证据 {{ pageItem.capture_count }} 版 · {{ pageItem.inspection_status || '待质检' }}
              <template v-if="pageItem.review_reason"> · {{ pageItem.review_reason }}</template>
            </span>
          </div>
        </div>
      </section>
      </details>

      <!-- 状态说明的规则清单，点一条再开规则详情 -->
      <RuleCategoryDialog
        v-model:show="categoryDialogVisible"
        :group="categoryGroup"
        :rules="categoryRules"
        @open-rule="openRuleDialog"
      />

      <!-- 规则详情：这条规则在这次任务里的每一次 -->
      <RuleUsageDialog
        v-model:show="ruleDialogVisible"
        :rule="ruleDialogRow"
        :target-platform="insight.summary.target_platform"
        :shot-ids="ruleScreenshotIds"
        :shot-url="evidenceUrl"
        :shots-loading="screenshotsLoading"
        :shots-error="screenshotsError"
        @open-shots="(ids, index) => openShots(ids, index, ruleDialogRow?.rule_name ?? '', ruleDialogRow?.items)"
        @green-view-evidence="openGreenSubitemShot"
        @judge="judgeFromDialog"
        @after-leave="onRuleDialogAfterLeave"
      />

      <!-- 所有汇总瓦片的「!」说明，结果类别保留查看规则清单入口 -->
      <n-modal v-model:show="summaryHelpVisible">
        <div v-if="summaryHelp" class="status-help" data-testid="status-help">
          <header class="status-help-head">
            <h3>{{ summaryHelp.title }}</h3>
            <button type="button" class="dialog-close" aria-label="关闭" @click="summaryHelpVisible = false">×</button>
          </header>
          <p v-for="(text, index) in summaryHelp.paragraphs" :key="index">{{ text }}</p>
          <footer class="status-help-foot">
            <button
              v-if="summaryHelp.group"
              type="button"
              class="qc-btn"
              data-testid="summary-view-rules"
              @click="openSummaryCategory"
            >查看规则清单</button>
            <button type="button" class="qc-btn qc-btn-primary" @click="summaryHelpVisible = false">知道了</button>
          </footer>
        </div>
      </n-modal>

      <!-- 截图证据大图 -->
      <n-modal v-model:show="viewerVisible">
        <div class="shot-viewer" data-testid="shot-viewer">
          <!-- 关联撤销时立即移除图片，保留弹窗根节点完成关闭与焦点清理。 -->
          <template v-if="viewerCurrent">
          <header class="shot-viewer-head">
            <div class="shot-viewer-title">
              <h3>截图证据 · {{ viewerRuleName }}</h3>
              <span>{{ viewerCurrent.page_name }} · {{ formatTime(viewerCurrent.captured_at) }}</span>
            </div>
            <div class="shot-viewer-actions">
              <div class="zoom-group" data-testid="viewer-zoom">
                <button type="button" class="zoom-btn" title="缩小" @click="zoomOut">−</button>
                <button type="button" class="zoom-btn zoom-label" :title="viewerZoom === null ? '切到原图尺寸' : '适应窗口'" @click="toggleZoomMode">
                  {{ viewerZoomLabel }}
                </button>
                <button type="button" class="zoom-btn" title="放大" @click="zoomIn">+</button>
              </div>
              <n-button size="small" secondary @click="downloadViewerCurrent">
                <template #icon>
                  <n-icon><DownloadOutline /></n-icon>
                </template>
                下载当前截图
              </n-button>
              <n-button size="small" secondary @click="viewerVisible = false">关闭</n-button>
            </div>
          </header>
          <div class="shot-stage" :class="{ zoomed: viewerZoom !== null }">
            <img
              :src="evidenceUrl(viewerCurrent.artifact_id)"
              :alt="viewerCurrent.page_name"
              :style="viewerImageStyle"
              @load="onViewerImageLoad"
            />
          </div>
          <div class="shot-viewer-nav">
            <button type="button" class="qc-btn qc-btn-sm" :disabled="viewerAtFirst" data-testid="viewer-prev" @click="viewerPrev">
              <n-icon size="14"><ChevronBackOutline /></n-icon>
              上一张
            </button>
            <span class="qc-mono" data-testid="viewer-count">{{ viewerIndex + 1 }} / {{ viewerItems.length }}</span>
            <button type="button" class="qc-btn qc-btn-sm" :disabled="viewerAtLast" data-testid="viewer-next" @click="viewerNext">
              下一张
              <n-icon size="14"><ChevronForwardOutline /></n-icon>
            </button>
          </div>
          <div v-if="viewerItems.length > 1" class="shot-strip">
            <button
              v-for="(entry, index) in viewerItems"
              :key="entry.artifact_id"
              type="button"
              class="strip-thumb"
              :class="{ active: index === viewerIndex }"
              :title="`第 ${index + 1} 张 · ${entry.page_name}`"
              @click="viewerIndex = index"
            >
              <img :src="evidenceUrl(entry.artifact_id)" :alt="entry.page_name" loading="lazy" />
            </button>
          </div>
          <footer v-if="viewerCurrent.related_rules.length" class="shot-viewer-foot">
            <span class="qc-muted small">关联规则：</span>
            <span
              v-for="rule in viewerCurrent.related_rules"
              :key="rule.rule_code"
              class="qc-chip"
              :class="resultGroupOf(rule.display_status).tone"
            >
              {{ rule.rule_name }} · {{ rule.display_status_label }}
            </span>
          </footer>
          <p class="viewer-hint">← → 翻页 · + − 缩放 · 0 适应窗口 · Esc 关闭</p>
          </template>
        </div>
      </n-modal>
    </template>
  </div>
</template>

<style scoped>
.web-evidence-details > summary {
  cursor: pointer;
  list-style: revert;
}

.report-detail-page {
  gap: 24px;
}

.state-panel {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 14px;
  padding: 60px 24px;
  color: var(--workstation-text-muted);
  text-align: center;
}

.state-panel h3 {
  margin: 0;
  color: var(--workstation-text-strong);
}

.state-panel p {
  margin: 0;
}

.state-actions {
  display: flex;
  gap: 10px;
}

/* ---------- 标题行 ---------- */

.report-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.report-header h1 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 18px;
  font-weight: 600;
}

.report-header p {
  margin: 4px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.qc-btn.is-disabled {
  opacity: 0.55;
}

.notice-row {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: -12px;
}

.notice {
  padding: 6px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text-muted);
  background: var(--workstation-panel);
  font-size: 12px;
}

.notice-final {
  border-color: var(--tint-emerald-line);
  color: var(--workstation-success-strong);
  background: var(--tint-success);
}

.notice-final_review {
  color: var(--workstation-warning-strong);
  background: var(--tint-warning);
}

.notice-preview {
  color: var(--workstation-primary);
  background: var(--tint-primary);
}

.report-none-notice {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border-color: var(--workstation-warning);
}

.report-none-notice strong {
  color: var(--workstation-text-strong);
}

.report-none-notice p {
  margin: 4px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.web-preview-status {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
}

.web-preview-status span {
  display: grid;
  gap: 4px;
  padding: 10px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text-muted);
  background: var(--tint-primary);
  font-size: 12px;
}

.web-preview-status strong {
  color: var(--workstation-text-strong);
  font-size: 15px;
}

/* ---------- 信息卡 ---------- */

.info-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
}

.card {
  padding: 20px;
}

.card-title {
  display: flex;
  align-items: baseline;
  gap: 8px;
  margin: 0 0 12px;
  color: var(--workstation-text);
  font-size: 14px;
  font-weight: 500;
}

.small {
  font-size: 12px;
  font-weight: 400;
}

.info-list {
  display: grid;
  grid-template-columns: 96px minmax(0, 1fr);
  gap: 12px 8px;
  margin: 0;
  font-size: 14px;
}

.info-list dt {
  color: var(--workstation-text-muted);
}

.info-list dd {
  margin: 0;
  color: var(--workstation-text-strong);
  word-break: break-all;
}

.info-list dd.strong {
  font-weight: 500;
}

/* 结果瓦片：p-4 rounded-lg + 淡底色；数字 text-2xl；六类结果右上角带「!」说明 */
.summary-reconciliation {
  margin: 12px 0 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
  line-height: 1.65;
}

.summary-reconciliation span {
  display: block;
}

.tile-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.tile {
  display: flex;
  flex-direction: column;
  padding: 16px;
  border: 1px solid transparent;
  border-radius: var(--workstation-radius-sm);
  text-align: left;
}

.tile.clickable {
  cursor: pointer;
}

.tile.clickable:focus-visible {
  outline: 2px solid var(--workstation-primary);
  outline-offset: 1px;
}

.tile-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
}

.tile-head span {
  font-size: 12px;
}

.tile-info {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 1px solid currentColor;
  border-radius: 50%;
  color: inherit;
  background: transparent;
  font: inherit;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
}

.tile strong {
  margin-top: 4px;
  font-size: 24px;
  font-weight: 600;
  line-height: 1.3;
}

.tile.active {
  border-color: currentColor;
}

.tile-neutral {
  background: var(--tint-neutral);
  color: var(--workstation-text-muted);
}

.tile-neutral strong {
  color: var(--workstation-text);
}

.tile-success {
  background: var(--tint-success);
  color: var(--workstation-success);
}

.tile-success strong {
  color: var(--workstation-success-strong);
}

.tile-danger {
  background: var(--tint-danger);
  color: var(--workstation-danger);
}

.tile-danger strong {
  color: var(--workstation-danger-strong);
}

.tile-warning {
  background: var(--tint-warning);
  color: var(--workstation-warning);
}

.tile-warning strong {
  color: var(--workstation-warning-strong);
}

.tile-orange {
  background: var(--tint-orange);
  color: var(--workstation-orange);
}

.tile-orange strong {
  color: var(--workstation-orange-strong);
}

.tile-violet {
  background: rgba(124, 58, 237, 0.08);
  color: var(--workstation-violet);
}

.tile-violet strong {
  color: var(--workstation-violet);
}

.tile-primary {
  background: var(--tint-primary);
  color: var(--workstation-primary);
}

.tile-primary strong {
  color: var(--workstation-primary-strong);
}

/* 人工确认卡片里没有截图时占位，保持文字对齐 */
.thumb-empty {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 48px;
  height: 64px;
  border: 1px dashed var(--workstation-border);
  border-radius: 6px;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.evidence-gap-label {
  color: #b45309;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}

.logical-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.logical-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}

.logical-row .url {
  color: var(--workstation-text);
  word-break: break-all;
}

/* ---------- 规则执行结果列表 ---------- */

.table-card {
  overflow: hidden;
}

.table-filters {
  display: flex;
  align-items: center;
  gap: 8px;
}

.table-filters .search-select {
  width: 150px;
}

.phase-list {
  display: grid;
  /* 表格比卡片宽时在表格里横向滚动，不把整期撑出卡片被裁掉 */
  grid-template-columns: minmax(0, 1fr);
  gap: 10px;
  padding: 16px 20px 20px;
}

.phase-group {
  min-width: 0;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel);
}

.phase-group.is-open {
  border-color: var(--workstation-primary);
}

.phase-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  width: 100%;
  padding: 14px 16px;
  border: 0;
  background: transparent;
  color: var(--workstation-text);
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}

.phase-caret {
  display: inline-block;
  color: var(--workstation-text-muted);
  font-size: 16px;
  transition: transform 0.15s ease;
}

.phase-group.is-open .phase-caret {
  transform: rotate(90deg);
}

.phase-name {
  color: var(--workstation-text-strong);
  font-size: 14px;
}

.phase-meta,
.phase-matched {
  color: var(--workstation-text-muted);
}

.phase-rate b {
  color: var(--workstation-primary-strong);
  font-size: 15px;
}

.phase-body {
  border-top: 1px solid var(--workstation-border);
}

.phase-more {
  display: block;
  margin: 10px auto 12px;
}

.table-wrap {
  overflow-x: auto;
}

/* 九列在常见宽度（1440）下放得下，不出横向滚动条 */
.rule-table th,
.rule-table td {
  padding-left: 12px;
  padding-right: 12px;
}

.rule-table td {
  vertical-align: top;
}

.name-cell {
  min-width: 200px;
}

.name-cell strong {
  display: block;
  margin-top: 4px;
  color: var(--workstation-text-strong);
  font-weight: 500;
}

.code-line {
  display: block;
  margin-top: 2px;
  color: var(--workstation-text-muted);
  font-size: 11px;
}

.muted-cell {
  color: var(--workstation-text-muted);
}

.rate-cell {
  white-space: nowrap;
}

.rate-cell strong {
  display: block;
  color: var(--workstation-text-strong);
}

.rate-cell span {
  display: block;
  margin-top: 4px;
  color: var(--workstation-text-muted);
  font-size: 11px;
}

.actions,
.nowrap-cell {
  white-space: nowrap;
}

.actions .qc-link + .qc-link {
  margin-left: 12px;
}

.qc-link.qc-link-plain {
  color: var(--workstation-text-muted);
}

.qc-link.qc-link-plain:hover {
  color: var(--workstation-text-strong);
}

/* ---------- 状态说明弹窗 ---------- */

.status-help {
  width: 92vw;
  max-width: 448px;
  padding: 20px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  color: var(--workstation-text);
  background: var(--workstation-panel-strong);
  box-shadow: var(--workstation-shadow-lg);
}

.status-help-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.status-help-head h3 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 15px;
  font-weight: 600;
}

.status-help p {
  margin: 12px 0 0;
  color: var(--workstation-text-muted);
  font-size: 14px;
  line-height: 1.75;
}

.status-help-foot {
  display: flex;
  justify-content: flex-end;
  margin-top: 20px;
}

.dialog-close {
  padding: 0 4px;
  border: 0;
  color: var(--workstation-text-faint);
  background: transparent;
  font-size: 20px;
  line-height: 1;
  cursor: pointer;
}

.dialog-close:hover {
  color: var(--workstation-text-strong);
}

/* ---------- 大图查看器 ---------- */

.shot-viewer {
  display: flex;
  flex-direction: column;
  gap: 12px;
  width: 94vw;
  max-width: min(1100px, 94vw);
  max-height: 92vh;
  padding: 18px 22px;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius);
  color: var(--workstation-text);
  background: var(--workstation-panel-strong);
  box-shadow: var(--workstation-shadow-lg);
}

.shot-viewer-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.shot-viewer-title h3 {
  margin: 0;
  color: var(--workstation-text-strong);
  font-size: 15px;
  font-weight: 600;
}

.shot-viewer-title span {
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.shot-viewer-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.zoom-group {
  display: inline-flex;
  border: 1px solid var(--workstation-border);
  border-radius: var(--workstation-radius-sm);
  overflow: hidden;
}

.zoom-btn {
  min-width: 30px;
  padding: 5px 8px;
  border: 0;
  color: var(--workstation-text);
  background: var(--workstation-panel-soft);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.zoom-btn + .zoom-btn {
  border-left: 1px solid var(--workstation-border);
}

.zoom-label {
  min-width: 82px;
}

.shot-stage {
  display: flex;
  align-items: center;
  justify-content: center;
  max-height: 62vh;
  min-height: 0;
  padding: 16px;
  border-radius: var(--workstation-radius-sm);
  background: var(--workstation-panel-soft);
  overflow: auto;
}

.shot-stage img {
  max-width: 100%;
  max-height: calc(62vh - 32px);
  border-radius: var(--workstation-radius-sm);
  object-fit: contain;
}

.shot-stage.zoomed {
  align-items: flex-start;
  justify-content: flex-start;
}

.shot-stage.zoomed img {
  flex-shrink: 0;
  max-width: none;
  max-height: none;
}

.shot-viewer-nav {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--workstation-text-muted);
}

.shot-strip {
  display: flex;
  gap: 8px;
  padding: 2px;
  overflow-x: auto;
}

.strip-thumb {
  flex-shrink: 0;
  width: 48px;
  height: 64px;
  padding: 0;
  border: 2px solid transparent;
  border-radius: var(--workstation-radius-sm);
  background: none;
  overflow: hidden;
  cursor: pointer;
}

.strip-thumb.active {
  border-color: var(--workstation-primary);
}

.strip-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  object-position: top;
}

.shot-viewer-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid var(--workstation-border);
}

.viewer-hint {
  margin: 0;
  color: var(--workstation-text-faint);
  font-size: 12px;
  text-align: center;
}

@media (max-width: 1080px) {
  .info-grid {
    grid-template-columns: 1fr;
  }

  .tile-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .report-header {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
