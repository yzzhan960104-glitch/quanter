/**
 * Vitest 配置（前端组件/单测层 —— 第 3 项「前端组件/单测」落地的测试运行器）。
 *
 * Why 与 vite.config.ts 分离独立成文：刻意不碰 vite.config.ts——该文件的 proxy target 被
 * scripts/check_ports.py 静态比对盯住（predev 护栏），任何无关改动都可能误触护栏或污染端口
 * 真值。vitest 启动时优先读取本文件（不回退 vite.config.ts），故 alias/plugins 在此重新声明，
 * 与 vite.config.ts 保持对齐（单一真相源 ./src）。
 *
 * environment: 'jsdom'  组件渲染测试（@vue/test-utils mount DatasetTable 等）需 DOM 环境。
 * globals: true         describe/it/expect 全局可用（社区惯例，减少 import 噪声）。
 * include: src 下（递归）所有 .spec.ts，与源码同目录共置（caisen.spec.ts 紧邻 caisen.ts）。
 */
import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.spec.ts'],
  },
})
