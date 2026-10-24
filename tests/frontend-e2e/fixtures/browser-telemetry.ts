import { appendFileSync, readFileSync } from 'node:fs'
import { Worker } from 'node:worker_threads'
import { test as base, expect, type Browser, type Page, type TestInfo } from '@playwright/test'

type Entry = { kind: string; mono_ms: number; wall_ms: number; [key: string]: unknown }
export type BrowserTelemetry = {
  entries: Entry[]
  mark: (kind: string, data?: object) => void
  pressureEntries: () => Entry[]
}

async function observe(browser: Browser, page: Page, info: TestInfo) {
  const path = info.outputPath('browser-telemetry.jsonl')
  const entries: Entry[] = []
  const mark = (kind: string, data = {}) => {
    const entry = { mono_ms: Number(process.hrtime.bigint()) / 1e6, wall_ms: Date.now(), kind, ...data }
    entries.push(entry)
    appendFileSync(path, JSON.stringify(entry) + '\n')
  }
  const root = await browser.newBrowserCDPSession()
  const renderer = await page.context().newCDPSession(page)
  const processes = await root.send('SystemInfo.getProcessInfo')
  const browserPid = processes.processInfo.find((row) => row.type === 'browser')?.id
  if (!browserPid) throw new Error('独占测试浏览器缺少 CDP 进程身份')
  mark('identity', { browser_pid: browserPid, node_pid: process.pid, version: browser.version(),
    processes, gpu: await root.send('SystemInfo.getInfo') })
  const workerPath = info.outputPath('browser-pressure.jsonl')
  const worker = new Worker(new URL('./browser-pressure-worker.mjs', import.meta.url), {
    workerData: { path: workerPath, browserPid, nodePid: process.pid },
  })
  const workerExited = new Promise<void>((resolve) => worker.once('exit', () => resolve()))
  worker.on('error', (error) => mark('sampler_error', { error: String(error) }))
  await new Promise<void>((resolve, reject) => {
    worker.once('message', () => resolve())
    worker.once('error', reject)
  })
  const onCrash = () => mark('page_crash')
  const onDisconnect = () => mark('browser_disconnect')
  const onLoad = () => mark('page_load', { url: page.url() })
  const onCommit = () => mark('navigation_commit', { url: page.url() })
  page.on('crash', onCrash)
  page.on('load', onLoad)
  page.on('domcontentloaded', onCommit)
  browser.on('disconnected', onDisconnect)
  let stopped = false
  let expected = Number(process.hrtime.bigint()) / 1e6 + 50
  const loop = setInterval(() => {
    const now = Number(process.hrtime.bigint()) / 1e6
    mark('node_event_loop', { lag_ms: Math.max(0, now - expected) })
    expected = now + 50
  }, 50)
  const timers: NodeJS.Timeout[] = []
  const pending = new Set<Promise<void>>()
  function probe(kind: string, read: () => Promise<unknown>) {
    const run = async () => {
      const start = Number(process.hrtime.bigint()) / 1e6
      mark(kind + '_start')
      let deadline: NodeJS.Timeout
      const task = (async () => {
        try {
          const value = await Promise.race([read(), new Promise((_, reject) => {
            deadline = setTimeout(() => reject(new Error('诊断采样 1500ms 未返回')), 1500)
          })])
          mark(kind, { elapsed_ms: Number(process.hrtime.bigint()) / 1e6 - start, value })
        } catch (error) {
          mark(kind, { elapsed_ms: Number(process.hrtime.bigint()) / 1e6 - start, error: String(error) })
        } finally {
          clearTimeout(deadline!)
        }
      })()
      pending.add(task)
      await task
      pending.delete(task)
      if (!stopped) timers.push(setTimeout(run, 200))
    }
    void run()
  }
  probe('browser_cdp', () => root.send('SystemInfo.getProcessInfo'))
  probe('renderer_cdp', () => renderer.send('Runtime.evaluate', {
    expression: '({ready:document.readyState,time:performance.now(),url:location.href})',
    returnByValue: true,
  }))
  return { entries, mark, pressureEntries: () => readFileSync(workerPath, 'utf8').trim()
    .split('\n').map((line) => JSON.parse(line) as Entry), async stop() {
    stopped = true
    timers.forEach(clearTimeout)
    clearInterval(loop)
    await Promise.all(pending)
    page.off('crash', onCrash)
    page.off('load', onLoad)
    page.off('domcontentloaded', onCommit)
    browser.off('disconnected', onDisconnect)
    await renderer.detach().catch((error) => mark('detach_error', { error: String(error) }))
    await root.detach().catch((error) => mark('detach_error', { error: String(error) }))
    worker.postMessage('stop')
    await workerExited
    mark('diagnostics_stopped')
    await info.attach('browser-telemetry', { path, contentType: 'application/x-ndjson' })
    await info.attach('browser-pressure', { path: workerPath, contentType: 'application/x-ndjson' })
  } }
}

// 默认测试不增加诊断负担；仅本轮显式隔离验证启用，原用例超时与断言不变。
export const test = base.extend<{ telemetryEnabled: boolean; telemetry: BrowserTelemetry | null }>({
  telemetryEnabled: [process.env.ALIOTH_BROWSER_TELEMETRY === '1', { option: true }],
  telemetry: [async ({ browser, page, telemetryEnabled }, use, info) => {
    if (!telemetryEnabled) return use(null)
    const recorder = await observe(browser, page, info)
    try { await use(recorder) } finally { await recorder.stop() }
  }, { auto: true }],
})
export { expect }
