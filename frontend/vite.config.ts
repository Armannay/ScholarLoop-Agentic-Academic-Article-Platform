import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server settings are chosen for a proxied, sandboxed environment:
//
//  * host 0.0.0.0 so the preview proxy in front of the sandbox can reach it;
//  * allowedHosts true so a preview hostname (not just localhost) is accepted;
//  * the browser only ever calls same-origin /api, which this server proxies to the
//    backend. The browser is never asked to resolve "localhost" itself, because the
//    browser is not where the backend runs.
//
// VITE_PROXY_TARGET overrides the proxy target (docker compose points it at the
// backend service); VITE_API_BASE_URL would bypass the proxy entirely, so it is
// deliberately empty by default.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    server: {
      host: '0.0.0.0',
      port: 5173,
      strictPort: true,
      allowedHosts: true,
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          secure: false,
        },
      },
      // HMR over the preview proxy: keep the client on the proxied host/port.
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
