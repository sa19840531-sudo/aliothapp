import { test, expect } from '../../fixtures/browser-telemetry'

test.use({ telemetryEnabled: true })

test('反例：健康点击与加载不因主机负载数值被诊断成GPU故障', async ({ page, telemetry }) => {
  await page.route('http://127.0.0.1:49199/**', (route) => route.fulfill({
    contentType: 'text/html', body: '<button onclick="this.textContent=\'clicked\'">open</button>',
  }))
  await page.goto('http://127.0.0.1:49199/healthy')
  await page.getByRole('button').click()
  await expect(page.getByRole('button')).toHaveText('clicked')
  await page.reload()
  await expect(page.getByRole('button')).toHaveText('open')
  await expect.poll(() => telemetry!.entries.filter((e) => e.kind === 'renderer_cdp').length).toBeGreaterThan(1)
  expect(telemetry!.entries.filter((e) => e.kind === 'page_crash')).toEqual([])
})

test('复现：点击派发后渲染主线程阻塞，浏览器CDP仍可响应', async ({ page, telemetry }) => {
  await page.setContent('<button>open</button>')
  await page.evaluate(() => document.querySelector('button')!.addEventListener('mousedown', () => {
    const until = performance.now() + 1000
    while (performance.now() < until) { /* 仅阻塞本测试独占渲染线程。 */ }
  }))
  telemetry!.mark('fault_renderer_begin')
  await expect(page.getByRole('button').click({ timeout: 250 })).rejects.toThrow(/Timeout/)
  await page.evaluate(() => document.readyState)
  await expect.poll(() => telemetry!.entries.some((e) => e.kind === 'renderer_cdp' && Number(e.elapsed_ms) > 450)).toBe(true)
  const fault = telemetry!.entries.find((e) => e.kind === 'fault_renderer_begin')!
  expect(telemetry!.entries.some((e) => e.kind === 'browser_cdp' && e.mono_ms > fault.mono_ms && !e.error)).toBe(true)
  expect(telemetry!.entries.filter((e) => e.kind === 'page_crash')).toEqual([])
})

test('复现：导航提交但子资源不结束，不能由503或GPU进程存在推断根因', async ({ page, telemetry }) => {
  let hold = false
  let release!: () => void
  const held = new Promise<void>((resolve) => { release = resolve })
  await page.route('http://127.0.0.1:49199/**', async (route) => {
    if (route.request().url().endsWith('/held.svg')) {
      if (hold) await held
      await route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg"/>' })
    } else await route.fulfill({ contentType: 'text/html', body: '<h1>ready</h1><img src="/held.svg">' })
  })
  await page.goto('http://127.0.0.1:49199/reload')
  hold = true
  telemetry!.mark('fault_load_begin')
  try {
    await expect(page.reload({ timeout: 250 })).rejects.toThrow(/Timeout/)
    expect(page.url()).toContain('/reload')
    expect(await page.evaluate(() => document.readyState)).toBe('interactive')
    await expect(page.getByRole('heading')).toHaveText('ready')
    telemetry!.mark('load_wait_confirmed')
  } finally { release() }
  await page.waitForLoadState('load')
  expect(telemetry!.entries.filter((e) => e.kind === 'page_crash')).toEqual([])
})

test('复现：测试线程阻塞与渲染阻塞分离记录，不能把迟到CDP回执当GPU崩溃', async ({ page, telemetry }) => {
  await page.setContent('<button>healthy</button>')
  telemetry!.mark('fault_node_begin')
  const start = telemetry!.entries.at(-1)!.mono_ms
  const until = performance.now() + 600
  while (performance.now() < until) { /* 独立采样线程继续记录进程和压力。 */ }
  telemetry!.mark('fault_node_end')
  const end = telemetry!.entries.at(-1)!.mono_ms
  await expect.poll(() => telemetry!.entries.some((e) => e.kind === 'node_event_loop' && Number(e.lag_ms) > 450)).toBe(true)
  expect(telemetry!.pressureEntries().some((e) => e.kind === 'host_processes' && e.mono_ms > start && e.mono_ms < end)).toBe(true)
  await page.getByRole('button').click()
  expect(telemetry!.entries.filter((e) => e.kind === 'page_crash')).toEqual([])
})

test('反例：真实渲染进程崩溃须有crash事件，不能仅从慢CDP推断', async ({ page, telemetry, browser }) => {
  await page.setContent('<h1>owned renderer</h1>')
  const session = await page.context().newCDPSession(page)
  const crashed = page.waitForEvent('crash')
  telemetry!.mark('fault_renderer_crash')
  const command = session.send('Page.crash').catch(() => undefined)
  await crashed
  expect(telemetry!.entries.some((e) => e.kind === 'page_crash')).toBe(true)
  const root = await browser.newBrowserCDPSession()
  expect((await root.send('SystemInfo.getProcessInfo')).processInfo.some((p) => p.type === 'browser')).toBe(true)
  await page.close()
  await command
  await root.detach()
})
