import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Unit and component tests run in jsdom, so the components under test are the ones the
// browser gets: real DOM, real events, real focus. The only transport is the in-memory
// mock (`src/api/mockServer.ts`), so no test reaches the network.
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
