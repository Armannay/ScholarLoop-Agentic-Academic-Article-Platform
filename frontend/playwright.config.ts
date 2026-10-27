import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './playwright',
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: devices['desktop Chrome'],
    },
  ],
})
