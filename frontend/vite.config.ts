import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// Dev server settings are chosen for a proxied, sandboxed environment:
//  * host 0.0.0.0 + allowedHosts true so the preview proxy can reach it;
//  * browser calls same-origin /api, proxied by Vite to VITE_PROXY_TARGET. In dev the
//    default target is the Prism mock of docs/openapi/openapi.yaml on port 4010
//    (`npm run mock` starts it; completion scenario). Point VITE_PROXY_TARGET at the
//    live backend (http://127.0.0.1:8000) to work against a real API.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const target = env.VITE_PROXY_TARGET || 'http://127.0.0.1:4010'

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
      hmr: {
        clientPort: 443,
      },
    },
    preview: {
      host: '0.0.0.0',
      port: 4173,
      strictPort: true,
      allowedHosts: true,
      proxy: {
        '/api': {
          target,
          changeOrigin: true,
          secure: false,
        },
      },
    },
    build: {
      outDir: 'dist',
      sourcemap: true,
    },
  }
})
