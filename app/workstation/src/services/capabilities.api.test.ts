import { afterEach, describe, expect, it, vi } from 'vitest'
import { configureHttpClient } from './http'
import { getV2Capabilities } from './capabilities.api'

afterEach(() => {
  configureHttpClient({})
  vi.unstubAllGlobals()
})

describe('V2 capabilities API', () => {
  it('读取 Web 探索门禁与固定 1920×1080 Profile', async () => {
    const payload = {
      web: {
        enabled: true,
        execution_modes: ['exploration_only'],
        default_browser_profile_id: 'desktop_fhd_1920x1080_v1',
        browser_profiles: [
          {
            profile_id: 'desktop_fhd_1920x1080_v1',
            viewport_width: 1920,
            viewport_height: 1080,
            screen_width: 1920,
            screen_height: 1080,
            device_scale_factor: 1,
            is_mobile: false,
            has_touch: false,
            color_scheme: 'light',
          },
        ],
      },
    }
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )
    vi.stubGlobal('fetch', fetchMock)

    await expect(getV2Capabilities()).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/v2/capabilities',
      expect.objectContaining({ headers: expect.any(Headers) }),
    )
  })
})
