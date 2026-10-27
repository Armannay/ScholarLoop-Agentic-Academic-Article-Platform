import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Unit tests run in jsdom so the same components the browser gets are the ones under
// test: real DOM, real events, real focus. The mock API (`src/mockServer.ts`) is the
// only transport, so no test ever reaches the network (HARD RULE 5 in the backend, and
// the same discipline here).
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['src/test/setup.ts'],
    restoreMocks: true,
    testTimeout: 15_000,
  },
})
