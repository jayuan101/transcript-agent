import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Backend (FastAPI in api.py) runs on :8000 by default.
// Use 127.0.0.1, not "localhost": on Windows localhost resolves to IPv6 (::1)
// but uvicorn binds IPv4 (0.0.0.0), so a "localhost" proxy target is refused.
const API_TARGET = process.env.API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
      '/health': { target: API_TARGET, changeOrigin: true },
      '/docs': { target: API_TARGET, changeOrigin: true },
    },
  },
})
