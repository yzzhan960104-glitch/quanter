/**
 * 应用入口
 *
 * 职责：
 * 1. 创建 Vue 应用实例
 * 2. 注册 Element Plus 组件库（全量注册，简化开发）
 * 3. 挂载路由
 * 4. 全局强制暗黑终端模式 + 注册 ECharts 暗色主题
 * 5. 挂载到 DOM
 */
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
// Element Plus 暗黑模式 CSS 变量覆盖（html.dark 生效）
import 'element-plus/theme-chalk/dark/css-vars.css'
// 全局终端主题层：必须在 EP dark css-vars 之后 import，靠后定义覆盖 EP 默认变量
// （TradingView 极夜黑调色板 + 等宽数字 + 表格/按钮加固，详见 styles/terminal.css）
import './styles/terminal.css'
import App from './App.vue'
import router from './router'
import { initTerminalDarkTheme } from './theme/echarts-terminal-dark'
import { logger } from './utils/logger'

// 全局强制暗黑终端模式：在 <html> 上挂 .dark 类，触发 EP dark css-vars
document.documentElement.classList.add('dark')
// 注册 ECharts 暗色主题（ProChart/NavChart 用 theme="terminal-dark"）
initTerminalDarkTheme()

const app = createApp(App)

// 全局错误兜底：任何组件内未捕获的异常都经此落到 console（带 [quanter] 前缀），
// 避免静默失败难定位。与 useTerminalState 的 SSE 错误打点配合，覆盖前端主要失败面。
app.config.errorHandler = (err, _instance, info) => {
  logger.error('未捕获的 Vue 异常:', err, '| 组件追踪:', info)
}

app.use(ElementPlus)
app.use(router)

app.mount('#app')
