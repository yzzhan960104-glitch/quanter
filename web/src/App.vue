<script setup lang="ts">
/**
 * 全局终端布局（CSS Grid，100vh 无滚动）
 *
 * 布局结构：
 *   左 300px   = ParamForm（参数输入，触发回测）
 *   中央 1fr   = 上 ProChart(70%) + 下 TerminalLogs(30%)
 *   右 250px   = MetricCards（绩效指标）+ PositionsTable（末态持仓）
 *
 * 设计意图（全局终端）：
 * - 取消 `<router-view/>`：把 `/` 路由的"ParamForm → runSingleBacktest → 喂给图表/指标"
 *   这套编排原由 SingleBacktest.vue 持有，现上提到 App.vue，由 useTerminalState 组合式共享。
 *   全屏单页终端即整个应用，/portfolio 组合页的接入作为后续迭代。
 * - 暗黑由 main.ts 在 <html> 上挂 .dark 类强制开启，本组件仅给终端底色 #0d1117。
 * - 不引入 Pinia：状态共享经 useTerminalState 模块级 reactive 单例完成。
 *
 * 桥接（移植自 SingleBacktest.vue.onSubmit）：
 * - ParamForm `@submit` 的 payload 字段（symbol/start_date/end_date/initial_capital/
 *   signal_freq/strategy_name/strategy_params）已与 SingleBacktestParams 完全对齐，
 *   故直接把 execute 绑到 @submit，无需任何字段映射——与原视图行为一致。
 */
import ParamForm from './components/ParamForm.vue'
import ProChart from './components/ProChart.vue'
import TerminalLogs from './components/TerminalLogs.vue'
import MetricCards from './components/MetricCards.vue'
import PositionsTable from './components/PositionsTable.vue'
import { useTerminalState } from './composables/useTerminalState'

// 解构拿到的 loading/result/error 均为 ref（toRefs 保证响应性），execute 触发回测
const { loading, result, error, execute } = useTerminalState()
</script>

<template>
  <div class="terminal-shell">
    <!-- 左栏：参数表单。@submit 直接绑 execute——payload 形状即 SingleBacktestParams -->
    <aside class="panel panel-left">
      <ParamForm mode="single" :loading="loading" @submit="execute" />
    </aside>

    <!-- 中央：上 K 线图 70% / 下 日志终端 30% -->
    <main class="panel-center">
      <section class="center-chart">
        <!-- 有结果才渲染图表，避免 ProChart 对空数组计算 markPoint/坐标轴时报错 -->
        <ProChart
          v-if="result"
          :ohlcv="result.ohlcv"
          :nav-series="result.nav_series"
          :trades="result.trades"
        />
        <el-empty
          v-else
          description="提交左侧参数后在此显示 K 线与买卖点"
          :image-size="80"
        />
      </section>
      <section class="center-logs">
        <TerminalLogs />
      </section>
    </main>

    <!-- 右栏：绩效指标卡 + 末态持仓快照 -->
    <aside class="panel panel-right">
      <!-- metrics 可能为 null（首次未提交），MetricCards 内部已做空态兜底 -->
      <MetricCards :metrics="result?.metrics ?? null" />
      <PositionsTable :positions="result?.positions ?? []" />
      <!-- 错误兜底红字（HTTP 错误已由 Axios 拦截器 ElMessage 弹窗，此处仅留痕） -->
      <div v-if="error" class="err-tip">{{ error }}</div>
    </aside>
  </div>
</template>

<style scoped>
/* 终端外壳：三列 Grid，撑满视口，整体不滚动（各面板内部各自滚动） */
.terminal-shell {
  display: grid;
  grid-template-columns: 300px 1fr 250px;
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  background: #0d1117; /* GitHub 暗黑底色，与各面板 #161b22 形成层级 */
}

/* 左右栏内部可纵向滚动，避免参数过多时溢出 */
.panel {
  overflow: auto;
}

.panel-left {
  border-right: 1px solid #30363d;
}

.panel-right {
  border-left: 1px solid #30363d;
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
}

/* 中央：上下两行 Grid，分别 70% / 30%，整体不滚动（图表与日志各自管理溢出） */
.panel-center {
  display: grid;
  grid-template-rows: 70% 30%;
  overflow: hidden;
}

/* 图表区：给底部留细分隔线，内部 hidden 防止 ECharts 撑爆容器 */
.center-chart {
  border-bottom: 1px solid #30363d;
  padding: 4px;
  overflow: hidden;
}

.center-logs {
  overflow: hidden;
}

/* 错误提示：终端红字，低饱和度避免抢眼 */
.err-tip {
  color: #f85149;
  font-size: 12px;
  padding: 4px;
}
</style>
