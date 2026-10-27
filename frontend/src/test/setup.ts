import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// Unmount between tests so a lingering SSE reconnect loop cannot leak into the next one.
afterEach(() => {
  cleanup()
})
