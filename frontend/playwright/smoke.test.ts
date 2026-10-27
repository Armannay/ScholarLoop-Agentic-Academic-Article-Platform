import { test, expect } from '@playwright/test'
import { access, constants } from 'node:fs'
import { spawn, type ChildProcess } from 'node:child_process'
import http from 'node:http'
import path from 'node:path'

const ROOT = path.join(__dirname, '..')
const FRONTEND = ROOT
const OPENAPI = path.join(ROOT, '..', 'docs', 'openapi')
const VITE_BIN = path.join(FRONTEND, 'node_modules', '.bin', 'vite')
const PRISM_BIN = path.join(OPENAPI, 'node_modules', '.bin', 'prism')
const COMPOSED_SPEC = path.join(OPENAPI, '.mock.completion.openapi.yaml')

let vite: ChildProcess | null = null
let prism: ChildProcess | null = null

// Wait for an HTTP endpoint to answer 200 (the dev proxy only reaches /api/health
// once both Vite and the Prism mock are up).
const waitFor = (url: string, timeoutMs = 30_000): Promise<void> => {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + timeoutMs
    const attempt = (): void => {
      const req = http.get(url, (res) => {
        res.resume()
        if (res.statusCode === 200) return resolve()
        retry()
      })
      req.on('error', retry)
      req.setTimeout(500, () => { req.destroy(); retry() })
      const retry = (): void => {
        if (Date.now() > deadline) reject(new Error(`timed out waiting for ${url}`))
        else setTimeout(attempt, 200)
      }
    }
    attempt()
  })
}

test.beforeAll(async () => {
  for (const bin of [VITE_BIN, PRISM_BIN]) {
    try { await access(bin, constants.X_OK) } catch {
      throw new Error(`missing binary ${bin}: run the matching `npm install` first`)
    }
  }
  try { await access(COMPOSED_SPEC, constants.R_OK) } catch {
    throw new Error(
      `missing composed spec ${COMPOSED_SPEC}: run \`npm run mock\` in docs/openapi first`,
    )
  }
  // The composed Prism document must exist: `npm run mock` generates it.
  prism = spawn(PRISM_BIN, [
    'mock', COMPOSED_SPEC, '--host', '127.0.0.1', '--port', '4010',
    '--dynamic', 'false', '--errors', 'false', '--cors', 'true', '--verboseLevel', 'error',
  ], { cwd: OPENAPI, stdio: 'ignore' })
  vite = spawn(VITE_BIN, [], { cwd: FRONTEND, stdio: 'ignore', env: { ...process.env } })
  await waitFor('http://127.0.0.1:5173/api/health')
})

test.afterAll(() => {
  vite?.kill()
  prism?.kill()
})

test('the console shell loads against the Prism mock', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'ScholarLoop' })).toBeVisible()
  await expect(page.getByText('Sign in to ScholarLoop')).toBeVisible()

  // /api is proxied by Vite to the Prism mock of the OpenAPI spec.
  const health = await page.request.get('/api/health')
  expect(health.status()).toBe(200)
  const body = await health.json()
  expect(body.status).toBe('ok')
  expect(body.contract_version).toBe(1)
})
