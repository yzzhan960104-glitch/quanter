<!--
  根组件

  职责：
  1. 顶部导航栏（Logo + 模式切换 Tab）
  2. 路由视图容器

  设计原则：
  - 导航栏常驻，切换单资产/组合模式
  - 不引入侧边栏等复杂布局，保持扁平
-->
<template>
  <el-container class="app-container">
    <!-- 顶部导航栏 -->
    <el-header class="app-header">
      <div class="header-left">
        <h1 class="app-title">Quanter 量化回测平台</h1>
      </div>
      <div class="header-center">
        <el-radio-group v-model="currentMode" @change="onModeChange">
          <el-radio-button value="single">单资产回测</el-radio-button>
          <el-radio-button value="portfolio">组合回测</el-radio-button>
        </el-radio-group>
      </div>
      <div class="header-right">
        <el-tag type="success" effect="dark" size="small">
          {{ apiStatus }}
        </el-tag>
      </div>
    </el-header>

    <!-- 主内容区 -->
    <el-main class="app-main">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'

const router = useRouter()

/** 当前模式：single / portfolio */
const currentMode = ref<'single' | 'portfolio'>('single')

/** 后端 API 连接状态 */
const apiStatus = ref<string>('检测中...')

/** 切换模式时跳转路由 */
function onModeChange(mode: 'single' | 'portfolio') {
  if (mode === 'single') {
    router.push('/')
  } else {
    router.push('/portfolio')
  }
}

/** 组件挂载时检测后端健康状态 */
onMounted(async () => {
  try {
    const res = await axios.get('/api/../health')
    apiStatus.value = res.data.status === 'ok' ? 'API 已连接' : 'API 异常'
  } catch {
    apiStatus.value = 'API 未连接'
  }
})
</script>

<style scoped>
.app-container {
  min-height: 100vh;
  background-color: #f5f7fa;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background-color: #fff;
  border-bottom: 1px solid #e4e7ed;
  padding: 0 24px;
  height: 60px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.05);
}

.app-title {
  font-size: 18px;
  font-weight: 600;
  color: #303133;
  margin: 0;
}

.header-center {
  flex: 1;
  display: flex;
  justify-content: center;
}

.app-main {
  padding: 20px;
  max-width: 1400px;
  margin: 0 auto;
  width: 100%;
}
</style>
