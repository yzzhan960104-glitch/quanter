<script setup lang="ts">
/**
 * 回测终端视图（路由 / ）
 *
 * 本视图即为「工业级蜕变」前 App.vue 持有的全屏单页终端 Grid：
 *   左 300px   = ParamForm（参数输入，触发回测）
 *   中央 1fr   = 上 ProChart(70%) + 下 TerminalLogs(30%)
 *   右 250px   = MetricCards（绩效指标）+ PositionsTable（末态持仓）
 *
 * Why 抽视图（T17 路由恢复）：
 * - 上一轮工业级蜕变取消了 <router-view/>，把回测编排上提到 App.vue。T17 需
 *   新增 /dashboard 宏观驾驶舱，必须恢复 vue-router，故把终端 Grid 整体下移
 *   到本视图，App.vue 退化为「顶部导航 + router-view」的纯壳。
 * - 状态共享经 useTerminalState 模块级 reactive 单例完成，无需 Pinia；视图
 *   切换不会丢失回测状态（result/logs 仍由单例持有）。
 *
 * 桥接（移植自原 App.vue）：
 * - ParamForm `@submit` 的 payload 字段已与 SingleBacktestParams 完全对齐，
 *   故直接把 execute 绑到 @submit，无需任何字段映射——与原 App.vue 行为一致。
 */
import ParamForm from '../components/ParamForm.vue'
import ProChart from '../components/ProChart.vue'
import TerminalLogs from '../components/TerminalLogs.vue'
import MetricCards from '../components/MetricCards.vue'
import PositionsTable from '../components/PositionsTable.vue'
import { useTerminalState } from '../composables/useTerminalState'

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
/*
 * 终端外壳：三列 Grid，撑满路由出口剩余空间，整体不滚动（各面板内部各自滚动）。
 *
 * Why flex:1 + min-height:0 而非 height:100vh：
 *   App.vue 现已退化为「顶部导航(36px) + router-view」纵向 flex 壳，本视图是
 *   router-view 的内容，需填满除导航外的剩余高度。用 height:100vh 会溢出底部
 *   36px 导致出现整页滚动条；改用 flex:1 + min-height:0 让 Grid 在 flex 父容器
 *   里正确收缩占满剩余高度（min-height:0 是 flex 子项允许收缩的关键）。
 */
.terminal-shell {
  display: grid;
  grid-template-columns: 300px 1fr 250px;
  flex: 1;
  min-height: 0;
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
