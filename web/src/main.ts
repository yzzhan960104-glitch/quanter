/**
 * 应用入口
 *
 * 职责：
 * 1. 创建 Vue 应用实例
 * 2. 注册 Element Plus 组件库（全量注册，简化开发）
 * 3. 挂载路由
 * 4. 挂载到 DOM
 */
import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)

app.use(ElementPlus)
app.use(router)

app.mount('#app')
