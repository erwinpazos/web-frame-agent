import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  // Load environment variables from backend directory or project root
  const envBackend = loadEnv(mode, path.resolve(import.meta.dirname, '../backend'), '')
  const envRoot = loadEnv(mode, path.resolve(import.meta.dirname, '..'), '')
  const env = { ...envRoot, ...envBackend, ...process.env }

  // Strictly enforce required backend connection parameters from env (Fail Fast)
  if (!env.BACKEND_HOST) {
    throw new Error(
      "[Vite Config] BACKEND_HOST is strictly required in .env but was missing or empty."
    )
  }
  if (!env.BACKEND_PORT) {
    throw new Error(
      "[Vite Config] BACKEND_PORT is strictly required in .env but was missing or empty."
    )
  }

  const backendHost = env.BACKEND_HOST
  const backendPort = env.BACKEND_PORT
  const backendTarget = `http://${backendHost}:${backendPort}`
  const backendWsTarget = `ws://${backendHost}:${backendPort}/api/v1/cdp/extension`
  // Derive frontend bind host and port:
  // If FRONTEND_URL is set, parse it. If not, auto-detect from VITE_PORT or default to dynamic port
  let serverHost: string | boolean = true
  let serverPort: number | undefined = undefined

  if (env.FRONTEND_URL) {
    try {
      const parsedUrl = new URL(env.FRONTEND_URL)
      serverHost = parsedUrl.hostname
      if (parsedUrl.port) {
        serverPort = parseInt(parsedUrl.port, 10)
      }
    } catch {}
  } else if (env.PORT || env.VITE_PORT) {
    const rawPort = env.PORT || env.VITE_PORT || ''
    serverPort = rawPort ? parseInt(rawPort, 10) : undefined
  }
  return {
    define: {
      '__COBROWSE_CONFIG__': JSON.stringify({
        backendWsUrl: backendWsTarget,
        frontendUrl: env.FRONTEND_URL || '',
      }),
    },
    plugins: [react(), tailwindcss()],
    server: {
      host: serverHost,
      port: serverPort,
      proxy: {
        '/api': {
          target: backendTarget,
          changeOrigin: true,
          ws: true,
          configure: (proxy) => {
            proxy.on('error', (_err, _req, _res) => {
              // Suppress unhandled proxy errors in terminal during restarts or socket resets
            })
            proxy.on('proxyReqWs', (_proxyReq, _req, socket) => {
              socket.on('error', () => {
                // Suppress ECONNABORTED and ECONNRESET from socket aborts
              })
            })
          },
        },
        '/health': {
          target: backendTarget,
          changeOrigin: true,
          configure: (proxy) => {
            proxy.on('error', () => {
              // Suppress offline health check connection refused logs
            })
          },
        },
      },
    },
  }
})
