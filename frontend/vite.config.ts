import type { IncomingMessage, ServerResponse } from 'node:http'
import { defineConfig, loadEnv, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { MockApiServer } from './src/mockServer'

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
              mockRes.headers.forEach((v, k) => res.setHeader(k, v))
              const body = await mockRes.text()
              res.end(body)
            })
            .catch((err: unknown) => {
              res.statusCode = 500
              res.setHeader('content-type', 'application/json')
              res.end(
                JSON.stringify({
                  error: {
                    code: 'mock_error',
                    message: err instanceof Error ? err.message : 'mock error',
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
//  * browser calls same-origin /api, proxied to VITE_PROXY_TARGET (or served by
//    mockApiPlugin when VITE_API_MOCK=true so frontend work is never blocked).
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'
  const enableServerMock = env.VITE_API_MOCK === 'true' || env.VITE_MOCK_SERVER === 'true'

  return {
    plugins: enableServerMock ? [mockApiPlugin(), react()] : [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
      allowedHosts: true,
      ...(enableServerMock
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
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
    },
  }
})
