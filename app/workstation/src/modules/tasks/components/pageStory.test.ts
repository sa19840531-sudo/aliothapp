import { describe, expect, it } from 'vitest'
import type { ExplorationActionItem, PageTimelineNode, RuleResultItem } from '@/types/task-insight'
import {
  buildPageStory,
  buildTaskLevelStory,
  groupNotApplicableItems,
  groupReviewItems,
  plainActionFailure,
  tallyRules,
  tallySummaryText,
} from './pageStory'

function node(overrides: Partial<PageTimelineNode> = {}): PageTimelineNode {
  return {
    node_seq: 2,
    source: 'discovered',
    step_name: null,
    page_type: 'video_list',
    page_type_name: '视频列表页',
    page_state: 'stable',
    status: 'recognized',
    status_label: '已识别',
    is_current: false,
    page_capture_id: 'capture-list',
    url: 'https://example.test/videos',
    screenshot_artifact_id: null,
    detection: {
      status: 'recognized',
      confidence: 0.9,
      confidence_source: null,
      model_page_type: 'video_list',
      model_confidence: 0.9,
      duration_ms: 10,
    },
    inspection: {
      lifecycle_status: 'active',
      status: null,
      status_label: null,
      failure_stage: null,
      failure_code: null,
      failure_message: null,
      failure_retryable: null,
      abandon_reason: null,
    },
    parent_page_capture_id: 'capture-home',
    element_count: 10,
    rule_progress: { executed: 0, total: null },
    result_rollup: null,
    result_rollup_label: null,
    result_counts: {},
    attention_items: [],
    current_rule_code: null,
    first_visited_at: null,
    captured_at: null,
    finished_at: null,
    ...overrides,
  }
}

function action(overrides: Partial<ExplorationActionItem>): ExplorationActionItem {
  return {
    exploration_action_id: 'action-1',
    source_page_capture_id: 'capture-home',
    source_page_type: 'home',
    action: 'click',
    origin: 'discovered',
    target: '视频',
    target_keywords: [],
    risk_level: 'low',
    purpose: null,
    expected_next_page_type: 'video_list',
    navigation_depth: 1,
    status: 'explored',
    result_page_capture_id: 'capture-list',
    failure_reason: null,
    failure_code: null,
    failure_message: null,
    failure_retryable: null,
    attempt_count: 1,
    created_at: '2026-09-22T00:00:00Z',
    updated_at: '2026-09-22T00:00:01Z',
    ...overrides,
  }
}

function rule(status: RuleResultItem['display_status'], label: string, problem: string | null = null) {
  return {
    rule_code: `rule-${status}-${problem}`,
    rule_name: `规则 ${label}`,
    display_status: status,
    display_status_label: label,
    problem_desc: problem,
    result_summary: null,
  } as unknown as RuleResultItem
}

describe('pageStory', () => {
  it('明确范围排除时说明未选该页，不冒充没有适用规则或检查失败', () => {
    const current = node({ page_type: 'post_list', page_type_name: '帖子列表页' })
    current.inspection.failure_code = 'PAGE_TYPE_OUTSIDE_INSPECTION_SCOPE'
    current.inspection.failure_message = '该页型未包含在本任务选择的质检范围内，仅保留页面证据。'
    const story = buildPageStory(current, {
      actions: [], nodeNameByCapture: new Map(), rules: [],
      explorationOnly: false, popupLabel: null,
    }).join('')
    expect(story).toContain('该页型未包含在本任务选择的质检范围内')
    expect(story).not.toContain('检查中途出了问题')
    expect(story).not.toContain('没有要检查的规则')
  })

  it.each([null, 0, 3])('已采集页面无执行结果时不声称没有规则：%s', (total) => {
    const story = buildPageStory(node({
      page_type: 'post_list', page_type_name: '帖子列表页', status: 'recognized',
      rule_progress: { executed: 0, total },
    }), {
      actions: [], nodeNameByCapture: new Map(), rules: [],
      explorationOnly: false, popupLabel: null,
    })
    expect(story.join('')).not.toContain('没有要检查的规则')
    expect(story.join('')).toContain('尚无规则执行结果')
  })

  it('点击进入的页面讲清从哪来、认成什么、检查结果', () => {
    const story = buildPageStory(node(), {
      actions: [action({})],
      nodeNameByCapture: new Map([['capture-home', '首页']]),
      rules: [rule('passed', '通过'), rule('failed', '未通过', '封面破图')],
      explorationOnly: false,
      popupLabel: null,
    })

    expect(story).toEqual([
      '在「首页」上点击「视频」，来到这一页。',
      '系统认出这是「视频列表页」，把握 90%。',
      '这一页检查了 2 条规则：通过 1 条、没通过 1 条。',
    ])
  })

  it('弹窗自己消失后重拍的过渡画面，只探索时不提规则', () => {
    const story = buildPageStory(
      node({ page_type: null, page_type_name: null, status: 'visiting', capture_count: 2 }),
      {
        actions: [
          action({
            action: 'observe',
            purpose: '弹窗在动作前已自动退场，等待页面稳定后在当前会话重新采集。',
          }),
        ],
        nodeNameByCapture: new Map(),
        rules: [],
        explorationOnly: true,
        popupLabel: null,
      },
    )

    expect(story).toEqual([
      '上一个弹窗自己消失了，等页面稳定后重新拍了一次。',
      '这一张是过渡画面，没有单独识别页面类型。',
      '这一页前后采了 2 次证据，以最新的主版本为准。',
    ])
  })

  it('探索动作的失败说明翻成人话', () => {
    expect(plainActionFailure('当前 UI Snapshot 未证明可关闭的唯一弹窗')).toBe('没找到能确认的关闭按钮')
    expect(plainActionFailure('UI Snapshot 读取失败。')).toBe('界面快照读取失败')
    expect(plainActionFailure(null)).toBe('没有成功')
  })

  it('同一个原因的规则合成一组，人多的组排前面', () => {
    const groups = groupReviewItems([
      rule('evidence_insufficient', '证据不足', '页面被弹窗挡住'),
      rule('not_executed', '未执行', '没轮到判定'),
      rule('not_executed', '未执行', '没轮到判定'),
    ])

    expect(groups.map((group) => [group.reason, group.items.length])).toEqual([
      ['未执行：没轮到判定', 2],
      ['证据不足：页面被弹窗挡住', 1],
    ])
  })

  it('不适用的规则按原因合组，优先用具体原因，没写原因的也说一句', () => {
    const groups = groupNotApplicableItems([
      rule('not_applicable', '确认不适用', '平台只用一种浏览器采集，不做多浏览器采样，这条规则不适用'),
      rule('not_applicable', '确认不适用', '平台只用一种浏览器采集，不做多浏览器采样，这条规则不适用'),
      { ...rule('not_applicable', '确认不适用'), result_summary: '该规则需要登录后才能检查，本次为未登录（游客）浏览，不适用。' },
      rule('not_detected', '未触发'),
    ])

    expect(groups.map((group) => [group.reason, group.items.length])).toEqual([
      ['平台只用一种浏览器采集，不做多浏览器采样，这条规则不适用', 2],
      ['该规则需要登录后才能检查，本次为未登录（游客）浏览，不适用', 1],
      ['机器判定不适用，但没有留下具体原因', 1],
    ])
  })

  it('汇总只列有数的状态；整体检查说明这些规则不对应某一页', () => {
    const tally = tallyRules([rule('passed', '通过'), rule('not_applicable', '确认不适用'), rule('manual_review', '待人工复核')])
    expect(tallySummaryText(tally)).toBe('通过 1 条、未完成判定 1 条、不适用 1 条')
    expect(buildTaskLevelStory([])[1]).toBe('这次没有这类规则。')
  })
})
