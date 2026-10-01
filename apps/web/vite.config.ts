import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

export default defineConfig(({ mode }) => {
  // Read only API routing settings on the dev server; never expose server secrets.
  const environment = loadEnv(mode, fileURLToPath(new URL('../../', import.meta.url)), 'API_')
  const port = environment.API_PORT || '8787'
  return {
  plugins: [react()],
  server: {
    proxy: {
      '/api': {
        target: `http://127.0.0.1:${port}`,
        changeOrigin: false,
      },
    },
  },
  }
})
