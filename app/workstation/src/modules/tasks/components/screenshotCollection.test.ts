import { describe, expect, it } from 'vitest'
import type {
  ExplorationActionItem,
  PageGraph,
  PageGraphTransition,
  PageTimeline,
  RuleResultItem,
  ScreenshotItem,
} from '@/types/task-insight'
import { buildScreenshotActionGroups, buildScreenshotCollection } from './screenshotCollection'

const group = { page_display_name: '帖子列表页' }

function screenshot(overrides: Partial<ScreenshotItem> = {}): ScreenshotItem {
  return {
    artifact_id: 'image-after',
    page_capture_id: 'capture-after',
    captured_at: '2026-10-09T00:00:02Z',
    content_type: 'image/png',
    size_bytes: 100,
    duplicate_count: 1,
    related_rules: [],
    capture_role: 'after_action',
    related_frames: [{
      artifact_id: 'image-before',
      page_capture_id: 'capture-before',
      captured_at: '2026-10-09T00:00:01Z',
      capture_role: 'before_action',
      duplicate_count: 1,
    }],
    ...overrides,
  }
}

function transition(overrides: Partial<PageGraphTransition> = {}): PageGraphTransition {
  return {
    transition_id: 'transition-close',
    action_execution_id: 'action-close',
    source_state_id: 'capture-before',
    target_state_id: 'capture-after',
    action_kind: 'click',
    semantic_target: '关闭广告按钮',
    risk_level: 'low',
    status: 'completed',
    proof: 'trusted',
    counts_as_business_arrival: false,
    replay_eligible: false,
    reason_codes: [],
    ...overrides,
  }
}

function graph(transitions: PageGraphTransition[]): PageGraph {
  return {
    protocol_version: 'page_graph.v1',
    status: 'available',
    task_id: 'task-1',
    agent_run_id: 'run-1',
    states: [],
    transitions,
  }
}

function explorationAction(overrides: Partial<ExplorationActionItem> = {}): ExplorationActionItem {
  return {
    exploration_action_id: 'action-close',
    source_page_capture_id: 'capture-before',
    source_page_type: 'post_list',
    action: 'click',
    origin: 'discovered',
    target: '关闭广告按钮',
    target_keywords: [],
    risk_level: 'low',
    purpose: '验证点击关闭按钮后广告是否消失',
    expected_next_page_type: null,
    navigation_depth: 1,
    status: 'explored',
    result_page_capture_id: 'capture-after',
    failure_reason: null,
    failure_code: null,
    failure_message: null,
    failure_retryable: null,
    attempt_count: 1,
    created_at: '2026-10-09T00:00:01Z',
    updated_at: '2026-10-09T00:00:02Z',
    ...overrides,
  }
}

function timeline(actions: ExplorationActionItem[]): PageTimeline {
  return { nodes: [], exploration_actions: actions } as unknown as PageTimeline
}

function rule(code: string, artifactIds: string[]): RuleResultItem {
  return {
    rule_code: code,
    rule_name: `验证规则 ${code}`,
    display_status: 'passed',
    display_status_label: '通过',
    evidence_artifact_ids: artifactIds,
  } as RuleResultItem
}

describe('截图合集的动作与规则事实', () => {
  it('按明确采集编号配对，图序和角色标签不会改变真实动作前后', () => {
    const entries = buildScreenshotCollection(group, screenshot({
      capture_role: 'before_action',
      captured_at: '2026-10-09T00:00:00Z',
      related_frames: [{
        artifact_id: 'image-before',
        page_capture_id: 'capture-before',
        captured_at: '2026-10-09T00:00:03Z',
        capture_role: 'after_action',
        duplicate_count: 1,
      }],
    }), {
      pageGraph: graph([transition()]),
      timeline: timeline([explorationAction()]),
    })

    expect(entries.map((entry) => entry.artifact_id)).toEqual(['image-after', 'image-before'])
    const result = buildScreenshotActionGroups(entries)
    expect(result.groups).toHaveLength(1)
    expect(result.groups[0]?.before.map((entry) => entry.artifact_id)).toEqual(['image-before'])
    expect(result.groups[0]?.after.map((entry) => entry.artifact_id)).toEqual(['image-after'])
    expect(result.groups[0]?.action).toMatchObject({
      id: 'action-close',
      action_label: '点击「关闭广告按钮」',
      purpose: '验证点击关闭按钮后广告是否消失',
      status_label: '已执行',
    })
    expect(result.unassociated).toEqual([])
  })

  it('没有动作关联时保留单帧，不凭角色或相邻时间猜测配对', () => {
    const entries = buildScreenshotCollection(group, screenshot())
    const result = buildScreenshotActionGroups(entries)
    expect(result.groups).toEqual([])
    expect(result.unassociated.map((entry) => entry.artifact_id)).toEqual(['image-before', 'image-after'])
  })

  it('逐图恢复明确制品引用的规则，代表图规则不会扩散到其它帧', () => {
    const entries = buildScreenshotCollection(group, screenshot({
      related_rules: [{
        rule_code: 'only-after', rule_name: '只引用操作后图的规则',
        display_status: 'failed', display_status_label: '未通过',
      }],
    }), { rules: [rule('only-before', ['image-before']), rule('outside', ['outside-image'])] })
    expect(entries.find((entry) => entry.artifact_id === 'image-before')?.related_rules)
      .toEqual([{ rule_code: 'only-before', rule_name: '验证规则 only-before', display_status: 'passed', display_status_label: '通过' }])
    expect(entries.find((entry) => entry.artifact_id === 'image-after')?.related_rules.map((item) => item.rule_code))
      .toEqual(['only-after'])
  })

  it('缺失前帧时保留空位，不从其它合集或时间线补入图片', () => {
    const entries = buildScreenshotCollection(group, screenshot({ related_frames: [] }), {
      pageGraph: graph([transition()]),
    })
    const result = buildScreenshotActionGroups(entries)
    expect(entries.map((entry) => entry.artifact_id)).toEqual(['image-after'])
    expect(result.groups[0]?.before).toEqual([])
    expect(result.groups[0]?.after.map((entry) => entry.artifact_id)).toEqual(['image-after'])
    expect(result.unassociated).toEqual([])
  })

  it('同图同规则的通过和未通过引用分别保留，相同状态只去重', () => {
    const entries = buildScreenshotCollection(group, screenshot({
      related_rules: [{
        rule_code: 'repeated-rule', rule_name: '重复执行的规则',
        display_status: 'passed', display_status_label: '通过',
      }],
    }), {
      rules: [
        rule('repeated-rule', ['image-after']),
        { ...rule('repeated-rule', ['image-after']), display_status: 'failed', display_status_label: '未通过' },
        { ...rule('repeated-rule', ['image-before']), display_status: 'failed', display_status_label: '未通过' },
        rule('repeated-rule', ['image-before']),
      ],
    })
    expect(entries.find((entry) => entry.artifact_id === 'image-after')?.related_rules.map((item) => item.display_status))
      .toEqual(['passed', 'failed'])
    expect(entries.find((entry) => entry.artifact_id === 'image-before')?.related_rules.map((item) => item.display_status))
      .toEqual(['failed', 'passed'])
  })

  it('不同动作独立分组，无关帧与前后编号相同的动作不冒充配对', () => {
    const entries = buildScreenshotCollection(group, screenshot({
      related_frames: [
        { artifact_id: 'image-before', page_capture_id: 'capture-before', captured_at: null, duplicate_count: 1 },
        { artifact_id: 'image-other-before', page_capture_id: 'other-before', captured_at: null, duplicate_count: 1 },
        { artifact_id: 'image-other-after', page_capture_id: 'other-after', captured_at: null, duplicate_count: 1 },
        { artifact_id: 'image-unrelated', page_capture_id: 'unrelated', captured_at: null, duplicate_count: 1 },
      ],
    }), {
      pageGraph: graph([
        transition(),
        transition({ transition_id: 'transition-scroll', action_execution_id: 'action-scroll',
          source_state_id: 'other-before', target_state_id: 'other-after', action_kind: 'scroll', semantic_target: null }),
        transition({ transition_id: 'transition-same', action_execution_id: 'action-same',
          source_state_id: 'unrelated', target_state_id: 'unrelated' }),
      ]),
    })
    const result = buildScreenshotActionGroups(entries)
    expect(result.groups.map((item) => ({
      id: item.action.id, before: item.before.map((entry) => entry.artifact_id), after: item.after.map((entry) => entry.artifact_id),
    }))).toEqual([
      { id: 'action-close', before: ['image-before'], after: ['image-after'] },
      { id: 'action-scroll', before: ['image-other-before'], after: ['image-other-after'] },
    ])
    expect(result.unassociated.map((entry) => entry.artifact_id)).toEqual(['image-unrelated'])
  })

  it('只有前后编号明确的旧时间线动作可回退，缺结果的 App 动作不猜', () => {
    const entries = buildScreenshotCollection(group, screenshot(), {
      timeline: timeline([
        explorationAction(),
        explorationAction({ exploration_action_id: 'action-incomplete', result_page_capture_id: null }),
      ]),
    })
    expect(buildScreenshotActionGroups(entries).groups.map((item) => item.action.id)).toEqual(['action-close'])
  })

  it('页面图优先，时间线只有相同前后编号才补目的，不合并同页其它动作', () => {
    const entries = buildScreenshotCollection(group, screenshot(), {
      pageGraph: graph([transition({ status: 'failed' })]),
      timeline: timeline([explorationAction({ source_page_capture_id: 'different-before' })]),
    })
    expect(buildScreenshotActionGroups(entries).groups[0]?.action).toMatchObject({
      id: 'action-close', purpose: null, status_label: '执行失败', source_capture_id: 'capture-before',
    })
  })

  it('明细尚未加载完整时保留截图，缺少制品引用的规则不附会成证据', () => {
    const entries = buildScreenshotCollection(group, screenshot(), {
      timeline: {} as PageTimeline,
      pageGraph: {} as PageGraph,
      rules: [{ rule_code: 'unloaded-rule' } as RuleResultItem],
    })
    expect(entries).toHaveLength(2)
    expect(entries.every((entry) => entry.related_rules.length === 0)).toBe(true)
    expect(buildScreenshotActionGroups(entries).unassociated).toHaveLength(2)
  })

  it('同一动作身份给出冲突端点时不生成错误配对', () => {
    const entries = buildScreenshotCollection(group, screenshot(), { pageGraph: graph([transition()]) })
    const original = entries[0]?.actions?.[0]
    expect(original).toBeDefined()
    entries[1]!.actions = [{ ...original!, source_capture_id: 'different-before' }]
    const result = buildScreenshotActionGroups(entries)
    expect(result.groups).toEqual([])
    expect(result.unassociated).toHaveLength(2)
  })

  it('合集入口遇到页面图同身份端点冲突时拒绝关联，时间线不能复活动作', () => {
    const entries = buildScreenshotCollection(group, screenshot(), {
      pageGraph: graph([
        transition(),
        transition({ transition_id: 'conflicting-transition', source_state_id: 'different-before' }),
      ]),
      timeline: timeline([explorationAction()]),
    })
    expect(entries.every((entry) => entry.actions?.length === 0)).toBe(true)
    const result = buildScreenshotActionGroups(entries)
    expect(result.groups).toEqual([])
    expect(result.unassociated).toHaveLength(2)
  })

  it('合集入口不把另一动作的目的套给显式图身份，旧图才允许唯一端点回退', () => {
    const context = { timeline: timeline([explorationAction({ exploration_action_id: 'another-action' })]) }
    const explicit = buildScreenshotCollection(group, screenshot(), {
      ...context,
      pageGraph: graph([transition()]),
    })
    expect(buildScreenshotActionGroups(explicit).groups[0]?.action.purpose).toBeNull()
    expect(buildScreenshotActionGroups(explicit).groups).toHaveLength(1)

    const legacy = buildScreenshotCollection(group, screenshot(), {
      ...context,
      pageGraph: graph([transition({ action_execution_id: null })]),
    })
    expect(buildScreenshotActionGroups(legacy).groups[0]?.action.purpose)
      .toBe('验证点击关闭按钮后广告是否消失')
  })
})
