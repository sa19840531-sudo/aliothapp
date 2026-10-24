import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import type { AppStageProjection } from '@/types/task-insight'
import AppStagePanel from './AppStagePanel.vue'

function stage(
  overrides: Partial<NonNullable<AppStageProjection['app_exploration']>> = {},
): AppStageProjection {
  return {
    applicable: true,
    task_id: 'task-1',
    task_status: 'done',
    bootstrap: { stage: 'app_ready', installed_package: 'com.example.app' },
    exploration_work: { succeeded: 3 },
    app_exploration: {
      status: 'exploration_completed',
      surface_phase: 'popup_chain',
      current_page_identity: 'page:home',
      completed_work_count: 3,
      pending_work_count: 0,
      action_count: 2,
      reobservation_count: 1,
      coverage_summary: {
        covered_business_page_count: 2,
        covered_page_type_count: 1,
        covered_business_pages: [
          {
            protocol_version: 'app-business-page-group.v1',
            page_group_key: 'app-page-group:profile',
            page_display_name: '个人中心-profile',
            page_name: '个人中心',
            page_name_source: 'top_bar',
            page_name_confidence: 0.96,
            page_type: 'profile',
            capture_count: 3,
            first_capture_id: 'capture-1',
            last_capture_id: 'capture-3',
          },
          {
            protocol_version: 'app-business-page-group.v1',
            page_group_key: 'app-page-group:invitation',
            page_display_name: '分享邀请-profile',
            page_name: '分享邀请',
            page_name_source: 'page_landmark',
            page_name_confidence: 0.92,
            page_type: 'profile',
            capture_count: 1,
            first_capture_id: 'capture-4',
            last_capture_id: 'capture-4',
          },
        ],
      },
      terminal_reason_code: 'exploration_completed',
      outcome: 'exploration_completed',
      evidence_index_id: 'index-1',
      evidence_index_hash: 'sha256:abc',
      evidence_gap_count: 0,
      surface_session_status: 'closed',
      started_at: '2026-08-11T12:00:00+00:00',
      completed_at: '2026-08-11T12:05:00+00:00',
      ...overrides,
    },
    inspection_summary: { work_item_count: 2, nonterminal_count: 0, failed_count: 0 },
    publication_projection: {
      current_stage: 'judged',
      publication_count: 1,
      status_counts: { published: 1 },
      stage_counts: { sealed: 0, delivered: 0, claimed: 0, judged: 1 },
      sealed_count: 1,
      delivered_count: 1,
      claimed_count: 1,
      judged_count: 1,
      outbox_pending_count: 0,
      outbox_dead_letter_count: 0,
      inbox_pending_count: 0,
      inbox_dead_letter_count: 0,
      needs_review_count: 0,
      failed_count: 0,
      items: [
        {
          publication_id: 'publication-1',
          status: 'published',
          current_stage: 'judged',
          evidence_index_id: 'index-1',
          evidence_index_hash: 'sha256:abc',
          row_version: 2,
          attempt_count: 1,
          max_attempts: 3,
          expected_entry_count: 1,
          terminal_entry_count: 1,
          expected_rule_count: 1,
          linked_work_item_count: 1,
          explicit_exclusion_count: 0,
          outbox_pending_count: 0,
          outbox_dead_letter_count: 0,
          inbox_pending_count: 0,
          inbox_dead_letter_count: 0,
          failure_code: null,
          failure_message: null,
          entries: [
            {
              entry_id: 1,
              sequence_no: 1,
              capture_id: 'capture-1',
              evidence_manifest_id: 'manifest-1',
              semantic_role: 'final_observation',
              page_type: 'home',
              page_state: 'stable',
              status: 'linked',
              current_stage: 'judged',
              candidate_rule_count: 1,
              linked_work_item_count: 1,
              explicit_exclusion_count: 0,
              claimed_work_count: 1,
              terminal_work_count: 1,
              failure_code: null,
              failure_message: null,
            },
          ],
        },
      ],
    },
    gate: { gate_passed: true, block_reasons: [] },
  }
}

describe('App 探索闭环阶段面板', () => {
  it('展示九阶段闭环且不再包含 Probe', () => {
    const wrapper = mount(AppStagePanel, { props: { stage: stage() } })

    for (const label of [
      'Build Validation',
      'Bootstrap',
      'Surface Readiness',
      'Page Exploration',
      'Evidence Validation',
      'Evidence Sealing',
      'Session Closure',
      'Formal Inspection',
      'Report / Review',
    ]) {
      expect(wrapper.text()).toContain(label)
    }
    expect(wrapper.text()).not.toContain('Probe')
    expect(wrapper.text()).toContain('sha256:abc')
    expect(wrapper.text()).toContain('closed')
    expect(wrapper.text()).toContain('业务页面 2 · 页型 1')
    expect(wrapper.get('[data-testid="covered-business-pages"]').text()).toContain(
      '个人中心-profile ×3',
    )
    expect(wrapper.get('[data-testid="covered-business-pages"]').text()).toContain(
      '分享邀请-profile ×1',
    )
  })

  it('区分证据不足、技术失败和会话关闭失败', async () => {
    const wrapper = mount(AppStagePanel, {
      props: {
        stage: stage({
          status: 'evidence_insufficient',
          outcome: 'evidence_insufficient',
          terminal_reason_code: 'coverage_not_satisfied',
          evidence_gap_count: 2,
        }),
      },
    })
    expect(wrapper.get('[data-testid="exploration-status"]').text()).toContain('证据不足')

    await wrapper.setProps({
      stage: stage({
        status: 'technical_failed',
        outcome: null,
        terminal_reason_code: 'device_disconnected',
        evidence_index_id: null,
        evidence_index_hash: null,
      }),
    })
    expect(wrapper.get('[data-testid="exploration-status"]').text()).toContain('技术失败')
    expect(wrapper.text()).not.toContain('证据不足')

    await wrapper.setProps({
      stage: stage({
        status: 'technical_failed',
        outcome: null,
        terminal_reason_code: 'device_disconnected',
        evidence_index_id: 'index-partial',
        evidence_index_hash: null,
        evidence_preserved: true,
        recovery_state: 'evidence_sealed',
        technical_failure: {
          phase: 'runtime',
          reason_code: 'device_disconnected',
          error_type: 'ConnectionError',
          recovery_state: 'evidence_sealed',
        },
      }),
    })
    expect(wrapper.text()).toContain('技术失败 · 证据已保留')
    expect(wrapper.text()).toContain('部分证据已保留')
    expect(wrapper.text()).toContain('runtime · ConnectionError · evidence_sealed')

    await wrapper.setProps({
      stage: stage({
        status: 'close_failed',
        surface_session_status: 'lost',
        terminal_reason_code: 'close_failed:RuntimeError',
      }),
    })
    expect(wrapper.get('[data-testid="session-closure-stage"]').text()).toContain('会话关闭失败')
    expect(wrapper.get('[data-testid="session-closure-stage"]').text()).toContain('lost')
  })

  it('展示 Publication 四阶段与 Entry 级规则交接状态', () => {
    const wrapper = mount(AppStagePanel, { props: { stage: stage() } })
    const publication = wrapper.get('[data-testid="app-publication-stage"]')

    for (const label of ['封存', '投递', '领取', '判定']) {
      expect(publication.text()).toContain(label)
    }
    expect(publication.text()).toContain('#1 · final_observation')
    expect(publication.text()).toContain('home · linked')
  })

  it('显式展示 Publication 需复核和失败数量', () => {
    const value = stage()
    value.publication_projection!.needs_review_count = 1
    value.publication_projection!.failed_count = 2
    const wrapper = mount(AppStagePanel, { props: { stage: value } })

    expect(wrapper.get('[data-testid="app-publication-stage"]').text()).toContain(
      'Publication 需复核 1 · 失败 2',
    )
  })

  it('展示锁定目标、覆盖缺口和视觉 Provider 降级事实', () => {
    const wrapper = mount(AppStagePanel, {
      props: {
        stage: stage({
          outcome: 'evidence_insufficient',
          coverage_summary: {
            required_page_types: ['home', 'content_detail'],
            covered_page_types: ['home'],
            missing_page_types: ['content_detail'],
            missing_milestones: ['journey:内容详情'],
            app_target_snapshot: { target_hash: 'sha256:target-1' },
            degraded_provider_facts: [
              {
                provider_id: 'llm-app-vision',
                error_code: 'provider_call_failed',
              },
            ],
            visual_quality_satisfied: false,
          },
        }),
      },
    })

    expect(wrapper.text()).toContain('目标页型 home、content_detail')
    expect(wrapper.text()).toContain('缺失页型 content_detail')
    expect(wrapper.text()).toContain('缺失里程碑 journey:内容详情')
    expect(wrapper.text()).toContain('sha256:target-1')
    expect(wrapper.text()).toContain('视觉 Provider 已降级 1 次')
  })

  it('App 仅页面探索将正式质检与报告阶段标记为不适用', () => {
    const wrapper = mount(AppStagePanel, {
      props: { stage: stage(), executionMode: 'exploration_only' },
    })

    const text = wrapper.text()
    expect(text).toContain('Formal Inspection')
    expect(text).toContain('Report / Review')
    expect(wrapper.find('.stage-card:nth-child(8)').text()).toContain('不适用')
    expect(wrapper.find('.report-stage').text()).toContain('不适用')
    expect(text).not.toContain('工作项 2')
  })

  it('兼容旧任务的空探索投影', () => {
    const legacy = stage()
    legacy.app_exploration = null

    const wrapper = mount(AppStagePanel, { props: { stage: legacy } })

    expect(wrapper.text()).toContain('旧任务暂无 App Exploration 封存事实')
    expect(wrapper.text()).not.toContain('Probe')
  })
})
