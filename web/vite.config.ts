import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

/**
 * Vite 配置
 *
 * 开发代理：将 /api 请求转发到 FastAPI 后端（默认 http://localhost:8000）
 * 避免前端跨域问题，同时保持前后端独立开发
 */
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
