import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import PageGraphPanel from './PageGraphPanel.vue'

const mocks = vi.hoisted(() => ({
  getPageGraph: vi.fn(),
}))

vi.mock('@/services/task-insight.api', () => ({
  getPageGraphProjection: mocks.getPageGraph,
}))

describe('PageGraphPanel', () => {
  beforeEach(() => {
    mocks.getPageGraph.mockReset()
  })

  it('renders layer split from page graph', async () => {
    mocks.getPageGraph.mockResolvedValue({
      status: 'ready',
      states: [
        {
          state_id: 's1',
          logical_page_key: 'page:home',
          counts_as_business_page: true,
          is_overlay: false,
          is_external: false,
        },
        {
          state_id: 'ov',
          logical_page_key: 'overlay:popup',
          counts_as_business_page: false,
          is_overlay: true,
          is_external: false,
        },
      ],
      transitions: [{ transition_id: 'e1', status: 'completed', proof: 'trusted' }],
      exploration_tree: {
        roots: [{ state_id: 's1', label: '首页', page_type: 'home' }],
      },
    })

    const wrapper = mount(PageGraphPanel, {
      props: { taskId: 'task-1' },
    })
    await flushPromises()

    expect(mocks.getPageGraph).toHaveBeenCalledWith('task-1')
    expect(wrapper.text()).toContain('可观测')
    expect(wrapper.text()).toContain('业务')
    expect(wrapper.text()).toContain('首页')
    expect(wrapper.text()).toContain('不驱动 Completion Gate')
  })

  it('shows unavailable reason without driving gate', async () => {
    mocks.getPageGraph.mockResolvedValue({
      status: 'unavailable',
      reason: 'graph_repository_not_configured',
    })
    const wrapper = mount(PageGraphPanel, {
      props: { taskId: 'task-2' },
    })
    await flushPromises()
    expect(wrapper.text()).toContain('graph_repository_not_configured')
  })
})
