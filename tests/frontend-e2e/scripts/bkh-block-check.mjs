import { createRequire } from 'node:module'
import { mkdir, writeFile } from 'node:fs/promises'
import path from 'node:path'

const targetUrl = 'http://h5.bkh052.com/'
const nodeModules = process.env.CODEX_NODE_MODULES

if (!nodeModules) {
  throw new Error('缺少 CODEX_NODE_MODULES 环境变量')
}

const requireFromRuntime = createRequire(path.join(nodeModules, '_resolver.cjs'))
const { chromium } = requireFromRuntime('playwright')

const resultRoot = path.resolve('test-results', 'bkh-block-check')
const profileRoot = path.join(resultRoot, 'isolated-chrome-profile')
await mkdir(resultRoot, { recursive: true })

const challengePatterns = [
  'cf-chl',
  'challenge-platform',
  'cloudflare',
  'just a moment',
  'cf-turnstile',
  '/cdn-cgi/challenge',
]

const commonArgs = [
  '--disable-blink-features=AutomationControlled',
  '--disable-infobars',
  '--no-first-run',
  '--no-default-browser-check',
]

function selectHeaders(headers) {
  const selected = {}
  for (const name of ['server', 'cf-ray', 'cf-mitigated', 'location', 'content-type']) {
    if (headers[name]) selected[name] = headers[name]
  }
  return selected
}

function summarizeResponse(response) {
  const url = response.url()
  return {
    url,
    status: response.status(),
    resourceType: response.request().resourceType(),
  }
}

async function inspectPage(page, context, modeName) {
  const importantResponses = []
  const failedRequests = []
  const consoleMessages = []

  page.on('response', (response) => {
    const url = response.url().toLowerCase()
    const status = response.status()
    if (
      response.request().isNavigationRequest()
      || url.includes('/bkapi/')
      || url.includes('/cdn-cgi/')
      || status >= 400
    ) {
      importantResponses.push(summarizeResponse(response))
    }
  })

  page.on('requestfailed', (request) => {
    failedRequests.push({
      url: request.url(),
      resourceType: request.resourceType(),
      errorText: request.failure()?.errorText ?? 'unknown',
    })
  })

  page.on('console', (message) => {
    if (['error', 'warning'].includes(message.type())) {
      consoleMessages.push({ type: message.type(), text: message.text().slice(0, 500) })
    }
  })

  const startedAt = new Date().toISOString()
  let navigationResponse = null
  let navigationError = null
  try {
    navigationResponse = await page.goto(targetUrl, {
      waitUntil: 'domcontentloaded',
      timeout: 60_000,
    })
    await page.waitForLoadState('networkidle', { timeout: 15_000 }).catch(() => undefined)
    await page.waitForTimeout(5_000)
  } catch (error) {
    navigationError = error instanceof Error ? error.message : String(error)
  }

  const finalUrl = page.url()
  const title = await page.title().catch(() => '')
  const html = await page.content().catch(() => '')
  const bodyText = await page.locator('body').innerText({ timeout: 3_000 }).catch(() => '')
  const lowerHtml = html.toLowerCase()
  const cookies = await context.cookies(finalUrl === 'about:blank' ? targetUrl : finalUrl)
  const cookieMetadata = cookies.map((cookie) => ({
    name: cookie.name,
    domain: cookie.domain,
    path: cookie.path,
    expires: cookie.expires,
    httpOnly: cookie.httpOnly,
    secure: cookie.secure,
    sameSite: cookie.sameSite,
  }))

  const automationState = await page.evaluate(() => ({
    webdriver: navigator.webdriver,
    userAgent: navigator.userAgent,
    language: navigator.language,
    platform: navigator.platform,
  })).catch(() => null)

  let navigationHeaders = {}
  if (navigationResponse) {
    navigationHeaders = selectHeaders(await navigationResponse.allHeaders())
  }

  const screenshotPath = path.join(resultRoot, `${modeName}.png`)
  await page.screenshot({ path: screenshotPath, fullPage: false }).catch(() => undefined)

  const detectedPatterns = challengePatterns.filter((pattern) => lowerHtml.includes(pattern))
  const cfCookieNames = cookieMetadata
    .map((cookie) => cookie.name)
    .filter((name) => name.startsWith('cf_') || name.startsWith('__cf'))

  return {
    mode: modeName,
    startedAt,
    finishedAt: new Date().toISOString(),
    requestedUrl: targetUrl,
    finalUrl,
    navigationStatus: navigationResponse?.status() ?? null,
    navigationHeaders,
    navigationError,
    title,
    bodyTextSample: bodyText.replace(/\s+/g, ' ').trim().slice(0, 1_000),
    challenge: {
      detectedPatterns,
      cfCookieNames,
      likelyChallenge: detectedPatterns.length > 0,
    },
    automationState,
    cookies: cookieMetadata,
    bkapiResponses: importantResponses.filter((item) => item.url.includes('/bkapi/')),
    importantResponses: importantResponses.slice(0, 200),
    failedRequests: failedRequests.slice(0, 100),
    consoleMessages: consoleMessages.slice(0, 100),
    screenshotPath,
  }
}

async function runEphemeralMode(mode) {
  const browser = await chromium.launch(mode.launchOptions)
  try {
    const context = await browser.newContext({
      viewport: mode.headless ? { width: 1440, height: 900 } : null,
      ignoreHTTPSErrors: false,
    })
    const page = await context.newPage()
    return await inspectPage(page, context, mode.name)
  } finally {
    await browser.close()
  }
}

async function runPersistentMode() {
  await mkdir(profileRoot, { recursive: true })
  const context = await chromium.launchPersistentContext(profileRoot, {
    channel: 'chrome',
    headless: false,
    args: commonArgs,
    viewport: null,
    ignoreHTTPSErrors: false,
  })
  try {
    const pages = context.pages()
    const page = pages[0] ?? await context.newPage()
    return await inspectPage(page, context, 'chrome-persistent-isolated')
  } finally {
    await context.close()
  }
}

const modes = [
  {
    name: 'chrome-headless-baseline',
    headless: true,
    launchOptions: {
      channel: 'chrome',
      headless: true,
    },
  },
  {
    name: 'chrome-headed-automationcontrolled',
    headless: false,
    launchOptions: {
      channel: 'chrome',
      headless: false,
      args: commonArgs,
    },
  },
]

const results = []
for (const mode of modes) {
  try {
    results.push(await runEphemeralMode(mode))
  } catch (error) {
    results.push({
      mode: mode.name,
      fatalError: error instanceof Error ? error.stack ?? error.message : String(error),
    })
  }
}

try {
  results.push(await runPersistentMode())
} catch (error) {
  results.push({
    mode: 'chrome-persistent-isolated',
    fatalError: error instanceof Error ? error.stack ?? error.message : String(error),
  })
}

const reportPath = path.join(resultRoot, 'report.json')
await writeFile(reportPath, `${JSON.stringify({ generatedAt: new Date().toISOString(), results }, null, 2)}\n`, 'utf8')
console.log(JSON.stringify({ reportPath, results }, null, 2))
