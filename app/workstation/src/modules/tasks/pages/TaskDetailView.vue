<script setup lang="ts">
import WebReportExplanationPanel from '@/components/WebReportExplanationPanel.vue'
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { usePortalNav } from '@/composables/usePortalNav'
import { NButton, NIcon, NPopconfirm, NSpin, useMessage } from 'naive-ui'
import {
  ChevronForwardOutline,
  ClipboardOutline,
  CloseCircleOutline,
  CopyOutline,
  DocumentTextOutline,
  DownloadOutline,
  ImageOutline,
  RefreshOutline,
  SearchOutline,
  TerminalOutline,
  WarningOutline,
} from '@vicons/ionicons5'
import PageExplorationPanel from '@/components/PageExplorationPanel.vue'
import WebStageDurationPanel from '@/components/WebStageDurationPanel.vue'
import { useEvidenceFileUrls } from '@/composables/useEvidenceFileUrls'
import AppStagePanel from '@/modules/tasks/components/AppStagePanel.vue'
import PageGraphPanel from '@/modules/tasks/components/PageGraphPanel.vue'
import ClosureOutcomePanel from '@/modules/tasks/components/ClosureOutcomePanel.vue'
import ExplorationProcessPanel from '@/modules/tasks/components/ExplorationProcessPanel.vue'
import PageDetailDialog from '@/modules/tasks/components/PageDetailDialog.vue'
import ScreenshotEvidenceViewer from '@/modules/tasks/components/ScreenshotEvidenceViewer.vue'
import H5CheckpointPanel from '@/modules/tasks/components/H5CheckpointPanel.vue'
import { isLinkedGreenResult } from '@/modules/tasks/components/greenRuleResultDetails'
import {
  buildScreenshotCollection,
  type ScreenshotViewerEntry,
} from '@/modules/tasks/components/screenshotCollection'
import {
  buildPageStory,
  buildTaskLevelStory,
  nodeCaptureIds,
  tallyRules,
  tallySummaryText,
} from '@/modules/tasks/components/pageStory'
import type {
  ClosureOutcome,
  EvidenceGaps,
  PageGraph,
  PageTypeJudgements,
  RecoveryRoutes,
  RuleReuse,
} from '@/types/task-insight'
import DeliveryIncidentPanel from '@/modules/tasks/components/DeliveryIncidentPanel.vue'
import { pageStateName, pageTypeName, pageTypeNames, reasonText } from '@/utils/exploration-labels'
import { getAgentRun, getTask, performTaskOperation } from '@/services/tasks.api'
import { getSystemStatus, type SystemStatus } from '@/services/system.api'
import { authStore } from '@/stores/auth'
import {
  downloadLogsExport,
  downloadPlatformProgressExport,
  downloadScreenshotsArchive,
  getAppStage,
  getEvidenceGaps,
  getClosureOutcome,
  getPageTypeJudgements,
  getRuleReuse,
  getInspectionWorkItems,
  getPageGraph,
  getPageRecoveryRoutes,
  getPageTimeline,
  getRuleResults,
  getTaskLogs,
  getTaskScreenshots,
  getTaskStats,
} from '@/services/task-insight.api'
import type { AgentRunDetail, AgentRunStep, WorkstationTaskDetail } from '@/types/task'
import type {
  AppStageProjection,
  ExplorationActionItem,
  H5InspectionPublicationProjection,
  InspectionWorkItem,
  InspectionWorkItems,
  PageTimeline,
  PageTimelineNode,
  RuleDisplayStatus,
  RuleResultItem,
  ScreenshotGroup,
  ScreenshotItem,
  TaskLogItem,
  TaskScreenshots,
  TaskStats,
} from '@/types/task-insight'
import { ruleExecutionStatusLabel, severityClass, taskStatusLabel } from '@/utils/status'
import { taskListReturnQuery } from '../taskListState'
import { countsAsQualityExecution } from '@/utils/rule-execution'
import { executionDurationMs, isLongExecution } from '../taskRuntime'

type TimelineTone = 'running' | 'pass' | 'warning' | 'error' | 'pending'

const route = useRoute()
const nav = usePortalNav()
const message = useMessage()
const evidenceFiles = useEvidenceFileUrls()

function returnToTaskList() {
  const query = taskListReturnQuery(route.query)
  void nav.push(query ? { path: '/tasks', query } : '/tasks')
}

const taskId = computed(() => route.params.taskId?.toString() || '')
const task = ref<WorkstationTaskDetail | null>(null)
const agentRun = ref<AgentRunDetail | null>(null)
const timeline = ref<PageTimeline | null>(null)
const appStage = ref<AppStageProjection | null>(null)
const inspectionWorkItems = ref<InspectionWorkItems | null>(null)
const ruleItems = ref<RuleResultItem[]>([])
const stats = ref<TaskStats | null>(null)
const screenshots = ref<TaskScreenshots | null>(null)
// 探索过程展示：页面状态图 / 只读恢复路线 / 证据缺口台账（只读投影）
const pageGraph = ref<PageGraph | null>(null)
const recoveryRoutes = ref<RecoveryRoutes | null>(null)
const evidenceGaps = ref<EvidenceGaps | null>(null)
const ruleReuse = ref<RuleReuse | null>(null)
const pageTypeJudgements = ref<PageTypeJudgements | null>(null)
const closureOutcome = ref<ClosureOutcome | null>(null)
const logItems = ref<TaskLogItem[]>([])
const logTotal = ref(0)
const logLimit = ref(100)
const systemStatus = ref<SystemStatus | null>(null)
const loading = ref(true)
/**
 * 各明细区块是否还在首次加载：数据多时要等一会儿，加载中显示「加载中…」，
 * 不能先显示「暂无…」让人以为没有数据。哪块先回来就先显示哪块。
 */
type DetailSection = 'timeline' | 'rules' | 'stats' | 'screenshots' | 'logs' | 'workItems' | 'appStage'
const sectionLoading = reactive<Record<DetailSection, boolean>>({
  timeline: false,
  rules: false,
  stats: false,
  screenshots: false,
  logs: false,
  workItems: false,
  appStage: false,
})
const loadError = ref('')
const partialLoadError = ref('')
const operating = ref(false)
const logsExpanded = ref(false)
const traceExpanded = ref(false)
// 研发排查区默认收起，展开才渲染（工作项、日志动辄几万字）
const diagnosticsExpanded = ref(false)
const timelineExpanded = ref(false)
const TIMELINE_PREVIEW_LIMIT = 12

let pollTimer: number | undefined
let clockTimer: number | undefined
let overviewRequestSerial = 0
let secondaryRequestSerial = 0
let secondaryRequest: {
  taskId: string
  serial: number
  snapshotKey: string
  refreshPending: boolean
} | null = null
let disposed = false
const nowTick = ref(Date.now())

function detailSnapshotKey(detail: WorkstationTaskDetail) {
  return JSON.stringify([
    detail.run_id,
    detail.status,
    detail.run_status,
    detail.execution_status,
    detail.finalization_state,
    detail.report_status,
    detail.report_availability,
  ])
}

async function loadAll(silent = false) {
  const requestedTaskId = taskId.value
  const requestSerial = ++overviewRequestSerial
  if (!requestedTaskId) {
    loadError.value = '缺少任务编号'
    loading.value = false
    return
  }
  if (!silent) {
    secondaryRequest = null
    secondaryRequestSerial += 1
    loading.value = true
    loadError.value = ''
    partialLoadError.value = ''
    // 切换任务或手动刷新先收起旧明细和原图，迟到的票据不能恢复旧任务的展示。
    pageDetailVisible.value = false
    pageDetailKey.value = null
    viewerVisible.value = false
    viewerSelection.value = null
    task.value = null
    agentRun.value = null
    timeline.value = null
    appStage.value = null
    inspectionWorkItems.value = null
    ruleItems.value = []
    stats.value = null
    screenshots.value = null
    pageGraph.value = null
    recoveryRoutes.value = null
    evidenceGaps.value = null
    ruleReuse.value = null
    pageTypeJudgements.value = null
    closureOutcome.value = null
    evidenceFiles.clear()
    logItems.value = []
    logTotal.value = 0
  }
  try {
    const detail = await getTask(requestedTaskId)
    if (disposed || taskId.value !== requestedTaskId || requestSerial !== overviewRequestSerial) {
      return
    }
    if (task.value && detailSnapshotKey(task.value) !== detailSnapshotKey(detail)) {
      // 新执行批次的概览先到时，先撤下旧绿色结果；新明细慢或失败也不能借用上一批结论。
      ruleItems.value = ruleItems.value.filter((item) => !item.green_result)
      if (viewerSelection.value?.kind === 'rule' && viewerSelection.value.greenContractHash !== undefined) {
        viewerVisible.value = false
        viewerSelection.value = null
      }
    }
    task.value = detail
    loadError.value = ''
    loading.value = false
    // 概览状态单独轮询，规则、截图等明细仍加载时也能及时收起失效操作。
    scheduleNextPoll()
    syncClock()
    const snapshotKey = detailSnapshotKey(detail)
    if (secondaryRequest?.taskId === requestedTaskId) {
      // 运行批次或收口状态已变化时，旧明细不再落到新概览；等当前批次完成后补一次读取。
      if (secondaryRequest.snapshotKey !== snapshotKey) secondaryRequest.refreshPending = true
      return
    }
    const sectionSerial = ++secondaryRequestSerial
    secondaryRequest = {
      taskId: requestedTaskId,
      serial: sectionSerial,
      snapshotKey,
      refreshPending: false,
    }
    const sectionCurrent = () =>
      !disposed && taskId.value === requestedTaskId && secondaryRequestSerial === sectionSerial &&
      secondaryRequest?.serial === sectionSerial && !secondaryRequest.refreshPending

    try {
      // 明细接口各自回来各自显示：一个接口慢（数据多时要几分钟）不拖住其它区块
      if (!silent) {
        for (const key of Object.keys(sectionLoading) as DetailSection[]) {
          sectionLoading[key] = true
        }
      }
      const fill = <T,>(request: Promise<T>, apply: (value: T) => void, section?: DetailSection) =>
        Promise.resolve(request)
          .then((value) => {
            if (sectionCurrent()) apply(value)
          })
          .finally(() => {
            if (section && sectionCurrent()) sectionLoading[section] = false
          })
      // 第一波只拉时间轴/截图并签缩略图票：浏览器同域连接约 6 条，若与十余个慢明细并行，
      // 66 张票会饿死在队列里，时间轴长期只剩 ImageOutline 占位。
      const thumbWaveResults = await Promise.allSettled([
        fill(getPageTimeline(requestedTaskId), (value) => {
          timeline.value = value
        }, 'timeline'),
        fill(getTaskScreenshots(requestedTaskId), (value) => {
          screenshots.value = value
        }, 'screenshots'),
      ])
      if (sectionCurrent()) {
        const previewIds = visibleTimelineNodes.value
          .map((node) => node.screenshot_artifact_id)
          .filter((id): id is string => Boolean(id))
        await evidenceFiles.load(currentEvidenceArtifactIds(), { priorityIds: previewIds })
      }
      if (!sectionCurrent()) {
        return
      }
      const secondaryResults = await Promise.allSettled([
        fill(getAgentRun(requestedTaskId), (value) => {
          agentRun.value = value.run
        }),
        fill(getRuleResults(requestedTaskId), (value) => {
          ruleItems.value = value.items
        }, 'rules'),
        fill(getTaskStats(requestedTaskId), (value) => {
          stats.value = value
        }, 'stats'),
        fill(getTaskLogs(requestedTaskId, { pageSize: logLimit.value }), (value) => {
          logItems.value = value.items
          logTotal.value = value.total
        }, 'logs'),
        fill(getAppStage(requestedTaskId), (value) => {
          appStage.value = value
        }, 'appStage'),
        fill(getInspectionWorkItems(requestedTaskId), (value) => {
          inspectionWorkItems.value = value
        }, 'workItems'),
        fill(getPageGraph(requestedTaskId), (value) => {
          pageGraph.value = value
        }),
        fill(getPageRecoveryRoutes(requestedTaskId), (value) => {
          recoveryRoutes.value = value
        }),
        fill(getEvidenceGaps(requestedTaskId), (value) => {
          evidenceGaps.value = value
        }),
        fill(getRuleReuse(requestedTaskId), (value) => {
          ruleReuse.value = value
        }),
        fill(getPageTypeJudgements(requestedTaskId), (value) => {
          pageTypeJudgements.value = value
        }),
        fill(getClosureOutcome(requestedTaskId), (value) => {
          closureOutcome.value = value
        }),
      ])
      if (!sectionCurrent()) {
        return
      }
      void evidenceFiles.load(currentEvidenceArtifactIds())
      const failedSectionCount = [...thumbWaveResults, ...secondaryResults].filter(
        (result) => result.status === 'rejected',
      ).length
      partialLoadError.value =
        failedSectionCount > 0
          ? `任务概览已加载，另有 ${failedSectionCount} 个明细模块加载失败，可稍后刷新重试`
          : ''

      if (detail.status === 'queued' && canReadSystemStatus.value) {
        // 排队中额外拉系统状态：Worker 挂掉/队列积压时页面必须给出提示，
        // 而不是让任务无限"排队中"装死。失败不阻塞详情展示。
        // 系统状态只对平台管理员开放，读不到的账号不请求（请求必然 403）。
        try {
          const status = await getSystemStatus()
          if (sectionCurrent() && task.value?.status === 'queued') systemStatus.value = status
        } catch {
          if (sectionCurrent()) systemStatus.value = null
        }
      } else {
        systemStatus.value = null
      }
    } finally {
      if (secondaryRequest?.serial === sectionSerial) {
        const refreshPending = secondaryRequest.refreshPending
        secondaryRequest = null
        // 终态会停止定时轮询，仍需把在途旧批次收束到终态后的权威明细；不并发重复请求。
        if (refreshPending && !disposed && taskId.value === requestedTaskId) void loadAll(true)
      }
    }
  } catch (error) {
    if (disposed || taskId.value !== requestedTaskId || requestSerial !== overviewRequestSerial) return
    const messageText = error instanceof Error ? error.message : '任务详情加载失败'
    if (task.value && silent) {
      partialLoadError.value = messageText
    } else {
      loadError.value = messageText
    }
  } finally {
    if (!disposed && taskId.value === requestedTaskId && requestSerial === overviewRequestSerial) {
      loading.value = false
      scheduleNextPoll()
      syncClock()
    }
  }
}

function scheduleNextPoll() {
  window.clearTimeout(pollTimer)
  const status = task.value?.status
  if (!disposed && !loadError.value && ['queued', 'running', 'canceling'].includes(status ?? '')) {
    pollTimer = window.setTimeout(() => void loadAll(true), 3000)
  }
}

function syncClock() {
  window.clearInterval(clockTimer)
  if (task.value?.status === 'running') {
    clockTimer = window.setInterval(() => {
      nowTick.value = Date.now()
    }, 1000)
  }
}

onMounted(() => void loadAll())
onBeforeUnmount(() => {
  disposed = true
  window.clearTimeout(pollTimer)
  window.clearInterval(clockTimer)
})
watch(taskId, () => void loadAll())

// 展开时间轴或截图集变更时补签缩略图票（不依赖明细波次是否被 refreshPending 打断）。
watch([timeline, screenshots, timelineExpanded], () => {
  const ids = currentEvidenceArtifactIds()
  if (!ids.length) return
  const previewIds = visibleTimelineNodes.value
    .map((node) => node.screenshot_artifact_id)
    .filter((id): id is string => Boolean(id))
  void evidenceFiles.load(ids, { priorityIds: previewIds })
})

const canCancel = computed(() => task.value?.available_actions.includes('cancel') ?? false)
const canRerun = computed(() => task.value?.available_actions.includes('rerun') ?? false)
const canRetryFinalization = computed(
  () => task.value?.available_actions.includes('retry-finalization') ?? false,
)
const isExplorationOnly = computed(() => task.value?.execution_mode === 'exploration_only')
const isWebV2FullInspection = computed(() => {
  const current = task.value
  return (
    current?.target_platform === 'web' &&
    current.execution_mode === 'full_inspection' &&
    current.workflow_version?.toLowerCase() === 'v2'
  )
})
const isH5ExplorationOnly = computed(() => {
  const current = task.value
  if (!current) {
    return false
  }
  return current.target_platform === 'h5' && isExplorationOnly.value
})
const isReportPreview = computed(() => task.value?.report_availability === 'preview')
const canViewReport = computed(() => {
  const current = task.value
  if (isWebV2FullInspection.value) {
    return Boolean(
      current?.report_available === true ||
        current?.report_availability === 'preview' ||
        current?.report_availability === 'final',
    )
  }
  return Boolean(
    current &&
      !isExplorationOnly.value &&
      current.available_actions.includes('report') &&
      ['ready', 'generated'].includes(current.report_status),
  )
})
const reportUnavailableReason = computed(() =>
  isWebV2FullInspection.value && task.value?.report_availability === 'none'
    ? '尚未产生可信采集，暂时没有可预览的报告'
    : null,
)

const webExecutionPhaseLabel = computed(() => {
  const phase = task.value?.execution_phase
  return phase
    ? ({
        queued: task.value?.target_platform === 'web' ? '已进入 Web 专属队列' : '已进入 App / H5 执行队列',
        pending: '等待执行调度确认',
        running: task.value?.target_platform === 'web' ? '正在探索与留证' : '任务执行中',
        configuration_error: '运行配置错误',
        navigation_failed: '页面导航失败',
        evidence_failed: task.value?.target_platform === 'web' ? '证据留存失败' : '执行异常',
        classification_review: task.value?.target_platform === 'web' ? '页面类型待复核' : '任务待复核',
        done: task.value?.target_platform === 'web' ? '探索已完成' : '执行已结束',
        canceled: task.value?.target_platform === 'web' ? '探索已取消' : '本次执行已取消',
      } as Record<string, string>)[phase] || phase
    : null
})

const canOperateDeliveryIncidents = computed(
  () =>
    (task.value?.target_platform === 'h5' || task.value?.target_platform === 'app') &&
    (authStore.user.value?.permissions.includes('systemOperations') ?? false),
)

/** 执行服务心跳属于运维数据，后端只对平台管理员（systemOperations）开放。 */
const canReadSystemStatus = computed(
  () => authStore.user.value?.permissions.includes('systemOperations') ?? false,
)

const QUEUED_ALERT_THRESHOLD_SECONDS = 120

const schedulerAlert = computed<{ tone: 'error' | 'warning'; title: string; detail: string } | null>(
  () => {
    if (!task.value || task.value.status !== 'queued') {
      return null
    }
    const status = systemStatus.value
    if (status && !status.worker.alive) {
      const redisHint = status.queue.redis_available
        ? ''
        : '（Redis 亦不可达，请一并检查）'
      return {
        tone: 'error',
        title: '质检 Worker 未运行，任务不会被执行',
        detail:
          `未检测到 Worker 心跳${redisHint}。请检查 alioth-worker 容器是否存活、启动日志是否有` +
          '自检报错（如规则引擎配置校验失败会导致启动即崩、任务永远停留在排队中）。',
      }
    }
    const waitingSeconds = Math.floor(
      (nowTick.value - new Date(task.value.created_at).getTime()) / 1000,
    )
    if (waitingSeconds > QUEUED_ALERT_THRESHOLD_SECONDS) {
      const minutes = Math.max(1, Math.floor(waitingSeconds / 60))
      const depth = status?.queue.depth
      const depthHint = depth != null ? `当前队列深度 ${depth}，` : ''
      return {
        tone: 'warning',
        title: `任务已排队约 ${minutes} 分钟仍未开始`,
        // 读不到服务状态时不下「Worker 存活」的结论
        detail: status
          ? `Worker 存活（实例 ${status.worker.active_instances ?? '?'} 个），${depthHint}` +
            '心跳在线不代表当前有空闲执行槽，请核对下方真实队列位置和启动条件；需要终止等待时可取消任务。'
          : '请核对下方真实队列位置和启动条件；需要终止等待时可取消任务。',
      }
    }
    return null
  },
)

async function handleCancel() {
  operating.value = true
  try {
    const result = await performTaskOperation(taskId.value, 'cancel')
    message.success(result.message || '任务取消操作已受理')
    await loadAll(true)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '任务取消失败')
    // 取消与结束会并发，失败后也读取当前权威状态，不能留下已过期的取消入口。
    await loadAll(true)
  } finally {
    operating.value = false
  }
}

async function handleRerun() {
  operating.value = true
  try {
    const result = await performTaskOperation(taskId.value, 'rerun')
    message.success(result.message || '任务重新执行已受理')
    if (result.task_id && result.task_id !== taskId.value) {
      const path = `/tasks/${result.task_id}`
      const query = taskListReturnQuery(route.query)
      await nav.push(query ? { path, query } : path)
    } else {
      await loadAll(true)
    }
  } catch (error) {
    message.error(error instanceof Error ? error.message : '任务重新执行失败')
  } finally {
    operating.value = false
  }
}

async function handleRetryFinalization() {
  operating.value = true
  try {
    const result = await performTaskOperation(taskId.value, 'retry-finalization')
    message.success(result.message || '正式报告收口重试已恢复')
    await loadAll(true)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '正式报告收口恢复失败')
  } finally {
    operating.value = false
  }
}

function viewReport() {
  // 报告中心是"单任务报告页"：带 task 参数跳过去并自动选中当前任务。
  void nav.push({ path: '/reports', query: { ...taskListReturnQuery(route.query), task: taskId.value } })
}

const canTestRules = computed(
  () => nav.isAdmin.value && (authStore.user.value?.permissions.includes('qcRules:write') ?? false),
)

function openIndependentRuleTest() {
  void nav.push({ path: '/rule-test', query: { task: taskId.value } })
}

async function exportPlatformProgress() {
  try {
    await downloadPlatformProgressExport(taskId.value)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '平台进展导出失败')
  }
}

function legacyCopy(text: string): boolean {
  const textarea = document.createElement('textarea')
  textarea.value = text
  // readonly 防 iOS 弹键盘；不能用 display:none，隐藏后无法选中
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.focus()
  textarea.select()
  // iOS Safari 的 select() 不生效，需显式设置选区
  textarea.setSelectionRange(0, text.length)
  let copied = false
  try {
    copied = document.execCommand('copy')
  } catch {
    copied = false
  }
  document.body.removeChild(textarea)
  return copied
}

const displayTargetUrl = computed(() => {
  const current = task.value
  if (!current) {
    return ''
  }
  const url = (current.target_url || '').trim()
  if (url) {
    return url
  }
  return 'App 质检（无 H5 URL）'
})

const isAppTask = computed(() => {
  const current = task.value
  if (!current) {
    return false
  }
  if (appStage.value?.applicable) {
    return true
  }
  return !(current.target_url || '').trim()
})

async function copyTargetUrl() {
  if (!task.value) {
    return
  }
  const targetLabel = displayTargetUrl.value
  if (!targetLabel) {
    message.warning('当前 App 任务没有可复制的目标链接')
    return
  }
  const successMessage = task.value.target_url ? '目标链接已复制' : '目标标识已复制'
  // HTTP（非 localhost）访问时 navigator.clipboard 不存在，降级到 execCommand
  if (window.isSecureContext && navigator.clipboard) {
    try {
      await navigator.clipboard.writeText(targetLabel)
      message.success(successMessage)
      return
    } catch {
      // 权限被拒等场景继续走降级通道
    }
  }
  if (legacyCopy(targetLabel)) {
    message.success(successMessage)
  } else {
    message.error('复制失败，请手动选择复制')
  }
}

const taskStatusMeta = computed(() => {
  const current = task.value
  if (current?.status === 'running' && ['failed', 'aborted'].includes(current.run_status ?? '')) {
    return { label: '执行异常，收口中', tone: 'error' as TimelineTone }
  }
  if (current?.status === 'running' && current.run_status === 'canceled') {
    return { label: '运行已取消，收口中', tone: 'warning' as TimelineTone }
  }
  const status =
    isWebV2FullInspection.value && current?.execution_status
      ? current.execution_status
      : current?.status || 'queued'
  return getTaskStatusMeta(status)
})

const stoppedRunAlert = computed(() => {
  const current = task.value
  if (current?.status !== 'running' || !['failed', 'aborted', 'canceled'].includes(current.run_status ?? '')) {
    return null
  }
  return current.failure_reason
    ? `主执行流程已停止：${current.failure_reason}。任务的规则与报告收口尚未完成，任务状态将继续同步。`
    : '主执行流程已停止，任务的规则与报告收口尚未完成，任务状态将继续同步。'
})

const reportActionLabel = computed(() =>
  isReportPreview.value
    ? task.value?.target_platform === 'h5'
      ? '查看报告（复核）'
      : '查看报告（预览）'
    : '查看报告',
)

const webClosureStages = computed(() => {
  const current = task.value
  if (!current || !isWebV2FullInspection.value) {
    return null
  }
  const reportFinished = current.report_available === true
  return {
    stages: [
      { label: '探索', state: webCoverageStageLabel(current.coverage_status) },
      { label: '规则质检', state: webInspectionStageLabel(current.execution_status) },
      {
        label: '报告生成',
        state: reportFinished
          ? '已绑定正式报告'
          : current.completion_gate_passed
            ? '等待报告绑定'
            : '等待完成校验',
      },
    ],
    observed: current.coverage_observed_page_types ?? [],
    missing: current.coverage_missing_page_types ?? [],
    unrecognized: current.coverage_unrecognized_page_types ?? [],
    normalization: current.coverage_normalization_protocol_version ?? null,
  }
})

function webCoverageStageLabel(status: string | null | undefined): string {
  const labels: Record<string, string> = {
    complete: '已完成',
    completed_with_gaps: '已完成，有证据缺口',
    failed: '失败',
    needs_review: '待复核',
  }
  return status ? labels[status] ?? status : '进行中'
}

function webInspectionStageLabel(status: string | null | undefined): string {
  const labels: Record<string, string> = {
    completed: '已完成',
    completed_with_gaps: '已完成，有证据缺口',
    failed: '失败',
    needs_review: '待复核',
  }
  return status ? labels[status] ?? status : '等待探索证据'
}

const executionEnd = computed(() => task.value?.execution_finished_at || task.value?.finished_at || null)

const elapsedText = computed(() => {
  const current = task.value
  if (!current?.started_at) {
    return '--:--:--'
  }
  const duration = executionDurationMs(current.started_at, executionEnd.value, nowTick.value)
  if (duration === null) return '--:--:--'
  const seconds = Math.floor(duration / 1000)
  const hh = String(Math.floor(seconds / 3600)).padStart(2, '0')
  const mm = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')
  const ss = String(seconds % 60).padStart(2, '0')
  return `${hh}:${mm}:${ss}`
})

const longExecution = computed(() => !!task.value && isLongExecution(
  task.value.status,
  executionDurationMs(task.value.started_at, executionEnd.value, nowTick.value),
  executionEnd.value,
))

// 与任务列表共用服务端任务进度，不用规则统计推导另一套百分比。
const taskProgressPercent = computed(() => task.value?.progress ?? 0)
const reviewExecutionEnded = computed(() => task.value?.status === 'review'
  && task.value.finalization_state === 'sealed' && !!task.value.finished_at)
const ruleProgressStats = computed(() => {
  const current = stats.value
  if (sectionLoading.stats || !current || !Number.isFinite(current.total) || current.total < 0
    || !Number.isFinite(current.finished) || current.finished < 0) {
    return null
  }
  return current
})
const reviewUnfinishedRules = computed(() => {
  if (task.value?.status !== 'review' || isExplorationOnly.value || !ruleProgressStats.value) {
    return null
  }
  return Math.max(ruleProgressStats.value.total - ruleProgressStats.value.finished, 0)
})
const h5InspectionProgress = computed(() => task.value?.target_platform === 'h5' && !isExplorationOnly.value)

// ---------- 用户旅程 chips ----------

type JourneyChipStatus = 'done' | 'current' | 'failed' | 'skipped' | 'pending'

const journeyChips = computed<Array<{ key: string; label: string; status: JourneyChipStatus }>>(
  () => {
    const nodes = (timeline.value?.nodes ?? []).filter((node) => node.source === 'journey')
    if (nodes.length) {
      return nodes.map((node) => ({
        key: `node-${node.node_seq}`,
        label: node.page_type_name || node.step_name || `步骤 ${node.node_seq}`,
        status: journeyChipStatus(node),
      }))
    }
    return (task.value?.journey_steps ?? []).map((step, index) => ({
      key: `step-${index}`,
      label: step,
      status: 'pending' as JourneyChipStatus,
    }))
  },
)

const hasConfiguredJourney = computed(
  () => (timeline.value?.journey_steps.length ?? task.value?.journey_steps.length ?? 0) > 0,
)

function journeyChipStatus(node: PageTimelineNode): JourneyChipStatus {
  if (node.is_current || node.status === 'running' || node.status === 'visiting') {
    return 'current'
  }
  if (node.status === 'recognized') {
    return 'done'
  }
  if (node.status === 'failed') {
    return 'failed'
  }
  if (node.status === 'skipped') {
    return 'skipped'
  }
  return 'pending'
}

// ---------- 规则展示态 ----------

const RULE_DISPLAY_META: Record<RuleDisplayStatus, { label: string; cls: string }> = {
  passed: { label: '通过', cls: 'rs-passed' },
  failed: { label: '未通过', cls: 'rs-failed' },
  not_detected: { label: '未触发/不适用', cls: 'rs-not-detected' },
  not_applicable: { label: '确认不适用', cls: 'rs-not-detected' },
  coverage_gap: { label: '运行未覆盖', cls: 'rs-pending' },
  not_executed: { label: '未执行', cls: 'rs-pending' },
  detect_error: { label: '执行异常', cls: 'rs-detect-error' },
  evidence_insufficient: { label: '证据不足', cls: 'rs-not-detected' },
  manual_review: { label: '待人工复核', cls: 'rs-manual-review' },
  running: { label: '执行中', cls: 'rs-running' },
  pending: { label: '待执行', cls: 'rs-pending' },
}

function ruleDisplayMeta(status: RuleDisplayStatus | string) {
  return (
    RULE_DISPLAY_META[status as RuleDisplayStatus] || { label: String(status), cls: 'rs-pending' }
  )
}

// ---------- 页面识别时间线 ----------

const ruleNameByCode = computed(() => {
  const map = new Map<string, string>()
  ruleItems.value.forEach((item) => map.set(item.rule_code, item.rule_name))
  return map
})

const explorationSummary = computed(() => timeline.value?.exploration_summary ?? null)

/** 时间线默认只放 12 个页面：执行中看最新的，结束后从头看；其余点「展开全部」。 */
/** 导航开始前浏览器的空白页（about:blank）不算业务页面，时间线里不显示。 */
const timelineNodes = computed(() =>
  (timeline.value?.nodes ?? []).filter(
    (node) => !(node.url === 'about:blank' && !node.page_type),
  ),
)

/**
 * 地址一打开就是 App 下载 / 引导页：引导页按规则只记录、不点下载或引导入口，没有可以继续探索的页面。
 * 探索结束后时间线上只有引导页时直接说明原因，免得以为任务卡住。
 */
const guidePageOnly = computed(() => {
  const current = task.value
  if (!current || (current.target_platform !== 'h5' && current.target_platform !== 'web')) return false
  if (['queued', 'running', 'canceling'].includes(current.status)) return false
  const nodes = timelineNodes.value
  return nodes.length > 0 && nodes.every((node) => node.page_type === 'guide_page')
})

// ---------- 页面详情弹窗 ----------

const TASK_LEVEL_KEY = '__task_level__'

function nodeKeyOf(node: PageTimelineNode) {
  return node.logical_page_id || node.page_capture_id || String(node.node_seq)
}

/** 规则结果按页面采集编号挂到时间线节点上；对不上任何页面的归「整体检查」。 */
const rulesByNode = computed(() => {
  const nodeKeyByCapture = new Map<string, string>()
  for (const node of timelineNodes.value) {
    for (const captureId of nodeCaptureIds(node)) {
      if (!nodeKeyByCapture.has(captureId)) {
        nodeKeyByCapture.set(captureId, nodeKeyOf(node))
      }
    }
  }
  const grouped = new Map<string, RuleResultItem[]>()
  const unbound: RuleResultItem[] = []
  for (const item of ruleItems.value) {
    const key = item.page_capture_id ? nodeKeyByCapture.get(item.page_capture_id) : undefined
    if (key) {
      grouped.set(key, [...(grouped.get(key) ?? []), item])
    } else {
      unbound.push(item)
    }
  }
  return { grouped, unbound }
})

const taskLevelRules = computed(() => rulesByNode.value.unbound)
const ruleTally = computed(() => tallyRules(ruleItems.value))
const inventoryRuleSummary = computed(() => {
  if (!ruleItems.value.some((item) => item.inventory_source)) return null
  const executed = ruleItems.value.filter(countsAsQualityExecution)
  return { total: new Set(ruleItems.value.map((item) => item.rule_id)).size,
    used: new Set(executed.map((item) => item.rule_id)).size, executions: executed.length }
})

function nodeRules(node: PageTimelineNode) {
  return rulesByNode.value.grouped.get(nodeKeyOf(node)) ?? []
}

function ruleSummaryText(items: RuleResultItem[]) {
  const tally = tallyRules(items)
  return tally.total ? `检查 ${tally.total} 条：${tallySummaryText(tally)}` : '没有要检查的规则'
}

/** 节点行上的一句话：还没有规则结果时沿用原来的进度说法。 */
function nodeRuleSummary(node: PageTimelineNode) {
  const items = nodeRules(node)
  return items.length ? ruleSummaryText(items) : nodeRuleProgressText(node)
}

const nodeNameByCapture = computed(() => {
  const names = new Map<string, string>()
  for (const node of timeline.value?.nodes ?? []) {
    for (const captureId of nodeCaptureIds(node)) {
      names.set(captureId, node.page_type_name || nodeDisplayName(node))
    }
  }
  return names
})

// 开关和内容分开：关弹窗只收开关，内容留着让关闭动画走完
const pageDetailVisible = ref(false)
const pageDetailKey = ref<string | null>(null)

function openPageDetail(key: string) {
  pageDetailKey.value = key
  pageDetailVisible.value = true
}

const pageDetail = computed(() => {
  const key = pageDetailKey.value
  if (!key) {
    return null
  }
  if (key === TASK_LEVEL_KEY) {
    return {
      title: '整体检查',
      subtitle: null,
      storyTitle: '这些规则看的是什么',
      story: buildTaskLevelStory(taskLevelRules.value, sectionLoading.rules),
      rules: taskLevelRules.value,
    }
  }
  const node = timelineNodes.value.find((entry) => nodeKeyOf(entry) === key)
  if (!node) {
    return null
  }
  const rules = nodeRules(node)
  const when = formatTime(node.captured_at || node.first_visited_at)
  return {
    // 弹窗节点标题带上宿主页面，免得只看到「广告/遮挡弹窗」不知道是哪一页
    title: popupStateBadge(node)
      ? `${node.page_type_name || '页面'} · ${popupKindLabel(node)}`
      : nodeDisplayName(node),
    subtitle: node.url ? `${when} · ${node.url}` : when,
    storyTitle: undefined,
    story: buildPageStory(node, {
      actions: timeline.value?.exploration_actions ?? [],
      nodeNameByCapture: nodeNameByCapture.value,
      rules,
      explorationOnly: isExplorationOnly.value,
      popupLabel: popupStateBadge(node) ? popupKindLabel(node) : null,
      rulesLoading: sectionLoading.rules,
    }),
    rules,
  }
})

const visibleTimelineNodes = computed(() => {
  const nodes = timelineNodes.value
  if (timelineExpanded.value || nodes.length <= TIMELINE_PREVIEW_LIMIT) {
    return nodes
  }
  return task.value?.status === 'running'
    ? nodes.slice(-TIMELINE_PREVIEW_LIMIT)
    : nodes.slice(0, TIMELINE_PREVIEW_LIMIT)
})

/** 研发排查区（工作项、通知闭环、页面图、日志等）只给管理后台看，部门平台不显示。 */
const showDiagnostics = computed(() => nav.isAdmin.value)
const h5Coverage = computed(() => timeline.value?.h5_coverage ?? null)

const h5ResultBanner = computed(() => {
  if (!isH5ExplorationOnly.value || !timeline.value?.h5_evidence_inventory) {
    return null
  }
  const summary = timeline.value.exploration_summary
  const inventory = timeline.value.h5_evidence_inventory
  if (summary.quality_status === 'in_progress' || summary.quality_status === 'unknown') {
    return null
  }
  const canceled = timeline.value.terminal_reason === 'work_cancelled'
  const tone = canceled
    ? 'needs-review'
    : summary.quality_status === 'technical_failed' || inventory.failed_count > 0
      ? 'technical-failed'
      : summary.quality_status === 'needs_review' ||
          inventory.insufficient_count > 0 ||
          timeline.value.terminal_reason != null
        ? 'needs-review'
        : summary.quality_status === 'complete'
        ? 'complete'
        : 'needs-review'
  return {
    tone,
    title: canceled
      ? '页面探索已取消'
      : tone === 'complete'
      ? '页面探索已完成'
      : tone === 'technical-failed'
        ? '探索技术失败'
        : '探索结果需要复核',
    statusLabel: canceled
      ? '已取消'
      : tone === 'complete'
      ? '已完成'
      : tone === 'technical-failed'
        ? '技术失败'
        : '需要复核',
    inventory,
    terminalReason: timeline.value.terminal_reason,
  }
})

// 「探索结果需要复核」默认只给计数，运营要看具体是哪些页面/证据时再展开。
const h5ReviewExpanded = ref(false)
const h5ReviewItems = computed(() =>
  (h5ResultBanner.value?.inventory.items ?? []).filter((item) => item.status !== 'pass'),
)
const H5_INVENTORY_STATUS_LABELS: Record<string, string> = {
  pass: '证据完整',
  insufficient: '证据不足',
  failed: '证据失败',
}
const H5_TERMINAL_REASON_LABELS: Record<string, string> = {
  work_cancelled: '任务被取消',
  evidence_failed: '证据采集失败',
  evidence_insufficient: '证据不足',
  budget_exhausted: '探索预算用尽',
  frontier_exhausted: '入口已探索完',
  session_lost: '浏览器会话丢失',
}
function h5TerminalReasonText(code: string) {
  return H5_TERMINAL_REASON_LABELS[code] ?? code
}
const H5_AUTHORITY_SOURCE_LABELS: Record<string, string> = {
  h5_page_type_assessment: '页型评估',
  legacy_frontier_derived: '历史探索推导',
}

const frontierIssues = computed(() =>
  (timeline.value?.exploration_actions ?? []).filter(
    (item) =>
      item.result_page_capture_id === null &&
      ['failed', 'blocked', 'skipped'].includes(item.status),
  ),
)

const inspectionWorkSummary = computed(() => inspectionWorkItems.value?.summary ?? null)

const visibleInspectionWorkItems = computed(() => inspectionWorkItems.value?.items ?? [])

const h5PublicationProjection = computed<H5InspectionPublicationProjection | null>(() => {
  if (
    task.value?.target_platform !== 'h5' ||
    task.value.execution_mode !== 'full_inspection'
  ) {
    return null
  }
  return timeline.value?.full_inspection_closure?.publication_projection ?? null
})

const popupDismissalActionsByCapture = computed(() => {
  const actions = new Map<string, ExplorationActionItem>()
  for (const item of timeline.value?.exploration_actions ?? []) {
    if (
      item.source_page_capture_id &&
      item.action === 'click' &&
      (
        item.purpose?.includes('关闭当前遮挡弹窗') ||
        item.purpose?.includes('关闭覆盖弹窗') ||
        item.target?.includes('van-dialog') ||
        item.target?.includes('van-overlay')
      )
    ) {
      actions.set(item.source_page_capture_id, item)
    }
  }
  return actions
})

const timelineNodesByCapture = computed(() => {
  const nodes = new Map<string, PageTimelineNode>()
  for (const item of timeline.value?.nodes ?? []) {
    if (item.page_capture_id) {
      nodes.set(item.page_capture_id, item)
    }
  }
  return nodes
})

function isPopupPageState(pageState: string | null | undefined) {
  const normalizedState = (pageState || '').toLowerCase()
  return ['popup', 'modal', 'overlay', 'dialog', '弹窗', '遮罩'].some(
    (marker) => normalizedState.includes(marker),
  )
}

function popupStateBadge(node: PageTimelineNode) {
  if (node.page_type === 'startup_ad') {
    return null
  }
  const stateIsPopup = isPopupPageState(node.page_state)
  const action = node.page_capture_id
    ? popupDismissalActionsByCapture.value.get(node.page_capture_id)
    : undefined
  if (!stateIsPopup && !action) {
    return null
  }
  if (action?.status === 'explored') {
    const resultNode = action.result_page_capture_id
      ? timelineNodesByCapture.value.get(action.result_page_capture_id)
      : undefined
    if (resultNode && !isPopupPageState(resultNode.page_state)) {
      return { label: `${popupKindLabel(node)}已关闭`, cls: 'rs-passed' }
    }
    return { label: `${popupKindLabel(node)}关闭未确认`, cls: 'rs-failed' }
  }
  if (action && ['failed', 'blocked', 'skipped'].includes(action.status)) {
    return { label: `${popupKindLabel(node)}未关闭`, cls: 'rs-failed' }
  }
  return { label: `${popupKindLabel(node)}关闭中`, cls: 'rs-running' }
}

function popupKindLabel(node: PageTimelineNode) {
  const purpose = node.page_capture_id
    ? popupDismissalActionsByCapture.value.get(node.page_capture_id)?.purpose || ''
    : ''
  if (purpose.includes('身份') || purpose.includes('凭证') || purpose.includes('登录')) {
    return '身份凭证弹窗'
  }
  if (purpose.includes('公告') || purpose.includes('通知')) {
    return '公告弹窗'
  }
  return '广告/遮挡弹窗'
}

function nodeDisplayName(node: PageTimelineNode) {
  if (popupStateBadge(node)) {
    return popupKindLabel(node)
  }
  const pageName = node.page_type_name || node.step_name || `页面 ${node.node_seq}`
  return node.page_role === 'home' ? `首页 · ${pageName}` : pageName
}

function nodeSourceLabel(node: PageTimelineNode) {
  if (node.source !== 'discovered') {
    return null
  }
  return hasConfiguredJourney.value ? '旅程外' : '动态发现'
}

function currentRuleText(node: PageTimelineNode) {
  if (!node.current_rule_code) {
    return ''
  }
  const name = ruleNameByCode.value.get(node.current_rule_code)
  return name ? `${name}（${node.current_rule_code}）` : node.current_rule_code
}

function nodeStatusClass(node: PageTimelineNode) {
  if (node.is_current || node.status === 'running' || node.status === 'visiting') {
    return 'node-running'
  }
  const map: Record<string, string> = {
    recognized: 'node-done',
    failed: 'node-failed',
    skipped: 'node-skipped',
    pending: 'node-pending',
  }
  return map[node.status] || 'node-pending'
}

function nodeStatusBadge(node: PageTimelineNode) {
  const inspectionStatus = node.inspection?.status
  const inspectionMap: Record<string, { label: string; cls: string }> = {
    completed: { label: '页面检查完成', cls: 'rs-passed' },
    degraded: { label: '页面检查完成 · 需复核', cls: 'rs-not-detected' },
    failed: { label: '页面检查失败', cls: 'rs-detect-error' },
    captured: { label: '已采集', cls: 'rs-running' },
    detecting: { label: '识别中', cls: 'rs-running' },
    matching: { label: '规则匹配中', cls: 'rs-running' },
    evaluating: { label: '规则执行中', cls: 'rs-running' },
  }
  if (inspectionStatus && inspectionMap[inspectionStatus]) {
    return inspectionMap[inspectionStatus]
  }
  if (node.is_current || node.status === 'running' || node.status === 'visiting') {
    return { label: '执行中', cls: 'rs-running' }
  }
  const map: Record<string, { label: string; cls: string }> = {
    recognized: { label: '识别成功', cls: 'rs-passed' },
    failed: { label: '识别失败', cls: 'rs-detect-error' },
    skipped: { label: '已跳过', cls: 'rs-pending' },
    pending: { label: '待执行', cls: 'rs-pending' },
  }
  return map[node.status] || { label: node.status_label, cls: 'rs-pending' }
}

function explorationActionText(action: string) {
  const labels: Record<string, string> = {
    goto: '打开页面',
    click: '点击',
    scroll: '滚动',
    hover: '悬停',
    network_fault: '网络故障模拟',
    capture: '采集页面',
  }
  return labels[action] || action
}

function frontierIssueStatusText(status: string) {
  const labels: Record<string, string> = {
    failed: '执行失败',
    blocked: '策略阻断',
    skipped: '已跳过',
  }
  return labels[status] || status
}

function workItemStatusMeta(status: string) {
  const map: Record<string, { label: string; cls: string }> = {
    pending: { label: '待派发', cls: 'rs-pending' },
    leased: { label: '已租约', cls: 'rs-running' },
    running: { label: '执行中', cls: 'rs-running' },
    succeeded: { label: '已完成', cls: 'rs-passed' },
    partial: { label: '部分完成', cls: 'rs-not-detected' },
    failed: { label: '失败', cls: 'rs-failed' },
    cancelled: { label: '已取消', cls: 'rs-pending' },
    superseded: { label: '已替换', cls: 'rs-pending' },
  }
  return map[status] || { label: status, cls: 'rs-pending' }
}

function workItemPageName(item: InspectionWorkItem) {
  if (!item.page_capture_id) {
    return '未绑定页面'
  }
  const node = timelineNodesByCapture.value.get(item.page_capture_id)
  return node?.page_type_name || node?.page_type || `页面 ${item.page_capture_id.slice(0, 8)}`
}

function compactHash(value: string | null | undefined) {
  if (!value) {
    return '-'
  }
  return value.length > 12 ? `${value.slice(0, 12)}…` : value
}

function publicationStageLabel(stage: string) {
  const labels: Record<string, string> = {
    not_started: '尚未开始',
    publication_persisted: 'Publication 已持久化',
    wakeup_delivered: '唤醒已送达',
    work_claimed: 'Work 已领取',
    inspection_completed: '质检已完成',
  }
  return labels[stage] ?? stage
}

function publicationStatusMeta(status: string) {
  if (status === 'published' || status === 'not_applicable') {
    return { label: status === 'published' ? '已发布' : '无需质检', cls: 'rs-passed' }
  }
  if (status === 'failed') {
    return { label: '发布失败', cls: 'rs-failed' }
  }
  if (status === 'needs_review') {
    return { label: '需要复核', cls: 'rs-manual-review' }
  }
  if (status === 'cancelled' || status === 'superseded') {
    return { label: status === 'cancelled' ? '已取消' : '已替代', cls: 'rs-pending' }
  }
  return { label: '处理中', cls: 'rs-running' }
}

function workItemRuleSummary(item: InspectionWorkItem) {
  if (!item.rules.length) {
    return '未绑定规则'
  }
  const codes = item.rules.slice(0, 4).map((rule) => rule.rule_code).join('、')
  const suffix = item.rules.length > 4 ? ` 等 ${item.rules.length} 条` : ''
  return `${codes}${suffix}`
}

function nodeProgressPercent(node: PageTimelineNode) {
  const total = node.rule_progress.total
  if (!total) {
    return 0
  }
  return Math.min(100, Math.round((node.rule_progress.executed / total) * 100))
}

function nodeRuleProgressText(node: PageTimelineNode) {
  if (isExplorationOnly.value) {
    return '仅采集页面证据'
  }
  if (node.inspection.failure_code === 'PAGE_TYPE_OUTSIDE_INSPECTION_SCOPE') {
    return '本次质检范围外'
  }
  const { executed, total } = node.rule_progress
  if (
    executed === 0
    && (total == null || total === 0)
    && (
      node.inspection.status === 'failed'
      || node.detection.status === 'ambiguous'
      || node.detection.status === 'insufficient'
      || node.detection.status === 'error'
    )
  ) {
    return '未进入规则检查'
  }
  if (!node.page_type && executed === 0 && (total == null || total === 0)) {
    return '规则匹配未启动'
  }
  if (executed === 0 && (total == null || total === 0)) {
    return '尚无规则执行结果'
  }
  return total == null ? `规则检查 ${executed} 项` : `规则检查 ${executed}/${total} 项`
}

function nodeThumbUrl(node: PageTimelineNode) {
  return node.screenshot_artifact_id ? evidenceFiles.get(node.screenshot_artifact_id) : ''
}

function formatTime(value: string | null) {
  if (!value) {
    return '--:--:--'
  }
  return new Date(value).toLocaleTimeString('zh-CN', { hour12: false })
}

function openRuleReview(item: RuleResultItem) {
  if (!item.execution_id) {
    return
  }
  void nav.push({
    path: '/reports',
    query: { task: taskId.value, execution: item.execution_id },
  })
}

// ---------- 截图证据 ----------

const viewerVisible = ref(false)
const viewerSelection = ref<
  { kind: 'collection'; group: ScreenshotGroup; item: ScreenshotItem }
  | { kind: 'rule'; artifactIds: string[]; executionId: string | null; ruleId: string; greenContractHash?: string }
  | null
>(null)
const viewerTitle = ref('')
const viewerIsCollection = ref(false)
const viewerInitialArtifactId = ref('')

function screenshotCollectionEntries(group: ScreenshotGroup, item: ScreenshotItem) {
  return buildScreenshotCollection(group, item, {
    rules: ruleItems.value,
    timeline: timeline.value,
    pageGraph: pageGraph.value,
  })
}

const screenshotIndex = computed(() => {
  const map = new Map<string, ScreenshotViewerEntry>()
  screenshots.value?.groups.forEach((group) => {
    group.items.forEach((item) => {
      for (const entry of screenshotCollectionEntries(group, item)) {
        map.set(entry.artifact_id, entry)
      }
    })
  })
  return map
})

const viewerItems = computed<ScreenshotViewerEntry[]>(() => {
  const selection = viewerSelection.value
  if (selection?.kind === 'collection') return screenshotCollectionEntries(selection.group, selection.item)
  if (selection?.kind === 'rule') {
    let ids = selection.artifactIds
    if (selection.greenContractHash !== undefined) {
      const current = ruleItems.value.find((item) => item.execution_id === selection.executionId
        && item.rule_id === selection.ruleId)
      if (!current || !isLinkedGreenResult(current.green_result)
        || current.green_result.rule_contract_hash !== selection.greenContractHash) return []
      const allowed = new Set(ruleScreenshotIds(current))
      ids = ids.filter((id) => allowed.has(id))
    }
    return ids.map((id) => screenshotIndex.value.get(id))
      .filter((entry): entry is ScreenshotViewerEntry => !!entry)
  }
  return []
})

watch(viewerItems, (entries) => {
  const selection = viewerSelection.value
  // 同任务轮询撤销关联或更换原图时也关闭旧查看器，不能继续显示已失效的票据图片。
  if (selection?.kind === 'rule' && selection.greenContractHash !== undefined
    && entries.length !== selection.artifactIds.length) {
    viewerVisible.value = false
    viewerSelection.value = null
  }
})

function ruleScreenshotIds(item: RuleResultItem) {
  if (item.green_result) {
    if (!isLinkedGreenResult(item.green_result)) return []
    const ids = item.green_result.subitems.flatMap((subitem) => subitem.screenshot_artifact_ids)
    return [...new Set(ids)].filter((id) => screenshotIndex.value.has(id))
  }
  return item.evidence_artifact_ids.filter((id) => screenshotIndex.value.has(id))
}

function screenshotImageCount(shot: ScreenshotItem) {
  return new Set([shot.artifact_id, ...(shot.related_frames ?? []).map((frame) => frame.artifact_id)]
    .filter(Boolean)).size
}

function screenshotGroupCountLabel(group: ScreenshotGroup) {
  const collections = group.items.filter((item) => screenshotImageCount(item) > 1).length
  const singles = group.items.length - collections
  return [collections ? `${collections}个合集` : '', singles ? `${singles}张单图` : '']
    .filter(Boolean).join(' · ')
}

function occlusionLabel(shot: ScreenshotItem) {
  if (shot.occlusion_kind === 'startup') return '开屏'
  if (shot.occlusion_kind === 'overlay') return '弹层'
  return ''
}

function openScreenshotCollection(group: ScreenshotGroup, shot: ScreenshotItem) {
  const entries = screenshotCollectionEntries(group, shot)
  if (!entries.length) {
    return
  }
  viewerSelection.value = { kind: 'collection', group, item: shot }
  viewerTitle.value = [group.page_display_name, occlusionLabel(shot)].filter(Boolean).join(' · ')
  viewerIsCollection.value = entries.length > 1
  viewerInitialArtifactId.value = shot.artifact_id
  viewerVisible.value = true
}

function openViewerForRule(item: RuleResultItem, artifactId: string) {
  if (!ruleScreenshotIds(item).includes(artifactId)) return
  const entries = ruleScreenshotIds(item)
    .map((id) => screenshotIndex.value.get(id))
    .filter((entry): entry is ScreenshotViewerEntry => !!entry)
  if (!entries.length) {
    return
  }
  viewerSelection.value = { kind: 'rule', artifactIds: entries.map((entry) => entry.artifact_id),
    executionId: item.execution_id, ruleId: item.rule_id,
    ...(item.green_result ? { greenContractHash: item.green_result.rule_contract_hash! } : {}),
  }
  viewerTitle.value = `${item.rule_name} · 规则证据`
  viewerIsCollection.value = false
  viewerInitialArtifactId.value = artifactId
  viewerVisible.value = true
}

async function downloadViewerScreenshot(artifactId: string) {
  if (viewerItems.value.some((item) => item.artifact_id === artifactId)) {
    try {
      await evidenceFiles.open(artifactId, { download: true })
    } catch (error) {
      message.error(error instanceof Error ? error.message : '证据下载失败')
    }
  }
}

async function downloadAllScreenshots() {
  try {
    await downloadScreenshotsArchive(taskId.value)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '截图证据包下载失败')
  }
}

const screenshotGroups = computed(() =>
  (screenshots.value?.groups ?? [])
    .map((group) => {
      const items = group.items.filter((item) => item.gallery_visible !== false)
      return { ...group, total: items.length, items }
    })
    .filter((group) => group.items.length > 0),
)

const hasScreenshots = computed(() =>
  screenshotGroups.value.some((group) => group.items.length > 0),
)

function evidenceUrl(artifactId: string) {
  return evidenceFiles.get(artifactId)
}

function currentEvidenceArtifactIds() {
  const ids: string[] = []
  timeline.value?.nodes.forEach((node) => {
    if (node.screenshot_artifact_id) {
      ids.push(node.screenshot_artifact_id)
    }
  })
  screenshots.value?.groups.forEach((group) => {
    group.items.forEach((item) => {
      ids.push(item.artifact_id)
      for (const related of item.related_frames ?? []) {
        ids.push(related.artifact_id)
      }
    })
  })
  return ids
}

// ---------- 执行日志 ----------

const LOG_LEVEL_CLASS: Record<string, string> = {
  INFO: 'log-info',
  WARN: 'log-warn',
  ERROR: 'log-error',
  EXEC: 'log-exec',
}

function logLevelClass(level: string) {
  return LOG_LEVEL_CLASS[level] || 'log-info'
}

const canLoadMoreLogs = computed(
  () => logItems.value.length < logTotal.value && logLimit.value < 500,
)

async function loadMoreLogs() {
  logLimit.value = Math.min(logLimit.value + 200, 500)
  try {
    const result = await getTaskLogs(taskId.value, { pageSize: logLimit.value })
    logItems.value = result.items
    logTotal.value = result.total
  } catch (error) {
    message.error(error instanceof Error ? error.message : '日志加载失败')
  }
}

async function exportLogs() {
  try {
    await downloadLogsExport(taskId.value)
  } catch (error) {
    message.error(error instanceof Error ? error.message : '日志导出失败')
  }
}

// ---------- Workflow 轨迹（保留的开发视角，默认折叠） ----------

const agentRunStatusMeta = computed(() => {
  const map: Record<string, { label: string; tone: string }> = {
    queued: { label: '排队中', tone: 'pending' },
    running: { label: '运行中', tone: 'running' },
    canceling: { label: '取消中', tone: 'warning' },
    done: { label: '已完成', tone: 'pass' },
    failed: { label: '运行失败', tone: 'error' },
    canceled: { label: '已取消', tone: 'pending' },
  }
  const status = agentRun.value?.status || ''
  return map[status] || { label: status || '-', tone: 'pending' }
})

// Agent 结论属于业务摘要，始终与页面主任务状态保持一致；技术状态仍留在 Workflow。
const agentConclusionStatusMeta = computed(() =>
  task.value ? taskStatusMeta.value : agentRunStatusMeta.value,
)

const agentRunDurationText = computed(() => {
  const run = agentRun.value
  if (!run?.started_at || !run.finished_at) {
    return '-'
  }
  const seconds = Math.round(
    (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000,
  )
  return `${Math.max(0, seconds)}s`
})

const agentRunCostText = computed(() => {
  const estimate = agentRun.value?.llm_cost_estimate
  if (!estimate) {
    return '-'
  }
  if (!estimate.pricing_configured) {
    return '未配置单价'
  }
  return `${formatCurrencySymbol(estimate.currency)}${formatCostAmount(estimate.total_cost)}`
})

const agentRunTokenBreakdownText = computed(() => {
  const run = agentRun.value
  if (!run) {
    return '-'
  }
  const cacheHit = run.cache_hit_tokens ? ` / 缓存命中 ${formatNumber(run.cache_hit_tokens)}` : ''
  return `输入 ${formatNumber(run.input_tokens)} / 输出 ${formatNumber(run.output_tokens)}${cacheHit}`
})

function stepStatusClass(status: string) {
  if (status === 'ok' || status === 'success' || status === 'pass') {
    return 'success'
  }
  if (status === 'error' || status === 'failed') {
    return 'error'
  }
  return 'running'
}

const expandedSteps = ref<Set<number>>(new Set())

function toggleStep(step: AgentRunStep) {
  if (!stepHasIO(step)) {
    return
  }
  const next = new Set(expandedSteps.value)
  if (next.has(step.step_seq)) {
    next.delete(step.step_seq)
  } else {
    next.add(step.step_seq)
  }
  expandedSteps.value = next
}

function isSkillStep(step: AgentRunStep) {
  return step.capability_kind === 'skill' || step.selected_capability === 'load_skill'
}

function stepTitle(step: AgentRunStep) {
  if (step.selected_capability === 'load_skill') {
    const skillName = step.capability_input?.skill_name
    return `加载 Skill · ${typeof skillName === 'string' ? skillName : '-'}`
  }
  if (step.selected_capability === 'record_rule_execution_tool') {
    const ruleCode = step.capability_input?.rule_code ?? step.capability_output?.rule_code
    const status =
      step.capability_input?.execution_status ?? step.capability_output?.execution_status
    if (typeof ruleCode === 'string') {
      return `写回 ${ruleCode} → ${typeof status === 'string' ? getRuleStatusMeta(status).label : '-'}`
    }
  }
  return step.selected_capability || step.phase || '-'
}

function isBlockedStep(step: AgentRunStep) {
  const text = step.capability_output?.text
  return typeof text === 'string' && text.includes('阻断')
}

function stepStatusText(step: AgentRunStep) {
  return isBlockedStep(step) ? '被阻断' : step.status.toUpperCase()
}

function hasPayload(value: Record<string, unknown> | null | undefined) {
  return !!value && Object.keys(value).length > 0
}

function stepHasIO(step: AgentRunStep) {
  return hasPayload(step.capability_input) || hasPayload(step.capability_output)
}

function decodeUnicodeEscapes(text: string) {
  return text.replace(/\\u([0-9a-fA-F]{4})/g, (_, hex: string) =>
    String.fromCharCode(parseInt(hex, 16)),
  )
}

function formatPayload(value: Record<string, unknown>) {
  if (typeof value.text === 'string' && Object.keys(value).length === 1) {
    try {
      return JSON.stringify(JSON.parse(value.text), null, 2)
    } catch {
      return decodeUnicodeEscapes(value.text)
    }
  }
  return JSON.stringify(value, null, 2)
}

function latencyText(ms: number | null) {
  if (ms === null) {
    return '-'
  }
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`
}

function formatNumber(value: number | null) {
  return typeof value === 'number' ? value.toLocaleString('zh-CN') : '-'
}

function formatCostAmount(value: number) {
  return value.toLocaleString('zh-CN', {
    minimumFractionDigits: value < 1 ? 6 : 2,
    maximumFractionDigits: value < 1 ? 6 : 2,
  })
}

function formatCurrencySymbol(currency: string) {
  return currency.toUpperCase() === 'CNY' ? '¥' : `${currency.toUpperCase()} `
}

// ---------- 基础信息 ----------

const scanModeTextMap: Record<string, string> = {
  full: '完整巡检',
  quick: '快速扫描',
  custom: '自定义规则',
}

const channelTextMap: Record<string, string> = {
  workbench: '工作台',
  api: '开放 API',
  telegram: 'Telegram',
}

const PLATFORM_TEXT: Record<string, string> = { h5: 'H5', web: 'Web', app: 'App' }
const EXECUTION_MODE_TEXT: Record<string, string> = {
  full_inspection: '完整质检',
  exploration_only: '仅页面探索',
}

/** 面向用户的基本信息；任务编号、规则集编码、Run ID 等放在研发排查区。 */
const overviewItems = computed(() => {
  if (!task.value) {
    return []
  }
  const items = [
    { label: '产品', value: task.value.product?.product_name || '-' },
    {
      label: '目标平台',
      value: PLATFORM_TEXT[task.value.target_platform] || task.value.target_platform,
    },
    {
      label: '质检方式',
      value: EXECUTION_MODE_TEXT[task.value.execution_mode] || task.value.execution_mode,
    },
    { label: '质检模式', value: scanModeTextMap[task.value.scan_mode] || task.value.scan_mode },
    { label: '发起人', value: task.value.submitter_name || '-' },
    { label: '创建时间', value: formatDate(task.value.created_at) },
    { label: '开始时间', value: formatDate(task.value.started_at) },
    { label: '完成时间', value: formatDate(task.value.finished_at) },
  ]
  if (task.value.target_platform === 'web') {
    items.splice(3, 0, {
      label: '桌面浏览器画像',
      value: task.value.browser_profile_id || '未锁定',
    })
  }
  return items
})

const technicalInfoItems = computed(() => {
  if (!task.value) {
    return []
  }
  return [
    { label: '任务编号', value: task.value.task_id },
    { label: '规则集版本', value: `${task.value.ruleset_code} v${task.value.ruleset_version}` },
    {
      label: '渠道 / 优先级',
      value: `${channelTextMap[task.value.channel] || task.value.channel} / ${task.value.priority}`,
    },
    { label: 'Run ID', value: task.value.run_id || '-' },
  ]
})

function getTaskStatusMeta(status: string) {
  const map: Record<string, { label: string; tone: string }> = {
    queued: { label: taskStatusLabel('queued'), tone: 'pending' },
    running: { label: taskStatusLabel('running'), tone: 'running' },
    canceling: { label: taskStatusLabel('canceling'), tone: 'warning' },
    review: { label: taskStatusLabel('review'), tone: 'warning' },
    done: { label: taskStatusLabel('done'), tone: 'pass' },
    failed: { label: taskStatusLabel('failed'), tone: 'error' },
    canceled: { label: taskStatusLabel('canceled'), tone: 'pending' },
    completed: { label: '已完成', tone: 'pass' },
    completed_with_gaps: { label: '已完成，有证据缺口', tone: 'warning' },
    needs_review: { label: '待复核', tone: 'warning' },
  }
  return map[status] || { label: taskStatusLabel(status), tone: 'pending' }
}

function getRuleStatusMeta(status: string) {
  const map: Record<string, { label: string; tone: TimelineTone }> = {
    pass: { label: ruleExecutionStatusLabel('pass'), tone: 'pass' },
    fail: { label: ruleExecutionStatusLabel('fail'), tone: 'error' },
    warning: { label: ruleExecutionStatusLabel('warning'), tone: 'warning' },
    manual_review: { label: ruleExecutionStatusLabel('manual_review'), tone: 'warning' },
    evidence_insufficient: { label: ruleExecutionStatusLabel('evidence_insufficient'), tone: 'pending' },
    skipped: { label: ruleExecutionStatusLabel('skipped'), tone: 'pending' },
    error: { label: ruleExecutionStatusLabel('error'), tone: 'error' },
  }
  return map[status] || { label: ruleExecutionStatusLabel(status), tone: 'pending' }
}

function formatDate(value: string | null) {
  if (!value) {
    return '-'
  }
  return new Date(value).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
</script>

<template>
  <section class="task-detail-shell">
    <div class="breadcrumb-row">
      <button class="breadcrumb-link" type="button" @click="returnToTaskList">任务中心</button>
      <n-icon size="15">
        <ChevronForwardOutline />
      </n-icon>
      <span>任务详情</span>
    </div>

    <div v-if="loading" class="loading-panel workstation-glass-panel">
      <n-spin size="large" />
      <p>任务详情加载中…</p>
    </div>

    <div v-else-if="loadError" class="error-panel workstation-glass-panel">
      <h3>任务详情加载失败</h3>
      <p>{{ loadError }}</p>
      <n-button type="primary" @click="loadAll()">重试</n-button>
    </div>

    <template v-else-if="task">
      <div v-if="stoppedRunAlert" class="partial-load-alert workstation-glass-panel" data-testid="stopped-run-alert">
        <n-icon size="18"><WarningOutline /></n-icon>
        <span>{{ stoppedRunAlert }}</span>
      </div>
      <div v-if="partialLoadError" class="partial-load-alert workstation-glass-panel">
        <n-icon size="18"><WarningOutline /></n-icon>
        <span>{{ partialLoadError }}</span>
        <n-button size="small" quaternary type="primary" @click="loadAll()">重新加载</n-button>
      </div>

      <div
        v-if="schedulerAlert"
        class="scheduler-alert workstation-glass-panel"
        :class="`scheduler-alert-${schedulerAlert.tone}`"
      >
        <n-icon size="20">
          <WarningOutline />
        </n-icon>
        <div class="scheduler-alert-body">
          <strong>{{ schedulerAlert.title }}</strong>
          <p>{{ schedulerAlert.detail }}</p>
        </div>
      </div>

      <div class="overview-card workstation-glass-panel workstation-card-hover">
        <div class="overview-header">
          <div class="overview-title-wrap">
            <div class="task-icon">
              <n-icon size="24">
                <ClipboardOutline />
              </n-icon>
            </div>
            <div>
              <div class="title-line">
                <h2>{{ task.task_name }}</h2>
                <span class="status-pill" :class="`tone-${taskStatusMeta.tone}`">
                  <span class="status-dot"></span>
                  {{ taskStatusMeta.label }}
                </span>
              </div>
              <div class="target-link-line">
                <span class="target-url mono">{{ displayTargetUrl }}</span>
                <button class="copy-button" type="button" title="复制目标标识" @click="copyTargetUrl">
                  <n-icon size="14"><CopyOutline /></n-icon>
                  复制
                </button>
              </div>
              <div
                v-if="task.target_platform === 'web' && task.browser_profile_id"
                class="web-browser-profile"
                data-testid="web-browser-profile"
              >
                <span>桌面浏览器配置</span>
                <code>{{ task.browser_profile_id }}</code>
                <b>{{ task.viewport_width ?? 1920 }} × {{ task.viewport_height ?? 1080 }}</b>
                <em>PNG 无损压缩 · 像素尺寸不变</em>
              </div>
            </div>
          </div>
          <div class="overview-actions">
            <n-button v-if="canViewReport" secondary class="ghost-action" @click="viewReport">
              <template #icon>
                <n-icon><DocumentTextOutline /></n-icon>
              </template>
              {{ reportActionLabel }}
            </n-button>
            <div v-else-if="reportUnavailableReason" class="report-none-action">
              <n-button
                secondary
                class="ghost-action"
                data-testid="report-none-action"
                :title="reportUnavailableReason"
                disabled
              >
                <template #icon>
                  <n-icon><DocumentTextOutline /></n-icon>
                </template>
                查看报告
              </n-button>
              <small>{{ reportUnavailableReason }}</small>
            </div>
            <!-- 规则测试只归体验中心管理员（管理后台规则配置里的测试页签） -->
            <n-button
              v-if="isExplorationOnly && canTestRules"
              secondary
              class="ghost-action"
              data-testid="independent-rule-test"
              @click="openIndependentRuleTest"
            >
              <template #icon>
                <n-icon><SearchOutline /></n-icon>
              </template>
              独立规则测试
            </n-button>
            <n-popconfirm
              v-if="canCancel"
              positive-text="确认取消"
              negative-text="再想想"
              @positive-click="handleCancel"
            >
              <template #trigger>
                <n-button secondary class="ghost-action" :disabled="operating">
                  <template #icon>
                    <n-icon><CloseCircleOutline /></n-icon>
                  </template>
                  取消任务
                </n-button>
              </template>
              确认取消当前任务？执行中的进度将被终止。
            </n-popconfirm>
            <n-popconfirm
              v-if="canRetryFinalization"
              data-testid="retry-finalization-confirm"
              positive-text="确认恢复"
              negative-text="暂不处理"
              @positive-click="handleRetryFinalization"
            >
              <template #trigger>
                <n-button type="warning" class="primary-action" :disabled="operating">
                  <template #icon>
                    <n-icon><RefreshOutline /></n-icon>
                  </template>
                  恢复报告收口
                </n-button>
              </template>
              该任务因重复出现相同收口错误已隔离。确认后仅重试原任务的正式报告收口，不会新建任务。
            </n-popconfirm>
            <n-popconfirm
              v-if="canRerun"
              positive-text="确认重跑"
              negative-text="再想想"
              @positive-click="handleRerun"
            >
              <template #trigger>
                <n-button type="primary" class="primary-action" :disabled="operating">
                  <template #icon>
                    <n-icon><RefreshOutline /></n-icon>
                  </template>
                  重新执行
                </n-button>
              </template>
              确认重新执行该任务？将创建新的执行记录。
            </n-popconfirm>
          </div>
        </div>

        <div v-if="longExecution" class="long-execution-alert" role="status" data-testid="long-execution-alert">
          <strong>任务持续执行已超过 2.5 小时（{{ elapsedText }}），请核查执行进展</strong>
          <p>如需停止本次执行，可取消任务；任务结束后可重新执行或复制任务发起质检。</p>
          <n-popconfirm v-if="canCancel" positive-text="确认取消" negative-text="继续等待" @positive-click="handleCancel">
            <template #trigger><n-button size="small" :disabled="operating">取消任务</n-button></template>
            确认取消当前任务？执行中的进度将被终止。
          </n-popconfirm>
        </div>

        <div class="progress-block">
          <div class="progress-meta">
            <span>任务进度</span>
            <strong>{{ taskProgressPercent }}%</strong>
          </div>
          <div class="progress-track">
            <div class="progress-fill" :style="{ width: `${taskProgressPercent}%` }"></div>
          </div>
          <div class="progress-footnote">
            <span v-if="journeyChips.length">旅程步骤 {{ journeyChips.length }} 个</span>
            <span v-if="explorationSummary?.tracking_available">
              <template v-if="task.target_platform === 'h5'">页面观察 {{ explorationSummary.page_total }} 条（含同页与弹窗）</template>
              <template v-else>已探索页面 {{ explorationSummary.page_total }} 个</template>
            </span>
            <span>
              <template v-if="isExplorationOnly">不执行正式规则{{ canTestRules ? ' · 可独立规则测试' : '' }}</template>
              <template v-else-if="ruleProgressStats">
                规则已完成 {{ ruleProgressStats.finished }}/{{ ruleProgressStats.total }} 项<template
                  v-if="reviewUnfinishedRules"
                > · 规则检查未完成 {{ reviewUnfinishedRules }} 项</template><template
                  v-if="task?.status === 'running' && (stats?.frontier_pending ?? 0) > 0"
                >（另有 {{ stats?.frontier_pending }} 个入口待探索）</template>
              </template>
              <template v-else-if="sectionLoading.stats">规则统计加载中</template>
              <template v-else>规则统计暂不可用</template>
            </span>
            <span>执行时长 <b class="mono">{{ elapsedText }}</b></span>
          </div>
          <p v-if="task?.status === 'review'" class="progress-review-note" role="status">
            <strong>{{ reviewExecutionEnded ? '自动执行已结束，待复核' : '已进入复核' }}</strong>
            <span v-if="reviewUnfinishedRules">。规则检查缺口已保留在结果中。</span>
          </p>
          <p v-if="h5InspectionProgress && task.status === 'review'" class="progress-footnote" data-testid="h5-progress-explanation">
            待复核不代表质检通过。任务进度仅表示执行阶段，规则完成数和证据不足或执行异常的缺口独立保留。
          </p>
        </div>

        <section
          v-if="h5ResultBanner"
          class="h5-result-banner"
          :class="`h5-result-${h5ResultBanner.tone}`"
        >
          <div class="h5-result-heading">
            <div>
              <strong>{{ h5ResultBanner.title }}</strong>
              <p>H5 页面探索与证据库存已按本次运行正式终态收口。</p>
            </div>
            <span>{{ h5ResultBanner.statusLabel }}</span>
          </div>
          <div class="h5-result-counts">
            <span>证据完整 {{ h5ResultBanner.inventory.pass_count }} 项</span>
            <span v-if="h5ResultBanner.inventory.insufficient_count">
              证据不足 {{ h5ResultBanner.inventory.insufficient_count }} 项
            </span>
            <span v-if="h5ResultBanner.inventory.failed_count">
              证据失败 {{ h5ResultBanner.inventory.failed_count }} 项
            </span>
            <span>库存合计 {{ h5ResultBanner.inventory.item_count }} 项</span>
            <button
              v-if="h5ReviewItems.length"
              type="button"
              class="h5-review-toggle"
              data-testid="h5-review-toggle"
              @click="h5ReviewExpanded = !h5ReviewExpanded"
            >
              {{ h5ReviewExpanded ? '收起明细' : `查看需复核明细（${h5ReviewItems.length} 项）` }}
            </button>
          </div>
          <ul v-if="h5ReviewExpanded && h5ReviewItems.length" class="h5-review-list" data-testid="h5-review-list">
            <li v-for="item in h5ReviewItems" :key="item.inventory_item_id">
              <b>{{ pageTypeName(item.page_type) }}</b>
              <span v-if="item.page_state">{{ pageStateName(item.page_state) }}</span>
              <span class="h5-review-status" :class="`is-${item.status}`">
                {{ H5_INVENTORY_STATUS_LABELS[item.status] ?? item.status }}
              </span>
              <span class="h5-review-reasons">
                {{ item.reason_codes.length ? item.reason_codes.map(reasonText).join('；') : '未给出原因' }}
              </span>
            </li>
          </ul>
          <code v-if="h5ResultBanner.terminalReason">
            结束原因：{{ h5TerminalReasonText(h5ResultBanner.terminalReason) }}
          </code>
        </section>


        <section
          v-if="webClosureStages"
          class="h5-result-banner"
          data-testid="web-formal-closure"
        >
          <div class="h5-result-heading">
            <div>
              <strong>Web 正式质检闭环</strong>
              <p>探索证据、规则质检与 Gate 绑定报告均来自服务端权威投影。</p>
            </div>
            <span>{{ taskStatusMeta.label }}</span>
          </div>
          <div class="h5-result-counts">
            <span v-for="stage in webClosureStages.stages" :key="stage.label">
              <b>{{ stage.label }}</b> {{ stage.state }}
            </span>
          </div>
          <div class="h5-result-counts">
            <span>已覆盖页型 {{ pageTypeNames(webClosureStages.observed) }}</span>
            <span v-if="webClosureStages.missing.length">
              缺失页型 {{ pageTypeNames(webClosureStages.missing) }}
            </span>
            <span v-if="webClosureStages.unrecognized.length">
              未识别页型 {{ pageTypeNames(webClosureStages.unrecognized) }}
            </span>
            <span v-if="webClosureStages.normalization">
              归一化协议 {{ webClosureStages.normalization }}
            </span>
          </div>
          <WebStageDurationPanel
            v-if="task.stage_durations"
            :durations="task.stage_durations"
          />
        </section>


        <div v-if="journeyChips.length" class="journey-chip-row">
          <span class="journey-chip-label">用户旅程</span>
          <div class="journey-chip-flow">
            <template v-for="(chip, index) in journeyChips" :key="chip.key">
              <span class="journey-chip" :class="`chip-${chip.status}`">
                {{ chip.label }}
                <em v-if="chip.status === 'current'">当前</em>
              </span>
              <n-icon v-if="index < journeyChips.length - 1" size="14" class="journey-chip-arrow">
                <ChevronForwardOutline />
              </n-icon>
            </template>
          </div>
        </div>


        <div class="info-grid">
          <div v-for="item in overviewItems" :key="item.label" class="info-cell">
            <span>{{ item.label }}</span>
            <strong>{{ item.value }}</strong>
          </div>
          <div v-if="task.remark" class="info-cell journey-cell">
            <span>备注</span>
            <p class="remark-text">{{ task.remark }}</p>
          </div>
        </div>
        <div
          v-if="webExecutionPhaseLabel"
          class="agent-conclusion tone-running"
          data-testid="web-execution-queue-state"
        >
          <div class="conclusion-head">
            <strong>{{ webExecutionPhaseLabel }}</strong>
          </div>
          <p v-if="task.execution_phase === 'queued' || task.execution_phase === 'pending'">
            队列位置 {{ task.queue_position ?? '未知' }} · 前方任务 {{ task.tasks_ahead ?? '未知' }} · 在线适配 Worker {{ task.available_worker_count ?? '未知' }}
          </p>
          <p v-if="task.start_condition">启动条件：{{ task.start_condition }}</p>
          <p v-if="task.failure_reason">{{ task.failure_reason }}</p>
        </div>
      </div>

      <div class="detail-grid single">
        <section class="timeline-panel workstation-glass-panel">
          <div class="panel-heading table-heading">
            <div class="log-title">
              <n-icon size="21"><ImageOutline /></n-icon>
              <div>
                <h3>页面识别时间线</h3>
                <p v-if="sectionLoading.timeline" data-testid="timeline-summary">页面加载中…</p>
                <p v-else data-testid="timeline-summary">
                  共 {{ timelineNodes.length }} {{ task.target_platform === 'h5' ? '条页面观察' : '个页面' }}<template v-if="!isExplorationOnly && sectionLoading.rules">，规则结果加载中</template><template v-else-if="!isExplorationOnly && inventoryRuleSummary">，清单 {{ inventoryRuleSummary.total }} 条规则，实际使用 {{ inventoryRuleSummary.used }} 条，实际执行 {{ inventoryRuleSummary.executions }} 次</template><template v-else-if="!isExplorationOnly && ruleTally.total">，规则 {{ ruleTally.total }} 条：{{ tallySummaryText(ruleTally) }}</template>。点「详情」看每一页是怎么走的、哪些规则没通过或尚未完成判定
                </p>
              </div>
            </div>
            <span v-if="task.status === 'running' || task.status === 'queued'" class="live-badge">实时监控中</span>
          </div>

          <div v-if="guidePageOnly" class="guide-page-hint" data-testid="guide-page-hint">
            <strong>这个地址打开是 App 下载 / 引导页</strong>
            <p>页面上只有下载或引导入口，按规则不会去点，所以没有可以继续探索的页面，只看了这一页。请换成产品真正的站点地址（不是下载页）后重新发起质检；要检查 App，请上传安装包。</p>
          </div>

          <div v-if="timelineNodes.length" class="timeline-list">
            <article
              v-for="node in visibleTimelineNodes"
              :key="node.logical_page_id || node.page_capture_id || node.node_seq"
              class="timeline-node"
              :class="[nodeStatusClass(node), { 'is-current': node.is_current }]"
            >
              <div class="timeline-rail">
                <span class="timeline-dot"></span>
                <span class="timeline-time mono">{{ formatTime(node.captured_at || node.first_visited_at) }}</span>
              </div>
              <div class="timeline-card">
                <div class="timeline-thumb">
                  <img
                    v-if="nodeThumbUrl(node)"
                    :src="nodeThumbUrl(node)"
                    :alt="node.page_type_name || node.step_name || '页面截图'"
                    loading="lazy"
                  />
                  <div v-else class="thumb-placeholder">
                    <n-icon size="22"><ImageOutline /></n-icon>
                  </div>
                </div>
                <div class="timeline-body">
                  <div class="timeline-title-row">
                    <strong>{{ nodeDisplayName(node) }}</strong>
                    <span
                      v-if="popupStateBadge(node)"
                      class="rs-badge"
                      :class="popupStateBadge(node)?.cls"
                    >
                      {{ popupStateBadge(node)?.label }}
                    </span>
                    <span
                      v-if="!popupStateBadge(node)"
                      class="rs-badge"
                      :class="nodeStatusBadge(node).cls"
                    >
                      {{ nodeStatusBadge(node).label }}
                    </span>
                    <span v-if="nodeSourceLabel(node)" class="discovered-tag">{{ nodeSourceLabel(node) }}</span>
                    <span v-if="!isExplorationOnly && node.result_rollup && !node.is_current" class="rs-badge rollup" :class="ruleDisplayMeta(node.result_rollup).cls">
                      {{ node.result_rollup_label }}
                    </span>
                  </div>
                  <p class="timeline-subline">
                    {{ popupStateBadge(node) ? '宿主页面' : '页面类型' }}: {{ node.page_type_name || '待识别' }}
                    <template v-if="node.url"> · <span class="mono url-text">{{ node.url }}</span></template>
                  </p>
                  <template v-if="node.status === 'pending' || node.status === 'skipped'">
                    <p class="timeline-metrics muted">
                      {{ node.status === 'pending' ? '待识别' : '已跳过' }}
                      <template v-if="!isExplorationOnly && node.rule_progress.total != null"> · 预计规则 {{ node.rule_progress.total }} 项</template>
                    </p>
                  </template>
                  <template v-else>
                    <p class="timeline-metrics">
                      <span v-if="(node.capture_count ?? 0) > 1">证据版本 {{ node.capture_count }} 个</span>
                      <span v-if="!isExplorationOnly">{{ nodeRuleSummary(node) }}</span>
                    </p>
                    <div v-if="!isExplorationOnly && node.is_current && task.status === 'running'" class="node-progress">
                      <div class="node-progress-track">
                        <div class="node-progress-fill" :style="{ width: `${nodeProgressPercent(node)}%` }"></div>
                      </div>
                      <span class="mono">{{ nodeProgressPercent(node) }}%</span>
                    </div>
                    <p v-if="!isExplorationOnly && node.is_current && currentRuleText(node)" class="current-rule-line">
                      正在执行规则: {{ currentRuleText(node) }}
                    </p>
                    <p
                      v-if="node.inspection?.failure_message && node.inspection.failure_code !== 'PAGE_TYPE_OUTSIDE_INSPECTION_SCOPE'"
                      class="page-failure-line"
                    >
                      这一页的检查中途出了问题，点「详情」看原因
                    </p>
                  </template>
                </div>
                <button
                  type="button"
                  class="detail-link node-detail-button"
                  :data-testid="`node-detail-${node.node_seq}`"
                  @click="openPageDetail(nodeKeyOf(node))"
                >
                  详情
                </button>
              </div>
            </article>
          </div>
          <div v-else-if="sectionLoading.timeline" class="empty-block section-loading" data-testid="timeline-loading">
            <n-spin size="small" />
            <span>页面加载中…</span>
          </div>
          <div v-else class="empty-block">
            暂无页面节点：任务尚未开始，或创建时未配置用户旅程（执行后按实际访问顺序生成）
          </div>
          <button
            v-if="timelineNodes.length > TIMELINE_PREVIEW_LIMIT"
            type="button"
            class="detail-link timeline-more"
            data-testid="timeline-more"
            @click="timelineExpanded = !timelineExpanded"
          >
            {{ timelineExpanded ? '收起页面' : `展开全部 ${timelineNodes.length} ${task.target_platform === 'h5' ? '条页面观察' : '个页面'}` }}
          </button>
          <article
            v-if="!isExplorationOnly && taskLevelRules.length"
            class="timeline-node task-level-node"
            data-testid="task-level-node"
          >
            <div class="timeline-rail">
              <span class="timeline-dot"></span>
              <span class="timeline-time mono">整体</span>
            </div>
            <div class="timeline-card">
              <div class="timeline-body">
                <div class="timeline-title-row">
                  <strong>整体检查</strong>
                  <span class="discovered-tag">不属于某一个页面</span>
                </div>
                <p class="timeline-subline">整体检查，以及没有绑定页面的适用性核对、范围未选择和未执行处置</p>
                <p class="timeline-metrics">
                  <span>{{ ruleSummaryText(taskLevelRules) }}</span>
                </p>
              </div>
              <button
                type="button"
                class="detail-link node-detail-button"
                data-testid="task-level-detail"
                @click="openPageDetail(TASK_LEVEL_KEY)"
              >
                详情
              </button>
            </div>
          </article>
        </section>

      </div>

      <section class="screenshot-panel workstation-glass-panel">
        <div class="panel-heading table-heading">
          <div class="log-title">
            <n-icon size="21"><ImageOutline /></n-icon>
            <div>
              <h3>截图证据</h3>
              <p>打开合集，查看每次操作的前后状态、操作目的和截图关联规则</p>
            </div>
          </div>
          <n-button v-if="hasScreenshots" secondary size="small" class="ghost-action" @click="downloadAllScreenshots">
            <template #icon>
              <n-icon><DownloadOutline /></n-icon>
            </template>
            下载全部截图
          </n-button>
        </div>
        <div v-if="hasScreenshots" class="screenshot-groups">
          <article v-for="group in screenshotGroups" :key="group.page_group_key" class="screenshot-group">
            <header>
              <strong>{{ group.page_display_name }}</strong>
              <span>{{ screenshotGroupCountLabel(group) }}</span>
            </header>
            <div class="screenshot-grid">
              <button
                v-for="shot in group.items"
                :key="shot.artifact_id"
                type="button"
                class="screenshot-cell"
                :class="{ 'is-collection': screenshotImageCount(shot) > 1 }"
                data-testid="screenshot-collection-card"
                :data-artifact-id="shot.artifact_id"
                :aria-label="screenshotImageCount(shot) > 1
                  ? `查看${group.page_display_name}截图合集，共${screenshotImageCount(shot)}张`
                  : `查看${group.page_display_name}截图`"
                @click="openScreenshotCollection(group, shot)"
              >
                <img :src="evidenceUrl(shot.artifact_id)" :alt="group.page_display_name" loading="lazy" />
                <span v-if="occlusionLabel(shot)" class="occ-badge">{{ occlusionLabel(shot) }}</span>
                <span v-if="screenshotImageCount(shot) > 1" class="dup-badge collection-badge">
                  合集 · {{ screenshotImageCount(shot) }}张
                </span>
                <span v-else-if="shot.duplicate_count > 1" class="dup-badge">
                  同图采集{{ shot.duplicate_count }}次
                </span>
                <span v-if="screenshotImageCount(shot) > 1" class="collection-link">查看合集</span>
                <span v-if="shot.related_rules.length" class="rel-badge" :class="ruleDisplayMeta(shot.related_rules[0].display_status).cls">
                  {{ shot.related_rules[0].display_status_label }}
                </span>
              </button>
            </div>
          </article>
        </div>
        <div v-else-if="sectionLoading.screenshots" class="empty-block section-loading" data-testid="screenshots-loading">
          <n-spin size="small" />
          <span>截图加载中…</span>
        </div>
        <div v-else class="empty-block">暂无截图证据（任务执行后由页面采集自动生成）</div>
      </section>


      <section v-if="showDiagnostics" class="diagnostics-panel workstation-glass-panel" data-testid="diagnostics-panel">
        <button type="button" class="collapse-head" data-testid="diagnostics-toggle" @click="diagnosticsExpanded = !diagnosticsExpanded">
          <div class="log-title">
            <n-icon size="21"><TerminalOutline /></n-icon>
            <div>
              <h3>研发排查</h3>
              <p>派发与通知、页面图、补采缺口、日志等技术明细，供研发排查问题；只在管理后台显示，默认收起</p>
            </div>
          </div>
          <div class="collapse-side">
            <span class="collapse-toggle">{{ diagnosticsExpanded ? '收起' : '展开' }}</span>
          </div>
        </button>
        <div v-if="diagnosticsExpanded" class="diagnostics-body">
          <div class="diagnostics-toolbar">
            <n-button
              quaternary
              size="small"
              class="progress-export-action"
              title="导出平台能力进展 Excel"
              @click="exportPlatformProgress"
            >
              <template #icon>
                <n-icon><DownloadOutline /></n-icon>
              </template>
              导出进展
            </n-button>
          </div>
          <div class="info-grid diagnostics-info">
            <div v-for="item in technicalInfoItems" :key="item.label" class="info-cell">
              <span>{{ item.label }}</span>
              <strong>{{ item.value }}</strong>
            </div>
          </div>
          <div
            v-if="agentRun && (agentRun.final_text || agentRun.error_message)"
            class="agent-conclusion"
            :class="`tone-${agentConclusionStatusMeta.tone}`"
          >
            <div class="conclusion-head">
              <strong>Agent 结论</strong>
              <span class="status-pill compact" :class="`tone-${agentConclusionStatusMeta.tone}`">
                <span class="status-dot"></span>
                {{ agentConclusionStatusMeta.label }}
              </span>
            </div>
            <p>{{ agentRun.error_message || agentRun.final_text }}</p>
          </div>
          <section v-if="h5Coverage" class="h5-result-banner">
            <div class="h5-result-heading">
              <div>
                <strong>H5 覆盖范围</strong>
                <p>
                  覆盖口径：{{ H5_AUTHORITY_SOURCE_LABELS[h5Coverage.authority_source] ?? h5Coverage.authority_source }}
                  · {{ h5Coverage.revision != null ? `第 ${h5Coverage.revision} 版` : '历史兼容' }}
                </p>
              </div>
              <span>{{ h5Coverage.completion_ready ? '覆盖完整' : '覆盖未完成' }}</span>
            </div>
            <div class="h5-result-counts">
              <span>已覆盖 {{ pageTypeNames(h5Coverage.covered_page_types) }}</span>
              <span>要求页型 {{ pageTypeNames(h5Coverage.required_page_types, '按实际页面') }}</span>
              <span v-if="h5Coverage.missing_page_types.length">
                缺失 {{ pageTypeNames(h5Coverage.missing_page_types) }}
              </span>
              <span>评估作用域 {{ h5Coverage.assessment_scope_ready ? '完整' : '不完整' }}</span>
            </div>
          </section>
          <AppStagePanel
            v-if="isAppTask"
            :stage="appStage"
            :loading="sectionLoading.appStage && !appStage"
            :execution-mode="task.execution_mode"
            :can-test-rules="canTestRules"
          />

          <PageGraphPanel
            v-if="isAppTask && task?.task_id"
            :task-id="task.task_id"
            :loading-parent="loading"
          />

          <DeliveryIncidentPanel
            v-if="canOperateDeliveryIncidents"
            :task-id="task.task_id"
            :target-platform="task.target_platform"
          />
          <div v-if="frontierIssues.length" class="frontier-issues">
            <div class="frontier-issues-title">
              <strong>未产出页面的 Frontier 终态</strong>
              <span>{{ frontierIssues.length }} 项</span>
            </div>
            <article v-for="item in frontierIssues" :key="item.exploration_action_id">
              <div>
                <span class="frontier-status" :class="`frontier-${item.status}`">
                  {{ frontierIssueStatusText(item.status) }}
                </span>
                <strong>{{ explorationActionText(item.action) }} · {{ item.target || item.purpose || '未指定目标' }}</strong>
              </div>
              <p>{{ item.failure_message || item.failure_reason || '已按探索边界收口' }}</p>
            </article>
          </div>
          <WebReportExplanationPanel
            v-if="isWebV2FullInspection"
            :explanation="timeline?.web_report_explanation"
            :loading="sectionLoading.timeline"
          />
          <PageExplorationPanel
            v-if="explorationSummary?.tracking_available"
            :summary="explorationSummary"
            variant="embedded"
            collapsible
            :default-expanded="false"
          />
          <ClosureOutcomePanel
            v-if="closureOutcome"
            :outcome="closureOutcome"
            :default-expanded="false"
          />
          <ExplorationProcessPanel
            v-if="pageGraph || evidenceGaps || ruleReuse || pageTypeJudgements"
            :graph="pageGraph"
            :routes="recoveryRoutes"
            :gaps="evidenceGaps"
            :reuse="ruleReuse"
            :judgements="pageTypeJudgements"
            :screenshots="screenshots"
            :screenshots-loading="sectionLoading.screenshots"
            :default-expanded="false"
          />
          <section class="anomaly-panel workstation-glass-panel">
            <div class="panel-heading compact-heading">
              <div class="log-title">
                <n-icon size="18"><WarningOutline /></n-icon>
                <div>
                  <h3>最近异常</h3>
                </div>
              </div>
              <span v-if="stats?.recent_anomalies.length" class="anomaly-count">{{ stats.recent_anomalies.length }} 条待关注</span>
            </div>
            <div v-if="stats?.recent_anomalies.length" class="anomaly-list">
              <article v-for="item in stats.recent_anomalies" :key="`${item.rule_code}-${item.occurred_at}`" class="anomaly-card" :class="[ruleDisplayMeta(item.display_status).cls, severityClass(item.severity_display)]">
                <div class="anomaly-head">
                  <span v-if="item.severity_display" class="severity-chip" :class="severityClass(item.severity_display)">{{ item.severity_display }}</span>
                  <strong>{{ item.rule_name }}</strong>
                  <time class="mono">{{ formatTime(item.occurred_at) }}</time>
                </div>
                <p>
                  {{ item.bound_page_type_name || '未识别页面' }}
                  · {{ item.display_status_label }}
                  <template v-if="item.problem_desc"> · {{ item.problem_desc }}</template>
                </p>
              </article>
            </div>
            <div v-else-if="sectionLoading.stats" class="empty-block slim section-loading">
              <n-spin size="small" />
              <span>加载中…</span>
            </div>
            <div v-else class="empty-block slim">暂无证据不足、待人工复核或执行异常规则</div>
          </section>
          <section
            v-if="h5PublicationProjection"
            class="inspection-work-panel workstation-glass-panel"
          >
            <div class="panel-heading table-heading">
              <div class="log-title">
                <n-icon size="21"><ClipboardOutline /></n-icon>
                <div>
                  <h3>H5 证据通知闭环</h3>
                  <p>按当前收口 epoch 展示 Publication、Outbox/Inbox、Work 与质检完成事实</p>
                </div>
              </div>
              <span class="rs-badge rs-running">
                当前：{{ publicationStageLabel(h5PublicationProjection.current_stage) }}
              </span>
            </div>

            <div class="work-summary-chips publication-stage-chips">
              <span>Publication 已持久化 {{ h5PublicationProjection.publication_persisted_count }}</span>
              <span>唤醒已送达 {{ h5PublicationProjection.wakeup_delivered_count }}</span>
              <span>Work 已领取 {{ h5PublicationProjection.work_claimed_count }}</span>
              <span>质检已完成 {{ h5PublicationProjection.inspection_completed_count }}</span>
              <span v-if="h5PublicationProjection.failed_count">
                异常 {{ h5PublicationProjection.failed_count }}
              </span>
            </div>

            <div v-if="h5PublicationProjection.items.length" class="inspection-work-list">
              <article
                v-for="item in h5PublicationProjection.items"
                :key="item.publication_id"
                class="inspection-work-card"
              >
                <div class="inspection-work-head">
                  <div>
                    <strong>{{ item.page_type || '未识别页面' }}</strong>
                    <p class="mono">publication={{ item.publication_id }}</p>
                  </div>
                  <div class="inspection-work-badges">
                    <span class="rs-badge" :class="publicationStatusMeta(item.status).cls">
                      {{ publicationStatusMeta(item.status).label }}
                    </span>
                    <span class="source-badge" :class="item.mode === 'enforced' ? 'source-execution' : 'source-bootstrap'">
                      {{ item.mode === 'enforced' ? '强制执行' : '影子审计' }}
                    </span>
                  </div>
                </div>
                <div class="inspection-work-meta">
                  <span>阶段：{{ publicationStageLabel(item.current_stage) }}</span>
                  <span>规则：{{ item.candidate_rule_count ?? 0 }}</span>
                  <span>批次：{{ item.published_work_item_count }} / {{ item.expected_batch_count ?? 0 }}</span>
                  <span>Outbox：{{ item.outbox_published_count }} 已发布 / {{ item.outbox_pending_count }} 待处理</span>
                  <span>Inbox：{{ item.inbox_completed_count }} 已完成 / {{ item.inbox_pending_count }} 待处理</span>
                  <span>Work：{{ item.terminal_work_count }} 终态 / {{ item.linked_work_count }} 总数</span>
                  <span>Attempt：{{ item.inspection_attempt_count }}</span>
                </div>
                <p v-if="item.failure_code" class="page-failure-line">
                  <b>{{ item.failure_code }}</b>
                  · {{ item.failure_message || '无错误详情' }}
                </p>
              </article>
            </div>
            <div v-else class="empty-block">尚无 H5 Publication；页面证据尚未通过 Inventory 门禁</div>
          </section>
          <section v-if="!isExplorationOnly" class="inspection-work-panel workstation-glass-panel">
            <div class="panel-heading table-heading">
              <div class="log-title">
                <n-icon size="21"><ClipboardOutline /></n-icon>
                <div>
                  <h3>质检工作项</h3>
                  <p>展示规则派发、补证、Attempt 和页面归属，辅助验收发布闭环</p>
                </div>
              </div>
              <div v-if="inspectionWorkSummary" class="work-summary-chips">
                <span>总数 {{ inspectionWorkSummary.work_item_count }}</span>
                <span>强制 {{ inspectionWorkSummary.enforced_count }}</span>
                <span>未终态 {{ inspectionWorkSummary.nonterminal_count }}</span>
                <span>失败 {{ inspectionWorkSummary.failed_count }}</span>
              </div>
            </div>

            <div v-if="visibleInspectionWorkItems.length" class="inspection-work-list">
              <article
                v-for="item in visibleInspectionWorkItems"
                :key="item.work_item_id"
                class="inspection-work-card"
              >
                <div class="inspection-work-head">
                  <div>
                    <strong>{{ workItemPageName(item) }}</strong>
                    <p class="mono">work_item={{ item.work_item_id }}</p>
                  </div>
                  <div class="inspection-work-badges">
                    <span class="rs-badge" :class="workItemStatusMeta(item.status).cls">
                      {{ workItemStatusMeta(item.status).label }}
                    </span>
                    <span class="source-badge" :class="item.control_mode === 'enforced' ? 'source-execution' : 'source-bootstrap'">
                      {{ item.control_mode === 'enforced' ? '强制执行' : '影子验证' }}
                    </span>
                  </div>
                </div>
                <div class="inspection-work-meta">
                  <span>策略：{{ item.strategy }}</span>
                  <span>分类：{{ item.category || '-' }}</span>
                  <span>会话依赖：{{ item.session_dependency }}</span>
                  <span>尝试：{{ item.attempt_count }} / {{ item.max_attempts }}</span>
                  <span>输入：<b class="mono">{{ compactHash(item.input_hash) }}</b></span>
                </div>
                <p class="inspection-rule-line">
                  <b>规则成员：</b>{{ workItemRuleSummary(item) }}
                </p>
                <div v-if="item.rules.length" class="inspection-rule-chips">
                  <span
                    v-for="rule in item.rules"
                    :key="`${item.work_item_id}:${rule.rule_id}`"
                    class="rule-member-chip"
                  >
                    <b class="mono">{{ rule.rule_code }}</b>
                    {{ rule.status }}
                    <template v-if="rule.rule_execution_id"> · 已落库</template>
                    <template v-if="rule.last_error_code"> · {{ rule.last_error_code }}</template>
                  </span>
                </div>
                <div v-if="item.attempts.length" class="inspection-attempt-list">
                  <article
                    v-for="attempt in item.attempts"
                    :key="attempt.attempt_id"
                    class="inspection-attempt"
                  >
                    <span class="mono">#{{ attempt.attempt_no }}</span>
                    <span>{{ attempt.status }}</span>
                    <span>{{ attempt.has_result_payload ? '已有结果' : '无结果载荷' }}</span>
                    <span>{{ formatTime(attempt.finished_at || attempt.updated_at) }}</span>
                    <span v-if="attempt.error_code" class="attempt-error">
                      {{ attempt.error_code }} · {{ attempt.error_message || '无错误详情' }}
                    </span>
                  </article>
                </div>
                <p v-if="item.last_error_code" class="page-failure-line">
                  <b>{{ item.last_error_code }}</b>
                  · {{ item.last_error_message || '无错误详情' }}
                </p>
              </article>
            </div>
            <div v-else-if="sectionLoading.workItems" class="empty-block section-loading">
              <n-spin size="small" />
              <span>质检工作项加载中…</span>
            </div>
            <div v-else class="empty-block">
              暂无质检工作项：任务尚未进入规则派发，或当前版本未启用持久化质检协议
            </div>
          </section>
          <section class="logs-panel workstation-glass-panel">
            <button type="button" class="collapse-head" @click="logsExpanded = !logsExpanded">
              <div class="log-title">
                <n-icon size="21"><TerminalOutline /></n-icon>
                <div>
                  <h3>执行日志</h3>
                  <p>任务启动 / 页面识别 / 规则执行 / 异常事件摘要</p>
                </div>
              </div>
              <div class="collapse-side">
                <span class="log-count mono">{{ logTotal }} 条</span>
                <span class="collapse-toggle">{{ logsExpanded ? '收起' : '展开' }}</span>
              </div>
            </button>
            <div v-if="logsExpanded" class="logs-body">
              <div class="logs-toolbar">
                <n-button size="tiny" secondary class="ghost-action" @click="exportLogs">
                  <template #icon>
                    <n-icon><DownloadOutline /></n-icon>
                  </template>
                  导出日志
                </n-button>
              </div>
              <div v-if="logItems.length" class="log-lines mono">
                <p v-for="log in logItems" :key="log.id" class="log-line" :class="logLevelClass(log.level)">
                  <time>[{{ formatTime(log.ts) }}]</time>
                  <b>{{ log.level }}</b>
                  <span>{{ log.message }}</span>
                </p>
              </div>
              <div v-else-if="sectionLoading.logs" class="empty-block slim section-loading">
                <n-spin size="small" />
                <span>日志加载中…</span>
              </div>
              <div v-else class="empty-block slim">暂无执行日志（旧任务执行于日志功能上线前）</div>
              <button v-if="canLoadMoreLogs" type="button" class="detail-link more-logs" @click="loadMoreLogs">
                加载更多（已显示 {{ logItems.length }} / {{ logTotal }}）
              </button>
            </div>
          </section>

          <section class="log-panel workstation-glass-panel">
            <button type="button" class="collapse-head" @click="traceExpanded = !traceExpanded">
              <div class="log-title">
                <n-icon size="21"><TerminalOutline /></n-icon>
                <div>
                  <h3>Workflow 执行轨迹</h3>
                  <p>开发视角：逐步 Skill/Tool 输入输出</p>
                </div>
              </div>
              <div class="collapse-side">
                <span v-if="agentRun" class="log-count mono">{{ agentRun.total_steps || agentRun.steps.length }} 步</span>
                <span class="collapse-toggle">{{ traceExpanded ? '收起' : '展开' }}</span>
              </div>
            </button>
            <template v-if="traceExpanded">
              <template v-if="agentRun">
                <div class="run-summary">
                  <span class="status-pill compact" :class="`tone-${agentRunStatusMeta.tone}`">
                    <span class="status-dot"></span>
                    {{ agentRunStatusMeta.label }}
                  </span>
                  <span>步数 {{ agentRun.total_steps || agentRun.steps.length }}</span>
                  <span>Tokens {{ formatNumber(agentRun.total_tokens) }}</span>
                  <span class="run-cost">成本 {{ agentRunCostText }}</span>
                  <span>{{ agentRunTokenBreakdownText }}</span>
                  <span>耗时 {{ agentRunDurationText }}</span>
                  <span v-if="agentRun.stop_reason">结束原因 {{ agentRun.stop_reason }}</span>
                </div>
                <p v-if="agentRun.error_message" class="run-error">{{ agentRun.error_message }}</p>
                <div v-if="agentRun.steps.length" class="trace-list">
                  <article v-for="step in agentRun.steps" :key="step.step_seq" class="trace-step" :class="{ 'is-blocked': isBlockedStep(step) }">
                    <button
                      type="button"
                      class="trace-head"
                      :class="{ expandable: stepHasIO(step) }"
                      @click="toggleStep(step)"
                    >
                      <span class="trace-seq mono">{{ step.step_seq.toString().padStart(2, '0') }}</span>
                      <span class="trace-kind" :class="isSkillStep(step) ? 'kind-skill' : 'kind-tool'">
                        {{ isSkillStep(step) ? 'Skill' : 'Tool' }}
                      </span>
                      <span class="trace-name">{{ stepTitle(step) }}</span>
                      <span class="trace-meta">
                        <b :class="isBlockedStep(step) ? 'warning' : stepStatusClass(step.status)">{{ stepStatusText(step) }}</b>
                        <i class="mono">{{ latencyText(step.latency_ms) }}</i>
                        <em v-if="stepHasIO(step)" class="trace-toggle">
                          {{ expandedSteps.has(step.step_seq) ? '收起' : '输入 / 输出' }}
                        </em>
                      </span>
                    </button>
                    <p
                      v-if="step.thought_summary || step.error_message"
                      class="trace-note"
                      :class="{ 'is-error': !!step.error_message }"
                    >
                      {{ step.error_message || step.thought_summary }}
                    </p>
                    <div v-if="expandedSteps.has(step.step_seq)" class="trace-io">
                      <div class="io-block">
                        <span>输入</span>
                        <pre>{{ hasPayload(step.capability_input) ? formatPayload(step.capability_input) : '（空）' }}</pre>
                      </div>
                      <div class="io-block">
                        <span>输出</span>
                        <pre>{{ hasPayload(step.capability_output) ? formatPayload(step.capability_output) : '（未记录出参，旧运行不含该数据）' }}</pre>
                      </div>
                    </div>
                  </article>
                </div>
                <div v-else class="empty-block">该运行未记录分步轨迹</div>
              </template>
              <div v-else class="empty-block">尚无 Agent 运行记录（任务排队中或尚未派发）</div>
            </template>
          </section>
        </div>
      </section>

      <H5CheckpointPanel v-if="task.target_platform === 'h5' && !isExplorationOnly"
        :task-id="task.task_id" :refresh-key="`${task.run_id}:${task.status}:${task.finalization_state}`" />
      <PageDetailDialog
        v-model:show="pageDetailVisible"
        :title="pageDetail?.title ?? ''"
        :subtitle="pageDetail?.subtitle ?? null"
        :story-title="pageDetail?.storyTitle"
        :story="pageDetail?.story ?? []"
        :rules="pageDetail?.rules ?? []"
        :exploration-only="isExplorationOnly"
        :screenshot-ids="ruleScreenshotIds"
        @view-evidence="openViewerForRule"
        @confirm="openRuleReview"
      />

      <ScreenshotEvidenceViewer
        v-if="viewerVisible"
        :entries="viewerItems"
        :title="viewerTitle"
        :collection="viewerIsCollection"
        :initial-artifact-id="viewerInitialArtifactId"
        :image-url="evidenceUrl"
        :image-error="evidenceFiles.getError"
        :format-time="formatTime"
        @close="viewerVisible = false"
        @download="downloadViewerScreenshot"
      />
    </template>
  </section>
</template>

<style scoped>
.long-execution-alert { margin-top: 16px; padding: 14px 16px; border: 1px solid var(--workstation-warning); border-radius: 10px; background: var(--workstation-panel-soft); }
.long-execution-alert p { margin: 8px 0; color: var(--workstation-text-muted); }
.diagnostics-panel {
  margin-top: 18px;
}

.detail-grid.single {
  grid-template-columns: minmax(0, 1fr);
}

/* 同一样式块后面的 .detail-link 会把边框、字号清掉，这里要比它具体 */
.detail-link.node-detail-button {
  flex-shrink: 0;
  align-self: center;
  margin-left: auto;
  padding: 4px 14px;
  border: 1px solid var(--workstation-primary);
  border-radius: 999px;
  font-size: 12px;
}

.detail-link.node-detail-button:hover {
  background: var(--workstation-primary);
  color: #fff;
  text-decoration: none;
}

.task-level-node .timeline-card {
  align-items: center;
}

.diagnostics-body {
  display: grid;
  gap: 16px;
  padding: 0 20px 20px;
}

.diagnostics-toolbar {
  display: flex;
  justify-content: flex-end;
}

.diagnostics-info {
  margin-top: 0;
}

.timeline-more {
  display: block;
  margin: 12px auto 0;
}

.rule-pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 12px;
  padding: 12px 4px 0;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.rule-pager button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.node-attention-panel > summary {
  cursor: pointer;
  color: var(--workstation-text-muted);
  font-size: 12px;
}

.h5-result-banner {
  margin-top: 18px;
  padding: 18px 20px;
  border: 1px solid rgba(56, 189, 248, 0.3);
  border-radius: 14px;
  background: rgba(14, 165, 233, 0.08);
}

.h5-result-heading,
.h5-result-counts {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.h5-result-heading {
  justify-content: space-between;
}

.h5-result-heading strong {
  color: var(--workstation-text-strong);
  font-size: 15px;
}

.h5-result-heading p {
  margin: 5px 0 0;
  color: var(--workstation-text-muted);
  font-size: 11px;
}

.h5-result-heading > span,
.h5-result-counts span {
  padding: 5px 9px;
  border-radius: 999px;
  color: var(--workstation-primary);
  background: rgba(14, 165, 233, 0.15);
  font-size: 10px;
  font-weight: 800;
}

.h5-result-counts {
  margin-top: 14px;
}

.h5-result-needs-review {
  border-color: rgba(251, 191, 36, 0.35);
  background: rgba(245, 158, 11, 0.08);
}

.h5-result-technical-failed {
  border-color: rgba(251, 113, 133, 0.35);
  background: rgba(244, 63, 94, 0.08);
}

.h5-result-banner code {
  display: block;
  margin-top: 10px;
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.h5-review-toggle {
  padding: 5px 10px;
  border: 1px solid rgba(251, 191, 36, 0.45);
  border-radius: 999px;
  color: var(--workstation-warning);
  background: rgba(245, 158, 11, 0.12);
  font: inherit;
  font-size: 10px;
  font-weight: 700;
  cursor: pointer;
}

.h5-review-list {
  display: grid;
  gap: 6px;
  margin: 12px 0 0;
  padding: 0;
  list-style: none;
}

.h5-review-list li {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  background: var(--workstation-panel-soft);
  font-size: 11px;
  color: var(--workstation-text);
}

.h5-review-list b {
  color: var(--workstation-text-strong);
}

.h5-review-status {
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
}

.h5-review-status.is-insufficient {
  color: var(--workstation-warning);
  background: rgba(245, 158, 11, 0.16);
}

.h5-review-status.is-failed {
  color: var(--workstation-danger);
  background: rgba(239, 68, 68, 0.16);
}

.h5-review-reasons {
  color: var(--workstation-text-muted);
}

.task-detail-shell {
  display: flex;
  flex-direction: column;
  gap: 24px;
}

.breadcrumb-row,
.overview-header,
.overview-title-wrap,
.title-line,
.overview-actions,
.progress-meta,
.progress-footnote,
.journey-flow,
.panel-heading,
.donut-wrap,
.legend-list,
.table-heading,
.table-tools,
.search-field,
.section-title-row,
.log-title,
.candidate-card > div {
  display: flex;
  align-items: center;
}

.breadcrumb-row {
  gap: 8px;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.breadcrumb-link {
  padding: 0;
  border: 0;
  color: var(--workstation-text-faint);
  background: transparent;
  font: inherit;
  cursor: pointer;
}

.breadcrumb-link:hover {
  color: var(--workstation-primary);
}

.loading-panel,
.error-panel {
  padding: 64px 24px;
  border-radius: 16px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 16px;
  text-align: center;
}

.loading-panel p,
.error-panel p {
  margin: 0;
  color: var(--workstation-text-faint);
}

.partial-load-alert {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
  color: var(--workstation-warning);
  border-color: rgba(251, 191, 36, 0.35);
  background: rgba(245, 158, 11, 0.08);
  font-size: 11px;
}

.partial-load-alert .n-button {
  margin-left: auto;
}

.error-panel h3 {
  margin: 0;
  color: var(--workstation-text-strong);
}

.empty-block {
  padding: 40px 24px;
  border-radius: 14px;
  color: var(--workstation-text-faint);
  font-size: 11px;
  text-align: center;
}

/* 区块首次加载：转圈 + 「xx 加载中…」，占住空状态的位置 */
.empty-block.section-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

.empty-cell {
  color: var(--workstation-text-faint);
  text-align: center;
}

.overview-card {
  padding: 24px;
  border-radius: 16px;
}

.overview-header {
  justify-content: space-between;
  gap: 24px;
}

.overview-title-wrap {
  gap: 16px;
  min-width: 0;
}

.task-icon {
  width: 52px;
  height: 52px;
  border-radius: 14px;
  display: grid;
  place-items: center;
  color: #ffffff;
  background: linear-gradient(135deg, #0ea5e9, #2563eb);
  box-shadow: 0 0 28px rgba(14, 165, 233, 0.25);
}

.title-line {
  gap: 12px;
  flex-wrap: wrap;
}

.title-line h2,
.panel-heading h3,
.section-title-row h3,
.log-title h3,
.candidate-card strong {
  margin: 0;
  color: var(--workstation-text-strong);
  letter-spacing: 0;
}

.title-line h2 {
  font-size: 19px;
  line-height: 1.2;
  font-weight: 800;
}

.overview-title-wrap p,
.panel-heading p,
.section-title-row p,
.log-title p,
.candidate-card p {
  margin: 6px 0 0;
  color: var(--workstation-text-faint);
}

.overview-title-wrap p {
  font-size: 11px;
  overflow-wrap: anywhere;
}

.overview-actions {
  justify-content: flex-end;
  gap: 10px;
  flex-wrap: wrap;
}

.report-none-action {
  display: grid;
  gap: 4px;
  justify-items: end;
  max-width: 260px;
}

.report-none-action small {
  color: var(--workstation-text-faint);
  font-size: 11px;
  text-align: right;
}

.progress-export-action {
  min-height: 30px;
  border-radius: 9px;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.progress-export-action:hover {
  color: var(--workstation-primary);
  background: rgba(14, 165, 233, 0.1);
}

.ghost-action,
.primary-action {
  min-height: 38px;
  border-radius: 12px;
  font-weight: 700;
}

.progress-block {
  margin-top: 24px;
}

.progress-meta,
.progress-footnote,
.panel-heading,
.section-title-row {
  justify-content: space-between;
}

.progress-meta span,
.progress-footnote,
.info-cell span,
.legend-list,
.section-title-row span,
.log-time {
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.progress-meta strong {
  color: var(--workstation-primary);
  font-size: 14px;
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.progress-track {
  height: 12px;
  margin-top: 10px;
  border-radius: 999px;
  overflow: hidden;
  background: rgba(148, 163, 184, 0.1);
}

.progress-fill {
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #0ea5e9, #38bdf8);
  box-shadow: 0 0 12px rgba(14, 165, 233, 0.4);
}

.progress-footnote {
  margin-top: 8px;
}

.progress-review-note {
  margin: 8px 0 0;
  color: var(--workstation-text-faint);
  font-size: 12px;
  line-height: 1.6;
}

.info-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-top: 24px;
}

.info-cell {
  min-width: 0;
  padding: 16px;
  border: 1px solid var(--workstation-border);
  border-radius: 14px;
  background: var(--workstation-panel-soft);
}

.info-cell strong {
  display: block;
  margin-top: 8px;
  overflow: hidden;
  color: var(--workstation-text-strong);
  font-size: 11px;
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.journey-cell {
  grid-column: 1 / -1;
}

.journey-flow {
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.journey-flow b {
  padding: 8px 12px;
  border: 1px solid rgba(56, 189, 248, 0.18);
  border-radius: 999px;
  color: var(--workstation-primary);
  font-size: 10px;
  background: rgba(56, 189, 248, 0.08);
}

.journey-flow i {
  width: 28px;
  height: 1px;
  background: linear-gradient(90deg, rgba(56, 189, 248, 0.3), rgba(148, 163, 184, 0.1));
}

.remark-text {
  margin: 10px 0 0;
  color: var(--workstation-text);
  font-size: 11px;
  line-height: 1.7;
  white-space: pre-wrap;
}

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(320px, 1fr);
  gap: 24px;
}

.stats-panel,
.result-panel,
.log-panel,
.candidate-panel {
  border-radius: 16px;
  overflow: hidden;
}

.panel-heading {
  padding: 20px 24px;
  border-bottom: 1px solid var(--workstation-border);
  gap: 16px;
}

.panel-heading h3,
.section-title-row h3,
.log-title h3 {
  font-size: 14px;
  font-weight: 800;
}

.panel-heading p,
.section-title-row p,
.log-title p {
  font-size: 10px;
}

.live-badge {
  padding: 4px 10px;
  border: 1px solid rgba(56, 189, 248, 0.2);
  border-radius: 999px;
  color: var(--workstation-primary);
  font-size: 10px;
  font-weight: 700;
  background: rgba(56, 189, 248, 0.1);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 999px;
  background: currentColor;
}

.mono {
  color: var(--workstation-text-faint);
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 10px;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  padding: 20px 20px 0;
}

.stat-tile {
  padding: 18px 14px;
  border-radius: 13px;
  text-align: center;
}

.stat-tile strong {
  display: block;
  color: var(--workstation-text-strong);
  font-size: 22px;
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.stat-tile span {
  display: block;
  margin-top: 5px;
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.stat-success strong {
  color: var(--workstation-success);
}

.stat-warning strong {
  color: var(--workstation-warning);
}

.stat-muted strong {
  color: var(--workstation-text-muted);
}

.donut-wrap {
  gap: 18px;
  margin: 20px;
  padding: 18px;
  border: 1px solid var(--workstation-border);
  border-radius: 14px;
  background: var(--workstation-panel-soft);
}

.donut-ring {
  width: 86px;
  height: 86px;
  border-radius: 999px;
  display: grid;
  place-items: center;
  color: var(--workstation-text-strong);
  font-weight: 900;
  font-family: 'JetBrains Mono', Consolas, monospace;
  box-shadow: 0 0 24px rgba(52, 211, 153, 0.12);
}

.donut-ring span {
  width: 58px;
  height: 58px;
  border-radius: inherit;
  display: grid;
  place-items: center;
  background: var(--workstation-panel-strong);
}

.donut-wrap p {
  margin: 0 0 5px;
  color: var(--workstation-primary);
  font-weight: 800;
}

.donut-wrap span {
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.coverage-line {
  margin: 0 20px 16px;
  padding: 12px 14px;
  border: 1px solid var(--workstation-border);
  border-radius: 12px;
  background: var(--workstation-panel-soft);
}

.coverage-line strong {
  display: block;
  margin-bottom: 5px;
  color: var(--workstation-text-strong);
  font-size: 10px;
}

.coverage-line span {
  color: var(--workstation-text-faint);
  font-size: 10px;
  line-height: 1.6;
}

.legend-list {
  align-items: flex-start;
  flex-direction: column;
  gap: 10px;
  padding: 0 20px 20px;
}

.legend-list i {
  display: inline-block;
  width: 8px;
  height: 8px;
  margin-right: 8px;
  border-radius: 999px;
}

.legend-pass {
  background: var(--workstation-success);
}

.legend-warning {
  background: var(--workstation-warning);
}

.legend-error {
  background: var(--workstation-danger);
}

.table-heading {
  flex-wrap: wrap;
}

.table-tools {
  gap: 10px;
}

.search-field {
  gap: 8px;
  min-width: 230px;
  min-height: 36px;
  padding: 0 12px;
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  color: var(--workstation-text-faint);
  background: var(--workstation-input-bg);
}

.search-field input {
  width: 100%;
  border: 0;
  outline: 0;
  color: var(--workstation-text);
  background: transparent;
}

.table-wrap {
  overflow-x: auto;
}

table {
  width: 100%;
  border-collapse: collapse;
}

th,
td {
  padding: 16px 24px;
  border-bottom: 1px solid var(--workstation-border);
  text-align: left;
  vertical-align: top;
}

th {
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
}

td {
  color: var(--workstation-text);
  font-size: 11px;
}

td strong,
td span {
  display: block;
}

td strong {
  color: var(--workstation-text-strong);
  font-size: 11px;
}

td strong + span {
  margin-top: 6px;
  color: var(--workstation-text-faint);
  line-height: 1.6;
}

.status-pill,
.severity-badge {
  display: inline-flex;
  align-items: center;
  width: fit-content;
  gap: 7px;
  border-radius: 999px;
  font-weight: 800;
}

.status-pill {
  padding: 5px 10px;
  border: 1px solid currentColor;
  font-size: 10px;
  background: color-mix(in srgb, currentColor 12%, transparent);
}

.status-pill.compact {
  padding: 4px 8px;
  font-size: 10px;
}

.tone-running,
.line-running {
  color: var(--workstation-primary);
}

.tone-pass,
.line-pass {
  color: var(--workstation-success);
}

.tone-warning,
.line-warning {
  color: var(--workstation-warning);
}

.tone-error,
.line-error {
  color: var(--workstation-danger);
}

.tone-pending,
.line-pending {
  color: var(--workstation-text-faint);
}

.severity-badge {
  padding: 5px 9px;
  border: 1px solid currentColor;
  font-size: 10px;
  background: color-mix(in srgb, currentColor 12%, transparent);
}

.source-badge {
  display: inline-flex;
  padding: 4px 9px;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 800;
  white-space: nowrap;
  background: color-mix(in srgb, currentColor 10%, transparent);
}

.source-agent {
  color: var(--workstation-primary);
}

.source-execution {
  color: var(--workstation-success);
}

.source-bootstrap {
  color: var(--workstation-text-faint);
}

.reason-cell {
  min-width: 220px;
  max-width: 340px;
  line-height: 1.6;
}

.severity-critical,
.severity-high {
  color: var(--workstation-danger);
}

.severity-medium {
  color: var(--workstation-warning);
}

.severity-low {
  color: var(--workstation-success);
}

.severity-info {
  color: var(--workstation-primary);
}

.evidence-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.log-title {
  gap: 12px;
}

.agent-conclusion {
  margin-top: 18px;
  padding: 16px;
  border: 1px solid var(--workstation-border);
  border-left: 4px solid currentColor;
  border-radius: 12px;
  background: var(--workstation-panel-soft);
}

.conclusion-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.agent-conclusion strong {
  color: var(--workstation-text-strong);
  font-size: 11px;
}

.agent-conclusion p {
  margin: 10px 0 0;
  color: var(--workstation-text);
  font-size: 11px;
  line-height: 1.7;
}

.run-summary {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 14px;
  padding: 16px 24px 0;
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.run-cost {
  color: var(--workstation-primary);
  font-weight: 800;
}

.run-error {
  margin: 12px 24px 0;
  color: var(--workstation-danger);
  font-size: 11px;
}

.trace-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 16px;
  padding: 18px 24px 24px;
  background: var(--workstation-panel-soft);
}

.detail-grid .trace-list {
  max-height: 640px;
  overflow-y: auto;
}

.trace-step {
  flex-shrink: 0;
  border: 1px solid var(--workstation-border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--workstation-panel-soft);
}

.trace-step.is-blocked {
  border-color: rgba(251, 191, 36, 0.4);
}

.trace-head {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding: 12px 14px;
  border: 0;
  color: var(--workstation-text);
  background: transparent;
  font: inherit;
  font-size: 11px;
  text-align: left;
  cursor: default;
}

.trace-head.expandable {
  cursor: pointer;
}

.trace-head.expandable:hover {
  background: var(--workstation-panel-soft);
}

.trace-seq {
  flex-shrink: 0;
}

.trace-kind {
  flex-shrink: 0;
  padding: 3px 8px;
  border: 1px solid currentColor;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 800;
}

.kind-skill {
  color: #c084fc;
}

.kind-tool {
  color: var(--workstation-primary);
}

.trace-name {
  min-width: 0;
  overflow: hidden;
  color: var(--workstation-text-strong);
  font-weight: 700;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
  margin-left: auto;
}

.trace-meta b {
  font-size: 10px;
}

.trace-meta b.success {
  color: var(--workstation-success);
}

.trace-meta b.running {
  color: var(--workstation-primary);
}

.trace-meta b.error {
  color: var(--workstation-danger);
}

.trace-meta b.warning {
  color: var(--workstation-warning);
}

.trace-meta i {
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-style: normal;
}

.trace-toggle {
  color: var(--workstation-primary);
  font-size: 10px;
  font-style: normal;
  font-weight: 700;
}

.trace-note {
  margin: 0;
  padding: 0 14px 12px 62px;
  color: var(--workstation-text-faint);
  font-size: 10px;
  line-height: 1.6;
}

.trace-note.is-error {
  color: var(--workstation-danger);
}

.trace-io {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  padding: 0 14px 14px;
}

.io-block span {
  display: block;
  margin-bottom: 6px;
  color: var(--workstation-text-faint);
  font-size: 10px;
  font-weight: 800;
  text-transform: uppercase;
}

.io-block pre {
  max-height: 260px;
  margin: 0;
  padding: 12px;
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  overflow: auto;
  color: #a5f3fc;
  background: var(--workstation-panel-soft);
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 10px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.candidate-list {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
  padding: 20px 24px 24px;
}

.candidate-card {
  position: relative;
  padding: 16px;
  border: 1px solid var(--workstation-border);
  border-radius: 14px;
  background: var(--workstation-panel-soft);
}

.candidate-card > div {
  justify-content: space-between;
  gap: 12px;
}

.candidate-card strong {
  font-size: 11px;
}

.candidate-card p {
  padding-right: 72px;
  font-size: 11px;
  line-height: 1.7;
}

.candidate-card .severity-badge {
  position: absolute;
  right: 16px;
  bottom: 16px;
}

.scheduler-alert {
  align-items: flex-start;
  border-radius: 14px;
  display: flex;
  gap: 12px;
  padding: 14px 18px;
}

.scheduler-alert-error {
  border: 1px solid rgba(240, 82, 82, 0.45);
  background: rgba(240, 82, 82, 0.12);
  color: #ff9c9c;
}

.scheduler-alert-warning {
  border: 1px solid rgba(240, 165, 2, 0.45);
  background: rgba(240, 165, 2, 0.1);
  color: #f5c97b;
}

.scheduler-alert-body strong {
  display: block;
  font-size: 11px;
  margin-bottom: 4px;
}

.scheduler-alert-body p {
  color: inherit;
  font-size: 10px;
  line-height: 1.6;
  margin: 0;
  opacity: 0.85;
}

@media (max-width: 1180px) {
  .detail-grid {
    grid-template-columns: 1fr;
  }

  .info-grid,
  .candidate-list {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 720px) {
  .overview-header,
  .progress-footnote,
  .panel-heading,
  .section-title-row,
  .table-tools {
    align-items: flex-start;
    flex-direction: column;
  }

  .trace-io {
    grid-template-columns: 1fr;
  }

  .info-grid,
  .candidate-list,
  .stats-grid {
    grid-template-columns: 1fr;
  }

  th,
  td {
    padding: 14px 16px;
  }
}

/* ---------- 原型改版新增：目标链接 / 旅程 chips ---------- */

.target-link-line {
  align-items: center;
  display: flex;
  gap: 10px;
  margin-top: 4px;
}

.target-url {
  color: rgba(148, 197, 255, 0.92);
  font-size: 11px;
  word-break: break-all;
}

.copy-button {
  align-items: center;
  background: rgba(59, 130, 246, 0.12);
  border: 1px solid rgba(59, 130, 246, 0.35);
  border-radius: 8px;
  color: var(--workstation-primary);
  cursor: pointer;
  display: inline-flex;
  flex-shrink: 0;
  font-size: 10px;
  gap: 4px;
  padding: 3px 10px;
}

.copy-button:hover {
  background: rgba(59, 130, 246, 0.22);
}

.web-browser-profile {
  align-items: center;
  color: var(--workstation-text-muted);
  display: flex;
  flex-wrap: wrap;
  font-size: 10px;
  gap: 8px;
  margin-top: 8px;
}

.web-browser-profile code,
.web-browser-profile b,
.web-browser-profile em {
  background: rgba(37, 99, 235, 0.12);
  border: 1px solid rgba(59, 130, 246, 0.25);
  border-radius: 7px;
  color: var(--workstation-primary);
  font-style: normal;
  font-weight: 500;
  padding: 3px 7px;
}

.journey-chip-row {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 18px;
}

.journey-chip-label {
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.journey-chip-flow {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.journey-chip {
  align-items: center;
  border: 1px solid var(--workstation-border);
  border-radius: 10px;
  color: var(--workstation-text);
  display: inline-flex;
  font-size: 10px;
  gap: 6px;
  padding: 5px 12px;
}

.journey-chip em {
  background: #2563eb;
  border-radius: 6px;
  color: #fff;
  font-size: 10px;
  font-style: normal;
  padding: 1px 6px;
}

.journey-chip.chip-done {
  border-color: rgba(52, 211, 153, 0.5);
  color: var(--workstation-success);
}

.journey-chip.chip-current {
  background: rgba(37, 99, 235, 0.16);
  border: 1px dashed rgba(96, 165, 250, 0.85);
  color: var(--workstation-primary);
}

.journey-chip.chip-failed {
  border-color: rgba(248, 113, 113, 0.55);
  color: var(--workstation-danger);
}

.journey-chip.chip-skipped,
.journey-chip.chip-pending {
  color: var(--workstation-text-muted);
}

.journey-chip-arrow {
  color: rgba(100, 116, 139, 0.8);
}

/* ---------- 六态徽标 ---------- */

.rs-badge {
  border: 1px solid transparent;
  border-radius: 999px;
  display: inline-flex;
  font-size: 10px;
  line-height: 1;
  padding: 5px 10px;
  white-space: nowrap;
}

.rs-badge.rs-passed {
  background: rgba(52, 211, 153, 0.12);
  border-color: rgba(52, 211, 153, 0.4);
  color: var(--workstation-success);
}

.rs-badge.rs-failed {
  background: rgba(248, 113, 113, 0.12);
  border-color: rgba(248, 113, 113, 0.45);
  color: var(--workstation-danger);
}

.rs-badge.rs-not-detected {
  background: rgba(251, 191, 36, 0.1);
  border-color: rgba(251, 191, 36, 0.45);
  color: var(--workstation-warning);
}

.rs-badge.rs-detect-error {
  background: rgba(251, 113, 133, 0.12);
  border-color: rgba(251, 113, 133, 0.5);
  color: var(--workstation-danger);
}

.rs-badge.rs-manual-review {
  background: rgba(167, 139, 250, 0.12);
  border-color: rgba(167, 139, 250, 0.5);
  color: var(--workstation-violet);
}

.rs-badge.rs-running {
  background: rgba(96, 165, 250, 0.12);
  border-color: rgba(96, 165, 250, 0.5);
  color: var(--workstation-primary);
}

.rs-badge.rs-pending {
  background: rgba(148, 163, 184, 0.1);
  border-color: var(--workstation-border);
  color: var(--workstation-text-muted);
}

/* ---------- 页面识别时间线 ---------- */

.frontier-issues-title,
.frontier-issues article > div {
  display: flex;
  align-items: center;
}

.timeline-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 22px 24px;
}

/* 地址是 App 下载 / 引导页时的说明 */
.guide-page-hint {
  padding: 12px 16px;
  border: 1px solid var(--workstation-border);
  border-left: 3px solid var(--workstation-warning);
  border-radius: var(--workstation-radius-sm);
  color: var(--workstation-text);
  background: var(--tint-warning);
  font-size: 13px;
  line-height: 1.7;
}

.guide-page-hint strong {
  display: block;
  margin-bottom: 2px;
  color: var(--workstation-warning);
  font-size: 14px;
}

.guide-page-hint p {
  margin: 0;
}

.timeline-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  position: relative;
}

.timeline-list::before {
  background: rgba(59, 130, 246, 0.25);
  bottom: 12px;
  content: '';
  left: 31px;
  position: absolute;
  top: 12px;
  width: 2px;
}

.timeline-node {
  display: flex;
  gap: 14px;
}

.timeline-rail {
  align-items: center;
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  gap: 6px;
  position: relative;
  width: 64px;
}

.timeline-dot {
  background: rgba(100, 116, 139, 0.9);
  border: 3px solid var(--workstation-border);
  border-radius: 50%;
  height: 16px;
  width: 16px;
  z-index: 1;
}

.timeline-node.node-done .timeline-dot {
  background: #34d399;
}

.timeline-node.node-failed .timeline-dot {
  background: #fb7185;
}

.timeline-node.node-running .timeline-dot {
  background: #3b82f6;
  box-shadow: 0 0 0 5px rgba(59, 130, 246, 0.25);
}

.timeline-time {
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.timeline-card {
  background: var(--workstation-panel-soft);
  border: 1px solid var(--workstation-border);
  border-radius: 14px;
  display: flex;
  flex: 1;
  gap: 14px;
  min-width: 0;
  padding: 14px 16px;
}

.timeline-node.is-current .timeline-card {
  border-color: rgba(96, 165, 250, 0.75);
  box-shadow: 0 0 24px rgba(59, 130, 246, 0.12);
}

.timeline-node.node-pending .timeline-card,
.timeline-node.node-skipped .timeline-card {
  border-style: dashed;
  opacity: 0.6;
}

.timeline-node.node-failed .timeline-card {
  border-color: rgba(251, 113, 133, 0.45);
}

.timeline-thumb {
  border-radius: 10px;
  flex-shrink: 0;
  height: 96px;
  overflow: hidden;
  width: 72px;
}

.timeline-thumb img {
  height: 100%;
  object-fit: cover;
  object-position: top;
  width: 100%;
}

.thumb-placeholder {
  align-items: center;
  background: rgba(51, 65, 85, 0.4);
  color: var(--workstation-text-muted);
  display: flex;
  height: 100%;
  justify-content: center;
  width: 100%;
}

.timeline-body {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: 7px;
  min-width: 0;
}

.timeline-title-row {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.timeline-title-row strong {
  font-size: 12px;
}

.discovered-tag {
  border: 1px solid rgba(148, 163, 184, 0.4);
  border-radius: 6px;
  color: var(--workstation-text-muted);
  font-size: 10px;
  padding: 1px 6px;
}

.timeline-subline {
  color: var(--workstation-text-muted);
  font-size: 10px;
  margin: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.url-text {
  color: rgba(125, 168, 224, 0.75);
}

.timeline-metrics {
  color: var(--workstation-text);
  display: flex;
  flex-wrap: wrap;
  font-size: 10px;
  gap: 14px;
  margin: 0;
}

.timeline-metrics.muted {
  color: var(--workstation-text-muted);
}

.page-failure-line {
  margin: 8px 0 0;
  padding: 8px 10px;
  border-radius: 8px;
  color: var(--workstation-danger);
  font-size: 11px;
  background: rgba(244, 63, 94, 0.08);
}

.page-failure-line b {
  font-family: 'JetBrains Mono', Consolas, monospace;
}

.inspection-work-panel {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 22px 24px;
}

.work-summary-chips,
.inspection-work-badges,
.inspection-work-meta,
.inspection-rule-chips,
.inspection-attempt {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.work-summary-chips span {
  padding: 5px 10px;
  border: 1px solid var(--workstation-border);
  border-radius: 999px;
  color: var(--workstation-text-muted);
  font-size: 11px;
  background: var(--workstation-panel-soft);
}

.inspection-work-list {
  display: grid;
  gap: 12px;
}

.inspection-work-card {
  padding: 16px;
  border: 1px solid var(--workstation-border);
  border-radius: 14px;
  background: var(--workstation-panel-soft);
}

.inspection-work-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}

.inspection-work-head strong {
  color: var(--workstation-text-strong);
  font-size: 13px;
}

.inspection-work-head p,
.inspection-rule-line {
  margin: 6px 0 0;
  color: var(--workstation-text-faint);
  font-size: 10px;
}

.inspection-work-meta {
  margin-top: 12px;
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.inspection-work-meta span {
  padding: 4px 8px;
  border-radius: 8px;
  background: var(--workstation-panel-soft);
}

.inspection-rule-chips {
  margin-top: 10px;
}

.rule-member-chip,
.inspection-attempt {
  padding: 7px 9px;
  border: 1px solid var(--workstation-border);
  border-radius: 9px;
  color: var(--workstation-text-muted);
  font-size: 10px;
  background: var(--workstation-panel-soft);
}

.rule-member-chip b {
  color: var(--workstation-text);
}

.inspection-attempt-list {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.attempt-error {
  color: var(--workstation-danger);
}

.frontier-issues {
  margin: 14px 20px 20px;
  padding-top: 14px;
  border-top: 1px solid var(--workstation-border);
}

.frontier-issues-title {
  justify-content: space-between;
  margin-bottom: 10px;
  color: var(--workstation-text);
  font-size: 12px;
}

.frontier-issues-title span {
  color: var(--workstation-text-faint);
}

.frontier-issues article {
  padding: 10px 12px;
  border: 1px solid var(--workstation-border);
  border-radius: 9px;
  background: var(--workstation-panel-soft);
}

.frontier-issues article + article {
  margin-top: 8px;
}

.frontier-issues article > div {
  gap: 8px;
}

.frontier-issues article strong {
  overflow: hidden;
  color: var(--workstation-text);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.frontier-issues article p {
  margin: 6px 0 0;
  color: var(--workstation-text-faint);
  font-size: 11px;
}

.frontier-status {
  padding: 2px 7px;
  border-radius: 999px;
  flex: 0 0 auto;
  font-size: 10px;
  font-weight: 800;
}

.frontier-failed {
  color: var(--workstation-danger);
  background: rgba(244, 63, 94, 0.12);
}

.frontier-blocked,
.frontier-skipped {
  color: var(--workstation-warning);
  background: rgba(245, 158, 11, 0.12);
}

.anomaly-chip {
  background: rgba(251, 191, 36, 0.12);
  border: 1px solid rgba(251, 191, 36, 0.4);
  border-radius: 6px;
  color: var(--workstation-warning);
  font-size: 10px;
  padding: 1px 7px;
}

.node-attention-panel {
  background: var(--workstation-panel-soft);
  border: 1px solid rgba(251, 191, 36, 0.2);
  border-radius: 10px;
  margin-top: 8px;
  padding: 9px 11px;
}

.node-attention-reason {
  color: var(--workstation-text);
  font-size: 11px;
  line-height: 1.65;
  margin: 0;
}

.node-attention-reason + .node-attention-reason {
  margin-top: 3px;
}

.node-attention-reason b {
  color: var(--workstation-warning);
}

.node-attention-details {
  margin-top: 7px;
}

.node-attention-details > summary {
  color: var(--workstation-primary);
  cursor: pointer;
  font-size: 11px;
  user-select: none;
}

.node-attention-list {
  display: grid;
  gap: 8px;
  margin-top: 9px;
}

.node-attention-item {
  background: var(--workstation-panel-soft);
  border: 1px solid var(--workstation-border);
  border-radius: 8px;
  padding: 9px 10px;
}

.node-attention-head {
  align-items: center;
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.node-attention-head strong {
  color: var(--workstation-text);
  font-size: 11px;
}

.node-attention-item p {
  color: var(--workstation-text-muted);
  font-size: 10px;
  line-height: 1.55;
  margin: 5px 0 0;
}

.node-attention-item p b {
  color: var(--workstation-text);
}

.node-attention-item code {
  color: var(--workstation-warning);
  display: inline-block;
  font-size: 9px;
  margin-top: 6px;
  overflow-wrap: anywhere;
}

.node-progress {
  align-items: center;
  display: flex;
  gap: 10px;
}

.node-progress-track {
  background: rgba(51, 65, 85, 0.6);
  border-radius: 999px;
  flex: 1;
  height: 6px;
  overflow: hidden;
}

.node-progress-fill {
  background: linear-gradient(90deg, #2563eb, #60a5fa);
  border-radius: 999px;
  height: 100%;
  transition: width 0.6s ease;
}

.node-progress > span {
  color: var(--workstation-primary);
  font-size: 10px;
}

.current-rule-line {
  color: var(--workstation-primary);
  font-size: 10px;
  margin: 0;
}

/* ---------- 命中规则统计 / 最近异常 ---------- */

.stats-column {
  display: flex;
  flex-direction: column;
  gap: 24px;
  min-width: 0;
}

.stats-grid.six {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}

.stat-tile.rs-total strong {
  color: var(--workstation-text);
}

.stat-tile.rs-passed strong {
  color: var(--workstation-success);
}

.stat-tile.rs-failed strong {
  color: var(--workstation-danger);
}

.stat-tile.rs-not-detected strong {
  color: var(--workstation-warning);
}

.stat-tile.rs-detect-error strong {
  color: var(--workstation-danger);
}

.stat-tile.rs-manual-review strong {
  color: var(--workstation-violet);
}

.stat-tile.rs-running strong {
  color: var(--workstation-primary);
}

.donut-legend {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.legend-title {
  color: var(--workstation-text);
  font-size: 11px;
  margin: 0 0 2px;
}

.legend-row {
  align-items: center;
  color: var(--workstation-text-muted);
  display: inline-flex;
  font-size: 10px;
  gap: 8px;
}

.legend-row i {
  border-radius: 3px;
  display: inline-block;
  height: 10px;
  width: 10px;
}

.legend-row b {
  margin-left: auto;
}

.anomaly-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 20px 22px;
}

.anomaly-count {
  color: rgba(251, 191, 36, 0.9);
  font-size: 10px;
}

.anomaly-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.anomaly-card {
  border: 1px solid rgba(251, 191, 36, 0.35);
  background: rgba(251, 191, 36, 0.06);
  border-radius: 12px;
  padding: 10px 14px;
}

.anomaly-card.rs-detect-error {
  border-color: rgba(251, 113, 133, 0.45);
  background: rgba(251, 113, 133, 0.08);
}

/* 有严重等级时边框/底色跟等级走（定义在状态色之后，等级优先）；未定级回落状态色 */
.anomaly-card.sev-critical {
  border-color: var(--sev-critical-line);
  background: var(--sev-critical-tint);
}

.anomaly-card.sev-high {
  border-color: var(--sev-high-line);
  background: var(--sev-high-tint);
}

.anomaly-card.sev-medium {
  border-color: var(--sev-medium-line);
  background: var(--sev-medium-tint);
}

.anomaly-card.sev-low {
  border-color: var(--sev-low-line);
  background: var(--sev-low-tint);
}

.anomaly-head {
  align-items: center;
  display: flex;
  gap: 8px;
}

.anomaly-head strong {
  flex: 1;
  font-size: 11px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.anomaly-head time {
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.anomaly-card p {
  color: var(--workstation-text);
  font-size: 10px;
  margin: 6px 0 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ---------- 规则执行结果表增强 ---------- */

.filter-select {
  width: 130px;
}

.filter-select.wide {
  width: 160px;
}

.trigger-cell {
  color: var(--workstation-text);
  font-size: 10px;
  max-width: 220px;
}

.row-dim td {
  opacity: 0.55;
}

.evidence-thumbs {
  align-items: center;
  display: flex;
  gap: 8px;
}

.evidence-thumb {
  background: none;
  border: 1px solid var(--workstation-border);
  border-radius: 8px;
  cursor: pointer;
  height: 46px;
  overflow: hidden;
  padding: 0;
  width: 36px;
}

.evidence-thumb img {
  height: 100%;
  object-fit: cover;
  object-position: top;
  width: 100%;
}

.thumb-more {
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.detail-link {
  background: none;
  border: none;
  color: var(--workstation-primary);
  cursor: pointer;
  font-size: 10px;
  padding: 0;
}

.detail-link:hover {
  text-decoration: underline;
}

.rule-action-cell {
  display: flex;
  gap: 12px;
  white-space: nowrap;
}

.rule-detail-row td {
  background: var(--workstation-panel-soft);
}

.rule-detail-grid {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  padding: 4px 2px;
}

.rule-detail-grid span {
  color: var(--workstation-text-muted);
  display: block;
  font-size: 10px;
  margin-bottom: 4px;
}

.rule-detail-grid p {
  color: var(--workstation-text);
  font-size: 10px;
  line-height: 1.6;
  margin: 0;
  white-space: pre-wrap;
}

.muted {
  color: var(--workstation-text-muted);
}

/* ---------- 截图证据 ---------- */

.screenshot-panel {
  display: flex;
  flex-direction: column;
  gap: 18px;
  padding: 22px 24px;
}

.screenshot-groups {
  display: flex;
  flex-direction: column;
  gap: 18px;
}

.screenshot-group header {
  align-items: center;
  display: flex;
  gap: 10px;
  margin-bottom: 10px;
}

.screenshot-group header strong {
  font-size: 11px;
}

.screenshot-group header span {
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.screenshot-grid {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
}

.screenshot-cell {
  background: var(--workstation-panel-soft);
  border: 1px solid var(--workstation-border);
  border-radius: 12px;
  cursor: pointer;
  height: 168px;
  overflow: hidden;
  padding: 0;
  position: relative;
}

.screenshot-cell:hover {
  border-color: rgba(96, 165, 250, 0.7);
}

.screenshot-cell:focus-visible {
  outline: 2px solid var(--workstation-primary);
  outline-offset: 3px;
}

.screenshot-cell.is-collection {
  border-color: var(--workstation-primary);
  box-shadow: 3px 3px 0 var(--workstation-panel-soft), 4px 4px 0 var(--workstation-border);
}

.collection-link {
  background: var(--workstation-panel-strong);
  border-radius: 6px;
  bottom: 6px;
  color: var(--workstation-primary);
  font-size: 10px;
  padding: 2px 6px;
  position: absolute;
  right: 6px;
}

.collection-badge {
  font-weight: 600;
}

.screenshot-cell img {
  height: 100%;
  object-fit: cover;
  object-position: top;
  width: 100%;
}

.dup-badge {
  background: var(--workstation-panel-strong);
  border-radius: 6px;
  color: var(--workstation-text);
  font-size: 10px;
  padding: 2px 6px;
  position: absolute;
  right: 6px;
  top: 6px;
}

.occ-badge {
  background: rgba(15, 23, 42, 0.72);
  border-radius: 6px;
  bottom: 6px;
  color: var(--workstation-text);
  font-size: 10px;
  left: 6px;
  padding: 2px 6px;
  position: absolute;
}

.rel-badge {
  border-radius: 6px;
  font-size: 10px;
  left: 6px;
  padding: 2px 6px;
  position: absolute;
  top: 6px;
}

.rel-badge.rs-passed {
  background: rgba(6, 78, 59, 0.85);
  color: var(--workstation-success);
}

.rel-badge.rs-failed,
.rel-badge.rs-detect-error {
  background: rgba(127, 29, 29, 0.85);
  color: var(--workstation-danger);
}

.rel-badge.rs-not-detected {
  background: rgba(113, 63, 18, 0.85);
  color: var(--workstation-warning);
}

.rel-badge.rs-running {
  background: rgba(30, 58, 138, 0.85);
  color: var(--workstation-primary);
}

.rel-badge.rs-pending {
  background: rgba(51, 65, 85, 0.85);
  color: var(--workstation-text);
}

/* ---------- 执行日志 / 折叠面板 ---------- */

.logs-panel {
  display: flex;
  flex-direction: column;
  padding: 6px 0 0;
}

.collapse-head {
  align-items: center;
  background: none;
  border: none;
  color: inherit;
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  padding: 18px 24px;
  text-align: left;
  width: 100%;
}

.collapse-side {
  align-items: center;
  display: flex;
  gap: 12px;
}

.log-count {
  color: var(--workstation-text-muted);
  font-size: 10px;
}

.collapse-toggle {
  color: var(--workstation-primary);
  font-size: 10px;
}

.logs-body {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 0 24px 20px;
}

.logs-toolbar {
  display: flex;
  justify-content: flex-end;
}

.log-lines {
  background: var(--workstation-panel-soft);
  border: 1px solid var(--workstation-border);
  border-radius: 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  max-height: 420px;
  overflow: auto;
  padding: 14px 16px;
}

.log-line {
  color: var(--workstation-text);
  display: flex;
  font-size: 10px;
  gap: 10px;
  line-height: 1.7;
  margin: 0;
}

.log-line time {
  color: rgba(100, 116, 139, 0.9);
  flex-shrink: 0;
}

.log-line b {
  flex-shrink: 0;
  width: 44px;
}

.log-line.log-info b {
  color: var(--workstation-primary);
}

.log-line.log-warn b {
  color: var(--workstation-warning);
}

.log-line.log-exec b {
  color: #22d3ee;
}

.log-line.log-error {
  background: rgba(127, 29, 29, 0.25);
  border-radius: 6px;
}

.log-line.log-error b,
.log-line.log-error span {
  color: var(--workstation-danger);
}

.more-logs {
  align-self: center;
}

.empty-block.slim {
  padding: 18px;
}

</style>
