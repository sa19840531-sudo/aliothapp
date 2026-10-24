import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import PageExplorationPanel from '@/components/PageExplorationPanel.vue'
import type { PageExplorationSummary } from '@/types/task-insight'

function summary(
  overrides: Partial<PageExplorationSummary> = {},
): PageExplorationSummary {
  return {
    tracking_available: true,
    complete: true,
    quality_status: 'complete',
    requires_review: false,
    page_total: 3,
    active_page_count: 2,
    terminal_page_count: 2,
    completed_page_count: 1,
    degraded_page_count: 1,
    failed_page_count: 0,
    pending_page_count: 0,
    superseded_page_count: 1,
    abandoned_page_count: 0,
    evidence_only_page_count: 1,
    page_status_counts: { completed: 1, degraded: 1 },
    page_lifecycle_counts: { active: 2, superseded: 1, evidence_only: 1 },
    action_total: 4,
    pending_action_count: 0,
    explored_action_count: 3,
    inactive_action_count: 1,
    failed_action_count: 1,
    blocked_action_count: 0,
    skipped_action_count: 0,
    action_status_counts: { explored: 3, failed: 1 },
    pending_work_count: 0,
    coverage: {
      catalog_observation: {
        catalog_page_types: ['home'],
        catalog_page_type_total: 1,
        observed_page_types: ['home'],
        observed_page_type_count: 1,
        business_observed_page_types: ['home'],
        missing_catalog_page_types: [],
        raw_coverage_rate: 1,
      },
      lifecycle_shells: {
        observed_page_types: [],
        strict_line_loading_observed: false,
        startup_ad_observed: false,
        loading_state_observed: false,
      },
      product_business: {
        assessment_available: true,
        assessment_scope_complete: true,
        assessment_scope_verified: true,
        assessment_complete: true,
        missing_assessment_page_types: [],
        applicable_page_types: ['home'],
        covered_page_types: ['home'],
        known_uncovered_page_types: [],
        unresolved_page_types: [],
        not_applicable_page_types: [],
        covered_count: 1,
        applicable_count: 1,
        coverage_rate: 1,
      },
      exploration: {
        action_total: 4,
        terminal_count: 3,
        pending_count: 0,
        explored_count: 3,
        blocked_count: 0,
        skipped_count: 0,
        failed_count: 1,
        terminal_rate: 0.75,
        explored_rate: 0.75,
      },
      gaps: [],
      acceptance_complete: true,
    },
    page_results: [
      {
        page_capture_id: 'capture-home',
        parent_page_capture_id: null,
        supersedes_page_capture_id: null,
        page_seq: 1,
        page_type: 'home',
        page_url: 'app://demo/home',
        page_title: '首页',
        captured_at: '2026-08-25T12:00:00+00:00',
        detection_status: 'recognized',
        inspection_status: 'completed',
        lifecycle_status: 'active',
        failure_stage: null,
        failure_code: null,
        failure_message: null,
        failure_retryable: null,
        abandon_reason: null,
      },
      {
        page_capture_id: 'capture-old',
        parent_page_capture_id: null,
        supersedes_page_capture_id: null,
        page_seq: 2,
        page_type: 'profile',
        page_url: 'app://demo/profile',
        page_title: '旧资料页',
        captured_at: '2026-08-25T12:01:00+00:00',
        detection_status: 'recognized',
        inspection_status: 'completed',
        lifecycle_status: 'superseded',
        failure_stage: null,
        failure_code: null,
        failure_message: null,
        failure_retryable: null,
        abandon_reason: null,
      },
      {
        page_capture_id: 'capture-side',
        parent_page_capture_id: null,
        supersedes_page_capture_id: null,
        page_seq: 3,
        page_type: 'ad',
        page_url: null,
        page_title: null,
        captured_at: null,
        detection_status: null,
        inspection_status: 'degraded',
        lifecycle_status: 'evidence_only',
        failure_stage: null,
        failure_code: null,
        failure_message: null,
        failure_retryable: null,
        abandon_reason: null,
      },
    ],
    ...overrides,
  }
}

describe('PageExplorationPanel', () => {
  it('renders complete metrics and lifecycle labels', () => {
    const wrapper = mount(PageExplorationPanel, {
      props: { summary: summary() },
    })

    expect(wrapper.text()).toContain('动态页面探索已完成')
    expect(wrapper.text()).toContain('已收敛')
    expect(wrapper.text()).toContain('发现页面 3')
    expect(wrapper.text()).toContain('替代 / 放弃 1')
    expect(wrapper.text()).toContain('旁路证据 1')
    expect(wrapper.text()).toContain('页面检查完成')
    expect(wrapper.text()).toContain('已被替代')
    expect(wrapper.text()).toContain('旁路证据')
    expect(wrapper.text()).toContain('产品业务页面覆盖合同')
    expect(wrapper.text()).toContain('产品覆盖已闭合')
    expect(wrapper.text()).toContain('有效覆盖率100%')
    expect(wrapper.text()).toContain('已覆盖home')
  })

  it('shows attention badge while exploration is incomplete', () => {
    const wrapper = mount(PageExplorationPanel, {
      props: {
        summary: summary({
          complete: false,
          quality_status: 'in_progress',
          pending_page_count: 2,
          pending_work_count: 1,
        }),
      },
    })

    expect(wrapper.text()).toContain('仍有动态工作待处理')
    expect(wrapper.text()).toContain('探索中')
    expect(wrapper.find('em.attention').exists()).toBe(true)
  })

  it('toggles collapsible body', async () => {
    const wrapper = mount(PageExplorationPanel, {
      props: {
        summary: summary(),
        collapsible: true,
        defaultExpanded: false,
      },
    })

    expect(wrapper.find('.metrics').exists()).toBe(false)
    await wrapper.find('button.collapsible-head').trigger('click')
    expect(wrapper.find('.metrics').exists()).toBe(true)
    expect(wrapper.text()).toContain('收起')
  })

  it('renders failure and abandon messages for abnormal pages', () => {
    const wrapper = mount(PageExplorationPanel, {
      props: {
        summary: summary({
          complete: true,
          requires_review: true,
          quality_status: 'needs_review',
          page_results: [
            {
              page_capture_id: 'capture-failed',
              parent_page_capture_id: null,
              supersedes_page_capture_id: null,
              page_seq: 1,
              page_type: 'home',
              page_url: 'app://demo/home',
              page_title: '失败页',
              captured_at: '2026-08-25T12:00:00+00:00',
              detection_status: 'recognized',
              inspection_status: 'failed',
              lifecycle_status: 'active',
              failure_stage: 'evaluating',
              failure_code: 'PAGE_INSPECTION_FAILED',
              failure_message: '规则执行超时',
              failure_retryable: false,
              abandon_reason: null,
            },
            {
              page_capture_id: 'capture-abandoned',
              parent_page_capture_id: null,
              supersedes_page_capture_id: null,
              page_seq: 2,
              page_type: 'login',
              page_url: null,
              page_title: '放弃页',
              captured_at: null,
              detection_status: null,
              inspection_status: 'failed',
              lifecycle_status: 'abandoned',
              failure_stage: null,
              failure_code: null,
              failure_message: null,
              failure_retryable: null,
              abandon_reason: '会话关闭后放弃',
            },
          ],
        }),
      },
    })

    expect(wrapper.text()).toContain('探索已完成，存在局部失败待复核')
    expect(wrapper.text()).toContain('已收敛 · 待复核')
    expect(wrapper.text()).toContain('PAGE_INSPECTION_FAILED · 规则执行超时')
    expect(wrapper.text()).toContain('会话关闭后放弃')
    expect(wrapper.text()).toContain('已放弃')
  })

  it('does not present incomplete applicability assessment as product coverage', () => {
    const base = summary()
    const wrapper = mount(PageExplorationPanel, {
      props: {
        summary: summary({
          coverage: {
            ...base.coverage,
            acceptance_complete: null,
            product_business: {
              ...base.coverage.product_business,
              assessment_scope_complete: false,
              assessment_scope_verified: false,
              assessment_complete: false,
              coverage_rate: null,
              unresolved_page_types: ['content_detail'],
              missing_assessment_page_types: ['profile'],
            },
          },
        }),
      },
    })

    expect(wrapper.text()).toContain('评估范围未闭合')
    expect(wrapper.text()).toContain('有效覆盖率待确认')
    expect(wrapper.text()).toContain('未形成结论content_detail')
    expect(wrapper.text()).toContain('缺少适用性评估profile')
  })
})
