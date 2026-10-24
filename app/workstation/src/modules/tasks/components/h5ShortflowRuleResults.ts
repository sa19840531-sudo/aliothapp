import type {
  H5ShortflowAttempt, H5ShortflowEvidenceRef, H5ShortflowPlan, H5ShortflowRuleResult,
} from '@/services/h5-shortflows.api'
import { RULE_EXECUTION_STATUS_LABELS } from '@/utils/status'
import type { ScreenshotViewerAction, ScreenshotViewerEntry } from './screenshotCollection'

const measurementLabels: Record<string, string> = {
  player_visible: '播放器可见', media_ready: '媒体就绪', paused: '暂停状态',
  loaded_frame: '画面加载完成', stable_frame: '画面持续稳定',
  media_time_stopped: '暂停后播放进度停止', media_time_advanced: '播放进度推进',
  fullscreen: '全屏状态', observation_complete: '观测完整',
  payment_display_visible: '支付信息可见', payment_plan_selected: '套餐已选择',
  price_visible: '价格可见', terms_visible: '条款可见', channel_visible: '支付渠道可见',
  order_created: '订单创建状态', returned_to_source: '返回原页面',
}

const missingLabels: Record<string, string> = {
  post_action_sample_scope_mismatch: '操作后的观测与当前业务对象不一致',
  latest_manifest_not_pass: '所需材料质量尚未通过检查',
  entry_or_instance_unverified: '未能确认前置状态或同一业务对象',
  readonly_contract_missing: '缺少允许执行该操作的只读合同',
  contradictory_expected_and_exit: '检查预期与结束条件存在冲突',
  checkpoint_or_dependency_missing: '缺少检查点或前置步骤的观测',
  dependency_instance_changed_without_restoration: '业务对象已变化，缺少恢复记录',
}

export function checkpointCheckLabel(name: string) {
  return measurementLabels[name] || '合同检查项'
}

export function checkpointMissingLabel(code: string) {
  if (missingLabels[code]) return missingLabels[code]
  if (code.startsWith('measurement:')) return `缺少${measurementLabels[code.slice(12)] || '所需状态'}的测量`
  if (code.startsWith('multi_sample_contract:')) return '缺少足量连续观测材料'
  if (code.startsWith('evidence_role:')) return '缺少所需角色的原始材料'
  return '所需证据未完整提供'
}

export function shortflowRuleStatus(result: H5ShortflowRuleResult) {
  if (result.linkage_status !== 'linked') return '关联待核对'
  const label = RULE_EXECUTION_STATUS_LABELS[result.execution_status || ''] || '结论待核对'
  return awaitingShortflowReview(result) ? `机器${label} · 待人工复核` : label
}

export function shortflowRuleStatusClass(result: H5ShortflowRuleResult) {
  if (result.linkage_status !== 'linked') return 'is-unlinked'
  return awaitingShortflowReview(result) ? 'is-manual_review' : `is-${result.execution_status}`
}

function awaitingShortflowReview(result: H5ShortflowRuleResult) {
  return result.review_status === 'pending' || result.review_status === 'needs_recheck'
}

export function checkpointElementId(planId: string, checkpointId: string) {
  return `shortflow-checkpoint-${encodeURIComponent(planId)}-${encodeURIComponent(checkpointId)}`
}

export function referencedScreenshots(plan: H5ShortflowPlan, reference: H5ShortflowEvidenceRef) {
  const checkpoint = plan.checkpoints.find((item) => item.checkpoint_id === reference.checkpoint_id)
  const attempt = checkpoint?.attempts.find((item) => item.attempt_id === reference.attempt_id
    && item.session_id === reference.session_id)
  const binding = attempt?.bindings.find((item) => item.role === reference.role
    && item.capture_id === reference.capture_id && item.latest_manifest_version === reference.manifest_version
    && (item.manifest_version !== reference.manifest_version || item.manifest_id === reference.manifest_id)
    && item.integrity === 'current')
  if (!binding) return []
  // 动作绑定保留初始版本；规则输入可引用质量检查后的最新版本，不能用初始版本否定正式引用。
  // 仅展示后端精确引用且仍在本次绑定中的截图，不从其他尝试或流程补图。
  return binding.artifacts.filter((item) => item.artifact_type === 'screenshot'
    && reference.artifact_ids.includes(item.artifact_id))
}

export function checkpointScreenshotEntries(checkpointId: string, attempt: H5ShortflowAttempt): ScreenshotViewerEntry[] {
  const bindings = attempt.bindings.filter((item) => item.integrity === 'current')
  const actions: ScreenshotViewerAction[] = []
  for (const id of new Set(bindings.map((item) => item.action_execution_id))) {
    const before = bindings.filter((item) => item.action_execution_id === id && item.role === 'before_action')
    const after = bindings.filter((item) => item.action_execution_id === id && item.role === 'after_action')
    // 前后配对只来自同一正式动作的唯一角色绑定，不按截图顺序或数量猜测。
    if (!id || before.length !== 1 || after.length !== 1) continue
    actions.push({ id, action_label: '流程操作', purpose: `检查点 ${checkpointId}`,
      status_label: '原件已关联', source_capture_id: before[0]!.capture_id, target_capture_id: after[0]!.capture_id })
  }
  return bindings.flatMap((binding) => binding.artifacts.filter((item) => item.artifact_type === 'screenshot')
    .map((artifact) => ({ artifact_id: artifact.artifact_id, page_name: `${checkpointId} · ${attempt.attempt_id}`,
      captured_at: binding.captured_at, capture_role: binding.role, duplicate_count: 1,
      related_rules: [], page_capture_id: binding.capture_id,
      actions: actions.filter((action) => action.id === binding.action_execution_id) })))
}
