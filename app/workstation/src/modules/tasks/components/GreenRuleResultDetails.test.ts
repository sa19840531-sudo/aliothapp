import { mount } from '@vue/test-utils'
import { expect, it } from 'vitest'
import type { GreenRuleResultProjection } from '@/types/task-insight'
import GreenRuleResultDetails from './GreenRuleResultDetails.vue'
import { greenConfidence, greenMissingLabel, greenReadableText, isLinkedGreenResult } from './greenRuleResultDetails'

function result(): GreenRuleResultProjection {
  return {
    schema_version: 'green-rule-result.v1', rule_key: '32', rule_contract_hash: 'frozen-contract',
    linkage_status: 'linked', linkage_notice: null, execution_status: 'evidence_insufficient',
    review_status: null, review_reason: null,
    missing_evidence: ['navigation:1:initial_navigation_state'],
    subitems: [
      { key: 'equal_width_navigation', status: 'pass', evaluated: true, confidence: 0.96,
        reason: '同级导航在已核验容器中等宽分布', missing_evidence: [],
        evidence_artifact_ids: ['actual-ui', 'actual-dom'], screenshot_artifact_ids: ['exact-png'],
        capture_ids: ['capture-current'] },
      { key: 'initial_next_label', status: 'evidence_insufficient', evaluated: false, confidence: null,
        reason: '初始状态尚缺原件', missing_evidence: ['navigation:1:initial_navigation_state'],
        evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [] },
    ],
  }
}

it('逐项保留部分通过和缺证，主状态完全使用后端结果', () => {
  const wrapper = mount(GreenRuleResultDetails, { props: { result: result() } })
  expect(wrapper.get('[data-testid="green-rule-status"]').text()).toBe('证据不足')
  const rows = wrapper.findAll('[data-testid="green-subitem"]')
  expect(rows[0]!.text()).toContain('通过')
  expect(rows[0]!.text()).toContain('置信度 96%')
  expect(rows[1]!.text()).toContain('尚未判定')
  expect(rows[1]!.text()).not.toContain('不符合')
  expect(rows[1]!.text()).not.toContain('置信度')
  expect(wrapper.text()).toContain('缺少导航初始状态的证据')
  expect(wrapper.text()).not.toContain('navigation:1')
})

it('明确失败保持失败色并附待复核，同时保留其他分项缺证', () => {
  const payload = result()
  payload.execution_status = 'fail'
  payload.review_status = 'pending'
  payload.review_reason = '缺少必要材料：navigation:1:initial_navigation_state'
  payload.subitems[0]!.status = 'fail'
  payload.subitems[0]!.reason = '同级导航宽度不同'
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload } })
  const badge = wrapper.get('[data-testid="green-rule-status"]')
  expect(badge.text()).toBe('未通过 · 待人工复核')
  expect(badge.classes()).toContain('is-fail')
  expect(wrapper.text()).toContain('尚未判定')
  expect(wrapper.get('[data-testid="green-review-status"]').text()).toContain('仍有分项缺少必要材料')
  expect(wrapper.text()).not.toContain('initial_navigation_state')
})

it.each(['pending', 'needs_recheck'])('机器通过但%s时主结果和分项都使用待复核样式', (review) => {
  const payload = result()
  payload.execution_status = 'pass'
  payload.review_status = review
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload } })
  const badge = wrapper.get('[data-testid="green-rule-status"]')
  expect(badge.text()).toBe('机器通过 · 待人工复核')
  expect(badge.classes()).not.toContain('is-pass')
  const subitem = wrapper.findAll('[data-testid="green-subitem-status"]')[0]!
  expect(subitem.text()).toBe('机器通过 · 待人工复核')
  expect(subitem.classes()).not.toContain('is-pass')
})

it('未判分项即使携带失败状态也不展示成不符合，可靠跳过展示不适用', () => {
  const payload = result()
  payload.subitems[0]!.status = 'skipped'
  payload.subitems[0]!.reason = '完整分类数量不适用该条件分支'
  payload.subitems[1]!.status = 'fail'
  payload.subitems[1]!.confidence = 0
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload } })
  const statuses = wrapper.findAll('[data-testid="green-subitem-status"]')
  expect(statuses[0]!.text()).toBe('不适用')
  expect(statuses[1]!.text()).toBe('尚未判定')
  expect(statuses[1]!.classes()).not.toContain('is-fail')
  expect(wrapper.findAll('[data-testid="green-subitem"]')[1]!.text()).not.toContain('置信度')
})

it('查看原图只使用后端正式截图引用与当前截图索引的交集', async () => {
  const payload = result()
  payload.subitems[0]!.screenshot_artifact_ids = ['exact-png', 'exact-png', 'other-png']
  const wrapper = mount(GreenRuleResultDetails, {
    props: { result: payload, allowedScreenshotIds: ['exact-png', 'actual-ui', 'nearby-png'] },
  })
  expect(wrapper.findAll('[data-testid="green-view-evidence"]')).toHaveLength(1)
  await wrapper.get('[data-testid="green-view-evidence"]').trigger('click')
  expect(wrapper.emitted('view-evidence')).toEqual([['exact-png']])
})

it.each([undefined, []])('未提供当前截图白名单时不开图，也不拿实际UI/DOM判据或同页近图补图', (allowed) => {
  const payload = result()
  payload.subitems[0]!.evidence_notice = '正式关联原图当前不可用，请核对材料归档'
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload, allowedScreenshotIds: allowed } })
  expect(wrapper.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
  expect(wrapper.text()).toContain('正式关联原图当前不可用')
  expect(wrapper.get('[data-testid="green-rule-status"]').text()).toBe('证据不足')
})

it('只有UI或页面结构判据时，不从当前任务其他原图补出按钮', () => {
  const payload = result()
  payload.subitems[0]!.screenshot_artifact_ids = []
  const wrapper = mount(GreenRuleResultDetails, {
    props: { result: payload, allowedScreenshotIds: ['actual-ui', 'actual-dom', 'nearby-png'] },
  })
  expect(wrapper.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
  expect(wrapper.text()).toContain('当前分项暂无可查看的关联原图')
})

it.each(['evidence_changed', 'missing_contract', 'unknown'])('关联%s时隐藏过期状态、分项和证据', (linkage) => {
  const payload = result()
  payload.linkage_status = linkage
  payload.execution_status = 'pass'
  payload.subitems[0]!.reason = '过期的成功结论'
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload, allowedScreenshotIds: ['exact-png'] } })
  expect(wrapper.get('[data-testid="green-rule-status"]').text()).toBe('关联待核对')
  expect(wrapper.text()).not.toContain('过期的成功结论')
  expect(wrapper.findAll('[data-testid="green-subitem"]')).toHaveLength(0)
  expect(wrapper.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
})

it('缺合同身份或未知投影版本时也不展示已关联结论', () => {
  for (const payload of [
    { ...result(), rule_contract_hash: null },
    { ...result(), schema_version: 'future-version' } as unknown as GreenRuleResultProjection,
    { ...result(), rule_key: 'future-rule' } as unknown as GreenRuleResultProjection,
    { ...result(), rule_key: 'constructor' } as unknown as GreenRuleResultProjection,
  ]) {
    const wrapper = mount(GreenRuleResultDetails, { props: { result: payload } })
    expect(wrapper.get('[data-testid="green-rule-status"]').text()).toBe('关联待核对')
    expect(wrapper.findAll('[data-testid="green-subitem"]')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('function Object')
  }
})

it('结果或截图索引切换后不保留上次按钮', async () => {
  const wrapper = mount(GreenRuleResultDetails, {
    props: { result: result(), allowedScreenshotIds: ['exact-png'] },
  })
  await wrapper.setProps({ allowedScreenshotIds: ['another-task-png'] })
  expect(wrapper.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
  await wrapper.setProps({ result: { ...result(), linkage_status: 'evidence_changed' } })
  expect(wrapper.findAll('[data-testid="green-subitem"]')).toHaveLength(0)
  expect(wrapper.emitted('view-evidence')).toBeUndefined()
})

it('英文缺证码与动态身份不暴露，未知码不编造具体业务义务', () => {
  expect(greenMissingLabel('navigation:7:next_label_text_bounds')).toBe('缺少下一项文字的真实边界')
  expect(greenMissingLabel('native_local_response_causality:action-secret-id')).toContain('完整因果关系')
  expect(greenMissingLabel('unknown_future_requirement:private-id')).toBe('所需证据尚不完整')
  expect(greenReadableText('缺少：navigation:7:next_label_text_bounds')).not.toContain('navigation:7')
  expect(greenReadableText('缺少：navigation:7:future')).not.toContain('navigation:7')
  const payload = result()
  payload.subitems[1]!.missing_evidence = ['unknown_future_requirement:private-id']
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload } })
  expect(wrapper.text()).not.toContain('private-id')
  expect(wrapper.text()).not.toContain('unknown_future_requirement')
  expect(wrapper.text()).toContain('所需证据尚不完整')
})

it('S35使用后端分项，未知分项不露内部字段名', () => {
  const payload = result()
  payload.rule_key = 's35'
  payload.subitems[0]!.key = 'correct_response'
  payload.subitems[1]!.key = 'private_future_check'
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload } })
  expect(wrapper.text()).toContain('点击响应正确')
  expect(wrapper.text()).toContain('合同检查分项')
  expect(wrapper.text()).not.toContain('private_future_check')
})

it('无投影时不产生额外入口，非法置信度不冒充零分', () => {
  expect(mount(GreenRuleResultDetails).find('[data-testid="green-rule-details"]').exists()).toBe(false)
  const item = result().subitems[0]!
  for (const confidence of [null, -1, 2, Number.NaN]) expect(greenConfidence({ ...item, confidence })).toBeNull()
})

it('共享关联门拒绝损坏分项，父层与组件都不能凭linked打开原图', () => {
  const payload = result()
  expect(isLinkedGreenResult(payload)).toBe(true)
  const invalid = { ...payload, subitems: [{ ...payload.subitems[0], screenshot_artifact_ids: null }] }
  expect(isLinkedGreenResult(invalid)).toBe(false)
  const wrapper = mount(GreenRuleResultDetails, {
    props: { result: invalid as unknown as GreenRuleResultProjection, allowedScreenshotIds: ['exact-png'] },
  })
  expect(wrapper.get('[data-testid="green-rule-status"]').text()).toBe('关联待核对')
  expect(wrapper.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
})

type AdditionalRule = {
  key: Exclude<GreenRuleResultProjection['rule_key'], '32' | 's35'>
  title: string
  subitems: Array<[string, string, string[]]>
}

const additionalRules: AdditionalRule[] = [
  { key: '102', title: '内容安全与范围分项判定', subitems: [
    ['minor_safety', '涉未风险', ['minor_age_context_original', 'controlled_minor_policy', 'minor_independent_evaluation']],
    ['political_safety', '涉政风险', ['political_context_original', 'controlled_political_policy', 'political_independent_evaluation']],
    ['terrorism_safety', '涉恐风险', ['terrorism_context_original', 'controlled_terrorism_policy', 'terrorism_independent_evaluation']],
    ['content_scope', '内容检查范围与实际覆盖', ['home_and_list_scope_original', 'actual_screen_coverage', 'advertisement_exclusion_source']],
  ] },
  { key: 's72', title: '空数据与加载失败分项判定', subitems: [
    ['empty_state_meaning', '真实空数据呈现', ['same_module_request_identity', 'actual_empty_response', 'module_rendering']],
    ['load_failure_meaning', '加载失败呈现与引导', ['same_module_request_identity', 'failure_rendering_and_recovery']],
  ] },
  { key: '41', title: 'App外链浏览器承接分项判定', subitems: [
    ['external_browser_handoff', '外链由系统或第三方浏览器承接', ['formal_external_link_action', 'known_outbound_target',
      'foreground_browser_component', 'current_browser_url_strong_source', 'builtin_webview_strong_source']],
  ] },
  { key: '62', title: '广告结束与正片衔接分项判定', subitems: [
    ['natural_end_handoff', '广告自然结束后正片衔接', ['ad_content_media_identity', 'natural_ad_end_event', 'full_handoff_window']],
    ['manual_close_handoff', '手动关闭广告后正片衔接', ['ad_content_media_identity', 'formal_ad_close_action', 'full_handoff_window']],
    ['content_preload', '正片预加载', ['ad_content_media_identity', 'content_preload_original']],
    ['presented_first_frame', '正片实际呈现首帧', ['content_presented_event', 'matching_first_frame_png', 'handoff_clock']],
  ] },
  { key: 's49', title: '异常原因与恢复路径分项判定', subitems: [
    ['failure_reason', '真实异常原因说明', ['actual_module_fault', 'failure_reason_rendering']],
    ['retry_entry', '重试或刷新入口可用', ['actual_module_fault', 'retry_refresh_usable_target']],
    ['business_recovery', '重试后业务恢复', ['same_module_retry_request', 'business_recovered_response']],
    ['video_line_recovery', '视频异常切线恢复', ['trusted_video_business_identity', 'actual_video_fault', 'line_recovery']],
    ['account_payment_support', '账号与支付异常客服路径', ['trusted_account_payment_identity', 'actual_account_payment_fault']],
    ['payment_not_interrupted', '支付关键过程不被客服打断', ['trusted_payment_stage', 'complete_support_interruption_window']],
  ] },
  { key: 's73', title: '加载、空态与成功反馈分项判定', subitems: [
    ['loading_feedback', '加载过程反馈', ['same_module_loading_window', 'complete_progress_feedback_observation']],
    ['empty_state_guidance', '真实空态插图或有效引导', ['actual_empty_response', 'same_module_illustration_or_guidance']],
    ['business_success_feedback', '本次业务成功反馈', ['trusted_business_success', 'native_click_request_clock', 'new_feedback_window']],
  ] },
]

it.each(additionalRules)('$key正式缺证结果保留各分项与实际缺项，不造通过或原图', (definition) => {
  const payload: GreenRuleResultProjection = {
    ...result(), rule_key: definition.key, execution_status: 'evidence_insufficient', review_status: 'pending',
    missing_evidence: [...new Set(definition.subitems.flatMap(([, , missing]) => missing))],
    subitems: definition.subitems.map(([key, , missing]) => ({ key, status: 'evidence_insufficient', evaluated: false,
      confidence: null, reason: '该分项尚缺必要原件或已接入的独立判读，未形成业务结论',
      missing_evidence: missing, evidence_artifact_ids: [], screenshot_artifact_ids: [], capture_ids: [] })),
  }
  const wrapper = mount(GreenRuleResultDetails, { props: { result: payload, allowedScreenshotIds: ['nearby-png'] } })
  expect(wrapper.get('header strong').text()).toBe(definition.title)
  expect(wrapper.get('[data-testid="green-rule-status"]').text()).toBe('证据不足 · 待人工复核')
  const subitems = wrapper.findAll('[data-testid="green-subitem"]')
  expect(subitems).toHaveLength(definition.subitems.length)
  subitems.forEach((item, index) => {
    expect(item.get('header strong').text()).toBe(definition.subitems[index]![1])
    expect(item.get('[data-testid="green-subitem-status"]').text()).toBe('尚未判定')
  })
  expect(wrapper.text()).toContain('仍需补齐的材料与判读')
  expect(wrapper.text()).toContain('材料、条件或判读能力尚不完整')
  expect(wrapper.text()).not.toContain('置信度')
  expect(wrapper.text()).not.toContain('所需证据尚不完整')
  expect(wrapper.findAll('[data-testid="green-view-evidence"]')).toHaveLength(0)
  for (const missing of payload.missing_evidence) expect(wrapper.text()).not.toContain(missing)
  if (definition.key === '102') {
    for (const risk of ['涉未', '涉政', '涉恐']) {
      expect(wrapper.text()).toContain(`缺少受控的${risk}政策依据`)
      expect(wrapper.text()).toContain(`${risk}分项判读尚未完成`)
    }
  }
})
