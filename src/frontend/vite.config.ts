import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
const __dirname = path.dirname(fileURLToPath(import.meta.url))

const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8000'
const proxiedPaths = ['/ui/workbench-tools.html', '/ui/assets', '/health', '/hello', '/v1', '/v2', '/v3', '/v4', '/v5', '/v6', '/v7', '/v8', '/v9', '/v10', '/v11', '/api']

export default defineConfig({
  base: './',
  cacheDir: '.cache/vite',
  plugins: [react(), tailwindcss()],
  resolve: { alias: { '@': path.resolve(__dirname, './src') } },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: Object.fromEntries(proxiedPaths.map((route) => [route, {
      target: apiTarget,
      changeOrigin: true,
      ws: route === '/api',
      configure: (proxy) => {
        proxy.on('proxyReq', (proxyReq) => {
          proxyReq.setHeader('origin', apiTarget)
          proxyReq.setHeader('referer', `${apiTarget}/`)
        })
      },
    }])),
  },
  build: {
    outDir: 'dist',
    sourcemap: process.env.LINGXI_DEBUG_BUILD === '1',
    chunkSizeWarningLimit: 900,
  },
})
