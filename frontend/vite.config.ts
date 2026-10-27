import type { IncomingMessage, ServerResponse } from 'node:http'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv, type Plugin } from 'vite'
import { MockApiServer } from './src/api/mockServer'

/**
 * Serve /api from the in-memory mock when `VITE_API_MOCK=true`.
 *
 * The client is identical either way: it always calls same-origin `/api`, and the dev
 * server decides whether that reaches the Prism mock (default), a live backend
 * (`VITE_PROXY_TARGET`), or the in-memory mock. That is what lets the console, the unit
 * tests and the Playwright smoke test share one code path with no keys and no network.
 */
function mockApiPlugin(): Plugin {
  const mock = new MockApiServer()
  return {
    name: 'scholarloop-mock-api',
    configureServer(server) {
      server.middlewares.use((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        const url = req.url ?? '/'
        if (!url.startsWith('/api')) {
          next()
          return
        }
        const chunks: Buffer[] = []
        req.on('data', (chunk: Buffer) => chunks.push(chunk))
        req.on('end', () => {
          const headers = new Headers()
          for (const [key, val] of Object.entries(req.headers)) {
            if (typeof val === 'string') headers.set(key, val)
            else if (Array.isArray(val)) headers.set(key, val.join(', '))
          }
          const init: RequestInit = { method: req.method ?? 'GET', headers }
          if (chunks.length > 0 && req.method !== 'GET' && req.method !== 'HEAD') {
            init.body = Buffer.concat(chunks).toString('utf-8')
          }
          void mock
            .fetch(`http://scholarloop.local${url}`, init)
            .then(async (mockRes) => {
              res.statusCode = mockRes.status
              mockRes.headers.forEach((value, key) => res.setHeader(key, value))
              const body = await mockRes.text()
              res.end(body)
            })
            .catch((error: unknown) => {
              res.statusCode = 500
              res.setHeader('content-type', 'application/json')
              res.end(
                JSON.stringify({
                  error: {
                    code: 'mock_error',
                    message: error instanceof Error ? error.message : 'mock error',
                    context: {},
                  },
                }),
              )
            })
        })
      })
    },
  }
}

// Dev server settings are chosen for a proxied, sandboxed environment:
//  * host 0.0.0.0 + allowedHosts true so the preview proxy can reach it;
//  * browser calls same-origin /api, proxied by Vite to VITE_PROXY_TARGET - the Prism
//    mock of docs/openapi/openapi.yaml on port 4010 by default (`npm run mock`), or a
//    live backend on :8000.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || 'http://127.0.0.1:4010'
  const useMock = env.VITE_API_MOCK === 'true' || env.VITE_API_MOCK === '1'

  return {
    plugins: useMock ? [mockApiPlugin(), react()] : [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
      allowedHosts: true,
      ...(useMock
        ? {}
        : {
            proxy: {
              '/api': {
                target,
                changeOrigin: true,
                secure: false,
              },
            },
          }),
      hmr: {
        clientPort: 443,
      },
    },
    preview: {
      host: '0.0.0.0',
      port: 4173,
      strictPort: true,
      allowedHosts: true,
      ...(useMock
        ? {}
        : {
            proxy: {
              '/api': {
                target,
                changeOrigin: true,
                secure: false,
              },
            },
          }),
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
    },
  }
})
