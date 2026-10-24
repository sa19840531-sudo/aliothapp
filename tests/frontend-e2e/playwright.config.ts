import { defineConfig, devices } from '@playwright/test'

const isCi = Boolean(process.env.CI)
const port = Number(process.env.ALIOTH_E2E_PORT || 4173)

export default defineConfig({
  testDir: './specs',
  outputDir: './test-results',
  fullyParallel: true,
  forbidOnly: isCi,
  retries: isCi ? 2 : 0,
  workers: isCi ? 1 : undefined,
  timeout: 30_000,
  expect: {
    timeout: 8_000,
  },
  reporter: isCi
    ? [
        ['line'],
        ['junit', { outputFile: 'test-results/junit.xml' }],
        ['html', { outputFolder: 'playwright-report', open: 'never' }],
      ]
    : [['list'], ['html', { outputFolder: 'playwright-report', open: 'never' }]],
  use: {
    ...devices['Desktop Chrome'],
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
  },
  projects: [
    {
      name: 'workstation-chromium',
      testMatch: /workstation\/.*\.spec\.ts/,
      use: {
        baseURL: `http://127.0.0.1:${port}`,
      },
    },
    {
      name: 'admin-chromium',
      testMatch: /admin\/.*\.spec\.ts/,
      use: {
        // 管理后台已并入工作台同一个 SPA（/admin 前缀），共用同一个 dev server
        baseURL: `http://127.0.0.1:${port}`,
      },
    },
  ],
  webServer: [
    {
      command:
        `npm --prefix ../../app/workstation run dev -- --host 127.0.0.1 --port ${port} --strictPort`,
      url: `http://127.0.0.1:${port}/login`,
      reuseExistingServer: !isCi,
      timeout: 120_000,
    },
  ],
})
