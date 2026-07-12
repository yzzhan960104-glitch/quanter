import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

/**
 * Vite 配置
 *
 * 开发代理：将 /api 请求转发到 FastAPI 后端（默认 http://localhost:8000）
 * 避免前端跨域问题，同时保持前后端独立开发
 *
 * resolve.alias：把 `@/*` 别名同步到打包/开发阶段。
 *
 * Why（反黑盒）：tsconfig.json 的 `paths: {"@/*": ["./src/*"]}` 只对 vue-tsc 类型检查
 * 生效，Vite/Rollup 不会自动读取 tsconfig paths（除非引入 vite-tsconfig-paths 插件）。
 * 之前 ProChart/PositionsTable 等用 `@/` 的组件未被任何已挂载视图引用，未进入打包图，
 * 缺陷被掩盖；一旦 useTerminalState.ts 用 `@/api/backtest` 进入入口依赖图，Rollup
 * 立即报 "failed to resolve import @/api/backtest"。此处显式补 alias，单一真相源
 * 与 tsconfig paths 对齐（均指向 ./src）。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    strictPort: true,   // 5173 被占则启动失败（不漂移到 5174+），强制走 scripts/dev.py 启动前清残留，杜绝多 vite 累积
    proxy: {
      '/api': {
        // 端口须与后端启动命令 `uvicorn server.main:app --port 8000` 对齐；
        // 用 127.0.0.1 锁定 IPv4，规避 Windows + Node17+ 下 localhost 解析为
        // ::1+127.0.0.1 双地址、internalConnectMultiple 并发尝试引发的 AggregateError。
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
