import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import WebReportExplanationPanel from './WebReportExplanationPanel.vue'

describe('WebReportExplanationPanel', () => {
  it('分别展示业务页长图、生命周期、unknown 与广告证据', () => {
    const wrapper = mount(WebReportExplanationPanel, {
      props: {
        explanation: {
          schema_version: 'web-report-explanation.v1',
          task_id: 'task-web',
          agent_run_id: 'run-web',
          finalization_epoch: 2,
          notifications: [],
          sequences: [],
          publication_rules: [],
          rule_materials: [],
          page_type_summary: {
            business_page_types: ['home', 'content_detail'],
            lifecycle_page_types: ['age_gate'],
            unknown_pages: ['capture-unknown'],
            ad_page_count: 1,
          },
          web_logical_pages: [
            {
              logical_page_id: 'logical-detail',
              primary_capture_id: 'capture-detail',
              page_type: 'content_detail',
              classification_status: 'classified',
              supplemental_full_page: {
                status: 'captured',
                capture_id: 'capture-full',
                screenshot_artifact_id: 'artifact-full',
              },
            },
          ],
          web_ad_pages: [
            {
              capture_id: 'capture-ad',
              parent_capture_id: 'capture-detail',
              capture_purpose: 'web_ad_page',
              page_type: 'ad_page',
              coverage_group: 'evidence_only',
              requested_url: 'https://ads.example/campaign',
              final_url: 'https://ads.example/landing',
              screenshot_artifact_id: 'artifact-ad',
            },
          ],
        },
      },
    })

    expect(wrapper.text()).toContain('业务页：home、content_detail')
    expect(wrapper.text()).toContain('生命周期页：age_gate')
    expect(wrapper.text()).toContain('Unknown：1 · 广告页：1')
    expect(wrapper.text()).toContain('全页长图 captured')
    expect(wrapper.text()).toContain('Capture capture-full')
    expect(wrapper.text()).toContain('ad_page · Capture capture-ad · 父 Capture capture-detail')
    expect(wrapper.text()).toContain('最终 https://ads.example/landing')
  })
})
