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
 *   （Epic 2 后 ParamForm 内部已把 symbol 劫持为 'dynamic_top50' 池子代号，
 *    字段形状仍是 { symbol: string, ... }，契约不变。）
 */
import ParamForm from '../components/ParamForm.vue'
import ProChart from '../components/ProChart.vue'
import TerminalLogs from '../components/TerminalLogs.vue'
import MetricCards from '../components/MetricCards.vue'
import PositionsTable from '../components/PositionsTable.vue'
import TerminalWatermark from '../components/TerminalWatermark.vue'
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
        <!-- 空态：极简水印替代 el-empty 纸箱子，传达「等待回测」而非「无数据」 -->
        <TerminalWatermark
          v-else
          subtitle="提交左侧参数后在此显示 K 线与买卖点"
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
 * 悬浮卡片化（Epic 1 呼吸感布局）：
 * - 外围 padding:8px + 列间 gap:8px，让三栏从「贴边分隔」变为「悬浮独立卡片」，
 *   打破原 border-right/border-left 的拥挤贴边感。
 * - 每个面板独立 #1e222d 卡片底 + 1px #2b3139 边框 + 6px 圆角，视觉脱离极夜黑底。
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
  gap: 8px;            /* 三栏呼吸间距，悬浮卡片感的关键 */
  padding: 8px;        /* 外围留白，让面板脱离视口边缘 */
  flex: 1;
  min-height: 0;
  overflow: hidden;
  background: #131722; /* 极夜黑底，衬托 #1e222d 卡片 */
}

/* 通用面板：悬浮卡片（暗底 + 极弱灰边 + 圆角 + 纵向可滚动） */
.panel {
  background: #1e222d;
  border: 1px solid #2b3139;
  border-radius: 6px;
  overflow: auto;
}

/* 左栏 ParamForm 内部自带 padding，这里不重复加，避免双倍留白 */

/* 右栏：内部纵向排列指标卡 / 持仓表 / 错误兜底 */
.panel-right {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 8px;
}

/* 中央列：仅作容器，不画卡片边框；上下两个子卡片自绘 */
.panel-center {
  display: grid;
  grid-template-rows: 70% 30%;
  gap: 8px;            /* 上下子卡片间同样保留呼吸间距 */
  overflow: hidden;
}

/* 图表区与日志区：各自独立子卡片，去贴边感；内部 hidden 防止 ECharts 撑爆容器 */
.center-chart,
.center-logs {
  background: #1e222d;
  border: 1px solid #2b3139;
  border-radius: 6px;
  overflow: hidden;
}

/* 图表区给一点内边距，让 K 线与卡片边框留出呼吸 */
.center-chart {
  padding: 4px;
}

/* 错误提示：终端红字，低饱和度避免抢眼 */
.err-tip {
  color: #ef5350;
  font-size: 12px;
  padding: 4px;
}
</style>
