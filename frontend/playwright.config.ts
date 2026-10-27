import { defineConfig, devices } from '@playwright/test'

// Browser smoke tests for the flows that must never break: sign in, create a project
// from a Brief, watch a run, answer its checkpoint, read and export the article.
//
// The dev server runs with VITE_API_MOCK=true, so the smoke run needs no backend, no
// database and no provider keys - the same in-memory mock the unit tests use. Point
// VITE_PROXY_TARGET at a real API and drop the env var to smoke-test against it.
export default defineConfig({
  testDir: './playwright',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list']],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: 'http://127.0.0.1:5173',
    trace: 'retain-on-failure',
    video: 'off',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev -- --host 127.0.0.1 --port 5173',
    url: 'http://127.0.0.1:5173',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
    env: { VITE_API_MOCK: 'true' },
  },
  projects: [
    {
      name: 'chromium',
      use: { ...(devices['Desktop Chrome'] ?? {}) },
    },
  ],
})
