import path from 'node:path'
import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  // .env lives in the repository root (one level above the frontend).
  // The dev proxy must target the same host/port the server binds.
  const env = loadEnv(mode, path.resolve(process.cwd(), '..'), '')
  const port = env.OBSERVATORY_PORT || '8080'
  // In Docker the port is published on OBSERVATORY_PUBLISH_HOST; outside
  // Docker the server binds OBSERVATORY_HOST. A wildcard bind is reachable
  // on loopback from the dev machine.
  const publishHost = env.OBSERVATORY_PUBLISH_HOST || env.OBSERVATORY_HOST || '127.0.0.1'
  const host = publishHost === '0.0.0.0' || publishHost === '::' ? '127.0.0.1' : publishHost

  return {
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: `http://${host}:${port}`,
          changeOrigin: true,
        },
      },
    },
    build: {
      outDir: 'dist',
      emptyOutDir: true,
    },
  }
})
