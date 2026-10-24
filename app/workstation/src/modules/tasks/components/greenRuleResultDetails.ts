import type { GreenRuleResultProjection, GreenRuleSubitemResult } from '@/types/task-insight'
import { REVIEW_STATUS_LABELS, RULE_EXECUTION_STATUS_LABELS } from '@/utils/status'

const ruleTitles: Record<GreenRuleResultProjection['rule_key'], string> = {
  '102': '内容安全与范围分项判定',
  s72: '空数据与加载失败分项判定',
  '41': 'App外链浏览器承接分项判定',
  '62': '广告结束与正片衔接分项判定',
  s35: '交互响应与稳定性分项判定',
  s49: '异常原因与恢复路径分项判定',
  s73: '加载、空态与成功反馈分项判定',
  '32': '导航布局分项判定',
}

const subitemLabels: Record<string, string> = {
  minor_safety: '涉未风险',
  political_safety: '涉政风险',
  terrorism_safety: '涉恐风险',
  content_scope: '内容检查范围与实际覆盖',
  empty_state_meaning: '真实空数据呈现',
  load_failure_meaning: '加载失败呈现与引导',
  external_browser_handoff: '外链由系统或第三方浏览器承接',
  natural_end_handoff: '广告自然结束后正片衔接',
  manual_close_handoff: '手动关闭广告后正片衔接',
  content_preload: '正片预加载',
  presented_first_frame: '正片实际呈现首帧',
  failure_reason: '真实异常原因说明',
  retry_entry: '重试或刷新入口可用',
  business_recovery: '重试后业务恢复',
  video_line_recovery: '视频异常切线恢复',
  account_payment_support: '账号与支付异常客服路径',
  payment_not_interrupted: '支付关键过程不被客服打断',
  loading_feedback: '加载过程反馈',
  empty_state_guidance: '真实空态插图或有效引导',
  business_success_feedback: '本次业务成功反馈',
  equal_width_navigation: '同级导航等宽分布',
  fit_navigation_spacing: '导航文字完整与间距',
  overflow_scroll: '导航超出时横向滚动',
  initial_next_label: '初始下一项文字露出',
  correct_response: '点击响应正确',
  usable_layout: '布局不影响使用',
  comparable_state_stability: '相同前置条件下操作稳定',
}

const missingLabels: Record<string, string> = {
  minor_age_context_original: '缺少可核验的年龄与内容上下文原件',
  controlled_minor_policy: '缺少受控的涉未政策依据',
  minor_independent_evaluation: '涉未分项判读尚未完成',
  political_context_original: '缺少涉政判读所需的上下文原件',
  controlled_political_policy: '缺少受控的涉政政策依据',
  political_independent_evaluation: '涉政分项判读尚未完成',
  terrorism_context_original: '缺少涉恐判读所需的上下文原件',
  controlled_terrorism_policy: '缺少受控的涉恐政策依据',
  terrorism_independent_evaluation: '涉恐分项判读尚未完成',
  home_and_list_scope_original: '缺少首页及内容列表检查范围的原始依据',
  actual_screen_coverage: '缺少实际屏幕覆盖范围的材料',
  advertisement_exclusion_source: '缺少广告排除的可信依据',
  same_module_request_identity: '尚未唯一关联同一模块的本次请求',
  actual_empty_response: '缺少可信的真实空数据结果',
  module_rendering: '缺少与本次请求对应的模块呈现',
  failure_rendering_and_recovery: '缺少加载失败呈现与刷新或重试路径的材料',
  formal_external_link_action: '缺少正式外链操作',
  known_outbound_target: '缺少已知外链目标的依据',
  foreground_browser_component: '缺少前台浏览器应用与组件身份',
  current_browser_url_strong_source: '缺少当前浏览器地址的可信来源',
  builtin_webview_strong_source: '缺少是否由内置网页承接的可信身份来源',
  ad_content_media_identity: '尚未关联同一广告与正片实例',
  natural_ad_end_event: '缺少广告自然结束的真实事件',
  full_handoff_window: '缺少广告到正片衔接的完整过程',
  formal_ad_close_action: '缺少正式关闭广告操作',
  content_preload_original: '缺少正片预加载原件',
  content_presented_event: '缺少正片实际呈现事件',
  matching_first_frame_png: '缺少与呈现事件对应的首帧原图',
  handoff_clock: '缺少广告与正片衔接的同源时间关联',
  actual_module_fault: '缺少当前模块真实异常的依据',
  failure_reason_rendering: '缺少异常原因呈现的材料',
  retry_refresh_usable_target: '缺少重试或刷新入口可用性的材料',
  same_module_retry_request: '尚未关联同一模块的本次重试请求',
  business_recovered_response: '缺少业务实际恢复的可信结果',
  trusted_video_business_identity: '缺少可信的视频业务类型依据',
  actual_video_fault: '缺少视频真实异常的依据',
  line_recovery: '缺少视频切线恢复过程',
  trusted_account_payment_identity: '缺少可信的账号或支付业务类型依据',
  actual_account_payment_fault: '缺少账号或支付真实异常的依据',
  trusted_payment_stage: '缺少可信的支付阶段依据',
  complete_support_interruption_window: '缺少客服是否影响支付过程的完整观察',
  same_module_loading_window: '缺少同一模块的完整加载窗口',
  complete_progress_feedback_observation: '缺少加载进度反馈的完整观察',
  same_module_illustration_or_guidance: '缺少同一模块空态插图或有效引导的材料',
  trusted_business_success: '缺少可信的本次业务成功结果',
  native_click_request_clock: '缺少操作与请求的原生时间关联',
  new_feedback_window: '缺少本次新提示或动效的完整窗口',
  explicit_green_page_contract: '缺少明确的绿色页面规则合同',
  trusted_page_originals: '缺少已核验的页面原件',
  original_screenshot: '缺少原始截图',
  same_capture_navigation_dom_and_ui_snapshot: '缺少同一次采集的导航结构与界面原件',
  navigation_original_unreadable: '导航原件暂无法读取',
  complete_browser_navigation_snapshot: '导航界面材料不完整或不适用于当前检查',
  navigation_viewport_and_scale: '缺少导航视口或缩放信息',
  complete_navigation_siblings: '尚未证明已完整列出全部同级分类',
  complete_navigation_siblings_missing: '尚未证明已完整列出全部同级分类',
  navigation_usable_container: '缺少导航可用区域的原始测量',
  sibling_bounds: '缺少同级导航槽位的完整边界',
  single_navigation_row: '尚未证明导航位于同一横向排列中',
  width_measurement_resolution: '当前测量精度不足以确认导航是否等宽',
  text_bounds: '缺少真实文字边界',
  navigation_spacing: '缺少可核验的导航间距',
  full_text_fit_condition: '尚未确认全部分类文字能否完整容纳',
  overflow_condition: '尚未确认导航是否超出可用区域',
  native_horizontal_scroll: '缺少实际横向滚动的证据',
  horizontal_scroll_behavior: '缺少实际横向滚动的证据',
  initial_navigation_state: '缺少导航初始状态的证据',
  initial_scroll_and_scale: '缺少初始滚动位置或缩放信息',
  next_label_text_bounds: '缺少下一项文字的真实边界',
  viewport_clipping: '缺少文字与视口裁切边界的核对',
  navigation_dom_truncated: '导航结构原件已截断，不能确认完整分类',
  navigation_dom_unreadable_or_over_capacity: '导航结构原件无法完整读取',
  navigation_group_capacity_exceeded: '导航材料超出本次检查容量，完整范围尚未验证',
  navigation_dom_snapshot_incomplete: '导航结构与界面原件未能完整对应',
  navigation_slot_identity_unverified: '导航槽位与实际控件的对应关系尚未确认',
  navigation_slot_parent_unverified: '尚未确认导航槽位属于同一组',
  navigation_dom_snapshot_sibling_drift: '结构与界面采集之间的同级分类已变化',
  native_click_event_time: '缺少原生点击事件时间',
  same_module_response_causality: '尚未建立同一模块的操作与响应因果关系',
  complete_interactive_layout: '缺少完整的交互布局材料',
  occlusion_effect_on_use: '尚未确认遮挡是否影响实际使用',
  independent_comparable_preconditions: '缺少独立且可比较的操作前置条件',
  complete_repeated_actions: '缺少完整的多次独立操作材料',
  trusted_h5_task_identity: '当前材料尚未具备对应平台的可信身份适配',
  formal_local_display_action: '缺少可核验的正式展示操作',
  native_local_response_causality: '尚未建立本次展示操作与原生响应的完整因果关系',
  green_input_scope_mismatch: '过程材料与当前检查范围不一致',
  green_capture_capacity_exceeded: '采集材料超出本次检查容量，完整范围尚未验证',
  green_original_capacity_exceeded: '原件超出本次读取容量，完整材料尚未验证',
  green_persisted_input_mismatch: '冻结输入与保存的材料成员不一致',
  green_task_run_epoch_mismatch: '材料与当前任务执行批次不一致',
  green_capture_unverified: '有关采集原件尚未通过身份或完整性核验',
  green_capture_not_in_frozen_input: '存在尚未纳入本次冻结输入的采集材料',
  green_action_window_not_frozen: '操作前后材料未完整纳入本次检查',
  green_action_lineage_mismatch: '操作与前后材料的关联尚未通过核验',
  green_formal_action_window_missing: '缺少正式操作及其完整前后材料',
  native_fact_producer_missing: '对应端尚未提供所需的原生业务事实',
}

const missingFallback = '所需证据尚不完整'

export function greenMissingLabel(code: string) {
  const normalized = code.replace(/^navigation:\d+:/, '')
  const known = missingLabels[normalized] || missingLabels[normalized.split(':')[0] || '']
  return known || missingFallback
}

/** 原因中夹带的内部缺证码也转成文案，不把动态材料身份展示给使用者。 */
export function greenReadableText(value: string | null | undefined, fallback = '') {
  if (!value?.trim()) return fallback
  if (!/[\u3400-\u9fff]/u.test(value)) return fallback || missingFallback
  return value
    .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, '相关材料')
    .replace(/\b[0-9a-f]{32,64}\b/gi, '相关材料')
    .replace(/\bnavigation:\d+:[a-z0-9_.:-]+/gi, greenMissingLabel)
    .replace(/\b(?:navigation:\d+:)?[a-z][a-z0-9]*_[a-z0-9_]+(?::[a-z0-9_.-]+)*/gi, greenMissingLabel)
}

function objectValue(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value)
}

function stringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === 'string')
}

function nullableString(value: unknown) {
  return value === null || typeof value === 'string'
}

function validSubitem(value: unknown) {
  return objectValue(value) && typeof value.key === 'string' && !!value.key.trim()
    && typeof value.status === 'string' && typeof value.evaluated === 'boolean'
    && (value.confidence === null || (typeof value.confidence === 'number'
      && Number.isFinite(value.confidence) && value.confidence >= 0 && value.confidence <= 1))
    && nullableString(value.reason) && stringArray(value.missing_evidence)
    && stringArray(value.evidence_artifact_ids) && stringArray(value.screenshot_artifact_ids)
    && stringArray(value.capture_ids)
    && (value.evidence_notice === undefined || nullableString(value.evidence_notice))
}

/** 宿主与分项组件共享同一关联门，损坏或未来版本投影不能开放旧结论和原图。 */
export function isLinkedGreenResult(result: unknown): result is GreenRuleResultProjection {
  return objectValue(result) && result.schema_version === 'green-rule-result.v1'
    && typeof result.rule_key === 'string' && Object.hasOwn(ruleTitles, result.rule_key)
    && result.linkage_status === 'linked'
    && typeof result.rule_contract_hash === 'string' && !!result.rule_contract_hash.trim()
    && nullableString(result.linkage_notice) && nullableString(result.execution_status)
    && nullableString(result.review_status) && nullableString(result.review_reason)
    && stringArray(result.missing_evidence) && Array.isArray(result.subitems)
    && result.subitems.every(validSubitem)
}

export const greenResultLinked = isLinkedGreenResult

function awaitingReview(result: GreenRuleResultProjection) {
  return result.review_status === 'pending' || result.review_status === 'needs_recheck'
}

export function greenResultTitle(result: GreenRuleResultProjection) {
  return Object.hasOwn(ruleTitles, result.rule_key) ? ruleTitles[result.rule_key] : '绿色规则分项判定'
}

export function greenResultStatus(result: GreenRuleResultProjection) {
  if (!greenResultLinked(result)) return { label: '关联待核对', className: 'is-review' }
  const status = result.execution_status
  const label = status === 'skipped' ? '不适用'
    : RULE_EXECUTION_STATUS_LABELS[status || ''] || '结论待核对'
  if (status === 'fail') {
    return { label: awaitingReview(result) ? `${label} · 待人工复核` : label, className: 'is-fail' }
  }
  if (awaitingReview(result)) {
    return { label: status === 'pass' ? '机器通过 · 待人工复核'
      : status === 'manual_review' ? label : `${label} · 待人工复核`, className: 'is-review' }
  }
  return { label, className: status === 'pass' ? 'is-pass'
    : status === 'skipped' ? 'is-neutral' : 'is-review' }
}

export function greenSubitemLabel(key: string) {
  return subitemLabels[key] || '合同检查分项'
}

export function greenSubitemStatus(item: GreenRuleSubitemResult, result?: GreenRuleResultProjection) {
  if (!item.evaluated) return { label: '尚未判定', className: 'is-review' }
  if (result && awaitingReview(result) && item.status === 'pass') {
    return { label: '机器通过 · 待人工复核', className: 'is-review' }
  }
  const label = item.status === 'skipped' ? '不适用'
    : RULE_EXECUTION_STATUS_LABELS[item.status] || '结论待核对'
  return { label, className: item.status === 'pass' ? 'is-pass'
    : item.status === 'fail' ? 'is-fail' : item.status === 'skipped' ? 'is-neutral' : 'is-review' }
}

export function greenConfidence(item: GreenRuleSubitemResult) {
  const value = item.confidence
  return item.evaluated && typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
    ? `${Math.round(value * 1000) / 10}%` : null
}

export function greenReviewStatus(result: GreenRuleResultProjection) {
  return REVIEW_STATUS_LABELS[result.review_status || ''] || '状态待核对'
}

export function greenReviewReason(result: GreenRuleResultProjection) {
  if (/\b(?:[a-z][a-z0-9]*_[a-z0-9_]+|navigation:\d+:)/i.test(result.review_reason || '')) {
    return result.missing_evidence.length ? '仍有分项缺少必要材料，等待人工复核' : '复核原因待核对'
  }
  return greenReadableText(result.review_reason, '复核原因待核对')
}

export function greenSubitemScreenshots(
  result: GreenRuleResultProjection,
  item: GreenRuleSubitemResult,
  allowedScreenshotIds: string[] = [],
) {
  if (!greenResultLinked(result) || !result.subitems.includes(item)) return []
  const allowed = new Set(allowedScreenshotIds)
  return [...new Set(item.screenshot_artifact_ids)].filter((id) => !!id && allowed.has(id))
}
