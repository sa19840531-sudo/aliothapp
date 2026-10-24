import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ExplorationProcessPanel from '@/modules/tasks/components/ExplorationProcessPanel.vue'
import type {
  EvidenceGaps,
  PageTypeJudgements,
  RuleReuse,
  PageGraph,
  RecoveryRoutes,
  TaskScreenshots,
} from '@/types/task-insight'

const HOME = 'url:https://demo.example/'
const LIST = 'url:https://demo.example/movie'

it('保留真实旧接口JSON字符串中的弹窗失败原因，不逐字符枚举或崩溃', () => {
  const value = graph()
  value.transitions[1]!.reason_codes = '["target_state_missing", "error:POPUP_DISMISSAL_NOT_FOUND"]' as unknown as string[]
  const wrapper = mount(ExplorationProcessPanel, {
    props: { graph: value, routes: null, gaps: null, screenshots: null },
  })
  expect(wrapper.text()).toContain('找不到弹窗关闭方式')
})

function state(overrides: Partial<PageGraph['states'][number]>) {
  return {
    state_id: 'state',
    logical_page_key: HOME,
    identity_status: 'confirmed',
    page_type: 'home',
    page_state: 'stable',
    is_overlay: false,
    is_external: false,
    counts_as_business_page: true,
    state_payload: { url: 'https://demo.example/' },
    ...overrides,
  }
}

function graph(): PageGraph {
  return {
    protocol_version: 'page_graph.v1',
    status: 'available',
    task_id: 'task-1',
    agent_run_id: 'run-1',
    finalization_epoch: 0,
    states: [
      state({ state_id: 'home-1' }),
      state({
        state_id: 'list-1',
        logical_page_key: LIST,
        page_type: 'video_list',
        state_payload: { url: 'https://demo.example/movie' },
      }),
      state({
        state_id: 'popup-1',
        logical_page_key: 'overlay:popup-1',
        identity_status: 'unknown',
        page_type: null,
        page_state: 'popup',
        is_overlay: true,
        counts_as_business_page: false,
      }),
      state({
        state_id: 'ext-1',
        logical_page_key: 'external:ext-1',
        identity_status: 'unknown',
        page_type: null,
        page_state: 'external',
        is_external: true,
        counts_as_business_page: false,
        state_payload: { url: 'https://ads.example/landing' },
      }),
    ],
    transitions: [
      {
        transition_id: 't-1',
        source_state_id: 'home-1',
        target_state_id: 'list-1',
        action_kind: 'click',
        semantic_target: '底栏 视频',
        risk_level: 'low',
        status: 'completed',
        proof: 'trusted',
        counts_as_business_arrival: true,
        replay_eligible: true,
        reason_codes: [],
      },
      {
        transition_id: 't-2',
        source_state_id: 'popup-1',
        target_state_id: 'home-1',
        action_kind: 'dismiss',
        semantic_target: '关闭弹窗',
        risk_level: 'low',
        status: 'blocked',
        proof: 'insufficient',
        counts_as_business_arrival: false,
        replay_eligible: false,
        reason_codes: ['transition_proof_missing', 'error:POPUP_DISMISSAL_NOT_OBSERVED'],
      },
      {
        transition_id: 't-3',
        source_state_id: 'list-1',
        target_state_id: 'ext-1',
        action_kind: 'click',
        semantic_target: '广告卡片',
        risk_level: 'low',
        status: 'completed',
        proof: 'insufficient',
        counts_as_business_arrival: false,
        replay_eligible: false,
        reason_codes: ['external_navigation'],
      },
    ],
    exploration_tree: {
      protocol_version: 'page_exploration_tree.v1',
      roots: [HOME],
      nodes: [
        {
          page_key: HOME,
          parent_page_key: null,
          children: [LIST],
          states: [
            {
              state_fingerprint: 'fp-home',
              capture_ids: ['home-1'],
              latest_capture_id: 'home-1',
              status: 'needs_evidence',
              reason_codes: ['unresolved_rule_evidence'],
            },
          ],
          overlays: [{ capture_id: 'popup-1', page_state: 'popup' }],
          targets: [
            { semantic_target: '底栏 视频', status: 'executed', progress_status: 'executed' },
            { semantic_target: '底栏 我的', status: 'pending', progress_status: 'unvisited' },
          ],
          transition_ids: ['t-1'],
          status: 'needs_evidence',
        },
        {
          page_key: LIST,
          parent_page_key: HOME,
          children: [],
          states: [
            {
              state_fingerprint: 'fp-list',
              capture_ids: ['list-1'],
              latest_capture_id: 'list-1',
              status: 'completed',
              reason_codes: [],
            },
          ],
          overlays: [],
          targets: [],
          transition_ids: [],
          status: 'completed',
        },
      ],
      cross_transition_ids: [],
      unattached_overlays: [],
      unknown_observation_ids: ['obs-9'],
      unassigned_targets: [],
    },
  }
}

function routes(): RecoveryRoutes {
  return {
    protocol_version: 'page_graph_recovery.v1',
    status: 'available',
    routes: [
      {
        source_state_id: 'home-1',
        target_state_id: 'list-1',
        target_page_key: LIST,
        steps: [graph().transitions[0]],
      },
    ],
    requires_stepwise_reverification: true,
    route_policy: '同运行、同会话的可信低风险历史路线；执行时必须逐步重新观察和验证',
  }
}

function gaps(): EvidenceGaps {
  return {
    protocol_version: 'evidence-gap-list.v1',
    task_id: 'task-1',
    task_status: 'review',
    run_id: 'run-1',
    total: 2,
    phase_counts: { after_close: 1, not_applicable: 1 },
    policy_counts: { fresh: 1, manual: 1 },
    disposition_counts: { recorded_only: 2 },
    execution_policy: 'recorded_only',
    items: [
      {
        execution_id: 'exec-1',
        rule_id: 'rule-1',
        rule_code: 'alioth_ops_8',
        rule_name: '封面加载完整性检查',
        category: 'visual',
        severity: 'high',
        execution_status: 'evidence_insufficient',
        page_capture_id: 'home-1',
        logical_page_key: HOME,
        state_fingerprint: 'fp-home',
        created_at: '2026-09-08T05:00:00Z',
        gap: {
          gap_id: 'gap-1',
          rule_code: 'alioth_ops_8',
          rule_name: '封面加载完整性检查',
          category: 'visual',
          target: {
            region: { kind: 'page', label: '页面可见内容', layer_target: 'base_page' },
            page_url: 'https://demo.example/',
            page_type: 'home',
            page_title: '首页',
            surface_kind: 'web',
            page_capture_id: 'home-1',
            observed_page_state: 'popup',
            required_page_states: [],
          },
          missing: {
            page_states: [],
            presentation: { code: 'BASE_PAGE_OCCLUDED', reason: '底层页面被遮挡' },
            capture_roles: [],
            artifact_types: [],
            judge_statements: [],
            quality_gate_sources: [],
          },
          disposition: 'recorded_only',
          reason_code: 'EVIDENCE_INSUFFICIENT',
          evidence_goal: '关闭遮挡后的首页证据',
          overlay_phase: 'after_close',
          recapture_policy: 'fresh',
          recapture_fallback: 'equivalent',
        },
      },
      {
        execution_id: 'exec-2',
        rule_id: 'rule-2',
        rule_code: 'alioth_ops_5',
        rule_name: '充值拉起支付检查',
        category: 'payment',
        severity: 'critical',
        execution_status: 'evidence_insufficient',
        page_capture_id: 'list-1',
        logical_page_key: LIST,
        state_fingerprint: 'fp-list',
        created_at: '2026-09-08T05:01:00Z',
        gap: {
          gap_id: 'gap-2',
          rule_code: 'alioth_ops_5',
          rule_name: '充值拉起支付检查',
          category: 'payment',
          target: {
            region: { kind: 'region', label: '支付与价格区域', layer_target: 'base_page' },
            page_url: 'https://demo.example/movie',
            page_type: 'video_list',
            page_title: '视频',
            surface_kind: 'web',
            page_capture_id: 'list-1',
            observed_page_state: 'stable',
            required_page_states: [],
          },
          missing: {
            page_states: [],
            presentation: null,
            capture_roles: ['before', 'primary'],
            artifact_types: ['action_trace'],
            judge_statements: ['缺少确认支付的动作记录'],
            quality_gate_sources: [],
          },
          disposition: 'recorded_only',
          reason_code: 'EVIDENCE_INSUFFICIENT',
          evidence_goal: '确认支付后的页面证据',
          overlay_phase: 'not_applicable',
          recapture_policy: 'manual',
        },
      },
    ],
  }
}

function screenshots(): TaskScreenshots {
  return {
    task_id: 'task-1',
    run_id: 'run-1',
    groups: [
      {
        page_group_key: 'g-home',
        page_title: '首页',
        page_display_name: '首页-home',
        page_type: 'home',
        page_type_name: '首页',
        total: 3,
        items: [
          {
            artifact_id: 'a-1',
            page_capture_id: 'home-1',
            captured_at: null,
            content_type: 'image/png',
            size_bytes: 1,
            duplicate_count: 1,
            related_rules: [],
            presentation: { topmost_layer_kind: 'page', base_page_visibility: 'visible', mutation_mode: 'natural' },
            settle: { outcome: 'settled', blocking_reasons: [], dynamic_reasons: [] },
          },
          {
            artifact_id: 'a-2',
            page_capture_id: 'popup-1',
            captured_at: null,
            content_type: 'image/png',
            size_bytes: 1,
            duplicate_count: 1,
            related_rules: [],
            presentation: {
              topmost_layer_kind: 'overlay',
              base_page_visibility: 'fully_occluded',
              mutation_mode: 'natural',
              overlay_semantic_kind: 'advertisement',
            },
            settle: { outcome: 'unsettled', blocking_reasons: ['network_pending'], dynamic_reasons: [] },
          },
          {
            artifact_id: 'a-3',
            page_capture_id: 'home-2',
            captured_at: null,
            content_type: 'image/png',
            size_bytes: 1,
            duplicate_count: 1,
            related_rules: [],
            presentation: {
              topmost_layer_kind: 'page',
              base_page_visibility: 'visible',
              mutation_mode: 'instrumented_diagnostic',
              diagnostic_operations: [{ kind: 'style_hide_obstruction', source: 'verified_high_z_obstruction_hide' }],
            },
            settle: null,
          },
        ],
      },
    ],
  }
}

function reuse(): RuleReuse {
  return {
    protocol_version: 'rule-reuse-list.v1',
    task_id: 'task-1',
    task_status: 'review',
    run_id: 'run-1',
    total: 1,
    scope_counts: { per_task: 1 },
    status_counts: { pass: 1 },
    presentation: 'reused_only',
    items: [
      {
        execution_id: 'exec-reuse-1',
        rule_id: 'rule-ops-1',
        rule_code: 'alioth_ops_1',
        rule_name: '产品域名可访问检查',
        category: 'interaction',
        severity: 'critical',
        execution_status: 'pass',
        result_summary: '沿用首页上的结论：域名可以正常访问',
        page_capture_id: 'list-1',
        page_type: 'video_list',
        page_url: 'https://demo.example/list',
        scope: 'per_task',
        scope_label: '整个任务一次',
        reuse_key: 'task:rule-ops-1',
        reason: '整个任务只判一次，沿用首个合格结论',
        invalidate_on: [
          { code: 'account_or_login_changed', label: '账号或登录态变化' },
          { code: 'rule_version_changed', label: '规则版本变化' },
        ],
        basis: { rule_contract_hash: 'contract-1', surface_kind: 'web' },
        source: {
          execution_id: 'exec-source',
          page_capture_id: 'home-1',
          page_type: 'home',
          page_url: 'https://demo.example/',
          execution_status: 'pass',
        },
        created_at: '2026-09-09T05:00:00Z',
      },
    ],
  }
}

function judgements(): PageTypeJudgements {
  return {
    protocol_version: 'page-type-judgement-list.v1',
    task_id: 'task-1',
    task_status: 'review',
    run_id: 'run-1',
    total: 2,
    status_counts: { confirmed: 1, conflicting: 1 },
    conflicting_count: 1,
    revised_count: 0,
    differs_count: 1,
    resolution_policy: 'recorded_only',
    items: [
      {
        page_capture_id: 'home-1',
        page_seq: 1,
        page_slug: 'home',
        page_url: 'https://demo.example/',
        logical_page_key: HOME,
        formal_page_type: 'home',
        resolution: {
          status: 'confirmed',
          status_label: '判断一致',
          page_type: 'home',
          reason: '各来源一致为 home',
          current_by_source: { structure_rules: 'home', model: 'home' },
          conflicting_page_types: [],
          revisions: [],
          adopted_judgement_id: 'j-2',
          judgement_count: 2,
          after_seal_count: 0,
        },
        differs_from_formal: false,
        judgements: [],
      },
      {
        page_capture_id: 'list-1',
        page_seq: 2,
        page_slug: 'list',
        page_url: 'https://demo.example/list',
        logical_page_key: 'web:demo.example/list',
        formal_page_type: 'video_list',
        resolution: {
          status: 'conflicting',
          status_label: '判断冲突',
          page_type: null,
          reason: '各来源判断不一致（结构规则判为 video_list，模型判断判为 content_list），按未知处理，不凭置信度硬定论',
          current_by_source: { structure_rules: 'video_list', model: 'content_list' },
          conflicting_page_types: ['content_list', 'video_list'],
          revisions: [],
          adopted_judgement_id: null,
          judgement_count: 2,
          after_seal_count: 0,
        },
        differs_from_formal: true,
        judgements: [
          {
            judgement_id: 'j-3',
            sequence_no: 1,
            source: 'structure_rules',
            source_label: '结构规则',
            page_type: 'video_list',
            page_state: 'stable',
            confidence: 0.9,
            kind: 'judgement',
            revises_judgement_id: null,
            basis: {},
            arrived_after_seal: false,
            created_at: '2026-09-09T05:00:00Z',
          },
          {
            judgement_id: 'j-4',
            sequence_no: 2,
            source: 'model',
            source_label: '模型判断',
            page_type: 'content_list',
            page_state: 'stable',
            confidence: 0.6,
            kind: 'judgement',
            revises_judgement_id: null,
            basis: {},
            arrived_after_seal: false,
            created_at: '2026-09-09T05:00:01Z',
          },
        ],
      },
    ],
  }
}

describe('ExplorationProcessPanel', () => {
  it('把页面图、缺口台账和截图质量投影成四块只读视图', () => {
    const wrapper = mount(ExplorationProcessPanel, {
      props: {
        graph: graph(),
        routes: routes(),
        gaps: gaps(),
        screenshots: screenshots(),
        reuse: reuse(),
        judgements: judgements(),
      },
    })
    const text = wrapper.text()

    // 头部摘要
    expect(text).toContain('页面 2')
    expect(text).toContain('弹窗 1')
    expect(text).toContain('外跳 1')
    expect(text).toContain('可信跳转 1/3')
    expect(text).toContain('补采缺口 2')
    expect(text).toContain('可用截图 1/3')

    // 页面路线：树按到达证明排列，子页面缩进；状态与原因用大白话
    const routeRows = wrapper.findAll('.route-row')
    expect(routeRows).toHaveLength(2)
    expect(routeRows[0].text()).toContain('首页')
    expect(routeRows[0].text()).toContain('缺证')
    expect(routeRows[0].text()).toContain('仍有规则证据未齐')
    expect(routeRows[0].text()).toContain('已执行 1')
    expect(routeRows[0].text()).toContain('待探索 1')
    expect(routeRows[1].attributes('style')).toContain('padding-left: 18px')
    expect(routeRows[1].text()).toContain('视频列表页')
    expect(routeRows[1].text()).toContain('/movie')
    expect(routeRows[1].text()).toContain('完成')
    expect(text).toContain('未确认身份的观察 1')

    // 阻断原因分布
    const failures = wrapper.find('[data-testid="process-failures"]')
    expect(failures.text()).toContain('阻断或失败的跳转 1 次')
    expect(failures.text()).toContain('弹窗关闭后仍可见')

    // 弹窗与外跳
    const overlays = wrapper.find('[data-testid="process-overlays"]')
    expect(overlays.text()).toContain('弹窗')
    expect(overlays.text()).toContain('demo.example/')
    expect(overlays.text()).toContain('关闭弹窗 被阻断：弹窗关闭后仍可见')
    expect(overlays.text()).toContain('https://ads.example/landing')
    expect(overlays.text()).toContain('/movie')
    expect(overlays.text()).toContain('未记录回源')

    // 截图质量
    const quality = wrapper.find('[data-testid="process-quality"]')
    expect(quality.text()).toContain('可用 1')
    expect(quality.text()).toContain('底页被遮挡 1')
    expect(quality.text()).toContain('诊断材料 1')
    expect(quality.text()).toContain('未稳定 1')
    expect(quality.text()).toContain('诊断操作：style_hide_obstruction')
    expect(quality.text()).toContain('遮挡物：advertisement')

    // 补采缺口与路线
    const gapsBlock = wrapper.find('[data-testid="process-gaps"]')
    expect(gapsBlock.text()).toContain('已记录，未执行')
    expect(gapsBlock.text()).toContain('关闭后 1')
    expect(gapsBlock.text()).toContain('不涉及弹窗 1')
    expect(gapsBlock.text()).toContain('重新观察 1')
    expect(gapsBlock.text()).toContain('人工 1')
    const gapRows = gapsBlock.findAll('tbody tr')
    expect(gapRows).toHaveLength(2)
    expect(gapRows[0].text()).toContain('alioth_ops_8 封面加载完整性检查')
    expect(gapRows[0].text()).toContain('首页（弹窗）')
    expect(gapRows[0].text()).toContain('底层页面被遮挡')
    expect(gapRows[0].text()).toContain('重新观察，备选等价状态')
    expect(gapRows[0].text()).toContain('暂无可信路线')
    expect(gapRows[1].text()).toContain('视频列表页（稳定）')
    expect(gapRows[1].text()).toContain('采集角色：before、primary')
    expect(gapRows[1].text()).toContain('缺少确认支付的动作记录')
    expect(gapRows[1].text()).toContain('可信路线 1 条，最短 1 步')

    // 规则复用占位
    // 页型判断与改判：只列冲突、改判或与正式识别不一致的页面
    expect(text).toContain('页型冲突 1')
    const judgementBlock = wrapper.find('[data-testid="process-judgements"]')
    expect(judgementBlock.text()).toContain('有判断记录的页面 2')
    expect(judgementBlock.text()).toContain('判断一致 1')
    expect(judgementBlock.text()).toContain('判断冲突 1')
    expect(judgementBlock.text()).toContain('与正式识别不一致 1')
    const judgementRows = judgementBlock.findAll('tbody tr')
    expect(judgementRows).toHaveLength(1)
    const judgementCells = judgementRows[0].findAll('td').map((cell) => cell.text())
    expect(judgementCells[0]).toBe('第 2 页 list')
    expect(judgementCells[1]).toBe('视频列表页')
    expect(judgementCells[2]).toContain('结构规则：视频列表页')
    expect(judgementCells[2]).toContain('模型判断：')
    expect(judgementCells[3]).toContain('未知（')
    expect(judgementCells[4]).toBe('—')
    expect(judgementCells[5]).toContain('不凭置信度硬定论')

    // 规则复用：沿用来的结论单独列，带来源页、复用范围、原因和失效条件
    expect(text).toContain('结论复用 1')
    const reuseBlock = wrapper.find('[data-testid="process-reuse"]')
    expect(reuseBlock.text()).toContain('整个任务一次 1')
    const reuseRows = reuseBlock.findAll('tbody tr')
    expect(reuseRows).toHaveLength(1)
    const reuseCells = reuseRows[0].findAll('td').map((cell) => cell.text())
    expect(reuseCells[0]).toBe('alioth_ops_1 产品域名可访问检查')
    expect(reuseCells[1]).toBe('视频列表页')
    expect(reuseCells[2]).toBe('首页')
    expect(reuseCells[3]).toBe('通过')
    expect(reuseCells[4]).toBe('整个任务一次')
    expect(reuseCells[5]).toContain('沿用首个合格结论')
    expect(reuseCells[6]).toBe('账号或登录态变化、规则版本变化')
  })

  it('页面图不可用时明说原因，不把空列表当成没有探索', () => {
    const wrapper = mount(ExplorationProcessPanel, {
      props: {
        graph: {
          protocol_version: 'page_graph.v1',
          status: 'unavailable',
          reason_code: 'graph_projection_not_complete',
          task_id: 'task-1',
          states: [],
          transitions: [],
        },
        routes: null,
        gaps: null,
        screenshots: null,
        reuse: null,
        judgements: null,
      },
    })
    const text = wrapper.text()
    expect(text).toContain('页面状态图还在投影中')
    expect(text).toContain('证据缺口台账尚未加载')
    expect(text).toContain('规则复用记录尚未加载')
    expect(text).toContain('页型判断记录尚未加载')
    expect(text).toContain('截图证据尚未加载')
    expect(text).not.toContain('页面 0')
  })

  it('可以收起展开', async () => {
    const wrapper = mount(ExplorationProcessPanel, {
      props: { graph: graph(), routes: null, gaps: gaps(), screenshots: null, defaultExpanded: false },
    })
    expect(wrapper.find('[data-testid="process-routes"]').exists()).toBe(false)
    await wrapper.find('button.process-head').trigger('click')
    expect(wrapper.find('[data-testid="process-routes"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="process-gaps"]').text()).toContain('路线未加载')
  })
})

describe('ExplorationProcessPanel 规则复用空态', () => {
  it('台账已加载但没有复用时明说规则都按页面各自执行', () => {
    const wrapper = mount(ExplorationProcessPanel, {
      props: {
        graph: graph(),
        routes: null,
        gaps: null,
        screenshots: null,
        reuse: { ...reuse(), total: 0, scope_counts: {}, status_counts: {}, items: [] },
      },
    })
    const block = wrapper.find('[data-testid="process-reuse"]')
    expect(block.text()).toContain('复用 0')
    expect(block.text()).toContain('本次任务没有沿用别处的结论')
    expect(block.findAll('tbody tr')).toHaveLength(0)
  })
})

describe('ExplorationProcessPanel 补采选路结论', () => {
  it('服务端已经替缺口选过路时，用服务端的原因和建议，不让页面自己再猜', () => {
    const base = gaps()
    const withPlan: EvidenceGaps = {
      ...base,
      route_plan_counts: { 没规划出路线: 1, 未提补采请求: 1 },
      items: [
        {
          ...base.items[0],
          route_plan: {
            state: '没规划出路线',
            request_status: 'unfulfillable',
            reason_code: 'RECOVERY_ROUTE_NOT_REDRIVABLE',
            reason_label: '通到目标页面的路线里有步骤没法重新走一遍',
            advice: '这些步骤没有语义落点，重跑只能照历史坐标点，平台不允许。',
            blocked_action_kinds: ['navigate'],
          },
        },
        base.items[1],
      ],
    }
    const wrapper = mount(ExplorationProcessPanel, {
      props: {
        graph: graph(),
        routes: routes(),
        gaps: withPlan,
        screenshots: null,
      },
    })
    const block = wrapper.find('[data-testid="process-gaps"]')
    const rows = block.findAll('tbody tr')

    expect(block.text()).toContain('没规划出路线 1')
    expect(rows[0].text()).toContain('通到目标页面的路线里有步骤没法重新走一遍')
    expect(rows[0].text()).toContain('挡路动作：navigate')
    expect(rows[0].text()).toContain('重跑只能照历史坐标点')
    // 没有 route_plan 的那条仍走页面原来的兜底文案
    expect(rows[1].text()).toContain('可信路线 1 条，最短 1 步')
  })
})
